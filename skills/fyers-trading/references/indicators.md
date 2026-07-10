# Technical Indicators with TA-Lib

`scripts/indicators.py` wraps [**TA-Lib**](https://github.com/ta-lib/ta-lib-python) —
the Python binding around the TA-Lib **C library**. Use it instead of hand-rolling
indicator math with `pandas.rolling()`: TA-Lib's implementations are fast (compiled C,
not Python loops), battle-tested (the reference implementation most charting platforms
and brokers use), and correct on edge cases (warm-up/lookback periods, smoothing
methods) that are easy to get subtly wrong rolling your own.

## Install (read this before importing `scripts/indicators.py`)

TA-Lib-python is a **wrapper around a C library** — `pip install TA-Lib` fails unless
that C library is present on the system first. This is the exact "install error /
package not available" pattern covered generally in `references/setup.md` §3; the
steps below are the specific fix for TA-Lib.

**Debian/Ubuntu/Linux:** apt's `ta-lib` package (where it exists) is usually too old;
build the C library from source, then install the Python wrapper:
```bash
sudo apt-get install python3-dev build-essential   # if headers/compiler are missing
wget http://prdownloads.sourceforge.net/ta-lib/ta-lib-0.7.1-src.tar.gz
tar -xzf ta-lib-0.7.1-src.tar.gz && cd ta-lib-0.7.1/
./configure --prefix=/usr
make                        # if `make -jX` fails partway, just rerun `make -jX`
sudo make install
python -m pip install TA-Lib
```

**Windows:** don't build from source — install the prebuilt library first (the
`ta-lib-0.7.1-windows-x86_64.msi` installer, or the matching `.zip`, from the
[ta-lib-python releases](https://github.com/ta-lib/ta-lib-python)), then:
```powershell
python -m pip install TA-Lib
```

**Conda (any OS):**
```bash
conda install -c conda-forge libta-lib ta-lib
```

Check current exact versions/commands against the
[ta-lib-python README](https://github.com/ta-lib/ta-lib-python) — the source tarball
version number changes over time.

`scripts/indicators.py` imports `talib` **lazily inside each function**, so the rest of
the skill's scripts still work with TA-Lib absent; you only hit the install requirement
when you actually call an indicator.

## Feeding FYERS candle data in

TA-Lib requires **numpy `float64` arrays**, not pandas Series directly. Pass a Series'
`.values.astype(float)` (or just the Series/list — `scripts/indicators.py`'s wrappers
convert for you via `_to_f64()`). Worked example on `NSE:SBIN-EQ`, building on the
candle DataFrame pattern from `references/backtesting.md`:

```python
import pandas as pd
from scripts.fyers_client import FyersClient
from scripts.indicators import rsi, bbands

fc = FyersClient()
raw = fc.history("NSE:SBIN-EQ", resolution="5",
                 range_from="2024-01-01", range_to="2024-03-31")
df = pd.DataFrame(raw["candles"], columns=["epoch", "open", "high", "low", "close", "volume"])

df["rsi14"] = rsi(df["close"], timeperiod=14)          # Series -> np.ndarray, aligned by position
upper, mid, lower = bbands(df["close"], timeperiod=20)
df["bb_upper"], df["bb_mid"], df["bb_lower"] = upper, mid, lower

print(df[["close", "rsi14", "bb_upper", "bb_lower"]].tail())
```

Every wrapper returns a plain numpy array the same length as the input, with leading
`NaN`s for the warm-up period — assign it straight into a DataFrame column. If you'd
rather call TA-Lib directly instead of the wrappers, remember the explicit cast:
```python
close = df["close"].values.astype(float)
```

## Indicators implemented (`scripts/indicators.py`)

| Function | Category | What it measures / when to use it |
|---|---|---|
| `sma(close, timeperiod)` | Overlap/trend | Simple moving average — smooths price, defines a trend baseline. |
| `ema(close, timeperiod)` | Overlap/trend | Exponential moving average — reacts faster to recent price than SMA. |
| `wma(close, timeperiod)` | Overlap/trend | Weighted moving average — linear recency weighting. |
| `bbands(close, timeperiod, nbdevup, nbdevdn)` | Overlap/trend | Bollinger Bands — volatility envelope around an SMA; squeeze/breakout and overbought/oversold context. |
| `adx(high, low, close, timeperiod)` | Overlap/trend | Trend *strength* (0-100), not direction; low = range-bound, high = trending. |
| `rsi(close, timeperiod)` | Momentum | 0-100 oscillator; >70 conventionally overbought, <30 oversold. |
| `macd(close, fastperiod, slowperiod, signalperiod)` | Momentum | MACD/signal/histogram — momentum shift and crossover signals. |
| `stoch(high, low, close, ...)` | Momentum | %K/%D — where close sits within the recent high-low range. |
| `cci(high, low, close, timeperiod)` | Momentum | Deviation from statistical mean price; >100/<-100 = strong trend/extreme. |
| `mom(close, timeperiod)` | Momentum | Raw price change vs N bars ago. |
| `roc(close, timeperiod)` | Momentum | Percentage price change vs N bars ago. |
| `obv(close, volume)` | Volume | Cumulative volume flow; confirms or diverges from price trend. |
| `ad(high, low, close, volume)` | Volume | Chaikin A/D line — volume-weighted buying/selling pressure. |
| `adosc(high, low, close, volume, fastperiod, slowperiod)` | Volume | MACD-style oscillator on the A/D line; zero-crossings signal momentum shifts. |
| `atr(high, low, close, timeperiod)` | Volatility | Average True Range — absolute volatility in price units; common stop-loss sizing input. |
| `natr(high, low, close, timeperiod)` | Volatility | ATR normalized to % of price; comparable across instruments. |

Each wrapper validates the input length against TA-Lib's minimum lookback and raises a
plain `ValueError` (e.g. `"rsi needs at least 15 data point(s), got 5"`) instead of
letting TA-Lib fail cryptically or silently return all-`NaN`.

## Smoke test without a token

```bash
python scripts/indicators.py demo
```
Runs every indicator against synthetic OHLCV data (no FYERS token or network call
needed) and prints the last value of each — use it to confirm TA-Lib is installed
correctly before wiring indicators into a real strategy.
