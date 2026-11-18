"""Diferencias de corta requieren autorización de gerencia sin cerrar antes."""
from test_floresvolcan_reconciliation_v07 import fv, value
from test_floresvolcan_roles_v09 import activate
from modules.mod_floresvolcan.harvest_review import read_review


def prepare(fv):
    planted = value(fv.plant(1000), 201)
    order = fv.harvest_order(planted['batch_id'])
    value(fv.cut(planted['batch_id'], order['id'], 900), 201)
    value(fv.client.post(f"/floresvolcan/harvest-orders/{order['id']}/complete"), 200)
    calendar = value(fv.client.get('/floresvolcan/harvest-calendar?phase=in_harvest&page=1'), 200)
    assert calendar['items'][0]['status'] == 'in_harvest'
    return planted, {
        'comments': 'Cierre tras conteo físico',
        'segments': [{'planting_record_id': planted['id'], 'loss_quantity': 100,
                      'loss_reason': 'Plantas dañadas verificadas'}],
    }


def test_admin_requests_manager_approves(fv):
    planted, payload = prepare(fv)
    activate(fv, 'admin')
    path = f"/floresvolcan/production-batches/{planted['batch_id']}"
    requested = value(fv.client.post(path + '/technical-close', json=payload), 201)
    assert requested['review_required']
    calendar = value(fv.client.get('/floresvolcan/harvest-calendar?phase=under_review&page=1'), 200)
    assert calendar['total'] == 1
    assert calendar['items'][0]['status'] == 'under_review'
    summary = value(fv.client.get(path + '/summary'), 200)
    assert summary['closure_review']['status'] == 'pending'
    assert summary['unreconciled_segments'] == 1
    value(fv.client.post(path + '/closure-review/approve', json={'comments':'Verificado'}), 403)
    fv.user.rol = 'gerente'
    fv.db.commit()
    value(fv.client.post(path + '/closure-review/approve', json={'comments':'Conteo y daños verificados'}), 200)
    assert read_review(fv.db, planted['batch_id'])['status'] == 'closed'


def test_manager_returns_request_without_closing(fv):
    planted, payload = prepare(fv)
    activate(fv, 'admin')
    path = f"/floresvolcan/production-batches/{planted['batch_id']}"
    value(fv.client.post(path + '/technical-close', json=payload), 201)
    fv.user.rol = 'gerente'
    fv.db.commit()
    value(fv.client.post(path + '/closure-review/reject', json={'comments':'Recontar daño en cama'}), 200)
    summary = value(fv.client.get(path + '/summary'), 200)
    assert summary['closure_review']['status'] == 'rejected'
    assert summary['unreconciled_segments'] == 1
    value(fv.client.post(path + '/technical-close', json=payload), 201)
    assert len(read_review(fv.db, planted['batch_id'])['history']) == 1


def test_changed_balance_rejects_stale_approval(fv):
    from modules.mod_floresvolcan.models import HarvestRecord
    planted, payload = prepare(fv)
    activate(fv, 'admin')
    path = f"/floresvolcan/production-batches/{planted['batch_id']}"
    value(fv.client.post(path + '/technical-close', json=payload), 201)
    cut = fv.db.query(HarvestRecord).first()
    cut.bloom_1_qty = 899
    cut.total_output = 899
    fv.user.rol = 'gerente'
    fv.db.commit()
    value(fv.client.post(path + '/closure-review/approve', json={'comments':'Verificado en cama'}), 409)
    assert read_review(fv.db, planted['batch_id'])['status'] == 'pending'
