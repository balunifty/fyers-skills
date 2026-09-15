#!/usr/bin/env python3
"""Rule-Based Trading Agent - No LLM Required.

Pure rule-based agent that monitors markets and alerts when strategy conditions are met.
Uses your existing strategies without any LLM dependency.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import pathlib
import sqlite3
import sys
import time
from dataclasses import dataclass, asdict
from typing import Optional
from zoneinfo import ZoneInfo

SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parents[2]
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

# Market hours
MARKET_OPEN = dt.time(9, 15)
MARKET_CLOSE = dt.time(15, 15)

# Alert configuration
ALERT_CONFIG = {
    "console": True,
    "file": True,
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
    """Simple alert system - console, file, and optional messaging."""

    def __init__(self, config: dict = None):
        self.config = config or ALERT_CONFIG
        self._ensure_log_files()

    def _ensure_log_files(self):
        """Create log directories and files."""
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        ALERTS_LOG.parent.mkdir(parents=True, exist_ok=True)

    def send_alert(self, signal: Signal):
        """Send alert for a signal."""
        message = signal.to_alert_message()

        if self.config.get("console"):
            self._print_console(message)

        if self.config.get("file"):
            self._write_to_file(message)

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
        market_close = dt.time(15, 15)

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
        """Fetch global market context (crude, USDINR, VIX)."""
        try:
            fetcher = GlobalMarketFetcher()
            crude = fetcher.get_crude_oil()
            usdinr = fetcher.get_usdinr()
            vix = fetcher.get_india_vix()

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
