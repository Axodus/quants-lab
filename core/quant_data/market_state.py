"""Historical MarketState reconstruction without a parallel domain contract."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable, Mapping

from .models import CanonicalHistoricalRecord


class LookAheadViolation(ValueError):
    pass


def _parse(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("historical timestamps must include timezone")
    return parsed.astimezone(timezone.utc)


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
    for feature in features:
        computed_at = _parse(str(feature["computedAt"]))
        if computed_at > observed_at:
            raise LookAheadViolation(f"feature {feature.get('featureId')} computed after observation availability")
    constructed = construction_time or record.observed_at
    _parse(constructed)
    return {
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
        "features": list(features),
        "sourceRefs": [record.source_ref, f"dataset:{dataset_reference['datasetId']}@{dataset_reference['datasetVersion']}"],
        "completeness": "complete" if record.quality == "valid" else "partial",
        "validationRefs": ["historical-causality-v1"],
        "observationRefs": [record.observation_id],
        "constructionVersion": construction_version,
        "qualityStatus": record.quality,
        "sequenceStatus": "sequenced" if record.sequence is not None else "not-provided",
    }
