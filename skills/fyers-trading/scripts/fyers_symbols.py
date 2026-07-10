#!/usr/bin/env python3
"""Download, cache, and search the FYERS symbol master files (stdlib only).

The symbol master is the authoritative, daily-refreshed list of every tradable
instrument. The JSON form is keyed by the API symbol ticker and carries the lot
size, tick size, expiry, strike, etc. — so resolving a name to an exact symbol
should be a lookup here, NOT hand-construction (the #1 source of -300 "invalid
symbol" errors).

Masters (one per exchange segment), refreshed every day:
    NSE_CM   NSE Capital Market (equity/indices)
    NSE_FO   NSE Equity Derivatives (futures/options)
    NSE_CD   NSE Currency Derivatives
    NSE_COM  NSE Commodity
    BSE_CM   BSE Capital Market
    BSE_FO   BSE Equity Derivatives
    MCX_COM  MCX Commodity

Each downloads from:
    https://public.fyers.in/sym_details/<MASTER>_sym_master.json

Cached under ~/.fyers/sym_master/<MASTER>.json for the day; re-fetched when the
cached copy's `lastUpdate` is older than today.

Usage:
    python scripts/fyers_symbols.py search NSE_CM SBIN
    python scripts/fyers_symbols.py search NSE_FO BANKNIFTY --opt CE --expiry 2026-06-30
    python scripts/fyers_symbols.py info  NSE_CM NSE:SBIN-EQ
    python scripts/fyers_symbols.py refresh NSE_FO
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import urllib.request

MASTERS = ("NSE_CM", "NSE_FO", "NSE_CD", "NSE_COM", "BSE_CM", "BSE_FO", "MCX_COM")
BASE_URL = "https://public.fyers.in/sym_details/{}_sym_master.json"
CACHE_DIR = os.path.expanduser("~/.fyers/sym_master")


def _cache_path(master: str) -> str:
    return os.path.join(CACHE_DIR, f"{master}.json")


def _is_fresh(path: str) -> bool:
    """True if the cached file's newest `lastUpdate` is today (markets update daily)."""
    if not os.path.exists(path):
        return False
    try:
        with open(path) as f:
            data = json.load(f)
        today = dt.date.today().isoformat()
        return any(v.get("lastUpdate") == today for v in data.values())
    except (ValueError, OSError):
        return False


def load_master(master: str, force: bool = False) -> dict:
    """Return the master dict, downloading/caching as needed."""
    if master not in MASTERS:
        raise ValueError(f"unknown master {master!r}; choose from {', '.join(MASTERS)}")
    path = _cache_path(master)
    if force or not _is_fresh(path):
        url = BASE_URL.format(master)
        req = urllib.request.Request(url, headers={"User-Agent": "fyers-skill/1.0"})
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read().decode())
        os.makedirs(CACHE_DIR, exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f)
        os.replace(tmp, path)
        return data
    with open(path) as f:
        return json.load(f)


def _expiry_iso(epoch: str) -> str:
    """Convert the master's epoch-seconds expiry to YYYY-MM-DD (empty for cash)."""
    if not epoch:
        return ""
    try:
        return dt.datetime.utcfromtimestamp(int(epoch)).date().isoformat()
    except (ValueError, OverflowError):
        return epoch


def search(master: str, term: str, opt: str | None = None,
           expiry: str | None = None, limit: int = 25) -> list[dict]:
    """Case-insensitive substring match over symbol/name; optional opt/expiry filter."""
    data = load_master(master)
    term = term.upper()
    out = []
    for sym, v in data.items():
        hay = f"{sym} {v.get('exSymName', '')} {v.get('symDetails', '')}".upper()
        if term not in hay:
            continue
        if opt and v.get("optType") != opt.upper():
            continue
        if expiry and _expiry_iso(v.get("expiryDate", "")) != expiry:
            continue
        out.append(_summarize(sym, v))
        if len(out) >= limit:
            break
    return out


def _summarize(sym: str, v: dict) -> dict:
    return {
        "symbol": v.get("symTicker", sym),
        "name": v.get("exSymName") or v.get("symDetails"),
        "lotSize": v.get("minLotSize"),
        "tickSize": v.get("tickSize"),
        "optType": v.get("optType"),
        "strike": v.get("strikePrice") if v.get("strikePrice", -1) >= 0 else None,
        "expiry": _expiry_iso(v.get("expiryDate", "")),
        "fyToken": v.get("fyToken"),
    }


def info(master: str, symbol: str) -> dict | None:
    data = load_master(master)
    return data.get(symbol)


# Map an exchange:symbol prefix to the master that should contain it.
_EXCHANGE_MASTERS = {
    "NSE": ("NSE_CM", "NSE_FO", "NSE_CD", "NSE_COM"),
    "BSE": ("BSE_CM", "BSE_FO"),
    "MCX": ("MCX_COM",),
}


def validate_symbol(symbol: str) -> dict:
    """Confirm a symbol exists in the daily master before it's used in an order.

    Returns the master record if found. Raises ValueError if the symbol is not a
    real, tradable instrument — this is the pre-order validation step the skill
    requires (an invalid symbol otherwise fails live with code -300).
    """
    exch = symbol.split(":", 1)[0].upper()
    candidates = _EXCHANGE_MASTERS.get(exch)
    if not candidates:
        raise ValueError(
            f"unknown exchange in {symbol!r}; expected one of NSE:, BSE:, MCX:"
        )
    for master in candidates:
        rec = info(master, symbol)
        if rec is not None:
            return rec
    raise ValueError(
        f"symbol {symbol!r} not found in {exch} master files "
        f"({', '.join(candidates)}); check spelling/expiry with "
        f"`fyers_symbols.py search`"
    )


def _cli() -> int:
    ap = argparse.ArgumentParser(description="Search FYERS symbol master files")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("search", help="substring search for a symbol")
    s.add_argument("master", choices=MASTERS)
    s.add_argument("term")
    s.add_argument("--opt", choices=["CE", "PE", "XX"], help="filter by option type")
    s.add_argument("--expiry", help="filter by expiry YYYY-MM-DD")
    s.add_argument("--limit", type=int, default=25)

    i = sub.add_parser("info", help="dump full record for an exact symbol")
    i.add_argument("master", choices=MASTERS)
    i.add_argument("symbol")

    r = sub.add_parser("refresh", help="force re-download of a master")
    r.add_argument("master", choices=MASTERS)

    args = ap.parse_args()

    if args.cmd == "search":
        rows = search(args.master, args.term, opt=args.opt,
                      expiry=args.expiry, limit=args.limit)
        if not rows:
            print("no matches")
            return 1
        print(json.dumps(rows, indent=2))
    elif args.cmd == "info":
        rec = info(args.master, args.symbol)
        if rec is None:
            print(f"not found: {args.symbol} in {args.master}")
            return 1
        print(json.dumps(rec, indent=2))
    elif args.cmd == "refresh":
        data = load_master(args.master, force=True)
        print(f"refreshed {args.master}: {len(data)} instruments -> {_cache_path(args.master)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
