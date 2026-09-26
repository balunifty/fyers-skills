#!/usr/bin/env python3
"""Query the stored 15-minute / daily candle databases.

The candle stores use one schema across several files, so this helper works
against any of them and prints readable tables instead of bare tuples.

  Auto-detects a database when --db is omitted.

Examples:
    python strategies/utils/query_candles.py summary
    python strategies/utils/query_candles.py symbols --min-price 500
    python strategies/utils/query_candles.py bars NSE:RELIANCE-EQ --limit 20
    python strategies/utils/query_candles.py bars NSE:NIFTY50-INDEX --since 2026-09-24
    python strategies/utils/query_candles.py latest
    python strategies/utils/query_candles.py sessions NSE:TCS-EQ
    python strategies/utils/query_candles.py sql "SELECT COUNT(*) FROM candles"
    python strategies/utils/query_candles.py --db strategies/databases/dashboard_15min_candles.db summary
"""
from __future__ import annotations

import argparse
import datetime as dt
import pathlib
import sqlite3
import sys
from zoneinfo import ZoneInfo

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
DATABASES_DIR = REPO_ROOT / "strategies" / "databases"
MARKET_TIMEZONE = ZoneInfo("Asia/Kolkata")

# Preferred order when --db is not given.
CANDIDATE_DATABASES = (
    "nifty500_15min.db",
    "dashboard_15min_candles.db",
    "NiftyFNOTop100_websocket.db",
)

RESOLUTION_LABELS = {
    "15": "15-minute",
    "5": "5-minute",
    "D": "Daily",
    "1": "1-minute",
}


# =============================================================================
# Output helpers
# =============================================================================
def print_table(headers: list[str], rows: list[tuple], max_width: int = 34) -> None:
    """Print aligned columns, trimming anything too wide to stay readable."""
    if not rows:
        print("  (no rows)")
        return
    cells = [[("" if value is None else str(value))[:max_width] for value in row]
             for row in rows]
    widths = [
        max(len(str(header)), *(len(row[index]) for row in cells))
        for index, header in enumerate(headers)
    ]
    line = "  " + "  ".join(str(h).ljust(w) for h, w in zip(headers, widths))
    print(line)
    print("  " + "  ".join("-" * w for w in widths))
    for row in cells:
        print("  " + "  ".join(value.ljust(w) for value, w in zip(row, widths)))


def ist(epoch: int | None, fmt: str = "%Y-%m-%d %H:%M") -> str:
    if not epoch:
        return "-"
    return dt.datetime.fromtimestamp(epoch, MARKET_TIMEZONE).strftime(fmt)


def count_fmt(value: int | None) -> str:
    return f"{value:,}" if value else "0"


def pct(value: float | None) -> str:
    return "-" if value is None else f"{value:+.2f}%"


# =============================================================================
# Connection
# =============================================================================
def has_candles_table(path: pathlib.Path) -> bool:
    if not path.exists():
        return False
    try:
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as conn:
            names = {
                row[0] for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'")
            }
    except sqlite3.Error:
        return False
    return "candles" in names


def resolve_database(requested: str | None) -> pathlib.Path:
    if requested:
        path = pathlib.Path(requested)
        if not path.exists():
            raise SystemExit(f"ERROR: no such database: {path}")
        if not has_candles_table(path):
            raise SystemExit(
                f"ERROR: {path.name} has no 'candles' table, so it is not a "
                f"candle store. Use summary on one of: "
                f"{', '.join(CANDIDATE_DATABASES)}"
            )
        return path

    for name in CANDIDATE_DATABASES:
        candidate = DATABASES_DIR / name
        if has_candles_table(candidate):
            return candidate

    raise SystemExit(
        "ERROR: no candle database found. Run a scan first, or pass --db <path>."
    )


def open_ro(path: pathlib.Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = None
    return conn


# =============================================================================
# Commands
# =============================================================================
def cmd_summary(conn: sqlite3.Connection, args) -> None:
    print(f"\n  {args.db}")
    print(f"  {args.db.stat().st_size / 1024 / 1024:.2f} MB\n")

    rows = []
    for res, n, syms, lo, hi in conn.execute(
        "SELECT resolution, COUNT(*), COUNT(DISTINCT symbol), MIN(epoch), MAX(epoch) "
        "FROM candles GROUP BY resolution ORDER BY resolution"
    ):
        label = RESOLUTION_LABELS.get(res, f"{res}-minute")
        rows.append((res, label, count_fmt(n), syms, ist(lo), ist(hi)))
    print_table(
        ["res", "interval", "rows", "symbols", "oldest IST", "newest IST"], rows
    )

    total, syms, indices = conn.execute(
        "SELECT COUNT(*), COUNT(DISTINCT symbol), "
        "COUNT(DISTINCT CASE WHEN symbol LIKE '%-INDEX' THEN symbol END) FROM candles"
    ).fetchone()
    print(f"\n  total rows : {count_fmt(total)}")
    print(f"  symbols    : {syms}  ({syms - (indices or 0)} stocks, {indices or 0} indices)")

    # Bar spacing proves the interval actually stored. It must be measured
    # within a single symbol, otherwise every symbol's 09:15 bar collides.
    print("\n  observed bar spacing (measured on one symbol):")
    for res in [r[0] for r in conn.execute(
            "SELECT DISTINCT resolution FROM candles ORDER BY resolution")]:
        sample_symbol = conn.execute(
            "SELECT symbol FROM candles WHERE resolution=? "
            "GROUP BY symbol ORDER BY COUNT(*) DESC LIMIT 1", (res,)
        ).fetchone()
        if not sample_symbol:
            continue
        epochs = [r[0] for r in conn.execute(
            "SELECT epoch FROM candles WHERE symbol=? AND resolution=? "
            "ORDER BY epoch", (sample_symbol[0], res))]
        gaps: dict[int, int] = {}
        for earlier, later in zip(epochs, epochs[1:]):
            minutes = (later - earlier) // 60
            gaps[minutes] = gaps.get(minutes, 0) + 1
        if not gaps:
            continue
        common = max(gaps, key=lambda k: gaps[k])
        others = ", ".join(f"{k}m x{v}" for k, v in sorted(gaps.items())
                           if k != common)
        print(f"    {res:>3} ({RESOLUTION_LABELS.get(res, '')}) on {sample_symbol[0]}: "
              f"{common}m within a session"
              + (f", plus {others}" if others else ""))
    print()


def cmd_symbols(conn: sqlite3.Connection, args) -> None:
    where, params = [], []
    if args.resolution:
        where.append("resolution = ?")
        params.append(args.resolution)
    if args.min_price is not None:
        where.append("close >= ?")
        params.append(args.min_price)
    if args.max_price is not None:
        where.append("close <= ?")
        params.append(args.max_price)
    if args.search:
        where.append("symbol LIKE ?")
        params.append(f"%{args.search.upper()}%")
    clause = ("WHERE " + " AND ".join(where)) if where else ""

    sql = f"""
        SELECT s.symbol,
               MAX(CASE WHEN s.resolution = '15' THEN s.close END) AS close_15,
               MAX(CASE WHEN s.resolution = 'D'  THEN s.close END) AS close_d,
               MAX(CASE WHEN s.resolution = '15' THEN s.epoch END) AS last_15,
               COUNT(*) AS bars
        FROM candles s
        {clause}
        GROUP BY s.symbol
        ORDER BY s.symbol
    """
    rows = []
    for symbol, close_15, close_d, last_15, bars in conn.execute(sql, params):
        if args.resolution and args.resolution == "15":
            price = close_15
        elif args.resolution and args.resolution == "D":
            price = close_d
        else:
            price = close_15 if close_15 is not None else close_d
        if price is not None and args.min_price is not None and price < args.min_price:
            continue
        if price is not None and args.max_price is not None and price > args.max_price:
            continue
        rows.append((symbol, f"{price:,.2f}" if price is not None else "-",
                     bars, ist(last_15, "%Y-%m-%d %H:%M") if last_15 else "-"))
    print(f"\n  {len(rows)} symbols\n")
    print_table(["symbol", "last price", "bars", "latest 15m IST"], rows[:args.limit])
    if len(rows) > args.limit:
        print(f"  ... {len(rows) - args.limit} more (raise --limit to see them)")


def cmd_bars(conn: sqlite3.Connection, args) -> None:
    since = None
    if args.since:
        try:
            since = int(dt.datetime.combine(
                dt.date.fromisoformat(args.since), dt.time(0, 0),
                tzinfo=MARKET_TIMEZONE).timestamp())
        except ValueError:
            raise SystemExit(f"ERROR: --since must be YYYY-MM-DD, got {args.since!r}")

    params = [args.symbol, args.resolution]
    clause = ""
    if since is not None:
        clause = "AND epoch >= ?"
        params.append(since)
    sql = f"""
        SELECT epoch, open, high, low, close, volume
        FROM candles
        WHERE symbol = ? AND resolution = ? {clause}
        ORDER BY epoch DESC
        LIMIT ?
    """
    params.append(args.limit)
    rows = list(conn.execute(sql, params))
    if not rows:
        found = conn.execute(
            "SELECT DISTINCT resolution FROM candles WHERE symbol = ?",
            (args.symbol,)).fetchall()
        if not found:
            print(f"\n  no data for {args.symbol}")
            print(f"  available resolutions: "
                  f"{', '.join(r[0] for r in found) if found else 'symbol not in this store'}")
        else:
            print(f"\n  no {args.resolution} bars for {args.symbol} "
                  f"(has: {', '.join(r[0] for r in found)})")
        return

    print(f"\n  {args.symbol}  {RESOLUTION_LABELS.get(args.resolution, args.resolution)}"
          f"  newest {args.limit}\n")
    table = []
    for epoch, o, h, low, c, v in rows:
        change = ((c - o) / o * 100) if o else 0.0
        table.append((ist(epoch), f"{o:,.2f}", f"{h:,.2f}", f"{low:,.2f}",
                      f"{c:,.2f}", pct(change), count_fmt(int(v))))
    print_table(["time IST", "open", "high", "low", "close", "chg%", "volume"],
                table, max_width=18)
    newest = rows[0]
    print(f"\n  newest bar: O={newest[1]} H={newest[2]} L={newest[3]} "
          f"C={newest[4]} V={count_fmt(int(newest[5]))}")
    print(f"  total {args.resolution}m bars stored: "
          f"{count_fmt(conn.execute('SELECT COUNT(*) FROM candles WHERE symbol=? AND resolution=?', (args.symbol, args.resolution)).fetchone()[0])}\n")


def cmd_latest(conn: sqlite3.Connection, args) -> None:
    rows = []
    for symbol, close, volume, epoch in conn.execute(
        "SELECT symbol, close, volume, MAX(epoch) FROM candles "
        "WHERE resolution = ? GROUP BY symbol ORDER BY symbol", (args.resolution,)
    ):
        rows.append((symbol, f"{close:,.2f}", count_fmt(int(volume)), ist(epoch)))
    print(f"\n  latest {RESOLUTION_LABELS.get(args.resolution, args.resolution)} "
          f"bar for {len(rows)} symbols\n")
    print_table(["symbol", "close", "volume", "bar time IST"], rows[:args.limit])
    if len(rows) > args.limit:
        print(f"  ... {len(rows) - args.limit} more (raise --limit to see them)")


def cmd_sessions(conn: sqlite3.Connection, args) -> None:
    """Per-session bar counts, to spot gaps or partial days."""
    rows = []
    cursor = conn.execute(
        "SELECT epoch FROM candles WHERE symbol = ? AND resolution = ? "
        "ORDER BY epoch", (args.symbol, args.resolution))
    buckets: dict[str, int] = {}
    for (epoch,) in cursor:
        day = ist(epoch, "%Y-%m-%d")
        buckets[day] = buckets.get(day, 0) + 1
    for day, count in sorted(buckets.items(), reverse=True):
        rows.append((day, count, "full" if count >= 25 else "partial"))
    print(f"\n  {args.symbol}  {RESOLUTION_LABELS.get(args.resolution, args.resolution)}"
          f"  by session\n")
    print_table(["session IST", "bars", "coverage"], rows)
    print(f"  a normal 15m session is 25 bars (09:15-15:30)\n")


def cmd_sql(conn: sqlite3.Connection, args) -> None:
    statement = " ".join(args.statement)
    try:
        cursor = conn.execute(statement)
    except sqlite3.Error as error:
        raise SystemExit(f"SQL error: {error}")
    if cursor.description is None:
        print(f"  statement completed, {cursor.rowcount} row(s) affected")
        return
    headers = [d[0] for d in cursor.description]
    rows = [tuple(r) for r in cursor.fetchall()]
    print(f"\n  {len(rows)} row(s)\n")
    print_table(headers, rows)


# =============================================================================
# CLI
# =============================================================================
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Query the stored candle databases",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--db", help=f"database file (default: auto-detect among "
                                     f"{', '.join(CANDIDATE_DATABASES)})")
    parser.add_argument("--limit", type=int, default=50,
                        help="max rows to print (default 50)")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("summary", help="what is stored in this database")

    p_sym = sub.add_parser("symbols", help="list stored symbols with last price")
    p_sym.add_argument("--resolution", default="15", choices=list(RESOLUTION_LABELS))
    p_sym.add_argument("--min-price", type=float)
    p_sym.add_argument("--max-price", type=float)
    p_sym.add_argument("--search", help="substring match on the symbol")
    p_sym.add_argument("--limit", type=int, default=50,
                       help="max rows to print (default 50)")

    p_bars = sub.add_parser("bars", help="candles for one symbol")
    p_bars.add_argument("symbol")
    p_bars.add_argument("--resolution", default="15", choices=list(RESOLUTION_LABELS))
    p_bars.add_argument("--since", help="only bars on/after YYYY-MM-DD")
    p_bars.add_argument("--limit", type=int, default=30,
                        help="max bars (default 30)")

    p_latest = sub.add_parser("latest", help="newest bar for every symbol")
    p_latest.add_argument("--resolution", default="15", choices=list(RESOLUTION_LABELS))
    p_latest.add_argument("--limit", type=int, default=50,
                          help="max rows to print (default 50)")

    p_sess = sub.add_parser("sessions", help="bar count per trading session")
    p_sess.add_argument("symbol")
    p_sess.add_argument("--resolution", default="15", choices=list(RESOLUTION_LABELS))

    p_sql = sub.add_parser("sql", help="run raw SQL (read-only)")
    p_sql.add_argument("statement", nargs="+")
    return parser


COMMANDS = {
    "summary": cmd_summary,
    "symbols": cmd_symbols,
    "bars": cmd_bars,
    "latest": cmd_latest,
    "sessions": cmd_sessions,
    "sql": cmd_sql,
}


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        args.db = resolve_database(args.db)
    except SystemExit as error:
        print(error)
        return 1
    try:
        with open_ro(args.db) as conn:
            COMMANDS[args.command](conn, args)
    except sqlite3.Error as error:
        print(f"SQL error: {error}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
