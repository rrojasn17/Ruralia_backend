"""Pasaporte público de cultivo, independiente del acceso de la cuadrilla."""

import json
import secrets
from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy.orm import Session
from database import get_db
from core.models import AppModule
from core.module_manager import require_module_permission
from .models import (
    ModuleSetting,
    PlantingOrder,
    Requisition,
    RequisitionLine,
    InventoryLot,
    GoodsReceiptLine,
    GoodsReceipt,
    PurchaseOrder,
    Supplier,
    SapProduct,
    PlantingRecord,
    ProductionBatch,
    HarvestRecord,
    ProductionSpace,
)

router = APIRouter()


def trace_info(db, order_id, create=False):
    row = db.query(ModuleSetting).filter_by(key=f"trace_order.{order_id}").first()
    if row:
        return json.loads(row.value_text)
    if not create:
        return None
    data = {"token": secrets.token_urlsafe(32), "notes": ""}
    db.add(ModuleSetting(key=f"trace_order.{order_id}", value_text=json.dumps(data)))
    db.add(ModuleSetting(key=f"trace_token.{data['token']}", value_text=str(order_id)))
    return data


@router.post("/planting-orders/{order_id}/trace-link")
def create_link(
    order_id: int,
    current=Depends(require_module_permission("floresvolcan:planting:manage")),
    db: Session = Depends(get_db),
):
    order = db.query(PlantingOrder).filter_by(id=order_id).with_for_update().first()
    if not order:
        raise HTTPException(404, "Siembra no encontrada")
    result = trace_info(db, order_id, True)
    db.commit()
    return result


class TraceNotes(BaseModel):
    notes: str = Field(max_length=2000)


@router.put("/planting-orders/{order_id}/trace-notes")
def update_notes(
    order_id: int,
    payload: TraceNotes,
    current=Depends(require_module_permission("floresvolcan:planting:manage")),
    db: Session = Depends(get_db),
):
    order = db.query(PlantingOrder).filter_by(id=order_id).with_for_update().first()
    if not order:
        raise HTTPException(404, "Siembra no encontrada")
    data = trace_info(db, order_id, True)
    db.flush()
    data["notes"] = payload.notes.strip()
    data["edited_by"] = current.id
    row = db.query(ModuleSetting).filter_by(key=f"trace_order.{order_id}").one()
    row.value_text = json.dumps(data, ensure_ascii=False)
    db.commit()
    return {"notes": data["notes"]}


@router.get("/public/trace/{token}")
def public_trace(token: str, response: Response, db: Session = Depends(get_db)):
    if not 20 <= len(token) <= 100:
        raise HTTPException(404, "Pasaporte no encontrado")
    module = db.query(AppModule).filter_by(key="mod_floresvolcan").first()
    if module and module.status != "active":
        raise HTTPException(404, "Pasaporte no disponible")
    pointer = db.query(ModuleSetting).filter_by(key=f"trace_token.{token}").first()
    order = db.get(PlantingOrder, int(pointer.value_text)) if pointer else None
    data = trace_info(db, order.id) if order else None
    if not order or not data or not secrets.compare_digest(data["token"], token):
        raise HTTPException(404, "Pasaporte no encontrado")
    origins = (
        db.query(RequisitionLine, InventoryLot, SapProduct, PurchaseOrder, Supplier)
        .join(Requisition, Requisition.id == RequisitionLine.requisition_id)
        .join(InventoryLot, InventoryLot.id == RequisitionLine.inventory_lot_id)
        .join(SapProduct, SapProduct.id == InventoryLot.sap_product_id)
        .join(
            GoodsReceiptLine, GoodsReceiptLine.id == InventoryLot.goods_receipt_line_id
        )
        .join(GoodsReceipt, GoodsReceipt.id == GoodsReceiptLine.goods_receipt_id)
        .join(PurchaseOrder, PurchaseOrder.id == GoodsReceipt.purchase_order_id)
        .join(Supplier, Supplier.id == PurchaseOrder.supplier_id)
        .filter(
            Requisition.planting_order_id == order.id, Requisition.status != "cancelled"
        )
        .all()
    )
    origins = list(
        {
            lot.id: (line, lot, product, po, supplier)
            for line, lot, product, po, supplier in origins
        }.values()
    )
    from .material_defaults import COLOR_NAMES

    materials = [
        {
            "lot": lot.supplier_lot_code or lot.lot_code,
            "variety": lot.variety_snapshot or product.variety or product.description,
            "color": COLOR_NAMES.get(
                lot.color_snapshot or product.color, lot.color_snapshot or product.color
            ),
            "common": lot.common_code_snapshot or product.common_code,
            "supplier": supplier.name,
            "country": supplier.country,
            "purchase_date": po.created_at,
            "received_at": lot.received_at,
        }
        for _, lot, product, po, supplier in origins
    ]
    planted = (
        db.query(
            func.coalesce(func.sum(PlantingRecord.quantity_planted), 0),
            func.min(PlantingRecord.planted_at),
        )
        .filter(PlantingRecord.planting_order_id == order.id)
        .one()
    )
    harvested = (
        db.query(
            func.coalesce(func.sum(HarvestRecord.total_output), 0),
            func.min(HarvestRecord.harvested_at),
            func.max(HarvestRecord.harvested_at),
        )
        .join(ProductionBatch, ProductionBatch.id == HarvestRecord.production_batch_id)
        .filter(
            ProductionBatch.planting_order_id == order.id,
            HarvestRecord.voided_at.is_(None),
        )
        .one()
    )
    batches = (
        db.query(ProductionBatch.status).filter_by(planting_order_id=order.id).all()
    )
    beds = (
        db.query(PlantingRecord.bed_space_id, func.sum(PlantingRecord.quantity_planted))
        .filter_by(planting_order_id=order.id)
        .group_by(PlantingRecord.bed_space_id)
        .all()
    )
    by_id = {}
    missing = {order.production_space_id} | {bed_id for bed_id, _ in beds}
    while missing:
        spaces = db.query(ProductionSpace).filter(ProductionSpace.id.in_(missing)).all()
        by_id.update({s.id: s for s in spaces})
        missing = {
            s.parent_id for s in spaces if s.parent_id and s.parent_id not in by_id
        }

    def location(space_id):
        chain = []
        seen = set()
        while space_id in by_id and space_id not in seen:
            seen.add(space_id)
            s = by_id[space_id]
            chain.append({"name": s.name, "code": s.code, "type": s.type})
            space_id = s.parent_id
        return list(reversed(chain))

    stage = (
        "closed"
        if batches and all(s[0] == "closed" for s in batches)
        else "harvesting"
        if harvested[1]
        else "growing"
        if planted[1]
        else "approved"
        if order.status in {"approved", "in_progress", "completed"}
        else "preparation"
    )
    if order.status == "cancelled":
        stage = "cancelled"
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Robots-Tag"] = "noindex, nofollow"
    return {
        "code": f"OS-{order.id:08d}",
        "stage": stage,
        "prepared_at": order.created_at,
        "planned_date": order.planned_start_date,
        "approved_at": order.approved_at,
        "first_planting": planted[1],
        "first_harvest": harvested[1],
        "last_harvest": harvested[2],
        "planned_quantity": order.planned_quantity,
        "planted": planted[0],
        "harvested": harvested[0],
        "materials": materials,
        "location": location(order.production_space_id),
        "beds": [
            {"location": location(bed_id), "planted": qty} for bed_id, qty in beds
        ],
        "notes": data.get("notes", ""),
    }
