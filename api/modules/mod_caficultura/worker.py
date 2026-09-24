from __future__ import annotations

from sqlalchemy.orm import Session


def run_cycle(db: Session) -> dict:
    """Ejecuta un ciclo de background propio de Caficultura."""
    from modules.mod_caficultura.model_notifications import NotificationRun
    from modules.mod_caficultura.notification_engine import run_notification_cycle

    requested = (
        db.query(NotificationRun)
        .filter(NotificationRun.status == "requested")
        .order_by(NotificationRun.started_at.asc())
        .first()
    )
    if requested:
        run = run_notification_cycle(
            db,
            trigger=requested.trigger or "manual",
            existing_run_id=requested.id,
        )
    else:
        run = run_notification_cycle(db, trigger="worker")

    return {
        "id": run.id,
        "status": run.status,
        "candidates": run.candidates_found,
        "events": run.events_created,
        "sent": run.emails_sent,
        "failed": run.emails_failed,
    }
