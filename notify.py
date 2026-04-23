"""
notify.py — Discord webhook notifications for the trading bot.

Sends rich embeds for:
- BUY / SELL trade executions
- Bot run start / end summaries
- Errors
- Regime changes
"""

from __future__ import annotations

import os
import requests
from datetime import datetime, timezone
from typing import Optional, Any


DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "")

# ── Colors ────────────────────────────────────────────────────────────────────
COLOR_GREEN   = 0x00ff9f   # wins / buys / success
COLOR_RED     = 0xff3864   # losses / errors / bear
COLOR_BLUE    = 0x00cfff   # info / run start
COLOR_YELLOW  = 0xffaa00   # warnings / sell
COLOR_PURPLE  = 0xbf5fff   # regime / neutral
COLOR_GREY    = 0x888888   # unknown


def _send(payload: dict[str, Any]) -> None:
    """Send a webhook payload. Never crashes the bot."""
    if not DISCORD_WEBHOOK_URL:
        return
    try:
        resp = requests.post(
            DISCORD_WEBHOOK_URL,
            json=payload,
            timeout=10,
        )
        resp.raise_for_status()
    except Exception as e:
        print(f"[NOTIFY] Discord webhook failed: {e}")


def _embed(
    title: str,
    description: str,
    color: int,
    fields: Optional[list[dict[str, Any]]] = None,
    footer: Optional[str] = None,
) -> dict[str, Any]:
    embed: dict[str, Any] = {
        "title": title,
        "description": description,
        "color": color,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    if fields:
        embed["fields"] = fields
    if footer:
        embed["footer"] = {"text": footer}
    return {"embeds": [embed]}


# ── Public API ─────────────────────────────────────────────────────────────────

def notify_buy(
    symbol: str,
    price: float,
    amount_usd: float,
    signal_type: str,
    confidence: float,
) -> None:
    payload: dict[str, Any] = _embed(
        title=f"📈 BUY — {symbol}",
        description=f"New position opened via **{signal_type}** signal",
        color=COLOR_GREEN,
        fields=[
            {"name": "Entry Price", "value": f"${price:.2f}", "inline": True},
            {"name": "Amount",      "value": f"${amount_usd:.2f}", "inline": True},
            {"name": "Confidence",  "value": f"{confidence:.0%}", "inline": True},
            {"name": "Strategy",    "value": signal_type.upper(), "inline": True},
        ],
        footer="Stock Trading Bot",
    )
    _send(payload)


def notify_sell(
    symbol: str,
    exit_price: float,
    pnl_pct: Optional[float],
    pnl_usd: Optional[float],
    amount_usd: float,
    strategy: str,
) -> None:
    win = (pnl_pct or 0) >= 0
    color = COLOR_GREEN if win else COLOR_RED
    emoji = "✅" if win else "❌"
    pnl_str = f"{'+' if win else ''}{pnl_pct*100:.2f}%" if pnl_pct is not None else "—"
    pnl_usd_str = f"${pnl_usd:.2f}" if pnl_usd is not None else "—"

    payload: dict[str, Any] = _embed(
        title=f"{emoji} SELL — {symbol}",
        description=f"Position closed via **{strategy}**",
        color=color,
        fields=[
            {"name": "Exit Price", "value": f"${exit_price:.2f}", "inline": True},
            {"name": "PnL %",      "value": pnl_str, "inline": True},
            {"name": "PnL $",      "value": pnl_usd_str, "inline": True},
            {"name": "Amount",     "value": f"${amount_usd:.2f}", "inline": True},
            {"name": "Strategy",   "value": strategy.upper(), "inline": True},
        ],
        footer="Stock Trading Bot",
    )
    _send(payload)


def notify_run_start(
    symbols: list[str],
    regime: Optional[str],
    equity: float,
    buying_power: float,
) -> None:
    regime_str = (regime or "unknown").upper()
    regime_emoji = "🟢" if regime == "bull" else "🔴" if regime == "bear" else "⚪"

    payload: dict[str, Any] = _embed(
        title="🤖 Bot Cycle Started",
        description=f"Regime: {regime_emoji} **{regime_str}**",
        color=COLOR_BLUE,
        fields=[
            {"name": "Equity",        "value": f"${equity:.2f}", "inline": True},
            {"name": "Buying Power",  "value": f"${buying_power:.2f}", "inline": True},
            {"name": "Symbols",       "value": ", ".join(symbols[:5]) + ("..." if len(symbols) > 5 else ""), "inline": False},
        ],
        footer="Stock Trading Bot",
    )
    _send(payload)


def notify_run_end(
    status: str,
    trades: int,
    errors: int,
    duration_ms: int,
) -> None:
    color = COLOR_GREEN if status == "success" else COLOR_YELLOW if status == "warning" else COLOR_RED
    emoji = "✅" if status == "success" else "⚠️" if status == "warning" else "❌"

    payload: dict[str, Any] = _embed(
        title=f"{emoji} Bot Cycle Complete",
        description=f"Status: **{status.upper()}**",
        color=color,
        fields=[
            {"name": "Trades",   "value": str(trades), "inline": True},
            {"name": "Errors",   "value": str(errors), "inline": True},
            {"name": "Duration", "value": f"{duration_ms / 1000:.1f}s", "inline": True},
        ],
        footer="Stock Trading Bot",
    )
    _send(payload)


def notify_error(
    stage: str,
    message: str,
    symbol: Optional[str] = None,
    severity: str = "warning",
) -> None:
    color = COLOR_RED if severity == "critical" else COLOR_YELLOW
    emoji = "🚨" if severity == "critical" else "⚠️"
    desc = f"`{message}`"
    if symbol:
        desc = f"**{symbol}** — {desc}"

    payload: dict[str, Any] = _embed(
        title=f"{emoji} Error — {stage}",
        description=desc,
        color=color,
        fields=[
            {"name": "Severity", "value": severity.upper(), "inline": True},
            {"name": "Stage",    "value": stage, "inline": True},
        ],
        footer="Stock Trading Bot",
    )
    _send(payload)


def notify_regime_change(
    old_regime: Optional[str],
    new_regime: Optional[str],
) -> None:
    if old_regime == new_regime:
        return
    color = COLOR_GREEN if new_regime == "bull" else COLOR_RED if new_regime == "bear" else COLOR_GREY
    emoji = "🟢" if new_regime == "bull" else "🔴" if new_regime == "bear" else "⚪"

    payload: dict[str, Any] = _embed(
        title=f"{emoji} Regime Change",
        description=f"Market regime shifted: **{(old_regime or 'unknown').upper()}** → **{(new_regime or 'unknown').upper()}**",
        color=color,
        footer="Stock Trading Bot",
    )
    _send(payload)