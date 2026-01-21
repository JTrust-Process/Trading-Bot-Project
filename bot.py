import os
import time
import uuid
import csv
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, Any, List

import requests
from dotenv import load_dotenv


# -----------------------------
# Config (edit these)
# -----------------------------
ALLOWED_SYMBOLS = {"SPY"}          # allowlist (start small)
MAX_ORDER_AMOUNT_USD = 10.0        # max dollars per order
MAX_TRADES_PER_DAY = 1             # hard limit
RUN_EVERY_SECONDS = 60   # daily; change to 60*15 for every 15 minutes
LOG_FILE = "trade_log.csv"

AUTH_URL = "https://api.public.com/userapiauthservice/personal/access-tokens"
ACCOUNT_URL = "https://api.public.com/userapigateway/trading/account"
QUOTES_URL_TMPL = "https://api.public.com/userapigateway/marketdata/{accountId}/quotes"
ORDER_URL_TMPL = "https://api.public.com/userapigateway/trading/{accountId}/order"
ORDER_STATUS_URL_TMPL = "https://api.public.com/userapigateway/trading/{accountId}/order/{orderId}"


# -----------------------------
# Utilities
# -----------------------------
def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def safe_float(x: Any, default: float = 0.0) -> float:
    try:
        return float(x)
    except Exception:
        return default


def ensure_log_header(path: str) -> None:
    if os.path.exists(path):
        return
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow([
            "timestamp_utc", "event", "symbol", "side", "amount_usd",
            "order_id", "status", "details"
        ])


def append_log(event: str, symbol: str = "", side: str = "", amount_usd: str = "",
               order_id: str = "", status: str = "", details: str = "") -> None:
    ensure_log_header(LOG_FILE)
    with open(LOG_FILE, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow([utcnow().isoformat(), event, symbol, side, amount_usd, order_id, status, details])


def today_key_utc() -> str:
    return utcnow().strftime("%Y-%m-%d")


def count_trades_today() -> int:
    if not os.path.exists(LOG_FILE):
        return 0
    tkey = today_key_utc()
    n = 0
    with open(LOG_FILE, "r", newline="", encoding="utf-8") as f:
        r = csv.DictReader(f)
        for row in r:
            ts = row.get("timestamp_utc", "")
            if ts.startswith(tkey) and row.get("event") == "ORDER_SUBMITTED":
                n += 1
    return n


# -----------------------------
# Token manager
# -----------------------------
@dataclass
class AccessToken:
    token: str
    expires_at_utc: datetime  # best-effort internal expiry tracking


class PublicAuth:
    def __init__(self, secret: str, validity_minutes: int = 15):
        self.secret = secret
        self.validity_minutes = validity_minutes
        self._access: Optional[AccessToken] = None

    def _fetch_token(self) -> AccessToken:
        resp = requests.post(
            AUTH_URL,
            headers={"Content-Type": "application/json"},
            json={"secret": self.secret, "validityInMinutes": self.validity_minutes},
            timeout=30,
        )
        resp.raise_for_status()
        token = resp.json()["accessToken"]
        # We track expiry ourselves: refresh a bit early (2 min buffer)
        expires_at = utcnow() + timedelta(minutes=self.validity_minutes - 2)
        return AccessToken(token=token, expires_at_utc=expires_at)

    def get_token(self) -> str:
        if self._access is None or utcnow() >= self._access.expires_at_utc:
            self._access = self._fetch_token()
            append_log("TOKEN_REFRESHED", details="New access token obtained (not logged).")
        return self._access.token

    def auth_headers(self) -> Dict[str, str]:
        tok = self.get_token()
        return {"Authorization": f"Bearer {tok}"}


# -----------------------------
# Public API client
# -----------------------------
class PublicClient:
    def __init__(self, auth: PublicAuth):
        self.auth = auth
        self._account_id: Optional[str] = None

    def get_account_id(self) -> str:
        if self._account_id:
            return self._account_id
        resp = requests.get(ACCOUNT_URL, headers=self.auth.auth_headers(), timeout=30)
        resp.raise_for_status()
        data = resp.json()
        accounts = data.get("accounts") or []
        if not accounts:
            raise RuntimeError("No accounts returned from Public.")
        self._account_id = accounts[0]["accountId"]
        append_log("ACCOUNT_LOADED", details=f"accountId loaded.")
        return self._account_id

    def get_quotes(self, instruments: List[Dict[str, str]]) -> Dict[str, Any]:
        account_id = self.get_account_id()
        url = QUOTES_URL_TMPL.format(accountId=account_id)
        resp = requests.post(
            url,
            headers={**self.auth.auth_headers(), "Content-Type": "application/json"},
            json={"instruments": instruments},
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json()

    def place_market_buy_amount(self, symbol: str, amount_usd: float) -> Dict[str, Any]:
        if symbol not in ALLOWED_SYMBOLS:
            raise ValueError(f"Symbol {symbol} not in ALLOWED_SYMBOLS.")
        if amount_usd <= 0 or amount_usd > MAX_ORDER_AMOUNT_USD:
            raise ValueError(f"amount_usd must be >0 and <= {MAX_ORDER_AMOUNT_USD}.")

        account_id = self.get_account_id()
        url = ORDER_URL_TMPL.format(accountId=account_id)
        order_id = str(uuid.uuid4())

        payload = {
            "orderId": order_id,
            "instrument": {"symbol": symbol, "type": "EQUITY"},
            "orderSide": "BUY",
            "orderType": "MARKET",
            "expiration": {"timeInForce": "DAY"},
            "amount": f"{amount_usd:.2f}",
        }

        resp = requests.post(
            url,
            headers={**self.auth.auth_headers(), "Content-Type": "application/json"},
            json=payload,
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        append_log(
            "ORDER_SUBMITTED",
            symbol=symbol,
            side="BUY",
            amount_usd=f"{amount_usd:.2f}",
            order_id=order_id,
            status="SUBMITTED",
            details="Market buy by notional amount",
        )
        return {"orderId": order_id, "response": data}

    def get_order_status(self, order_id: str) -> Dict[str, Any]:
        account_id = self.get_account_id()
        url = ORDER_STATUS_URL_TMPL.format(accountId=account_id, orderId=order_id)
        resp = requests.get(url, headers=self.auth.auth_headers(), timeout=30)
        # docs note order can 404 briefly; handle gracefully
        if resp.status_code == 404:
            return {"status": "NOT_READY"}
        resp.raise_for_status()
        return resp.json()


# -----------------------------
# Trading logic (starter)
# -----------------------------
def run_trading_logic(client: PublicClient, DRY_RUN: bool) -> None:
    """
    Starter logic:
    - Only run if trades_today < MAX_TRADES_PER_DAY
    - Buy $10 of SPY once per day
    Replace this with your real strategy later.
    """
    trades_today = count_trades_today()
    if trades_today >= MAX_TRADES_PER_DAY:
        append_log("SKIP", details=f"Trade limit reached for today ({trades_today}).")
        return

    symbol = "SPY"
    amount = min(MAX_ORDER_AMOUNT_USD, 10.0)

    # Optional: pull quotes to ensure API is healthy
    _ = client.get_quotes([{"symbol": symbol, "type": "EQUITY"}])
    
    print("Dry run: fetched quote successfully; would buy SPY for", amount)


    # Place order
    if DRY_RUN:
        append_log("WOULD_BUY", symbol=symbol, side="BUY", amount_usd=f"{amount:.2f}",
               details="DRY_RUN enabled: not placing order")
        return



# -----------------------------
# Main loop ("make it a script")
# -----------------------------
def main() -> None:
    load_dotenv()
    DRY_RUN = os.getenv("DRY_RUN", "true").lower() in ("1", "true", "yes")
    secret = os.getenv("PUBLIC_API_KEY")
    if not secret:
        raise RuntimeError("Missing PUBLIC_API_KEY in environment/.env")

    auth = PublicAuth(secret=secret, validity_minutes=15)
    client = PublicClient(auth=auth)

    append_log("BOT_START", details="Bot started.")

    # Warm-up: verify we can load accountId
    _ = client.get_account_id()

    while True:
        try:
            run_trading_logic(client, DRY_RUN)
        except Exception as e:
            # Never log tokens/secrets, just the error message
            append_log("ERROR", details=str(e))

        time.sleep(RUN_EVERY_SECONDS)


if __name__ == "__main__":
    main()