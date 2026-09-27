"""
InstrumentSpec — authoritative instrument contract for multi-symbol replay.

Every symbol that enters the canonical frame builder must have a frozen InstrumentSpec.
The spec carries exact-decimal scale factors used by the fixed-point kernel;
it must never be inferred from market data at replay time.
"""
from __future__ import annotations
import hashlib
import json
from dataclasses import dataclass, asdict
from decimal import Decimal
from decimal import localcontext
from pathlib import Path
from typing import ClassVar


def _decimal_scale(increment: Decimal) -> int:
    """Return an exact power-of-ten scale for integer raw price/quantity units."""
    normalized = increment.normalize()
    scale = 10 ** max(0, -normalized.as_tuple().exponent)
    with localcontext() as context:
        context.prec = max(50, len(increment.as_tuple().digits) + abs(increment.as_tuple().exponent) + 8)
        scaled = increment * scale
    if scaled != scaled.to_integral_value():
        raise ValueError("increment must be exactly representable in base-10 fixed-point units")
    return scale


@dataclass(frozen=True)
class InstrumentSpec:
    """Authoritative venue/instrument contract for one trading pair.

    price_scale and quantity_scale are the integer multipliers used by the
    C++ kernel to represent price and quantity as int64 ticks and steps
    respectively:

        tick   = round(price_raw * price_scale)
        step   = round(qty_raw   * quantity_scale)

    price_precision and quantity_precision are the number of decimal places
    accepted by the provider Parquet schema for price and quantity columns.
    They are used only for validation; the scale factors are authoritative.
    """

    symbol: str
    venue: str
    market_type: str        # e.g. "USD-M Futures"
    tick_size: Decimal       # minimum price increment (Decimal exact)
    step_size: Decimal       # minimum quantity increment (Decimal exact)
    price_scale: int         # base-10 multiplier preserving the tick decimal exactly
    quantity_scale: int      # base-10 multiplier preserving the step decimal exactly
    price_precision: int     # Parquet decimal places for price
    quantity_precision: int  # Parquet decimal places for quantity
    metadata_source: str
    metadata_timestamp: str  # ISO 8601 UTC
    metadata_hash: str       # SHA-256 of the freeze JSON
    base_asset: str = ""
    quote_asset: str = ""
    settlement_asset: str = ""
    min_quantity: Decimal | None = None
    min_notional: Decimal | None = None
    contract_size: Decimal | None = None
    maker_fee: Decimal | None = None
    taker_fee: Decimal | None = None
    fee_classification: str = "UNKNOWN"

    # Registry of known instruments.  Research code may resolve a symbol
    # without network access by calling InstrumentSpec.from_registry(symbol).
    _REGISTRY: ClassVar[dict[str, "InstrumentSpec"]] = {}

    # ------------------------------------------------------------------ #
    # Construction                                                          #
    # ------------------------------------------------------------------ #

    @classmethod
    def register(cls, spec: "InstrumentSpec") -> None:
        spec.validate()
        cls._REGISTRY[spec.symbol] = spec

    @classmethod
    def from_manifest(cls, payload: dict) -> "InstrumentSpec":
        """Load a frozen JSON-safe instrument contract and verify its hash."""
        spec = cls(
            symbol=payload["symbol"], venue=payload["venue"], market_type=payload["marketType"],
            tick_size=Decimal(payload["tickSize"]), step_size=Decimal(payload["stepSize"]),
            price_scale=int(payload["priceScale"]), quantity_scale=int(payload["quantityScale"]),
            price_precision=int(payload["pricePrecision"]), quantity_precision=int(payload["quantityPrecision"]),
            metadata_source=payload["metadataSource"], metadata_timestamp=payload["metadataTimestamp"],
            metadata_hash=payload["metadataHash"], base_asset=payload.get("baseAsset", ""),
            quote_asset=payload.get("quoteAsset", ""), settlement_asset=payload.get("settlementAsset", ""),
            min_quantity=Decimal(payload["minQuantity"]) if payload.get("minQuantity") is not None else None,
            min_notional=Decimal(payload["minNotional"]) if payload.get("minNotional") is not None else None,
            contract_size=Decimal(payload["contractSize"]) if payload.get("contractSize") is not None else None,
            maker_fee=Decimal(payload["makerFee"]) if payload.get("makerFee") is not None else None,
            taker_fee=Decimal(payload["takerFee"]) if payload.get("takerFee") is not None else None,
            fee_classification=payload.get("feeClassification", "UNKNOWN"),
        )
        spec.validate()
        expected = payload.get("instrumentSpecHash")
        if expected and expected != spec.canonical_hash():
            raise ValueError("InstrumentSpec hash mismatch")
        return spec

    @classmethod
    def load(cls, path: Path) -> "InstrumentSpec":
        return cls.from_manifest(json.loads(Path(path).read_text()))

    @classmethod
    def from_registry(cls, symbol: str) -> "InstrumentSpec":
        if symbol not in cls._REGISTRY:
            raise KeyError(
                f"InstrumentSpec for {symbol!r} not found in registry. "
                "Acquire and freeze metadata before building frames."
            )
        return cls._REGISTRY[symbol]

    @classmethod
    def is_registered(cls, symbol: str) -> bool:
        return symbol in cls._REGISTRY

    # ------------------------------------------------------------------ #
    # Validation helpers                                                    #
    # ------------------------------------------------------------------ #

    def validate(self) -> None:
        """Raise ValueError on internal consistency failures."""
        if self.tick_size <= 0 or self.step_size <= 0:
            raise ValueError("tick_size and step_size must be positive")
        expected_price_scale = _decimal_scale(self.tick_size)
        expected_qty_scale = _decimal_scale(self.step_size)
        if self.price_scale != expected_price_scale:
            raise ValueError(
                f"price_scale {self.price_scale} inconsistent with "
                f"tick_size {self.tick_size} (expected {expected_price_scale})"
            )
        if self.quantity_scale != expected_qty_scale:
            raise ValueError(
                f"quantity_scale {self.quantity_scale} inconsistent with "
                f"step_size {self.step_size} (expected {expected_qty_scale})"
            )
        if self.price_scale <= 0 or self.quantity_scale <= 0:
            raise ValueError("price_scale and quantity_scale must be positive")
        if self.price_precision < 0 or self.quantity_precision < 0:
            raise ValueError("price and quantity precision must be non-negative")
        if self.min_quantity is not None and self.min_quantity <= 0:
            raise ValueError("min_quantity must be positive")
        if self.min_notional is not None and self.min_notional <= 0:
            raise ValueError("min_notional must be positive")

    def spread_ticks(self, best_bid: Decimal, best_ask: Decimal) -> Decimal:
        """Spread expressed in ticks (exact decimal)."""
        return (best_ask - best_bid) / self.tick_size

    def price_from_ticks(self, ticks: int) -> Decimal:
        return Decimal(ticks) / self.price_scale

    def quantity_from_steps(self, steps: int) -> Decimal:
        return Decimal(steps) / self.quantity_scale

    def price_to_ticks(self, raw_price: Decimal | str) -> int:
        value = Decimal(raw_price)
        fixed = value * self.price_scale
        if fixed != fixed.to_integral_value():
            raise ValueError(f"price is not representable at fixed scale {self.price_scale}")
        tick_units = self.tick_size * self.price_scale
        if fixed % tick_units != 0:
            raise ValueError(f"price is not aligned to tick_size {self.tick_size}")
        return int(fixed)

    def quantity_to_steps(self, raw_quantity: Decimal | str) -> int:
        value = Decimal(raw_quantity)
        fixed = value * self.quantity_scale
        if fixed != fixed.to_integral_value():
            raise ValueError(f"quantity is not representable at fixed scale {self.quantity_scale}")
        step_units = self.step_size * self.quantity_scale
        if fixed % step_units != 0:
            raise ValueError(f"quantity is not aligned to step_size {self.step_size}")
        return int(fixed)

    def canonical_hash(self) -> str:
        """Hash the authoritative fields, excluding the self-referential hash."""
        payload = asdict(self)
        payload.pop("metadata_hash", None)
        canonical = json.dumps(payload, sort_keys=True, default=str, separators=(",", ":"))
        return hashlib.sha256(canonical.encode()).hexdigest()

    def to_manifest(self) -> dict[str, str | int]:
        """Return a JSON-safe frozen instrument contract for run manifests."""
        return {
            "symbol": self.symbol,
            "venue": self.venue,
            "marketType": self.market_type,
            "tickSize": str(self.tick_size),
            "stepSize": str(self.step_size),
            "priceScale": self.price_scale,
            "quantityScale": self.quantity_scale,
            "pricePrecision": self.price_precision,
            "quantityPrecision": self.quantity_precision,
            "metadataSource": self.metadata_source,
            "metadataTimestamp": self.metadata_timestamp,
            "metadataHash": self.metadata_hash,
            "baseAsset": self.base_asset,
            "quoteAsset": self.quote_asset,
            "settlementAsset": self.settlement_asset,
            "minQuantity": None if self.min_quantity is None else str(self.min_quantity),
            "minNotional": None if self.min_notional is None else str(self.min_notional),
            "contractSize": None if self.contract_size is None else str(self.contract_size),
            "makerFee": None if self.maker_fee is None else str(self.maker_fee),
            "takerFee": None if self.taker_fee is None else str(self.taker_fee),
            "feeClassification": self.fee_classification,
            "instrumentSpecHash": self.canonical_hash(),
        }


# ------------------------------------------------------------------ #
# Built-in known instruments (extend as needed)                         #
# ------------------------------------------------------------------ #

_BTCUSDT = InstrumentSpec(
    symbol="BTCUSDT",
    venue="Binance USD-M Futures",
    market_type="USD-M Futures",
    tick_size=Decimal("0.10"),
    step_size=Decimal("0.001"),
    price_scale=10,
    quantity_scale=1000,
    price_precision=2,
    quantity_precision=3,
    metadata_source="Binance exchangeInfo (qualified 2026-06-23/2026-07-06)",
    metadata_timestamp="2026-06-23T00:00:00Z",
    metadata_hash="e94672207d220535323d8408104331558ea0c8cb9b1d217414687b03a516ec6d",
    base_asset="BTC", quote_asset="USDT", settlement_asset="USDT",
    min_quantity=Decimal("0.001"), maker_fee=Decimal("0.0002"), taker_fee=Decimal("0.0005"),
    fee_classification="CONSERVATIVE_ASSUMPTION",
)
_BTCUSDT.validate()
InstrumentSpec.register(_BTCUSDT)

_ETHUSDC = InstrumentSpec(
    symbol="ETHUSDC",
    venue="Binance USD-M Futures",
    market_type="USD-M Futures",
    tick_size=Decimal("0.01"),
    step_size=Decimal("0.001"),
    price_scale=100,
    quantity_scale=1000,
    price_precision=2,
    quantity_precision=3,
    metadata_source="Binance exchangeInfo (qualified 2026-06-23/2026-07-06)",
    metadata_timestamp="2026-06-23T00:00:00Z",
    metadata_hash="3c099160f6f3602d7397c98affd78b496235366b2dcaa2150cdf6c8e810c768c",
    base_asset="ETH", quote_asset="USDC", settlement_asset="USDC",
    min_quantity=Decimal("0.001"), maker_fee=Decimal("0"), taker_fee=Decimal("0.0004"),
    fee_classification="CONSERVATIVE_ASSUMPTION / PROMO_BASELINE",
)
_ETHUSDC.validate()
InstrumentSpec.register(_ETHUSDC)
