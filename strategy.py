import numpy as np
import pandas as pd
import os

ATR_PERIOD = int(os.getenv("ATR_PERIOD", 14))
TREND_FAST = int(os.getenv("TREND_FAST", 5))
TREND_SLOW = int(os.getenv("TREND_SLOW", 20))


def calculate_atr(df):
    high = df["high"]
    low = df["low"]
    close = df["close"]

    tr1 = high - low
    tr2 = (high - close.shift()).abs()
    tr3 = (low - close.shift()).abs()

    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    atr = tr.rolling(ATR_PERIOD).mean()

    return atr.iloc[-1]


def trend_strength(df):
    sma_fast = df["close"].rolling(TREND_FAST).mean()
    sma_slow = df["close"].rolling(TREND_SLOW).mean()

    strength = abs(sma_fast.iloc[-1] - sma_slow.iloc[-1]) / df["close"].iloc[-1]
    return strength


def generate_signal(df):
    sma_fast = df["close"].rolling(TREND_FAST).mean()
    sma_slow = df["close"].rolling(TREND_SLOW).mean()

    if sma_fast.iloc[-1] > sma_slow.iloc[-1]:
        return "BUY"
    elif sma_fast.iloc[-1] < sma_slow.iloc[-1]:
        return "SELL"
    else:
        return None
