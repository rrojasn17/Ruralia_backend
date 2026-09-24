from __future__ import annotations

import logging
import os
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote

from fastapi import Depends, FastAPI, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from starlette.middleware.trustedhost import TrustedHostMiddleware

from config import (
    ADMIN_EMAIL,
    ADMIN_NAME,
    ADMIN_PASSWORD,
    ADMIN_PASSWORD_SYNC_ON_STARTUP,
    APP_NAME,
    APP_VERSION,
    AUTO_CREATE_SCHEMA,
    CORS_ORIGINS,
    DEFAULT_SESSION_DAYS,
    ENABLE_DOCS,
    ENFORCE_ORIGIN_CHECK,
    IS_PRODUCTION,
    MAX_REQUEST_MB,
    SECURE_HEADERS,
    TRUSTED_HOSTS,
    validate_runtime_config,
)
from core.module_manager import get_active_industry_module, require_active_module, sync_builtin_modules
from core.routers.admin import router as core_admin_router
from core.routers.auth import router as auth_router
from core.services import bootstrap_core
from database import Base, SessionLocal, engine

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("navia-api")

validate_runtime_config()

UPLOAD_DIR = Path(os.getenv("NAVIA_UPLOAD_DIR", "uploads")).resolve()
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
_runtime_registered = False


class SafeUploadStaticFiles(StaticFiles):
    async def get_response(self, path: str, scope):
        response = await super().get_response(path, scope)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Content-Security-Policy", "default-src 'none'; sandbox")
        extension = Path(path).suffix.lower()
        if extension not in {".png", ".jpg", ".jpeg", ".webp"}:
            filename = quote(Path(path).name or "archivo")
            response.headers.setdefault("Content-Disposition", f"attachment; filename*=UTF-8''{filename}")
        return response


def register_module_runtimes(app: FastAPI) -> None:
    """Registra routers de runtimes revisados sin activar su industria.

    El core no conoce routers concretos. ``modules.registry`` es el único punto
    de integración de código desplegado; cada router queda protegido por el
    estado persistente del módulo correspondiente.
    """
    global _runtime_registered
    if _runtime_registered:
        return

    from modules.registry import iter_runtime_routers

    for module_key, router in iter_runtime_routers():
        app.include_router(router, dependencies=[Depends(require_active_module(module_key))])

    _runtime_registered = True
    app.openapi_schema = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    # En una instancia nueva sólo se crean tablas core en este punto. Los modelos
    # industriales se registran después y su esquema se crea al activar el módulo.
    if AUTO_CREATE_SCHEMA:
        Base.metadata.create_all(bind=engine)

    db = SessionLocal()
    try:
        bootstrap_core(
            db,
            ADMIN_EMAIL,
            ADMIN_PASSWORD,
            ADMIN_NAME,
            DEFAULT_SESSION_DAYS,
            sync_admin_password=ADMIN_PASSWORD_SYNC_ON_STARTUP,
        )
        sync_builtin_modules(db)

        # Carga los runtimes después de inicializar el core para conservar el estado
        # base limpio. Importarlos registra sus modelos, pero no instala sus tablas.
        register_module_runtimes(app)

        active = get_active_industry_module(db)
        if active:
            from core.module_manager import _call_lifecycle
            _call_lifecycle(active.key, "activate", db)

        logger.info(
            "%s API %s inicializada; modulo_industria=%s",
            APP_NAME,
            APP_VERSION,
            active.key if active else "ninguno",
        )
    finally:
        db.close()
    yield


app = FastAPI(
    title=f"{APP_NAME} API",
    version=APP_VERSION,
    description="Núcleo modular multipropósito con autenticación, RBAC, CMS y módulos de industria instalables.",
    docs_url="/docs" if ENABLE_DOCS else None,
    redoc_url="/redoc" if ENABLE_DOCS else None,
    openapi_url="/openapi.json" if ENABLE_DOCS else None,
    lifespan=lifespan,
)

if TRUSTED_HOSTS:
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=TRUSTED_HOSTS)

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Accept", "Authorization", "Cache-Control", "Content-Type", "X-API-Token", "X-IoT-Token", "X-Portal-Token", "X-Request-ID", "X-TTN-Webhook-Secret"],
    expose_headers=["X-Request-ID"],
    max_age=600,
)


@app.middleware("http")
async def production_guards(request: Request, call_next):
    request_id = request.headers.get("x-request-id") or str(uuid.uuid4())
    if ENFORCE_ORIGIN_CHECK and request.method in {"POST", "PUT", "PATCH", "DELETE"}:
        origin = request.headers.get("origin")
        if origin and origin.rstrip("/") not in {item.rstrip("/") for item in CORS_ORIGINS}:
            return JSONResponse(
                status_code=status.HTTP_403_FORBIDDEN,
                content={"detail": "Origen no autorizado"},
                headers={"X-Request-ID": request_id},
            )

    content_length = request.headers.get("content-length")
    if content_length:
        try:
            if int(content_length) > MAX_REQUEST_MB * 1024 * 1024:
                return JSONResponse(
                    status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                    content={"detail": f"La solicitud supera el máximo de {MAX_REQUEST_MB} MB"},
                    headers={"X-Request-ID": request_id},
                )
        except ValueError:
            return JSONResponse(
                status_code=status.HTTP_400_BAD_REQUEST,
                content={"detail": "Content-Length inválido"},
                headers={"X-Request-ID": request_id},
            )

    response = await call_next(request)
    response.headers["X-Request-ID"] = request_id

    if SECURE_HEADERS:
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        if IS_PRODUCTION:
            response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")

    if request.url.path.startswith(("/auth/", "/public/")):
        response.headers.setdefault("Cache-Control", "no-store")

    return response


app.mount("/uploads", SafeUploadStaticFiles(directory=str(UPLOAD_DIR)), name="uploads")
app.include_router(auth_router)
app.include_router(core_admin_router)


@app.get("/health", tags=["system"])
def health():
    return {"ok": True, "service": "modular-core-api", "version": APP_VERSION}


@app.get("/ready", tags=["system"])
def readiness():
    db = SessionLocal()
    try:
        db.execute(text("SELECT 1"))
        active = get_active_industry_module(db)
        return {"ok": True, "database": "ready", "version": APP_VERSION, "active_module": active.key if active else None}
    except Exception:
        logger.exception("Falló la verificación de disponibilidad de base de datos")
        return JSONResponse(status_code=503, content={"ok": False, "database": "unavailable"})
    finally:
        db.close()
