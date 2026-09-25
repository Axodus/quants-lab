from pathlib import Path
import json, numpy as np, pyarrow.parquet as pq
ROOT=Path('/run/media/mzfshark/Storage/Axodus/Trading/market-data/normalized/canonical')

def arr(c):
 a=c.to_numpy(zero_copy_only=False)
 if a.dtype.kind in 'iu': return a.astype(np.int64,copy=False)
 o=np.full(len(a),-1,dtype=np.int64)
 for i,x in enumerate(a):
  if x is not None and not(isinstance(x,float) and np.isnan(x)): o[i]=int(x)
 return o

def scan(sym,start_hour,snapshot_id):
 files=[]
 for day,hours in [('2026-06-22',range(start_hour,24)),('2026-06-23',range(0,1))]:
  for h in hours:
   p=ROOT/day/f'{h:02d}'/f'{sym}_orderbook.parquet'
   if p.exists(): files.append(p)
 events=[]; carry=None; started=False
 for p in files:
  for b in pq.ParquetFile(p).iter_batches(columns=['event_type','event_time','transaction_time','first_update_id','final_update_id','prev_final_update_id','last_update_id'],batch_size=1_000_000):
   n=b.num_rows
   ty=b.column(0).to_numpy(zero_copy_only=False); et=arr(b.column(1)); tt=arr(b.column(2)); f=arr(b.column(3)); u=arr(b.column(4)); pu=arr(b.column(5)); lid=arr(b.column(6))
   if not n: continue
   same=(ty[1:]==ty[:-1])&(et[1:]==et[:-1])&(tt[1:]==tt[:-1])&(f[1:]==f[:-1])&(u[1:]==u[:-1])&(pu[1:]==pu[:-1])&(lid[1:]==lid[:-1])
   st=np.r_[0,np.flatnonzero(~same)+1]
   keys=list(zip(ty.tolist(),et.tolist(),tt.tolist(),f.tolist(),u.tolist(),pu.tolist(),lid.tolist()))
   if carry is not None and keys[0]==carry: st=st[1:]
   if not len(st): carry=keys[-1]; continue
   for j in st:
    typ=str(ty[j]); rec={'partition':f'{p.parts[-3]}/{p.parts[-2]}','eventTime':int(et[j]),'eventType':typ,'first':int(f[j]),'final':int(u[j]),'prev':int(pu[j]),'last':int(lid[j])}
    if typ=='snapshot' and rec['last']==snapshot_id:
     started=True
    if started: events.append(rec)
   carry=keys[-1]
 # locate anchor and check update chain
 anchor_idx=next((i for i,e in enumerate(events) if e['eventType']=='snapshot' and e['last']==snapshot_id),None)
 if anchor_idx is None: return {'symbol':sym,'anchorFound':False,'files':[str(x) for x in files]}
 seq=events[anchor_idx:]
 prev_final=snapshot_id; gaps=[]; first_in_window=None
 for e in seq[1:]:
  if e['eventType']=='snapshot':
   prev_final=e['last']; continue
  if e['partition']=='2026-06-23/00' and first_in_window is None: first_in_window=e
  if e['prev'] != prev_final:
   gaps.append({'partition':e['partition'],'eventTime':e['eventTime'],'prevFinal':e['prev'],'previousFinal':prev_final,'first':e['first'],'final':e['final']})
  prev_final=e['final']
 return {'symbol':sym,'anchorFound':True,'anchor':seq[0],'eventsFromAnchor':len(seq),'lastPreWindowEvent':next((e for e in reversed(seq) if e['partition']=='2026-06-22/23'),None),'firstInWindow':first_in_window,'gaps':gaps[:20],'gapCount':len(gaps)}

out=[scan('BTCUSDT',22,10869949249143),scan('ETHUSDC',14,10867286016586)]
print(json.dumps(out,indent=2))
