# fyers-trading — an Agent Skill for the FYERS API v3

A portable **Agent Skill** that helps AI coding agents build **trading strategies,
automation bots, and backtesting scripts** on the official **FYERS Developer API v3**
(Indian markets — NSE/BSE/MCX). It imports cleanly into **Claude Code**, **Cursor**,
and **Open Claw**.

It targets the *public developer API* (`https://api-t1.fyers.in` + the `fyers-apiv3`
SDK + WebSocket feeds) — **not** the FIA chat-assistant proxy (`fia.fyers.in`).

## What's in here

```
SKILL.md                  # The skill: frontmatter + instructions (start here)
.env.example              # FYERS_APP_ID / FYERS_SECRET_ID / FYERS_REDIRECT_URI
references/               # Loaded on demand (progressive disclosure)
  auth.md                 #   OAuth v3 flow, appIdHash, daily token, refresh
  endpoints.md            #   Full path + payload + ENUM CODE catalog
  symbols.md              #   Symbol format (eq/fut/opt, weekly vs monthly), masters
  market-data.md          #   quotes / depth / history / option chain / status
  orders.md               #   place/modify/cancel, GTT, smart orders, positions
  websocket.md            #   data / order / TBT sockets
  backtesting.md          #   candles -> DataFrame -> strategy -> costs/pitfalls
  rate-limits.md          #   10/s, 200/min, 100k/day; error codes; retry policy
scripts/
  fyers_login.py          #   OAuth login + daily token cache (~/.fyers/token.json)
  fyers_client.py         #   reusable REST client (dry-run orders, 429 backoff)
  example_strategy.py     #   data -> signal -> DRY-RUN order skeleton
  validate-skill.sh       #   lints SKILL.md before importing
assets/                   # fixtures / images
```

## Quick start (as a developer using this skill)

1. Create an app at https://myapi.fyers.in/dashboard/ and note the app id, secret,
   and redirect URI.
2. `cp .env.example .env` and fill in the values (or export them as env vars).
3. Authenticate: `python scripts/fyers_login.py` (caches the daily token).
4. Verify: `python scripts/fyers_client.py profile`.
5. Ask your agent to build a strategy/bot/backtest — it will load the right reference.

## Safety model (real money)

The skill enforces these in any code it generates:
- Secrets via environment variables only — never hardcoded or committed.
- **Order placement is dry-run by default**; live trading needs an explicit opt-in.
- Confirm before any live place/modify/cancel.
- Respect rate limits (10/s · 200/min · 100k/day; order ops ≤10/s).
- Use WebSocket for live ticks, not polling.
- Tokens expire daily — a 401 means re-login, not retry.

## Importing the skill

The skill's folder name should match its `name:` (`fyers-trading`). Copy the repo (or a
folder containing `SKILL.md`, `references/`, `scripts/`, `.env.example`) into the host's
skills directory.

### Claude Code
```bash
mkdir -p .claude/skills/fyers-trading        # per-project
# or: ~/.claude/skills/fyers-trading          # per-user
cp -r SKILL.md references scripts .env.example .claude/skills/fyers-trading/
```
Invoke with `/fyers-trading`, or just describe a FYERS task.

### Cursor
```bash
mkdir -p .cursor/skills/fyers-trading
cp -r SKILL.md references scripts .env.example .cursor/skills/fyers-trading/
```
Reload the window after copying.

### Open Claw
```bash
mkdir -p ~/.openclaw/skills/fyers-trading
cp -r SKILL.md references scripts .env.example ~/.openclaw/skills/fyers-trading/
```

> Skills-directory paths vary by host version. If a host can't find the skill, check
> its docs for the active skills directory and confirm the folder name matches `name:`.

## Validate before importing

```bash
./scripts/validate-skill.sh SKILL.md
python -m py_compile scripts/*.py
```

## Notes & caveats

- Built against FYERS API **v3** (`api-t1.fyers.in`). Span-margin and EDIS still use v2
  (`api.fyers.in/api/v2`).
- The exact access-token TTL isn't published; `fyers_login.py --check` verifies liveness
  by calling `/profile` rather than guessing an expiry.
- The refresh-token flow may be discontinued by FYERS — daily re-login is the reliable
  path. Verify against the live docs (https://myapi.fyers.in/docsv3) if you depend on it.
- For WebSocket streaming, install the official SDK: `pip install fyers-apiv3`.

## License

MIT — see [LICENSE](LICENSE).
