"""Strategy-layer FYERS configuration selection and live-order guards."""
from __future__ import annotations

import json
import os
import re
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from fyers_client import FyersClient

CONFIG_ROOT = Path(__file__).resolve().parent
EQUITY_CONFIG_PATH = CONFIG_ROOT / "equity" / "config.json"
FNO_CONFIG_PATH = CONFIG_ROOT / "fno" / "config.json"
ORDER_STATE_DIR = Path(__file__).with_name("order_state")
DEFAULT_SCRIPT_NAME = "FNO"
ORDER_PLACEMENT_KEY = "place_order"
MAX_STOCKS_KEY = "max_stocks_per_day"
QTY_KEY = "qty"
STOP_LOSS_KEY = "stop_loss"
TRAILING_STOP_LOSS_KEY = "trailing_stop_loss"
MARKET_TIMEZONE = ZoneInfo("Asia/Kolkata")


class OrderPlacementDisabledError(RuntimeError):
    """Raised before a live order when place_order is not YES."""


class DailyStockLimitError(RuntimeError):
    """Raised before transmission when a strategy reaches its daily stock cap."""


def get_config_path(script_name: str = DEFAULT_SCRIPT_NAME) -> Path:
    """Select equity config for SCRIPT_NAME values beginning with Equity; else F&O."""
    name = str(script_name or DEFAULT_SCRIPT_NAME).strip()
    return EQUITY_CONFIG_PATH if name.lower().startswith("equity") else FNO_CONFIG_PATH


def _load_config(script_name: str = DEFAULT_SCRIPT_NAME) -> dict:
    try:
        config = json.loads(get_config_path(script_name).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    return config if isinstance(config, dict) else {}


def is_place_order_enabled(script_name: str = DEFAULT_SCRIPT_NAME) -> bool:
    """Return True only when the selected config enables live orders."""
    config = _load_config(script_name)
    return str(config.get(ORDER_PLACEMENT_KEY, "")).strip().upper() == "YES"


def get_max_stocks_per_day(script_name: str = DEFAULT_SCRIPT_NAME) -> int:
    """Return the selected config's daily entry-stock cap; invalid means zero."""
    value = _load_config(script_name).get(MAX_STOCKS_KEY, 0)
    if isinstance(value, bool):
        return 0
    try:
        limit = int(value)
    except (TypeError, ValueError):
        return 0
    return max(0, limit)


def get_entry_qty(script_name: str = DEFAULT_SCRIPT_NAME) -> int:
    """Return configured lots for F&O entries or shares for equity entries."""
    value = _load_config(script_name).get(QTY_KEY, 1)
    if isinstance(value, bool):
        return 1
    try:
        quantity = int(value)
    except (TypeError, ValueError):
        return 1
    return max(1, quantity)


def _get_percent_fraction(
    key: str,
    default: float,
    script_name: str = DEFAULT_SCRIPT_NAME,
) -> float:
    value = _load_config(script_name).get(key, default)
    if isinstance(value, bool):
        return default / 100.0
    try:
        percent = float(value)
    except (TypeError, ValueError):
        return default / 100.0
    if percent < 0 or percent > 100:
        return default / 100.0
    return percent / 100.0


def get_stop_loss_percent(script_name: str = DEFAULT_SCRIPT_NAME) -> float:
    """Return stop loss as a decimal fraction; config 1.5 means 1.5%."""
    return _get_percent_fraction(STOP_LOSS_KEY, 1.5, script_name)


def get_trailing_stop_loss_percent(script_name: str = DEFAULT_SCRIPT_NAME) -> float:
    """Return trailing stop as a decimal fraction; config 0.5 means 0.5%."""
    return _get_percent_fraction(TRAILING_STOP_LOSS_KEY, 0.5, script_name)


def require_place_order_enabled(
    operation: str,
    script_name: str = DEFAULT_SCRIPT_NAME,
) -> None:
    """Fail closed unless the selected strategy config explicitly contains YES."""
    if is_place_order_enabled(script_name):
        return
    config_path = get_config_path(script_name)
    raise OrderPlacementDisabledError(
        f"Live FYERS {operation} blocked: set \"{ORDER_PLACEMENT_KEY}\": \"YES\" "
        f"in {config_path}. Dry-run mode remains available."
    )


def _is_exit_order(order: dict, meta: Any) -> bool:
    """Identify strategy exit orders so risk exits never consume entry-stock slots."""
    if not isinstance(meta, dict):
        return False
    signal = str(meta.get("signal", "")).strip().upper()
    return bool(signal and "ENTRY" not in signal and (
        "EXIT" in signal or "STOP_LOSS" in signal or "CLOSE_ALL" in signal
    ))


@contextmanager
def _state_file_lock(state_path: Path):
    """Serialize daily-cap state updates across independent strategy processes."""
    state_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = state_path.with_suffix(state_path.suffix + ".lock")
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


class ConfigGatedFyersClient(FyersClient):
    """FyersClient guarded by the selected strategy config and daily stock cap."""

    def __init__(
        self,
        *args: Any,
        strategy_name: str = "default",
        script_name: str = DEFAULT_SCRIPT_NAME,
        **kwargs: Any,
    ):
        self.strategy_name = strategy_name
        self.script_name = script_name
        super().__init__(*args, **kwargs)

    def _state_path(self, strategy_name: str | None = None) -> Path:
        selected_strategy = strategy_name or self.strategy_name
        safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "_", selected_strategy).strip("_")
        return ORDER_STATE_DIR / f"{safe_name or 'default'}.json"

    def _reserve_entry_stock(
        self,
        symbol: str,
        strategy_name: str | None = None,
    ) -> None:
        selected_strategy = strategy_name or self.strategy_name
        limit = get_max_stocks_per_day(self.script_name)
        if limit <= 0:
            raise DailyStockLimitError(
                f"Live entry blocked for {selected_strategy}: {MAX_STOCKS_KEY} must be "
                f"a positive integer in {get_config_path(self.script_name)}."
            )

        today = datetime.now(MARKET_TIMEZONE).date().isoformat()
        state_path = self._state_path(selected_strategy)
        with _state_file_lock(state_path):
            try:
                state = json.loads(state_path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                state = {"date": today, "symbols": []}
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                raise DailyStockLimitError(
                    f"Cannot verify the daily stock limit for {selected_strategy}: "
                    f"invalid state file {state_path}."
                ) from exc

            if not isinstance(state, dict):
                raise DailyStockLimitError(
                    f"Cannot verify the daily stock limit for {selected_strategy}: "
                    f"invalid state file {state_path}."
                )
            state_date = state.get("date")
            if not isinstance(state_date, str) or not state_date:
                raise DailyStockLimitError(
                    f"Cannot verify the daily stock limit for {selected_strategy}: "
                    f"invalid date in {state_path}."
                )
            if state_date == today:
                symbols = state.get("symbols", [])
                if not isinstance(symbols, list) or not all(
                    isinstance(item, str) for item in symbols
                ):
                    raise DailyStockLimitError(
                        f"Cannot verify the daily stock limit for {selected_strategy}: "
                        f"invalid symbols in {state_path}."
                    )
                symbols = sorted(set(symbols))
            else:
                symbols = []

            if symbol in symbols:
                return
            if len(symbols) >= limit:
                raise DailyStockLimitError(
                    f"Daily stock limit reached for {selected_strategy}: "
                    f"{len(symbols)}/{limit} unique entry stocks attempted today. No order sent."
                )

            symbols.append(symbol)
            symbols.sort()
            payload = {"date": today, "symbols": symbols}
            temporary_name: str | None = None
            try:
                descriptor, temporary_name = tempfile.mkstemp(
                    prefix=f".{state_path.stem}.",
                    suffix=".tmp",
                    dir=state_path.parent,
                )
                with os.fdopen(descriptor, "w", encoding="utf-8") as temporary_file:
                    temporary_file.write(json.dumps(payload, indent=2) + "\n")
                os.replace(temporary_name, state_path)
                temporary_name = None
            except OSError as exc:
                if temporary_name:
                    try:
                        os.unlink(temporary_name)
                    except OSError:
                        pass
                raise DailyStockLimitError(
                    f"Cannot persist the daily stock limit for {selected_strategy}; "
                    f"no order was sent."
                ) from exc

    def place_order(
        self,
        order: dict,
        dry_run: bool = True,
        *args: Any,
        **kwargs: Any,
    ) -> dict:
        if not dry_run:
            require_place_order_enabled("order placement", self.script_name)
            if (
                isinstance(order, dict)
                and order.get("symbol")
                and not _is_exit_order(order, kwargs.get("meta"))
            ):
                meta = kwargs.get("meta")
                selected_strategy = (
                    str(meta.get("strategy"))
                    if isinstance(meta, dict) and meta.get("strategy")
                    else self.strategy_name
                )
                self._reserve_entry_stock(
                    str(order["symbol"]), selected_strategy
                )
        return super().place_order(order, dry_run=dry_run, *args, **kwargs)

    def exit_position(
        self,
        position_id: str,
        dry_run: bool = True,
        *args: Any,
        **kwargs: Any,
    ) -> dict:
        if not dry_run:
            require_place_order_enabled("position exit", self.script_name)
        return super().exit_position(position_id, dry_run=dry_run, *args, **kwargs)
