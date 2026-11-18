"""Completa solamente NULL con referencias técnicas auditadas. Nunca al arrancar."""
import json
import sys
from collections import Counter
from sqlalchemy import text, or_
import core.models
from database import SessionLocal
from modules.mod_floresvolcan.models import SapProduct, FlowerCrop, ModuleSetting, BusinessStatus
from modules.mod_floresvolcan.technical_materials import reference_for


def main(apply=False):
    with SessionLocal() as db:
        db.execute(text("SET LOCAL lock_timeout='5s'"))
        db.execute(text('LOCK TABLE fv_sap_products IN SHARE ROW EXCLUSIVE MODE'))
        rows=db.query(SapProduct).join(FlowerCrop,FlowerCrop.id==SapProduct.crop_id).filter(
            FlowerCrop.code=='LIRIO',or_(SapProduct.default_density.is_(None),SapProduct.default_cycle_weeks.is_(None))).all()
        counts=Counter();changes=[];pending=[]
        for row in rows:
            result=reference_for(row)
            if result['updates']:
                changes.append({'id':row.id,'code':row.code,'variety':row.variety,'caliber':row.caliber,
                    'before':{'default_density':row.default_density,'default_cycle_weeks':row.default_cycle_weeks},**result})
                counts.update(result['updates'].keys())
            density=result['updates'].get('default_density',row.default_density)
            cycle=result['updates'].get('default_cycle_weeks',row.default_cycle_weeks)
            if density is None or cycle is None:
                pending.append({'code':row.code,'variety':row.variety,'caliber':row.caliber,'reason':result['pending']})
            if apply:
                for field,value in result['updates'].items():setattr(row,field,value)
                name='technical_material.'+str(row.id)
                ref=db.query(ModuleSetting).filter_by(key=name).first()
                if not ref:ref=ModuleSetting(key=name);db.add(ref)
                ref.value_text=json.dumps(result,ensure_ascii=False,default=str)
        report={'affected_products':len(changes),'filled_fields':dict(counts),'remaining_incomplete':len(pending),'pending':pending}
        if apply:
            marker='maintenance.technical_material_fill.20261105'
            if db.query(ModuleSetting).filter_by(key=marker).first():raise ValueError('Carga ya aplicada; revisar antes de repetir')
            db.add(ModuleSetting(key=marker,value_text=json.dumps({'sources_checked_on':'2026-11-05','changes':changes,'report':report},ensure_ascii=False,default=str)))
            db.query(BusinessStatus).delete(synchronize_session=False)
            db.commit()
        else:db.rollback()
        print(json.dumps({'applied':apply,**report},ensure_ascii=False))


if __name__=='__main__':main('--apply' in sys.argv)
