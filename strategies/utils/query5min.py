#!/usr/bin/env python3
"""Query the 5-minute database."""
import sqlite3

DB = "strategies/databases/nifty50_5min.db"

with sqlite3.connect(DB) as conn:
    tables = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    print(f"Tables: {len(tables)}")
    for t in tables:
        name = t[0]
        count = conn.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
        latest = conn.execute(f"SELECT candle_time, close, ema10, ema20, ema30, rsi14 FROM {name} ORDER BY epoch DESC LIMIT 1").fetchone()
        print(f"\n{name}: {count} rows")
        if latest:
            print(f"  Latest: time={latest[0]} close={latest[1]} ema10={latest[2]} ema20={latest[3]} ema30={latest[4]} rsi={latest[5]}")
