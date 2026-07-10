#!/usr/bin/env python3
"""Append-only trade audit logger (stdlib only).

Every order attempt and notable event is recorded as a single JSON line to
~/.fyers/trades.jsonl.  The file is never truncated or rotated — every entry
is permanent so the user retains a complete audit trail.

Usage (module):
    from scripts.trade_logger import log_order, log_event, tail, summary

Usage (CLI):
    python scripts/trade_logger.py tail           # last 20 entries
    python scripts/trade_logger.py tail --n 50    # last 50 entries
    python scripts/trade_logger.py summary        # aggregate stats
"""
from __future__ import annotations

import datetime
import json
import sys
from pathlib import Path

LOG_PATH = Path.home() / ".fyers" / "trades.jsonl"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _now_utc() -> str:
    """Return the current UTC time as an ISO 8601 string with a Z suffix."""
    return datetime.datetime.utcnow().isoformat() + "Z"


def _append(record: dict) -> None:
    """Append *record* as a single JSONL line.  Never raises."""
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(record, default=str) + "\n"
        with LOG_PATH.open("a", encoding="utf-8") as fh:
            fh.write(line)
    except Exception:  # noqa: BLE001 — logging must never crash caller
        pass


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def log_order(
    order: dict,
    result: "dict | str",
    dry_run: bool,
    meta: "dict | None" = None,
) -> None:
    """Record one order attempt (real or dry-run) to the JSONL log.

    Parameters
    ----------
    order:
        The full order payload that was passed to place_order().
    result:
        The API response dict, or an error string if the call raised.
    dry_run:
        True when this was a simulated order (nothing sent to the exchange).
    meta:
        Optional dict for strategy context (e.g. ``{"strategy": "sma_cross",
        "signal": "BUY"}``).  Stored as-is.
    """
    if isinstance(result, Exception):
        result_val: "dict | str" = str(result)
    else:
        result_val = result

    # Derive outcome from the result
    if dry_run:
        outcome = "dry_run"
    elif isinstance(result_val, str):
        outcome = "error"
    elif isinstance(result_val, dict) and result_val.get("s") == "ok":
        outcome = "submitted"
    elif isinstance(result_val, dict) and result_val.get("s") == "dry_run":
        outcome = "dry_run"
    else:
        outcome = "error"

    record: dict = {
        "event_type": "order",
        "timestamp": _now_utc(),
        "dry_run": dry_run,
        "order": order,
        "result": result_val,
        "outcome": outcome,
    }
    if meta:
        record["meta"] = meta
    _append(record)


def log_event(event_type: str, data: dict) -> None:
    """Record a general-purpose event (login, token refresh, symbol error, …).

    Parameters
    ----------
    event_type:
        A short label, e.g. ``"login"``, ``"token_refresh"``,
        ``"symbol_validation_failed"``.
    data:
        Arbitrary dict with event-specific fields.
    """
    record: dict = {
        "event_type": event_type,
        "timestamp": _now_utc(),
        "data": data,
    }
    _append(record)


def tail(n: int = 20) -> list[dict]:
    """Return the last *n* entries from the log as a list of dicts.

    Returns an empty list when the log file does not exist yet.
    """
    if not LOG_PATH.exists():
        return []
    try:
        lines = LOG_PATH.read_text(encoding="utf-8").splitlines()
        recent = lines[-n:] if len(lines) > n else lines
        result = []
        for line in recent:
            line = line.strip()
            if line:
                try:
                    result.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
        return result
    except Exception:  # noqa: BLE001
        return []


def summary() -> dict:
    """Return aggregate statistics over the full log.

    Keys
    ----
    total_entries       — total lines in the log (orders + events)
    total_orders        — entries where event_type == "order"
    dry_run_orders      — orders with outcome == "dry_run"
    live_orders         — orders with dry_run == False
    submitted_orders    — orders with outcome == "submitted"
    error_orders        — orders with outcome == "error"
    unique_symbols      — set of distinct symbols that appear in order payloads
    """
    stats: dict = {
        "total_entries": 0,
        "total_orders": 0,
        "dry_run_orders": 0,
        "live_orders": 0,
        "submitted_orders": 0,
        "error_orders": 0,
        "unique_symbols": [],
    }
    if not LOG_PATH.exists():
        return stats

    symbols: set[str] = set()
    try:
        with LOG_PATH.open(encoding="utf-8") as fh:
            for raw in fh:
                raw = raw.strip()
                if not raw:
                    continue
                stats["total_entries"] += 1
                try:
                    rec = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if rec.get("event_type") != "order":
                    continue
                stats["total_orders"] += 1
                outcome = rec.get("outcome", "")
                if outcome == "dry_run" or rec.get("dry_run"):
                    stats["dry_run_orders"] += 1
                if not rec.get("dry_run"):
                    stats["live_orders"] += 1
                if outcome == "submitted":
                    stats["submitted_orders"] += 1
                elif outcome == "error":
                    stats["error_orders"] += 1
                sym = rec.get("order", {}).get("symbol")
                if sym:
                    symbols.add(sym)
    except Exception:  # noqa: BLE001
        pass

    stats["unique_symbols"] = sorted(symbols)
    return stats


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _cli() -> int:
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        return 0

    cmd = args[0]

    if cmd == "tail":
        n = 20
        if "--n" in args:
            idx = args.index("--n")
            try:
                n = int(args[idx + 1])
            except (IndexError, ValueError):
                print("--n requires an integer argument", file=sys.stderr)
                return 2
        entries = tail(n)
        if not entries:
            print(f"(no entries in {LOG_PATH})")
        else:
            for entry in entries:
                print(json.dumps(entry, indent=2))
        return 0

    if cmd == "summary":
        stats = summary()
        print(json.dumps(stats, indent=2))
        return 0

    print(f"unknown command: {cmd!r}  (use: tail | summary)", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(_cli())
