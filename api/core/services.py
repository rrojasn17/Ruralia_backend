from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import text
from sqlalchemy.orm import Session

from config import DEFAULT_SESSION_DAYS, TRAINING_CURRENT_VERSION
from core.models import ConfiguracionSistema, PasswordResetToken, SesionUsuario, Usuario
from core.schemas import UsuarioOut
from security import generar_token, hash_token, hashear_contrasena, verificar_contrasena

CORE_ROLE_PERMISSIONS = {
    # Solo perfiles realmente globales. Los perfiles de industria los aporta el
    # manifiesto del módulo activo. Roles desconocidos quedan sin permisos core (default deny).
    "admin": ["core:dashboard:view", "core:users:manage", "core:config:manage"],
    "gerente": ["core:dashboard:view"],
    "operario": ["core:dashboard:view"],
    "trabajador": [],
}


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def get_config_int(db: Session, key: str, default: int) -> int:
    row = db.query(ConfiguracionSistema).filter(ConfiguracionSistema.clave == key).first()
    if not row:
        return default
    try:
        return int(row.valor)
    except Exception:
        return default


def get_config_value(db: Session, key: str, default: str = "") -> str:
    row = db.query(ConfiguracionSistema).filter(ConfiguracionSistema.clave == key).first()
    return str(row.valor or default) if row else default


def set_config(db: Session, key: str, value: str, descripcion: str | None = None) -> ConfiguracionSistema:
    row = db.query(ConfiguracionSistema).filter(ConfiguracionSistema.clave == key).first()
    if not row:
        row = ConfiguracionSistema(clave=key, valor=str(value), descripcion=descripcion)
        db.add(row)
    else:
        row.valor = str(value)
        if descripcion is not None:
            row.descripcion = descripcion
    return row


def create_session(db: Session, usuario: Usuario) -> tuple[SesionUsuario, str]:
    days = get_config_int(db, "session_days", DEFAULT_SESSION_DAYS)
    raw_token = generar_token(48)
    session = SesionUsuario(usuario_id=usuario.id, token=hash_token(raw_token), expires_at=utcnow() + timedelta(days=days))
    db.add(session)
    db.flush()
    return session, raw_token


def core_permissions_for_user(user: Usuario) -> list[str]:
    if bool(getattr(user, "is_superadmin", False)):
        return ["*"]
    base = CORE_ROLE_PERMISSIONS.get((user.rol or "operario").lower(), [])
    explicit = [str(p).strip() for p in (getattr(user, "permisos", None) or []) if str(p).strip()]
    return sorted(set(base + explicit))


def usuario_out(user: Usuario, extra_permissions: list[str] | None = None) -> UsuarioOut:
    permissions = core_permissions_for_user(user)
    if extra_permissions and "*" not in permissions:
        permissions = sorted(set(permissions + extra_permissions))
    onboarding_version = int(getattr(user, "onboarding_version", 0) or 0)
    return UsuarioOut(
        id=user.id,
        nombre=user.nombre,
        correo=user.correo,
        rol=user.rol,
        activo=bool(user.activo),
        is_superadmin=bool(getattr(user, "is_superadmin", False)),
        firma_url=getattr(user, "firma_url", None),
        permisos=permissions,
        permisos_asignados=[str(p) for p in (getattr(user, "permisos", None) or [])],
        onboarding_version=onboarding_version,
        onboarding_current_version=TRAINING_CURRENT_VERSION,
        onboarding_required=onboarding_version < TRAINING_CURRENT_VERSION,
        onboarding_completed_at=getattr(user, "onboarding_completed_at", None),
        created_at=user.created_at,
    )


def _ensure_user_columns(db: Session) -> None:
    statements = [
        "ALTER TABLE navia_usuarios ADD COLUMN IF NOT EXISTS firma_url VARCHAR(1000)",
        "ALTER TABLE navia_usuarios ADD COLUMN IF NOT EXISTS permisos JSON",
        "ALTER TABLE navia_usuarios ADD COLUMN IF NOT EXISTS onboarding_version INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE navia_usuarios ADD COLUMN IF NOT EXISTS onboarding_completed_at TIMESTAMP WITH TIME ZONE",
        "ALTER TABLE navia_usuarios ADD COLUMN IF NOT EXISTS is_superadmin BOOLEAN NOT NULL DEFAULT FALSE",
    ]
    try:
        for statement in statements:
            db.execute(text(statement))
        db.commit()
    except Exception:
        db.rollback()


def bootstrap_core(
    db: Session,
    admin_email: str,
    admin_password: str,
    admin_name: str,
    session_days: int,
    sync_admin_password: bool = False,
) -> None:
    _ensure_user_columns(db)
    set_config(db, "session_days", str(session_days), "Duración de sesión en días")

    admin = db.query(Usuario).filter(Usuario.correo == admin_email).first()
    if not admin:
        admin = Usuario(
            nombre=admin_name,
            correo=admin_email,
            hash_contrasena=hashear_contrasena(admin_password),
            rol="admin",
            activo=True,
            is_superadmin=True,
            permisos=["*"],
        )
        db.add(admin)
    else:
        admin.nombre = admin_name or admin.nombre
        admin.rol = "admin"
        admin.activo = True
        admin.is_superadmin = True
        admin.permisos = ["*"]
        if sync_admin_password and not verificar_contrasena(admin_password, admin.hash_contrasena):
            admin.hash_contrasena = hashear_contrasena(admin_password)
            now = utcnow()
            for session in db.query(SesionUsuario).filter(SesionUsuario.usuario_id == admin.id, SesionUsuario.revoked_at.is_(None)).all():
                session.revoked_at = now

    session_cleanup_cutoff = utcnow() - timedelta(days=30)
    db.query(SesionUsuario).filter(
        (SesionUsuario.expires_at < session_cleanup_cutoff) | (SesionUsuario.revoked_at < session_cleanup_cutoff)
    ).delete(synchronize_session=False)
    db.query(PasswordResetToken).filter(PasswordResetToken.created_at < utcnow() - timedelta(days=7)).delete(synchronize_session=False)
    db.commit()
