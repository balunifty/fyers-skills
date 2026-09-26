#!/usr/bin/env python3
"""Rule-Based Trading Agent - No LLM Required.

Pure rule-based agent that monitors markets and alerts when strategy conditions are met.
Uses your existing strategies without any LLM dependency.
"""
from __future__ import annotations

import datetime as dt
import importlib.util
import json
import os
import pathlib
import sqlite3
import sys
import time
from dataclasses import dataclass, asdict
from typing import Optional
from zoneinfo import ZoneInfo
from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Font, PatternFill

SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
# parents[0] is strategies/, parents[1] is the repo root. This used to say
# parents[2], which pointed a directory above the repo, so the fyers_client and
# common_indicators imports below both failed and the agent could not start.
REPO_ROOT = SCRIPT_DIR.parents[1]
sys.path.insert(0, str(REPO_ROOT / "skills" / "fyers-trading" / "scripts"))
sys.path.insert(0, str(REPO_ROOT / "strategies" / "utils"))

from fyers_client import FyersClient  # noqa: E402
from shared_data_fetcher import SharedDataFetcher  # noqa: E402
from common_indicators import ema, rsi, hma  # noqa: E402
from global_market_fetcher import GlobalMarketFetcher  # noqa: E402

# =============================================================================
# Configuration
# =============================================================================
MARKET_TIMEZONE = ZoneInfo("Asia/Kolkata")
KNOWLEDGE_DIR = SCRIPT_DIR / "knowledge"
STRATEGIES_FILE = KNOWLEDGE_DIR / "strategies.json"
MARKET_WISDOM_FILE = KNOWLEDGE_DIR / "market_wisdom.json"
LOG_PATH = SCRIPT_DIR / "logs" / "agent.log"
POSITIONS_PATH = SCRIPT_DIR / "data" / "positions.json"
ALERTS_LOG = SCRIPT_DIR / "logs" / "alerts.log"

STRATEGIES_DIR = SCRIPT_DIR.parent
EXCEL_DIR = STRATEGIES_DIR / "logs"
EXCEL_DIR.mkdir(parents=True, exist_ok=True)

# The dashboard columns' strategies live beside the older scripts. They are
# loaded by path rather than imported, so the agent and the dashboard run the
# same file for a rule instead of two implementations drifting apart.
STRATEGY_SCRIPTS_DIR = STRATEGIES_DIR / "scripts"

# Strategies the engine delegates to their own source file. Each entry is
# (file, function, adapter) where the adapter turns the source's return value
# into (signal_type, reason) or None. A strategy id absent from this mapping
# is evaluated by the if/elif chain in _evaluate_strategy; one present here is
# never handled there, so the two paths cannot both fire for the same id.
#
# Only strategies that need nothing but 15-minute candles appear, because
# SharedDataFetcher.get_candles returns a 15-minute series and nothing else.
# Anything needing a daily series or 5-minute bars is in strategies.json with
# engine_support="not_wired" and a stated reason.
_STRATEGY_MODULE_CACHE: dict[str, object] = {}


def load_strategy_module(name: str, path: pathlib.Path):
    """Load a strategy script by path, once, and cache it."""
    if name in _STRATEGY_MODULE_CACHE:
        return _STRATEGY_MODULE_CACHE[name]
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load strategy module {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    _STRATEGY_MODULE_CACHE[name] = module
    return module


def _side_signal(result) -> Optional[tuple[str, str]]:
    """Adapt an evaluator that returns (side, details), side being a string.

    The sources spell a side three ways: CE/PE, BUY/SELL, and plain boolean
    for the buy-only crossovers. All of them mean the same two things here.
    """
    if not isinstance(result, tuple) or len(result) != 2:
        return None
    side, details = result
    if isinstance(side, bool):
        if not side:
            return None
        return "CE", _reason_from(details, "")
    side = str(side).upper()
    if side in ("NONE", "", "FLAT"):
        return None
    if side in ("BUY", "CE", "LONG"):
        return "CE", _reason_from(details, "")
    if side in ("SELL", "PE", "PUT", "SHORT"):
        return "PE", _reason_from(details, "")
    return None


def _side_bool(result) -> Optional[tuple[str, str]]:
    """Adapt an evaluator that returns (matched, details) and is buy-only."""
    if not isinstance(result, tuple) or len(result) != 2:
        return None
    matched, details = result
    if not matched:
        return None
    return "CE", _reason_from(details, "")


def _reason_from(details, fallback: str) -> str:
    """A one-line reason for the alert, preferring the source's own words."""
    if not isinstance(details, dict):
        return fallback or ""
    parts = []
    rule = details.get("rule") or details.get("trigger")
    if rule:
        parts.append(str(rule))
    reason = details.get("reason")
    if reason:
        parts.append(str(reason))
    return " - ".join(parts) if parts else (fallback or "signal")


def judged_at(candles: list) -> dt.datetime:
    """When the newest bar closed, in market time.

    The rejection evaluators want the moment being judged, not the wall clock.
    The dashboard passes the same thing while walking a session, and using the
    bar's own close keeps the agent correct on a weekend or a holiday, when the
    newest bar belongs to the last session but the clock says otherwise.
    """
    if candles:
        started = dt.datetime.fromtimestamp(candles[-1].epoch, MARKET_TIMEZONE)
        return started + dt.timedelta(seconds=BAR_SECONDS)
    return dt.datetime.now(MARKET_TIMEZONE)


#: strategy id -> (file, function, adapter). The adapter is called as
#: adapter(evaluator, candles) because the sources disagree on their
#: signatures: some take the bar size, some take the judged moment too.
DELEGATED_STRATEGIES: dict[str, tuple[str, str, object]] = {
    "STRAT_012": (
        "EquityEma10_20_Signals15min.py", "ema10_ema20_signal",
        lambda e, c: _side_signal(e(c, BAR_SECONDS))),
    "STRAT_013": (
        "EquityLowerHighCloseSignal15min.py", "lower_high_close_sell_signal",
        lambda e, c: _side_signal(e(c, BAR_SECONDS))),
    "STRAT_014": (
        "EquityEma15_10_20_50Crossover15min.py", "evaluate_fresh_crossover",
        lambda e, c: _side_bool(e(c))),
    "STRAT_015": (
        "EquityEma15_10_20_50Crossover15min.py",
        "evaluate_ema10_pullback_cross", lambda e, c: _side_bool(e(c))),
    "STRAT_016": (
        "R1PrevHighRejectionStrategy.py", "doji_rejection_signal",
        lambda e, c: _side_signal(e(c, judged_at(c), False))),
    "STRAT_017": (
        "R1PrevHighRejectionStrategy.py", "higher_high_low_rejection_signal",
        lambda e, c: _side_signal(e(c, judged_at(c), False))),
    "STRAT_018": (
        "R1PrevHighRejectionStrategy.py",
        "higher_high_close_rejection_signal",
        lambda e, c: _side_signal(e(c, judged_at(c), False))),
    "STRAT_029": (
        "EquityOrbLowRejectionSignal15min.py", "orb_low_rejection_buy_signal",
        lambda e, c: _side_signal(e(c, judged_at(c), False))),
    "STRAT_030": (
        "EquityDoubleBottomBullishSignal15min.py",
        "double_bottom_bullish_signal",
        # No daily series here, so the previous-day leg is unavailable and only
        # the opening-range leg can carry the signal. The rule reports the leg
        # it used, and strategies.json says the daily leg is inert here.
        lambda e, c: _side_signal(e(c, NO_DAILY, judged_at(c), False))),
    "STRAT_031": (
        "EquityOpenRangeOpeningSignals15min.py",
        "second_candle_gap_ema_sell_signal",
        lambda e, c: _side_signal(e(c, judged_at(c), False))),
}


def _delegated(strategy_id: str, candles: list):
    """Call a delegated strategy's own source, or return None if not one."""
    entry = DELEGATED_STRATEGIES.get(strategy_id)
    if entry is None:
        return None
    file_name, function_name, adapter = entry
    module = load_strategy_module(
        f"rule_agent_strategy_{pathlib.Path(file_name).stem}",
        STRATEGY_SCRIPTS_DIR / file_name,
    )
    evaluator = getattr(module, function_name, None)
    if evaluator is None:
        raise AttributeError(
            f"{file_name} has no evaluator named {function_name}")
    return adapter(evaluator, candles)

# Market hours
MARKET_OPEN = dt.time(9, 15)
MARKET_CLOSE = dt.time(15, 30)

# Bar size of the series the fetcher returns, passed to the evaluators that
# stamp their own candle times.
BAR_SECONDS = 15 * 60

#: The fetcher returns 15-minute candles only, so a strategy that can also read
#: a daily series is handed this instead. The rule must report the leg it could
#: not test rather than treat the missing level as a passed one.
NO_DAILY: list = []

# Alert configuration
ALERT_CONFIG = {
    "console": True,
    "file": True,
    "excel": True,
    "whatsapp": False,  # Set True to enable
    "telegram": False,  # Set True to enable
}


# =============================================================================
# Data Classes
# =============================================================================
@dataclass
class Signal:
    symbol: str
    strategy_id: str
    strategy_name: str
    signal_type: str  # CE or PE
    confidence: float
    reason: str
    current_price: float
    indicators: dict
    timestamp: str
    warnings: list = None  # Market wisdom warnings

    def to_dict(self):
        return asdict(self)

    def to_alert_message(self) -> str:
        """Format signal as alert message."""
        emoji = "🟢" if self.signal_type == "CE" else "🔴"
        confidence_level = "HIGH" if self.confidence >= 0.75 else "MEDIUM" if self.confidence >= 0.6 else "LOW"

        message = f"""
{emoji} *TRADING SIGNAL*

*Strategy:* {self.strategy_name}
*Symbol:* {self.symbol}
*Signal:* {self.signal_type} ({'CALL' if self.signal_type == 'CE' else 'PUT'})
*Confidence:* {confidence_level} ({self.confidence:.0%})
*Price:* ₹{self.current_price:,.2f}
*Time:* {self.timestamp}

*Indicators:*
{chr(10).join(f"- {k}: {v:.2f}" if isinstance(v, float) else f"- {k}: {v}" for k, v in self.indicators.items())}

*Analysis:*
{self.reason}
"""

        # Add warnings if present
        if self.warnings:
            message += "\n⚠️ *MARKET WISDOM WARNINGS:*\n"
            for w in self.warnings:
                severity_emoji = "🔴" if w["severity"] == "CRITICAL" else "🟡" if w["severity"] == "HIGH" else "🔵"
                message += f"\n{severity_emoji} {w['message']}\n"
                message += f"   💡 Action: {w['action']}\n"

        message += f"\n*Action:* {'✅ Consider buying ' + self.signal_type if self.confidence >= 0.7 else '⚠️ Low confidence - review'}"

        return message


@dataclass
class Position:
    symbol: str
    option_type: str
    entry_price: float
    quantity: int
    entry_time: str
    stop_loss: float


# =============================================================================
# Alert System (No LLM)
# =============================================================================
class AlertSystem:
    """Alert system - console, file, Excel, and optional messaging."""

    EXCEL_HEADERS = ["Date", "Time", "Symbol", "Strategy", "SignalType", "Confidence", "Price", "Details"]
    HEADER_FILL = PatternFill(start_color="4472C4", end_color="4472C4", fill_type="solid")
    HEADER_FONT = Font(color="FFFFFF", bold=True)

    def __init__(self, config: dict = None):
        self.config = config or ALERT_CONFIG
        self._ensure_log_files()

    def _ensure_log_files(self):
        """Create log directories and files."""
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        ALERTS_LOG.parent.mkdir(parents=True, exist_ok=True)
        EXCEL_DIR.mkdir(parents=True, exist_ok=True)

    def _get_excel_path(self) -> Path:
        """Get today's Excel file path (new file each day)."""
        today = dt.datetime.now(MARKET_TIMEZONE).strftime("%Y-%m-%d")
        return EXCEL_DIR / f"Agent_Alerts_{today}.xlsx"

    def _ensure_excel_workbook(self, path: Path):
        """Create Excel workbook with headers if it doesn't exist."""
        if path.exists():
            return load_workbook(path)
        wb = Workbook()
        ws = wb.active
        ws.title = "Alerts"
        for col_idx, header in enumerate(self.EXCEL_HEADERS, 1):
            cell = ws.cell(row=1, column=col_idx, value=header)
            cell.fill = self.HEADER_FILL
            cell.font = self.HEADER_FONT
            cell.alignment = Alignment(horizontal="center")
        ws.column_dimensions["A"].width = 12
        ws.column_dimensions["B"].width = 10
        ws.column_dimensions["C"].width = 25
        ws.column_dimensions["D"].width = 30
        ws.column_dimensions["E"].width = 12
        ws.column_dimensions["F"].width = 12
        ws.column_dimensions["G"].width = 12
        ws.column_dimensions["H"].width = 50
        wb.save(path)
        return wb

    def send_alert(self, signal: Signal):
        """Send alert for a signal."""
        message = signal.to_alert_message()

        if self.config.get("console"):
            self._print_console(message)

        if self.config.get("file"):
            self._write_to_file(message)

        if self.config.get("excel"):
            self._write_to_excel(signal)

        if self.config.get("whatsapp"):
            self._send_whatsapp(message)

        if self.config.get("telegram"):
            self._send_telegram(message)

    def _print_console(self, message: str):
        """Print alert to console with formatting."""
        print("\n" + "=" * 60)
        print(f"🚨 ALERT [{dt.datetime.now(MARKET_TIMEZONE).strftime('%H:%M:%S')}]")
        print("=" * 60)
        print(message)
        print("=" * 60 + "\n")

    def _write_to_file(self, message: str):
        """Write alert to log file."""
        timestamp = dt.datetime.now(MARKET_TIMEZONE).isoformat()
        with open(ALERTS_LOG, "a", encoding="utf-8") as f:
            f.write(f"\n{'='*60}\n")
            f.write(f"ALERT [{timestamp}]\n")
            f.write(f"{'='*60}\n")
            f.write(message + "\n")

    def _write_to_excel(self, signal: Signal):
        """Write alert to today's Excel file."""
        try:
            excel_path = self._get_excel_path()
            wb = self._ensure_excel_workbook(excel_path)
            ws = wb.active

            now = dt.datetime.now(MARKET_TIMEZONE)
            row = ws.max_row + 1
            values = [
                now.strftime("%Y-%m-%d"),
                now.strftime("%H:%M:%S"),
                signal.symbol,
                signal.strategy_name,
                signal.signal_type,
                f"{signal.confidence:.0%}",
                f"{signal.current_price:.2f}",
                signal.reason,
            ]
            for col_idx, value in enumerate(values, 1):
                cell = ws.cell(row=row, column=col_idx, value=value)
                cell.alignment = Alignment(horizontal="left")

            wb.save(excel_path)
        except Exception as e:
            print(f"Excel log error: {e}", file=sys.stderr)

    def _send_whatsapp(self, message: str):
        """Send WhatsApp message (requires setup)."""
        # TODO: Implement WhatsApp Business API
        pass

    def _send_telegram(self, message: str):
        """Send Telegram message (requires setup)."""
        # TODO: Implement Telegram Bot API
        pass


# =============================================================================
# Market Wisdom (Remembered Rules)
# =============================================================================
class MarketWisdom:
    """Loads and checks market wisdom rules before placing orders."""

    def __init__(self):
        self.rules = self._load_rules()

    def _load_rules(self) -> dict:
        """Load market wisdom rules from JSON."""
        if MARKET_WISDOM_FILE.exists():
            return json.loads(MARKET_WISDOM_FILE.read_text())
        return {"categories": []}

    def check_all(self, context: dict) -> list[dict]:
        """Check all rules and return applicable warnings.

        Args:
            context: Dict with market context like:
                - rsi_eod: End of day RSI
                - consecutive_holidays: Number of consecutive holidays
                - open_positions: Number of open positions
                - daily_pnl: Daily P&L percentage
                - current_hour: Current hour
                - minutes_to_close: Minutes to market close
                - signal_type: CE or PE
                - volume: Current volume
                - avg_volume_20d: 20-day average volume
        """
        warnings = []

        for category in self.rules.get("categories", []):
            for rule in category.get("rules", []):
                warning = self._evaluate_rule(rule, context)
                if warning:
                    warnings.append(warning)

        return warnings

    def _evaluate_rule(self, rule: dict, context: dict) -> dict | None:
        """Evaluate a single rule against context."""
        condition = rule.get("condition", "")
        rule_id = rule.get("id", "")

        try:
            # RSI rules
            if rule_id == "RSI_OVERSOLD" and context.get("rsi_eod", 50) < 30:
                return self._format_warning(rule, context["rsi_eod"])
            elif rule_id == "RSI_OVERBOUGHT" and context.get("rsi_eod", 50) > 70:
                return self._format_warning(rule, context["rsi_eod"])
            elif rule_id == "RSI_EXTREME_OVERSOLD" and context.get("rsi_eod", 50) < 20:
                return self._format_warning(rule, context["rsi_eod"])
            elif rule_id == "RSI_EXTREME_OVERBOUGHT" and context.get("rsi_eod", 50) > 80:
                return self._format_warning(rule, context["rsi_eod"])

            # Holiday reversal
            elif rule_id == "HOLIDAY_REVERSAL" and context.get("consecutive_holidays", 0) >= 3:
                return self._format_warning(rule)

            # Position management
            elif rule_id == "MAX_POSITIONS" and context.get("open_positions", 0) >= 3:
                return self._format_warning(rule, context["open_positions"])
            elif rule_id == "DAILY_LOSS_LIMIT" and context.get("daily_pnl", 0) < -2:
                return self._format_warning(rule, abs(context["daily_pnl"]))
            elif rule_id == "CONSECUTIVE_LOSSES" and context.get("consecutive_losses", 0) >= 3:
                return self._format_warning(rule, context["consecutive_losses"])

            # Time rules
            elif rule_id == "MARKET_OPEN_VOLATILITY" and context.get("minutes_since_open", 999) < 15:
                return self._format_warning(rule)
            elif rule_id == "LAST_HOUR" and context.get("minutes_to_close", 999) < 60:
                return self._format_warning(rule)
            elif rule_id == "NO_NEW_TRADES" and context.get("minutes_to_close", 999) < 30:
                return self._format_warning(rule)

            # Technical rules
            elif rule_id == "HIGH_RSI_WITH_CE" and context.get("rsi", 50) > 70 and context.get("signal_type") == "CE":
                return self._format_warning(rule, context["rsi"])
            elif rule_id == "LOW_RSI_WITH_PE" and context.get("rsi", 50) < 30 and context.get("signal_type") == "PE":
                return self._format_warning(rule, context["rsi"])
            elif rule_id == "VOLUME_CONFIRMATION" and context.get("volume", 0) < context.get("avg_volume_20d", 1) * 0.5:
                pct = (context.get("volume", 0) / context.get("avg_volume_20d", 1)) * 100
                return self._format_warning(rule, f"{pct:.0f}")

            # Crude Oil rules
            elif rule_id == "CRUDE_SPIKE" and context.get("crude_change_1d", 0) > 2:
                return self._format_warning(rule, f"{context['crude_change_1d']:.1f}")
            elif rule_id == "CRUDE_CRASH" and context.get("crude_change_1d", 0) < -2:
                return self._format_warning(rule, f"{context['crude_change_1d']:.1f}")
            elif rule_id == "CRUDE_ABOVE_90" and context.get("crude_price", 0) > 90:
                return self._format_warning(rule)
            elif rule_id == "CRUDE_BELOW_70" and context.get("crude_price", 0) < 70 and context.get("crude_price", 0) > 0:
                return self._format_warning(rule)
            elif rule_id == "CRUDE_7DAY_TREND" and context.get("crude_change_7d", 0) > 10:
                return self._format_warning(rule, f"{context['crude_change_7d']:.1f}")

            # USDINR rules
            elif rule_id == "INR_WEAKENING" and context.get("inr_weakening", 0) > 0.5:
                return self._format_warning(rule, f"{context['inr_weakening']:.1f}")
            elif rule_id == "INR_STRENGTHENING" and context.get("usdinr_change_1d", 0) < -0.5:
                return self._format_warning(rule, f"{abs(context['usdinr_change_1d']):.1f}")
            elif rule_id == "USDAbove85" and context.get("usdinr_rate", 0) > 85:
                return self._format_warning(rule)
            elif rule_id == "USD Below 80" and context.get("usdinr_rate", 100) < 80:
                return self._format_warning(rule)
            elif rule_id == "INR_SHARP_MOVE" and context.get("usdinr_change_1d", 0) > 1:
                return self._format_warning(rule, f"{context['usdinr_change_1d']:.1f}")

            # Global market rules
            elif rule_id == "GIFT_NIFTY_GAP" and context.get("gift_nifty_gap", 0) > 1:
                return self._format_warning(rule, f"{context['gift_nifty_gap']:.1f}")
            elif rule_id == "GIFT_NIFTY_GAP_DOWN" and context.get("gift_nifty_gap", 0) < -1:
                return self._format_warning(rule, f"{context['gift_nifty_gap']:.1f}")
            elif rule_id == "VIX_HIGH" and context.get("india_vix", 15) > 20:
                return self._format_warning(rule, f"{context['india_vix']:.1f}")

        except Exception:
            pass

        return None

    def _format_warning(self, rule: dict, *args) -> dict:
        """Format warning message with arguments."""
        alert = rule.get("alert", "")
        if args:
            try:
                alert = alert.format(*args)
            except:
                pass

        return {
            "rule_id": rule.get("id"),
            "severity": rule.get("severity", "MEDIUM"),
            "message": alert,
            "action": rule.get("action", "Review before proceeding")
        }

    def get_pre_trade_checklist(self, signal: Signal, context: dict) -> list[dict]:
        """Get pre-trade checklist warnings for a signal."""
        # Add signal-specific context
        check_context = {
            **context,
            "signal_type": signal.signal_type,
            "rsi": signal.indicators.get("rsi", 50),
        }
        return self.check_all(check_context)


# =============================================================================
# Strategy Engine
# =============================================================================
class StrategyEngine:
    """Evaluates trading strategies based on rules."""

    def __init__(self):
        self.strategies = self._load_strategies()

    def _load_strategies(self) -> dict:
        """Load strategies from knowledge base."""
        if STRATEGIES_FILE.exists():
            return json.loads(STRATEGIES_FILE.read_text())
        return {"strategies": []}

    def evaluate(self, symbol: str, candles: list) -> list[Signal]:
        """Evaluate all strategies for a symbol."""
        signals = []

        for strategy in self.strategies.get("strategies", []):
            signal = self._evaluate_strategy(strategy, symbol, candles)
            if signal:
                signals.append(signal)

        return signals

    def _evaluate_strategy(self, strategy: dict, symbol: str, candles: list) -> Optional[Signal]:
        """Evaluate a single strategy."""
        # The knowledge base is authoritative. A strategy marked not_wired is
        # skipped even where a branch or a delegation exists, because the
        # reason it is not wired is that the engine cannot feed it correctly:
        # a 5-minute rule read off 15-minute candles, or one needing a daily
        # series the fetcher does not return. Evaluating it anyway would emit
        # a confident alert for a rule that is not the one being described.
        if strategy.get("engine_support") == "not_wired":
            return None

        # Delegated strategies run their own source file. This happens before
        # the length guard and the indicator block below, because those exist
        # for the hand-written chain and a delegated evaluator may need far
        # less history (the lower-high rule needs two bars).
        delegated = _delegated(strategy["id"], candles)
        if delegated is not None:
            signal_type, reason = delegated
            return Signal(
                symbol, strategy["id"], strategy["name"], signal_type,
                strategy.get("confidence", 0.7),
                reason or strategy["name"],
                candles[-1].close if candles else 0.0,
                {}, self._now(),
            )
        if strategy["id"] in DELEGATED_STRATEGIES:
            # Delegated and silent: the source ran and did not fire. Falling
            # through to the chain would give the id a second chance at being
            # evaluated by a different set of rules.
            return None

        if len(candles) < 35:
            return None

        closes = [c.close for c in candles]
        curr = candles[-1]
        prev = candles[-2] if len(candles) > 1 else curr

        # Calculate indicators
        ema9 = ema(closes, 9)[-1]
        ema26 = ema(closes, 26)[-1]
        ema35 = ema(closes, 35)[-1]
        ema10 = ema(closes, 10)[-1]
        ema20 = ema(closes, 20)[-1]
        ema30 = ema(closes, 30)[-1]
        ema50 = ema(closes, 50)[-1]
        rsi_val = rsi(closes)[-1]
        hma21_val = hma(closes, 21)[-1]

        # Previous values for crossover detection
        prev_ema9 = ema(closes, 9)[-2] if len(closes) > 1 else None
        prev_ema26 = ema(closes, 26)[-2] if len(closes) > 1 else None
        prev_ema10 = ema(closes, 10)[-2] if len(closes) > 1 else None
        prev_ema50 = ema(closes, 50)[-2] if len(closes) > 1 else None

        if not all([ema9, ema26, rsi_val]):
            return None

        strat_id = strategy["id"]
        indicators = {
            "ema9": ema9, "ema26": ema26, "ema35": ema35,
            "ema10": ema10, "ema20": ema20, "ema30": ema30, "ema50": ema50,
            "rsi": rsi_val, "hma21": hma21_val
        }

        # STRAT_001: ORB Breakout
        if strat_id == "STRAT_001":
            orb_high = max(c.high for c in candles[:6])  # First 30 min high
            orb_low = min(c.low for c in candles[:6])    # First 30 min low

            if curr.close > orb_high:
                return Signal(symbol, strat_id, strategy["name"], "CE",
                            strategy.get("confidence", 0.7),
                            f"ORB Breakout: close={curr.close} > orb_high={orb_high}",
                            curr.close, indicators, self._now())
            elif curr.close < orb_low:
                return Signal(symbol, strat_id, strategy["name"], "PE",
                            strategy.get("confidence", 0.7),
                            f"ORB Breakdown: close={curr.close} < orb_low={orb_low}",
                            curr.close, indicators, self._now())

        # STRAT_005: EMA Crossover with RSI
        elif strat_id == "STRAT_005":
            if prev_ema9 and prev_ema26:
                if prev_ema9 <= prev_ema26 and ema9 > ema26 and curr.close > ema9 and rsi_val > 55:
                    return Signal(symbol, strat_id, strategy["name"], "CE",
                                strategy.get("confidence", 0.72),
                                f"EMA Crossover: 9 EMA ({ema9:.2f}) crossed above 26 EMA ({ema26:.2f}), RSI={rsi_val:.1f}",
                                curr.close, indicators, self._now())

        # STRAT_006: Index Rejection - NIFTY/SENSEX
        elif strat_id == "STRAT_006":
            # PE signal
            if curr.high >= ema26 and curr.close < ema26 and ema9 < ema26 and rsi_val <= 50:
                return Signal(symbol, strat_id, strategy["name"], "PE",
                            strategy.get("confidence", 0.75),
                            f"Bearish rejection at 26 EMA: close={curr.close} < ema26={ema26:.2f}, RSI={rsi_val:.1f}",
                            curr.close, indicators, self._now())

            # CE signal
            if curr.low <= ema9 and curr.close > ema26 and rsi_val > 50:
                return Signal(symbol, strat_id, strategy["name"], "CE",
                            strategy.get("confidence", 0.75),
                            f"Bullish bounce at 9 EMA: close={curr.close} > ema26={ema26:.2f}, RSI={rsi_val:.1f}",
                            curr.close, indicators, self._now())

        # STRAT_007: Index Rejection - BANKNIFTY
        elif strat_id == "STRAT_007":
            # PE signal
            if curr.high >= ema35 and curr.close < ema35 and ema9 < ema35 and rsi_val <= 50:
                return Signal(symbol, strat_id, strategy["name"], "PE",
                            strategy.get("confidence", 0.75),
                            f"Bearish rejection at 35 EMA: close={curr.close} < ema35={ema35:.2f}, RSI={rsi_val:.1f}",
                            curr.close, indicators, self._now())

            # CE signal
            if curr.low <= ema9 and curr.close > ema35 and rsi_val > 50:
                return Signal(symbol, strat_id, strategy["name"], "CE",
                            strategy.get("confidence", 0.75),
                            f"Bullish bounce at 9 EMA: close={curr.close} > ema35={ema35:.2f}, RSI={rsi_val:.1f}",
                            curr.close, indicators, self._now())

        # STRAT_008: EMA 10/20/30 Crossover
        elif strat_id == "STRAT_008":
            if ema10 and ema20 and ema30:
                if ema10 > ema20 > ema30 and rsi_val > 55:
                    return Signal(symbol, strat_id, strategy["name"], "CE",
                                strategy.get("confidence", 0.70),
                                f"Triple EMA bullish: EMA10={ema10:.2f} > EMA20={ema20:.2f} > EMA30={ema30:.2f}, RSI={rsi_val:.1f}",
                                curr.close, indicators, self._now())

        # STRAT_009: EMA 10/50 Crossover
        elif strat_id == "STRAT_009":
            if prev_ema10 and prev_ema50:
                if prev_ema10 <= prev_ema50 and ema10 > ema50 and rsi_val > 55:
                    return Signal(symbol, strat_id, strategy["name"], "CE",
                                strategy.get("confidence", 0.68),
                                f"Golden cross: EMA10 ({ema10:.2f}) crossed above EMA50 ({ema50:.2f}), RSI={rsi_val:.1f}",
                                curr.close, indicators, self._now())

        # STRAT_10: Second Candle Breakout (CE)
        elif strat_id == "STRAT_10":
            signal = self._evaluate_second_candle_breakout(symbol, strat_id, candles, strategy, indicators)
            if signal:
                return signal

        # STRAT_11: Bearish Reversal - Two Bullish Candles (PE)
        elif strat_id == "STRAT_11":
            signal = self._evaluate_bearish_reversal(symbol, strat_id, candles, strategy, indicators)
            if signal:
                return signal

        return None

    def _find_candle_by_time(self, candles: list, hour: int, minute: int):
        """Find a candle that starts at the given time (hour, minute) today."""
        today = dt.date.today()
        for candle in candles:
            candle_dt = dt.datetime.fromtimestamp(candle.epoch, MARKET_TIMEZONE)
            if candle_dt.date() == today and candle_dt.time() == dt.time(hour, minute):
                return candle
        return None

    def _get_today_candles(self, candles: list) -> list:
        """Filter candles to only today's data."""
        today = dt.date.today()
        return [c for c in candles if dt.datetime.fromtimestamp(c.epoch, MARKET_TIMEZONE).date() == today]

    def _evaluate_second_candle_breakout(self, symbol: str, strat_id: str, candles: list,
                                          strategy: dict, indicators: dict) -> Optional[Signal]:
        """Evaluate Second Candle Breakout strategy (STRAT_10).

        CE signal:
          - Bullish 2nd candle (9:30): subsequent candle high > 2nd candle high
          - Bearish 2nd candle: 3rd candle (9:45) close > 2nd candle open
        """
        if len(candles) < 3:
            return None

        second_candle = self._find_candle_by_time(candles, 9, 30)
        third_candle = self._find_candle_by_time(candles, 9, 45)

        if second_candle is None:
            return None

        today_candles = self._get_today_candles(candles)
        if len(today_candles) < 3:
            return None

        sc_high = second_candle.high
        sc_low = second_candle.low
        sc_open = second_candle.open
        sc_close = second_candle.close
        sc_dt = dt.datetime.fromtimestamp(second_candle.epoch, MARKET_TIMEZONE)
        is_bullish = sc_close > sc_open

        if is_bullish:
            # Bullish: any subsequent candle high breaks above second candle high
            curr_candle = today_candles[-1]
            curr_high = curr_candle.high
            curr_dt = dt.datetime.fromtimestamp(curr_candle.epoch, MARKET_TIMEZONE)

            if curr_dt <= sc_dt:
                return None

            if curr_high > sc_high:
                return Signal(symbol, strat_id, strategy["name"], "CE",
                            strategy.get("confidence", 0.75),
                            f"2nd candle BULLISH breakout: curr_high={curr_high} > sc_high={sc_high}, SL={sc_low}",
                            curr_candle.close, indicators, self._now())
        else:
            # Bearish: third candle must close above second candle open
            if third_candle is None:
                return None

            third_close = third_candle.close
            third_dt = dt.datetime.fromtimestamp(third_candle.epoch, MARKET_TIMEZONE)

            if third_dt <= sc_dt:
                return None

            if third_close > sc_open:
                return Signal(symbol, strat_id, strategy["name"], "CE",
                            strategy.get("confidence", 0.75),
                            f"2nd candle BEARISH reversal: 3rd_close={third_close} > sc_open={sc_open}, SL={sc_low}",
                            third_close, indicators, self._now())

        return None

    def _evaluate_bearish_reversal(self, symbol: str, strat_id: str, candles: list,
                                    strategy: dict, indicators: dict) -> Optional[Signal]:
        """Evaluate Bearish Reversal strategy (STRAT_11).

        PE signal when:
          - 1st candle (9:15) bullish
          - 2nd candle (9:30) bullish
          - 3rd candle (9:45) close < 2nd candle high AND shows weakness
        """
        if len(candles) < 3:
            return None

        first_candle = self._find_candle_by_time(candles, 9, 15)
        second_candle = self._find_candle_by_time(candles, 9, 30)
        third_candle = self._find_candle_by_time(candles, 9, 45)

        if first_candle is None or second_candle is None or third_candle is None:
            return None

        first_bullish = first_candle.close > first_candle.open
        second_bullish = second_candle.close > second_candle.open

        if not first_bullish or not second_bullish:
            return None

        sc_high = second_candle.high
        sc_dt = dt.datetime.fromtimestamp(second_candle.epoch, MARKET_TIMEZONE)

        third_close = third_candle.close
        third_open = third_candle.open
        third_high = third_candle.high
        third_dt = dt.datetime.fromtimestamp(third_candle.epoch, MARKET_TIMEZONE)

        if third_dt <= sc_dt:
            return None

        close_below_second_high = third_close < sc_high
        body = abs(third_close - third_open)
        upper_wick = third_high - max(third_close, third_open)
        rejection = upper_wick > body
        bearish_close = third_close < third_open
        lower_high = third_high < sc_high

        weakness = rejection or bearish_close or lower_high

        if close_below_second_high and weakness:
            reason_parts = []
            if rejection:
                reason_parts.append(f"upper_wick={upper_wick:.2f} > body={body:.2f}")
            if bearish_close:
                reason_parts.append("bearish close")
            if lower_high:
                reason_parts.append(f"high={third_high} < sc_high={sc_high}")

            return Signal(symbol, strat_id, strategy["name"], "PE",
                        strategy.get("confidence", 0.72),
                        f"Bearish reversal: 3rd_close={third_close} < sc_high={sc_high}, weakness: {', '.join(reason_parts)}",
                        third_close, indicators, self._now())

        return None

    def _now(self) -> str:
        return dt.datetime.now(MARKET_TIMEZONE).isoformat()


# =============================================================================
# Trading Agent
# =============================================================================
class TradingAgent:
    """Rule-based trading agent - No LLM required."""

    def __init__(self):
        self.client = FyersClient()
        self.fetcher = SharedDataFetcher()
        self.strategy_engine = StrategyEngine()
        self.market_wisdom = MarketWisdom()
        self.alert_system = AlertSystem()
        self.positions = self._load_positions()

        print(f"\n🤖 Rule-Based Trading Agent Initialized")
        print(f"📊 Loaded {len(self.strategy_engine.strategies.get('strategies', []))} strategies")
        print(f"🧠 Loaded {len(self.market_wisdom.rules.get('categories', []))} wisdom categories")
        print(f"⏰ Market: {'OPEN' if self._is_market_open() else 'CLOSED'}")

    def _load_positions(self) -> list[Position]:
        """Load positions from file."""
        if POSITIONS_PATH.exists():
            try:
                data = json.loads(POSITIONS_PATH.read_text())
                return [Position(**p) for p in data]
            except:
                return []
        return []

    def _save_positions(self):
        """Save positions to file."""
        POSITIONS_PATH.parent.mkdir(parents=True, exist_ok=True)
        data = [asdict(p) for p in self.positions]
        POSITIONS_PATH.write_text(json.dumps(data, indent=2))

    def _is_market_open(self) -> bool:
        """Check if market is currently open."""
        now = dt.datetime.now(MARKET_TIMEZONE)
        return now.weekday() < 5 and MARKET_OPEN <= now.time() <= MARKET_CLOSE

    def scan_symbol(self, symbol: str, force_refresh: bool = False) -> list[Signal]:
        """Scan a single symbol for signals."""
        candles = self.fetcher.get_candles(symbol, force_refresh=force_refresh)
        if not candles:
            return []

        return self.strategy_engine.evaluate(symbol, candles)

    def scan_market(self, symbols: list[str] = None, force_refresh: bool = False) -> list[Signal]:
        """Scan multiple symbols for signals."""
        if symbols is None:
            symbols = self.fetcher.get_stocks()[:20]  # Default scan list

        all_signals = []

        for i, symbol in enumerate(symbols):
            try:
                if force_refresh:
                    print(f"[{i+1}/{len(symbols)}] Fetching {symbol}...", file=sys.stderr)
                signals = self.scan_symbol(symbol, force_refresh=force_refresh)
                all_signals.extend(signals)
            except Exception as e:
                print(f"Error scanning {symbol}: {e}", file=sys.stderr)

        return all_signals

    def process_signals(self, signals: list[Signal]):
        """Process and alert for signals with market wisdom checks."""
        # Check for gap-blocked state
        context = self._get_market_context()
        if context.get("gap_blocked"):
            print(f"\n🚫 GAP BLOCKED: {context.get('gap_reason', 'NIFTY gap too large')}")
            print("   Skipping all new entries until gap fills.")
            return

        for signal in signals:
            # Only alert for high confidence signals
            if signal.confidence >= 0.7:
                # Get market wisdom warnings
                context = self._get_market_context()
                warnings = self.market_wisdom.get_pre_trade_checklist(signal, context)

                # Add warnings to signal
                if warnings:
                    signal.warnings = warnings
                    print(f"\n⚠️ MARKET WISDOM WARNINGS for {signal.symbol}:")
                    for w in warnings:
                        severity_emoji = "🔴" if w["severity"] == "CRITICAL" else "🟡" if w["severity"] == "HIGH" else "🔵"
                        print(f"{severity_emoji} {w['message']}")
                        print(f"   Action: {w['action']}")

                self.alert_system.send_alert(signal)

    def _get_market_context(self) -> dict:
        """Get current market context for wisdom checks."""
        now = dt.datetime.now(MARKET_TIMEZONE)
        market_open = dt.time(9, 15)
        market_close = dt.time(15, 30)

        minutes_since_open = 0
        if now.time() >= market_open:
            open_dt = now.replace(hour=9, minute=15, second=0)
            minutes_since_open = int((now - open_dt).total_seconds() / 60)

        minutes_to_close = 999
        if now.time() <= market_close:
            close_dt = now.replace(hour=15, minute=15, second=0)
            minutes_to_close = int((close_dt - now).total_seconds() / 60)

        # Fetch global market data
        global_context = self._get_global_context()

        return {
            "current_hour": now.hour,
            "minutes_since_open": minutes_since_open,
            "minutes_to_close": minutes_to_close,
            "open_positions": len(self.positions),
            "daily_pnl": self._calculate_daily_pnl(),
            "consecutive_losses": self._get_consecutive_losses(),
            "consecutive_holidays": self._get_consecutive_holidays(),
            **global_context,
        }

    def _get_global_context(self) -> dict:
        """Fetch global market context (crude, USDINR, VIX, gap)."""
        try:
            fetcher = GlobalMarketFetcher()
            crude = fetcher.get_crude_oil()
            usdinr = fetcher.get_usdinr()
            vix = fetcher.get_india_vix()
            gap_ctx = fetcher.get_gap_context(threshold_pct=0.5)

            context = {}

            if crude:
                context["crude_price"] = crude.get("ltp", 0)
                context["crude_change_1d"] = crude.get("change_pct", 0)

            if usdinr:
                context["usdinr_rate"] = usdinr.get("ltp", 0)
                context["usdinr_change_1d"] = usdinr.get("change_pct", 0)
                # INR weakening = USDINR going up = negative
                context["inr_weakening"] = max(0, usdinr.get("change_pct", 0))

            if vix:
                context["india_vix"] = vix.get("ltp", 15)

            # Merge gap context
            context.update(gap_ctx)

            return context
        except Exception as e:
            print(f"Error fetching global data: {e}", file=sys.stderr)
            return {}

    def _calculate_daily_pnl(self) -> float:
        """Calculate daily P&L percentage."""
        # TODO: Implement actual P&L calculation
        return 0.0

    def _get_consecutive_losses(self) -> int:
        """Get consecutive loss count."""
        # TODO: Implement from trade history
        return 0

    def _get_consecutive_holidays(self) -> int:
        """Get consecutive holidays count."""
        # TODO: Implement from calendar
        return 0

    def get_status(self) -> dict:
        """Get agent status."""
        return {
            "market_open": self._is_market_open(),
            "strategies_loaded": len(self.strategy_engine.strategies.get("strategies", [])),
            "positions": len(self.positions),
            "last_scan": self._now(),
        }

    def show_global_markets(self):
        """Show global market indicators."""
        fetcher = GlobalMarketFetcher()
        print(fetcher.format_report())

    def _now(self) -> str:
        return dt.datetime.now(MARKET_TIMEZONE).isoformat()

    def run_scan(self, symbols: list[str] = None, force_refresh: bool = False):
        """Run a single market scan."""
        print(f"\n📡 Scanning markets at {self._now()}...")

        signals = self.scan_market(symbols, force_refresh=force_refresh)

        if signals:
            print(f"\n📊 Found {len(signals)} signals:")
            for s in signals:
                print(f"\n{s.to_alert_message()}")
            self.process_signals(signals)
        else:
            print("\n✅ No signals found")

        return signals

    def run_monitor(self, interval_seconds: int = 300):
        """Run continuous market monitoring."""
        print(f"\n🚀 Starting Market Monitor")
        print(f"📅 Date: {dt.datetime.now(MARKET_TIMEZONE).strftime('%Y-%m-%d')}")
        print(f"⏰ Market: {'OPEN' if self._is_market_open() else 'CLOSED'}")
        print(f"🔄 Scan interval: {interval_seconds}s")
        print(f"{'='*60}\n")

        while self._is_market_open():
            try:
                signals = self.scan_market()
                self.process_signals(signals)

                print(f"[{dt.datetime.now(MARKET_TIMEZONE).strftime('%H:%M:%S')}] "
                      f"Scanned: {len(signals)} signals")

            except Exception as e:
                print(f"Error: {e}", file=sys.stderr)

            time.sleep(interval_seconds)

        print("\n⏹️ Monitor stopped - Market closed")


# =============================================================================
# CLI Interface
# =============================================================================
def main():
    """Main entry point."""
    import argparse

    parser = argparse.ArgumentParser(description="Rule-Based Trading Agent (No LLM)")
    parser.add_argument("--scan", action="store_true", help="Run single market scan")
    parser.add_argument("--monitor", action="store_true", help="Start continuous monitoring")
    parser.add_argument("--symbols", nargs="+", help="Specific symbols to scan")
    parser.add_argument("--status", action="store_true", help="Show agent status")
    parser.add_argument("--force-refresh", action="store_true", help="Force fresh data from API")
    parser.add_argument("--global", action="store_true", dest="show_global", help="Show global market data (Crude, DXY, VIX)")
    args = parser.parse_args()

    agent = TradingAgent()

    if args.status:
        status = agent.get_status()
        print(json.dumps(status, indent=2))

    elif args.show_global:
        agent.show_global_markets()

    elif args.monitor:
        agent.run_monitor()

    elif args.scan:
        agent.run_scan(args.symbols, force_refresh=args.force_refresh)

    else:
        # Default: single scan
        agent.run_scan(args.symbols, force_refresh=args.force_refresh)


if __name__ == "__main__":
    main()
