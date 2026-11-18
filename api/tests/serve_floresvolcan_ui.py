"""Local-only real API and in-memory records for the Flores browser regression.

Run with the backend's Python environment (including pytest). No persisted data,
external services, or real credentials are used. The identity override applies
only to this test app; it must never be mounted in the application server.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# Set before importing database/config; never inherit a real connection string.
os.environ["DATABASE_URL"] = "sqlite://"
os.environ["APP_ENV"] = "local"
os.environ["NAVIA_UPLOAD_DIR"] = "/tmp/floresvolcan-ui-uploads"
os.environ.pop("FV_TEST_DATABASE_URL", None)
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.middleware.cors import CORSMiddleware  # noqa: E402
import uvicorn  # noqa: E402
from core.models import AppModule  # noqa: E402
from core.routers import admin, auth  # noqa: E402
from modules.mod_floresvolcan.manifest import MODULE_MANIFEST  # noqa: E402
from test_floresvolcan_reconciliation_v07 import fv  # noqa: E402


def main() -> None:
    records = fv.__wrapped__()
    state = next(records)
    try:
        state.db.add(AppModule(
            key="mod_floresvolcan", name="Flores Volcán",
            version=MODULE_MANIFEST["version"], status="active",
            module_type="industry", manifest=MODULE_MANIFEST, built_in=True,
        ))
        state.db.commit()
        app = state.client.app
        app.include_router(auth.router)
        app.include_router(admin.router)
        app.add_middleware(
            CORSMiddleware, allow_origins=["http://127.0.0.1:3049"],
            allow_credentials=True, allow_methods=["*"], allow_headers=["*"],
        )
        planted = state.plant(1000)
        assert planted.status_code == 201, planted.text
        state.harvest_order(planted.json()["batch_id"])
        uvicorn.run(app, host="127.0.0.1", port=8059)
    finally:
        try:
            next(records)
        except StopIteration:
            pass


if __name__ == "__main__":
    main()
