from __future__ import annotations

import os
import shutil
import sys
import tempfile
import uuid
from pathlib import Path

import pytest


TEST_DATABASE_PATH = Path(tempfile.gettempdir()) / f"navia-ai-tests-{uuid.uuid4().hex}.sqlite3"
API_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(API_ROOT))

os.environ.update(
    {
        "DATABASE_URL": f"sqlite+pysqlite:///{TEST_DATABASE_PATH}",
        "APP_ENV": "local",
        "AUTO_CREATE_SCHEMA": "true",
        "SEED_DEMO_DATA": "false",
        "TRUSTED_HOSTS": "testserver,localhost,127.0.0.1",
        "ADMIN_EMAIL": "ai-tests-admin@example.com",
        "ADMIN_PASSWORD": "AiTestsAdmin2026!",
        "ADMIN_NAME": "AI Tests Admin",
        "OPENAI_API_KEY": "",
        "AI_PRIVATE_UPLOAD_DIR": str(TEST_DATABASE_PATH.with_suffix(".uploads")),
        "RECEIPT_PRIVATE_UPLOAD_DIR": str(TEST_DATABASE_PATH.with_suffix(".receipt-uploads")),
        "NAVIA_UPLOAD_DIR": str(TEST_DATABASE_PATH.with_suffix(".public-uploads")),
    }
)


@pytest.fixture
def db_session():
    from database import Base, SessionLocal, engine

    Base.metadata.create_all(bind=engine)
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


def pytest_sessionfinish(session, exitstatus):
    del session, exitstatus
    TEST_DATABASE_PATH.unlink(missing_ok=True)
    shutil.rmtree(TEST_DATABASE_PATH.with_suffix(".uploads"), ignore_errors=True)
    shutil.rmtree(TEST_DATABASE_PATH.with_suffix(".receipt-uploads"), ignore_errors=True)
    shutil.rmtree(TEST_DATABASE_PATH.with_suffix(".public-uploads"), ignore_errors=True)
