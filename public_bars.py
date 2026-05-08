"""
public_bars.py — Historical bars data layer using the Public.com API.

Reference: https://public.com/api/docs/resources/market-data/get-bars-v2

Endpoint:
    GET https://api.public.com/userapigateway/marketdata/historicdata/{symbol}/{period}

Periods supported by Public:
    DAY, WEEK, MONTH, QUARTER, HALF_YEAR, YEAR, FIVE_YEARS, YTD, SINCE_PURCHASE

Response shape (subject to change — we parse defensively):
    {
      "regularMarket": {"bars": [ {timestamp, open, close, high, low, value, volume,
                                    gainAmount, gainPercentage}, ... ]},
      "preMarket":     {"bars": [ ... ]},   # optional
      "afterMarket":   {"bars": [ ... ]}    # optional
    }

Numeric fields come back as strings on Public. We coerce them to floats and
log keys safely if anything looks off.

This module is *additive*. It does not replace `market_data.get_daily_bars`.
It produces the same DataFrame shape (columns: open, high, low, close, volume),
sorted oldest -> newest, so it can drop into existing strategy/momentum/breakout
code unchanged.

It DOES NOT execute any orders. It is read-only.
"""

from __future__ import annotations

import os
import time
from typing import Any, Dict, List, Optional

import requests
import pandas as pd
from dotenv import load_dotenv
load_dotenv()


# -----------------------------
# Constants
# -----------------------------
PUBLIC_AUTH_URL = "https://api.public.com/userapiauthservice/personal/access-tokens"
# Per Public docs (https://public.com/api/docs/resources/market-data/get-bars-v2):
#     GET /userapigateway/historicdata/{symbol}/{period}
PUBLIC_BARS_URL_TMPL = (
    "https://api.public.com/userapigateway/historicdata/{symbol}/{period}"
)

VALID_PERIODS = {
    "DAY",
    "WEEK",
    "MONTH",
    "QUARTER",
    "HALF_YEAR",
    "YEAR",
    "FIVE_YEARS",
    "YTD",
    "SINCE_PURCHASE",
}

# Required bar fields we expect. We accept missing volume/value gracefully.
_NUMERIC_KEYS = ("open", "high", "low", "close", "volume", "value")


# -----------------------------
# HTTP defaults
# -----------------------------
DEFAULT_TIMEOUT = 12.0  # seconds — short enough that a hung request gives up fast


# -----------------------------
# Token cache (process-local, never persisted)
# -----------------------------
_session = requests.Session()
_token_cache: Dict[str, Any] = {"token": None, "expires_at": 0.0}


def _redacted_status(resp: requests.Response) -> str:
    """Build a debug-safe status line — never include the token or full body."""
    try:
        body_excerpt = resp.text[:200].replace("\n", " ")
    except Exception:
        body_excerpt = "<unreadable>"
    return f"status={resp.status_code} body[:200]={body_excerpt!r}"


def get_access_token(
    secret: Optional[str] = None,
    validity_minutes: int = 60,
    force_refresh: bool = False,
) -> Optional[str]:
    """
    Fetch (and cache) a Public.com access token using the same auth flow
    the live bot uses. Reads PUBLIC_SECRET from env when `secret` is None.

    Returns None on failure — callers should handle that gracefully.
    """
    secret = secret if secret is not None else os.getenv("PUBLIC_SECRET", "")
    if not secret:
        print("[public_bars] ERROR: PUBLIC_SECRET not set")
        return None

    now = time.time()
    if (
        not force_refresh
        and _token_cache.get("token")
        and now < float(_token_cache.get("expires_at") or 0.0)
    ):
        return str(_token_cache["token"])

    try:
        resp = _session.post(
            PUBLIC_AUTH_URL,
            headers={"Content-Type": "application/json"},
            json={"secret": secret, "validityInMinutes": int(validity_minutes)},
            timeout=DEFAULT_TIMEOUT,
        )
    except requests.exceptions.Timeout:
        print(f"[public_bars] auth TIMEOUT after {DEFAULT_TIMEOUT}s")
        return None
    except requests.exceptions.ConnectionError as e:
        print(f"[public_bars] auth CONNECTION_ERROR — {e}")
        return None
    except requests.RequestException as e:
        print(f"[public_bars] auth request failed: {e}")
        return None

    if resp.status_code != 200:
        print(f"[public_bars] auth failed {_redacted_status(resp)}")
        return None

    try:
        data = resp.json()
    except ValueError:
        print(f"[public_bars] auth returned non-JSON {_redacted_status(resp)}")
        return None

    token = data.get("accessToken")
    if not token:
        # Don't leak any payload that might contain sensitive data
        print(f"[public_bars] auth response missing accessToken (keys={list(data.keys())})")
        return None

    # Refresh a little before actual expiry
    _token_cache["token"] = token
    _token_cache["expires_at"] = now + max(60.0, (validity_minutes - 2) * 60.0)
    return str(token)


# -----------------------------
# Bar parsing
# -----------------------------
def _coerce_bar(raw: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """
    Convert one raw bar dict from Public into a normalized OHLCV dict.

    Returns None if essential fields (open/high/low/close) are missing or
    cannot be parsed.
    """
    if not isinstance(raw, dict):
        return None

    out: Dict[str, Any] = {"timestamp": raw.get("timestamp")}
    for k in _NUMERIC_KEYS:
        v = raw.get(k)
        if v is None:
            continue
        try:
            out[k] = float(v)
        except (TypeError, ValueError):
            # Numeric coercion failed; treat as missing
            continue

    # Require at least open/high/low/close. Volume can be missing.
    for k in ("open", "high", "low", "close"):
        if k not in out:
            return None

    # Default volume to 0 if missing — keeps downstream code happy.
    out.setdefault("volume", 0.0)
    return out


def _extract_bars_section(payload: Dict[str, Any], section: str) -> List[Dict[str, Any]]:
    """Pull `bars` list out of regularMarket / preMarket / afterMarket safely."""
    block = payload.get(section)
    if not isinstance(block, dict):
        return []
    bars = block.get("bars")
    if not isinstance(bars, list):
        return []
    return [b for b in bars if isinstance(b, dict)]


def _bars_to_df(bars: List[Dict[str, Any]]) -> pd.DataFrame:
    """
    Convert normalized bar dicts to the same DataFrame shape that
    `market_data.get_daily_bars` returns: columns=[open, high, low, close, volume],
    sorted oldest -> newest.
    """
    if not bars:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])

    df = pd.DataFrame(bars)

    # Sort by timestamp if available so behavior is deterministic
    if "timestamp" in df.columns:
        df = df.sort_values("timestamp").reset_index(drop=True)

    keep = ["open", "high", "low", "close", "volume"]
    for col in keep:
        if col not in df.columns:
            df[col] = 0.0

    df = df[keep].copy()
    df = df.dropna(subset=["close"])
    df = df[df["close"] > 0].reset_index(drop=True)
    return df


# -----------------------------
# Public API
# -----------------------------


def get_public_bars(
    symbol: str,
    period: str = "YEAR",
    *,
    include_pre_market: bool = False,
    include_after_market: bool = False,
    token: Optional[str] = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> Optional[pd.DataFrame]:
    """
    Fetch historical bars for a symbol from Public.

    Returns a DataFrame with columns [open, high, low, close, volume],
    sorted oldest -> newest. Returns None on any hard failure (auth, HTTP,
    malformed response). Returns an empty DataFrame if the response was
    valid but contained no bars.

    Args:
        symbol: Ticker like "SPY", "AAPL" (case-insensitive).
        period: One of VALID_PERIODS. Defaults to "YEAR".
        include_pre_market: If True, append preMarket bars.
        include_after_market: If True, append afterMarket bars.
        token: Optional override token. If None, fetched/cached automatically.
        timeout: Per-request HTTP timeout in seconds.
    """
    if not symbol:
        print("[public_bars] ERROR: empty symbol")
        return None

    period = (period or "YEAR").upper().strip()
    if period not in VALID_PERIODS:
        print(f"[public_bars] ERROR: invalid period {period!r}; "
              f"valid: {sorted(VALID_PERIODS)}")
        return None

    sym = symbol.upper().strip()

    if token is None:
        token = get_access_token()
        if token is None:
            return None

    url = PUBLIC_BARS_URL_TMPL.format(symbol=sym, period=period)
    headers = {"Authorization": f"Bearer {token}"}

    try:
        resp = _session.get(url, headers=headers, timeout=timeout)
    except requests.exceptions.Timeout:
        print(f"[public_bars] {sym} {period}: TIMEOUT after {timeout}s — skipping")
        return None
    except requests.exceptions.ConnectionError as e:
        print(f"[public_bars] {sym} {period}: CONNECTION_ERROR — {e}")
        return None
    except requests.RequestException as e:
        print(f"[public_bars] {sym} {period}: request failed: {e}")
        return None

    if resp.status_code == 401:
        # Token might have expired mid-flight — try once more with a fresh token
        new_token = get_access_token(force_refresh=True)
        if new_token is None:
            print(f"[public_bars] {sym} {period}: auth refused, no token available")
            return None
        try:
            resp = _session.get(
                url,
                headers={"Authorization": f"Bearer {new_token}"},
                timeout=timeout,
            )
        except requests.exceptions.Timeout:
            print(f"[public_bars] {sym} {period}: retry TIMEOUT after {timeout}s — skipping")
            return None
        except requests.exceptions.ConnectionError as e:
            print(f"[public_bars] {sym} {period}: retry CONNECTION_ERROR — {e}")
            return None
        except requests.RequestException as e:
            print(f"[public_bars] {sym} {period}: retry failed: {e}")
            return None

    if resp.status_code != 200:
        print(f"[public_bars] {sym} {period}: {_redacted_status(resp)}")
        return None

    try:
        payload: Dict[str, Any] = resp.json()
    except ValueError:
        print(f"[public_bars] {sym} {period}: non-JSON response "
              f"{_redacted_status(resp)}")
        return None

    if not isinstance(payload, dict):
        print(f"[public_bars] {sym} {period}: unexpected payload type "
              f"{type(payload).__name__}")
        return None

    raw_bars: List[Dict[str, Any]] = []
    if include_pre_market:
        raw_bars.extend(_extract_bars_section(payload, "preMarket"))
    raw_bars.extend(_extract_bars_section(payload, "regularMarket"))
    if include_after_market:
        raw_bars.extend(_extract_bars_section(payload, "afterMarket"))

    if not raw_bars:
        # Empty regularMarket bars is a real (rare) outcome — log keys safely.
        print(f"[public_bars] {sym} {period}: no bars in response "
              f"(top-level keys={list(payload.keys())})")
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])

    normalized = [b for b in (_coerce_bar(b) for b in raw_bars) if b is not None]
    return _bars_to_df(normalized)


# -----------------------------
# Convenience: matches market_data.get_daily_bars signature loosely
# -----------------------------
def get_daily_bars_via_public(
    symbol: str,
    outputsize: int = 100,
    *,
    timeout: float = DEFAULT_TIMEOUT,
) -> Optional[pd.DataFrame]:
    """
    Drop-in shaped like `market_data.get_daily_bars`: returns up to `outputsize`
    most recent daily bars. Internally it requests YEAR (~252 daily bars).

    NOT wired into the live bot. Provided so backtest/simulation code can
    A/B against the existing live data path.
    """
    df = get_public_bars(symbol, period="YEAR", timeout=timeout)
    if df is None:
        return None
    if df.empty:
        return df
    return df.tail(min(outputsize, len(df))).reset_index(drop=True)


if __name__ == "__main__":
    # Tiny smoke test you can run locally:
    #     python public_bars.py SPY YEAR
    import sys

    sym = sys.argv[1] if len(sys.argv) > 1 else "SPY"
    per = sys.argv[2] if len(sys.argv) > 2 else "YEAR"
    print(f"[smoke] fetching {sym} {per}")
    out = get_public_bars(sym, per)
    if out is None:
        print("[smoke] returned None")
    else:
        print(f"[smoke] rows={len(out)} cols={list(out.columns)}")
        if len(out):
            print(out.tail(3).to_string())
