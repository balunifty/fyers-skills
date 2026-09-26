#!/usr/bin/env python3
"""Rejection or bounce at a level taken from the previous session.

Two levels, both from the prior trading day:

``r1``
    The classic pivot: ``P = (H + L + C) / 3`` and ``R1 = 2P - L``.
``prev_high``
    The previous day's high itself.

Selling needs the bar to test the level and close back beneath it. Buying is the
mirror image, where the level holds and price closes back above it.

The reference session is always strictly before the day being judged, so a
holiday, a weekend and the end-of-day view all measure against a real prior
session rather than comparing a day with itself.

This module is deliberately signal-only: no database, order or spreadsheet
plumbing. It was extracted from the dashboard's own implementation so the
dashboard column and any live caller apply identical rules; before the
extraction each had its own copy, which is how a UI file ended up owning a
trading rule.

Candles are anything exposing ``epoch``, ``open``, ``high``, ``low`` and
``close``.
"""
from __future__ import annotations

import datetime as dt
from typing import Any, Sequence
from zoneinfo import ZoneInfo

MARKET_TIMEZONE = ZoneInfo("Asia/Kolkata")

#: The level kinds this module understands.
LEVEL_R1 = "r1"
LEVEL_PREV_HIGH = "prev_high"

#: At least two daily bars, so a strictly-earlier session can be found.
MINIMUM_DAILY_BARS = 2


def _ist(candle: Any) -> dt.datetime:
    return dt.datetime.fromtimestamp(candle.epoch, MARKET_TIMEZONE)


def previous_session(
    daily: Sequence[Any],
    session_day: dt.date,
) -> Any | None:
    """Most recent daily bar from a session strictly before session_day."""
    for candle in sorted(daily, key=lambda c: c.epoch, reverse=True):
        if _ist(candle).date() < session_day:
            return candle
    return None


def level_from_session(
    reference: Any,
    level: str,
) -> tuple[float, str]:
    """The price to test, and the name to call it in a reason.

    R1 is the classic pivot 2P - L; the previous day's high is itself.
    """
    high = float(reference.high)
    low = float(reference.low)
    close = float(reference.close)
    if level == LEVEL_R1:
        pivot = (high + low + close) / 3
        return 2 * pivot - low, "R1"
    return high, "Prev day high"


def level_rejection_signal(
    candles_15min: Sequence[Any],
    daily: Sequence[Any],
    level: str = LEVEL_R1,
    current: dt.datetime | None = None,
) -> tuple[str, dict]:
    """Rejection (PE) or bounce (CE) at a level from the prior session.

    Returns ("PE"|"CE"|"NONE", details), using the ORB strategy's CE/PE
    vocabulary where CE is the buy side.
    """
    if not candles_15min or len(daily) < MINIMUM_DAILY_BARS:
        return "NONE", {
            "reason": (
                f"need 15-minute candles and at least {MINIMUM_DAILY_BARS} "
                "daily bars"
            ),
            "intraday_bars": len(candles_15min),
            "daily_bars": len(daily),
        }

    ordered = sorted(candles_15min, key=lambda candle: candle.epoch)
    judged = ordered[-1]
    if current is None:
        session_day = _ist(judged).date()
    else:
        session_day = current.date() if current.tzinfo is None else \
            current.astimezone(MARKET_TIMEZONE).date()
        if _ist(judged).date() != session_day:
            # The judged bar belongs to another day, so measure against the
            # day it actually belongs to rather than the caller's clock.
            session_day = _ist(judged).date()

    reference = previous_session(daily, session_day)
    if reference is None:
        return "NONE", {
            "reason": "no daily bar from a session before the one being judged",
            "session_day": session_day.isoformat(),
        }

    price, name = level_from_session(reference, level)
    high = float(reference.high)
    low = float(reference.low)
    close = float(reference.close)

    details = {
        "strategy": f"{name.upper()} REJECTION",
        "level": round(price, 2),
        "previous_day": _ist(reference).date().isoformat(),
        "pivot": round((high + low + close) / 3, 2),
        "previous_high": high,
        "previous_low": low,
        "previous_close": close,
        "curr_open": float(judged.open),
        "curr_high": float(judged.high),
        "curr_low": float(judged.low),
        "curr_close": float(judged.close),
        "curr_time": _ist(judged).isoformat(),
        "level_kind": level,
    }

    if judged.high >= price and judged.close < price:
        return "PE", {
            **details,
            "reason": f"high {judged.high:g} tested {name} {price:g} but the "
                      f"close {judged.close:g} fell back below it",
        }
    if judged.low <= price and judged.close > price:
        return "CE", {
            **details,
            "reason": f"low {judged.low:g} tested {name} {price:g} and the "
                      f"close {judged.close:g} held above it",
        }
    return "NONE", {
        **details,
        "reason": f"the bar neither rejected nor held {name} {price:g}",
    }
