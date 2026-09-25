"""Vectorized logical-event sequence classification for CryptoHFTData L2."""
from __future__ import annotations
import json, sys, time
from pathlib import Path
import numpy as np
import pyarrow.parquet as pq

ROOT=Path('/run/media/mzfshark/Storage/Axodus/Trading/market-data/normalized/canonical')
OUT=Path('/opt/Axodus/Trading/.instructions/reports/AXODUS-TRADING-REQ-QUANT-HIST-DATA-02-SEQUENCE-LEDGER.json')
START='2026-06-23'; END='2026-07-06'; BATCH=1_000_000
COLS=['event_type','event_time','transaction_time','first_update_id','final_update_id','prev_final_update_id','last_update_id']

def arr(col, fill=-1):
    a=col.to_numpy(zero_copy_only=False)
    if a.dtype.kind in 'iu': return a.astype(np.int64, copy=False)
    out=np.full(len(a),fill,dtype=np.int64)
    for i,x in enumerate(a):
        if x is not None and not (isinstance(x, float) and np.isnan(x)): out[i]=int(x)
    return out

def textarr(col): return col.to_numpy(zero_copy_only=False)

def classify(symbol):
    paths=sorted(ROOT.glob(f'*/**/{symbol}_orderbook.parquet'))
    paths=[p for p in paths if START<=p.parts[-3]<=END]
    m={'symbol':symbol,'files':len(paths),'rows':0,'logicalEvents':0,'updateEvents':0,'snapshotRows':0,'snapshotEvents':0,'continuousTransitions':0,'snapshotBootstraps':0,'snapshotRebootstraps':0,'sequenceResets':0,'gaps':0,'duplicates':0,'outOfOrder':0,'qualifiedSegments':0,'qualifiedEventCount':0,'invalidEventCount':0,'anomalies':[],'segments':[]}
    last_final=None; prev=None; seg_start=None; seg_count=0; seg_qualified=False; seg_invalid=False; seen=set(); carry_key=None
    def finish(reason):
        nonlocal seg_start,seg_count,seg_qualified,seg_invalid
        if seg_start is not None:
            m['segments'].append({'start':seg_start['eventTime'],'startPartition':seg_start['partition'],'end':prev['eventTime'] if prev else None,'endPartition':prev['partition'] if prev else None,'eventCount':seg_count,'qualified':bool(seg_qualified and not seg_invalid),'endReason':reason})
        seg_start=None; seg_count=0; seg_qualified=False; seg_invalid=False
    def anomaly(cls, ev, evidence, boundary=False, day=False, snap=False):
        m['anomalies'].append({'symbol':symbol,'partition':ev['partition'],'timestamp':ev['eventTime'],'eventIdentity':ev['eventIdentity'],'previousFinalUpdateId':None if prev is None else prev.get('finalUpdateId'),'currentFirstUpdateId':ev.get('firstUpdateId'),'currentPrevFinalUpdateId':ev.get('prevFinalUpdateId'),'currentFinalUpdateId':ev.get('finalUpdateId'),'previousEventType':None if prev is None else prev.get('eventType'),'currentEventType':ev['eventType'],'partitionBoundary':boundary,'dayBoundary':day,'snapshotBoundary':snap,'classification':cls,'evidence':evidence})
    for fi,p in enumerate(paths,1):
        part=f'{p.parts[-3]}/{p.parts[-2]}'; day=p.parts[-3]; pf=pq.ParquetFile(p)
        for rb in pf.iter_batches(columns=COLS,batch_size=BATCH):
            n=rb.num_rows; m['rows']+=n
            typ=textarr(rb.column(0)); et=arr(rb.column(1)); tt=arr(rb.column(2)); first=arr(rb.column(3)); final=arr(rb.column(4)); pfinal=arr(rb.column(5)); lastid=arr(rb.column(6))
            m['snapshotRows']+=int(np.count_nonzero(typ=='snapshot'))
            if not n: continue
            same_prev=(typ[1:]==typ[:-1])&(et[1:]==et[:-1])&(tt[1:]==tt[:-1])&(first[1:]==first[:-1])&(final[1:]==final[:-1])&(pfinal[1:]==pfinal[:-1])&(lastid[1:]==lastid[:-1])
            starts=np.r_[0,np.flatnonzero(~same_prev)+1]
            if carry_key is not None:
                k0=(typ[0],int(et[0]),int(tt[0]),int(first[0]),int(final[0]),int(pfinal[0]),int(lastid[0]))
                if k0==carry_key: starts=starts[1:]
            if starts.size==0:
                carry_key=(typ[-1],int(et[-1]),int(tt[-1]),int(first[-1]),int(final[-1]),int(pfinal[-1]),int(lastid[-1])); continue
            gtyp=typ[starts]; get=et[starts]; gtt=tt[starts]; gf=first[starts]; gu=final[starts]; gp=pfinal[starts]; gl=lastid[starts]
            m['logicalEvents']+=len(starts); m['snapshotEvents']+=int(np.count_nonzero(gtyp=='snapshot')); m['updateEvents']+=int(np.count_nonzero(gtyp!='snapshot'))
            # Process only event transitions; 70m events still use compact Python loop, but no row-level loop.
            for j in range(len(starts)):
                typj=str(gtyp[j]); ev={'partition':part,'eventTime':None if get[j]<0 else int(get[j]),'eventType':typj,'eventIdentity':[typj,int(get[j]),int(gtt[j]),int(gl[j]) if typj=='snapshot' else int(gf[j]),int(gu[j]) if typj!='snapshot' else None,int(gp[j]) if typj!='snapshot' else None],'firstUpdateId':None if gf[j]<0 else int(gf[j]),'finalUpdateId':None if gu[j]<0 else int(gu[j]),'prevFinalUpdateId':None if gp[j]<0 else int(gp[j]),'lastUpdateId':None if gl[j]<0 else int(gl[j])}
                boundary=prev is not None and prev['partition']!=part; dayb=prev is not None and prev['partition'].split('/')[0]!=day; snapb=typj=='snapshot' or (prev is not None and prev['eventType']=='snapshot')
                if typj=='snapshot':
                    sid=ev['lastUpdateId']
                    if sid is None: m['invalidEventCount']+=1; finish('INVALID_SNAPSHOT')
                    elif sid in seen: m['duplicates']+=1; anomaly('AMBIGUOUS',ev,'duplicate snapshot identity',boundary,dayb,True)
                    else:
                        seen.add(sid)
                        if last_final is None: m['snapshotBootstraps']+=1
                        else: m['snapshotRebootstraps']+=1; finish('VALID_SNAPSHOT_REBOOTSTRAP')
                        m['qualifiedSegments']+=1; last_final=sid; seg_start=ev; seg_qualified=True; seg_invalid=False; seg_count=1
                    prev=ev; continue
                f,u,pu=ev['firstUpdateId'],ev['finalUpdateId'],ev['prevFinalUpdateId']
                if f is None or u is None or pu is None: m['invalidEventCount']+=1; prev=ev; continue
                if seg_start is None: seg_start=ev; seg_qualified=last_final is not None
                if last_final is None: m['invalidEventCount']+=1; anomaly('AMBIGUOUS',ev,'update before valid snapshot',boundary,dayb,snapb)
                elif prev is not None and prev['eventType']=='snapshot' and f<=last_final+1<=u: m['continuousTransitions']+=1; m['qualifiedEventCount']+=1
                elif u<last_final: m['outOfOrder']+=1; anomaly('TRUE_OUT_OF_ORDER',ev,'logical final_update_id decreased',boundary,dayb,snapb); finish('TRUE_OUT_OF_ORDER'); seg_start=ev; seg_qualified=False; seg_invalid=True; m['invalidEventCount']+=1
                elif u==last_final: m['duplicates']+=1; anomaly('AMBIGUOUS',ev,'logical final_update_id repeated after grouping',boundary,dayb,snapb)
                elif pu==last_final: m['continuousTransitions']+=1; m['qualifiedEventCount']+=1; seg_qualified=True
                else: m['gaps']+=1; anomaly('TRUE_SEQUENCE_GAP',ev,'prev_final_update_id does not equal previous logical final_update_id',boundary,dayb,snapb); finish('TRUE_SEQUENCE_GAP'); seg_start=ev; seg_qualified=False; seg_invalid=True; m['invalidEventCount']+=1
                last_final=u; seg_count+=1; prev=ev
            carry_key=(typ[-1],int(et[-1]),int(tt[-1]),int(first[-1]),int(final[-1]),int(pfinal[-1]),int(lastid[-1]))
        print(f'{symbol}: file {fi}/{len(paths)} events={m["logicalEvents"]} anomalies={len(m["anomalies"])}',file=sys.stderr,flush=True)
    finish('END_OF_WINDOW'); m['anomalyCount']=len(m['anomalies']); return m

def main():
    t=time.time(); out={'datasetId':'orderflow-binance-futures-14d-20260623-20260706-v1','window':{'start':'2026-06-23T00:00:00Z','end':'2026-07-06T23:59:59.999Z'},'generatedAt':time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime()),'symbols':[classify(s) for s in ('BTCUSDT','ETHUSDC')],'durationSeconds':round(time.time()-t,3)}; OUT.parent.mkdir(parents=True,exist_ok=True); OUT.write_text(json.dumps(out,indent=2)); print(json.dumps(out,indent=2))
if __name__=='__main__': main()
