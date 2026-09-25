import os, sys, json, time
from pathlib import Path
import cryptohftdata as chd
import pyarrow as pa
import pyarrow.parquet as pq

env_path = Path('/opt/Axodus/Trading/.env.local')
api_key = os.getenv('HFT_DATA_API_KEY')
if not api_key and env_path.exists():
    for raw_line in env_path.read_text().splitlines():
        if raw_line.startswith('HFT_DATA_API_KEY='):
            api_key = raw_line.split('=', 1)[1].strip().strip('"').strip("'")
            break

if not api_key:
    print('ERROR: HFT_DATA_API_KEY not found', file=sys.stderr)
    sys.exit(1)

client = chd.CryptoHFTDataClient(api_key=api_key, timeout=30)
data_root = Path('/run/media/mzfshark/Storage/Axodus/Trading/market-data')
funding_dir = data_root / 'normalized' / 'auxiliary'
funding_dir.mkdir(parents=True, exist_ok=True)

START_DATE = '2026-06-23'
END_DATE   = '2026-07-06'

results = []

for sym in ('BTCUSDT', 'ETHUSDC'):
    print(f'=== Acquiring Funding & Mark Price for {sym} across 14 days ===')
    t0 = time.time()
    try:
        # Fetch mark price / funding from CryptoHFTData
        df = client.get_mark_price(
            symbol=sym,
            exchange=chd.exchanges.BINANCE_FUTURES,
            start_date=START_DATE,
            end_date=END_DATE,
        )
        row_count = len(df)
        print(f'Fetched {row_count} rows for {sym} in {time.time()-t0:.2f}s')

        # Persist as Parquet for durable local provenance
        out_p = funding_dir / f'{sym}_mark_price_14d.parquet'
        table = pa.Table.from_pandas(df)
        pq.write_table(table, out_p, compression='zstd')

        # Check funding events (every 8 hours = 3/day * 14 days = 42 events expected)
        first_ts = int(df['event_time'].min()) if 'event_time' in df.columns else None
        last_ts  = int(df['event_time'].max()) if 'event_time' in df.columns else None
        funding_events = len(df[df['funding_rate'].notnull() & (df['funding_rate'] != 0)]) if 'funding_rate' in df.columns else 0

        results.append({
            'symbol': sym,
            'provider': 'CryptoHFTData',
            'product': 'Binance USD-M Futures Mark Price & Funding',
            'window': {'start': f'{START_DATE}T00:00:00Z', 'end': f'{END_DATE}T23:59:59.999Z'},
            'persistedPath': str(out_p),
            'fileSizeBytes': out_p.stat().st_size,
            'rowCount': row_count,
            'firstEventTs': first_ts,
            'lastEventTs': last_ts,
            'fundingEventCount': funding_events,
            'coverageStatus': 'PASS' if row_count > 0 else 'FAIL',
        })
    except Exception as e:
        print(f'Error fetching {sym}: {e}')
        results.append({
            'symbol': sym,
            'coverageStatus': 'FAIL',
            'error': str(e),
        })

out_json = Path('/opt/Axodus/Trading/.instructions/reports/AXODUS-TRADING-REQ-QUANT-HIST-DATA-02-FUNDING-METADATA.json')
out_json.parent.mkdir(parents=True, exist_ok=True)
out_json.write_text(json.dumps(results, indent=2))
print(json.dumps(results, indent=2))
