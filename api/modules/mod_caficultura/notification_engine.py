from __future__ import annotations

import hashlib
import logging
from calendar import monthrange
from datetime import datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import or_, text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from config import (
    AI_NOTIFICATION_MAX_CANDIDATES,
    APP_TIMEZONE,
    NOTIFICATION_BATCH_SIZE,
    NOTIFICATION_DEFAULT_MIN_SEVERITY,
    NOTIFICATION_MAX_DELIVERY_ATTEMPTS,
    NOTIFICATION_RETRY_BASE_SECONDS,
)
from modules.mod_caficultura.model_notifications import (
    NotificationDelivery,
    NotificationEvent,
    NotificationPreference,
    NotificationRun,
    ScheduledNotification,
)
from core.models import Usuario
from services.email_service import send_notification_email
from modules.mod_caficultura.notification_ai import SEVERITY_RANK, evaluate_candidate, fallback_decision
from modules.mod_caficultura.notification_candidates import NotificationCandidate, collect_candidates
from modules.mod_caficultura.ai_automation_engine import process_due_automations

logger = logging.getLogger("navia-api.notification-engine")
WORKER_LOCK_ID = 20_260_720
VALID_RECURRENCES = {"none", "daily", "weekly", "monthly"}


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def normalize_dt(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def to_utc(value: datetime, timezone_name: str = APP_TIMEZONE) -> datetime:
    if value.tzinfo is not None:
        return value.astimezone(timezone.utc)
    try:
        zone = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError:
        zone = timezone.utc
    return value.replace(tzinfo=zone).astimezone(timezone.utc)


def _severity_at_least(value: str, minimum: str) -> bool:
    return SEVERITY_RANK.get(value, 1) >= SEVERITY_RANK.get(minimum, 1)


def _is_quiet_now(preference: NotificationPreference, now: datetime) -> bool:
    if not preference.quiet_hours_start or not preference.quiet_hours_end:
        return False
    try:
        start_hour, start_minute = (int(part) for part in preference.quiet_hours_start.split(":"))
        end_hour, end_minute = (int(part) for part in preference.quiet_hours_end.split(":"))
        zone = ZoneInfo(preference.timezone or APP_TIMEZONE)
    except (ValueError, ZoneInfoNotFoundError):
        return False

    local_time = now.astimezone(zone).time().replace(second=0, microsecond=0)
    start = time(start_hour, start_minute)
    end = time(end_hour, end_minute)
    if start == end:
        return False
    if start < end:
        return start <= local_time < end
    return local_time >= start or local_time < end


def _preference_for(db: Session, user_id: int) -> NotificationPreference:
    preference = (
        db.query(NotificationPreference)
        .filter(NotificationPreference.usuario_id == user_id)
        .first()
    )
    if preference:
        return preference
    preference = NotificationPreference(
        usuario_id=user_id,
        email_enabled=True,
        minimum_severity=NOTIFICATION_DEFAULT_MIN_SEVERITY,
        timezone=APP_TIMEZONE,
    )
    db.add(preference)
    db.flush()
    return preference


def eligible_recipients(
    db: Session,
    user_ids: list[int],
    severity: str,
    now: datetime,
    *,
    ignore_quiet_hours: bool = False,
    ignore_minimum_severity: bool = False,
) -> list[Usuario]:
    if not user_ids:
        return []
    users = (
        db.query(Usuario)
        .filter(Usuario.id.in_(set(user_ids)), Usuario.activo.is_(True))
        .order_by(Usuario.id.asc())
        .all()
    )
    result: list[Usuario] = []
    for user in users:
        if not (user.correo or "").strip():
            continue
        preference = _preference_for(db, user.id)
        if not preference.email_enabled:
            continue
        if not ignore_minimum_severity and not _severity_at_least(severity, preference.minimum_severity):
            continue
        if severity != "critical" and not ignore_quiet_hours and _is_quiet_now(preference, now):
            continue
        result.append(user)
    return result


def _dedupe_key(base_key: str, now: datetime, cooldown_hours: int) -> str:
    bucket_seconds = max(1, cooldown_hours) * 3600
    bucket = int(now.timestamp()) // bucket_seconds
    raw = f"{base_key}:{bucket}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _create_event(
    db: Session,
    *,
    dedupe_key: str,
    category: str,
    entity_type: str | None,
    entity_id: str | None,
    severity: str,
    title: str,
    message: str,
    recommended_action: str | None,
    source_payload: dict[str, Any],
    ai_payload: dict[str, Any],
    status: str,
    recipients: list[Usuario],
    now: datetime,
) -> NotificationEvent | None:
    try:
        with db.begin_nested():
            event = NotificationEvent(
                dedupe_key=dedupe_key,
                category=category,
                entity_type=entity_type,
                entity_id=entity_id,
                severity=severity,
                title=title[:180],
                message=message,
                recommended_action=recommended_action,
                source_payload=source_payload,
                ai_payload=ai_payload,
                status=status,
                detected_at=now,
                last_seen_at=now,
            )
            db.add(event)
            db.flush()
            for user in recipients:
                db.add(
                    NotificationDelivery(
                        event_id=event.id,
                        usuario_id=user.id,
                        recipient_email=user.correo.strip().lower(),
                        recipient_name=user.nombre,
                        status="pending",
                        next_retry_at=now,
                    )
                )
            db.flush()
        return event
    except IntegrityError:
        logger.debug("Evento duplicado omitido: %s", dedupe_key)
        return None


def create_candidate_event(
    db: Session,
    candidate: NotificationCandidate,
    now: datetime,
    *,
    use_ai: bool = True,
) -> tuple[NotificationEvent | None, bool]:
    explicit_dedupe_key = candidate.context.get("dedupe_key")
    dedupe_key = str(explicit_dedupe_key) if explicit_dedupe_key else _dedupe_key(
        candidate.base_key, now, candidate.cooldown_hours
    )
    if explicit_dedupe_key:
        existing = (
            db.query(NotificationEvent)
            .filter(NotificationEvent.dedupe_key == dedupe_key)
            .first()
        )
    else:
        cooldown_start = now - timedelta(hours=max(1, candidate.cooldown_hours))
        existing = (
            db.query(NotificationEvent)
            .filter(
                NotificationEvent.category == candidate.category,
                NotificationEvent.entity_type == candidate.entity_type,
                NotificationEvent.entity_id == candidate.entity_id,
                NotificationEvent.detected_at >= cooldown_start,
            )
            .order_by(NotificationEvent.detected_at.desc())
            .first()
        )
    if existing:
        existing.last_seen_at = now
        return None, False

    if use_ai:
        decision, ai_used = evaluate_candidate(candidate)
    else:
        decision, ai_used = fallback_decision(candidate, "scheduled_notification"), False

    if candidate.force_send:
        decision.send = True
    recipients = eligible_recipients(
        db,
        candidate.recipient_user_ids,
        decision.severity,
        now,
        ignore_quiet_hours=bool(candidate.context.get("ignore_quiet_hours")),
        ignore_minimum_severity=bool(candidate.context.get("ignore_minimum_severity")),
    )
    status = "queued" if decision.send and recipients else "suppressed"
    event = _create_event(
        db,
        dedupe_key=dedupe_key,
        category=candidate.category,
        entity_type=candidate.entity_type,
        entity_id=candidate.entity_id,
        severity=decision.severity,
        title=decision.subject,
        message=decision.summary,
        recommended_action=decision.recommended_action,
        source_payload={
            "facts": candidate.facts,
            "initial_title": candidate.title,
            "initial_severity": candidate.severity,
            "recipient_user_ids": candidate.recipient_user_ids,
            "context": candidate.context,
        },
        ai_payload={
            "used": ai_used,
            "send": decision.send,
            "rationale": decision.rationale,
        },
        status=status,
        recipients=recipients if decision.send else [],
        now=now,
    )
    return event, ai_used


def _advance_month(value: datetime) -> datetime:
    year = value.year + (1 if value.month == 12 else 0)
    month = 1 if value.month == 12 else value.month + 1
    day = min(value.day, monthrange(year, month)[1])
    return value.replace(year=year, month=month, day=day)


def next_occurrence(value: datetime, recurrence: str) -> datetime | None:
    normalized = recurrence if recurrence in VALID_RECURRENCES else "none"
    if normalized == "daily":
        return value + timedelta(days=1)
    if normalized == "weekly":
        return value + timedelta(days=7)
    if normalized == "monthly":
        return _advance_month(value)
    return None


def enqueue_due_schedules(db: Session, now: datetime) -> tuple[int, int]:
    query = (
        db.query(ScheduledNotification)
        .options(selectinload(ScheduledNotification.usuario))
        .filter(
            ScheduledNotification.active.is_(True),
            ScheduledNotification.next_run_at <= now,
        )
        .order_by(ScheduledNotification.next_run_at.asc())
        .limit(NOTIFICATION_BATCH_SIZE)
    )
    if db.bind and db.bind.dialect.name == "postgresql":
        query = query.with_for_update(skip_locked=True)
    rows = query.all()
    created = 0
    ai_evaluated = 0
    for row in rows:
        if not row.usuario or not row.usuario.activo:
            row.active = False
            row.last_error = "El usuario destinatario no existe o está inactivo"
            continue

        sequence = int(row.send_count or 0) + 1
        candidate = NotificationCandidate(
            category="scheduled_notification",
            entity_type="scheduled_notification",
            entity_id=str(row.id),
            severity="info",
            title=row.title,
            facts={"message": row.message, "scheduled_for": normalize_dt(row.next_run_at).isoformat()},
            recipient_user_ids=[row.usuario_id],
            recommended_action="Ingrese a NAVIA si necesita completar o verificar esta actividad.",
            cooldown_hours=1,
            force_send=False,
            context={
                "schedule_id": row.id,
                "sequence": sequence,
                "ignore_quiet_hours": True,
                "ignore_minimum_severity": True,
                "dedupe_key": hashlib.sha256(
                    f"scheduled:{row.id}:{sequence}".encode("utf-8")
                ).hexdigest(),
            },
        )
        # Una programación explícita siempre se encola. La IA solo puede mejorar la redacción.
        if row.ai_enhance:
            event, used = create_candidate_event(db, candidate, now, use_ai=True)
            ai_evaluated += int(used)
        else:
            decision = fallback_decision(candidate, "explicit_schedule")
            dedupe = hashlib.sha256(f"scheduled:{row.id}:{sequence}".encode("utf-8")).hexdigest()
            recipients = eligible_recipients(
                db,
                [row.usuario_id],
                "info",
                now,
                ignore_quiet_hours=True,
                ignore_minimum_severity=True,
            )
            event = _create_event(
                db,
                dedupe_key=dedupe,
                category="scheduled_notification",
                entity_type="scheduled_notification",
                entity_id=str(row.id),
                severity="info",
                title=f"NAVIA · {row.title}"[:180],
                message=row.message,
                recommended_action=decision.recommended_action,
                source_payload={"schedule_id": row.id, "sequence": sequence, "payload": row.payload or {}},
                ai_payload={"used": False, "send": True, "rationale": "explicit_schedule"},
                status="queued" if recipients else "suppressed",
                recipients=recipients,
                now=now,
            )
        if event:
            created += 1
            row.send_count = sequence
            row.last_error = None
            next_run = next_occurrence(normalize_dt(row.next_run_at) or now, row.recurrence)
            if row.max_sends is not None and row.send_count >= row.max_sends:
                next_run = None
            if next_run is None:
                row.active = False
            else:
                while next_run <= now:
                    following = next_occurrence(next_run, row.recurrence)
                    if following is None:
                        break
                    next_run = following
                row.next_run_at = next_run
    db.flush()
    return created, ai_evaluated


def _update_event_status(event: NotificationEvent) -> None:
    deliveries = list(event.deliveries)
    statuses = {delivery.status for delivery in deliveries}
    if statuses and statuses <= {"sent"}:
        event.status = "sent"
        event.sent_at = max(
            (delivery.sent_at for delivery in deliveries if delivery.sent_at),
            default=utcnow(),
        )
        event.next_retry_at = None
        return

    retryable = any(
        delivery.status in {"pending", "failed"}
        and int(delivery.attempts or 0) < NOTIFICATION_MAX_DELIVERY_ATTEMPTS
        for delivery in deliveries
    )
    if retryable:
        event.status = "partial" if "sent" in statuses else "queued"
        retry_times = [
            delivery.next_retry_at
            for delivery in deliveries
            if delivery.next_retry_at and delivery.status in {"pending", "failed"}
        ]
        event.next_retry_at = min(retry_times) if retry_times else None
    elif "sent" in statuses:
        event.status = "partial"
        event.next_retry_at = None
    elif statuses:
        event.status = "failed"
        event.next_retry_at = None


def process_due_deliveries(db: Session, now: datetime) -> tuple[int, int]:
    query = (
        db.query(NotificationDelivery)
        .options(selectinload(NotificationDelivery.event).selectinload(NotificationEvent.deliveries))
        .filter(
            NotificationDelivery.status.in_(["pending", "failed"]),
            NotificationDelivery.attempts < NOTIFICATION_MAX_DELIVERY_ATTEMPTS,
            or_(NotificationDelivery.next_retry_at.is_(None), NotificationDelivery.next_retry_at <= now),
        )
        .order_by(NotificationDelivery.created_at.asc())
        .limit(NOTIFICATION_BATCH_SIZE)
    )
    if db.bind and db.bind.dialect.name == "postgresql":
        query = query.with_for_update(skip_locked=True)
    deliveries = query.all()
    sent = 0
    failed = 0

    for delivery in deliveries:
        event = delivery.event
        if not event:
            delivery.status = "failed"
            delivery.last_error = "El evento asociado ya no existe"
            failed += 1
            continue
        delivery.attempts = int(delivery.attempts or 0) + 1
        event.attempt_count = int(event.attempt_count or 0) + 1
        try:
            send_notification_email(
                recipient=delivery.recipient_email,
                recipient_name=delivery.recipient_name,
                subject=event.title,
                summary=event.message,
                recommended_action=event.recommended_action,
                severity=event.severity,
            )
            delivery.status = "sent"
            delivery.sent_at = now
            delivery.next_retry_at = None
            delivery.last_error = None
            sent += 1

            if event.category == "scheduled_notification" and event.entity_id:
                schedule = db.query(ScheduledNotification).filter(ScheduledNotification.id == int(event.entity_id)).first()
                if schedule:
                    schedule.last_sent_at = now
                    schedule.last_error = None
        except Exception as exc:
            failed += 1
            delivery.status = "failed"
            delivery.last_error = str(exc)[:2000]
            delay = NOTIFICATION_RETRY_BASE_SECONDS * (2 ** max(0, delivery.attempts - 1))
            delivery.next_retry_at = now + timedelta(seconds=delay)
            event.next_retry_at = delivery.next_retry_at
            if event.category == "scheduled_notification" and event.entity_id:
                schedule = db.query(ScheduledNotification).filter(ScheduledNotification.id == int(event.entity_id)).first()
                if schedule:
                    schedule.last_error = delivery.last_error
        _update_event_status(event)
    db.flush()
    return sent, failed


def acquire_cycle_lock(db: Session) -> tuple[bool, Connection | None]:
    bind = db.get_bind()
    if bind.dialect.name != "postgresql":
        return True, None

    connection = bind.connect()
    try:
        acquired = bool(
            connection.execute(
                text("SELECT pg_try_advisory_lock(:lock_id)"),
                {"lock_id": WORKER_LOCK_ID},
            ).scalar()
        )
        if not acquired:
            connection.close()
            return False, None
        return True, connection
    except Exception:
        connection.close()
        raise


def release_cycle_lock(connection: Connection | None) -> None:
    if connection is None:
        return
    try:
        connection.execute(
            text("SELECT pg_advisory_unlock(:lock_id)"),
            {"lock_id": WORKER_LOCK_ID},
        )
    except Exception:
        logger.exception("No se pudo liberar el bloqueo del worker de notificaciones")
    finally:
        connection.close()


def run_notification_cycle(
    db: Session,
    trigger: str = "worker",
    *,
    existing_run_id: int | None = None,
) -> NotificationRun:
    now = utcnow()
    if existing_run_id is not None:
        run = db.query(NotificationRun).filter(NotificationRun.id == existing_run_id).first()
        if not run:
            raise ValueError("La ejecución solicitada ya no existe")
        run.trigger = trigger
        run.status = "running"
        run.started_at = now
        run.finished_at = None
        run.error = None
        run.candidates_found = 0
        run.events_created = 0
        run.ai_evaluated = 0
        run.emails_sent = 0
        run.emails_failed = 0
        run.details = {}
    else:
        run = NotificationRun(trigger=trigger, status="running", started_at=now)
        db.add(run)
    db.commit()
    db.refresh(run)
    run_id = run.id

    locked = False
    lock_connection: Connection | None = None
    try:
        locked, lock_connection = acquire_cycle_lock(db)
        if not locked:
            run.status = "requested" if existing_run_id is not None else "skipped"
            run.finished_at = None if existing_run_id is not None else utcnow()
            run.details = {"reason": "another_worker_holds_lock"}
            db.commit()
            return run

        schedule_events, schedule_ai = enqueue_due_schedules(db, now)
        candidates = collect_candidates(db, now)
        run.candidates_found = len(candidates)
        events_created = schedule_events
        ai_evaluated = schedule_ai

        for index, candidate in enumerate(candidates):
            event, used_ai = create_candidate_event(
                db,
                candidate,
                now,
                use_ai=index < AI_NOTIFICATION_MAX_CANDIDATES,
            )
            events_created += int(event is not None)
            ai_evaluated += int(used_ai)

        automation_totals = process_due_automations(db, now)
        db.commit()
        sent, failed = process_due_deliveries(db, utcnow())
        run.events_created = events_created
        run.ai_evaluated = ai_evaluated
        run.emails_sent = sent
        run.emails_failed = failed
        run.status = "completed"
        run.finished_at = utcnow()
        run.details = {
            "scheduled_events": schedule_events,
            "operational_candidates": len(candidates),
            "ai_automations": automation_totals,
        }
        db.commit()
        db.refresh(run)
        return run
    except Exception as exc:
        db.rollback()
        run = db.query(NotificationRun).filter(NotificationRun.id == run_id).first() or run
        run.status = "failed"
        run.finished_at = utcnow()
        run.error = str(exc)[:4000]
        db.add(run)
        db.commit()
        logger.exception("Falló el ciclo de notificaciones")
        return run
    finally:
        if locked:
            release_cycle_lock(lock_connection)
