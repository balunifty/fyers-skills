#!/usr/bin/env python3
"""Read 15-min candle data from websocket database for other strategies.

This module reads cached candle data from the websocket agent's database
(NiftyFNOTop100_websocket.db) so other scripts can use pre-fetched data
without making additional API calls.

Usage:
    from utils.websocket_data_reader import WebsocketDataReader

    reader = WebsocketDataReader()
    candles_15min = reader.get_candles("NSE:RELIANCE-EQ")
    closes = reader.get_closes("NSE:RELIANCE-EQ")
"""
from __future__ import annotations

import datetime as dt
import pathlib
import re
import sqlite3
import sys
from dataclasses import dataclass
from zoneinfo import ZoneInfo

SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parents[2]
STRATEGIES_DIR = SCRIPT_DIR.parent

# =============================================================================
# Configuration
# =============================================================================
MARKET_TIMEZONE = ZoneInfo("Asia/Kolkata")
DATABASE_PATH = STRATEGIES_DIR / "databases" / "NiftyFNOTop100_websocket.db"
STOCKS_PATH = STRATEGIES_DIR / "data" / "NiftyFNOTop100.txt"


# =============================================================================
# Data Classes
# =============================================================================
@dataclass
class Candle:
    epoch: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    ema10: float | None = None
    ema20: float | None = None
    ema30: float | None = None
    hma21: float | None = None
    rsi14: float | None = None


# =============================================================================
# Websocket Data Reader
# =============================================================================
class WebsocketDataReader:
    """Read candle data from websocket agent's SQLite database."""

    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return
        self._initialized = True

    def _table_name(self, symbol: str) -> str:
        """Convert symbol to table name format."""
        safe = re.sub(r"[^A-Za-z0-9_]+", "_", symbol).strip("_")
        return f"candles_15m_{safe}"

    def get_stocks(self) -> list[str]:
        """Read stock list from NiftyFNOTop100.txt."""
        if not STOCKS_PATH.exists():
            return []
        return [
            line.strip()
            for line in STOCKS_PATH.read_text().splitlines()
            if line.strip() and not line.startswith("#")
        ]

    def get_candles(self, symbol: str, limit: int = 50) -> list[Candle]:
        """Get 15-min candles for a symbol from websocket database.

        Args:
            symbol: FYERS symbol (e.g., "NSE:RELIANCE-EQ")
            limit: Maximum number of candles to return

        Returns:
            List of Candle objects sorted by time (oldest first)
        """
        if not DATABASE_PATH.exists():
            return []

        table = self._table_name(symbol)

        try:
            with sqlite3.connect(DATABASE_PATH) as conn:
                # Check if table exists
                cursor = conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                    (table,)
                )
                if not cursor.fetchone():
                    return []

                # Fetch candles
                cursor = conn.execute(f"""
                    SELECT candle_time, open, high, low, close, volume,
                           ema10, ema20, ema30, hma21, rsi14
                    FROM {table}
                    ORDER BY candle_time DESC
                    LIMIT ?
                """, (limit,))
                rows = cursor.fetchall()

            if not rows:
                return []

            # Convert to Candle objects (oldest first)
            candles = []
            for row in reversed(rows):
                candle_time = dt.datetime.fromisoformat(row[0])
                epoch = int(candle_time.timestamp())
                candles.append(Candle(
                    epoch=epoch,
                    open=row[1],
                    high=row[2],
                    low=row[3],
                    close=row[4],
                    volume=row[5],
                    ema10=row[6],
                    ema20=row[7],
                    ema30=row[8],
                    hma21=row[9],
                    rsi14=row[10]
                ))

            return candles
        except Exception as e:
            print(f"Error reading {symbol}: {e}", file=sys.stderr)
            return []

    def get_closes(self, symbol: str, limit: int = 50) -> list[float]:
        """Get list of closing prices for indicator calculations."""
        candles = self.get_candles(symbol, limit)
        return [c.close for c in candles]

    def get_latest_close(self, symbol: str) -> float | None:
        """Get the latest closing price for a symbol."""
        candles = self.get_candles(symbol, limit=1)
        if candles:
            return candles[-1].close
        return None

    def get_latest_candle(self, symbol: str) -> Candle | None:
        """Get the most recent candle for a symbol."""
        candles = self.get_candles(symbol, limit=1)
        if candles:
            return candles[-1]
        return None

    def get_indicators(self, symbol: str) -> dict | None:
        """Get latest indicator values for a symbol."""
        candle = self.get_latest_candle(symbol)
        if candle:
            return {
                "ema10": candle.ema10,
                "ema20": candle.ema20,
                "ema30": candle.ema30,
                "hma21": candle.hma21,
                "rsi14": candle.rsi14,
            }
        return None

    def get_all_symbols(self) -> list[str]:
        """Get list of all symbols that have data in the database."""
        if not DATABASE_PATH.exists():
            return []

        try:
            with sqlite3.connect(DATABASE_PATH) as conn:
                cursor = conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'candles_15m_%'"
                )
                tables = cursor.fetchall()

            symbols = []
            for table in tables:
                # Extract symbol from table name
                symbol = table[0].replace("candles_15m_", "")
                symbols.append(symbol)

            return symbols
        except Exception:
            return []

    def get_data_age_minutes(self, symbol: str) -> float | None:
        """Get how old the data is in minutes."""
        candle = self.get_latest_candle(symbol)
        if candle:
            now = dt.datetime.now(MARKET_TIMEZONE)
            candle_time = dt.datetime.fromtimestamp(candle.epoch, MARKET_TIMEZONE)
            return (now - candle_time).total_seconds() / 60
        return None

    def is_data_fresh(self, symbol: str, max_age_minutes: int = 15) -> bool:
        """Check if data is fresh enough (within max_age_minutes)."""
        age = self.get_data_age_minutes(symbol)
        if age is None:
            return False
        return age <= max_age_minutes


# =============================================================================
# Convenience functions
# =============================================================================
def get_websocket_reader() -> WebsocketDataReader:
    """Get the websocket data reader instance."""
    return WebsocketDataReader()


def get_candles_from_websocket(symbol: str, limit: int = 50) -> list[Candle]:
    """Get 15-min candles from websocket database."""
    reader = WebsocketDataReader()
    return reader.get_candles(symbol, limit)


def get_closes_from_websocket(symbol: str, limit: int = 50) -> list[float]:
    """Get closing prices from websocket database."""
    reader = WebsocketDataReader()
    return reader.get_closes(symbol, limit)


if __name__ == "__main__":
    # Test the reader
    reader = WebsocketDataReader()
    symbols = reader.get_all_symbols()
    print(f"Found {len(symbols)} symbols in websocket database")

    for symbol in symbols[:5]:
        candles = reader.get_candles(symbol, limit=3)
        print(f"\n{symbol}: {len(candles)} candles")
        for c in candles:
            print(f"  {dt.datetime.fromtimestamp(c.epoch, MARKET_TIMEZONE)}: "
                  f"O={c.open} H={c.high} L={c.low} C={c.close} "
                  f"EMA10={c.ema10:.2f if c.ema10 else None} "
                  f"RSI={c.rsi14:.1f if c.rsi14 else None}")
