from __future__ import annotations

from datetime import timedelta, timezone
from typing import Optional
from urllib.parse import quote

from fastapi import APIRouter, BackgroundTasks, Cookie, Depends, Header, HTTPException, Request, Response, status
from sqlalchemy.orm import Session

from config import (
    APP_VERSION,
    AUTH_RATE_WINDOW_SECONDS,
    COOKIE_DOMAIN,
    COOKIE_HTTPONLY,
    COOKIE_NAME,
    COOKIE_PATH,
    COOKIE_SAMESITE,
    COOKIE_SECURE,
    EXPOSE_AUTH_TOKEN,
    LOGIN_RATE_LIMIT,
    PASSWORD_RESET_ENABLED,
    PASSWORD_RESET_RATE_LIMIT,
    PASSWORD_RESET_TOKEN_MINUTES,
    PASSWORD_RESET_URL,
    SESSION_TOUCH_INTERVAL_SECONDS,
    TRAINING_CURRENT_VERSION,
    smtp_is_configured,
)
from database import get_db
from core.models import PasswordResetToken, SesionUsuario, Usuario
from rate_limit import client_ip, enforce_rate_limit
from core.schemas import (
    ChangePasswordIn,
    ForgotPasswordIn,
    LoginIn,
    LoginOut,
    MessageOut,
    ResetPasswordIn,
    TrainingCompleteIn,
    UsuarioOut,
)
from security import (
    generar_token,
    hash_token,
    hashear_contrasena,
    validar_fortaleza_contrasena,
    verificar_contrasena,
    verificar_contrasena_dummy,
)
from services.email_service import send_password_reset_email
from core.services import core_permissions_for_user, create_session, get_config_int, usuario_out, utcnow

router = APIRouter(prefix="/auth", tags=["auth"])


def _normalize_dt(dt):
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _revoke_user_sessions(db: Session, user_id: int) -> None:
    now = utcnow()
    sessions = (
        db.query(SesionUsuario)
        .filter(SesionUsuario.usuario_id == user_id, SesionUsuario.revoked_at.is_(None))
        .all()
    )
    for session in sessions:
        session.revoked_at = now


def get_current_user(
    request: Request,
    db: Session = Depends(get_db),
    authorization: Optional[str] = Header(default=None, alias="Authorization"),
    x_api_token: Optional[str] = Header(default=None, alias="X-API-Token"),
    cookie_token: Optional[str] = Cookie(default=None, alias=COOKIE_NAME),
) -> Usuario:
    token = None
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization.split(" ", 1)[1].strip()
    token = token or x_api_token or cookie_token
    if not token:
        raise HTTPException(status_code=401, detail="No autenticado")

    token_digest = hash_token(token)
    session_row = (
        db.query(SesionUsuario, Usuario)
        .join(Usuario, Usuario.id == SesionUsuario.usuario_id)
        .filter(
            SesionUsuario.token.in_([token_digest, token]),
            SesionUsuario.revoked_at.is_(None),
        )
        .first()
    )
    if not session_row:
        raise HTTPException(status_code=401, detail="Token inválido")
    session, user = session_row
    now = utcnow()
    session_changed = False
    if session.token == token:
        # Migración transparente de sesiones antiguas almacenadas en texto plano.
        session.token = token_digest
        session_changed = True
    if _normalize_dt(session.expires_at) <= now:
        session.revoked_at = now
        db.commit()
        raise HTTPException(status_code=401, detail="Sesión expirada")

    if not user or not user.activo:
        raise HTTPException(status_code=403, detail="Usuario desactivado")

    last_used_at = _normalize_dt(session.last_used_at)
    is_initial_check = request.url.path.rstrip("/").endswith("/auth/me")
    if not is_initial_check and (
        last_used_at is None or (now - last_used_at).total_seconds() >= SESSION_TOUCH_INTERVAL_SECONDS
    ):
        session.last_used_at = now
        session_changed = True
    if session_changed:
        db.commit()
    return user


def require_roles(*roles: str):
    allowed = {r.lower() for r in roles}

    def dep(current: Usuario = Depends(get_current_user)) -> Usuario:
        if bool(getattr(current, "is_superadmin", False)):
            return current
        if current.rol == "gerente" or current.rol in allowed or (current.rol == "admin" and "gerente" in allowed):
            return current
        raise HTTPException(status_code=403, detail="No autorizado para esta acción")

    return dep


def require_core_permission(permission: str):
    """Autoriza capacidades del core sin acoplarlas a perfiles de industria."""
    def dep(current: Usuario = Depends(get_current_user)) -> Usuario:
        permissions = set(core_permissions_for_user(current))
        if "*" in permissions or permission in permissions:
            return current
        raise HTTPException(status_code=403, detail=f"Permiso requerido: {permission}")

    return dep


@router.get("/status")
def auth_status():
    return {
        "ok": True,
        "module": "auth",
        "version": APP_VERSION,
        "password_reset_enabled": PASSWORD_RESET_ENABLED and smtp_is_configured(),
    }


@router.post("/login", response_model=LoginOut)
def login(payload: LoginIn, request: Request, response: Response, db: Session = Depends(get_db)):
    enforce_rate_limit(request, "auth-login", LOGIN_RATE_LIMIT, AUTH_RATE_WINDOW_SECONDS)

    correo = payload.correo.strip().lower()
    user = db.query(Usuario).filter(Usuario.correo == correo).first()

    if not user:
        verificar_contrasena_dummy(payload.password)
        raise HTTPException(status_code=401, detail="Credenciales inválidas")

    if not verificar_contrasena(payload.password, user.hash_contrasena):
        raise HTTPException(status_code=401, detail="Credenciales inválidas")
    if not user.activo:
        raise HTTPException(status_code=403, detail="Usuario desactivado")

    session, raw_session_token = create_session(db, user)
    db.commit()
    max_age = get_config_int(db, "session_days", 30) * 24 * 60 * 60
    response.set_cookie(
        key=COOKIE_NAME,
        value=raw_session_token,
        max_age=max_age,
        expires=session.expires_at,
        httponly=COOKIE_HTTPONLY,
        samesite=COOKIE_SAMESITE,
        secure=COOKIE_SECURE,
        domain=COOKIE_DOMAIN,
        path=COOKIE_PATH,
    )
    return LoginOut(token=raw_session_token if EXPOSE_AUTH_TOKEN else None, expires_at=session.expires_at, user=usuario_out(user))


@router.post("/forgot-password", response_model=MessageOut, status_code=status.HTTP_202_ACCEPTED)
def forgot_password(
    payload: ForgotPasswordIn,
    request: Request,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    enforce_rate_limit(request, "auth-forgot-password", PASSWORD_RESET_RATE_LIMIT, AUTH_RATE_WINDOW_SECONDS)

    if not PASSWORD_RESET_ENABLED or not smtp_is_configured():
        raise HTTPException(status_code=503, detail="La recuperación de contraseña no está configurada")

    correo = payload.correo.strip().lower()
    user = db.query(Usuario).filter(Usuario.correo == correo, Usuario.activo.is_(True)).first()

    if user:
        now = utcnow()
        previous_tokens = (
            db.query(PasswordResetToken)
            .filter(PasswordResetToken.usuario_id == user.id, PasswordResetToken.used_at.is_(None))
            .all()
        )
        for previous in previous_tokens:
            previous.used_at = now

        raw_token = generar_token(48)
        db.add(
            PasswordResetToken(
                usuario_id=user.id,
                token_hash=hash_token(raw_token),
                expires_at=now + timedelta(minutes=PASSWORD_RESET_TOKEN_MINUTES),
                requested_ip=client_ip(request),
            )
        )
        db.commit()

        separator = "&" if "?" in PASSWORD_RESET_URL else "?"
        reset_url = f"{PASSWORD_RESET_URL}{separator}token={quote(raw_token, safe='')}"
        background_tasks.add_task(send_password_reset_email, user.correo, user.nombre, reset_url)

    return MessageOut(
        message="Si el correo corresponde a una cuenta activa, recibirá instrucciones para restablecer la contraseña."
    )


@router.post("/reset-password", response_model=MessageOut)
def reset_password(payload: ResetPasswordIn, request: Request, db: Session = Depends(get_db)):
    enforce_rate_limit(request, "auth-reset-password", PASSWORD_RESET_RATE_LIMIT * 2, AUTH_RATE_WINDOW_SECONDS)

    try:
        validar_fortaleza_contrasena(payload.password)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    now = utcnow()
    reset = (
        db.query(PasswordResetToken)
        .filter(
            PasswordResetToken.token_hash == hash_token(payload.token),
            PasswordResetToken.used_at.is_(None),
        )
        .first()
    )

    if not reset or _normalize_dt(reset.expires_at) <= now:
        raise HTTPException(status_code=400, detail="El enlace es inválido o ya venció")

    user = db.query(Usuario).filter(Usuario.id == reset.usuario_id, Usuario.activo.is_(True)).first()
    if not user:
        raise HTTPException(status_code=400, detail="El enlace es inválido o ya venció")

    user.hash_contrasena = hashear_contrasena(payload.password)
    reset.used_at = now
    _revoke_user_sessions(db, user.id)

    other_tokens = (
        db.query(PasswordResetToken)
        .filter(
            PasswordResetToken.usuario_id == user.id,
            PasswordResetToken.id != reset.id,
            PasswordResetToken.used_at.is_(None),
        )
        .all()
    )
    for other in other_tokens:
        other.used_at = now

    db.commit()
    return MessageOut(message="Contraseña actualizada. Ya puede iniciar sesión.")


@router.post("/change-password", response_model=MessageOut)
def change_password(
    payload: ChangePasswordIn,
    current: Usuario = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if not verificar_contrasena(payload.current_password, current.hash_contrasena):
        raise HTTPException(status_code=400, detail="La contraseña actual no es correcta")

    try:
        validar_fortaleza_contrasena(payload.new_password)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    current.hash_contrasena = hashear_contrasena(payload.new_password)
    _revoke_user_sessions(db, current.id)
    db.commit()
    return MessageOut(message="Contraseña actualizada. Inicie sesión nuevamente.")


@router.get("/me")
def me(current: Usuario = Depends(get_current_user)):
    return usuario_out(current)


@router.post("/training-complete", response_model=UsuarioOut)
def complete_training(
    payload: TrainingCompleteIn,
    current: Usuario = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Registra de forma idempotente la versión del recorrido completado.

    El cliente no puede adelantar arbitrariamente el progreso: solo se acepta
    la versión que el servidor publica como vigente.
    """
    if payload.version != TRAINING_CURRENT_VERSION:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="La versión de la capacitación cambió. Actualice la aplicación e inténtelo nuevamente.",
        )

    user = (
        db.query(Usuario)
        .filter(Usuario.id == current.id, Usuario.activo.is_(True))
        .with_for_update()
        .first()
    )
    if not user:
        raise HTTPException(status_code=403, detail="Usuario desactivado")

    if int(user.onboarding_version or 0) < TRAINING_CURRENT_VERSION:
        user.onboarding_version = TRAINING_CURRENT_VERSION
        user.onboarding_completed_at = utcnow()
        db.commit()
        db.refresh(user)

    return usuario_out(user)


@router.post("/logout")
def logout(response: Response, db: Session = Depends(get_db), current: Usuario = Depends(get_current_user)):
    _revoke_user_sessions(db, current.id)
    db.commit()
    response.delete_cookie(key=COOKIE_NAME, domain=COOKIE_DOMAIN, path=COOKIE_PATH)
    return {"ok": True}
