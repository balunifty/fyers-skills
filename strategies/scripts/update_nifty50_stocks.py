#!/usr/bin/env python3
"""Fetch the official NIFTY 50 constituents and update the agent stock list."""
from __future__ import annotations

import csv
import io
import pathlib
import tempfile
import urllib.request

SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
STRATEGIES_DIR = SCRIPT_DIR.parent
STOCKS_PATH = STRATEGIES_DIR / "data" / "Nifty50.txt"
NIFTY50_CSV_URL = "https://www.niftyindices.com/IndexConstituent/ind_nifty50list.csv"


def fetch_nifty50_symbols() -> list[str]:
    request = urllib.request.Request(
        NIFTY50_CSV_URL,
        headers={"User-Agent": "Mozilla/5.0 (compatible; FyersMarketDataAgent/1.0)"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        content = response.read().decode("utf-8-sig")
    rows = csv.DictReader(io.StringIO(content))
    if not rows.fieldnames or "Symbol" not in rows.fieldnames:
        raise ValueError(f"NIFTY 50 CSV has no Symbol column; columns={rows.fieldnames}")
    symbols = []
    for row in rows:
        ticker = (row.get("Symbol") or "").strip().upper()
        if ticker and ticker not in symbols:
            symbols.append(ticker)
    if len(symbols) != 50:
        raise ValueError(f"expected 50 NIFTY 50 symbols, received {len(symbols)}")
    return [f"NSE:{ticker}-EQ" for ticker in symbols]


def update_stocks_file(symbols: list[str]) -> None:
    content = "# Official NIFTY 50 constituents; refreshed by update_nifty50_stocks.py\n"
    content += "\n".join(symbols) + "\n"
    STOCKS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=STOCKS_PATH.parent,
                                     delete=False) as temporary:
        temporary.write(content)
        temporary_path = pathlib.Path(temporary.name)
    temporary_path.replace(STOCKS_PATH)


def main() -> int:
    try:
        symbols = fetch_nifty50_symbols()
        update_stocks_file(symbols)
    except (OSError, ValueError) as error:
        print(f"ERROR: {error}")
        return 1
    print(f"Updated {STOCKS_PATH} with {len(symbols)} NIFTY 50 symbols")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())