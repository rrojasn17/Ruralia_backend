from __future__ import annotations

import hashlib
import json
import math
import re
import os
import shutil
import unicodedata
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from config import AI_ACTION_EXPIRY_MINUTES, APP_BASE_URL
from modules.mod_caficultura.model_ai_consulting import AIAttachment, AIAuditLog, AIConversation, AIMessage, AIPendingAction
from modules.mod_caficultura.models import (
    ActividadFinca,
    TrabajadorFinca,
    InsumoFinca,
    Cliente,
    Proveedor,
    ProveedorContacto,
    ComentarioLote,
    DocumentoLote,
    Finca,
    OrdenTrabajo,
    RegistroFinca,
    SolicitudVenta,
    SolicitudVentaDocumento,
    SolicitudVentaLinea,
)
from core.models import Usuario
from modules.mod_caficultura.schemas import ReciboCreate
from modules.mod_caficultura.ai_metrics import _resolve_named, resolve_client, resolve_farm, resolve_lot
from services.ai_storage import resolve_storage_path, safe_original_name
from modules.mod_caficultura.services import create_recibo_from_payload, next_code, serialize_recibo


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _json_default(value: Any) -> str:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return str(value)


def _float_or_none(value: Any) -> float | None:
    if value in {None, ""}:
        return None
    try:
        numeric = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"'{value}' no es un número válido") from exc
    if not math.isfinite(numeric):
        raise ValueError("El valor debe ser un número finito")
    if numeric < 0:
        raise ValueError("Los valores numéricos no pueden ser negativos")
    return numeric


def _create_pending_action(
    db: Session,
    *,
    conversation: AIConversation,
    user: Usuario,
    message: AIMessage,
    action_type: str,
    summary: str,
    payload: dict[str, Any],
) -> AIPendingAction:
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=_json_default)
    idempotency_key = hashlib.sha256(
        f"{conversation.id}:{action_type}:{canonical}".encode("utf-8")
    ).hexdigest()
    existing = db.query(AIPendingAction).filter(AIPendingAction.idempotency_key == idempotency_key).first()
    if existing:
        return existing

    row = AIPendingAction(
        public_id=uuid.uuid4().hex,
        idempotency_key=idempotency_key,
        conversation_id=conversation.id,
        usuario_id=user.id,
        requested_by_message_id=message.id,
        action_type=action_type,
        summary=summary,
        payload=json.loads(canonical),
        status="pending",
        version=1,
        expires_at=utcnow() + timedelta(minutes=AI_ACTION_EXPIRY_MINUTES),
    )
    db.add(row)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        existing = db.query(AIPendingAction).filter(AIPendingAction.idempotency_key == idempotency_key).first()
        if existing:
            return existing
        raise
    return row


def prepare_receipt_action(
    db: Session,
    conversation: AIConversation,
    user: Usuario,
    message: AIMessage,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    missing: list[str] = []
    client_name = str(arguments.get("client_name") or "").strip()
    producer_name = str(arguments.get("producer_name") or "").strip()
    farm_name = str(arguments.get("farm_name") or "").strip()
    raw_date = arguments.get("date")
    client: Cliente | None = None
    farm: Finca | None = None

    if client_name:
        resolved_client = resolve_client(db, client_name)
        if not resolved_client.get("ok"):
            return resolved_client
        client = resolved_client["row"]
        producer_name = producer_name or client.nombre_completo
    elif not producer_name:
        missing.append("cliente o nombre del productor")

    if farm_name:
        resolved_farm = resolve_farm(db, farm_name)
        if not resolved_farm.get("ok"):
            return resolved_farm
        farm = resolved_farm["row"]
        if client and farm.cliente_id != client.id:
            return {
                "ok": False,
                "needs_clarification": True,
                "message": f"La finca {farm.nombre} no pertenece a {client.nombre_completo}. Confirme cuál dato es correcto.",
            }
        if not client and farm.cliente:
            client = farm.cliente
            producer_name = producer_name or client.nombre_completo
    elif client:
        farms = db.query(Finca).filter(Finca.cliente_id == client.id, Finca.activa.is_(True)).order_by(Finca.nombre).all()
        if len(farms) == 1:
            farm = farms[0]
        elif len(farms) > 1:
            return {
                "ok": False,
                "needs_clarification": True,
                "message": f"{client.nombre_completo} tiene varias fincas. ¿De cuál proviene el café?",
                "options": [{"id": row.id, "label": row.nombre} for row in farms],
            }

    if not raw_date:
        missing.append("fecha del recibo")
        receipt_date = None
    else:
        try:
            receipt_date = date.fromisoformat(str(raw_date))
        except ValueError:
            return {"ok": False, "needs_clarification": True, "message": "La fecha debe usar el formato AAAA-MM-DD."}

    try:
        cajuelas = _float_or_none(arguments.get("cajuelas")) or 0
        cuartillos = _float_or_none(arguments.get("cuartillos")) or 0
        flote = _float_or_none(arguments.get("porcentaje_flote"))
        verde = _float_or_none(arguments.get("porcentaje_verde"))
        peso = _float_or_none(arguments.get("peso_promedio_cajuela"))
        precio = _float_or_none(arguments.get("precio_fanega"))
    except ValueError as exc:
        return {"ok": False, "needs_clarification": True, "message": str(exc)}

    if cajuelas <= 0 and cuartillos <= 0:
        missing.append("cantidad recibida en cajuelas o cuartillos")
    if flote is not None and flote > 100:
        missing.append("porcentaje de flote entre 0 y 100")
    if verde is not None and verde > 100:
        missing.append("porcentaje de verde entre 0 y 100")
    if missing:
        return {
            "ok": False,
            "needs_clarification": True,
            "message": "Para preparar el recibo todavía necesito: " + ", ".join(missing) + ".",
            "missing_fields": missing,
        }

    payload = {
        "fecha": receipt_date.isoformat(),
        "cliente_id": client.id if client else None,
        "finca_id": farm.id if farm else None,
        "productor_nombre": producer_name,
        "productor_cedula": str(arguments.get("producer_id") or (client.numero_identificacion if client else "")).strip() or None,
        "provincia": farm.provincia if farm else (client.provincia if client else None),
        "canton": farm.canton if farm else (client.canton if client else None),
        "distrito": farm.distrito if farm else (client.distrito if client else None),
        "zona": str(arguments.get("zone") or "").strip() or None,
        "cajuelas": cajuelas,
        "cuartillos": cuartillos,
        "porcentaje_flote": flote,
        "porcentaje_verde": verde,
        "peso_promedio_cajuela": peso,
        "precio_fanega": precio,
        "beneficio_recibe_usuario_id": user.id,
        "beneficio_recibe": user.nombre,
        "productor_entrega": str(arguments.get("delivered_by") or producer_name).strip(),
        "estado": "recibido",
        "observaciones": str(arguments.get("observations") or "").strip() or None,
    }

    try:
        ReciboCreate.model_validate(payload)
    except ValidationError as exc:
        return {
            "ok": False,
            "needs_clarification": True,
            "message": "Hay datos del recibo que necesitan corrección.",
            "validation_errors": exc.errors(include_url=False),
        }

    quantity = cajuelas + cuartillos / 4
    summary = (
        f"Crear recibo para {producer_name}, finca {farm.nombre if farm else 'sin finca'}, "
        f"fecha {receipt_date.isoformat()}, {quantity:.2f} cajuelas equivalentes."
    )
    action = _create_pending_action(
        db,
        conversation=conversation,
        user=user,
        message=message,
        action_type="create_receipt",
        summary=summary,
        payload=payload,
    )
    return {
        "ok": True,
        "requires_confirmation": True,
        "action_public_id": action.public_id,
        "action_version": action.version,
        "summary": action.summary,
        "expires_at": action.expires_at.isoformat(),
    }


def _normalize_activity_name(value: object) -> str:
    normalized = unicodedata.normalize("NFD", str(value or "").strip().lower())
    return "".join(char for char in normalized if unicodedata.category(char) != "Mn")


def prepare_farm_activity_action(
    db: Session,
    conversation: AIConversation,
    user: Usuario,
    message: AIMessage,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    from core.routers.auth import require_roles
    require_roles("gerente", "administrativo", "supervisor_finca")(user)
    farm_name = str(arguments.get("farm_name") or "").strip()
    activity_name = str(arguments.get("activity_name") or "").strip()
    if not farm_name:
        return {"ok": False, "needs_clarification": True, "message": "¿En cuál finca desea registrar la actividad?"}
    resolved_farm = resolve_farm(db, farm_name)
    if not resolved_farm.get("ok"):
        return resolved_farm
    farm: Finca = resolved_farm["row"]
    if not farm.gestion_fincas_habilitada:
        return {"ok": False, "message": f"La finca {farm.nombre} no tiene habilitado el módulo de gestión de actividades."}

    activities = (
        db.query(ActividadFinca)
        .filter(ActividadFinca.activa.is_(True))
        .order_by(ActividadFinca.nombre.asc())
        .all()
    )
    if not activity_name:
        return {
            "ok": False,
            "needs_clarification": True,
            "message": "¿Qué actividad desea registrar? Seleccione una actividad del catálogo.",
            "options": [{"id": row.id, "label": f"{row.nombre} · {row.tipo}"} for row in activities[:20]],
        }
    normalized_name = _normalize_activity_name(activity_name)
    # Common spoken verbs map to catalog categories, never to invented activities.
    for pattern, category in ((r"\b(abon\w*|fertiliz\w*)", "fertiliz"),
                              (r"\b(chapi\w*|chapear|chapea\w*)", "chapi"),
                              (r"\b(poda\w*)", "poda")):
        if re.search(pattern, normalized_name):
            candidates = [row for row in activities if re.search(pattern, _normalize_activity_name(f"{row.nombre} {row.tipo}"))]
            if len(candidates) == 1:
                normalized_name = candidates[0].nombre
            break
    resolved_activity = _resolve_named(activities, normalized_name, ("nombre", "tipo"), "la actividad")
    if not resolved_activity.get("ok"):
        return resolved_activity
    activity = resolved_activity["row"]
    raw_date = str(arguments.get("date") or "").strip()
    try:
        activity_date = date.fromisoformat(raw_date) if raw_date else date.today()
    except ValueError:
        return {"ok": False, "needs_clarification": True, "message": "La fecha debe usar el formato AAAA-MM-DD."}

    # Resolve names from speech against real catalogs; never guess ambiguous people or supplies.
    details = {"trabajadores": [], "insumos": []}
    detail_labels = []
    for key, model, id_key in (("workers", TrabajadorFinca, "trabajador_id"), ("supplies", InsumoFinca, "insumo_id")):
        seen = set()
        for item in arguments.get(key) or []:
            name = _normalize_activity_name(item.get("name"))
            rows = db.query(model).filter(model.activo.is_(True)).all()
            resolved = _resolve_named(rows, name, ("nombre",), "el trabajador" if key == "workers" else "el insumo")
            if not resolved.get("ok"):
                return resolved
            selected = resolved["row"]
            if selected.id in seen:
                return {"ok": False, "needs_clarification": True, "message": f"Indique un único total para {selected.nombre}."}
            seen.add(selected.id)
            try:
                amount = _float_or_none(item.get("hours" if key == "workers" else "quantity"))
                if amount is None or (key == "supplies" and amount <= 0):
                    raise ValueError(f"Indique {'horas' if key == 'workers' else 'cantidad'} para {selected.nombre}.")
                if key == "workers":
                    wage = _float_or_none(item.get("daily_wage"))
                    wage = selected.jornal_diario if wage is None else wage
                    details["trabajadores"].append({id_key: selected.id, "horas": amount, "jornal": wage})
                    detail_labels.append(f"{selected.nombre}: {amount:g} horas, jornal {wage if wage is not None else 'sin tarifa'}")
                else:
                    unit = str(item.get("unit") or "").strip()
                    if _normalize_activity_name(unit) != _normalize_activity_name(selected.unidad):
                        raise ValueError(f"Indique la cantidad de {selected.nombre} en {selected.unidad}.")
                    cost = _float_or_none(item.get("unit_cost"))
                    cost = (selected.precio_unitario if selected.precio_unitario is not None else selected.costo_unitario) if cost is None else cost
                    details["insumos"].append({id_key: selected.id, "cantidad": amount, "unidad": selected.unidad, "costo_unitario": cost})
                    detail_labels.append(f"{selected.nombre}: {amount:g} {selected.unidad}, costo unitario {cost if cost is not None else 'sin tarifa'}")
            except ValueError as exc:
                return {"ok": False, "needs_clarification": True, "message": str(exc)}

    description = str(arguments.get("description") or activity.nombre).strip()[:255]
    observations = str(arguments.get("observations") or "").strip()[:4000] or None
    payload = {
        "farm_id": farm.id,
        "farm_name": farm.nombre,
        "activity_id": activity.id,
        "activity_name": activity.nombre,
        **details,
        "date": activity_date.isoformat(),
        "description": description,
        "observations": observations,
    }
    summary = f"Registrar {activity.nombre} en {farm.nombre} el {activity_date.isoformat()}"
    summary += f". Detalle: {description}. " + ("; ".join(detail_labels) or "Sin trabajadores ni insumos indicados")
    if observations:
        summary += f". Observación: {observations}"
    action = _create_pending_action(
        db,
        conversation=conversation,
        user=user,
        message=message,
        action_type="create_farm_activity",
        summary=summary,
        payload=payload,
    )
    return {
        "ok": True,
        "requires_confirmation": True,
        "action_public_id": action.public_id,
        "action_version": action.version,
        "summary": action.summary,
        "expires_at": action.expires_at.isoformat(),
    }


def prepare_provider_action(
    db: Session,
    conversation: AIConversation,
    user: Usuario,
    message: AIMessage,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    from core.routers.auth import require_roles
    require_roles("administrativo", "admin")(user)

    legal_name = str(arguments.get("legal_name") or "").strip()
    commercial_name = str(arguments.get("commercial_name") or "").strip() or None
    identification = str(arguments.get("identification") or "").strip() or None
    if not legal_name:
        return {
            "ok": False,
            "needs_clarification": True,
            "message": "¿Cuál es el nombre legal o nombre completo del proveedor?",
        }
    if len(legal_name) < 2:
        return {"ok": False, "needs_clarification": True, "message": "El nombre del proveedor es demasiado corto."}

    if identification:
        duplicate = db.query(Proveedor).filter(Proveedor.identificacion == identification).first()
        if duplicate:
            return {
                "ok": False,
                "message": f"Ya existe un proveedor con la identificación {identification}: {duplicate.nombre_comercial or duplicate.nombre_legal}.",
                "existing": {"id": duplicate.id, "path": "/proveedores"},
            }

    # No se bloquea por nombre, porque dos proveedores pueden compartir una razón social similar.

    contacts: list[dict[str, Any]] = []
    for raw in arguments.get("contacts") or []:
        if not isinstance(raw, dict):
            continue
        name = str(raw.get("name") or "").strip()
        if not name:
            continue
        contacts.append({
            "nombre": name[:180],
            "puesto": str(raw.get("position") or "").strip()[:140] or None,
            "correo": str(raw.get("email") or "").strip()[:180] or None,
            "telefono": str(raw.get("phone") or "").strip()[:80] or None,
            "principal": bool(raw.get("primary", False)),
            "activo": True,
            "notas": str(raw.get("notes") or "").strip()[:2000] or None,
        })
    if contacts and not any(item["principal"] for item in contacts):
        contacts[0]["principal"] = True

    payload = {
        "nombre_legal": legal_name[:220],
        "nombre_comercial": commercial_name[:220] if commercial_name else None,
        "identificacion": identification[:100] if identification else None,
        "tipo_identificacion": str(arguments.get("identification_type") or "").strip()[:40] or None,
        "correo": str(arguments.get("email") or "").strip()[:180] or None,
        "telefono": str(arguments.get("phone") or "").strip()[:80] or None,
        "sitio_web": str(arguments.get("website") or "").strip()[:500] or None,
        "direccion": str(arguments.get("address") or "").strip()[:500] or None,
        "provincia": str(arguments.get("province") or "").strip()[:80] or None,
        "canton": str(arguments.get("canton") or "").strip()[:80] or None,
        "distrito": str(arguments.get("district") or "").strip()[:80] or None,
        "notas": str(arguments.get("notes") or "").strip()[:4000] or None,
        "contactos": contacts,
    }
    summary = f"Crear proveedor {commercial_name or legal_name}"
    details = []
    if identification:
        details.append(f"identificación {identification}")
    if payload["telefono"]:
        details.append(f"teléfono {payload['telefono']}")
    if payload["correo"]:
        details.append(f"correo {payload['correo']}")
    if contacts:
        details.append(f"{len(contacts)} contacto(s)")
    if details:
        summary += ". " + "; ".join(details)

    action = _create_pending_action(
        db,
        conversation=conversation,
        user=user,
        message=message,
        action_type="create_provider",
        summary=summary,
        payload=payload,
    )
    return {
        "ok": True,
        "requires_confirmation": True,
        "action_public_id": action.public_id,
        "action_version": action.version,
        "summary": action.summary,
        "expires_at": action.expires_at.isoformat(),
    }


def prepare_lot_document_action(
    db: Session,
    conversation: AIConversation,
    user: Usuario,
    message: AIMessage,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    lot_code = str(arguments.get("lot_code") or "").strip()
    attachment_id = arguments.get("attachment_id")
    if not lot_code:
        return {"ok": False, "needs_clarification": True, "message": "Indique el código del lote."}
    if not attachment_id:
        return {
            "ok": False,
            "needs_clarification": True,
            "message": "Adjunte el documento o indique cuál archivo de este mensaje desea usar.",
        }
    resolved_lot = resolve_lot(db, lot_code)
    if not resolved_lot.get("ok"):
        return resolved_lot
    lot: OrdenTrabajo = resolved_lot["row"]
    if str(lot.estado or "").lower() == "unido":
        return {"ok": False, "message": "Ese lote fue unido y no admite nuevos documentos."}
    try:
        attachment_numeric_id = int(attachment_id)
    except (TypeError, ValueError):
        return {"ok": False, "needs_clarification": True, "message": "El archivo indicado no es válido."}
    attachment = (
        db.query(AIAttachment)
        .join(AIMessage, AIMessage.id == AIAttachment.message_id)
        .filter(
            AIAttachment.id == attachment_numeric_id,
            AIAttachment.usuario_id == user.id,
            AIMessage.conversation_id == conversation.id,
        )
        .first()
    )
    if not attachment:
        return {"ok": False, "needs_clarification": True, "message": "No encontré ese archivo en la conversación."}

    title = str(arguments.get("title") or attachment.original_name).strip()[:180]
    payload = {
        "lot_id": lot.id,
        "lot_code": lot.codigo_lote,
        "attachment_id": attachment.id,
        "title": title,
        "document_type": str(arguments.get("document_type") or "documento").strip().lower()[:60],
        "description": str(arguments.get("description") or "").strip() or None,
    }
    summary = f"Adjuntar '{attachment.original_name}' al lote {lot.codigo_lote} con el título '{title}'."
    action = _create_pending_action(
        db,
        conversation=conversation,
        user=user,
        message=message,
        action_type="attach_lot_document",
        summary=summary,
        payload=payload,
    )
    return {
        "ok": True,
        "requires_confirmation": True,
        "action_public_id": action.public_id,
        "action_version": action.version,
        "summary": action.summary,
        "expires_at": action.expires_at.isoformat(),
    }


def _sale_request_guide(
    *,
    client_name: str | None,
    lines: list[dict[str, Any]],
    attachment: AIAttachment | None,
    next_prompt: str,
) -> dict[str, Any]:
    valid_lines: list[dict[str, Any]] = []
    for item in lines:
        try:
            if _float_or_none(item.get("quantity_quintals")):
                valid_lines.append(item)
        except ValueError:
            continue
    completed = int(bool(client_name)) + int(bool(valid_lines)) + int(bool(attachment))
    return {
        "type": "guided_flow",
        "title": "Crear solicitud de venta",
        "description": "Dígame los datos de forma natural. Prepararé la solicitud y usted decidirá si se guarda.",
        "progress": round(completed / 3 * 100),
        "steps": [
            {
                "key": "client",
                "label": "Cliente comprador",
                "status": "complete" if client_name else "current",
                "value": client_name,
            },
            {
                "key": "coffee",
                "label": "Cantidad y tipo de café",
                "status": "complete" if valid_lines else ("current" if client_name else "pending"),
                "value": f"{len(valid_lines)} línea(s)" if valid_lines else None,
            },
            {
                "key": "purchase_order",
                "label": "Orden de compra (opcional)",
                "status": "complete" if attachment else ("current" if valid_lines else "pending"),
                "value": attachment.original_name if attachment else "Puede adjuntarla ahora o continuar sin ella",
            },
        ],
        "next_prompt": next_prompt,
        "attachment_hint": "Puede adjuntar PDF, Word, Excel o una fotografía de la orden de compra.",
    }


def _conversation_attachment(
    db: Session,
    conversation: AIConversation,
    user: Usuario,
    attachment_id: object,
) -> AIAttachment | None:
    try:
        numeric_id = int(attachment_id)
    except (TypeError, ValueError):
        return None
    return (
        db.query(AIAttachment)
        .join(AIMessage, AIMessage.id == AIAttachment.message_id)
        .filter(
            AIAttachment.id == numeric_id,
            AIAttachment.usuario_id == user.id,
            AIMessage.conversation_id == conversation.id,
        )
        .first()
    )


def prepare_sale_request_action(
    db: Session,
    conversation: AIConversation,
    user: Usuario,
    message: AIMessage,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    client_name = str(arguments.get("client_name") or "").strip()
    raw_lines = arguments.get("lines") if isinstance(arguments.get("lines"), list) else []
    attachment_id = arguments.get("purchase_order_attachment_id")
    attachment = _conversation_attachment(db, conversation, user, attachment_id) if attachment_id else None

    if attachment_id and not attachment:
        guide = _sale_request_guide(
            client_name=client_name or None,
            lines=raw_lines,
            attachment=None,
            next_prompt="Indique cuál archivo adjunto corresponde a la orden de compra.",
        )
        return {
            "ok": False,
            "needs_clarification": True,
            "message": "No encontré ese archivo dentro de esta conversación.",
            "components": [guide],
        }

    if not client_name:
        guide = _sale_request_guide(
            client_name=None,
            lines=raw_lines,
            attachment=attachment,
            next_prompt="¿Para cuál cliente comprador desea crear la solicitud?",
        )
        return {
            "ok": False,
            "needs_clarification": True,
            "message": "Empecemos por el cliente comprador.",
            "missing_fields": ["cliente comprador"],
            "components": [guide],
        }

    resolved = resolve_client(db, client_name)
    if not resolved.get("ok"):
        guide = _sale_request_guide(
            client_name=None,
            lines=raw_lines,
            attachment=attachment,
            next_prompt=str(resolved.get("message") or "Aclare el cliente comprador."),
        )
        return {**resolved, "components": [guide]}
    client: Cliente = resolved["row"]

    if not raw_lines:
        guide = _sale_request_guide(
            client_name=client.nombre_completo,
            lines=[],
            attachment=attachment,
            next_prompt="¿Cuántos quintales necesita y qué proceso prefiere: miel, natural, semilavado o flexible?",
        )
        return {
            "ok": False,
            "needs_clarification": True,
            "message": f"Ya identifiqué a {client.nombre_completo}. Ahora necesito la cantidad y el tipo de café.",
            "missing_fields": ["cantidad en quintales", "proceso preferido"],
            "components": [guide],
        }

    normalized_lines: list[dict[str, Any]] = []
    allowed_processes = {"miel", "natural", "semilavado", "flexible"}
    allowed_currencies = {"USD", "CRC", "EUR"}
    for index, item in enumerate(raw_lines, start=1):
        try:
            quantity = _float_or_none(item.get("quantity_quintals"))
            target_price = _float_or_none(item.get("target_price"))
        except ValueError as exc:
            return {"ok": False, "needs_clarification": True, "message": f"Línea {index}: {exc}"}
        if not quantity or quantity <= 0:
            return {
                "ok": False,
                "needs_clarification": True,
                "message": f"La línea {index} necesita una cantidad mayor que cero en quintales.",
                "components": [_sale_request_guide(
                    client_name=client.nombre_completo,
                    lines=normalized_lines,
                    attachment=attachment,
                    next_prompt=f"Indique la cantidad en quintales para la línea {index}.",
                )],
            }
        process = _norm_process(item.get("preferred_process"))
        if process not in allowed_processes:
            return {
                "ok": False,
                "needs_clarification": True,
                "message": f"El proceso de la línea {index} debe ser miel, natural, semilavado o flexible.",
            }
        currency = str(item.get("currency") or arguments.get("currency") or "USD").strip().upper()
        if currency not in allowed_currencies:
            return {"ok": False, "needs_clarification": True, "message": "La moneda debe ser USD, CRC o EUR."}
        description = str(item.get("description") or f"Café {process}").strip()[:500]
        normalized_lines.append({
            "description": description,
            "preferred_process": process,
            "quantity_quintals": round(quantity, 3),
            "target_price": round(target_price, 2) if target_price is not None else None,
            "currency": currency,
            "observations": str(item.get("observations") or "").strip() or None,
        })

    currency = str(arguments.get("currency") or normalized_lines[0]["currency"] or "USD").strip().upper()
    if currency not in allowed_currencies:
        return {"ok": False, "needs_clarification": True, "message": "La moneda debe ser USD, CRC o EUR."}
    total = round(sum(float(item["quantity_quintals"]) for item in normalized_lines), 3)
    payload = {
        "client_id": client.id,
        "client_name": client.nombre_completo,
        "currency": currency,
        "observations": str(arguments.get("observations") or "").strip() or None,
        "lines": normalized_lines,
        "purchase_order_attachment_id": attachment.id if attachment else None,
        "purchase_order_title": str(arguments.get("purchase_order_title") or (attachment.original_name if attachment else "Orden de compra")).strip()[:180],
    }
    summary = f"Crear solicitud de venta para {client.nombre_completo} por {total:g} quintales en {len(normalized_lines)} línea(s)"
    if attachment:
        summary += f", adjuntando '{attachment.original_name}'"
    summary += "."
    action = _create_pending_action(
        db,
        conversation=conversation,
        user=user,
        message=message,
        action_type="create_sale_request",
        summary=summary,
        payload=payload,
    )
    return {
        "ok": True,
        "requires_confirmation": True,
        "action_public_id": action.public_id,
        "action_version": action.version,
        "summary": action.summary,
        "expires_at": action.expires_at.isoformat(),
        "components": [_sale_request_guide(
            client_name=client.nombre_completo,
            lines=normalized_lines,
            attachment=attachment,
            next_prompt="Revise la tarjeta y confirme solo si todos los datos son correctos.",
        )],
    }


def _norm_process(value: object) -> str:
    normalized = str(value or "flexible").strip().lower().replace("_", " ")
    aliases = {
        "honey": "miel",
        "semi lavado": "semilavado",
        "semi-lavado": "semilavado",
        "washed": "semilavado",
        "any": "flexible",
        "cualquiera": "flexible",
    }
    return aliases.get(normalized, normalized)


def _acquire_write_lock(db: Session, lock_id: int) -> None:
    if db.get_bind().dialect.name == "postgresql":
        db.execute(text("SELECT pg_advisory_xact_lock(:lock_id)"), {"lock_id": lock_id})


def _execute_receipt(db: Session, action: AIPendingAction, user: Usuario) -> dict[str, Any]:
    _acquire_write_lock(db, 20_260_818)
    payload = dict(action.payload or {})
    payload["client_uuid"] = f"ai-{action.public_id}"
    validated = ReciboCreate.model_validate(payload)
    receipt = create_recibo_from_payload(db, validated, user)
    db.flush()
    serialized = serialize_recibo(receipt).model_dump(mode="json")
    return {
        "entity": "receipt",
        "id": receipt.id,
        "number": receipt.numero_recibo,
        "path": f"/recibos/{receipt.id}",
        "receipt": serialized,
    }


def _execute_farm_activity(db: Session, action: AIPendingAction, user: Usuario) -> dict[str, Any]:
    from core.routers.auth import require_roles
    from modules.mod_caficultura.router import apply_registro_finca_items
    from modules.mod_caficultura.schemas import RegistroFincaCreate

    require_roles("gerente", "administrativo", "supervisor_finca")(user)
    payload = dict(action.payload or {})
    farm = db.query(Finca).filter(
        Finca.id == int(payload["farm_id"]),
        Finca.activa.is_(True),
        Finca.gestion_fincas_habilitada.is_(True),
    ).first()
    activity = db.query(ActividadFinca).filter(
        ActividadFinca.id == int(payload["activity_id"]),
        ActividadFinca.activa.is_(True),
    ).first()
    if not farm:
        raise ValueError("La finca ya no existe, está inactiva o no tiene habilitada la gestión")
    if not activity:
        raise ValueError("La actividad ya no existe o está inactiva")
    activity_date = date.fromisoformat(str(payload["date"]))
    week_start = activity_date - timedelta(days=activity_date.weekday())
    row = RegistroFinca(
        fecha=activity_date,
        semana_inicio=week_start,
        semana_fin=week_start + timedelta(days=5),
        finca_id=farm.id,
        actividad_id=activity.id,
        descripcion=str(payload.get("description") or activity.nombre)[:255],
        estado="registrado",
        observaciones=payload.get("observations"),
        created_by_id=user.id,
    )
    validated = RegistroFincaCreate.model_validate({
        "fecha": activity_date, "finca_id": farm.id, "actividad_id": activity.id,
        "trabajadores": payload.get("trabajadores", []), "insumos": payload.get("insumos", []),
    })
    db.add(row)
    db.flush()
    apply_registro_finca_items(db, row, validated)
    return {
        "entity": "farm_activity",
        "id": row.id,
        "farm_id": farm.id,
        "farm_name": farm.nombre,
        "activity_name": activity.nombre,
        "date": activity_date.isoformat(),
        "path": f"/gestionfincas/{row.id}",
    }


def _execute_provider(db: Session, action: AIPendingAction, user: Usuario) -> dict[str, Any]:
    from core.routers.auth import require_roles
    require_roles("administrativo", "admin")(user)
    payload = dict(action.payload or {})
    identification = str(payload.get("identificacion") or "").strip() or None
    if identification:
        existing = db.query(Proveedor).filter(Proveedor.identificacion == identification).first()
        if existing:
            raise ValueError(f"Ya existe un proveedor con la identificación {identification}")

    row = Proveedor(
        codigo=next_code(db, Proveedor, "codigo", "PROV-", 4),
        nombre_legal=str(payload.get("nombre_legal") or "").strip(),
        nombre_comercial=str(payload.get("nombre_comercial") or "").strip() or None,
        identificacion=identification,
        tipo_identificacion=str(payload.get("tipo_identificacion") or "").strip() or None,
        correo=str(payload.get("correo") or "").strip() or None,
        telefono=str(payload.get("telefono") or "").strip() or None,
        sitio_web=str(payload.get("sitio_web") or "").strip() or None,
        direccion=str(payload.get("direccion") or "").strip() or None,
        provincia=str(payload.get("provincia") or "").strip() or None,
        canton=str(payload.get("canton") or "").strip() or None,
        distrito=str(payload.get("distrito") or "").strip() or None,
        notas=str(payload.get("notas") or "").strip() or None,
        activo=True,
    )
    if not row.nombre_legal:
        raise ValueError("El proveedor no tiene nombre legal")
    for item in payload.get("contactos") or []:
        row.contactos.append(ProveedorContacto(
            nombre=str(item.get("nombre") or "").strip()[:180],
            puesto=str(item.get("puesto") or "").strip()[:140] or None,
            correo=str(item.get("correo") or "").strip()[:180] or None,
            telefono=str(item.get("telefono") or "").strip()[:80] or None,
            principal=bool(item.get("principal")),
            activo=True,
            notas=str(item.get("notas") or "").strip()[:2000] or None,
        ))
    db.add(row)
    db.flush()
    return {
        "entity": "provider",
        "id": row.id,
        "code": row.codigo,
        "name": row.nombre_comercial or row.nombre_legal,
        "path": "/proveedores",
    }


def _execute_lot_document(db: Session, action: AIPendingAction, user: Usuario) -> tuple[dict[str, Any], Path]:
    payload = dict(action.payload or {})
    lot = db.query(OrdenTrabajo).filter(OrdenTrabajo.id == int(payload["lot_id"])).first()
    attachment = db.query(AIAttachment).filter(
        AIAttachment.id == int(payload["attachment_id"]),
        AIAttachment.usuario_id == user.id,
    ).first()
    if not lot:
        raise ValueError("El lote ya no existe")
    if not attachment:
        raise ValueError("El archivo temporal ya no existe")
    if str(lot.estado or "").lower() == "unido":
        raise ValueError("El lote fue unido y ya no admite documentos")
    source = resolve_storage_path(attachment.storage_key)
    if not source.is_file():
        raise ValueError("No se encontró el archivo almacenado")

    upload_root = Path(os.getenv("NAVIA_UPLOAD_DIR", "uploads")).resolve()
    target_folder = upload_root / "lotes" / str(lot.id)
    target_folder.mkdir(parents=True, exist_ok=True)
    extension = Path(attachment.original_name).suffix.lower()
    target = target_folder / f"{uuid.uuid4().hex}{extension}"
    shutil.copy2(source, target)
    relative = "/".join(target.relative_to(upload_root).parts)
    file_url = f"{APP_BASE_URL}/uploads/{relative}"

    document = DocumentoLote(
        ot_id=lot.id,
        titulo=str(payload.get("title") or attachment.original_name)[:180],
        tipo=str(payload.get("document_type") or "documento")[:60],
        descripcion=payload.get("description"),
        file_url=file_url,
        file_name=safe_original_name(attachment.original_name),
        content_type=attachment.media_type,
        size_bytes=attachment.size_bytes,
        created_by_id=user.id,
    )
    db.add(document)
    db.add(
        ComentarioLote(
            ot_id=lot.id,
            tipo="documento",
            comentario=f"{user.nombre} adjuntó mediante el asistente IA: {document.titulo} ({document.file_name}).",
            created_by_id=user.id,
        )
    )
    db.flush()
    return (
        {
            "entity": "lot_document",
            "id": document.id,
            "lot_id": lot.id,
            "lot_code": lot.codigo_lote,
            "path": f"/ot/{lot.codigo_lote}",
            "file_name": document.file_name,
        },
        target,
    )


def _execute_sale_request(db: Session, action: AIPendingAction, user: Usuario) -> tuple[dict[str, Any], list[Path]]:
    _acquire_write_lock(db, 20_260_819)
    payload = dict(action.payload or {})
    client = db.query(Cliente).filter(Cliente.id == int(payload["client_id"]), Cliente.activo.is_(True)).first()
    if not client:
        raise ValueError("El cliente ya no existe o está inactivo")
    lines = list(payload.get("lines") or [])
    if not lines:
        raise ValueError("La solicitud no contiene líneas de café")
    total = round(sum(_safe_positive_line_quantity(item) for item in lines), 3)
    processes = {str(item.get("preferred_process") or "flexible") for item in lines}
    parent_process = next(iter(processes)) if len(processes) == 1 else "flexible"
    prices = {item.get("target_price") for item in lines if item.get("target_price") is not None}
    row = SolicitudVenta(
        codigo=next_code(db, SolicitudVenta, "codigo", "SV-", 5),
        cliente_id=client.id,
        estado="pendiente",
        cantidad_quintales=total,
        proceso_preferido=None if parent_process == "flexible" else parent_process,
        precio_objetivo=next(iter(prices)) if len(prices) == 1 else None,
        moneda=str(payload.get("currency") or "USD").upper(),
        observaciones=payload.get("observations"),
        created_by_id=user.id,
    )
    db.add(row)
    db.flush()
    for item in lines:
        process = str(item.get("preferred_process") or "flexible")
        db.add(SolicitudVentaLinea(
            solicitud_id=row.id,
            descripcion=str(item.get("description") or f"Café {process}")[:500],
            proceso_preferido=None if process == "flexible" else process,
            cantidad_quintales=_safe_positive_line_quantity(item),
            precio_objetivo=item.get("target_price"),
            moneda=str(item.get("currency") or payload.get("currency") or "USD").upper(),
            observaciones=item.get("observations"),
        ))

    copied_paths: list[Path] = []
    attachment_id = payload.get("purchase_order_attachment_id")
    if attachment_id:
        attachment = db.query(AIAttachment).filter(
            AIAttachment.id == int(attachment_id),
            AIAttachment.usuario_id == user.id,
        ).first()
        if not attachment:
            raise ValueError("La orden de compra adjunta ya no está disponible")
        source = resolve_storage_path(attachment.storage_key)
        if not source.is_file():
            raise ValueError("No se encontró la orden de compra almacenada")
        upload_root = Path(os.getenv("NAVIA_UPLOAD_DIR", "uploads")).resolve()
        target_folder = upload_root / "solicitudes_venta" / str(row.id)
        target_folder.mkdir(parents=True, exist_ok=True)
        target = target_folder / f"{uuid.uuid4().hex}{Path(attachment.original_name).suffix.lower()}"
        shutil.copy2(source, target)
        copied_paths.append(target)
        relative = "/".join(target.relative_to(upload_root).parts)
        db.add(SolicitudVentaDocumento(
            solicitud_id=row.id,
            titulo=str(payload.get("purchase_order_title") or attachment.original_name)[:180],
            tipo="orden_compra",
            descripcion="Orden de compra adjuntada desde NAVIA IA.",
            file_url=f"{APP_BASE_URL}/uploads/{relative}",
            file_name=safe_original_name(attachment.original_name),
            content_type=attachment.media_type,
            size_bytes=attachment.size_bytes,
            created_by_id=user.id,
        ))
    db.flush()
    return ({
        "entity": "sale_request",
        "id": row.id,
        "code": row.codigo,
        "path": f"/solicitudes-venta/{row.id}",
        "quantity_quintals": total,
        "lines": len(lines),
        "purchase_order_attached": bool(attachment_id),
    }, copied_paths)


def _safe_positive_line_quantity(item: dict[str, Any]) -> float:
    quantity = _float_or_none(item.get("quantity_quintals"))
    if not quantity or quantity <= 0:
        raise ValueError("Cada línea debe tener una cantidad mayor que cero")
    return round(quantity, 3)


def confirm_pending_action(
    db: Session,
    public_id: str,
    user: Usuario,
    expected_version: int,
    *,
    channel: str = "web",
    request_id: str | None = None,
) -> AIPendingAction:
    query = db.query(AIPendingAction).filter(
        AIPendingAction.public_id == public_id,
        AIPendingAction.usuario_id == user.id,
    )
    if db.get_bind().dialect.name == "postgresql":
        query = query.with_for_update()
    action = query.first()
    if not action:
        raise HTTPException(status_code=404, detail="Acción pendiente no encontrada")
    if action.status == "executed":
        return action
    if action.status != "pending":
        raise HTTPException(status_code=409, detail=f"La acción ya está {action.status}")
    if action.version != expected_version:
        raise HTTPException(status_code=409, detail="La acción cambió; recargue la conversación antes de confirmar")
    expires = action.expires_at
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    if expires <= utcnow():
        action.status = "expired"
        action.version += 1
        db.commit()
        raise HTTPException(status_code=410, detail="La confirmación venció; solicite preparar la acción nuevamente")

    copied_paths: list[Path] = []
    try:
        action.status = "confirmed"
        action.confirmed_at = utcnow()
        action.version += 1
        if action.action_type == "create_receipt":
            result = _execute_receipt(db, action, user)
        elif action.action_type == "create_farm_activity":
            result = _execute_farm_activity(db, action, user)
        elif action.action_type == "create_provider":
            result = _execute_provider(db, action, user)
        elif action.action_type == "attach_lot_document":
            result, copied_path = _execute_lot_document(db, action, user)
            copied_paths.append(copied_path)
        elif action.action_type == "create_sale_request":
            result, copied_paths = _execute_sale_request(db, action, user)
        else:
            raise ValueError("Tipo de acción no soportado")
        action.status = "executed"
        action.executed_at = utcnow()
        action.result = result
        action.error = None
        db.add(
            AIAuditLog(
                usuario_id=user.id,
                conversation_id=action.conversation_id,
                channel=channel,
                action=action.action_type,
                status="executed",
                request_id=request_id,
                input_payload={"action_public_id": action.public_id, "payload": action.payload},
                output_payload=result,
            )
        )
        db.commit()
        db.refresh(action)
        return action
    except HTTPException:
        db.rollback()
        for copied_path in copied_paths:
            copied_path.unlink(missing_ok=True)
        raise
    except Exception as exc:
        db.rollback()
        for copied_path in copied_paths:
            copied_path.unlink(missing_ok=True)
        db.add(
            AIAuditLog(
                usuario_id=user.id,
                conversation_id=action.conversation_id,
                channel=channel,
                action=action.action_type,
                status="failed",
                request_id=request_id,
                input_payload={"action_public_id": public_id},
                output_payload={},
                error=str(exc)[:2000],
            )
        )
        db.commit()
        raise HTTPException(status_code=422, detail=f"No se pudo ejecutar la acción: {exc}") from exc


def cancel_pending_action(db: Session, public_id: str, user: Usuario) -> AIPendingAction:
    action = db.query(AIPendingAction).filter(
        AIPendingAction.public_id == public_id,
        AIPendingAction.usuario_id == user.id,
    ).first()
    if not action:
        raise HTTPException(status_code=404, detail="Acción pendiente no encontrada")
    if action.status == "executed":
        raise HTTPException(status_code=409, detail="La acción ya fue ejecutada y no puede cancelarse")
    if action.status == "pending":
        action.status = "cancelled"
        action.version += 1
        db.commit()
        db.refresh(action)
    return action
