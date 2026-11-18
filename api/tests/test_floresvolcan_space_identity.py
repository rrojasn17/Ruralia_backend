from test_floresvolcan_reconciliation_v07 import fv as fv_fixture, value
from modules.mod_floresvolcan import models as m

fv=fv_fixture


def test_space_codes_are_short_and_bed_code_is_generated(fv):
    def create(**body):return fv.client.post('/floresvolcan/production-spaces',json=body)
    value(create(code='ABCDEFG',name='Finca larga',type='farm'),422)
    value(create(code='A-1',name='Finca guion',type='farm'),422)
    farm=value(create(code='f01',name='Finca nueva',type='farm'),201)
    assert farm['code']=='F01'
    crop=fv.db.query(m.FlowerCrop).filter_by(code='LIRIO').first()
    bed={'parent_id':farm['id'],'name':'1a','type':'bed','length_m':10,'width_m':2,'profiles':[{'crop_id':crop.id,'capacity_units':100}]}
    result=value(create(**bed),201)
    assert result['name']=='1A' and result['code']=='F01-1A'
    value(create(**{**bed,'code':'IGNORED'}),409)
    value(create(**{**bed,'name':'12345'}),422)
    value(create(**{**bed,'name':'A-1'}),422)
    one=value(create(**{**bed,'name':'2'}),201)
    assert one['code']=='F01-2'
    house=value(create(code='inv017',name='Invernadero',type='greenhouse',parent_id=farm['id']),201)
    result=value(create(**{**bed,'parent_id':house['id'],'name':'12b'}),201)
    assert result['code']=='INV017-12B'
