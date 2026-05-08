"""
momentum.py — Momentum-rotation signal engine.

Scores symbols using weighted short-term returns:
    score = (1d * 0.3) + (5d * 0.4) + (10d * 0.3)

Returns a ranked list so bot.py can buy the top N
and sell anything that drops out of the top tier.
"""

from __future__ import annotations

import os
from typing import List, Optional
from dataclasses import dataclass

import pandas as pd

from market_data import get_daily_bars


# ── Weights (can be overridden via env) ───────────────────────────────────────
W1  = float(os.getenv("MOMENTUM_W1",  "0.3"))   # 1-day return weight
W5  = float(os.getenv("MOMENTUM_W5",  "0.4"))   # 5-day return weight
W10 = float(os.getenv("MOMENTUM_W10", "0.3"))   # 10-day return weight


@dataclass
class MomentumScore:
    symbol:   str
    score:    float   # composite momentum score
    rank:     int     # 1 = strongest
    ret_1d:   Optional[float] = None
    ret_5d:   Optional[float] = None
    ret_10d:  Optional[float] = None
    valid:    bool = True   # False if data was unavailable


def _safe_return(closes: pd.Series, lookback: int) -> Optional[float]:
    """Compute simple return over `lookback` bars. Returns None if not enough data."""
    if len(closes) < lookback + 1:
        return None
    try:
        end   = float(closes.iloc[-1])
        start = float(closes.iloc[-(lookback + 1)])
        if start <= 0:
            return None
        return (end - start) / start
    except Exception:
        return None


def score_symbol(symbol: str, bars: int = 20) -> MomentumScore:
    """Fetch daily bars and compute a momentum score for one symbol."""
    df = get_daily_bars(symbol, bars)
    if df is None or len(df) < 11:
        return MomentumScore(symbol=symbol, score=-999.0, rank=999, valid=False)

    closes = df["close"]
    r1  = _safe_return(closes, 1)
    r5  = _safe_return(closes, 5)
    r10 = _safe_return(closes, 10)

    # If any component is missing, mark invalid but score what we have
    parts = [
        (r1,  W1),
        (r5,  W5),
        (r10, W10),
    ]
    total_weight = sum(w for r, w in parts if r is not None)
    if total_weight == 0:
        return MomentumScore(symbol=symbol, score=-999.0, rank=999, valid=False)

    score = sum(r * w for r, w in parts if r is not None) / total_weight

    return MomentumScore(
        symbol=symbol,
        score=round(score, 6),
        rank=0,           # assigned after full ranking
        ret_1d=r1,
        ret_5d=r5,
        ret_10d=r10,
        valid=True,
    )


def rank_symbols(symbols: List[str]) -> List[MomentumScore]:
    """
    Score and rank a list of symbols.
    Returns list sorted strongest → weakest with rank assigned (1 = best).
    Invalid symbols are appended at the end.
    """
    scores = [score_symbol(s) for s in symbols]

    valid   = sorted([s for s in scores if s.valid],  key=lambda x: x.score, reverse=True)
    invalid = [s for s in scores if not s.valid]

    for i, ms in enumerate(valid, start=1):
        ms.rank = i
    for i, ms in enumerate(invalid, start=len(valid) + 1):
        ms.rank = i

    return valid + invalid


def get_buy_candidates(
    symbols: List[str],
    top_n: int = 3,
) -> List[MomentumScore]:
    """
    Return the top_n momentum leaders with score > 0.
    These are candidates for new buys.
    """
    ranked = rank_symbols(symbols)
    return [ms for ms in ranked[:top_n] if ms.valid and ms.score > 0]


def should_sell_momentum(
    symbol: str,
    all_scores: List[MomentumScore],
    sell_rank_threshold: int,
) -> bool:
    """
    Returns True if a held symbol should be sold on momentum grounds:
      - Its score has gone negative, OR
      - Its rank has dropped below sell_rank_threshold

    IMPORTANT: when the score is invalid (data fetch failed for this cycle),
    return False — i.e. take no action. A transient Polygon outage should
    NOT trigger a fire-sale of the entire momentum book.
    """
    for ms in all_scores:
        if ms.symbol == symbol:
            # Don't act on bad data. Caller's other exit paths (TP/SL/drawdown)
            # remain in effect via the live portfolio gainPercentage value.
            if not ms.valid:
                return False
            if ms.score <= 0:
                return True
            if ms.rank > sell_rank_threshold:
                return True
            return False
    # Symbol not in the scored universe (e.g. removed from MOMENTUM_SYMBOLS) —
    # don't initiate a sell here; let TP/SL/drawdown decide.
    return False