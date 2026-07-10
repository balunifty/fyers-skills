#!/usr/bin/env python3
"""Performance tear sheets & risk analytics — wrappers around QuantStats for a returns series.

DEVIATION FROM THIS REPO'S "stdlib-only" CONVENTION: QuantStats is a heavy third-party
package (`pip install quantstats`) that pulls in pandas/numpy/scipy/matplotlib/seaborn to
compute portfolio risk metrics and render HTML tear sheets — it cannot be reimplemented in
pure stdlib. `quantstats` (and `matplotlib`) are imported lazily inside `_quantstats()` so
importing this module never crashes the rest of the skill's scripts when QuantStats isn't
installed — see `references/quantstats.md` for install steps. `pandas` is imported at module
top level because the whole skill's backtest layer already depends on it.

This is the VISUALIZATION / REPORTING layer that plugs in *after* a backtest. Feed it the
returns series a backtest produces (e.g. `df["strat_ret"]` from `references/backtesting.md`)
and it writes a professional HTML tear sheet (Sharpe, Sortino, drawdown, monthly heatmap,
rolling stats, …) plus headline risk metrics.

KEY GOTCHA — annualization needs a REGULAR (usually daily) frequency. FYERS candles are often
intraday (5-min, UTC epochs); QuantStats' time-based metrics (annualized Sharpe, CAGR, rolling
stats, monthly heatmap) assume returns at a regular periodic frequency. `daily_returns()`
resamples an intraday returns series down to daily (compounding within each day) and sets a
proper `Asia/Kolkata` DatetimeIndex, so annualization is meaningful. Pass `freq="D"` (already
daily) to skip resampling.

TWO WIN RATES — QuantStats analyzes PERIOD returns, so its "win rate" is the % of positive
PERIODS (e.g. positive days), NOT the trade-level win rate (winning trades / total trades)
that a backtest loop computes. They are complementary, not contradictory. This module exposes
QuantStats' figure as `positive_period_pct` and never labels it "win_rate". See the
"Two win rates" callout in `references/quantstats.md`.

Functions
---------
    daily_returns(data, freq="auto", equity=False)   build a clean daily IST returns Series
    html_report(returns, output="report.html", benchmark=None, title=..., rf=0.0)
                                                      write a full HTML tear sheet -> path
    metrics(returns, benchmark=None, mode="full", rf=0.0)   full metrics table (DataFrame)
    key_metrics(returns, rf=0.0)                      dict of headline risk metrics
    plots(returns, output_dir=".", benchmark=None)    save individual PNG plots -> [paths]

Usage
-----
    import pandas as pd
    from scripts.quantstats_report import daily_returns, html_report, key_metrics

    # df["strat_ret"] is the strategy's per-bar returns from references/backtesting.md
    rets = daily_returns(df["strat_ret"])          # intraday -> daily, IST index
    print(key_metrics(rets))
    html_report(rets, output="strategies/sma_crossover/report.html",
                title="SMA crossover — NSE:SBIN-EQ")

CLI
---
    python scripts/quantstats_report.py demo     # synthetic daily returns -> metrics + HTML
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def _quantstats():
    """Lazily import quantstats with a helpful error if it (or matplotlib) is missing."""
    try:
        import quantstats as qs  # noqa: F401
        return qs
    except ImportError as e:
        raise ImportError(
            "QuantStats is not installed. Run `pip install quantstats` (it pulls in "
            "pandas/numpy/scipy/matplotlib/seaborn). See references/quantstats.md for "
            "install steps and the headless-matplotlib (Agg backend) note."
        ) from e


def _use_headless_backend() -> None:
    """Force matplotlib's non-interactive Agg backend so plotting/HTML works on a headless
    server (no display). Safe to call repeatedly; must run before pyplot draws anything."""
    try:
        import matplotlib
        matplotlib.use("Agg", force=True)
    except ImportError:  # matplotlib rides in with quantstats; _quantstats() reports if absent
        pass


def _to_series(data) -> pd.Series:
    """Coerce a pandas Series / list / ndarray / dict into a float pandas Series.

    A DatetimeIndex on the input is preserved (needed for time-based metrics)."""
    if isinstance(data, pd.Series):
        s = data.astype(float)
    elif isinstance(data, pd.DataFrame):
        if data.shape[1] != 1:
            raise ValueError(
                f"expected a single returns column, got a DataFrame with {data.shape[1]} "
                f"columns — pass one column (e.g. df['strat_ret'])."
            )
        s = data.iloc[:, 0].astype(float)
    else:
        s = pd.Series(np.asarray(data, dtype=float))
    return s


def _ensure_ist_index(s: pd.Series) -> pd.Series:
    """Give the Series a tz-aware Asia/Kolkata DatetimeIndex.

    - Integer/float index that looks like UTC epoch seconds -> converted (matches the
      `pd.to_datetime(epoch, unit="s", utc=True).tz_convert("Asia/Kolkata")` pattern in
      references/backtesting.md §1).
    - Naive DatetimeIndex -> localized to IST; tz-aware -> converted to IST.
    - No usable datetime index (e.g. a plain RangeIndex from a list/array) -> a synthetic
      daily IST index is stamped so annualization has *a* regular frequency to work from.
    """
    idx = s.index
    if isinstance(idx, pd.DatetimeIndex):
        if idx.tz is None:
            s.index = idx.tz_localize("Asia/Kolkata")
        else:
            s.index = idx.tz_convert("Asia/Kolkata")
        return s
    if pd.api.types.is_numeric_dtype(idx) and len(idx) and idx.max() > 1_000_000_000:
        # looks like UTC epoch seconds
        dt = pd.to_datetime(idx, unit="s", utc=True).tz_convert("Asia/Kolkata")
        s.index = dt
        return s
    # No datetime information at all — stamp a synthetic daily IST index.
    s.index = pd.date_range("2020-01-01", periods=len(s), freq="D", tz="Asia/Kolkata")
    return s


def daily_returns(data, freq: str = "auto", equity: bool = False) -> pd.Series:
    """Build a clean DAILY returns Series (IST-indexed) ready for QuantStats.

    Args:
        data:   a returns Series/array (per-bar returns like df["strat_ret"]), OR — with
                `equity=True` — an equity curve (cumulative value), which is differenced into
                returns via `pct_change()`.
        freq:   "auto" (default) resamples intraday data down to daily, compounding within
                each day: ``(1 + r).prod() - 1``. Pass "D" if the input is ALREADY daily
                (skips resampling). Any other pandas offset alias (e.g. "W") resamples to that.
        equity: treat `data` as an equity/price curve instead of returns.

    Why resample: QuantStats' annualized metrics (Sharpe, CAGR, rolling stats, monthly
    heatmap) assume a regular periodic frequency. Feeding raw 5-min intraday returns produces
    nonsense annualization. Collapsing to one compounded return per day fixes that.

    Returns a float Series with a tz-aware Asia/Kolkata DatetimeIndex and NaNs dropped.
    """
    s = _to_series(data)
    if s.empty:
        raise ValueError("empty input — no returns/equity data to analyze.")

    if equity:
        s = s.pct_change()

    s = s.dropna()
    if s.empty:
        raise ValueError("all values are NaN after cleaning — nothing to analyze.")

    s = _ensure_ist_index(s)

    if freq and freq != "D":
        rule = "D" if freq == "auto" else freq
        # compound within each period: (1 + r).prod() - 1
        s = s.groupby(pd.Grouper(freq=rule)).apply(lambda x: (1.0 + x).prod() - 1.0)
        s = s.dropna()

    s = s[np.isfinite(s.values)]
    if s.empty:
        raise ValueError("no finite returns left after resampling.")
    s.name = "strategy"
    return s


def _check_points(s: pd.Series, minimum: int = 2) -> None:
    """Raise a clear error when there are too few points for meaningful (annualized) stats."""
    if len(s) < minimum:
        raise ValueError(
            f"need at least {minimum} return points for annualized metrics, got {len(s)}. "
            f"Backtest over a longer window, or pass daily (not per-trade) returns."
        )


def _clean_benchmark(benchmark) -> "pd.Series | None":
    """Coerce an optional benchmark into a daily IST returns Series (or None).

    Uses the same ``freq="auto"`` resampling the strategy path uses, so an INTRADAY
    benchmark is compounded down to daily exactly like the strategy returns were — a
    benchmark left at bar frequency would annualize wrong and misalign against a strategy
    aggregated with ``daily_returns(..., freq="auto")``. An already-daily benchmark passes
    through unchanged (daily resampling is idempotent on it)."""
    if benchmark is None:
        return None
    b = daily_returns(benchmark)   # freq="auto": resample intraday -> daily like the strategy
    b.name = "benchmark"
    return b


def html_report(
    returns,
    output: str = "report.html",
    benchmark=None,
    title: str = "FYERS Strategy",
    rf: float = 0.0,
) -> str:
    """Write a full standalone HTML tear sheet to `output` and return the path.

    This is the DEFAULT "visualize a backtest" path. `returns` should be a daily returns
    Series (run it through `daily_returns()` first if it's intraday). `benchmark`, when given,
    is an optional daily returns Series to compare against (pull one via
    `scripts/fyers_client.py history()` — e.g. NSE:NIFTY50-INDEX — NOT QuantStats' built-in
    yfinance download, which doesn't cover Indian markets well; see references/quantstats.md).
    `rf` is the annual risk-free rate (e.g. 0.07 for 7%).
    """
    qs = _quantstats()
    _use_headless_backend()
    r = _to_series(returns)
    r = _ensure_ist_index(r).dropna()
    _check_points(r)
    bench = _clean_benchmark(benchmark)
    qs.reports.html(r, benchmark=bench, rf=rf, output=output, title=title)
    return output


def metrics(returns, benchmark=None, mode: str = "full", rf: float = 0.0) -> pd.DataFrame:
    """Return QuantStats' metrics table as a DataFrame (mode="basic" or "full").

    Note: QuantStats reports its period "Win Rate" (% of positive periods) in this table.
    That is NOT the trade-level win rate — see `key_metrics()` and references/quantstats.md.
    """
    qs = _quantstats()
    r = _to_series(returns)
    r = _ensure_ist_index(r).dropna()
    _check_points(r)
    bench = _clean_benchmark(benchmark)
    return qs.reports.metrics(r, benchmark=bench, mode=mode, rf=rf, display=False)


def key_metrics(returns, rf: float = 0.0) -> dict:
    """Return a small dict of headline risk metrics for quick reporting in chat.

    Keys: sharpe, sortino, max_drawdown, cagr, calmar, volatility_ann, positive_period_pct.

    `positive_period_pct` is QuantStats' period win rate (fraction of positive periods),
    DELIBERATELY not labeled "win_rate" — the trade-level win rate (winning trades / total
    trades) comes from the backtest engine, which is the only layer that knows what a "trade"
    is. Reconcile the two for the user; never conflate them (see references/quantstats.md).
    """
    qs = _quantstats()
    r = _to_series(returns)
    r = _ensure_ist_index(r).dropna()
    _check_points(r)

    def _f(x) -> float:
        return round(float(x), 6)

    return {
        "sharpe":               _f(qs.stats.sharpe(r, rf=rf)),
        "sortino":              _f(qs.stats.sortino(r, rf=rf)),
        "max_drawdown":         _f(qs.stats.max_drawdown(r)),
        "cagr":                 _f(qs.stats.cagr(r)),
        "calmar":               _f(qs.stats.calmar(r)),
        "volatility_ann":       _f(qs.stats.volatility(r, annualize=True)),
        "positive_period_pct":  _f(qs.stats.win_rate(r)),   # % of positive PERIODS, not trades
    }


def plots(returns, output_dir: str = ".", benchmark=None) -> list[str]:
    """Save individual PNG plots (snapshot, drawdown, monthly heatmap) into `output_dir`.

    Returns the list of written file paths. Use `html_report()` for the full tear sheet;
    this is for embedding specific charts. Uses the headless Agg backend.

    When `benchmark` (a daily returns Series pulled via `scripts/fyers_client.py history()`,
    e.g. NSE:NIFTY50-INDEX — see references/quantstats.md) is given, two extra comparison
    charts are added: `returns.png` (cumulative strategy vs benchmark) and
    `active_heatmap.png` (monthly *active* returns, strategy − benchmark). QuantStats'
    `snapshot`/`drawdown` plots have no benchmark parameter, so those stay strategy-only.
    """
    import os

    qs = _quantstats()
    _use_headless_backend()
    r = _to_series(returns)
    r = _ensure_ist_index(r).dropna()
    _check_points(r)
    bench = _clean_benchmark(benchmark)
    os.makedirs(output_dir, exist_ok=True)

    paths: list[str] = []
    snap = os.path.join(output_dir, "snapshot.png")
    qs.plots.snapshot(r, savefig=snap, show=False)
    paths.append(snap)

    dd = os.path.join(output_dir, "drawdown.png")
    qs.plots.drawdown(r, savefig=dd, show=False)
    paths.append(dd)

    heat = os.path.join(output_dir, "monthly_heatmap.png")
    qs.plots.monthly_heatmap(r, savefig=heat, show=False)
    paths.append(heat)

    if bench is not None:
        # Cumulative strategy-vs-benchmark comparison.
        cmp = os.path.join(output_dir, "returns.png")
        qs.plots.returns(r, benchmark=bench, savefig=cmp, show=False)
        paths.append(cmp)

        # Monthly ACTIVE returns (strategy − benchmark); active=True is what makes
        # monthly_heatmap actually consume the benchmark.
        active_heat = os.path.join(output_dir, "active_heatmap.png")
        qs.plots.monthly_heatmap(r, benchmark=bench, active=True,
                                 savefig=active_heat, show=False)
        paths.append(active_heat)

    return paths


# ---------------------------------------------------------------------------
# CLI demo
# ---------------------------------------------------------------------------

def _demo() -> int:
    """Build a SYNTHETIC daily returns series (no token, no network), print headline metrics,
    and write an HTML tear sheet to a temp path. Confirms the headless (Agg) backend works."""
    import tempfile
    import os

    rng = np.random.default_rng(42)
    n = 504  # ~2 trading years of daily returns
    idx = pd.date_range("2023-01-02", periods=n, freq="B", tz="Asia/Kolkata")
    # slight positive drift so the tear sheet has something to show
    rets = pd.Series(rng.normal(loc=0.0006, scale=0.011, size=n), index=idx, name="strategy")

    r = daily_returns(rets, freq="D")   # already daily -> no resampling
    print(f"points: {len(r)}   from {r.index[0].date()} to {r.index[-1].date()}")

    km = key_metrics(r, rf=0.0)
    print("\nkey_metrics:")
    for k, v in km.items():
        print(f"  {k:22s} -> {v}")
    print("\nnote: positive_period_pct is the % of positive DAYS, NOT the trade-level win "
          "rate (see references/quantstats.md).")

    out = os.path.join(tempfile.gettempdir(), "fyers_quantstats_demo.html")
    path = html_report(r, output=out, title="Demo — synthetic daily returns")
    size = os.path.getsize(path) if os.path.exists(path) else 0
    print(f"\nHTML tear sheet written: {path} ({size} bytes)")

    # Error-path smoke test: too few points should give a clear error, not a cryptic one.
    try:
        key_metrics(pd.Series([0.01]))
    except ValueError as e:
        print(f"\nkey_metrics(1 point) raised: {e}")

    return 0


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "demo":
        raise SystemExit(_demo())
    print(__doc__)
