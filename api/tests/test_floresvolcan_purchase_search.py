"""Compra: búsqueda de materiales sin acceso administrativo al catálogo SAP."""
from test_floresvolcan_reconciliation_v07 import fv as fv_fixture, value
from test_floresvolcan_roles_v09 import activate

fv = fv_fixture


def test_purchase_search_name_code_variety_and_permissions(fv):
    activate(fv, 'admin')
    fv.product.description = 'Bulbos blancos de prueba'
    fv.product.variety = 'Navona'
    fv.db.commit()
    for term in ('blancos', 'navona', fv.product.code):
        result = value(fv.client.get('/floresvolcan/purchase-catalog', params={'q': term}), 200)
        assert fv.product.id in [row['id'] for row in result['items']]
    value(fv.client.get('/floresvolcan/sap-products'), 403)
    result = value(fv.client.get('/floresvolcan/purchase-catalog', params={'q': '%'}), 200)
    assert not result['items']
    fv.product.active = False
    fv.db.commit()
    assert not value(fv.client.get('/floresvolcan/purchase-catalog', params={'q': 'blancos'}), 200)['items']
    fv.user.rol = 'trabajador'
    fv.db.commit()
    value(fv.client.get('/floresvolcan/purchase-catalog'), 403)
