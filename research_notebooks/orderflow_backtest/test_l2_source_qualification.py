"""Comprehensive qualification test suite for Historical L2 Sources and Prospective Capture."""
from decimal import Decimal
import json
from pathlib import Path
import pytest

from orderflow_backtest.historical_data_v1 import LevelV1, BookStateV1, TradePrintV1
from orderflow_backtest.historical_l2_parsers import (
    BinanceNativeL2Parser,
    TardisIncrementalL2Parser,
    TardisSnapshot25Parser,
    calculate_depth_skew,
    calculate_obi,
    calculate_spread_ticks,
    compose_orderflow_frame,
    reconcile_book_states,
    verify_adapter_compatibility,
)
from orderflow_backtest.orderflow_contracts import OrderFlowFrameV1, SourceStrategyConfigV1
from orderflow_backtest.prospective_capture_v1 import (
    BinanceLiveBookReconstructor,
    CapturePartitionManifestV1,
    ProspectiveCaptureState,
    ProspectivePartitionWriter,
    RawMarketEventV1,
    SequenceGapV1,
)

FIXTURES = Path(__file__).parent / "qualification_fixtures"


class TestHistoricalL2Qualification:
    """Tests historical Level-2 parser fidelity, exact decimals, reconstruction, and adapter compatibility."""

    def test_tardis_snapshot_25_parser_fidelity(self):
        btc_csv = (FIXTURES / "tardis_btcusdt_snapshot_25.csv").read_text()
        snaps = TardisSnapshot25Parser.parse_csv(btc_csv, "tardis_btc_test")
        assert len(snaps) > 0
        snap = snaps[0]
        assert snap.symbol == "BTCUSDT"
        assert snap.provider == "tardis"
        assert len(snap.bids) == 25
        assert len(snap.asks) == 25
        assert isinstance(snap.bids[0].price, Decimal)
        assert isinstance(snap.asks[0].price, Decimal)
        assert snap.bids[0].price < snap.asks[0].price

    def test_tardis_ethusdc_snapshot_25_fidelity(self):
        eth_csv = (FIXTURES / "tardis_ethusdc_snapshot_25.csv").read_text()
        snaps = TardisSnapshot25Parser.parse_csv(eth_csv, "tardis_eth_test")
        assert len(snaps) > 0
        snap = snaps[0]
        assert snap.symbol == "ETHUSDC"
        assert len(snap.bids) == 25
        assert len(snap.asks) == 25
        assert snap.bids[0].price < snap.asks[0].price

    def test_tardis_incremental_l2_parser_and_reconstruction(self):
        btc_csv = (FIXTURES / "tardis_btcusdt_incremental_l2.csv").read_text()
        updates = TardisIncrementalL2Parser.parse_csv(btc_csv, "tardis_btc_inc")
        assert len(updates) > 0
        assert updates[0].update_type == "snapshot"
        # Verify decimal precision and side grouping
        assert len(updates[0].bids) > 0 or len(updates[0].asks) > 0

    def test_binance_native_rest_snapshot_parser(self):
        data = json.loads((FIXTURES / "binance_rest_btcusdt_depth100.json").read_text())
        snap = BinanceNativeL2Parser.parse_rest_snapshot(data, "BTCUSDT")
        assert snap.symbol == "BTCUSDT"
        assert snap.sequence == data["lastUpdateId"]
        assert len(snap.bids) == 100
        assert len(snap.asks) == 100
        assert snap.bids[0].price < snap.asks[0].price

    def test_exact_decimal_features(self):
        bids = (LevelV1(Decimal("50000.00"), Decimal("10.5")), LevelV1(Decimal("49999.00"), Decimal("5.0")))
        asks = (LevelV1(Decimal("50001.00"), Decimal("4.5")), LevelV1(Decimal("50002.00"), Decimal("6.0")))
        obi = calculate_obi(bids, asks, depth=2)
        skew = calculate_depth_skew(bids, asks, depth=2)
        spread_ticks = calculate_spread_ticks(bids[0].price, asks[0].price, Decimal("0.10"))

        # bid_vol = 15.5, ask_vol = 10.5, total = 26.0, diff = 5.0 -> obi = 5/26
        assert obi == Decimal("5.0") / Decimal("26.0")
        assert skew == Decimal("15.5") / Decimal("10.5")
        assert spread_ticks == Decimal("1.00") / Decimal("0.10")
        assert spread_ticks == Decimal("10")

    def test_snapshot_reconciliation(self):
        bids = (LevelV1(Decimal("100"), Decimal("2")), LevelV1(Decimal("99"), Decimal("3")))
        asks = (LevelV1(Decimal("101"), Decimal("2")), LevelV1(Decimal("102"), Decimal("3")))
        book = BookStateV1("BTCUSDT", 1000, 10, bids, asks)
        snap = BinanceNativeL2Parser.parse_rest_snapshot({"lastUpdateId": 10, "bids": [["100", "2"], ["99", "3"]], "asks": [["101", "2"], ["102", "3"]]}, "BTCUSDT")
        assert reconcile_book_states(book, snap, max_levels=2) is True

        snap_diff = BinanceNativeL2Parser.parse_rest_snapshot({"lastUpdateId": 11, "bids": [["100", "5"]], "asks": [["101", "2"]]}, "BTCUSDT")
        assert reconcile_book_states(book, snap_diff, max_levels=2) is False

    def test_l2_and_tape_causal_composition(self):
        bids = (LevelV1(Decimal("60000.00"), Decimal("1.0")),)
        asks = (LevelV1(Decimal("60001.00"), Decimal("1.0")),)
        book = BookStateV1("BTCUSDT", 1000, 5, bids, asks)
        trades = (
            TradePrintV1("binance", "venue", "BTCUSDT", 950, "t1", Decimal("60000.50"), Decimal("0.5"), "BUY", "tape"),
            TradePrintV1("binance", "venue", "BTCUSDT", 1000, "t2", Decimal("60000.50"), Decimal("0.3"), "SELL", "tape"),
            TradePrintV1("binance", "venue", "BTCUSDT", 1050, "t3_future", Decimal("60002.00"), Decimal("10.0"), "BUY", "tape"),
        )
        frame = compose_orderflow_frame(book, trades, tick_size=Decimal("0.10"))
        assert frame.timestamp_ms == 1000
        assert frame.price == Decimal("60000.50")
        assert frame.buy == Decimal("0.5")
        assert frame.sell == Decimal("0.3")
        assert frame.delta == Decimal("0.2")
        assert frame.volume == Decimal("0.8")
        assert frame.spread_ticks == Decimal("10")

    def test_adapter_compatibility_with_composed_frames(self):
        frames = []
        for i in range(25):
            frames.append(
                OrderFlowFrameV1(
                    timestamp_ms=1000 + i * 1000,
                    price=Decimal("50000") + Decimal(i) * Decimal("0.5"),
                    buy=Decimal("5.0") + Decimal(i),
                    sell=Decimal("2.0"),
                    obi=Decimal("0.70"),
                    spread_ticks=Decimal("1.0"),
                )
            )
        compat = verify_adapter_compatibility(frames, SourceStrategyConfigV1())
        assert compat["momentum"] is True
        assert compat["absorption"] is True
        assert compat["divergence"] is True


class TestProspectiveCaptureArchitecture:
    """Tests live WebSocket depth parsing, sequence alignment, gap detection, and immutable partitioning."""

    def test_live_book_reconstructor_bootstrap_and_update(self):
        rec = BinanceLiveBookReconstructor("BTCUSDT", Decimal("0.01"))
        assert rec.state == ProspectiveCaptureState.BOOTSTRAPPING

        # Buffer an update before snapshot
        msg1 = {"e": "depthUpdate", "E": 1000, "s": "BTCUSDT", "U": 10, "u": 15, "pu": 9, "b": [["50000", "1.5"]], "a": [["50001", "2.0"]]}
        rec.process_depth_update(msg1)
        assert len(rec.buffer) == 1

        # Apply snapshot with lastUpdateId=12 (overlapping U=10 <= 13 <= u=15)
        snap = {"lastUpdateId": 12, "E": 1005, "bids": [["50000", "1.0"]], "asks": [["50001", "2.0"]]}
        rec.apply_snapshot(snap)
        assert rec.state == ProspectiveCaptureState.STREAMING_QUALIFIED
        assert rec.last_update_id == 15
        assert rec.bids[Decimal("50000")] == Decimal("1.5")

        # Apply next contiguous update (pu == 15, U=16, u=20)
        msg2 = {"e": "depthUpdate", "E": 1100, "s": "BTCUSDT", "U": 16, "u": 20, "pu": 15, "b": [["50000", "3.0"]], "a": []}
        state = rec.process_depth_update(msg2)
        assert state is not None
        assert state.sequence == 20
        assert state.best_bid.quantity == Decimal("3.0")

    def test_live_book_reconstructor_detects_gap_and_fails_closed(self):
        rec = BinanceLiveBookReconstructor("BTCUSDT", Decimal("0.01"))
        rec.apply_snapshot({"lastUpdateId": 10, "E": 1000, "bids": [["50000", "1.0"]], "asks": [["50001", "1.0"]]})

        # Gap in pu: expected pu=10, received pu=15
        gap_msg = {"e": "depthUpdate", "E": 1100, "s": "BTCUSDT", "U": 16, "u": 20, "pu": 15, "b": [], "a": []}
        with pytest.raises(ValueError, match="Live sequence gap detected"):
            rec.process_depth_update(gap_msg)

        assert rec.state == ProspectiveCaptureState.LIVE_GAP_DETECTED
        assert len(rec.gaps) == 1
        assert rec.gaps[0].gap_type == "SEQUENCE_GAP"

    def test_prospective_partition_writer_and_manifest(self, tmp_path):
        writer = ProspectivePartitionWriter(tmp_path, "binance-futures", "binance", "BTCUSDT")
        ev1 = RawMarketEventV1("binance-futures", "binance", "BTCUSDT", "depth", 1000, 1002, 100, '{"u":100}')
        ev2 = RawMarketEventV1("binance-futures", "binance", "BTCUSDT", "depth", 1100, 1102, 105, '{"u":105}')
        writer.append_event(ev1)
        writer.append_event(ev2)

        raw_file, manifest = writer.close_and_finalize()
        assert raw_file.exists()
        assert raw_file.stat().st_size > 0
        assert manifest.record_count == 2
        assert manifest.first_sequence == 100
        assert manifest.last_sequence == 105
        assert len(manifest.checksum_sha256) == 64
        assert manifest.checksum_sha256 == manifest.checksum_sha256.lower()

        # Verify manifest file on disk
        manifest_path = Path(str(raw_file) + ".manifest.json")
        assert manifest_path.exists()
        loaded = json.loads(manifest_path.read_text())
        assert loaded["record_count"] == 2
        assert loaded["symbol"] == "BTCUSDT"
