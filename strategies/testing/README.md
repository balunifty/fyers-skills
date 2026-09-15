# 5-minute EMA/RSI ATM Call Strategy

This scanner fetches closed 5-minute candles for every underlying in `stocks.txt`.
When all entry conditions are true, it finds the nearest ATM call from FYERS' option
chain and submits a market order through the shared client.

The default mode is a dry-run. Real orders require `--live` and a second confirmation
that types the exact option symbol. Credentials are loaded by the shared login flow from
environment variables and the daily token cache; no secrets belong in this folder.

## Setup

From any directory:

```powershell
python C:\Users\Admin\fyersAutomation\fyers-skills\skills\fyers-trading\scripts\fyers_login.py
```

Alternatively, change to `C:\Users\Admin\fyersAutomation\fyers-skills` first and use
`python skills/fyers-trading/scripts/fyers_login.py`.

Edit `stocks.txt` with one underlying symbol per line, for example:

```text
NSE:SBIN-EQ
NSE:INFY-EQ
```

The login script reads `FYERS_APP_ID`, `FYERS_SECRET_ID`, and `FYERS_REDIRECT_URI`
from the environment or a `.env` file. See `skills/fyers-trading/references/auth.md`.

## Run

Dry-run scan and order payload:

```text
python strategies/ema_rsi_atm_call/run.py
```

To test order placement for every symbol in `stocks.txt` without an interactive
prompt:

```text
python strategies/ema_rsi_atm_call/sample_place_order.py --qty 1
```

Add `--live` only when you intentionally want to send real buy orders for every
symbol in the file:

```text
python strategies/ema_rsi_atm_call/sample_place_order.py --qty 1 --live
```

The script uses the top-level `DEFAULT_QTY = 250` constant when no quantity is supplied.
Each execution clears and rewrites `../logs/sample_place_order.log`. The log includes
the exact `ORDER_REQUEST` payload and the final broker result.
It records `DRY-RUN`, `PLACED` with the broker order ID, `REJECTED` with the FYERS
error code/message, and authentication or other execution errors.

The strategy runner similarly clears and rewrites `../logs/run.log` on each execution.

Live placement is intentionally explicit:

```text
python strategies/ema_rsi_atm_call/run.py
```

The runner uses `DEFAULT_QTY = 250` and `LIVE = False` constants at the top of
`run.py`. Change `LIVE` to `True` only when you intentionally want real orders.

Only the most recent completed candle is evaluated. Conditions:

1. EMA(10) crossed above EMA(20) on the latest closed candle.
2. EMA(20) crossed above EMA(30) on the latest closed candle.
3. RSI(14) is above 60.
4. Close is above EMA(10).
5. Close is above open.
6. Upper wick is smaller than the candle body.

The option chain's nearest expiry is used. Review expiry and lot size before enabling
live mode. This is execution plumbing, not investment advice; paper-trade and validate
the strategy independently before using real money.