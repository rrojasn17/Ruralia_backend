"""Ground sensor follow-ups in fresh queries and explicit temporal evidence."""
from __future__ import annotations

import re
import unicodedata
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from fastapi.encoders import jsonable_encoder

TZ = ZoneInfo("America/Costa_Rica")

SENSOR_ANALYSIS_GUIDANCE = """
Para sensores consulta de nuevo farm_sensor_analysis (o analyze_farm_telemetry en una finca)
en cada seguimiento sobre valores, evolución o un período nuevo. No reutilices cifras de otra ventana.
Conserva la finca, el sensor y las variables del contexto de telemetría, salvo cambios del usuario.
Un último mes son 30 días móviles; mes pasado es el mes calendario anterior, en Costa Rica.
Responde primero si subió, bajó o quedó igual comparando first/first_at y latest/latest_at;
un máximo y una última lectura no prueban una tendencia. Distingue cambio neto de slope_per_day.
Con menos de dos fechas distintas no hay evidencia de evolución, aunque change sea cero.
Indica fechas reales de las observaciones, intervalo solicitado, cantidad y cobertura disponible;
no llames mensual a un historial que solo tiene unas horas ni supongas continuidad entre muestras.
Puedes apoyar una evolución o comparación con show_chart=true si aporta claridad; no es obligatorio.
Si el usuario pide un gráfico, usa show_chart=true. Se muestran gráficos interactivos dentro del chat.
Cuando no muestres uno, puedes sugerir una variable concreta para graficar, sin pedir confirmación
para leer datos. No añadas preguntas sobre formato. No infieras causas de riego o fertilización
solo por la telemetría. Usa exclusivamente la unidad registrada; si falta, dilo, sin adivinarla.
""".strip()


def normalized(value):
    return "".join(c for c in unicodedata.normalize("NFKD", str(value or "").lower()) if not unicodedata.combining(c))


def needs_sensor_query(text, context=None):
    text = normalized(text)
    return bool(re.search(r"\b(sensor\w*|telemetria|ambiente\w*|humedad|temperatura|conductividad|co2|pm10|pm2_5)\b", text) or
                (context and (period_from_text(text) or re.search(r"\b(sub\w*|baj\w*|aument\w*|dismin\w*|graf\w*|tendencia|comportamiento)\b", text))))


def sensor_context(conversation):
    messages = list(conversation.messages)
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        context = (message.meta or {}).get("sensor_context")
        if message.role == "assistant" and context:
            return dict(context)
        if message.role == "assistant":
            for card in reversed((message.meta or {}).get("metric_cards") or []):
                if card.get("metric") == "farm_sensor_analysis" and card.get("farm"):
                    period = card.get("period") or {}
                    previous_user = next((item.content for item in reversed(messages[:index]) if item.role == "user"), "")
                    return {"farm_name": card["farm"]["name"], "farm_id": card["farm"].get("id"),
                            "start_date": period.get("from"), "end_date": period.get("to"), "focus_text": previous_user}
    return {}


def period_from_text(text, now=None):
    now = now or datetime.now(timezone.utc)
    text = normalized(text)
    explicit = re.search(r"desde\s+(\d{4}-\d{2}-\d{2}(?:[t ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:z|[+-]\d{2}:\d{2})?)?)\s+hasta\s+(\d{4}-\d{2}-\d{2}(?:[t ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:z|[+-]\d{2}:\d{2})?)?)", text)
    if explicit:
        return {"start_date": explicit[1].upper(), "end_date": explicit[2].upper(), "hours": None}
    if re.search(r"\bmes (pasado|anterior)\b", text):
        end = now.astimezone(TZ).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        start = (end - timedelta(days=1)).replace(day=1)
        return {"start_date": start.isoformat(), "end_date": (end - timedelta(microseconds=1)).isoformat(), "hours": None}
    match = re.search(r"(?:ultim[oa]s?|pasad[oa]s?)\s+(?:(\d+|un[oa]?)\s+)?(horas?|dias?|semanas?|mes(?:es)?)\b", text)
    if not match:
        return {}
    amount = int(match[1]) if match[1] and match[1].isdigit() else 1
    unit = match[2]
    factor = 1 if unit.startswith("hora") else 24 if unit.startswith("dia") else 168 if unit.startswith("semana") else 720
    # _range rejects unsupported windows rather than silently shortening them.
    return {"start_date": None, "end_date": None, "hours": amount * factor}


def resolve_sensor_arguments(arguments, text, previous=None):
    previous = previous or {}
    arguments = dict(arguments)
    # Prompts emitted by the UI carry their original farm, node and dates, even
    # if a different sensor has since been discussed in the conversation.
    named = re.search(r"del sensor (.+?) de la finca (.+?), desde\b", text, re.IGNORECASE)
    if named:
        arguments.update({"node_name": named[1], "farm_name": named[2], "node_id": None, "_chart_prompt": True})
    explicit_variable = re.search(r"\bvariable\s+([\w.-]+)", text)
    if named and explicit_variable:
        arguments["variable_ids"] = [explicit_variable[1]]
    merged = dict(arguments)
    same_farm = not arguments.get("farm_name") or normalized(arguments["farm_name"]) == normalized(previous.get("farm_name"))
    if same_farm:
        for key in ("farm_name", "node_id", "node_name", "variable_ids", "hours", "start_date", "end_date"):
            if merged.get(key) is None and previous.get(key) is not None:
                merged[key] = previous[key]
    requested_period = period_from_text(text)
    if same_farm and previous and not requested_period and not arguments.get("start_date") and not arguments.get("end_date"):
        for key in ("hours", "start_date", "end_date"):
            if key in previous:
                merged[key] = previous[key]
    merged.update(requested_period)
    if re.search(r"\b(graf\w*)\b", normalized(text)):
        merged["show_chart"] = True
    return merged


def select_variables(nodes, text):
    text = normalized(text)
    predicates = []
    if "conductividad" in text:
        predicates.append(lambda value: "conduct" in value or "soil_ec" in value)
    if "humedad" in text:
        soil = "suelo" in text or "soil" in text
        predicates.append(lambda value: ("hum" in value or "water_soil" in value or "moist" in value) and (not soil or "soil" in value or "suelo" in value or "vwc" in value))
    if "temperatura" in text:
        predicates.append(lambda value: "temp" in value)
    if not predicates:
        # New payloads remain queryable using their configured labels or keys.
        direct = list(dict.fromkeys(key for node in nodes for key, meta in (node.variables_config or {}).items()
            if any(len(normalized(name)) >= 3 and re.search(r"(?<!\w)" + re.escape(normalized(name)) + r"(?!\w)", text)
                   for name in (key, meta.get("label"), meta.get("source_key")) if name)))
        return direct or None
    return list(dict.fromkeys(key for node in nodes for key, meta in (node.variables_config or {}).items()
        if any(predicate(normalized(f"{key} {meta.get('label', '')} {meta.get('source_key', '')}")) for predicate in predicates)))


def chart_points(points, limit=1000):
    """Keep endpoints and each bucket's extrema, ordered by the actual timestamps."""
    if len(points) <= limit:
        return points
    size = max(1, (len(points) + 498) // 499)
    indices = {0, len(points) - 1}
    for start in range(0, len(points), size):
        bucket = range(start, min(start + size, len(points)))
        indices.add(min(bucket, key=lambda index: points[index]["value"]))
        indices.add(max(bucket, key=lambda index: points[index]["value"]))
    return [points[index] for index in sorted(indices)]


def enrich_sensor_result(result, arguments):
    result = jsonable_encoder(result)
    if not result.get("ok"):
        return result
    context = {key: arguments.get(key) for key in ("node_id", "node_name", "variable_ids", "hours", "start_date", "end_date")}
    context["farm_name"] = result["farm"]["name"]
    context["farm_id"] = result["farm"]["id"]
    result["sensor_context"] = context
    components = []
    suggestions = []
    for variable in result.get("variables", []):
        points = variable.pop("points", [])
        if arguments.get("show_chart") and points:
            reduced = chart_points(points)
            components.append({"type": "sensor_chart", "title": variable["label"], "unit": variable["unit"],
                "sensor_name": variable["node_name"], "period": result["period"], "points": reduced,
                "sample_count": variable["count"], "reduced": len(reduced) != len(points), "truncated": result.get("truncated", False)})
        elif variable["count"] > 1 and len(suggestions) < 3:
            # Explicit dates keep each historical suggestion tied to its own analysis.
            suggestions.append({"label": f"Ver gráfico de {variable['label']}", "prompt":
                f"Muestra el gráfico de la variable {variable['variable_id']} del sensor {variable['node_name']} "
                f"de la finca {result['farm']['name']}, desde {result['period']['from']} hasta {result['period']['to']}."})
    if suggestions:
        components.append({"type": "prompt_suggestions", "items": suggestions})
    result["components"] = components
    return result


def analyze_sensor_chat(db, arguments, text, previous=None):
    from modules.mod_caficultura.ai_metrics import resolve_farm
    from modules.mod_caficultura.ai_farm_agent import analyze_farm_telemetry
    from modules.mod_caficultura.model_iot import IoTNode
    from modules.mod_caficultura.models import Finca

    arguments = resolve_sensor_arguments(arguments, text, previous)
    farm_name = str(arguments.get("farm_name") or "")
    exact = [farm for farm in db.query(Finca).filter(Finca.activa.is_(True)).all() if normalized(farm.nombre) == normalized(farm_name)]
    resolved = {"ok": True, "row": exact[0]} if len(exact) == 1 else resolve_farm(db, farm_name)
    if not resolved.get("ok"):
        return resolved
    farm = resolved["row"]
    nodes = db.query(IoTNode).filter(IoTNode.finca_id == farm.id).all()
    if arguments.get("node_name"):
        matches = [node for node in nodes if normalized(node.nombre) == normalized(arguments["node_name"])]
        if len(matches) != 1:
            return {"ok": False, "message": "Seleccione un sensor de esta finca.", "nodes": [{"id": n.id, "name": n.nombre} for n in nodes]}
        arguments["node_id"] = matches[0].id
    selected = [node for node in nodes if not arguments.get("node_id") or node.id == arguments["node_id"]]
    explicit_variable = re.search(r"\bvariable\s+([\w.-]+)", text)
    if explicit_variable and any(explicit_variable[1] in (node.variables_config or {}) for node in selected):
        arguments["variable_ids"] = [explicit_variable[1]]
    focused = None if arguments.get("_chart_prompt") else select_variables(selected, text)
    if focused is None and not arguments.get("variable_ids") and previous and previous.get("focus_text"):
        focused = select_variables(selected, previous["focus_text"])
    if focused is not None:
        if not focused:
            return {"ok": False, "message": "No se encontró esa variable registrada en los sensores seleccionados."}
        arguments["variable_ids"] = focused
    if not arguments.get("hours") and not arguments.get("start_date"):
        arguments["hours"] = 24
    arguments["include_points"] = bool(arguments.get("show_chart"))
    result = enrich_sensor_result(analyze_farm_telemetry(db, farm.id, arguments), arguments)
    if result.get("ok"):
        result.update({"metric": "farm_sensor_analysis", "sensor_count": len(nodes),
            "summary": result.get("warning") or result.get("message") or
                f"{len(result['variables'])} variables analizadas entre "
                f"{datetime.fromisoformat(result['period']['from']).astimezone(TZ).strftime('%d/%m/%Y %H:%M')} y "
                f"{datetime.fromisoformat(result['period']['to']).astimezone(TZ).strftime('%d/%m/%Y %H:%M')} (hora de Costa Rica)."})
    return result
