# The optimization loop

Supercharge Mode runs in **iterative cycles**. Each cycle (a "round") takes the current
strategy state, runs the council through six phases, and produces backtested variants plus a
recorded decision. The system pauses for the user every **three rounds** (`checkpoints.md`).

The orchestrator (the host agent) drives this; the deterministic parts — scoring, ranking,
persistence — are delegated to `scorecard.py`, `dashboard.py`, and `evolution_log.py`.

> **The baseline is sacred.** The original strategy is captured once, scored, and never mutated.
> Every variant is measured against it (and against the current best variant). See `variants.md`.

## Round lifecycle — the six phases

### Phase 1 — Understand
Every seat studies the strategy and its evidence: entry logic, exit logic, position sizing,
capital usage, instrument selection, timeframe, and the backtest output (per-trade list, equity
curve, returns series, risk metrics, market conditions). Capture the baseline scorecard first
(`scorecard.py`) so everything downstream is measured against it.

### Phase 2 — Diagnose
Each seat, from its domain, identifies: strengths, weaknesses, bottlenecks, hidden risks, missed
opportunities, unstated assumptions, and the specific metrics that most need improvement for the
active objective.

### Phase 3 — Debate
Seats openly challenge one another — the disagreement is the point. Typical clashes:
- Risk Strategist recommends tighter stops → **Return Strategist** argues profits will decline.
- **Execution Strategist** flags that tighter stops increase slippage/whipsaw at the tick level.
- **Simplicity & Robustness Strategist** warns an added filter is unjustified complexity.

The orchestrator resolves disagreements via the **consensus rule** (below) and converts the
surviving proposals into concrete experiments. Every debate outcome — including the major
disagreements and whether they were resolved — is recorded with `evolution_log.log_decision()`.

### Phase 4 — Generate variants
The Variant Generation Strategist turns consensus experiments into candidate strategies per the
contract in `variants.md`. **Each variant changes only a limited number of variables** so the
resulting improvement is attributable and measurable. Each variant declares: objective, rules
changed, rules preserved, expected benefit, expected downside, success metrics.

### Phase 5 — Backtest
Every variant is backtested under **identical assumptions** to the baseline — same data window,
same costs/slippage model, same capital — via `fyers-trading`'s documented backtest path
(`references/backtesting.md`). Each variant is then scored with `scorecard.py` (which reuses
`fyers-trading`'s `quantstats_report.key_metrics()`), and `dashboard.py` compares it against:
the original strategy, the previous best variant, and all active variants.

### Phase 6 — Learn
The council reviews results: which changes worked, which failed, any unexpected behavior, and new
hypotheses. Accepted/rejected variants and the round's leader are recorded
(`evolution_log.log_variant()` / `log_round()`). The strongest ideas seed the next round.

## Consensus rule (default)

This converts debated proposals into experiments. **No single agent decides.**

1. **Independent proposals** — each seat proposes changes, each tied to a metric in `metrics.md`
   with an expected delta. Proposals with no measurable target don't enter the debate.
2. **Cross-critique** — every seat reviews every other seat's proposals: agree, object (with
   reason), or veto (only where its mandate allows — see `council.md`).
3. **Revision** — a proposer may revise once to address critique. A veto must be resolved (amend
   to clear it) or the proposal is dropped.
4. **Aggregation** — a proposal becomes an experiment when it has **no unresolved veto** and a
   **majority of contributing seats agree**. Ties / unresolved conflicts are **not** silently
   decided — they are logged as dissents and surfaced at the next checkpoint for the user.

> This majority-plus-veto rule is the default. It can be replaced (e.g. weighted vote, or
> orchestrator-synthesizes-with-justification) **without changing any phase** — only this section.

## What each round persists (via `evolution_log.py`)

- `log_round(round_no, objective, leader, notes)` — the round header + current leader.
- `log_decision(round_no, summary, disagreements, resolved)` — the debate outcome.
- `log_variant(round_no, variant_id, scorecard, decision, rules_changed, ..., reason)` — one per
  variant, with the standardized scorecard and WHY it was accepted/rejected/parked.

This append-only journal is the single source of truth the final deliverables are built from
(`deliverables.md`) and what the checkpoint report rolls up (`checkpoints.md`).
