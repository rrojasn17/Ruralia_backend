"""Regressions for management roles, receipt allocation and commercial lifecycle."""
import uuid
from datetime import date
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from core.routers.auth import get_current_user, require_roles
from database import SessionLocal
from main import app
from core.models import Usuario, AppModule


@pytest.mark.parametrize('role,superadmin,allowed', [
    ('gerente', False, True), ('admin', False, True),
    ('operario', True, True), ('operario', False, False),
    ('administrativo', False, False),
])
def test_management_authorization(role, superadmin, allowed):
    user = SimpleNamespace(rol=role, is_superadmin=superadmin)
    if allowed:
        assert require_roles('gerente')(user) is user
    else:
        with pytest.raises(HTTPException) as error:
            require_roles('gerente')(user)
        assert error.value.status_code == 403


def test_admin_receipt_to_sold_lot_and_superuser_visibility():
    with TestClient(app) as client:
        with SessionLocal() as db:
            from core.module_manager import BUILTIN_MANIFESTS, install_module, activate_module
            if not db.query(AppModule).filter_by(key='mod_caficultura').first():
                manifest = BUILTIN_MANIFESTS['mod_caficultura']
                db.add(AppModule(key='mod_caficultura', name=manifest['name'], version=manifest['version'],
                                 module_type='industry', status='imported', manifest=manifest))
                db.commit()
            install_module(db, "mod_caficultura")
            activate_module(db, "mod_caficultura")
            user = Usuario(nombre='Administrador operativo', correo=f'{uuid.uuid4().hex}@example.com',
                           hash_contrasena='unused', rol='admin', activo=True, is_superadmin=False)
            db.add(user)
            db.commit()
            db.refresh(user)
            db.expunge(user)
        app.dependency_overrides[get_current_user] = lambda: user
        try:
            def post(path, payload, status=201):
                response = client.post(path, json=payload)
                assert response.status_code == status, response.text
                return response.json()
            day = date.today().isoformat()
            receipt = post('/recibos', {'fecha': day, 'productor_nombre': 'Productor prueba', 'cajuelas': 40, 'precio_fanega': 100000})
            assert user.id in [item['id'] for item in client.get('/empleados').json()]
            allocation = {'recibo_id': receipt['id'], 'cajuelas_asignadas': 40}
            payload = {'fecha_inicio': day, 'operario_id': user.id, 'proceso': 'miel', 'recibos': [allocation]}
            post('/ot', {**payload, 'recibos': [allocation, allocation]}, 422)
            lot = post('/ot', payload)
            post('/ot', payload, 422)
            post(f"/ot/{lot['id']}/solicitar-finalizacion", {}, 200)
            post(f"/ot/{lot['id']}/aprobar-finalizacion", {}, 200)
            assert lot['id'] in [item['id'] for item in client.get('/ot/lotes-aprobados').json()]
            sale = post('/solicitudes-venta', {'cantidad_quintales': 2, 'proceso_preferido': 'miel', 'precio_objetivo': 200})
            post(f"/solicitudes-venta/{sale['id']}/liquidar", {
                'fecha': day, 'lineas': [{'solicitud_linea_id': sale['lineas'][0]['id'], 'ot_id': lot['id'], 'cantidad_quintales': 2, 'precio_unitario': 200}]
            })
            assert lot['id'] not in [item['id'] for item in client.get('/ot/lotes-aprobados').json()]
            assert lot['id'] in [item['id'] for item in client.get('/ot/lotes-vendidos').json()]
            # A superuser with a different base role must still see all lots.
            user.rol, user.is_superadmin, user.id = 'operario', True, -1
            assert lot['id'] in [item['id'] for item in client.get('/ot').json()]
        finally:
            app.dependency_overrides.pop(get_current_user, None)
