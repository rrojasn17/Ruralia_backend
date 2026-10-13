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
    payload["schema"] = "navia_backup_v1"
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


def test_restore_matches_ttn_application_and_remaps_node_reference(databases):
    source, target = databases
    payload = seed_backup(source)
    target.add(IoTIntegration(
        id=1, nombre="Otra aplicación", provider="ttn", application_id="other-app",
        integration_key="other-key", webhook_secret_hash="c" * 64,
    ))
    target.add(IoTIntegration(
        id=42, nombre="Local", provider="ttn", application_id="backup-app",
        integration_key="local-key", webhook_secret_hash="b" * 64,
    ))
    target.commit()
    restore(target, payload)
    assert target.query(IoTIntegration).count() == 2
    assert target.query(IoTNode).one().integration_id == 42
    assert target.get(IoTIntegration, 42).integration_key == "test-key"
    assert target.get(IoTIntegration, 1).application_id == "other-app"
    restore(target, payload)
    assert target.query(IoTIntegration).count() == 2
    assert target.query(IoTReading).count() == 1


def test_payload_discovers_dragino_and_future_numeric_variables():
    from modules.mod_caficultura.iot_router import _map_payload

    node = IoTNode(variables_config={})
    assert _map_payload(node, {"CO2": 410, "PM10": 12, "PM2_5": "8"}) == {
        "co2": 410, "pm10": 12, "pm2_5": 8,
    }
    assert _map_payload(node, {"CO2": 420, "future": {"measurement": 7}}) == {
        "co2": 420, "future_measurement": 7,
    }
    assert set(node.variables_config) == {"co2", "pm10", "pm2_5", "future_measurement"}


def test_catalog_id_remap_keeps_activity_references_and_is_repeatable(databases):
    from datetime import date
    from modules.mod_caficultura.models import ActividadFinca, RegistroFinca
    source, target = databases
    seed_backup(source)
    source.add_all([
        ActividadFinca(id=1, nombre='Fertilizar', tipo='fertilizacion'),
        ActividadFinca(id=20, nombre='Atomizar', tipo='atomizacion'),
    ])
    source.flush()
    day = date(2026, 10, 12)
    source.add_all([
        RegistroFinca(id=1, fecha=day, semana_inicio=day, semana_fin=day, finca_id=1, actividad_id=1),
        RegistroFinca(id=2, fecha=day, semana_inicio=day, semana_fin=day, finca_id=1, actividad_id=20),
    ])
    source.commit()
    payload = json.loads(exportar_respaldo(current=source.get(Usuario, 1), db=source).body)
    target.add_all([
        ActividadFinca(id=20, nombre='Fertilizar', tipo='fertilizacion'),
        ActividadFinca(id=21, nombre='Atomizar', tipo='atomizacion'),
    ])
    target.commit()
    restore(target, payload)
    assert target.get(RegistroFinca, 1).actividad_id == 20
    assert target.get(RegistroFinca, 2).actividad_id == 21
    assert target.query(ActividadFinca).count() == 2
    assert restore(target, payload)['importados'] == 0


def test_import_preserves_unrelated_records_with_occupied_source_ids(databases):
    source, target = databases
    payload = seed_backup(source)
    target.add(Usuario(id=1, nombre='Local user', correo='unrelated@example.test', hash_contrasena='local', rol='gerente'))
    target.commit()
    restore(target, payload)
    imported_user = target.query(Usuario).filter(Usuario.correo == 'iot-backup@example.test').one()
    assert imported_user.id != 1
    assert target.get(Usuario, 1).correo == 'unrelated@example.test'
    assert target.query(IoTNode).one().created_by_id == imported_user.id
    assert target.query(IoTDashboardPreset).one().usuario_id == imported_user.id
    assert restore(target, payload)['importados'] == 0


def test_conflicting_unique_keys_roll_back_import(databases):
    source, target = databases
    payload = seed_backup(source)
    target.add_all([
        Cliente(codigo='BACKUP', nombre_completo='By code', numero_identificacion='OTHER'),
        Cliente(codigo='OTHER', nombre_completo='By identification', numero_identificacion='BACKUP'),
    ])
    target.commit()
    with pytest.raises(HTTPException) as error:
        restore(target, payload)
    assert error.value.status_code == 409
    assert target.query(Usuario).count() == 0
    assert target.query(Cliente).count() == 2


def test_full_reset_clears_owned_data_and_prevents_reseeding(databases):
    from config import ADMIN_EMAIL
    from core.models import ConfiguracionSistema
    from sqlalchemy import func, select
    from modules.mod_caficultura.models import NotaRapida, Proveedor, ProveedorContacto
    from modules.mod_caficultura.model_ai_consulting import AIAgent
    from modules.mod_caficultura.router import borrado_completo_app, reset_data_tables
    from modules.mod_caficultura.services import bootstrap

    source, target = databases
    restore(target, seed_backup(source))
    admin = target.query(Usuario).one()
    admin.correo = ADMIN_EMAIL
    admin.is_superadmin = True
    target.add_all([
        ConfiguracionSistema(clave='app_name', valor='Local brand'),
        NotaRapida(contenido='Test', finca_id=1, created_by_id=admin.id),
        Proveedor(id=1, nombre_legal='Test provider'),
        AIAgent(name='Test agent', slug='test-agent', model='test-model', instructions='Test'),
    ])
    target.flush()
    target.add(ProveedorContacto(proveedor_id=1, nombre='Test contact'))
    target.commit()
    result = borrado_completo_app(current=admin, db=target)
    target.expire_all()
    assert result['deleted']['navia_iot_integrations'] == 1
    assert result['deleted']['navia_notas_rapidas'] == 1
    assert result['deleted']['navia_proveedores'] == 1
    assert result['deleted']['navia_ai_agents'] == 1
    for table in reset_data_tables():
        assert target.scalar(select(func.count()).select_from(table)) == 0, table.name
    assert target.query(Usuario).one().correo == ADMIN_EMAIL
    assert target.query(ConfiguracionSistema).filter_by(clave='app_name').one().valor == 'Local brand'
    # Simulate a later startup, including an instance configured with demo data.
    bootstrap(target, ADMIN_EMAIL, 'unused', 'Test', 30, seed_demo_data=True, manage_core_identity=False)
    for table in reset_data_tables():
        assert target.scalar(select(func.count()).select_from(table)) == 0, table.name
    payload = json.loads(exportar_respaldo(current=source.get(Usuario, 1), db=source).body)
    restore(target, payload)
    assert target.query(IoTReading).count() == len(payload['tables']['navia_iot_readings'])


def test_node_identity_remaps_readings_and_formula_json(databases):
    source, target = databases
    payload = seed_backup(source)
    restore(target, payload)
    payload['tables']['navia_iot_nodes'][0]['id'] = 99
    payload['tables']['navia_iot_readings'][0]['node_id'] = 99
    widget = payload['tables']['navia_iot_dashboard_presets'][0]['widgets'][0]
    widget['node_id'] = 99
    widget['inputs']['x1']['node_id'] = 99
    result = restore(target, payload)
    assert result['importados'] == 0
    assert target.query(IoTNode).count() == 1
    assert target.query(IoTReading).one().node_id == 1
    restored = target.query(IoTDashboardPreset).one().widgets[0]
    assert restored['node_id'] == 1
    assert restored['inputs']['x1']['node_id'] == 1
