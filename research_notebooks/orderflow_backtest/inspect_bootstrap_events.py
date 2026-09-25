from pathlib import Path
import numpy as np, pyarrow.parquet as pq, json
ROOT=Path('/run/media/mzfshark/Storage/Axodus/Trading/market-data/normalized/canonical')
COLS=['event_type','event_time','transaction_time','first_update_id','final_update_id','prev_final_update_id','last_update_id']
def ia(c):
 a=c.to_numpy(zero_copy_only=False)
 if a.dtype.kind in 'iu': return a.astype(np.int64,copy=False)
 o=np.full(len(a),-1,dtype=np.int64)
 for i,x in enumerate(a):
  if x is not None and not(isinstance(x,float) and np.isnan(x)): o[i]=int(x)
 return o
def inspect(sym, day, hour):
 p=ROOT/day/f'{hour:02d}'/f'{sym}_orderbook.parquet'; events=[]; carry=None
 for rg,b in enumerate(pq.ParquetFile(p).iter_batches(columns=COLS,batch_size=1_000_000)):
  n=b.num_rows; ty=b.column(0).to_numpy(zero_copy_only=False); et=ia(b.column(1)); tt=ia(b.column(2)); f=ia(b.column(3)); u=ia(b.column(4)); pu=ia(b.column(5)); lid=ia(b.column(6))
  if not n: continue
  same=(ty[1:]==ty[:-1])&(et[1:]==et[:-1])&(tt[1:]==tt[:-1])&(f[1:]==f[:-1])&(u[1:]==u[:-1])&(pu[1:]==pu[:-1])&(lid[1:]==lid[:-1])
  st=np.r_[0,np.flatnonzero(~same)+1]
  keys=list(zip(ty.tolist(),et.tolist(),tt.tolist(),f.tolist(),u.tolist(),pu.tolist(),lid.tolist()))
  if carry is not None and keys[0]==carry: st=st[1:]
  for j in st:
   events.append({'rowGroup':rg,'type':str(ty[j]),'eventTime':int(et[j]),'first':int(f[j]),'final':int(u[j]),'prev':int(pu[j]),'last':int(lid[j])})
  carry=keys[-1]
 idx=next((i for i,e in enumerate(events) if e['type']=='snapshot'),None)
 return {'path':str(p),'eventCount':len(events),'snapshotIndex':idx,'eventsAroundSnapshot':events[max(0,(idx or 0)-2):(idx or 0)+5] if idx is not None else events[:5]}
print(json.dumps([inspect('BTCUSDT','2026-06-22',22),inspect('ETHUSDC','2026-06-22',14)],indent=2))
