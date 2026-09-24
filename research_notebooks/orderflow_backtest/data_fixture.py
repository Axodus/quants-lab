"""
Data Fixture and Tick Generator for Order Flow Backtesting.

Generates realistic 90-day (or customizable duration) high-frequency L2 order book
depth snapshots and Cumulative Volume Delta (CVD) data for BTCUSDT and ETHUSDC.

Features generated per interval (1m / 5m):
- timestamp: Unix epoch timestamp in seconds
- open, high, low, close: Price action
- volume: Total traded volume in quote currency
- buy_volume, sell_volume: Aggressive taker volume split
- cvd: Cumulative Volume Delta (buy_volume - sell_volume accumulated)
- delta: Instantaneous delta (buy_volume - sell_volume)
- bid_depth, ask_depth: L2 depth inside the top 10 levels
- bid_skew: (bid_depth - ask_depth) / (bid_depth + ask_depth)
- vwap: Volume Weighted Average Price
- order_imbalance: Normalized aggressive buy/sell ratio
- absorption_score: Metric of large passive orders absorbing market orders
"""

import math
import random
from dataclasses import dataclass
from typing import Dict, List, Optional, Union


@dataclass
class MarketTick:
    """Represents a single granular market state tick."""
    timestamp: float
    open: float
    high: float
    low: float
    close: float
    volume: float
    buy_volume: float
    sell_volume: float
    delta: float
    cvd: float
    bid_depth: float
    ask_depth: float
    bid_skew: float
    vwap: float
    order_imbalance: float
    absorption_score: float

    def to_dict(self) -> Dict[str, Union[float, int]]:
        return {
            "timestamp": self.timestamp,
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
            "volume": self.volume,
            "buy_volume": self.buy_volume,
            "sell_volume": self.sell_volume,
            "delta": self.delta,
            "cvd": self.cvd,
            "bid_depth": self.bid_depth,
            "ask_depth": self.ask_depth,
            "bid_skew": self.bid_skew,
            "vwap": self.vwap,
            "order_imbalance": self.order_imbalance,
            "absorption_score": self.absorption_score,
        }


def generate_synthetic_ticks(
    pair: str = "BTCUSDT",
    days: int = 90,
    interval_seconds: int = 60,
    seed: Optional[int] = 42,
    base_price: Optional[float] = None,
    volatility: Optional[float] = None,
) -> List[MarketTick]:
    """
    Generate synthetic high-frequency intraday order flow ticks.

    Args:
        pair: Target trading pair ('BTCUSDT' or 'ETHUSDC')
        days: Number of days in the simulation window (e.g. 90)
        interval_seconds: Interval length in seconds (60 = 1m, 300 = 5m)
        seed: Random seed for deterministic reproducibility
        base_price: Starting mid-price
        volatility: Annualized / step volatility factor

    Returns:
        List of MarketTick objects with depth and CVD metrics.
    """
    if seed is not None:
        random.seed(seed)

    # Base price defaults
    if base_price is None:
        if "BTC" in pair.upper():
            base_price = 65000.0
            avg_volume = 120.0  # units per min
            depth_scale = 500.0
        else:  # ETH or others
            base_price = 3500.0
            avg_volume = 1500.0
            depth_scale = 4000.0
    else:
        avg_volume = 100.0
        depth_scale = 500.0

    if volatility is None:
        step_vol = 0.0008  # ~1.2% daily vol on 1m bars
    else:
        step_vol = volatility

    total_steps = int((days * 24 * 3600) / interval_seconds)
    current_time = 1704067200.0  # 2024-01-01 00:00:00 UTC
    current_price = base_price
    running_cvd = 0.0
    cum_pv = 0.0
    cum_v = 0.0

    # Macro regime simulation (cycles of trend, range, absorption)
    regime_period = int((3 * 24 * 3600) / interval_seconds)  # regime changes every ~3 days
    regime_bias = 0.0

    ticks: List[MarketTick] = []

    for step in range(total_steps):
        # Update macro regime periodically
        if step % regime_period == 0:
            regime_bias = random.gauss(0.0, step_vol * 0.4)

        # Micro-structure noise + mean reversion + regime drift
        drift = regime_bias + (base_price - current_price) * 0.00002
        noise = random.gauss(0.0, step_vol)
        price_ret = drift + noise

        p_open = current_price
        p_close = max(1.0, current_price * (1.0 + price_ret))

        # High and low with intraday wick simulation
        spread_noise = abs(random.gauss(0.0, step_vol * 0.8))
        p_high = max(p_open, p_close) * (1.0 + spread_noise)
        p_low = min(p_open, p_close) * (1.0 - spread_noise)

        # Volume dynamics correlated with volatility
        vol_multiplier = math.exp(abs(price_ret) / step_vol - 0.5)
        step_volume = avg_volume * max(0.1, random.lognormvariate(0, 0.5)) * vol_multiplier

        # Order flow aggression split
        # Price up -> higher buy volume taker aggression
        prob_buy = 0.5 + (price_ret / (step_vol * 3.0))
        prob_buy = max(0.15, min(0.85, prob_buy))

        # Occasional absorption pattern (price drops but delta stays positive or vice versa)
        is_absorption_event = (random.random() < 0.05)
        if is_absorption_event:
            # Strong aggressive flow absorbed by resting limit book
            if random.random() < 0.5:
                # Heavy buy aggression absorbed at resistance -> delta strongly positive, price flat/down
                buy_vol = step_volume * random.uniform(0.75, 0.90)
                sell_vol = step_volume - buy_vol
                p_close = min(p_open * 1.0002, p_close)
            else:
                # Heavy sell aggression absorbed at bid support -> delta strongly negative, price flat/up
                sell_vol = step_volume * random.uniform(0.75, 0.90)
                buy_vol = step_volume - sell_vol
                p_close = max(p_open * 0.9998, p_close)
        else:
            buy_vol = step_volume * prob_buy
            sell_vol = step_volume * (1.0 - prob_buy)

        step_delta = buy_vol - sell_vol
        running_cvd += step_delta

        # Cumulative VWAP calculation (resets or moving window)
        typical_price = (p_high + p_low + p_close) / 3.0
        cum_pv += typical_price * step_volume
        cum_v += step_volume
        vwap = cum_pv / cum_v if cum_v > 0 else current_price

        # L2 Depth Simulation
        bid_depth = depth_scale * max(0.2, random.lognormvariate(0, 0.4))
        ask_depth = depth_scale * max(0.2, random.lognormvariate(0, 0.4))

        if is_absorption_event:
            if buy_vol > sell_vol:
                # Ask wall absorbed buyers
                ask_depth *= 2.5
            else:
                # Bid wall absorbed sellers
                bid_depth *= 2.5

        bid_skew = (bid_depth - ask_depth) / (bid_depth + ask_depth + 1e-9)
        order_imbalance = (buy_vol - sell_vol) / (step_volume + 1e-9)

        # Absorption Score: High imbalance with low price progress
        price_progress_pct = abs(p_close - p_open) / p_open
        norm_delta_impact = abs(order_imbalance) / (price_progress_pct * 1000.0 + 1.0)
        absorption_score = min(10.0, norm_delta_impact * 2.0)

        tick = MarketTick(
            timestamp=current_time,
            open=p_open,
            high=p_high,
            low=p_low,
            close=p_close,
            volume=step_volume,
            buy_volume=buy_vol,
            sell_volume=sell_vol,
            delta=step_delta,
            cvd=running_cvd,
            bid_depth=bid_depth,
            ask_depth=ask_depth,
            bid_skew=bid_skew,
            vwap=vwap,
            order_imbalance=order_imbalance,
            absorption_score=absorption_score,
        )

        ticks.append(tick)
        current_price = p_close
        current_time += interval_seconds

    return ticks
