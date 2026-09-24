from __future__ import annotations

import logging
import os

from sqlalchemy import text

from config import APP_NAME, APP_VERSION, validate_runtime_config
from core.module_manager import _call_lifecycle, get_active_industry_module, sync_builtin_modules
from core.models import ConfiguracionSistema, PasswordResetToken, SesionUsuario, Usuario
from database import Base, SessionLocal, engine

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("navia-schema-upgrade")

CORE_TABLES = [
    ConfiguracionSistema.__table__,
    Usuario.__table__,
    SesionUsuario.__table__,
    PasswordResetToken.__table__,
]


def _upgrade_core() -> None:
    # AppModule se registra al importar core.module_manager; tomamos además todas
    # las tablas cuyo namespace es explícitamente core.
    from core.models import AppModule

    tables = [*CORE_TABLES, AppModule.__table__]
    Base.metadata.create_all(bind=engine, tables=tables)
    if engine.dialect.name != "postgresql":
        return
    statements = [
        "ALTER TABLE navia_usuarios ADD COLUMN IF NOT EXISTS firma_url VARCHAR(1000)",
        "ALTER TABLE navia_usuarios ADD COLUMN IF NOT EXISTS permisos JSON",
        "ALTER TABLE navia_usuarios ADD COLUMN IF NOT EXISTS onboarding_version INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE navia_usuarios ADD COLUMN IF NOT EXISTS onboarding_completed_at TIMESTAMP WITH TIME ZONE",
        "ALTER TABLE navia_usuarios ADD COLUMN IF NOT EXISTS is_superadmin BOOLEAN NOT NULL DEFAULT FALSE",
    ]
    with engine.begin() as connection:
        for statement in statements:
            connection.execute(text(statement))


def main() -> int:
    validate_runtime_config()
    _upgrade_core()

    db = SessionLocal()
    try:
        sync_builtin_modules(db)
        active = get_active_industry_module(db)
        if active:
            _call_lifecycle(active.key, "upgrade_schema", db)
    finally:
        db.close()

    logger.info(
        "Esquema de %s %s verificado/actualizado; módulo industria=%s",
        APP_NAME,
        APP_VERSION,
        active.key if active else "ninguno",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
