#!/usr/bin/env python3
"""Fetch and list all F&O (Futures & Options) stocks available on NSE.

Downloads the NSE_FO symbol master from FYERS and extracts unique underlying
stocks that have active derivatives. Outputs a clean list with symbol details.

Usage:
    python strategies/scripts/GetFNOScriptList.py
    python strategies/scripts/GetFNOScriptList.py --csv fno_stocks.csv
    python strategies/scripts/GetFNOScriptList.py --refresh
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import os
import pathlib
import sys

SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parents[1]
STRATEGIES_DIR = SCRIPT_DIR.parent
sys.path.insert(0, str(REPO_ROOT / "skills" / "fyers-trading" / "scripts"))

from fyers_symbols import load_master, _expiry_iso

OUTPUT_DIR = STRATEGIES_DIR / "data"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


def get_fno_stocks(force_refresh: bool = False) -> list[dict]:
    """Extract unique F&O stocks from the NSE_FO master.

    Returns a list of dicts with stock symbol, name, lot size, and expiry info.
    """
    data = load_master("NSE_FO", force=force_refresh)

    stocks = {}
    for sym, v in data.items():
        strike = v.get("strikePrice", -1)

        # Use exSymbol which contains the clean underlying name
        underlying = v.get("exSymbol", "")
        if not underlying:
            continue

        # Skip indices
        if underlying in ("NIFTY", "BANKNIFTY", "FINNIFTY"):
            continue
        if strike < 0:
            continue

        # Extract lot size and expiry
        lot_size = v.get("minLotSize", 0)
        expiry = _expiry_iso(v.get("expiryDate", ""))
        fy_token = v.get("fyToken", "")
        sym_ticker = v.get("symTicker", sym)

        # Determine instrument type
        opt_type = v.get("optType", "XX")
        inst_type_val = v.get("exInstType", 0)
        if opt_type in ("CE", "PE"):
            inst_type = "OPT"
        elif inst_type_val == 13:
            inst_type = "FUT"
        else:
            inst_type = "EQ"

        if underlying not in stocks:
            stocks[underlying] = {
                "underlying": underlying,
                "name": v.get("short_name", ""),
                "lot_size": lot_size,
                "expiries": set(),
                "inst_types": set(),
                "sample_symbol": sym_ticker,
                "fy_token": fy_token,
            }

        stocks[underlying]["lot_size"] = lot_size
        stocks[underlying]["expiries"].add(expiry)
        stocks[underlying]["inst_types"].add(inst_type)
        if fy_token:
            stocks[underlying]["fy_token"] = fy_token
            stocks[underlying]["sample_symbol"] = sym_ticker

    result = []
    for underlying, info in sorted(stocks.items()):
        result.append({
            "underlying": underlying,
            "name": info["name"],
            "lot_size": info["lot_size"],
            "expiries": sorted(info["expiries"]),
            "inst_types": sorted(info["inst_types"]),
            "sample_symbol": info["sample_symbol"],
            "fy_token": info["fy_token"],
        })

    return result


def save_json(stocks: list[dict], path: pathlib.Path) -> None:
    """Save stock list to JSON."""
    with open(path, "w", encoding="utf-8") as f:
        json.dump(stocks, f, indent=2, ensure_ascii=False)
    print(f"Saved {len(stocks)} stocks to {path}")


def save_csv(stocks: list[dict], path: pathlib.Path) -> None:
    """Save stock list to CSV."""
    if not stocks:
        print("No stocks to save")
        return

    fieldnames = ["underlying", "name", "lot_size", "expiries", "inst_types", "sample_symbol", "fy_token"]
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for stock in stocks:
            row = stock.copy()
            row["expiries"] = "|".join(row["expiries"])
            row["inst_types"] = "|".join(row["inst_types"])
            writer.writerow(row)
    print(f"Saved {len(stocks)} stocks to {path}")


def save_txt(stocks: list[dict], path: pathlib.Path) -> None:
    """Save stock list as plain text (one symbol per line)."""
    with open(path, "w", encoding="utf-8") as f:
        for stock in stocks:
            f.write(f"{stock['underlying']}\n")
    print(f"Saved {len(stocks)} symbols to {path}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Get list of all F&O stocks on NSE")
    parser.add_argument("--csv", type=str, help="Export to CSV file")
    parser.add_argument("--json", type=str, help="Export to JSON file")
    parser.add_argument("--txt", type=str, help="Export to TXT file (symbols only)")
    parser.add_argument("--refresh", action="store_true", help="Force re-download of master file")
    parser.add_argument("--json-only", action="store_true", help="Output JSON to stdout only")
    args = parser.parse_args()

    try:
        stocks = get_fno_stocks(force_refresh=args.refresh)
        print(f"\nFound {len(stocks)} F&O stocks on NSE\n")

        if not args.json_only:
            print(f"{'Underlying':<20} {'Name':<30} {'Lot Size':<10} {'Expiries':<15} {'Types':<10}")
            print("-" * 85)
            for s in stocks:
                expiries = ", ".join(s["expiries"][:2])
                if len(s["expiries"]) > 2:
                    expiries += f" +{len(s['expiries'])-2}"
                print(f"{s['underlying']:<20} {s['name'][:28]:<30} {s['lot_size']:<10} {expiries:<15} {','.join(s['inst_types']):<10}")

        # Default save
        if not args.csv and not args.json and not args.txt:
            save_json(stocks, OUTPUT_DIR / "fno_stocks.json")
            save_txt(stocks, OUTPUT_DIR / "fno_stocks.txt")

        if args.csv:
            save_csv(stocks, pathlib.Path(args.csv))
        if args.json:
            save_json(stocks, pathlib.Path(args.json))
        if args.txt:
            save_txt(stocks, pathlib.Path(args.txt))

        if args.json_only:
            print(json.dumps(stocks, indent=2))

    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
