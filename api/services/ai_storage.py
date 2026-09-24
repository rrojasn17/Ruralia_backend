from __future__ import annotations

import hashlib
import mimetypes
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import HTTPException, UploadFile, status

from config import AI_MAX_UPLOAD_MB, AI_PRIVATE_UPLOAD_DIR


ALLOWED_EXTENSIONS = {
    ".pdf",
    ".png",
    ".jpg",
    ".jpeg",
    ".webp",
    ".doc",
    ".docx",
    ".xls",
    ".xlsx",
    ".csv",
    ".json",
    ".md",
    ".txt",
    ".webm",
    ".ogg",
    ".mp3",
    ".m4a",
    ".wav",
    ".mp4",
}
BLOCKED_MEDIA_TYPES = {
    "text/html",
    "application/xhtml+xml",
    "image/svg+xml",
    "application/javascript",
    "text/javascript",
    "application/x-executable",
}
AUDIO_EXTENSIONS = {".webm", ".ogg", ".mp3", ".m4a", ".wav", ".mp4"}
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}


def private_upload_root() -> Path:
    root = Path(AI_PRIVATE_UPLOAD_DIR).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


def safe_original_name(value: str | None) -> str:
    raw = Path(value or "archivo").name
    cleaned = re.sub(r"[^A-Za-z0-9._ -]+", "_", raw).strip(" ._")
    return (cleaned or "archivo")[:255]


def classify_attachment(extension: str, media_type: str) -> str:
    if extension in IMAGE_EXTENSIONS or media_type.startswith("image/"):
        return "image"
    if extension in AUDIO_EXTENSIONS or media_type.startswith("audio/"):
        return "audio"
    return "file"


def _validate_signature(extension: str, first_bytes: bytes) -> None:
    if extension == ".pdf" and not first_bytes.startswith(b"%PDF-"):
        raise HTTPException(status_code=422, detail="El archivo no contiene un PDF válido")
    if extension == ".png" and not first_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
        raise HTTPException(status_code=422, detail="El archivo no contiene una imagen PNG válida")
    if extension in {".jpg", ".jpeg"} and not first_bytes.startswith(b"\xff\xd8\xff"):
        raise HTTPException(status_code=422, detail="El archivo no contiene una imagen JPEG válida")
    if extension == ".webp" and not (first_bytes.startswith(b"RIFF") and first_bytes[8:12] == b"WEBP"):
        raise HTTPException(status_code=422, detail="El archivo no contiene una imagen WEBP válida")
    if extension in {".docx", ".xlsx"} and not first_bytes.startswith(b"PK"):
        raise HTTPException(status_code=422, detail="El documento Office no es válido")


def _target_for(usuario_id: int, original_name: str) -> tuple[Path, str, str, str]:
    original = safe_original_name(original_name)
    extension = Path(original).suffix.lower()
    if extension not in ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Tipo de archivo no permitido. Use audio, imagen, PDF, Word, Excel, CSV o TXT.",
        )
    now = datetime.now(timezone.utc)
    relative = Path(str(usuario_id)) / f"{now.year:04d}" / f"{now.month:02d}" / f"{uuid.uuid4().hex}{extension}"
    target = private_upload_root() / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    return target, relative.as_posix(), original, extension


def _write_chunks(target: Path, chunks, extension: str) -> tuple[int, str]:
    max_bytes = AI_MAX_UPLOAD_MB * 1024 * 1024
    size = 0
    digest = hashlib.sha256()
    first = bytearray()
    try:
        with target.open("xb") as destination:
            for chunk in chunks:
                if not chunk:
                    continue
                size += len(chunk)
                if size > max_bytes:
                    raise HTTPException(
                        status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                        detail=f"Cada archivo debe pesar como máximo {AI_MAX_UPLOAD_MB} MB",
                    )
                if len(first) < 32:
                    first.extend(chunk[: 32 - len(first)])
                digest.update(chunk)
                destination.write(chunk)
        if size == 0:
            raise HTTPException(status_code=422, detail="El archivo está vacío")
        _validate_signature(extension, bytes(first))
        return size, digest.hexdigest()
    except Exception:
        target.unlink(missing_ok=True)
        raise


def save_upload(file: UploadFile, usuario_id: int) -> dict[str, object]:
    target, storage_key, original, extension = _target_for(usuario_id, file.filename or "archivo")
    media_type = (file.content_type or mimetypes.guess_type(original)[0] or "application/octet-stream").split(";", 1)[0].lower()
    if media_type in BLOCKED_MEDIA_TYPES:
        raise HTTPException(status_code=422, detail="El tipo de contenido no está permitido")

    def chunks():
        while True:
            chunk = file.file.read(1024 * 1024)
            if not chunk:
                return
            yield chunk

    size, digest = _write_chunks(target, chunks(), extension)
    return {
        "storage_key": storage_key,
        "original_name": original,
        "media_type": media_type,
        "kind": classify_attachment(extension, media_type),
        "size_bytes": size,
        "sha256": digest,
    }


def save_bytes(content: bytes, usuario_id: int, filename: str, media_type: str | None = None) -> dict[str, object]:
    target, storage_key, original, extension = _target_for(usuario_id, filename)
    normalized_type = (media_type or mimetypes.guess_type(original)[0] or "application/octet-stream").split(";", 1)[0].lower()
    if normalized_type in BLOCKED_MEDIA_TYPES:
        raise HTTPException(status_code=422, detail="El tipo de contenido no está permitido")
    size, digest = _write_chunks(target, [content], extension)
    return {
        "storage_key": storage_key,
        "original_name": original,
        "media_type": normalized_type,
        "kind": classify_attachment(extension, normalized_type),
        "size_bytes": size,
        "sha256": digest,
    }


def resolve_storage_path(storage_key: str) -> Path:
    root = private_upload_root()
    candidate = (root / storage_key).resolve()
    if root != candidate and root not in candidate.parents:
        raise ValueError("Ruta de almacenamiento inválida")
    return candidate
