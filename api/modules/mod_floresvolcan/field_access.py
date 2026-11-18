"""Acceso de cuadrillas: cookie propia, una orden y ningún permiso del sistema."""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import secrets
import re
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy import text
from sqlalchemy.orm import Session
from config import COOKIE_SECURE, COOKIE_SAMESITE, COOKIE_DOMAIN
from database import get_db
from .models import (
    Worker,
    PlantingOrder,
    HarvestOrder,
    FieldAccessSession,
    FieldAccessAttempt,
)
from .schemas import FieldLoginIn, PlantingRecordIn, HarvestRecordIn

public_router = APIRouter(prefix="/public/field")
COOKIE = "ruralia_fv_field"


def digest(value):
    return hashlib.sha256(str(value).encode()).hexdigest()


def utc(value):
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


@dataclass
class FieldActor:
    field_worker_id: int
    id: int | None
    nombre: str
    is_superadmin: bool = False
    rol: str = "trabajador"


def validate_record_worker(db, current, crew_id, code, paper_id=None):
    from .router import _field_worker, _active_crew_ids

    if isinstance(current, FieldActor):
        worker = _field_worker(db, current)
        if code and not secrets.compare_digest(code, worker.employee_code):
            raise HTTPException(403, "Código de trabajador incorrecto")
    elif not code:
        # La aprobación de una hoja conserva al autor de la captura; el superusuario
        # mantiene las operaciones de soporte. La captura móvil requiere código.
        if current.is_superadmin or paper_id:
            return None
        raise HTTPException(422, "Ingrese su código de trabajador de cuatro dígitos")
    else:
        worker = (
            db.query(Worker)
            .filter(Worker.employee_code == code, Worker.active.is_(True))
            .first()
        )
        own = _field_worker(db, current)
        if not worker or (own and own.id != worker.id):
            raise HTTPException(403, "Código de trabajador incorrecto")
    if crew_id not in _active_crew_ids(db, worker):
        raise HTTPException(403, "El trabajador no pertenece a esta cuadrilla")
    return worker


def bound_order(db, operation, order_id):
    model = PlantingOrder if operation == "planting" else HarvestOrder
    row = db.get(model, order_id)
    if not row or row.status not in {"approved", "in_progress"}:
        raise HTTPException(403, "Esta orden ya no admite registros")
    return row


@public_router.post("/login")
def login(
    payload: FieldLoginIn,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
):
    now = datetime.now(timezone.utc)
    scope = digest(payload.operation + ":" + payload.qr_token)
    ip = digest(request.client.host if request.client else "unknown")
    if db.get_bind().dialect.name == "postgresql":
        db.execute(
            text("SELECT pg_advisory_xact_lock(:key)"), {"key": int(ip[:15], 16)}
        )
    all_recent = db.query(FieldAccessAttempt).filter(
        FieldAccessAttempt.created_at > now - timedelta(minutes=15)
    )
    if all_recent.filter(FieldAccessAttempt.ip_hash == ip).count() >= 30:
        raise HTTPException(429, "Demasiados intentos", headers={"Retry-After": "900"})
    recent = db.query(FieldAccessAttempt).filter(
        FieldAccessAttempt.scope_hash == scope,
        FieldAccessAttempt.created_at > now - timedelta(minutes=15),
    )
    if (
        recent.count() >= 20
        or recent.filter(FieldAccessAttempt.ip_hash == ip).count() >= 5
    ):
        raise HTTPException(
            429,
            "Demasiados intentos. Espere 15 minutos.",
            headers={"Retry-After": "900"},
        )
    attempt = FieldAccessAttempt(scope_hash=scope, ip_hash=ip)
    db.add(attempt)
    db.commit()
    model = PlantingOrder if payload.operation == "planting" else HarvestOrder
    order = (
        db.query(model)
        .filter(
            model.qr_token == payload.qr_token,
            model.status.in_(["approved", "in_progress"]),
        )
        .first()
    )
    worker = (
        db.query(Worker)
        .filter(Worker.employee_code == payload.employee_code, Worker.active.is_(True))
        .first()
        if re.fullmatch(r"[0-9]{4}", payload.employee_code)
        else None
    )
    from .router import _active_crew_ids

    if not order or not worker or order.crew_id not in _active_crew_ids(db, worker):
        raise HTTPException(
            401, "Código o enlace incorrecto; verifique la orden asignada"
        )
    token = secrets.token_urlsafe(48)
    session = FieldAccessSession(
        token_hash=digest(token),
        worker_id=worker.id,
        operation=payload.operation,
        order_id=order.id,
        code_hash=digest(worker.employee_code),
        csrf_token=secrets.token_urlsafe(32),
        expires_at=now + timedelta(hours=12),
    )
    db.add(session)
    db.delete(attempt)
    db.commit()
    response.set_cookie(
        COOKIE,
        token,
        max_age=43200,
        httponly=True,
        secure=COOKIE_SECURE,
        samesite=COOKIE_SAMESITE,
        domain=COOKIE_DOMAIN,
        path="/",
    )
    response.headers["Cache-Control"] = "no-store"
    return {
        "worker_name": worker.name,
        "operation": session.operation,
        "order_id": order.id,
        "csrf_token": session.csrf_token,
    }


def field_session(request: Request, db: Session = Depends(get_db)):
    token = request.cookies.get(COOKIE, "")
    session = (
        db.query(FieldAccessSession)
        .filter(FieldAccessSession.token_hash == digest(token))
        .first()
        if token
        else None
    )
    if not session or utc(session.expires_at) <= datetime.now(timezone.utc):
        raise HTTPException(401, "Ingrese nuevamente su código")
    worker = db.get(Worker, session.worker_id)
    if (
        not worker
        or not worker.active
        or not secrets.compare_digest(digest(worker.employee_code), session.code_hash)
    ):
        raise HTTPException(401, "Acceso revocado")
    order = bound_order(db, session.operation, session.order_id)
    from .router import _active_crew_ids

    if order.crew_id not in _active_crew_ids(db, worker):
        raise HTTPException(403, "La orden ya no está asignada a este trabajador")
    return session, worker, order


@public_router.get("/session")
def details(
    response: Response, state=Depends(field_session), db: Session = Depends(get_db)
):
    from .router import field_tasks

    session, worker, order = state
    tasks = field_tasks(
        current=FieldActor(worker.id, worker.user_id, worker.name), db=db,
        include_waiting_material=session.operation == "planting"
    )
    if session.operation == "planting":
        tasks["planting_orders"] = [
            o for o in tasks["planting_orders"] if o["id"] == order.id
        ]
        tasks["open_batches"] = []
        tasks["harvest_orders"] = []
    else:
        tasks["planting_orders"] = []
        tasks["open_batches"] = [
            b for b in tasks["open_batches"] if b["harvest_order_id"] == order.id
        ]
        tasks["harvest_orders"] = [
            o for o in tasks["harvest_orders"] if o["id"] == order.id
        ]
    response.headers["Cache-Control"] = "private, no-store"
    return {
        "worker_name": worker.name,
        "operation": session.operation,
        "order_id": order.id,
        "order_number": order.number,
        "csrf_token": session.csrf_token,
        "tasks": tasks,
    }


def check_write(request, state, operation, order_id):
    session, _, _ = state
    if session.operation != operation or session.order_id != order_id:
        raise HTTPException(403, "La sesión solo permite registrar la orden asignada")
    if not secrets.compare_digest(
        request.headers.get("x-fv-csrf", ""), session.csrf_token
    ):
        raise HTTPException(403, "Sesión de registro inválida")


@public_router.post("/planting", status_code=201)
def plant(
    payload: PlantingRecordIn,
    request: Request,
    state=Depends(field_session),
    db: Session = Depends(get_db),
):
    from .router import register_planting

    check_write(request, state, "planting", payload.planting_order_id)
    if payload.paper_capture_id:
        raise HTTPException(403, "Las hojas se aprueban desde administración")
    _, worker, _ = state
    payload.worker_code = worker.employee_code
    return register_planting(
        payload, current=FieldActor(worker.id, worker.user_id, worker.name), db=db
    )


@public_router.post("/harvest", status_code=201)
def harvest(
    payload: HarvestRecordIn,
    request: Request,
    state=Depends(field_session),
    db: Session = Depends(get_db),
):
    from .router import register_harvest

    check_write(request, state, "harvest", payload.harvest_order_id)
    if payload.paper_capture_id:
        raise HTTPException(403, "Las hojas se aprueban desde administración")
    _, worker, _ = state
    payload.worker_code = worker.employee_code
    return register_harvest(
        payload, current=FieldActor(worker.id, worker.user_id, worker.name), db=db
    )


@public_router.post("/logout")
def logout(response: Response, request: Request, db: Session = Depends(get_db)):
    token = request.cookies.get(COOKIE, "")
    session = (
        db.query(FieldAccessSession)
        .filter(FieldAccessSession.token_hash == digest(token))
        .first()
    )
    if session and not secrets.compare_digest(
        request.headers.get("x-fv-csrf", ""), session.csrf_token
    ):
        raise HTTPException(403, "Sesión de registro inválida")
    if session:
        db.delete(session)
    db.commit()
    response.delete_cookie(COOKIE, path="/", domain=COOKIE_DOMAIN)
    return {"ok": True}
