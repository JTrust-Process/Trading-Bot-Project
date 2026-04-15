"""
market_data.py — Daily OHLCV bars via Alpha Vantage free API.

Free tier: 25 requests/day, 1 req/sec burst limit.
Requires ALPHA_VANTAGE_API_KEY environment variable.
Sign up free at: https://www.alphavantage.co/support/#api-key

Note: free tier only supports outputsize=compact (last 100 bars).
The regime detection uses a 50-day SMA instead of 200-day to fit within this limit.
"""

import os
import time
import requests
import pandas as pd
from typing import Optional

_session = requests.Session()
_AV_BASE = "https://www.alphavantage.co/query"
_last_call_time = 0.0  # track last API call for rate limiting


def _throttle():
    """Ensure at least 1.2 seconds between API calls (free tier: 1 req/sec)."""
    global _last_call_time
    elapsed = time.time() - _last_call_time
    if elapsed < 1.2:
        time.sleep(1.2 - elapsed)
    _last_call_time = time.time()


def get_daily_bars(symbol: str, outputsize: int = 100) -> Optional[pd.DataFrame]:
    """
    Fetch daily OHLCV bars from Alpha Vantage.

    Returns a DataFrame with columns: open, high, low, close, volume
    sorted oldest → newest (up to outputsize bars), or None on failure.

    Note: free tier is capped at 100 bars (compact). outputsize > 100
    will still only return 100 bars on the free plan.
    """
    api_key = os.getenv("ALPHA_VANTAGE_API_KEY", "")
    if not api_key:
        print(f"[market_data] ERROR: ALPHA_VANTAGE_API_KEY not set")
        return None

    # Free tier only supports compact (100 bars) — full is premium
    params = {
        "function": "TIME_SERIES_DAILY",
        "symbol": symbol,
        "outputsize": "compact",
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
                msg = data.get("Note") or data.get("Information", "")
                print(f"[market_data] AV rate limit for {symbol}, waiting 60s...")
                time.sleep(60)
                continue

            ts = data.get("Time Series (Daily)", {})
            if not ts:
                return None

            rows = []
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
            # Cap at actual available bars
            df = df.tail(min(outputsize, 100)).reset_index(drop=True)

            return df if len(df) > 0 else None

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