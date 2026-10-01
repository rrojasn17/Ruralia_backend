from __future__ import annotations

import csv
import hashlib
import hmac
import io
import json
import math
import re
import unicodedata
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import (
    APIRouter,
    Depends,
    Header,
    HTTPException,
    Query,
    Request,
    Response,
    status,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from config import (
    IOT_INGEST_RATE_LIMIT_PER_MINUTE,
    IOT_MAX_EXPORT_ROWS,
    IOT_MAX_INGEST_BYTES,
    IOT_MAX_QUERY_ROWS,
    IOT_MAX_VARIABLES,
    IOT_TTN_WEBHOOK_SECRET,
)
from database import get_db
from modules.mod_caficultura.models import Cliente, Finca
from modules.mod_caficultura.model_iot import (
    IoTDashboardPreset,
    IoTIntegration,
    IoTNode,
    IoTReading,
)
from core.models import Usuario
from rate_limit import enforce_rate_limit
from core.routers.auth import get_current_user, require_roles
from modules.mod_caficultura.schema_iot import (
    IoTCredentialOut,
    IoTIntegrationCreate,
    IoTIntegrationCreatedOut,
    IoTIntegrationOut,
    IoTIntegrationSecretOut,
    IoTIntegrationUpdate,
    IoTDashboardPresetIn,
    IoTDashboardPresetOut,
    IoTMicrocontrollerIn,
    IoTNodeCreate,
    IoTNodeCreatedOut,
    IoTNodeOut,
    IoTNodeUpdate,
    IoTTelemetryOut,
    IoTNodeStatusOut,
    IoTTtnConfigOut,
    normalize_node_did,
)
from security import generar_token, hash_token
from modules.mod_caficultura.iot_formulas import calculated_series


router = APIRouter(prefix="/iot", tags=["iot"])
manager_dependency = require_roles("admin")

SAFE_COLOR = re.compile(r"^(#[0-9A-Fa-f]{3,8}|[a-zA-Z]{3,20})$")
SAFE_VARIABLE_ID = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,79}$")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime) -> datetime:
    return (
        value.replace(tzinfo=timezone.utc)
        if value.tzinfo is None
        else value.astimezone(timezone.utc)
    )


def _farm_or_404(db: Session, finca_id: int) -> Finca:
    # Esta es la frontera de pertenencia: toda consulta parte de la finca y
    # obtiene el cliente exclusivamente por su FK. No se confía en IDs enviados
    # por telemetría ni en metadatos declarados por el dispositivo.
    row = (
        db.query(Finca)
        .options(selectinload(Finca.cliente))
        .join(Cliente, Cliente.id == Finca.cliente_id)
        .filter(Finca.id == finca_id)
        .first()
    )
    if not row:
        raise HTTPException(status_code=404, detail="Finca no encontrada")
    return row


def _integration_query(db: Session):
    return db.query(IoTIntegration).options(selectinload(IoTIntegration.nodes))


def _integration_or_404(db: Session, integration_id: int) -> IoTIntegration:
    row = _integration_query(db).filter(IoTIntegration.id == integration_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Integración IoT no encontrada")
    return row


def _integration_by_key_or_404(db: Session, integration_key: str) -> IoTIntegration:
    row = (
        _integration_query(db)
        .filter(IoTIntegration.integration_key == integration_key)
        .first()
    )
    if not row:
        raise HTTPException(status_code=404, detail="Integración IoT no encontrada")
    return row


def _serialize_integration(row: IoTIntegration) -> dict[str, Any]:
    return {
        "id": row.id,
        "nombre": row.nombre,
        "provider": row.provider,
        "integration_key": row.integration_key,
        "application_id": row.application_id,
        "activo": bool(row.activo),
        "webhook_path": f"/iot/ingest/ttn/{row.integration_key}",
        "header_name": "X-TTN-Webhook-Secret",
        "node_count": len(row.nodes or []),
        "last_seen_at": row.last_seen_at,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


def _node_query(db: Session):
    return db.query(IoTNode).options(
        selectinload(IoTNode.finca).selectinload(Finca.cliente),
        selectinload(IoTNode.integration),
    )


def _node_or_404(db: Session, node_id: int) -> IoTNode:
    row = _node_query(db).filter(IoTNode.id == node_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Sensor IoT no encontrado")
    return row


def _node_for_farm_or_404(db: Session, finca_id: int, node_id: int) -> IoTNode:
    _farm_or_404(db, finca_id)
    row = (
        _node_query(db)
        .filter(IoTNode.id == node_id, IoTNode.finca_id == finca_id)
        .first()
    )
    if not row:
        # 404 deliberado: no revela si el ID existe en otra finca o cliente.
        raise HTTPException(
            status_code=404, detail="Sensor IoT no encontrado en esta finca"
        )
    return row


def _serialize_node(row: IoTNode) -> dict[str, Any]:
    finca = row.finca
    cliente = finca.cliente if finca else None
    return {
        "id": row.id,
        "finca_id": row.finca_id,
        "finca_nombre": finca.nombre if finca else "",
        "cliente_id": finca.cliente_id if finca else 0,
        "cliente_nombre": cliente.nombre_completo if cliente else "",
        "integration_id": row.integration_id,
        "integration_name": row.integration.nombre if row.integration else None,
        "ttn_application_id": row.integration.application_id if row.integration else None,
        "nombre": row.nombre,
        "did": row.did,
        "tipo": row.tipo,
        "activo": bool(row.activo),
        "variables_config": row.variables_config or {},
        "last_seen_at": row.last_seen_at,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


def _clean_variable_id(value: str, used: set[str]) -> str:
    normalized = unicodedata.normalize("NFD", str(value or ""))
    normalized = "".join(ch for ch in normalized if unicodedata.category(ch) != "Mn")
    normalized = re.sub(r"[^A-Za-z0-9_]+", "_", normalized).strip("_").lower()
    if not normalized or not normalized[0].isalpha():
        normalized = f"v_{normalized or 'dato'}"
    normalized = normalized[:72]
    candidate = normalized
    suffix = 2
    while candidate in used:
        candidate = f"{normalized[:68]}_{suffix}"
        suffix += 1
    used.add(candidate)
    return candidate


def _variable_defaults(source_key: str) -> dict[str, Any]:
    key = unicodedata.normalize("NFD", source_key.lower())
    key = "".join(ch for ch in key if unicodedata.category(ch) != "Mn")
    compact = re.sub(r"[^a-z0-9]+", "_", key).strip("_")
    label = source_key.replace("_", " ").replace("-", " ").strip().title() or "Variable"
    unit = ""
    color = "#176B75"
    minimum = None
    maximum = None

    # Alias frecuentes observados en nodos LoRa/TTN. Son solo metadatos de
    # presentación: cualquier clave numérica desconocida sigue siendo válida.
    if any(token in compact for token in ("battery_voltage", "bat_voltage", "voltaje_bateria")) or compact.startswith(("batv", "vbat")):
        label, unit, color, minimum = "Voltaje de batería", "V", "#397D54", 0
    elif any(token in compact for token in ("battery", "bateria", "bat_pct", "battery_pct", "battery_percent")):
        label, unit, color, minimum, maximum = "Batería", "%", "#397D54", 0, 100
    elif any(token in compact for token in ("voltage", "voltaje", "volt")):
        label, unit, color = "Voltaje", "V", "#397D54"
    elif any(token in compact for token in ("soil_moist", "humedad_suelo", "vwc")):
        label, unit, color, minimum, maximum = (
            "Humedad del suelo",
            "%",
            "#176B75",
            0,
            100,
        )
    elif any(token in compact for token in ("soil_temp", "temperatura_suelo")):
        label, unit, color = "Temperatura del suelo", "°C", "#986A4A"
    elif (
        any(token in compact for token in ("conduct", "soil_ec", "salinity"))
        or re.match(r"^ec(?:_|\d|$)", compact)
        or compact == "ec"
    ):
        label, unit, color = "Conductividad", "mS/cm", "#7C3AED"
    elif (
        any(token in compact for token in ("humidity", "humedad", "relative_humidity"))
        or compact == "rh"
        or compact.startswith("hum")
    ):
        label, unit, color, minimum, maximum = (
            "Humedad relativa",
            "%",
            "#2563EB",
            0,
            100,
        )
    elif any(token in compact for token in ("temperature", "temperatura", "temp", "tempc")):
        label, unit, color = "Temperatura", "°C", "#EA580C"
    elif any(token in compact for token in ("rain", "lluvia", "precip")):
        label, unit, color, minimum = "Precipitación", "mm", "#0284C7", 0
    elif any(token in compact for token in ("pressure", "presion", "barometer")):
        label, unit, color = "Presión", "hPa", "#64748B"
    elif any(token in compact for token in ("light", "luminos", "lux")):
        label, unit, color, minimum = "Luminosidad", "lux", "#CA8A04", 0
    elif compact.startswith("co2") or "carbon_dioxide" in compact:
        label, unit, color, minimum = "CO₂", "ppm", "#475569", 0
    elif "rssi" in compact:
        label, unit, color = "RSSI", "dBm", "#64748B"
    elif "snr" in compact:
        label, unit, color = "SNR", "dB", "#64748B"
    elif any(token in compact for token in ("wind_speed", "velocidad_viento", "windspeed")):
        label, unit, color, minimum = "Velocidad del viento", "m/s", "#0F766E", 0

    return {
        "source_key": source_key,
        "label": label,
        "unit": unit,
        "color": color,
        "precision": 2,
        "minimum": minimum,
        "maximum": maximum,
    }


def _normalize_config(config: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    clean: dict[str, dict[str, Any]] = {}
    used: set[str] = set()
    for raw_id, raw_meta in list((config or {}).items())[:IOT_MAX_VARIABLES]:
        var_id = str(raw_id or "").strip()
        if not SAFE_VARIABLE_ID.fullmatch(var_id) or var_id in used:
            var_id = _clean_variable_id(var_id, used)
        else:
            used.add(var_id)
        meta = (
            raw_meta.model_dump()
            if hasattr(raw_meta, "model_dump")
            else dict(raw_meta or {})
        )
        source_key = str(
            meta.get("source_key") or meta.get("key_ttn") or raw_id
        ).strip()[:120]
        defaults = _variable_defaults(source_key)
        color = str(meta.get("color") or defaults["color"]).strip()
        clean[var_id] = {
            "source_key": source_key,
            "label": str(meta.get("label") or defaults["label"]).strip()[:180],
            "unit": str(meta.get("unit") or defaults["unit"]).strip()[:30],
            "color": color if SAFE_COLOR.fullmatch(color) else defaults["color"],
            "precision": max(0, min(6, int(meta.get("precision", 2) or 0))),
            "minimum": meta.get("minimum", defaults["minimum"]),
            "maximum": meta.get("maximum", defaults["maximum"]),
        }
    return clean


def _numeric(value: Any) -> float | int | None:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)) and not isinstance(value, complex):
        return value if math.isfinite(float(value)) else None
    if isinstance(value, str):
        try:
            parsed = float(value.replace(",", ".").strip())
            if not math.isfinite(parsed):
                return None
            return int(parsed) if parsed.is_integer() else parsed
        except (TypeError, ValueError):
            return None
    return None


def _flatten_numeric(
    payload: dict[str, Any], prefix: str = "", depth: int = 0
) -> dict[str, float | int]:
    out: dict[str, float | int] = {}
    for key, value in payload.items():
        source_key = f"{prefix}.{key}" if prefix else str(key)
        number = _numeric(value)
        if number is not None:
            out[source_key] = number
        elif isinstance(value, dict) and depth < 1:
            out.update(_flatten_numeric(value, source_key, depth + 1))
        if len(out) >= IOT_MAX_VARIABLES:
            break
    return out


def _source_value(payload: dict[str, Any], source_key: str) -> Any:
    current: Any = payload
    for part in source_key.split("."):
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    return current


def _map_payload(node: IoTNode, raw_data: dict[str, Any]) -> dict[str, float | int]:
    config = _normalize_config(node.variables_config or {})
    mapped: dict[str, float | int] = {}
    used_source_keys: set[str] = set()
    for var_id, meta in config.items():
        source_key = str(meta["source_key"])
        used_source_keys.add(source_key)
        number = _numeric(_source_value(raw_data, source_key))
        if number is not None:
            mapped[var_id] = number

    flattened = _flatten_numeric(raw_data)
    used_ids = set(config)
    for source_key, number in flattened.items():
        if source_key in used_source_keys or len(config) >= IOT_MAX_VARIABLES:
            continue
        var_id = _clean_variable_id(source_key, used_ids)
        config[var_id] = _variable_defaults(source_key)
        mapped[var_id] = number

    if config != (node.variables_config or {}):
        node.variables_config = config
    return mapped


def _record_reading(
    db: Session,
    node: IoTNode,
    *,
    raw_data: dict[str, Any],
    recorded_at: datetime | None,
    source: str,
    source_event_id: str | None,
    raw_payload: dict[str, Any] | None,
) -> tuple[IoTReading | None, dict[str, float | int], bool]:
    if not node.activo:
        raise HTTPException(status_code=409, detail="El sensor está desactivado")
    # Fuerza la carga y validación de la cadena cliente -> finca -> sensor.
    finca = _farm_or_404(db, node.finca_id)
    if finca.cliente_id is None:
        raise HTTPException(
            status_code=409, detail="La finca no tiene cliente propietario"
        )

    event_id = str(source_event_id or "").strip()[:180] or None
    if event_id:
        existing = (
            db.query(IoTReading)
            .filter(
                IoTReading.node_id == node.id,
                IoTReading.source_event_id == event_id,
            )
            .first()
        )
        if existing:
            return existing, dict(existing.data or {}), True

    mapped = _map_payload(node, raw_data)
    if not mapped:
        raise HTTPException(
            status_code=422,
            detail="El payload no contiene variables numéricas compatibles",
        )
    timestamp = _aware(recorded_at or utcnow())
    row = IoTReading(
        node_id=node.id,
        recorded_at=timestamp,
        source=source,
        source_event_id=event_id,
        data=mapped,
        raw_payload=raw_payload,
    )
    node.last_seen_at = utcnow()
    db.add(row)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        if event_id:
            existing = (
                db.query(IoTReading)
                .filter(
                    IoTReading.node_id == node.id,
                    IoTReading.source_event_id == event_id,
                )
                .first()
            )
            if existing:
                return existing, dict(existing.data or {}), True
        raise HTTPException(status_code=409, detail="La lectura ya existe") from exc
    db.refresh(row)
    return row, mapped, False


def _parse_ttn_timestamp(payload: dict[str, Any]) -> datetime | None:
    raw = (payload.get("uplink_message") or {}).get("received_at") or payload.get(
        "received_at"
    )
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None


def _ttn_application_id(payload: dict[str, Any]) -> str:
    identifiers = payload.get("end_device_ids") or {}
    applications = identifiers.get("application_ids") or {}
    return str(applications.get("application_id") or "").strip()


def _ttn_event_id(payload: dict[str, Any], dev_eui: str) -> str | None:
    """Construye una clave estable para reintentos del mismo uplink.

    The Things Stack expone ``session_key_id`` y ``f_cnt`` en cada uplink. La
    combinación evita duplicar una lectura cuando el webhook se reintenta y
    sigue siendo segura ante un nuevo join porque cambia la sesión.
    """
    uplink = payload.get("uplink_message") or {}
    session_key_id = str(uplink.get("session_key_id") or "").strip()
    frame_counter = uplink.get("f_cnt")
    if session_key_id and frame_counter is not None:
        return f"ttn:{dev_eui}:{session_key_id}:{frame_counter}"[:180]

    correlation_ids = payload.get("correlation_ids") or []
    if correlation_ids:
        return f"ttn:{dev_eui}:{str(correlation_ids[0])}"[:180]

    received_at = payload.get("received_at") or uplink.get("received_at")
    if received_at:
        return f"ttn:{dev_eui}:{received_at}"[:180]
    return None


def _ttn_source_keys(raw_data: dict[str, Any]) -> list[str]:
    return sorted(_flatten_numeric(raw_data).keys())[:IOT_MAX_VARIABLES]


def _ensure_ingest_payload_size(payload: dict[str, Any]) -> None:
    # FastAPI ya aplica el límite global de request. Este límite específico evita
    # persistir por error payloads TTN enormes en ``raw_payload``.
    try:
        size = len(json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))
    except (TypeError, ValueError):
        raise HTTPException(status_code=422, detail="Payload TTN inválido")
    if size > IOT_MAX_INGEST_BYTES:
        raise HTTPException(status_code=413, detail="El payload TTN supera el tamaño permitido")


@router.get("/ttn/config", response_model=IoTTtnConfigOut)
def get_ttn_config(
    current: Usuario = Depends(manager_dependency),
    db: Session = Depends(get_db),
):
    del current
    integration_count = (
        db.query(IoTIntegration.id)
        .filter(IoTIntegration.provider == "ttn", IoTIntegration.activo.is_(True))
        .count()
    )
    return {
        "integration_webhook_template": "/iot/ingest/ttn/{integration_key}",
        "header_name": "X-TTN-Webhook-Secret",
        "integration_count": integration_count,
        # Compatibilidad de transición únicamente. Las instalaciones nuevas no
        # deben depender de un secreto TTN global.
        "legacy_webhook_path": "/iot/ingest/ttn",
        "legacy_webhook_enabled": len(IOT_TTN_WEBHOOK_SECRET) >= 24,
        "device_identifier": "dev_eui",
        "event_type": "uplink_message",
        "payload_field": "uplink_message.decoded_payload",
    }


def _new_integration_key(db: Session) -> str:
    for _ in range(8):
        candidate = f"ttn_{generar_token(12)}"[:80]
        exists = (
            db.query(IoTIntegration.id)
            .filter(IoTIntegration.integration_key == candidate)
            .first()
        )
        if not exists:
            return candidate
    raise HTTPException(status_code=503, detail="No se pudo generar la clave de integración")


@router.get("/integrations", response_model=list[IoTIntegrationOut])
def list_integrations(
    provider: str | None = Query(default=None, max_length=40),
    current: Usuario = Depends(manager_dependency),
    db: Session = Depends(get_db),
):
    del current
    query = _integration_query(db)
    if provider:
        query = query.filter(IoTIntegration.provider == provider.strip().lower())
    rows = query.order_by(IoTIntegration.nombre.asc(), IoTIntegration.id.asc()).all()
    return [_serialize_integration(row) for row in rows]


@router.post(
    "/integrations",
    response_model=IoTIntegrationCreatedOut,
    status_code=status.HTTP_201_CREATED,
)
def create_integration(
    payload: IoTIntegrationCreate,
    current: Usuario = Depends(manager_dependency),
    db: Session = Depends(get_db),
):
    secret = generar_token(36)
    row = IoTIntegration(
        nombre=payload.nombre,
        provider=payload.provider,
        integration_key=_new_integration_key(db),
        application_id=payload.application_id,
        webhook_secret_hash=hash_token(secret),
        activo=payload.activo,
        config={},
        created_by_id=current.id,
    )
    db.add(row)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail="Ya existe una integración para ese Application ID de TTN",
        ) from exc
    row = _integration_or_404(db, row.id)
    return {**_serialize_integration(row), "webhook_secret": secret}


@router.patch("/integrations/{integration_id}", response_model=IoTIntegrationOut)
def update_integration(
    integration_id: int,
    payload: IoTIntegrationUpdate,
    current: Usuario = Depends(manager_dependency),
    db: Session = Depends(get_db),
):
    del current
    row = _integration_or_404(db, integration_id)
    values = payload.model_dump(exclude_unset=True)
    for key, value in values.items():
        setattr(row, key, value)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail="Ya existe una integración para ese Application ID de TTN",
        ) from exc
    row = _integration_or_404(db, integration_id)
    return _serialize_integration(row)


@router.post(
    "/integrations/{integration_id}/rotate-secret",
    response_model=IoTIntegrationSecretOut,
)
def rotate_integration_secret(
    integration_id: int,
    current: Usuario = Depends(manager_dependency),
    db: Session = Depends(get_db),
):
    del current
    row = _integration_or_404(db, integration_id)
    secret = generar_token(36)
    row.webhook_secret_hash = hash_token(secret)
    db.commit()
    return {"integration_id": row.id, "webhook_secret": secret}


@router.delete("/integrations/{integration_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_integration(
    integration_id: int,
    current: Usuario = Depends(manager_dependency),
    db: Session = Depends(get_db),
):
    del current
    row = _integration_or_404(db, integration_id)
    if row.nodes:
        raise HTTPException(
            status_code=409,
            detail="La integración tiene sensores asociados. Reasígnelos o desactive la integración.",
        )
    db.delete(row)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/nodes/{node_id}/status", response_model=IoTNodeStatusOut)
def get_node_status(
    node_id: int,
    current: Usuario = Depends(manager_dependency),
    db: Session = Depends(get_db),
):
    del current
    node = _node_or_404(db, node_id)
    latest = (
        db.query(IoTReading)
        .filter(IoTReading.node_id == node.id)
        .order_by(IoTReading.recorded_at.desc(), IoTReading.id.desc())
        .first()
    )
    reading_count = db.query(IoTReading.id).filter(IoTReading.node_id == node.id).count()
    raw_data = {}
    if latest and isinstance(latest.raw_payload, dict):
        raw_data = (latest.raw_payload.get("uplink_message") or {}).get("decoded_payload") or {}
    if not raw_data and latest:
        raw_data = latest.data or {}
    return {
        "node": _serialize_node(node),
        "has_data": latest is not None,
        "reading_count": reading_count,
        "latest_reading_at": latest.recorded_at if latest else None,
        "latest_received_at": latest.received_at if latest else None,
        "latest_source": latest.source if latest else None,
        "latest_values": dict(latest.data or {}) if latest else {},
        "latest_source_keys": _ttn_source_keys(raw_data) if isinstance(raw_data, dict) else [],
    }


@router.get("/nodes", response_model=list[IoTNodeOut])
def list_nodes(
    finca_id: int | None = Query(default=None, gt=0),
    current: Usuario = Depends(manager_dependency),
    db: Session = Depends(get_db),
):
    query = _node_query(db)
    if finca_id is not None:
        _farm_or_404(db, finca_id)
        query = query.filter(IoTNode.finca_id == finca_id)
    rows = query.order_by(IoTNode.nombre.asc(), IoTNode.id.asc()).all()
    return [_serialize_node(row) for row in rows]


@router.post(
    "/nodes", response_model=IoTNodeCreatedOut, status_code=status.HTTP_201_CREATED
)
def create_node(
    payload: IoTNodeCreate,
    current: Usuario = Depends(manager_dependency),
    db: Session = Depends(get_db),
):
    _farm_or_404(db, payload.finca_id)
    integration = None
    if payload.tipo == "ttn":
        integration = _integration_or_404(db, int(payload.integration_id or 0))
        if integration.provider != "ttn" or not integration.activo:
            raise HTTPException(status_code=422, detail="Seleccione una integración TTN activa")
    ingest_token = generar_token(36) if payload.tipo == "microcontrolador" else None
    row = IoTNode(
        finca_id=payload.finca_id,
        integration_id=integration.id if integration else None,
        nombre=payload.nombre,
        did=payload.did,
        tipo=payload.tipo,
        activo=payload.activo,
        variables_config=_normalize_config(payload.variables_config),
        ingest_token_hash=hash_token(ingest_token) if ingest_token else None,
        created_by_id=current.id,
    )
    db.add(row)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=409, detail="Ya existe un sensor con ese DID/devEUI"
        ) from exc
    row = _node_or_404(db, row.id)
    return {**_serialize_node(row), "ingest_token": ingest_token}


@router.patch("/nodes/{node_id}", response_model=IoTNodeCreatedOut)
def update_node(
    node_id: int,
    payload: IoTNodeUpdate,
    current: Usuario = Depends(manager_dependency),
    db: Session = Depends(get_db),
):
    row = _node_or_404(db, node_id)
    values = payload.model_dump(exclude_unset=True)
    target_type = str(values.get("tipo") or row.tipo)
    ingest_token = None
    if "finca_id" in values:
        _farm_or_404(db, int(values["finca_id"]))
    if "did" in values or "tipo" in values:
        try:
            values["did"] = normalize_node_did(
                str(values.get("did") or row.did), target_type
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
    if "variables_config" in values:
        values["variables_config"] = _normalize_config(values["variables_config"])
    target_integration_id = values.get("integration_id", row.integration_id)
    if target_type == "ttn":
        if not target_integration_id:
            # Permite editar nodos TTN heredados mientras se migran, pero si el
            # cliente envía explícitamente integration_id debe ser válido.
            if "integration_id" in values:
                raise HTTPException(status_code=422, detail="Seleccione una integración TTN")
        else:
            integration = _integration_or_404(db, int(target_integration_id))
            if integration.provider != "ttn" or not integration.activo:
                raise HTTPException(status_code=422, detail="Seleccione una integración TTN activa")
        values["ingest_token_hash"] = None
    else:
        values["integration_id"] = None
        if not row.ingest_token_hash:
            ingest_token = generar_token(36)
            values["ingest_token_hash"] = hash_token(ingest_token)
    for key, value in values.items():
        setattr(row, key, value)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=409, detail="Ya existe un sensor con ese DID/devEUI"
        ) from exc
    row = _node_or_404(db, node_id)
    return {**_serialize_node(row), "ingest_token": ingest_token}


@router.post("/nodes/{node_id}/rotate-token", response_model=IoTCredentialOut)
def rotate_node_token(
    node_id: int,
    current: Usuario = Depends(manager_dependency),
    db: Session = Depends(get_db),
):
    row = _node_or_404(db, node_id)
    if row.tipo != "microcontrolador":
        raise HTTPException(
            status_code=422, detail="Los nodos TTN usan el secreto de su integración"
        )
    token = generar_token(36)
    row.ingest_token_hash = hash_token(token)
    db.commit()
    return {"node_id": row.id, "did": row.did, "ingest_token": token}


@router.delete("/nodes/{node_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_node(
    node_id: int,
    current: Usuario = Depends(manager_dependency),
    db: Session = Depends(get_db),
):
    row = _node_or_404(db, node_id)
    has_readings = db.query(IoTReading.id).filter(IoTReading.node_id == row.id).first()
    if has_readings:
        raise HTTPException(
            status_code=409,
            detail="El sensor tiene telemetría histórica. Desactívelo para conservar la trazabilidad.",
        )
    db.delete(row)
    db.commit()


@router.get("/fincas/{finca_id}/nodes", response_model=list[IoTNodeOut])
def list_farm_nodes(
    finca_id: int,
    current: Usuario = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _farm_or_404(db, finca_id)
    rows = (
        _node_query(db)
        .filter(IoTNode.finca_id == finca_id)
        .order_by(IoTNode.nombre.asc())
        .all()
    )
    return [_serialize_node(row) for row in rows]


def _telemetry_window(
    from_ts: datetime | None, to_ts: datetime | None, hours: int
) -> tuple[datetime, datetime]:
    end = _aware(to_ts or utcnow())
    start = _aware(from_ts) if from_ts else end - timedelta(hours=hours)
    if start > end:
        raise HTTPException(
            status_code=422, detail="La fecha inicial no puede ser posterior a la final"
        )
    if end - start > timedelta(days=366):
        raise HTTPException(
            status_code=422, detail="El rango máximo de consulta es de 366 días"
        )
    return start, end


def _reading_query(
    db: Session,
    finca_id: int,
    start: datetime,
    end: datetime,
    node_ids: list[int] | None = None,
):
    query = (
        db.query(IoTReading)
        .join(IoTNode, IoTNode.id == IoTReading.node_id)
        .join(Finca, Finca.id == IoTNode.finca_id)
        .join(Cliente, Cliente.id == Finca.cliente_id)
        .filter(
            Finca.id == finca_id,
            IoTReading.recorded_at >= start,
            IoTReading.recorded_at <= end,
        )
    )
    if node_ids:
        query = query.filter(IoTNode.id.in_(node_ids))
    return query


@router.get("/fincas/{finca_id}/telemetry", response_model=IoTTelemetryOut)
def get_farm_telemetry(
    finca_id: int,
    hours: int = Query(default=24, ge=1, le=8784),
    from_ts: datetime | None = Query(default=None),
    to_ts: datetime | None = Query(default=None),
    node_id: list[int] | None = Query(default=None),
    current: Usuario = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    finca = _farm_or_404(db, finca_id)
    start, end = _telemetry_window(from_ts, to_ts, hours)
    nodes = (
        _node_query(db)
        .filter(IoTNode.finca_id == finca_id)
        .order_by(IoTNode.nombre.asc())
        .all()
    )
    allowed_ids = {row.id for row in nodes}
    requested_ids = [int(item) for item in (node_id or [])]
    if requested_ids and not set(requested_ids).issubset(allowed_ids):
        raise HTTPException(
            status_code=404, detail="Uno o más sensores no pertenecen a esta finca"
        )
    rows = (
        _reading_query(db, finca_id, start, end, requested_ids)
        .order_by(IoTReading.recorded_at.asc(), IoTReading.id.asc())
        .limit(IOT_MAX_QUERY_ROWS + 1)
        .all()
    )
    truncated = len(rows) > IOT_MAX_QUERY_ROWS
    rows = rows[:IOT_MAX_QUERY_ROWS]
    preset = db.query(IoTDashboardPreset).filter(
        IoTDashboardPreset.usuario_id == current.id,
        IoTDashboardPreset.finca_id == finca_id,
    ).first()
    return {
        "finca": {
            "id": finca.id,
            "nombre": finca.nombre,
            "cliente_id": finca.cliente_id,
            "cliente_nombre": finca.cliente.nombre_completo if finca.cliente else "",
        },
        "from_ts": start,
        "to_ts": end,
        "nodes": [
            _serialize_node(row)
            for row in nodes
            if not requested_ids or row.id in requested_ids
        ],
        "readings": [
            {
                "id": row.id,
                "node_id": row.node_id,
                "recorded_at": row.recorded_at,
                "received_at": row.received_at,
                "values": row.data or {},
            }
            for row in rows
        ],
        "truncated": truncated,
        "calculated": calculated_series(preset.widgets if preset else [], rows),
    }


@router.get("/fincas/{finca_id}/telemetry/export")
def export_farm_telemetry(
    finca_id: int,
    hours: int = Query(default=24, ge=1, le=8784),
    from_ts: datetime | None = Query(default=None),
    to_ts: datetime | None = Query(default=None),
    node_id: list[int] | None = Query(default=None),
    current: Usuario = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    finca = _farm_or_404(db, finca_id)
    start, end = _telemetry_window(from_ts, to_ts, hours)
    nodes = _node_query(db).filter(IoTNode.finca_id == finca_id).all()
    node_map = {row.id: row for row in nodes}
    requested_ids = [int(item) for item in (node_id or [])]
    if requested_ids and not set(requested_ids).issubset(node_map):
        raise HTTPException(
            status_code=404, detail="Uno o más sensores no pertenecen a esta finca"
        )
    rows = (
        _reading_query(db, finca_id, start, end, requested_ids)
        .order_by(IoTReading.recorded_at.asc(), IoTReading.id.asc())
        .limit(IOT_MAX_EXPORT_ROWS + 1)
        .all()
    )
    if len(rows) > IOT_MAX_EXPORT_ROWS:
        raise HTTPException(
            status_code=413,
            detail="La exportación supera el máximo permitido; reduzca el rango",
        )
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer)
    writer.writerow(
        [
            "fecha_hora_utc",
            "cliente",
            "finca",
            "sensor",
            "did_devEui",
            "variable",
            "etiqueta",
            "valor",
            "unidad",
        ]
    )
    for reading in rows:
        node = node_map.get(reading.node_id)
        if not node:
            continue
        config = node.variables_config or {}
        for variable_id, value in (reading.data or {}).items():
            meta = config.get(variable_id) or {}
            writer.writerow(
                [
                    _aware(reading.recorded_at).isoformat(),
                    finca.cliente.nombre_completo if finca.cliente else "",
                    finca.nombre,
                    node.nombre,
                    node.did,
                    variable_id,
                    meta.get("label") or variable_id,
                    value,
                    meta.get("unit") or "",
                ]
            )
    filename = f"telemetria-{finca.codigo}-{start.date().isoformat()}-{end.date().isoformat()}.csv"
    return Response(
        content="\ufeff" + buffer.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get("/fincas/{finca_id}/dashboard-preset", response_model=IoTDashboardPresetOut)
def get_dashboard_preset(
    finca_id: int,
    current: Usuario = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _farm_or_404(db, finca_id)
    row = (
        db.query(IoTDashboardPreset)
        .filter(
            IoTDashboardPreset.usuario_id == current.id,
            IoTDashboardPreset.finca_id == finca_id,
        )
        .first()
    )
    return {
        "id": row.id if row else None,
        "finca_id": finca_id,
        "widgets": row.widgets if row else [],
        "updated_at": row.updated_at if row else None,
    }


@router.put("/fincas/{finca_id}/dashboard-preset", response_model=IoTDashboardPresetOut)
def save_dashboard_preset(
    finca_id: int,
    payload: IoTDashboardPresetIn,
    current: Usuario = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _farm_or_404(db, finca_id)
    nodes = _node_query(db).filter(IoTNode.finca_id == finca_id).all()
    node_map = {row.id: row for row in nodes}
    widgets: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, widget in enumerate(
        sorted(payload.widgets, key=lambda item: item.order), start=1
    ):
        node = node_map.get(widget.node_id)
        if not node:
            raise HTTPException(
                status_code=422,
                detail="Un widget referencia un sensor ajeno a la finca",
            )
        if widget.variable_id not in (node.variables_config or {}):
            raise HTTPException(
                status_code=422,
                detail="Un widget referencia una variable no disponible",
            )
        for source in widget.inputs.values():
            source_node = node_map.get(source.node_id)
            if not source_node or source.variable_id not in (source_node.variables_config or {}):
                raise HTTPException(status_code=422, detail="La fórmula referencia una variable no disponible en esta finca")
        widget_id = widget.id.strip()
        if widget_id in seen:
            raise HTTPException(
                status_code=422, detail="Los identificadores de widget deben ser únicos"
            )
        seen.add(widget_id)
        data = widget.model_dump()
        data["order"] = index
        widgets.append(data)
    row = (
        db.query(IoTDashboardPreset)
        .filter(
            IoTDashboardPreset.usuario_id == current.id,
            IoTDashboardPreset.finca_id == finca_id,
        )
        .first()
    )
    if not row:
        row = IoTDashboardPreset(usuario_id=current.id, finca_id=finca_id, widgets=[])
        db.add(row)
    row.widgets = widgets
    db.commit()
    db.refresh(row)
    return {
        "id": row.id,
        "finca_id": finca_id,
        "widgets": row.widgets or [],
        "updated_at": row.updated_at,
    }


def _validate_ttn_secret(candidate: str | None, expected_hash: str) -> bool:
    if not candidate or not expected_hash:
        return False
    return hmac.compare_digest(hash_token(candidate), expected_hash)


def _ingest_ttn_for_node(
    db: Session,
    node: IoTNode,
    payload: dict[str, Any],
    did: str,
    application_id: str,
    integration: IoTIntegration | None = None,
) -> dict[str, Any]:
    if not node.activo:
        return {"status": "ignored", "reason": "sensor_inactive", "did": did}

    raw_data = (payload.get("uplink_message") or {}).get("decoded_payload") or {}
    if not isinstance(raw_data, dict):
        raise HTTPException(
            status_code=422, detail="uplink_message.decoded_payload debe ser un objeto JSON"
        )
    if not _flatten_numeric(raw_data):
        return {"status": "ignored", "reason": "no_numeric_decoded_payload", "did": did}

    row, mapped, duplicate = _record_reading(
        db,
        node,
        raw_data=raw_data,
        recorded_at=_parse_ttn_timestamp(payload),
        source="ttn",
        source_event_id=_ttn_event_id(payload, did),
        raw_payload=payload,
    )
    if integration is not None:
        integration.last_seen_at = utcnow()
        db.commit()
    return {
        "status": "duplicate" if duplicate else "success",
        "reading_id": row.id if row else None,
        "did": node.did,
        "integration_id": integration.id if integration else None,
        "application_id": application_id or None,
        "data": mapped,
    }


@router.post("/ingest/ttn/{integration_key}")
async def ingest_ttn_integration(
    integration_key: str,
    payload: dict[str, Any],
    request: Request,
    x_ttn_webhook_secret: str | None = Header(
        default=None, alias="X-TTN-Webhook-Secret"
    ),
    db: Session = Depends(get_db),
):
    integration = _integration_by_key_or_404(db, integration_key)
    if integration.provider != "ttn" or not integration.activo:
        raise HTTPException(status_code=404, detail="Integración TTN no disponible")
    if not _validate_ttn_secret(x_ttn_webhook_secret, integration.webhook_secret_hash):
        raise HTTPException(status_code=403, detail="Secreto TTN inválido")

    enforce_rate_limit(
        request,
        f"iot-ttn-ingest:{integration.id}",
        IOT_INGEST_RATE_LIMIT_PER_MINUTE,
        60,
    )
    _ensure_ingest_payload_size(payload)
    identifiers = payload.get("end_device_ids") or {}
    raw_did = identifiers.get("dev_eui")
    if not raw_did:
        raise HTTPException(status_code=422, detail="TTN no envió end_device_ids.dev_eui")
    try:
        did = normalize_node_did(str(raw_did), "ttn")
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    application_id = _ttn_application_id(payload)
    if not application_id:
        raise HTTPException(status_code=422, detail="TTN no envió application_id")
    if integration.application_id and application_id != integration.application_id:
        raise HTTPException(
            status_code=403,
            detail="El Application ID del uplink no coincide con esta integración",
        )

    node = (
        _node_query(db)
        .filter(
            IoTNode.did == did,
            IoTNode.tipo == "ttn",
            IoTNode.integration_id == integration.id,
        )
        .first()
    )
    if not node:
        # 2xx evita reintentos innecesarios de TTN por dispositivos que todavía
        # no han sido registrados o fueron asociados a otra integración.
        return {
            "status": "ignored",
            "reason": "sensor_not_registered_for_integration",
            "did": did,
            "integration_id": integration.id,
        }
    return _ingest_ttn_for_node(db, node, payload, did, application_id, integration)


@router.post("/ingest/ttn")
async def ingest_ttn_legacy(
    payload: dict[str, Any],
    request: Request,
    x_ttn_webhook_secret: str | None = Header(
        default=None, alias="X-TTN-Webhook-Secret"
    ),
    db: Session = Depends(get_db),
):
    """Compatibilidad para instalaciones 1.3.x mientras migran a integraciones.

    No restringe Application ID. Solo acepta nodos TTN heredados que todavía no
    tengan ``integration_id``. Las nuevas configuraciones deben usar la ruta con
    ``integration_key`` y secreto independiente por aplicación TTN.
    """
    if len(IOT_TTN_WEBHOOK_SECRET) < 24:
        raise HTTPException(status_code=410, detail="Webhook TTN heredado deshabilitado")
    if not x_ttn_webhook_secret or not hmac.compare_digest(
        x_ttn_webhook_secret, IOT_TTN_WEBHOOK_SECRET
    ):
        raise HTTPException(status_code=403, detail="Secreto TTN inválido")
    enforce_rate_limit(request, "iot-ttn-ingest:legacy", IOT_INGEST_RATE_LIMIT_PER_MINUTE, 60)
    _ensure_ingest_payload_size(payload)

    identifiers = payload.get("end_device_ids") or {}
    raw_did = identifiers.get("dev_eui")
    if not raw_did:
        raise HTTPException(status_code=422, detail="TTN no envió end_device_ids.dev_eui")
    try:
        did = normalize_node_did(str(raw_did), "ttn")
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    node = (
        _node_query(db)
        .filter(
            IoTNode.did == did,
            IoTNode.tipo == "ttn",
            IoTNode.integration_id.is_(None),
        )
        .first()
    )
    if not node:
        return {"status": "ignored", "reason": "legacy_sensor_not_registered", "did": did}
    return _ingest_ttn_for_node(
        db, node, payload, did, _ttn_application_id(payload), None
    )


@router.post("/ingest/microcontroller/{device_did}")
def ingest_microcontroller(
    device_did: str,
    payload: IoTMicrocontrollerIn,
    request: Request,
    x_iot_token: str | None = Header(default=None, alias="X-IoT-Token"),
    db: Session = Depends(get_db),
):
    enforce_rate_limit(
        request, f"iot-micro-{device_did}", IOT_INGEST_RATE_LIMIT_PER_MINUTE, 60
    )
    try:
        did = normalize_node_did(device_did, "microcontrolador")
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    node = (
        _node_query(db)
        .filter(IoTNode.did == did, IoTNode.tipo == "microcontrolador")
        .first()
    )
    if not node or not node.ingest_token_hash:
        raise HTTPException(status_code=404, detail="Sensor no encontrado")
    candidate_hash = hashlib.sha256(str(x_iot_token or "").encode("utf-8")).hexdigest()
    if not x_iot_token or not hmac.compare_digest(
        candidate_hash, node.ingest_token_hash
    ):
        raise HTTPException(status_code=403, detail="Token del sensor inválido")
    row, mapped, duplicate = _record_reading(
        db,
        node,
        raw_data=payload.data,
        recorded_at=payload.timestamp,
        source="microcontrolador",
        source_event_id=payload.event_id,
        raw_payload=None,
    )
    return {
        "status": "duplicate" if duplicate else "success",
        "reading_id": row.id if row else None,
        "did": node.did,
        "data": mapped,
    }
