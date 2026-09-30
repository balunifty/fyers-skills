#!/usr/bin/env python3
"""Local read-only web dashboard for the configured FYERS strategies.

Run from the repository root:

    python strategies/ui/equity_strategy_dashboard.py

The dashboard scans the FNO top-100 universe plus the traded indices, evaluates
every strategy on every symbol, and renders one row per stock with a Buy/Sell
cell and the crossover bar time for each strategy column. It never places an
order.
"""
from __future__ import annotations

import argparse
import datetime as dt
import importlib.util
import json
import pathlib
import socket
import sqlite3
import sys
import threading
import time
import traceback
import webbrowser
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

UI_DIR = pathlib.Path(__file__).resolve().parent
STRATEGIES_DIR = UI_DIR.parent
REPO_ROOT = STRATEGIES_DIR.parent
SCRIPTS_DIR = STRATEGIES_DIR / "scripts"
STOCKS_PATH = STRATEGIES_DIR / "data" / "Nifty50.txt"
FNO_STOCKS_PATH = STRATEGIES_DIR / "data" / "NiftyFNOTop100.txt"
# Row universe for the dashboard. Defaults to the FNO top-100 list because the
# ORB and option strategies trade that basket. Point this at STOCKS_PATH to use
# the Nifty 50 list instead; nothing else needs changing.
UNIVERSE_PATH = FNO_STOCKS_PATH
MARKET_TIMEZONE = ZoneInfo("Asia/Kolkata")
CANDLE_SECONDS = 15 * 60
FIVE_MINUTE_SECONDS = 5 * 60
MARKET_OPEN = dt.time(9, 15)
MARKET_CLOSE = dt.time(15, 30)
HISTORY_RETRIES = 4
RETRY_BACKOFF_SECONDS = 2.0
# The longest indicator window is EMA50, which needs 51 bars. Twelve sessions of
# 15-minute candles is roughly 310 bars, so this is comfortably sufficient while
# keeping the payload small enough to scan the universe quickly.
INTRADAY_HISTORY_DAYS = 12
DAILY_HISTORY_DAYS = 120

# The dashboard already downloads 15-minute candles for every symbol on each
# scan and then discards them. Persisting them costs no extra API calls and
# gives the EOD view and any offline analysis a real history to work from.
CANDLE_DB_PATH = STRATEGIES_DIR / "databases" / "dashboard_15min_candles.db"
CANDLE_STORE_ENABLED = True
CANDLE_RETENTION_DAYS = 180

# A symbol whose last traded price is at or below this is skipped: no candles
# are fetched for it and anything already stored for it is purged. The floor
# applies to every universe, indices included. A symbol the quotes API cannot
# price is NOT treated as cheap, only a known price at or below the floor is.
MIN_PRICE = 100.0
# FYERS rejects a quotes request with more than 50 symbols.
QUOTE_BATCH_SIZE = 50

sys.path.insert(0, str(REPO_ROOT / "skills" / "fyers-trading" / "scripts"))
sys.path.insert(0, str(STRATEGIES_DIR / "config"))
sys.path.insert(0, str(STRATEGIES_DIR / "utils"))
sys.path.insert(0, str(REPO_ROOT / "strategies"))
from fyers_client import FyersClient  # noqa: E402


def log_message(message: str) -> None:
    """Print a scan-level note to the console running the dashboard."""
    print(f"[dashboard] {message}")


def log_error(message: str) -> None:
    """Print a failure. Kept separate so the wording is easy to grep for."""
    print(f"[dashboard] ERROR: {message}")


@dataclass(frozen=True)
class StrategySpec:
    """One strategy rendered as a single table column."""

    key: str
    label: str
    column: str
    module_name: str
    module_path: pathlib.Path
    evaluator_name: str
    kind: str
    universe: str = "fno"
    bar_seconds: int = CANDLE_SECONDS
    needs_daily: bool = False
    level: str = ""
    intraday_only: bool = False
    sell_only: bool = False
    hide_in_intraday: bool = False
    # A session-anchored strategy is decided by the candles at the open, so it
    # is judged once per session rather than once per bar.
    session_anchored: bool = False
    # Whether the session's first bar is judged during the session sweep.
    # Candle-pattern strategies leave it False because their "previous bar"
    # would be yesterday's close, which their own live scripts refuse to pair
    # across the overnight gap. Indicator strategies set it True: their EMAs are
    # computed over the whole series, so the open bar is a normal judgement.
    judge_session_open: bool = False


STRATEGY_SPECS = (
    StrategySpec(
        key="open_ema_stack",
        label="Buy: first or second 15-minute candle bullish and closing above "
              "the previous day's high, with ema10>ema20>ema30 and "
              "ema10>ema20>ema50. Sell: ema10<ema20<ema50",
        column="Open EMA stack",
        module_name="dashboard_open_ema_stack_strategy",
        module_path=SCRIPTS_DIR / "EquityOpenCandleEmaStackBuy15min.py",
        evaluator_name="open_candle_ema_stack_buy_signal",
        kind="open_ema_stack",
        intraday_only=True,
        needs_daily=True,
        session_anchored=True,
        # The sell side belongs in the Rejections tab, like R1 and PrevHigh.
        sell_only=True,
    ),
    StrategySpec(
        key="orb",
        label="ORB breakout/rejection + double top/bottom + EMA/RSI patterns",
        column="ORB",
        module_name="dashboard_orb_strategy",
        module_path=SCRIPTS_DIR / "OrbStrategyCallPut.py",
        evaluator_name="orb_breakout_signal",
        kind="orb",
        bar_seconds=FIVE_MINUTE_SECONDS,
        intraday_only=True,
    ),
    StrategySpec(
        key="orb_high_rejection",
        label="Bearish rejection at the opening-range high (PE)",
        column="ORB High Rej",
        module_name="dashboard_orb_strategy",
        module_path=SCRIPTS_DIR / "OrbStrategyCallPut.py",
        evaluator_name="orb_rejection_signal",
        kind="orb_high_rejection",
        intraday_only=True,
        sell_only=True,
    ),
    StrategySpec(
        key="orb_low_rejection",
        label="Bullish rejection at the opening-range low (CE)",
        column="ORB Low Rej",
        module_name="dashboard_orb_low_rejection_strategy",
        module_path=SCRIPTS_DIR / "EquityOrbLowRejectionSignal15min.py",
        evaluator_name="orb_low_rejection_buy_signal",
        kind="orb_low_rejection",
        intraday_only=True,
    ),
    StrategySpec(
        key="double_bottom",
        label="Double Bottom - 15-minute candle closed above its open (CE)",
        column="Double Bottom",
        module_name="dashboard_double_bottom_strategy",
        module_path=SCRIPTS_DIR / "EquityDoubleBottomBullishSignal15min.py",
        evaluator_name="double_bottom_bullish_signal",
        kind="double_bottom",
        needs_daily=True,
    ),
    StrategySpec(
        key="r1_rejection",
        label="Rejection/bounce at R1 (classic pivot 2P-L from the previous day)",
        column="R1 rejection",
        module_name="dashboard_orb_strategy",
        module_path=SCRIPTS_DIR / "OrbStrategyCallPut.py",
        evaluator_name="r1_rejection_signal",
        kind="level_rejection",
        level="r1",
        needs_daily=True,
        sell_only=True,
    ),
    StrategySpec(
        key="prev_high_rejection",
        label="Rejection/bounce at the previous trading day's high (PDH)",
        column="PDH Rejection",
        module_name="dashboard_orb_strategy",
        module_path=SCRIPTS_DIR / "OrbStrategyCallPut.py",
        evaluator_name="prev_high_rejection_signal",
        kind="level_rejection",
        level="prev_high",
        needs_daily=True,
        sell_only=True,
    ),
    StrategySpec(
        key="doji_rejection",
        label="Doji rejection - open=high bar closing below the prior doji's low",
        column="Doji rejection",
        module_name="dashboard_r1_rejection_strategy",
        module_path=SCRIPTS_DIR / "R1PrevHighRejectionStrategy.py",
        evaluator_name="doji_rejection_signal",
        kind="doji_rejection",
        sell_only=True,
    ),
    StrategySpec(
        key="higher_high_rejection",
        label="Higher high, then close below the previous bar's low",
        column="HigherHigh rej",
        module_name="dashboard_r1_rejection_strategy",
        module_path=SCRIPTS_DIR / "R1PrevHighRejectionStrategy.py",
        evaluator_name="higher_high_low_rejection_signal",
        kind="higher_high",
        sell_only=True,
    ),
    StrategySpec(
        key="higher_high_close_rejection",
        label="Higher high, then close below the previous bar's close",
        column="HH vs Close",
        module_name="dashboard_r1_rejection_strategy",
        module_path=SCRIPTS_DIR / "R1PrevHighRejectionStrategy.py",
        evaluator_name="higher_high_close_rejection_signal",
        kind="higher_high_close",
        sell_only=True,
    ),
    StrategySpec(
        key="lower_high_close",
        label="Lower high (high below the previous bar's high) and close below "
              "the previous bar's close (15-minute)",
        column="LH",
        module_name="dashboard_lower_high_strategy",
        module_path=SCRIPTS_DIR / "EquityLowerHighCloseSignal15min.py",
        evaluator_name="lower_high_close_sell_signal",
        kind="lower_high",
        # A rejection, so its sell belongs in the sell-only tab like R1.
        sell_only=True,
    ),
    StrategySpec(
        key="second_candle",
        label="Second Candle Breakout (CE/PE)",
        column="SecondCandle",
        module_name="dashboard_second_candle_strategy",
        module_path=SCRIPTS_DIR / "SecondCandleBreakout.py",
        evaluator_name="second_candle_breakout_signal",
        kind="second_candle",
        intraday_only=True,
    ),
    StrategySpec(
        key="ema_10_20_30",
        label="EMA 10/20/30 crossover + RSI cross above 60 (5-minute)",
        column="ema:10-20-30",
        module_name="dashboard_ema_10_20_30_strategy",
        module_path=SCRIPTS_DIR / "BuyCallOption102030EmaCrossover5min.py",
        evaluator_name="ema_rsi_entry_signal",
        kind="ema_10_20_30",
        bar_seconds=FIVE_MINUTE_SECONDS,
    ),
    StrategySpec(
        key="ema_fresh",
        label="EMA 15/10/20/50 fresh crossover",
        column="ema15:10-20-50 crossover",
        module_name="dashboard_ema_strategy",
        module_path=SCRIPTS_DIR / "EquityEma15_10_20_50Crossover15min.py",
        evaluator_name="evaluate_fresh_crossover",
        kind="ema",
    ),
    StrategySpec(
        key="ema_10_cross_20",
        label="Buy: ema10 crossing above ema20. Sell: ema10 crossing below "
              "ema20; or ema10<ema20<ema30 with the close below ema10; or "
              "ema10<ema20<ema50 with the close below ema10 (15-minute)",
        column="ema10:20 signals",
        module_name="dashboard_ema_10_cross_20_strategy",
        module_path=SCRIPTS_DIR / "EquityEma10_20_Signals15min.py",
        evaluator_name="ema10_ema20_signal",
        kind="ema_cross",
        # Not session anchored: a crossing can happen at any bar, so this is
        # judged on every 15-minute bar of the session like the other trends.
        # The 09:15 bar counts too, since the EMAs run over the whole series
        # and a crossing at the open is a real one.
        judge_session_open=True,
    ),
    StrategySpec(
        key="ema_pullback",
        label="EMA10 bullish pullback/up-cross",
        column="EMA10 pullback",
        module_name="dashboard_ema_pullback_strategy",
        module_path=SCRIPTS_DIR / "EquityEma15_10_20_50Crossover15min.py",
        evaluator_name="evaluate_ema10_pullback_cross",
        kind="ema",
    ),
    StrategySpec(
        key="daily_breakout",
        label="Daily breakout + RSI/volume",
        column="Daily breakout",
        module_name="dashboard_daily_breakout_strategy",
        module_path=SCRIPTS_DIR / "EquityDailyBreakoutRsiVolume15min.py",
        evaluator_name="evaluate_strategy",
        kind="daily",
        needs_daily=True,
        hide_in_intraday=True,
    ),
    StrategySpec(
        key="index_rejection",
        label="Index Rejection (NIFTY/SENSEX/BANKNIFTY)",
        column="Index rejection",
        module_name="dashboard_index_rejection_strategy",
        module_path=SCRIPTS_DIR / "IndexRejectionStrategy.py",
        evaluator_name="index_rejection_signal",
        kind="index_rejection",
        universe="index_config",
        intraday_only=True,
        hide_in_intraday=True,
    ),
)
STRATEGY_BY_KEY = {spec.key: spec for spec in STRATEGY_SPECS}


def load_module(module_name: str, path: pathlib.Path):
    """Load a strategy script without executing its main block."""
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load strategy module: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


class StrategyRegistry:
    """Lazy-load strategy modules and expose their signal evaluators."""

    def __init__(self, specs=STRATEGY_SPECS):
        self.specs = tuple(specs)
        self._modules: dict[str, Any] = {}

    def module_for(self, spec: StrategySpec):
        if spec.module_name not in self._modules:
            self._modules[spec.module_name] = load_module(
                spec.module_name, spec.module_path
            )
        return self._modules[spec.module_name]

    def evaluator_for(self, spec: StrategySpec):
        return getattr(self.module_for(spec), spec.evaluator_name)

    def selected_specs(self, selection: str) -> tuple[StrategySpec, ...]:
        if selection == "all":
            return self.specs
        spec = STRATEGY_BY_KEY.get(selection)
        if spec is None:
            raise ValueError(f"Unknown strategy: {selection}")
        return (spec,)

    def labels(self) -> list[dict[str, str]]:
        return [
            {
                "key": spec.key,
                "column": spec.column,
                "label": spec.label,
                "universe": spec.universe,
            }
            for spec in self.specs
        ]


BUY = "BUY"
SELL = "SELL"
NEUTRAL = "-"
SIGNAL_TIME_KEYS = ("curr_time", "third_candle_time", "curr_candle_time", "signal_time")


def signal_state(signal: str) -> str:
    """Map a strategy signal onto the shared Buy/Sell vocabulary."""
    if signal in (BUY, "CE"):
        return BUY
    if signal in (SELL, "PE"):
        return SELL
    return NEUTRAL


def signal_note(details: dict, fallback: str) -> str:
    """Short human label describing which sub-pattern fired."""
    return str(details.get("strategy") or fallback)


def bar_close_time_ist(
    details: dict,
    candles: list[Any],
    bar_seconds: int,
    session_close: bool = False,
) -> str | None:
    """Return HH:MM IST close time of the bar that produced the signal.

    A daily bar has no meaningful intra-day timestamp, so end-of-day signals
    are stamped with the 15:30 session close.

    A signal may declare its own bar size in details["bar_seconds"]. One column
    can aggregate several timeframes: the ORB column reads 5-minute bars for
    its breakout and 15-minute bars for everything else, so falling back to the
    spec's bar size alone would stamp every 15-minute signal ten minutes early.
    """
    if session_close:
        return MARKET_CLOSE.strftime("%H:%M")
    seconds = details.get("bar_seconds") or bar_seconds
    for key in SIGNAL_TIME_KEYS:
        raw = details.get(key)
        if not raw:
            continue
        try:
            started = dt.datetime.fromisoformat(str(raw))
        except (TypeError, ValueError):
            continue
        if started.tzinfo is None:
            started = started.replace(tzinfo=MARKET_TIMEZONE)
        close = started.astimezone(MARKET_TIMEZONE) + dt.timedelta(seconds=seconds)
        return close.strftime("%H:%M")
    if candles:
        latest = candles[-1]
        close = dt.datetime.fromtimestamp(latest.epoch, MARKET_TIMEZONE)
        return (close + dt.timedelta(seconds=bar_seconds)).strftime("%H:%M")
    return None


def empty_cell() -> dict[str, Any]:
    return {
        "state": NEUTRAL,
        "time_ist": None,
        "note": "",
        "price": None,
        "times_ist": [],
        "hit_count": 0,
        "on_latest_bar": False,
    }


def session_bar_bounds(candles: list[Any]) -> tuple[int, int]:
    """Half-open index range of the final session inside a 15-minute series.

    Judges every bar of the most recent session rather than only the newest one,
    so a pattern that fired at 10:15 is still reported at close. The series is
    ascending by epoch, as parse_candles returns it.
    """
    if not candles:
        return 0, 0
    last_day = dt.datetime.fromtimestamp(
        candles[-1].epoch, MARKET_TIMEZONE).date()
    for index, candle in enumerate(candles):
        if dt.datetime.fromtimestamp(
                candle.epoch, MARKET_TIMEZONE).date() == last_day:
            return index, len(candles)
    return 0, len(candles)


def session_open(candles: list[Any]) -> float | None:
    """Open of the first bar of the final session.

    This is the base the total gain is measured from, so the figure reads as
    the whole day's move rather than the latest bar's. Returns None when there
    is nothing usable, and the caller then falls back to the latest bar's open.
    """
    start, _ = session_bar_bounds(candles)
    if not candles or start >= len(candles):
        return None
    value = float(candles[start].open)
    return value or None


# Extra opening-range signals reported in the ORB column. They live in their
# own module so OrbStrategyCallPut.py, which is the user's own working file, is
# left untouched while its column still grows. needs_daily says whether the
# evaluator wants the daily series, for the previous day's high or close.
#
# Order matters: build_cell keeps the first firing outcome and a cell holds one
# state, so the buy is listed first and wins a tie against either sell. Between
# the two sells the gap-down rule comes first, being the more deliberate of the
# two setups.
#: The level-rejection rules, imported so the dashboard and the consolidated
#: 15-minute scanner share one implementation of each.
_level_module = load_module(
    "dashboard_level_rejection_strategy",
    SCRIPTS_DIR / "EquityLevelRejectionSignal15min.py",
)
level_rejection_signal = _level_module.level_rejection_signal

ORB_COMPANION_SIGNALS = (
    (
        "dashboard_orb_opening_signals",
        "EquityOpenRangeOpeningSignals15min.py",
        "second_candle_prev_high_buy_signal",
        True,
    ),
    (
        "dashboard_orb_opening_signals",
        "EquityOpenRangeOpeningSignals15min.py",
        "first_candle_gap_down_sell_signal",
        True,
    ),
    (
        "dashboard_orb_opening_signals",
        "EquityOpenRangeOpeningSignals15min.py",
        "first_candle_small_body_sell_signal",
        False,
    ),
    (
        "dashboard_orb_opening_signals",
        "EquityOpenRangeOpeningSignals15min.py",
        "second_candle_gap_ema_sell_signal",
        False,
    ),
)


def sweeps_session(spec: StrategySpec) -> bool:
    """Whether this strategy is judged on every 15-minute bar of the session.

    Only 15-minute strategies qualify. A 5-minute strategy is judged on its own
    latest bar, the daily breakout reads daily bars, which have no intraday
    session to walk, and a session-anchored strategy is settled by the opening
    candles, so walking the session would only repeat the same verdict.
    """
    if spec.session_anchored:
        return False
    return spec.bar_seconds == CANDLE_SECONDS and spec.kind != "daily"


def eod_specs(specs: list[StrategySpec]) -> list[StrategySpec]:
    """The strategies that still mean something when judged on a daily bar.

    A strategy whose logic is about the shape of a session cannot be read off a
    daily bar: there is no 09:15 open, no 5-minute opening range and no second
    candle, so those are marked intraday_only. Handing them a daily series
    anyway found no such bar and reported nothing, so the EOD tab used to carry
    six permanently blank columns - the three ORB ones among them. They are
    dropped here instead, so every column the tab shows is judged on data the
    strategy can actually read.
    """
    return [spec for spec in specs if not spec.intraday_only]


def merge_session_cells(cells: list[dict[str, Any]]) -> dict[str, Any]:
    """Fold one cell per 15-minute bar into a single day-level cell.

    The newest firing bar is the one shown, being the freshest read of the
    pattern, but every firing time is kept so the day can be read at a glance.
    on_latest_bar says whether the newest firing bar is the newest bar of the
    session: when it is false the pattern has already stopped forming, which
    the table marks so it is not mistaken for a live signal.
    """
    hits = [cell for cell in cells if cell["state"] in (BUY, SELL)]
    if not hits:
        return empty_cell()
    latest = hits[-1]
    times: list[str] = []
    for cell in hits:
        stamp = cell.get("time_ist")
        if stamp and stamp not in times:
            times.append(stamp)
    return {
        "state": latest["state"],
        "time_ist": latest.get("time_ist"),
        "note": latest.get("note") or "",
        "price": latest.get("price"),
        "times_ist": times,
        "hit_count": len(times),
        "on_latest_bar": latest is cells[-1],
    }


def build_cell(
    spec: StrategySpec,
    outcomes: list[tuple[str, dict, str]],
    candles: list[Any],
    session_close: bool = False,
) -> dict[str, Any]:
    """Collapse the first firing signal for a strategy into one table cell."""
    for signal, details, note in outcomes:
        state = signal_state(signal)
        if state == NEUTRAL:
            continue
        price = details.get("curr_close")
        when = bar_close_time_ist(
            details, candles, spec.bar_seconds, session_close
        )
        return {
            "state": state,
            "time_ist": when,
            "note": note,
            "price": round(float(price), 2) if isinstance(price, (int, float)) else None,
            "times_ist": [when] if when else [],
            "hit_count": 1,
            "on_latest_bar": True,
        }
    return empty_cell()


def error_cell(message: str) -> dict[str, Any]:
    cell = empty_cell()
    cell["state"] = "error"
    cell["note"] = str(message)[:120]
    return cell


def market_is_open(current: dt.datetime) -> bool:
    """Weekday inside the 09:15-15:30 IST cash session."""
    return (
        current.weekday() < 5
        and MARKET_OPEN <= current.time() <= MARKET_CLOSE
    )


def describe_data_state(
    freshness_by_symbol: dict[str, dict[str, Any]],
    all_latest: bool,
    current: dt.datetime,
) -> dict[str, Any]:
    """Explain whether the data is live, or a previous session's close.

    On a weekend or holiday the newest bar is legitimately the last trading
    day's, so saying "data is not latest" would be misleading.
    """
    timestamps = [
        item["latest_candle_ist"]
        for item in freshness_by_symbol.values()
        if item.get("latest_candle_ist")
    ]
    # latest_candle_ist is the bar's START; report when that bar closed instead,
    # so the figure matches the crossover times shown in each cell.
    as_of = None
    if timestamps:
        try:
            as_of = dt.datetime.fromisoformat(max(timestamps)) + dt.timedelta(
                seconds=CANDLE_SECONDS
            )
        except ValueError:
            as_of = None

    if all_latest:
        message = "Data is live - latest 15-minute candle"
        kind = "live"
        note = ""
    elif not market_is_open(current):
        if as_of:
            when = (
                "today" if as_of.date() == current.date()
                else as_of.strftime("%a %d %b")
            )
            message = f"Market closed - showing {when} close (no live session)"
            note = (
                f"Market is closed, so the latest completed 15-minute candle is "
                f"from {as_of:%a %d %b %H:%M}. Crossover times are that session's "
                f"bar close times, not live ticks."
            )
        else:
            message = "Market closed - no candle data returned"
            note = "Market is closed and no candle data was returned."
        kind = "closed"
    else:
        message = "Data is not latest 15min - refresh to catch up"
        kind = "stale"
        note = "The market is open but the data is behind. Refresh to catch up."

    return {
        "market_open": market_is_open(current),
        "as_of_ist": as_of.isoformat(timespec="seconds") if as_of else None,
        "as_of_label": as_of.strftime("%a %d %b %H:%M") if as_of else None,
        "message": message,
        "note": note,
        "kind": kind,
    }


def daily_freshness_item(daily: list[Any]) -> dict[str, Any]:
    """Describe the newest completed daily bar for the EOD view."""
    latest = daily[-1]
    bar_day = dt.datetime.fromtimestamp(latest.epoch, MARKET_TIMEZONE).date()
    return {
        "latest": True,
        "latest_candle_ist": dt.datetime.combine(
            bar_day, MARKET_OPEN, tzinfo=MARKET_TIMEZONE
        ).isoformat(timespec="seconds"),
        "bar_date": bar_day.isoformat(),
        "age_seconds": None,
        "message": "Last completed daily candle",
    }


def describe_eod_state(
    daily_freshness: dict[str, dict[str, Any]],
    current: dt.datetime,
    intraday_only: tuple[str, ...] = (),
) -> dict[str, Any]:
    """The EOD tab always shows the last completed session's close."""
    dates = sorted(
        item["bar_date"] for item in daily_freshness.values() if item.get("bar_date")
    )
    if not dates:
        return {
            "market_open": False,
            "as_of_ist": None,
            "as_of_label": None,
            "message": "No daily candle data available",
            "note": "No completed daily candle was returned, so there is no EOD view.",
            "kind": "closed",
        }

    bar_day = dt.date.fromisoformat(max(dates))
    label = bar_day.strftime("%a %d %b")
    as_of = dt.datetime.combine(bar_day, MARKET_CLOSE, tzinfo=MARKET_TIMEZONE)
    note = (
        f"End-of-day signals are evaluated on daily bars, so every cell and "
        f"price below is from the {label} close."
    )
    if intraday_only:
        note += (
            " These need intraday bars, so they are not columns here: "
            + ", ".join(intraday_only) + "."
        )

    return {
        "market_open": False,
        "as_of_ist": as_of.isoformat(timespec="seconds"),
        "as_of_label": label,
        "message": f"End-of-day view - last completed session {label}",
        "note": note,
        "kind": "eod",
    }


def build_view(
    view_id: str,
    title: str,
    rows: list[dict[str, Any]],
    state: dict[str, Any],
    all_latest: bool,
    fresh_symbols: int,
    total_symbols: int,
    columns: list[dict[str, Any]],
    sell_only: bool = False,
    column_keys: set[str] | None = None,
) -> dict[str, Any]:
    """Assemble one tab's payload.

    A tab may show a subset of the strategy columns (column_keys). Cells outside
    that subset are dropped and the per-row signal count is recomputed, so the
    "With signals" figure and the row highlight only ever reflect what is
    actually on screen. The sell-only tab additionally blanks every buy cell.
    """
    message = state["message"]
    if not all_latest and total_symbols:
        message = f"{message} ({fresh_symbols}/{total_symbols} symbols current)"

    if column_keys is None:
        column_keys = {column["key"] for column in columns}
    view_columns = [c for c in columns if c["key"] in column_keys]
    subset = view_columns != list(columns)

    def signal_count_for(cells: dict[str, dict]) -> int:
        """How many signals this tab should count for a row.

        The sell-only tab counts sells only, because it hides every buy cell.
        """
        total = 0
        for cell in cells.values():
            state = cell.get("state")
            if state == SELL:
                total += 1
            elif state == BUY and not sell_only:
                total += 1
        return total

    view_rows = rows
    if subset or sell_only:
        view_rows = []
        for row in rows:
            source = row.get("cells") or {}
            cells = {}
            for key in column_keys:
                cell = source.get(key)
                if not cell:
                    cells[key] = empty_cell()
                elif sell_only and cell.get("state") != SELL:
                    cells[key] = empty_cell()
                else:
                    cells[key] = cell
            # Recomputed so the row highlight and filter chips reflect only the
            # signals this tab actually shows.
            view_rows.append({
                **row, "cells": cells,
                "signal_count": signal_count_for(cells),
            })

    def counts_as_signal(row: dict[str, Any]) -> bool:
        return signal_count_for(row["cells"]) > 0

    groups = {row.get("group") for row in view_rows}
    return {
        "id": view_id,
        "title": title,
        "columns": view_columns,
        "sell_only": sell_only,
        "rows": view_rows,
        "total_rows": len(view_rows),
        "signal_rows": sum(1 for row in view_rows if counts_as_signal(row)),
        # A tab holding a single kind of row (stocks only, or indices only)
        # needs no "FNO top 100" / "Indices" band above it.
        "single_group": len(groups) <= 1,
        "latest_15min": all_latest,
        "latest_message": message,
        "market_open": state["market_open"],
        "as_of_ist": state["as_of_ist"],
        "as_of_label": state["as_of_label"],
        "note": state["note"],
        "kind": state["kind"],
    }


def read_symbols(path: pathlib.Path | None = None) -> list[str]:
    """Read one symbol per line, skipping blanks and comments."""
    return read_symbol_file(path or STOCKS_PATH)


def now_ist() -> dt.datetime:
    return dt.datetime.now(MARKET_TIMEZONE)


def candle_freshness(candles: list[Any], current: dt.datetime) -> dict[str, Any]:
    """Describe whether the latest returned bar is the expected latest 15m bar."""
    if not candles:
        return {
            "latest": False,
            "latest_candle_ist": None,
            "age_seconds": None,
            "message": "No completed 15-minute candle was returned",
        }
    latest = candles[-1]
    end_epoch = int(latest.epoch) + CANDLE_SECONDS
    age_seconds = int(current.timestamp() - end_epoch)
    is_latest = 0 <= age_seconds < CANDLE_SECONDS
    latest_time = dt.datetime.fromtimestamp(latest.epoch, MARKET_TIMEZONE)
    return {
        "latest": is_latest,
        "latest_candle_ist": latest_time.isoformat(timespec="seconds"),
        "age_seconds": age_seconds,
        "message": "Latest 15-minute candle" if is_latest else "15-minute candle is stale",
    }


class DashboardRateLimiter:
    """Rate-limit dashboard history calls independently of strategy scripts."""

    def __init__(self, calls_per_second: int = 5):
        self.interval = 1.0 / calls_per_second
        self.last_call = 0.0

    def call(self, function, *args, **kwargs):
        elapsed = time.monotonic() - self.last_call
        if elapsed < self.interval:
            time.sleep(self.interval - elapsed)
        self.last_call = time.monotonic()
        return function(*args, **kwargs)


class CandleStore:
    """Persist downloaded candles to one SQLite file.

    A single table keyed on (symbol, resolution, epoch) is used rather than the
    per-symbol table layout the older strategy scripts use: it needs no DDL per
    symbol, keeps one index instead of hundreds of tables, and merges repeated
    scans for free through INSERT OR REPLACE.
    """

    def __init__(self, path: pathlib.Path | None = None, enabled: bool = True):
        # Resolved at call time so CANDLE_DB_PATH can be redirected, which is
        # what keeps test runs from writing into the real database.
        self.path = pathlib.Path(path) if path is not None else CANDLE_DB_PATH
        self.enabled = enabled
        self._connection: sqlite3.Connection | None = None
        self.stored_rows = 0

    def connect(self) -> sqlite3.Connection | None:
        """Open the database, creating the schema on first use.

        Must be called from the thread that will use the connection; the scan
        runs in its own thread and only one scan runs at a time.
        """
        if not self.enabled or self._connection is not None:
            return self._connection
        try:
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
        except sqlite3.Error as error:
            log_message(f"CANDLE_STORE_UNAVAILABLE: {error}")
            self.enabled = False
            self._connection = None
        return self._connection

    def close(self) -> None:
        if self._connection is not None:
            try:
                self._connection.close()
            except sqlite3.Error:
                pass
            self._connection = None

    def purge_symbols(self, symbols: list[str]) -> int:
        """Delete every stored bar for these symbols. Returns rows removed.

        Used when a symbol drops out of the scan, so the local cache does not
        keep serving history the dashboard no longer covers.
        """
        connection = self.connect()
        if connection is None:
            return 0
        wanted = [s for s in dict.fromkeys(symbols) if s]
        if not wanted:
            return 0
        removed = 0
        try:
            for start in range(0, len(wanted), 400):
                batch = wanted[start:start + 400]
                placeholders = ",".join("?" * len(batch))
                cursor = connection.execute(
                    f"DELETE FROM candles WHERE symbol IN ({placeholders})",
                    batch,
                )
                removed += cursor.rowcount if cursor.rowcount > 0 else 0
            connection.commit()
        except sqlite3.Error as error:
            log_error(f"CANDLE_STORE_PURGE_FAILED: {error}")
            return 0
        if removed:
            log_message(
                f"CANDLE_STORE_PURGE: removed {removed} rows for "
                f"{len(wanted)} symbol(s) priced at or below {MIN_PRICE:g}")
        return removed

    def store(self, symbol: str, resolution: str, candles: list[Any]) -> int:
        """Merge one symbol's bars. Returns the number of rows written."""
        connection = self.connect()
        if connection is None or not candles:
            return 0
        stored_at = now_ist().isoformat(timespec="seconds")
        rows = [
            (
                symbol, resolution, int(candle.epoch),
                float(candle.open), float(candle.high), float(candle.low),
                float(candle.close), float(candle.volume), stored_at,
            )
            for candle in candles
        ]
        try:
            connection.executemany(
                "INSERT OR REPLACE INTO candles "
                "(symbol, resolution, epoch, open, high, low, close, volume, stored_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                rows,
            )
            connection.commit()
        except sqlite3.Error as error:
            log_error(f"CANDLE_STORE_WRITE_FAILED {symbol}: {error}")
            return 0
        self.stored_rows += len(rows)
        return len(rows)

    def prune(self, retention_days: int = CANDLE_RETENTION_DAYS) -> int:
        """Drop bars older than the retention window."""
        connection = self.connect()
        if connection is None:
            return 0
        cutoff = int(
            (now_ist() - dt.timedelta(days=retention_days)).timestamp()
        )
        try:
            cursor = connection.execute("DELETE FROM candles WHERE epoch < ?", (cutoff,))
            connection.commit()
            return cursor.rowcount or 0
        except sqlite3.Error as error:
            log_error(f"CANDLE_STORE_PRUNE_FAILED: {error}")
            return 0

    def summary(self) -> dict[str, Any]:
        """Counts and date ranges, so the page can show what is stored."""
        connection = self.connect()
        if connection is None:
            return {
                "enabled": self.enabled, "path": str(self.path),
                "total_rows": 0, "by_resolution": [],
            }
        try:
            rows = connection.execute("""
                SELECT resolution,
                       COUNT(*),
                       COUNT(DISTINCT symbol),
                       MIN(epoch),
                       MAX(epoch)
                FROM candles
                GROUP BY resolution
                ORDER BY resolution
            """).fetchall()
            totals = connection.execute(
                "SELECT COUNT(*), COUNT(DISTINCT symbol) FROM candles"
            ).fetchone()
            index_rows = connection.execute(
                "SELECT COUNT(DISTINCT symbol) FROM candles WHERE symbol LIKE '%-INDEX'"
            ).fetchone()[0]
        except sqlite3.Error as error:
            log_error(f"CANDLE_STORE_SUMMARY_FAILED: {error}")
            return {
                "enabled": self.enabled, "path": str(self.path),
                "total_rows": 0, "by_resolution": [],
            }

        def stamp(epoch: int | None) -> str | None:
            if not epoch:
                return None
            return dt.datetime.fromtimestamp(epoch, MARKET_TIMEZONE).isoformat(
                timespec="minutes")

        return {
            "enabled": True,
            "path": str(self.path),
            "total_rows": int(totals[0] or 0),
            "symbols": int(totals[1] or 0),
            "index_symbols": int(index_rows or 0),
            "by_resolution": [
                {
                    "resolution": row[0],
                    "rows": int(row[1]),
                    "symbols": int(row[2]),
                    "oldest_ist": stamp(row[3]),
                    "newest_ist": stamp(row[4]),
                }
                for row in rows
            ],
        }


def parse_candles(
    response: dict,
    current: dt.datetime,
    seconds: int,
    daily: bool = False,
) -> list[Any]:
    """Parse FYERS history rows into simple read-only candle objects."""
    current_epoch = int(current.timestamp())
    by_epoch: dict[int, Any] = {}
    for row in response.get("candles") or []:
        if not isinstance(row, (list, tuple)) or len(row) < 6:
            continue
        try:
            epoch = int(row[0])
            candle = type(
                "HistoryCandle",
                (),
                {
                    "epoch": epoch,
                    "open": float(row[1]),
                    "high": float(row[2]),
                    "low": float(row[3]),
                    "close": float(row[4]),
                    "volume": float(row[5]),
                },
            )()
        except (TypeError, ValueError):
            continue
        candle_date = dt.datetime.fromtimestamp(epoch, MARKET_TIMEZONE).date()
        if daily:
            if candle_date < current.date():
                by_epoch[epoch] = candle
        elif epoch + seconds <= current_epoch:
            by_epoch[epoch] = candle
    return [by_epoch[epoch] for epoch in sorted(by_epoch)]


def fetch_history(
    client,
    limiter: DashboardRateLimiter,
    symbol: str,
    resolution: str,
    current: dt.datetime,
    days: int,
    seconds: int,
    daily: bool = False,
) -> list[Any]:
    """Fetch candles, backing off when FYERS throttles with HTTP 429."""
    start = (current.date() - dt.timedelta(days=days)).isoformat()
    end = current.date().isoformat()
    for attempt in range(HISTORY_RETRIES):
        try:
            response = limiter.call(
                client.history, symbol, resolution, start, end
            )
        except Exception as error:
            if "429" not in str(error) or attempt == HISTORY_RETRIES - 1:
                raise
            time.sleep(RETRY_BACKOFF_SECONDS * (attempt + 1))
            continue
        if response.get("s") != "ok":
            raise RuntimeError(f"history fetch failed for {symbol}: {response}")
        return parse_candles(response, current, seconds, daily=daily)
    raise RuntimeError(f"history fetch exhausted retries for {symbol}")


def read_symbol_file(path: pathlib.Path) -> list[str]:
    return list(dict.fromkeys(
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ))


def candle_at_time(candles: list[Any], current: dt.datetime, hour: int, minute: int):
    target_date = current.date()
    target = dt.time(hour, minute)
    for candle in candles:
        timestamp = dt.datetime.fromtimestamp(candle.epoch, MARKET_TIMEZONE)
        if timestamp.date() == target_date and timestamp.time() == target:
            return candle
    return None


def last_traded_prices(
    client,
    limiter: "DashboardRateLimiter",
    symbols: list[str],
) -> dict[str, float]:
    """Last traded price per symbol, in the batches the quotes API accepts.

    FYERS returns the symbol under "n"; "s" is the per-item status, not the
    name. A symbol the API cannot price is simply absent from the result: some
    indices answer with no last price at all, and a missing price must never be
    read as a low one, or those symbols would silently disappear.
    """
    prices: dict[str, float] = {}
    unique = list(dict.fromkeys(symbols))
    for start in range(0, len(unique), QUOTE_BATCH_SIZE):
        batch = unique[start:start + QUOTE_BATCH_SIZE]
        try:
            response = limiter.call(client.quotes, batch)
        except Exception:
            # No price is better than a wrong one: the caller keeps anything it
            # cannot price, so a failed request degrades to no filtering.
            continue
        if not isinstance(response, dict) or response.get("s") != "ok":
            continue
        for item in response.get("d") or []:
            if not isinstance(item, dict):
                continue
            values = item.get("v") or {}
            symbol = item.get("n") or values.get("symbol")
            try:
                price = float(values.get("lp") or 0)
            except (TypeError, ValueError):
                continue
            if symbol and price > 0:
                prices[symbol] = price
    return prices


def apply_price_floor(
    targets: list[tuple[str, str, str]],
    prices: dict[str, float],
    min_price: float = MIN_PRICE,
) -> tuple[list[tuple[str, str, str]], list[dict[str, Any]]]:
    """Split scan targets on the price floor.

    Returns (kept, excluded). A symbol is excluded only when its price is known
    and at or below the floor; an unpriced symbol is kept, so a partial quotes
    response can never shrink the universe on a guess.
    """
    kept: list[tuple[str, str, str]] = []
    excluded: list[dict[str, Any]] = []
    for display_symbol, history_symbol, group in targets:
        price = prices.get(history_symbol)
        if price is not None and price <= min_price:
            excluded.append({
                "symbol": display_symbol,
                "history_symbol": history_symbol,
                "group": group,
                "price": price,
            })
            continue
        kept.append((display_symbol, history_symbol, group))
    return kept, excluded


class DashboardScanner:
    """Fetch candles once per symbol and evaluate every strategy on it."""

    def __init__(self, registry: StrategyRegistry | None = None):
        self.registry = registry or StrategyRegistry()

    def _resolve_specs(self, specs) -> tuple[list[StrategySpec], dict[str, Any], list[str]]:
        """Load every selected strategy module once, reporting load failures."""
        loaded: list[StrategySpec] = []
        modules: dict[str, Any] = {}
        errors: list[str] = []
        for spec in specs:
            try:
                modules[spec.key] = self.registry.module_for(spec)
            except Exception as error:
                errors.append(f"{spec.key}: {error}")
                continue
            loaded.append(spec)
        return loaded, modules, errors

    def _scan_targets(
        self,
        specs: list[StrategySpec],
        modules: dict[str, Any],
    ) -> list[tuple[str, str, str]]:
        """Return (display symbol, history symbol, group) rows for this scan.

        Stocks come from the FNO top-100 list, then the configured indices are
        appended so index-only strategies still have somewhere to report.
        """
        targets: list[tuple[str, str, str]] = []
        seen: set[str] = set()

        def add(display_symbol: str, history_symbol: str, group: str) -> None:
            if display_symbol in seen:
                return
            seen.add(display_symbol)
            targets.append((display_symbol, history_symbol, group))

        if any(spec.universe == "fno" for spec in specs):
            for symbol in read_symbols(UNIVERSE_PATH):
                add(symbol, symbol, "stock")
        for spec in specs:
            if spec.universe != "index_config":
                continue
            for index_name, config in modules[spec.key].INDEX_CONFIG.items():
                add(index_name, config["fy_symbol"], "index")
        return targets

    def _specs_for_group(
        self,
        specs: list[StrategySpec],
        group: str,
    ) -> list[StrategySpec]:
        """Which strategies a row of this group is judged by.

        A stock row skips the index-only strategies, which have nothing to say
        about an individual stock. An index row is judged by *every* strategy,
        the index-only ones included: the 15-minute and daily patterns are pure
        functions of candles and read an index series perfectly well, so the
        indices tab shows the same columns as the others rather than one.
        """
        if group == "index":
            return list(specs)
        return [spec for spec in specs if spec.universe != "index_config"]

    def describe_universe(self, selection: str = "all") -> dict[str, Any]:
        """Explain which symbols a scan will cover, before it runs.

        The page shows this so the row count is never a mystery.
        """
        try:
            specs = self.registry.selected_specs(selection)
        except ValueError:
            return {"total": 0, "stocks": 0, "indices": 0, "label": "unknown",
                    "source": None, "detail": "Unknown strategy selection"}

        indices: list[str] = []
        for spec in specs:
            if spec.universe != "index_config":
                continue
            try:
                indices = list(self.registry.module_for(spec).INDEX_CONFIG)
            except Exception:
                continue

        try:
            stocks = read_symbols(UNIVERSE_PATH)
        except Exception as error:
            stocks = []
            return {"total": 0, "stocks": 0, "indices": len(indices),
                    "label": "unreadable", "source": UNIVERSE_PATH.name,
                    "detail": f"Could not read {UNIVERSE_PATH.name}: {error}"}

        source = UNIVERSE_PATH.name
        label = {
            "NiftyFNOTop100.txt": "FNO Top 100",
            "Nifty50.txt": "Nifty 50",
        }.get(source, source)
        parts = [f"{len(stocks)} {label} stocks"]
        if indices:
            parts.append(f"{len(indices)} indices ({', '.join(indices)})")
        # Stated up front, because the scanned row count will be lower than the
        # file's symbol count whenever any of them price under the floor.
        parts.append(f"only symbols priced above {MIN_PRICE:g} are fetched")
        return {
            "stocks": len(stocks),
            "indices": len(indices),
            "total": len(stocks) + len(indices),
            "label": label,
            "source": source,
            "min_price": MIN_PRICE,
            "detail": " + ".join(parts),
        }

    def _evaluate(
        self,
        spec: StrategySpec,
        module,
        display_symbol: str,
        candles_15m: list[Any],
        candles_5m: list[Any],
        daily: list[Any],
        current: dt.datetime,
    ) -> list[tuple[str, dict, str]]:
        """Return (signal, details, note) for every pattern that fired."""
        if spec.kind == "ema_cross":
            # A fresh 10-over-20 crossing, judged on whatever bar the series
            # ends on, so the session sweep can walk the day bar by bar.
            signal, details = self.registry.evaluator_for(spec)(
                candles_15m, spec.bar_seconds
            )
            if signal == "NONE":
                return []
            return [(signal, details, signal_note(details, spec.column))]

        if spec.kind in ("ema", "ema_10_20_30"):
            source = candles_5m if spec.kind == "ema_10_20_30" else candles_15m
            if not source:
                return []
            matched, details = self.registry.evaluator_for(spec)(source)
            return [(BUY, details, spec.column)] if matched else []

        if spec.kind == "daily":
            matched, details = self.registry.evaluator_for(spec)(candles_15m, daily)
            return [(BUY, details, spec.column)] if matched else []

        if spec.kind == "second_candle":
            first = candle_at_time(candles_15m, current, 9, 15)
            second = candle_at_time(candles_15m, current, 9, 30)
            third = candle_at_time(candles_15m, current, 9, 45)
            gate = load_module(
                "dashboard_second_candle_gate",
                SCRIPTS_DIR / "SecondCandleSellGate15min.py",
            )
            results = []
            for signal, details, fallback in (
                (*module.second_candle_breakout_signal(
                    candles_15m, second, third), "Second candle"),
                (*module.bearish_reversal_signal(
                    candles_15m, first, second, third), "Bearish reversal"),
            ):
                if signal == "NONE":
                    continue
                note = signal_note(details, fallback)
                if signal_state(signal) == SELL:
                    # A second-candle sell is only shown when price is
                    # actually weak: below yesterday's high, or below ema10.
                    allowed, verdict = gate.second_candle_sell_gate(
                        candles_15m, daily, current, False)
                    if not allowed:
                        continue
                    details = {**details, **verdict}
                    # build_cell only carries the note through, so the reason
                    # the gate let this sell through has to ride on it.
                    note = f"{note} - {verdict['reason']}"
                results.append((signal, details, note))
            return results

        if spec.kind == "index_rejection":
            signal, details = module.index_rejection_signal(
                candles_15m, display_symbol
            )
            if signal == "NONE":
                return []
            return [(signal, details, signal_note(details, "Index rejection"))]

        if spec.kind == "orb_high_rejection":
            # Only the bearish branch of the ORB rejection pattern: price pokes
            # above the opening-range high and closes back below it.
            orb_range = module.calculate_orb_range(candles_15m)
            if not orb_range:
                return []
            signal, details = module.orb_rejection_signal(
                candles_15m, orb_range[0], orb_range[1]
            )
            if signal != "PE":
                return []
            return [(signal, details, signal_note(details, "ORB high rejection"))]

        if spec.kind == "orb_low_rejection":
            # The mirror of the ORB high rejection, with a bullish opening
            # candle required. The module finds the opening range from the
            # session's own 09:15 bar rather than through calculate_orb_range,
            # which reads the wall-clock date and so goes quiet on a holiday.
            signal, details = self.registry.evaluator_for(spec)(
                candles_15m, current, False
            )
            if signal == "NONE":
                return []
            return [(signal, details, "ORB low rejection")]

        if spec.kind == "double_bottom":
            # Swept with the rest of the 15-minute columns, so the cell shows
            # the newest bar of the session that fired and how many bars did.
            # The daily series supplies the previous session's low; the
            # opening-range low comes from the session's own 09:15 candle.
            signal, details = self.registry.evaluator_for(spec)(
                candles_15m, daily, current, False
            )
            if signal == "NONE":
                return []
            return [(signal, details, "Double bottom")]

        if spec.kind == "level_rejection":
            return self._evaluate_level_rejection(spec, candles_15m, daily)

        if spec.kind == "doji_rejection":
            # Delegates to the live strategy so the column and the trading
            # script can never disagree. restrict_to_today=False keeps this
            # column useful on a holiday, when the newest bars are the last
            # session's.
            signal, details = self.registry.evaluator_for(spec)(
                candles_15m, current, False
            )
            if signal == "NONE":
                return []
            trigger = str(details.get("trigger") or "doji")
            return [(signal, details, trigger.replace("_", " ").lower())]

        if spec.kind in ("higher_high", "higher_high_close"):
            # Also delegated to the live strategy script.
            signal, details = self.registry.evaluator_for(spec)(
                candles_15m, current, False
            )
            if signal == "NONE":
                return []
            trigger = str(details.get("trigger") or spec.kind)
            return [(signal, details, trigger.replace("_", " ").lower())]

        if spec.kind == "lower_high":
            # Delegated to the signal module so the column and any live caller
            # apply identical rules.
            signal, details = self.registry.evaluator_for(spec)(
                candles_15m, spec.bar_seconds
            )
            if signal == "NONE":
                return []
            return [(signal, details, signal_note(details, "Lower high"))]

        if spec.kind == "open_ema_stack":
            # Delegated to the signal module so the column and any live caller
            # apply identical rules. restrict_to_today=False keeps the column
            # useful on a holiday, when the newest bars are the last session's.
            signal, details = self.registry.evaluator_for(spec)(
                candles_15m, daily, current, False
            )
            if signal == "NONE":
                return []
            candle_label = str(details.get("candle") or "opening")
            direction = "bullish" if signal == BUY else "bearish"
            return [(signal, details,
                     f"{candle_label} candle {direction} EMA stack")]

        if spec.kind == "orb":
            results = []
            # Companions are judged first and independently. calculate_orb_range
            # only looks for *today's* 09:15 bar, so on a holiday or weekend it
            # returns None and would otherwise suppress these entirely, even
            # though they read the newest session's two opening candles.
            for module_name, file_name, evaluator_name, needs_daily in (
                ORB_COMPANION_SIGNALS
            ):
                companion = load_module(module_name, SCRIPTS_DIR / file_name)
                evaluator = getattr(companion, evaluator_name)
                signal, details = (
                    evaluator(candles_15m, daily, current, False)
                    if needs_daily
                    else evaluator(candles_15m, current, False)
                )
                if signal != "NONE":
                    results.append((
                        signal,
                        {**details, "bar_seconds": CANDLE_SECONDS},
                        signal_note(details, "Opening range"),
                    ))

            orb_range = module.calculate_orb_range(candles_15m)
            if not orb_range:
                return results
            orb_high, orb_low = orb_range
            # Each leg declares the bar size it was judged on, so the cell is
            # stamped with the close of the bar that actually produced it.
            evaluators = [
                (module.orb_breakout_signal, candles_5m, FIVE_MINUTE_SECONDS,
                 orb_high, orb_low),
                (module.orb_rejection_signal, candles_15m, CANDLE_SECONDS,
                 orb_high, orb_low),
                (module.double_top_rejection_signal, candles_15m, CANDLE_SECONDS),
                (module.double_bottom_rejection_signal, candles_15m, CANDLE_SECONDS),
                (module.ema_crossover_rsi_signal, candles_15m, CANDLE_SECONDS),
            ]
            for evaluator, candles, seconds, *args in evaluators:
                signal, details = evaluator(candles, *args)
                if signal != "NONE":
                    results.append((
                        signal, {**details, "bar_seconds": seconds},
                        signal_note(details, "ORB"),
                    ))
            return results

        raise ValueError(f"Unknown dashboard strategy kind: {spec.kind}")

    def _candles_for(self, spec: StrategySpec, candles_15m, candles_5m):
        """The bar series whose last close time is the crossover time."""
        if spec.kind == "ema_10_20_30":
            return candles_5m
        if spec.kind == "orb":
            return candles_5m or candles_15m
        return candles_15m

    def _evaluate_level_rejection(
        self,
        spec: StrategySpec,
        candles_15m: list[Any],
        daily: list[Any],
    ) -> list[tuple[str, dict, str]]:
        """Rejection or bounce at a level taken from the prior session.

        R1 is the classic pivot: P = (H + L + C) / 3 and R1 = 2P - L. The
        previous day's high is the other level. Selling needs the bar to test
        the level and close back beneath it; buying is the mirror image, where
        the level holds and price closes back above.

        The rule itself lives in EquityLevelRejectionSignal15min.py, so the
        dashboard column and the consolidated 15-minute scanner apply the same
        one. It used to be written out here, which meant a trading rule lived
        in a UI file with no second copy to catch a change.
        """
        signal, details = level_rejection_signal(
            candles_15m, daily, spec.level, None
        )
        if signal == "PE":
            return [(signal, details, f"{details['strategy'].title()} rejection")]
        if signal == "CE":
            return [(signal, details, f"{details['strategy'].title()} bounce")]
        return []

    def _sweep_session_cells(
        self,
        spec: StrategySpec,
        module,
        display_symbol: str,
        candles_15m: list[Any],
        candles_5m: list[Any],
        daily: list[Any],
        bounds: tuple[int, int],
        session_close: bool = False,
        judge_session_open: bool = False,
    ) -> dict[str, Any]:
        """Judge the strategy on every 15-minute bar of the final session.

        Every evaluator only ever inspects the last bar it is handed, which is
        exactly what the live trading scripts want. Handing it a series
        truncated to bar N therefore judges bar N, so a pattern that formed at
        10:15 is still found at 15:30 without altering the strategy's own logic
        or the meaning of its verdict. Earlier sessions are kept in each window
        so strategies needing more history keep working.

        The session's first bar is skipped unless judge_session_open is set: for
        a candle pattern its previous bar would be yesterday's close, which the
        live scripts deliberately never pair with today's open. An indicator
        strategy has no such problem, and the open bar is often exactly where it
        wants to fire.
        """
        start, stop = bounds
        first = start if (judge_session_open or start == 0) else start + 1
        indexes = list(range(first, stop))
        if not indexes:
            # A one-bar session still has to be judged, so fall back to it.
            indexes = [stop - 1]
        per_bar: list[dict[str, Any]] = []
        for index in indexes:
            window = candles_15m[:index + 1]
            at = dt.datetime.fromtimestamp(
                candles_15m[index].epoch, MARKET_TIMEZONE
            ) + dt.timedelta(seconds=CANDLE_SECONDS)
            per_bar.append(build_cell(
                spec,
                self._evaluate(spec, module, display_symbol,
                               window, candles_5m, daily, at),
                window,
                session_close,
            ))
        if not per_bar:
            return empty_cell()
        return merge_session_cells(per_bar)

    def _build_cells(
        self,
        specs: list[StrategySpec],
        modules: dict[str, Any],
        display_symbol: str,
        candles_15m: list[Any],
        candles_5m: list[Any],
        daily: list[Any],
        current: dt.datetime,
        session_close: bool = False,
        sweep_session: bool = False,
    ) -> dict[str, dict]:
        cells: dict[str, dict] = {}
        bounds = session_bar_bounds(candles_15m) if sweep_session else (0, 0)
        for spec in specs:
            try:
                if sweep_session and sweeps_session(spec):
                    cells[spec.key] = self._sweep_session_cells(
                        spec, modules[spec.key], display_symbol,
                        candles_15m, candles_5m, daily, bounds, session_close,
                        spec.judge_session_open,
                    )
                    continue
                outcomes = self._evaluate(
                    spec, modules[spec.key], display_symbol,
                    candles_15m, candles_5m, daily, current,
                )
                cells[spec.key] = build_cell(
                    spec, outcomes,
                    self._candles_for(spec, candles_15m, candles_5m),
                    session_close,
                )
            except Exception as error:
                cells[spec.key] = error_cell(error)
        return cells

    def _make_row(
        self,
        display_symbol: str,
        group: str,
        source_candles: list[Any],
        cells: dict[str, dict],
        freshness: dict[str, Any],
        base_open: float | None = None,
    ) -> dict[str, Any]:
        """Build one table row from the bar series that defines the view.

        total_gain_percent is the whole session's move, measured from the open
        of the session's first bar to the close of its last, so the figure
        means the same thing in every tab. base_open lets the caller supply
        that first open; without it the latest bar's own open is used, which is
        the correct base for a daily bar.
        """
        latest = source_candles[-1]
        open_price = base_open if base_open else float(latest.open)
        gain = (
            (float(latest.close) - open_price) / open_price * 100
            if open_price else 0.0
        )
        return {
            "symbol": display_symbol,
            "group": group,
            "close": float(latest.close),
            "total_gain_percent": round(gain, 2),
            "volume_15m": int(latest.volume),
            "latest_candle_ist": freshness["latest_candle_ist"],
            "data_age_seconds": freshness["age_seconds"],
            "signal_count": sum(
                1 for cell in cells.values() if cell["state"] in (BUY, SELL)
            ),
            "cells": cells,
        }

    @staticmethod
    def _rank(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Actionable rows first, then by liquidity so the top is useful."""
        rows.sort(
            key=lambda item: (-item["signal_count"], -item["volume_15m"], item["symbol"])
        )
        return rows

    def scan(self, selection: str = "all") -> dict[str, Any]:
        """Run one scan, always releasing the candle-store file handle."""
        store = CandleStore()
        try:
            return self._scan_with_store(selection, store)
        finally:
            store.close()

    def _scan_with_store(
        self,
        selection: str,
        store: CandleStore,
    ) -> dict[str, Any]:
        selected_specs, modules, errors = self._resolve_specs(
            self.registry.selected_specs(selection)
        )
        client = FyersClient()
        limiter = DashboardRateLimiter()
        freshness_by_symbol: dict[str, dict[str, Any]] = {}
        daily_freshness: dict[str, dict[str, Any]] = {}
        index_symbols: set[str] = set()
        intraday_rows: list[dict[str, Any]] = []
        eod_rows: list[dict[str, Any]] = []

        # The price floor is settled before any history is requested, so a
        # symbol under it costs one shared quotes call and nothing else.
        targets = self._scan_targets(selected_specs, modules)
        prices = last_traded_prices(
            client, limiter, [history for _d, history, _g in targets])
        targets, price_excluded = apply_price_floor(targets, prices)
        unpriced = [
            history for _d, history, _g in targets if history not in prices
        ]
        purged_rows = store.purge_symbols(
            [item["history_symbol"] for item in price_excluded])

        for display_symbol, history_symbol, group in targets:
            applicable = self._specs_for_group(selected_specs, group)
            # The EOD tab judges daily bars, so it gets its own narrower list.
            eod_applicable = eod_specs(applicable)
            if group == "index":
                index_symbols.add(history_symbol)
            check_time = now_ist()
            candles_15m: list[Any] = []
            candles_5m: list[Any] = []
            daily: list[Any] = []

            try:
                candles_15m = fetch_history(
                    client, limiter, history_symbol, "15", check_time,
                    days=INTRADAY_HISTORY_DAYS, seconds=CANDLE_SECONDS,
                )
            except Exception as error:
                errors.append(f"{display_symbol}: {error}")

            needs_5m = any(
                spec.bar_seconds == FIVE_MINUTE_SECONDS for spec in applicable
            )
            if not market_is_open(check_time):
                # The only 5-minute consumer is the ORB breakout, which needs a
                # bar from the current session. On a weekend or holiday none
                # exists, so fetching 5-minute history per symbol is pure waste.
                needs_5m = False
            try:
                if needs_5m:
                    candles_5m = fetch_history(
                        client, limiter, history_symbol, "5", check_time,
                        days=5, seconds=FIVE_MINUTE_SECONDS,
                    )
            except Exception as error:
                errors.append(f"{display_symbol}: {error}")

            # The EOD tab is built from daily bars, so always fetch them.
            try:
                daily = fetch_history(
                    client, limiter, history_symbol, "D", check_time,
                    days=DAILY_HISTORY_DAYS, seconds=0, daily=True,
                )
            except Exception as error:
                errors.append(f"{display_symbol}: {error}")

            if not candles_15m and not daily:
                errors.append(f"{display_symbol}: no candle data returned")
                continue

            # Persist what was just downloaded. This reuses the candles already
            # in hand, so it costs no extra API calls.
            store.store(history_symbol, "15", candles_15m)
            store.store(history_symbol, "5", candles_5m)
            store.store(history_symbol, "D", daily)

            if candles_15m:
                freshness = candle_freshness(candles_15m, check_time)
                freshness_by_symbol[history_symbol] = freshness
                # sweep_session judges every 15-minute bar of the final session
                # rather than the newest bar alone, so a pattern that formed
                # hours ago is still listed at the close.
                cells = self._build_cells(
                    applicable, modules, display_symbol,
                    candles_15m, candles_5m, daily, check_time,
                    sweep_session=True,
                )
                intraday_rows.append(
                    self._make_row(
                        display_symbol, group, candles_15m, cells, freshness,
                        base_open=session_open(candles_15m),
                    )
                )

            if daily:
                eod_freshness = daily_freshness_item(daily)
                daily_freshness[history_symbol] = eod_freshness
                # The EOD tab is judged on daily bars, so only the strategies
                # that read a daily bar are run. Session-shaped rules are left
                # out rather than handed a daily series to misread.
                eod_cells = self._build_cells(
                    eod_applicable, modules, display_symbol,
                    daily, daily, daily, check_time, session_close=True,
                )
                eod_rows.append(
                    self._make_row(display_symbol, group, daily, eod_cells, eod_freshness)
                )

        # The indices live in their own tab, so the stock tabs list stocks only.
        stock_15m_rows = [r for r in intraday_rows if r["group"] != "index"]
        index_15m_rows = [r for r in intraday_rows if r["group"] == "index"]
        stock_eod_rows = [r for r in eod_rows if r["group"] != "index"]

        total_symbols = len(freshness_by_symbol)
        fresh_symbols = sum(
            1 for item in freshness_by_symbol.values() if item["latest"]
        )
        all_latest = bool(total_symbols) and fresh_symbols == total_symbols
        intraday_state = describe_data_state(freshness_by_symbol, all_latest, now_ist())
        eod_state = describe_eod_state(
            daily_freshness,
            now_ist(),
            intraday_only=tuple(
                spec.column for spec in selected_specs if spec.intraday_only
            ),
        )

        # The indices tab judges the indices alone, so its freshness message
        # must count the indices alone too.
        index_freshness = {
            symbol: item
            for symbol, item in freshness_by_symbol.items()
            if symbol in index_symbols
        }
        index_total = len(index_freshness)
        index_current = sum(
            1 for item in index_freshness.values() if item["latest"]
        )
        index_latest = bool(index_total) and index_current == index_total
        index_state = describe_data_state(
            index_freshness, index_latest, now_ist())

        stock_15m_rows = self._rank(stock_15m_rows)
        index_15m_rows = self._rank(index_15m_rows)
        eod_rows = self._rank(stock_eod_rows)
        storage = store.summary()
        store.prune()

        columns = [
            {"key": spec.key, "column": spec.column, "label": spec.label}
            for spec in selected_specs
        ]
        rejection_keys = {spec.key for spec in selected_specs if spec.sell_only}
        # Daily breakout is evaluated on daily bars, so it earns no column in a
        # 15-minute view. Index-only strategies are hidden from the stock tabs
        # and return in the indices tab, which is where they belong.
        intraday_keys = {
            spec.key for spec in selected_specs if not spec.hide_in_intraday
        }
        # The EOD tab is judged on daily bars, so the session-shaped strategies
        # (the three ORB columns, the open-candle stack, the second candle) are
        # dropped there rather than shown as an empty or misleading column.
        # Derived from the same helper the scan uses, so the headers and the
        # evaluation can never disagree about which columns belong.
        eod_keys = {spec.key for spec in eod_specs(selected_specs)}
        index_keys = intraday_keys | {
            spec.key for spec in selected_specs
            if spec.universe == "index_config"
        }

        return {
            "selection": selection,
            "selection_label": (
                "All strategies" if selection == "all" else selected_specs[0].label
            ),
            "scanned_at_ist": now_ist().isoformat(timespec="seconds"),
            "universe": self.describe_universe(selection),
            "price_filter": {
                "min_price": MIN_PRICE,
                "excluded": price_excluded,
                "excluded_count": len(price_excluded),
                "unpriced": unpriced,
                "unpriced_count": len(unpriced),
                "purged_rows": purged_rows,
            },
            "storage": storage,
            "stored_this_scan": store.stored_rows,
            "fresh_symbols": fresh_symbols,
            "total_symbols": total_symbols,
            "columns": columns,
            "views": {
                "intraday": build_view(
                    "intraday",
                    "Intraday 15-minute",
                    stock_15m_rows,
                    intraday_state,
                    all_latest,
                    fresh_symbols,
                    total_symbols,
                    columns,
                    column_keys=intraday_keys,
                ),
                "eod": build_view(
                    "eod",
                    "End of day",
                    eod_rows,
                    eod_state,
                    True,
                    total_symbols,
                    total_symbols,
                    columns,
                    column_keys=eod_keys,
                ),
                "rejections": build_view(
                    "rejections",
                    "Rejections (sell)",
                    stock_15m_rows,
                    intraday_state,
                    all_latest,
                    fresh_symbols,
                    total_symbols,
                    columns,
                    sell_only=True,
                    column_keys=rejection_keys,
                ),
                "indices": build_view(
                    "indices",
                    "Indices 15-minute",
                    index_15m_rows,
                    index_state,
                    index_latest,
                    index_current,
                    index_total,
                    columns,
                    column_keys=index_keys,
                ),
            },
            "errors": errors[:20],
        }


class ScanManager:
    """Run one dashboard scan in a background job and expose its status."""

    def __init__(self, scanner: DashboardScanner):
        self.scanner = scanner
        self._lock = threading.Lock()
        self._jobs: dict[str, dict[str, Any]] = {}

    def start(self, selection: str) -> tuple[str, bool]:
        with self._lock:
            for job_id, job in self._jobs.items():
                if job["status"] == "running":
                    return job_id, False
            job_id = f"scan-{int(time.time() * 1000)}"
            self._jobs[job_id] = {
                "status": "running",
                "selection": selection,
                "started_at": now_ist().isoformat(timespec="seconds"),
                "result": None,
                "error": None,
            }
        thread = threading.Thread(
            target=self._run,
            args=(job_id, selection),
            daemon=True,
            name=f"dashboard-{job_id}",
        )
        thread.start()
        return job_id, True

    def _run(self, job_id: str, selection: str) -> None:
        try:
            result = self.scanner.scan(selection)
            with self._lock:
                self._jobs[job_id].update(
                    status="complete",
                    result=result,
                    finished_at=now_ist().isoformat(timespec="seconds"),
                )
        except Exception as error:
            with self._lock:
                self._jobs[job_id].update(
                    status="error",
                    error=str(error),
                    traceback=traceback.format_exc(),
                    finished_at=now_ist().isoformat(timespec="seconds"),
                )

    def get(self, job_id: str) -> dict[str, Any] | None:
        with self._lock:
            job = self._jobs.get(job_id)
            return dict(job) if job else None

    def latest_complete(self) -> dict[str, Any] | None:
        """Most recent finished scan, so a reloaded page can reuse it.

        Results are held in memory only. Without this a page reload could not
        recover the previous scan and had to wait out a full rescan.
        """
        with self._lock:
            finished = [
                job for job in self._jobs.values()
                if job.get("status") == "complete" and job.get("result")
            ]
            if not finished:
                return None
            newest = max(finished, key=lambda job: job.get("finished_at") or "")
            return dict(newest)


INDEX_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>FYERS Multi-Strategy Dashboard</title>
<style>
:root {
  color-scheme: dark;
  font-family: Inter, "Segoe UI", system-ui, -apple-system, sans-serif;
  --bg: #070b14;
  --surface: #0e1524;
  --surface-2: #131c2e;
  --line: #1f2a40;
  --line-soft: #182236;
  --text: #e8edf7;
  --muted: #8494ad;
  --muted-2: #64748b;
  --accent: #4f8cff;
  --buy: #22c55e;
  --buy-bg: rgba(34, 197, 94, .14);
  --sell: #f43f5e;
  --sell-bg: rgba(244, 63, 94, .14);
  --when: #ffffff;
  /* Column header text: pure white, at 17:1 against --surface-2. There is no
     hover shade, because white is already the top of the range - the hover
     affordance is carried by the background shift instead. The active sort
     keeps the blue accent so it is still tellable apart from the rest. */
  --header: #ffffff;
}
* { box-sizing: border-box; }
body {
  margin: 0;
  display: flex;
  flex-direction: column;
  min-height: 100vh;
  background: radial-gradient(1200px 600px at 50% -10%, #16223c 0%, var(--bg) 60%);
  background-attachment: fixed;
  color: var(--text);
  font-size: 14px;
  -webkit-font-smoothing: antialiased;
}

/* ---------- centred search header ---------- */
.appbar {
  position: sticky; top: 0; z-index: 30;
  padding: 18px 24px 14px;
  background: rgba(7, 11, 20, .88);
  backdrop-filter: blur(14px);
  border-bottom: 1px solid var(--line);
}
/* The title is now a slim inline label in the toolbar; the search box has
   moved into the Stock column header, so .searchwrap must size to that cell
   rather than to the page. */
.appbar-title { margin: 0 6px 0 0; font-size: 12px; font-weight: 650; letter-spacing: .18em; text-transform: uppercase; color: var(--muted); }
.searchwrap { position: relative; display: block; }
.searchwrap svg { position: absolute; left: 16px; top: 50%; transform: translateY(-50%); width: 17px; height: 17px; color: var(--muted-2); pointer-events: none; }
#search {
  width: 100%; padding: 13px 44px 13px 44px;
  font: inherit; font-size: 15px; color: var(--text);
  background: var(--surface); border: 1px solid var(--line);
  border-radius: 12px; outline: none;
  transition: border-color .15s, box-shadow .15s;
}
#search::placeholder { color: var(--muted-2); }
#search:focus { border-color: var(--accent); box-shadow: 0 0 0 3px rgba(79, 140, 255, .16); }
/* Inside the table header the box is compact and must not inherit the
   uppercase, wide-tracked styling that header cells use for their labels. */
thead th .col-label { display: flex; align-items: baseline; gap: 6px; margin-bottom: 3px; }
/* How many symbols the active tab lists, beside the Stock label. It is the
   server-side row count for the tab, so it reports the whole universe the
   price floor left behind and is deliberately unaffected by the search box. */
thead th .col-count {
  font-size: 10px; font-weight: 650; letter-spacing: 0;
  color: var(--header); background: var(--surface);
  border: 1px solid var(--line); border-radius: 999px;
  padding: 0 6px; line-height: 15px; cursor: help;
}
thead th .searchslot { display: block; }
thead th #search {
  padding: 5px 20px 5px 21px; font-size: 11.5px; line-height: 1.4;
  background: var(--bg); border-radius: 7px;
  text-transform: none; letter-spacing: normal;
}
thead th #search::placeholder { font-size: 11px; }
thead th #search:focus { box-shadow: 0 0 0 2px rgba(79, 140, 255, .18); }
thead th .searchwrap svg { left: 6px; width: 12px; height: 12px; }
thead th #clearSearch { right: 3px; width: 17px; height: 17px; font-size: 12px; border-radius: 5px; }
#clearSearch {
  position: absolute; right: 8px; top: 50%; transform: translateY(-50%);
  display: none; width: 26px; height: 26px; border: 0; border-radius: 7px;
  background: var(--surface-2); color: var(--muted); font-size: 15px;
  line-height: 1; cursor: pointer;
}
#clearSearch:hover { color: var(--text); }
#clearSearch.show { display: block; }

.toolbar { max-width: 1440px; margin: 14px auto 0; display: flex; gap: 10px; align-items: center; justify-content: center; flex-wrap: wrap; }
.chipset { display: flex; gap: 6px; padding: 4px; background: var(--surface); border: 1px solid var(--line); border-radius: 10px; }
.chipset button {
  font: inherit; font-size: 12.5px; font-weight: 600; letter-spacing: .02em;
  padding: 6px 12px; border: 0; border-radius: 7px;
  background: transparent; color: var(--muted); cursor: pointer;
}
.chipset button:hover { color: var(--text); }
.chipset button.active { background: var(--accent); color: #fff; }
.tabrow { max-width: 1440px; margin: 16px auto 0; display: flex; justify-content: center; }
.tabs { display: inline-flex; gap: 6px; padding: 5px; background: var(--surface-2); border: 1px solid var(--line); border-radius: 12px; }
.tabs button {
  font: inherit; font-size: 14px; font-weight: 650; letter-spacing: .01em;
  padding: 10px 26px; border: 0; border-radius: 9px; cursor: pointer;
  background: transparent; color: var(--muted); white-space: nowrap;
  transition: background .12s, color .12s;
}
.tabs button:hover { color: var(--text); }
.tabs button.active { background: var(--accent); color: #fff; box-shadow: 0 1px 6px rgba(79, 140, 255, .35); }
.tabs button .pill { display: inline-block; margin-left: 7px; padding: 1px 7px; border-radius: 20px; font-size: 10.5px; font-weight: 700; background: rgba(255, 255, 255, .16); }
.tabs button:not(.active) .pill { background: var(--buy-bg); color: #4ade80; }
.tabs button.active .pill { background: rgba(0, 0, 0, .22); color: #fff; }
button.primary {
  font: inherit; font-size: 13px; font-weight: 650; cursor: pointer;
  padding: 10px 18px; border: 0; border-radius: 10px;
  background: var(--accent); color: #fff;
}
button.primary:hover:not(:disabled) { filter: brightness(1.08); }
button.primary:disabled { opacity: .5; cursor: wait; }
.hint { color: var(--muted-2); font-size: 12px; }

/* ---------- status + stats ---------- */
/* Two columns: the table, and a rail of stat tiles stacked down the right.
   min-height: 0 lets the table shrink inside the flex body instead of being
   pushed off screen. */
main {
  flex: 1 1 auto; min-height: 0; width: 100%; max-width: 1600px; margin: 0 auto; padding: 16px 24px 24px;
  display: grid; grid-template-columns: minmax(0, 1fr) 188px; gap: 16px; align-items: start;
}
.content { display: flex; flex-direction: column; min-height: 0; }
.rail { min-width: 0; }
/* The market-state line sits above everything else on the page, so it is the
   first thing read. It carries transient scan messages too, so it stays a
   single bar rather than a block of prose. */
.status {
  display: flex; align-items: center; gap: 9px; flex: none;
  margin: 10px 18px 0; padding: 11px 14px;
  border-radius: 10px; background: #12203a; color: #bfdbfe;
  border: 1px solid var(--line);
}
.status .dot { width: 8px; height: 8px; border-radius: 50%; background: currentColor; flex: none; }
.status.good { background: rgba(34, 197, 94, .1); color: #86efac; }
.status.bad { background: rgba(244, 63, 94, .1); color: #fda4af; }
.stats { display: flex; flex-direction: column; gap: 8px; margin: 0; }
.stat { background: var(--surface); border: 1px solid var(--line); border-radius: 10px; padding: 12px 14px; }
.stat .k { font-size: 11px; text-transform: uppercase; letter-spacing: .08em; color: var(--muted-2); }
.stat .v { font-size: 20px; font-weight: 650; margin-top: 4px; font-variant-numeric: tabular-nums; }
.stat .v.buy { color: var(--buy); }
.stat .v.sell { color: var(--sell); }
.stat .v.closed { color: var(--muted); }
.stat .v.live { color: var(--buy); }

/* ---------- table ---------- */
/* Fixed layout with explicit widths on the four data columns: the signal
   columns share the remainder equally, so the table can never overflow
   horizontally no matter how many strategy columns are added. */
.tablewrap { flex: 1 1 auto; min-height: 260px; overflow-y: auto; overflow-x: hidden; border: 1px solid var(--line); border-radius: 12px; background: var(--surface); }
table { width: 100%; table-layout: fixed; border-collapse: separate; border-spacing: 0; font-size: 13px; }
thead th {
  position: sticky; top: 0; z-index: 5;
  background: var(--surface-2); color: var(--header);
  font-size: 10px; font-weight: 650; text-transform: uppercase; letter-spacing: .04em;
  text-align: center; padding: 9px 4px; white-space: normal; line-height: 1.25;
  border-bottom: 1px solid var(--line);
  overflow-wrap: break-word; hyphens: auto;
}
/* Width is fitted to the longest symbol on screen by fitStockColumn(); this is
   only the fallback before the first render. The search box shares this cell,
   which is what sets the floor - see STOCK_COL_MIN. */
thead th.col-stock { width: 11%; padding: 5px 6px 6px 10px; }
thead th.col-price { width: 7%; }
thead th.col-chg { width: 7%; }
thead th.col-vol { width: 9%; }
thead th.left { text-align: left; padding-left: 10px; }
/* A sortable header behaves like a button: it says so on hover, on focus and
   while it is the active sort. */
thead th.sortable { cursor: pointer; user-select: none; }
/* The header is already pure white, so a hover cannot brighten the text any
   further. The background shift carries the affordance on its own. */
thead th.sortable:hover { background: var(--surface); }
thead th.sortable:focus-visible { outline: 2px solid var(--accent); outline-offset: -2px; }
thead th.sortable.sorted { color: var(--accent); }
thead th .arrow { display: block; font-size: 11px; line-height: 1; }
thead th .unit { display: block; margin-top: 2px; font-size: 9px; letter-spacing: .03em; color: var(--muted-2); text-transform: none; }
tbody td { padding: 7px 4px; border-bottom: 1px solid var(--line-soft); text-align: center; vertical-align: middle; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
tbody td.left { text-align: left; padding-left: 10px; }
tbody tr:hover td { background: rgba(79, 140, 255, .07); }
tbody tr.has-signal td:first-child { box-shadow: inset 3px 0 0 var(--accent); }
.num { font-variant-numeric: tabular-nums; }
.sym { font-weight: 650; letter-spacing: .01em; font-size: 12.5px; }
.tag { display: block; margin-top: 2px; font-size: 10px; color: var(--muted-2); text-transform: uppercase; letter-spacing: .06em; }
.up { color: var(--buy); }
.down { color: var(--sell); }
.sig .badge {
  display: inline-block; min-width: 40px; padding: 2px 6px;
  border-radius: 5px; font-size: 10.5px; font-weight: 700; letter-spacing: .04em;
}
.sig .badge.buy { background: var(--buy-bg); color: #4ade80; box-shadow: inset 0 0 0 1px rgba(34, 197, 94, .38); }
.sig .badge.sell { background: var(--sell-bg); color: #fb7185; box-shadow: inset 0 0 0 1px rgba(244, 63, 94, .38); }
.sig .when { display: block; margin-top: 2px; font-size: 10.5px; color: var(--when); font-variant-numeric: tabular-nums; }
.sig.na { color: var(--muted-2); }
/* A signal that fired earlier in the session but is not on the newest bar:
   the pattern has stopped forming, so it is dimmed to say so at a glance. */
.sig.earlier .badge { opacity: .55; box-shadow: none; }
.sig.earlier .when { color: rgba(255, 255, 255, .42); }
/* "+n" beside the badge counts the other times it fired today. */
.sig .more {
  display: inline-block; margin-left: 3px; padding: 0 4px; border-radius: 20px;
  font-size: 9px; font-weight: 700; vertical-align: 2px;
  background: var(--surface-2); color: var(--muted);
}
.group-sep td { background: var(--surface-2); color: var(--muted); font-size: 10.5px; font-weight: 650; text-transform: uppercase; letter-spacing: .1em; text-align: left; padding: 7px 12px; }
.empty { padding: 46px 20px; text-align: center; color: var(--muted); }
.errors { margin-top: 14px; color: #fda4af; font-size: 12px; }
.errors summary { cursor: pointer; color: var(--muted); }
.errors pre { margin: 8px 0 0; white-space: pre-wrap; font-size: 11.5px; }
/* An empty result set is drawn as a table row rather than replacing the table,
   so the header cell holding the search box is never taken away. */
tbody tr.empty-row td { padding: 44px 18px; text-align: center; color: var(--muted); font-size: 13px; }
/* Narrow screens: the rail unpins and the tiles run in a row above the table. */
@media (max-width: 1000px) {
  main { grid-template-columns: minmax(0, 1fr); }
  .stats { flex-direction: row; flex-wrap: wrap; }
  .stat { flex: 1 1 150px; }
  .tablewrap { min-height: 340px; }
}
</style>
</head>
<body>

<header class="appbar">
  <div class="tabrow">
    <div class="tabs" id="tabs">
      <button data-view="intraday" class="active">Intraday 15-min</button>
      <button data-view="eod">End of Day</button>
      <button data-view="rejections">Rejections (sell)</button>
      <button data-view="indices">Indices</button>
    </div>
  </div>
  <div class="toolbar">
    <h1 class="appbar-title">FYERS Strategy Scanner</h1>
    <div class="chipset" id="filters">
      <button data-filter="all" class="active">All</button>
      <button data-filter="signal">With signals</button>
      <button data-filter="buy">Buy only</button>
      <button data-filter="sell">Sell only</button>
    </div>
    <button class="primary" id="refresh">Refresh 15-minute data</button>
    <span class="hint">Read-only &mdash; no orders are placed. Press <b>/</b> to search.</span>
  </div>
</header>

<div class="status" id="status"><span class="dot"></span><span id="statusText">Starting the first scan&hellip;</span></div>

<main>
  <div class="content">
    <div class="tablewrap" id="tablewrap"><div class="empty">Starting the first scan&hellip;</div></div>
    <details class="errors" id="errors" hidden></details>
  </div>
  <aside class="rail">
    <div class="stats">
      <div class="stat"><div class="k">Market</div><div class="v" id="statMarket" style="font-size:15px">Closed</div></div>
      <div class="stat"><div class="k">Data as of</div><div class="v" id="statAsOf" style="font-size:14px">--</div></div>
      <div class="stat"><div class="k">Stocks scanned</div><div class="v" id="statScanned">0</div></div>
      <div class="stat"><div class="k">With signals</div><div class="v" id="statSignals">0</div></div>
      <div class="stat"><div class="k">Buy signals</div><div class="v buy" id="statBuy">0</div></div>
      <div class="stat"><div class="k">Sell signals</div><div class="v sell" id="statSell">0</div></div>
    </div>
  </aside>
</main>

<!-- The search box is mounted into the Stock column header by render(). It
     waits here, hidden, only so the markup is defined once. -->
<div id="searchHost" hidden>
  <div class="searchwrap" id="searchwrap">
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/></svg>
    <input id="search" type="search" autocomplete="off" spellcheck="false" placeholder="Filter">
    <button id="clearSearch" title="Clear search" aria-label="Clear search">&times;</button>
  </div>
</div>

<script>
const searchInput = document.getElementById('search');
const clearButton = document.getElementById('clearSearch');
/* The search box lives in the Stock column header, but render() rebuilds the
   whole table on every keystroke. This node is therefore moved into each new
   header rather than re-created, so the typed text and the caret survive. */
const searchWrap = document.getElementById('searchwrap');
const refreshButton = document.getElementById('refresh');
const statusBox = document.getElementById('status');
const statusText = document.getElementById('statusText');
const tableWrap = document.getElementById('tablewrap');
const errorsBox = document.getElementById('errors');
const filtersBox = document.getElementById('filters');
const tabsBox = document.getElementById('tabs');

const REFRESH_COOLDOWN_MS = 5 * 60 * 1000;
const REFRESH_ALLOWED_KEY = 'equityDashboardRefreshAllowedAt';
const REFRESH_LABEL = 'Refresh 15-minute data';

let pollTimer = null;
let cooldownTimer = null;
let activeJobId = null;
let pollFailures = 0;
let signalFilter = 'all';
let activeView = 'intraday';
let lastResult = null;
/* null keeps the server's ranking, which puts actionable rows first. An
   explicit sort replaces it, and survives re-renders, tab switches and
   searches because it lives here rather than in the rebuilt table. */
let sortColumn = null;
let sortDirection = 'desc';
let lastUniverse = null;
const resultCache = new Map();

/** Human sentence naming the exact symbol universe being scanned. */
function universeSummary() {
  const u = lastUniverse;
  if (!u || !u.total) return 'the universe';
  const parts = [u.stocks + ' ' + u.label + ' stocks'];
  if (u.indices) parts.push(u.indices + ' indices');
  return parts.join(' + ');
}
function esc(value) {
  return String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}
function setStatus(message, kind) {
  statusText.textContent = message;
  statusBox.className = 'status' + (kind ? ' ' + kind : '');
}
function formatRemaining(ms) {
  const seconds = Math.max(0, Math.ceil(ms / 1000));
  return Math.floor(seconds / 60) + ':' + String(seconds % 60).padStart(2, '0');
}
function clockTime(iso) {
  const stamp = Date.parse(iso || '');
  if (!Number.isFinite(stamp)) return '--:--';
  return new Date(stamp).toLocaleTimeString('en-GB', {hour: '2-digit', minute: '2-digit'});
}

/* ---------- refresh cooldown ---------- */
function applyCooldown(allowedAt) {
  if (cooldownTimer) clearTimeout(cooldownTimer);
  const remaining = Number(allowedAt) - Date.now();
  if (remaining > 0) {
    refreshButton.disabled = true;
    refreshButton.textContent = 'Refresh available in ' + formatRemaining(remaining);
    cooldownTimer = setTimeout(() => applyCooldown(allowedAt), Math.min(remaining + 50, 60000));
  } else {
    localStorage.removeItem(REFRESH_ALLOWED_KEY);
    refreshButton.disabled = false;
    refreshButton.textContent = REFRESH_LABEL;
  }
}
function beginCooldown() {
  const allowedAt = Date.now() + REFRESH_COOLDOWN_MS;
  localStorage.setItem(REFRESH_ALLOWED_KEY, String(allowedAt));
  applyCooldown(allowedAt);
}
function restoreCooldown() {
  const allowedAt = Number(localStorage.getItem(REFRESH_ALLOWED_KEY) || 0);
  if (allowedAt > Date.now()) applyCooldown(allowedAt);
}

/* ---------- symbol display ---------- */
/* A FYERS symbol carries its exchange as a prefix: NSE:HDFCBANK-EQ. The prefix
   is needed to fetch history, but in a table where every stock is NSE anyway it
   is noise, and it was the widest thing in the column. Stripped for display
   only - the row keeps the full symbol, so search still matches either form
   (typing "HDFC" or "NSE:HDFC" both work) and sorting still orders by the real
   symbol rather than the tidied one. */
const EXCHANGE_PREFIX = /^[A-Z]{3}:/;
function displaySymbol(symbol) {
  return String(symbol ?? '').replace(EXCHANGE_PREFIX, '');
}

/* Size the Stock column to the longest symbol actually on screen, so a tab of
   three indices does not carry a column sized for a hundred long stock names.
   Measured over the whole tab rather than the visible rows, so the column does
   not jump about while the search box is being typed into.

   The search box shares this cell, and that sets the floor: it needs room for
   its own 41px of padding plus enough text to read, so below about 150px it
   stops being worth using and the column would be actively worse for being
   tight. */
const STOCK_COL_MIN = 150;
const STOCK_COL_MAX = 240;
function fitStockColumn(rows) {
  const head = tableWrap.querySelector('th.col-stock');
  if (!head) return;
  let longest = 0;
  (rows || []).forEach(row => {
    longest = Math.max(longest, displaySymbol(row.symbol).length);
  });
  // 7.6px per character at the .sym size, plus the cell's own padding.
  const wanted = Math.round(longest * 7.6) + 22;
  head.style.width = Math.min(STOCK_COL_MAX, Math.max(STOCK_COL_MIN, wanted)) + 'px';
}

/* ---------- row filtering ---------- */
function rowStates(row) {
  const states = Object.values(row.cells || {})
    .filter(cell => cell && typeof cell === 'object')
    .map(cell => cell.state);
  return {
    any: states.some(s => s === 'BUY' || s === 'SELL'),
    buy: states.includes('BUY'),
    sell: states.includes('SELL')
  };
}
/* How a sortable column reads its value off a row. */
const SORT_FIELDS = {
  volume: row => Number(row.volume_15m || 0),
  symbol: row => String(row.symbol || ''),
  gain: row => Number(row.total_gain_percent || 0)
};
function sortRows(rows) {
  if (!sortColumn || !SORT_FIELDS[sortColumn]) return rows;
  const read = SORT_FIELDS[sortColumn];
  const sign = sortDirection === 'asc' ? 1 : -1;
  // A copy: view.rows belongs to the payload and must not be reordered.
  return rows.slice().sort((a, b) => {
    const left = read(a);
    const right = read(b);
    if (left < right) return -1 * sign;
    if (left > right) return 1 * sign;
    // Stable and predictable when the values tie.
    return String(a.symbol).localeCompare(String(b.symbol));
  });
}
function visibleRows(view) {
  const needle = searchInput.value.trim().toLowerCase();
  const matched = (view.rows || []).filter(row => {
    if (needle && !row.symbol.toLowerCase().includes(needle)) return false;
    const states = rowStates(row);
    if (signalFilter === 'signal') return states.any;
    if (signalFilter === 'buy') return states.buy;
    if (signalFilter === 'sell') return states.sell;
    return true;
  });
  return sortRows(matched);
}
/* Called from the header click handler. Re-clicking the active column flips
   the direction; choosing another column starts on the useful end. */
function toggleSort(column) {
  if (!SORT_FIELDS[column]) return;
  if (sortColumn === column) {
    sortDirection = sortDirection === 'desc' ? 'asc' : 'desc';
  } else {
    sortColumn = column;
    sortDirection = column === 'symbol' ? 'asc' : 'desc';
  }
  rerender();
}
function sortHeaderLabel(cssClass, key, label, unit) {
  const active = sortColumn === key;
  // The arrow is an HTML entity, so it must go in raw: passing it through esc()
  // would turn the ampersand into &amp; and print the entity as literal text.
  const arrow = active ? (sortDirection === 'asc' ? '&uarr;' : '&darr;') : '';
  return '<th class="' + cssClass + ' sortable' + (active ? ' sorted' : '') + '"' +
    ' data-sort="' + key + '"' +
    ' title="Sort by ' + label.toLowerCase() + '"' +
    ' aria-sort="' + (active
      ? (sortDirection === 'asc' ? 'ascending' : 'descending')
      : 'none') + '"' +
    ' tabindex="0" role="button">' + esc(label) +
    (arrow ? '<span class="arrow">' + arrow + '</span>' : '') +
    '<span class="unit">' + unit + '</span></th>';
}

/* ---------- rendering ---------- */
function cellHtml(row, column, view) {
  const cell = (row.cells || {})[column.key];
  if (!cell || typeof cell !== 'object') {
    return '<td class="sig na" title="Not applicable for this symbol">n/a</td>';
  }
  if (cell.state === 'error') {
    return '<td class="sig na" title="' + esc(cell.note) + '">err</td>';
  }
  if (cell.state !== 'BUY' && cell.state !== 'SELL') {
    return '<td class="sig na">—</td>';
  }
  const side = cell.state === 'BUY' ? 'buy' : 'sell';
  const parts = [cell.note || column.label];
  if (cell.price) parts.push('price ' + cell.price);
  if (cell.time_ist) parts.push('crossover at ' + cell.time_ist + ' IST');
  if (view.as_of_label) parts.push('session ' + view.as_of_label);
  // Every 15-minute bar of the session is judged, so say how often it fired
  // and at which times, not just the most recent one.
  const times = cell.times_ist || [];
  if (times.length > 1) {
    parts.push('fired ' + times.length + ' times today at ' + times.join(', '));
  } else if (times.length === 1) {
    parts.push('fired once today');
  }
  // Not on the newest bar means the pattern has already stopped forming, so
  // mark it rather than let it read as a signal that is still live.
  const earlier = cell.on_latest_bar === false;
  if (earlier) parts.push('earlier today - no longer forming on the latest bar');
  const more = times.length > 1
    ? '<span class="more">+' + (times.length - 1) + '</span>' : '';
  return '<td class="sig' + (earlier ? ' earlier' : '') + '" title="' + esc(parts.join(' · ')) + '">' +
    '<span class="badge ' + side + '">' + cell.state + '</span>' + more +
    '<span class="when">' + esc(cell.time_ist || '--:--') + '</span></td>';
}
function render(result) {
  lastResult = result;
  resultCache.set(result.selection, result);

  const views = result.views || {};
  const view = views[activeView];
  // The universe is still needed for the wording shown when a search matches
  // nothing, but it is no longer printed as a block of its own.
  if (result.universe) lastUniverse = result.universe;
  if (!view) {
    // Surface the problem instead of showing a silently blank table.
    tableWrap.innerHTML = '<div class="empty">No <b>' + esc(activeView) +
      '</b> data in this scan response. Available views: ' +
      esc(Object.keys(views).join(', ') || 'none') + '. Please refresh.</div>';
    setStatus('Scan response has no "' + activeView + '" view.', 'bad');
    return;
  }
  const hasErrors = (result.errors || []).length > 0;
  setStatus(
    view.latest_message + (hasErrors ? ' (' + result.errors.length + ' symbol errors)' : ''),
    (view.latest_15min || view.market_open === false) && !hasErrors ? 'good' : 'bad'
  );

  errorsBox.hidden = !hasErrors;
  if (hasErrors) {
    errorsBox.innerHTML = '<summary>Scan errors</summary><pre>' +
      result.errors.map(esc).join('\n') + '</pre>';
  }

  const columns = view.columns || result.columns || [];
  const rows = visibleRows(view);

  let buys = 0, sells = 0;
  (view.rows || []).forEach(row => {
    const states = rowStates(row);
    if (states.buy) buys += 1;
    if (states.sell) sells += 1;
  });
  document.getElementById('statScanned').textContent = view.total_rows || 0;
  document.getElementById('statSignals').textContent = view.signal_rows || 0;
  document.getElementById('statBuy').textContent = buys;
  document.getElementById('statSell').textContent = sells;

  // Signal counts per tab, so both tabs can be judged before switching.
  tabsBox.querySelectorAll('button[data-view]').forEach(button => {
    const target = (views[button.dataset.view] || {}).signal_rows || 0;
    let pill = button.querySelector('.pill');
    if (!pill) {
      pill = document.createElement('span');
      pill.className = 'pill';
      button.appendChild(pill);
    }
    pill.textContent = target;
    pill.style.display = target ? '' : 'none';
  });

  // Market state: make it obvious when rows are a previous session's close.
  const marketBox = document.getElementById('statMarket');
  const asOfBox = document.getElementById('statAsOf');
  const eodActive = activeView === 'eod';
  const sellOnly = view.sell_only === true;
  marketBox.textContent = sellOnly ? 'SELL' : (eodActive ? 'EOD' : (view.market_open ? 'Open' : 'Closed'));
  marketBox.className = 'v ' + (view.market_open && !eodActive && !sellOnly ? 'live' : 'closed');
  asOfBox.textContent = view.as_of_label || '--';

  const priceUnit = eodActive ? 'day close' : '15m close';
  const volUnit = eodActive ? 'day shares' : '15m shares';

  /* Row count for the active tab, beside the Stock label. Read from the
     server's total_rows rather than the rendered rows, so the figure is the
     tab's whole count and does not shrink as the search box is typed into. */
  const rowCount = Number(view.total_rows || 0);
  const rowNoun = activeView === 'indices'
    ? (rowCount === 1 ? 'index' : 'indices')
    : (rowCount === 1 ? 'stock' : 'stocks');
  const countHtml = '<span class="col-count" title="' +
    rowCount.toLocaleString('en-IN') + ' ' + rowNoun +
    ' listed in this tab">' + rowCount.toLocaleString('en-IN') + '</span>';

  /* This function replaces the table wholesale, which would tear the search
     box out from under the caret on every keystroke. Remember the focus and
     caret offset so both can be restored once the new header exists. */
  const hadFocus = document.activeElement === searchInput;
  const caret = hadFocus ? searchInput.selectionStart : null;

  let html = '<table><thead><tr>' +
    '<th class="left col-stock"><span class="col-label">Stock' + countHtml +
    '</span><span class="searchslot"></span></th>' +
    '<th class="col-price">Price<span class="unit">' + priceUnit + '</span></th>' +
    sortHeaderLabel('col-chg', 'gain', 'gain%', 'day open&rarr;close') +
    sortHeaderLabel('col-vol', 'volume', 'Volume', volUnit) +
    columns.map(c => '<th class="sig-col" title="' + esc(c.label) + '">' +
      esc(c.column) + '</th>').join('') +
    '</tr></thead><tbody>';

  if (!rows.length) {
    /* The header is drawn even with nothing to list, so the search box stays
       reachable. Replacing the table with a bare message div would let a
       search that matches no row delete the only control able to clear it. */
    const message = !(view.rows || []).length
      ? (activeView === 'eod'
          ? 'No end-of-day data was returned for this scan. Click Refresh to try again.'
          : 'No 15-minute data was returned for this scan. Click Refresh to try again.')
      : 'No stock matches the current search and filter.';
    html += '<tr class="empty-row"><td colspan="' + (4 + columns.length) + '">' +
      esc(message) + '</td></tr>';
  } else {
    let lastGroup = null;
    rows.forEach(row => {
      // A tab that holds one kind of row needs no band above it.
      if (row.group !== lastGroup && view.single_group !== true) {
        html += '<tr class="group-sep"><td colspan="' + (4 + columns.length) + '">' +
          esc(row.group === 'index' ? 'Indices' : 'FNO top 100 stocks') + '</td></tr>';
      }
      lastGroup = row.group;
      const gain = Number(row.total_gain_percent || 0);
      const gainClass = gain > 0 ? 'up' : (gain < 0 ? 'down' : '');
      const states = rowStates(row);
      html += '<tr' + (states.any ? ' class="has-signal"' : '') + '>' +
        '<td class="left"><span class="sym">' + esc(displaySymbol(row.symbol)) +
        '</span></td>' +
        '<td class="num">' + Number(row.close).toFixed(2) + '</td>' +
        '<td class="num ' + gainClass + '">' + (gain > 0 ? '+' : '') + gain.toFixed(2) + '%</td>' +
        '<td class="num">' + Number(row.volume_15m).toLocaleString('en-IN') + '</td>' +
        columns.map(c => cellHtml(row, c, view)).join('') +
        '</tr>';
    });
  }
  tableWrap.innerHTML = html + '</tbody></table>';

  /* After the header exists, so the width lands on the real cell. Measured
     over the tab's rows, not the filtered ones. */
  fitStockColumn(view.rows);

  /* Move the live search node into the freshly built header. Moving the same
     node keeps its value and button state; only the focus needs restoring. */
  const slot = tableWrap.querySelector('th.col-stock .searchslot');
  if (slot) slot.appendChild(searchWrap);
  if (hadFocus) {
    searchInput.focus();
    try { searchInput.setSelectionRange(caret, caret); } catch (err) { /* not focusable */ }
  }
}
function rerender() {
  if (lastResult) render(lastResult);
}

/* ---------- scanning ---------- */
async function refresh(force) {
  const cached = lastResult && resultCache.get(lastResult.selection);
  if (!force && cachedResultIsCurrent(cached)) {
    render(cached);
    setStatus('Data is live - no refresh needed.', 'good');
    return;
  }
  if (!force) {
    const allowedAt = Number(localStorage.getItem(REFRESH_ALLOWED_KEY) || 0);
    if (allowedAt > Date.now()) {
      applyCooldown(allowedAt);
      return;
    }
  }
  if (activeJobId) {
    setStatus('A scan is already running; please wait for it to finish.');
    return;
  }
  refreshButton.disabled = true;
  refreshButton.textContent = 'Scanning…';
  setStatus('Scanning the universe on the latest completed candle…');
  tableWrap.innerHTML = '<div class="empty">' +
    esc('Scanning ' + universeSummary() + ', this usually takes 60-90 seconds…') +
    '</div>';
  try {
    const response = await fetch('/api/refresh', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({strategy: 'all'})
    });
    const start = await response.json();
    if (!response.ok) throw new Error(start.error || 'Could not start scan');
    if (start.universe) lastUniverse = start.universe;
    activeJobId = start.job_id;
    pollFailures = 0;
    poll(activeJobId);
  } catch (error) {
    activeJobId = null;
    setStatus('Refresh failed: ' + error.message, 'bad');
    beginCooldown();
  }
}
function poll(jobId) {
  pollTimer = setInterval(async () => {
    try {
      const response = await fetch('/api/job?id=' + encodeURIComponent(jobId));
      const job = await response.json();
      if (response.status === 404) {
        clearInterval(pollTimer);
        activeJobId = null;
        pollFailures = 0;
        setStatus('Previous scan session expired; starting again.');
        refresh(true);
        return;
      }
      if (!response.ok) throw new Error(job.error || 'Could not read scan status');
      pollFailures = 0;
      if (job.status === 'running') return;
      clearInterval(pollTimer);
      activeJobId = null;
      if (job.status === 'complete') {
        render(job.result);
      } else {
        setStatus('Scan failed: ' + (job.error || 'unknown error'), 'bad');
      }
      beginCooldown();
    } catch (error) {
      pollFailures += 1;
      if (pollFailures <= 5) {
        setStatus('Connection issue; retrying scan status…', 'bad');
        return;
      }
      clearInterval(pollTimer);
      activeJobId = null;
      setStatus('Dashboard server unavailable. Start it again, then retry.', 'bad');
      beginCooldown();
    }
  }, 700);
}
function cachedResultIsCurrent(result) {
  if (!result || (result.errors || []).length) return false;
  // Freshness is judged on the intraday view, since one refresh fills both tabs.
  const view = (result.views || {}).intraday;
  if (!view || !view.latest_15min) return false;
  const scannedAt = Date.parse(result.scanned_at_ist || '');
  return Number.isFinite(scannedAt) && Date.now() - scannedAt < 15 * 60 * 1000;
}

/* ---------- events ---------- */
searchInput.addEventListener('input', () => {
  clearButton.classList.toggle('show', searchInput.value.length > 0);
  rerender();
});
clearButton.addEventListener('click', () => {
  searchInput.value = '';
  clearButton.classList.remove('show');
  searchInput.focus();
  rerender();
});
/* The header is rebuilt on every render, so the click is delegated from the
   container rather than bound to the cell. The search box lives in a header
   cell too, but only the sortable ones carry data-sort. */
tableWrap.addEventListener('click', event => {
  const header = event.target.closest('th[data-sort]');
  if (!header) return;
  toggleSort(header.dataset.sort);
});
tableWrap.addEventListener('keydown', event => {
  if (event.key !== 'Enter' && event.key !== ' ') return;
  const header = event.target.closest('th[data-sort]');
  if (!header) return;
  event.preventDefault();
  toggleSort(header.dataset.sort);
});
tabsBox.addEventListener('click', event => {
  const button = event.target.closest('button[data-view]');
  if (!button || button.dataset.view === activeView) return;
  activeView = button.dataset.view;
  tabsBox.querySelectorAll('button').forEach(b => b.classList.toggle('active', b === button));
  rerender();
});
filtersBox.addEventListener('click', event => {
  const button = event.target.closest('button[data-filter]');
  if (!button) return;
  signalFilter = button.dataset.filter;
  filtersBox.querySelectorAll('button').forEach(b => b.classList.toggle('active', b === button));
  rerender();
});
document.addEventListener('keydown', event => {
  if (event.key === '/' && document.activeElement !== searchInput) {
    event.preventDefault();
    searchInput.focus();
    searchInput.select();
  }
  if (event.key === 'Escape' && document.activeElement === searchInput) {
    searchInput.value = '';
    clearButton.classList.remove('show');
    searchInput.blur();
    rerender();
  }
});
refreshButton.addEventListener('click', () => refresh(false));

/* ---------- first load ---------- */
/* Scan results live only in the server's memory, so a reload would otherwise
   have nothing to show and would need a full rescan. Ask the server for the
   last completed scan first and reuse it; only scan when there is none. */
async function fetchLatest() {
  try {
    const response = await fetch('/api/latest');
    if (!response.ok) return null;
    const job = await response.json();
    if (job.status !== 'complete' || !job.result) return null;
    return job;
  } catch (error) {
    return null;
  }
}

async function bootstrap() {
  restoreCooldown();
  tableWrap.innerHTML = '<div class="empty">Loading the last scan&hellip;</div>';

  const latest = await fetchLatest();
  if (latest) {
    const reused = latest.result;
    resultCache.set(reused.selection, reused);
    render(reused);
    setStatus('Showing the last scan - ' + (reused.views?.intraday?.latest_message || ''),
              reused.views?.intraday?.latest_15min ? 'good' : 'bad');
    return;
  }
  maybeAutoScan();
}

// A scan is only forced when there is no result to reuse, so reloading during
// a scan no longer strands the page on an empty table.
function maybeAutoScan() {
  if (activeJobId) return;
  refresh(true);
}

bootstrap();
</script>
</body>
</html>
"""


class DashboardHandler(BaseHTTPRequestHandler):
    manager: ScanManager

    def log_message(self, format: str, *args) -> None:
        print(f"[dashboard] {self.address_string()} {format % args}")

    def send_json(self, payload: dict[str, Any], status: int = 200) -> None:
        body = json.dumps(payload, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store, max-age=0")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/":
            body = INDEX_HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            # The page is generated from a string in this file, so the browser
            # must never reuse a stale copy after the script is edited.
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
            self.send_header("Pragma", "no-cache")
            self.send_header("Expires", "0")
            self.end_headers()
            self.wfile.write(body)
            return
        if parsed.path == "/api/strategies":
            self.send_json({"strategies": self.manager.scanner.registry.labels()})
            return
        if parsed.path == "/api/job":
            job_id = parse_qs(parsed.query).get("id", [""])[0]
            job = self.manager.get(job_id)
            if job is None:
                self.send_json({"error": "Unknown scan job"}, 404)
            else:
                self.send_json(job)
            return
        if parsed.path == "/api/storage":
            self.send_json({"storage": CandleStore().summary()})
            return
        if parsed.path == "/api/latest":
            job = self.manager.latest_complete()
            if job is None:
                self.send_json({"error": "No completed scan yet"}, 404)
            else:
                self.send_json(job)
            return
        self.send_json({"error": "Not found"}, 404)

    def do_POST(self) -> None:
        if urlparse(self.path).path != "/api/refresh":
            self.send_json({"error": "Not found"}, 404)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length > 10_000:
                raise ValueError("Request body too large")
            request = json.loads(self.rfile.read(length) or b"{}")
            selection = str(request.get("strategy", "all"))
            if selection != "all" and selection not in STRATEGY_BY_KEY:
                raise ValueError(f"Unknown strategy: {selection}")
            job_id, started = self.manager.start(selection)
            self.send_json(
                {
                    "job_id": job_id,
                    "started": started,
                    "universe": self.manager.scanner.describe_universe(selection),
                },
                202,
            )
        except (ValueError, json.JSONDecodeError) as error:
            self.send_json({"error": str(error)}, 400)
        except Exception as error:
            self.send_json({"error": str(error)}, 500)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9999)
    parser.add_argument("--open-browser", action="store_true")
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    return args


def port_is_in_use(host: str, port: int) -> bool:
    """True when something is already accepting connections on host:port.

    Windows allows two processes to bind the same port, because sockets
    reuse addresses by default. Binding again therefore succeeds and the
    browser silently keeps talking to whichever process won, which serves
    stale page code. Probing first turns that into a clear error.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(1.0)
        return probe.connect_ex((host, port)) == 0


def report_port_clash(host: str, port: int, detail: str) -> None:
    script = pathlib.Path(__file__).name
    print(f"\nERROR: cannot serve {host}:{port} - {detail}\n")
    print("A dashboard is already running on that port. It keeps serving the")
    print("page code it was started with, so edits will not appear until it is")
    print("stopped. Close the other console window (or press Ctrl+C there),")
    print("then start this one again.")
    print(f"\nTo run a second copy side by side instead:")
    print(f"    python {script} --port {port + 1}\n")


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if port_is_in_use(args.host, args.port):
        report_port_clash(args.host, args.port, "the port is already in use")
        return 1
    scanner = DashboardScanner()
    DashboardHandler.manager = ScanManager(scanner)
    url = f"http://{args.host}:{args.port}/"
    try:
        server = ThreadingHTTPServer((args.host, args.port), DashboardHandler)
    except OSError as error:
        report_port_clash(args.host, args.port, str(error))
        return 1
    print(f"Equity strategy dashboard running at {url}")
    print("Read-only mode: no orders will be placed.")
    if args.open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping dashboard.")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
