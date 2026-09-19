"""Canonical serialization shared by identity and provenance calculations."""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timezone
from enum import Enum
from typing import Any


def _normalize(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (datetime, date)):
        if isinstance(value, datetime) and value.tzinfo is None:
            raise ValueError("naive datetimes are not valid canonical values")
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z") if isinstance(value, datetime) else value.isoformat()
    if hasattr(value, "to_canonical_dict"):
        return _normalize(value.to_canonical_dict())
    if hasattr(value, "to_dict"):
        return _normalize(value.to_dict())
    if isinstance(value, dict):
        return {str(key): _normalize(item) for key, item in value.items() if item is not None}
    if isinstance(value, (list, tuple)):
        return [_normalize(item) for item in value]
    return value


def canonical_json(value: Any) -> str:
    """Return compact JSON with sorted object keys and stable array ordering."""

    return json.dumps(
        _normalize(value),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        allow_nan=False,
    )


def sha256_digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()
