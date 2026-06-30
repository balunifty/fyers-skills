---
name: fyers-skills
description: >-
  Build trading strategies, automation bots, and backtesting scripts on the FYERS
  Trading API v3 (Indian markets — NSE/BSE/MCX). Use when the user wants to fetch
  market data, historical candles, quotes, market depth, or option chains; place,
  modify, or cancel orders (regular, GTT, smart orders); manage positions/holdings;
  stream live data over WebSocket; authenticate with FYERS OAuth; or backtest a
  strategy with FYERS historical data. Triggers: "fyers", "fyers api", "fyers bot",
  "fyers strategy", "fyers backtest", "place an order on fyers", "fyers option chain",
  "fyers historical data", "fyers websocket".
version: 1.0.0
license: MIT
allowed-tools:
  - Bash
  - Read
  - Write
  - Edit
  - Glob
  - Grep
  - WebFetch
---

# FYERS Trading API v3

Help developers build **strategies, automation, and backtesting** on the official
**FYERS Developer API v3** (`https://api-t1.fyers.in`). Generate working Python
(`fyers-apiv3` SDK) or raw-REST code, wire up OAuth correctly, and respect the
real-money safety rules below.

> **Scope note.** This skill targets the *public developer API* (`api-t1.fyers.in`
> + the `fyers-apiv3` SDK + WebSockets). It is **not** the FIA chat-assistant proxy
> (`fia.fyers.in`). Generate code freely; the "never write code / no orders" rules
> from FIA do **not** apply here.

## Safety rules (non-negotiable — real money)

1. **Secrets only via environment variables.** Never hardcode `app_id`, `secret_id`,
   `access_token`, or PIN in generated code or commit them. Read from env / `.env`.
2. **Dry-run by default.** Order-placing code must default to a `DRY_RUN=True` (or
   `--dry-run`) mode that logs the payload instead of sending it. Live placement
   requires an explicit, obvious opt-in flag the user sets themselves.
3. **Validate the symbol against the master before every order.** Never place,
   modify, or build an order from a hand-constructed symbol. Confirm it exists in the
   daily symbol master first (`scripts/fyers_symbols.py` / `validate_symbol()`), which
   also gives the lot size to check `qty` against. An unvalidated symbol fails live with
   code `-300`. `fyers_client.place_order()` enforces this by default.
4. **Confirm before going live.** Before running anything that places/modifies/
   cancels real orders, state plainly what it will do and have the user confirm.
5. **Respect rate limits:** 10 req/sec, 200 req/min, 100,000 req/day; order ops ≤10/sec
   (HTTP 429 → honor `Retry-After`). Breach the per-minute cap >3×/day → blocked all day.
6. **Use WebSocket for live ticks**, never a polling loop on `/quotes`.
7. **Tokens expire daily.** A 401 / code `-8`/`-15`/`-16`/`-17` means re-login, not retry.

## Step 1 — Authenticate (do this first)

Check whether a valid token exists before any data/order work:

```bash
python scripts/fyers_login.py --check   # prints OK if cached token is valid
```

If missing or 401, run the OAuth flow (env vars `FYERS_APP_ID`, `FYERS_SECRET_ID`,
`FYERS_REDIRECT_URI` must be set — see `.env.example`):

```bash
python scripts/fyers_login.py           # opens auth URL, exchanges code, caches token
```

This caches the daily `access_token` to `~/.fyers/token.json`. The flow is:
`generate-authcode` → user logs in → `auth_code` → `appIdHash = SHA256("app_id:secret_id")`
→ `validate-authcode` → `access_token`. Full detail: **`references/auth.md`**.

## Step 2 — Route the task

| User wants… | Load this reference | Use |
|---|---|---|
| Login / token / OAuth / refresh | `references/auth.md` | `scripts/fyers_login.py` |
| Quotes, depth, history, option chain, market status | `references/market-data.md` | `scripts/fyers_client.py` |
| Place / modify / cancel / GTT / smart orders, positions | `references/orders.md` | `scripts/fyers_client.py` |
| Symbol strings (eq/fut/opt), look up a name → exact symbol, lot/tick/expiry | `references/symbols.md` | `scripts/fyers_symbols.py` |
| Live streaming (data / order / TBT sockets) | `references/websocket.md` | — |
| Backtest a strategy from historical candles | `references/backtesting.md` | `scripts/example_strategy.py` |
| Any endpoint path / payload / enum code | `references/endpoints.md` | — |
| Rate limits, error codes, retries | `references/rate-limits.md` | — |

Read references **on demand** — don't load all of them up front. `endpoints.md` is the
full path/field/enum-code catalog; the others are task-focused.

## Step 3 — Write, verify, report

- Prefer reusing `scripts/fyers_client.py` (loads the cached token, adds the
  `Authorization: app_id:access_token` header, and wraps the safety/rate-limit logic).
- After writing code, verify it imports/compiles (`python -m py_compile <file>`), and
  run data-only paths against the live API when a token exists. **Never** run live order
  code to "test" it — use dry-run.
- Get **enum codes exact** (order `type` 1/2/3/4, `side` 1/-1, `productType`, segment/
  exchange IDs). They're in `references/endpoints.md`; do not guess them.

## Scripts

- `scripts/fyers_login.py` — OAuth login + daily token cache (`--check`, `--print-token`).
- `scripts/fyers_client.py` — reusable REST client (profile/funds/holdings/positions/
  orders/quotes/history/optionchain) with dry-run order placement + 429 handling.
- `scripts/fyers_symbols.py` — download/cache the daily symbol master files and resolve a
  name → exact symbol (`search` / `info` / `refresh`); no token needed (public files).
- `scripts/example_strategy.py` — end-to-end template: fetch candles → signal → **dry-run**
  order. Copy and adapt; flip to live only with explicit `--live`.
