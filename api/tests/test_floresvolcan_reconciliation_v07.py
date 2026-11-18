"""Contrato transaccional de Flores Volcán: cantidades, identidad y liberación física.

FV_TEST_DATABASE_URL permite ejecutar los mismos casos en PostgreSQL temporal.
Cada caso usa una base SQLite o un esquema PostgreSQL nuevo, nunca producción.
"""

from __future__ import annotations

import io
import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone
from decimal import Decimal
from threading import Barrier
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from openpyxl import load_workbook
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from core.models import Usuario
from core.routers.auth import get_current_user
from database import Base, get_db
from modules.mod_floresvolcan import lifecycle, models as m, router as r
from modules.mod_floresvolcan.legacy_excel import build_lirio_workbook
from modules.mod_floresvolcan.production_control import batch_metrics
from modules.mod_floresvolcan.schemas import HarvestRecordIn


@pytest.fixture
def fv():
    url = os.getenv("FV_TEST_DATABASE_URL")
    schema = "fvtest_" + uuid.uuid4().hex
    if url:
        assert "@127.0.0.1:55439/fv_test" in url, (
            "Solo se admite la base temporal explícita de pruebas"
        )
        bootstrap = create_engine(url)
        with bootstrap.begin() as conn:
            conn.execute(text(f'CREATE SCHEMA "{schema}"'))
        engine = create_engine(url, connect_args={"options": f"-csearch_path={schema}"})
    else:
        engine = create_engine(
            "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
        )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
    db = factory()
    lifecycle.install(db)
    user = Usuario(
        nombre="Supervisor de pruebas",
        correo="fv-test@example.com",
        hash_contrasena="test-only",
        rol="admin",
        is_superadmin=True,
    )
    db.add(user)
    db.flush()
    worker = m.Worker(
        employee_code="TEST",
        identification="TEST-ID",
        name="Trabajador",
        hire_date=date(2025, 1, 1),
        user_id=user.id,
    )
    db.add(worker)
    db.flush()
    crew = m.WorkCrew(
        name="Cuadrilla de prueba", members=[m.WorkCrewMember(worker_id=worker.id)]
    )
    bed = m.ProductionSpace(
        code="TEST-BED",
        name="Mesa de prueba",
        type="bed",
        status="active",
        capacity=100,
    )
    supplier = m.Supplier(
        code="TEST-SUP", name="Proveedor de prueba", country="País de prueba"
    )
    product = m.SapProduct(
        code="TEST-SAP",
        description="Bulbos de prueba",
        variety="Variedad de prueba",
        default_density=100,
        default_cycle_weeks=2,
        expected_stems_per_bulb=1,
    )
    db.add_all([crew, bed, supplier, product])
    db.flush()
    order = m.PlantingOrder(
        number="TEST-ORDER",
        production_space_id=bed.id,
        crew_id=crew.id,
        status="approved",
    )
    po = m.PurchaseOrder(
        number="TEST-PO",
        supplier_id=supplier.id,
        warehouse_due_date=date(2026, 1, 1),
        status="in_process",
    )
    db.add_all([order, po])
    db.flush()
    po_line = m.PurchaseOrderLine(
        purchase_order_id=po.id,
        sap_product_id=product.id,
        product_code_snapshot=product.code,
        description_snapshot=product.description,
        quantity=50,
        unit_price_estimated=1,
    )
    receipt = m.GoodsReceipt(
        number="TEST-REC", purchase_order_id=po.id, container_number="TEST-CONTAINER"
    )
    db.add_all([po_line, receipt])
    db.flush()
    receipt_line = m.GoodsReceiptLine(
        goods_receipt_id=receipt.id,
        purchase_order_line_id=po_line.id,
        quantity_received=5000,
    )
    db.add(receipt_line)
    db.flush()
    lot = m.InventoryLot(
        lot_code="TEST-LOT",
        supplier_lot_code="PROVIDER-LOT",
        sap_product_id=product.id,
        goods_receipt_line_id=receipt_line.id,
        quantity_received=5000,
        quantity_available=3000,
        quantity_issued=2000,
        quantity_reserved=0,
        variety_snapshot=product.variety,
        container_number="TEST-CONTAINER",
    )
    db.add(lot)
    db.flush()
    req = m.Requisition(
        number="TEST-REQ", planting_order_id=order.id, status="delivered"
    )
    db.add(req)
    db.flush()
    line = m.RequisitionLine(
        requisition_id=req.id,
        inventory_lot_id=lot.id,
        sap_product_id=product.id,
        quantity_requested=2000,
        quantity_approved=2000,
        quantity_delivered=2000,
    )
    db.add(line)
    db.commit()
    app = FastAPI()
    app.include_router(r.router)

    def session_override():
        session = factory()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = session_override
    app.dependency_overrides[get_current_user] = lambda: user
    client = TestClient(app)
    state = SimpleNamespace(
        db=db,
        factory=factory,
        client=client,
        user=user,
        bed=bed,
        crew=crew,
        order=order,
        line=line,
        req=req,
        lot=lot,
        product=product,
        engine=engine,
    )

    def plant(qty=1000, **changes):
        body = dict(
            client_token=str(uuid.uuid4()),
            planting_order_id=order.id,
            requisition_line_id=line.id,
            bed_space_id=bed.id,
            quantity_planted=qty,
            side="A",
            planted_at="2026-01-05T12:00:00-06:00",
        )
        body.update(changes)
        return client.post("/floresvolcan/field/planting", json=body)

    state.plant = plant

    def harvest_order(batch_id, **changes):
        body = dict(
            production_batch_id=batch_id, crew_id=crew.id, planned_date="2026-01-19"
        )
        body.update(changes)
        response = client.post("/floresvolcan/harvest-orders", json=body)
        assert response.status_code == 201, response.text
        ho = response.json()
        response = client.post(f"/floresvolcan/harvest-orders/{ho['id']}/approve")
        assert response.status_code == 200, response.text
        return ho

    state.harvest_order = harvest_order

    def cut(batch_id, ho_id, qty=100, **changes):
        body = dict(
            client_token=str(uuid.uuid4()),
            production_batch_id=batch_id,
            harvest_order_id=ho_id,
            bloom_1_qty=qty,
            harvested_at="2026-01-20T12:00:00-06:00",
        )
        body.update(changes)
        return client.post("/floresvolcan/field/harvest", json=body)

    state.cut = cut
    yield state
    client.close()
    db.close()
    engine.dispose()
    if url:
        with bootstrap.begin() as conn:
            conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        bootstrap.dispose()


def value(response, expected):
    assert response.status_code == expected, response.text
    return response.json()


def test_full_cycle_partial_cuts_reconciliation_and_cleaning(fv):
    planting = value(fv.plant(), 201)
    batch = planting["batch_id"]
    ho = fv.harvest_order(batch)
    value(fv.cut(batch, ho["id"], 300), 201)
    value(
        fv.cut(batch, ho["id"], 400, discard_qty=20, discard_reason="Daño verificado"),
        201,
    )
    value(fv.cut(batch, ho["id"], 250), 201)
    value(fv.cut(batch, ho["id"], 31), 409)
    summary = value(
        fv.client.get(f"/floresvolcan/production-batches/{batch}/summary"), 200
    )
    assert Decimal(summary["sale"]) == 950
    assert Decimal(summary["discarded"]) == 20
    assert Decimal(summary["remaining"]) == 30
    value(
        fv.client.post(
            f"/floresvolcan/production-batches/{batch}/technical-close",
            json={"comments": "Revisión de prueba"},
        ),
        409,
    )
    value(fv.client.post(f"/floresvolcan/harvest-orders/{ho['id']}/complete"), 200)
    value(
        fv.client.post(
            f"/floresvolcan/production-batches/{batch}/technical-close",
            json={"comments": "No se justifica remanente"},
        ),
        409,
    )
    closed = value(
        fv.client.post(
            f"/floresvolcan/production-batches/{batch}/technical-close",
            json={
                "comments": "Cierre con revisión física",
                "segments": [
                    {
                        "planting_record_id": planting["id"],
                        "loss_quantity": 30,
                        "loss_reason": "Plantas sin desarrollo verificadas",
                    }
                ],
            },
        ),
        201,
    )
    assert Decimal(closed["difference"]) == 0
    assert Decimal(closed["total_loss"]) == 30
    cycles = value(fv.client.get("/floresvolcan/bed-cycles"), 200)
    cycle = cycles[0]
    assert cycle["status"] == "pending_cleaning"
    assert Decimal(cycle["occupied_m2"]) == 10
    value(fv.plant(10), 409)
    value(
        fv.client.post(
            f"/floresvolcan/bed-cycles/{cycle['id']}/clean",
            json={"comments": "Limpieza realizada", "confirmed_empty": False},
        ),
        422,
    )
    value(
        fv.client.post(
            f"/floresvolcan/bed-cycles/{cycle['id']}/clean",
            json={"comments": "Retiro y limpieza verificados", "confirmed_empty": True},
        ),
        200,
    )
    following = value(
        fv.plant(10, planted_at=datetime.now(timezone.utc).isoformat()), 201
    )
    assert following["batch_id"] != batch


def test_close_without_harvest_requires_explicit_loss(fv):
    planting = value(fv.plant(100), 201)
    path = f"/floresvolcan/production-batches/{planting['batch_id']}/technical-close"
    value(
        fv.client.post(path, json={"comments": "Intento de cierre sin explicación"}),
        409,
    )
    value(
        fv.client.post(
            path,
            json={
                "comments": "Pérdida total verificada",
                "segments": [
                    {
                        "planting_record_id": planting["id"],
                        "loss_quantity": 100,
                        "loss_reason": "Daño total comprobado antes de cosecha",
                    }
                ],
            },
        ),
        201,
    )


def test_factor_is_used_by_limits_metrics_and_closure(fv):
    fv.product.expected_stems_per_bulb = 2
    fv.db.commit()
    planting = value(fv.plant(100), 201)
    batch = planting["batch_id"]
    ho = fv.harvest_order(batch)
    value(fv.cut(batch, ho["id"], 150), 201)
    summary = value(
        fv.client.get(f"/floresvolcan/production-batches/{batch}/summary"), 200
    )
    assert Decimal(summary["expected_stems"]) == 200
    assert Decimal(summary["remaining"]) == 50
    assert Decimal(summary["yield_percentage"]) == 75
    assert Decimal(summary["stems_per_bulb"]) == Decimal("1.5")


def test_segments_same_lot_and_week_do_not_share_harvest_allowance(fv):
    one = value(fv.plant(100), 201)
    two = value(fv.plant(200), 201)
    assert one["batch_id"] == two["batch_id"] and one["id"] != two["id"]
    ho = fv.harvest_order(one["batch_id"])
    value(fv.cut(one["batch_id"], ho["id"], 10), 422)
    value(fv.cut(one["batch_id"], ho["id"], 101, planting_record_id=one["id"]), 409)
    value(fv.cut(one["batch_id"], ho["id"], 100, planting_record_id=one["id"]), 201)
    value(fv.cut(one["batch_id"], ho["id"], 200, planting_record_id=two["id"]), 201)


@pytest.mark.parametrize(
    "change",
    [
        {"requisition_line_id": 999999},
        {"production_batch_id": 999999},
        {"greenhouse_space_id": 999999},
    ],
)
def test_invalid_explicit_ids_are_not_replaced(fv, change):
    value(fv.plant(100, **change), 422)


def test_explicit_batch_must_match_side_and_order(fv):
    one = value(fv.plant(100, side="A"), 201)
    value(fv.plant(100, side="B", production_batch_id=one["batch_id"]), 422)


def test_idempotent_retries_and_changed_payload_conflict(fv):
    token = str(uuid.uuid4())
    first = value(fv.plant(100, client_token=token), 201)
    retry = value(fv.plant(100, client_token=token), 201)
    assert first["id"] == retry["id"] and retry["idempotent"]
    value(fv.plant(101, client_token=token), 409)
    ho = fv.harvest_order(first["batch_id"])
    ht = str(uuid.uuid4())
    cut = value(fv.cut(first["batch_id"], ho["id"], 50, client_token=ht), 201)
    again = value(fv.cut(first["batch_id"], ho["id"], 50, client_token=ht), 201)
    assert cut["id"] == again["id"]
    value(fv.cut(first["batch_id"], ho["id"], 51, client_token=ht), 409)


def test_real_dates_control_week_and_maturity(fv):
    one = value(fv.plant(100, planted_at="2026-01-04T23:30:00-06:00"), 201)
    assert one["planting_week"] == "26-01"
    ho = fv.harvest_order(one["batch_id"])
    value(
        fv.cut(one["batch_id"], ho["id"], 5, harvested_at="2026-01-01T12:00:00-06:00"),
        409,
    )
    value(
        fv.cut(one["batch_id"], ho["id"], 5, harvested_at="2026-01-18T12:00:00-06:00"),
        409,
    )
    value(fv.plant(10, planted_at=None), 422)
    value(fv.plant(10, planted_at="2099-01-01T12:00:00-06:00"), 422)


def test_later_segment_requires_its_own_maturity(fv):
    one = value(fv.plant(100), 201)
    two = value(fv.plant(100, planted_at="2026-01-09T12:00:00-06:00"), 201)
    ho = fv.harvest_order(one["batch_id"])
    value(
        fv.cut(
            one["batch_id"],
            ho["id"],
            10,
            planting_record_id=two["id"],
            harvested_at="2026-01-20T12:00:00-06:00",
        ),
        409,
    )
    value(
        fv.cut(
            one["batch_id"],
            ho["id"],
            10,
            planting_record_id=one["id"],
            harvested_at="2026-01-20T12:00:00-06:00",
        ),
        201,
    )


def test_authorized_overage_is_bounded(fv):
    one = value(fv.plant(100), 201)
    ho = fv.harvest_order(
        one["batch_id"],
        allow_overage=True,
        overage_quantity=10,
        overage_reason="Reconteo técnico documentado",
    )
    value(fv.cut(one["batch_id"], ho["id"], 110), 201)
    value(fv.cut(one["batch_id"], ho["id"], 1), 409)


def test_two_batches_keep_bed_occupied_until_both_close(fv):
    one = value(fv.plant(100, side="A"), 201)
    two = value(fv.plant(100, side="B"), 201)
    assert one["batch_id"] != two["batch_id"]
    value(
        fv.client.post(
            f"/floresvolcan/production-batches/{one['batch_id']}/technical-close",
            json={
                "comments": "Pérdida justificada",
                "segments": [
                    {
                        "planting_record_id": one["id"],
                        "loss_quantity": 100,
                        "loss_reason": "Verificación técnica de pérdida",
                    }
                ],
            },
        ),
        201,
    )
    cycle = value(fv.client.get("/floresvolcan/bed-cycles"), 200)[0]
    assert cycle["open_batches"] == 1 and cycle["status"] == "reconciliation"
    value(
        fv.client.post(
            f"/floresvolcan/bed-cycles/{cycle['id']}/clean",
            json={
                "comments": "Intento de limpieza anticipada",
                "confirmed_empty": True,
            },
        ),
        409,
    )


def test_historical_cut_must_be_linked_before_more_harvest(fv):
    one = value(fv.plant(100), 201)
    ho = fv.harvest_order(one["batch_id"])
    previous = m.HarvestRecord(
        production_batch_id=one["batch_id"],
        crew_id=fv.crew.id,
        quantity_harvested=80,
        total_output=80,
        harvested_at=datetime(2026, 1, 20, tzinfo=timezone.utc),
    )
    fv.db.add(previous)
    fv.db.commit()
    value(fv.cut(one["batch_id"], ho["id"], 21), 409)
    value(
        fv.client.post(
            f"/floresvolcan/harvest-records/{previous.id}/assign-segment",
            json={
                "planting_record_id": one["id"],
                "comments": "Verificado con bitácora original",
            },
        ),
        200,
    )
    value(fv.cut(one["batch_id"], ho["id"], 21), 409)
    value(fv.cut(one["batch_id"], ho["id"], 20), 201)


def test_material_return_loss_and_release_preserve_inventory(fv):
    path = f"/floresvolcan/requisition-lines/{fv.line.id}/resolve-material"
    value(
        fv.client.post(
            path,
            json={
                "client_token": str(uuid.uuid4()),
                "kind": "return",
                "quantity": 100,
                "comments": "Material no utilizado devuelto",
            },
        ),
        201,
    )
    value(
        fv.client.post(
            path,
            json={
                "client_token": str(uuid.uuid4()),
                "kind": "field_loss",
                "quantity": 50,
                "comments": "Material dañado antes de siembra",
            },
        ),
        201,
    )
    value(fv.plant(1851), 409)
    value(fv.plant(1850), 201)
    fv.db.expire_all()
    lot = fv.db.get(m.InventoryLot, fv.lot.id)
    assert (
        lot.quantity_received
        == lot.quantity_available
        + lot.quantity_reserved
        + lot.quantity_issued
        + lot.quantity_consumed
        + lot.quantity_written_off
    )
    assert lot.quantity_written_off == 50 and lot.quantity_available == 3100


def test_export_has_stable_segment_keys_and_single_row_reconciliation(fv):
    one = value(fv.plant(100), 201)
    ho = fv.harvest_order(one["batch_id"])
    value(fv.cut(one["batch_id"], ho["id"], 20), 201)
    value(fv.cut(one["batch_id"], ho["id"], 30), 201)
    fv.db.expire_all()
    wb = load_workbook(io.BytesIO(build_lirio_workbook(fv.db)), data_only=True)
    assert wb["Conciliacion"].max_row == 2
    assert wb["Siembra"]["A2"].value == f"SIEMBRA-{one['id']}"
    assert (
        wb["Corta"]["A2"].value == wb["Corta"]["A3"].value == wb["Siembra"]["A2"].value
    )
    assert "NO SUMAR" in wb["Corta"]["I1"].value
    assert wb["Conciliacion"]["K2"].value == 100
    assert wb["Conciliacion"]["P2"].value == 50


def test_legacy_upgrade_preserves_output_and_is_idempotent(fv):
    one = value(fv.plant(100), 201)
    cut = m.HarvestRecord(
        production_batch_id=one["batch_id"],
        crew_id=fv.crew.id,
        quantity_harvested=40,
        total_output=0,
        harvested_at=datetime(2026, 1, 20, tzinfo=timezone.utc),
    )
    fv.db.add(cut)
    fv.db.query(m.ModuleSetting).filter(
        m.ModuleSetting.key == "production_control.v07"
    ).delete()
    fv.db.commit()
    lifecycle.install(fv.db)
    lifecycle.install(fv.db)
    fv.db.refresh(cut)
    assert cut.total_output == 40 and cut.planting_record_id is None
    assert (
        batch_metrics(fv.db, fv.db.get(m.ProductionBatch, one["batch_id"]))[
            "unlinked_cuts"
        ]
        == 1
    )
    indexes = inspect(fv.engine).get_indexes("fv_harvest_records")
    assert any(i["unique"] and i["column_names"] == ["client_token"] for i in indexes)


@pytest.mark.skipif(
    not os.getenv("FV_TEST_DATABASE_URL"),
    reason="Requiere PostgreSQL temporal para locks reales",
)
def test_concurrent_cuts_cannot_exceed_same_segment(fv):
    one = value(fv.plant(100), 201)
    ho = fv.harvest_order(one["batch_id"])
    barrier = Barrier(2)

    def submit():
        db = fv.factory()
        try:
            barrier.wait(timeout=10)
            payload = HarvestRecordIn(
                client_token=str(uuid.uuid4()),
                production_batch_id=one["batch_id"],
                harvest_order_id=ho["id"],
                bloom_1_qty=60,
                harvested_at="2026-01-20T12:00:00-06:00",
            )
            try:
                r.register_harvest(payload, current=fv.user, db=db)
                return 201
            except Exception as exc:
                db.rollback()
                return getattr(exc, "status_code", str(exc))
        finally:
            db.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses = list(pool.map(lambda _: submit(), range(2)))
    assert sorted(statuses) == [201, 409]
    fv.db.expire_all()
    assert (
        batch_metrics(fv.db, fv.db.get(m.ProductionBatch, one["batch_id"]))["harvested"]
        == 60
    )


def test_void_keeps_original_and_restores_segment_allowance(fv):
    one = value(fv.plant(100), 201)
    ho = fv.harvest_order(one["batch_id"])
    cut = value(fv.cut(one["batch_id"], ho["id"], 80), 201)
    value(
        fv.client.post(
            f"/floresvolcan/harvest-records/{cut['id']}/void",
            json={"comments": "Conteo duplicado según bitácora original"},
        ),
        200,
    )
    summary = value(
        fv.client.get(f"/floresvolcan/production-batches/{one['batch_id']}/summary"),
        200,
    )
    assert Decimal(summary["harvested"]) == 0
    assert (
        summary["cuts"][0]["void_reason"] == "Conteo duplicado según bitácora original"
    )
    value(fv.cut(one["batch_id"], ho["id"], 100), 201)
    fv.db.expire_all()
    wb = load_workbook(io.BytesIO(build_lirio_workbook(fv.db)), data_only=True)
    assert wb["Corta"].max_row == 2
    assert wb["Cortas anuladas"].max_row == 2
    assert wb["Cortas anuladas"]["G2"].value == 80


def test_historical_overage_requires_exact_quantity_and_remains_audited(fv):
    one = value(fv.plant(100), 201)
    historical = m.HarvestRecord(
        production_batch_id=one["batch_id"],
        planting_record_id=one["id"],
        crew_id=fv.crew.id,
        quantity_harvested=110,
        total_output=110,
        harvested_at=datetime(2026, 1, 20, tzinfo=timezone.utc),
    )
    fv.db.add(historical)
    fv.db.commit()
    path = f"/floresvolcan/production-batches/{one['batch_id']}"
    value(
        fv.client.post(
            path + "/technical-close",
            json={"comments": "Exceso todavía sin autorización"},
        ),
        409,
    )
    value(
        fv.client.post(
            path + "/approve-historical-overage",
            json={"quantity": 11, "comments": "Reconteo técnico del original"},
        ),
        409,
    )
    value(
        fv.client.post(
            path + "/approve-historical-overage",
            json={"quantity": 10, "comments": "Reconteo técnico del original"},
        ),
        201,
    )
    closed = value(
        fv.client.post(
            path + "/technical-close",
            json={"comments": "Cierre con excedente revisado"},
        ),
        201,
    )
    assert Decimal(closed["difference"]) == 0
    summary = value(fv.client.get(path + "/summary"), 200)
    assert Decimal(summary["difference"]) == 0
    assert Decimal(summary["overage"]) == 10
    value(
        fv.client.post(
            f"/floresvolcan/harvest-records/{historical.id}/void",
            json={"comments": "No se permite cambiar lo cerrado"},
        ),
        409,
    )


def test_release_of_unused_reservation_and_order_completion(fv):
    one = value(fv.plant(100), 201)
    fv.db.expire_all()
    fv.line.quantity_approved = 2500
    fv.line.quantity_requested = 2500
    fv.req.status = "partially_delivered"
    fv.lot.quantity_available = 2500
    fv.lot.quantity_reserved = 500
    fv.db.commit()
    path = f"/floresvolcan/requisition-lines/{fv.line.id}/resolve-material"
    value(
        fv.client.post(
            path,
            json={
                "client_token": str(uuid.uuid4()),
                "kind": "release",
                "quantity": 500,
                "comments": "Material ya no requerido",
            },
        ),
        201,
    )
    value(
        fv.client.post(
            path,
            json={
                "client_token": str(uuid.uuid4()),
                "kind": "release",
                "quantity": 1,
                "comments": "Intento sobre saldo agotado",
            },
        ),
        409,
    )
    value(fv.client.post(f"/floresvolcan/planting-orders/{fv.order.id}/complete"), 409)
    value(
        fv.client.post(
            f"/floresvolcan/production-batches/{one['batch_id']}/technical-close",
            json={
                "comments": "Pérdida verificada",
                "segments": [
                    {
                        "planting_record_id": one["id"],
                        "loss_quantity": 100,
                        "loss_reason": "Daño comprobado antes de cosechar",
                    }
                ],
            },
        ),
        201,
    )
    value(fv.client.post(f"/floresvolcan/planting-orders/{fv.order.id}/complete"), 409)
    value(
        fv.client.post(
            path,
            json={
                "client_token": str(uuid.uuid4()),
                "kind": "return",
                "quantity": 1900,
                "comments": "Material entregado no utilizado",
            },
        ),
        201,
    )
    value(fv.client.post(f"/floresvolcan/planting-orders/{fv.order.id}/complete"), 200)
    value(fv.plant(1), 409)


def test_worker_can_register_but_cannot_close_or_authorize(fv):
    from core.models import AppModule
    from modules.mod_floresvolcan.manifest import MODULE_MANIFEST

    fv.db.add(
        AppModule(
            key="mod_floresvolcan",
            name="Flores Volcán",
            module_type="industry",
            status="active",
            manifest=MODULE_MANIFEST,
        )
    )
    fv.user.is_superadmin = False
    fv.user.rol = "trabajador"
    fv.db.query(m.Worker).filter(m.Worker.user_id==fv.user.id).one().employee_code="2468"
    fv.db.commit()
    one = value(fv.plant(10,worker_code="2468"), 201)
    value(
        fv.client.post(
            f"/floresvolcan/production-batches/{one['batch_id']}/technical-close",
            json={"comments": "Trabajador no puede autorizar pérdidas"},
        ),
        403,
    )
    value(
        fv.client.post(
            f"/floresvolcan/production-batches/{one['batch_id']}/approve-historical-overage",
            json={"quantity": 1, "comments": "Sin autoridad técnica"},
        ),
        403,
    )
    value(fv.plant(10, density=200,worker_code="2468"), 422)


@pytest.mark.skipif(
    not os.getenv("FV_TEST_DATABASE_URL"),
    reason="Requiere PostgreSQL temporal para locks reales",
)
def test_concurrent_plantings_from_different_lots_cannot_overfill_bed(fv):
    from modules.mod_floresvolcan.schemas import PlantingRecordIn

    fv.bed.capacity = 1
    another_lot = m.InventoryLot(
        lot_code="OTHER-LOT",
        supplier_lot_code="OTHER-SOURCE",
        sap_product_id=fv.product.id,
        goods_receipt_line_id=fv.lot.goods_receipt_line_id,
        quantity_received=100,
        quantity_available=0,
        quantity_issued=100,
    )
    another_order = m.PlantingOrder(
        number="OTHER-ORDER",
        production_space_id=fv.bed.id,
        crew_id=fv.crew.id,
        status="approved",
    )
    fv.db.add_all([another_lot, another_order])
    fv.db.flush()
    another_req = m.Requisition(
        number="OTHER-REQ", planting_order_id=another_order.id, status="delivered"
    )
    fv.db.add(another_req)
    fv.db.flush()
    another_line = m.RequisitionLine(
        requisition_id=another_req.id,
        inventory_lot_id=another_lot.id,
        sap_product_id=fv.product.id,
        quantity_requested=100,
        quantity_approved=100,
        quantity_delivered=100,
    )
    fv.db.add(another_line)
    fv.db.commit()
    barrier = Barrier(2)

    def submit(pair):
        db = fv.factory()
        try:
            order_id, line_id = pair
            payload = PlantingRecordIn(
                client_token=str(uuid.uuid4()),
                planting_order_id=order_id,
                requisition_line_id=line_id,
                bed_space_id=fv.bed.id,
                quantity_planted=60,
                planted_at="2026-01-05T12:00:00-06:00",
            )
            barrier.wait(timeout=10)
            try:
                r.register_planting(payload, current=fv.user, db=db)
                return 201
            except Exception as exc:
                db.rollback()
                return getattr(exc, "status_code", str(exc))
        finally:
            db.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses = list(
            pool.map(
                submit, [(fv.order.id, fv.line.id), (another_order.id, another_line.id)]
            )
        )
    assert sorted(statuses) == [201, 409]


@pytest.mark.skipif(
    not os.getenv("FV_TEST_DATABASE_URL"),
    reason="Requiere PostgreSQL temporal para locks reales",
)
def test_concurrent_retry_creates_only_one_planting_and_movement(fv):
    from modules.mod_floresvolcan.schemas import PlantingRecordIn

    barrier = Barrier(2)
    payload = PlantingRecordIn(
        client_token=str(uuid.uuid4()),
        planting_order_id=fv.order.id,
        requisition_line_id=fv.line.id,
        bed_space_id=fv.bed.id,
        quantity_planted=100,
        planted_at="2026-01-05T12:00:00-06:00",
    )

    def submit(_):
        db = fv.factory()
        try:
            barrier.wait(timeout=10)
            return r.register_planting(payload, current=fv.user, db=db)["id"]
        finally:
            db.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        ids = list(pool.map(submit, range(2)))
    assert ids[0] == ids[1]
    assert fv.db.query(m.PlantingRecord).count() == 1
    assert (
        fv.db.query(m.InventoryMovement)
        .filter(m.InventoryMovement.movement_type == "PLANTING_CONSUMPTION")
        .count()
        == 1
    )


def test_material_resolution_retry_is_idempotent(fv):
    path = f"/floresvolcan/requisition-lines/{fv.line.id}/resolve-material"
    body = {
        "client_token": str(uuid.uuid4()),
        "kind": "return",
        "quantity": 100,
        "comments": "Devolución revisada",
    }
    first = value(fv.client.post(path, json=body), 201)
    second = value(fv.client.post(path, json=body), 201)
    assert first["id"] == second["id"] and second["idempotent"]
    value(fv.client.post(path, json={**body, "quantity": 101}), 409)
    fv.db.expire_all()
    assert fv.db.get(m.RequisitionLine, fv.line.id).quantity_returned == 100


@pytest.mark.parametrize("operation", ["planting", "harvest"])
def test_fractional_physical_counts_are_rejected(fv, operation):
    if operation == "planting":
        value(fv.plant(0.5), 422)
    else:
        one = value(fv.plant(100), 201)
        ho = fv.harvest_order(one["batch_id"])
        value(fv.cut(one["batch_id"], ho["id"], 0.5), 422)
