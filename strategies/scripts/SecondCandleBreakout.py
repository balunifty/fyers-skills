#!/usr/bin/env python3
"""Second Candle Breakout Strategy (15min).

CE Entry - Buy CALL option based on the second 15-min candle (9:30-9:45):
  - If second candle is BULLISH (close > open):
      Entry when any subsequent candle's HIGH breaks above second candle's HIGH.
  - If second candle is BEARISH (close < open):
      Entry when the third candle (9:45-10:00) CLOSES above second candle's OPEN.
  Fixed and trailing risk levels come from strategies/config/fno/config.json.

PE Entry - Buy PUT option when first two candles are bullish but third candle shows weakness:
  - First candle (9:15-9:30) bullish, second candle (9:30-9:45) bullish.
  - Third candle (9:45-10:00) close < second candle high AND
    (upper_wick > body OR close < open OR third high < second high).
  Fixed and trailing stops use percentages from strategies/config/fno/config.json.
"""
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
LOG_PATH = STRATEGIES_DIR / "logs" / "SecondCandleBreakout.log"
LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
LOG_PATH.touch(exist_ok=True)
sys.path.insert(0, str(WORKSPACE_ROOT))
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
from shared_data_fetcher import SharedDataFetcher  # noqa: E402
from excel_logger import log_to_excel  # noqa: E402

STOCKS_PATH = STRATEGIES_DIR / "data" / "Indices.txt"
ENTERED_PATH = STRATEGIES_DIR / "data" / "entered_second_candle.json"
POSITIONS_PATH = STRATEGIES_DIR / "data" / "second_candle_positions.json"
STRATEGY_NAME = "SECOND_CANDLE_BREAKOUT"
SCRIPT_NAME = "SecondCandleBreakout.py"

# --- Tuning constants --------------------------------------------------------
LIVE = True
SKIP_MARKET_HOURS = False
POLL_SECONDS = 300
MARKET_TIMEZONE = ZoneInfo("Asia/Kolkata")
MARKET_OPEN = dt.time(9, 15)
MARKET_CLOSE = dt.time(15, 30)
MAX_API_CALLS_PER_SECOND = 8
ENTRY_ORDER_TYPE = 1                  # 1=Limit 2=Market
ENTRY_PRODUCT_TYPE = "INTRADAY"
CE_OPTION_TYPE = "CE"
PE_OPTION_TYPE = "PE"
LIMIT_BUFFER_PERCENT = 0.10   # 10% of candle range for limit order buffer
# --------------------------------------------------------------------------------


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
    entry = f"{dt.datetime.now().isoformat(timespec='seconds')} [{STRATEGY_NAME}] ERROR: {message}"
    with LOG_PATH.open("a", encoding="utf-8") as log_file:
        log_file.write(entry + "\n")
    print(entry, file=sys.stderr)


def log_message(message: str, error: bool = False) -> None:
    entry = f"{dt.datetime.now().isoformat(timespec='seconds')} [{STRATEGY_NAME}] {message}"
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


def get_today_candles(candles: list) -> list:
    """Filter candles to only today's data."""
    today = dt.date.today()
    today_candles = []
    for c in candles:
        candle_dt = dt.datetime.fromtimestamp(c.epoch, MARKET_TIMEZONE)
        if candle_dt.date() == today:
            today_candles.append(c)
    return today_candles


def get_candle_by_time(candles: list, hour: int, minute: int):
    """Get a specific 15-min candle of today by its start time."""
    today = dt.date.today()
    for candle in candles:
        candle_dt = dt.datetime.fromtimestamp(candle.epoch, MARKET_TIMEZONE)
        if candle_dt.date() == today and candle_dt.time() == dt.time(hour, minute):
            return candle
    return None


def second_candle_breakout_signal(candles_15min: list, second_candle, third_candle) -> tuple[str, dict]:
    """Check for CE entry based on second candle type.

    Bullish second candle (close > open):
        Entry when any subsequent candle's HIGH breaks above second candle's HIGH.
    Bearish second candle (close < open):
        Entry when the third candle CLOSES above second candle's OPEN.

    Stop loss is always the LOW of the second candle.

    Returns:
        ("CE", details) if breakout detected
        ("NONE", details) if no signal
    """
    if second_candle is None:
        return "NONE", {"reason": "second candle not found"}

    if len(candles_15min) < 3:
        return "NONE", {"reason": "insufficient candles"}

    today_candles = get_today_candles(candles_15min)
    if len(today_candles) < 3:
        return "NONE", {"reason": "insufficient today candles for breakout check"}

    second_candle_high = second_candle.high
    second_candle_low = second_candle.low
    second_candle_open = second_candle.open
    second_candle_close = second_candle.close
    second_candle_dt = dt.datetime.fromtimestamp(second_candle.epoch, MARKET_TIMEZONE)
    is_bullish = second_candle_close > second_candle_open

    details = {
        "strategy": "SECOND_CANDLE_BREAKOUT",
        "second_candle_type": "BULLISH" if is_bullish else "BEARISH",
        "second_candle_open": second_candle_open,
        "second_candle_close": second_candle_close,
        "second_candle_high": second_candle_high,
        "second_candle_low": second_candle_low,
        "second_candle_time": second_candle_dt.isoformat(),
        "stop_loss_percent": get_stop_loss_percent(SCRIPT_NAME),
        "trailing_stop_percent": get_trailing_stop_loss_percent(SCRIPT_NAME),
    }

    if is_bullish:
        # Bullish: any subsequent candle high breaks above second candle high
        curr_candle = today_candles[-1]
        curr_high = curr_candle.high
        curr_close = curr_candle.close
        curr_low = curr_candle.low
        curr_epoch = curr_candle.epoch
        curr_dt = dt.datetime.fromtimestamp(curr_epoch, MARKET_TIMEZONE)

        if curr_dt <= second_candle_dt:
            return "NONE", {"reason": "current candle not after second candle"}

        details["curr_high"] = curr_high
        details["curr_close"] = curr_close
        details["curr_low"] = curr_low
        details["curr_time"] = curr_dt.isoformat()

        if curr_high > second_candle_high:
            log_message(f"BREAKOUT_SIGNAL[BULLISH]: CE - curr_high={curr_high} > second_candle_high={second_candle_high} "
                        f"stop_loss={get_stop_loss_percent(SCRIPT_NAME):.2%} time={curr_dt}")
            return "CE", details

    else:
        # Bearish: third candle must close above second candle open
        if third_candle is None:
            return "NONE", {"reason": "third candle not found yet"}

        third_close = third_candle.close
        third_epoch = third_candle.epoch
        third_dt = dt.datetime.fromtimestamp(third_epoch, MARKET_TIMEZONE)

        if third_dt <= second_candle_dt:
            return "NONE", {"reason": "third candle not after second candle"}

        details["third_candle_close"] = third_close
        details["third_candle_time"] = third_dt.isoformat()

        if third_close > second_candle_open:
            log_message(f"BREAKOUT_SIGNAL[BEARISH]: CE - third_close={third_close} > second_candle_open={second_candle_open} "
                        f"stop_loss={get_stop_loss_percent(SCRIPT_NAME):.2%} time={third_dt}")
            return "CE", details

    return "NONE", details


def bearish_reversal_signal(candles_15min: list, first_candle, second_candle, third_candle) -> tuple[str, dict]:
    """Check for PE entry when two bullish candles are followed by a weak third candle.

    Conditions:
      - First candle (9:15) is bullish (close > open)
      - Second candle (9:30) is bullish (close > open)
      - Third candle (9:45) closes below second candle's high AND
        shows weakness: upper_wick > body, or close < open, or high < second high

    Returns:
        ("PE", details) if signal detected
        ("NONE", details) if no signal
    """
    if first_candle is None or second_candle is None or third_candle is None:
        return "NONE", {"reason": "missing candles"}

    first_bullish = first_candle.close > first_candle.open
    second_bullish = second_candle.close > second_candle.open

    if not first_bullish or not second_bullish:
        return "NONE", {"reason": "first two candles not both bullish"}

    third_close = third_candle.close
    third_open = third_candle.open
    third_high = third_candle.high
    third_low = third_candle.low
    third_epoch = third_candle.epoch
    third_dt = dt.datetime.fromtimestamp(third_epoch, MARKET_TIMEZONE)

    second_candle_high = second_candle.high
    second_candle_dt = dt.datetime.fromtimestamp(second_candle.epoch, MARKET_TIMEZONE)

    if third_dt <= second_candle_dt:
        return "NONE", {"reason": "third candle not after second candle"}

    close_below_second_high = third_close < second_candle_high
    body = abs(third_close - third_open)
    upper_wick = third_high - max(third_close, third_open)
    rejection = upper_wick > body
    bearish_close = third_close < third_open
    lower_high = third_high < second_candle_high

    weakness = rejection or bearish_close or lower_high

    details = {
        "strategy": "BEARISH_REVERSAL",
        "first_candle_type": "BULLISH",
        "second_candle_type": "BULLISH",
        "second_candle_high": second_candle_high,
        "third_candle_close": third_close,
        "third_candle_high": third_high,
        "third_candle_time": third_dt.isoformat(),
        "upper_wick": upper_wick,
        "body": body,
        "close_below_second_high": close_below_second_high,
        "rejection": rejection,
        "bearish_close": bearish_close,
        "lower_high": lower_high,
        "stop_loss_percent": get_stop_loss_percent(SCRIPT_NAME),
        "trailing_stop_percent": get_trailing_stop_loss_percent(SCRIPT_NAME),
    }

    if close_below_second_high and weakness:
        log_message(f"BEARISH_REVERSAL_SIGNAL: PE - third_close={third_close} < second_high={second_candle_high} "
                    f"weakness: rejection={rejection} bearish={bearish_close} lower_high={lower_high} time={third_dt}")
        return "PE", details

    return "NONE", details


def monitor_stop_loss(client: FyersClient, limiter: ApiRateLimiter) -> None:
    """Exit tracked positions when the configured fixed or trailing stop hits."""
    stop_loss_percent = get_stop_loss_percent(SCRIPT_NAME)
    trailing_stop_percent = get_trailing_stop_loss_percent(SCRIPT_NAME)
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
        entry_price = float(pos_data.get("entry_price", 0) or 0)
        option_type = pos_data.get("option_type", "CE")
        highest_ltp = float(pos_data.get("highest_ltp", entry_price) or entry_price)

        for live_pos in live_positions:
            live_symbol = live_pos.get("symbol") or live_pos.get("symbolName")
            net_qty = int(live_pos.get("netQty", 0))

            if live_symbol == symbol and net_qty != 0:
                try:
                    quote_response = limiter.call(client.quotes, [symbol])
                    if quote_response.get("s") == "ok":
                        quote_data = quote_response.get("d", [{}])[0].get("v", {})
                        ltp = quote_data.get("lp", 0)

                        # Update highest LTP for trailing stop
                        if ltp > highest_ltp:
                            highest_ltp = ltp
                            pos_data["highest_ltp"] = highest_ltp
                            save_positions(positions)

                        # Fixed and percentage-based trailing stops share the tighter level.
                        if entry_price > 0 and highest_ltp > 0:
                            fixed_stop = entry_price * (1 - stop_loss_percent)
                            trailing_stop = highest_ltp * (1 - trailing_stop_percent)
                            sl = max(fixed_stop, trailing_stop)
                        else:
                            sl = 0

                        if ltp <= sl:
                            log_message(f"STOP_LOSS_HIT: {symbol} entry={entry_price} current={ltp} "
                                       f"highest={highest_ltp} sl={sl} type={option_type} - EXITING")
                            buffer = ltp * 0.02  # 2% buffer for exit
                            order = {
                                "symbol": symbol,
                                "qty": abs(net_qty),
                                "type": ENTRY_ORDER_TYPE,
                                "side": -1,
                                "productType": ENTRY_PRODUCT_TYPE,
                                "limitPrice": ltp + buffer,
                                "orderTag": f"{option_type.lower()}_sl",
                            }
                            meta = {"strategy": STRATEGY_NAME, "signal": "STOP_LOSS",
                                    "description": f"{option_type} SL hit at {sl}"}
                            response = limiter.call(client.place_order, order, dry_run=not LIVE, meta=meta, gap_threshold_pct=0)
                            log_message(f"STOP_LOSS_ORDER: {symbol} response={response}")
                            del positions[pos_id]
                            save_positions(positions)
                            return
                except Exception as e:
                    log_error(f"STOP_LOSS_CHECK_ERROR: {symbol} {e}")
                break

        if live_symbol != symbol or net_qty == 0:
            if pos_id in positions:
                del positions[pos_id]
                save_positions(positions)


# =============================================================================
# Candle Pattern Exit Condition
# =============================================================================
def candle_pattern_exit_check(client: FyersClient, limiter: ApiRateLimiter,
                               candles_15min: list, live: bool) -> None:
    """Exit positions based on candle pattern reversal conditions.

    Bearish exit (for CE positions):
      - Current candle opens below previous candle's close (gap down)
      - OR current candle open = high (pure bearish, no upper wick)
      Stoploss reference: previous candle's high

    Bullish exit (for PE positions):
      - Current candle opens above previous candle's close (gap up)
      - OR current candle open = low (pure bullish, no lower wick)
      Stoploss reference: previous candle's low
    """
    if len(candles_15min) < 2:
        return

    today = dt.date.today()
    today_candles = []
    for c in candles_15min:
        candle_dt = dt.datetime.fromtimestamp(c.epoch, MARKET_TIMEZONE)
        if candle_dt.date() == today:
            today_candles.append(c)

    if len(today_candles) < 2:
        return

    curr_candle = today_candles[-1]
    prev_candle = today_candles[-2]

    curr_open = curr_candle.open
    curr_high = curr_candle.high
    curr_low = curr_candle.low
    prev_close = prev_candle.close
    prev_high = prev_candle.high
    prev_low = prev_candle.low

    # Detect bearish conditions
    bearish_gap_down = curr_open < prev_close
    bearish_open_eq_high = curr_open == curr_high

    # Detect bullish conditions
    bullish_gap_up = curr_open > prev_close
    bullish_open_eq_low = curr_open == curr_low

    if not (bearish_gap_down or bearish_open_eq_high or bullish_gap_up or bullish_open_eq_low):
        return

    positions = load_positions()
    if not positions:
        return

    try:
        live_positions = limiter.call(client.positions).get("netPositions", [])
    except Exception as e:
        log_error(f"CANDLE_PATTERN_EXIT_POSITIONS_ERROR: {e}")
        return

    for pos_id, pos_data in list(positions.items()):
        symbol = pos_data.get("symbol")
        option_type = pos_data.get("option_type", "CE")

        # --- CE exit: bearish candle pattern ---
        if option_type == "CE" and (bearish_gap_down or bearish_open_eq_high):
            stop_ref = prev_high
            for live_pos in live_positions:
                live_symbol = live_pos.get("symbol") or live_pos.get("symbolName")
                net_qty = int(live_pos.get("netQty", 0))
                if live_symbol == symbol and net_qty != 0:
                    try:
                        quote_response = limiter.call(client.quotes, [symbol])
                        if quote_response.get("s") == "ok":
                            quote_data = quote_response.get("d", [{}])[0].get("v", {})
                            ltp = quote_data.get("lp", 0)
                            if ltp <= 0:
                                continue

                            if bearish_gap_down:
                                reason = f"GAP_DOWN: open={curr_open} < prev_close={prev_close}, prev_high={stop_ref}"
                            else:
                                reason = f"OPEN_EQ_HIGH: open={curr_open} = high={curr_high}, prev_high={stop_ref}"

                            log_message(f"CANDLE_PATTERN_EXIT[CE]: {symbol} {reason} - EXITING")
                            order = {
                                "symbol": symbol,
                                "qty": abs(net_qty),
                                "type": ENTRY_ORDER_TYPE,
                                "side": -1,
                                "productType": ENTRY_PRODUCT_TYPE,
                                "limitPrice": ltp,
                                "orderTag": "candle_pattern_ce_exit",
                            }
                            meta = {"strategy": STRATEGY_NAME, "signal": "CANDLE_PATTERN_EXIT",
                                    "description": f"CE exit: {reason}"}
                            response = limiter.call(client.place_order, order, dry_run=not live,
                                                     meta=meta, gap_threshold_pct=0)
                            log_message(f"CANDLE_PATTERN_EXIT_ORDER: {symbol} response={response}")
                            del positions[pos_id]
                            save_positions(positions)
                            return
                    except Exception as e:
                        log_error(f"CANDLE_PATTERN_EXIT_ERROR: {symbol} {e}")

        # --- PE exit: bullish candle pattern ---
        if option_type == "PE" and (bullish_gap_up or bullish_open_eq_low):
            stop_ref = prev_low
            for live_pos in live_positions:
                live_symbol = live_pos.get("symbol") or live_pos.get("symbolName")
                net_qty = int(live_pos.get("netQty", 0))
                if live_symbol == symbol and net_qty != 0:
                    try:
                        quote_response = limiter.call(client.quotes, [symbol])
                        if quote_response.get("s") == "ok":
                            quote_data = quote_response.get("d", [{}])[0].get("v", {})
                            ltp = quote_data.get("lp", 0)
                            if ltp <= 0:
                                continue

                            if bullish_gap_up:
                                reason = f"GAP_UP: open={curr_open} > prev_close={prev_close}, prev_low={stop_ref}"
                            else:
                                reason = f"OPEN_EQ_LOW: open={curr_open} = low={curr_low}, prev_low={stop_ref}"

                            log_message(f"CANDLE_PATTERN_EXIT[PE]: {symbol} {reason} - EXITING")
                            order = {
                                "symbol": symbol,
                                "qty": abs(net_qty),
                                "type": ENTRY_ORDER_TYPE,
                                "side": -1,
                                "productType": ENTRY_PRODUCT_TYPE,
                                "limitPrice": ltp,
                                "orderTag": "candle_pattern_pe_exit",
                            }
                            meta = {"strategy": STRATEGY_NAME, "signal": "CANDLE_PATTERN_EXIT",
                                    "description": f"PE exit: {reason}"}
                            response = limiter.call(client.place_order, order, dry_run=not live,
                                                     meta=meta, gap_threshold_pct=0)
                            log_message(f"CANDLE_PATTERN_EXIT_ORDER: {symbol} response={response}")
                            del positions[pos_id]
                            save_positions(positions)
                            return
                    except Exception as e:
                        log_error(f"CANDLE_PATTERN_EXIT_ERROR: {symbol} {e}")


def enter_position(client: FyersClient, symbol: str, ltp: float,
                   stop_loss_percent: float, option_type: str,
                   limiter: ApiRateLimiter, entered: set[str],
                   candle_range: float = 0) -> None:
    """Place a BUY order for ATM CE or PE option."""
    SCRIPT_NAME = "SecondCandleBreakout.py"
    if symbol in entered:
        return

    result = resolve_atm_option(client, symbol, ltp, option_type, limiter)
    if not result:
        log_error(f"ENTRY_ABORT: {symbol} - could not resolve ATM {option_type} for ltp={ltp}")
        log_to_excel(STRATEGY_NAME, SCRIPT_NAME, symbol, "SIGNAL_MATCH", "FAILED", details=f"ATM resolve failed, LTP={ltp}")
        return

    option_symbol, lot_size = result
    order_qty = get_entry_qty(SCRIPT_NAME) * lot_size

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

    tag = "second_candle_ce" if option_type == "CE" else "second_candle_pe"
    buffer = candle_range * LIMIT_BUFFER_PERCENT
    order = {
        "symbol": option_symbol,
        "qty": order_qty,
        "type": ENTRY_ORDER_TYPE,
        "side": 1,
        "productType": ENTRY_PRODUCT_TYPE,
        "limitPrice": ltp - buffer,
        "orderTag": tag,
    }
    meta = {"strategy": STRATEGY_NAME, "signal": f"ENTRY_{option_type}",
            "description": f"{option_type} entry for {symbol} at LTP {ltp}, SL={stop_loss_percent:.2%}, buffer={buffer:.2f}"}
    log_message(f"ENTRY_ORDER: {json.dumps(order, sort_keys=True)}")
    response = limiter.call(client.place_order, order, dry_run=not LIVE, meta=meta, gap_threshold_pct=0)
    order_id = response.get("id") or response.get("id_fyers", "not_returned")
    print_entry_result(option_symbol, order_id, response)
    order_status = "DRY_RUN" if not LIVE else "PLACED"
    log_to_excel(STRATEGY_NAME, SCRIPT_NAME, symbol, f"ENTRY_{option_type}", order_status, order_id=order_id,
                 details=f"Option={option_symbol}, Qty={order_qty}, LTP={ltp}, SL={stop_loss_percent:.2%}")

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
            "stop_loss_percent": stop_loss_percent,
            "trailing_stop_percent": get_trailing_stop_loss_percent(SCRIPT_NAME),
            "highest_ltp": ltp,
        }
        save_positions(positions_data)


def main() -> int:
    parser = argparse.ArgumentParser(description="Second Candle Breakout Strategy - Buy CE on 15min high break")
    parser.add_argument("--live", action="store_true", help="Send real orders (default: dry-run)")
    args = parser.parse_args()

    live = args.live or LIVE

    clear_log_on_new_day()
    clear_entered_on_new_day()

    try:
        symbols = read_stocks()
        if not symbols:
            raise ValueError(f"no symbols found in {STOCKS_PATH}")

        client = ConfigGatedFyersClient(
            strategy_name=STRATEGY_NAME,
            script_name=SCRIPT_NAME,
        )
        limiter = ApiRateLimiter()
        entered = load_entered()
        shared_fetcher = SharedDataFetcher()
        second_candles = {}
        third_candles = {}
        first_candles = {}

        while market_open():
            monitor_stop_loss(client, limiter)

            for symbol in symbols:
                try:
                    candles_15min = shared_fetcher.get_candles(symbol)
                    if not candles_15min:
                        continue

                    # Check candle pattern exit conditions
                    candle_pattern_exit_check(client, limiter, candles_15min, live)

                    if symbol not in first_candles:
                        fc = get_candle_by_time(candles_15min, 9, 15)
                        if fc:
                            first_candles[symbol] = fc

                    if symbol not in second_candles:
                        sc = get_candle_by_time(candles_15min, 9, 30)
                        if sc:
                            second_candles[symbol] = sc

                    if symbol not in second_candles:
                        continue

                    second_candle = second_candles[symbol]

                    if symbol not in third_candles:
                        tc = get_candle_by_time(candles_15min, 9, 45)
                        if tc:
                            third_candles[symbol] = tc

                    third_candle = third_candles.get(symbol)

                    # CE signal
                    signal, details = second_candle_breakout_signal(candles_15min, second_candle, third_candle)
                    if signal != "NONE" and symbol not in entered:
                        log_message(f"BREAKOUT_SIGNAL: {symbol} type={details.get('second_candle_type')}")
                        ltp = candles_15min[-1].close
                        stop_loss_percent = details["stop_loss_percent"]
                        candle_range = details.get('second_candle_high', 0) - details.get('second_candle_low', 0)
                        enter_position(client, symbol, ltp, stop_loss_percent, CE_OPTION_TYPE, limiter, entered, candle_range)
                        continue

                    # PE signal
                    first_candle = first_candles.get(symbol)
                    pe_signal, pe_details = bearish_reversal_signal(candles_15min, first_candle, second_candle, third_candle)
                    if pe_signal != "NONE" and symbol not in entered:
                        log_message(f"BEARISH_REVERSAL_SIGNAL: {symbol}")
                        ltp = candles_15min[-1].close
                        candle_range = pe_details.get('second_candle_high', 0) - pe_details.get('second_candle_low', 0)
                        enter_position(client, symbol, ltp, get_stop_loss_percent(SCRIPT_NAME), PE_OPTION_TYPE, limiter, entered, candle_range)

                except (RuntimeError, ValueError) as error:
                    log_error(f"{symbol}: {error}")

            time.sleep(POLL_SECONDS)

    except (FyersAuthError, OSError, RuntimeError, ValueError) as error:
        log_error(str(error))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
