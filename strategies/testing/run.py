#!/usr/bin/env python3
"""Scan 5-minute FYERS candles and optionally buy the ATM call on a signal."""
from __future__ import annotations

import datetime as dt
import importlib.util
import json
import pathlib
import sys
from dataclasses import dataclass
from datetime import datetime
from datetime import time as dt_time
from zoneinfo import ZoneInfo

SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parents[1]
WORKSPACE_ROOT = REPO_ROOT
sys.path.insert(0, str(WORKSPACE_ROOT))
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "strategies"))
LOG_PATH = SCRIPT_DIR.parent / "logs" / "run.log"
STOCKS_PATH = SCRIPT_DIR / "stocks.txt"
DEFAULT_DAYS = 10
DEFAULT_QTY = 250
LIVE = False
MARKET_TIMEZONE = ZoneInfo("Asia/Kolkata")
MARKET_OPEN = dt_time(9, 15)
LAST_INTRADAY_ENTRY = dt_time(15, 20)
sys.path.insert(0, str(REPO_ROOT / "skills" / "fyers-trading" / "scripts"))

_indicator_spec = importlib.util.spec_from_file_location(
    "common_indicators", SCRIPT_DIR.parent / "common_indicators.py"
)
assert _indicator_spec and _indicator_spec.loader
_indicator_module = importlib.util.module_from_spec(_indicator_spec)
_indicator_spec.loader.exec_module(_indicator_module)
ema, rsi = _indicator_module.ema, _indicator_module.rsi
from fyers_client import FyersAuthError, FyersClient  # noqa: E402
from fyers_symbols import validate_symbol  # noqa: E402
from option_chain import atm_strike, parse_chain  # noqa: E402


@dataclass(frozen=True)
class Candle:
    epoch: int
    open: float
    high: float
    low: float
    close: float
    volume: float


def log_message(message: str, error: bool = False) -> None:
    """Write a timestamped message to the current run's log and console."""
    entry = f"{datetime.now().isoformat(timespec='seconds')} {message}"
    with LOG_PATH.open("a", encoding="utf-8") as log_file:
        log_file.write(entry + "\n")
    print(entry, file=sys.stderr if error else sys.stdout)


def print_order_result(symbol: str, response: dict) -> None:
    """Print and log a concise order outcome from a FYERS response."""
    status = response.get("s")
    code = response.get("code")
    message = response.get("message", "")
    if status == "dry_run":
        log_message(f"DRY-RUN: {symbol} - no order sent")
    elif status == "ok":
        order_id = response.get("id") or response.get("id_fyers", "not returned")
        log_message(f"PLACED: {symbol} - order_id={order_id} - {message}")
    elif status == "error" or (isinstance(code, int) and code < 0):
        log_message(f"REJECTED: {symbol} - code={code} - {message}", error=True)
    else:
        log_message(f"UNKNOWN: {symbol} - response={response}", error=True)


def candles_from_response(response: dict) -> list[Candle]:
    return [Candle(int(row[0]), *map(float, row[1:6])) for row in response.get("candles", [])]


def entry_signal(candles: list[Candle], rsi_period: int = 14) -> tuple[bool, dict]:
    """Evaluate the latest closed candle and return (signal, indicator snapshot)."""
    closes = [candle.close for candle in candles]
    ema10, ema20, ema30 = ema(closes, 10), ema(closes, 20), ema(closes, 30)
    rsi_values = rsi(closes, rsi_period)
    if len(candles) < 31 or any(value is None for value in (
        ema10[-2], ema10[-1], ema20[-2], ema20[-1], ema30[-2], ema30[-1], rsi_values[-1]
    )):
        return False, {"reason": "insufficient indicator data"}

    previous, current = candles[-2], candles[-1]
    current_ema10, current_ema20, current_ema30 = ema10[-1], ema20[-1], ema30[-1]
    previous_ema10, previous_ema20, previous_ema30 = ema10[-2], ema20[-2], ema30[-2]
    body = abs(current.close - current.open)
    upper_wick = current.high - max(current.open, current.close)
    conditions = {
        "ema10_crossed_ema20": previous_ema10 <= previous_ema20 and current_ema10 > current_ema20,
        "ema20_crossed_ema30": previous_ema20 <= previous_ema30 and current_ema20 > current_ema30,
        "rsi_above_60": rsi_values[-1] > 60,
        "close_above_ema10": current.close > current_ema10,
        "bullish_candle": current.close > current.open,
        "upper_wick_less_than_body": upper_wick < body,
    }
    snapshot = {
        "close": current.close, "ema10": current_ema10, "ema20": current_ema20,
        "ema30": current_ema30, "rsi": rsi_values[-1], "conditions": conditions,
    }
    return all(conditions.values()), snapshot


def read_stocks(path: pathlib.Path) -> list[str]:
    return [line.strip() for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")]


def intraday_market_open() -> bool:
    """Return whether a new MIS/INTRADAY order can normally be entered."""
    now = datetime.now(MARKET_TIMEZONE)
    return now.weekday() < 5 and MARKET_OPEN <= now.time() <= LAST_INTRADAY_ENTRY


def scan_symbol(client: FyersClient, underlying: str, days: int, qty: int, live: bool) -> dict:
    today = dt.date.today()
    start = today - dt.timedelta(days=days)
    response = client.history(underlying, "5", start.isoformat(), today.isoformat())
    if response.get("s") != "ok":
        raise RuntimeError(f"history fetch failed: {response}")
    candles = candles_from_response(response)
    if len(candles) < 2:
        return {"underlying": underlying, "signal": False, "reason": "no candles"}
    signal, snapshot = entry_signal(candles[:-1])
    result = {"underlying": underlying, "signal": signal, "snapshot": snapshot}
    if not signal:
        return result

    chain = parse_chain(client.option_chain(underlying, strikecount=1))
    strike = atm_strike(chain["calls"], chain["spot"])
    option = chain["calls"][strike]
    contract = validate_symbol(option["symbol"])
    lot_size = int(contract.get("minLotSize", 1))
    order = {
        "symbol": option["symbol"], "qty": qty,
        "type": 2, "side": 1, "productType": "INTRADAY", "orderTag": "emarsiatm",
    }
    result["option"] = {"symbol": option["symbol"], "strike": strike, "lot_size": lot_size}
    if live:
        log_message(f"LIVE order requested for {option['symbol']}. Type the exact symbol to confirm:")
        if input().strip() != option["symbol"]:
            log_message("Confirmation failed; using dry-run.", error=True)
            result["order"] = client.place_order(order, dry_run=True)
            return result
    result["order"] = client.place_order(order, dry_run=not live)
    return result


def main() -> int:
    LOG_PATH.write_text("", encoding="utf-8")
    if DEFAULT_DAYS < 1 or DEFAULT_QTY < 1:
        log_message("ERROR: DEFAULT_DAYS and DEFAULT_QTY must be positive", error=True)
        return 1
    try:
        if LIVE and not intraday_market_open():
            log_message("SKIPPED: market is closed or the intraday entry window has ended; "
                        "FYERS would reject this MIS order after system square-off")
            return 0
        client = FyersClient()
        for underlying in read_stocks(STOCKS_PATH):
            result = scan_symbol(client, underlying, DEFAULT_DAYS, DEFAULT_QTY, LIVE)
            if "order" in result:
                log_message(
                    "ORDER_READY: "
                    + json.dumps({
                        "underlying": underlying,
                        "option": result["option"],
                        "order": result["order"],
                        "indicators": result["snapshot"],
                    }, sort_keys=True)
                )
                print_order_result(result["option"]["symbol"], result["order"])
            else:
                log_message(f"SIGNAL: {underlying} - {result}")
    except (FyersAuthError, OSError, RuntimeError, ValueError) as error:
        log_message(f"ERROR: {error}", error=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())