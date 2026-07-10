# Performance Tear Sheets with QuantStats

`scripts/quantstats_report.py` wraps [**QuantStats**](https://github.com/ranaroussi/quantstats) —
a portfolio-analytics and visualization library. It is the **reporting / visualization
layer** that plugs in *after* a backtest: feed it the strategy's returns series (the
`df["strat_ret"]` produced in `references/backtesting.md`) and it renders a professional
HTML tear sheet — annualized Sharpe/Sortino, drawdown curve, monthly-returns heatmap,
rolling stats, value-at-risk — plus a small dict of headline risk metrics for chat.

Use it instead of hand-plotting an equity curve: QuantStats computes the risk-adjusted
metrics serious traders actually read, and produces one standalone HTML file you can hand
to the user.

## Install (read this before importing `scripts/quantstats_report.py`)

QuantStats is a heavy dependency — `pip install quantstats` pulls in
pandas/numpy/scipy/**matplotlib**/seaborn/tabulate. It's not in the default
`requirements.txt`; install it on demand (same pattern as the TA-Lib row in
`references/setup.md`):

```bash
pip install quantstats
```

If the install fails (compiled deps like scipy/matplotlib occasionally lack a wheel for the
local Python/OS), follow the generic troubleshooting in `references/setup.md` §3 — install
the rest first, retry the failing package alone, read the actual pip error.

**Headless servers (no display):** matplotlib defaults to an interactive backend that
crashes without a display. `scripts/quantstats_report.py` forces the non-interactive **Agg**
backend (`matplotlib.use("Agg")`) lazily, only when it actually draws a plot or HTML, so the
module imports cleanly and plotting works over SSH / in CI. You don't need to set anything.

`scripts/quantstats_report.py` imports `quantstats` **lazily inside `_quantstats()`**, so the
rest of the skill's scripts still work with QuantStats absent; you only hit the install
requirement when you actually generate a report.

## Feeding FYERS backtest results in

QuantStats consumes a **returns series** — a pandas Series of periodic returns indexed by
datetime — NOT prices and NOT a list of trades. Take the SMA-crossover example from
`references/backtesting.md` (which ends with `df["strat_ret"]` and `equity`) and hand
`strat_ret` straight in:

```python
import pandas as pd
from scripts.fyers_client import FyersClient
from scripts.quantstats_report import daily_returns, html_report, key_metrics

fc = FyersClient()
raw = fc.history("NSE:SBIN-EQ", resolution="5",
                 range_from="2024-01-01", range_to="2024-03-31")
df = pd.DataFrame(raw["candles"], columns=["epoch","open","high","low","close","volume"])
df["dt"] = pd.to_datetime(df["epoch"], unit="s", utc=True).dt.tz_convert("Asia/Kolkata")
df = df.set_index("dt")

# ... SMA-crossover signal from references/backtesting.md produces df["strat_ret"] ...
df["fast"] = df["close"].rolling(20).mean()
df["slow"] = df["close"].rolling(50).mean()
df["signal"] = (df["fast"] > df["slow"]).astype(int)
df["ret"] = df["close"].pct_change().fillna(0)
df["strat_ret"] = df["signal"].shift(1) * df["ret"]

rets = daily_returns(df["strat_ret"])            # intraday 5-min -> daily, IST index
print(key_metrics(rets))
html_report(rets, output="strategies/sma_crossover/report.html",
            title="SMA crossover — NSE:SBIN-EQ")
```

### Resample intraday → daily first (this is the critical step)

FYERS candles are often intraday (5-min, UTC epochs). QuantStats' time-based metrics
(annualized Sharpe, CAGR, rolling stats, monthly heatmap) assume returns at a **regular
periodic frequency** — typically daily. Feeding raw 5-min returns straight in produces
nonsense annualization (it thinks each 5-min bar is one "period"). `daily_returns()`
collapses the intraday series to one **compounded** return per day and stamps an
`Asia/Kolkata` DatetimeIndex:

```python
rets = daily_returns(df["strat_ret"])            # freq="auto" -> resample intraday to daily
rets_already_daily = daily_returns(daily_series, freq="D")   # skip resampling
equity_input = daily_returns(equity_curve, equity=True)      # differences an equity curve
```

Compounding within each day is `(1 + r).prod() - 1`. `daily_returns()` also handles the
UTC-epoch → IST conversion (matching `references/backtesting.md` §1:
`pd.to_datetime(epoch, unit="s", utc=True).tz_convert("Asia/Kolkata")`) when handed
epoch-indexed data.

## Benchmark comparison (pull it through FYERS, not yfinance)

QuantStats can download a benchmark itself via yfinance — **don't use that for Indian
markets**, its NSE/BSE coverage is poor/unreliable. Pull the benchmark's candles through the
skill's own `scripts/fyers_client.py history()` (e.g. `NSE:NIFTY50-INDEX`), turn them into a
daily returns Series, and pass it as `benchmark=`:

```python
braw = fc.history("NSE:NIFTY50-INDEX", resolution="D",
                  range_from="2024-01-01", range_to="2024-03-31")
bdf = pd.DataFrame(braw["candles"], columns=["epoch","open","high","low","close","volume"])
bench = daily_returns(bdf.set_index(
    pd.to_datetime(bdf["epoch"], unit="s", utc=True).tz_convert("Asia/Kolkata")
)["close"], equity=True)                          # close -> daily returns

html_report(rets, output="report.html", benchmark=bench,
            title="SMA crossover vs NIFTY50")
```

## Two win rates, and why they differ

A backtest and QuantStats each report a "win rate", and they are **different numbers by
design** — reconcile them for the user, never conflate them:

- **Trade-level win rate** = winning trades / total trades. It comes from the **backtest
  engine**, because only the backtest loop knows what a "trade" is (an entry paired with an
  exit). This is what most traders mean by "win rate".
- **Positive-period %** = fraction of positive *periods* (e.g. positive days). It comes from
  **QuantStats**, which marks the strategy to market each bar and counts positive bars. A
  single multi-day winning trade contributes several positive daily periods; a losing trade
  spans several negative ones. This module exposes it as `positive_period_pct` and
  **never** labels it `win_rate`, precisely to avoid colliding with the trade-level figure.

Division of labor: **trade-level stats** (win rate, expectancy, profit factor, avg hold)
come from the backtest engine; **QuantStats owns the time-series risk metrics and
visualization**. Don't ask QuantStats to compute trade stats — it doesn't see trades.

**Serious-trader caveat:** win rate alone is near-meaningless and must always be paired with
**payoff ratio / expectancy**. A 30% win rate with 5:1 winners beats an 80% win rate with
1:8 losers. When you report either win rate, report the payoff ratio alongside it.

## What the script exposes (`scripts/quantstats_report.py`)

| Function | What it does |
|---|---|
| `daily_returns(data, freq="auto", equity=False)` | Build a clean **daily** IST-indexed returns Series from a returns series/array or (with `equity=True`) an equity curve; resamples intraday→daily by default, `freq="D"` skips it. |
| `html_report(returns, output, benchmark=None, title=..., rf=0.0)` | **Default path** — write a full standalone HTML tear sheet to `output` and return the path. |
| `metrics(returns, benchmark=None, mode="full", rf=0.0)` | Return QuantStats' full metrics table as a DataFrame (`mode="basic"|"full"`). |
| `key_metrics(returns, rf=0.0)` | Small dict of headline risk metrics for chat (see below); win rate relabeled `positive_period_pct`. |
| `plots(returns, output_dir=".", benchmark=None)` | Save individual PNG charts (snapshot, drawdown, monthly heatmap); returns the file paths. With `benchmark=`, also writes `returns.png` (cumulative strategy vs benchmark) and `active_heatmap.png` (monthly active returns). `snapshot`/`drawdown` have no benchmark parameter and stay strategy-only. |

Inputs are validated with plain `ValueError`s (empty series, all-NaN, too few points for
annualization) instead of a cryptic library error.

## Headline metrics QuantStats computes

| Metric | One-line meaning |
|---|---|
| Sharpe | Annualized excess return per unit of total volatility (risk-adjusted return). |
| Sortino | Like Sharpe but only penalizes *downside* volatility. |
| Calmar | CAGR divided by max drawdown — return per unit of worst-case pain. |
| Max drawdown | Largest peak-to-trough equity decline (as a %). |
| CAGR | Compound annual growth rate of the equity curve. |
| Volatility (ann.) | Annualized standard deviation of returns. |
| VaR / CVaR | Value-at-Risk: the loss threshold at a confidence level; CVaR = average loss beyond it. |
| Ulcer index | Drawdown-based risk measure — depth *and* duration of underwater periods. |
| Positive-period % | Fraction of positive periods (days). **Not** the trade-level win rate — see above. |

`key_metrics()` returns the subset most useful for a quick chat summary:
`sharpe`, `sortino`, `max_drawdown`, `cagr`, `calmar`, `volatility_ann`, `positive_period_pct`.

## Notes on inputs

- Input must be a **returns Series indexed by datetime** at a regular frequency — run it
  through `daily_returns()` first. Per-trade returns (one value per closed trade) are **not**
  a time series and will annualize wrong; use bar/daily marked-to-market returns.
- `rf` is the **annual** risk-free rate as a decimal (e.g. `0.07` for 7%).
- Plots/HTML render matplotlib figures; on a headless box the module already forces the Agg
  backend. A `findfont: Font family 'Arial' not found` message is a harmless matplotlib
  warning, not an error.

## Smoke test without a token

```bash
python scripts/quantstats_report.py demo
```
Builds a synthetic ~2-year daily returns series (no FYERS token or network call needed),
prints the headline `key_metrics`, and writes an HTML tear sheet to a temp path — use it to
confirm QuantStats is installed and the headless Agg backend works before wiring it into a
real strategy's reporting.
