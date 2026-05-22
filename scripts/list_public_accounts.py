"""
List every Public.com brokerage account this PUBLIC_SECRET can see, with a
brief portfolio summary (equity, position count, cash buying power) for each.

Run locally to figure out which `accountId` to put in PUBLIC_ACCOUNT_ID
when you have multiple accounts under the same login.

Usage:
    python scripts/list_public_accounts.py

Prereq:
    PUBLIC_SECRET must be set in your environment (or in .env).
"""

from __future__ import annotations

import os
import sys
from typing import Any, Dict, List

import requests

try:
    # Optional — load .env if python-dotenv is installed (it is in requirements.txt).
    from dotenv import load_dotenv  # type: ignore[import-untyped]
    load_dotenv()
except Exception:
    pass


AUTH_URL = "https://api.public.com/userapiauthservice/personal/access-tokens"
ACCOUNT_URL = "https://api.public.com/userapigateway/trading/account"
PORTFOLIO_V2_URL_TMPL = (
    "https://api.public.com/userapigateway/trading/{accountId}/portfolio/v2"
)


def _get_token(secret: str) -> str:
    resp = requests.post(
        AUTH_URL,
        json={"secret": secret, "validityInMinutes": 5},
        timeout=30,
    )
    resp.raise_for_status()
    return str(resp.json()["accessToken"])


def _portfolio_summary(token: str, account_id: str) -> Dict[str, Any]:
    try:
        resp = requests.get(
            PORTFOLIO_V2_URL_TMPL.format(accountId=account_id),
            headers={"Authorization": f"Bearer {token}"},
            timeout=30,
        )
        if not resp.ok:
            return {"error": f"HTTP {resp.status_code}: {resp.text[:160]}"}
        data: Dict[str, Any] = resp.json()
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}

    positions = data.get("positions") or []
    equity_items = data.get("equity") or []
    bp = data.get("buyingPower") or {}

    total_equity = 0.0
    for item in equity_items:
        try:
            total_equity += float(item.get("value", 0.0) or 0.0)
        except Exception:
            pass

    cash_bp = 0.0
    for key in ("cashOnlyBuyingPower", "buyingPower", "availableBuyingPower"):
        v = bp.get(key)
        if v is not None:
            try:
                cash_bp = float(v)
                break
            except Exception:
                pass

    symbols: List[str] = []
    for p in positions:
        inst = p.get("instrument") or {}
        sym = inst.get("symbol")
        if sym:
            symbols.append(str(sym))

    return {
        "equity_usd": round(total_equity, 2),
        "buying_power_usd": round(cash_bp, 2),
        "position_count": len(positions),
        "symbols": symbols[:25],
    }


def main() -> int:
    secret = (os.environ.get("PUBLIC_SECRET") or "").strip()
    if not secret:
        print("ERROR: PUBLIC_SECRET not set in environment.", file=sys.stderr)
        return 2

    print("Fetching access token...")
    token = _get_token(secret)

    print("Listing accounts...")
    resp = requests.get(
        ACCOUNT_URL,
        headers={"Authorization": f"Bearer {token}"},
        timeout=30,
    )
    resp.raise_for_status()
    accounts: List[Dict[str, Any]] = resp.json().get("accounts") or []

    if not accounts:
        print("No accounts returned for this PUBLIC_SECRET.")
        return 1

    print(f"\nFound {len(accounts)} account(s):\n")
    for i, a in enumerate(accounts):
        aid = str(a.get("accountId") or "")
        a_type = a.get("accountType") or "?"
        b_type = a.get("brokerageAccountType") or "?"
        perms = a.get("tradePermissions") or "?"
        print(f"[{i}] accountId: {aid}")
        print(f"    accountType: {a_type}   brokerageType: {b_type}   permissions: {perms}")
        summary = _portfolio_summary(token, aid)
        if "error" in summary:
            print(f"    portfolio: ERROR — {summary['error']}")
        else:
            syms = ", ".join(summary["symbols"]) if summary["symbols"] else "(none)"
            print(
                f"    equity=${summary['equity_usd']:.2f}   "
                f"buying_power=${summary['buying_power_usd']:.2f}   "
                f"open_positions={summary['position_count']}"
            )
            print(f"    symbols: {syms}")
        print()

    print(
        "Pick the account whose equity / open positions match the bot you "
        "are running, copy its accountId, and set it as the PUBLIC_ACCOUNT_ID "
        "GitHub secret (and in your local .env)."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
