from __future__ import annotations

import logging
import os
import signal
import time
from pathlib import Path

from config import APP_NAME, APP_VERSION, NOTIFICATION_POLL_SECONDS, NOTIFICATION_WORKER_ENABLED, validate_runtime_config
from core.module_manager import get_active_industry_module, sync_builtin_modules
from database import SessionLocal
from modules.registry import resolve_worker

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("navia-module-worker")
STOP_REQUESTED = False
POLL_SECONDS = max(1, int(os.getenv("MODULE_WORKER_POLL_SECONDS", str(NOTIFICATION_POLL_SECONDS))))
HEARTBEAT_PATH = Path(
    os.getenv(
        "MODULE_WORKER_HEARTBEAT_PATH",
        os.getenv("NOTIFICATION_HEARTBEAT_PATH", "/tmp/navia-module-worker-heartbeat"),
    )
)


def _request_stop(signum, _frame) -> None:
    global STOP_REQUESTED
    STOP_REQUESTED = True
    logger.info("Señal %s recibida; el worker finalizará después del ciclo actual", signum)


def _write_heartbeat() -> None:
    HEARTBEAT_PATH.write_text(str(int(time.time())), encoding="utf-8")


def main() -> int:
    validate_runtime_config()
    if not NOTIFICATION_WORKER_ENABLED:
        logger.warning("Worker modular deshabilitado por configuración")
        return 0

    signal.signal(signal.SIGTERM, _request_stop)
    signal.signal(signal.SIGINT, _request_stop)
    logger.info("%s module worker %s iniciado; intervalo=%ss", APP_NAME, APP_VERSION, POLL_SECONDS)

    while not STOP_REQUESTED:
        db = SessionLocal()
        try:
            sync_builtin_modules(db)
            active = get_active_industry_module(db)
            worker = resolve_worker(active.key) if active else None
            if worker:
                result = worker(db)
                logger.info("Ciclo módulo=%s resultado=%s", active.key, result)
            _write_heartbeat()
        except Exception:
            logger.exception("Error no controlado en el worker modular")
            try:
                db.rollback()
            except Exception:
                pass
        finally:
            db.close()

        for _ in range(POLL_SECONDS):
            if STOP_REQUESTED:
                break
            time.sleep(1)

    logger.info("Worker modular finalizado")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
