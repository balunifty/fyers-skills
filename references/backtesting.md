# Backtesting with FYERS Data

FYERS provides **historical candles** (`/data/history`); the backtest engine is yours.
The pattern: pull candles → build a DataFrame → run a strategy → measure → (optionally)
forward to paper/live.

## 1. Pull candles into a DataFrame

```python
import pandas as pd
from scripts.fyers_client import FyersClient   # or use the SDK directly

fc = FyersClient()                              # loads cached token
raw = fc.history("NSE:SBIN-EQ", resolution="5",
                 range_from="2024-01-01", range_to="2024-03-31")
df = pd.DataFrame(raw["candles"], columns=["epoch","open","high","low","close","volume"])
df["dt"] = pd.to_datetime(df["epoch"], unit="s", utc=True).dt.tz_convert("Asia/Kolkata")
df = df.set_index("dt")
```

**Chunk long ranges:** minute data is capped at 100 days/request, day/week/month at 366
days, seconds at the last 30 trading days. For longer histories, loop over date windows
and concatenate.

## 2. Run a strategy

Use a real backtest library rather than rolling your own P&L loop:
- **`vectorbt`** (default — in `requirements.txt`) — fast, vectorized, portfolio-level.
- **`backtesting.py`** — simpler, good for single-instrument candle strategies with
  built-in plotting; install on demand (`pip install backtesting`) if the user prefers it.
- **`backtrader`** — feature-rich, event-driven; install on demand if the strategy needs
  bar-by-bar broker simulation instead of vectorized backtesting.

See `references/setup.md` for the full dependency list and install troubleshooting.

Example signal (SMA crossover) without a framework:
```python
df["fast"] = df["close"].rolling(20).mean()
df["slow"] = df["close"].rolling(50).mean()
df["signal"] = (df["fast"] > df["slow"]).astype(int)   # 1 = long, 0 = flat
df["ret"] = df["close"].pct_change().fillna(0)
df["strat_ret"] = df["signal"].shift(1) * df["ret"]    # shift(1) avoids look-ahead
equity = (1 + df["strat_ret"]).cumprod()
```

For signals beyond simple SMA crossovers (RSI, MACD, Bollinger Bands, ATR-based stops,
etc.), use `scripts/indicators.py` (TA-Lib wrappers) instead of hand-rolling more
`rolling()` math — see `references/indicators.md`.

## 3. Model costs honestly

A backtest that ignores costs lies. Apply per-executed-order brokerage and statutory
charges:
- Equity delivery: ~₹20/order (or 0).
- Equity intraday & F&O: ₹20 per executed order or 0.03%, whichever is lower.
- Plus STT, exchange txn charges, GST, SEBI fees, stamp duty.

Subtract estimated cost per trade from returns, and add realistic **slippage** (e.g. a
tick or a few bps) on entries/exits.

## Pitfalls (call these out in generated code)

- **Look-ahead bias:** decide on bar *t*, trade on bar *t+1* open. Use `.shift(1)` on signals.
- **Survivorship bias:** delisted symbols won't be in today's master file.
- **Partial last candle:** the most recent candle may be incomplete — drop it for backtests.
- **Incomplete current trading day:** for day-bounded intraday strategies (enter on a
  signal, exit at a fixed time like 15:20), if the backtest window runs through "today"
  and that exit time hasn't happened yet, there's no real exit fill. Treating the last
  available bar as the exit fabricates a trade that never closed. Detect this (no candle
  at/after the exit time) and mark the position `open`/mark-to-market — exclude it from
  win-rate and total P&L, don't silently report it as a closed trade. (This trade-level win
  rate is distinct from QuantStats' positive-period % — see the "Two win rates" note in
  `references/quantstats.md`; never conflate them.)
- **Timezone:** FYERS epochs are UTC; convert to `Asia/Kolkata` for session logic.
- **Corporate actions:** raw candles aren't always split/bonus-adjusted — verify for equities.
- **Overfitting:** validate out-of-sample; a curve fit to one period isn't a strategy.

## 4. From backtest to execution

Once a strategy is validated, wire signals to `references/orders.md` — but keep
`DRY_RUN=True` until the user explicitly opts into live trading, and paper-trade first.
`scripts/example_strategy.py` shows the full data → signal → dry-run-order skeleton.

To **visualize and report** a validated strategy's performance — a professional HTML tear
sheet with Sharpe/Sortino/drawdown/CAGR and a monthly-returns heatmap from the `strat_ret`
returns series above — see `references/quantstats.md` and `scripts/quantstats_report.py`.
