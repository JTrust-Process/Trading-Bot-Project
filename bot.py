import os
import time
import uuid
import csv
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone, date
from typing import Optional, Dict, Any, List, Tuple

import requests
from dotenv import load_dotenv
from zoneinfo import ZoneInfo
from market_data import get_daily_bars
from strategy import calculate_atr, trend_strength, generate_signal


# -----------------------------
# Constants / Config
# -----------------------------
AUTH_URL = "https://api.public.com/userapiauthservice/personal/access-tokens"
ACCOUNT_URL = "https://api.public.com/userapigateway/trading/account"
QUOTES_URL_TMPL = "https://api.public.com/userapigateway/marketdata/{accountId}/quotes"
ORDER_URL_TMPL = "https://api.public.com/userapigateway/trading/{accountId}/order"
PORTFOLIO_V2_URL_TMPL = "https://api.public.com/userapigateway/trading/{accountId}/portfolio/v2"

NY_TZ = ZoneInfo("America/New_York")
STATE_FILE = "state.json"
STOP_FILE = "STOP.txt"


# -----------------------------
# Time helpers
# -----------------------------
def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def today_key_et() -> str:
    return datetime.now(NY_TZ).strftime("%Y-%m-%d")


def today_date_et() -> date:
    return datetime.now(NY_TZ).date()


def ensure_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)


def parse_date_ymd(s: str) -> date:
    return datetime.strptime(s.strip(), "%Y-%m-%d").date()


def is_market_hours_now() -> bool:
    now = datetime.now(NY_TZ)
    if now.weekday() >= 5:
        return False
    open_time = now.replace(hour=9, minute=30, second=0, microsecond=0)
    close_time = now.replace(hour=16, minute=0, second=0, microsecond=0)
    return open_time <= now < close_time


def seconds_until_next_open() -> int:
    now = datetime.now(NY_TZ)

    # move to next weekday midnight if weekend
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
# Env parsing
# -----------------------------
def get_env_str(name: str, default: str) -> str:
    raw = os.getenv(name)
    if raw is None:
        return default
    raw = raw.strip()
    return raw if raw else default


def get_env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    raw = raw.strip()
    if not raw:
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
    if not raw:
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
    if not raw:
        return default
    return raw in ("1", "true", "yes", "y", "on")


def parse_symbols_env() -> List[str]:
    raw = get_env_str("SYMBOLS", "SPY")
    syms = [s.strip().upper() for s in raw.split(",") if s.strip()]
    # de-dupe while preserving order
    out = []
    seen = set()
    for s in syms:
        if s not in seen:
            out.append(s)
            seen.add(s)
    return out


# -----------------------------
# Logging (CSV)
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
    *,
    symbol: str = "",
    side: str = "",
    amount_usd: str = "",
    order_id: str = "",
    status: str = "",
    details: str = "",
) -> None:
    ensure_log_header(log_file)
    with open(log_file, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow([utcnow().isoformat(), event, symbol, side, amount_usd, order_id, status, details])


def print_status(event: str, details: str = "") -> None:
    msg = f"[BOT] {event}"
    if details:
        msg += f" | {details}"
    print(msg, flush=True)


def count_trades_today(log_file: str) -> int:
    if not os.path.exists(log_file):
        return 0

    tkey_et = today_key_et()
    n = 0

    with open(log_file, "r", newline="", encoding="utf-8") as f:
        r = csv.DictReader(f)
        for row in r:
            ev = (row.get("event", "") or "").strip()
            if ev not in ("LIVE_BUY_SUBMITTED", "LIVE_SELL_SUBMITTED"):
                continue

            ts = (row.get("timestamp_utc", "") or "").strip()
            if not ts:
                continue

            try:
                ts_norm = ts.replace("Z", "+00:00")
                dt_utc = datetime.fromisoformat(ts_norm)
                if dt_utc.tzinfo is None:
                    dt_utc = dt_utc.replace(tzinfo=timezone.utc)
                dt_et = dt_utc.astimezone(NY_TZ)
                if dt_et.strftime("%Y-%m-%d") == tkey_et:
                    n += 1
            except Exception:
                # fallback (best-effort)
                if ts.startswith(tkey_et):
                    n += 1

    return n


# -----------------------------
# State (per-symbol)
# -----------------------------
def load_state() -> Dict[str, Any]:
    if not os.path.exists(STATE_FILE):
        return {"symbols": {}}
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f) or {}
        if "symbols" not in data or not isinstance(data["symbols"], dict):
            data["symbols"] = {}
        return data
    except Exception:
        return {"symbols": {}}


def save_state(state: Dict[str, Any]) -> None:
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, sort_keys=True)
    os.replace(tmp, STATE_FILE)


# -----------------------------
# Withdrawal glide path / exposure caps (portfolio-level)
# -----------------------------
def parse_withdraw_date_env() -> Optional[date]:
    raw = os.getenv("WITHDRAW_DATE")
    if not raw:
        return None
    raw = raw.strip()
    try:
        return datetime.strptime(raw, "%Y-%m-%d").date()
    except Exception:
        raise RuntimeError("WITHDRAW_DATE must be YYYY-MM-DD (e.g., 2026-05-17)")


def days_until_withdraw(withdraw_date: Optional[date]) -> Optional[int]:
    if not withdraw_date:
        return None
    return (withdraw_date - today_date_et()).days


def compute_max_exposure(days_left: Optional[int], *, early: float, mid: float, late: float, lockdown_days: int) -> float:
    # Total portfolio exposure cap target (not per-symbol).
    if days_left is None:
        return early
    if days_left <= lockdown_days:
        return 0.0
    if days_left <= 30:
        return late
    if days_left <= 60:
        return mid
    return early


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

    def get_portfolio_v2(self) -> Dict[str, Any]:
        account_id = self.get_account_id()
        url = PORTFOLIO_V2_URL_TMPL.format(accountId=account_id)
        resp = requests.get(url, headers=self.auth.auth_headers(), timeout=30)
        resp.raise_for_status()
        return resp.json()

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

    def deterministic_order_id(self, account_id: str, side: str, symbol: str) -> str:
        seed = f"{account_id}:{today_key_et()}:{side}:{symbol}"
        return str(uuid.uuid5(uuid.NAMESPACE_DNS, seed))

    def place_market_sell_quantity(self, symbol: str, quantity: float) -> Dict[str, Any]:
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
# Portfolio helpers
# -----------------------------

def get_market_regime():
    """
    Returns: "bull", "bear", or None if data unavailable
    """
    try:
        regime_symbol = os.getenv("REGIME_SYMBOL", "SPY")
        fast = int(os.getenv("REGIME_FAST", "50"))
        slow = int(os.getenv("REGIME_SLOW", "200"))

        df = get_daily_bars(regime_symbol, max(fast, slow) + 5)
        if df is None or len(df) < slow:
            print_status("REGIME", "insufficient data")
            return None

        ma_fast = df["close"].rolling(fast).mean().iloc[-1]
        ma_slow = df["close"].rolling(slow).mean().iloc[-1]

        if ma_fast > ma_slow:
            return "bull"
        else:
            return "bear"

    except Exception as e:
        print_status("REGIME_ERROR", str(e))
        return None

def find_equity_position(portfolio: Dict[str, Any], symbol: str) -> Optional[Dict[str, Any]]:
    for p in (portfolio.get("positions") or []):
        inst = p.get("instrument") or {}
        if inst.get("type") == "EQUITY" and (inst.get("symbol") or "").upper() == symbol.upper():
            return p
    return None


def get_buying_power_usd(portfolio: Dict[str, Any]) -> float:
    bp = portfolio.get("buyingPower") or {}
    for key in ("cashOnlyBuyingPower", "buyingPower", "availableBuyingPower"):
        v = bp.get(key)
        if v is not None:
            try:
                return float(v)
            except Exception:
                pass
    return 0.0


def get_total_equity_usd(portfolio: Dict[str, Any]) -> float:
    total = 0.0
    for item in (portfolio.get("equity") or []):
        try:
            total += float(item.get("value", 0.0) or 0.0)
        except Exception:
            pass
    return float(total)


def get_position_qty(pos: Dict[str, Any]) -> float:
    for key in ("quantity", "qty", "shares"):
        v = pos.get(key)
        if v is not None:
            try:
                return float(v)
            except Exception:
                pass
    return 0.0


def get_position_value_usd(pos: Dict[str, Any]) -> float:
    v = pos.get("currentValue")
    if v is not None:
        try:
            return float(v)
        except Exception:
            pass

    # fallback: qty * lastPrice.lastPrice
    try:
        qty = float(pos.get("quantity", 0.0) or 0.0)
        lp = pos.get("lastPrice") or {}
        px = float(lp.get("lastPrice", 0.0) or 0.0)
        return qty * px
    except Exception:
        return 0.0


def get_unrealized_pnl_pct(pos: Dict[str, Any]) -> Optional[float]:
    ig = pos.get("instrumentGain") or {}
    gp = ig.get("gainPercentage")
    if gp is None:
        return None
    try:
        return float(gp) / 100.0
    except Exception:
        return None


def get_last_price(pos: Dict[str, Any]) -> Optional[float]:
    lp = pos.get("lastPrice") or {}
    v = lp.get("lastPrice")
    if v is None:
        return None
    try:
        return float(v)
    except Exception:
        return None


# -----------------------------
# Per-symbol risk state helpers (position-value based)
# -----------------------------
def sym_state(state: Dict[str, Any], symbol: str) -> Dict[str, Any]:
    state.setdefault("symbols", {})
    s = state["symbols"].setdefault(symbol, {})
    if "day_key_et" not in s:
        s["day_key_et"] = ""
    if "day_start_value" not in s:
        s["day_start_value"] = 0.0
    if "peak_value" not in s:
        s["peak_value"] = 0.0
    if "halt_until_day_key_et" not in s:
        s["halt_until_day_key_et"] = ""
    return s


def should_halt_symbol(sstate: Dict[str, Any]) -> bool:
    halt_key = (sstate.get("halt_until_day_key_et") or "").strip()
    if not halt_key:
        return False
    # halt for the whole day key
    return today_key_et() <= halt_key


# -----------------------------
# Trading Logic (LIVE) - manage existing portfolio only
# -----------------------------
def run_live_cycle(
    client: PublicClient,
    *,
    log_file: str,
    symbols: List[str],
    max_trades_per_day: int,
    atr_vol_threshold: float,
    take_profit_base: float,
    stop_loss_base: float,
    trend_strong_threshold: float,
    withdraw_date: Optional[date],
    lockdown_days: int,
    max_exposure_early: float,
    max_exposure_mid: float,
    max_exposure_late: float,
    max_drawdown: float,
    daily_loss_limit: float,
    atr_period: int,
    trend_fast: int,
    trend_slow: int,
) -> None:

    regime = get_market_regime()
    print_status("MARKET_REGIME", regime)

    if os.path.exists(STOP_FILE):
        append_log(log_file, "STOPPED", details="STOP file detected")
        return

    trades_today = count_trades_today(log_file)
    if trades_today >= max_trades_per_day:
        return

    state = load_state()

    portfolio = client.get_portfolio_v2()
    total_equity = get_total_equity_usd(portfolio)
    buying_power = get_buying_power_usd(portfolio)

    days_left = days_until_withdraw(withdraw_date)
    cap_total = compute_max_exposure(
        days_left,
        early=max_exposure_early,
        mid=max_exposure_mid,
        late=max_exposure_late,
        lockdown_days=lockdown_days,
    )

    snapshots = []
    total_pos_value = 0.0

    for sym in symbols:
        pos = find_equity_position(portfolio, sym)
        if not pos:
            continue

        qty = get_position_qty(pos)
        if qty <= 0:
            continue

        pos_value = get_position_value_usd(pos)
        pnl_pct = get_unrealized_pnl_pct(pos)
        last_px = get_last_price(pos)

        snapshots.append({
            "symbol": sym,
            "pos": pos,
            "qty": float(qty),
            "value": float(pos_value),
            "pnl_pct": pnl_pct,
            "last": last_px,
        })

        total_pos_value += float(pos_value)

    total_exposure = (
        total_pos_value / total_equity if total_equity > 0 else 0
    )

    # ==========================================================
    # ENTRY LOGIC (FIXED)
    # ==========================================================
    if (
        trades_today < max_trades_per_day
        and regime == "bull"
        and total_exposure < cap_total
        and buying_power > 50
    ):

        for sym in symbols:
            if trades_today >= max_trades_per_day:
                break

            if find_equity_position(portfolio, sym):
                continue

            df = get_daily_bars(sym)
            if df is None or len(df) < trend_slow + 5:
                continue

            signal = generate_signal(df)
            if signal != "BUY":
                continue

            atr = calculate_atr(df, atr_period)
            price = float(df["close"].iloc[-1])

            if price <= 0 or atr is None:
                continue

            if atr / price < atr_vol_threshold:
                continue

            strength = trend_strength(df, trend_fast, trend_slow)
            if strength < trend_strong_threshold:
                continue

            remaining_exposure = cap_total - total_exposure
            if remaining_exposure <= 0:
                break

            alloc_value = min(
                total_equity * 0.10,
                total_equity * remaining_exposure,
                buying_power * 0.95,
            )

            if alloc_value < 50:
                continue

            qty = alloc_value / price
            if qty <= 0:
                continue

            result = client.place_market_buy_quantity(sym, qty)
            trades_today += 1

            total_pos_value += alloc_value
            total_exposure = total_pos_value / total_equity

            append_log(
                log_file,
                "LIVE_BUY_SUBMITTED",
                symbol=sym,
                side="BUY",
                order_id=result["orderId"],
                details=f"signal=BUY strength={strength:.4f} alloc={alloc_value:.2f}",
            )

    # ==========================================================
    # EXIT LOGIC (IMPROVED DYNAMIC TP)
    # ==========================================================
    for snap in snapshots:

        if trades_today >= max_trades_per_day:
            break

        sym = snap["symbol"]
        qty = snap["qty"]
        pnl_pct = snap["pnl_pct"]

        if pnl_pct is None:
            continue

        df = get_daily_bars(sym)
        if df is None or len(df) < trend_slow + 5:
            continue

        atr = calculate_atr(df, atr_period)
        price = float(df["close"].iloc[-1])

        if not price or atr / price < atr_vol_threshold:
            continue

        strength = trend_strength(df, trend_fast, trend_slow)

        # More stable scaling
        strength_ratio = min(strength / trend_strong_threshold, 1.5)
        dynamic_tp = take_profit_base * (1 + 0.5 * strength_ratio)

        dynamic_sl = stop_loss_base
        if regime == "bear":
            dynamic_sl *= 0.8

        if pnl_pct >= dynamic_tp:
            result = client.place_market_sell_quantity(sym, qty)
            trades_today += 1
            append_log(
                log_file,
                "LIVE_SELL_SUBMITTED",
                symbol=sym,
                side="SELL",
                order_id=result["orderId"],
                details=f"DYNAMIC_TP pnl={pnl_pct:.2%}",
            )

        elif pnl_pct <= -dynamic_sl:
            result = client.place_market_sell_quantity(sym, qty)
            trades_today += 1
            append_log(
                log_file,
                "LIVE_SELL_SUBMITTED",
                symbol=sym,
                side="SELL",
                order_id=result["orderId"],
                details=f"DYNAMIC_SL pnl={pnl_pct:.2%}",
            )

    save_state(state)

# -----------------------------
# Main
# -----------------------------
def main() -> None:
    load_dotenv()

    mode = get_env_str("MODE", "live").lower()
    if mode != "live":
        raise RuntimeError("This file is LIVE-only. Set MODE=live")

    # Allowlist
    symbols = parse_symbols_env()
    atr_period = get_env_int("ATR_PERIOD", 14)
    atr_vol_threshold = get_env_float("ATR_VOL_THRESHOLD", 0.01)

    trend_fast = get_env_int("TREND_FAST", 5)
    trend_slow = get_env_int("TREND_SLOW", 20)
    trend_strong_threshold = get_env_float("TREND_STRONG_THRESHOLD", 0.5)

    take_profit_base = get_env_float("TAKE_PROFIT_BASE", 0.04)
    stop_loss_base = get_env_float("STOP_LOSS_BASE", 0.02)


    # Core knobs
    max_trades_per_day = get_env_int("MAX_TRADES_PER_DAY", 1)
    token_validity_minutes = get_env_int("TOKEN_VALIDITY_MINUTES", 15)

    

    poll_seconds = get_env_int("POLL_SECONDS", 900)
    run_once = get_env_bool("RUN_ONCE", False)
    dry_run = get_env_bool("DRY_RUN", False)

    # Withdrawal + exposure caps (portfolio-level)
    withdraw_date = parse_withdraw_date_env()
    lockdown_days = get_env_int("LOCKDOWN_DAYS", 14)

    max_exposure_early = get_env_float("MAX_EXPOSURE_EARLY", 0.30)
    max_exposure_mid = get_env_float("MAX_EXPOSURE_MID", 0.20)
    max_exposure_late = get_env_float("MAX_EXPOSURE_LATE", 0.10)

    # Per-symbol kill switch thresholds (position-value based)
    max_drawdown = get_env_float("MAX_DRAWDOWN", 0.015)         # 1.5%
    daily_loss_limit = get_env_float("DAILY_LOSS_LIMIT", 0.005) # 0.5%

    secret = os.getenv("PUBLIC_SECRET")
    if not secret or not secret.strip():
        raise RuntimeError("Missing PUBLIC_SECRET")

    ensure_dir("logs")
    log_file = os.path.join("logs", "trade_log.csv")

    append_log(
        log_file,
        "BOT_START",
        details=(
            f"symbols={symbols} TP_BASE={take_profit_base:.4f} SL_BASE={stop_loss_base:.4f} "
            f"max_trades_day={max_trades_per_day} poll={poll_seconds}s run_once={run_once} dry_run={dry_run} "
            f"withdraw_date={withdraw_date} lockdown_days={lockdown_days} "
            f"cap(early/mid/late)={max_exposure_early}/{max_exposure_mid}/{max_exposure_late} "
            f"max_dd={max_drawdown} daily_loss={daily_loss_limit}"
        ),
    )
    print_status(
        "BOT_START",
        f"symbols={symbols} TP_BASE={take_profit_base:.2%} SL_BASE={stop_loss_base:.2%} "
        f"withdraw_date={withdraw_date} lockdown_days={lockdown_days} dry_run={dry_run}",
    )

    auth = PublicAuth(secret=secret, validity_minutes=token_validity_minutes, log_file=log_file)
    client = PublicClient(auth=auth, log_file=log_file, dry_run=dry_run)
    _ = client.get_account_id()

    while True:
        try:
            if os.path.exists(STOP_FILE):
                append_log(log_file, "STOPPED", details=f"{STOP_FILE} detected; stopping main loop.")
                print_status("STOPPED", f"{STOP_FILE} detected; exiting")
                return

            if not is_market_hours_now():
                wait = max(60, seconds_until_next_open())
                append_log(log_file, "SLEEP", details=f"Outside market hours. Sleeping {wait}s.")
                print_status("SLEEP", f"outside market hours; next open in {wait // 60} min")
                if run_once:
                    return
                time.sleep(wait)
                continue

            run_live_cycle(
                client,
                log_file=log_file,
                symbols=symbols,
                max_trades_per_day=max_trades_per_day,
                atr_vol_threshold=atr_vol_threshold,
                take_profit_base=take_profit_base,
                stop_loss_base=stop_loss_base,
                trend_strong_threshold=trend_strong_threshold,
                withdraw_date=withdraw_date,
                lockdown_days=lockdown_days,
                max_exposure_early=max_exposure_early,
                max_exposure_mid=max_exposure_mid,
                max_exposure_late=max_exposure_late,
                max_drawdown=max_drawdown,
                daily_loss_limit=daily_loss_limit,
                atr_period=atr_period,
                trend_fast=trend_fast,
                trend_slow=trend_slow,
            )

        except Exception as e:
            append_log(log_file, "ERROR", details=str(e))
            print_status("ERROR", str(e))

        if run_once:
            return

        time.sleep(max(30, poll_seconds))


if __name__ == "__main__":
    main()
