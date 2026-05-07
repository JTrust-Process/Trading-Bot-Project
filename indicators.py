"""
indicators.py — Pure pandas/numpy strategy helpers.

These functions are intentionally small and side-effect free so they can be
called from both the live bot path AND the backtest harness without dragging
in HTTP, env vars, or Supabase.

Functions:
    sma(series, period)                 -> Simple Moving Average
    ema(series, period)                 -> Exponential Moving Average
    atr(df, period=14)                  -> Average True Range (Wilder)
    momentum_pct(series, lookback)      -> percentage return over `lookback` bars
    breakout_threshold(series, lookback,
                       buffer_pct=0.0)  -> price level above recent high
    is_above_sma(series, period)        -> bool: latest close > SMA(period)
    regime(series, fast=50, slow=200)   -> "bull" | "bear" via fast/slow SMA cross

Conventions:
    - Inputs are pandas Series of closes or DataFrames with 'high','low','close'.
    - Returns floats, bools, or strings — no DataFrames are mutated in place.
    - Returns None if there is not enough data.
"""

from __future__ import annotations

from typing import Optional

import numpy as np
import pandas as pd


# -----------------------------
# Moving averages
# -----------------------------
def sma(series: pd.Series, period: int) -> Optional[float]:
    """Simple Moving Average of the latest `period` values."""
    if period <= 0 or len(series) < period:
        return None
    val = float(series.tail(period).mean())
    if np.isnan(val):
        return None
    return val


def ema(series: pd.Series, period: int) -> Optional[float]:
    """
    Exponential Moving Average — uses pandas' adjust=False so it matches
    the standard recursive EMA most charting libraries use.
    """
    if period <= 0 or len(series) < period:
        return None
    e = series.ewm(span=period, adjust=False).mean()
    if e.empty:
        return None
    val = float(e.iloc[-1])
    if np.isnan(val):
        return None
    return val


# -----------------------------
# ATR (Wilder)
# -----------------------------
def atr(df: pd.DataFrame, period: int = 14) -> Optional[float]:
    """
    Average True Range using Wilder's smoothing.

    df must have columns: high, low, close.
    """
    needed = {"high", "low", "close"}
    if not needed.issubset(df.columns):
        return None
    if len(df) < period + 1:
        return None

    high = df["high"].astype(float)
    low = df["low"].astype(float)
    close = df["close"].astype(float)

    prev_close = close.shift(1)
    tr = pd.concat(
        [
            (high - low).abs(),
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)

    # Wilder's smoothing == EMA with alpha = 1/period
    atr_series = tr.ewm(alpha=1.0 / period, adjust=False).mean()
    if atr_series.empty:
        return None
    val = float(atr_series.iloc[-1])
    if np.isnan(val):
        return None
    return val


# -----------------------------
# Momentum
# -----------------------------
def momentum_pct(series: pd.Series, lookback: int) -> Optional[float]:
    """
    Percentage return over the last `lookback` bars.
    Returns None if there isn't enough data, or the start price is <= 0.
    """
    if lookback <= 0 or len(series) < lookback + 1:
        return None
    try:
        end = float(series.iloc[-1])
        start = float(series.iloc[-(lookback + 1)])
    except Exception:
        return None
    if start <= 0:
        return None
    return (end - start) / start


# -----------------------------
# Breakout
# -----------------------------
def breakout_threshold(
    series: pd.Series,
    lookback: int,
    buffer_pct: float = 0.0,
) -> Optional[float]:
    """
    Compute the breakout level = recent_high * (1 + buffer_pct), where
    `recent_high` is the max of the prior `lookback` bars (excluding today).
    """
    if lookback <= 0 or len(series) < lookback + 1:
        return None
    hist = series.iloc[-(lookback + 1):-1]
    if len(hist) < lookback:
        return None
    recent_high = float(hist.max())
    if np.isnan(recent_high) or recent_high <= 0:
        return None
    return recent_high * (1.0 + float(buffer_pct))


def is_breakout(
    series: pd.Series,
    lookback: int,
    buffer_pct: float = 0.0,
) -> Optional[bool]:
    """True if the latest close exceeds breakout_threshold."""
    th = breakout_threshold(series, lookback, buffer_pct)
    if th is None or len(series) == 0:
        return None
    last = float(series.iloc[-1])
    return last > th


# -----------------------------
# Regime / trend filters
# -----------------------------
def is_above_sma(series: pd.Series, period: int) -> Optional[bool]:
    """True if latest close is above SMA(period)."""
    avg = sma(series, period)
    if avg is None or len(series) == 0:
        return None
    return float(series.iloc[-1]) > avg


def regime(series: pd.Series, fast: int = 50, slow: int = 200) -> Optional[str]:
    """
    Return "bull" if SMA(fast) > SMA(slow), "bear" if SMA(fast) < SMA(slow),
    None if there isn't enough data.
    """
    f = sma(series, fast)
    s = sma(series, slow)
    if f is None or s is None:
        return None
    if f > s:
        return "bull"
    if f < s:
        return "bear"
    return "neutral"


# -----------------------------
# Composite — a conservative bars-based score
# -----------------------------
def conservative_score(
    closes: pd.Series,
    *,
    short_lookback: int = 5,
    medium_lookback: int = 10,
    long_lookback: int = 20,
    weights: tuple[float, float, float] = (0.3, 0.4, 0.3),
) -> Optional[float]:
    """
    Weighted composite of short / medium / long momentum returns.
    Returns None if any of the three lookbacks can't be computed.

    Defaults mirror the live bot's momentum.py weights so the comparison is
    apples-to-apples in the backtest harness.
    """
    r_s = momentum_pct(closes, short_lookback)
    r_m = momentum_pct(closes, medium_lookback)
    r_l = momentum_pct(closes, long_lookback)
    if r_s is None or r_m is None or r_l is None:
        return None
    w_s, w_m, w_l = weights
    total = float(w_s + w_m + w_l)
    if total <= 0:
        return None
    return (r_s * w_s + r_m * w_m + r_l * w_l) / total
