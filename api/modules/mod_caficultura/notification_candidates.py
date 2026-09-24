from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from typing import Any

from sqlalchemy.orm import Session, selectinload

from config import (
    NOTIFICATION_ALERT_COOLDOWN_HOURS,
    NOTIFICATION_PENDING_APPROVAL_HOURS,
    NOTIFICATION_PENDING_SALE_DAYS,
    NOTIFICATION_STALE_OT_HOURS,
    NOTIFICATION_UNASSIGNED_RECEIPT_HOURS,
    NOTIFICATION_WARNING_COOLDOWN_HOURS,
)
from modules.mod_caficultura.models import InsumoFinca, OrdenTrabajo, ReciboCafe, SolicitudVenta
from core.models import Usuario


@dataclass(slots=True)
class NotificationCandidate:
    category: str
    entity_type: str
    entity_id: str
    severity: str
    title: str
    facts: dict[str, Any]
    recipient_user_ids: list[int]
    recommended_action: str
    cooldown_hours: int = NOTIFICATION_WARNING_COOLDOWN_HOURS
    force_send: bool = False
    context: dict[str, Any] = field(default_factory=dict)

    @property
    def base_key(self) -> str:
        return f"{self.category}:{self.entity_type}:{self.entity_id}"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _date_to_utc(value: date | None) -> datetime | None:
    if value is None:
        return None
    return datetime.combine(value, time.min, tzinfo=timezone.utc)


def _unique_ids(*groups: list[int | None]) -> list[int]:
    result: list[int] = []
    seen: set[int] = set()
    for group in groups:
        for raw in group:
            if raw is None:
                continue
            value = int(raw)
            if value not in seen:
                result.append(value)
                seen.add(value)
    return result


def _management_user_ids(db: Session) -> list[int]:
    rows = (
        db.query(Usuario.id)
        .filter(
            Usuario.activo.is_(True),
            (Usuario.rol.in_(["gerente", "administrativo"])) | (Usuario.is_superadmin.is_(True)),
        )
        .all()
    )
    return [int(row[0]) for row in rows]


def _latest_ot_activity(row: OrdenTrabajo) -> datetime:
    values: list[datetime] = []
    for seguimiento in row.seguimientos or []:
        candidate = _aware(seguimiento.marca_tiempo) or _aware(seguimiento.created_at) or _date_to_utc(seguimiento.fecha)
        if candidate:
            values.append(candidate)
    for value in (_aware(row.updated_at), _aware(row.created_at), _date_to_utc(row.fecha_inicio)):
        if value:
            values.append(value)
    return max(values) if values else utcnow()


def _latest_alert_message(row: OrdenTrabajo) -> str | None:
    alerts = [item for item in (row.seguimientos or []) if bool(item.dar_alerta)]
    alerts.sort(
        key=lambda item: _aware(item.marca_tiempo) or _aware(item.created_at) or _date_to_utc(item.fecha) or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )
    for alert in alerts:
        message = (alert.alerta_mensaje or alert.comentario or "").strip()
        if message:
            return message[:1000]
    return None


def detect_critical_ot_alerts(db: Session, now: datetime, managers: list[int]) -> list[NotificationCandidate]:
    rows = (
        db.query(OrdenTrabajo)
        .options(selectinload(OrdenTrabajo.seguimientos), selectinload(OrdenTrabajo.operario))
        .filter(OrdenTrabajo.estado == "alerta")
        .order_by(OrdenTrabajo.updated_at.asc())
        .all()
    )
    candidates: list[NotificationCandidate] = []
    for row in rows:
        recipients = _unique_ids([row.operario_id], managers)
        if not recipients:
            continue
        message = _latest_alert_message(row) or "El lote fue marcado con una alerta operativa que requiere revisión."
        candidates.append(
            NotificationCandidate(
                category="ot_critical_alert",
                entity_type="orden_trabajo",
                entity_id=str(row.id),
                severity="critical",
                title=f"Alerta crítica en lote {row.codigo_lote}",
                facts={
                    "codigo_lote": row.codigo_lote,
                    "proceso": row.proceso,
                    "estado": row.estado,
                    "fecha_inicio": row.fecha_inicio.isoformat() if row.fecha_inicio else None,
                    "alerta": message,
                    "horas_desde_ultima_actividad": round((now - _latest_ot_activity(row)).total_seconds() / 3600, 1),
                },
                recipient_user_ids=recipients,
                recommended_action="Revise el lote, confirme la causa de la alerta y registre la acción correctiva en NAVIA.",
                cooldown_hours=NOTIFICATION_ALERT_COOLDOWN_HOURS,
                force_send=True,
            )
        )
    return candidates


def detect_pending_approvals(db: Session, now: datetime, managers: list[int]) -> list[NotificationCandidate]:
    cutoff = now - timedelta(hours=NOTIFICATION_PENDING_APPROVAL_HOURS)
    rows = (
        db.query(OrdenTrabajo)
        .options(selectinload(OrdenTrabajo.operario))
        .filter(OrdenTrabajo.estado == "pendiente_aprobacion", OrdenTrabajo.updated_at <= cutoff)
        .order_by(OrdenTrabajo.updated_at.asc())
        .all()
    )
    candidates: list[NotificationCandidate] = []
    for row in rows:
        updated_at = _aware(row.updated_at) or now
        hours = max(0.0, (now - updated_at).total_seconds() / 3600)
        recipients = _unique_ids(managers)
        if not recipients:
            continue
        candidates.append(
            NotificationCandidate(
                category="ot_pending_approval",
                entity_type="orden_trabajo",
                entity_id=str(row.id),
                severity="high" if hours >= NOTIFICATION_PENDING_APPROVAL_HOURS * 2 else "warning",
                title=f"Lote {row.codigo_lote} pendiente de aprobación",
                facts={
                    "codigo_lote": row.codigo_lote,
                    "proceso": row.proceso,
                    "horas_pendiente": round(hours, 1),
                    "tiene_operario_asignado": bool(row.operario_id),
                },
                recipient_user_ids=recipients,
                recommended_action="Revise la solicitud de cierre y apruebe o devuelva el lote con observaciones.",
                cooldown_hours=NOTIFICATION_WARNING_COOLDOWN_HOURS,
            )
        )
    return candidates


def detect_stale_active_ots(db: Session, now: datetime, managers: list[int]) -> list[NotificationCandidate]:
    rows = (
        db.query(OrdenTrabajo)
        .options(selectinload(OrdenTrabajo.seguimientos), selectinload(OrdenTrabajo.operario))
        .filter(OrdenTrabajo.estado.in_(["abierta", "en_proceso"]))
        .order_by(OrdenTrabajo.updated_at.asc())
        .all()
    )
    candidates: list[NotificationCandidate] = []
    threshold = float(NOTIFICATION_STALE_OT_HOURS)
    for row in rows:
        last_activity = _latest_ot_activity(row)
        hours = max(0.0, (now - last_activity).total_seconds() / 3600)
        if hours < threshold:
            continue
        recipients = _unique_ids([row.operario_id], managers)
        if not recipients:
            continue
        candidates.append(
            NotificationCandidate(
                category="ot_without_followup",
                entity_type="orden_trabajo",
                entity_id=str(row.id),
                severity="high" if hours >= threshold * 2 else "warning",
                title=f"Lote {row.codigo_lote} sin seguimiento reciente",
                facts={
                    "codigo_lote": row.codigo_lote,
                    "proceso": row.proceso,
                    "estado": row.estado,
                    "horas_sin_actividad": round(hours, 1),
                    "tiene_operario_asignado": bool(row.operario_id),
                },
                recipient_user_ids=recipients,
                recommended_action="Confirme el estado real del lote y registre un seguimiento actualizado.",
                cooldown_hours=NOTIFICATION_WARNING_COOLDOWN_HOURS,
            )
        )
    return candidates


def detect_low_inventory(db: Session, managers: list[int]) -> list[NotificationCandidate]:
    rows = (
        db.query(InsumoFinca)
        .filter(
            InsumoFinca.activo.is_(True),
            InsumoFinca.stock_minimo.is_not(None),
            InsumoFinca.stock_actual <= InsumoFinca.stock_minimo,
        )
        .order_by(InsumoFinca.stock_actual.asc())
        .all()
    )
    candidates: list[NotificationCandidate] = []
    for row in rows:
        recipients = _unique_ids(managers)
        if not recipients:
            continue
        stock = float(row.stock_actual or 0)
        minimum = float(row.stock_minimo or 0)
        candidates.append(
            NotificationCandidate(
                category="inventory_below_minimum",
                entity_type="insumo",
                entity_id=str(row.id),
                severity="critical" if stock <= 0 else "high",
                title=f"Inventario bajo: {row.nombre}",
                facts={
                    "codigo": row.codigo,
                    "insumo": row.nombre,
                    "unidad": row.unidad,
                    "stock_actual": stock,
                    "stock_minimo": minimum,
                },
                recipient_user_ids=recipients,
                recommended_action="Revise consumos pendientes y gestione la reposición del insumo.",
                cooldown_hours=NOTIFICATION_WARNING_COOLDOWN_HOURS,
                force_send=stock <= 0,
            )
        )
    return candidates


def detect_pending_sales(db: Session, now: datetime, managers: list[int]) -> list[NotificationCandidate]:
    cutoff = now - timedelta(days=NOTIFICATION_PENDING_SALE_DAYS)
    rows = (
        db.query(SolicitudVenta)
        .options(selectinload(SolicitudVenta.cliente))
        .filter(SolicitudVenta.estado == "pendiente", SolicitudVenta.created_at <= cutoff)
        .order_by(SolicitudVenta.created_at.asc())
        .all()
    )
    candidates: list[NotificationCandidate] = []
    for row in rows:
        created_at = _aware(row.created_at) or now
        days = max(0.0, (now - created_at).total_seconds() / 86400)
        recipients = _unique_ids(managers)
        if not recipients:
            continue
        candidates.append(
            NotificationCandidate(
                category="sale_request_pending",
                entity_type="solicitud_venta",
                entity_id=str(row.id),
                severity="high" if days >= NOTIFICATION_PENDING_SALE_DAYS * 2 else "warning",
                title=f"Solicitud de venta {row.codigo} sin asignación",
                facts={
                    "codigo": row.codigo,
                    "dias_pendiente": round(days, 1),
                    "cantidad_quintales": float(row.cantidad_quintales or 0),
                    "proceso_preferido": row.proceso_preferido,
                    "tiene_cliente_asignado": bool(row.cliente_id),
                },
                recipient_user_ids=recipients,
                recommended_action="Revise disponibilidad de lotes, contacte al cliente y actualice el estado comercial.",
                cooldown_hours=NOTIFICATION_WARNING_COOLDOWN_HOURS,
            )
        )
    return candidates


def detect_unassigned_receipts(db: Session, now: datetime, managers: list[int]) -> list[NotificationCandidate]:
    cutoff = now - timedelta(hours=NOTIFICATION_UNASSIGNED_RECEIPT_HOURS)
    cutoff_date = cutoff.date()
    rows = (
        db.query(ReciboCafe)
        .options(selectinload(ReciboCafe.recibos_ot))
        .filter(ReciboCafe.estado == "recibido", ReciboCafe.fecha <= cutoff_date)
        .order_by(ReciboCafe.fecha.asc())
        .all()
    )
    candidates: list[NotificationCandidate] = []
    for row in rows:
        if row.recibos_ot:
            continue
        receipt_dt = _date_to_utc(row.fecha) or now
        hours = max(0.0, (now - receipt_dt).total_seconds() / 3600)
        recipients = _unique_ids(managers)
        if not recipients:
            continue
        candidates.append(
            NotificationCandidate(
                category="receipt_without_lot",
                entity_type="recibo_cafe",
                entity_id=str(row.id),
                severity="warning",
                title=f"Recibo {row.numero_recibo} aún no asignado a lote",
                facts={
                    "numero_recibo": row.numero_recibo,
                    "fecha": row.fecha.isoformat() if row.fecha else None,
                    "fanegas_estimadas": round((float(row.cajuelas or 0) + float(row.cuartillos or 0) / 4) / 20, 3),
                    "horas_sin_asignar": round(hours, 1),
                },
                recipient_user_ids=recipients,
                recommended_action="Valide el recibo y asígnelo a una orden de trabajo o documente la razón de la espera.",
                cooldown_hours=NOTIFICATION_WARNING_COOLDOWN_HOURS,
            )
        )
    return candidates


def collect_candidates(db: Session, now: datetime | None = None) -> list[NotificationCandidate]:
    current = _aware(now) or utcnow()
    managers = _management_user_ids(db)
    candidates: list[NotificationCandidate] = []
    candidates.extend(detect_critical_ot_alerts(db, current, managers))
    candidates.extend(detect_pending_approvals(db, current, managers))
    candidates.extend(detect_stale_active_ots(db, current, managers))
    candidates.extend(detect_low_inventory(db, managers))
    candidates.extend(detect_pending_sales(db, current, managers))
    candidates.extend(detect_unassigned_receipts(db, current, managers))
    return candidates
