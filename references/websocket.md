# FYERS v3 WebSocket Feeds

Use WebSockets for **live data and order updates** — never poll `/quotes` in a loop.
Auth token format on all sockets: `<app_id>:<access_token>` (same as the REST header).

## Data socket — live quotes & depth

Python SDK (`data_ws.FyersDataSocket`). The SDK manages the underlying `wss://` URL.

```python
from fyers_apiv3.FyersWebsocket import data_ws

def on_message(msg): print(msg)
def on_open():
    fyers.subscribe(symbols=["NSE:SBIN-EQ","NSE:NIFTY50-INDEX"], data_type="SymbolUpdate")
    fyers.keep_running()

fyers = data_ws.FyersDataSocket(
    access_token=f"{APP_ID}:{ACCESS_TOKEN}", log_path="", litemode=False,
    write_to_file=False, reconnect=True,
    on_connect=on_open, on_message=on_message, on_error=print, on_close=print,
)
fyers.connect()
```

- `data_type`: `"SymbolUpdate"` (LTP/quote) or `"DepthUpdate"` (5-level depth).
- Message `type`: `cn` connect · `sub` subscribe · `if` index · `dp` depth · `sf` equity/option.
- `SymbolUpdate` fields: `ltp, prev_close_price, open_price, high_price, low_price, ch, chp,
  vol_traded_today, last_traded_time, bid_price, ask_price, bid_size, ask_size,
  last_traded_qty, tot_buy_qty, tot_sell_qty, avg_trade_price`.
- `litemode=True` → only `{symbol, ltp, type:'sf'}` (lower bandwidth).
- `DepthUpdate`: `bid_price1..5, ask_price1..5, bid_size1..5, ask_size1..5, bid_order1..5, ask_order1..5`.
- `unsubscribe(symbols=[...], data_type=...)` to drop symbols.
- **Limit: 5000 symbols per connection.** Tune throughput via `setQueueProcessInterval(ms)` (1–2000ms).

## Order socket — live order/trade/position updates

URL: `wss://socket.fyers.in/trade/v3`. Header `authorization: <app_id>:<access_token>`.

```python
from fyers_apiv3.FyersWebsocket import order_ws

fyers = order_ws.FyersOrderSocket(
    access_token=f"{APP_ID}:{ACCESS_TOKEN}", log_path="",
    on_orders=print, on_trades=print, on_positions=print, on_general=print,
)
fyers.connect()
fyers.subscribe(data_type="OnOrders,OnTrades,OnPositions")
```

- Raw subscribe: `{"T":"SUB_ORD","SLIST":["orders","trades","positions","edis","pricealerts","login"],"SUB_T":1}` (`-1` to unsub).
- Send the string `"ping"` every ~10s to keep alive. Subscribe ok → `{"code":1605,...}`.
- 403 = bad token, 404 = wrong URL.

## TBT socket — 50-level tick-by-tick depth

URL: `wss://rtsocket-api.fyers.in/versova` (bootstrap GET `…/indus/home/tbtws`). **NFO +
NSE equity only.** Messages are **protobuf** (schema
`https://public.fyers.in/tbtproto/1.0.0/msg.proto`). First packet is a full snapshot, then
diffs.

```python
from fyers_apiv3.FyersWebsocket.tbt_ws import FyersTbtSocket, SubscriptionModes
# subscribe(symbol_tickers=[...], channelNo="1", mode=SubscriptionModes.DEPTH)
# then switchChannel(resume_channels=["1"], pause_channels=[])  # required to actually stream
```
Limits: 3 connections per app per user · 5 symbols per connection · channels 1–50.

## Choosing a feed
- LTP/quote/strategy signals → **data socket** `SymbolUpdate` (or `litemode`).
- 5-level book → data socket `DepthUpdate`.
- Full 50-level order book (HFT) → **TBT socket**.
- Knowing when your orders fill → **order socket**.
