# Strategy Configuration

## Live order switch

Configuration is split by market segment:

- `SCRIPT_NAME` beginning with `Equity` (case-insensitive) loads
  `strategies/config/equity/config.json`.
- Every other script loads `strategies/config/fno/config.json`.

Both files use the same schema:

```json
{
  "place_order": "NO",
  "max_stocks_per_day": 5,
  "qty": 1,
  "stop_loss": 1.5,
  "trailing_stop_loss": 0.5
}
```

The selected file is read before a live order or position exit and whenever order/risk
settings are evaluated.

Set `"place_order"` to `"YES"` to allow those scripts to transmit live orders. This is an
additional gate: a script must also be in its own live/`--live` mode. `NO`, a missing file,
invalid JSON, or any other value blocks live placement. Dry-run requests are still allowed.
The file is read for every live attempt, so an already-running strategy uses a changed value
on its next order attempt.

`max_stocks_per_day` is a separate cap for each strategy. For example, `5` allows live entry
attempts for at most five unique stock/option symbols per strategy per IST calendar day. The
count is stored in the git-ignored `strategies/config/order_state/` directory, so restarting
a strategy does not reset it. Repeated attempts for an already-counted symbol do not consume
another slot, dry-runs do not consume slots, and stop-loss/position exits do not consume entry
slots. Counters are reserved before transmission so a failed or uncertain request still
counts, preventing accidental retries from exceeding the cap.

`qty` is authoritative for every strategy entry order: it means option lots for F&O
strategies (multiplied by the contract lot size) and shares for equity strategies. The
value is read when each order is built, so config edits apply without restarting a running
strategy.

`stop_loss` and `trailing_stop_loss` are percentage values, not points: `1.5` means `1.5%`.
The tighter of the fixed stop (`entry × (1 - stop_loss)`) and trailing stop (`highest LTP ×
(1 - trailing_stop_loss)`) is used by strategies with managed position tracking. These
values are also read on every monitoring cycle, so changes apply to open tracked positions.

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