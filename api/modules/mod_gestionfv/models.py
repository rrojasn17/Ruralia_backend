from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, Integer, String, Text

from database import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class GestionFVSetting(Base):
    """Tabla mínima propia del módulo para validar instalación aislada.

    No modela todavía procesos florícolas. La siguiente fase agregará entidades
    de producción/exportación dentro de este mismo namespace.
    """

    __tablename__ = "gestionfv_module_settings"

    id = Column(Integer, primary_key=True)
    clave = Column(String(120), nullable=False, unique=True, index=True)
    valor = Column(Text, nullable=True)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow)
