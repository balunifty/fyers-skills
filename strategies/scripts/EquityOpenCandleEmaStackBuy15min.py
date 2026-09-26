#!/usr/bin/env python3
"""Open-candle EMA stack signal (15-minute).

Signals BUY when either of the session's first two 15-minute candles is
bullish, closes above the previous trading day's high, and the EMA stack is
aligned at that candle's close:

    ema10 > ema20 > ema30
    ema10 > ema20 > ema50

Signals SELL when the order is inverted at either of those candles:

    ema10 < ema20 < ema50

The two are mutually exclusive, since one needs ema10 above ema20 and the other
below it. The sell is judged on the EMA order alone, exactly as specified, so in
a weak market it is true for nearly every symbol; the buy, which also needs a
candle clearing the previous day's high, is far rarer. When one candle
qualifies for the buy and the other for the sell, the buy wins as the more
specific setup.

The buy's two stacks are checked separately, so ema30 and ema50 are free to sit
in either order relative to one another. Only the first qualifying candle is
reported, being the earliest point at which the setup was actually valid.

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
    module_name = "open_candle_ema_stack_common_indicators"
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
STRATEGY_NAME = "OPEN_CANDLE_EMA_STACK"

#: The EMAs the stack is built from, and the order each stack must hold.
STACKS: tuple[tuple[int, ...], ...] = ((10, 20, 30), (10, 20, 50))

#: The bearish order that produces the sell, exactly as specified:
#: ema10 < ema20 < ema50. Deliberately not the mirror of STACKS, which would
#: additionally demand ema20 < ema30.
INVERTED_STACK: tuple[int, ...] = (10, 20, 50)

#: Only the first two candles of a session are considered.
CANDLES_CHECKED = 2


def _ist(candle: Any) -> dt.datetime:
    return dt.datetime.fromtimestamp(candle.epoch, MARKET_TIMEZONE)


def session_candles(
    candles_15min: Sequence[Any],
    current: dt.datetime,
    restrict_to_today: bool = True,
) -> tuple[list[Any], dt.date]:
    """The session's candles and the day they belong to.

    restrict_to_today is True for live trading, so only the current session is
    judged. The read-only dashboard passes False, so a holiday or weekend still
    reports the most recent session's setup instead of going quiet.
    """
    ordered = sorted(candles_15min, key=lambda candle: candle.epoch)
    if not ordered:
        return [], current.date()
    if restrict_to_today:
        day = current.date()
    else:
        day = _ist(ordered[-1]).date()
    return [c for c in ordered if _ist(c).date() == day], day


def previous_session(daily: Sequence[Any], session_day: dt.date) -> Any | None:
    """Most recent daily bar from a session strictly before session_day."""
    for candle in sorted(daily, key=lambda c: c.epoch, reverse=True):
        if _ist(candle).date() < session_day:
            return candle
    return None


def ema_stack_holds(closes: Sequence[float]) -> tuple[bool, dict[str, float | None]]:
    """Check both EMA stacks on a close series ending at the candle of interest.

    Returns (holds, values) where holds is True only when every stack is
    strictly descending. A missing EMA (too little history) fails the check
    rather than being treated as satisfied.
    """
    series = {period: ema(list(closes), period) for period in
              (10, 20, 30, 50)}
    latest = {period: values[-1] for period, values in series.items()}
    if any(value is None for value in latest.values()):
        return False, latest

    holds = True
    for stack in STACKS:
        for fast, slow in zip(stack, stack[1:]):
            if not latest[fast] > latest[slow]:
                holds = False
                break
        if not holds:
            break
    return holds, latest


def ema_stack_inverted(values: dict[str, float | None]) -> bool:
    """True when the order is ema10 < ema20 < ema50, which produces the sell.

    Takes the values dict that ema_stack_holds already computed, so the EMAs
    are not recomputed. A missing EMA fails the check rather than being treated
    as satisfied.
    """
    if any(values.get(period) is None for period in INVERTED_STACK):
        return False
    return all(
        values[fast] < values[slow]
        for fast, slow in zip(INVERTED_STACK, INVERTED_STACK[1:])
    )


def open_candle_ema_stack_buy_signal(
    candles_15min: Sequence[Any],
    daily: Sequence[Any],
    current: dt.datetime,
    restrict_to_today: bool = True,
) -> tuple[str, dict]:
    """Buy when an opening candle clears yesterday's high on an aligned stack.

    Returns ("BUY"|"NONE", details). The reported candle is the earliest of the
    first two that satisfies every rule, so the timestamp is the first moment
    the setup was tradeable rather than whenever the scan happened to run.
    """
    bars, session_day = session_candles(candles_15min, current, restrict_to_today)
    if not bars:
        return "NONE", {"reason": "no candles for the session", "session_day":
                        session_day.isoformat()}
    if len(bars) < CANDLES_CHECKED:
        # Also the answer for a daily series, which can only ever hold one bar
        # per session and so has no opening candle to judge.
        return "NONE", {
            "reason": f"fewer than {CANDLES_CHECKED} candles in the session",
            "session_bars": len(bars),
            "session_day": session_day.isoformat(),
        }

    # Defence in depth: a session holding bars far apart is not a 15-minute
    # series, whatever it was labelled.
    if bars[1].epoch - bars[0].epoch > CANDLE_SECONDS:
        return "NONE", {
            "reason": "candles are not 15-minute bars",
            "first_gap_seconds": bars[1].epoch - bars[0].epoch,
            "session_day": session_day.isoformat(),
        }

    reference = previous_session(daily, session_day)
    if reference is None:
        return "NONE", {"reason": "no previous session in daily history",
                        "session_day": session_day.isoformat()}

    previous_high = float(reference.high)
    # Locate this candle inside the full series so the EMAs carry every earlier
    # session's closes; a 50-period EMA spans more than one trading day.
    ordered = sorted(candles_15min, key=lambda candle: candle.epoch)
    closes = [float(candle.close) for candle in ordered]
    closes_by_epoch = {candle.epoch: index for index, candle in enumerate(ordered)}

    first_failure: dict | None = None
    sell_hit: dict | None = None
    for considered, bar in enumerate(bars[:CANDLES_CHECKED], start=1):
        label = "first" if considered == 1 else "second"
        opened = dt.datetime.fromisoformat(_ist(bar).isoformat())
        bullish = float(bar.close) > float(bar.open)
        above_previous_high = float(bar.close) > previous_high
        index = closes_by_epoch.get(bar.epoch)
        holds, values = (
            ema_stack_holds(closes[:index + 1]) if index is not None
            else (False, {10: None, 20: None, 30: None, 50: None})
        )
        inverted = ema_stack_inverted(values)

        common = {
            "strategy": STRATEGY_NAME,
            "candle": label,
            "candle_epoch": bar.epoch,
            "curr_time": opened.isoformat(),
            # Declared so a caller aggregating several timeframes stamps this
            # cell with the 15-minute close rather than its own default.
            "bar_seconds": CANDLE_SECONDS,
            "session_day": session_day.isoformat(),
            "previous_day": _ist(reference).date().isoformat(),
            "previous_high": round(previous_high, 2),
            "open": float(bar.open), "high": float(bar.high),
            "low": float(bar.low), "close": float(bar.close),
            "curr_close": float(bar.close),
            "bullish": bullish,
            "above_previous_high": above_previous_high,
            "bullish_stack": holds,
            "inverted_stack": inverted,
            "ema10": _round(values[10]), "ema20": _round(values[20]),
            "ema30": _round(values[30]), "ema50": _round(values[50]),
        }

        # The buy is the rarer, more specific setup, so it is preferred over a
        # sell found on the other candle. Both are collected first because the
        # sell is judged on the EMA order alone.
        if bullish and above_previous_high and holds:
            return "BUY", {**common, "reason": "all conditions met"}
        if inverted and sell_hit is None:
            sell_hit = {
                **common,
                "reason": "ema10 < ema20 < ema50 at the "
                          f"{label} candle",
            }

        # Neither fired here, so remember why for the no-signal report.
        if not bullish:
            first_failure = first_failure or {
                **common, "reason": f"{label} candle is not bullish"}
        elif not above_previous_high:
            first_failure = first_failure or {
                **common,
                "reason": f"{label} candle did not close above the "
                          "previous day's high"}
        elif not holds:
            first_failure = first_failure or {
                **common,
                "reason": f"EMA stack not aligned at the {label} candle: need "
                          "ema10>ema20>ema30 and ema10>ema20>ema50"}

    if sell_hit is not None:
        return "SELL", sell_hit
    if first_failure is not None:
        return "NONE", first_failure
    return "NONE", {
        "reason": "no opening candle satisfied any rule",
        "session_day": session_day.isoformat(),
    }


def _round(value: float | None) -> float | None:
    return round(float(value), 2) if isinstance(value, (int, float)) else None
