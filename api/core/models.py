from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Index, Integer, String, Text, func, text
from sqlalchemy.orm import relationship
from sqlalchemy.types import JSON

from database import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ConfiguracionSistema(Base):
    __tablename__ = "navia_configuracion_sistema"

    id = Column(Integer, primary_key=True)
    clave = Column(String(120), nullable=False, unique=True, index=True)
    valor = Column(String(500), nullable=False)
    descripcion = Column(String(500), nullable=True)
    updated_at = Column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)


class Usuario(Base):
    """Identidad global de la instancia.

    Los usuarios pertenecen al core, no a un módulo de industria. ``rol`` mantiene
    compatibilidad con el sistema actual y ``permisos`` permite excepciones globales
    o por módulo sin duplicar usuarios al cambiar de industria.
    """

    __tablename__ = "navia_usuarios"

    id = Column(Integer, primary_key=True, index=True)
    nombre = Column(String(180), nullable=False)
    correo = Column(String(180), nullable=False, unique=True, index=True)
    hash_contrasena = Column(String(255), nullable=False)
    rol = Column(String(40), nullable=False, default="operario", index=True)
    activo = Column(Boolean, nullable=False, default=True)
    is_superadmin = Column(Boolean, nullable=False, default=False, server_default="false", index=True)
    firma_url = Column(String(1000), nullable=True)
    permisos = Column(JSON, nullable=False, default=list)
    onboarding_version = Column(Integer, nullable=False, default=0, server_default="0")
    onboarding_completed_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)

    sesiones = relationship("SesionUsuario", back_populates="usuario", cascade="all, delete-orphan")


class SesionUsuario(Base):
    __tablename__ = "navia_sesiones_usuario"

    id = Column(Integer, primary_key=True)
    usuario_id = Column(Integer, ForeignKey("navia_usuarios.id", ondelete="CASCADE"), nullable=False, index=True)
    token = Column(String(255), nullable=False, unique=True, index=True)
    expires_at = Column(DateTime(timezone=True), nullable=False, index=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    last_used_at = Column(DateTime(timezone=True), nullable=True)
    revoked_at = Column(DateTime(timezone=True), nullable=True)

    usuario = relationship("Usuario", back_populates="sesiones")


class PasswordResetToken(Base):
    __tablename__ = "navia_password_reset_tokens"

    id = Column(Integer, primary_key=True)
    usuario_id = Column(Integer, ForeignKey("navia_usuarios.id", ondelete="CASCADE"), nullable=False, index=True)
    token_hash = Column(String(64), nullable=False, unique=True, index=True)
    expires_at = Column(DateTime(timezone=True), nullable=False, index=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    used_at = Column(DateTime(timezone=True), nullable=True, index=True)
    requested_ip = Column(String(80), nullable=True)

    usuario = relationship("Usuario")


class AppModule(Base):
    """Catálogo persistente de módulos instalados en una instancia."""

    __tablename__ = "core_app_modules"
    __table_args__ = (
        Index(
            "uq_core_single_active_industry",
            "module_type",
            unique=True,
            postgresql_where=text("status = 'active' AND module_type = 'industry'"),
            sqlite_where=text("status = 'active' AND module_type = 'industry'"),
        ),
    )

    id = Column(Integer, primary_key=True)
    key = Column(String(120), nullable=False, unique=True, index=True)
    name = Column(String(180), nullable=False)
    version = Column(String(40), nullable=False, default="1.0.0")
    module_type = Column(String(40), nullable=False, default="industry", index=True)
    description = Column(Text, nullable=True)
    status = Column(String(30), nullable=False, default="installed", index=True)  # installed|active|disabled
    built_in = Column(Boolean, nullable=False, default=False)
    source = Column(String(80), nullable=False, default="builtin")
    manifest = Column(JSON, nullable=False, default=dict)
    package_path = Column(String(1000), nullable=True)
    checksum_sha256 = Column(String(64), nullable=True)
    installed_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)
    activated_at = Column(DateTime(timezone=True), nullable=True)
    deactivated_at = Column(DateTime(timezone=True), nullable=True)
    updated_at = Column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)
