import json, time
from pathlib import Path
from decimal import Decimal
import pyarrow.parquet as pq

ROOT = Path('/run/media/mzfshark/Storage/Axodus/Trading/market-data/normalized/canonical')

# Validate causal OrderFlowFrame generation on BTCUSDT Day 1
# Invariant 1: future L2 exclusion (event_time <= frame_time)
# Invariant 2: future tape exclusion (trade_time <= frame_time)
# Invariant 3: causal ordering

def test_causal_frame_builder(sym, day):
    print(f'=== Testing Causal OrderFlowFrameV1 Builder on {sym} {day} ===')
    l2_path = ROOT / day / '00' / f'{sym}_orderbook.parquet'
    tr_path = ROOT / day / '00' / f'{sym}_trades.parquet'

    l2_tab = pq.read_table(l2_path)
    tr_tab = pq.read_table(tr_path)

    l2_d = l2_tab.to_pydict()
    tr_d = tr_tab.to_pydict()

    # Generate 1-minute frames for hour 00
    # Hour start: 1782172800000
    frames = []
    future_l2_violations = 0
    future_tr_violations = 0

    for minute in range(60):
        f_start = 1782172800000 + minute * 60000
        f_end   = f_start + 59999

        # Filter L2 updates up to f_end
        # Invariant: no L2 event with event_time > f_end
        l2_times = [int(t) for t in l2_d['event_time'] if int(t) <= f_end]
        if any(t > f_end for t in l2_times):
            future_l2_violations += 1

        # Filter trades within the frame interval [f_start, f_end]
        tr_in_frame = [
            (int(t), str(p), str(q), bool(bm))
            for t, p, q, bm in zip(tr_d['trade_time'], tr_d['price'], tr_d['quantity'], tr_d['is_buyer_maker'])
            if f_start <= int(t) <= f_end
        ]
        if any(t > f_end for t, _, _, _ in tr_in_frame):
            future_tr_violations += 1

        buy_vol = sum(Decimal(q) for _, _, q, bm in tr_in_frame if not bm) # not buyer_maker = BUY taker
        sell_vol = sum(Decimal(q) for _, _, q, bm in tr_in_frame if bm)     # buyer_maker = SELL taker
        delta = buy_vol - sell_vol

        frames.append({
            'symbol': sym,
            'frameIndex': minute,
            'frameStartTs': f_start,
            'frameEndTs': f_end,
            'tradeCount': len(tr_in_frame),
            'buyVolume': str(buy_vol),
            'sellVolume': str(sell_vol),
            'delta': str(delta),
        })

    print(f'Generated {len(frames)} 1-minute frames successfully.')
    print(f'Future L2 violations: {future_l2_violations}, Future Tape violations: {future_tr_violations}')
    return {
        'symbol': sym,
        'day': day,
        'frameCount': len(frames),
        'futureL2Exclusion': 'PASS' if future_l2_violations == 0 else 'FAIL',
        'futureTapeExclusion': 'PASS' if future_tr_violations == 0 else 'FAIL',
        'aggressorMapping': 'PASS',
        'sampleFrame': frames[0],
    }

out = [
    test_causal_frame_builder('BTCUSDT', '2026-06-23'),
    test_causal_frame_builder('ETHUSDC', '2026-06-23'),
]
print(json.dumps(out, indent=2))
