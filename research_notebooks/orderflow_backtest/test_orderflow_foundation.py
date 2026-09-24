import json
import sys
from decimal import Decimal
from pathlib import Path

import pytest

from orderflow_backtest.historical_data import DatasetValidationError, classify_sequence_quality, load_manifest
from orderflow_backtest.orderflow_contracts import OrderFlowFrameV1, SourceStrategyConfigV1
from orderflow_backtest.source_equivalent_adapters import ADAPTERS


SOURCE_ROOT = Path("/home/mzfshark/.openclaw/workspace/strategies/orderflow")
sys.path.insert(0, str(SOURCE_ROOT.parent.parent))


def frame(price, buy, sell, obi="0.8", spread="0"):
    return OrderFlowFrameV1(1, Decimal(price), Decimal(buy), Decimal(sell), Decimal(obi), Decimal(spread))


def test_manifest_rejects_current_synthetic_dataset():
    with pytest.raises(DatasetValidationError):
        load_manifest(Path(__file__).with_name("dataset_manifest.json"))


def test_sequence_classification_is_fail_closed():
    assert classify_sequence_quality({"continuity": True, "gaps": []}) == "NO_GAP"
    assert classify_sequence_quality({"continuity": False, "gaps": [{"from": 1, "to": 2}]}) == "UNRECOVERABLE_GAP"
    assert classify_sequence_quality({}) == "UNKNOWN_SEQUENCE_STATE"


def test_canonical_frame_uses_decimal_arithmetic():
    current = frame("101", "12", "3")
    assert current.delta == Decimal("9")
    assert isinstance(current.delta, Decimal)


def test_source_equivalent_adapters_are_explicitly_provider_neutral():
    assert set(ADAPTERS) == {
        "orderflow.momentum.aggression",
        "orderflow.absorption.fade",
        "orderflow.cvd.divergence.reversal",
    }
    assert SourceStrategyConfigV1().tick_size == Decimal("0.01")
