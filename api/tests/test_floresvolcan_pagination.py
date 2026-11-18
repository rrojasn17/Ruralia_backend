"""Bounded lists retain full search coverage, totals and role restrictions."""

from datetime import datetime, timezone

import pytest

from modules.mod_floresvolcan import models as m, router as r
from test_floresvolcan_reconciliation_v07 import fv as fv_fixture, value

fv = fv_fixture


def test_inventory_searches_beyond_first_page_and_keeps_global_totals(fv):
    for i in range(83):
        fv.db.add(
            m.InventoryLot(
                lot_code=f"PAGE-{i:03d}",
                sap_product_id=fv.product.id,
                goods_receipt_line_id=fv.lot.goods_receipt_line_id,
                quantity_received=10,
                quantity_available=10 if i else 0,
                variety_snapshot="Variedad remota" if i == 0 else "Otra variedad",
                received_at=datetime(2020, 1, 1, tzinfo=timezone.utc),
            )
        )
    fv.db.commit()
    first = value(fv.client.get("/floresvolcan/inventory?page=1"), 200)
    second = value(fv.client.get("/floresvolcan/inventory?page=2"), 200)
    assert first["total"] == 84 and len(first["items"]) == 40
    assert first["summary"]["available"] == 3820
    assert not {r["id"] for r in first["items"]} & {r["id"] for r in second["items"]}
    assert second["summary"] == first["summary"]
    found = value(fv.client.get("/floresvolcan/inventory?page=1&q=remota"), 200)
    assert found["total"] == 1 and found["items"][0]["lot_code"] == "PAGE-000"
    assert not value(
        fv.client.get("/floresvolcan/inventory?page=1&q=remota&available_only=true"),
        200,
    )["items"]
    assert len(value(fv.client.get("/floresvolcan/inventory"), 200)) == 84
    selected = value(
        fv.client.get(
            "/floresvolcan/inventory",
            params={"page": 1, "selected_id": found["items"][0]["id"]},
        ),
        200,
    )
    assert selected["total"] == 1


@pytest.mark.parametrize(
    "path",
    [
        "inventory",
        "sap-products",
        "purchase-orders",
        "planting-orders",
        "requisitions",
        "production-spaces",
        "harvest-calendar",
    ],
)
def test_pagination_limits_and_empty_search(fv, path):
    value(fv.client.get(f"/floresvolcan/{path}?page=0"), 422)
    value(fv.client.get(f"/floresvolcan/{path}?page=1&page_size=101"), 422)
    result = value(fv.client.get(f"/floresvolcan/{path}?page=1&q=does-not-exist"), 200)
    assert result["total"] == 0 and result["items"] == []


def test_spaces_search_parent_without_loading_whole_tree(fv):
    parent = m.ProductionSpace(
        code="GREENHOUSE-Z", name="Invernadero remoto", type="greenhouse"
    )
    fv.db.add(parent)
    fv.db.flush()
    fv.bed.parent_id = parent.id
    fv.db.commit()
    result = value(
        fv.client.get(
            "/floresvolcan/production-spaces",
            params={"page": 1, "q": "remoto", "type": "bed"},
        ),
        200,
    )
    assert result["total"] == 1 and result["items"][0]["parent_name"] == parent.name


def test_orders_search_team_and_requisitions_search_order(fv):
    result = value(
        fv.client.get(
            "/floresvolcan/planting-orders",
            params={
                "page": 1,
                "q": fv.db.query(m.Worker).first().name,
                "status": "approved,in_progress",
            },
        ),
        200,
    )
    assert result["total"] == 1
    result = value(
        fv.client.get(
            "/floresvolcan/requisitions", params={"page": 1, "q": fv.order.number}
        ),
        200,
    )
    assert result["total"] == 1
    assert result["items"][0]["planting_order_number"] == fv.order.number
    assert result["items"][0]["lines"][0]["lot_code"] == fv.lot.lot_code
    assert r.list_requisitions(fv.user, fv.db)[0]["id"] == fv.req.id


def test_calendar_global_summary_and_selected_batch(fv):
    planted = value(fv.plant(10), 201)
    batch = fv.db.get(m.ProductionBatch, planted["batch_id"])
    batch.history_review_required = False
    fv.db.commit()
    rows = value(fv.client.get("/floresvolcan/harvest-calendar"), 200)
    page = value(
        fv.client.get("/floresvolcan/harvest-calendar?page=1&page_size=1"), 200
    )
    assert page["total"] == len(rows) == sum(page["summary"].values())
    assert page["items"][0]["planted"] == 10
    selected = value(
        fv.client.get(
            "/floresvolcan/harvest-calendar",
            params={"page": 1, "selected_id": batch.id, "phase": "selectable"},
        ),
        200,
    )
    assert selected["items"][0]["id"] == batch.id
    batch.status = "closed"
    fv.db.commit()
    assert not value(
        fv.client.get("/floresvolcan/harvest-calendar?page=1&phase=active"), 200
    )["items"]
    assert (
        value(fv.client.get("/floresvolcan/harvest-calendar?page=1&phase=closed"), 200)[
            "total"
        ]
        == 1
    )


def test_calendar_reconciles_only_visible_batches_and_searches_beyond_old_limit(
    fv, monkeypatch
):
    for i in range(510):
        fv.db.add(
            m.ProductionBatch(
                code=f"PERF-{i:03d}",
                planting_order_id=fv.order.id,
                sap_product_id=fv.product.id,
                production_space_id=fv.bed.id,
                bed_space_id=fv.bed.id,
                status="closed" if i < 200 else "open",
            )
        )
    fv.db.commit()
    sizes = []
    original = r.bulk_batch_metrics

    def measured(db, batches):
        sizes.append(len(batches))
        return original(db, batches)

    monkeypatch.setattr(r, "bulk_batch_metrics", measured)
    result = value(
        fv.client.get("/floresvolcan/harvest-calendar?page=1&phase=active"), 200
    )
    assert result["total"] == 310 and len(result["items"]) == 40
    assert result["summary"]["closed"] == 200
    assert sizes == [40]
    found = value(
        fv.client.get("/floresvolcan/harvest-calendar?page=1&q=PERF-000&phase=closed"),
        200,
    )
    assert found["total"] == 1 and found["items"][0]["code"] == "PERF-000"


def test_pagination_preserves_role_ceiling(fv):
    from test_floresvolcan_roles_v09 import activate

    activate(fv, "trabajador")
    for path in [
        "inventory",
        "purchase-orders",
        "planting-orders",
        "production-spaces",
        "harvest-calendar",
        "requisitions",
        "sap-products",
        "bed-cycles",
    ]:
        value(fv.client.get(f"/floresvolcan/{path}?page=1&q=TEST"), 403)
    fv.user.rol = "admin"
    fv.db.commit()
    value(fv.client.get("/floresvolcan/sap-products?page=1"), 403)
    value(fv.client.get("/floresvolcan/planting-orders?page=1"), 200)


def test_bed_cycles_pagination_retains_occupancy_and_review(fv):
    value(fv.plant(10), 201)
    rows = value(fv.client.get("/floresvolcan/bed-cycles"), 200)
    result = value(
        fv.client.get("/floresvolcan/bed-cycles?page=1&active_only=true"), 200
    )
    assert result["total"] == 1
    assert result["items"] == rows
    assert result["items"][0]["occupied_m2"] == 0.1
