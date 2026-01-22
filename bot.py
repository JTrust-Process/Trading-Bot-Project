import os
import time
import uuid
import csv
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, Any, List

import requests
from dotenv import load_dotenv

from paper_portfolio import PaperPortfolio


# -----------------------------
# Constants (safe defaults)
# -----------------------------
AUTH_URL = "https://api.public.com/userapiauthservice/personal/access-tokens"
ACCOUNT_URL = "https://api.public.com/userapigateway/trading/account"
QUOTES_URL_TMPL = "https://api.public.com/userapigateway/marketdata/{accountId}/quotes"
ORDER_URL_TMPL = "https://api.public.com/userapigateway/trading/{accountId}/order"
ORDER_STATUS_URL_TMPL = "https://api.public.com/userapigateway/trading/{accountId}/order/{orderId}"

ALLOWED_SYMBOLS = {"SPY"}  # start with one symbol only


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# -----------------------------
# Logging (CSV) with Windows-safe retry
# -----------------------------
def ensure_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)


def ensure_log_header(path: str) -> None:
    if os.path.exists(path):
        return
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["timestamp_utc", "event", "symbol", "side", "amount_usd", "order_id", "status", "details"])


def append_log(log_file: str, event: str, symbol: str = "", side: str = "", amount_usd: str = "",
               order_id: str = "", status: str = "", details: str = "") -> None:
    ensure_log_header(log_file)
    # retry for Windows file lock
    for _ in range(3):
        try:
            with open(log_file, "a", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                w.writerow([utcnow().isoformat(), event, symbol, side, amount_usd, order_id, status, details])
            return
        except PermissionError:
            time.sleep(0.2)
    # if it still fails, raise
    with open(log_file, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow([utcnow().isoformat(), event, symbol, side, amount_usd, order_id, status, details])


def log_paper_equity(equity_file: str, total_value: float, cash: float, positions: dict) -> None:
    file_exists = os.path.exists(equity_file)
    # retry for Windows file lock
    for _ in range(3):
        try:
            with open(equity_file, "a", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                if not file_exists:
                    w.writerow(["timestamp_utc", "total_value", "cash", "positions_json"])
                w.writerow([utcnow().isoformat(), f"{total_value:.4f}", f"{cash:.4f}", str(positions)])
            return
        except PermissionError:
            time.sleep(0.2)
    # last attempt
    with open(equity_file, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if not file_exists:
            w.writerow(["timestamp_utc", "total_value", "cash", "positions_json"])
        w.writerow([utcnow().isoformat(), f"{total_value:.4f}", f"{cash:.4f}", str(positions)])


def today_key_utc() -> str:
    return utcnow().strftime("%Y-%m-%d")


def count_trades_today(log_file: str) -> int:
    if not os.path.exists(log_file):
        return 0
    tkey = today_key_utc()
    n = 0
    with open(log_file, "r", newline="", encoding="utf-8") as f:
        r = csv.DictReader(f)
        for row in r:
            ts = row.get("timestamp_utc", "")
            ev = row.get("event", "")
            if ts.startswith(tkey) and ev in ("ORDER_SUBMITTED", "PAPER_TRADE", "LIVE_ORDER"):
                n += 1
    return n


# -----------------------------
# Token Manager
# -----------------------------
@dataclass
class AccessToken:
    token: str
    expires_at_utc: datetime


class PublicAuth:
    def __init__(self, secret: str, validity_minutes: int, log_file: str):
        self.secret = secret
        self.validity_minutes = validity_minutes
        self.log_file = log_file
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
        # refresh a bit early (2 min buffer)
        expires_at = utcnow() + timedelta(minutes=max(1, self.validity_minutes - 2))
        return AccessToken(token=token, expires_at_utc=expires_at)

    def get_token(self) -> str:
        if self._access is None or utcnow() >= self._access.expires_at_utc:
            self._access = self._fetch_token()
            append_log(self.log_file, "TOKEN_REFRESHED", details="New access token obtained (not logged).")
        return self._access.token

    def auth_headers(self) -> Dict[str, str]:
        return {"Authorization": f"Bearer {self.get_token()}"}


# -----------------------------
# Public API Client
# -----------------------------
class PublicClient:
    def __init__(self, auth: PublicAuth, log_file: str):
        self.auth = auth
        self.log_file = log_file
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
        append_log(self.log_file, "ACCOUNT_LOADED", details="accountId loaded.")
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
        if amount_usd <= 0:
            raise ValueError("amount_usd must be > 0")

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
            self.log_file,
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
        if resp.status_code == 404:
            return {"status": "NOT_READY"}
        resp.raise_for_status()
        return resp.json()


# -----------------------------
# Trading Logic (simple)
# -----------------------------
def run_trading_logic(
    client: PublicClient,
    *,
    mode: str,
    log_file: str,
    equity_file: str,
    paper_state_file: str,
    paper_start_cash: float,
    max_trades_per_day: int,
    max_order_amount_usd: float,
) -> None:
    symbol = "SPY"
    amount = min(max_order_amount_usd, 10.0)

    # Get quote
    quotes = client.get_quotes([{"symbol": symbol, "type": "EQUITY"}])

    try:
        q = quotes["quotes"][0]
        last = float(q["last"])
        bid = float(q["bid"])
        ask = float(q["ask"])
        price_for_buy = ask  # conservative for market buys
        append_log(log_file, "QUOTE", symbol=symbol, details=f"last={last:.2f} bid={bid:.2f} ask={ask:.2f}")
    except Exception:
        append_log(log_file, "ERROR", details=f"Could not parse quote. Raw: {str(quotes)[:300]}")
        return

    # Paper equity snapshot every run
    if mode == "paper":
        pf = PaperPortfolio.load(paper_state_file, start_cash=paper_start_cash)
        total = pf.total_value({symbol: price_for_buy})
        log_paper_equity(equity_file, total_value=total, cash=pf.cash, positions=pf.positions)

    # Enforce daily trade limit (paper + live)
    trades_today = count_trades_today(log_file)
    if trades_today >= max_trades_per_day:
        append_log(log_file, "SKIP", details=f"Trade limit reached for today ({trades_today}).")
        print(f"SKIP: trade limit reached. Price={price_for_buy:.2f}")
        return

    if mode == "dry":
        append_log(
            log_file,
            "WOULD_BUY",
            symbol=symbol,
            side="BUY",
            amount_usd=f"{amount:.2f}",
            details=f"MODE=dry: not placing order (ref ask={price_for_buy:.2f})",
        )
        print(f"DRY: would buy ${amount:.2f} of {symbol} at ~{price_for_buy:.2f}")
        return

    if mode == "paper":
        pf = PaperPortfolio.load(paper_state_file, start_cash=paper_start_cash)
        msg = pf.buy_notional(symbol=symbol, dollars=amount, price=price_for_buy)
        pf.save(paper_state_file)

        total = pf.total_value({symbol: price_for_buy})
        log_paper_equity(equity_file, total_value=total, cash=pf.cash, positions=pf.positions)

        append_log(
            log_file,
            "PAPER_TRADE",
            symbol=symbol,
            side="BUY",
            amount_usd=f"{amount:.2f}",
            details=f"{msg} (fill=ask {price_for_buy:.2f})",
        )
        print(f"PAPER: {msg} | Fill={price_for_buy:.2f} | Total=${total:.2f} Cash=${pf.cash:.2f} Pos={pf.positions}")
        return

    if mode == "live":
        # Guardrails for live mode
        if amount > max_order_amount_usd:
            append_log(log_file, "ERROR", details=f"Live trade blocked: amount {amount} > max {max_order_amount_usd}")
            return

        result = client.place_market_buy_amount(symbol, amount)
        append_log(
            log_file,
            "LIVE_ORDER",
            symbol=symbol,
            side="BUY",
            amount_usd=f"{amount:.2f}",
            order_id=result["orderId"],
            details=f"Placed live order (ref ask={price_for_buy:.2f})",
        )
        print("LIVE: order submitted", result["orderId"])
        return

    append_log(log_file, "ERROR", details=f"Unknown MODE={mode}")


# -----------------------------
# Main
# -----------------------------
def main() -> None:
    # Load .env FIRST so MODE/PAPER_START_CASH work from .env
    load_dotenv()

    # Read runtime config from env (with safe defaults)
    mode = os.getenv("MODE", "dry").lower()  # dry | paper | live
    paper_start_cash = float(os.getenv("PAPER_START_CASH", "100"))
    run_every_seconds = int(os.getenv("RUN_EVERY_SECONDS", str(60 * 60 * 24)))
    max_order_amount_usd = float(os.getenv("MAX_ORDER_AMOUNT_USD", "10"))
    max_trades_per_day = int(os.getenv("MAX_TRADES_PER_DAY", "1"))
    token_validity_minutes = int(os.getenv("TOKEN_VALIDITY_MINUTES", "15"))

    # Secrets
    secret = os.getenv("PUBLIC_SECRET")
    if not secret:
        raise RuntimeError("Missing PUBLIC_SECRET in environment/.env")

    # Create logs dir to avoid Windows locking issues in root
    ensure_dir("logs")
    log_file = os.path.join("logs", "trade_log.csv")
    equity_file = os.path.join("logs", "paper_equity.csv")
    paper_state_file = os.path.join("logs", "paper_state.json")

    append_log(log_file, "BOT_START", details=f"Bot started. mode={mode} run_every_seconds={run_every_seconds}")

    auth = PublicAuth(secret=secret, validity_minutes=token_validity_minutes, log_file=log_file)
    client = PublicClient(auth=auth, log_file=log_file)

    # Warm-up: verify we can load accountId
    _ = client.get_account_id()

    while True:
        try:
            run_trading_logic(
                client,
                mode=mode,
                log_file=log_file,
                equity_file=equity_file,
                paper_state_file=paper_state_file,
                paper_start_cash=paper_start_cash,
                max_trades_per_day=max_trades_per_day,
                max_order_amount_usd=max_order_amount_usd,
            )
        except Exception as e:
            append_log(log_file, "ERROR", details=str(e))

        time.sleep(run_every_seconds)


if __name__ == "__main__":
    main()
