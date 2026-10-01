from datetime import date
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from main import app
from core.models import Usuario
from core.routers.auth import get_current_user
from modules.mod_caficultura.models import Cliente, Finca, RegistroFinca, RegistroFincaTrabajador, RegistroFincaInsumo


@pytest.fixture
def profile_data(db_session):
    suffix = uuid4().hex[:8]
    customer = Cliente(codigo=suffix, nombre_completo='Cliente pruebas', numero_identificacion=suffix)
    db_session.add(customer)
    db_session.flush()
    farms = [Finca(cliente_id=customer.id, codigo=f'{suffix}-{i}', nombre=f'Finca {i}', gestion_fincas_habilitada=True) for i in range(2)]
    db_session.add_all(farms)
    db_session.flush()
    rows = []
    for farm, day in [(farms[0], '2026-09-01'), (farms[0], '2026-09-30'), (farms[0], '2026-08-31'), (farms[1], '2026-09-15')]:
        row = RegistroFinca(finca_id=farm.id, fecha=date.fromisoformat(day), semana_inicio=date.fromisoformat(day), semana_fin=date.fromisoformat(day))
        db_session.add(row)
        db_session.flush()
        db_session.add_all([
            RegistroFincaTrabajador(registro_id=row.id, nombre_snapshot='Ana', horas=8, costo=10000),
            RegistroFincaTrabajador(registro_id=row.id, nombre_snapshot='Luis', horas=4, costo=5000),
            RegistroFincaInsumo(registro_id=row.id, nombre_snapshot='Abono', cantidad=2, costo_total=1200),
            RegistroFincaInsumo(registro_id=row.id, nombre_snapshot='Cal', cantidad=1, costo_total=800),
        ])
        rows.append(row)
    db_session.commit()
    return farms, rows


def test_farm_activity_filter_dates_costs_and_missing_farm(profile_data, monkeypatch):
    farms, rows = profile_data
    monkeypatch.setitem(app.dependency_overrides, get_current_user, lambda: Usuario(id=1, rol='gerente'))
    with TestClient(app) as client:
        response = client.get('/gestion-fincas/registros', params={'finca_id': farms[0].id, 'desde': '2026-09-01', 'hasta': '2026-09-30'})
        assert response.status_code == 200, response.text
        data = response.json()
        assert [row['id'] for row in data] == [rows[1].id, rows[0].id]
        assert data[0]['costo_mano_obra'] == 15000
        assert data[0]['costo_insumos'] == 2000
        assert data[0]['costo_total'] == 17000
        assert len(client.get('/gestion-fincas/registros', params={'finca_id': farms[0].id}).json()) == 3
        assert client.get('/gestion-fincas/registros', params={'desde': '2026-10-01', 'hasta': '2026-09-01'}).status_code == 422
        assert client.get('/gestion-fincas/registros', params={'finca_id': 99999999}).status_code == 404
        assert client.get('/gestion-fincas/registros', params={'finca_id': farms[0].id, 'desde': '2027-01-01'}).json() == []


def test_farm_activity_filter_does_not_grant_access_to_operator(monkeypatch):
    monkeypatch.setitem(app.dependency_overrides, get_current_user, lambda: Usuario(id=1, rol='operario', is_superadmin=False))
    with TestClient(app) as client:
        assert client.get('/gestion-fincas/registros?finca_id=1').status_code == 403
