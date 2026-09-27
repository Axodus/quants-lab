"""Authoritative public venue metadata normalization for InstrumentSpec."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Callable
from urllib.request import Request, urlopen

from .instrument_spec import InstrumentSpec


BINANCE_FUTURES_EXCHANGE_INFO = "https://fapi.binance.com/fapi/v1/exchangeInfo"


def _precision(value: Decimal) -> int:
    return max(-value.normalize().as_tuple().exponent, 0)


def _scale(value: Decimal) -> int:
    normalized = value.normalize()
    scale = 10 ** max(0, -normalized.as_tuple().exponent)
    if value * scale != (value * scale).to_integral_value():
        raise ValueError(f"instrument increment {value} cannot use exact decimal fixed-point")
    return scale


def fetch_exchange_info(fetcher: Callable[[str], bytes] | None = None) -> dict:
    if fetcher is None:
        def fetcher(url: str) -> bytes:
            request = Request(url, headers={"User-Agent": "Axodus-Quants-Lab/1"})
            with urlopen(request, timeout=15) as response:
                return response.read()
    return json.loads(fetcher(BINANCE_FUTURES_EXCHANGE_INFO))


def normalize_instrument_spec(symbol: str, exchange_info: dict, retrieved_at: str | None = None) -> InstrumentSpec:
    row = next((item for item in exchange_info.get("symbols", []) if item.get("symbol") == symbol), None)
    if row is None:
        raise KeyError(f"AUTHORITATIVE_METADATA_UNAVAILABLE: {symbol}")
    filters = {item["filterType"]: item for item in row.get("filters", [])}
    price_filter = filters.get("PRICE_FILTER")
    lot_filter = filters.get("LOT_SIZE")
    if not price_filter or not lot_filter:
        raise ValueError(f"INCOMPLETE_INSTRUMENT_METADATA: {symbol}")
    tick = Decimal(price_filter["tickSize"])
    step = Decimal(lot_filter["stepSize"])
    min_notional_row = filters.get("MIN_NOTIONAL") or filters.get("NOTIONAL") or {}
    min_notional_value = min_notional_row.get("notional") or min_notional_row.get("minNotional")
    canonical_row = json.dumps(row, sort_keys=True, separators=(",", ":")).encode()
    timestamp = retrieved_at or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    return InstrumentSpec(
        symbol=symbol, venue="Binance USD-M Futures", market_type=row.get("contractType", "USD-M Futures"),
        tick_size=tick, step_size=step, price_scale=_scale(tick), quantity_scale=_scale(step),
        price_precision=_precision(tick), quantity_precision=_precision(step),
        metadata_source="Binance Futures exchangeInfo", metadata_timestamp=timestamp,
        metadata_hash=hashlib.sha256(canonical_row).hexdigest(), base_asset=row.get("baseAsset", ""),
        quote_asset=row.get("quoteAsset", ""), settlement_asset=row.get("marginAsset", row.get("quoteAsset", "")),
        min_quantity=Decimal(lot_filter["minQty"]),
        min_notional=Decimal(min_notional_value) if min_notional_value is not None else None,
        contract_size=Decimal("1"), fee_classification="UNKNOWN / VENUE ACCOUNT DEPENDENT",
    )


def resolve_and_freeze(symbol: str, output_root: Path, fetcher: Callable[[str], bytes] | None = None) -> InstrumentSpec:
    spec = normalize_instrument_spec(symbol, fetch_exchange_info(fetcher))
    target = Path(output_root) / "instrument-specs" / f"{symbol}.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(".tmp")
    temporary.write_text(json.dumps(spec.to_manifest(), indent=2, sort_keys=True) + "\n")
    temporary.replace(target)
    InstrumentSpec.register(spec)
    return spec
