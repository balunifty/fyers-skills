#!/usr/bin/env python3
"""R1 Rejection, Previous-Day-High Rejection & Doji Rejection for F&O stocks.

Three mean-reversion entries on completed 15-minute candles.

  Levels (from the previous completed session's daily bar H / L / C):
    Pivot P = (H + L + C) / 3
    R1     = 2P - L            (classic first resistance)
    PrevHigh = H

  1. R1 rejection            - price tests R1 and closes back below  -> buy PE
  2. Previous-day high rej.  - price tests PrevHigh, closes back below -> buy PE
  3. R1 / PrevHigh bounce    - price dips to the level, closes back above -> buy CE

  A "test" must breach the level by at least MIN_REJECTION_PERCENT, so a graze
  of the level is not treated as a rejection.

  4. Doji rejection - a doji marks indecision and the next bar fails to hold it.
     A bar is a doji when its body is under DOJI_MAX_BODY_RATIO of its range.
     Three sell-side triggers, checked most-specific first:
       DOJI_BREAKOUT_FAIL  opens at/above the doji high, no upper wick
                          (high == open), then closes below the doji low
       DOJI_OPEN_HIGH      no upper wick (high == open) and closes below the
                          doji low
       DOJI_REJECTION      high reaches the doji high and the close falls back
                          below the doji low (no wick requirement)
     All three buy PE. There is deliberately no buy-side mirror.

  5. Higher-high rejection - the bar makes a new high, looks bullish, then
     fails and closes back down. Two strictness levels, both buy PE:
       HIGHER_HIGH_LOW_CLOSE   close below the PREVIOUS BAR'S LOW (clean
                               outside-bar reversal, rare)
       HIGHER_HIGH_CLOSE       close below the PREVIOUS BAR'S CLOSE (looser,
                               fires on most down candles that poke a high)

Risk:
  - Stop loss uses the tighter of the config fixed stop and the trailing stop.
  - One entry per symbol per day, tracked in ENTERED_PATH.
  - Entries are suspended while NIFTY gaps beyond GAP_THRESHOLD_PERCENT.
  - Live orders require both --live and "place_order": "YES" in the F&O config.

Usage:
    python strategies/scripts/R1PrevHighRejectionStrategy.py            # dry-run
    python strategies/scripts/R1PrevHighRejectionStrategy.py --live     # live
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
import sqlite3
import sys
import time
from dataclasses import dataclass
from zoneinfo import ZoneInfo

SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parents[1]
STRATEGIES_DIR = SCRIPT_DIR.parent
LOG_PATH = STRATEGIES_DIR / "logs" / "R1PrevHighRejection.log"
LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
LOG_PATH.touch(exist_ok=True)
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "strategies"))
sys.path.insert(0, str(REPO_ROOT / "strategies" / "utils"))
sys.path.insert(0, str(REPO_ROOT / "skills" / "fyers-trading" / "scripts"))
sys.path.insert(0, str(STRATEGIES_DIR / "config"))
sys.path.insert(0, str(SCRIPT_DIR))

from fyers_client import FyersAuthError, FyersClient  # noqa: E402
from order_config import (  # noqa: E402
    ConfigGatedFyersClient,
    get_entry_qty,
    get_stop_loss_percent,
    get_trailing_stop_loss_percent,
)
from excel_logger import log_to_excel  # noqa: E402

STOCKS_PATH = STRATEGIES_DIR / "data" / "NiftyFNOTop100.txt"
DATABASE_PATH_15MIN = STRATEGIES_DIR / "databases" / "r1_rejection_15min.db"
ENTERED_PATH = STRATEGIES_DIR / "data" / "entered_r1_rejection.json"
POSITIONS_PATH = STRATEGIES_DIR / "data" / "r1_rejection_positions.json"
STRATEGY_NAME = "R1_PREV_HIGH_REJECTION"
SCRIPT_NAME = "R1PrevHighRejectionStrategy.py"

# --- Tuning constants --------------------------------------------------------
LIVE = False                         # True = send real orders
SKIP_MARKET_HOURS = False            # True = run outside market hours (test mode)
FORCE_ENTRY = False                  # True = place a test order ignoring the signal
POLL_SECONDS = 300                   # 5 minutes
HISTORY_DAYS = 30                    # calendar days of 15-minute history
DAILY_HISTORY_DAYS = 60              # calendar days of daily history
MARKET_TIMEZONE = ZoneInfo("Asia/Kolkata")
MARKET_OPEN = dt.time(9, 15)
MARKET_CLOSE = dt.time(15, 30)
MAX_API_CALLS_PER_SECOND = 5
ENTRY_ORDER_TYPE = 1                 # 1 = Limit, 2 = Market
ENTRY_PRODUCT_TYPE = "INTRADAY"
ENTRY_CANDLE_MINUTES = 15            # trigger timeframe
MIN_BARS_TODAY = 3                   # completed bars required before signalling
MIN_REJECTION_PERCENT = 0.05         # level must be breached by 0.05% to count
CE_OPTION_TYPE = "CE"
PE_OPTION_TYPE = "PE"
GAP_THRESHOLD_PERCENT = 0.5          # suspend entries if NIFTY gaps more
LIMIT_BUFFER_PERCENT = 0.10          # 10% of candle range for the limit buffer
EXIT_BUFFER_PERCENT = 0.02           # 2% buffer when exiting
# --------------------------------------------------------------------------------

LEVEL_R1 = "r1"
LEVEL_PREV_HIGH = "prev_high"
KIND_DOJI = "doji"
KIND_HIGHER_HIGH = "higher_high"
KIND_HIGHER_HIGH_CLOSE = "higher_high_close"

# Doji rejection tuning
DOJI_MAX_BODY_RATIO = 0.10       # body must be under 10% of the candle's range
NO_WICK_TOLERANCE_PCT = 0.01     # high may exceed open by at most 0.01% to count
                                # as having no upper wick

# Doji rejection trigger names, most specific first
DOJI_BREAKOUT_FAIL = "DOJI_BREAKOUT_FAIL"
DOJI_OPEN_HIGH = "DOJI_OPEN_HIGH"
DOJI_REJECTION = "DOJI_REJECTION"


@dataclass(frozen=True)
class Candle:
    epoch: int
    open: float
    high: float
    low: float
    close: float
    volume: float


class ApiRateLimiter:
    def __init__(self, calls_per_second: int = MAX_API_CALLS_PER_SECOND):
        if calls_per_second <= 0:
            raise ValueError("calls_per_second must be positive")
        self.interval = 1.0 / calls_per_second
        self.last_call = 0.0

    def call(self, function, *args, **kwargs):
        elapsed = time.monotonic() - self.last_call
        if elapsed < self.interval:
            time.sleep(self.interval - elapsed)
        self.last_call = time.monotonic()
        return function(*args, **kwargs)


# =============================================================================
# Logging
# =============================================================================
def log_message(msg: str) -> None:
    timestamp = dt.datetime.now(MARKET_TIMEZONE).strftime("%H:%M:%S")
    line = f"{timestamp} {msg}"
    print(line, file=sys.stderr)
    try:
        with open(LOG_PATH, "a") as handle:
            handle.write(line + "\n")
    except OSError:
        pass


def log_error(msg: str) -> None:
    log_message(f"ERROR: {msg}")


def print_entry_result(symbol: str, order_id: str, response: dict) -> None:
    log_message(f"ENTRY_RESPONSE [{symbol}] id={order_id} s={response.get('s')}")


def clear_log_on_new_day() -> None:
    today = dt.date.today().isoformat()
    if LOG_PATH.exists():
        first_line = LOG_PATH.read_text(encoding="utf-8").splitlines()
        if first_line and today not in first_line[0]:
            LOG_PATH.write_text(f"{today}\n", encoding="utf-8")


def market_open() -> bool:
    if SKIP_MARKET_HOURS:
        return True
    now = dt.datetime.now(MARKET_TIMEZONE)
    return MARKET_OPEN <= now.time() <= MARKET_CLOSE and now.weekday() < 5


# =============================================================================
# State
# =============================================================================
def read_stocks() -> list[str]:
    if not STOCKS_PATH.exists():
        return []
    return list(dict.fromkeys(
        line.strip()
        for line in STOCKS_PATH.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ))


def load_entered() -> set[str]:
    if ENTERED_PATH.exists():
        try:
            data = json.loads(ENTERED_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return set()
        if isinstance(data, list):
            return {str(item) for item in data}
    return set()


def save_entered(data: set[str]) -> None:
    ENTERED_PATH.parent.mkdir(parents=True, exist_ok=True)
    ENTERED_PATH.write_text(json.dumps(sorted(data)), encoding="utf-8")


def clear_entered_on_new_day() -> None:
    today = dt.date.today().isoformat()
    if not ENTERED_PATH.exists():
        return
    try:
        data = json.loads(ENTERED_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return
    if isinstance(data, list) and data and today not in str(data[0]):
        ENTERED_PATH.write_text("[]", encoding="utf-8")


def load_positions() -> dict:
    if POSITIONS_PATH.exists():
        try:
            data = json.loads(POSITIONS_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}
        if isinstance(data, dict):
            return data
    return {}


def save_positions(data: dict) -> None:
    POSITIONS_PATH.parent.mkdir(parents=True, exist_ok=True)
    POSITIONS_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")


# =============================================================================
# Candle storage
# =============================================================================
def table_name(symbol: str, interval: str) -> str:
    return f"candles_{symbol.replace(':', '_').replace('-', '_')}_{interval}min"


def initialize_database(connection: sqlite3.Connection, table: str) -> None:
    connection.execute(f"""
        CREATE TABLE IF NOT EXISTS {table} (
            epoch  INTEGER PRIMARY KEY,
            open   REAL,
            high   REAL,
            low    REAL,
            close  REAL,
            volume REAL
        )
    """)
    connection.commit()


def store_candles(connection: sqlite3.Connection, table: str, candles: list[Candle]) -> None:
    connection.executemany(
        f"INSERT OR REPLACE INTO {table} (epoch, open, high, low, close, volume) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        [(c.epoch, c.open, c.high, c.low, c.close, c.volume) for c in candles],
    )
    connection.commit()


def parse_candles(response: dict) -> list[Candle]:
    candles: list[Candle] = []
    for row in response.get("candles") or []:
        if not isinstance(row, (list, tuple)) or len(row) < 6:
            continue
        try:
            candles.append(Candle(
                epoch=int(row[0]), open=float(row[1]), high=float(row[2]),
                low=float(row[3]), close=float(row[4]), volume=float(row[5]),
            ))
        except (TypeError, ValueError):
            continue
    return sorted(candles, key=lambda c: c.epoch)


def fetch_candles(client: FyersClient, limiter: ApiRateLimiter, symbol: str,
                  resolution: str, days: int, current: dt.datetime) -> list[Candle]:
    """Fetch history, keeping only bars that have already closed."""
    start = (current.date() - dt.timedelta(days=days)).isoformat()
    end = current.date().isoformat()
    response = limiter.call(client.history, symbol, resolution, start, end)
    if response.get("s") != "ok":
        raise RuntimeError(f"history fetch failed for {symbol}: {response}")

    seconds = {"15": ENTRY_CANDLE_MINUTES * 60, "D": 0}.get(resolution, 0)
    now_epoch = int(current.timestamp())
    candles: list[Candle] = []
    for candle in parse_candles(response):
        if seconds and candle.epoch + seconds > now_epoch:
            continue
        if not seconds:
            # A daily bar is only complete once the session has ended.
            bar_day = dt.datetime.fromtimestamp(candle.epoch, MARKET_TIMEZONE).date()
            if bar_day >= current.date():
                continue
        candles.append(candle)
    return candles


def fetch_symbol_candles(client: FyersClient, connection: sqlite3.Connection, symbol: str,
                         table: str, resolution: str, days: int,
                         limiter: ApiRateLimiter) -> list[Candle]:
    current = dt.datetime.now(MARKET_TIMEZONE)
    candles = fetch_candles(client, limiter, symbol, resolution, days, current)
    if candles:
        store_candles(connection, table, candles)
    return candles


# =============================================================================
# Option resolution
# =============================================================================
def round_to_strike(price: float, step: int) -> float:
    if step <= 0:
        return price
    return round(price / step) * step


def get_strike_step(chains: list[dict]) -> int:
    strikes = sorted({
        item.get("strike_price", 0) for item in chains if item.get("strike_price", 0) > 0
    })
    if len(strikes) < 2:
        return 50
    diffs = [strikes[i + 1] - strikes[i] for i in range(len(strikes) - 1)]
    return int(min(diffs)) if diffs else 50


def resolve_atm_option(client: FyersClient, stock_symbol: str, ltp: float,
                       option_type: str, limiter: ApiRateLimiter) -> tuple[str, int] | None:
    """Resolve the nearest-expiry ATM option and its lot size."""
    response = limiter.call(client.option_chain, stock_symbol, strikecount=5, greeks=False)
    if response.get("s") != "ok":
        log_error(f"option_chain failed for {stock_symbol}: {response}")
        return None

    data = response.get("data", {})
    chains = data.get("optionsChain", []) or data.get("chain", [])
    if not chains:
        log_error(f"OPTION_CHAIN_EMPTY: {stock_symbol}")
        return None

    strike_step = get_strike_step(chains)
    atm_strike = round_to_strike(ltp, strike_step)

    best_symbol = None
    best_diff = float("inf")
    nearest_expiry = None
    for item in chains:
        if (item.get("option_type") or item.get("optType")) != option_type:
            continue
        strike = item.get("strike_price", 0)
        diff = abs(strike - atm_strike)
        if diff < best_diff:
            best_diff = diff
            best_symbol = item.get("symbol", "")
            nearest_expiry = item.get("expiryDate", "") or item.get("expiry_date", "")

    if not best_symbol:
        log_error(f"ATM_OPTION_NOT_FOUND: {stock_symbol} ltp={ltp} "
                  f"atm_strike={atm_strike} type={option_type}")
        return None

    try:
        from fyers_symbols import load_master
        fo_master = load_master("NSE_FO")
    except Exception as error:
        log_error(f"FO master load failed: {error}")
        return None

    record = fo_master.get(best_symbol, {})
    if not record:
        log_error(f"LOT_SIZE_NOT_FOUND: {best_symbol} not in FO master")
        return None
    lot_size = int(record.get("minLotSize", 0) or 0)
    if lot_size <= 0:
        log_error(f"INVALID_LOT_SIZE: {best_symbol} lot_size={lot_size}")
        return None

    log_message(f"ATM_OPTION: {stock_symbol} ltp={ltp} strike_step={strike_step} "
                f"atm_strike={atm_strike} option={best_symbol} type={option_type} "
                f"lot={lot_size} expiry={nearest_expiry}")
    return best_symbol, lot_size


# =============================================================================
# Signal detection
# =============================================================================
def previous_session(daily: list[Candle], current_day: dt.date) -> Candle | None:
    """Most recent daily bar from a session strictly before current_day."""
    for candle in reversed(daily):
        bar_day = dt.datetime.fromtimestamp(candle.epoch, MARKET_TIMEZONE).date()
        if bar_day < current_day:
            return candle
    return None


def classic_r1(high: float, low: float, close: float) -> float:
    """Classic first resistance: 2P - L with P = (H + L + C) / 3."""
    pivot = (high + low + close) / 3
    return 2 * pivot - low


def resolve_level(kind: str, reference: Candle) -> float:
    if kind == LEVEL_R1:
        return classic_r1(reference.high, reference.low, reference.close)
    return reference.high


def todays_candles(candles_15min: list[Candle], current_day: dt.date) -> list[Candle]:
    return [
        candle for candle in candles_15min
        if dt.datetime.fromtimestamp(candle.epoch, MARKET_TIMEZONE).date() == current_day
    ]


def level_rejection_signal(candles_15min: list[Candle], daily: list[Candle],
                           kind: str, current: dt.datetime) -> tuple[str, dict]:
    """Reject or bounce at a level taken from the previous session.

    Returns ("PE"|"CE"|"NONE", details). PE means the level held from above and
    price closed back beneath it; CE means it held from below and price closed
    back above. The level must be breached by MIN_REJECTION_PERCENT so a graze
    is not counted.
    """
    label = "R1" if kind == LEVEL_R1 else "PREV_HIGH"
    if not candles_15min or len(daily) < 2:
        return "NONE", {"reason": "insufficient candles", "level_kind": kind}

    current_day = current.date()
    bars = todays_candles(candles_15min, current_day)
    if len(bars) < MIN_BARS_TODAY:
        return "NONE", {"reason": f"need at least {MIN_BARS_TODAY} completed bars today",
                        "today_bars": len(bars), "level_kind": kind}

    reference = previous_session(daily, current_day)
    if reference is None:
        return "NONE", {"reason": "no previous session in daily history",
                        "level_kind": kind}

    level = resolve_level(kind, reference)
    if level <= 0:
        return "NONE", {"reason": "non-positive level", "level_kind": kind}

    current_bar = bars[-1]
    breach = MIN_REJECTION_PERCENT / 100
    upper = level * (1 + breach)
    lower = level * (1 - breach)
    reference_day = dt.datetime.fromtimestamp(
        reference.epoch, MARKET_TIMEZONE
    ).date().isoformat()

    details = {
        "strategy": f"{STRATEGY_NAME}_{label}",
        "level_kind": kind,
        "level": round(level, 2),
        "previous_day": reference_day,
        "previous_high": reference.high,
        "previous_low": reference.low,
        "previous_close": reference.close,
        "curr_open": current_bar.open,
        "curr_high": current_bar.high,
        "curr_low": current_bar.low,
        "curr_close": current_bar.close,
        "curr_time": dt.datetime.fromtimestamp(
            current_bar.epoch, MARKET_TIMEZONE
        ).isoformat(),
    }

    if current_bar.high >= upper and current_bar.close < level:
        log_message(f"{label}_REJECTION_SIGNAL: PE - high={current_bar.high} >= "
                    f"{label}={level:.2f} but close={current_bar.close} < {level:.2f} "
                    f"| prev_day={reference_day} "
                    f"time={details['curr_time']}")
        return "PE", details

    if current_bar.low <= lower and current_bar.close > level:
        log_message(f"{label}_BOUNCE_SIGNAL: CE - low={current_bar.low} <= "
                    f"{label}={level:.2f} but close={current_bar.close} > {level:.2f} "
                    f"| prev_day={reference_day} "
                    f"time={details['curr_time']}")
        return "CE", details

    return "NONE", details


# =============================================================================
# Doji rejection
# =============================================================================
def is_doji(candle: Candle, max_body_ratio: float = DOJI_MAX_BODY_RATIO) -> bool:
    """True when the body is a small fraction of the candle's full range.

    A zero-range bar is rejected rather than treated as a doji, because such a
    bar is far more often missing or degenerate data than real indecision.
    """
    spread = candle.high - candle.low
    if spread <= 0:
        return False
    return abs(candle.close - candle.open) / spread < max_body_ratio


def has_no_upper_wick(candle: Candle) -> bool:
    """True when the high never meaningfully exceeds the open."""
    if candle.open <= 0:
        return False
    return candle.high <= candle.open * (1 + NO_WICK_TOLERANCE_PCT / 100)


def doji_rejection_signal(
    candles_15min: list[Candle],
    current: dt.datetime,
    restrict_to_today: bool = True,
) -> tuple[str, dict]:
    """Sell-side rejection of the bar that follows a doji.

    The doji marks indecision; the next bar failing to hold above it is the
    rejection. Returns ("PE"|"NONE", details) with the matched trigger named.

    restrict_to_today is True for live trading, so only the current session's
    completed bars are judged. The read-only dashboard passes False so that a
    holiday still shows the most recent session's pattern.
    """
    if restrict_to_today:
        bars = todays_candles(candles_15min, current.date())
        minimum = MIN_BARS_TODAY
    else:
        bars = sorted(candles_15min, key=lambda c: c.epoch)
        minimum = 2

    if len(bars) < minimum:
        return "NONE", {"reason": f"need at least {minimum} completed bars",
                        "bars": len(bars), "level_kind": KIND_DOJI}

    doji = bars[-2]
    current_bar = bars[-1]

    if not is_doji(doji):
        spread = doji.high - doji.low
        ratio = abs(doji.close - doji.open) / spread if spread > 0 else None
        return "NONE", {
            "reason": "previous bar is not a doji",
            "level_kind": KIND_DOJI,
            "doji_body_ratio": round(ratio, 4) if ratio is not None else None,
        }

    no_upper_wick = has_no_upper_wick(current_bar)
    opens_above = current_bar.open >= doji.high
    reaches_above = current_bar.high >= doji.high
    closes_below = current_bar.close < doji.low

    details = {
        "strategy": f"{STRATEGY_NAME}_DOJI",
        "level_kind": KIND_DOJI,
        "trigger": None,
        "doji_time": dt.datetime.fromtimestamp(doji.epoch, MARKET_TIMEZONE).isoformat(),
        "doji_open": doji.open, "doji_high": doji.high,
        "doji_low": doji.low, "doji_close": doji.close,
        "doji_body_ratio": round(abs(doji.close - doji.open) / (doji.high - doji.low), 4),
        "curr_open": current_bar.open, "curr_high": current_bar.high,
        "curr_low": current_bar.low, "curr_close": current_bar.close,
        "curr_time": dt.datetime.fromtimestamp(
            current_bar.epoch, MARKET_TIMEZONE
        ).isoformat(),
        "opens_above_doji_high": opens_above,
        "no_upper_wick": no_upper_wick,
    }

    # Most specific first, so the reported trigger is the informative one.
    if opens_above and no_upper_wick and closes_below:
        details["trigger"] = DOJI_BREAKOUT_FAIL
    elif no_upper_wick and closes_below:
        details["trigger"] = DOJI_OPEN_HIGH
    elif reaches_above and closes_below:
        details["trigger"] = DOJI_REJECTION
    else:
        details["reason"] = "no doji rejection trigger matched"
        return "NONE", details

    log_message(f"{details['trigger']}_SIGNAL: PE - doji O={doji.open} H={doji.high} "
                f"L={doji.low} C={doji.close} | current O={current_bar.open} "
                f"H={current_bar.high} L={current_bar.low} C={current_bar.close} "
                f"| doji_time={details['doji_time']} time={details['curr_time']}")
    return "PE", details


# =============================================================================
# Higher-high rejection
# =============================================================================
def _higher_high_signal(
    candles_15min: list[Candle],
    current: dt.datetime,
    close_below_close: bool,
    restrict_to_today: bool,
) -> tuple[str, dict]:
    """Shared logic for the two higher-high rejection variants.

    The bar must set a new high against the previous bar and then fail, closing
    back below it. close_below_close selects how deep the failure must be:
    False requires the close below the previous bar's LOW, True only below its
    CLOSE. Both are sell-side.
    """
    kind = KIND_HIGHER_HIGH_CLOSE if close_below_close else KIND_HIGHER_HIGH
    if restrict_to_today:
        bars = todays_candles(candles_15min, current.date())
        minimum = MIN_BARS_TODAY
    else:
        bars = sorted(candles_15min, key=lambda c: c.epoch)
        minimum = 2

    if len(bars) < minimum:
        return "NONE", {"reason": f"need at least {minimum} completed bars",
                        "bars": len(bars), "level_kind": kind}

    previous = bars[-2]
    current_bar = bars[-1]

    higher_high = current_bar.high > previous.high
    reference = previous.close if close_below_close else previous.low
    closed_below = current_bar.close < reference

    details = {
        "strategy": f"{STRATEGY_NAME}_HIGHER_HIGH",
        "level_kind": kind,
        "trigger": None,
        "threshold": "prev_close" if close_below_close else "prev_low",
        "prev_time": dt.datetime.fromtimestamp(previous.epoch, MARKET_TIMEZONE).isoformat(),
        "prev_open": previous.open, "prev_high": previous.high,
        "prev_low": previous.low, "prev_close": previous.close,
        "curr_open": current_bar.open, "curr_high": current_bar.high,
        "curr_low": current_bar.low, "curr_close": current_bar.close,
        "curr_time": dt.datetime.fromtimestamp(
            current_bar.epoch, MARKET_TIMEZONE
        ).isoformat(),
        "higher_high": higher_high,
        "close_below": closed_below,
    }

    if not (higher_high and closed_below):
        details["reason"] = (
            "not a higher high" if not higher_high
            else f"close {current_bar.close} did not fall below the "
                 f"{details['threshold']} {reference}"
        )
        return "NONE", details

    details["trigger"] = "HIGHER_HIGH_LOW_CLOSE" if not close_below_close else "HIGHER_HIGH_CLOSE"
    log_message(f"{details['trigger']}_SIGNAL: PE - high={current_bar.high} > "
                f"prev_high={previous.high} but close={current_bar.close} < "
                f"{details['threshold']}={reference} | "
                f"prev_time={details['prev_time']} time={details['curr_time']}")
    return "PE", details


def higher_high_low_rejection_signal(
    candles_15min: list[Candle],
    current: dt.datetime,
    restrict_to_today: bool = True,
) -> tuple[str, dict]:
    """Higher high, then a close below the previous bar's low. -> PE"""
    return _higher_high_signal(candles_15min, current, False, restrict_to_today)


def higher_high_close_rejection_signal(
    candles_15min: list[Candle],
    current: dt.datetime,
    restrict_to_today: bool = True,
) -> tuple[str, dict]:
    """Higher high, then a close below the previous bar's close. -> PE"""
    return _higher_high_signal(candles_15min, current, True, restrict_to_today)


# =============================================================================
# Risk controls
# =============================================================================
def detect_nifty_gap(client: FyersClient, limiter: ApiRateLimiter) -> tuple[float, bool]:
    """Return (gap percent, blocked) for NIFTY against the previous close."""
    try:
        response = limiter.call(client.history, "NSE:NIFTY50-INDEX", "D",
                                (dt.date.today() - dt.timedelta(days=5)).isoformat(),
                                dt.date.today().isoformat())
        if response.get("s") != "ok":
            return 0.0, False
        candles = parse_candles(response)
        if len(candles) < 2:
            return 0.0, False
        previous_close = candles[-2].close
        latest_open = candles[-1].open
        if previous_close <= 0:
            return 0.0, False
        gap = (latest_open - previous_close) / previous_close * 100
        return gap, abs(gap) > GAP_THRESHOLD_PERCENT
    except Exception as error:
        log_error(f"GAP_CHECK_ERROR: {error}")
        return 0.0, False


# =============================================================================
# Order management
# =============================================================================
def enter_position(client: FyersClient, symbol: str, option_type: str, ltp: float,
                   limiter: ApiRateLimiter, entered: set[str], kind: str,
                   candle_range: float = 0) -> None:
    """Buy the ATM CE/PE when a level rejection fires."""
    if symbol in entered:
        return

    result = resolve_atm_option(client, symbol, ltp, option_type, limiter)
    if not result:
        log_error(f"ENTRY_ABORT: {symbol} - could not resolve ATM {option_type} ltp={ltp}")
        log_to_excel(STRATEGY_NAME, SCRIPT_NAME, symbol, f"{kind}_{option_type}",
                     "FAILED", details=f"ATM resolve failed, LTP={ltp}")
        return

    option_symbol, lot_size = result
    order_qty = get_entry_qty(SCRIPT_NAME) * lot_size

    for pos_data in load_positions().values():
        if pos_data.get("symbol") == option_symbol:
            log_message(f"ENTRY_SKIP: {symbol} already holding {option_symbol}")
            log_to_excel(STRATEGY_NAME, SCRIPT_NAME, symbol, f"{kind}_{option_type}",
                         "SKIPPED", details=f"Open position exists: {option_symbol}")
            return

    try:
        for pos in limiter.call(client.positions).get("netPositions", []):
            pos_symbol = pos.get("symbol") or pos.get("symbolName")
            if pos_symbol == option_symbol and int(pos.get("netQty", 0)) != 0:
                log_message(f"ENTRY_SKIP: {symbol} has open position "
                            f"({option_symbol}, net_qty={pos.get('netQty')})")
                log_to_excel(STRATEGY_NAME, SCRIPT_NAME, symbol, f"{kind}_{option_type}",
                             "SKIPPED", details=f"Open position exists: {option_symbol}")
                return
    except Exception as error:
        log_error(f"POSITION_CHECK_ERROR: {error}")

    buffer = candle_range * LIMIT_BUFFER_PERCENT
    order = {
        "symbol": option_symbol,
        "qty": order_qty,
        "type": ENTRY_ORDER_TYPE,
        "side": 1,
        "productType": ENTRY_PRODUCT_TYPE,
        "limitPrice": round(max(0.05, ltp - buffer), 2),
        "orderTag": f"{kind[:15]}_{option_type.lower()}"[:20],
    }
    meta = {
        "strategy": STRATEGY_NAME,
        "signal": f"ENTRY_{option_type}",
        "description": f"{kind} {option_type} entry for {symbol} at LTP {ltp}, buffer={buffer:.2f}",
    }
    log_message(f"ENTRY_ORDER: {json.dumps(order, sort_keys=True)}")
    try:
        response = limiter.call(client.place_order, order, dry_run=not LIVE, meta=meta,
                                gap_threshold_pct=GAP_THRESHOLD_PERCENT)
    except (FyersAuthError, RuntimeError, ValueError) as error:
        log_error(f"ENTRY_FAILED: {symbol} {error}")
        log_to_excel(STRATEGY_NAME, SCRIPT_NAME, symbol, f"{kind}_{option_type}",
                     "FAILED", details=str(error)[:200])
        return

    order_id = response.get("id") or response.get("id_fyers", "not_returned")
    print_entry_result(option_symbol, str(order_id), response)
    order_status = "PLACED" if LIVE else "DRY_RUN"
    log_to_excel(STRATEGY_NAME, SCRIPT_NAME, symbol, f"{kind}_{option_type}", order_status,
                 order_id=str(order_id),
                 details=f"Option={option_symbol}, Qty={order_qty}, LTP={ltp}")

    if response.get("s") in ("ok", "dry_run"):
        entered.add(symbol)
        save_entered(entered)
        positions = load_positions()
        positions[str(order_id)] = {
            "symbol": option_symbol,
            "underlying": symbol,
            "entry_price": ltp,
            "option_type": option_type,
            "level_kind": kind,
            "qty": order_qty,
            "order_id": str(order_id),
            "highest_ltp": ltp,
        }
        save_positions(positions)


def exit_position(client: FyersClient, symbol: str, qty: int,
                  limiter: ApiRateLimiter, pos_id: str, ltp: float = 0) -> None:
    """Sell the tracked option at market."""
    buffer = ltp * EXIT_BUFFER_PERCENT
    order = {
        "symbol": symbol,
        "qty": qty,
        "type": 2,  # MARKET
        "side": -1,
        "productType": ENTRY_PRODUCT_TYPE,
        "limitPrice": 0,
        "orderTag": "r1_stop_exit",
    }
    meta = {
        "strategy": STRATEGY_NAME,
        "signal": "EXIT",
        "description": f"Stop-loss exit for {symbol}, buffer={buffer:.2f}",
    }
    log_message(f"EXIT_ORDER: {json.dumps(order, sort_keys=True)}")
    try:
        response = limiter.call(client.place_order, order, dry_run=not LIVE, meta=meta)
    except (FyersAuthError, RuntimeError, ValueError) as error:
        log_error(f"EXIT_FAILED: {symbol} {error}")
        return

    if response.get("s") in ("ok", "dry_run"):
        positions = load_positions()
        positions.pop(pos_id, None)
        save_positions(positions)
        log_message(f"EXIT_SUCCESS: {symbol} qty={qty} ltp={ltp}")
        log_to_excel(STRATEGY_NAME, SCRIPT_NAME, symbol, "EXIT", "CLOSED",
                     order_id=str(response.get("id", "")), details=f"Qty={qty}, LTP={ltp}")


def monitor_stop_loss(client: FyersClient, limiter: ApiRateLimiter) -> None:
    """Exit tracked positions when the tighter of the fixed/trailing stop hits."""
    stop_loss_percent = get_stop_loss_percent(SCRIPT_NAME)
    trailing_stop_percent = get_trailing_stop_loss_percent(SCRIPT_NAME)
    positions = load_positions()
    if not positions:
        return

    try:
        live_positions = limiter.call(client.positions).get("netPositions", [])
    except Exception as error:
        log_error(f"POSITIONS_FETCH_ERROR: {error}")
        return

    for pos_id, pos_data in list(positions.items()):
        symbol = pos_data.get("symbol")
        entry_price = float(pos_data.get("entry_price", 0) or 0)
        highest_ltp = float(pos_data.get("highest_ltp", entry_price) or entry_price)

        for live_pos in live_positions:
            live_symbol = live_pos.get("symbol") or live_pos.get("symbolName")
            net_qty = int(live_pos.get("netQty", 0))
            if live_symbol != symbol or net_qty == 0:
                continue
            try:
                quote_response = limiter.call(client.quotes, [symbol])
                if quote_response.get("s") != "ok":
                    continue
                quote_data = quote_response.get("d", [{}])[0].get("v", {})
                ltp = float(quote_data.get("lp", 0) or 0)
                if ltp <= 0 or entry_price <= 0:
                    continue

                if ltp > highest_ltp:
                    highest_ltp = ltp
                    pos_data["highest_ltp"] = highest_ltp
                    save_positions(positions)

                fixed_stop = entry_price * (1 - stop_loss_percent)
                trailing_stop = highest_ltp * (1 - trailing_stop_percent)
                stop_price = max(fixed_stop, trailing_stop)
                if ltp <= stop_price:
                    log_message(f"STOP_LOSS_HIT: {symbol} entry={entry_price} ltp={ltp} "
                                f"highest={highest_ltp} stop={stop_price:.2f} "
                                f"pnl={ltp - entry_price:.2f}")
                    exit_position(client, symbol, net_qty, limiter, pos_id, ltp)
            except Exception as error:
                log_error(f"QUOTE_ERROR {symbol}: {error}")


# =============================================================================
# Main loop
# =============================================================================
def main() -> int:
    parser = argparse.ArgumentParser(
        description="R1 rejection and previous-day-high rejection strategy (F&O)")
    parser.add_argument("--live", action="store_true", help="Send real orders (default: dry-run)")
    args = parser.parse_args()

    live = args.live or LIVE
    if live:
        log_message("LIVE MODE: real orders will be placed")

    clear_log_on_new_day()
    clear_entered_on_new_day()

    try:
        symbols = read_stocks()
        if not symbols:
            raise ValueError(f"no symbols found in {STOCKS_PATH}")

        DATABASE_PATH_15MIN.parent.mkdir(parents=True, exist_ok=True)
        client = ConfigGatedFyersClient(
            strategy_name=STRATEGY_NAME,
            script_name=SCRIPT_NAME,
        )
        limiter = ApiRateLimiter()
        entered = load_entered()
        current = dt.datetime.now(MARKET_TIMEZONE)
        log_message(f"STARTED [{STRATEGY_NAME}]: {len(symbols)} symbols, "
                    f"interval={POLL_SECONDS}s, market={MARKET_OPEN}-{MARKET_CLOSE} IST, "
                    f"already_entered={len(entered)}, "
                    f"stop_loss={get_stop_loss_percent(SCRIPT_NAME):.2%}, "
                    f"trailing_stop={get_trailing_stop_loss_percent(SCRIPT_NAME):.2%}, "
                    f"min_rejection={MIN_REJECTION_PERCENT}%, "
                    f"timeframe={ENTRY_CANDLE_MINUTES}min, "
                    f"strategies=R1,PrevHigh,Doji,HigherHighLow,HigherHighClose")

        with sqlite3.connect(DATABASE_PATH_15MIN) as connection:
            gap_blocked = False
            gap_checked = False

            while market_open():
                now = dt.datetime.now(MARKET_TIMEZONE)
                log_message(f"FETCH_CYCLE: {now.isoformat(timespec='seconds')}")

                if not gap_checked:
                    gap_pct, gap_blocked = detect_nifty_gap(client, limiter)
                    gap_checked = True
                    if gap_blocked:
                        log_message(f"GAP_BLOCKED: entries suspended, NIFTY gap "
                                    f"{gap_pct:+.2f}% exceeds {GAP_THRESHOLD_PERCENT}%")
                if gap_blocked:
                    monitor_stop_loss(client, limiter)
                    if now.minute % 5 == 0:
                        gap_pct, gap_blocked = detect_nifty_gap(client, limiter)
                        if not gap_blocked:
                            log_message(f"GAP_FILLED: NIFTY gap {gap_pct:+.2f}%, entries resumed")
                    if gap_blocked:
                        time.sleep(POLL_SECONDS)
                        continue

                monitor_stop_loss(client, limiter)

                for symbol in symbols:
                    if symbol in entered:
                        continue
                    table = table_name(symbol, str(ENTRY_CANDLE_MINUTES))
                    initialize_database(connection, table)
                    try:
                        candles_15min = fetch_symbol_candles(
                            client, connection, symbol, table, "15",
                            HISTORY_DAYS, limiter)
                        daily = fetch_candles(
                            client, limiter, symbol, "D", DAILY_HISTORY_DAYS, now)
                    except Exception as error:
                        log_error(f"FETCH_ERROR {symbol}: {error}")
                        continue

                    if FORCE_ENTRY:
                        option_type = CE_OPTION_TYPE
                        enter_position(client, symbol, option_type, 100.0, limiter,
                                       entered, "force", candle_range=0.0)
                        continue

                    for kind in (LEVEL_R1, LEVEL_PREV_HIGH):
                        signal, details = level_rejection_signal(
                            candles_15min, daily, kind, now)
                        if signal == "NONE":
                            continue
                        option_type = CE_OPTION_TYPE if signal == "CE" else PE_OPTION_TYPE
                        ltp = float(details.get("curr_close", 0) or 0)
                        if ltp <= 0:
                            continue
                        candle_range = details["curr_high"] - details["curr_low"]
                        enter_position(client, symbol, option_type, ltp, limiter,
                                       entered, kind, candle_range)
                        if symbol in entered:
                            break

                    if symbol in entered:
                        continue

                    signal, details = doji_rejection_signal(candles_15min, now)
                    if signal == "NONE":
                        signal, details = higher_high_low_rejection_signal(
                            candles_15min, now)
                    if signal == "NONE":
                        signal, details = higher_high_close_rejection_signal(
                            candles_15min, now)
                    if signal == "NONE":
                        continue
                    ltp = float(details.get("curr_close", 0) or 0)
                    if ltp <= 0:
                        continue
                    kind = str(details.get("level_kind") or KIND_DOJI)
                    trigger = str(details.get("trigger") or "")
                    compact_kind = (kind.lower() if not trigger
                                    else f"{kind}_{trigger.lower()}")
                    candle_range = details["curr_high"] - details["curr_low"]
                    enter_position(client, symbol, PE_OPTION_TYPE, ltp, limiter,
                                   entered, compact_kind, candle_range)

                time.sleep(POLL_SECONDS)

        log_message("STOPPED: market closed")
        return 0

    except KeyboardInterrupt:
        log_message("STOPPED: interrupted by user")
        return 0
    except FyersAuthError as error:
        log_error(f"AUTH_ERROR: {error}")
        return 1
    except Exception as error:
        log_error(f"FATAL: {error}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
