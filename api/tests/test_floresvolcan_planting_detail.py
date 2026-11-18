from decimal import Decimal
from test_floresvolcan_reconciliation_v07 import fv as fv_fixture, value
from modules.mod_floresvolcan import models as m

fv = fv_fixture


def test_detail_has_worker_real_timestamp_origin_location_and_pending(fv):
    farm=m.ProductionSpace(code='F1',name='Finca prueba',type='farm',status='active')
    fv.db.add(farm);fv.db.flush()
    house=m.ProductionSpace(code='I1',name='Invernadero prueba',type='greenhouse',parent_id=farm.id,status='active')
    fv.db.add(house);fv.db.flush()
    fv.bed.parent_id=house.id
    fv.order.planned_quantity=1000
    worker=fv.db.query(m.Worker).first();worker.employee_code='2468'
    fv.db.commit()
    registered=value(fv.plant(100,worker_code='2468',notes='Hilera norte'),201)
    detail=value(fv.client.get(f'/floresvolcan/planting-orders/{fv.order.id}'),200)
    assert Decimal(detail['summary']['planted'])==100
    assert Decimal(detail['summary']['remaining_planned'])==900
    assert Decimal(detail['summary']['pending_delivered'])==Decimal(fv.line.quantity_delivered)-100
    row=detail['records'][0]
    assert row['id']==registered['id'] and row['worker_name']==worker.name
    assert row['registered_by_name']==fv.user.nombre
    assert row['recorded_at'] is not None
    assert row['recorded_at']!=row['planted_at']
    assert row['notes']=='Hilera norte'
    assert row['source']['lot_code']==fv.lot.lot_code
    assert row['location']['farm']['name']=='Finca prueba'
    assert row['location']['greenhouse']['name']=='Invernadero prueba'
    assert row['location']['bed']['id']==fv.bed.id
    assert Decimal(detail['locations'][0]['planted'])==100
    # A return does not count as planting, but reduces the delivered pending balance.
    fv.line.quantity_returned=50;fv.line.quantity_field_loss=10;fv.db.commit()
    updated=value(fv.client.get(f'/floresvolcan/planting-orders/{fv.order.id}'),200)
    assert Decimal(updated['summary']['remaining_planned'])==900
    assert Decimal(updated['summary']['pending_delivered'])==Decimal(fv.line.quantity_delivered)-160


def test_detail_pagination_does_not_change_totals_or_infer_missing_location(fv):
    for _ in range(41): value(fv.plant(1),201)
    path=f'/floresvolcan/planting-orders/{fv.order.id}'
    first=value(fv.client.get(path),200)
    second=value(fv.client.get(path+'?records_page=2'),200)
    assert len(first['records'])==40 and len(second['records'])==1
    assert first['summary']==second['summary']
    assert first['records_total']==41
    assert {r['id'] for r in first['records']}.isdisjoint(r['id'] for r in second['records'])
    assert first['locations'][0]['farm'] is None
    assert first['locations'][0]['greenhouse'] is None
    assert first['summary']['planned'] is None
    assert first['summary']['remaining_planned'] is None
    value(fv.client.get(path+'?records_page=0'),422)
