from __future__ import annotations

import io
import uuid

from fastapi.testclient import TestClient
from PIL import Image

from database import SessionLocal
from main import app
from models import Cliente, Finca
from services.ai_knowledge import search_knowledge_base


ADMIN_EMAIL = "ai-tests-admin@example.com"
ADMIN_PASSWORD = "AiTestsAdmin2026!"


def _login(client: TestClient, correo: str, password: str) -> None:
    response = client.post("/auth/login", json={"correo": correo, "password": password})
    assert response.status_code == 200, response.text


def _create_farm_pair() -> tuple[Finca, Finca]:
    suffix = uuid.uuid4().hex[:10]
    db = SessionLocal()
    try:
        farms: list[Finca] = []
        for index in (1, 2):
            customer = Cliente(
                codigo=f"CL-IOT-{suffix}-{index}",
                nombre_completo=f"Cliente IoT {suffix} {index}",
                tipo_persona="personal",
                categoria="agro",
                numero_identificacion=f"IOT-{suffix}-{index}",
                activo=True,
            )
            db.add(customer)
            db.flush()
            farm = Finca(
                cliente_id=customer.id,
                codigo=f"FN-IOT-{suffix}-{index}",
                nombre=f"Finca IoT {suffix} {index}",
                propietario=customer.nombre_completo,
                cultivo="Café",
                activa=True,
            )
            db.add(farm)
            db.flush()
            farms.append(farm)
        ids = [farm.id for farm in farms]
        db.commit()
        return tuple(db.query(Finca).filter(Finca.id.in_(ids)).order_by(Finca.id).all())  # type: ignore[return-value]
    finally:
        db.close()


def test_white_label_is_public_but_only_managers_can_change_it():
    platform_name = f"Agro Marca {uuid.uuid4().hex[:6]}"
    with TestClient(app) as client:
        client.headers["X-Forwarded-For"] = "198.51.100.101"
        public = client.get("/branding")
        assert public.status_code == 200
        assert set(public.json()) == {"app_name", "app_logo_url"}
        assert (
            client.patch("/configuracion", json={"app_name": platform_name}).status_code
            == 401
        )

        _login(client, ADMIN_EMAIL, ADMIN_PASSWORD)
        updated = client.patch("/configuracion", json={"app_name": platform_name})
        assert updated.status_code == 200, updated.text
        assert updated.json()["app_name"] == platform_name

        image_buffer = io.BytesIO()
        Image.new("RGB", (16, 16), "#176B75").save(image_buffer, format="PNG")
        logo = client.post(
            "/configuracion/branding/logo",
            files={"file": ("marca.png", image_buffer.getvalue(), "image/png")},
        )
        assert logo.status_code == 200, logo.text
        assert "/uploads/branding/logo_" in logo.json()["app_logo_url"]

        client.post("/auth/logout")
        public = client.get("/branding")
        assert public.json()["app_name"] == platform_name
        assert "/uploads/branding/logo_" in public.json()["app_logo_url"]

        _login(client, ADMIN_EMAIL, ADMIN_PASSWORD)
        assert client.delete("/configuracion/branding/logo").status_code == 200
        assert (
            client.patch("/configuracion", json={"app_name": "NAVIA"}).status_code
            == 200
        )


def test_iot_ingestion_dashboard_and_export_enforce_farm_ownership(monkeypatch):
    farm_one, farm_two = _create_farm_pair()
    suffix = uuid.uuid4().hex[:8]
    with TestClient(app) as client:
        client.headers["X-Forwarded-For"] = "198.51.100.102"
        _login(client, ADMIN_EMAIL, ADMIN_PASSWORD)
        first = client.post(
            "/iot/nodes",
            json={
                "finca_id": farm_one.id,
                "nombre": "Sonda de suelo",
                "did": f"SOIL-{suffix}",
                "tipo": "microcontrolador",
                "variables_config": {},
            },
        )
        assert first.status_code == 201, first.text
        first_node = first.json()
        assert first_node["cliente_id"] == farm_one.cliente_id
        assert first_node["ingest_token"]

        second = client.post(
            "/iot/nodes",
            json={
                "finca_id": farm_two.id,
                "nombre": "Estación ajena",
                "did": f"OTHER-{suffix}",
                "tipo": "microcontrolador",
                "variables_config": {},
            },
        )
        assert second.status_code == 201, second.text
        second_node = second.json()

        integration_response = client.post(
            "/iot/integrations",
            json={
                "nombre": "TTN Café pruebas",
                "provider": "ttn",
                "application_id": "ruralia-cafe-test",
                "activo": True,
            },
        )
        assert integration_response.status_code == 201, integration_response.text
        integration = integration_response.json()
        assert integration["webhook_secret"]

        ttn = client.post(
            "/iot/nodes",
            json={
                "finca_id": farm_one.id,
                "integration_id": integration["id"],
                "nombre": "Estación ambiental TTN",
                "did": "A84041ABCDEF1234",
                "tipo": "ttn",
                "variables_config": {},
            },
        )
        assert ttn.status_code == 201, ttn.text
        ttn_node = ttn.json()
        assert ttn_node["ingest_token"] is None
        assert ttn_node["integration_id"] == integration["id"]
        assert ttn_node["ttn_application_id"] == "ruralia-cafe-test"

        ttn_secret = integration["webhook_secret"]
        ttn_path = integration["webhook_path"]
        ttn_payload = {
            "end_device_ids": {
                "dev_eui": "A84041ABCDEF1234",
                "application_ids": {"application_id": "ruralia-cafe-test"},
            },
            "correlation_ids": [f"ttn-{suffix}"],
            "received_at": "2026-08-20T12:00:01Z",
            "uplink_message": {
                "session_key_id": f"session-{suffix}",
                "f_cnt": 42,
                "received_at": "2026-08-20T12:00:00Z",
                "decoded_payload": {"temperature": 24.5, "humidity": 77, "battery": 88},
            },
        }
        assert client.post(ttn_path, json=ttn_payload).status_code == 403
        accepted_ttn = client.post(
            ttn_path,
            headers={"X-TTN-Webhook-Secret": ttn_secret},
            json=ttn_payload,
        )
        assert accepted_ttn.status_code == 200, accepted_ttn.text
        assert accepted_ttn.json()["status"] == "success"
        assert accepted_ttn.json()["did"] == "A84041ABCDEF1234"

        repeated_ttn = client.post(
            ttn_path,
            headers={"X-TTN-Webhook-Secret": ttn_secret},
            json=ttn_payload,
        )
        assert repeated_ttn.status_code == 200, repeated_ttn.text
        assert repeated_ttn.json()["status"] == "duplicate"

        ttn_status = client.get(f"/iot/nodes/{ttn_node['id']}/status")
        assert ttn_status.status_code == 200, ttn_status.text
        ttn_status_body = ttn_status.json()
        assert ttn_status_body["has_data"] is True
        assert ttn_status_body["reading_count"] == 1
        assert ttn_status_body["latest_source_keys"] == ["battery", "humidity", "temperature"]

        refreshed_ttn = client.get("/iot/nodes", params={"finca_id": farm_one.id})
        assert refreshed_ttn.status_code == 200
        refreshed_node = next(row for row in refreshed_ttn.json() if row["id"] == ttn_node["id"])
        assert set(refreshed_node["variables_config"]) == {"temperature", "humidity", "battery"}

        payload = {
            "event_id": f"event-{suffix}",
            "data": {"soil": {"temperature": 21.75, "moisture": 64.2}, "battery": 91},
        }
        rejected = client.post(
            f"/iot/ingest/microcontroller/{first_node['did']}",
            headers={"X-IoT-Token": "invalid-token"},
            json=payload,
        )
        assert rejected.status_code == 403

        accepted = client.post(
            f"/iot/ingest/microcontroller/{first_node['did']}",
            headers={"X-IoT-Token": first_node["ingest_token"]},
            json=payload,
        )
        assert accepted.status_code == 200, accepted.text
        assert accepted.json()["status"] == "success"
        assert accepted.json()["data"] == {
            "soil_temperature": 21.75,
            "soil_moisture": 64.2,
            "battery": 91,
        }

        duplicate = client.post(
            f"/iot/ingest/microcontroller/{first_node['did']}",
            headers={"X-IoT-Token": first_node["ingest_token"]},
            json=payload,
        )
        assert duplicate.status_code == 200
        assert duplicate.json()["status"] == "duplicate"

        telemetry = client.get(f"/iot/fincas/{farm_one.id}/telemetry?hours=8784")
        assert telemetry.status_code == 200, telemetry.text
        body = telemetry.json()
        assert body["finca"]["cliente_id"] == farm_one.cliente_id
        assert {row["id"] for row in body["nodes"]} == {
            first_node["id"],
            ttn_node["id"],
        }
        assert len(body["readings"]) == 2

        cross_farm = client.get(
            f"/iot/fincas/{farm_one.id}/telemetry",
            params={"node_id": second_node["id"]},
        )
        assert cross_farm.status_code == 404

        invalid_preset = client.put(
            f"/iot/fincas/{farm_one.id}/dashboard-preset",
            json={
                "widgets": [
                    {
                        "id": "foreign-widget",
                        "node_id": second_node["id"],
                        "variable_id": "battery",
                        "kind": "stat",
                        "order": 1,
                    }
                ],
            },
        )
        assert invalid_preset.status_code == 422

        exported = client.get(f"/iot/fincas/{farm_one.id}/telemetry/export?hours=8784")
        assert exported.status_code == 200, exported.text
        assert first_node["did"] in exported.text
        assert ttn_node["did"] in exported.text
        assert second_node["did"] not in exported.text
        assert farm_one.nombre in exported.text
        assert farm_two.nombre not in exported.text

        conversation = client.post(
            f"/ai/farms/{farm_one.id}/conversations",
            json={},
        )
        assert conversation.status_code == 201, conversation.text
        conversation_id = conversation.json()["id"]
        assert conversation.json()["finca_id"] == farm_one.id
        assert conversation.json()["channel"] == "farm_web"
        assert (
            client.get(
                f"/ai/farms/{farm_two.id}/conversations/{conversation_id}/messages"
            ).status_code
            == 404
        )
        answer = client.post(
            f"/ai/farms/{farm_one.id}/conversations/{conversation_id}/messages",
            json={"text": "Analice la humedad de suelo y ambiente."},
        )
        assert answer.status_code == 200, answer.text
        assert answer.json()["assistant_message"]["status"] == "failed"
        assert answer.json()["assistant_message"]["meta"] == {
            "retryable": True,
            "finca_id": farm_one.id,
            "read_only": True,
        }


def test_rag_documents_are_private_indexed_and_scoped_to_the_agent():
    suffix = uuid.uuid4().hex[:8]
    worker_email = f"rag-worker-{suffix}@example.com"
    with TestClient(app) as client:
        client.headers["X-Forwarded-For"] = "198.51.100.103"
        _login(client, ADMIN_EMAIL, ADMIN_PASSWORD)
        agent = client.get("/ai/agents").json()[0]
        uploaded = client.post(
            "/ai/knowledge-documents",
            data={"agent_id": str(agent["id"]), "title": "Manual de humedad del suelo"},
            files={
                "file": (
                    "manual-suelo.txt",
                    b"La humedad del suelo debe interpretarse junto con temperatura, conductividad y drenaje. "
                    b"Revise la tendencia antes de recomendar riego y valide siempre en campo.",
                    "text/plain",
                ),
            },
        )
        assert uploaded.status_code == 201, uploaded.text
        document = uploaded.json()
        assert document["chunk_count"] >= 1
        assert document["character_count"] > 20

        db = SessionLocal()
        try:
            result = search_knowledge_base(
                db, agent["id"], "humedad conductividad riego", limit=3
            )
            assert result["count"] >= 1
            assert result["results"][0]["document_id"] == document["id"]
        finally:
            db.close()

        created_user = client.post(
            "/usuarios",
            json={
                "nombre": "Operario RAG",
                "correo": worker_email,
                "password": "WorkerRag2026!",
                "rol": "operario",
                "activo": True,
            },
        )
        assert created_user.status_code == 201, created_user.text
        client.post("/auth/logout")
        _login(client, worker_email, "WorkerRag2026!")
        assert client.get("/ai/knowledge-documents").status_code == 403
        assert (
            client.get(f"/ai/knowledge-documents/{document['id']}/download").status_code
            == 403
        )

        client.post("/auth/logout")
        _login(client, ADMIN_EMAIL, ADMIN_PASSWORD)
        paused = client.patch(
            f"/ai/knowledge-documents/{document['id']}",
            json={"active": False},
        )
        assert paused.status_code == 200
        db = SessionLocal()
        try:
            assert (
                search_knowledge_base(
                    db, agent["id"], "humedad conductividad", limit=3
                )["count"]
                == 0
            )
        finally:
            db.close()
        assert (
            client.delete(f"/ai/knowledge-documents/{document['id']}").status_code
            == 204
        )


def test_iot_calculated_widgets_persist_and_respect_farm_scope():
    farm, other_farm = _create_farm_pair()
    suffix = uuid.uuid4().hex[:8]
    with TestClient(app) as client:
        client.headers['X-Forwarded-For'] = '198.51.100.119'
        _login(client, ADMIN_EMAIL, ADMIN_PASSWORD)
        node = client.post('/iot/nodes', json={
            'finca_id': farm.id, 'nombre': 'Estación calculada', 'did': f'CALC-{suffix}',
            'tipo': 'microcontrolador', 'variables_config': {
                'temp': {'source_key': 'temp', 'label': 'Temperatura', 'unit': '°C'},
                'rh': {'source_key': 'rh', 'label': 'Humedad ambiental', 'unit': '%'},
            },
        }).json()
        from datetime import datetime, timezone
        from modules.mod_caficultura.model_iot import IoTReading
        with SessionLocal() as db:
            db.add(IoTReading(node_id=node['id'], recorded_at=datetime.now(timezone.utc), data={'temp': 25, 'rh': 80}))
            db.commit()
        endpoint = f'/iot/fincas/{farm.id}/dashboard-preset'
        assert client.get(endpoint).json()['id'] is None
        widget = {
            'id': 'fahrenheit', 'node_id': node['id'], 'variable_id': 'temp',
            'formula': 'x1 * 9 / 5 + 32', 'inputs': {'x1': {'node_id': node['id'], 'variable_id': 'temp'}},
            'unit': '°F', 'color': '#aa1122', 'kind': 'gauge', 'decimals': 1,
            'minimum': 0, 'maximum': 120, 'hidden': True,
        }
        saved = client.put(endpoint, json={'widgets': [widget]})
        assert saved.status_code == 200, saved.text
        loaded = client.get(endpoint).json()
        assert loaded['id'] is not None
        assert len(loaded['widgets']) == 1
        assert all(loaded['widgets'][0][key] == value for key, value in widget.items())
        telemetry = client.get(f'/iot/fincas/{farm.id}/telemetry?hours=24').json()
        assert telemetry['calculated']['fahrenheit']['points'][0]['value'] == 77
        foreign = client.post('/iot/nodes', json={
            'finca_id': other_farm.id, 'nombre': 'Otra finca', 'did': f'FOREIGN-{suffix}',
            'tipo': 'microcontrolador', 'variables_config': {'rh': {'source_key': 'rh', 'label': 'Humedad'}},
        }).json()
        invalid = {**widget, 'formula': 'x1 + x2', 'inputs': {
            **widget['inputs'], 'x2': {'node_id': foreign['id'], 'variable_id': 'rh'},
        }}
        assert client.put(endpoint, json={'widgets': [invalid]}).status_code == 422
        invalid['inputs']['x2'] = {'node_id': node['id'], 'variable_id': 'missing'}
        assert client.put(endpoint, json={'widgets': [invalid]}).status_code == 422
        invalid['inputs']['x2'] = {'node_id': node['id'], 'variable_id': 'rh'}
        assert client.put(endpoint, json={'widgets': [invalid]}).status_code == 200
        telemetry = client.get(f'/iot/fincas/{farm.id}/telemetry?hours=24').json()
        assert telemetry['calculated']['fahrenheit']['points'][0]['value'] == 105
        assert client.put(endpoint, json={'widgets': [{**widget, 'formula': '__import__("os")'}]}).status_code == 422
        assert client.put(endpoint, json={'widgets': []}).status_code == 200
        empty = client.get(endpoint).json()
        assert empty['id'] is not None and empty['widgets'] == []
        assert client.get(f'/iot/fincas/{farm.id}/telemetry').json()['calculated'] == {}
