from __future__ import annotations

import asyncio
from copy import deepcopy

import pytest
from fastapi import Request

from test_iot_backup import databases, seed_backup
from modules.mod_caficultura.iot_router import ingest_ttn_integration
from modules.mod_caficultura.model_iot import IoTIntegration, IoTNode, IoTReading
from security import hash_token


@pytest.fixture
def ingress(databases):
    source, _ = databases
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
