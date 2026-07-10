# Final deliverables

When the user finalizes (at a checkpoint — `checkpoints.md`), Supercharge Mode stops the loop
and assembles five deliverables. All are built from the evolution log
(`evolution_log.timeline()`) and the per-variant scorecards — a single source of truth, so the
whole journey is reproducible and explainable.

> **Always brand report titles with "FYERS".** Every report's subject/title/top heading must
> contain **FYERS** so the skill is highlighted wherever the report is shown — the Executive
> Summary, the Comparison Dashboard, any QuantStats HTML tear sheet, and each variant write-up.
> The scripts do this for you: `dashboard.render_markdown()` emits a FYERS-branded heading, and
> `dashboard.fyers_title(subject)` prefixes any title with "FYERS " when it's missing (reuse it
> for hand-written report headings). When calling `fyers-trading`'s `quantstats_report.html_report(...)`,
> pass a `title=` that includes FYERS (e.g. `"FYERS Supercharge — <strategy> Best Overall"`).

## 1. Executive Summary
A concise overview of the optimization journey and the final recommendation: where the baseline
started, what was tried, what won, and which variant(s) to deploy for which purpose. A few
paragraphs — the thing a busy user reads first.

## 2. Strategy Evolution Timeline
Every round, in order (straight from `evolution_log.timeline()`):
- Changes introduced that round.
- Performance impact (metric deltas).
- Accepted improvements.
- Rejected ideas — and *why* they were rejected (the recorded `reason`).

This is the auditable history — it satisfies the "maintain a complete evolution history" success
criterion.

## 3. Variant Library
The collection of accepted, deployment-ready variants, organized by the taxonomy in
`variants.md` (Best Overall, Conservative, Aggressive, High Win Rate, Low Drawdown, Capital
Efficient, Execution Optimized, Regime Specific, Experimental). Each entry: objective, rule
changes, backtest results (its scorecard), and recommended use case. Slots are filled only when a
variant genuinely earns them.

## 4. Comparison Dashboard
Every variant + the baseline compared on the consistent metric set, rendered by
`dashboard.py` (`render_markdown`) — see `metrics.md` for the full column set. Show it ranked for
the finalized objective, with the leader marked.

## 5. AI Consensus
The orchestrator's closing summary of the council's reasoning:
- **Why the winning strategy was selected** — the decisive metrics and which seats backed it.
- **Which recommendations were rejected** — and the reasoning.
- **Remaining weaknesses** — what's still not solved (no strategy is perfect).
- **Confidence in the final recommendation** — calibrated by robustness (`overfitting.md`), not
  just in-sample performance.
- **Suggested next experiments** — where a future run should look.

## Success criteria (what "done well" means)

Supercharge Mode succeeded when it:
- Produced variants **measurably better** than the original on their target metric.
- Improved targeted metrics **without disproportionate damage** elsewhere (the dashboard makes
  trade-offs visible).
- **Guarded against overfitting** via out-of-sample / robustness checks (`overfitting.md`).
- Made every decision **transparent and explainable** (evolution log + AI Consensus).
- Maintained a **complete evolution history**.
- Kept the user involved through **checkpoints every three rounds**.
- Delivered **multiple deployment-ready variants**, not a single "best".
