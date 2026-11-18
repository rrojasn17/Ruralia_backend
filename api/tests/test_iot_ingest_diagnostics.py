from __future__ import annotations

import asyncio
import os
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4

import pytest
from fastapi import HTTPException, Request

from test_iot_backup import databases as databases, seed_backup
from modules.mod_caficultura.iot_router import ingest_ttn_integration
from modules.mod_caficultura.model_iot import IoTIntegration, IoTNode, IoTReading
from security import hash_token


@pytest.fixture
def ingress(request):
    source, _ = request.getfixturevalue('databases')
    seed_backup(source)
    node = source.query(IoTNode).one()
    # No prior readings: reproduce a newly registered sensor on the same application.
    source.query(IoTReading).delete(synchronize_session=False)
    node.variables_config = {}
    integration = source.query(IoTIntegration).one()
    integration.webhook_secret_hash = hash_token('test-webhook-secret')
    source.commit()
    payload = {
        'end_device_ids': {'dev_eui': node.did.lower(), 'application_ids': {'application_id': integration.application_id}},
        'uplink_message': {'decoded_payload': {'CO2': 450, 'PM10': 12, 'PM2_5': 8}, 'f_cnt': 1, 'session_key_id': 'test-session'},
    }
    return source, node, integration, payload


def ingest(db, integration, payload):
    request = Request({'type': 'http', 'method': 'POST', 'path': '/iot/ingest/ttn/test', 'headers': [], 'client': ('127.0.0.1', 1234)})
    return asyncio.run(ingest_ttn_integration(integration_key=integration.integration_key, payload=payload, request=request, x_ttn_webhook_secret='test-webhook-secret', db=db))


def test_registered_sensor_on_same_ttn_app_accepts_new_payload_and_retry(ingress):
    db, node, integration, payload = ingress
    result = ingest(db, integration, payload)
    assert result['status'] == 'success'
    assert result['data'] == {'co2': 450, 'pm10': 12, 'pm2_5': 8}
    assert db.query(IoTReading).one().node_id == node.id
    assert node.last_seen_at is not None
    assert ingest(db, integration, payload)['status'] == 'duplicate'
    assert db.query(IoTReading).count() == 1


def test_unmatched_dev_eui_is_logged_even_when_webhook_returns_success_http(ingress, caplog):
    db, node, integration, payload = ingress
    payload = deepcopy(payload)
    payload['end_device_ids']['dev_eui'] = 'A840410000000099'
    result = ingest(db, integration, payload)
    assert result['reason'] == 'sensor_not_registered_for_integration'
    assert 'A840410000000099' in caplog.text
    assert 'sensor_not_registered_for_integration' in caplog.text
    assert 'test-webhook-secret' not in caplog.text
    assert db.query(IoTReading).count() == 0
    assert node.last_seen_at is None


@pytest.mark.parametrize('decoded', [{}, {'status': 'no measurement'}, None])
def test_no_numeric_decoded_payload_is_logged_without_fabricating_readings(ingress, caplog, decoded):
    db, node, integration, payload = ingress
    payload['uplink_message']['decoded_payload'] = decoded
    result = ingest(db, integration, payload)
    assert result['reason'] == 'no_numeric_decoded_payload'
    assert 'no_numeric_decoded_payload' in caplog.text
    assert 'test-webhook-secret' not in caplog.text
    assert db.query(IoTReading).count() == 0
    assert node.last_seen_at is None
    assert node.ingest_diagnostics['reason'] == 'no_numeric_decoded_payload'
    assert node.ingest_diagnostics['received_at']


def add_second_sensor(db, node, integration):
    node.did = '2CF7F1C07380029C'
    second = IoTNode(
        finca_id=node.finca_id, integration_id=integration.id,
        nombre='v2CF7F1C0738002C3', did='2CF7F1C0738002C3',
        tipo='ttn', activo=True, variables_config={},
    )
    db.add(second)
    db.commit()
    return second


def test_sensecaps_same_app_session_counter_and_values_are_independent(ingress):
    db, node, integration, payload = ingress
    second = add_second_sensor(db, node, integration)
    payload['end_device_ids']['dev_eui'] = node.did.lower()
    first_result = ingest(db, integration, payload)
    second_payload = deepcopy(payload)
    second_payload['end_device_ids']['dev_eui'] = second.did.lower()
    second_result = ingest(db, integration, second_payload)
    assert first_result['status'] == second_result['status'] == 'success'
    assert first_result['reading_id'] != second_result['reading_id']
    assert first_result['data'] == second_result['data']
    assert ingest(db, integration, payload)['status'] == 'duplicate'
    assert ingest(db, integration, second_payload)['status'] == 'duplicate'
    assert db.query(IoTReading).count() == 2


def test_equal_values_on_new_frame_or_session_are_not_duplicates(ingress):
    db, node, integration, payload = ingress
    assert ingest(db, integration, payload)['status'] == 'success'
    payload['uplink_message']['f_cnt'] = 2
    assert ingest(db, integration, payload)['status'] == 'success'
    payload['uplink_message'].update(f_cnt=1, session_key_id='new-session')
    assert ingest(db, integration, payload)['status'] == 'success'
    assert db.query(IoTReading).count() == 3


def test_long_session_id_does_not_truncate_frame_counter(ingress):
    db, node, integration, payload = ingress
    payload['uplink_message']['session_key_id'] = 'x' * 300
    assert ingest(db, integration, payload)['status'] == 'success'
    payload['uplink_message']['f_cnt'] = 2
    assert ingest(db, integration, payload)['status'] == 'success'
    assert ingest(db, integration, payload)['status'] == 'duplicate'
    assert db.query(IoTReading).count() == 2


def test_shared_connection_correlation_cannot_discard_distinct_uplinks(ingress):
    db, node, integration, payload = ingress
    payload['uplink_message'].pop('session_key_id')
    payload['correlation_ids'] = ['gs:conn:shared-connection']
    payload['received_at'] = '2026-11-17T00:00:00Z'
    assert ingest(db, integration, payload)['status'] == 'success'
    payload['received_at'] = '2026-11-17T00:10:00Z'
    assert ingest(db, integration, payload)['status'] == 'success'
    assert ingest(db, integration, payload)['status'] == 'duplicate'
    assert db.query(IoTReading).count() == 2


def test_event_specific_correlation_is_used_after_shared_connection(ingress):
    db, node, integration, payload = ingress
    payload['uplink_message'].pop('session_key_id')
    payload['correlation_ids'] = ['gs:conn:shared', 'as:up:event-1']
    assert ingest(db, integration, payload)['status'] == 'success'
    payload['correlation_ids'][1] = 'as:up:event-2'
    assert ingest(db, integration, payload)['status'] == 'success'
    assert ingest(db, integration, payload)['status'] == 'duplicate'
    assert db.query(IoTReading).count() == 2


def test_official_sensecap_messages_remain_stable_when_reordered_and_fragmented(ingress):
    db, node, integration, payload = ingress
    # Seeed-Solution/SenseCAP-Decoder S210X/TTN sample-2, not a flat custom decoder.
    payload['uplink_message']['decoded_payload'] = {
        'valid': True, 'err': 0, 'payload': '01011098530000010210A87A0000AF51',
        'messages': [
            {'type': 'report_telemetry', 'measurementId': 4097, 'measurementValue': 21.4},
            {'type': 'report_telemetry', 'measurementId': 4098, 'measurementValue': 31.4},
        ],
    }
    assert ingest(db, integration, payload)['data'] == {'sensecap_4097': 21.4, 'sensecap_4098': 31.4}
    payload['uplink_message']['f_cnt'] = 2
    payload['uplink_message']['decoded_payload']['messages'].reverse()
    assert ingest(db, integration, payload)['data'] == {'sensecap_4097': 21.4, 'sensecap_4098': 31.4}
    payload['uplink_message']['f_cnt'] = 3
    payload['uplink_message']['decoded_payload']['messages'] = [
        {'type': 'report_telemetry', 'measurementId': 4098, 'measurementValue': 35},
    ]
    assert ingest(db, integration, payload)['data'] == {'sensecap_4098': 35}
    assert set(node.variables_config) == {'sensecap_4097', 'sensecap_4098'}
    assert db.query(IoTReading).order_by(IoTReading.id).first().raw_payload['uplink_message']['decoded_payload']['messages'][0]['measurementId'] == 4097


@pytest.mark.parametrize('valid,err,reason', [(False, -1, 'decoder_error'), (True, 0, 'no_numeric_decoded_payload')])
def test_sensecap_decoder_flags_are_not_measurements(ingress, valid, err, reason):
    db, node, integration, payload = ingress
    payload['uplink_message']['decoded_payload'] = {'valid': valid, 'err': err, 'messages': []}
    assert ingest(db, integration, payload)['reason'] == reason
    assert db.query(IoTReading).count() == 0
    assert node.ingest_diagnostics['reason'] == reason
    assert node.last_seen_at is None
    assert integration.last_seen_at is not None


def test_sensecap_startup_reports_only_battery(ingress):
    db, node, integration, payload = ingress
    payload['uplink_message']['decoded_payload'] = {
        'valid': True, 'err': 0, 'messages': [
            {'type': 'upload_version', 'hardwareVersion': '1.0', 'softwareVersion': '1.1'},
            {'type': 'upload_battery', 'battery': 100},
            {'type': 'upload_interval', 'interval': 3600},
            {'type': 'report_remove_sensor', 'channel': 1},
        ],
    }
    assert ingest(db, integration, payload)['data'] == {'battery': 100}


@pytest.mark.parametrize('decoded,expected', [
    ({'TempC_SHT': 25.5, 'Hum_SHT': '80', 'BatV': 3.1}, {'tempc_sht': 25.5, 'hum_sht': 80, 'batv': 3.1}),
    ({'measurements': [{'temperature': 25}, {'humidity': 80}]}, {'measurements_0_temperature': 25, 'measurements_1_humidity': 80}),
    ({'sensors': {'soil': {'values': {'temperature': 20}}}}, {'sensors_soil_values_temperature': 20}),
    ({'soil.temperature': 20}, {'soil_temperature': 20}),
])
def test_dragino_nested_and_literal_dot_payloads_work_after_first_message(ingress, decoded, expected):
    db, node, integration, payload = ingress
    payload['uplink_message']['decoded_payload'] = decoded
    assert ingest(db, integration, payload)['data'] == expected
    payload['uplink_message']['f_cnt'] = 2
    assert ingest(db, integration, payload)['data'] == expected
    assert len(node.variables_config) == len(expected)


def test_sensor_rate_limits_do_not_block_other_sensor_on_same_app(ingress, monkeypatch):
    import modules.mod_caficultura.iot_router as router

    monkeypatch.setattr(router, 'IOT_INGEST_RATE_LIMIT_PER_MINUTE', 1)
    db, node, integration, payload = ingress
    second = add_second_sensor(db, node, integration)
    payload['end_device_ids']['dev_eui'] = node.did
    assert ingest(db, integration, payload)['status'] == 'success'
    with pytest.raises(HTTPException) as error:
        ingest(db, integration, payload)
    assert error.value.status_code == 429
    payload['end_device_ids']['dev_eui'] = second.did
    assert ingest(db, integration, payload)['status'] == 'success'


@pytest.mark.parametrize('key,value', [
    ('end_device_ids', []), ('uplink_message', []), ('correlation_ids', 'bad'),
])
def test_malformed_envelopes_are_rejected_instead_of_crashing(ingress, key, value):
    db, node, integration, payload = ingress
    payload[key] = value
    with pytest.raises(HTTPException) as error:
        ingest(db, integration, payload)
    assert error.value.status_code == 422
    assert db.query(IoTReading).count() == 0


@pytest.mark.parametrize('decoded', [[], False, 0, 'invalid'])
def test_non_object_decoded_payload_is_rejected(ingress, decoded):
    db, node, integration, payload = ingress
    payload['uplink_message']['decoded_payload'] = decoded
    with pytest.raises(HTTPException) as error:
        ingest(db, integration, payload)
    assert error.value.status_code == 422
    assert db.query(IoTReading).count() == 0


def test_configuration_limit_is_diagnosed_without_fabricating_data(ingress, monkeypatch):
    import modules.mod_caficultura.iot_router as router

    monkeypatch.setattr(router, 'IOT_MAX_VARIABLES', 1)
    db, node, integration, payload = ingress
    node.variables_config = {'old': {'source_key': 'unavailable'}}
    db.commit()
    assert ingest(db, integration, payload)['reason'] == 'no_mapped_numeric_variables'
    assert db.query(IoTReading).count() == 0
    assert node.ingest_diagnostics['reason'] == 'no_mapped_numeric_variables'


def test_flatten_nested_payload_respects_total_variable_limit(monkeypatch):
    import modules.mod_caficultura.iot_router as router

    monkeypatch.setattr(router, 'IOT_MAX_VARIABLES', 2)
    assert len(router._flatten_numeric({'a': 1, 'b': {'x': 2, 'y': 3}})) == 2


def test_disabled_sensor_receipt_is_distinct_from_stored_measurement(ingress):
    db, node, integration, payload = ingress
    node.activo = False
    db.commit()
    assert ingest(db, integration, payload)['reason'] == 'sensor_inactive'
    assert node.ingest_diagnostics['status'] == 'ignored'
    assert db.query(IoTReading).count() == 0


def test_wrong_application_is_never_assigned_to_registered_sensor(ingress):
    db, node, integration, payload = ingress
    payload['end_device_ids']['application_ids']['application_id'] = 'another-app'
    with pytest.raises(HTTPException) as error:
        ingest(db, integration, payload)
    assert error.value.status_code == 403
    assert db.query(IoTReading).count() == 0


def test_status_exposes_last_receipt_and_duplicate_without_payload_or_secret(ingress):
    from modules.mod_caficultura.iot_router import get_node_status
    from modules.mod_caficultura.schema_iot import IoTNodeStatusOut

    db, node, integration, payload = ingress
    ingest(db, integration, payload)
    ingest(db, integration, payload)
    result = IoTNodeStatusOut.model_validate(get_node_status(node.id, current=None, db=db))
    assert result.reading_count == 1
    assert result.last_ingest['status'] == 'duplicate'
    assert result.last_ingest['decoded_keys'] == ['CO2', 'PM10', 'PM2_5']
    assert 'test-webhook-secret' not in result.model_dump_json()
    assert 'session_key_id' not in result.model_dump_json()


@pytest.fixture
def webhook_client():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    from sqlalchemy.pool import StaticPool
    from database import Base, get_db
    from modules.mod_caficultura.iot_router import router

    engine = create_engine('sqlite://', connect_args={'check_same_thread': False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        seed_backup(db)
        db.query(IoTReading).delete()
        node = db.query(IoTNode).one()
        integration = db.query(IoTIntegration).one()
        integration.webhook_secret_hash = hash_token('test-webhook-secret')
        add_second_sensor(db, node, integration)
    app = FastAPI()
    app.include_router(router)

    def database():
        with Session(engine, autoflush=False) as db:
            yield db

    app.dependency_overrides[get_db] = database
    with TestClient(app) as client:
        yield client, engine
    engine.dispose()


def test_http_webhook_accepts_both_sensecaps_and_retries(webhook_client):
    from sqlalchemy.orm import Session

    client, engine = webhook_client
    for did in ('2CF7F1C07380029C', '2CF7F1C0738002C3'):
        payload = {
            'end_device_ids': {'dev_eui': did, 'application_ids': {'application_id': 'backup-app'}},
            'received_at': '2026-11-17T00:00:00Z',
            'uplink_message': {'decoded_payload': {'Temperature': 20, 'Humidity': 80}, 'session_key_id': 'same-session', 'f_cnt': 1},
        }
        headers = {'X-TTN-Webhook-Secret': 'test-webhook-secret'}
        assert client.post('/iot/ingest/ttn/test-key', json=payload).status_code == 403
        response = client.post('/iot/ingest/ttn/test-key', json=payload, headers=headers)
        assert response.status_code == 200
        assert response.json()['status'] == 'success'
        assert client.post('/iot/ingest/ttn/test-key', json=payload, headers=headers).json()['status'] == 'duplicate'
    with Session(engine) as db:
        assert db.query(IoTReading).count() == 2


@pytest.fixture
def postgres_ingress():
    from sqlalchemy import create_engine, text
    from sqlalchemy.engine import make_url
    from sqlalchemy.orm import Session
    from database import Base

    url = os.getenv('IOT_TEST_POSTGRES_URL')
    if not url:
        pytest.skip('Requires a dedicated temporary PostgreSQL database')
    assert make_url(url).database == 'iot_test', 'Use only the dedicated iot_test database'
    schema = f'iot_test_{uuid4().hex}'
    admin_engine = create_engine(url)
    with admin_engine.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA {schema}'))
    engine = create_engine(url, connect_args={'options': f'-csearch_path={schema}'})
    try:
        Base.metadata.create_all(engine)
        with Session(engine) as db:
            seed_backup(db)
            db.query(IoTReading).delete()
            node = db.query(IoTNode).one()
            node.variables_config = {}
            integration = db.query(IoTIntegration).one()
            integration.webhook_secret_hash = hash_token('test-webhook-secret')
            db.commit()
            payload = {
                'end_device_ids': {'dev_eui': node.did, 'application_ids': {'application_id': integration.application_id}},
                'uplink_message': {'decoded_payload': {'Temperature': 20}, 'session_key_id': 'test-session', 'f_cnt': 1},
            }
            integration_id = integration.id
        yield engine, integration_id, payload
    finally:
        engine.dispose()
        with admin_engine.begin() as connection:
            connection.execute(text(f'DROP SCHEMA {schema} CASCADE'))
        admin_engine.dispose()


def test_postgres_simultaneous_retries_store_exactly_one_reading(postgres_ingress):
    from sqlalchemy.orm import Session

    engine, integration_id, payload = postgres_ingress
    barrier = Barrier(4)

    def deliver(_):
        with Session(engine, autoflush=False) as db:
            integration = db.get(IoTIntegration, integration_id)
            barrier.wait(timeout=10)
            return ingest(db, integration, deepcopy(payload))['status']

    with ThreadPoolExecutor(max_workers=4) as pool:
        statuses = list(pool.map(deliver, range(4)))
    assert statuses.count('success') == 1
    assert statuses.count('duplicate') == 3
    with Session(engine) as db:
        assert db.query(IoTReading).count() == 1


def test_postgres_simultaneous_fragments_preserve_all_variables(postgres_ingress, monkeypatch):
    import time
    import modules.mod_caficultura.iot_router as router
    from sqlalchemy.orm import Session

    engine, integration_id, payload = postgres_ingress
    barrier = Barrier(4)
    original_map = router._map_payload

    def slow_map(node, raw_data):
        # Widen the former read/modify/write race for independently arriving fragments.
        time.sleep(0.03)
        return original_map(node, raw_data)

    monkeypatch.setattr(router, '_map_payload', slow_map)

    def deliver(index):
        with Session(engine, autoflush=False) as db:
            integration = db.get(IoTIntegration, integration_id)
            fragment = deepcopy(payload)
            fragment['uplink_message'].update(f_cnt=index, decoded_payload={f'measurement_{index}': index})
            barrier.wait(timeout=10)
            return ingest(db, integration, fragment)['status']

    with ThreadPoolExecutor(max_workers=4) as pool:
        assert list(pool.map(deliver, range(4))) == ['success'] * 4
    with Session(engine) as db:
        assert db.query(IoTReading).count() == 4
        assert set(db.query(IoTNode).one().variables_config) == {f'measurement_{index}' for index in range(4)}


def test_postgres_diagnostic_migration_is_repeatable(postgres_ingress):
    from sqlalchemy import inspect, text
    from sqlalchemy.orm import Session
    from modules.mod_caficultura.lifecycle import upgrade_schema

    engine, _, _ = postgres_ingress
    with engine.begin() as connection:
        connection.execute(text('ALTER TABLE navia_iot_nodes DROP COLUMN ingest_diagnostics'))
    with Session(engine) as db:
        upgrade_schema(db)
        upgrade_schema(db)
    assert 'ingest_diagnostics' in {column['name'] for column in inspect(engine).get_columns('navia_iot_nodes')}
    with Session(engine) as db:
        assert db.query(IoTNode).count() == 1
