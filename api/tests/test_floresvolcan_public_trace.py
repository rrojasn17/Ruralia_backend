"""El pasaporte público es estable y no concede acceso a registrar trabajo."""

from core.routers.auth import get_current_user
from test_floresvolcan_reconciliation_v07 import fv as fv_fixture, value
from modules.mod_floresvolcan import models as m
from test_floresvolcan_roles_v09 import activate

fv = fv_fixture


def test_public_trace_is_stable_and_filters_internal_information(fv):
    fv.order.comments = "PRIVATE-COMMENT"
    fv.db.commit()
    path = f"/floresvolcan/planting-orders/{fv.order.id}"
    first = value(fv.client.post(path + "/trace-link"), 200)
    second = value(fv.client.post(path + "/trace-link"), 200)
    assert first["token"] == second["token"]
    value(
        fv.client.put(
            path + "/trace-notes",
            json={"notes": "Cultivado con cuidado en nuestra finca."},
        ),
        200,
    )
    override = fv.client.app.dependency_overrides.pop(get_current_user)
    try:
        response = fv.client.get("/floresvolcan/public/trace/" + first["token"])
        data = value(response, 200)
        assert data["materials"][0]["supplier"] == "Proveedor de prueba"
        assert data["materials"][0]["lot"] == "PROVIDER-LOT"
        assert data["notes"] == "Cultivado con cuidado en nuestra finca."
        assert "PRIVATE-COMMENT" not in response.text
        for key in [
            "qr_token",
            "worker_id",
            "requested_by_id",
            "quantity_available",
            "unit_price",
            "hash_contrasena",
            "employee_code",
        ]:
            assert key not in response.text
        assert response.headers["cache-control"] == "no-store"
        value(fv.client.get("/floresvolcan/public/trace/bad-token"), 404)
        value(
            fv.client.post(
                "/floresvolcan/public/field/login",
                json={
                    "operation": "planting",
                    "employee_code": "2468",
                    "qr_token": first["token"],
                },
            ),
            401,
        )
    finally:
        fv.client.app.dependency_overrides[get_current_user] = override
    fv.order.status = "completed"
    fv.db.commit()
    assert value(fv.client.get("/floresvolcan/public/trace/" + first["token"]), 200)[
        "code"
    ]
    assert value(fv.client.post(path + "/trace-link"), 200)["token"] == first["token"]


def test_trace_survives_harvest_and_reports_actual_counts(fv):
    planted = value(fv.plant(1000), 201)
    token = value(
        fv.client.post(f"/floresvolcan/planting-orders/{fv.order.id}/trace-link"), 200
    )["token"]
    ho = fv.harvest_order(planted["batch_id"])
    value(fv.cut(planted["batch_id"], ho["id"], 100), 201)
    data = value(fv.client.get("/floresvolcan/public/trace/" + token), 200)
    assert data["stage"] == "harvesting"
    assert float(data["planted"]) == 1000 and float(data["harvested"]) == 100
    assert data["beds"][0]["location"][-1]["name"] == fv.bed.name
    batch = fv.db.get(m.ProductionBatch, planted["batch_id"])
    batch.status = "closed"
    fv.db.commit()
    assert (
        value(fv.client.get("/floresvolcan/public/trace/" + token), 200)["stage"]
        == "closed"
    )


def test_preparation_already_has_public_token_and_worker_cannot_publish(fv):
    row = value(
        fv.client.post(
            "/floresvolcan/planting-orders",
            json={"production_space_id": fv.bed.id, "crew_id": fv.crew.id},
        ),
        201,
    )
    data = value(fv.client.get(f"/floresvolcan/planting-orders/{row['id']}"), 200)
    assert data["trace"]["token"] and data["status"] == "pending_approval"
    public = value(
        fv.client.get("/floresvolcan/public/trace/" + data["trace"]["token"]), 200
    )
    assert public["stage"] == "preparation"
    activate(fv, "trabajador")
    value(
        fv.client.put(
            f"/floresvolcan/planting-orders/{row['id']}/trace-notes",
            json={"notes": "No autorizado"},
        ),
        403,
    )
