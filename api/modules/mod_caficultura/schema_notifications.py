from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, EmailStr, Field, field_validator

Severity = Literal["info", "warning", "high", "critical"]
Recurrence = Literal["none", "daily", "weekly", "monthly"]


class NotificationPreferenceOut(BaseModel):
    usuario_id: int
    email_enabled: bool = True
    minimum_severity: Severity = "warning"
    quiet_hours_start: str | None = None
    quiet_hours_end: str | None = None
    timezone: str = "America/Costa_Rica"

    model_config = {"from_attributes": True}


class NotificationPreferenceUpdate(BaseModel):
    email_enabled: bool | None = None
    minimum_severity: Severity | None = None
    quiet_hours_start: str | None = Field(default=None, max_length=5)
    quiet_hours_end: str | None = Field(default=None, max_length=5)
    timezone: str | None = Field(default=None, max_length=80)

    @field_validator("quiet_hours_start", "quiet_hours_end")
    @classmethod
    def validate_hhmm(cls, value: str | None) -> str | None:
        if value in {None, ""}:
            return None
        parts = value.split(":")
        if len(parts) != 2:
            raise ValueError("La hora debe usar formato HH:MM")
        try:
            hour, minute = int(parts[0]), int(parts[1])
        except ValueError as exc:
            raise ValueError("La hora debe usar formato HH:MM") from exc
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValueError("La hora debe usar formato HH:MM")
        return f"{hour:02d}:{minute:02d}"


class ScheduledNotificationCreate(BaseModel):
    usuario_id: int | None = None
    title: str = Field(min_length=3, max_length=180)
    message: str = Field(min_length=3, max_length=5000)
    next_run_at: datetime
    recurrence: Recurrence = "none"
    ai_enhance: bool = False
    max_sends: int | None = Field(default=None, ge=1, le=1000)
    payload: dict[str, Any] = Field(default_factory=dict)


class ScheduledNotificationUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=3, max_length=180)
    message: str | None = Field(default=None, min_length=3, max_length=5000)
    next_run_at: datetime | None = None
    recurrence: Recurrence | None = None
    active: bool | None = None
    ai_enhance: bool | None = None
    max_sends: int | None = Field(default=None, ge=1, le=1000)
    payload: dict[str, Any] | None = None


class ScheduledNotificationOut(BaseModel):
    id: int
    usuario_id: int
    title: str
    message: str
    next_run_at: datetime
    recurrence: str
    active: bool
    ai_enhance: bool
    max_sends: int | None
    send_count: int
    last_sent_at: datetime | None
    last_error: str | None
    payload: dict[str, Any]
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class NotificationDeliveryOut(BaseModel):
    id: int
    usuario_id: int | None
    recipient_email: EmailStr
    recipient_name: str | None
    status: str
    attempts: int
    next_retry_at: datetime | None
    last_error: str | None
    sent_at: datetime | None

    model_config = {"from_attributes": True}


class NotificationEventOut(BaseModel):
    id: int
    category: str
    entity_type: str | None
    entity_id: str | None
    severity: str
    title: str
    message: str
    recommended_action: str | None
    status: str
    source_payload: dict[str, Any]
    ai_payload: dict[str, Any]
    detected_at: datetime
    sent_at: datetime | None
    deliveries: list[NotificationDeliveryOut] = Field(default_factory=list)

    model_config = {"from_attributes": True}


class NotificationRunOut(BaseModel):
    id: int
    trigger: str
    status: str
    started_at: datetime
    finished_at: datetime | None
    candidates_found: int
    events_created: int
    ai_evaluated: int
    emails_sent: int
    emails_failed: int
    details: dict[str, Any]
    error: str | None

    model_config = {"from_attributes": True}


class NotificationStatusOut(BaseModel):
    worker_enabled: bool
    smtp_configured: bool
    ai_enabled: bool
    openai_configured: bool
    openai_model: str
    poll_seconds: int
    queued_deliveries: int
    failed_deliveries: int
    active_schedules: int
    last_run: NotificationRunOut | None = None


class NotificationManualRunOut(BaseModel):
    run: NotificationRunOut
