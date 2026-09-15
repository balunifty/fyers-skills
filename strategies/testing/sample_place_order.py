#!/usr/bin/env python3
"""Safely test FYERS order construction and placement for symbols in stocks.txt."""
from __future__ import annotations

import argparse
import importlib.util
import json
import math
import pathlib
import sys
from datetime import datetime
from datetime import time as dt_time
from zoneinfo import ZoneInfo

SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parents[1]
LOG_PATH = SCRIPT_DIR.parent / "logs" / "sample_place_order.log"
DEFAULT_QTY = 1
PRICE_TICK = 0.05
MARKET_TIMEZONE = ZoneInfo("Asia/Kolkata")
MARKET_OPEN = dt_time(9, 15)
LAST_INTRADAY_ENTRY = dt_time(15, 20)
sys.path.insert(0, str(REPO_ROOT / "skills" / "fyers-trading" / "scripts"))

_client_spec = importlib.util.spec_from_file_location(
    "fyers_client", REPO_ROOT / "skills" / "fyers-trading" / "scripts" / "fyers_client.py"
)
assert _client_spec and _client_spec.loader
_client_module = importlib.util.module_from_spec(_client_spec)
sys.modules[_client_spec.name] = _client_module
_client_spec.loader.exec_module(_client_module)
FyersAuthError, FyersClient = _client_module.FyersAuthError, _client_module.FyersClient


def quote_price(client: FyersClient, symbol: str) -> tuple[float, float]:
    """Return the current LTP and a tick-rounded limit price for an equity."""
    response = client.quotes([symbol])
    if response.get("s") != "ok" or not response.get("d"):
        raise RuntimeError(f"quote fetch failed for {symbol}: {response}")
    quote = response["d"][0].get("v", {})
    ltp = quote.get("lp")
    if ltp is None:
        raise RuntimeError(f"quote did not contain LTP for {symbol}: {response}")
    limit_price = math.floor(float(ltp) / PRICE_TICK) * PRICE_TICK
    return float(ltp), round(limit_price, 2)


def log_message(message: str, error: bool = False) -> None:
    """Write a timestamped message to the current run's log and the console."""
    entry = f"{datetime.now().isoformat(timespec='seconds')} {message}"
    with LOG_PATH.open("a", encoding="utf-8") as log_file:
        log_file.write(entry + "\n")
    print(entry, file=sys.stderr if error else sys.stdout)


def print_order_result(symbol: str, response: dict) -> None:
    """Print a concise, human-readable order outcome from a FYERS response."""
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


def intraday_market_open() -> bool:
    """Return whether a new MIS/INTRADAY order can normally be entered."""
    now = datetime.now(MARKET_TIMEZONE)
    return now.weekday() < 5 and MARKET_OPEN <= now.time() <= LAST_INTRADAY_ENTRY


def main() -> int:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    LOG_PATH.write_text("", encoding="utf-8")
    log_message("STARTED: sample order script")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stocks", type=pathlib.Path, default=SCRIPT_DIR / "stocks.txt",
                        help="file containing one FYERS symbol per line (default: stocks.txt)")
    parser.add_argument("--qty", type=int, default=DEFAULT_QTY,
                        help=f"valid quantity for each symbol (default: {DEFAULT_QTY})")
    parser.add_argument("--live", action="store_true",
                        help="send real orders instead of the default dry-run")
    args = parser.parse_args()
    if args.qty < 1:
        parser.error("--qty must be positive")

    try:
        if args.live and not intraday_market_open():
            log_message(
                "SKIPPED: market is closed or the intraday entry window has ended; "
                "FYERS would reject this MIS order after system square-off"
            )
            return 0
        client = FyersClient()
        symbols = [line.strip() for line in args.stocks.read_text(encoding="utf-8").splitlines()
                   if line.strip() and not line.lstrip().startswith("#")]
        if not symbols:
            raise ValueError(f"no symbols found in {args.stocks}")
        for symbol in symbols:
            ltp, limit_price = quote_price(client, symbol)
            order = {
                "symbol": symbol,
                "qty": args.qty,
                "type": 1,             # Limit order; prevents buying above the observed LTP
                "side": 1,             # Buy
                "productType": "INTRADAY",
                "limitPrice": limit_price,
                "stopPrice": 0,
                "validity": "DAY",
                "offlineOrder": False,
                "orderTag": "sampletest",
            }
            mode = "LIVE" if args.live else "DRY-RUN"
            log_message(f"MARKET_PRICE: {symbol} - ltp={ltp}")
            log_message(f"{mode} BUY: {symbol}, quantity={args.qty}, ltp={ltp}, limit={limit_price}")
            log_message(f"ORDER_REQUEST: {json.dumps(order, sort_keys=True)}")
            result = client.place_order(order, dry_run=not args.live)
            print_order_result(symbol, result)
    except FyersAuthError as error:
        log_message(f"AUTHENTICATION ERROR: {error}", error=True)
        return 1
    except OSError as error:
        log_message(f"FILE ERROR: {error}", error=True)
        return 1
    except (RuntimeError, ValueError) as error:
        log_message(f"ERROR: {error}", error=True)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())