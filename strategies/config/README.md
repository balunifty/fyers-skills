# 15-minute Market Data Agent

This agent reads underlyings from `Nifty50.txt`, fetches completed 15-minute FYERS
candles during NSE market hours, calculates EMA(10/20/30/50), HMA(21), RSI(14), and ADX(14),
and stores the candle plus indicators in SQLite.

Run from any directory:

```powershell
python C:\Users\Admin\fyersAutomation\fyers-skills\strategies\market_data_agent\agentNifty50.py
```

Edit the constants at the top of `agentNifty50.py` to change the database path, polling
interval, or one-shot/continuous behavior. The default agent runs continuously,
polling every 10 minutes during `09:15` to `15:45` IST, and exits after market close.
All history, position, and exit requests are rate-limited to a maximum of 8 API calls
per second, below the requested 9 calls per second.

To replace `Nifty50.txt` with the current official NIFTY 50 constituents, run:

```powershell
python C:\Users\Admin\fyersAutomation\fyers-skills\strategies\market_data_agent\update_nifty50_stocks.py
```

The update is atomic and validates that exactly 50 symbols were received before
replacing the file.

Database:

```text
../databases/nifty50.db
```

Each stock has its own table, for example `candles_15m_NSE_SBIN_EQ`. The
table contains the unique symbol/time candle and these columns:
`ema10`, `ema20`, `ema30`, `ema50`, `hma21`, `rsi14`, and `adx14`. The latest stored rows can
be inspected with any SQLite client.

The agent also checks for an HMA(21) cross-down on each completed candle. When the
previous close is above its HMA(21) and the current close is below it, matching open
equity positions are exited. `LIVE = False` in `agentNifty50.py` keeps exits in dry-run
mode; set it to `True` only after validating the behavior. Exit events use the same
`MARKET_PRICE`, `ORDER_REQUEST`, and final status structure as the sample order script.

Errors are written to `../logs/agent.log`. The file is cleared once each new calendar day,
so errors remain available across restarts on the same day. It remains empty when all
fetches and database writes succeed.

## WebSocket collector

For tick-driven candle construction, run:

```powershell
python C:\Users\Admin\fyersAutomation\fyers-skills\strategies\market_data_agent\websocketNifty50.py
```

It subscribes to NIFTY 50, BANKNIFTY, and all symbols in `Nifty50.txt`, builds 15-minute
IST candles, and stores them with EMA10/20/30, HMA21, and RSI14 in
`../databases/nifty50_websocket.db`. A candle is persisted when the first tick of the next 15-minute
bucket arrives. WebSocket errors are written to `../logs/websocketNifty50.log`.