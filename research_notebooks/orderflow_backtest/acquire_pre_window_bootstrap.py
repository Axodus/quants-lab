from __future__ import annotations
import os, sys
from pathlib import Path
import numpy as np
import pyarrow.parquet as pq
env_path = Path('/opt/Axodus/Trading/.env.local')
api_key = os.getenv('HFT_DATA_API_KEY')
if not api_key and env_path.exists():
    for raw_line in env_path.read_text().splitlines():
        if raw_line.startswith('HFT_DATA_API_KEY='):
            api_key = raw_line.split('=', 1)[1].strip().strip('"').strip("'")
            break
if not api_key:
    print('ERROR: HFT_DATA_API_KEY missing', file=sys.stderr)
    sys.exit(1)
from bulk_historical_downloader import BulkHistoricalDownloader
data_root = Path('/run/media/mzfshark/Storage/Axodus/Trading/market-data')
manifest_path = data_root / 'manifests' / 'bootstrap_acquisition_manifest.json'
dl = BulkHistoricalDownloader(api_key=api_key, data_root=data_root, manifest_path=manifest_path, symbols=('BTCUSDT', 'ETHUSDC'), data_types=('orderbook',), window_start='2026-06-22', window_end='2026-06-22')
for sym in ('BTCUSDT', 'ETHUSDC'):
    print(f'=== Searching pre-window bootstrap for {sym} ===')
    found = False
    for hour in range(23, -1, -1):
        print(f'Fetching {sym} 2026-06-22/{hour:02d}...')
        ok = dl.download_partition(sym, 'orderbook', '2026-06-22', hour)
        if not ok: continue
        norm_path = data_root / 'normalized' / 'canonical' / '2026-06-22' / f'{hour:02d}' / f'{sym}_orderbook.parquet'
        if not norm_path.exists(): continue
        pf = pq.ParquetFile(norm_path)
        for rg in range(pf.num_row_groups):
            tab = pf.read_row_group(rg, columns=['event_type', 'event_time', 'transaction_time', 'first_update_id', 'final_update_id', 'prev_final_update_id', 'last_update_id'])
            ty = tab.column(0).to_numpy(zero_copy_only=False)
            idx = np.flatnonzero(ty == 'snapshot')
            if len(idx) > 0:
                et = tab.column(1).to_numpy(zero_copy_only=False)[idx]
                lid = tab.column(6).to_numpy(zero_copy_only=False)[idx]
                print(f'  FOUND SNAPSHOT in 2026-06-22/{hour:02d}! count={len(idx)} et={et[-1]} lid={lid[-1]}')
                found = True
                break
        if found: break
