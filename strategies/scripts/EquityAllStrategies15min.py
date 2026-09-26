#!/usr/bin/env python3
"""One 15-minute scanner for every 15-minute strategy, instead of one each.

Consolidates the 15-minute rules that today live in a dozen files behind a
dozen scheduled tasks into a single process, one fetch pass per symbol, and one
durable signal ledger. The rules themselves are **not** reimplemented here: each
entry in :data:`STRATEGY_REGISTRY` names the script that already owns the rule
and calls that function, so a change to a rule lands in the dashboard, the agent
and this scanner at once. What this file adds is the registry, the adapter that
normalises the sources' different return shapes, and the buy/sell split.

Why one process rather than several:

* **One fetch per symbol.** Each rule needs the same 15-minute series, and six
  of them also need the daily series. One pass serves all of them, instead of
  each task re-fetching the same history every poll.
* **One rate limiter.** ``ApiRateLimiter`` is shared, so 100 stocks plus 3
  indices at two resolutions cannot collectively outrun the API budget the way
  separate processes each with their own limiter can.
* **One signal ledger.** ``SignalStore`` keys identity on
  ``strategy_name:symbol:candle_epoch``, so every strategy keeps its own
  exactly-once identity and two strategies firing on one symbol cannot suppress
  each other.

Scope, deliberately narrow:

* **15-minute rules only.** The 5-minute EMA 10/20/30 crossover and the 5-minute
  ORB breakout are excluded, as is the daily-bar breakout and the index
  rejection strategy (its ``fetch_candles`` uses a stale ``history()``
  signature). The two index series in the universe are still scanned by the
  15-minute rules, which is where ``BANKNIFTY`` earns its place.
* **Buys trade, sells do not.** A sell signal is claimed, logged and written to
  the workbook with its order status recorded as not-sent. The existing
  scheduled scripts have a buy path only (``ORDER_SIDE = 1``); treating a sell
  as an exit needs position tracking and an exit order, which is not in here.
* **The config gate is the existing one.** ``ConfigGatedFyersClient`` reads
  ``strategies/config/equity/config.json`` because this file's name begins with
  "Equity", so ``place_order``, ``max_stocks_per_day``, ``qty``,
  ``stop_loss`` and ``trailing_stop`` apply exactly as they do to the scripts
  this replaces. With ``place_order`` set to ``YES`` in that file, ``--live``
  sends real orders. The default is ``--dry-run``.

The durable machinery - the signal store, the config-gated client, the rate
limiter, the order-tag and signal locks, the workbook upsert, the unfinished
signal recovery - is imported from ``EquityEma15_10_20_50Crossover15min.py``
rather than copied, so there is one implementation of it. Its ``log_message`` is
routed into this script's log so a consolidated run leaves one trail.

Usage::

    python EquityAllStrategies15min.py --dry-run --once   # one pass, no orders
    python EquityAllStrategies15min.py --live             # real orders, polling
"""
from __future__ import annotations

import argparse
import datetime as dt
import importlib.util
import pathlib
import sys
import time
from dataclasses import dataclass
from typing import Any, Callable, Sequence
from zoneinfo import ZoneInfo

SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parents[1]
STRATEGIES_DIR = SCRIPT_DIR.parent

STOCKS_PATH = STRATEGIES_DIR / "data" / "NiftyFNOTop100.txt"
INDICES_PATH = STRATEGIES_DIR / "data" / "Indices.txt"
DATABASE_PATH = (
    STRATEGIES_DIR / "databases" / "equity_all_strategies_15min.db"
)
LOG_PATH = STRATEGIES_DIR / "logs" / "EquityAllStrategies15min.log"
EXCEL_PATH = STRATEGIES_DIR / "logs" / "EquityAllStrategies15min.xlsx"

#: Starts with "Equity", so order_config selects strategies/config/equity.
SCRIPT_NAME = "EquityAllStrategies15min.py"
#: One ledger identity prefix, so this script's signals never collide with the
#: per-strategy scripts' own ledgers.
STRATEGY_NAME_PREFIX = "EQUITY_ALL_15MIN"

MARKET_TIMEZONE = ZoneInfo("Asia/Kolkata")
CANDLE_SECONDS = 15 * 60
DEFAULT_POLL_SECONDS = 5 * 60
HISTORY_DAYS = 20
DAILY_HISTORY_DAYS = 30

BUY = "BUY"
SELL = "SELL"
NONE = "NONE"

# The order constants, restated rather than inherited so this file reads on its
# own. FYERS: type 2 is MARKET, side 1 is BUY.
ORDER_TYPE = 2
ORDER_SIDE = 1
PRODUCT_TYPE = "INTRADAY"

sys.path.insert(0, str(STRATEGIES_DIR / "utils"))
sys.path.insert(0, str(REPO_ROOT / "skills" / "fyers-trading" / "scripts"))
sys.path.insert(0, str(STRATEGIES_DIR / "config"))


# =============================================================================
# Shared durable machinery, imported rather than copied
# =============================================================================
def _load_shared() -> Any:
    """Load the machinery from the script this one consolidates.

    SignalStore, ConfigGatedFyersClient, ApiRateLimiter, the order-tag and
    signal locks, the workbook upsert and the unfinished-signal recovery all
    live there already. Reusing them is the point: a fix to the exactly-once
    ledger then applies to every strategy at once.
    """
    name = "equity_15min_shared_machinery"
    if name in sys.modules:
        return sys.modules[name]
    path = SCRIPT_DIR / "EquityEma15_10_20_50Crossover15min.py"
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load the shared machinery from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


_shared = _load_shared()

SignalStore = _shared.SignalStore
ConfigGatedFyersClient = _shared.ConfigGatedFyersClient
ApiRateLimiter = _shared.ApiRateLimiter
Candle = _shared.Candle
process_claimed_signal = _shared.process_claimed_signal
recover_unfinished_signals = _shared.recover_unfinished_signals
flush_pending_excel = _shared.flush_pending_excel
get_entry_qty = _shared.get_entry_qty
market_is_open = _shared.market_is_open
live_order_window_is_open = _shared.live_order_window_is_open
seconds_until_next_candle_close = _shared.seconds_until_next_candle_close
completed_candles_from_response = _shared.completed_candles_from_response
candle_is_current = _shared.candle_is_current
LAST_INTRADAY_ENTRY = _shared.LAST_INTRADAY_ENTRY


def now_ist() -> dt.datetime:
    return dt.datetime.now(MARKET_TIMEZONE)


def log_message(message: str, error: bool = False) -> None:
    """Print and append to this script's log, in the house format."""
    stamp = dt.datetime.now(MARKET_TIMEZONE).isoformat(timespec="seconds")
    print(f"{stamp} {message}")
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with LOG_PATH.open("a", encoding="utf-8") as handle:
            handle.write(f"{stamp} {message}\n")
    except OSError:
        pass


# The borrowed code logs through its own module-level log_message, which points
# at the other script's log file, and its order helper quotes that script's name.
# Redirect both, so a consolidated run leaves one trail and one identity.
_shared.log_message = log_message
_shared.LOG_PATH = LOG_PATH
_shared.SCRIPT_NAME = SCRIPT_NAME


# =============================================================================
# The registry
# =============================================================================
def load_strategy_module(name: str, file_name: str):
    """Load a rule's owning script by path, once, and cache it."""
    cache_name = f"equity_all_15min_{name}"
    if cache_name in sys.modules:
        return sys.modules[cache_name]
    path = SCRIPT_DIR / file_name
    if not path.is_file():
        # ImportError rather than letting spec_from_file_location raise
        # FileNotFoundError, so a missing rule reads as the same class of
        # problem as a rule that cannot be imported at all.
        raise ImportError(f"no such rule module: {path}")
    spec = importlib.util.spec_from_file_location(cache_name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load the rule module {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[cache_name] = module
    spec.loader.exec_module(module)
    return module


# --- adapters. Each takes (candles, daily, current) and returns (side, details).

def _normalise(side: Any, details: dict | None) -> tuple[str, dict]:
    """Map a source's side onto BUY / SELL / NONE.

    The sources spell a side four ways: CE/PE, BUY/SELL, a plain boolean for the
    buy-only rules, and a string for the ones that can go either way. Anything
    unrecognised is treated as no signal, because inventing a direction is worse
    than staying quiet.
    """
    payload = dict(details or {})
    if isinstance(side, bool):
        return (BUY, payload) if side else (NONE, payload)
    text = str(side).strip().upper()
    if text in ("BUY", "CE", "LONG"):
        return BUY, payload
    if text in ("SELL", "PE", "PUT", "SHORT"):
        return SELL, payload
    return NONE, payload


def adapter_side_string_current_restrict(
    module_name: str, file_name: str, function_name: str
) -> Callable[[list, list, dt.datetime], tuple[str, dict]]:
    """For (candles, current, restrict_to_today) -> (side, details)."""
    def run(candles: list, daily: list, current: dt.datetime) -> tuple[str, dict]:
        module = load_strategy_module(module_name, file_name)
        side, details = getattr(module, function_name)(candles, current, True)
        return _normalise(side, details)
    return run


def adapter_side_string_bar(
    module_name: str, file_name: str, function_name: str
) -> Callable[[list, list, dt.datetime], tuple[str, dict]]:
    """For (candles, bar_seconds) -> (side, details)."""
    def run(candles: list, daily: list, current: dt.datetime) -> tuple[str, dict]:
        module = load_strategy_module(module_name, file_name)
        side, details = getattr(module, function_name)(candles, CANDLE_SECONDS)
        return _normalise(side, details)
    return run


def adapter_side_bool(
    module_name: str, file_name: str, function_name: str
) -> Callable[[list, list, dt.datetime], tuple[str, dict]]:
    """For (candles) -> (matched, details), where matched means buy."""
    def run(candles: list, daily: list, current: dt.datetime) -> tuple[str, dict]:
        module = load_strategy_module(module_name, file_name)
        matched, details = getattr(module, function_name)(candles)
        return _normalise(matched, details)
    return run


def adapter_side_string_daily(
    module_name: str, file_name: str, function_name: str
) -> Callable[[list, list, dt.datetime], tuple[str, dict]]:
    """For (candles, daily, current, restrict_to_today) -> (side, details)."""
    def run(candles: list, daily: list, current: dt.datetime) -> tuple[str, dict]:
        module = load_strategy_module(module_name, file_name)
        side, details = getattr(module, function_name)(candles, daily, current,
                                                       True)
        return _normalise(side, details)
    return run


def adapter_orb_rejection(
    module_name: str, file_name: str, function_name: str, want: str
) -> Callable[[list, list, dt.datetime], tuple[str, dict]]:
    """For the ORB script's (candles, orb_high, orb_low) rejection rule.

    The script returns either a PE (at the opening-range high) or a CE (at the
    low) from one call, so `want` picks the branch this column means.
    """
    def run(candles: list, daily: list, current: dt.datetime) -> tuple[str, dict]:
        module = load_strategy_module(module_name, file_name)
        orb_range = module.calculate_orb_range(candles)
        if not orb_range:
            return NONE, {"reason": "no opening range in this session"}
        side, details = getattr(module, function_name)(
            candles, orb_range[0], orb_range[1])
        resolved, payload = _normalise(side, details)
        if resolved != want:
            return NONE, payload
        payload["orb_high"] = orb_range[0]
        payload["orb_low"] = orb_range[1]
        return resolved, payload
    return run


def adapter_level_rejection(level: str) -> Callable[[list, list, dt.datetime],
                                                   tuple[str, dict]]:
    """Rejection or bounce at a level from the prior session.

    The rule lives in EquityLevelRejectionSignal15min.py, which the dashboard
    column also calls, so there is one implementation of it. It can return
    either side: a rejection sells, a bounce buys.
    """
    def run(candles: list, daily: list, current: dt.datetime) -> tuple[str, dict]:
        module = load_strategy_module("level", _LEVEL)
        side, details = module.level_rejection_signal(candles, daily, level,
                                                      current)
        return _normalise(side, details)
    return run


def _candle_at(candles: Sequence[Any], current: dt.datetime,
               hour: int, minute: int):
    for candle in candles:
        stamp = dt.datetime.fromtimestamp(candle.epoch, MARKET_TIMEZONE)
        if (stamp.date() == current.date() and stamp.hour == hour
                and stamp.minute == minute):
            return candle
    return None


def _second_candle_buy_side(candles: list, daily: list,
                            current: dt.datetime) -> tuple[str, dict]:
    """The second-candle breakout buy."""
    module = load_strategy_module("second", _SECOND)
    second = _candle_at(candles, current, 9, 30)
    third = _candle_at(candles, current, 9, 45)
    return _normalise(*module.second_candle_breakout_signal(
        candles, second, third))


def _second_candle_sell_side(candles: list, daily: list,
                             current: dt.datetime) -> tuple[str, dict]:
    """The bearish reversal sell, held back unless price is genuinely weak.

    The gate is the same one the dashboard applies, called from the same source
    file, so a sell can never be recorded here that the column would suppress.
    The gate fails open by design when it cannot read a level, which is why
    ``fetch_daily_candles`` always supplies the daily series.
    """
    module = load_strategy_module("second", _SECOND)
    first = _candle_at(candles, current, 9, 15)
    second = _candle_at(candles, current, 9, 30)
    third = _candle_at(candles, current, 9, 45)
    side, details = module.bearish_reversal_signal(
        candles, first, second, third)
    resolved, payload = _normalise(side, details)
    if resolved != SELL:
        return resolved, payload
    gate = load_strategy_module("gate", _GATE)
    allowed, verdict = gate.second_candle_sell_gate(
        candles, daily, current, True)
    if not allowed:
        return NONE, {**payload, **verdict}
    return SELL, {**payload, **verdict}


def adapter_ema_rule(
    module_name: str, file_name: str, function_name: str, want_rule: str
) -> Callable[[list, list, dt.datetime], tuple[str, dict]]:
    """One registry entry per rule inside a multi-rule evaluator.

    ``ema10_ema20_signal`` returns whichever of its four rules matched and names
    it in ``details["rule"]``. Registering it once would give all four the same
    ledger identity, so a cross and a later stack state on the same bar would
    collapse into one indistinguishable row. Filtering on the rule name keeps
    each one separately identified without touching the source.
    """
    def run(candles: list, daily: list, current: dt.datetime) -> tuple[str, dict]:
        module = load_strategy_module(module_name, file_name)
        side, details = getattr(module, function_name)(candles, CANDLE_SECONDS)
        resolved, payload = _normalise(side, details)
        if str(payload.get("rule")) != want_rule:
            # A different rule matched, or none did. Say which, so a quiet
            # scan is not mistaken for a broken entry.
            fired = payload.get("rule")
            return NONE, {
                **payload,
                "want_rule": want_rule,
                "reason": (
                    f"no {want_rule} on this bar"
                    if fired is None
                    else f"{want_rule} did not match; {fired} did"
                ),
            }
        return resolved, payload
    return run


@dataclass(frozen=True)
class StrategySpec:
    """One 15-minute rule, and where it lives."""

    key: str
    column: str
    file_name: str
    module_name: str
    evaluate: Callable[[list, list, dt.datetime], tuple[str, dict]]
    needs_daily: bool = False
    note: str = ""
    #: True when the owning script reads the wall-clock date rather than the
    #: series it is handed. Those rules find nothing on a holiday or a weekend,
    #: which is correct during market hours and silently useless outside them.
    #: Recorded here so a run can say so out loud rather than reporting a quiet
    #: scan as a clean one.
    wall_clock_date: bool = False

    @property
    def strategy_name(self) -> str:
        """The ledger identity for this rule.

        Distinct per key, so two rules firing on the same symbol and the same
        candle keep separate rows and separate order tags.
        """
        return f"{STRATEGY_NAME_PREFIX}_{self.key.upper()}"


_ORB = "OrbStrategyCallPut.py"
_R1 = "R1PrevHighRejectionStrategy.py"
_LEVEL = "EquityLevelRejectionSignal15min.py"
_OPEN = "EquityOpenCandleEmaStackBuy15min.py"
_COMPANIONS = "EquityOpenRangeOpeningSignals15min.py"
_EMA_FRESH = "EquityEma15_10_20_50Crossover15min.py"
_EMA_10_20 = "EquityEma10_20_Signals15min.py"
_LH = "EquityLowerHighCloseSignal15min.py"
_SECOND = "SecondCandleBreakout.py"
_GATE = "SecondCandleSellGate15min.py"
_ORB_LOW = "EquityOrbLowRejectionSignal15min.py"
_DOUBLE = "EquityDoubleBottomBullishSignal15min.py"

STRATEGY_REGISTRY: tuple[StrategySpec, ...] = (
    # --- opening-range group
    StrategySpec(
        "orb", "ORB", _ORB, "orb",
        adapter_orb_rejection("orb", _ORB, "orb_rejection_signal", BUY),
        note="bullish rejection at the opening-range low",
        wall_clock_date=True,   # calculate_orb_range reads dt.date.today()
    ),
    StrategySpec(
        "orb_high_rejection", "ORB High Rej", _ORB, "orb",
        adapter_orb_rejection("orb", _ORB, "orb_rejection_signal", SELL),
        note="bearish rejection at the opening-range high",
        wall_clock_date=True,   # calculate_orb_range reads dt.date.today()
    ),
    StrategySpec(
        "orb_low_rejection", "ORB Low Rej", _ORB_LOW, "orb_low",
        adapter_side_string_current_restrict(
            "orb_low", _ORB_LOW, "orb_low_rejection_buy_signal"),
        note="bullish rejection at the opening-range low under a bullish open",
    ),
    StrategySpec(
        "orb_second_candle_prev_high", "ORB 2nd>PrevHigh", _COMPANIONS,
        "companions",
        adapter_side_string_daily(
            "companions", _COMPANIONS, "second_candle_prev_high_buy_signal"),
        needs_daily=True,
        note="second candle above the first close and the previous day's high",
    ),
    StrategySpec(
        "orb_first_candle_gap_down", "ORB 1st GapDown", _COMPANIONS,
        "companions",
        adapter_side_string_daily(
            "companions", _COMPANIONS, "first_candle_gap_down_sell_signal"),
        needs_daily=True,
        note="first candle gaps down below the previous close and falls further",
    ),
    StrategySpec(
        "orb_first_candle_small_body", "ORB 1st SmallBody", _COMPANIONS,
        "companions",
        adapter_side_string_current_restrict(
            "companions", _COMPANIONS, "first_candle_small_body_sell_signal"),
        note="first candle has a small body, read as indecision",
    ),
    StrategySpec(
        "orb_second_candle_gap_ema", "ORB 2nd Gap+EMA", _COMPANIONS,
        "companions",
        adapter_side_string_current_restrict(
            "companions", _COMPANIONS, "second_candle_gap_ema_sell_signal"),
        note="second candle gaps below the first close and closes under ema10",
    ),
    # --- level rejections
    StrategySpec(
        "r1_rejection", "R1 rejection", _LEVEL, "level",
        adapter_level_rejection("r1"), needs_daily=True,
        note="rejection or bounce at R1 from the previous day's range",
    ),
    StrategySpec(
        "prev_high_rejection", "PDH Rejection", _LEVEL, "level",
        adapter_level_rejection("prev_high"), needs_daily=True,
        note="rejection or bounce at the previous trading day's high",
    ),
    # --- candle-shape rejections
    StrategySpec(
        "doji_rejection", "Doji rejection", _R1, "r1",
        adapter_side_string_current_restrict(
            "r1", _R1, "doji_rejection_signal"),
        note="bearish rejection of the prior doji",
    ),
    StrategySpec(
        "higher_high_rejection", "HigherHigh rej", _R1, "r1",
        adapter_side_string_current_restrict(
            "r1", _R1, "higher_high_low_rejection_signal"),
        note="higher high that fails below the prior low",
    ),
    StrategySpec(
        "higher_high_close_rejection", "HH vs Close", _R1, "r1",
        adapter_side_string_current_restrict(
            "r1", _R1, "higher_high_close_rejection_signal"),
        note="higher high that fails back under the prior close",
    ),
    StrategySpec(
        "lower_high_close", "LH", _LH, "lh",
        adapter_side_string_bar("lh", _LH, "lower_high_close_sell_signal"),
        note="lower high with the close under the prior close",
    ),
    # --- opening candle
    StrategySpec(
        "open_ema_stack", "Open EMA stack", _OPEN, "open_stack",
        adapter_side_string_daily(
            "open_stack", _OPEN, "open_candle_ema_stack_buy_signal"),
        needs_daily=True,
        note="bullish opening candle in an EMA stack, or the inverse stack",
    ),
    # --- second candle
    StrategySpec(
        "second_candle_buy", "Second Candle CE", _SECOND, "second",
        _second_candle_buy_side,
        note="second-candle breakout buy",
        wall_clock_date=True,   # get_today_candles reads dt.date.today()
    ),
    StrategySpec(
        "second_candle_sell", "Second Candle PE", _SECOND, "second",
        _second_candle_sell_side,
        needs_daily=True,
        note="bearish reversal sell, behind the close-below-prior-low-or-ema "
             "gate",
    ),
    # --- indicator group
    StrategySpec(
        "ema_fresh", "ema15:10-20-50 crossover", _EMA_FRESH, "ema_fresh",
        adapter_side_bool("ema_fresh", _EMA_FRESH, "evaluate_fresh_crossover"),
        note="fresh bullish alignment off ema15",
    ),
    StrategySpec(
        "ema_pullback", "EMA10 pullback", _EMA_FRESH, "ema_pullback",
        adapter_side_bool("ema_pullback", _EMA_FRESH,
                          "evaluate_ema10_pullback_cross"),
        note="pullback into ema10 closing back above it",
    ),
    # --- ema10 against ema20, one entry per rule so each keeps its own ledger
    # identity. The source function returns whichever rule matched; these four
    # filter on its rule name.
    StrategySpec(
        "ema_10_cross_up", "ema10>20 cross", _EMA_10_20, "ema_10_20",
        adapter_ema_rule("ema_10_20", _EMA_10_20, "ema10_ema20_signal",
                         "cross_up"),
        note="ema10 crossed above ema20",
    ),
    StrategySpec(
        "ema_10_cross_down", "ema10<20 cross", _EMA_10_20, "ema_10_20",
        adapter_ema_rule("ema_10_20", _EMA_10_20, "ema10_ema20_signal",
                         "cross_down"),
        note="ema10 crossed below ema20",
    ),
    StrategySpec(
        "ema_10_stack_10_20_30", "ema10<20<30", _EMA_10_20, "ema_10_20",
        adapter_ema_rule("ema_10_20", _EMA_10_20, "ema10_ema20_signal",
                         "stack_10_20_30"),
        note="ema10<ema20<ema30 with the close under ema10",
    ),
    StrategySpec(
        "ema_10_stack_10_20_50", "ema10<20<50", _EMA_10_20, "ema_10_20",
        adapter_ema_rule("ema_10_20", _EMA_10_20, "ema10_ema20_signal",
                         "stack_10_20_50"),
        note="ema10<ema20<ema50 with the close under ema10",
    ),
    StrategySpec(
        "double_bottom", "Double Bottom", _DOUBLE, "double",
        adapter_side_string_daily("double", _DOUBLE,
                                  "double_bottom_bullish_signal"),
        needs_daily=True,
        note="bullish candle that rejected the prior low or the range low",
    ),
)


SPEC_BY_KEY = {spec.key: spec for spec in STRATEGY_REGISTRY}

#: Rules whose owning script reads the wall-clock date, so they only find
#: anything during a live session. Worth saying out loud at startup: on a
#: holiday a scan would otherwise look clean while three rules did nothing.
WALL_CLOCK_RULES = tuple(
    spec.key for spec in STRATEGY_REGISTRY if spec.wall_clock_date
)


# =============================================================================
# Fetching
# =============================================================================
def read_symbols(path: pathlib.Path) -> list[str]:
    """One FYERS symbol per line, skipping blanks and comments."""
    if not path.exists():
        return []
    return [
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


def read_universe() -> list[str]:
    """The FNO Top 100 plus the three indices, de-duplicated in order."""
    symbols = read_symbols(STOCKS_PATH) + read_symbols(INDICES_PATH)
    return list(dict.fromkeys(symbols))


def fetch_daily_candles(
    client,
    symbol: str,
    limiter: ApiRateLimiter,
    current: dt.datetime,
) -> list[Candle]:
    """Fetch daily bars, dropping today's.

    Every daily consumer wants a session strictly before the one being judged,
    and each source's ``previous_session`` already filters on that. Dropping the
    same-day bar here as well means a partially-formed daily bar can never be
    reached even by a rule that forgets to filter.
    """
    response = limiter.call(
        client.history,
        symbol,
        "D",
        (current.date() - dt.timedelta(days=DAILY_HISTORY_DAYS)).isoformat(),
        current.date().isoformat(),
    )
    if response.get("s") != "ok":
        raise RuntimeError(f"daily history failed for {symbol}: {response}")
    by_epoch: dict[int, Candle] = {}
    for row in response.get("candles") or []:
        if not isinstance(row, (list, tuple)) or len(row) < 6:
            continue
        try:
            candle = Candle(int(row[0]), *map(float, row[1:6]))
        except (TypeError, ValueError):
            continue
        stamp = dt.datetime.fromtimestamp(candle.epoch, MARKET_TIMEZONE)
        if stamp.date() < current.date():
            by_epoch[candle.epoch] = candle
    return [by_epoch[epoch] for epoch in sorted(by_epoch)]


def fetch_intraday_candles(
    client,
    symbol: str,
    limiter: ApiRateLimiter,
    current: dt.datetime,
) -> list[Candle]:
    """Completed 15-minute bars, borrowed from the script this replaces."""
    response = limiter.call(
        client.history,
        symbol,
        "15",
        (current.date() - dt.timedelta(days=HISTORY_DAYS)).isoformat(),
        current.date().isoformat(),
    )
    if response.get("s") != "ok":
        raise RuntimeError(f"history failed for {symbol}: {response}")
    return completed_candles_from_response(response, current)


# =============================================================================
# Acting on a match
# =============================================================================
def _judged_epoch(details: dict, candles: list) -> int | None:
    """The epoch of the bar the signal was judged on.

    The ledger's identity is ``strategy:symbol:candle_epoch``, so a rule that
    does not name its own bar would collapse every bar of the day into one
    signal. The sources disagree about which key they use, so the common ones
    are read before falling back to the newest bar.
    """
    for key in ("candle_epoch", "curr_time", "signal_time", "second_time",
                "curr_candle_time"):
        raw = details.get(key)
        if raw is None:
            continue
        if key == "candle_epoch":
            try:
                return int(raw)
            except (TypeError, ValueError):
                continue
        try:
            return int(dt.datetime.fromisoformat(str(raw)).timestamp())
        except (TypeError, ValueError):
            continue
    return candles[-1].epoch if candles else None


def handle_buy(
    client,
    store: SignalStore,
    spec: StrategySpec,
    symbol: str,
    details: dict,
    live: bool,
    excel_path: pathlib.Path,
) -> bool:
    """Claim a buy and send it through the normal order path."""
    matched_at = now_ist()
    epoch = details.get("candle_epoch")
    candle_start = dt.datetime.fromtimestamp(epoch, MARKET_TIMEZONE)
    payload = dict(details)
    payload["strategy_name"] = spec.strategy_name
    signal_id = store.claim(
        symbol,
        epoch,
        candle_start.isoformat(timespec="seconds"),
        matched_at.isoformat(timespec="seconds"),
        payload,
        "LIVE" if live else "DRY_RUN",
        get_entry_qty(SCRIPT_NAME),
        strategy_name=spec.strategy_name,
    )
    if signal_id is None:
        return False
    process_claimed_signal(
        client, store, store.get_row(signal_id), allow_live_send=live)
    flush_pending_excel(store, excel_path)
    return True


def handle_sell(
    store: SignalStore,
    spec: StrategySpec,
    symbol: str,
    details: dict,
    live: bool,
    excel_path: pathlib.Path,
) -> bool:
    """Claim a sell, log it, and record that no order was sent.

    The row is written with the ledger's own not-sent status rather than left
    pending, so the workbook and the recovery pass both see a decided outcome
    instead of an order that may or may not have gone out.
    """
    matched_at = now_ist()
    epoch = details.get("candle_epoch")
    candle_start = dt.datetime.fromtimestamp(epoch, MARKET_TIMEZONE)
    payload = dict(details)
    payload["strategy_name"] = spec.strategy_name
    signal_id = store.claim(
        symbol,
        epoch,
        candle_start.isoformat(timespec="seconds"),
        matched_at.isoformat(timespec="seconds"),
        payload,
        "LIVE" if live else "DRY_RUN",
        get_entry_qty(SCRIPT_NAME),
        strategy_name=spec.strategy_name,
    )
    if signal_id is None:
        return False
    store.finish_order(
        signal_id,
        "NOT_SENT",
        "NOT_SENT",
        "sell signal recorded; sells are logged only and place no order",
    )
    flush_pending_excel(store, excel_path)
    return True


def evaluate_symbol(
    client,
    store: SignalStore,
    spec: StrategySpec,
    symbol: str,
    candles: list,
    daily: list,
    live: bool,
    excel_path: pathlib.Path,
) -> str | None:
    """Run one rule for one symbol and act on it. Returns the side acted on."""
    current = now_ist()
    try:
        side, details = spec.evaluate(candles, daily, current)
    except (OSError, RuntimeError, ValueError, TypeError, AttributeError,
            IndexError, KeyError) as error:
        log_message(f"EVAL ERROR | {spec.key} | {symbol} | {error}", error=True)
        return None
    if side == NONE:
        return None

    details = dict(details or {})
    epoch = _judged_epoch(details, candles)
    if epoch is None:
        log_message(
            f"SKIP | {spec.key} | {symbol} | no bar to attribute the signal to",
            error=True)
        return None
    details["candle_epoch"] = epoch
    reason = str(details.get("reason") or spec.note)
    stamp = dt.datetime.fromtimestamp(epoch, MARKET_TIMEZONE)
    log_message(
        f"MATCH | {spec.strategy_name} | {symbol} | {side} | "
        f"{stamp:%H:%M} IST | {reason}"
    )

    if side == SELL:
        return SELL if handle_sell(store, spec, symbol, details, live,
                                   excel_path) else None
    return BUY if handle_buy(client, store, spec, symbol, details, live,
                            excel_path) else None


# =============================================================================
# The scan
# =============================================================================
def run_cycle(
    client,
    limiter: ApiRateLimiter,
    store: SignalStore,
    live: bool,
    symbols: list[str],
    excel_path: pathlib.Path = EXCEL_PATH,
) -> dict[str, int]:
    """Recover durable work, then fetch each symbol once and judge every rule."""
    recover_unfinished_signals(client, store, excel_path, allow_live_send=live)
    tally = {"buy": 0, "sell": 0, "errors": 0, "skipped": 0}
    for symbol in symbols:
        current = now_ist()
        try:
            candles = fetch_intraday_candles(client, symbol, limiter, current)
            if not candles:
                tally["skipped"] += 1
                continue
            latest_time = dt.datetime.fromtimestamp(
                candles[-1].epoch, MARKET_TIMEZONE)
            if latest_time.date() != current.date() or not candle_is_current(
                candles[-1], current
            ):
                # Never repeat a prior session's signal or act on stale history.
                tally["skipped"] += 1
                continue
            daily = fetch_daily_candles(client, symbol, limiter, current)
            for spec in STRATEGY_REGISTRY:
                if spec.needs_daily and not daily:
                    continue
                side = evaluate_symbol(
                    client, store, spec, symbol, candles, daily, live,
                    excel_path)
                if side == BUY:
                    tally["buy"] += 1
                elif side == SELL:
                    tally["sell"] += 1
        except Exception as error:  # noqa: BLE001 - one symbol must not stop the scan
            tally["errors"] += 1
            log_message(f"SCAN ERROR | {symbol} | {error}", error=True)
    flush_pending_excel(store, excel_path)
    return tally


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Every 15-minute strategy in one scanner.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--live",
        action="store_true",
        help="Send real buy orders. Also requires place_order=YES in "
             "strategies/config/equity/config.json.",
    )
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="Record and print signals without sending them (default).",
    )
    parser.add_argument(
        "--once", action="store_true",
        help="Run one scan cycle and exit, for use from a scheduler.")
    parser.add_argument(
        "--list", action="store_true",
        help="Print the strategy registry and exit.")
    parser.add_argument(
        "--poll-seconds", type=int, default=DEFAULT_POLL_SECONDS,
        help=f"Seconds between scans (default: {DEFAULT_POLL_SECONDS}).")
    args = parser.parse_args(argv)
    if args.poll_seconds < 1:
        parser.error("--poll-seconds must be positive")
    return args


def print_registry() -> None:
    print(f"{len(STRATEGY_REGISTRY)} strategies, 15-minute timeframe")
    print(f"{'key':<30} {'column':<26} {'daily':<6} {'clock':<6} module")
    for spec in STRATEGY_REGISTRY:
        print(f"{spec.key:<30} {spec.column:<26} "
              f"{'yes' if spec.needs_daily else '-':<6} "
              f"{'yes' if spec.wall_clock_date else '-':<6} "
              f"{spec.file_name}")
    if WALL_CLOCK_RULES:
        print()
        print("These read the wall-clock date inside their own script, so they "
              "find nothing")
        print("outside a live session (a holiday or a weekend):")
        for key in WALL_CLOCK_RULES:
            print(f"  {key}")


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.list:
        print_registry()
        return 0

    live = bool(args.live)
    log_message(
        f"STARTED | strategies={len(STRATEGY_REGISTRY)} | "
        f"mode={'LIVE' if live else 'DRY_RUN'} | "
        f"sells={'logged only, no order'} | poll={args.poll_seconds}s"
    )
    if live:
        log_message(
            "WARNING | --live sends real buy orders, subject to place_order, "
            "max_stocks_per_day and qty in the equity config.")
    if not market_is_open() and WALL_CLOCK_RULES:
        log_message(
            "NOTE | the market is closed, so these rules read the wall-clock "
            f"date and will find nothing: {','.join(WALL_CLOCK_RULES)}")

    try:
        client = ConfigGatedFyersClient(
            strategy_name=STRATEGY_NAME_PREFIX, script_name=SCRIPT_NAME)
        limiter = ApiRateLimiter()
        store = SignalStore(DATABASE_PATH)
        recover_unfinished_signals(client, store, EXCEL_PATH,
                                   allow_live_send=live)

        if live and not live_order_window_is_open():
            log_message(
                "STOPPED | live entries are allowed only from 09:15 through "
                f"{LAST_INTRADAY_ENTRY:%H:%M} IST", error=True)
            return 0

        symbols = read_universe()
        if not symbols:
            raise ValueError(
                f"no symbols found in {STOCKS_PATH} or {INDICES_PATH}")
        log_message(f"UNIVERSE | {len(symbols)} symbols "
                    f"({STOCKS_PATH.name} + {INDICES_PATH.name})")

        if not args.once and not market_is_open():
            log_message("STOPPED | market is closed")
            return 0

        while args.once or market_is_open():
            tally = run_cycle(client, limiter, store, live, symbols)
            log_message(
                f"CYCLE | buys={tally['buy']} sells={tally['sell']} "
                f"skipped={tally['skipped']} errors={tally['errors']}")
            if args.once:
                break
            time.sleep(min(args.poll_seconds,
                           seconds_until_next_candle_close()))

        log_message("STOPPED | scan complete")
        return 0
    except Exception as error:  # noqa: BLE001 - the scheduler reads the exit code
        log_message(f"ERROR | {error}", error=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
