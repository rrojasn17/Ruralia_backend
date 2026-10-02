from __future__ import annotations

import logging
import hashlib
import re
import unicodedata
from calendar import monthrange
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy.orm import Session

from config import NOTIFICATION_BATCH_SIZE, smtp_is_configured, telegram_is_configured
from modules.mod_caficultura.model_ai_consulting import AIAgent, AIAutomation, AIAutomationRun
from modules.mod_caficultura.model_notifications import NotificationDelivery, NotificationEvent
from modules.mod_caficultura.ai_agent import ensure_default_agent, summarize_metric
from modules.mod_caficultura.ai_metrics import execute_metric
from services.email_service import send_notification_email
from services.telegram_service import send_message


logger = logging.getLogger("navia-api.ai-automation")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def parse_monitoring_instruction(instruction: str) -> dict[str, Any] | None:
    normalized = unicodedata.normalize("NFD", str(instruction or "").strip().lower())
    normalized = "".join(char for char in normalized if unicodedata.category(char) != "Mn")
    normalized = normalized.replace(",", ".")

    days_match = re.search(
        r"mas\s+de\s+(\d+(?:\.\d+)?)\s+dias?\s+sin\s+(?:abonar|fertilizar)",
        normalized,
    )
    if days_match:
        return {
            "metric_key": "days_since_fertilization",
            "condition_operator": "gt",
            "threshold": float(days_match.group(1)),
            "parameters": {},
        }

    hours_match = re.search(
        r"(\d+(?:\.\d+)?)\s*horas?\s+(?:seguidas?|consecutivas?)",
        normalized,
    )
    humidity_match = re.search(
        r"humedad(?:\s+relativa)?\s+(?:mayor|superior)\s+(?:al|a|de)\s*(\d+(?:\.\d+)?)\s*%?",
        normalized,
    )
    if hours_match and humidity_match:
        return {
            "metric_key": "farm_sensor_analysis",
            "condition_operator": "gte",
            "threshold": float(hours_match.group(1)),
            "parameters": {"humidity_threshold": float(humidity_match.group(1))},
        }
    return None


def _normalize(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _advance_month(value: datetime) -> datetime:
    year = value.year + (1 if value.month == 12 else 0)
    month = 1 if value.month == 12 else value.month + 1
    return value.replace(year=year, month=month, day=min(value.day, monthrange(year, month)[1]))


def next_automation_run(row: AIAutomation, scheduled_for: datetime, now: datetime) -> datetime | None:
    recurrence = str(row.recurrence or "once").lower()
    if recurrence == "once":
        return None
    try:
        zone = ZoneInfo(row.timezone or "America/Costa_Rica")
    except ZoneInfoNotFoundError:
        zone = timezone.utc
    candidate = _normalize(scheduled_for).astimezone(zone)
    if recurrence == "interval":
        candidate += timedelta(minutes=max(15, int(row.interval_minutes or 15)))
    elif recurrence == "daily":
        candidate += timedelta(days=1)
    elif recurrence == "weekly":
        candidate += timedelta(days=7)
    elif recurrence == "monthly":
        candidate = _advance_month(candidate)
    else:
        return None
    candidate = candidate.astimezone(timezone.utc)
    while candidate <= now:
        if recurrence == "interval":
            candidate += timedelta(minutes=max(15, int(row.interval_minutes or 15)))
        elif recurrence == "daily":
            candidate += timedelta(days=1)
        elif recurrence == "weekly":
            candidate += timedelta(days=7)
        else:
            candidate = _advance_month(candidate.astimezone(zone)).astimezone(timezone.utc)
    return candidate


def _numeric_primary(result: dict[str, Any]) -> float | None:
    value = result.get("primary_value")
    if isinstance(value, bool):
        return float(int(value))
    if isinstance(value, (int, float)):
        return float(value)
    return None


def condition_matches(row: AIAutomation, result: dict[str, Any]) -> bool:
    if result.get("monitoring_data_available") is False:
        return False
    operator = str(row.condition_operator or "always")
    current = _numeric_primary(result)
    previous = _numeric_primary(dict(row.last_value or {}))
    threshold = float(row.threshold) if row.threshold is not None else None
    if operator == "always":
        return True
    if operator == "changed":
        return previous is None or current != previous
    if current is None or threshold is None:
        return False
    return {
        "gt": current > threshold,
        "gte": current >= threshold,
        "lt": current < threshold,
        "lte": current <= threshold,
        "eq": current == threshold,
    }.get(operator, False)


class _TemplateValues(dict):
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


def _render_template(row: AIAutomation, result: dict[str, Any]) -> str:
    fallback = str(result.get("summary") or "Consulta programada completada.")
    if not row.message_template:
        return fallback
    values = _TemplateValues(
        summary=fallback,
        value=result.get("primary_value", ""),
        automation=row.name,
        metric=row.metric_key,
    )
    try:
        return str(row.message_template).format_map(values)[:4000]
    except (ValueError, KeyError):
        return fallback


def _deliver(row: AIAutomation, message: str) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    channels = set(row.channels or [])
    if "in_app" in channels:
        results.append({"channel": "in_app", "status": "sent"})
    if "email" in channels:
        for recipient in row.email_recipients or []:
            try:
                if not smtp_is_configured():
                    raise RuntimeError("SMTP no está configurado")
                send_notification_email(
                    recipient=str(recipient),
                    recipient_name=None,
                    subject=f"NAVIA · {row.name}"[:160],
                    summary=message,
                    recommended_action="Abra NAVIA para revisar el detalle y los datos de origen.",
                    severity="info",
                )
                results.append({"channel": "email", "recipient": str(recipient), "status": "sent"})
            except Exception as exc:
                logger.exception("Falló automatización %s por email", row.id)
                results.append({"channel": "email", "recipient": str(recipient), "status": "failed", "error": str(exc)[:500]})
    if "telegram" in channels:
        for chat_id in row.telegram_chat_ids or []:
            try:
                if not telegram_is_configured():
                    raise RuntimeError("Telegram no está configurado")
                send_message(str(chat_id), f"NAVIA · {row.name}\n\n{message}")
                results.append({"channel": "telegram", "recipient": str(chat_id), "status": "sent"})
            except Exception as exc:
                logger.exception("Falló automatización %s por Telegram", row.id)
                results.append({"channel": "telegram", "recipient": str(chat_id), "status": "failed", "error": str(exc)[:500]})
    return results


def _create_in_app_alert(
    db: Session,
    row: AIAutomation,
    run: AIAutomationRun,
    result: dict[str, Any],
    message: str,
    now: datetime,
) -> None:
    owner = row.usuario
    if not owner or not str(owner.correo or "").strip():
        return
    dedupe_key = hashlib.sha256(f"ai-automation:{row.id}:{run.id}".encode("utf-8")).hexdigest()
    event = NotificationEvent(
        dedupe_key=dedupe_key,
        category="farm_monitoring",
        entity_type="ai_automation",
        entity_id=str(row.id),
        severity="warning",
        title=row.name[:180],
        message=message,
        recommended_action="Abra NAVIA para revisar la finca, la regla y las lecturas de origen.",
        status="sent",
        source_payload={"automation_id": row.id, "metric_key": row.metric_key, "value": result},
        ai_payload={"channels": list(row.channels or []), "in_app": True},
        detected_at=now,
        last_seen_at=now,
        sent_at=now,
    )
    db.add(event)
    db.flush()
    db.add(NotificationDelivery(
        event_id=event.id,
        usuario_id=owner.id,
        recipient_email=owner.correo.strip().lower(),
        recipient_name=owner.nombre,
        status="sent",
        sent_at=now,
    ))


def process_due_automations(
    db: Session,
    now: datetime | None = None,
    *,
    automation_ids: set[int] | None = None,
) -> dict[str, int]:
    now = _normalize(now or utcnow())
    query = db.query(AIAutomation).filter(
        AIAutomation.enabled.is_(True),
        AIAutomation.next_run_at <= now,
    )
    if automation_ids is not None:
        query = query.filter(AIAutomation.id.in_(automation_ids))
    query = query.order_by(AIAutomation.next_run_at.asc()).limit(NOTIFICATION_BATCH_SIZE)
    if db.get_bind().dialect.name == "postgresql":
        query = query.with_for_update(skip_locked=True)
    rows = query.all()
    totals = {"processed": 0, "sent": 0, "skipped": 0, "failed": 0}
    for row in rows:
        scheduled_for = _normalize(row.next_run_at)
        run = AIAutomationRun(
            automation_id=row.id,
            status="running",
            scheduled_for=scheduled_for,
            started_at=now,
        )
        db.add(run)
        db.flush()
        totals["processed"] += 1
        try:
            result = execute_metric(db, row.metric_key, dict(row.parameters or {}))
            run.value = result
            row.last_run_at = now
            row.run_count = int(row.run_count or 0) + 1
            next_run = next_automation_run(row, scheduled_for, now)
            if next_run is None:
                row.enabled = False
            else:
                row.next_run_at = next_run
            if not result.get("ok"):
                raise ValueError(str(result.get("message") or "La consulta programada no pudo completarse"))
            previous_value = dict(row.last_value or {})
            matches = condition_matches(row, result)
            operator = str(row.condition_operator or "always")
            persistent_threshold = operator in {"gt", "gte", "lt", "lte", "eq"}
            already_active = (
                matches
                and persistent_threshold
                and (
                    previous_value.get("_condition_active") is True
                    or (
                        "_condition_active" not in previous_value
                        and row.last_sent_at is not None
                        and condition_matches(row, previous_value)
                    )
                )
            )
            row.last_value = {**result, "_condition_active": matches} if persistent_threshold else result
            if not matches:
                run.status = "skipped"
                run.message = str(result.get("summary") or "Condición no cumplida")
                run.delivery_results = []
                run.finished_at = utcnow()
                row.last_error = None
                totals["skipped"] += 1
                continue
            if already_active:
                run.status = "skipped"
                run.message = "La condición continúa activa; la notificación se rearmará cuando deje de cumplirse."
                run.delivery_results = []
                run.finished_at = utcnow()
                row.last_error = None
                totals["skipped"] += 1
                continue
            agent: AIAgent = row.agent or ensure_default_agent(db, row.usuario_id)
            base_message = _render_template(row, result)
            message = summarize_metric(agent, result, base_message) if row.ai_enhance else base_message
            _create_in_app_alert(db, row, run, result, message, now)
            deliveries = _deliver(row, message)
            failures = [item for item in deliveries if item.get("status") != "sent"]
            successes = [item for item in deliveries if item.get("status") == "sent"]
            run.message = message
            run.delivery_results = deliveries
            run.finished_at = utcnow()
            if successes and not failures:
                run.status = "completed"
                row.last_sent_at = run.finished_at
                row.last_error = None
                totals["sent"] += 1
            elif successes:
                run.status = "partial"
                row.last_sent_at = run.finished_at
                row.last_error = "; ".join(str(item.get("error") or "Fallo de entrega") for item in failures)[:2000]
                totals["sent"] += 1
                totals["failed"] += 1
            else:
                run.status = "failed"
                if persistent_threshold:
                    row.last_value = {**result, "_condition_active": False}
                row.last_error = "; ".join(str(item.get("error") or "Sin destinatarios válidos") for item in failures)[:2000]
                run.error = row.last_error
                totals["failed"] += 1
        except Exception as exc:
            logger.exception("Falló automatización IA %s", row.id)
            run.status = "failed"
            # A failed attempt must not consume the threshold transition.
            row.last_value = {**dict(row.last_value or {}), "_condition_active": False}
            run.error = str(exc)[:2000]
            run.finished_at = utcnow()
            row.last_error = run.error
            totals["failed"] += 1
        finally:
            db.flush()
    return totals
