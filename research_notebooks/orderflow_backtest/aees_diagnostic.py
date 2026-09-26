"""Bounded real-event Decimal oracle. Writes only verifiable measurements."""
import json,hashlib,ctypes
from pathlib import Path
from decimal import Decimal
import numpy as np
import pyarrow.parquet as pq
from .parquet_orderflow_frames import FixedPointCausalParquetFrameBuilder,IS_START_MS
ROOT=Path('/run/media/mzfshark/Storage/Axodus/Trading/market-data')
T=1782175087235
FIELDS=['event_time','transaction_time','event_type','first_update_id','final_update_id','prev_final_update_id','last_update_id']
def main():
 b=FixedPointCausalParquetFrameBuilder(ROOT);lib=b._load_kernel();ptr=ctypes.POINTER(ctypes.c_int64)
 lib.ob_levels.argtypes=[ctypes.c_void_p,ctypes.c_int64,ptr];lib.ob_levels.restype=ctypes.c_int64
 h=lib.ob_new(IS_START_MS,T,5)
 paths=[b.canonical_root/'2026-06-22'/hour/'BTCUSDT_orderbook.parquet' for hour in ['22','23']]+[b.canonical_root/'2026-06-23/00/BTCUSDT_orderbook.parquet']
 def state():
  books=[]
  for side in [0,1]:
   n=lib.ob_levels(h,side,None);a=np.empty((n,2),dtype=np.int64);lib.ob_levels(h,side,a.ctypes.data_as(ptr));books.append({Decimal(int(p))/10:Decimal(int(q))/1000 for p,q in a})
  return books
 try:
  for path in paths:
   for batch in pq.ParquetFile(path).iter_batches(batch_size=250000):
    a=b._kernel_rows(batch); a=np.ascontiguousarray(a[a[:,0]<T]);
    if len(a):assert lib.ob_push(h,a.ctypes.data_as(ptr),len(a))==0
  assert lib.ob_finish(h,0)==0
  before=state();ref=[dict(x) for x in before]
  table=pq.read_table(paths[-1],filters=[('event_time','=',T)])
  rows=table.to_pylist();keys={tuple(r[k] for k in FIELDS) for r in rows}
  changes=[]
  for r in rows:
   side=0 if r['side']=='bid' else 1;p=Decimal(r['price']);q=Decimal(r['quantity']);old=ref[side].get(p,Decimal(0))
   if q:ref[side][p]=q
   else:ref[side].pop(p,None)
   changes.append({'side':r['side'],'price':str(p),'previousQuantity':str(old),'incomingQuantity':str(q),'resultingQuantity':str(ref[side].get(p,Decimal(0)))})
  a=b._kernel_rows(table);assert lib.ob_push(h,a.ctypes.data_as(ptr),len(a))==0;assert lib.ob_finish(h,0)==0
  actual=state();assert actual==ref
  partial=[dict(x) for x in before]
  for r in rows[:71]:
   side=0 if r['side']=='bid' else 1;p=Decimal(r['price']);q=Decimal(r['quantity'])
   if q:partial[side][p]=q
   else:partial[side].pop(p,None)
  def best(x):return {'bid':str(max(x[0])),'ask':str(min(x[1]))}
  result={'timestamp':T,'rawRows':len(rows),'canonicalEvents':len(keys),'identity':dict(zip(FIELDS,next(iter(keys)))),'previous71Classification':'INCOMPLETE_EVENT','bidRows':sum(r['side']=='bid' for r in rows),'askRows':sum(r['side']=='ask' for r in rows),'before':best(before),'after71Rows':best(partial),'afterCompleteEvent':best(actual),'decimalFullBookEquality':True,'levelChanges':changes,'normalizedFileSha256':hashlib.file_digest(paths[-1].open('rb'),'sha256').hexdigest(),'providerBookProvenCrossed':False}
  out=ROOT/'tmp/aees-closure/event-oracle.json';out.write_text(json.dumps(result,indent=2));print(json.dumps({k:v for k,v in result.items() if k!='levelChanges'}),flush=True)
 finally:lib.ob_free(h)
if __name__=='__main__':main()
