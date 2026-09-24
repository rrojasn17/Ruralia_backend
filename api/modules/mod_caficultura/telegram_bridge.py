from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import selectinload

from database import SessionLocal
from modules.mod_caficultura.model_ai_consulting import AIAttachment, AIAuditLog, AIConversation, AIMessage, AITelegramConnection, AITelegramPairing
from core.models import Usuario
from modules.mod_caficultura.ai_actions import cancel_pending_action, confirm_pending_action
from modules.mod_caficultura.ai_agent import ensure_default_agent, process_message, transcribe_audio
from services.ai_storage import resolve_storage_path, save_bytes
from services.telegram_service import (
    TelegramError,
    answer_callback_query,
    download_file,
    send_message,
)


logger = logging.getLogger("navia-api.telegram-bridge")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _hash_code(code: str) -> str:
    return hashlib.sha256(code.encode("utf-8")).hexdigest()


def _chat_data(message: dict[str, Any]) -> tuple[str, str | None, str | None]:
    chat = message.get("chat") or {}
    sender = message.get("from") or {}
    chat_id = str(chat.get("id") or "")
    username = str(sender.get("username") or "").strip() or None
    display = " ".join(
        part for part in [str(sender.get("first_name") or "").strip(), str(sender.get("last_name") or "").strip()] if part
    ) or None
    return chat_id, username, display


def _manager(user: Usuario | None) -> bool:
    if not user or not user.activo:
        return False
    return bool(user.is_superadmin) or str(user.rol or "").strip().lower() in {
        "gerente",
        "administrador",
        "admin",
        "superadmin",
    }


def _pair(db, message: dict[str, Any], code: str) -> None:
    chat_id, username, display = _chat_data(message)
    pairing = db.query(AITelegramPairing).filter(
        AITelegramPairing.code_hash == _hash_code(code),
        AITelegramPairing.used_at.is_(None),
        AITelegramPairing.expires_at > utcnow(),
    ).first()
    if not pairing or not _manager(pairing.usuario):
        send_message(chat_id, "El código de vinculación no es válido o ya venció. Genere uno nuevo desde Configuración → Agentes IA.")
        return
    occupied = db.query(AITelegramConnection).filter(AITelegramConnection.chat_id == chat_id).first()
    if occupied and occupied.usuario_id != pairing.usuario_id:
        send_message(chat_id, "Este chat ya está vinculado a otra cuenta de NAVIA. Desvincúlelo desde esa cuenta primero.")
        return
    connection = occupied or AITelegramConnection(usuario_id=pairing.usuario_id, chat_id=chat_id)
    connection.chat_username = username
    connection.display_name = display
    connection.active = True
    connection.paired_at = utcnow()
    connection.last_seen_at = utcnow()
    pairing.used_at = utcnow()
    db.add(connection)
    db.commit()
    send_message(
        chat_id,
        f"Listo, {pairing.usuario.nombre}. Este chat quedó vinculado de forma segura con NAVIA. "
        "Ya puede consultar métricas, alertas, trabajadores y trazabilidad.",
    )


def _telegram_file(message: dict[str, Any]) -> tuple[str, str, str] | None:
    if message.get("voice"):
        item = message["voice"]
        return str(item["file_id"]), f"voz-{message.get('message_id')}.ogg", str(item.get("mime_type") or "audio/ogg")
    if message.get("audio"):
        item = message["audio"]
        return str(item["file_id"]), str(item.get("file_name") or f"audio-{message.get('message_id')}.mp3"), str(item.get("mime_type") or "audio/mpeg")
    if message.get("document"):
        item = message["document"]
        return str(item["file_id"]), str(item.get("file_name") or f"documento-{message.get('message_id')}.pdf"), str(item.get("mime_type") or "application/octet-stream")
    photos = message.get("photo") or []
    if photos:
        item = photos[-1]
        return str(item["file_id"]), f"imagen-{message.get('message_id')}.jpg", "image/jpeg"
    return None


def _conversation(db, user: Usuario, chat_id: str) -> AIConversation:
    row = (
        db.query(AIConversation)
        .filter(
            AIConversation.usuario_id == user.id,
            AIConversation.channel == "telegram",
            AIConversation.external_chat_id == chat_id,
            AIConversation.status == "active",
        )
        .order_by(AIConversation.id.desc())
        .first()
    )
    if row:
        return row
    agent = ensure_default_agent(db, user.id)
    row = AIConversation(
        usuario_id=user.id,
        agent_id=agent.id,
        title="Consulta por Telegram",
        channel="telegram",
        external_chat_id=chat_id,
        status="active",
    )
    db.add(row)
    db.flush()
    return row


def _action_keyboard(assistant: AIMessage) -> dict[str, Any] | None:
    action_ids = list((assistant.meta or {}).get("pending_action_ids") or [])
    if not action_ids:
        return None
    public_id = str(action_ids[-1])
    action = next((row for row in assistant.conversation.pending_actions if row.public_id == public_id), None)
    if not action:
        return None
    return {
        "inline_keyboard": [[
            {"text": "✅ Confirmar", "callback_data": f"ai:confirm:{action.public_id}:{action.version}"},
            {"text": "Cancelar", "callback_data": f"ai:cancel:{action.public_id}:{action.version}"},
        ]]
    }


def _handle_message(db, update: dict[str, Any], message: dict[str, Any], request_id: str) -> None:
    chat_id, _, _ = _chat_data(message)
    text = str(message.get("text") or message.get("caption") or "").strip()
    if text.lower().startswith("/vincular "):
        _pair(db, message, text.split(maxsplit=1)[1].strip())
        return
    if text.lower() in {"/start", "/ayuda", "/help"}:
        send_message(chat_id, "Abra NAVIA → Configuración → Agentes IA, genere un código y envíe: /vincular CÓDIGO")
        return

    connection = (
        db.query(AITelegramConnection)
        .options(selectinload(AITelegramConnection.usuario))
        .filter(AITelegramConnection.chat_id == chat_id, AITelegramConnection.active.is_(True))
        .first()
    )
    if not connection or not _manager(connection.usuario):
        send_message(chat_id, "Este chat no está autorizado. Vincúlelo desde una cuenta gerente o administradora de NAVIA.")
        return
    user = connection.usuario
    connection.last_seen_at = utcnow()
    conversation = _conversation(db, user, chat_id)
    location = message.get("location") or {}
    meta: dict[str, Any] = {"telegram_message_id": message.get("message_id")}
    if location:
        meta["location"] = {
            "latitude": location.get("latitude"),
            "longitude": location.get("longitude"),
            "accuracy": location.get("horizontal_accuracy"),
        }
        text = text or f"Compartí esta ubicación: {location.get('latitude')}, {location.get('longitude')}"

    user_message = AIMessage(
        conversation_id=conversation.id,
        role="user",
        content=text,
        content_type="location" if location else "text",
        status="completed",
        meta=meta,
    )
    db.add(user_message)
    db.flush()
    file_spec = _telegram_file(message)
    stored_path = None
    if file_spec:
        try:
            file_id, filename, media_type = file_spec
            content, remote_path = download_file(file_id)
            stored = save_bytes(content, user.id, filename, media_type)
            stored_path = resolve_storage_path(str(stored["storage_key"]))
            attachment = AIAttachment(message_id=user_message.id, usuario_id=user.id, **stored)
            db.add(attachment)
            db.flush()
            user_message.content_type = str(stored["kind"])
            if stored["kind"] == "audio":
                transcript = transcribe_audio(attachment)
                user_message.content = f"{text}\n\nTranscripción del audio: {transcript}".strip()
                user_message.meta = {**meta, "transcript": transcript, "telegram_file_path": remote_path}
            elif not text:
                user_message.content = f"Adjunté el archivo {filename}."
        except Exception:
            if stored_path:
                stored_path.unlink(missing_ok=True)
            raise
    if not user_message.content and not file_spec:
        send_message(chat_id, "Envíe un mensaje, audio, imagen, documento o ubicación para consultar NAVIA.")
        return
    conversation.last_message_at = utcnow()
    try:
        db.commit()
    except Exception:
        if stored_path:
            stored_path.unlink(missing_ok=True)
        raise

    conversation = (
        db.query(AIConversation)
        .populate_existing()
        .options(
            selectinload(AIConversation.messages).selectinload(AIMessage.attachments),
            selectinload(AIConversation.pending_actions),
            selectinload(AIConversation.agent),
        )
        .filter(AIConversation.id == conversation.id)
        .first()
    )
    user_message = next(row for row in conversation.messages if row.id == user_message.id)
    assistant = process_message(db, conversation, user, user_message, request_id=request_id)
    conversation.last_message_at = utcnow()
    db.commit()
    db.refresh(assistant)
    # Recargar las acciones preparadas para construir los botones con su versión vigente.
    conversation = (
        db.query(AIConversation)
        .populate_existing()
        .options(selectinload(AIConversation.pending_actions))
        .filter(AIConversation.id == conversation.id)
        .first()
    )
    assistant.conversation = conversation
    send_message(chat_id, assistant.content, reply_markup=_action_keyboard(assistant))
    db.add(
        AIAuditLog(
            usuario_id=user.id,
            conversation_id=conversation.id,
            channel="telegram",
            action="telegram_update",
            status="completed",
            request_id=request_id,
            input_payload={"update_id": update.get("update_id")},
            output_payload={"assistant_message_id": assistant.id},
        )
    )
    db.commit()


def _handle_callback(db, callback: dict[str, Any], request_id: str) -> None:
    callback_id = str(callback.get("id") or "")
    message = callback.get("message") or {}
    chat_id, _, _ = _chat_data(message)
    connection = db.query(AITelegramConnection).filter(
        AITelegramConnection.chat_id == chat_id,
        AITelegramConnection.active.is_(True),
    ).first()
    if not connection or not _manager(connection.usuario):
        answer_callback_query(callback_id, "Chat no autorizado")
        return
    parts = str(callback.get("data") or "").split(":")
    if len(parts) != 4 or parts[0] != "ai":
        answer_callback_query(callback_id, "Acción inválida")
        return
    operation, public_id = parts[1], parts[2]
    try:
        version = int(parts[3])
    except ValueError:
        answer_callback_query(callback_id, "Versión inválida")
        return
    if operation == "confirm":
        action = confirm_pending_action(
            db,
            public_id,
            connection.usuario,
            version,
            channel="telegram",
            request_id=request_id,
        )
        answer_callback_query(callback_id, "Acción ejecutada")
        send_message(chat_id, f"✅ Acción completada.\n\n{action.summary}")
    elif operation == "cancel":
        action = cancel_pending_action(db, public_id, connection.usuario)
        answer_callback_query(callback_id, "Acción cancelada")
        send_message(chat_id, f"Acción cancelada: {action.summary}")
    else:
        answer_callback_query(callback_id, "Operación no permitida")


def process_telegram_update(update: dict[str, Any]) -> None:
    request_id = f"telegram:{update.get('update_id', 'unknown')}"
    db = SessionLocal()
    chat_id = ""
    try:
        if db.query(AIAuditLog).filter(AIAuditLog.request_id == request_id, AIAuditLog.status == "completed").first():
            return
        if update.get("callback_query"):
            callback = update["callback_query"]
            chat_id, _, _ = _chat_data(callback.get("message") or {})
            _handle_callback(db, callback, request_id)
        elif update.get("message"):
            chat_id, _, _ = _chat_data(update["message"])
            _handle_message(db, update, update["message"], request_id)
        if not db.query(AIAuditLog).filter(AIAuditLog.request_id == request_id).first():
            db.add(
                AIAuditLog(
                    channel="telegram",
                    action="telegram_update",
                    status="completed",
                    request_id=request_id,
                    input_payload={"update_id": update.get("update_id")},
                    output_payload={"handled": True},
                )
            )
            db.commit()
    except Exception:
        db.rollback()
        logger.exception("No se pudo procesar actualización de Telegram %s", request_id)
        try:
            if not db.query(AIAuditLog).filter(AIAuditLog.request_id == request_id).first():
                db.add(
                    AIAuditLog(
                        channel="telegram",
                        action="telegram_update",
                        status="completed",
                        request_id=request_id,
                        input_payload={"update_id": update.get("update_id")},
                        output_payload={"handled": False},
                        error="La actualización finalizó con un error controlado; no se reintentará automáticamente.",
                    )
                )
                db.commit()
        except Exception:
            db.rollback()
            logger.exception("No se pudo registrar el cierre de la actualización %s", request_id)
        if chat_id:
            try:
                send_message(chat_id, "No pude completar la solicitud. Los datos de NAVIA no fueron modificados. Inténtelo nuevamente.")
            except TelegramError:
                logger.exception("Tampoco se pudo notificar el error por Telegram")
    finally:
        db.close()
