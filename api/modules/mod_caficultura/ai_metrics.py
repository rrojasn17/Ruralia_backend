from __future__ import annotations

import math
import unicodedata
from datetime import date, datetime, timedelta, timezone
from difflib import SequenceMatcher
from typing import Any

from fastapi.encoders import jsonable_encoder
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from modules.mod_caficultura.models import (
    Cliente,
    CompraInsumoFactura,
    CompraInsumoLinea,
    ActividadFinca,
    Finca,
    InsumoFinca,
    OrdenTrabajo,
    OrdenTrabajoRecibo,
    ReciboCafe,
    RegistroFinca,
    RegistroFincaInsumo,
    RegistroFincaTrabajador,
    SeguimientoOT,
    SolicitudSalidaVenta,
    SolicitudSalidaVentaLinea,
    SolicitudVenta,
    TrabajadorFinca,
)
from modules.mod_caficultura.model_notifications import NotificationEvent
from modules.mod_caficultura.model_iot import IoTNode, IoTReading
from core.models import Usuario


METRIC_OPTIONS = [
    {"value": "farm_spend", "label": "Gasto acumulado por finca"},
    {"value": "coffee_received", "label": "Café recibido en planta"},
    {"value": "coffee_in_patio", "label": "Café disponible en patio"},
    {"value": "worker_status", "label": "Actividad de un trabajador"},
    {"value": "pending_work", "label": "Pendientes operativos"},
    {"value": "latest_alerts", "label": "Alertas recientes"},
    {"value": "lot_qr", "label": "QR de lote vendido"},
    {"value": "lot_status", "label": "Estado integral de un lote"},
    {"value": "farm_activity", "label": "Actividad y costos de finca"},
    {"value": "days_since_fertilization", "label": "Días desde última fertilización"},
    {"value": "farm_sensor_analysis", "label": "Condiciones ambientales y sensores de finca"},
    {"value": "inventory_status", "label": "Inventario de insumos"},
    {"value": "sales_summary", "label": "Resumen de ventas"},
    {"value": "business_overview", "label": "Resumen general del negocio"},
]


def _norm(value: object) -> str:
    normalized = unicodedata.normalize("NFD", str(value or "").strip().lower())
    return "".join(char for char in normalized if unicodedata.category(char) != "Mn")


def _json_value(value: Any) -> Any:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return 0
    return value


def _period(parameters: dict[str, Any], *, default_year: bool = True) -> tuple[date | None, date | None]:
    year = parameters.get("year")
    start = parameters.get("start_date")
    end = parameters.get("end_date")
    try:
        start_date = date.fromisoformat(str(start)) if start else None
        end_date = date.fromisoformat(str(end)) if end else None
    except ValueError:
        raise ValueError("Las fechas deben utilizar el formato AAAA-MM-DD")

    if year is not None:
        try:
            numeric_year = int(year)
        except (TypeError, ValueError) as exc:
            raise ValueError("El año debe ser numérico") from exc
        if numeric_year < 2000 or numeric_year > date.today().year + 1:
            raise ValueError("El año solicitado está fuera del rango permitido")
        start_date = start_date or date(numeric_year, 1, 1)
        end_date = end_date or date(numeric_year, 12, 31)
    elif default_year and not start_date and not end_date:
        current = date.today().year
        start_date, end_date = date(current, 1, 1), date(current, 12, 31)

    if start_date and end_date and start_date > end_date:
        raise ValueError("La fecha inicial no puede ser posterior a la fecha final")
    return start_date, end_date


def _resolve_named(rows: list[Any], query: str, fields: tuple[str, ...], label: str) -> dict[str, Any]:
    needle = _norm(query)
    if not needle:
        return {"ok": False, "needs_clarification": True, "message": f"Indique {label}."}

    ranked: list[tuple[float, Any, str]] = []
    for row in rows:
        values = [str(getattr(row, field, "") or "") for field in fields]
        haystack = " ".join(values)
        normalized_values = [_norm(value) for value in values if value]
        # Compare each field separately: codes/owners must not dilute a name match.
        variants = normalized_values + [value.removeprefix("finca ").strip() for value in normalized_values]
        if needle in variants:
            score = 1.0
        elif len(needle) >= 3 and any(needle in value for value in variants):
            score = 0.92
        else:
            score = max((SequenceMatcher(None, needle.removeprefix("finca "), value).ratio()
                         for value in variants), default=0.0) if len(needle) >= 4 else 0.0
        if score >= 0.55:
            ranked.append((score, row, haystack))
    ranked.sort(key=lambda item: (-item[0], getattr(item[1], "id", 0)))

    if not ranked:
        return {"ok": False, "needs_clarification": True, "message": f"No encontré {label} con '{query}'."}
    if ranked[0][0] < 0.82 or (len(ranked) > 1 and ranked[0][0] - ranked[1][0] < 0.08):
        options = [
            {"id": item[1].id, "label": item[2]}
            for item in ranked[:5]
        ]
        return {
            "ok": False,
            "needs_clarification": True,
            "message": f"Encontré posibles coincidencias para {label}. ¿Cuál desea usar?",
            "options": options,
        }
    return {"ok": True, "row": ranked[0][1], "match_score": ranked[0][0], "matched_label": ranked[0][2]}


def resolve_client(db: Session, query: str) -> dict[str, Any]:
    rows = db.query(Cliente).filter(Cliente.activo.is_(True)).order_by(Cliente.nombre_completo).limit(1000).all()
    return _resolve_named(rows, query, ("nombre_completo", "codigo", "numero_identificacion"), "el cliente")


def resolve_farm(db: Session, query: str) -> dict[str, Any]:
    rows = db.query(Finca).filter(Finca.activa.is_(True)).order_by(Finca.nombre).limit(1000).all()
    return _resolve_named(rows, query, ("nombre", "codigo", "propietario"), "la finca")


def days_since_fertilization(db: Session, parameters: dict[str, Any]) -> dict[str, Any]:
    farm_name = str(parameters.get("farm_name") or parameters.get("finca") or "").strip()
    resolved = resolve_farm(db, farm_name)
    if not resolved.get("ok"):
        return resolved
    farm: Finca = resolved["row"]
    fertilization_activity = or_(
        func.lower(ActividadFinca.tipo).like("%fertiliz%"),
        func.lower(ActividadFinca.tipo).like("%abon%"),
        func.lower(ActividadFinca.nombre).like("%fertiliz%"),
        func.lower(ActividadFinca.nombre).like("%abon%"),
    )
    last_date = (
        db.query(func.max(RegistroFinca.fecha))
        .join(ActividadFinca, ActividadFinca.id == RegistroFinca.actividad_id)
        .filter(RegistroFinca.finca_id == farm.id, fertilization_activity)
        .scalar()
    )
    baseline = last_date or (farm.created_at.date() if farm.created_at else date.today())
    days = max((date.today() - baseline).days, 0)
    return {
        "ok": True,
        "metric": "days_since_fertilization",
        "farm": {"id": farm.id, "name": farm.nombre},
        "last_fertilization_date": last_date.isoformat() if last_date else None,
        "baseline": "last_fertilization" if last_date else "farm_created_at",
        "days_without_fertilization": days,
        "primary_value": days,
        "unit": "days",
        "summary": (
            f"La finca {farm.nombre} lleva {days} días desde la última fertilización registrada."
            if last_date
            else f"La finca {farm.nombre} no tiene fertilizaciones registradas; se cuentan {days} días desde su creación."
        ),
    }


def farm_sensor_analysis(db: Session, parameters: dict[str, Any]) -> dict[str, Any]:
    farm_name = str(parameters.get("farm_name") or parameters.get("finca") or "").strip()
    resolved = resolve_farm(db, farm_name)
    if not resolved.get("ok"):
        return resolved
    farm: Finca = resolved["row"]
    # Import lazily: the farm assistant reuses the general agent configuration.
    from modules.mod_caficultura.ai_farm_agent import analyze_farm_telemetry, farm_sensor_inventory

    inventory = jsonable_encoder(farm_sensor_inventory(db, farm.id))
    hours = min(max(int(parameters.get("hours") or 24), 1), 168)
    telemetry = jsonable_encoder(analyze_farm_telemetry(db, farm.id, {"hours": hours}))
    node_metadata = {node["id"]: node["variables"] for node in inventory["nodes"]}
    humidity_threshold = min(max(float(parameters.get("humidity_threshold") or 85), 0), 100)
    max_gap_hours = min(max(float(parameters.get("max_gap_hours") or 2), 0.25), 6)
    now = datetime.now(timezone.utc)
    start = now - timedelta(hours=hours)
    rows = (
        db.query(IoTNode.id, IoTNode.nombre, IoTReading.recorded_at, IoTReading.data)
        .join(IoTReading, IoTReading.node_id == IoTNode.id)
        .filter(
            IoTNode.finca_id == farm.id,
            IoTNode.activo.is_(True),
            IoTReading.recorded_at >= start,
            IoTReading.recorded_at <= now,
        )
        .order_by(IoTReading.recorded_at.asc(), IoTReading.id.asc())
        .limit(5000)
        .all()
    )

    node_samples: dict[int, list[dict[str, Any]]] = {}
    for node_id, node_name, recorded_at, raw_values in rows:
        values = raw_values if isinstance(raw_values, dict) else {}
        humidity_values = []
        for key, value in values.items():
            meta = node_metadata.get(node_id, {}).get(key) or {}
            normalized = _norm(" ".join(str(item or "") for item in (key, meta.get("label"), meta.get("source_key")))).replace("_", "").replace("-", "")
            if "humed" not in normalized and "humidity" not in normalized:
                continue
            if "suelo" in normalized or "soil" in normalized:
                continue
            try:
                numeric = float(value)
                if math.isfinite(numeric) and 0 <= numeric <= 100:
                    humidity_values.append(numeric)
            except (TypeError, ValueError):
                continue
        if not humidity_values:
            continue
        timestamp = recorded_at.replace(tzinfo=timezone.utc) if recorded_at.tzinfo is None else recorded_at.astimezone(timezone.utc)
        node_samples.setdefault(node_id, []).append({
            "node": node_name,
            "recorded_at": timestamp,
            "humidity": max(humidity_values),
        })

    current_streak_hours = 0.0
    latest_reading_at: datetime | None = None
    latest_humidity: float | None = None
    samples: list[dict[str, Any]] = []
    max_gap = timedelta(hours=max_gap_hours)
    for readings in node_samples.values():
        streak_start: datetime | None = None
        previous_at: datetime | None = None
        streak_hours = 0.0
        for reading in readings:
            timestamp = reading["recorded_at"]
            humidity = reading["humidity"]
            samples.append({
                "node": reading["node"],
                "recorded_at": timestamp.isoformat(),
                "relative_humidity": round(humidity, 2),
            })
            if latest_reading_at is None or timestamp > latest_reading_at:
                latest_reading_at = timestamp
                latest_humidity = humidity
            if humidity <= humidity_threshold:
                streak_start = None
                previous_at = None
                streak_hours = 0.0
                continue
            if streak_start is None or previous_at is None or timestamp - previous_at > max_gap:
                streak_start = timestamp
            previous_at = timestamp
            streak_hours = max((timestamp - streak_start).total_seconds() / 3600, 0.0)
        if previous_at and now - previous_at <= max_gap:
            current_streak_hours = max(current_streak_hours, streak_hours)

    samples.sort(key=lambda item: item["recorded_at"], reverse=True)
    return {
        "ok": True,
        "metric": "farm_sensor_analysis",
        "sensor_count": len(inventory["nodes"]),
        "active_sensor_count": sum(bool(node["active"]) for node in inventory["nodes"]),
        "nodes": inventory["nodes"],
        "variables": telemetry.get("variables", []),
        "period": telemetry.get("period"),
        "reading_rows": telemetry.get("reading_rows", 0),
        "truncated": telemetry.get("truncated", False),
        "environmental_summary": (
            f"La finca {farm.nombre} tiene {len(inventory['nodes'])} sensores registrados y "
            f"{telemetry.get('reading_rows', 0)} lecturas en las últimas {hours} horas. "
            "Consulte variables para temperatura, humedad y otras mediciones disponibles; "
            "la ausencia de lecturas recientes no significa ausencia de sensores."
        ),
        "farm": {"id": farm.id, "name": farm.nombre},
        "period_hours": hours,
        "humidity_threshold": humidity_threshold,
        "max_sample_gap_hours": max_gap_hours,
        "current_consecutive_high_humidity_hours": round(current_streak_hours, 2),
        "latest_reading_at": latest_reading_at.isoformat() if latest_reading_at else None,
        "latest_relative_humidity": round(latest_humidity, 2) if latest_humidity is not None else None,
        "sample_count": len(samples),
        "samples": samples[:30],
        "primary_value": round(current_streak_hours, 2),
        "unit": "hours",
        "summary": (
            f"La finca {farm.nombre} registra {current_streak_hours:.2f} horas consecutivas con humedad relativa superior a {humidity_threshold:g}%."
            if samples
            else f"No hay lecturas recientes de humedad relativa para la finca {farm.nombre} en las últimas {hours} horas."
        ),
    }


def resolve_worker(db: Session, query: str) -> dict[str, Any]:
    rows = db.query(TrabajadorFinca).filter(TrabajadorFinca.activo.is_(True)).order_by(TrabajadorFinca.nombre).limit(1000).all()
    return _resolve_named(rows, query, ("nombre", "codigo", "identificacion"), "el trabajador")


def resolve_lot(db: Session, query: str) -> dict[str, Any]:
    rows = db.query(OrdenTrabajo).order_by(OrdenTrabajo.created_at.desc()).limit(1500).all()
    return _resolve_named(rows, query, ("codigo_lote",), "el lote")


def farm_spend(db: Session, parameters: dict[str, Any]) -> dict[str, Any]:
    farm_name = str(parameters.get("farm_name") or parameters.get("finca") or "").strip()
    resolved = resolve_farm(db, farm_name)
    if not resolved.get("ok"):
        return resolved
    farm: Finca = resolved["row"]
    start_date, end_date = _period(parameters)

    labor_query = (
        db.query(
            func.coalesce(func.sum(RegistroFincaTrabajador.costo), 0),
            func.coalesce(func.sum(RegistroFincaTrabajador.horas), 0),
            func.count(RegistroFincaTrabajador.id),
        )
        .join(RegistroFinca, RegistroFinca.id == RegistroFincaTrabajador.registro_id)
        .filter(RegistroFinca.finca_id == farm.id)
    )
    supplies_query = (
        db.query(
            func.coalesce(func.sum(RegistroFincaInsumo.costo_total), 0),
            func.count(RegistroFincaInsumo.id),
        )
        .join(RegistroFinca, RegistroFinca.id == RegistroFincaInsumo.registro_id)
        .filter(RegistroFinca.finca_id == farm.id)
    )
    records_query = db.query(func.count(RegistroFinca.id)).filter(RegistroFinca.finca_id == farm.id)
    if start_date:
        labor_query = labor_query.filter(RegistroFinca.fecha >= start_date)
        supplies_query = supplies_query.filter(RegistroFinca.fecha >= start_date)
        records_query = records_query.filter(RegistroFinca.fecha >= start_date)
    if end_date:
        labor_query = labor_query.filter(RegistroFinca.fecha <= end_date)
        supplies_query = supplies_query.filter(RegistroFinca.fecha <= end_date)
        records_query = records_query.filter(RegistroFinca.fecha <= end_date)

    labor_cost, hours, labor_lines = labor_query.one()
    supply_cost, supply_lines = supplies_query.one()
    total = round(float(labor_cost or 0) + float(supply_cost or 0), 2)
    return {
        "ok": True,
        "metric": "farm_spend",
        "farm": {"id": farm.id, "code": farm.codigo, "name": farm.nombre},
        "period": {"start": _json_value(start_date), "end": _json_value(end_date)},
        "currency": "CRC",
        "labor_cost": round(float(labor_cost or 0), 2),
        "supplies_cost": round(float(supply_cost or 0), 2),
        "total": total,
        "primary_value": total,
        "hours": round(float(hours or 0), 2),
        "labor_entries": int(labor_lines or 0),
        "supply_entries": int(supply_lines or 0),
        "activity_records": int(records_query.scalar() or 0),
        "summary": f"La finca {farm.nombre} acumula ₡{total:,.2f} en el período consultado.",
    }


def coffee_received(db: Session, parameters: dict[str, Any]) -> dict[str, Any]:
    start_date, end_date = _period(parameters)
    query = db.query(
        func.coalesce(func.sum(ReciboCafe.cajuelas), 0),
        func.coalesce(func.sum(ReciboCafe.cuartillos), 0),
        func.count(ReciboCafe.id),
    ).filter(ReciboCafe.estado != "anulado")
    if start_date:
        query = query.filter(ReciboCafe.fecha >= start_date)
    if end_date:
        query = query.filter(ReciboCafe.fecha <= end_date)
    farm_name = str(parameters.get("farm_name") or "").strip()
    farm = None
    if farm_name:
        resolved = resolve_farm(db, farm_name)
        if not resolved.get("ok"):
            return resolved
        farm = resolved["row"]
        query = query.filter(ReciboCafe.finca_id == farm.id)
    cajuelas, cuartillos, receipts = query.one()
    total_cajuelas = round(float(cajuelas or 0) + float(cuartillos or 0) / 4, 3)
    fanegas = round(total_cajuelas / 20, 3)
    return {
        "ok": True,
        "metric": "coffee_received",
        "farm": {"id": farm.id, "name": farm.nombre} if farm else None,
        "period": {"start": _json_value(start_date), "end": _json_value(end_date)},
        "receipts": int(receipts or 0),
        "cajuelas_equivalent": total_cajuelas,
        "fanegas": fanegas,
        "primary_value": fanegas,
        "summary": f"Han ingresado {fanegas:,.3f} fanegas ({total_cajuelas:,.3f} cajuelas equivalentes) en {int(receipts or 0)} recibos.",
    }


def coffee_in_patio(db: Session, parameters: dict[str, Any]) -> dict[str, Any]:
    assigned = (
        db.query(
            OrdenTrabajoRecibo.recibo_id.label("recibo_id"),
            func.coalesce(
                func.sum(OrdenTrabajoRecibo.cajuelas_asignadas + OrdenTrabajoRecibo.cuartillos_asignados / 4.0),
                0,
            ).label("assigned"),
        )
        .group_by(OrdenTrabajoRecibo.recibo_id)
        .subquery()
    )
    available_expr = (
        func.coalesce(ReciboCafe.cajuelas, 0)
        + func.coalesce(ReciboCafe.cuartillos, 0) / 4.0
        - func.coalesce(assigned.c.assigned, 0)
    )
    query = (
        db.query(
            func.coalesce(func.sum(available_expr), 0),
            func.count(ReciboCafe.id),
            func.min(ReciboCafe.fecha),
            func.max(ReciboCafe.fecha),
        )
        .outerjoin(assigned, assigned.c.recibo_id == ReciboCafe.id)
        .filter(ReciboCafe.estado == "recibido", available_expr > 0.0001)
    )
    farm_name = str(parameters.get("farm_name") or "").strip()
    farm = None
    if farm_name:
        resolved = resolve_farm(db, farm_name)
        if not resolved.get("ok"):
            return resolved
        farm = resolved["row"]
        query = query.filter(ReciboCafe.finca_id == farm.id)
    cajuelas, receipts, oldest, newest = query.one()
    total_cajuelas = round(max(float(cajuelas or 0), 0), 3)
    fanegas = round(total_cajuelas / 20, 3)
    return {
        "ok": True,
        "metric": "coffee_in_patio",
        "definition": "Café recibido que todavía no ha sido asignado completamente a un lote.",
        "farm": {"id": farm.id, "name": farm.nombre} if farm else None,
        "receipts": int(receipts or 0),
        "cajuelas_equivalent": total_cajuelas,
        "fanegas": fanegas,
        "oldest_receipt_date": _json_value(oldest),
        "newest_receipt_date": _json_value(newest),
        "primary_value": fanegas,
        "summary": f"Hay {fanegas:,.3f} fanegas disponibles en patio, distribuidas en {int(receipts or 0)} recibos.",
    }


def worker_status(db: Session, parameters: dict[str, Any]) -> dict[str, Any]:
    worker_name = str(parameters.get("worker_name") or parameters.get("trabajador") or "").strip()
    resolved = resolve_worker(db, worker_name)
    if not resolved.get("ok"):
        return resolved
    worker: TrabajadorFinca = resolved["row"]
    start_date, end_date = _period(parameters)
    query = (
        db.query(
            func.count(RegistroFincaTrabajador.id),
            func.coalesce(func.sum(RegistroFincaTrabajador.horas), 0),
            func.coalesce(func.sum(RegistroFincaTrabajador.costo), 0),
            func.max(RegistroFinca.fecha),
        )
        .join(RegistroFinca, RegistroFinca.id == RegistroFincaTrabajador.registro_id)
        .filter(RegistroFincaTrabajador.trabajador_id == worker.id)
    )
    if start_date:
        query = query.filter(RegistroFinca.fecha >= start_date)
    if end_date:
        query = query.filter(RegistroFinca.fecha <= end_date)
    entries, hours, cost, last_date = query.one()
    recent = (
        db.query(RegistroFinca)
        .join(RegistroFincaTrabajador, RegistroFincaTrabajador.registro_id == RegistroFinca.id)
        .filter(RegistroFincaTrabajador.trabajador_id == worker.id)
        .order_by(RegistroFinca.fecha.desc(), RegistroFinca.id.desc())
        .limit(5)
        .all()
    )
    return {
        "ok": True,
        "metric": "worker_status",
        "worker": {"id": worker.id, "code": worker.codigo, "name": worker.nombre, "position": worker.puesto},
        "period": {"start": _json_value(start_date), "end": _json_value(end_date)},
        "entries": int(entries or 0),
        "hours": round(float(hours or 0), 2),
        "cost": round(float(cost or 0), 2),
        "primary_value": round(float(hours or 0), 2),
        "last_activity_date": _json_value(last_date),
        "recent_activities": [
            {
                "date": _json_value(row.fecha),
                "description": row.descripcion or (row.actividad.nombre if row.actividad else "Actividad"),
                "farm": row.finca.nombre if row.finca else None,
                "status": row.estado,
            }
            for row in recent
        ],
        "summary": f"{worker.nombre} registra {float(hours or 0):,.2f} horas en {int(entries or 0)} participaciones durante el período.",
    }


def pending_work(db: Session, parameters: dict[str, Any]) -> dict[str, Any]:
    active_states = ["abierta", "en_proceso", "pendiente_aprobacion"]
    query = db.query(OrdenTrabajo).filter(OrdenTrabajo.estado.in_(active_states))
    farm_name = str(parameters.get("farm_name") or "").strip()
    farm = None
    if farm_name:
        resolved = resolve_farm(db, farm_name)
        if not resolved.get("ok"):
            return resolved
        farm = resolved["row"]
        query = query.filter(OrdenTrabajo.finca_id == farm.id)
    rows = query.order_by(OrdenTrabajo.fecha_inicio.asc()).limit(50).all()
    state_counts: dict[str, int] = {}
    for row in rows:
        state_counts[row.estado] = state_counts.get(row.estado, 0) + 1
    pending_alerts = db.query(func.count(SeguimientoOT.id)).filter(
        SeguimientoOT.dar_alerta.is_(True),
        SeguimientoOT.estado_revision == "pendiente",
    ).scalar()
    return {
        "ok": True,
        "metric": "pending_work",
        "farm": {"id": farm.id, "name": farm.nombre} if farm else None,
        "total": len(rows),
        "primary_value": len(rows),
        "by_status": state_counts,
        "pending_alerts": int(pending_alerts or 0),
        "lots": [
            {
                "id": row.id,
                "code": row.codigo_lote,
                "status": row.estado,
                "process": row.proceso,
                "start_date": _json_value(row.fecha_inicio),
                "operator": row.operario.nombre if row.operario else None,
            }
            for row in rows[:15]
        ],
        "summary": f"Hay {len(rows)} lotes con trabajo pendiente y {int(pending_alerts or 0)} alertas por revisar.",
    }


def latest_alerts(db: Session, parameters: dict[str, Any]) -> dict[str, Any]:
    limit = max(1, min(int(parameters.get("limit") or 10), 30))
    rows = (
        db.query(NotificationEvent)
        .order_by(NotificationEvent.detected_at.desc())
        .limit(limit)
        .all()
    )
    unsent = sum(1 for row in rows if row.status not in {"sent", "suppressed"})
    return {
        "ok": True,
        "metric": "latest_alerts",
        "count": len(rows),
        "primary_value": unsent,
        "pending_or_failed": unsent,
        "alerts": [
            {
                "id": row.id,
                "severity": row.severity,
                "title": row.title,
                "message": row.message,
                "status": row.status,
                "detected_at": _json_value(row.detected_at),
            }
            for row in rows
        ],
        "summary": f"Revisé {len(rows)} alertas recientes; {unsent} siguen pendientes o presentan fallo de entrega.",
    }


def lot_qr_for_client(db: Session, parameters: dict[str, Any]) -> dict[str, Any]:
    client_name = str(parameters.get("client_name") or "").strip()
    lot_code = str(parameters.get("lot_code") or "").strip()
    if not client_name and not lot_code:
        return {
            "ok": False,
            "needs_clarification": True,
            "message": "Indique el cliente o el código del lote vendido.",
        }
    query = (
        db.query(OrdenTrabajo, Cliente, SolicitudSalidaVenta)
        .join(SolicitudSalidaVentaLinea, SolicitudSalidaVentaLinea.ot_id == OrdenTrabajo.id)
        .join(SolicitudSalidaVenta, SolicitudSalidaVenta.id == SolicitudSalidaVentaLinea.solicitud_id)
        .join(Cliente, Cliente.id == SolicitudSalidaVenta.cliente_id)
        .filter(SolicitudSalidaVenta.estado == "vendida")
    )
    if client_name:
        resolved = resolve_client(db, client_name)
        if not resolved.get("ok"):
            return resolved
        query = query.filter(Cliente.id == resolved["row"].id)
    if lot_code:
        resolved_lot = resolve_lot(db, lot_code)
        if not resolved_lot.get("ok"):
            return resolved_lot
        query = query.filter(OrdenTrabajo.id == resolved_lot["row"].id)
    rows = query.order_by(SolicitudSalidaVenta.fecha.desc()).limit(10).all()
    if not rows:
        return {"ok": False, "needs_clarification": True, "message": "No encontré un lote vendido con esos datos."}
    if len(rows) > 1 and not lot_code:
        return {
            "ok": False,
            "needs_clarification": True,
            "message": "Ese cliente tiene varios lotes vendidos. Indique cuál desea.",
            "options": [{"id": lot.id, "label": lot.codigo_lote} for lot, _, _ in rows],
        }
    lot, client, sale = rows[0]
    return {
        "ok": True,
        "metric": "lot_qr",
        "primary_value": lot.id,
        "client": {"id": client.id, "name": client.nombre_completo},
        "sale": {"id": sale.id, "code": sale.codigo, "date": _json_value(sale.fecha)},
        "lot": {
            "id": lot.id,
            "code": lot.codigo_lote,
            "status": lot.estado,
            "qr_url": lot.qr_public_url,
            "traceability_path": f"/trazabilidad/{lot.qr_token}" if lot.qr_token else None,
        },
        "summary": f"El lote {lot.codigo_lote} vendido a {client.nombre_completo} tiene su trazabilidad disponible.",
    }


def lot_status(db: Session, parameters: dict[str, Any]) -> dict[str, Any]:
    lot_code = str(parameters.get("lot_code") or "").strip()
    resolved = resolve_lot(db, lot_code)
    if not resolved.get("ok"):
        return resolved
    lot: OrdenTrabajo = resolved["row"]
    assigned_fanegas, receipt_count = db.query(
        func.coalesce(func.sum(OrdenTrabajoRecibo.fanegas_asignadas), 0),
        func.count(OrdenTrabajoRecibo.id),
    ).filter(OrdenTrabajoRecibo.ot_id == lot.id).one()
    sold_quintals = (
        db.query(func.coalesce(func.sum(SolicitudSalidaVentaLinea.cantidad_quintales), 0))
        .join(SolicitudSalidaVenta, SolicitudSalidaVenta.id == SolicitudSalidaVentaLinea.solicitud_id)
        .filter(
            SolicitudSalidaVentaLinea.ot_id == lot.id,
            SolicitudSalidaVenta.estado == "vendida",
        )
        .scalar()
    )
    followups = (
        db.query(SeguimientoOT)
        .filter(SeguimientoOT.orden_trabajo_id == lot.id)
        .order_by(SeguimientoOT.fecha.desc(), SeguimientoOT.id.desc())
        .limit(8)
        .all()
    )
    pending_alerts = sum(
        1
        for item in followups
        if item.dar_alerta and item.estado_revision == "pendiente"
    )
    traceability_path = f"/trazabilidad/{lot.qr_token}" if lot.qr_token else None
    return {
        "ok": True,
        "metric": "lot_status",
        "lot": {
            "id": lot.id,
            "code": lot.codigo_lote,
            "process": lot.proceso,
            "status": lot.estado,
            "start_date": _json_value(lot.fecha_inicio),
            "closed_at": _json_value(lot.fecha_cierre),
            "estimated_fanegas": round(float(lot.fanegas_estimadas or 0), 3),
            "traceability_path": traceability_path,
            "qr_url": lot.qr_public_url,
        },
        "farm": {"id": lot.finca.id, "name": lot.finca.nombre} if lot.finca else None,
        "operator": {"id": lot.operario.id, "name": lot.operario.nombre} if lot.operario else None,
        "receipt_count": int(receipt_count or 0),
        "assigned_fanegas": round(float(assigned_fanegas or 0), 3),
        "sold_quintals": round(float(sold_quintals or 0), 3),
        "pending_alerts": pending_alerts,
        "documents": len(lot.documentos or []),
        "followups": [
            {
                "date": _json_value(item.fecha),
                "activity": item.actividad_realizada,
                "resulting_status": item.estado_lote_resultante,
                "alert": bool(item.dar_alerta),
                "review_status": item.estado_revision,
            }
            for item in followups
        ],
        "primary_value": round(float(lot.fanegas_estimadas or 0), 3),
        "summary": (
            f"El lote {lot.codigo_lote} está {lot.estado}, proceso {lot.proceso}, con "
            f"{float(lot.fanegas_estimadas or 0):,.3f} fanegas estimadas y {pending_alerts} alertas pendientes."
        ),
    }


def farm_activity(db: Session, parameters: dict[str, Any]) -> dict[str, Any]:
    farm_name = str(parameters.get("farm_name") or "").strip()
    farm: Finca | None = None
    if farm_name:
        resolved = resolve_farm(db, farm_name)
        if not resolved.get("ok"):
            return resolved
        farm = resolved["row"]
    start_date, end_date = _period(parameters)
    query = db.query(RegistroFinca)
    labor_query = db.query(
        func.coalesce(func.sum(RegistroFincaTrabajador.horas), 0),
        func.coalesce(func.sum(RegistroFincaTrabajador.costo), 0),
    ).join(RegistroFinca, RegistroFinca.id == RegistroFincaTrabajador.registro_id)
    supply_query = db.query(
        func.coalesce(func.sum(RegistroFincaInsumo.costo_total), 0)
    ).join(RegistroFinca, RegistroFinca.id == RegistroFincaInsumo.registro_id)
    status_query = db.query(RegistroFinca.estado, func.count(RegistroFinca.id))
    if farm:
        query = query.filter(RegistroFinca.finca_id == farm.id)
        labor_query = labor_query.filter(RegistroFinca.finca_id == farm.id)
        supply_query = supply_query.filter(RegistroFinca.finca_id == farm.id)
        status_query = status_query.filter(RegistroFinca.finca_id == farm.id)
    if start_date:
        query = query.filter(RegistroFinca.fecha >= start_date)
        labor_query = labor_query.filter(RegistroFinca.fecha >= start_date)
        supply_query = supply_query.filter(RegistroFinca.fecha >= start_date)
        status_query = status_query.filter(RegistroFinca.fecha >= start_date)
    if end_date:
        query = query.filter(RegistroFinca.fecha <= end_date)
        labor_query = labor_query.filter(RegistroFinca.fecha <= end_date)
        supply_query = supply_query.filter(RegistroFinca.fecha <= end_date)
        status_query = status_query.filter(RegistroFinca.fecha <= end_date)
    record_count = query.count()
    recent = query.order_by(RegistroFinca.fecha.desc(), RegistroFinca.id.desc()).limit(12).all()
    labor_hours, labor_cost = labor_query.one()
    supply_cost = supply_query.scalar()
    statuses = {str(state): int(count) for state, count in status_query.group_by(RegistroFinca.estado).all()}
    total_cost = round(float(labor_cost or 0) + float(supply_cost or 0), 2)
    scope = farm.nombre if farm else "todas las fincas"
    return {
        "ok": True,
        "metric": "farm_activity",
        "farm": {"id": farm.id, "code": farm.codigo, "name": farm.nombre} if farm else None,
        "period": {"start": _json_value(start_date), "end": _json_value(end_date)},
        "records": record_count,
        "by_status": statuses,
        "labor_hours": round(float(labor_hours or 0), 2),
        "labor_cost": round(float(labor_cost or 0), 2),
        "supplies_cost": round(float(supply_cost or 0), 2),
        "total_cost": total_cost,
        "currency": "CRC",
        "recent": [
            {
                "id": row.id,
                "date": _json_value(row.fecha),
                "farm": row.finca.nombre if row.finca else None,
                "activity": row.actividad.nombre if row.actividad else row.descripcion,
                "status": row.estado,
            }
            for row in recent
        ],
        "primary_value": total_cost,
        "summary": f"{scope.capitalize()} registran {record_count} actividades y ₡{total_cost:,.2f} en costos durante el período.",
    }


def inventory_status(db: Session, _: dict[str, Any]) -> dict[str, Any]:
    rows = db.query(InsumoFinca).filter(InsumoFinca.activo.is_(True)).order_by(InsumoFinca.nombre).all()
    # Latest recorded purchase per supply, including date/currency as evidence.
    ranked = db.query(
        CompraInsumoLinea.id.label("line_id"),
        func.row_number().over(
            partition_by=CompraInsumoLinea.insumo_id,
            order_by=(CompraInsumoFactura.fecha.desc(), CompraInsumoLinea.id.desc()),
        ).label("position"),
    ).join(CompraInsumoFactura, CompraInsumoFactura.id == CompraInsumoLinea.factura_id).subquery()
    purchases = db.query(CompraInsumoLinea, CompraInsumoFactura).join(
        CompraInsumoFactura, CompraInsumoFactura.id == CompraInsumoLinea.factura_id,
    ).join(ranked, ranked.c.line_id == CompraInsumoLinea.id).filter(ranked.c.position == 1).all()
    latest = {line.insumo_id: (line, invoice) for line, invoice in purchases}
    supplies = []
    for row in rows[:100]:
        purchase = latest.get(row.id)
        reference = None
        if purchase:
            line, invoice = purchase
            reference = {
                "invoice_id": invoice.id, "date": invoice.fecha.isoformat(),
                "unit_price": float(line.precio_unitario), "currency": invoice.moneda,
                "unit": line.unidad, "tax_percent": float(line.impuesto_porcentaje or 0),
                "basis": "Precio histórico antes de impuestos y descuentos; no es una cotización vigente",
            }
        supplies.append({
            "id": row.id, "name": row.nombre, "unit": row.unidad,
            "stock": float(row.stock_actual or 0), "minimum": row.stock_minimo,
            "out_of_stock": float(row.stock_actual or 0) <= 0,
            "historical_purchase": reference,
        })
    low_stock = [
        row
        for row in rows
        if row.stock_minimo is not None and float(row.stock_actual or 0) <= float(row.stock_minimo)
    ]
    inventory_value = round(
        sum(float(row.stock_actual or 0) * float(row.costo_unitario or 0) for row in rows),
        2,
    )
    return {
        "ok": True,
        "metric": "inventory_status",
        "active_supplies": len(rows),
        "supplies": supplies,
        "supplies_truncated": len(rows) > len(supplies),
        "low_stock_count": len(low_stock),
        "inventory_cost_value": inventory_value,
        "currency": "CRC",
        "low_stock": [
            {
                "id": row.id,
                "code": row.codigo,
                "name": row.nombre,
                "unit": row.unidad,
                "stock": round(float(row.stock_actual or 0), 3),
                "minimum": round(float(row.stock_minimo or 0), 3),
            }
            for row in low_stock[:20]
        ],
        "primary_value": len(low_stock),
        "summary": f"Hay {len(low_stock)} insumos en o por debajo del mínimo, de {len(rows)} insumos activos.",
    }


def sales_summary(db: Session, parameters: dict[str, Any]) -> dict[str, Any]:
    start_date, end_date = _period(parameters)
    client_name = str(parameters.get("client_name") or "").strip()
    client: Cliente | None = None
    if client_name:
        resolved = resolve_client(db, client_name)
        if not resolved.get("ok"):
            return resolved
        client = resolved["row"]
    sales_query = db.query(SolicitudSalidaVenta).filter(SolicitudSalidaVenta.estado == "vendida")
    lines_query = (
        db.query(func.coalesce(func.sum(SolicitudSalidaVentaLinea.cantidad_quintales), 0))
        .join(SolicitudSalidaVenta, SolicitudSalidaVenta.id == SolicitudSalidaVentaLinea.solicitud_id)
        .filter(SolicitudSalidaVenta.estado == "vendida")
    )
    if start_date:
        sales_query = sales_query.filter(SolicitudSalidaVenta.fecha >= start_date)
        lines_query = lines_query.filter(SolicitudSalidaVenta.fecha >= start_date)
    if end_date:
        sales_query = sales_query.filter(SolicitudSalidaVenta.fecha <= end_date)
        lines_query = lines_query.filter(SolicitudSalidaVenta.fecha <= end_date)
    if client:
        sales_query = sales_query.filter(SolicitudSalidaVenta.cliente_id == client.id)
        lines_query = lines_query.filter(SolicitudSalidaVenta.cliente_id == client.id)
    sale_count = sales_query.count()
    sales = sales_query.order_by(SolicitudSalidaVenta.fecha.desc()).limit(12).all()
    quintals = float(lines_query.scalar() or 0)
    totals_by_currency = {
        str(currency): round(float(total or 0), 2)
        for currency, total in sales_query.with_entities(
            SolicitudSalidaVenta.moneda,
            func.coalesce(func.sum(SolicitudSalidaVenta.total), 0),
        ).group_by(SolicitudSalidaVenta.moneda).all()
    }
    pending_query = db.query(SolicitudVenta).filter(
        SolicitudVenta.estado.in_(["pendiente", "asignada", "salida_solicitada"])
    )
    if client:
        pending_query = pending_query.filter(SolicitudVenta.cliente_id == client.id)
    pending_count, pending_quintals = pending_query.with_entities(
        func.count(SolicitudVenta.id),
        func.coalesce(func.sum(SolicitudVenta.cantidad_quintales), 0),
    ).one()
    return {
        "ok": True,
        "metric": "sales_summary",
        "client": {"id": client.id, "name": client.nombre_completo} if client else None,
        "period": {"start": _json_value(start_date), "end": _json_value(end_date)},
        "sales": sale_count,
        "sold_quintals": round(quintals, 3),
        "totals_by_currency": totals_by_currency,
        "pending_requests": int(pending_count or 0),
        "pending_quintals": round(float(pending_quintals or 0), 3),
        "recent_sales": [
            {
                "id": row.id,
                "code": row.codigo,
                "date": _json_value(row.fecha),
                "client": row.cliente.nombre_completo if row.cliente else None,
                "currency": row.moneda,
                "total": round(float(row.total or 0), 2),
            }
            for row in sales
        ],
        "primary_value": round(quintals, 3),
        "summary": (
            f"Se registran {sale_count} ventas por {quintals:,.3f} quintales en el período; "
            f"quedan {int(pending_count or 0)} solicitudes activas."
        ),
    }


def business_overview(db: Session, _: dict[str, Any]) -> dict[str, Any]:
    clients = db.query(func.count(Cliente.id)).filter(Cliente.activo.is_(True)).scalar()
    farms = db.query(func.count(Finca.id)).filter(Finca.activa.is_(True)).scalar()
    active_lots = db.query(func.count(OrdenTrabajo.id)).filter(
        OrdenTrabajo.estado.in_(["abierta", "en_proceso", "pendiente_aprobacion"])
    ).scalar()
    workers = db.query(func.count(TrabajadorFinca.id)).filter(TrabajadorFinca.activo.is_(True)).scalar()
    coffee = coffee_received(db, {"year": date.today().year})
    patio = coffee_in_patio(db, {})
    return {
        "ok": True,
        "metric": "business_overview",
        "clients": int(clients or 0),
        "farms": int(farms or 0),
        "workers": int(workers or 0),
        "active_lots": int(active_lots or 0),
        "coffee_received_fanegas": coffee.get("fanegas", 0),
        "coffee_in_patio_fanegas": patio.get("fanegas", 0),
        "primary_value": int(active_lots or 0),
        "summary": (
            f"NAVIA registra {int(farms or 0)} fincas, {int(workers or 0)} trabajadores y "
            f"{int(active_lots or 0)} lotes activos."
        ),
    }


def search_records(db: Session, entity: str, query: str, limit: int = 8) -> dict[str, Any]:
    limit = max(1, min(int(limit or 8), 20))
    needle = f"%{str(query or '').strip()}%"
    entity = str(entity or "").strip().lower()
    if not str(query or "").strip():
        return {"ok": False, "needs_clarification": True, "message": "Indique qué texto desea buscar."}

    if entity == "client":
        rows = db.query(Cliente).filter(or_(Cliente.nombre_completo.ilike(needle), Cliente.codigo.ilike(needle))).limit(limit).all()
        data = [{"id": row.id, "code": row.codigo, "name": row.nombre_completo, "active": row.activo} for row in rows]
    elif entity == "farm":
        rows = db.query(Finca).filter(or_(Finca.nombre.ilike(needle), Finca.codigo.ilike(needle))).limit(limit).all()
        data = [{"id": row.id, "code": row.codigo, "name": row.nombre, "client": row.cliente.nombre_completo if row.cliente else None} for row in rows]
    elif entity == "worker":
        rows = db.query(TrabajadorFinca).filter(or_(TrabajadorFinca.nombre.ilike(needle), TrabajadorFinca.codigo.ilike(needle))).limit(limit).all()
        data = [{"id": row.id, "code": row.codigo, "name": row.nombre, "position": row.puesto, "active": row.activo} for row in rows]
    elif entity == "lot":
        rows = db.query(OrdenTrabajo).filter(OrdenTrabajo.codigo_lote.ilike(needle)).limit(limit).all()
        data = [{"id": row.id, "code": row.codigo_lote, "status": row.estado, "process": row.proceso} for row in rows]
    elif entity == "receipt":
        rows = db.query(ReciboCafe).filter(or_(ReciboCafe.numero_recibo.ilike(needle), ReciboCafe.productor_nombre.ilike(needle))).limit(limit).all()
        data = [{"id": row.id, "number": row.numero_recibo, "date": _json_value(row.fecha), "producer": row.productor_nombre, "status": row.estado} for row in rows]
    elif entity == "user":
        rows = db.query(Usuario).filter(or_(Usuario.nombre.ilike(needle), Usuario.correo.ilike(needle))).limit(limit).all()
        data = [{"id": row.id, "name": row.nombre, "role": row.rol, "active": row.activo} for row in rows]
    else:
        return {"ok": False, "message": "Entidad de búsqueda no permitida."}
    return {"ok": True, "entity": entity, "count": len(data), "records": data}


def execute_metric(db: Session, metric_key: str, parameters: dict[str, Any] | None = None) -> dict[str, Any]:
    params = parameters or {}
    handlers = {
        "farm_spend": farm_spend,
        "coffee_received": coffee_received,
        "coffee_in_patio": coffee_in_patio,
        "worker_status": worker_status,
        "pending_work": pending_work,
        "latest_alerts": latest_alerts,
        "lot_qr": lot_qr_for_client,
        "lot_status": lot_status,
        "farm_activity": farm_activity,
        "days_since_fertilization": days_since_fertilization,
        "farm_sensor_analysis": farm_sensor_analysis,
        "inventory_status": inventory_status,
        "sales_summary": sales_summary,
        "business_overview": business_overview,
    }
    handler = handlers.get(str(metric_key or "").strip().lower())
    if not handler:
        return {"ok": False, "message": "Métrica no permitida."}
    try:
        return handler(db, params)
    except ValueError as exc:
        return {"ok": False, "needs_clarification": True, "message": str(exc)}
