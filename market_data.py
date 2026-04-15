"""
market_data.py — Daily OHLCV bars via Alpha Vantage free API.

Free tier: 25 requests/day, no rate limit per minute issues.
Requires ALPHA_VANTAGE_API_KEY environment variable.
Sign up free at: https://www.alphavantage.co/support/#api-key
"""

import os
import time
import requests
import pandas as pd
from typing import Optional


_session = requests.Session()
_AV_BASE = "https://www.alphavantage.co/query"


def get_daily_bars(symbol: str, outputsize: int = 250) -> Optional[pd.DataFrame]:
    """
    Fetch daily OHLCV bars from Alpha Vantage.

    Returns a DataFrame with columns: open, high, low, close, volume
    sorted oldest → newest (up to outputsize bars), or None on failure.
    """
    api_key = os.getenv("ALPHA_VANTAGE_API_KEY", "")
    if not api_key:
        print(f"[market_data] ERROR: ALPHA_VANTAGE_API_KEY not set")
        return None

    # outputcompact = last 100 bars, outputfull = last 20+ years
    size = "compact" if outputsize <= 100 else "full"

    params = {
        "function": "TIME_SERIES_DAILY",
        "symbol": symbol,
        "outputsize": size,
        "apikey": api_key,
        "datatype": "json",
    }

    max_retries = 3
    backoff = [5, 10, 20]

    for attempt in range(max_retries):
        try:
            resp = _session.get(_AV_BASE, params=params, timeout=30)

            if resp.status_code != 200:
                if attempt < max_retries - 1:
                    time.sleep(backoff[attempt])
                    continue
                return None

            data = resp.json()

            # Check for API errors
            if "Error Message" in data:
                print(f"[market_data] AV error for {symbol}: {data['Error Message']}")
                return None

            if "Note" in data:
                # Rate limit hit
                print(f"[market_data] AV rate limit hit, waiting...")
                time.sleep(60)
                continue

            if "Information" in data:
                print(f"[market_data] AV info: {data['Information']}")
                return None

            ts = data.get("Time Series (Daily)", {})
            if not ts:
                return None

            rows = []
            for date_str, vals in ts.items():
                rows.append({
                    "date": date_str,
                    "open":   float(vals["1. open"]),
                    "high":   float(vals["2. high"]),
                    "low":    float(vals["3. low"]),
                    "close":  float(vals["4. close"]),
                    "volume": float(vals["5. volume"]),
                })

            df = pd.DataFrame(rows)
            df = df.sort_values("date").reset_index(drop=True)
            df = df.drop(columns=["date"])
            df = df.tail(outputsize).reset_index(drop=True)

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