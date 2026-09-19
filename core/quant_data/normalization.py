"""Historical source and deterministic candle normalization boundaries."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable, Mapping, Protocol

from .models import CanonicalHistoricalRecord


class HistoricalSourceAdapter(Protocol):
    def read(self) -> Iterable[Mapping[str, Any]]: ...


class InMemoryHistoricalSource:
    def __init__(self, records: Iterable[Mapping[str, Any]]) -> None:
        self._records = tuple(records)

    def read(self) -> Iterable[Mapping[str, Any]]:
        return iter(self._records)


def _timestamp(value: Any, field_name: str) -> str:
    if isinstance(value, (int, float)):
        parsed = datetime.fromtimestamp(float(value) / 1000, tz=timezone.utc)
    elif isinstance(value, str):
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError(f"{field_name} must include timezone")
        parsed = parsed.astimezone(timezone.utc)
    else:
        raise ValueError(f"{field_name} must be an ISO timestamp or epoch milliseconds")
    return parsed.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _number(value: Any, field_name: str) -> str:
    try:
        decimal = Decimal(str(value))
    except (InvalidOperation, ValueError) as error:
        raise ValueError(f"{field_name} must be numeric") from error
    if not decimal.is_finite():
        raise ValueError(f"{field_name} must be finite")
    normalized = format(decimal.normalize(), "f")
    return "0" if normalized in {"-0", ""} else normalized


class CandleNormalizer:
    """Normalize common candle payloads into the provider-neutral observation shape."""

    normalizer_version = "candle-normalizer-v1"
    schema_version = "market-observation-candle-v1"

    def normalize(self, raw: Mapping[str, Any]) -> CanonicalHistoricalRecord:
        instrument = raw.get("instrument", raw.get("trading_pair", raw.get("symbol")))
        venue = raw.get("venue", raw.get("connector", raw.get("connector_name")))
        observation_id = raw.get("observationId", raw.get("observation_id", raw.get("id")))
        event_time = raw.get("eventTime", raw.get("event_time", raw.get("timestamp")))
        if not all(isinstance(value, str) and value.strip() for value in (instrument, venue, observation_id)):
            raise ValueError("instrument, venue and observation id are required")
        event = _timestamp(event_time, "event_time")
        observed = _timestamp(raw.get("observedAt", raw.get("observed_at", event)), "observed_at")
        received = _timestamp(raw.get("receivedAt", raw.get("received_at", observed)), "received_at")
        values = {name: _number(raw[name], name) for name in ("open", "high", "low", "close")}
        if "volume" in raw:
            values["volume"] = _number(raw["volume"], "volume")
        if "interval" in raw:
            values["interval"] = str(raw["interval"])
        return CanonicalHistoricalRecord(
            observation_id=observation_id,
            instrument=instrument,
            venue=venue,
            observation_type="candle",
            event_time=event,
            observed_at=observed,
            received_at=received,
            value=values,
            source_ref=str(raw.get("sourceRef", raw.get("source_ref", f"source:{venue}:{observation_id}"))),
            schema_version=self.schema_version,
            sequence=raw.get("sequence"),
            sequence_scope=raw.get("sequenceScope", raw.get("sequence_scope")),
        )
