#!/usr/bin/env python3
"""Double bottom: a 15-minute candle that closes above its own open and
rejects a level on the way.

The rule, as specified:

    close > open
    AND ( rejected the previous trading day's low
          OR rejected the opening-range low )

A rejection at a level ``L`` means the bar traded at or below it and closed
back above it::

    low <= L   and   close > L

The two legs are independent, so one is enough: a bar that dips into the
opening range's low is a double bottom even on a day whose prior low was never
touched, and the reverse holds too. The previous day's low is read from a daily
series because it is a different resolution; the opening-range low is the
session's own 09:15 candle, so it comes from the 15-minute series.

**The previous day's low is optional.** Pass no daily series and that leg is
simply unavailable: it is reported as such and the opening-range leg still
applies. The rule never reports an untested level as a rejection, so a missing
daily series makes the signal rarer rather than wrong. The dashboard always has
a daily series; the rule_agent does not, which is why the leg is optional.

**Judged on every bar of the session.** The dashboard walks all 25 fifteen-minute
bars and shows the newest one that fired, with a count of the rest, so a signal
that formed at 13:30 is reported as having formed at 13:30 rather than being
lost. The judged bar is always the newest of the series handed in, so a caller
that wants a different bar truncates the series first.

This is deliberately looser than the double bottom in
``OrbStrategyCallPut.double_bottom_rejection_signal``, which additionally wants
the close above HMA(21), the bar's low to test the intraday low so far, and RSI
trending up. That stricter pattern is already reported in the ORB column.

The session is taken from the newest bar rather than the wall clock, so a
holiday or weekend still reports the last session's candle instead of going
quiet. ``restrict_to_today`` switches that off for live trading.

This module is deliberately signal-only: no database, order or spreadsheet
plumbing, so the dashboard column and any live caller apply identical rules.

Candles are anything exposing ``epoch``, ``open``, ``high``, ``low`` and
``close``; the dashboard's parsed history rows satisfy that directly.
"""
from __future__ import annotations

import datetime as dt
from typing import Any, Sequence
from zoneinfo import ZoneInfo

MARKET_TIMEZONE = ZoneInfo("Asia/Kolkata")
CANDLE_SECONDS = 15 * 60

#: Bars whose spacing exceeds this are not a 15-minute series. A daily series
#: holds one bar per session, so it is rejected here rather than judged.
MAX_BAR_GAP_SECONDS = CANDLE_SECONDS

#: The opening candle's own time. The opening range is this one candle.
OPENING_HOUR = 9
OPENING_MINUTE = 15

#: Which leg carried the signal, in the details' "rejected" field.
LEG_PREVIOUS_DAY_LOW = "previous_day_low"
LEG_ORB_LOW = "orb_low"


def _ist(candle: Any) -> dt.datetime:
    return dt.datetime.fromtimestamp(candle.epoch, MARKET_TIMEZONE)


def session_candles(
    candles_15min: Sequence[Any],
    current: dt.datetime,
    restrict_to_today: bool = False,
) -> tuple[list[Any], dt.date]:
    """The newest session's candles and the day they belong to."""
    ordered = sorted(candles_15min, key=lambda candle: candle.epoch)
    if not ordered:
        return [], current.date()
    if restrict_to_today:
        day = current.date()
    else:
        day = _ist(ordered[-1]).date()
    return [c for c in ordered if _ist(c).date() == day], day


def previous_session(
    daily: Sequence[Any],
    session_day: dt.date,
) -> Any | None:
    """Most recent daily bar from a session strictly before session_day.

    A same-day daily bar is skipped: it is today's own range, not the previous
    trading day's, and comparing a day with itself would make the level a
    moving target.
    """
    for candle in sorted(daily, key=lambda c: c.epoch, reverse=True):
        if _ist(candle).date() < session_day:
            return candle
    return None


def opening_candle(bars: Sequence[Any]) -> Any | None:
    """The session's 09:15 candle, whose low is the opening-range low.

    Located by clock time rather than by position, so a series missing an
    earlier bar cannot silently shift the level onto a later candle.
    """
    for candle in bars:
        stamp = _ist(candle)
        if stamp.hour == OPENING_HOUR and stamp.minute == OPENING_MINUTE:
            return candle
    return None


def _rejects(level: float, low: float, close: float) -> bool:
    """Whether the bar tested `level` and closed back above it."""
    return low <= level and close > level


def double_bottom_bullish_signal(
    candles_15min: Sequence[Any],
    daily: Sequence[Any],
    current: dt.datetime,
    restrict_to_today: bool = False,
) -> tuple[str, dict]:
    """Buy a bullish candle that rejected the prior low or the opening-range low.

    Returns ("CE", details) when the rule fires and ("NONE", details) when it
    does not, using the ORB strategy's CE/PE vocabulary where CE is the buy
    side.
    """
    bars, session_day = session_candles(candles_15min, current, restrict_to_today)
    base = {
        "strategy": "DOUBLE_BOTTOM_BULLISH",
        "session_day": session_day.isoformat(),
        "session_bars": len(bars),
    }
    if not bars:
        return "NONE", {**base, "reason": "no candles"}

    if len(bars) >= 2 and bars[1].epoch - bars[0].epoch > MAX_BAR_GAP_SECONDS:
        return "NONE", {
            **base,
            "reason": "candles are not 15-minute bars",
            "first_gap_seconds": bars[1].epoch - bars[0].epoch,
        }

    judged = bars[-1]
    open_ = float(judged.open)
    close = float(judged.close)
    low = float(judged.low)
    bullish = close > open_

    opening = opening_candle(bars)
    orb_low = float(opening.low) if opening is not None else None

    # The opening candle *defines* the range low, so judging it would find
    # "low <= that low" trivially true and "close > that low" on any bullish
    # body. The rule needs a later bar to reject the level.
    if opening is not None and opening.epoch == judged.epoch:
        return "NONE", {
            **base,
            "reason": "only the opening candle is present, and it defines the "
                      "opening-range low rather than rejecting it",
        }

    reference = previous_session(daily, session_day)
    previous_day_low = float(reference.low) if reference is not None else None
    previous_day = (_ist(reference).date().isoformat()
                    if reference is not None else None)

    previous_day_rejection = (
        None if previous_day_low is None
        else _rejects(previous_day_low, low, close))
    orb_low_rejection = (
        None if orb_low is None else _rejects(orb_low, low, close))

    legs = [leg for leg in (previous_day_rejection, orb_low_rejection)
            if leg is True]

    details = {
        **base,
        "curr_open": open_,
        "curr_high": float(judged.high),
        "curr_low": low,
        "curr_close": close,
        "curr_time": _ist(judged).isoformat(),
        "bullish": bullish,
        "body": round(close - open_, 4),
        "prev_day_low": (round(previous_day_low, 4)
                         if previous_day_low is not None else None),
        "prev_day": previous_day,
        "prev_day_low_available": previous_day_low is not None,
        "orb_low": (round(orb_low, 4) if orb_low is not None else None),
        "orb_low_available": orb_low is not None,
        "previous_day_rejection": previous_day_rejection,
        "orb_low_rejection": orb_low_rejection,
    }

    if not bullish:
        return "NONE", {
            **details,
            "rejected": None,
            "reason": f"close {close:g} is not above open {open_:g}",
        }
    if not legs:
        return "NONE", {
            **details,
            "rejected": None,
            "reason": _no_rejection_reason(low, close, previous_day_low,
                                           orb_low, previous_day_rejection,
                                           orb_low_rejection),
        }

    if previous_day_rejection and orb_low_rejection:
        leg = f"{LEG_PREVIOUS_DAY_LOW} and {LEG_ORB_LOW}"
        why = (f"low {low:g} rejected the previous day's low "
               f"{previous_day_low:g} and the opening range low {orb_low:g}, "
               f"closing back above both at {close:g}")
    elif previous_day_rejection:
        leg = LEG_PREVIOUS_DAY_LOW
        why = (f"low {low:g} rejected the previous day's low "
               f"{previous_day_low:g}, closing back above it at {close:g}")
    else:
        leg = LEG_ORB_LOW
        why = (f"low {low:g} rejected the opening range low {orb_low:g}, "
               f"closing back above it at {close:g}")

    return "CE", {
        **details,
        "rejected": leg,
        "trigger": "DOUBLE_BOTTOM_BULLISH",
        "reason": (f"15-minute candle closed above its open ({close:g} > "
                   f"{open_:g}) and {why}"),
    }


def _no_rejection_reason(
    low: float,
    close: float,
    previous_day_low: float | None,
    orb_low: float | None,
    previous_day_rejection: bool | None,
    orb_low_rejection: bool | None,
) -> str:
    """Say which level was missed and why, rather than just 'no signal'."""
    tested_any = False
    for level, rejection, name in (
        (previous_day_low, previous_day_rejection, "the previous day's low"),
        (orb_low, orb_low_rejection, "the opening range low"),
    ):
        if level is None or rejection is None:
            continue
        if rejection:
            continue
        if low <= level:
            tested_any = True
            return (f"low {low:g} reached {name} {level:g} but close {close:g} "
                    f"did not get back above it")
    if orb_low is None and previous_day_low is None:
        return "neither the previous day's low nor the opening range low is known"
    if not tested_any:
        return (f"low {low:g} reached neither the previous day's low nor the "
                f"opening range low")
    return f"low {low:g} rejected no level"
