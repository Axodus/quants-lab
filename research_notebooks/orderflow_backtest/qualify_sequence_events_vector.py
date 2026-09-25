import json,sys,time
from pathlib import Path
import numpy as np
import pyarrow.parquet as pq
ROOT=Path('/run/media/mzfshark/Storage/Axodus/Trading/market-data/normalized/canonical')
OUT=Path('/opt/Axodus/Trading/.instructions/reports/AXODUS-TRADING-REQ-QUANT-HIST-DATA-02-SEQUENCE-LEDGER.json')
COLS=['event_type','event_time','transaction_time','first_update_id','final_update_id','prev_final_update_id','last_update_id']
def ia(c):
 a=c.to_numpy(zero_copy_only=False)
 if a.dtype.kind in 'iu': return a.astype('int64',copy=False)
 o=np.full(len(a),-1,dtype='int64')
 for i,x in enumerate(a):
  if x is not None and not(isinstance(x,float) and np.isnan(x)): o[i]=int(x)
 return o
def classify(sym):
 ps=sorted(p for p in ROOT.glob(f'*/**/{sym}_orderbook.parquet') if '2026-06-23'<=p.parts[-3]<='2026-07-06')
 m={'symbol':sym,'files':len(ps),'rows':0,'logicalEvents':0,'updateEvents':0,'snapshotRows':0,'snapshotEvents':0,'continuousTransitions':0,'snapshotBootstraps':0,'snapshotRebootstraps':0,'sequenceResets':0,'gaps':0,'duplicates':0,'outOfOrder':0,'qualifiedSegments':0,'qualifiedEventCount':0,'invalidEventCount':0,'anomalies':[]}
 prev=None; prev_final=None; prev_type=None; carry=None; seen=set()
 for fi,p in enumerate(ps,1):
  part=f'{p.parts[-3]}/{p.parts[-2]}'; day=p.parts[-3]
  for b in pq.ParquetFile(p).iter_batches(columns=COLS,batch_size=1000000):
   n=b.num_rows;m['rows']+=n
   ty=b.column(0).to_numpy(zero_copy_only=False); et=ia(b.column(1)); tt=ia(b.column(2)); f=ia(b.column(3)); u=ia(b.column(4)); pu=ia(b.column(5)); lid=ia(b.column(6))
   m['snapshotRows']+=int((ty=='snapshot').sum())
   if not n: continue
   same=(ty[1:]==ty[:-1])&(et[1:]==et[:-1])&(tt[1:]==tt[:-1])&(f[1:]==f[:-1])&(u[1:]==u[:-1])&(pu[1:]==pu[:-1])&(lid[1:]==lid[:-1])
   st=np.r_[0,np.flatnonzero(~same)+1]
   if carry is not None and (ty[0],int(et[0]),int(tt[0]),int(f[0]),int(u[0]),int(pu[0]),int(lid[0]))==carry: st=st[1:]
   if not len(st): carry=(ty[-1],int(et[-1]),int(tt[-1]),int(f[-1]),int(u[-1]),int(pu[-1]),int(lid[-1]));continue
   gt,ge,gf,gu,gpu,gl=ty[st],et[st],f[st],u[st],pu[st],lid[st]; k=len(st);m['logicalEvents']+=k;m['snapshotEvents']+=int((gt=='snapshot').sum());m['updateEvents']+=int((gt!='snapshot').sum())
   snap=gt=='snapshot'; eff=np.where(snap,gl,gu); prior=np.r_[(-1 if prev_final is None else prev_final),eff[:-1]]; pty=np.concatenate(([prev_type],gt[:-1])) if k else np.array([],dtype=object)
   valid=(~snap)&(gf>=0)&(gu>=0)&(gpu>=0); boot=valid&(pty=='snapshot')&(gf<=prior+1)&(prior+1<=gu); cont=valid&(pty!='snapshot')&(gpu==prior); oo=valid&(gu<prior)&~boot; dup=valid&(gu==prior)&~boot; gap=valid&~(boot|cont|oo|dup); amb=(~snap)&~valid
   m['continuousTransitions']+=int((boot|cont).sum());m['qualifiedEventCount']+=int((boot|cont).sum());m['outOfOrder']+=int(oo.sum());m['duplicates']+=int(dup.sum());m['gaps']+=int(gap.sum());m['invalidEventCount']+=int((oo|gap|amb).sum())
   for j in np.flatnonzero(snap):
    if gl[j]>=0:
     if gl[j] in seen: m['duplicates']+=1
     else:
      seen.add(int(gl[j]));m['snapshotBootstraps']+=int(prev_final is None);m['snapshotRebootstraps']+=int(prev_final is not None);m['qualifiedSegments']+=1
   bad=oo|gap|dup|amb
   for j in np.flatnonzero(bad):
    typ='TRUE_OUT_OF_ORDER' if oo[j] else 'TRUE_SEQUENCE_GAP' if gap[j] else 'PARSER_GROUPING_ERROR' if dup[j] else 'AMBIGUOUS'
    evtyp=str(gt[j]); eid=[evtyp,int(ge[j]),int(gl[j] if evtyp=='snapshot' else gf[j]),None if evtyp=='snapshot' else int(gu[j]),None if evtyp=='snapshot' else int(gpu[j])]
    m['anomalies'].append({'symbol':sym,'partition':part,'timestamp':None if ge[j]<0 else int(ge[j]),'eventIdentity':eid,'previousFinalUpdateId':None if prev is None else prev.get('finalUpdateId'),'currentFirstUpdateId':None if gf[j]<0 else int(gf[j]),'currentPrevFinalUpdateId':None if gpu[j]<0 else int(gpu[j]),'currentFinalUpdateId':None if gu[j]<0 else int(gu[j]),'previousEventType':None if prev is None else prev.get('eventType'),'currentEventType':evtyp,'partitionBoundary':prev is not None and prev['partition']!=part,'dayBoundary':prev is not None and prev['partition'].split('/')[0]!=day,'snapshotBoundary':evtyp=='snapshot' or (prev is not None and prev.get('eventType')=='snapshot'),'classification':typ,'evidence':'logical event transition evaluated after price-level grouping'})
   j=k-1; prev={'partition':part,'eventType':str(gt[j]),'finalUpdateId':None if gu[j]<0 else int(gu[j])};prev_final=int(eff[j]) if eff[j]>=0 else prev_final;prev_type=str(gt[j]);carry=(ty[-1],int(et[-1]),int(tt[-1]),int(f[-1]),int(u[-1]),int(pu[-1]),int(lid[-1]))
  print(f'{sym}: file {fi}/{len(ps)} events={m["logicalEvents"]} anomalies={len(m["anomalies"])}',file=sys.stderr,flush=True)
 m['anomalyCount']=len(m['anomalies']);return m
r={'datasetId':'orderflow-binance-futures-14d-20260623-20260706-v1','window':{'start':'2026-06-23T00:00:00Z','end':'2026-07-06T23:59:59.999Z'},'generatedAt':time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime()),'symbols':[classify(s) for s in ('BTCUSDT','ETHUSDC')]};OUT.parent.mkdir(parents=True,exist_ok=True);OUT.write_text(json.dumps(r,indent=2));print(json.dumps(r,indent=2))
