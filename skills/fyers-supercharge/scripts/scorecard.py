#!/usr/bin/env python3
"""Standardized variant scorecard for Supercharge Mode (stdlib-first).

Turns one strategy variant's backtest output into the SINGLE, uniform metric dict that the
comparison dashboard ranks on. Every variant — and the immutable baseline — is scored by this
exact function so the numbers are comparable (that is the whole point of the optimization loop).

TWO INPUTS, per `references/variants.md`'s variant contract:
    returns : the per-bar returns series a backtest produces (fyers-trading's `df["strat_ret"]`,
              see that skill's references/backtesting.md §2). A pandas Series, list, or ndarray.
    trades  : the list of closed trades — [{"pnl": float, ...}, ...]. Needed for the trade-level
              metrics QuantStats structurally cannot compute (it never sees "trades").

REUSES fyers-trading's engine when available. The risk metrics (Sharpe, Sortino, max drawdown,
CAGR, Calmar, annualized vol, positive-period %) come from fyers-trading's
`scripts/quantstats_report.py` via `key_metrics()` — the SAME code that scored the baseline — so
a variant is never measured by a different yardstick. If that skill isn't importable (supercharge
installed without fyers-trading, or QuantStats not installed), we fall back to a small pure-stdlib
computation of Sharpe/Sortino/max-drawdown so this script still runs and its `demo` always works.
Full-fidelity scoring needs fyers-trading + QuantStats installed — see the skill's SKILL.md.

TWO WIN RATES (do not conflate — mirrors fyers-trading/references/quantstats.md):
    win_rate            — TRADE-LEVEL: winning trades / total trades. Computed here from `trades`.
    positive_period_pct — QuantStats' % of positive PERIODS (e.g. days). From the returns series.
They answer different questions; the scorecard reports both under distinct keys.

Functions
---------
    scorecard(returns, trades=None, capital=None, rf=0.0, periods_per_year=252,
              oos_fraction=0.3, capital_utilization=None, execution_risk=None) -> dict
    trade_metrics(trades, capital=None) -> dict      just the trade-level figures
    robustness_score(returns, oos_fraction=0.3, ...) -> float   in/out-of-sample Sharpe stability

Usage
-----
    from scorecard import scorecard
    card = scorecard(variant_df["strat_ret"], trades=variant_trades, capital=100_000)

CLI
---
    python scorecard.py demo     # synthetic returns + trades -> a full scorecard, no token/network
"""
from __future__ import annotations

import math
import sys

# The dashboard-facing key order (see references/metrics.md). Kept as the canonical schema so
# every scorecard dict has the same shape even when some fields are None (not derivable).
SCORECARD_KEYS = [
    "net_profit", "return_pct", "profit_factor", "sharpe", "sortino",
    "win_rate", "avg_profit", "avg_loss", "max_drawdown", "trade_count",
    "return_on_capital", "capital_utilization", "largest_loss",
    "longest_losing_streak", "execution_risk", "robustness_score",
    # Reported alongside but deliberately distinct from win_rate:
    "positive_period_pct", "cagr", "calmar", "volatility_ann",
]


# ---------------------------------------------------------------------------
# fyers-trading reuse (with graceful fallback)
# ---------------------------------------------------------------------------

def _load_quantstats():
    """Return fyers-trading's `(key_metrics, daily_returns)`, or `(None, None)` if unavailable.

    Tries a plain import first (works when the host puts both skills' scripts/ on sys.path),
    then probes for a sibling `fyers-trading/scripts` directory next to this skill and imports
    from there. Any failure (missing skill, missing QuantStats) returns (None, None) -> fallback.

    Both functions are returned together because scoring must mirror fyers-trading's documented
    flow: `daily_returns()` FIRST (resample intraday->daily, stamp an IST index), THEN
    `key_metrics()` — otherwise annualized Sharpe/drawdown/CAGR on an intraday bar series are
    wrong and the variant wouldn't be measured on the same yardstick as the baseline.
    """
    try:
        from quantstats_report import key_metrics, daily_returns  # type: ignore
        return key_metrics, daily_returns
    except Exception:  # noqa: BLE001 — fall through to sibling-path probe
        pass

    import os
    here = os.path.dirname(os.path.abspath(__file__))
    # this file: .../skills/fyers-supercharge/scripts/scorecard.py
    # sibling:   .../skills/fyers-trading/scripts
    candidates = [
        os.path.normpath(os.path.join(here, "..", "..", "fyers-trading", "scripts")),
    ]
    for cand in candidates:
        qs = os.path.join(cand, "quantstats_report.py")
        if os.path.isfile(qs):
            if cand not in sys.path:
                sys.path.insert(0, cand)
            try:
                from quantstats_report import key_metrics, daily_returns  # type: ignore
                return key_metrics, daily_returns
            except Exception:  # noqa: BLE001 — e.g. QuantStats not installed
                return None, None
    return None, None


# ---------------------------------------------------------------------------
# Pure-stdlib returns helpers (fallback path — no numpy/pandas required)
# ---------------------------------------------------------------------------

def _as_floats(returns) -> list[float]:
    """Coerce a pandas Series / list / ndarray of per-bar returns into a clean float list."""
    try:
        values = list(returns.values)  # pandas Series / ndarray
    except AttributeError:
        values = list(returns)
    out: list[float] = []
    for v in values:
        try:
            f = float(v)
        except (TypeError, ValueError):
            continue
        if math.isfinite(f):
            out.append(f)
    return out


def _mean(xs: list[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def _std(xs: list[float]) -> float:
    """Sample standard deviation (ddof=1)."""
    n = len(xs)
    if n < 2:
        return 0.0
    m = _mean(xs)
    var = sum((x - m) ** 2 for x in xs) / (n - 1)
    return math.sqrt(var)


def _sharpe(rets: list[float], rf: float, ppy: int) -> float:
    """Annualized Sharpe from a periodic returns list (rf is the annual risk-free rate)."""
    if len(rets) < 2:
        return 0.0
    per_period_rf = rf / ppy
    excess = [r - per_period_rf for r in rets]
    sd = _std(excess)
    if sd == 0:
        return 0.0
    return (_mean(excess) / sd) * math.sqrt(ppy)


def _sortino(rets: list[float], rf: float, ppy: int) -> float:
    """Annualized Sortino: uses downside deviation (negative excess returns only)."""
    if len(rets) < 2:
        return 0.0
    per_period_rf = rf / ppy
    excess = [r - per_period_rf for r in rets]
    downside = [min(0.0, e) for e in excess]
    dd = math.sqrt(sum(d * d for d in downside) / len(downside)) if downside else 0.0
    if dd == 0:
        return 0.0
    return (_mean(excess) / dd) * math.sqrt(ppy)


def _max_drawdown(rets: list[float]) -> float:
    """Max drawdown of the compounded equity curve, as a negative fraction (e.g. -0.23)."""
    if not rets:
        return 0.0
    equity = 1.0
    peak = 1.0
    mdd = 0.0
    for r in rets:
        equity *= (1.0 + r)
        peak = max(peak, equity)
        if peak > 0:
            dd = equity / peak - 1.0
            mdd = min(mdd, dd)
    return mdd


def _fallback_risk_metrics(returns, rf: float, ppy: int) -> dict:
    """Stdlib risk metrics matching key_metrics()'s keys when fyers-trading isn't available.

    Assumes the returns are at a REGULAR periodic frequency (default daily, ppy=252) — the same
    assumption QuantStats makes. cagr/calmar are left None here (they need calendar dates, which
    the full quantstats_report path supplies); the dashboard tolerates None."""
    rets = _as_floats(returns)
    mdd = _max_drawdown(rets)
    positive = sum(1 for r in rets if r > 0)
    return {
        "sharpe": round(_sharpe(rets, rf, ppy), 6),
        "sortino": round(_sortino(rets, rf, ppy), 6),
        "max_drawdown": round(mdd, 6),
        "cagr": None,
        "calmar": None,
        "volatility_ann": round(_std(rets) * math.sqrt(ppy), 6),
        "positive_period_pct": round(positive / len(rets), 6) if rets else 0.0,
    }


def _risk_metrics(returns, rf: float, ppy: int, freq: str = "auto") -> tuple:
    """Risk metrics via fyers-trading's key_metrics() if possible, else stdlib fallback.

    Mirrors fyers-trading/references/backtesting.md's flow: normalize with `daily_returns(freq)`
    FIRST (intraday bar returns -> compounded daily, IST-indexed), THEN `key_metrics()`. Skipping
    the normalization would annualize an intraday `strat_ret` series wrongly, so a variant would
    be scored on a different yardstick than the baseline. Pass freq="D" when the returns are
    already daily to skip resampling. Returns (metrics_dict, used_quantstats)."""
    key_metrics, daily_returns = _load_quantstats()
    if key_metrics is not None and daily_returns is not None:
        try:
            rets = daily_returns(returns, freq=freq)   # normalize exactly like the baseline
            return dict(key_metrics(rets, rf=rf)), True
        except Exception:  # noqa: BLE001 — bad/short series, missing dep at call time, etc.
            pass
    return _fallback_risk_metrics(returns, rf, ppy), False


# ---------------------------------------------------------------------------
# Trade-level metrics (QuantStats cannot compute these — it never sees trades)
# ---------------------------------------------------------------------------

def trade_metrics(trades: "list[dict] | None", capital: "float | None" = None) -> dict:
    """Compute trade-level figures from a list of closed trades.

    Each trade is a dict with at least a ``pnl`` (float, in currency). Returns a dict with:
    net_profit, profit_factor, win_rate (winning/total), avg_profit (avg win), avg_loss
    (avg loss, negative), trade_count, largest_loss, longest_losing_streak, return_on_capital.
    Missing/empty trades -> a dict of zeros/None (still shaped for the dashboard).
    """
    keys = ["net_profit", "profit_factor", "win_rate", "avg_profit", "avg_loss",
            "trade_count", "largest_loss", "longest_losing_streak", "return_on_capital"]
    empty = {k: (0 if k in ("trade_count", "longest_losing_streak") else None) for k in keys}
    if not trades:
        return empty

    pnls: list[float] = []
    for t in trades:
        try:
            pnls.append(float(t["pnl"]))
        except (KeyError, TypeError, ValueError):
            continue
    if not pnls:
        return empty

    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    net_profit = sum(pnls)

    # Longest consecutive losing streak.
    longest = streak = 0
    for p in pnls:
        if p < 0:
            streak += 1
            longest = max(longest, streak)
        else:
            streak = 0

    profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else None  # None = no losses

    return {
        "net_profit": round(net_profit, 4),
        "profit_factor": round(profit_factor, 4) if profit_factor is not None else None,
        "win_rate": round(len(wins) / len(pnls), 6),
        "avg_profit": round(gross_profit / len(wins), 4) if wins else None,
        "avg_loss": round(sum(losses) / len(losses), 4) if losses else None,
        "trade_count": len(pnls),
        "largest_loss": round(min(pnls), 4),
        "longest_losing_streak": longest,
        "return_on_capital": round(net_profit / capital, 6) if capital else None,
    }


# ---------------------------------------------------------------------------
# Robustness (backs the Backtest-Integrity seat & the Robustness Score)
# ---------------------------------------------------------------------------

def robustness_score(returns, oos_fraction: float = 0.3, rf: float = 0.0,
                     periods_per_year: int = 252) -> float:
    """A 0..1 stability score: how well in-sample edge survives out-of-sample.

    Splits the returns chronologically into in-sample (first 1-oos_fraction) and out-of-sample
    (last oos_fraction), and compares Sharpe. score = clamp(oos_sharpe / is_sharpe, 0, 1) when
    the in-sample Sharpe is positive; 0 otherwise. A curve-fit strategy whose edge vanishes
    out-of-sample scores near 0; one that holds up scores near 1. Deliberately simple and
    explainable — see references/overfitting.md for the fuller guardrails this summarizes.
    """
    rets = _as_floats(returns)
    if len(rets) < 4:
        return 0.0
    split = int(len(rets) * (1.0 - oos_fraction))
    in_sample, oos = rets[:split], rets[split:]
    if len(in_sample) < 2 or len(oos) < 2:
        return 0.0
    is_sharpe = _sharpe(in_sample, rf, periods_per_year)
    oos_sharpe = _sharpe(oos, rf, periods_per_year)
    if is_sharpe <= 0:
        return 0.0
    return round(max(0.0, min(1.0, oos_sharpe / is_sharpe)), 6)


# ---------------------------------------------------------------------------
# The scorecard
# ---------------------------------------------------------------------------

def scorecard(returns, trades: "list[dict] | None" = None, capital: "float | None" = None,
              rf: float = 0.0, periods_per_year: int = 252, oos_fraction: float = 0.3,
              freq: str = "auto", capital_utilization: "float | None" = None,
              execution_risk: "float | None" = None) -> dict:
    """Score one variant into the uniform dashboard dict (keys = SCORECARD_KEYS).

    Args:
        returns: per-bar returns series (pandas Series / list / ndarray) — the variant contract.
        trades:  list of closed-trade dicts with a ``pnl`` field (for trade-level metrics).
        capital: starting capital, for return_on_capital / return_pct.
        rf:      annual risk-free rate (e.g. 0.07).
        periods_per_year: annualization factor for the stdlib fallback (252 = daily).
        oos_fraction:     out-of-sample tail fraction for the robustness score.
        freq:    normalization frequency passed to fyers-trading's `daily_returns()` before
            scoring — "auto" (default) resamples intraday bar returns down to daily (matching the
            baseline), "D" if the returns are already daily. Every variant and the baseline must
            use the SAME freq so the comparison stays apples-to-apples.
        capital_utilization / execution_risk: optional caller-supplied estimates (the Execution
            and Capital-Efficiency seats provide these; they are NOT derivable from returns alone,
            so they default to None rather than being fabricated).

    The returned dict always has every key in SCORECARD_KEYS (None where a figure isn't
    available), plus a `_meta` sub-dict noting whether fyers-trading's QuantStats was used.
    """
    risk, used_qs = _risk_metrics(returns, rf, periods_per_year, freq=freq)
    tm = trade_metrics(trades, capital=capital)

    net_profit = tm.get("net_profit")
    return_pct = round(net_profit / capital, 6) if (capital and net_profit is not None) else None

    card = {
        "net_profit": net_profit,
        "return_pct": return_pct,
        "profit_factor": tm.get("profit_factor"),
        "sharpe": risk.get("sharpe"),
        "sortino": risk.get("sortino"),
        "win_rate": tm.get("win_rate"),
        "avg_profit": tm.get("avg_profit"),
        "avg_loss": tm.get("avg_loss"),
        "max_drawdown": risk.get("max_drawdown"),
        "trade_count": tm.get("trade_count"),
        "return_on_capital": tm.get("return_on_capital"),
        "capital_utilization": capital_utilization,
        "largest_loss": tm.get("largest_loss"),
        "longest_losing_streak": tm.get("longest_losing_streak"),
        "execution_risk": execution_risk,
        "robustness_score": robustness_score(returns, oos_fraction, rf, periods_per_year),
        "positive_period_pct": risk.get("positive_period_pct"),
        "cagr": risk.get("cagr"),
        "calmar": risk.get("calmar"),
        "volatility_ann": risk.get("volatility_ann"),
    }
    card["_meta"] = {
        "scored_with_quantstats": used_qs,
        "periods_per_year": periods_per_year,
        "rf": rf,
        "freq": freq,
    }
    return card


# ---------------------------------------------------------------------------
# CLI demo (no token / no network)
# ---------------------------------------------------------------------------

def _demo() -> int:
    """Synthetic returns + trades -> a full scorecard. Runs with or without QuantStats."""
    import json
    import random

    rng = random.Random(7)
    # ~1 year of daily returns with slight positive drift.
    rets = [rng.gauss(0.0006, 0.011) for _ in range(252)]
    # A handful of synthetic closed trades.
    trades = [{"pnl": rng.gauss(120, 400)} for _ in range(60)]

    card = scorecard(rets, trades=trades, capital=100_000, rf=0.07)
    used = card["_meta"]["scored_with_quantstats"]
    print(f"scored_with_quantstats = {used}  "
          f"({'reused fyers-trading key_metrics()' if used else 'stdlib fallback'})")
    print(json.dumps(card, indent=2))

    print("\nnote: win_rate is TRADE-LEVEL; positive_period_pct is % of positive DAYS — "
          "distinct figures, never conflate them (see references/metrics.md).")
    return 0


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "demo":
        raise SystemExit(_demo())
    print(__doc__)
