#!/usr/bin/env python3
"""Index Rejection Strategy for NIFTY, SENSEX & BANKNIFTY Options.

Strategy Rules:
  NIFTY & SENSEX:
    - PUT: 15min candle high >= 26 EMA, close < 26 EMA, 9 EMA < 26 EMA, RSI <= 50
    - CALL: 15min candle low <= 9 EMA, close > 26 EMA, RSI > 50

  BANKNIFTY:
    - PUT: 15min candle high >= 35 EMA, close < 35 EMA, 9 EMA < 35 EMA, RSI <= 50
    - CALL: 15min candle low <= 9 EMA, close > 35 EMA, RSI > 50

  Stop Loss: 15 points on all trades

Usage:
    python strategies/scripts/IndexRejectionStrategy.py --live
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sqlite3
import sys
import time
import pathlib
from dataclasses import dataclass
from zoneinfo import ZoneInfo

SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parents[1]
sys.path.insert(0, str(REPO_ROOT / "skills" / "fyers-trading" / "scripts"))
sys.path.insert(0, str(REPO_ROOT / "strategies" / "scripts"))
sys.path.insert(0, str(REPO_ROOT / "strategies" / "utils"))
sys.path.insert(0, str(REPO_ROOT / "strategies"))

from fyers_client import FyersAuthError, FyersClient  # noqa: E402
from fyers_symbols import load_master  # noqa: E402
from common_indicators import ema, rsi, hma  # noqa: E402
from excel_logger import log_to_excel  # noqa: E402

# =============================================================================
# Configuration
# =============================================================================
LIVE = True
MARKET_TIMEZONE = ZoneInfo("Asia/Kolkata")
MARKET_OPEN = dt.time(9, 15)
MARKET_CLOSE = dt.time(15, 15)
POLL_SECONDS = 300  # 5 minutes

STRATEGIES_DIR = SCRIPT_DIR.parent
DATABASE_PATH_15MIN = STRATEGIES_DIR / "databases" / "index_rejection_15min.db"
POSITIONS_PATH = STRATEGIES_DIR / "data" / "index_rejection_positions.json"
ENTERED_PATH = STRATEGIES_DIR / "data" / "index_rejection_entered.json"
LOG_PATH = STRATEGIES_DIR / "logs" / "index_rejection_strategy.log"

STOP_LOSS_POINTS = 15  # 15 points stop loss

ENTRY_ORDER_TYPE = 20  # MARKET
ENTRY_PRODUCT_TYPE = "INTRADAY"
ENTRY_QTY = 10
MAX_ORDERS_PER_DAY = 3  # Maximum orders per day
STRATEGY_NAME = "INDEX_REJECTION_STRATEGY"

# Rejection is tested at 26 ema for nifty and sensex
# Rejection is tested at 35 ema for BankNifty


# Index symbols with their EMA periods
INDEX_CONFIG = {
    "NIFTY": {
        "fy_symbol": "NSE:NIFTY50-INDEX",
        "rejection_ema": 26,
        "support_ema": 9,
        "option_prefix": "NIFTY",
    },
    "SENSEX": {
        "fy_symbol": "BSE:SENSEX-INDEX",
        "rejection_ema": 26,
        "support_ema": 9,
        "option_prefix": "SENSEX",
    },
    "BANKNIFTY": {
        "fy_symbol": "NSE:NIFTYBANK-INDEX",
        "rejection_ema": 35,
        "support_ema": 9,
        "option_prefix": "BANKNIFTY",
    },
}

CE_OPTION_TYPE = "CE"
PE_OPTION_TYPE = "PE"


# =============================================================================
# Data Classes
# =============================================================================
@dataclass
class Candle:
    epoch: float
    open: float
    high: float
    low: float
    close: float
    volume: float


# =============================================================================
# Utility Functions
# =============================================================================
def log_message(msg: str) -> None:
    timestamp = dt.datetime.now(MARKET_TIMEZONE).strftime("%H:%M:%S")
    line = f"{timestamp} {msg}"
    print(line, file=sys.stderr)
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG_PATH, "a") as f:
            f.write(line + "\n")
    except OSError:
        pass


def log_error(msg: str) -> None:
    log_message(f"ERROR: {msg}")


def market_open() -> bool:
    now = dt.datetime.now(MARKET_TIMEZONE)
    return MARKET_OPEN <= now.time() <= MARKET_CLOSE and now.weekday() < 5


def clear_log_on_new_day() -> None:
    today = dt.date.today().isoformat()
    if LOG_PATH.exists():
        first_line = LOG_PATH.read_text().splitlines()
        if first_line and today not in first_line[0]:
            LOG_PATH.write_text(f"{today}\n")


# =============================================================================
# Database Functions
# =============================================================================
def table_name(symbol: str, interval: str) -> str:
    return f"candles_{symbol.replace(':', '_').replace('-', '_')}_{interval}min"


def initialize_database(conn: sqlite3.Connection, table: str) -> None:
    conn.execute(f"""
        CREATE TABLE IF NOT EXISTS {table} (
            epoch    INTEGER PRIMARY KEY,
            open     REAL,
            high     REAL,
            low      REAL,
            close    REAL,
            volume   REAL
        )
    """)
    conn.commit()


def get_cached_candles(conn: sqlite3.Connection, table: str, limit: int = 50) -> list[Candle]:
    cursor = conn.execute(f"""
        SELECT epoch, open, high, low, close, volume
        FROM {table}
        ORDER BY epoch DESC
        LIMIT {limit}
    """)
    rows = cursor.fetchall()
    return [Candle(epoch=r[0], open=r[1], high=r[2], low=r[3], close=r[4], volume=r[5]) for r in reversed(rows)]


def cache_candles(conn: sqlite3.Connection, table: str, candles: list[Candle]) -> None:
    for c in candles:
        conn.execute(
            f"INSERT OR REPLACE INTO {table} (epoch, open, high, low, close, volume) VALUES (?, ?, ?, ?, ?, ?)",
            (c.epoch, c.open, c.high, c.low, c.close, c.volume)
        )
    conn.commit()


# =============================================================================
# Candle Fetching
# =============================================================================
def fetch_candles(client: FyersClient, limiter: 'ApiRateLimiter', symbol: str,
                  resolution: str, limit: int = 50) -> list[Candle]:
    try:
        response = limiter.call(
            client.history,
            symbol=symbol,
            resolution=resolution,
            date_format="1",
            limit=limit,
            flag="0",
        )

        if response.get("s") != "ok":
            return []

        candles = []
        data = response.get("candles", [])
        for row in data:
            if len(row) >= 6:
                candles.append(Candle(
                    epoch=row[0],
                    open=row[1],
                    high=row[2],
                    low=row[3],
                    close=row[4],
                    volume=row[5],
                ))
        return candles
    except Exception as e:
        log_error(f"FETCH_ERROR {symbol}: {e}")
        return []


# =============================================================================
# Option Chain & Symbol Resolution
# =============================================================================
def get_atm_option_symbol(client: FyersClient, limiter: 'ApiRateLimiter',
                          index_name: str, option_type: str, ltp: float) -> tuple[str, float] | None:
    """Get ATM option symbol for the given index."""
    config = INDEX_CONFIG.get(index_name)
    if not config:
        return None

    try:
        response = limiter.call(
            client.option_chain,
            config["fy_symbol"],
            strikecount=10,
            greeks=False,
        )
        if response.get("s") != "ok":
            return None

        data = response.get("data", {})
        chains = data.get("optionsChain", [])
        expiry = data.get("expiry", "")

        if not chains or not expiry:
            return None

        # Find nearest strike to LTP
        strikes = set()
        for item in chains:
            strike = item.get("strike_price", 0)
            if strike > 0:
                strikes.add(strike)

        if not strikes:
            return None

        nearest_strike = min(strikes, key=lambda x: abs(x - ltp))

        # Find lot size from master
        master_data = load_master("NSE_FO")
        lot_size = 1
        option_symbol = None

        prefix = config["option_prefix"]
        for sym, v in master_data.items():
            underlying = v.get("exSymbol", "")
            strike = v.get("strikePrice", -1)
            opt_type = v.get("optionType", "")
            exp = v.get("expiryDate", "")

            if (underlying == prefix and
                strike == nearest_strike and
                opt_type == option_type and
                exp == expiry):
                option_symbol = sym
                lot_size = v.get("lotSize", 1)
                break

        if not option_symbol:
            return None

        fy_token = None
        for sym, v in master_data.items():
            if sym == option_symbol:
                fy_token = v.get("fyToken", "")
                break

        if fy_token:
            option_symbol = f"NSE:{option_symbol}"

        log_message(f"ATM_OPTION: {index_name} ltp={ltp} strike={nearest_strike} "
                    f"option={option_symbol} type={option_type} lot={lot_size}")
        return option_symbol, lot_size

    except Exception as e:
        log_error(f"OPTION_CHAIN_ERROR {index_name}: {e}")
        return None


# =============================================================================
# Signal Detection
# =============================================================================
def index_rejection_signal(candles_15min: list[Candle], index_name: str) -> tuple[str, dict]:
    """Detect rejection pattern for index options.

    NIFTY/SENSEX:
      - PUT: high >= 26 EMA, close < 26 EMA, 9 EMA < 26 EMA, RSI <= 50
      - CALL: low <= 9 EMA, close > 26 EMA, RSI > 50

    BANKNIFTY:
      - PUT: high >= 35 EMA, close < 35 EMA, 9 EMA < 35 EMA, RSI <= 50
      - CALL: low <= 9 EMA, close > 35 EMA, RSI > 50
    """
    config = INDEX_CONFIG.get(index_name)
    if not config:
        return "NONE", {"reason": f"unknown index {index_name}"}

    rejection_ema_period = config["rejection_ema"]
    support_ema_period = config["support_ema"]

    if len(candles_15min) < max(rejection_ema_period, support_ema_period) + 5:
        return "NONE", {"reason": "insufficient candles"}

    today = dt.date.today()
    today_candles = []
    for c in candles_15min:
        candle_dt = dt.datetime.fromtimestamp(c.epoch, MARKET_TIMEZONE)
        if candle_dt.date() == today:
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

    rejection_ema_values = ema(closes, rejection_ema_period)
    support_ema_values = ema(closes, support_ema_period)
    rsi_values = rsi(closes)

    curr_rejection_ema = rejection_ema_values[-1]
    curr_support_ema = support_ema_values[-1]
    curr_rsi = rsi_values[-1]

    if curr_rejection_ema is None or curr_support_ema is None or curr_rsi is None:
        return "NONE", {"reason": "insufficient EMA/RSI data"}

    details = {
        "strategy": f"STRATEGY_NAME_{index_name}",
        "index": index_name,
        "curr_close": curr_close,
        "curr_high": curr_high,
        "curr_low": curr_low,
        "curr_time": curr_dt.isoformat(),
        f"ema{rejection_ema_period}": curr_rejection_ema,
        f"ema{support_ema_period}": curr_support_ema,
        "curr_rsi": curr_rsi,
    }

    # PUT rejection: high >= rejection EMA, close < rejection EMA
    cond_put_high_test = curr_high >= curr_rejection_ema
    cond_put_close_below = curr_close < curr_rejection_ema
    cond_put_trend = curr_support_ema < curr_rejection_ema
    cond_put_rsi = curr_rsi <= 50

    if cond_put_high_test and cond_put_close_below and cond_put_trend and cond_put_rsi:
        log_message(f"PUT_SIGNAL: {index_name} - high={curr_high} >= ema{rejection_ema_period}={curr_rejection_ema:.2f} | "
                    f"close={curr_close} < ema{rejection_ema_period}={curr_rejection_ema:.2f} | "
                    f"ema{support_ema_period}={curr_support_ema:.2f} < ema{rejection_ema_period}={curr_rejection_ema:.2f} | "
                    f"rsi={curr_rsi:.1f} <= 50 | time={curr_dt}")
        return "PE", details

    # CALL rejection: low <= support EMA, close > rejection EMA
    cond_call_low_test = curr_low <= curr_support_ema
    cond_call_close_above = curr_close > curr_rejection_ema
    cond_call_rsi = curr_rsi > 50

    if cond_call_low_test and cond_call_close_above and cond_call_rsi:
        log_message(f"CALL_SIGNAL: {index_name} - low={curr_low} <= ema{support_ema_period}={curr_support_ema:.2f} | "
                    f"close={curr_close} > ema{rejection_ema_period}={curr_rejection_ema:.2f} | "
                    f"rsi={curr_rsi:.1f} > 50 | time={curr_dt}")
        return "CE", details

    return "NONE", details


# =============================================================================
# Position Management
# =============================================================================
def load_positions() -> dict:
    if POSITIONS_PATH.exists():
        try:
            return json.loads(POSITIONS_PATH.read_text())
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def save_positions(data: dict) -> None:
    POSITIONS_PATH.parent.mkdir(parents=True, exist_ok=True)
    POSITIONS_PATH.write_text(json.dumps(data, indent=2))


def load_entered() -> set:
    if ENTERED_PATH.exists():
        try:
            return set(json.loads(ENTERED_PATH.read_text()))
        except (json.JSONDecodeError, OSError):
            return set()
    return set()


def save_entered(data: set) -> None:
    ENTERED_PATH.parent.mkdir(parents=True, exist_ok=True)
    ENTERED_PATH.write_text(json.dumps(list(data)))


def clear_entered_on_new_day() -> None:
    today = dt.date.today().isoformat()
    if ENTERED_PATH.exists():
        try:
            data = json.loads(ENTERED_PATH.read_text())
            if isinstance(data, list) and data and today not in str(data[0]):
                ENTERED_PATH.write_text("[]")
        except (json.JSONDecodeError, OSError):
            pass


def monitor_stop_loss(client: FyersClient, limiter: 'ApiRateLimiter') -> None:
    """Check open positions and exit if stop loss is hit."""
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

                        if ltp > 0 and entry_price > 0:
                            pnl = ltp - entry_price
                            if pnl <= -STOP_LOSS_POINTS:
                                log_message(f"STOP_LOSS_HIT: {symbol} entry={entry_price} ltp={ltp} pnl={pnl:.2f}")
                                exit_position(client, symbol, net_qty, limiter, pos_id)
                except Exception as e:
                    log_error(f"QUOTE_ERROR {symbol}: {e}")


def exit_position(client: FyersClient, symbol: str, qty: int,
                  limiter: 'ApiRateLimiter', pos_id: str) -> None:
    """Exit position at market price."""
    order = {
        "symbol": symbol,
        "qty": qty,
        "type": 20,
        "side": -1,
        "productType": "INTRADAY",
        "orderTag": "stop_loss_exit",
    }
    meta = {"strategy": STRATEGY_NAME, "signal": "EXIT", "description": f"Index rejection stop loss exit for {symbol}"}
    log_message(f"EXIT_ORDER: {json.dumps(order, sort_keys=True)}")
    response = limiter.call(client.place_order, order, dry_run=not LIVE, meta=meta)

    if response.get("s") in ("ok", "dry_run"):
        positions = load_positions()
        positions.pop(pos_id, None)
        save_positions(positions)
        log_message(f"EXIT_SUCCESS: {symbol}")


def close_all_positions(client: FyersClient, limiter: 'ApiRateLimiter') -> None:
    """Close all open positions at market price."""
    positions = load_positions()
    if not positions:
        log_message("NO_POSITIONS: No open positions to close")
        return

    log_message(f"CLOSING_ALL: {len(positions)} positions to close")

    try:
        live_positions = limiter.call(client.positions).get("netPositions", [])
    except Exception as e:
        log_error(f"POSITIONS_FETCH_ERROR: {e}")
        return

    closed_count = 0
    for pos_id, pos_data in list(positions.items()):
        symbol = pos_data.get("symbol")
        entry_price = pos_data.get("entry_price", 0)

        for live_pos in live_positions:
            live_symbol = live_pos.get("symbol") or live_pos.get("symbolName")
            net_qty = int(live_pos.get("netQty", 0))

            if live_symbol == symbol and net_qty != 0:
                try:
                    quote_response = limiter.call(client.quotes, [symbol])
                    ltp = 0
                    if quote_response.get("s") == "ok":
                        quote_data = quote_response.get("d", [{}])[0].get("v", {})
                        ltp = quote_data.get("lp", 0)

                    pnl = ltp - entry_price if ltp > 0 and entry_price > 0 else 0
                    log_message(f"CLOSING: {symbol} entry={entry_price} ltp={ltp} pnl={pnl:.2f} qty={net_qty}")

                    order = {
                        "symbol": symbol,
                        "qty": net_qty,
                        "type": 20,
                        "side": -1,
                        "productType": "INTRADAY",
                        "orderTag": "manual_close_all",
                    }
                    meta = {"strategy": STRATEGY_NAME, "signal": "CLOSE_ALL", "description": f"Manual close all for {symbol}"}
                    log_message(f"EXIT_ORDER: {json.dumps(order, sort_keys=True)}")
                    response = limiter.call(client.place_order, order, dry_run=not LIVE, meta=meta)

                    if response.get("s") in ("ok", "dry_run"):
                        closed_count += 1
                        log_message(f"EXIT_SUCCESS: {symbol}")
                except Exception as e:
                    log_error(f"EXIT_ERROR {symbol}: {e}")

    # Clear all positions
    save_positions({})
    log_message(f"CLOSED_ALL: {closed_count} positions closed")


def close_opposite_positions(client: FyersClient, index_name: str, signal: str,
                             limiter: 'ApiRateLimiter', entered: set) -> None:
    """Close existing position if opposite signal is detected.

    If holding CALL and PUT signal detected → close CALL
    If holding PUT and CALL signal detected → close PUT
    """
    positions = load_positions()
    if not positions:
        return

    opposite_type = PE_OPTION_TYPE if signal == "CE" else CE_OPTION_TYPE

    try:
        live_positions = limiter.call(client.positions).get("netPositions", [])
    except Exception as e:
        log_error(f"POSITIONS_FETCH_ERROR: {e}")
        return

    for pos_id, pos_data in list(positions.items()):
        symbol = pos_data.get("symbol")
        pos_option_type = pos_data.get("option_type", "")

        # Check if this position is for the same index
        is_index_position = any(
            symbol and config["option_prefix"] in symbol
            for config in INDEX_CONFIG.values()
        )

        if not is_index_position:
            continue

        # Close if holding opposite option type
        if pos_option_type == opposite_type:
            for live_pos in live_positions:
                live_symbol = live_pos.get("symbol") or live_pos.get("symbolName")
                net_qty = int(live_pos.get("netQty", 0))

                if live_symbol == symbol and net_qty != 0:
                    try:
                        quote_response = limiter.call(client.quotes, [symbol])
                        ltp = 0
                        entry_price = pos_data.get("entry_price", 0)
                        if quote_response.get("s") == "ok":
                            quote_data = quote_response.get("d", [{}])[0].get("v", {})
                            ltp = quote_data.get("lp", 0)

                        pnl = ltp - entry_price if ltp > 0 and entry_price > 0 else 0
                        log_message(f"OPPOSITE_SIGNAL_CLOSE: {index_name} signal={signal} | "
                                    f"closing {pos_option_type} {symbol} entry={entry_price} ltp={ltp} pnl={pnl:.2f}")

                        order = {
                            "symbol": symbol,
                            "qty": net_qty,
                            "type": 20,
                            "side": -1,
                            "productType": "INTRADAY",
                            "orderTag": f"opposite_signal_{signal.lower()}",
                        }
                        meta = {"strategy": STRATEGY_NAME, "signal": "OPPOSITE_SIGNAL_EXIT", "description": f"Opposite signal exit for {symbol} due to {signal}"}
                        log_message(f"EXIT_ORDER: {json.dumps(order, sort_keys=True)}")
                        response = limiter.call(client.place_order, order, dry_run=not LIVE, meta=meta)

                        if response.get("s") in ("ok", "dry_run"):
                            positions.pop(pos_id, None)
                            save_positions(positions)
                            entered.discard(index_name)
                            save_entered(entered)
                            log_message(f"OPPOSITE_SIGNAL_EXIT_SUCCESS: {symbol}")
                    except Exception as e:
                        log_error(f"EXIT_ERROR {symbol}: {e}")


def enter_position(client: FyersClient, index_name: str, option_type: str,
                   ltp: float, limiter: 'ApiRateLimiter', entered: set) -> None:
    """Enter new position."""
    SCRIPT_NAME = "IndexRejectionStrategy.py"
    if len(entered) >= MAX_ORDERS_PER_DAY:
        log_message(f"ENTRY_SKIP: Max orders ({MAX_ORDERS_PER_DAY}) reached for today")
        log_to_excel(STRATEGY_NAME, SCRIPT_NAME, index_name, "SIGNAL_MATCH", "SKIPPED", details="Max orders reached")
        return
    result = get_atm_option_symbol(client, limiter, index_name, option_type, ltp)
    if not result:
        log_error(f"NO_OPTION: {index_name} {option_type}")
        log_to_excel(STRATEGY_NAME, SCRIPT_NAME, index_name, "SIGNAL_MATCH", "FAILED", details=f"No option found for {option_type}")
        return

    option_symbol, lot_size = result
    order_qty = ENTRY_QTY * lot_size

    positions_data = load_positions()
    for pos_data in positions_data.values():
        if pos_data.get("symbol") == option_symbol:
            log_message(f"ENTRY_SKIP: {index_name} already holding {option_symbol}")
            log_to_excel(STRATEGY_NAME, SCRIPT_NAME, index_name, "SIGNAL_MATCH", "SKIPPED", details=f"Open position exists: {option_symbol}")
            return

    try:
        live_positions = limiter.call(client.positions).get("netPositions", [])
        for pos in live_positions:
            pos_symbol = pos.get("symbol") or pos.get("symbolName")
            net_qty = int(pos.get("netQty", 0))
            if pos_symbol == option_symbol and net_qty != 0:
                log_message(f"ENTRY_SKIP: {index_name} already has open position ({option_symbol}, net_qty={net_qty})")
                log_to_excel(STRATEGY_NAME, SCRIPT_NAME, index_name, "SIGNAL_MATCH", "SKIPPED", details=f"Open position exists: {option_symbol}")
                return
    except Exception as e:
        log_error(f"POSITION_CHECK_ERROR: {e}")

    order = {
        "symbol": option_symbol,
        "qty": order_qty,
        "type": ENTRY_ORDER_TYPE,
        "side": 1,
        "productType": ENTRY_PRODUCT_TYPE,
        "orderTag": f"index_rejection_{option_type.lower()}",
    }
    meta = {"strategy": STRATEGY_NAME, "signal": f"ENTRY_{option_type}", "description": f"Index rejection {option_type} entry for {index_name} at LTP {ltp}"}
    log_message(f"ENTRY_ORDER: {json.dumps(order, sort_keys=True)}")
    response = limiter.call(client.place_order, order, dry_run=not LIVE, meta=meta)
    order_id = response.get("id") or response.get("id_fyers", "not_returned")
    order_status = "DRY_RUN" if not LIVE else "PLACED"
    log_to_excel(STRATEGY_NAME, SCRIPT_NAME, index_name, f"ENTRY_{option_type}", order_status, order_id=order_id, details=f"Option={option_symbol}, Qty={order_qty}, LTP={ltp}")

    if response.get("s") in ("ok", "dry_run"):
        entered.add(index_name)
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
        log_message(f"ENTRY_SUCCESS: {index_name} {option_symbol} qty={order_qty}")


# =============================================================================
# Main Loop
# =============================================================================
class ApiRateLimiter:
    def __init__(self, calls_per_second: int = 5):
        self.interval = 1.0 / calls_per_second
        self.last_call = 0.0

    def call(self, function, *args, **kwargs):
        elapsed = time.monotonic() - self.last_call
        if elapsed < self.interval:
            time.sleep(self.interval - elapsed)
        self.last_call = time.monotonic()
        return function(*args, **kwargs)


def exit_position_if_hma_cross(client: FyersClient, index_name: str,
                               candles_15min: list, live: bool,
                               limiter: 'ApiRateLimiter') -> None:
    """Exit position based on HMA(21) condition.
    
    CE exit: 15-min candle close < HMA(21)
    PE exit: 15-min candle open >= HMA(21), close > open, close > HMA(21), upper_wick < body
    """
    SCRIPT_NAME = "IndexRejectionStrategy.py"
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
        if index_name in pos_symbol:
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
                            "type": 20,
                            "side": -1,
                            "productType": ENTRY_PRODUCT_TYPE,
                            "orderTag": "hma21_exit",
                        }
                        if option_type == "CE":
                            exit_reason = f"close={current_close} < HMA21={current_hma:.2f}"
                        else:
                            exit_reason = f"Bullish candle: open={current_open} >= HMA21={current_hma:.2f}, close={current_close} > open, upper_wick={upper_wick:.2f} < body={body:.2f}"
                        meta = {"strategy": STRATEGY_NAME, "signal": "EXIT", "description": f"HMA21 {option_type} exit for {index_name}, {exit_reason}"}
                        log_message(f"HMA_EXIT_ORDER: {json.dumps(order, sort_keys=True)}")
                        response = limiter.call(client.place_order, order, dry_run=not live, meta=meta)
                        order_id = response.get("id") or response.get("id_fyers", "not_returned")
                        log_message(f"HMA_EXIT_RESULT: {index_name} order_id={order_id} response={response}")
                        log_to_excel(STRATEGY_NAME, SCRIPT_NAME, index_name, "EXIT_HMA21", "PLACED" if live else "DRY_RUN", order_id=order_id, details=f"{option_type} {exit_reason}")
                        del positions_data[pos_id]
                        save_positions(positions_data)
                        return
            except Exception as e:
                log_error(f"HMA_EXIT_ERROR: {index_name} {e}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Index Rejection Strategy - NIFTY/SENSEX/BANKNIFTY")
    parser.add_argument("--live", action="store_true", help="Send real orders (default: dry-run)")
    parser.add_argument("--close", action="store_true", help="Close all open positions and exit")
    args = parser.parse_args()

    live = args.live or LIVE
    if live:
        log_message("LIVE MODE: real orders will be placed")

    clear_log_on_new_day()
    clear_entered_on_new_day()

    try:
        client = FyersClient()
        limiter = ApiRateLimiter()

        # Close all positions if --close flag is provided
        if args.close:
            log_message("CLOSE_MODE: Closing all open positions")
            close_all_positions(client, limiter)
            return 0

        entered = load_entered()
        log_message(f"STARTED [{STRATEGY_NAME}]: indices={list(INDEX_CONFIG.keys())}, interval={POLL_SECONDS}s, "
                    f"market={MARKET_OPEN}-{MARKET_CLOSE} IST, "
                    f"already_entered={len(entered)}, stop_loss={STOP_LOSS_POINTS} points")

        with sqlite3.connect(DATABASE_PATH_15MIN) as conn_15min:
            while market_open():
                now = dt.datetime.now(MARKET_TIMEZONE)
                log_message(f"FETCH_CYCLE: {now.isoformat(timespec='seconds')}")

                # Auto-close positions at 15:10 (5 min before market close)
                if now.time() >= dt.time(15, 10):
                    log_message("AUTO_CLOSE: Market closing soon, closing all positions")
                    close_all_positions(client, limiter)
                    break

                monitor_stop_loss(client, limiter)

                for index_name, config in INDEX_CONFIG.items():
                    fy_symbol = config["fy_symbol"]
                    table_15m = table_name(index_name, "15")
                    initialize_database(conn_15min, table_15m)

                    try:
                        candles_15min = fetch_candles(client, limiter, fy_symbol, "15", limit=50)
                        if candles_15min:
                            cache_candles(conn_15min, table_15m, candles_15min)
                            # Check HMA(21) exit on 15-min candles
                            exit_position_if_hma_cross(client, index_name, candles_15min, live, limiter)

                        cached_candles = get_cached_candles(conn_15min, table_15m)
                        if len(cached_candles) < 5:
                            log_message(f"INSUFFICIENT_CANDLES: {index_name}")
                            continue

                        signal, details = index_rejection_signal(cached_candles, index_name)
                        if signal != "NONE":
                            log_message(f"SIGNAL_{signal}: {index_name}")
                            ltp = cached_candles[-1].close

                            # Close opposite positions first
                            close_opposite_positions(client, index_name, signal, limiter, entered)

                            # Enter new position if not already holding
                            if index_name not in entered:
                                option_type = CE_OPTION_TYPE if signal == "CE" else PE_OPTION_TYPE
                                enter_position(client, index_name, option_type, ltp, limiter, entered)

                    except (RuntimeError, ValueError) as error:
                        log_error(f"{index_name}: {error}")

                time.sleep(POLL_SECONDS)

        log_message("STOPPED: market closed")

    except (FyersAuthError, OSError, RuntimeError, ValueError, sqlite3.Error) as error:
        log_error(str(error))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
