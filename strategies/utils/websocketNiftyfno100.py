#!/usr/bin/env python3
"""Build 15-minute NIFTY/BANKNIFTY/stock candles from the FYERS data WebSocket."""
from __future__ import annotations

import datetime as dt
import importlib.util
import pathlib
import re
import sqlite3
import sys
import threading
from dataclasses import dataclass
from zoneinfo import ZoneInfo

SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parents[1]
STRATEGIES_DIR = SCRIPT_DIR.parent
WORKSPACE_ROOT = REPO_ROOT
sys.path.insert(0, str(WORKSPACE_ROOT))
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "strategies"))
sys.path.insert(0, str(REPO_ROOT / "strategies" / "utils"))
sys.path.insert(0, str(REPO_ROOT / "skills" / "fyers-trading" / "scripts"))
sys.path.insert(0, str(SCRIPT_DIR))

from fyers_client import FyersAuthError  # noqa: E402
from fyers_login import load_token  # noqa: E402
_indicator_spec = importlib.util.spec_from_file_location(
    "common_indicators", STRATEGIES_DIR / "utils" / "common_indicators.py"
)
assert _indicator_spec and _indicator_spec.loader
_indicator_module = importlib.util.module_from_spec(_indicator_spec)
_indicator_spec.loader.exec_module(_indicator_module)
ema, hma, rsi, wma = (_indicator_module.ema, _indicator_module.hma,
                       _indicator_module.rsi, _indicator_module.wma)

STOCKS_PATH = STRATEGIES_DIR / "data" / "NiftyFNOTop100.txt"
DATABASE_PATH = STRATEGIES_DIR / "databases" / "NiftyFNOTop100_websocket.db"
LOG_PATH = STRATEGIES_DIR / "logs" / "websocketNiftyFNOTop100.log"
INDEX_SYMBOLS = ["NSE:NIFTY50-INDEX", "NSE:NIFTYBANK-INDEX"]
MARKET_TIMEZONE = ZoneInfo("Asia/Kolkata")
MARKET_OPEN = dt.time(9, 15)
MARKET_CLOSE = dt.time(15, 45)


@dataclass
class Candle:
    start: dt.datetime
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0

    def update(self, price: float, volume: float = 0.0) -> None:
        self.high = max(self.high, price)
        self.low = min(self.low, price)
        self.close = price
        self.volume = max(self.volume, volume)


def log_error(message: str) -> None:
    entry = f"{dt.datetime.now().isoformat(timespec='seconds')} ERROR: {message}"
    with LOG_PATH.open("a", encoding="utf-8") as log_file:
        log_file.write(entry + "\n")
    print(entry, file=sys.stderr)


def table_name(symbol: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_]+", "_", symbol).strip("_")
    return f"candles_15m_{safe}"


def read_symbols() -> list[str]:
    stocks = [line.strip() for line in STOCKS_PATH.read_text(encoding="utf-8").splitlines()
              if line.strip() and not line.lstrip().startswith("#")]
    return list(dict.fromkeys(INDEX_SYMBOLS + stocks))


def initialize_table(connection: sqlite3.Connection, table: str) -> None:
    connection.execute(f"""
        CREATE TABLE IF NOT EXISTS {table} (
            candle_time TEXT PRIMARY KEY,
            open REAL NOT NULL, high REAL NOT NULL, low REAL NOT NULL,
            close REAL NOT NULL, volume REAL NOT NULL,
            ema10 REAL, ema20 REAL, ema30 REAL, hma21 REAL, rsi14 REAL
        )
    """)
    connection.commit()


class CandleStore:
    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection
        self.lock = threading.Lock()
        self.active: dict[str, Candle] = {}
        self.history: dict[str, list[Candle]] = {}

    def update(self, symbol: str, timestamp: int, price: float, volume: float) -> None:
        candle_start = dt.datetime.fromtimestamp(timestamp, MARKET_TIMEZONE).replace(
            minute=(dt.datetime.fromtimestamp(timestamp, MARKET_TIMEZONE).minute // 15) * 15,
            second=0, microsecond=0
        )
        current = self.active.get(symbol)
        if current is not None and candle_start <= current.start:
            current.update(price, volume)
            return
        if current is not None:
            self.persist(symbol, current)
        self.active[symbol] = Candle(candle_start, price, price, price, price, volume)

    def persist(self, symbol: str, candle: Candle) -> None:
        history = self.history.setdefault(symbol, [])
        history.append(candle)
        history[:] = history[-250:]
        closes = [item.close for item in history]
        values = (ema(closes, 10)[-1], ema(closes, 20)[-1], ema(closes, 30)[-1],
                  hma(closes, 21)[-1], rsi(closes, 14)[-1])
        table = table_name(symbol)
        with self.lock:
            initialize_table(self.connection, table)
            self.connection.execute(f"""
                INSERT INTO {table}
                (candle_time, open, high, low, close, volume,
                 ema10, ema20, ema30, hma21, rsi14)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(candle_time) DO UPDATE SET
                  open=excluded.open, high=excluded.high, low=excluded.low,
                  close=excluded.close, volume=excluded.volume, ema10=excluded.ema10,
                  ema20=excluded.ema20, ema30=excluded.ema30, hma21=excluded.hma21,
                  rsi14=excluded.rsi14
            """, (candle.start.isoformat(), candle.open, candle.high, candle.low,
                  candle.close, candle.volume, *values))
            self.connection.commit()
        print(f"{symbol}: closed {candle.start.isoformat()} close={candle.close}")


def clear_log_on_new_day() -> None:
    """Clear the log file only when the first entry is from a previous day."""
    if not LOG_PATH.exists() or LOG_PATH.stat().st_size == 0:
        return
    first_line = LOG_PATH.read_text(encoding="utf-8").splitlines()[0]
    try:
        log_date = dt.datetime.fromisoformat(first_line.split(" ", 1)[0]).date()
    except (IndexError, ValueError):
        return
    if log_date != dt.datetime.now(MARKET_TIMEZONE).date():
        LOG_PATH.write_text("", encoding="utf-8")


def main() -> int:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    LOG_PATH.touch(exist_ok=True)
    clear_log_on_new_day()
    try:
        token = load_token()
        if not token or not token.get("app_id") or not token.get("access_token"):
            raise FyersAuthError("No valid cached token. Run fyers_login.py first.")
        from fyers_apiv3.FyersWebsocket import data_ws

        symbols = read_symbols()
        DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
        store = CandleStore(connection)
        print(f"WebSocket agent started: {len(symbols)} symbols, "
              f"market={MARKET_OPEN}-{MARKET_CLOSE} IST")

        def on_open() -> None:
            socket.subscribe(symbols=symbols, data_type="SymbolUpdate")
            print(f"Subscribed to {len(symbols)} symbols")

        def on_message(message: dict) -> None:
            if not isinstance(message, dict):
                return
            symbol = message.get("symbol")
            price = message.get("ltp")
            timestamp = message.get("last_traded_time") or message.get("tt")
            if symbol not in symbols or price is None or timestamp is None:
                return
            tick_time = dt.datetime.fromtimestamp(int(timestamp), MARKET_TIMEZONE)
            if tick_time.weekday() >= 5 or not (MARKET_OPEN <= tick_time.time() <= MARKET_CLOSE):
                return
            store.update(symbol, int(timestamp), float(price),
                         float(message.get("vol_traded_today") or 0))

        def on_error(error) -> None:
            log_error(f"WebSocket error: {error}")

        def on_close(message) -> None:
            log_error(f"WebSocket closed: {message}")

        log_dir = STRATEGIES_DIR / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        socket = data_ws.FyersDataSocket(
            access_token=f"{token['app_id']}:{token['access_token']}",
            log_path=str(log_dir), litemode=False, write_to_file=False, reconnect=True,
            on_connect=on_open, on_message=on_message, on_error=on_error, on_close=on_close,
        )
        socket.connect()
        return 0
    except (FyersAuthError, ImportError, OSError, ValueError, sqlite3.Error) as error:
        log_error(str(error))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())