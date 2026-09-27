"""Causal pre-window bootstrap resolution for symbol-specific replays.

The resolver consumes already grouped canonical L2 events.  It deliberately
does not infer a book from a snapshot that occurs after the requested start.
That snapshot can establish only a partial causal window.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable


BOOTSTRAP_QUALIFIED = "BOOTSTRAP_QUALIFIED"
BOOTSTRAP_PARTIAL = "BOOTSTRAP_PARTIAL"
BOOTSTRAP_UNAVAILABLE = "BOOTSTRAP_UNAVAILABLE"
BOOTSTRAP_SEQUENCE_BROKEN = "BOOTSTRAP_SEQUENCE_BROKEN"


def _parse_time(value: str | int | float | datetime) -> datetime:
    if isinstance(value, datetime):
        result = value
    elif isinstance(value, (int, float)):
        # Canonical fixtures use milliseconds when numeric timestamps are used.
        result = datetime.fromtimestamp(value / 1000, tz=timezone.utc)
    else:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if result.tzinfo is None:
        result = result.replace(tzinfo=timezone.utc)
    return result.astimezone(timezone.utc)


def _event_time(event: dict[str, Any]) -> datetime:
    return _parse_time(event.get("event_time", event.get("eventTime")))


def _event_type(event: dict[str, Any]) -> str:
    return str(event.get("event_type", event.get("eventType", ""))).lower()


def _last_update(event: dict[str, Any]) -> int | None:
    value = event.get("last_update_id", event.get("lastUpdateId", event.get("final_update_id", event.get("final"))))
    return None if value in (None, "", -1) else int(value)


def _first_update(event: dict[str, Any]) -> int | None:
    value = event.get("first_update_id", event.get("firstUpdateId", event.get("first")))
    return None if value in (None, "", -1) else int(value)


def _final_update(event: dict[str, Any]) -> int | None:
    value = event.get("final_update_id", event.get("finalUpdateId", event.get("final")))
    return None if value in (None, "", -1) else int(value)


def _previous_final(event: dict[str, Any]) -> int | None:
    value = event.get("prev_final_update_id", event.get("prevFinalUpdateId", event.get("prev")))
    return None if value in (None, "", -1) else int(value)


def _bridges(previous_final: int | None, event: dict[str, Any]) -> bool:
    if previous_final is None or _event_type(event) == "snapshot":
        return True
    previous = _previous_final(event)
    first = _first_update(event)
    final = _final_update(event)
    if previous == previous_final:
        return True
    # Binance-style inclusive update ranges may overlap the previous ID.
    return first is not None and final is not None and first <= previous_final + 1 <= final


def _next_minute(value: datetime) -> datetime:
    if value.second == 0 and value.microsecond == 0:
        return value
    return value.replace(second=0, microsecond=0) + timedelta(minutes=1)


def expected_minute_frames(start: datetime, end: datetime) -> int:
    """Count minute-close frames from the first qualified minute through end."""
    start = _next_minute(start)
    end = _parse_time(end)
    if start > end:
        return 0
    return int((end.replace(second=0, microsecond=0) - start).total_seconds() // 60) + 1


@dataclass(frozen=True)
class QualifiedWindow:
    requested_start: str
    requested_end: str
    qualified_start: str | None
    qualified_end: str | None
    excluded_intervals: list[dict[str, str]] = field(default_factory=list)
    excluded_duration_seconds: int = 0
    exclusion_reason: str | None = None
    causal_coverage: float = 0.0
    expected_frames_for_qualified_window: int = 0


@dataclass(frozen=True)
class BootstrapResolution:
    symbol: str
    status: str
    snapshot: dict[str, Any] | None
    bridge: dict[str, Any]
    window: QualifiedWindow
    acquisition_request: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "status": self.status,
            "snapshot": self.snapshot,
            "bridge": self.bridge,
            "window": {
                "requestedWindow": {"start": self.window.requested_start, "end": self.window.requested_end},
                "qualifiedWindow": {"start": self.window.qualified_start, "end": self.window.qualified_end},
                "excludedIntervals": self.window.excluded_intervals,
                "excludedDurationSeconds": self.window.excluded_duration_seconds,
                "exclusionReason": self.window.exclusion_reason,
                "causalCoverage": self.window.causal_coverage,
                "expectedFramesForQualifiedWindow": self.window.expected_frames_for_qualified_window,
            },
            "acquisitionRequest": self.acquisition_request,
        }


class BootstrapResolver:
    """Resolve the latest valid snapshot and its canonical update bridge."""

    def resolve(
        self,
        symbol: str,
        events: Iterable[dict[str, Any]],
        requested_start: str,
        requested_end: str,
    ) -> BootstrapResolution:
        requested_start_dt = _parse_time(requested_start)
        requested_end_dt = _parse_time(requested_end)
        # ``events`` is already the qualified canonical event stream.  Preserve
        # its provider/sequence order; timestamp sorting could reorder same-time
        # updates and weaken the sequence authority used by the dataset gate.
        ordered = [dict(event) for event in events]
        snapshots_before = [
            event for event in ordered
            if _event_type(event) == "snapshot" and _event_time(event) < requested_start_dt
        ]
        snapshots_in_window = [
            event for event in ordered
            if _event_type(event) == "snapshot" and requested_start_dt <= _event_time(event) <= requested_end_dt
        ]

        if not snapshots_before and not snapshots_in_window:
            return self._unavailable(symbol, requested_start, requested_end)

        latest_before = snapshots_before[-1] if snapshots_before else None
        if latest_before is not None:
            bridge = self._bridge_from(ordered, latest_before, requested_end_dt)
            if bridge["status"] == "PASS":
                return self._full(symbol, latest_before, bridge, requested_start, requested_end)

        # A snapshot inside the request may establish a later, valid partial
        # interval, but it must never synthesize frames before its timestamp.
        for snapshot in snapshots_in_window:
            bridge = self._bridge_from(ordered, snapshot, requested_end_dt)
            if bridge["status"] != "PASS":
                continue
            qualified_start = _event_time(snapshot)
            return self._partial(symbol, snapshot, bridge, requested_start, requested_end, qualified_start)

        reason = "BOOTSTRAP_SEQUENCE_BROKEN" if latest_before is not None else "UNAVAILABLE_CAUSAL_BOOTSTRAP"
        acquisition = {
            "status": "BOOTSTRAP_ACQUISITION_REQUIRED",
            "symbol": symbol,
            "requested_start": requested_start,
            "search_before": requested_start,
            "required_datatype": "orderbook",
        }
        return BootstrapResolution(
            symbol=symbol,
            status=BOOTSTRAP_SEQUENCE_BROKEN if latest_before is not None else BOOTSTRAP_PARTIAL,
            snapshot=latest_before,
            bridge={"status": "FAIL", "reason": reason},
            window=self._window_without_start(requested_start, requested_end, None, reason),
            acquisition_request=acquisition,
        )

    def _bridge_from(self, ordered: list[dict[str, Any]], snapshot: dict[str, Any], end: datetime) -> dict[str, Any]:
        anchor = ordered.index(snapshot)
        previous_final = _last_update(snapshot)
        checked = 0
        for event in ordered[anchor + 1:]:
            if _event_time(event) > end:
                break
            if _event_type(event) == "snapshot":
                previous_final = _last_update(event)
                checked += 1
                continue
            if not _bridges(previous_final, event):
                return {"status": "FAIL", "reason": "SEQUENCE_GAP", "failedEvent": event, "eventsChecked": checked}
            previous_final = _final_update(event) or previous_final
            checked += 1
        return {"status": "PASS", "eventsChecked": checked, "lastUpdateId": previous_final}

    def _full(self, symbol: str, snapshot: dict[str, Any], bridge: dict[str, Any], start: str, end: str) -> BootstrapResolution:
        window = QualifiedWindow(start, end, start, end, causal_coverage=100.0, expected_frames_for_qualified_window=expected_minute_frames(_parse_time(start), _parse_time(end)))
        return BootstrapResolution(symbol, BOOTSTRAP_QUALIFIED, snapshot, bridge, window)

    def _partial(self, symbol: str, snapshot: dict[str, Any], bridge: dict[str, Any], start: str, end: str, qualified_start: datetime) -> BootstrapResolution:
        requested_start_dt = _parse_time(start)
        end_dt = _parse_time(end)
        excluded_seconds = max(0, int((qualified_start - requested_start_dt).total_seconds()))
        total_seconds = max(1, int((end_dt - requested_start_dt).total_seconds()) + 1)
        window = QualifiedWindow(
            start, end, qualified_start.isoformat().replace("+00:00", "Z"), end,
            excluded_intervals=[{"start": start, "end": qualified_start.isoformat().replace("+00:00", "Z")}],
            excluded_duration_seconds=excluded_seconds,
            exclusion_reason="UNAVAILABLE_CAUSAL_BOOTSTRAP",
            causal_coverage=round(max(0.0, 100.0 * (total_seconds - excluded_seconds) / total_seconds), 4),
            expected_frames_for_qualified_window=expected_minute_frames(qualified_start, end_dt),
        )
        return BootstrapResolution(symbol, BOOTSTRAP_PARTIAL, snapshot, bridge, window)

    def _window_without_start(self, start: str, end: str, qualified_start: datetime | None, reason: str) -> QualifiedWindow:
        end_dt = _parse_time(end)
        if qualified_start is None:
            return QualifiedWindow(start, end, None, None, exclusion_reason=reason)
        return self._partial("", None, {}, start, end, qualified_start).window

    @staticmethod
    def _unavailable(symbol: str, start: str, end: str) -> BootstrapResolution:
        return BootstrapResolution(
            symbol, BOOTSTRAP_UNAVAILABLE, None, {"status": "FAIL", "reason": "NO_SNAPSHOT_AVAILABLE"},
            QualifiedWindow(start, end, None, None, excluded_intervals=[{"start": start, "end": end}],
                            excluded_duration_seconds=max(0, int((_parse_time(end) - _parse_time(start)).total_seconds()) + 1),
                            exclusion_reason="UNAVAILABLE_CAUSAL_BOOTSTRAP"),
            {"status": "BOOTSTRAP_ACQUISITION_REQUIRED", "symbol": symbol, "requested_start": start,
             "search_before": start, "required_datatype": "orderbook"},
        )
