"""league_status.py — Trading Bot League heartbeat adapter (Stock bot).

ADDITIVE. This module touches NO trading logic. It only sends a heartbeat
and a bot_runs row to the Trading Bot League Supabase project so the
cross-bot dashboard and health monitor see this bot.

Mirrors `Trading Bot League/league_core/status.py`. Keep the two in sync
when either changes (rare — this is a tiny stable surface).

Safety properties:
  * FAIL-SILENT. If LEAGUE_SUPABASE_URL / LEAGUE_SUPABASE_KEY / LEAGUE_BOT_ID
    are unset, or any HTTP call errors, every function in this module is a
    no-op. The existing bot continues exactly as today.
  * Lazy env reads. Nothing here calls os.getenv() at import time, so it's
    safe to import before bot.main() runs load_dotenv().
  * No new dependencies. Uses `requests` which is already in requirements.txt.
  * Internal _current_run_id cache so callers don't need to thread a run_id
    through their existing finally blocks.
"""

from __future__ import annotations

import json
import os
import sys
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

try:
    import requests
except ImportError:  # pragma: no cover - bot already has requests
    requests = None  # type: ignore[assignment]


_current_run_id: Optional[str] = None  # set by start_run, read by end_run


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _config() -> Optional[dict[str, str]]:
    url = os.getenv("LEAGUE_SUPABASE_URL", "").rstrip("/")
    key = os.getenv("LEAGUE_SUPABASE_KEY", "")
    bot_id = os.getenv("LEAGUE_BOT_ID", "")
    if not url or not key or not bot_id:
        return None
    return {"url": url, "key": key, "bot_id": bot_id}


def _print(msg: str) -> None:
    print(f"[league] {msg}", file=sys.stdout, flush=True)


def _post(cfg, table, rows, *, upsert=False, return_repr=False, timeout=5.0):
    if requests is None:
        return None
    headers = {
        "apikey": cfg["key"],
        "Authorization": f"Bearer {cfg['key']}",
        "Content-Type": "application/json",
    }
    prefer = []
    if upsert:
        prefer.append("resolution=merge-duplicates")
    prefer.append("return=representation" if return_repr else "return=minimal")
    headers["Prefer"] = ",".join(prefer)
    url = f"{cfg['url']}/rest/v1/{table}"
    try:
        resp = requests.post(url, headers=headers, data=json.dumps(rows), timeout=timeout)
    except Exception as e:  # noqa: BLE001 - fail-silent at the boundary
        _print(f"POST {table} failed: {e!r}")
        return None
    if resp.status_code >= 400:
        _print(f"POST {table} status={resp.status_code} body={resp.text[:200]}")
        return None
    if return_repr:
        try:
            return resp.json()
        except Exception as e:  # noqa: BLE001
            _print(f"POST {table} json decode failed: {e!r}")
            return None
    return []


def _patch(cfg, table, filter_qs, payload, *, timeout=5.0) -> bool:
    if requests is None:
        return False
    headers = {
        "apikey": cfg["key"],
        "Authorization": f"Bearer {cfg['key']}",
        "Content-Type": "application/json",
        "Prefer": "return=minimal",
    }
    url = f"{cfg['url']}/rest/v1/{table}?{filter_qs}"
    try:
        resp = requests.patch(url, headers=headers, data=json.dumps(payload), timeout=timeout)
    except Exception as e:  # noqa: BLE001
        _print(f"PATCH {table} failed: {e!r}")
        return False
    if resp.status_code >= 400:
        _print(f"PATCH {table} status={resp.status_code} body={resp.text[:200]}")
        return False
    return True


# ── Public API ──────────────────────────────────────────────────────────────


def heartbeat(
    health: str = "healthy",
    details: Optional[dict[str, Any]] = None,
    *,
    last_run_id: Optional[str] = None,
    last_run_status: Optional[str] = None,
    last_error_msg: Optional[str] = None,
) -> None:
    """Upsert this bot's row in bot_status. No-op if League not configured."""
    cfg = _config()
    if cfg is None:
        return
    if health not in ("healthy", "degraded", "down", "unknown", "muted"):
        health = "unknown"
    row: dict[str, Any] = {
        "bot_id": cfg["bot_id"],
        "last_heartbeat_at": _now_iso(),
        "health": health,
        "details": details or {},
        "updated_at": _now_iso(),
    }
    if last_run_id is not None:
        row["last_run_id"] = last_run_id
    if last_run_status is not None:
        row["last_run_status"] = last_run_status
    if last_error_msg is not None:
        row["last_error_at"] = _now_iso()
        row["last_error_msg"] = last_error_msg[:500]
    _post(cfg, "bot_status", [row], upsert=True)


def start_run(trigger: str = "cron", git_sha: Optional[str] = None) -> Optional[str]:
    """Insert a bot_runs row (status='running'). Returns the new id, or None."""
    global _current_run_id
    cfg = _config()
    if cfg is None:
        return None
    run_id = str(uuid.uuid4())
    row = {
        "id": run_id,
        "bot_id": cfg["bot_id"],
        "started_at": _now_iso(),
        "status": "running",
        "trigger": trigger,
        "git_sha": git_sha or os.getenv("GITHUB_SHA"),
    }
    rows = _post(cfg, "bot_runs", [row], return_repr=True)
    if rows is None:
        return None
    _current_run_id = run_id
    heartbeat(health="healthy", last_run_id=run_id, last_run_status="running")
    return run_id


def end_run(
    run_id: Optional[str] = None,
    status: str = "success",
    *,
    trade_count: int = 0,
    error_count: int = 0,
    notes: Optional[str] = None,
) -> None:
    """Close out a bot_runs row. Falls back to the cached id if run_id=None."""
    global _current_run_id
    cfg = _config()
    rid = run_id if run_id is not None else _current_run_id
    if cfg is None or rid is None:
        # No League configured OR no open run — still emit a heartbeat if we can.
        if cfg is not None:
            heartbeat(health="healthy" if status == "success" else "degraded")
        return
    if status not in ("running", "success", "warning", "failed", "timeout"):
        status = "warning"
    payload: dict[str, Any] = {
        "ended_at": _now_iso(),
        "status": status,
        "trade_count": int(trade_count),
        "error_count": int(error_count),
    }
    if notes is not None:
        payload["notes"] = notes[:1000]
    _patch(cfg, "bot_runs", f"id=eq.{rid}", payload)
    health = {
        "success": "healthy",
        "warning": "degraded",
        "failed": "down",
        "timeout": "down",
        "running": "healthy",
    }.get(status, "unknown")
    heartbeat(
        health=health,
        last_run_id=rid,
        last_run_status=status,
        details={"trade_count": int(trade_count), "error_count": int(error_count)},
    )
    _current_run_id = None  # consumed


# ── Trade mirror ────────────────────────────────────────────────────────────
# A small allow-list of ETF tickers used by this bot. Anything not in this
# set defaults to asset_class='equity'. Hand-edit if you add new ETFs to
# the bot's SYMBOLS / SCAN_SYMBOLS / MOMENTUM_SYMBOLS.
_ETF_SYMBOLS = frozenset({
    "QQQ", "SPY", "VTI", "SCHB", "SCHD", "SGOV",
    "IWM", "DIA", "VOO", "VEA", "VWO",
    "XLK", "XLF", "XLE", "XLY", "XLV", "XLI", "XLP", "XLU", "XLB", "XLRE", "XLC",
    "BND", "TLT", "HYG", "LQD", "GLD", "SLV", "UUP",
})


def _classify(symbol: str) -> str:
    return "etf" if (symbol or "").upper() in _ETF_SYMBOLS else "equity"


def log_trade(
    symbol: str,
    side: str,
    *,
    quantity: Optional[float] = None,
    price: Optional[float] = None,
    amount_usd: Optional[float] = None,
    fees_usd: float = 0.0,
    pnl_usd: Optional[float] = None,
    pnl_pct: Optional[float] = None,
    reason: Optional[str] = None,
    strategy: Optional[str] = None,
    is_paper: bool = False,
    order_id: Optional[str] = None,
    run_id: Optional[str] = None,
    metadata: Optional[dict[str, Any]] = None,
) -> None:
    """Mirror a trade into the League's bot_trades table. Fail-silent.

    Side is uppercased and validated against the table's CHECK constraint.
    Asset class is auto-derived from a small ETF allow-list; anything else
    is recorded as 'equity'. The unique partial index on (bot_id, order_id)
    in the table makes repeat inserts of the same Public order_id idempotent
    at the DB level — but we do NOT retry here; one shot per call.
    """
    cfg = _config()
    if cfg is None:
        return
    side_u = (side or "").upper()
    if side_u not in ("BUY", "SELL", "SHORT", "COVER"):
        _print(f"log_trade: invalid side={side!r}; skipping")
        return
    rid = run_id if run_id is not None else _current_run_id
    row: dict[str, Any] = {
        "bot_id":      cfg["bot_id"],
        "run_id":      rid,
        "occurred_at": _now_iso(),
        "symbol":      symbol,
        "asset_class": _classify(symbol),
        "side":        side_u,
        "quantity":    quantity,
        "price":       price,
        "amount_usd":  amount_usd,
        "fees_usd":    fees_usd or 0,
        "pnl_usd":     pnl_usd,
        "pnl_pct":     pnl_pct,
        "reason":      reason,
        "strategy":    strategy,
        "is_paper":    bool(is_paper),
        "order_id":    order_id,
        "metadata":    metadata or {},
    }
    _post(cfg, "bot_trades", [row], upsert=False)


__all__ = ["heartbeat", "start_run", "end_run", "log_trade"]
