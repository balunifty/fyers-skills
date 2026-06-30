# FYERS v3 Rate Limits & Error Handling

## Rate limits

| Window | Limit |
|---|---|
| Per second | 10 requests |
| Per minute | 200 requests |
| Per day | 100,000 (1 lakh) requests |
| Order ops/sec | 10 (place + modify + cancel combined) |

- Exceed → **HTTP 429**. Order ops return `Retry-After` (seconds) and `X-Retry-After-Ms`
  (ms) headers — honor them.
- Breaching the **per-minute** cap **more than 3 times in a day** gets the user **blocked
  for the rest of the day**. Build conservative pacing.
- **Don't poll for live prices** — use the data WebSocket (see `references/websocket.md`).
- WebSocket: data socket ≤5000 symbols/connection; TBT socket 3 connections/user, 5
  symbols/connection.

### Practical pacing
Add a small client-side throttle (e.g. a token bucket at ~8 req/s) and exponential backoff
on 429. `scripts/fyers_client.py` includes basic 429 handling.

## HTTP status codes
`200` OK · `400` bad request · `401` auth error · `403` permission error · `429` rate
limited (possibly blocked) · `500` server error.

## Common API error codes
| Code | Meaning | Action |
|---|---|---|
| -8 | Token expired | Re-login |
| -15 | Invalid token | Re-login |
| -16 | Server can't authenticate token | Re-login |
| -17 | Token invalid or expired | Re-login |
| -50 | One or more invalid params | Fix payload (see `message`) |
| -51 | Invalid order ID | Check id |
| -53 | Invalid position ID | Check id |
| -99 | Order rejected | Read rejection `message` |
| -300 | Invalid symbol | URL-encode specials (`M&M`→`M%26M`); verify vs master |
| -352 | Invalid App ID / no position to exit | Check creds / state |
| -429 | API rate limit exceeded | Back off |

## Success codes
`200` info · `201` request made, no ack (poll orderbook) · `1101` order placed · `1102`
modified · `1103` cancelled · `1605` socket subscribed.

## Retry policy (recommended)
- `401` / negative auth codes (`-8/-15/-16/-17`) → **re-login once**, then surface the error.
  Don't retry-loop with a dead token.
- `429` → back off per `Retry-After`/`X-Retry-After-Ms`, then retry with exponential backoff.
- `5xx` → retry a few times with backoff + jitter.
- `-50` / `-300` / `-99` → **do not retry**; the request is wrong — fix and report.
