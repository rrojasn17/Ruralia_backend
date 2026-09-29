import uuid
from fastapi.testclient import TestClient
from database import SessionLocal, engine

# Impide ejecutar estos tests contra la base real incluso con configuración errónea.
assert engine.url.get_backend_name() == 'sqlite'
assert str(engine.url.database).startswith('/tmp/navia-ai-tests-')
from main import app
from modules.mod_caficultura.models import Cliente, Finca, ActividadFinca, InsumoFinca, CompraInsumoFactura


def login(client):
    response = client.post('/auth/login', json={'correo': 'ai-tests-admin@example.com', 'password': 'AiTestsAdmin2026!'})
    assert response.status_code == 200, response.text


def test_public_activity_edit_and_chronological_history():
    with TestClient(app) as client:
        login(client)
        with SessionLocal() as db:
            suffix = uuid.uuid4().hex[:10]
            owner = Cliente(codigo=f'CL-{suffix}', nombre_completo='Cliente prueba', tipo_persona='personal', categoria='agro', numero_identificacion=suffix, activo=True)
            db.add(owner)
            db.flush()
            farm = Finca(codigo=f'FN-{suffix}', cliente_id=owner.id, nombre='Finca prueba', activa=True, gestion_fincas_habilitada=True)
            activity = ActividadFinca(nombre=f'Fertilizar {suffix}', tipo='fertilizacion', activa=True)
            db.add_all([farm, activity])
            db.commit()
            farm_id, activity_id = farm.id, activity.id
        def create(day, **extra):
            response = client.post('/gestion-fincas/registros', json={'fecha': day, 'finca_id': farm_id, 'actividad_id': activity_id, **extra})
            assert response.status_code == 201, response.text
            return response.json()
        recent = create('2026-09-20')
        old = create('2026-08-01', ispublic=True)
        assert recent['ispublic'] is False
        assert old['ispublic'] is True
        def ordered_ids():
            return [r['id'] for r in client.get('/gestion-fincas/registros').json() if r['finca_id'] == farm_id]
        assert ordered_ids() == [recent['id'], old['id']]
        changed = client.patch(f"/gestion-fincas/registros/{old['id']}", json={'fecha': '2026-09-22'})
        assert changed.status_code == 200, changed.text
        assert changed.json()['ispublic'] is True
        assert changed.json()['semana_inicio'] == '2026-09-21'
        assert ordered_ids() == [old['id'], recent['id']]
        assert client.patch(f"/gestion-fincas/registros/{old['id']}", json={'ispublic': False}).json()['ispublic'] is False


def test_discount_persistence_and_invalid_discount_preserves_inventory():
    with TestClient(app) as client:
        login(client)
        name = 'Insumo descuento ' + uuid.uuid4().hex[:10]
        payload = {'fecha': '2026-09-25', 'descuento': 26, 'lineas': [{'nombre': name, 'cantidad': 2, 'precio_unitario': 100, 'impuesto_porcentaje': 13}]}
        response = client.post('/insumos/compras', json=payload)
        assert response.status_code == 201, response.text
        invoice = response.json()
        assert [invoice[k] for k in ['subtotal', 'impuesto', 'descuento', 'total']] == [200, 26, 26, 200]
        assert client.get(f"/insumos/compras/{invoice['id']}").json()['descuento'] == 26
        with SessionLocal() as db:
            stock = db.query(InsumoFinca).filter_by(nombre=name).one().stock_actual
            count = db.query(CompraInsumoFactura).count()
        for discount in [-1, 227]:
            response = client.post('/insumos/compras', json={**payload, 'descuento': discount})
            assert response.status_code == 422, response.text
        with SessionLocal() as db:
            assert db.query(InsumoFinca).filter_by(nombre=name).one().stock_actual == stock
            assert db.query(CompraInsumoFactura).count() == count
        payload.pop('descuento')
        legacy = client.post('/insumos/compras', json=payload)
        assert legacy.status_code == 201, legacy.text
        assert legacy.json()['descuento'] == 0
        assert legacy.json()['total'] == 226


def test_ai_inventory_exposes_latest_dated_purchase_and_missing_stock(db_session):
    from datetime import date
    from modules.mod_caficultura.models import CompraInsumoLinea
    from modules.mod_caficultura.ai_metrics import inventory_status
    supply = InsumoFinca(nombre='Fertilizante referencia ' + uuid.uuid4().hex[:6], unidad='kg', stock_actual=0, stock_minimo=5, activo=True)
    db_session.add(supply)
    db_session.flush()
    for day, price in [(date(2026, 9, 20), 800), (date(2026, 8, 1), 700)]:
        invoice = CompraInsumoFactura(fecha=day, moneda='CRC')
        db_session.add(invoice)
        db_session.flush()
        db_session.add(CompraInsumoLinea(factura_id=invoice.id, insumo_id=supply.id, nombre_snapshot=supply.nombre, cantidad=5, unidad='kg', precio_unitario=price))
    db_session.commit()
    result = inventory_status(db_session, {})
    row = next(item for item in result['supplies'] if item['id'] == supply.id)
    assert row['out_of_stock'] is True
    assert row['historical_purchase']['unit_price'] == 800
    assert row['historical_purchase']['date'] == '2026-09-20'
    assert row['historical_purchase']['currency'] == 'CRC'
