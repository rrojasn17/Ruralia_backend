from __future__ import annotations

import os
import uuid
from io import BytesIO
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from sqlalchemy import func, or_
from sqlalchemy.orm import Session, joinedload

from core.models import Usuario
from core.module_manager import require_module_permission
from core.routers.auth import get_current_user
from database import get_db
from modules.mod_floresvolcan.field_ai import interpret_field_text, transcribe_field_audio
from modules.mod_floresvolcan.models import (
    BackupRun, CropCatalogValue, FlowerCrop, GoodsReceipt, GoodsReceiptLine, HarvestRecord, InventoryIssue, InventoryLot, InventoryMovement,
    PlantingOrder, PlantingRecord, ProductionBatch, ProductionSpace, PurchaseOrder,
    PurchaseOrderEvent, PurchaseOrderEventAttachment, PurchaseOrderLine, Requisition, RequisitionDelivery,
    RequisitionLine, SapProduct, Supplier, SupplierContact, TechnicalClosure, Worker,
    WorkCrew, WorkCrewMember, ModuleSetting,
)
from modules.mod_floresvolcan.schemas import (
    CrewIn, EventIn, FieldInterpretIn, GoodsReceiptIn, HarvestRecordIn, InventoryWriteoffIn, PlantingOrderIn,
    PlantingRecordIn, ProductionSpaceIn, PurchaseOrderIn, RequisitionIn, SapProductIn, SapProductStatusIn,
    SupplierIn, TechnicalClosureIn, WorkerIn, CropCatalogValueIn, CropCatalogValuePatch, RequisitionDeliveryIn, BackupSettingsIn,
)
from fastapi.responses import FileResponse, StreamingResponse
from modules.mod_floresvolcan.legacy_excel import build_lirio_workbook, build_excel_bundle
from modules.mod_floresvolcan.backups import public_settings as backup_public_settings, set_settings as backup_set_settings, run_backup
from modules.mod_floresvolcan.workers_excel import build_worker_template, import_workers

router = APIRouter(prefix="/floresvolcan", tags=["floresvolcan"])


def now() -> datetime:
    return datetime.now(timezone.utc)


def _decimal(value) -> Decimal:
    return Decimal(str(value or 0))


def _next(prefix: str, count: int, width: int = 6) -> str:
    return f"{prefix}-{now().year}-{count + 1:0{width}d}"


def _next_supplier_code(db: Session) -> str:
    # No derive el consecutivo únicamente del COUNT: un proveedor eliminado
    # podría hacer reutilizar un código todavía existente.
    seq = db.query(Supplier).count() + 1
    while True:
        code = f"PRV-{seq:05d}"
        if not db.query(Supplier.id).filter(Supplier.code == code).first():
            return code
        seq += 1


def _iso_week(value: date | datetime) -> str:
    target = value.date() if isinstance(value, datetime) else value
    iso = target.isocalendar()
    return f"{str(iso.year)[-2:]}-{iso.week:02d}"


def _projected_week(value: date | datetime, cycle_weeks: int | None) -> str | None:
    if not cycle_weeks:
        return None
    return _iso_week(value + timedelta(weeks=int(cycle_weeks)))


def _belongs_to(db: Session, space: ProductionSpace, ancestor_id: int) -> bool:
    current = space
    visited: set[int] = set()
    while current and current.id not in visited:
        if current.id == ancestor_id:
            return True
        visited.add(current.id)
        if not current.parent_id:
            return False
        current = db.query(ProductionSpace).filter(ProductionSpace.id == current.parent_id).first()
    return False


def _ancestor_of_type(db: Session, space: ProductionSpace, wanted_type: str) -> ProductionSpace | None:
    current = space
    visited: set[int] = set()
    while current and current.id not in visited:
        if current.type == wanted_type:
            return current
        visited.add(current.id)
        if not current.parent_id:
            return None
        current = db.query(ProductionSpace).filter(ProductionSpace.id == current.parent_id).first()
    return None


def _obj(row, fields):
    return {field: getattr(row, field) for field in fields}


def _user_label(db: Session, user_id: int | None) -> str | None:
    if not user_id:
        return None
    user = db.query(Usuario).filter(Usuario.id == user_id).first()
    return user.nombre if user else None


def _lily_crop(db: Session) -> FlowerCrop | None:
    return db.query(FlowerCrop).filter(FlowerCrop.code == "LIRIO").first()


def _catalog_item(db: Session, crop_id: int | None, item_id: int | None, category: str, *, required: bool = False) -> CropCatalogValue | None:
    if not item_id:
        if required:
            raise HTTPException(422, f"Debe seleccionar {category}")
        return None
    row = db.query(CropCatalogValue).filter(CropCatalogValue.id == item_id).first()
    if not row or row.crop_id != crop_id or row.category != category:
        raise HTTPException(422, f"Valor inválido para catálogo {category}")
    if not row.active:
        raise HTTPException(422, f"El valor seleccionado de {category} está inactivo")
    return row


def _resolve_catalog(db: Session, crop_id: int | None, item_id: int | None, category: str, legacy_text: str | None = None, *, required: bool = False) -> CropCatalogValue | None:
    if item_id:
        return _catalog_item(db, crop_id, item_id, category, required=required)
    if legacy_text and crop_id:
        key = legacy_text.strip()
        row = db.query(CropCatalogValue).filter(
            CropCatalogValue.crop_id == crop_id, CropCatalogValue.category == category,
            or_(func.lower(CropCatalogValue.code) == key.lower(), func.lower(CropCatalogValue.label) == key.lower())
        ).first()
        if row and row.active:
            return row
    if required:
        raise HTTPException(422, f"Debe seleccionar {category} desde Configuración → Lirios")
    return None


def _apply_product_catalogs(db: Session, product: SapProduct, payload: SapProductIn) -> None:
    crop = db.query(FlowerCrop).filter(FlowerCrop.id == product.crop_id).first() if product.crop_id else _lily_crop(db)
    crop_id = crop.id if crop else None
    is_lily = bool(crop and crop.code == "LIRIO")
    # Variedad y calibre son la identificación comercial mínima de un bulbo de Lirio.
    variety = _resolve_catalog(db, crop_id, payload.variety_catalog_id, "variety", payload.variety, required=is_lily)
    caliber = _resolve_catalog(db, crop_id, payload.caliber_catalog_id, "caliber", payload.caliber, required=is_lily)
    common = _resolve_catalog(db, crop_id, payload.common_catalog_id, "common", payload.common_code)
    color = _resolve_catalog(db, crop_id, payload.color_catalog_id, "color", payload.color)
    product.variety_catalog_id = variety.id if variety else None
    product.caliber_catalog_id = caliber.id if caliber else None
    product.common_catalog_id = common.id if common else None
    product.color_catalog_id = color.id if color else None
    # Los textos se mantienen como snapshot/compatibilidad para los Excel legacy.
    product.variety = variety.label if variety else (payload.variety.strip() if payload.variety else None)
    product.caliber = caliber.label if caliber else (payload.caliber.strip() if payload.caliber else None)
    product.common_code = common.code if common else (payload.common_code.strip().upper() if payload.common_code else None)
    product.color = color.label if color else (payload.color.strip() if payload.color else None)
    product.purchase_unit = payload.purchase_unit.strip().lower()
    product.inventory_unit = payload.inventory_unit.strip().lower()
    product.unit = product.inventory_unit
    product.units_per_purchase_unit = payload.units_per_purchase_unit


def _field_worker(db: Session, current: Usuario) -> Worker | None:
    if current.is_superadmin:
        return None
    if str(current.rol or "").strip().lower() != "trabajador":
        raise HTTPException(403, "El acceso de campo es exclusivo para trabajadores")
    worker = db.query(Worker).filter(Worker.user_id == current.id, Worker.active.is_(True)).first()
    if not worker:
        raise HTTPException(403, "Su usuario trabajador no está vinculado a un trabajador activo")
    return worker


def supplier_dict(row: Supplier):
    data = _obj(row, ["id", "code", "name", "country", "department", "address", "phone", "email", "website", "active", "created_at", "updated_at"])
    data["contacts"] = [_obj(c, ["id", "name", "email", "phone", "position", "active"]) for c in row.contacts]
    return data


def product_dict(row: SapProduct):
    return _obj(row, [
        "id", "crop_id", "code", "description", "material_type", "last_price", "currency", "unit", "active",
        "purchase_unit", "inventory_unit", "units_per_purchase_unit",
        "variety_catalog_id", "caliber_catalog_id", "common_catalog_id", "color_catalog_id",
        "variety", "caliber", "common_code", "color", "default_density",
        "default_cycle_weeks", "created_at", "updated_at"
    ])


def po_dict(row: PurchaseOrder, include_detail=True, db: Session | None = None):
    data = _obj(row, ["id", "number", "supplier_id", "requested_by_id", "approver_user_id", "warehouse_due_date", "status", "comments", "subtotal", "tax_total", "total_estimated", "approved_at", "approved_by_id", "completed_at", "cancelled_at", "created_at", "updated_at"])
    data["supplier_name"] = row.supplier.name if row.supplier else None
    if db is not None:
        data["requested_by_name"] = _user_label(db, row.requested_by_id)
        data["approver_name"] = _user_label(db, row.approver_user_id)
        data["approved_by_name"] = _user_label(db, row.approved_by_id)
    if include_detail:
        data["lines"] = []
        for x in row.lines:
            item = _obj(x, ["id", "sap_product_id", "product_code_snapshot", "description_snapshot",
                     "variety_snapshot", "caliber_snapshot", "common_code_snapshot", "color_snapshot",
                     "purchase_unit_snapshot", "inventory_unit_snapshot", "units_per_purchase_unit_snapshot", "currency_snapshot",
                     "quantity", "unit_price_estimated", "apply_tax", "tax_rate", "line_subtotal", "line_tax", "line_total"])
            if db is not None:
                received_expr = func.coalesce(GoodsReceiptLine.purchase_quantity_received, GoodsReceiptLine.boxes_received, GoodsReceiptLine.quantity_received)
                item["purchase_quantity_received"] = _decimal(db.query(func.coalesce(func.sum(received_expr), 0)).filter(GoodsReceiptLine.purchase_order_line_id == x.id).scalar())
                item["purchase_quantity_pending"] = max(Decimal("0"), _decimal(x.quantity) - _decimal(item["purchase_quantity_received"]))
            data["lines"].append(item)
        data["events"] = []
        for e in sorted(row.events, key=lambda item: item.event_date):
            event = {**_obj(e, ["id", "user_id", "event_type", "message", "event_date", "created_at"]),
                     "attachments": [{**_obj(a, ["id", "filename", "mime_type", "size"]), "download_url": f"/floresvolcan/purchase-orders/attachments/{a.id}"} for a in e.attachments]}
            if db is not None:
                event["user_name"] = _user_label(db, e.user_id) or "Sistema"
            data["events"].append(event)
    return data


@router.get("/dashboard")
def dashboard(current: Usuario = Depends(require_module_permission("floresvolcan:dashboard:view")), db: Session = Depends(get_db)):
    return {
        "module": "mod_floresvolcan",
        "user": current.nombre,
        "suppliers": db.query(Supplier).filter(Supplier.active).count(),
        "purchase_orders_pending": db.query(PurchaseOrder).filter(PurchaseOrder.status == "pending_approval").count(),
        "inventory_lots": db.query(InventoryLot).count(),
        "planting_orders_open": db.query(PlantingOrder).filter(PlantingOrder.status.in_(["pending_approval", "approved", "in_progress"])).count(),
        "batches_open": db.query(ProductionBatch).filter(ProductionBatch.status == "open").count(),
    }


# ---------- Proveedores ----------
@router.get("/suppliers")
def list_suppliers(current=Depends(require_module_permission("floresvolcan:suppliers:view")), db: Session = Depends(get_db)):
    del current
    return [supplier_dict(x) for x in db.query(Supplier).options(joinedload(Supplier.contacts)).order_by(Supplier.name).all()]


@router.get("/suppliers/{supplier_id}")
def get_supplier(supplier_id: int, current=Depends(require_module_permission("floresvolcan:suppliers:view")), db: Session = Depends(get_db)):
    del current
    row = db.query(Supplier).options(joinedload(Supplier.contacts)).filter(Supplier.id == supplier_id).first()
    if not row:
        raise HTTPException(404, "Proveedor no encontrado")
    orders = db.query(PurchaseOrder).options(joinedload(PurchaseOrder.supplier)).filter(PurchaseOrder.supplier_id == supplier_id).order_by(PurchaseOrder.created_at.desc()).all()
    data = supplier_dict(row)
    data["orders"] = [po_dict(x, False, db=db) for x in orders]
    data["summary"] = {
        "orders_total": len(orders),
        "orders_pending": sum(1 for x in orders if x.status in {"pending_approval", "in_process"}),
        "completed": sum(1 for x in orders if x.status == "completed"),
        "total_estimated": sum((_decimal(x.total_estimated) for x in orders), Decimal("0")),
    }
    return data


@router.post("/suppliers", status_code=201)
def create_supplier(payload: SupplierIn, current=Depends(require_module_permission("floresvolcan:suppliers:manage")), db: Session = Depends(get_db)):
    del current
    code = (payload.code or _next_supplier_code(db)).strip().upper()
    if db.query(Supplier).filter(Supplier.code == code).first():
        raise HTTPException(409, "Código de proveedor ya existe")
    row = Supplier(code=code, **payload.model_dump(exclude={"code", "contacts"}))
    row.contacts = [SupplierContact(**item.model_dump()) for item in payload.contacts]
    db.add(row); db.commit(); db.refresh(row)
    return supplier_dict(row)


@router.put("/suppliers/{supplier_id}")
def update_supplier(supplier_id: int, payload: SupplierIn, current=Depends(require_module_permission("floresvolcan:suppliers:manage")), db: Session = Depends(get_db)):
    del current
    row = db.query(Supplier).options(joinedload(Supplier.contacts)).filter(Supplier.id == supplier_id).first()
    if not row: raise HTTPException(404, "Proveedor no encontrado")
    data = payload.model_dump(exclude={"contacts", "code"})
    for k, v in data.items(): setattr(row, k, v)
    if payload.code and payload.code.strip().upper() != row.code:
        candidate = payload.code.strip().upper()
        if db.query(Supplier).filter(Supplier.code == candidate, Supplier.id != row.id).first(): raise HTTPException(409, "Código de proveedor ya existe")
        row.code = candidate
    row.contacts.clear(); db.flush()
    row.contacts.extend(SupplierContact(**item.model_dump()) for item in payload.contacts)
    db.commit(); db.refresh(row)
    return supplier_dict(row)


@router.delete("/suppliers/{supplier_id}")
def delete_supplier(supplier_id: int, current=Depends(require_module_permission("floresvolcan:suppliers:manage")), db: Session = Depends(get_db)):
    del current
    row = db.query(Supplier).filter(Supplier.id == supplier_id).first()
    if not row:
        raise HTTPException(404, "Proveedor no encontrado")
    pending = db.query(PurchaseOrder).filter(PurchaseOrder.supplier_id == supplier_id, PurchaseOrder.status.in_(["pending_approval", "in_process"])).count()
    if pending:
        raise HTTPException(409, f"No se puede desactivar: existen {pending} orden(es) pendiente(s) o en proceso")
    history = db.query(PurchaseOrder).filter(PurchaseOrder.supplier_id == supplier_id).count()
    if history:
        row.active = False
        db.commit()
        return {"ok": True, "soft_deleted": True, "active": False}
    db.delete(row)
    db.commit()
    return {"ok": True, "deleted": True}


@router.get("/purchase-suppliers")
def purchase_supplier_options(current=Depends(require_module_permission("floresvolcan:purchases:create")), db: Session = Depends(get_db)):
    del current
    rows = db.query(Supplier).filter(Supplier.active.is_(True)).order_by(Supplier.name).all()
    return [{"id": row.id, "code": row.code, "name": row.name, "country": row.country, "active": bool(row.active)} for row in rows]

@router.get("/purchase-order-options")
def purchase_order_options(current=Depends(require_module_permission("floresvolcan:purchases:create")), db: Session = Depends(get_db)):
    """Opciones del wizard sin depender del endpoint global de usuarios."""
    del current
    suppliers=db.query(Supplier).filter(Supplier.active.is_(True)).order_by(Supplier.name).all()
    approvers=db.query(Usuario).filter(Usuario.activo.is_(True),or_(Usuario.is_superadmin.is_(True),func.lower(Usuario.rol).in_(["admin","administrador","gerente"]))).order_by(Usuario.nombre).all()
    return {
        "suppliers":[{"id":x.id,"code":x.code,"name":x.name,"country":x.country,"active":bool(x.active)} for x in suppliers],
        "approvers":[{"id":x.id,"nombre":x.nombre,"rol":x.rol} for x in approvers],
    }


# ---------- SAP ----------
@router.get("/sap-products")
def list_products(q: str | None = None, active: bool | None = None, current=Depends(require_module_permission("floresvolcan:sap:view")), db: Session = Depends(get_db)):
    del current
    query = db.query(SapProduct)
    if active is not None: query = query.filter(SapProduct.active == active)
    if q: query = query.filter((SapProduct.code.ilike(f"%{q}%")) | (SapProduct.description.ilike(f"%{q}%")) | (SapProduct.variety.ilike(f"%{q}%")))
    return [product_dict(x) for x in query.order_by(SapProduct.code).limit(200).all()]


@router.get("/sap-options")
def sap_options(current=Depends(require_module_permission("floresvolcan:sap:view")), db: Session = Depends(get_db)):
    del current
    crop = _lily_crop(db)
    values = [] if not crop else db.query(CropCatalogValue).filter(CropCatalogValue.crop_id == crop.id, CropCatalogValue.active.is_(True)).order_by(CropCatalogValue.category, CropCatalogValue.sort_order, CropCatalogValue.label).all()
    grouped: dict[str, list[dict]] = {}
    for item in values:
        grouped.setdefault(item.category, []).append(_obj(item, ["id", "code", "label", "sort_order"]))
    return {
        "crop": _obj(crop, ["id", "code", "name", "material_type"]) if crop else None,
        "catalogs": grouped,
        "purchase_units": [
            {"value": "caja", "label": "Caja"}, {"value": "unidad", "label": "Unidad"},
            {"value": "bandeja", "label": "Bandeja"}, {"value": "paquete", "label": "Paquete"},
        ],
        "inventory_units": [
            {"value": "bulbo", "label": "Bulbo"}, {"value": "unidad", "label": "Unidad"},
            {"value": "tallo", "label": "Tallo"}, {"value": "planta", "label": "Planta"},
        ],
    }


@router.get("/sap-products/code/{code}")
def product_by_code(code: str, current=Depends(require_module_permission("floresvolcan:sap:view")), db: Session = Depends(get_db)):
    del current
    row = db.query(SapProduct).filter(func.lower(SapProduct.code) == code.strip().lower()).first()
    if not row: raise HTTPException(404, "Código SAP no encontrado")
    return product_dict(row)


@router.get("/purchase-catalog/code/{code}")
def purchase_catalog_by_code(code: str, current=Depends(require_module_permission("floresvolcan:purchases:create")), db: Session = Depends(get_db)):
    del current
    row = db.query(SapProduct).filter(func.lower(SapProduct.code) == code.strip().lower(), SapProduct.active.is_(True)).first()
    if not row:
        raise HTTPException(404, "Código SAP no encontrado o inactivo")
    return product_dict(row)


@router.patch("/sap-products/{product_id}/status")
def set_product_status(product_id: int, payload: SapProductStatusIn, current=Depends(require_module_permission("floresvolcan:sap:manage")), db: Session = Depends(get_db)):
    del current
    row = db.query(SapProduct).filter(SapProduct.id == product_id).first()
    if not row:
        raise HTTPException(404, "Producto no encontrado")
    row.active = payload.active
    db.commit()
    db.refresh(row)
    return product_dict(row)


@router.post("/sap-products", status_code=201)
def create_product(payload: SapProductIn, current: Usuario = Depends(require_module_permission("floresvolcan:sap:manage")), db: Session = Depends(get_db)):
    code = payload.code.strip().upper()
    if db.query(SapProduct).filter(SapProduct.code == code).first():
        raise HTTPException(409, "Código SAP ya existe")
    crop = _lily_crop(db)
    row = SapProduct(
        code=code, description=payload.description.strip(), crop_id=(crop.id if crop else None), created_by_id=current.id,
        material_type=payload.material_type.strip().lower(), last_price=payload.last_price, currency=payload.currency,
        active=payload.active, default_density=payload.default_density, default_cycle_weeks=payload.default_cycle_weeks,
    )
    _apply_product_catalogs(db, row, payload)
    db.add(row); db.commit(); db.refresh(row)
    return product_dict(row)


@router.put("/sap-products/{product_id}")
def update_product(product_id: int, payload: SapProductIn, current=Depends(require_module_permission("floresvolcan:sap:manage")), db: Session = Depends(get_db)):
    del current
    row = db.query(SapProduct).filter(SapProduct.id == product_id).first()
    if not row:
        raise HTTPException(404, "Producto no encontrado")
    used = db.query(PurchaseOrderLine).filter(PurchaseOrderLine.sap_product_id == product_id).first()
    new_code = payload.code.strip().upper()
    if used and new_code != row.code:
        raise HTTPException(409, "El código SAP no puede cambiar después de usarse en una transacción")
    duplicate = db.query(SapProduct.id).filter(SapProduct.code == new_code, SapProduct.id != row.id).first()
    if duplicate:
        raise HTTPException(409, "Código SAP ya existe")
    row.code = new_code
    row.description = payload.description.strip()
    row.material_type = payload.material_type.strip().lower()
    row.last_price = payload.last_price
    row.currency = payload.currency
    row.active = payload.active
    row.default_density = payload.default_density
    row.default_cycle_weeks = payload.default_cycle_weeks
    _apply_product_catalogs(db, row, payload)
    db.commit(); db.refresh(row)
    return product_dict(row)


@router.delete("/sap-products/{product_id}")
def deactivate_product(product_id: int, current=Depends(require_module_permission("floresvolcan:sap:manage")), db: Session = Depends(get_db)):
    del current
    row = db.query(SapProduct).filter(SapProduct.id == product_id).first()
    if not row: raise HTTPException(404, "Producto no encontrado")
    row.active = False; db.commit(); return {"ok": True, "active": False}


# ---------- Compras ----------
@router.get("/purchase-orders")
def list_purchase_orders(status: str | None = None, current=Depends(require_module_permission("floresvolcan:purchases:view")), db: Session = Depends(get_db)):
    del current
    q = db.query(PurchaseOrder).options(joinedload(PurchaseOrder.supplier))
    if status: q = q.filter(PurchaseOrder.status == status)
    return [po_dict(x, False, db=db) for x in q.order_by(PurchaseOrder.created_at.desc()).all()]


@router.get("/purchase-orders/{po_id}")
def get_purchase_order(po_id: int, current=Depends(require_module_permission("floresvolcan:purchases:view")), db: Session = Depends(get_db)):
    del current
    row = db.query(PurchaseOrder).options(joinedload(PurchaseOrder.supplier), joinedload(PurchaseOrder.lines), joinedload(PurchaseOrder.events).joinedload(PurchaseOrderEvent.attachments)).filter(PurchaseOrder.id == po_id).first()
    if not row: raise HTTPException(404, "Orden de compra no encontrada")
    return po_dict(row, db=db)


@router.post("/purchase-orders", status_code=201)
def create_purchase_order(payload: PurchaseOrderIn, current: Usuario = Depends(require_module_permission("floresvolcan:purchases:create")), db: Session = Depends(get_db)):
    if not db.query(Supplier).filter(Supplier.id == payload.supplier_id, Supplier.active).first(): raise HTTPException(422, "Proveedor inválido o inactivo")
    if not db.query(Usuario).filter(Usuario.id == payload.approver_user_id, Usuario.activo).first(): raise HTTPException(422, "Usuario aprobador inválido")
    row = PurchaseOrder(number=_next("OC", db.query(PurchaseOrder).count()), supplier_id=payload.supplier_id, requested_by_id=current.id, approver_user_id=payload.approver_user_id, warehouse_due_date=payload.warehouse_due_date, comments=payload.comments, status="pending_approval")
    subtotal = Decimal("0"); tax_total = Decimal("0")
    for item in payload.lines:
        product = db.query(SapProduct).filter(SapProduct.id == item.sap_product_id, SapProduct.active).first()
        if not product: raise HTTPException(422, f"Producto SAP {item.sap_product_id} inválido o inactivo")
        line_subtotal = _decimal(item.quantity) * _decimal(item.unit_price_estimated)
        line_tax = line_subtotal * (_decimal(item.tax_rate) / Decimal("100")) if item.apply_tax else Decimal("0")
        row.lines.append(PurchaseOrderLine(
            sap_product_id=product.id, product_code_snapshot=product.code, description_snapshot=product.description,
            variety_snapshot=product.variety, caliber_snapshot=product.caliber, common_code_snapshot=product.common_code, color_snapshot=product.color,
            purchase_unit_snapshot=product.purchase_unit or "caja", inventory_unit_snapshot=product.inventory_unit or "bulbo",
            units_per_purchase_unit_snapshot=product.units_per_purchase_unit, currency_snapshot=product.currency or "USD",
            quantity=item.quantity, unit_price_estimated=item.unit_price_estimated, apply_tax=item.apply_tax,
            tax_rate=item.tax_rate if item.apply_tax else 0, line_subtotal=line_subtotal, line_tax=line_tax, line_total=line_subtotal + line_tax))
        subtotal += line_subtotal; tax_total += line_tax
    row.subtotal = subtotal; row.tax_total = tax_total; row.total_estimated = subtotal + tax_total
    row.events.append(PurchaseOrderEvent(user_id=current.id, event_type="created", message="Orden creada y enviada a aprobación"))
    db.add(row); db.commit(); db.refresh(row); return po_dict(row, db=db)


@router.post("/purchase-orders/{po_id}/approve")
def approve_purchase_order(po_id: int, current: Usuario = Depends(require_module_permission("floresvolcan:purchases:approve")), db: Session = Depends(get_db)):
    row = db.query(PurchaseOrder).filter(PurchaseOrder.id == po_id).first()
    if not row: raise HTTPException(404, "Orden no encontrada")
    if row.status != "pending_approval": raise HTTPException(409, "La orden ya fue procesada")
    row.status = "in_process"; row.approved_at = now(); row.approved_by_id = current.id
    row.events.append(PurchaseOrderEvent(user_id=current.id, event_type="approved", message="Orden aprobada; timeline habilitado"))
    db.commit(); return {"ok": True, "status": row.status}


@router.post("/purchase-orders/{po_id}/cancel")
def cancel_purchase_order(po_id: int, current: Usuario = Depends(require_module_permission("floresvolcan:purchases:approve")), db: Session = Depends(get_db)):
    row = db.query(PurchaseOrder).filter(PurchaseOrder.id == po_id).first()
    if not row: raise HTTPException(404, "Orden no encontrada")
    if row.status == "completed": raise HTTPException(409, "Una orden completada no se puede cancelar")
    row.status="cancelled"; row.cancelled_at=now(); row.events.append(PurchaseOrderEvent(user_id=current.id, event_type="cancelled", message="Orden cancelada")); db.commit(); return {"ok":True}


@router.post("/purchase-orders/{po_id}/events", status_code=201)
def add_po_event(po_id: int, payload: EventIn, current: Usuario = Depends(require_module_permission("floresvolcan:purchases:update")), db: Session = Depends(get_db)):
    po = db.query(PurchaseOrder).filter(PurchaseOrder.id == po_id).first()
    if not po: raise HTTPException(404, "Orden no encontrada")
    if po.status != "in_process": raise HTTPException(409, "El timeline solo está abierto para órdenes en proceso")
    event = PurchaseOrderEvent(purchase_order_id=po.id, user_id=current.id, event_type=payload.event_type, message=payload.message, event_date=payload.event_date or now())
    db.add(event); db.commit(); db.refresh(event); return _obj(event, ["id", "event_type", "message", "event_date", "created_at"])


@router.post("/purchase-orders/events/{event_id}/attachment", status_code=201)
async def upload_po_attachment(event_id: int, file: UploadFile = File(...), current=Depends(require_module_permission("floresvolcan:purchases:update")), db: Session = Depends(get_db)):
    del current
    event = db.query(PurchaseOrderEvent).filter(PurchaseOrderEvent.id == event_id).first()
    if not event: raise HTTPException(404, "Evento no encontrado")
    content = await file.read()
    if len(content) > 15 * 1024 * 1024: raise HTTPException(413, "Adjunto máximo 15 MB")
    root = Path(os.getenv("NAVIA_UPLOAD_DIR", "uploads")).resolve() / "floresvolcan" / "po_events"
    root.mkdir(parents=True, exist_ok=True)
    safe_name = Path(file.filename or "archivo").name
    target = root / f"{uuid.uuid4().hex}_{safe_name}"
    target.write_bytes(content)
    item = PurchaseOrderEventAttachment(event_id=event.id, filename=safe_name, storage_key=str(target), mime_type=file.content_type, size=len(content))
    db.add(item); db.commit(); db.refresh(item); return _obj(item, ["id", "filename", "mime_type", "size"])


@router.get("/purchase-orders/attachments/{attachment_id}")
def download_po_attachment(attachment_id: int, current=Depends(require_module_permission("floresvolcan:purchases:view")), db: Session = Depends(get_db)):
    del current
    item = db.query(PurchaseOrderEventAttachment).filter(PurchaseOrderEventAttachment.id == attachment_id).first()
    if not item:
        raise HTTPException(404, "Adjunto no encontrado")
    path = Path(item.storage_key)
    if not path.exists() or not path.is_file():
        raise HTTPException(404, "Archivo no disponible")
    return FileResponse(path, filename=item.filename, media_type=item.mime_type or "application/octet-stream")


# ---------- Recepción / Inventario ----------
@router.get("/inventory")
def inventory(current=Depends(require_module_permission("floresvolcan:inventory:view")), db: Session = Depends(get_db)):
    del current
    rows = db.query(InventoryLot).options(joinedload(InventoryLot.product)).order_by(InventoryLot.received_at.desc()).all()
    return [
        {
            **_obj(x, [
                "id", "lot_code", "supplier_lot_code", "sap_product_id", "container_number", "harvest_year",
                "variety_snapshot", "caliber_snapshot", "common_code_snapshot", "color_snapshot",
                "boxes_received", "units_per_box", "purchase_quantity_received", "purchase_unit_snapshot", "inventory_unit_snapshot",
                "quantity_received", "quantity_available", "quantity_reserved", "quantity_issued", "quantity_consumed", "quantity_written_off",
                "storage_status", "received_at"
            ]),
            "product_code": x.product.code,
            "description": x.product.description,
            "variety": x.variety_snapshot or x.product.variety,
            "caliber": x.caliber_snapshot or x.product.caliber,
            "common_code": x.common_code_snapshot or x.product.common_code,
            "color": x.color_snapshot or x.product.color,
        }
        for x in rows
    ]


@router.post("/goods-receipts", status_code=201)
def receive_goods(payload: GoodsReceiptIn, current: Usuario = Depends(require_module_permission("floresvolcan:inventory:receive")), db: Session = Depends(get_db)):
    po = db.query(PurchaseOrder).options(joinedload(PurchaseOrder.lines)).filter(PurchaseOrder.id == payload.purchase_order_id).first()
    if not po:
        raise HTTPException(404, "Orden de compra no encontrada")
    if po.status != "in_process":
        raise HTTPException(409, "Solo se recibe inventario de una orden aprobada/en proceso")

    receipt = GoodsReceipt(
        number=_next("REC", db.query(GoodsReceipt).count()),
        purchase_order_id=po.id, container_number=payload.container_number.strip(),
        received_by_id=current.id, comments=payload.comments,
    )
    db.add(receipt); db.flush()
    po_line_map = {line.id: line for line in po.lines}

    for item in payload.lines:
        line = po_line_map.get(item.purchase_order_line_id)
        if not line:
            raise HTTPException(422, "Una línea no pertenece a la orden seleccionada")
        product = db.query(SapProduct).filter(SapProduct.id == line.sap_product_id).first()
        if not product:
            raise HTTPException(409, "El producto SAP de la línea ya no existe")

        # La OC se controla en unidad de compra (normalmente cajas).
        purchase_qty = _decimal(item.purchase_quantity_received if item.purchase_quantity_received is not None else item.boxes_received)
        if purchase_qty <= 0:
            # Compatibilidad con clientes 0.5.0 que solo enviaban quantity_received.
            if item.quantity_received is not None and not (line.units_per_purchase_unit_snapshot or product.units_per_purchase_unit):
                purchase_qty = _decimal(item.quantity_received)
            else:
                raise HTTPException(422, f"Indique la cantidad recibida en {line.purchase_unit_snapshot or product.purchase_unit or 'unidad de compra'} para {line.product_code_snapshot}")

        already_expr = func.coalesce(GoodsReceiptLine.purchase_quantity_received, GoodsReceiptLine.boxes_received, GoodsReceiptLine.quantity_received)
        already = _decimal(db.query(func.coalesce(func.sum(already_expr), 0)).filter(GoodsReceiptLine.purchase_order_line_id == line.id).scalar())
        if already + purchase_qty > _decimal(line.quantity):
            raise HTTPException(409, f"La recepción excede lo ordenado para {line.product_code_snapshot}: ordenado {line.quantity} {line.purchase_unit_snapshot or 'unidades'}, recibido previamente {already}")

        units_per = _decimal(item.units_per_purchase_unit or item.units_per_box or line.units_per_purchase_unit_snapshot or product.units_per_purchase_unit)
        inventory_qty = _decimal(item.quantity_received) if item.quantity_received is not None else Decimal("0")
        if units_per > 0:
            calculated = purchase_qty * units_per
            if inventory_qty > 0 and abs(calculated - inventory_qty) > Decimal("0.001"):
                raise HTTPException(422, f"{purchase_qty} {line.purchase_unit_snapshot or product.purchase_unit} × {units_per} {line.inventory_unit_snapshot or product.inventory_unit} no coincide con el total recibido para {line.product_code_snapshot}")
            inventory_qty = calculated
        elif inventory_qty <= 0:
            raise HTTPException(422, f"Configure unidades por {line.purchase_unit_snapshot or product.purchase_unit} en SAP o indíquelas en la recepción de {line.product_code_snapshot}")

        boxes = purchase_qty if (line.purchase_unit_snapshot or product.purchase_unit) == "caja" else item.boxes_received
        rec_line = GoodsReceiptLine(
            purchase_order_line_id=line.id,
            quantity_received=inventory_qty, inventory_quantity_received=inventory_qty, purchase_quantity_received=purchase_qty,
            supplier_lot_code=(item.supplier_lot_code or "").strip() or None, harvest_year=item.harvest_year,
            boxes_received=boxes, units_per_box=(units_per if (line.purchase_unit_snapshot or product.purchase_unit) == "caja" else item.units_per_box),
        )
        receipt.lines.append(rec_line); db.flush()

        lot = InventoryLot(
            lot_code=_next("LOT", db.query(InventoryLot).count()), supplier_lot_code=rec_line.supplier_lot_code,
            sap_product_id=line.sap_product_id, goods_receipt_line_id=rec_line.id, container_number=receipt.container_number,
            harvest_year=rec_line.harvest_year, variety_snapshot=line.variety_snapshot or product.variety,
            caliber_snapshot=line.caliber_snapshot or product.caliber, common_code_snapshot=line.common_code_snapshot or product.common_code,
            color_snapshot=line.color_snapshot or product.color, boxes_received=rec_line.boxes_received, units_per_box=rec_line.units_per_box,
            purchase_quantity_received=purchase_qty, purchase_unit_snapshot=line.purchase_unit_snapshot or product.purchase_unit,
            inventory_unit_snapshot=line.inventory_unit_snapshot or product.inventory_unit,
            quantity_received=inventory_qty, quantity_available=inventory_qty, quantity_reserved=0, quantity_issued=0,
            quantity_consumed=0, quantity_written_off=0, storage_status="refrigerated",
        )
        db.add(lot); db.flush()
        db.add(InventoryMovement(
            inventory_lot_id=lot.id, movement_type="RECEIPT", quantity=inventory_qty,
            reference_type="goods_receipt", reference_id=receipt.id, user_id=current.id,
            comments=f"Recepción {purchase_qty} {line.purchase_unit_snapshot or product.purchase_unit}; {inventory_qty} {line.inventory_unit_snapshot or product.inventory_unit}",
        ))
        # El último precio pasa a ser el último precio de una compra efectivamente recibida.
        product.last_price = line.unit_price_estimated

    db.flush()
    all_complete = True
    for line in po.lines:
        already_expr = func.coalesce(GoodsReceiptLine.purchase_quantity_received, GoodsReceiptLine.boxes_received, GoodsReceiptLine.quantity_received)
        total = _decimal(db.query(func.coalesce(func.sum(already_expr), 0)).filter(GoodsReceiptLine.purchase_order_line_id == line.id).scalar())
        if total < _decimal(line.quantity):
            all_complete = False
    if all_complete:
        po.status = "completed"; po.completed_at = now()
        po.events.append(PurchaseOrderEvent(user_id=current.id, event_type="completed", message=f"Orden completada con recepción del contenedor {receipt.container_number}"))
    else:
        po.events.append(PurchaseOrderEvent(user_id=current.id, event_type="container_update", message=f"Recepción parcial del contenedor {receipt.container_number}"))
    db.commit(); db.refresh(receipt)
    return {"id": receipt.id, "number": receipt.number, "container_number": receipt.container_number, "purchase_order_id": po.id, "po_status": po.status}


@router.post("/inventory/{lot_id}/writeoff")
def writeoff_inventory(lot_id: int, payload: InventoryWriteoffIn, current: Usuario = Depends(require_module_permission("floresvolcan:inventory:writeoff")), db: Session = Depends(get_db)):
    lot = db.query(InventoryLot).filter(InventoryLot.id == lot_id).with_for_update().first()
    if not lot:
        raise HTTPException(404, "Lote de inventario no encontrado")
    qty = _decimal(payload.quantity)
    if qty > _decimal(lot.quantity_available):
        raise HTTPException(409, "La baja excede la cantidad disponible")
    lot.quantity_available = _decimal(lot.quantity_available) - qty
    lot.quantity_written_off = _decimal(lot.quantity_written_off) + qty
    db.add(InventoryMovement(
        inventory_lot_id=lot.id,
        movement_type="WRITEOFF",
        quantity=qty,
        reference_type="inventory_writeoff",
        reference_id=lot.id,
        user_id=current.id,
        comments=payload.comments,
    ))
    db.commit()
    db.refresh(lot)
    return {
        "ok": True,
        "lot_id": lot.id,
        "quantity_available": lot.quantity_available,
        "quantity_written_off": lot.quantity_written_off,
    }


@router.get("/inventory/{lot_id}/traceability")
def inventory_traceability(lot_id: int, current=Depends(require_module_permission("floresvolcan:inventory:view")), db: Session = Depends(get_db)):
    del current
    lot = db.query(InventoryLot).options(joinedload(InventoryLot.product)).filter(InventoryLot.id == lot_id).first()
    if not lot:
        raise HTTPException(404, "Lote no encontrado")
    movements = db.query(InventoryMovement).filter(InventoryMovement.inventory_lot_id == lot.id).order_by(InventoryMovement.created_at).all()
    issues = db.query(InventoryIssue).filter(InventoryIssue.inventory_lot_id == lot.id).order_by(InventoryIssue.issued_at).all()
    plantings = db.query(PlantingRecord).filter(PlantingRecord.inventory_lot_id == lot.id).order_by(PlantingRecord.planted_at).all()
    return {
        "lot": {
            **_obj(lot, ["id", "lot_code", "supplier_lot_code", "container_number", "quantity_received", "quantity_available", "quantity_reserved", "quantity_issued", "quantity_consumed", "quantity_written_off"]),
            "product_code": lot.product.code,
            "description": lot.product.description,
        },
        "movements": [_obj(x, ["id", "movement_type", "quantity", "reference_type", "reference_id", "comments", "created_at"]) for x in movements],
        "issues": [_obj(x, ["id", "number", "requisition_line_id", "boxes", "units_per_box", "quantity", "issue_week", "issued_at", "comments"]) for x in issues],
        "plantings": [_obj(x, ["id", "production_batch_id", "bed_space_id", "side", "quantity_planted", "density", "area_m2", "planting_week", "projected_harvest_week", "planted_at"]) for x in plantings],
    }


# ---------- Trabajadores / cuadrillas ----------
@router.get("/workers")
def list_workers(current=Depends(require_module_permission("floresvolcan:workers:view")), db: Session = Depends(get_db)):
    del current
    rows = db.query(Worker).order_by(Worker.name).all()
    return [{**_obj(x, ["id", "employee_code", "identification", "name", "hire_date", "nationality", "birth_date", "daily_salary", "active", "user_id"]),
             "user_name": _user_label(db, x.user_id)} for x in rows]


def _validate_worker_user(db: Session, user_id: int | None, worker_id: int | None = None) -> None:
    if user_id is None:
        return
    user = db.query(Usuario).filter(Usuario.id == user_id, Usuario.activo.is_(True)).first()
    if not user:
        raise HTTPException(422, "Usuario de acceso no existe o está inactivo")
    if str(user.rol).lower() != "trabajador" and not user.is_superadmin:
        raise HTTPException(422, "El usuario vinculado debe tener rol trabajador")
    duplicate = db.query(Worker).filter(Worker.user_id == user_id)
    if worker_id:
        duplicate = duplicate.filter(Worker.id != worker_id)
    if duplicate.first():
        raise HTTPException(409, "Ese usuario ya está vinculado a otro trabajador")


@router.post("/workers", status_code=201)
def create_worker(payload: WorkerIn, current=Depends(require_module_permission("floresvolcan:workers:manage")), db: Session = Depends(get_db)):
    del current
    if db.query(Worker).filter((Worker.employee_code == payload.employee_code) | (Worker.identification == payload.identification)).first():
        raise HTTPException(409, "Código o identificación ya registrados")
    _validate_worker_user(db, payload.user_id)
    row = Worker(**payload.model_dump())
    db.add(row); db.commit(); db.refresh(row)
    return {**_obj(row, ["id", "employee_code", "identification", "name", "active", "user_id"]), "user_name": _user_label(db, row.user_id)}


@router.put("/workers/{worker_id}")
def update_worker(worker_id:int,payload:WorkerIn,current=Depends(require_module_permission("floresvolcan:workers:manage")),db:Session=Depends(get_db)):
    del current
    row=db.query(Worker).filter(Worker.id==worker_id).first()
    if not row: raise HTTPException(404,"Trabajador no encontrado")
    _validate_worker_user(db, payload.user_id, row.id)
    for k,v in payload.model_dump().items(): setattr(row,k,v)
    db.commit(); db.refresh(row)
    return {**_obj(row,["id","employee_code","identification","name","active","user_id"]), "user_name": _user_label(db, row.user_id)}


@router.delete("/workers/{worker_id}")
def deactivate_worker(worker_id:int,current=Depends(require_module_permission("floresvolcan:workers:manage")),db:Session=Depends(get_db)):
    del current
    row=db.query(Worker).filter(Worker.id==worker_id).first()
    if not row: raise HTTPException(404,"Trabajador no encontrado")
    row.active=False; db.commit(); return {"ok":True,"active":False}


@router.get("/worker-user-options")
def worker_user_options(current=Depends(require_module_permission("floresvolcan:workers:manage")), db: Session = Depends(get_db)):
    del current
    linked = {row[0] for row in db.query(Worker.user_id).filter(Worker.user_id.isnot(None)).all()}
    users = db.query(Usuario).filter(Usuario.activo.is_(True), func.lower(Usuario.rol) == "trabajador").order_by(Usuario.nombre).all()
    return [
        {"id": user.id, "nombre": user.nombre, "correo": user.correo, "linked": user.id in linked}
        for user in users
    ]


@router.get("/crews")
def list_crews(current=Depends(require_module_permission("floresvolcan:planting:view")), db: Session = Depends(get_db)):
    del current
    rows=db.query(WorkCrew).options(joinedload(WorkCrew.members).joinedload(WorkCrewMember.worker)).all()
    return [{"id":x.id,"name":x.name,"active":x.active,"planting_order_id":x.planting_order_id,"members":[{"worker_id":m.worker_id,"name":m.worker.name,"valid_from":m.valid_from,"valid_until":m.valid_until} for m in x.members]} for x in rows]


# Endpoint legacy cerrado: las cuadrillas existen únicamente dentro de una orden de siembra.
@router.post("/crews", status_code=409)
def create_crew(payload: CrewIn, current=Depends(require_module_permission("floresvolcan:planting:manage")), db: Session = Depends(get_db)):
    del payload, current, db
    raise HTTPException(409, "Las cuadrillas se crean únicamente al crear una orden de siembra")


# ---------- Espacios ----------
@router.get("/production-spaces")
def list_spaces(current=Depends(require_module_permission("floresvolcan:spaces:view")), db: Session = Depends(get_db)):
    del current; return [_obj(x,["id","parent_id","code","name","type","latitude","longitude","area","area_unit","capacity","status"]) for x in db.query(ProductionSpace).order_by(ProductionSpace.code).all()]


@router.get("/production-spaces/{space_id}")
def get_space(space_id:int,current=Depends(require_module_permission("floresvolcan:spaces:view")),db:Session=Depends(get_db)):
    del current
    row=db.query(ProductionSpace).filter(ProductionSpace.id==space_id).first()
    if not row: raise HTTPException(404,"Espacio productivo no encontrado")
    parent=db.query(ProductionSpace).filter(ProductionSpace.id==row.parent_id).first() if row.parent_id else None
    children=db.query(ProductionSpace).filter(ProductionSpace.parent_id==row.id).order_by(ProductionSpace.code).all()
    orders=db.query(PlantingOrder).filter(PlantingOrder.production_space_id==row.id).order_by(PlantingOrder.created_at.desc()).all()
    batches=db.query(ProductionBatch).filter(or_(ProductionBatch.production_space_id==row.id,ProductionBatch.greenhouse_space_id==row.id,ProductionBatch.bed_space_id==row.id)).order_by(ProductionBatch.opened_at.desc()).limit(100).all()
    data=_obj(row,["id","parent_id","code","name","type","latitude","longitude","area","area_unit","capacity","status"])
    data["parent"]=_obj(parent,["id","code","name","type"]) if parent else None
    data["children"]=[_obj(x,["id","parent_id","code","name","type","area","area_unit","capacity","status"]) for x in children]
    data["orders"]=[_obj(x,["id","number","status","planned_start_date","actual_start_date","created_at"]) for x in orders]
    data["batches"]=[{**_obj(x,["id","code","status","side","planting_week","projected_harvest_week"]),**_batch_metrics(db,x)} for x in batches]
    data["summary"]={"children":len(children),"orders":len(orders),"open_batches":sum(1 for x in batches if x.status=="open")}
    return data


@router.post("/production-spaces", status_code=201)
def create_space(payload: ProductionSpaceIn, current=Depends(require_module_permission("floresvolcan:spaces:manage")), db: Session = Depends(get_db)):
    del current
    if db.query(ProductionSpace).filter(ProductionSpace.code == payload.code).first(): raise HTTPException(409,"Código de espacio ya existe")
    row=ProductionSpace(**payload.model_dump()); db.add(row); db.commit(); db.refresh(row); return _obj(row,["id","parent_id","code","name","type","capacity","status"])


@router.put("/production-spaces/{space_id}")
def update_space(space_id:int,payload:ProductionSpaceIn,current=Depends(require_module_permission("floresvolcan:spaces:manage")),db:Session=Depends(get_db)):
    del current
    row=db.query(ProductionSpace).filter(ProductionSpace.id==space_id).first()
    if not row: raise HTTPException(404,"Espacio no encontrado")
    for k,v in payload.model_dump().items(): setattr(row,k,v)
    db.commit(); db.refresh(row); return _obj(row,["id","parent_id","code","name","type","capacity","status"])


@router.delete("/production-spaces/{space_id}")
def deactivate_space(space_id:int,current=Depends(require_module_permission("floresvolcan:spaces:manage")),db:Session=Depends(get_db)):
    del current
    row=db.query(ProductionSpace).filter(ProductionSpace.id==space_id).first()
    if not row: raise HTTPException(404,"Espacio no encontrado")
    row.status="inactive"; db.commit(); return {"ok":True,"status":"inactive"}


# ---------- Siembra / requisiciones ----------
@router.get("/planting-orders")
def list_planting_orders(current=Depends(require_module_permission("floresvolcan:planting:view")), db: Session = Depends(get_db)):
    del current
    rows = db.query(PlantingOrder).order_by(PlantingOrder.created_at.desc()).all()
    result=[]
    for x in rows:
        crew=db.query(WorkCrew).options(joinedload(WorkCrew.members).joinedload(WorkCrewMember.worker)).filter(WorkCrew.id==x.crew_id).first()
        result.append({**_obj(x,["id","number","production_space_id","crew_id","status","planned_start_date","actual_start_date","approved_at","comments","created_at"]),
                       "crew_members":[{"worker_id":m.worker_id,"name":m.worker.name} for m in (crew.members if crew else [])]})
    return result


@router.get("/planting-orders/{order_id}")
def get_planting_order(order_id:int,current=Depends(require_module_permission("floresvolcan:planting:view")),db:Session=Depends(get_db)):
    del current
    order=db.query(PlantingOrder).filter(PlantingOrder.id==order_id).first()
    if not order: raise HTTPException(404,"Orden de siembra no encontrada")
    space=db.query(ProductionSpace).filter(ProductionSpace.id==order.production_space_id).first()
    crew=db.query(WorkCrew).options(joinedload(WorkCrew.members).joinedload(WorkCrewMember.worker)).filter(WorkCrew.id==order.crew_id).first()
    reqs=db.query(Requisition).filter(Requisition.planting_order_id==order.id).order_by(Requisition.created_at).all()
    batches=db.query(ProductionBatch).filter(ProductionBatch.planting_order_id==order.id).order_by(ProductionBatch.opened_at).all()
    return {
        **_obj(order,["id","number","production_space_id","crew_id","requested_by_id","approved_by_id","approved_at","status","planned_start_date","actual_start_date","completed_at","comments","created_at"]),
        "requested_by_name":_user_label(db,order.requested_by_id),"approved_by_name":_user_label(db,order.approved_by_id),
        "space": _obj(space,["id","code","name","type"]) if space else None,
        "crew":{"id":crew.id,"name":crew.name,"members":[{"worker_id":m.worker_id,"name":m.worker.name} for m in crew.members]} if crew else None,
        "requisitions":[{"id":r.id,"number":r.number,"status":r.status,"created_at":r.created_at,"approved_at":r.approved_at} for r in reqs],
        "batches":[{**_obj(b,["id","code","status","bed_space_id","side","planting_week","projected_harvest_week","opened_at","closed_at"]),**_batch_metrics(db,b)} for b in batches],
    }


@router.get("/field/planting-orders/{order_id}")
def get_field_planting_order(order_id:int,current:Usuario=Depends(require_module_permission("floresvolcan:field:use")),db:Session=Depends(get_db)):
    _field_worker(db, current)
    order=db.query(PlantingOrder).filter(PlantingOrder.id==order_id).first()
    if not order: raise HTTPException(404,"Orden de siembra no encontrada")
    space=db.query(ProductionSpace).filter(ProductionSpace.id==order.production_space_id).first()
    crew=db.query(WorkCrew).options(joinedload(WorkCrew.members).joinedload(WorkCrewMember.worker)).filter(WorkCrew.id==order.crew_id).first()
    batches=db.query(ProductionBatch).filter(ProductionBatch.planting_order_id==order.id).order_by(ProductionBatch.opened_at.desc()).all()
    return {**_obj(order,["id","number","status","planned_start_date","actual_start_date","comments"]),
            "space":_obj(space,["id","code","name","type"]) if space else None,
            "crew_members":[m.worker.name for m in crew.members] if crew else [],
            "batches":[{**_obj(b,["id","code","status","bed_space_id","side","planting_week","projected_harvest_week"]),**_batch_metrics(db,b)} for b in batches]}


@router.post("/planting-orders", status_code=201)
def create_planting_order(payload: PlantingOrderIn, current: Usuario=Depends(require_module_permission("floresvolcan:planting:manage")), db: Session=Depends(get_db)):
    if not db.query(ProductionSpace).filter(ProductionSpace.id==payload.production_space_id, ProductionSpace.status=="active").first():
        raise HTTPException(422,"Espacio productivo inválido")
    ids=list(dict.fromkeys(payload.worker_ids))
    workers=db.query(Worker).filter(Worker.id.in_(ids), Worker.active.is_(True)).all()
    if len(workers)!=len(ids):
        raise HTTPException(422,"Todos los integrantes de la cuadrilla deben existir y estar activos")
    order_number=_next("OS",db.query(PlantingOrder).count())
    crew=WorkCrew(name=f"Cuadrilla {order_number}")
    crew.members=[WorkCrewMember(worker_id=w.id) for w in workers]
    db.add(crew); db.flush()
    row=PlantingOrder(number=order_number,crop_id=(_lily_crop(db).id if _lily_crop(db) else None),production_space_id=payload.production_space_id,crew_id=crew.id,requested_by_id=current.id,planned_start_date=payload.planned_start_date,comments=payload.comments)
    db.add(row); db.flush(); crew.planting_order_id=row.id
    db.commit(); db.refresh(row)
    return _obj(row,["id","number","status","crew_id"])


@router.post("/planting-orders/{order_id}/approve")
def approve_planting_order(order_id:int,current:Usuario=Depends(require_module_permission("floresvolcan:planting:approve")),db:Session=Depends(get_db)):
    row=db.query(PlantingOrder).filter(PlantingOrder.id==order_id).first()
    if not row: raise HTTPException(404,"Orden de siembra no encontrada")
    if row.status != "pending_approval": raise HTTPException(409,"La orden ya fue procesada")
    row.status="approved"; row.approved_by_id=current.id; row.approved_at=now(); db.commit(); return {"ok":True,"status":row.status}


@router.get("/requisitions")
def list_requisitions(current=Depends(require_module_permission("floresvolcan:requisitions:view")),db:Session=Depends(get_db)):
    del current
    rows = db.query(Requisition).options(joinedload(Requisition.lines)).order_by(Requisition.created_at.desc()).all()
    return [
        {
            **_obj(x,["id","number","planting_order_id","status","comments","created_at","approved_at"]),
            "lines":[{
                **_obj(l,["id","inventory_lot_id","sap_product_id","quantity_requested","quantity_approved","quantity_delivered","quantity_planted"]),
                **(lambda lot, product: {
                    "lot_code": lot.lot_code if lot else None,
                    "supplier_lot_code": lot.supplier_lot_code if lot else None,
                    "container_number": lot.container_number if lot else None,
                    "product_code": product.code if product else None,
                    "description": product.description if product else None,
                    "variety": (lot.variety_snapshot if lot else None) or (product.variety if product else None),
                    "caliber": (lot.caliber_snapshot if lot else None) or (product.caliber if product else None),
                    "default_density": product.default_density if product else None,
                    "default_cycle_weeks": product.default_cycle_weeks if product else None,
                    "reserved_pending_delivery": max(Decimal("0"), _decimal(l.quantity_approved)-_decimal(l.quantity_delivered)),
                    "pending_to_plant": max(Decimal("0"), _decimal(l.quantity_delivered)-_decimal(l.quantity_planted)),
                })(db.query(InventoryLot).filter(InventoryLot.id==l.inventory_lot_id).first(), db.query(SapProduct).filter(SapProduct.id==l.sap_product_id).first())
            } for l in x.lines]
        }
        for x in rows
    ]


@router.get("/requisitions/{requisition_id}")
def get_requisition(requisition_id:int,current=Depends(require_module_permission("floresvolcan:requisitions:view")),db:Session=Depends(get_db)):
    del current
    row=db.query(Requisition).filter(Requisition.id==requisition_id).first()
    if not row: raise HTTPException(404,"Requisición no encontrada")
    order=db.query(PlantingOrder).filter(PlantingOrder.id==row.planting_order_id).first()
    lines=db.query(RequisitionLine).filter(RequisitionLine.requisition_id==row.id).all()
    out=[]
    for line in lines:
        lot=db.query(InventoryLot).filter(InventoryLot.id==line.inventory_lot_id).first()
        product=db.query(SapProduct).filter(SapProduct.id==line.sap_product_id).first()
        plantings=db.query(PlantingRecord).filter(PlantingRecord.requisition_line_id==line.id).order_by(PlantingRecord.planted_at).all()
        out.append({**_obj(line,["id","inventory_lot_id","sap_product_id","quantity_requested","quantity_approved","quantity_delivered","quantity_planted"]),
                    "lot_code":lot.lot_code if lot else None,"supplier_lot_code":lot.supplier_lot_code if lot else None,
                    "container_number":lot.container_number if lot else None,
                    "units_per_box":lot.units_per_box if lot else None,
                    "product_code":product.code if product else None,"description":product.description if product else None,
                    "variety":(lot.variety_snapshot if lot else None) or (product.variety if product else None),
                    "common_code":(lot.common_code_snapshot if lot else None) or (product.common_code if product else None),
                    "color":(lot.color_snapshot if lot else None) or (product.color if product else None),
                    "caliber":(lot.caliber_snapshot if lot else None) or (product.caliber if product else None),
                    "available":lot.quantity_available if lot else 0,
                    "reserved":max(Decimal("0"),_decimal(line.quantity_approved)-_decimal(line.quantity_delivered)),
                    "pending_to_plant":max(Decimal("0"),_decimal(line.quantity_delivered)-_decimal(line.quantity_planted)),
                    "plantings":[_obj(x,["id","production_batch_id","bed_space_id","side","quantity_planted","planting_week","planted_at"]) for x in plantings]})
    return {**_obj(row,["id","number","planting_order_id","requested_by_id","approved_by_id","status","comments","created_at","approved_at"]),
            "requested_by_name":_user_label(db,row.requested_by_id),"approved_by_name":_user_label(db,row.approved_by_id),
            "planting_order_number":order.number if order else None,"lines":out}


@router.post("/requisitions", status_code=201)
def create_requisition(payload:RequisitionIn,current:Usuario=Depends(require_module_permission("floresvolcan:requisitions:create")),db:Session=Depends(get_db)):
    order=db.query(PlantingOrder).filter(PlantingOrder.id==payload.planting_order_id).first()
    if not order or order.status not in {"approved","in_progress"}:
        raise HTTPException(409,"La orden de siembra debe estar aprobada")
    row=Requisition(number=_next("RQ",db.query(Requisition).count()),planting_order_id=order.id,requested_by_id=current.id,comments=payload.comments)
    for item in payload.lines:
        lot=db.query(InventoryLot).filter(InventoryLot.id==item.inventory_lot_id).first()
        if not lot:
            raise HTTPException(422,"Lote de inventario inexistente")
        row.lines.append(RequisitionLine(
            inventory_lot_id=lot.id,
            sap_product_id=lot.sap_product_id,
            quantity_requested=item.quantity_requested,
            quantity_approved=0,
            quantity_delivered=0,
            quantity_planted=0,
        ))
    db.add(row)
    db.commit()
    db.refresh(row)
    return {"id":row.id,"number":row.number,"status":row.status}


@router.post("/requisitions/{requisition_id}/approve")
def approve_requisition(requisition_id:int,current:Usuario=Depends(require_module_permission("floresvolcan:requisitions:approve")),db:Session=Depends(get_db)):
    # PostgreSQL no permite FOR UPDATE sobre el lado nullable de ciertos LEFT JOIN.
    # Bloqueamos primero la cabecera y luego cada lote por consultas independientes.
    row=db.query(Requisition).filter(Requisition.id==requisition_id).with_for_update().first()
    if not row:
        raise HTTPException(404,"Requisición no encontrada")
    if row.status!="pending":
        raise HTTPException(409,"La requisición ya fue procesada")
    lines=db.query(RequisitionLine).filter(RequisitionLine.requisition_id==row.id).order_by(RequisitionLine.id).all()
    if not lines:
        raise HTTPException(422,"La requisición no contiene líneas")
    for line in lines:
        lot=db.query(InventoryLot).filter(InventoryLot.id==line.inventory_lot_id).with_for_update().first()
        if not lot:
            raise HTTPException(409,f"El lote {line.inventory_lot_id} ya no existe")
        qty=_decimal(line.quantity_requested)
        available=_decimal(lot.quantity_available)
        if available<qty:
            raise HTTPException(409,f"Saldo insuficiente en {lot.lot_code}: solicitado {qty}, disponible {available}")
        lot.quantity_available=available-qty
        lot.quantity_reserved=_decimal(lot.quantity_reserved)+qty
        line.quantity_approved=qty
        db.add(InventoryMovement(inventory_lot_id=lot.id,movement_type="RESERVATION",quantity=qty,reference_type="requisition",reference_id=row.id,user_id=current.id))
    row.status="approved"; row.approved_by_id=current.id; row.approved_at=now()
    db.commit()
    return {"ok":True,"status":row.status}


# v0.4: no existe una operación separada de "Salida de bulbo".
# La requisición aprobada reserva el inventario y la siembra consume directamente ese saldo.
# fv_inventory_issues se conserva como tabla histórica para instalaciones actualizadas desde v0.3.


# ---------- Captura de campo ----------
@router.get("/field/tasks")
def field_tasks(current:Usuario=Depends(require_module_permission("floresvolcan:field:use")),db:Session=Depends(get_db)):
    worker=_field_worker(db,current)
    crew_ids=[m.crew_id for m in db.query(WorkCrewMember).filter(WorkCrewMember.worker_id==worker.id).all()]
    orders=[]
    if crew_ids:
        candidates=db.query(PlantingOrder).filter(PlantingOrder.crew_id.in_(crew_ids),PlantingOrder.status.in_(["approved","in_progress"])).order_by(PlantingOrder.created_at).all()
        for order in candidates:
            eligible=(db.query(RequisitionLine,Requisition,InventoryLot,SapProduct)
                .join(Requisition,Requisition.id==RequisitionLine.requisition_id)
                .join(InventoryLot,InventoryLot.id==RequisitionLine.inventory_lot_id)
                .join(SapProduct,SapProduct.id==RequisitionLine.sap_product_id)
                .filter(Requisition.planting_order_id==order.id,Requisition.status.in_(["approved","partially_delivered","delivered"]),RequisitionLine.quantity_delivered>RequisitionLine.quantity_planted)
                .order_by(Requisition.approved_at,RequisitionLine.id).all())
            if not eligible: continue
            materials=[]
            for line,req,lot,product in eligible:
                materials.append({
                    "requisition_id":req.id,"requisition_number":req.number,"requisition_line_id":line.id,
                    "product_code":product.code,"description":product.description,
                    "variety":lot.variety_snapshot or product.variety,"common_code":lot.common_code_snapshot or product.common_code,
                    "color":lot.color_snapshot or product.color,"caliber":lot.caliber_snapshot or product.caliber,
                    "lot_code":lot.lot_code,"supplier_lot_code":lot.supplier_lot_code,"container_number":lot.container_number,
                    "pending_to_plant":max(Decimal("0"),_decimal(line.quantity_delivered)-_decimal(line.quantity_planted)),
                    "delivered":line.quantity_delivered,"planted":line.quantity_planted,
                    "default_density":product.default_density,"default_cycle_weeks":product.default_cycle_weeks,
                })
            space=db.query(ProductionSpace).filter(ProductionSpace.id==order.production_space_id).first()
            bed_options=[]
            if space:
                if space.type in {"bed","row","table"}:
                    bed_options=[space]
                else:
                    for bed in db.query(ProductionSpace).filter(ProductionSpace.type.in_(["bed","row","table"]),ProductionSpace.status=="active").order_by(ProductionSpace.code).all():
                        if _belongs_to(db,bed,space.id): bed_options.append(bed)
            first=materials[0]
            orders.append({**_obj(order,["id","number","production_space_id","crew_id","status","planned_start_date"]),
                "space":_obj(space,["id","code","name","type"]) if space else None,
                "default_bed_id":space.id if space and space.type in {"bed","row","table"} else None,
                "bed_options":[_obj(x,["id","code","name","parent_id","capacity"]) for x in bed_options],
                "requisition_id":first["requisition_id"],"requisition_number":first["requisition_number"],
                "requisition_line_id":first["requisition_line_id"],"material":first,"materials":materials})
    allowed_order_ids={x["id"] for x in orders}
    batches=[]
    for x in db.query(ProductionBatch).filter(ProductionBatch.status=="open").order_by(ProductionBatch.opened_at.desc()).all():
        if x.planting_order_id not in allowed_order_ids: continue
        lot=db.query(InventoryLot).filter(InventoryLot.id==x.inventory_lot_id).first() if x.inventory_lot_id else None
        product=db.query(SapProduct).filter(SapProduct.id==x.sap_product_id).first()
        bed=db.query(ProductionSpace).filter(ProductionSpace.id==x.bed_space_id).first() if x.bed_space_id else None
        greenhouse=db.query(ProductionSpace).filter(ProductionSpace.id==x.greenhouse_space_id).first() if x.greenhouse_space_id else None
        metrics=_batch_metrics(db,x)
        batches.append({**_obj(x,["id","code","planting_order_id","sap_product_id","inventory_lot_id","production_space_id","greenhouse_space_id","bed_space_id","side","planting_week","projected_harvest_week","cycle_weeks","status"]),
            **metrics,"product_code":product.code if product else None,"description":product.description if product else None,
            "variety":(lot.variety_snapshot if lot else None) or (product.variety if product else None),
            "common_code":(lot.common_code_snapshot if lot else None) or (product.common_code if product else None),
            "color":(lot.color_snapshot if lot else None) or (product.color if product else None),
            "caliber":(lot.caliber_snapshot if lot else None) or (product.caliber if product else None),
            "supplier_lot_code":lot.supplier_lot_code if lot else None,"container_number":lot.container_number if lot else None,
            "bed_code":bed.code if bed else None,"bed_name":bed.name if bed else None,"greenhouse_name":greenhouse.name if greenhouse else None})
    crop=_lily_crop(db)
    discard_reasons=[x.label for x in db.query(CropCatalogValue).filter(CropCatalogValue.crop_id==crop.id,CropCatalogValue.category=="discard_reason",CropCatalogValue.active.is_(True)).order_by(CropCatalogValue.sort_order,CropCatalogValue.label).all()] if crop else []
    return {"planting_orders":orders,"open_batches":batches,"discard_reasons":discard_reasons}


@router.post("/field/planting", status_code=201)
def register_planting(payload:PlantingRecordIn,current:Usuario=Depends(require_module_permission("floresvolcan:planting:register")),db:Session=Depends(get_db)):
    _field_worker(db, current)
    if payload.client_token:
        existing=db.query(PlantingRecord).filter(PlantingRecord.client_token==payload.client_token).first()
        if existing:
            batch=db.query(ProductionBatch).filter(ProductionBatch.id==existing.production_batch_id).first()
            return {"id":existing.id,"batch_id":batch.id,"batch_code":batch.code,"quantity":existing.quantity_planted,"idempotent":True}
    order=db.query(PlantingOrder).filter(PlantingOrder.id==payload.planting_order_id).first()
    line=None
    if payload.requisition_line_id:
        line=db.query(RequisitionLine).filter(RequisitionLine.id==payload.requisition_line_id).first()
    if not line:
        line=(db.query(RequisitionLine).join(Requisition,Requisition.id==RequisitionLine.requisition_id)
              .filter(Requisition.planting_order_id==payload.planting_order_id,Requisition.status.in_(["approved","partially_delivered","delivered"]),RequisitionLine.quantity_delivered>RequisitionLine.quantity_planted)
              .order_by(Requisition.approved_at,RequisitionLine.id).first())
    bed=db.query(ProductionSpace).filter(ProductionSpace.id==payload.bed_space_id,ProductionSpace.type.in_(["bed","row","table"]),ProductionSpace.status=="active").first()
    if not order or order.status not in {"approved","in_progress"}:
        raise HTTPException(409,"Orden de siembra no disponible")
    if not line or line.requisition.planting_order_id!=order.id:
        raise HTTPException(422,"Línea de requisición no corresponde a la orden")
    if line.requisition.status not in {"approved","partially_delivered","delivered"}:
        raise HTTPException(409,"La orden requiere una requisición aprobada y material entregado antes de sembrarse")
    if not bed:
        raise HTTPException(422,"Cama inválida")
    if not _belongs_to(db,bed,order.production_space_id):
        raise HTTPException(422,"La cama no pertenece al espacio productivo de la orden")
    qty=_decimal(payload.quantity_planted)
    committed_pending=_decimal(line.quantity_delivered)-_decimal(line.quantity_planted)
    if qty>committed_pending:
        raise HTTPException(409,"La cantidad supera el material entregado de la requisición pendiente de sembrar")
    lot=db.query(InventoryLot).filter(InventoryLot.id==line.inventory_lot_id).with_for_update().first()
    if not lot: raise HTTPException(409,"El lote de inventario ya no existe")
    committed_inventory=_decimal(lot.quantity_issued)
    if committed_inventory<qty:
        raise HTTPException(409,"El material entregado a campo ya no alcanza")
    product=db.query(SapProduct).filter(SapProduct.id==line.sap_product_id).first()
    density=_decimal(payload.density or (product.default_density if product else None))
    if density<=0:
        raise HTTPException(422,"Configure la densidad del producto SAP antes de registrar la siembra")
    cycle_weeks=int(payload.cycle_weeks or (product.default_cycle_weeks if product else 0) or 0)
    if cycle_weeks<=0:
        raise HTTPException(422,"Configure el ciclo en semanas del producto SAP antes de registrar la siembra")
    planted_at=payload.planted_at or now()
    planting_week=_iso_week(planted_at)
    projected_week=_projected_week(planted_at,cycle_weeks)
    area_m2=qty/density
    used_area=_decimal(db.query(func.coalesce(func.sum(PlantingRecord.area_m2),0)).join(ProductionBatch,ProductionBatch.id==PlantingRecord.production_batch_id).filter(PlantingRecord.bed_space_id==bed.id,ProductionBatch.status=="open").scalar())
    if bed.capacity is not None and used_area+area_m2>_decimal(bed.capacity):
        raise HTTPException(409,f"La siembra requiere {area_m2:.2f} m² y supera la capacidad restante de la cama")
    greenhouse=None
    if payload.greenhouse_space_id:
        greenhouse=db.query(ProductionSpace).filter(ProductionSpace.id==payload.greenhouse_space_id,ProductionSpace.type=="greenhouse").first()
    greenhouse=greenhouse or _ancestor_of_type(db,bed,"greenhouse")
    side=(payload.side or "").upper() or None
    batch=None
    if payload.production_batch_id:
        batch=db.query(ProductionBatch).filter(ProductionBatch.id==payload.production_batch_id,ProductionBatch.status=="open").first()
        if batch and (batch.inventory_lot_id!=lot.id or batch.bed_space_id!=bed.id):
            raise HTTPException(422,"El lote productivo seleccionado no corresponde al lote/cama de esta siembra")
    if not batch:
        batch=db.query(ProductionBatch).filter(
            ProductionBatch.planting_order_id==order.id,
            ProductionBatch.inventory_lot_id==lot.id,
            ProductionBatch.bed_space_id==bed.id,
            ProductionBatch.side==side,
            ProductionBatch.planting_week==planting_week,
            ProductionBatch.status=="open",
        ).first()
    if not batch:
        batch=ProductionBatch(
            code=_next("FV",db.query(ProductionBatch).count()),crop_id=order.crop_id,planting_order_id=order.id,sap_product_id=line.sap_product_id,
            inventory_lot_id=lot.id,production_space_id=order.production_space_id,
            greenhouse_space_id=greenhouse.id if greenhouse else None,bed_space_id=bed.id,side=side,
            planting_week=planting_week,projected_harvest_week=projected_week,cycle_weeks=cycle_weeks,
        )
        db.add(batch); db.flush()
    rec=PlantingRecord(
        client_token=payload.client_token,production_batch_id=batch.id,planting_order_id=order.id,requisition_line_id=line.id,
        inventory_lot_id=lot.id,greenhouse_space_id=greenhouse.id if greenhouse else None,bed_space_id=bed.id,side=side,
        table_space_id=bed.id,row_space_id=bed.id,
        quantity_planted=qty,density=density,area_m2=area_m2,cycle_weeks=cycle_weeks,planting_week=planting_week,
        projected_harvest_week=projected_week,sealed_at=payload.sealed_at,crew_id=order.crew_id,
        operator_user_id=current.id,planted_at=planted_at,notes=payload.notes,
    )
    db.add(rec); db.flush()
    lot.quantity_issued=_decimal(lot.quantity_issued)-qty
    lot.quantity_consumed=_decimal(lot.quantity_consumed)+qty
    line.quantity_planted=_decimal(line.quantity_planted)+qty
    db.add(InventoryMovement(inventory_lot_id=lot.id,movement_type="PLANTING_CONSUMPTION",quantity=qty,reference_type="planting_record",reference_id=rec.id,user_id=current.id))
    order.status="in_progress"; order.actual_start_date=order.actual_start_date or planted_at.date()
    db.commit(); db.refresh(rec)
    return {
        "id":rec.id,"batch_id":batch.id,"batch_code":batch.code,"quantity":rec.quantity_planted,
        "density":rec.density,"area_m2":rec.area_m2,"planting_week":rec.planting_week,
        "projected_harvest_week":rec.projected_harvest_week,"bed_used_m2":used_area+area_m2,
        "bed_remaining_m2":max(Decimal("0"),_decimal(bed.capacity)-(used_area+area_m2)) if bed.capacity is not None else None,
    }


@router.post("/field/harvest", status_code=201)
def register_harvest(payload:HarvestRecordIn,current:Usuario=Depends(require_module_permission("floresvolcan:harvest:register")),db:Session=Depends(get_db)):
    _field_worker(db, current)
    if payload.client_token:
        existing=db.query(HarvestRecord).filter(HarvestRecord.client_token==payload.client_token).first()
        if existing:
            return _obj(existing,["id","production_batch_id","quantity_harvested","total_output","actual_harvest_week","harvested_at"])
    batch=db.query(ProductionBatch).filter(ProductionBatch.id==payload.production_batch_id).first()
    if not batch or batch.status!="open":
        raise HTTPException(409,"Lote productivo cerrado o inexistente")
    sale=_decimal(payload.bloom_1_qty)+_decimal(payload.bloom_2_qty)+_decimal(payload.bloom_3_5_qty)
    discarded=_decimal(payload.discard_qty)
    total=sale+discarded
    if total<=0:
        raise HTTPException(422,"Registre al menos una flor cortada o una cantidad de descarte")
    if discarded>0 and not (payload.discard_reason or "").strip():
        raise HTTPException(422,"Indique el motivo del descarte")
    if payload.planting_record_id:
        segment=db.query(PlantingRecord).filter(PlantingRecord.id==payload.planting_record_id,PlantingRecord.production_batch_id==batch.id).first()
        if not segment:
            raise HTTPException(422,"El segmento de siembra no pertenece al lote productivo")
    order=db.query(PlantingOrder).filter(PlantingOrder.id==batch.planting_order_id).first()
    harvested_at=payload.harvested_at or now()
    rec=HarvestRecord(
        client_token=payload.client_token,production_batch_id=batch.id,planting_record_id=payload.planting_record_id,
        bed_space_id=batch.bed_space_id,table_space_id=batch.bed_space_id,row_space_id=batch.bed_space_id,crew_id=order.crew_id,operator_user_id=current.id,
        bloom_1_qty=payload.bloom_1_qty,bloom_2_qty=payload.bloom_2_qty,bloom_3_5_qty=payload.bloom_3_5_qty,
        discard_qty=discarded,discard_reason=(payload.discard_reason or "").strip() or None,
        quantity_harvested=sale,total_output=total,actual_harvest_week=_iso_week(harvested_at),
        harvested_at=harvested_at,notes=payload.notes,
    )
    db.add(rec); db.commit(); db.refresh(rec)
    return _obj(rec,["id","production_batch_id","planting_record_id","bloom_1_qty","bloom_2_qty","bloom_3_5_qty","discard_qty","discard_reason","quantity_harvested","total_output","actual_harvest_week","harvested_at"])


def _batch_metrics(db: Session, batch: ProductionBatch) -> dict:
    planted=_decimal(db.query(func.coalesce(func.sum(PlantingRecord.quantity_planted),0)).filter(PlantingRecord.production_batch_id==batch.id).scalar())
    sale=_decimal(db.query(func.coalesce(func.sum(HarvestRecord.quantity_harvested),0)).filter(HarvestRecord.production_batch_id==batch.id).scalar())
    output=_decimal(db.query(func.coalesce(func.sum(HarvestRecord.total_output),0)).filter(HarvestRecord.production_batch_id==batch.id).scalar())
    discarded=max(Decimal("0"),output-sale)
    bloom1=_decimal(db.query(func.coalesce(func.sum(HarvestRecord.bloom_1_qty),0)).filter(HarvestRecord.production_batch_id==batch.id).scalar())
    bloom2=_decimal(db.query(func.coalesce(func.sum(HarvestRecord.bloom_2_qty),0)).filter(HarvestRecord.production_batch_id==batch.id).scalar())
    bloom35=_decimal(db.query(func.coalesce(func.sum(HarvestRecord.bloom_3_5_qty),0)).filter(HarvestRecord.production_batch_id==batch.id).scalar())
    return {
        "planted":planted,"harvested":output,"sale":sale,"discarded":discarded,
        "difference":planted-output,"yield_percentage":(output/planted*100 if planted else Decimal("0")),
        "sale_yield_percentage":(sale/planted*100 if planted else Decimal("0")),
        "bloom_1":bloom1,"bloom_2":bloom2,"bloom_3_5":bloom35,
        "cut_events":db.query(HarvestRecord).filter(HarvestRecord.production_batch_id==batch.id).count(),
    }


@router.get("/production-batches")
def list_batches(current=Depends(require_module_permission("floresvolcan:planting:view")),db:Session=Depends(get_db)):
    del current
    rows=db.query(ProductionBatch).order_by(ProductionBatch.opened_at.desc()).all()
    result=[]
    for batch in rows:
        lot=db.query(InventoryLot).filter(InventoryLot.id==batch.inventory_lot_id).first() if batch.inventory_lot_id else None
        product=db.query(SapProduct).filter(SapProduct.id==batch.sap_product_id).first()
        bed=db.query(ProductionSpace).filter(ProductionSpace.id==batch.bed_space_id).first() if batch.bed_space_id else None
        result.append({
            **_obj(batch,["id","code","planting_order_id","sap_product_id","inventory_lot_id","production_space_id","greenhouse_space_id","bed_space_id","side","planting_week","projected_harvest_week","cycle_weeks","status","opened_at","closed_at"]),
            **_batch_metrics(db,batch),
            "product_code":product.code if product else None,
            "variety":(lot.variety_snapshot if lot else None) or (product.variety if product else None),
            "source_lot":lot.supplier_lot_code if lot else None,
            "bed_code":bed.code if bed else None,
        })
    return result


@router.get("/production-batches/{batch_id}/summary")
def batch_summary(batch_id:int,current=Depends(require_module_permission("floresvolcan:planting:view")),db:Session=Depends(get_db)):
    del current
    batch=db.query(ProductionBatch).filter(ProductionBatch.id==batch_id).first()
    if not batch:
        raise HTTPException(404,"Lote no encontrado")
    records=db.query(HarvestRecord).filter(HarvestRecord.production_batch_id==batch.id).order_by(HarvestRecord.harvested_at).all()
    return {
        **_obj(batch,["id","code","status","planting_week","projected_harvest_week","side","bed_space_id","inventory_lot_id"]),
        **_batch_metrics(db,batch),
        "cuts":[_obj(x,["id","actual_harvest_week","harvested_at","bloom_1_qty","bloom_2_qty","bloom_3_5_qty","discard_qty","discard_reason","quantity_harvested","total_output","notes"]) for x in records],
    }


@router.post("/production-batches/{batch_id}/technical-close", status_code=201)
def technical_close(batch_id:int,payload:TechnicalClosureIn,current:Usuario=Depends(require_module_permission("floresvolcan:technical_close:execute")),db:Session=Depends(get_db)):
    batch=db.query(ProductionBatch).filter(ProductionBatch.id==batch_id).with_for_update().first()
    if not batch:
        raise HTTPException(404,"Lote no encontrado")
    if batch.status=="closed":
        raise HTTPException(409,"El lote ya está cerrado")
    metrics=_batch_metrics(db,batch)
    closure=TechnicalClosure(
        production_batch_id=batch.id,total_planted=metrics["planted"],total_harvested=metrics["harvested"],
        total_sale=metrics["sale"],total_discarded=metrics["discarded"],difference=metrics["difference"],
        yield_percentage=metrics["yield_percentage"],sale_yield_percentage=metrics["sale_yield_percentage"],
        closed_by_id=current.id,comments=payload.comments,
    )
    db.add(closure); batch.status="closed"; batch.closed_at=now(); db.commit(); db.refresh(closure)
    return _obj(closure,["id","production_batch_id","total_planted","total_harvested","total_sale","total_discarded","difference","yield_percentage","sale_yield_percentage","closed_at","comments"])


@router.post("/ai/field/transcribe")
async def ai_field_transcribe(file:UploadFile=File(...),current=Depends(require_module_permission("floresvolcan:ai:use"))):
    del current
    content=await file.read()
    text=transcribe_field_audio(file.filename or "campo.webm",content,file.content_type)
    return {"text":text}


@router.post("/ai/field/interpret")
def ai_field_interpret(payload:FieldInterpretIn,current=Depends(require_module_permission("floresvolcan:ai:use")),db:Session=Depends(get_db)):
    del current
    context={"planting_order_id":payload.planting_order_id,"production_batch_id":payload.production_batch_id,"current_space_id":payload.current_space_id}
    result=interpret_field_text(operation=payload.operation,text=payload.text,context=context)
    # La IA interpreta texto; únicamente la base de datos resuelve IDs válidos.
    if result.get("greenhouse_code"):
        greenhouse=db.query(ProductionSpace).filter(func.lower(ProductionSpace.code)==str(result["greenhouse_code"]).lower(),ProductionSpace.type=="greenhouse",ProductionSpace.status=="active").first()
        result["resolved_greenhouse_id"]=greenhouse.id if greenhouse else None
        if not greenhouse: result.setdefault("warnings",[]).append("No se encontró el invernadero indicado")
    if result.get("bed_code"):
        q=db.query(ProductionSpace).filter(func.lower(ProductionSpace.code)==str(result["bed_code"]).lower(),ProductionSpace.type.in_(["bed","row","table"]),ProductionSpace.status=="active")
        if result.get("resolved_greenhouse_id"):
            matches=[x for x in q.all() if _belongs_to(db,x,result["resolved_greenhouse_id"])]
            bed=matches[0] if len(matches)==1 else None
        else:
            matches=q.all(); bed=matches[0] if len(matches)==1 else None
        result["resolved_bed_id"]=bed.id if bed else None
        if not bed: result.setdefault("warnings",[]).append("La cama no existe o es ambigua; selecciónela manualmente")
    if result.get("requisition_number"):
        rq=db.query(Requisition).options(joinedload(Requisition.lines)).filter(func.lower(Requisition.number)==str(result["requisition_number"]).lower()).first()
        result["resolved_requisition_id"]=rq.id if rq else None
        candidates=list(rq.lines) if rq else []
        if result.get("source_lot") and candidates:
            source=str(result["source_lot"]).strip().lower()
            candidates=[ln for ln in candidates if (lambda lot: lot and source in {str(lot.lot_code or '').lower(),str(lot.supplier_lot_code or '').lower()})(db.query(InventoryLot).filter(InventoryLot.id==ln.inventory_lot_id).first())]
        result["resolved_requisition_line_id"]=(candidates[0].id if len(candidates)==1 else None)
        if rq and len(candidates)!=1: result.setdefault("warnings",[]).append("La requisición tiene varias líneas; seleccione el lote de bulbo")
    if result.get("source_lot"):
        source=str(result["source_lot"]).strip().lower()
        lots=db.query(InventoryLot).filter(or_(func.lower(InventoryLot.lot_code)==source,func.lower(InventoryLot.supplier_lot_code)==source)).all()
        result["resolved_inventory_lot_id"]=(lots[0].id if len(lots)==1 else None)
        if len(lots)>1: result.setdefault("warnings",[]).append("El lote de bulbo aparece más de una vez; seleccione el contenedor correcto")
    if result.get("batch_code"):
        batch=db.query(ProductionBatch).filter(func.lower(ProductionBatch.code)==str(result["batch_code"]).lower(),ProductionBatch.status=="open").first()
        result["resolved_batch_id"]=batch.id if batch else None
        if not batch: result.setdefault("warnings",[]).append("No se encontró un lote productivo abierto con ese código")
    result["requires_confirmation"]=True
    return result

# ============================================================================
# v0.5.0 - Perfil Lirios / ERP compatible con Excel histórico
# ============================================================================

def _dashboard_v05(db: Session) -> dict:
    inv_available = _decimal(db.query(func.coalesce(func.sum(InventoryLot.quantity_available), 0)).scalar())
    inv_reserved = _decimal(db.query(func.coalesce(func.sum(InventoryLot.quantity_reserved), 0)).scalar())
    inv_issued = _decimal(db.query(func.coalesce(func.sum(InventoryLot.quantity_issued), 0)).scalar())
    inv_consumed = _decimal(db.query(func.coalesce(func.sum(InventoryLot.quantity_consumed), 0)).scalar())
    planted = _decimal(db.query(func.coalesce(func.sum(PlantingRecord.quantity_planted), 0)).scalar())
    sale = _decimal(db.query(func.coalesce(func.sum(HarvestRecord.quantity_harvested), 0)).scalar())
    output = _decimal(db.query(func.coalesce(func.sum(HarvestRecord.total_output), 0)).scalar())
    discarded = max(Decimal("0"), output - sale)
    today = date.today()

    # Series por semana productiva.
    planting_series = [
        {"week": week or "Sin semana", "value": _decimal(value)}
        for week, value in db.query(PlantingRecord.planting_week, func.sum(PlantingRecord.quantity_planted))
            .group_by(PlantingRecord.planting_week).order_by(PlantingRecord.planting_week.desc()).limit(16).all()
    ][::-1]
    harvest_series = [
        {"week": week or "Sin semana", "sale": _decimal(sale_v), "discard": _decimal(discard_v)}
        for week, sale_v, discard_v in db.query(
            HarvestRecord.actual_harvest_week,
            func.sum(HarvestRecord.quantity_harvested),
            func.sum(HarvestRecord.discard_qty),
        ).group_by(HarvestRecord.actual_harvest_week).order_by(HarvestRecord.actual_harvest_week.desc()).limit(16).all()
    ][::-1]

    # Top variedades se resuelve con snapshots para respetar la trazabilidad histórica.
    variety_totals: dict[str, Decimal] = {}
    for rec, lot, product in db.query(PlantingRecord, InventoryLot, SapProduct).join(
        InventoryLot, InventoryLot.id == PlantingRecord.inventory_lot_id
    ).join(SapProduct, SapProduct.id == InventoryLot.sap_product_id).all():
        variety = (lot.variety_snapshot or product.variety or "SIN VARIEDAD").strip()
        variety_totals[variety] = variety_totals.get(variety, Decimal("0")) + _decimal(rec.quantity_planted)
    top_varieties = [
        {"variety": k, "planted": v}
        for k, v in sorted(variety_totals.items(), key=lambda item: item[1], reverse=True)[:10]
    ]

    discard_rows = db.query(HarvestRecord.discard_reason, func.sum(HarvestRecord.discard_qty)).filter(
        HarvestRecord.discard_qty > 0
    ).group_by(HarvestRecord.discard_reason).order_by(func.sum(HarvestRecord.discard_qty).desc()).all()
    discard_reasons = [{"reason": reason or "SIN MOTIVO", "quantity": _decimal(qty)} for reason, qty in discard_rows]

    grade_1 = _decimal(db.query(func.coalesce(func.sum(HarvestRecord.bloom_1_qty), 0)).scalar())
    grade_2 = _decimal(db.query(func.coalesce(func.sum(HarvestRecord.bloom_2_qty), 0)).scalar())
    grade_35 = _decimal(db.query(func.coalesce(func.sum(HarvestRecord.bloom_3_5_qty), 0)).scalar())

    alerts = []
    late_po = db.query(PurchaseOrder).filter(PurchaseOrder.status.in_(["pending_approval", "in_process"]), PurchaseOrder.warehouse_due_date < today).count()
    if late_po: alerts.append({"severity":"danger","title":"Compras vencidas","message":f"{late_po} orden(es) superaron la fecha compromiso de bodega.","to":"/floresvolcan/compras"})
    req_without_delivery = db.query(Requisition).filter(Requisition.status == "approved").join(RequisitionLine).group_by(Requisition.id).having(func.sum(RequisitionLine.quantity_delivered) <= 0).count()
    if req_without_delivery: alerts.append({"severity":"warning","title":"Requisiciones sin entrega","message":f"{req_without_delivery} requisición(es) aprobadas aún no tienen salida física registrada.","to":"/floresvolcan/produccion"})
    sap_missing = db.query(SapProduct).filter(SapProduct.active.is_(True), or_(SapProduct.default_density.is_(None), SapProduct.default_cycle_weeks.is_(None))).count()
    if sap_missing: alerts.append({"severity":"warning","title":"SAP incompleto","message":f"{sap_missing} código(s) activos no tienen densidad o ciclo configurado.","to":"/floresvolcan/sap"})
    negative_balance = db.query(InventoryLot).filter(or_(InventoryLot.quantity_available < 0, InventoryLot.quantity_reserved < 0, InventoryLot.quantity_issued < 0)).count()
    if negative_balance: alerts.append({"severity":"danger","title":"Inventario inconsistente","message":f"{negative_balance} lote(s) presentan saldo negativo.","to":"/floresvolcan/inventario"})
    open_batches = db.query(ProductionBatch).filter(ProductionBatch.status == "open").count()
    over_output = 0
    for batch in db.query(ProductionBatch).filter(ProductionBatch.status == "open").all():
        metrics = _batch_metrics(db, batch)
        if metrics["harvested"] > metrics["planted"]:
            over_output += 1
    if over_output: alerts.append({"severity":"warning","title":"Corte mayor a siembra","message":f"{over_output} lote(s) tienen salida contabilizada superior a la cantidad sembrada.","to":"/floresvolcan/produccion"})

    return {
        "kpis": {
            "suppliers_active": db.query(Supplier).filter(Supplier.active.is_(True)).count(),
            "purchase_pending": db.query(PurchaseOrder).filter(PurchaseOrder.status == "pending_approval").count(),
            "purchase_in_process": db.query(PurchaseOrder).filter(PurchaseOrder.status == "in_process").count(),
            "inventory_lots": db.query(InventoryLot).count(),
            "inventory_available": inv_available, "inventory_reserved": inv_reserved, "inventory_in_field": inv_issued,
            "inventory_consumed": inv_consumed,
            "planting_orders_open": db.query(PlantingOrder).filter(PlantingOrder.status.in_(["pending_approval","approved","in_progress"])).count(),
            "requisitions_pending": db.query(Requisition).filter(Requisition.status == "pending").count(),
            "batches_open": open_batches, "total_planted": planted, "total_sale": sale, "total_discarded": discarded,
            "technical_yield": (output / planted * 100 if planted else Decimal("0")),
            "sale_yield": (sale / planted * 100 if planted else Decimal("0")),
        },
        "planting_by_week": planting_series,
        "harvest_by_week": harvest_series,
        "top_varieties": top_varieties,
        "discard_reasons": discard_reasons,
        "grade_distribution": [
            {"label":"1 BL","value":grade_1},{"label":"2 BL","value":grade_2},{"label":"3-5 BL","value":grade_35},
        ],
        "alerts": alerts,
    }


@router.get("/management-dashboard")
def management_dashboard(current=Depends(require_module_permission("floresvolcan:dashboard:view")), db: Session=Depends(get_db)):
    del current
    return _dashboard_v05(db)


@router.get("/crops")
def list_crops(current=Depends(require_module_permission("floresvolcan:config:view")), db: Session=Depends(get_db)):
    del current
    return [_obj(x,["id","code","name","material_type","active","is_default"]) for x in db.query(FlowerCrop).order_by(FlowerCrop.name).all()]


@router.get("/crops/{crop_code}/catalogs")
def crop_catalogs(crop_code: str, category: str | None=None, include_inactive: bool=False,
                  current=Depends(require_module_permission("floresvolcan:config:view")), db: Session=Depends(get_db)):
    del current
    crop=db.query(FlowerCrop).filter(func.lower(FlowerCrop.code)==crop_code.strip().lower()).first()
    if not crop: raise HTTPException(404,"Cultivo no encontrado")
    q=db.query(CropCatalogValue).filter(CropCatalogValue.crop_id==crop.id, CropCatalogValue.source != "deleted")
    if category: q=q.filter(CropCatalogValue.category==category)
    if not include_inactive: q=q.filter(CropCatalogValue.active.is_(True))
    rows=q.order_by(CropCatalogValue.category,CropCatalogValue.sort_order,CropCatalogValue.label).all()
    return {"crop":_obj(crop,["id","code","name","material_type","active","is_default"]),
            "values":[_obj(x,["id","category","code","label","sort_order","active","source"]) for x in rows]}


@router.post("/crops/{crop_code}/catalogs", status_code=201)
def add_crop_catalog(crop_code:str,payload:CropCatalogValueIn,current=Depends(require_module_permission("floresvolcan:config:manage")),db:Session=Depends(get_db)):
    del current
    crop=db.query(FlowerCrop).filter(func.lower(FlowerCrop.code)==crop_code.strip().lower()).first()
    if not crop: raise HTTPException(404,"Cultivo no encontrado")
    code=payload.code.strip().upper(); category=payload.category.strip().lower()
    if db.query(CropCatalogValue.id).filter(CropCatalogValue.crop_id==crop.id,CropCatalogValue.category==category,CropCatalogValue.code==code).first():
        raise HTTPException(409,"El valor ya existe en ese catálogo")
    row=CropCatalogValue(crop_id=crop.id,category=category,code=code,label=payload.label.strip(),sort_order=payload.sort_order,active=payload.active,source="manual")
    db.add(row); db.commit(); db.refresh(row)
    return _obj(row,["id","category","code","label","sort_order","active","source"])


@router.patch("/catalog-values/{catalog_id}")
def patch_catalog_value(catalog_id:int,payload:CropCatalogValuePatch,current=Depends(require_module_permission("floresvolcan:config:manage")),db:Session=Depends(get_db)):
    del current
    row=db.query(CropCatalogValue).filter(CropCatalogValue.id==catalog_id).first()
    if not row:
        raise HTTPException(404,"Valor de catálogo no encontrado")
    data=payload.model_dump(exclude_none=True)
    if "code" in data:
        candidate=str(data["code"]).strip().upper()
        duplicate=db.query(CropCatalogValue.id).filter(
            CropCatalogValue.crop_id==row.crop_id, CropCatalogValue.category==row.category,
            CropCatalogValue.code==candidate, CropCatalogValue.id!=row.id
        ).first()
        if duplicate:
            raise HTTPException(409,"Ya existe otro valor con ese código en el catálogo")
        data["code"]=candidate
    if "label" in data:
        data["label"]=str(data["label"]).strip()
    for key,value in data.items():
        setattr(row,key,value)
    # Sincroniza los snapshots actuales de SAP si se edita un catálogo enlazado.
    refs={
        "variety": (SapProduct.variety_catalog_id, "variety", "label"),
        "caliber": (SapProduct.caliber_catalog_id, "caliber", "label"),
        "common": (SapProduct.common_catalog_id, "common_code", "code"),
        "color": (SapProduct.color_catalog_id, "color", "label"),
    }
    ref=refs.get(row.category)
    if ref:
        column, attr, source_attr=ref
        for product in db.query(SapProduct).filter(column==row.id).all():
            setattr(product, attr, getattr(row, source_attr))
    db.commit(); db.refresh(row)
    return _obj(row,["id","category","code","label","sort_order","active","source"])


@router.delete("/catalog-values/{catalog_id}")
def delete_catalog_value(catalog_id:int,current=Depends(require_module_permission("floresvolcan:config:manage")),db:Session=Depends(get_db)):
    del current
    row=db.query(CropCatalogValue).filter(CropCatalogValue.id==catalog_id).first()
    if not row:
        raise HTTPException(404,"Valor de catálogo no encontrado")
    refs={
        "variety": SapProduct.variety_catalog_id,
        "caliber": SapProduct.caliber_catalog_id,
        "common": SapProduct.common_catalog_id,
        "color": SapProduct.color_catalog_id,
    }
    ref_col=refs.get(row.category)
    if ref_col is not None:
        used=db.query(SapProduct.id).filter(ref_col==row.id).count()
        if used:
            raise HTTPException(409,f"No se puede eliminar: {used} código(s) SAP usan este valor. Puede desactivarlo o editarlo.")
    # Tombstone: desaparece del CRUD, pero evita que una futura activación vuelva a sembrar el valor por defecto.
    row.active=False; row.source="deleted"; db.commit()
    return {"ok":True,"deleted":True}


@router.get("/lirios/overview")
def lirios_overview(current=Depends(require_module_permission("floresvolcan:lilies:view")),db:Session=Depends(get_db)):
    del current
    crop=_lily_crop(db)
    return {
        "crop":_obj(crop,["id","code","name","material_type","active"]) if crop else None,
        "catalog_counts": {category: db.query(CropCatalogValue).filter(CropCatalogValue.crop_id==crop.id,CropCatalogValue.category==category,CropCatalogValue.active.is_(True)).count() for category in ["variety","color","caliber","common","density","cycle_weeks","discard_reason"]} if crop else {},
        "greenhouses":db.query(ProductionSpace).filter(ProductionSpace.type=="greenhouse",ProductionSpace.status=="active").count(),
        "beds":db.query(ProductionSpace).filter(ProductionSpace.type=="bed",ProductionSpace.status=="active").count(),
        "inventory_lots":db.query(InventoryLot).count(),
        "planting_records":db.query(PlantingRecord).count(),
        "harvest_events":db.query(HarvestRecord).count(),
        "open_batches":db.query(ProductionBatch).filter(ProductionBatch.status=="open").count(),
    }


@router.get("/worker-import/template")
def worker_import_template(current=Depends(require_module_permission("floresvolcan:workers:manage"))):
    del current
    data=build_worker_template()
    return StreamingResponse(BytesIO(data),media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",headers={"Content-Disposition":"attachment; filename=plantilla_trabajadores_floresvolcan.xlsx"})


@router.post("/worker-import")
async def worker_import(file:UploadFile=File(...),current=Depends(require_module_permission("floresvolcan:workers:manage")),db:Session=Depends(get_db)):
    del current
    if not (file.filename or "").lower().endswith(".xlsx"):
        raise HTTPException(422,"Use la plantilla .xlsx de trabajadores")
    try:
        return import_workers(db, await file.read())
    except ValueError as exc:
        raise HTTPException(422,str(exc)) from exc


@router.get("/exports/lirios/{variant}")
def export_lirios(variant:str,current=Depends(require_module_permission("floresvolcan:exports:use")),db:Session=Depends(get_db)):
    del current
    if variant.lower() not in {"lirio","liriodoc"}: raise HTTPException(404,"Exportación no disponible")
    data=build_lirio_workbook(db,variant)
    filename="Liriodoc.xlsx" if variant.lower()=="liriodoc" else "Lirio.xlsx"
    return StreamingResponse(BytesIO(data),media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",headers={"Content-Disposition":f"attachment; filename={filename}"})


@router.get("/exports/lirios-bundle")
def export_lirios_bundle(current=Depends(require_module_permission("floresvolcan:exports:use")),db:Session=Depends(get_db)):
    del current
    data=build_excel_bundle(db)
    return StreamingResponse(BytesIO(data),media_type="application/zip",headers={"Content-Disposition":"attachment; filename=FloresVolcan_Lirios_Excel.zip"})


@router.get("/backup/settings")
def get_backup_settings(current=Depends(require_module_permission("floresvolcan:backup:manage")),db:Session=Depends(get_db)):
    del current
    return backup_public_settings(db)


@router.put("/backup/settings")
def put_backup_settings(payload:BackupSettingsIn,current=Depends(require_module_permission("floresvolcan:backup:manage")),db:Session=Depends(get_db)):
    del current
    data=payload.model_dump()
    backup_set_settings(db,{
        "backup.enabled":str(data["enabled"]).lower(),"backup.frequency":data["frequency"],"backup.hour":str(data["hour"]),
        "backup.weekday":str(data["weekday"]),"backup.retention_days":str(data["retention_days"]),"backup.destination":data["destination"],
        "backup.drive_folder_id":data.get("drive_folder_id") or "","backup.drive_client_id":data.get("drive_client_id") or "",
        "backup.drive_client_secret":data.get("drive_client_secret") or "","backup.drive_refresh_token":data.get("drive_refresh_token") or "",
    })
    return backup_public_settings(db)


@router.post("/backup/run", status_code=201)
def manual_backup(destination:str|None=None,current:Usuario=Depends(require_module_permission("floresvolcan:backup:manage")),db:Session=Depends(get_db)):
    row=run_backup(db,current.id,destination)
    if row.status=="failed": raise HTTPException(502,row.error or "Falló el respaldo")
    return _obj(row,["id","status","destination","filename","storage_key","drive_file_id","size_bytes","created_at","completed_at"])


@router.get("/backup/runs")
def backup_runs(current=Depends(require_module_permission("floresvolcan:backup:manage")),db:Session=Depends(get_db)):
    del current
    rows=db.query(BackupRun).order_by(BackupRun.created_at.desc()).limit(100).all()
    return [_obj(x,["id","status","destination","filename","drive_file_id","size_bytes","error","created_at","completed_at"]) for x in rows]


@router.get("/backup/runs/{backup_id}/download")
def download_backup(backup_id:int,current=Depends(require_module_permission("floresvolcan:backup:manage")),db:Session=Depends(get_db)):
    del current
    row=db.query(BackupRun).filter(BackupRun.id==backup_id).first()
    if not row or not row.storage_key: raise HTTPException(404,"Respaldo local no disponible")
    path=Path(row.storage_key)
    if not path.is_file(): raise HTTPException(404,"Archivo de respaldo no encontrado")
    return FileResponse(path,media_type="application/zip",filename=row.filename or path.name)


@router.get("/requisitions/{requisition_id}/deliveries")
def requisition_deliveries(requisition_id:int,current=Depends(require_module_permission("floresvolcan:requisitions:view")),db:Session=Depends(get_db)):
    del current
    req=db.query(Requisition).filter(Requisition.id==requisition_id).first()
    if not req: raise HTTPException(404,"Requisición no encontrada")
    rows=(db.query(RequisitionDelivery,RequisitionLine,InventoryLot,SapProduct)
          .join(RequisitionLine,RequisitionLine.id==RequisitionDelivery.requisition_line_id)
          .join(InventoryLot,InventoryLot.id==RequisitionDelivery.inventory_lot_id)
          .join(SapProduct,SapProduct.id==RequisitionLine.sap_product_id)
          .filter(RequisitionLine.requisition_id==req.id).order_by(RequisitionDelivery.delivered_at).all())
    return [{**_obj(d,["id","number","requisition_line_id","inventory_lot_id","boxes","units_per_box","quantity","delivery_week","delivered_at","comments"]),
             "lot_code":lot.lot_code,"supplier_lot_code":lot.supplier_lot_code,"product_code":product.code,"variety":lot.variety_snapshot or product.variety}
            for d,line,lot,product in rows]


@router.post("/requisitions/{requisition_id}/deliveries", status_code=201)
def deliver_requisition(requisition_id:int,payload:RequisitionDeliveryIn,current:Usuario=Depends(require_module_permission("floresvolcan:inventory:manage")),db:Session=Depends(get_db)):
    req=db.query(Requisition).filter(Requisition.id==requisition_id).first()
    if not req: raise HTTPException(404,"Requisición no encontrada")
    if req.status not in {"approved","partially_delivered"}: raise HTTPException(409,"Solo se entrega material de una requisición aprobada con saldo pendiente")
    line=db.query(RequisitionLine).filter(RequisitionLine.id==payload.requisition_line_id,RequisitionLine.requisition_id==req.id).with_for_update().first()
    if not line: raise HTTPException(422,"La línea no pertenece a la requisición")
    qty=_decimal(payload.quantity)
    pending=_decimal(line.quantity_approved)-_decimal(line.quantity_delivered)
    if qty>pending: raise HTTPException(409,f"La entrega supera el saldo pendiente ({pending})")
    lot=db.query(InventoryLot).filter(InventoryLot.id==line.inventory_lot_id).with_for_update().first()
    if not lot: raise HTTPException(409,"Lote de inventario inexistente")
    if _decimal(lot.quantity_reserved)<qty: raise HTTPException(409,"El lote ya no tiene suficiente cantidad reservada")
    delivered_at=payload.delivered_at or now()
    row=RequisitionDelivery(number=_next("SAL",db.query(RequisitionDelivery).count()),requisition_line_id=line.id,inventory_lot_id=lot.id,
        boxes=payload.boxes,units_per_box=payload.units_per_box or lot.units_per_box,quantity=qty,delivery_week=_iso_week(delivered_at),
        delivered_at=delivered_at,delivered_by_id=current.id,comments=payload.comments)
    db.add(row); db.flush()
    lot.quantity_reserved=_decimal(lot.quantity_reserved)-qty
    lot.quantity_issued=_decimal(lot.quantity_issued)+qty
    line.quantity_delivered=_decimal(line.quantity_delivered)+qty
    db.add(InventoryMovement(inventory_lot_id=lot.id,movement_type="ISSUE_TO_FIELD",quantity=qty,reference_type="requisition_delivery",reference_id=row.id,user_id=current.id,comments=payload.comments))
    if line.quantity_delivered>=line.quantity_approved:
        all_lines=db.query(RequisitionLine).filter(RequisitionLine.requisition_id==req.id).all()
        if all(_decimal(x.quantity_delivered)>=_decimal(x.quantity_approved) for x in all_lines): req.status="delivered"
        else: req.status="partially_delivered"
    else: req.status="partially_delivered"
    db.commit(); db.refresh(row)
    return _obj(row,["id","number","requisition_line_id","quantity","boxes","units_per_box","delivery_week","delivered_at","comments"])
