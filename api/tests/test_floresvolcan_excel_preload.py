from __future__ import annotations

import json
from datetime import date

import pytest
from openpyxl import Workbook
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session

from core.models import Usuario
from database import Base
from modules.mod_floresvolcan import lifecycle, models as m
from modules.mod_floresvolcan.excel_history import (
    prepare_import,
    preload,
    clear_module_data,
)
from modules.mod_floresvolcan.production_control import require_linked_history
from fastapi import HTTPException


@pytest.fixture
def book(tmp_path):
    data = {
        "Entradas": [
            {
                "A": "COD1",
                "B": "Lirio",
                "C": "14/16",
                "D": "LA",
                "E": "2026-01-01",
                "F": "Contenedor 1",
                "G": "Lote 1",
                "H": 2025,
                "I": "SUP",
                "J": 10,
                "K": 100,
                "L": 1000,
            }
        ],
        "Salida Bulbo": [
            {
                "A": "COD1",
                "B": "Lirio",
                "C": "14/16",
                "D": "LA",
                "E": "Contenedor 1",
                "F": "Lote 1",
                "G": "SUP",
                "I": 8,
                "J": 1,
                "K": "2026-01-03",
                "L": 100,
                "M": 800,
            }
        ],
        "Siembra": [
            {
                "A": "KEY1",
                "B": "Lote 1",
                "C": "BG1",
                "D": "A",
                "E": 1,
                "F": "LA",
                "G": "SUP",
                "H": "WH",
                "I": "Lirio",
                "J": 600,
                "K": 2,
                "L": "26-02",
                "M": "2026-01-05",
                "N": "26-04",
                "O": "14/16",
                "P": 100,
                "R": "ADONIS-ERICK",
            }
        ],
        "Corta": [
            {
                "A": "KEY1",
                "B": "Lote 1",
                "C": "BG1",
                "D": 1,
                "E": "LA",
                "F": "SUP",
                "G": "WH",
                "H": "Lirio",
                "I": 600,
                "J": "26-02",
                "K": "26-04",
                "L": "26-04",
                "M": "2026-01-20",
                "N": 20,
                "O": "Daño",
                "P": 200,
                "Q": 0,
                "R": 0,
                "S": 200,
                "T": 220,
            }
        ],
    }

    def make(change=None):
        source = json.loads(json.dumps(data))
        if change:
            change(source)
        wb = Workbook()
        wb.remove(wb.active)
        for name, rows in source.items():
            ws = wb.create_sheet(name)
            for row in rows:
                for col in row:
                    ws[f"{col}1"] = col
            for n, row in enumerate(rows, 2):
                for col, value in row.items():
                    ws[f"{col}{n}"] = value
        path = tmp_path / "Lirio (2).xlsx"
        wb.save(path)
        return path

    return make


@pytest.fixture
def db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path}/preload.db")

    @event.listens_for(engine, "connect")
    def foreign_keys(connection, _):
        connection.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(
            Usuario(
                nombre="Cuenta conservada",
                correo="preload@example.com",
                hash_contrasena="test",
                rol="admin",
                is_superadmin=True,
            )
        )
        session.add(m.ModuleSetting(key="backup.destination", value_text="local"))
        session.commit()
        yield session
    engine.dispose()


def plan(book, change=None):
    return prepare_import(book(change), today=date(2026, 11, 4))


def test_conservation_stock_original_rows_and_no_invented_personal_data(book, db):
    run = preload(db, plan(book))
    db.commit()
    assert json.loads(run.summary_json)["operational"] == dict.fromkeys(
        ["Entradas", "Salida Bulbo", "Siembra", "Corta"], 1
    )
    lot = db.query(m.InventoryLot).one()
    assert (
        lot.quantity_received,
        lot.quantity_available,
        lot.quantity_issued,
        lot.quantity_consumed,
    ) == (1000, 200, 200, 600)
    assert db.query(m.ExcelImportRow).count() == 4
    assert db.query(m.HarvestRecord).one().total_output == 220
    assert db.query(m.Worker).count() == 2
    assert all(
        w.identification is None and w.hire_date is None for w in db.query(m.Worker)
    )
    assert db.query(Usuario).one().nombre == "Cuenta conservada"
    with pytest.raises(HTTPException) as exc:
        require_linked_history(db, db.query(m.ProductionBatch).one())
    assert exc.value.status_code == 409


def test_duplicate_origins_do_not_choose_first_entry(book):
    def change(data):
        extra = dict(data["Entradas"][0], A="COD2", F="Contenedor 2")
        data["Entradas"].append(extra)

    p = plan(book, change)
    assert p.rows["Siembra"][0].destination is None
    assert p.rows["Corta"][0].destination is None
    assert p.rows["Siembra"][0].raw["J"] == 600


def test_duplicate_sowing_keys_leave_cuts_unassigned(book):
    p = plan(book, lambda d: d["Siembra"].append(dict(d["Siembra"][0], D="B", J=100)))
    assert all(r.destination == "planting" for r in p.rows["Siembra"])
    assert p.rows["Corta"][0].destination is None


@pytest.mark.parametrize("column,value", [("Q", "19+"), ("T", "SA"), ("N", -1)])
def test_invalid_counts_are_preserved_and_never_changed_to_zero(book, column, value):
    p = plan(book, lambda d: d["Corta"][0].update({column: value}))
    row = p.rows["Corta"][0]
    assert row.destination is None and row.raw[column] == value


def test_inconsistent_sales_are_not_recalculated_silently(book):
    p = plan(book, lambda d: d["Corta"][0].update(S=199))
    assert p.rows["Corta"][0].destination is None
    assert p.rows["Corta"][0].raw["S"] == 199


def test_overage_is_not_approved_automatically(book):
    p = plan(book, lambda d: d["Corta"][0].update(P=700, S=700, T=720))
    assert p.rows["Corta"][0].destination is None
    assert "referencia" in p.rows["Corta"][0].issues[0]


def test_future_date_is_preserved_in_review(book):
    p = plan(book, lambda d: d["Siembra"][0].update(M="2026-11-17"))
    assert p.rows["Siembra"][0].destination is None
    assert p.rows["Siembra"][0].raw["M"] == "2026-11-17"


def test_duplicate_cuts_are_both_preserved_for_review(book):
    p = plan(book, lambda d: d["Corta"].append(dict(d["Corta"][0])))
    assert len(p.rows["Corta"]) == 2
    assert all(r.destination is None for r in p.rows["Corta"])


def test_outgoing_and_planting_never_create_negative_stock(book, db):
    p = plan(book, lambda d: d["Salida Bulbo"][0].update(I=12, M=1200))
    preload(db, p)
    db.commit()
    lot = db.query(m.InventoryLot).one()
    assert (lot.quantity_available, lot.quantity_issued, lot.quantity_consumed) == (
        1000,
        0,
        0,
    )
    assert db.query(m.PlantingRecord).count() == 0
    assert db.query(m.ExcelImportRow).filter_by(status="review").count() == 3


def test_second_workbook_is_provenance_not_additional_transactions(book, db, tmp_path):
    source = book()
    comparison = tmp_path / "Lirio.xlsx"
    comparison.write_bytes(source.read_bytes())
    run = preload(
        db, prepare_import(source, today=date(2026, 11, 4)), comparison=comparison
    )
    db.commit()
    assert run.comparison_filename == "Lirio.xlsx"
    assert (
        db.query(m.InventoryLot).count() == 1
        and db.query(m.ExcelImportRow).count() == 4
    )
    with pytest.raises(ValueError):
        preload(db, prepare_import(source, today=date(2026, 11, 4)))


def test_replace_rollback_preserves_accounts_settings_and_old_data(book, db):
    preload(db, plan(book))
    db.commit()
    old_id = db.query(m.InventoryLot).one().id
    clear_module_data(db)
    db.rollback()
    db.expire_all()
    assert db.query(m.InventoryLot).one().id == old_id
    assert db.query(Usuario).count() == 1
    assert (
        db.query(m.ModuleSetting).filter_by(key="backup.destination").one().value_text
        == "local"
    )


def test_replacing_module_does_not_remove_accounts_or_backup_settings(book, db):
    preload(db, plan(book))
    db.commit()
    clear_module_data(db)
    db.expunge_all()
    preload(db, plan(book))
    db.commit()
    assert db.query(Usuario).count() == 1
    assert db.query(m.ModuleSetting).filter_by(key="backup.destination").count() == 1
    assert db.query(m.ExcelImportRun).count() == 1
    assert db.query(m.ExcelImportRow).count() == 4


def test_restart_keeps_the_chosen_excel_catalogs_and_history_protection(book, db):
    preload(db, plan(book))
    db.commit()
    before = (
        db.query(m.ProductionSpace).count(),
        db.query(m.CropCatalogValue).count(),
        db.query(m.Supplier).count(),
    )
    lifecycle.install(db)
    after = (
        db.query(m.ProductionSpace).count(),
        db.query(m.CropCatalogValue).count(),
        db.query(m.Supplier).count(),
    )
    assert after == before
    assert db.query(m.ProductionBatch).one().history_review_required
