"""Bounded deterministic executor for Trinity-facing order-flow research actions.

The executor accepts only ``ResearchActionKind`` requests that have passed
``ResearchCapabilityBoundary``.  It has no shell, provider-trading, credential,
or runtime-installation capability.  Handlers are injected by Axodus-owned code
and their compact state is persisted beneath the dedicated market-data root.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .instrument_spec import InstrumentSpec
from .research_capabilities import (
    APPROVED_DATA_ROOT,
    APPROVED_VENV,
    ResearchActionKind,
    ResearchCapabilityBoundary,
)
from .research_capabilities import SECRET_ARGUMENT_NAMES


Handler = Callable[[dict[str, Any]], dict[str, Any]]


@dataclass(frozen=True)
class RuntimePreflight:
    data_root: str
    runtime: str
    runtime_executable: str | None
    runtime_prefix: str | None
    available_capacity_bytes: int
    pyarrow_available: bool
    cryptohftdata_available: bool
    zstandard_available: bool

    @property
    def runtime_matches_expected(self) -> bool:
        return self.runtime_executable == self.runtime and self.runtime_prefix == str(Path(self.runtime).parent.parent)

    @property
    def available(self) -> bool:
        return (
            Path(self.data_root).is_dir()
            and Path(self.runtime).is_file()
            and self.runtime_matches_expected
            and self.pyarrow_available
            and self.cryptohftdata_available
            and self.zstandard_available
        )

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), "available": self.available}


class BoundedResearchExecutor:
    """Executes only pre-authorized typed research actions.

    This is intentionally an application-level boundary: a caller cannot pass a
    command string, arbitrary path, or provider credential through it.  The
    action result is persisted with zero trading-mutation counters, enabling a
    later caller to resume or inspect state without conversational memory.
    """

    def __init__(
        self,
        data_root: Path = APPROVED_DATA_ROOT,
        runtime: Path = APPROVED_VENV / "bin" / "python",
        state_root: Path | None = None,
        boundary: ResearchCapabilityBoundary | None = None,
        handlers: dict[ResearchActionKind, Handler] | None = None,
    ) -> None:
        self.data_root = Path(data_root).resolve()
        # Keep the venv entrypoint itself. Resolving it follows the Python
        # symlink to the base interpreter and silently drops venv packages.
        self.runtime = Path(runtime)
        self.state_root = (state_root or self.data_root / "manifests" / "trinity-research").resolve()
        self.boundary = boundary or ResearchCapabilityBoundary()
        self.handlers = handlers or {}

    def preflight(self) -> RuntimePreflight:
        if not self.data_root.is_dir() or not self.runtime.is_file():
            return RuntimePreflight(
                data_root=str(self.data_root), runtime=str(self.runtime), runtime_executable=None,
                runtime_prefix=None, available_capacity_bytes=0,
                pyarrow_available=False, cryptohftdata_available=False, zstandard_available=False,
            )
        available_capacity = shutil.disk_usage(self.data_root).free
        dependency_probe = subprocess.run(
            [str(self.runtime), "-c", "import importlib.util,json,sys; print(json.dumps({'executable': sys.executable, 'prefix': sys.prefix, 'dependencies': {n: importlib.util.find_spec(n) is not None for n in ['pyarrow','cryptohftdata','zstandard']}}))"],
            check=False, capture_output=True, text=True, timeout=15,
        )
        dependencies = {}
        runtime_executable = None
        runtime_prefix = None
        if dependency_probe.returncode == 0:
            try:
                probe = json.loads(dependency_probe.stdout.strip())
                runtime_executable = probe.get("executable")
                runtime_prefix = probe.get("prefix")
                dependencies = probe.get("dependencies", {})
            except json.JSONDecodeError:
                dependencies = {}
        return RuntimePreflight(
            data_root=str(self.data_root),
            runtime=str(self.runtime),
            runtime_executable=runtime_executable,
            runtime_prefix=runtime_prefix,
            available_capacity_bytes=available_capacity,
            pyarrow_available=bool(dependencies.get("pyarrow")),
            cryptohftdata_available=bool(dependencies.get("cryptohftdata")),
            zstandard_available=bool(dependencies.get("zstandard")),
        )

    def execute(self, kind: str | ResearchActionKind, arguments: dict[str, Any]) -> dict[str, Any]:
        """Authorize, execute an injected bounded handler, and persist a receipt."""
        authorization = self.boundary.authorize(kind, arguments)
        if not authorization.authorized:
            self._persist_receipt(str(kind), arguments, authorization.reason, "REJECTED", {})
            raise PermissionError(authorization.reason)

        action = ResearchActionKind(kind)
        self._validate_paths(action, arguments)
        self._validate_runtime_if_needed(action)
        handler = self.handlers.get(action)
        if handler is None:
            reason = f"ACTION_HANDLER_UNAVAILABLE: {action.value} has no Axodus-owned bounded handler"
            self._persist_receipt(action.value, arguments, reason, "BLOCKED", {})
            raise RuntimeError(reason)

        result = handler(dict(arguments))
        if not isinstance(result, dict):
            raise TypeError("bounded research handler must return a dict")
        self._persist_receipt(action.value, arguments, "PASS", "COMPLETE", result)
        return result

    def read_status(self, receipt_id: str) -> dict[str, Any]:
        path = self.state_root / f"{receipt_id}.json"
        if not path.is_file():
            raise FileNotFoundError(f"RUN_STATUS_NOT_FOUND: {receipt_id}")
        return json.loads(path.read_text())

    def _validate_runtime_if_needed(self, action: ResearchActionKind) -> None:
        if action in {
            ResearchActionKind.GET_RUN_STATUS,
            ResearchActionKind.READ_CANONICAL_RESULTS,
        }:
            return
        current_executable = str(Path(sys.executable))
        expected_runtime = str(self.runtime)
        if current_executable != expected_runtime:
            raise RuntimeError(
                f"EXPECTED_RUNTIME_MISMATCH: current={current_executable} expected={expected_runtime}"
            )
        preflight = self.preflight()
        if not preflight.available:
            if not preflight.runtime_matches_expected:
                raise RuntimeError(
                    "EXPECTED_RUNTIME_MISMATCH: "
                    f"resolved={preflight.runtime_executable} expected={expected_runtime}"
                )
            raise RuntimeError("RUNTIME_ENVIRONMENT_UNAVAILABLE")

    def _validate_paths(self, action: ResearchActionKind, arguments: dict[str, Any]) -> None:
        for key in ("data_root", "dataset_root", "output_root", "manifest_path"):
            value = arguments.get(key)
            if value is None:
                continue
            candidate = Path(value).resolve()
            if not candidate.is_relative_to(self.data_root):
                raise PermissionError(f"PATH_OUTSIDE_APPROVED_DATA_ROOT: {key}")
        symbol = arguments.get("symbol")
        if symbol and action in {ResearchActionKind.INSPECT_SYMBOL, ResearchActionKind.BUILD_FRAMES}:
            if not InstrumentSpec.is_registered(symbol):
                raise PermissionError(f"MISSING_INSTRUMENT_SPEC: {symbol}")

    def _persist_receipt(
        self,
        action: str,
        arguments: dict[str, Any],
        reason: str,
        status: str,
        result: dict[str, Any],
    ) -> str:
        self.state_root.mkdir(parents=True, exist_ok=True)
        safe_arguments = _redact_secrets(arguments)
        canonical = json.dumps(
            {"action": action, "arguments": safe_arguments, "status": status, "result": result},
            sort_keys=True, default=str, separators=(",", ":"),
        )
        receipt_id = hashlib.sha256(canonical.encode()).hexdigest()[:24]
        receipt = {
            "receiptId": receipt_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "action": action,
            "arguments": safe_arguments,
            "status": status,
            "reason": reason,
            "result": result,
            "runtime": str(self.runtime),
            "dataRoot": str(self.data_root),
            "exchangeOrderMutations": 0,
            "testnetMutations": 0,
            "mainnetMutations": 0,
            "realCapital": 0,
            "secretsExposed": False,
        }
        temporary = self.state_root / f".{receipt_id}.{os.getpid()}.tmp"
        destination = self.state_root / f"{receipt_id}.json"
        temporary.write_text(json.dumps(receipt, sort_keys=True, indent=2, default=str) + "\n")
        temporary.replace(destination)
        return receipt_id


def _redact_secrets(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: ("[REDACTED]" if str(key).lower() in SECRET_ARGUMENT_NAMES else _redact_secrets(child))
            for key, child in value.items()
        }
    if isinstance(value, list):
        return [_redact_secrets(child) for child in value]
    if isinstance(value, tuple):
        return [_redact_secrets(child) for child in value]
    return value
