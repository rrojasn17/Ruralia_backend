"""Plan de material y aprobador de siembra; snapshot ligado a la orden."""

import json
from decimal import Decimal
from fastapi import HTTPException
from sqlalchemy.orm import joinedload
from core.models import Usuario
from core.module_manager import role_for_module
from .models import (
    InventoryLot,
    ModuleSetting,
    Requisition,
    RequisitionLine,
    FlowerCrop,
)


def is_manager(db, user):
    return bool(
        user
        and user.activo
        and (
            user.is_superadmin
            or role_for_module(db, user, "mod_floresvolcan") == "gerente"
        )
    )


def read_plan(db, order_id):
    row = db.query(ModuleSetting).filter_by(key=f"planting_plan.{order_id}").first()
    return json.loads(row.value_text) if row else None


def validate_plan(db, payload, crop_id, current):
    if not payload.materials and not payload.approver_user_id:
        if (
            current.is_superadmin
        ):  # Compatibility for existing administrative integrations.
            return None
        raise HTTPException(
            422, "Seleccione material disponible y el gerente aprobador"
        )
    manager = (
        db.get(Usuario, payload.approver_user_id) if payload.approver_user_id else None
    )
    if not is_manager(db, manager):
        raise HTTPException(
            422, "El aprobador debe ser un gerente activo o superusuario"
        )
    if not payload.materials:
        raise HTTPException(422, "Seleccione al menos un lote disponible")
    ids = [line.inventory_lot_id for line in payload.materials]
    if len(set(ids)) != len(ids):
        raise HTTPException(422, "Agrupe la cantidad del mismo lote en una sola línea")
    lots = {
        lot.id: lot
        for lot in db.query(InventoryLot)
        .options(joinedload(InventoryLot.product))
        .filter(InventoryLot.id.in_(ids))
        .all()
    }
    from .material_defaults import COLOR_NAMES

    default_crop = db.query(FlowerCrop).filter_by(code="LIRIO").first()
    materials = []
    for line in payload.materials:
        lot = lots.get(line.inventory_lot_id)
        if not lot or not lot.product.active:
            raise HTTPException(422, "El material no existe o está inactivo")
        material_crop_id = lot.product.crop_id or (
            default_crop.id if default_crop else None
        )
        if material_crop_id != crop_id:
            raise HTTPException(
                422, "El material no corresponde al cultivo seleccionado"
            )
        if line.quantity_requested > lot.quantity_available:
            raise HTTPException(409, f"Saldo insuficiente en {lot.lot_code}")
        materials.append(
            {
                "inventory_lot_id": lot.id,
                "sap_product_id": lot.sap_product_id,
                "product_code": lot.product.code,
                "lot_code": lot.lot_code,
                "variety": lot.variety_snapshot
                or lot.product.variety
                or lot.product.description,
                "color": lot.color_snapshot or lot.product.color,
                "color_label": COLOR_NAMES.get(
                    lot.color_snapshot or lot.product.color,
                    lot.color_snapshot or lot.product.color,
                ),
                "common_code": lot.common_code_snapshot or lot.product.common_code,
                "quantity_requested": str(line.quantity_requested),
            }
        )
    total = sum((Decimal(x["quantity_requested"]) for x in materials), Decimal(0))
    if payload.planned_quantity is not None and total != payload.planned_quantity:
        raise HTTPException(
            422, "El total de material debe coincidir con la cantidad planificada"
        )
    return {
        "approver_user_id": manager.id,
        "approver_name": manager.nombre,
        "requested_by_id": current.id,
        "materials": materials,
        "total": str(total),
    }


def attach_plan(db, order, plan, number):
    request = Requisition(
        number=number,
        planting_order_id=order.id,
        requested_by_id=order.requested_by_id,
        comments="Material definido en la preparación de siembra",
    )
    request.lines = [
        RequisitionLine(
            inventory_lot_id=m["inventory_lot_id"],
            sap_product_id=m["sap_product_id"],
            quantity_requested=Decimal(m["quantity_requested"]),
            quantity_approved=0,
            quantity_delivered=0,
            quantity_planted=0,
        )
        for m in plan["materials"]
    ]
    db.add(request)
    db.flush()
    plan["requisition_id"] = request.id
    plan["requisition_number"] = request.number
    db.add(
        ModuleSetting(
            key=f"planting_plan.{order.id}",
            value_text=json.dumps(plan, ensure_ascii=False),
        )
    )


def require_approver(db, current, plan=None):
    if not is_manager(db, current):
        raise HTTPException(403, "La preparación de siembra la aprueba gerencia")
    if plan and not current.is_superadmin and plan["approver_user_id"] != current.id:
        raise HTTPException(403, "Esta preparación está asignada a otro gerente")


def verify_materials(db, plan, request):
    """Reject changed identity or request lines before any inventory movement."""
    expected = {
        m["inventory_lot_id"]: Decimal(m["quantity_requested"])
        for m in plan["materials"]
    }
    actual = {line.inventory_lot_id: line.quantity_requested for line in request.lines}
    if actual != expected or len(request.lines) != len(expected):
        raise HTTPException(409, "La salida no coincide con el material preparado")
    for material in sorted(plan["materials"], key=lambda m: m["inventory_lot_id"]):
        lot = (
            db.query(InventoryLot)
            .filter_by(id=material["inventory_lot_id"])
            .with_for_update()
            .first()
        )
        if not lot or not lot.product.active:
            raise HTTPException(409, "El material preparado ya no está activo")
        identity = {
            "sap_product_id": lot.sap_product_id,
            "variety": lot.variety_snapshot
            or lot.product.variety
            or lot.product.description,
            "color": lot.color_snapshot or lot.product.color,
            "common_code": lot.common_code_snapshot or lot.product.common_code,
        }
        if any(material.get(k) != v for k, v in identity.items()):
            raise HTTPException(
                409, "Cambió la identificación del material; revise la preparación"
            )
