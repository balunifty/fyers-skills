#!/usr/bin/env python3
"""Trading Agent Core - LangChain + GPT-4o.

Main agent that monitors markets and alerts when strategy conditions are met.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import pathlib
import sys
from dataclasses import dataclass
from typing import Optional
from zoneinfo import ZoneInfo

SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parents[2]
sys.path.insert(0, str(SCRIPT_DIR))

from fyers_tools import FyersTools, ToolResult  # noqa: E402

# =============================================================================
# Configuration
# =============================================================================
MARKET_TIMEZONE = ZoneInfo("Asia/Kolkata")
KNOWLEDGE_DIR = SCRIPT_DIR / "knowledge"
STRATEGIES_FILE = KNOWLEDGE_DIR / "strategies.json"
LOG_PATH = SCRIPT_DIR / "logs" / "agent.log"

# Alert configuration
ALERT_CONFIG = {
    "whatsapp": {
        "enabled": False,
        "api_key": os.getenv("WHATSAPP_API_KEY"),
        "from_number": os.getenv("WHATSAPP_FROM"),
        "to_number": os.getenv("WHATSAPP_TO"),
    },
    "telegram": {
        "enabled": False,
        "bot_token": os.getenv("TELEGRAM_BOT_TOKEN"),
        "chat_id": os.getenv("TELEGRAM_CHAT_ID"),
    },
    "console": {"enabled": True}
}


# =============================================================================
# Agent Memory
# =============================================================================
@dataclass
class AgentMemory:
    """Stores agent's trading memory."""
    signals_today: list[dict]
    executed_trades: list[dict]
    alerts_sent: list[dict]
    last_scan_time: Optional[dt.datetime] = None

    def add_signal(self, signal: dict):
        self.signals_today.append({
            **signal,
            "timestamp": dt.datetime.now(MARKET_TIMEZONE).isoformat()
        })

    def add_trade(self, trade: dict):
        self.executed_trades.append({
            **trade,
            "timestamp": dt.datetime.now(MARKET_TIMEZONE).isoformat()
        })

    def add_alert(self, alert: dict):
        self.alerts_sent.append({
            **alert,
            "timestamp": dt.datetime.now(MARKET_TIMEZONE).isoformat()
        })

    def to_context(self) -> str:
        """Convert memory to context string for LLM."""
        return f"""
TRADING MEMORY:
- Signals today: {len(self.signals_today)}
- Executed trades: {len(self.executed_trades)}
- Alerts sent: {len(self.alerts_sent)}
- Last scan: {self.last_scan_time.isoformat() if self.last_scan_time else 'Never'}

Recent signals:
{json.dumps(self.signals_today[-5:], indent=2)}

Recent trades:
{json.dumps(self.executed_trades[-3:], indent=2)}
"""


# =============================================================================
# Alert System
# =============================================================================
class AlertSystem:
    """Handles sending alerts via different channels."""

    def __init__(self, config: dict = None):
        self.config = config or ALERT_CONFIG

    def send_alert(self, message: str, alert_type: str = "signal") -> bool:
        """Send alert via configured channels."""
        success = False

        if self.config.get("console", {}).get("enabled"):
            success |= self._send_console(message)

        if self.config.get("whatsapp", {}).get("enabled"):
            success |= self._send_whatsapp(message)

        if self.config.get("telegram", {}).get("enabled"):
            success |= self._send_telegram(message)

        return success

    def _send_console(self, message: str) -> bool:
        """Print alert to console."""
        print(f"\n{'='*60}")
        print(f"🚨 ALERT [{dt.datetime.now(MARKET_TIMEZONE).strftime('%H:%M:%S')}]")
        print(f"{'='*60}")
        print(message)
        print(f"{'='*60}\n")
        return True

    def _send_whatsapp(self, message: str) -> bool:
        """Send WhatsApp message via API."""
        try:
            import requests
            config = self.config["whatsapp"]
            # Implement WhatsApp Business API call here
            # Example with Twilio:
            # url = f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Messages.json"
            # data = {"From": config["from_number"], "To": config["to_number"], "Body": message}
            # response = requests.post(url, data=data, auth=(account_sid, auth_token))
            return True
        except Exception as e:
            print(f"WhatsApp alert failed: {e}", file=sys.stderr)
            return False

    def _send_telegram(self, message: str) -> bool:
        """Send Telegram message."""
        try:
            import requests
            config = self.config["telegram"]
            url = f"https://api.telegram.org/bot{config['bot_token']}/sendMessage"
            data = {"chat_id": config["chat_id"], "text": message, "parse_mode": "HTML"}
            response = requests.post(url, data=data)
            return response.status_code == 200
        except Exception as e:
            print(f"Telegram alert failed: {e}", file=sys.stderr)
            return False


# =============================================================================
# Trading Agent
# =============================================================================
class TradingAgent:
    """Main trading agent that monitors markets and executes strategies."""

    def __init__(self, use_llm: bool = True):
        self.tools = FyersTools()
        self.memory = AgentMemory(
            signals_today=[],
            executed_trades=[],
            alerts_sent=[]
        )
        self.alert_system = AlertSystem()
        self.use_llm = use_llm
        self.strategies = self._load_strategies()

        # Initialize LLM if enabled
        if use_llm:
            self._init_llm()

    def _load_strategies(self) -> dict:
        """Load strategies from knowledge base."""
        if STRATEGIES_FILE.exists():
            return json.loads(STRATEGIES_FILE.read_text())
        return {"strategies": []}

    def _init_llm(self):
        """Initialize LangChain LLM."""
        try:
            from langchain_openai import ChatOpenAI
            from langchain.agents import create_tool_calling_agent, AgentExecutor
            from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder

            # Create LLM
            self.llm = ChatOpenAI(
                model="gpt-4o",
                temperature=0,
                api_key=os.getenv("OPENAI_API_KEY")
            )

            # Create prompt
            self.prompt = ChatPromptTemplate.from_messages([
                ("system", self._get_system_prompt()),
                MessagesPlaceholder(variable_name="chat_history"),
                ("human", "{input}"),
                MessagesPlaceholder(variable_name="agent_scratchpad"),
            ])

            # Create tools list for LangChain
            self.langchain_tools = self._create_langchain_tools()

            # Create agent
            self.agent = create_tool_calling_agent(
                self.llm,
                self.langchain_tools,
                self.prompt
            )

            self.executor = AgentExecutor(
                agent=self.agent,
                tools=self.langchain_tools,
                verbose=True,
                handle_parsing_errors=True
            )

            print("✅ LLM Agent initialized successfully", file=sys.stderr)
        except ImportError as e:
            print(f"⚠️ LangChain not installed: {e}", file=sys.stderr)
            print("Running in rule-based mode only", file=sys.stderr)
            self.use_llm = False
        except Exception as e:
            print(f"❌ LLM initialization failed: {e}", file=sys.stderr)
            self.use_llm = False

    def _get_system_prompt(self) -> str:
        """Get system prompt for the LLM agent."""
        return f"""You are an expert F&O trading agent for Indian markets.

TIME: {dt.datetime.now(MARKET_TIMEZONE).strftime('%Y-%m-%d %H:%M:%S')} IST
MARKET: {'OPEN' if self._is_market_open() else 'CLOSED'}

STRATEGIES YOU MONITOR:
{json.dumps(self.strategies.get('strategies', []), indent=2)}

RISK MANAGEMENT:
- Max position size: 2% of capital
- Max daily loss: 5%
- Stop loss: 15% (fixed)
- Market hours: 9:15 AM - 3:15 PM IST

YOUR TASKS:
1. Monitor market data for strategy signals
2. When a signal is detected, analyze it and send alert
3. Always confirm with user before placing orders
4. Track all signals and trades in memory

RESPONSE FORMAT:
When you detect a signal, format your response as:
📊 SIGNAL DETECTED
Strategy: [Strategy Name]
Symbol: [Symbol]
Signal: [CE/PE]
Price: [Current Price]
Confidence: [High/Medium/Low]
Reason: [Why this signal was triggered]
Action: [Recommended action]

Always be conservative and confirm before executing trades.
"""

    def _create_langchain_tools(self):
        """Create LangChain compatible tools."""
        from langchain_core.tools import tool

        @tool
        def get_market_data(symbol: str) -> str:
            """Get current market data and candles for a symbol."""
            result = self.tools.get_candles(symbol)
            return json.dumps(result.to_dict(), indent=2)

        @tool
        def check_signals(symbol: str) -> str:
            """Check if any trading signals are triggered for a symbol."""
            result = self.tools.check_strategy_signals(symbol)
            return json.dumps(result.to_dict(), indent=2)

        @tool
        def get_option_data(symbol: str) -> str:
            """Get option chain data for an index."""
            result = self.tools.get_option_chain(symbol)
            return json.dumps(result.to_dict(), indent=2)

        @tool
        def get_portfolio() -> str:
            """Get current positions and portfolio status."""
            result = self.tools.get_positions()
            return json.dumps(result.to_dict(), indent=2)

        @tool
        def scan_all_symbols() -> str:
            """Scan all symbols in NiftyFNOTop100 for signals."""
            stocks = self.tools.fetcher.get_stocks()
            result = self.tools.scan_market(stocks[:20])  # Limit for demo
            return json.dumps(result.to_dict(), indent=2)

        return [get_market_data, check_signals, get_option_data, get_portfolio, scan_all_symbols]

    def _is_market_open(self) -> bool:
        """Check if market is currently open."""
        now = dt.datetime.now(MARKET_TIMEZONE)
        market_open = dt.time(9, 15)
        market_close = dt.time(15, 15)
        return now.weekday() < 5 and market_open <= now.time() <= market_close

    # -------------------------------------------------------------------------
    # Main Agent Methods
    # -------------------------------------------------------------------------
    def scan_markets(self, symbols: list[str] = None) -> list[dict]:
        """Scan markets for trading signals."""
        if symbols is None:
            symbols = self.tools.fetcher.get_stocks()[:10]  # Default scan list

        all_signals = []

        for symbol in symbols:
            try:
                result = self.tools.check_strategy_signals(symbol)
                if result.success and result.data.get("signals"):
                    for signal in result.data["signals"]:
                        signal["symbol"] = symbol
                        all_signals.append(signal)
                        self.memory.add_signal(signal)
            except Exception as e:
                print(f"Error scanning {symbol}: {e}", file=sys.stderr)

        self.memory.last_scan_time = dt.datetime.now(MARKET_TIMEZONE)
        return all_signals

    def analyze_signal(self, signal: dict) -> str:
        """Analyze a signal and generate alert message."""
        symbol = signal.get("symbol", "Unknown")
        strat_name = signal.get("strategy_name", "Unknown")
        signal_type = signal.get("signal", "?")
        confidence = signal.get("confidence", 0)
        reason = signal.get("reason", "No reason provided")

        confidence_level = "HIGH" if confidence >= 0.75 else "MEDIUM" if confidence >= 0.6 else "LOW"

        message = f"""
📊 *SIGNAL DETECTED*

*Strategy:* {strat_name}
*Symbol:* {symbol}
*Signal:* {signal_type} ({'CALL' if signal_type == 'CE' else 'PUT'})
*Confidence:* {confidence_level} ({confidence:.0%})
*Time:* {dt.datetime.now(MARKET_TIMEZONE).strftime('%H:%M:%S IST')}

*Analysis:*
{reason}

*Recommended Action:*
{'✅ Consider buying ' + signal_type + ' option' if confidence >= 0.7 else '⚠️ Low confidence - review manually'}

*Current Positions:*
Check portfolio before executing.
"""
        return message

    def process_user_input(self, user_input: str) -> str:
        """Process user input using LLM or rule-based approach."""
        if self.use_llm and hasattr(self, 'executor'):
            try:
                result = self.executor.invoke({
                    "input": user_input,
                    "chat_history": []
                })
                return result.get("output", "No response generated")
            except Exception as e:
                return f"LLM Error: {e}. Falling back to rule-based response."

        # Rule-based fallback
        return self._rule_based_response(user_input)

    def _rule_based_response(self, user_input: str) -> str:
        """Simple rule-based responses when LLM is not available."""
        user_input_lower = user_input.lower()

        if "scan" in user_input_lower:
            signals = self.scan_markets()
            if signals:
                return f"Found {len(signals)} signals:\n" + "\n".join(
                    [f"- {s['symbol']}: {s['signal']} ({s['strategy_name']})" for s in signals[:5]]
                )
            return "No signals found in current scan."

        elif "position" in user_input_lower:
            result = self.tools.get_positions()
            return json.dumps(result.to_dict(), indent=2)

        elif "strategy" in user_input_lower or "strategies" in user_input_lower:
            strats = self.strategies.get("strategies", [])
            return f"Loaded {len(strats)} strategies:\n" + "\n".join(
                [f"- {s['id']}: {s['name']}" for s in strats]
            )

        elif "help" in user_input_lower:
            return """
Available commands:
- scan: Scan markets for signals
- positions: View current positions
- strategies: List all strategies
- help: Show this help message

Or ask me to check specific symbols:
- "Check NIFTY for signals"
- "Get option chain for BANKNIFTY"
"""

        return "I don't understand. Type 'help' for available commands."

    def run_scan_loop(self, interval_seconds: int = 300):
        """Run continuous market scanning loop."""
        print(f"\n🚀 Trading Agent Started")
        print(f"📅 Date: {dt.datetime.now(MARKET_TIMEZONE).strftime('%Y-%m-%d')}")
        print(f"⏰ Market: {'OPEN' if self._is_market_open() else 'CLOSED'}")
        print(f"🔄 Scan interval: {interval_seconds}s")
        print(f"{'='*60}\n")

        while self._is_market_open():
            try:
                # Scan markets
                signals = self.scan_markets()

                # Process signals
                for signal in signals:
                    if signal.get("confidence", 0) >= 0.7:
                        message = self.analyze_signal(signal)
                        self.alert_system.send_alert(message)
                        self.memory.add_alert({"signal": signal, "message": message})

                # Log status
                print(f"[{dt.datetime.now(MARKET_TIMEZONE).strftime('%H:%M:%S')}] "
                      f"Scanned: {len(signals)} signals found")

            except Exception as e:
                print(f"Error in scan loop: {e}", file=sys.stderr)

            # Wait for next scan
            import time
            time.sleep(interval_seconds)

        print("\n⏹️ Agent stopped - Market closed")


# =============================================================================
# Main Entry Point
# =============================================================================
def main():
    """Main entry point for the trading agent."""
    import argparse

    parser = argparse.ArgumentParser(description="F&O Trading Agent")
    parser.add_argument("--scan", action="store_true", help="Run single market scan")
    parser.add_argument("--chat", action="store_true", help="Start interactive chat mode")
    parser.add_argument("--monitor", action="store_true", help="Start continuous monitoring")
    parser.add_argument("--no-llm", action="store_true", help="Disable LLM (rule-based only)")
    parser.add_argument("--symbols", nargs="+", help="Specific symbols to scan")
    args = parser.parse_args()

    # Create agent
    agent = TradingAgent(use_llm=not args.no_llm)

    if args.scan:
        # Single scan
        signals = agent.scan_markets(args.symbols)
        if signals:
            print(f"\n📊 Found {len(signals)} signals:")
            for s in signals:
                print(f"\n{agent.analyze_signal(s)}")
        else:
            print("\n✅ No signals found")

    elif args.chat:
        # Interactive chat mode
        print("\n🤖 Trading Agent Chat Mode")
        print("Type 'exit' to quit, 'help' for commands\n")

        while True:
            user_input = input("You: ").strip()
            if user_input.lower() in ["exit", "quit", "q"]:
                break

            response = agent.process_user_input(user_input)
            print(f"\nAgent: {response}\n")

    elif args.monitor:
        # Continuous monitoring
        agent.run_scan_loop()

    else:
        # Default: single scan
        signals = agent.scan_markets(args.symbols)
        print(f"\n📊 Scan complete: {len(signals)} signals found")


if __name__ == "__main__":
    main()
