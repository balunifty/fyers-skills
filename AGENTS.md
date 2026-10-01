# AGENTS.md — fyers-skills

> Comprehensive guide for AI agents working on this project.

---

## Project Overview

**fyers-skills** is a fork of the public `FyersDev/fyers-skills` repo (Agent Skills for the FYERS API v3) that has been heavily extended with a local `strategies/` automation layer — a full Indian-market (NSE) intraday trading system with scanners, schedulers, SQLite stores, a web dashboard, and LLM-based agents.

**Two halves:**
1. **`skills/`** — distributable Agent Skills (`fyers-trading`, `fyers-supercharge`) for Claude Code / Cursor / Open Claw
2. **`strategies/`** — the owner's local trading automation platform (the bulk of the codebase)

**Tech Stack**: Python 3.11, SQLite, FYERS API v3 (`fyers-apiv3` SDK), WebSockets, Flask/`http.server`, pandas, numpy, LangChain (LLM agent), Windows Task Scheduler

**Git**: Branch `scratch/baluFyers`, remote `origin` → `github.com/balunifty/fyers-skills` (fork), `upstream` → `github.com/FyersDev/fyers-skills`

---

## Project Structure

```
fyers-skills/
├── .env                        # FYERS credentials (git-ignored, present locally)
├── README.md                   # Upstream skill documentation
├── skills/                     # Distributable Agent Skills
│   ├── fyers-trading/          #   Core skill (OAuth, REST, WebSocket, orders, indicators)
│   │   ├── SKILL.md
│   │   ├── references/         #   12 docs (auth, endpoints, orders, websocket, etc.)
│   │   └── scripts/            #   11 Python scripts (login, client, symbols, indicators...)
│   └── fyers-supercharge/      #   Multi-agent optimization loop skill
│       ├── SKILL.md
│       ├── references/         #   7 docs (loop, council, variants, metrics, etc.)
│       └── scripts/            #   scorecard.py, dashboard.py, evolution_log.py
└── strategies/                 # ← Local trading automation platform
    ├── agent/                  #   LLM + rule-based trading agents
    │   ├── rule_agent.py       #   No-LLM rule engine (--scan/--chat/--monitor)
    │   ├── trading_agent.py    #   LangChain + GPT-4o agent
    │   ├── fyers_tools.py      #   Tool wrappers for agents
    │   ├── global_market_fetcher.py
    │   └── knowledge/          #   strategies.json, market_wisdom.json
    ├── config/                 #   Order gating configuration
    │   ├── order_config.py     #   ConfigGatedFyersClient (safety core)
    │   ├── equity/config.json  #   place_order: "NO" (see Current config state)
    │   ├── fno/config.json     #   place_order: "NO", qty: 100
    │   └── order_state/        #   Daily entry counters (git-ignored)
    ├── data/                   #   Symbol universe lists (txt/csv/json)
    │   ├── NiftyFNOTop100.txt  #   100 NSE F&O symbols
    │   ├── Nifty50.txt, Nifty500.txt, Indices.txt
    │   └── fno_symbols.txt, fno_stocks.csv/json, entered_5min.json
    ├── databases/              #   SQLite DBs + state (git-ignored)
    ├── logs/                   #   Log files + Excel trade logs (git-ignored)
    ├── scripts/                #   ~21 strategy scripts + bat/ps1 launchers
    ├── testing/                #   20 unittest suites + runners + flow.mmd
    ├── ui/                     #   Read-only web dashboard (port 9999)
    ├── utils/                  #   Shared fetchers, indicators, excel logger, queries
    └── __init__.py
```

---

## What the System Does

### Data Flow

```
[Universe]  NiftyFNOTop100.txt + Indices.txt
     │
     ▼
COLLECT ──► FO_WebSocket (websocketNiftyfno100.py)
     │        15-min IST candles + EMA/HMA/RSI → NiftyFNOTop100_websocket.db
     │      FO_SharedDataFetcher (shared_data_fetcher.py) every 15 min
     │        priority: websocket DB → shared_15min_candles.db → REST API
     │      SyncNifty500Shared15min.py → nifty500_15min.db  (SHARED store)
     │        FetchNifty500Above100 (30d, >₹100) + NIFTY/BANKNIFTY indices
     │        + balu-compatible indicators_15m + intra_15m_* read views
     │        also read by the balutradingapp (its own 15m scheduler is off)
     │      FetchVolumeShockers15min.py --reuse-candles (same shared DB)
     │        reuses stored 30d bars, volume-shocker screen + EMA/ORB filter
     │      EquityNifty50Ema10crossover50-15min.py → nifty50.db
     ▼
SCAN ────► run_all_15min_strategies.ps1 → EquityAllStrategies15min.py
     │        --once every 15 min, 09:15–15:15 IST, dry-run default
     │        STRATEGY_REGISTRY = 23 rules, adapter per source function
     │      Legacy 5-min tasks → OrbStrategyCallPut / BuyCallOption* (--live)
     │      FO_RuleAgent → rule_agent.py --scan every 5 min
     │      Dashboard (manual) → equity_strategy_dashboard.py :9999
     ▼
GATE ────► config/order_config.py ConfigGatedFyersClient
     │        equity/config.json (name starts "Equity") vs fno/config.json
     │        place_order=="YES" • max_stocks_per_day (file-locked state)
     │        qty / stop_loss / trailing_stop_loss read live
     │        + CLI --live flag (both required = "two-gate rule")
     ▼
EXECUTE ─► skills/fyers-trading/scripts/fyers_client.py
     │        symbol validation, MARKET order, 429 backoff
     │        token from ~/.fyers/token.json (daily OAuth)
     ▼
OUTPUT ──► SQLite ledgers (matched_signals, exactly-once by signal_id)
              Excel: logs/Strategy_Trade_Log.xlsx + per-strategy .xlsx
              Logs: strategies/logs/*.log + scheduler logs
              Alerts: rule_agent alerts.log, trading_agent console/WA/TG
              Dashboard: read-only HTML table (never orders)
```

---

## Database Schemas

### `shared_15min_candles.db` (API cache)
```sql
CREATE TABLE candles_15min (
    symbol TEXT, epoch INTEGER, candle_time TEXT,
    open REAL, high REAL, low REAL, close REAL, volume INTEGER,
    fetched_at TEXT,
    PRIMARY KEY (symbol, epoch)
)
```

### `NiftyFNOTop100_websocket.db`
```sql
-- One table per symbol: candles_15m_<safe_symbol>
CREATE TABLE candles_15m_<symbol> (
    candle_time TEXT PRIMARY KEY,
    open REAL, high REAL, low REAL, close REAL, volume INTEGER,
    ema10 REAL, ema20 REAL, ema30 REAL, hma21 REAL, rsi14 REAL
)
```

### `dashboard_15min_candles.db` / `nifty500_15min.db` (shared 15-min store)
```sql
CREATE TABLE candles (
    symbol TEXT, resolution TEXT, epoch INTEGER,
    open REAL, high REAL, low REAL, close REAL, volume INTEGER,
    stored_at TEXT,
    PRIMARY KEY (symbol, resolution, epoch)
)
-- Index: idx_candles_symbol_resolution(symbol, resolution, epoch DESC)
-- WAL mode enabled

-- nifty500_15min.db only: balu-compatible indicators written by
-- SyncNifty500Shared15min.py (formulas from strategies/utils/balu_indicators.py)
CREATE TABLE indicators_15m (
    symbol TEXT NOT NULL, datetime TEXT NOT NULL,  -- bare symbol: TCS, NIFTY
    ema_5 REAL, ema_9 REAL, ema_10 REAL, ema_15 REAL, ema_20 REAL, ema_21 REAL, ema_50 REAL,
    sma_20 REAL, sma_50 REAL, sma_200 REAL,
    rsi_14 REAL, tsi REAL, hull_ma_9 REAL, macd_hist REAL, adx_14 REAL, vwap REAL,
    PRIMARY KEY (symbol, datetime)
);
-- nifty500_15min.db only: per-symbol compatibility VIEWS for the
-- balutradingapp, shaped like its old fetch script's tables. datetime is
-- local-time '%Y-%m-%d %H:%M:%S' from the bar's epoch (same key
-- indicators_15m joins on); volume CAST to INTEGER:
--   CREATE VIEW intra_15m_<SYM> AS
--     SELECT datetime(epoch,'unixepoch','localtime') AS datetime,
--            open, high, low, close, CAST(volume AS INTEGER) AS volume
--     FROM candles WHERE symbol='NSE:<SYM>-EQ' AND resolution='15'
-- The shockers/runs tables (next section) also live here; the sync prunes
-- candles + indicators older than 30 days each run.
```

### `nifty50.db`
```sql
CREATE TABLE candles_15m_<symbol> (
    symbol TEXT, epoch INTEGER, candle_time TEXT,
    open REAL, high REAL, low REAL, close REAL, volume INTEGER,
    ema10 REAL, ema20 REAL, ema30 REAL, ema50 REAL,
    rsi14 REAL, adx14 REAL, hma21 REAL, fetched_at TEXT,
    PRIMARY KEY (symbol, epoch)
)
```

### `shockers` + `runs` (FetchVolumeShockers15min.py; **in the shared `nifty500_15min.db`**)
```sql
-- The script's connect() also ensures a candles table (same layout as
-- dashboard_15min_candles.db); in the shared store it already exists.
-- The legacy file strategies/databases/volume_shockers_15min.db (its old
-- destination) is no longer read by anything.
-- Rewritten every run (even with zero rows) so stale shockers cannot linger;
-- the dashboard tab reads only rows with at least one strategy flag = 1
-- (ema_cross, orb_breakout, the six *_rej flags, crossed50ema); the loader
-- filters on whatever flags the open schema actually has, and connect()
-- widens an older table in place with ALTER TABLE
CREATE TABLE shockers (
    symbol TEXT PRIMARY KEY, price REAL, change_percent REAL,
    volume_today REAL, avg_daily_volume REAL, sessions_used INTEGER,
    session_progress REAL, rvol REAL,
    ema_cross INTEGER DEFAULT 0, orb_breakout INTEGER DEFAULT 0,
    orb_high_rej INTEGER DEFAULT 0, r1_rej INTEGER DEFAULT 0,
    pdh_rej INTEGER DEFAULT 0, pdl_rej INTEGER DEFAULT 0,
    s1_rej INTEGER DEFAULT 0, orb_low_rej INTEGER DEFAULT 0,
    crossed50ema INTEGER DEFAULT 0,
    orb_high REAL, ema10 REAL, ema20 REAL, ema30 REAL,
    signal_candle_epoch INTEGER, run_at_ist TEXT, details_json TEXT
    -- details_json: {ema, orb, crossed50, rejections:{kind:{level, reason,
    -- candle_epoch, sl_hit, sl_candle_epoch, sl_reason, ...}}}
);
-- One row per run (newest first) with thresholds and counts
CREATE TABLE runs (
    run_at_ist TEXT PRIMARY KEY, rvol_threshold REAL, min_price REAL,
    min_volume REAL, history_days INTEGER, universe_size INTEGER,
    symbols_screened INTEGER,
    shockers INTEGER, strategy_filtered INTEGER, ema_crosses INTEGER,
    orb_breakouts INTEGER, market_open INTEGER
);
```

### Signal ledgers (`equity_all_strategies_15min.db`, etc.)
```sql
CREATE TABLE matched_signals (
    signal_id TEXT PRIMARY KEY,
    strategy_name TEXT, symbol TEXT,
    candle_epoch INTEGER, candle_time_ist TEXT, matched_at_ist TEXT,
    details_json TEXT, mode TEXT, quantity INTEGER,
    order_tag TEXT, order_status TEXT, order_id TEXT, broker_message TEXT,
    match_printed INTEGER, attempted_at_ist TEXT,
    excel_logged INTEGER, excel_revision INTEGER,
    UNIQUE (symbol, candle_epoch)  -- exactly-once guarantee
)
```

---

## Scheduled Tasks

Register via `strategies\scripts\schedule_tasks.bat` (run as Administrator). Uses `pythonw.exe` (Python 3.11), `WORKDIR=C:\Users\Admin\fyersAutomation\fyers-skills`.

| Task Name | Command | Trigger |
|-----------|---------|---------|
| `FO_WebSocket` | `pythonw strategies\utils\websocketNiftyfno100.py` | Daily 09:15 → 15:00 |
| `FO_SharedDataFetcher` | `pythonw strategies\utils\shared_data_fetcher.py` | Every 15 min, 09:15 → 15:00 |
| `FO_OrbStrategy` | `pythonw strategies\scripts\OrbStrategyCallPut.py --live` | Every 5 min, 09:15 → 15:00 |
| `FO_BuyCallEMA_10_20_30` | `pythonw ...\BuyCallOption102030EmaCrossover5min.py --live` | Every 5 min |
| `FO_BuyCallEMA_10_50` | `pythonw ...\BuyCallOptionEma10_50Crossover.py --live` | Every 5 min |
| `FO_RuleAgent` | `pythonw strategies\agent\rule_agent.py --scan` | Every 5 min |
| `FO_VolumeShockers` | `pythonw strategies\scripts\FetchVolumeShockers15min.py --weekdays-only --reuse-candles` | Mon–Fri, every 15 min, 09:20 → 15:50 |
| `FO_Nifty500Fetch` | `pythonw strategies\scripts\SyncNifty500Shared15min.py` | Mon–Fri, every 15 min, 09:15 → 15:45 |

**Important notes:**
- Tasks run **only while the user is logged in** (no SYSTEM/service mode)
- F&O `--live` flags are present but `fno/config.json` has `place_order: "NO"` → orders blocked at config gate
- **Recurring triggers**: register repeat-tasks with `/sc weekly /d MON,...,FRI /st HH:MM /ri N /du HH:MM` (or `/sc daily /ri /du`). Never use `/sc minute ... /et HH:MM` — `schtasks` only stamps that as an absolute `EndBoundary` on the creation date, so the task fires once and never again (this killed FO_SharedDataFetcher/FO_OrbStrategy/FO_BuyCall*/FO_RuleAgent registrations after 2026-09-15)
- `FO_VolumeShockers` starts 5 min after `FO_Nifty500Fetch` on purpose: the shared store must already hold the just-closed bar for `--reuse-candles` to skip the API (both windows end after the 15:30 close so the final 15:15–15:30 candle lands in the screen)
- `delete_tasks.bat` deletes all `FO_*` tasks it knows about (including `FO_RuleAgent`, `FO_VolumeShockers` and `FO_Nifty500Fetch`)
- 15-min equity strategies are **NOT** in Task Scheduler — they run in PowerShell loops (see below)

### PowerShell Scheduler Loops (not Task Scheduler)

| Script | Purpose |
|--------|---------|
| `run_all_15min_strategies.ps1` | Runs `EquityAllStrategies15min.py --once` every 15 min, 09:15–15:15 IST. Prefers `pythonw`. Logs to `EquityAllStrategies15min_scheduler.log`. Self-terminates after 15:15. |
| `run_equity_ema_crossover_15min_scheduler.ps1` | Same slot logic for `EquityEma15_10_20_50Crossover15min.py`. |
| `start_all_15min_strategies.bat` | Launcher: maps `live`/`once` → `-Live`/`-Once`, runs PS1 with `-ExecutionPolicy Bypass`. **Dry-run is default.** |
| `start_equity_ema_crossover_15min_scheduler.bat` | Launcher for EMA-crossover PS1. |
| `run_equity_ema_crossover_15min.bat` | Direct run of EMA crossover script. Default `--dry-run`. |

**Console window rule**: Always use `pythonw.exe` (not `python.exe`) for scheduled/background scripts to prevent console windows from flashing.

---

## Key Scripts

### Data Collection (`strategies/utils/`)

| Script | Purpose |
|--------|---------|
| `websocketNiftyfno100.py` | WebSocket collector: 15-min IST candles + indicators → `NiftyFNOTop100_websocket.db` |
| `shared_data_fetcher.py` | Singleton fetcher with priority: websocket DB → API cache → REST. Rate limiter 5/s. |
| `common_indicators.py` | Pure-Python `ema`, `wma`, `hma`, `rsi` shared by all strategies |
| `excel_logger.py` | Appends to `logs/Strategy_Trade_Log.xlsx` |
| `websocket_data_reader.py` | Read-only reader over websocket DB |
| `balu_indicators.py` | Verbatim port of `balutradingapp/indicators/calculator.py` → feeds `indicators_15m` in the shared store; must stay formula-identical to balu's copy |

### Strategy Scripts (`strategies/scripts/`)

**Consolidated scanner:**
- `EquityAllStrategies15min.py` — **the main scanner**. `STRATEGY_REGISTRY` = 23 rules, adapter per source function. One fetch/symbol + one rate limiter + one signal ledger. Dry-run default.

**15-min equity signals (each owns one rule):**
- `EquityEma15_10_20_50Crossover15min.py` — EMA15/10/20/50 crossover + pullback. **Owns shared durable machinery** (`SignalStore`, `ConfigGatedFyersClient`, locks) that other scripts import.
- `EquityEma10_20_Signals15min.py` — EMA10 vs EMA20 cross
- `EquityOpenCandleEmaStackBuy15min.py` — Open-candle EMA stack buy
- `EquityOrbLowRejectionSignal15min.py` — Bullish rejection of ORB low
- `EquityLevelRejectionSignal15min.py` — R1 / PDH rejection
- `EquityLowerHighCloseSignal15min.py` — Lower-high/lower-close sell
- `EquityDoubleBottomBullishSignal15min.py` — Double bottom
- `EquityDailyBreakoutRsiVolume15min.py` — Daily breakout + RSI/volume
- `SecondCandleBreakout.py`, `SecondCandleSellGate15min.py` — Second-candle rules
- `EquityNifty50Ema10crossover50-15min.py` — NIFTY 50 indicators → `nifty50.db`

**F&O / 5-min strategies:**
- `OrbStrategyCallPut.py` — ORB multi-entry CE/PE (`--live`, `--close`)
- `R1PrevHighRejectionStrategy.py` — R1/prev-high rejection for F&O
- `IndexRejectionStrategy.py` — NIFTY/SENSEX/BANKNIFTY rejection
- `BuyCallOption102030EmaCrossover5min.py` — EMA10/20/30 + RSI55 → ATM CE
- `BuyCallOptionEma10_50Crossover.py` — EMA10/50 crossover → ATM CE

**Data/universe fetchers:**
- `GetFNOScriptList.py` — NSE F&O stocks (`--csv/--json/--txt/--refresh`)
- `FetchNifty500Above100.py` — NIFTY 500 universe → 15-min candles (30d,
  price floor ₹100) in `nifty500_15min.db`; called by `SyncNifty500Shared15min`
- `SyncNifty500Shared15min.py` — **keeper of the shared 15-min store**
  `strategies/databases/nifty500_15min.db`: runs `FetchNifty500Above100`
  (`--days 30`), fetches the NIFTY 50 / NIFTY BANK index symbols, computes the
  balu-compatible `indicators_15m` rows (via `strategies/utils/balu_indicators.py`,
  a verbatim port of balutradingapp's calculator), creates the `intra_15m_*`
  read views, and prunes candles/indicators older than 30 days.
  `--skip-fetch` re-derives indicators and views offline. Scheduled as
  `FO_Nifty500Fetch`. Both fyers-skills (`FO_VolumeShockers`, the dashboard's
  shockers tab) and the balutradingapp read this one file; the balu app's own
  15-min scheduler (`BalutaIntraday15mFetch`) is disabled.
- `FetchVolumeShockers15min.py` — NIFTY 500 volume-shocker screen: 30d of 15-min
  candles **reused from the shared store** (`--reuse-candles`, default on since
  2026-10-01; only symbols whose newest stored bar predates the latest completed
  candle hit the API; `--no-reuse-candles` forces a full refetch), then filters shockers
  (RVol ≥ `--rvol-threshold`, default 2.0 = session volume vs 30-day average
  pro-rated by session progress) through **nine strategy legs**, all swept
  over the latest session's candles (bars pair inside the session only, the
  session's opening bar excepted — it may pair with the prior session's final
  close, so a cross at the open is caught; later bars never pair across the
  gap):
  EMA10 cross + 10>20>30 stack · ORB high breakout · 50 EMA cross, and six
  level rejections — sell at ORB high / R1 / PDH (high reaches the level,
  close back under it, close ≤ open), buy at PDL / S1 / ORB low (low reaches
  it, close back over, close ≥ open); R1/S1 are the previous session's classic
  pivots, PDH/PDL its raw extremes, ORB levels the 09:15 candle (which defines
  them and is excluded from its own rejections). A later close back beyond
  the level marks the cell `sl_hit` ("SL HIT" on the dashboard). Session
  volume below `--min-volume` (default 5,000,000 shares) never becomes a
  shocker, and the per-run table rewrite deletes any sub-floor row left
  behind from an earlier run (`--min-price 100`, `--days 30`, `--db` (defaults
  to the shared store), `--report`; scheduled as
  `FO_VolumeShockers`, Mon–Fri every 15 min 09:20→15:50 — 5 min behind
  `FO_Nifty500Fetch` so the reuse check sees the just-closed bar; feeds the
  dashboard's Volume Shockers tab)
- `update_nifty50_stocks.py` — Refresh NIFTY 50 constituents (validates exactly 50)

### Order Gating (`strategies/config/order_config.py`)

The **safety core**. Key behaviors:
- Selects `equity/config.json` if `SCRIPT_NAME` starts with "Equity", else `fno/config.json`
- `require_place_order_enabled()` fails closed unless `"place_order": "YES"`
- `ConfigGatedFyersClient(FyersClient)` intercepts `place_order`/`exit_position`
- Enforces `max_stocks_per_day` per IST calendar day with file-lock + atomic-rename
- Exit/SL orders never consume entry slots
- `qty`, `stop_loss`, `trailing_stop_loss` read live from config each use
- **Two-gate rule**: requires BOTH config `"place_order": "YES"` AND CLI `--live` flag

### Dashboard (`strategies/ui/equity_strategy_dashboard.py`)

- Local **read-only** HTTP dashboard (`ThreadingHTTPServer`, port 9999, `--open-browser`)
- Scans F&O Top-100 + indices, evaluates **18 `STRATEGY_SPECS` columns**
- Five tabs: Intraday 15-min, End of Day, Rejections (sell), Indices, and
  **Volume Shockers** — the last one is built from `load_volume_shockers()` /
  `build_shocker_view()`, which read the shared `nifty500_15min.db`
  (`SHOCKERS_DB_PATH`; `shockers`/`runs` written by
  `FetchVolumeShockers15min.py`) read-only and show only shockers passing at
  least one of the nine strategy legs (`SHOCKER_COLUMNS`: EMA 10 Cross, ORB
  Breakout, the six rejection columns, Crossed 50 EMA; rejection cells carry
  SELL/BUY plus an amber "SL HIT" badge when the stop loss was hit;
  `SHOCKERS_DB_PATH` is patchable in tests; a missing DB yields an empty tab
  with instructions, never a scan failure)
- Price floor ₹100, quote batches of 50
- Persists scanned candles to `dashboard_15min_candles.db` (180-day retention)
- **Never places orders**

### Agents (`strategies/agent/`)

| Script | Purpose |
|--------|---------|
| `rule_agent.py` | No-LLM rule engine. `--scan/--chat/--monitor`, `--no-llm`. Loads `knowledge/strategies.json` + `market_wisdom.json`. Delegates to strategy scripts. |
| `trading_agent.py` | LangChain + GPT-4o agent. Console/WhatsApp/Telegram alerts. ChromaDB RAG memory. |
| `fyers_tools.py` | `FyersTools` wrapping `ConfigGatedFyersClient` + `SharedDataFetcher` + indicators |
| `global_market_fetcher.py` | Crude oil, dollar index, VIX, US 10Y |

---

## Technical Indicators

| Indicator | Parameters |
|-----------|------------|
| EMA | 10, 15, 20, 30, 50 |
| SMA | 20, 50, 200 |
| HMA | 21 |
| RSI | 14 (and RSI55 for 5-min strategies) |
| ADX | 14 |
| WMA | (used internally by HMA) |

All implemented in pure Python in `strategies/utils/common_indicators.py`.

---

## Safety Model

1. **Secrets**: Only via `.env` (git-ignored) or environment variables. Never committed.
2. **Dry-run by default**: All scripts default to `--dry-run`. `--live` required for orders.
3. **Two-gate rule**: Orders require BOTH config `"place_order": "YES"` AND CLI `--live`.
4. **Config gating**: `ConfigGatedFyersClient` intercepts all order calls.
5. **Daily limits**: `max_stocks_per_day` enforced with file-locked state.
6. **Symbol validation**: Before any order.
7. **Rate limits**: 10/s · 200/min · 100k/day (FYERS API). `ApiRateLimiter` at 5/s.
8. **WebSocket preferred** over REST polling.
9. **Daily token re-login**: OAuth token cached at `~/.fyers/token.json`.

**Current config state:**
- `equity/config.json`: `"place_order": "NO"`, `qty: 1000` — Equity orders
  **blocked at the config gate** since 2026-10-01 09:11 (a deliberate
  working-tree edit; HEAD ships `"YES"` and no script writes this key).
  `test_equity_all_strategies_15min.ConfigGateTests.test_the_shipped_config_has_live_orders_enabled`
  fails while the working tree says NO — that tripwire is the test doing its
  job: re-arm the config (`"YES"`) or amend the test when the owner decides.
- `fno/config.json`: `"place_order": "NO"`, `qty: 100` — F&O orders blocked

---

## Dependencies

### Declared
- `skills/fyers-trading/requirements.txt`: `fyers-apiv3>=3.1.7, pandas>=2.0, numpy>=1.24, python-dotenv>=1.0, vectorbt>=0.26, openpyxl>=3.1`
- `strategies/agent/requirements.txt`: `langchain, langchain-openai, langchain-core, openai, requests, pandas, numpy, chromadb` (+ optional twilio/telegram/ollama)

### Actually imported in strategy/scanner/dashboard core
- **stdlib only**: `sqlite3`, `urllib`, `http.server`, `unittest`, `zoneinfo`, `json`, `threading`, `argparse`
- `openpyxl` (Excel ledgers)
- `fyers_apiv3.FyersWebsocket.data_ws` (WebSocket collector only)
- `talib`, `quantstats`, `pandas` (skill scripts, lazily imported)
- `langchain*` (LLM agent only)

**No root `requirements.txt`**. Python target: **3.11**.

---

## Testing

20 unittest suites in `strategies/testing/`:

| Test File | Covers |
|-----------|--------|
| `test_equity_strategy_dashboard.py` (3,247 lines) | Dashboard full coverage |
| `test_equity_all_strategies_15min.py` | Consolidated scanner |
| `test_equity_ema15_10_20_50_crossover.py` | EMA crossover strategy |
| `test_equity_daily_breakout_rsi_volume.py` | Daily breakout |
| `test_ema10_20_signals.py` | EMA10/20 signals |
| `test_double_bottom.py` | Double bottom |
| `test_lower_high_close_signal.py` | Lower high close |
| `test_open_candle_ema_stack.py` | Open candle EMA stack |
| `test_open_range_opening_signals.py` | Opening range |
| `test_orb_low_rejection.py` | ORB low rejection |
| `test_orb_second_candle_gap_ema.py` | ORB second candle |
| `test_r1_prev_high_rejection.py` | R1 rejection |
| `test_second_candle_sell_gate.py` | Second candle sell gate |
| `test_order_config.py` | Order gating safety |
| `test_rule_agent_strategies.py` | Rule agent |
| `test_websocket_nifty_fno100.py` | WebSocket collector |
| `test_fetch_nifty500_above100.py` | Nifty 500 fetcher |
| `test_sync_nifty500_shared15min.py` | Shared-store sync: indicator schema, compat views, prune, `--skip-fetch` |
| `test_fetch_volume_shockers.py` | Volume-shocker screen, DB store, candle reuse (`--reuse-candles`/`--no-reuse-candles`), dashboard view + end-to-end run |
| `test_run.py` | 5-min EMA/RSI scanner |

Run: `python -m pytest strategies/testing/ -v` or `python -m unittest discover strategies/testing/`

---

## Common Tasks

### Start the Dashboard
```bat
cd C:\Users\Admin\fyersAutomation\fyers-skills
strategies\ui\start_equity_strategy_dashboard.bat
```
Then open `http://localhost:9999`.

### Run 15-min Strategies (dry-run)
```bat
cd C:\Users\Admin\fyersAutomation\fyers-skills\strategies\scripts
start_all_15min_strategies.bat
```

### Run 15-min Strategies (live)
```bat
start_all_15min_strategies.bat live
```

### Run EMA Crossover Strategy
```bat
start_equity_ema_crossover_15min_scheduler.bat
```

### Register Scheduled Tasks (as Administrator)
```bat
cd C:\Users\Admin\fyersAutomation\fyers-skills\strategies\scripts
schedule_tasks.bat
```

### Delete Scheduled Tasks
```bat
delete_tasks.bat
```
**Note**: Does NOT delete `FO_RuleAgent`.

### Check FYERS Token
```bat
python skills\fyers-trading\scripts\fyers_login.py --check
```

### Refresh FYERS Token
```bat
python skills\fyers-trading\scripts\fyers_login.py
```

### Run Tests
```bat
cd C:\Users\Admin\fyersAutomation\fyers-skills
python -m pytest strategies\testing\ -v
```

---

## Architecture Notes

1. **Adapter pattern**: `EquityAllStrategies15min.py` does NOT reimplement rules — each `STRATEGY_REGISTRY` entry names the owning script file and calls its function through an adapter, so a rule fix lands in dashboard + agent + scanner at once.
2. **Exactly-once signal ledger**: `matched_signals` table with `UNIQUE(symbol, candle_epoch)` prevents duplicate orders across runs.
3. **Priority data source**: WebSocket DB → API cache DB → live REST API (with rate limiter).
4. **Two-gate safety**: Config JSON `place_order` flag AND CLI `--live` flag both required.
5. **File-locked daily counters**: `order_state/` uses file locks + atomic rename for `max_stocks_per_day`.
6. **Pure-Python indicators**: No TA-Lib dependency for strategies (only in skill scripts).
7. **Per-stock tables**: Each symbol gets its own table rather than one large table.
8. **Session phases**: WebSocket collector knows weekend/pre-open/closed/open, closes at 15:30 IST.
9. **Sells logged but not executed**: The scanner claims/logs sell signals but has no exit order path.
10. **One shared 15-min store**: `strategies/databases/nifty500_15min.db` is the single 15-minute source of truth. fyers-skills writes it (`SyncNifty500Shared15min.py` → candles + indices + `indicators_15m` + `intra_15m_*` views; `FetchVolumeShockers15min.py` → `shockers`/`runs`), and the balutradingapp reads it purely — its `app.py` `INTRADAY_DB_PATH` points here, its `BalutaIntraday15mFetch` 15-min task is disabled (its 5m/yfinance fetchers still run), and its `rsi_buyers` route needed `ema_cross_ts` defaulted at entry init because symbols below the ₹100 price floor have no compat view. The balu app's Flask reloader picks up `app.py` edits automatically (debug=True).

---

## Environment

- **OS**: Windows
- **Python**: 3.11 (`C:\Users\Admin\AppData\Local\Programs\Python\Python311\`)
- **Data Source**: FYERS API v3 (`https://api-t1.fyers.in`), WebSocket + REST
- **Market Hours**: 9:15 AM – 3:30 PM IST (Mon–Fri)
- **Timezone**: Asia/Kolkata (IST)
- **Token**: Cached at `~/.fyers/token.json`, daily OAuth re-login required
- **Working Directory**: `C:\Users\Admin\fyersAutomation\fyers-skills`

---

## Known Issues / Gotchas

1. **Real credentials in `.env.example`**: `skills/fyers-trading/.env.example` contains real app ID, secret, and PIN — contradicts "secrets never committed" rule.
2. **`schtasks /sc minute ... /et` is one-shot**: it only writes an absolute `EndBoundary` for the creation date — the task fires that day and dies (this is how the FO_SharedDataFetcher/FO_OrbStrategy/FO_BuyCall/FO_RuleAgent tasks went dead after 2026-09-15 while showing status "Ready"). Register recurring tasks with `/sc weekly|daily /ri N /du HH:MM`, which produces a `CalendarTrigger` + `Repetition` that re-arms daily/weekly. See the Scheduled Tasks notes.
3. **FYERS history API can return the latest session twice**: `GET /data/history` has been observed duplicating every bar of the most recent session (when `range_to` lands on a day with no data yet). The SQLite stores collapse this on the primary key, but any strategy maths run on the *raw* response will be distorted — `FetchVolumeShockers15min.parse_candles` deduplicates by epoch (last row wins) for this reason; reuse that pattern before screening fetched candles.
4. **Stale docs**: `strategies/config/README.md` references `market_data_agent\agentNifty50.py` which no longer exists (moved to `scripts/`).
5. **Equity config tripwire failing**: the working tree has `equity/config.json` at `place_order: "NO"` (manual edit, 2026-10-01 09:11; HEAD ships `"YES"`), so `test_the_shipped_config_has_live_orders_enabled` fails — a loud, intended statement that live equity trading is currently gated OFF. Also, `strategies/databases/volume_shockers_15min.db` is legacy after the shared-store consolidation (shockers/runs moved into `nifty500_15min.db`; last write to the old file 2026-10-01 15:48) — nothing reads it anymore.
6. **Console window flashing**: Fixed by using `pythonw.exe` for all scheduled/background scripts. Do NOT revert to `python.exe` in `.bat` or `.ps1` schedulers.
