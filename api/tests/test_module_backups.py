"""Module backups must not mutate another industry's records or core access."""
import json
from io import BytesIO
from zipfile import ZipFile

import pytest
from fastapi import HTTPException
from sqlalchemy import Column, ForeignKey, Integer, String

from test_iot_backup import seed_backup
from core.models import ConfiguracionSistema, ModuleUserRole, Usuario
from core.module_backups import ModuleBackupSpec, SCHEMA, export_bytes, resolve_backup_spec, restore_bytes, snapshot
from database import Base
from modules.mod_caficultura import backup_archive as cafe_files
from modules.mod_caficultura.model_iot import IoTNode, IoTReading
from modules.mod_floresvolcan import backup_provider as flower_files
from modules.mod_floresvolcan.models import BusinessStatus, PaperCapture, Supplier

pytest_plugins = ['test_iot_backup']


class FutureModuleRecord(Base):
    __module__ = 'modules.mod_example.models'
    __tablename__ = 'example_backup_records'
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey('navia_usuarios.id'), nullable=False)
    label = Column(String(80), nullable=False)


def make_root(db, email='local-root@example.test'):
    user = Usuario(nombre='Local root', correo=email, hash_contrasena='local-password',
                   rol='gerente', is_superadmin=True, activo=True, permisos=['*'])
    db.add(user)
    db.commit()
    return user


def test_export_contains_only_owned_tables_and_referenced_identities(databases):
    source, _ = databases
    seed_backup(source)
    source.add_all([Usuario(nombre='Unrelated', correo='other@example.test', hash_contrasena='never-copy', rol='gerente'),
                    ConfiguracionSistema(clave='app_name', valor='Shared brand'),
                    Supplier(code='FLOWER', name='Only Flores', country='CR')])
    source.commit()
    spec = resolve_backup_spec('mod_caficultura')
    data = snapshot(source, spec)
    assert data['schema'] == SCHEMA and data['module'] == 'mod_caficultura'
    assert 'navia_usuarios' not in data['tables']
    assert 'navia_configuracion_sistema' not in data['tables']
    assert not any(name.startswith('fv_') for name in data['tables'])
    assert data['identities'] == [{'id': 1, 'nombre': 'Backup', 'correo': 'iot-backup@example.test'}]
    assert 'hash_contrasena' not in json.dumps(data)
    assert 'never-copy' not in json.dumps(data)


@pytest.mark.parametrize('mode', ['merge', 'replace'])
def test_restore_never_changes_existing_accounts_brand_or_other_module_roles(databases, mode):
    source, target = databases
    seed_backup(source)
    actor = make_root(target, email='iot-backup@example.test')
    target.add_all([ConfiguracionSistema(clave='app_name', valor='Keep brand'),
                    ConfiguracionSistema(clave='session_days', valor='7'),
                    ModuleUserRole(usuario_id=actor.id, module_key='mod_floresvolcan', role='gerente'),
                    BusinessStatus(user_id=actor.id, scope_hash='original', message='Keep Flores'),
                    Supplier(code='KEEP', name='Flores supplier', country='CR')])
    target.commit()
    before = (actor.hash_contrasena, actor.rol, actor.is_superadmin, actor.activo, actor.permisos)
    spec = resolve_backup_spec('mod_caficultura')
    raw = export_bytes(source, spec, archive=False)
    result = restore_bytes(target, spec, raw, mode=mode, current=actor)
    target.expire_all()
    actor = target.get(Usuario, actor.id)
    assert before == (actor.hash_contrasena, actor.rol, actor.is_superadmin, actor.activo, actor.permisos)
    assert target.query(Usuario).count() == 1
    assert target.query(ConfiguracionSistema).filter_by(clave='app_name').one().valor == 'Keep brand'
    assert target.query(ConfiguracionSistema).filter_by(clave='session_days').one().valor == '7'
    assert target.query(ModuleUserRole).one().module_key == 'mod_floresvolcan'
    assert target.query(ModuleUserRole).one().role == 'gerente'
    assert target.query(BusinessStatus).one().message == 'Keep Flores'
    assert target.query(Supplier).one().code == 'KEEP'
    assert target.query(IoTReading).count() == 1
    assert result['identidades_nuevas'] == 0


def test_missing_authors_are_inactive_and_no_global_grants_are_copied(databases):
    source, target = databases
    seed_backup(source)
    source.query(Usuario).one().is_superadmin = True
    source.query(Usuario).one().permisos = ['*']
    source.commit()
    actor = make_root(target)
    spec = resolve_backup_spec('mod_caficultura')
    restore_bytes(target, spec, export_bytes(source, spec, archive=False), mode='replace', current=actor)
    author = target.query(Usuario).filter_by(correo='iot-backup@example.test').one()
    assert author.activo is False and author.is_superadmin is False and author.permisos == []
    assert author.rol == 'trabajador' and author.hash_contrasena != 'test-only'
    assert target.query(IoTNode).one().created_by_id == author.id
    assert actor.hash_contrasena == 'local-password'


@pytest.mark.parametrize('bad_change', ['foreign_table', 'foreign_module', 'credentials'])
def test_wrong_scope_is_rejected_before_any_replacement(databases, bad_change):
    source, target = databases
    seed_backup(source)
    seed_backup(target)
    actor = target.query(Usuario).one()
    actor.is_superadmin = True
    target.commit()
    spec = resolve_backup_spec('mod_caficultura')
    payload = snapshot(source, spec)
    if bad_change == 'foreign_table':
        payload['tables']['fv_business_status'] = []
    elif bad_change == 'foreign_module':
        payload['module'] = 'mod_floresvolcan'
    else:
        payload['identities'][0]['hash_contrasena'] = 'must-not-import'
    with pytest.raises(HTTPException) as error:
        restore_bytes(target, spec, json.dumps(payload).encode(), mode='replace', current=actor)
    assert error.value.status_code == 422
    assert target.query(IoTReading).count() == 1
    assert target.query(Usuario).one().hash_contrasena == 'test-only'


def test_flowers_replace_preserves_coffee_and_core_files(databases, tmp_path, monkeypatch):
    source, target = databases
    seed_backup(source)
    seed_backup(target)
    actor = target.query(Usuario).one()
    actor.is_superadmin = True
    target.add(Supplier(code='OLD', name='Old Flores', country='CR'))
    target.commit()
    roots = {'fv_documents': tmp_path / 'source/public/floresvolcan/po_events',
             'fv_paper': tmp_path / 'source/private/floresvolcan/paper'}
    for path in roots.values():
        path.mkdir(parents=True)
    (roots['fv_paper'] / 'capture.jpg').write_bytes(b'flower-paper')
    source.add_all([Supplier(code='SOURCE', name='Source Flores', country='CR'),
                    PaperCapture(capture_key='test-capture', operation='planting', storage_key='capture.jpg',
                                 sha256='f' * 64, mime_type='image/jpeg', created_by_id=1)])
    source.commit()
    monkeypatch.setattr(flower_files, 'upload_roots', lambda: roots)
    raw = export_bytes(source, resolve_backup_spec('mod_floresvolcan'))
    with ZipFile(BytesIO(raw)) as archive:
        assert set(archive.namelist()) == {'backup.json', 'fv_paper/capture.jpg'}
    roots = {'fv_documents': tmp_path / 'destination/public/floresvolcan/po_events',
             'fv_paper': tmp_path / 'destination/private/floresvolcan/paper'}
    for path in roots.values():
        path.mkdir(parents=True)
    coffee_file = tmp_path / 'destination/public/lotes/1/proof.pdf'
    coffee_file.parent.mkdir(parents=True)
    coffee_file.write_bytes(b'coffee-proof')
    logo = tmp_path / 'destination/public/branding/logo.png'
    logo.parent.mkdir()
    logo.write_bytes(b'core-logo')
    restore_bytes(target, resolve_backup_spec('mod_floresvolcan'), raw, mode='replace', current=actor)
    assert target.query(Supplier).one().code == 'SOURCE'
    assert target.query(IoTReading).count() == 1
    assert target.query(IoTNode).one().did == 'A840410000000001'
    assert coffee_file.read_bytes() == b'coffee-proof'
    assert logo.read_bytes() == b'core-logo'
    assert (roots['fv_paper'] / 'capture.jpg').read_bytes() == b'flower-paper'


def test_cafe_export_excludes_flores_files_and_restore_leaves_them_untouched(databases, tmp_path, monkeypatch):
    source, target = databases
    seed_backup(source)
    actor = make_root(target)
    roots = {'uploads': tmp_path / 'public', 'ai_uploads': tmp_path / 'private', 'receipt_uploads': tmp_path / 'private/receipts'}
    for path in roots.values():
        path.mkdir(parents=True, exist_ok=True)
    flower = roots['ai_uploads'] / 'floresvolcan/paper/photo.jpg'
    flower.parent.mkdir(parents=True)
    flower.write_bytes(b'keep-flores')
    core = roots['uploads'] / 'branding/logo.png'
    core.parent.mkdir()
    core.write_bytes(b'keep-core')
    monkeypatch.setattr(cafe_files, 'upload_roots', lambda: roots)
    spec = resolve_backup_spec('mod_caficultura')
    raw = export_bytes(source, spec)
    with ZipFile(BytesIO(raw)) as archive:
        assert set(archive.namelist()) == {'backup.json'}
    restore_bytes(target, spec, raw, mode='replace', current=actor)
    assert flower.read_bytes() == b'keep-flores' and core.read_bytes() == b'keep-core'


def test_legacy_coffee_backup_ignores_global_accounts_and_configuration(databases):
    source, target = databases
    seed_backup(source)
    actor = make_root(target, email='iot-backup@example.test')
    target.add(ConfiguracionSistema(clave='app_name', valor='Local brand'))
    target.commit()
    spec = resolve_backup_spec('mod_caficultura')
    payload = snapshot(source, spec)
    payload['schema'] = 'navia_backup_v2'
    payload['tables']['navia_usuarios'] = [{'id': 1, 'correo': actor.correo, 'nombre': 'Wrong name',
                                          'hash_contrasena': 'replace-password', 'rol': 'operario', 'is_superadmin': False}]
    payload['tables']['navia_configuracion_sistema'] = [{'id': 1, 'clave': 'app_name', 'valor': 'Foreign brand'}]
    del payload['identities']
    result = restore_bytes(target, spec, json.dumps(payload).encode(), mode='replace', current=actor)
    target.refresh(actor)
    assert actor.hash_contrasena == 'local-password' and actor.nombre == 'Local root' and actor.is_superadmin
    assert target.query(ConfiguracionSistema).filter_by(clave='app_name').one().valor == 'Local brand'
    assert target.query(IoTReading).count() == 1
    assert any('configuración global' in item for item in result['advertencias'])


def test_new_module_plugs_into_registry_without_changes_to_cafe_or_flores(databases, monkeypatch):
    from modules import registry
    source, target = databases
    author = make_root(source)
    actor = make_root(target)
    source.add(FutureModuleRecord(user_id=author.id, label='Future business'))
    target.add(Supplier(code='KEEP', name='Keep Flores', country='CR'))
    target.commit()
    source.commit()
    spec = ModuleBackupSpec(key='mod_example', version='1.0.0', models=(FutureModuleRecord,))
    runtime = registry.ModuleRuntimeSpec(key='mod_example', manifest={}, lifecycle_module='test', backup_entrypoint='test:backup')
    original = registry.resolve_entrypoint
    monkeypatch.setitem(registry.RUNTIME_MODULES, 'mod_example', runtime)
    monkeypatch.setattr(registry, 'resolve_entrypoint', lambda key: (lambda: spec) if key == 'test:backup' else original(key))
    resolved = resolve_backup_spec('mod_example')
    raw = export_bytes(source, resolved, archive=False)
    restore_bytes(target, resolved, raw, mode='replace', current=actor)
    assert target.query(FutureModuleRecord).one().label == 'Future business'
    assert target.query(Supplier).one().code == 'KEEP'


def test_provider_cannot_claim_global_or_other_module_tables():
    from modules import registry
    from unittest.mock import patch
    runtime = registry.ModuleRuntimeSpec(key='mod_example', manifest={}, lifecycle_module='test', backup_entrypoint='test:backup')
    for models in [(Usuario,), (Supplier,)]:
        spec = ModuleBackupSpec(key='mod_example', version='1.0.0', models=models)
        with patch.object(registry, 'get_runtime_spec', return_value=runtime), patch.object(registry, 'resolve_entrypoint', return_value=lambda: spec):
            with pytest.raises(RuntimeError):
                resolve_backup_spec('mod_example')


def test_cafe_cleanup_does_not_delete_accounts_sessions_or_flores(databases):
    from datetime import datetime, timedelta, timezone
    from core.models import SesionUsuario
    from config import ADMIN_EMAIL
    from modules.mod_caficultura.router import borrado_completo_app
    _, db = databases
    seed_backup(db)
    actor = db.query(Usuario).one()
    actor.correo = ADMIN_EMAIL
    actor.is_superadmin = True
    colleague = Usuario(nombre='Other industry', correo='keep@example.test', hash_contrasena='keep', rol='trabajador')
    db.add(colleague)
    db.flush()
    db.add_all([SesionUsuario(usuario_id=colleague.id, token='keep-session', expires_at=datetime.now(timezone.utc) + timedelta(days=1)),
                BusinessStatus(user_id=colleague.id, scope_hash='keep', message='Keep Flores')])
    db.commit()
    borrado_completo_app(current=actor, db=db)
    db.expire_all()
    assert db.query(Usuario).count() == 2
    assert db.query(SesionUsuario).one().token == 'keep-session'
    assert db.query(BusinessStatus).one().message == 'Keep Flores'
    assert db.query(IoTReading).count() == 0


def test_import_restores_roles_for_selected_module_only(databases):
    source, target = databases
    seed_backup(source)
    actor = make_root(target, email='iot-backup@example.test')
    source.add(ModuleUserRole(usuario_id=1, module_key='mod_caficultura', role='administrativo'))
    target.add(ModuleUserRole(usuario_id=actor.id, module_key='mod_floresvolcan', role='gerente'))
    target.commit()
    source.commit()
    spec = resolve_backup_spec('mod_caficultura')
    raw = export_bytes(source, spec, archive=False)
    restore_bytes(target, spec, raw, mode='replace', current=actor)
    assert target.query(ModuleUserRole).filter_by(module_key='mod_caficultura').one().role == 'administrativo'
    assert target.query(ModuleUserRole).filter_by(module_key='mod_floresvolcan').one().role == 'gerente'


def test_nonroot_cannot_replace_module_or_restore_memberships(databases):
    source, target = databases
    seed_backup(source)
    actor = make_root(target)
    source.add(ModuleUserRole(usuario_id=1, module_key='mod_caficultura', role='gerente'))
    source.commit()
    actor.is_superadmin = False
    target.commit()
    spec = resolve_backup_spec('mod_caficultura')
    raw = export_bytes(source, spec, archive=False)
    for mode in ['replace', 'merge']:
        with pytest.raises(HTTPException) as error:
            restore_bytes(target, spec, raw, mode=mode, current=actor)
        assert error.value.status_code == 403
    assert target.query(IoTNode).count() == 0


def test_root_can_restore_disabled_module_without_switching_industry(databases):
    from core.models import AppModule
    from core.routers.backups import export_backup, import_backup
    from fastapi import UploadFile
    import asyncio
    source, target = databases
    seed_backup(source)
    actor = make_root(target)
    target.add_all([AppModule(key='mod_caficultura', name='Coffee', module_type='industry', status='disabled'),
                    AppModule(key='mod_floresvolcan', name='Flores', module_type='industry', status='active')])
    target.commit()
    spec = resolve_backup_spec('mod_caficultura')
    raw = export_bytes(source, spec, archive=False)
    result = asyncio.run(import_backup(module_key=spec.key, file=UploadFile(filename='cafe.json', file=BytesIO(raw)),
                                       mode='replace', current=actor, db=target))
    assert result['ok'] is True
    assert target.query(AppModule).filter_by(key='mod_floresvolcan').one().status == 'active'
    assert target.query(AppModule).filter_by(key='mod_caficultura').one().status == 'disabled'
    assert json.loads(export_backup(spec.key, format='json', current=actor, db=target).body)['module'] == spec.key


def test_export_fails_when_a_referenced_module_file_is_missing(databases, tmp_path, monkeypatch):
    source, _ = databases
    seed_backup(source)
    source.add(PaperCapture(capture_key='missing', operation='planting', storage_key='missing.jpg',
                            sha256='f' * 64, mime_type='image/jpeg', created_by_id=1))
    source.commit()
    monkeypatch.setattr(flower_files, 'upload_roots', lambda: {'fv_documents': tmp_path / 'documents', 'fv_paper': tmp_path / 'paper'})
    with pytest.raises(HTTPException) as error:
        export_bytes(source, resolve_backup_spec('mod_floresvolcan'))
    assert error.value.status_code == 409


def test_legacy_roster_does_not_import_unreferenced_users(databases):
    source, target = databases
    seed_backup(source)
    actor = make_root(target, email='iot-backup@example.test')
    spec = resolve_backup_spec('mod_caficultura')
    payload = snapshot(source, spec)
    payload['schema'] = 'navia_backup_v2'
    del payload['identities']
    payload['tables']['navia_usuarios'] = [{'id': 1, 'correo': actor.correo, 'nombre': 'Author'},
                                          {'id': 99, 'correo': 'other-module@example.test', 'nombre': 'Not a Cafe author'}]
    restore_bytes(target, spec, json.dumps(payload).encode(), mode='replace', current=actor)
    assert target.query(Usuario).count() == 1
