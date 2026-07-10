# Overfitting & robustness guardrails

The optimization loop searches hard for improvements — which is exactly how overfitting creeps
in. These guardrails, owned by the **Backtest Integrity** and **Simplicity & Robustness** seats,
keep "better" honest. They back those seats' vetoes and the `robustness_score` in the scorecard.

## The core discipline

**An improvement that only exists in-sample is not an improvement.** Every accepted variant must
show its edge survives data it wasn't tuned on. A curve fit to one period is not a strategy.

## Guardrails

1. **Out-of-sample / walk-forward split.** Reserve a tail of the data the council never tunes on.
   `scorecard.robustness_score()` splits the returns chronologically (default: last 30%
   out-of-sample) and compares in-sample vs. out-of-sample Sharpe — score near 1 = edge holds,
   near 0 = edge vanishes out-of-sample. Prefer walk-forward (roll the split) for strategies with
   enough data.
2. **Parameter-sensitivity check.** A robust rule degrades *gracefully* as its parameters move. If
   a variant only works at exactly `stop = 1.2%` and collapses at 1.0% or 1.4%, it's fit to noise.
   Test a small neighborhood around any tuned parameter.
3. **Complexity penalty.** Each added rule/parameter must earn its place with a robust,
   out-of-sample gain. The Simplicity & Robustness seat can veto complexity that isn't justified —
   prefer the simpler variant when two perform comparably.
4. **Deflated-Sharpe caution / trial counting.** The more variants you test, the more likely one
   looks great by luck. Track how many variants were tried (the evolution log does this) and treat
   a lone standout among many trials with suspicion — favor improvements that repeat across rounds.
5. **Identical assumptions.** Same data window, cost model, slippage, and capital for every
   candidate (Phase 5). A "win" from a quietly-easier assumption is not a win.
6. **Enough trades.** Metrics from a handful of trades aren't reliable. Weight confidence by
   `trade_count`; a high win rate over 6 trades means little.

## How this shows up in the flow

- The **Robustness Score** column in the dashboard (`metrics.md`) surfaces guardrail #1 on every
  candidate.
- The Backtest Integrity seat can **veto** a proposal whose evidence is unsound (look-ahead,
  survivorship, overfit) — see `optimization-loop.md`'s consensus rule.
- The **AI Consensus** deliverable calibrates its confidence by robustness, not raw in-sample
  performance (`deliverables.md`).

Also carry over `fyers-trading/references/backtesting.md`'s pitfalls (look-ahead via `.shift(1)`,
survivorship, partial last candle, timezone, corporate actions) — those are backtest-correctness
bugs that masquerade as edge.
