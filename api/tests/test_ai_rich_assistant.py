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
from services.ai_actions import confirm_pending_action, prepare_farm_activity_action, prepare_sale_request_action
from modules.mod_caficultura.ai_agent import TOOLS, ensure_default_agent, process_message
from modules.mod_caficultura.ai_metrics import days_since_fertilization, farm_sensor_analysis
from modules.mod_caficultura.model_iot import IoTNode, IoTReading
from modules.mod_caficultura.models import ActividadFinca, RegistroFinca
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


def test_farm_context_metrics_use_fertilization_and_continuous_sensor_readings(db_session):
    _user, _client, farm, _lot = _seed_operational_data(db_session, "FARM-CONTEXT")
    last_fertilization = date.today() - timedelta(days=46)
    activity = ActividadFinca(nombre="Fertilización monitor FARM-CONTEXT", tipo="fertilizacion", activa=True)
    db_session.add(activity)
    db_session.flush()
    db_session.add(RegistroFinca(
        fecha=last_fertilization,
        semana_inicio=last_fertilization,
        semana_fin=last_fertilization,
        finca_id=farm.id,
        actividad_id=activity.id,
    ))
    node = IoTNode(
        finca_id=farm.id,
        nombre="Sensor ambiental FARM-CONTEXT",
        did="FARM-CONTEXT-SENSOR",
        tipo="microcontrolador",
        activo=True,
    )
    db_session.add(node)
    db_session.flush()
    now = datetime.now(timezone.utc)
    db_session.add_all([
        IoTReading(
            node_id=node.id,
            recorded_at=now - timedelta(hours=15 - index),
            source="manual",
            source_event_id=f"farm-context-{index}",
            data={"humidity": 90},
        )
        for index in range(16)
    ])
    db_session.commit()

    fertilization = days_since_fertilization(db_session, {"farm_name": farm.nombre})
    humidity = farm_sensor_analysis(db_session, {"farm_name": farm.nombre, "humidity_threshold": 85})
    metric_tool = next(item for item in TOOLS if item["name"] == "get_business_metric")

    assert fertilization["ok"] is True
    assert fertilization["days_without_fertilization"] == 46
    assert humidity["ok"] is True
    assert humidity["sample_count"] == 16
    assert humidity["current_consecutive_high_humidity_hours"] >= 14.99
    assert {"days_since_fertilization", "farm_sensor_analysis"} <= set(
        metric_tool["parameters"]["properties"]["metric"]["enum"]
    )


def test_ai_farm_activity_requires_confirmation_and_records_operational_week(db_session):
    user, _client, farm, _lot = _seed_operational_data(db_session, "ACTIVITY-ACTION")
    farm.gestion_fincas_habilitada = True
    activity = ActividadFinca(nombre="Fertilización IA ACTIVITY-ACTION", tipo="fertilizacion", activa=True)
    db_session.add(activity)
    db_session.flush()
    agent = ensure_default_agent(db_session, user.id)
    conversation = AIConversation(
        usuario_id=user.id,
        agent_id=agent.id,
        title="Registro de finca",
        channel="web",
        status="active",
    )
    db_session.add(conversation)
    db_session.flush()
    message = AIMessage(
        conversation_id=conversation.id,
        role="user",
        content="Registrar fertilización en la finca",
        status="completed",
    )
    db_session.add(message)
    db_session.commit()

    prepared = prepare_farm_activity_action(
        db_session,
        conversation,
        user,
        message,
        {
            "farm_name": farm.nombre,
            "activity_name": activity.nombre,
            "date": date.today().isoformat(),
            "description": "Fertilización del lote norte",
            "observations": "Aplicación registrada por voz",
        },
    )
    assert prepared["requires_confirmation"] is True
    assert db_session.query(RegistroFinca).filter(RegistroFinca.finca_id == farm.id).count() == 0

    action = confirm_pending_action(
        db_session,
        prepared["action_public_id"],
        user,
        prepared["action_version"],
    )
    record = db_session.query(RegistroFinca).filter(RegistroFinca.finca_id == farm.id).one()
    assert action.status == "executed"
    assert record.actividad_id == activity.id
    assert record.descripcion == "Fertilización del lote norte"
    assert record.semana_inicio.weekday() == 0
    assert record.semana_fin.weekday() == 5


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

    documents = search_documents(db_session, _lot.codigo_lote, "laboratorio", 10)
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


def test_monitoring_alert_rearms_only_after_condition_clears(db_session, monkeypatch):
    from modules.mod_caficultura import ai_automation_engine as engine
    from modules.mod_caficultura.model_ai_consulting import AIAutomation
    from modules.mod_caficultura.model_notifications import NotificationEvent

    user, _, farm, _ = _seed_operational_data(db_session, 'MONITOR-REARM')
    now = datetime.now(timezone.utc)
    rule = AIAutomation(usuario_id=user.id, name='Fertilización pendiente', metric_key='days_since_fertilization',
                        parameters={'farm_name': farm.nombre}, condition_operator='gt', threshold=45,
                        recurrence='interval', interval_minutes=15, next_run_at=now, channels=['email'],
                        email_recipients=[user.correo], ai_enhance=False)
    db_session.add(rule)
    db_session.commit()
    sent = []
    monkeypatch.setattr(engine, '_deliver', lambda row, message: sent.append(message) or [{'status': 'sent'}])
    for index, value in enumerate([46, 47, 0, 46]):
        monkeypatch.setattr(engine, 'execute_metric', lambda *args, value=value: {
            'ok': True, 'primary_value': value, 'summary': f'{value} días sin fertilizar', 'unit': 'days'
        })
        engine.process_due_automations(db_session, now + timedelta(minutes=15 * index), automation_ids={rule.id})
        db_session.commit()
        assert len(sent) == [1, 1, 1, 2][index]
    assert db_session.query(NotificationEvent).filter_by(entity_type='ai_automation', entity_id=str(rule.id)).count() == 2


def test_monitoring_natural_language_examples():
    from modules.mod_caficultura.ai_automation_engine import parse_monitoring_instruction
    days = parse_monitoring_instruction('Avisarme si pasan más de 45 días sin fertilizar.')
    assert days['metric_key'] == 'days_since_fertilization'
    assert days['threshold'] == 45
    humidity = parse_monitoring_instruction('15 horas seguidas de humedad mayor al 85 % representan riesgo de roya')
    assert humidity['metric_key'] == 'farm_sensor_analysis'
    assert humidity['threshold'] == 15
    assert humidity['parameters']['humidity_threshold'] == 85
    assert parse_monitoring_instruction('Una condición sin datos suficientes') is None


def test_in_app_monitoring_does_not_require_external_recipients():
    from modules.mod_caficultura.schema_ai_consulting import AIAutomationCreate
    from modules.mod_caficultura.ai_automation_engine import _deliver
    rule = AIAutomationCreate(name='Monitorear finca', metric_key='inventory_status',
                              next_run_at=datetime.now(timezone.utc), channels=['in_app'])
    assert rule.email_recipients == []
    assert _deliver(rule, 'Aviso interno') == [{'channel': 'in_app', 'status': 'sent'}]
