"""Explicit, module-scoped replacement from a reviewed historical XLSX.

Default is a dry run. --apply-replace first writes and verifies a private module
snapshot, then replaces module data in one transaction. Core accounts, settings,
backups and all other industries are preserved. Original files are read-only.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import func, text

from core.models import AppModule, Usuario  # register core foreign keys
from database import SessionLocal
from . import lifecycle, models as m
from .backups import _module_snapshot
from .excel_history import prepare_import, preload, clear_module_data


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--comparison", type=Path)
    parser.add_argument("--apply-replace", action="store_true")
    parser.add_argument("--backup-dir", type=Path)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    plan = prepare_import(args.source)
    summary = plan.summary()
    if not args.apply_replace:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return
    if args.backup_dir is None:
        parser.error("--apply-replace requiere --backup-dir persistente")
    with SessionLocal() as db:
        active = (
            db.query(AppModule)
            .filter_by(key="mod_floresvolcan", status="active")
            .first()
        )
        if active is None:
            raise RuntimeError(
                "Flores Volcán debe ser el módulo activo para reemplazar sus datos"
            )
        lifecycle.install(db)
        if db.get_bind().dialect.name == "postgresql":
            db.execute(
                text("SELECT pg_advisory_xact_lock(hashtext('floresvolcan-preload'))")
            )
        before = _module_snapshot(db)
        payload = json.dumps(before, ensure_ascii=False, indent=2).encode()
        args.backup_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        target = args.backup_dir / (
            "floresvolcan-before-excel-"
            + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            + ".json"
        )
        with target.open("xb") as handle:
            os.chmod(target, 0o600)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        if target.read_bytes() != payload or json.loads(
            target.read_bytes()
        ) != json.loads(payload):
            raise RuntimeError("No se pudo verificar el respaldo; no se borraron datos")
        users_before = db.query(func.count(Usuario.id)).scalar()
        clear_module_data(db)
        db.expunge_all()  # discard identity-map entries of the deleted rows
        run = preload(db, plan, comparison=args.comparison)
        users_after = db.query(func.count(Usuario.id)).scalar()
        if users_before != users_after:
            raise RuntimeError("Cambió la cantidad de cuentas; se revierte la carga")
        # The inventory equation must hold for every imported origin.
        bad = (
            db.query(m.InventoryLot.id)
            .filter(
                (m.InventoryLot.quantity_available < 0)
                | (m.InventoryLot.quantity_issued < 0)
                | (
                    m.InventoryLot.quantity_received
                    != m.InventoryLot.quantity_available
                    + m.InventoryLot.quantity_reserved
                    + m.InventoryLot.quantity_issued
                    + m.InventoryLot.quantity_consumed
                    + m.InventoryLot.quantity_written_off
                )
            )
            .first()
        )
        if bad:
            raise RuntimeError(
                "La carga no cumple la conciliación del inventario; se revierte"
            )
        if db.query(m.ExcelImportRow).count() != sum(
            len(rows) for rows in plan.rows.values()
        ):
            raise RuntimeError(
                "No se conservaron todas las filas originales; se revierte"
            )
        summary = json.loads(run.summary_json)
        summary["backup"] = str(target)
        summary["backup_sha256"] = hashlib.sha256(payload).hexdigest()
        summary["accounts_preserved"] = users_after
        summary["removed_counts"] = {
            name: len(rows)
            for name, rows in before.items()
            if name not in {"fv_module_settings", "fv_backup_runs"}
        }
        db.commit()
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(summary, ensure_ascii=False, indent=2))
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
