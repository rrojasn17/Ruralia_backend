from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

from fastapi.testclient import TestClient
from sqlalchemy.orm import selectinload

from main import app
from models import AIConversation, AIMessage, Cliente, Finca, ReciboCafe, Usuario
from services.ai_actions import confirm_pending_action, prepare_receipt_action
from modules.mod_caficultura.ai_agent import ensure_default_agent, process_message
from services.ai_metrics import execute_metric
from services.stable import bootstrap


ADMIN_EMAIL = "ai-tests-admin@example.com"
ADMIN_PASSWORD = "AiTestsAdmin2026!"


def _login(client: TestClient, correo: str, password: str) -> None:
    response = client.post("/auth/login", json={"correo": correo, "password": password})
    assert response.status_code == 200, response.text


def test_ai_routes_are_manager_only_and_chat_fails_safely_without_provider():
    with TestClient(app) as client:
        _login(client, ADMIN_EMAIL, ADMIN_PASSWORD)

        config = client.get("/ai/config")
        assert config.status_code == 200
        assert config.json()["openai_configured"] is False

        worker_email = "ai-tests-worker@example.com"
        created = client.post(
            "/usuarios",
            json={
                "nombre": "Trabajador de pruebas",
                "correo": worker_email,
                "password": "WorkerTests2026!",
                "rol": "operario",
                "activo": True,
            },
        )
        assert created.status_code in {201, 409}, created.text
        client.post("/auth/logout")
        _login(client, worker_email, "WorkerTests2026!")
        assert client.get("/ai/config").status_code == 403

        client.post("/auth/logout")
        _login(client, ADMIN_EMAIL, ADMIN_PASSWORD)
        agents = client.get("/ai/agents")
        assert agents.status_code == 200
        assert len(agents.json()) >= 1

        conversation = client.post("/ai/conversations", json={"title": "Prueba de integración"})
        assert conversation.status_code == 201, conversation.text
        conversation_id = conversation.json()["id"]
        sent = client.post(
            f"/ai/conversations/{conversation_id}/messages",
            data={"text": "¿Cuánto café ha llegado este año a la planta?"},
        )
        assert sent.status_code == 200, sent.text
        assert sent.json()["assistant_message"]["status"] == "failed"
        history = client.get(f"/ai/conversations/{conversation_id}/messages")
        assert history.status_code == 200
        assert len(history.json()) == 2

        automation = client.post(
            "/ai/automations",
            json={
                "name": "Resumen ejecutivo diario",
                "metric_key": "business_overview",
                "parameters": {},
                "condition_operator": "always",
                "recurrence": "daily",
                "timezone": "America/Costa_Rica",
                "next_run_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
                "channels": ["email"],
                "email_recipients": [ADMIN_EMAIL],
                "telegram_chat_ids": [],
                "ai_enhance": False,
                "enabled": True,
            },
        )
        assert automation.status_code == 201, automation.text
        assert automation.json()["metric_key"] == "business_overview"


def test_receipt_action_requires_clarification_confirmation_and_is_idempotent(db_session):
    bootstrap(
        db_session,
        ADMIN_EMAIL,
        ADMIN_PASSWORD,
        "AI Tests Admin",
        30,
        sync_admin_password=False,
        seed_demo_data=False,
    )
    user = db_session.query(Usuario).filter(Usuario.correo == ADMIN_EMAIL).one()
    client = Cliente(
        codigo="CL-IA-TEST",
        nombre_completo="Productora El Roble",
        tipo_persona="personal",
        categoria="agro",
        numero_identificacion="IA-TEST-001",
        provincia="Alajuela",
        canton="Naranjo",
        distrito="San Juan",
        activo=True,
    )
    db_session.add(client)
    db_session.flush()
    farm = Finca(
        cliente_id=client.id,
        codigo="FN-IA-TEST",
        nombre="Finca El Roble",
        propietario=client.nombre_completo,
        provincia="Alajuela",
        canton="Naranjo",
        distrito="San Juan",
        activa=True,
    )
    db_session.add(farm)
    db_session.flush()
    agent = ensure_default_agent(db_session, user.id)
    conversation = AIConversation(
        usuario_id=user.id,
        agent_id=agent.id,
        title="Acciones IA",
        channel="web",
        status="active",
    )
    db_session.add(conversation)
    db_session.flush()
    message = AIMessage(
        conversation_id=conversation.id,
        role="user",
        content="Cree un recibo",
        status="completed",
    )
    db_session.add(message)
    db_session.commit()

    incomplete = prepare_receipt_action(
        db_session,
        conversation,
        user,
        message,
        {"client_name": "Productora El Roble"},
    )
    assert incomplete["needs_clarification"] is True

    prepared = prepare_receipt_action(
        db_session,
        conversation,
        user,
        message,
        {
            "client_name": "Productora El Roble",
            "farm_name": "Finca El Roble",
            "date": date.today().isoformat(),
            "cajuelas": 40,
            "cuartillos": 2,
            "porcentaje_flote": 3.5,
            "porcentaje_verde": 1.2,
            "peso_promedio_cajuela": 12.4,
            "precio_fanega": 105_000,
            "delivered_by": "María Roble",
        },
    )
    db_session.commit()
    assert prepared["requires_confirmation"] is True

    action = confirm_pending_action(
        db_session,
        prepared["action_public_id"],
        user,
        prepared["action_version"],
    )
    assert action.status == "executed"
    assert db_session.query(ReciboCafe).count() == 1

    repeated = confirm_pending_action(
        db_session,
        prepared["action_public_id"],
        user,
        prepared["action_version"],
    )
    assert repeated.id == action.id
    assert db_session.query(ReciboCafe).count() == 1

    received = execute_metric(db_session, "coffee_received", {"year": date.today().year})
    patio = execute_metric(db_session, "coffee_in_patio", {})
    activity = execute_metric(db_session, "farm_activity", {"farm_name": "Finca El Roble"})
    inventory = execute_metric(db_session, "inventory_status", {})
    sales = execute_metric(db_session, "sales_summary", {"year": date.today().year})
    assert received["fanegas"] == 2.025
    assert patio["fanegas"] == 2.025
    assert activity["records"] == 0
    assert inventory["low_stock_count"] == 0
    assert sales["sales"] == 0


def test_agent_uses_read_tools_and_returns_verified_metric_cards(db_session, monkeypatch):
    bootstrap(
        db_session,
        ADMIN_EMAIL,
        ADMIN_PASSWORD,
        "AI Tests Admin",
        30,
        sync_admin_password=False,
        seed_demo_data=False,
    )
    user = db_session.query(Usuario).filter(Usuario.correo == ADMIN_EMAIL).one()
    agent = ensure_default_agent(db_session, user.id)
    conversation = AIConversation(
        usuario_id=user.id,
        agent_id=agent.id,
        title="Herramientas IA",
        channel="web",
        status="active",
    )
    db_session.add(conversation)
    db_session.flush()
    user_message = AIMessage(
        conversation_id=conversation.id,
        role="user",
        content="Dame un resumen del negocio",
        status="completed",
    )
    db_session.add(user_message)
    db_session.commit()
    conversation = (
        db_session.query(AIConversation)
        .populate_existing()
        .options(
            selectinload(AIConversation.agent),
            selectinload(AIConversation.messages).selectinload(AIMessage.attachments),
        )
        .filter(AIConversation.id == conversation.id)
        .one()
    )
    user_message = next(item for item in conversation.messages if item.id == user_message.id)

    class FakeResponses:
        def __init__(self):
            self.calls = 0

        def create(self, **_kwargs):
            self.calls += 1
            if self.calls == 1:
                tool_call = SimpleNamespace(
                    type="function_call",
                    name="get_business_metric",
                    arguments=json.dumps(
                        {
                            "metric": "business_overview",
                            "farm_name": None,
                            "worker_name": None,
                            "client_name": None,
                            "lot_code": None,
                            "year": None,
                            "start_date": None,
                            "end_date": None,
                            "limit": None,
                        }
                    ),
                    call_id="call-business-overview",
                )
                return SimpleNamespace(output=[tool_call], output_text="")
            return SimpleNamespace(output=[], output_text="Resumen verificado con datos de NAVIA.")

    fake_responses = FakeResponses()
    monkeypatch.setattr("modules.mod_caficultura.ai_agent.openai_is_configured", lambda: True)
    monkeypatch.setattr(
        "modules.mod_caficultura.ai_agent._openai_client",
        lambda: SimpleNamespace(responses=fake_responses),
    )

    assistant = process_message(db_session, conversation, user, user_message, request_id="test-tool-call")
    db_session.commit()
    assert assistant.content == "Resumen verificado con datos de NAVIA."
    assert assistant.meta["tool_names"] == ["get_business_metric"]
    assert assistant.meta["metric_cards"][0]["metric"] == "business_overview"
    assert fake_responses.calls == 2


def test_telegram_webhook_requires_secret_and_valid_update(monkeypatch):
    secret = "telegram-tests-secret-with-32-characters"
    monkeypatch.setattr("modules.mod_caficultura.ai_consulting_router.telegram_is_configured", lambda: True)
    monkeypatch.setattr("modules.mod_caficultura.ai_consulting_router.TELEGRAM_WEBHOOK_SECRET", secret)
    monkeypatch.setattr("modules.mod_caficultura.ai_consulting_router.process_telegram_update", lambda _update: None)

    with TestClient(app) as client:
        rejected = client.post("/ai/telegram/webhook", json={"update_id": 1001})
        assert rejected.status_code == 403
        invalid = client.post(
            "/ai/telegram/webhook",
            headers={"X-Telegram-Bot-Api-Secret-Token": secret},
            json={"message": {}},
        )
        assert invalid.status_code == 422
        accepted = client.post(
            "/ai/telegram/webhook",
            headers={"X-Telegram-Bot-Api-Secret-Token": secret},
            json={"update_id": 1002, "message": {"chat": {"id": 10}, "text": "/start"}},
        )
        assert accepted.status_code == 200
        assert accepted.json() == {"ok": True}
