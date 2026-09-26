#!/usr/bin/env python3
"""Bullish rejection of the opening-range low, judged on 15-minute candles.

The mirror of the ORB high rejection: price dips to the opening range's low,
fails to hold it, and closes back above it. Three conditions must all hold on
the bar being judged:

1. the session's **first** 15-minute candle (09:15) is bullish,
2. that candle's low was tested, and the close came back above it,
3. the judged candle is itself bullish.

Condition 1 is the point of the rule. A bullish opening candle says the session
opened with buyers in control, so a dip into the opening range's low is a
pullback into support rather than a broken range. Without it the rule is just
the existing ORB rejection's bullish branch, which fires on any low probe
whether or not the session opened strongly.

The opening range is the first 15-minute candle, matching
``OrbStrategyCallPut.calculate_orb_range``.

The judged candle must come strictly after the 09:15 candle. The 09:15 candle
*defines* the opening range, so it trivially satisfies "low tested, close back
above it" and would otherwise fire on every bullish session.

The session is taken from the newest bar rather than the wall clock, so a
holiday or weekend still reports the last session's setup instead of going
quiet. ``restrict_to_today`` switches that off for live trading.

This module is deliberately signal-only: no database, order or spreadsheet
plumbing, so the dashboard column and any live caller apply identical rules.
It lives apart from OrbStrategyCallPut.py, which is a working file of its own.

Candles are anything exposing ``epoch``, ``open``, ``high``, ``low`` and
``close``; the dashboard's parsed history rows satisfy that directly.
"""
from __future__ import annotations

import datetime as dt
from typing import Any, Sequence
from zoneinfo import ZoneInfo

MARKET_TIMEZONE = ZoneInfo("Asia/Kolkata")
CANDLE_SECONDS = 15 * 60

#: The opening candle's own times. The range is this one candle, so it is
#: located by clock time rather than by position in the session.
OPENING_HOUR = 9
OPENING_MINUTE = 15


def _ist(candle: Any) -> dt.datetime:
    return dt.datetime.fromtimestamp(candle.epoch, MARKET_TIMEZONE)


def session_candles(
    candles_15min: Sequence[Any],
    current: dt.datetime,
    restrict_to_today: bool = False,
) -> tuple[list[Any], dt.date]:
    """The session's candles and the day they belong to.

    restrict_to_today is True for live trading, so only the current session is
    judged. The read-only dashboard passes False, so a holiday or weekend still
    reports the most recent session's setup.
    """
    ordered = sorted(candles_15min, key=lambda candle: candle.epoch)
    if not ordered:
        return [], current.date()
    if restrict_to_today:
        day = current.date()
    else:
        day = _ist(ordered[-1]).date()
    return [c for c in ordered if _ist(c).date() == day], day


def opening_candle(
    bars: Sequence[Any],
) -> Any | None:
    """The session's 09:15 candle, which is the opening range.

    Located by clock time rather than by position, so a series missing an
    earlier bar cannot silently shift the range onto a later candle.
    """
    for candle in bars:
        stamp = _ist(candle)
        if stamp.hour == OPENING_HOUR and stamp.minute == OPENING_MINUTE:
            return candle
    return None


def orb_low_rejection_buy_signal(
    candles_15min: Sequence[Any],
    current: dt.datetime,
    restrict_to_today: bool = False,
) -> tuple[str, dict]:
    """Buy a rejection of the opening-range low under a bullish open.

    Returns ("CE", details) when the rule fires and ("NONE", details) when it
    does not, using the ORB strategy's CE/PE vocabulary where CE is the buy
    side.
    """
    bars, session_day = session_candles(candles_15min, current, restrict_to_today)
    base = {
        "strategy": "ORB_LOW_REJECTION",
        "session_day": session_day.isoformat(),
        "session_bars": len(bars),
    }
    if len(bars) < 2:
        return "NONE", {
            **base,
            "reason": "need at least 2 candles in the session: the opening "
                      "candle and one to judge",
        }
    if bars[1].epoch - bars[0].epoch > CANDLE_SECONDS:
        return "NONE", {
            **base,
            "reason": "candles are not 15-minute bars",
            "first_gap_seconds": bars[1].epoch - bars[0].epoch,
        }

    opening = opening_candle(bars)
    if opening is None:
        return "NONE", {
            **base,
            "reason": f"no {OPENING_HOUR:02d}:{OPENING_MINUTE:02d} candle in "
                      "the session, so there is no opening range",
        }

    # A daily series holds one bar per session, so it can never have a second
    # opening candle to judge. The gap check above catches it, but say so
    # plainly rather than reporting a candle-count problem.
    judged = bars[-1]
    if _ist(judged).hour == OPENING_HOUR and _ist(judged).minute == OPENING_MINUTE:
        return "NONE", {
            **base,
            "reason": "only the opening candle is present, and it defines the "
                      "opening range rather than rejecting it",
        }

    opening_open = float(opening.open)
    opening_close = float(opening.close)
    opening_low = float(opening.low)
    close = float(judged.close)
    low = float(judged.low)
    open_ = float(judged.open)

    opening_bullish = opening_close > opening_open
    low_tested = low <= opening_low
    closed_back_above = close > opening_low
    judged_bullish = close > open_

    details = {
        **base,
        "opening_time": _ist(opening).isoformat(),
        "opening_open": opening_open,
        "opening_close": opening_close,
        "opening_low": opening_low,
        "opening_bullish": opening_bullish,
        "orb_low": opening_low,
        "curr_open": open_,
        "curr_high": float(judged.high),
        "curr_low": low,
        "curr_close": close,
        "curr_time": _ist(judged).isoformat(),
        "low_tested": low_tested,
        "closed_back_above": closed_back_above,
        "judged_bullish": judged_bullish,
    }

    if not (opening_bullish and low_tested and closed_back_above
            and judged_bullish):
        if not opening_bullish:
            reason = (f"the opening candle is not bullish: close "
                      f"{opening_close:g} <= open {opening_open:g}")
        elif not low_tested:
            reason = (f"low {low:g} never reached the opening range low "
                      f"{opening_low:g}")
        elif not closed_back_above:
            reason = (f"close {close:g} did not get back above the opening "
                      f"range low {opening_low:g}")
        else:
            reason = (f"the candle is not bullish: close {close:g} <= open "
                      f"{open_:g}")
        return "NONE", {**details, "reason": reason}

    return "CE", {
        **details,
        "trigger": "ORB_LOW_REJECTION",
        "reason": (
            f"opening candle bullish (close {opening_close:g} > open "
            f"{opening_open:g}) and low {low:g} rejected at the opening range "
            f"low {opening_low:g}, closing back above at {close:g}"
        ),
    }
