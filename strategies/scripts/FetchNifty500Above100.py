#!/usr/bin/env python3
"""Fetch 15-minute candles for NIFTY 500 stocks trading above a price floor.

The price filter is applied from live quotes BEFORE any history is requested, so
only symbols that qualify are downloaded. For a typical NIFTY 500 that is a
few hundred history calls instead of five hundred.

  1. Universe : official NIFTY 500 constituents, cached in
                strategies/data/Nifty500.txt (refreshed from niftyindices.com,
                falling back to the cached list when offline)
  2. Filter   : last traded price > --min-price (default 100.00)
  3. Fetch    : completed 15-minute candles, most recent first

Candles are stored in SQLite under (symbol, resolution, epoch), the same layout
the dashboard uses, so a run can be pointed at either database.

Usage:
    python strategies/scripts/FetchNifty500Above100.py
    python strategies/scripts/FetchNifty500Above100.py --min-price 200 --days 20
    python strategies/scripts/FetchNifty500Above100.py --db strategies/databases/dashboard_15min_candles.db
    python strategies/scripts/FetchNifty500Above100.py --refresh-universe
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import io
import json
import pathlib
import sqlite3
import sys
import tempfile
import time
import urllib.request
from dataclasses import dataclass
from zoneinfo import ZoneInfo

SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parents[1]
STRATEGIES_DIR = SCRIPT_DIR.parent
LOG_PATH = STRATEGIES_DIR / "logs" / "FetchNifty500Above100.log"
LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
LOG_PATH.touch(exist_ok=True)
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "strategies"))
sys.path.insert(0, str(REPO_ROOT / "skills" / "fyers-trading" / "scripts"))
sys.path.insert(0, str(STRATEGIES_DIR / "config"))

from fyers_client import FyersAuthError, FyersClient  # noqa: E402

# --- Configuration ------------------------------------------------------------
STOCKS_PATH = STRATEGIES_DIR / "data" / "Nifty500.txt"
DEFAULT_DB_PATH = STRATEGIES_DIR / "databases" / "nifty500_15min.db"
NIFTY500_CSV_URL = "https://www.niftyindices.com/IndexConstituent/ind_nifty500list.csv"
EXPECTED_UNIVERSE_SIZE = 500

MARKET_TIMEZONE = ZoneInfo("Asia/Kolkata")
CANDLE_MINUTES = 15
CANDLE_SECONDS = CANDLE_MINUTES * 60
RESOLUTION = "15"
MARKET_OPEN = dt.time(9, 15)
MARKET_CLOSE = dt.time(15, 30)
MIN_PRICE = 100.0                # keep stocks priced above this
HISTORY_DAYS = 12                # calendar days of 15-minute history
QUOTE_BATCH_SIZE = 50            # FYERS caps a quotes request at 50 symbols
MAX_API_CALLS_PER_SECOND = 5
HISTORY_RETRIES = 4
RETRY_BACKOFF_SECONDS = 2.0
USER_AGENT = "Mozilla/5.0 (compatible; FyersMarketDataAgent/1.0)"
# ------------------------------------------------------------------------------


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


def log_message(message: str) -> None:
    timestamp = dt.datetime.now(MARKET_TIMEZONE).strftime("%H:%M:%S")
    line = f"{timestamp} {message}"
    print(line)
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except OSError:
        pass


def log_error(message: str) -> None:
    log_message(f"ERROR: {message}")


def market_open(now: dt.datetime | None = None) -> bool:
    """True inside the weekday 09:15-15:30 IST cash session."""
    moment = now or dt.datetime.now(MARKET_TIMEZONE)
    return MARKET_OPEN <= moment.time() <= MARKET_CLOSE and moment.weekday() < 5


def clear_log_on_new_day() -> None:
    today = dt.date.today().isoformat()
    try:
        first_line = LOG_PATH.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    if first_line and today not in first_line[0]:
        LOG_PATH.write_text(f"{today}\n", encoding="utf-8")


# =============================================================================
# Universe
# =============================================================================
def fetch_nifty500_symbols() -> list[str]:
    """Download the official NIFTY 500 constituents as FYERS symbols."""
    request = urllib.request.Request(
        NIFTY500_CSV_URL, headers={"User-Agent": USER_AGENT}
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        content = response.read().decode("utf-8-sig")

    rows = csv.DictReader(io.StringIO(content))
    if not rows.fieldnames or "Symbol" not in rows.fieldnames:
        raise ValueError(
            f"NIFTY 500 CSV has no Symbol column; columns={rows.fieldnames}"
        )

    symbols: list[str] = []
    seen: set[str] = set()
    for row in rows:
        ticker = (row.get("Symbol") or "").strip().upper()
        if not ticker or ticker in seen:
            continue
        seen.add(ticker)
        symbols.append(f"NSE:{ticker}-EQ")

    if len(symbols) < EXPECTED_UNIVERSE_SIZE:
        raise ValueError(
            f"expected about {EXPECTED_UNIVERSE_SIZE} NIFTY 500 symbols, "
            f"received {len(symbols)}"
        )
    return symbols


def write_symbols_file(symbols: list[str]) -> None:
    content = (
        "# Official NIFTY 500 constituents; refreshed by "
        "FetchNifty500Above100.py --refresh-universe\n"
    )
    content += "\n".join(symbols) + "\n"
    STOCKS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=STOCKS_PATH.parent, delete=False
    ) as temporary:
        temporary.write(content)
        temporary_path = pathlib.Path(temporary.name)
    temporary_path.replace(STOCKS_PATH)


def read_symbols_file() -> list[str]:
    if not STOCKS_PATH.exists():
        return []
    return list(dict.fromkeys(
        line.strip()
        for line in STOCKS_PATH.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ))


def load_universe(refresh: bool = False) -> tuple[list[str], str]:
    """Return (symbols, source). Refreshes from the NSE index when asked."""
    if refresh:
        symbols = fetch_nifty500_symbols()
        write_symbols_file(symbols)
        log_message(f"UNIVERSE_REFRESHED: {len(symbols)} symbols -> {STOCKS_PATH}")
        return symbols, NIFTY500_CSV_URL

    cached = read_symbols_file()
    if cached:
        return cached, str(STOCKS_PATH)

    symbols = fetch_nifty500_symbols()
    write_symbols_file(symbols)
    return symbols, NIFTY500_CSV_URL


# =============================================================================
# Price filter
# =============================================================================
def chunks(items: list[str], size: int) -> list[list[str]]:
    return [items[i:i + size] for i in range(0, len(items), size)]


def fetch_prices(
    client: FyersClient,
    limiter: ApiRateLimiter,
    symbols: list[str],
) -> dict[str, float]:
    """Last traded price per symbol, in batches the quotes API accepts."""
    prices: dict[str, float] = {}
    for batch in chunks(symbols, QUOTE_BATCH_SIZE):
        try:
            response = limiter.call(client.quotes, batch)
        except (FyersAuthError, ValueError) as error:
            log_error(f"QUOTES_FAILED {batch[0]}: {error}")
            continue
        if response.get("s") != "ok":
            log_error(f"QUOTES_REJECTED {batch[0]}: {response.get('message', response)}")
            continue
        for item in response.get("d") or []:
            if not isinstance(item, dict):
                continue
            # FYERS returns the symbol under "n"; "s" is the per-item status.
            values = item.get("v") or {}
            symbol = item.get("n") or values.get("symbol")
            try:
                price = float(values.get("lp") or 0)
            except (TypeError, ValueError):
                continue
            if symbol and price > 0:
                prices[symbol] = price
    return prices


def filter_by_price(
    symbols: list[str],
    prices: dict[str, float],
    min_price: float,
) -> tuple[list[str], list[tuple[str, float]]]:
    """Split into (kept, rejected-with-reason) on a strict > min_price test."""
    kept: list[str] = []
    rejected: list[tuple[str, float]] = []
    for symbol in symbols:
        price = prices.get(symbol)
        if price is None:
            continue
        if price > min_price:
            kept.append(symbol)
        else:
            rejected.append((symbol, price))
    return kept, rejected


# =============================================================================
# Candle fetching
# =============================================================================
def parse_candles(response: dict) -> list[Candle]:
    candles: list[Candle] = []
    for row in response.get("candles") or []:
        if not isinstance(row, (list, tuple)) or len(row) < 6:
            continue
        try:
            candles.append(Candle(
                epoch=int(row[0]), open=float(row[1]), high=float(row[2]),
                low=float(row[3]), close=float(row[4]), volume=float(row[5]),
            ))
        except (TypeError, ValueError):
            continue
    return sorted(candles, key=lambda c: c.epoch)


def fetch_candles(
    client: FyersClient,
    limiter: ApiRateLimiter,
    symbol: str,
    days: int,
    current: dt.datetime,
) -> list[Candle]:
    """Completed 15-minute bars only; a bar closing exactly now counts."""
    start = (current.date() - dt.timedelta(days=days)).isoformat()
    end = current.date().isoformat()
    for attempt in range(HISTORY_RETRIES):
        try:
            response = limiter.call(client.history, symbol, RESOLUTION, start, end)
        except Exception as error:
            if "429" not in str(error) or attempt == HISTORY_RETRIES - 1:
                raise
            time.sleep(RETRY_BACKOFF_SECONDS * (attempt + 1))
            continue
        if response.get("s") != "ok":
            raise RuntimeError(f"history fetch failed for {symbol}: {response}")

        now_epoch = int(current.timestamp())
        return [
            candle for candle in parse_candles(response)
            if candle.epoch + CANDLE_SECONDS <= now_epoch
        ]
    raise RuntimeError(f"history fetch exhausted retries for {symbol}")


# =============================================================================
# Storage
# =============================================================================
class CandleStore:
    """SQLite store keyed on (symbol, resolution, epoch), matching the dashboard."""

    def __init__(self, path: pathlib.Path):
        self.path = pathlib.Path(path)
        self._connection: sqlite3.Connection | None = None
        self.written = 0

    def connect(self) -> sqlite3.Connection:
        if self._connection is not None:
            return self._connection
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=30)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
        connection.execute("""
            CREATE TABLE IF NOT EXISTS candles (
                symbol      TEXT    NOT NULL,
                resolution  TEXT    NOT NULL,
                epoch       INTEGER NOT NULL,
                open        REAL,
                high        REAL,
                low         REAL,
                close       REAL,
                volume      REAL,
                stored_at   TEXT,
                PRIMARY KEY (symbol, resolution, epoch)
            )
        """)
        connection.execute("""
            CREATE INDEX IF NOT EXISTS idx_candles_symbol_resolution
            ON candles (symbol, resolution, epoch DESC)
        """)
        connection.commit()
        self._connection = connection
        return connection

    def close(self) -> None:
        if self._connection is not None:
            try:
                self._connection.close()
            except sqlite3.Error:
                pass
            self._connection = None

    def store(self, symbol: str, candles: list[Candle]) -> int:
        if not candles:
            return 0
        connection = self.connect()
        stored_at = dt.datetime.now(MARKET_TIMEZONE).isoformat(timespec="seconds")
        connection.executemany(
            "INSERT OR REPLACE INTO candles "
            "(symbol, resolution, epoch, open, high, low, close, volume, stored_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (symbol, RESOLUTION, c.epoch, c.open, c.high, c.low, c.close,
                 c.volume, stored_at)
                for c in candles
            ],
        )
        connection.commit()
        self.written += len(candles)
        return len(candles)

    def summary(self) -> dict:
        connection = self.connect()
        total, symbols = connection.execute(
            "SELECT COUNT(*), COUNT(DISTINCT symbol) FROM candles "
            "WHERE resolution = ?", (RESOLUTION,)
        ).fetchone()
        span = connection.execute(
            "SELECT MIN(epoch), MAX(epoch) FROM candles WHERE resolution = ?",
            (RESOLUTION,)
        ).fetchone()

        def stamp(epoch: int | None) -> str | None:
            if not epoch:
                return None
            return dt.datetime.fromtimestamp(
                epoch, MARKET_TIMEZONE).strftime("%Y-%m-%d %H:%M")

        return {
            "total_rows": int(total or 0),
            "symbols": int(symbols or 0),
            "oldest_ist": stamp(span[0]),
            "newest_ist": stamp(span[1]),
        }


# =============================================================================
# Main
# =============================================================================
def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Fetch 15-minute candles for NIFTY 500 stocks above a price floor"
    )
    parser.add_argument(
        "--min-price", type=float, default=MIN_PRICE,
        help=f"keep only stocks priced above this (default {MIN_PRICE})")
    parser.add_argument(
        "--days", type=int, default=HISTORY_DAYS,
        help=f"calendar days of 15-minute history (default {HISTORY_DAYS})")
    parser.add_argument(
        "--db", type=pathlib.Path, default=DEFAULT_DB_PATH,
        help="destination SQLite file")
    parser.add_argument(
        "--refresh-universe", action="store_true",
        help="re-download the NIFTY 500 constituent list before running")
    parser.add_argument(
        "--report", type=pathlib.Path,
        help="write a JSON summary of the run to this path")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.days < 1:
        print("ERROR: --days must be at least 1")
        return 2

    clear_log_on_new_day()
    current = dt.datetime.now(MARKET_TIMEZONE)
    log_message(
        f"STARTED: min_price={args.min_price} days={args.days} "
        f"resolution={CANDLE_MINUTES}m db={args.db.name} "
        f"market={'open' if market_open() else 'closed'}"
    )

    try:
        symbols, source = load_universe(refresh=args.refresh_universe)
    except (OSError, ValueError) as error:
        log_error(f"UNIVERSE_FAILED: {error}")
        return 1
    if not symbols:
        log_error(f"no symbols available (looked in {STOCKS_PATH})")
        return 1
    log_message(f"UNIVERSE: {len(symbols)} NIFTY 500 symbols from {source}")

    try:
        client = FyersClient()
    except Exception as error:
        log_error(f"CLIENT_INIT_FAILED: {error}")
        return 1

    limiter = ApiRateLimiter()
    store = CandleStore(args.db)
    try:
        # 1. price first, so history is only pulled for qualifying symbols
        prices = fetch_prices(client, limiter, symbols)
        log_message(f"PRICES: resolved {len(prices)}/{len(symbols)} last traded prices")
        if not prices:
            log_error("no prices resolved; is the FYERS session valid?")
            return 1

        kept, rejected = filter_by_price(symbols, prices, args.min_price)
        log_message(
            f"FILTER: {len(kept)} symbols above {args.min_price}, "
            f"{len(rejected)} at or below, {len(symbols) - len(prices)} unpriced"
        )
        if rejected:
            sample = ", ".join(f"{s}={p:.2f}" for s, p in sorted(rejected, key=lambda x: x[1])[:5])
            log_message(f"FILTER_SAMPLE_REJECTED: {sample}")

        # 2. history for the survivors only
        written = 0
        empty: list[str] = []
        failed: list[str] = []
        for index, symbol in enumerate(kept, start=1):
            try:
                candles = fetch_candles(client, limiter, symbol, args.days, current)
            except FyersAuthError as error:
                log_error(f"AUTH_ERROR: {error}")
                return 1
            except Exception as error:
                failed.append(symbol)
                log_error(f"FETCH_FAILED {symbol}: {error}")
                continue
            if not candles:
                empty.append(symbol)
                continue
            written += store.store(symbol, candles)
            if index % 50 == 0:
                log_message(f"PROGRESS: {index}/{len(kept)} symbols, {written} bars")

        summary = store.summary()
        log_message(
            f"DONE: wrote {written} bars for {len(kept) - len(empty) - len(failed)} symbols "
            f"(empty={len(empty)} failed={len(failed)})"
        )
        log_message(
            f"STORE: {summary['total_rows']} rows across {summary['symbols']} symbols, "
            f"{summary['oldest_ist']} -> {summary['newest_ist']} IST"
        )

        if args.report:
            report = {
                "generated_at_ist": current.isoformat(timespec="seconds"),
                "market_open": market_open(),
                "universe_source": source,
                "universe_size": len(symbols),
                "min_price": args.min_price,
                "history_days": args.days,
                "candle_minutes": CANDLE_MINUTES,
                "prices_resolved": len(prices),
                "symbols_kept": len(kept),
                "symbols_rejected": len(rejected),
                "symbols_unpriced": len(symbols) - len(prices),
                "symbols_empty": empty,
                "symbols_failed": failed,
                "bars_written": written,
                "database": str(args.db),
                "store": summary,
                "rejected_sample": [
                    {"symbol": s, "price": p} for s, p in
                    sorted(rejected, key=lambda x: x[1])[:20]
                ],
            }
            args.report.parent.mkdir(parents=True, exist_ok=True)
            args.report.write_text(
                json.dumps(report, indent=2) + "\n", encoding="utf-8")
            log_message(f"REPORT: {args.report}")

        print()
        print(f"  universe        : {len(symbols)} NIFTY 500 symbols")
        print(f"  price filter    : > {args.min_price}  ->  {len(kept)} kept, "
              f"{len(rejected)} rejected")
        print(f"  bars written    : {written:,}")
        print(f"  database        : {args.db}")
        print(f"  store now holds : {summary['total_rows']:,} rows / "
              f"{summary['symbols']} symbols")
        print(f"  15m range       : {summary['oldest_ist']} -> {summary['newest_ist']} IST")
        return 0

    except KeyboardInterrupt:
        log_message("STOPPED: interrupted by user")
        return 0
    except FyersAuthError as error:
        log_error(f"AUTH_ERROR: {error}")
        return 1
    except Exception as error:
        log_error(f"FATAL: {error}")
        return 1
    finally:
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())
