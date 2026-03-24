import numpy as np
import pandas as pd
import os


def calculate_atr(df, period=14):
    high = df["high"]
    low = df["low"]
    close = df["close"]

    tr1 = high - low
    tr2 = (high - close.shift()).abs()
    tr3 = (low - close.shift()).abs()

    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    atr = tr.rolling(period).mean()

    return atr.iloc[-1]


def trend_strength(df, fast=5, slow=20):
    if len(df) < slow + 2:
        return 0.0

    sma_fast = df["close"].rolling(fast).mean()
    sma_slow = df["close"].rolling(slow).mean()

    if pd.isna(sma_fast.iloc[-1]) or pd.isna(sma_slow.iloc[-1]):
        return 0.0

    strength = abs(sma_fast.iloc[-1] - sma_slow.iloc[-1]) / df["close"].iloc[-1]
    return float(strength)


def generate_signal(df):
    """Legacy signal function — kept for backtest.py compatibility."""
    fast = int(os.getenv("TREND_FAST", "5"))
    slow = int(os.getenv("TREND_SLOW", "20"))

    sma_fast = df["close"].rolling(fast).mean()
    sma_slow = df["close"].rolling(slow).mean()

    if sma_fast.iloc[-1] > sma_slow.iloc[-1]:
        return "BUY"
    elif sma_fast.iloc[-1] < sma_slow.iloc[-1]:
        return "SELL"
    else:
        return None