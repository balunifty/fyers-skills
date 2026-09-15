#!/usr/bin/env python3
"""ORB Strategy: Multiple entry signals for CE/PE options.
   1. ORB Breakout: Buy CE when 5min close > ORB High, Buy PE when 5min close < ORB Low
   2. ORB Rejection: Buy PE when 15min high >= ORB High but close < ORB High, vice versa for CE
   3. Double Top: Buy PE when 15min close < HMA(21), rejection at intraday high, RSI trending down
   15% stop loss for all entries."""
from __future__ import annotations

import argparse
import datetime as dt
import importlib.util
import json
import math
import pathlib
import re
import sqlite3
import sys
import time
from dataclasses import dataclass
from zoneinfo import ZoneInfo

SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parents[1]
STRATEGIES_DIR = SCRIPT_DIR.parent
WORKSPACE_ROOT = REPO_ROOT
LOG_PATH = STRATEGIES_DIR / "logs" / "OrbStrategyCallPut.log"
LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
LOG_PATH.touch(exist_ok=True)
sys.path.insert(0, str(WORKSPACE_ROOT))
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "strategies"))
sys.path.insert(0, str(REPO_ROOT / "strategies" / "utils"))
sys.path.insert(0, str(REPO_ROOT / "skills" / "fyers-trading" / "scripts"))
sys.path.insert(0, str(SCRIPT_DIR))

_indicator_spec = importlib.util.spec_from_file_location(
    "common_indicators", STRATEGIES_DIR / "utils" / "common_indicators.py"
)
assert _indicator_spec and _indicator_spec.loader
_indicator_module = importlib.util.module_from_spec(_indicator_spec)
_indicator_spec.loader.exec_module(_indicator_module)
adx = _indicator_module.adx
ema, hma, wma = _indicator_module.ema, _indicator_module.hma, _indicator_module.wma
rsi = _indicator_module.rsi
from fyers_client import FyersAuthError, FyersClient  # noqa: E402
from shared_data_fetcher import SharedDataFetcher  # noqa: E402
from excel_logger import log_to_excel  # noqa: E402

STOCKS_PATH = STRATEGIES_DIR / "data" / "NiftyFNOTop100.txt"
DATABASE_PATH_5MIN = STRATEGIES_DIR / "databases" / "NiftyFNOTop100_5min.db"
ENTERED_PATH = STRATEGIES_DIR / "data" / "entered_orb.json"
POSITIONS_PATH = STRATEGIES_DIR / "data" / "orb_positions.json"
STRATEGY_NAME = "ORB_STRATEGIES"

# --- Tuning constants --------------------------------------------------------
LIVE = True                          # True = send real orders
SKIP_MARKET_HOURS = False           # True = run outside market hours (test mode)
FORCE_ENTRY = False                  # True = place test order ignoring signal (test mode)
POLL_SECONDS = 30                    # test: 30s (prod: 5 * 60)
HISTORY_DAYS = 10                    # calendar days of candle history to fetch
MARKET_TIMEZONE = ZoneInfo("Asia/Kolkata")
MARKET_OPEN = dt.time(9, 15)
MARKET_CLOSE = dt.time(15, 45)
MAX_API_CALLS_PER_SECOND = 8
ENTRY_QTY = 10                       # number of lots per entry (lot size fetched from master)
ENTRY_ORDER_TYPE = 2                 # 1=Limit 2=Market
ENTRY_PRODUCT_TYPE = "INTRADAY"      # INTRADAY | CNC | MARGIN
ORB_CANDLE_MINUTES = 15              # ORB candle timeframe in minutes
ENTRY_CANDLE_MINUTES = 5             # Entry candle timeframe in minutes
ORB_START_TIME = dt.time(9, 15)     # ORB candle start (9:15 AM)
ORB_END_TIME = dt.time(9, 30)       # ORB candle end (9:30 AM)
STOP_LOSS_PERCENT = 0.15            # 15% stop loss
CE_OPTION_TYPE = "CE"               # Call option
PE_OPTION_TYPE = "PE"               # Put option
MAX_ORDERS_PER_DAY = 3              # Maximum orders per day
# --------------------------------------------------------------------------------


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


def log_error(message: str) -> None:
    entry = f"{dt.datetime.now().isoformat(timespec='seconds')} ERROR: {message}"
    with LOG_PATH.open("a", encoding="utf-8") as log_file:
        log_file.write(entry + "\n")
    print(entry, file=sys.stderr)


def log_message(message: str, error: bool = False) -> None:
    entry = f"{dt.datetime.now().isoformat(timespec='seconds')} {message}"
    with LOG_PATH.open("a", encoding="utf-8") as log_file:
        log_file.write(entry + "\n")
    print(entry, file=sys.stderr if error else sys.stdout)


def print_entry_result(symbol: str, order_id: str, response: dict) -> None:
    status = response.get("s")
    code = response.get("code")
    message = response.get("message", "")
    if status == "dry_run":
        log_message(f"ENTRY_DRY_RUN: {symbol} - no order sent")
    elif status == "ok":
        log_message(f"ENTRY_PLACED: {symbol} order_id={order_id} - {message}")
    elif status == "error" or (isinstance(code, int) and code < 0):
        log_message(f"ENTRY_REJECTED: {symbol} code={code} - {message}", error=True)
    else:
        log_message(f"ENTRY_UNKNOWN: {symbol} response={response}", error=True)


def clear_log_on_new_day() -> None:
    if not LOG_PATH.exists() or LOG_PATH.stat().st_size == 0:
        LOG_PATH.write_text("", encoding="utf-8")
        return
    first_line = LOG_PATH.read_text(encoding="utf-8").splitlines()[0]
    try:
        log_date = dt.datetime.fromisoformat(first_line.split(" ", 1)[0]).date()
    except (IndexError, ValueError):
        LOG_PATH.write_text("", encoding="utf-8")
        return
    if log_date != dt.datetime.now().date():
        LOG_PATH.write_text("", encoding="utf-8")


def clear_entered_on_new_day() -> None:
    if not ENTERED_PATH.exists():
        return
    try:
        data = json.loads(ENTERED_PATH.read_text(encoding="utf-8"))
        saved_date = data.get("date")
    except (json.JSONDecodeError, OSError):
        ENTERED_PATH.unlink(missing_ok=True)
        return
    today = dt.date.today().isoformat()
    if saved_date != today:
        ENTERED_PATH.unlink(missing_ok=True)


def market_open() -> bool:
    if SKIP_MARKET_HOURS:
        return True
    now = dt.datetime.now(MARKET_TIMEZONE)
    return now.weekday() < 5 and MARKET_OPEN <= now.time() <= MARKET_CLOSE


def read_stocks() -> list[str]:
    return [line.strip() for line in STOCKS_PATH.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")]


def load_entered() -> set[str]:
    if not ENTERED_PATH.exists():
        return set()
    try:
        data = json.loads(ENTERED_PATH.read_text(encoding="utf-8"))
        if data.get("date") != dt.date.today().isoformat():
            return set()
        return set(data.get("entered", []))
    except (json.JSONDecodeError, OSError):
        return set()


def save_entered(entered: set[str]) -> None:
    ENTERED_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {"date": dt.date.today().isoformat(), "entered": sorted(entered)}
    ENTERED_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def load_positions() -> dict:
    if not POSITIONS_PATH.exists():
        return {}
    try:
        data = json.loads(POSITIONS_PATH.read_text(encoding="utf-8"))
        if data.get("date") != dt.date.today().isoformat():
            return {}
        return data.get("positions", {})
    except (json.JSONDecodeError, OSError):
        return {}


def save_positions(positions: dict) -> None:
    POSITIONS_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {"date": dt.date.today().isoformat(), "positions": positions}
    POSITIONS_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def table_name(symbol: str, timeframe: str) -> str:
    safe_symbol = re.sub(r"[^A-Za-z0-9_]+", "_", symbol).strip("_")
    return f"candles_{timeframe}m_{safe_symbol}"


def initialize_database(connection: sqlite3.Connection, table: str) -> None:
    connection.execute(f"""
        CREATE TABLE IF NOT EXISTS {table} (
            symbol TEXT NOT NULL,
            epoch INTEGER NOT NULL,
            candle_time TEXT NOT NULL,
            open REAL NOT NULL,
            high REAL NOT NULL,
            low REAL NOT NULL,
            close REAL NOT NULL,
            volume REAL NOT NULL,
            fetched_at TEXT NOT NULL,
            PRIMARY KEY (symbol, epoch)
        )
    """)
    connection.commit()


def store_candles(connection: sqlite3.Connection, symbol: str,
                  table: str, candles: list[Candle]) -> int:
    fetched_at = dt.datetime.now(dt.timezone.utc).isoformat()
    rows = []
    for candle in candles:
        candle_time = dt.datetime.fromtimestamp(candle.epoch, MARKET_TIMEZONE).isoformat()
        rows.append((symbol, candle.epoch, candle_time, candle.open, candle.high, candle.low,
                     candle.close, candle.volume, fetched_at))
    connection.executemany(f"""
        INSERT INTO {table}
        (symbol, epoch, candle_time, open, high, low, close, volume, fetched_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(symbol, epoch) DO UPDATE SET
          candle_time=excluded.candle_time, open=excluded.open, high=excluded.high,
          low=excluded.low, close=excluded.close, volume=excluded.volume,
          fetched_at=excluded.fetched_at
    """, rows)
    connection.commit()
    return len(rows)


def fetch_symbol_candles(client: FyersClient, connection: sqlite3.Connection, symbol: str,
                         table: str, resolution: str, limiter: ApiRateLimiter) -> list[Candle]:
    today = dt.date.today()
    start = today - dt.timedelta(days=HISTORY_DAYS)
    response = limiter.call(client.history, symbol, resolution, start.isoformat(), today.isoformat())
    if response.get("s") != "ok":
        raise RuntimeError(f"history fetch failed: {response}")
    raw_candles = response.get("candles", [])
    candles = [Candle(int(row[0]), *map(float, row[1:6])) for row in raw_candles]
    if len(candles) > 1:
        candles = candles[:-1]
    store_candles(connection, symbol, table, candles)
    return candles


def round_to_strike(price: float, step: int) -> float:
    return round(price / step) * step


def get_strike_step(chains: list[dict]) -> int:
    strikes = sorted(set(item.get("strike_price", 0) for item in chains if item.get("strike_price", 0) > 0))
    if len(strikes) < 2:
        return 50
    diffs = [strikes[i+1] - strikes[i] for i in range(len(strikes)-1)]
    return int(min(diffs)) if diffs else 50


def resolve_atm_option(client: FyersClient, stock_symbol: str,
                       ltp: float, option_type: str, limiter: ApiRateLimiter) -> tuple[str, int] | None:
    response = limiter.call(client.option_chain, stock_symbol, strikecount=5, greeks=False)
    if response.get("s") != "ok":
        log_error(f"option_chain failed for {stock_symbol}: {response}")
        return None

    data = response.get("data", {})
    chains = data.get("optionsChain", []) or data.get("chain", [])
    strike_step = get_strike_step(chains)
    atm_strike = round_to_strike(ltp, strike_step)
    best_symbol = None
    best_diff = float("inf")
    best_lot = None
    nearest_expiry = None

    try:
        from fyers_symbols import load_master
        fo_master = load_master("NSE_FO")
    except Exception as e:
        log_error(f"FO master load failed: {e}")
        return None

    for item in chains:
        strike = item.get("strike_price", 0)
        expiry = item.get("expiryDate", "") or item.get("expiry_date", "")
        opt_type = item.get("option_type", "") or item.get("optType", "")
        sym = item.get("symbol", "")

        if opt_type != option_type:
            continue
        diff = abs(strike - atm_strike)
        if diff < best_diff:
            best_diff = diff
            best_symbol = sym
            nearest_expiry = expiry

    if not best_symbol:
        log_error(f"ATM_OPTION_NOT_FOUND: {stock_symbol} ltp={ltp} atm_strike={atm_strike} type={option_type}")
        return None

    rec = fo_master.get(best_symbol, {})
    if not rec:
        log_error(f"LOT_SIZE_NOT_FOUND: {best_symbol} not in FO master")
        return None
    best_lot = int(rec.get("minLotSize", 0))
    if best_lot <= 0:
        log_error(f"INVALID_LOT_SIZE: {best_symbol} lot_size={best_lot}")
        return None

    log_message(f"ATM_OPTION: {stock_symbol} ltp={ltp} strike_step={strike_step} atm_strike={atm_strike} "
                f"option={best_symbol} type={option_type} lot={best_lot} expiry={nearest_expiry}")
    return best_symbol, best_lot


def calculate_orb_range(candles_15min: list[Candle]) -> tuple[float, float] | None:
    """Calculate ORB range from the first 15-min candle (9:15 - 9:30).

    Returns (ORB_High, ORB_Low) or None if no candle found.
    """
    today = dt.date.today()
    for candle in candles_15min:
        candle_dt = dt.datetime.fromtimestamp(candle.epoch, MARKET_TIMEZONE)
        if candle_dt.date() == today:
            candle_time = candle_dt.time()
            if candle_time.hour == 9 and candle_time.minute == 15:
                orb_high = candle.high
                orb_low = candle.low
                log_message(f"ORB_RANGE: High={orb_high} Low={orb_low} from candle {candle_dt}")
                return orb_high, orb_low

    if candles_15min:
        latest = candles_15min[-1]
        candle_dt = dt.datetime.fromtimestamp(latest.epoch, MARKET_TIMEZONE)
        if candle_dt.date() == today and candle_dt.time().hour == 9 and candle_dt.time().minute == 15:
            log_message(f"ORB_RANGE: High={latest.high} Low={latest.low} from last candle")
            return latest.high, latest.low

    return None


# =============================================================================
# STRATEGY 1: ORB Breakout
# =============================================================================
def orb_breakout_signal(candles_5min: list[Candle], orb_high: float, orb_low: float) -> tuple[str, dict]:
    """Check for ORB breakout signal on 5-min candles.

    Buy CE when 5min candle closes above ORB High.
    Buy PE when 5min candle closes below ORB Low.

    Returns:
        ("CE", details) if 5min candle closes above ORB High
        ("PE", details) if 5min candle closes below ORB Low
        ("NONE", details) if no signal
    """
    if len(candles_5min) < 1:
        return "NONE", {"reason": "insufficient candles"}

    curr_candle = candles_5min[-1]
    curr_close = curr_candle.close
    curr_epoch = curr_candle.epoch
    curr_dt = dt.datetime.fromtimestamp(curr_epoch, MARKET_TIMEZONE)

    if curr_dt.date() != dt.date.today():
        return "NONE", {"reason": "no candle today yet"}

    details = {
        "strategy": "ORB_BREAKOUT",
        "curr_close": curr_close,
        "curr_high": curr_candle.high,
        "curr_low": curr_candle.low,
        "curr_time": curr_dt.isoformat(),
        "orb_high": orb_high,
        "orb_low": orb_low,
    }

    if curr_close > orb_high:
        log_message(f"ORB_BREAKOUT_SIGNAL: CE - close={curr_close} > orb_high={orb_high} time={curr_dt}")
        return "CE", details
    elif curr_close < orb_low:
        log_message(f"ORB_BREAKOUT_SIGNAL: PE - close={curr_close} < orb_low={orb_low} time={curr_dt}")
        return "PE", details

    return "NONE", details


# =============================================================================
# STRATEGY 2: ORB Rejection
# =============================================================================
def orb_rejection_signal(candles_15min: list[Candle], orb_high: float, orb_low: float) -> tuple[str, dict]:
    """Check for ORB rejection signal on 15-min candles.

    Buy PE when 15min candle high >= ORB High but close < ORB High (bearish rejection).
    Buy CE when 15min candle low <= ORB Low but close > ORB Low (bullish rejection).

    Returns:
        ("PE", details) if 15min candle shows rejection at ORB High
        ("CE", details) if 15min candle shows rejection at ORB Low
        ("NONE", details) if no signal
    """
    if len(candles_15min) < 1:
        return "NONE", {"reason": "insufficient candles"}

    curr_candle = candles_15min[-1]
    curr_close = curr_candle.close
    curr_high = curr_candle.high
    curr_low = curr_candle.low
    curr_epoch = curr_candle.epoch
    curr_dt = dt.datetime.fromtimestamp(curr_epoch, MARKET_TIMEZONE)

    if curr_dt.date() != dt.date.today():
        return "NONE", {"reason": "no candle today yet"}

    details = {
        "strategy": "ORB_REJECTION",
        "curr_close": curr_close,
        "curr_high": curr_high,
        "curr_low": curr_low,
        "curr_time": curr_dt.isoformat(),
        "orb_high": orb_high,
        "orb_low": orb_low,
    }

    if curr_high >= orb_high and curr_close < orb_high:
        log_message(f"ORB_REJECTION_SIGNAL: PE - high={curr_high} >= orb_high={orb_high} but close={curr_close} < orb_high time={curr_dt}")
        return "PE", details

    if curr_low <= orb_low and curr_close > orb_low:
        log_message(f"ORB_REJECTION_SIGNAL: CE - low={curr_low} <= orb_low={orb_low} but close={curr_close} > orb_low time={curr_dt}")
        return "CE", details

    return "NONE", details


# =============================================================================
# STRATEGY 3: Double Top Rejection (PE only)
# =============================================================================
def double_top_rejection_signal(candles_15min: list[Candle]) -> tuple[str, dict]:
    """Detect double top rejection pattern on 15-min candles for PE entry.

    Conditions:
      - 15-min candle close < HMA(21)
      - Candle high >= intraday high (tests/resistance zone)
      - Close below intraday high (rejection)
      - RSI trending down compared to when price was at intraday high

    Returns:
        ("PE", details) if double top rejection detected
        ("NONE", details) if no signal
    """
    if len(candles_15min) < 5:
        return "NONE", {"reason": "insufficient candles for HMA/RSI calculation"}

    today = dt.date.today()
    today_candles = []
    for c in candles_15min:
        c_dt = dt.datetime.fromtimestamp(c.epoch, MARKET_TIMEZONE)
        if c_dt.date() == today:
            today_candles.append(c)

    if len(today_candles) < 3:
        return "NONE", {"reason": "insufficient today candles"}

    curr_candle = today_candles[-1]
    curr_close = curr_candle.close
    curr_high = curr_candle.high
    curr_low = curr_candle.low
    curr_epoch = curr_candle.epoch
    curr_dt = dt.datetime.fromtimestamp(curr_epoch, MARKET_TIMEZONE)

    closes = [c.close for c in today_candles]
    highs = [c.high for c in today_candles]

    hma_values = hma(closes, 21)
    rsi_values = rsi(closes)

    curr_hma = hma_values[-1]
    curr_rsi = rsi_values[-1]

    if curr_hma is None or curr_rsi is None:
        return "NONE", {"reason": "insufficient HMA/RSI data"}

    intraday_high = max(highs)
    intraday_high_idx = highs.index(intraday_high)
    rsi_at_intraday_high = rsi_values[intraday_high_idx]

    if rsi_at_intraday_high is None:
        rsi_at_intraday_high = curr_rsi

    details = {
        "strategy": "DOUBLE_TOP_REJECTION",
        "curr_close": curr_close,
        "curr_high": curr_high,
        "curr_low": curr_low,
        "curr_time": curr_dt.isoformat(),
        "curr_hma21": curr_hma,
        "curr_rsi": curr_rsi,
        "intraday_high": intraday_high,
        "rsi_at_intraday_high": rsi_at_intraday_high,
        "rsi_trending_down": curr_rsi < rsi_at_intraday_high,
    }

    cond_close_below_hma = curr_close < curr_hma
    cond_rejection_at_high = curr_high >= intraday_high * 0.998 and curr_close < intraday_high
    cond_rsi_declining = curr_rsi < rsi_at_intraday_high

    if cond_close_below_hma and cond_rejection_at_high and cond_rsi_declining:
        log_message(f"DOUBLE_TOP_SIGNAL: PE - close={curr_close} < hma21={curr_hma:.2f} | "
                    f"high={curr_high} tested intraday_high={intraday_high} | "
                    f"rsi={curr_rsi:.1f} < rsi_at_high={rsi_at_intraday_high:.1f} | "
                    f"time={curr_dt}")
        return "PE", details

    return "NONE", details


# =============================================================================
# STRATEGY 4: Double Bottom Rejection (CE only)
# =============================================================================
def double_bottom_rejection_signal(candles_15min: list[Candle]) -> tuple[str, dict]:
    """Detect double bottom rejection pattern on 15-min candles for CE entry.

    Conditions:
      - 15-min candle close > HMA(21)
      - Candle low <= intraday low (tests/support zone)
      - Close above intraday low (rejection/bounce)
      - RSI trending up compared to when price was at intraday low

    Returns:
        ("CE", details) if double bottom rejection detected
        ("NONE", details) if no signal
    """
    if len(candles_15min) < 5:
        return "NONE", {"reason": "insufficient candles for HMA/RSI calculation"}

    today = dt.date.today()
    today_candles = []
    for c in candles_15min:
        c_dt = dt.datetime.fromtimestamp(c.epoch, MARKET_TIMEZONE)
        if c_dt.date() == today:
            today_candles.append(c)

    if len(today_candles) < 3:
        return "NONE", {"reason": "insufficient today candles"}

    curr_candle = today_candles[-1]
    curr_close = curr_candle.close
    curr_high = curr_candle.high
    curr_low = curr_candle.low
    curr_epoch = curr_candle.epoch
    curr_dt = dt.datetime.fromtimestamp(curr_epoch, MARKET_TIMEZONE)

    closes = [c.close for c in today_candles]
    lows = [c.low for c in today_candles]

    hma_values = hma(closes, 21)
    rsi_values = rsi(closes)

    curr_hma = hma_values[-1]
    curr_rsi = rsi_values[-1]

    if curr_hma is None or curr_rsi is None:
        return "NONE", {"reason": "insufficient HMA/RSI data"}

    intraday_low = min(lows)
    intraday_low_idx = lows.index(intraday_low)
    rsi_at_intraday_low = rsi_values[intraday_low_idx]

    if rsi_at_intraday_low is None:
        rsi_at_intraday_low = curr_rsi

    details = {
        "strategy": "DOUBLE_BOTTOM_REJECTION",
        "curr_close": curr_close,
        "curr_high": curr_high,
        "curr_low": curr_low,
        "curr_time": curr_dt.isoformat(),
        "curr_hma21": curr_hma,
        "curr_rsi": curr_rsi,
        "intraday_low": intraday_low,
        "rsi_at_intraday_low": rsi_at_intraday_low,
        "rsi_trending_up": curr_rsi > rsi_at_intraday_low,
    }

    cond_close_above_hma = curr_close > curr_hma
    cond_rejection_at_low = curr_low <= intraday_low * 1.002 and curr_close > intraday_low
    cond_rsi_rising = curr_rsi > rsi_at_intraday_low

    if cond_close_above_hma and cond_rejection_at_low and cond_rsi_rising:
        log_message(f"DOUBLE_BOTTOM_SIGNAL: CE - close={curr_close} > hma21={curr_hma:.2f} | "
                    f"low={curr_low} tested intraday_low={intraday_low} | "
                    f"rsi={curr_rsi:.1f} > rsi_at_low={rsi_at_intraday_low:.1f} | "
                    f"time={curr_dt}")
        return "CE", details

    return "NONE", details


# =============================================================================
# STRATEGY 5: EMA Crossover with RSI (CE only)
# =============================================================================
def ema_crossover_rsi_signal(candles_15min: list[Candle]) -> tuple[str, dict]:
    """Detect 9 EMA / 26 EMA crossover with RSI confirmation on 15-min candles.

    Conditions:
      - 9 EMA crosses above 26 EMA (crossover)
      - Candle close > 9 EMA
      - RSI > 55

    Returns:
        ("CE", details) if signal detected
        ("NONE", details) if no signal
    """
    if len(candles_15min) < 26:
        return "NONE", {"reason": "insufficient candles for EMA calculation"}

    curr_candle = candles_15min[-1]
    prev_candle = candles_15min[-2]

    curr_close = curr_candle.close
    prev_close = prev_candle.close
    curr_epoch = curr_candle.epoch
    curr_dt = dt.datetime.fromtimestamp(curr_epoch, MARKET_TIMEZONE)

    if curr_dt.date() != dt.date.today():
        return "NONE", {"reason": "no candle today yet"}

    closes = [c.close for c in candles_15min]
    ema9_values = ema(closes, 9)
    ema26_values = ema(closes, 26)
    rsi_values = rsi(closes)

    curr_ema9 = ema9_values[-1]
    curr_ema26 = ema26_values[-1]
    prev_ema9 = ema9_values[-2]
    prev_ema26 = ema26_values[-2]
    curr_rsi = rsi_values[-1]

    if curr_ema9 is None or curr_ema26 is None or prev_ema9 is None or prev_ema26 is None or curr_rsi is None:
        return "NONE", {"reason": "insufficient EMA/RSI data"}

    details = {
        "strategy": "EMA_CROSSOVER_RSI",
        "curr_close": curr_close,
        "curr_ema9": curr_ema9,
        "curr_ema26": curr_ema26,
        "prev_ema9": prev_ema9,
        "prev_ema26": prev_ema26,
        "curr_rsi": curr_rsi,
        "curr_time": curr_dt.isoformat(),
    }

    cond_crossover = prev_ema9 <= prev_ema26 and curr_ema9 > curr_ema26
    cond_close_above_ema9 = curr_close > curr_ema9
    cond_rsi_above_55 = curr_rsi > 55

    if cond_crossover and cond_close_above_ema9 and cond_rsi_above_55:
        log_message(f"EMA_CROSSOVER_SIGNAL: CE - close={curr_close} > ema9={curr_ema9:.2f} | "
                    f"ema9={curr_ema9:.2f} crossed ema26={curr_ema26:.2f} | "
                    f"rsi={curr_rsi:.1f} > 55 | "
                    f"time={curr_dt}")
        return "CE", details

    return "NONE", details


# =============================================================================
# Stop Loss & Position Management
# =============================================================================
def monitor_stop_loss(client: FyersClient, limiter: ApiRateLimiter) -> None:
    """Check open positions and exit if stop loss is hit (10% loss)."""
    positions = load_positions()
    if not positions:
        return

    try:
        live_positions = limiter.call(client.positions).get("netPositions", [])
    except Exception as e:
        log_error(f"POSITIONS_FETCH_ERROR: {e}")
        return

    for pos_id, pos_data in list(positions.items()):
        symbol = pos_data.get("symbol")
        entry_price = pos_data.get("entry_price", 0)

        for live_pos in live_positions:
            live_symbol = live_pos.get("symbol") or live_pos.get("symbolName")
            net_qty = int(live_pos.get("netQty", 0))

            if live_symbol == symbol and net_qty != 0:
                try:
                    quote_response = limiter.call(client.quotes, [symbol])
                    if quote_response.get("s") == "ok":
                        quote_data = quote_response.get("d", [{}])[0].get("v", {})
                        ltp = quote_data.get("lp", 0)

                        if entry_price > 0:
                            loss_pct = (entry_price - ltp) / entry_price
                            if loss_pct >= STOP_LOSS_PERCENT:
                                log_message(f"STOP_LOSS_HIT: {symbol} entry={entry_price} current={ltp} "
                                           f"loss={loss_pct:.2%} - EXITING")
                                order = {
                                    "symbol": symbol,
                                    "qty": abs(net_qty),
                                    "type": ENTRY_ORDER_TYPE,
                                    "side": 2,
                                    "productType": ENTRY_PRODUCT_TYPE,
                                    "orderTag": "orb_sl",
                                }
                                meta = {"strategy": STRATEGY_NAME, "signal": "STOP_LOSS", "description": f"ORB stop loss hit at {loss_pct:.2%}"}
                                response = limiter.call(client.place_order, order, dry_run=not LIVE, meta=meta)
                                log_message(f"STOP_LOSS_ORDER: {symbol} response={response}")
                                del positions[pos_id]
                                save_positions(positions)
                                return
                except Exception as e:
                    log_error(f"STOP_LOSS_CHECK_ERROR: {symbol} {e}")
                break

        if live_symbol != symbol or net_qty == 0:
            log_message(f"POSITION_CLOSED_EXTERNALLY: {symbol} - removing from tracking")
            if pos_id in positions:
                del positions[pos_id]
                save_positions(positions)


def enter_position(client: FyersClient, symbol: str, option_type: str, ltp: float,
                   limiter: ApiRateLimiter, entered: set[str]) -> None:
    """Place a BUY order for ATM CE/PE option."""
    SCRIPT_NAME = "OrbStrategyCallPut.py"
    if symbol in entered:
        return
    if len(entered) >= MAX_ORDERS_PER_DAY:
        log_message(f"ENTRY_SKIP: Max orders ({MAX_ORDERS_PER_DAY}) reached for today")
        log_to_excel(STRATEGY_NAME, SCRIPT_NAME, symbol, "SIGNAL_MATCH", "SKIPPED", details="Max orders reached")
        return

    result = resolve_atm_option(client, symbol, ltp, option_type, limiter)
    if not result:
        log_error(f"ENTRY_ABORT: {symbol} - could not resolve ATM {option_type} for ltp={ltp}")
        log_to_excel(STRATEGY_NAME, SCRIPT_NAME, symbol, "SIGNAL_MATCH", "FAILED", details=f"ATM resolve failed, LTP={ltp}")
        return

    option_symbol, lot_size = result
    order_qty = ENTRY_QTY * lot_size

    positions_data = load_positions()
    for pos_data in positions_data.values():
        if pos_data.get("symbol") == option_symbol:
            log_message(f"ENTRY_SKIP: {symbol} already holding {option_symbol}")
            log_to_excel(STRATEGY_NAME, SCRIPT_NAME, symbol, "SIGNAL_MATCH", "SKIPPED", details=f"Open position exists: {option_symbol}")
            return

    try:
        live_positions = limiter.call(client.positions).get("netPositions", [])
        for pos in live_positions:
            pos_symbol = pos.get("symbol") or pos.get("symbolName")
            net_qty = int(pos.get("netQty", 0))
            if pos_symbol == option_symbol and net_qty != 0:
                log_message(f"ENTRY_SKIP: {symbol} already has open position ({option_symbol}, net_qty={net_qty})")
                log_to_excel(STRATEGY_NAME, SCRIPT_NAME, symbol, "SIGNAL_MATCH", "SKIPPED", details=f"Open position exists: {option_symbol}")
                return
    except Exception as e:
        log_error(f"POSITION_CHECK_ERROR: {e}")

    order = {
        "symbol": option_symbol,
        "qty": order_qty,
        "type": ENTRY_ORDER_TYPE,
        "side": 1,
        "productType": ENTRY_PRODUCT_TYPE,
        "orderTag": f"orb_{option_type.lower()}",
    }
    meta = {"strategy": STRATEGY_NAME, "signal": f"ENTRY_{option_type}", "description": f"ORB {option_type} entry for {symbol} at LTP {ltp}"}
    log_message(f"ENTRY_ORDER: {json.dumps(order, sort_keys=True)}")
    response = limiter.call(client.place_order, order, dry_run=not LIVE, meta=meta)
    order_id = response.get("id") or response.get("id_fyers", "not_returned")
    print_entry_result(option_symbol, order_id, response)
    order_status = "DRY_RUN" if not LIVE else "PLACED"
    log_to_excel(STRATEGY_NAME, SCRIPT_NAME, symbol, f"ENTRY_{option_type}", order_status, order_id=order_id, details=f"Option={option_symbol}, Qty={order_qty}, LTP={ltp}")

    if response.get("s") in ("ok", "dry_run"):
        entered.add(symbol)
        save_entered(entered)
        positions_data = load_positions()
        positions_data[order_id] = {
            "symbol": option_symbol,
            "entry_price": ltp,
            "option_type": option_type,
            "qty": order_qty,
            "order_id": order_id,
        }
        save_positions(positions_data)


def exit_position_if_hma_cross(client: FyersClient, symbol: str,
                               candles_15min: list[Candle], live: bool,
                               limiter: ApiRateLimiter) -> None:
    """Exit position based on HMA(21) condition.
    
    CE exit: 15-min candle close < HMA(21)
    PE exit: 15-min candle open >= HMA(21), close > open, close > HMA(21), upper_wick < body
    """
    SCRIPT_NAME = "OrbStrategyCallPut.py"
    if len(candles_15min) < 21:
        return
    
    closes = [c.close for c in candles_15min]
    hma_values = hma(closes, 21)
    if hma_values[-1] is None:
        return
    
    current_candle = candles_15min[-1]
    current_close = current_candle.close
    current_open = current_candle.open
    current_high = current_candle.high
    current_hma = hma_values[-1]
    
    # Get open positions
    positions_data = load_positions()
    for pos_id, pos_data in list(positions_data.items()):
        pos_symbol = pos_data.get("symbol")
        option_type = pos_data.get("option_type", "CE")
        if pos_symbol == symbol:
            # Check exit condition based on option type
            if option_type == "CE":
                if current_close >= current_hma:
                    return  # CE: only exit if close < HMA
            if option_type == "PE":
                # PE exit conditions:
                # 1. Open >= HMA(21)
                # 2. Close > Open (bullish candle)
                # 3. Close > HMA(21)
                # 4. Upper wick < Body
                body = current_close - current_open
                upper_wick = current_high - current_close
                pe_exit_condition = (
                    current_open >= current_hma and
                    current_close > current_open and
                    current_close > current_hma and
                    upper_wick < body
                )
                if not pe_exit_condition:
                    return  # PE: only exit if all conditions met
            
            try:
                live_positions = limiter.call(client.positions).get("netPositions", [])
                for live_pos in live_positions:
                    live_symbol = live_pos.get("symbol") or live_pos.get("symbolName")
                    net_qty = int(live_pos.get("netQty", 0))
                    if live_symbol == pos_symbol and net_qty != 0:
                        order = {
                            "symbol": pos_symbol,
                            "qty": abs(net_qty),
                            "type": ENTRY_ORDER_TYPE,
                            "side": -1,
                            "productType": ENTRY_PRODUCT_TYPE,
                            "orderTag": "hma21_exit",
                        }
                        if option_type == "CE":
                            exit_reason = f"close={current_close} < HMA21={current_hma:.2f}"
                        else:
                            exit_reason = f"Bullish candle: open={current_open} >= HMA21={current_hma:.2f}, close={current_close} > open, upper_wick={upper_wick:.2f} < body={body:.2f}"
                        meta = {"strategy": STRATEGY_NAME, "signal": "EXIT", "description": f"HMA21 {option_type} exit for {symbol}, {exit_reason}"}
                        log_message(f"HMA_EXIT_ORDER: {json.dumps(order, sort_keys=True)}")
                        response = limiter.call(client.place_order, order, dry_run=not live, meta=meta)
                        order_id = response.get("id") or response.get("id_fyers", "not_returned")
                        log_message(f"HMA_EXIT_RESULT: {symbol} order_id={order_id} response={response}")
                        log_to_excel(STRATEGY_NAME, SCRIPT_NAME, symbol, "EXIT_HMA21", "PLACED" if live else "DRY_RUN", order_id=order_id, details=f"{option_type} {exit_reason}")
                        del positions_data[pos_id]
                        save_positions(positions_data)
                        return
            except Exception as e:
                log_error(f"HMA_EXIT_ERROR: {symbol} {e}")


def main() -> int:
    parser = argparse.ArgumentParser(description="ORB Strategy - Call & Put Options (Multiple Strategies)")
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

        DATABASE_PATH_5MIN.parent.mkdir(parents=True, exist_ok=True)
        client = FyersClient()
        limiter = ApiRateLimiter()
        entered = load_entered()
        orb_calculated = {}
        shared_fetcher = SharedDataFetcher()
        log_message(f"STARTED [{STRATEGY_NAME}]: {len(symbols)} symbols, interval={POLL_SECONDS}s, "
                    f"market={MARKET_OPEN}-{MARKET_CLOSE} IST, "
                    f"already_entered={len(entered)}, stop_loss={STOP_LOSS_PERCENT:.0%}")

        with sqlite3.connect(DATABASE_PATH_5MIN) as conn_5min:

            while market_open():
                now = dt.datetime.now(MARKET_TIMEZONE)
                log_message(f"FETCH_CYCLE: {now.isoformat(timespec='seconds')}")

                monitor_stop_loss(client, limiter)

                for symbol in symbols:
                    table_5m = table_name(symbol, "5")
                    initialize_database(conn_5m, table_5m)

                    try:
                        candles_5min = fetch_symbol_candles(client, conn_5m, symbol,
                                                           table_5m, "5", limiter)
                        # Use shared fetcher for 15-min candles
                        candles_15min = shared_fetcher.get_candles(symbol)

                        # Check HMA(21) exit on 15-min candles
                        if candles_15min:
                            exit_position_if_hma_cross(client, symbol, candles_15min, live, limiter)

                        orb_range = calculate_orb_range(candles_15min)
                        if not orb_range:
                            log_message(f"ORB_NOT_READY: {symbol} - waiting for 15min candle")
                            continue

                        orb_high, orb_low = orb_range
                        orb_calculated[symbol] = (orb_high, orb_low)

                        # Strategy 1: ORB Breakout
                        signal, details = orb_breakout_signal(candles_5min, orb_high, orb_low)
                        if signal != "NONE" and symbol not in entered:
                            option_type = CE_OPTION_TYPE if signal == "CE" else PE_OPTION_TYPE
                            log_message(f"STRATEGY_1_{signal}_SIGNAL: {symbol}")
                            ltp = candles_5min[-1].close
                            enter_position(client, symbol, option_type, ltp, limiter, entered)
                            continue

                        # Strategy 2: ORB Rejection
                        signal, details = orb_rejection_signal(candles_15min, orb_high, orb_low)
                        if signal != "NONE" and symbol not in entered:
                            option_type = CE_OPTION_TYPE if signal == "CE" else PE_OPTION_TYPE
                            log_message(f"STRATEGY_2_{signal}_SIGNAL: {symbol}")
                            ltp = candles_5min[-1].close
                            enter_position(client, symbol, option_type, ltp, limiter, entered)
                            continue

                        # Strategy 3: Double Top Rejection
                        signal, details = double_top_rejection_signal(candles_15min)
                        if signal != "NONE" and symbol not in entered:
                            option_type = CE_OPTION_TYPE if signal == "CE" else PE_OPTION_TYPE
                            log_message(f"STRATEGY_3_{signal}_SIGNAL: {symbol}")
                            ltp = candles_5min[-1].close
                            enter_position(client, symbol, option_type, ltp, limiter, entered)
                            continue

                        # Strategy 4: Double Bottom Rejection
                        signal, details = double_bottom_rejection_signal(candles_15min)
                        if signal != "NONE" and symbol not in entered:
                            option_type = CE_OPTION_TYPE if signal == "CE" else PE_OPTION_TYPE
                            log_message(f"STRATEGY_4_{signal}_SIGNAL: {symbol}")
                            ltp = candles_5min[-1].close
                            enter_position(client, symbol, option_type, ltp, limiter, entered)
                            continue

                        # Strategy 5: EMA Crossover with RSI
                        signal, details = ema_crossover_rsi_signal(candles_15min)
                        if signal != "NONE" and symbol not in entered:
                            option_type = CE_OPTION_TYPE if signal == "CE" else PE_OPTION_TYPE
                            log_message(f"STRATEGY_5_{signal}_SIGNAL: {symbol}")
                            ltp = candles_5min[-1].close
                            enter_position(client, symbol, option_type, ltp, limiter, entered)
                            continue

                        log_message(f"NO_SIGNAL: {symbol} close={details.get('curr_close')} "
                                   f"orb_high={orb_high} orb_low={orb_low}")

                    except (RuntimeError, ValueError) as error:
                        log_error(f"{symbol}: {error}")

                time.sleep(POLL_SECONDS)

        log_message("STOPPED: market closed")

    except (FyersAuthError, OSError, RuntimeError, ValueError, sqlite3.Error) as error:
        log_error(str(error))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
