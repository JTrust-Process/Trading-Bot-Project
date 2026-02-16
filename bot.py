import os
import time
import uuid
import csv
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, Any, List

import requests
from dotenv import load_dotenv

# market-hours helpers (Python 3.9+)
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


# -----------------------------
# Time helpers
# -----------------------------
def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def today_key_et() -> str:
    # Use New York date for "daily" limits (matches market hours)
    return datetime.now(NY_TZ).strftime("%Y-%m-%d")


def ensure_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)


# -----------------------------
# Robust env parsing (fixes GitHub Actions empty-string secrets)
# -----------------------------
def get_env_str(name: str, default: str) -> str:
    raw = os.getenv(name)
    if raw is None:
        return default
    raw = raw.strip()
    return raw if raw != "" else default


def get_env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    raw = raw.strip()
    if raw == "":
        return default
    try:
        return int(raw)
    except Exception:
        return default


def get_env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None:
        return default
    raw = raw.strip()
    if raw == "":
        return default
    try:
        return float(raw)
    except Exception:
        return default


def get_env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    raw = raw.strip().lower()
    if raw == "":
        return default
    return raw in ("1", "true", "yes", "y", "on")


# -----------------------------
# Console status helper (so Actions shows what happened without opening logs)
# -----------------------------
def print_status(event: str, details: str = "") -> None:
    msg = f"[BOT] {event}"
    if details:
        msg += f" | {details}"
    print(msg, flush=True)


# -----------------------------
# Market hours gate (NYSE core: Mon–Fri, 9:30–16:00 ET)
# Note: does NOT account for holidays/early closes
# -----------------------------
def is_market_hours_now() -> bool:
    now = datetime.now(NY_TZ)
    if now.weekday() >= 5:
        return False
    open_time = now.replace(hour=9, minute=30, second=0, microsecond=0)
    close_time = now.replace(hour=16, minute=0, second=0, microsecond=0)
    return open_time <= now < close_time


def seconds_until_next_open() -> int:
    now = datetime.now(NY_TZ)

    # move to weekday midnight if weekend
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


def append_log(
    log_file: str,
    event: str,
    symbol: str = "",
    side: str = "",
    amount_usd: str = "",
    order_id: str = "",
    status: str = "",
    details: str = "",
) -> None:
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
    """
    Counts today's submitted trades using the ET (New York) calendar day.
    We store timestamp_utc, so we parse it and convert to ET for comparison.
    """
    if not os.path.exists(log_file):
        return 0

    tkey_et = today_key_et()
    n = 0

    with open(log_file, "r", newline="", encoding="utf-8") as f:
        r = csv.DictReader(f)
        for row in r:
            ev = row.get("event", "")
            if ev not in ("LIVE_BUY_SUBMITTED", "LIVE_SELL_SUBMITTED"):
                continue

            ts = (row.get("timestamp_utc", "") or "").strip()
            if not ts:
                continue

            # Robust ISO parse (handles "...Z" and "+00:00")
            try:
                ts_norm = ts.replace("Z", "+00:00")
                dt_utc = datetime.fromisoformat(ts_norm)
                if dt_utc.tzinfo is None:
                    dt_utc = dt_utc.replace(tzinfo=timezone.utc)
                dt_et = dt_utc.astimezone(NY_TZ)
                if dt_et.strftime("%Y-%m-%d") == tkey_et:
                    n += 1
            except Exception:
                # if parse fails, fall back to old behavior (best-effort)
                if ts.startswith(tkey_et):
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
            print_status("TOKEN_REFRESHED")
        return self._access.token

    def auth_headers(self) -> Dict[str, str]:
        return {"Authorization": f"Bearer {self.get_token()}"}


# -----------------------------
# Public API Client
# -----------------------------
class PublicClient:
    def __init__(self, auth: PublicAuth, log_file: str, *, dry_run: bool = False):
        self.auth = auth
        self.log_file = log_file
        self.dry_run = dry_run
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
        print_status("ACCOUNT_LOADED")
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

    def deterministic_order_id(self, account_id: str, side: str, symbol: str) -> str:
        # Use ET day so BUY/SELL order IDs align with market-day logic
        seed = f"{account_id}:{today_key_et()}:{side}:{symbol}"
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

        if self.dry_run:
            return {"orderId": order_id, "response": {"dry_run": True, "payload": payload}}

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

        if self.dry_run:
            return {"orderId": order_id, "response": {"dry_run": True, "payload": payload}}

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
    convert it to decimal (-0.0012).
    """
    ig = pos.get("instrumentGain") or {}
    gp = ig.get("gainPercentage")
    if gp is not None:
        try:
            return float(gp) / 100.0
        except Exception:
            pass
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
        print_status("STOPPED", "STOP.txt detected; skipping cycle")
        return

    # Trade limit per day
    trades_today = count_trades_today(log_file)
    if trades_today >= max_trades_per_day:
        append_log(log_file, "SKIP", details="Trade limit reached for today.")
        print_status("SKIP", f"trade limit reached (trades_today={trades_today}, limit={max_trades_per_day})")
        return

    # Portfolio + position
    portfolio = client.get_portfolio_v2()
    pos = find_equity_position(portfolio, symbol)
    qty = get_position_qty(pos) if pos else 0.0
    buying_power = get_buying_power_usd(portfolio)

    append_log(
        log_file,
        "DECISION",
        symbol=symbol,
        details=(
            f"qty={qty:.8f} buying_power={buying_power:.2f} "
            f"tp={take_profit_pct:.4%} sl={stop_loss_pct:.4%} "
            f"trades_today={trades_today}/{max_trades_per_day} dry_run={client.dry_run}"
        ),
    )

    # Quote (for logging / decisions)
    quotes = client.get_quotes([{"symbol": symbol, "type": "EQUITY"}])
    q = quotes["quotes"][0]
    last = float(q["last"])
    bid = float(q["bid"])
    ask = float(q["ask"])
    append_log(log_file, "QUOTE", symbol=symbol, details=f"last={last:.2f} bid={bid:.2f} ask={ask:.2f}")
    print_status("QUOTE", f"{symbol} last={last:.2f} bid={bid:.2f} ask={ask:.2f}")

    # If holding: decide sell based on Portfolio V2 PnL%
    if qty > 0 and pos is not None:
        pnl_pct = get_unrealized_pnl_pct(pos)

        if pnl_pct is None:
            append_log(log_file, "HOLD", symbol=symbol, details="Have position but PnL% not available; no sell decision.")
            print_status("HOLD", "position detected but PnL% not available")
            return

        append_log(
            log_file,
            "POSITION",
            symbol=symbol,
            details=f"qty={qty:.8f} pnl_pct={pnl_pct:.4%} last={last:.2f} bid={bid:.2f} ask={ask:.2f}",
        )
        print_status("POSITION", f"qty={qty:.8f} pnl_pct={pnl_pct:.2%}")

        if pnl_pct >= take_profit_pct:
            result = client.place_market_sell_quantity(symbol, qty)
            append_log(
                log_file,
                "LIVE_SELL_SUBMITTED",
                symbol=symbol,
                side="SELL",
                order_id=result["orderId"],
                status="SUBMITTED" if not client.dry_run else "DRY_RUN",
                details=f"TAKE_PROFIT pnl_pct={pnl_pct:.4%} qty={qty:.8f}",
            )
            print_status("SELL_SUBMITTED", f"TAKE_PROFIT order_id={result['orderId']} qty={qty:.8f} pnl={pnl_pct:.2%}")
            return

        if pnl_pct <= -stop_loss_pct:
            result = client.place_market_sell_quantity(symbol, qty)
            append_log(
                log_file,
                "LIVE_SELL_SUBMITTED",
                symbol=symbol,
                side="SELL",
                order_id=result["orderId"],
                status="SUBMITTED" if not client.dry_run else "DRY_RUN",
                details=f"STOP_LOSS pnl_pct={pnl_pct:.4%} qty={qty:.8f}",
            )
            print_status("SELL_SUBMITTED", f"STOP_LOSS order_id={result['orderId']} qty={qty:.8f} pnl={pnl_pct:.2%}")
            return

        append_log(log_file, "HOLD", symbol=symbol, details="No exit signal.")
        print_status("HOLD", "no exit signal")
        return

    # If flat: buy if buying power allows
    if buying_power < 1.0:
        append_log(log_file, "SKIP", details=f"No buying power (buying_power={buying_power:.2f}).")
        print_status("SKIP", f"no buying power ({buying_power:.2f})")
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
        status="SUBMITTED" if not client.dry_run else "DRY_RUN",
        details=f"BUY amount={amount:.2f} ref_ask={ask:.2f}",
    )
    print_status("BUY_SUBMITTED", f"order_id={result['orderId']} amount=${amount:.2f}")


# -----------------------------
# Main
# -----------------------------
def main() -> None:
    load_dotenv()

    mode = get_env_str("MODE", "live").lower()
    if mode != "live":
        raise RuntimeError("This file is LIVE-only. Set MODE=live")

    # SAFE env parsing (this fixes your GitHub Actions crash)
    max_trades_per_day = get_env_int("MAX_TRADES_PER_DAY", 1)
    max_order_amount_usd = get_env_float("MAX_ORDER_AMOUNT_USD", 10.0)
    token_validity_minutes = get_env_int("TOKEN_VALIDITY_MINUTES", 15)

    take_profit_pct = get_env_float("TAKE_PROFIT_PCT", 0.01)  # +1.0%
    stop_loss_pct = get_env_float("STOP_LOSS_PCT", 0.007)     # -0.7%

    poll_seconds = get_env_int("POLL_SECONDS", 900)           # 15 min default
    run_once = get_env_bool("RUN_ONCE", False)

    # NEW: safety switch
    dry_run = get_env_bool("DRY_RUN", False)

    secret = os.getenv("PUBLIC_SECRET")
    if not secret or secret.strip() == "":
        raise RuntimeError("Missing PUBLIC_SECRET")

    ensure_dir("logs")
    log_file = os.path.join("logs", "trade_log.csv")

    append_log(
        log_file,
        "BOT_START",
        details=(
            f"live loop | TP={take_profit_pct:.4f} SL={stop_loss_pct:.4f} "
            f"max_trades_day={max_trades_per_day} max_order={max_order_amount_usd} "
            f"poll={poll_seconds}s run_once={run_once} dry_run={dry_run}"
        ),
    )
    print_status(
        "BOT_START",
        f"TP={take_profit_pct:.2%} SL={stop_loss_pct:.2%} "
        f"max_trades_day={max_trades_per_day} max_order=${max_order_amount_usd:.2f} "
        f"poll={poll_seconds}s run_once={run_once} dry_run={dry_run}",
    )

    auth = PublicAuth(secret=secret, validity_minutes=token_validity_minutes, log_file=log_file)
    client = PublicClient(auth=auth, log_file=log_file, dry_run=dry_run)

    _ = client.get_account_id()

    while True:
        try:
            if os.path.exists("STOP.txt"):
                append_log(log_file, "STOPPED", details="STOP.txt detected; stopping main loop.")
                print_status("STOPPED", "STOP.txt detected; exiting")
                return

            if not is_market_hours_now():
                wait = max(60, seconds_until_next_open())
                append_log(log_file, "SLEEP", details=f"Outside market hours. Sleeping {wait}s.")
                print_status("SLEEP", f"outside market hours; next open in {wait // 60} min")

                # In GitHub Actions (RUN_ONCE=1), exit immediately outside market hours
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
            print_status("ERROR", str(e))

        if run_once:
            return

        time.sleep(max(30, poll_seconds))


if __name__ == "__main__":
    main()
