#!/usr/bin/env python3
"""Comparison dashboard for Supercharge Mode (stdlib only).

Ranks the immutable baseline and every optimized variant on the SAME metric set (produced by
`scorecard.py`) and renders the Comparison Dashboard table the PRD calls for. Ranking is
objective-aware: "lower drawdown" sorts by max_drawdown, "higher returns" by return_pct, etc.

The dashboard never decides on its own — it orders candidates so the council (and the user at a
checkpoint) can see which variant leads for the current objective. The baseline is always shown
for comparison and is never dropped.

Functions
---------
    rank(cards, objective="risk_adjusted") -> list[dict]
        cards: {name: scorecard_dict}. Returns a list of rows (dicts) sorted best-first for the
        objective, each row = the scorecard plus "name" and "rank".
    render_markdown(cards, objective="risk_adjusted", columns=None, title=None) -> str
        A Markdown table (baseline + variants), ranked, with the leader marked. The heading
        always carries the FYERS mark (see fyers_title) so the skill is highlighted.
    fyers_title(subject) -> str
        Ensure a report title/subject contains "FYERS" (prefixes it when missing). Reuse this for
        every report the skill emits — dashboards, tear sheets, and the final deliverables.
    OBJECTIVES -> dict   mapping objective key -> (metric, direction) used for ranking.

Usage
-----
    from dashboard import rank, render_markdown
    print(render_markdown({"baseline": base_card, "v1_tighter_stop": v1_card},
                          objective="low_drawdown"))

CLI
---
    python dashboard.py demo     # synthetic baseline + variants -> ranked table, no token/network
"""
from __future__ import annotations

import sys

# objective key -> (scorecard metric, "max"|"min"). These mirror the checkpoint "focus" menu in
# references/checkpoints.md and the Variant Library objectives in references/variants.md.
OBJECTIVES: dict = {
    "risk_adjusted":  ("sharpe", "max"),          # default: best Sharpe
    "higher_returns": ("return_pct", "max"),
    "low_drawdown":   ("max_drawdown", "max"),     # drawdown is negative; closer to 0 = larger
    "high_win_rate":  ("win_rate", "max"),
    "consistency":    ("sortino", "max"),
    "capital_eff":    ("return_on_capital", "max"),
    "execution":      ("execution_risk", "min"),   # lower execution risk is better
    "robustness":     ("robustness_score", "max"),
}

# Default columns shown in the rendered table (subset of the full scorecard, in reading order).
DEFAULT_COLUMNS = [
    "net_profit", "return_pct", "profit_factor", "sharpe", "sortino", "win_rate",
    "max_drawdown", "trade_count", "return_on_capital", "largest_loss",
    "longest_losing_streak", "robustness_score",
]


def fyers_title(subject: str) -> str:
    """Return a report title that always carries the FYERS mark so the skill is highlighted.

    If `subject` already mentions FYERS (any case), it's returned unchanged; otherwise it's
    prefixed with "FYERS ". Every report title/subject the skill emits should pass through here."""
    subject = (subject or "").strip()
    if not subject:
        return "FYERS Report"
    if "fyers" in subject.lower():
        return subject
    return f"FYERS {subject}"


def _sort_key(metric: str, direction: str):
    """Build a sort key that pushes missing (None) values to the bottom regardless of direction."""
    worst = float("-inf") if direction == "max" else float("inf")

    def key(row: dict):
        v = row.get(metric)
        if v is None:
            return worst
        try:
            v = float(v)
        except (TypeError, ValueError):
            return worst
        return v

    return key


def rank(cards: dict, objective: str = "risk_adjusted") -> list:
    """Rank scorecards best-first for `objective`. `cards` maps name -> scorecard dict.

    Unknown objective falls back to the default (risk_adjusted). Rows carry "name" and 1-based
    "rank". Ties keep input order (Python sort is stable)."""
    metric, direction = OBJECTIVES.get(objective, OBJECTIVES["risk_adjusted"])
    rows = [dict(card, name=name) for name, card in cards.items()]
    rows.sort(key=_sort_key(metric, direction), reverse=(direction == "max"))
    for i, row in enumerate(rows, start=1):
        row["rank"] = i
    return rows


def _fmt(v) -> str:
    """Format a cell: percents/ratios to a sensible precision, None -> '—'."""
    if v is None:
        return "—"
    if isinstance(v, bool):
        return str(v)
    if isinstance(v, float):
        return f"{v:,.4f}".rstrip("0").rstrip(".") if abs(v) < 1000 else f"{v:,.0f}"
    return str(v)


def render_markdown(cards: dict, objective: str = "risk_adjusted",
                    columns: "list | None" = None, title: "str | None" = None) -> str:
    """Render the ranked comparison as a Markdown table string.

    The leader (rank 1) is marked with a ★ next to its name. The active objective and the metric
    it sorts on are noted above the table for transparency.

    `title` is the report heading. It defaults to a **FYERS**-branded title so the skill is
    highlighted wherever the report is shown; any caller-supplied title is likewise prefixed with
    "FYERS " when it doesn't already mention FYERS (report titles always carry the FYERS mark)."""
    if not cards:
        return "_(no scorecards to compare)_"
    cols = columns or DEFAULT_COLUMNS
    metric, direction = OBJECTIVES.get(objective, OBJECTIVES["risk_adjusted"])
    rows = rank(cards, objective)

    heading = fyers_title(title or "Supercharge — Comparison Dashboard")
    header = "| # | Strategy | " + " | ".join(cols) + " |"
    sep = "|---|---|" + "|".join(["---"] * len(cols)) + "|"
    lines = [
        f"# {heading}",
        "",
        f"**Objective:** `{objective}` "
        f"(ranked by `{metric}`, {'higher' if direction == 'max' else 'lower'} is better)",
        "",
        header,
        sep,
    ]
    for row in rows:
        name = row["name"] + (" ★" if row["rank"] == 1 else "")
        cells = " | ".join(_fmt(row.get(c)) for c in cols)
        lines.append(f"| {row['rank']} | {name} | {cells} |")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI demo (no token / no network)
# ---------------------------------------------------------------------------

def _demo() -> int:
    """Build synthetic scorecards (via scorecard.py) for a baseline + 3 variants and render."""
    import random

    # Import the sibling scorecard module regardless of CWD.
    import os
    here = os.path.dirname(os.path.abspath(__file__))
    if here not in sys.path:
        sys.path.insert(0, here)
    from scorecard import scorecard  # type: ignore

    def synth(seed, drift, vol, n_trades, avg_pnl):
        rng = random.Random(seed)
        rets = [rng.gauss(drift, vol) for _ in range(252)]
        trades = [{"pnl": rng.gauss(avg_pnl, 400)} for _ in range(n_trades)]
        return scorecard(rets, trades=trades, capital=100_000, rf=0.07)

    cards = {
        "baseline":            synth(1, 0.0004, 0.012, 50, 90),
        "v1_tighter_stop":     synth(2, 0.0005, 0.009, 62, 70),   # lower vol, more trades
        "v2_trend_filter":     synth(3, 0.0007, 0.013, 38, 160),  # higher drift, fewer trades
        "v3_vol_target":       synth(4, 0.0006, 0.008, 55, 110),  # smoother
    }

    print(render_markdown(cards, objective="risk_adjusted"))
    print()
    print(render_markdown(cards, objective="low_drawdown"))
    return 0


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "demo":
        raise SystemExit(_demo())
    print(__doc__)
