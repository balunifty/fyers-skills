# Setting up this skill's Python environment

The skill's own `scripts/*.py` are stdlib-only and need no install. This setup is for
the **strategy/bot/backtest code the skill generates** — it uses third-party packages
(`fyers-apiv3`, `pandas`, etc.) that must be installed before that code can run.

## 1. Create a project-local venv

Always use a **venv scoped to the project**, not the system Python — keeps FYERS
strategy deps isolated from anything else on the machine.

```bash
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
python -m pip install --upgrade pip
```

## 2. Install requirements

```bash
pip install -r requirements.txt
```

`requirements.txt` (repo root) pins the default set:

| Package | Why |
|---|---|
| `fyers-apiv3` | Official SDK — WebSocket streaming (`references/websocket.md`) and a supported alternative to raw REST for auth/orders. |
| `pandas` | Candle data → DataFrame; used throughout `references/backtesting.md`. |
| `numpy` | Vectorized signal math (SMA, returns, indicators). |
| `python-dotenv` | Load `FYERS_APP_ID` / `FYERS_SECRET_ID` / `FYERS_REDIRECT_URI` / `FYERS_PIN` from `.env` instead of hand-rolling env parsing. Under the default conversational flow the agent scaffolds `.env` (keys present, values blank) and the user fills the values in directly — see `references/auth.md` for the step-by-step; don't assume the user has already done this before you start. |
| `vectorbt` | Default backtest engine — vectorized, fast, handles portfolio-level backtests. See `references/backtesting.md`. |

**Optional, install only if the strategy needs it** (not in the default
`requirements.txt` — add on demand):

| Package | When |
|---|---|
| `backtesting` (the `backtesting.py` package) | User wants a simpler single-instrument backtest with built-in plotting, instead of vectorbt. |
| `backtrader` | User wants event-driven backtesting (bar-by-bar broker simulation) rather than vectorized. |
| `TA-Lib` | Strategy needs indicators (RSI, MACD, Bollinger, ATR, etc.) beyond a few `pandas.rolling()` calls. Needs the **TA-Lib C library installed first**, then `pip install TA-Lib` — a common source of the install-error pattern in §3 below. Full install steps + usage: `references/indicators.md`, `scripts/indicators.py`. |
| `matplotlib` | Only if the user wants custom plots beyond a backtest library's built-in charting. |
| `quantstats` | User wants a performance tear sheet / risk viz (Sharpe, drawdown, monthly returns) on a backtest's returns series. Pulls in matplotlib/seaborn/scipy. Install steps + usage: `references/quantstats.md`, `scripts/quantstats_report.py`. |

## 3. If a package install fails

Some packages (e.g. `vectorbt`, `ta-lib`) pull in compiled dependencies (numba, LLVM
bindings, C extensions) that occasionally fail to build a wheel for the local Python
version or OS. When `pip install -r requirements.txt` errors out on one package:

1. **Install everything else first**, then retry the failing package alone — don't let
   one bad package block the rest:
   ```bash
   pip install -r requirements.txt || true
   pip install <failing-package>       # see the actual error
   ```
2. Read the actual pip error before guessing — usually one of:
   - **No wheel for this Python version** → `python3 -m pip install --upgrade pip`
     and retry (pip's wheel resolution improves), or pin an older compatible version
     from the package's PyPI page.
   - **Missing system build tools** (`error: Microsoft Visual C++ ...`, `gcc: command
     not found`) → the package needs a compiler; install OS build tools rather than
     giving up on the package (Linux: `build-essential`; macOS: Xcode command line
     tools; Windows: prefer a prebuilt wheel over building from source).
3. Once resolved, re-run `pip install -r requirements.txt` to confirm everything is
   installed, **then** move on to strategy generation. Don't generate strategy code
   against an environment with unresolved import errors.

## 4. Verify

```bash
python -c "import fyers_apiv3, pandas, numpy, dotenv, vectorbt; print('OK')"
```

Then proceed to Step 1 in `SKILL.md` (authenticate) and strategy generation.
