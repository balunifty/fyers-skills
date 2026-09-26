#!/usr/bin/env python3
"""EMA 10 against EMA 20 signals (15-minute).

Four independent rules, all judged on the bar the series ends on, so handing
this a series truncated to bar N judges bar N. That is how the dashboard walks
a session.

Buy:

* ``ema10`` crosses **above** ``ema20`` (the previous bar had ema10 at or below
  ema20, and this bar has it above).

Sell:

* ``ema10`` crosses **below** ``ema20``, the mirror of the buy.
* ``ema10 < ema20 < ema30`` and the bar closes below ``ema10``.
* ``ema10 < ema20 < ema50`` and the bar closes below ``ema10``.

The two crossings need the previous bar on the other side, so a stack that is
already aligned does not keep re-firing. The two bearish rules are *states*
rather than events: they hold for as long as the stack and the close agree, so
they can be true on consecutive bars. A crossing is reported in preference to
them, being the more specific event.

This module is deliberately signal-only: it holds no database, order or
spreadsheet plumbing, so the dashboard column and any caller evaluate exactly
the same rules from exactly the same code.

Candles are anything exposing ``epoch``, ``open``, ``high``, ``low`` and
``close``; the dashboard's parsed history rows satisfy that directly.
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


def _load_common_indicators():
    """Load the shared indicator helpers by path.

    Imported by path rather than by name so this module works whichever
    directory the caller's sys.path happens to start from.
    """
    module_name = "ema10_20_signals_common_indicators"
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

MARKET_TIMEZONE = ZoneInfo("Asia/Kolkata")
CANDLE_SECONDS = 15 * 60
STRATEGY_NAME = "EMA10_EMA20"

#: The pair both crossings are measured on. Kept as data so the maths and the
#: wording cannot drift apart.
FAST = 10
SLOW = 20

#: The two bearish stacks, each needing its own slower average.
BEARISH_STACKS: tuple[tuple[int, ...], ...] = ((10, 20, 30), (10, 20, 50))


def _round(value: float | None) -> float | None:
    return round(float(value), 2) if isinstance(value, (int, float)) else None


def _descending(values: dict[int, float | None], stack: Sequence[int]) -> bool:
    """True when the stack is strictly descending and fully known."""
    if any(values.get(period) is None for period in stack):
        return False
    return all(values[fast] < values[slow]
               for fast, slow in zip(stack, stack[1:]))


def ema10_ema20_signal(
    candles: Sequence[Any],
    bar_seconds: int = CANDLE_SECONDS,
) -> tuple[str, dict]:
    """Judge the bar the series ends on against every rule above.

    Returns ("BUY"|"SELL"|"NONE", details). The details name which rule fired,
    and report each condition whether or not it held, so a near miss can be
    read off the cell's tooltip.
    """
    ordered = sorted(candles, key=lambda candle: candle.epoch)
    # Every return carries a "rule" key, so a caller can rely on the shape.
    if len(ordered) < 2:
        return "NONE", {"rule": None, "bars": len(ordered),
                        "reason": "need at least 2 candles"}

    closes = [float(candle.close) for candle in ordered]
    series = {period: ema(closes, period) for period in (10, 20, 30, 50)}
    latest = {period: values[-1] for period, values in series.items()}
    previous = {period: values[-2] for period, values in series.items()}

    # Both crossings compare two bars, so the previous bar's EMAs must exist
    # too. ema() seeds with an SMA, so at exactly `period` values the second to
    # last slot is still None. The 30 and 50 averages are only read for the
    # bearish states, which look at the latest bar alone.
    if (latest[FAST] is None or latest[SLOW] is None
            or previous[FAST] is None or previous[SLOW] is None):
        return "NONE", {"rule": None,
                        "reason": f"insufficient history for ema{FAST} and "
                                  f"ema{SLOW}", "bars": len(ordered)}

    close_price = closes[-1]
    crossed_up = (previous[FAST] <= previous[SLOW]
                  and latest[FAST] > latest[SLOW])
    crossed_down = (previous[FAST] >= previous[SLOW]
                    and latest[FAST] < latest[SLOW])
    close_below_fast = close_price < latest[FAST]
    stacks = {stack: _descending(latest, stack) for stack in BEARISH_STACKS}
    # Each bearish rule needs only its own slower average, so one of the two
    # being unavailable must not silence the other.
    stack_30, stack_50 = BEARISH_STACKS
    bearish_30 = stacks[stack_30] and close_below_fast
    bearish_50 = stacks[stack_50] and close_below_fast

    current, prior = ordered[-1], ordered[-2]
    started = dt.datetime.fromtimestamp(current.epoch, MARKET_TIMEZONE)

    details = {
        "strategy": STRATEGY_NAME,
        "candle_epoch": current.epoch,
        "curr_time": started.isoformat(),
        # Declared so a caller aggregating several timeframes stamps this cell
        # with the close of the bar that actually produced it.
        "bar_seconds": bar_seconds,
        "open": float(current.open), "high": float(current.high),
        "low": float(current.low), "close": close_price,
        "curr_open": float(current.open), "curr_high": float(current.high),
        "curr_low": float(current.low), "curr_close": close_price,
        "previous_close": float(prior.close),
        f"ema{FAST}": _round(latest[FAST]), f"ema{SLOW}": _round(latest[SLOW]),
        "ema30": _round(latest[30]), "ema50": _round(latest[50]),
        f"previous_ema{FAST}": _round(previous[FAST]),
        f"previous_ema{SLOW}": _round(previous[SLOW]),
        "crossed_up": crossed_up,
        "crossed_down": crossed_down,
        "close_below_ema10": close_below_fast,
        "stack_10_20_30": stacks[stack_30],
        "stack_10_20_50": stacks[stack_50],
    }

    # A crossing is an event and wins over the two states, which can be true on
    # any number of consecutive bars.
    if crossed_up:
        return "BUY", {**details, "rule": "cross_up",
                       "reason": f"ema{FAST} crossed above ema{SLOW}"}
    if crossed_down:
        return "SELL", {**details, "rule": "cross_down",
                        "reason": f"ema{FAST} crossed below ema{SLOW}"}
    if bearish_30:
        return "SELL", {
            **details, "rule": "stack_10_20_30",
            "reason": f"ema{FAST}<ema{SLOW}<ema30 and the close "
                      f"{close_price:.2f} is below ema{FAST} "
                      f"{latest[FAST]:.2f}"}
    if bearish_50:
        return "SELL", {
            **details, "rule": "stack_10_20_50",
            "reason": f"ema{FAST}<ema{SLOW}<ema50 and the close "
                      f"{close_price:.2f} is below ema{FAST} "
                      f"{latest[FAST]:.2f}"}

    return "NONE", {**details, "rule": None,
                    "reason": f"no ema{FAST}/ema{SLOW} rule fired"}
