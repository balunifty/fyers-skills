#!/usr/bin/env python3
"""FYERS API Tools for Trading Agent.

Tools that the agent can use to interact with FYERS API.
"""
from __future__ import annotations

import datetime as dt
import json
import pathlib
import sys
from dataclasses import dataclass, asdict
from typing import Callable
from zoneinfo import ZoneInfo

SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parents[2]
sys.path.insert(0, str(REPO_ROOT / "skills" / "fyers-trading" / "scripts"))
sys.path.insert(0, str(REPO_ROOT / "strategies" / "utils"))

from fyers_client import FyersClient  # noqa: E402
from shared_data_fetcher import SharedDataFetcher  # noqa: E402
from common_indicators import ema, rsi, hma  # noqa: E402

# =============================================================================
# Configuration
# =============================================================================
MARKET_TIMEZONE = ZoneInfo("Asia/Kolkata")
KNOWLEDGE_DIR = SCRIPT_DIR / "knowledge"
STRATEGIES_FILE = KNOWLEDGE_DIR / "strategies.json"


# =============================================================================
# Data Classes
# =============================================================================
@dataclass
class ToolResult:
    success: bool
    data: dict | list | None
    error: str | None = None

    def to_dict(self):
        return asdict(self)


# =============================================================================
# FYERS API Tools
# =============================================================================
class FyersTools:
    """Collection of tools for FYERS API interaction."""

    def __init__(self):
        self.client = FyersClient()
        self.fetcher = SharedDataFetcher()
        self.strategies = self._load_strategies()

    def _load_strategies(self) -> dict:
        """Load strategies from knowledge base."""
        if STRATEGIES_FILE.exists():
            return json.loads(STRATEGIES_FILE.read_text())
        return {"strategies": []}

    # -------------------------------------------------------------------------
    # Market Data Tools
    # -------------------------------------------------------------------------
    def get_candles(self, symbol: str, timeframe: str = "15", limit: int = 50) -> ToolResult:
        """Get OHLC candle data for a symbol.

        Args:
            symbol: FYERS symbol (e.g., "NSE:RELIANCE-EQ")
            timeframe: "5" or "15" minutes
            limit: Number of candles
        """
        try:
            candles = self.fetcher.get_candles(symbol)
            if not candles:
                return ToolResult(False, None, f"No data found for {symbol}")

            data = [
                {
                    "epoch": c.epoch,
                    "time": dt.datetime.fromtimestamp(c.epoch, MARKET_TIMEZONE).isoformat(),
                    "open": c.open,
                    "high": c.high,
                    "low": c.low,
                    "close": c.close,
                    "volume": c.volume
                }
                for c in candles[-limit:]
            ]
            return ToolResult(True, data)
        except Exception as e:
            return ToolResult(False, None, str(e))

    def get_option_chain(self, symbol: str, strike_count: int = 10) -> ToolResult:
        """Get option chain for a symbol.

        Args:
            symbol: Index/stock symbol (e.g., "NSE:NIFTY50-INDEX")
            strike_count: Number of strikes to fetch
        """
        try:
            response = self.client.option_chain(symbol, strikecount=strike_count, greeks=False)
            if response.get("s") != "ok":
                return ToolResult(False, None, f"API error: {response}")

            data = response.get("data", {})
            return ToolResult(True, {
                "expiry": data.get("expiry"),
                "call_oi": data.get("callOi"),
                "put_oi": data.get("putOi"),
                "chain": data.get("optionsChain", [])[:20]  # Limit for readability
            })
        except Exception as e:
            return ToolResult(False, None, str(e))

    def get_quotes(self, symbols: list[str]) -> ToolResult:
        """Get current quotes for symbols.

        Args:
            symbols: List of FYERS symbols
        """
        try:
            response = self.client.quotes(symbols)
            if response.get("s") != "ok":
                return ToolResult(False, None, f"API error: {response}")

            quotes = []
            for item in response.get("d", []):
                v = item.get("v", {})
                quotes.append({
                    "symbol": item.get("symbol"),
                    "ltp": v.get("lp"),
                    "open": v.get("open"),
                    "high": v.get("high"),
                    "low": v.get("low"),
                    "close": v.get("prev_close"),
                    "volume": v.get("vol_traded_today"),
                    "change": v.get("change_percent"),
                })
            return ToolResult(True, quotes)
        except Exception as e:
            return ToolResult(False, None, str(e))

    # -------------------------------------------------------------------------
    # Indicator Tools
    # -------------------------------------------------------------------------
    def calculate_indicators(self, symbol: str, indicators: list[str] = None) -> ToolResult:
        """Calculate technical indicators for a symbol.

        Args:
            symbol: FYERS symbol
            indicators: List of indicators ["ema9", "ema26", "rsi", "hma21"]
        """
        if indicators is None:
            indicators = ["ema9", "ema26", "ema35", "rsi", "hma21"]

        try:
            candles = self.fetcher.get_candles(symbol)
            if not candles or len(candles) < 35:
                return ToolResult(False, None, f"Insufficient data for {symbol}")

            closes = [c.close for c in candles]
            result = {"symbol": symbol, "indicators": {}}

            for ind in indicators:
                if ind.startswith("ema"):
                    period = int(ind.replace("ema", ""))
                    values = ema(closes, period)
                    result["indicators"][ind] = values[-1] if values[-1] else None
                elif ind == "rsi":
                    values = rsi(closes)
                    result["indicators"]["rsi"] = values[-1] if values[-1] else None
                elif ind.startswith("hma"):
                    period = int(ind.replace("hma", ""))
                    values = hma(closes, period)
                    result["indicators"][ind] = values[-1] if values[-1] else None

            result["current_price"] = closes[-1]
            return ToolResult(True, result)
        except Exception as e:
            return ToolResult(False, None, str(e))

    def check_strategy_signals(self, symbol: str, strategy_id: str = None) -> ToolResult:
        """Check if any strategy signals are triggered for a symbol.

        Args:
            symbol: FYERS symbol
            strategy_id: Specific strategy to check (e.g., "STRAT_001")
        """
        try:
            candles = self.fetcher.get_candles(symbol)
            if not candles or len(candles) < 35:
                return ToolResult(False, None, f"Insufficient data for {symbol}")

            closes = [c.close for c in candles]
            curr = candles[-1]
            signals = []

            # Check each strategy
            for strat in self.strategies.get("strategies", []):
                if strategy_id and strat["id"] != strategy_id:
                    continue

                signal = self._evaluate_strategy(strat, candles, closes)
                if signal:
                    signals.append(signal)

            return ToolResult(True, {
                "symbol": symbol,
                "current_price": closes[-1],
                "signals": signals,
                "signal_count": len(signals)
            })
        except Exception as e:
            return ToolResult(False, None, str(e))

    def _evaluate_strategy(self, strategy: dict, candles: list, closes: list) -> dict | None:
        """Evaluate a single strategy for signals."""
        strat_id = strategy["id"]
        curr = candles[-1]
        prev = candles[-2] if len(candles) > 1 else curr

        # Get indicator values
        ema9 = ema(closes, 9)[-1]
        ema26 = ema(closes, 26)[-1]
        ema35 = ema(closes, 35)[-1]
        rsi_val = rsi(closes)[-1]
        hma21 = hma(closes, 21)[-1]

        if not all([ema9, ema26, rsi_val]):
            return None

        # STRAT_005: EMA Crossover with RSI
        if strat_id == "STRAT_005":
            prev_ema9 = ema(closes, 9)[-2]
            prev_ema26 = ema(closes, 26)[-2]
            if prev_ema9 and prev_ema26:
                if prev_ema9 <= prev_ema26 and ema9 > ema26 and curr.close > ema9 and rsi_val > 55:
                    return {
                        "strategy_id": strat_id,
                        "strategy_name": strategy["name"],
                        "signal": "CE",
                        "confidence": strategy.get("confidence", 0.7),
                        "reason": f"9 EMA ({ema9:.2f}) crossed above 26 EMA ({ema26:.2f}), RSI={rsi_val:.1f}"
                    }

        # STRAT_006/007: Index Rejection
        if strat_id in ["STRAT_006", "STRAT_007"]:
            rejection_ema = ema35 if strat_id == "STRAT_007" else ema26

            # PE signal
            if curr.high >= rejection_ema and curr.close < rejection_ema and ema9 < rejection_ema and rsi_val <= 50:
                return {
                    "strategy_id": strat_id,
                    "strategy_name": strategy["name"],
                    "signal": "PE",
                    "confidence": strategy.get("confidence", 0.75),
                    "reason": f"Rejection at EMA, close={curr.close} < EMA={rejection_ema:.2f}, RSI={rsi_val:.1f}"
                }

            # CE signal
            if curr.low <= ema9 and curr.close > rejection_ema and rsi_val > 50:
                return {
                    "strategy_id": strat_id,
                    "strategy_name": strategy["name"],
                    "signal": "CE",
                    "confidence": strategy.get("confidence", 0.75),
                    "reason": f"Bounce at EMA9, close={curr.close} > EMA={rejection_ema:.2f}, RSI={rsi_val:.1f}"
                }

        return None

    # -------------------------------------------------------------------------
    # Position Tools
    # -------------------------------------------------------------------------
    def get_positions(self) -> ToolResult:
        """Get current open positions."""
        try:
            response = self.client.positions()
            positions = response.get("netPositions", [])
            return ToolResult(True, positions)
        except Exception as e:
            return ToolResult(False, None, str(e))

    def place_order(self, symbol: str, side: str, quantity: int,
                    order_type: str = "MARKET", product: str = "INTRADAY") -> ToolResult:
        """Place an order.

        Args:
            symbol: FYERS symbol
            side: "BUY" or "SELL"
            quantity: Number of shares/lots
            order_type: "MARKET" or "LIMIT"
            product: "INTRADAY" or "CNC"
        """
        try:
            side_val = 1 if side.upper() == "BUY" else -1
            type_val = 20 if order_type.upper() == "MARKET" else 2

            order = {
                "symbol": symbol,
                "qty": quantity,
                "type": type_val,
                "side": side_val,
                "productType": product,
                "orderTag": "agent_order",
            }

            response = self.client.place_order(order)
            return ToolResult(True, {
                "order_id": response.get("id"),
                "status": response.get("s"),
                "message": response.get("message")
            })
        except Exception as e:
            return ToolResult(False, None, str(e))

    # -------------------------------------------------------------------------
    # Strategy Tools
    # -------------------------------------------------------------------------
    def get_strategies(self) -> ToolResult:
        """Get all loaded strategies."""
        return ToolResult(True, self.strategies.get("strategies", []))

    def get_strategy(self, strategy_id: str) -> ToolResult:
        """Get a specific strategy by ID."""
        for strat in self.strategies.get("strategies", []):
            if strat["id"] == strategy_id:
                return ToolResult(True, strat)
        return ToolResult(False, None, f"Strategy {strategy_id} not found")

    def scan_market(self, symbols: list[str]) -> ToolResult:
        """Scan multiple symbols for signals.

        Args:
            symbols: List of symbols to scan
        """
        try:
            all_signals = []
            for symbol in symbols:
                result = self.check_strategy_signals(symbol)
                if result.success and result.data.get("signals"):
                    all_signals.extend(result.data["signals"])

            return ToolResult(True, {
                "symbols_scanned": len(symbols),
                "total_signals": len(all_signals),
                "signals": all_signals
            })
        except Exception as e:
            return ToolResult(False, None, str(e))

    def get_tool_definitions(self) -> list[dict]:
        """Get tool definitions for LangChain."""
        return [
            {
                "name": "get_candles",
                "description": "Get OHLC candle data for a symbol",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "symbol": {"type": "string", "description": "FYERS symbol"},
                        "timeframe": {"type": "string", "enum": ["5", "15"], "default": "15"},
                        "limit": {"type": "integer", "default": 50}
                    },
                    "required": ["symbol"]
                }
            },
            {
                "name": "get_option_chain",
                "description": "Get option chain for index/stock",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "symbol": {"type": "string", "description": "Index symbol"},
                        "strike_count": {"type": "integer", "default": 10}
                    },
                    "required": ["symbol"]
                }
            },
            {
                "name": "get_quotes",
                "description": "Get current quotes for symbols",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "symbols": {"type": "array", "items": {"type": "string"}}
                    },
                    "required": ["symbols"]
                }
            },
            {
                "name": "calculate_indicators",
                "description": "Calculate technical indicators",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "symbol": {"type": "string"},
                        "indicators": {"type": "array", "items": {"type": "string"}}
                    },
                    "required": ["symbol"]
                }
            },
            {
                "name": "check_strategy_signals",
                "description": "Check if strategy signals are triggered",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "symbol": {"type": "string"},
                        "strategy_id": {"type": "string"}
                    },
                    "required": ["symbol"]
                }
            },
            {
                "name": "get_positions",
                "description": "Get current open positions"
            },
            {
                "name": "place_order",
                "description": "Place a trade order",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "symbol": {"type": "string"},
                        "side": {"type": "string", "enum": ["BUY", "SELL"]},
                        "quantity": {"type": "integer"},
                        "order_type": {"type": "string", "enum": ["MARKET", "LIMIT"], "default": "MARKET"}
                    },
                    "required": ["symbol", "side", "quantity"]
                }
            },
            {
                "name": "scan_market",
                "description": "Scan multiple symbols for trading signals",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "symbols": {"type": "array", "items": {"type": "string"}}
                    },
                    "required": ["symbols"]
                }
            }
        ]
