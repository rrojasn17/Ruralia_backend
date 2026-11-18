from __future__ import annotations

import os
import uuid
import secrets
import json
from io import BytesIO
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from fastapi import Body, APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy import func, or_, case
from sqlalchemy.orm import Session, joinedload, aliased

from core.models import Usuario, ModuleUserRole, ConfiguracionSistema
from core.module_manager import require_module_permission, role_for_module
from core.routers.auth import get_current_user
from database import get_db
from modules.mod_floresvolcan.field_ai import interpret_field_text, transcribe_field_audio
from modules.mod_floresvolcan.models import (
    BackupRun, CropCatalogValue, FlowerCrop, GoodsReceipt, GoodsReceiptLine, HarvestRecord, InventoryIssue, InventoryLot, InventoryMovement,
    PlantingOrder, PlantingRecord, ProductionBatch, ProductionSpace, PurchaseOrder,
    PurchaseOrderEvent, PurchaseOrderEventAttachment, PurchaseOrderLine, Requisition, RequisitionDelivery,
    RequisitionLine, SapProduct, Supplier, SupplierContact, TechnicalClosure, Worker,
    WorkCrew, WorkCrewMember, ModuleSetting, HarvestOrder, PaperCapture,
    BedCycle, BedCropProfile, PlantingOrderBed, SegmentReconciliation, MaterialResolution, HistoricalOverageApproval,
)
from modules.mod_floresvolcan.schemas import (
    FlowerCropIn, CrewIn, EventIn, FieldInterpretIn, GoodsReceiptIn, HarvestRecordIn, InventoryWriteoffIn, PlantingOrderIn,
    PlantingRecordIn, ProductionSpaceIn, PurchaseOrderIn, RequisitionIn, SapProductIn, SapProductStatusIn,
    SupplierIn, TechnicalClosureIn, WorkerIn, CropCatalogValueIn, CropCatalogValuePatch, RequisitionDeliveryIn, BackupSettingsIn, HarvestOrderIn,
    BedCleaningIn, MaterialResolutionIn, HistoricalHarvestLinkIn, ProductionReviewIn, HistoricalOverageIn,
)
from modules.mod_floresvolcan.production_control import (
    bed_planting_availability,
    bulk_batch_metrics, batch_metrics, batch_segments, check_retry, request_hash, operation_time, local_date,
    lock_bed, lock_batch, planting_cycle, occupied_area, pending_to_plant, pending_delivery,
    require_linked_history, authorized_overage, source_supplier, LOCAL_TZ,
)
from fastapi.responses import FileResponse, StreamingResponse
from modules.mod_floresvolcan.legacy_excel import build_lirio_workbook, build_excel_bundle
from modules.mod_floresvolcan.backups import public_settings as backup_public_settings, set_settings as backup_set_settings, run_backup
from modules.mod_floresvolcan.workers_excel import build_worker_template, import_workers
from modules.mod_floresvolcan.paper_forms import build_field_pdf, normalize_scan, suggest_photo_data, paper_storage_dir

router = APIRouter(prefix="/floresvolcan", tags=["floresvolcan"])


def _page(query, page: int | None, page_size: int):
    """Opt-in pagination; legacy array callers keep their existing contract."""
    if page is None:
        return query.all(), None
    if page < 1 or not 1 <= page_size <= 100:
        raise HTTPException(422, "Página o tamaño inválido (máximo 100)")
    total = query.order_by(None).count()
    return query.offset((page - 1) * page_size).limit(page_size).all(), total


def _paged(items, total, page, page_size, **extra):
    return items if page is None else {"items": items, "total": total, "page": page, "page_size": page_size, **extra}


def _search(query, text, *columns):
    # Escaping makes codes containing % or _ literal, never wildcard-only scans.
    if text and text.strip():
        term = text.strip().replace("!", "!!").replace("%", "!%").replace("_", "!_")
        query = query.filter(or_(*(c.ilike(f"%{term}%", escape="!") for c in columns)))
    return query


def now() -> datetime:
    return datetime.now(timezone.utc)


def _decimal(value) -> Decimal:
    return Decimal(str(value or 0))


def _next(prefix: str, count: int, width: int = 6) -> str:
    # Mantiene el consecutivo visible y usa sufijo aleatorio para evitar colisiones por carreras/borrados.
    return f"{prefix}-{now().year}-{count + 1:0{width}d}-{uuid.uuid4().hex[:10].upper()}"


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
    target = local_date(value) if isinstance(value, datetime) else value
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
    user = db.get(Usuario, user_id)
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
    product.color = color.code if color else (payload.color.strip() if payload.color else None)
    product.purchase_unit = payload.purchase_unit.strip().lower()
    product.inventory_unit = payload.inventory_unit.strip().lower()
    product.unit = product.inventory_unit
    product.units_per_purchase_unit = payload.units_per_purchase_unit
    product.expected_stems_per_bulb = payload.expected_stems_per_bulb


def _field_worker(db: Session, current: Usuario) -> Worker | None:
    from .field_access import FieldActor
    if isinstance(current, FieldActor):
        worker = db.get(Worker, current.field_worker_id)
        if not worker or not worker.active: raise HTTPException(403, "Trabajador inactivo")
        return worker
    # Administradores conservan soporte operativo sin usurpar cuentas de trabajadores.
    if current.is_superadmin or str(role_for_module(db,current,'mod_floresvolcan') or '').lower() in {"admin", "gerente"}:
        return None
    if str(role_for_module(db,current,'mod_floresvolcan') or '').lower() != "trabajador":
        raise HTTPException(403, "Acceso exclusivo de campo")
    worker = db.query(Worker).filter(Worker.user_id == current.id, Worker.active.is_(True)).first()
    if not worker:
        raise HTTPException(403, "Debe vincular su cuenta con un trabajador activo")
    return worker


def _active_crew_ids(db: Session, worker: Worker | None) -> list[int]:
    if worker is None:
        return [x.id for x in db.query(WorkCrew).filter(WorkCrew.active.is_(True)).all()]
    today = datetime.now(LOCAL_TZ).date()
    return [x.crew_id for x in db.query(WorkCrewMember).join(WorkCrew, WorkCrew.id == WorkCrewMember.crew_id)
            .filter(WorkCrewMember.worker_id == worker.id, WorkCrew.active.is_(True),
                    WorkCrewMember.valid_from <= today,
                    or_(WorkCrewMember.valid_until.is_(None), WorkCrewMember.valid_until >= today)).all()]


def _assert_crew(db: Session, current: Usuario, crew_id: int) -> None:
    worker = _field_worker(db, current)
    if worker is not None and crew_id not in _active_crew_ids(db, worker):
        raise HTTPException(403, "No pertenece a la cuadrilla asignada a esta orden")


def _assert_paper_capture(db: Session, capture_id: int | None, operation: str, order_id: int, current: Usuario) -> PaperCapture | None:
    if capture_id is None:
        return None
    if not current.is_superadmin and str(role_for_module(db,current,'mod_floresvolcan') or '').lower() not in {"admin", "administrador", "gerente"}:
        raise HTTPException(403, "La hoja debe ser revisada y aprobada por un administrador")
    row = db.query(PaperCapture).filter(PaperCapture.id == capture_id).with_for_update().first()
    if not row or row.operation != operation or row.status not in {"pending_review", "submitted"}:
        raise HTTPException(409, "Fotografía inexistente o ya digitalizada")
    if operation == "planting" and row.planting_order_id != order_id:
        raise HTTPException(422, "La foto pertenece a otra orden de siembra")
    if operation == "harvest" and row.harvest_order_id != order_id:
        raise HTTPException(422, "La foto pertenece a otra orden de cosecha")
    if not current.is_superadmin and str(role_for_module(db,current,'mod_floresvolcan') or '').lower() not in {"admin", "gerente"} and row.created_by_id != current.id:
        raise HTTPException(403, "Esta captura pertenece a otro trabajador")
    return row


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
        "default_cycle_weeks", "expected_stems_per_bulb", "created_at", "updated_at"
    ])


def po_dict(row: PurchaseOrder, include_detail=True, db: Session | None = None):
    data = _obj(row, ["id", "number", "supplier_id", "requested_by_id", "approver_user_id", "warehouse_due_date", "status", "comments", "subtotal", "tax_total", "total_estimated", "approved_at", "approved_by_id", "completed_at", "cancelled_at", "created_at", "updated_at"])
    data["reconstructed_from_excel"] = row.number.startswith("XL-")
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
    data["paid_at"]=row.paid_at
    data["shipped_at"]=row.shipped_at
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
        "has_unpriced_excel_orders": any(x.number.startswith("XL-") for x in orders),
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
    from core.models import ModuleUserRole
    approvers=db.query(Usuario).outerjoin(ModuleUserRole,(ModuleUserRole.usuario_id==Usuario.id)&(ModuleUserRole.module_key=='mod_floresvolcan')).filter(
        Usuario.activo.is_(True),or_(Usuario.is_superadmin.is_(True),ModuleUserRole.role.in_(['gerente','superadmin']),
        func.lower(Usuario.rol)=='gerente')).order_by(Usuario.nombre).all()
    return {
        "suppliers":[{"id":x.id,"code":x.code,"name":x.name,"country":x.country,"active":bool(x.active)} for x in suppliers],
        "approvers":[{"id":x.id,"nombre":x.nombre,"rol":x.rol} for x in approvers],
    }


# ---------- SAP ----------
@router.get("/sap-products")
def list_products(q: str | None = None, active: bool | None = None, current=Depends(require_module_permission("floresvolcan:sap:view")), db: Session = Depends(get_db), page: int | None = None, page_size: int = 40):
    del current
    query = db.query(SapProduct)
    if active is not None: query = query.filter(SapProduct.active == active)
    if q: query = query.filter((SapProduct.code.ilike(f"%{q}%")) | (SapProduct.description.ilike(f"%{q}%")) | (SapProduct.variety.ilike(f"%{q}%")))
    rows, total = _page(query.order_by(SapProduct.code, SapProduct.id), page, page_size)
    from .technical_materials import references
    refs=references(db,[x.id for x in rows])
    return _paged([{**product_dict(x),"technical_reference":refs.get(x.id)} for x in rows], total, page, page_size)


@router.get("/sap-options")
def sap_options(crop_id:int|None=None,current=Depends(require_module_permission("floresvolcan:sap:view")), db: Session = Depends(get_db)):
    del current
    crop = db.get(FlowerCrop,crop_id) if crop_id else _lily_crop(db)
    values = [] if not crop else db.query(CropCatalogValue).filter(CropCatalogValue.crop_id == crop.id, CropCatalogValue.active.is_(True)).order_by(CropCatalogValue.category, CropCatalogValue.sort_order, CropCatalogValue.label).all()
    grouped: dict[str, list[dict]] = {}
    for item in values:
        grouped.setdefault(item.category, []).append(_obj(item, ["id", "code", "label", "sort_order"]))
    from .material_defaults import material_profiles
    return {"material_defaults":material_profiles() if crop and crop.code=="LIRIO" else [],
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


@router.get("/purchase-catalog")
def purchase_catalog(q: str | None = None, selected_id: int | None = None,
                     page: int = 1, page_size: int = 30,
                     current=Depends(require_module_permission("floresvolcan:purchases:create")),
                     db: Session = Depends(get_db)):
    del current
    query = db.query(SapProduct).filter(SapProduct.active.is_(True))
    if selected_id is not None:
        query = query.filter(SapProduct.id == selected_id)
    query = _search(query, q, SapProduct.code, SapProduct.description, SapProduct.variety)
    rows, total = _page(query.order_by(SapProduct.code, SapProduct.id), page, page_size)
    return _paged([product_dict(row) for row in rows], total, page, page_size)


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
    crop = db.get(FlowerCrop,payload.crop_id) if payload.crop_id else _lily_crop(db)
    if not crop or not crop.active: raise HTTPException(422,"Cultivo no disponible")
    if 'material_type' not in payload.model_fields_set:payload.material_type=crop.material_type
    if 'inventory_unit' not in payload.model_fields_set:payload.inventory_unit=payload.material_type
    row = SapProduct(
        code=code, description=payload.description.strip(), crop_id=(crop.id if crop else None), created_by_id=current.id,
        material_type=payload.material_type.strip().lower(), last_price=payload.last_price, currency=payload.currency,
        active=payload.active, default_density=payload.default_density, default_cycle_weeks=payload.default_cycle_weeks,
    )
    _apply_product_catalogs(db, row, payload)
    if crop and crop.code=="LIRIO":
        from .material_defaults import complete_material
        complete_material(row)
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
    if payload.crop_id is not None:
        crop=db.get(FlowerCrop,payload.crop_id)
        if not crop or not crop.active:raise HTTPException(422,"Cultivo no disponible")
        row.crop_id=crop.id
    _apply_product_catalogs(db, row, payload)
    if row.crop_id and db.get(FlowerCrop,row.crop_id).code=="LIRIO":
        from .material_defaults import complete_material
        complete_material(row)
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
def list_purchase_orders(status: str | None = None, current=Depends(require_module_permission("floresvolcan:purchases:view")), db: Session = Depends(get_db), page: int | None = None, page_size: int = 40, selected_id: int | None = None, q: str | None = None, exclude_status: str | None = None):
    del current
    query = db.query(PurchaseOrder).join(Supplier).options(joinedload(PurchaseOrder.supplier))
    if status: query = query.filter(PurchaseOrder.status.in_(status.split(",")))
    if exclude_status: query=query.filter(~PurchaseOrder.status.in_(exclude_status.split(",")))
    query = _search(query, q, PurchaseOrder.number, Supplier.name)
    if selected_id is not None: query = query.filter(PurchaseOrder.id == selected_id)
    rows, total = _page(query.order_by(PurchaseOrder.created_at.desc(), PurchaseOrder.id.desc()), page, page_size)
    return _paged([po_dict(x, False, db=db) for x in rows], total, page, page_size)


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
def inventory(current=Depends(require_module_permission("floresvolcan:inventory:view")), db: Session = Depends(get_db), page: int | None = None, page_size: int = 40, selected_id: int | None = None, q: str | None = None, available_only: bool = False, crop_id: int | None = None, empty_only: bool = False, sap_product_id: int | None = None, variety: str | None = None, color: str | None = None, common_code: str | None = None):
    del current
    query = db.query(InventoryLot).join(SapProduct).options(joinedload(InventoryLot.product))
    query = _search(query, q, InventoryLot.lot_code, InventoryLot.supplier_lot_code, InventoryLot.container_number,
                    InventoryLot.variety_snapshot, SapProduct.code, SapProduct.description, SapProduct.variety)
    if available_only: query = query.filter(InventoryLot.quantity_available > 0)
    if empty_only: query = query.filter(InventoryLot.quantity_available <= 0)
    if crop_id is not None: query = query.filter(or_(SapProduct.crop_id == crop_id, SapProduct.crop_id.is_(None)))
    if sap_product_id is not None: query = query.filter(InventoryLot.sap_product_id == sap_product_id)
    for value, snapshot, original in ((variety, InventoryLot.variety_snapshot, func.coalesce(SapProduct.variety,SapProduct.description)),
                                     (color, InventoryLot.color_snapshot, SapProduct.color),
                                     (common_code, InventoryLot.common_code_snapshot, SapProduct.common_code)):
        if value is not None: query=query.filter(func.coalesce(func.nullif(snapshot,''),original,'')==value)
    if selected_id is not None: query = query.filter(InventoryLot.id == selected_id)
    rows, total = _page(query.order_by(InventoryLot.received_at.desc(), InventoryLot.id.desc()), page, page_size)
    totals = {}
    if page is not None:
        sums = db.query(*(func.coalesce(func.sum(c), 0) for c in (InventoryLot.quantity_available, InventoryLot.quantity_reserved, InventoryLot.quantity_issued))).one()
        totals = dict(zip(("available", "reserved", "field"), sums))
    # Resolve purchase origins in one query for the current page (including archived lots).
    origins = {
        line_id: {"supplier_name": supplier_name, "purchase_order_id": order_id,
                  "purchase_order_number": number, "purchase_order_created_at": created_at}
        for line_id, supplier_name, order_id, number, created_at in (
            db.query(GoodsReceiptLine.id, Supplier.name, PurchaseOrder.id,
                     PurchaseOrder.number, PurchaseOrder.created_at)
            .join(GoodsReceipt, GoodsReceipt.id == GoodsReceiptLine.goods_receipt_id)
            .join(PurchaseOrder, PurchaseOrder.id == GoodsReceipt.purchase_order_id)
            .outerjoin(Supplier, Supplier.id == PurchaseOrder.supplier_id)
            .filter(GoodsReceiptLine.id.in_([x.goods_receipt_line_id for x in rows])).all()
        )
    }
    items = [
        {
            **origins.get(x.goods_receipt_line_id, {}),
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
    return _paged(items, total, page, page_size, summary=totals)


@router.post("/goods-receipts", status_code=201)
def receive_goods(payload: GoodsReceiptIn, current: Usuario = Depends(require_module_permission("floresvolcan:inventory:receive")), db: Session = Depends(get_db)):
    po = db.query(PurchaseOrder).filter(PurchaseOrder.id == payload.purchase_order_id).populate_existing().with_for_update().first()
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
def list_workers(current=Depends(require_module_permission("floresvolcan:workers:view")), db: Session = Depends(get_db),
                 page: int | None = None, page_size: int = 40, q: str | None = None, active_only: bool = False):
    del current
    query = db.query(Worker)
    if active_only: query = query.filter(Worker.active.is_(True))
    query = _search(query, q, Worker.name, Worker.employee_code, Worker.identification, Worker.job_title)
    rows, total = _page(query.order_by(Worker.name, Worker.id), page, page_size)
    users = {u.id: u.nombre for u in db.query(Usuario.id, Usuario.nombre).filter(Usuario.id.in_({w.user_id for w in rows if w.user_id})).all()}
    result = [{**_obj(x, ["id", "employee_code", "identification", "name", "hire_date", "nationality", "birth_date", "daily_salary", "job_title", "active", "user_id"]),
               "user_name": users.get(x.user_id)} for x in rows]
    return _paged(result, total, page, page_size)


def _validate_worker_user(db: Session, user_id: int | None, worker_id: int | None = None) -> None:
    if user_id is None:
        return
    user = db.query(Usuario).filter(Usuario.id == user_id, Usuario.activo.is_(True)).first()
    if not user:
        raise HTTPException(422, "Usuario de acceso no existe o está inactivo")
    if role_for_module(db, user, "mod_floresvolcan") != "trabajador" and not user.is_superadmin:
        raise HTTPException(422, "El usuario vinculado debe tener rol trabajador")
    duplicate = db.query(Worker).filter(Worker.user_id == user_id)
    if worker_id:
        duplicate = duplicate.filter(Worker.id != worker_id)
    if duplicate.first():
        raise HTTPException(409, "Ese usuario ya está vinculado a otro trabajador")


@router.post("/workers", status_code=201)
def create_worker(payload: WorkerIn, current=Depends(require_module_permission("floresvolcan:workers:manage")), db: Session = Depends(get_db)):
    del current
    if db.query(Worker).filter(Worker.identification == payload.identification).first():
        raise HTTPException(409, "Código o identificación ya registrados")
    _validate_worker_user(db, payload.user_id)
    from .worker_codes import allocate_worker_code
    row = Worker(**{**payload.model_dump(), "employee_code": allocate_worker_code(db)})
    db.add(row); db.commit(); db.refresh(row)
    return {**_obj(row, ["id", "employee_code", "identification", "name", "active", "user_id"]), "user_name": _user_label(db, row.user_id)}


@router.put("/workers/{worker_id}")
def update_worker(worker_id:int,payload:WorkerIn,current=Depends(require_module_permission("floresvolcan:workers:manage")),db:Session=Depends(get_db)):
    del current
    row=db.query(Worker).filter(Worker.id==worker_id).first()
    if not row: raise HTTPException(404,"Trabajador no encontrado")
    _validate_worker_user(db, payload.user_id, row.id)
    for k,v in payload.model_dump(exclude={"employee_code"}).items(): setattr(row,k,v)
    db.commit(); db.refresh(row)
    return {**_obj(row,["id","employee_code","identification","name","active","user_id"]), "user_name": _user_label(db, row.user_id)}


@router.delete("/workers/{worker_id}")
def deactivate_worker(worker_id:int,current=Depends(require_module_permission("floresvolcan:workers:manage")),db:Session=Depends(get_db)):
    del current
    row=db.query(Worker).filter(Worker.id==worker_id).first()
    if not row: raise HTTPException(404,"Trabajador no encontrado")
    row.active=False; db.commit(); return {"ok":True,"active":False}


@router.get("/worker-user-options")
def worker_user_options(current=Depends(require_module_permission("floresvolcan:workers:manage")), db: Session = Depends(get_db),
                        page: int | None = None, page_size: int = 40, q: str | None = None, selected_id: int | None = None,
                        available_only: bool = False):
    del current
    query=db.query(Usuario).join(ModuleUserRole,ModuleUserRole.usuario_id==Usuario.id).filter(
        Usuario.activo.is_(True),ModuleUserRole.module_key=='mod_floresvolcan',ModuleUserRole.role=='trabajador'
    )
    # Fallback de instalaciones pre-migración. No propagar identidad de otra industria.
    if not db.query(ModuleUserRole.id).first() and not db.query(ConfiguracionSistema.id).filter(
        ConfiguracionSistema.clave=='module_roles_v1_backfill').first():
        query=db.query(Usuario).filter(Usuario.activo.is_(True),func.lower(Usuario.rol)=='trabajador')
    query=_search(query,q,Usuario.nombre,Usuario.correo)
    if selected_id is not None: query=query.filter(Usuario.id==selected_id)
    if available_only:
        # Evita listas de miles de usuarios ya vinculados, salvo el registro editado.
        taken=db.query(Worker.user_id).filter(Worker.user_id.isnot(None))
        if selected_id is not None:
            query=query.filter(or_(~Usuario.id.in_(taken),Usuario.id==selected_id))
        else: query=query.filter(~Usuario.id.in_(taken))
    rows,total=_page(query.order_by(Usuario.nombre,Usuario.id),page,page_size)
    linked={x[0] for x in db.query(Worker.user_id).filter(Worker.user_id.in_([u.id for u in rows])).all()}
    return _paged([{'id':u.id,'nombre':u.nombre,'correo':u.correo,'linked':u.id in linked} for u in rows],total,page,page_size)


@router.get("/crews")
def list_crews(current=Depends(require_module_permission("floresvolcan:planting:view")), db:Session=Depends(get_db),
               page:int|None=None,page_size:int=40,q:str|None=None,selected_id:int|None=None,active_only:bool=False,
               activity:str|None=None,work_date:date|None=None):
    from .crew_management import serialize
    query=_search(db.query(WorkCrew),q,WorkCrew.name)
    configs={int(p.key.split('.')[-1]):json.loads(p.value_text) for p in db.query(ModuleSetting).filter(ModuleSetting.key.like('crew_config.%')).all()}
    excluded=[]
    for crew_id,config in configs.items():
        if config.get('kind')!='temporary':continue
        if (active_only and config.get('work_date','')<str(datetime.now(LOCAL_TZ).date())) or (activity and config.get('activity')!=activity) or (work_date and config.get('work_date')!=str(work_date)):
            excluded.append(crew_id)
    if excluded:query=query.filter(~WorkCrew.id.in_(excluded))
    if active_only:query=query.filter(WorkCrew.active.is_(True))
    if selected_id is not None:query=query.filter(WorkCrew.id==selected_id)
    rows,total=_page(query.options(joinedload(WorkCrew.members).joinedload(WorkCrewMember.worker)).order_by(WorkCrew.name,WorkCrew.id),page,page_size)
    items=[serialize(db,r,configs.get(r.id,{'kind':'permanent','activity':None,'work_date':None})) for r in rows]
    return _paged(items,total,page,page_size)


@router.get('/crews/{crew_id}')
def get_crew(crew_id:int,current=Depends(require_module_permission('floresvolcan:planting:view')),db:Session=Depends(get_db)):
    from .crew_management import serialize
    row=db.query(WorkCrew).filter_by(id=crew_id).first()
    if not row:raise HTTPException(404,'Cuadrilla no encontrada')
    return serialize(db,row)


def _save_crew(db,payload,current,crew_id=None):
    from .crew_management import save,serialize
    query=db.query(WorkCrew).filter(func.lower(WorkCrew.name)==payload.name.strip().lower())
    if crew_id is not None:query=query.filter(WorkCrew.id!=crew_id)
    if query.first():raise HTTPException(409,'Ya existe una cuadrilla con este nombre')
    row=db.query(WorkCrew).filter_by(id=crew_id).with_for_update().first() if crew_id else WorkCrew(name=payload.name.strip())
    if not row:raise HTTPException(404,'Cuadrilla no encontrada')
    if crew_id is None:db.add(row);db.flush()
    save(db,row,payload,current,datetime.now(LOCAL_TZ).date(),creating=crew_id is None)
    db.commit();return serialize(db,row)


@router.post('/crews',status_code=201)
def create_crew(payload:CrewIn,current=Depends(require_module_permission('floresvolcan:workers:manage')),db:Session=Depends(get_db)):
    return _save_crew(db,payload,current)


@router.put('/crews/{crew_id}')
def update_crew(crew_id:int,payload:CrewIn,current=Depends(require_module_permission('floresvolcan:workers:manage')),db:Session=Depends(get_db)):
    return _save_crew(db,payload,current,crew_id)


@router.delete('/crews/{crew_id}')
def delete_crew(crew_id:int,current=Depends(require_module_permission('floresvolcan:workers:manage')),db:Session=Depends(get_db)):
    from .crew_management import links
    if not current.is_superadmin:raise HTTPException(403,'Solo el superusuario puede borrar cuadrillas')
    row=db.query(WorkCrew).filter_by(id=crew_id).with_for_update().first()
    if not row:raise HTTPException(404,'Cuadrilla no encontrada')
    if links(db,crew_id,opened=True):raise HTTPException(409,'Finalice las órdenes pendientes antes de borrar la cuadrilla')
    row.active=False;db.commit();return {'ok':True,'active':False}


def _profile_dict(profile: BedCropProfile, crop: FlowerCrop | None = None) -> dict:
    return {**_obj(profile, ['id', 'bed_space_id', 'crop_id', 'material_type', 'input_mode',
                             'capacity_units', 'density', 'active']),
            'crop_name': crop.name if crop else None}


def _save_bed_profiles(db: Session, bed: ProductionSpace, profiles) -> None:
    if profiles is None:
        return
    if bed.type != 'bed':
        if profiles:
            raise HTTPException(422, 'Los perfiles de flores solo se configuran en camas')
        return
    if not bed.length_m or not bed.width_m:
        raise HTTPException(422, 'Indique largo y ancho para configurar los bulbos por cama')
    area = _decimal(bed.length_m) * _decimal(bed.width_m)
    if area <= 0:
        raise HTTPException(422, 'La cama debe tener superficie mayor a cero')
    ids = [p.crop_id for p in profiles]
    if len(ids) != len(set(ids)):
        raise HTTPException(422, 'No repita el mismo tipo de flor en la cama')
    crops = {c.id: c for c in db.query(FlowerCrop).filter(FlowerCrop.id.in_(ids), FlowerCrop.active.is_(True)).all()}
    if len(crops) != len(ids):
        raise HTTPException(422, 'Algún tipo de flor no existe o está inactivo')
    previous = {p.crop_id: p for p in db.query(BedCropProfile).filter(BedCropProfile.bed_space_id == bed.id).all()}
    for crop_id, old in previous.items():
        if crop_id not in ids:
            old.active = False  # nunca borrar configuración usada históricamente
    for item in profiles:
        crop = crops[item.crop_id]
        quantity = item.capacity_units or int((item.density * area).to_integral_value())
        density = item.density or (Decimal(quantity) / area)
        if quantity <= 0:
            raise HTTPException(422, 'Cantidad de bulbos debe ser positiva')
        current = previous.get(item.crop_id)
        if current and current.material_type != (item.material_type or crop.material_type):
            raise HTTPException(409, 'El tipo de material ya está registrado; conserve su historial')
        if current:
            current.capacity_units, current.density, current.active = quantity, density, True
            current.input_mode = 'units' if item.capacity_units is not None else 'density'
        else:
            db.add(BedCropProfile(bed_space_id=bed.id, crop_id=crop.id,
                material_type=item.material_type or crop.material_type,
                capacity_units=quantity, density=density,
                input_mode='units' if item.capacity_units is not None else 'density', active=True))
    # Capacidad física de la cama en metros cuadrados; no sobrecargarla con bulbos.
    bed.capacity = area
    bed.area = area
    bed.area_unit = 'm2'


def _order_bed_summary(db: Session, order: PlantingOrder) -> list[dict]:
    links = db.query(PlantingOrderBed).filter(PlantingOrderBed.planting_order_id == order.id).order_by(PlantingOrderBed.id).all()
    if not links:
        return []
    beds = {b.id: b for b in db.query(ProductionSpace).filter(ProductionSpace.id.in_([x.bed_space_id for x in links])).all()}
    planted = dict(db.query(PlantingRecord.bed_space_id, func.sum(PlantingRecord.quantity_planted)).filter(
        PlantingRecord.planting_order_id == order.id, PlantingRecord.bed_space_id.in_([x.bed_space_id for x in links])
    ).group_by(PlantingRecord.bed_space_id).all())
    return [{'id': x.bed_space_id, 'code': beds[x.bed_space_id].code, 'name': beds[x.bed_space_id].name,
             'capacity_units': x.capacity_units_snapshot, 'planted_quantity': planted.get(x.bed_space_id, 0)}
            for x in links if x.bed_space_id in beds]


# ---------- Espacios ----------
@router.get("/production-spaces")
def list_spaces(current=Depends(require_module_permission("floresvolcan:spaces:view")), db: Session = Depends(get_db), page: int | None = None, page_size: int = 40, q: str | None = None, type: str | None = None, status: str | None = None, selected_id: int | None = None):
    del current
    parent = aliased(ProductionSpace)
    query = db.query(ProductionSpace).outerjoin(parent, ProductionSpace.parent_id == parent.id)
    query = _search(query, q, ProductionSpace.code, ProductionSpace.name, parent.name, parent.code)
    if type: query = query.filter(ProductionSpace.type.in_([x.strip() for x in type.split(",") if x.strip()]))
    if status: query = query.filter(ProductionSpace.status == status)
    if selected_id is not None: query = query.filter(ProductionSpace.id == selected_id)
    rows, total = _page(query.order_by(ProductionSpace.code, ProductionSpace.id), page, page_size)
    parents = dict(db.query(ProductionSpace.id, ProductionSpace.name).filter(ProductionSpace.id.in_({x.parent_id for x in rows})).all()) if page is not None else {}
    items = [{**_obj(x,["id","parent_id","code","name","type","latitude","longitude","area","area_unit","capacity","status"]),
              **({"parent_name": parents.get(x.parent_id)} if page is not None else {})} for x in rows]
    return _paged(items, total, page, page_size)


@router.get("/production-spaces/{space_id}")
def get_space(space_id:int,current=Depends(require_module_permission("floresvolcan:spaces:view")),db:Session=Depends(get_db)):
    del current
    row=db.query(ProductionSpace).filter(ProductionSpace.id==space_id).first()
    if not row: raise HTTPException(404,"Espacio productivo no encontrado")
    parent=db.query(ProductionSpace).filter(ProductionSpace.id==row.parent_id).first() if row.parent_id else None
    children=db.query(ProductionSpace).filter(ProductionSpace.parent_id==row.id).order_by(ProductionSpace.code).all()
    orders=db.query(PlantingOrder).filter(PlantingOrder.production_space_id==row.id).order_by(PlantingOrder.created_at.desc()).all()
    batches=db.query(ProductionBatch).filter(or_(ProductionBatch.production_space_id==row.id,ProductionBatch.greenhouse_space_id==row.id,ProductionBatch.bed_space_id==row.id)).order_by(ProductionBatch.opened_at.desc()).limit(100).all()
    data=_obj(row,["id","parent_id","code","name","type","latitude","longitude","google_maps_url","area","area_unit","capacity","length_m","width_m","status"])
    if row.type=='bed':data.update(latitude=None,longitude=None,google_maps_url=None)
    profiles=db.query(BedCropProfile).filter(BedCropProfile.bed_space_id==row.id).all()
    crops={c.id:c for c in db.query(FlowerCrop).filter(FlowerCrop.id.in_({p.crop_id for p in profiles})).all()}
    data["profiles"]=[_profile_dict(p,crops.get(p.crop_id)) for p in profiles]
    data["parent"]=_obj(parent,["id","code","name","type"]) if parent else None
    data["children"]=[_obj(x,["id","parent_id","code","name","type","area","area_unit","capacity","length_m","width_m","status"]) for x in children]
    data["orders"]=[_obj(x,["id","number","status","planned_start_date","actual_start_date","created_at"]) for x in orders]
    batch_stats=bulk_batch_metrics(db,batches) if batches else {}
    data["batches"]=[{**_obj(x,["id","code","status","side","planting_week","projected_harvest_week"]),**batch_stats.get(x.id,{})} for x in batches]
    data["summary"]={"children":len(children),"orders":len(orders),"open_batches":sum(1 for x in batches if x.status=="open")}
    return data


def _validate_space_location(db: Session, payload: ProductionSpaceIn, own_id: int | None = None):
    if payload.type == 'bed':
        payload.google_maps_url = None; payload.latitude = None; payload.longitude = None
    elif payload.google_maps_url:
        from services.map_location import resolve_map_location
        try: location=resolve_map_location(payload.google_maps_url)
        except ValueError as exc: raise HTTPException(422,str(exc))
        payload.latitude=Decimal(str(location.latitude)); payload.longitude=Decimal(str(location.longitude))
        payload.google_maps_url=location.canonical_url

    if not payload.parent_id:
        if payload.type not in {'farm', 'greenhouse'}:
            raise HTTPException(422, 'Una cama requiere finca o invernadero padre')
        return
    if payload.parent_id == own_id:
        raise HTTPException(422, 'Un espacio no puede contenerse a sí mismo')
    parent = db.get(ProductionSpace, payload.parent_id)
    if not parent or parent.status != 'active':
        raise HTTPException(422, 'El espacio padre no existe o está inactivo')
    if payload.type == 'bed' and parent.type not in {'farm', 'greenhouse'}:
        raise HTTPException(422, 'Las camas pertenecen a una finca o invernadero')
    if payload.type == 'greenhouse' and parent.type != 'farm':
        raise HTTPException(422, 'El invernadero debe pertenecer a una finca')
    if own_id and _belongs_to(db, parent, own_id):
        raise HTTPException(422, 'La jerarquía contiene un ciclo')


@router.post('/production-spaces', status_code=201)
def create_space(payload: ProductionSpaceIn, current=Depends(require_module_permission('floresvolcan:spaces:manage')), db: Session=Depends(get_db)):
    _validate_space_location(db, payload)
    from .space_identity import validate_space_identity
    validate_space_identity(db,payload)
    if db.query(ProductionSpace.id).filter(func.upper(ProductionSpace.code) == payload.code.upper()).first():
        raise HTTPException(409, 'Código de espacio ya existe')
    row=ProductionSpace(**payload.model_dump(exclude={'profiles'}))
    db.add(row); db.flush()
    _save_bed_profiles(db,row,payload.profiles)
    db.commit(); db.refresh(row)
    return _obj(row,['id','parent_id','code','name','type','capacity','status'])


@router.put('/production-spaces/{space_id}')
def update_space(space_id: int, payload: ProductionSpaceIn, current=Depends(require_module_permission('floresvolcan:spaces:manage')), db: Session=Depends(get_db)):
    row=db.get(ProductionSpace,space_id)
    if not row: raise HTTPException(404, 'Espacio no encontrado')
    _validate_space_location(db,payload,space_id)
    from .space_identity import validate_space_identity
    validate_space_identity(db,payload,row)
    if row.type != payload.type and db.query(ProductionBatch.id).filter(ProductionBatch.bed_space_id==space_id).first():
        raise HTTPException(409, 'No cambie el tipo de una cama con trazabilidad histórica')
    if db.query(ProductionSpace.id).filter(func.upper(ProductionSpace.code)==payload.code.upper(),ProductionSpace.id!=space_id).first():
        raise HTTPException(409, 'Código de espacio ya existe')
    for k,v in payload.model_dump(exclude={'profiles'}).items(): setattr(row,k,v)
    db.flush(); _save_bed_profiles(db,row,payload.profiles)
    db.commit(); db.refresh(row)
    return _obj(row,['id','parent_id','code','name','type','capacity','status'])


@router.delete("/production-spaces/{space_id}")
def deactivate_space(space_id:int,current=Depends(require_module_permission("floresvolcan:spaces:manage")),db:Session=Depends(get_db)):
    del current
    row=db.query(ProductionSpace).filter(ProductionSpace.id==space_id).first()
    if not row: raise HTTPException(404,"Espacio no encontrado")
    row.status="inactive"; db.commit(); return {"ok":True,"status":"inactive"}


# ---------- Siembra / requisiciones ----------
@router.get('/planting/flower-options')
def flower_options(current=Depends(require_module_permission('floresvolcan:planting:view')),db:Session=Depends(get_db)):
    return [{'id':c.id,'code':c.code,'name':c.name,'material_type':c.material_type} for c in db.query(FlowerCrop).filter(FlowerCrop.active.is_(True)).order_by(FlowerCrop.name).all()]


@router.get('/planting/beds')
def eligible_beds(space_id:int,crop_id:int,current=Depends(require_module_permission('floresvolcan:planting:view')),db:Session=Depends(get_db)):
    space=db.get(ProductionSpace,space_id)
    if not space or space.status != 'active' or space.type not in {'farm','greenhouse'}:
        raise HTTPException(422,'Seleccione una finca o invernadero activo')
    parent_ids=[space.id]
    if space.type=='farm':
        parent_ids += [r[0] for r in db.query(ProductionSpace.id).filter(ProductionSpace.parent_id==space.id,
                  ProductionSpace.type=='greenhouse', ProductionSpace.status=='active').all()]
    beds=db.query(ProductionSpace,BedCropProfile).join(BedCropProfile,BedCropProfile.bed_space_id==ProductionSpace.id).filter(
        ProductionSpace.parent_id.in_(parent_ids),ProductionSpace.type=='bed',ProductionSpace.status=='active',
        BedCropProfile.crop_id==crop_id,BedCropProfile.active.is_(True)).order_by(ProductionSpace.code).all()
    return [{**_obj(b,['id','code','name','parent_id','length_m','width_m']), 'capacity_units':p.capacity_units,
             'density':p.density,'material_type':p.material_type} for b,p in beds]


@router.get('/planting/approvers')
def planting_approvers(current=Depends(require_module_permission('floresvolcan:planting:manage')), db:Session=Depends(get_db)):
    from .planting_preparation import is_manager
    users=db.query(Usuario).outerjoin(ModuleUserRole,(ModuleUserRole.usuario_id==Usuario.id)&(ModuleUserRole.module_key=='mod_floresvolcan')).filter(Usuario.activo.is_(True),or_(Usuario.is_superadmin.is_(True),func.lower(Usuario.rol)=='gerente',ModuleUserRole.role=='gerente')).order_by(Usuario.nombre).all()
    return [{'id':u.id,'name':u.nombre} for u in users if is_manager(db,u)]


@router.get('/planting/material-options')
def planting_material_options(crop_id:int, q:str|None=None, page:int|None=None, page_size:int=40, selected_id:int|None=None,
                             current=Depends(require_module_permission('floresvolcan:planting:manage')),db:Session=Depends(get_db)):
    from .material_defaults import COLOR_NAMES
    lily=_lily_crop(db)
    variety=func.coalesce(func.nullif(InventoryLot.variety_snapshot,''),SapProduct.variety,SapProduct.description,'')
    color=func.coalesce(func.nullif(InventoryLot.color_snapshot,''),SapProduct.color,'')
    common=func.coalesce(func.nullif(InventoryLot.common_code_snapshot,''),SapProduct.common_code,'')
    query=db.query(func.min(InventoryLot.id).label('id'), InventoryLot.sap_product_id,
                   variety.label('variety'),color.label('color'),common.label('common_code'),
                   func.sum(InventoryLot.quantity_available).label('quantity_available'))        .join(SapProduct).filter(InventoryLot.quantity_available>0,SapProduct.active.is_(True),
                                or_(SapProduct.crop_id==crop_id,SapProduct.crop_id.is_(None) if lily and lily.id==crop_id else False))
    if q:
        term=f'%{q.strip()}%'
        query=query.filter(or_(variety.ilike(term),color.ilike(term),common.ilike(term),SapProduct.code.ilike(term),SapProduct.description.ilike(term),color.in_([code for code,name in COLOR_NAMES.items() if q.strip().casefold() in name.casefold()])))
    query=query.group_by(InventoryLot.sap_product_id,variety,color,common)
    if selected_id is not None: query=query.having(func.min(InventoryLot.id)==selected_id)
    rows,total=_page(query.order_by(variety,color,common,InventoryLot.sap_product_id),page,page_size)
    from .material_defaults import COLOR_NAMES
    return _paged([{**dict(row._mapping),'color_label':COLOR_NAMES.get(row.color,row.color)} for row in rows],total,page,page_size)


@router.get("/planting-orders")
def list_planting_orders(current=Depends(require_module_permission("floresvolcan:planting:view")), db: Session = Depends(get_db), page: int | None = None, page_size: int = 40, q: str | None = None, status: str | None = None, selected_id: int | None = None):
    from .planting_preparation import is_manager
    manager=is_manager(db,current)
    query = db.query(PlantingOrder).outerjoin(ProductionSpace, PlantingOrder.production_space_id == ProductionSpace.id)
    if status: query = query.filter(PlantingOrder.status.in_(status.split(",")))
    if selected_id is not None: query = query.filter(PlantingOrder.id == selected_id)
    if q:
        term = f"%{q.strip()}%"
        crew_ids = db.query(WorkCrewMember.crew_id).join(Worker).filter(Worker.name.ilike(term))
        query = query.filter(or_(PlantingOrder.number.ilike(term), ProductionSpace.name.ilike(term), ProductionSpace.code.ilike(term), PlantingOrder.crew_id.in_(crew_ids)))
    rows, total = _page(query.order_by(PlantingOrder.created_at.desc(), PlantingOrder.id.desc()), page, page_size)
    crews={c.id:c for c in db.query(WorkCrew).options(joinedload(WorkCrew.members).joinedload(WorkCrewMember.worker)).filter(WorkCrew.id.in_([x.crew_id for x in rows])).all()}
    plans={int(p.key.split('.')[-1]):json.loads(p.value_text) for p in db.query(ModuleSetting).filter(ModuleSetting.key.in_([f'planting_plan.{x.id}' for x in rows])).all()}
    result=[]
    for x in rows:
        plan=plans.get(x.id)
        crew=crews.get(x.crew_id)
        result.append({**_obj(x,["id","number","production_space_id","crew_id","status","planned_start_date","actual_start_date","approved_at","comments","created_at"]),
                       "crew_members":[{"worker_id":m.worker_id,"name":m.worker.name} for m in (crew.members if crew else [])],
                       "planned_quantity":x.planned_quantity,"crop_id":x.crop_id,
                       "can_approve":manager and (not plan or current.is_superadmin or plan["approver_user_id"]==current.id)})
    return _paged(result, total, page, page_size)


@router.post("/planting-orders/{order_id}/share-link")
def planting_share_link(order_id:int,current=Depends(require_module_permission("floresvolcan:planting:manage")),db:Session=Depends(get_db)):
    import secrets
    order=db.query(PlantingOrder).filter(PlantingOrder.id==order_id).with_for_update().first()
    if not order: raise HTTPException(404,"Orden de siembra no encontrada")
    if order.status not in {"approved","in_progress"}:
        raise HTTPException(409,"Apruebe la orden antes de compartirla con la cuadrilla")
    order.qr_token=order.qr_token or secrets.token_urlsafe(32)
    db.commit()
    return {"qr_token":order.qr_token}


@router.get("/planting-orders/{order_id}")
def get_planting_order(order_id:int,current=Depends(require_module_permission("floresvolcan:planting:view")),db:Session=Depends(get_db),records_page:int=1):
    if records_page<1: raise HTTPException(422,"Página inválida")
    order=db.query(PlantingOrder).filter(PlantingOrder.id==order_id).first()
    if not order: raise HTTPException(404,"Orden de siembra no encontrada")
    space=db.query(ProductionSpace).filter(ProductionSpace.id==order.production_space_id).first()
    crew=db.query(WorkCrew).options(joinedload(WorkCrew.members).joinedload(WorkCrewMember.worker)).filter(WorkCrew.id==order.crew_id).first()
    reqs=db.query(Requisition).filter(Requisition.planting_order_id==order.id).order_by(Requisition.created_at).all()
    batches=db.query(ProductionBatch).filter(ProductionBatch.planting_order_id==order.id).order_by(ProductionBatch.opened_at).all()
    stats=bulk_batch_metrics(db,batches) if batches else {}
    from .planting_preparation import read_plan,is_manager
    from .public_trace import trace_info
    plan=read_plan(db,order.id)
    from .planting_detail import order_trace
    trace=order_trace(db,order,_order_bed_summary(db,order),records_page)
    return {
        **trace,
        "preparation": plan,
        "trace": trace_info(db,order.id),
        "can_approve":is_manager(db,current) and (not plan or current.is_superadmin or plan["approver_user_id"]==current.id),
        **_obj(order,["id","number","production_space_id","crew_id","requested_by_id","approved_by_id","approved_at","status","planned_start_date","actual_start_date","completed_at","comments","created_at"]),
        "requested_by_name":_user_label(db,order.requested_by_id),"approved_by_name":_user_label(db,order.approved_by_id),
        "crop_id":order.crop_id,"planned_quantity":order.planned_quantity,"qr_token":order.qr_token,
        "requested_bulbs":_decimal(db.query(func.coalesce(func.sum(RequisitionLine.quantity_requested),0)).join(Requisition).filter(
            Requisition.planting_order_id==order.id,Requisition.status!='cancelled').scalar()),
        "space": _obj(space,["id","code","name","type"]) if space else None,
        "crew":{"id":crew.id,"name":crew.name,"members":[{"worker_id":m.worker_id,"name":m.worker.name} for m in crew.members]} if crew else None,
        "requisitions":[{"id":r.id,"number":r.number,"status":r.status,"created_at":r.created_at,"approved_at":r.approved_at} for r in reqs],
        "batches":[{**_obj(b,["id","code","status","bed_space_id","side","planting_week","projected_harvest_week","opened_at","closed_at"]),**stats.get(b.id,{})} for b in batches],
    }


@router.get("/field/planting-orders/{order_id}")
def get_field_planting_order(order_id:int,current:Usuario=Depends(require_module_permission("floresvolcan:field:use")),db:Session=Depends(get_db)):
    _field_worker(db, current)
    order=db.query(PlantingOrder).filter(PlantingOrder.id==order_id).first()
    if not order: raise HTTPException(404,"Orden de siembra no encontrada")
    _assert_crew(db,current,order.crew_id)
    space=db.query(ProductionSpace).filter(ProductionSpace.id==order.production_space_id).first()
    crew=db.query(WorkCrew).options(joinedload(WorkCrew.members).joinedload(WorkCrewMember.worker)).filter(WorkCrew.id==order.crew_id).first()
    batches=db.query(ProductionBatch).filter(ProductionBatch.planting_order_id==order.id).order_by(ProductionBatch.opened_at.desc()).all()
    stats=bulk_batch_metrics(db,batches) if batches else {}
    return {**_obj(order,["id","number","status","planned_start_date","actual_start_date","comments"]),
            "space":_obj(space,["id","code","name","type"]) if space else None,
            "crew_members":[{"worker_id":m.worker_id,"name":m.worker.name} for m in crew.members] if crew else [],
            "batches":[{**_obj(b,["id","code","status","bed_space_id","side","planting_week","projected_harvest_week"]),**stats.get(b.id,{})} for b in batches]}


@router.get('/planting-orders/{order_id}/paper-form')
def planting_order_paper_form(order_id:int,current=Depends(require_module_permission('floresvolcan:planting:view')),db:Session=Depends(get_db)):
    order=db.get(PlantingOrder,order_id)
    if not order or order.status not in {'approved','in_progress','completed'}:
        raise HTTPException(404,'La orden debe estar aprobada para imprimir')
    area=db.get(ProductionSpace,order.production_space_id)
    beds=_order_bed_summary(db,order)
    context={'location':area.name if area else '', 'lot':order.number,
             'variety':(db.get(FlowerCrop,order.crop_id).name if order.crop_id and db.get(FlowerCrop,order.crop_id) else ''),
             'worker':'', 'beds':', '.join(b['code'] for b in beds)}
    lots=db.query(InventoryLot).join(RequisitionLine,RequisitionLine.inventory_lot_id==InventoryLot.id).join(Requisition,Requisition.id==RequisitionLine.requisition_id).filter(Requisition.planting_order_id==order.id).all()
    context['lot']=' / '.join(dict.fromkeys(l.supplier_lot_code or l.lot_code for l in lots)) or order.number
    pdf=build_field_pdf(operation='planting',number=order.number,context=context)
    return StreamingResponse(BytesIO(pdf),media_type='application/pdf',headers={
       'Content-Disposition':f'attachment; filename="FloresVolcan-Siembra-{order.number}.pdf"',
       'Cache-Control':'private, no-store'})


@router.post('/field/validate-planting-order')
def validate_planting_order(payload:dict=Body(...), current:Usuario=Depends(require_module_permission('floresvolcan:field:use')), db:Session=Depends(get_db)):
    order=db.query(PlantingOrder).filter(PlantingOrder.number==str(payload.get('order_number','')).strip()).first()
    if not order or order.status not in {'approved','in_progress'}:
        raise HTTPException(422,'Número de lote u orden no válido')
    supplied_token=str(payload.get('qr_token') or '').strip()
    if supplied_token and not secrets.compare_digest(supplied_token,str(order.qr_token or '')):
        raise HTTPException(403,'El QR no corresponde a esta orden')
    worker=db.query(Worker).filter(Worker.id==payload.get('worker_id'), Worker.employee_code==str(payload.get('employee_code','')).strip(),Worker.active.is_(True)).first()
    if not worker:
        raise HTTPException(422,'Trabajador o código de empleado incorrecto')
    crew_ids=_active_crew_ids(db,worker)
    if order.crew_id not in crew_ids:
        raise HTTPException(403,'El trabajador no pertenece a esta cuadrilla')
    if not current.is_superadmin and worker.user_id!=current.id:
        raise HTTPException(403,'Debe ingresar con la cuenta vinculada a este trabajador')
    return {'ok':True,'order_id':order.id,'number':order.number,'worker':worker.name}


@router.post("/planting-orders", status_code=201)
def create_planting_order(payload: PlantingOrderIn, current: Usuario=Depends(require_module_permission("floresvolcan:planting:manage")), db: Session=Depends(get_db)):
    space=db.query(ProductionSpace).filter(ProductionSpace.id==payload.production_space_id, ProductionSpace.status=="active").first()
    if not space: raise HTTPException(422,"Espacio productivo inválido")
    from .planting_preparation import validate_plan, attach_plan
    crop_id = payload.crop_id or (_lily_crop(db).id if _lily_crop(db) else None)
    plan = validate_plan(db, payload, crop_id, current)
    order_number=_next("OS",db.query(PlantingOrder).count())
    if payload.crew_id:
        crew=db.query(WorkCrew).filter(WorkCrew.id==payload.crew_id,WorkCrew.active.is_(True)).first()
        if not crew or not crew.members:
            raise HTTPException(422,"La cuadrilla seleccionada no existe o está vacía")
    else:
        ids=list(dict.fromkeys(payload.worker_ids))
        if not ids: raise HTTPException(422,"Seleccione una cuadrilla o trabajadores")
        workers=db.query(Worker).filter(Worker.id.in_(ids), Worker.active.is_(True)).all()
        if len(workers)!=len(ids):
            raise HTTPException(422,"Todos los integrantes de la cuadrilla deben existir y estar activos")
        crew=WorkCrew(name=f"Cuadrilla {order_number}")
        crew.members=[WorkCrewMember(worker_id=w.id,valid_from=payload.planned_start_date or datetime.now(LOCAL_TZ).date(),valid_until=payload.planned_start_date or datetime.now(LOCAL_TZ).date()) for w in workers]
        db.add(crew);db.flush()
    from .crew_management import validate_assignment
    if payload.crew_id:validate_assignment(db,crew,"planting",payload.planned_start_date or datetime.now(LOCAL_TZ).date())
    selected = []
    crop_id = payload.crop_id or (_lily_crop(db).id if _lily_crop(db) else None)
    if payload.bed_space_ids:
        if space.type not in {'farm','greenhouse'}:
            raise HTTPException(422, 'Seleccione una finca o invernadero')
        ids = list(dict.fromkeys(payload.bed_space_ids))
        beds = db.query(ProductionSpace).filter(ProductionSpace.id.in_(ids),ProductionSpace.type=='bed',ProductionSpace.status=='active').all()
        if len(beds)!=len(ids): raise HTTPException(422,'Hay camas inactivas o inexistentes')
        profiles={p.bed_space_id:p for p in db.query(BedCropProfile).filter(BedCropProfile.bed_space_id.in_(ids),
            BedCropProfile.crop_id==crop_id,BedCropProfile.active.is_(True)).all()}
        for bed in beds:
            if not _belongs_to(db,bed,space.id) or bed.id not in profiles:
                raise HTTPException(422,'Una cama no pertenece al área o no tiene capacidad configurada para esa flor')
            selected.append((bed,profiles[bed.id]))
    elif payload.crop_id and space.type in {'farm','greenhouse'}:
        raise HTTPException(422, 'Marque al menos una cama para preparar la siembra')
    if plan and not selected:
        raise HTTPException(422,"Seleccione las camas de la preparación")
    desired_quantity=Decimal(plan["total"]) if plan else payload.planned_quantity
    if desired_quantity and selected and desired_quantity>sum(p.capacity_units for _,p in selected):
        raise HTTPException(422,'La cantidad supera la capacidad de las camas elegidas')
    row=PlantingOrder(number=order_number,crop_id=crop_id,production_space_id=payload.production_space_id,
        crew_id=crew.id,requested_by_id=current.id,planned_start_date=payload.planned_start_date,comments=payload.comments,
        planned_quantity=(int(Decimal(plan["total"])) if plan else sum(p.capacity_units for _,p in selected) if selected else payload.planned_quantity))
    db.add(row);db.flush()
    from .public_trace import trace_info
    trace_info(db,row.id,True)
    if selected:
        db.add_all([PlantingOrderBed(planting_order_id=row.id,bed_space_id=bed.id,bed_profile_id=p.id,
                                     capacity_units_snapshot=p.capacity_units) for bed,p in selected])
    if not payload.crew_id:
        crew.planting_order_id=row.id
        from .crew_management import write_config
        write_config(db,crew.id,{'kind':'temporary','activity':'planting','work_date':str(payload.planned_start_date or datetime.now(LOCAL_TZ).date())})
    if plan: attach_plan(db,row,plan,_next("RQ",db.query(Requisition).count()))
    db.commit(); db.refresh(row)
    return _obj(row,["id","number","status","crew_id"])


@router.post("/planting-orders/{order_id}/approve")
def approve_planting_order(order_id:int,current:Usuario=Depends(require_module_permission("floresvolcan:planting:approve")),db:Session=Depends(get_db)):
    from .planting_preparation import read_plan, require_approver, verify_materials
    row=db.query(PlantingOrder).filter(PlantingOrder.id==order_id).populate_existing().with_for_update().first()
    if not row: raise HTTPException(404,"Orden de siembra no encontrada")
    plan=read_plan(db,order_id)
    require_approver(db,current,plan)
    if row.status != "pending_approval": raise HTTPException(409,"La orden ya fue procesada")
    if plan:
        request=db.query(Requisition).filter(Requisition.id==plan['requisition_id']).with_for_update().first()
        if not request or request.status!='pending':raise HTTPException(409,'La solicitud de salida ya fue procesada')
        verify_materials(db,plan,request)
        _reserve_requisition(db,request,current)
        db.flush()
        _deliver_prepared_material(db,request,current)
    row.status="approved"; row.approved_by_id=current.id; row.approved_at=now()
    row.qr_token=row.qr_token or secrets.token_urlsafe(32)
    db.commit(); return {"ok":True,"status":row.status,"requisition_id":plan['requisition_id'] if plan else None}


@router.get("/requisitions")
def list_requisitions(current=Depends(require_module_permission("floresvolcan:requisitions:view")),db:Session=Depends(get_db), page: int | None = None, page_size: int = 40, q: str | None = None, status:str|None=None,exclude_status:str|None=None):
    del current
    query = db.query(Requisition).join(PlantingOrder).options(joinedload(Requisition.lines))
    if status:query=query.filter(Requisition.status.in_(status.split(",")))
    if exclude_status:query=query.filter(~Requisition.status.in_(exclude_status.split(",")))
    query = _search(query, q, Requisition.number, Requisition.comments, PlantingOrder.number)
    rows, total = _page(query.order_by(Requisition.created_at.desc(), Requisition.id.desc()), page, page_size)
    order_numbers = dict(db.query(PlantingOrder.id, PlantingOrder.number).filter(PlantingOrder.id.in_({x.planting_order_id for x in rows}))) if page is not None else {}
    lot_ids = {line.inventory_lot_id for row in rows for line in row.lines}
    product_ids = {line.sap_product_id for row in rows for line in row.lines}
    lots = {lot.id: lot for lot in db.query(InventoryLot).filter(InventoryLot.id.in_(lot_ids)).all()}
    products = {product.id: product for product in db.query(SapProduct).filter(SapProduct.id.in_(product_ids)).all()}
    prepared={int(p.key.split('.')[-1]) for p in db.query(ModuleSetting).filter(ModuleSetting.key.in_([f'planting_plan.{x.planting_order_id}' for x in rows])).all()}
    items = [
        {
            "prepared":x.planting_order_id in prepared,
            **_obj(x,["id","number","planting_order_id","status","comments","created_at","approved_at"]),
            **({"planting_order_number": order_numbers.get(x.planting_order_id)} if page is not None else {}),
            "lines":[{
                **_obj(l,["id","inventory_lot_id","sap_product_id","quantity_requested","quantity_approved","quantity_delivered","quantity_planted","quantity_returned","quantity_field_loss","quantity_released"]),
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
                    "reserved_pending_delivery": max(Decimal("0"), pending_delivery(l)),
                    "pending_to_plant": max(Decimal("0"), pending_to_plant(l)),
                })(lots.get(l.inventory_lot_id), products.get(l.sap_product_id))
            } for l in x.lines]
        }
        for x in rows
    ]
    return _paged(items, total, page, page_size)


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
        out.append({**_obj(line,["id","inventory_lot_id","sap_product_id","quantity_requested","quantity_approved","quantity_delivered","quantity_planted","quantity_returned","quantity_field_loss","quantity_released"]),
                    "lot_code":lot.lot_code if lot else None,"supplier_lot_code":lot.supplier_lot_code if lot else None,
                    "container_number":lot.container_number if lot else None,
                    "units_per_box":lot.units_per_box if lot else None,
                    "product_code":product.code if product else None,"description":product.description if product else None,
                    "variety":(lot.variety_snapshot if lot else None) or (product.variety if product else None),
                    "common_code":(lot.common_code_snapshot if lot else None) or (product.common_code if product else None),
                    "color":(lot.color_snapshot if lot else None) or (product.color if product else None),
                    "caliber":(lot.caliber_snapshot if lot else None) or (product.caliber if product else None),
                    "available":lot.quantity_available if lot else 0,
                    "reserved":max(Decimal("0"),pending_delivery(line)),
                    "pending_to_plant":max(Decimal("0"),pending_to_plant(line)),
                    "plantings":[_obj(x,["id","production_batch_id","bed_space_id","side","quantity_planted","planting_week","planted_at"]) for x in plantings]})
    from .planting_preparation import read_plan
    return {"preparation":read_plan(db,row.planting_order_id), **_obj(row,["id","number","planting_order_id","requested_by_id","approved_by_id","status","comments","created_at","approved_at"]),
            "requested_by_name":_user_label(db,row.requested_by_id),"approved_by_name":_user_label(db,row.approved_by_id),
            "planting_order_number":order.number if order else None,"lines":out}


@router.post("/requisitions", status_code=201)
def create_requisition(payload:RequisitionIn,current:Usuario=Depends(require_module_permission("floresvolcan:requisitions:create")),db:Session=Depends(get_db)):
    order=db.query(PlantingOrder).filter(PlantingOrder.id==payload.planting_order_id).populate_existing().with_for_update().first()
    from .planting_preparation import read_plan
    if order and read_plan(db,order.id):raise HTTPException(409,"Esta siembra ya tiene una salida definida en su preparación; consulte la solicitud vinculada")
    if not order or order.status not in {"approved","in_progress"}:
        raise HTTPException(409,"La orden de siembra debe estar aprobada")
    if order.planned_quantity is not None:
        already=_decimal(db.query(func.coalesce(func.sum(RequisitionLine.quantity_requested),0)).join(Requisition).filter(
            Requisition.planting_order_id==order.id,Requisition.status!='cancelled').scalar())
        requested=sum((item.quantity_requested for item in payload.lines),Decimal('0'))
        if already+requested>order.planned_quantity:
            raise HTTPException(409,f'La salida excede el cupo de las camas: disponible {max(0,order.planned_quantity-already)} bulbos')
    row=Requisition(number=_next("RQ",db.query(Requisition).count()),planting_order_id=order.id,requested_by_id=current.id,comments=payload.comments)
    for item in payload.lines:
        lot=db.query(InventoryLot).filter(InventoryLot.id==item.inventory_lot_id).first()
        if not lot:
            raise HTTPException(422,"Lote de inventario inexistente")
        if order.planned_quantity is not None:
            product=db.get(SapProduct,lot.sap_product_id)
            if product and product.crop_id and product.crop_id!=order.crop_id:
                raise HTTPException(422,'El lote de inventario no corresponde al tipo de flor de la siembra')
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


def _reserve_requisition(db, row, current):
    lines=db.query(RequisitionLine).filter(RequisitionLine.requisition_id==row.id).order_by(RequisitionLine.inventory_lot_id,RequisitionLine.id).all()
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


def _deliver_prepared_material(db, request, current, comments=None):
    """Salida automática de una preparación aprobada, dentro de su transacción."""
    lines=db.query(RequisitionLine).filter_by(requisition_id=request.id).order_by(RequisitionLine.inventory_lot_id).all()
    for line in lines:
        qty=max(Decimal('0'),pending_delivery(line))
        if not qty:continue
        lot=db.query(InventoryLot).filter_by(id=line.inventory_lot_id).with_for_update().first()
        if not lot or _decimal(lot.quantity_reserved)<qty:
            raise HTTPException(409,'El saldo reservado no permite completar la salida de la preparación')
        issued_at=now()
        delivery=RequisitionDelivery(number=_next('SAL',db.query(RequisitionDelivery).count()),
            requisition_line_id=line.id,inventory_lot_id=lot.id,quantity=qty,
            units_per_box=lot.units_per_box,delivery_week=_iso_week(issued_at),delivered_at=issued_at,
            delivered_by_id=current.id,comments=comments or 'Salida automática por aprobación de la preparación de siembra')
        db.add(delivery);db.flush()
        lot.quantity_reserved=_decimal(lot.quantity_reserved)-qty
        lot.quantity_issued=_decimal(lot.quantity_issued)+qty
        line.quantity_delivered=_decimal(line.quantity_delivered)+qty
        db.add(InventoryMovement(inventory_lot_id=lot.id,movement_type='ISSUE_TO_FIELD',quantity=qty,
            reference_type='requisition_delivery',reference_id=delivery.id,user_id=current.id,
            comments=delivery.comments))
    request.status='delivered'
    db.flush()


@router.post("/requisitions/{requisition_id}/approve")
def approve_requisition(requisition_id:int,current:Usuario=Depends(require_module_permission("floresvolcan:requisitions:approve")),db:Session=Depends(get_db)):
    # PostgreSQL no permite FOR UPDATE sobre el lado nullable de ciertos LEFT JOIN.
    # Bloqueamos primero la cabecera y luego cada lote por consultas independientes.
    row=db.query(Requisition).filter(Requisition.id==requisition_id).with_for_update().first()
    if not row:
        raise HTTPException(404,"Requisición no encontrada")
    if row.status!="pending":
        raise HTTPException(409,"La requisición ya fue procesada")
    order=db.query(PlantingOrder).filter(PlantingOrder.id==row.planting_order_id).first()
    if not order or order.status not in {"approved","in_progress"}:
        raise HTTPException(409,"La orden de siembra ya no admite material")
    from .planting_preparation import read_plan, require_approver
    require_approver(db,current,read_plan(db,row.planting_order_id))
    _reserve_requisition(db,row,current)
    db.commit()
    return {"ok":True,"status":row.status}


# v0.4: no existe una operación separada de "Salida de bulbo".
# La requisición aprobada reserva el inventario y la siembra consume directamente ese saldo.
# fv_inventory_issues se conserva como tabla histórica para instalaciones actualizadas desde v0.3.


# ---------- Captura de campo ----------
@router.get("/field/tasks")
def field_tasks(current:Usuario=Depends(require_module_permission("floresvolcan:field:use")),db:Session=Depends(get_db),include_waiting_material:bool=False):
    worker=_field_worker(db,current)
    crew_ids=_active_crew_ids(db, worker)
    orders=[]
    if crew_ids:
        candidates=db.query(PlantingOrder).filter(PlantingOrder.crew_id.in_(crew_ids),PlantingOrder.status.in_(["approved","in_progress"])).order_by(PlantingOrder.created_at).all()
        for order in candidates:
            remaining_condition=RequisitionLine.quantity_delivered>(RequisitionLine.quantity_planted+RequisitionLine.quantity_returned+RequisitionLine.quantity_field_loss)
            if include_waiting_material:
                remaining_condition=or_(remaining_condition,RequisitionLine.quantity_approved>(RequisitionLine.quantity_delivered+RequisitionLine.quantity_released))
            eligible=(db.query(RequisitionLine,Requisition,InventoryLot,SapProduct)
                .join(Requisition,Requisition.id==RequisitionLine.requisition_id)
                .join(InventoryLot,InventoryLot.id==RequisitionLine.inventory_lot_id)
                .join(SapProduct,SapProduct.id==RequisitionLine.sap_product_id)
                .filter(Requisition.planting_order_id==order.id,Requisition.status.in_(["approved","partially_delivered","delivered"]),remaining_condition)
                .order_by(Requisition.approved_at,RequisitionLine.id).all())
            if not eligible: continue
            materials=[]
            for line,req,lot,product in eligible:
                materials.append({
                    "requisition_id":req.id,"requisition_number":req.number,"requisition_line_id":line.id,
                    "product_code":product.code,"description":product.description,
                    "variety":lot.variety_snapshot or product.variety,"common_code":lot.common_code_snapshot or product.common_code,
                    "color":lot.color_snapshot or product.color,"caliber":lot.caliber_snapshot or product.caliber,
                    "supplier":source_supplier(db,lot),"lot_code":lot.lot_code,"supplier_lot_code":lot.supplier_lot_code,"container_number":lot.container_number,
                    "pending_to_plant":max(Decimal("0"),pending_to_plant(line)),
                    "pending_delivery":max(Decimal("0"),pending_delivery(line)),
                    "delivered":line.quantity_delivered,"planted":line.quantity_planted,
                    "default_density":product.default_density,"default_cycle_weeks":product.default_cycle_weeks,
                })
            space=db.query(ProductionSpace).filter(ProductionSpace.id==order.production_space_id).first()
            bed_options=[]
            planned_ids=[r[0] for r in db.query(PlantingOrderBed.bed_space_id).filter(
                PlantingOrderBed.planting_order_id==order.id).all()]
            if planned_ids:
                bed_options=db.query(ProductionSpace).filter(ProductionSpace.id.in_(planned_ids)).order_by(ProductionSpace.code).all()
            elif space:
                if space.type in {"bed","row","table"}:
                    bed_options=[space]
                else:
                    for bed in db.query(ProductionSpace).filter(ProductionSpace.type.in_(["bed","row","table"]),ProductionSpace.status=="active").order_by(ProductionSpace.code).all():
                        if _belongs_to(db,bed,space.id): bed_options.append(bed)
            bed_availability=bed_planting_availability(db,bed_options)
            first=materials[0]
            orders.append({**_obj(order,["id","number","production_space_id","crew_id","status","planned_start_date"]),
                "space":_obj(space,["id","code","name","type"]) if space else None,
                "default_bed_id":space.id if space and space.type in {"bed","row","table"} else None,
                "bed_options":[{**_obj(x,["id","code","name","parent_id","capacity"]),**bed_availability[x.id]} for x in bed_options],
                "requisition_id":first["requisition_id"],"requisition_number":first["requisition_number"],
                "requisition_line_id":first["requisition_line_id"],"material":first,"materials":materials})
    harvest_orders=db.query(HarvestOrder).filter(
        HarvestOrder.crew_id.in_(crew_ids),
        HarvestOrder.status.in_(["approved","in_progress"])
    ).order_by(HarvestOrder.planned_date,HarvestOrder.id).all() if crew_ids else []
    batches=[]
    active_harvest_orders=[]
    for ho in harvest_orders:
        x=db.query(ProductionBatch).filter(ProductionBatch.id==ho.production_batch_id,ProductionBatch.status=="open").first()
        if not x: continue
        lot=db.query(InventoryLot).filter(InventoryLot.id==x.inventory_lot_id).first() if x.inventory_lot_id else None
        product=db.query(SapProduct).filter(SapProduct.id==x.sap_product_id).first()
        bed=db.query(ProductionSpace).filter(ProductionSpace.id==x.bed_space_id).first() if x.bed_space_id else None
        greenhouse=db.query(ProductionSpace).filter(ProductionSpace.id==x.greenhouse_space_id).first() if x.greenhouse_space_id else None
        metrics=_batch_metrics(db,x)
        eta=_harvest_eta(db,x)
        segments=db.query(PlantingRecord).filter(PlantingRecord.production_batch_id==x.id).order_by(PlantingRecord.planted_at).all()
        entry={**_obj(x,["id","code","planting_order_id","sap_product_id","inventory_lot_id","production_space_id","greenhouse_space_id","bed_space_id","side","planting_week","projected_harvest_week","cycle_weeks","status"]),
            **metrics,"product_code":product.code if product else None,"description":product.description if product else None,
            "variety":(lot.variety_snapshot if lot else None) or (product.variety if product else None),
            "caliber":(lot.caliber_snapshot if lot else None) or (product.caliber if product else None),
            "supplier":source_supplier(db,lot),"supplier_lot_code":lot.supplier_lot_code if lot else None,"container_number":lot.container_number if lot else None,
            "bed_code":bed.code if bed else None,"bed_name":bed.name if bed else None,"greenhouse_name":greenhouse.name if greenhouse else None,
            "harvest_order_id":ho.id,"harvest_order_number":ho.number,"harvest_due_date":eta,
            "expected_stems_per_bulb":x.expected_stems_per_bulb,"allow_overage":ho.allow_overage,
            "segments":batch_segments(db,x)}
        batches.append(entry)
        active_harvest_orders.append(_harvest_order_dict(db,ho,x))
    crop=_lily_crop(db)
    discard_reasons=[x.label for x in db.query(CropCatalogValue).filter(CropCatalogValue.crop_id==crop.id,CropCatalogValue.category=="discard_reason",CropCatalogValue.active.is_(True)).order_by(CropCatalogValue.sort_order,CropCatalogValue.label).all()] if crop else []
    return {"planting_orders":orders,"open_batches":batches,"harvest_orders":active_harvest_orders,"discard_reasons":discard_reasons}


@router.post("/field/planting", status_code=201)
def register_planting(payload:PlantingRecordIn,current:Usuario=Depends(require_module_permission("floresvolcan:planting:register")),db:Session=Depends(get_db)):
    _field_worker(db, current)
    existing=check_retry(db,PlantingRecord,payload,current)
    if existing:
        return {"id":existing.id,"batch_id":existing.production_batch_id,"quantity":existing.quantity_planted,"idempotent":True}
    order=db.query(PlantingOrder).filter(PlantingOrder.id==payload.planting_order_id).populate_existing().with_for_update().first()
    line=None
    if payload.requisition_line_id:
        line=db.query(RequisitionLine).filter(RequisitionLine.id==payload.requisition_line_id).first()
    bed=lock_bed(db,payload.bed_space_id)
    existing=check_retry(db,PlantingRecord,payload,current)
    if existing:
        return {"id":existing.id,"batch_id":existing.production_batch_id,"quantity":existing.quantity_planted,"idempotent":True}
    if not order or order.status not in {"approved","in_progress"}:
        raise HTTPException(409,"Orden de siembra no disponible")
    _assert_crew(db, current, order.crew_id)
    if not line or line.requisition.planting_order_id!=order.id:
        raise HTTPException(422,"Línea de requisición no corresponde a la orden")
    if line.requisition.status not in {"approved","partially_delivered","delivered"}:
        raise HTTPException(409,"La orden requiere una requisición aprobada y material entregado antes de sembrarse")
    if bed.type not in {"bed","row","table"} or bed.status!="active":
        raise HTTPException(422,"Cama inválida")
    if not _belongs_to(db,bed,order.production_space_id):
        raise HTTPException(422,"La cama no pertenece al espacio productivo de la orden")
    capture=_assert_paper_capture(db,payload.paper_capture_id,"planting",order.id,current)
    qty=_decimal(payload.quantity_planted)
    db.refresh(line, with_for_update=True)
    committed_pending=pending_to_plant(line)
    if qty>committed_pending:
        raise HTTPException(409,"La cantidad supera el material entregado de la requisición pendiente de sembrar")
    lot=db.query(InventoryLot).filter(InventoryLot.id==line.inventory_lot_id).with_for_update().first()
    if not lot: raise HTTPException(409,"El lote de inventario ya no existe")
    committed_inventory=_decimal(lot.quantity_issued)
    if committed_inventory<qty:
        raise HTTPException(409,"El material entregado a campo ya no alcanza")
    product=db.query(SapProduct).filter(SapProduct.id==line.sap_product_id).first()
    density=_decimal(product.default_density if product else None)
    cycle_weeks=int((product.default_cycle_weeks if product else 0) or 0)
    if density<=0 or cycle_weeks<=0:
        raise HTTPException(422,"Configure densidad y ciclo en Materiales (SAP) antes de registrar la siembra")
    if (payload.density is not None and _decimal(payload.density)!=density) or (payload.cycle_weeks is not None and payload.cycle_weeks!=cycle_weeks):
        raise HTTPException(422,"La densidad y el ciclo deben coincidir con la configuración del material")
    from .field_access import validate_record_worker
    record_worker=validate_record_worker(db,current,order.crew_id,payload.worker_code,payload.paper_capture_id)
    assigned=None
    selected_beds=db.query(PlantingOrderBed).filter(PlantingOrderBed.planting_order_id==order.id).all()
    if selected_beds:
        selected_ids={x.bed_space_id for x in selected_beds}
        if bed.id not in selected_ids:
            raise HTTPException(422,'La cama no fue seleccionada en la preparación de siembra')
        assigned=next(x for x in selected_beds if x.bed_space_id==bed.id)
        # Bloqueo ya obtenido en lock_bed; capacidad por material sobre cupo planificado.
        total_in_bed=_decimal(db.query(func.coalesce(func.sum(PlantingRecord.quantity_planted),0)).filter(
            PlantingRecord.planting_order_id==order.id,PlantingRecord.bed_space_id==bed.id).scalar())
        if total_in_bed+qty>assigned.capacity_units_snapshot:
            raise HTTPException(409,'Se excede el cupo de bulbos de esta cama en la orden')
    planted_at=operation_time(payload.planted_at)
    cycle=planting_cycle(db,bed)
    if db.query(BedCycle.id).filter(BedCycle.bed_space_id==bed.id,BedCycle.status=="cleaned",BedCycle.cleaned_at>planted_at).first():
        raise HTTPException(409,"La fecha pertenece a una ocupación anterior de la mesa")
    planting_week=_iso_week(planted_at)
    projected_week=_projected_week(planted_at,cycle_weeks)
    area_m2=qty/density
    if assigned:
        prior=db.query(PlantingRecord).join(ProductionBatch,ProductionBatch.id==PlantingRecord.production_batch_id).filter(ProductionBatch.bed_cycle_id==cycle.id).all()
        used_fraction=sum((_decimal(rec.quantity_planted)/Decimal(rec.bed_capacity_units_snapshot) if rec.bed_capacity_units_snapshot else _decimal(rec.area_m2)/_decimal(bed.capacity) if bed.capacity else Decimal('0') for rec in prior),Decimal('0'))
        if used_fraction+qty/Decimal(assigned.capacity_units_snapshot)>1:
            raise HTTPException(409,'La suma de las siembras del ciclo supera el cupo configurado de la cama')
    used_area=occupied_area(db,cycle)
    if bed.capacity is not None and used_area+area_m2>_decimal(bed.capacity):
        raise HTTPException(409,f"La siembra requiere {area_m2:.2f} m² y supera la capacidad restante de la cama")
    greenhouse=None
    if payload.greenhouse_space_id:
        greenhouse=db.query(ProductionSpace).filter(ProductionSpace.id==payload.greenhouse_space_id,ProductionSpace.type=="greenhouse").first()
        if not greenhouse or not _belongs_to(db,bed,greenhouse.id):
            raise HTTPException(422,"El invernadero no corresponde a la cama")
    greenhouse=greenhouse or _ancestor_of_type(db,bed,"greenhouse")
    side=(payload.side or "").upper() or None
    batch=None
    if payload.production_batch_id:
        batch=db.query(ProductionBatch).filter(ProductionBatch.id==payload.production_batch_id,ProductionBatch.status=="open").first()
        if not batch or (batch.inventory_lot_id,batch.bed_space_id,batch.planting_order_id,batch.side,batch.planting_week,batch.cycle_weeks,batch.bed_cycle_id,_decimal(batch.expected_stems_per_bulb))!=(lot.id,bed.id,order.id,side,planting_week,cycle_weeks,cycle.id,_decimal(product.expected_stems_per_bulb or 1)):
            raise HTTPException(422,"El lote productivo no corresponde a la orden, origen, cama, lado, semana y ciclo de esta siembra")
    if not batch:
        batch=db.query(ProductionBatch).filter(
            ProductionBatch.planting_order_id==order.id,
            ProductionBatch.inventory_lot_id==lot.id,
            ProductionBatch.bed_space_id==bed.id,
            ProductionBatch.side==side,
            ProductionBatch.planting_week==planting_week,
            ProductionBatch.cycle_weeks==cycle_weeks,
            ProductionBatch.expected_stems_per_bulb==_decimal(product.expected_stems_per_bulb or 1),
            ProductionBatch.bed_cycle_id==cycle.id,
            ProductionBatch.status=="open",
        ).first()
    if not batch:
        batch=ProductionBatch(
            bed_cycle_id=cycle.id,
            code=_next("FV",db.query(ProductionBatch).count()),crop_id=order.crop_id,planting_order_id=order.id,sap_product_id=line.sap_product_id,
            inventory_lot_id=lot.id,production_space_id=order.production_space_id,
            greenhouse_space_id=greenhouse.id if greenhouse else None,bed_space_id=bed.id,side=side,
            planting_week=planting_week,projected_harvest_week=projected_week,cycle_weeks=cycle_weeks,
            expected_stems_per_bulb=_decimal(product.expected_stems_per_bulb if product else 1) or Decimal("1"),
        )
        db.add(batch); db.flush()
    rec=PlantingRecord(
        bed_capacity_units_snapshot=assigned.capacity_units_snapshot if assigned else None,
        worker_id=record_worker.id if record_worker else None,
        request_hash=request_hash(payload),
        client_token=payload.client_token,production_batch_id=batch.id,planting_order_id=order.id,requisition_line_id=line.id,
        inventory_lot_id=lot.id,greenhouse_space_id=greenhouse.id if greenhouse else None,bed_space_id=bed.id,side=side,
        table_space_id=bed.id,row_space_id=bed.id,
        quantity_planted=qty,density=density,area_m2=area_m2,cycle_weeks=cycle_weeks,planting_week=planting_week,
        projected_harvest_week=projected_week,sealed_at=payload.sealed_at,crew_id=order.crew_id,
        operator_user_id=capture.created_by_id if capture else current.id,planted_at=planted_at,notes=payload.notes,
    )
    db.add(rec); db.flush()
    lot.quantity_issued=_decimal(lot.quantity_issued)-qty
    lot.quantity_consumed=_decimal(lot.quantity_consumed)+qty
    line.quantity_planted=_decimal(line.quantity_planted)+qty
    db.add(InventoryMovement(inventory_lot_id=lot.id,movement_type="PLANTING_CONSUMPTION",quantity=qty,reference_type="planting_record",reference_id=rec.id,user_id=current.id))
    order.status="in_progress"; order.actual_start_date=order.actual_start_date or planted_at.date()
    if capture:
        capture.status="digitized"; capture.linked_record_id=rec.id; capture.reviewed_by_id=current.id; capture.reviewed_at=now()
    db.commit(); db.refresh(rec)
    return {
        "id":rec.id,"batch_id":batch.id,"batch_code":batch.code,"quantity":rec.quantity_planted,
        "density":rec.density,"area_m2":rec.area_m2,"planting_week":rec.planting_week,
        "projected_harvest_week":rec.projected_harvest_week,"bed_used_m2":used_area+area_m2,
        "bed_remaining_m2":max(Decimal("0"),_decimal(bed.capacity)-(used_area+area_m2)) if bed.capacity is not None else None,
    }


@router.post("/field/harvest", status_code=201)
def register_harvest(payload:HarvestRecordIn,current:Usuario=Depends(require_module_permission("floresvolcan:harvest:register")),db:Session=Depends(get_db)):
    _field_worker(db,current)
    existing=check_retry(db,HarvestRecord,payload,current)
    if existing:
        return {**_obj(existing,["id","production_batch_id","quantity_harvested","total_output","actual_harvest_week","harvested_at"]),"idempotent":True}
    batch=lock_batch(db,payload.production_batch_id)
    existing=check_retry(db,HarvestRecord,payload,current)
    if existing:
        return {"id":existing.id,"production_batch_id":existing.production_batch_id,"total_output":existing.total_output,"idempotent":True}
    if not payload.harvest_order_id:
        raise HTTPException(422,"Seleccione una orden de cosecha autorizada")
    ho=db.query(HarvestOrder).filter(HarvestOrder.id==payload.harvest_order_id).populate_existing().with_for_update().first()
    if not ho or ho.status not in {"approved","in_progress"}:
        raise HTTPException(409,"La orden de cosecha no existe o no está autorizada")
    if ho.production_batch_id!=payload.production_batch_id:
        raise HTTPException(422,"El lote no corresponde a la orden de cosecha")
    _assert_crew(db,current,ho.crew_id)
    harvested_at=operation_time(payload.harvested_at)
    if local_date(harvested_at)<ho.planned_date and not (ho.early_release_reason or '').strip():
        raise HTTPException(409,'La fecha real es anterior a la fecha autorizada de cosecha')
    if not batch or batch.status!="open":
        raise HTTPException(409,"Lote productivo cerrado o inexistente")
    sale=_decimal(payload.bloom_1_qty)+_decimal(payload.bloom_2_qty)+_decimal(payload.bloom_3_5_qty)
    discarded=_decimal(payload.discard_qty)
    total=sale+discarded
    if total<=0: raise HTTPException(422,"Registre al menos una flor cortada o descartada")
    if discarded>0 and not (payload.discard_reason or "").strip():
        raise HTTPException(422,"Indique el motivo del descarte")
    segments=db.query(PlantingRecord).filter(PlantingRecord.production_batch_id==batch.id).all()
    if not segments: raise HTTPException(409,"Este lote aún no tiene siembras reales")
    if payload.planting_record_id:
        segment=next((x for x in segments if x.id==payload.planting_record_id),None)
        if not segment: raise HTTPException(422,"El segmento de siembra no pertenece al lote")
    elif len(segments)==1: segment=segments[0]
    else: raise HTTPException(422,"Seleccione el segmento exacto de siembra: la cama contiene varios registros")
    require_linked_history(db,batch)
    if local_date(harvested_at)<local_date(segment.planted_at):
        raise HTTPException(422,"La corta no puede ser anterior a la siembra")
    eta=local_date(segment.planted_at)+timedelta(weeks=int(segment.cycle_weeks or batch.cycle_weeks or 0))
    if local_date(harvested_at)<eta and not (ho.early_release_reason or '').strip():
        raise HTTPException(409,"Este segmento aún no cumple su ciclo; requiere autorización de cosecha anticipada")
    factor=_decimal(batch.expected_stems_per_bulb or 1)
    if factor<=0: raise HTTPException(409,"Configure el factor tallos/bulbo de este lote")
    accumulated=_decimal(db.query(func.coalesce(func.sum(HarvestRecord.total_output),0))
        .filter(HarvestRecord.planting_record_id==segment.id,HarvestRecord.voided_at.is_(None)).scalar())
    cap=(_decimal(segment.quantity_planted)*factor).quantize(Decimal("0.001"))
    segment_excess=max(Decimal("0"),accumulated+total-cap)-max(Decimal("0"),accumulated-cap)
    if segment_excess>0:
        authorized=authorized_overage(db,batch.id)
        used=_batch_metrics(db,batch)["overage"]
        if not ho.allow_overage or not (ho.overage_reason or "").strip() or used+segment_excess>authorized:
            raise HTTPException(409,f"La corta supera el saldo técnico del segmento; excedente autorizado disponible: {max(Decimal('0'),authorized-used)} tallos")
    capture=_assert_paper_capture(db,payload.paper_capture_id,"harvest",ho.id,current)
    from .field_access import validate_record_worker
    record_worker=validate_record_worker(db,current,ho.crew_id,payload.worker_code,payload.paper_capture_id)
    rec=HarvestRecord(
        worker_id=record_worker.id if record_worker else None,
        request_hash=request_hash(payload),
        client_token=payload.client_token,production_batch_id=batch.id,harvest_order_id=ho.id,planting_record_id=segment.id,
        bed_space_id=batch.bed_space_id,table_space_id=batch.bed_space_id,row_space_id=batch.bed_space_id,
        crew_id=ho.crew_id,operator_user_id=capture.created_by_id if capture else current.id,
        bloom_1_qty=payload.bloom_1_qty,bloom_2_qty=payload.bloom_2_qty,bloom_3_5_qty=payload.bloom_3_5_qty,
        discard_qty=discarded,discard_reason=(payload.discard_reason or "").strip() or None,
        quantity_harvested=sale,total_output=total,actual_harvest_week=_iso_week(harvested_at),
        harvested_at=harvested_at,notes=payload.notes,
    )
    db.add(rec); db.flush()
    ho.status="in_progress"
    cycle=db.query(BedCycle).filter(BedCycle.id==batch.bed_cycle_id).first()
    if cycle: cycle.status="harvesting"
    if capture:
        capture.status="digitized";capture.linked_record_id=rec.id;capture.reviewed_by_id=current.id;capture.reviewed_at=now()
    db.commit();db.refresh(rec)
    return _obj(rec,["id","harvest_order_id","production_batch_id","planting_record_id","bloom_1_qty","bloom_2_qty","bloom_3_5_qty","discard_qty","discard_reason","quantity_harvested","total_output","actual_harvest_week","harvested_at"])


def _batch_metrics(db: Session, batch: ProductionBatch) -> dict:
    return batch_metrics(db,batch)


@router.get("/production-batches")
def list_batches(current=Depends(require_module_permission("floresvolcan:planting:view")),db:Session=Depends(get_db)):
    del current
    rows=db.query(ProductionBatch).order_by(ProductionBatch.opened_at.desc()).all()
    result=[]
    metrics = bulk_batch_metrics(db, rows)
    lots = {r.id:r for r in db.query(InventoryLot).filter(InventoryLot.id.in_([b.inventory_lot_id for b in rows if b.inventory_lot_id])).all()}
    products = {r.id:r for r in db.query(SapProduct).filter(SapProduct.id.in_([b.sap_product_id for b in rows])).all()}
    beds = {r.id:r for r in db.query(ProductionSpace).filter(ProductionSpace.id.in_([b.bed_space_id for b in rows if b.bed_space_id])).all()}
    for batch in rows:
        lot=lots.get(batch.inventory_lot_id)
        product=products.get(batch.sap_product_id)
        bed=beds.get(batch.bed_space_id)
        result.append({
            **_obj(batch,["id","code","planting_order_id","sap_product_id","inventory_lot_id","production_space_id","greenhouse_space_id","bed_space_id","side","planting_week","projected_harvest_week","cycle_weeks","status","opened_at","closed_at"]),
            **metrics[batch.id],
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
    segments=batch_segments(db,batch)
    from .harvest_review import read_review
    review=read_review(db,batch.id)
    segment_ids={s["id"] for s in segments}
    lot=db.query(InventoryLot).filter(InventoryLot.id==batch.inventory_lot_id).first()
    bed=db.query(ProductionSpace).filter(ProductionSpace.id==batch.bed_space_id).first()
    return {
        "variety":lot.variety_snapshot if lot else None,"source_lot":lot.supplier_lot_code if lot else None,
        "bed_code":bed.code if bed else None,"supplier":source_supplier(db,lot),
        **_obj(batch,["id","code","status","planting_week","projected_harvest_week","side","bed_space_id","inventory_lot_id"]),
        **_batch_metrics(db,batch),
        "segments":segments,"closure_review":review,
        "bed_cycle_id":batch.bed_cycle_id,
        "cuts":[{**_obj(x,["id","planting_record_id","actual_harvest_week","harvested_at","bloom_1_qty","bloom_2_qty","bloom_3_5_qty","discard_qty","discard_reason","quantity_harvested","total_output","notes","voided_at","void_reason"]),"link_valid":x.planting_record_id in segment_ids} for x in records],
    }


@router.post("/production-batches/{batch_id}/technical-close", status_code=201)
def technical_close(batch_id:int,payload:TechnicalClosureIn,current:Usuario=Depends(require_module_permission("floresvolcan:technical_close:execute")),db:Session=Depends(get_db)):
    batch=lock_batch(db,batch_id)
    metrics=_batch_metrics(db,batch)
    if batch.status=="closed" and not metrics["unreconciled_segments"]:
        raise HTTPException(409,"El lote ya está conciliado y cerrado")
    unfinished=db.query(HarvestOrder.id).filter(HarvestOrder.production_batch_id==batch.id,HarvestOrder.status.in_(["pending_approval","approved","in_progress"])).first()
    if unfinished:
        raise HTTPException(409,"Complete las órdenes de cosecha pendientes antes de cerrar el lote")
    require_linked_history(db,batch)
    segments=batch_segments(db,batch)
    if not segments:
        raise HTTPException(409,"No hay siembras reales para conciliar")
    supplied={s.planting_record_id:s for s in payload.segments}
    if len(supplied)!=len(payload.segments) or set(supplied)-{s["id"] for s in segments}:
        raise HTTPException(422,"Los segmentos de conciliación son repetidos o no pertenecen al lote")
    authorized=authorized_overage(db,batch.id)
    if metrics["overage"]>authorized:
        raise HTTPException(409,"El excedente histórico supera la cantidad autorizada; revise sus cortas antes de cerrar")
    losses=Decimal("0")
    for s in segments:
        entry=supplied.get(s["id"])
        loss=_decimal(entry.loss_quantity if entry else 0)
        if loss!=s["remaining"]:
            raise HTTPException(409,f"Segmento #{s['id']}: faltan justificar {s['remaining']} tallos de potencial; cierre sin diferencia pendiente")
        if loss and (not entry.loss_reason or len(entry.loss_reason.strip())<3):
            raise HTTPException(422,f"Indique la causa de la merma del segmento #{s['id']}")
        losses+=loss
    from .harvest_review import closing_gate
    pending_review=closing_gate(db,batch,current,payload,metrics,segments)
    if pending_review:return pending_review
    closure=db.query(TechnicalClosure).filter(TechnicalClosure.production_batch_id==batch.id).first()
    if closure:
        if not closure.previous_summary_json:
            closure.previous_summary_json=json.dumps(_obj(closure,["total_planted","total_harvested","difference","comments","closed_by_id","closed_at"]),default=str)
    else:
        closure=TechnicalClosure(production_batch_id=batch.id)
        db.add(closure)
    closure.total_planted=metrics["planted"];closure.total_expected=metrics["expected_stems"]
    closure.total_harvested=metrics["harvested"];closure.total_sale=metrics["sale"]
    closure.total_discarded=metrics["discarded"];closure.total_loss=losses
    closure.difference=metrics["expected_stems"]-metrics["harvested"]-losses+metrics["overage"]
    closure.yield_percentage=metrics["yield_percentage"];closure.sale_yield_percentage=metrics["sale_yield_percentage"]
    closure.closed_by_id=current.id;closure.closed_at=now();closure.comments=payload.comments.strip()
    db.flush()
    for s in segments:
        entry=supplied.get(s["id"])
        line=db.query(SegmentReconciliation).filter(SegmentReconciliation.planting_record_id==s["id"]).first()
        if line:
            raise HTTPException(409,"El segmento ya tiene conciliación registrada")
        db.add(SegmentReconciliation(planting_record_id=s["id"],technical_closure_id=closure.id,
            expected_quantity=s["expected_stems"],output_quantity=s["harvested"],
            loss_quantity=_decimal(entry.loss_quantity if entry else 0),overage_quantity=s["overage"],
            loss_reason=entry.loss_reason.strip() if entry and entry.loss_reason else None))
    batch.status="closed";batch.closed_at=now()
    db.flush()
    cycle=db.query(BedCycle).filter(BedCycle.id==batch.bed_cycle_id).first()
    if cycle:
        others=db.query(ProductionBatch.id).filter(ProductionBatch.bed_cycle_id==cycle.id,ProductionBatch.status!="closed").first()
        cycle.status="reconciliation" if others else "pending_cleaning"
    from .harvest_review import read_review,save_review
    reviewed=read_review(db,batch.id)
    if reviewed:reviewed.update(status='closed',closed_by=current.id);save_review(db,batch.id,reviewed)
    db.commit();db.refresh(closure)
    return _obj(closure,["id","production_batch_id","total_planted","total_expected","total_harvested","total_sale","total_discarded","total_loss","difference","yield_percentage","sale_yield_percentage","closed_at","comments"])


@router.post('/production-batches/{batch_id}/closure-review/{decision}')
def decide_closure_review(batch_id:int,decision:str,payload:dict=Body(...),current=Depends(require_module_permission('floresvolcan:technical_close:execute')),db:Session=Depends(get_db)):
    if not current.is_superadmin and role_for_module(db,current,'mod_floresvolcan')!='gerente':raise HTTPException(403,'Solo gerencia puede validar diferencias de cierre')
    batch=lock_batch(db,batch_id)
    if not batch:raise HTTPException(404,'Lote no encontrado')
    from .harvest_review import read_review,save_review,fingerprint
    review=read_review(db,batch_id)
    if not review or review['status']!='pending':raise HTTPException(409,'No hay cierre pendiente de gerencia')
    comment=str(payload.get('comments') or '').strip()
    if decision not in {'approve','reject'} or not 3<=len(comment)<=2000:raise HTTPException(422,'Indique la decisión y un comentario de 3 a 2000 caracteres')
    if decision=='approve' and review['fingerprint']!=fingerprint(_batch_metrics(db,batch),batch_segments(db,batch)):raise HTTPException(409,'El balance cambió desde la solicitud; devuelva el cierre para corregirlo')
    review.update(status='approved' if decision=='approve' else 'rejected',approved_by=current.id,decision_comments=comment,decided_at=now().isoformat())
    save_review(db,batch_id,review)
    if decision=='reject':db.commit();return {'status':'rejected'}
    return technical_close(batch_id,TechnicalClosureIn.model_validate(review['payload']),current,db)


@router.get("/bed-cycles")
def bed_cycles(current=Depends(require_module_permission("floresvolcan:planting:view")), db: Session = Depends(get_db),
               page: int | None = None, page_size: int = 40, active_only: bool = False):
    query = db.query(BedCycle)
    if active_only:
        query = query.filter(BedCycle.status != "cleaned")
    cycles, total = _page(query.order_by(BedCycle.id.desc()), page, page_size)
    ids = [cycle.id for cycle in cycles]
    beds = {bed.id: bed for bed in db.query(ProductionSpace).filter(ProductionSpace.id.in_({c.bed_space_id for c in cycles}))}
    batches = db.query(ProductionBatch).filter(ProductionBatch.bed_cycle_id.in_(ids)).all()
    metrics = bulk_batch_metrics(db, batches)
    by_cycle = {}
    for batch in batches:
        by_cycle.setdefault(batch.bed_cycle_id, []).append(batch)
    areas = dict(db.query(ProductionBatch.bed_cycle_id, func.coalesce(func.sum(PlantingRecord.area_m2), 0))
                 .join(PlantingRecord, PlantingRecord.production_batch_id == ProductionBatch.id)
                 .filter(ProductionBatch.bed_cycle_id.in_(ids)).group_by(ProductionBatch.bed_cycle_id).all())
    result = []
    for cycle in cycles:
        bed = beds.get(cycle.bed_space_id)
        members = by_cycle.get(cycle.id, [])
        result.append({**_obj(cycle, ["id", "bed_space_id", "status", "opened_at", "cleaned_at", "cleaned_by_id", "cleaning_notes"]),
                       "bed_name": bed.name if bed else None, "bed_code": bed.code if bed else None,
                       "batch_ids": [b.id for b in members], "open_batches": sum(b.status != "closed" for b in members),
                       "requires_review": any(metrics[b.id]["unlinked_cuts"] or metrics[b.id]["unreconciled_segments"] for b in members),
                       "occupied_m2": areas.get(cycle.id, Decimal("0")) if cycle.status != "cleaned" else Decimal("0")})
    return _paged(result, total, page, page_size)


@router.post("/bed-cycles/{cycle_id}/clean")
def confirm_bed_cleaning(cycle_id:int,payload:BedCleaningIn,current:Usuario=Depends(require_module_permission("floresvolcan:technical_close:execute")),db:Session=Depends(get_db)):
    cycle=db.query(BedCycle).filter(BedCycle.id==cycle_id).first()
    if not cycle: raise HTTPException(404,"Ciclo no encontrado")
    lock_bed(db,cycle.bed_space_id)
    db.refresh(cycle)
    if cycle.status=="cleaned": raise HTTPException(409,"La limpieza ya fue confirmada")
    if not payload.confirmed_empty: raise HTTPException(422,"Confirme que la mesa quedó vacía y limpia")
    batches=db.query(ProductionBatch).filter(ProductionBatch.bed_cycle_id==cycle.id).all()
    for batch in batches:
        metrics=_batch_metrics(db,batch)
        if batch.status!="closed" or metrics["unlinked_cuts"] or metrics["unreconciled_segments"]:
            raise HTTPException(409,"Todos los lotes y segmentos de esta mesa deben estar conciliados y cerrados")
    if not batches: raise HTTPException(409,"El ciclo no tiene lotes verificables")
    cycle.status="cleaned";cycle.cleaned_at=now();cycle.cleaned_by_id=current.id;cycle.cleaning_notes=payload.comments.strip()
    db.commit()
    return {"id":cycle.id,"status":cycle.status,"cleaned_at":cycle.cleaned_at}


@router.post("/harvest-records/{record_id}/assign-segment")
def assign_historical_harvest(record_id:int,payload:HistoricalHarvestLinkIn,current:Usuario=Depends(require_module_permission("floresvolcan:technical_close:execute")),db:Session=Depends(get_db)):
    record=db.query(HarvestRecord).filter(HarvestRecord.id==record_id).first()
    if not record: raise HTTPException(404,"Corta inexistente")
    batch=lock_batch(db,record.production_batch_id)
    db.refresh(record)
    if record.voided_at: raise HTTPException(409,"La corta fue anulada")
    if record.planting_record_id is not None and db.query(PlantingRecord.id).filter(PlantingRecord.id==record.planting_record_id,PlantingRecord.production_batch_id==batch.id).first():
        raise HTTPException(409,"La corta ya tiene segmento; no se permite reasignación silenciosa")
    segment=db.query(PlantingRecord).filter(PlantingRecord.id==payload.planting_record_id,PlantingRecord.production_batch_id==batch.id).first()
    if not segment: raise HTTPException(422,"El segmento no pertenece al lote de la corta")
    if local_date(record.harvested_at)<local_date(segment.planted_at):
        raise HTTPException(422,"La corta histórica es anterior a esa siembra; revise el original")
    record.planting_record_id=segment.id
    record.notes=(record.notes or "")+f"\nAsignación histórica {now().isoformat()} por usuario #{current.id}: {payload.comments.strip()}"
    db.commit()
    return {"id":record.id,"planting_record_id":segment.id}


@router.post("/requisition-lines/{line_id}/resolve-material",status_code=201)
def resolve_material(line_id:int,payload:MaterialResolutionIn,current:Usuario=Depends(require_module_permission("floresvolcan:inventory:manage")),db:Session=Depends(get_db)):
    line=db.query(RequisitionLine).filter(RequisitionLine.id==line_id).with_for_update().first()
    if not line: raise HTTPException(404,"Línea inexistente")
    previous=db.query(MaterialResolution).filter(MaterialResolution.client_token==payload.client_token).first()
    if previous:
        if previous.requisition_line_id!=line_id or previous.user_id!=current.id or previous.request_hash!=request_hash(payload):
            raise HTTPException(409,"El identificador de movimiento ya corresponde a otros datos")
        return {"id":previous.id,"pending_to_plant":pending_to_plant(line),"pending_delivery":pending_delivery(line),"idempotent":True}
    if line.requisition.status not in {"approved","partially_delivered","delivered"}:
        raise HTTPException(409,"La requisición no está aprobada")
    lot=db.query(InventoryLot).filter(InventoryLot.id==line.inventory_lot_id).populate_existing().with_for_update().first()
    qty=_decimal(payload.quantity)
    if payload.kind=="release":
        if qty>pending_delivery(line) or qty>_decimal(lot.quantity_reserved):
            raise HTTPException(409,"La liberación supera el material reservado sin entregar")
        lot.quantity_reserved=_decimal(lot.quantity_reserved)-qty
        lot.quantity_available=_decimal(lot.quantity_available)+qty
        line.quantity_released=_decimal(line.quantity_released)+qty
    else:
        if qty>pending_to_plant(line) or qty>_decimal(lot.quantity_issued):
            raise HTTPException(409,"La cantidad supera el material entregado pendiente de sembrar")
        lot.quantity_issued=_decimal(lot.quantity_issued)-qty
        if payload.kind=="return":
            lot.quantity_available=_decimal(lot.quantity_available)+qty
            line.quantity_returned=_decimal(line.quantity_returned)+qty
        else:
            lot.quantity_written_off=_decimal(lot.quantity_written_off)+qty
            line.quantity_field_loss=_decimal(line.quantity_field_loss)+qty
    resolution=MaterialResolution(client_token=payload.client_token,request_hash=request_hash(payload),requisition_line_id=line.id,kind=payload.kind,quantity=qty,comments=payload.comments.strip(),user_id=current.id)
    db.add(resolution);db.flush()
    db.add(InventoryMovement(inventory_lot_id=lot.id,movement_type={"return":"RETURN_FROM_FIELD","field_loss":"FIELD_WRITEOFF","release":"RESERVATION_RELEASE"}[payload.kind],
        quantity=qty,reference_type="material_resolution",reference_id=resolution.id,user_id=current.id,comments=payload.comments.strip()))
    db.commit()
    return {"id":resolution.id,"pending_to_plant":pending_to_plant(line),"pending_delivery":pending_delivery(line)}


@router.post("/planting-orders/{order_id}/complete")
def complete_planting_order(order_id:int,current:Usuario=Depends(require_module_permission("floresvolcan:planting:manage")),db:Session=Depends(get_db)):
    order=db.query(PlantingOrder).filter(PlantingOrder.id==order_id).with_for_update().first()
    if not order or order.status not in {"approved","in_progress"}: raise HTTPException(409,"Orden no disponible")
    batches=db.query(ProductionBatch).filter(ProductionBatch.planting_order_id==order.id).all()
    if not batches or any(b.status!="closed" or _batch_metrics(db,b)["unreconciled_segments"] for b in batches):
        raise HTTPException(409,"Concilie y cierre todos los lotes de la orden")
    reqs=db.query(Requisition).filter(Requisition.planting_order_id==order.id).all()
    if any(r.status=="pending" for r in reqs): raise HTTPException(409,"Resuelva las requisiciones pendientes")
    for req in reqs:
        if any(pending_to_plant(line)>0 or pending_delivery(line)>0 for line in req.lines):
            raise HTTPException(409,"Devuelva, dé de baja o libere el material pendiente de la requisición")
    order.status="completed";order.completed_at=now();db.commit()
    return {"id":order.id,"status":order.status}


@router.post("/harvest-records/{record_id}/void")
def void_harvest(record_id:int,payload:ProductionReviewIn,current:Usuario=Depends(require_module_permission("floresvolcan:technical_close:execute")),db:Session=Depends(get_db)):
    record=db.query(HarvestRecord).filter(HarvestRecord.id==record_id).first()
    if not record: raise HTTPException(404,"Corta inexistente")
    batch=lock_batch(db,record.production_batch_id)
    db.refresh(record)
    if record.voided_at: raise HTTPException(409,"La corta ya fue anulada")
    if db.query(SegmentReconciliation.id).join(PlantingRecord,PlantingRecord.id==SegmentReconciliation.planting_record_id).filter(PlantingRecord.production_batch_id==batch.id).first():
        raise HTTPException(409,"El lote ya está conciliado; sus movimientos están cerrados")
    cycle=db.query(BedCycle).filter(BedCycle.id==batch.bed_cycle_id).first()
    if cycle and cycle.status=="cleaned": raise HTTPException(409,"La mesa ya fue liberada")
    record.voided_at=now();record.voided_by_id=current.id;record.void_reason=payload.comments.strip()
    db.commit()
    return {"id":record.id,"voided_at":record.voided_at,"void_reason":record.void_reason}


@router.post("/production-batches/{batch_id}/approve-historical-overage",status_code=201)
def approve_historical_overage(batch_id:int,payload:HistoricalOverageIn,current:Usuario=Depends(require_module_permission("floresvolcan:planting:approve")),db:Session=Depends(get_db)):
    batch=lock_batch(db,batch_id)
    require_linked_history(db,batch)
    metrics=_batch_metrics(db,batch)
    if not metrics["unreconciled_segments"]: raise HTTPException(409,"El lote ya está conciliado")
    missing=max(Decimal("0"),metrics["overage"]-metrics["authorized_overage"])
    if payload.quantity!=missing or not missing:
        raise HTTPException(409,f"La autorización debe coincidir con el excedente histórico pendiente: {missing}")
    approval=HistoricalOverageApproval(production_batch_id=batch.id,quantity=missing,comments=payload.comments.strip(),approved_by_id=current.id)
    db.add(approval);db.commit()
    return {"id":approval.id,"quantity":approval.quantity}


@router.post("/harvest-orders/{order_id}/cancel")
def cancel_harvest_order(order_id:int,payload:ProductionReviewIn,current:Usuario=Depends(require_module_permission("floresvolcan:planting:manage")),db:Session=Depends(get_db)):
    row=db.query(HarvestOrder).filter(HarvestOrder.id==order_id).first()
    if not row: raise HTTPException(404,"Orden inexistente")
    lock_batch(db,row.production_batch_id)
    db.refresh(row)
    if row.status not in {"pending_approval","approved","in_progress"}: raise HTTPException(409,"La orden ya fue procesada")
    if db.query(HarvestRecord.id).filter(HarvestRecord.harvest_order_id==row.id,HarvestRecord.voided_at.is_(None)).first():
        raise HTTPException(409,"La orden tiene cortas vigentes; complete la OT para conservar sus cantidades")
    row.status="cancelled";row.completed_at=now()
    row.notes=(row.notes or "")+f"\nCancelada por usuario #{current.id}: {payload.comments.strip()}"
    db.commit()
    return {"id":row.id,"status":row.status}


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
    sale = _decimal(db.query(func.coalesce(func.sum(HarvestRecord.quantity_harvested), 0)).filter(HarvestRecord.voided_at.is_(None)).scalar())
    output = _decimal(db.query(func.coalesce(func.sum(HarvestRecord.total_output), 0)).filter(HarvestRecord.voided_at.is_(None)).scalar())
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
        ).filter(HarvestRecord.voided_at.is_(None)).group_by(HarvestRecord.actual_harvest_week).order_by(HarvestRecord.actual_harvest_week.desc()).limit(16).all()
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
        HarvestRecord.discard_qty > 0, HarvestRecord.voided_at.is_(None)
    ).group_by(HarvestRecord.discard_reason).order_by(func.sum(HarvestRecord.discard_qty).desc()).all()
    discard_reasons = [{"reason": reason or "SIN MOTIVO", "quantity": _decimal(qty)} for reason, qty in discard_rows]

    grade_1 = _decimal(db.query(func.coalesce(func.sum(HarvestRecord.bloom_1_qty), 0)).filter(HarvestRecord.voided_at.is_(None)).scalar())
    grade_2 = _decimal(db.query(func.coalesce(func.sum(HarvestRecord.bloom_2_qty), 0)).filter(HarvestRecord.voided_at.is_(None)).scalar())
    grade_35 = _decimal(db.query(func.coalesce(func.sum(HarvestRecord.bloom_3_5_qty), 0)).filter(HarvestRecord.voided_at.is_(None)).scalar())

    alerts = []
    late_po = db.query(PurchaseOrder).filter(PurchaseOrder.status.in_(["pending_approval", "in_process"]), PurchaseOrder.warehouse_due_date < today).count()
    if late_po: alerts.append({"severity":"danger","title":"Compras vencidas","message":f"{late_po} orden(es) superaron la fecha compromiso de bodega.","to":"/floresvolcan/compras"})
    req_without_delivery = db.query(Requisition).filter(Requisition.status == "approved").join(RequisitionLine).group_by(Requisition.id).having(func.sum(RequisitionLine.quantity_delivered) <= 0).count()
    if req_without_delivery: alerts.append({"severity":"warning","title":"Requisiciones sin entrega","message":f"{req_without_delivery} requisición(es) aprobadas aún no tienen salida física registrada.","to":"/floresvolcan/produccion"})
    sap_missing = db.query(SapProduct).filter(SapProduct.active.is_(True), or_(SapProduct.default_density.is_(None), SapProduct.default_cycle_weeks.is_(None))).count()
    if sap_missing: alerts.append({"severity":"warning","title":"SAP incompleto","message":f"{sap_missing} código(s) activos no tienen densidad o ciclo configurado.","to":"/floresvolcan/sap"})
    negative_balance = db.query(InventoryLot).filter(or_(InventoryLot.quantity_available < 0, InventoryLot.quantity_reserved < 0, InventoryLot.quantity_issued < 0)).count()
    if negative_balance: alerts.append({"severity":"danger","title":"Inventario inconsistente","message":f"{negative_balance} lote(s) presentan saldo negativo.","to":"/floresvolcan/inventario"})
    batches = db.query(ProductionBatch).filter(ProductionBatch.status == "open").all()
    open_batches = len(batches)
    over_output = sum(m["overage"] > 0 for m in bulk_batch_metrics(db, batches).values())
    if over_output: alerts.append({"severity":"warning","title":"Corte mayor a siembra","message":f"{over_output} lote(s) tienen salida contabilizada superior a la cantidad sembrada.","to":"/floresvolcan/produccion"})

    cut_segments=db.query(HarvestRecord.planting_record_id).filter(HarvestRecord.voided_at.is_(None),HarvestRecord.planting_record_id.isnot(None))
    cultivated=_decimal(db.query(func.coalesce(func.sum(PlantingRecord.quantity_planted),0)).join(ProductionBatch,ProductionBatch.id==PlantingRecord.production_batch_id).filter(ProductionBatch.status=='open',~PlantingRecord.id.in_(cut_segments)).scalar())
    warehouse_bulbs=inv_available+inv_reserved
    box_equivalents=_decimal(db.query(func.coalesce(func.sum(case((InventoryLot.units_per_box>0,(InventoryLot.quantity_available+InventoryLot.quantity_reserved)/InventoryLot.units_per_box),else_=0)),0)).scalar())
    transit=db.query(PurchaseOrder).filter(PurchaseOrder.status=='in_process',PurchaseOrder.paid_at.isnot(None),PurchaseOrder.shipped_at.isnot(None)).count()
    return {
        "kpis": {
            "cultivated_bulbs":cultivated,"warehouse_bulbs":warehouse_bulbs,"warehouse_box_equivalents":box_equivalents,"purchase_in_transit":transit,
            "suppliers_active": db.query(Supplier).filter(Supplier.active.is_(True)).count(),
            "purchase_pending": db.query(PurchaseOrder).filter(PurchaseOrder.status == "pending_approval").count(),
            "purchase_in_process": db.query(PurchaseOrder).filter(PurchaseOrder.status == "in_process").count(),
            "inventory_lots": db.query(InventoryLot).count(),
            "inventory_available": inv_available, "inventory_reserved": inv_reserved, "inventory_in_field": inv_issued,
            "inventory_consumed": inv_consumed,
            "total_received": _decimal(db.query(func.coalesce(func.sum(InventoryLot.quantity_received), 0)).scalar()),
            "total_issued": _decimal(db.query(func.coalesce(func.sum(RequisitionLine.quantity_delivered), 0)).scalar()),
            "total_harvested": output,
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
    result = _dashboard_v05(db)
    from modules.mod_floresvolcan.access_policy import role_permissions
    role = role_for_module(db,current,"mod_floresvolcan")
    allowed = role_permissions(role) if not current.is_superadmin and role != "superadmin" else None
    route_permissions = {'/floresvolcan/sap':'sap:view','/floresvolcan/compras':'purchases:view',
                         '/floresvolcan/inventario':'inventory:view','/floresvolcan/produccion':'planting:view'}
    if allowed is not None:
        result['alerts'] = [a for a in result['alerts'] if 'floresvolcan:'+route_permissions.get(a.get('to'),'dashboard:view') in allowed]
    result['session'] = {'user_id':current.id,'role':'superadmin' if current.is_superadmin else role}
    result['pending_paper_reviews'] = db.query(PaperCapture).filter(PaperCapture.status=='submitted').count()
    can_approve=lambda key:allowed is None or 'floresvolcan:'+key in allowed
    from .planting_preparation import is_manager
    manager=is_manager(db,current)
    pending_orders=[i for (i,) in db.query(PlantingOrder.id).filter(PlantingOrder.status=='pending_approval')]
    plans={int(p.key.split('.')[-1]):json.loads(p.value_text) for p in db.query(ModuleSetting).filter(ModuleSetting.key.like('planting_plan.%')).all()}
    my_orders=sum(1 for i in pending_orders if manager and (current.is_superadmin or i not in plans or plans[i]['approver_user_id']==current.id))
    legacy_requisitions=db.query(Requisition.planting_order_id).filter(Requisition.status=='pending').all()
    pending_material=sum(1 for (i,) in legacy_requisitions if i not in plans) if manager else 0
    result['kpis']['pending_approvals']=(pending_material if can_approve('requisitions:approve') else 0)+my_orders+(db.query(HarvestOrder).filter(HarvestOrder.status=='pending_approval').count() if can_approve('planting:approve') else 0)+(result['pending_paper_reviews'] if can_approve('paper:review') else 0)+(result['kpis']['purchase_pending'] if can_approve('purchases:approve') else 0)
    if my_orders:
        result['alerts'].append({'severity':'warning','title':'Siembras por aprobar','message':f'{my_orders} preparaciones esperan su aprobación','to':'/floresvolcan/produccion'})
    if can_approve('config:view'):
        pending_closures = sum(1 for row in db.query(ModuleSetting).filter(ModuleSetting.key.like('harvest_close.%')).all() if json.loads(row.value_text).get('status') == 'pending')
        result['kpis']['pending_approvals'] += pending_closures
        if pending_closures:
            result['alerts'].append({'severity':'warning','title':'Cierres por validar','message':f'{pending_closures} cierres de cosecha con diferencias por revisar','to':'/floresvolcan/cosechas?phase=under_review'})
    return result


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
    req=db.query(Requisition).filter(Requisition.id==requisition_id).populate_existing().with_for_update().first()
    if not req: raise HTTPException(404,"Requisición no encontrada")
    if req.status not in {"approved","partially_delivered"}: raise HTTPException(409,"Solo se entrega material de una requisición aprobada con saldo pendiente")
    line=db.query(RequisitionLine).filter(RequisitionLine.id==payload.requisition_line_id,RequisitionLine.requisition_id==req.id).with_for_update().first()
    if not line: raise HTTPException(422,"La línea no pertenece a la requisición")
    qty=_decimal(payload.quantity)
    pending=pending_delivery(line)
    if qty>pending: raise HTTPException(409,f"La entrega supera el saldo pendiente ({pending})")
    lot=db.query(InventoryLot).filter(InventoryLot.id==line.inventory_lot_id).with_for_update().first()
    if not lot: raise HTTPException(409,"Lote de inventario inexistente")
    if _decimal(lot.quantity_reserved)<qty: raise HTTPException(409,"El lote ya no tiene suficiente cantidad reservada")
    delivered_at=payload.delivered_at or now()
    if payload.boxes is not None and abs(_decimal(payload.boxes)*_decimal(payload.units_per_box or lot.units_per_box)-qty)>Decimal("0.001"):
        raise HTTPException(422,"Cajas por unidades por caja no coincide con la cantidad entregada")
    row=RequisitionDelivery(number=_next("SAL",db.query(RequisitionDelivery).count()),requisition_line_id=line.id,inventory_lot_id=lot.id,
        boxes=payload.boxes,units_per_box=payload.units_per_box or lot.units_per_box,quantity=qty,delivery_week=_iso_week(delivered_at),
        delivered_at=delivered_at,delivered_by_id=current.id,comments=payload.comments)
    db.add(row); db.flush()
    lot.quantity_reserved=_decimal(lot.quantity_reserved)-qty
    lot.quantity_issued=_decimal(lot.quantity_issued)+qty
    line.quantity_delivered=_decimal(line.quantity_delivered)+qty
    db.add(InventoryMovement(inventory_lot_id=lot.id,movement_type="ISSUE_TO_FIELD",quantity=qty,reference_type="requisition_delivery",reference_id=row.id,user_id=current.id,comments=payload.comments))
    if pending_delivery(line)<=0:
        all_lines=db.query(RequisitionLine).filter(RequisitionLine.requisition_id==req.id).all()
        if all(pending_delivery(x)<=0 for x in all_lines): req.status="delivered"
        else: req.status="partially_delivered"
    else: req.status="partially_delivered"
    db.commit(); db.refresh(row)
    return _obj(row,["id","number","requisition_line_id","quantity","boxes","units_per_box","delivery_week","delivered_at","comments"])

# ---------- Órdenes de cosecha, calendario y papel → digital ----------
def _harvest_eta(db: Session, batch: ProductionBatch) -> date | None:
    segments=batch_segments(db,batch)
    return min((x["harvest_due_date"] for x in segments),default=None)


def _harvest_order_dict(db:Session, row:HarvestOrder,batch:ProductionBatch|None=None, *, eta=None, preloaded=False, crew=None)->dict:
    batch=batch or db.query(ProductionBatch).filter(ProductionBatch.id==row.production_batch_id).first()
    crew=crew if preloaded else db.query(WorkCrew).filter(WorkCrew.id==row.crew_id).first()
    return {**_obj(row,["id","number","qr_token","production_batch_id","crew_id","status","planned_date","early_release_reason",
            "allow_overage","overage_quantity","overage_reason","notes","created_at","approved_at","completed_at"]),
            "batch_code":batch.code if batch else None,"harvest_due_date":eta if preloaded else _harvest_eta(db,batch) if batch else None,
            "crew_name":crew.name if crew else None}


@router.get('/harvest-calendar')
def harvest_calendar(current=Depends(require_module_permission('floresvolcan:planting:view')),db:Session=Depends(get_db), page: int | None = None, page_size: int = 40, q: str | None = None, phase: str | None = None, selected_id: int | None = None):
    if page is not None and (page < 1 or not 1 <= page_size <= 100):
        raise HTTPException(422, "Página o tamaño inválido (máximo 100)")
    rows=db.query(ProductionBatch).order_by(ProductionBatch.opened_at.desc(), ProductionBatch.id.desc()).all()
    batches_by_id={b.id:b for b in rows}
    today=datetime.now(LOCAL_TZ).date()
    result=[]
    ids=[b.id for b in rows]
    from .harvest_timing import harvest_timing
    etas={}; timing={}
    for rec in db.query(PlantingRecord).filter(PlantingRecord.production_batch_id.in_(ids)):
        batch=batches_by_id[rec.production_batch_id]
        candidate=harvest_timing(local_date(rec.planted_at),rec.cycle_weeks or batch.cycle_weeks,today)
        due=candidate['harvest_due_date']
        if due and (batch.id not in etas or due<etas[batch.id]):
            etas[batch.id]=due;timing[batch.id]=candidate
        elif batch.id not in timing:
            timing[batch.id]=candidate
    by_batch={}
    for order in db.query(HarvestOrder).filter(HarvestOrder.production_batch_id.in_(ids)).order_by(HarvestOrder.created_at.desc()):
        by_batch.setdefault(order.production_batch_id,[]).append(order)
    lots={x.id:x for x in db.query(InventoryLot).filter(InventoryLot.id.in_([b.inventory_lot_id for b in rows if b.inventory_lot_id]))}
    products={x.id:x for x in db.query(SapProduct).filter(SapProduct.id.in_([b.sap_product_id for b in rows]))}
    beds={x.id:x for x in db.query(ProductionSpace).filter(ProductionSpace.id.in_([b.bed_space_id for b in rows if b.bed_space_id]))}
    crews={x.id:x for x in db.query(WorkCrew)}
    places=dict(beds)
    pending_parents={b.parent_id for b in beds.values() if b.parent_id}
    while pending_parents:
        ancestors=db.query(ProductionSpace).filter(ProductionSpace.id.in_(pending_parents)).all()
        places.update({x.id:x for x in ancestors})
        pending_parents={x.parent_id for x in ancestors if x.parent_id and x.parent_id not in places}
    def location_name(bed):
        if not bed:return ''
        name=bed.name;seen={bed.id};node=places.get(bed.parent_id);farm=None
        while node and node.id not in seen:
            seen.add(node.id)
            if node.type=='greenhouse':return f'{node.name} → {name}'
            if node.type=='farm':farm=node.name
            node=places.get(node.parent_id)
        return f'{farm} → {name}' if farm else name
    cut_batch_ids={r[0] for r in db.query(HarvestRecord.production_batch_id).filter(HarvestRecord.production_batch_id.in_(ids),HarvestRecord.voided_at.is_(None)).distinct()}
    review_rows=db.query(ModuleSetting).filter(ModuleSetting.key.in_([f'harvest_close.{i}' for i in ids])).all()
    reviewing={int(r.key.split('.')[-1]) for r in review_rows if json.loads(r.value_text).get('status')=='pending'}
    summaries={}
    matching=[]
    states={}
    for batch in rows:
        eta=etas.get(batch.id)
        orders=by_batch.get(batch.id,[])
        state=('history_review' if batch.history_review_required else 'closed' if batch.status=='closed' else 'under_review' if batch.id in reviewing else 'in_harvest' if batch.id in cut_batch_ids or any(x.status=='in_progress' for x in orders)
               else 'harvest_scheduled' if any(x.status=='approved' for x in orders)
               else 'ready' if eta and eta<=today else 'upcoming' if eta and eta<=today+timedelta(days=21)
               else 'growing')
        summaries[state]=summaries.get(state,0)+1
        states[batch.id]=state
        lot=lots.get(batch.inventory_lot_id); prod=products.get(batch.sap_product_id); bed=beds.get(batch.bed_space_id)
        terms=" ".join(str(v or "") for v in (batch.code, batch.planting_week, lot.variety_snapshot if lot else None, prod.variety if prod else None, prod.description if prod else None, bed.name if bed else None))
        if q and q.casefold().strip() not in terms.casefold(): continue
        if selected_id is not None and batch.id != selected_id: continue
        if phase == 'active' and state == 'closed': continue
        if phase == 'selectable' and state in {'closed','history_review'}: continue
        if phase == 'ready' and (timing.get(batch.id,{}).get('readiness') not in {'ready','upcoming'} or state in {'closed','history_review'}): continue
        if phase == 'in_harvest' and state not in {'harvest_scheduled','in_harvest'}: continue
        if phase == 'under_review' and state != 'under_review': continue
        if phase == 'closed' and state != 'closed': continue
        matching.append(batch)
    total=len(matching)
    rows=matching if page is None else matching[(page-1)*page_size:page*page_size]
    all_metrics=bulk_batch_metrics(db,rows)
    for batch in rows:
        eta=etas.get(batch.id); orders=by_batch.get(batch.id,[]); state=states[batch.id]
        metrics=all_metrics[batch.id]
        lot=lots.get(batch.inventory_lot_id); prod=products.get(batch.sap_product_id); bed=beds.get(batch.bed_space_id)
        result.append({"id":batch.id,"code":batch.code,"variety":(lot.variety_snapshot if lot else None) or (prod.variety if prod else None) or (prod.description if prod else None),
                       "bed_name":bed.name if bed else None,"location_label":location_name(bed),
                       **timing.get(batch.id,{}),"planting_week":batch.planting_week,"harvest_due_date":eta,
                       "bed_cycle_id":batch.bed_cycle_id,"status":state,"planting_order_id":batch.planting_order_id,"expected_stems_per_bulb":batch.expected_stems_per_bulb,
                       "expected_stems":metrics['planted']*_decimal(batch.expected_stems_per_bulb or 1),
                       **metrics,"harvest_orders":[_harvest_order_dict(db,x,batch,eta=eta,preloaded=True,crew=crews.get(x.crew_id)) for x in orders]})
    return _paged(result, total, page, page_size, summary=summaries)


@router.get('/harvest-orders')
def list_harvest_orders(current=Depends(require_module_permission('floresvolcan:planting:view')),db:Session=Depends(get_db)):
    return [_harvest_order_dict(db,row) for row in db.query(HarvestOrder).order_by(HarvestOrder.created_at.desc()).all()]


@router.post('/harvest-orders',status_code=201)
def create_harvest_order(payload:HarvestOrderIn,current:Usuario=Depends(require_module_permission('floresvolcan:planting:manage')),db:Session=Depends(get_db)):
    batch=lock_batch(db,payload.production_batch_id)
    require_linked_history(db,batch)
    if batch.status!='open': raise HTTPException(409,'Lote productivo cerrado o inexistente')
    if not db.query(PlantingRecord.id).filter(PlantingRecord.production_batch_id==batch.id).first():
        raise HTTPException(409,'No existe siembra real para esta cosecha')
    crew=db.query(WorkCrew).filter(WorkCrew.id==payload.crew_id,WorkCrew.active.is_(True)).first()
    if not crew or not crew.members: raise HTTPException(422,'Seleccione una cuadrilla activa con trabajadores')
    from .crew_management import validate_assignment
    validate_assignment(db,crew,'harvest',payload.planned_date)
    active=db.query(HarvestOrder.id).filter(HarvestOrder.production_batch_id==batch.id,
        HarvestOrder.status.in_(['pending_approval','approved','in_progress'])).first()
    if active: raise HTTPException(409,'Ya existe una orden de cosecha abierta para este lote')
    eta=_harvest_eta(db,batch)
    if eta and payload.planned_date<eta and not (payload.early_release_reason or '').strip():
        raise HTTPException(422,'La cosecha anticipada requiere justificación técnica')
    if payload.allow_overage and (not (payload.overage_reason or '').strip() or payload.overage_quantity<=0):
        raise HTTPException(422,'Indique el motivo y la cantidad máxima adicional del excedente')
    if not payload.allow_overage and payload.overage_quantity:
        raise HTTPException(422,'La cantidad adicional requiere habilitar la excepción')
    row=HarvestOrder(number=_next('OCOS',db.query(HarvestOrder).count()),production_batch_id=batch.id,
        crew_id=crew.id,planned_date=payload.planned_date,early_release_reason=payload.early_release_reason,
        allow_overage=payload.allow_overage,overage_quantity=payload.overage_quantity,overage_reason=payload.overage_reason,notes=payload.notes,created_by_id=current.id)
    db.add(row);db.commit();db.refresh(row)
    return _harvest_order_dict(db,row,batch)


@router.post('/harvest-orders/{order_id}/approve')
def approve_harvest_order(order_id:int,current:Usuario=Depends(require_module_permission('floresvolcan:planting:approve')),db:Session=Depends(get_db)):
    row=db.query(HarvestOrder).filter(HarvestOrder.id==order_id).first()
    if not row: raise HTTPException(404,'Orden de cosecha inexistente')
    batch=lock_batch(db,row.production_batch_id)
    row=db.query(HarvestOrder).filter(HarvestOrder.id==order_id).populate_existing().with_for_update().first()
    if row.status!='pending_approval': raise HTTPException(409,'La orden ya fue procesada')
    batch=db.query(ProductionBatch).filter(ProductionBatch.id==row.production_batch_id).first()
    if not batch or batch.status!='open': raise HTTPException(409,'Lote productivo cerrado')
    require_linked_history(db,batch)
    if row.allow_overage and (not row.overage_quantity or not (row.overage_reason or '').strip()):
        raise HTTPException(409,'La excepción debe tener una cantidad máxima y justificación')
    row.qr_token=row.qr_token or uuid.uuid4().hex
    row.status='approved';row.approved_at=now();row.approved_by_id=current.id
    cycle=db.query(BedCycle).filter(BedCycle.id==batch.bed_cycle_id).first()
    if cycle: cycle.status='harvesting'
    db.commit();return {'ok':True,'status':row.status}


@router.post('/harvest-orders/{order_id}/complete')
def complete_harvest_order(order_id:int,current:Usuario=Depends(require_module_permission('floresvolcan:planting:manage')),db:Session=Depends(get_db)):
    row=db.query(HarvestOrder).filter(HarvestOrder.id==order_id).first()
    if not row: raise HTTPException(404,'Orden de cosecha inexistente')
    batch=lock_batch(db,row.production_batch_id)
    row=db.query(HarvestOrder).filter(HarvestOrder.id==order_id).populate_existing().with_for_update().first()
    if row.status not in {'approved','in_progress'}: raise HTTPException(409,'Solo se completa una OT autorizada')
    if not db.query(HarvestRecord.id).filter(HarvestRecord.harvest_order_id==row.id,HarvestRecord.voided_at.is_(None)).first():
        raise HTTPException(409,'Registre por lo menos una corta antes de completar')
    row.status='completed';row.completed_at=now();db.commit()
    return {'ok':True,'status':row.status}


def _paper_order_context(db:Session,current:Usuario,operation:str,order_id:int,require_active:bool=True):
    if operation=='planting':
        row=db.query(PlantingOrder).filter(PlantingOrder.id==order_id).first()
        if not row or (require_active and row.status not in {'approved','in_progress'}): raise HTTPException(404,'OT de siembra no disponible')
        _assert_crew(db,current,row.crew_id)
        bed=db.query(ProductionSpace).filter(ProductionSpace.id==row.production_space_id).first()
        return row,{'location':bed.name if bed else ''}
    if operation=='harvest':
        row=db.query(HarvestOrder).filter(HarvestOrder.id==order_id).first()
        if not row or (require_active and row.status not in {'approved','in_progress'}): raise HTTPException(404,'OT de cosecha no disponible')
        _assert_crew(db,current,row.crew_id)
        batch=db.query(ProductionBatch).filter(ProductionBatch.id==row.production_batch_id).first()
        lot=db.query(InventoryLot).filter(InventoryLot.id==batch.inventory_lot_id).first() if batch and batch.inventory_lot_id else None
        product=db.query(SapProduct).filter(SapProduct.id==batch.sap_product_id).first() if batch else None
        bed=db.query(ProductionSpace).filter(ProductionSpace.id==batch.bed_space_id).first() if batch else None
        return row,{'variety':(lot.variety_snapshot if lot else None) or (product.variety if product else ''),
                    'lot':lot.supplier_lot_code if lot else '', 'batch':batch.code if batch else '',
                    'location':bed.name if bed else ''}
    raise HTTPException(422,'Tipo de formulario inválido')


@router.get('/field/paper/template/{operation}/{order_id}')
def paper_template(operation:str,order_id:int,planting_record_id:int|None=None,requisition_line_id:int|None=None,current:Usuario=Depends(require_module_permission('floresvolcan:field:use')),db:Session=Depends(get_db)):
    _field_worker(db,current)
    row,context=_paper_order_context(db,current,operation,order_id)
    if operation=='harvest':
        segments=db.query(PlantingRecord).filter(PlantingRecord.production_batch_id==row.production_batch_id).all()
        segment=next((s for s in segments if s.id==planting_record_id),None) if planting_record_id else segments[0] if len(segments)==1 else None
        if not segment: raise HTTPException(422,'Seleccione la siembra exacta antes de imprimir la hoja')
        context['lot']=f"{context.get('lot','')} / Siembra #{segment.id} / lado {segment.side or '—'}"
    elif requisition_line_id:
        line=db.query(RequisitionLine).filter(RequisitionLine.id==requisition_line_id).first()
        if not line or line.requisition.planting_order_id!=row.id: raise HTTPException(422,'El material no corresponde a esta orden')
        lot=db.query(InventoryLot).filter(InventoryLot.id==line.inventory_lot_id).first()
        product=db.query(SapProduct).filter(SapProduct.id==line.sap_product_id).first()
        context['variety']=lot.variety_snapshot or product.variety or product.description
        context['lot']=f"{lot.lot_code} / Req. línea #{line.id}"
        context['density_cycle']=f"{product.default_density or '—'} / {product.default_cycle_weeks or '—'} semanas"
    elif operation=='planting':
        raise HTTPException(422,'Seleccione el material antes de imprimir la hoja')
    context['worker']=current.nombre
    content=build_field_pdf(operation=operation,number=row.number,context=context)
    return StreamingResponse(BytesIO(content),media_type='application/pdf',headers={'Content-Disposition':f'attachment; filename="FV-{operation}-{row.id}.pdf"',
        'Cache-Control':'private, no-store'})


@router.post('/field/paper/scans',status_code=201)
async def paper_scan(operation:str=Form(...),order_id:int=Form(...),file:UploadFile=File(...),
    current:Usuario=Depends(require_module_permission('floresvolcan:field:use')),db:Session=Depends(get_db)):
    _field_worker(db,current)
    order,_context=_paper_order_context(db,current,operation,order_id)
    db.refresh(order,with_for_update=True)
    raw=await file.read(8*1024*1024+1)
    content,digest=normalize_scan(raw)
    duplicate=db.query(PaperCapture).filter(PaperCapture.operation==operation,PaperCapture.sha256==digest,
        PaperCapture.planting_order_id==order_id if operation=='planting' else PaperCapture.harvest_order_id==order_id).first()
    if duplicate:
        if duplicate.status not in {'pending_review','submitted'}: raise HTTPException(409,'Esta imagen ya fue contabilizada; consulte el registro original')
        if duplicate.created_by_id!=current.id: raise HTTPException(409,'Esta imagen ya fue presentada por otro trabajador')
        return {'id':duplicate.id,'status':duplicate.status,'ocr':json.loads(duplicate.suggested_json or '{}'),'sha256':digest}
    storage=paper_storage_dir()
    filename=uuid.uuid4().hex+'.jpg'
    target=storage/filename
    try:
        target.write_bytes(content)
        target.chmod(0o600)
        proposed=suggest_photo_data(content,operation)
        row=PaperCapture(capture_key=uuid.uuid4().hex,operation=operation,
            planting_order_id=order_id if operation=='planting' else None,
            harvest_order_id=order_id if operation=='harvest' else None,
            storage_key=filename,sha256=digest,mime_type='image/jpeg',
            suggested_json=json.dumps(proposed,ensure_ascii=False),created_by_id=current.id)
        db.add(row);db.commit();db.refresh(row)
    except Exception:
        db.rollback();target.unlink(missing_ok=True)
        raise
    return {'id':row.id,'status':row.status,'ocr':proposed,'sha256':digest,
            'message':'Fotografía guardada. Envíe los datos para revisión del administrador.'}


@router.get('/field/paper/captures/{capture_id}/image')
def paper_capture_image(capture_id:int,current:Usuario=Depends(require_module_permission('floresvolcan:field:use')),db:Session=Depends(get_db)):
    _field_worker(db,current)
    row=db.query(PaperCapture).filter(PaperCapture.id==capture_id).first()
    if not row: raise HTTPException(404,'Captura inexistente')
    _paper_order_context(db,current,row.operation,row.planting_order_id if row.operation=='planting' else row.harvest_order_id,require_active=False)
    if not current.is_superadmin and str(role_for_module(db,current,'mod_floresvolcan') or '').lower() not in {'admin','gerente'} and row.created_by_id!=current.id:
        raise HTTPException(403,'No autorizado a consultar esta imagen')
    path=paper_storage_dir()/row.storage_key
    if not path.is_file(): raise HTTPException(404,'Archivo no encontrado')
    return FileResponse(path,media_type='image/jpeg',headers={'Cache-Control':'private, no-store','X-Content-Type-Options':'nosniff'})


# ---------- Histórico de Excel conservado para exploración ----------
@router.get('/excel-history/summary')
def excel_history_summary(current=Depends(require_module_permission('floresvolcan:dashboard:view')), db: Session = Depends(get_db)):
    from .models import ExcelImportRun
    row = db.query(ExcelImportRun).order_by(ExcelImportRun.id.desc()).first()
    if not row:
        return {"loaded": False}
    return {"loaded": True, "id": row.id, "filename": row.filename,
            "comparison_filename": row.comparison_filename, "created_at": row.created_at,
            **json.loads(row.summary_json)}


@router.get('/excel-history/rows')
def excel_history_rows(sheet: str = 'Entradas', q: str = '', status: str = '', page: int = 1, page_size: int = 40,
                       current=Depends(require_module_permission('floresvolcan:dashboard:view')), db: Session = Depends(get_db)):
    from .models import ExcelImportRun, ExcelImportRow
    from .excel_history import SHEETS, normalized
    if sheet not in SHEETS or status not in {'', 'imported', 'review'} or not 1 <= page_size <= 100 or page < 1 or len(q) > 200:
        raise HTTPException(422, 'Filtros de histórico inválidos')
    run = db.query(ExcelImportRun).order_by(ExcelImportRun.id.desc()).first()
    if not run:
        return {"rows": [], "total": 0, "page": page, "page_size": page_size}
    query = db.query(ExcelImportRow).filter(ExcelImportRow.import_run_id == run.id, ExcelImportRow.sheet == sheet)
    if status:
        query = query.filter(ExcelImportRow.status == status)
    if q.strip():
        query = query.filter(ExcelImportRow.search_text.contains(normalized(q), autoescape=True))
    total = query.count()
    rows = query.order_by(ExcelImportRow.source_row).offset((page-1)*page_size).limit(page_size).all()
    return {"rows": [{"id": r.id, "source_row": r.source_row, "status": r.status, "values": json.loads(r.raw_json),
                      "issues": json.loads(r.issues_json), "links": json.loads(r.links_json)} for r in rows],
            "total": total, "page": page, "page_size": page_size}


@router.post('/field/paper/captures/{capture_id}/submit')
def submit_paper_capture(capture_id:int, payload:dict=Body(...),
    current:Usuario=Depends(require_module_permission('floresvolcan:field:use')),db:Session=Depends(get_db)):
    row=db.query(PaperCapture).filter(PaperCapture.id==capture_id).with_for_update().first()
    if not row or row.created_by_id!=current.id:
        raise HTTPException(404,'Hoja no encontrada')
    if row.status not in {'pending_review','submitted'}:
        raise HTTPException(409,'Esta hoja ya fue revisada')
    order_id=row.planting_order_id if row.operation=='planting' else row.harvest_order_id
    _paper_order_context(db,current,row.operation,order_id)
    schema=PlantingRecordIn if row.operation=='planting' else HarvestRecordIn
    from pydantic import ValidationError
    try:
        data=schema.model_validate({**payload,'paper_capture_id':row.id,'client_token':f'paper-{row.capture_key}'})
    except ValidationError as exc:
        raise HTTPException(422,str(exc)) from exc
    if (data.planting_order_id if row.operation=='planting' else data.harvest_order_id)!=order_id:
        raise HTTPException(422,'Los datos pertenecen a otra orden')
    saved=json.loads(row.suggested_json or '{}')
    proposed=data.model_dump(mode='json',exclude={'worker_code'})
    if row.status=='submitted':
        if {k:v for k,v in (saved.get('submission') or {}).items() if k!='worker_code'}!=proposed:
            raise HTTPException(409,'La hoja ya fue enviada; sus datos están bloqueados durante la revisión')
        return {'id':row.id,'status':row.status,'idempotent':True}
    saved['submission']=proposed
    row.suggested_json=json.dumps(saved,ensure_ascii=False)
    row.status='submitted'
    db.commit()
    return {'id':row.id,'status':row.status,'message':'Enviada al administrador. Aún no modifica los saldos.'}


@router.get('/paper-reviews')
def paper_reviews(current:Usuario=Depends(require_module_permission('floresvolcan:paper:review')),db:Session=Depends(get_db)):
    rows=db.query(PaperCapture,Usuario.nombre).outerjoin(Usuario,Usuario.id==PaperCapture.created_by_id).filter(
        PaperCapture.status.in_(['pending_review','submitted'])).order_by(PaperCapture.created_at).limit(100).all()
    return [{**_obj(row,['id','operation','planting_order_id','harvest_order_id','status','created_at','created_by_id']),
             'worker_name':name,'ocr':json.loads(row.suggested_json or '{}')} for row,name in rows]


@router.post('/paper-reviews/{capture_id}/approve')
def approve_paper_capture(capture_id:int,payload:dict=Body(...),
    current:Usuario=Depends(require_module_permission('floresvolcan:paper:review')),db:Session=Depends(get_db)):
    row=db.query(PaperCapture).filter(PaperCapture.id==capture_id).with_for_update().first()
    if not row: raise HTTPException(404,'Hoja inexistente')
    if row.status=='digitized': return {'id':row.linked_record_id,'idempotent':True}
    if row.status!='submitted': raise HTTPException(409,'El trabajador debe enviar los datos para revisión')
    if row.created_by_id==current.id and not current.is_superadmin:
        raise HTTPException(403,'Otra persona debe aprobar su hoja')
    saved=json.loads(row.suggested_json or '{}')
    submission=saved.get('submission') or {}
    # Only editable measurements; the order, segment, material and bed stay linked.
    editable={'planted_at','harvested_at','quantity_planted','side','density','cycle_weeks',
              'bloom_1_qty','bloom_2_qty','bloom_3_5_qty','discard_qty','discard_reason','notes'}
    submission.update({k:v for k,v in payload.items() if k in editable})
    schema=PlantingRecordIn if row.operation=='planting' else HarvestRecordIn
    from pydantic import ValidationError
    try: data=schema.model_validate(submission)
    except ValidationError as exc: raise HTTPException(422,str(exc)) from exc
    saved['submission']={k:v for k,v in submission.items() if k!='worker_code'}
    saved['approved_payload']=data.model_dump(mode='json',exclude={'worker_code'})
    row.suggested_json=json.dumps(saved,ensure_ascii=False)
    # These handlers apply all inventory, crew, date and harvest balance guards;
    # capture state and stock are committed together by the registration handler.
    return register_planting(data,current,db) if row.operation=='planting' else register_harvest(data,current,db)


@router.post('/paper-reviews/{capture_id}/reject')
def reject_paper_capture(capture_id:int,payload:dict=Body(...),
    current:Usuario=Depends(require_module_permission('floresvolcan:paper:review')),db:Session=Depends(get_db)):
    row=db.query(PaperCapture).filter(PaperCapture.id==capture_id).with_for_update().first()
    if not row or row.status not in {'pending_review','submitted'}: raise HTTPException(409,'Hoja no disponible para revisión')
    reason=str(payload.get('reason') or '').strip()
    if not reason or len(reason)>2000: raise HTTPException(422,'Indique el motivo de rechazo (máximo 2000 caracteres)')
    saved=json.loads(row.suggested_json or '{}'); saved['rejection_reason']=reason
    row.suggested_json=json.dumps(saved,ensure_ascii=False); row.status='rejected'
    row.reviewed_by_id=current.id; row.reviewed_at=now(); db.commit()
    return {'id':row.id,'status':row.status}


@router.patch('/workers/{worker_id}/status')
def worker_status(worker_id:int,payload:SapProductStatusIn,current:Usuario=Depends(require_module_permission('floresvolcan:workers:manage')),db:Session=Depends(get_db)):
    row=db.get(Worker,worker_id)
    if not row: raise HTTPException(404,'Trabajador no encontrado')
    row.active=payload.active;db.commit()
    return {'id':row.id,'active':row.active}


@router.patch('/suppliers/{supplier_id}/status')
def supplier_status(supplier_id:int,payload:SapProductStatusIn,current:Usuario=Depends(require_module_permission('floresvolcan:suppliers:manage')),db:Session=Depends(get_db)):
    row=db.get(Supplier,supplier_id)
    if not row: raise HTTPException(404,'Proveedor no encontrado')
    if not payload.active and db.query(PurchaseOrder.id).filter(PurchaseOrder.supplier_id==row.id,PurchaseOrder.status.in_(['pending_approval','in_process'])).first():
        raise HTTPException(409,'El proveedor tiene órdenes pendientes o en proceso')
    row.active=payload.active;db.commit()
    return {'id':row.id,'active':row.active}


@router.get('/field/paper/mine')
def my_paper_captures(current:Usuario=Depends(require_module_permission('floresvolcan:field:use')),db:Session=Depends(get_db)):
    rows=db.query(PaperCapture).filter(PaperCapture.created_by_id==current.id).order_by(PaperCapture.created_at.desc()).limit(30).all()
    return [{**_obj(r,['id','operation','planting_order_id','harvest_order_id','status','created_at']),
        'ocr':json.loads(r.suggested_json or '{}')} for r in rows]


@router.get('/paper-reviews/{capture_id}')
def paper_review_detail(capture_id:int,current:Usuario=Depends(require_module_permission('floresvolcan:paper:review')),db:Session=Depends(get_db)):
    row=db.get(PaperCapture,capture_id)
    if not row: raise HTTPException(404,'Hoja inexistente')
    order_id=row.planting_order_id if row.operation=='planting' else row.harvest_order_id
    order,context=_paper_order_context(db,current,row.operation,order_id,require_active=False)
    data=json.loads(row.suggested_json or '{}')
    submission=data.get('submission') or {}
    if row.operation=='planting':
        line=db.get(RequisitionLine,submission.get('requisition_line_id')) if submission.get('requisition_line_id') else None
        if line and line.requisition.planting_order_id==order.id:
            lot=db.get(InventoryLot,line.inventory_lot_id);product=db.get(SapProduct,line.sap_product_id)
            context['variety']=(lot.variety_snapshot if lot else None) or (product.variety if product else '')
            context['lot']=lot.supplier_lot_code or lot.lot_code if lot else ''
            context['product_code']=product.code if product else ''
        bed=db.get(ProductionSpace,submission.get('bed_space_id')) if submission.get('bed_space_id') else None
        if bed:context['location']=bed.name
    context['order_number']=order.number
    context['planting_record_id']=submission.get('planting_record_id')
    context['side']=submission.get('side')
    return {'id':row.id,'context':context,'ocr':data}


@router.post('/planting/flower-options',status_code=201)
def add_flower_crop(payload: FlowerCropIn, current=Depends(require_module_permission('floresvolcan:spaces:manage')), db: Session=Depends(get_db)):
    from .schemas import FlowerCropIn
    import unicodedata
    data=payload
    name=data.name.strip()
    if db.query(FlowerCrop.id).filter(func.lower(FlowerCrop.name)==name.lower()).first(): raise HTTPException(409,'El cultivo ya existe')
    code=''.join(c for c in unicodedata.normalize('NFKD',name).upper() if c.isascii() and (c.isalnum() or c==' ')).replace(' ','-')[:30] or 'CULTIVO'
    if db.query(FlowerCrop.id).filter(FlowerCrop.code==code).first():code+='-'+uuid.uuid4().hex[:6]
    row=FlowerCrop(code=code,name=name,material_type=data.material_type.strip().lower(),active=True)
    db.add(row);db.commit();db.refresh(row)
    return _obj(row,['id','code','name','material_type','active'])


@router.post('/spaces/resolve-map')
def resolve_space_map(payload: dict=Body(...),current=Depends(require_module_permission('floresvolcan:spaces:manage'))):
    from services.map_location import resolve_map_location
    try: result=resolve_map_location(str(payload.get('url') or ''))
    except ValueError as exc: raise HTTPException(422,str(exc))
    return {'latitude':result.latitude,'longitude':result.longitude,'google_maps_url':result.canonical_url}


from .field_access import public_router
router.include_router(public_router)
from .business_agent import agent_router
router.include_router(agent_router)


@router.post('/purchase-orders/{po_id}/transit')
def mark_purchase_transit(po_id:int,payload:dict=Body(...),current=Depends(require_module_permission('floresvolcan:purchases:approve')),db:Session=Depends(get_db)):
    row=db.query(PurchaseOrder).filter(PurchaseOrder.id==po_id).with_for_update().first()
    if not row or row.status!='in_process':raise HTTPException(409,'La compra debe estar aprobada y pendiente de recibir')
    try:paid=date.fromisoformat(str(payload['paid_at']));shipped=date.fromisoformat(str(payload['shipped_at']))
    except (ValueError,KeyError):raise HTTPException(422,'Indique fechas de pago y despacho válidas')
    if paid>shipped or shipped>datetime.now(LOCAL_TZ).date():raise HTTPException(422,'Las fechas de pago y despacho no coinciden con el tránsito')
    row.paid_at=paid;row.shipped_at=shipped
    db.add(PurchaseOrderEvent(purchase_order_id=row.id,user_id=current.id,event_type='shipping_update',message=f'Pago registrado: {paid}. Despacho: {shipped}.',event_date=now()))
    db.commit();return {'ok':True,'paid_at':paid,'shipped_at':shipped}

from .public_trace import router as trace_router
router.include_router(trace_router)
