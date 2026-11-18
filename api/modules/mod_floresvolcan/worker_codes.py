"""Códigos aleatorios únicos. El código anterior se conserva como referencia."""

import re
import secrets
from pathlib import Path
from fastapi import HTTPException
from sqlalchemy import text
from .models import Worker


def allocate_worker_code(db):
    db.flush()
    if db.get_bind().dialect.name == "postgresql":
        db.execute(text("LOCK TABLE fv_workers IN SHARE ROW EXCLUSIVE MODE"))
    used = {x[0] for x in db.query(Worker.employee_code)}
    available = [str(x) for x in range(1000, 10000) if str(x) not in used]
    if not available:
        raise HTTPException(409, "Se agotaron los códigos de cuatro dígitos")
    return secrets.choice(available)


def normalize_worker_codes(db):
    for worker in db.query(Worker).order_by(Worker.id):
        if not re.fullmatch(r"[0-9]{4}", worker.employee_code or ""):
            worker.legacy_employee_code = (
                worker.legacy_employee_code or worker.employee_code
            )
            worker.employee_code = allocate_worker_code(db)
            db.flush()
    db.commit()


def install_worker_code_guard(db):
    """Protege también las escrituras de importadores de versiones anteriores."""
    if db.get_bind().dialect.name != "postgresql":
        return
    migration = Path(__file__).with_name("migrations") / "worker_codes_4_digits.sql"
    db.execute(text(migration.read_text()))
