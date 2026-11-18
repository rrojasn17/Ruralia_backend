"""Regresión v0.10: identidad por módulo, capacidad de siembra y acceso QR.

Se ejecuta en base efímera del fixture, sin servicios externos.
"""
from decimal import Decimal

from sqlalchemy import func
from core.models import AppModule, ModuleUserRole, Usuario
from core.module_manager import role_for_module, module_permissions
from modules.mod_floresvolcan import models as m
from modules.mod_floresvolcan.manifest import MODULE_MANIFEST
from test_floresvolcan_reconciliation_v07 import fv as fv_fixture

fv=fv_fixture


def test_membership_isolated_by_module_and_automatic_roles(fv):
    fv.user.is_superadmin=False
    fv.user.rol='admin'
    row=AppModule(key='mod_floresvolcan',name='Flores',status='active',module_type='industry',manifest=MODULE_MANIFEST)
    fv.db.add(row)
    fv.db.add(ModuleUserRole(usuario_id=fv.user.id,module_key='mod_floresvolcan',role='trabajador'))
    fv.db.commit()
    assert role_for_module(fv.db,fv.user,'mod_floresvolcan')=='trabajador'
    assert role_for_module(fv.db,fv.user,'mod_caficultura') is None
    perms=module_permissions(fv.db,fv.user,row)
    assert 'floresvolcan:field:use' in perms
    assert 'floresvolcan:sap:manage' not in perms
    assert 'floresvolcan:workers:manage' not in perms
    fv.db.query(ModuleUserRole).filter_by(usuario_id=fv.user.id,module_key='mod_floresvolcan').update({'role':'admin'})
    fv.db.commit()
    assert 'floresvolcan:workers:manage' in module_permissions(fv.db,fv.user,row)
    assert role_for_module(fv.db,fv.user,'mod_caficultura') is None


def make_area(fv):
    crop=fv.db.query(m.FlowerCrop).filter_by(code='LIRIO').first()
    other=m.FlowerCrop(code='GIRASOL-PRUEBA',name='Girasol de prueba',material_type='bulbo',active=True)
    farm=m.ProductionSpace(code='FVTEST-FARM',name='Finca A',type='farm',status='active')
    fv.db.add_all([other,farm]);fv.db.flush()
    greenhouse=m.ProductionSpace(code='FVTEST-GH',name='Invernadero 1',type='greenhouse',parent_id=farm.id,status='active')
    fv.db.add(greenhouse);fv.db.flush()
    beds=[]
    for i,qty in [(1,110),(2,90)]:
        b=m.ProductionSpace(code=f'FVTEST-BED-{i}',name=f'Cama {i}',type='bed',parent_id=greenhouse.id,status='active',length_m=10,width_m=2,capacity=20)
        fv.db.add(b);fv.db.flush()
        fv.db.add(m.BedCropProfile(bed_space_id=b.id,crop_id=crop.id,material_type='bulbo',input_mode='units',capacity_units=qty,density=Decimal(qty)/Decimal(20),active=True))
        fv.db.add(m.BedCropProfile(bed_space_id=b.id,crop_id=other.id,material_type='bulbo',input_mode='units',capacity_units=50,density=Decimal(2.5),active=True))
        beds.append(b)
    fv.db.commit()
    return crop,farm,greenhouse,beds


def test_multiple_beds_and_flowers_keep_correct_bulb_capacity(fv):
    crop,farm,gh,beds=make_area(fv)
    response=fv.client.get('/floresvolcan/planting/beds',params={'space_id':farm.id,'crop_id':crop.id})
    assert response.status_code==200,response.text
    assert {b['id'] for b in response.json()}=={b.id for b in beds}
    payload={'production_space_id':farm.id,'crop_id':crop.id,'bed_space_ids':[b.id for b in beds],'crew_id':fv.crew.id}
    result=fv.client.post('/floresvolcan/planting-orders',json=payload)
    assert result.status_code==201,result.text
    order_id=result.json()['id']
    current=fv.client.get(f'/floresvolcan/planting-orders/{order_id}')
    assert current.status_code==200,current.text
    assert current.json()['planned_quantity']==200
    assert len(current.json()['beds'])==2
    approved=fv.client.post(f'/floresvolcan/planting-orders/{order_id}/approve')
    assert approved.status_code==200,approved.text
    pdf=fv.client.get(f'/floresvolcan/planting-orders/{order_id}/paper-form')
    assert pdf.status_code==200 and pdf.content.startswith(b'%PDF'),pdf.text[:120]
    assert fv.client.post('/floresvolcan/requisitions',json={
        'planting_order_id':order_id,'lines':[{'inventory_lot_id':fv.lot.id,'quantity_requested':201}]
    }).status_code==409
    requested=fv.client.post('/floresvolcan/requisitions',json={
        'planting_order_id':order_id,'lines':[{'inventory_lot_id':fv.lot.id,'quantity_requested':160}]
    })
    assert requested.status_code==201,requested.text
    assert fv.client.post('/floresvolcan/requisitions',json={
        'planting_order_id':order_id,'lines':[{'inventory_lot_id':fv.lot.id,'quantity_requested':41}]
    }).status_code==409
    assert fv.client.post('/floresvolcan/requisitions',json={
        'planting_order_id':order_id,'lines':[{'inventory_lot_id':fv.lot.id,'quantity_requested':40}]
    }).status_code==201
    assert fv.db.query(m.RequisitionLine).join(m.Requisition).filter(m.Requisition.planting_order_id==order_id).count()==2
    # Cama ajena o sin perfil no es admisible.
    rejected=fv.client.post('/floresvolcan/planting-orders',json={**payload,'bed_space_ids':[fv.bed.id]})
    assert rejected.status_code==422,rejected.text


def test_approved_qr_requires_matching_worker_and_code(fv):
    crop,farm,gh,beds=make_area(fv)
    payload={'production_space_id':farm.id,'crop_id':crop.id,'bed_space_ids':[beds[0].id],'crew_id':fv.crew.id}
    result=fv.client.post('/floresvolcan/planting-orders',json=payload)
    assert result.status_code==201,result.text
    order_id=result.json()['id']
    assert fv.client.post(f'/floresvolcan/planting-orders/{order_id}/approve').status_code==200
    order=fv.db.get(m.PlantingOrder,order_id)
    worker=fv.db.query(m.Worker).filter_by(employee_code='TEST').first()
    valid={'order_number':order.number,'worker_id':worker.id,'employee_code':'TEST','qr_token':order.qr_token}
    good=fv.client.post('/floresvolcan/field/validate-planting-order',json=valid)
    assert good.status_code==200,good.text
    assert fv.client.post('/floresvolcan/field/validate-planting-order',json={**valid,'employee_code':'BAD'}).status_code==422
    assert fv.client.post('/floresvolcan/field/validate-planting-order',json={**valid,'qr_token':'FAKE'}).status_code==403


def test_worker_select_pagination_and_query(fv):
    for i in range(75):
        fv.db.add(m.Worker(employee_code=f'PAG-{i:03}',identification=f'PAG-ID-{i:03}',name=f'Persona {i:03}',active=True))
    fv.db.commit()
    response=fv.client.get('/floresvolcan/workers',params={'page':1,'page_size':20,'q':'Persona'})
    assert response.status_code==200,response.text
    assert response.json()['total']==75
    assert len(response.json()['items'])==20
    assert fv.client.get('/floresvolcan/workers',params={'page_size':101,'page':1}).status_code==422


def test_module_user_creation_does_not_grant_global_administrator(fv):
    from core.routers import admin as core_admin
    from core.routers.auth import get_current_user
    from core.services import core_permissions_for_user
    fv.client.app.include_router(core_admin.router)
    fv.db.add(AppModule(key='mod_floresvolcan',name='Flores',status='active',module_type='industry',manifest=MODULE_MANIFEST))
    fv.db.commit()
    payload={'nombre':'Empleado Nuevo','correo':'fv-new-worker@example.com','password':'PruebaSegura_2026!', 'role':'trabajador'}
    response=fv.client.post('/modules/mod_floresvolcan/users',json=payload)
    assert response.status_code==201,response.text
    assigned=fv.db.query(Usuario).filter_by(correo=payload['correo']).first()
    assert assigned.rol=='operario' and not assigned.is_superadmin
    assert role_for_module(fv.db,assigned,'mod_floresvolcan')=='trabajador'
    assert role_for_module(fv.db,assigned,'mod_caficultura') is None
    assert 'core:users:manage' not in core_permissions_for_user(assigned)
    assert 'floresvolcan:field:use' in module_permissions(fv.db,assigned,fv.db.query(AppModule).filter_by(key='mod_floresvolcan').one())
    # Gerente del módulo accede a personal del módulo, no a usuarios globales.
    fv.user.is_superadmin=False
    fv.user.rol='gerente'
    fv.db.add(ModuleUserRole(usuario_id=fv.user.id,module_key='mod_floresvolcan',role='gerente'))
    fv.db.commit()
    assert fv.client.get('/modules/mod_floresvolcan/users').status_code==200
    assert fv.client.get('/usuarios').status_code==403
    assert fv.client.patch('/modules/mod_floresvolcan/users/'+str(assigned.id),json={'role':'admin'}).status_code==200
    assert assigned.rol=='operario'
    assert fv.client.post('/modules/mod_floresvolcan/users',json={'nombre':'Malo','correo':'fv-bad@example.com','password':'PruebaSegura_2026!', 'role':'superadmin'}).status_code==403


def test_production_schema_upgrade_creates_module_membership_table():
    from schema_upgrade import CORE_TABLES
    assert ModuleUserRole.__table__ in CORE_TABLES
