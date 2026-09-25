import json, time
from pathlib import Path
import numpy as np, pyarrow.dataset as ds

ROOT = Path('/run/media/mzfshark/Storage/Axodus/Trading/market-data/normalized/canonical')
START, END = '2026-06-23', '2026-07-06'
OUT_JSON = Path('/opt/Axodus/Trading/.instructions/reports/AXODUS-TRADING-REQ-QUANT-HIST-DATA-02-QUALIFIED-SEGMENTS.json')
LEDGER_JSON = Path('/opt/Axodus/Trading/.instructions/reports/AXODUS-TRADING-REQ-QUANT-HIST-DATA-02-SEQUENCE-LEDGER.json')

ledger = json.loads(LEDGER_JSON.read_text())

def get_snapshots_arrow(sym):
    paths = sorted(p for p in ROOT.glob(f'*/**/{sym}_orderbook.parquet') if '2026-06-22' <= p.parts[-3] <= '2026-07-06')
    snaps = []
    for p in paths:
        dset = ds.dataset(p, format="parquet")
        filt = ds.field("event_type") == "snapshot"
        table = dset.to_table(filter=filt, columns=["event_time", "last_update_id"])
        if table.num_rows > 0:
            et = table.column("event_time").to_numpy(zero_copy_only=False)
            lid = table.column("last_update_id").to_numpy(zero_copy_only=False)
            for i in range(table.num_rows):
                snaps.append({
                    'partition': f'{p.parts[-3]}/{p.parts[-2]}',
                    'eventTime': int(et[i]),
                    'lastUpdateId': int(lid[i]),
                })
    seen = set(); uniq = []
    for s in snaps:
        if s['lastUpdateId'] not in seen:
            seen.add(s['lastUpdateId'])
            uniq.append(s)
    return uniq

print('Scanning snapshots using pyarrow.dataset predicate pushdown...')
t0 = time.time()
btc_snaps = get_snapshots_arrow('BTCUSDT')
eth_snaps = get_snapshots_arrow('ETHUSDC')
print(f'Done in {time.time()-t0:.2f}s: Found {len(btc_snaps)} BTC snapshots, {len(eth_snaps)} ETH snapshots.')

WINDOW_START_TS = 1782172800000 # 2026-06-23T00:00:00.000Z
WINDOW_END_TS   = 1783382399999 # 2026-07-06T23:59:59.999Z
IS_END_TS       = 1783036799999 # 2026-07-02T23:59:59.999Z
TOTAL_SEC       = 14 * 86400
IS_TOTAL_SEC    = 10 * 86400
OOS_TOTAL_SEC   = 4 * 86400

def build_symbol_ledger(sym, snaps, findings):
    gaps = [f for f in findings if f['classification'] in ('TRUE_SEQUENCE_GAP', 'AMBIGUOUS')]
    in_window_gaps = [g for g in gaps if g['partition'] != '2026-06-23/00']

    segments = []
    invalid_intervals = []

    current_state = 'QUALIFIED'
    current_start_ts = WINDOW_START_TS
    current_start_part = '2026-06-23/00'
    current_source = 'PRE_WINDOW_BOOTSTRAP_CONTINUATION'

    in_window_snaps = sorted([s for s in snaps if s['eventTime'] >= WINDOW_START_TS], key=lambda x: x['eventTime'])

    timeline = []
    for g in in_window_gaps:
        timeline.append(('GAP', g['timestamp'], g))
    for s in in_window_snaps:
        timeline.append(('SNAPSHOT', s['eventTime'], s))
    timeline.sort(key=lambda x: x[1])

    for evt_type, evt_ts, evt_data in timeline:
        if evt_type == 'GAP':
            if current_state == 'QUALIFIED':
                segments.append({
                    'startTs': current_start_ts,
                    'endTs': evt_ts,
                    'startPartition': current_start_part,
                    'endPartition': evt_data['partition'],
                    'bootstrapSource': current_source,
                    'terminalReason': 'SEQUENCE_GAP',
                    'durationSeconds': round((evt_ts - current_start_ts) / 1000.0, 3),
                })
                current_state = 'INVALID'
                current_start_ts = evt_ts
                current_start_part = evt_data['partition']
                current_source = f"GAP_{evt_data['currentPrevFinalUpdateId']}_ne_prev"
        elif evt_type == 'SNAPSHOT':
            if current_state == 'INVALID':
                invalid_intervals.append({
                    'startTs': current_start_ts,
                    'endTs': evt_ts,
                    'startPartition': current_start_part,
                    'endPartition': evt_data['partition'],
                    'durationSeconds': round((evt_ts - current_start_ts) / 1000.0, 3),
                    'recoverySnapshotId': evt_data['lastUpdateId'],
                })
                current_state = 'QUALIFIED'
                current_start_ts = evt_ts
                current_start_part = evt_data['partition']
                current_source = f"SNAPSHOT_REBOOT_{evt_data['lastUpdateId']}"

    if current_state == 'QUALIFIED':
        segments.append({
            'startTs': current_start_ts,
            'endTs': WINDOW_END_TS,
            'startPartition': current_start_part,
            'endPartition': '2026-07-06/23',
            'bootstrapSource': current_source,
            'terminalReason': 'END_OF_WINDOW',
            'durationSeconds': round((WINDOW_END_TS - current_start_ts) / 1000.0, 3),
        })
    else:
        invalid_intervals.append({
            'startTs': current_start_ts,
            'endTs': WINDOW_END_TS,
            'startPartition': current_start_part,
            'endPartition': '2026-07-06/23',
            'durationSeconds': round((WINDOW_END_TS - current_start_ts) / 1000.0, 3),
            'recoverySnapshotId': None,
        })

    qual_sec = sum(s['durationSeconds'] for s in segments)
    inv_sec = sum(i['durationSeconds'] for i in invalid_intervals)

    is_qual_sec = 0.0
    oos_qual_sec = 0.0
    for s in segments:
        st, et = s['startTs'], s['endTs']
        if et <= IS_END_TS:
            is_qual_sec += (et - st) / 1000.0
        elif st > IS_END_TS:
            oos_qual_sec += (et - st) / 1000.0
        else:
            is_qual_sec += (IS_END_TS - st) / 1000.0
            oos_qual_sec += (et - IS_END_TS) / 1000.0

    return {
        'symbol': sym,
        'totalWindowSeconds': TOTAL_SEC,
        'qualifiedSeconds': round(qual_sec, 3),
        'invalidSeconds': round(inv_sec, 3),
        'qualifiedCoveragePct': round(100.0 * qual_sec / TOTAL_SEC, 4),
        'isQualifiedCoveragePct': round(100.0 * is_qual_sec / IS_TOTAL_SEC, 4),
        'oosQualifiedCoveragePct': round(100.0 * oos_qual_sec / OOS_TOTAL_SEC, 4),
        'segmentCount': len(segments),
        'invalidIntervalCount': len(invalid_intervals),
        'largestQualifiedSegmentSec': max((s['durationSeconds'] for s in segments), default=0),
        'maxInvalidIntervalSec': max((i['durationSeconds'] for i in invalid_intervals), default=0),
        'qualifiedSegments': segments,
        'invalidIntervals': invalid_intervals,
    }

btc_findings = next(s['anomalies'] for s in ledger['symbols'] if s['symbol']=='BTCUSDT')
eth_findings = next(s['anomalies'] for s in ledger['symbols'] if s['symbol']=='ETHUSDC')

res = {
    'datasetId': 'orderflow-binance-futures-14d-20260623-20260706-v1',
    'window': {'start': '2026-06-23T00:00:00Z', 'end': '2026-07-06T23:59:59.999Z'},
    'generatedAt': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
    'symbols': [
        build_symbol_ledger('BTCUSDT', btc_snaps, btc_findings),
        build_symbol_ledger('ETHUSDC', eth_snaps, eth_findings),
    ]
}
OUT_JSON.write_text(json.dumps(res, indent=2))
for s in res['symbols']:
    print(f"\n=== {s['symbol']} QUALIFIED SUMMARY ===")
    print(f"  Coverage: {s['qualifiedCoveragePct']}% (IS: {s['isQualifiedCoveragePct']}%, OOS: {s['oosQualifiedCoveragePct']}%)")
    print(f"  Segments: {s['segmentCount']}, Invalid intervals: {s['invalidIntervalCount']}")
    print(f"  Largest segment: {s['largestQualifiedSegmentSec']}s, Max invalid: {s['maxInvalidIntervalSec']}s")
