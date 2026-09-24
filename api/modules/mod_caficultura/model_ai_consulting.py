from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Float,
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


class AIAgent(Base):
    __tablename__ = "navia_ai_agents"

    id = Column(Integer, primary_key=True)
    name = Column(String(120), nullable=False)
    slug = Column(String(80), nullable=False, unique=True, index=True)
    provider = Column(String(40), nullable=False, default="openai", server_default="openai")
    model = Column(String(100), nullable=False)
    instructions = Column(Text, nullable=False)
    enabled = Column(Boolean, nullable=False, default=True, server_default="true", index=True)
    is_default = Column(Boolean, nullable=False, default=False, server_default="false", index=True)
    max_output_tokens = Column(Integer, nullable=False, default=1800, server_default="1800")
    capabilities = Column(JSON, nullable=False, default=list)
    created_by_id = Column(Integer, ForeignKey("navia_usuarios.id", ondelete="SET NULL"), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)

    created_by = relationship("Usuario")
    conversations = relationship("AIConversation", back_populates="agent")


class AIConversation(Base):
    __tablename__ = "navia_ai_conversations"
    __table_args__ = (
        Index("ix_navia_ai_conversation_user_updated", "usuario_id", "updated_at"),
        Index("ix_navia_ai_conversation_external", "channel", "external_chat_id"),
    )

    id = Column(Integer, primary_key=True)
    usuario_id = Column(Integer, ForeignKey("navia_usuarios.id", ondelete="CASCADE"), nullable=False, index=True)
    agent_id = Column(Integer, ForeignKey("navia_ai_agents.id", ondelete="SET NULL"), nullable=True, index=True)
    finca_id = Column(Integer, ForeignKey("navia_fincas.id", ondelete="SET NULL"), nullable=True, index=True)
    title = Column(String(180), nullable=False, default="Nueva consulta")
    channel = Column(String(24), nullable=False, default="web", server_default="web", index=True)
    external_chat_id = Column(String(120), nullable=True)
    status = Column(String(24), nullable=False, default="active", server_default="active", index=True)
    last_message_at = Column(DateTime(timezone=True), nullable=True, index=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)

    usuario = relationship("Usuario")
    agent = relationship("AIAgent", back_populates="conversations")
    finca = relationship("Finca")
    messages = relationship(
        "AIMessage",
        back_populates="conversation",
        cascade="all, delete-orphan",
        order_by="AIMessage.created_at",
    )
    pending_actions = relationship("AIPendingAction", back_populates="conversation", cascade="all, delete-orphan")


class AIMessage(Base):
    __tablename__ = "navia_ai_messages"
    __table_args__ = (Index("ix_navia_ai_message_conversation_created", "conversation_id", "created_at"),)

    id = Column(Integer, primary_key=True)
    conversation_id = Column(
        Integer,
        ForeignKey("navia_ai_conversations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    role = Column(String(20), nullable=False, index=True)
    content = Column(Text, nullable=False, default="")
    content_type = Column(String(24), nullable=False, default="text", server_default="text")
    status = Column(String(24), nullable=False, default="completed", server_default="completed", index=True)
    meta = Column(JSON, nullable=False, default=dict)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    conversation = relationship("AIConversation", back_populates="messages")
    attachments = relationship("AIAttachment", back_populates="message")


class AIAttachment(Base):
    __tablename__ = "navia_ai_attachments"

    id = Column(Integer, primary_key=True)
    message_id = Column(Integer, ForeignKey("navia_ai_messages.id", ondelete="SET NULL"), nullable=True, index=True)
    usuario_id = Column(Integer, ForeignKey("navia_usuarios.id", ondelete="CASCADE"), nullable=False, index=True)
    storage_key = Column(String(500), nullable=False, unique=True)
    original_name = Column(String(255), nullable=False)
    media_type = Column(String(160), nullable=False)
    kind = Column(String(24), nullable=False, default="file", server_default="file", index=True)
    size_bytes = Column(Integer, nullable=False)
    sha256 = Column(String(64), nullable=False, index=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    message = relationship("AIMessage", back_populates="attachments")
    usuario = relationship("Usuario")


class AIPendingAction(Base):
    __tablename__ = "navia_ai_pending_actions"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_navia_ai_action_idempotency"),
        Index("ix_navia_ai_action_user_status", "usuario_id", "status"),
    )

    id = Column(Integer, primary_key=True)
    public_id = Column(String(48), nullable=False, unique=True, index=True)
    idempotency_key = Column(String(64), nullable=False)
    conversation_id = Column(
        Integer,
        ForeignKey("navia_ai_conversations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    usuario_id = Column(Integer, ForeignKey("navia_usuarios.id", ondelete="CASCADE"), nullable=False, index=True)
    requested_by_message_id = Column(Integer, ForeignKey("navia_ai_messages.id", ondelete="SET NULL"), nullable=True)
    action_type = Column(String(60), nullable=False, index=True)
    summary = Column(Text, nullable=False)
    payload = Column(JSON, nullable=False, default=dict)
    status = Column(String(24), nullable=False, default="pending", server_default="pending", index=True)
    version = Column(Integer, nullable=False, default=1, server_default="1")
    result = Column(JSON, nullable=False, default=dict)
    error = Column(Text, nullable=True)
    expires_at = Column(DateTime(timezone=True), nullable=False, index=True)
    confirmed_at = Column(DateTime(timezone=True), nullable=True)
    executed_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)

    conversation = relationship("AIConversation", back_populates="pending_actions")
    usuario = relationship("Usuario")
    requested_by_message = relationship("AIMessage")


class AIAutomation(Base):
    __tablename__ = "navia_ai_automations"
    __table_args__ = (Index("ix_navia_ai_automation_due", "enabled", "next_run_at"),)

    id = Column(Integer, primary_key=True)
    usuario_id = Column(Integer, ForeignKey("navia_usuarios.id", ondelete="CASCADE"), nullable=False, index=True)
    agent_id = Column(Integer, ForeignKey("navia_ai_agents.id", ondelete="SET NULL"), nullable=True, index=True)
    name = Column(String(180), nullable=False)
    metric_key = Column(String(80), nullable=False, index=True)
    parameters = Column(JSON, nullable=False, default=dict)
    condition_operator = Column(String(24), nullable=False, default="always", server_default="always")
    threshold = Column(Float, nullable=True)
    recurrence = Column(String(24), nullable=False, default="daily", server_default="daily")
    interval_minutes = Column(Integer, nullable=True)
    timezone = Column(String(80), nullable=False, default="America/Costa_Rica")
    next_run_at = Column(DateTime(timezone=True), nullable=False, index=True)
    channels = Column(JSON, nullable=False, default=list)
    email_recipients = Column(JSON, nullable=False, default=list)
    telegram_chat_ids = Column(JSON, nullable=False, default=list)
    message_template = Column(Text, nullable=True)
    ai_enhance = Column(Boolean, nullable=False, default=True, server_default="true")
    enabled = Column(Boolean, nullable=False, default=True, server_default="true", index=True)
    last_value = Column(JSON, nullable=False, default=dict)
    last_run_at = Column(DateTime(timezone=True), nullable=True)
    last_sent_at = Column(DateTime(timezone=True), nullable=True)
    last_error = Column(Text, nullable=True)
    run_count = Column(Integer, nullable=False, default=0, server_default="0")
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)

    usuario = relationship("Usuario")
    agent = relationship("AIAgent")
    runs = relationship("AIAutomationRun", back_populates="automation", cascade="all, delete-orphan")


class AIAutomationRun(Base):
    __tablename__ = "navia_ai_automation_runs"
    __table_args__ = (
        UniqueConstraint("automation_id", "scheduled_for", name="uq_navia_ai_automation_scheduled_run"),
        Index("ix_navia_ai_automation_run_created", "automation_id", "created_at"),
    )

    id = Column(Integer, primary_key=True)
    automation_id = Column(
        Integer,
        ForeignKey("navia_ai_automations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    status = Column(String(24), nullable=False, default="running", index=True)
    scheduled_for = Column(DateTime(timezone=True), nullable=False)
    value = Column(JSON, nullable=False, default=dict)
    message = Column(Text, nullable=True)
    delivery_results = Column(JSON, nullable=False, default=list)
    error = Column(Text, nullable=True)
    started_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)
    finished_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    automation = relationship("AIAutomation", back_populates="runs")


class AITelegramConnection(Base):
    __tablename__ = "navia_ai_telegram_connections"
    __table_args__ = (
        UniqueConstraint("chat_id", name="uq_navia_ai_telegram_chat"),
        Index("ix_navia_ai_telegram_user_active", "usuario_id", "active"),
    )

    id = Column(Integer, primary_key=True)
    usuario_id = Column(Integer, ForeignKey("navia_usuarios.id", ondelete="CASCADE"), nullable=False, index=True)
    chat_id = Column(String(80), nullable=False)
    chat_username = Column(String(120), nullable=True)
    display_name = Column(String(180), nullable=True)
    active = Column(Boolean, nullable=False, default=True, server_default="true")
    paired_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)
    last_seen_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    usuario = relationship("Usuario")


class AITelegramPairing(Base):
    __tablename__ = "navia_ai_telegram_pairings"

    id = Column(Integer, primary_key=True)
    usuario_id = Column(Integer, ForeignKey("navia_usuarios.id", ondelete="CASCADE"), nullable=False, index=True)
    code_hash = Column(String(64), nullable=False, unique=True, index=True)
    expires_at = Column(DateTime(timezone=True), nullable=False, index=True)
    used_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    usuario = relationship("Usuario")


class AIAuditLog(Base):
    __tablename__ = "navia_ai_audit_log"
    __table_args__ = (Index("ix_navia_ai_audit_user_created", "usuario_id", "created_at"),)

    id = Column(Integer, primary_key=True)
    usuario_id = Column(Integer, ForeignKey("navia_usuarios.id", ondelete="SET NULL"), nullable=True, index=True)
    conversation_id = Column(Integer, ForeignKey("navia_ai_conversations.id", ondelete="SET NULL"), nullable=True)
    channel = Column(String(24), nullable=False, default="web")
    action = Column(String(80), nullable=False, index=True)
    status = Column(String(24), nullable=False, index=True)
    request_id = Column(String(80), nullable=True, index=True)
    input_payload = Column(JSON, nullable=False, default=dict)
    output_payload = Column(JSON, nullable=False, default=dict)
    error = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    usuario = relationship("Usuario")
    conversation = relationship("AIConversation")
