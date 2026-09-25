import json, time
from pathlib import Path
import numpy as np, pyarrow.parquet as pq

ROOT = Path('/run/media/mzfshark/Storage/Axodus/Trading/market-data/normalized/canonical')
START, END = '2026-06-23', '2026-07-06'
OUT_JSON = Path('/opt/Axodus/Trading/.instructions/reports/AXODUS-TRADING-REQ-QUANT-HIST-DATA-02-QUALIFIED-SEGMENTS.json')

def arr(c):
    a = c.to_numpy(zero_copy_only=False)
    if a.dtype.kind in 'iu': return a.astype(np.int64, copy=False)
    o = np.full(len(a), -1, dtype=np.int64)
    for i, x in enumerate(a):
        if x is not None and not (isinstance(x, float) and np.isnan(x)): o[i] = int(x)
    return o

def compute_segments(sym, bootstrap_day, bootstrap_hour, bootstrap_lid):
    # Gather bootstrap files + in-window files
    files = []
    for h in range(bootstrap_hour, 24):
        p = ROOT / bootstrap_day / f'{h:02d}' / f'{sym}_orderbook.parquet'
        if p.exists(): files.append(p)
    in_window_paths = sorted(p for p in ROOT.glob(f'*/**/{sym}_orderbook.parquet') if START <= p.parts[-3] <= END)
    files.extend(in_window_paths)

    # Iterate and track segments strictly within the 14d window [2026-06-23T00:00:00Z, 2026-07-06T23:59:59.999Z]
    window_start_ts = 1782172800000
    window_end_ts   = 1783382399999

    active_qualified = False
    last_final = None
    prev_type = None
    started = False
    carry = None

    # Segments inside the 14-day window
    segments = []
    invalid_intervals = []

    current_seg_start_ts = None
    current_seg_start_seq = None
    current_seg_start_part = None
    current_seg_boot_source = None

    current_inv_start_ts = None
    current_inv_start_part = None
    current_inv_reason = None

    total_events = 0
    qualified_events = 0

    for p in files:
        part = f'{p.parts[-3]}/{p.parts[-2]}'
        is_window = START <= p.parts[-3] <= END
        for b in pq.ParquetFile(p).iter_batches(columns=['event_type','event_time','transaction_time','first_update_id','final_update_id','prev_final_update_id','last_update_id'], batch_size=1_000_000):
            n = b.num_rows
            ty = b.column(0).to_numpy(zero_copy_only=False); et = arr(b.column(1)); tt = arr(b.column(2))
            f = arr(b.column(3)); u = arr(b.column(4)); pu = arr(b.column(5)); lid = arr(b.column(6))
            if not n: continue
            same = (ty[1:]==ty[:-1])&(et[1:]==et[:-1])&(tt[1:]==tt[:-1])&(f[1:]==f[:-1])&(u[1:]==u[:-1])&(pu[1:]==pu[:-1])&(lid[1:]==lid[:-1])
            st = np.r_[0, np.flatnonzero(~same)+1]
            keys = list(zip(ty.tolist(),et.tolist(),tt.tolist(),f.tolist(),u.tolist(),pu.tolist(),lid.tolist()))
            if carry is not None and keys[0]==carry: st = st[1:]
            if not len(st): carry = keys[-1]; continue

            for j in st:
                typ = str(ty[j]); event_time = int(et[j]); f_id = int(f[j]); u_id = int(u[j]); pu_id = int(pu[j]); l_id = int(lid[j])

                # Look for anchor snapshot
                if not started:
                    if typ == 'snapshot' and l_id == bootstrap_lid:
                        started = True
                        active_qualified = True
                        last_final = l_id
                        prev_type = 'snapshot'
                    continue

                # Process event
                if typ == 'snapshot':
                    # Snapshot recovery / rebootstrap
                    if not active_qualified:
                        # We were in an invalid interval; snapshot recovers it
                        if is_window and current_inv_start_ts is not None:
                            invalid_intervals.append({
                                'startTs': current_inv_start_ts,
                                'endTs': event_time,
                                'startPartition': current_inv_start_part,
                                'endPartition': part,
                                'durationSeconds': round((event_time - current_inv_start_ts) / 1000.0, 3),
                                'reason': current_inv_reason,
                                'recoverySnapshotId': l_id,
                            })
                            current_inv_start_ts = None
                        active_qualified = True
                        if is_window:
                            current_seg_start_ts = event_time
                            current_seg_start_seq = l_id
                            current_seg_start_part = part
                            current_seg_boot_source = f'SNAPSHOT_REBOOT_{l_id}'
                    last_final = l_id
                    prev_type = 'snapshot'
                    continue

                # It is an update
                if is_window and current_seg_start_ts is None and active_qualified:
                    current_seg_start_ts = event_time
                    current_seg_start_seq = f_id
                    current_seg_start_part = part
                    current_seg_boot_source = 'PRE_WINDOW_BOOTSTRAP'

                # Continuity check
                valid_bootstrap = (prev_type == 'snapshot') and (f_id <= last_final + 1 <= u_id)
                valid_continuous = (prev_type != 'snapshot') and (pu_id == last_final)

                if is_window:
                    total_events += 1

                if valid_bootstrap or valid_continuous:
                    if is_window and active_qualified:
                        qualified_events += 1
                    last_final = u_id
                    prev_type = 'update'
                else:
                    # TRUE SEQUENCE GAP DETECTED
                    if active_qualified:
                        active_qualified = False
                        if is_window and current_seg_start_ts is not None:
                            segments.append({
                                'startTs': current_seg_start_ts,
                                'endTs': event_time,
                                'startPartition': current_seg_start_part,
                                'endPartition': part,
                                'firstSequence': current_seg_start_seq,
                                'lastSequence': last_final,
                                'bootstrapSource': current_seg_boot_source,
                                'terminalReason': 'SEQUENCE_GAP',
                                'durationSeconds': round((event_time - current_seg_start_ts) / 1000.0, 3),
                            })
                            current_seg_start_ts = None
                        if is_window:
                            current_inv_start_ts = event_time
                            current_inv_start_part = part
                            current_inv_reason = f'GAP_pu_{pu_id}_ne_prev_{last_final}'
                    last_final = u_id
                    prev_type = 'update'
            carry = keys[-1]

    # Close trailing segment / interval
    if active_qualified and current_seg_start_ts is not None:
        segments.append({
            'startTs': current_seg_start_ts,
            'endTs': window_end_ts,
            'startPartition': current_seg_start_part,
            'endPartition': '2026-07-06/23',
            'firstSequence': current_seg_start_seq,
            'lastSequence': last_final,
            'bootstrapSource': current_seg_boot_source,
            'terminalReason': 'END_OF_WINDOW',
            'durationSeconds': round((window_end_ts - current_seg_start_ts) / 1000.0, 3),
        })
    elif not active_qualified and current_inv_start_ts is not None:
        invalid_intervals.append({
            'startTs': current_inv_start_ts,
            'endTs': window_end_ts,
            'startPartition': current_inv_start_part,
            'endPartition': '2026-07-06/23',
            'durationSeconds': round((window_end_ts - current_inv_start_ts) / 1000.0, 3),
            'reason': current_inv_reason,
            'recoverySnapshotId': None,
        })

    # Coverage math
    total_window_sec = 14 * 86400
    qual_sec = sum(s['durationSeconds'] for s in segments)
    inv_sec = sum(i['durationSeconds'] for i in invalid_intervals)

    # Split coverage
    is_end_ts = 1783036799999 # 2026-07-02T23:59:59.999Z
    is_total_sec = 10 * 86400
    oos_total_sec = 4 * 86400

    is_qual_sec = 0.0
    oos_qual_sec = 0.0
    for s in segments:
        st, et = s['startTs'], s['endTs']
        if et <= is_end_ts:
            is_qual_sec += (et - st) / 1000.0
        elif st > is_end_ts:
            oos_qual_sec += (et - st) / 1000.0
        else:
            is_qual_sec += (is_end_ts - st) / 1000.0
            oos_qual_sec += (et - is_end_ts) / 1000.0

    return {
        'symbol': sym,
        'totalWindowSeconds': total_window_sec,
        'qualifiedSeconds': round(qual_sec, 3),
        'invalidSeconds': round(inv_sec, 3),
        'qualifiedCoveragePct': round(100.0 * qual_sec / total_window_sec, 4),
        'isQualifiedCoveragePct': round(100.0 * is_qual_sec / is_total_sec, 4),
        'oosQualifiedCoveragePct': round(100.0 * oos_qual_sec / oos_total_sec, 4),
        'segmentCount': len(segments),
        'invalidIntervalCount': len(invalid_intervals),
        'largestQualifiedSegmentSec': max((s['durationSeconds'] for s in segments), default=0),
        'maxInvalidIntervalSec': max((i['durationSeconds'] for i in invalid_intervals), default=0),
        'qualifiedSegments': segments,
        'invalidIntervals': invalid_intervals,
        'totalEventsInWindow': total_events,
        'qualifiedEventsInWindow': qualified_events,
    }

print('Computing qualified segment ledger for BTCUSDT and ETHUSDC...')
res = {
    'datasetId': 'orderflow-binance-futures-14d-20260623-20260706-v1',
    'window': {'start': '2026-06-23T00:00:00Z', 'end': '2026-07-06T23:59:59.999Z'},
    'generatedAt': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
    'symbols': [
        compute_segments('BTCUSDT', '2026-06-22', 22, 10869949249143),
        compute_segments('ETHUSDC', '2026-06-22', 14, 10867286016586),
    ]
}
OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
OUT_JSON.write_text(json.dumps(res, indent=2))

for s in res['symbols']:
    print(f"\n=== {s['symbol']} QUALIFIED SUMMARY ===")
    print(f"  Coverage: {s['qualifiedCoveragePct']}% (IS: {s['isQualifiedCoveragePct']}%, OOS: {s['oosQualifiedCoveragePct']}%)")
    print(f"  Segments: {s['segmentCount']}, Invalid intervals: {s['invalidIntervalCount']}")
    print(f"  Largest segment: {s['largestQualifiedSegmentSec']}s, Max invalid: {s['maxInvalidIntervalSec']}s")
    print(f"  Events: {s['qualifiedEventsInWindow']}/{s['totalEventsInWindow']}")
