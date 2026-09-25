import json, time
from pathlib import Path
from decimal import Decimal
import pyarrow.parquet as pq

ROOT = Path('/run/media/mzfshark/Storage/Axodus/Trading/market-data/normalized/canonical')

class OrderBookReconstructor:
    def __init__(self, symbol: str):
        self.symbol = symbol
        self.bids: dict[Decimal, Decimal] = {}
        self.asks: dict[Decimal, Decimal] = {}
        self.last_update_id = None
        self.crossed_count = 0
        self.empty_bid_count = 0
        self.empty_ask_count = 0
        self.invalid_qty_count = 0
        self.events_processed = 0
        self.reconciliation_passes = 0
        self.reconciliation_failures = 0

    def apply_snapshot(self, bids: list[tuple[str, str]], asks: list[tuple[str, str]], last_update_id: int):
        self.bids = {Decimal(p): Decimal(q) for p, q in bids if Decimal(q) > 0}
        self.asks = {Decimal(p): Decimal(q) for p, q in asks if Decimal(q) > 0}
        self.last_update_id = last_update_id
        self.check_invariants()

    def apply_update(self, side: str, price: str, qty: str, update_id: int):
        p = Decimal(price)
        q = Decimal(qty)
        if q < 0:
            self.invalid_qty_count += 1
            return
        book = self.bids if side == 'bid' or side == 'b' else self.asks
        if q == 0:
            book.pop(p, None)
        else:
            book[p] = q
        self.last_update_id = update_id
        self.events_processed += 1
        self.check_invariants()

    def check_invariants(self):
        if not self.bids:
            self.empty_bid_count += 1
            return
        if not self.asks:
            self.empty_ask_count += 1
            return
        best_bid = max(self.bids.keys())
        best_ask = min(self.asks.keys())
        if best_bid >= best_ask:
            self.crossed_count += 1

    def reconcile_with_snapshot(self, snap_bids, snap_asks, snap_lid):
        # Compare top-10 reconstructed bids/asks with provider snapshot
        rec_top_b = sorted(self.bids.items(), key=lambda x: x[0], reverse=True)[:10]
        rec_top_a = sorted(self.asks.items(), key=lambda x: x[0])[:10]
        snap_top_b = sorted([(Decimal(p), Decimal(q)) for p, q in snap_bids], key=lambda x: x[0], reverse=True)[:10]
        snap_top_a = sorted([(Decimal(p), Decimal(q)) for p, q in snap_asks], key=lambda x: x[0])[:10]

        # Check best bid / ask exact match
        if rec_top_b and snap_top_b and rec_top_b[0] == snap_top_b[0] and rec_top_a and snap_top_a and rec_top_a[0] == snap_top_a[0]:
            self.reconciliation_passes += 1
            return True
        else:
            self.reconciliation_failures += 1
            return False

def test_symbol(sym, boot_day, boot_hour, boot_lid, target_days=['2026-06-23']):
    print(f'=== Testing Reconstructor for {sym} ===')
    recon = OrderBookReconstructor(sym)

    # 1. Bootstrap
    p = ROOT / boot_day / f'{boot_hour:02d}' / f'{sym}_orderbook.parquet'
    t = pq.read_table(p)
    d = t.to_pydict()
    snap_bids, snap_asks = [], []
    snap_idx = None
    for i, typ in enumerate(d['event_type']):
        if typ == 'snapshot':
            if d['side'][i] == 'bid': snap_bids.append((str(d['price'][i]), str(d['quantity'][i])))
            else: snap_asks.append((str(d['price'][i]), str(d['quantity'][i])))
            snap_idx = i
    recon.apply_snapshot(snap_bids, snap_asks, boot_lid)
    print(f'Bootstrap applied: bids={len(recon.bids)}, asks={len(recon.asks)}, best_b={max(recon.bids.keys())}, best_a={min(recon.asks.keys())}')

    # Apply rest of bootstrap hour
    for i in range(snap_idx + 1, len(d['event_type'])):
        if d['event_type'][i] != 'snapshot':
            recon.apply_update(d['side'][i], str(d['price'][i]), str(d['quantity'][i]), int(d['final_update_id'][i]))

    # Replay target days and reconcile
    for day in target_days:
        for h in range(24):
            hp = ROOT / day / f'{h:02d}' / f'{sym}_orderbook.parquet'
            pf = pq.ParquetFile(hp)
            for batch in pf.iter_batches(columns=['event_type', 'side', 'price', 'quantity', 'final_update_id', 'last_update_id']):
                bd = batch.to_pydict()
                bty = bd['event_type']
                bside = bd['side']
                bp = bd['price']
                bq = bd['quantity']
                bfinal = bd['final_update_id']
                blid = bd['last_update_id']

                # If batch contains snapshot, collect it for reconciliation
                if 'snapshot' in bty:
                    cur_bids, cur_asks = [], []
                    cur_lid = None
                    for k in range(len(bty)):
                        if bty[k] == 'snapshot':
                            cur_lid = int(blid[k])
                            if bside[k] == 'bid': cur_bids.append((str(bp[k]), str(bq[k])))
                            else: cur_asks.append((str(bp[k]), str(bq[k])))
                    if cur_bids and cur_asks:
                        recon.reconcile_with_snapshot(cur_bids, cur_asks, cur_lid)

                for k in range(len(bty)):
                    if bty[k] != 'snapshot':
                        recon.apply_update(bside[k], str(bp[k]), str(bq[k]), int(bfinal[k]))

    best_b, best_a = max(recon.bids.keys()), min(recon.asks.keys())
    print(f'Final State {sym} after {len(target_days)} days: best_bid={best_b}, best_ask={best_a}, spread={best_a-best_b}')
    print(f'Stats: updates={recon.events_processed}, crossed={recon.crossed_count}, empty_bids={recon.empty_bid_count}, empty_asks={recon.empty_ask_count}, reconciliations={recon.reconciliation_passes} pass / {recon.reconciliation_failures} fail')
    return {
        'symbol': sym,
        'finalBestBid': str(best_b),
        'finalBestAsk': str(best_a),
        'spread': str(best_a - best_b),
        'updatesProcessed': recon.events_processed,
        'crossedBooks': recon.crossed_count,
        'emptyBids': recon.empty_bid_count,
        'emptyAsks': recon.empty_ask_count,
        'snapshotReconciliationPasses': recon.reconciliation_passes,
        'snapshotReconciliationFailures': recon.reconciliation_failures,
        'reconciliationStatus': 'PASS' if recon.reconciliation_failures == 0 and recon.crossed_count == 0 else 'FAIL'
    }

res = [
    test_symbol('BTCUSDT', '2026-06-22', 22, 10869949249143, ['2026-06-23', '2026-06-24']),
]
print(json.dumps(res, indent=2))
