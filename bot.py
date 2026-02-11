import os
import time
import uuid
import csv
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, Any, List

import requests
from dotenv import load_dotenv

# NEW: market-hours helpers (Python 3.9+)
from zoneinfo import ZoneInfo


# -----------------------------
# Constants
# -----------------------------
AUTH_URL = "https://api.public.com/userapiauthservice/personal/access-tokens"
ACCOUNT_URL = "https://api.public.com/userapigateway/trading/account"
QUOTES_URL_TMPL = "https://api.public.com/userapigateway/marketdata/{accountId}/quotes"
ORDER_URL_TMPL = "https://api.public.com/userapigateway/trading/{accountId}/order"
PORTFOLIO_V2_URL_TMPL = "https://api.public.com/userapigateway/trading/{accountId}/portfolio/v2"

ALLOWED_SYMBOLS = {"SPY"}

NY_TZ = ZoneInfo("America/New_York")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def today_key_utc() -> str:
    return utcnow().strftime("%Y-%m-%d")


def ensure_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)


# -----------------------------
# Market hours gate (NYSE core: Mon–Fri, 9:30–16:00 ET)
# Note: does NOT account for holidays/early closes
# -----------------------------
def is_market_hours_now() -> bool:
    now = datetime.now(NY_TZ)
    if now.weekday() >= 5:  # Sat/Sun
        return False
    open_time = now.replace(hour=9, minute=30, second=0, microsecond=0)
    close_time = now.replace(hour=16, minute=0, second=0, microsecond=0)
    return open_time <= now < close_time


def seconds_until_next_open() -> int:
    now = datetime.now(NY_TZ)

    # Weekend -> next weekday
    while now.weekday() >= 5:
        now = now + timedelta(days=1)
        now = now.replace(hour=0, minute=0, second=0, microsecond=0)

    open_time = now.replace(hour=9, minute=30, second=0, microsecond=0)
    if now < open_time:
        return max(0, int((open_time - now).total_seconds()))

    close_time = now.replace(hour=16, minute=0, second=0, microsecond=0)
    if now >= close_time:
        nxt = now + timedelta(days=1)
        while nxt.weekday() >= 5:
            nxt = nxt + timedelta(days=1)
        nxt_open = nxt.replace(hour=9, minute=30, second=0, microsecond=0)
        return max(0, int((nxt_open - now).total_seconds()))

    return 0


# -----------------------------
# Logging (CSV) with Windows-safe retry
# -----------------------------
def ensure_log_header(path: str) -> None:
    if os.path.exists(path):
        return
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["timestamp_utc", "event", "symbol", "side", "amount_usd", "order_id", "status", "details"])


def append_log(log_file: str, event: str, symbol: str = "", side: str = "", amount_usd: str = "",
               order_id: str = "", status: str = "", details: str = "") -> None:
    ensure_log_header(log_file)
    for _ in range(3):
        try:
            with open(log_file, "a", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                w.writerow([utcnow().isoformat(), event, symbol, side, amount_usd, order_id, status, details])
            return
        except PermissionError:
            time.sleep(0.2)
    # last attempt
    with open(log_file, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow([utcnow().isoformat(), event, symbol, side, amount_usd, order_id, status, details])


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
            if ts.startswith(tkey) and ev in ("LIVE_BUY_SUBMITTED", "LIVE_SELL_SUBMITTED"):
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

    def get_portfolio_v2(self) -> Dict[str, Any]:
        account_id = self.get_account_id()
        url = PORTFOLIO_V2_URL_TMPL.format(accountId=account_id)
        resp = requests.get(url, headers=self.auth.auth_headers(), timeout=30)
        resp.raise_for_status()
        return resp.json()

    @staticmethod
    def deterministic_order_id(account_id: str, side: str, symbol: str) -> str:
        seed = f"{account_id}:{today_key_utc()}:{side}:{symbol}"
        return str(uuid.uuid5(uuid.NAMESPACE_DNS, seed))

    def place_market_buy_amount(self, symbol: str, amount_usd: float) -> Dict[str, Any]:
        if symbol not in ALLOWED_SYMBOLS:
            raise ValueError(f"Symbol {symbol} not in ALLOWED_SYMBOLS.")
        if amount_usd <= 0:
            raise ValueError("amount_usd must be > 0")

        account_id = self.get_account_id()
        url = ORDER_URL_TMPL.format(accountId=account_id)
        order_id = self.deterministic_order_id(account_id, "BUY", symbol)

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
        return {"orderId": order_id, "response": resp.json()}

    def place_market_sell_quantity(self, symbol: str, quantity: float) -> Dict[str, Any]:
        if symbol not in ALLOWED_SYMBOLS:
            raise ValueError(f"Symbol {symbol} not in ALLOWED_SYMBOLS.")
        if quantity <= 0:
            raise ValueError("quantity must be > 0")

        account_id = self.get_account_id()
        url = ORDER_URL_TMPL.format(accountId=account_id)
        order_id = self.deterministic_order_id(account_id, "SELL", symbol)

        payload = {
            "orderId": order_id,
            "instrument": {"symbol": symbol, "type": "EQUITY"},
            "orderSide": "SELL",
            "orderType": "MARKET",
            "expiration": {"timeInForce": "DAY"},
            "quantity": f"{quantity:.8f}",
        }

        resp = requests.post(
            url,
            headers={**self.auth.auth_headers(), "Content-Type": "application/json"},
            json=payload,
            timeout=30,
        )
        resp.raise_for_status()
        return {"orderId": order_id, "response": resp.json()}


# -----------------------------
# Helpers to read portfolio
# -----------------------------
def find_equity_position(portfolio: Dict[str, Any], symbol: str) -> Optional[Dict[str, Any]]:
    positions = portfolio.get("positions") or []
    for p in positions:
        inst = (p.get("instrument") or {})
        if inst.get("symbol") == symbol and inst.get("type") == "EQUITY":
            return p
    return None


def get_buying_power_usd(portfolio: Dict[str, Any]) -> float:
    bp = portfolio.get("buyingPower") or {}
    for key in ("cashOnlyBuyingPower", "buyingPower", "availableBuyingPower"):
        val = bp.get(key)
        if val is not None:
            try:
                return float(val)
            except Exception:
                pass
    return 0.0


def get_unit_cost(pos: Dict[str, Any]) -> float:
    cb = pos.get("costBasis") or {}
    for key in ("unitCost", "averageCost", "avgCost", "averagePrice"):
        val = cb.get(key)
        if val is not None:
            try:
                f = float(val)
                if f > 0:
                    return f
            except Exception:
                pass
    return 0.0


def get_position_qty(pos: Dict[str, Any]) -> float:
    for key in ("quantity", "qty", "shares"):
        val = pos.get(key)
        if val is not None:
            try:
                return float(val)
            except Exception:
                pass
    return 0.0


def get_unrealized_pnl_pct(pos: Dict[str, Any]) -> Optional[float]:
    """
    Prefer Portfolio V2's instrumentGain.gainPercentage (e.g. "-0.12" means -0.12%),
    convert it to decimal (-0.0012). Fallback to cost basis math if needed.
    """
    ig = pos.get("instrumentGain") or {}
    gp = ig.get("gainPercentage")
    if gp is not None:
        try:
            return float(gp) / 100.0
        except Exception:
            pass

    unit_cost = get_unit_cost(pos)
    lp = (pos.get("lastPrice") or {}).get("lastPrice")
    try:
        last = float(lp) if lp is not None else 0.0
    except Exception:
        last = 0.0

    if unit_cost > 0 and last > 0:
        return (last - unit_cost) / unit_cost

    return None


# -----------------------------
# Trading Logic (LIVE)
# -----------------------------
def run_live_cycle(
    client: PublicClient,
    *,
    log_file: str,
    max_trades_per_day: int,
    max_order_amount_usd: float,
    take_profit_pct: float,
    stop_loss_pct: float,
) -> None:
    symbol = "SPY"

    # Kill switch
    if os.path.exists("STOP.txt"):
        append_log(log_file, "STOPPED", details="STOP.txt detected; skipping cycle.")
        print("STOP.txt present — bot halted.")
        return

    # Trade limit per day
    if count_trades_today(log_file) >= max_trades_per_day:
        append_log(log_file, "SKIP", details="Trade limit reached for today.")
        print("SKIP: trade limit reached today.")
        return

    # Get portfolio + position
    portfolio = client.get_portfolio_v2()
    pos = find_equity_position(portfolio, symbol)
    qty = get_position_qty(pos) if pos else 0.0
    buying_power = get_buying_power_usd(portfolio)

    # Get quote (for logging / reference only)
    quotes = client.get_quotes([{"symbol": symbol, "type": "EQUITY"}])
    q = quotes["quotes"][0]
    last = float(q["last"])
    bid = float(q["bid"])
    ask = float(q["ask"])
    append_log(log_file, "QUOTE", symbol=symbol, details=f"last={last:.2f} bid={bid:.2f} ask={ask:.2f}")

    # If holding: decide sell
    if qty > 0 and pos is not None:
        pnl_pct = get_unrealized_pnl_pct(pos)

        if pnl_pct is None:
            append_log(log_file, "HOLD", symbol=symbol, details="Have position but PnL% not available; no sell decision.")
            print("HOLD: position detected but PnL% not available.")
            return

        append_log(
            log_file,
            "POSITION",
            symbol=symbol,
            details=f"qty={qty:.8f} pnl_pct={pnl_pct:.4%} last={last:.2f} bid={bid:.2f} ask={ask:.2f}",
        )

        if pnl_pct >= take_profit_pct:
            result = client.place_market_sell_quantity(symbol, qty)
            append_log(
                log_file,
                "LIVE_SELL_SUBMITTED",
                symbol=symbol,
                side="SELL",
                order_id=result["orderId"],
                status="SUBMITTED",
                details=f"TAKE_PROFIT pnl_pct={pnl_pct:.4%} qty={qty:.8f}",
            )
            print("LIVE: SELL submitted (take profit)", result["orderId"])
            return

        if pnl_pct <= -stop_loss_pct:
            result = client.place_market_sell_quantity(symbol, qty)
            append_log(
                log_file,
                "LIVE_SELL_SUBMITTED",
                symbol=symbol,
                side="SELL",
                order_id=result["orderId"],
                status="SUBMITTED",
                details=f"STOP_LOSS pnl_pct={pnl_pct:.4%} qty={qty:.8f}",
            )
            print("LIVE: SELL submitted (stop loss)", result["orderId"])
            return

        print("HOLD: no exit signal.")
        append_log(log_file, "HOLD", symbol=symbol, details="No exit signal.")
        return

    # If flat: buy if buying power allows
    if buying_power < 1.0:
        append_log(log_file, "SKIP", details=f"No buying power (buying_power={buying_power:.2f}).")
        print(f"SKIP: no buying power ({buying_power:.2f})")
        return

    amount = min(max_order_amount_usd, buying_power, 10.0)
    result = client.place_market_buy_amount(symbol, amount)
    append_log(
        log_file,
        "LIVE_BUY_SUBMITTED",
        symbol=symbol,
        side="BUY",
        amount_usd=f"{amount:.2f}",
        order_id=result["orderId"],
        status="SUBMITTED",
        details=f"BUY amount={amount:.2f} ref_ask={ask:.2f}",
    )
    print("LIVE: BUY submitted", result["orderId"])


# -----------------------------
# Main (NOW: runs continuously during market hours)
# -----------------------------
def main() -> None:
    load_dotenv()

    mode = os.getenv("MODE", "live").lower()
    if mode != "live":
        raise RuntimeError("This version is LIVE-only. Set MODE=live in .env")

    max_trades_per_day = int(os.getenv("MAX_TRADES_PER_DAY", "1"))
    max_order_amount_usd = float(os.getenv("MAX_ORDER_AMOUNT_USD", "10"))
    token_validity_minutes = int(os.getenv("TOKEN_VALIDITY_MINUTES", "15"))

    take_profit_pct = float(os.getenv("TAKE_PROFIT_PCT", "0.01"))   # +1.0%
    stop_loss_pct = float(os.getenv("STOP_LOSS_PCT", "0.007"))      # -0.7%

    # NEW: how often to check during market hours (default: 15 min)
    poll_seconds = int(os.getenv("POLL_SECONDS", "900"))

    secret = os.getenv("PUBLIC_SECRET")
    if not secret:
        raise RuntimeError("Missing PUBLIC_SECRET in .env")

    ensure_dir("logs")
    log_file = os.path.join("logs", "trade_log.csv")

    append_log(
        log_file,
        "BOT_START",
        details=f"live loop | TP={take_profit_pct:.4f} SL={stop_loss_pct:.4f} "
                f"max_trades_day={max_trades_per_day} max_order={max_order_amount_usd} poll={poll_seconds}s",
    )

    auth = PublicAuth(secret=secret, validity_minutes=token_validity_minutes, log_file=log_file)
    client = PublicClient(auth=auth, log_file=log_file)

    _ = client.get_account_id()

    run_once = os.getenv("RUN_ONCE", "0").lower() in ("1", "true", "yes")

    while True:
        try:
            if os.path.exists("STOP.txt"):
                append_log(log_file, "STOPPED", details="STOP.txt detected; stopping main loop.")
                print("STOP.txt present — exiting.")
                return

            if not is_market_hours_now():
                wait = max(60, seconds_until_next_open())
                append_log(log_file, "SLEEP", details=f"Outside market hours. Sleeping {wait}s.")
                print(f"Outside market hours. Sleeping {wait // 60} minutes...")

                # In GitHub Actions, we should EXIT if outside market hours
                if run_once:
                    return

                time.sleep(wait)
                continue

            run_live_cycle(
                client,
                log_file=log_file,
                max_trades_per_day=max_trades_per_day,
                max_order_amount_usd=max_order_amount_usd,
                take_profit_pct=take_profit_pct,
                stop_loss_pct=stop_loss_pct,
            )

        except Exception as e:
            append_log(log_file, "ERROR", details=str(e))

        if run_once:
            return

        time.sleep(poll_seconds)



if __name__ == "__main__":
    main()
