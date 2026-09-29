from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, EmailStr, Field, field_validator, model_validator


Channel = Literal["in_app", "email", "telegram"]
Recurrence = Literal["once", "interval", "daily", "weekly", "monthly"]
ConditionOperator = Literal["always", "gt", "gte", "lt", "lte", "eq", "changed"]


class AIAgentCreate(BaseModel):
    name: str = Field(min_length=3, max_length=120)
    slug: str = Field(min_length=3, max_length=80, pattern=r"^[a-z0-9][a-z0-9-]*$")
    provider: Literal["openai"] = "openai"
    model: str = Field(min_length=3, max_length=100)
    instructions: str = Field(min_length=40, max_length=20_000)
    enabled: bool = True
    is_default: bool = False
    max_output_tokens: int = Field(default=1800, ge=300, le=8000)
    capabilities: list[str] = Field(default_factory=lambda: ["read", "prepare_actions"])

    @field_validator("name", "instructions")
    @classmethod
    def strip_text(cls, value: str) -> str:
        return value.strip()


class AIAgentUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=3, max_length=120)
    model: str | None = Field(default=None, min_length=3, max_length=100)
    instructions: str | None = Field(default=None, min_length=40, max_length=20_000)
    enabled: bool | None = None
    is_default: bool | None = None
    max_output_tokens: int | None = Field(default=None, ge=300, le=8000)
    capabilities: list[str] | None = None


class AIAgentOut(BaseModel):
    id: int
    name: str
    slug: str
    provider: str
    model: str
    instructions: str
    enabled: bool
    is_default: bool
    max_output_tokens: int
    capabilities: list[str]
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class AIConversationCreate(BaseModel):
    agent_id: int | None = None
    title: str | None = Field(default=None, max_length=180)


class AIConversationOut(BaseModel):
    id: int
    usuario_id: int
    agent_id: int | None
    finca_id: int | None = None
    agent_name: str | None = None
    title: str
    channel: str
    status: str
    last_message_at: datetime | None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class AIKnowledgeDocumentOut(BaseModel):
    id: int
    agent_id: int
    title: str
    original_name: str
    media_type: str
    size_bytes: int
    character_count: int
    chunk_count: int = 0
    active: bool
    created_at: datetime
    updated_at: datetime


class AIKnowledgeDocumentUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=2, max_length=220)
    active: bool | None = None

    @field_validator("title")
    @classmethod
    def clean_title(cls, value: str | None) -> str | None:
        if value is None:
            return None
        clean = value.strip()
        if len(clean) < 2:
            raise ValueError("El título debe tener al menos 2 caracteres")
        return clean


class AIFarmConversationCreate(BaseModel):
    agent_id: int | None = None


class AIFarmMessageIn(BaseModel):
    text: str = Field(min_length=1, max_length=8_000)


class AIAttachmentOut(BaseModel):
    id: int
    original_name: str
    media_type: str
    kind: str
    size_bytes: int
    download_url: str
    created_at: datetime


class AIPendingActionOut(BaseModel):
    public_id: str
    action_type: str
    summary: str
    payload: dict[str, Any]
    status: str
    version: int
    result: dict[str, Any]
    error: str | None
    expires_at: datetime
    created_at: datetime

    model_config = {"from_attributes": True}


class AIMessageOut(BaseModel):
    id: int
    conversation_id: int
    role: str
    content: str
    content_type: str
    status: str
    meta: dict[str, Any]
    attachments: list[AIAttachmentOut] = Field(default_factory=list)
    pending_actions: list[AIPendingActionOut] = Field(default_factory=list)
    created_at: datetime


class AIMessageSendOut(BaseModel):
    conversation: AIConversationOut
    user_message: AIMessageOut
    assistant_message: AIMessageOut


class AIActionConfirm(BaseModel):
    expected_version: int = Field(ge=1)


class AIAutomationCreate(BaseModel):
    name: str = Field(min_length=3, max_length=180)
    agent_id: int | None = None
    metric_key: str = Field(min_length=3, max_length=80)
    parameters: dict[str, Any] = Field(default_factory=dict)
    condition_operator: ConditionOperator = "always"
    threshold: float | None = None
    recurrence: Recurrence = "daily"
    interval_minutes: int | None = Field(default=None, ge=15, le=43_200)
    timezone: str = Field(default="America/Costa_Rica", min_length=3, max_length=80)
    next_run_at: datetime
    channels: list[Channel] = Field(min_length=1, max_length=3)
    email_recipients: list[EmailStr] = Field(default_factory=list, max_length=20)
    telegram_chat_ids: list[str] = Field(default_factory=list, max_length=20)
    message_template: str | None = Field(default=None, max_length=4000)
    ai_enhance: bool = True
    enabled: bool = True

    @field_validator("name", "message_template")
    @classmethod
    def trim_optional_text(cls, value: str | None) -> str | None:
        return value.strip() if value else value

    @field_validator("channels")
    @classmethod
    def unique_channels(cls, value: list[str]) -> list[str]:
        return list(dict.fromkeys(value))

    @field_validator("telegram_chat_ids")
    @classmethod
    def validate_chat_ids(cls, value: list[str]) -> list[str]:
        cleaned = [str(item).strip() for item in value if str(item).strip()]
        if any(not re.fullmatch(r"-?\d{4,30}", item) for item in cleaned):
            raise ValueError("Los Chat ID de Telegram deben ser numéricos")
        return list(dict.fromkeys(cleaned))

    @model_validator(mode="after")
    def validate_delivery_and_condition(self):
        if "email" in self.channels and not self.email_recipients:
            raise ValueError("Debe indicar al menos un correo para el canal email")
        if "telegram" in self.channels and not self.telegram_chat_ids:
            raise ValueError("Debe indicar al menos un Chat ID para Telegram")
        if self.condition_operator not in {"always", "changed"} and self.threshold is None:
            raise ValueError("La condición seleccionada requiere un umbral")
        if self.recurrence == "interval" and self.interval_minutes is None:
            raise ValueError("La recurrencia por intervalo requiere minutos")
        return self


class AIAutomationUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=3, max_length=180)
    agent_id: int | None = None
    metric_key: str | None = Field(default=None, min_length=3, max_length=80)
    parameters: dict[str, Any] | None = None
    condition_operator: ConditionOperator | None = None
    threshold: float | None = None
    recurrence: Recurrence | None = None
    interval_minutes: int | None = Field(default=None, ge=15, le=43_200)
    timezone: str | None = Field(default=None, min_length=3, max_length=80)
    next_run_at: datetime | None = None
    channels: list[Channel] | None = None
    email_recipients: list[EmailStr] | None = None
    telegram_chat_ids: list[str] | None = None
    message_template: str | None = Field(default=None, max_length=4000)
    ai_enhance: bool | None = None
    enabled: bool | None = None


class AIAutomationOut(BaseModel):
    id: int
    usuario_id: int
    agent_id: int | None
    name: str
    metric_key: str
    parameters: dict[str, Any]
    condition_operator: str
    threshold: float | None
    recurrence: str
    interval_minutes: int | None
    timezone: str
    next_run_at: datetime
    channels: list[str]
    email_recipients: list[str]
    telegram_chat_ids: list[str]
    message_template: str | None
    ai_enhance: bool
    enabled: bool
    last_value: dict[str, Any]
    last_run_at: datetime | None
    last_sent_at: datetime | None
    last_error: str | None
    run_count: int
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class AIAutomationRunOut(BaseModel):
    id: int
    automation_id: int
    status: str
    scheduled_for: datetime
    value: dict[str, Any]
    message: str | None
    delivery_results: list[dict[str, Any]]
    error: str | None
    started_at: datetime
    finished_at: datetime | None

    model_config = {"from_attributes": True}


class AITelegramConnectionOut(BaseModel):
    id: int
    chat_id: str
    chat_username: str | None
    display_name: str | None
    active: bool
    paired_at: datetime
    last_seen_at: datetime | None

    model_config = {"from_attributes": True}


class AITelegramPairingOut(BaseModel):
    code: str
    command: str
    expires_at: datetime


class AIConfigOut(BaseModel):
    enabled: bool
    openai_configured: bool
    telegram_configured: bool
    telegram_webhook_configured: bool
    allowed_models: list[str]
    default_model: str
    transcription_model: str
    max_upload_mb: int
    knowledge_max_documents: int
    knowledge_extensions: list[str]
    metric_options: list[dict[str, str]]
    automation_recurrences: list[dict[str, str]]
    automation_conditions: list[dict[str, str]]


class TelegramTestIn(BaseModel):
    chat_id: str = Field(min_length=4, max_length=30, pattern=r"^-?\d+$")


class MessageOut(BaseModel):
    ok: bool = True
    message: str
