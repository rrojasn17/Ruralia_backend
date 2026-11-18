"""Module-owned snapshots, with shared identities kept outside module data."""
from __future__ import annotations

import json
import secrets
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Callable

from fastapi import HTTPException
from sqlalchemy import func, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from config import APP_VERSION
from core import backup_archive
from core.backup_rows import reconcile_backup_rows
from core.backup_sequences import repair_sequences
from core.models import ModuleUserRole, Usuario
from security import hashear_contrasena

SCHEMA = 'ruralia_module_backup_v1'


@dataclass(frozen=True)
class ModuleBackupSpec:
    key: str
    version: str
    models: tuple
    roots: dict[str, Path] = field(default_factory=dict)
    file_paths: Callable = lambda payload: set()
    clear_models: tuple = ()
    remap_json: Callable = lambda model, values, maps: values
    upgrade_legacy: Callable = lambda payload, warnings: payload
    after_restore: Callable = lambda db: None


def resolve_backup_spec(key: str) -> ModuleBackupSpec:
    from modules.registry import get_runtime_spec, resolve_entrypoint
    runtime = get_runtime_spec(key)
    if not runtime or not runtime.backup_entrypoint:
        raise HTTPException(404, 'Este módulo no declara un proveedor de respaldos')
    spec = resolve_entrypoint(runtime.backup_entrypoint)()
    if not isinstance(spec, ModuleBackupSpec) or spec.key != key:
        raise RuntimeError('El proveedor de respaldo no coincide con su módulo')
    owned = {model.__tablename__ for model in spec.models}
    if len(owned) != len(spec.models) or owned & {'navia_usuarios', 'navia_configuracion_sistema', 'core_module_user_roles'}:
        raise RuntimeError('Un respaldo modular no puede declarar tablas globales')
    for model in (*spec.models, *spec.clear_models):
        if not model.__module__.startswith(f'modules.{key}.'):
            raise RuntimeError(f'El módulo {key} no es propietario de {model.__tablename__}')
        for foreign_key in model.__table__.foreign_keys:
            target = foreign_key.column.table.name
            if target not in owned | {'navia_usuarios'} and target not in {item.__tablename__ for item in spec.clear_models}:
                raise RuntimeError(f'{model.__tablename__} depende de una tabla ajena al módulo: {target}')
    return spec


def serialize_value(value):
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value


def parse_value(column, value):
    if value is None:
        return None
    try:
        kind = column.type.python_type
    except NotImplementedError:
        return value
    if kind in (datetime, date) and isinstance(value, str):
        return kind.fromisoformat(value.replace('Z', '+00:00'))
    return value


def _user_ids(spec, tables):
    ids = set()
    for model in spec.models:
        for column in model.__table__.columns:
            if any(key.column.table.name == 'navia_usuarios' for key in column.foreign_keys):
                ids.update(row[column.name] for row in tables.get(model.__tablename__, []) if row.get(column.name) is not None)
    return ids


def snapshot(db: Session, spec: ModuleBackupSpec):
    payload = {'schema': SCHEMA, 'module': spec.key, 'module_version': spec.version,
               'app_version': APP_VERSION, 'exported_at': datetime.now(timezone.utc).isoformat(),
               'files_included': False, 'tables': {}}
    with Session(bind=db.get_bind()) as view:
        if view.get_bind().dialect.name == 'postgresql':
            view.connection(execution_options={'isolation_level': 'REPEATABLE READ'})
        for model in spec.models:
            payload['tables'][model.__tablename__] = [
                {column.name: serialize_value(getattr(row, column.name)) for column in model.__table__.columns}
                for row in view.query(model).order_by(model.id).all()
            ]
        memberships = view.query(ModuleUserRole).filter(ModuleUserRole.module_key == spec.key).all()
        payload['memberships'] = [{'user_id': row.usuario_id, 'role': row.role} for row in memberships]
        ids = _user_ids(spec, payload['tables']) | {row.usuario_id for row in memberships}
        payload['identities'] = [{'id': row.id, 'correo': row.correo, 'nombre': row.nombre}
                                 for row in view.query(Usuario).filter(Usuario.id.in_(ids)).order_by(Usuario.id).all()]
    return payload


def export_bytes(db, spec, *, archive=True, extras=None):
    payload = snapshot(db, spec)
    if not archive:
        return json.dumps(payload, ensure_ascii=False, indent=2).encode()
    paths = set(spec.file_paths(payload))
    missing = {path for path in paths if not backup_archive.safe_target(path, spec.roots).is_file()}
    if missing:
        raise HTTPException(409, f'Faltan {len(missing)} adjuntos del módulo; revise sus archivos antes de exportar una copia completa')
    return backup_archive.build_archive(payload, spec.roots, paths=paths, extras=extras)


def _validate(payload, spec, warnings):
    if not isinstance(payload, dict) or not isinstance(payload.get('tables'), dict):
        raise HTTPException(422, 'El archivo no corresponde a un respaldo modular compatible')
    if payload.get('module') != spec.key:
        raise HTTPException(422, 'Este respaldo pertenece a otro módulo')
    if payload.get('schema') != SCHEMA:
        raise HTTPException(422, 'La versión del respaldo no es compatible')
    expected = {model.__tablename__ for model in spec.models}
    unknown = set(payload['tables']) - expected
    missing = expected - set(payload['tables'])
    if unknown:
        raise HTTPException(422, 'El respaldo contiene tablas ajenas o no soportadas: ' + ', '.join(sorted(unknown)))
    if missing and not payload.get('_legacy_partial'):
        raise HTTPException(422, 'El respaldo completo omite tablas: ' + ', '.join(sorted(missing)))
    if missing:
        warnings.append('El respaldo anterior omite tablas: ' + ', '.join(sorted(missing)))
    for model in spec.models:
        rows = payload['tables'].get(model.__tablename__, [])
        if not isinstance(rows, list) or any(not isinstance(row, dict) or type(row.get('id')) is not int or row['id'] < 1 for row in rows):
            raise HTTPException(422, f'La tabla {model.__tablename__} contiene registros inválidos')
        if len({row['id'] for row in rows}) != len(rows):
            raise HTTPException(422, f'La tabla {model.__tablename__} contiene IDs duplicados')
        columns = set(model.__table__.columns.keys())
        if any(set(row) - columns for row in rows):
            raise HTTPException(422, f'La tabla {model.__tablename__} requiere campos no soportados')
    identities = payload.get('identities', [])
    if not isinstance(identities, list) or any(not isinstance(row, dict) or type(row.get('id')) is not int or row['id'] < 1
            or not isinstance(row.get('correo'), str) or '@' not in row['correo'] or len(row['correo']) > 180
            or set(row) - {'id', 'correo', 'nombre'} for row in identities):
        raise HTTPException(422, 'Las referencias de identidad del respaldo son inválidas')
    if len({row['id'] for row in identities}) != len(identities) or len({row['correo'].strip().lower() for row in identities}) != len(identities):
        raise HTTPException(422, 'El respaldo contiene referencias de identidad duplicadas')
    memberships = payload.get('memberships', [])
    if not isinstance(memberships, list) or any(not isinstance(row, dict) or type(row.get('user_id')) is not int
            or not isinstance(row.get('role'), str) or not row['role'].strip() or len(row['role']) > 40
            or set(row) != {'user_id', 'role'} for row in memberships):
        raise HTTPException(422, 'Las asignaciones de rol del módulo son inválidas')
    if len({row['user_id'] for row in memberships}) != len(memberships):
        raise HTTPException(422, 'El respaldo repite asignaciones de rol')
    required = _user_ids(spec, payload['tables']) | {row['user_id'] for row in memberships}
    if required - {row['id'] for row in identities}:
        raise HTTPException(422, 'El respaldo omite las identidades referenciadas por sus registros')


def _resolve_identities(db, identities):
    mapping = {}
    created = 0
    for identity in identities:
        email = identity['correo'].strip().lower()
        user = db.query(Usuario).filter(func.lower(Usuario.correo) == email).first()
        if user is None:
            # Historical authors are portable; login credentials and global grants are not.
            user = Usuario(nombre=str(identity.get('nombre') or email)[:180], correo=email,
                           hash_contrasena=hashear_contrasena(secrets.token_urlsafe(40)),
                           rol='trabajador', activo=False, is_superadmin=False, permisos=[])
            db.add(user)
            db.flush()
            created += 1
        mapping[identity['id']] = user.id
    return mapping, created


def restore_bytes(db, spec, raw, *, mode='merge', current=None):
    if mode not in {'merge', 'replace'}:
        raise HTTPException(422, 'Modo de restauración inválido')
    if mode == 'replace' and not getattr(current, 'is_superadmin', False):
        raise HTTPException(403, 'Solo Root / SuperAdmin puede reemplazar los datos de un módulo')
    payload, files = backup_archive.read_backup(raw, spec.roots)
    warnings = []
    if not isinstance(payload, dict):
        raise HTTPException(422, 'El respaldo debe contener un objeto JSON')
    legacy = payload.get('schema') != SCHEMA
    if not legacy:
        payload.pop('_legacy_partial', None)
        payload.pop('_preserve_memberships', None)
    payload = spec.upgrade_legacy(deepcopy(payload), warnings) if legacy else payload
    _validate(payload, spec, warnings)
    if payload.get('files_included') is True and files is None:
        raise HTTPException(422, 'El respaldo declara archivos que no están incluidos en un ZIP')
    allowed = set(spec.file_paths(payload))
    if files is not None:
        foreign = set(files) - allowed
        if foreign and not legacy:
            raise HTTPException(422, 'El respaldo contiene archivos ajenos a los registros del módulo')
        if foreign:
            warnings.append(f'Se omitieron {len(foreign)} archivos globales o ajenos al módulo del respaldo anterior.')
        files = {path: content for path, content in files.items() if path in allowed}
    if payload.get('memberships') and not getattr(current, 'is_superadmin', False):
        raise HTTPException(403, 'Solo Root / SuperAdmin puede restaurar asignaciones de rol')
    managed = set(spec.file_paths(snapshot(db, spec))) if mode == 'replace' and files is not None else set()
    if files is None and mode == 'replace':
        warnings.append('Este JSON conserva los archivos existentes; no incluye adjuntos.')
    imported = updated = 0
    table_counts = {}
    try:
        with backup_archive.FileRestore(files, spec.roots, replace=mode == 'replace', managed_paths=managed) as storage:
            if db.get_bind().dialect.name == 'postgresql':
                quote = db.get_bind().dialect.identifier_preparer.quote
                names = ', '.join(quote(model.__tablename__) for model in spec.models)
                if names:
                    db.execute(text(f'LOCK TABLE {names} IN ACCESS EXCLUSIVE MODE'))
            if mode == 'replace':
                clear = {model.__tablename__: model for model in (*spec.models, *spec.clear_models)}
                from database import Base
                for table in reversed(Base.metadata.sorted_tables):
                    if table.name in clear:
                        db.execute(table.delete())
                db.expire_all()
            required_users = _user_ids(spec, payload['tables']) | {row['user_id'] for row in payload.get('memberships', [])}
            user_map, created_users = _resolve_identities(db, [row for row in payload.get('identities', []) if row['id'] in required_users])
            id_maps = {'navia_usuarios': user_map}
            for model in spec.models:
                rows = payload['tables'].get(model.__tablename__, [])
                table_counts[model.__tablename__] = len(rows)
                columns = {column.name: column for column in model.__table__.columns}
                parsed = [{key: parse_value(columns[key], value) for key, value in row.items()} for row in rows]
                for existing, values in reconcile_backup_rows(db, model, parsed, id_maps):
                    values = spec.remap_json(model, values, id_maps)
                    if existing is None:
                        db.add(model(**values))
                        imported += 1
                    else:
                        for key, value in values.items():
                            setattr(existing, key, value)
                        updated += 1
                db.flush()
            if getattr(current, 'is_superadmin', False) and not payload.get('_preserve_memberships'):
                if mode == 'replace':
                    db.query(ModuleUserRole).filter(ModuleUserRole.module_key == spec.key).delete(synchronize_session=False)
                for item in payload.get('memberships', []):
                    user_id = user_map[item['user_id']]
                    row = db.query(ModuleUserRole).filter_by(module_key=spec.key, usuario_id=user_id).first()
                    if row is None:
                        db.add(ModuleUserRole(module_key=spec.key, usuario_id=user_id, role=item['role']))
                    else:
                        row.role = item['role']
            repair_sequences(db, spec.models)
            spec.after_restore(db)
            db.flush()
            storage.apply()
            db.commit()
            storage.committed = True
    except HTTPException:
        db.rollback()
        raise
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(409, 'El respaldo contiene referencias o claves que entran en conflicto con los datos del módulo') from exc
    except (ValueError, TypeError) as exc:
        db.rollback()
        raise HTTPException(422, 'El respaldo contiene valores no compatibles con el módulo') from exc
    except Exception:
        db.rollback()
        raise
    if created_users:
        warnings.append(f'Se crearon {created_users} identidades históricas inactivas, sin contraseñas ni permisos importados.')
    return {'ok': True, 'module': spec.key, 'mode': mode, 'importados': imported, 'actualizados': updated,
            'tablas': table_counts, 'advertencias': warnings, 'archivos': len(files or {}), 'identidades_nuevas': created_users}
