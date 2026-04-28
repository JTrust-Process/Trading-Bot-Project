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
├── market_data.py      # yfinance price data wrapper
├── monitor.py          # Run lifecycle tracking (Supabase)
├── notify.py           # Discord webhook notifications
├── backtest.py         # Backtesting engine (offline use)
├── list_symbols.py     # Utility to list held positions
├── requirements.txt
└── .github/
    └── workflows/
        └── trading-bot.yml
```

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

### 4. Add GitHub Secrets

Go to **Settings → Secrets → Actions** and add:

| Secret | Description |
|--------|-------------|
| `PUBLIC_SECRET` | Public.com API secret |
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
| `MIN_HOLD_DAYS` | `2` | Minimum days to hold before selling (prevents PDT) |
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
| Price data | Polygon.io |
| Database | Supabase (PostgreSQL) |
| Dashboard | Next.js 16, Tailwind, Recharts, Framer Motion |
| Hosting | Vercel |
| Notifications | Discord webhooks |

---

## Disclaimer

This bot trades real money in a live brokerage account. Use at your own risk. Past performance does not guarantee future results. This is a personal project for educational purposes only.