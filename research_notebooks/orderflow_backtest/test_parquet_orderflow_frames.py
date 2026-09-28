from decimal import Decimal
import pytest
import pyarrow as pa

from orderflow_backtest.instrument_spec import InstrumentSpec
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


def test_builder_accepts_independently_authorized_non_btc_window(tmp_path):
    from orderflow_backtest.parquet_orderflow_frames import IS_END_MS
    end = IS_END_MS + 60_000
    builder = CausalParquetFrameBuilder(
        tmp_path,
        symbol="ETHUSDC",
        is_end_ms=end,
        authorized_end_ms=end,
    )
    assert builder.authorized_end_ms == end


def test_builder_rejects_other_symbols(tmp_path):
    eth = CausalParquetFrameBuilder(tmp_path, symbol="ETHUSDC")
    assert eth.instrument_spec.symbol == "ETHUSDC"
    assert eth.instrument_spec.price_scale == 100


def test_fixed_point_kernel_uses_eth_instrument_scale(tmp_path):
    builder = FixedPointCausalParquetFrameBuilder(tmp_path, symbol="ETHUSDC")
    builder._fixed_book_bids = {250000: 1000}
    builder._fixed_book_asks = {250005: 1000}
    frame = builder._fixed_frame(60_000, 0, None)
    assert frame is not None
    assert frame.best_bid == Decimal("2500")
    assert frame.best_ask == Decimal("2500.05")
    assert frame.spread_ticks == Decimal("0.05")


def test_missing_instrument_spec_fails_closed(tmp_path):
    with pytest.raises(ValueError, match="missing InstrumentSpec"):
        CausalParquetFrameBuilder(tmp_path, symbol="UNKNOWNUSDT")


def test_non_btc_event_grouping_is_atomic_across_record_batches(tmp_path):
    builder = FixedPointCausalParquetFrameBuilder(tmp_path, symbol="ETHUSDC")
    fields = {
        "event_time": [1, 1], "transaction_time": [1, 1], "event_type": ["update", "update"],
        "first_update_id": [10, 10], "final_update_id": [10, 10], "prev_final_update_id": [9, 9],
        "last_update_id": [0, 0], "side": ["bid", "ask"],
        "price": ["2500.00", "2500.05"], "quantity": ["1.000", "2.000"],
    }
    batches = [pa.record_batch({name: [values[index]] for name, values in fields.items()}) for index in range(2)]
    events = list(builder._fixed_event_stream(batches))
    assert len(events) == 1
    assert events[0][1] == [("bid", 250000, 1000), ("ask", 250005, 2000)]


def test_fixed_point_kernel_rejects_non_tick_aligned_non_btc_price(tmp_path):
    spec = InstrumentSpec(
        symbol="QUARTERUSDT", venue="test", market_type="futures",
        tick_size=Decimal("0.25"), step_size=Decimal("0.005"),
        price_scale=100, quantity_scale=1000,
        price_precision=2, quantity_precision=3,
        metadata_source="fixture", metadata_timestamp="2026-09-27T00:00:00Z",
        metadata_hash="0" * 64,
    )
    builder = FixedPointCausalParquetFrameBuilder(tmp_path, symbol="QUARTERUSDT", instrument_spec=spec)
    batch = pa.record_batch({
        "event_time": [1], "transaction_time": [1], "event_type": ["update"],
        "first_update_id": [10], "final_update_id": [10], "prev_final_update_id": [9],
        "last_update_id": [0], "side": ["bid"], "price": ["10.40"], "quantity": ["1.000"],
    })
    with pytest.raises(ValueError, match="not aligned"):
        builder._kernel_rows(batch)


def test_compiled_rows_exclude_events_after_authorized_end(tmp_path):
    builder = FixedPointCausalParquetFrameBuilder(
        tmp_path,
        is_start_ms=0,
        is_end_ms=100,
        authorized_end_ms=100,
    )
    batch = pa.record_batch({
        "event_time": [99, 101], "transaction_time": [99, 101],
        "event_type": ["update", "update"],
        "first_update_id": [10, 11], "final_update_id": [10, 11],
        "prev_final_update_id": [9, 10], "last_update_id": [0, 0],
        "side": ["bid", "ask"], "price": ["100.00", "100.10"],
        "quantity": ["1.000", "1.000"],
    })
    rows = builder._kernel_rows(batch)
    assert rows[:, 0].tolist() == [99]
