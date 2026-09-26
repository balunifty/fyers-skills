#!/usr/bin/env python3
"""Lower-high continuation sell (15-minute).

Signals SELL when the bar being judged makes a **lower high** than the bar
before it and also **closes below** that bar's close:

    high < previous high   (LH)
    close < previous close

The mirror of a higher high. A bar that cannot reach the prior high and still
finishes under the prior close is failing to make progress, which reads as
continuation of the weakness rather than a pause in it.

Only the two bars either side of the one being judged matter, so the series is
judged on its own last two bars and a caller can walk a session by truncating
it.

This module is deliberately signal-only: it holds no database, order or
spreadsheet plumbing, so the dashboard column and any caller evaluate exactly
the same rules from exactly the same code.

Candles are anything exposing ``epoch``, ``open``, ``high``, ``low`` and
``close``; the dashboard's parsed history rows satisfy that directly.
"""
from __future__ import annotations

import datetime as dt
from typing import Any, Sequence
from zoneinfo import ZoneInfo

MARKET_TIMEZONE = ZoneInfo("Asia/Kolkata")
CANDLE_SECONDS = 15 * 60
STRATEGY_NAME = "LOWER_HIGH_CLOSE"

#: The bar and the one before it are both needed.
BARS_REQUIRED = 2


def _ist(candle: Any) -> dt.datetime:
    return dt.datetime.fromtimestamp(candle.epoch, MARKET_TIMEZONE)


def lower_high_close_sell_signal(
    candles: Sequence[Any],
    bar_seconds: int = CANDLE_SECONDS,
) -> tuple[str, dict]:
    """Sell on a lower high that also closes below the previous close.

    Returns ("PE"|"NONE", details), using the PE vocabulary the rejection
    columns already share.
    """
    ordered = sorted(candles, key=lambda candle: candle.epoch)
    # Every return carries a "rule" key so a caller can rely on the shape.
    if len(ordered) < BARS_REQUIRED:
        return "NONE", {"rule": None, "bars": len(ordered),
                        "reason": f"need at least {BARS_REQUIRED} candles"}

    previous, current = ordered[-2], ordered[-1]
    high = float(current.high)
    close = float(current.close)
    previous_high = float(previous.high)
    previous_close = float(previous.close)
    lower_high = high < previous_high
    closed_lower = close < previous_close
    started = _ist(current)

    details = {
        "strategy": STRATEGY_NAME,
        "rule": "lower_high_close",
        "candle_epoch": current.epoch,
        "curr_time": started.isoformat(),
        # Declared so a caller aggregating several timeframes stamps this cell
        # with the close of the bar that actually produced it.
        "bar_seconds": bar_seconds,
        "open": float(current.open), "high": high, "low": float(current.low),
        "close": close,
        "curr_open": float(current.open), "curr_high": high,
        "curr_low": float(current.low), "curr_close": close,
        "previous_open": float(previous.open),
        "previous_high": previous_high, "previous_low": float(previous.low),
        "previous_close": previous_close,
        "previous_time": _ist(previous).isoformat(),
        "lower_high": lower_high,
        "closed_below_previous": closed_lower,
    }

    if lower_high and closed_lower:
        return "PE", {
            **details,
            "reason": f"lower high {high:g} < {previous_high:g} and close "
                      f"{close:g} < {previous_close:g}"}
    if not lower_high:
        reason = (f"high {high:g} is not below the previous bar's "
                  f"{previous_high:g}")
    else:
        reason = f"close {close:g} did not fall below {previous_close:g}"
    return "NONE", {**details, "reason": reason}
