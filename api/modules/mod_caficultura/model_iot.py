from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import relationship
from sqlalchemy.types import JSON

from database import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class IoTIntegration(Base):
    """Conector de entrada IoT configurable por la instancia.

    Hoy el proveedor implementado es TTN. La entidad se mantiene genérica para
    permitir agregar en el futuro ChirpStack, MQTT bridge u otros conectores sin
    acoplar los sensores ni las fincas a credenciales globales del proceso.
    """

    __tablename__ = "navia_iot_integrations"
    __table_args__ = (
        UniqueConstraint(
            "provider", "application_id", name="uq_navia_iot_integration_provider_app"
        ),
        Index("ix_navia_iot_integration_provider_active", "provider", "activo"),
    )

    id = Column(Integer, primary_key=True)
    nombre = Column(String(180), nullable=False, index=True)
    provider = Column(String(40), nullable=False, default="ttn", server_default="ttn", index=True)
    integration_key = Column(String(80), nullable=False, unique=True, index=True)
    application_id = Column(String(120), nullable=True, index=True)
    webhook_secret_hash = Column(String(64), nullable=False)
    activo = Column(Boolean, nullable=False, default=True, server_default="true", index=True)
    config = Column(JSON, nullable=False, default=dict)
    last_seen_at = Column(DateTime(timezone=True), nullable=True, index=True)
    created_by_id = Column(
        Integer, ForeignKey("navia_usuarios.id", ondelete="SET NULL"), nullable=True
    )
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )

    created_by = relationship("Usuario")
    nodes = relationship("IoTNode", back_populates="integration")


class IoTNode(Base):
    __tablename__ = "navia_iot_nodes"
    __table_args__ = (
        CheckConstraint(
            "tipo IN ('ttn', 'microcontrolador')", name="ck_navia_iot_node_tipo"
        ),
        Index("ix_navia_iot_node_finca_activo", "finca_id", "activo"),
        Index("ix_navia_iot_node_integration_activo", "integration_id", "activo"),
    )

    id = Column(Integer, primary_key=True)
    finca_id = Column(
        Integer,
        ForeignKey("navia_fincas.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    integration_id = Column(
        Integer,
        ForeignKey("navia_iot_integrations.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    nombre = Column(String(180), nullable=False, index=True)
    # Para TTN es el DevEUI normalizado. Para API propia es el DID del nodo.
    did = Column(String(100), nullable=False, unique=True, index=True)
    tipo = Column(
        String(30), nullable=False, default="ttn", server_default="ttn", index=True
    )
    activo = Column(
        Boolean, nullable=False, default=True, server_default="true", index=True
    )
    variables_config = Column(JSON, nullable=False, default=dict)
    ingest_token_hash = Column(String(64), nullable=True)
    last_seen_at = Column(DateTime(timezone=True), nullable=True, index=True)
    created_by_id = Column(
        Integer, ForeignKey("navia_usuarios.id", ondelete="SET NULL"), nullable=True
    )
    created_at = Column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at = Column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )

    finca = relationship("Finca")
    integration = relationship("IoTIntegration", back_populates="nodes")
    created_by = relationship("Usuario")
    readings = relationship(
        "IoTReading", back_populates="node", cascade="all, delete-orphan"
    )


class IoTReading(Base):
    __tablename__ = "navia_iot_readings"
    __table_args__ = (
        UniqueConstraint(
            "node_id", "source_event_id", name="uq_navia_iot_reading_source_event"
        ),
        Index("ix_navia_iot_reading_node_recorded", "node_id", "recorded_at"),
    )

    id = Column(Integer, primary_key=True)
    node_id = Column(
        Integer,
        ForeignKey("navia_iot_nodes.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    recorded_at = Column(DateTime(timezone=True), nullable=False, index=True)
    received_at = Column(
        DateTime(timezone=True),
        default=utcnow,
        server_default=func.now(),
        nullable=False,
    )
    source = Column(
        String(30),
        nullable=False,
        default="manual",
        server_default="manual",
        index=True,
    )
    source_event_id = Column(String(180), nullable=True)
    data = Column(JSON, nullable=False, default=dict)
    raw_payload = Column(JSON, nullable=True)

    node = relationship("IoTNode", back_populates="readings")


class IoTDashboardPreset(Base):
    __tablename__ = "navia_iot_dashboard_presets"
    __table_args__ = (
        UniqueConstraint(
            "usuario_id", "finca_id", name="uq_navia_iot_preset_usuario_finca"
        ),
    )

    id = Column(Integer, primary_key=True)
    usuario_id = Column(
        Integer,
        ForeignKey("navia_usuarios.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    finca_id = Column(
        Integer,
        ForeignKey("navia_fincas.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    widgets = Column(JSON, nullable=False, default=list)
    created_at = Column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at = Column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )

    usuario = relationship("Usuario")
    finca = relationship("Finca")
