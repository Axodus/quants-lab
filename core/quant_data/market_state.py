"""Historical MarketState reconstruction without a parallel domain contract."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable, Mapping

from .models import CanonicalHistoricalRecord

VALID_VALIDITY_CONTEXTS = {"live", "historical", "simulation", "paper", "testnet"}
VALID_FRESHNESS = {"current", "stale", "historical", "unknown"}
VALID_COMPLETENESS = {"complete", "partial", "unknown"}


class LookAheadViolation(ValueError):
    pass


class MarketStateValidationError(ValueError):
    """Raised when a MarketState dictionary violates the canonical schema."""


def _parse(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("historical timestamps must include timezone")
    return parsed.astimezone(timezone.utc)


def validate_market_state_schema(market_state: Mapping[str, Any]) -> None:
    """Validate that a MarketState dictionary strictly conforms to the cross-EPIC SCF contract."""
    if not isinstance(market_state, Mapping):
        raise MarketStateValidationError("MarketState must be a mapping/dict")

    required_string_fields = [
        "marketStateId",
        "instrument",
        "timeframe",
        "marketTime",
        "observedAt",
        "constructedAt",
        "validityContext",
        "freshness",
        "schemaVersion",
        "completeness",
    ]
    for field in required_string_fields:
        val = market_state.get(field)
        if not isinstance(val, str) or not val.strip():
            raise MarketStateValidationError(f"Field '{field}' must be a non-empty string, got: {val!r}")

    # Validate timestamps parse properly
    for ts_field in ["marketTime", "observedAt", "constructedAt"]:
        try:
            _parse(market_state[ts_field])
        except Exception as exc:
            raise MarketStateValidationError(f"Timestamp field '{ts_field}' is invalid: {exc}") from exc

    if market_state["validityContext"] not in VALID_VALIDITY_CONTEXTS:
        raise MarketStateValidationError(f"Invalid validityContext: {market_state['validityContext']!r}")

    if market_state["freshness"] not in VALID_FRESHNESS:
        raise MarketStateValidationError(f"Invalid freshness: {market_state['freshness']!r}")

    if market_state["validityContext"] == "live" and market_state["freshness"] != "current":
        raise MarketStateValidationError("Live MarketState freshness must be 'current'")

    if market_state["completeness"] not in VALID_COMPLETENESS:
        raise MarketStateValidationError(f"Invalid completeness: {market_state['completeness']!r}")

    # Validate features array
    features = market_state.get("features")
    if not isinstance(features, list):
        raise MarketStateValidationError("Field 'features' must be a list")

    for idx, feat in enumerate(features):
        if not isinstance(feat, Mapping):
            raise MarketStateValidationError(f"features[{idx}] must be a mapping/dict")
        for feat_str_field in ["featureId", "featureVersion", "computedAt"]:
            f_val = feat.get(feat_str_field)
            if not isinstance(f_val, str) or not f_val.strip():
                raise MarketStateValidationError(f"features[{idx}].{feat_str_field} must be a non-empty string")
        if "value" not in feat:
            raise MarketStateValidationError(f"features[{idx}] missing required 'value' key")

    # Validate sourceRefs and validationRefs
    for list_field in ["sourceRefs", "validationRefs"]:
        items = market_state.get(list_field)
        if not isinstance(items, (list, tuple)):
            raise MarketStateValidationError(f"Field '{list_field}' must be a list/tuple")
        for idx, item in enumerate(items):
            if not isinstance(item, str) or not item.strip():
                raise MarketStateValidationError(f"{list_field}[{idx}] must be a non-empty string")


def reconstruct_historical_market_state(
    record: CanonicalHistoricalRecord,
    dataset_reference: Mapping[str, Any],
    features: Iterable[Mapping[str, Any]] = (),
    *,
    construction_time: str | None = None,
    construction_version: str = "historical-market-state-v1",
) -> dict[str, Any]:
    event_time = _parse(record.event_time)
    observed_at = _parse(record.observed_at)
    features_list = list(features)
    for feature in features_list:
        computed_at = _parse(str(feature["computedAt"]))
        if computed_at > observed_at:
            raise LookAheadViolation(f"feature {feature.get('featureId')} computed after observation availability")
    constructed = construction_time or record.observed_at
    _parse(constructed)
    state = {
        "marketStateId": f"historical:{dataset_reference['datasetId']}@{dataset_reference['datasetVersion']}:{record.observation_id}",
        "instrument": record.instrument,
        "venue": record.venue,
        "timeframe": str(record.value.get("interval", "unknown")),
        "marketTime": record.event_time,
        "observedAt": record.observed_at,
        "constructedAt": constructed,
        "validityContext": "historical",
        "freshness": "historical",
        "schemaVersion": "1.0.0",
        "features": features_list,
        "sourceRefs": [record.source_ref, f"dataset:{dataset_reference['datasetId']}@{dataset_reference['datasetVersion']}"],
        "completeness": "complete" if record.quality == "valid" else "partial",
        "validationRefs": ["historical-causality-v1"],
        "observationRefs": [record.observation_id],
        "constructionVersion": construction_version,
        "qualityStatus": record.quality,
        "sequenceStatus": "sequenced" if record.sequence is not None else "not-provided",
    }
    validate_market_state_schema(state)
    return state
