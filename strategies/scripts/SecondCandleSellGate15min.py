#!/usr/bin/env python3
"""Sell gate for the Second Candle strategy (15-minute).

The strategy's own PE branches fire on the shape of the opening candles alone,
which is a weak filter on their own. This gate holds a second-candle **sell**
back unless price is genuinely weak at the moment the signal is judged:

    close < previous trading day's high   OR   close < ema10

Either leg is enough. The buy side is not touched.

``close`` is the close of the bar the signal was judged on, which for a
15-minute column is the newest bar of the series handed in, so the test is made
against the live price rather than a fixed opening candle.

The gate fails **open**: if neither leg can be evaluated, for want of a previous
session or enough history for ema10, the sell is allowed through. A data gap
must never be the reason a real signal disappears.

This module is deliberately signal-only: it holds no database, order or
spreadsheet plumbing, so the dashboard column and any caller apply identical
rules from exactly the same code.
"""
from __future__ import annotations

import datetime as dt
import importlib.util
import pathlib
import sys
from typing import Any, Sequence
from zoneinfo import ZoneInfo

SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
STRATEGIES_DIR = SCRIPT_DIR.parent
MARKET_TIMEZONE = ZoneInfo("Asia/Kolkata")
CANDLE_SECONDS = 15 * 60
FAST = 10


def _load_common_indicators():
    """Load the shared indicator helpers by path."""
    module_name = "second_candle_gate_common_indicators"
    if module_name in sys.modules:
        return sys.modules[module_name]
    spec = importlib.util.spec_from_file_location(
        module_name, STRATEGIES_DIR / "utils" / "common_indicators.py")
    if spec is None or spec.loader is None:
        raise ImportError("cannot locate strategies/utils/common_indicators.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


_indicators = _load_common_indicators()
ema = _indicators.ema


def previous_session_high(
    daily: Sequence[Any],
    session_day: dt.date,
) -> float | None:
    """High of the most recent daily bar from before session_day."""
    for candle in sorted(daily, key=lambda c: c.epoch, reverse=True):
        stamp = dt.datetime.fromtimestamp(candle.epoch, MARKET_TIMEZONE)
        if stamp.date() < session_day:
            return float(candle.high)
    return None


def second_candle_sell_gate(
    candles_15min: Sequence[Any],
    daily: Sequence[Any],
    current: dt.datetime,
    restrict_to_today: bool = False,
) -> tuple[bool, dict]:
    """Whether a second-candle sell may be shown for the newest bar.

    Returns (allowed, gate) where gate names the leg that carried it, so the
    cell's tooltip can say why the sell was kept.
    """
    ordered = sorted(candles_15min, key=lambda candle: candle.epoch)
    gate: dict[str, Any] = {"rule": "second_candle_sell_gate"}
    if not ordered:
        gate.update(allowed=True, reason="no candles, nothing to gate",
                    below_previous_high=None, below_ema10=None)
        return True, gate

    latest = ordered[-1]
    close = float(latest.close)
    judged = dt.datetime.fromtimestamp(latest.epoch, MARKET_TIMEZONE)
    if restrict_to_today:
        session_day = current.date()
    else:
        session_day = judged.date()

    closes = [float(candle.close) for candle in ordered]
    fast = ema(closes, FAST)
    ema10 = fast[-1] if fast else None
    previous_high = previous_session_high(daily, session_day)

    below_previous_high = (
        None if previous_high is None else close < previous_high)
    below_ema10 = None if ema10 is None else close < ema10

    gate.update({
        "judged_close": close,
        "judged_time": judged.isoformat(),
        "session_day": session_day.isoformat(),
        "gate_previous_high": (round(previous_high, 2)
                               if previous_high is not None else None),
        "gate_ema10": round(ema10, 2) if ema10 is not None else None,
        "gate_previous_day": (session_day - dt.timedelta(days=1)).isoformat(),
        "below_previous_high": below_previous_high,
        "below_ema10": below_ema10,
    })

    legs = [below for below in (below_previous_high, below_ema10)
            if below is not None]
    if not legs:
        # Fail open: nothing could be checked, so the sell is not held back.
        gate.update(allowed=True,
                    reason="neither gate leg could be evaluated, allowing")
        return True, gate

    allowed = any(legs)
    if allowed:
        if below_previous_high and below_ema10:
            reason = f"close {close:g} is below the previous day's high and ema{FAST}"
        elif below_previous_high:
            reason = (f"close {close:g} is below the previous day's high "
                      f"{previous_high:g}")
        else:
            reason = f"close {close:g} is below ema{FAST} {ema10:g}"
    else:
        reason = (f"close {close:g} is not below the previous day's high "
                  f"nor ema{FAST}")
    gate.update(allowed=allowed, reason=reason)
    return allowed, gate
