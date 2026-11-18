"""Completa salidas pendientes de preparaciones ya aprobadas. Vista previa por defecto."""

import argparse
import json
from decimal import Decimal
from core.models import Usuario
from database import SessionLocal
from .models import ModuleSetting, PlantingOrder, Requisition, RequisitionLine
from .production_control import pending_delivery
from .planting_preparation import verify_materials
from .router import _deliver_prepared_material


def run(apply=False):
    with SessionLocal() as db:
        actor = (
            db.query(Usuario)
            .filter(Usuario.is_superadmin.is_(True), Usuario.activo.is_(True))
            .order_by(Usuario.id)
            .first()
        )
        if not actor:
            raise RuntimeError(
                "No hay superusuario activo para auditar la actualización"
            )
        result = []
        for setting in (
            db.query(ModuleSetting)
            .filter(ModuleSetting.key.like("planting_plan.%"))
            .all()
        ):
            plan = json.loads(setting.value_text)
            order = (
                db.query(PlantingOrder)
                .filter_by(id=int(setting.key.split(".")[-1]))
                .with_for_update()
                .first()
            )
            if not order or order.status not in {"approved", "in_progress"}:
                continue
            req = (
                db.query(Requisition)
                .filter_by(id=plan["requisition_id"], planting_order_id=order.id)
                .with_for_update()
                .first()
            )
            if (
                not req
                or req.status not in {"approved", "partially_delivered"}
                or not req.approved_by_id
            ):
                continue
            lines = db.query(RequisitionLine).filter_by(requisition_id=req.id).all()
            qty = sum(
                (max(Decimal(0), pending_delivery(line)) for line in lines), Decimal(0)
            )
            if not qty:
                continue
            verify_materials(db, plan, req)
            result.append(
                {"order_id": order.id, "requisition_id": req.id, "quantity": str(qty)}
            )
            if apply:
                _deliver_prepared_material(
                    db,
                    req,
                    actor,
                    comments="Salida automática al unificar el flujo de una preparación previamente aprobada",
                )
        if apply:
            db.commit()
        else:
            db.rollback()
        return {"applied": apply, "orders": result}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    print(json.dumps(run(parser.parse_args().apply), ensure_ascii=False))
