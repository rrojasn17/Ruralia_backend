"""Preparación de material disponible y aprobación única asignada a gerencia."""

from datetime import datetime, timezone
from decimal import Decimal
import pytest
from core.models import Usuario, ModuleUserRole
from core.routers.auth import get_current_user
from modules.mod_floresvolcan import models as m
from test_floresvolcan_reconciliation_v07 import fv as fv_fixture, value
from test_floresvolcan_module_beds_v10 import make_area
from test_floresvolcan_roles_v09 import activate


fv = fv_fixture


def setup_plan(fv):
    crop, farm, greenhouse, beds = make_area(fv)
    manager = Usuario(
        nombre="Gerente asignado",
        correo="manager-plan@example.com",
        hash_contrasena="test-only",
        rol="gerente",
        activo=True,
    )
    other = Usuario(
        nombre="Otro gerente",
        correo="other-plan@example.com",
        hash_contrasena="test-only",
        rol="gerente",
        activo=True,
    )
    fv.db.add_all([manager, other])
    fv.db.flush()
    for user in (manager, other):
        fv.db.add(
            ModuleUserRole(
                usuario_id=user.id, module_key="mod_floresvolcan", role="gerente"
            )
        )
    fv.product.crop_id = crop.id
    fv.lot.color_snapshot = "WH"
    fv.lot.common_code_snapshot = "OT"
    fv.db.commit()
    activate(fv, "admin")
    payload = {
        "production_space_id": farm.id,
        "crop_id": crop.id,
        "bed_space_ids": [bed.id for bed in beds],
        "crew_id": fv.crew.id,
        "planned_quantity": 150,
        "approver_user_id": manager.id,
        "materials": [{"inventory_lot_id": fv.lot.id, "quantity_requested": 150}],
    }
    return manager, other, payload


def login(fv, user):
    fv.client.app.dependency_overrides[get_current_user] = lambda: user


def test_single_assigned_approval_reserves_prefilled_request(fv):
    manager, other, payload = setup_plan(fv)
    row = value(fv.client.post("/floresvolcan/planting-orders", json=payload), 201)
    path = f"/floresvolcan/planting-orders/{row['id']}"
    order = value(fv.client.get(path), 200)
    assert order["planned_quantity"] == 150  # not the full 200-bed capacity
    plan = order["preparation"]
    assert plan["approver_user_id"] == manager.id
    assert plan["materials"][0]["color"] == "WH"
    assert plan["materials"][0]["common_code"] == "OT"
    assert len(order["requisitions"]) == 1
    assert order["requisitions"][0]["status"] == "pending"
    fv.db.refresh(fv.lot)
    assert fv.lot.quantity_available == 3000
    value(fv.client.post(path + "/approve"), 403)
    login(fv, other)
    assert (
        value(fv.client.get("/floresvolcan/management-dashboard"), 200)["kpis"][
            "pending_approvals"
        ]
        == 0
    )
    assert value(fv.client.get(path), 200)["can_approve"] is False
    value(fv.client.post(path + "/approve"), 403)
    login(fv, manager)
    assert (
        value(fv.client.get("/floresvolcan/management-dashboard"), 200)["kpis"][
            "pending_approvals"
        ]
        == 1
    )
    assert value(fv.client.get(path), 200)["can_approve"] is True
    value(fv.client.post(path + "/approve"), 200)
    req = value(
        fv.client.get(f"/floresvolcan/requisitions/{plan['requisition_id']}"), 200
    )
    assert req["status"] == "delivered" and req["approved_by_id"] == manager.id
    assert Decimal(req["lines"][0]["quantity_approved"]) == 150
    order = value(fv.client.get(path), 200)
    assert order["approved_by_id"] == manager.id and order["approved_at"]
    fv.db.refresh(fv.lot)
    assert fv.lot.quantity_available == 2850 and fv.lot.quantity_reserved == 0
    assert fv.lot.quantity_issued == 2150
    assert Decimal(req["lines"][0]["quantity_delivered"]) == 150
    assert Decimal(req["lines"][0]["pending_to_plant"]) == 150
    value(fv.client.post(path + "/approve"), 409)
    assert (
        fv.db.query(m.RequisitionDelivery)
        .filter_by(requisition_line_id=req["lines"][0]["id"])
        .count()
        == 1
    )

    assert (
        fv.db.query(m.InventoryMovement)
        .filter_by(reference_id=plan["requisition_id"], movement_type="RESERVATION")
        .count()
        == 1
    )


def test_rechecks_stock_and_keeps_both_pending_on_failure(fv):
    manager, other, payload = setup_plan(fv)
    row = value(fv.client.post("/floresvolcan/planting-orders", json=payload), 201)
    fv.lot.quantity_available = 100
    fv.db.commit()
    login(fv, manager)
    value(fv.client.post(f"/floresvolcan/planting-orders/{row['id']}/approve"), 409)
    order = value(fv.client.get(f"/floresvolcan/planting-orders/{row['id']}"), 200)
    assert order["status"] == "pending_approval"
    assert order["requisitions"][0]["status"] == "pending"
    fv.db.refresh(fv.lot)
    assert fv.lot.quantity_reserved == 0


@pytest.mark.parametrize("bad", ["manager", "duplicate", "capacity", "quantity"])
def test_rejects_invalid_preparations(fv, bad):
    manager, other, payload = setup_plan(fv)
    if bad == "manager":
        payload["approver_user_id"] = fv.user.id
    if bad == "duplicate":
        payload["materials"] *= 2
    if bad == "capacity":
        payload["planned_quantity"] = 201
        payload["materials"][0]["quantity_requested"] = 201
    if bad == "quantity":
        payload["planned_quantity"] = 149
    value(fv.client.post("/floresvolcan/planting-orders", json=payload), 422)


def test_material_options_and_lots_follow_snapshot_identity(fv):
    manager, other, payload = setup_plan(fv)
    options = value(
        fv.client.get(
            "/floresvolcan/planting/material-options",
            params={"crop_id": payload["crop_id"], "page": 1},
        ),
        200,
    )
    item = options["items"][0]
    assert item["color_label"] == "Blanco" and item["common_code"] == "OT"
    lots = value(
        fv.client.get(
            "/floresvolcan/inventory",
            params={
                "page": 1,
                "available_only": True,
                "sap_product_id": item["sap_product_id"],
                "variety": item["variety"],
                "color": item["color"],
                "common_code": item["common_code"],
            },
        ),
        200,
    )
    assert lots["total"] == 1
    assert lots["items"][0]["id"] == fv.lot.id
    assert (
        value(
            fv.client.get("/floresvolcan/inventory", params={"page": 1, "color": "RE"}),
            200,
        )["total"]
        == 0
    )


def test_changed_material_identity_requires_new_review(fv):
    manager, other, payload = setup_plan(fv)
    row = value(fv.client.post("/floresvolcan/planting-orders", json=payload), 201)
    fv.lot.color_snapshot = "RE"
    fv.db.commit()
    login(fv, manager)
    value(fv.client.post(f"/floresvolcan/planting-orders/{row['id']}/approve"), 409)
    fv.db.refresh(fv.lot)
    assert fv.lot.quantity_available == 3000 and fv.lot.quantity_reserved == 0


def test_existing_approved_preparation_completes_issue_once(fv, monkeypatch):
    from modules.mod_floresvolcan import auto_issue_backfill as migration
    from modules.mod_floresvolcan.router import _reserve_requisition

    manager, other, payload = setup_plan(fv)
    row = value(fv.client.post("/floresvolcan/planting-orders", json=payload), 201)
    detail = value(fv.client.get(f"/floresvolcan/planting-orders/{row['id']}"), 200)
    req = fv.db.get(m.Requisition, detail["preparation"]["requisition_id"])
    order = fv.db.get(m.PlantingOrder, row["id"])
    order.status = "approved"
    order.approved_by_id = manager.id
    _reserve_requisition(fv.db, req, manager)
    fv.db.add(
        Usuario(
            nombre="Auditor sistema",
            correo="audit-auto-issue@example.com",
            hash_contrasena="test-only",
            is_superadmin=True,
            activo=True,
        )
    )
    fv.db.commit()
    monkeypatch.setattr(migration, "SessionLocal", fv.factory)
    assert len(migration.run()["orders"]) == 1
    fv.db.refresh(fv.lot)
    assert fv.lot.quantity_reserved == 150
    assert len(migration.run(apply=True)["orders"]) == 1
    assert migration.run(apply=True)["orders"] == []
    fv.db.refresh(fv.lot)
    assert fv.lot.quantity_reserved == 0 and fv.lot.quantity_issued == 2150


def test_worker_can_register_immediately_after_preparation_approval(fv):
    manager, other, payload = setup_plan(fv)
    worker = fv.db.query(m.Worker).first()
    worker.employee_code = "2468"
    fv.db.commit()
    created = value(fv.client.post("/floresvolcan/planting-orders", json=payload), 201)
    login(fv, manager)
    value(fv.client.post(f"/floresvolcan/planting-orders/{created['id']}/approve"), 200)
    order = value(fv.client.get(f"/floresvolcan/planting-orders/{created['id']}"), 200)
    session = value(
        fv.client.post(
            "/floresvolcan/public/field/login",
            json={
                "operation": "planting",
                "employee_code": "2468",
                "qr_token": order["qr_token"],
            },
        ),
        200,
    )
    task = value(fv.client.get("/floresvolcan/public/field/session"), 200)["tasks"][
        "planting_orders"
    ][0]
    assert float(task["materials"][0]["pending_to_plant"]) == 150
    assert float(task["materials"][0]["pending_delivery"]) == 0
    value(
        fv.client.post(
            "/floresvolcan/public/field/planting",
            headers={"X-FV-CSRF": session["csrf_token"]},
            json={
                "planting_order_id": created["id"],
                "requisition_line_id": task["materials"][0]["requisition_line_id"],
                "bed_space_id": task["bed_options"][0]["id"],
                "quantity_planted": 10,
                "client_token": "auto-issue-worker-test-unique",
                "planted_at": datetime.now(timezone.utc).isoformat(),
            },
        ),
        201,
    )
