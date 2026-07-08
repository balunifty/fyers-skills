#!/usr/bin/env python3
"""End-to-end FYERS v3 strategy template: data -> signal -> (dry-run) order.

This is a SKELETON to copy and adapt, not a profitable strategy. It demonstrates
the safe pattern the skill enforces:

  1. Authenticate via the cached daily token.
  2. Pull historical candles for a symbol.
  3. Compute a simple signal (SMA crossover) with no look-ahead.
  4. Decide an action and place it in DRY-RUN by default.

Live placement requires the explicit --live flag AND a typed confirmation, so it
cannot fire by accident.

Usage:
    python scripts/example_strategy.py --symbol NSE:SBIN-EQ           # dry-run
    python scripts/example_strategy.py --symbol NSE:SBIN-EQ --live    # real order (asks to confirm)

Requires a valid token: python scripts/fyers_login.py
"""
from __future__ import annotations

import argparse
import datetime as dt
import sys

try:
    from .fyers_client import FyersClient, FyersAuthError
except ImportError:
    from fyers_client import FyersClient, FyersAuthError


def sma(values: list[float], window: int) -> list[float | None]:
    out: list[float | None] = []
    for i in range(len(values)):
        if i + 1 < window:
            out.append(None)
        else:
            out.append(sum(values[i + 1 - window:i + 1]) / window)
    return out


def compute_signal(closes: list[float], fast: int = 20, slow: int = 50) -> str:
    """Return 'BUY', 'SELL', or 'HOLD' from an SMA crossover on closed candles.

    Uses only completed candles (the caller should drop the partial last candle),
    avoiding look-ahead bias.
    """
    if len(closes) < slow + 1:
        return "HOLD"
    f, s = sma(closes, fast), sma(closes, slow)
    # compare the last two fully-formed points for a crossover
    if None in (f[-1], f[-2], s[-1], s[-2]):
        return "HOLD"
    crossed_up = f[-2] <= s[-2] and f[-1] > s[-1]
    crossed_down = f[-2] >= s[-2] and f[-1] < s[-1]
    if crossed_up:
        return "BUY"
    if crossed_down:
        return "SELL"
    return "HOLD"


def main() -> int:
    ap = argparse.ArgumentParser(description="FYERS strategy skeleton (dry-run by default)")
    ap.add_argument("--symbol", default="NSE:SBIN-EQ")
    ap.add_argument("--resolution", default="15")
    ap.add_argument("--qty", type=int, default=1)
    ap.add_argument("--days", type=int, default=60, help="lookback window in days")
    ap.add_argument("--live", action="store_true", help="place a REAL order (asks to confirm)")
    args = ap.parse_args()

    try:
        fc = FyersClient()
    except FyersAuthError as e:
        print(e)
        return 1

    today = dt.date.today()
    start = today - dt.timedelta(days=args.days)
    resp = fc.history(args.symbol, args.resolution,
                      range_from=start.isoformat(), range_to=today.isoformat())
    if resp.get("s") != "ok" or not resp.get("candles"):
        print(f"history fetch failed: {resp}")
        return 1

    candles = resp["candles"]
    # Drop the most recent candle — it may still be forming.
    closes = [c[4] for c in candles[:-1]]
    last_price = closes[-1] if closes else None
    print(f"{args.symbol}: {len(closes)} closed candles, last close={last_price}")

    signal = compute_signal(closes)
    print(f"Signal: {signal}")
    if signal == "HOLD":
        print("No action.")
        return 0

    order = {
        "symbol": args.symbol,
        "qty": args.qty,
        "type": 2,                       # 2 = Market
        "side": 1 if signal == "BUY" else -1,
        "productType": "INTRADAY",
        "limitPrice": 0,
        "stopPrice": 0,
        "validity": "DAY",
        "offlineOrder": False,
        "orderTag": "examplebot",
    }

    dry_run = not args.live
    if args.live:
        print("\n*** LIVE ORDER REQUESTED — this will use REAL money. ***")
        confirm = input(f"Type the symbol '{args.symbol}' to confirm: ").strip()
        if confirm != args.symbol:
            print("Confirmation failed — staying in dry-run.")
            dry_run = True

    result = fc.place_order(order, dry_run=dry_run)
    print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
