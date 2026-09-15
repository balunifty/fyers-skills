#!/usr/bin/env python3
"""Quick helper to query nifty50.db from the command line."""
import sqlite3, sys

DB_PATH = r"strategies\databases\nifty50.db"

def main():
    if len(sys.argv) < 2:
        # Default: list tables
        sql = "SELECT name FROM sqlite_master WHERE type='table'"
    else:
        sql = " ".join(sys.argv[1:])
    with sqlite3.connect(DB_PATH) as conn:
        for row in conn.execute(sql):
            print(row)

if __name__ == "__main__":
    main()
