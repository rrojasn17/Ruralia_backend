"""Only Caficultura owns these records, file references and compatibility rules."""
from pathlib import PurePosixPath
from urllib.parse import urlsplit

from fastapi import HTTPException

from core.module_backups import ModuleBackupSpec, SCHEMA
from . import backup_archive
from .manifest import MODULE_MANIFEST


def _relative(value):
    path = PurePosixPath(str(value))
    if path.is_absolute() or '..' in path.parts or '\\' in str(value):
        raise HTTPException(422, 'El respaldo de Caficultura contiene una referencia de archivo inválida')
    return path.as_posix()


def owned_files(payload):
    tables = payload.get('tables') or {}
    files = set()
    for name, prefix in [('navia_lote_documentos', 'lotes/'), ('navia_solicitudes_venta_documentos', 'solicitudes_venta/')]:
        for row in tables.get(name, []):
            path = urlsplit(str(row.get('file_url') or '')).path
            if not path.startswith('/uploads/' + prefix):
                raise HTTPException(422, 'Un documento de Caficultura apunta fuera de su carpeta')
            files.add('uploads/' + _relative(path.removeprefix('/uploads/')))
    for name in ['navia_ai_attachments', 'navia_ai_knowledge_documents']:
        for row in tables.get(name, []):
            path = _relative(row.get('storage_key') or '')
            first = PurePosixPath(path).parts[0] if PurePosixPath(path).parts else ''
            # Existing Café attachments predate module namespacing; new modules must use their own roots.
            if not (first.isdigit() or first in {'mod_caficultura', 'knowledge'}):
                raise HTTPException(422, 'Un adjunto de Caficultura apunta fuera de su carpeta')
            files.add('ai_uploads/' + path)
    for row in tables.get('navia_recibos_cafe', []):
        if row.get('liquidacion_comprobante_storage_key'):
            files.add('receipt_uploads/' + _relative(row['liquidacion_comprobante_storage_key']))
    return files


def upgrade_legacy(payload, warnings):
    if payload.get('schema') == SCHEMA:
        return payload
    if payload.get('schema') not in {'navia_backup_v1', 'navia_backup_v2'}:
        raise HTTPException(422, 'El archivo no corresponde a un respaldo de Caficultura compatible')
    if payload.get('module', 'mod_caficultura') != 'mod_caficultura':
        raise HTTPException(422, 'Este respaldo pertenece a otro módulo')
    tables = payload.get('tables')
    if not isinstance(tables, dict):
        raise HTTPException(422, 'Las tablas del respaldo son inválidas')
    users = tables.pop('navia_usuarios', [])
    had_config = tables.pop('navia_configuracion_sistema', None)
    if not isinstance(users, list) or any(not isinstance(row, dict) for row in users):
        raise HTTPException(422, 'Las referencias de identidad del respaldo anterior son inválidas')
    payload['identities'] = payload.get('identities', [{key: row[key] for key in ('id', 'correo', 'nombre') if key in row} for row in users])
    if users or had_config is not None:
        warnings.append('Respaldo anterior: se omitieron contraseñas, permisos y configuración global; solo se resuelven los autores por correo.')
    payload['_legacy_partial'] = payload['schema'] == 'navia_backup_v1'
    payload['_preserve_memberships'] = True
    payload['memberships'] = []
    payload['schema'] = SCHEMA
    payload['module'] = 'mod_caficultura'
    return payload


def remap_json(model, values, maps):
    if model.__tablename__ == 'navia_iot_dashboard_presets':
        nodes = maps.get('navia_iot_nodes', {})
        for widget in values.get('widgets') or []:
            widget['node_id'] = nodes.get(widget.get('node_id'), widget.get('node_id'))
            for source in (widget.get('inputs') or {}).values():
                source['node_id'] = nodes.get(source.get('node_id'), source.get('node_id'))
    return values


def after_restore(db):
    from core.services import set_config
    set_config(db, 'caficultura_catalogs_cleared', 'true', 'Mantener los catálogos propios restaurados')


def backup_spec():
    from database import Base
    from .lifecycle import _module_tables
    models = tuple(backup_archive.snapshot_models())
    all_models = {mapper.local_table.name: mapper.class_ for mapper in Base.registry.mappers}
    transient = tuple(all_models[item.name] for item in _module_tables() if item.name in backup_archive.EXCLUDED_TABLES)
    return ModuleBackupSpec(key='mod_caficultura', version=MODULE_MANIFEST['version'], models=models,
                            roots=backup_archive.upload_roots(), file_paths=owned_files, clear_models=transient,
                            remap_json=remap_json, upgrade_legacy=upgrade_legacy, after_restore=after_restore)
