# F&O Trading Agent

AI-powered trading agent that monitors markets and alerts when strategy conditions are met.

## Quick Start

```bash
# 1. Install requirements
pip install -r requirements.txt

# 2. Set OpenAI API key
set OPENAI_API_KEY=your_key_here

# 3. Run agent
python trading_agent.py --scan
```

## Usage Modes

### Single Scan
```bash
python trading_agent.py --scan
python trading_agent.py --scan --symbols NSE:NIFTY50-INDEX NSE:BANKNIFTY-INDEX
```

### Interactive Chat
```bash
python trading_agent.py --chat
```

### Continuous Monitoring
```bash
python trading_agent.py --monitor
```

### Rule-based Only (No LLM)
```bash
python trading_agent.py --scan --no-llm
```

## Files

| File | Description |
|------|-------------|
| `trading_agent.py` | Main agent with LLM integration |
| `fyers_tools.py` | FYERS API tools for the agent |
| `knowledge/strategies.json` | Strategy knowledge base |
| `requirements.txt` | Python dependencies |

## Strategy Knowledge Base

Edit `knowledge/strategies.json` to add/modify strategies.

## Alerts

Configure alerts in `trading_agent.py`:
- Console (default)
- WhatsApp Business API
- Telegram Bot

## Architecture

```
┌─────────────────────────────────────────┐
│           Trading Agent                 │
├─────────────────────────────────────────┤
│  ┌─────────┐    ┌──────────────────┐   │
│  │  LLM    │◄──►│  FYERS Tools     │   │
│  │ (GPT-4o)│    │  (API + Indicators)│   │
│  └─────────┘    └──────────────────┘   │
│       ▲                   ▲            │
│       │                   │            │
│  ┌────┴────┐    ┌─────────┴─────────┐  │
│  │ Memory  │    │   Alert System    │  │
│  │ (RAG)   │    │ (WhatsApp/SMS)    │  │
│  └─────────┘    └───────────────────┘  │
└─────────────────────────────────────────┘
```
