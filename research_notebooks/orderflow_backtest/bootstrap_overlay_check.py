from pathlib import Path
import json, pyarrow.parquet as pq, numpy as np
ROOT=Path('/run/media/mzfshark/Storage/Axodus/Trading/market-data/normalized/canonical')
def arr(c):
 a=c.to_numpy(zero_copy_only=False)
 if a.dtype.kind in 'iu': return a.astype(np.int64,copy=False)
 o=np.full(len(a),-1,dtype=np.int64)
 for i,x in enumerate(a):
  if x is not None and not(isinstance(x,float) and np.isnan(x)): o[i]=int(x)
 return o
def verify(sym,day,hour,snap_id):
 p=ROOT/day/f'{hour:02d}'/f'{sym}_orderbook.parquet'
 b=pq.ParquetFile(p).read_row_group(0,columns=['event_type','event_time','first_update_id','final_update_id','prev_final_update_id','last_update_id'])
 ty=b.column(0).to_numpy(zero_copy_only=False); f=arr(b.column(2)); u=arr(b.column(3)); pu=arr(b.column(4)); lid=arr(b.column(5))
 idx=np.flatnonzero(ty=='snapshot')
 if not len(idx): return 'no snapshot'
 s_idx=idx[0]; snap_last=int(lid[s_idx])
 # find first update event after snapshot
 u_idx=np.flatnonzero((ty!='snapshot') & (np.arange(len(ty))>s_idx))
 if not len(u_idx): return 'no update after snapshot'
 first_u=u_idx[0]
 return {'sym':sym,'snap_last':snap_last,'first_update':{'first':int(f[first_u]),'final':int(u[first_u]),'prev':int(pu[first_u])},'overlap_valid':bool(int(f[first_u])<=snap_last+1<=int(u[first_u]))}
print(json.dumps([verify('BTCUSDT','2026-06-22',22,10869949249143),verify('ETHUSDC','2026-06-22',14,10867286016586)],indent=2))
