# FYERS v3 Orders & Positions

> **Real money.** Default every order script to dry-run. Require explicit opt-in for live
> placement. Confirm with the user before any live place/modify/cancel. See the safety
> rules in `SKILL.md`. Enum codes are in `references/endpoints.md` — use them exactly.

## Before any order: validate the symbol (required)

Always confirm the symbol exists in the **daily symbol master** before building or
sending an order. A typo or stale expiry otherwise fails live with code `-300`, and the
master also gives you the lot size to validate `qty`.

```bash
python scripts/fyers_symbols.py info NSE_CM NSE:SBIN-EQ   # found → real symbol + lot/tick
```
```python
from fyers_symbols import validate_symbol
from helper import order_checks

rec = validate_symbol("NSE:BANKNIFTY26JUL65000CE")  # raises if symbol not real
result = order_checks("NSE:BANKNIFTY26JUL65000CE", qty=30, price=805.03, master_record=rec)
# result = {'qty': 30, 'price': 805.05, 'lot_size': 30, 'warnings': ['price rounded ...']}
```
`fyers_client.place_order()` runs symbol existence + lot-size + expiry checks automatically
(`validate_symbol=True` default) — including in dry-run. For full pre-flight (tick rounding,
DTE warnings), call `order_checks()` from `scripts/helper.py` before building the order.

## Place a regular order — POST `/api/v3/orders/sync`

```json
{ "symbol":"NSE:SBIN-EQ", "qty":1, "type":2, "side":1, "productType":"INTRADAY",
  "limitPrice":0, "stopPrice":0, "disclosedQty":0, "validity":"DAY",
  "offlineOrder":false, "stopLoss":0, "takeProfit":0, "orderTag":"mybot",
  "isSliceOrder":false }
```
Field rules:
- `type`: `1` Limit (needs `limitPrice`), `2` Market, `3` SL-M (needs `stopPrice`),
  `4` SL-L (needs `stopPrice` + `limitPrice`).
- SL-L: buy → `stopPrice < limitPrice`; sell → `stopPrice > limitPrice`.
- `productType`: `CNC` equity delivery · `INTRADAY` · `MARGIN` derivatives carry-forward
  · `CO` (stopLoss mandatory, in **points**, multiple of 0.05; validity DAY; disclosedQty 0)
  · `BO` (stopLoss + takeProfit mandatory, in **rupees**; validity DAY) · `MTF` (needs activation).
- `orderTag`: alphanumeric 1–30 chars; not for BO/CO; can't be the clientId or "Untagged".
- `isSliceOrder: true` auto-slices large F&O orders (NSE CM/NFO/BFO only, ≤10 slices).

Response: `{"s":"ok","code":1101,"message":"Order submitted... Ref. No.XXX","id":"XXX"}`.
Code `201` = request made but no ack → poll the orderbook to confirm.

### Modify — PATCH `/api/v3/orders/sync`
Body: `{"id":"<orderId>", "type":1, "limitPrice":..., "stopPrice":..., "qty":..., "disclosedQty":...}`.

### Cancel — DELETE `/api/v3/orders/sync`
Body: `{"id":"<orderId>"}`. Success code `1103`.

### Multi-order (basket, ≤10) — `/api/v3/multi-order/sync`
POST/PATCH/DELETE with a JSON **array** of order/modify/cancel objects.

### Async variants — `/api/v3/orders/async`, `/api/v3/multi-order/async`
Queued; the immediate response returns `id_fyers` (not final). Confirm via the order
WebSocket or by polling the orderbook. Use sync unless you need high throughput.

## Sync vs async — which to use
Use **sync** for almost everything (you get an immediate `id` and clear errors). Use
**async** only for high-frequency bursts where you'll reconcile via the order socket.

## GTT (Good-Till-Triggered) — `/api/v3/gtt/orders/sync`
```json
{ "side":1, "symbol":"NSE:SBIN-EQ", "productType":"CNC",
  "orderInfo": { "leg1": {"price":805, "triggerPrice":804.9, "qty":1} } }
```
- OCO: add `leg2`. Rule: leg1 trigger **above** LTP, leg2 trigger **below** LTP.
- `productType` ∈ CNC/MARGIN/MTF. Valid up to 1 year.
- Codes: place `1101`, modify `1102`, cancel `1103`. Modify=PATCH, Cancel=DELETE (body `{"id":...}`).
- Order book: GET `/api/v3/gtt/orders` (`gtt_oco_ind` 1=GTT/2=OCO; `ord_status` 1 Cancelled,
  2 Traded, 4 Transit, 5 Rejected, 6 Pending).

## Smart orders — `/api/v3/smart-order/{limit,trail,step,sip}`
Server-side managed orders (≤100/day). `flowtype`: 3 Step, 4 Limit, 5 Peg, 6 Trail, 7 SIP.
Modify/cancel/pause/resume take a `flowId`; most must be paused before modify (except Limit
and SIP). See `references/endpoints.md` for the full path list and field sets; only reach
for these if the user explicitly wants trailing/stepwise/SIP automation.

## Positions — `/api/v3/positions`
- Get: GET. Returns `netPositions[]` (netQty, avgPrice, side, productType, pl, realized/
  unrealized_profit, ltp, …) + `overall`.
- Exit all: DELETE `{"exit_all":1}`.
- Exit specific: DELETE `{"id":["NSE:SBIN-EQ-INTRADAY"]}`.
- Exit by filter: DELETE `{"segment":[10],"side":[1,-1],"productType":["INTRADAY"]}`.
- Convert: POST `{"symbol":"<posId>","overnight":1,"positionSide":1,"convertQty":1,
  "convertFrom":"INTRADAY","convertTo":"CNC"}`. CNC/CO/BO/MTF can't be converted; can't
  convert **to** CO/BO/MTF.

## Margin check before placing
`POST /api/v3/multiorder/margin` with `{"data":[{symbol,qty,side,type,productType,
limitPrice,...}]}` returns `margin_total`, `margin_new_order`, `margin_avail`. Check funds
(`/funds`) and margin before live placement.

## Order rate limit
≤10 order ops/sec (place+modify+cancel combined). HTTP 429 → read `Retry-After` (sec) and
`X-Retry-After-Ms` (ms) headers and back off.
