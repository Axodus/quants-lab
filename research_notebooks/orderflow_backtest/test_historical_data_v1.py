from decimal import Decimal
import pytest

from orderflow_backtest.historical_data_v1 import (
    GapClassification, LevelV1, OrderBookUpdateV1, TradePrintV1,
    align_tape_to_book, detect_sequence_gap, reconstruct_book, signed_trade_volume,
)


def update(seq, kind="delta", bid_qty="2", ask_qty="2"):
    return OrderBookUpdateV1("fixture", "venue", "BTCUSDT", seq * 1000, seq,
                             (LevelV1(Decimal("99"), Decimal(bid_qty)),),
                             (LevelV1(Decimal("101"), Decimal(ask_qty)),), kind, "fixture.jsonl")


def test_sequence_gap_is_explicit_and_fail_closed():
    assert detect_sequence_gap(10, 11) == GapClassification.NO_GAP
    assert detect_sequence_gap(10, 13) == GapClassification.UNRECOVERABLE_GAP
    assert detect_sequence_gap(None, 1) == GapClassification.UNKNOWN_SEQUENCE_STATE


def test_reconstruct_snapshot_then_delta_preserves_decimal_book():
    states = reconstruct_book((update(1, "snapshot"), update(2)))
    assert states[-1].best_bid.price == Decimal("99")
    assert states[-1].best_ask.price == Decimal("101")
    assert states[-1].spread == Decimal("2")


def test_reconstruct_rejects_gap_and_invalid_book():
    with pytest.raises(ValueError, match="UNRECOVERABLE_GAP"):
        reconstruct_book((update(1, "snapshot"), update(3)))
    crossed = OrderBookUpdateV1("fixture", "venue", "BTCUSDT", 1000, 1,
                                (LevelV1(Decimal("101"), Decimal("1")),),
                                (LevelV1(Decimal("101"), Decimal("1")),), "snapshot", "fixture")
    with pytest.raises(ValueError, match="invalid"):
        reconstruct_book((crossed,))


def test_tape_cvd_and_causal_alignment():
    book = reconstruct_book((update(1, "snapshot"),))[0]
    trades = (
        TradePrintV1("fixture", "venue", "BTCUSDT", 900, "1", Decimal("2"), Decimal("3"), "BUY", "tape"),
        TradePrintV1("fixture", "venue", "BTCUSDT", 1100, "2", Decimal("2"), Decimal("1"), "SELL", "tape"),
    )
    assert signed_trade_volume(trades) == Decimal("2")
    assert len(align_tape_to_book(book, trades)) == 1
