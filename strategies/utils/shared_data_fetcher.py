#!/usr/bin/env python3
"""Shared 15-min candle data fetcher for all strategies.

Fetches and caches 15-min candle data for stocks in NiftyFNOTop100.txt.
Multiple scripts can import and use this module to avoid duplicate API calls.

Supports two data sources:
1. WebSocket database (preferred - real-time data from websocketNiftyfno100.py)
2. FYERS API (fallback - historical data)

Usage:
    from utils.shared_data_fetcher import SharedDataFetcher

    fetcher = SharedDataFetcher()
    candles = fetcher.get_candles("NSE:RELIANCE-EQ")
"""
from __future__ import annotations

import datetime as dt
import pathlib
import re
import sqlite3
import sys
import time
from dataclasses import dataclass
from zoneinfo import ZoneInfo

SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parents[2]
sys.path.insert(0, str(REPO_ROOT / "skills" / "fyers-trading" / "scripts"))

from fyers_client import FyersClient  # noqa: E402

# =============================================================================
# Configuration
# =============================================================================
MARKET_TIMEZONE = ZoneInfo("Asia/Kolkata")
STRATEGIES_DIR = SCRIPT_DIR.parent
DATA_DIR = STRATEGIES_DIR / "data"

# Database paths
WEBSOCKET_DB_PATH = STRATEGIES_DIR / "databases" / "NiftyFNOTop100_websocket.db"
API_CACHE_DB_PATH = STRATEGIES_DIR / "databases" / "shared_15min_candles.db"
STOCKS_PATH = DATA_DIR / "NiftyFNOTop100.txt"

HISTORY_DAYS = 10
MAX_API_CALLS_PER_SECOND = 5


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
    # Optional indicator values (available from websocket data)
    ema10: float | None = None
    ema20: float | None = None
    ema30: float | None = None
    hma21: float | None = None
    rsi14: float | None = None


# =============================================================================
# Rate Limiter
# =============================================================================
class ApiRateLimiter:
    def __init__(self, calls_per_second: int = MAX_API_CALLS_PER_SECOND):
        self.interval = 1.0 / calls_per_second
        self.last_call = 0.0

    def call(self, function, *args, **kwargs):
        elapsed = time.monotonic() - self.last_call
        if elapsed < self.interval:
            time.sleep(self.interval - elapsed)
        self.last_call = time.monotonic()
        return function(*args, **kwargs)


# =============================================================================
# Shared Data Fetcher
# =============================================================================
class SharedDataFetcher:
    """Singleton-like fetcher that caches 15-min candles in shared SQLite DB.

    Data source priority:
    1. WebSocket database (real-time, includes indicators)
    2. API cache database (historical)
    3. FYERS API (fallback, fresh fetch)
    """

    _instance = None
    _client = None
    _limiter = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return
        self._initialized = True
        self._client = None
        self._limiter = None
        self._ensure_api_cache_database()

    def _ensure_api_cache_database(self):
        """Create API cache database and table if they don't exist."""
        API_CACHE_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(API_CACHE_DB_PATH) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS candles_15min (
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
            conn.commit()

    def _get_client(self):
        """Lazy initialize FYERS client."""
        if self._client is None:
            self._client = FyersClient()
            self._limiter = ApiRateLimiter()
        return self._client, self._limiter

    def get_stocks(self) -> list[str]:
        """Read stock list from NiftyFNOTop100.txt."""
        if not STOCKS_PATH.exists():
            return []
        return [
            line.strip()
            for line in STOCKS_PATH.read_text().splitlines()
            if line.strip() and not line.startswith("#")
        ]

    def _table_name(self, symbol: str) -> str:
        """Convert symbol to websocket table name format."""
        safe = re.sub(r"[^A-Za-z0-9_]+", "_", symbol).strip("_")
        return f"candles_15m_{safe}"

    def get_candles(self, symbol: str, force_refresh: bool = False) -> list[Candle]:
        """Get 15-min candles for a symbol.

        Tries sources in order:
        1. WebSocket database (if fresh)
        2. API cache database
        3. FYERS API

        Args:
            symbol: FYERS symbol (e.g., "NSE:RELIANCE-EQ")
            force_refresh: If True, fetch fresh data from API

        Returns:
            List of Candle objects sorted by epoch
        """
        if not force_refresh:
            # Try websocket database first (preferred)
            candles = self._get_from_websocket(symbol)
            if candles:
                return candles

            # Try API cache
            candles = self._get_from_api_cache(symbol)
            if candles:
                return candles

        # Fetch from API
        return self._fetch_from_api(symbol)

    def _get_from_websocket(self, symbol: str) -> list[Candle] | None:
        """Get candles from websocket database."""
        if not WEBSOCKET_DB_PATH.exists():
            return None

        table = self._table_name(symbol)

        try:
            with sqlite3.connect(WEBSOCKET_DB_PATH) as conn:
                # Check if table exists
                cursor = conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                    (table,)
                )
                if not cursor.fetchone():
                    return None

                # Fetch candles
                cursor = conn.execute(f"""
                    SELECT candle_time, open, high, low, close, volume,
                           ema10, ema20, ema30, hma21, rsi14
                    FROM {table}
                    ORDER BY candle_time DESC
                    LIMIT 50
                """)
                rows = cursor.fetchall()

            if not rows:
                return None

            # Check if data is fresh (within last hour)
            latest_time = dt.datetime.fromisoformat(rows[0][0])
            now = dt.datetime.now(MARKET_TIMEZONE)
            age_minutes = (now - latest_time).total_seconds() / 60
            if age_minutes > 60:
                return None

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
        except Exception:
            return None

    def _get_from_api_cache(self, symbol: str) -> list[Candle] | None:
        """Get candles from API cache database."""
        with sqlite3.connect(API_CACHE_DB_PATH) as conn:
            cursor = conn.execute(
                """SELECT epoch, open, high, low, close, volume
                   FROM candles_15min
                   WHERE symbol = ?
                   ORDER BY epoch DESC
                   LIMIT 50""",
                (symbol,)
            )
            rows = cursor.fetchall()

        if not rows:
            return None

        # Check if data is fresh (fetched within last hour)
        latest_epoch = rows[0][0]
        now = dt.datetime.now(dt.timezone.utc)
        fetched_time = dt.datetime.fromtimestamp(latest_epoch, dt.timezone.utc)
        age_minutes = (now - fetched_time).total_seconds() / 60
        if age_minutes > 60:
            return None

        return [Candle(epoch=r[0], open=r[1], high=r[2], low=r[3], close=r[4], volume=r[5])
                for r in reversed(rows)]

    def _fetch_from_api(self, symbol: str) -> list[Candle]:
        """Fetch only new candles from FYERS API (delta fetch)."""
        client, limiter = self._get_client()
        today = dt.date.today()

        # Check what we already have in cache
        last_epoch = self._get_last_cached_epoch(symbol)
        if last_epoch:
            # Start from last cached candle time
            start_dt = dt.datetime.fromtimestamp(last_epoch, MARKET_TIMEZONE).date()
        else:
            # No cache, fetch full history
            start_dt = today - dt.timedelta(days=HISTORY_DAYS)

        try:
            response = limiter.call(
                client.history,
                symbol,
                "15",
                start_dt.isoformat(),
                today.isoformat()
            )

            if response.get("s") != "ok":
                return []

            raw_candles = response.get("candles", [])
            candles = [Candle(int(row[0]), *map(float, row[1:6])) for row in raw_candles]

            # Filter: only keep candles newer than what we have
            if last_epoch:
                candles = [c for c in candles if c.epoch > last_epoch]

            # Remove last incomplete candle
            if len(candles) > 1:
                candles = candles[:-1]

            # Cache only new candles
            if candles:
                self._store_candles(symbol, candles)
                print(f"[{symbol}] Fetched {len(candles)} new candles", file=sys.stderr)

            return candles
        except Exception as e:
            print(f"Error fetching {symbol}: {e}", file=sys.stderr)
            return []

    def _get_last_cached_epoch(self, symbol: str) -> int | None:
        """Get the last cached candle epoch for a symbol."""
        with sqlite3.connect(API_CACHE_DB_PATH) as conn:
            cursor = conn.execute(
                "SELECT MAX(epoch) FROM candles_15min WHERE symbol = ?",
                (symbol,)
            )
            result = cursor.fetchone()
            return result[0] if result and result[0] else None

    def _store_candles(self, symbol: str, candles: list[Candle]):
        """Store candles in API cache database."""
        fetched_at = dt.datetime.now(dt.timezone.utc).isoformat()
        rows = []
        for candle in candles:
            candle_time = dt.datetime.fromtimestamp(candle.epoch, MARKET_TIMEZONE).isoformat()
            rows.append((symbol, candle.epoch, candle_time, candle.open, candle.high,
                        candle.low, candle.close, candle.volume, fetched_at))

        with sqlite3.connect(API_CACHE_DB_PATH) as conn:
            conn.executemany("""
                INSERT INTO candles_15min
                (symbol, epoch, candle_time, open, high, low, close, volume, fetched_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(symbol, epoch) DO UPDATE SET
                    candle_time=excluded.candle_time, open=excluded.open, high=excluded.high,
                    low=excluded.low, close=excluded.close, volume=excluded.volume,
                    fetched_at=excluded.fetched_at
            """, rows)
            conn.commit()

    def fetch_all_stocks(self, progress_callback=None) -> dict[str, list[Candle]]:
        """Fetch 15-min candles for all stocks in NiftyFNOTop100.txt.

        Args:
            progress_callback: Optional callback(current, total, symbol) for progress updates

        Returns:
            Dict mapping symbol to list of candles
        """
        stocks = self.get_stocks()
        results = {}

        for i, symbol in enumerate(stocks):
            if progress_callback:
                progress_callback(i + 1, len(stocks), symbol)
            results[symbol] = self.get_candles(symbol)

        return results

    def get_latest_close(self, symbol: str) -> float | None:
        """Get the latest closing price for a symbol."""
        candles = self.get_candles(symbol)
        if candles:
            return candles[-1].close
        return None

    def get_closes(self, symbol: str) -> list[float]:
        """Get list of closing prices for indicator calculations."""
        candles = self.get_candles(symbol)
        return [c.close for c in candles]

    def get_indicators(self, symbol: str) -> dict | None:
        """Get latest indicator values (only available from websocket data)."""
        candles = self.get_candles(symbol)
        if candles:
            latest = candles[-1]
            return {
                "ema10": latest.ema10,
                "ema20": latest.ema20,
                "ema30": latest.ema30,
                "hma21": latest.hma21,
                "rsi14": latest.rsi14,
            }
        return None


# =============================================================================
# Convenience functions
# =============================================================================
def get_shared_fetcher() -> SharedDataFetcher:
    """Get the shared data fetcher instance."""
    return SharedDataFetcher()


def fetch_all_15min_data(progress_callback=None) -> dict[str, list[Candle]]:
    """Fetch all 15-min data for stocks in NiftyFNOTop100.txt."""
    fetcher = SharedDataFetcher()
    return fetcher.fetch_all_stocks(progress_callback)


if __name__ == "__main__":
    # Test the fetcher
    def progress(current, total, symbol):
        print(f"[{current}/{total}] Fetching {symbol}...")

    results = fetch_all_15min_data(progress_callback=progress)
    print(f"\nFetched data for {len(results)} stocks")
    for symbol, candles in list(results.items())[:3]:
        print(f"  {symbol}: {len(candles)} candles")
        if candles:
            latest = candles[-1]
            print(f"    Latest: {latest.close} EMA10={latest.ema10} RSI={latest.rsi14}")

