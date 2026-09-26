#!/usr/bin/env python3
"""Buy NIFTY 50 equities using a 15-minute daily-breakout/volume strategy.

A BUY signal requires all of the following on the latest completed 15-minute
candle:

  1. The candle close OR candle open is above the previous trading day's close.
  2. RSI(14) on the 15-minute closes is above 55. A fresh cross above 55 is
     recorded as an additional detail, but is not required by the strategy.
  3. The average volume of the last 20 completed daily candles is above
     10,000,000 shares.
  4. The current 15-minute close is above 100.

This strategy same as R1 breakout or PDH Breakout .
The script is dry-run by default. Use --live only after reviewing the equity
configuration. Matched signals are printed, persisted once in SQLite, and
upserted into one dedicated Excel workbook.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import importlib.util
import json
import os
import pathlib
import sqlite3
import sys
import tempfile
import time
from contextlib import closing, contextmanager
from dataclasses import dataclass
from zoneinfo import ZoneInfo

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Font, PatternFill

SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parents[1]
STRATEGIES_DIR = SCRIPT_DIR.parent

STOCKS_PATH = STRATEGIES_DIR / "data" / "Nifty50.txt"
DATABASE_PATH = STRATEGIES_DIR / "databases" / "equity_daily_breakout_rsi_volume_15min.db"
LOG_PATH = STRATEGIES_DIR / "logs" / "EquityDailyBreakoutRsiVolume15min.log"
EXCEL_PATH = STRATEGIES_DIR / "logs" / "EquityDailyBreakoutRsiVolume15min.xlsx"

STRATEGY_NAME = "EQUITY_DAILY_BREAKOUT_RSI_VOLUME_15MIN"
SCRIPT_NAME = "EquityDailyBreakoutRsiVolume15min.py"

MARKET_TIMEZONE = ZoneInfo("Asia/Kolkata")
MARKET_OPEN = dt.time(9, 15)
MARKET_CLOSE = dt.time(15, 30)
LAST_INTRADAY_ENTRY = dt.time(15, 20)
CANDLE_SECONDS = 15 * 60
DEFAULT_POLL_SECONDS = 5 * 60
INTRADAY_HISTORY_DAYS = 30
DAILY_HISTORY_DAYS = 60
DAILY_VOLUME_LOOKBACK = 20
MIN_AVERAGE_DAILY_VOLUME = 10_000_000.0
MIN_PRICE = 100.0
RSI_PERIOD = 14
RSI_THRESHOLD = 55.0
MAX_API_CALLS_PER_SECOND = 8

ORDER_TYPE = 2  # FYERS: 2 = MARKET
ORDER_SIDE = 1  # FYERS: 1 = BUY
PRODUCT_TYPE = "INTRADAY"
ORDER_TAG_PREFIX = "edb15"

sys.path.insert(0, str(REPO_ROOT / "skills" / "fyers-trading" / "scripts"))
sys.path.insert(0, str(STRATEGIES_DIR / "config"))
sys.path.insert(0, str(STRATEGIES_DIR / "utils"))

_indicator_spec = importlib.util.spec_from_file_location(
    "daily_breakout_common_indicators",
    STRATEGIES_DIR / "utils" / "common_indicators.py",
)
assert _indicator_spec and _indicator_spec.loader
_indicator_module = importlib.util.module_from_spec(_indicator_spec)
_indicator_spec.loader.exec_module(_indicator_module)
rsi = _indicator_module.rsi

from fyers_client import AmbiguousOrderError, FyersAuthError  # noqa: E402
from order_config import (  # noqa: E402
    ConfigGatedFyersClient,
    DailyStockLimitError,
    OrderPlacementDisabledError,
    get_entry_qty,
)


@dataclass(frozen=True)
class Candle:
    """One FYERS OHLCV candle; epoch is the candle start time in seconds."""

    epoch: int
    open: float
    high: float
    low: float
    close: float
    volume: float


class ApiRateLimiter:
    """Space history requests below the configured requests-per-second cap."""

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


@contextmanager
def exclusive_file_lock(path: pathlib.Path):
    """Serialize workbook updates across simultaneous scanner processes."""
    lock_path = path.with_suffix(path.suffix + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as lock_file:
        lock_file.seek(0, os.SEEK_END)
        if lock_file.tell() == 0:
            lock_file.write(b"\0")
            lock_file.flush()
        lock_file.seek(0)
        if os.name == "nt":
            import msvcrt

            while True:
                try:
                    msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    time.sleep(0.1)
        else:
            import fcntl

            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            lock_file.seek(0)
            if os.name == "nt":
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def order_tag_for_signal(signal_id: str) -> str:
    digest = hashlib.sha256(signal_id.encode("utf-8")).hexdigest()[:20]
    return f"{ORDER_TAG_PREFIX}{digest}"


def signal_lock_path(signal_id: str, directory: pathlib.Path) -> pathlib.Path:
    digest = hashlib.sha256(signal_id.encode("utf-8")).hexdigest()[:24]
    return directory / f"equity_daily_breakout_signal_{digest}"


class SignalStore:
    """Persistent one-row-per-signal ledger for retries and Excel writes."""

    def __init__(self, path: pathlib.Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute("""
                CREATE TABLE IF NOT EXISTS matched_signals (
                    signal_id TEXT PRIMARY KEY,
                    symbol TEXT NOT NULL,
                    candle_epoch INTEGER NOT NULL,
                    matched_at_ist TEXT NOT NULL,
                    details_json TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    quantity INTEGER NOT NULL,
                    order_tag TEXT NOT NULL,
                    order_status TEXT NOT NULL DEFAULT 'MATCHED',
                    order_id TEXT NOT NULL DEFAULT '',
                    broker_message TEXT NOT NULL DEFAULT '',
                    match_printed INTEGER NOT NULL DEFAULT 0,
                    excel_logged INTEGER NOT NULL DEFAULT 0,
                    excel_revision INTEGER NOT NULL DEFAULT 0,
                    UNIQUE (symbol, candle_epoch)
                )
            """)
            columns = {
                row[1] for row in connection.execute("PRAGMA table_info(matched_signals)")
            }
            if "excel_revision" not in columns:
                connection.execute(
                    "ALTER TABLE matched_signals ADD COLUMN excel_revision INTEGER NOT NULL DEFAULT 0"
                )
            connection.commit()

    def claim(
        self,
        symbol: str,
        candle_epoch: int,
        matched_at_ist: str,
        details: dict,
        mode: str,
        quantity: int,
    ) -> str | None:
        signal_id = f"{STRATEGY_NAME}:{symbol}:{candle_epoch}"
        with closing(sqlite3.connect(self.path)) as connection, connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO matched_signals
                    (signal_id, symbol, candle_epoch, matched_at_ist,
                     details_json, mode, quantity, order_tag)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    signal_id,
                    symbol,
                    candle_epoch,
                    matched_at_ist,
                    json.dumps(details, sort_keys=True),
                    mode,
                    quantity,
                    order_tag_for_signal(signal_id),
                ),
            )
            return signal_id if cursor.rowcount == 1 else None

    def mark_printed(self, signal_id: str) -> None:
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute(
                "UPDATE matched_signals SET match_printed = 1 WHERE signal_id = ?",
                (signal_id,),
            )
            connection.commit()

    def mark_submitting(self, signal_id: str) -> bool:
        with closing(sqlite3.connect(self.path)) as connection, connection:
            cursor = connection.execute(
                """
                UPDATE matched_signals
                SET order_status = 'SUBMITTING', excel_revision = excel_revision + 1
                WHERE signal_id = ? AND order_status = 'MATCHED'
                """,
                (signal_id,),
            )
            return cursor.rowcount == 1

    def finish_order(
        self,
        signal_id: str,
        status: str,
        order_id: str,
        message: str,
    ) -> None:
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute(
                """
                UPDATE matched_signals
                SET order_status = ?, order_id = ?, broker_message = ?,
                    excel_logged = 0, excel_revision = excel_revision + 1
                WHERE signal_id = ?
                """,
                (status, order_id, message, signal_id),
            )
            connection.commit()

    def unfinished_rows(self) -> list[sqlite3.Row]:
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.row_factory = sqlite3.Row
            return connection.execute(
                """
                SELECT * FROM matched_signals
                WHERE order_status IN ('MATCHED', 'SUBMITTING', 'UNKNOWN', 'PENDING_ACK')
                ORDER BY candle_epoch, symbol
                """
            ).fetchall()

    def pending_excel_rows(self) -> list[sqlite3.Row]:
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.row_factory = sqlite3.Row
            return connection.execute(
                """
                SELECT * FROM matched_signals
                WHERE excel_logged = 0
                ORDER BY candle_epoch, symbol
                """
            ).fetchall()

    def get_row(self, signal_id: str) -> sqlite3.Row:
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute(
                "SELECT * FROM matched_signals WHERE signal_id = ?",
                (signal_id,),
            ).fetchone()
        if row is None:
            raise RuntimeError(f"signal row not found: {signal_id}")
        return row

    def mark_excel_logged(self, signal_id: str, revision: int) -> bool:
        with closing(sqlite3.connect(self.path)) as connection, connection:
            cursor = connection.execute(
                """
                UPDATE matched_signals SET excel_logged = 1
                WHERE signal_id = ? AND excel_revision = ?
                """,
                (signal_id, revision),
            )
            return cursor.rowcount == 1


EXCEL_HEADERS = [
    "SignalID",
    "MatchedAtIST",
    "SignalCandleStartIST",
    "SignalCandleEndIST",
    "Symbol",
    "Timeframe",
    "Open",
    "High",
    "Low",
    "Close",
    "Previous15mClose",
    "RSI14",
    "PreviousRSI14",
    "RSICrossedAbove55",
    "PreviousDayDate",
    "PreviousDayOpen",
    "PreviousDayHigh",
    "PreviousDayLow",
    "PreviousDayClose",
    "AverageDailyVolume",
    "VolumeLookbackDays",
    "Mode",
    "Quantity",
    "OrderType",
    "ProductType",
    "OrderStatus",
    "OrderID",
    "OrderTag",
    "BrokerMessage",
    "Details",
]
EXCEL_WIDTHS = {
    "A": 65, "B": 27, "C": 27, "D": 27, "E": 24, "F": 12,
    "G": 13, "H": 13, "I": 13, "J": 13, "K": 18, "L": 12,
    "M": 15, "N": 20, "O": 18, "P": 17, "Q": 17, "R": 17,
    "S": 18, "T": 22, "U": 18, "V": 12, "W": 12, "X": 12,
    "Y": 15, "Z": 16, "AA": 25, "AB": 28, "AC": 45, "AD": 90,
}


def now_ist() -> dt.datetime:
    return dt.datetime.now(MARKET_TIMEZONE)


def log_message(message: str, error: bool = False) -> None:
    entry = f"{now_ist().isoformat(timespec='seconds')} {message}"
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOG_PATH.open("a", encoding="utf-8") as log_file:
        log_file.write(entry + "\n")
    print(entry, file=sys.stderr if error else sys.stdout)


def log_contains_signal(signal_id: str) -> bool:
    try:
        return f"signal_id={signal_id}" in LOG_PATH.read_text(encoding="utf-8")
    except OSError:
        return False


def read_symbols() -> list[str]:
    return list(dict.fromkeys(
        line.strip()
        for line in STOCKS_PATH.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ))


def completed_intraday_candles(
    response: dict,
    current_time: dt.datetime | None = None,
) -> list[Candle]:
    current = current_time or now_ist()
    if current.tzinfo is None:
        current = current.replace(tzinfo=MARKET_TIMEZONE)
    current_epoch = int(current.timestamp())
    by_epoch: dict[int, Candle] = {}
    for row in response.get("candles") or []:
        if not isinstance(row, (list, tuple)) or len(row) < 6:
            continue
        try:
            candle = Candle(int(row[0]), *map(float, row[1:6]))
        except (TypeError, ValueError):
            continue
        if candle.epoch + CANDLE_SECONDS <= current_epoch:
            by_epoch[candle.epoch] = candle
    return [by_epoch[epoch] for epoch in sorted(by_epoch)]


def completed_daily_candles(
    response: dict,
    current_time: dt.datetime | None = None,
) -> list[Candle]:
    """Return completed prior trading days, excluding today's partial daily bar."""
    current = current_time or now_ist()
    if current.tzinfo is None:
        current = current.replace(tzinfo=MARKET_TIMEZONE)
    by_epoch: dict[int, Candle] = {}
    for row in response.get("candles") or []:
        if not isinstance(row, (list, tuple)) or len(row) < 6:
            continue
        try:
            candle = Candle(int(row[0]), *map(float, row[1:6]))
        except (TypeError, ValueError):
            continue
        candle_date = dt.datetime.fromtimestamp(candle.epoch, MARKET_TIMEZONE).date()
        if candle_date < current.date():
            by_epoch[candle.epoch] = candle
    return [by_epoch[epoch] for epoch in sorted(by_epoch)]


def fetch_candles(
    client,
    symbol: str,
    resolution: str,
    days: int,
    limiter: ApiRateLimiter,
    current_time: dt.datetime,
    daily: bool = False,
) -> list[Candle]:
    start_date = current_time.date() - dt.timedelta(days=days)
    response = limiter.call(
        client.history,
        symbol,
        resolution,
        start_date.isoformat(),
        current_time.date().isoformat(),
    )
    if response.get("s") != "ok":
        raise RuntimeError(f"history fetch failed for {symbol}: {response}")
    if daily:
        return completed_daily_candles(response, current_time)
    return completed_intraday_candles(response, current_time)


def evaluate_strategy(
    intraday: list[Candle],
    daily: list[Candle],
) -> tuple[bool, dict]:
    if len(intraday) < RSI_PERIOD + 1:
        return False, {"reason": f"need at least {RSI_PERIOD + 1} 15-minute candles"}
    if len(daily) < DAILY_VOLUME_LOOKBACK:
        return False, {
            "reason": f"need at least {DAILY_VOLUME_LOOKBACK} completed daily candles"
        }

    previous = intraday[-2]
    current = intraday[-1]
    closes = [candle.close for candle in intraday]
    rsi_values = rsi(closes, RSI_PERIOD)
    previous_rsi = rsi_values[-2]
    current_rsi = rsi_values[-1]
    if previous_rsi is None or current_rsi is None:
        return False, {"reason": "insufficient RSI history"}

    previous_day = daily[-1]
    volume_window = daily[-DAILY_VOLUME_LOOKBACK:]
    average_daily_volume = sum(candle.volume for candle in volume_window) / len(volume_window)
    price_above_previous_close = (
        current.close > previous_day.close or current.open > previous_day.close
    )
    conditions = {
        "close_or_open_above_previous_day_close": price_above_previous_close,
        "rsi_above_55": current_rsi > RSI_THRESHOLD,
        "average_daily_volume_above_10_million": (
            average_daily_volume > MIN_AVERAGE_DAILY_VOLUME
        ),
        "price_above_100": current.close > MIN_PRICE,
    }
    details = {
        "candle_epoch": current.epoch,
        "open": current.open,
        "high": current.high,
        "low": current.low,
        "close": current.close,
        "previous_15m_close": previous.close,
        "rsi14": current_rsi,
        "previous_rsi14": previous_rsi,
        "rsi_crossed_above_55": previous_rsi <= RSI_THRESHOLD < current_rsi,
        "previous_day_date": dt.datetime.fromtimestamp(
            previous_day.epoch, MARKET_TIMEZONE
        ).date().isoformat(),
        "previous_day_open": previous_day.open,
        "previous_day_high": previous_day.high,
        "previous_day_low": previous_day.low,
        "previous_day_close": previous_day.close,
        "average_daily_volume": average_daily_volume,
        "volume_lookback_days": DAILY_VOLUME_LOOKBACK,
        "conditions": conditions,
    }
    triggered = all(conditions.values())
    if not triggered:
        details["failed_conditions"] = [
            name for name, passed in conditions.items() if not passed
        ]
    return triggered, details


def create_excel_workbook() -> Workbook:
    workbook = Workbook()
    worksheet = workbook.active
    worksheet.title = "DailyBreakoutSignals"
    fill = PatternFill(start_color="4472C4", end_color="4472C4", fill_type="solid")
    font = Font(color="FFFFFF", bold=True)
    for column, header in enumerate(EXCEL_HEADERS, 1):
        cell = worksheet.cell(1, column, header)
        cell.fill = fill
        cell.font = font
        cell.alignment = Alignment(horizontal="center")
    for column, width in EXCEL_WIDTHS.items():
        worksheet.column_dimensions[column].width = width
    worksheet.freeze_panes = "A2"
    return workbook


def excel_values(row: sqlite3.Row) -> list:
    details = json.loads(row["details_json"])
    return [
        row["signal_id"],
        row["matched_at_ist"],
        dt.datetime.fromtimestamp(row["candle_epoch"], MARKET_TIMEZONE).isoformat(timespec="seconds"),
        dt.datetime.fromtimestamp(row["candle_epoch"] + CANDLE_SECONDS, MARKET_TIMEZONE).isoformat(timespec="seconds"),
        row["symbol"],
        "15m",
        details["open"],
        details["high"],
        details["low"],
        details["close"],
        details["previous_15m_close"],
        details["rsi14"],
        details["previous_rsi14"],
        details["rsi_crossed_above_55"],
        details["previous_day_date"],
        details["previous_day_open"],
        details["previous_day_high"],
        details["previous_day_low"],
        details["previous_day_close"],
        details["average_daily_volume"],
        details["volume_lookback_days"],
        row["mode"],
        row["quantity"],
        "MARKET",
        PRODUCT_TYPE,
        row["order_status"],
        row["order_id"],
        row["order_tag"],
        row["broker_message"],
        row["details_json"],
    ]


def upsert_excel(path: pathlib.Path, row: sqlite3.Row) -> bool:
    """Upsert one SignalID under a cross-process lock."""
    with exclusive_file_lock(path):
        workbook = None
        temporary_path: pathlib.Path | None = None
        try:
            if path.exists():
                workbook = load_workbook(path)
                worksheet = workbook.active
                headers = [worksheet.cell(1, col).value for col in range(1, len(EXCEL_HEADERS) + 1)]
                if headers != EXCEL_HEADERS:
                    raise RuntimeError(f"unexpected Excel header in {path}")
            else:
                workbook = create_excel_workbook()
                worksheet = workbook.active

            output_row = None
            for row_number in range(2, worksheet.max_row + 1):
                if worksheet.cell(row_number, 1).value == row["signal_id"]:
                    output_row = row_number
                    break
            if output_row is None:
                output_row = worksheet.max_row + 1
            for column, value in enumerate(excel_values(row), 1):
                cell = worksheet.cell(output_row, column, value)
                cell.alignment = Alignment(vertical="top", wrap_text=column == len(EXCEL_HEADERS))

            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{path.stem}.", suffix=".tmp.xlsx", dir=path.parent
            )
            os.close(descriptor)
            temporary_path = pathlib.Path(temporary_name)
            workbook.save(temporary_path)
            workbook.close()
            workbook = None
            os.replace(temporary_path, path)
            temporary_path = None
            return True
        finally:
            if workbook is not None:
                workbook.close()
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)


def flush_excel(store: SignalStore, path: pathlib.Path) -> None:
    for row in store.pending_excel_rows():
        try:
            upsert_excel(path, row)
            store.mark_excel_logged(row["signal_id"], row["excel_revision"])
        except Exception as error:
            log_message(f"EXCEL DEFERRED | {row['signal_id']} | {error}", error=True)
            return


def order_result(response: dict) -> tuple[str, str, str]:
    status = str(response.get("s", "")).lower()
    order_id = str(response.get("id") or response.get("id_fyers") or "")
    message = str(response.get("message", ""))
    if status == "dry_run":
        return "DRY_RUN", order_id or "DRY_RUN", message
    if status == "ok" and str(response.get("code")) == "201":
        return "PENDING_ACK", order_id or "PENDING_ACK", message
    if status == "ok":
        return "PLACED", order_id or "NOT_RETURNED", message
    return "REJECTED", order_id or "NOT_RETURNED", message


def _walk_order_items(value):
    if isinstance(value, list):
        for item in value:
            yield from _walk_order_items(item)
    elif isinstance(value, dict):
        if value.get("orderTag") or value.get("order_tag"):
            yield value
        for key in ("d", "orderBook", "orderbook", "orders"):
            if key in value:
                yield from _walk_order_items(value[key])


def reconcile_order(client, store: SignalStore, row: sqlite3.Row) -> bool:
    """Resolve an ambiguous/pending send by deterministic orderTag."""
    try:
        response = client.orderbook()
    except FyersAuthError:
        raise
    except Exception as error:
        log_message(f"RECONCILE FAILED | {row['signal_id']} | {error}", error=True)
        return False
    item = next(
        (
            item for item in _walk_order_items(response)
            if str(item.get("orderTag") or item.get("order_tag")) == row["order_tag"]
        ),
        None,
    )
    if item is None:
        return False
    order_id = str(item.get("id") or item.get("orderId") or "FOUND_WITHOUT_ID")
    broker_status = item.get("orderStatus") or item.get("status") or "UNKNOWN"
    store.finish_order(
        row["signal_id"],
        "RECONCILED",
        order_id,
        f"orderbook tag match; broker_status={broker_status}",
    )
    return True


def place_buy_order(client, row: sqlite3.Row) -> dict:
    live = row["mode"] == "LIVE"
    order = {
        "symbol": row["symbol"],
        "qty": row["quantity"],
        "type": ORDER_TYPE,
        "side": ORDER_SIDE,
        "productType": PRODUCT_TYPE,
        "orderTag": row["order_tag"],
    }
    return client.place_order(
        order,
        dry_run=not live,
        meta={"strategy": STRATEGY_NAME, "signal": "ENTRY", "timeframe": "15m"},
        retry_transient=False,
    )


def print_match(symbol: str, row: sqlite3.Row, details: dict) -> None:
    log_message(
        f"MATCH | signal_id={row['signal_id']} | symbol={symbol} | timeframe=15m | "
        f"candle={dt.datetime.fromtimestamp(row['candle_epoch'], MARKET_TIMEZONE).isoformat(timespec='seconds')} | "
        f"O={details['open']:.2f} H={details['high']:.2f} L={details['low']:.2f} C={details['close']:.2f} | "
        f"RSI14={details['rsi14']:.2f} previous_day_close={details['previous_day_close']:.2f} | "
        f"average_daily_volume={details['average_daily_volume']:.0f} | "
        f"conditions={json.dumps(details['conditions'], sort_keys=True)}"
    )


def process_claimed_row(
    client,
    store: SignalStore,
    row: sqlite3.Row,
    allow_live_send: bool = True,
) -> None:
    signal_id = row["signal_id"]
    details = json.loads(row["details_json"])
    if not row["match_printed"]:
        if not log_contains_signal(signal_id):
            print_match(row["symbol"], row, details)
        store.mark_printed(signal_id)
        row = store.get_row(signal_id)

    current = now_ist()
    if row["mode"] == "LIVE" and not allow_live_send:
        return
    if row["mode"] == "LIVE":
        candle_date = dt.datetime.fromtimestamp(row["candle_epoch"], MARKET_TIMEZONE).date()
        candle_end = row["candle_epoch"] + CANDLE_SECONDS
        if candle_date != current.date() or not (
            0 <= current.timestamp() - candle_end < CANDLE_SECONDS
        ):
            store.finish_order(
                signal_id,
                "SKIPPED_STALE_RECOVERY",
                "NOT_SENT",
                "signal candle is no longer the current completed 15-minute bar",
            )
            return
        if not (current.weekday() < 5 and MARKET_OPEN <= current.time() <= LAST_INTRADAY_ENTRY):
            store.finish_order(
                signal_id,
                "SKIPPED_LATE_ENTRY",
                "NOT_SENT",
                "outside live equity entry window",
            )
            return

    if not store.mark_submitting(signal_id):
        return
    row = store.get_row(signal_id)
    try:
        response = place_buy_order(client, row)
        status, order_id, message = order_result(response)
        store.finish_order(signal_id, status, order_id, message)
        if status == "PENDING_ACK":
            reconcile_order(client, store, store.get_row(signal_id))
    except AmbiguousOrderError as error:
        store.finish_order(
            signal_id,
            "UNKNOWN",
            "RECONCILE_REQUIRED",
            f"ambiguous order transmission; not retried: {error}",
        )
        reconcile_order(client, store, store.get_row(signal_id))
    except FyersAuthError as error:
        store.finish_order(signal_id, "FAILED_NOT_SENT", "NOT_SENT", str(error))
        raise
    except (OrderPlacementDisabledError, DailyStockLimitError, ValueError) as error:
        store.finish_order(signal_id, "NOT_SENT", "NOT_SENT", str(error))
    except RuntimeError as error:
        store.finish_order(signal_id, "REJECTED_NOT_SENT", "NOT_SENT", str(error))
    except Exception as error:
        store.finish_order(
            signal_id,
            "UNKNOWN",
            "RECONCILE_REQUIRED",
            f"unexpected order error; not retried: {error}",
        )
        reconcile_order(client, store, store.get_row(signal_id))

    final_row = store.get_row(signal_id)
    log_message(
        f"ORDER | {final_row['symbol']} | mode={final_row['mode']} | "
        f"status={final_row['order_status']} | order_id={final_row['order_id']} | "
        f"message={final_row['broker_message']}"
    )


def recover_pending_signals(
    client,
    store: SignalStore,
    allow_live_send: bool = True,
) -> None:
    for snapshot in store.unfinished_rows():
        with exclusive_file_lock(signal_lock_path(snapshot["signal_id"], store.path.parent)):
            row = store.get_row(snapshot["signal_id"])
            if row["order_status"] == "MATCHED":
                process_claimed_row(client, store, row, allow_live_send)
            elif row["order_status"] == "SUBMITTING" and row["mode"] == "DRY_RUN":
                store.finish_order(
                    row["signal_id"],
                    "DRY_RUN_INCOMPLETE",
                    "NOT_SENT",
                    "dry-run stopped before completion; no live order was sent",
                )
            else:
                reconciled = reconcile_order(client, store, row)
                if not reconciled and row["order_status"] == "SUBMITTING":
                    store.finish_order(
                        row["signal_id"],
                        "UNKNOWN",
                        "RECONCILE_REQUIRED",
                        "submitting state has no visible tagged order; automatic resend is disabled",
                    )


def handle_match(
    client,
    store: SignalStore,
    symbol: str,
    intraday: list[Candle],
    daily: list[Candle],
    live: bool,
) -> bool:
    triggered, details = evaluate_strategy(intraday, daily)
    if not triggered:
        return False
    details = dict(details)
    details["strategy_name"] = STRATEGY_NAME
    current = now_ist()
    signal_id = store.claim(
        symbol,
        details["candle_epoch"],
        current.isoformat(timespec="seconds"),
        details,
        "LIVE" if live else "DRY_RUN",
        get_entry_qty(SCRIPT_NAME),
    )
    if signal_id is None:
        return False
    row = store.get_row(signal_id)
    with exclusive_file_lock(signal_lock_path(signal_id, store.path.parent)):
        process_claimed_row(client, store, store.get_row(signal_id), allow_live_send=live)
    return True


def market_is_open(current: dt.datetime | None = None) -> bool:
    current = current or now_ist()
    return current.weekday() < 5 and MARKET_OPEN <= current.time() <= MARKET_CLOSE


def seconds_until_next_candle_close(current: dt.datetime | None = None) -> float:
    current = current or now_ist()
    next_boundary = ((current.minute // 15) + 1) * 15
    if next_boundary == 60:
        target = current.replace(minute=0, second=0, microsecond=0) + dt.timedelta(hours=1)
    else:
        target = current.replace(minute=next_boundary, second=0, microsecond=0)
    return max(1.0, (target - current).total_seconds())


def run_cycle(
    client,
    limiter: ApiRateLimiter,
    store: SignalStore,
    live: bool,
) -> None:
    recover_pending_signals(client, store, allow_live_send=live)
    flush_excel(store, EXCEL_PATH)
    for symbol in read_symbols():
        current = now_ist()
        try:
            intraday = fetch_candles(
                client, symbol, "15", INTRADAY_HISTORY_DAYS, limiter, current
            )
            daily = fetch_candles(
                client, symbol, "D", DAILY_HISTORY_DAYS, limiter, current, daily=True
            )
            if not intraday or not daily:
                continue
            check_time = now_ist()
            latest = intraday[-1]
            latest_time = dt.datetime.fromtimestamp(latest.epoch, MARKET_TIMEZONE)
            candle_end = latest.epoch + CANDLE_SECONDS
            if latest_time.date() != check_time.date() or not (
                0 <= check_time.timestamp() - candle_end < CANDLE_SECONDS
            ):
                continue
            handle_match(client, store, symbol, intraday, daily, live)
        except FyersAuthError:
            raise
        except (OSError, RuntimeError, ValueError, sqlite3.Error) as error:
            log_message(f"SCAN ERROR | {symbol} | {error}", error=True)
    flush_excel(store, EXCEL_PATH)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--live", action="store_true", help="Send real BUY orders")
    mode.add_argument("--dry-run", action="store_true", help="Preview orders (default)")
    parser.add_argument("--once", action="store_true", help="Run one scan and exit")
    parser.add_argument("--poll-seconds", type=int, default=DEFAULT_POLL_SECONDS)
    args = parser.parse_args(argv)
    if args.poll_seconds < 1:
        parser.error("--poll-seconds must be positive")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    live = bool(args.live)
    log_message(
        f"STARTED | strategy={STRATEGY_NAME} | mode={'LIVE' if live else 'DRY_RUN'} | "
        f"window={MARKET_OPEN}-{MARKET_CLOSE} IST | volume_lookback={DAILY_VOLUME_LOOKBACK}d"
    )
    try:
        client = ConfigGatedFyersClient(
            strategy_name=STRATEGY_NAME,
            script_name=SCRIPT_NAME,
        )
        limiter = ApiRateLimiter()
        store = SignalStore(DATABASE_PATH)
        recover_pending_signals(client, store, allow_live_send=live)
        flush_excel(store, EXCEL_PATH)
        if not args.once and not market_is_open():
            log_message("STOPPED | market is closed")
            return 0
        while args.once or market_is_open():
            run_cycle(client, limiter, store, live)
            if args.once:
                break
            time.sleep(min(args.poll_seconds, seconds_until_next_candle_close()))
        log_message("STOPPED | scan complete")
        return 0
    except FyersAuthError as error:
        log_message(f"AUTHENTICATION ERROR | {error}", error=True)
        return 1
    except (OSError, RuntimeError, ValueError, sqlite3.Error) as error:
        log_message(f"ERROR | {error}", error=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
