#!/usr/bin/env python3
"""Fetch top N most liquid F&O stocks based on option chain liquidity metrics.

Measures liquidity using:
  - Total Open Interest (CE + PE)
  - Total Volume (CE + PE)
  - Number of strikes with OI
  - Bid-ask spread (tighter = more liquid)

Usage:
    python strategies/scripts/GetTopFNOLiquidStocks.py --top 100
    python strategies/scripts/GetTopFNOLiquidStocks.py --top 50 --csv liquid_stocks.csv
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import pathlib
import sys
import time

SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parents[1]
STRATEGIES_DIR = SCRIPT_DIR.parent
sys.path.insert(0, str(REPO_ROOT / "skills" / "fyers-trading" / "scripts"))

from fyers_client import FyersAuthError, FyersClient  # noqa: E402
from fyers_symbols import load_master  # noqa: E402

OUTPUT_DIR = STRATEGIES_DIR / "data"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

MAX_API_CALLS_PER_SECOND = 5
TOP_N = 50


class ApiRateLimiter:
    def __init__(self, calls_per_second: int = MAX_API_CALLS_PER_SECOND):
        self.interval = 1.0 / calls_per_second
        self.last_call = 0.0

    def call(self, function, *args, **kwargs):
        elapsed = time.monotonic() - self.last_call
        if elapsed < self.interval:
            time.sleep(self.interval - elapsed)
        self.last_call = time.monotonic()
        return function(*args, **kwargs)


def log_message(msg: str) -> None:
    print(f"{dt.datetime.now().strftime('%H:%M:%S')} {msg}", file=sys.stderr)


def get_fno_symbols() -> list[tuple[str, str]]:
    """Get unique F&O stock symbols with their fyToken format."""
    data = load_master("NSE_FO")
    stocks = {}
    for sym, v in data.items():
        underlying = v.get("exSymbol", "")
        if not underlying or underlying in ("NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "NIFTYNXT50", "NIFTYFPI"):
            continue
        strike = v.get("strikePrice", -1)
        if strike >= 0:
            continue
        fy_token = v.get("fyToken", "")
        if underlying not in stocks:
            stocks[underlying] = f"NSE:{underlying}-EQ"
    return sorted(stocks.items())


def fetch_option_chain_liquidity(client: FyersClient, symbol: str, fy_symbol: str,
                                  limiter: ApiRateLimiter) -> dict | None:
    """Fetch option chain and calculate liquidity metrics."""
    try:
        response = limiter.call(client.option_chain, fy_symbol, strikecount=10, greeks=False)
        if response.get("s") != "ok":
            return None

        data = response.get("data", {})
        chains = data.get("optionsChain", [])
        call_oi = data.get("callOi", 0) or 0
        put_oi = data.get("putOi", 0) or 0

        if not chains:
            return None

        total_oi = 0
        total_volume = 0
        strikes_with_oi = set()
        bid_ask_spreads = []

        for item in chains:
            opt_type = item.get("option_type", "")
            if opt_type not in ("CE", "PE"):
                continue

            oi = item.get("oi", 0) or 0
            volume = item.get("volume", 0) or 0
            bid = item.get("bid", 0) or 0
            ask = item.get("ask", 0) or 0
            strike = item.get("strike_price", 0)

            total_oi += oi
            total_volume += volume

            if oi > 0 and strike > 0:
                strikes_with_oi.add(strike)

            if bid > 0 and ask > 0 and ask > bid:
                bid_ask_spreads.append(ask - bid)

        avg_spread = sum(bid_ask_spreads) / len(bid_ask_spreads) if bid_ask_spreads else 999
        num_strikes = len(strikes_with_oi)

        # Liquidity score formula
        liquidity_score = (
            (total_oi * 0.4) +
            (total_volume * 0.3) +
            (num_strikes * 5000 * 0.2) +
            ((1 / (avg_spread + 0.01)) * 50000 * 0.1)
        )

        return {
            "symbol": symbol,
            "total_oi": total_oi,
            "total_volume": total_volume,
            "strikes_with_oi": num_strikes,
            "avg_bid_ask_spread": round(avg_spread, 2),
            "liquidity_score": round(liquidity_score, 2),
        }
    except Exception as e:
        log_message(f"ERROR {symbol}: {e}")
        return None


def main() -> int:
    parser = argparse.ArgumentParser(description="Get top 50 liquid F&O stocks")
    parser.add_argument("--csv", type=str, help="Export to CSV file")
    parser.add_argument("--json", type=str, help="Export to JSON file")
    parser.add_argument("--top", type=int, default=TOP_N, help=f"Number of stocks (default: {TOP_N})")
    args = parser.parse_args()

    try:
        client = FyersClient()
        limiter = ApiRateLimiter()

        symbols = get_fno_symbols()
        log_message(f"Found {len(symbols)} F&O stocks. Fetching option chains...")

        results = []
        for i, (symbol, fy_symbol) in enumerate(symbols):
            if (i + 1) % 20 == 0:
                log_message(f"Progress: {i+1}/{len(symbols)}")
            result = fetch_option_chain_liquidity(client, symbol, fy_symbol, limiter)
            if result:
                results.append(result)

        results.sort(key=lambda x: x["liquidity_score"], reverse=True)
        top_stocks = results[:args.top]

        print(f"\n{'Rank':<6} {'Symbol':<20} {'Total OI':<15} {'Volume':<12} {'Strikes':<10} {'Avg Spread':<12} {'Score':<12}")
        print("-" * 90)
        for i, stock in enumerate(top_stocks, 1):
            print(f"{i:<6} {stock['symbol']:<20} {stock['total_oi']:<15,} {stock['total_volume']:<12,} "
                  f"{stock['strikes_with_oi']:<10} {stock['avg_bid_ask_spread']:<12.2f} {stock['liquidity_score']:<12,.0f}")

        # Save files
        default_csv = OUTPUT_DIR / f"top{args.top}_liquid_stocks.csv"
        default_json = OUTPUT_DIR / f"top{args.top}_liquid_stocks.json"

        csv_path = pathlib.Path(args.csv) if args.csv else default_csv
        json_path = pathlib.Path(args.json) if args.json else default_json

        save_csv(top_stocks, csv_path)
        save_json(top_stocks, json_path)

    except (FyersAuthError, Exception) as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    return 0


def save_csv(stocks: list[dict], path: pathlib.Path) -> None:
    fieldnames = ["symbol", "total_oi", "total_volume", "strikes_with_oi",
                  "avg_bid_ask_spread", "liquidity_score"]
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(stocks)
    print(f"\nSaved {len(stocks)} stocks to {path}")


def save_json(stocks: list[dict], path: pathlib.Path) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(stocks, f, indent=2)
    print(f"Saved {len(stocks)} stocks to {path}")


if __name__ == "__main__":
    raise SystemExit(main())
