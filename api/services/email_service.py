from __future__ import annotations

import logging
import smtplib
from email.message import EmailMessage
from email.utils import formataddr
from html import escape

from config import (
    APP_NAME,
    FRONTEND_URL,
    PASSWORD_RESET_TOKEN_MINUTES,
    SMTP_FROM_EMAIL,
    SMTP_FROM_NAME,
    SMTP_HOST,
    SMTP_PASSWORD,
    SMTP_PORT,
    SMTP_TIMEOUT_SECONDS,
    SMTP_USERNAME,
    SMTP_USE_SSL,
    SMTP_USE_TLS,
)

logger = logging.getLogger("navia-api.email")


def _smtp_client():
    if SMTP_USE_SSL:
        return smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=SMTP_TIMEOUT_SECONDS)
    return smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=SMTP_TIMEOUT_SECONDS)


def send_email_message(message: EmailMessage) -> None:
    try:
        with _smtp_client() as client:
            if not SMTP_USE_SSL and SMTP_USE_TLS:
                client.ehlo()
                client.starttls()
                client.ehlo()
            if SMTP_USERNAME:
                client.login(SMTP_USERNAME, SMTP_PASSWORD)
            client.send_message(message)
    except Exception:
        logger.exception("No se pudo enviar correo a %s", message.get("To", "destinatario desconocido"))
        raise


def send_password_reset_email(recipient: str, recipient_name: str, reset_url: str) -> None:
    message = EmailMessage()
    message["Subject"] = f"Restablecer contraseña de {APP_NAME}"
    message["From"] = formataddr((SMTP_FROM_NAME, SMTP_FROM_EMAIL))
    message["To"] = recipient

    safe_name = recipient_name.strip() or "usuario"
    html_name = escape(safe_name)
    html_reset_url = escape(reset_url, quote=True)
    message.set_content(
        f"Hola {safe_name},\n\n"
        f"Recibimos una solicitud para restablecer tu contraseña de {APP_NAME}.\n\n"
        f"Abre este enlace para crear una nueva contraseña:\n{reset_url}\n\n"
        f"El enlace vence en {PASSWORD_RESET_TOKEN_MINUTES} minutos y solo puede utilizarse una vez.\n"
        "Si no solicitaste este cambio, puedes ignorar este mensaje.\n"
    )
    message.add_alternative(
        f"""
        <!doctype html>
        <html lang="es">
          <body style="font-family:Arial,sans-serif;background:#f8fafc;color:#0f172a;padding:24px">
            <div style="max-width:560px;margin:auto;background:#ffffff;border:1px solid #e2e8f0;border-radius:16px;padding:28px">
              <h2 style="margin-top:0">Restablecer contraseña</h2>
              <p>Hola {html_name},</p>
              <p>Recibimos una solicitud para restablecer tu contraseña de <strong>{escape(APP_NAME)}</strong>.</p>
              <p style="margin:28px 0">
                <a href="{html_reset_url}" style="background:#0f172a;color:#ffffff;text-decoration:none;padding:12px 18px;border-radius:10px;display:inline-block">
                  Crear nueva contraseña
                </a>
              </p>
              <p>El enlace vence en {PASSWORD_RESET_TOKEN_MINUTES} minutos y solo puede utilizarse una vez.</p>
              <p style="color:#64748b;font-size:13px">Si no solicitaste este cambio, ignora este mensaje.</p>
            </div>
          </body>
        </html>
        """,
        subtype="html",
    )
    send_email_message(message)


def send_notification_email(
    recipient: str,
    recipient_name: str | None,
    subject: str,
    summary: str,
    recommended_action: str | None,
    severity: str,
) -> None:
    safe_name = (recipient_name or "usuario").strip() or "usuario"
    severity_labels = {
        "info": "Información",
        "warning": "Atención",
        "high": "Prioridad alta",
        "critical": "Crítica",
    }
    severity_label = severity_labels.get(severity, "Atención")
    safe_subject = subject.strip()[:160] or f"Aviso de {APP_NAME}"
    safe_summary = summary.strip()[:4000]
    safe_action = (recommended_action or "Ingrese a NAVIA para revisar el evento.").strip()[:2000]

    message = EmailMessage()
    message["Subject"] = safe_subject
    message["From"] = formataddr((SMTP_FROM_NAME, SMTP_FROM_EMAIL))
    message["To"] = recipient
    message["X-NAVIA-Severity"] = severity
    message.set_content(
        f"Hola {safe_name},\n\n"
        f"Nivel: {severity_label}\n\n"
        f"{safe_summary}\n\n"
        f"Acción recomendada:\n{safe_action}\n\n"
        f"Abrir NAVIA: {FRONTEND_URL}\n\n"
        "Este correo fue generado por el motor de notificaciones de NAVIA a partir de datos operativos registrados en la plataforma.\n"
    )

    message.add_alternative(
        f"""
        <!doctype html>
        <html lang="es">
          <body style="font-family:Arial,sans-serif;background:#f1f5f9;color:#0f172a;padding:24px">
            <div style="max-width:640px;margin:auto;background:#ffffff;border:1px solid #e2e8f0;border-radius:16px;overflow:hidden">
              <div style="padding:20px 28px;background:#0f172a;color:#ffffff">
                <div style="font-size:12px;letter-spacing:.08em;text-transform:uppercase;opacity:.8">{escape(APP_NAME)} · {escape(severity_label)}</div>
                <h2 style="margin:8px 0 0;font-size:22px">{escape(safe_subject)}</h2>
              </div>
              <div style="padding:28px">
                <p>Hola {escape(safe_name)},</p>
                <p style="line-height:1.6">{escape(safe_summary).replace(chr(10), '<br>')}</p>
                <div style="margin:24px 0;padding:18px;border-left:4px solid #0f172a;background:#f8fafc;border-radius:8px">
                  <strong>Acción recomendada</strong>
                  <p style="margin:8px 0 0;line-height:1.6">{escape(safe_action).replace(chr(10), '<br>')}</p>
                </div>
                <p style="margin:28px 0">
                  <a href="{escape(FRONTEND_URL, quote=True)}" style="display:inline-block;background:#166534;color:#ffffff;text-decoration:none;padding:12px 18px;border-radius:10px">
                    Abrir NAVIA
                  </a>
                </p>
                <p style="color:#64748b;font-size:12px;line-height:1.5">
                  Aviso automático basado en hechos registrados en NAVIA. La IA redacta y prioriza el mensaje, pero no modifica los datos de origen.
                </p>
              </div>
            </div>
          </body>
        </html>
        """,
        subtype="html",
    )
    send_email_message(message)
