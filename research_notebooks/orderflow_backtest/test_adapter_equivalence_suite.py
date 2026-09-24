import sys
from decimal import Decimal
from pathlib import Path

import pytest

from orderflow_backtest.orderflow_contracts import OrderFlowFrameV1, SourceStrategyConfigV1
from orderflow_backtest.source_equivalent_adapters import (
    absorption_observe,
    divergence_observe,
    momentum_observe,
)

SOURCE_ROOT = Path("/home/mzfshark/.openclaw/workspace")
sys.path.insert(0, str(SOURCE_ROOT))
from strategies.orderflow import absorption as absorption_mod  # noqa: E402
from strategies.orderflow import divergence as divergence_mod  # noqa: E402
from strategies.orderflow import metrics as metrics_mod  # noqa: E402
from strategies.orderflow import momentum as momentum_mod  # noqa: E402


def make_frame(ts, price, buy, sell, obi, spread):
    return OrderFlowFrameV1(
        timestamp_ms=ts,
        price=Decimal(str(price)),
        buy=Decimal(str(buy)),
        sell=Decimal(str(sell)),
        obi=Decimal(str(obi)),
        spread_ticks=Decimal(str(spread)),
    )


def make_source_frame(ts, price, buy, sell, obi, spread):
    return metrics_mod.Frame(
        timestamp_ms=ts,
        price=Decimal(str(price)),
        buy=Decimal(str(buy)),
        sell=Decimal(str(sell)),
        obi=Decimal(str(obi)),
        spread_ticks=Decimal(str(spread)),
    )


def test_momentum_equivalence_across_positive_negative_and_boundary():
    cfg_source = metrics_mod.Config(window=3, depth=1, tick_size="1", slope_z="1.5", imbalance="0.65")
    cfg_adapter = SourceStrategyConfigV1(window=3, depth=1, tick_size=Decimal("1"), slope_z=Decimal("1.5"), imbalance=Decimal("0.65"))

    # Setup 3 warmup frames
    h_src = [
        make_source_frame(1000, "100", "4", "5", "0.0", "0"),
        make_source_frame(2000, "100", "5", "5", "0.0", "0"),
        make_source_frame(3000, "100", "6", "5", "0.0", "0"),
    ]
    h_adt = [
        make_frame(1000, "100", "4", "5", "0.0", "0"),
        make_frame(2000, "100", "5", "5", "0.0", "0"),
        make_frame(3000, "100", "6", "5", "0.0", "0"),
    ]
    cvds = [Decimal("-1"), Decimal("-1"), Decimal("0")]

    # Long trigger
    cur_src = make_source_frame(4000, "101", "20", "0", "0.8", "0")
    cur_adt = make_frame(4000, "101", "20", "0", "0.8", "0")
    sig_src, reason_src, _ = momentum_mod.observe(cur_src, h_src, cvds, Decimal("20"), cfg_source)
    sig_adt = momentum_observe(cur_adt, h_adt, cvds, Decimal("20"), cfg_adapter)
    assert sig_src == "LONG"
    assert sig_adt.side == "LONG"
    assert sig_adt.reason == reason_src

    # Short trigger
    cur_src_s = make_source_frame(4000, "99", "0", "20", "-0.8", "0")
    cur_adt_s = make_frame(4000, "99", "0", "20", "-0.8", "0")
    sig_src_s, reason_src_s, _ = momentum_mod.observe(cur_src_s, h_src, cvds, Decimal("-20"), cfg_source)
    sig_adt_s = momentum_observe(cur_adt_s, h_adt, cvds, Decimal("-20"), cfg_adapter)
    assert sig_src_s == "SHORT"
    assert sig_adt_s.side == "SHORT"
    assert sig_adt_s.reason == reason_src_s

    # Boundary failure: insufficient OBI
    cur_src_b = make_source_frame(4000, "101", "20", "0", "0.5", "0")
    cur_adt_b = make_frame(4000, "101", "20", "0", "0.5", "0")
    sig_src_b, _, _ = momentum_mod.observe(cur_src_b, h_src, cvds, Decimal("20"), cfg_source)
    sig_adt_b = momentum_observe(cur_adt_b, h_adt, cvds, Decimal("20"), cfg_adapter)
    assert sig_src_b == "NO_SIGNAL"
    assert sig_adt_b.side == "NO_SIGNAL"


def test_absorption_equivalence_across_positive_and_rejections():
    cfg_source = metrics_mod.Config(window=3, depth=1, tick_size="1", volume_multiple="2.5", aggression_fraction="0.7", confirmation_delta="1")
    cfg_adapter = SourceStrategyConfigV1(window=3, depth=1, tick_size=Decimal("1"), volume_multiple=Decimal("2.5"), aggression_fraction=Decimal("0.7"), confirmation_delta=Decimal("1"))

    # Candidate spike on frame 4
    h_src = [
        make_source_frame(1000, "100", "5", "5", "0.0", "0"),
        make_source_frame(2000, "100", "5", "5", "0.0", "0"),
        make_source_frame(3000, "100", "5", "5", "0.0", "0"),
        make_source_frame(4000, "100", "0", "50", "-0.8", "0"),  # spike sell
    ]
    h_adt = [
        make_frame(1000, "100", "5", "5", "0.0", "0"),
        make_frame(2000, "100", "5", "5", "0.0", "0"),
        make_frame(3000, "100", "5", "5", "0.0", "0"),
        make_frame(4000, "100", "0", "50", "-0.8", "0"),
    ]
    cvds = [Decimal("0"), Decimal("0"), Decimal("0"), Decimal("-50")]

    # Long trigger on frame 5 (counter buying + price higher)
    cur_src = make_source_frame(5000, "101", "10", "0", "0.8", "0")
    cur_adt = make_frame(5000, "101", "10", "0", "0.8", "0")
    sig_src, reason_src, _ = absorption_mod.observe(cur_src, h_src, cvds, Decimal("-40"), cfg_source)
    sig_adt = absorption_observe(cur_adt, h_adt, cvds, Decimal("-40"), cfg_adapter)
    assert sig_src == "LONG"
    assert sig_adt.side == "LONG"
    assert sig_adt.reason == reason_src

    # Rejection: price does not exceed candidate price
    cur_src_rej = make_source_frame(5000, "100", "10", "0", "0.8", "0")
    cur_adt_rej = make_frame(5000, "100", "10", "0", "0.8", "0")
    sig_src_rej, _, _ = absorption_mod.observe(cur_src_rej, h_src, cvds, Decimal("-40"), cfg_source)
    sig_adt_rej = absorption_observe(cur_adt_rej, h_adt, cvds, Decimal("-40"), cfg_adapter)
    assert sig_src_rej == "NO_SIGNAL"
    assert sig_adt_rej.side == "NO_SIGNAL"


def test_divergence_equivalence_across_extrema_and_confirmations():
    cfg_source = metrics_mod.Config(window=3, depth=1, tick_size="1", divergence_price_ticks="1", divergence_delta="1")
    cfg_adapter = SourceStrategyConfigV1(window=3, depth=1, tick_size=Decimal("1"), divergence_price_ticks=Decimal("1"), divergence_delta=Decimal("1"))

    h_src = [
        make_source_frame(1000, "100", "5", "5", "0.0", "0"),
        make_source_frame(2000, "100", "0", "10", "0.0", "0"), # Low CVD
        make_source_frame(3000, "100", "5", "5", "0.0", "0"),
    ]
    h_adt = [
        make_frame(1000, "100", "5", "5", "0.0", "0"),
        make_frame(2000, "100", "0", "10", "0.0", "0"),
        make_frame(3000, "100", "5", "5", "0.0", "0"),
    ]
    cvds = [Decimal("0"), Decimal("-10"), Decimal("-10")]

    # Bullish divergence: price makes lower low (99 <= 100 - 1), but CVD is higher (-8 >= -10 + 1) with delta > 0
    cur_src = make_source_frame(4000, "99", "7", "5", "0.2", "0")
    cur_adt = make_frame(4000, "99", "7", "5", "0.2", "0")
    sig_src, reason_src, _ = divergence_mod.observe(cur_src, h_src, cvds, Decimal("-8"), cfg_source)
    sig_adt = divergence_observe(cur_adt, h_adt, cvds, Decimal("-8"), cfg_adapter)
    assert sig_src == "LONG"
    assert sig_adt.side == "LONG"
    assert sig_adt.reason == reason_src
