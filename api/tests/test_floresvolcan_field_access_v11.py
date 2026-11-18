"""Acceso por PIN limitado a una orden; ninguna sesión global se crea."""

from types import SimpleNamespace
import json
import uuid

from core.routers.auth import get_current_user
from modules.mod_floresvolcan import models as m
from modules.mod_floresvolcan.worker_codes import normalize_worker_codes
from test_floresvolcan_reconciliation_v07 import fv as fv_fixture, value

fv = fv_fixture


def enable_link(fv):
    worker = fv.db.query(m.Worker).first()
    worker.employee_code = "2468"
    fv.order.qr_token = uuid.uuid4().hex
    fv.db.commit()
    return worker, fv.order.qr_token


def login(fv):
    worker, token = enable_link(fv)
    result = value(
        fv.client.post(
            "/floresvolcan/public/field/login",
            json={"operation": "planting", "qr_token": token, "employee_code": "2468"},
        ),
        200,
    )
    return worker, result


def body(fv, **changes):
    return {
        "planting_order_id": fv.order.id,
        "requisition_line_id": fv.line.id,
        "bed_space_id": fv.bed.id,
        "quantity_planted": 10,
        "planted_at": "2026-01-05T12:00:00-06:00",
        "client_token": uuid.uuid4().hex,
        **changes,
    }


def test_public_scope_csrf_attribution_and_retry(fv):
    worker, access = login(fv)
    detail = value(fv.client.get("/floresvolcan/public/field/session"), 200)
    assert [o["id"] for o in detail["tasks"]["planting_orders"]] == [fv.order.id]
    assert detail["tasks"]["harvest_orders"] == []
    payload = body(fv)
    value(fv.client.post("/floresvolcan/public/field/planting", json=payload), 403)
    headers = {"x-fv-csrf": access["csrf_token"]}
    value(
        fv.client.post(
            "/floresvolcan/public/field/planting",
            headers=headers,
            json={**payload, "planting_order_id": 999},
        ),
        403,
    )
    result = value(
        fv.client.post(
            "/floresvolcan/public/field/planting", headers=headers, json=payload
        ),
        201,
    )
    record = fv.db.get(m.PlantingRecord, result["id"])
    assert record.worker_id == worker.id
    assert value(
        fv.client.post(
            "/floresvolcan/public/field/planting", headers=headers, json=payload
        ),
        201,
    )["idempotent"]
    # The worker cookie never authorizes sensitive application endpoints.
    override = fv.client.app.dependency_overrides.pop(get_current_user)
    try:
        for path in [
            "/floresvolcan/inventory",
            "/floresvolcan/sap-products",
            "/floresvolcan/workers",
            "/floresvolcan/management-dashboard",
        ]:
            value(fv.client.get(path), 401)
    finally:
        fv.client.app.dependency_overrides[get_current_user] = override


def test_invalid_codes_rate_limit_and_assignment_revoke(fv):
    worker, token = enable_link(fv)
    for _ in range(5):
        value(
            fv.client.post(
                "/floresvolcan/public/field/login",
                json={
                    "operation": "planting",
                    "qr_token": token,
                    "employee_code": "9999",
                },
            ),
            401,
        )
    value(
        fv.client.post(
            "/floresvolcan/public/field/login",
            json={"operation": "planting", "qr_token": token, "employee_code": "2468"},
        ),
        429,
    )
    assert fv.db.query(m.FieldAccessSession).count() == 0


def test_session_revoked_on_code_or_order_change(fv):
    worker, _ = login(fv)
    worker.employee_code = "1357"
    fv.db.commit()
    value(fv.client.get("/floresvolcan/public/field/session"), 401)
    login(fv)
    fv.order.status = "completed"
    fv.db.commit()
    value(fv.client.get("/floresvolcan/public/field/session"), 403)


def test_material_settings_cannot_be_overridden(fv):
    _, access = login(fv)
    value(
        fv.client.post(
            "/floresvolcan/public/field/planting",
            headers={"x-fv-csrf": access["csrf_token"]},
            json=body(fv, density=999),
        ),
        422,
    )
    assert fv.db.query(m.PlantingRecord).count() == 0


def test_codes_are_unique_four_digits_and_legacy_preserved(fv):
    for i in range(80):
        fv.db.add(m.Worker(employee_code=f"OLD-{i}", name=f"Persona {i}"))
    fv.db.commit()
    normalize_worker_codes(fv.db)
    workers = fv.db.query(m.Worker).all()
    assert len({w.employee_code for w in workers}) == len(workers)
    assert all(len(w.employee_code) == 4 and w.employee_code.isdigit() for w in workers)
    assert any(w.legacy_employee_code == "TEST" for w in workers)
    previous = {w.id: w.employee_code for w in workers}
    normalize_worker_codes(fv.db)
    assert previous == {w.id: w.employee_code for w in fv.db.query(m.Worker)}


def test_generic_crop_and_map_configuration(fv):
    crop = value(
        fv.client.post(
            "/floresvolcan/planting/flower-options",
            json={"name": "Chile dulce", "material_type": "semilla"},
        ),
        201,
    )
    assert crop["material_type"] == "semilla"
    farm = value(
        fv.client.post(
            "/floresvolcan/production-spaces",
            json={
                "code": "MF01",
                "name": "Finca con mapa",
                "type": "farm",
                "google_maps_url": "https://www.google.com/maps?q=9.8,-83.9",
            },
        ),
        201,
    )
    detail = value(fv.client.get(f"/floresvolcan/production-spaces/{farm['id']}"), 200)
    assert float(detail["latitude"]) == 9.8 and float(detail["longitude"]) == -83.9
    bed = value(
        fv.client.post(
            "/floresvolcan/production-spaces",
            json={
                "code": "MAP-BED",
                "name": "1A",
                "type": "bed",
                "parent_id": farm["id"],
                "length_m": 10,
                "width_m": 2,
                "latitude": 20,
                "longitude": 30,
                "profiles": [{"crop_id": crop["id"], "capacity_units": 100}],
            },
        ),
        201,
    )
    detail = value(fv.client.get(f"/floresvolcan/production-spaces/{bed['id']}"), 200)
    assert detail["latitude"] is None and detail["longitude"] is None
    assert detail["profiles"][0]["density"] == 5
    value(
        fv.client.post(
            "/floresvolcan/spaces/resolve-map",
            json={"url": "http://127.0.0.1:8027/secret"},
        ),
        422,
    )


def test_agent_key_redaction_role_context_and_message_limit(fv, monkeypatch):
    import openai
    from modules.mod_floresvolcan import business_agent as agent

    captured = []

    def create(**kwargs):
        captured.append(kwargs)
        return SimpleNamespace(
            output_text=json.dumps({"severity": "info", "message": "X" * 450})
        )

    monkeypatch.setattr(
        openai,
        "OpenAI",
        lambda **kwargs: SimpleNamespace(responses=SimpleNamespace(create=create)),
    )
    settings = value(
        fv.client.put(
            "/floresvolcan/business-agent/settings",
            json={
                "enabled": True,
                "model": "test-model",
                "api_key": "test-key-not-real",
            },
        ),
        200,
    )
    assert settings["has_api_key"] and "api_key" not in settings
    value(fv.client.get("/floresvolcan/business-status"), 200)
    result = value(fv.client.get("/floresvolcan/business-status"), 200)
    assert result["source"] == "openai" and len(result["message"]) == 300
    assert "test-key-not-real" not in captured[0]["input"]
    from test_floresvolcan_roles_v09 import activate

    activate(fv, "trabajador")
    data = agent.context(fv.db, fv.user)
    assert "kpis" not in data and "alerts" not in data
    assert "assigned_planting_orders" in data
    value(fv.client.get("/floresvolcan/business-agent/settings"), 403)


def test_dashboard_transit_requires_confirmed_payment_and_dispatch(fv):
    po = fv.db.query(m.PurchaseOrder).first()
    assert (
        value(fv.client.get("/floresvolcan/management-dashboard"), 200)["kpis"][
            "purchase_in_transit"
        ]
        == 0
    )
    value(
        fv.client.post(
            f"/floresvolcan/purchase-orders/{po.id}/transit",
            json={"paid_at": "2026-01-01", "shipped_at": "2026-01-02"},
        ),
        200,
    )
    kpis = value(fv.client.get("/floresvolcan/management-dashboard"), 200)["kpis"]
    assert kpis["purchase_in_transit"] == 1 and kpis["warehouse_bulbs"] == 3000
    po.status = "completed"
    fv.db.commit()
    assert (
        value(fv.client.get("/floresvolcan/management-dashboard"), 200)["kpis"][
            "purchase_in_transit"
        ]
        == 0
    )


def test_harvest_code_session_is_bound_to_the_harvest_order(fv):
    worker, _ = enable_link(fv)
    planted = value(fv.plant(10), 201)
    ho = fv.harvest_order(planted["batch_id"])
    ho["qr_token"] = fv.db.get(m.HarvestOrder, ho["id"]).qr_token
    assert ho["qr_token"]
    access = value(
        fv.client.post(
            "/floresvolcan/public/field/login",
            json={
                "operation": "harvest",
                "qr_token": ho["qr_token"],
                "employee_code": "2468",
            },
        ),
        200,
    )
    detail = value(fv.client.get("/floresvolcan/public/field/session"), 200)
    assert not detail["tasks"]["planting_orders"]
    assert len(detail["tasks"]["open_batches"]) == 1
    headers = {"x-fv-csrf": access["csrf_token"]}
    payload = {
        "client_token": uuid.uuid4().hex,
        "harvest_order_id": ho["id"],
        "production_batch_id": planted["batch_id"],
        "planting_record_id": planted["id"],
        "bloom_1_qty": 2,
        "harvested_at": "2026-02-02T12:00:00-06:00",
    }
    result = value(
        fv.client.post(
            "/floresvolcan/public/field/harvest", headers=headers, json=payload
        ),
        201,
    )
    assert fv.db.get(m.HarvestRecord, result["id"]).worker_id == worker.id
    value(
        fv.client.post(
            "/floresvolcan/public/field/planting", headers=headers, json=body(fv)
        ),
        403,
    )


def test_combined_orders_cannot_overfill_the_same_bed_cycle(fv):
    from decimal import Decimal

    crop = fv.db.query(m.FlowerCrop).filter_by(code="LIRIO").one()
    fv.bed.length_m = 10
    fv.bed.width_m = 2
    fv.bed.capacity = 20
    profile = m.BedCropProfile(
        bed_space_id=fv.bed.id,
        crop_id=crop.id,
        material_type="bulbo",
        capacity_units=110,
        density=Decimal("5.5"),
    )
    fv.db.add(profile)
    fv.db.flush()
    fv.db.add(
        m.PlantingOrderBed(
            planting_order_id=fv.order.id,
            bed_space_id=fv.bed.id,
            bed_profile_id=profile.id,
            capacity_units_snapshot=110,
        )
    )
    second = m.PlantingOrder(
        number="SECOND-ORDER",
        production_space_id=fv.bed.id,
        crew_id=fv.crew.id,
        status="approved",
        planned_quantity=110,
    )
    fv.db.add(second)
    fv.db.flush()
    fv.db.add(
        m.PlantingOrderBed(
            planting_order_id=second.id,
            bed_space_id=fv.bed.id,
            bed_profile_id=profile.id,
            capacity_units_snapshot=110,
        )
    )
    req = m.Requisition(
        number="SECOND-REQ", planting_order_id=second.id, status="delivered"
    )
    fv.db.add(req)
    fv.db.flush()
    line = m.RequisitionLine(
        requisition_id=req.id,
        inventory_lot_id=fv.lot.id,
        sap_product_id=fv.product.id,
        quantity_requested=110,
        quantity_approved=110,
        quantity_delivered=110,
    )
    fv.db.add(line)
    fv.lot.quantity_issued += 110
    fv.lot.quantity_available -= 110
    fv.db.commit()
    value(fv.plant(60), 201)
    value(fv.plant(60, planting_order_id=second.id, requisition_line_id=line.id), 409)
    assert fv.db.query(m.PlantingRecord).count() == 1


def test_worker_code_is_generated_and_cannot_be_replaced_by_form(fv):
    payload = {
        "name": "Persona con código",
        "identification": "PIN-NEW-ID",
        "hire_date": "2026-01-01",
        "employee_code": "CUSTOM",
    }
    created = value(fv.client.post("/floresvolcan/workers", json=payload), 201)
    assert len(created["employee_code"]) == 4 and created["employee_code"].isdigit()
    updated = value(
        fv.client.put(
            f"/floresvolcan/workers/{created['id']}",
            json={**payload, "employee_code": "0001"},
        ),
        200,
    )
    assert updated["employee_code"] == created["employee_code"]


def test_existing_code_from_another_crew_does_not_grant_access(fv):
    _, token = enable_link(fv)
    fv.db.add(m.Worker(name="Otra persona", employee_code="1357", active=True))
    fv.db.commit()
    value(
        fv.client.post(
            "/floresvolcan/public/field/login",
            json={"operation": "planting", "qr_token": token, "employee_code": "1357"},
        ),
        401,
    )
    assert fv.db.query(m.FieldAccessSession).count() == 0


def test_generic_material_and_unambiguous_workbook_defaults(fv):
    crop = value(
        fv.client.post(
            "/floresvolcan/planting/flower-options",
            json={"name": "Chile para material", "material_type": "semilla"},
        ),
        201,
    )
    product = value(
        fv.client.post(
            "/floresvolcan/sap-products",
            json={
                "code": "GENSEED",
                "description": "Semilla de chile",
                "crop_id": crop["id"],
                "default_density": 25,
                "default_cycle_weeks": 12,
            },
        ),
        201,
    )
    assert (
        product["material_type"] == "semilla" and product["inventory_unit"] == "semilla"
    )
    opts = value(fv.client.get("/floresvolcan/sap-options"), 200)
    assert (
        next(c["label"] for c in opts["catalogs"]["color"] if c["code"] == "WH")
        == "Blanco"
    )
    varieties = {c["code"] for c in opts["catalogs"]["variety"]}
    calibers = {c["code"] for c in opts["catalogs"]["caliber"]}
    profile = next(
        p
        for p in opts["material_defaults"]
        if len(p["cycles"]) == len(p["densities"]) == 1
        and p["variety"] in varieties
        and p["caliber"] in calibers
    )
    product = value(
        fv.client.post(
            "/floresvolcan/sap-products",
            json={
                "code": "SRCDEF",
                "description": "Material con fuente",
                "variety": profile["variety"],
                "caliber": profile["caliber"],
                "color": "WH",
            },
        ),
        201,
    )
    assert product["default_cycle_weeks"] == profile["cycles"][0]
    assert product["default_density"] == profile["densities"][0]
    assert product["color"] == "WH"


def test_legacy_approved_order_gets_complete_stable_share_token(fv):
    fv.order.qr_token = None
    fv.db.commit()
    path = f'/floresvolcan/planting-orders/{fv.order.id}/share-link'
    first = value(fv.client.post(path), 200)
    assert len(first['qr_token']) >= 32
    assert value(fv.client.post(path), 200) == first
    detail = value(fv.client.get(f'/floresvolcan/planting-orders/{fv.order.id}'), 200)
    assert detail['qr_token'] == first['qr_token']
    fv.order.status = 'completed'
    fv.db.commit()
    value(fv.client.post(path), 409)


def test_bed_block_is_visible_before_registration_and_never_cleared(fv):
    from modules.mod_floresvolcan.production_control import bed_planting_availability
    cycle = m.BedCycle(bed_space_id=fv.bed.id, status='reconciliation')
    fv.db.add(cycle)
    fv.db.commit()
    info = bed_planting_availability(fv.db, [fv.bed])[fv.bed.id]
    assert info['planting_allowed'] is False
    assert info['bed_cycle_id'] == cycle.id
    assert 'conciliación' in info['planting_block_reason']
    tasks = value(fv.client.get('/floresvolcan/field/tasks'), 200)
    option = next(b for o in tasks['planting_orders'] for b in o['bed_options'] if b['id'] == fv.bed.id)
    assert option['planting_allowed'] is False
    assert fv.db.get(m.BedCycle, cycle.id).status == 'reconciliation'


def test_reserved_bulbs_show_worker_form_before_delivery_and_unlock_after(fv):
    fv.req.status = 'approved'
    fv.line.quantity_requested = 250
    fv.line.quantity_approved = 250
    fv.line.quantity_delivered = 0
    fv.lot.quantity_available = 4750
    fv.lot.quantity_reserved = 250
    fv.lot.quantity_issued = 0
    fv.db.commit()
    _, access = login(fv)
    headers = {'x-fv-csrf': access['csrf_token']}
    detail = value(fv.client.get('/floresvolcan/public/field/session'), 200)
    order = detail['tasks']['planting_orders'][0]
    assert order['id'] == fv.order.id
    assert float(order['materials'][0]['pending_delivery']) == 250
    assert float(order['materials'][0]['pending_to_plant']) == 0
    # Seeing the reserved material does not permit consuming it before delivery.
    value(fv.client.post('/floresvolcan/public/field/planting', headers=headers,
                         json=body(fv)), 409)
    assert fv.db.query(m.PlantingRecord).count() == 0
    value(fv.client.post(f'/floresvolcan/requisitions/{fv.req.id}/deliveries', json={
        'requisition_line_id':fv.line.id, 'quantity':100,
    }), 201)
    detail = value(fv.client.get('/floresvolcan/public/field/session'), 200)
    material = detail['tasks']['planting_orders'][0]['materials'][0]
    assert float(material['pending_delivery']) == 150
    assert float(material['pending_to_plant']) == 100
    value(fv.client.post('/floresvolcan/public/field/planting', headers=headers,
                         json=body(fv,quantity_planted=10)), 201)
    detail = value(fv.client.get('/floresvolcan/public/field/session'), 200)
    assert float(detail['tasks']['planting_orders'][0]['materials'][0]['pending_to_plant']) == 90
