from __future__ import annotations

import math
import unicodedata
from collections import defaultdict
from datetime import date, datetime
from difflib import SequenceMatcher
from typing import Any, Callable

from sqlalchemy import or_
from sqlalchemy.orm import Session, joinedload

from modules.mod_caficultura.models import (
    Cliente,
    DocumentoLote,
    Finca,
    InsumoFinca,
    OrdenTrabajo,
    ReciboCafe,
    RegistroFinca,
    SolicitudSalidaVenta,
    SolicitudVenta,
    SolicitudVentaDocumento,
    TrabajadorFinca,
)
from modules.mod_caficultura.model_notifications import NotificationEvent
from core.models import Usuario


ACTIVE_LOT_STATUSES = {"abierta", "en_proceso", "pendiente_aprobacion"}
MAX_QUERY_ROWS = 100
MAX_DOCUMENT_ROWS = 40


def _norm(value: object) -> str:
    normalized = unicodedata.normalize("NFD", str(value or "").strip().lower())
    return "".join(char for char in normalized if unicodedata.category(char) != "Mn")


def _safe_float(value: object) -> float:
    try:
        numeric = float(value or 0)
    except (TypeError, ValueError):
        return 0
    return 0 if math.isnan(numeric) or math.isinf(numeric) else numeric


def _receipt_payment_filter(value: object) -> str | None:
    normalized = _norm(value).replace("_", " ")
    if not normalized:
        return None
    if any(phrase in normalized for phrase in ("no liquid", "sin liquid", "no pag", "sin pag", "por pagar")):
        return "pendiente"
    tokens = normalized.split()
    pending_targets = ("pendiente", "pendientes", "impago", "impagos")
    paid_targets = ("liquidado", "liquidados", "liquidada", "liquidadas", "pagado", "pagados", "pagada", "pagadas")
    if any(SequenceMatcher(None, token, target).ratio() >= 0.78 for token in tokens for target in pending_targets):
        return "pendiente"
    if any(SequenceMatcher(None, token, target).ratio() >= 0.76 for token in tokens for target in paid_targets):
        return "liquidado"
    return None


def _iso(value: date | datetime | None) -> str | None:
    return value.isoformat() if value else None


def _parse_period(parameters: dict[str, Any]) -> tuple[date | None, date | None]:
    start_value = parameters.get("start_date")
    end_value = parameters.get("end_date")
    year_value = parameters.get("year")
    try:
        start = date.fromisoformat(str(start_value)) if start_value else None
        end = date.fromisoformat(str(end_value)) if end_value else None
        if year_value is not None:
            year = int(year_value)
            if year < 2000 or year > date.today().year + 1:
                raise ValueError("El año está fuera del rango permitido")
            start = start or date(year, 1, 1)
            end = end or date(year, 12, 31)
    except (TypeError, ValueError) as exc:
        raise ValueError("Use fechas AAAA-MM-DD y un año válido") from exc
    if start and end and start > end:
        raise ValueError("La fecha inicial no puede ser posterior a la fecha final")
    return start, end


def _limit(parameters: dict[str, Any]) -> int:
    try:
        return max(1, min(int(parameters.get("limit") or 25), MAX_QUERY_ROWS))
    except (TypeError, ValueError):
        return 25


def _table(
    *,
    title: str,
    subtitle: str,
    columns: list[dict[str, Any]],
    rows: list[dict[str, Any]],
    total: int,
    filename: str,
) -> dict[str, Any]:
    return {
        "type": "data_table",
        "title": title,
        "subtitle": subtitle,
        "columns": columns,
        "rows": rows,
        "total": total,
        "truncated": total > len(rows),
        "export_filename": filename,
    }


def _chart(
    *,
    title: str,
    subtitle: str,
    variant: str,
    items: list[dict[str, Any]],
    value_format: str = "number",
) -> dict[str, Any]:
    return {
        "type": "chart",
        "title": title,
        "subtitle": subtitle,
        "variant": variant if variant in {"bar", "donut", "line"} else "bar",
        "items": items[:30],
        "value_format": value_format,
    }


def _group_items(
    rows: list[dict[str, Any]],
    key: str,
    value: str | None = None,
) -> list[dict[str, Any]]:
    totals: dict[str, float] = defaultdict(float)
    for row in rows:
        label = str(row.get(key) or "Sin dato")
        totals[label] += _safe_float(row.get(value)) if value else 1
    return [
        {"label": label, "value": round(amount, 3)}
        for label, amount in sorted(totals.items(), key=lambda item: (-item[1], item[0]))
    ]


def _query_receipts(db: Session, parameters: dict[str, Any]) -> dict[str, Any]:
    start, end = _parse_period(parameters)
    query = db.query(ReciboCafe).options(joinedload(ReciboCafe.cliente), joinedload(ReciboCafe.finca))
    if start:
        query = query.filter(ReciboCafe.fecha >= start)
    if end:
        query = query.filter(ReciboCafe.fecha <= end)
    raw_status = parameters.get("status")
    payment_filter = _receipt_payment_filter(raw_status)
    status = _norm(raw_status)
    if payment_filter == "liquidado":
        query = query.filter(ReciboCafe.liquidado.is_(True))
    elif payment_filter == "pendiente":
        query = query.filter(ReciboCafe.liquidado.is_(False), ReciboCafe.estado != "anulado")
    elif status:
        query = query.filter(ReciboCafe.estado.ilike(str(parameters["status"])))
    else:
        query = query.filter(ReciboCafe.estado != "anulado")
    search = str(parameters.get("search") or "").strip()
    if search:
        like = f"%{search}%"
        query = query.filter(
            or_(
                ReciboCafe.numero_recibo.ilike(like),
                ReciboCafe.productor_nombre.ilike(like),
                ReciboCafe.productor_cedula.ilike(like),
                ReciboCafe.cliente.has(Cliente.nombre_completo.ilike(like)),
                ReciboCafe.finca.has(Finca.nombre.ilike(like)),
            )
        )
    client_name = str(parameters.get("client_name") or "").strip()
    if client_name:
        query = query.filter(ReciboCafe.cliente.has(Cliente.nombre_completo.ilike(f"%{client_name}%")))
    farm_name = str(parameters.get("farm_name") or "").strip()
    if farm_name:
        query = query.filter(ReciboCafe.finca.has(Finca.nombre.ilike(f"%{farm_name}%")))
    total = query.count()
    records = query.order_by(ReciboCafe.fecha.desc(), ReciboCafe.id.desc()).limit(_limit(parameters)).all()
    rows: list[dict[str, Any]] = []
    for receipt in records:
        fanegas = round((_safe_float(receipt.cajuelas) + _safe_float(receipt.cuartillos) / 4) / 20, 3)
        rows.append({
            "receipt": receipt.numero_recibo,
            "date": _iso(receipt.fecha),
            "producer": receipt.productor_nombre,
            "farm": receipt.finca.nombre if receipt.finca else "Sin finca",
            "fanegas": fanegas,
            "status": receipt.estado,
            "payment": "Liquidado" if receipt.liquidado else "Pendiente",
            "liquidated_at": _iso(receipt.liquidado_at),
            "liquidation_amount": round(_safe_float(receipt.liquidacion_monto), 2) if receipt.liquidado else None,
            "transfer": receipt.liquidacion_numero_transferencia if receipt.liquidado else None,
            "path": f"/recibos/{receipt.id}",
        })
    period = "histórico completo"
    if start or end:
        period = f"{_iso(start) or 'inicio'} a {_iso(end) or 'hoy'}"
    components: list[dict[str, Any]] = [_table(
        title="Recibos de café",
        subtitle=f"{total} recibos · {period}",
        columns=[
            {"key": "receipt", "label": "Recibo", "primary": True},
            {"key": "date", "label": "Fecha", "format": "date"},
            {"key": "producer", "label": "Productor"},
            {"key": "farm", "label": "Finca", "hide_on_mobile": True},
            {"key": "fanegas", "label": "Fanegas", "format": "decimal"},
            {"key": "payment", "label": "Pago", "format": "status"},
            {"key": "liquidation_amount", "label": "Monto liquidado", "format": "currency_crc"},
            {"key": "transfer", "label": "Transferencia", "hide_on_mobile": True},
            {"key": "path", "label": "Abrir", "format": "link", "link_label": "Ver"},
        ],
        rows=rows,
        total=total,
        filename="recibos-navia.csv",
    )]
    visualization = str(parameters.get("visualization") or "table")
    if visualization in {"bar", "donut", "line"}:
        group = str(parameters.get("group_by") or "month")
        if group == "status":
            items = _group_items(rows, "payment")
            value_format = "number"
            title = "Recibos por estado de pago"
        elif group == "farm":
            items = _group_items(rows, "farm", "fanegas")
            value_format = "fanegas"
            title = "Café recibido por finca"
        else:
            month_rows = [{**row, "month": str(row.get("date") or "")[:7]} for row in rows]
            items = sorted(_group_items(month_rows, "month", "fanegas"), key=lambda item: item["label"])
            value_format = "fanegas"
            title = "Ingreso de café por mes"
        components.append(_chart(
            title=title,
            subtitle="Basado en los registros mostrados",
            variant=visualization,
            items=items,
            value_format=value_format,
        ))
    return {
        "ok": True,
        "dataset": "receipts",
        "count": len(rows),
        "total_matches": total,
        "summary": f"Encontré {total} recibos. La tabla contiene {len(rows)} registros ordenados del más reciente al más antiguo.",
        "components": components,
    }


def _query_lots(db: Session, parameters: dict[str, Any], *, active_only: bool) -> dict[str, Any]:
    start, end = _parse_period(parameters)
    query = db.query(OrdenTrabajo).options(joinedload(OrdenTrabajo.finca), joinedload(OrdenTrabajo.operario))
    if active_only:
        query = query.filter(OrdenTrabajo.estado.in_(sorted(ACTIVE_LOT_STATUSES)))
    elif parameters.get("status"):
        query = query.filter(OrdenTrabajo.estado.ilike(str(parameters["status"])))
    if start:
        query = query.filter(OrdenTrabajo.fecha_inicio >= start)
    if end:
        query = query.filter(OrdenTrabajo.fecha_inicio <= end)
    search = str(parameters.get("search") or "").strip()
    if search:
        like = f"%{search}%"
        query = query.filter(
            or_(OrdenTrabajo.codigo_lote.ilike(like), OrdenTrabajo.proceso.ilike(like), OrdenTrabajo.finca.has(Finca.nombre.ilike(like)))
        )
    farm_name = str(parameters.get("farm_name") or "").strip()
    if farm_name:
        query = query.filter(OrdenTrabajo.finca.has(Finca.nombre.ilike(f"%{farm_name}%")))
    total = query.count()
    records = query.order_by(OrdenTrabajo.fecha_inicio.asc(), OrdenTrabajo.id.asc()).limit(_limit(parameters)).all()
    today = date.today()
    rows: list[dict[str, Any]] = []
    for lot in records:
        end_value = lot.fecha_cierre.date() if isinstance(lot.fecha_cierre, datetime) else lot.fecha_cierre
        elapsed = max(((end_value or today) - lot.fecha_inicio).days, 0)
        rows.append({
            "lot": lot.codigo_lote,
            "process": lot.proceso,
            "status": lot.estado,
            "farm": lot.finca.nombre if lot.finca else "Sin finca",
            "operator": lot.operario.nombre if lot.operario else "Sin asignar",
            "start": _iso(lot.fecha_inicio),
            "elapsed_days": elapsed,
            "fanegas": round(_safe_float(lot.fanegas_estimadas), 3),
            "path": f"/ot/{lot.codigo_lote}",
        })
    title = "Lotes activos" if active_only else "Lotes de café"
    components: list[dict[str, Any]] = [_table(
        title=title,
        subtitle=f"{total} lotes encontrados",
        columns=[
            {"key": "lot", "label": "Lote", "primary": True},
            {"key": "process", "label": "Proceso", "format": "status"},
            {"key": "status", "label": "Estado", "format": "status"},
            {"key": "farm", "label": "Finca"},
            {"key": "operator", "label": "Responsable", "hide_on_mobile": True},
            {"key": "start", "label": "Inicio", "format": "date"},
            {"key": "elapsed_days", "label": "Transcurrido", "format": "duration_days"},
            {"key": "fanegas", "label": "Fanegas", "format": "decimal"},
            {"key": "path", "label": "Abrir", "format": "link", "link_label": "Ver"},
        ],
        rows=rows,
        total=total,
        filename="lotes-activos-navia.csv" if active_only else "lotes-navia.csv",
    )]
    visualization = str(parameters.get("visualization") or "table")
    if visualization in {"bar", "donut", "line"}:
        group = str(parameters.get("group_by") or ("elapsed" if parameters.get("include_elapsed") else "status"))
        if group == "elapsed":
            items = [{"label": row["lot"], "value": row["elapsed_days"], "detail": row["status"]} for row in rows]
            chart_title = "Tiempo transcurrido por lote"
            value_format = "duration_days"
        elif group == "process":
            items = _group_items(rows, "process")
            chart_title = "Lotes por proceso"
            value_format = "number"
        elif group == "farm":
            items = _group_items(rows, "farm", "fanegas")
            chart_title = "Fanegas estimadas por finca"
            value_format = "fanegas"
        else:
            items = _group_items(rows, "status")
            chart_title = "Lotes por estado"
            value_format = "number"
        components.append(_chart(
            title=chart_title,
            subtitle=f"{len(rows)} registros visibles",
            variant=visualization,
            items=items,
            value_format=value_format,
        ))
    return {
        "ok": True,
        "dataset": "active_lots" if active_only else "lots",
        "count": len(rows),
        "total_matches": total,
        "summary": f"Encontré {total} lotes{' activos' if active_only else ''}. El tiempo se calcula desde la fecha de inicio hasta hoy o el cierre.",
        "components": components,
    }


def _query_sale_requests(db: Session, parameters: dict[str, Any]) -> dict[str, Any]:
    query = db.query(SolicitudVenta).options(joinedload(SolicitudVenta.cliente))
    if parameters.get("status"):
        query = query.filter(SolicitudVenta.estado.ilike(str(parameters["status"])))
    search = str(parameters.get("search") or parameters.get("client_name") or "").strip()
    if search:
        like = f"%{search}%"
        query = query.filter(
            or_(SolicitudVenta.codigo.ilike(like), SolicitudVenta.cliente.has(Cliente.nombre_completo.ilike(like)), SolicitudVenta.observaciones.ilike(like))
        )
    total = query.count()
    records = query.order_by(SolicitudVenta.created_at.desc()).limit(_limit(parameters)).all()
    rows = [{
        "request": row.codigo,
        "client": row.cliente.nombre_completo if row.cliente else "Sin cliente",
        "status": row.estado,
        "quintals": round(_safe_float(row.cantidad_quintales), 3),
        "process": row.proceso_preferido or "Flexible",
        "target_price": _safe_float(row.precio_objetivo) if row.precio_objetivo is not None else None,
        "currency": row.moneda,
        "created": _iso(row.created_at),
        "path": f"/solicitudes-venta/{row.id}",
    } for row in records]
    components: list[dict[str, Any]] = [_table(
        title="Solicitudes de venta",
        subtitle=f"{total} solicitudes encontradas",
        columns=[
            {"key": "request", "label": "Solicitud", "primary": True},
            {"key": "client", "label": "Cliente"},
            {"key": "status", "label": "Estado", "format": "status"},
            {"key": "quintals", "label": "Quintales", "format": "decimal"},
            {"key": "process", "label": "Proceso", "hide_on_mobile": True},
            {"key": "created", "label": "Creada", "format": "date"},
            {"key": "path", "label": "Abrir", "format": "link", "link_label": "Ver"},
        ],
        rows=rows,
        total=total,
        filename="solicitudes-venta-navia.csv",
    )]
    visualization = str(parameters.get("visualization") or "table")
    if visualization in {"bar", "donut", "line"}:
        components.append(_chart(
            title="Solicitudes por estado",
            subtitle="Distribución de los registros visibles",
            variant=visualization,
            items=_group_items(rows, "status"),
        ))
    return {"ok": True, "dataset": "sale_requests", "count": len(rows), "total_matches": total, "summary": f"Encontré {total} solicitudes de venta.", "components": components}


def _simple_query(
    db: Session,
    parameters: dict[str, Any],
    *,
    dataset: str,
    model: Any,
    active_column: Any | None,
    title: str,
    search_columns: list[Any],
    order_column: Any,
    row_factory: Callable[[Any], dict[str, Any]],
    columns: list[dict[str, Any]],
) -> dict[str, Any]:
    query = db.query(model)
    if active_column is not None and not parameters.get("status"):
        query = query.filter(active_column.is_(True))
    search = str(parameters.get("search") or "").strip()
    if search:
        like = f"%{search}%"
        query = query.filter(or_(*[column.ilike(like) for column in search_columns]))
    total = query.count()
    rows = [row_factory(row) for row in query.order_by(order_column).limit(_limit(parameters)).all()]
    component = _table(
        title=title,
        subtitle=f"{total} registros encontrados",
        columns=columns,
        rows=rows,
        total=total,
        filename=f"{dataset}-navia.csv",
    )
    return {"ok": True, "dataset": dataset, "count": len(rows), "total_matches": total, "summary": f"Encontré {total} registros en {title.lower()}.", "components": [component]}


def _query_farm_records(db: Session, parameters: dict[str, Any]) -> dict[str, Any]:
    start, end = _parse_period(parameters)
    query = db.query(RegistroFinca).options(
        joinedload(RegistroFinca.finca),
        joinedload(RegistroFinca.actividad),
        joinedload(RegistroFinca.trabajadores),
        joinedload(RegistroFinca.insumos),
    )
    if start:
        query = query.filter(RegistroFinca.fecha >= start)
    if end:
        query = query.filter(RegistroFinca.fecha <= end)
    if parameters.get("status"):
        query = query.filter(RegistroFinca.estado.ilike(str(parameters["status"])))
    farm_name = str(parameters.get("farm_name") or "").strip()
    if farm_name:
        query = query.filter(RegistroFinca.finca.has(Finca.nombre.ilike(f"%{farm_name}%")))
    total = query.count()
    records = query.order_by(RegistroFinca.fecha.desc(), RegistroFinca.id.desc()).limit(_limit(parameters)).all()
    rows = []
    for row in records:
        labor = sum(_safe_float(item.costo) for item in row.trabajadores)
        supplies = sum(_safe_float(item.costo_total) for item in row.insumos)
        rows.append({
            "date": _iso(row.fecha),
            "farm": row.finca.nombre if row.finca else "Sin finca",
            "activity": row.actividad.nombre if row.actividad else row.descripcion or "Actividad",
            "status": row.estado,
            "workers": len(row.trabajadores),
            "supplies": len(row.insumos),
            "cost": round(labor + supplies, 2),
            "path": f"/gestionfincas/{row.finca_id}" if row.finca_id else "/gestionfincas",
        })
    components: list[dict[str, Any]] = [_table(
        title="Registros de finca",
        subtitle=f"{total} actividades encontradas",
        columns=[
            {"key": "date", "label": "Fecha", "format": "date", "primary": True},
            {"key": "farm", "label": "Finca"},
            {"key": "activity", "label": "Actividad"},
            {"key": "status", "label": "Estado", "format": "status"},
            {"key": "workers", "label": "Personal", "format": "number", "hide_on_mobile": True},
            {"key": "cost", "label": "Costo", "format": "currency_crc"},
            {"key": "path", "label": "Abrir", "format": "link", "link_label": "Ver"},
        ],
        rows=rows,
        total=total,
        filename="registros-finca-navia.csv",
    )]
    visualization = str(parameters.get("visualization") or "table")
    if visualization in {"bar", "donut", "line"}:
        components.append(_chart(
            title="Costos por finca",
            subtitle="Mano de obra e insumos de los registros visibles",
            variant=visualization,
            items=_group_items(rows, "farm", "cost"),
            value_format="currency_crc",
        ))
    return {"ok": True, "dataset": "farm_records", "count": len(rows), "total_matches": total, "summary": f"Encontré {total} registros de trabajo en finca.", "components": components}


def _query_alerts(db: Session, parameters: dict[str, Any]) -> dict[str, Any]:
    query = db.query(NotificationEvent)
    if parameters.get("status"):
        query = query.filter(NotificationEvent.status.ilike(str(parameters["status"])))
    search = str(parameters.get("search") or "").strip()
    if search:
        like = f"%{search}%"
        query = query.filter(or_(NotificationEvent.title.ilike(like), NotificationEvent.message.ilike(like), NotificationEvent.category.ilike(like)))
    total = query.count()
    records = query.order_by(NotificationEvent.detected_at.desc()).limit(_limit(parameters)).all()
    rows = [{
        "detected": _iso(row.detected_at),
        "title": row.title,
        "category": row.category,
        "severity": row.severity,
        "status": row.status,
        "message": row.message,
    } for row in records]
    components: list[dict[str, Any]] = [_table(
        title="Alertas del sistema",
        subtitle=f"{total} alertas encontradas",
        columns=[
            {"key": "title", "label": "Alerta", "primary": True},
            {"key": "detected", "label": "Detectada", "format": "datetime"},
            {"key": "category", "label": "Categoría"},
            {"key": "severity", "label": "Severidad", "format": "status"},
            {"key": "status", "label": "Estado", "format": "status"},
            {"key": "message", "label": "Detalle", "hide_on_mobile": True},
        ],
        rows=rows,
        total=total,
        filename="alertas-navia.csv",
    )]
    visualization = str(parameters.get("visualization") or "table")
    if visualization in {"bar", "donut", "line"}:
        components.append(_chart(title="Alertas por severidad", subtitle="Distribución visible", variant=visualization, items=_group_items(rows, "severity")))
    return {"ok": True, "dataset": "alerts", "count": len(rows), "total_matches": total, "summary": f"Encontré {total} alertas.", "components": components}


def _query_sales(db: Session, parameters: dict[str, Any]) -> dict[str, Any]:
    start, end = _parse_period(parameters)
    query = db.query(SolicitudSalidaVenta).options(joinedload(SolicitudSalidaVenta.cliente))
    if start:
        query = query.filter(SolicitudSalidaVenta.fecha >= start)
    if end:
        query = query.filter(SolicitudSalidaVenta.fecha <= end)
    if parameters.get("status"):
        query = query.filter(SolicitudSalidaVenta.estado.ilike(str(parameters["status"])))
    total = query.count()
    records = query.order_by(SolicitudSalidaVenta.fecha.desc(), SolicitudSalidaVenta.id.desc()).limit(_limit(parameters)).all()
    rows = [{
        "sale": row.codigo,
        "date": _iso(row.fecha),
        "client": row.cliente.nombre_completo if row.cliente else "Sin cliente",
        "status": row.estado,
        "amount": round(_safe_float(row.total), 2),
        "currency": row.moneda,
        "path": f"/lotes-vendidos/{row.codigo}",
    } for row in records]
    components: list[dict[str, Any]] = [_table(
        title="Ventas y salidas",
        subtitle=f"{total} ventas encontradas",
        columns=[
            {"key": "sale", "label": "Venta", "primary": True},
            {"key": "date", "label": "Fecha", "format": "date"},
            {"key": "client", "label": "Cliente"},
            {"key": "status", "label": "Estado", "format": "status"},
            {"key": "amount", "label": "Total", "format": "decimal"},
            {"key": "currency", "label": "Moneda"},
            {"key": "path", "label": "Abrir", "format": "link", "link_label": "Ver"},
        ],
        rows=rows,
        total=total,
        filename="ventas-navia.csv",
    )]
    visualization = str(parameters.get("visualization") or "table")
    if visualization in {"bar", "donut", "line"}:
        components.append(_chart(title="Ventas por cliente", subtitle="Totales de registros visibles", variant=visualization, items=_group_items(rows, "client", "amount"), value_format="decimal"))
    return {"ok": True, "dataset": "sales", "count": len(rows), "total_matches": total, "summary": f"Encontré {total} ventas.", "components": components}


def query_operational_data(db: Session, dataset: str, parameters: dict[str, Any]) -> dict[str, Any]:
    try:
        if dataset == "receipts":
            return _query_receipts(db, parameters)
        if dataset == "active_lots":
            return _query_lots(db, parameters, active_only=True)
        if dataset == "lots":
            return _query_lots(db, parameters, active_only=False)
        if dataset == "sale_requests":
            return _query_sale_requests(db, parameters)
        if dataset == "farm_records":
            return _query_farm_records(db, parameters)
        if dataset == "alerts":
            return _query_alerts(db, parameters)
        if dataset == "sales":
            return _query_sales(db, parameters)
        if dataset == "clients":
            return _simple_query(
                db, parameters, dataset=dataset, model=Cliente, active_column=Cliente.activo,
                title="Clientes", search_columns=[Cliente.codigo, Cliente.nombre_completo, Cliente.numero_identificacion],
                order_column=Cliente.nombre_completo.asc(),
                row_factory=lambda row: {"code": row.codigo, "name": row.nombre_completo, "category": row.categoria, "phone": row.telefono, "email": row.correo, "path": f"/clientes/{row.id}"},
                columns=[{"key": "code", "label": "Código", "primary": True}, {"key": "name", "label": "Cliente"}, {"key": "category", "label": "Categoría", "format": "status"}, {"key": "phone", "label": "Teléfono", "hide_on_mobile": True}, {"key": "email", "label": "Correo", "hide_on_mobile": True}, {"key": "path", "label": "Abrir", "format": "link", "link_label": "Ver"}],
            )
        if dataset == "farms":
            return _simple_query(
                db, parameters, dataset=dataset, model=Finca, active_column=Finca.activa,
                title="Fincas", search_columns=[Finca.codigo, Finca.nombre, Finca.propietario], order_column=Finca.nombre.asc(),
                row_factory=lambda row: {"code": row.codigo, "name": row.nombre, "owner": row.propietario or (row.cliente.nombre_completo if row.cliente else None), "location": ", ".join(filter(None, [row.distrito, row.canton, row.provincia])), "area": row.area, "path": f"/fincas/{row.id}"},
                columns=[{"key": "code", "label": "Código", "primary": True}, {"key": "name", "label": "Finca"}, {"key": "owner", "label": "Propietario"}, {"key": "location", "label": "Ubicación", "hide_on_mobile": True}, {"key": "area", "label": "Área", "format": "decimal"}, {"key": "path", "label": "Abrir", "format": "link", "link_label": "Ver"}],
            )
        if dataset == "workers":
            return _simple_query(
                db, parameters, dataset=dataset, model=TrabajadorFinca, active_column=TrabajadorFinca.activo,
                title="Trabajadores", search_columns=[TrabajadorFinca.codigo, TrabajadorFinca.nombre, TrabajadorFinca.identificacion], order_column=TrabajadorFinca.nombre.asc(),
                row_factory=lambda row: {"code": row.codigo, "name": row.nombre, "role": row.puesto, "phone": row.telefono, "daily_wage": row.jornal_diario},
                columns=[{"key": "code", "label": "Código", "primary": True}, {"key": "name", "label": "Trabajador"}, {"key": "role", "label": "Puesto", "format": "status"}, {"key": "phone", "label": "Teléfono", "hide_on_mobile": True}, {"key": "daily_wage", "label": "Jornal", "format": "currency_crc"}],
            )
        if dataset == "inventory":
            result = _simple_query(
                db, parameters, dataset=dataset, model=InsumoFinca, active_column=InsumoFinca.activo,
                title="Inventario de insumos", search_columns=[InsumoFinca.codigo, InsumoFinca.nombre, InsumoFinca.tipo], order_column=InsumoFinca.nombre.asc(),
                row_factory=lambda row: {"code": row.codigo, "name": row.nombre, "type": row.tipo, "stock": round(_safe_float(row.stock_actual), 3), "minimum": row.stock_minimo, "unit": row.unidad, "status": "Bajo mínimo" if row.stock_minimo is not None and _safe_float(row.stock_actual) <= _safe_float(row.stock_minimo) else "Disponible", "path": f"/insumos/{row.codigo}" if row.codigo else "/insumos"},
                columns=[{"key": "code", "label": "Código", "primary": True}, {"key": "name", "label": "Insumo"}, {"key": "type", "label": "Tipo", "hide_on_mobile": True}, {"key": "stock", "label": "Existencia", "format": "decimal"}, {"key": "unit", "label": "Unidad"}, {"key": "status", "label": "Estado", "format": "status"}, {"key": "path", "label": "Abrir", "format": "link", "link_label": "Ver"}],
            )
            visualization = str(parameters.get("visualization") or "table")
            if visualization in {"bar", "donut", "line"}:
                result["components"].append(_chart(title="Existencias por insumo", subtitle="Inventario visible", variant=visualization, items=[{"label": row["name"], "value": row["stock"], "detail": row["unit"]} for row in result["components"][0]["rows"]], value_format="decimal"))
            return result
        if dataset == "users":
            return _simple_query(
                db, parameters, dataset=dataset, model=Usuario, active_column=Usuario.activo,
                title="Usuarios", search_columns=[Usuario.nombre, Usuario.correo, Usuario.rol], order_column=Usuario.nombre.asc(),
                row_factory=lambda row: {"name": row.nombre, "email": row.correo, "role": row.rol, "status": "Activo" if row.activo else "Inactivo"},
                columns=[{"key": "name", "label": "Usuario", "primary": True}, {"key": "email", "label": "Correo"}, {"key": "role", "label": "Rol", "format": "status"}, {"key": "status", "label": "Estado", "format": "status"}],
            )
        return {"ok": False, "message": "El conjunto de datos solicitado no está autorizado."}
    except ValueError as exc:
        return {"ok": False, "needs_clarification": True, "message": str(exc)}


def search_documents(db: Session, query: str, document_type: str | None = None, limit: int = 12) -> dict[str, Any]:
    max_rows = max(1, min(int(limit or 12), MAX_DOCUMENT_ROWS))
    items: list[dict[str, Any]] = []
    raw_query = str(query or "").strip()
    raw_type = str(document_type or "").strip()
    like = f"%{raw_query}%"

    lot_query = db.query(DocumentoLote).options(joinedload(DocumentoLote.ot))
    if raw_query:
        lot_query = lot_query.filter(or_(
            DocumentoLote.titulo.ilike(like),
            DocumentoLote.tipo.ilike(like),
            DocumentoLote.descripcion.ilike(like),
            DocumentoLote.file_name.ilike(like),
            DocumentoLote.ot.has(OrdenTrabajo.codigo_lote.ilike(like)),
        ))
    if raw_type:
        lot_query = lot_query.filter(DocumentoLote.tipo.ilike(f"%{raw_type}%"))
    lot_total = lot_query.count()
    lot_docs = lot_query.order_by(DocumentoLote.created_at.desc()).limit(max_rows).all()
    for row in lot_docs:
        source = row.ot.codigo_lote if row.ot else "Lote"
        items.append({
            "title": row.titulo,
            "file_name": row.file_name,
            "document_type": row.tipo,
            "media_type": row.content_type,
            "size_bytes": row.size_bytes,
            "download_url": row.file_url,
            "source_label": f"Lote {source}",
            "source_path": f"/ot/{source}" if row.ot else None,
            "created_at": _iso(row.created_at),
        })

    sale_query = db.query(SolicitudVentaDocumento).options(joinedload(SolicitudVentaDocumento.solicitud))
    if raw_query:
        sale_query = sale_query.filter(or_(
            SolicitudVentaDocumento.titulo.ilike(like),
            SolicitudVentaDocumento.tipo.ilike(like),
            SolicitudVentaDocumento.descripcion.ilike(like),
            SolicitudVentaDocumento.file_name.ilike(like),
            SolicitudVentaDocumento.solicitud.has(SolicitudVenta.codigo.ilike(like)),
        ))
    if raw_type:
        sale_query = sale_query.filter(SolicitudVentaDocumento.tipo.ilike(f"%{raw_type}%"))
    sale_total = sale_query.count()
    sale_docs = sale_query.order_by(SolicitudVentaDocumento.created_at.desc()).limit(max_rows).all()
    for row in sale_docs:
        source = row.solicitud.codigo if row.solicitud else "Solicitud"
        items.append({
            "title": row.titulo,
            "file_name": row.file_name,
            "document_type": row.tipo,
            "media_type": row.content_type,
            "size_bytes": row.size_bytes,
            "download_url": row.file_url,
            "source_label": f"Solicitud {source}",
            "source_path": f"/solicitudes-venta/{row.solicitud_id}",
            "created_at": _iso(row.created_at),
        })

    proof_query = db.query(ReciboCafe).filter(ReciboCafe.liquidacion_comprobante_storage_key.is_not(None))
    if raw_query:
        proof_query = proof_query.filter(or_(
            ReciboCafe.numero_recibo.ilike(like),
            ReciboCafe.productor_nombre.ilike(like),
            ReciboCafe.liquidacion_comprobante_nombre.ilike(like),
            ReciboCafe.liquidacion_numero_transferencia.ilike(like),
        ))
    normalized_type = _norm(raw_type)
    proof_type_allowed = not normalized_type or any(token in normalized_type for token in ("comprobante", "transferencia", "liquidacion"))
    proof_total = proof_query.count() if proof_type_allowed else 0
    proofs = proof_query.order_by(ReciboCafe.liquidado_at.desc()).limit(max_rows).all() if proof_type_allowed else []
    for row in proofs:
        items.append({
            "title": f"Comprobante de {row.numero_recibo}",
            "file_name": row.liquidacion_comprobante_nombre or "comprobante",
            "document_type": "comprobante_transferencia",
            "media_type": row.liquidacion_comprobante_tipo,
            "size_bytes": row.liquidacion_comprobante_tamano,
            "download_url": f"/recibos/{row.id}/liquidacion/comprobante",
            "source_label": f"Recibo {row.numero_recibo}",
            "source_path": f"/recibos/{row.id}",
            "created_at": _iso(row.liquidado_at),
        })

    items.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)
    selected = items[:max_rows]
    total_matches = lot_total + sale_total + proof_total
    component = {
        "type": "document_list",
        "title": "Documentos encontrados",
        "subtitle": f"{total_matches} coincidencias en lotes, ventas y liquidaciones",
        "items": selected,
        "total": total_matches,
        "truncated": total_matches > len(selected),
    }
    return {
        "ok": True,
        "count": len(selected),
        "total_matches": total_matches,
        "summary": f"Encontré {total_matches} documentos relacionados con '{query or 'la búsqueda'}'.",
        "components": [component],
    }
