#!/usr/bin/env python3
"""Append-only evolution log for Supercharge Mode (stdlib only).

Records the strategy's optimization journey so nothing is lost between rounds and the final
deliverables (Strategy Evolution Timeline, Executive Summary, AI Consensus) can be assembled from
a single source of truth. Mirrors fyers-trading's `trade_logger.py`: one JSON object per line,
never truncated or rotated, logging must never crash the caller.

Unlike the trade log (which is global at ~/.fyers/trades.jsonl), the evolution log is PER
STRATEGY and lives inside the strategy folder so it travels with the work:
    strategies/<name>/supercharge/evolution.jsonl
Build that path with ``default_path("<name>")``; pass the resulting string as the `path` arg
(or --path on the CLI) so each strategy keeps its own log. Omitting the name falls back to a
``_unassigned`` placeholder that stays inside the same layout rather than sharing one global log.

Record types
------------
    round     — a completed optimization loop: {round, objective, leader, notes}
    variant   — a generated + backtested variant: {round, variant_id, objective, scorecard,
                rules_changed, rules_preserved, decision ("accepted"|"rejected"|"parked"), reason}
    decision  — a debate/consensus outcome: {round, summary, disagreements, resolved}
    checkpoint— a user checkpoint every 3 rounds: {round, report, user_choice}

Functions
---------
    log_round(round_no, objective, leader=None, notes=None, path=DEFAULT_PATH)
    log_variant(round_no, variant_id, scorecard, decision, objective=None,
                rules_changed=None, rules_preserved=None, reason=None, path=DEFAULT_PATH)
    log_decision(round_no, summary, disagreements=None, resolved=None, path=DEFAULT_PATH)
    log_checkpoint(round_no, report, user_choice=None, path=DEFAULT_PATH)
    timeline(path=DEFAULT_PATH) -> list[dict]        every record, in order
    checkpoint_report(round_no, path=DEFAULT_PATH) -> dict   rolls up the last 3 rounds
    summary(path=DEFAULT_PATH) -> dict               aggregate counts + current leader

CLI
---
    python evolution_log.py demo                  # write a synthetic journey to a temp log + read back
    python evolution_log.py timeline [--path P]   # print every record
    python evolution_log.py summary  [--path P]
    python evolution_log.py tail --n 20 [--path P]
"""
from __future__ import annotations

import datetime
import json
import os
import sys

def default_path(strategy: str = "_unassigned") -> str:
    """Build the per-strategy evolution-log path documented in references/variants.md:
    ``strategies/<strategy>/supercharge/evolution.jsonl``. Pass the strategy name so each
    strategy gets its own log — callers that omit it share the ``_unassigned`` fallback."""
    return os.path.join("strategies", strategy, "supercharge", "evolution.jsonl")


# Fallback default when no strategy name is supplied. Kept INSIDE the documented per-strategy
# layout (strategies/<name>/supercharge/…) via a placeholder name, so it never writes outside it
# and doesn't silently collide with a named strategy's log. Prefer default_path("<name>").
DEFAULT_PATH = default_path()


# ---------------------------------------------------------------------------
# Internal helpers (mirror trade_logger.py)
# ---------------------------------------------------------------------------

def _now_utc() -> str:
    return datetime.datetime.utcnow().isoformat() + "Z"


def _append(record: dict, path: str) -> None:
    """Append *record* as one JSONL line. Never raises (logging must not crash the loop)."""
    try:
        parent = os.path.dirname(os.path.abspath(path))
        os.makedirs(parent, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, default=str) + "\n")
    except Exception:  # noqa: BLE001
        pass


def _read(path: str) -> list:
    """Return all parsed records from the log, or [] if it doesn't exist / can't be read."""
    if not os.path.isfile(path):
        return []
    out: list = []
    try:
        with open(path, encoding="utf-8") as fh:
            for raw in fh:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    out.append(json.loads(raw))
                except json.JSONDecodeError:
                    continue
    except Exception:  # noqa: BLE001
        return out
    return out


# ---------------------------------------------------------------------------
# Writers
# ---------------------------------------------------------------------------

def log_round(round_no: int, objective: str, leader: "str | None" = None,
              notes: "str | None" = None, path: str = DEFAULT_PATH) -> None:
    """Record a completed optimization loop."""
    _append({
        "record_type": "round",
        "timestamp": _now_utc(),
        "round": round_no,
        "objective": objective,
        "leader": leader,
        "notes": notes,
    }, path)


def log_variant(round_no: int, variant_id: str, scorecard: dict, decision: str,
                objective: "str | None" = None, rules_changed: "list | None" = None,
                rules_preserved: "list | None" = None, reason: "str | None" = None,
                path: str = DEFAULT_PATH) -> None:
    """Record a generated + backtested variant and the council's decision on it.

    `decision` is one of "accepted" | "rejected" | "parked". `scorecard` is the dict from
    scorecard.py. `reason` captures WHY (kept for the transparency success-criterion)."""
    _append({
        "record_type": "variant",
        "timestamp": _now_utc(),
        "round": round_no,
        "variant_id": variant_id,
        "objective": objective,
        "scorecard": scorecard,
        "rules_changed": rules_changed or [],
        "rules_preserved": rules_preserved or [],
        "decision": decision,
        "reason": reason,
    }, path)


def log_decision(round_no: int, summary: str, disagreements: "list | None" = None,
                 resolved: "bool | None" = None, path: str = DEFAULT_PATH) -> None:
    """Record a debate/consensus outcome, including the major disagreements on record."""
    _append({
        "record_type": "decision",
        "timestamp": _now_utc(),
        "round": round_no,
        "summary": summary,
        "disagreements": disagreements or [],
        "resolved": resolved,
    }, path)


def log_checkpoint(round_no: int, report: dict, user_choice: "str | None" = None,
                   path: str = DEFAULT_PATH) -> None:
    """Record a user checkpoint (fired every 3 rounds) and the direction the user chose."""
    _append({
        "record_type": "checkpoint",
        "timestamp": _now_utc(),
        "round": round_no,
        "report": report,
        "user_choice": user_choice,
    }, path)


# ---------------------------------------------------------------------------
# Readers
# ---------------------------------------------------------------------------

def timeline(path: str = DEFAULT_PATH) -> list:
    """Return every record in the log, in write order (the raw evolution timeline)."""
    return _read(path)


def summary(path: str = DEFAULT_PATH) -> dict:
    """Aggregate counts + the most recent leader across the whole log."""
    recs = _read(path)
    stats = {
        "total_records": len(recs),
        "rounds": 0,
        "variants": 0,
        "variants_accepted": 0,
        "variants_rejected": 0,
        "variants_parked": 0,
        "checkpoints": 0,
        "current_leader": None,
    }
    for r in recs:
        rt = r.get("record_type")
        if rt == "round":
            stats["rounds"] += 1
            if r.get("leader"):
                stats["current_leader"] = r["leader"]
        elif rt == "variant":
            stats["variants"] += 1
            d = r.get("decision")
            if d == "accepted":
                stats["variants_accepted"] += 1
            elif d == "rejected":
                stats["variants_rejected"] += 1
            elif d == "parked":
                stats["variants_parked"] += 1
        elif rt == "checkpoint":
            stats["checkpoints"] += 1
    return stats


def checkpoint_report(round_no: int, path: str = DEFAULT_PATH) -> dict:
    """Roll up the last 3 rounds ending at `round_no` into the checkpoint report contents.

    Assembles exactly what references/checkpoints.md prescribes: variants created/accepted/
    rejected in the window, metrics that moved, the major disagreements, and the current leader.
    The recommended next direction is left to the orchestrator/council — this provides the data.
    """
    recs = _read(path)
    window = set(range(max(1, round_no - 2), round_no + 1))  # 3-round window

    created, accepted, rejected, disagreements = [], [], [], []
    leader = None
    for r in recs:
        rnd = r.get("round")
        if rnd not in window:
            # Track the leader from earlier rounds only — NEVER from rounds after `round_no`,
            # so recomputing an old checkpoint from a longer log shows the leader as of that
            # checkpoint, not one from a later optimization round.
            if (r.get("record_type") == "round" and r.get("leader")
                    and isinstance(rnd, int) and rnd <= round_no):
                leader = r["leader"]
            continue
        rt = r.get("record_type")
        if rt == "variant":
            created.append(r.get("variant_id"))
            if r.get("decision") == "accepted":
                accepted.append(r.get("variant_id"))
            elif r.get("decision") == "rejected":
                rejected.append(r.get("variant_id"))
        elif rt == "decision":
            disagreements.extend(r.get("disagreements", []))
        elif rt == "round" and r.get("leader"):
            leader = r["leader"]

    return {
        "round": round_no,
        "window_rounds": sorted(window),
        "variants_created": created,
        "variants_accepted": accepted,
        "variants_discarded": rejected,
        "major_disagreements": disagreements,
        "current_leader": leader,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _arg_path(args: list) -> str:
    if "--path" in args:
        i = args.index("--path")
        try:
            return args[i + 1]
        except IndexError:
            print("--path requires a value", file=sys.stderr)
            raise SystemExit(2)
    return DEFAULT_PATH


def _demo() -> int:
    """Write a synthetic 3-round journey to a temp log and read it back — no token/network."""
    import tempfile

    path = os.path.join(tempfile.gettempdir(), "fyers_supercharge_demo_evolution.jsonl")
    # fresh start
    try:
        os.remove(path)
    except OSError:
        pass

    card = {"sharpe": 1.3, "max_drawdown": -0.12, "return_pct": 0.14, "win_rate": 0.55}
    log_round(1, "risk_adjusted", leader="baseline", notes="baseline captured", path=path)
    log_variant(1, "v1_tighter_stop", card, "accepted", objective="low_drawdown",
                rules_changed=["stop: 2%->1.2%"], rules_preserved=["entry SMA cross"],
                reason="cut drawdown 3pts, Sharpe held", path=path)
    log_decision(1, "Risk vs Return on stop tightness",
                 disagreements=["Return Strategist: fewer winners let run"],
                 resolved=True, path=path)
    log_round(2, "low_drawdown", leader="v1_tighter_stop", path=path)
    log_variant(2, "v2_vol_target", card, "rejected", objective="low_drawdown",
                reason="robustness_score 0.1 — overfit to 2023 vol", path=path)
    log_round(3, "low_drawdown", leader="v1_tighter_stop", path=path)
    rep = checkpoint_report(3, path=path)
    log_checkpoint(3, rep, user_choice="focus_drawdown", path=path)

    print(f"log written: {path}\n")
    print("summary:")
    print(json.dumps(summary(path), indent=2))
    print("\ncheckpoint_report(round 3):")
    print(json.dumps(rep, indent=2))
    print(f"\ntimeline: {len(timeline(path))} records")
    return 0


def _cli() -> int:
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        return 0
    cmd = args[0]

    if cmd == "demo":
        return _demo()

    path = _arg_path(args)

    if cmd == "timeline":
        recs = timeline(path)
        if not recs:
            print(f"(no records in {path})")
        for r in recs:
            print(json.dumps(r, indent=2))
        return 0

    if cmd == "summary":
        print(json.dumps(summary(path), indent=2))
        return 0

    if cmd == "tail":
        n = 20
        if "--n" in args:
            try:
                n = int(args[args.index("--n") + 1])
            except (IndexError, ValueError):
                print("--n requires an integer", file=sys.stderr)
                return 2
        recs = timeline(path)
        recs = recs[-n:] if len(recs) > n else recs
        if not recs:
            print(f"(no records in {path})")
        for r in recs:
            print(json.dumps(r, indent=2))
        return 0

    print(f"unknown command: {cmd!r}  (use: demo | timeline | summary | tail)", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(_cli())
