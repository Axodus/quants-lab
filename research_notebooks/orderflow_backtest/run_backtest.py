#!/usr/bin/env python3
"""
CLI entrypoint for 90-Day Order Flow Backtesting Harness.

Usage:
  python3 run_backtest.py --pair BTCUSDT --days 90 --strategy all
  python3 run_backtest.py --pair ETHUSDC --days 30 --strategy absorption --json
"""

import argparse
import json
import sys
from pathlib import Path

# Add current directory to path if needed
current_dir = Path(__file__).resolve().parent
if str(current_dir) not in sys.path:
    sys.path.insert(0, str(current_dir))

from engine import BacktestConfig, BacktestResult, FeeModel, run_backtest
from data_fixture import generate_synthetic_ticks


def format_table(rows: list, headers: list) -> str:
    """Format plain ascii table without third-party dependencies."""
    col_widths = [len(h) for h in headers]
    for row in rows:
        for i, val in enumerate(row):
            col_widths[i] = max(col_widths[i], len(str(val)))

    header_line = " | ".join(f"{h:<{col_widths[i]}}" for i, h in enumerate(headers))
    sep_line = "-+-".join("-" * col_widths[i] for i in range(len(headers)))
    row_lines = [
        " | ".join(f"{str(val):<{col_widths[i]}}" for i, val in enumerate(row))
        for row in rows
    ]

    return f"{header_line}\n{sep_line}\n" + "\n".join(row_lines)


def print_cli_report(result: BacktestResult, fee_model: FeeModel, output_json: bool = False):
    if output_json:
        payload = {
            "config": {
                "pair": result.config.pair,
                "simulation_days": result.config.days,
                "ticks_evaluated": result.ticks_evaluated,
                "initial_capital": result.config.initial_capital,
                "position_size_usd": result.config.position_size_usd,
                "fee_schedule": {
                    "taker_fee_pct": f"{fee_model.taker_fee_rate * 100:.3f}%",
                    "maker_fee_pct": f"{fee_model.maker_fee_rate * 100:.3f}%",
                    "slippage_bps": fee_model.slippage_bps,
                },
            },
            "results": {
                name: rep.to_dict() for name, rep in result.reports.items()
            },
        }
        print(json.dumps(payload, indent=2))
        return

    print("=" * 88)
    print(f" AXODUS QUANTS-LAB | 90-DAY ORDER FLOW BACKTEST ENGINE")
    print("=" * 88)
    print(f" Target Pair       : {result.config.pair}")
    print(f" Simulation Period : {result.config.days} Days ({result.ticks_evaluated:,} 1m Ticks)")
    print(f" Initial Capital   : ${result.config.initial_capital:,.2f} | Trade Notional: ${result.config.position_size_usd:,.2f}")
    print(
        f" Fee Structure     : Taker: {fee_model.taker_fee_rate * 100:.3f}% | "
        f"Maker: {fee_model.maker_fee_rate * 100:.3f}% | Slippage: {fee_model.slippage_bps} bps"
    )
    print("=" * 88)
    print()

    headers = [
        "Strategy",
        "Trades",
        "Win%",
        "Gross PnL",
        "Fees Paid",
        "Slippage",
        "Net PnL",
        "Return%",
        "Profit Factor",
        "Max DD%",
        "Sharpe",
        "Fee Drag%",
    ]

    rows = []
    for name, rep in result.reports.items():
        pf_str = f"{rep.profit_factor:.2f}" if rep.profit_factor != float("inf") else "inf"
        rows.append([
            name[:25],
            str(rep.total_trades),
            f"{rep.win_rate_pct:.1f}%",
            f"${rep.gross_pnl:+,.2f}",
            f"${rep.total_fees_paid:,.2f}",
            f"${rep.total_slippage_cost:,.2f}",
            f"${rep.net_pnl:+,.2f}",
            f"{rep.net_return_pct:+.2f}%",
            pf_str,
            f"{rep.max_drawdown_pct:.2f}%",
            f"{rep.sharpe_ratio:.2f}",
            f"{rep.fee_drag_pct:.1f}%",
        ])

    print(format_table(rows, headers))
    print()
    print("=" * 88)
    print(" STRATEGY BREAKDOWN & ECONOMIC IMPACT")
    print("=" * 88)
    for name, rep in result.reports.items():
        print(f"\n Strategy: {name}")
        print(f"   • Total Trades: {rep.total_trades} (Wins: {rep.winning_trades}, Losses: {rep.losing_trades})")
        print(f"   • Gross PnL: ${rep.gross_pnl:+,.2f} -> Net PnL: ${rep.net_pnl:+,.2f}")
        print(f"   • Economic Friction: Total Fees: ${rep.total_fees_paid:,.2f}, Slippage: ${rep.total_slippage_cost:,.2f}")
        print(f"   • Fee Drag Impact: {rep.fee_drag_pct:.2f}% of gross edge consumed by exchange/slippage")
        print(f"   • Risk Profile: Max Drawdown: {rep.max_drawdown_pct:.2f}% (${rep.max_drawdown_dollars:,.2f}), Sharpe: {rep.sharpe_ratio:.2f}")


def main():
    parser = argparse.ArgumentParser(
        description="Run 90-Day Order Flow High-Frequency Backtest on BTCUSDT or ETHUSDC"
    )
    parser.add_argument(
        "--pair",
        type=str,
        default="BTCUSDT",
        choices=["BTCUSDT", "ETHUSDC"],
        help="Trading pair configuration (BTCUSDT or ETHUSDC)",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=90,
        help="Number of days in simulation window (default: 90)",
    )
    parser.add_argument(
        "--strategy",
        type=str,
        default="all",
        choices=["all", "momentum", "absorption", "divergence"],
        help="Strategy to evaluate (default: all)",
    )
    parser.add_argument(
        "--capital",
        type=float,
        default=10000.0,
        help="Initial capital in USD (default: 10000.0)",
    )
    parser.add_argument(
        "--position-size",
        type=float,
        default=1000.0,
        help="Fixed trade size in USD (default: 1000.0)",
    )
    parser.add_argument(
        "--slippage-bps",
        type=float,
        default=1.0,
        help="Execution slippage in basis points (default: 1.0 bps)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output raw JSON format for automated pipelines",
    )
    parser.add_argument(
        "--interval-seconds",
        type=int,
        default=60,
        help="Tick resolution in seconds (default: 60 for 1m)",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=42,
        help="Random seed for repeatable synthetic benchmark generation",
    )

    args = parser.parse_args()

    fee_model = FeeModel.for_pair(args.pair)
    fee_model.slippage_bps = args.slippage_bps

    # Generate synthetic 90-day high-frequency ticks with depth & CVD
    ticks = generate_synthetic_ticks(
        pair=args.pair,
        days=args.days,
        interval_seconds=args.interval_seconds,
        seed=args.seed,
    )

    result = run_backtest(
        pair=args.pair,
        days=args.days,
        strategy_key=args.strategy,
        ticks=ticks,
        initial_capital=args.capital,
        position_size_usd=args.position_size,
        slippage_bps=args.slippage_bps,
    )

    print_cli_report(result, fee_model, output_json=args.json)


if __name__ == "__main__":
    main()
