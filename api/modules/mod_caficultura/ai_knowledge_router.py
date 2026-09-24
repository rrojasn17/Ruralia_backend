from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from fastapi.responses import FileResponse
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from config import AI_KNOWLEDGE_MAX_DOCUMENTS
from database import get_db
from modules.mod_caficultura.model_ai_consulting import AIAgent
from modules.mod_caficultura.model_ai_knowledge import AIKnowledgeDocument
from core.models import Usuario
from core.routers.auth import require_roles
from modules.mod_caficultura.schema_ai_consulting import AIKnowledgeDocumentOut, AIKnowledgeDocumentUpdate
from modules.mod_caficultura.ai_knowledge import (
    KnowledgeExtractionError,
    SUPPORTED_EXTENSIONS,
    extract_document_text,
    index_document,
)
from services.ai_storage import resolve_storage_path, save_upload


router = APIRouter(prefix="/ai", tags=["ai-knowledge"])
manager_dependency = require_roles("gerente")


def _document_or_404(db: Session, document_id: int) -> AIKnowledgeDocument:
    row = (
        db.query(AIKnowledgeDocument)
        .options(selectinload(AIKnowledgeDocument.chunks))
        .filter(AIKnowledgeDocument.id == document_id)
        .first()
    )
    if not row:
        raise HTTPException(status_code=404, detail="Documento RAG no encontrado")
    return row


def _serialize(row: AIKnowledgeDocument) -> dict:
    return {
        "id": row.id,
        "agent_id": row.agent_id,
        "title": row.title,
        "original_name": row.original_name,
        "media_type": row.media_type,
        "size_bytes": row.size_bytes,
        "character_count": row.character_count,
        "chunk_count": len(row.chunks or []),
        "active": bool(row.active),
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


@router.get("/knowledge-documents", response_model=list[AIKnowledgeDocumentOut])
def list_knowledge_documents(
    agent_id: int | None = None,
    current: Usuario = Depends(manager_dependency),
    db: Session = Depends(get_db),
):
    query = db.query(AIKnowledgeDocument).options(
        selectinload(AIKnowledgeDocument.chunks)
    )
    if agent_id is not None:
        query = query.filter(AIKnowledgeDocument.agent_id == agent_id)
    rows = query.order_by(
        AIKnowledgeDocument.created_at.desc(), AIKnowledgeDocument.id.desc()
    ).all()
    return [_serialize(row) for row in rows]


@router.post(
    "/knowledge-documents",
    response_model=AIKnowledgeDocumentOut,
    status_code=status.HTTP_201_CREATED,
)
async def upload_knowledge_document(
    agent_id: int = Form(...),
    title: str | None = Form(default=None),
    file: UploadFile = File(...),
    current: Usuario = Depends(manager_dependency),
    db: Session = Depends(get_db),
):
    agent = db.query(AIAgent).filter(AIAgent.id == agent_id).first()
    if not agent:
        raise HTTPException(status_code=404, detail="Agente no encontrado")
    document_count = (
        db.query(AIKnowledgeDocument.id)
        .filter(AIKnowledgeDocument.agent_id == agent_id)
        .count()
    )
    if document_count >= AI_KNOWLEDGE_MAX_DOCUMENTS:
        raise HTTPException(
            status_code=409, detail="El agente alcanzó el máximo de documentos RAG"
        )
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in SUPPORTED_EXTENSIONS:
        raise HTTPException(
            status_code=422, detail="Use PDF, DOCX, XLSX, CSV, JSON, Markdown o TXT"
        )

    stored = save_upload(file, current.id)
    stored_path = resolve_storage_path(str(stored["storage_key"]))
    try:
        extracted = extract_document_text(stored_path, str(stored["original_name"]))
        row = AIKnowledgeDocument(
            agent_id=agent_id,
            title=(str(title or "").strip() or Path(str(stored["original_name"])).stem)[
                :220
            ],
            original_name=str(stored["original_name"]),
            storage_key=str(stored["storage_key"]),
            media_type=str(stored["media_type"]),
            size_bytes=int(stored["size_bytes"]),
            sha256=str(stored["sha256"]),
            created_by_id=current.id,
        )
        db.add(row)
        db.flush()
        index_document(db, row, extracted)
        db.commit()
        row = _document_or_404(db, row.id)
        return _serialize(row)
    except KnowledgeExtractionError as exc:
        db.rollback()
        stored_path.unlink(missing_ok=True)
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except IntegrityError as exc:
        db.rollback()
        stored_path.unlink(missing_ok=True)
        raise HTTPException(
            status_code=409, detail="Este documento ya está indexado para el agente"
        ) from exc
    except Exception:
        db.rollback()
        stored_path.unlink(missing_ok=True)
        raise


@router.patch(
    "/knowledge-documents/{document_id}", response_model=AIKnowledgeDocumentOut
)
def update_knowledge_document(
    document_id: int,
    payload: AIKnowledgeDocumentUpdate,
    current: Usuario = Depends(manager_dependency),
    db: Session = Depends(get_db),
):
    row = _document_or_404(db, document_id)
    values = payload.model_dump(exclude_unset=True)
    if "title" in values:
        values["title"] = str(values["title"]).strip()
    for key, value in values.items():
        setattr(row, key, value)
    db.commit()
    row = _document_or_404(db, document_id)
    return _serialize(row)


@router.get("/knowledge-documents/{document_id}/download")
def download_knowledge_document(
    document_id: int,
    current: Usuario = Depends(manager_dependency),
    db: Session = Depends(get_db),
):
    row = _document_or_404(db, document_id)
    path = resolve_storage_path(row.storage_key)
    if not path.is_file():
        raise HTTPException(status_code=410, detail="El archivo ya no está disponible")
    return FileResponse(
        path,
        media_type=row.media_type,
        filename=row.original_name,
        content_disposition_type="attachment",
        headers={
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.delete(
    "/knowledge-documents/{document_id}", status_code=status.HTTP_204_NO_CONTENT
)
def delete_knowledge_document(
    document_id: int,
    current: Usuario = Depends(manager_dependency),
    db: Session = Depends(get_db),
):
    row = _document_or_404(db, document_id)
    path = resolve_storage_path(row.storage_key)
    db.delete(row)
    db.commit()
    path.unlink(missing_ok=True)
