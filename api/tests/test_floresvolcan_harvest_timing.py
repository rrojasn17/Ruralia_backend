from datetime import date, timedelta
from modules.mod_floresvolcan.harvest_timing import harvest_timing
from modules.mod_floresvolcan.models import ProductionSpace
from test_floresvolcan_reconciliation_v07 import fv as fv_fixture, value

fv = fv_fixture


def test_three_week_boundary_due_and_overdue():
    planted = date(2026, 1, 5)
    due = planted + timedelta(weeks=12)
    assert (
        harvest_timing(planted, 12, due - timedelta(days=22))["readiness"] == "growing"
    )
    soon = harvest_timing(planted, 12, due - timedelta(days=21))
    assert soon["readiness"] == "upcoming" and soon["weeks_elapsed"] == 9
    assert harvest_timing(planted, 12, due)["readiness"] == "ready"
    assert harvest_timing(planted, 12, due + timedelta(days=5))["readiness"] == "ready"
    unknown = harvest_timing(planted, None, due)
    assert unknown["harvest_due_date"] is None and unknown["readiness"] == "growing"


def test_calendar_uses_frozen_record_cycle_and_full_location(fv):
    farm = ProductionSpace(code="FARM", name="Finca", type="farm", status="active")
    fv.db.add(farm)
    fv.db.flush()
    fv.bed.parent_id = farm.id
    fv.db.commit()
    value(fv.plant(10), 201)
    fv.product.default_cycle_weeks = 40
    fv.db.commit()
    row = value(fv.client.get("/floresvolcan/harvest-calendar"), 200)[0]
    assert row["cycle_weeks"] == 2
    assert row["harvest_due_date"] == "2026-01-19"
    assert row["weeks_elapsed"] >= 0
    assert "Finca" in row["location_label"] and fv.bed.name in row["location_label"]
