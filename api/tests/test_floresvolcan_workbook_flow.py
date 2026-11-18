"""Workbook totals remain distinct from live balances and retain lot provenance."""

from sqlalchemy import event

from modules.mod_floresvolcan import router as r
from test_floresvolcan_reconciliation_v07 import fv as fv_fixture, value

fv = fv_fixture


def test_workbook_totals_distinguish_receipts_deliveries_and_remaining_stock(fv):
    planted = value(fv.plant(10), 201)
    summary = value(fv.client.get("/floresvolcan/management-dashboard"), 200)["kpis"]
    assert summary["total_received"] == 5000
    assert summary["total_issued"] == 2000
    assert summary["total_planted"] == 10
    assert summary["inventory_available"] == 3000
    assert summary["inventory_in_field"] == 1990
    order = fv.harvest_order(planted["batch_id"])
    value(
        fv.cut(
            planted["batch_id"],
            order["id"],
            qty=2,
            discard_qty=1,
            discard_reason="Daño",
        ),
        201,
    )
    summary = value(fv.client.get("/floresvolcan/management-dashboard"), 200)["kpis"]
    assert summary["total_harvested"] == 3
    assert summary["total_sale"] == 2
    assert summary["total_discarded"] == 1
    assert summary["total_issued"] == 2000


def test_bulb_issue_list_keeps_provenance_with_three_queries(fv):
    calls = []

    def record(*args):
        calls.append(args[2])

    event.listen(fv.engine, "before_cursor_execute", record)
    try:
        rows = r.list_requisitions(fv.user, fv.db)
    finally:
        event.remove(fv.engine, "before_cursor_execute", record)
    row = next(row for row in rows if row["id"] == fv.req.id)
    assert row["planting_order_id"] == fv.order.id
    assert row["lines"][0]["lot_code"] == fv.lot.lot_code
    assert row["lines"][0]["product_code"] == fv.product.code
    assert row["lines"][0]["quantity_delivered"] == 2000
    assert len(calls) == 3
