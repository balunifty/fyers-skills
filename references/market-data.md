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

### Request parameters

| Param | Type | Required | Notes |
|---|---|---|---|
| `symbol` | string | yes | Underlying symbol — use `-INDEX` form for index options: `NSE:NIFTY50-INDEX`, `NSE:NIFTYBANK-INDEX`. Equity options: `NSE:SBIN-EQ`. |
| `strikecount` | int | no | ATM strike ± N strikes each side. Max **50**. Default = 1 (ATM only). A count of 5 returns 5 ITM + ATM + 5 OTM = 11 strikes per expiry. |
| `timestamp` | string | no | Expiry epoch seconds to select a specific expiry. Omit for the **nearest** (front-month or front-week) expiry. Use `expiryData[]` from a prior call to enumerate available expiries. |
| `greeks` | string | no | `"1"` to include Black-Scholes greeks (`delta`, `gamma`, `theta`, `vega`, `iv`) per leg. Only meaningful for equity/index options. |

### Response structure

```
data.callOi          int     Total open interest across all call strikes (returned)
data.putOi           int     Total OI across all put strikes
data.expiryData[]           List of all available expiries for this underlying
  .date              str     "DD-MM-YYYY"
  .expiry            str     Epoch seconds (use as `timestamp` param for a specific expiry)
  .expiry_flag       str     "W" = weekly, "M" = monthly
data.indiavixData           Live India VIX snapshot (ltp, ltpch, ltpchp, symbol, fyToken)
data.optionsChain[]         One entry per strike × option_type, PLUS one entry for the underlying spot
  [underlying spot]          option_type = "", strike_price = -1, has ltp/ltpch/ltpchp/fp/fpch
  [per option leg]
    .symbol          str     API symbol (e.g. NSE:NIFTY2632423050CE)
    .fyToken         str     Instrument token (for WebSocket subscriptions)
    .option_type     str     "CE" or "PE"
    .strike_price    int     Strike price
    .ltp             float   Last traded price
    .ltpch           float   Change in LTP
    .ltpchp          float   % change in LTP
    .bid             float   Best bid
    .ask             float   Best ask
    .oi              int     Open interest
    .oich            int     Change in OI
    .oichp           float   % change in OI
    .prev_oi         int     Previous session OI
    .volume          int     Volume traded
    .greeks                  Present when greeks=1 was requested
      .delta         float   Sensitivity to ₹1 change in underlying (+ve for CE, -ve for PE)
      .gamma         float   Rate of change of delta
      .theta         float   Daily time decay (negative — option loses value each day)
      .vega          float   Sensitivity to 1% change in IV
      .iv            float   Implied volatility (%)
```

### Greeks interpretation

| Greek | Sign | What it tells you |
|---|---|---|
| `delta` | CE: 0 to +1 · PE: -1 to 0 | How much the option price moves per ₹1 move in the underlying |
| `gamma` | always +ve | How fast delta changes; highest ATM near expiry |
| `theta` | always -ve | Time decay per day; accelerates near expiry |
| `vega` | always +ve | P&L impact of a 1% IV change; highest for longer-dated options |
| `iv` | % | Implied volatility of this specific leg; compare with IndiaVIX for context |

### How to select an expiry

```python
# 1. Fetch the chain without timestamp to get all expiry dates
resp = fc.option_chain("NSE:NIFTY50-INDEX", strikecount=1)
expiries = resp["data"]["expiryData"]
# expiries = [{"date":"24-03-2026","expiry":"1774346400","expiry_flag":"W"}, ...]

# 2. Pick the one you want (e.g. nearest monthly)
monthly = [e for e in expiries if e["expiry_flag"] == "M"][0]

# 3. Fetch the full chain for that expiry
resp2 = fc.option_chain("NSE:NIFTY50-INDEX", strikecount=10,
                         timestamp=monthly["expiry"], greeks=True)
```

### Full example — ATM straddle cost

```python
resp = fc.option_chain("NSE:NIFTY50-INDEX", strikecount=3, greeks=True)
chain = resp["data"]["optionsChain"]

# Spot is the entry where option_type == ""
spot = next(c for c in chain if c["option_type"] == "")["ltp"]

# Find ATM strike (nearest to spot)
strikes = sorted({c["strike_price"] for c in chain if c["option_type"] in ("CE","PE")})
atm = min(strikes, key=lambda s: abs(s - spot))

# Pull CE and PE at ATM
atm_ce = next(c for c in chain if c["strike_price"] == atm and c["option_type"] == "CE")
atm_pe = next(c for c in chain if c["strike_price"] == atm and c["option_type"] == "PE")

straddle_cost = atm_ce["ltp"] + atm_pe["ltp"]
print(f"Spot: {spot}  ATM: {atm}  Straddle: {straddle_cost}")
if "greeks" in atm_ce:
    print(f"CE IV: {atm_ce['greeks']['iv']}%  PE IV: {atm_pe['greeks']['iv']}%")
    print(f"Theta: CE {atm_ce['greeks']['theta']:.2f}  PE {atm_pe['greeks']['theta']:.2f}")
```

### OI-based PCR (Put/Call Ratio)

```python
total_call_oi = resp["data"]["callOi"]
total_put_oi  = resp["data"]["putOi"]
pcr = total_put_oi / total_call_oi
print(f"PCR: {pcr:.2f}  ({'bearish' if pcr > 1.2 else 'bullish' if pcr < 0.8 else 'neutral'})")
```

### Pitfalls

- `strikecount=50` is the hard cap; requesting more returns an error.
- `greeks` is only meaningful for options (`option_type` CE/PE). The underlying spot entry and `indiavixData` never have a `greeks` block.
- The `symbol` param must use the underlying, not the option contract. Use `-INDEX` for indices (`NSE:NIFTY50-INDEX`), not `-EQ`.
- To get a non-default expiry, you **must** pass `timestamp` as an epoch string — the `date` field ("DD-MM-YYYY") is display-only and not accepted as a param.
- `fyers_client.option_chain()` wraps this endpoint; pass `greeks=True` to the method.

## Market status — GET `/data/marketStatus`

Returns `marketStatus[]`: `{exchange, segment, market_type (NORMAL/ODD_LOT/AUCTION/...),
status (OPEN/CLOSE/PREOPEN/POSTCLOSE_START/...)}`. Check this before assuming the market
is tradable.
