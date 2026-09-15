#!/usr/bin/env python3
"""Fetch completed 15-minute FYERS candles and persist technical indicators."""
from __future__ import annotations

import datetime as dt
import importlib.util
import json
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
LOG_PATH = STRATEGIES_DIR / "logs" / "Eq_Nifty50.log"
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
hma = _indicator_module.hma
from fyers_client import FyersAuthError, FyersClient  # noqa: E402
from excel_logger import log_to_excel  # noqa: E402

STOCKS_PATH = STRATEGIES_DIR / "data" / "Nifty50.txt"
DATABASE_PATH = STRATEGIES_DIR / "databases" / "nifty50.db"
ENTERED_PATH = STRATEGIES_DIR / "data" / "entered_symbols.json"
STRATEGY_NAME = "EMA10_CROSS_EMA50_15MIN_CANDLE"

# --- Tuning constants (swap values for testing, revert before production) -------
LIVE = True                          # True = send real orders
POLL_SECONDS = 10 * 60               # pull data every 10 minutes (uses 15-min candles)
# POLL_SECONDS = 30                    #   <-- test: faster cycle for dev
HISTORY_DAYS = 10                     # calendar days of candle history to fetch
# HISTORY_DAYS = 2                    #   <-- test: shorter history
MARKET_TIMEZONE = ZoneInfo("Asia/Kolkata")
MARKET_OPEN = dt.time(9, 15)          # IST market open
MARKET_CLOSE = dt.time(15, 45)        # IST market close (prod: 15:45)
# MARKET_CLOSE = dt.time(23, 59)      #   <-- test: run outside market hours
MAX_API_CALLS_PER_SECOND = 8          # broker rate-limit ceiling
# MAX_API_CALLS_PER_SECOND = 2        #   <-- test: gentler on API
ENTRY_QTY = 1                          # number of shares per entry order
ENTRY_ORDER_TYPE = 2                   # 1=Limit 2=Market 3=SL-M 4=SL-L
ENTRY_PRODUCT_TYPE = "INTRADAY"        # INTRADAY | CNC | MARGIN
RSI_ENTRY_THRESHOLD = 55               # RSI must cross above this to enter
MAX_ORDERS_PER_DAY = 3                 # Maximum orders per day
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
    """Space API calls to keep the agent below the broker request-rate limit."""

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
    """Append only errors to the current run's log and report them on stderr."""
    entry = f"{dt.datetime.now().isoformat(timespec='seconds')} ERROR: {message}"
    with LOG_PATH.open("a", encoding="utf-8") as log_file:
        log_file.write(entry + "\n")
    print(entry, file=sys.stderr)


def log_message(message: str, error: bool = False) -> None:
    """Write a timestamped order/event message to the shared log and console."""
    entry = f"{dt.datetime.now().isoformat(timespec='seconds')} {message}"
    with LOG_PATH.open("a", encoding="utf-8") as log_file:
        log_file.write(entry + "\n")
    print(entry, file=sys.stderr if error else sys.stdout)


def print_exit_result(symbol: str, position_id: str, response: dict) -> None:
    """Log a clear exit result using the same response structure as entry orders."""
    status = response.get("s")
    code = response.get("code")
    message = response.get("message", "")
    if status == "dry_run":
        log_message(f"EXIT_DRY_RUN: {symbol} position={position_id} - no exit sent")
    elif status == "ok":
        order_id = response.get("id") or response.get("id_fyers", "not returned")
        log_message(f"EXIT_PLACED: {symbol} position={position_id} order_id={order_id} - {message}")
    elif status == "error" or (isinstance(code, int) and code < 0):
        log_message(f"EXIT_REJECTED: {symbol} position={position_id} code={code} - {message}", error=True)
    else:
        log_message(f"EXIT_UNKNOWN: {symbol} position={position_id} response={response}", error=True)


def clear_log_on_new_day() -> None:
    """Clear the error log once when its first entry belongs to an older day."""
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
    """Clear the entered-symbols file at the start of a new trading day."""
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
    now = dt.datetime.now(MARKET_TIMEZONE)
    return now.weekday() < 5 and MARKET_OPEN <= now.time() <= MARKET_CLOSE


def read_stocks() -> list[str]:
    return [line.strip() for line in STOCKS_PATH.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")]


def load_entered() -> set[str]:
    """Load the set of symbols already entered today from disk."""
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
    """Persist the entered symbols set to disk."""
    ENTERED_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {"date": dt.date.today().isoformat(), "entered": sorted(entered)}
    ENTERED_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def table_name(symbol: str) -> str:
    """Return a safe, separate SQLite table name for one symbol."""
    safe_symbol = re.sub(r"[^A-Za-z0-9_]+", "_", symbol).strip("_")
    return f"candles_15m_{safe_symbol}"


def hma_crossed_down(candles: list[Candle]) -> tuple[bool, dict]:
    """Return true when the latest completed candle crosses below HMA(21)."""
    closes = [candle.close for candle in candles]
    hma_values = hma(closes, 21)
    if len(candles) < 2 or hma_values[-2] is None or hma_values[-1] is None:
        return False, {"reason": "insufficient HMA(21) data"}
    previous_close, current_close = closes[-2], closes[-1]
    previous_hma, current_hma = hma_values[-2], hma_values[-1]
    details = {
        "previous_close": previous_close,
        "previous_hma21": previous_hma,
        "current_close": current_close,
        "current_hma21": current_hma,
    }
    crossed = previous_close > previous_hma and current_close < current_hma
    if crossed:
        log_message(f"hma_crossed_down: PREV close={previous_close} > hma={previous_hma} "
                    f"→ CURR close={current_close} < hma={current_hma}")
    return crossed, details


def ema_rsi_entry_signal(candles: list[Candle]) -> tuple[bool, dict]:
    """Detect EMA10/EMA50 crossover entry with RSI confirmation.

    Entry rules:
      - Previous candle: close < EMA10 AND EMA10 < EMA50  (both EMAs above price)
      - Current candle:  close > EMA10                     (price crosses above EMA10)
      - EMA10 crosses above EMA50:  prev EMA10 <= EMA50 AND curr EMA10 > EMA50
      - RSI14 crosses above threshold:  prev RSI <= threshold AND curr RSI > threshold
    """
    if len(candles) < 3:
        return False, {"reason": "insufficient candles"}
    closes = [candle.close for candle in candles]
    ema10_vals = ema(closes, 10)
    ema50_vals = ema(closes, 50)
    rsi_vals = rsi(closes)

    prev_idx, curr_idx = -2, -1
    if any(v is None for v in (ema10_vals[prev_idx], ema50_vals[prev_idx], rsi_vals[prev_idx],
                                ema10_vals[curr_idx], ema50_vals[curr_idx], rsi_vals[curr_idx])):
        return False, {"reason": "insufficient indicator data"}

    prev_close = closes[prev_idx]
    curr_close = closes[curr_idx]
    prev_ema10, curr_ema10 = ema10_vals[prev_idx], ema10_vals[curr_idx]
    prev_ema50, curr_ema50 = ema50_vals[prev_idx], ema50_vals[curr_idx]
    prev_rsi, curr_rsi = rsi_vals[prev_idx], rsi_vals[curr_idx]

    details = {
        "prev_close": prev_close, "curr_close": curr_close,
        "prev_ema10": prev_ema10, "curr_ema10": curr_ema10,
        "prev_ema50": prev_ema50, "curr_ema50": curr_ema50,
        "prev_rsi": prev_rsi, "curr_rsi": curr_rsi,
    }

    cond_prev_below = prev_close < prev_ema10 and prev_ema10 < prev_ema50
    cond_curr_cross_above = curr_close > curr_ema10
    cond_ema_cross = prev_ema10 <= prev_ema50 and curr_ema10 > curr_ema50
    cond_rsi_cross = prev_rsi <= RSI_ENTRY_THRESHOLD and curr_rsi > RSI_ENTRY_THRESHOLD

    triggered = cond_prev_below and cond_curr_cross_above and cond_ema_cross and cond_rsi_cross
    if triggered:
        log_message(f"ema_rsi_entry_signal: PREV close={prev_close} < ema10={prev_ema10} < ema50={prev_ema50} "
                    f"→ CURR close={curr_close} > ema10={curr_ema10} | ema10_crossed_ema50 | "
                    f"rsi {prev_rsi:.1f}→{curr_rsi:.1f}")
    return triggered, details


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
            ema10 REAL,
            ema20 REAL,
            ema30 REAL,
            ema50 REAL,
            rsi14 REAL,
            adx14 REAL,
            hma21 REAL,
            fetched_at TEXT NOT NULL,
            PRIMARY KEY (symbol, epoch)
        )
    """)
    columns = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
    if "hma21" not in columns:
        connection.execute(f"ALTER TABLE {table} ADD COLUMN hma21 REAL")
    connection.commit()


def store_candles(connection: sqlite3.Connection, symbol: str,
                  table: str, candles: list[Candle]) -> int:
    closes = [candle.close for candle in candles]
    indicators = (ema(closes, 10), ema(closes, 20), ema(closes, 30), ema(closes, 50),
                  rsi(closes), adx(candles), hma(closes, 21))
    fetched_at = dt.datetime.now(dt.timezone.utc).isoformat()
    rows = []
    for index, candle in enumerate(candles):
        values = [series[index] for series in indicators]
        candle_time = dt.datetime.fromtimestamp(candle.epoch, MARKET_TIMEZONE).isoformat()
        rows.append((symbol, candle.epoch, candle_time, candle.open, candle.high, candle.low,
                     candle.close, candle.volume, *values, fetched_at))
    connection.executemany(f"""
        INSERT INTO {table}
        (symbol, epoch, candle_time, open, high, low, close, volume,
         ema10, ema20, ema30, ema50, rsi14, adx14, hma21, fetched_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(symbol, epoch) DO UPDATE SET
          candle_time=excluded.candle_time, open=excluded.open, high=excluded.high,
          low=excluded.low, close=excluded.close, volume=excluded.volume,
          ema10=excluded.ema10, ema20=excluded.ema20, ema30=excluded.ema30,
          ema50=excluded.ema50, rsi14=excluded.rsi14, adx14=excluded.adx14,
          hma21=excluded.hma21,
          fetched_at=excluded.fetched_at
    """, rows)
    connection.commit()
    return len(rows)


def fetch_symbol(client: FyersClient, connection: sqlite3.Connection, symbol: str,
                 table: str, limiter: ApiRateLimiter) -> list[Candle]:
    today = dt.date.today()
    start = today - dt.timedelta(days=HISTORY_DAYS)
    response = limiter.call(client.history, symbol, "15", start.isoformat(), today.isoformat())
    if response.get("s") != "ok":
        raise RuntimeError(f"history fetch failed: {response}")
    raw_candles = response.get("candles", [])
    candles = [Candle(int(row[0]), *map(float, row[1:6])) for row in raw_candles]
    if len(candles) > 1:
        candles = candles[:-1]
    store_candles(connection, symbol, table, candles)
    return candles


def log_signal(symbol: str, signal_type: str, details: dict) -> None:
    """Log every signal evaluation for auditability."""
    log_message(f"SIGNAL_EVAL: {symbol} type={signal_type} "
                f"prev_close={details.get('previous_close')} prev_hma={details.get('previous_hma21')} "
                f"curr_close={details.get('current_close')} curr_hma={details.get('current_hma21')}")


def exit_position_if_triggered(client: FyersClient, symbol: str,
                               candles: list[Candle], live: bool,
                               limiter: ApiRateLimiter) -> None:
    triggered, details = hma_crossed_down(candles)
    if not triggered:
        log_message(f"SIGNAL_SKIP: {symbol} HMA21 no cross - "
                    f"close={details.get('current_close')} hma21={details.get('current_hma21')}")
        return
    log_signal(symbol, "HMA_CROSS_DOWN", details)
    positions = limiter.call(client.positions).get("netPositions", [])
    for position in positions:
        position_symbol = position.get("symbol") or position.get("symbolName")
        net_qty = int(position.get("netQty", 0))
        if position_symbol != symbol or net_qty == 0:
            continue
        position_id = (position.get("id") or position.get("positionId")
                       or f"{symbol}-{position.get('productType', 'INTRADAY')}")
        log_message(f"POSITION_FOUND: {symbol} position={position_id} net_qty={net_qty}")
        log_message(f"MARKET_PRICE: {symbol} - ltp={details['current_close']}")
        log_message(f"EXIT_READY: {symbol} position={position_id} qty={abs(net_qty)} "
                    f"details={details}")
        order_request = {
            "position_id": position_id,
            "symbol": symbol,
            "quantity": abs(net_qty),
            "side": "EXIT",
        }
        log_message(f"ORDER_REQUEST: {json.dumps(order_request, sort_keys=True)}")
        response = limiter.call(client.exit_position, position_id, dry_run=not live)
        print_exit_result(symbol, position_id, response)
    else:
        log_message(f"NO_POSITION: {symbol} HMA cross triggered but no open position to exit")


def print_entry_result(symbol: str, order_id: str, response: dict) -> None:
    """Log the result of an entry order attempt."""
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


def enter_position_if_triggered(client: FyersClient, symbol: str,
                                candles: list[Candle], live: bool,
                                limiter: ApiRateLimiter,
                                entered: set[str]) -> None:
    """Place a BUY order when EMA10/EMA50 + RSI entry conditions are met.

    Skips if:
      - Symbol was already entered today (persisted to disk)
      - Max orders per day reached
    """
    SCRIPT_NAME = "agentNifty50.py"
    if symbol in entered:
        return
    if len(entered) >= MAX_ORDERS_PER_DAY:
        log_message(f"ENTRY_SKIP: Max orders ({MAX_ORDERS_PER_DAY}) reached for today")
        log_to_excel(STRATEGY_NAME, SCRIPT_NAME, symbol, "SIGNAL_MATCH", "SKIPPED", details="Max orders reached")
        return
    triggered, details = ema_rsi_entry_signal(candles)
    if not triggered:
        return
    log_signal(symbol, "EMA_RSI_ENTRY", details)

    positions = limiter.call(client.positions).get("netPositions", [])
    for pos in positions:
        pos_symbol = pos.get("symbol") or pos.get("symbolName")
        net_qty = int(pos.get("netQty", 0))
        if pos_symbol == symbol and net_qty != 0:
            log_message(f"ENTRY_SKIP: {symbol} already has open position (net_qty={net_qty})")
            log_to_excel(STRATEGY_NAME, SCRIPT_NAME, symbol, "SIGNAL_MATCH", "SKIPPED", details=f"Open position exists, net_qty={net_qty}")
            entered.add(symbol)
            save_entered(entered)
            return

    order = {
        "symbol": symbol,
        "qty": ENTRY_QTY,
        "type": ENTRY_ORDER_TYPE,
        "side": 1,                       # 1 = BUY
        "productType": ENTRY_PRODUCT_TYPE,
        "orderTag": "ema_rsi_entry",
    }
    meta = {"strategy": STRATEGY_NAME, "signal": "ENTRY", "description": f"EMA/RSI entry for {symbol}"}
    log_message(f"ENTRY_ORDER: {json.dumps(order, sort_keys=True)}")
    response = limiter.call(client.place_order, order, dry_run=not live, meta=meta)
    order_id = response.get("id") or response.get("id_fyers", "not_returned")
    print_entry_result(symbol, order_id, response)
    order_status = "DRY_RUN" if not live else "PLACED"
    log_to_excel(STRATEGY_NAME, SCRIPT_NAME, symbol, "ENTRY", order_status, order_id=order_id, details=f"Qty={ENTRY_QTY}")
    entered.add(symbol)
    save_entered(entered)


def exit_position_if_hma_cross(client: FyersClient, symbol: str,
                               candles_15min: list[Candle], live: bool,
                               limiter: ApiRateLimiter) -> None:
    """Exit position if 15-min candle closes below HMA(21)."""
    SCRIPT_NAME = "EquityNifty50Ema10crossover50-15min.py"
    if len(candles_15min) < 21:
        return
    
    closes = [c.close for c in candles_15min]
    hma_values = hma(closes, 21)
    if hma_values[-1] is None:
        return
    
    current_close = candles_15min[-1].close
    current_hma = hma_values[-1]
    
    if current_close >= current_hma:
        return
    
    # Get open positions
    try:
        positions = limiter.call(client.positions).get("netPositions", [])
        for pos in positions:
            pos_symbol = pos.get("symbol") or pos.get("symbolName")
            net_qty = int(pos.get("netQty", 0))
            if pos_symbol == symbol and net_qty != 0:
                order = {
                    "symbol": pos_symbol,
                    "qty": abs(net_qty),
                    "type": 2,
                    "side": -1,
                    "productType": ENTRY_PRODUCT_TYPE,
                    "orderTag": "hma21_exit",
                }
                meta = {"strategy": STRATEGY_NAME, "signal": "EXIT", "description": f"HMA21 exit for {symbol}, close={current_close} < HMA21={current_hma:.2f}"}
                log_message(f"HMA_EXIT_ORDER: {json.dumps(order, sort_keys=True)}")
                response = limiter.call(client.place_order, order, dry_run=not live, meta=meta)
                order_id = response.get("id") or response.get("id_fyers", "not_returned")
                log_message(f"HMA_EXIT_RESULT: {symbol} order_id={order_id} response={response}")
                log_to_excel(STRATEGY_NAME, SCRIPT_NAME, symbol, "EXIT_HMA21", "PLACED" if live else "DRY_RUN", order_id=order_id, details=f"Close={current_close} < HMA21={current_hma:.2f}")
    except Exception as e:
        log_error(f"HMA_EXIT_ERROR: {symbol} {e}")


def main() -> int:
    clear_log_on_new_day()
    clear_entered_on_new_day()
    try:
        symbols = read_stocks()
        if not symbols:
            raise ValueError(f"no symbols found in {STOCKS_PATH}")
        DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
        client = FyersClient()
        limiter = ApiRateLimiter()
        entered = load_entered()
        log_message(f"STARTED [{STRATEGY_NAME}]: {len(symbols)} symbols, interval={POLL_SECONDS}s, "
                    f"market={MARKET_OPEN}-{MARKET_CLOSE} IST, "
                    f"already_entered={len(entered)}")
        with sqlite3.connect(DATABASE_PATH) as connection:
            while market_open():
                log_message(f"FETCH_CYCLE: {dt.datetime.now(MARKET_TIMEZONE).isoformat(timespec='seconds')}")
                for symbol in symbols:
                    table = table_name(symbol)
                    initialize_database(connection, table)
                    try:
                        candles = fetch_symbol(client, connection, symbol, table, limiter)
                        count = len(candles)
                        enter_position_if_triggered(client, symbol, candles, LIVE, limiter, entered)
                        # Check HMA(21) exit on 15-min candles
                        exit_position_if_hma_cross(client, symbol, candles, LIVE, limiter)
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