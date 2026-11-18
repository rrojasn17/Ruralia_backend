"""Reglas compartidas de saldo, identidad y ocupación física de Flores Volcán."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from sqlalchemy import func

from .models import (
    BedCycle,
    HarvestRecord,
    HarvestOrder,
    PlantingRecord,
    ProductionBatch,
    ProductionSpace,
    SegmentReconciliation,
    HistoricalOverageApproval,
    Supplier,
    PurchaseOrder,
    GoodsReceipt,
    GoodsReceiptLine,
)

LOCAL_TZ = ZoneInfo("America/Costa_Rica")
ZERO = Decimal("0")


def decimal(value):
    return Decimal(str(value or 0))


def local_date(value):
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)  # registros persistidos en UTC
    return value.astimezone(LOCAL_TZ).date()


def operation_time(value):
    if value is None:
        raise HTTPException(422, "Indique la fecha real del trabajo")
    if value.tzinfo is None:
        value = value.replace(
            tzinfo=LOCAL_TZ
        )  # fecha manual sin offset: hora de Costa Rica
    value = value.astimezone(timezone.utc)
    if local_date(value) > datetime.now(LOCAL_TZ).date():
        raise HTTPException(422, "La fecha real no puede estar en el futuro")
    return value


def request_hash(payload):
    def canonical(value):
        if isinstance(value, Decimal):
            return format(value.normalize(), "f")
        if isinstance(value, datetime):
            return (
                value.astimezone(timezone.utc).isoformat()
                if value.tzinfo
                else value.replace(tzinfo=LOCAL_TZ).astimezone(timezone.utc).isoformat()
            )
        if isinstance(value, dict):
            return {k: canonical(v) for k, v in value.items()}
        if isinstance(value, list):
            return [canonical(v) for v in value]
        return value

    data = payload.model_dump(exclude={"client_token"})
    if data.get("worker_code") is None:
        data.pop("worker_code", None)
    return hashlib.sha256(
        json.dumps(canonical(data), sort_keys=True, default=str).encode()
    ).hexdigest()


def check_retry(db, model, payload, user):
    if not payload.client_token or len(payload.client_token.strip()) < 8:
        raise HTTPException(
            422, "Se requiere un identificador de registro de al menos 8 caracteres"
        )
    existing = (
        db.query(model).filter(model.client_token == payload.client_token).first()
    )
    if existing:
        if getattr(existing, "voided_at", None):
            raise HTTPException(
                409,
                "El registro original fue anulado; consulte su motivo antes de capturar otro evento",
            )
        if existing.operator_user_id != user.id and not user.is_superadmin:
            raise HTTPException(409, "El identificador pertenece a otro trabajador")
        if not existing.request_hash or existing.request_hash != request_hash(payload):
            raise HTTPException(
                409,
                "Este identificador ya fue usado con otros datos; consulte el registro original",
            )
    return existing


def pending_to_plant(line):
    return (
        decimal(line.quantity_delivered)
        - decimal(line.quantity_planted)
        - decimal(line.quantity_returned)
        - decimal(line.quantity_field_loss)
    )


def pending_delivery(line):
    return (
        decimal(line.quantity_approved)
        - decimal(line.quantity_delivered)
        - decimal(line.quantity_released)
    )


def lock_bed(db, bed_id):
    bed = (
        db.query(ProductionSpace)
        .filter(ProductionSpace.id == bed_id)
        .populate_existing()
        .with_for_update()
        .first()
    )
    if not bed:
        raise HTTPException(409, "La cama del lote ya no existe")
    return bed


def lock_batch(db, batch_id):
    batch = db.query(ProductionBatch).filter(ProductionBatch.id == batch_id).first()
    if not batch:
        raise HTTPException(404, "Lote productivo no encontrado")
    lock_bed(db, batch.bed_space_id)
    return (
        db.query(ProductionBatch)
        .filter(ProductionBatch.id == batch_id)
        .populate_existing()
        .with_for_update()
        .first()
    )


def bed_planting_availability(db, beds):
    ids = [b.id for b in beds]
    cycles = (
        db.query(BedCycle)
        .filter(BedCycle.bed_space_id.in_(ids), BedCycle.status != "cleaned")
        .all()
        if ids
        else []
    )
    cut_cycles = (
        {
            r[0]
            for r in db.query(ProductionBatch.bed_cycle_id)
            .join(
                HarvestRecord, HarvestRecord.production_batch_id == ProductionBatch.id
            )
            .filter(
                ProductionBatch.bed_cycle_id.in_([c.id for c in cycles]),
                HarvestRecord.voided_at.is_(None),
            )
            .distinct()
        }
        if cycles
        else set()
    )
    labels = {
        "harvesting": "cosecha",
        "reconciliation": "conciliación",
        "pending_cleaning": "limpieza pendiente",
    }
    result = {
        b.id: {
            "planting_allowed": True,
            "planting_block_reason": None,
            "bed_cycle_id": None,
        }
        for b in beds
    }
    for cycle in cycles:
        if cycle.status != "growing" or cycle.id in cut_cycles:
            result[cycle.bed_space_id] = {
                "planting_allowed": False,
                "bed_cycle_id": cycle.id,
                "planting_block_reason": f"Ciclo #{cycle.id} en {labels.get(cycle.status, 'cosecha')}. El encargado debe conciliar sus lotes y confirmar la limpieza antes de una nueva siembra.",
            }
    return result


def planting_cycle(db, bed):
    # El llamador conserva el lock de la cama durante validación y escritura.
    cycle = (
        db.query(BedCycle)
        .filter(BedCycle.bed_space_id == bed.id, BedCycle.status != "cleaned")
        .first()
    )
    has_cuts = (
        cycle
        and db.query(HarvestRecord.id)
        .join(ProductionBatch, ProductionBatch.id == HarvestRecord.production_batch_id)
        .filter(
            ProductionBatch.bed_cycle_id == cycle.id, HarvestRecord.voided_at.is_(None)
        )
        .first()
    )
    if cycle and (cycle.status != "growing" or has_cuts):
        raise HTTPException(
            409,
            f"La cama {bed.code} tiene el ciclo #{cycle.id} pendiente. El encargado debe conciliar sus lotes y confirmar la limpieza en Cosecha → Limpieza de mesas antes de una nueva siembra.",
        )
    if not cycle:
        cycle = BedCycle(bed_space_id=bed.id, status="growing")
        db.add(cycle)
        db.flush()
    return cycle


def segment_metrics(segment, batch, cuts, reconciliation):
    output = sum((decimal(c.total_output) for c in cuts), ZERO)
    sale = sum((decimal(c.quantity_harvested) for c in cuts), ZERO)
    discarded = sum((decimal(c.discard_qty) for c in cuts), ZERO)
    expected = (
        decimal(segment.quantity_planted) * decimal(batch.expected_stems_per_bulb or 1)
    ).quantize(Decimal("0.001"))
    loss = decimal(reconciliation.loss_quantity) if reconciliation else ZERO
    eta = local_date(segment.planted_at) + timedelta(
        weeks=int(segment.cycle_weeks or batch.cycle_weeks or 0)
    )
    return {
        "id": segment.id,
        "quantity_planted": decimal(segment.quantity_planted),
        "planted_at": segment.planted_at,
        "side": segment.side,
        "bed_space_id": segment.bed_space_id,
        "inventory_lot_id": segment.inventory_lot_id,
        "planting_week": segment.planting_week,
        "projected_harvest_week": segment.projected_harvest_week,
        "cycle_weeks": segment.cycle_weeks,
        "harvest_due_date": eta,
        "expected_stems": expected,
        "harvested": output,
        "sale": sale,
        "discarded": discarded,
        "loss_quantity": loss,
        "loss_reason": reconciliation.loss_reason if reconciliation else None,
        "reconciled": reconciliation is not None,
        "remaining": max(ZERO, expected - output - loss),
        "overage": max(ZERO, output - expected),
        "difference": max(ZERO, expected - output) - loss,
        "raw_difference": expected - output,
        "cut_events": len(cuts),
    }


def batch_segments(db, batch, loaded=None):
    if loaded is not None:
        records, cuts, reconciliations, _ = loaded
        return [
            segment_metrics(
                r,
                batch,
                [c for c in cuts if c.planting_record_id == r.id],
                reconciliations.get(r.id),
            )
            for r in records
        ]
    records = (
        db.query(PlantingRecord)
        .filter(PlantingRecord.production_batch_id == batch.id)
        .order_by(PlantingRecord.planted_at, PlantingRecord.id)
        .all()
    )
    cuts_by_segment = {}
    for cut in (
        db.query(HarvestRecord)
        .filter(
            HarvestRecord.production_batch_id == batch.id,
            HarvestRecord.voided_at.is_(None),
        )
        .all()
    ):
        cuts_by_segment.setdefault(cut.planting_record_id, []).append(cut)
    reconciliations = {
        r.planting_record_id: r
        for r in db.query(SegmentReconciliation)
        .join(
            PlantingRecord,
            PlantingRecord.id == SegmentReconciliation.planting_record_id,
        )
        .filter(PlantingRecord.production_batch_id == batch.id)
        .all()
    }
    return [
        segment_metrics(
            record,
            batch,
            cuts_by_segment.get(record.id, []),
            reconciliations.get(record.id),
        )
        for record in records
    ]


def batch_metrics(db, batch, loaded=None):
    segments = batch_segments(db, batch, loaded)
    cuts = (
        loaded[1]
        if loaded is not None
        else (
            db.query(HarvestRecord)
            .filter(
                HarvestRecord.production_batch_id == batch.id,
                HarvestRecord.voided_at.is_(None),
            )
            .all()
        )
    )
    planted = sum((s["quantity_planted"] for s in segments), ZERO)
    expected = sum((s["expected_stems"] for s in segments), ZERO)
    sale = sum((decimal(c.quantity_harvested) for c in cuts), ZERO)
    output = sum((decimal(c.total_output) for c in cuts), ZERO)
    discard = sum((decimal(c.discard_qty) for c in cuts), ZERO)
    loss = sum((s["loss_quantity"] for s in segments), ZERO)
    return {
        "history_review_required": bool(batch.history_review_required),
        "planted": planted,
        "harvested": output,
        "sale": sale,
        "discarded": discard,
        "total_planted": planted,
        "total_sale": sale,
        "total_discarded": discard,  # compatibilidad UI anterior
        "expected_stems": expected,
        "loss_quantity": loss,
        "remaining": sum((s["remaining"] for s in segments), ZERO),
        "overage": sum((s["overage"] for s in segments), ZERO),
        "difference": sum((s["difference"] for s in segments), ZERO),
        "raw_difference": expected - output,
        "yield_percentage": output / expected * 100 if expected else ZERO,
        "sale_yield_percentage": sale / expected * 100 if expected else ZERO,
        "stems_per_bulb": output / planted if planted else ZERO,
        "bloom_1": sum((decimal(c.bloom_1_qty) for c in cuts), ZERO),
        "bloom_2": sum((decimal(c.bloom_2_qty) for c in cuts), ZERO),
        "bloom_3_5": sum((decimal(c.bloom_3_5_qty) for c in cuts), ZERO),
        "cut_events": len(cuts),
        "unlinked_cuts": sum(
            c.planting_record_id not in {s["id"] for s in segments} for c in cuts
        ),
        "unreconciled_segments": sum(not s["reconciled"] for s in segments),
        "authorized_overage": loaded[3]
        if loaded is not None
        else authorized_overage(db, batch.id),
    }


def require_linked_history(db, batch):
    if batch.history_review_required:
        raise HTTPException(
            409,
            "Lote histórico de exploración: revise los datos de Excel, unidades y registros pendientes antes de incorporar nuevas cortas o cerrar",
        )
    if batch_metrics(db, batch)["unlinked_cuts"]:
        raise HTTPException(
            409,
            "Hay cortas históricas sin una siembra válida; asigne su segmento antes de continuar",
        )


def occupied_area(db, cycle):
    return decimal(
        db.query(func.coalesce(func.sum(PlantingRecord.area_m2), 0))
        .join(ProductionBatch, ProductionBatch.id == PlantingRecord.production_batch_id)
        .filter(ProductionBatch.bed_cycle_id == cycle.id)
        .scalar()
    )


def authorized_overage(db, batch_id):
    orders = (
        db.query(func.coalesce(func.sum(HarvestOrder.overage_quantity), 0))
        .filter(
            HarvestOrder.production_batch_id == batch_id,
            HarvestOrder.status.in_(["approved", "in_progress", "completed"]),
            HarvestOrder.allow_overage.is_(True),
        )
        .scalar()
    )
    historical = (
        db.query(func.coalesce(func.sum(HistoricalOverageApproval.quantity), 0))
        .filter(HistoricalOverageApproval.production_batch_id == batch_id)
        .scalar()
    )
    return decimal(orders) + decimal(historical)


def source_supplier(db, lot):
    if not lot:
        return None
    supplier = (
        db.query(Supplier)
        .join(PurchaseOrder, PurchaseOrder.supplier_id == Supplier.id)
        .join(GoodsReceipt, GoodsReceipt.purchase_order_id == PurchaseOrder.id)
        .join(GoodsReceiptLine, GoodsReceiptLine.goods_receipt_id == GoodsReceipt.id)
        .filter(GoodsReceiptLine.id == lot.goods_receipt_line_id)
        .first()
    )
    return (
        {"id": supplier.id, "code": supplier.code, "name": supplier.name}
        if supplier
        else None
    )


def bulk_batch_metrics(db, batches):
    """Five queries per collection, preserving segment-level balance rules."""
    ids = [b.id for b in batches]
    if not ids:
        return {}
    records = {i: [] for i in ids}
    cuts = {i: [] for i in ids}
    reconciliations = {}
    allowances = {}
    for r in (
        db.query(PlantingRecord)
        .filter(PlantingRecord.production_batch_id.in_(ids))
        .order_by(PlantingRecord.planted_at, PlantingRecord.id)
    ):
        records[r.production_batch_id].append(r)
    for c in db.query(HarvestRecord).filter(
        HarvestRecord.production_batch_id.in_(ids), HarvestRecord.voided_at.is_(None)
    ):
        cuts[c.production_batch_id].append(c)
    for r in (
        db.query(SegmentReconciliation)
        .join(
            PlantingRecord,
            PlantingRecord.id == SegmentReconciliation.planting_record_id,
        )
        .filter(PlantingRecord.production_batch_id.in_(ids))
    ):
        reconciliations[r.planting_record_id] = r
    for bid, qty in (
        db.query(
            HarvestOrder.production_batch_id, func.sum(HarvestOrder.overage_quantity)
        )
        .filter(
            HarvestOrder.production_batch_id.in_(ids),
            HarvestOrder.status.in_(["approved", "in_progress", "completed"]),
            HarvestOrder.allow_overage.is_(True),
        )
        .group_by(HarvestOrder.production_batch_id)
    ):
        allowances[bid] = decimal(qty)
    for bid, qty in (
        db.query(
            HistoricalOverageApproval.production_batch_id,
            func.sum(HistoricalOverageApproval.quantity),
        )
        .filter(HistoricalOverageApproval.production_batch_id.in_(ids))
        .group_by(HistoricalOverageApproval.production_batch_id)
    ):
        allowances[bid] = allowances.get(bid, ZERO) + decimal(qty)
    return {
        b.id: batch_metrics(
            db,
            b,
            (records[b.id], cuts[b.id], reconciliations, allowances.get(b.id, ZERO)),
        )
        for b in batches
    }
