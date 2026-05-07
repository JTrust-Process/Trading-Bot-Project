"""
backtest_public.py — Standalone backtest harness for Public-bars-driven strategies.

This is a NEW file. It does not modify the existing backtest.py, bot.py, or any
live order placement logic.

Usage:
    # Use Public.com historical bars (requires PUBLIC_SECRET in env):
    python backtest_public.py --source public --period YEAR

    # Use yfinance for an offline / no-auth comparison:
    python backtest_public.py --source yfinance --period YEAR

Default symbol universe: SPY QQQ AAPL MSFT NVDA AMZN GOOGL META TSLA

Outputs:
    backtest_public_summary.csv  — one row per (symbol, strategy) with metrics
    backtest_public_trades.csv   — every simulated trade
    Console summary table

Strategies compared:
    1. "old"  — replicates the live bot's momentum+breakout entry rules using the
                indicator math actually in production today (SMA-cross trend +
                weighted 1d/5d/10d momentum + recent-high breakout).
    2. "new"  — a more conservative bars-based variant that adds:
                  - SPY-above-SMA(50) regime filter
                  - ATR-based stop-loss / take-profit
                  - minimum hold period
                  - cooldown after exit

Neither strategy places real orders. They both simulate next-day fills at the
open. This file is purely for offline evaluation.
"""

from __future__ import annotations

import argparse
import csv
import math
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

# Local modules — additive, no live-bot imports
from indicators import (
    atr,
    breakout_threshold,
    conservative_score,
    is_above_sma,
    momentum_pct,
    regime,
    sma,
)


DEFAULT_SYMBOLS: List[str] = [
    "SPY", "QQQ", "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA",
]


# ------------------------------------------------------------
# Data sources
# ------------------------------------------------------------
def _load_public_bars(
    symbol: str,
    period: str,
    *,
    timeout: float = 12.0,
) -> Optional[pd.DataFrame]:
    """Lazy import so this script still runs when network/auth aren't set up."""
    try:
        from public_bars import get_public_bars  # type: ignore[import-not-found]
    except ImportError as e:
        print(f"[backtest_public] cannot import public_bars: {e}")
        return None
    return get_public_bars(symbol, period=period, timeout=timeout)


def _load_yfinance_bars(symbol: str, period: str) -> Optional[pd.DataFrame]:
    """Pull a roughly comparable window from yfinance for offline backtests."""
    try:
        import yfinance as yf  # type: ignore[import-untyped]
    except ImportError:
        print("[backtest_public] yfinance not installed; pip install yfinance")
        return None

    # Map our period to yfinance period strings
    period_map = {
        "DAY": "5d",
        "WEEK": "1mo",
        "MONTH": "3mo",
        "QUARTER": "6mo",
        "HALF_YEAR": "1y",
        "YEAR": "1y",
        "FIVE_YEARS": "5y",
        "YTD": "ytd",
    }
    yf_period = period_map.get(period.upper(), "1y")

    try:
        df = yf.Ticker(symbol).history(period=yf_period, auto_adjust=False)
    except Exception as e:
        print(f"[backtest_public] yfinance fetch failed for {symbol}: {e}")
        return None

    if df is None or df.empty:
        return None

    df = df.rename(
        columns={
            "Open": "open",
            "High": "high",
            "Low": "low",
            "Close": "close",
            "Volume": "volume",
        }
    )
    keep = ["open", "high", "low", "close", "volume"]
    for col in keep:
        if col not in df.columns:
            df[col] = 0.0
    df = df[keep].dropna(subset=["close"]).reset_index(drop=True)
    return df


def load_bars(
    symbol: str,
    period: str,
    source: str,
    *,
    timeout: float = 12.0,
) -> Optional[pd.DataFrame]:
    if source == "public":
        return _load_public_bars(symbol, period, timeout=timeout)
    if source == "yfinance":
        # yfinance path is intentionally unchanged — its own retry/timeout logic
        # has been working fine for you.
        return _load_yfinance_bars(symbol, period)
    raise ValueError(f"unknown source: {source!r}")


# ------------------------------------------------------------
# Metrics
# ------------------------------------------------------------
def _max_drawdown(equity: List[float]) -> float:
    if not equity:
        return 0.0
    peak = equity[0]
    mdd = 0.0
    for v in equity:
        if v > peak:
            peak = v
        if peak > 0:
            dd = (v / peak) - 1.0
            if dd < mdd:
                mdd = dd
    return mdd


def _profit_factor(trade_returns: List[float]) -> float:
    wins = sum(r for r in trade_returns if r > 0)
    losses = -sum(r for r in trade_returns if r < 0)
    if losses <= 0:
        return float("inf") if wins > 0 else 0.0
    return wins / losses


# ------------------------------------------------------------
# Trade record
# ------------------------------------------------------------
@dataclass
class Trade:
    symbol: str
    strategy: str
    entry_idx: int
    entry_price: float
    exit_idx: int
    exit_price: float
    bars_held: int
    pnl_pct: float
    reason: str


@dataclass
class StrategyResult:
    symbol: str
    strategy: str
    trades: List[Trade] = field(default_factory=list)
    equity_curve: List[float] = field(default_factory=list)

    def summary(self) -> Dict[str, Any]:
        rets = [t.pnl_pct for t in self.trades]
        n = len(rets)
        wins = [r for r in rets if r > 0]
        losses = [r for r in rets if r < 0]
        total_return = (self.equity_curve[-1] / self.equity_curve[0] - 1.0) if self.equity_curve else 0.0
        return {
            "symbol": self.symbol,
            "strategy": self.strategy,
            "trades": n,
            "win_rate": (len(wins) / n) if n else 0.0,
            "avg_win": (sum(wins) / len(wins)) if wins else 0.0,
            "avg_loss": (sum(losses) / len(losses)) if losses else 0.0,
            "max_drawdown": _max_drawdown(self.equity_curve),
            "profit_factor": _profit_factor(rets),
            "total_return": total_return,
        }


# ------------------------------------------------------------
# Strategy: OLD (replicates current live bot logic on bars)
# ------------------------------------------------------------
def run_old_strategy(symbol: str, df: pd.DataFrame) -> StrategyResult:
    """
    Replicates the live bot's signal stack on historical bars:
        - SMA fast/slow trend cross (TREND_FAST=5, TREND_SLOW=20)
        - Weighted 1d/5d/10d momentum (>0 to enter)
        - Breakout above prior 20-bar high (with small buffer)
    Exit rule: SMA fast crosses below slow OR composite momentum <= 0.
    """
    res = StrategyResult(symbol=symbol, strategy="old")
    closes = df["close"].astype(float).reset_index(drop=True)
    opens = df["open"].astype(float).reset_index(drop=True)

    if len(closes) < 25:
        res.equity_curve = [1.0] * max(1, len(closes))
        return res

    cash = 1.0
    shares = 0.0
    entry_price: Optional[float] = None
    entry_idx: Optional[int] = None

    for i in range(len(closes)):
        # Mark-to-market end of day
        equity = cash + shares * float(closes.iloc[i])
        res.equity_curve.append(equity)

        # Need enough history to make a decision. Decide today, fill at NEXT open.
        if i < 21 or i + 1 >= len(closes):
            continue

        window = closes.iloc[: i + 1]
        sma_fast = sma(window, 5)
        sma_slow = sma(window, 20)
        comp = conservative_score(window)
        bo_th = breakout_threshold(window, 20, buffer_pct=0.001)

        if sma_fast is None or sma_slow is None or comp is None or bo_th is None:
            continue

        last_close = float(closes.iloc[i])

        # Exit logic first (so we don't churn into a fresh buy on the same bar)
        if shares > 0:
            exit_signal = (sma_fast < sma_slow) or (comp <= 0.0)
            if exit_signal:
                fill = float(opens.iloc[i + 1])
                pnl = (fill / entry_price - 1.0) if entry_price else 0.0
                res.trades.append(
                    Trade(
                        symbol=symbol,
                        strategy="old",
                        entry_idx=entry_idx if entry_idx is not None else i,
                        entry_price=entry_price or fill,
                        exit_idx=i + 1,
                        exit_price=fill,
                        bars_held=(i + 1 - (entry_idx or i)),
                        pnl_pct=pnl,
                        reason="trend_or_momentum_fail",
                    )
                )
                cash = shares * fill
                shares = 0.0
                entry_price = None
                entry_idx = None
            continue

        # Entry logic
        bullish = (sma_fast > sma_slow) and (comp > 0.0) and (last_close > bo_th)
        if bullish:
            fill = float(opens.iloc[i + 1])
            if fill <= 0:
                continue
            shares = cash / fill
            cash = 0.0
            entry_price = fill
            entry_idx = i + 1

    # Close any open position at the last close so equity is clean
    if shares > 0:
        last_close = float(closes.iloc[-1])
        pnl = (last_close / entry_price - 1.0) if entry_price else 0.0
        res.trades.append(
            Trade(
                symbol=symbol,
                strategy="old",
                entry_idx=entry_idx or len(closes) - 1,
                entry_price=entry_price or last_close,
                exit_idx=len(closes) - 1,
                exit_price=last_close,
                bars_held=len(closes) - 1 - (entry_idx or len(closes) - 1),
                pnl_pct=pnl,
                reason="end_of_window",
            )
        )

    return res


# ------------------------------------------------------------
# Strategy: NEW (bars-based, conservative additions)
# ------------------------------------------------------------
def run_new_strategy(
    symbol: str,
    df: pd.DataFrame,
    spy_df: Optional[pd.DataFrame],
    *,
    atr_period: int = 14,
    atr_stop_mult: float = 2.0,
    atr_take_mult: float = 4.0,
    min_hold_bars: int = 3,
    cooldown_bars: int = 2,
    breakout_lookback: int = 20,
    breakout_buffer: float = 0.002,
) -> StrategyResult:
    """
    Conservative bars-based strategy:
      - Regime filter: only enter when SPY > SMA(50)
      - Entry: composite momentum > 0 AND price > 20-bar high * (1+buffer)
      - Exit: ATR-based stop or take, OR composite momentum drops below 0
      - Minimum hold: don't exit before `min_hold_bars`
      - Cooldown: wait `cooldown_bars` after exit before re-entering
    """
    res = StrategyResult(symbol=symbol, strategy="new")
    closes = df["close"].astype(float).reset_index(drop=True)
    opens = df["open"].astype(float).reset_index(drop=True)

    if len(closes) < 60:
        res.equity_curve = [1.0] * max(1, len(closes))
        return res

    spy_closes: Optional[pd.Series] = None
    if spy_df is not None and not spy_df.empty:
        spy_closes = spy_df["close"].astype(float).reset_index(drop=True)

    cash = 1.0
    shares = 0.0
    entry_price: Optional[float] = None
    entry_idx: Optional[int] = None
    cooldown_until: int = -1
    last_atr: Optional[float] = None

    for i in range(len(closes)):
        equity = cash + shares * float(closes.iloc[i])
        res.equity_curve.append(equity)

        if i < 50 or i + 1 >= len(closes):
            continue

        window_df = df.iloc[: i + 1]
        window_close = closes.iloc[: i + 1]

        # Regime filter
        spy_ok = True
        if spy_closes is not None:
            n = min(len(spy_closes), i + 1)
            if n >= 50:
                above = is_above_sma(spy_closes.iloc[:n], 50)
                spy_ok = bool(above) if above is not None else False

        comp = conservative_score(window_close)
        bo_th = breakout_threshold(window_close, breakout_lookback, breakout_buffer)
        a = atr(window_df, atr_period)
        last_close = float(closes.iloc[i])

        if comp is None or bo_th is None or a is None:
            continue

        if shares > 0:
            assert entry_price is not None and entry_idx is not None
            held = i - entry_idx
            stop = entry_price - atr_stop_mult * (last_atr or a)
            take = entry_price + atr_take_mult * (last_atr or a)

            hit_stop = last_close <= stop
            hit_take = last_close >= take
            momo_dead = comp <= 0.0

            if held >= min_hold_bars and (hit_stop or hit_take or momo_dead):
                fill = float(opens.iloc[i + 1])
                pnl = (fill / entry_price - 1.0)
                reason = "atr_stop" if hit_stop else ("atr_take" if hit_take else "momo_fail")
                res.trades.append(
                    Trade(
                        symbol=symbol,
                        strategy="new",
                        entry_idx=entry_idx,
                        entry_price=entry_price,
                        exit_idx=i + 1,
                        exit_price=fill,
                        bars_held=(i + 1 - entry_idx),
                        pnl_pct=pnl,
                        reason=reason,
                    )
                )
                cash = shares * fill
                shares = 0.0
                entry_price = None
                entry_idx = None
                cooldown_until = (i + 1) + cooldown_bars
                last_atr = None
            continue

        if i < cooldown_until:
            continue

        bullish = spy_ok and (comp > 0.0) and (last_close > bo_th)
        if bullish:
            fill = float(opens.iloc[i + 1])
            if fill <= 0:
                continue
            shares = cash / fill
            cash = 0.0
            entry_price = fill
            entry_idx = i + 1
            last_atr = a

    if shares > 0 and entry_price is not None and entry_idx is not None:
        last_close = float(closes.iloc[-1])
        pnl = (last_close / entry_price - 1.0)
        res.trades.append(
            Trade(
                symbol=symbol,
                strategy="new",
                entry_idx=entry_idx,
                entry_price=entry_price,
                exit_idx=len(closes) - 1,
                exit_price=last_close,
                bars_held=len(closes) - 1 - entry_idx,
                pnl_pct=pnl,
                reason="end_of_window",
            )
        )

    return res


# ------------------------------------------------------------
# CLI
# ------------------------------------------------------------
def _print_summary(rows: List[Dict[str, Any]]) -> None:
    if not rows:
        print("[backtest_public] no results to print")
        return

    cols = ["symbol", "strategy", "trades", "win_rate", "avg_win", "avg_loss",
            "max_drawdown", "profit_factor", "total_return"]
    widths = {c: max(len(c), 10) for c in cols}

    def fmt(v: Any, c: str) -> str:
        if isinstance(v, float):
            if c in ("win_rate", "avg_win", "avg_loss", "max_drawdown", "total_return"):
                return f"{v*100:+7.2f}%"
            return f"{v:8.2f}"
        return str(v)

    header = "  ".join(c.ljust(widths[c]) for c in cols)
    print(header)
    print("-" * len(header))
    for r in rows:
        print("  ".join(fmt(r.get(c), c).ljust(widths[c]) for c in cols))


def main() -> None:
    p = argparse.ArgumentParser(description="Public-bars backtest harness")
    p.add_argument("--symbols", default=",".join(DEFAULT_SYMBOLS),
                   help=f"Comma-separated symbols. Default: {','.join(DEFAULT_SYMBOLS)}")
    p.add_argument("--period", default="YEAR",
                   help="Public period: DAY|WEEK|MONTH|QUARTER|HALF_YEAR|YEAR|FIVE_YEARS|YTD")
    p.add_argument("--source", choices=["public", "yfinance"], default="public",
                   help="Where to pull bars from")
    p.add_argument("--timeout", type=float, default=12.0,
                   help="Per-request HTTP timeout in seconds (Public source). "
                        "Default 12s. yfinance source ignores this.")
    p.add_argument("--sleep", type=float, default=1.0,
                   help="Seconds to sleep between Public symbol requests. "
                        "Default 1.0. Ignored for yfinance.")
    p.add_argument("--out-summary", default="backtest_public_summary.csv")
    p.add_argument("--out-trades", default="backtest_public_trades.csv")
    args = p.parse_args()

    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    if "SPY" not in symbols:
        # Always include SPY for the regime filter on the new strategy
        symbols = ["SPY"] + symbols

    print(f"[backtest_public] source={args.source} period={args.period} "
          f"timeout={args.timeout}s sleep={args.sleep}s symbols={symbols}")

    bars_by_symbol: Dict[str, pd.DataFrame] = {}
    total = len(symbols)
    for idx, sym in enumerate(symbols, start=1):
        print(f"[backtest_public] fetching {sym} {idx}/{total} (source={args.source})...",
              flush=True)
        try:
            df = load_bars(sym, args.period, args.source, timeout=args.timeout)
        except KeyboardInterrupt:
            # Honor Ctrl+C immediately rather than swallowing it
            print(f"[backtest_public] interrupted while fetching {sym}; stopping early")
            break
        except Exception as e:
            # One bad symbol must not stop the whole backtest
            print(f"[backtest_public] {sym}: FETCH_ERROR {type(e).__name__}: {e} — skipping")
            df = None

        if df is None or df.empty:
            print(f"[backtest_public] {sym}: no bars; skipping")
        else:
            bars_by_symbol[sym] = df
            print(f"[backtest_public] {sym}: rows={len(df)}")

        # Politeness sleep between Public requests so we don't hammer the API.
        # yfinance has its own backoff so we skip the sleep there.
        if args.source == "public" and idx < total and args.sleep > 0:
            time.sleep(args.sleep)

    spy_df = bars_by_symbol.get("SPY")

    summary_rows: List[Dict[str, Any]] = []
    trade_rows: List[Dict[str, Any]] = []

    for sym, df in bars_by_symbol.items():
        old = run_old_strategy(sym, df)
        new = run_new_strategy(sym, df, spy_df)
        for r in (old, new):
            summary_rows.append(r.summary())
            for t in r.trades:
                trade_rows.append({
                    "symbol": t.symbol,
                    "strategy": t.strategy,
                    "entry_idx": t.entry_idx,
                    "entry_price": t.entry_price,
                    "exit_idx": t.exit_idx,
                    "exit_price": t.exit_price,
                    "bars_held": t.bars_held,
                    "pnl_pct": t.pnl_pct,
                    "reason": t.reason,
                })

    # Write CSVs
    if summary_rows:
        with open(args.out_summary, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(summary_rows[0].keys()))
            w.writeheader()
            for r in summary_rows:
                w.writerow(r)
        print(f"[backtest_public] wrote {args.out_summary}")
    if trade_rows:
        with open(args.out_trades, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(trade_rows[0].keys()))
            w.writeheader()
            for r in trade_rows:
                w.writerow(r)
        print(f"[backtest_public] wrote {args.out_trades}")

    print()
    _print_summary(summary_rows)


if __name__ == "__main__":
    main()
