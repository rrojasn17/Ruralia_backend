from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    File,
    Form,
    Header,
    HTTPException,
    Request,
    UploadFile,
    status,
)
from fastapi.responses import FileResponse
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from config import (
    AI_ALLOWED_MODELS,
    AI_CHAT_RATE_LIMIT_PER_MINUTE,
    AI_CONSULTING_ENABLED,
    AI_DEFAULT_MODEL,
    AI_MAX_UPLOAD_MB,
    AI_KNOWLEDGE_MAX_DOCUMENTS,
    AI_TRANSCRIPTION_MODEL,
    TELEGRAM_PAIRING_MINUTES,
    TELEGRAM_WEBHOOK_SECRET,
    TELEGRAM_WEBHOOK_URL,
    openai_is_configured,
    telegram_is_configured,
)
from database import get_db
from modules.mod_caficultura.model_ai_consulting import (
    AIAgent,
    AIAttachment,
    AIAuditLog,
    AIAutomation,
    AIAutomationRun,
    AIConversation,
    AIMessage,
    AIPendingAction,
    AITelegramConnection,
    AITelegramPairing,
)
from core.models import Usuario
from core.routers.auth import require_roles
from rate_limit import enforce_rate_limit
from modules.mod_caficultura.schema_ai_consulting import (
    AIActionConfirm,
    AIAgentCreate,
    AIAgentOut,
    AIAgentUpdate,
    AIAttachmentOut,
    AIAutomationCreate,
    AIAutomationOut,
    AIAutomationRunOut,
    AIAutomationUpdate,
    AIConfigOut,
    AIConversationCreate,
    AIConversationOut,
    AIMessageOut,
    AIMessageSendOut,
    AIPendingActionOut,
    AITelegramConnectionOut,
    AITelegramPairingOut,
    MessageOut,
    TelegramTestIn,
)
from modules.mod_caficultura.ai_actions import cancel_pending_action, confirm_pending_action
from modules.mod_caficultura.ai_agent import ensure_default_agent, process_message, transcribe_audio
from modules.mod_caficultura.ai_automation_engine import process_due_automations
from modules.mod_caficultura.ai_metrics import METRIC_OPTIONS, execute_metric
from services.ai_storage import resolve_storage_path, save_upload
from modules.mod_caficultura.telegram_bridge import process_telegram_update
from services.telegram_service import TelegramError, configure_webhook, send_message


logger = logging.getLogger("navia-api.ai-router")
router = APIRouter(prefix="/ai", tags=["ai-consulting"])
manager_dependency = require_roles("gerente", "administrador", "admin", "superadmin")

AUTOMATION_RECURRENCES = [
    {"value": "once", "label": "Una vez"},
    {"value": "interval", "label": "Cada cierto número de minutos"},
    {"value": "daily", "label": "Diaria"},
    {"value": "weekly", "label": "Semanal"},
    {"value": "monthly", "label": "Mensual"},
]
AUTOMATION_CONDITIONS = [
    {"value": "always", "label": "Enviar siempre"},
    {"value": "changed", "label": "Cuando cambie el valor"},
    {"value": "gt", "label": "Mayor que"},
    {"value": "gte", "label": "Mayor o igual que"},
    {"value": "lt", "label": "Menor que"},
    {"value": "lte", "label": "Menor o igual que"},
    {"value": "eq", "label": "Igual a"},
]


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _conversation_out(row: AIConversation) -> AIConversationOut:
    return AIConversationOut(
        id=row.id,
        usuario_id=row.usuario_id,
        agent_id=row.agent_id,
        finca_id=row.finca_id,
        agent_name=row.agent.name if row.agent else None,
        title=row.title,
        channel=row.channel,
        status=row.status,
        last_message_at=row.last_message_at,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _attachment_out(row: AIAttachment) -> AIAttachmentOut:
    return AIAttachmentOut(
        id=row.id,
        original_name=row.original_name,
        media_type=row.media_type,
        kind=row.kind,
        size_bytes=row.size_bytes,
        download_url=f"/ai/attachments/{row.id}",
        created_at=row.created_at,
    )


def _action_out(row: AIPendingAction) -> AIPendingActionOut:
    return AIPendingActionOut.model_validate(row)


def _message_out(row: AIMessage, actions: list[AIPendingAction] | None = None) -> AIMessageOut:
    action_ids = set((row.meta or {}).get("pending_action_ids") or [])
    related = [item for item in (actions or []) if item.public_id in action_ids]
    return AIMessageOut(
        id=row.id,
        conversation_id=row.conversation_id,
        role=row.role,
        content=row.content,
        content_type=row.content_type,
        status=row.status,
        meta=row.meta or {},
        attachments=[_attachment_out(item) for item in (row.attachments or [])],
        pending_actions=[_action_out(item) for item in related],
        created_at=row.created_at,
    )


def _owned_conversation(db: Session, conversation_id: int, user: Usuario) -> AIConversation:
    row = (
        db.query(AIConversation)
        .populate_existing()
        .options(
            selectinload(AIConversation.agent),
            selectinload(AIConversation.messages).selectinload(AIMessage.attachments),
            selectinload(AIConversation.pending_actions),
        )
        .filter(
            AIConversation.id == conversation_id,
            AIConversation.usuario_id == user.id,
            AIConversation.channel != "farm_web",
            AIConversation.status != "deleted",
        )
        .first()
    )
    if not row:
        raise HTTPException(status_code=404, detail="Conversación no encontrada")
    return row


def _validate_model(model: str) -> None:
    if model not in AI_ALLOWED_MODELS:
        raise HTTPException(status_code=422, detail="El modelo no está incluido en AI_ALLOWED_MODELS")


def _set_default_agent(db: Session, agent: AIAgent) -> None:
    db.query(AIAgent).filter(AIAgent.id != agent.id, AIAgent.is_default.is_(True)).update(
        {AIAgent.is_default: False}, synchronize_session=False
    )
    agent.is_default = True
    agent.enabled = True


def _validate_telegram_recipients(db: Session, user: Usuario, chat_ids: list[str]) -> None:
    if not chat_ids or bool(user.is_superadmin):
        return
    allowed = {
        row.chat_id
        for row in db.query(AITelegramConnection).filter(
            AITelegramConnection.usuario_id == user.id,
            AITelegramConnection.active.is_(True),
        )
    }
    invalid = sorted(set(chat_ids) - allowed)
    if invalid:
        raise HTTPException(status_code=422, detail="Uno o más Chat ID no están vinculados a su cuenta")


def _validate_automation_metric(db: Session, metric_key: str, parameters: dict[str, Any]) -> None:
    required = {
        "farm_spend": ("farm_name", "Indique la finca que desea medir"),
        "worker_status": ("worker_name", "Indique el trabajador que desea seguir"),
        "lot_status": ("lot_code", "Indique el lote que desea seguir"),
    }
    requirement = required.get(metric_key)
    if requirement and not str(parameters.get(requirement[0]) or "").strip():
        raise HTTPException(status_code=422, detail=requirement[1])
    if metric_key == "lot_qr" and not any(
        str(parameters.get(key) or "").strip() for key in ("client_name", "lot_code")
    ):
        raise HTTPException(status_code=422, detail="Indique el cliente o el lote vendido")
    result = execute_metric(db, metric_key, parameters)
    if not result.get("ok"):
        raise HTTPException(status_code=422, detail=str(result.get("message") or "Los parámetros de la métrica no son válidos"))


@router.get("/config", response_model=AIConfigOut)
def get_ai_config(current: Usuario = Depends(manager_dependency)):
    return AIConfigOut(
        enabled=AI_CONSULTING_ENABLED,
        openai_configured=openai_is_configured(),
        telegram_configured=telegram_is_configured(),
        telegram_webhook_configured=bool(TELEGRAM_WEBHOOK_URL and TELEGRAM_WEBHOOK_SECRET),
        allowed_models=AI_ALLOWED_MODELS,
        default_model=AI_DEFAULT_MODEL,
        transcription_model=AI_TRANSCRIPTION_MODEL,
        max_upload_mb=AI_MAX_UPLOAD_MB,
        knowledge_max_documents=AI_KNOWLEDGE_MAX_DOCUMENTS,
        knowledge_extensions=["pdf", "docx", "xlsx", "csv", "json", "md", "txt"],
        metric_options=METRIC_OPTIONS,
        automation_recurrences=AUTOMATION_RECURRENCES,
        automation_conditions=AUTOMATION_CONDITIONS,
    )


@router.get("/agents", response_model=list[AIAgentOut])
def list_agents(current: Usuario = Depends(manager_dependency), db: Session = Depends(get_db)):
    ensure_default_agent(db, current.id)
    db.commit()
    return db.query(AIAgent).order_by(AIAgent.is_default.desc(), AIAgent.name.asc()).all()


@router.post("/agents", response_model=AIAgentOut, status_code=status.HTTP_201_CREATED)
def create_agent(payload: AIAgentCreate, current: Usuario = Depends(manager_dependency), db: Session = Depends(get_db)):
    _validate_model(payload.model)
    row = AIAgent(**payload.model_dump(), created_by_id=current.id)
    if row.is_default:
        db.query(AIAgent).filter(AIAgent.is_default.is_(True)).update(
            {AIAgent.is_default: False}, synchronize_session=False
        )
    db.add(row)
    try:
        db.flush()
        db.commit()
        db.refresh(row)
        return row
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="Ya existe un agente con ese identificador") from exc


@router.patch("/agents/{agent_id}", response_model=AIAgentOut)
def update_agent(
    agent_id: int,
    payload: AIAgentUpdate,
    current: Usuario = Depends(manager_dependency),
    db: Session = Depends(get_db),
):
    row = db.query(AIAgent).filter(AIAgent.id == agent_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Agente no encontrado")
    values = payload.model_dump(exclude_unset=True)
    if values.get("model"):
        _validate_model(str(values["model"]))
    make_default = values.pop("is_default", None)
    for key, value in values.items():
        setattr(row, key, value)
    if make_default:
        _set_default_agent(db, row)
    elif make_default is False:
        row.is_default = False
    if row.is_default and not row.enabled:
        row.is_default = False
    db.flush()
    if not db.query(AIAgent).filter(AIAgent.is_default.is_(True), AIAgent.enabled.is_(True)).first():
        ensure_default_agent(db, current.id)
    db.commit()
    db.refresh(row)
    return row


@router.get("/conversations", response_model=list[AIConversationOut])
def list_conversations(current: Usuario = Depends(manager_dependency), db: Session = Depends(get_db)):
    rows = (
        db.query(AIConversation)
        .options(selectinload(AIConversation.agent))
        .filter(AIConversation.usuario_id == current.id, AIConversation.status != "deleted")
        .filter(AIConversation.channel != "farm_web")
        .order_by(AIConversation.updated_at.desc())
        .limit(100)
        .all()
    )
    return [_conversation_out(row) for row in rows]


@router.post("/conversations", response_model=AIConversationOut, status_code=status.HTTP_201_CREATED)
def create_conversation(
    payload: AIConversationCreate,
    current: Usuario = Depends(manager_dependency),
    db: Session = Depends(get_db),
):
    agent = None
    if payload.agent_id:
        agent = db.query(AIAgent).filter(AIAgent.id == payload.agent_id, AIAgent.enabled.is_(True)).first()
        if not agent:
            raise HTTPException(status_code=422, detail="El agente no existe o está deshabilitado")
    else:
        agent = ensure_default_agent(db, current.id)
    row = AIConversation(
        usuario_id=current.id,
        agent_id=agent.id,
        title=(payload.title or "Nueva consulta").strip()[:180],
        channel="web",
        status="active",
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    row.agent = agent
    return _conversation_out(row)


@router.get("/conversations/{conversation_id}/messages", response_model=list[AIMessageOut])
def list_messages(
    conversation_id: int,
    current: Usuario = Depends(manager_dependency),
    db: Session = Depends(get_db),
):
    conversation = _owned_conversation(db, conversation_id, current)
    return [_message_out(row, conversation.pending_actions) for row in conversation.messages]


@router.post("/conversations/{conversation_id}/archive", response_model=AIConversationOut)
def archive_conversation(
    conversation_id: int,
    current: Usuario = Depends(manager_dependency),
    db: Session = Depends(get_db),
):
    row = _owned_conversation(db, conversation_id, current)
    row.status = "archived"
    db.commit()
    db.refresh(row)
    return _conversation_out(row)


@router.post("/conversations/{conversation_id}/messages", response_model=AIMessageSendOut)
def send_chat_message(
    conversation_id: int,
    request: Request,
    text: str | None = Form(default=None),
    latitude: float | None = Form(default=None),
    longitude: float | None = Form(default=None),
    accuracy: float | None = Form(default=None),
    files: list[UploadFile] | None = File(default=None),
    current: Usuario = Depends(manager_dependency),
    db: Session = Depends(get_db),
):
    conversation = _owned_conversation(db, conversation_id, current)
    if conversation.status != "active":
        raise HTTPException(status_code=409, detail="La conversación está archivada")
    enforce_rate_limit(
        request,
        f"ai-chat-user-{current.id}",
        AI_CHAT_RATE_LIMIT_PER_MINUTE,
        60,
    )
    uploads = list(files or [])
    if len(uploads) > 6:
        raise HTTPException(status_code=422, detail="Puede adjuntar como máximo 6 archivos por mensaje")
    clean_text = str(text or "").strip()
    has_location = latitude is not None or longitude is not None
    if has_location and (latitude is None or longitude is None):
        raise HTTPException(status_code=422, detail="La ubicación requiere latitud y longitud")
    if latitude is not None and not -90 <= latitude <= 90:
        raise HTTPException(status_code=422, detail="Latitud inválida")
    if longitude is not None and not -180 <= longitude <= 180:
        raise HTTPException(status_code=422, detail="Longitud inválida")
    if not clean_text and not uploads and not has_location:
        raise HTTPException(status_code=422, detail="Envíe un mensaje, archivo, audio, imagen o ubicación")

    meta: dict[str, Any] = {}
    if has_location:
        meta["location"] = {"latitude": latitude, "longitude": longitude, "accuracy": accuracy}
        clean_text = clean_text or f"Compartí esta ubicación: {latitude}, {longitude}."
    user_message = AIMessage(
        conversation_id=conversation.id,
        role="user",
        content=clean_text,
        content_type="location" if has_location else "text",
        status="completed",
        meta=meta,
    )
    db.add(user_message)
    db.flush()
    stored_paths: list[Path] = []
    try:
        attachments: list[AIAttachment] = []
        for upload in uploads:
            stored = save_upload(upload, current.id)
            stored_paths.append(resolve_storage_path(str(stored["storage_key"])))
            attachment = AIAttachment(message_id=user_message.id, usuario_id=current.id, **stored)
            db.add(attachment)
            attachments.append(attachment)
        db.flush()
        transcripts: list[str] = []
        transcription_errors: list[str] = []
        for attachment in attachments:
            if attachment.kind != "audio":
                continue
            try:
                transcripts.append(transcribe_audio(attachment))
            except Exception:
                logger.exception("No se pudo transcribir adjunto %s", attachment.id)
                transcription_errors.append(attachment.original_name)
        if transcripts:
            user_message.content = f"{clean_text}\n\nTranscripción del audio:\n" + "\n".join(transcripts)
            user_message.meta = {**meta, "transcripts": transcripts}
        elif uploads and not user_message.content:
            names = ", ".join(attachment.original_name for attachment in attachments)
            user_message.content = f"Adjunté: {names}."
        if transcription_errors:
            user_message.meta = {**(user_message.meta or {}), "transcription_errors": transcription_errors}
            if not transcripts and not clean_text:
                user_message.content += " No fue posible transcribir el audio; solicítame volver a grabarlo."
        kinds = {attachment.kind for attachment in attachments}
        if len(kinds) == 1 and not clean_text and not has_location:
            user_message.content_type = next(iter(kinds))
        elif attachments:
            user_message.content_type = "mixed"
        if conversation.title == "Nueva consulta":
            conversation.title = user_message.content.replace("\n", " ")[:80] or "Consulta con archivos"
        conversation.last_message_at = utcnow()
        db.commit()
    except Exception:
        db.rollback()
        for path in stored_paths:
            path.unlink(missing_ok=True)
        raise

    conversation = _owned_conversation(db, conversation_id, current)
    user_message = next(item for item in conversation.messages if item.id == user_message.id)
    request_id = request.headers.get("x-request-id")
    try:
        assistant = process_message(db, conversation, current, user_message, request_id=request_id)
        conversation.last_message_at = utcnow()
        db.commit()
    except Exception as exc:
        db.rollback()
        logger.exception("Falló respuesta IA en conversación %s", conversation_id)
        content = (
            "El servicio de IA todavía no está configurado. Un administrador debe revisar OPENAI_API_KEY y el modelo permitido."
            if "configur" in str(exc).lower() or "openai_api_key" in str(exc).lower()
            else "No pude completar la respuesta. Los datos de NAVIA no fueron modificados; puede intentarlo nuevamente."
        )
        assistant = AIMessage(
            conversation_id=conversation_id,
            role="assistant",
            content=content,
            content_type="error",
            status="failed",
            meta={"retryable": True},
        )
        db.add(assistant)
        db.add(
            AIAuditLog(
                usuario_id=current.id,
                conversation_id=conversation_id,
                channel="web",
                action="chat_completion",
                status="failed",
                request_id=request_id,
                input_payload={"message_id": user_message.id},
                output_payload={},
                error=str(exc)[:2000],
            )
        )
        db.commit()
    conversation = _owned_conversation(db, conversation_id, current)
    assistant = next(item for item in conversation.messages if item.id == assistant.id)
    user_message = next(item for item in conversation.messages if item.id == user_message.id)
    return AIMessageSendOut(
        conversation=_conversation_out(conversation),
        user_message=_message_out(user_message, conversation.pending_actions),
        assistant_message=_message_out(assistant, conversation.pending_actions),
    )


@router.get("/attachments/{attachment_id}")
def download_attachment(
    attachment_id: int,
    current: Usuario = Depends(manager_dependency),
    db: Session = Depends(get_db),
):
    row = db.query(AIAttachment).filter(
        AIAttachment.id == attachment_id,
        AIAttachment.usuario_id == current.id,
    ).first()
    if not row:
        raise HTTPException(status_code=404, detail="Archivo no encontrado")
    path = resolve_storage_path(row.storage_key)
    if not path.is_file():
        raise HTTPException(status_code=410, detail="El archivo ya no está disponible")
    disposition = "inline" if row.kind in {"image", "audio"} else "attachment"
    return FileResponse(
        path,
        media_type=row.media_type,
        filename=row.original_name,
        content_disposition_type=disposition,
        headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"},
    )


@router.post("/actions/{public_id}/confirm", response_model=AIPendingActionOut)
def confirm_action(
    public_id: str,
    payload: AIActionConfirm,
    request: Request,
    current: Usuario = Depends(manager_dependency),
    db: Session = Depends(get_db),
):
    row = confirm_pending_action(
        db,
        public_id,
        current,
        payload.expected_version,
        request_id=request.headers.get("x-request-id"),
    )
    return _action_out(row)


@router.post("/actions/{public_id}/cancel", response_model=AIPendingActionOut)
def cancel_action(
    public_id: str,
    current: Usuario = Depends(manager_dependency),
    db: Session = Depends(get_db),
):
    return _action_out(cancel_pending_action(db, public_id, current))


@router.get("/automations", response_model=list[AIAutomationOut])
def list_automations(current: Usuario = Depends(manager_dependency), db: Session = Depends(get_db)):
    query = db.query(AIAutomation)
    if not current.is_superadmin:
        query = query.filter(AIAutomation.usuario_id == current.id)
    return query.order_by(AIAutomation.enabled.desc(), AIAutomation.next_run_at.asc()).all()


@router.post("/automations", response_model=AIAutomationOut, status_code=status.HTTP_201_CREATED)
def create_automation(
    payload: AIAutomationCreate,
    current: Usuario = Depends(manager_dependency),
    db: Session = Depends(get_db),
):
    if payload.metric_key not in {item["value"] for item in METRIC_OPTIONS}:
        raise HTTPException(status_code=422, detail="Métrica de automatización no permitida")
    _validate_automation_metric(db, payload.metric_key, payload.parameters)
    if payload.agent_id and not db.query(AIAgent).filter(AIAgent.id == payload.agent_id, AIAgent.enabled.is_(True)).first():
        raise HTTPException(status_code=422, detail="Agente no encontrado o deshabilitado")
    _validate_telegram_recipients(db, current, payload.telegram_chat_ids)
    values = payload.model_dump()
    row = AIAutomation(usuario_id=current.id, **values)
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


@router.patch("/automations/{automation_id}", response_model=AIAutomationOut)
def update_automation(
    automation_id: int,
    payload: AIAutomationUpdate,
    current: Usuario = Depends(manager_dependency),
    db: Session = Depends(get_db),
):
    query = db.query(AIAutomation).filter(AIAutomation.id == automation_id)
    if not current.is_superadmin:
        query = query.filter(AIAutomation.usuario_id == current.id)
    row = query.first()
    if not row:
        raise HTTPException(status_code=404, detail="Automatización no encontrada")
    current_values = {
        "name": row.name,
        "agent_id": row.agent_id,
        "metric_key": row.metric_key,
        "parameters": row.parameters or {},
        "condition_operator": row.condition_operator,
        "threshold": row.threshold,
        "recurrence": row.recurrence,
        "interval_minutes": row.interval_minutes,
        "timezone": row.timezone,
        "next_run_at": row.next_run_at,
        "channels": row.channels or [],
        "email_recipients": row.email_recipients or [],
        "telegram_chat_ids": row.telegram_chat_ids or [],
        "message_template": row.message_template,
        "ai_enhance": row.ai_enhance,
        "enabled": row.enabled,
    }
    current_values.update(payload.model_dump(exclude_unset=True))
    validated = AIAutomationCreate.model_validate(current_values)
    if validated.metric_key not in {item["value"] for item in METRIC_OPTIONS}:
        raise HTTPException(status_code=422, detail="Métrica de automatización no permitida")
    _validate_automation_metric(db, validated.metric_key, validated.parameters)
    _validate_telegram_recipients(db, current, validated.telegram_chat_ids)
    for key, value in validated.model_dump().items():
        setattr(row, key, value)
    db.commit()
    db.refresh(row)
    return row


@router.delete("/automations/{automation_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_automation(
    automation_id: int,
    current: Usuario = Depends(manager_dependency),
    db: Session = Depends(get_db),
):
    query = db.query(AIAutomation).filter(AIAutomation.id == automation_id)
    if not current.is_superadmin:
        query = query.filter(AIAutomation.usuario_id == current.id)
    row = query.first()
    if not row:
        raise HTTPException(status_code=404, detail="Automatización no encontrada")
    db.delete(row)
    db.commit()


@router.post("/automations/{automation_id}/run", response_model=list[AIAutomationRunOut])
def run_automation(
    automation_id: int,
    current: Usuario = Depends(manager_dependency),
    db: Session = Depends(get_db),
):
    query = db.query(AIAutomation).filter(AIAutomation.id == automation_id)
    if not current.is_superadmin:
        query = query.filter(AIAutomation.usuario_id == current.id)
    row = query.first()
    if not row:
        raise HTTPException(status_code=404, detail="Automatización no encontrada")
    row.enabled = True
    row.next_run_at = utcnow()
    db.commit()
    process_due_automations(db, utcnow(), automation_ids={row.id})
    db.commit()
    return (
        db.query(AIAutomationRun)
        .filter(AIAutomationRun.automation_id == row.id)
        .order_by(AIAutomationRun.created_at.desc())
        .limit(5)
        .all()
    )


@router.get("/telegram/connections", response_model=list[AITelegramConnectionOut])
def telegram_connections(current: Usuario = Depends(manager_dependency), db: Session = Depends(get_db)):
    return db.query(AITelegramConnection).filter(AITelegramConnection.usuario_id == current.id).order_by(
        AITelegramConnection.active.desc(), AITelegramConnection.paired_at.desc()
    ).all()


@router.post("/telegram/pairing", response_model=AITelegramPairingOut)
def create_telegram_pairing(current: Usuario = Depends(manager_dependency), db: Session = Depends(get_db)):
    if not telegram_is_configured():
        raise HTTPException(status_code=503, detail="TELEGRAM_BOT_TOKEN no está configurado")
    code = f"{secrets.randbelow(100_000_000):08d}"
    expires_at = utcnow() + timedelta(minutes=TELEGRAM_PAIRING_MINUTES)
    row = AITelegramPairing(
        usuario_id=current.id,
        code_hash=hashlib.sha256(code.encode("utf-8")).hexdigest(),
        expires_at=expires_at,
    )
    db.add(row)
    db.commit()
    return AITelegramPairingOut(code=code, command=f"/vincular {code}", expires_at=expires_at)


@router.delete("/telegram/connections/{connection_id}", status_code=status.HTTP_204_NO_CONTENT)
def disconnect_telegram(
    connection_id: int,
    current: Usuario = Depends(manager_dependency),
    db: Session = Depends(get_db),
):
    row = db.query(AITelegramConnection).filter(
        AITelegramConnection.id == connection_id,
        AITelegramConnection.usuario_id == current.id,
    ).first()
    if not row:
        raise HTTPException(status_code=404, detail="Vinculación no encontrada")
    row.active = False
    db.commit()


@router.post("/telegram/setup-webhook")
def setup_telegram_webhook(current: Usuario = Depends(manager_dependency)):
    if not current.is_superadmin:
        raise HTTPException(status_code=403, detail="Solo el superadmin puede configurar el webhook global")
    try:
        return {"ok": True, "webhook": configure_webhook()}
    except TelegramError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.post("/telegram/test", response_model=MessageOut)
def test_telegram(
    payload: TelegramTestIn,
    current: Usuario = Depends(manager_dependency),
    db: Session = Depends(get_db),
):
    _validate_telegram_recipients(db, current, [payload.chat_id])
    try:
        send_message(payload.chat_id, f"✅ Prueba de NAVIA completada para {current.nombre}.")
    except TelegramError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return MessageOut(message="Mensaje de prueba enviado")


@router.post("/telegram/webhook", include_in_schema=False)
async def telegram_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
):
    if not telegram_is_configured() or len(TELEGRAM_WEBHOOK_SECRET) < 24:
        raise HTTPException(status_code=503, detail="Webhook no configurado")
    if not x_telegram_bot_api_secret_token or not hmac.compare_digest(
        x_telegram_bot_api_secret_token,
        TELEGRAM_WEBHOOK_SECRET,
    ):
        raise HTTPException(status_code=403, detail="Firma de Telegram inválida")
    try:
        update = await request.json()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="JSON inválido") from exc
    if not isinstance(update, dict) or not isinstance(update.get("update_id"), int):
        raise HTTPException(status_code=422, detail="Actualización de Telegram inválida")
    background_tasks.add_task(process_telegram_update, update)
    return {"ok": True}
