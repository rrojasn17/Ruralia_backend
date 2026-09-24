from __future__ import annotations

import logging
from calendar import monthrange
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy.orm import Session

from config import NOTIFICATION_BATCH_SIZE, smtp_is_configured, telegram_is_configured
from modules.mod_caficultura.model_ai_consulting import AIAgent, AIAutomation, AIAutomationRun
from modules.mod_caficultura.ai_agent import ensure_default_agent, summarize_metric
from modules.mod_caficultura.ai_metrics import execute_metric
from services.email_service import send_notification_email
from services.telegram_service import send_message


logger = logging.getLogger("navia-api.ai-automation")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


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
            matches = condition_matches(row, result)
            row.last_value = result
            if not matches:
                run.status = "skipped"
                run.message = str(result.get("summary") or "Condición no cumplida")
                run.delivery_results = []
                run.finished_at = utcnow()
                row.last_error = None
                totals["skipped"] += 1
                continue
            agent: AIAgent = row.agent or ensure_default_agent(db, row.usuario_id)
            base_message = _render_template(row, result)
            message = summarize_metric(agent, result, base_message) if row.ai_enhance else base_message
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
                row.last_error = "; ".join(str(item.get("error") or "Sin destinatarios válidos") for item in failures)[:2000]
                run.error = row.last_error
                totals["failed"] += 1
        except Exception as exc:
            logger.exception("Falló automatización IA %s", row.id)
            run.status = "failed"
            run.error = str(exc)[:2000]
            run.finished_at = utcnow()
            row.last_error = run.error
            totals["failed"] += 1
        finally:
            db.flush()
    return totals
