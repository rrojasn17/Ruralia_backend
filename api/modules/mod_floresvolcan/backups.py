from __future__ import annotations

import json
import mimetypes
import os
import secrets
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from io import BytesIO
from pathlib import Path
from zoneinfo import ZoneInfo
from zipfile import ZIP_DEFLATED, ZipFile

from sqlalchemy.orm import Session

from modules.mod_floresvolcan import models
from modules.mod_floresvolcan.legacy_excel import build_lirio_workbook
from modules.mod_floresvolcan.models import BackupRun, ModuleSetting


def _setting(db: Session, key: str, default: str = "") -> str:
    row = db.query(ModuleSetting).filter(ModuleSetting.key == key).first()
    return (row.value_text if row and row.value_text is not None else default).strip()


def set_settings(db: Session, values: dict[str, str]) -> None:
    secret_keys = {"backup.drive_client_secret", "backup.drive_refresh_token"}
    for key, value in values.items():
        row = db.query(ModuleSetting).filter(ModuleSetting.key == key).first()
        if row is None:
            row = ModuleSetting(key=key)
            db.add(row)
        # Un valor secreto vacío en un PATCH no borra accidentalmente una credencial existente.
        if key in secret_keys and not str(value or "").strip() and row.value_text:
            continue
        row.value_text = str(value or "")
        row.is_secret = key in secret_keys
    db.commit()


def public_settings(db: Session) -> dict:
    keys = [
        "backup.enabled", "backup.frequency", "backup.hour", "backup.weekday", "backup.retention_days",
        "backup.destination", "backup.drive_folder_id", "backup.drive_client_id",
        "backup.drive_client_secret", "backup.drive_refresh_token",
    ]
    out = {key: _setting(db, key) for key in keys}
    out["backup.drive_client_secret_configured"] = bool(out.pop("backup.drive_client_secret", ""))
    out["backup.drive_refresh_token_configured"] = bool(out.pop("backup.drive_refresh_token", ""))
    return out


def _json_value(value):
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    return value


def _module_snapshot(db: Session) -> dict:
    payload: dict[str, list[dict]] = {}
    for name, obj in vars(models).items():
        table = getattr(obj, "__table__", None)
        if not isinstance(name, str) or table is None or not str(table.name).startswith("fv_"):
            continue
        rows = db.query(obj).all()
        payload[table.name] = [
            {column.name: _json_value(getattr(row, column.name)) for column in table.columns}
            for row in rows
        ]
    return payload


def _backup_root() -> Path:
    configured = os.getenv("AI_PRIVATE_UPLOAD_DIR", "").strip()
    root = Path(configured).expanduser() if configured else Path("/app/private_uploads" if Path("/app/private_uploads").exists() else "private_uploads")
    target = root / "floresvolcan_backups"
    target.mkdir(parents=True, exist_ok=True)
    return target


def build_backup_bytes(db: Session) -> bytes:
    excel = build_lirio_workbook(db)
    bio = BytesIO()
    with ZipFile(bio, "w", ZIP_DEFLATED) as zf:
        zf.writestr("floresvolcan_data.json", json.dumps(_module_snapshot(db), ensure_ascii=False, indent=2))
        zf.writestr("excel/Lirio.xlsx", excel)
        zf.writestr("excel/Liriodoc.xlsx", excel)
        zf.writestr("README.txt", "Respaldo FloresVolcan: datos normalizados + exportaciones Excel compatibles con el flujo Lirio.\n")
    return bio.getvalue()


def _refresh_drive_token(db: Session) -> str:
    client_id = _setting(db, "backup.drive_client_id")
    client_secret = _setting(db, "backup.drive_client_secret")
    refresh_token = _setting(db, "backup.drive_refresh_token")
    if not (client_id and client_secret and refresh_token):
        raise RuntimeError("Google Drive no está configurado: faltan client_id, client_secret o refresh_token")
    body = urllib.parse.urlencode({
        "client_id": client_id, "client_secret": client_secret, "refresh_token": refresh_token,
        "grant_type": "refresh_token",
    }).encode()
    req = urllib.request.Request("https://oauth2.googleapis.com/token", data=body, headers={"Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read().decode())
    token = data.get("access_token")
    if not token:
        raise RuntimeError("Google Drive no devolvió access_token")
    return token


def _upload_drive(db: Session, filename: str, content: bytes) -> str:
    token = _refresh_drive_token(db)
    folder_id = _setting(db, "backup.drive_folder_id")
    metadata: dict = {"name": filename}
    if folder_id:
        metadata["parents"] = [folder_id]
    boundary = "fv_" + secrets.token_hex(12)
    body = BytesIO()
    body.write(f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n".encode())
    body.write(json.dumps(metadata).encode())
    body.write(f"\r\n--{boundary}\r\nContent-Type: application/zip\r\n\r\n".encode())
    body.write(content)
    body.write(f"\r\n--{boundary}--\r\n".encode())
    req = urllib.request.Request(
        "https://www.googleapis.com/upload/drive/v3/files?uploadType=multipart&fields=id,name",
        data=body.getvalue(), method="POST",
        headers={"Authorization": f"Bearer {token}", "Content-Type": f"multipart/related; boundary={boundary}"},
    )
    with urllib.request.urlopen(req, timeout=90) as resp:
        data = json.loads(resp.read().decode())
    if not data.get("id"):
        raise RuntimeError("Google Drive no confirmó el archivo")
    return str(data["id"])


def _cleanup(db: Session) -> None:
    try:
        retention = max(1, int(_setting(db, "backup.retention_days", "30") or "30"))
    except ValueError:
        retention = 30
    cutoff = datetime.now(timezone.utc) - timedelta(days=retention)
    old = db.query(BackupRun).filter(BackupRun.created_at < cutoff).all()
    root = _backup_root().resolve()
    for row in old:
        if row.storage_key:
            path = Path(row.storage_key).resolve()
            if root in path.parents and path.exists():
                try: path.unlink()
                except OSError: pass
        db.delete(row)
    db.commit()


def run_backup(db: Session, created_by_id: int | None = None, destination: str | None = None) -> BackupRun:
    destination = (destination or _setting(db, "backup.destination", "local") or "local").lower()
    if destination not in {"local", "drive", "both"}:
        destination = "local"
    row = BackupRun(status="running", destination=destination, created_by_id=created_by_id)
    db.add(row); db.commit(); db.refresh(row)
    try:
        content = build_backup_bytes(db)
        filename = f"floresvolcan-backup-{datetime.now(timezone.utc):%Y%m%d-%H%M%S}.zip"
        row.filename = filename; row.size_bytes = len(content)
        if destination in {"local", "both"}:
            path = _backup_root() / filename
            path.write_bytes(content)
            row.storage_key = str(path)
        if destination in {"drive", "both"}:
            row.drive_file_id = _upload_drive(db, filename, content)
        row.status = "completed"; row.completed_at = datetime.now(timezone.utc)
        db.commit(); _cleanup(db); db.refresh(row)
        return row
    except Exception as exc:
        row.status = "failed"; row.error = str(exc)[:4000]; row.completed_at = datetime.now(timezone.utc)
        db.commit(); db.refresh(row)
        return row


def worker_cycle(db: Session):
    if _setting(db, "backup.enabled", "false").lower() not in {"1", "true", "yes", "si", "sí"}:
        return {"backup": "disabled"}
    tzname = os.getenv("APP_TIMEZONE", "America/Costa_Rica")
    try: local_now = datetime.now(ZoneInfo(tzname))
    except Exception: local_now = datetime.now(timezone.utc)
    try: hour = max(0, min(23, int(_setting(db, "backup.hour", "2") or "2")))
    except ValueError: hour = 2
    if local_now.hour != hour:
        return {"backup": "not_due"}
    freq = (_setting(db, "backup.frequency", "daily") or "daily").lower()
    if freq == "weekly":
        try: weekday = int(_setting(db, "backup.weekday", "0") or "0")
        except ValueError: weekday = 0
        if local_now.weekday() != weekday:
            return {"backup": "not_due"}
    last = db.query(BackupRun).filter(BackupRun.status == "completed").order_by(BackupRun.completed_at.desc()).first()
    if last and last.completed_at:
        last_local = last.completed_at.astimezone(local_now.tzinfo) if last.completed_at.tzinfo else last.completed_at.replace(tzinfo=timezone.utc).astimezone(local_now.tzinfo)
        if last_local.date() == local_now.date():
            return {"backup": "already_done"}
    row = run_backup(db)
    return {"backup": row.status, "id": row.id}
