"""Independent Decimal oracle and event-atomicity regression tests."""
import ctypes
from decimal import Decimal
from pathlib import Path
import numpy as np
import pytest
from orderflow_backtest.parquet_orderflow_frames import FixedPointCausalParquetFrameBuilder
ROOT=Path('/run/media/mzfshark/Storage/Axodus/Trading/market-data')

def run(rows,chunks=(1,),end=59999):
    b=FixedPointCausalParquetFrameBuilder(ROOT,is_start_ms=0,is_end_ms=end)
    lib=b._load_kernel(); p=lib.ob_new(0,end,5)
    try:
        a=np.asarray(rows,dtype=np.int64); offset=0
        for n in chunks+(len(a),):
            part=np.ascontiguousarray(a[offset:offset+n]);offset+=len(part)
            if len(part):
                e=lib.ob_push(p,part.ctypes.data_as(ctypes.POINTER(ctypes.c_int64)),len(part))
                if e:return e,None
        e=lib.ob_finish(p,1)
        count=lib.ob_frames(p,None);f=np.zeros((count,7),dtype=np.int64)
        lib.ob_frames(p,f.ctypes.data_as(ctypes.POINTER(ctypes.c_int64)))
        return e,f
    finally:lib.ob_free(p)

def snap(t,p,q,side,seq=10):return [t,t,1,0,0,0,seq,side,p,q]
def upd(t,p,q,side,seq=11,prev=10):return [t,t,0,seq,seq,prev,0,side,p,q]

def test_event_atomicity_and_delete():
    rows=[snap(0,100,10,0),snap(0,101,20,1),upd(1,99,20,1),upd(1,100,0,0),upd(1,98,10,0)]
    for chunks in [(1,1,1,1),(2,3),(4,)]:
        e,f=run(rows,chunks);assert e==0;assert tuple(f[0,1:5])==(98,99,10,40)

def test_snapshot_replaces_stale_levels():
    e,f=run([snap(0,100,10,0),snap(0,101,10,1),snap(1,90,4,0,12),snap(1,91,6,1,12)])
    assert e==0;assert tuple(f[0,1:5])==(90,91,4,6)

def test_sequence_rejection():
    e,_=run([snap(0,100,1,0),snap(0,101,1,1),upd(1,100,2,0,15,14)])
    assert e==1

def test_oos_rejection():
    e,_=run([snap(0,100,1,0),snap(0,101,1,1),upd(60000,100,2,0)])
    assert e==4

def test_crossed_complete_event_rejected():
    e,_=run([snap(0,100,1,0),snap(0,101,1,1),upd(1,99,2,1)])
    assert e==6

def test_decimal_oracle_and_batch_determinism():
    rows=[snap(0,100,10,0),snap(0,101,10,1)]
    for j in range(1,50):rows.append(upd(j,100,j,0,10+j,9+j))
    books=[{},{}]
    last=None
    for r in rows:
        key=tuple(r[:7])
        if r[2] and key!=last:books=[{},{}]
        p=Decimal(int(r[8]))/10;q=Decimal(int(r[9]))/1000
        if q:books[r[7]][p]=q
        else:books[r[7]].pop(p,None)
        last=key
    e,f=run(rows,(1,3,7,13));e2,f2=run(rows,(len(rows),))
    assert e==e2==0;assert np.array_equal(f,f2)
    assert Decimal(int(f[0,1]))/10==max(books[0])
    assert Decimal(int(f[0,3]))/1000==sum(books[0].values())


def test_real_343_row_update_remains_atomic_at_row_71():
    import json
    data = json.loads(Path(__file__).with_name('fixtures').joinpath('aees_event_20260623_003807235.json').read_text())
    identity = data['identity']
    # A narrow book projection preserves all touched levels and original top.
    levels = [{}, {}]
    levels[0][639582] = 1
    levels[1][639583] = 1
    for change in data['levelChanges']:
        side = int(change['side'] == 'ask')
        p = int(Decimal(change['price']) * 10)
        q = int(Decimal(change['previousQuantity']) * 1000)
        if q:
            levels[side][p] = q
    previous = identity['prev_final_update_id']
    rows = [snap(0, p, q, side, previous) for side in (0, 1) for p, q in levels[side].items()]
    boundary = len(rows) + 71
    for change in data['levelChanges']:
        rows.append([1, 1, 0, identity['first_update_id'], identity['final_update_id'], previous, 0,
                     int(change['side'] == 'ask'), int(Decimal(change['price']) * 10),
                     int(Decimal(change['incomingQuantity']) * 1000)])
    error, frames = run(rows, (boundary,))
    assert error == 0
    assert tuple(frames[0, 1:3]) == (639577, 639578)


def test_snapshot_bridge_sequence_authority_one_ms_clock_overlap():
    rows = [snap(988,100,1,0,15), snap(988,101,1,1,15),
            [987,985,0,13,16,12,0,0,100,2],
            [1015,1012,0,17,18,16,0,0,100,3]]
    error, frames = run(rows, (2,1))
    assert error == 0
    assert frames[0,3] == 3
