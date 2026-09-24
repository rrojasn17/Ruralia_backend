from __future__ import annotations

import logging
from typing import Any

import httpx

from config import (
    TELEGRAM_API_BASE,
    TELEGRAM_BOT_TOKEN,
    TELEGRAM_TIMEOUT_SECONDS,
    TELEGRAM_WEBHOOK_SECRET,
    TELEGRAM_WEBHOOK_URL,
    telegram_is_configured,
)


logger = logging.getLogger("navia-api.telegram")


class TelegramError(RuntimeError):
    pass


def _endpoint(method: str) -> str:
    if not telegram_is_configured():
        raise TelegramError("Telegram no está configurado")
    return f"{TELEGRAM_API_BASE}/bot{TELEGRAM_BOT_TOKEN}/{method}"


def _call(method: str, payload: dict[str, Any] | None = None) -> Any:
    try:
        with httpx.Client(timeout=TELEGRAM_TIMEOUT_SECONDS) as client:
            response = client.post(_endpoint(method), json=payload or {})
            response.raise_for_status()
            data = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        logger.exception("Falló Telegram %s", method)
        raise TelegramError(f"No fue posible comunicarse con Telegram ({method})") from exc
    if not data.get("ok"):
        description = str(data.get("description") or "Error no especificado")[:500]
        raise TelegramError(description)
    return data.get("result")


def bot_info() -> dict[str, Any]:
    result = _call("getMe")
    return result if isinstance(result, dict) else {}


def webhook_info() -> dict[str, Any]:
    result = _call("getWebhookInfo")
    return result if isinstance(result, dict) else {}


def configure_webhook() -> dict[str, Any]:
    if not TELEGRAM_WEBHOOK_URL:
        raise TelegramError("TELEGRAM_WEBHOOK_URL no está configurada")
    if len(TELEGRAM_WEBHOOK_SECRET) < 24:
        raise TelegramError("TELEGRAM_WEBHOOK_SECRET no cumple el mínimo de seguridad")
    _call(
        "setWebhook",
        {
            "url": TELEGRAM_WEBHOOK_URL,
            "secret_token": TELEGRAM_WEBHOOK_SECRET,
            "allowed_updates": ["message", "callback_query"],
            "drop_pending_updates": False,
        },
    )
    return webhook_info()


def send_message(chat_id: str, text: str, *, reply_markup: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    clean = str(text or "").strip() or "NAVIA no generó contenido para este mensaje."
    chunks = [clean[index : index + 3900] for index in range(0, len(clean), 3900)]
    results: list[dict[str, Any]] = []
    for index, chunk in enumerate(chunks):
        payload: dict[str, Any] = {
            "chat_id": str(chat_id),
            "text": chunk,
            "disable_web_page_preview": False,
        }
        if reply_markup and index == len(chunks) - 1:
            payload["reply_markup"] = reply_markup
        result = _call("sendMessage", payload)
        if isinstance(result, dict):
            results.append(result)
    return results


def answer_callback_query(callback_query_id: str, text: str | None = None) -> None:
    payload: dict[str, Any] = {"callback_query_id": callback_query_id}
    if text:
        payload["text"] = text[:180]
    _call("answerCallbackQuery", payload)


def download_file(file_id: str) -> tuple[bytes, str]:
    result = _call("getFile", {"file_id": file_id})
    if not isinstance(result, dict) or not result.get("file_path"):
        raise TelegramError("Telegram no devolvió la ruta del archivo")
    file_path = str(result["file_path"])
    url = f"{TELEGRAM_API_BASE}/file/bot{TELEGRAM_BOT_TOKEN}/{file_path}"
    try:
        with httpx.Client(timeout=TELEGRAM_TIMEOUT_SECONDS) as client:
            response = client.get(url)
            response.raise_for_status()
            return response.content, file_path
    except httpx.HTTPError as exc:
        raise TelegramError("No fue posible descargar el archivo de Telegram") from exc
