from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import relationship
from sqlalchemy.types import JSON

from database import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class NotificationPreference(Base):
    __tablename__ = "navia_notification_preferences"
    __table_args__ = (
        UniqueConstraint("usuario_id", name="uq_notification_preference_usuario"),
    )

    id = Column(Integer, primary_key=True)
    usuario_id = Column(
        Integer,
        ForeignKey("navia_usuarios.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    email_enabled = Column(Boolean, nullable=False, default=True, server_default="true")
    minimum_severity = Column(String(20), nullable=False, default="warning", server_default="warning")
    quiet_hours_start = Column(String(5), nullable=True)
    quiet_hours_end = Column(String(5), nullable=True)
    timezone = Column(String(80), nullable=False, default="America/Costa_Rica", server_default="America/Costa_Rica")
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)

    usuario = relationship("Usuario")


class ScheduledNotification(Base):
    __tablename__ = "navia_scheduled_notifications"
    __table_args__ = (
        Index("ix_scheduled_notifications_due", "active", "next_run_at"),
    )

    id = Column(Integer, primary_key=True)
    usuario_id = Column(
        Integer,
        ForeignKey("navia_usuarios.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    created_by_id = Column(
        Integer,
        ForeignKey("navia_usuarios.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    title = Column(String(180), nullable=False)
    message = Column(Text, nullable=False)
    next_run_at = Column(DateTime(timezone=True), nullable=False, index=True)
    recurrence = Column(String(20), nullable=False, default="none", server_default="none")
    active = Column(Boolean, nullable=False, default=True, server_default="true", index=True)
    ai_enhance = Column(Boolean, nullable=False, default=False, server_default="false")
    max_sends = Column(Integer, nullable=True)
    send_count = Column(Integer, nullable=False, default=0, server_default="0")
    last_sent_at = Column(DateTime(timezone=True), nullable=True)
    last_error = Column(Text, nullable=True)
    payload = Column(JSON, nullable=False, default=dict)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)

    usuario = relationship("Usuario", foreign_keys=[usuario_id])
    created_by = relationship("Usuario", foreign_keys=[created_by_id])


class NotificationEvent(Base):
    __tablename__ = "navia_notification_events"
    __table_args__ = (
        UniqueConstraint("dedupe_key", name="uq_notification_event_dedupe_key"),
        Index("ix_notification_events_status_retry", "status", "next_retry_at"),
        Index("ix_notification_events_entity", "entity_type", "entity_id"),
    )

    id = Column(Integer, primary_key=True)
    dedupe_key = Column(String(64), nullable=False, unique=True, index=True)
    category = Column(String(80), nullable=False, index=True)
    entity_type = Column(String(80), nullable=True, index=True)
    entity_id = Column(String(120), nullable=True, index=True)
    severity = Column(String(20), nullable=False, default="warning", index=True)
    title = Column(String(180), nullable=False)
    message = Column(Text, nullable=False)
    recommended_action = Column(Text, nullable=True)
    status = Column(String(24), nullable=False, default="queued", server_default="queued", index=True)
    source_payload = Column(JSON, nullable=False, default=dict)
    ai_payload = Column(JSON, nullable=False, default=dict)
    detected_at = Column(DateTime(timezone=True), nullable=False, default=utcnow, index=True)
    last_seen_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)
    next_retry_at = Column(DateTime(timezone=True), nullable=True, index=True)
    attempt_count = Column(Integer, nullable=False, default=0, server_default="0")
    sent_at = Column(DateTime(timezone=True), nullable=True, index=True)
    expires_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)

    deliveries = relationship(
        "NotificationDelivery",
        back_populates="event",
        cascade="all, delete-orphan",
    )


class NotificationDelivery(Base):
    __tablename__ = "navia_notification_deliveries"
    __table_args__ = (
        UniqueConstraint("event_id", "recipient_email", name="uq_notification_delivery_recipient"),
        Index("ix_notification_deliveries_due", "status", "next_retry_at"),
    )

    id = Column(Integer, primary_key=True)
    event_id = Column(
        Integer,
        ForeignKey("navia_notification_events.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    usuario_id = Column(
        Integer,
        ForeignKey("navia_usuarios.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    recipient_email = Column(String(180), nullable=False, index=True)
    recipient_name = Column(String(180), nullable=True)
    status = Column(String(24), nullable=False, default="pending", server_default="pending", index=True)
    attempts = Column(Integer, nullable=False, default=0, server_default="0")
    next_retry_at = Column(DateTime(timezone=True), nullable=True, index=True)
    last_error = Column(Text, nullable=True)
    sent_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)

    event = relationship("NotificationEvent", back_populates="deliveries")
    usuario = relationship("Usuario")


class NotificationRun(Base):
    __tablename__ = "navia_notification_runs"

    id = Column(Integer, primary_key=True)
    trigger = Column(String(40), nullable=False, default="worker", index=True)
    status = Column(String(24), nullable=False, default="running", index=True)
    started_at = Column(DateTime(timezone=True), nullable=False, default=utcnow, index=True)
    finished_at = Column(DateTime(timezone=True), nullable=True)
    candidates_found = Column(Integer, nullable=False, default=0, server_default="0")
    events_created = Column(Integer, nullable=False, default=0, server_default="0")
    ai_evaluated = Column(Integer, nullable=False, default=0, server_default="0")
    emails_sent = Column(Integer, nullable=False, default=0, server_default="0")
    emails_failed = Column(Integer, nullable=False, default=0, server_default="0")
    details = Column(JSON, nullable=False, default=dict)
    error = Column(Text, nullable=True)
