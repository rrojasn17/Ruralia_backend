from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import json

import pytest

from services.stable import bootstrap
from core.models import Usuario
from modules.mod_caficultura.models import Cliente, Finca
from modules.mod_caficultura.ai_farm_agent import analyze_farm_telemetry, _range
from modules.mod_caficultura.ai_sensor_analysis import (
    analyze_sensor_chat, chart_points, period_from_text, sensor_context,
)
from modules.mod_caficultura.ai_agent import ensure_default_agent, process_message
from modules.mod_caficultura.model_ai_consulting import AIConversation, AIMessage
from modules.mod_caficultura.model_iot import IoTNode, IoTReading


def seed_sensor(db, suffix):
    bootstrap(db, 'ai-tests-admin@example.com', 'AiTestsAdmin2026!', 'AI Tests Admin', 30,
              sync_admin_password=False, seed_demo_data=False)
    user = db.query(Usuario).filter(Usuario.correo == 'ai-tests-admin@example.com').one()
    client = Cliente(codigo=f'CL-{suffix}', nombre_completo=f'Cliente {suffix}', numero_identificacion=suffix, activo=True)
    db.add(client)
    db.flush()
    farm = Finca(cliente_id=client.id, codigo=f'FN-{suffix}', nombre=f'Finca {suffix}', propietario='Prueba', activa=True)
    db.add(farm)
    db.flush()
    node = IoTNode(finca_id=farm.id, nombre='Suelo1', did=suffix, tipo='microcontrolador', activo=True,
        variables_config={'ec': {'label': 'Conductividad', 'unit': 'µS/cm', 'source_key': 'conduct_SOIL'},
                          'water': {'label': 'Humedad del suelo', 'unit': '%', 'source_key': 'water_SOIL'}})
    db.add(node)
    db.flush()
    now = datetime.now(timezone.utc)
    for days, ec in [(29, 10), (20, 30), (2, 19), (0.5, 17)]:
        db.add(IoTReading(node_id=node.id, source='manual', recorded_at=now - timedelta(days=days),
                         data={'ec': ec, 'water': 27, 'invalid_bool': True}))
    db.commit()
    return user, farm, node


def test_month_queries_real_history_and_has_timestamped_extrema(db_session):
    _, farm, node = seed_sensor(db_session, 'HISTORY-MONTH')
    unrelated = IoTNode(finca_id=farm.id, nombre='Ambiente', did='UNRELATED-HISTORY', tipo='microcontrolador', activo=True,
                        variables_config={'temp': {'label': 'Temperatura', 'unit': '°C'}})
    db_session.add(unrelated)
    db_session.flush()
    db_session.add(IoTReading(node_id=unrelated.id, source='manual', recorded_at=datetime.now(timezone.utc), data={'temp': 25}))
    db_session.flush()
    # Even an erroneous default from the model cannot turn "último mes" into 24 hours.
    result = analyze_sensor_chat(db_session, {'farm_name': farm.nombre, 'hours': 24},
                                 'Analiza la conductividad del suelo en el último mes')
    metric = result['variables'][0]
    assert result['period']['hours'] == 720
    assert result['reading_rows'] == 4 and result['queried_reading_rows'] == 5
    assert result['analyzed_sensor_count'] == 1 and result['sensor_count'] == 2
    assert metric['count'] == 4 and metric['first'] == 10 and metric['latest'] == 17
    assert metric['change'] == 7  # Peak 30 to latest 17 must not be called a net decrease.
    assert metric['change_percent'] == 70 and metric['unit'] == 'µS/cm'
    assert metric['minimum_at'] == metric['first_at']
    assert metric['maximum_at'] < metric['latest_at']
    assert metric['days_with_readings'] == 4
    assert metric['largest_gap_hours'] == 432
    assert 'humedad relativa' not in result['summary']
    assert result['components'][0]['type'] == 'prompt_suggestions'
    day = analyze_sensor_chat(db_session, {'farm_name': farm.nombre, 'hours': 24}, 'conductividad últimas 24 horas')
    assert day['variables'][0]['count'] == 1


def test_followup_chart_keeps_context_and_exact_dates(db_session):
    _, farm, node = seed_sensor(db_session, 'HISTORY-CHART')
    first = analyze_sensor_chat(db_session, {'farm_name': farm.nombre, 'node_name': 'Suelo1'}, 'conductividad del último mes')
    suggestion = first['components'][0]['items'][0]['prompt']
    other = IoTNode(finca_id=farm.id, nombre='Suelo2', did='OTHER-HISTORY-CHART', tipo='microcontrolador', activo=True, variables_config=node.variables_config)
    db_session.add(other)
    db_session.flush()
    db_session.add(IoTReading(node_id=other.id, source='manual', recorded_at=datetime.now(timezone.utc) - timedelta(hours=1), data={'ec': 999}))
    db_session.flush()
    changed_context = {**first['sensor_context'], 'node_id': other.id, 'node_name': 'Suelo2'}
    chart = analyze_sensor_chat(db_session, {'hours': 24}, suggestion, changed_context)
    assert chart['sensor_context']['node_id'] == node.id
    assert chart['period'] == first['period']
    assert chart['components'][0]['type'] == 'sensor_chart'
    assert len(chart['components'][0]['points']) == 4
    assert chart['components'][0]['points'][0]['value'] == 10
    follow = analyze_sensor_chat(db_session, {'hours': 24}, '¿Pero ha subido o no?', first['sensor_context'])
    assert follow['period']['hours'] == 720
    assert [item['variable_id'] for item in follow['variables']] == ['ec']


def test_date_only_uses_costa_rica_midnight_and_invalid_ranges_fail():
    start, end = _range({'start_date': '2026-10-01', 'end_date': '2026-10-01'})
    assert start == datetime(2026, 10, 1, 6, tzinfo=timezone.utc)
    assert end == datetime(2026, 10, 2, 5, 59, 59, 999999, tzinfo=timezone.utc)
    with pytest.raises(ValueError):
        _range({'hours': 9000})
    with pytest.raises(ValueError):
        _range({'start_date': '2026-10-01'})
    last_month = period_from_text('mes pasado', datetime(2026, 10, 1, 5, tzinfo=timezone.utc))
    # It is still September locally, so the preceding calendar month is August.
    assert last_month['start_date'].startswith('2026-08-01T00:00:00-06:00')


def test_foreign_sensor_and_empty_variable_are_not_silently_substituted(db_session):
    _, farm, node = seed_sensor(db_session, 'HISTORY-SCOPE')
    result = analyze_farm_telemetry(db_session, farm.id, {'node_id': node.id + 100000})
    assert result['ok'] is False
    result = analyze_sensor_chat(db_session, {'farm_name': farm.nombre}, 'temperatura del último mes')
    assert result['ok'] is False
    result = analyze_farm_telemetry(db_session, farm.id, {'hours': 720, 'variable_ids': ['invalid_bool']})
    assert result['variables'] == [] and result['message']


def test_chart_reduction_preserves_peaks_order_endpoints():
    points = [{'timestamp': index, 'value': 0} for index in range(20001)]
    points[101]['value'] = 999
    points[3456]['value'] = -999
    reduced = chart_points(points)
    assert len(reduced) <= 1000
    assert reduced[0] == points[0] and reduced[-1] == points[-1]
    assert points[101] in reduced and points[3456] in reduced
    assert sorted(reduced, key=lambda p: p['timestamp']) == reduced


def test_general_chat_forces_fresh_month_query_on_contextual_followup(db_session, monkeypatch):
    user, farm, node = seed_sensor(db_session, 'HISTORY-CHAT')
    agent = ensure_default_agent(db_session, user.id)
    conversation = AIConversation(usuario_id=user.id, agent_id=agent.id, title='Sensores', channel='web')
    db_session.add(conversation)
    db_session.flush()
    db_session.add(AIMessage(conversation_id=conversation.id, role='assistant', content='Análisis de ayer', status='completed',
        meta={'sensor_context': {'farm_name': farm.nombre, 'node_id': node.id, 'variable_ids': ['ec'], 'hours': 24}}))
    message = AIMessage(conversation_id=conversation.id, role='user', content='analiza el último mes', status='completed')
    db_session.add(message)
    db_session.commit()
    calls = []

    def respond(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            assert kwargs['tool_choice'] == {'type': 'function', 'name': 'get_business_metric'}
            return SimpleNamespace(output=[SimpleNamespace(type='function_call', name='get_business_metric',
                arguments=json.dumps({'metric': 'farm_sensor_analysis', 'hours': 24}), call_id='history')], output_text='')
        data = json.loads(kwargs['input'][-1]['output'])
        assert data['period']['hours'] == 720 and data['variables'][0]['change'] == 7
        return SimpleNamespace(output=[], output_text='Subió de 10 a 17 durante las fechas disponibles.')

    monkeypatch.setattr('modules.mod_caficultura.ai_agent.openai_is_configured', lambda: True)
    monkeypatch.setattr('modules.mod_caficultura.ai_agent._openai_client', lambda: SimpleNamespace(responses=SimpleNamespace(create=respond)))
    answer = process_message(db_session, conversation, user, message)
    assert answer.meta['sensor_context']['hours'] == 720
    assert answer.meta['dynamic_components'][0]['type'] == 'prompt_suggestions'
    assert answer.meta['metric_cards'][0]['reading_rows'] == 4


def test_existing_conversation_preserves_soil_focus(db_session):
    _, farm, node = seed_sensor(db_session, 'HISTORY-LEGACY')
    conversation = SimpleNamespace(messages=[
        SimpleNamespace(role='user', content='revisa conductividad del sensor de suelo', meta={}),
        SimpleNamespace(role='assistant', meta={'metric_cards': [{'metric': 'farm_sensor_analysis',
            'farm': {'id': farm.id, 'name': farm.nombre}, 'period': {'from': '2026-10-01T00:00:00Z', 'to': '2026-10-02T00:00:00Z'}}]}),
    ])
    result = analyze_sensor_chat(db_session, {}, 'analiza el último mes', sensor_context(conversation))
    assert result['period']['hours'] == 720
    assert [v['variable_id'] for v in result['variables']] == ['ec']


@pytest.mark.parametrize('variable', ['conductividad', 'temperatura'])
def test_farm_chat_returns_interactive_chart_and_fresh_temporal_evidence(db_session, monkeypatch, variable):
    from modules.mod_caficultura.ai_farm_agent import process_farm_message
    user, farm, node = seed_sensor(db_session, f'HISTORY-FARM-CHAT-{variable}')
    agent = ensure_default_agent(db_session, user.id)
    conversation = AIConversation(usuario_id=user.id, agent_id=agent.id, finca_id=farm.id, title='Suelo', channel='farm_web')
    db_session.add(conversation)
    db_session.flush()
    message = AIMessage(conversation_id=conversation.id, role='user', content=f'Gráfico de {variable} del último mes', status='completed')
    db_session.add(message)
    db_session.commit()
    calls = []

    def respond(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            assert kwargs['tool_choice'] == {'type': 'function', 'name': 'analyze_farm_telemetry'}
            return SimpleNamespace(output=[SimpleNamespace(type='function_call', name='analyze_farm_telemetry',
                arguments=json.dumps({'hours': 24, 'node_id': node.id}), call_id='farm-history')], output_text='')
        data = json.loads(kwargs['input'][-1]['output'])
        if variable == 'temperatura':
            assert data['ok'] is False and 'No se encontró esa variable' in data['message']
            return SimpleNamespace(output=[], output_text='Este sensor no tiene temperatura registrada.')
        assert data['period']['hours'] == 720 and data['variables'][0]['change'] == 7
        assert data['components'][0]['type'] == 'sensor_chart'
        return SimpleNamespace(output=[], output_text='La conductividad subió de 10 a 17 µS/cm; el gráfico muestra sus fechas.')

    monkeypatch.setattr('modules.mod_caficultura.ai_farm_agent.openai_is_configured', lambda: True)
    monkeypatch.setattr('modules.mod_caficultura.ai_farm_agent._openai_client', lambda: SimpleNamespace(responses=SimpleNamespace(create=respond)))
    answer = process_farm_message(db_session, conversation, user, message)
    if variable == 'conductividad':
        assert answer.meta['sensor_context']['farm_id'] == farm.id
        assert answer.meta['dynamic_components'][0]['sample_count'] == 4
    else:
        assert answer.meta['dynamic_components'] == []
    assert answer.meta['read_only'] is True
