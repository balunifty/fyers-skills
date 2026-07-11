# fyers-skills — Agent Skills for the FYERS API v3

A portable collection of **Agent Skills** that help AI coding agents build **trading
strategies, automation bots, and backtesting scripts** on the official **FYERS Developer
API v3** (Indian markets — NSE/BSE/MCX). They import cleanly into **Claude Code**,
**Cursor**, and **Open Claw**.

They target the *public developer API* (`https://api-t1.fyers.in` + the `fyers-apiv3`
SDK + WebSocket feeds) — **not** the FIA chat-assistant proxy (`fia.fyers.in`).

## Skills in this repo

Each skill is a self-contained folder under `skills/`. The install command (below) discovers
them all.

- **`fyers-trading`** — build/backtest/order plumbing on the FYERS API v3 (the core skill).
- **`fyers-supercharge`** — *Supercharge Mode*: an iterative multi-agent optimization loop
  that debates a strategy, generates optimized variants, backtests them, and searches for
  measurably better versions across rounds (keeping the original as the baseline). Depends on
  `fyers-trading`; install both together.

## What's in here

```
skills/
  fyers-trading/            # The core skill (start here)
    SKILL.md                #   frontmatter + instructions
    .env.example            #   FYERS_APP_ID / FYERS_SECRET_ID / FYERS_REDIRECT_URI
    requirements.txt        #   Default deps for generated strategy/bot/backtest code
    references/             #   Loaded on demand (progressive disclosure)
      setup.md              #     venv + install steps, package list, troubleshooting
      auth.md               #     OAuth v3 flow, appIdHash, daily token, refresh
      endpoints.md          #     Full path + payload + ENUM CODE catalog
      symbols.md            #     Symbol format (eq/fut/opt, weekly vs monthly), masters
      market-data.md        #     quotes / depth / history / option chain / status
      orders.md             #     place/modify/cancel, GTT, smart orders, positions
      websocket.md          #     data / order / TBT sockets
      backtesting.md        #     candles -> DataFrame -> strategy -> costs/pitfalls
      indicators.md         #     TA-Lib install + indicator wrappers reference
      quantstats.md         #     QuantStats install + tear-sheet / risk-metrics reference
      rate-limits.md        #     10/s, 200/min, 100k/day; error codes; retry policy
    scripts/
      fyers_login.py        #     OAuth login + daily token cache (~/.fyers/token.json)
      fyers_client.py       #     reusable REST client (dry-run orders, 429 backoff)
      fyers_symbols.py      #     download/cache symbol masters; resolve name -> symbol
      helper.py             #     token-free order utilities: lot validation, tick rounding, DTE
      option_chain.py       #     option chain helpers: parse, ATM, PCR, straddle, max pain
      example_strategy.py   #     data -> signal -> DRY-RUN order skeleton
      indicators.py         #     TA-Lib indicator wrappers (SMA/EMA/RSI/MACD/BBANDS/ATR/...)
      quantstats_report.py  #     QuantStats tear sheet + risk metrics from a returns series
      trade_logger.py       #     append-only JSONL audit log (~/.fyers/trades.jsonl)
      validate-skill.sh     #     lints SKILL.md before importing
  fyers-supercharge/        # Supercharge Mode (iterative multi-agent optimization)
    SKILL.md                #   orchestrator + council + optimization-loop instructions
    references/
      optimization-loop.md  #   the 6-phase round lifecycle + consensus rule
      council.md            #   12-seat roster: domains, mandates, veto powers, owned metrics
      variants.md           #   variant contract, strategy interface, Variant Library taxonomy
      metrics.md            #   Comparison Dashboard metric set + where each number comes from
      checkpoints.md        #   the user checkpoint every 3 rounds
      deliverables.md       #   final outputs + success criteria
      overfitting.md        #   robustness / anti-overfitting guardrails
    scripts/
      scorecard.py          #   standardized per-variant metrics (reuses quantstats_report)
      dashboard.py          #   rank baseline + variants, render comparison dashboard
      evolution_log.py      #   append-only JSONL journal of rounds / variants / decisions
assets/                     # fixtures / images
```

## Quick start (as a developer using this skill)

1. Create an app at https://myapi.fyers.in/dashboard/ and note the app id, secret,
   and redirect URI.
2. Create a venv and install deps: `python3 -m venv .venv && source .venv/bin/activate
   && pip install -r requirements.txt` (see `references/setup.md` for the package list
   and install troubleshooting).
3. Ask your agent to build a strategy/bot/backtest. By default the agent drives setup and
   development *with* you conversationally: it scaffolds `.env` (keys present, values
   blank) in the project folder and waits for you to fill in the values yourself, then
   runs the login flow and verifies it against `/profile` — see `references/auth.md`. It
   then builds the strategy incrementally, running each piece for real and showing you
   the output as it goes (see `SKILL.md`'s "Default mode: conversational execution").
   Once the strategy runs, it **automatically backtests it and generates a QuantStats tear
   sheet** (Sharpe/Sortino/drawdown + monthly heatmap) without you having to ask, then offers
   Supercharge Mode. If you'd rather just get code without the agent running anything, say so
   explicitly.
4. Prefer to do it yourself instead? `cp skills/fyers-trading/.env.example .env`, fill in
   the values, run `python skills/fyers-trading/scripts/fyers_login.py` (caches the daily
   token), then verify with `python skills/fyers-trading/scripts/fyers_client.py profile`.

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
npx skills add FyersDev/fyers-skills          # discovers all skills, prompts you to pick
npx skills add FyersDev/fyers-skills -g        # global (user-level)
npx skills add FyersDev/fyers-skills --all     # all skills, all detected agents, no prompts
```

This walks the repo, finds every `SKILL.md` under `skills/`, installs the ones you pick
(`fyers-trading`, `fyers-supercharge`), and wires them into the agent directories it
detects. Useful flags: `-a/--agent <agents>` to target specific agents, `-l/--list` to
preview without installing, `--copy` to copy files instead of symlinking. List or remove
later with `npx skills list` / `npx skills remove fyers-trading`.

> **Supercharge Mode depends on the core skill.** `fyers-supercharge` calls into
> `fyers-trading`'s scripts, so install both (`--all`, or pick both at the prompt).

> **Requires a public repo.** `skills add` fetches over public GitHub and has no
> private-repo/token support — if `FyersDev/fyers-skills` is private the command 404s.
> Make the repo public, or use the manual install below.

### Manual install (works with a private repo over SSH)

Each skill's folder name matches its `name:` (`fyers-trading`, `fyers-supercharge`).
Clone the repo, then copy each skill folder from `skills/` into the host's skills directory:

```bash
git clone git@github.com:FyersDev/fyers-skills.git && cd fyers-skills
```

**Claude Code**
```bash
mkdir -p .claude/skills                      # per-project (or ~/.claude/skills for per-user)
cp -r skills/fyers-trading skills/fyers-supercharge .claude/skills/
```
Invoke with `/fyers-trading` / `/fyers-supercharge`, or just describe a FYERS task.

**Cursor**
```bash
mkdir -p .cursor/skills
cp -r skills/fyers-trading skills/fyers-supercharge .cursor/skills/
```
Reload the window after copying.

**Open Claw**
```bash
mkdir -p ~/.openclaw/skills
cp -r skills/fyers-trading skills/fyers-supercharge ~/.openclaw/skills/
```

> Skills-directory paths vary by host version. If a host can't find the skill, check
> its docs for the active skills directory and confirm the folder name matches `name:`.

## Validate before importing

```bash
skills/fyers-trading/scripts/validate-skill.sh skills/fyers-trading/SKILL.md
skills/fyers-trading/scripts/validate-skill.sh skills/fyers-supercharge/SKILL.md
python -m py_compile skills/fyers-trading/scripts/*.py skills/fyers-supercharge/scripts/*.py
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
