import argparse
from dataclasses import dataclass

import numpy as np
import pandas as pd
import yfinance as yf
import matplotlib.pyplot as plt


# -----------------------------
# Data: robust OHLC loader
# -----------------------------
def _normalize_ohlc_columns(df: pd.DataFrame) -> pd.DataFrame:
    """
    yfinance can return:
      - columns: Open/High/Low/Close/Adj Close/Volume
      - MultiIndex columns: ('Open','SPY') etc
      - tuple columns: ('Open','Spy')
    This function returns a DataFrame with columns: Open, High, Low, Close
    """
    if df is None or df.empty:
        raise RuntimeError("No data returned (empty DataFrame).")

    cols = df.columns

    # MultiIndex -> keep level 0
    if isinstance(cols, pd.MultiIndex):
        df = df.copy()
        df.columns = [str(c[0]).strip() for c in cols.to_list()]

    # tuple columns -> keep tuple[0]
    elif len(cols) > 0 and isinstance(cols[0], tuple):
        df = df.copy()
        df.columns = [str(c[0]).strip() for c in cols]

    # Normalize casing/spaces
    df = df.copy()
    df.columns = [str(c).strip() for c in df.columns]

    # Map common variants to standard
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

    out = df[needed].dropna().copy()
    return out


def fetch_ohlc(symbol: str, start: str, end: str, debug: bool = False) -> pd.DataFrame:
    # Use Ticker().history() instead of download() to avoid some grouping edge cases
    t = yf.Ticker(symbol)
    df = t.history(start=start, end=end, auto_adjust=False)

    # yfinance history() sometimes returns lowercase or weird columns too
    df = _normalize_ohlc_columns(df)

    if debug:
        print(f"[DEBUG] {symbol} rows={len(df)} cols={list(df.columns)} "
              f"first_date={df.index.min().date()} last_date={df.index.max().date()}")

    return df


# -----------------------------
# Metrics
# -----------------------------
def max_drawdown(equity: pd.Series) -> float:
    peak = equity.cummax()
    dd = (equity / peak) - 1.0
    return float(dd.min())


def sharpe_ratio(daily_returns: pd.Series, risk_free_daily: float = 0.0) -> float:
    r = daily_returns.dropna() - risk_free_daily
    if r.std(ddof=1) == 0 or len(r) < 2:
        return 0.0
    return float(np.sqrt(252) * (r.mean() / r.std(ddof=1)))


# -----------------------------
# Strategy: TP/SL on next-day entries (simple, consistent)
# - Enter at next open when flat
# - Exit when either:
#    * intraday hits TP based on entry price (use High)
#    * intraday hits SL (use Low)
#   If both hit same day, assume SL first (conservative)
# - If neither hit, hold
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
) -> BacktestResult:
    idx = ohlc.index
    o = ohlc["Open"].values
    h = ohlc["High"].values
    l = ohlc["Low"].values
    c = ohlc["Close"].values

    cash = start_cash
    shares = 0.0
    entry_price = None
    entry_i = None

    equity = []
    trade_rows = []

    # we’ll enter at open when flat; keep it deterministic
    for i in range(len(idx)):
        # mark-to-market at close
        mtm = cash + shares * c[i]
        equity.append(mtm)

        if shares == 0.0:
            # enter at OPEN today
            entry_price = o[i]
            if entry_price <= 0:
                continue
            shares = cash / entry_price
            cash = 0.0
            entry_i = i
            continue

        # If in position, check TP/SL intraday relative to entry_price
        assert entry_price is not None and entry_i is not None

        tp_price = entry_price * (1.0 + tp)
        sl_price = entry_price * (1.0 - sl)

        hit_tp = h[i] >= tp_price
        hit_sl = l[i] <= sl_price

        if hit_tp or hit_sl:
            # conservative: if both hit, assume SL filled first
            if hit_sl:
                exit_price = sl_price
                reason = "SL"
            else:
                exit_price = tp_price
                reason = "TP"

            cash = shares * exit_price
            pnl = cash - (shares * entry_price)

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

    equity_s = pd.Series(equity, index=idx, name="equity")
    rets = equity_s.pct_change().fillna(0.0)

    trades = pd.DataFrame(trade_rows)
    if len(trades) == 0:
        win_rate = 0.0
    else:
        win_rate = float((trades["pnl"] > 0).mean())

    sr = sharpe_ratio(rets)
    mdd = max_drawdown(equity_s)

    return BacktestResult(
        equity=equity_s,
        trades=trades,
        win_rate=win_rate,
        sharpe=sr,
        mdd=mdd,
    )


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
    
    
def fetch_close_prices(symbols: list, start: str, end: str, debug: bool = False) -> pd.DataFrame:
    """
    Returns a DataFrame of Close prices with columns = symbols, index = dates.
    Uses yf.download for multi-symbol but normalizes the returned shape.
    """
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

    # If multi-symbol, yfinance returns MultiIndex columns like ('Close','SPY')
    # We want just Close for each symbol.
    if isinstance(df.columns, pd.MultiIndex):
        # pick Close level
        if "Close" not in df.columns.get_level_values(0):
            raise RuntimeError(f"Missing Close in downloaded data. Got: {df.columns.levels[0].tolist()}")
        close = df["Close"].copy()
    else:
        # Single symbol: expects Close column
        if "Close" not in df.columns:
            raise RuntimeError(f"Missing Close column. Got columns: {list(df.columns)}")
        close = df[["Close"]].copy()
        close.columns = [symbols[0]]

    # Normalize column names
    close.columns = [str(c).upper().strip() for c in close.columns]
    close = close.dropna(how="all")

    # Ensure all requested symbols exist
    missing = [s for s in [x.upper() for x in symbols] if s not in close.columns]
    if missing:
        raise RuntimeError(f"Missing Close series for: {missing}. Got columns: {list(close.columns)}")

    # Drop rows where any symbol is NaN (align trading calendar)
    close = close[[s.upper() for s in symbols]].dropna()

    if debug:
        print(f"[DEBUG] close prices rows={len(close)} cols={list(close.columns)} "
              f"first_date={close.index.min().date()} last_date={close.index.max().date()}")

    return close

def annualized_vol(daily_returns: pd.DataFrame, window: int) -> pd.DataFrame:
    """
    Annualized realized volatility using rolling std of daily returns.
    vol = std(daily) * sqrt(252)
    """
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
    vol_target_annual: float = 0.12,   # 12% annual target vol (conservative)
    vol_window: int = 20,
    max_weight: float = 1.0,           # never lever up by default
) -> dict:
    """
    Momentum rotator + risk controls.

    - Momentum score: close/close.shift(lookback) - 1
    - Winner each day = max momentum
    - Must-beat-SPY filter: if winner momentum <= SPY momentum -> CASH
    - Vol scaling: weight = clamp(target_vol / realized_vol, 0, max_weight)
      (uses realized vol of held ETF)
    - Costs: applied on turnover when position/weight changes:
        cost_return = turnover * (fee_bps + slippage_bps) / 10000
      turnover is absolute change in weight between t-1 and t
    - Returns: no lookahead
        today's return uses yesterday's selected holding and yesterday's weight
    """
    if lookback < 2:
        raise ValueError("lookback must be >= 2")

    # Normalize column names
    close = close.copy()
    close.columns = [str(c).upper().strip() for c in close.columns]

    if must_beat_spy and "SPY" not in close.columns:
        raise ValueError("must_beat_spy=True requires SPY in your symbols universe.")

    # daily returns
    daily_ret = close.pct_change()

    # momentum score
    mom = (close / close.shift(lookback) - 1.0).dropna()

    # align returns to momentum dates
    daily_ret = daily_ret.loc[mom.index].fillna(0.0)

    # realized vol (annualized)
    vol = annualized_vol(daily_ret, vol_window)

    # pick winner each day based on momentum
    winner = mom.idxmax(axis=1)

    # must-beat-SPY filter -> CASH
    if must_beat_spy:
        spy_mom = mom["SPY"]
        # safer version (no .lookup):
        win_mom = pd.Series(
            [mom.loc[dt, winner.loc[dt]] for dt in mom.index],
            index=mom.index,
            name="winner_mom",
        )
        winner = winner.where(win_mom > spy_mom, other="CASH")

    # We trade using info available at the close:
    # today’s return uses yesterday’s winner (avoid lookahead)
    holding = winner.shift(1).dropna()

    # align daily returns and vol to holding index
    daily_ret = daily_ret.loc[holding.index]
    vol = vol.loc[holding.index]

    # build weight series (vol scaling)
    weights = []
    for dt in holding.index:
        sym = holding.loc[dt]
        if sym == "CASH":
            weights.append(0.0)
            continue

        v = vol.loc[dt, sym]
        if pd.isna(v) or v <= 0:
            # if vol not available yet, stay in cash (safer)
            weights.append(0.0)
            continue

        w = vol_target_annual / float(v)
        weights.append(clamp(w, 0.0, max_weight))

    weight_s = pd.Series(weights, index=holding.index, name="weight")

    # strategy daily returns with cash blend:
    # r = weight * r_asset + (1-weight) * 0
    strat_ret = []
    for dt in holding.index:
        sym = holding.loc[dt]
        w = float(weight_s.loc[dt])
        if sym == "CASH" or w <= 0.0:
            strat_ret.append(0.0)
        else:
            strat_ret.append(w * float(daily_ret.loc[dt, sym]))

    strat_ret = pd.Series(strat_ret, index=holding.index, name="strategy_return")

    # transaction costs/slippage on turnover (weight changes)
    total_bps = float(fee_bps) + float(slippage_bps)
    if total_bps > 0:
        # turnover = |w_t - w_{t-1}|, plus a full reset when symbol changes
        prev_h = holding.shift(1)
        prev_w = weight_s.shift(1).fillna(0.0)

        symbol_changed = (holding != prev_h) & prev_h.notna()
        # if symbol changes, treat as fully exiting then entering:
        # turnover approx = prev_w + w_t
        turnover = (weight_s - prev_w).abs()
        turnover = turnover.where(~symbol_changed, other=(prev_w.abs() + weight_s.abs()))

        cost_ret = turnover * (total_bps / 10000.0)
        strat_ret = strat_ret - cost_ret.fillna(0.0)

    # equity curve
    equity = (1.0 + strat_ret).cumprod() * start_cash
    equity.name = "equity"

    # switches count (ignoring CASH->CASH)
    switches = int((holding != holding.shift(1)).sum() - 1) if len(holding) > 1 else 0
    switches = max(0, switches)

    # metrics
    sr = sharpe_ratio(equity.pct_change().fillna(0.0))
    mdd = max_drawdown(equity)

    # win rate (day-level): % of days strat_ret > 0
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
# CLI
# -----------------------------
def main():
    p = argparse.ArgumentParser()
    p.add_argument("--mode", choices=["tp_sl", "rotator"], default="tp_sl")

    # common
    p.add_argument("--start", required=True)
    p.add_argument("--end", default=None)
    p.add_argument("--cash", type=float, default=10000.0)
    p.add_argument("--debug", action="store_true")
    p.add_argument("--plot", action="store_true")

    # tp/sl mode
    p.add_argument("--symbol", default="SPY")
    p.add_argument("--tp", type=float, default=0.01)
    p.add_argument("--sl", type=float, default=0.007)

    # rotator mode
    p.add_argument("--lookback", type=int, default=30)
    p.add_argument("--symbols", default="SPY,QQQ,VTI,IWM")
    # rotator upgrades
    p.add_argument("--fee_bps", type=float, default=1.0, help="Trading fees in bps per turnover (e.g., 1.0 = 0.01%)")
    p.add_argument("--slip_bps", type=float, default=2.0, help="Slippage in bps per turnover (e.g., 2.0 = 0.02%)")
    p.add_argument("--no_must_beat_spy", action="store_true", help="Disable must-beat-SPY filter")
    p.add_argument("--vol_target", type=float, default=0.12, help="Target annual volatility (e.g., 0.12 = 12%)")
    p.add_argument("--vol_window", type=int, default=20, help="Rolling window for realized vol (trading days)")
    p.add_argument("--max_weight", type=float, default=1.0, help="Max position weight (<=1 avoids leverage)")


    args = p.parse_args()
    end = args.end or pd.Timestamp.utcnow().strftime("%Y-%m-%d")

    if args.mode == "tp_sl":
        ohlc = fetch_ohlc(args.symbol, args.start, end, debug=args.debug)
        res = backtest_tp_sl(ohlc, tp=args.tp, sl=args.sl, start_cash=args.cash)

        print(f"Mode: tp_sl")
        print(f"Symbol: {args.symbol}")
        print(f"Period: {ohlc.index.min().date()} → {ohlc.index.max().date()}  (bars={len(ohlc)})")
        print(f"TP={args.tp:.4%}  SL={args.sl:.4%}")
        print(f"Trades: {len(res.trades)}")
        print(f"Win rate: {res.win_rate:.2%}")
        print(f"Sharpe: {res.sharpe:.2f}")
        print(f"Max drawdown: {res.mdd:.2%}")
        print(f"Final equity: ${res.equity.iloc[-1]:,.2f}")

        if args.plot:
            plot_equity(res.equity, f"{args.symbol} TP/SL Backtest")

        if len(res.trades) > 0:
            res.trades.to_csv("trades.csv", index=False)

        return

    # -----------------------------
    # Mode: rotator
    # -----------------------------
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

    print("Mode: rotator")
    print(f"Universe: {symbols}")
    print(f"Lookback: {args.lookback} trading days")
    print(f"Must-beat-SPY: {not args.no_must_beat_spy}")
    print(f"Costs: fee={args.fee_bps:.2f} bps, slip={args.slip_bps:.2f} bps (per turnover)")
    print(f"Vol scaling: target={args.vol_target:.2%}, window={args.vol_window}, max_weight={args.max_weight:.2f}")
    print(f"Period: {prices.index.min().date()} → {prices.index.max().date()}  (bars={len(prices)})")
    print(f"Switches: {res['switches']}")
    print(f"Win rate (day-level): {res['win_rate']:.2%}")
    print(f"Sharpe: {res['sharpe']:.2f}")
    print(f"Max drawdown: {res['mdd']:.2%}")
    print(f"Final equity: ${res['equity'].iloc[-1]:,.2f}")

    # save outputs
    res["holdings"].to_csv("holdings.csv")
    res["weights"].to_csv("weights.csv")
    res["equity"].to_frame("equity").to_csv("equity.csv")
    res["strategy_return"].to_csv("strategy_return.csv")


    print("Mode: rotator")
    print(f"Universe: {symbols}")
    print(f"Lookback: {args.lookback} trading days")
    print(f"Period: {prices.index.min().date()} → {prices.index.max().date()}  (bars={len(prices)})")
    print(f"Switches: {res['switches']}")
    print(f"Sharpe: {res['sharpe']:.2f}")
    print(f"Max drawdown: {res['mdd']:.2%}")
    print(f"Final equity: ${res['equity'].iloc[-1]:,.2f}")

    # save holdings / equity
    res["holdings"].to_csv("holdings.csv")
    res["equity"].to_frame("equity").to_csv("equity.csv")

    if args.plot:
        plot_equity(res["equity"], f"Rotator ({args.lookback}d momentum)")


if __name__ == "__main__":
    main()
