"""Portable snapshots of Caficultura tables and uploaded files."""
from __future__ import annotations

import hashlib
import json
import os
import stat
from io import BytesIO
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory
from zipfile import BadZipFile, ZIP_DEFLATED, ZipFile

from fastapi import HTTPException

from config import AI_PRIVATE_UPLOAD_DIR, MAX_REQUEST_MB, RECEIPT_PRIVATE_UPLOAD_DIR

EXCLUDED_TABLES = {'navia_cliente_portal_sessions', 'navia_ai_telegram_pairings'}


def snapshot_models():
    from database import Base
    from core.models import Usuario, ConfiguracionSistema
    from modules.mod_caficultura.lifecycle import _module_tables

    included = {table.name for table in _module_tables()} - EXCLUDED_TABLES
    included.update((Usuario.__tablename__, ConfiguracionSistema.__tablename__))
    models = {mapper.local_table.name: mapper.class_ for mapper in Base.registry.mappers}
    ordered = [models[table.name] for table in Base.metadata.sorted_tables if table.name in included]
    # Widget/formula references live in JSON and therefore have no SQL FK edge.
    presets = models['navia_iot_dashboard_presets']
    return [model for model in ordered if model is not presets] + [presets]


def upload_roots():
    return {
        'uploads': Path(os.getenv('NAVIA_UPLOAD_DIR', 'uploads')).expanduser().resolve(),
        'ai_uploads': Path(AI_PRIVATE_UPLOAD_DIR).expanduser().resolve(),
        'receipt_uploads': Path(RECEIPT_PRIVATE_UPLOAD_DIR).expanduser().resolve(),
    }


def root_files(name, roots):
    root = roots[name]
    preceding = list(roots)[:list(roots).index(name)]
    if any(roots[key] == root for key in preceding):
        return []
    if not root.exists():
        return []
    nested = [path for key, path in roots.items() if key != name and root in path.parents]
    return sorted(path for path in root.rglob('*')
                  if path.is_file() and not path.is_symlink()
                  and root in path.resolve().parents
                  and not any(part.startswith('.navia-restore-') for part in path.relative_to(root).parts)
                  and not any(other == path or other in path.parents for other in nested))


def build_archive(payload):
    roots = upload_roots()
    buffer = BytesIO()
    manifest = []
    total = 0
    with ZipFile(buffer, 'w', ZIP_DEFLATED) as archive:
        for name, root in roots.items():
            for path in root_files(name, roots):
                content = path.read_bytes()
                total += len(content)
                if total > MAX_REQUEST_MB * 1024 * 1024 * 10:
                    raise HTTPException(413, 'Los archivos superan el tamaño máximo del respaldo')
                archive_path = f'{name}/{path.relative_to(root).as_posix()}'
                archive.writestr(archive_path, content)
                manifest.append({'path': archive_path, 'sha256': hashlib.sha256(content).hexdigest()})
        payload['files'] = manifest
        payload['files_included'] = True
        archive.writestr('backup.json', json.dumps(payload, ensure_ascii=False, indent=2))
    # Leave room for the multipart form around an imported archive.
    if buffer.tell() > MAX_REQUEST_MB * 1024 * 1024 - 65536:
        raise HTTPException(413, 'El respaldo supera el tamaño de importación permitido en esta instancia')
    return buffer.getvalue()


def safe_target(archive_path, roots):
    relative = PurePosixPath(archive_path)
    if relative.is_absolute() or '\\' in archive_path or '..' in relative.parts or len(relative.parts) < 2 or any(part.startswith('.navia-restore-') for part in relative.parts):
        raise HTTPException(422, 'El respaldo contiene una ruta de archivo inválida')
    root = roots.get(relative.parts[0])
    if root is None:
        raise HTTPException(422, 'El respaldo contiene una carpeta de archivos desconocida')
    target = root.joinpath(*relative.parts[1:])
    # Reject symlinks even when they currently point inside the upload root.
    if any(path.is_symlink() for path in (target, *target.parents) if path != root and root in path.parents):
        raise HTTPException(422, 'El respaldo intenta escribir a través de un enlace simbólico')
    if root not in target.resolve().parents or (target.exists() and not target.is_file()):
        raise HTTPException(422, 'El respaldo contiene una ruta de archivo inválida')
    return target


def read_backup(raw):
    if not raw.startswith(b'PK'):
        try:
            return json.loads(raw.decode('utf-8')), None
        except (UnicodeError, ValueError):
            raise HTTPException(422, 'El archivo no es un JSON o ZIP válido de respaldo NAVIA')
    try:
        with ZipFile(BytesIO(raw)) as archive:
            entries = archive.infolist()
            if len(entries) > 10000 or sum(item.file_size for item in entries) > MAX_REQUEST_MB * 1024 * 1024 * 10:
                raise HTTPException(413, 'El contenido del respaldo supera el tamaño permitido')
            names = [item.filename for item in entries]
            if len(set(names)) != len(names) or 'backup.json' not in names:
                raise HTTPException(422, 'El respaldo ZIP contiene entradas duplicadas o no incluye backup.json')
            payload = json.loads(archive.read('backup.json'))
            if not isinstance(payload, dict) or payload.get('files_included') is not True:
                raise HTTPException(422, 'El respaldo ZIP no declara sus archivos')
            manifest = payload.get('files')
            if not isinstance(manifest, list) or any(not isinstance(item, dict) for item in manifest):
                raise HTTPException(422, 'El manifiesto de archivos del respaldo es inválido')
            expected = {item.get('path'): item.get('sha256') for item in manifest}
            if len(expected) != len(manifest) or set(names) != {'backup.json', *expected}:
                raise HTTPException(422, 'Los archivos del ZIP no coinciden con su manifiesto')
            roots = upload_roots()
            targets = set()
            files = {}
            for item in entries:
                if item.filename == 'backup.json':
                    continue
                if stat.S_ISLNK(item.external_attr >> 16) or item.is_dir():
                    raise HTTPException(422, 'El respaldo ZIP contiene un tipo de archivo inválido')
                target = safe_target(item.filename, roots)
                if target in targets:
                    raise HTTPException(422, 'El respaldo contiene destinos de archivo duplicados')
                targets.add(target)
                content = archive.read(item)
                if hashlib.sha256(content).hexdigest() != expected[item.filename]:
                    raise HTTPException(422, 'Un archivo del respaldo no coincide con su huella de integridad')
                files[item.filename] = content
            return payload, files
    except HTTPException:
        raise
    except (BadZipFile, ValueError, KeyError, TypeError, RuntimeError, NotImplementedError):
        raise HTTPException(422, 'El archivo no es un ZIP válido de respaldo NAVIA')


class FileRestore:
    """Stage uploads and retain originals until the database commit succeeds."""
    def __init__(self, files, replace=False):
        self.files = files
        self.replace = replace
        self.roots = upload_roots()
        self.temporary = {}
        self.undo = []
        self.committed = False

    def __enter__(self):
        return self

    def _stage(self, name):
        if name not in self.temporary:
            self.roots[name].mkdir(parents=True, exist_ok=True)
            self.temporary[name] = TemporaryDirectory(prefix='.navia-restore-', dir=self.roots[name], ignore_cleanup_errors=True)
        return Path(self.temporary[name].name)

    def _retain(self, target, name):
        original = None
        if target.exists():
            original = self._stage(name) / f'original-{len(self.undo)}'
            os.replace(target, original)
        self.undo.append((target, original))

    def apply(self):
        if self.files is None:
            return
        incoming = {}
        for path, content in self.files.items():
            target = safe_target(path, self.roots)
            name = PurePosixPath(path).parts[0]
            staged = self._stage(name) / f'incoming-{len(incoming)}'
            staged.write_bytes(content)
            incoming[target] = (name, staged)
        if self.replace:
            for name in self.roots:
                for existing in root_files(name, self.roots):
                    if existing not in incoming:
                        self._retain(existing, name)
        for target, (name, staged) in incoming.items():
            target.parent.mkdir(parents=True, exist_ok=True)
            self._retain(target, name)
            os.replace(staged, target)

    def __exit__(self, *_):
        if not self.committed:
            for target, original in reversed(self.undo):
                target.unlink(missing_ok=True)
                if original is not None:
                    os.replace(original, target)
        for temporary in self.temporary.values():
            temporary.cleanup()
