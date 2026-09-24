from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session, selectinload

from config import AI_CHAT_RATE_LIMIT_PER_MINUTE
from database import get_db
from modules.mod_caficultura.model_ai_consulting import AIAgent, AIAuditLog, AIConversation, AIMessage
from modules.mod_caficultura.models import Cliente, Finca
from core.models import Usuario
from rate_limit import enforce_rate_limit
from core.routers.auth import get_current_user
from modules.mod_caficultura.schema_ai_consulting import (
    AIFarmConversationCreate,
    AIFarmMessageIn,
    AIConversationOut,
    AIMessageOut,
    AIMessageSendOut,
)
from modules.mod_caficultura.ai_agent import ensure_default_agent
from modules.mod_caficultura.ai_farm_agent import process_farm_message


logger = logging.getLogger("navia-api.ai-farm")
router = APIRouter(prefix="/ai/farms", tags=["ai-farm"])


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _farm_or_404(db: Session, finca_id: int) -> Finca:
    row = (
        db.query(Finca)
        .options(selectinload(Finca.cliente))
        .join(Cliente, Cliente.id == Finca.cliente_id)
        .filter(Finca.id == finca_id)
        .first()
    )
    if not row:
        raise HTTPException(status_code=404, detail="Finca no encontrada")
    return row


def _owned_conversation(
    db: Session, finca_id: int, conversation_id: int, user: Usuario
) -> AIConversation:
    _farm_or_404(db, finca_id)
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
            AIConversation.finca_id == finca_id,
            AIConversation.channel == "farm_web",
            AIConversation.status != "deleted",
        )
        .first()
    )
    if not row:
        # No se revela si la conversación existe en otra finca o usuario.
        raise HTTPException(
            status_code=404, detail="Conversación de finca no encontrada"
        )
    return row


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


def _message_out(row: AIMessage) -> AIMessageOut:
    return AIMessageOut(
        id=row.id,
        conversation_id=row.conversation_id,
        role=row.role,
        content=row.content,
        content_type=row.content_type,
        status=row.status,
        meta=row.meta or {},
        attachments=[],
        pending_actions=[],
        created_at=row.created_at,
    )


@router.post(
    "/{finca_id}/conversations",
    response_model=AIConversationOut,
    status_code=status.HTTP_201_CREATED,
)
def create_farm_conversation(
    finca_id: int,
    payload: AIFarmConversationCreate,
    current: Usuario = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    farm = _farm_or_404(db, finca_id)
    if payload.agent_id:
        agent = (
            db.query(AIAgent)
            .filter(AIAgent.id == payload.agent_id, AIAgent.enabled.is_(True))
            .first()
        )
        if not agent:
            raise HTTPException(
                status_code=422, detail="El agente no existe o está deshabilitado"
            )
    else:
        agent = ensure_default_agent(db, current.id)
    row = AIConversation(
        usuario_id=current.id,
        agent_id=agent.id,
        finca_id=farm.id,
        title=f"Sensores · {farm.nombre}"[:180],
        channel="farm_web",
        status="active",
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    row.agent = agent
    return _conversation_out(row)


@router.get(
    "/{finca_id}/conversations/{conversation_id}/messages",
    response_model=list[AIMessageOut],
)
def list_farm_messages(
    finca_id: int,
    conversation_id: int,
    current: Usuario = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    conversation = _owned_conversation(db, finca_id, conversation_id, current)
    return [_message_out(row) for row in conversation.messages]


@router.post(
    "/{finca_id}/conversations/{conversation_id}/messages",
    response_model=AIMessageSendOut,
)
def send_farm_message(
    finca_id: int,
    conversation_id: int,
    payload: AIFarmMessageIn,
    request: Request,
    current: Usuario = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    conversation = _owned_conversation(db, finca_id, conversation_id, current)
    if conversation.status != "active":
        raise HTTPException(status_code=409, detail="La conversación está archivada")
    enforce_rate_limit(
        request, f"ai-farm-user-{current.id}", AI_CHAT_RATE_LIMIT_PER_MINUTE, 60
    )
    user_message = AIMessage(
        conversation_id=conversation.id,
        role="user",
        content=payload.text.strip(),
        content_type="text",
        status="completed",
        meta={"finca_id": finca_id, "read_only": True},
    )
    db.add(user_message)
    conversation.last_message_at = utcnow()
    db.commit()

    conversation = _owned_conversation(db, finca_id, conversation_id, current)
    user_message = next(
        row for row in conversation.messages if row.id == user_message.id
    )
    try:
        assistant = process_farm_message(
            db,
            conversation,
            current,
            user_message,
            request_id=request.headers.get("x-request-id"),
        )
        conversation.last_message_at = utcnow()
        db.commit()
    except Exception as exc:
        db.rollback()
        logger.exception("Falló análisis IA de finca %s", finca_id)
        assistant = AIMessage(
            conversation_id=conversation_id,
            role="assistant",
            content=(
                "El análisis agrícola con IA no está disponible todavía. Revise la configuración del agente y OPENAI_API_KEY."
                if "configur" in str(exc).lower() or "openai" in str(exc).lower()
                else "No pude completar el análisis. Ninguna lectura ni configuración fue modificada; puede intentarlo de nuevo."
            ),
            content_type="error",
            status="failed",
            meta={"retryable": True, "finca_id": finca_id, "read_only": True},
        )
        db.add(assistant)
        db.add(
            AIAuditLog(
                usuario_id=current.id,
                conversation_id=conversation_id,
                channel="farm_web",
                action="farm_sensor_analysis",
                status="failed",
                request_id=request.headers.get("x-request-id"),
                input_payload={"message_id": user_message.id, "finca_id": finca_id},
                output_payload={},
                error=str(exc)[:2_000],
            )
        )
        db.commit()

    conversation = _owned_conversation(db, finca_id, conversation_id, current)
    user_message = next(
        row for row in conversation.messages if row.id == user_message.id
    )
    assistant = next(row for row in conversation.messages if row.id == assistant.id)
    return AIMessageSendOut(
        conversation=_conversation_out(conversation),
        user_message=_message_out(user_message),
        assistant_message=_message_out(assistant),
    )
