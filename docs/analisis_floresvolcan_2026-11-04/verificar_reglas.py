"""Executes selected real router functions with an in-memory query stub; no app/DB imports."""
from __future__ import annotations
import ast,json
from pathlib import Path
from decimal import Decimal
from types import SimpleNamespace as N
from datetime import datetime,timezone,date,timedelta
source=Path(__file__).resolve().parents[2] / 'api/modules/mod_floresvolcan/router.py'
if 'from modules.mod_floresvolcan.production_control import' in source.read_text():
    print('Este diagnóstico corresponde al código anterior a 0.7.0. Las reglas fueron reemplazadas; ejecute api/tests/test_floresvolcan_reconciliation_v07.py para verificar la versión actual.')
    raise SystemExit(0)
tree=ast.parse(source.read_text())
wanted={'_decimal','_obj','_batch_metrics','technical_close','_harvest_eta'}
selected=[]
for node in tree.body:
 if isinstance(node,ast.FunctionDef) and node.name in wanted:
  node.decorator_list=[]
  for arg in node.args.args:arg.annotation=None
  node.returns=None;node.args.defaults=[]
  selected.append(node)
class Expr:
 def __init__(self,name):self.name=name
 def __eq__(self,other):return self
 def in_(self,other):return self
class ModelMeta(type):
 def __getattr__(cls,name):return Expr(name)
class PlantingRecord(metaclass=ModelMeta):pass
class HarvestRecord(metaclass=ModelMeta):pass
class ProductionBatch(metaclass=ModelMeta):pass
class HarvestOrder(metaclass=ModelMeta):pass
class HTTPException(Exception):pass
class Query:
 def __init__(self,db,expr):self.db=db;self.expr=expr
 def filter(self,*a):return self
 def with_for_update(self):return self
 def first(self):return self.db.batch
 def count(self):return 0
 def scalar(self):return self.db.values.get(self.expr.name,0)
class DB:
 def __init__(self,batch,values):self.batch=batch;self.values=values;self.added=[]
 def query(self,expr):return Query(self,expr)
 def add(self,value):self.added.append(value)
 def commit(self):pass
 def refresh(self,value):value.id=1;value.closed_at=datetime.now(timezone.utc)
func=N(sum=lambda v:v,coalesce=lambda v,*_:v,min=lambda v:v)
ns=dict(globals(),now=lambda:datetime.now(timezone.utc),TechnicalClosure=lambda **kw:N(**kw))
exec(compile(ast.fix_missing_locations(ast.Module(body=selected,type_ignores=[])),str(source),'exec'),ns)
batch=N(id=1,status='open',expected_stems_per_bulb=Decimal(1))
db=DB(batch,{'quantity_planted':Decimal(100)})
r=ns['technical_close'](1,N(comments='Cierre de prueba'),N(id=1),db)
assert batch.status=='closed' and r['difference']==100 and r['total_harvested']==0
print('CONFIRMED_CLOSE_WITHOUT_OUTPUT',json.dumps({k:str(v) for k,v in r.items()}))
batch=N(id=2,status='open',expected_stems_per_bulb=Decimal(2))
db=DB(batch,{'quantity_planted':Decimal(100),'quantity_harvested':Decimal(150),'total_output':Decimal(150)})
r=ns['_batch_metrics'](db,batch)
assert r['difference']==-50 and r['yield_percentage']==150
print('CONFIRMED_FACTOR_METRICS',json.dumps({k:str(v) for k,v in r.items()}))
print('EXPECTED_TECHNICAL_PENDING',100*2-150)
