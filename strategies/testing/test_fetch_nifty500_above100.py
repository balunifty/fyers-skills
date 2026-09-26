import datetime as dt
import importlib.util
import io
import json
import pathlib
import sys
import tempfile
import unittest
from unittest.mock import patch

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
MODULE_PATH = REPO_ROOT / "strategies" / "scripts" / "FetchNifty500Above100.py"
sys.path.insert(0, str(REPO_ROOT / "skills" / "fyers-trading" / "scripts"))
sys.path.insert(0, str(REPO_ROOT / "strategies" / "config"))
SPEC = importlib.util.spec_from_file_location("fetch_nifty500_above100", MODULE_PATH)
assert SPEC and SPEC.loader
fetcher = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = fetcher
SPEC.loader.exec_module(fetcher)

TZ = fetcher.MARKET_TIMEZONE


class FakeLimiter:
    def call(self, function, *args, **kwargs):
        return function(*args, **kwargs)


def make_candles(start_epoch, count):
    return [
        fetcher.Candle(epoch=start_epoch + i * fetcher.CANDLE_SECONDS,
                       open=100.0 + i, high=101.0 + i, low=99.0 + i,
                       close=100.5 + i, volume=1000 + i)
        for i in range(count)
    ]


class UniverseTests(unittest.TestCase):
    def test_chunks_split_evenly_and_with_a_remainder(self):
        names = [f"S{i}" for i in range(125)]
        batches = fetcher.chunks(names, 50)
        self.assertEqual([len(b) for b in batches], [50, 50, 25])
        self.assertEqual([name for batch in batches for name in batch], names)
        self.assertEqual(fetcher.chunks([], 50), [])

    def test_parse_args_defaults(self):
        args = fetcher.parse_args([])
        self.assertEqual(args.min_price, 100.0)
        self.assertEqual(args.days, fetcher.HISTORY_DAYS)
        self.assertEqual(args.resolution if hasattr(args, "resolution") else "15", "15")
        self.assertFalse(args.refresh_universe)

    def test_symbols_file_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "Nifty500.txt"
            with patch.object(fetcher, "STOCKS_PATH", path):
                symbols = ["NSE:AAA-EQ", "NSE:BBB-EQ"]
                fetcher.write_symbols_file(symbols)
                self.assertEqual(fetcher.read_symbols_file(), symbols)
                # comments and blanks are ignored
                path.write_text("# note\n\nNSE:AAA-EQ\nNSE:BBB-EQ\n\n")
                self.assertEqual(fetcher.read_symbols_file(), symbols)

    def test_cached_universe_is_preferred_over_the_network(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "Nifty500.txt"
            path.write_text("NSE:AAA-EQ\n")
            with patch.object(fetcher, "STOCKS_PATH", path):
                with patch.object(fetcher, "fetch_nifty500_symbols") as fetch:
                    symbols, source = fetcher.load_universe(refresh=False)
            fetch.assert_not_called()
            self.assertEqual(symbols, ["NSE:AAA-EQ"])
            self.assertEqual(source, str(path))

    def test_refresh_replaces_the_cached_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "Nifty500.txt"
            path.write_text("NSE:OLD-EQ\n")
            with patch.object(fetcher, "STOCKS_PATH", path):
                with patch.object(
                        fetcher, "fetch_nifty500_symbols",
                        return_value=["NSE:NEW1-EQ", "NSE:NEW2-EQ"]):
                    symbols, _ = fetcher.load_universe(refresh=True)
            self.assertEqual(symbols, ["NSE:NEW1-EQ", "NSE:NEW2-EQ"])
            self.assertEqual(fetcher.read_symbols_file.__wrapped__(path)
                             if hasattr(fetcher.read_symbols_file, "__wrapped__")
                             else path.read_text().split()[-2:],
                             ["NSE:NEW1-EQ", "NSE:NEW2-EQ"])


class PriceFilterTests(unittest.TestCase):
    def test_keeps_only_symbols_strictly_above_the_floor(self):
        symbols = ["NSE:A-EQ", "NSE:B-EQ", "NSE:C-EQ", "NSE:D-EQ"]
        prices = {"NSE:A-EQ": 250.0, "NSE:B-EQ": 100.01,
                  "NSE:C-EQ": 100.0, "NSE:D-EQ": 42.5}
        kept, rejected = fetcher.filter_by_price(symbols, prices, 100.0)

        self.assertEqual(kept, ["NSE:A-EQ", "NSE:B-EQ"])
        self.assertEqual(dict(rejected), {"NSE:C-EQ": 100.0, "NSE:D-EQ": 42.5})

    def test_exactly_at_the_floor_is_rejected(self):
        kept, rejected = fetcher.filter_by_price(
            ["NSE:X-EQ"], {"NSE:X-EQ": 100.0}, 100.0)
        self.assertEqual(kept, [])
        self.assertEqual(len(rejected), 1)

    def test_unpriced_symbols_are_skipped_not_counted_as_rejected(self):
        kept, rejected = fetcher.filter_by_price(
            ["NSE:A-EQ", "NSE:NOPRICE-EQ"], {"NSE:A-EQ": 500.0}, 100.0)
        self.assertEqual(kept, ["NSE:A-EQ"])
        self.assertEqual(rejected, [])

    def test_custom_floor_is_honoured(self):
        kept, rejected = fetcher.filter_by_price(
            ["NSE:A-EQ", "NSE:B-EQ"],
            {"NSE:A-EQ": 150.0, "NSE:B-EQ": 250.0}, 200.0)
        self.assertEqual(kept, ["NSE:B-EQ"])
        self.assertEqual(dict(rejected), {"NSE:A-EQ": 150.0})

    def test_quotes_are_read_in_batches_and_gathered(self):
        batches = []

        class Client:
            def quotes(self, symbols):
                batches.append(list(symbols))
                # Real FYERS shape: symbol under "n", "s" is the item status.
                return {"s": "ok", "d": [
                    {"n": s, "s": "ok", "v": {"lp": 300.0, "symbol": s}}
                    for s in symbols
                ] + [{"n": "NSE:BAD-EQ", "s": "ok", "v": {"lp": 0}}]}

        symbols = [f"NSE:S{i}-EQ" for i in range(120)]
        prices = fetcher.fetch_prices(Client(), FakeLimiter(), symbols)

        self.assertEqual([len(b) for b in batches], [50, 50, 20])
        self.assertEqual(len(prices), 120)
        self.assertEqual(prices["NSE:S0-EQ"], 300.0)
        # the symbol must not be read from "s", which is the item status
        self.assertNotIn("ok", prices)
        self.assertNotIn("NSE:BAD-EQ", prices)

    def test_a_response_using_status_as_symbol_would_not_be_mistaken(self):
        """Regression guard for the real API shape."""
        class Client:
            def quotes(self, symbols):
                return {"s": "ok", "d": [
                    {"n": s, "s": "ok", "v": {"lp": 150.0}} for s in symbols]}

        prices = fetcher.fetch_prices(
            Client(), FakeLimiter(), ["NSE:A-EQ", "NSE:B-EQ"])
        self.assertEqual(prices, {"NSE:A-EQ": 150.0, "NSE:B-EQ": 150.0})

    def test_a_failed_quote_batch_is_logged_not_fatal(self):
        class Client:
            def quotes(self, symbols):
                raise ValueError("quotes: max 50 symbols per request")

        prices = fetcher.fetch_prices(Client(), FakeLimiter(),
                                      ["NSE:A-EQ", "NSE:B-EQ"])
        self.assertEqual(prices, {})


class CandleFetchTests(unittest.TestCase):
    def _history(self, payload):
        class Client:
            def history(self, symbol, resolution, start, end):
                return payload
        return Client()

    def test_incomplete_bars_are_excluded(self):
        now = dt.datetime(2026, 1, 2, 10, 0, tzinfo=TZ)
        base = int(dt.datetime(2026, 1, 2, 9, 15, tzinfo=TZ).timestamp())
        payload = {"s": "ok", "candles": [
            [base, 100, 101, 99, 100, 10],                 # closes 09:30, complete
            [base + 900, 100, 101, 99, 100, 10],          # closes 09:45, complete
            [base + 1800, 100, 105, 99, 104, 10],         # closes 10:00, just done
        ]}
        candles = fetcher.fetch_candles(
            self._history(payload), FakeLimiter(), "NSE:A-EQ", 5, now)
        self.assertEqual(len(candles), 3)

        now2 = dt.datetime(2026, 1, 2, 9, 59, tzinfo=TZ)
        candles2 = fetcher.fetch_candles(
            self._history(payload), FakeLimiter(), "NSE:A-EQ", 5, now2)
        self.assertEqual(len(candles2), 2, "the 09:45 bar is still forming at 09:59")

    def test_rejects_a_failed_response(self):
        now = dt.datetime(2026, 1, 2, 10, 0, tzinfo=TZ)
        with self.assertRaises(RuntimeError):
            fetcher.fetch_candles(
                self._history({"s": "error", "candles": []}),
                FakeLimiter(), "NSE:A-EQ", 5, now)

    def test_retries_on_http_429(self):
        attempts = {"n": 0}
        now = dt.datetime(2026, 1, 2, 10, 0, tzinfo=TZ)

        class Client:
            def history(self, symbol, resolution, start, end):
                attempts["n"] += 1
                if attempts["n"] < 3:
                    raise RuntimeError("HTTP 429 on GET")
                return {"s": "ok", "candles": [[1, 1, 1, 1, 1, 1]]}

        with patch.object(fetcher.time, "sleep"):
            candles = fetcher.fetch_candles(
                Client(), FakeLimiter(), "NSE:A-EQ", 5, now)
        self.assertEqual(attempts["n"], 3)
        self.assertEqual(len(candles), 1)

    def test_gives_up_after_the_retry_budget(self):
        now = dt.datetime(2026, 1, 2, 10, 0, tzinfo=TZ)

        class Client:
            def history(self, *args, **kwargs):
                raise RuntimeError("HTTP 429")

        with patch.object(fetcher.time, "sleep"):
            with self.assertRaises(RuntimeError):
                fetcher.fetch_candles(
                    Client(), FakeLimiter(), "NSE:A-EQ", 5, now)

    def test_malformed_rows_are_skipped(self):
        parsed = fetcher.parse_candles({"candles": [
            [1, 2, 3, 4, 5, 6],
            [1, 2],
            "not-a-row",
            [2, "x", 3, 4, 5, 6],
        ]})
        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0].close, 5.0)

    def test_candles_come_back_sorted(self):
        parsed = fetcher.parse_candles({"candles": [
            [3000, 1, 1, 1, 1, 1],
            [1000, 1, 1, 1, 1, 1],
            [2000, 1, 1, 1, 1, 1],
        ]})
        self.assertEqual([c.epoch for c in parsed], [1000, 2000, 3000])


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = pathlib.Path(self.tmp.name) / "n500.db"
        self.store = fetcher.CandleStore(self.path)
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.store.close)

    def test_schema_is_created_and_rows_written(self):
        self.assertEqual(self.store.store("NSE:A-EQ", make_candles(1000, 3)), 3)
        summary = self.store.summary()
        self.assertEqual(summary["total_rows"], 3)
        self.assertEqual(summary["symbols"], 1)
        self.assertTrue(summary["newest_ist"])

    def test_rescanning_merges_without_duplicating(self):
        self.store.store("NSE:A-EQ", make_candles(1000, 5))
        self.store.store("NSE:A-EQ", make_candles(1000, 5))
        self.assertEqual(self.store.summary()["total_rows"], 5)

        # overlapping two bars plus two new ones
        self.store.store("NSE:A-EQ", make_candles(1000 + 3 * fetcher.CANDLE_SECONDS, 4))
        self.assertEqual(self.store.summary()["total_rows"], 7)

    def test_rows_are_tagged_with_the_15m_resolution(self):
        self.store.store("NSE:A-EQ", make_candles(1000, 2))
        with self.store.connect() as conn:
            resolution = conn.execute(
                "SELECT DISTINCT resolution FROM candles").fetchall()
        self.assertEqual(resolution, [("15",)])

    def test_empty_candles_write_nothing(self):
        self.assertEqual(self.store.store("NSE:A-EQ", []), 0)
        self.assertEqual(self.store.summary()["total_rows"], 0)


class MainTests(unittest.TestCase):
    def test_a_full_run_keeps_history_calls_for_filtered_symbols_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = pathlib.Path(tmp) / "out.db"
            symbols = [f"NSE:S{i}-EQ" for i in range(120)]
            prices = {}
            for index, symbol in enumerate(symbols):
                # every third symbol is at or below the floor
                prices[symbol] = 50.0 if index % 3 == 0 else 400.0

            class Client:
                def __init__(self, *args, **kwargs):
                    pass

                def quotes(self, batch):
                    return {"s": "ok", "d": [
                        {"n": s, "s": "ok", "v": {"lp": prices[s]}} for s in batch]}

                def history(self, symbol, resolution, start, end):
                    base = int(dt.datetime(2026, 1, 2, 9, 15, tzinfo=TZ).timestamp())
                    return {"s": "ok", "candles": [
                        [base + i * 900, 100, 101, 99, 100, 10] for i in range(4)]}

            report = pathlib.Path(tmp) / "report.json"
            with (
                patch.object(fetcher, "load_universe",
                             return_value=(symbols, "test")),
                patch.object(fetcher, "FyersClient", Client),
                patch.object(fetcher, "LOG_PATH",
                             pathlib.Path(tmp) / "s.log"),
            ):
                code = fetcher.main([
                    "--db", str(db), "--min-price", "100", "--report", str(report)])

            self.assertEqual(code, 0)
            data = json.loads(report.read_text())
            self.assertEqual(data["universe_size"], 120)
            self.assertEqual(data["symbols_rejected"], 40)
            self.assertEqual(data["symbols_kept"], 80)
            self.assertEqual(data["candle_minutes"], 15)
            self.assertEqual(data["bars_written"], 80 * 4)
            # only the qualifying symbols were written
            self.assertEqual(data["store"]["symbols"], 80)

    def test_rejects_a_nonsense_day_count(self):
        self.assertEqual(fetcher.main(["--days", "0"]), 2)

    def test_market_open_reflects_the_clock(self):
        def at(*args):
            return dt.datetime(*args, tzinfo=TZ)

        self.assertTrue(fetcher.market_open(at(2026, 1, 2, 10, 0)))   # Friday
        self.assertFalse(fetcher.market_open(at(2026, 1, 2, 8, 0)))  # before open
        self.assertFalse(fetcher.market_open(at(2026, 1, 2, 16, 0)))  # after close
        self.assertFalse(fetcher.market_open(at(2026, 1, 3, 10, 0)))  # Saturday
        self.assertTrue(fetcher.market_open(at(2026, 1, 5, 9, 15)))   # boundary


if __name__ == "__main__":
    unittest.main()
