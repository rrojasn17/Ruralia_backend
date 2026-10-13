from __future__ import annotations

import asyncio
import hashlib
import json
from decimal import Decimal
from io import BytesIO
from zipfile import ZipFile, ZIP_DEFLATED

import pytest
from fastapi import HTTPException, UploadFile

from test_iot_backup import databases, seed_backup, restore
from config import ADMIN_EMAIL
from core.models import Usuario
from modules.mod_caficultura import backup_archive
from modules.mod_caficultura.model_ai_consulting import AIAgent, AIConversation, AIMessage
from modules.mod_caficultura.model_ai_knowledge import AIKnowledgeDocument, AIKnowledgeChunk
from modules.mod_caficultura.model_notifications import NotificationPreference
from modules.mod_caficultura.models import Cliente
from modules.mod_caficultura.model_iot import IoTIntegration, IoTReading, IoTDashboardPreset
from modules.mod_caficultura.router import exportar_respaldo, importar_respaldo, serialize_backup_value


def roots_at(path):
    return {'uploads': path / 'public', 'ai_uploads': path / 'private', 'receipt_uploads': path / 'private' / 'receipts'}


def import_bytes(db, raw, current, mode='replace'):
    return asyncio.run(importar_respaldo(file=UploadFile(filename='backup.zip', file=BytesIO(raw)), current=current, db=db, mode=mode))


def local_admin(db):
    user = db.query(Usuario).one()
    user.correo = ADMIN_EMAIL
    user.is_superadmin = True
    user.hash_contrasena = 'local-access-hash'
    db.commit()
    return user


def test_complete_archive_replaces_data_files_and_preserves_local_access(databases, tmp_path, monkeypatch):
    source, target = databases
    seed_backup(source)
    source.add(AIAgent(id=1, name='Advisor', slug='advisor', model='test', instructions='Help', created_by_id=1))
    source.flush()
    source.add_all([
        AIConversation(id=1, usuario_id=1, agent_id=1, finca_id=1),
        NotificationPreference(usuario_id=1, email_enabled=False),
        AIKnowledgeDocument(id=1, agent_id=1, title='Guide', original_name='guide.txt', storage_key='guide.txt', media_type='text/plain', size_bytes=5, sha256=hashlib.sha256(b'guide').hexdigest()),
    ])
    source.flush()
    source.add_all([
        AIMessage(conversation_id=1, role='user', content='Test question'),
        AIKnowledgeChunk(document_id=1, position=0, content='guide'),
    ])
    source.commit()
    source_roots = roots_at(tmp_path / 'source')
    for root in source_roots.values():
        root.mkdir(parents=True, exist_ok=True)
    (source_roots['uploads'] / 'logo.png').write_bytes(b'logo')
    (source_roots['ai_uploads'] / 'guide.txt').write_bytes(b'guide')
    (source_roots['receipt_uploads'] / 'proof.pdf').write_bytes(b'proof')
    monkeypatch.setattr(backup_archive, 'upload_roots', lambda: source_roots)
    exported = exportar_respaldo(current=source.get(Usuario, 1), db=source, format='zip').body
    with ZipFile(BytesIO(exported)) as archive:
        assert set(archive.namelist()) == {'backup.json', 'uploads/logo.png', 'ai_uploads/guide.txt', 'receipt_uploads/proof.pdf'}
    restore(target, json.loads(exportar_respaldo(current=source.get(Usuario, 1), db=source).body))
    admin = local_admin(target)
    target.add_all([
        Cliente(codigo='EXTRA', numero_identificacion='EXTRA', nombre_completo='Unrelated destination data'),
        IoTIntegration(nombre='Other TTN', provider='ttn', application_id='other', integration_key='other', webhook_secret_hash='c' * 64),
    ])
    target.commit()
    destination_roots = roots_at(tmp_path / 'destination')
    destination_roots['uploads'].mkdir(parents=True)
    (destination_roots['uploads'] / 'old.txt').write_bytes(b'old')
    monkeypatch.setattr(backup_archive, 'upload_roots', lambda: destination_roots)
    result = import_bytes(target, exported, admin)
    target.expire_all()
    assert result['mode'] == 'replace' and result['advertencias'] == [] and result['archivos'] == 3
    assert target.query(Cliente).count() == 1
    assert target.query(IoTIntegration).count() == 1
    assert target.query(IoTReading).count() == 1
    assert target.query(AIMessage).one().content == 'Test question'
    assert target.query(AIKnowledgeChunk).one().content == 'guide'
    assert target.query(NotificationPreference).one().email_enabled is False
    assert target.get(Usuario, admin.id).hash_contrasena == 'local-access-hash'
    assert target.get(Usuario, admin.id).is_superadmin is True
    assert target.query(IoTDashboardPreset).one().usuario_id != admin.id
    assert not (destination_roots['uploads'] / 'old.txt').exists()
    assert (destination_roots['uploads'] / 'logo.png').read_bytes() == b'logo'
    assert (destination_roots['ai_uploads'] / 'guide.txt').read_bytes() == b'guide'
    assert (destination_roots['receipt_uploads'] / 'proof.pdf').read_bytes() == b'proof'
    import_bytes(target, exported, admin)
    assert target.query(IoTReading).count() == 1


def test_replace_failure_rolls_back_deleted_records(databases):
    source, target = databases
    payload = seed_backup(source)
    restore(target, payload)
    admin = local_admin(target)
    target.add(Cliente(codigo='KEEP', numero_identificacion='KEEP', nombre_completo='Keep on failure'))
    target.commit()
    payload['tables']['navia_iot_readings'][0]['node_id'] = 9999
    with pytest.raises(HTTPException) as error:
        import_bytes(target, json.dumps(payload).encode(), admin)
    assert error.value.status_code == 409
    assert target.query(Cliente).filter_by(codigo='KEEP').one()
    assert target.query(IoTReading).count() == 1
    assert target.get(Usuario, admin.id).hash_contrasena == 'local-access-hash'


def test_replace_is_restricted_and_complete_schema_cannot_omit_tables(databases):
    source, target = databases
    payload = seed_backup(source)
    restore(target, payload)
    user = target.query(Usuario).one()
    with pytest.raises(HTTPException) as error:
        import_bytes(target, json.dumps(payload).encode(), user)
    assert error.value.status_code == 403
    admin = local_admin(target)
    del payload['tables']['navia_ai_agents']
    with pytest.raises(HTTPException) as error:
        import_bytes(target, json.dumps(payload).encode(), admin)
    assert error.value.status_code == 422
    assert target.query(IoTReading).count() == 1


@pytest.mark.parametrize('path, digest', [('uploads/../../escape.txt', None), ('uploads/file.txt', 'wrong')])
def test_invalid_archived_paths_and_hashes_are_rejected(tmp_path, monkeypatch, path, digest):
    monkeypatch.setattr(backup_archive, 'upload_roots', lambda: roots_at(tmp_path))
    content = b'test'
    payload = {'files_included': True, 'files': [{'path': path, 'sha256': digest or hashlib.sha256(content).hexdigest()}]}
    buffer = BytesIO()
    with ZipFile(buffer, 'w', ZIP_DEFLATED) as archive:
        archive.writestr('backup.json', json.dumps(payload))
        archive.writestr(path, content)
    with pytest.raises(HTTPException) as error:
        backup_archive.read_backup(buffer.getvalue())
    assert error.value.status_code == 422


def test_file_restore_recovers_originals_on_failure(tmp_path, monkeypatch):
    roots = roots_at(tmp_path)
    roots['uploads'].mkdir(parents=True)
    (roots['uploads'] / 'old.txt').write_bytes(b'old')
    (roots['uploads'] / 'same.txt').write_bytes(b'original')
    monkeypatch.setattr(backup_archive, 'upload_roots', lambda: roots)
    with pytest.raises(RuntimeError):
        with backup_archive.FileRestore({'uploads/same.txt': b'new', 'uploads/new.txt': b'new'}, replace=True) as storage:
            storage.apply()
            raise RuntimeError('Simulate failed database commit')
    assert (roots['uploads'] / 'old.txt').read_bytes() == b'old'
    assert (roots['uploads'] / 'same.txt').read_bytes() == b'original'
    assert not (roots['uploads'] / 'new.txt').exists()


def test_backup_serialization_preserves_decimal_amounts():
    assert serialize_backup_value(Decimal('1234.56')) == '1234.56'
