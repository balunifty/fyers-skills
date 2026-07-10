# The Strategist Council — roster

The orchestrator spawns one subagent per seat (via the `Agent` tool). Each strategist analyzes
the strategy **only** from its own domain, proposes measurable improvements (each tied to a metric
in `metrics.md` with an expected delta), and critiques other seats' proposals during the debate
(`optimization-loop.md`). **No seat decides alone** — proposals become experiments only through
the consensus rule.

For each seat: **Domain** (what it studies), **Mandate** (what it proposes), **Owned metrics**
(what it is judged on), **Veto** (what, if anything, it can block).

> **Extensible by design (PRD).** The roster below is the starting council, "including but not
> limited to" these seats. New strategist seats can be added over time **without changing the
> orchestration flow** — the 6-phase loop, the debate, and the scorecard/dashboard contracts are
> seat-agnostic. To add a seat, append an entry here with the same four fields; the orchestrator
> spawns whatever seats this file lists.

## Risk Strategist

- **Domain:** drawdown, tail risk, position sizing, leverage, exposure/concentration.
- **Mandate:** sizing and stop/exposure rules that cap ruin risk without killing edge.
- **Owned metrics:** max_drawdown, volatility_ann, largest_loss, longest_losing_streak.
- **Veto:** ruin-risk — can block a proposal that materially raises probability of blow-up.

## Return Strategist

- **Domain:** profitability — net profit, return %, capital growth.
- **Mandate:** grow returns; argue against changes that quietly sacrifice profit for optics.
- **Owned metrics:** net_profit, return_pct, cagr, profit_factor.
- **Veto:** none (advisory).

## Entry Precision Strategist

- **Domain:** entry logic — signal timing, confirmation, false-signal filtering.
- **Mandate:** sharpen entries (better fills, fewer whipsaws) without adding fragile complexity.
- **Owned metrics:** win_rate (trade-level), avg_profit, trade_count quality.
- **Veto:** none (advisory).

## Exit Intelligence Strategist

- **Domain:** exit logic — stops, targets, trailing rules, time-based exits.
- **Mandate:** improve how trades are closed (let winners run, cut losers) — the other half of edge.
- **Owned metrics:** avg_profit, avg_loss, profit_factor, largest_loss.
- **Veto:** none (advisory).

## Market Regime Strategist

- **Domain:** behavior across regimes (trend/range, high/low vol, event days, sessions).
- **Mandate:** expose regime dependence; propose regime filters / robustness improvements.
- **Owned metrics:** per-regime return/Sharpe, consistency, robustness_score.
- **Veto:** none (advisory) — but its regime evidence often triggers the Integrity veto.

## Backtest Integrity Strategist

- **Domain:** validity of the evidence — look-ahead, survivorship, overfitting, data-snooping.
- **Mandate:** ensure every claimed improvement is real, not a backtest artifact. Owns the
  guardrails in `overfitting.md`.
- **Owned metrics:** robustness_score, in-vs-out-of-sample gap, parameter sensitivity.
- **Veto:** invalid evidence — can block a proposal whose backtest support is unsound or overfit.

## Execution Strategist

- **Domain:** real-world tradability — slippage, costs, fills, order types, rate-limit cadence.
- **Mandate:** keep the strategy executable under FYERS limits (10/s · 200/min · 100k/day) and
  honest about costs. Bridges into `fyers-trading`'s orders/rate-limits references.
- **Owned metrics:** execution_risk, net-of-cost return, turnover, orders/min feasibility.
- **Veto:** infeasibility — can block a proposal that can't be executed within broker/rate limits.

## Options Strategist

- **Domain:** options-specific structure — strike/expiry selection, greeks, IV, spreads, decay.
- **Mandate:** for options strategies, improve structure (defined risk, theta/vega posture). For
  non-options strategies this seat is dormant (contributes nothing rather than forcing options in).
- **Owned metrics:** strategy-dependent (margin efficiency, payoff asymmetry, IV sensitivity).
- **Veto:** none (advisory).

## Capital Efficiency Strategist

- **Domain:** how hard the capital works — utilization, margin, idle cash, return on capital.
- **Mandate:** improve return per unit of deployed capital/margin, not just absolute return.
- **Owned metrics:** return_on_capital, capital_utilization.
- **Veto:** none (advisory).

## Simplicity & Robustness Strategist

- **Domain:** parsimony — number of parameters/rules, complexity vs. benefit.
- **Mandate:** resist unnecessary complexity; favor changes that survive out-of-sample. Guards
  against the classic "one more parameter" overfit.
- **Owned metrics:** parameter count, robustness_score, complexity penalty.
- **Veto:** complexity — can block a proposal whose added complexity isn't justified by a robust,
  out-of-sample improvement.

## Variant Generation Strategist

- **Domain:** turning debated consensus into concrete candidate strategies.
- **Mandate:** author the variants (per the contract in `variants.md`), changing only a limited
  number of variables per variant so each improvement stays measurable.
- **Owned metrics:** none directly (produces the variants others score).
- **Veto:** none.

## Performance Ranking Strategist

- **Domain:** comparing candidates fairly on the standardized scorecard.
- **Mandate:** run `scorecard.py`/`dashboard.py`, rank variants for the active objective, and
  surface the leader + trade-offs. Keeps the comparison apples-to-apples.
- **Owned metrics:** owns the dashboard, not a single metric.
- **Veto:** none.

---

## Consensus & vetoes (summary — full rule in `optimization-loop.md`)

A proposal becomes an experiment when it draws **no unresolved veto** and a **majority of
contributing seats agree**. Veto-holding seats (Risk, Backtest Integrity, Execution, Simplicity &
Robustness) can block within their mandate; a veto must be resolved (proposal amended) or the
proposal is dropped. Unresolved conflicts are **not** silently decided — they are recorded as
dissents (`evolution_log.log_decision`) and surfaced at the next checkpoint. *(This is the default
rule; it can be swapped for a different aggregation without changing the loop.)*
