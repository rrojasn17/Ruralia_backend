"""Resumen y bitácora de una orden, sin confundir fecha de trabajo y registro."""

from decimal import Decimal
from .production_control import local_date
from sqlalchemy import func
from core.models import Usuario
from .models import (
    ProductionSpace,
    PlantingRecord,
    InventoryMovement,
    Worker,
    Requisition,
    RequisitionLine,
    InventoryLot,
    SapProduct,
)


def order_trace(db, order, beds, page=1, page_size=40):
    totals = dict(
        db.query(
            PlantingRecord.requisition_line_id,
            func.sum(PlantingRecord.quantity_planted),
        )
        .filter(PlantingRecord.planting_order_id == order.id)
        .group_by(PlantingRecord.requisition_line_id)
    )
    locations = dict(
        db.query(PlantingRecord.bed_space_id, func.sum(PlantingRecord.quantity_planted))
        .filter(PlantingRecord.planting_order_id == order.id)
        .group_by(PlantingRecord.bed_space_id)
    )
    spaces = {}
    missing = {b["id"] for b in beds} | set(locations) | {order.production_space_id}
    while missing:
        rows = db.query(ProductionSpace).filter(ProductionSpace.id.in_(missing)).all()
        spaces.update({s.id: s for s in rows})
        missing = {
            s.parent_id for s in rows if s.parent_id and s.parent_id not in spaces
        }

    def location(space_id):
        chain, seen = [], set()
        node = spaces.get(space_id)
        while node and node.id not in seen:
            seen.add(node.id)
            chain.append(
                {"id": node.id, "code": node.code, "name": node.name, "type": node.type}
            )
            node = spaces.get(node.parent_id)
        chain.reverse()
        return {
            "path": chain,
            "farm": next((s for s in chain if s["type"] == "farm"), None),
            "greenhouse": next((s for s in chain if s["type"] == "greenhouse"), None),
            "bed": next(
                (s for s in reversed(chain) if s["type"] in {"bed", "table", "row"}),
                None,
            ),
        }

    query = db.query(PlantingRecord).filter(
        PlantingRecord.planting_order_id == order.id
    )
    total = query.count()
    records = (
        query.order_by(PlantingRecord.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )
    ids = [r.id for r in records]
    recorded = (
        dict(
            db.query(
                InventoryMovement.reference_id, func.min(InventoryMovement.created_at)
            )
            .filter(
                InventoryMovement.reference_type == "planting_record",
                InventoryMovement.movement_type == "PLANTING_CONSUMPTION",
                InventoryMovement.reference_id.in_(ids),
            )
            .group_by(InventoryMovement.reference_id)
        )
        if ids
        else {}
    )
    workers = {
        w.id: w
        for w in db.query(Worker).filter(
            Worker.id.in_({r.worker_id for r in records if r.worker_id})
        )
    }
    users = {
        u.id: u.nombre
        for u in db.query(Usuario).filter(
            Usuario.id.in_({r.operator_user_id for r in records if r.operator_user_id})
        )
    }
    lines = (
        db.query(RequisitionLine, InventoryLot, SapProduct, Requisition.status)
        .join(Requisition, Requisition.id == RequisitionLine.requisition_id)
        .join(InventoryLot, InventoryLot.id == RequisitionLine.inventory_lot_id)
        .join(SapProduct, SapProduct.id == RequisitionLine.sap_product_id)
        .filter(Requisition.planting_order_id == order.id)
        .all()
    )
    origins = {}
    for line, lot, product, status in lines:
        planted = totals.get(line.id, Decimal(0))
        origins[line.id] = {
            "requisition_line_id": line.id,
            "lot_code": lot.lot_code,
            "supplier_lot_code": lot.supplier_lot_code,
            "container_number": lot.container_number,
            "product_code": product.code,
            "variety": lot.variety_snapshot or product.variety or product.description,
            "requested": line.quantity_requested,
            "delivered": line.quantity_delivered,
            "planted": planted,
            "returned": line.quantity_returned,
            "field_loss": line.quantity_field_loss,
            "pending_to_plant": max(
                Decimal(0),
                line.quantity_delivered
                - planted
                - line.quantity_returned
                - line.quantity_field_loss,
            ),
            "cancelled": status == "cancelled",
        }
    planted = sum(totals.values(), Decimal(0))
    active = [x for x in origins.values() if not x["cancelled"]]
    return {
        "summary": {
            "planned": order.planned_quantity,
            "planted": planted,
            "remaining_planned": max(
                Decimal(0), Decimal(order.planned_quantity) - planted
            )
            if order.planned_quantity is not None
            else None,
            "delivered": sum((x["delivered"] for x in active), Decimal(0)),
            "pending_delivered": sum(
                (x["pending_to_plant"] for x in active), Decimal(0)
            ),
            "returned": sum((x["returned"] for x in active), Decimal(0)),
            "field_loss": sum((x["field_loss"] for x in active), Decimal(0)),
            "records_count": total,
            "beds_count": len(locations),
        },
        "source_lots": list(origins.values()),
        "locations": [
            {"bed_id": bed_id, "planted": qty, **location(bed_id)}
            for bed_id, qty in locations.items()
        ],
        "beds": [{**b, "location": location(b["id"])} for b in beds],
        "records": [
            {
                "id": r.id,
                "quantity_planted": r.quantity_planted,
                "worker_name": workers[r.worker_id].name
                if r.worker_id in workers
                else None,
                "registered_by_name": users.get(r.operator_user_id),
                "recorded_at": recorded.get(r.id),
                "planted_at": r.planted_at,
                "work_date": local_date(r.planted_at),
                "notes": r.notes,
                "side": r.side,
                "source": origins.get(r.requisition_line_id),
                "location": location(r.bed_space_id),
            }
            for r in records
        ],
        "records_total": total,
        "records_page": page,
        "records_page_size": page_size,
    }
