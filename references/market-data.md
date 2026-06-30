# FYERS v3 Market Data

Data host: `https://api-t1.fyers.in/data`. Header: `Authorization: <app_id>:<access_token>`.

## Historical candles — GET `/data/history`

| Param | Required | Meaning |
|---|---|---|
| `symbol` | yes | e.g. `NSE:SBIN-EQ` |
| `resolution` | yes | see values below |
| `date_format` | yes | `0` = epoch seconds, `1` = `yyyy-mm-dd` |
| `range_from` | yes | start (format per `date_format`) |
| `range_to` | yes | end (format per `date_format`) |
| `cont_flag` | no | `1` for continuous futures |
| `oi_flag` | no | `1` to append Open Interest to each candle |

**Resolution values:** `"5S" "10S" "15S" "30S" "45S"` (seconds), `"1" "2" "3" "5" "10"
"15" "20" "30" "60" "120" "240"` (minutes), `"D"`/`"1D"` (day), `"1W"`, `"1M"`.

**Max range per request:**
- Minute resolutions: **100 days** per request; history available from **3 Jul 2017**.
- Day/Week/Month: **366 days** per request.
- Seconds resolutions: only the **last 30 trading days**.

To pull more than the cap, chunk the date range and concatenate. Candle array order:
`[epoch, open, high, low, close, volume]` (OI appended when `oi_flag=1`).

Response: `{"s":"ok","candles":[[...],[...]]}`.

```python
data = {"symbol":"NSE:SBIN-EQ","resolution":"5","date_format":"1",
        "range_from":"2024-01-01","range_to":"2024-01-31","cont_flag":"1"}
resp = fyers.history(data=data)   # SDK
```

## Quotes — GET `/data/quotes`

Param `symbols` = comma-separated, **max 50**. Returns `d[]`, each with
`v: {lp, ch, chp, open_price, high_price, low_price, prev_close_price, volume, bid, ask,
atp, spread, tt, fyToken, ...}`. Use this to validate a symbol or get a snapshot — for
live updates use the WebSocket, not a poll loop.

## Market depth — GET `/data/depth`

Params `symbol` (**one** symbol), `ohlcv_flag` (`1` to include OHLCV). Returns 5-level
`bids[]`/`ask[]` (each `{price, volume, ord}`), plus `totalbuyqty`, `totalsellqty`, `ltp`,
`v`, `atp`, `oi`, `lower_ckt`, `upper_ckt`, etc.

## Option chain — GET `/data/options-chain-v3`

| Param | Meaning |
|---|---|
| `symbol` | underlying, e.g. `NSE:NIFTY50-INDEX` |
| `strikecount` | ATM + N ITM + N OTM each side (max 50) |
| `timestamp` | expiry epoch; empty = nearest expiry |
| `greeks` | `1` to include `delta/gamma/theta/vega/iv` |

Returns `data`: `callOi`, `putOi`, `expiryData[]` (`{date "DD-MM-YYYY", expiry epoch,
expiry_flag W/M}`), `indiavixData`, `optionsChain[]` (per strike: `option_type` CE/PE,
`strike_price`, `ltp`, `oi`, `oich`, `volume`, `bid`, `ask`, optional `greeks`).

## Market status — GET `/data/marketStatus`

Returns `marketStatus[]`: `{exchange, segment, market_type (NORMAL/ODD_LOT/AUCTION/...),
status (OPEN/CLOSE/PREOPEN/POSTCLOSE_START/...)}`. Check this before assuming the market
is tradable.
