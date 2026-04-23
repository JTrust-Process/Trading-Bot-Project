"""
market_data.py — Daily OHLCV bars via Alpha Vantage free API.

Free tier: 25 requests/day, 1 req/sec burst limit.
Uses in-memory cache so each symbol is only fetched once per bot run,
keeping total API calls well within the 25/day limit.

Sign up free at: https://www.alphavantage.co/support/#api-key
"""

import os
import time
import requests
import pandas as pd
from typing import Optional, Any

_session = requests.Session()
_AV_BASE = "https://www.alphavantage.co/query"
_last_call_time = 0.0

# In-memory cache: symbol -> (timestamp, DataFrame)
# Cached for 1 hour so a single bot run reuses data across all callers
_cache: dict[str, tuple[float, pd.DataFrame]] = {}
_CACHE_TTL = 3600  # seconds


def _throttle():
    """Enforce 1.2s between API calls (free tier: 1 req/sec)."""
    global _last_call_time
    elapsed = time.time() - _last_call_time
    if elapsed < 1.2:
        time.sleep(1.2 - elapsed)
    _last_call_time = time.time()


def get_daily_bars(symbol: str, outputsize: int = 100) -> Optional[pd.DataFrame]:
    """
    Fetch daily OHLCV bars from Alpha Vantage with in-memory caching.

    Returns a DataFrame with columns: open, high, low, close, volume
    sorted oldest → newest (up to 100 bars on free tier), or None on failure.
    """
    symbol = symbol.upper()

    # Return cached data if fresh
    if symbol in _cache:
        cache_entry: tuple[float, pd.DataFrame] = _cache[symbol]
        cached_time: float = cache_entry[0]
        cached_df: pd.DataFrame = cache_entry[1]
        if time.time() - cached_time < _CACHE_TTL:
            actual = min(outputsize, len(cached_df))
            return cached_df.tail(actual).reset_index(drop=True)

    api_key = os.getenv("ALPHA_VANTAGE_API_KEY", "")
    if not api_key:
        print(f"[market_data] ERROR: ALPHA_VANTAGE_API_KEY not set")
        return None

    params = {
        "function": "TIME_SERIES_DAILY",
        "symbol": symbol,
        "outputsize": "compact",  # 100 bars — free tier only
        "apikey": api_key,
        "datatype": "json",
    }

    max_retries = 3
    backoff = [5, 10, 20]

    for attempt in range(max_retries):
        try:
            _throttle()
            resp = _session.get(_AV_BASE, params=params, timeout=30)

            if resp.status_code != 200:
                if attempt < max_retries - 1:
                    time.sleep(backoff[attempt])
                    continue
                return None

            data = resp.json()

            if "Error Message" in data:
                print(f"[market_data] AV error for {symbol}: {data['Error Message']}")
                return None

            if "Note" in data or "Information" in data:
                print(f"[market_data] AV rate limit for {symbol}, waiting 65s...")
                time.sleep(65)
                continue

            ts = data.get("Time Series (Daily)", {})
            if not ts:
                return None

            rows: list[dict[str, Any]] = []
            for date_str, vals in ts.items():
                rows.append({
                    "date":   date_str,
                    "open":   float(vals["1. open"]),
                    "high":   float(vals["2. high"]),
                    "low":    float(vals["3. low"]),
                    "close":  float(vals["4. close"]),
                    "volume": float(vals["5. volume"]),
                })

            df = pd.DataFrame(rows)
            df = df.sort_values("date").reset_index(drop=True)
            df = df.drop(columns=["date"])

            # Store full 100 bars in cache
            _cache[symbol] = (time.time(), df)

            actual = min(outputsize, len(df))
            return df.tail(actual).reset_index(drop=True)

        except requests.exceptions.Timeout:
            if attempt < max_retries - 1:
                time.sleep(backoff[attempt])
                continue
            return None
        except Exception as e:
            print(f"[market_data] Exception for {symbol}: {e}")
            if attempt < max_retries - 1:
                time.sleep(backoff[attempt])
                continue
            return None

    return None