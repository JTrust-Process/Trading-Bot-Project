"""
breakout.py — Volatility breakout signal engine.

Detects when price breaks above a recent consolidation range.

Bull regime: tighter threshold, full sizing
Bear regime:  wider threshold (must clear more convincingly), half sizing
"""

from __future__ import annotations

import os
from typing import Optional
from dataclasses import dataclass

import pandas as pd

from market_data import get_daily_bars


# ── Config (overridable via env) ──────────────────────────────────────────────
BREAKOUT_LOOKBACK          = int(float(os.getenv("BREAKOUT_LOOKBACK",           "20")))
BREAKOUT_THRESHOLD_BULL    = float(os.getenv("BREAKOUT_THRESHOLD_BULL",         "0.1"))
BREAKOUT_THRESHOLD_BEAR    = float(os.getenv("BREAKOUT_THRESHOLD_BEAR",         "0.3"))
BREAKOUT_BEAR_SIZE_FACTOR  = float(os.getenv("BREAKOUT_BEAR_SIZE_FACTOR",       "0.5"))


@dataclass
class BreakoutResult:
    symbol:        str
    signal:        bool          # True = breakout detected
    regime:        str           # "bull" or "bear"
    price:         Optional[float] = None
    recent_high:   Optional[float] = None
    recent_low:    Optional[float] = None
    range_size:    Optional[float] = None
    threshold:     Optional[float] = None   # actual price level needed
    size_factor:   float = 1.0              # multiply alloc by this
    reason:        str = ""


def check_breakout(
    symbol: str,
    regime: Optional[str],
    momentum_score: float = 0.0,
    lookback: int = BREAKOUT_LOOKBACK,
) -> BreakoutResult:
    """
    Check if a symbol is breaking out of its recent range.

    Bear regime rules:
      - Use wider threshold (harder to trigger)
      - Block if momentum score is deeply negative (< -0.03)
      - Cut position size by BREAKOUT_BEAR_SIZE_FACTOR
    """
    r = regime or "bull"

    df = get_daily_bars(symbol, lookback + 5)
    if df is None or len(df) < lookback + 1:
        return BreakoutResult(symbol=symbol, signal=False, regime=r,
                              reason="insufficient data")

    closes = df["close"]
    price = float(closes.iloc[-1])

    # Use the prior N bars (exclude today) to avoid look-ahead
    hist = closes.iloc[-(lookback + 1):-1]
    if len(hist) < lookback:
        return BreakoutResult(symbol=symbol, signal=False, regime=r,
                              reason="insufficient history")

    recent_high = float(hist.max())
    recent_low  = float(hist.min())
    range_size  = recent_high - recent_low

    if range_size <= 0 or recent_high <= 0:
        return BreakoutResult(symbol=symbol, signal=False, regime=r,
                              reason="zero range")

    # Choose threshold and size factor based on regime
    if r == "bear":
        # In bear: block if momentum deeply negative
        if momentum_score < -0.03:
            return BreakoutResult(
                symbol=symbol, signal=False, regime=r,
                price=price, recent_high=recent_high, recent_low=recent_low,
                range_size=range_size,
                reason=f"bear+deep_negative_momentum score={momentum_score:.4f}",
            )
        threshold_pct = BREAKOUT_THRESHOLD_BEAR
        size_factor   = BREAKOUT_BEAR_SIZE_FACTOR
    else:
        threshold_pct = BREAKOUT_THRESHOLD_BULL
        size_factor   = 1.0

    breakout_level = recent_high + (threshold_pct * range_size)
    signal = price > breakout_level

    return BreakoutResult(
        symbol=symbol,
        signal=signal,
        regime=r,
        price=price,
        recent_high=recent_high,
        recent_low=recent_low,
        range_size=round(range_size, 4),
        threshold=round(breakout_level, 4),
        size_factor=size_factor,
        reason="breakout" if signal else f"price={price:.2f} below threshold={breakout_level:.2f}",
    )