from __future__ import annotations

import os

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

DB_HOST = os.getenv("POSTGRES_HOST", "postgres")
DB_PORT = os.getenv("POSTGRES_PORT", "5432")
DB_NAME = os.getenv("POSTGRES_DB", "navia")
DB_USER = os.getenv("POSTGRES_USER", "navia_user")
DB_PASS = os.getenv("POSTGRES_PASSWORD", "navia_pass")
DATABASE_URL = os.getenv(
    "DATABASE_URL",
    f"postgresql+psycopg2://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}",
)


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


engine_options: dict = {
    "pool_pre_ping": True,
    "future": True,
}

if DATABASE_URL.startswith("postgresql"):
    engine_options.update({
        "pool_size": max(1, _env_int("DB_POOL_SIZE", 5)),
        "max_overflow": max(0, _env_int("DB_MAX_OVERFLOW", 10)),
        "pool_recycle": max(60, _env_int("DB_POOL_RECYCLE_SECONDS", 1800)),
        "pool_timeout": max(5, _env_int("DB_POOL_TIMEOUT_SECONDS", 30)),
        "connect_args": {
            "connect_timeout": max(3, _env_int("DB_CONNECT_TIMEOUT_SECONDS", 10)),
            "options": f"-c statement_timeout={max(1000, _env_int('DB_STATEMENT_TIMEOUT_MS', 30000))}",
        },
    })
elif DATABASE_URL.startswith("sqlite"):
    engine_options["connect_args"] = {"check_same_thread": False}

engine = create_engine(DATABASE_URL, **engine_options)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, expire_on_commit=False, bind=engine, future=True)


class Base(DeclarativeBase):
    pass


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
