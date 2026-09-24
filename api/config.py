from __future__ import annotations

import os
from urllib.parse import urlparse


def _get_bool(name: str, default: str = "false") -> bool:
    return os.getenv(name, default).strip().lower() in {"1", "true", "yes", "y", "on"}


def _get_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _get_csv(name: str, default: str = "") -> list[str]:
    return [item.strip() for item in os.getenv(name, default).split(",") if item.strip()]


APP_ENV = os.getenv("APP_ENV", "local").strip().lower()
IS_PRODUCTION = APP_ENV in {"prod", "production"}
APP_NAME = os.getenv("APP_NAME", "NAVIA").strip() or "NAVIA"
APP_HOST = os.getenv("APP_HOST", "0.0.0.0")
APP_PORT = _get_int("APP_PORT", 8000)
APP_BASE_URL = os.getenv("APP_BASE_URL", f"http://localhost:{APP_PORT}").rstrip("/")
FRONTEND_URL = os.getenv("FRONTEND_URL", "http://localhost:3000").rstrip("/")
APP_TIMEZONE = os.getenv("APP_TIMEZONE", "America/Costa_Rica")
APP_VERSION = os.getenv("APP_VERSION", "1.4.0")
AUTO_CREATE_SCHEMA = _get_bool("AUTO_CREATE_SCHEMA", "false" if IS_PRODUCTION else "true")
SESSION_TOUCH_INTERVAL_SECONDS = max(60, _get_int("SESSION_TOUCH_INTERVAL_SECONDS", 300))
ENFORCE_ORIGIN_CHECK = _get_bool("ENFORCE_ORIGIN_CHECK", "true" if IS_PRODUCTION else "false")

CORS_ORIGINS = _get_csv(
    "CORS_ORIGINS",
    "http://localhost:3000,http://localhost:3001,http://127.0.0.1:3000",
)
TRUSTED_HOSTS = _get_csv("TRUSTED_HOSTS", "localhost,127.0.0.1")
ENABLE_DOCS = _get_bool("ENABLE_DOCS", "false" if IS_PRODUCTION else "true")
SECURE_HEADERS = _get_bool("SECURE_HEADERS", "true")
MAX_REQUEST_MB = max(1, _get_int("MAX_REQUEST_MB", 25))

COOKIE_NAME = os.getenv("COOKIE_NAME", "navia_token")
COOKIE_DOMAIN = os.getenv("COOKIE_DOMAIN", "").strip() or None
COOKIE_PATH = os.getenv("COOKIE_PATH", "/")
COOKIE_SAMESITE = os.getenv("COOKIE_SAMESITE", "lax").strip().lower()
COOKIE_SECURE = _get_bool("COOKIE_SECURE", "true" if IS_PRODUCTION else "false")
COOKIE_HTTPONLY = _get_bool("COOKIE_HTTPONLY", "true")
DEFAULT_SESSION_DAYS = max(1, _get_int("DEFAULT_SESSION_DAYS", 30))
TRAINING_CURRENT_VERSION = max(1, min(100, _get_int("TRAINING_CURRENT_VERSION", 2)))
EXPOSE_AUTH_TOKEN = _get_bool("EXPOSE_AUTH_TOKEN", "false" if IS_PRODUCTION else "true")

ADMIN_EMAIL = os.getenv("ADMIN_EMAIL", "admin@navia.com").strip().lower()
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "AdminNavia2026!")
ADMIN_NAME = os.getenv("ADMIN_NAME", "Administrador NAVIA").strip() or "Administrador NAVIA"
ADMIN_PASSWORD_SYNC_ON_STARTUP = _get_bool("ADMIN_PASSWORD_SYNC_ON_STARTUP", "false")
SEED_DEMO_DATA = _get_bool("SEED_DEMO_DATA", "false" if IS_PRODUCTION else "true")

PASSWORD_RESET_ENABLED = _get_bool("PASSWORD_RESET_ENABLED", "true")
PASSWORD_RESET_TOKEN_MINUTES = max(5, _get_int("PASSWORD_RESET_TOKEN_MINUTES", 30))
PASSWORD_RESET_URL = os.getenv(
    "PASSWORD_RESET_URL",
    f"{FRONTEND_URL}/reset-password",
).strip()

SMTP_HOST = os.getenv("SMTP_HOST", "").strip()
SMTP_PORT = _get_int("SMTP_PORT", 587)
SMTP_USERNAME = os.getenv("SMTP_USERNAME", "").strip()
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
SMTP_FROM_EMAIL = os.getenv("SMTP_FROM_EMAIL", SMTP_USERNAME).strip()
SMTP_FROM_NAME = os.getenv("SMTP_FROM_NAME", APP_NAME).strip() or APP_NAME
SMTP_USE_TLS = _get_bool("SMTP_USE_TLS", "true")
SMTP_USE_SSL = _get_bool("SMTP_USE_SSL", "false")
SMTP_TIMEOUT_SECONDS = max(5, _get_int("SMTP_TIMEOUT_SECONDS", 20))

LOGIN_RATE_LIMIT = max(1, _get_int("LOGIN_RATE_LIMIT", 10))
PASSWORD_RESET_RATE_LIMIT = max(1, _get_int("PASSWORD_RESET_RATE_LIMIT", 5))
AUTH_RATE_WINDOW_SECONDS = max(60, _get_int("AUTH_RATE_WINDOW_SECONDS", 900))

NOTIFICATION_WORKER_ENABLED = _get_bool("NOTIFICATION_WORKER_ENABLED", "false")
NOTIFICATION_POLL_SECONDS = max(15, _get_int("NOTIFICATION_POLL_SECONDS", 60))
NOTIFICATION_BATCH_SIZE = max(1, min(100, _get_int("NOTIFICATION_BATCH_SIZE", 25)))
NOTIFICATION_MAX_DELIVERY_ATTEMPTS = max(1, min(10, _get_int("NOTIFICATION_MAX_DELIVERY_ATTEMPTS", 4)))
NOTIFICATION_RETRY_BASE_SECONDS = max(30, _get_int("NOTIFICATION_RETRY_BASE_SECONDS", 300))
NOTIFICATION_DEFAULT_MIN_SEVERITY = os.getenv("NOTIFICATION_DEFAULT_MIN_SEVERITY", "warning").strip().lower()
NOTIFICATION_STALE_OT_HOURS = max(1, _get_int("NOTIFICATION_STALE_OT_HOURS", 48))
NOTIFICATION_PENDING_APPROVAL_HOURS = max(1, _get_int("NOTIFICATION_PENDING_APPROVAL_HOURS", 12))
NOTIFICATION_PENDING_SALE_DAYS = max(1, _get_int("NOTIFICATION_PENDING_SALE_DAYS", 3))
NOTIFICATION_UNASSIGNED_RECEIPT_HOURS = max(1, _get_int("NOTIFICATION_UNASSIGNED_RECEIPT_HOURS", 24))
NOTIFICATION_ALERT_COOLDOWN_HOURS = max(1, _get_int("NOTIFICATION_ALERT_COOLDOWN_HOURS", 12))
NOTIFICATION_WARNING_COOLDOWN_HOURS = max(1, _get_int("NOTIFICATION_WARNING_COOLDOWN_HOURS", 24))

AI_NOTIFICATIONS_ENABLED = _get_bool("AI_NOTIFICATIONS_ENABLED", "false")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4.1-mini").strip() or "gpt-4.1-mini"
OPENAI_TIMEOUT_SECONDS = max(5, _get_int("OPENAI_TIMEOUT_SECONDS", 30))
OPENAI_MAX_RETRIES = max(0, min(5, _get_int("OPENAI_MAX_RETRIES", 1)))
AI_NOTIFICATION_MIN_SEVERITY = os.getenv("AI_NOTIFICATION_MIN_SEVERITY", "warning").strip().lower()
AI_NOTIFICATION_MAX_CANDIDATES = max(1, min(100, _get_int("AI_NOTIFICATION_MAX_CANDIDATES", 20)))

# Asistente conversacional y automatizaciones. Las credenciales permanecen en
# variables de entorno; la base de datos solo conserva agentes y modelos permitidos.
AI_CONSULTING_ENABLED = _get_bool("AI_CONSULTING_ENABLED", "true")
AI_ALLOWED_MODELS = _get_csv(
    "AI_ALLOWED_MODELS",
    "gpt-4.1,gpt-4.1-mini",
)
AI_DEFAULT_MODEL = os.getenv("AI_DEFAULT_MODEL", OPENAI_MODEL).strip() or OPENAI_MODEL
AI_TRANSCRIPTION_MODEL = os.getenv("AI_TRANSCRIPTION_MODEL", "gpt-transcribe").strip()
AI_MAX_OUTPUT_TOKENS = max(300, min(8000, _get_int("AI_MAX_OUTPUT_TOKENS", 1800)))
AI_MAX_HISTORY_MESSAGES = max(4, min(80, _get_int("AI_MAX_HISTORY_MESSAGES", 30)))
AI_MAX_TOOL_ROUNDS = max(1, min(8, _get_int("AI_MAX_TOOL_ROUNDS", 5)))
AI_CHAT_RATE_LIMIT_PER_MINUTE = max(1, min(120, _get_int("AI_CHAT_RATE_LIMIT_PER_MINUTE", 20)))
AI_MAX_UPLOAD_MB = max(1, min(MAX_REQUEST_MB, _get_int("AI_MAX_UPLOAD_MB", 20)))
AI_ACTION_EXPIRY_MINUTES = max(5, min(1440, _get_int("AI_ACTION_EXPIRY_MINUTES", 30)))
AI_PRIVATE_UPLOAD_DIR = os.getenv("AI_PRIVATE_UPLOAD_DIR", "/app/private_uploads").strip()
AI_KNOWLEDGE_MAX_DOCUMENTS = max(1, min(500, _get_int("AI_KNOWLEDGE_MAX_DOCUMENTS", 100)))
AI_KNOWLEDGE_MAX_CHARS = max(20_000, min(5_000_000, _get_int("AI_KNOWLEDGE_MAX_CHARS", 750_000)))

# IoT Caficultura. Las aplicaciones TTN se registran dinámicamente en base de
# datos y cada integración administra su propio secreto. Esta variable global
# queda únicamente como compatibilidad de migración para instalaciones <=1.3.x.
IOT_TTN_WEBHOOK_SECRET = os.getenv("IOT_TTN_WEBHOOK_SECRET", "").strip()
IOT_INGEST_RATE_LIMIT_PER_MINUTE = max(10, min(10_000, _get_int("IOT_INGEST_RATE_LIMIT_PER_MINUTE", 600)))
IOT_MAX_INGEST_BYTES = max(4_096, min(1_048_576, _get_int("IOT_MAX_INGEST_BYTES", 131_072)))
IOT_MAX_VARIABLES = max(1, min(128, _get_int("IOT_MAX_VARIABLES", 32)))
IOT_MAX_QUERY_ROWS = max(500, min(100_000, _get_int("IOT_MAX_QUERY_ROWS", 20_000)))
IOT_MAX_EXPORT_ROWS = max(IOT_MAX_QUERY_ROWS, min(1_000_000, _get_int("IOT_MAX_EXPORT_ROWS", 250_000)))

# Portal de recibos para clientes. Los comprobantes bancarios permanecen fuera
# del directorio público y las sesiones son breves y revocables.
RECEIPT_PRIVATE_UPLOAD_DIR = os.getenv(
    "RECEIPT_PRIVATE_UPLOAD_DIR",
    os.path.join(AI_PRIVATE_UPLOAD_DIR, "receipt_payments"),
).strip()
RECEIPT_PROOF_MAX_MB = max(1, min(MAX_REQUEST_MB, _get_int("RECEIPT_PROOF_MAX_MB", 10)))
RECEIPT_PORTAL_SESSION_MINUTES = max(15, min(1440, _get_int("RECEIPT_PORTAL_SESSION_MINUTES", 120)))
RECEIPT_PORTAL_VERIFY_LIMIT = max(3, min(30, _get_int("RECEIPT_PORTAL_VERIFY_LIMIT", 8)))
RECEIPT_PORTAL_RATE_WINDOW_SECONDS = max(60, _get_int("RECEIPT_PORTAL_RATE_WINDOW_SECONDS", 900))
RECEIPT_OFFICIAL_LOGO_URL = os.getenv(
    "RECEIPT_OFFICIAL_LOGO_URL",
    f"{FRONTEND_URL}/images/logos/cafe-navarro-recibo.png",
).strip()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_WEBHOOK_SECRET = os.getenv("TELEGRAM_WEBHOOK_SECRET", "").strip()
TELEGRAM_WEBHOOK_URL = os.getenv("TELEGRAM_WEBHOOK_URL", "").strip()
TELEGRAM_API_BASE = os.getenv("TELEGRAM_API_BASE", "https://api.telegram.org").rstrip("/")
TELEGRAM_TIMEOUT_SECONDS = max(5, min(60, _get_int("TELEGRAM_TIMEOUT_SECONDS", 20)))
TELEGRAM_PAIRING_MINUTES = max(5, min(60, _get_int("TELEGRAM_PAIRING_MINUTES", 10)))


def smtp_is_configured() -> bool:
    return bool(SMTP_HOST and SMTP_PORT and SMTP_FROM_EMAIL)


def openai_is_configured() -> bool:
    return bool(OPENAI_API_KEY and OPENAI_MODEL)


def telegram_is_configured() -> bool:
    return bool(TELEGRAM_BOT_TOKEN)


def validate_runtime_config() -> None:
    errors: list[str] = []

    if COOKIE_SAMESITE not in {"lax", "strict", "none"}:
        errors.append("COOKIE_SAMESITE debe ser lax, strict o none")

    if SMTP_USE_SSL and SMTP_USE_TLS:
        errors.append("SMTP_USE_SSL y SMTP_USE_TLS no pueden estar activos al mismo tiempo")

    if PASSWORD_RESET_ENABLED and not PASSWORD_RESET_URL:
        errors.append("PASSWORD_RESET_URL es obligatorio cuando PASSWORD_RESET_ENABLED=true")

    try:
        parsed_reset_url = urlparse(PASSWORD_RESET_URL)
        if PASSWORD_RESET_ENABLED and parsed_reset_url.scheme not in {"http", "https"}:
            errors.append("PASSWORD_RESET_URL debe ser una URL http o https válida")
    except Exception:
        errors.append("PASSWORD_RESET_URL no es válida")

    valid_severities = {"info", "warning", "high", "critical"}
    if NOTIFICATION_DEFAULT_MIN_SEVERITY not in valid_severities:
        errors.append("NOTIFICATION_DEFAULT_MIN_SEVERITY debe ser info, warning, high o critical")
    if AI_NOTIFICATION_MIN_SEVERITY not in valid_severities:
        errors.append("AI_NOTIFICATION_MIN_SEVERITY debe ser info, warning, high o critical")
    if AI_NOTIFICATIONS_ENABLED and not openai_is_configured():
        errors.append("OPENAI_API_KEY y OPENAI_MODEL son obligatorios cuando AI_NOTIFICATIONS_ENABLED=true")
    if AI_DEFAULT_MODEL not in AI_ALLOWED_MODELS:
        errors.append("AI_DEFAULT_MODEL debe estar incluido en AI_ALLOWED_MODELS")
    if TELEGRAM_WEBHOOK_URL and not TELEGRAM_BOT_TOKEN:
        errors.append("TELEGRAM_BOT_TOKEN es obligatorio cuando TELEGRAM_WEBHOOK_URL está configurado")
    if TELEGRAM_WEBHOOK_URL and len(TELEGRAM_WEBHOOK_SECRET) < 24:
        errors.append("TELEGRAM_WEBHOOK_SECRET debe tener al menos 24 caracteres")
    if IOT_TTN_WEBHOOK_SECRET and len(IOT_TTN_WEBHOOK_SECRET) < 24:
        errors.append("IOT_TTN_WEBHOOK_SECRET debe tener al menos 24 caracteres")
    if not RECEIPT_PRIVATE_UPLOAD_DIR:
        errors.append("RECEIPT_PRIVATE_UPLOAD_DIR es obligatorio")
    if NOTIFICATION_WORKER_ENABLED and not (smtp_is_configured() or telegram_is_configured()):
        errors.append("Configure SMTP o Telegram cuando NOTIFICATION_WORKER_ENABLED=true")

    if IS_PRODUCTION:
        if not CORS_ORIGINS or "*" in CORS_ORIGINS:
            errors.append("CORS_ORIGINS debe listar dominios explícitos en producción")
        if any(not origin.startswith("https://") for origin in CORS_ORIGINS):
            errors.append("Todos los CORS_ORIGINS deben usar https en producción")
        if not TRUSTED_HOSTS or "*" in TRUSTED_HOSTS:
            errors.append("TRUSTED_HOSTS debe listar hosts explícitos en producción")
        if not APP_BASE_URL.startswith("https://") or not FRONTEND_URL.startswith("https://"):
            errors.append("APP_BASE_URL y FRONTEND_URL deben usar https en producción")
        if PASSWORD_RESET_ENABLED and not PASSWORD_RESET_URL.startswith("https://"):
            errors.append("PASSWORD_RESET_URL debe usar https en producción")
        if not COOKIE_SECURE:
            errors.append("COOKIE_SECURE debe ser true en producción")
        if COOKIE_SAMESITE == "none":
            errors.append("COOKIE_SAMESITE=none no está permitido sin protección CSRF; use frontend y API en el mismo sitio")

        admin_password_weak = (
            ADMIN_PASSWORD == "AdminNavia2026!"
            or "CAMBIE_" in ADMIN_PASSWORD.upper()
            or len(ADMIN_PASSWORD) < 12
            or not any(ch.islower() for ch in ADMIN_PASSWORD)
            or not any(ch.isupper() for ch in ADMIN_PASSWORD)
            or not any(ch.isdigit() for ch in ADMIN_PASSWORD)
            or not any(not ch.isalnum() for ch in ADMIN_PASSWORD)
        )
        if admin_password_weak:
            errors.append("ADMIN_PASSWORD debe ser una clave real, fuerte y de al menos 12 caracteres")
        if ADMIN_EMAIL == "admin@navia.com":
            errors.append("ADMIN_EMAIL debe cambiarse en producción")

        postgres_password = os.getenv("POSTGRES_PASSWORD", "")
        if not postgres_password or postgres_password in {"navia_pass", "navia_pass_segura"} or "CAMBIE_" in postgres_password.upper():
            errors.append("POSTGRES_PASSWORD debe configurarse con una clave real")

        if PASSWORD_RESET_ENABLED and not smtp_is_configured():
            errors.append("SMTP debe configurarse si recuperación de contraseña está habilitada en producción")
        if AI_CONSULTING_ENABLED and not openai_is_configured():
            errors.append("OPENAI_API_KEY y OPENAI_MODEL son obligatorios cuando AI_CONSULTING_ENABLED=true")
        if OPENAI_API_KEY and "CAMBIE_" in OPENAI_API_KEY.upper():
            errors.append("OPENAI_API_KEY todavía contiene el valor de ejemplo")
        if TELEGRAM_BOT_TOKEN and "CAMBIE_" in TELEGRAM_BOT_TOKEN.upper():
            errors.append("TELEGRAM_BOT_TOKEN todavía contiene el valor de ejemplo")
        if TELEGRAM_WEBHOOK_SECRET and "CAMBIE_" in TELEGRAM_WEBHOOK_SECRET.upper():
            errors.append("TELEGRAM_WEBHOOK_SECRET todavía contiene el valor de ejemplo")
        if IOT_TTN_WEBHOOK_SECRET and "CAMBIE_" in IOT_TTN_WEBHOOK_SECRET.upper():
            errors.append("IOT_TTN_WEBHOOK_SECRET todavía contiene el valor de ejemplo")

    if errors:
        raise RuntimeError("Configuración inválida: " + "; ".join(errors))
