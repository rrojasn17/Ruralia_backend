from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from io import BytesIO
from types import SimpleNamespace

import pytest
from fastapi import HTTPException, UploadFile
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session

from main import app  # noqa: F401 - Register industry models and relationships.
from database import Base
from core.models import Usuario
from modules.mod_caficultura.models import Cliente, Finca
from modules.mod_caficultura.model_iot import (
    IoTIntegration,
    IoTNode,
    IoTReading,
    IoTDashboardPreset,
)
from modules.mod_caficultura.router import (
    exportar_respaldo,
    importar_respaldo,
    IOT_BACKUP_MODELS,
)


@pytest.fixture
def databases():
    engines = [create_engine("sqlite://") for _ in range(2)]
    for engine in engines:

        @event.listens_for(engine, "connect")
        def foreign_keys(connection, _record):
            connection.execute("PRAGMA foreign_keys=ON")

        Base.metadata.create_all(engine)
    with (
        Session(engines[0], autoflush=False) as source,
        Session(engines[1], autoflush=False) as target,
    ):
        yield source, target
    for engine in engines:
        engine.dispose()


def seed_backup(db):
    user = Usuario(
        nombre="Backup",
        correo="iot-backup@example.test",
        hash_contrasena="test-only",
        rol="gerente",
    )
    customer = Cliente(
        codigo="BACKUP",
        nombre_completo="Cliente Backup",
        numero_identificacion="BACKUP",
    )
    db.add_all([user, customer])
    db.flush()
    farm = Finca(cliente_id=customer.id, codigo="BACKUP-FARM", nombre="Finca Backup")
    integration = IoTIntegration(
        nombre="TTN Backup",
        provider="ttn",
        integration_key="test-key",
        application_id="backup-app",
        webhook_secret_hash="a" * 64,
        created_by_id=user.id,
    )
    db.add_all([farm, integration])
    db.flush()
    node = IoTNode(
        finca_id=farm.id,
        integration_id=integration.id,
        nombre="Ambiente",
        did="A840410000000001",
        tipo="ttn",
        created_by_id=user.id,
        variables_config={
            "temperature": {
                "source_key": "temperature",
                "label": "Temperatura",
                "unit": "°C",
            }
        },
    )
    db.add(node)
    db.flush()
    reading = IoTReading(
        node_id=node.id,
        recorded_at=datetime.now(timezone.utc),
        source="ttn",
        source_event_id="event-1",
        data={"temperature": 25},
        raw_payload={"uplink_message": {"decoded_payload": {"temperature": 25}}},
    )
    preset = IoTDashboardPreset(
        usuario_id=user.id,
        finca_id=farm.id,
        widgets=[
            {
                "id": "fahrenheit",
                "node_id": node.id,
                "variable_id": "temperature",
                "formula": "x1 * 9 / 5 + 32",
                "inputs": {"x1": {"node_id": node.id, "variable_id": "temperature"}},
                "hidden": True,
                "color": "#112233",
                "kind": "area",
                "unit": "°F",
            }
        ],
    )
    db.add_all([reading, preset])
    db.commit()
    return json.loads(exportar_respaldo(current=user, db=db).body)


def restore(db, payload):
    upload = UploadFile(
        filename="backup.json", file=BytesIO(json.dumps(payload).encode())
    )
    return asyncio.run(
        importar_respaldo(file=upload, current=SimpleNamespace(id=1), db=db)
    )


def test_backup_roundtrip_includes_iot_data_relationships_and_preset(databases):
    source, target = databases
    payload = seed_backup(source)
    for model in IOT_BACKUP_MODELS:
        assert len(payload["tables"][model.__tablename__]) == 1
    result = restore(target, payload)
    assert result["advertencias"] == []
    node = target.query(IoTNode).one()
    assert node.finca.nombre == "Finca Backup"
    assert node.integration.application_id == "backup-app"
    assert node.integration.webhook_secret_hash == "a" * 64
    assert node.variables_config["temperature"]["unit"] == "°C"
    reading = target.query(IoTReading).one()
    assert reading.node_id == node.id and reading.data == {"temperature": 25}
    assert reading.source_event_id == "event-1"
    assert reading.raw_payload["uplink_message"]["decoded_payload"] == reading.data
    preset = target.query(IoTDashboardPreset).one()
    assert preset.usuario_id == node.created_by_id
    assert preset.widgets[0]["hidden"] is True
    assert preset.widgets[0]["formula"] == "x1 * 9 / 5 + 32"
    again = restore(target, payload)
    assert again["importados"] == 0
    assert target.query(IoTReading).count() == 1


def test_legacy_backup_warns_and_does_not_remove_existing_iot(databases):
    source, target = databases
    payload = seed_backup(source)
    restore(target, payload)
    for model in IOT_BACKUP_MODELS:
        del payload["tables"][model.__tablename__]
    result = restore(target, payload)
    assert len(result["advertencias"]) == 1
    assert "navia_iot_nodes" in result["advertencias"][0]
    assert target.query(IoTNode).count() == 1
    assert target.query(IoTReading).count() == 1


def test_invalid_iot_reference_rolls_back_entire_import(databases):
    source, target = databases
    payload = seed_backup(source)
    payload["tables"]["navia_iot_readings"][0]["node_id"] = 9999
    with pytest.raises(HTTPException) as error:
        restore(target, payload)
    assert error.value.status_code == 409
    assert target.query(IoTNode).count() == 0
    assert target.query(Cliente).count() == 0


@pytest.mark.parametrize("rows", [{}, [None], [{"node_id": 1}]])
def test_malformed_iot_table_is_rejected(databases, rows):
    _, target = databases
    with pytest.raises(HTTPException) as error:
        restore(
            target,
            {"schema": "navia_backup_v1", "tables": {"navia_iot_readings": rows}},
        )
    assert error.value.status_code == 422
