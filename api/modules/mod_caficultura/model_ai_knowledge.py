from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import relationship

from database import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class AIKnowledgeDocument(Base):
    __tablename__ = "navia_ai_knowledge_documents"
    __table_args__ = (
        UniqueConstraint("agent_id", "sha256", name="uq_navia_ai_knowledge_agent_sha"),
        Index("ix_navia_ai_knowledge_agent_active", "agent_id", "active"),
    )

    id = Column(Integer, primary_key=True)
    agent_id = Column(
        Integer,
        ForeignKey("navia_ai_agents.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    title = Column(String(220), nullable=False)
    original_name = Column(String(255), nullable=False)
    storage_key = Column(String(500), nullable=False, unique=True)
    media_type = Column(String(160), nullable=False)
    size_bytes = Column(Integer, nullable=False)
    sha256 = Column(String(64), nullable=False, index=True)
    character_count = Column(Integer, nullable=False, default=0, server_default="0")
    active = Column(
        Boolean, nullable=False, default=True, server_default="true", index=True
    )
    created_by_id = Column(
        Integer, ForeignKey("navia_usuarios.id", ondelete="SET NULL"), nullable=True
    )
    created_at = Column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at = Column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )

    agent = relationship("AIAgent")
    created_by = relationship("Usuario")
    chunks = relationship(
        "AIKnowledgeChunk", back_populates="document", cascade="all, delete-orphan"
    )


class AIKnowledgeChunk(Base):
    __tablename__ = "navia_ai_knowledge_chunks"
    __table_args__ = (
        UniqueConstraint(
            "document_id", "position", name="uq_navia_ai_knowledge_chunk_position"
        ),
        Index("ix_navia_ai_knowledge_chunk_document", "document_id", "position"),
    )

    id = Column(Integer, primary_key=True)
    document_id = Column(
        Integer,
        ForeignKey("navia_ai_knowledge_documents.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    position = Column(Integer, nullable=False)
    content = Column(Text, nullable=False)
    created_at = Column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    document = relationship("AIKnowledgeDocument", back_populates="chunks")
