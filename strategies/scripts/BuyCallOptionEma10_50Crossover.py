#!/usr/bin/env python3
"""5-minute candle strategy: EMA 10/50 crossover + RSI55 -> ATM CE option entry."""
from __future__ import annotations

import argparse
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
LOG_PATH = STRATEGIES_DIR / "logs" / "BuyCallOptionEma10_50Crossover.log"
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

STOCKS_PATH = STRATEGIES_DIR / "data" / "Nifty50.txt"
DATABASE_PATH = STRATEGIES_DIR / "databases" / "nifty50_5min.db"
ENTERED_PATH = STRATEGIES_DIR / "data" / "entered_ema10_50.json"
STRATEGY_NAME = "EMA_10_CROSSOVER_50_5MIN"

# --- Tuning constants --------------------------------------------------------
LIVE = True                          # True = send real orders
SKIP_MARKET_HOURS = False             # True = run outside market hours (test mode)
FORCE_ENTRY = False                   # True = place test order ignoring signal (test mode)
POLL_SECONDS = 30                    # test: 30s (prod: 5 * 60)
HISTORY_DAYS = 10                    # calendar days of candle history to fetch
MARKET_TIMEZONE = ZoneInfo("Asia/Kolkata")
MARKET_OPEN = dt.time(9, 15)
MARKET_CLOSE = dt.time(15, 45)
MAX_API_CALLS_PER_SECOND = 8
ENTRY_QTY = 10                         # number of lots per entry (lot size fetched from master)
ENTRY_ORDER_TYPE = 2                  # 1=Limit 2=Market
ENTRY_PRODUCT_TYPE = "INTRADAY"       # INTRADAY | CNC | MARGIN
RSI_ENTRY_THRESHOLD = 55
STRIKE_STEP = 50                      # default, overridden dynamically per stock
OPTION_TYPE = "CE"                    # CE for buy/bullish signals
MAX_ORDERS_PER_DAY = 3                # Maximum orders per day
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


def table_name(symbol: str) -> str:
    safe_symbol = re.sub(r"[^A-Za-z0-9_]+", "_", symbol).strip("_")
    return f"candles_5m_{safe_symbol}"


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
            fetched_at TEXT NOT NULL,
            PRIMARY KEY (symbol, epoch)
        )
    """)
    connection.commit()


def store_candles(connection: sqlite3.Connection, symbol: str,
                  table: str, candles: list[Candle]) -> int:
    closes = [candle.close for candle in candles]
    indicators = (ema(closes, 10), ema(closes, 20), ema(closes, 30), ema(closes, 50),
                  rsi(closes), adx(candles))
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
         ema10, ema20, ema30, ema50, rsi14, adx14, fetched_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(symbol, epoch) DO UPDATE SET
          candle_time=excluded.candle_time, open=excluded.open, high=excluded.high,
          low=excluded.low, close=excluded.close, volume=excluded.volume,
          ema10=excluded.ema10, ema20=excluded.ema20, ema30=excluded.ema30,
          ema50=excluded.ema50, rsi14=excluded.rsi14, adx14=excluded.adx14,
          fetched_at=excluded.fetched_at
    """, rows)
    connection.commit()
    return len(rows)


def fetch_symbol(client: FyersClient, connection: sqlite3.Connection, symbol: str,
                 table: str, limiter: ApiRateLimiter) -> list[Candle]:
    today = dt.date.today()
    start = today - dt.timedelta(days=HISTORY_DAYS)
    response = limiter.call(client.history, symbol, "5", start.isoformat(), today.isoformat())
    if response.get("s") != "ok":
        raise RuntimeError(f"history fetch failed: {response}")
    raw_candles = response.get("candles", [])
    candles = [Candle(int(row[0]), *map(float, row[1:6])) for row in raw_candles]
    if len(candles) > 1:
        candles = candles[:-1]
    store_candles(connection, symbol, table, candles)
    return candles


def round_to_strike(price: float, step: int) -> float:
    """Round price to nearest strike given the stock's strike spacing."""
    return round(price / step) * step


def get_strike_step(chains: list[dict]) -> int:
    """Calculate strike step from option chain (min difference between consecutive strikes)."""
    strikes = sorted(set(item.get("strike_price", 0) for item in chains if item.get("strike_price", 0) > 0))
    if len(strikes) < 2:
        return 50  # fallback
    diffs = [strikes[i+1] - strikes[i] for i in range(len(strikes)-1)]
    return int(min(diffs)) if diffs else 50


def resolve_atm_option(client: FyersClient, stock_symbol: str,
                       ltp: float, limiter: ApiRateLimiter) -> tuple[str, int] | None:
    """Find the ATM option symbol (CE) for the given stock and LTP.

    Returns (option_symbol, lot_size) or None if not found.
    """
    response = limiter.call(client.option_chain, stock_symbol, strikecount=5, greeks=False)
    if response.get("s") != "ok":
        log_error(f"option_chain failed for {stock_symbol}: {response}")
        return None

    data = response.get("data", {})
    chains = data.get("optionsChain", []) or data.get("chain", [])

    # Get strike step dynamically from option chain
    strike_step = get_strike_step(chains)
    atm_strike = round_to_strike(ltp, strike_step)
    best_symbol = None
    best_diff = float("inf")
    best_lot = None
    nearest_expiry = None

    # Load NSE_FO master to get lot sizes
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

        if opt_type != OPTION_TYPE:
            continue
        diff = abs(strike - atm_strike)
        if diff < best_diff:
            best_diff = diff
            best_symbol = sym
            nearest_expiry = expiry

    if not best_symbol:
        log_error(f"ATM_OPTION_NOT_FOUND: {stock_symbol} ltp={ltp} atm_strike={atm_strike}")
        return None

    # Get lot size from FO master
    rec = fo_master.get(best_symbol, {})
    if not rec:
        log_error(f"LOT_SIZE_NOT_FOUND: {best_symbol} not in FO master")
        return None
    best_lot = int(rec.get("minLotSize", 0))
    if best_lot <= 0:
        log_error(f"INVALID_LOT_SIZE: {best_symbol} lot_size={best_lot}")
        return None

    log_message(f"ATM_OPTION: {stock_symbol} ltp={ltp} strike_step={strike_step} atm_strike={atm_strike} "
                f"option={best_symbol} lot={best_lot} expiry={nearest_expiry}")
    return best_symbol, best_lot


def ema10_ema50_entry_signal(candles: list[Candle]) -> tuple[bool, dict]:
    """Detect EMA 10/50 crossover + RSI entry on 5-min candles.

    Entry rules:
      - Previous candle: close < EMA10 AND EMA10 < EMA50
      - Current candle: close > EMA10
      - EMA10 crosses above EMA50: prev EMA10 <= EMA50 AND curr EMA10 > EMA50
      - RSI14 > threshold (current candle)
    """
    if len(candles) < 3:
        return False, {"reason": "insufficient candles"}
    closes = [candle.close for candle in candles]
    ema10_vals = ema(closes, 10)
    ema50_vals = ema(closes, 50)
    rsi_vals = rsi(closes)

    prev_idx, curr_idx = -2, -1
    required = (ema10_vals[prev_idx], ema50_vals[prev_idx], rsi_vals[prev_idx],
                ema10_vals[curr_idx], ema50_vals[curr_idx], rsi_vals[curr_idx])
    if any(v is None for v in required):
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
    cond_rsi_above = curr_rsi > RSI_ENTRY_THRESHOLD

    triggered = cond_prev_below and cond_curr_cross_above and cond_ema_cross and cond_rsi_above
    if triggered:
        log_message(f"ema10_ema50_entry: PREV close={prev_close} < ema10={prev_ema10} < ema50={prev_ema50} -> "
                    f"CURR close={curr_close} > ema10={curr_ema10} | ema10>ema50 | rsi={curr_rsi:.1f}")
    return triggered, details


def log_signal(symbol: str, signal_type: str, details: dict) -> None:
    log_message(f"SIGNAL_EVAL: {symbol} type={signal_type} "
                f"prev_close={details.get('prev_close')} curr_close={details.get('curr_close')} "
                f"prev_ema10={details.get('prev_ema10')} curr_ema10={details.get('curr_ema10')} "
                f"prev_ema50={details.get('prev_ema50')} curr_ema50={details.get('curr_ema50')} "
                f"prev_rsi={details.get('prev_rsi')} curr_rsi={details.get('curr_rsi')}")


def has_open_position(client: FyersClient, symbol: str, limiter: ApiRateLimiter) -> bool:
    """Check if there's already an open position for the underlying stock."""
    positions = limiter.call(client.positions).get("netPositions", [])
    for pos in positions:
        pos_symbol = pos.get("symbol") or pos.get("symbolName")
        net_qty = int(pos.get("netQty", 0))
        if pos_symbol == symbol and net_qty != 0:
            return True
    return False


def is_market_bearish(shared_fetcher) -> bool:
    """Check if market is bearish based on multiple conditions.
    
    Bearish conditions:
    1. EMA20 < EMA50 and close below EMA20
    2. Opened above previous day's high but closed below it (failed breakout)
    """
    try:
        nifty_candles = shared_fetcher.get_candles("NSE:NIFTY50-INDEX")
        if not nifty_candles or len(nifty_candles) < 50:
            return False
        
        # Find previous day's high
        today = dt.date.today()
        prev_day_high = None
        for candle in reversed(nifty_candles):
            if candle.timestamp.date() < today:
                prev_day_high = candle.high
                break
        if prev_day_high is None:
            return False
        
        # Current candle data
        current_candle = nifty_candles[-1]
        current_close = current_candle.close
        current_open = current_candle.open
        
        # Condition 1: EMA trend
        closes = [c.close for c in nifty_candles]
        ema20_vals = ema(closes, 20)
        ema50_vals = ema(closes, 50)
        if ema20_vals[-1] is None or ema50_vals[-1] is None:
            return False
        current_ema20 = ema20_vals[-1]
        current_ema50 = ema50_vals[-1]
        
        # Condition 2: Failed breakout
        failed_breakout = current_open > prev_day_high and current_close < prev_day_high
        
        # Bearish if either condition is true
        is_bearish = (current_ema20 < current_ema50 and current_close < current_ema20) or failed_breakout
        
        if is_bearish:
            if failed_breakout:
                log_message(f"MARKET_BEARISH: NIFTY50 failed breakout - open={current_open} > prev_high={prev_day_high} but close={current_close} < prev_high")
            else:
                log_message(f"MARKET_BEARISH: NIFTY50 close={current_close} < EMA20={current_ema20:.2f} < EMA50={current_ema50:.2f}")
        return is_bearish
    except Exception as e:
        log_error(f"MARKET_CHECK_ERROR: {e}")
        return False


def enter_position_if_triggered(client: FyersClient, symbol: str,
                                candles: list[Candle], live: bool,
                                limiter: ApiRateLimiter,
                                entered: set[str],
                                shared_fetcher=None) -> None:
    """Place a BUY order for ATM CE option when entry conditions are met.

    Skips if:
      - Symbol was already entered today (persisted to disk)
      - There's an existing open position for this stock
      - Max orders per day reached
      - Market is bearish (NIFTY50 below EMA20)
    """
    SCRIPT_NAME = "BuyCallOptionEma10_50Crossover.py"
    if symbol in entered:
        return
    if len(entered) >= MAX_ORDERS_PER_DAY:
        log_message(f"ENTRY_SKIP: Max orders ({MAX_ORDERS_PER_DAY}) reached for today")
        log_to_excel(STRATEGY_NAME, SCRIPT_NAME, symbol, "SIGNAL_MATCH", "SKIPPED", details="Max orders reached")
        return
    if shared_fetcher and is_market_bearish(shared_fetcher):
        log_message(f"ENTRY_SKIP: {symbol} - market is bearish, skipping CE entry")
        log_to_excel(STRATEGY_NAME, SCRIPT_NAME, symbol, "SIGNAL_MATCH", "SKIPPED", details="Market bearish")
        return

    # Check for existing open position
    if has_open_position(client, symbol, limiter):
        log_message(f"ENTRY_SKIP: {symbol} already has open position")
        log_to_excel(STRATEGY_NAME, SCRIPT_NAME, symbol, "SIGNAL_MATCH", "SKIPPED", details="Open position exists")
        entered.add(symbol)
        save_entered(entered)
        return

    triggered, details = ema10_ema50_entry_signal(candles)
    if not triggered and not FORCE_ENTRY:
        return
    if FORCE_ENTRY and not triggered:
        log_message(f"FORCE_ENTRY: {symbol} - bypassing signal check for testing")
        details = {"forced": True, "ltp": candles[-1].close}
    log_signal(symbol, "EMA10_EMA50_ENTRY", details)

    ltp = candles[-1].close
    result = resolve_atm_option(client, symbol, ltp, limiter)
    if not result:
        log_error(f"ENTRY_ABORT: {symbol} - could not resolve ATM option for ltp={ltp}")
        log_to_excel(STRATEGY_NAME, SCRIPT_NAME, symbol, "SIGNAL_MATCH", "FAILED", details=f"ATM resolve failed, LTP={ltp}")
        return
    option_symbol, lot_size = result
    order_qty = ENTRY_QTY * lot_size

    # Double-check: don't place if already holding this specific option
    positions = limiter.call(client.positions).get("netPositions", [])
    for pos in positions:
        pos_symbol = pos.get("symbol") or pos.get("symbolName")
        net_qty = int(pos.get("netQty", 0))
        if pos_symbol == option_symbol and net_qty != 0:
            log_message(f"ENTRY_SKIP: {symbol} already holding option ({option_symbol}, net_qty={net_qty})")
            log_to_excel(STRATEGY_NAME, SCRIPT_NAME, symbol, "SIGNAL_MATCH", "SKIPPED", details=f"Open position exists: {option_symbol}")
            entered.add(symbol)
            save_entered(entered)
            return

    order = {
        "symbol": option_symbol,
        "qty": order_qty,
        "type": ENTRY_ORDER_TYPE,
        "side": 1,
        "productType": ENTRY_PRODUCT_TYPE,
        "orderTag": "ema1050",
    }
    meta = {"strategy": STRATEGY_NAME, "signal": "ENTRY", "description": f"EMA 10/50 crossover entry for {symbol} at LTP {ltp}"}
    log_message(f"ENTRY_ORDER: {json.dumps(order, sort_keys=True)}")
    response = limiter.call(client.place_order, order, dry_run=not live, meta=meta)
    order_id = response.get("id") or response.get("id_fyers", "not_returned")
    print_entry_result(option_symbol, order_id, response)
    order_status = "DRY_RUN" if not live else "PLACED"
    log_to_excel(STRATEGY_NAME, SCRIPT_NAME, symbol, "ENTRY", order_status, order_id=order_id, details=f"Option={option_symbol}, Qty={order_qty}, LTP={ltp}")
    entered.add(symbol)
    save_entered(entered)


def exit_position_if_hma_cross(client: FyersClient, symbol: str,
                               candles_15min: list[Candle], live: bool,
                               limiter: ApiRateLimiter) -> None:
    """Exit position if 15-min candle closes below HMA(21)."""
    SCRIPT_NAME = "BuyCallOptionEma10_50Crossover.py"
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
    parser = argparse.ArgumentParser(description="5-min EMA 10/50 crossover options strategy")
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
        DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
        client = FyersClient()
        limiter = ApiRateLimiter()
        entered = load_entered()
        shared_fetcher = SharedDataFetcher()
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
                        enter_position_if_triggered(client, symbol, candles, live, limiter, entered, shared_fetcher)
                        # Check HMA(21) exit on 15-min candles
                        candles_15min = shared_fetcher.get_candles(symbol)
                        if candles_15min:
                            exit_position_if_hma_cross(client, symbol, candles_15min, live, limiter)
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
