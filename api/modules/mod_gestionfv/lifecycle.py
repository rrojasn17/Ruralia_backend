from __future__ import annotations

from sqlalchemy.orm import Session

from database import Base
from modules.mod_gestionfv.models import GestionFVSetting


def _tables():
    return [GestionFVSetting.__table__]


def install(db: Session) -> None:
    Base.metadata.create_all(bind=db.get_bind(), tables=_tables())
    row = db.query(GestionFVSetting).filter(GestionFVSetting.clave == "module_version").first()
    if not row:
        db.add(GestionFVSetting(clave="module_version", valor="0.1.0"))
    else:
        row.valor = "0.1.0"
    db.commit()


def upgrade_schema(db: Session) -> None:
    install(db)


def activate(db: Session) -> None:
    install(db)


def deactivate(db: Session) -> None:
    del db


def uninstall(db: Session) -> None:
    # No destructivo: los datos de una industria nunca se eliminan al desactivar.
    del db


def user_delete_blockers(db: Session, user_id: int) -> list[str]:
    # En esta fase GestiónFV aún no tiene entidades que referencien usuarios.
    del db, user_id
    return []
