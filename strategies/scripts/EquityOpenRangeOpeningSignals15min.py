#!/usr/bin/env python3
"""Opening-range signals judged from the first two 15-minute candles.

Two independent rules, both reported in the dashboard's ORB column:

``second_candle_prev_high_buy_signal``
    BUY when the session's second 15-minute candle closes above both the first
    candle's close and the previous trading day's high. The first two candles
    define the opening range, so taking out both the opening close and the
    prior day's high is continuation rather than a gap fade.

``first_candle_small_body_sell_signal``
    SELL when the session's first 15-minute candle has a body under
    ``SMALL_BODY_RATIO`` of its own high-low range. A body that small is
    indecision at the open, and is treated as the bearish side of the range.

``first_candle_gap_down_sell_signal``
    SELL when the session's first 15-minute candle opens below the previous
    trading day's close and then closes below its own open: a gap-down open
    which fails to recover, read as continuation of the weakness.

``second_candle_gap_ema_sell_signal``
    SELL when the session's second 15-minute candle opens below the first
    candle's close and closes below ema10: a gap down between the two opening
    candles that leaves price under its own recent average.

This module is deliberately signal-only: no database, order or spreadsheet
plumbing, so the dashboard column and any live caller apply identical rules.
It lives apart from OrbStrategyCallPut.py, which is a working file of its own,
while still feeding that strategy's column.

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
    """Load the shared indicator helpers by path."""
    name = "orb_opening_common_indicators"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(
        name, STRATEGIES_DIR / "utils" / "common_indicators.py")
    if spec is None or spec.loader is None:
        raise ImportError("cannot locate strategies/utils/common_indicators.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


_indicators = _load_common_indicators()
ema = _indicators.ema

MARKET_TIMEZONE = ZoneInfo("Asia/Kolkata")
CANDLE_SECONDS = 15 * 60

#: The average the second candle's close is measured against.
FAST = 10

#: A first candle whose body is under this fraction of its range is indecision.
SMALL_BODY_RATIO = 0.20

#: The second candle can only be judged once this many session bars exist.
CANDLES_REQUIRED = 2


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


def _bars_are_intraday(bars: Sequence[Any], session_day: dt.date) -> dict | None:
    """Reject inputs that cannot hold a pair of opening 15-minute candles.

    Also the answer for a daily series, which holds one bar per session and so
    has no opening candles to judge. Defence in depth: a session whose bars sit
    hours apart is not a 15-minute series either.
    """
    if len(bars) < CANDLES_REQUIRED:
        return {
            "reason": f"fewer than {CANDLES_REQUIRED} candles in the session",
            "session_bars": len(bars),
            "session_day": session_day.isoformat(),
        }
    if bars[1].epoch - bars[0].epoch > CANDLE_SECONDS:
        return {
            "reason": "candles are not 15-minute bars",
            "first_gap_seconds": bars[1].epoch - bars[0].epoch,
            "session_day": session_day.isoformat(),
        }
    return None


def second_candle_prev_high_buy_signal(
    candles_15min: Sequence[Any],
    daily: Sequence[Any],
    current: dt.datetime,
    restrict_to_today: bool = True,
) -> tuple[str, dict]:
    """Buy when the second opening candle clears the first close and prev high.

    Returns ("CE"|"NONE", details), matching the ORB strategy's CE/PE
    vocabulary where CE is the buy side.
    """
    bars, session_day = session_candles(candles_15min, current, restrict_to_today)
    rejected = _bars_are_intraday(bars, session_day)
    if rejected is not None:
        return "NONE", rejected

    reference = previous_session(daily, session_day)
    if reference is None:
        return "NONE", {"reason": "no previous session in daily history",
                        "session_day": session_day.isoformat()}

    first, second = bars[0], bars[1]
    first_close = float(first.close)
    second_close = float(second.close)
    previous_high = float(reference.high)
    above_first = second_close > first_close
    above_previous_high = second_close > previous_high

    details = {
        "strategy": "ORB_SECOND_CANDLE_PREV_HIGH",
        "session_day": session_day.isoformat(),
        "previous_day": _ist(reference).date().isoformat(),
        "previous_high": round(previous_high, 2),
        "first_open": float(first.open), "first_high": float(first.high),
        "first_low": float(first.low), "first_close": first_close,
        "first_time": _ist(first).isoformat(),
        "second_open": float(second.open), "second_high": float(second.high),
        "second_low": float(second.low), "second_close": second_close,
        "second_time": _ist(second).isoformat(),
        # curr_* mirror the ORB strategy's own detail keys so the dashboard
        # stamps the cell with the second candle's close time.
        "curr_open": float(second.open), "curr_high": float(second.high),
        "curr_low": float(second.low), "curr_close": second_close,
        "curr_time": _ist(second).isoformat(),
        # Declared so a caller aggregating several timeframes stamps this cell
        # with the 15-minute close rather than its own default bar size.
        "bar_seconds": CANDLE_SECONDS,
        "above_first_close": above_first,
        "above_previous_high": above_previous_high,
    }

    if above_first and above_previous_high:
        return "CE", {**details, "reason": "second candle closed above the "
                                         "first close and the previous day's high"}
    if not above_first:
        reason = "second candle did not close above the first candle"
    else:
        reason = "second candle did not close above the previous day's high"
    return "NONE", {**details, "reason": reason}


def second_candle_gap_ema_sell_signal(
    candles_15min: Sequence[Any],
    current: dt.datetime,
    restrict_to_today: bool = True,
) -> tuple[str, dict]:
    """Sell when the second candle gaps below the first and closes under ema10.

    Two conditions, both on the session's second 15-minute candle:

    1. its **open** is below the first candle's **close**, so the session gapped
       down between the two opening candles rather than continuing, and
    2. its **close** is below the 10-period EMA at that bar, so the weakness was
       not recovered and price is under its own recent average.

    The gap is measured against the first candle's close, not its open. Against
    the open it would be a weaker test, and against the first candle's low it
    would need a gap straight through the opening range, which no symbol in the
    last session's universe managed. An equal open is not a gap.

    ema10 is read from the whole 15-minute history, so the average carries the
    sessions before today. Insufficient history leaves the rule unable to judge
    rather than judging it against a short average.

    Returns ("PE"|"NONE", details), using the ORB strategy's PE for the sell.
    """
    bars, session_day = session_candles(candles_15min, current, restrict_to_today)
    base = {"session_day": session_day.isoformat(), "session_bars": len(bars)}
    rejected = _bars_are_intraday(bars, session_day)
    if rejected is not None:
        return "NONE", rejected

    first, second = bars[0], bars[1]
    ordered = sorted(candles_15min, key=lambda candle: candle.epoch)
    index = next((i for i, candle in enumerate(ordered)
                  if candle.epoch == second.epoch), None)
    closes = [float(candle.close) for candle in ordered]
    averages = ema(closes, FAST) if index is not None else []
    ema10 = averages[index] if averages and index < len(averages) else None

    first_close = float(first.close)
    second_open = float(second.open)
    second_close = float(second.close)
    gapped_down = second_open < first_close
    below_ema10 = None if ema10 is None else second_close < ema10

    details = {
        "strategy": "ORB_SECOND_CANDLE_GAP_EMA",
        **base,
        "first_open": float(first.open), "first_high": float(first.high),
        "first_low": float(first.low), "first_close": first_close,
        "first_time": _ist(first).isoformat(),
        "second_open": second_open, "second_high": float(second.high),
        "second_low": float(second.low), "second_close": second_close,
        "second_time": _ist(second).isoformat(),
        "gap": round(second_open - first_close, 2),
        "gap_percent": (round((second_open - first_close) / first_close * 100, 4)
                        if first_close else None),
        "gapped_down": gapped_down,
        "ema10": round(ema10, 4) if ema10 is not None else None,
        "below_ema10": below_ema10,
        # curr_* mirror the ORB strategy's own detail keys so the dashboard
        # stamps the cell with the second candle's close time.
        "curr_open": second_open, "curr_high": float(second.high),
        "curr_low": float(second.low), "curr_close": second_close,
        "curr_time": _ist(second).isoformat(),
        "bar_seconds": CANDLE_SECONDS,
    }

    if ema10 is None:
        return "NONE", {
            **details,
            "reason": f"insufficient history for ema{FAST} at the second "
                      "candle",
        }
    if gapped_down and below_ema10:
        return "PE", {
            **details,
            "reason": f"second candle opened {second_open:.2f} below the "
                      f"first candle's close {first_close:.2f} and closed at "
                      f"{second_close:.2f}, under ema{FAST} {ema10:.2f}",
        }
    if not gapped_down:
        reason = (f"second candle did not open below the first candle's close "
                  f"{first_close:.2f}")
    else:
        reason = (f"second candle closed at {second_close:.2f}, not under "
                  f"ema{FAST} {ema10:.2f}")
    return "NONE", {**details, "reason": reason}


def first_candle_gap_down_sell_signal(
    candles_15min: Sequence[Any],
    daily: Sequence[Any],
    current: dt.datetime,
    restrict_to_today: bool = True,
) -> tuple[str, dict]:
    """Sell when the first candle gaps down and then falls further.

    The gap is measured against the previous trading day's close, so a
    genuinely lower open is required; an equal open is not a gap down. The
    candle must then close below its own open, meaning the weakness carried
    into the first quarter hour rather than being recovered.

    Returns ("PE"|"NONE", details), using the ORB strategy's PE for the sell.
    """
    bars, session_day = session_candles(candles_15min, current, restrict_to_today)
    rejected = _bars_are_intraday(bars, session_day)
    if rejected is not None:
        return "NONE", rejected

    reference = previous_session(daily, session_day)
    if reference is None:
        return "NONE", {"reason": "no previous session in daily history",
                        "session_day": session_day.isoformat()}

    first = bars[0]
    open_price = float(first.open)
    close_price = float(first.close)
    previous_close = float(reference.close)
    gapped_down = open_price < previous_close
    fell = close_price < open_price

    details = {
        "strategy": "ORB_FIRST_CANDLE_GAP_DOWN",
        "session_day": session_day.isoformat(),
        "previous_day": _ist(reference).date().isoformat(),
        "previous_close": round(previous_close, 2),
        "first_open": open_price, "first_high": float(first.high),
        "first_low": float(first.low), "first_close": close_price,
        "first_time": _ist(first).isoformat(),
        "gap": round(open_price - previous_close, 2),
        "gap_percent": (round((open_price - previous_close) / previous_close * 100, 4)
                        if previous_close else None),
        "gapped_down": gapped_down,
        "closed_below_open": fell,
        # curr_* mirror the ORB strategy's own detail keys so the dashboard
        # stamps the cell with the first candle's close time.
        "curr_open": open_price, "curr_high": float(first.high),
        "curr_low": float(first.low), "curr_close": close_price,
        "curr_time": _ist(first).isoformat(),
        "bar_seconds": CANDLE_SECONDS,
    }

    if gapped_down and fell:
        return "PE", {**details,
                      "reason": f"first candle opened {open_price:.2f} below "
                                f"yesterday's close {previous_close:.2f} and "
                                f"closed lower at {close_price:.2f}"}
    if not gapped_down:
        reason = ("first candle did not open below the previous day's close")
    else:
        reason = "first candle recovered and did not close below its open"
    return "NONE", {**details, "reason": reason}


def first_candle_small_body_sell_signal(
    candles_15min: Sequence[Any],
    current: dt.datetime,
    restrict_to_today: bool = True,
) -> tuple[str, dict]:
    """Sell when the first opening candle's body is under 20% of its range.

    The body is |close - open| and the range is high - low, so a body under a
    fifth of the range is an opening bar that went nowhere: indecision, read
    here as the bearish side. Returns ("PE"|"NONE", details), using the ORB
    strategy's PE for the sell side.

    A candle with no range at all cannot express the ratio and is not judged.
    """
    bars, session_day = session_candles(candles_15min, current, restrict_to_today)
    rejected = _bars_are_intraday(bars, session_day)
    if rejected is not None:
        return "NONE", rejected

    first = bars[0]
    body = abs(float(first.close) - float(first.open))
    span = float(first.high) - float(first.low)
    ratio = body / span if span > 0 else None

    details = {
        "strategy": "ORB_FIRST_CANDLE_SMALL_BODY",
        "session_day": session_day.isoformat(),
        "first_open": float(first.open), "first_high": float(first.high),
        "first_low": float(first.low), "first_close": float(first.close),
        "first_time": _ist(first).isoformat(),
        "body": round(body, 4),
        "range": round(span, 4),
        "body_ratio": round(ratio, 4) if ratio is not None else None,
        "body_ratio_limit": SMALL_BODY_RATIO,
        # curr_* mirror the ORB strategy's own detail keys so the dashboard
        # stamps the cell with the first candle's close time.
        "curr_open": float(first.open), "curr_high": float(first.high),
        "curr_low": float(first.low), "curr_close": float(first.close),
        "curr_time": _ist(first).isoformat(),
        "bar_seconds": CANDLE_SECONDS,
    }

    if ratio is None:
        return "NONE", {**details,
                        "reason": "first candle has no high-low range"}
    if body < SMALL_BODY_RATIO * span:
        return "PE", {**details,
                      "reason": f"first candle body {ratio:.1%} of its range "
                                f"is under {SMALL_BODY_RATIO:.0%}"}
    return "NONE", {**details,
                    "reason": f"first candle body {ratio:.1%} of its range "
                              f"is not under {SMALL_BODY_RATIO:.0%}"}
