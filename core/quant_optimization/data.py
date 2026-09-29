"""Hash-verified historical inputs only; no download or live-run reader."""
import json
from datetime import datetime, timezone
from pathlib import Path
from .models import OptimizationError, decimal
from .provenance import file_hash

PROTECTED_RUN = '064e34bd-55c2-4be9-a8c4-212827eb5c9b'


def approved_path(dataset):
    path = Path(dataset.path).resolve()
    if PROTECTED_RUN in str(path) or 'runs' in path.parts or dataset.provenance not in {'KNOWN_HISTORICAL', 'SYNTHETIC_CERTIFICATION', 'SEALED_UNOBSERVED'}:
        raise OptimizationError('live/evaluation inputs prohibited; explicit historical provenance required')
    return path


def load_states(dataset, window):
    path = approved_path(dataset)
    if file_hash(path) != dataset.sha256:
        raise OptimizationError('dataset hash mismatch')
    rows = json.loads(path.read_text(), parse_float=lambda _: (_ for _ in ()).throw(OptimizationError('float prices prohibited')))
    result = []
    previous = None
    start = datetime.fromisoformat(window.start.replace('Z', '+00:00'))
    end = datetime.fromisoformat(window.end.replace('Z', '+00:00'))
    for row in rows:
        # Binance candle close time, not open time, is information availability.
        if isinstance(row, list):
            dt = datetime.fromtimestamp(row[6] // 1000, timezone.utc).replace(microsecond=(row[6] % 1000)*1000)
            close, high, low = row[4], row[2], row[3]
        else:
            dt = datetime.fromisoformat(row['marketTime'].replace('Z', '+00:00'))
            if dt.tzinfo is None or row.get('validityContext') != 'historical' or row.get('symbol') != dataset.symbol:
                raise OptimizationError('historical symbol/time identity mismatch')
            close, high, low = row['marketPrice'], row['high'], row['low']
        if not start <= dt < end:
            continue
        if previous is not None and (dt-previous).total_seconds() != 60:
            raise OptimizationError('missing/duplicate/out-of-order candle')
        previous = dt
        if not 0 < decimal(low) <= decimal(close) <= decimal(high):
            raise OptimizationError('invalid OHLC')
        stamp = dt.isoformat().replace('+00:00', 'Z')
        result.append({'marketTime': stamp, 'observedAt': stamp, 'marketPrice':close, 'high':high, 'low':low,
                       'validityContext':'historical', 'symbol':dataset.symbol, 'features':[]})
    if len(result) < 32 or (datetime.fromisoformat(result[0]['marketTime'].replace('Z','+00:00'))-start).total_seconds() >= 60 or (end-previous).total_seconds() > 60:
        raise OptimizationError('insufficient complete causal candle coverage')
    return result
