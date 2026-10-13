"""Reconcile backup identities and references across installations."""
from __future__ import annotations

from copy import deepcopy
from typing import Any

from fastapi import HTTPException
from sqlalchemy import UniqueConstraint, func
from sqlalchemy.orm import Session


def _identity_keys(model) -> list[tuple[str, ...]]:
    table = model.__table__
    keys = {
        tuple(column.name for column in constraint.columns)
        for constraint in table.constraints
        if isinstance(constraint, UniqueConstraint)
    }
    keys.update(
        tuple(column.name for column in index.columns)
        for index in table.indexes
        if index.unique
        and index.dialect_options['postgresql'].get('where') is None
        and index.dialect_options['sqlite'].get('where') is None
    )
    return sorted(key for key in keys if key and 'id' not in key)


def _remap_references(model, values: dict[str, Any], id_maps: dict, *, same_table_only: bool = False) -> dict[str, Any]:
    values = deepcopy(values)
    for column in model.__table__.columns:
        if values.get(column.name) is None:
            continue
        for foreign_key in column.foreign_keys:
            target_table = foreign_key.column.table.name
            if (target_table == model.__tablename__) != same_table_only:
                continue
            mapping = id_maps.get(target_table, {})
            values[column.name] = mapping.get(values[column.name], values[column.name])
    if not same_table_only and model.__tablename__ == 'navia_iot_dashboard_presets':
        nodes = id_maps.get('navia_iot_nodes', {})
        for widget in values.get('widgets') or []:
            widget['node_id'] = nodes.get(widget.get('node_id'), widget.get('node_id'))
            for source in (widget.get('inputs') or {}).values():
                source['node_id'] = nodes.get(source.get('node_id'), source.get('node_id'))
    return values


def reconcile_backup_rows(db: Session, model, rows: list[dict], id_maps: dict):
    """Plan a whole table before writing so self references can also be remapped.

    A unique business key identifies a record across databases. An occupied
    source ID with a different identity gets a fresh ID rather than replacing
    an unrelated local record. Tables without business keys retain ID updates.
    """
    keys = _identity_keys(model)
    mapping = id_maps.setdefault(model.__tablename__, {})
    existing_maximum = db.query(func.max(model.id)).scalar()
    if existing_maximum is None:
        mapping.update({row['id']: row['id'] for row in rows})
        return [(None, _remap_references(model, _remap_references(model, row, id_maps), id_maps, same_table_only=True)) for row in rows]
    maximum = max([existing_maximum, *(row['id'] for row in rows)])
    planned = []
    used_targets = set()
    for row in rows:
        values = _remap_references(model, row, id_maps)
        source_id = row['id']
        usable_keys = [key for key in keys if all(values.get(field) is not None for field in key)]
        matches = {}
        for key in usable_keys:
            match = db.query(model).filter(*(getattr(model, field) == values[field] for field in key)).first()
            if match is not None:
                matches[match.id] = match
        if len(matches) > 1:
            raise HTTPException(status_code=409, detail=f'La tabla {model.__tablename__}, registro {source_id}, coincide con varios registros locales por sus claves únicas')
        existing = next(iter(matches.values()), None)
        if existing is None:
            existing = db.get(model, source_id)
            if existing is not None and usable_keys:
                # No identity matches: this ID belongs to another record.
                maximum += 1
                values['id'] = maximum
                existing = None
        else:
            values['id'] = existing.id
        if values['id'] in used_targets:
            raise HTTPException(status_code=422, detail=f'La tabla {model.__tablename__} contiene identidades duplicadas en el respaldo')
        used_targets.add(values['id'])
        mapping[source_id] = values['id']
        planned.append((existing, values))
    return [(existing, _remap_references(model, values, id_maps, same_table_only=True)) for existing, values in planned]
