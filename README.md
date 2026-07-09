# fyers-skills — an Agent Skill for the FYERS API v3

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
requirements.txt          # Default deps for generated strategy/bot/backtest code
references/               # Loaded on demand (progressive disclosure)
  setup.md                #   venv + install steps, package list, troubleshooting
  auth.md                 #   OAuth v3 flow, appIdHash, daily token, refresh
  endpoints.md            #   Full path + payload + ENUM CODE catalog
  symbols.md              #   Symbol format (eq/fut/opt, weekly vs monthly), masters
  market-data.md          #   quotes / depth / history / option chain / status
  orders.md               #   place/modify/cancel, GTT, smart orders, positions
  websocket.md            #   data / order / TBT sockets
  backtesting.md          #   candles -> DataFrame -> strategy -> costs/pitfalls
  indicators.md           #   TA-Lib install + indicator wrappers reference
  rate-limits.md          #   10/s, 200/min, 100k/day; error codes; retry policy
scripts/
  fyers_login.py          #   OAuth login + daily token cache (~/.fyers/token.json)
  fyers_client.py         #   reusable REST client (dry-run orders, 429 backoff)
  fyers_symbols.py        #   download/cache symbol masters; resolve name -> symbol
  helper.py               #   token-free order utilities: lot validation, tick rounding, DTE
  option_chain.py         #   option chain helpers: parse, ATM, PCR, straddle, max pain
  example_strategy.py     #   data -> signal -> DRY-RUN order skeleton
  indicators.py           #   TA-Lib indicator wrappers (SMA/EMA/RSI/MACD/BBANDS/ATR/...)
  trade_logger.py         #   append-only JSONL audit log (~/.fyers/trades.jsonl)
  validate-skill.sh       #   lints SKILL.md before importing
assets/                   # fixtures / images
```

## Quick start (as a developer using this skill)

1. Create an app at https://myapi.fyers.in/dashboard/ and note the app id, secret,
   and redirect URI.
2. Create a venv and install deps: `python3 -m venv .venv && source .venv/bin/activate
   && pip install -r requirements.txt` (see `references/setup.md` for the package list
   and install troubleshooting).
3. `cp .env.example .env` and fill in the values (or export them as env vars).
4. Authenticate: `python scripts/fyers_login.py` (caches the daily token).
5. Verify: `python scripts/fyers_client.py profile`.
6. Ask your agent to build a strategy/bot/backtest — it will load the right reference.

## Safety model (real money)

The skill enforces these in any code it generates:
- Secrets via environment variables only — never hardcoded or committed.
- **Order placement is dry-run by default**; live trading needs an explicit opt-in.
- Confirm before any live place/modify/cancel.
- Respect rate limits (10/s · 200/min · 100k/day; order ops ≤10/s).
- Use WebSocket for live ticks, not polling.
- Tokens expire daily — a 401 means re-login, not retry.

## Installing the skill

### Recommended: `npx skills add` (the [`skills`](https://github.com/vercel-labs/skills) CLI)

```bash
npx skills add FyersDev/fyers-skills          # project-level (./)
npx skills add FyersDev/fyers-skills -g        # global (user-level)
npx skills add FyersDev/fyers-skills --all     # all skills, all detected agents, no prompts
```

This reads `SKILL.md`, installs the skill as **`fyers-skills`**, and wires it into the
agent directories it detects. Useful flags: `-a/--agent <agents>` to target specific
agents, `-l/--list` to preview without installing, `--copy` to copy files instead of
symlinking. List or remove later with `npx skills list` / `npx skills remove fyers-skills`.

> **Requires a public repo.** `skills add` fetches over public GitHub and has no
> private-repo/token support — if `FyersDev/fyers-skills` is private the command 404s.
> Make the repo public, or use the manual install below.

### Manual install (works with a private repo over SSH)

The skill's folder name should match its `name:` (`fyers-skills`). Clone the repo, then
copy `SKILL.md`, `references/`, `scripts/`, and `.env.example` into the host's skills
directory:

```bash
git clone git@github.com:FyersDev/fyers-skills.git && cd fyers-skills
```

**Claude Code**
```bash
mkdir -p .claude/skills/fyers-skills        # per-project
# or: ~/.claude/skills/fyers-skills          # per-user
cp -r SKILL.md references scripts .env.example .claude/skills/fyers-skills/
```
Invoke with `/fyers-skills`, or just describe a FYERS task.

**Cursor**
```bash
mkdir -p .cursor/skills/fyers-skills
cp -r SKILL.md references scripts .env.example .cursor/skills/fyers-skills/
```
Reload the window after copying.

**Open Claw**
```bash
mkdir -p ~/.openclaw/skills/fyers-skills
cp -r SKILL.md references scripts .env.example ~/.openclaw/skills/fyers-skills/
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

## Star History

[![Star History Chart](https://api.star-history.com/svg?repos=FyersDev/fyers-skills&type=Date)](https://star-history.com/#FyersDev/fyers-skills&Date)
