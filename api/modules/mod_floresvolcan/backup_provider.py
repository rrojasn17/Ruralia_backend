"""Flores Volcán owns its backup schema and file boundaries."""
import json
import os
from pathlib import Path, PurePosixPath

from fastapi import HTTPException

from core.module_backups import ModuleBackupSpec
from .manifest import MODULE_MANIFEST


def upload_roots():
    private = Path(os.getenv('AI_PRIVATE_UPLOAD_DIR', '/app/private_uploads')).expanduser().resolve()
    public = Path(os.getenv('NAVIA_UPLOAD_DIR', 'uploads')).expanduser().resolve()
    return {'fv_documents': public / 'floresvolcan' / 'po_events',
            'fv_paper': Path(os.getenv('FV_PAPER_STORAGE_DIR') or private / 'floresvolcan' / 'paper').resolve()}


def _document_relative(value):
    text = str(value).replace('\\', '/')
    marker = '/floresvolcan/po_events/'
    relative = text.split(marker, 1)[1] if marker in text else text
    path = PurePosixPath(relative)
    if path.is_absolute() or '..' in path.parts or not path.parts:
        raise HTTPException(422, 'El respaldo contiene un documento ajeno a Flores Volcán')
    return path.as_posix()


def owned_files(payload):
    files = set()
    for row in payload['tables'].get('fv_purchase_order_event_attachments', []):
        files.add('fv_documents/' + _document_relative(row['storage_key']))
    for row in payload['tables'].get('fv_paper_captures', []):
        key = str(row['storage_key'])
        if PurePosixPath(key).name != key or key in {'', '.', '..'} or '\\' in key:
            raise HTTPException(422, 'El respaldo contiene una hoja de campo con ruta inválida')
        files.add('fv_paper/' + key)
    return files


def remap_json(model, values, maps):
    if model.__tablename__ == 'fv_purchase_order_event_attachments':
        values['storage_key'] = str(upload_roots()['fv_documents'] / _document_relative(values['storage_key']))
    if model.__tablename__ == 'fv_module_settings' and values.get('value_text'):
        users = maps.get('navia_usuarios', {})
        user_fields = {'user_id', 'approver_user_id', 'requested_by_id', 'approved_by', 'closed_by'}
        def remap(value):
            if isinstance(value, dict):
                return {key: users.get(item, item) if key in user_fields and isinstance(item, int) else remap(item)
                        for key, item in value.items()}
            if isinstance(value, list):
                return [remap(item) for item in value]
            return value
        try:
            parsed = json.loads(values['value_text'])
        except (ValueError, TypeError):
            pass
        else:
            values['value_text'] = json.dumps(remap(parsed), ensure_ascii=False)
    return values


def backup_spec():
    from database import Base
    from .lifecycle import _tables
    models = {mapper.local_table.name: mapper.class_ for mapper in Base.registry.mappers}
    owned = {item.name for item in _tables()} - {'fv_backup_runs'}
    ordered = tuple(models[item.name] for item in Base.metadata.sorted_tables if item.name in owned)
    return ModuleBackupSpec(key='mod_floresvolcan', version=MODULE_MANIFEST['version'], models=ordered,
                            roots=upload_roots(), file_paths=owned_files, remap_json=remap_json)
