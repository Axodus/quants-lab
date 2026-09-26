from decimal import Decimal

from orderflow_backtest.orderflow_contracts import OrderFlowFrameV1
from orderflow_backtest.real_absorption_replay import ReplayConfig
from orderflow_backtest.parquet_orderflow_frames import CausalParquetFrameBuilder, FixedPointCausalParquetFrameBuilder


def test_frame_contract_contains_microstructure_fields():
    frame = OrderFlowFrameV1(0, Decimal("100"), Decimal("2"), Decimal("1"), Decimal("0.2"), Decimal("1"), Decimal("99"), Decimal("101"), Decimal("10"), Decimal("8"), Decimal("1.25"), Decimal("1"), Decimal("1"), Decimal("0"), Decimal("100"), Decimal("100"))
    assert frame.best_bid < frame.best_ask
    assert frame.absolute_delta == Decimal("1")


def test_builder_paths_are_is_bounded(tmp_path):
    builder = CausalParquetFrameBuilder(tmp_path, is_start_ms=0, is_end_ms=59_999)
    assert builder._paths("orderbook") == []


def test_replay_config_keeps_oos_end_explicit():
    config = ReplayConfig(run_id="test")
    assert config.is_end_ms < int(1783036800 * 1000)


def test_fixed_point_conversion_preserves_tick_and_step_units():
    assert FixedPointCausalParquetFrameBuilder._fixed_array(["77312.80", "0.10"], 10) == [773128, 1]
    assert FixedPointCausalParquetFrameBuilder._fixed_array(["0.537", "0.001"], 1000) == [537, 1]


def test_fixed_point_frame_timestamp_is_minute_close():
    builder = FixedPointCausalParquetFrameBuilder("/tmp")
    builder._fixed_book_bids = {100: 10}
    builder._fixed_book_asks = {101: 10}
    frame = builder._fixed_frame(60_000, 0, None)
    assert frame is not None
    assert frame.timestamp_ms == 119_999


def test_fixed_point_crossed_book_is_rejected_and_recorded():
    builder = FixedPointCausalParquetFrameBuilder("/tmp")
    builder._fixed_book_bids = {101: 10}
    builder._fixed_book_asks = {101: 10}
    assert builder._fixed_frame(60_000, 0, None) is None
    assert builder.stats["crossed_books"] == 1
    assert builder.crossed_frame_times == [119_999]


def test_builder_rejects_oos_before_partition_discovery(tmp_path):
    import pytest
    from orderflow_backtest.parquet_orderflow_frames import IS_END_MS
    with pytest.raises(ValueError, match="OOS boundary"):
        CausalParquetFrameBuilder(tmp_path, is_end_ms=IS_END_MS + 1)


def test_builder_rejects_other_symbols(tmp_path):
    import pytest
    with pytest.raises(ValueError, match="BTCUSDT only"):
        CausalParquetFrameBuilder(tmp_path, symbol="ETHUSDC")
