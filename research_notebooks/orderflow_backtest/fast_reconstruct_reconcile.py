import json, time
from pathlib import Path
from decimal import Decimal
import pyarrow.parquet as pq

ROOT = Path('/run/media/mzfshark/Storage/Axodus/Trading/market-data/normalized/canonical')

class FastOrderBook:
    def __init__(self, symbol: str):
        self.symbol = symbol
        self.bids: dict[Decimal, Decimal] = {}
        self.asks: dict[Decimal, Decimal] = {}
        self.best_bid = None
        self.best_ask = None
        self.crossed_count = 0
        self.empty_count = 0
        self.updates_count = 0
        self.reconcile_pass = 0
        self.reconcile_fail = 0

    def apply_snapshot(self, bids, asks):
        self.bids = {Decimal(p): Decimal(q) for p, q in bids if Decimal(q) > 0}
        self.asks = {Decimal(p): Decimal(q) for p, q in asks if Decimal(q) > 0}
        self.best_bid = max(self.bids.keys()) if self.bids else None
        self.best_ask = min(self.asks.keys()) if self.asks else None

    def apply_batch_rows(self, sides, prices, qtys):
        for s, p_str, q_str in zip(sides, prices, qtys):
            p = Decimal(p_str)
            q = Decimal(q_str)
            book = self.bids if s == 'bid' or s == 'b' else self.asks
            if q == 0:
                book.pop(p, None)
            else:
                book[p] = q
        self.updates_count += len(sides)
        if self.bids and self.asks:
            self.best_bid = max(self.bids.keys())
            self.best_ask = min(self.asks.keys())
            if self.best_bid >= self.best_ask:
                self.crossed_count += 1
        else:
            self.empty_count += 1

    def reconcile(self, snap_bids, snap_asks):
        if not self.bids or not self.asks:
            self.reconcile_fail += 1
            return False
        cur_bb = max(self.bids.keys())
        cur_ba = min(self.asks.keys())
        sp_bids = sorted([(Decimal(p), Decimal(q)) for p, q in snap_bids], key=lambda x: x[0], reverse=True)
        sp_asks = sorted([(Decimal(p), Decimal(q)) for p, q in snap_asks], key=lambda x: x[0])
        if sp_bids and sp_asks and cur_bb == sp_bids[0][0] and cur_ba == sp_asks[0][0]:
            self.reconcile_pass += 1
            return True
        else:
            self.reconcile_fail += 1
            return False

def run_reconstruction(sym, boot_day, boot_h, boot_lid, days=['2026-06-23', '2026-06-24', '2026-06-25']):
    print(f'=== Fast Reconstruction for {sym} across {len(days)} days ===')
    t0 = time.time()
    ob = FastOrderBook(sym)

    # Bootstrap
    p = ROOT / boot_day / f'{boot_h:02d}' / f'{sym}_orderbook.parquet'
    t = pq.read_table(p, columns=['event_type', 'side', 'price', 'quantity', 'last_update_id'])
    d = t.to_pydict()
    sb, sa = [], []
    s_idx = None
    for i, typ in enumerate(d['event_type']):
        if typ == 'snapshot':
            if d['side'][i] == 'bid': sb.append((str(d['price'][i]), str(d['quantity'][i])))
            else: sa.append((str(d['price'][i]), str(d['quantity'][i])))
            s_idx = i
    ob.apply_snapshot(sb, sa)

    # Rest of bootstrap hour
    if s_idx is not None and s_idx + 1 < len(d['event_type']):
        rem_sides = d['side'][s_idx+1:]
        rem_prices = [str(x) for x in d['price'][s_idx+1:]]
        rem_qtys = [str(x) for x in d['quantity'][s_idx+1:]]
        ob.apply_batch_rows(rem_sides, rem_prices, rem_qtys)

    # Target days
    for day in days:
        for h in range(24):
            hp = ROOT / day / f'{h:02d}' / f'{sym}_orderbook.parquet'
            pf = pq.ParquetFile(hp)
            for batch in pf.iter_batches(columns=['event_type', 'side', 'price', 'quantity', 'last_update_id']):
                bd = batch.to_pydict()
                types = bd['event_type']
                sides = bd['side']
                prices = [str(x) for x in bd['price']]
                qtys = [str(x) for x in bd['quantity']]

                if 'snapshot' in types:
                    # Reconcile with snapshot
                    cur_sb, cur_sa = [], []
                    for k, typ in enumerate(types):
                        if typ == 'snapshot':
                            if sides[k] == 'bid': cur_sb.append((prices[k], qtys[k]))
                            else: cur_sa.append((prices[k], qtys[k]))
                    if cur_sb and cur_sa:
                        ob.reconcile(cur_sb, cur_sa)

                # Apply updates in batch
                up_sides = [sides[k] for k, typ in enumerate(types) if typ != 'snapshot']
                up_prices = [prices[k] for k, typ in enumerate(types) if typ != 'snapshot']
                up_qtys = [qtys[k] for k, typ in enumerate(types) if typ != 'snapshot']
                if up_sides:
                    ob.apply_batch_rows(up_sides, up_prices, up_qtys)

    dur = time.time() - t0
    print(f'Done {sym} in {dur:.2f}s: best_bid={ob.best_bid}, best_ask={ob.best_ask}, spread={ob.best_ask-ob.best_bid}')
    print(f'Stats: updates_rows={ob.updates_count}, crossed={ob.crossed_count}, empty={ob.empty_count}, reconciliations={ob.reconcile_pass} pass / {ob.reconcile_fail} fail')
    return {
        'symbol': sym,
        'days': len(days),
        'durationSeconds': round(dur, 2),
        'bestBid': str(ob.best_bid),
        'bestAsk': str(ob.best_ask),
        'spread': str(ob.best_ask - ob.best_bid),
        'updatesRowsProcessed': ob.updates_count,
        'crossedBooks': ob.crossed_count,
        'emptyBooks': ob.empty_count,
        'snapshotReconciliationPasses': ob.reconcile_pass,
        'snapshotReconciliationFailures': ob.reconcile_fail,
        'reconciliationStatus': 'PASS' if ob.reconcile_fail == 0 and ob.crossed_count == 0 else 'FAIL'
    }

res = [
    run_reconstruction('BTCUSDT', '2026-06-22', 22, 10869949249143, ['2026-06-23', '2026-06-24', '2026-06-25']),
    run_reconstruction('ETHUSDC', '2026-06-22', 14, 10867286016586, ['2026-06-23']),
]
print(json.dumps(res, indent=2))
