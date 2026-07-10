# Variants — contract, discipline, and the Variant Library

A variant is a candidate strategy derived from the baseline to pursue **one** objective. Pro
Supercharge Mode produces *many* variants, not a single "best" — the deliverable is a library of
deployment-ready alternatives (below).

## The variant contract (declared before backtesting)

Every variant declares, up front:

- **Objective** — the one thing it optimizes (higher returns, lower drawdown, better
  risk-adjusted, higher win rate, better capital efficiency, better execution, better robustness).
- **Rules changed** — the specific rules/parameters altered from the baseline.
- **Rules preserved** — what is deliberately held constant (so the change is attributable).
- **Expected benefit** — the metric expected to improve, and by roughly how much.
- **Expected downside** — the metric expected to worsen (there is almost always a trade-off).
- **Success metrics** — the scorecard fields that decide whether it worked.

**Change only a limited number of variables per variant.** One conceptual change at a time (e.g.
"tighten the stop" OR "add a trend filter", not both) so Phase 6 can attribute the result. A
variant that changes ten things at once teaches nothing even if it wins.

## The strategy interface every variant must expose

`fyers-trading` documents a per-bar returns series (`df["strat_ret"]`) as the hand-off into the
metrics layer, but it defines **no formal runner interface** — so Supercharge Mode imposes a
thin one so `scorecard.py` can score every variant identically:

A variant is a self-contained runner that, over the shared backtest window, produces:
1. **`strat_ret`** — a per-bar returns Series (pandas), exactly as in
   `fyers-trading/references/backtesting.md` §2 (`signal.shift(1) * ret`, costs subtracted). This
   feeds the risk metrics (Sharpe/Sortino/drawdown/CAGR/…).
2. **`trades`** — a list of closed-trade dicts, each with at least `{"pnl": <float in currency>}`
   (plus optional `entry`/`exit`/`side`/`qty`). This feeds the **trade-level** metrics QuantStats
   structurally cannot compute (win rate, profit factor, avg win/loss, largest loss, streaks).

Score it with:
```python
from scorecard import scorecard
# freq="auto" resamples intraday bar returns down to daily before scoring (matching the baseline);
# pass freq="D" only if strat_ret is already daily. Use the SAME freq for every variant + baseline.
card = scorecard(variant_df["strat_ret"], trades=variant_trades, capital=CAPITAL, rf=0.07)
```

`scorecard()` normalizes the returns with `fyers-trading`'s `daily_returns()` **before**
computing risk metrics — exactly as `fyers-trading/references/backtesting.md` does — so an
intraday `strat_ret` series is annualized correctly rather than producing a bogus Sharpe/drawdown.

Both artifacts must come from the **same** data window, cost model, and capital as the baseline
(Phase 5's "identical assumptions"). Do not compare a variant scored on 2023 to a baseline scored
on 2022–2024.

## Where variants live

Under the strategy folder, isolated from the baseline so the baseline stays intact:

```
strategies/<name>/
  ...                         # the original strategy (baseline) — never modified
  supercharge/
    evolution.jsonl           # the evolution log (evolution_log.py)
    variants/
      v1_tighter_stop/        # one folder per variant: runner + config + scorecard.json
      v2_trend_filter/
      ...
```

## The Variant Library (final deliverable taxonomy)

When optimization concludes, the accepted variants are organized into this library. A slot is
filled only if a variant genuinely earns it — do not fabricate a variant to fill a slot.

- **Best Overall** — best risk-adjusted performance across the board.
- **Conservative** — lowest risk / smoothest equity, modest returns.
- **Aggressive** — highest returns, accepts higher drawdown.
- **High Win Rate** — highest trade-level win rate (may have smaller average wins).
- **Low Drawdown** — smallest max drawdown.
- **Capital Efficient** — best return on deployed capital / margin.
- **Execution Optimized** — lowest execution risk / most realistic to trade live.
- **Regime Specific** — tuned for a particular regime (e.g. trending markets); note the regime.
- **Experimental** — promising but not yet robust; flagged as higher-risk / needs more validation.

Each library entry includes its objective, rule changes, backtest results (its scorecard), and a
**recommended use case**. All are compared on the same metrics via the Comparison Dashboard
(`metrics.md`, `dashboard.py`).
