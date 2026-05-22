import os
import time
import uuid
import csv
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone, date
from typing import Optional, Dict, Any, List, cast

import requests
from dotenv import load_dotenv
from zoneinfo import ZoneInfo
from market_data import get_daily_bars
from strategy import calculate_atr, trend_strength
from momentum import rank_symbols, should_sell_momentum
from breakout import check_breakout
from supabase import create_client
from monitor import monitor
import league_status  # ADDITIVE — fail-silent League heartbeat. Touches NO trading logic.
from notify import (
    notify_buy, notify_sell, notify_run_start,
    notify_run_end, notify_error, notify_regime_change,
    notify_daily_summary, notify_stale_bot,
)


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

# Supabase client — initialized after load_dotenv() in main()
supabase = None
_last_regime: str | None = None  # track regime changes for notifications

def _load_last_regime_from_supabase() -> Optional[str]:
    """Load last known regime from Supabase to avoid UNKNOWN → X spam on GHA restarts."""
    global supabase
    if supabase is None:
        return None
    try:
        res = supabase.table("bot_logs").select("details").eq("event", "MARKET_REGIME").order(
            "timestamp_utc", desc=True
        ).limit(1).execute()
        if res.data:
            row: Dict[str, Any] = cast(Dict[str, Any], res.data[0])
            val = str(row.get("details") or "").strip()
            return val if val in ("bull", "bear") else None
    except Exception:
        pass
    return None


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


def is_market_hours_now() -> bool:
    now = datetime.now(NY_TZ)
    if now.weekday() >= 5:
        return False
    open_time = now.replace(hour=9, minute=30, second=0, microsecond=0)
    close_time = now.replace(hour=16, minute=0, second=0, microsecond=0)
    return open_time <= now < close_time


def seconds_until_next_open() -> int:
    now = datetime.now(NY_TZ)
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


def parse_symbols_env(key: str = "SYMBOLS", default: str = "SPY") -> List[str]:
    raw = get_env_str(key, default)
    if not raw:
        return []
    syms = [s.strip().upper() for s in raw.split(",") if s.strip()]
    out: List[str] = []
    seen: set[str] = set()
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
        w.writerow([
            "timestamp_utc", "event", "symbol", "side",
            "amount_usd", "order_id", "status", "details",
        ])


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
    ts = utcnow().isoformat()
    with open(log_file, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow([ts, event, symbol, side, amount_usd, order_id, status, details])
    _log_to_supabase(ts, event, symbol=symbol, side=side, amount_usd=amount_usd,
                     order_id=order_id, status=status, details=details)


def _log_to_supabase(
    ts: str,
    event: str,
    *,
    symbol: str = "",
    side: str = "",
    amount_usd: str = "",
    order_id: str = "",
    status: str = "",
    details: str = "",
) -> None:
    """Write a log entry to Supabase bot_logs. Never crashes the bot."""
    global supabase
    if supabase is None:
        return
    try:
        supabase.table("bot_logs").insert({
            "timestamp_utc": ts,
            "event": event,
            "symbol": symbol or None,
            "side": side or None,
            "amount_usd": amount_usd or None,
            "order_id": order_id or None,
            "status": status or None,
            "details": details or None,
        }).execute()
    except Exception as e:
        print_status("SUPABASE_LOG_ERROR", str(e))


def print_status(event: str, details: str = "") -> None:
    msg = f"[BOT] {event}"
    if details:
        msg += f" | {details}"
    print(msg, flush=True)


def count_trades_today(log_file: str) -> int:
    """
    Count LIVE_BUY_SUBMITTED + LIVE_SELL_SUBMITTED events for today (ET).

    Prefers Supabase (the only source that persists across ephemeral GHA runs).
    Falls back to the local CSV if Supabase isn't configured — useful for local dev.
    """
    global supabase

    # Prefer Supabase: persists across GHA runs, which the local CSV does not.
    if supabase is not None:
        try:
            now_et = datetime.now(NY_TZ)
            day_start_et = now_et.replace(hour=0, minute=0, second=0, microsecond=0)
            day_start_utc_iso = day_start_et.astimezone(timezone.utc).isoformat()
            # NOTE: supabase-py expects a `CountMethod` enum for `count`; "exact"
            # is the runtime-accepted string. Suppressing the type warning here
            # is safer than importing from a private postgrest path that varies
            # by client version.
            res = (
                supabase.table("bot_logs")
                .select("event", count="exact")  # type: ignore[arg-type]
                .in_("event", ["LIVE_BUY_SUBMITTED", "LIVE_SELL_SUBMITTED"])
                .gte("timestamp_utc", day_start_utc_iso)
                .execute()
            )
            # supabase-py returns the row count in res.count when count="exact"
            res_count = getattr(res, "count", None)
            if res_count is not None:
                return int(res_count or 0)
            # Fallback: len(data) (capped by default page size, but accurate for low-volume)
            return len(res.data or [])
        except Exception as e:
            print_status("TRADES_COUNT_SUPABASE_ERROR", str(e))
            # Fall through to CSV fallback so the bot still has *some* signal

    # Local CSV fallback (development / Supabase outage)
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
                if ts.startswith(tkey_et):
                    n += 1
    return n


# -----------------------------
# Supabase logging
# -----------------------------
def log_trade(
    symbol: str,
    side: str,
    entry_price: Optional[float],
    exit_price: Optional[float],
    pnl_pct: Optional[float],
    pnl_usd: Optional[float],
    amount_usd: Optional[float] = None,
    strategy: str = "default",
) -> None:
    """Write a trade event to Supabase. Never crashes the bot."""
    global supabase
    if supabase is None:
        print_status("SUPABASE_SKIP", "client not initialized")
        return
    try:
        record: Dict[str, Any] = {
            "symbol": symbol,
            "side": side,
            "entry_price": entry_price,
            "exit_price": exit_price,
            "pnl_percent": round(pnl_pct * 100, 4) if pnl_pct is not None else None,
            "pnl_usd": pnl_usd,
            "amount_usd": amount_usd,
            "strategy": strategy,
            "timestamp_utc": utcnow().isoformat(),
        }
        supabase.table("trades").insert(record).execute()
    except Exception as e:
        print_status("SUPABASE_ERROR", str(e))

    # League mirror — additive, fail-silent. Stock bot trades use a single
    # log_trade() callsite for both BUY (entry_price set, exit_price/PnL None)
    # and SELL (exit_price + PnL set). We pick the realized price accordingly
    # so bot_trades.price always reflects the side's fill price.
    try:
        is_buy = (side or "").upper() == "BUY"
        league_status.log_trade(
            symbol=symbol,
            side=side,
            price=(entry_price if is_buy else exit_price),
            amount_usd=amount_usd,
            pnl_usd=(None if is_buy else pnl_usd),
            pnl_pct=(None if is_buy else pnl_pct),
            strategy=strategy,
            is_paper=False,  # this code path only runs in live mode (asserted in main())
        )
    except Exception:
        pass



# -----------------------------
# Supabase positions tracking
# -----------------------------
def upsert_open_position(
    symbol: str,
    entry_price: float,
    entry_date: str,
    quantity: float,
    amount_usd: float,
    strategy: str,
    estimated: bool = False,
) -> None:
    """Insert or update an open position in Supabase. Never crashes the bot."""
    global supabase
    if supabase is None:
        return
    try:
        # Mark any existing open position as replaced
        supabase.table("positions").update({
            "status": "replaced",
            "updated_at": utcnow().isoformat(),
        }).eq("symbol", symbol).eq("status", "open").execute()
        # Insert fresh record — no unique constraint needed
        supabase.table("positions").insert({
            "symbol": symbol,
            "entry_price": entry_price,
            "entry_date": entry_date,
            "quantity": quantity,
            "amount_usd": amount_usd,
            "strategy": strategy,
            "status": "open",
            "entry_estimated": estimated,
            "updated_at": utcnow().isoformat(),
        }).execute()
    except Exception as e:
        print_status("POSITIONS_ERROR", f"upsert failed for {symbol}: {e}")


def get_open_position(symbol: str) -> Optional[Dict[str, Any]]:
    """Fetch open position record from Supabase. Returns None if not found."""
    global supabase
    if supabase is None:
        return None
    try:
        result = supabase.table("positions").select("*").eq("symbol", symbol).eq("status", "open").execute()
        if result.data and len(result.data) > 0:
            return cast(Dict[str, Any], result.data[0])
        return None
    except Exception as e:
        print_status("POSITIONS_ERROR", f"get failed for {symbol}: {e}")
        return None


def close_position(
    symbol: str,
    exit_price: float,
    pnl_pct: Optional[float],
    pnl_usd: Optional[float],
    reason: str,
) -> None:
    """Mark a position as closed in Supabase. Never crashes the bot."""
    global supabase
    if supabase is None:
        return
    try:
        supabase.table("positions").update({
            "status": "closed",
            "exit_price": exit_price,
            "exit_date": today_key_et(),
            "pnl_pct": round(pnl_pct * 100, 4) if pnl_pct is not None else None,
            "pnl_usd": pnl_usd,
            "close_reason": reason,
            "updated_at": utcnow().isoformat(),
        }).eq("symbol", symbol).eq("status", "open").execute()
    except Exception as e:
        print_status("POSITIONS_ERROR", f"close failed for {symbol}: {e}")



# -----------------------------
# Daily summary + health check
# -----------------------------
_last_summary_date: str = ""  # track so we only send once per day
_last_run_time: datetime = datetime.now(timezone.utc)  # track for stale detection

def maybe_send_daily_summary(
    log_file: str,
    equity: float,
    buying_power: float,
    regime: Optional[str],
) -> None:
    """Send daily summary at ~3:55pm ET and stale alert if bot hasn't run in 2h."""
    global _last_summary_date, _last_run_time, supabase

    now_et = datetime.now(NY_TZ)
    today = today_key_et()

    # Update last run time
    _last_run_time = datetime.now(timezone.utc)

    # Stale check — alert if >2h gap during market hours (9:30-16:00)
    # This runs every cycle so we check the previous gap not current
    # Actually handled by GHA not running — skip for now

    # Daily summary — send once at market close (3:50-4:00pm ET)
    is_close = now_et.hour == 15 and now_et.minute >= 50
    if not is_close or _last_summary_date == today:
        return

    _last_summary_date = today

    # Fetch today's trades from Supabase
    trades_today = 0
    wins = 0
    losses = 0
    pnl_usd = 0.0
    last_run_str = now_et.strftime("%I:%M %p")
    open_positions: list[dict[str, Any]] = []

    if supabase:
        try:
            # Today's closed trades
            # Use UTC date range for today (market hours are 9:30am-4pm ET = 14:30-21:00 UTC)
            today_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            result = supabase.table("trades").select("*").eq("side", "SELL").gte(
                "timestamp_utc", f"{today_utc}T00:00:00+00:00"
            ).execute()
            if result.data:
                for t in result.data:
                    t_row: Dict[str, Any] = cast(Dict[str, Any], t)
                    trades_today += 1
                    monitor.trade_count += 1
                    p_val = t_row.get("pnl_percent") or t_row.get("pnl_pct") or 0
                    u_val = t_row.get("pnl_usd") or 0
                    try:
                        u_float = float(str(u_val))
                        p_float = float(str(p_val))
                        pnl_usd += u_float
                    except Exception:
                        p_float = 0.0
                    if p_float >= 0:
                        wins += 1
                    else:
                        losses += 1
            # Open positions
            pos_result = supabase.table("positions").select("symbol,entry_price").eq("status", "open").execute()
            if pos_result.data:
                open_positions = [cast(Dict[str, Any], p) for p in pos_result.data]
        except Exception as e:
            print_status("SUMMARY_ERROR", str(e))

    notify_daily_summary(
        date_str=now_et.strftime("%b %d, %Y"),
        equity=equity,
        buying_power=buying_power,
        open_positions=open_positions,
        trades_today=trades_today,
        wins_today=wins,
        losses_today=losses,
        pnl_today_usd=pnl_usd,
        regime=regime,
        last_run=last_run_str,
        hours_since_run=0.0,
    )
    print_status("DAILY_SUMMARY", f"Sent for {today}")

# -----------------------------
# State (per-symbol) — consolidated from risk.py
# -----------------------------
def load_state() -> Dict[str, Any]:
    """
    Load per-symbol state. Order of preference:
      1. Supabase `bot_state` table  (persists across GHA runs — authoritative)
      2. Local STATE_FILE             (local dev, or Supabase outage)
      3. Empty                        (first run)

    Cooldowns, drawdown peaks, and day-start values must persist across
    runs or the risk controls are no-ops on ephemeral GHA runners.
    """
    global supabase

    # 1. Supabase
    if supabase is not None:
        try:
            res = supabase.table("bot_state").select("symbol,state").execute()
            if res.data:
                symbols_state: Dict[str, Any] = {}
                for row in res.data:
                    row_d: Dict[str, Any] = cast(Dict[str, Any], row)
                    sym = str(row_d.get("symbol") or "").upper().strip()
                    raw_state = row_d.get("state") or {}
                    if sym and isinstance(raw_state, dict):
                        symbols_state[sym] = raw_state
                if symbols_state:
                    return {"symbols": symbols_state}
        except Exception as e:
            print_status("STATE_LOAD_SUPABASE_ERROR", str(e))
            # fall through

    # 2. Local file fallback
    if not os.path.exists(STATE_FILE):
        return {"symbols": {}}
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data: Dict[str, Any] = json.load(f) or {}
        if "symbols" not in data or not isinstance(data["symbols"], dict):
            data["symbols"] = {}
        return data
    except Exception:
        return {"symbols": {}}


def save_state(state: Dict[str, Any]) -> None:
    """
    Persist state to BOTH the local file (handy for local dev) AND Supabase
    (the only source that survives a fresh GHA runner). Failures on either
    side are logged but never crash the bot.
    """
    global supabase

    # Local file (best-effort — directory may be read-only in some envs)
    try:
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, sort_keys=True)
        os.replace(tmp, STATE_FILE)
    except Exception as e:
        print_status("STATE_SAVE_FILE_ERROR", str(e))

    # Supabase mirror
    if supabase is None:
        return
    try:
        symbols_state: Dict[str, Any] = state.get("symbols", {}) or {}
        rows = [
            {
                "symbol": str(sym).upper(),
                "state": sstate or {},
                "updated_at": utcnow().isoformat(),
            }
            for sym, sstate in symbols_state.items()
            if sym
        ]
        if rows:
            supabase.table("bot_state").upsert(rows, on_conflict="symbol").execute()
    except Exception as e:
        print_status("STATE_SAVE_SUPABASE_ERROR", str(e))


def sym_state(state: Dict[str, Any], symbol: str) -> Dict[str, Any]:
    state.setdefault("symbols", {})
    s = state["symbols"].setdefault(symbol, {})
    s.setdefault("day_key_et", "")
    s.setdefault("day_start_value", 0.0)
    s.setdefault("peak_value", 0.0)
    s.setdefault("halt_until_day_key_et", "")
    s.setdefault("cooldown_until", "")  # multi-day cooldown from risk.py
    return s


def should_halt_symbol(sstate: Dict[str, Any]) -> bool:
    # Intra-day halt (e.g. after a stop loss fires today)
    halt_key = (sstate.get("halt_until_day_key_et") or "").strip()
    if halt_key and today_key_et() <= halt_key:
        return True
    # Multi-day cooldown (LOSS_COOLDOWN_DAYS from risk.py)
    cooldown = (sstate.get("cooldown_until") or "").strip()
    if cooldown:
        try:
            until_dt = datetime.fromisoformat(cooldown).replace(tzinfo=None)
            if datetime.now(timezone.utc).replace(tzinfo=None) < until_dt:
                return True
        except Exception:
            pass
    return False


def trigger_cooldown(sstate: Dict[str, Any], cooldown_days: int) -> None:
    """Put a symbol into multi-day cooldown after a loss (from risk.py)."""
    until = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(days=cooldown_days)
    sstate["cooldown_until"] = until.isoformat()


# -----------------------------
# Withdrawal glide path / exposure caps
# -----------------------------
def parse_withdraw_date_env() -> Optional[date]:
    raw = os.getenv("WITHDRAW_DATE")
    if not raw:
        return None
    raw = raw.strip()
    try:
        return datetime.strptime(raw, "%Y-%m-%d").date()
    except Exception:
        raise RuntimeError("WITHDRAW_DATE must be YYYY-MM-DD (e.g., 2026-05-10)")


def days_until_withdraw(withdraw_date: Optional[date]) -> Optional[int]:
    if not withdraw_date:
        return None
    return (withdraw_date - today_date_et()).days


def compute_max_exposure(
    days_left: Optional[int], *, early: float, mid: float, late: float, lockdown_days: int
) -> float:
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
# HTTP retry helper (for read-only API calls — NEVER used for order POSTs)
# -----------------------------
def _http_with_retry(
    method: str,
    url: str,
    *,
    headers: Optional[Dict[str, str]] = None,
    json_body: Optional[Dict[str, Any]] = None,
    timeout: int = 30,
    max_attempts: int = 3,
    label: str = "",
) -> requests.Response:
    """
    Retry transient network errors and 5xx responses with exponential backoff.
    Never retries 4xx (those are deterministic — auth, validation, idempotency).
    NEVER call this for order placement; idempotency on Public is per-orderId
    not per-attempt, and a duplicate would mean a duplicate fill.
    """
    last_exc: Optional[Exception] = None
    for attempt in range(max_attempts):
        try:
            resp = requests.request(
                method.upper(),
                url,
                headers=headers,
                json=json_body,
                timeout=timeout,
            )
            # Retry only on 5xx
            if 500 <= resp.status_code < 600 and attempt < max_attempts - 1:
                wait = 2 ** attempt
                print_status("HTTP_RETRY",
                    f"{label} {method} {resp.status_code} attempt={attempt + 1}/{max_attempts} backoff={wait}s body={resp.text[:200]}")
                time.sleep(wait)
                continue
            return resp
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
            last_exc = e
            if attempt < max_attempts - 1:
                wait = 2 ** attempt
                print_status("HTTP_RETRY",
                    f"{label} {method} network_error={type(e).__name__} attempt={attempt + 1}/{max_attempts} backoff={wait}s")
                time.sleep(wait)
                continue
            raise
    # Exhausted without returning a response (only via continue path on 5xx)
    if last_exc is not None:
        raise last_exc
    raise RuntimeError(f"{label} {method} {url}: retry loop exited without response")


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
        resp = _http_with_retry(
            "POST",
            AUTH_URL,
            headers={"Content-Type": "application/json"},
            json_body={"secret": self.secret, "validityInMinutes": self.validity_minutes},
            timeout=30,
            label="auth",
        )
        if not resp.ok:
            # Log status + body so misconfig (bad secret, IP block, etc.) is visible
            print_status("AUTH_ERROR", f"status={resp.status_code} body={resp.text[:500]}")
        resp.raise_for_status()
        token = resp.json()["accessToken"]
        expires_at = utcnow() + timedelta(minutes=max(1, self.validity_minutes - 2))
        return AccessToken(token=token, expires_at_utc=expires_at)

    def get_token(self) -> str:
        if self._access is None or utcnow() >= self._access.expires_at_utc:
            self._access = self._fetch_token()
            append_log(self.log_file, "TOKEN_REFRESHED", details="New access token obtained.")
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
        """
        Resolve the Public brokerage accountId to use for every trading call.

        A single Public personal access token can authenticate against MULTIPLE
        brokerage accounts (e.g. if the user has opened a second brokerage
        account under the same login). The /account endpoint then returns an
        array, and picking accounts[0] is fragile — order is not guaranteed
        and a newly-opened empty account can shadow the real one, causing the
        bot to read equity=0 / positions=[] and silently skip every TP/SL.

        Resolution order:
          1. PUBLIC_ACCOUNT_ID env var (preferred — explicit pin).
          2. The only BROKERAGE account, if exactly one exists.
          3. accounts[0] as a last-resort fallback (with a loud warning if
             more than one account was returned).
        """
        if self._account_id:
            return self._account_id
        resp = _http_with_retry(
            "GET", ACCOUNT_URL,
            headers=self.auth.auth_headers(),
            timeout=30, label="account",
        )
        if not resp.ok:
            print_status("ACCOUNT_ERROR", f"status={resp.status_code} body={resp.text[:500]}")
        resp.raise_for_status()
        accounts: list[dict[str, Any]] = resp.json().get("accounts") or []
        if not accounts:
            raise RuntimeError("No accounts returned from Public.")

        # Compact diagnostic summary of every account the token can see.
        # Helps the user pick the right PUBLIC_ACCOUNT_ID without leaking
        # full IDs (mask all but last 4 chars).
        def _mask(aid: str) -> str:
            s = str(aid)
            return s if len(s) <= 4 else f"...{s[-4:]}"
        summary = ", ".join(
            f"{_mask(a.get('accountId', ''))}({a.get('accountType', '?')}/"
            f"{a.get('brokerageAccountType', '?')})"
            for a in accounts
        )

        pinned = (os.environ.get("PUBLIC_ACCOUNT_ID") or "").strip()
        chosen: Optional[dict[str, Any]] = None
        selection_reason = ""

        if pinned:
            for a in accounts:
                if str(a.get("accountId") or "") == pinned:
                    chosen = a
                    selection_reason = "env:PUBLIC_ACCOUNT_ID"
                    break
            if chosen is None:
                # Pin set but not found — refuse to fall back silently. A bad
                # pin would otherwise put us back into the original failure
                # mode (wrong account, empty portfolio, no exits firing).
                append_log(
                    self.log_file, "ACCOUNT_PIN_NOT_FOUND",
                    details=f"PUBLIC_ACCOUNT_ID={_mask(pinned)} not in accounts=[{summary}]",
                )
                raise RuntimeError(
                    f"PUBLIC_ACCOUNT_ID={_mask(pinned)} did not match any "
                    f"account returned by Public. Available: [{summary}]"
                )

        if chosen is None:
            brokerage = [a for a in accounts if str(a.get("accountType") or "").upper() == "BROKERAGE"]
            if len(brokerage) == 1:
                chosen = brokerage[0]
                selection_reason = "sole-brokerage"
            elif len(accounts) == 1:
                chosen = accounts[0]
                selection_reason = "sole-account"

        if chosen is None:
            # Multiple accounts and no pin — fall back to [0] but make the
            # ambiguity extremely visible so the user knows to set the env var.
            chosen = accounts[0]
            selection_reason = "fallback-index-0-AMBIGUOUS"
            append_log(
                self.log_file, "ACCOUNT_AMBIGUOUS",
                details=(
                    f"{len(accounts)} accounts returned and PUBLIC_ACCOUNT_ID "
                    f"not set; defaulting to [0]. Set PUBLIC_ACCOUNT_ID to one of: "
                    f"[{summary}]"
                ),
            )
            print_status(
                "ACCOUNT_AMBIGUOUS",
                f"{len(accounts)} accounts; defaulting to [0]. "
                f"Set PUBLIC_ACCOUNT_ID. Available: [{summary}]",
            )

        self._account_id = str(chosen["accountId"])
        append_log(
            self.log_file, "ACCOUNT_LOADED",
            details=(
                f"selected={_mask(self._account_id)} "
                f"type={chosen.get('accountType', '?')}/"
                f"{chosen.get('brokerageAccountType', '?')} "
                f"reason={selection_reason} available=[{summary}]"
            ),
        )
        print_status("ACCOUNT_LOADED",
            f"selected={_mask(self._account_id)} reason={selection_reason}")
        return self._account_id

    def get_portfolio_v2(self) -> Dict[str, Any]:
        account_id = self.get_account_id()
        resp = _http_with_retry(
            "GET",
            PORTFOLIO_V2_URL_TMPL.format(accountId=account_id),
            headers=self.auth.auth_headers(),
            timeout=30, label="portfolio",
        )
        if not resp.ok:
            print_status("PORTFOLIO_ERROR", f"status={resp.status_code} body={resp.text[:500]}")
        resp.raise_for_status()
        return resp.json()

    def get_quotes(self, instruments: List[Dict[str, str]]) -> Dict[str, Any]:
        account_id = self.get_account_id()
        resp = _http_with_retry(
            "POST",
            QUOTES_URL_TMPL.format(accountId=account_id),
            headers={**self.auth.auth_headers(), "Content-Type": "application/json"},
            json_body={"instruments": instruments},
            timeout=30, label="quotes",
        )
        if not resp.ok:
            print_status("QUOTES_ERROR", f"status={resp.status_code} body={resp.text[:500]}")
        resp.raise_for_status()
        return resp.json()

    def deterministic_order_id(self, account_id: str, side: str, symbol: str) -> str:
        now_min = datetime.now(NY_TZ).strftime("%Y-%m-%d-%H-%M")
        seed = f"{account_id}:{now_min}:{side}:{symbol}"
        return str(uuid.uuid5(uuid.NAMESPACE_DNS, seed))

    def place_market_buy_amount(self, symbol: str, amount_usd: float) -> Dict[str, Any]:
        if amount_usd <= 0:
            raise ValueError("amount_usd must be > 0")
        account_id = self.get_account_id()
        order_id = self.deterministic_order_id(account_id, "BUY", symbol)
        notional = round(amount_usd, 2)
        payload: Dict[str, Any] = {
            "orderId": order_id,
            "instrument": {"symbol": symbol, "type": "EQUITY"},
            "orderSide": "BUY",
            "orderType": "MARKET",
            "expiration": {"timeInForce": "DAY"},
            "amount": f"{notional:.2f}",
        }
        if self.dry_run:
            return {"orderId": order_id, "response": {"dry_run": True, "payload": payload}}
        resp = requests.post(
            ORDER_URL_TMPL.format(accountId=account_id),
            headers={**self.auth.auth_headers(), "Content-Type": "application/json"},
            json=payload,
            timeout=30,
        )
        if not resp.ok:
            print_status("ORDER_RESPONSE_ERROR", f"BUY {symbol} status={resp.status_code} body={resp.text[:500]}")
        resp.raise_for_status()
        return {"orderId": order_id, "response": resp.json()}

    def get_fill_price(self, order_id: str, log_file: str = "") -> Optional[float]:
        """
        Fetch actual fill price for a completed order.
        Retries up to 4 times with 2s waits (8s total) to allow order to settle.
        Logs the full response to bot_logs so we can discover Public's exact field names.
        Returns None if unavailable — caller marks entry as estimated.
        """
        account_id = self.get_account_id()
        url = f"{ORDER_URL_TMPL.format(accountId=account_id)}/{order_id}"
        last_data: Dict[str, Any] = {}

        for attempt in range(4):
            try:
                # First attempt: try immediately. Subsequent attempts: 2s backoff so
                # an instantly-filled order doesn't pay a 2s latency tax.
                if attempt > 0:
                    time.sleep(2)
                resp = requests.get(url, headers=self.auth.auth_headers(), timeout=15)

                if resp.status_code != 200:
                    print_status("FILL_PRICE_ERROR", f"order={order_id} status={resp.status_code} attempt={attempt+1}")
                    continue

                data: Dict[str, Any] = resp.json()
                last_data = data

                # Log full response on first attempt so we can see Public's fields
                if attempt == 0 and log_file:
                    import json as _json
                    append_log(log_file, "ORDER_RESPONSE", symbol="",
                        details=f"order_id={order_id} keys={list(data.keys())} raw={_json.dumps(data)[:500]}")

                # Try all known field names at top level
                for field in ("averagePrice", "avgFillPrice", "fillPrice", "averageFillPrice",
                               "price", "filledPrice", "executedPrice", "avgPrice",
                               "filled_price", "fill_price", "average_price"):
                    val = data.get(field)
                    if val is not None:
                        try:
                            fp = float(str(val))  # type: ignore[arg-type]
                            if fp > 0:
                                print_status("FILL_PRICE", f"order={order_id} field={field} price={fp:.4f}")
                                return fp
                        except Exception:
                            pass

                # Try nested objects
                for key in ("order", "fill", "execution", "orderExecution", "fills"):
                    nested_raw = data.get(key)
                    if not isinstance(nested_raw, dict):
                        continue
                    nested: Dict[str, Any] = nested_raw
                    for field in ("avgFillPrice", "fillPrice", "price",
                                   "executedPrice", "averagePrice", "avgPrice"):
                        val = nested.get(field)
                        if val is not None:
                            try:
                                fp = float(str(val))  # type: ignore[arg-type]
                                if fp > 0:
                                    print_status("FILL_PRICE", f"order={order_id} nested={key}.{field} price={fp:.4f}")
                                    return fp
                            except Exception:
                                pass

                # Check if filled — if so averagePrice should be present, no point retrying
                status = str(data.get("status") or data.get("orderStatus") or "unknown")
                if status.upper() == "FILLED":
                    # Order filled but no price parsed — log exact value for debugging
                    print_status("FILL_PRICE_PARSE_MISS",
                        f"order={order_id} FILLED but no price parsed. averagePrice={data.get('averagePrice')} keys={list(data.keys())}")
                    break
                print_status("FILL_PRICE_RETRY", f"order={order_id} status={status} attempt={attempt+1}/4 keys={list(data.keys())}")

            except Exception as e:
                print_status("FILL_PRICE_EXCEPTION", f"order={order_id} attempt={attempt+1} err={e}")

        # Log that we couldn't find fill price so it shows in bot logs
        if log_file:
            import json as _json
            append_log(log_file, "FILL_PRICE_UNKNOWN", symbol="",
                details=f"order_id={order_id} could not find fill price — entry marked as estimated. keys={list(last_data.keys())}")
        print_status("FILL_PRICE_UNKNOWN", f"order={order_id} — using estimated price")
        return None

    def place_market_sell_quantity(self, symbol: str, quantity: float) -> Dict[str, Any]:
        if quantity <= 0:
            raise ValueError("quantity must be > 0")
        account_id = self.get_account_id()
        order_id = self.deterministic_order_id(account_id, "SELL", symbol)
        payload: Dict[str, Any] = {
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
            ORDER_URL_TMPL.format(accountId=account_id),
            headers={**self.auth.auth_headers(), "Content-Type": "application/json"},
            json=payload,
            timeout=30,
        )
        if not resp.ok:
            print_status("ORDER_RESPONSE_ERROR", f"SELL {symbol} status={resp.status_code} body={resp.text[:500]}")
        resp.raise_for_status()
        return {"orderId": order_id, "response": resp.json()}


# -----------------------------
# Portfolio helpers
# -----------------------------
def get_market_regime() -> Optional[str]:
    """
    Returns 'bull', 'bear', or None if data unavailable.

    Uses dual SMA confirmation:
      - Bull: price above BOTH 20-day AND 50-day SMA
      - Bear: price below EITHER SMA

    The 20-day reacts faster (catches short-term selloffs like tariff events).
    The 50-day filters out noise and confirms the broader trend.
    Both must agree before calling bull — one disagreement = bear.
    """
    try:
        regime_symbol = os.getenv("REGIME_SYMBOL", "SPY")
        fast_period = int(os.getenv("REGIME_FAST", "20"))   # short-term filter
        slow_period = int(os.getenv("REGIME_SLOW", "50"))   # medium-term filter
        df = get_daily_bars(regime_symbol, slow_period + 10)
        if df is None or len(df) < slow_period:
            print_status("REGIME", f"insufficient data (got {len(df) if df is not None else 0} bars)")
            return None
        sma_fast = df["close"].rolling(fast_period).mean().iloc[-1]
        sma_slow = df["close"].rolling(slow_period).mean().iloc[-1]
        price = df["close"].iloc[-1]
        # Both SMAs must confirm bull — if either disagrees it's bear
        above_fast = price > sma_fast
        above_slow = price > sma_slow
        regime = "bull" if (above_fast and above_slow) else "bear"
        print_status("REGIME", f"price={price:.2f} sma{fast_period}={sma_fast:.2f} sma{slow_period}={sma_slow:.2f} above_fast={above_fast} above_slow={above_slow} -> {regime}")
        return regime
    except Exception as e:
        print_status("REGIME_ERROR", str(e))
        return None


def find_equity_position(portfolio: Dict[str, Any], symbol: str) -> Optional[Dict[str, Any]]:
    positions: list[Dict[str, Any]] = cast(list[Dict[str, Any]], portfolio.get("positions") or [])
    for p_typed in positions:
        inst: Dict[str, Any] = cast(Dict[str, Any], p_typed.get("instrument") or {})
        if inst.get("type") == "EQUITY" and str(inst.get("symbol") or "").upper() == symbol.upper():
            return p_typed
    return None


def count_open_positions(portfolio: Dict[str, Any], symbols: List[str]) -> int:
    return sum(1 for s in symbols if find_equity_position(portfolio, s))


def get_buying_power_usd(portfolio: Dict[str, Any]) -> float:
    bp: Dict[str, Any] = portfolio.get("buyingPower") or {}
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
    equity_items: list[Dict[str, Any]] = cast(list[Dict[str, Any]], portfolio.get("equity") or [])
    for item_d in equity_items:
        try:
            total += float(item_d.get("value", 0.0) or 0.0)
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
    try:
        qty = float(pos.get("quantity", 0.0) or 0.0)
        lp2: Dict[str, Any] = pos.get("lastPrice") or {}
        px = float(lp2.get("lastPrice", 0.0) or 0.0)
        return qty * px
    except Exception:
        return 0.0


def get_unrealized_pnl_pct(pos: Dict[str, Any]) -> Optional[float]:
    ig: Dict[str, Any] = pos.get("instrumentGain") or {}
    gp: Optional[Any] = ig.get("gainPercentage")
    if gp is None:
        return None
    try:
        return float(gp) / 100.0
    except Exception:
        return None


def get_unrealized_pnl_usd(pos: Dict[str, Any]) -> Optional[float]:
    ig: Dict[str, Any] = pos.get("instrumentGain") or {}
    v: Optional[Any] = ig.get("gain")
    if v is None:
        return None
    try:
        return float(v)
    except Exception:
        return None


def get_last_price(pos: Dict[str, Any]) -> Optional[float]:
    # Try nested lastPrice.lastPrice first (Public portfolio format)
    lp: Dict[str, Any] = pos.get("lastPrice") or {}
    v: Optional[Any] = lp.get("lastPrice")
    if v is not None:
        try:
            return float(v)
        except Exception:
            pass
    # Try top-level price fields
    for key in ("lastPrice", "currentPrice", "price", "marketValue", "last", "mark"):
        val = pos.get(key)
        if val is not None and not isinstance(val, dict):
            try:
                return float(val)
            except Exception:
                pass
    # Fallback: derive from position value / quantity
    try:
        qty = get_position_qty(pos)
        val_usd = get_position_value_usd(pos)
        if qty and qty > 0 and val_usd and val_usd > 0:
            return round(val_usd / qty, 4)
    except Exception:
        pass
    return None


def get_avg_cost(pos: Dict[str, Any]) -> Optional[float]:
    for key in ("averageCost", "avgCost", "costBasis", "averagePrice"):
        v = pos.get(key)
        if v is not None:
            try:
                return float(v)
            except Exception:
                pass
    return None




# -----------------------------
# Confidence Score
# -----------------------------
def compute_confidence(
    momentum_score: float,
    trend_str: float,
    breakout_margin: float,
    regime: Optional[str],
) -> float:
    """
    Compute a 0.0–1.0 confidence score combining momentum, trend, and breakout signals.

    momentum_score:   raw momentum score (typically -0.05 to +0.05)
    trend_str:        trend_strength() output (0.0 to ~0.02)
    breakout_margin:  how far price cleared the breakout level as % of range (0.0 to 1.0+)
    regime:           "bull" / "bear" / None

    Returns:
        confidence in [0.0, 1.0]
        >= 0.7 → high  → full size
        0.4–0.7 → medium → 75% size
        < 0.4  → low   → 50% size
    """
    # Normalize each component to 0–1
    mom_norm = max(0.0, min(1.0, momentum_score / 0.05))      # 0.05 = strong momentum
    trend_norm = max(0.0, min(1.0, trend_str / 0.01))          # 0.01 = strong trend
    bo_norm = max(0.0, min(1.0, breakout_margin))               # already 0–1

    # Weighted combination
    raw = (mom_norm * 0.40) + (trend_norm * 0.30) + (bo_norm * 0.30)

    # Bear regime penalty
    if regime == "bear":
        raw *= 0.7

    return round(max(0.0, min(1.0, raw)), 4)


def confidence_size_factor(confidence: float) -> float:
    """Map confidence to position size multiplier."""
    if confidence >= 0.7:
        return 1.0    # full size
    elif confidence >= 0.4:
        return 0.75   # medium
    else:
        return 0.5    # low confidence — half size

# -----------------------------
# Trading Logic (LIVE)
# -----------------------------
def run_live_cycle(
    client: PublicClient,
    *,
    log_file: str,
    symbols: List[str],
    scan_symbols: List[str],
    max_trades_per_day: int,
    max_order_amount_usd: float,
    max_open_positions: int,
    atr_vol_threshold: float,
    take_profit_base: float,
    stop_loss_base: float,
    trend_strong_threshold: float,
    base_position_size: float,
    max_position_scale: float,
    withdraw_date: Optional[date],
    lockdown_days: int,
    max_exposure_early: float,
    max_exposure_mid: float,
    max_exposure_late: float,
    max_drawdown: float,
    daily_loss_limit: float,
    loss_cooldown_days: int,
    manage_all_positions: bool,
    atr_period: int,
    trend_fast: int,
    trend_slow: int,
    momentum_symbols: List[str],
    momentum_top_n: int = 3,
    momentum_sell_rank: int = 5,
    confidence_min_threshold: float = 0.3,
    breakout_lookback: int = 20,
    min_hold_days: int = 1,
) -> None:

    global _last_regime
    # Restore last regime from Supabase so GHA fresh runners don't spam "UNKNOWN → BULL"
    if _last_regime is None:
        _last_regime = _load_last_regime_from_supabase()
    regime = get_market_regime()
    print_status("MARKET_REGIME", str(regime))
    if regime != _last_regime:
        # Suppress UNKNOWN → X notifications (happens on fresh GHA runners)
        if _last_regime is not None:
            notify_regime_change(_last_regime, regime)
        _last_regime = regime

    # ── Momentum ranking (computed once per cycle) ───────────────────────────
    momentum_scores = rank_symbols(momentum_symbols)
    for ms in momentum_scores[:10]:
        if ms.valid:
            append_log(log_file, "MOMENTUM_SCORE", symbol=ms.symbol,
                details=f"rank={ms.rank} score={ms.score:.4f} r1d={ms.ret_1d:.4f} r5d={ms.ret_5d:.4f} r10d={ms.ret_10d:.4f}")
        print_status("MOMENTUM", f"{ms.symbol} rank={ms.rank} score={ms.score:.4f}")

    if os.path.exists(STOP_FILE):
        append_log(log_file, "STOPPED", details="STOP file detected")
        return

    trades_today = count_trades_today(log_file)
    if trades_today >= max_trades_per_day:
        append_log(log_file, "SKIP", details=f"Trade limit reached (trades_today={trades_today})")
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

    # Full watchlist: SYMBOLS + SCAN_SYMBOLS, de-duped
    all_managed = list(dict.fromkeys(symbols + scan_symbols))

    # Build snapshots of currently held positions
    snapshots: list[dict[str, Any]] = []
    total_pos_value = 0.0
    snapshot_symbols = all_managed if manage_all_positions else symbols

    for sym in snapshot_symbols:
        pos = find_equity_position(portfolio, sym)
        if not pos:
            continue
        qty = get_position_qty(pos)
        if qty <= 0:
            continue
        pos_value = get_position_value_usd(pos)
        pnl_pct = get_unrealized_pnl_pct(pos)
        pnl_usd = get_unrealized_pnl_usd(pos)
        last_px = get_last_price(pos)
        avg_cost = get_avg_cost(pos)
        if last_px is None:
            print_status("PRICE_FALLBACK", f"{sym} last_px=None from portfolio — pos keys={list(pos.keys())[:8]}")

        snapshots.append({
            "symbol": sym,
            "pos": pos,
            "qty": float(qty),
            "value": float(pos_value),
            "pnl_pct": pnl_pct,
            "pnl_usd": pnl_usd,
            "last": last_px,
            "avg_cost": avg_cost,
        })
        total_pos_value += float(pos_value)

    total_exposure = total_pos_value / total_equity if total_equity > 0 else 0.0

    notify_run_start(
        symbols=all_managed[:5],
        regime=regime,
        equity=total_equity,
        buying_power=buying_power,
    )

    append_log(
        log_file, "PORTFOLIO",
        details=(
            f"equity={total_equity:.2f} pos_value={total_pos_value:.2f} "
            f"exposure={total_exposure:.2%} bp={buying_power:.2f} "
            f"days_left={days_left} cap_total={cap_total:.2%} "
            f"open_positions={len(snapshots)}"
        ),
    )
    print_status(
        "PORTFOLIO",
        f"equity={total_equity:.2f} exposure={total_exposure:.2%} "
        f"cap={cap_total:.2%} positions={len(snapshots)}",
    )

    # ----------------------------------------------------------
    # Safety guard: empty portfolio while Supabase has open positions
    # ----------------------------------------------------------
    # If Public returned equity=0 / bp=0 / no snapshots but we have open
    # positions recorded in Supabase, the API is almost certainly pointed at
    # the wrong account (e.g. PUBLIC_ACCOUNT_ID missing when the token can
    # see multiple accounts). Without this guard the exit loop iterates an
    # empty list and silently skips TP/SL for every real holding.
    if total_equity == 0 and buying_power == 0 and not snapshots:
        ghost_opens: list[str] = []
        if supabase is not None:
            try:
                ghost_res = (
                    supabase.table("positions")
                    .select("symbol")
                    .eq("status", "open")
                    .execute()
                )
                ghost_opens = [
                    str(r.get("symbol") or "")
                    for r in (ghost_res.data or [])
                    if r.get("symbol")
                ]
            except Exception as _e:
                print_status("GHOST_CHECK_ERROR", str(_e))
        if ghost_opens:
            msg = (
                f"Public portfolio is empty (equity=0, bp=0, positions=0) "
                f"but Supabase has {len(ghost_opens)} open positions: "
                f"{','.join(sorted(set(ghost_opens))[:10])}. "
                f"Likely wrong account selected — set PUBLIC_ACCOUNT_ID. "
                f"Skipping cycle to avoid silently missing TP/SL."
            )
            append_log(log_file, "PORTFOLIO_EMPTY_ABORT", details=msg)
            print_status("PORTFOLIO_EMPTY_ABORT", msg)
            try:
                notify_error("run_live_cycle", msg, severity="critical")
            except Exception:
                pass
            return

    # ----------------------------------------------------------
    # Hard lockdown: sell everything we manage
    # ----------------------------------------------------------
    in_lockdown = days_left is not None and days_left <= lockdown_days
    if in_lockdown and snapshots:
        for snap in snapshots:
            if trades_today >= max_trades_per_day:
                break
            sym = snap["symbol"]
            qty = snap["qty"]
            result = client.place_market_sell_quantity(sym, qty)
            trades_today += 1
            monitor.trade_count += 1
            log_trade(
                symbol=sym, side="SELL",
                entry_price=snap["avg_cost"], exit_price=snap["last"],
                pnl_pct=snap["pnl_pct"], pnl_usd=snap["pnl_usd"],
                amount_usd=snap["value"], strategy="lockdown",
            )
            append_log(
                log_file, "LIVE_SELL_SUBMITTED",
                symbol=sym, side="SELL",
                amount_usd=f"{snap['value']:.2f}",
                order_id=result["orderId"],
                status="SUBMITTED" if not client.dry_run else "DRY_RUN",
                details=f"LOCKDOWN days_left={days_left} qty={qty:.8f}",
            )
            print_status("SELL_SUBMITTED", f"{sym} LOCKDOWN qty={qty:.8f}")
        save_state(state)
        return

    # ----------------------------------------------------------
    # Per-symbol kill switches + TP/SL (EXIT LOGIC)
    # ----------------------------------------------------------
    for snap in snapshots:
        if trades_today >= max_trades_per_day:
            break

        sym = snap["symbol"]
        qty = snap["qty"]
        pnl_pct = snap["pnl_pct"]
        pnl_usd = snap["pnl_usd"]
        current_price = snap["last"]
        entry_price = snap["avg_cost"]
        pos_value = snap["value"]

        sstate = sym_state(state, sym)

        # Roll day state at start of new trading day
        dkey = today_key_et()
        if sstate.get("day_key_et") != dkey:
            sstate["day_key_et"] = dkey
            sstate["day_start_value"] = float(pos_value)
            if float(sstate.get("peak_value") or 0.0) <= 0:
                sstate["peak_value"] = float(pos_value)
            if (sstate.get("halt_until_day_key_et") or "") < dkey:
                sstate["halt_until_day_key_et"] = ""

        if should_halt_symbol(sstate):
            append_log(log_file, "SKIP", symbol=sym,
                       details=f"HALTED cooldown={sstate.get('cooldown_until')} halt_day={sstate.get('halt_until_day_key_et')}")
            print_status("SKIP", f"{sym} halted")
            continue

        # ── Authoritative entry lookup ──────────────────────────────────
        # Prefer the bot's own ledger (local sstate → Supabase positions row)
        # over Public.com's instrumentGain.gainPercentage. The API value uses
        # average cost basis across ALL lots — including any manual buys you
        # placed in the Public app — so it can disagree significantly with
        # what the bot actually paid. The bot only owns the orders it placed
        # itself; using its captured fill price gives deterministic TP/SL.
        bot_entry_price: Optional[float] = None
        try:
            _e = sstate.get("entry_price")
            if _e is not None:
                _ef = float(_e)
                if _ef > 0:
                    bot_entry_price = _ef
        except Exception:
            bot_entry_price = None
        entry_date_str = str(sstate.get("entry_date") or sstate.get("entry_day") or "")
        if bot_entry_price is None or not entry_date_str:
            _pos = get_open_position(sym)
            if _pos:
                if bot_entry_price is None:
                    try:
                        ep = _pos.get("entry_price")
                        if ep is not None:
                            epf = float(ep)
                            if epf > 0:
                                bot_entry_price = epf
                                sstate["entry_price"] = epf
                    except Exception:
                        pass
                if not entry_date_str:
                    ed = str(_pos.get("entry_date") or "")
                    if ed:
                        entry_date_str = ed
                        sstate["entry_date"] = ed

        # Override pnl_pct with bot's own basis if we have a real entry price.
        # API's avg-basis pnl is kept as `api_pnl_pct` for divergence logging.
        api_pnl_pct = pnl_pct  # what Public's portfolio v2 reported
        if bot_entry_price is not None and current_price is not None and float(current_price) > 0:
            new_pnl_pct = (float(current_price) - bot_entry_price) / bot_entry_price
            if api_pnl_pct is not None and abs(new_pnl_pct - api_pnl_pct) >= 0.005:
                append_log(log_file, "PNL_BASIS_DIFF", symbol=sym,
                    details=(
                        f"bot_entry={bot_entry_price:.4f} bot_pnl={new_pnl_pct:.2%} | "
                        f"api_avg_pnl={api_pnl_pct:.2%} | "
                        f"diff={(new_pnl_pct - api_pnl_pct):.2%} "
                        f"(bot uses bot_entry for TP/SL — api avg includes any manual lots)"
                    ))
            pnl_pct = new_pnl_pct
            # entry_price below is used for log fields — keep it bot-basis too.
            entry_price = bot_entry_price
            # pnl_usd is best-effort: pnl_pct * pos_value approximates dollar PnL,
            # accurate to the order of (1 + pnl_pct). Used for notifications only.
            pnl_usd = pnl_pct * float(pos_value or 0)

        # Minimum hold period — prevents PDT violations (default 1 = no same-day sells)
        if entry_date_str and min_hold_days > 0:
            try:
                entry_dt = datetime.strptime(entry_date_str, "%Y-%m-%d").date()
                days_held = (today_date_et() - entry_dt).days
                if days_held < min_hold_days:
                    append_log(log_file, "SKIP", symbol=sym,
                        details=f"MIN_HOLD: held {days_held}d < {min_hold_days}d required (entry={entry_date_str})")
                    print_status("SKIP", f"{sym} MIN_HOLD {days_held}d < {min_hold_days}d")
                    continue
            except Exception:
                pass

        # Momentum exit: sell if rank faded or score went negative (only for momentum symbols)
        # Don't momentum_exit at a loss — let stop loss handle it to avoid fee drag
        FEE_BUFFER = 0.005  # 0.5% minimum gain to cover fees before momentum exit
        if sym in momentum_symbols and should_sell_momentum(sym, momentum_scores, momentum_sell_rank) and (pnl_pct is None or pnl_pct >= FEE_BUFFER):
            try:
                result = client.place_market_sell_quantity(sym, qty)
            except Exception as sell_err:
                append_log(log_file, "ORDER_ERROR", symbol=sym, details=f"MOMENTUM_EXIT sell failed: {sell_err}")
                monitor.log_error("place_sell_momentum", sell_err, symbol=sym, severity="warning")
                continue
            trades_today += 1
            monitor.trade_count += 1
            monitor.log_event("ORDER_FILLED", symbol=sym, metadata={"side": "SELL", "strategy": "momentum_exit"})
            # Fetch fill price FIRST, then compute PnL, then log — correct order
            sell_fill = client.get_fill_price(result["orderId"], log_file) if not client.dry_run else None
            # Get entry price: local state → Supabase positions → snapshot → 0
            _raw_entry = sstate.get("entry_price")
            if not _raw_entry or float(_raw_entry) <= 0:
                _pos = get_open_position(sym)
                if _pos:
                    _raw_entry = _pos.get("entry_price")
            true_entry = float(_raw_entry or entry_price or 0)
            true_amount = float(sstate.get("entry_amount") or pos_value or 0)
            actual_exit = sell_fill if sell_fill and sell_fill > 0 else float(current_price or 0)
            true_pnl_pct = ((actual_exit - true_entry) / true_entry) if true_entry > 0 else None
            true_pnl_usd = (true_pnl_pct * true_amount) if true_pnl_pct is not None else None
            sstate.pop("entry_price", None)
            sstate.pop("entry_amount", None)
            sstate.pop("entry_day", None)
            sstate.pop("entry_date", None)
            log_trade(
                symbol=sym, side="SELL",
                entry_price=true_entry if true_entry > 0 else None,
                exit_price=actual_exit,
                pnl_pct=true_pnl_pct, pnl_usd=true_pnl_usd,
                amount_usd=true_amount, strategy="momentum_exit",
            )
            append_log(
                log_file, "LIVE_SELL_SUBMITTED",
                symbol=sym, side="SELL",
                amount_usd=f"{true_amount:.2f}",
                order_id=result["orderId"],
                status="SUBMITTED" if not client.dry_run else "DRY_RUN",
                details=f"MOMENTUM_EXIT entry={true_entry or 0:.2f} exit={actual_exit:.2f} pnl={true_pnl_pct or 0:.2%} qty={qty:.8f} fill={sell_fill or 'N/A'}",
            )
            print_status("SELL_SUBMITTED", f"{sym} MOMENTUM_EXIT pnl={true_pnl_pct or 0:.2%} fill={actual_exit:.2f}")
            # Calculate estimated PnL for Discord if true values unavailable
            _disc_pnl_pct = true_pnl_pct
            _disc_pnl_usd = true_pnl_usd
            if _disc_pnl_pct is None and true_entry > 0:
                _disc_pnl_pct = (actual_exit - true_entry) / true_entry
                _disc_pnl_usd = _disc_pnl_pct * true_amount
            notify_sell(sym, actual_exit, _disc_pnl_pct, _disc_pnl_usd, true_amount, "momentum_exit")
            close_position(sym, actual_exit, true_pnl_pct, true_pnl_usd, "momentum_exit")
            sstate["halt_until_day_key_et"] = dkey
            continue

        # Update peak for drawdown tracking
        peak = max(float(sstate.get("peak_value") or 0.0), float(pos_value))
        sstate["peak_value"] = peak
        dd = (1.0 - float(pos_value) / peak) if peak > 0 else 0.0

        start_val = float(sstate.get("day_start_value") or float(pos_value) or 0.0)
        daily_ret = (float(pos_value) / start_val - 1.0) if start_val > 0 else 0.0

        append_log(
            log_file, "SYMBOL_STATE", symbol=sym,
            details=(
                f"value={pos_value:.2f} daily_ret={daily_ret:.2%} "
                f"dd={dd:.2%} peak={peak:.2f} start={start_val:.2f} "
                f"last={current_price or 'N/A'} avg_cost={entry_price or 'N/A'}"
            ),
        )

        # Kill switch: daily loss limit
        if daily_loss_limit > 0 and daily_ret <= -daily_loss_limit:
            try:
                result = client.place_market_sell_quantity(sym, qty)
            except Exception as sell_err:
                append_log(log_file, "ORDER_ERROR", symbol=sym, details=f"DAILY_LOSS_LIMIT sell failed: {sell_err}")
                monitor.log_error("place_sell_daily_loss", sell_err, symbol=sym, severity="warning")
                continue
            trades_today += 1
            monitor.trade_count += 1
            log_trade(
                symbol=sym, side="SELL",
                entry_price=entry_price, exit_price=current_price,
                pnl_pct=pnl_pct, pnl_usd=pnl_usd,
                amount_usd=pos_value, strategy="daily_loss_limit",
            )
            append_log(
                log_file, "LIVE_SELL_SUBMITTED",
                symbol=sym, side="SELL",
                amount_usd=f"{pos_value:.2f}",
                order_id=result["orderId"],
                status="SUBMITTED" if not client.dry_run else "DRY_RUN",
                details=f"DAILY_LOSS_LIMIT entry={entry_price or 0:.2f} exit={current_price or 0:.2f} daily_ret={daily_ret:.4%} qty={qty:.8f}",
            )
            print_status("SELL_SUBMITTED", f"{sym} DAILY_LOSS_LIMIT daily={daily_ret:.2%}")
            sstate["halt_until_day_key_et"] = dkey
            trigger_cooldown(sstate, loss_cooldown_days)
            continue

        # Kill switch: max drawdown
        if max_drawdown > 0 and dd >= max_drawdown:
            try:
                result = client.place_market_sell_quantity(sym, qty)
            except Exception as sell_err:
                append_log(log_file, "ORDER_ERROR", symbol=sym, details=f"MAX_DRAWDOWN sell failed: {sell_err}")
                monitor.log_error("place_sell_max_drawdown", sell_err, symbol=sym, severity="warning")
                continue
            trades_today += 1
            monitor.trade_count += 1
            log_trade(
                symbol=sym, side="SELL",
                entry_price=entry_price, exit_price=current_price,
                pnl_pct=pnl_pct, pnl_usd=pnl_usd,
                amount_usd=pos_value, strategy="max_drawdown",
            )
            append_log(
                log_file, "LIVE_SELL_SUBMITTED",
                symbol=sym, side="SELL",
                amount_usd=f"{pos_value:.2f}",
                order_id=result["orderId"],
                status="SUBMITTED" if not client.dry_run else "DRY_RUN",
                details=f"MAX_DRAWDOWN entry={entry_price or 0:.2f} exit={current_price or 0:.2f} dd={dd:.4%} qty={qty:.8f}",
            )
            print_status("SELL_SUBMITTED", f"{sym} MAX_DRAWDOWN dd={dd:.2%}")
            sstate["halt_until_day_key_et"] = dkey
            trigger_cooldown(sstate, loss_cooldown_days)
            continue

        # Dynamic TP / SL
        # If Public API doesn't return gainPercentage, calculate from stored entry_price
        # Fall back to Supabase positions table if not in local state (survives runner restarts)
        if pnl_pct is None:
            stored_entry = sstate.get("entry_price")
            entry_source = "sstate"
            if not stored_entry:
                pos_record = get_open_position(sym)
                if pos_record:
                    stored_entry = pos_record.get("entry_price")
                    entry_source = "supabase_positions"
                    if stored_entry:
                        sstate["entry_price"] = float(stored_entry)
                        sstate["entry_date"] = str(pos_record.get("entry_date") or "")
            if stored_entry and current_price and float(stored_entry) > 0:
                pnl_pct = (float(current_price) - float(stored_entry)) / float(stored_entry)
                append_log(log_file, "SYMBOL_STATE", symbol=sym,
                    details=f"pnl_pct from entry_price ({entry_source}): entry={float(stored_entry):.2f} last={current_price:.2f} pnl={pnl_pct:.2%}")
            else:
                # Previously a silent `continue` — every TP/SL was skipped without
                # explanation. Make this loud so we can see in the dashboard exactly
                # which positions are missing entry data and why.
                append_log(log_file, "SKIP", symbol=sym,
                    details=(
                        f"TP/SL skipped: pnl_pct unavailable "
                        f"(api_gain_pct=None, sstate_entry={sstate.get('entry_price')}, "
                        f"supabase_entry={stored_entry}, last_price={current_price})"
                    ))
                print_status("SKIP", f"{sym} TP/SL no pnl_pct, no entry_price found")
                continue

        df = get_daily_bars(sym)
        if df is None or len(df) < trend_slow + 5:
            append_log(log_file, "SKIP", symbol=sym,
                details=f"TP/SL skipped: insufficient bars (got {len(df) if df is not None else 0}, need {trend_slow + 5})")
            print_status("SKIP", f"{sym} TP/SL insufficient bars")
            continue

        atr = calculate_atr(df, atr_period)
        price = float(df["close"].iloc[-1])

        if not price or not atr or atr / price < atr_vol_threshold:
            atr_pct = (atr / price) if (atr and price) else 0.0
            append_log(log_file, "SKIP", symbol=sym,
                details=(
                    f"TP/SL skipped: ATR gate "
                    f"(price={price}, atr={atr}, atr/price={atr_pct:.4%} < threshold={atr_vol_threshold:.2%})"
                ))
            print_status("SKIP", f"{sym} TP/SL ATR gate atr_pct={atr_pct:.4%} < {atr_vol_threshold:.2%}")
            continue

        strength = trend_strength(df, trend_fast, trend_slow)

        # Scale TP based on trend strength (from risk.py dynamic_take_profit)
        scale = min(1 + (strength / trend_strong_threshold), max_position_scale)
        dynamic_tp = take_profit_base * (1 + 0.5 * min(scale - 1, 1.5))
        dynamic_sl = stop_loss_base * (0.8 if regime == "bear" else 1.0)

        # Add fee buffer so we only TP if gain meaningfully exceeds fees
        fee_adjusted_tp = dynamic_tp + 0.003  # ~0.3% fee buffer per round trip
        if pnl_pct >= fee_adjusted_tp:
            try:
                result = client.place_market_sell_quantity(sym, qty)
            except Exception as sell_err:
                append_log(log_file, "ORDER_ERROR", symbol=sym, details=f"DYNAMIC_TP sell failed: {sell_err}")
                monitor.log_error("place_sell_tp", sell_err, symbol=sym, severity="warning")
                continue
            trades_today += 1
            monitor.trade_count += 1
            # Use stored entry price for accurate PnL if available
            # Fetch fill price FIRST, then compute PnL, then log — correct order
            sell_fill = client.get_fill_price(result["orderId"], log_file) if not client.dry_run else None
            _raw_entry = sstate.get("entry_price")
            if not _raw_entry or float(_raw_entry) <= 0:
                _pos = get_open_position(sym)
                if _pos:
                    _raw_entry = _pos.get("entry_price")
            true_entry = float(_raw_entry or entry_price or 0)
            true_amount = float(sstate.get("entry_amount") or pos_value or 0)
            actual_exit = sell_fill if sell_fill and sell_fill > 0 else float(current_price or 0)
            true_pnl_pct = ((actual_exit - true_entry) / true_entry) if true_entry > 0 else None
            true_pnl_usd = (true_pnl_pct * true_amount) if true_pnl_pct is not None else None
            sstate.pop("entry_price", None)
            sstate.pop("entry_amount", None)
            sstate.pop("entry_day", None)
            sstate.pop("entry_date", None)
            log_trade(
                symbol=sym, side="SELL",
                entry_price=true_entry if true_entry > 0 else None,
                exit_price=actual_exit,
                pnl_pct=true_pnl_pct, pnl_usd=true_pnl_usd,
                amount_usd=true_amount, strategy="dynamic_tp",
            )
            append_log(
                log_file, "LIVE_SELL_SUBMITTED",
                symbol=sym, side="SELL",
                amount_usd=f"{true_amount:.2f}",
                order_id=result["orderId"],
                status="SUBMITTED" if not client.dry_run else "DRY_RUN",
                details=f"DYNAMIC_TP entry={true_entry or 0:.2f} exit={actual_exit:.2f} pnl={true_pnl_pct or 0:.2%} tp={fee_adjusted_tp:.2%} fill={sell_fill or 'N/A'}",
            )
            print_status("SELL_SUBMITTED", f"{sym} DYNAMIC_TP pnl={true_pnl_pct or 0:.2%} fill={actual_exit:.2f}")
            _disc_pnl_pct = true_pnl_pct
            _disc_pnl_usd = true_pnl_usd
            if _disc_pnl_pct is None and true_entry > 0:
                _disc_pnl_pct = (actual_exit - true_entry) / true_entry
                _disc_pnl_usd = _disc_pnl_pct * true_amount
            notify_sell(sym, actual_exit, _disc_pnl_pct, _disc_pnl_usd, true_amount, "dynamic_tp")
            close_position(sym, actual_exit, true_pnl_pct, true_pnl_usd, "dynamic_tp")
            sstate["halt_until_day_key_et"] = dkey

        elif pnl_pct <= -dynamic_sl:
            try:
                result = client.place_market_sell_quantity(sym, qty)
            except Exception as sell_err:
                append_log(log_file, "ORDER_ERROR", symbol=sym, details=f"DYNAMIC_SL sell failed: {sell_err}")
                monitor.log_error("place_sell_sl", sell_err, symbol=sym, severity="warning")
                continue
            trades_today += 1
            monitor.trade_count += 1
            # Fetch fill price FIRST, then compute PnL, then log — correct order
            sell_fill = client.get_fill_price(result["orderId"], log_file) if not client.dry_run else None
            _raw_entry = sstate.get("entry_price")
            if not _raw_entry or float(_raw_entry) <= 0:
                _pos = get_open_position(sym)
                if _pos:
                    _raw_entry = _pos.get("entry_price")
            true_entry = float(_raw_entry or entry_price or 0)
            true_amount = float(sstate.get("entry_amount") or pos_value or 0)
            actual_exit = sell_fill if sell_fill and sell_fill > 0 else float(current_price or 0)
            true_pnl_pct = ((actual_exit - true_entry) / true_entry) if true_entry > 0 else None
            true_pnl_usd = (true_pnl_pct * true_amount) if true_pnl_pct is not None else None
            sstate.pop("entry_price", None)
            sstate.pop("entry_amount", None)
            sstate.pop("entry_day", None)
            sstate.pop("entry_date", None)
            log_trade(
                symbol=sym, side="SELL",
                entry_price=true_entry if true_entry > 0 else None,
                exit_price=actual_exit,
                pnl_pct=true_pnl_pct, pnl_usd=true_pnl_usd,
                amount_usd=true_amount, strategy="dynamic_sl",
            )
            append_log(
                log_file, "LIVE_SELL_SUBMITTED",
                symbol=sym, side="SELL",
                amount_usd=f"{true_amount:.2f}",
                order_id=result["orderId"],
                status="SUBMITTED" if not client.dry_run else "DRY_RUN",
                details=f"DYNAMIC_SL entry={true_entry or 0:.2f} exit={actual_exit:.2f} pnl={true_pnl_pct or 0:.2%} sl={dynamic_sl:.2%} fill={sell_fill or 'N/A'}",
            )
            print_status("SELL_SUBMITTED", f"{sym} DYNAMIC_SL pnl={true_pnl_pct or 0:.2%} fill={actual_exit:.2f}")
            _disc_pnl_pct = true_pnl_pct
            _disc_pnl_usd = true_pnl_usd
            if _disc_pnl_pct is None and true_entry > 0:
                _disc_pnl_pct = (actual_exit - true_entry) / true_entry
                _disc_pnl_usd = _disc_pnl_pct * true_amount
            notify_sell(sym, actual_exit, _disc_pnl_pct, _disc_pnl_usd, true_amount, "dynamic_sl")
            close_position(sym, actual_exit, true_pnl_pct, true_pnl_usd, "dynamic_sl")
            sstate["halt_until_day_key_et"] = dkey
            trigger_cooldown(sstate, loss_cooldown_days)

        else:
            # Position is held but neither TP nor SL triggered. Log the decision
            # so the dashboard shows *why* we didn't sell (instead of just nothing).
            append_log(log_file, "TP_SL_HOLD", symbol=sym,
                details=(
                    f"pnl={pnl_pct:.2%} between -sl={(-dynamic_sl):.2%} and tp={fee_adjusted_tp:.2%} "
                    f"(strength={strength:.4f} scale={scale:.2f})"
                ))

    # ----------------------------------------------------------
    # ENTRY LOGIC
    # ----------------------------------------------------------
    open_positions = count_open_positions(portfolio, all_managed)
    _exp_str = f"{total_exposure:.2%}"
    _cap_str = f"{cap_total:.2%}"
    _bp_str  = f"{buying_power:.2f}"
    append_log(log_file, "ENTRY_SCAN", details=f"trades_today={trades_today} regime={regime} exposure={_exp_str} cap={_cap_str} bp={_bp_str} open={open_positions}/{max_open_positions}")

    # Keep a $25 buffer to avoid margin calls (maintenance requirement cushion)
    MARGIN_BUFFER = 25.0
    safe_buying_power = buying_power - MARGIN_BUFFER

    if (
        trades_today < max_trades_per_day
        and total_exposure < cap_total
        and safe_buying_power > 5
        and open_positions < max_open_positions
    ):
        # Buy candidates: union of momentum_symbols and all_managed, ranked by momentum
        buy_universe = list(dict.fromkeys(momentum_symbols + all_managed))
        for sym in buy_universe:
            if trades_today >= max_trades_per_day:
                break
            if open_positions >= max_open_positions:
                break

            # Skip if already holding (live portfolio is the source of truth)
            if find_equity_position(portfolio, sym):
                continue

            # Belt-and-suspenders: also check Supabase. If a recent buy hasn't
            # surfaced in the portfolio mirror yet (very rare for market orders
            # but possible across short cron cadences), this prevents a duplicate.
            existing_open = get_open_position(sym)
            if existing_open:
                append_log(log_file, "SKIP", symbol=sym,
                    details=f"Open Supabase position exists (entry={existing_open.get('entry_date')}); skipping buy")
                continue

            # Skip if in cooldown
            sstate = sym_state(state, sym)
            if should_halt_symbol(sstate):
                append_log(log_file, "SKIP", symbol=sym, details="In cooldown, skipping buy")
                continue

            # ── Signal check: momentum OR breakout ───────────────────
            mom_score = next((ms for ms in momentum_scores if ms.symbol == sym), None)
            mom_score_val = mom_score.score if (mom_score and mom_score.valid) else -999.0

            # Momentum buys require bull regime
            momentum_signal = (
                regime == "bull"
                and mom_score is not None
                and mom_score.valid
                and mom_score.score > 0
                and mom_score.rank <= momentum_top_n
            )

            # Only run breakout check if momentum didn't already qualify
            breakout_result = None
            breakout_signal = False
            breakout_size_factor = 1.0
            if not momentum_signal and sym in momentum_symbols:
                breakout_result = check_breakout(
                    sym, regime,
                    momentum_score=mom_score_val,
                    lookback=breakout_lookback,
                )
                breakout_signal = breakout_result.signal
                breakout_size_factor = breakout_result.size_factor
                append_log(log_file, "BREAKOUT_CHECK", symbol=sym,
                    details=f"signal={breakout_result.signal} reason={breakout_result.reason} "
                            f"price={breakout_result.price} threshold={breakout_result.threshold} "
                            f"regime={regime} size_factor={breakout_result.size_factor}")

            if not momentum_signal and not breakout_signal:
                reason = "no momentum or breakout signal"
                if mom_score and mom_score.valid:
                    reason = f"momentum rank={mom_score.rank} score={mom_score.score:.4f}, no breakout"
                append_log(log_file, "SKIP", symbol=sym, details=reason)
                continue

            signal_type = "momentum" if momentum_signal else "breakout"

            df = get_daily_bars(sym)
            if df is None or len(df) < trend_slow + 5:
                append_log(log_file, "SKIP", symbol=sym, details=f"no price data or insufficient bars (got {len(df) if df is not None else 0})")
                continue

            atr = calculate_atr(df, atr_period)
            price = float(df["close"].iloc[-1])

            if price <= 0 or not atr:
                append_log(log_file, "SKIP", symbol=sym, details="invalid price or ATR")
                continue

            if atr / price < atr_vol_threshold:
                append_log(log_file, "SKIP", symbol=sym, details="ATR below volatility threshold")
                continue

            strength = trend_strength(df, trend_fast, trend_slow)

            remaining_exposure = cap_total - total_exposure
            if remaining_exposure <= 0:
                break

            # ── Confidence score ──────────────────────────────────────────
            bo_margin = 0.0
            if breakout_result is not None and breakout_result.range_size and breakout_result.range_size > 0:
                bo_margin = max(0.0, (price - (breakout_result.recent_high or price)) / breakout_result.range_size)

            confidence = compute_confidence(
                momentum_score=mom_score_val,
                trend_str=strength,
                breakout_margin=bo_margin,
                regime=regime,
            )
            conf_factor = confidence_size_factor(confidence)

            if confidence < confidence_min_threshold:
                append_log(log_file, "SKIP", symbol=sym,
                    details=f"confidence={confidence:.4f} below min={confidence_min_threshold:.2f}")
                continue

            # Position sizing: base * scale * confidence factor
            scale = min(1 + (strength / trend_strong_threshold), max_position_scale)
            raw_alloc = min(
                total_equity * base_position_size * scale,
                max_order_amount_usd,
                total_equity * remaining_exposure,
                safe_buying_power * 0.95,
            )
            alloc_value = raw_alloc * conf_factor * breakout_size_factor

            if alloc_value < 1.0:
                continue

            qty = alloc_value / price
            if qty <= 0:
                continue

            try:
                result = client.place_market_buy_amount(sym, alloc_value)
            except Exception as order_err:
                append_log(log_file, "ORDER_ERROR", symbol=sym,
                    details=f"BUY failed: {order_err} qty={qty:.8f} alloc={alloc_value:.2f} price={price:.4f}")
                monitor.log_error("place_buy", order_err, symbol=sym, severity="warning")
                continue
            trades_today += 1
            monitor.trade_count += 1
            open_positions += 1
            total_pos_value += alloc_value
            total_exposure = total_pos_value / total_equity

            # Store entry price and date in state for accurate PnL tracking and hold period
            sstate = sym_state(state, sym)
            sstate["entry_price"] = price
            sstate["entry_amount"] = alloc_value
            sstate["entry_date"] = today_key_et()

            # Fetch actual fill price from Public API (more accurate than Polygon close)
            fill_price = client.get_fill_price(result["orderId"], log_file) if not client.dry_run else None
            actual_entry = fill_price if fill_price and fill_price > 0 else price
            entry_is_estimated = fill_price is None or fill_price <= 0

            log_trade(
                symbol=sym, side="BUY",
                entry_price=actual_entry, exit_price=None,
                pnl_pct=None, pnl_usd=None,
                amount_usd=alloc_value, strategy=signal_type,
            )
            append_log(
                log_file, "LIVE_BUY_SUBMITTED",
                symbol=sym, side="BUY",
                amount_usd=f"{alloc_value:.2f}",
                order_id=result["orderId"],
                status="SUBMITTED" if not client.dry_run else "DRY_RUN",
                details=f"signal={signal_type} confidence={confidence:.4f} conf_factor={conf_factor:.2f} strength={strength:.4f} scale={scale:.2f} alloc={alloc_value:.2f} price={actual_entry:.4f} fill={fill_price or 'N/A'}",
            )
            # Store actual fill price in state for accurate exit PnL
            sstate["entry_price"] = actual_entry
            print_status("BUY_SUBMITTED", f"{sym} alloc=${alloc_value:.2f} confidence={confidence:.4f} signal={signal_type} fill={actual_entry:.2f}")
            notify_buy(sym, actual_entry, alloc_value, signal_type, confidence)
            upsert_open_position(sym, actual_entry, today_key_et(), qty, alloc_value, signal_type, estimated=entry_is_estimated)

    save_state(state)

    # Send daily summary at market close
    maybe_send_daily_summary(log_file, total_equity, buying_power, regime)


# -----------------------------
# Main
# -----------------------------
def main() -> None:
    load_dotenv()

    # Initialize Supabase after env is loaded
    global supabase
    supabase_url = os.getenv("SUPABASE_URL")
    supabase_key = os.getenv("SUPABASE_SERVICE_KEY")
    if supabase_url and supabase_key:
        supabase = create_client(supabase_url, supabase_key)
        print_status("SUPABASE_INIT", "client initialized")
    else:
        print_status("SUPABASE_SKIP", "SUPABASE_URL or SUPABASE_SERVICE_KEY not set — trades won't sync")

    monitor.start_run()

    # League heartbeat — additive, fail-silent. Does NOT affect monitor.start_run()
    # or any trading logic. If the League project is unreachable, this no-ops.
    try:
        league_status.start_run("cron")
    except Exception:
        pass

    mode = get_env_str("MODE", "live").lower()
    if mode != "live":
        raise RuntimeError("This file is LIVE-only. Set MODE=live")

    symbols = parse_symbols_env("SYMBOLS", "SPY")
    scan_symbols = parse_symbols_env("SCAN_SYMBOLS", "")

    atr_period = get_env_int("ATR_PERIOD", 14)
    atr_vol_threshold = get_env_float("ATR_VOL_THRESHOLD", 0.01)

    trend_fast = get_env_int("TREND_FAST", 5)
    trend_slow = get_env_int("TREND_SLOW", 20)
    trend_strong_threshold = get_env_float("TREND_STRONG_THRESHOLD", 0.5)

    # Momentum-rotation config
    momentum_symbols_raw = get_env_str("MOMENTUM_SYMBOLS", "AAPL,MSFT,NVDA,AMZN,GOOGL,META,TSLA,QQQ,SPY")
    momentum_symbols = [s.strip().upper() for s in momentum_symbols_raw.split(",") if s.strip()]
    momentum_top_n = get_env_int("MOMENTUM_TOP_N", 3)
    momentum_sell_rank = get_env_int("MOMENTUM_SELL_RANK", 5)

    # Confidence score config
    confidence_min_threshold = get_env_float("CONFIDENCE_MIN_THRESHOLD", 0.3)

    # Breakout config
    breakout_lookback = get_env_int("BREAKOUT_LOOKBACK", 20)
    min_hold_days = get_env_int("MIN_HOLD_DAYS", 1)

    take_profit_base = get_env_float("TAKE_PROFIT_BASE", 0.04)
    stop_loss_base = get_env_float("STOP_LOSS_BASE", 0.02)

    base_position_size = get_env_float("BASE_POSITION_SIZE", 0.10)
    max_position_scale = get_env_float("MAX_POSITION_SCALE", 2.0)
    max_order_amount_usd = get_env_float("MAX_ORDER_AMOUNT_USD", 10.0)
    max_open_positions = get_env_int("MAX_OPEN_POSITIONS", 5)
    manage_all_positions = get_env_bool("MANAGE_ALL_POSITIONS", False)

    max_trades_per_day = get_env_int("MAX_TRADES_PER_DAY", 3)
    token_validity_minutes = get_env_int("TOKEN_VALIDITY_MINUTES", 15)
    poll_seconds = get_env_int("POLL_SECONDS", 900)
    run_once = get_env_bool("RUN_ONCE", False)
    dry_run = get_env_bool("DRY_RUN", False)

    withdraw_date = parse_withdraw_date_env()
    lockdown_days = get_env_int("LOCKDOWN_DAYS", 14)

    max_exposure_early = get_env_float("MAX_EXPOSURE_EARLY", 0.90)
    max_exposure_mid = get_env_float("MAX_EXPOSURE_MID", 0.75)
    max_exposure_late = get_env_float("MAX_EXPOSURE_LATE", 0.50)

    max_drawdown = get_env_float("MAX_DRAWDOWN", 0.08)
    daily_loss_limit = get_env_float("DAILY_LOSS_LIMIT", 0.03)
    loss_cooldown_days = get_env_int("LOSS_COOLDOWN_DAYS", 3)

    secret = os.getenv("PUBLIC_SECRET")
    if not secret or not secret.strip():
        raise RuntimeError("Missing PUBLIC_SECRET")

    ensure_dir("logs")
    log_file = os.path.join("logs", "trade_log.csv")

    append_log(
        log_file, "BOT_START",
        details=(
            f"symbols={symbols} scan={scan_symbols} "
            f"TP_BASE={take_profit_base:.4f} SL_BASE={stop_loss_base:.4f} "
            f"max_trades_day={max_trades_per_day} max_open={max_open_positions} "
            f"max_order=${max_order_amount_usd} base_pos={base_position_size} scale={max_position_scale} "
            f"poll={poll_seconds}s run_once={run_once} dry_run={dry_run} "
            f"withdraw_date={withdraw_date} lockdown_days={lockdown_days} "
            f"cap(early/mid/late)={max_exposure_early}/{max_exposure_mid}/{max_exposure_late} "
            f"max_dd={max_drawdown} daily_loss={daily_loss_limit} cooldown_days={loss_cooldown_days}"
        ),
    )
    print_status(
        "BOT_START",
        f"symbols={symbols} scan={scan_symbols} dry_run={dry_run} "
        f"withdraw_date={withdraw_date} max_open={max_open_positions}",
    )

    auth = PublicAuth(secret=secret, validity_minutes=token_validity_minutes, log_file=log_file)
    client = PublicClient(auth=auth, log_file=log_file, dry_run=dry_run)
    _ = client.get_account_id()

    while True:
        try:
            if os.path.exists(STOP_FILE):
                append_log(log_file, "STOPPED", details=f"{STOP_FILE} detected; stopping.")
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
                scan_symbols=scan_symbols,
                momentum_symbols=momentum_symbols,
                momentum_top_n=momentum_top_n,
                momentum_sell_rank=momentum_sell_rank,
                confidence_min_threshold=confidence_min_threshold,
                breakout_lookback=breakout_lookback,
                min_hold_days=min_hold_days,
                max_trades_per_day=max_trades_per_day,
                max_order_amount_usd=max_order_amount_usd,
                max_open_positions=max_open_positions,
                atr_vol_threshold=atr_vol_threshold,
                take_profit_base=take_profit_base,
                stop_loss_base=stop_loss_base,
                trend_strong_threshold=trend_strong_threshold,
                base_position_size=base_position_size,
                max_position_scale=max_position_scale,
                withdraw_date=withdraw_date,
                lockdown_days=lockdown_days,
                max_exposure_early=max_exposure_early,
                max_exposure_mid=max_exposure_mid,
                max_exposure_late=max_exposure_late,
                max_drawdown=max_drawdown,
                daily_loss_limit=daily_loss_limit,
                loss_cooldown_days=loss_cooldown_days,
                manage_all_positions=manage_all_positions,
                atr_period=atr_period,
                trend_fast=trend_fast,
                trend_slow=trend_slow,
            )

        except Exception as e:
            append_log(log_file, "ERROR", details=str(e))
            print_status("ERROR", str(e))
            notify_error("run_live_cycle", str(e), severity="warning")

        if run_once:
            return

        time.sleep(max(30, poll_seconds))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        monitor.log_error("bot_main", e, severity="critical")
        raise
    finally:
        # Always mark the run as complete, even if killed or crashed
        final_status = (
            "failed" if monitor.critical_error
            else "warning" if monitor.error_count > 0
            else "success"
        )
        try:
            monitor.end_run(final_status)
        except Exception:
            pass
        # League end_run — additive, fail-silent. Closes the bot_runs row in the
        # League project and pushes a final heartbeat with the run outcome.
        try:
            league_status.end_run(
                status=final_status,
                trade_count=monitor.trade_count,
                error_count=monitor.error_count,
            )
        except Exception:
            pass
        try:
            notify_run_end(
                status=final_status,
                trades=monitor.trade_count,
                errors=monitor.error_count,
                duration_ms=int((datetime.now(timezone.utc) - monitor.start_time).total_seconds() * 1000) if monitor.start_time else 0,
            )
        except Exception:
            pass