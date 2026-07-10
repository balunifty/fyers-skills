# Metrics & the Comparison Dashboard

Every variant — and the immutable baseline — is scored on the **same** metric set so comparisons
are apples-to-apples. `scorecard.py` produces one uniform dict per candidate; `dashboard.py` ranks
them for the active objective. "Improvement" always means a quantified delta versus the baseline
(and versus the current best variant), never a qualitative opinion.

## Where each number comes from

Two sources, deliberately kept separate:

- **Risk metrics — from `fyers-trading`'s `quantstats_report.key_metrics()`** (the same code that
  scored the baseline): `sharpe`, `sortino`, `max_drawdown`, `cagr`, `calmar`, `volatility_ann`,
  `positive_period_pct`. Computed off the per-bar returns series (`strat_ret`).
- **Trade-level metrics — computed by `scorecard.py` from the trades list** (QuantStats never sees
  "trades"): `net_profit`, `return_pct`, `profit_factor`, `win_rate`, `avg_profit`, `avg_loss`,
  `trade_count`, `largest_loss`, `longest_losing_streak`, `return_on_capital`.
- **Caller-supplied estimates** (not derivable from returns alone; the relevant seat provides
  them, else `None`): `capital_utilization` (Capital Efficiency seat), `execution_risk`
  (Execution seat).
- **Derived by `scorecard.py`:** `robustness_score` (in/out-of-sample Sharpe stability — see
  `overfitting.md`).

> If `fyers-trading` / QuantStats isn't installed, `scorecard.py` falls back to a stdlib
> computation of Sharpe/Sortino/max-drawdown/vol so the loop still runs, but full-fidelity scoring
> (CAGR, Calmar, the tear sheet) needs `fyers-trading` present. Install both skills together.

## ⚠️ Two win rates — never conflate

- **`win_rate`** — TRADE-LEVEL: winning trades / total trades. This is the intuitive "win rate".
- **`positive_period_pct`** — QuantStats' % of positive PERIODS (e.g. positive days).

They answer different questions and will differ. The dashboard reports both under distinct
columns; never relabel `positive_period_pct` as "win rate" (mirrors
`fyers-trading/references/quantstats.md`).

## The Comparison Dashboard metric set (PRD)

| Metric | Scorecard key | Source |
|---|---|---|
| Net Profit | `net_profit` | trades |
| Return % | `return_pct` | trades + capital |
| Profit Factor | `profit_factor` | trades (gross profit / gross loss) |
| Sharpe Ratio | `sharpe` | quantstats |
| Sortino Ratio | `sortino` | quantstats |
| Win Rate (trade-level) | `win_rate` | trades |
| Average Profit | `avg_profit` | trades (mean winning trade) |
| Average Loss | `avg_loss` | trades (mean losing trade) |
| Maximum Drawdown | `max_drawdown` | quantstats |
| Trade Count | `trade_count` | trades |
| Return on Capital | `return_on_capital` | trades + capital |
| Capital Utilization | `capital_utilization` | caller (Capital Efficiency seat) |
| Largest Loss | `largest_loss` | trades |
| Longest Losing Streak | `longest_losing_streak` | trades |
| Execution Risk | `execution_risk` | caller (Execution seat) |
| Robustness Score | `robustness_score` | derived (in/out-of-sample) |

Also carried for context: `cagr`, `calmar`, `volatility_ann`, `positive_period_pct`.

## Objectives → ranking metric

`dashboard.py` ranks on one metric per objective (the checkpoint "focus" menu maps onto these):

| Objective | Ranks by | Direction |
|---|---|---|
| `risk_adjusted` (default) | `sharpe` | higher |
| `higher_returns` | `return_pct` | higher |
| `low_drawdown` | `max_drawdown` | higher (closer to 0) |
| `high_win_rate` | `win_rate` | higher |
| `consistency` | `sortino` | higher |
| `capital_eff` | `return_on_capital` | higher |
| `execution` | `execution_risk` | lower |
| `robustness` | `robustness_score` | higher |

## Reporting a proposal's improvement

State it as a delta: *metric → baseline value → variant value → basis (the backtest run)*. A
proposal that can't be expressed this way doesn't enter the debate (`optimization-loop.md`).
Guard against improving one metric while quietly wrecking another — the dashboard shows the whole
row so trade-offs are visible, which is a success criterion of the mode.
