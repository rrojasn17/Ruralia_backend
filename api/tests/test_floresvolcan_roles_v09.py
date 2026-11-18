"""Hierarchy, handwriting approval and constant-query collection contract."""

import io
import uuid

from PIL import Image
from sqlalchemy import event

from core.models import AppModule, Usuario
from core.routers.auth import get_current_user
from core.services import core_permissions_for_user
from modules.mod_floresvolcan import models as m
from modules.mod_floresvolcan.manifest import MODULE_MANIFEST
from modules.mod_floresvolcan.production_control import (
    batch_metrics,
    bulk_batch_metrics,
)
from test_floresvolcan_reconciliation_v07 import fv as fv_fixture, value

fv = fv_fixture


def activate(fv, role):
    fv.db.add(
        AppModule(
            key="mod_floresvolcan",
            name="Flores",
            module_type="industry",
            status="active",
            manifest=MODULE_MANIFEST,
        )
    )
    fv.user.is_superadmin = False
    fv.user.rol = role
    fv.user.permisos = ["*", "floresvolcan:sap:manage", "core:users:manage"]
    fv.db.commit()


def test_admin_ceiling_even_with_legacy_global_grant(fv):
    activate(fv, "admin")
    for path in ["/sap-products", "/sap-options", "/crops", "/backup/settings"]:
        response = fv.client.get("/floresvolcan" + path)
        assert response.status_code == 403, (path, response.text)
    value(fv.client.post("/floresvolcan/purchase-orders/1/approve"), 403)
    value(fv.client.get("/floresvolcan/planting-orders"), 200)
    value(fv.client.get("/floresvolcan/purchase-orders"), 200)
    assert "core:users:manage" not in core_permissions_for_user(fv.user)
    assert "core:config:manage" not in core_permissions_for_user(fv.user)


def test_manager_can_approve_and_manage_sap_but_cannot_delete(fv):
    activate(fv, "gerente")
    value(fv.client.get("/floresvolcan/sap-products"), 200)
    value(fv.client.delete("/floresvolcan/suppliers/1"), 403)
    value(fv.client.delete("/floresvolcan/sap-products/1"), 403)
    assert {"core:users:manage", "core:config:manage"} <= set(
        core_permissions_for_user(fv.user)
    )
    # Existing order rather than a false positive 404 from a forbidden endpoint.
    po = fv.db.query(m.PurchaseOrder).first()
    po.status = "pending_approval"
    fv.db.commit()
    value(fv.client.post(f"/floresvolcan/purchase-orders/{po.id}/approve"), 200)


def test_worker_photo_requires_other_reviewer_and_is_atomic(fv, monkeypatch, tmp_path):
    monkeypatch.setenv("FV_PAPER_STORAGE_DIR", str(tmp_path))
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    activate(fv, "trabajador")
    value(fv.client.get("/floresvolcan/management-dashboard"), 403)
    value(fv.client.get("/floresvolcan/sap-products"), 403)
    image = io.BytesIO()
    Image.new("RGB", (300, 400), "white").save(image, format="PNG")
    scan = value(
        fv.client.post(
            "/floresvolcan/field/paper/scans",
            data={"operation": "planting", "order_id": fv.order.id},
            files={"file": ("sheet.png", image.getvalue(), "image/png")},
        ),
        201,
    )
    payload = {
        "client_token": str(uuid.uuid4()),
        "paper_capture_id": scan["id"],
        "planting_order_id": fv.order.id,
        "requisition_line_id": fv.line.id,
        "bed_space_id": fv.bed.id,
        "quantity_planted": 10,
        "planted_at": "2026-01-05T12:00:00-06:00",
    }
    value(fv.client.post("/floresvolcan/field/planting", json=payload), 403)
    value(
        fv.client.post(
            f"/floresvolcan/field/paper/captures/{scan['id']}/submit", json=payload
        ),
        200,
    )
    assert fv.db.query(m.PlantingRecord).count() == 0
    value(
        fv.client.post(
            f"/floresvolcan/field/paper/captures/{scan['id']}/submit",
            json={**payload, "quantity_planted": 99},
        ),
        409,
    )
    value(
        fv.client.post(f"/floresvolcan/paper-reviews/{scan['id']}/approve", json={}),
        403,
    )
    reviewer = Usuario(
        nombre="Administrador",
        correo="reviewer@example.com",
        hash_contrasena="test",
        rol="admin",
        is_superadmin=False,
    )
    fv.db.add(reviewer)
    fv.db.commit()
    fv.client.app.dependency_overrides[get_current_user] = lambda: reviewer
    value(fv.client.get("/floresvolcan/paper-reviews"), 200)
    # Failed review cannot consume stock or mark the capture approved.
    value(
        fv.client.post(
            f"/floresvolcan/paper-reviews/{scan['id']}/approve",
            json={"quantity_planted": 999999},
        ),
        409,
    )
    assert fv.db.query(m.PlantingRecord).count() == 0
    result = value(
        fv.client.post(
            f"/floresvolcan/paper-reviews/{scan['id']}/approve",
            json={"quantity_planted": 12},
        ),
        200,
    )
    record = fv.db.get(m.PlantingRecord, result["id"])
    assert record.quantity_planted == 12
    assert record.operator_user_id == fv.user.id
    fv.db.expire_all()
    capture = fv.db.get(m.PaperCapture, scan["id"])
    assert capture.reviewed_by_id == reviewer.id and capture.status == "digitized"
    value(
        fv.client.post(f"/floresvolcan/paper-reviews/{scan['id']}/approve", json={}),
        200,
    )
    assert fv.db.query(m.PlantingRecord).count() == 1


def test_bulk_metrics_equal_transactional_rules_and_fixed_queries(fv):
    value(fv.plant(10), 201)
    value(fv.plant(20, side="B"), 201)
    batches = fv.db.query(m.ProductionBatch).all()
    expected = {b.id: batch_metrics(fv.db, b) for b in batches}
    calls = []

    def record(*args):
        calls.append(args[2])

    event.listen(fv.engine, "before_cursor_execute", record)
    try:
        actual = bulk_batch_metrics(fv.db, batches)
    finally:
        event.remove(fv.engine, "before_cursor_execute", record)
    assert actual == expected
    assert len(calls) == 5


def test_core_hierarchy_and_module_install_guards(fv):
    from core.routers import admin

    fv.client.app.include_router(admin.router)
    activate(fv, "admin")
    value(fv.client.post("/usuarios", json={}), 403)
    value(fv.client.get("/configuracion"), 403)
    fv.user.rol = "gerente"
    fv.db.commit()
    value(fv.client.get("/configuracion"), 200)
    value(fv.client.delete(f"/usuarios/{fv.user.id}"), 403)
    value(fv.client.post("/modules/mod_floresvolcan/install"), 403)


def test_harvest_sheet_is_reviewed_and_retains_worker(fv, monkeypatch, tmp_path):
    planted = value(fv.plant(100), 201)
    order = fv.harvest_order(planted["batch_id"])
    activate(fv, "trabajador")
    monkeypatch.setenv("FV_PAPER_STORAGE_DIR", str(tmp_path))
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    image = io.BytesIO()
    Image.new("RGB", (300, 400), "white").save(image, format="PNG")
    scan = value(
        fv.client.post(
            "/floresvolcan/field/paper/scans",
            data={"operation": "harvest", "order_id": order["id"]},
            files={"file": ("cut.png", image.getvalue(), "image/png")},
        ),
        201,
    )
    body = {
        "client_token": str(uuid.uuid4()),
        "paper_capture_id": scan["id"],
        "production_batch_id": planted["batch_id"],
        "harvest_order_id": order["id"],
        "planting_record_id": planted["id"],
        "bloom_1_qty": 10,
        "bloom_2_qty": 5,
        "bloom_3_5_qty": 0,
        "discard_qty": 2,
        "discard_reason": "Daño",
        "harvested_at": "2026-01-20T12:00:00-06:00",
    }
    value(fv.client.post("/floresvolcan/field/harvest", json=body), 403)
    value(
        fv.client.post(
            f"/floresvolcan/field/paper/captures/{scan['id']}/submit", json=body
        ),
        200,
    )
    assert fv.db.query(m.HarvestRecord).count() == 0
    reviewer = Usuario(
        nombre="Revisor",
        correo="harvest-review@example.com",
        hash_contrasena="test",
        rol="admin",
        is_superadmin=False,
    )
    fv.db.add(reviewer)
    fv.db.commit()
    fv.client.app.dependency_overrides[get_current_user] = lambda: reviewer
    detail = value(fv.client.get(f"/floresvolcan/paper-reviews/{scan['id']}"), 200)
    assert detail["context"]["order_number"] == order["number"]
    assert detail["context"]["planting_record_id"] == planted["id"]
    approved = value(
        fv.client.post(
            f"/floresvolcan/paper-reviews/{scan['id']}/approve",
            json={"bloom_1_qty": 11},
        ),
        200,
    )
    row = fv.db.get(m.HarvestRecord, approved["id"])
    assert row.operator_user_id == fv.user.id
    assert row.total_output == 18
    value(
        fv.client.post(f"/floresvolcan/paper-reviews/{scan['id']}/approve", json={}),
        200,
    )
    assert fv.db.query(m.HarvestRecord).count() == 1
