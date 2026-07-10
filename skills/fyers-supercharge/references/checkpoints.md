# User checkpoints — every three rounds

Supercharge Mode **pauses after every three optimization rounds**. It never continues silently:
the user stays in control of direction. The orchestrator assembles the report from the evolution
log (`evolution_log.checkpoint_report(round_no)`) and presents it, then waits for the user's choice
before starting the next three rounds.

## The optimization report (what the pause shows)

Built from the last three rounds via `evolution_log.checkpoint_report()`, plus the current
dashboard (`dashboard.py`):

- **Improvements achieved** — metrics that moved vs. baseline and vs. the previous leader.
- **Variants created** — every candidate produced this window (`variants_created`).
- **Variants discarded** — and *why* (`variants_discarded`, with the recorded `reason`).
- **Metrics that improved** — with deltas.
- **Metrics that worsened** — trade-offs made, stated honestly.
- **Major disagreements** — the notable council conflicts on record (`major_disagreements`).
- **Current leading strategy** — the present best variant (`current_leader`) + its scorecard.
- **Recommended next optimization direction** — the council's suggestion, with rationale.

## The user's choices

At the checkpoint the user picks one (recorded via `evolution_log.log_checkpoint(..., user_choice)`):

| Choice | Effect on the next rounds |
|---|---|
| Continue optimization | Keep the current objective. |
| Focus on reducing drawdown | Objective → `low_drawdown`. |
| Focus on increasing returns | Objective → `higher_returns`. |
| Focus on improving consistency | Objective → `consistency` (Sortino / steadier equity). |
| Focus on increasing trade frequency | Objective → more trades (Entry Precision seat leads). |
| Focus on execution quality | Objective → `execution` (Execution seat leads). |
| Finalize current best variants | Stop the loop → assemble final deliverables (`deliverables.md`). |

The chosen objective sets the dashboard ranking metric (`metrics.md`) and which seats lead the
next debate. The loop then runs three more rounds and pauses again — until the user finalizes.

## Orchestrator responsibilities at a checkpoint

1. Generate the report data (`checkpoint_report`) and render the current dashboard.
2. Present it plainly in chat — improvements, trade-offs, disagreements, leader, recommendation.
3. **Wait for an explicit choice.** Do not auto-continue.
4. Record the choice (`log_checkpoint`) and set the next objective before resuming.
