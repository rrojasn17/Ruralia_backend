from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

from sqlalchemy.orm import selectinload

from models import (
    AIConversation,
    AIAttachment,
    AIMessage,
    Cliente,
    DocumentoLote,
    Finca,
    OrdenTrabajo,
    ReciboCafe,
    SolicitudVenta,
    SolicitudVentaDocumento,
    SolicitudVentaLinea,
    Usuario,
)
from services.ai_actions import confirm_pending_action, prepare_sale_request_action
from modules.mod_caficultura.ai_agent import TOOLS, ensure_default_agent, process_message
from services.ai_data_views import query_operational_data, search_documents
from services.ai_storage import save_bytes
from services.stable import bootstrap


ADMIN_EMAIL = "ai-tests-admin@example.com"
ADMIN_PASSWORD = "AiTestsAdmin2026!"


def _seed_operational_data(db_session, suffix: str):
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
        codigo=f"CL-RICH-{suffix}",
        nombre_completo=f"Café Comprador Internacional {suffix}",
        tipo_persona="juridica",
        categoria="comercial",
        numero_identificacion=f"RICH-CLIENT-{suffix}",
        activo=True,
    )
    db_session.add(client)
    db_session.flush()
    farm = Finca(
        cliente_id=client.id,
        codigo=f"FN-RICH-{suffix}",
        nombre=f"Finca Las Nubes {suffix}",
        propietario=client.nombre_completo,
        activa=True,
    )
    db_session.add(farm)
    db_session.flush()
    db_session.add_all([
        ReciboCafe(
            numero_recibo=f"RC-RICH-{suffix}-01",
            fecha=date.today(),
            cliente_id=client.id,
            finca_id=farm.id,
            productor_nombre="Productor Uno",
            cajuelas=40,
            cuartillos=0,
            estado="recibido",
        ),
        ReciboCafe(
            numero_recibo=f"RC-RICH-{suffix}-02",
            fecha=date.today() - timedelta(days=4),
            cliente_id=client.id,
            finca_id=farm.id,
            productor_nombre="Productor Dos",
            cajuelas=20,
            cuartillos=0,
            estado="recibido",
        ),
    ])
    lot = OrdenTrabajo(
        codigo_lote=f"LT-RICH-{suffix}",
        proceso="miel",
        estado="en_proceso",
        fecha_inicio=date.today() - timedelta(days=8),
        finca_id=farm.id,
        operario_id=user.id,
        fanegas_estimadas=3,
        created_by_id=user.id,
    )
    db_session.add(lot)
    db_session.flush()
    db_session.add(DocumentoLote(
        ot_id=lot.id,
        titulo="Análisis de calidad",
        tipo="laboratorio",
        descripcion="Resultado físico del lote",
        file_url="https://example.test/analisis-calidad.pdf",
        file_name="analisis-calidad.pdf",
        content_type="application/pdf",
        size_bytes=1024,
        created_by_id=user.id,
    ))
    db_session.commit()
    return user, client, farm, lot


def test_operational_queries_return_mobile_tables_charts_and_documents(db_session):
    _user, _client, _farm, _lot = _seed_operational_data(db_session, "QUERY")

    receipts = query_operational_data(db_session, "receipts", {
        "year": date.today().year,
        "search": "RICH-QUERY",
        "limit": 20,
        "visualization": "bar",
        "group_by": "month",
    })
    assert receipts["ok"] is True
    assert receipts["total_matches"] == 2
    assert [item["type"] for item in receipts["components"]] == ["data_table", "chart"]
    assert receipts["components"][0]["rows"][0]["path"].startswith("/recibos/")

    lots = query_operational_data(db_session, "active_lots", {
        "search": "LT-RICH-QUERY",
        "limit": 20,
        "include_elapsed": True,
        "visualization": "bar",
        "group_by": "elapsed",
    })
    assert lots["ok"] is True
    assert lots["components"][0]["rows"][0]["elapsed_days"] == 8
    assert lots["components"][1]["value_format"] == "duration_days"

    documents = search_documents(db_session, "calidad", None, 10)
    assert documents["ok"] is True
    assert documents["total_matches"] == 1
    assert documents["components"][0]["items"][0]["file_name"] == "analisis-calidad.pdf"

    tool_names = {item["name"] for item in TOOLS}
    assert {"query_operational_data", "search_documents", "prepare_sale_request"} <= tool_names
    assert not any("delete" in name or "remove" in name for name in tool_names)


def test_liquidated_receipt_intent_is_deterministic_and_tolerates_typo(db_session):
    user, _client, _farm, _lot = _seed_operational_data(db_session, "ROUTING")
    receipt = db_session.query(ReciboCafe).filter(ReciboCafe.numero_recibo == "RC-RICH-ROUTING-01").one()
    receipt.liquidado = True
    receipt.liquidado_at = datetime.now(timezone.utc)
    receipt.liquidacion_nota = "Transferencia verificada"
    receipt.liquidacion_numero_transferencia = "TRX-ROUTING-001"
    receipt.liquidacion_monto = 210_000
    receipt.liquidacion_comprobante_storage_key = "tests/comprobante-routing.pdf"
    receipt.liquidacion_comprobante_nombre = "comprobante-routing.pdf"
    receipt.liquidacion_comprobante_tipo = "application/pdf"
    receipt.liquidacion_comprobante_tamano = 1200

    agent = ensure_default_agent(db_session, user.id)
    conversation = AIConversation(
        usuario_id=user.id,
        agent_id=agent.id,
        title="Enrutamiento de liquidaciones",
        channel="web",
        status="active",
    )
    db_session.add(conversation)
    db_session.flush()
    message = AIMessage(
        conversation_id=conversation.id,
        role="user",
        content="¿Cuáles recibos se han loquidado?",
        status="completed",
    )
    db_session.add(message)
    db_session.commit()

    assistant = process_message(db_session, conversation, user, message)
    assert "recibo" in assistant.content.lower()
    assert "liquidado" in assistant.content.lower()
    assert assistant.meta["tool_names"] == ["query_operational_data"]
    assert assistant.meta["routing"] == {
        "mode": "deterministic",
        "intent": "receipt_payment_status",
        "dataset": "receipts",
        "status": "liquidado",
    }
    assert assistant.meta["metric_cards"] == []
    table = assistant.meta["dynamic_components"][0]
    assert table["type"] == "data_table"
    target = next(row for row in table["rows"] if row["receipt"] == "RC-RICH-ROUTING-01")
    assert target["payment"] == "Liquidado"
    assert target["liquidation_amount"] == 210_000
    assert target["transfer"] == "TRX-ROUTING-001"

    typo_filter = query_operational_data(db_session, "receipts", {"status": "loquidados", "limit": 20})
    assert any(row["receipt"] == "RC-RICH-ROUTING-01" for row in typo_filter["components"][0]["rows"])


def test_sale_request_is_guided_confirmed_and_idempotent(db_session):
    user, client, _farm, _lot = _seed_operational_data(db_session, "SALE")
    agent = ensure_default_agent(db_session, user.id)
    conversation = AIConversation(
        usuario_id=user.id,
        agent_id=agent.id,
        title="Venta guiada",
        channel="web",
        status="active",
    )
    db_session.add(conversation)
    db_session.flush()
    message = AIMessage(
        conversation_id=conversation.id,
        role="user",
        content="Quiero crear una solicitud de venta",
        status="completed",
    )
    db_session.add(message)
    db_session.flush()
    stored = save_bytes(
        b"%PDF-1.4\n% orden de compra de prueba\n",
        user.id,
        "orden-compra-rich.pdf",
        "application/pdf",
    )
    attachment = AIAttachment(message_id=message.id, usuario_id=user.id, **stored)
    db_session.add(attachment)
    db_session.commit()

    incomplete = prepare_sale_request_action(db_session, conversation, user, message, {
        "client_name": client.nombre_completo,
        "lines": None,
    })
    assert incomplete["needs_clarification"] is True
    assert incomplete["components"][0]["type"] == "guided_flow"

    prepared = prepare_sale_request_action(db_session, conversation, user, message, {
        "client_name": client.nombre_completo,
        "currency": "USD",
        "observations": "Entrega en dos tractos",
        "purchase_order_attachment_id": attachment.id,
        "purchase_order_title": "Orden de compra del cliente",
        "lines": [{
            "description": "Café miel exportación",
            "preferred_process": "miel",
            "quantity_quintals": 12.5,
            "target_price": 275,
            "currency": "USD",
            "observations": None,
        }],
    })
    db_session.commit()
    assert prepared["requires_confirmation"] is True
    assert db_session.query(SolicitudVenta).count() == 0

    action = confirm_pending_action(
        db_session,
        prepared["action_public_id"],
        user,
        prepared["action_version"],
    )
    assert action.status == "executed"
    assert action.result["entity"] == "sale_request"
    assert db_session.query(SolicitudVenta).count() == 1
    assert db_session.query(SolicitudVentaLinea).count() == 1
    assert db_session.query(SolicitudVentaDocumento).count() == 1
    assert action.result["purchase_order_attached"] is True

    repeated = confirm_pending_action(
        db_session,
        prepared["action_public_id"],
        user,
        prepared["action_version"],
    )
    assert repeated.id == action.id
    assert db_session.query(SolicitudVenta).count() == 1


def test_agent_persists_structured_components_from_tool_results(db_session, monkeypatch):
    user, _client, _farm, _lot = _seed_operational_data(db_session, "AGENT")
    agent = ensure_default_agent(db_session, user.id)
    conversation = AIConversation(
        usuario_id=user.id,
        agent_id=agent.id,
        title="Datos visuales",
        channel="web",
        status="active",
    )
    db_session.add(conversation)
    db_session.flush()
    message = AIMessage(
        conversation_id=conversation.id,
        role="user",
        content="Lista los lotes activos con el tiempo transcurrido",
        status="completed",
    )
    db_session.add(message)
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
    message = next(item for item in conversation.messages if item.id == message.id)

    class FakeResponses:
        def __init__(self):
            self.calls = 0

        def create(self, **_kwargs):
            self.calls += 1
            if self.calls == 1:
                return SimpleNamespace(output=[SimpleNamespace(
                    type="function_call",
                    name="query_operational_data",
                    arguments=json.dumps({
                        "dataset": "active_lots",
                        "search": None,
                        "status": None,
                        "client_name": None,
                        "farm_name": None,
                        "year": None,
                        "start_date": None,
                        "end_date": None,
                        "limit": 20,
                        "include_elapsed": True,
                        "visualization": "bar",
                        "group_by": "elapsed",
                    }),
                    call_id="call-active-lots",
                )], output_text="")
            return SimpleNamespace(output=[], output_text="Encontré los lotes activos y calculé su antigüedad.")

    fake = FakeResponses()
    monkeypatch.setattr("modules.mod_caficultura.ai_agent.openai_is_configured", lambda: True)
    monkeypatch.setattr("modules.mod_caficultura.ai_agent._openai_client", lambda: SimpleNamespace(responses=fake))

    assistant = process_message(db_session, conversation, user, message)
    assert assistant.meta["tool_names"] == ["query_operational_data"]
    assert [item["type"] for item in assistant.meta["dynamic_components"]] == ["data_table", "chart"]
    assert fake.calls == 2
