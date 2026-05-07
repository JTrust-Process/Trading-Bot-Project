"""
analyze_backtests.py — Multi-period decision-support tool over Public bars.

This is research / advisory only. It does NOT modify bot.py, order placement,
risk settings, or the live SCAN_SYMBOLS list. The output ranking is meant to
inform a human decision later.

What it does:
    1. Runs both the OLD strategy (current live logic, replayed on bars) and
       the NEW strategy (conservative bars-based variant) across multiple
       periods: MONTH, QUARTER, HALF_YEAR, YEAR.
    2. Aggregates per-symbol metrics across periods using the median (robust
       to one bad period dominating).
    3. Classifies each symbol into one of:
            keep_active        (label: strong)
            reduce_priority    (label: marginal)
            paper_only         (label: unstable / mixed)
            remove             (label: weak)
       The classification is based on the OLD strategy because that's what the
       live bot actually runs today.
    4. Writes two CSVs:
            analyze_detail.csv   — symbol × period × strategy detailed metrics
            analyze_ranking.csv  — per-symbol aggregated metrics + classification
       Plus a human-readable console table.

Usage:
    python analyze_backtests.py --source public
    python analyze_backtests.py --source yfinance
    python analyze_backtests.py --source public --periods QUARTER,HALF_YEAR,YEAR
    python analyze_backtests.py --source public --symbols SPY,QQQ,AAPL

Notes:
    - SPY is auto-included for the NEW strategy regime filter (ignored otherwise).
    - MONTH (~21 bars) is too short for the NEW strategy (which needs >= 50 bars
      for SMA(50) regime). Those slots will show 0 trades; that's expected.
    - Profit factor of `inf` (no losing trades) is capped at 9999 in CSV output
      and at 10 when computing the median for fairness.
"""

from __future__ import annotations

import argparse
import csv
import math
import statistics
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import pandas as pd

from backtest_public import (
    DEFAULT_SYMBOLS,
    load_bars,
    run_new_strategy,
    run_old_strategy,
)


# -----------------------------
# Config — easy to tune
# -----------------------------
VALID_PERIODS: List[str] = ["MONTH", "QUARTER", "HALF_YEAR", "YEAR"]

# Classification thresholds (applied to the OLD strategy's median metrics).
# Tweak these freely — they're advisory.
KEEP_ACTIVE_MIN_RETURN: float = 0.05    # +5% median return across periods
KEEP_ACTIVE_MIN_PF: float = 1.5
KEEP_ACTIVE_MIN_CONSISTENCY: float = 0.5  # >=50% of periods positive
KEEP_ACTIVE_MAX_DD: float = -0.20         # don't keep if median DD worse than -20%

REDUCE_MIN_RETURN: float = 0.0
REDUCE_MIN_PF: float = 1.0
REDUCE_MIN_CONSISTENCY: float = 0.5

REMOVE_MAX_RETURN: float = -0.05  # if median return below this, remove
REMOVE_MAX_PF: float = 0.8
REMOVE_MAX_CONSISTENCY: float = 0.25

# Cap profit factor at this value when computing medians, so an "inf"
# profit factor (zero losses) doesn't break statistics.
PF_CAP: float = 10.0
PF_CSV_CAP: float = 9999.0


# -----------------------------
# Per-period record
# -----------------------------
@dataclass
class PeriodResult:
    symbol: str
    period: str
    strategy: str  # "old" or "new"
    trades: int
    win_rate: float
    avg_win: float
    avg_loss: float
    win_loss_ratio: float       # avg_win / abs(avg_loss); 0 if no wins, inf if no losses
    max_drawdown: float
    profit_factor: float
    total_return: float
    bars: int


def _safe_pf(pf: float) -> float:
    """Cap inf for CSV/median use."""
    if pf == float("inf"):
        return PF_CSV_CAP
    if math.isnan(pf):
        return 0.0
    return float(pf)


def _safe_pf_for_median(pf: float) -> float:
    """Tighter cap for median aggregation so one inf doesn't dominate."""
    return min(_safe_pf(pf), PF_CAP)


def _winloss_ratio(avg_win: float, avg_loss: float) -> float:
    """avg_win / |avg_loss|. 0 if no wins; large finite cap if no losses."""
    if avg_win == 0.0 and avg_loss == 0.0:
        return 0.0
    if avg_loss == 0.0:
        return PF_CSV_CAP
    return avg_win / abs(avg_loss)


# -----------------------------
# Aggregation across periods
# -----------------------------
def aggregate(results: List[PeriodResult]) -> Dict[str, Any]:
    """Aggregate one (symbol, strategy) across periods using the median."""
    active = [r for r in results if r.trades > 0]
    if not active:
        return {
            "periods_tested": len(results),
            "periods_with_trades": 0,
            "median_total_return": 0.0,
            "median_profit_factor": 0.0,
            "median_win_rate": 0.0,
            "median_winloss_ratio": 0.0,
            "median_max_drawdown": 0.0,
            "total_trades": 0,
            "consistency": 0.0,
        }

    rets = [r.total_return for r in active]
    pos = sum(1 for r in rets if r > 0)
    return {
        "periods_tested": len(results),
        "periods_with_trades": len(active),
        "median_total_return": statistics.median(rets),
        "median_profit_factor": statistics.median(
            [_safe_pf_for_median(r.profit_factor) for r in active]
        ),
        "median_win_rate": statistics.median([r.win_rate for r in active]),
        "median_winloss_ratio": statistics.median(
            [_winloss_ratio(r.avg_win, r.avg_loss) for r in active]
        ),
        "median_max_drawdown": statistics.median([r.max_drawdown for r in active]),
        "total_trades": sum(r.trades for r in active),
        "consistency": pos / len(active),
    }


# -----------------------------
# Classification
# -----------------------------
def classify(agg: Dict[str, Any]) -> Dict[str, str]:
    """
    Returns {classification, label, reasoning} given aggregated OLD-strategy
    metrics. Advisory only — tune thresholds at the top of this file.
    """
    if agg["periods_with_trades"] == 0:
        return {
            "classification": "paper_only",
            "label": "no_signal",
            "reasoning": "no trades produced across any tested period",
        }

    ret = agg["median_total_return"]
    pf = agg["median_profit_factor"]
    cons = agg["consistency"]
    dd = agg["median_max_drawdown"]

    reason = (
        f"ret={ret*100:+.1f}% pf={pf:.2f} "
        f"win={agg['median_win_rate']*100:.0f}% "
        f"winloss={agg['median_winloss_ratio']:.2f} "
        f"dd={dd*100:.1f}% pos_periods={cons*100:.0f}%"
    )

    # Bad first — if it triggers any "remove" condition, classify as remove.
    if (
        ret < REMOVE_MAX_RETURN
        or pf < REMOVE_MAX_PF
        or cons < REMOVE_MAX_CONSISTENCY
    ):
        return {"classification": "remove", "label": "weak", "reasoning": reason}

    # Strong
    if (
        ret > KEEP_ACTIVE_MIN_RETURN
        and pf >= KEEP_ACTIVE_MIN_PF
        and cons >= KEEP_ACTIVE_MIN_CONSISTENCY
        and dd > KEEP_ACTIVE_MAX_DD
    ):
        return {"classification": "keep_active", "label": "strong", "reasoning": reason}

    # Marginal positive
    if (
        ret > REDUCE_MIN_RETURN
        and pf >= REDUCE_MIN_PF
        and cons >= REDUCE_MIN_CONSISTENCY
    ):
        return {
            "classification": "reduce_priority",
            "label": "marginal",
            "reasoning": reason,
        }

    # Otherwise mixed
    return {"classification": "paper_only", "label": "unstable", "reasoning": reason}


# -----------------------------
# Strategy execution helper
# -----------------------------
def run_strategies_on(
    sym: str,
    period: str,
    df: pd.DataFrame,
    spy_df: Optional[pd.DataFrame],
) -> List[PeriodResult]:
    """Run OLD and NEW strategies on a single (symbol, period) bar set."""
    out: List[PeriodResult] = []
    bars = len(df)
    pairs = [
        ("old", run_old_strategy(sym, df)),
        ("new", run_new_strategy(sym, df, spy_df)),
    ]
    for name, res in pairs:
        s = res.summary()
        out.append(
            PeriodResult(
                symbol=sym,
                period=period,
                strategy=name,
                trades=int(s["trades"]),
                win_rate=float(s["win_rate"]),
                avg_win=float(s["avg_win"]),
                avg_loss=float(s["avg_loss"]),
                win_loss_ratio=_winloss_ratio(float(s["avg_win"]), float(s["avg_loss"])),
                max_drawdown=float(s["max_drawdown"]),
                profit_factor=_safe_pf(float(s["profit_factor"])),
                total_return=float(s["total_return"]),
                bars=bars,
            )
        )
    return out


# -----------------------------
# CSV writers
# -----------------------------
def _fmt(v: float, kind: str) -> str:
    if kind == "pct":
        return f"{v*100:.4f}"
    if kind == "ratio":
        return f"{v:.4f}"
    return f"{v:.4f}"


def write_detail_csv(rows: List[PeriodResult], path: str) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow([
            "symbol", "period", "strategy", "trades",
            "win_rate", "avg_win", "avg_loss", "win_loss_ratio",
            "max_drawdown", "profit_factor", "total_return", "bars",
        ])
        for r in rows:
            w.writerow([
                r.symbol, r.period, r.strategy, r.trades,
                _fmt(r.win_rate, "pct"),
                _fmt(r.avg_win, "pct"),
                _fmt(r.avg_loss, "pct"),
                _fmt(r.win_loss_ratio, "ratio"),
                _fmt(r.max_drawdown, "pct"),
                _fmt(r.profit_factor, "ratio"),
                _fmt(r.total_return, "pct"),
                r.bars,
            ])


def write_ranking_csv(rows: List[Dict[str, Any]], path: str) -> None:
    if not rows:
        return
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        for r in rows:
            w.writerow(r)


# -----------------------------
# Console table
# -----------------------------
def print_ranking_table(rows: List[Dict[str, Any]]) -> None:
    if not rows:
        print("[analyze] no rankings to print")
        return
    header = (
        f"{'Symbol':<7} {'Class':<16} {'Label':<10} "
        f"{'OldRet':>8} {'OldPF':>6} {'Win':>5} {'W/L':>5} "
        f"{'Trades':>6} {'Cons':>5} {'NewVsOld':>10}"
    )
    print(header)
    print("-" * len(header))
    for r in rows:
        print(
            f"{r['symbol']:<7} "
            f"{r['classification']:<16} "
            f"{r['label']:<10} "
            f"{r['old_median_return']*100:+7.2f}% "
            f"{r['old_median_pf']:6.2f} "
            f"{r['old_median_winrate']*100:4.0f}% "
            f"{r['old_median_winloss']:5.2f} "
            f"{int(r['old_total_trades']):6} "
            f"{r['old_consistency']*100:4.0f}% "
            f"{r['new_vs_old']:>10}"
        )
        print(f"        why: {r['reasoning']}")


# -----------------------------
# Main
# -----------------------------
def main() -> None:
    p = argparse.ArgumentParser(
        description="Multi-period symbol ranking & classification (research only)"
    )
    p.add_argument("--symbols", default=",".join(DEFAULT_SYMBOLS),
                   help="Comma-separated symbol list. SPY is auto-added if missing.")
    p.add_argument("--periods", default=",".join(VALID_PERIODS),
                   help=f"Comma-separated periods from {VALID_PERIODS}.")
    p.add_argument("--source", choices=["public", "yfinance"], default="public")
    p.add_argument("--timeout", type=float, default=12.0,
                   help="Per-request HTTP timeout (Public source). Default 12s.")
    p.add_argument("--sleep", type=float, default=1.0,
                   help="Sleep between Public requests. Default 1.0. Ignored for yfinance.")
    p.add_argument("--out-detail", default="analyze_detail.csv")
    p.add_argument("--out-ranking", default="analyze_ranking.csv")
    args = p.parse_args()

    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    if "SPY" not in symbols:
        symbols = ["SPY"] + symbols

    periods = [p_.strip().upper() for p_ in args.periods.split(",") if p_.strip()]
    invalid = [p_ for p_ in periods if p_ not in VALID_PERIODS]
    if invalid:
        raise SystemExit(f"Invalid periods: {invalid}. Choose from {VALID_PERIODS}")

    print(
        f"[analyze] source={args.source} periods={periods} "
        f"timeout={args.timeout}s sleep={args.sleep}s "
        f"symbols={symbols}"
    )

    # ---- 1. Fetch all bars (sym, period) up front ---------------------------
    all_bars: Dict[str, Dict[str, pd.DataFrame]] = {sym: {} for sym in symbols}
    fetch_count = 0
    total_fetches = len(symbols) * len(periods)
    interrupted = False
    for sym in symbols:
        if interrupted:
            break
        for period in periods:
            fetch_count += 1
            print(
                f"[analyze] fetching {sym}/{period} {fetch_count}/{total_fetches} "
                f"(source={args.source})...",
                flush=True,
            )
            try:
                df = load_bars(sym, period, args.source, timeout=args.timeout)
            except KeyboardInterrupt:
                print(f"[analyze] interrupted while fetching {sym}/{period}; stopping early")
                interrupted = True
                break
            except Exception as e:
                print(f"[analyze] {sym}/{period}: FETCH_ERROR {type(e).__name__}: {e} — skipping")
                df = None

            if df is None or df.empty:
                print(f"[analyze] {sym}/{period}: no bars; skipping this slot")
            else:
                all_bars[sym][period] = df
                print(f"[analyze] {sym}/{period}: rows={len(df)}")

            if args.source == "public" and fetch_count < total_fetches and args.sleep > 0:
                time.sleep(args.sleep)

    # ---- 2. Run strategies for every (sym, period) we have bars for --------
    detail_rows: List[PeriodResult] = []
    for sym in symbols:
        for period in periods:
            df = all_bars[sym].get(period)
            if df is None or df.empty:
                continue
            spy_df = all_bars.get("SPY", {}).get(period)
            detail_rows.extend(run_strategies_on(sym, period, df, spy_df))

    # ---- 3. Aggregate per (symbol, strategy) and classify ------------------
    rankings: List[Dict[str, Any]] = []
    for sym in symbols:
        old_results = [r for r in detail_rows if r.symbol == sym and r.strategy == "old"]
        new_results = [r for r in detail_rows if r.symbol == sym and r.strategy == "new"]
        old_agg = aggregate(old_results)
        new_agg = aggregate(new_results)
        cls = classify(old_agg)

        # Compare new vs old on median return
        if old_agg["periods_with_trades"] and new_agg["periods_with_trades"]:
            d = new_agg["median_total_return"] - old_agg["median_total_return"]
            if abs(d) < 0.005:
                vs = "tie"
            elif d > 0:
                vs = "new_better"
            else:
                vs = "old_better"
        else:
            vs = "n/a"

        rankings.append({
            "symbol": sym,
            "classification": cls["classification"],
            "label": cls["label"],
            "reasoning": cls["reasoning"],
            "old_periods_with_trades": old_agg["periods_with_trades"],
            "old_total_trades": old_agg["total_trades"],
            "old_median_return": old_agg["median_total_return"],
            "old_median_pf": old_agg["median_profit_factor"],
            "old_median_winrate": old_agg["median_win_rate"],
            "old_median_winloss": old_agg["median_winloss_ratio"],
            "old_median_mdd": old_agg["median_max_drawdown"],
            "old_consistency": old_agg["consistency"],
            "new_periods_with_trades": new_agg["periods_with_trades"],
            "new_total_trades": new_agg["total_trades"],
            "new_median_return": new_agg["median_total_return"],
            "new_median_pf": new_agg["median_profit_factor"],
            "new_median_winrate": new_agg["median_win_rate"],
            "new_median_winloss": new_agg["median_winloss_ratio"],
            "new_median_mdd": new_agg["median_max_drawdown"],
            "new_consistency": new_agg["consistency"],
            "new_vs_old": vs,
        })

    # Sort: keep_active > reduce_priority > paper_only > remove,
    # then by old_median_return desc within each bucket.
    cls_order = {"keep_active": 0, "reduce_priority": 1, "paper_only": 2, "remove": 3}
    rankings.sort(key=lambda r: (cls_order.get(r["classification"], 9),
                                  -r["old_median_return"]))

    # ---- 4. Write CSVs ------------------------------------------------------
    if detail_rows:
        write_detail_csv(detail_rows, args.out_detail)
        print(f"[analyze] wrote {args.out_detail}")
    if rankings:
        write_ranking_csv(rankings, args.out_ranking)
        print(f"[analyze] wrote {args.out_ranking}")

    # ---- 5. Console summary -------------------------------------------------
    print()
    print_ranking_table(rankings)

    # Bucket counts
    counts: Dict[str, int] = {}
    for r in rankings:
        counts[r["classification"]] = counts.get(r["classification"], 0) + 1
    print()
    print("[analyze] bucket counts:", counts)
    print(
        "[analyze] reminder: this is RESEARCH ONLY. "
        "Live bot, order placement, and SCAN_SYMBOLS were not modified."
    )


if __name__ == "__main__":
    main()
