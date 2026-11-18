"""Mantenimiento solicitado: acrónimos de seis caracteres y prefijos de camas.

Por defecto solo revisa el plan. Usar --apply para aplicar en una transacción.
"""
import json
import re
import sys
from collections import Counter
from sqlalchemy import text
from database import engine

ACRONYMS = {
    'Lirios': 'LIRIOS', 'BG2': 'INVBG2', '7V': 'INV07V', 'Laura': 'LAUR01',
    '3C': 'INV03C', '9V': 'INV09V', 'CATALINAS': 'CATALI', 'BG1': 'INVBG1',
    '1C': 'INV01C', '2C': 'INV02C', '7C': 'INV07C', '8V': 'INV08V',
    'VICTORIA A': 'VICTOA', 'VICTORIA B': 'VICTOB', '4C': 'INV04C',
    '6V': 'INV06V', '5C': 'INV05C', '6C': 'INV06C', 'B2': 'INV0B2',
    'B1': 'INV0B1', 'FINCA 3': 'FINCA3', 'InvernaderoElSol': 'ELSOL1',
}
MARKER='maintenance.space_acronyms.20261105'


def main(apply=False):
    with engine.begin() as db:
        db.execute(text("SET LOCAL lock_timeout='5s'"))
        db.execute(text('LOCK TABLE fv_production_spaces IN SHARE ROW EXCLUSIVE MODE'))
        if db.execute(text('SELECT 1 FROM fv_module_settings WHERE key=:key'),{'key':MARKER}).scalar():
            print(json.dumps({'already_applied':True}));return
        rows=[dict(r) for r in db.execute(text('SELECT id,code,name,type,parent_id FROM fv_production_spaces ORDER BY id')).mappings()]
        parents=[r for r in rows if r['type'] in {'farm','greenhouse'}]
        if len(parents)!=len(ACRONYMS) or {p['name'] for p in parents}!=set(ACRONYMS):
            raise ValueError('Los espacios cambiaron: revisar el plan antes de aplicar')
        codes={p['id']:ACRONYMS[p['name']] for p in parents}
        assert all(re.fullmatch(r'[A-Z0-9]{6}',v) for v in codes.values())
        lookup={p['id']:p for p in parents}
        for row in rows:
            if row['type'] not in {'bed','row','table'} or row['parent_id'] not in lookup:continue
            parent=lookup[row['parent_id']]
            prefix=parent['code']+'-'
            if not row['code'].casefold().startswith(prefix.casefold()):
                raise ValueError(f"Prefijo inesperado en cama {row['id']}; revisar antes de aplicar")
            codes[row['id']]=codes[parent['id']]+'-'+row['code'][len(prefix):]
        final={r['id']:codes.get(r['id'],r['code']) for r in rows}
        if any(n>1 for n in Counter(c.upper() for c in final.values()).values()):
            raise ValueError('El plan genera códigos repetidos')
        changes=[{**r,'new_code':final[r['id']]} for r in rows if final[r['id']]!=r['code']]
        report={'parent_codes':[{p['name']:codes[p['id']]} for p in parents],
                'parents_changed':sum(r['type'] in {'farm','greenhouse'} for r in changes),
                'beds_changed':sum(r['type'] in {'bed','row','table'} for r in changes)}
        if apply:
            db.execute(text('INSERT INTO fv_module_settings(key,value_text,is_secret,updated_at) VALUES(:key,:value,false,CURRENT_TIMESTAMP)'),
                       {'key':MARKER,'value':json.dumps({'reason':'Usuario solicita acrónimos de seis caracteres desde nombres originales','changes':changes},ensure_ascii=False)})
            # Dos fases evitan colisiones transitorias con códigos originales.
            for r in changes:db.execute(text('UPDATE fv_production_spaces SET code=:code WHERE id=:id'),{'id':r['id'],'code':f"FVRENAME20261105_{r['id']}"})
            for r in changes:db.execute(text('UPDATE fv_production_spaces SET code=:code WHERE id=:id'),{'id':r['id'],'code':r['new_code']})
        print(json.dumps({'applied':apply,**report},ensure_ascii=False))


if __name__=='__main__':main('--apply' in sys.argv)
