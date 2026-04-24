"""
market_data.py — Daily OHLCV bars via Polygon.io free tier.

Free tier: unlimited daily calls, 5 calls/minute.
Requires POLYGON_API_KEY environment variable.
Sign up free at: https://polygon.io/

In-memory cache so each symbol is only fetched once per bot run.
"""

import os
import time
import requests
import pandas as pd
from typing import Optional, Any
from datetime import datetime, timedelta

_session = requests.Session()
_POLYGON_BASE = "https://api.polygon.io/v2/aggs/ticker"
_last_call_time = 0.0

# In-memory cache: symbol -> (timestamp, DataFrame)
_cache: dict[str, tuple[float, pd.DataFrame]] = {}
_CACHE_TTL = 3600  # 1 hour


def _throttle() -> None:
    """Enforce 13 seconds between calls — stays safely under 5/min Polygon free tier limit."""
    global _last_call_time
    elapsed = time.time() - _last_call_time
    if elapsed < 13.0:
        time.sleep(13.0 - elapsed)
    _last_call_time = time.time()


def get_daily_bars(symbol: str, outputsize: int = 100) -> Optional[pd.DataFrame]:
    """
    Fetch daily OHLCV bars from Polygon.io.

    Returns a DataFrame with columns: open, high, low, close, volume
    sorted oldest → newest, or None on failure.
    """
    symbol = symbol.upper()

    # Return cached data if fresh
    if symbol in _cache:
        cache_entry: tuple[float, pd.DataFrame] = _cache[symbol]
        cached_time: float = cache_entry[0]
        cached_df: pd.DataFrame = cache_entry[1]
        if time.time() - cached_time < _CACHE_TTL:
            return cached_df.tail(min(outputsize, len(cached_df))).reset_index(drop=True)

    api_key = os.getenv("POLYGON_API_KEY", "")
    if not api_key:
        print(f"[market_data] ERROR: POLYGON_API_KEY not set")
        return None

    # Fetch last 200 trading days (~10 months) to cover 50-day SMA + momentum
    end_date = datetime.utcnow().strftime("%Y-%m-%d")
    start_date = (datetime.utcnow() - timedelta(days=300)).strftime("%Y-%m-%d")

    url = f"{_POLYGON_BASE}/{symbol}/range/1/day/{start_date}/{end_date}"
    params = {
        "adjusted": "true",
        "sort": "asc",
        "limit": 200,
        "apiKey": api_key,
    }

    max_retries = 3
    backoff = [5, 10, 20]

    for attempt in range(max_retries):
        try:
            _throttle()
            resp = _session.get(url, params=params, timeout=30)

            if resp.status_code == 429:
                print(f"[market_data] Polygon rate limit for {symbol}, waiting 60s...")
                time.sleep(60)
                continue

            if resp.status_code == 403:
                print(f"[market_data] Polygon auth error — check POLYGON_API_KEY")
                return None

            if resp.status_code != 200:
                if attempt < max_retries - 1:
                    time.sleep(backoff[attempt])
                    continue
                return None

            data: dict[str, Any] = resp.json()

            if data.get("status") == "ERROR":
                print(f"[market_data] Polygon error for {symbol}: {data.get('error')}")
                return None

            results: list[dict[str, Any]] = data.get("results") or []
            if not results:
                return None

            rows: list[dict[str, Any]] = []
            for r in results:
                rows.append({
                    "open":   float(r.get("o", 0)),
                    "high":   float(r.get("h", 0)),
                    "low":    float(r.get("l", 0)),
                    "close":  float(r.get("c", 0)),
                    "volume": float(r.get("v", 0)),
                })

            df = pd.DataFrame(rows)
            df = df.dropna(subset=["close"])
            df = df[df["close"] > 0].reset_index(drop=True)

            # Store full history in cache
            _cache[symbol] = (time.time(), df)

            return df.tail(min(outputsize, len(df))).reset_index(drop=True)

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