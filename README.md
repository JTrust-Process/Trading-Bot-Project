# 🤖 Public Trading Bot

An automated stock trading bot built on Python, GitHub Actions, and Supabase. Executes momentum-rotation and volatility breakout strategies on US equities via the [Public.com API](https://public.com/api), with a cyberpunk-themed Next.js dashboard for real-time monitoring.

---

## Architecture

```
GitHub Actions (cron) → bot.py → Public.com API (orders)
                              → Supabase (trades, logs, runs)
                              → Discord (notifications)
                              ↓
                        Next.js Dashboard (Vercel)
```

---

## Features

- **Momentum rotation** — ranks symbols by weighted short-term returns `(1d×0.3) + (5d×0.4) + (10d×0.3)` and buys the top N leaders
- **Volatility breakout** — detects price breakouts above a 20-bar consolidation range, with separate thresholds for bull/bear regimes
- **Confidence scoring** — combines momentum, trend strength, and breakout margin into a 0–1 score that scales position size
- **Market regime filter** — Dual SMA (20/50-day) determines bull/bear. Price must be above BOTH SMAs for bull — faster reaction than single SMA; momentum buys blocked in bear, breakouts allowed with stricter thresholds and half sizing
- **Risk management** — dynamic TP/SL, daily loss limit, max drawdown, multi-day cooldowns, exposure caps
- **Full observability** — every log event mirrored to Supabase `bot_logs`, run lifecycle tracked in `bot_runs`, errors in `bot_errors`
- **Discord notifications** — rich embeds for buys, sells, run summaries, errors, and regime changes
- **Live dashboard** — 3-tab Next.js app (Trade Analytics, Bot Monitor, Bot Logs) with real-time Supabase sync

---

## File Structure

```
├── bot.py              # Main bot engine
├── strategy.py         # ATR and trend strength calculations
├── momentum.py         # Momentum scoring and ranking
├── breakout.py         # Volatility breakout signal engine
├── market_data.py      # Polygon price data wrapper (live bot path)
├── public_bars.py      # NEW: Public.com historical bars data layer (additive)
├── indicators.py       # NEW: pure-pandas SMA/EMA/ATR/momentum/breakout helpers
├── backtest_public.py  # NEW: standalone backtest harness over Public bars
├── analyze_backtests.py # NEW: multi-period symbol ranking + classification (advisory)
├── monitor.py          # Run lifecycle tracking (Supabase)
├── notify.py           # Discord webhook notifications
├── backtest.py         # Backtesting engine (offline use, yfinance based)
├── list_symbols.py     # Utility to list held positions
├── requirements.txt
└── .github/
    └── workflows/
        └── trading-bot.yml
```

---

## Public.com historical bars (`public_bars.py`)

We pull historical OHLCV directly from Public via:

```
GET https://api.public.com/userapigateway/marketdata/historicdata/{symbol}/{period}
```

Reference: <https://public.com/api/docs/resources/market-data/get-bars-v2>

Supported periods: `DAY`, `WEEK`, `MONTH`, `QUARTER`, `HALF_YEAR`, `YEAR`,
`FIVE_YEARS`, `YTD`, `SINCE_PURCHASE`. Default in our code is `YEAR`
(~252 daily bars, enough for 200-day SMA / regime filters).

The response is parsed defensively — Public returns numeric fields as strings,
and we coerce them to floats. `regularMarket.bars` is read by default;
`preMarket` and `afterMarket` can be opted into with flags.

Quick check from your venv:

```bash
python public_bars.py SPY YEAR
```

This is **additive**. The live bot still uses `market_data.py` (Polygon) for
its current price history. `public_bars.py` exists so we can backtest and
A/B-compare a Public-bars-driven signal stack before migrating live trading.

---

## Backtest harness (`backtest_public.py`)

Standalone offline script. Does **not** touch live order placement.

```bash
# Use Public.com bars (requires PUBLIC_SECRET):
python backtest_public.py --source public --period YEAR

# Or use yfinance for an offline baseline:
python backtest_public.py --source yfinance --period YEAR
```

Default symbol universe: `SPY,QQQ,AAPL,MSFT,NVDA,AMZN,GOOGL,META,TSLA`.
Outputs `backtest_public_summary.csv` and `backtest_public_trades.csv`, plus
a console table with: trades, win rate, avg win, avg loss, max drawdown,
profit factor, total return.

The harness runs both:

1. **`old`** — replicates the live bot's SMA-cross + weighted momentum + recent-high breakout logic on the same bars.
2. **`new`** — a more conservative bars-based variant: SPY-above-SMA(50) regime filter, ATR-based stop/take, minimum hold, cooldown after exit.

Use this to compare signal quality before flipping any live trading code.

---

## Multi-period analyzer (`analyze_backtests.py`)

Research / decision-support layer over `backtest_public.py`. Runs both
strategies across `MONTH`, `QUARTER`, `HALF_YEAR`, and `YEAR` for every
symbol, then aggregates with the median (robust to one bad period).

```bash
python analyze_backtests.py --source public
python analyze_backtests.py --source yfinance
python analyze_backtests.py --source public --periods QUARTER,HALF_YEAR,YEAR
python analyze_backtests.py --source public --symbols SPY,QQQ,AAPL
```

Outputs:

- `analyze_detail.csv` — every (symbol × period × strategy) row with
  trades, win rate, avg win, avg loss, win/loss ratio, max drawdown, profit
  factor, total return, bar count.
- `analyze_ranking.csv` — one row per symbol with the OLD- and
  NEW-strategy aggregated medians plus a classification.
- A console table with the ranking sorted by classification.

**Classification buckets** (from OLD-strategy aggregated metrics — the OLD
strategy is what the live bot actually runs today):

| classification    | label      | rough rule                                     |
| ----------------- | ---------- | ---------------------------------------------- |
| `keep_active`     | strong     | median ret >+5% & PF >=1.5 & >=50% positive periods & DD better than -20% |
| `reduce_priority` | marginal   | median ret >0 & PF >=1.0 & >=50% positive periods                          |
| `paper_only`      | unstable   | mixed signals across periods, didn't qualify above              |
| `remove`          | weak       | median ret < -5% OR PF < 0.8 OR <25% positive periods           |

The thresholds live as constants at the top of `analyze_backtests.py` —
tune them freely; they're advisory.

**This script is research-only.** It does not modify `bot.py`, order
placement, risk settings, or `SCAN_SYMBOLS`. Use the output to inform a
human decision about which symbols to keep, demote, paper-test, or drop.

---

## Setup

### 1. Clone the repo

```bash
git clone https://github.com/YOUR_USERNAME/YOUR_REPO.git
cd YOUR_REPO
python -m venv venv
venv\Scripts\activate        # Windows
pip install -r requirements.txt
```

### 2. Create `.env`

```dotenv
PUBLIC_SECRET=your_public_api_secret
# Optional, but REQUIRED if your Public personal access token can see more
# than one brokerage account. Otherwise the bot defaults to accounts[0] and
# can silently lock onto the wrong/empty account, missing every TP/SL.
# Run `python scripts/list_public_accounts.py` to see the available IDs.
# PUBLIC_ACCOUNT_ID=your_brokerage_account_id
SUPABASE_URL=https://your-project.supabase.co
SUPABASE_SERVICE_KEY=your_service_role_key
DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/...

MODE=live
SYMBOLS=QQQ,SCHB,SCHD,SGOV,SPY,VTI
SCAN_SYMBOLS=AAPL,MSFT,NVDA,AMZN,GOOGL,META,TSLA
MOMENTUM_SYMBOLS=AAPL,MSFT,NVDA,AMZN,GOOGL,META,TSLA,QQQ,SPY
MOMENTUM_TOP_N=3
MOMENTUM_SELL_RANK=5
MAX_ORDER_AMOUNT_USD=8
MAX_OPEN_POSITIONS=1
```

### 3. Create Supabase tables

Run these in your Supabase SQL editor:

```sql
create table trades (
  id uuid default gen_random_uuid() primary key,
  created_at timestamptz default now(),
  timestamp_utc text, symbol text, side text,
  entry_price numeric, exit_price numeric,
  pnl_percent numeric, pnl_usd numeric,
  amount_usd numeric, strategy text
);

create table bot_runs (
  id uuid primary key, start_time timestamptz,
  end_time timestamptz, status text default 'running',
  total_trades int default 0, total_errors int default 0,
  duration_ms bigint, notes text
);

create table bot_events (
  id uuid primary key, run_id uuid references bot_runs(id),
  timestamp timestamptz, event_type text, symbol text, metadata jsonb
);

create table bot_errors (
  id uuid primary key, run_id uuid references bot_runs(id),
  timestamp timestamptz, stage text, symbol text,
  error_type text, message text, severity text, retry_count int default 0
);

create table bot_logs (
  id uuid default gen_random_uuid() primary key,
  timestamp_utc timestamptz, event text, symbol text,
  side text, amount_usd text, order_id text, status text, details text
);
```

Enable RLS and add public read policies for each table:

```sql
alter table trades enable row level security;
create policy "Allow public read" on trades for select using (true);
-- repeat for bot_runs, bot_events, bot_errors, bot_logs
```

For audit fixes (per-symbol state mirror, ID defaults, positions unique
constraint), run the migration in `supabase_migrations/2026_05_08_audit_fixes.sql`
in the Supabase SQL editor. This is required for cooldowns, drawdown peaks,
and the daily-loss kill switch to actually persist across GitHub Actions runs.

Also create the positions table for open/closed position tracking:

```sql
create table positions (
  id uuid default gen_random_uuid() primary key,
  symbol text not null,
  entry_price numeric,
  entry_date text,
  quantity numeric,
  amount_usd numeric,
  strategy text,
  status text default 'open',
  exit_price numeric,
  exit_date text,
  pnl_pct numeric,
  pnl_usd numeric,
  close_reason text,
  entry_estimated boolean default false,
  updated_at timestamptz default now()
);

alter table positions enable row level security;
create policy "Allow public read" on positions for select using (true);
alter publication supabase_realtime add table positions;
```


### 4. Add GitHub Secrets

Go to **Settings → Secrets → Actions** and add:

| Secret | Description |
|--------|-------------|
| `PUBLIC_SECRET` | Public.com API secret |
| `PUBLIC_ACCOUNT_ID` | (Optional) Pins which brokerage account to trade against. **Required if you have more than one Public brokerage account under the same login** — otherwise the bot defaults to `accounts[0]` and can lock onto the wrong/empty account, silently skipping every TP/SL. Run `python scripts/list_public_accounts.py` locally to get the IDs. |
| `SUPABASE_URL` | Supabase project URL |
| `SUPABASE_SERVICE_KEY` | Supabase service role key |
| `DISCORD_WEBHOOK_URL` | Discord webhook URL |

### 5. Run locally

```bash
python bot.py
```

---

## Configuration

All config is set via environment variables. Key parameters:

| Variable | Default | Description |
|----------|---------|-------------|
| `SYMBOLS` | `SPY` | Held symbols to manage TP/SL on |
| `SCAN_SYMBOLS` | `""` | Additional symbols to scan for buys |
| `MOMENTUM_SYMBOLS` | Mag 7 + QQQ/SPY | Symbols ranked by momentum |
| `MOMENTUM_TOP_N` | `3` | Buy top N momentum leaders |
| `MOMENTUM_SELL_RANK` | `5` | Sell if rank drops below this |
| `MAX_ORDER_AMOUNT_USD` | `10` | Hard dollar cap per buy order |
| `MAX_OPEN_POSITIONS` | `1` | Max simultaneous positions |
| `TAKE_PROFIT_BASE` | `0.04` | Base take profit (4%) |
| `STOP_LOSS_BASE` | `0.02` | Base stop loss (2%) |
| `MAX_DRAWDOWN` | `0.08` | Max drawdown before forced sell (8%) |
| `DAILY_LOSS_LIMIT` | `0.03` | Daily loss limit per symbol (3%) |
| `LOSS_COOLDOWN_DAYS` | `3` | Days to halt symbol after loss |
| `REGIME_SYMBOL` | `SPY` | Symbol used for regime detection |
| `REGIME_FAST` | `20` | Fast SMA for regime (20-day) — price must be above this |
| `REGIME_SLOW` | `50` | Slow SMA for regime (50-day) — price must be above this |
| `MIN_HOLD_DAYS` | `1` (code) / `2` (workflow) | Minimum days to hold before selling (prevents PDT). Workflow sets `2`. |
| `CONFIDENCE_MIN_THRESHOLD` | `0.3` | Minimum confidence score to buy |
| `BREAKOUT_LOOKBACK` | `20` | Bars to define consolidation range |
| `BREAKOUT_THRESHOLD_BULL` | `0.1` | Bull breakout threshold (10% of range) |
| `BREAKOUT_THRESHOLD_BEAR` | `0.3` | Bear breakout threshold (30% of range) |

---

## Signal Logic

Each cycle the bot:

1. Computes SPY 50/200 SMA regime (`bull` / `bear`)
2. Scores and ranks all `MOMENTUM_SYMBOLS` by weighted return
3. Checks exit conditions on held positions (momentum fade, TP, SL, drawdown)
4. Enters buy loop — for each candidate symbol:
   - Momentum signal: requires `bull` regime, positive score, rank ≤ `MOMENTUM_TOP_N`
   - Breakout signal: runs in both regimes with regime-specific thresholds
   - Computes confidence score from momentum + trend strength + breakout margin
   - Sizes position: `base_alloc × confidence_factor × breakout_size_factor`
   - Places order via Public API using notional `amount` for fractional shares

---

## Dashboard

A separate Next.js app deployed on Vercel reads from Supabase in real time.

**Tabs:**
- **Trade Analytics** — equity curve, daily PnL, win rate, symbol performance, strategy breakdown
- **Bot Monitor** — run history, event feed, error log
- **Bot Logs** — full filterable log of every bot event with symbol filter

Dashboard repo: `Trading-Bot-Dashboard`

---

## Backtesting

```bash
python backtest.py \
  --mode tp_sl \
  --symbol SPY \
  --start 2024-01-01 \
  --tp 0.04 \
  --sl 0.02 \
  --plot
```

---

## Tech Stack

| Layer | Technology |
|-------|-----------|
| Bot engine | Python 3.11 |
| Scheduling | GitHub Actions (hourly, Mon–Fri) |
| Brokerage | Public.com API |
| Price data | Polygon.io (free tier) |
| Database | Supabase (PostgreSQL) |
| Dashboard | Next.js 16, Tailwind, Recharts, Framer Motion |
| Hosting | Vercel |
| Notifications | Discord webhooks |

---

## Disclaimer

This bot trades real money in a live brokerage account. Use at your own risk. Past performance does not guarantee future results. This is a personal project for educational purposes only.