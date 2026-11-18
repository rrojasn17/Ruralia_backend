"""Caficultura storage adapter for the shared, module-scoped archive transport."""
from pathlib import Path
import os

from config import AI_PRIVATE_UPLOAD_DIR, RECEIPT_PRIVATE_UPLOAD_DIR
from core import backup_archive as transport

EXCLUDED_TABLES = {'navia_cliente_portal_sessions', 'navia_ai_telegram_pairings'}


def snapshot_models():
    from database import Base
    from modules.mod_caficultura.lifecycle import _module_tables
    included = {item.name for item in _module_tables()} - EXCLUDED_TABLES
    models = {mapper.local_table.name: mapper.class_ for mapper in Base.registry.mappers}
    ordered = [models[item.name] for item in Base.metadata.sorted_tables if item.name in included]
    presets = models['navia_iot_dashboard_presets']
    return [model for model in ordered if model is not presets] + [presets]


def upload_roots():
    return {'uploads': Path(os.getenv('NAVIA_UPLOAD_DIR', 'uploads')).expanduser().resolve(),
            'ai_uploads': Path(AI_PRIVATE_UPLOAD_DIR).expanduser().resolve(),
            'receipt_uploads': Path(RECEIPT_PRIVATE_UPLOAD_DIR).expanduser().resolve()}


def build_archive(payload):
    from .backup_provider import owned_files
    return transport.build_archive(payload, upload_roots(), paths=owned_files(payload))


def read_backup(raw):
    return transport.read_backup(raw, upload_roots())


safe_target = transport.safe_target
root_files = transport.root_files


class FileRestore(transport.FileRestore):
    def __init__(self, files, replace=False, managed_paths=()):
        super().__init__(files, upload_roots(), replace=replace, managed_paths=managed_paths)
