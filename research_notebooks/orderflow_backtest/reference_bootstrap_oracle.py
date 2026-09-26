"""Independent bounded Decimal reconstruction from the qualified snapshot."""
from decimal import Decimal
from pathlib import Path
import json
import time
import pyarrow.parquet as pq

FIELDS = ['event_time', 'transaction_time', 'event_type', 'first_update_id',
          'final_update_id', 'prev_final_update_id', 'last_update_id']


def run(root):
    start = time.monotonic()
    canonical = root / 'normalized/canonical'
    paths = [canonical / '2026-06-22' / h / 'BTCUSDT_orderbook.parquet' for h in ('22', '23')]
    paths.append(canonical / '2026-06-23/00/BTCUSDT_orderbook.parquet')
    target = 1782175087235
    books = [{}, {}]
    pending = None
    live = False
    events = 0
    rows = 0
    previous = None
    bridge = False
    before = None
    def best():
        return {'bid': str(max(books[0])), 'ask': str(min(books[1]))}
    for path in paths:
        for batch in pq.ParquetFile(path).iter_batches(batch_size=100000):
            data = batch.to_pydict()
            columns = [data[k] for k in FIELDS]
            for i in range(batch.num_rows):
                key = tuple(c[i] for c in columns)
                if key[0] > target:
                    break
                if key != pending:
                    if pending is not None and live:
                        previous = pending[6] if pending[2] == 'snapshot' else pending[4]
                        events += 1
                    if key[2] == 'snapshot':
                        books = [{}, {}]
                        live = True
                        bridge = True
                    elif not live:
                        pending = None
                        continue
                    elif bridge:
                        assert key[3] <= previous <= key[4] or key[5] == previous
                        bridge = False
                    else:
                        assert key[5] == previous, (pending, key)
                    if key[0] == target:
                        before = best()
                    pending = key
                side = 0 if data['side'][i] == 'bid' else 1
                price, quantity = Decimal(data['price'][i]), Decimal(data['quantity'][i])
                if quantity:
                    books[side][price] = quantity
                else:
                    books[side].pop(price, None)
                rows += 1
            if data['event_time'][-1] > target:
                break
    assert before is not None and max(books[0]) < min(books[1])
    result = {'independentFromSnapshot': True, 'snapshotLastUpdateId': 10869949249143,
              'target': target, 'before': before, 'after': best(), 'rows': rows,
              'logicalEvents': events + 1, 'runtimeSeconds': time.monotonic()-start,
              'top10Bids': [[str(p), str(books[0][p])] for p in sorted(books[0], reverse=True)[:10]],
              'top10Asks': [[str(p), str(books[1][p])] for p in sorted(books[1])[:10]]}
    (root/'tmp/aees-closure/independent-reference.json').write_text(json.dumps(result, indent=2)+'\n')
    print(json.dumps(result))


if __name__ == '__main__':
    run(Path('/run/media/mzfshark/Storage/Axodus/Trading/market-data'))
