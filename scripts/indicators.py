#!/usr/bin/env python3
"""Technical indicators — thin wrappers around TA-Lib for FYERS candle data.

DEVIATION FROM THIS REPO'S "stdlib-only" CONVENTION: TA-Lib is a compiled C-extension
package (`pip install TA-Lib`, wrapping the TA-Lib C library) and cannot be reimplemented
in pure stdlib without losing speed/accuracy. `talib` is imported lazily inside each
function so importing this module never crashes the rest of the skill's scripts when
TA-Lib isn't installed — see `references/indicators.md` for install steps (the C library
must be installed *before* `pip install TA-Lib` works).

All functions take a `pandas.Series`, plain `list`, or `numpy.ndarray` of prices and
return numpy float64 arrays (TA-Lib's native output), matching the length of the input
with leading `NaN`s for the warm-up/lookback period. Feed them straight from the
candle DataFrame built in `references/backtesting.md` (e.g. `df["close"]`).

Functions
---------
Overlap / trend
    sma(close, timeperiod=20)                 Simple Moving Average
    ema(close, timeperiod=20)                  Exponential Moving Average
    wma(close, timeperiod=20)                  Weighted Moving Average
    bbands(close, timeperiod=20, nbdevup=2, nbdevdn=2)   Bollinger Bands -> (upper, middle, lower)
    adx(high, low, close, timeperiod=14)       Average Directional Index (trend strength)

Momentum
    rsi(close, timeperiod=14)                  Relative Strength Index
    macd(close, fastperiod=12, slowperiod=26, signalperiod=9)  -> (macd, signal, hist)
    stoch(high, low, close, ...)               Stochastic Oscillator -> (%K, %D)
    cci(high, low, close, timeperiod=14)       Commodity Channel Index
    mom(close, timeperiod=10)                  Momentum
    roc(close, timeperiod=10)                  Rate of Change (%)

Volume
    obv(close, volume)                         On-Balance Volume
    ad(high, low, close, volume)               Chaikin Accumulation/Distribution Line
    adosc(high, low, close, volume, ...)       Chaikin A/D Oscillator

Volatility
    atr(high, low, close, timeperiod=14)       Average True Range
    natr(high, low, close, timeperiod=14)      Normalized ATR (%)

Usage
-----
    from scripts.indicators import rsi, bbands

    df["rsi14"] = rsi(df["close"])
    upper, mid, lower = bbands(df["close"], timeperiod=20)

CLI
---
    python scripts/indicators.py demo     # run every indicator on mock OHLCV data
"""
from __future__ import annotations

from typing import Sequence

import numpy as np


def _talib():
    """Lazily import talib with a helpful error if the C library / wrapper is missing."""
    try:
        import talib  # noqa: F401
        return talib
    except ImportError as e:
        raise ImportError(
            "TA-Lib is not installed. It requires the TA-Lib C library on the system "
            "*before* `pip install TA-Lib` will work. See references/indicators.md for "
            "platform-specific install steps (macOS/Linux/Windows)."
        ) from e


def _to_f64(series) -> np.ndarray:
    """Convert a pandas Series / list / ndarray to a contiguous float64 array TA-Lib wants."""
    if hasattr(series, "values"):
        series = series.values
    return np.ascontiguousarray(series, dtype=np.float64)


def _check_len(name: str, n: int, minimum: int) -> None:
    """Raise a clear error instead of TA-Lib's cryptic one when there isn't enough data."""
    if n < minimum:
        raise ValueError(
            f"{name} needs at least {minimum} data point(s), got {n}. "
            f"Pull more candles or shorten the timeperiod."
        )


# ---------------------------------------------------------------------------
# Overlap / trend
# ---------------------------------------------------------------------------

def sma(close: Sequence[float], timeperiod: int = 20) -> np.ndarray:
    """Simple Moving Average — arithmetic mean of the last `timeperiod` closes."""
    ta = _talib()
    arr = _to_f64(close)
    _check_len("sma", len(arr), timeperiod)
    return ta.SMA(arr, timeperiod=timeperiod)


def ema(close: Sequence[float], timeperiod: int = 20) -> np.ndarray:
    """Exponential Moving Average — weights recent closes more heavily than SMA."""
    ta = _talib()
    arr = _to_f64(close)
    _check_len("ema", len(arr), timeperiod)
    return ta.EMA(arr, timeperiod=timeperiod)


def wma(close: Sequence[float], timeperiod: int = 20) -> np.ndarray:
    """Weighted Moving Average — linearly weights each price by its recency."""
    ta = _talib()
    arr = _to_f64(close)
    _check_len("wma", len(arr), timeperiod)
    return ta.WMA(arr, timeperiod=timeperiod)


def bbands(
    close: Sequence[float], timeperiod: int = 20, nbdevup: float = 2.0, nbdevdn: float = 2.0
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Bollinger Bands: SMA middle band +/- nbdev * rolling std deviation.

    Returns (upper, middle, lower). Price near the upper band => overbought context;
    near the lower band => oversold context; a squeeze (bands narrow) often precedes
    a breakout.
    """
    ta = _talib()
    arr = _to_f64(close)
    _check_len("bbands", len(arr), timeperiod)
    upper, middle, lower = ta.BBANDS(arr, timeperiod=timeperiod, nbdevup=nbdevup, nbdevdn=nbdevdn)
    return upper, middle, lower


def adx(high: Sequence[float], low: Sequence[float], close: Sequence[float],
        timeperiod: int = 14) -> np.ndarray:
    """Average Directional Index — trend *strength* (not direction), 0-100.

    Below ~20 = weak/no trend (range-bound); above ~25 = trending market. Commonly
    used to decide whether to run a trend-following vs mean-reversion strategy.
    """
    ta = _talib()
    h, l, c = _to_f64(high), _to_f64(low), _to_f64(close)
    _check_len("adx", len(c), timeperiod * 2)  # ADX needs ~2x timeperiod to stabilize
    return ta.ADX(h, l, c, timeperiod=timeperiod)


# ---------------------------------------------------------------------------
# Momentum
# ---------------------------------------------------------------------------

def rsi(close: Sequence[float], timeperiod: int = 14) -> np.ndarray:
    """Relative Strength Index, 0-100. >70 conventionally overbought, <30 oversold."""
    ta = _talib()
    arr = _to_f64(close)
    _check_len("rsi", len(arr), timeperiod + 1)
    return ta.RSI(arr, timeperiod=timeperiod)


def macd(
    close: Sequence[float], fastperiod: int = 12, slowperiod: int = 26, signalperiod: int = 9
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Moving Average Convergence/Divergence -> (macd, signal, histogram).

    macd crossing above signal => bullish momentum shift; below => bearish.
    histogram = macd - signal, useful for spotting the crossover early.
    """
    ta = _talib()
    arr = _to_f64(close)
    _check_len("macd", len(arr), slowperiod + signalperiod)
    macd_line, signal_line, hist = ta.MACD(
        arr, fastperiod=fastperiod, slowperiod=slowperiod, signalperiod=signalperiod
    )
    return macd_line, signal_line, hist


def stoch(
    high: Sequence[float], low: Sequence[float], close: Sequence[float],
    fastk_period: int = 14, slowk_period: int = 3, slowd_period: int = 3,
) -> tuple[np.ndarray, np.ndarray]:
    """Stochastic Oscillator -> (%K, %D), 0-100. >80 overbought, <20 oversold.

    Measures where the close sits within the recent high-low range.
    """
    ta = _talib()
    h, l, c = _to_f64(high), _to_f64(low), _to_f64(close)
    _check_len("stoch", len(c), fastk_period + slowk_period + slowd_period)
    slowk, slowd = ta.STOCH(
        h, l, c,
        fastk_period=fastk_period, slowk_period=slowk_period, slowk_matype=0,
        slowd_period=slowd_period, slowd_matype=0,
    )
    return slowk, slowd


def cci(high: Sequence[float], low: Sequence[float], close: Sequence[float],
        timeperiod: int = 14) -> np.ndarray:
    """Commodity Channel Index — deviation of price from its statistical mean.

    >100 strong uptrend / overbought, <-100 strong downtrend / oversold.
    """
    ta = _talib()
    h, l, c = _to_f64(high), _to_f64(low), _to_f64(close)
    _check_len("cci", len(c), timeperiod)
    return ta.CCI(h, l, c, timeperiod=timeperiod)


def mom(close: Sequence[float], timeperiod: int = 10) -> np.ndarray:
    """Momentum — raw price difference vs `timeperiod` bars ago (close[t] - close[t-n])."""
    ta = _talib()
    arr = _to_f64(close)
    _check_len("mom", len(arr), timeperiod + 1)
    return ta.MOM(arr, timeperiod=timeperiod)


def roc(close: Sequence[float], timeperiod: int = 10) -> np.ndarray:
    """Rate of Change (%) — like `mom` but expressed as a percentage of the prior price."""
    ta = _talib()
    arr = _to_f64(close)
    _check_len("roc", len(arr), timeperiod + 1)
    return ta.ROC(arr, timeperiod=timeperiod)


# ---------------------------------------------------------------------------
# Volume
# ---------------------------------------------------------------------------

def obv(close: Sequence[float], volume: Sequence[float]) -> np.ndarray:
    """On-Balance Volume — cumulative volume, added on up days and subtracted on down days.

    Rising OBV alongside rising price confirms the trend; divergence warns of weakness.
    """
    ta = _talib()
    c, v = _to_f64(close), _to_f64(volume)
    _check_len("obv", len(c), 1)
    return ta.OBV(c, v)


def ad(high: Sequence[float], low: Sequence[float], close: Sequence[float],
       volume: Sequence[float]) -> np.ndarray:
    """Chaikin Accumulation/Distribution Line — volume-weighted measure of buying/selling
    pressure using where the close sits within the bar's high-low range."""
    ta = _talib()
    h, l, c, v = _to_f64(high), _to_f64(low), _to_f64(close), _to_f64(volume)
    _check_len("ad", len(c), 1)
    return ta.AD(h, l, c, v)


def adosc(
    high: Sequence[float], low: Sequence[float], close: Sequence[float], volume: Sequence[float],
    fastperiod: int = 3, slowperiod: int = 10,
) -> np.ndarray:
    """Chaikin A/D Oscillator — MACD-style fast/slow EMA spread of the A/D line;
    crossing zero signals a shift in accumulation/distribution momentum."""
    ta = _talib()
    h, l, c, v = _to_f64(high), _to_f64(low), _to_f64(close), _to_f64(volume)
    _check_len("adosc", len(c), slowperiod)
    return ta.ADOSC(h, l, c, v, fastperiod=fastperiod, slowperiod=slowperiod)


# ---------------------------------------------------------------------------
# Volatility
# ---------------------------------------------------------------------------

def atr(high: Sequence[float], low: Sequence[float], close: Sequence[float],
        timeperiod: int = 14) -> np.ndarray:
    """Average True Range — absolute volatility, in price units. Common stop-loss sizing
    input (e.g. stop = entry - 2 * ATR)."""
    ta = _talib()
    h, l, c = _to_f64(high), _to_f64(low), _to_f64(close)
    _check_len("atr", len(c), timeperiod + 1)
    return ta.ATR(h, l, c, timeperiod=timeperiod)


def natr(high: Sequence[float], low: Sequence[float], close: Sequence[float],
         timeperiod: int = 14) -> np.ndarray:
    """Normalized ATR (%) — ATR expressed as a percentage of close, comparable across
    instruments/price levels (unlike raw ATR)."""
    ta = _talib()
    h, l, c = _to_f64(high), _to_f64(low), _to_f64(close)
    _check_len("natr", len(c), timeperiod + 1)
    return ta.NATR(h, l, c, timeperiod=timeperiod)


# ---------------------------------------------------------------------------
# CLI demo
# ---------------------------------------------------------------------------

def _demo() -> int:
    """Run every indicator against synthetic OHLCV data — no token, no live import needed
    beyond TA-Lib itself."""
    rng = np.random.default_rng(42)
    n = 120
    steps = rng.normal(loc=0.05, scale=1.0, size=n)
    close = 100 + np.cumsum(steps)
    high = close + rng.uniform(0.1, 1.0, size=n)
    low = close - rng.uniform(0.1, 1.0, size=n)
    volume = rng.uniform(1_000, 10_000, size=n)

    def last(arr) -> float:
        return round(float(arr[-1]), 4)

    print(f"bars: {n}")
    print(f"{'sma(20)':15s} -> {last(sma(close, 20))}")
    print(f"{'ema(20)':15s} -> {last(ema(close, 20))}")
    print(f"{'wma(20)':15s} -> {last(wma(close, 20))}")

    upper, mid, lower = bbands(close, timeperiod=20)
    print(f"{'bbands':15s} -> upper={last(upper)} mid={last(mid)} lower={last(lower)}")

    print(f"{'adx(14)':15s} -> {last(adx(high, low, close, 14))}")
    print(f"{'rsi(14)':15s} -> {last(rsi(close, 14))}")

    macd_line, signal_line, hist = macd(close)
    print(f"{'macd':15s} -> macd={last(macd_line)} signal={last(signal_line)} hist={last(hist)}")

    slowk, slowd = stoch(high, low, close)
    print(f"{'stoch':15s} -> %K={last(slowk)} %D={last(slowd)}")

    print(f"{'cci(14)':15s} -> {last(cci(high, low, close, 14))}")
    print(f"{'mom(10)':15s} -> {last(mom(close, 10))}")
    print(f"{'roc(10)':15s} -> {last(roc(close, 10))}")

    print(f"{'obv':15s} -> {last(obv(close, volume))}")
    print(f"{'ad':15s} -> {last(ad(high, low, close, volume))}")
    print(f"{'adosc':15s} -> {last(adosc(high, low, close, volume))}")

    print(f"{'atr(14)':15s} -> {last(atr(high, low, close, 14))}")
    print(f"{'natr(14)':15s} -> {last(natr(high, low, close, 14))}")

    # Error-path smoke test: too little data should give a clear error, not a TA-Lib crash.
    try:
        rsi(close[:5], timeperiod=14)
    except ValueError as e:
        print(f"\nrsi(5 bars, timeperiod=14) raised: {e}")

    return 0


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "demo":
        raise SystemExit(_demo())
    print(__doc__)
