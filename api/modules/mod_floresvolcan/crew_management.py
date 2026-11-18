"""Configuración de cuadrillas y vigencia de sus integrantes."""

import json
from datetime import date, datetime, timedelta, timezone
from fastapi import HTTPException
from .models import ModuleSetting, WorkCrewMember, Worker, PlantingOrder, HarvestOrder


def read_config(db, crew_id):
    row = db.query(ModuleSetting).filter_by(key=f"crew_config.{crew_id}").first()
    return (
        json.loads(row.value_text)
        if row
        else {"kind": "permanent", "activity": None, "work_date": None}
    )


def write_config(db, crew_id, value):
    row = db.query(ModuleSetting).filter_by(key=f"crew_config.{crew_id}").first()
    if not row:
        row = ModuleSetting(key=f"crew_config.{crew_id}")
        db.add(row)
    row.value_text = json.dumps(value, ensure_ascii=False, default=str)


def links(db, crew_id, opened=False):
    p = db.query(PlantingOrder.id).filter(PlantingOrder.crew_id == crew_id)
    h = db.query(HarvestOrder.id).filter(HarvestOrder.crew_id == crew_id)
    if opened:
        p = p.filter(
            PlantingOrder.status.in_(["pending_approval", "approved", "in_progress"])
        )
        h = h.filter(
            HarvestOrder.status.in_(["pending_approval", "approved", "in_progress"])
        )
    return bool(p.first() or h.first())


def serialize(db, crew, config=None):
    config = config if config is not None else read_config(db, crew.id)
    target = (
        date.fromisoformat(config["work_date"]) if config.get("work_date") else None
    )
    members = [
        m
        for m in crew.members
        if (
            target is not None
            and m.valid_from <= target
            and (m.valid_until is None or m.valid_until >= target)
        )
        or (target is None and m.valid_until is None)
    ]
    return {
        "id": crew.id,
        "name": crew.name,
        "active": crew.active,
        "planting_order_id": crew.planting_order_id,
        "kind": config["kind"],
        "activity": config.get("activity"),
        "work_date": config.get("work_date"),
        "members": [
            {
                "worker_id": m.worker_id,
                "name": m.worker.name,
                "valid_from": m.valid_from,
                "valid_until": m.valid_until,
            }
            for m in members
        ],
        "worker_ids": [m.worker_id for m in members],
    }


def save(db, crew, payload, current, today, creating=False):
    ids = list(dict.fromkeys(payload.worker_ids))
    workers = db.query(Worker).filter(Worker.id.in_(ids), Worker.active.is_(True)).all()
    if len(workers) != len(ids):
        raise HTTPException(422, "Todos los integrantes deben ser trabajadores activos")
    old = read_config(db, crew.id) if not creating else None
    used = not creating and links(db, crew.id)
    current_ids = {
        m.worker_id
        for m in crew.members
        if m.valid_until is None
        or (old and old.get("work_date") and str(m.valid_until) == old["work_date"])
    }
    configuration = {
        "kind": payload.kind,
        "activity": payload.activity if payload.kind == "temporary" else None,
        "work_date": str(payload.work_date) if payload.kind == "temporary" else None,
    }
    changed = old and any(old.get(k) != v for k, v in configuration.items())
    if used and changed:
        raise HTTPException(
            409,
            "La cuadrilla tiene historial; cree otra para cambiar el tipo, fecha o actividad",
        )
    if used and old.get("kind") == "temporary" and set(ids) != current_ids:
        raise HTTPException(
            409,
            "La cuadrilla temporal ya tiene actividad registrada; cree otra para cambiar los integrantes",
        )
    if (
        used
        and links(db, crew.id, opened=True)
        and (set(ids) != current_ids or not payload.active)
    ):
        raise HTTPException(
            409,
            "Hay órdenes pendientes; finalícelas antes de cambiar integrantes o desactivar la cuadrilla",
        )
    history = (old or {}).get("history", [])
    if old:
        history = history + [
            {
                "name": crew.name,
                "worker_ids": sorted(current_ids),
                "active": crew.active,
                "changed_by": current.id,
                "changed_at": datetime.now(timezone.utc).isoformat(),
            }
        ]
    if creating or not used:
        crew.members.clear()
        db.flush()
        crew.members = [
            WorkCrewMember(
                worker_id=i,
                valid_from=payload.work_date if payload.kind == "temporary" else today,
                valid_until=payload.work_date if payload.kind == "temporary" else None,
            )
            for i in ids
        ]
    else:
        for member in crew.members:
            if member.worker_id in current_ids and member.worker_id not in ids:
                member.valid_until = today - timedelta(days=1)
        for worker_id in set(ids) - current_ids:
            same = next(
                (
                    m
                    for m in crew.members
                    if m.worker_id == worker_id and m.valid_from == today
                ),
                None,
            )
            if same:
                same.valid_until = None
            else:
                crew.members.append(
                    WorkCrewMember(worker_id=worker_id, valid_from=today)
                )
    crew.name = payload.name.strip()
    crew.active = payload.active
    write_config(db, crew.id, {**configuration, "history": history})


def validate_assignment(db, crew, activity, planned_date):
    config = read_config(db, crew.id)
    if config["kind"] == "temporary" and (
        config.get("activity") != activity
        or config.get("work_date") != str(planned_date)
    ):
        raise HTTPException(
            422,
            "La cuadrilla temporal solo puede asignarse a su actividad y fecha configuradas",
        )
