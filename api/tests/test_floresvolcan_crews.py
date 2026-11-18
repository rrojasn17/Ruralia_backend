"""CRUD de cuadrillas, vigencia temporal y protección de historial."""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from test_floresvolcan_reconciliation_v07 import fv as fv_fixture, value
from test_floresvolcan_roles_v09 import activate
from modules.mod_floresvolcan import models as m
from modules.mod_floresvolcan.router import _active_crew_ids

fv = fv_fixture


def test_crud_and_root_only_retirement(fv):
    worker = fv.db.query(m.Worker).first()
    row = value(
        fv.client.post(
            "/floresvolcan/crews",
            json={"name": "Permanente de prueba", "worker_ids": [worker.id]},
        ),
        201,
    )
    assert row["kind"] == "permanent" and row["worker_ids"] == [worker.id]
    row = value(
        fv.client.put(
            f"/floresvolcan/crews/{row['id']}",
            json={"name": "Nombre corregido", "worker_ids": [worker.id]},
        ),
        200,
    )
    assert row["name"] == "Nombre corregido"
    detail = value(fv.client.get(f"/floresvolcan/crews/{row['id']}"), 200)
    assert detail["members"][0]["name"] == worker.name
    activate(fv, "gerente")
    value(fv.client.delete(f"/floresvolcan/crews/{row['id']}"), 403)
    fv.user.is_superadmin = True
    fv.db.commit()
    value(fv.client.delete(f"/floresvolcan/crews/{row['id']}"), 200)
    assert (
        value(fv.client.get(f"/floresvolcan/crews/{row['id']}"), 200)["active"] is False
    )


def test_temporary_day_activity_and_expiry(fv):
    worker = fv.db.query(m.Worker).first()
    today = datetime.now(ZoneInfo("America/Costa_Rica")).date()
    body = {
        "name": "Temporal de prueba",
        "worker_ids": [worker.id],
        "kind": "temporary",
        "activity": "harvest",
        "work_date": str(today),
    }
    row = value(fv.client.post("/floresvolcan/crews", json=body), 201)
    assert str(row["members"][0]["valid_until"]) == str(today)
    assert row["id"] in _active_crew_ids(fv.db, worker)
    from modules.mod_floresvolcan.crew_management import validate_assignment
    from fastapi import HTTPException
    import pytest

    with pytest.raises(HTTPException):
        validate_assignment(fv.db, fv.db.get(m.WorkCrew, row["id"]), "planting", today)
    with pytest.raises(HTTPException):
        validate_assignment(
            fv.db,
            fv.db.get(m.WorkCrew, row["id"]),
            "harvest",
            today + timedelta(days=1),
        )
    validate_assignment(fv.db, fv.db.get(m.WorkCrew, row["id"]), "harvest", today)
    expired = value(
        fv.client.post(
            "/floresvolcan/crews",
            json={
                **body,
                "name": "Temporal finalizada",
                "work_date": str(today - timedelta(days=1)),
            },
        ),
        201,
    )
    assert expired["id"] not in _active_crew_ids(fv.db, worker)
    available = value(fv.client.get("/floresvolcan/crews?page=1&active_only=true"), 200)
    assert expired["id"] not in [c["id"] for c in available["items"]]


def test_open_orders_protect_membership_and_history(fv):
    value(fv.client.delete(f"/floresvolcan/crews/{fv.crew.id}"), 409)
    worker = fv.db.query(m.Worker).first()
    other = m.Worker(
        employee_code="5678",
        identification="OTHER-CREW",
        name="Otra persona",
        hire_date=worker.hire_date,
    )
    fv.db.add(other)
    fv.db.commit()
    value(
        fv.client.put(
            f"/floresvolcan/crews/{fv.crew.id}",
            json={"name": fv.crew.name, "worker_ids": [other.id]},
        ),
        409,
    )
    row = value(fv.client.get(f"/floresvolcan/crews/{fv.crew.id}"), 200)
    assert row["worker_ids"] == [worker.id]


def test_temporary_requires_day_and_activity(fv):
    worker = fv.db.query(m.Worker).first()
    value(
        fv.client.post(
            "/floresvolcan/crews",
            json={
                "name": "Temporal sin fecha",
                "kind": "temporary",
                "worker_ids": [worker.id],
            },
        ),
        422,
    )


def test_closed_activity_keeps_old_members_and_changes_current_access(fv):
    old = fv.db.query(m.Worker).first()
    other = m.Worker(
        employee_code="6789",
        identification="NEW-CREW",
        name="Nuevo integrante",
        hire_date=old.hire_date,
    )
    fv.db.add(other)
    fv.order.status = "completed"
    fv.db.commit()
    value(
        fv.client.put(
            f"/floresvolcan/crews/{fv.crew.id}",
            json={"name": fv.crew.name, "worker_ids": [other.id]},
        ),
        200,
    )
    assert fv.db.query(m.WorkCrewMember).filter_by(crew_id=fv.crew.id).count() == 2
    assert fv.crew.id not in _active_crew_ids(fv.db, old)
    assert fv.crew.id in _active_crew_ids(fv.db, other)
    from modules.mod_floresvolcan.crew_management import read_config

    assert old.id in read_config(fv.db, fv.crew.id)["history"][0]["worker_ids"]


def test_assignment_rejects_different_day_or_activity(fv):
    worker = fv.db.query(m.Worker).first()
    today = datetime.now(ZoneInfo("America/Costa_Rica")).date()
    crew = value(
        fv.client.post(
            "/floresvolcan/crews",
            json={
                "name": "Temporal para siembra",
                "worker_ids": [worker.id],
                "kind": "temporary",
                "activity": "planting",
                "work_date": str(today),
            },
        ),
        201,
    )
    base = {"production_space_id": fv.bed.id, "crew_id": crew["id"]}
    value(
        fv.client.post(
            "/floresvolcan/planting-orders",
            json={**base, "planned_start_date": str(today + timedelta(days=1))},
        ),
        422,
    )
    value(
        fv.client.post(
            "/floresvolcan/planting-orders",
            json={**base, "planned_start_date": str(today)},
        ),
        201,
    )


def test_used_temporary_cannot_gain_members_after_its_activity(fv):
    worker = fv.db.query(m.Worker).first()
    today = datetime.now(ZoneInfo("America/Costa_Rica")).date()
    crew = value(
        fv.client.post(
            "/floresvolcan/crews",
            json={
                "name": "Temporal con historial",
                "kind": "temporary",
                "activity": "planting",
                "work_date": str(today),
                "worker_ids": [worker.id],
            },
        ),
        201,
    )
    order = m.PlantingOrder(
        number="CLOSED-TEMP-CREW",
        production_space_id=fv.bed.id,
        crew_id=crew["id"],
        status="completed",
    )
    other = m.Worker(
        employee_code="7890",
        identification="TEMP-OTHER",
        name="Nuevo temporal",
        hire_date=worker.hire_date,
    )
    fv.db.add_all([order, other])
    fv.db.commit()
    value(
        fv.client.put(
            f"/floresvolcan/crews/{crew['id']}",
            json={
                "name": crew["name"],
                "kind": "temporary",
                "activity": "planting",
                "work_date": str(today),
                "worker_ids": [other.id],
            },
        ),
        409,
    )
    assert crew["id"] not in _active_crew_ids(fv.db, other)
