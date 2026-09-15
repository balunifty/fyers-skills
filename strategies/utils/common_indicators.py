"""Pure-Python technical indicators shared by strategy implementations."""
from __future__ import annotations

from typing import Sequence


def ema(values: list[float], period: int) -> list[float | None]:
    """Return EMA values seeded with the SMA of the first period values."""
    if period <= 0:
        raise ValueError("period must be positive")
    if len(values) < period:
        return [None] * len(values)
    result: list[float | None] = [None] * (period - 1)
    current = sum(values[:period]) / period
    result.append(current)
    alpha = 2 / (period + 1)
    for value in values[period:]:
        current += alpha * (value - current)
        result.append(current)
    return result


def wma(values: list[float], period: int) -> list[float | None]:
    """Return linearly weighted moving-average values."""
    if period <= 0:
        raise ValueError("period must be positive")
    if len(values) < period:
        return [None] * len(values)
    denominator = period * (period + 1) / 2
    result: list[float | None] = [None] * (period - 1)
    for index in range(period - 1, len(values)):
        window = values[index - period + 1:index + 1]
        result.append(sum(value * weight for value, weight in
                          zip(window, range(1, period + 1))) / denominator)
    return result


def hma(values: list[float], period: int = 21) -> list[float | None]:
    """Return HMA values using WMA(2*WMA(n/2)-WMA(n), sqrt(n))."""
    if period <= 0:
        raise ValueError("period must be positive")
    half_period = period // 2
    smoothing_period = max(1, int(period ** 0.5))
    half_wma = wma(values, half_period)
    full_wma = wma(values, period)
    difference = [
        2 * half_wma[index] - full_wma[index]
        if half_wma[index] is not None and full_wma[index] is not None else None
        for index in range(len(values))
    ]
    first_valid = next((index for index, value in enumerate(difference) if value is not None), len(values))
    smoothed = wma([value for value in difference if value is not None], smoothing_period)
    result: list[float | None] = [None] * len(values)
    for offset, value in enumerate(smoothed):
        result[first_valid + offset] = value
    return result


def rsi(values: list[float], period: int = 14) -> list[float | None]:
    """Return Wilder RSI values in the range 0..100."""
    if period <= 0:
        raise ValueError("period must be positive")
    if len(values) <= period:
        return [None] * len(values)
    changes = [values[index] - values[index - 1] for index in range(1, len(values))]
    gains = [max(change, 0.0) for change in changes]
    losses = [max(-change, 0.0) for change in changes]
    average_gain = sum(gains[:period]) / period
    average_loss = sum(losses[:period]) / period
    result: list[float | None] = [None] * period

    def current_rsi() -> float:
        if average_loss == 0:
            return 100.0
        relative_strength = average_gain / average_loss
        return 100 - (100 / (1 + relative_strength))

    result.append(current_rsi())
    for gain, loss in zip(gains[period:], losses[period:]):
        average_gain = ((average_gain * (period - 1)) + gain) / period
        average_loss = ((average_loss * (period - 1)) + loss) / period
        result.append(current_rsi())
    return result


def adx(candles: Sequence[object], period: int = 14) -> list[float | None]:
    """Return Wilder ADX values for objects exposing high, low, and close."""
    if period <= 0:
        raise ValueError("period must be positive")
    if len(candles) <= period * 2:
        return [None] * len(candles)
    true_ranges = [0.0]
    plus_dm = [0.0]
    minus_dm = [0.0]
    for previous, current in zip(candles, candles[1:]):
        true_ranges.append(max(current.high - current.low,
                               abs(current.high - previous.close),
                               abs(current.low - previous.close)))
        up_move = current.high - previous.high
        down_move = previous.low - current.low
        plus_dm.append(up_move if up_move > down_move and up_move > 0 else 0.0)
        minus_dm.append(down_move if down_move > up_move and down_move > 0 else 0.0)

    true_range = sum(true_ranges[1:period + 1])
    plus = sum(plus_dm[1:period + 1])
    minus = sum(minus_dm[1:period + 1])
    dx: list[float | None] = [None] * period
    for index in range(period, len(candles)):
        if index > period:
            true_range = true_range - true_range / period + true_ranges[index]
            plus = plus - plus / period + plus_dm[index]
            minus = minus - minus / period + minus_dm[index]
        plus_di = 100 * plus / true_range if true_range else 0.0
        minus_di = 100 * minus / true_range if true_range else 0.0
        denominator = plus_di + minus_di
        dx.append(100 * abs(plus_di - minus_di) / denominator if denominator else 0.0)

    first_adx = sum(value for value in dx[period:period * 2] if value is not None) / period
    result: list[float | None] = [None] * (period * 2 - 1)
    result.append(first_adx)
    current = first_adx
    for value in dx[period * 2:]:
        current = ((current * (period - 1)) + (value or 0.0)) / period
        result.append(current)
    return result
