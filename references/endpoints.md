# FYERS v3 Endpoint & Enum Catalog

Full path/field/code reference. Hosts:
- REST (transactions/auth): `https://api-t1.fyers.in/api/v3`
- Data: `https://api-t1.fyers.in/data`
- v2 (Span margin, EDIS only): `https://api.fyers.in/api/v2`

Header on every call: `Authorization: <app_id>:<access_token>`.

Response envelope: success `{"s":"ok","code":200,...}`; error `{"s":"error","code":<neg>,"message":"..."}`.

## REST endpoints

### User
| What | Method | Path |
|---|---|---|
| Profile | GET | `/api/v3/profile` |
| Funds | GET | `/api/v3/funds` |
| Holdings | GET | `/api/v3/holdings` |
| Logout | POST | `/api/v3/logout` |

`funds.fund_limit[].id`: 1 Total Balance · 2 Utilized · 3 Clear Balance · 4 Realized P&L
· 5 Collaterals · 6 Fund Transfer · 7 Receivables · 8 Adhoc Limit · 9 Limit at SOD · 10
Available Balance.

### Transaction info
| What | Method | Path | Notes |
|---|---|---|---|
| Orderbook | GET | `/api/v3/orders` | `?id=<orderId>` or `?order_tag=1:Tag` |
| Tradebook | GET | `/api/v3/tradebook` | |
| Positions | GET | `/api/v3/positions` | |

### Reports (GET; params `from_date`,`to_date` YYYY-MM-DD, `page_size`,`page_no`,`segment_type`,`exchange_type`)
`/api/v3/order-history` · `/api/v3/trade-history` · `/api/v3/charges-history` ·
`/api/v3/realised-pnl-history` · `/api/v3/tax-pnl-history` · `/api/v3/ledger-history`.

### Orders — sync (immediate ack, returns `id`)
| What | Method | Path |
|---|---|---|
| Place single | POST | `/api/v3/orders/sync` |
| Modify | PATCH | `/api/v3/orders/sync` |
| Cancel | DELETE | `/api/v3/orders/sync` (body `{"id":...}`) |
| Place multi (≤10) | POST | `/api/v3/multi-order/sync` |
| Modify multi | PATCH | `/api/v3/multi-order/sync` |
| Cancel multi | DELETE | `/api/v3/multi-order/sync` |
| MultiLeg | POST | `/api/v3/multileg/orders/sync` |

### Orders — async (queued; returns `id_fyers`, confirm via WS/orderbook)
`POST/PATCH/DELETE /api/v3/orders/async` · `POST/PATCH/DELETE /api/v3/multi-order/async`.

### Positions
| What | Method | Path | Body |
|---|---|---|---|
| Exit all | DELETE | `/api/v3/positions` | `{"exit_all":1}` |
| Exit by id | DELETE | `/api/v3/positions` | `{"id":["NSE:SBIN-EQ-INTRADAY"]}` |
| Exit by filter | DELETE | `/api/v3/positions` | `{"segment":[10],"side":[1,-1],"productType":["INTRADAY"]}` |
| Convert | POST | `/api/v3/positions` | `{symbol,overnight,positionSide,convertQty,convertFrom,convertTo}` |

### GTT (valid up to 1 year)
| What | Method | Path |
|---|---|---|
| Place / OCO | POST | `/api/v3/gtt/orders/sync` |
| Modify | PATCH | `/api/v3/gtt/orders/sync` |
| Cancel | DELETE | `/api/v3/gtt/orders/sync` (body `{"id":...}`) |
| Order book | GET | `/api/v3/gtt/orders` |

### Smart orders (≤100/day; `flowId` to modify/cancel/pause/resume)
`POST /api/v3/smart-order/{limit,trail,step,sip}` · `PATCH /smart-order/modify` ·
`DELETE /smart-order/cancel` · `PATCH /smart-order/{pause,resume}` · `GET /smart-order/orderbook`.
`flowtype`: 3 Step · 4 Limit · 5 Peg · 6 Trail · 7 SIP.

### Margin / EDIS
| What | Method | Path |
|---|---|---|
| Span margin | POST | `https://api.fyers.in/api/v2/span_margin` |
| Multiorder margin | POST | `https://api-t1.fyers.in/api/v3/multiorder/margin` |
| EDIS TPIN | GET | `https://api.fyers.in/api/v2/tpin` |
| EDIS details/index/inquiry | GET/POST | `https://api.fyers.in/api/v2/{details,index,inquiry}` |

### Data
| What | Method | Path |
|---|---|---|
| Historical candles | GET | `/data/history` |
| Quotes (≤50 symbols) | GET | `/data/quotes` |
| Market depth (1 symbol) | GET | `/data/depth` |
| Option chain | GET | `/data/options-chain-v3` |
| Market status | GET | `/data/marketStatus` |

## Order payload (single, `/orders/sync`)

```json
{
  "symbol": "NSE:SBIN-EQ",
  "qty": 1,
  "type": 2,
  "side": 1,
  "productType": "INTRADAY",
  "limitPrice": 0,
  "stopPrice": 0,
  "disclosedQty": 0,
  "validity": "DAY",
  "offlineOrder": false,
  "stopLoss": 0,
  "takeProfit": 0,
  "orderTag": "mybot",
  "isSliceOrder": false
}
```

## Enum codes (get these exact — do not guess)

- **type** (order type): `1` Limit · `2` Market · `3` Stop / SL-M · `4` Stop-Limit / SL-L.
- **side**: `1` Buy · `-1` Sell.
- **productType**: `CNC` (equity delivery) · `INTRADAY` · `MARGIN` (derivatives CF) ·
  `CO` (cover, stopLoss in points, mult of 0.05) · `BO` (bracket, stopLoss+takeProfit in ₹)
  · `MTF`.
- **validity**: `DAY` · `IOC`.
- **offlineOrder**: `false` = normal (market hours) · `true` = AMO.
- **order status**: `1` Cancelled · `2` Traded/Filled · `3` (reserved) · `4` Transit ·
  `5` Rejected · `6` Pending · `7` Expired.
- **position side**: `1` Long · `-1` Short · `0` Closed.
- **exchange**: `10` NSE · `11` MCX · `12` BSE.
- **segment**: `10` Capital Market · `11` Equity Derivatives · `12` Currency Derivatives
  · `20` Commodity Derivatives.
- **instrument type (CM)**: `0` EQ · `8` MF · `9` ETF · `10` INDEX (others: 1 PREFSHARES,
  2 DEBENTURES, 3 WARRANTS, 5 SGB, 6 G-Sec, 7 T-Bill).
- **instrument type (FO)**: `11` FUTIDX · `13` FUTSTK · `14` OPTIDX · `15` OPTSTK.
- **holding type**: `T1` (bought, not yet in demat) · `HLD` (in demat).
- **order source**: `M` Mobile · `W` Web · `R` Fyers One · `A` Admin · `ITS` API.

## Success codes

`200` get/info · `201` request made, no ack (check orderbook) · `1101` order placed ·
`1102` GTT/order modified · `1103` cancelled · `1605` socket subscribed.
