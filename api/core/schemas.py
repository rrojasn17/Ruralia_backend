from __future__ import annotations

from datetime import datetime
import re
from typing import Any

from pydantic import BaseModel, EmailStr, Field, field_validator

ROLE_RE = re.compile(r"^[a-z][a-z0-9_\-]{1,39}$")


def normalize_role(value: str | None) -> str:
    """Normaliza alias heredados y conserva perfiles aportados por módulos futuros."""
    raw = (value or "operario").strip().lower().replace(" ", "_")
    aliases = {
        "administrador": "admin",
        "superadmin": "admin",
        "operator": "operario",
        "oficina": "administrativo",
    }
    role = aliases.get(raw, raw)
    if not ROLE_RE.fullmatch(role):
        raise ValueError("Rol inválido: use letras minúsculas, números, guion o guion bajo")
    return role



class LoginIn(BaseModel):
    correo: EmailStr
    password: str = Field(min_length=1, max_length=128)


class LoginOut(BaseModel):
    token: str | None = None
    token_type: str = "bearer"
    expires_at: datetime
    user: Any


class ForgotPasswordIn(BaseModel):
    correo: EmailStr


class ResetPasswordIn(BaseModel):
    token: str = Field(min_length=32, max_length=512)
    password: str = Field(min_length=10, max_length=128)


class ChangePasswordIn(BaseModel):
    current_password: str = Field(min_length=1, max_length=128)
    new_password: str = Field(min_length=10, max_length=128)


class MessageOut(BaseModel):
    ok: bool = True
    message: str


class TrainingCompleteIn(BaseModel):
    version: int = Field(ge=1, le=100)


class UsuarioBase(BaseModel):
    nombre: str = Field(min_length=2, max_length=180)
    correo: EmailStr
    rol: str = "operario"
    activo: bool = True
    is_superadmin: bool = False
    firma_url: str | None = Field(default=None, max_length=1000)
    permisos: list[str] = Field(default_factory=list)

    @field_validator("rol")
    @classmethod
    def rol_ok(cls, value: str) -> str:
        return normalize_role(value)


class UsuarioCreate(UsuarioBase):
    password: str = Field(min_length=8, max_length=128)


class UsuarioUpdate(BaseModel):
    nombre: str | None = Field(default=None, min_length=2, max_length=180)
    correo: EmailStr | None = None
    rol: str | None = None
    activo: bool | None = None
    is_superadmin: bool | None = None
    firma_url: str | None = Field(default=None, max_length=1000)
    permisos: list[str] | None = None
    password: str | None = Field(default=None, min_length=8, max_length=128)

    @field_validator("rol")
    @classmethod
    def rol_ok(cls, value: str | None) -> str | None:
        return normalize_role(value) if value is not None else None


class UsuarioOut(BaseModel):
    id: int
    nombre: str
    correo: EmailStr
    rol: str
    activo: bool
    is_superadmin: bool = False
    firma_url: str | None = None
    permisos: list[str] = Field(default_factory=list)
    permisos_asignados: list[str] = Field(default_factory=list)
    onboarding_version: int = 0
    onboarding_current_version: int = 1
    onboarding_required: bool = True
    onboarding_completed_at: datetime | None = None
    created_at: datetime | None = None

    model_config = {"from_attributes": True}


class ConfiguracionOut(BaseModel):
    session_days: int = 30
    app_name: str = "NAVIA"
    app_logo_url: str | None = None
    alert_whatsapp: str | None = None
    alert_email: str | None = None
    can_export_backup: bool = False
    can_import_backup: bool = False
    can_full_reset: bool = False


class ConfiguracionUpdate(BaseModel):
    session_days: int | None = Field(default=None, ge=1, le=365)
    app_name: str | None = Field(default=None, min_length=2, max_length=80)
    alert_whatsapp: str | None = Field(default=None, max_length=80)
    alert_email: str | None = Field(default=None, max_length=180)


class BrandingOut(BaseModel):
    app_name: str = "NAVIA"
    app_logo_url: str | None = None


class ModuleOut(BaseModel):
    key: str
    name: str
    version: str
    module_type: str
    description: str | None = None
    status: str
    built_in: bool
    source: str
    manifest: dict[str, Any] = Field(default_factory=dict)
    installed_at: datetime | None = None
    activated_at: datetime | None = None


class ModuleRuntimeOut(BaseModel):
    active_module: ModuleOut | None = None
    effective_permissions: list[str] = Field(default_factory=list)
    available_modules: list[ModuleOut] = Field(default_factory=list)
    # Manifiestos de runtimes desplegados, aunque todavía no hayan sido importados.
    # El frontend los usa para saber qué rutas pertenecen a un módulo inactivo.
    known_manifests: list[dict[str, Any]] = Field(default_factory=list)
