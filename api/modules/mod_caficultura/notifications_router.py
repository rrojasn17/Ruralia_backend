from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session, selectinload

from config import (
    AI_NOTIFICATIONS_ENABLED,
    APP_TIMEZONE,
    NOTIFICATION_POLL_SECONDS,
    NOTIFICATION_WORKER_ENABLED,
    OPENAI_MODEL,
    openai_is_configured,
    smtp_is_configured,
)
from database import get_db
from modules.mod_caficultura.model_notifications import (
    NotificationDelivery,
    NotificationEvent,
    NotificationPreference,
    NotificationRun,
    ScheduledNotification,
)
from core.models import Usuario
from core.routers.auth import get_current_user, require_roles
from modules.mod_caficultura.schema_notifications import (
    NotificationEventOut,
    NotificationManualRunOut,
    NotificationPreferenceOut,
    NotificationPreferenceUpdate,
    NotificationRunOut,
    NotificationStatusOut,
    ScheduledNotificationCreate,
    ScheduledNotificationOut,
    ScheduledNotificationUpdate,
)
from services.email_service import send_notification_email
from modules.mod_caficultura.notification_engine import to_utc, utcnow

router = APIRouter(prefix="/notifications", tags=["notifications"])
MANAGER_ROLES = {"gerente"}


def _is_manager(user: Usuario) -> bool:
    return bool(getattr(user, "is_superadmin", False)) or user.rol in MANAGER_ROLES


def _get_preference(db: Session, user: Usuario) -> NotificationPreference:
    preference = (
        db.query(NotificationPreference)
        .filter(NotificationPreference.usuario_id == user.id)
        .first()
    )
    if preference:
        return preference
    preference = NotificationPreference(
        usuario_id=user.id,
        email_enabled=True,
        minimum_severity="warning",
        timezone=APP_TIMEZONE,
    )
    db.add(preference)
    db.commit()
    db.refresh(preference)
    return preference


def _validate_timezone(value: str) -> str:
    try:
        ZoneInfo(value)
    except ZoneInfoNotFoundError as exc:
        raise HTTPException(status_code=422, detail="Zona horaria inválida") from exc
    return value


def _load_schedule(db: Session, schedule_id: int) -> ScheduledNotification:
    row = db.query(ScheduledNotification).filter(ScheduledNotification.id == schedule_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Notificación programada no encontrada")
    return row


def _assert_schedule_access(row: ScheduledNotification, current: Usuario) -> None:
    if row.usuario_id != current.id and not _is_manager(current):
        raise HTTPException(status_code=403, detail="No autorizado para esta notificación")


@router.get("/preferences/me", response_model=NotificationPreferenceOut)
def get_my_preferences(
    current: Usuario = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return _get_preference(db, current)


@router.patch("/preferences/me", response_model=NotificationPreferenceOut)
def update_my_preferences(
    payload: NotificationPreferenceUpdate,
    current: Usuario = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    preference = _get_preference(db, current)
    changes = payload.model_dump(exclude_unset=True)
    if "timezone" in changes and changes["timezone"]:
        changes["timezone"] = _validate_timezone(changes["timezone"])
    for field, value in changes.items():
        setattr(preference, field, value)
    db.commit()
    db.refresh(preference)
    return preference


@router.get("/schedules", response_model=list[ScheduledNotificationOut])
def list_schedules(
    include_all: bool = Query(default=False),
    active: bool | None = Query(default=None),
    current: Usuario = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    query = db.query(ScheduledNotification)
    if not (include_all and _is_manager(current)):
        query = query.filter(ScheduledNotification.usuario_id == current.id)
    if active is not None:
        query = query.filter(ScheduledNotification.active.is_(active))
    return query.order_by(ScheduledNotification.next_run_at.asc()).limit(500).all()


@router.post("/schedules", response_model=ScheduledNotificationOut, status_code=status.HTTP_201_CREATED)
def create_schedule(
    payload: ScheduledNotificationCreate,
    current: Usuario = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    target_user_id = payload.usuario_id or current.id
    if target_user_id != current.id and not _is_manager(current):
        raise HTTPException(status_code=403, detail="Solo gerencia puede programar correos para otro usuario")
    target = db.query(Usuario).filter(Usuario.id == target_user_id, Usuario.activo.is_(True)).first()
    if not target:
        raise HTTPException(status_code=404, detail="Usuario destinatario no encontrado o inactivo")

    preference = _get_preference(db, target)
    row = ScheduledNotification(
        usuario_id=target.id,
        created_by_id=current.id,
        title=payload.title.strip(),
        message=payload.message.strip(),
        next_run_at=to_utc(payload.next_run_at, preference.timezone),
        recurrence=payload.recurrence,
        active=True,
        ai_enhance=payload.ai_enhance,
        max_sends=payload.max_sends,
        payload=payload.payload,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


@router.patch("/schedules/{schedule_id}", response_model=ScheduledNotificationOut)
def update_schedule(
    schedule_id: int,
    payload: ScheduledNotificationUpdate,
    current: Usuario = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    row = _load_schedule(db, schedule_id)
    _assert_schedule_access(row, current)
    changes = payload.model_dump(exclude_unset=True)
    if "next_run_at" in changes and changes["next_run_at"] is not None:
        preference = _get_preference(db, row.usuario)
        changes["next_run_at"] = to_utc(changes["next_run_at"], preference.timezone)
    for field, value in changes.items():
        setattr(row, field, value)
    db.commit()
    db.refresh(row)
    return row


@router.delete("/schedules/{schedule_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_schedule(
    schedule_id: int,
    current: Usuario = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    row = _load_schedule(db, schedule_id)
    _assert_schedule_access(row, current)
    db.delete(row)
    db.commit()
    return None


@router.get("/status", response_model=NotificationStatusOut)
def notification_status(
    _: Usuario = Depends(require_roles("gerente")),
    db: Session = Depends(get_db),
):
    last_run = db.query(NotificationRun).order_by(NotificationRun.started_at.desc()).first()
    return NotificationStatusOut(
        worker_enabled=NOTIFICATION_WORKER_ENABLED,
        smtp_configured=smtp_is_configured(),
        ai_enabled=AI_NOTIFICATIONS_ENABLED,
        openai_configured=openai_is_configured(),
        openai_model=OPENAI_MODEL,
        poll_seconds=NOTIFICATION_POLL_SECONDS,
        queued_deliveries=db.query(NotificationDelivery).filter(
            NotificationDelivery.status.in_(["pending", "failed"])
        ).count(),
        failed_deliveries=db.query(NotificationDelivery).filter(
            NotificationDelivery.status == "failed"
        ).count(),
        active_schedules=db.query(ScheduledNotification).filter(
            ScheduledNotification.active.is_(True)
        ).count(),
        last_run=last_run,
    )


@router.get("/events", response_model=list[NotificationEventOut])
def list_events(
    event_status: str | None = Query(default=None, alias="status"),
    severity: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    _: Usuario = Depends(require_roles("gerente")),
    db: Session = Depends(get_db),
):
    query = db.query(NotificationEvent).options(selectinload(NotificationEvent.deliveries))
    if event_status:
        query = query.filter(NotificationEvent.status == event_status)
    if severity:
        query = query.filter(NotificationEvent.severity == severity)
    return query.order_by(NotificationEvent.detected_at.desc()).limit(limit).all()


@router.get("/runs", response_model=list[NotificationRunOut])
def list_runs(
    limit: int = Query(default=50, ge=1, le=200),
    _: Usuario = Depends(require_roles("gerente")),
    db: Session = Depends(get_db),
):
    return db.query(NotificationRun).order_by(NotificationRun.started_at.desc()).limit(limit).all()


@router.post(
    "/run",
    response_model=NotificationManualRunOut,
    status_code=status.HTTP_202_ACCEPTED,
)
def run_now(
    current: Usuario = Depends(require_roles("gerente")),
    db: Session = Depends(get_db),
):
    pending = (
        db.query(NotificationRun)
        .filter(NotificationRun.status == "requested")
        .order_by(NotificationRun.started_at.asc())
        .first()
    )
    if pending:
        return NotificationManualRunOut(run=pending)

    run = NotificationRun(
        trigger=f"manual:{current.id}",
        status="requested",
        started_at=utcnow(),
        details={"requested_by_user_id": current.id},
    )
    db.add(run)
    db.commit()
    db.refresh(run)
    return NotificationManualRunOut(run=run)


@router.post("/test-email")
def test_email(
    current: Usuario = Depends(require_roles("gerente")),
):
    if not smtp_is_configured():
        raise HTTPException(status_code=503, detail="SMTP no está configurado")
    send_notification_email(
        recipient=current.correo,
        recipient_name=current.nombre,
        subject="NAVIA · Prueba del motor de notificaciones",
        summary=(
            "El canal de correo está operativo. Esta prueba no ejecutó el análisis de base de datos "
            "ni generó una alerta productiva."
        ),
        recommended_action="Revise que el mensaje haya llegado a la bandeja esperada.",
        severity="info",
    )
    return {"ok": True, "sent_to": current.correo, "sent_at": datetime.now().isoformat()}
