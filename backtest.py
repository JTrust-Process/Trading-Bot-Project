import argparse
from dataclasses import dataclass
from datetime import datetime, date
from typing import Optional, Dict, Any, List

import numpy as np
import pandas as pd
import yfinance as yf
import matplotlib.pyplot as plt


# -----------------------------
# Data helpers
# -----------------------------
def _normalize_ohlc_columns(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        raise RuntimeError("No data returned (empty DataFrame).")

    cols = df.columns
    if isinstance(cols, pd.MultiIndex):
        df = df.copy()
        df.columns = [str(c[0]).strip() for c in cols.to_list()]
    elif len(cols) > 0 and isinstance(cols[0], tuple):
        df = df.copy()
        df.columns = [str(c[0]).strip() for c in cols]

    df = df.copy()
    df.columns = [str(c).strip() for c in df.columns]

    rename = {}
    for c in df.columns:
        lc = c.lower().replace("_", " ").strip()
        if lc == "open":
            rename[c] = "Open"
        elif lc == "high":
            rename[c] = "High"
        elif lc == "low":
            rename[c] = "Low"
        elif lc == "close":
            rename[c] = "Close"
        elif lc in ("adj close", "adjusted close", "adjclose"):
            rename[c] = "Adj Close"
        elif lc == "volume":
            rename[c] = "Volume"

    df = df.rename(columns=rename)

    needed = ["Open", "High", "Low", "Close"]
    missing = [c for c in needed if c not in df.columns]
    if missing:
        raise RuntimeError(f"Missing OHLC columns {missing}. Got columns: {list(df.columns)}")

    return df[needed].dropna().copy()


def fetch_ohlc(symbol: str, start: str, end: str, debug: bool = False) -> pd.DataFrame:
    t = yf.Ticker(symbol)
    df = t.history(start=start, end=end, auto_adjust=False)
    df = _normalize_ohlc_columns(df)

    if debug:
        print(
            f"[DEBUG] {symbol} rows={len(df)} cols={list(df.columns)} "
            f"first_date={df.index.min().date()} last_date={df.index.max().date()}"
        )
    return df


def fetch_close_prices(symbols: List[str], start: str, end: str, debug: bool = False) -> pd.DataFrame:
    df = yf.download(
        symbols,
        start=start,
        end=end,
        auto_adjust=False,
        progress=False,
        group_by="column",
        threads=True,
    )

    if df is None or df.empty:
        raise RuntimeError(f"No data returned for {symbols}")

    if isinstance(df.columns, pd.MultiIndex):
        if "Close" not in df.columns.get_level_values(0):
            raise RuntimeError(f"Missing Close in downloaded data. Got: {df.columns.levels[0].tolist()}")
        close = df["Close"].copy()
    else:
        if "Close" not in df.columns:
            raise RuntimeError(f"Missing Close column. Got columns: {list(df.columns)}")
        close = df[["Close"]].copy()
        close.columns = [symbols[0]]

    close.columns = [str(c).upper().strip() for c in close.columns]
    close = close.dropna(how="all")

    missing = [s for s in [x.upper() for x in symbols] if s not in close.columns]
    if missing:
        raise RuntimeError(f"Missing Close series for: {missing}. Got columns: {list(close.columns)}")

    close = close[[s.upper() for s in symbols]].dropna()

    if debug:
        print(
            f"[DEBUG] close prices rows={len(close)} cols={list(close.columns)} "
            f"first_date={close.index.min().date()} last_date={close.index.max().date()}"
        )
    return close


# -----------------------------
# Metrics
# -----------------------------
def max_drawdown(equity: pd.Series) -> float:
    peak = equity.cummax()
    dd = (equity / peak) - 1.0
    return float(dd.min())


def sharpe_ratio(daily_returns: pd.Series, risk_free_daily: float = 0.0) -> float:
    r = daily_returns.dropna() - risk_free_daily
    if len(r) < 2 or r.std(ddof=1) == 0:
        return 0.0
    return float(np.sqrt(252) * (r.mean() / r.std(ddof=1)))


# -----------------------------
# Glide path helpers
# -----------------------------
def _parse_date(s: str) -> date:
    return datetime.strptime(s.strip(), "%Y-%m-%d").date()


def days_until_withdraw(withdraw_date: Optional[date], current_date: date) -> Optional[int]:
    if withdraw_date is None:
        return None
    return int((withdraw_date - current_date).days)


def _max_exposure(days_left: Optional[int], early: float, mid: float, late: float, lockdown_days: int) -> float:
    """
    Simple 3-tier glide path:
      - None days_left -> early
      - <= lockdown_days -> 0
      - <= 30 -> late
      - <= 60 -> mid
      - else -> early
    """
    if days_left is None:
        return float(early)
    if days_left <= lockdown_days:
        return 0.0
    if days_left <= 30:
        return float(late)
    if days_left <= 60:
        return float(mid)
    return float(early)


# -----------------------------
# Strategy: TP/SL (single symbol)
# -----------------------------
@dataclass
class BacktestResult:
    equity: pd.Series
    trades: pd.DataFrame
    win_rate: float
    sharpe: float
    mdd: float


def backtest_tp_sl(
    ohlc: pd.DataFrame,
    tp: float,
    sl: float,
    start_cash: float = 10_000.0,
    *,
    withdraw_date: Optional[date] = None,
    lockdown_days: int = 14,
    max_exposure_early: float = 0.30,
    max_exposure_mid: float = 0.20,
    max_exposure_late: float = 0.10,
) -> BacktestResult:
    idx = ohlc.index
    o = ohlc["Open"].values
    h = ohlc["High"].values
    l = ohlc["Low"].values
    c = ohlc["Close"].values

    cash = float(start_cash)
    shares = 0.0
    entry_price: Optional[float] = None
    entry_i: Optional[int] = None

    equity: List[float] = []
    trade_rows: List[Dict[str, Any]] = []

    for i in range(len(idx)):
        cur_date = idx[i].date()
        mtm = cash + shares * c[i]
        equity.append(mtm)

        days_left = days_until_withdraw(withdraw_date, cur_date)
        cap = _max_exposure(
            days_left,
            early=max_exposure_early,
            mid=max_exposure_mid,
            late=max_exposure_late,
            lockdown_days=lockdown_days,
        )

        in_lockdown = (days_left is not None and days_left <= lockdown_days)

        if in_lockdown:
            if shares > 0:
                exit_price = float(c[i])
                cost_basis = float(entry_price if entry_price is not None else exit_price)
                cash = shares * exit_price
                pnl = cash - (shares * cost_basis)

                trade_rows.append({
                    "entry_date": idx[entry_i] if entry_i is not None else idx[i],
                    "exit_date": idx[i],
                    "entry_price": cost_basis,
                    "exit_price": exit_price,
                    "reason": "LOCKDOWN",
                    "pnl": float(pnl),
                    "return": float((exit_price / cost_basis) - 1.0) if cost_basis > 0 else 0.0,
                    "bars_held": int(i - entry_i) if entry_i is not None else 0,
                })

                shares = 0.0
                entry_price = None
                entry_i = None
            continue

        # enter
        if shares == 0.0:
            ep = float(o[i])
            if ep <= 0:
                continue

            target_value = max(0.0, min(cap * mtm, cash))
            if target_value < 1.0:
                continue

            shares = target_value / ep
            cash -= target_value
            entry_price = ep
            entry_i = i
            continue

        # manage position
        assert entry_price is not None and entry_i is not None
        tp_price = entry_price * (1.0 + tp)
        sl_price = entry_price * (1.0 - sl)

        hit_tp = h[i] >= tp_price
        hit_sl = l[i] <= sl_price

        if hit_tp or hit_sl:
            # conservative: if both hit, assume SL first
            if hit_sl:
                exit_price = float(sl_price)
                reason = "SL"
            else:
                exit_price = float(tp_price)
                reason = "TP"

            cash += shares * exit_price
            pnl = (shares * exit_price) - (shares * entry_price)

            trade_rows.append({
                "entry_date": idx[entry_i],
                "exit_date": idx[i],
                "entry_price": float(entry_price),
                "exit_price": float(exit_price),
                "reason": reason,
                "pnl": float(pnl),
                "return": float((exit_price / entry_price) - 1.0),
                "bars_held": int(i - entry_i),
            })

            shares = 0.0
            entry_price = None
            entry_i = None
            continue

        # trim at close if above cap
        if cap > 0:
            mtm = cash + shares * c[i]
            pos_value = shares * c[i]
            exposure = (pos_value / mtm) if mtm > 0 else 0.0

            if exposure > cap and c[i] > 0:
                target_pos_value = cap * mtm
                excess_value = max(0.0, pos_value - target_pos_value)
                sell_qty = min(shares, excess_value / c[i])
                if sell_qty > 0:
                    cash += sell_qty * c[i]
                    shares -= sell_qty

    equity_s = pd.Series(equity, index=idx, name="equity")
    rets = equity_s.pct_change().fillna(0.0)

    trades = pd.DataFrame(trade_rows)
    win_rate = float((trades["pnl"] > 0).mean()) if len(trades) else 0.0

    return BacktestResult(
        equity=equity_s,
        trades=trades,
        win_rate=win_rate,
        sharpe=sharpe_ratio(rets),
        mdd=max_drawdown(equity_s),
    )


# -----------------------------
# Strategy: cash_safe_spy (single symbol, SPY only)
# -----------------------------
def backtest_cash_safe_spy(
    ohlc: pd.DataFrame,
    *,
    tp: float,
    sl: float,
    start_cash: float,
    withdraw_date: Optional[date],
    lockdown_days: int,
    max_exposure_early: float,
    max_exposure_mid: float,
    max_exposure_late: float,
    max_drawdown_limit: float,
    daily_loss_limit: float,
    max_order_amount_usd: float,
) -> Dict[str, Any]:
    idx = ohlc.index
    o = ohlc["Open"].values
    h = ohlc["High"].values
    l = ohlc["Low"].values
    c = ohlc["Close"].values

    cash = float(start_cash)
    shares = 0.0
    avg_cost = 0.0

    equity_curve: List[float] = []
    trades: List[Dict[str, Any]] = []

    equity_peak = float(start_cash)
    halt_next_day = False

    for i in range(len(idx)):
        dt = idx[i].date()
        day_start_equity = cash + shares * o[i]

        if halt_next_day:
            end_equity = cash + shares * c[i]
            equity_curve.append(end_equity)
            halt_next_day = False
            continue

        days_left = (withdraw_date - dt).days if withdraw_date is not None else None
        max_exp = _max_exposure(days_left, max_exposure_early, max_exposure_mid, max_exposure_late, lockdown_days)

        # lockdown => liquidate at open
        if max_exp <= 0.0 and shares > 0:
            exit_price = float(o[i])
            pnl = shares * (exit_price - avg_cost)
            trades.append({"date": dt, "action": "SELL", "reason": "LOCKDOWN", "price": exit_price, "shares": float(shares), "pnl": float(pnl)})
            cash += shares * exit_price
            shares = 0.0
            avg_cost = 0.0

        # TP/SL intraday (SL first)
        if shares > 0:
            tp_price = avg_cost * (1.0 + tp)
            sl_price = avg_cost * (1.0 - sl)

            hit_tp = h[i] >= tp_price
            hit_sl = l[i] <= sl_price

            if hit_tp or hit_sl:
                if hit_sl:
                    exit_price = float(sl_price)
                    reason = "SL"
                else:
                    exit_price = float(tp_price)
                    reason = "TP"

                pnl = shares * (exit_price - avg_cost)
                trades.append({"date": dt, "action": "SELL", "reason": reason, "price": exit_price, "shares": float(shares), "pnl": float(pnl)})
                cash += shares * exit_price
                shares = 0.0
                avg_cost = 0.0

        # trim at open if above cap
        if shares > 0 and max_exp > 0:
            total_equity_open = cash + shares * o[i]
            pos_value_open = shares * o[i]
            exp_open = (pos_value_open / total_equity_open) if total_equity_open > 0 else 0.0
            if exp_open > max_exp and o[i] > 0:
                target_value = max_exp * total_equity_open
                excess_value = max(0.0, pos_value_open - target_value)
                if excess_value > 1.0:
                    sell_qty = min(shares, excess_value / o[i])
                    if sell_qty > 0:
                        exit_price = float(o[i])
                        pnl = sell_qty * (exit_price - avg_cost)
                        trades.append({"date": dt, "action": "SELL", "reason": "TRIM", "price": exit_price, "shares": float(sell_qty), "pnl": float(pnl)})
                        cash += sell_qty * exit_price
                        shares -= sell_qty

        # enter at open
        if shares == 0 and max_exp > 0:
            target_value = max_exp * cash
            amount = min(float(max_order_amount_usd), cash, target_value)
            if amount >= 1.0 and o[i] > 0:
                buy_price = float(o[i])
                buy_shares = amount / buy_price
                shares = buy_shares
                avg_cost = buy_price
                cash -= amount
                trades.append({"date": dt, "action": "BUY", "reason": "TARGET_EXPOSURE", "price": buy_price, "shares": float(buy_shares), "pnl": 0.0})

        end_equity = cash + shares * c[i]
        equity_curve.append(float(end_equity))

        equity_peak = max(equity_peak, float(end_equity))
        dd = (1.0 - end_equity / equity_peak) if equity_peak > 0 else 0.0
        daily_ret = (end_equity / day_start_equity - 1.0) if day_start_equity > 0 else 0.0

        hit_daily_loss = (daily_loss_limit > 0 and daily_ret <= -daily_loss_limit)
        hit_dd = (max_drawdown_limit > 0 and dd >= max_drawdown_limit)

        if (hit_daily_loss or hit_dd) and shares > 0:
            exit_price = float(c[i])
            pnl = shares * (exit_price - avg_cost)
            trades.append({"date": dt, "action": "SELL", "reason": "DAILY_LOSS_LIMIT" if hit_daily_loss else "MAX_DRAWDOWN", "price": exit_price, "shares": float(shares), "pnl": float(pnl)})
            cash += shares * exit_price
            shares = 0.0
            avg_cost = 0.0
            equity_curve[-1] = float(cash)
            halt_next_day = True

    equity_s = pd.Series(equity_curve, index=idx, name="equity")
    rets = equity_s.pct_change().fillna(0.0)

    trades_df = pd.DataFrame(trades)
    if len(trades_df):
        sells = trades_df[trades_df["action"] == "SELL"]
        win_rate = float((sells["pnl"] > 0).mean()) if len(sells) else 0.0
    else:
        win_rate = 0.0

    return {
        "equity": equity_s,
        "trades": trades_df,
        "win_rate": win_rate,
        "sharpe": sharpe_ratio(rets),
        "mdd": max_drawdown(equity_s),
        "final_equity": float(equity_s.iloc[-1]) if len(equity_s) else float(start_cash),
    }


# -----------------------------
# Rotator strategy
# -----------------------------
def annualized_vol(daily_returns: pd.DataFrame, window: int) -> pd.DataFrame:
    return daily_returns.rolling(window).std(ddof=1) * np.sqrt(252)


def clamp(x: float, lo: float, hi: float) -> float:
    return float(max(lo, min(hi, x)))


def backtest_rotator(
    close: pd.DataFrame,
    lookback: int,
    start_cash: float = 10_000.0,
    *,
    fee_bps: float = 0.0,
    slippage_bps: float = 0.0,
    must_beat_spy: bool = True,
    vol_target_annual: float = 0.12,
    vol_window: int = 20,
    max_weight: float = 1.0,
) -> Dict[str, Any]:
    if lookback < 2:
        raise ValueError("lookback must be >= 2")

    close = close.copy()
    close.columns = [str(c).upper().strip() for c in close.columns]

    if must_beat_spy and "SPY" not in close.columns:
        raise ValueError("must_beat_spy=True requires SPY in your symbols universe.")

    daily_ret = close.pct_change()
    mom = (close / close.shift(lookback) - 1.0).dropna()
    daily_ret = daily_ret.loc[mom.index].fillna(0.0)
    vol = annualized_vol(daily_ret, vol_window)

    winner = mom.idxmax(axis=1)

    if must_beat_spy:
        spy_mom = mom["SPY"]
        win_mom = pd.Series([mom.loc[dt, winner.loc[dt]] for dt in mom.index], index=mom.index, name="winner_mom")
        winner = winner.where(win_mom > spy_mom, other="CASH")

    holding = winner.shift(1).dropna()
    daily_ret = daily_ret.loc[holding.index]
    vol = vol.loc[holding.index]

    weights = []
    for dt in holding.index:
        sym = holding.loc[dt]
        if sym == "CASH":
            weights.append(0.0)
            continue
        v = vol.loc[dt, sym]
        if pd.isna(v) or v <= 0:
            weights.append(0.0)
            continue
        w = vol_target_annual / float(v)
        weights.append(clamp(w, 0.0, max_weight))

    weight_s = pd.Series(weights, index=holding.index, name="weight")

    strat_ret = []
    for dt in holding.index:
        sym = holding.loc[dt]
        w = float(weight_s.loc[dt])
        if sym == "CASH" or w <= 0.0:
            strat_ret.append(0.0)
        else:
            strat_ret.append(w * float(daily_ret.loc[dt, sym]))

    strat_ret = pd.Series(strat_ret, index=holding.index, name="strategy_return")

    total_bps = float(fee_bps) + float(slippage_bps)
    if total_bps > 0:
        prev_h = holding.shift(1)
        prev_w = weight_s.shift(1).fillna(0.0)
        symbol_changed = (holding != prev_h) & prev_h.notna()
        turnover = (weight_s - prev_w).abs()
        turnover = turnover.where(~symbol_changed, other=(prev_w.abs() + weight_s.abs()))
        cost_ret = turnover * (total_bps / 10000.0)
        strat_ret = strat_ret - cost_ret.fillna(0.0)

    equity = (1.0 + strat_ret).cumprod() * start_cash
    equity.name = "equity"

    switches = int((holding != holding.shift(1)).sum() - 1) if len(holding) > 1 else 0
    switches = max(0, switches)

    sr = sharpe_ratio(equity.pct_change().fillna(0.0))
    mdd = max_drawdown(equity)
    win_rate = float((strat_ret > 0).mean()) if len(strat_ret) else 0.0

    return {
        "equity": equity,
        "holdings": holding.rename("holding"),
        "weights": weight_s,
        "strategy_return": strat_ret,
        "sharpe": sr,
        "mdd": mdd,
        "switches": switches,
        "win_rate": win_rate,
    }


# -----------------------------
# Plot
# -----------------------------
def plot_equity(equity: pd.Series, title: str) -> None:
    plt.figure()
    plt.plot(equity.index, equity.values)
    plt.title(title)
    plt.xlabel("Date")
    plt.ylabel("Equity ($)")
    plt.xticks(rotation=30, ha="right")
    plt.tight_layout()
    plt.show()


# -----------------------------
# CLI
# -----------------------------
def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--mode", choices=["tp_sl", "rotator", "cash_safe_spy"], default="tp_sl")

    # common
    p.add_argument("--start", required=True, help="YYYY-MM-DD")
    p.add_argument("--end", default=None, help="YYYY-MM-DD (default: today UTC)")
    p.add_argument("--cash", type=float, default=10000.0)
    p.add_argument("--debug", action="store_true")
    p.add_argument("--plot", action="store_true")

    # TP/SL + cash_safe_spy
    p.add_argument("--symbol", default="SPY")
    p.add_argument("--tp", type=float, default=0.01)
    p.add_argument("--sl", type=float, default=0.007)

    p.add_argument("--withdraw_date", default=None, help="YYYY-MM-DD (e.g., 2026-05-10)")
    p.add_argument("--lockdown_days", type=int, default=14)
    p.add_argument("--max_exposure_early", type=float, default=0.30)
    p.add_argument("--max_exposure_mid", type=float, default=0.20)
    p.add_argument("--max_exposure_late", type=float, default=0.10)

    # cash_safe_spy extras
    p.add_argument("--max_drawdown", type=float, default=0.015)
    p.add_argument("--daily_loss_limit", type=float, default=0.005)
    p.add_argument("--max_order", type=float, default=10.0)

    # rotator
    p.add_argument("--lookback", type=int, default=30)
    p.add_argument("--symbols", default="SPY,QQQ,VTI,IWM")
    p.add_argument("--fee_bps", type=float, default=1.0)
    p.add_argument("--slip_bps", type=float, default=2.0)
    p.add_argument("--no_must_beat_spy", action="store_true")
    p.add_argument("--vol_target", type=float, default=0.12)
    p.add_argument("--vol_window", type=int, default=20)
    p.add_argument("--max_weight", type=float, default=1.0)

    args = p.parse_args()
    end = args.end or pd.Timestamp.utcnow().strftime("%Y-%m-%d")

    mode = args.mode

    if mode == "tp_sl":
        sym = args.symbol.upper().strip()
        ohlc = fetch_ohlc(sym, args.start, end, debug=args.debug)
        wd = _parse_date(args.withdraw_date) if args.withdraw_date else None

        res = backtest_tp_sl(
            ohlc,
            tp=args.tp,
            sl=args.sl,
            start_cash=args.cash,
            withdraw_date=wd,
            lockdown_days=args.lockdown_days,
            max_exposure_early=args.max_exposure_early,
            max_exposure_mid=args.max_exposure_mid,
            max_exposure_late=args.max_exposure_late,
        )

        print(f"Mode: tp_sl | Symbol: {sym}")
        print(f"Period: {ohlc.index.min().date()} → {ohlc.index.max().date()} | bars={len(ohlc)}")
        print(f"Withdraw date: {wd} | lockdown_days={args.lockdown_days}")
        print(f"Exposure caps: early={args.max_exposure_early:.0%} mid={args.max_exposure_mid:.0%} late={args.max_exposure_late:.0%}")
        print(f"TP={args.tp:.2%} SL={args.sl:.2%}")
        print(f"Trades: {len(res.trades)} | Win rate: {res.win_rate:.2%}")
        print(f"Sharpe: {res.sharpe:.2f} | Max drawdown: {res.mdd:.2%} | Final equity: ${res.equity.iloc[-1]:,.2f}")

        res.equity.to_frame("equity").to_csv("equity.csv")
        res.trades.to_csv("trades.csv", index=False)

        if args.plot:
            plot_equity(res.equity, f"{sym} tp_sl + glide path")
        return

    if mode == "cash_safe_spy":
        sym = args.symbol.upper().strip()
        if sym != "SPY":
            raise SystemExit("cash_safe_spy mode is designed for SPY. Use --symbol SPY")

        ohlc = fetch_ohlc("SPY", args.start, end, debug=args.debug)
        wd = _parse_date(args.withdraw_date) if args.withdraw_date else None

        res = backtest_cash_safe_spy(
            ohlc,
            tp=args.tp,
            sl=args.sl,
            start_cash=args.cash,
            withdraw_date=wd,
            lockdown_days=args.lockdown_days,
            max_exposure_early=args.max_exposure_early,
            max_exposure_mid=args.max_exposure_mid,
            max_exposure_late=args.max_exposure_late,
            max_drawdown_limit=args.max_drawdown,
            daily_loss_limit=args.daily_loss_limit,
            max_order_amount_usd=args.max_order,
        )

        print("Mode: cash_safe_spy | Symbol: SPY")
        print(f"Period: {ohlc.index.min().date()} → {ohlc.index.max().date()} | bars={len(ohlc)}")
        print(f"Withdraw date: {wd} | lockdown_days={args.lockdown_days}")
        print(f"Exposure caps: early={args.max_exposure_early:.0%} mid={args.max_exposure_mid:.0%} late={args.max_exposure_late:.0%}")
        print(f"Kill switches: max_dd={args.max_drawdown:.2%} daily_loss={args.daily_loss_limit:.2%} | Order cap=${args.max_order:.2f}")
        print(f"TP={args.tp:.2%} SL={args.sl:.2%}")
        print(f"Trades: {len(res['trades'])} | Win rate (sells): {res['win_rate']:.2%}")
        print(f"Sharpe: {res['sharpe']:.2f} | Max drawdown: {res['mdd']:.2%} | Final equity: ${res['final_equity']:,.2f}")

        res["equity"].to_frame("equity").to_csv("equity.csv")
        res["trades"].to_csv("trades.csv", index=False)

        if args.plot:
            plot_equity(res["equity"], "cash_safe_spy equity")
        return

    # rotator
    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    if len(symbols) < 2:
        raise SystemExit("rotator mode needs at least 2 symbols")

    prices = fetch_close_prices(symbols, args.start, end, debug=args.debug)
    res = backtest_rotator(
        prices,
        lookback=args.lookback,
        start_cash=args.cash,
        fee_bps=args.fee_bps,
        slippage_bps=args.slip_bps,
        must_beat_spy=(not args.no_must_beat_spy),
        vol_target_annual=args.vol_target,
        vol_window=args.vol_window,
        max_weight=args.max_weight,
    )

    print(f"Mode: rotator | Universe: {symbols}")
    print(f"Lookback: {args.lookback} | Must-beat-SPY: {not args.no_must_beat_spy}")
    print(f"Costs: fee={args.fee_bps:.2f} bps slip={args.slip_bps:.2f} bps | Vol target={args.vol_target:.2%}")
    print(f"Period: {prices.index.min().date()} → {prices.index.max().date()} | bars={len(prices)}")
    print(f"Switches: {res['switches']} | Win rate (day-level): {res['win_rate']:.2%}")
    print(f"Sharpe: {res['sharpe']:.2f} | Max drawdown: {res['mdd']:.2%} | Final equity: ${res['equity'].iloc[-1]:,.2f}")

    res["holdings"].to_csv("holdings.csv")
    res["weights"].to_csv("weights.csv")
    res["equity"].to_frame("equity").to_csv("equity.csv")
    res["strategy_return"].to_csv("strategy_return.csv")

    if args.plot:
        plot_equity(res["equity"], f"Rotator ({args.lookback}d momentum)")


if __name__ == "__main__":
    main()
