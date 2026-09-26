"""Verify complete IS frame artifacts and persist repeatable Absorption evidence.

No raw market data, network calls, or OOS evaluation are performed here.
"""
from dataclasses import fields
from decimal import Decimal
from pathlib import Path
import argparse
import hashlib
import json

from .orderflow_contracts import OrderFlowFrameV1
from .parquet_orderflow_frames import IS_START_MS, IS_END_MS
from .real_absorption_replay import AbsorptionReplay, ReplayConfig


def load_verified_frames(path: Path, expected_hash: str):
    result = []
    digest = hashlib.sha256()
    for line in path.open():
        row = json.loads(line)
        timestamp = row['timestamp_ms']
        if not IS_START_MS <= timestamp <= IS_END_MS:
            raise ValueError('frame outside sealed IS contract')
        frame = OrderFlowFrameV1(**{f.name: (timestamp if f.name == 'timestamp_ms' else Decimal(row[f.name]))
                                    for f in fields(OrderFlowFrameV1)})
        digest.update(json.dumps(frame.__dict__, sort_keys=True, default=str, separators=(',', ':')).encode())
        if frame.timestamp_ms != IS_START_MS + len(result) * 60000 + 59999:
            raise ValueError('missing or duplicate minute')
        if frame.best_bid >= frame.best_ask:
            raise ValueError('crossed frame')
        result.append(frame)
    if len(result) != 14400 or digest.hexdigest() != expected_hash:
        raise ValueError('complete frame identity not verified')
    return result


def run(frame_dir: Path, output_root: Path):
    stats = json.loads((frame_dir/'stats.json').read_text())['stats']
    frames = load_verified_frames(frame_dir/'frames.jsonl', stats['frame_stream_hash'])
    config = ReplayConfig(run_id='absorption-btcusdt-is-'+stats['frame_stream_hash'][:16],
                          frame_stream_hash=stats['frame_stream_hash'])
    results = [AbsorptionReplay(config).run(frames, output_root/f'run-{i}') for i in (1, 2)]
    if results[0]['hashes'] != results[1]['hashes'] or results[0]['aggregate'] != results[1]['aggregate']:
        raise ValueError('canonical replay nondeterministic')
    # Historical funding must be resolved before any open-position funding crossing is accepted.
    trades = json.loads((output_root/'run-1/trade_ledger.json').read_text())
    crossings = [t['trade_id'] for t in trades
                 if t['entry_time_ms']//28800000 != t['exit_time_ms']//28800000]
    if crossings:
        raise ValueError('historical funding resolution required for '+','.join(crossings))
    daily = results[0]['aggregate']['daily_net_pnl']
    from datetime import datetime, timezone, timedelta
    for day in range(10):
        key = str((datetime(2026,6,23,tzinfo=timezone.utc)+timedelta(days=day)).date())
        daily.setdefault(key, '0')
    total = sum((Decimal(v) for v in daily.values()), Decimal(0))
    assert total == Decimal(results[0]['aggregate']['net_pnl'])
    result = {'runId': config.run_id, 'frameHash': stats['frame_stream_hash'],
              'hashes': results[0]['hashes'], 'aggregate': results[0]['aggregate'],
              'deterministicReplay': 'PASS', 'fundingCrossingTrades': crossings,
              'oosEventsConsumed': 0}
    output_root.mkdir(parents=True,exist_ok=True)
    (output_root/'reconciliation.json').write_text(json.dumps(result,indent=2)+'\n')
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--frame-dir', type=Path, required=True)
    parser.add_argument('--output-root', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(run(args.frame_dir, args.output_root),indent=2))
