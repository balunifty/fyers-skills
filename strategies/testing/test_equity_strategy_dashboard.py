import datetime as dt
import importlib.util
import json
import pathlib
import re
import shutil
import socket
import subprocess
import tempfile
import time
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
MODULE_PATH = REPO_ROOT / "strategies" / "ui" / "equity_strategy_dashboard.py"
SPEC = importlib.util.spec_from_file_location("equity_strategy_dashboard", MODULE_PATH)
assert SPEC and SPEC.loader
dashboard = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = dashboard
SPEC.loader.exec_module(dashboard)

IST = dashboard.MARKET_TIMEZONE


class FakeLimiter:
    def call(self, function, *args, **kwargs):
        return function(*args, **kwargs)


class FakeClient:
    def __init__(self, *args, **kwargs):
        pass


class FakeHistoryClient:
    """Returns a completed 15m bar, a 5m bar, and a prior daily bar."""

    def __init__(self, *args, **kwargs):
        pass

    def history(self, symbol, resolution, start, end):
        now = dt.datetime(2026, 1, 2, 10, 0, tzinfo=IST)
        if symbol.startswith("NSE:A"):
            volume = 20_000_000
        elif symbol.startswith("NSE:B"):
            volume = 10_000_000
        else:
            volume = 5_000_000
        if resolution == "D":
            day = dt.datetime(2026, 1, 1, tzinfo=IST)
            return {"s": "ok", "candles": [
                [int(day.timestamp()), 90, 95, 89, 94, volume],
            ]}
        seconds = dashboard.FIVE_MINUTE_SECONDS if resolution == "5" else dashboard.CANDLE_SECONDS
        epoch = int(now.timestamp()) - seconds
        return {"s": "ok", "candles": [[epoch, 100, 101, 99, 100, volume]]}


class FakeRegistry:
    def __init__(self, module):
        self.module = module
        self.spec = dashboard.StrategySpec(
            key="fake",
            label="Fake strategy",
            column="Fake",
            module_name="fake",
            module_path=pathlib.Path("fake.py"),
            evaluator_name="evaluate",
            kind="ema",
        )

    def selected_specs(self, selection):
        return (self.spec,)

    def module_for(self, spec):
        return self.module

    def evaluator_for(self, spec):
        return lambda candles: (True, {"curr_close": 42.5})

    def labels(self):
        return [{"key": "fake", "column": "Fake", "label": "Fake strategy"}]


def run_scan(symbols):
    """Scan against fakes, keeping the real candle store untouched."""
    scanner = dashboard.DashboardScanner(FakeRegistry(object()))
    current = dt.datetime(2026, 1, 2, 10, 0, tzinfo=IST)
    with tempfile.TemporaryDirectory() as tmp:
        store_path = pathlib.Path(tmp) / "candles.db"
        with (
            patch.object(dashboard, "read_symbols", return_value=symbols),
            patch.object(dashboard, "FyersClient", FakeHistoryClient),
            patch.object(dashboard, "now_ist", return_value=current),
            patch.object(dashboard, "CANDLE_DB_PATH", store_path),
        ):
            return scanner.scan("fake")


class TwoViewTests(unittest.TestCase):
    def test_scan_returns_all_four_tabs(self):
        result = run_scan(["NSE:A-EQ", "NSE:B-EQ"])

        self.assertEqual(
            set(result["views"]),
            {"intraday", "eod", "rejections", "indices"})
        self.assertEqual(result["columns"], [
            {"key": "fake", "column": "Fake", "label": "Fake strategy"}
        ])
        for view_id in ("intraday", "eod"):
            view = result["views"][view_id]
            self.assertEqual(view["id"], view_id)
            self.assertTrue(view["title"])
            self.assertFalse(view["sell_only"])
            self.assertEqual(len(view["rows"]), 2)
            for row in view["rows"]:
                self.assertEqual(list(row["cells"]), ["fake"])

        # This fake universe declares no index-only strategy, so the indices
        # tab exists but is empty. IndexViewTests covers it properly.
        indices = result["views"]["indices"]
        self.assertEqual(indices["id"], "indices")
        self.assertEqual(indices["rows"], [])
        self.assertTrue(indices["single_group"])

    def test_intraday_tab_uses_the_latest_15m_bar(self):
        row = run_scan(["NSE:A-EQ"])["views"]["intraday"]["rows"][0]

        self.assertEqual(row["close"], 100.0)
        self.assertEqual(row["volume_15m"], 20_000_000)
        self.assertEqual(row["total_gain_percent"], 0.0)
        # 09:45 bar closes at 10:00.
        self.assertEqual(row["cells"]["fake"]["time_ist"], "10:00")

    def test_eod_tab_uses_the_last_completed_daily_bar(self):
        view = run_scan(["NSE:A-EQ"])["views"]["eod"]
        row = view["rows"][0]

        # Price/volume come from the daily bar, not the 15-minute one.
        self.assertEqual(row["close"], 94.0)
        self.assertEqual(row["volume_15m"], 20_000_000)
        self.assertNotEqual(row["total_gain_percent"], 0.0)
        # A daily signal resolves at the 15:30 session close.
        self.assertEqual(row["cells"]["fake"]["time_ist"], "15:30")

    def test_eod_tab_is_pinned_to_the_last_completed_session(self):
        current = dt.datetime(2026, 1, 2, 10, 0, tzinfo=IST)  # mid-session Friday
        view = run_scan(["NSE:A-EQ"])["views"]["eod"]

        self.assertFalse(view["market_open"])
        self.assertEqual(view["kind"], "eod")
        self.assertEqual(view["as_of_label"], "Thu 01 Jan")
        self.assertIn("last completed session", view["latest_message"])
        self.assertTrue(view["note"])

    def test_intraday_tab_reports_market_state(self):
        view = run_scan(["NSE:A-EQ"])["views"]["intraday"]

        self.assertTrue(view["latest_15min"])
        self.assertIn("Data is live", view["latest_message"])
        self.assertEqual(view["as_of_label"], "Fri 02 Jan 10:00")

    def test_rows_ranked_by_signals_in_all_tabs(self):
        result = run_scan(["NSE:A-EQ", "NSE:B-EQ", "NSE:C-EQ"])
        for view_id in ("intraday", "eod", "rejections"):
            rows = result["views"][view_id]["rows"]
            self.assertEqual([r["symbol"] for r in rows],
                             ["NSE:A-EQ", "NSE:B-EQ", "NSE:C-EQ"])
            self.assertEqual(rows[0]["volume_15m"], 20_000_000)
            self.assertEqual(rows[-1]["volume_15m"], 5_000_000)

        # The fake strategy is a buy, not a rejection, so the sell-only tab has
        # nothing to show from it.
        self.assertEqual(result["views"]["intraday"]["signal_rows"], 3)
        self.assertEqual(result["views"]["eod"]["signal_rows"], 3)
        self.assertEqual(result["views"]["rejections"]["signal_rows"], 0)


class SellOnlyViewTests(unittest.TestCase):
    """The Rejections tab lists only sell columns and hides every buy cell."""

    def test_rejection_columns_are_flagged(self):
        flagged = {s.key for s in dashboard.STRATEGY_SPECS if s.sell_only}
        # Spelled out rather than counted, so a new sell column is deliberate.
        self.assertEqual(flagged, {
            "orb_high_rejection", "r1_rejection", "prev_high_rejection",
            "doji_rejection", "higher_high_rejection",
            "higher_high_close_rejection", "open_ema_stack",
            "lower_high_close",
        })

    def test_rejections_view_shows_only_the_sell_columns(self):
        result = run_scan(["NSE:A-EQ"])
        view = result["views"]["rejections"]

        self.assertTrue(view["sell_only"])
        self.assertEqual([c["key"] for c in view["columns"]], [])
        self.assertEqual(len(result["columns"]), 1)  # the fake single spec
        # the wide tabs still carry every column
        self.assertEqual(
            len(result["views"]["intraday"]["columns"]), 1)

    def test_rejections_view_never_shows_a_buy_cell(self):
        result = run_scan(["NSE:A-EQ", "NSE:B-EQ"])
        view = result["views"]["rejections"]

        for row in view["rows"]:
            for cell in row["cells"].values():
                self.assertIn(cell["state"], ("-", "SELL"))
        # signal_rows is recomputed from the sells actually shown
        self.assertEqual(view["signal_rows"], 0)

    def test_rejections_view_drops_non_rejection_columns(self):
        rows = [{
            "symbol": "NSE:A-EQ", "group": "stock", "close": 1.0,
            "total_gain_percent": 0.0, "volume_15m": 1, "signal_count": 2,
            "cells": {
                "r1_rejection": {"state": "SELL", "time_ist": "10:00", "note": "", "price": 1.0},
                "ema_fresh": {"state": "BUY", "time_ist": "10:00", "note": "", "price": 1.0},
            },
        }]
        columns = [
            {"key": "r1_rejection", "column": "R1", "label": "r1"},
            {"key": "ema_fresh", "column": "EMA", "label": "ema"},
        ]
        view = dashboard.build_view(
            "rejections", "Rejections", rows,
            {"message": "m", "market_open": False, "as_of_ist": None,
             "as_of_label": "x", "note": "", "kind": "closed"},
            True, 1, 1, columns, sell_only=True,
            column_keys={"r1_rejection"},
        )

        self.assertEqual([c["key"] for c in view["columns"]], ["r1_rejection"])
        cells = view["rows"][0]["cells"]
        self.assertEqual(cells["r1_rejection"]["state"], "SELL")
        self.assertNotIn("ema_fresh", cells)
        self.assertEqual(view["signal_rows"], 1)

    def test_sell_only_view_keeps_price_and_volume(self):
        result = run_scan(["NSE:A-EQ"])
        row = result["views"]["rejections"]["rows"][0]
        self.assertIn("close", row)
        self.assertIn("volume_15m", row)


class MarketStateTests(unittest.TestCase):
    def _freshness(self, iso, latest=True):
        return {"NSE:X-EQ": {
            "latest": latest, "latest_candle_ist": iso, "age_seconds": 0,
            "message": "x",
        }}

    def test_weekend_reports_a_closed_market_not_stale_data(self):
        saturday = dt.datetime(2026, 1, 3, 11, 0, tzinfo=IST)
        state = dashboard.describe_data_state(
            self._freshness("2026-01-02T15:30:00+05:30"), False, saturday
        )

        self.assertFalse(state["market_open"])
        self.assertEqual(state["kind"], "closed")
        self.assertIn("Market closed", state["message"])
        self.assertIn("Fri 02 Jan", state["message"])
        self.assertIn("not live ticks", state["note"])

    def test_market_is_open_only_inside_the_cash_session(self):
        self.assertTrue(dashboard.market_is_open(dt.datetime(2026, 1, 2, 10, 0, tzinfo=IST)))
        self.assertFalse(dashboard.market_is_open(dt.datetime(2026, 1, 2, 9, 0, tzinfo=IST)))
        self.assertFalse(dashboard.market_is_open(dt.datetime(2026, 1, 2, 16, 0, tzinfo=IST)))
        self.assertFalse(dashboard.market_is_open(dt.datetime(2026, 1, 3, 10, 0, tzinfo=IST)))

    def test_open_market_with_lagging_data_is_reported_as_stale(self):
        friday = dt.datetime(2026, 1, 2, 10, 0, tzinfo=IST)
        state = dashboard.describe_data_state(
            self._freshness("2026-01-02T09:15:00+05:30", latest=False), False, friday
        )

        self.assertTrue(state["market_open"])
        self.assertEqual(state["kind"], "stale")
        self.assertIn("not latest", state["message"])

    def test_eod_state_reports_the_latest_daily_bar(self):
        daily = {
            "NSE:A-EQ": {"bar_date": "2026-01-01", "latest_candle_ist": "x"},
            "NSE:B-EQ": {"bar_date": "2026-01-02", "latest_candle_ist": "x"},
        }
        state = dashboard.describe_eod_state(
            daily, dt.datetime(2026, 1, 2, 10, 0, tzinfo=IST),
            intraday_only=("ORB", "Second Candle"),
        )

        self.assertEqual(state["as_of_label"], "Fri 02 Jan")
        self.assertEqual(state["kind"], "eod")
        # They are dropped from the tab, not left as blank columns.
        self.assertIn("are not columns here: ORB, Second Candle.", state["note"])
        self.assertNotIn("stay blank here", state["note"])
        # with nothing flagged, no trailing clause is added
        plain = dashboard.describe_eod_state(
            daily, dt.datetime(2026, 1, 2, 10, 0, tzinfo=IST)
        )
        self.assertNotIn("are not columns here", plain["note"])

    def test_daily_freshness_uses_the_session_open_as_the_bar_stamp(self):
        day = dt.datetime(2026, 1, 1, 18, 30, tzinfo=IST)  # late IST still 1 Jan
        item = dashboard.daily_freshness_item(
            [SimpleNamespace(epoch=int(day.timestamp()), volume=1)]
        )

        self.assertEqual(item["bar_date"], "2026-01-01")
        self.assertTrue(item["latest_candle_ist"].startswith("2026-01-01T09:15"))


class EodTabTests(unittest.TestCase):
    """The EOD tab is judged on daily bars, and says so by its columns.

    It used to run every strategy against the daily series and show every
    column. The six session-shaped strategies - the three ORB columns among them
    - find no 09:15 or 5-minute bar in a daily series and reported nothing, so
    the tab carried six permanently blank columns. They are now left out.
    """

    INTRADAY_ONLY_KEYS = {
        "orb", "orb_high_rejection", "orb_low_rejection",
        "open_ema_stack", "second_candle", "index_rejection",
    }

    def test_eod_specs_drops_the_session_shaped_strategies(self):
        kept = {spec.key for spec in dashboard.eod_specs(dashboard.STRATEGY_SPECS)}
        self.assertEqual(kept & self.INTRADAY_ONLY_KEYS, set())

    def test_eod_specs_keeps_the_daily_and_bar_agnostic_strategies(self):
        """A daily breakout, and a rule that only compares to the prior bar,
        are both perfectly readable on a daily bar."""
        kept = {spec.key for spec in dashboard.eod_specs(dashboard.STRATEGY_SPECS)}
        for key in ("daily_breakout", "r1_rejection", "prev_high_rejection",
                    "doji_rejection", "lower_high_close", "ema_10_cross_20",
                    "ema_fresh", "ema_pullback", "double_bottom"):
            self.assertIn(key, kept, key)

    def test_the_three_orb_columns_are_marked_intraday_only(self):
        for key in ("orb", "orb_high_rejection", "orb_low_rejection"):
            spec = dashboard.STRATEGY_BY_KEY[key]
            self.assertTrue(spec.intraday_only, key)

    def test_eod_specs_preserves_the_original_order(self):
        """Column order is the registry's; filtering must not reshuffle it."""
        all_keys = [spec.key for spec in dashboard.STRATEGY_SPECS]
        kept = [spec.key for spec in dashboard.eod_specs(dashboard.STRATEGY_SPECS)]
        self.assertEqual(kept, [k for k in all_keys if k in set(kept)])

    def test_the_intraday_tabs_keep_every_orb_column(self):
        """The exclusion is for the EOD tab only."""
        for key in ("orb", "orb_high_rejection", "orb_low_rejection"):
            spec = dashboard.STRATEGY_BY_KEY[key]
            self.assertFalse(spec.hide_in_intraday, key)

    def test_the_scan_only_evaluates_daily_strategies_for_the_eod_tab(self):
        """The narrowing has to happen at evaluation, not just in the headers.

        Filtering the columns alone would leave the ORB evaluators running on
        the daily series and then discarding their cells, which is the waste and
        the source of the misleading readings this change removes.
        """
        source = Path(MODULE_PATH).read_text(encoding="utf-8")
        self.assertIn("eod_applicable = eod_specs(applicable)", source)
        self.assertIn("column_keys=eod_keys", source)
        # The EOD cell build must not be handed the intraday list.
        eod_call = source[
            source.index("eod_cells = self._build_cells("):
            source.index("eod_rows.append(")]
        first_argument = re.search(r"_build_cells\(\s*(\w+),", eod_call)
        self.assertIsNotNone(first_argument, eod_call)
        # The bare name would also match as a substring of eod_applicable, so
        # the first argument itself is what has to be checked.
        self.assertEqual(first_argument.group(1), "eod_applicable")


    def test_the_eod_column_set_is_derived_from_the_same_helper(self):
        """The headers and the evaluation must not be able to disagree.

        eod_keys used to repeat the intraday_only test inline, so it could drift
        from eod_specs and drop a column's header while still evaluating it (or
        the reverse). It is now derived from the helper, and this pins that.
        """
        source = Path(MODULE_PATH).read_text(encoding="utf-8")
        self.assertIn("eod_keys = {spec.key for spec in eod_specs(selected_specs)}",
                      source)
        self.assertNotIn(
            "spec.key for spec in selected_specs if not spec.intraday_only",
            source,
            "eod_keys must not restate the filter instead of calling eod_specs",
        )

    def test_the_dropped_columns_could_never_have_fired_on_daily_bars(self):
        """Why they are dropped rather than fixed.

        Driven on a real daily series: a session-shaped rule finds no 09:15 or
        5-minute bar, so it reports nothing. That is why these were blank
        columns rather than wrong ones, and why dropping them loses no signal.
        """
        scanner = dashboard.DashboardScanner()
        intraday_only = [s for s in dashboard.STRATEGY_SPECS if s.intraday_only]
        self.assertTrue(intraday_only)
        loaded, modules, errs = scanner._resolve_specs(intraday_only)
        self.assertEqual(errs, [])

        # A rising daily series, long enough for any indicator to warm up.
        day = dt.datetime(2026, 1, 1, tzinfo=IST)
        bars = [
            SimpleNamespace(
                epoch=int((day + dt.timedelta(days=i)).timestamp()),
                open=100.0 + i, high=101.0 + i, low=99.0 + i,
                close=100.5 + i, volume=1_000,
            )
            for i in range(40)
        ]
        fired = 0
        for spec in loaded:
            outcomes = scanner._evaluate(
                spec, modules[spec.key], "NSE:T-EQ",
                bars, bars, bars, dt.datetime(2026, 2, 10, 15, 0, tzinfo=IST))
            fired += len([o for o in outcomes if o[0] in ("CE", "PE", "BUY", "SELL")])
        self.assertEqual(
            fired, 0,
            "an intraday-only strategy reported a signal from daily bars, so it "
            "is not safely excludable and the reasoning above is wrong",
        )


class SignalMappingTests(unittest.TestCase):
    def test_option_signals_map_to_buy_and_sell(self):
        self.assertEqual(dashboard.signal_state("CE"), "BUY")
        self.assertEqual(dashboard.signal_state("PE"), "SELL")
        self.assertEqual(dashboard.signal_state("NONE"), "-")

    def test_crossover_time_is_the_signal_bar_close_time(self):
        spec = dashboard.StrategySpec(
            key="orb", label="ORB", column="ORB",
            module_name="m", module_path=pathlib.Path("m.py"),
            evaluator_name="e", kind="orb",
            bar_seconds=dashboard.FIVE_MINUTE_SECONDS,
        )
        # ORB reports the 5m bar open time; the crossover closes 5 minutes later.
        details = {"curr_time": "2026-01-02T09:20:00+05:30"}
        self.assertEqual(
            dashboard.bar_close_time_ist(details, [], spec.bar_seconds), "09:25"
        )

    def test_crossover_time_falls_back_to_the_latest_bar(self):
        current = dt.datetime(2026, 1, 2, 9, 45, tzinfo=IST)
        candles = [SimpleNamespace(epoch=int(current.timestamp()))]
        self.assertEqual(
            dashboard.bar_close_time_ist({}, candles, dashboard.CANDLE_SECONDS), "10:00"
        )

    def test_session_close_mode_uses_the_daily_close_time(self):
        self.assertEqual(
            dashboard.bar_close_time_ist({}, [], 900, session_close=True), "15:30"
        )

    def test_a_buy_only_evaluator_produces_buy(self):
        spec = dashboard.StrategySpec(
            key="ema", label="EMA", column="EMA",
            module_name="m", module_path=pathlib.Path("m.py"),
            evaluator_name="e", kind="ema",
        )
        current = dt.datetime(2026, 1, 2, 10, 0, tzinfo=IST)
        candles = [SimpleNamespace(epoch=int(current.timestamp()) - dashboard.CANDLE_SECONDS)]
        scanner = dashboard.DashboardScanner(FakeRegistry(object()))

        outcomes = scanner._evaluate(spec, None, "NSE:A-EQ", candles, [], [], current)
        self.assertEqual(dashboard.build_cell(spec, outcomes, candles)["state"], "BUY")

    def test_a_broken_evaluator_degrades_to_an_error_cell(self):
        spec = dashboard.STRATEGY_BY_KEY["second_candle"]
        current = dt.datetime(2026, 1, 2, 10, 0, tzinfo=IST)
        scanner = dashboard.DashboardScanner(FakeRegistry(object()))
        cells = scanner._build_cells(
            [spec], {"second_candle": None}, "NSE:A-EQ", [], [], [], current
        )
        self.assertEqual(cells["second_candle"]["state"], "error")


class RetryTests(unittest.TestCase):
    def test_history_retries_when_fyers_throttles(self):
        attempts = {"n": 0}

        class ThrottlingClient:
            def history(self, symbol, resolution, start, end):
                attempts["n"] += 1
                if attempts["n"] < 3:
                    raise RuntimeError("HTTP 429 on GET https://api-t1.fyers.in")
                return {"s": "ok", "candles": [[1, 1, 1, 1, 1, 1]]}

        current = dt.datetime(2026, 1, 2, 10, 0, tzinfo=IST)
        with patch.object(dashboard.time, "sleep"):
            candles = dashboard.fetch_history(
                ThrottlingClient(), FakeLimiter(), "NSE:A-EQ", "15", current,
                days=1, seconds=dashboard.CANDLE_SECONDS,
            )

        self.assertEqual(attempts["n"], 3)
        self.assertEqual(len(candles), 1)

    def test_history_gives_up_after_the_retry_budget(self):
        class AlwaysThrottled:
            def history(self, *args, **kwargs):
                raise RuntimeError("HTTP 429")

        current = dt.datetime(2026, 1, 2, 10, 0, tzinfo=IST)
        with patch.object(dashboard.time, "sleep"):
            with self.assertRaises(RuntimeError):
                dashboard.fetch_history(
                    AlwaysThrottled(), FakeLimiter(), "NSE:A-EQ", "15", current,
                    days=1, seconds=dashboard.CANDLE_SECONDS,
                )


class RegistryTests(unittest.TestCase):
    def test_registry_contains_all_requested_strategies(self):
        registry = dashboard.StrategyRegistry()
        keys = {item["key"] for item in registry.labels()}
        self.assertTrue({
            "orb", "orb_high_rejection", "r1_rejection", "prev_high_rejection",
            "second_candle", "ema_10_20_30", "index_rejection",
        }.issubset(keys))
        for spec in dashboard.STRATEGY_SPECS:
            registry.module_for(spec)
        # Several columns share one strategy script, so compare distinct
        # module names rather than the column count.
        self.assertEqual(
            len(registry._modules),
            len({spec.module_name for spec in dashboard.STRATEGY_SPECS}),
        )

    def test_every_strategy_has_a_short_column_header(self):
        for spec in dashboard.STRATEGY_SPECS:
            self.assertTrue(spec.column, spec.key)
            self.assertNotIn("\n", spec.column)
            # Headers wrap onto a second line, so the only hard rule is that a
            # header stays short enough to read at a glance.
            self.assertLessEqual(len(spec.column), 28, spec.column)

    def test_ema_column_headers_use_the_requested_names(self):
        names = {spec.key: spec.column for spec in dashboard.STRATEGY_SPECS}
        self.assertEqual(names["ema_10_20_30"], "ema:10-20-30")
        self.assertEqual(names["ema_fresh"], "ema15:10-20-50 crossover")
        self.assertNotIn("ema10/20/30", names.values())
        self.assertNotIn("ema15/10/20/50", names.values())

    def test_index_only_strategy_is_skipped_for_stock_rows(self):
        scanner = dashboard.DashboardScanner(dashboard.StrategyRegistry())
        specs = list(dashboard.STRATEGY_SPECS)
        stock_keys = {s.key for s in scanner._specs_for_group(specs, "stock")}

        self.assertNotIn("index_rejection", stock_keys)
        # every other strategy, including the newly added ones, judges a stock
        for key in ("open_ema_stack", "ema_10_cross_20", "lower_high_close",
                    "orb"):
            self.assertIn(key, stock_keys)

    def test_an_index_row_is_judged_by_every_strategy(self):
        """The indices tab shows the same columns as the others.

        It used to be judged only by index-only strategies, so every other
        column read "n/a" for all three indices.
        """
        scanner = dashboard.DashboardScanner(dashboard.StrategyRegistry())
        specs = list(dashboard.STRATEGY_SPECS)
        index_keys = {s.key for s in scanner._specs_for_group(specs, "index")}

        self.assertEqual(index_keys, {s.key for s in specs})
        for key in ("index_rejection", "open_ema_stack", "ema_10_cross_20",
                    "lower_high_close", "orb", "doji_rejection"):
            self.assertIn(key, index_keys)

    def test_a_stock_row_still_skips_the_index_only_strategies(self):
        scanner = dashboard.DashboardScanner(dashboard.StrategyRegistry())
        specs = list(dashboard.STRATEGY_SPECS)
        stock_keys = {s.key for s in scanner._specs_for_group(specs, "stock")}
        index_only = {s.key for s in specs if s.universe == "index_config"}
        self.assertTrue(index_only)
        self.assertFalse(stock_keys & index_only)


class PortGuardTests(unittest.TestCase):
    def test_a_busy_port_is_detected_before_binding(self):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
            server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            server.bind(("127.0.0.1", 0))
            server.listen(1)
            port = server.getsockname()[1]
            self.assertTrue(dashboard.port_is_in_use("127.0.0.1", port))

    def test_a_free_port_is_reported_free(self):
        self.assertFalse(dashboard.port_is_in_use("127.0.0.1", 9))


class UniverseTests(unittest.TestCase):
    def test_universe_is_described_before_a_scan_runs(self):
        summary = dashboard.DashboardScanner().describe_universe("all")

        self.assertGreater(summary["total"], 0)
        self.assertEqual(summary["total"], summary["stocks"] + summary["indices"])
        self.assertIn("stocks", summary["detail"])
        self.assertEqual(summary["source"], dashboard.UNIVERSE_PATH.name)

    def test_universe_counts_match_the_configured_file(self):
        expected = len(dashboard.read_symbols(dashboard.UNIVERSE_PATH))
        summary = dashboard.DashboardScanner().describe_universe("all")

        self.assertEqual(summary["stocks"], expected)
        self.assertEqual(summary["label"], "FNO Top 100")
        self.assertEqual(summary["indices"], 3)  # NIFTY, SENSEX, BANKNIFTY
        self.assertEqual(summary["total"], 103)

    def test_switching_to_nifty50_is_a_one_line_change(self):
        self.assertEqual(dashboard.UNIVERSE_PATH, dashboard.FNO_STOCKS_PATH)
        with patch.object(dashboard, "UNIVERSE_PATH", dashboard.STOCKS_PATH):
            summary = dashboard.DashboardScanner().describe_universe("all")
        self.assertEqual(summary["label"], "Nifty 50")
        self.assertEqual(summary["stocks"], 50)
        self.assertEqual(summary["total"], 53)

    def test_unknown_selection_does_not_raise(self):
        self.assertEqual(
            dashboard.DashboardScanner().describe_universe("nope")["total"], 0
        )

    def test_page_never_prints_a_hardcoded_symbol_count(self):
        html = dashboard.INDEX_HTML
        self.assertNotIn("100+ symbols", html)
        # The count still comes from the scan, in the wording shown when a
        # search matches nothing...
        self.assertIn("universeSummary()", html)
        self.assertIn("lastUniverse = start.universe;", html)
        # ...and the universe is no longer printed as a block of its own.
        self.assertNotIn('id="universeNote"', html)
        self.assertNotIn("function showUniverse(", html)


class LevelRejectionTests(unittest.TestCase):
    """R1 = 2P - L (classic pivot) and previous-day-high rejection."""

    def setUp(self):
        self.scanner = dashboard.DashboardScanner()
        self.specs, self.modules, errs = self.scanner._resolve_specs(
            list(dashboard.STRATEGY_SPECS)
        )
        self.assertEqual(errs, [])
        self.now = dt.datetime.now(IST)
        self.today = self.now.date()
        self.prev = self.today - dt.timedelta(days=1)
        while self.prev.weekday() >= 5:
            self.prev -= dt.timedelta(days=1)

    def _at(self, day, hour, minute, o, h, low, c):
        stamp = dt.datetime.combine(day, dt.time(hour, minute), tzinfo=IST)
        return SimpleNamespace(epoch=int(stamp.timestamp()), open=o, high=h,
                               low=low, close=c, volume=1)

    def _daily(self, high, low, close):
        """Two daily bars ending on the session before today."""
        return [
            self._at(self.prev - dt.timedelta(days=1), 15, 30, 100, 105, 95, 100),
            self._at(self.prev, 15, 30, 100, high, low, close),
        ]

    def _state(self, spec_key, bar, daily):
        spec = dashboard.STRATEGY_BY_KEY[spec_key]
        outcomes = self.scanner._evaluate(
            spec, self.modules[spec_key], "NSE:X-EQ", [bar], [], daily, self.now
        )
        return dashboard.build_cell(spec, outcomes, [bar])

    def test_r1_is_the_classic_pivot_of_the_previous_session(self):
        # H=110 L=100 C=105 -> P=105 -> R1 = 2*105 - 100 = 110
        daily = self._daily(110, 100, 105)
        bar = self._at(self.today, 10, 0, 106, 113, 105, 107)
        cell = self._state("r1_rejection", bar, daily)

        self.assertEqual(cell["state"], "SELL")
        outcomes = self.scanner._evaluate(
            dashboard.STRATEGY_BY_KEY["r1_rejection"],
            self.modules["r1_rejection"], "NSE:X-EQ", [bar], [], daily, self.now,
        )
        self.assertEqual(outcomes[0][1]["level"], 110.0)
        self.assertEqual(outcomes[0][1]["pivot"], 105.0)
        self.assertEqual(outcomes[0][1]["previous_day"], self.prev.isoformat())

    def test_both_directions_fire(self):
        daily = self._daily(110, 100, 105)  # R1 = 110
        sell = self._state("r1_rejection",
                           self._at(self.today, 10, 0, 106, 113, 105, 107), daily)
        buy = self._state("r1_rejection",
                          self._at(self.today, 10, 15, 105, 111, 104, 110.5), daily)
        self.assertEqual(sell["state"], "SELL")
        self.assertEqual(buy["state"], "BUY")

    def test_no_signal_when_the_level_is_untouched(self):
        daily = self._daily(110, 100, 105)
        self.assertEqual(
            self._state("r1_rejection",
                        self._at(self.today, 10, 0, 100, 104, 99, 103), daily)["state"], "-"
        )
        # high never reached R1 even though the close sat above it
        self.assertEqual(
            self._state("r1_rejection",
                        self._at(self.today, 10, 0, 108, 109.5, 107, 108.5), daily)["state"], "-"
        )

    def test_r1_and_previous_high_are_distinct_levels(self):
        # H=112 L=98 C=104 -> R1 = 111.33, previous high = 112
        daily = self._daily(112, 98, 104)
        bar = self._at(self.today, 11, 0, 110, 111.8, 110, 111.0)
        self.assertEqual(self._state("r1_rejection", bar, daily)["state"], "SELL")
        self.assertEqual(self._state("prev_high_rejection", bar, daily)["state"], "-")

    def test_previous_high_rejection_uses_the_prior_high(self):
        daily = self._daily(112, 98, 104)
        cell = self._state("prev_high_rejection",
                           self._at(self.today, 11, 15, 111, 112.5, 111, 111.5), daily)
        outcomes = self.scanner._evaluate(
            dashboard.STRATEGY_BY_KEY["prev_high_rejection"],
            self.modules["prev_high_rejection"], "NSE:X-EQ",
            [self._at(self.today, 11, 15, 111, 112.5, 111, 111.5)], [], daily, self.now,
        )
        self.assertEqual(cell["state"], "SELL")
        self.assertEqual(outcomes[0][1]["level"], 112.0)

    def test_eod_view_measures_against_the_prior_session_not_itself(self):
        # The judged bar IS the newest daily bar, so the level must come from
        # the session before it rather than from itself.
        # Reference H=60 L=40 C=55 -> P=51.67 -> R1=63.33
        daily = [
            self._at(self.prev - dt.timedelta(days=2), 15, 30, 50, 60, 40, 55),
            self._at(self.prev - dt.timedelta(days=1), 15, 30, 50, 60, 40, 55),
        ]
        judged = self._at(self.prev, 15, 30, 60, 70, 55, 68)  # low 55 < R1, close 68 > R1
        outcomes = self.scanner._evaluate(
            dashboard.STRATEGY_BY_KEY["r1_rejection"],
            self.modules["r1_rejection"], "NSE:X-EQ", [judged], [], daily, self.now,
        )

        self.assertTrue(outcomes, "expected a signal from the EOD-style bar")
        self.assertEqual(outcomes[0][0], "CE")
        self.assertEqual(round(outcomes[0][1]["level"], 2), 63.33)
        self.assertEqual(outcomes[0][1]["previous_day"],
                         (self.prev - dt.timedelta(days=1)).isoformat())
        self.assertNotEqual(outcomes[0][1]["previous_day"], self.prev.isoformat())

    def test_missing_daily_history_produces_no_signal(self):
        spec = dashboard.STRATEGY_BY_KEY["r1_rejection"]
        bar = self._at(self.today, 10, 0, 106, 113, 105, 107)
        self.assertEqual(self.scanner._evaluate(
            spec, self.modules["r1_rejection"], "NSE:X-EQ", [bar], [], [], self.now), [])

    def test_orb_high_rejection_only_fires_on_the_bearish_branch(self):
        spec = dashboard.STRATEGY_BY_KEY["orb_high_rejection"]
        orb_bar = self._at(self.today, 9, 15, 99.5, 101.0, 99.0, 100.0)
        cases = [
            ("high>=101 and close<101", self._at(self.today, 10, 0, 100.5, 103.0, 100.0, 100.5), "SELL"),
            ("low<=99 and close>99", self._at(self.today, 10, 15, 99.0, 100.0, 98.0, 99.8), "-"),
            ("inside the range", self._at(self.today, 10, 30, 100.0, 100.8, 99.5, 100.2), "-"),
        ]
        for label, bar, expected in cases:
            outcomes = self.scanner._evaluate(
                spec, self.modules["orb_high_rejection"], "NSE:X-EQ",
                [orb_bar, bar], [], [], self.now,
            )
            cell = dashboard.build_cell(spec, outcomes, [bar])
            self.assertEqual(cell["state"], expected, label)

    def test_new_columns_are_ordered_right_after_orb(self):
        keys = [spec.key for spec in dashboard.STRATEGY_SPECS]
        # Open EMA stack leads the table, then the opening-range group. The
        # two ORB rejections sit together, high side then low side, and the
        # double bottom follows them.
        self.assertEqual(keys[:7], [
            "open_ema_stack", "orb", "orb_high_rejection", "orb_low_rejection",
            "double_bottom", "r1_rejection", "prev_high_rejection",
        ])

    def test_the_open_ema_stack_column_is_the_first_signal_column(self):
        """First means first among the strategies, not ahead of the symbol."""
        keys = [spec.key for spec in dashboard.STRATEGY_SPECS]
        self.assertEqual(keys[0], "open_ema_stack")
        # The four data columns still lead the table. Two of the headers are
        # now built by sortHeaderLabel() at render time rather than written out
        # literally, so the row is checked in the order the script emits it.
        script = ScriptSyntaxTests._script()
        head = script[script.index("let html = '<table><thead><tr>'"):
                      script.index("columns.map(c => '<th class=\"sig-col\"")]
        for fragment in ('class="left col-stock"', '<th class="col-price"',
                         "sortHeaderLabel('col-chg'",
                         "sortHeaderLabel('col-vol'"):
            self.assertIn(fragment, head, fragment)
        self.assertLess(head.index('class="left col-stock"'),
                        head.index('<th class="col-price"'))
        self.assertLess(head.index('<th class="col-price"'),
                        head.index("sortHeaderLabel('col-chg'"))
        self.assertLess(head.index("sortHeaderLabel('col-chg'"),
                        head.index("sortHeaderLabel('col-vol'"))

    def test_intraday_only_columns_are_flagged_for_the_eod_note(self):
        flagged = {s.key for s in dashboard.STRATEGY_SPECS if s.intraday_only}
        self.assertEqual(flagged, {
            "orb", "orb_high_rejection", "orb_low_rejection", "second_candle",
            "index_rejection", "open_ema_stack",
        })


class DojiColumnTests(unittest.TestCase):
    """The Doji rejection column must reuse the live strategy's own function."""

    def setUp(self):
        self.scanner = dashboard.DashboardScanner()
        self.column = dashboard.STRATEGY_BY_KEY["doji_rejection"]
        self.modules = {self.column.key: self.scanner.registry.module_for(self.column)}

    def _bars(self, today):
        def at(hour, minute, o, h, low, c):
            stamp = dt.datetime.combine(today, dt.time(hour, minute), tzinfo=IST)
            return SimpleNamespace(epoch=int(stamp.timestamp()), open=o, high=h,
                                   low=low, close=c, volume=1000)
        return [
            at(9, 15, 103.0, 104.0, 102.0, 103.5),
            at(9, 30, 103.0, 104.0, 102.0, 103.5),
            at(10, 15, 105.0, 110.0, 100.0, 105.5),   # doji, body 5% of range
            at(10, 30, 110.0, 110.0, 99.0, 99.5),     # opens at the high, closes below
        ]

    def test_the_column_is_present(self):
        columns = {spec.key: spec.column for spec in dashboard.STRATEGY_SPECS}
        self.assertIn("Doji rejection", columns.values())
        # The full set is spelled out rather than counted, so adding a column
        # is a deliberate act instead of an invisible count change.
        self.assertEqual(set(columns.values()), {
            "ORB", "ORB High Rej", "ORB Low Rej", "Double Bottom",
            "R1 rejection", "PDH Rejection", "Doji rejection", "HigherHigh rej",
            "HH vs Close", "LH", "SecondCandle", "ema:10-20-30",
            "ema15:10-20-50 crossover", "ema10:20 signals", "Open EMA stack",
            "EMA10 pullback", "Daily breakout", "Index rejection",
        })

    def test_it_points_at_the_live_strategy_script(self):
        script = REPO_ROOT / "strategies" / "scripts" / "R1PrevHighRejectionStrategy.py"
        self.assertEqual(self.column.module_path, script)
        self.assertEqual(self.column.evaluator_name, "doji_rejection_signal")
        self.assertTrue(hasattr(self.modules["doji_rejection"], "doji_rejection_signal"))

    def test_a_failed_doji_breakout_shows_a_sell_with_the_bar_close_time(self):
        today = dt.date(2026, 1, 2)
        now = dt.datetime.combine(today, dt.time(11, 0), tzinfo=IST)
        cells = self.scanner._build_cells(
            [self.column], self.modules, "NSE:X-EQ", self._bars(today), [], [], now)
        cell = cells["doji_rejection"]

        self.assertEqual(cell["state"], "SELL")
        self.assertIn("breakout fail", cell["note"])
        # the signal bar opened 10:30, so its 15-minute bar closes at 10:45
        self.assertEqual(cell["time_ist"], "10:45")

    def test_a_strong_previous_bar_leaves_the_cell_blank(self):
        today = dt.date(2026, 1, 2)
        now = dt.datetime.combine(today, dt.time(11, 0), tzinfo=IST)
        bars = self._bars(today)
        # replace the doji with a strong bull bar, so no rejection is possible
        bars[2] = SimpleNamespace(epoch=bars[2].epoch, open=103.0, high=108.0,
                                  low=102.0, close=107.5, volume=1000)
        cells = self.scanner._build_cells(
            [self.column], self.modules, "NSE:X-EQ", bars, [], [], now)

        self.assertEqual(cells["doji_rejection"]["state"], "-")

    def test_stock_rows_include_it_and_index_rows_do_too(self):
        """The rejection columns read an index series just as well."""
        specs = list(dashboard.STRATEGY_SPECS)
        stock_keys = {s.key for s in self.scanner._specs_for_group(specs, "stock")}
        index_keys = {s.key for s in self.scanner._specs_for_group(specs, "index")}

        self.assertIn("doji_rejection", stock_keys)
        self.assertIn("doji_rejection", index_keys)
        # the index-only strategy still goes the other way
        self.assertNotIn("index_rejection", stock_keys)
        self.assertIn("index_rejection", index_keys)

    def test_every_stock_row_gains_a_cell_for_it(self):
        today = dt.date(2026, 1, 2)
        now = dt.datetime.combine(today, dt.time(11, 0), tzinfo=IST)
        stock_specs = self.scanner._specs_for_group(list(dashboard.STRATEGY_SPECS), "stock")
        _specs, modules, errors = self.scanner._resolve_specs(stock_specs)
        self.assertEqual(errors, [])

        cells = self.scanner._build_cells(
            stock_specs, modules, "NSE:X-EQ", self._bars(today), [], [], now)
        self.assertIn("doji_rejection", cells)
        self.assertEqual(len(cells), len(stock_specs))


class LayoutTests(unittest.TestCase):
    def test_table_cannot_scroll_horizontally(self):
        html = dashboard.INDEX_HTML
        self.assertIn("table-layout: fixed", html)
        self.assertIn("overflow-x: hidden", html)
        self.assertNotIn("overflow: auto", html.split(".tablewrap")[1][:200])

    def test_data_columns_have_fixed_widths_so_signals_share_the_rest(self):
        html = dashboard.INDEX_HTML
        for cls in ("col-stock", "col-price", "col-chg", "col-vol"):
            self.assertIn(f"thead th.{cls} {{ width:", html)
        self.assertIn('class="left col-stock"', html)
        self.assertIn("<th class=\"sig-col\"", html)

    def test_headers_are_allowed_to_wrap_instead_of_scrolling(self):
        self.assertIn("thead th {", dashboard.INDEX_HTML)
        header_rule = dashboard.INDEX_HTML.split("thead th {")[1].split("}")[0]
        self.assertIn("white-space: normal", header_rule)


class ScanManagerTests(unittest.TestCase):
    """The page can recover the last scan, so a reload is never stranded."""

    def setUp(self):
        # ScanManager.start() launches a real background thread that would call
        # the live scanner, hit the FYERS API and write the production candle
        # store. Replace the scan so these tests stay pure.
        patcher = patch.object(
            dashboard.DashboardScanner, "scan",
            return_value={"selection": "all", "marker": "stubbed", "views": {}},
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def _manager(self):
        return dashboard.ScanManager(dashboard.DashboardScanner())

    def test_no_latest_scan_before_anything_runs(self):
        self.assertIsNone(self._manager().latest_complete())

    def test_latest_complete_returns_the_newest_finished_scan(self):
        manager = self._manager()
        for finished in ("2026-01-02T10:00:00+05:30", "2026-01-02T11:00:00+05:30"):
            job_id, _ = manager.start("all")
            with manager._lock:
                job = manager._jobs[job_id]
                job.update(status="complete", result={"marker": finished},
                           finished_at=finished)
        latest = manager.latest_complete()
        self.assertEqual(latest["result"]["marker"], "2026-01-02T11:00:00+05:30")

    def test_running_and_failed_jobs_are_never_returned(self):
        manager = self._manager()
        job_id, _ = manager.start("all")
        with manager._lock:
            manager._jobs[job_id]["status"] = "running"
        self.assertIsNone(manager.latest_complete())

        job_id2, _ = manager.start("all")
        with manager._lock:
            manager._jobs[job_id2].update(
                status="error", error="boom", finished_at="2026-01-02T10:00:00+05:30")
        self.assertIsNone(manager.latest_complete())

    def test_a_started_job_does_not_run_a_real_scan(self):
        """Guard: a background job must never reach the live scanner."""
        manager = self._manager()
        job_id, _ = manager.start("all")
        deadline = time.time() + 5
        while time.time() < deadline:
            with manager._lock:
                if manager._jobs[job_id]["status"] != "running":
                    break
            time.sleep(0.05)
        with manager._lock:
            result = manager._jobs[job_id].get("result")
        self.assertEqual(result["marker"], "stubbed")

    def test_api_latest_endpoint_is_registered(self):
        source = Path(MODULE_PATH).read_text(encoding="utf-8")
        self.assertIn('parsed.path == "/api/latest"', source)


class CandleStoreTests(unittest.TestCase):
    """The dashboard must persist the candles it already downloads."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = pathlib.Path(self.tmp.name) / "candles.db"
        self.store = dashboard.CandleStore(self.path)
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.store.close)

    def _candles(self, start_epoch, count):
        return [
            SimpleNamespace(epoch=start_epoch + i * 900, open=100.0 + i,
                            high=101.0 + i, low=99.0 + i, close=100.5 + i,
                            volume=1000 + i)
            for i in range(count)
        ]

    def test_schema_and_file_are_created_on_first_write(self):
        self.assertFalse(self.path.exists())
        self.store.store("NSE:A-EQ", "15", self._candles(1_700_000_000, 3))
        self.assertTrue(self.path.exists())
        self.assertGreater(self.store.stored_rows, 0)

    def test_rows_are_written_for_every_resolution(self):
        self.store.store("NSE:A-EQ", "15", self._candles(1_700_000_000, 3))
        self.store.store("NSE:A-EQ", "D", self._candles(1_700_000_000, 2))
        summary = self.store.summary()
        self.assertEqual(summary["total_rows"], 5)
        self.assertEqual(
            {item["resolution"] for item in summary["by_resolution"]}, {"15", "D"})

    def test_rescanning_merges_instead_of_duplicating(self):
        first = self._candles(1_700_000_000, 5)
        self.store.store("NSE:A-EQ", "15", first)
        self.store.store("NSE:A-EQ", "15", first)          # identical rescan
        self.assertEqual(self.store.summary()["total_rows"], 5)

        # a later scan overlapping the last two bars and adding two more:
        # epochs 0-4 plus 5-6, with 3 and 4 replaced rather than duplicated
        self.store.store("NSE:A-EQ", "15", self._candles(1_700_000_000 + 3 * 900, 4))
        summary = self.store.summary()
        self.assertEqual(summary["total_rows"], 7)
        self.assertEqual(summary["symbols"], 1)

    def test_indices_and_stocks_are_stored_side_by_side(self):
        self.store.store("NSE:RELIANCE-EQ", "15", self._candles(1_700_000_000, 2))
        self.store.store("NSE:NIFTY50-INDEX", "15", self._candles(1_700_000_000, 2))
        summary = self.store.summary()
        self.assertEqual(summary["symbols"], 2)
        self.assertEqual(summary["index_symbols"], 1)

    def test_summary_reports_the_newest_bar(self):
        self.store.store("NSE:A-EQ", "15", self._candles(1_700_000_000, 4))
        item = self.store.summary()["by_resolution"][0]
        expected = dt.datetime.fromtimestamp(
            1_700_000_000 + 3 * 900, IST).isoformat(timespec="minutes")
        self.assertEqual(item["newest_ist"], expected)
        self.assertTrue(item["oldest_ist"])
        self.assertEqual(item["symbols"], 1)

    def test_prune_drops_bars_beyond_the_retention_window(self):
        old = 1_500_000_000
        recent = int(dt.datetime.now(IST).timestamp()) - 3600
        self.store.store("NSE:OLD-EQ", "15", self._candles(old, 3))
        self.store.store("NSE:NEW-EQ", "15", self._candles(recent, 3))
        self.assertEqual(self.store.summary()["total_rows"], 6)

        removed = self.store.prune(retention_days=30)
        self.assertEqual(removed, 3)
        remaining = self.store.summary()
        self.assertEqual(remaining["total_rows"], 3)
        self.assertEqual(remaining["symbols"], 1)

    def test_a_disabled_store_writes_nothing(self):
        store = dashboard.CandleStore(self.path, enabled=False)
        self.assertEqual(store.store("NSE:A-EQ", "15", self._candles(1, 3)), 0)
        self.assertIsNone(store.connect())
        self.assertFalse(self.path.exists())
        self.assertEqual(store.prune(), 0)
        self.assertEqual(store.summary()["total_rows"], 0)

    def test_a_disabled_store_still_reports_disabled(self):
        summary = dashboard.CandleStore(self.path, enabled=False).summary()
        self.assertFalse(summary["enabled"])
        self.assertEqual(summary["by_resolution"], [])

    def test_empty_candle_list_is_a_no_op(self):
        self.assertEqual(self.store.store("NSE:A-EQ", "15", []), 0)
        self.assertEqual(self.store.summary()["total_rows"], 0)

    def test_scan_result_reports_storage(self):
        result = run_scan(["NSE:A-EQ"])
        self.assertIn("storage", result)
        self.assertIn("stored_this_scan", result)
        self.assertTrue(result["storage"]["enabled"])

    def test_tests_never_write_to_the_production_candle_store(self):
        """A regression guard: the fakes must not pollute the real database.

        Comparing the file size is not enough - INSERT OR REPLACE rewrites the
        same rows and leaves the size identical - so the contents are compared.
        """
        real_path = dashboard.CANDLE_DB_PATH
        self.assertTrue(
            real_path.name.startswith("dashboard_"),
            "the production store should be the dashboard database",
        )

        def fingerprint():
            if not real_path.exists():
                return None
            import sqlite3 as sqlite
            try:
                conn = sqlite.connect(f"file:{real_path}?mode=ro", uri=True)
            except sqlite.Error:
                return None
            try:
                rows = conn.execute(
                    "SELECT symbol, resolution, COUNT(*), MAX(stored_at) "
                    "FROM candles GROUP BY symbol, resolution "
                    "ORDER BY symbol, resolution").fetchall()
                return rows
            except sqlite.Error:
                return None
            finally:
                conn.close()

        before = fingerprint()
        result = run_scan(["NSE:A-EQ", "NSE:B-EQ", "NSE:C-EQ"])

        # the fakes stored something...
        self.assertGreater(result["stored_this_scan"], 0)
        # ...into a temporary file, leaving the production store untouched
        self.assertNotEqual(result["storage"]["path"], str(real_path))
        self.assertEqual(
            fingerprint(), before,
            "the production candle database contents must not change during tests",
        )

    def test_storage_endpoint_exists_and_the_page_prints_no_storage_note(self):
        """The endpoint still reports the cache; the page no longer narrates it.

        The note was four dense lines between the market line and the table, so
        it was removed at the user's request. The endpoint stays, because it is
        how the cache is inspected without reading the database file.
        """
        source = Path(MODULE_PATH).read_text(encoding="utf-8")
        self.assertIn('parsed.path == "/api/storage"', source)
        self.assertNotIn('id="storageNote"', dashboard.INDEX_HTML)
        self.assertNotIn("function showStorage(", dashboard.INDEX_HTML)


REGEX_PRECEDERS = set("(,=:[!&|?{};+-*%~^<>\n")


class ScriptSyntaxTests(unittest.TestCase):
    """The served page must contain syntactically valid JavaScript.

    A single stray parenthesis makes the whole script fail to parse, which
    leaves the page rendering with no data at all and no visible error.
    """

    @staticmethod
    def _script() -> str:
        match = re.search(r"<script>([\s\S]*?)</script>", dashboard.INDEX_HTML)
        assert match, "the page has no <script> block"
        return match.group(1)

    @staticmethod
    def _node() -> str | None:
        for name in ("node", "nodejs"):
            path = shutil.which(name)
            if path:
                return path
        return None

    def test_the_embedded_script_parses(self):
        node = self._node()
        if node is None:
            self.skipTest("node is not installed, cannot syntax-check the page script")
        with tempfile.TemporaryDirectory() as tmp:
            script = pathlib.Path(tmp) / "page.js"
            script.write_text(self._script(), encoding="utf-8")
            result = subprocess.run(
                [node, "--check", str(script)],
                capture_output=True, text=True,
            )
        self.assertEqual(
            result.returncode, 0,
            f"the page script does not parse:\n{result.stdout}\n{result.stderr}",
        )

    def test_brackets_balance_outside_strings_and_comments(self):
        """A pure-Python fallback that does not need node installed.

        A string-aware scan, so multi-line concatenations and apostrophes
        inside comments are not mistaken for problems.
        """
        script = self._script()
        pairs = {")": "(", "]": "[", "}": "{"}
        stack: list[tuple[str, int]] = []
        index = 0
        line = 1
        state = None  # None | "'" | '"' | '`' | 'line' | 'block' | 'regex'
        prev_significant = ""
        prev = ""
        while index < len(script):
            char = script[index]
            nxt = script[index + 1] if index + 1 < len(script) else ""
            if char == "\n":
                line += 1
                if state == "line":
                    state = None
                index += 1
                continue
            if state in ("'", '"', "`"):
                if char == "\\":
                    index += 2
                    continue
                if char == state:
                    state = None
                index += 1
                continue
            if state == "regex":
                if char == "\\":
                    index += 2
                    continue
                if char == "/":
                    state = None
                elif char == "[":
                    # a character class can hold unescaped '/'
                    while index < len(script) and script[index] != "]":
                        index += 2 if script[index] == "\\" else 1
                index += 1
                continue
            if state == "block":
                if char == "*" and nxt == "/":
                    state = None
                    index += 2
                    continue
                index += 1
                continue
            if state == "line":
                index += 1
                continue
            # not in a string or comment
            if char == "/" and nxt == "/":
                state = "line"
                index += 2
                continue
            if char == "/" and nxt == "*":
                state = "block"
                index += 2
                continue
            # A regex literal may contain quotes, so it must be recognised
            # before quotes. The standard heuristic: a '/' that follows an
            # operator or an opening bracket starts a regex, not a division.
            if char == "/" and (prev_significant in REGEX_PRECEDERS or prev == ""):
                state = "regex"
                index += 1
                continue
            if char in "'\"`":
                state = char
                index += 1
                continue
            if not char.isspace():
                prev_significant = char
                prev = char
            if char in "([{":
                stack.append((char, line))
            elif char in ")]}":
                if not stack:
                    self.fail(f"unmatched '{char}' on script line {line}")
                opener, opened_at = stack.pop()
                if opener != pairs[char]:
                    self.fail(
                        f"'{opener}' opened on line {opened_at} is closed by "
                        f"'{char}' on line {line}"
                    )
            index += 1

        self.assertIsNone(state, f"unterminated string or comment: {state!r}")
        self.assertEqual(
            stack, [], f"unclosed brackets: {[(c, l) for c, l in stack]}")

    def test_bootstrap_does_not_reference_undeclared_names(self):
        script = self._script()
        match = re.search(r"async function bootstrap\(\)\s*\{(.*?)\n\}", script, re.S)
        assert match, "bootstrap() not found"
        body = match.group(1)
        # `result` is not declared in bootstrap; only the payload variable is.
        self.assertNotRegex(body, r"(?<![\w.])result\.")
        self.assertIn("latest.result", body)

    def test_scanning_placeholder_keeps_the_string_inside_esc(self):
        """The exact shape that broke: a stray ')' before the string ended."""
        script = self._script()
        self.assertIn(
            "esc('Scanning ' + universeSummary() + ", script,
            "the scanning placeholder must build one string inside esc()",
        )
        self.assertNotIn("universeSummary()) + '", script)


class PerViewColumnTests(unittest.TestCase):
    """The intraday tab drops the two columns that do not belong there."""

    def test_the_two_columns_are_flagged(self):
        flagged = {s.key for s in dashboard.STRATEGY_SPECS if s.hide_in_intraday}
        self.assertEqual(flagged, {"daily_breakout", "index_rejection"})

    def test_intraday_view_omits_them_and_eod_keeps_them(self):
        scanner = dashboard.DashboardScanner()
        specs = list(dashboard.STRATEGY_SPECS)
        modules, errors = scanner._resolve_specs(specs)[1:]
        self.assertEqual(errors, [])
        columns = [
            {"key": s.key, "column": s.column, "label": s.label} for s in specs
        ]
        intraday_keys = {s.key for s in specs if not s.hide_in_intraday}

        def row(cells):
            return {"symbol": "NSE:A-EQ", "group": "stock", "close": 1.0,
                    "total_gain_percent": 0.0, "volume_15m": 1, "signal_count": 0,
                    "cells": cells}

        cells = {
            "daily_breakout": {"state": "BUY", "time_ist": "10:00", "note": "", "price": 1.0},
            "index_rejection": {"state": "SELL", "time_ist": "10:00", "note": "", "price": 1.0},
            "r1_rejection": {"state": "BUY", "time_ist": "10:00", "note": "", "price": 1.0},
        }
        state = {"message": "m", "market_open": False, "as_of_ist": None,
                 "as_of_label": "x", "note": "", "kind": "closed"}

        intraday = dashboard.build_view(
            "intraday", "Intraday", [row(cells)], state, True, 1, 1, columns,
            column_keys=intraday_keys)
        eod = dashboard.build_view(
            "eod", "EOD", [row(cells)], state, True, 1, 1, columns)

        self.assertNotIn("daily_breakout", [c["key"] for c in intraday["columns"]])
        self.assertNotIn("index_rejection", [c["key"] for c in intraday["columns"]])
        self.assertIn("r1_rejection", [c["key"] for c in intraday["columns"]])
        self.assertEqual(len(intraday["columns"]), len(columns) - 2)

        # the end-of-day tab keeps every column
        self.assertEqual(len(eod["columns"]), len(columns))
        self.assertIn("daily_breakout", [c["key"] for c in eod["columns"]])
        self.assertIn("index_rejection", [c["key"] for c in eod["columns"]])

    def test_hidden_columns_cannot_inflate_the_signal_count(self):
        """A row whose only signal is hidden must not count as a signal row."""
        columns = [
            {"key": "daily_breakout", "column": "Daily", "label": "d"},
            {"key": "r1_rejection", "column": "R1", "label": "r"},
        ]
        row = {
            "symbol": "NSE:A-EQ", "group": "stock", "close": 1.0,
            "total_gain_percent": 0.0, "volume_15m": 1, "signal_count": 99,
            "cells": {
                "daily_breakout": {"state": "BUY", "time_ist": "10:00", "note": "", "price": 1.0},
                "r1_rejection": {"state": "-", "time_ist": None, "note": "", "price": None},
            },
        }
        state = {"message": "m", "market_open": False, "as_of_ist": None,
                 "as_of_label": "x", "note": "", "kind": "closed"}

        view = dashboard.build_view(
            "intraday", "Intraday", [row], state, True, 1, 1, columns,
            column_keys={"r1_rejection"})

        self.assertEqual(view["signal_rows"], 0)
        self.assertEqual(view["rows"][0]["signal_count"], 0)
        self.assertEqual(list(view["rows"][0]["cells"]), ["r1_rejection"])

    def test_a_visible_signal_is_still_counted(self):
        columns = [
            {"key": "daily_breakout", "column": "Daily", "label": "d"},
            {"key": "r1_rejection", "column": "R1", "label": "r"},
        ]
        row = {
            "symbol": "NSE:A-EQ", "group": "stock", "close": 1.0,
            "total_gain_percent": 0.0, "volume_15m": 1, "signal_count": 0,
            "cells": {
                "daily_breakout": {"state": "BUY", "time_ist": "10:00", "note": "", "price": 1.0},
                "r1_rejection": {"state": "SELL", "time_ist": "10:00", "note": "", "price": 1.0},
            },
        }
        state = {"message": "m", "market_open": False, "as_of_ist": None,
                 "as_of_label": "x", "note": "", "kind": "closed"}
        view = dashboard.build_view(
            "intraday", "Intraday", [row], state, True, 1, 1, columns,
            column_keys={"r1_rejection"})
        self.assertEqual(view["signal_rows"], 1)
        self.assertEqual(view["rows"][0]["signal_count"], 1)

    def test_rejections_tab_is_unaffected(self):
        scanner = dashboard.DashboardScanner()
        specs = list(dashboard.STRATEGY_SPECS)
        rejection_keys = {s.key for s in specs if s.sell_only}
        columns = [{"key": s.key, "column": s.column, "label": s.label} for s in specs]
        row = {"symbol": "NSE:A-EQ", "group": "stock", "close": 1.0,
               "total_gain_percent": 0.0, "volume_15m": 1, "signal_count": 0,
               "cells": {"r1_rejection": {"state": "SELL", "time_ist": "10:00",
                                          "note": "", "price": 1.0}}}
        state = {"message": "m", "market_open": False, "as_of_ist": None,
                 "as_of_label": "x", "note": "", "kind": "closed"}
        view = dashboard.build_view(
            "rejections", "Rejections", [row], state, True, 1, 1, columns,
            sell_only=True, column_keys=rejection_keys)
        self.assertEqual(len(view["columns"]), len(rejection_keys))
        self.assertEqual(view["signal_rows"], 1)


class SearchMountTests(unittest.TestCase):
    """Run the real page script in a small DOM and watch the search box.

    The box lives inside the Stock header cell, which render() rebuilds on every
    keystroke. No substring assertion can prove the box survives that rebuild,
    so the actual page script is executed here. Skipped when node is absent.
    """

    # A minimal DOM. The page script is concatenated into the same scope, so
    # `document` is the body element itself and focus bookkeeping hangs off
    # the root of each tree.
    SHIM = r"""
class El {
  constructor(tag) {
    this.tagName = (tag || 'div').toUpperCase();
    this.children = []; this.parent = null; this._html = '';
    this._classes = new Set(); this.attrs = {}; this.dataset = {};
    this.style = {}; this.hidden = false; this.value = ''; this.id = '';
    this._listeners = {}; this.selectionStart = 0; this.selectionEnd = 0;
  }
  set className(v) { this._classes = new Set(String(v).split(/\s+/).filter(Boolean)); }
  get className() { return [...this._classes].join(' '); }
  set innerHTML(v) {
    /* A browser blurs the focused element when it is detached from the
       document. Modelling that is what makes the focus-restore assertion in
       render() meaningful: without this the caret would never be lost and the
       missing restore would go unnoticed. */
    const active = this.root.activeElement;
    for (let n = active; n; n = n.parent) {
      if (n === this) { this.root.activeElement = null; break; }
    }
    this._html = String(v);
    this.children.forEach(c => { c.parent = null; });
    this.children = parse(String(v));
    this.children.forEach(c => { c.parent = this; });
  }
  get innerHTML() { return this._html; }
  get classList() {
    const s = this._classes;
    return { add: c => s.add(c), remove: c => s.delete(c),
      toggle: (c, on) => (on === undefined ? (s.has(c) ? s.delete(c) : s.add(c))
                                          : (on ? s.add(c) : s.delete(c))),
      contains: c => s.has(c) };
  }
  get all() {
    const out = []; const walk = n => n.children.forEach(c => { out.push(c); walk(c); });
    walk(this); return out;
  }
  get root() { let r = this; while (r.parent) r = r.parent; return r; }
  matchOne(sel) {
    return (sel.match(/(\[[^\]]+\]|[.#]?[\w-]+)/g) || []).every(p => {
      if (p.startsWith('.')) return this._classes.has(p.slice(1));
      if (p.startsWith('#')) return this.id === p.slice(1);
      if (p.startsWith('[')) { const m = p.match(/\[([^=\]]+)(?:="([^"]*)")?\]/);
        if (!m) return false;
        return m[2] !== undefined ? this.attrs[m[1]] === m[2] : m[1] in this.attrs; }
      return this.tagName === p.toUpperCase();
    });
  }
  querySelectorAll(sel) {
    const parts = sel.trim().split(/\s+/);
    const last = parts[parts.length - 1];
    const rest = parts.slice(0, -1);
    return this.all.filter(el => {
      if (!el.matchOne(last)) return false;
      let n = el.parent;
      for (let i = rest.length - 1; i >= 0; i--) {
        let hit = false;
        while (n) { if (n.matchOne(rest[i])) { hit = true; n = n.parent; break; } n = n.parent; }
        if (!hit) return false;
      }
      return true;
    });
  }
  querySelector(sel) { return this.querySelectorAll(sel)[0] || null; }
  contains(node) { let n = node; while (n) { if (n === this) return true; n = n.parent; } return false; }
  closest(sel) { let n = this; while (n) { if (n.matchOne(sel.trim())) return n; n = n.parent; } return null; }
  appendChild(c) {
    if (c.parent) c.parent.children = c.parent.children.filter(x => x !== c);
    c.parent = this; this.children.push(c); return c;
  }
  addEventListener(t, h) { (this._listeners[t] = this._listeners[t] || []).push(h); }
  focus() { this.root.activeElement = this; }
  blur() { if (this.root.activeElement === this) this.root.activeElement = null; }
  select() { this.selectionStart = 0; this.selectionEnd = this.value.length; }
  setSelectionRange(a, b) { this.selectionStart = a; this.selectionEnd = b; }
  fire(t) {
    const ev = { type: t, target: this, preventDefault() {}, stopPropagation() {} };
    let n = this; while (n) { (n._listeners[t] || []).forEach(h => h.call(n, ev)); n = n.parent; }
  }
}
const VOID = new Set(['input', 'br', 'hr', 'img', 'meta', 'link']);
function parse(html) {
  const root = new El('root'); const stack = [root];
  const re = /<\/?([a-zA-Z][\w-]*)((?:\s+[\w-]+(?:\s*=\s*"[^"]*")?)*)\s*(\/?)>/g;
  let m;
  while ((m = re.exec(html))) {
    const [full, tag, attrs, sc] = m; const lower = tag.toLowerCase();
    if (full.startsWith('</')) {
      if (stack.length > 1 && stack[stack.length - 1].tagName === lower.toUpperCase()) stack.pop();
      continue;
    }
    const el = new El(lower);
    for (const a of (attrs || '').matchAll(/([\w-]+)(?:\s*=\s*"([^"]*)")?/g)) {
      const k = a[1], v = a[2] === undefined ? '' : a[2];
      if (k === 'class') el.className = v;
      else if (k === 'id') el.id = v;
      else { el.attrs[k] = v; if (k.startsWith('data-')) el.dataset[k.slice(5)] = v; }
    }
    if ('hidden' in el.attrs) el.hidden = true;
    stack[stack.length - 1].appendChild(el);
    if (!sc && !VOID.has(lower)) stack.push(el);
  }
  return root.children;
}
const doc = new El('body');
doc.getElementById = id => doc.querySelector('#' + id) || new El('div');
doc.createElement = tag => new El(tag);
doc.addEventListener = () => {};
doc.activeElement = null;
/* The page script talks to `document`, so expose the body element under that
   name rather than keeping a separate wrapper that could drift out of sync. */
const document = doc;
parse(markup.slice(markup.indexOf('<body>'), markup.indexOf('<script>')))
  .forEach(node => doc.appendChild(node));
const store = {};
const localStorage = {
  getItem: k => (k in store ? store[k] : null),
  setItem: (k, v) => { store[k] = String(v); },
  removeItem: k => { delete store[k]; }
};
const fetch = async url => String(url).indexOf('/api/latest') >= 0
  ? { ok: true, status: 200, json: async () => ({ status: 'complete', result: payload }) }
  : { ok: false, status: 404, json: async () => ({}) };
"""

    TAIL = r"""
(async () => {
  // bootstrap() renders through a promise chain, so let it settle first.
  for (let i = 0; i < 8; i++) await new Promise(r => setImmediate(r));
  const out = {};
  const box = doc.getElementById('tablewrap');
  const input = doc.getElementById('search');
  const wrap = doc.getElementById('searchwrap');
  // the wrap is parented to the .searchslot span, not to the th directly
  const inStock = () => {
    const slot = box.querySelector('th.col-stock .searchslot');
    return !!(slot && slot.children.some(c => c === wrap));
  };
  const inCell = () => {
    const th = box.querySelector('th.col-stock');
    return !!(th && th.contains(input));
  };
  out.inHeaderAfterFirst = inCell();
  out.headerLabel = !!box.querySelector('th.col-stock .col-label');
  out.slot = !!box.querySelector('th.col-stock .searchslot');
  out.initialRows = box.querySelectorAll('tbody .sym').length;

  // Type, which re-renders the whole table exactly as a keystroke does.
  input.value = 'RELI';
  input.focus();
  input.selectionStart = input.selectionEnd = 4;
  input.fire('input');
  out.valueKept = input.value;
  out.focusKept = doc.activeElement === input;
  out.caret = input.selectionStart;
  out.sameNode = doc.getElementById('searchwrap') === wrap;
  out.stillInHeader = inStock() && inCell();
  out.rowsFiltered = box.querySelectorAll('tbody .sym').length;

  // A search matching nothing must not take away the control that clears it.
  input.value = 'NOSUCHSTOCK';
  input.fire('input');
  out.emptyRow = !!box.querySelector('tbody tr.empty-row');
  out.theadSurvived = !!box.querySelector('thead');
  out.inputAfterEmpty = inStock() && inCell();
  out.noMessageDiv = !box.querySelector('div.empty');

  process.stdout.write(JSON.stringify(out));
})();
"""

    PAYLOAD = {
        "selection": "all",
        "scanned_at_ist": "2026-09-25T15:30:00+05:30",
        "errors": [],
        "columns": [{"key": "orb", "column": "ORB", "label": "Opening range breakout"}],
        "universe": {"total": 2, "stocks": 2, "indices": 0, "label": "test",
                     "source": "test", "detail": "test"},
        "views": {
            "intraday": {
                "id": "intraday", "title": "Intraday", "sell_only": False,
                "columns": [{"key": "orb", "column": "ORB",
                             "label": "Opening range breakout"}],
                "rows": [
                    {"symbol": "NSE:RELIANCE-EQ", "group": "stock", "close": 100.0,
                     "total_gain_percent": 0.5, "volume_15m": 10, "signal_count": 1,
                     "cells": {"orb": {"state": "BUY", "time_ist": "10:15",
                                       "note": "", "price": 100.0}}},
                    {"symbol": "NSE:TCS-EQ", "group": "stock", "close": 200.0,
                     "total_gain_percent": -0.5, "volume_15m": 20, "signal_count": 0,
                     "cells": {"orb": {"state": "-", "time_ist": None,
                                       "note": "", "price": None}}},
                ],
                "total_rows": 2, "signal_rows": 1, "latest_15min": True,
                "latest_message": "Latest 15-minute candle", "market_open": True,
                "as_of_ist": "2026-09-25T15:30:00+05:30",
                "as_of_label": "Fri 25 Sep close", "note": "", "kind": "live",
            }
        },
    }

    def _run(self):
        node = ScriptSyntaxTests._node()
        if node is None:
            self.skipTest("node is not installed, cannot exercise the page script")
        driver = (
            "const markup = " + json.dumps(dashboard.INDEX_HTML) + ";\n"
            "const payload = " + json.dumps(self.PAYLOAD) + ";\n"
            + self.SHIM
            + ScriptSyntaxTests._script()
            + self.TAIL
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "mount.js"
            path.write_text(driver, encoding="utf-8")
            result = subprocess.run([node, str(path)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0,
                         f"driver failed:\n{result.stdout}\n{result.stderr}")
        self.assertTrue(result.stdout.strip(), "driver produced no output")
        return json.loads(result.stdout)

    def test_the_search_box_lands_in_the_stock_header(self):
        out = self._run()
        self.assertTrue(out["inHeaderAfterFirst"],
                        "render() must mount the search box in the Stock header")
        self.assertTrue(out["headerLabel"], "the Stock column keeps its label")
        self.assertTrue(out["slot"], "the header provides a slot for the box")
        self.assertEqual(out["initialRows"], 2, "both rows should be listed")

    def test_typing_survives_the_table_rebuild(self):
        out = self._run()
        self.assertEqual(out["valueKept"], "RELI")
        self.assertTrue(out["focusKept"],
                        "focus must be restored, or the caret is lost after one key")
        self.assertEqual(out["caret"], 4, "the caret must return to where it was")
        self.assertTrue(out["sameNode"],
                        "the same node must be moved, not re-created")
        self.assertTrue(out["stillInHeader"])
        self.assertEqual(out["rowsFiltered"], 1, "the search must actually filter")

    def test_a_search_matching_nothing_keeps_the_box_reachable(self):
        out = self._run()
        self.assertTrue(out["emptyRow"], "an empty result is shown as a table row")
        self.assertTrue(out["theadSurvived"], "the header must survive an empty result")
        self.assertTrue(out["inputAfterEmpty"],
                        "otherwise a fruitless search deletes the only way to clear it")
        self.assertTrue(out["noMessageDiv"],
                        "the table must not be replaced by a bare message div")


class IndexViewTests(unittest.TestCase):
    """The indices live in their own tab and nowhere else."""

    class _IndexModule:
        INDEX_CONFIG = {
            "NIFTY": {"fy_symbol": "NSE:NIFTY50-INDEX"},
            "SENSEX": {"fy_symbol": "BSE:SENSEX-INDEX"},
        }

        @staticmethod
        def index_rejection_signal(candles, display_symbol):
            return "NONE", {"reason": "not signalled"}

    def _registry(self):
        stock = dashboard.StrategySpec(
            key="fake", label="Fake strategy", column="Fake",
            module_name="fake", module_path=pathlib.Path("fake.py"),
            evaluator_name="evaluate", kind="ema",
        )
        index = dashboard.StrategySpec(
            key="idx", label="Index rejection", column="Index rejection",
            module_name="idx", module_path=pathlib.Path("idx.py"),
            evaluator_name="index_rejection_signal", kind="index_rejection",
            universe="index_config", intraday_only=True, sell_only=True,
            hide_in_intraday=True,
        )
        registry = FakeRegistry(object())
        registry.specs = (stock, index)
        registry._modules = {"fake": object(), "idx": self._IndexModule()}
        registry.selected_specs = lambda selection: (stock, index)
        registry.module_for = lambda spec: registry._modules[spec.key]
        registry.evaluator_for = lambda spec: (lambda candles: (True, {"curr_close": 1.0}))
        return registry

    def _scan(self):
        scanner = dashboard.DashboardScanner(self._registry())
        current = dt.datetime(2026, 1, 2, 10, 0, tzinfo=IST)
        with tempfile.TemporaryDirectory() as tmp:
            store_path = pathlib.Path(tmp) / "candles.db"
            with (
                patch.object(dashboard, "read_symbols",
                             return_value=["NSE:A-EQ", "NSE:B-EQ"]),
                patch.object(dashboard, "FyersClient", FakeHistoryClient),
                patch.object(dashboard, "now_ist", return_value=current),
                patch.object(dashboard, "CANDLE_DB_PATH", store_path),
            ):
                return scanner.scan("all")

    def test_indices_appear_only_in_the_indices_tab(self):
        views = self._scan()["views"]

        index_symbols = {"NIFTY", "SENSEX"}
        for view_id in ("intraday", "eod", "rejections"):
            symbols = {row["symbol"] for row in views[view_id]["rows"]}
            self.assertEqual(symbols, {"NSE:A-EQ", "NSE:B-EQ"}, view_id)
            self.assertFalse(symbols & index_symbols, view_id)

        self.assertEqual(
            {row["symbol"] for row in views["indices"]["rows"]}, index_symbols)

    def test_the_indices_tab_carries_the_index_rejection_column(self):
        views = self._scan()["views"]
        keys = {c["key"] for c in views["indices"]["columns"]}

        # Index rejection is hidden from the stock intraday tab but is exactly
        # what the indices tab is for.
        self.assertIn("idx", keys)
        self.assertNotIn("idx",
                         {c["key"] for c in views["intraday"]["columns"]})

    def test_index_rows_are_tagged_as_indices(self):
        views = self._scan()["views"]
        for row in views["indices"]["rows"]:
            self.assertEqual(row["group"], "index")
        for row in views["intraday"]["rows"]:
            self.assertEqual(row["group"], "stock")

    def test_crossover_times_are_white(self):
        html = dashboard.INDEX_HTML
        self.assertIn("--when: #ffffff", html)
        self.assertRegex(
            html, r"\.sig \.when \{[^}]*color: var\(--when\)")
        # A signal that stopped forming stays recessed, but keeps the same
        # colour family so it still reads as "a time".
        self.assertRegex(
            html, r"\.sig\.earlier \.when \{[^}]*color: rgba\(255, 255, 255,")
        self.assertNotIn("--when: var(--muted)", html)
        self.assertNotIn("--when: #ffd60a", html)

    def test_a_single_group_tab_needs_no_group_band(self):
        views = self._scan()["views"]
        self.assertTrue(views["indices"]["single_group"])
        self.assertTrue(views["intraday"]["single_group"])

    def test_the_indices_tab_counts_only_the_indices(self):
        view = self._scan()["views"]["indices"]
        self.assertEqual(view["total_rows"], 2)
        # Freshness is judged over the indices alone, never mixed with the
        # 100 stocks, so the message cannot claim 102/102 symbols.
        self.assertNotIn("104", str(view["latest_message"]))


class SessionSweepTests(unittest.TestCase):
    """Every 15-minute bar of the session is judged, not just the newest."""

    @staticmethod
    def _at(epoch, o, h, l, c, v=1000):
        return SimpleNamespace(epoch=epoch, open=o, high=h, low=l, close=c, volume=v)

    def _session(self):
        """Three bars in the final session after one earlier session."""
        day1 = dt.datetime(2026, 1, 1, 9, 15, tzinfo=IST)
        day2 = dt.datetime(2026, 1, 2, 9, 15, tzinfo=IST)
        bars = [self._at(int((day1 + dt.timedelta(minutes=15 * i)).timestamp()),
                         10, 11, 9, 10)
                for i in range(3)]
        bars += [self._at(int((day2 + dt.timedelta(minutes=15 * i)).timestamp()),
                          20, 21, 19, 20)
                 for i in range(3)]
        return bars

    def test_bounds_cover_only_the_final_session(self):
        candles = self._session()
        start, stop = dashboard.session_bar_bounds(candles)
        self.assertEqual((start, stop), (3, 6))
        self.assertEqual(len(candles) - start, 3)

    def test_bounds_of_a_single_session_cover_everything(self):
        candles = self._session()[3:]
        self.assertEqual(dashboard.session_bar_bounds(candles), (0, 3))

    def test_bounds_of_no_candles(self):
        self.assertEqual(dashboard.session_bar_bounds([]), (0, 0))

    def test_only_15m_strategies_are_swept(self):
        swept = {s.key for s in dashboard.STRATEGY_SPECS
                 if dashboard.sweeps_session(s)}
        # 5-minute strategies and the daily breakout keep their latest-bar read.
        self.assertNotIn("orb", swept)
        self.assertNotIn("ema_10_20_30", swept)
        self.assertNotIn("daily_breakout", swept)
        for key in ("doji_rejection", "higher_high_rejection",
                    "higher_high_close_rejection", "r1_rejection",
                    "prev_high_rejection", "orb_high_rejection"):
            self.assertIn(key, swept)

    def test_a_signal_from_an_earlier_bar_is_still_reported(self):
        """The whole point: a 10:15 firing must survive until the close."""
        scanner = dashboard.DashboardScanner()
        spec = dashboard.STRATEGY_BY_KEY["higher_high_rejection"]
        module = scanner.registry.module_for(spec)
        candles = self._session()
        now = dt.datetime(2026, 1, 2, 10, 15, tzinfo=IST)

        # Bars 10:00 and 10:15 of the final session both make a higher high and
        # then close below the previous bar's low.
        def rising_then_failing(index):
            stamp = candles[index].epoch
            prev = candles[index - 1]
            return self._at(stamp, 20, prev.high + 1, prev.low - 1, prev.low - 0.5)

        candles[4] = rising_then_failing(4)
        candles[5] = rising_then_failing(5)

        only_last = scanner._evaluate(
            spec, module, "NSE:X-EQ", candles, [], [], now)
        self.assertEqual(len(only_last), 1,
                         "the newest bar alone should see one signal")

        cells = scanner._build_cells(
            [spec], {spec.key: module}, "NSE:X-EQ",
            candles, [], [], now, sweep_session=True)
        cell = cells[spec.key]

        self.assertEqual(cell["state"], "SELL")
        self.assertGreaterEqual(cell["hit_count"], 2,
                                "both firings in the session must be counted")
        # The final session runs 09:15, 09:30, 09:45. A cell is stamped with
        # its bar's close, so the 09:30 and 09:45 bars read 09:45 and 10:00.
        self.assertEqual(cell["times_ist"], ["09:45", "10:00"])
        # The newest firing bar is also the newest bar, so this is live.
        self.assertTrue(cell["on_latest_bar"])
        self.assertEqual(cell["time_ist"], "10:00")

    def test_a_signal_that_died_before_the_close_is_marked_earlier(self):
        scanner = dashboard.DashboardScanner()
        spec = dashboard.STRATEGY_BY_KEY["higher_high_rejection"]
        module = scanner.registry.module_for(spec)
        candles = self._session()
        now = dt.datetime(2026, 1, 2, 10, 15, tzinfo=IST)

        # Only the 09:30 bar fires; the 09:45 bar is quiet.
        prev = candles[3]
        candles[4] = self._at(candles[4].epoch, 20, prev.high + 1,
                              prev.low - 1, prev.low - 0.5)
        candles[5] = self._at(candles[5].epoch, 20, 21, 19.5, 20.5)

        cell = scanner._build_cells(
            [spec], {spec.key: module}, "NSE:X-EQ",
            candles, [], [], now, sweep_session=True)[spec.key]

        self.assertEqual(cell["state"], "SELL")
        self.assertEqual(cell["times_ist"], ["09:45"])
        self.assertFalse(cell["on_latest_bar"],
                         "a pattern that stopped forming must be marked earlier")
        self.assertEqual(cell["time_ist"], "09:45")

    def test_a_silent_session_reports_nothing(self):
        scanner = dashboard.DashboardScanner()
        spec = dashboard.STRATEGY_BY_KEY["higher_high_rejection"]
        module = scanner.registry.module_for(spec)
        candles = self._session()
        now = dt.datetime(2026, 1, 2, 10, 15, tzinfo=IST)

        cell = scanner._build_cells(
            [spec], {spec.key: module}, "NSE:X-EQ",
            candles, [], [], now, sweep_session=True)[spec.key]

        self.assertEqual(cell["state"], "-")
        self.assertEqual(cell["hit_count"], 0)
        self.assertEqual(cell["times_ist"], [])

    def test_a_one_bar_session_is_still_judged(self):
        """A short series must not silently lose its only signal."""
        scanner = dashboard.DashboardScanner()
        spec = dashboard.STRATEGY_BY_KEY["higher_high_rejection"]
        module = scanner.registry.module_for(spec)
        candles = self._session()[:1]
        now = dt.datetime(2026, 1, 1, 10, 0, tzinfo=IST)

        cell = scanner._build_cells(
            [spec], {spec.key: module}, "NSE:X-EQ",
            candles, [], [], now, sweep_session=True)[spec.key]

        # A single bar has no previous bar, so nothing can fire, but the cell
        # must be a well-formed empty one rather than missing.
        self.assertEqual(cell["state"], "-")
        self.assertIn("times_ist", cell)
        self.assertEqual(cell["hit_count"], 0)

    def test_every_cell_carries_the_session_fields(self):
        for cell in (dashboard.empty_cell(),
                     dashboard.error_cell("boom")):
            for field in ("times_ist", "hit_count", "on_latest_bar"):
                self.assertIn(field, cell)

    def test_merge_keeps_the_newest_firing_bar(self):
        sell = {"state": "SELL", "time_ist": "10:15", "note": "a", "price": 1.0}
        buy = {"state": "BUY", "time_ist": "11:00", "note": "b", "price": 2.0}
        merged = dashboard.merge_session_cells(
            [dashboard.empty_cell(), sell, dashboard.empty_cell(), buy])
        self.assertEqual(merged["state"], "BUY")
        self.assertEqual(merged["time_ist"], "11:00")
        self.assertEqual(merged["note"], "b")
        self.assertEqual(merged["times_ist"], ["10:15", "11:00"])
        self.assertEqual(merged["hit_count"], 2)
        # the newest bar itself fired, so this is still live
        self.assertTrue(merged["on_latest_bar"])

    def test_merge_marks_a_signal_that_died_before_the_close(self):
        sell = {"state": "SELL", "time_ist": "10:15", "note": "a", "price": 1.0}
        merged = dashboard.merge_session_cells(
            [sell, dashboard.empty_cell(), dashboard.empty_cell()])
        self.assertEqual(merged["state"], "SELL")
        self.assertEqual(merged["times_ist"], ["10:15"])
        self.assertFalse(merged["on_latest_bar"],
                         "the newest bar did not fire, so the pattern has stopped")

    def test_merge_deduplicates_repeated_times(self):
        hit = {"state": "SELL", "time_ist": "10:15", "note": "a", "price": 1.0}
        merged = dashboard.merge_session_cells([hit, dict(hit), hit])
        self.assertEqual(merged["times_ist"], ["10:15"])
        self.assertEqual(merged["hit_count"], 1)

    def test_the_page_explains_repeat_and_earlier_signals(self):
        html = dashboard.INDEX_HTML
        self.assertIn("cell.times_ist", html)
        self.assertIn("times.length > 1", html)
        self.assertIn("no longer forming", html)
        self.assertIn(".sig.earlier .badge", html)
        self.assertIn(".sig .more", html)


class SignalCellRenderTests(unittest.TestCase):
    """Check the markup a session-swept cell actually renders.

    Substring checks cannot tell whether the repeat count and the "earlier"
    marker reach the page, so cellHtml is called for real.
    """

    def _render(self, cell):
        node = ScriptSyntaxTests._node()
        if node is None:
            self.skipTest("node is not installed")
        driver = (
            "const markup = " + json.dumps(dashboard.INDEX_HTML) + ";\n"
            "const payload = {};\n"
            + SearchMountTests.SHIM
            + ScriptSyntaxTests._script()
            + "\nprocess.stdout.write(cellHtml("
            + json.dumps({"cells": {"orb": None}}) + ", "
            + json.dumps({"key": "orb", "label": "ORB"}) + ", "
            + json.dumps({"as_of_label": "Fri 25 Sep close"}) + "));\n"
        )
        # splice the cell under test into the row the driver renders
        driver = driver.replace(
            json.dumps({"cells": {"orb": None}}),
            json.dumps({"cells": {"orb": cell}}),
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "cell.js"
            path.write_text(driver, encoding="utf-8")
            result = subprocess.run([node, str(path)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0,
                         f"driver failed:\n{result.stdout}\n{result.stderr}")
        return result.stdout

    def test_a_repeat_signal_shows_the_extra_count_and_every_time(self):
        html = self._render({
            "state": "SELL", "time_ist": "13:30", "note": "higher high",
            "price": 101.5, "times_ist": ["10:15", "11:45", "13:30"],
            "hit_count": 3, "on_latest_bar": True,
        })
        self.assertIn("+2", html)
        self.assertIn("13:30", html)
        self.assertIn("fired 3 times today at 10:15, 11:45, 13:30", html)
        self.assertIn("badge sell", html)
        self.assertNotIn("sig earlier", html)

    def test_a_signal_off_the_latest_bar_is_marked_and_explained(self):
        html = self._render({
            "state": "BUY", "time_ist": "10:15", "note": "ema",
            "price": 99.0, "times_ist": ["10:15"], "hit_count": 1,
            "on_latest_bar": False,
        })
        self.assertIn("sig earlier", html)
        self.assertIn("no longer forming", html)
        self.assertIn("fired once today", html)
        self.assertNotIn("+", html.split("badge")[0].split("<td")[1])

    def test_a_single_live_signal_is_plain(self):
        html = self._render({
            "state": "SELL", "time_ist": "14:45", "note": "doji",
            "price": 50.0, "times_ist": ["14:45"], "hit_count": 1,
            "on_latest_bar": True,
        })
        self.assertIn("badge sell", html)
        self.assertIn("14:45", html)
        self.assertNotIn("sig earlier", html)
        self.assertNotIn("class=\"more\"", html)

    def test_a_cell_with_no_session_fields_still_renders(self):
        """Older payloads without the new fields must not break the table."""
        html = self._render({
            "state": "SELL", "time_ist": "10:00", "note": "old", "price": 1.0,
        })
        self.assertIn("badge sell", html)
        self.assertNotIn("sig earlier", html)
        self.assertNotIn("class=\"more\"", html)

    def test_a_neutral_cell_stays_a_dash(self):
        html = self._render({
            "state": "-", "time_ist": None, "note": "", "price": None,
            "times_ist": [], "hit_count": 0, "on_latest_bar": False,
        })
        self.assertIn('class="sig na"', html)
        self.assertNotIn("badge", html)


class OpenEmaStackColumnTests(unittest.TestCase):
    """The Open EMA stack column must reuse the signal module's own function."""

    @staticmethod
    def _scanner():
        return dashboard.DashboardScanner()

    def test_the_column_is_registered(self):
        spec = dashboard.STRATEGY_BY_KEY["open_ema_stack"]
        self.assertEqual(spec.column, "Open EMA stack")
        self.assertEqual(spec.kind, "open_ema_stack")
        self.assertEqual(spec.evaluator_name,
                         "open_candle_ema_stack_buy_signal")
        self.assertTrue(spec.module_path.exists(), spec.module_path)

    def test_its_sell_side_belongs_in_the_rejections_tab(self):
        """The column now produces a sell, so it joins the sell-only tab.

        This mirrors R1 and PrevHigh, which are flagged the same way despite
        also being able to report a buy.
        """
        spec = dashboard.STRATEGY_BY_KEY["open_ema_stack"]
        self.assertTrue(spec.sell_only)
        self.assertIn(spec.key, {s.key for s in dashboard.STRATEGY_SPECS
                                 if s.sell_only})
        # and its label must describe both directions
        self.assertIn("Sell:", spec.label)
        self.assertIn("ema10<ema20<ema50", spec.label)

    def test_the_column_calls_the_signal_module_function_itself(self):
        """Object identity, so the column cannot drift from the strategy."""
        scanner = self._scanner()
        spec = dashboard.STRATEGY_BY_KEY["open_ema_stack"]
        module = scanner.registry.module_for(spec)
        self.assertIs(scanner.registry.evaluator_for(spec),
                      module.open_candle_ema_stack_buy_signal)

    def test_it_is_not_swept_across_the_session(self):
        """Its verdict is set by the opening candles, so one judge is enough."""
        spec = dashboard.STRATEGY_BY_KEY["open_ema_stack"]
        self.assertTrue(spec.session_anchored)
        self.assertFalse(dashboard.sweeps_session(spec))

    def test_a_session_anchored_strategy_is_never_swept(self):
        for spec in dashboard.STRATEGY_SPECS:
            if spec.session_anchored:
                self.assertFalse(dashboard.sweeps_session(spec), spec.key)

    def test_it_needs_the_daily_bars_for_yesterdays_high(self):
        spec = dashboard.STRATEGY_BY_KEY["open_ema_stack"]
        self.assertTrue(spec.needs_daily)

    def test_the_column_appears_in_the_intraday_and_indices_tabs(self):
        specs = list(dashboard.STRATEGY_SPECS)
        intraday_keys = {s.key for s in specs if not s.hide_in_intraday}
        index_keys = intraday_keys | {s.key for s in specs
                                      if s.universe == "index_config"}
        self.assertIn("open_ema_stack", intraday_keys)
        self.assertIn("open_ema_stack", index_keys)
        # it is a 15-minute column, so the EOD tab keeps it too, where the
        # module itself refuses the daily bars it is handed
        self.assertNotIn("open_ema_stack",
                         {s.key for s in specs if s.hide_in_intraday})

    def test_evaluate_returns_a_buy_outcome_with_the_candle_label(self):
        scanner = self._scanner()
        spec = dashboard.STRATEGY_BY_KEY["open_ema_stack"]
        module = scanner.registry.module_for(spec)
        # daily bars standing in for intraday: the module must refuse them
        daily_only = [
            SimpleNamespace(epoch=1_767_225_000, open=1.0, high=2.0,
                            low=0.5, close=1.5, volume=1),
            SimpleNamespace(epoch=1_767_225_900, open=1.0, high=2.0,
                            low=0.5, close=1.5, volume=1),
        ]
        outcomes = scanner._evaluate(
            spec, module, "NSE:X-EQ", daily_only, [], daily_only,
            dt.datetime.fromtimestamp(daily_only[-1].epoch, IST))

        self.assertEqual(outcomes, [],
                         "daily bars must never produce this 15-minute signal")


class TotalGainTests(unittest.TestCase):
    """Total gain is the whole session's move, not the latest bar's."""

    @staticmethod
    def _bar(day, hour, minute, o, h, l, c, v=1000):
        return SimpleNamespace(
            epoch=int(dt.datetime(day.year, day.month, day.day, hour, minute,
                                  tzinfo=IST).timestamp()),
            open=o, high=h, low=l, close=c, volume=v)

    def _session(self):
        """Yesterday's session, then today's three bars."""
        prior = dt.date(2025, 12, 31)
        today = dt.date(2026, 1, 2)
        return [
            self._bar(prior, 9, 15, 50, 52, 49, 51),
            self._bar(prior, 9, 30, 51, 53, 50, 52),
            self._bar(today, 9, 15, 100, 101, 99, 100.5),
            self._bar(today, 9, 30, 105, 109, 104, 108),
            self._bar(today, 9, 45, 107, 108, 105, 106),
        ]

    def test_session_open_is_the_first_bar_of_the_final_session(self):
        # Not the first bar of the whole series, which opened at 50.
        self.assertEqual(dashboard.session_open(self._session()), 100.0)

    def test_gain_is_measured_from_the_session_open(self):
        scanner = dashboard.DashboardScanner()
        candles = self._session()
        cells = {"fake": dashboard.empty_cell()}
        freshness = {"latest_candle_ist": None, "age_seconds": 0}

        row = scanner._make_row(
            "NSE:X-EQ", "stock", candles, cells, freshness,
            base_open=dashboard.session_open(candles))

        # (106 - 100) / 100 = +6.00%, not the last bar's (106 - 107) / 107.
        self.assertEqual(row["total_gain_percent"], 6.0)
        self.assertAlmostEqual(
            (106 - 107) / 107 * 100, -0.93, places=2)

    def test_the_whole_series_open_would_be_wrongly_huge(self):
        """Guards against measuring from the first bar of all history."""
        scanner = dashboard.DashboardScanner()
        candles = self._session()
        row = scanner._make_row(
            "NSE:X-EQ", "stock", candles, {"fake": dashboard.empty_cell()},
            {"latest_candle_ist": None, "age_seconds": 0},
            base_open=float(candles[0].open))

        # (106 - 50) / 50 = +112%, which is not a day's move.
        self.assertEqual(row["total_gain_percent"], 112.0)

    def test_a_daily_row_falls_back_to_the_daily_bars_own_open(self):
        """A daily bar is already the whole day, so its own open is the base."""
        scanner = dashboard.DashboardScanner()
        daily = [self._bar(dt.date(2025, 12, 30), 9, 15, 90, 95, 89, 94),
                 self._bar(dt.date(2025, 12, 31), 9, 15, 94, 101, 93, 99)]
        row = scanner._make_row(
            "NSE:X-EQ", "stock", daily, {"fake": dashboard.empty_cell()},
            {"latest_candle_ist": None, "age_seconds": 0},
            base_open=dashboard.session_open(daily))

        self.assertEqual(row["total_gain_percent"], 5.32)  # (99-94)/94

    def test_a_zero_open_does_not_divide_by_zero(self):
        scanner = dashboard.DashboardScanner()
        candles = [self._bar(dt.date(2026, 1, 2), 9, 15, 0, 5, 0, 4),
                   self._bar(dt.date(2026, 1, 2), 9, 30, 4, 6, 3, 5)]
        row = scanner._make_row(
            "NSE:X-EQ", "stock", candles, {"fake": dashboard.empty_cell()},
            {"latest_candle_ist": None, "age_seconds": 0},
            base_open=dashboard.session_open(candles))

        # session_open returns None for a zero open, so the latest bar's own
        # open is used and the row still renders a number.
        self.assertIsNone(dashboard.session_open(candles))
        self.assertEqual(row["total_gain_percent"], 25.0)  # (5-4)/4

    def test_no_candles_means_no_session_open(self):
        self.assertIsNone(dashboard.session_open([]))

    def test_the_column_is_labelled_as_a_total(self):
        html = dashboard.INDEX_HTML
        self.assertIn("Total gain", html)
        self.assertIn("day open&rarr;close", html)
        self.assertNotIn(">Chg<", html)
        # the unit no longer varies per tab, so the old conditional is gone
        self.assertNotIn("chgUnit", html)
        self.assertIn("row.total_gain_percent", html)

    def test_the_gain_still_colours_by_direction(self):
        html = dashboard.INDEX_HTML
        self.assertIn("const gainClass = gain > 0 ? 'up' : (gain < 0 ? 'down' : '')",
                      html)


class SignalTimestampTests(unittest.TestCase):
    """A cell must show the close of the bar that actually produced it."""

    def test_a_signal_may_declare_its_own_bar_size(self):
        """The ORB column mixes 5-minute and 15-minute signals.

        Its spec says 5 minutes because of the breakout leg, so without an
        override every 15-minute signal in that column was stamped ten minutes
        early.
        """
        details = {"curr_time": "2026-01-02T09:30:00+05:30",
                   "bar_seconds": 900}
        self.assertEqual(
            dashboard.bar_close_time_ist(details, [], 300), "09:45")

    def test_the_spec_bar_size_is_the_fallback(self):
        details = {"curr_time": "2026-01-02T09:30:00+05:30"}
        self.assertEqual(
            dashboard.bar_close_time_ist(details, [], 300), "09:35")
        self.assertEqual(
            dashboard.bar_close_time_ist(details, [], 900), "09:45")

    def test_the_orb_column_tags_each_leg_with_its_bar_size(self):
        scanner = dashboard.DashboardScanner()
        spec = dashboard.STRATEGY_BY_KEY["orb"]
        # the spec itself declares 5 minutes
        self.assertEqual(spec.bar_seconds, dashboard.FIVE_MINUTE_SECONDS)

        class _FakeOrb:
            @staticmethod
            def calculate_orb_range(candles):
                return 110.0, 90.0

            @staticmethod
            def orb_breakout_signal(candles, high, low):
                return "CE", {"curr_time": "2026-01-02T09:30:00+05:30"}

            @staticmethod
            def orb_rejection_signal(candles, high, low):
                return "PE", {"curr_time": "2026-01-02T09:30:00+05:30"}

            @staticmethod
            def double_top_rejection_signal(candles):
                return "NONE", {}

            @staticmethod
            def double_bottom_rejection_signal(candles):
                return "NONE", {}

            @staticmethod
            def ema_crossover_rsi_signal(candles):
                return "NONE", {}

        outcomes = scanner._evaluate(
            spec, _FakeOrb, "NSE:X-EQ", [SimpleNamespace(epoch=1, open=1, high=1,
                                                          low=1, close=1, volume=1)],
            [], [], dt.datetime(2026, 1, 2, 15, 30, tzinfo=IST))
        by_side = {o[0]: o[1] for o in outcomes}
        self.assertEqual(by_side["CE"]["bar_seconds"],
                         dashboard.FIVE_MINUTE_SECONDS)
        self.assertEqual(by_side["PE"]["bar_seconds"], dashboard.CANDLE_SECONDS)

        cells = {
            side: dashboard.build_cell(spec, [(side, d, "ORB")], [], False)
            for side, d in by_side.items()
        }
        self.assertEqual(cells["CE"]["time_ist"], "09:35")   # 5-minute bar
        self.assertEqual(cells["PE"]["time_ist"], "09:45")   # 15-minute bar


class VolumeSortTests(unittest.TestCase):
    """Clicking a column header reorders the rows, and the order persists.

    The shim's parser drops text nodes, so row identity and values are read
    back out of the rendered HTML rather than from textContent.
    """

    #: Three symbols with distinct volumes, deliberately in an order that is
    #: neither ascending nor descending, so any reordering is unambiguous.
    VOLUME_PAYLOAD = {
        **SearchMountTests.PAYLOAD,
        "views": {
            "intraday": {
                **SearchMountTests.PAYLOAD["views"]["intraday"],
                "rows": [
                    {"symbol": "NSE:MID-EQ", "group": "stock", "close": 10.0,
                     "change_percent": 0.0, "volume_15m": 500, "signal_count": 0,
                     "cells": {"orb": {"state": "-", "time_ist": None,
                                       "note": "", "price": None}}},
                    {"symbol": "NSE:LOW-EQ", "group": "stock", "close": 10.0,
                     "change_percent": 0.0, "volume_15m": 100, "signal_count": 0,
                     "cells": {"orb": {"state": "-", "time_ist": None,
                                       "note": "", "price": None}}},
                    {"symbol": "NSE:HIGH-EQ", "group": "stock", "close": 10.0,
                     "change_percent": 0.0, "volume_15m": 900, "signal_count": 0,
                     "cells": {"orb": {"state": "-", "time_ist": None,
                                       "note": "", "price": None}}},
                ],
            }
        },
    }

    # Helpers injected into the driver: read the rendered table back. The
    # regexes use a dot for the quote character so nothing needs escaping
    # through the Python -> JavaScript string boundary.
    HELPERS = (
        "  const strip = s => s.replace(/<[^>]*>/g, '');\n"
        "  const syms = () => (box.innerHTML.match(/<span class=.sym.>[^<]*<\\/span>/g) || [])\n"
        "    .map(strip);\n"
        "  const nums = () => (box.innerHTML.match(/<td class=.num[^>]*>[^<]*<\\/td>/g) || [])\n"
        "    .map(s => Number(strip(s).replace(/[,+%]/g, '')));\n"
        "  const vols = () => { const n = nums(); const out = [];\n"
        "    for (let i = 2; i < n.length; i += 3) out.push(n[i]); return out; };\n"
    )

    def _driver(self, clicks):
        node = ScriptSyntaxTests._node()
        if node is None:
            self.skipTest("node is not installed")
        driver = (
            "const markup = " + json.dumps(dashboard.INDEX_HTML) + ";\n"
            "const payload = " + json.dumps(self.VOLUME_PAYLOAD) + ";\n"
            + SearchMountTests.SHIM
            + ScriptSyntaxTests._script()
            + "\n(async () => {\n"
            + "  for (let i = 0; i < 8; i++) await new Promise(r => setImmediate(r));\n"
            + "  const box = doc.getElementById('tablewrap');\n"
            + self.HELPERS
            + "  const out = {};\n"
            + "  out.sortable = box.querySelectorAll('th[data-sort]')\n"
            + "    .map(n => n.dataset.sort);\n"
            + "  out.before = syms(); out.beforeVolumes = vols();\n"
            + clicks
            + "  process.stdout.write(JSON.stringify(out));\n"
            + "})();\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "sort.js"
            path.write_text(driver, encoding="utf-8")
            result = subprocess.run([node, str(path)], capture_output=True,
                                    text=True)
        self.assertEqual(result.returncode, 0,
                         f"driver failed:\n{result.stdout}\n{result.stderr}")
        return json.loads(result.stdout)

    CLICK_VOLUME = (
        "  const vol = () => box.querySelector('th.col-vol');\n"
        "  vol().fire('click');\n"
        "  out.desc = syms(); out.descVolumes = vols();\n"
        "  vol().fire('click');\n"
        "  out.asc = syms(); out.ascVolumes = vols();\n"
    )

    def test_the_volume_header_is_a_sortable_link(self):
        out = self._driver("")
        self.assertIn("volume", out["sortable"])
        self.assertIn("gain", out["sortable"])
        # the data-sort key is the field, not the CSS class
        self.assertNotIn("col-vol", out["sortable"])

    def test_the_unsorted_payload_is_neither_way(self):
        out = self._driver("")
        self.assertEqual(out["before"], ["NSE:MID-EQ", "NSE:LOW-EQ",
                                         "NSE:HIGH-EQ"])
        self.assertEqual(out["beforeVolumes"], [500, 100, 900])

    def test_clicking_volume_sorts_descending_then_ascending(self):
        out = self._driver(self.CLICK_VOLUME)
        self.assertEqual(out["desc"], ["NSE:HIGH-EQ", "NSE:MID-EQ",
                                       "NSE:LOW-EQ"])
        self.assertEqual(out["descVolumes"], [900, 500, 100])
        self.assertEqual(out["asc"], ["NSE:LOW-EQ", "NSE:MID-EQ",
                                      "NSE:HIGH-EQ"])
        self.assertEqual(out["ascVolumes"], [100, 500, 900])

    def test_the_sort_survives_a_rerender(self):
        """Any re-render must keep the chosen column and direction.

        A filter chip is used to trigger one, because the table is rebuilt
        from scratch on every render and the header cell is replaced with it.
        """
        out = self._driver(
            self.CLICK_VOLUME
            + "  const chip = doc.querySelector('#filters')\n"
            + "    .querySelector('button[data-filter]');\n"
            + "  chip.fire('click');\n"
            + "  out.afterRerender = vols();\n"
            + "  out.ariaAfter = box.querySelector('th.col-vol')\n"
            + "    .attrs['aria-sort'];\n"
        )
        self.assertEqual(out["afterRerender"], out["ascVolumes"])
        self.assertEqual(out["ariaAfter"], "ascending")

    def test_the_sort_survives_a_search(self):
        out = self._driver(
            self.CLICK_VOLUME
            + "  const input = doc.getElementById('search');\n"
            + "  input.value = 'MID'; input.fire('input');\n"
            + "  out.afterSearch = syms();\n"
        )
        self.assertEqual(out["afterSearch"], ["NSE:MID-EQ"])

    def test_the_search_box_click_does_not_sort(self):
        out = self._driver(
            "  doc.getElementById('search').fire('click');\n"
            "  out.afterSearchClick = syms();\n"
        )
        self.assertEqual(out["afterSearchClick"], out["before"])

    def test_the_active_header_is_marked_and_carries_an_arrow(self):
        # The arrow is read from the wrapper, since innerHTML is only recorded
        # on the node it was assigned to and the th is built by innerHTML.
        out = self._driver(
            "  const vol = () => box.querySelector('th.col-vol');\n"
            "  vol().fire('click');\n"
            "  out.aria = vol().attrs['aria-sort'];\n"
            "  out.marked = vol().classList.contains('sorted');\n"
            "  out.arrowShown = box.innerHTML.indexOf('&darr;') >= 0;\n"
            "  vol().fire('click');\n"
            "  out.ariaAsc = vol().attrs['aria-sort'];\n"
            "  out.arrowAsc = box.innerHTML.indexOf('&uarr;') >= 0;\n"
            "  out.onlyOneArrow = (box.innerHTML.match(/class=.arrow./g) || []).length;\n"
        )
        self.assertEqual(out["aria"], "descending")
        self.assertEqual(out["ariaAsc"], "ascending")
        self.assertTrue(out["marked"])
        self.assertTrue(out["arrowShown"])
        self.assertTrue(out["arrowAsc"])
        # exactly one header is the active sort, so only one arrow is drawn
        self.assertEqual(out["onlyOneArrow"], 1)

    def test_the_payload_rows_are_never_reordered_in_place(self):
        """view.rows belongs to the cached result and must stay untouched."""
        out = self._driver(
            self.CLICK_VOLUME
            + "  out.payloadOrder = payload.views.intraday.rows\n"
            + "    .map(r => r.symbol);\n"
        )
        self.assertEqual(out["payloadOrder"],
                         ["NSE:MID-EQ", "NSE:LOW-EQ", "NSE:HIGH-EQ"])

    def test_sorting_by_gain_is_also_available(self):
        out = self._driver(
            "  const chg = () => box.querySelector('th.col-chg');\n"
            "  chg().fire('click');\n"
            "  out.gainSorted = chg().attrs['aria-sort'];\n"
        )
        self.assertEqual(out["gainSorted"], "descending")


class PriceFloorTests(unittest.TestCase):
    """Symbols at or below the price floor are not fetched and are purged."""

    class _QuoteClient:
        """Answers quotes from a fixed table, keyed by the FYERS symbol."""

        def __init__(self, prices, batch_limit=None):
            self.prices = prices
            self.batch_limit = batch_limit
            self.asked_batches = []

        def quotes(self, symbols):
            if self.batch_limit is not None and len(symbols) > self.batch_limit:
                raise ValueError("quotes: max 50 symbols per request")
            self.asked_batches.append(list(symbols))
            return {"s": "ok", "d": [
                {"n": symbol, "s": "ok",
                 "v": {"lp": self.prices.get(symbol)}}
                for symbol in symbols
            ]}

    def test_the_floor_is_one_hundred(self):
        self.assertEqual(dashboard.MIN_PRICE, 100.0)

    def test_prices_are_read_from_the_n_key_and_lp(self):
        client = self._QuoteClient({"NSE:A-EQ": 250.0, "NSE:B-EQ": 40.0})
        prices = dashboard.last_traded_prices(client, FakeLimiter(),
                                              ["NSE:A-EQ", "NSE:B-EQ"])
        # "n" is the symbol; "s" is the per-item status and must be ignored
        self.assertEqual(prices, {"NSE:A-EQ": 250.0, "NSE:B-EQ": 40.0})

    def test_requests_are_batched_at_fifty(self):
        symbols = [f"NSE:S{index}-EQ" for index in range(103)]
        client = self._QuoteClient({s: 500.0 for s in symbols}, batch_limit=50)
        dashboard.last_traded_prices(client, FakeLimiter(), symbols)
        self.assertEqual([len(b) for b in client.asked_batches], [50, 50, 3])

    def test_a_symbol_the_api_cannot_price_is_absent_not_zero(self):
        """BANKNIFTY answers with no last price; that must not read as cheap."""
        client = self._QuoteClient({"NSE:NIFTY50-INDEX": 23140.5})
        prices = dashboard.last_traded_prices(
            client, FakeLimiter(),
            ["NSE:NIFTY50-INDEX", "NSE:BANKNIFTY-INDEX"])
        self.assertIn("NSE:NIFTY50-INDEX", prices)
        self.assertNotIn("NSE:BANKNIFTY-INDEX", prices)

    def test_at_or_below_the_floor_is_excluded(self):
        targets = [("A", "NSE:A-EQ", "stock"), ("B", "NSE:B-EQ", "stock"),
                   ("C", "NSE:C-EQ", "stock")]
        prices = {"NSE:A-EQ": 100.01, "NSE:B-EQ": 100.0, "NSE:C-EQ": 99.99}
        kept, excluded = dashboard.apply_price_floor(targets, prices)
        self.assertEqual([t[0] for t in kept], ["A"])
        self.assertEqual([(e["symbol"], e["price"]) for e in excluded],
                         [("B", 100.0), ("C", 99.99)])

    def test_an_unpriced_symbol_is_kept(self):
        targets = [("A", "NSE:A-EQ", "stock"), ("X", "NSE:X-INDEX", "index")]
        kept, excluded = dashboard.apply_price_floor(targets, {})
        self.assertEqual([t[0] for t in kept], ["A", "X"])
        self.assertEqual(excluded, [])

    def test_a_failed_quotes_call_excludes_nothing(self):
        class _Broken:
            def quotes(self, symbols):
                raise RuntimeError("network down")

        prices = dashboard.last_traded_prices(_Broken(), FakeLimiter(),
                                              ["NSE:A-EQ"])
        self.assertEqual(prices, {})
        kept, excluded = dashboard.apply_price_floor(
            [("A", "NSE:A-EQ", "stock")], prices)
        self.assertEqual(len(kept), 1)
        self.assertEqual(excluded, [])

    def test_a_rejected_response_excludes_nothing(self):
        class _Rejected:
            def quotes(self, symbols):
                return {"s": "error", "message": "too many"}

        self.assertEqual(
            dashboard.last_traded_prices(_Rejected(), FakeLimiter(),
                                         ["NSE:A-EQ"]),
            {})

    def test_the_floor_applies_to_indices_too(self):
        targets = [("NIFTY", "NSE:NIFTY50-INDEX", "index")]
        kept, excluded = dashboard.apply_price_floor(
            targets, {"NSE:NIFTY50-INDEX": 23140.5})
        self.assertEqual(len(kept), 1)
        self.assertEqual(excluded, [])

    def test_purge_removes_every_stored_bar_for_a_symbol(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = dashboard.CandleStore(pathlib.Path(tmp) / "c.db")
            # Closed inside the block: on Windows an open handle would block
            # the temporary directory from being removed.
            try:
                candles = [
                    SimpleNamespace(epoch=1_700_000_000 + 900 * i, open=1.0,
                                    high=2.0, low=0.5, close=1.5, volume=10.0)
                    for i in range(5)
                ]
                store.store("NSE:KEEP-EQ", "15", candles)
                store.store("NSE:DROP-EQ", "15", candles)
                store.store("NSE:DROP-EQ", "D", candles)
                self.assertEqual(store.summary()["symbols"], 2)

                removed = store.purge_symbols(["NSE:DROP-EQ"])
                self.assertEqual(removed, 10)  # 5 bars at 15m plus 5 at D
                self.assertEqual(store.summary()["symbols"], 1)
                self.assertEqual(
                    store.connect().execute(
                        "select count(*) from candles "
                        "where symbol='NSE:KEEP-EQ'"
                    ).fetchone()[0], 5)
            finally:
                store.close()

    def test_purging_nothing_is_a_no_op(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = dashboard.CandleStore(pathlib.Path(tmp) / "c.db")
            try:
                self.assertEqual(store.purge_symbols([]), 0)
                self.assertEqual(store.purge_symbols(["NSE:ABSENT-EQ"]), 0)
            finally:
                store.close()

    def test_the_scan_reports_the_floor_and_its_exclusions(self):
        result = run_scan(["NSE:A-EQ"])
        report = result["price_filter"]
        self.assertEqual(report["min_price"], 100.0)
        for key in ("excluded", "excluded_count", "unpriced",
                    "unpriced_count", "purged_rows"):
            self.assertIn(key, report)
        # the fake client cannot price anything, so the one target is kept
        # rather than guessed at
        self.assertEqual(report["excluded_count"], 0)
        self.assertEqual(report["unpriced_count"], 1)
        self.assertEqual(result["universe"]["min_price"], 100.0)

    def test_the_floor_is_still_reported_but_not_printed(self):
        """The skipped symbols stay in the payload; the note is gone.

        The floor itself is unchanged and still reports which symbols it kept
        out, so the row count is explainable from the scan response. Only the
        printed block was removed, at the user's request.
        """
        html = dashboard.INDEX_HTML
        # Reported by the server...
        self.assertIn('"price_filter"', Path(MODULE_PATH).read_text("utf-8"))
        # ...but no longer rendered on the page.
        self.assertNotIn('id="priceFilterNote"', html)
        self.assertNotIn("function showPriceFilter(", html)


class MarkupTests(unittest.TestCase):
    def test_search_box_lives_in_the_stock_header_cell(self):
        """The search box is mounted into the Stock header, not the app bar."""
        html = dashboard.INDEX_HTML
        # The markup is parked in a hidden host and moved into the header.
        self.assertIn('id="search"', html)
        self.assertIn('id="searchwrap"', html)
        self.assertIn('id="searchHost" hidden', html)
        # render() must provide a slot inside the Stock header cell.
        self.assertIn('<span class="searchslot">', html)
        self.assertIn('class="left col-stock"', html)
        # The old centred band is gone.
        self.assertNotIn('class="brand"', html)
        self.assertNotIn("margin: 12px auto 0;", html)

    def test_search_node_is_moved_not_recreated(self):
        """Rebuilding the table must not destroy the box the user is typing in."""
        script = ScriptSyntaxTests._script()
        # The same node is re-parented, so its value and button state survive.
        self.assertIn("slot.appendChild(searchWrap)", script)
        # Focus and caret are captured before the rebuild and restored after.
        self.assertIn("const hadFocus = document.activeElement === searchInput;", script)
        self.assertIn("searchInput.setSelectionRange(caret, caret)", script)

    def test_header_is_rendered_even_with_no_matching_rows(self):
        """An empty result must not delete the only control that clears it."""
        script = ScriptSyntaxTests._script()
        self.assertNotIn("tableWrap.innerHTML = '<div class=\"empty\">' + esc(message)", script)
        # The markup, not the bare word: a comment mentioning the row class used
        # to satisfy this search ahead of the real markup and invert the order.
        empty_row = "'<tr class=\"empty-row\"><td colspan=\"'"
        header_row = "'<table><thead><tr>'"
        self.assertIn(empty_row, script)
        self.assertIn(header_row, script)
        # The message must come after the header is opened, not instead of it.
        self.assertLess(script.index(header_row), script.index(empty_row))

    def test_stats_tiles_are_stacked_in_a_right_hand_rail(self):
        html = dashboard.INDEX_HTML
        self.assertIn('<aside class="rail">', html)
        self.assertIn("grid-template-columns: minmax(0, 1fr) 188px", html)
        self.assertIn(".stats { display: flex; flex-direction: column;", html)
        # Every tile the user asked to align lives inside the rail.
        rail = html[html.index('<aside class="rail">'):html.index("</aside>")]
        for stat_id in ("statMarket", "statAsOf", "statScanned",
                        "statSignals", "statBuy", "statSell"):
            self.assertIn(f'id="{stat_id}"', rail, stat_id)

    def test_table_fills_the_height_instead_of_a_magic_number(self):
        html = dashboard.INDEX_HTML
        self.assertNotIn("calc(100vh - 320px)", html)
        self.assertIn(".tablewrap { flex: 1 1 auto; min-height: 260px;", html)

    def test_both_tabs_are_present_with_a_default(self):
        self.assertIn('data-view="intraday" class="active"', dashboard.INDEX_HTML)
        self.assertIn('data-view="eod"', dashboard.INDEX_HTML)
        self.assertIn('data-view="indices"', dashboard.INDEX_HTML)
        self.assertIn("let activeView = 'intraday'", dashboard.INDEX_HTML)

    def test_tab_click_switches_the_view_without_rescanning(self):
        self.assertIn("tabsBox.addEventListener('click'", dashboard.INDEX_HTML)
        self.assertIn("activeView = button.dataset.view", dashboard.INDEX_HTML)
        self.assertIn("rerender();", dashboard.INDEX_HTML)
        # switching a tab must never kick off a scan
        self.assertNotIn("button.dataset.view) refresh", dashboard.INDEX_HTML)

    def test_tabs_sit_in_their_own_row_above_the_filter_row(self):
        html = dashboard.INDEX_HTML
        tabrow = html.index('class="tabrow"')
        tabs = html.index('<div class="tabs" id="tabs">')
        toolbar = html.index('class="toolbar"')
        filters = html.index('id="filters"')
        refresh = html.index('id="refresh"')

        # order: tab strip, then the filter/refresh row
        self.assertLess(tabrow, tabs)
        self.assertLess(tabs, toolbar)
        self.assertLess(toolbar, filters)
        self.assertLess(filters, refresh)
        # the tab strip holds nothing but the four tabs
        strip = html[tabrow:toolbar]
        self.assertEqual(strip.count('<button'), 4)
        self.assertIn('data-view="intraday"', strip)
        self.assertIn('data-view="eod"', strip)
        self.assertIn('data-view="rejections"', strip)
        self.assertIn('data-view="indices"', strip)
        # and no filter/refresh control leaked into it
        self.assertNotIn('id="filters"', strip)
        self.assertNotIn('id="refresh"', strip)

    def test_a_missing_view_reports_instead_of_rendering_a_blank_table(self):
        self.assertIn('if (!view) {', dashboard.INDEX_HTML)
        self.assertIn('No <b>', dashboard.INDEX_HTML)
        self.assertIn("Available views:", dashboard.INDEX_HTML)

    def test_page_is_served_with_no_store_cache_headers(self):
        self.assertIn('"Cache-Control", "no-store, no-cache, must-revalidate"',
                      dashboard.INDEX_HTML + Path(MODULE_PATH).read_text(encoding="utf-8"))
        self.assertIn('self.send_header("Cache-Control", "no-store, max-age=0")',
                      Path(MODULE_PATH).read_text(encoding="utf-8"))

    def test_columns_headers_switch_units_per_tab(self):
        self.assertIn("const priceUnit = eodActive ? 'day close' : '15m close';",
                      dashboard.INDEX_HTML)

    def test_data_as_of_and_market_state_are_surfaced(self):
        for marker in ('id="statAsOf"', 'id="statMarket"',
                       'view.as_of_label', 'view.market_open'):
            self.assertIn(marker, dashboard.INDEX_HTML)

    def test_the_market_line_is_the_first_thing_under_the_controls(self):
        """"Market closed - showing ..." is read before the table.

        The four note blocks used to sit between the market line and the table,
        so the state of the data was the fifth thing on the page. They were
        removed at the user's request, which puts the market line directly
        above the table with nothing in between.
        """
        html = dashboard.INDEX_HTML
        status = html.index('class="status"')
        main = html.index("<main>")
        # It is inside <main>'s sibling order: above the table, not below it.
        self.assertLess(status, main, "the status bar must sit above <main>")
        for gone in ('id="asofNote"', 'id="universeNote"',
                     'id="priceFilterNote"', 'id="storageNote"'):
            self.assertNotIn(gone, html, gone)
        # And the market line is what render() puts in it.
        self.assertIn("view.latest_message", html)
        self.assertIn("function setStatus(message, kind)", html)

    def test_table_renders_a_cell_per_strategy_column(self):
        self.assertIn("columns.map(c => cellHtml(row, c, view)).join('')", dashboard.INDEX_HTML)
        self.assertIn("visibleRows(view)", dashboard.INDEX_HTML)

    def test_search_filters_rows_without_starting_a_scan(self):
        self.assertIn("searchInput.addEventListener('input'", dashboard.INDEX_HTML)
        self.assertNotIn("searchInput.addEventListener('input', () => refresh", dashboard.INDEX_HTML)

    def test_a_scan_runs_automatically_when_there_is_nothing_to_reuse(self):
        html = dashboard.INDEX_HTML
        self.assertIn("function maybeAutoScan()", html)
        self.assertIn("maybeAutoScan();", html)
        # forced, so the 5-minute refresh cooldown cannot leave the page blank
        self.assertIn("refresh(true);", html)

    def test_page_reuses_the_last_scan_instead_of_rescanning(self):
        html = dashboard.INDEX_HTML
        self.assertIn("fetch('/api/latest')", html)
        self.assertIn("async function bootstrap()", html)
        self.assertIn("bootstrap();", html)
        # the old localStorage guard stranded reloads on an empty table
        self.assertNotIn("AUTO_SCAN_GUARD_MS", html)
        self.assertNotIn("LAST_AUTO_SCAN_KEY", html)
        self.assertNotIn("Reloaded during the last scan", html)

    def test_scan_skips_the_5m_fetch_when_the_market_is_closed(self):
        # The only 5-minute consumer needs a bar from the current session.
        source = Path(MODULE_PATH).read_text(encoding="utf-8")
        self.assertIn("if not market_is_open(check_time):", source)
        self.assertIn("needs_5m = False", source)
        self.assertEqual(dashboard.INTRADAY_HISTORY_DAYS, 12)

    def test_page_is_never_blank_before_the_first_scan_finishes(self):
        html = dashboard.INDEX_HTML
        self.assertIn('id="tablewrap"><div class="empty">Starting the first scan',
                      html)
        self.assertIn('id="statusText">Starting the first scan', html)
        self.assertIn("this usually takes 60-90 seconds", html)


class StockCountTests(unittest.TestCase):
    """The Stock header reports how many symbols the active tab lists.

    A substring check cannot tell the rendered figure from a constant, so the
    page script is run for real and the rendered header is read back out of
    the table wrapper's HTML, as the shim drops text nodes.
    """

    #: Two stock rows and three index rows, so the two tabs must not read the
    #: same, and the noun has to change with them.
    PAYLOAD = {
        **SearchMountTests.PAYLOAD,
        "views": {
            "intraday": {**SearchMountTests.PAYLOAD["views"]["intraday"]},
            "indices": {
                **SearchMountTests.PAYLOAD["views"]["intraday"],
                "id": "indices", "title": "Indices",
                "columns": [{"key": "orb", "column": "ORB",
                             "label": "Opening range breakout"}],
                "rows": [
                    {"symbol": "NSE:NIFTY50-INDEX", "group": "index",
                     "close": 10.0, "total_gain_percent": 0.0,
                     "volume_15m": 1, "signal_count": 0,
                     "cells": {"orb": {"state": "-", "time_ist": None,
                                       "note": "", "price": None}}},
                ],
                "total_rows": 1, "single_group": True,
            },
        },
    }

    HELPERS = (
        "  const strip = s => s.replace(/<[^>]*>/g, '');\n"
        "  const count = () => {\n"
        "    const m = box.innerHTML.match(/<span class=\"col-count\"[^>]*>([^<]*)</);\n"
        "    return m ? m[1] : null; };\n"
        "  const countTitle = () => {\n"
        "    const m = box.innerHTML.match(/class=\"col-count\" title=\"([^\"]*)\"/);\n"
        "    return m ? m[1] : null; };\n"
    )

    def _driver(self, clicks=""):
        node = ScriptSyntaxTests._node()
        if node is None:
            self.skipTest("node is not installed")
        driver = (
            "const markup = " + json.dumps(dashboard.INDEX_HTML) + ";\n"
            "const payload = " + json.dumps(self.PAYLOAD) + ";\n"
            + SearchMountTests.SHIM
            + ScriptSyntaxTests._script()
            + "\n(async () => {\n"
            + "  for (let i = 0; i < 8; i++) await new Promise(r => setImmediate(r));\n"
            + "  const box = doc.getElementById('tablewrap');\n"
            + self.HELPERS
            + "  const out = {};\n"
            + "  out.stock = count(); out.stockTitle = countTitle();\n"
            + clicks
            + "  process.stdout.write(JSON.stringify(out));\n"
            + "})();\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "count.js"
            path.write_text(driver, encoding="utf-8")
            result = subprocess.run([node, str(path)], capture_output=True,
                                    text=True)
        self.assertEqual(result.returncode, 0,
                         f"driver failed:\n{result.stdout}\n{result.stderr}")
        return json.loads(result.stdout)

    def test_the_stock_header_shows_the_tab_row_count(self):
        out = self._driver("")
        self.assertEqual(out["stock"], "2")

    def test_the_count_is_grouped_for_locales(self):
        """en-IN grouping, so 1,234 rather than 1234, in the badge and its
        tooltip alike."""
        node = ScriptSyntaxTests._node()
        if node is None:
            self.skipTest("node is not installed")
        driver = (
            "const markup = " + json.dumps(dashboard.INDEX_HTML) + ";\n"
            "const payload = " + json.dumps({
                **SearchMountTests.PAYLOAD,
                "views": {"intraday": {
                    **SearchMountTests.PAYLOAD["views"]["intraday"],
                    "total_rows": 1234}},
            }) + ";\n"
            + SearchMountTests.SHIM
            + ScriptSyntaxTests._script()
            + "\n(async () => {\n"
            + "  for (let i = 0; i < 8; i++) await new Promise(r => setImmediate(r));\n"
            + "  const box = doc.getElementById('tablewrap');\n"
            + self.HELPERS
            + "  process.stdout.write(JSON.stringify([count(), countTitle()]));\n"
            + "})();\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "group.js"
            path.write_text(driver, encoding="utf-8")
            result = subprocess.run([node, str(path)], capture_output=True,
                                    text=True)
        self.assertEqual(result.returncode, 0,
                         f"driver failed:\n{result.stdout}\n{result.stderr}")
        badge, title = json.loads(result.stdout)
        self.assertEqual(badge, "1,234")
        self.assertEqual(title, "1,234 stocks listed in this tab")

    def test_the_count_title_says_stocks(self):
        out = self._driver("")
        self.assertIn("2 stocks", out["stockTitle"])
        self.assertIn("listed in this tab", out["stockTitle"])

    def test_a_search_does_not_shrink_the_count(self):
        """It is the tab's total, not the number of rows still on screen."""
        out = self._driver(
            "  const input = doc.getElementById('search');\n"
            "  input.value = 'RELIANCE';\n"
            "  input.fire('input');\n"
            "  out.afterSearch = count();\n"
            "  out.rowsLeft = box.innerHTML.match(/class=\"sym\"/g).length;\n"
        )
        self.assertEqual(out["afterSearch"], "2",
                         "the count reports the whole tab, not the matches")
        self.assertEqual(out["rowsLeft"], 1, "only one row should still show")

    def test_the_count_follows_the_active_tab(self):
        out = self._driver(
            "  const tab = doc.querySelector('button[data-view=\"indices\"]')\n"
            "    || Array.from(doc.querySelectorAll('button'))\n"
            "       .find(b => b.dataset.view === 'indices');\n"
            "  tab.fire('click');\n"
            "  out.indices = count(); out.indicesTitle = countTitle();\n"
        )
        self.assertEqual(out["indices"], "1")
        self.assertIn("1 index", out["indicesTitle"],
                      "the noun must be singular, and must say index")

    def test_the_count_survives_a_resort(self):
        out = self._driver(
            "  const vol = box.querySelector('th.col-vol');\n"
            "  vol.fire('click');\n"
            "  out.afterSort = count();\n"
        )
        self.assertEqual(out["afterSort"], "2")

    def test_the_count_badge_is_styled_and_titled(self):
        html = dashboard.INDEX_HTML
        self.assertIn("thead th .col-count {", html)
        # cursor: help, so hovering it explains the figure
        self.assertIn("cursor: help", html)
        # the label is a flex row so the badge sits beside the word
        self.assertIn(
            "thead th .col-label { display: flex; align-items: baseline;",
            html)

    def test_the_badge_only_uses_theme_variables_that_exist(self):
        """An undefined custom property silently drops the whole declaration,
        so the badge would lose its border with no error anywhere."""
        html = dashboard.INDEX_HTML
        defined = set(re.findall(r"(--[\w-]+)\s*:", html))
        start = html.index("thead th .col-count {")
        block = html[start:html.index("}", start)]
        used = set(re.findall(r"var\((--[\w-]+)\)", block))
        self.assertTrue(used, "the badge rule should use theme variables")
        self.assertEqual(used - defined, set(),
                         f"undefined custom properties: {used - defined}")

    def test_the_badge_sits_inside_the_stock_header(self):
        script = ScriptSyntaxTests._script()
        self.assertIn(
            '\'<th class="left col-stock"><span class="col-label">Stock\''
            " + countHtml +",
            script)
        self.assertIn("const rowCount = Number(view.total_rows || 0);", script)

    def test_the_count_reads_the_server_row_count_not_the_rendered_rows(self):
        """build_view must publish the count the header shows."""
        view = dashboard.build_view(
            "intraday", "Intraday",
            [{"symbol": "A", "group": "stock", "cells": {}}],
            {"message": "m", "market_open": True, "as_of_ist": None,
             "as_of_label": None, "note": "", "kind": "live"},
            True, 1, 1, [{"key": "orb", "column": "ORB", "label": "l"}],
        )
        self.assertEqual(view["total_rows"], 1)


class HeaderColourTests(unittest.TestCase):
    """The column header text is yellow, and stays yellow on hover.

    A colour change is invisible to a substring test that only proves the
    variable is *used*, so these assert the value and the cascade together.
    The hue assertions are what make the test survive a later colour change:
    they fail on any colour outside the yellow band rather than on one literal.
    """

    #: Yellow sits roughly between 40 and 70 degrees of hue. The header used to
    #: be the grey-blue at 217 and briefly a pink at 329, both well outside.
    YELLOW_BAND = (40, 70)

    @staticmethod
    def _rule(selector):
        html = dashboard.INDEX_HTML
        start = html.index(selector)
        body = html[html.index("{", start) + 1:]
        return body[:body.index("}")]

    @staticmethod
    def _hue(hex_colour):
        red, green, blue = (int(hex_colour[i:i + 2], 16) for i in (1, 3, 5))
        high, low = max(red, green, blue), min(red, green, blue)
        delta = high - low
        if delta == 0:
            return 0.0
        if high == red:
            hue = 60 * (((green - blue) / delta) % 6)
        elif high == green:
            hue = 60 * (((blue - red) / delta) + 2)
        else:
            hue = 60 * (((red - green) / delta) + 4)
        return hue % 360

    def _variable(self, name):
        match = re.search(r"--" + name + r":\s*(#[0-9a-fA-F]{6});",
                          dashboard.INDEX_HTML)
        self.assertIsNotNone(match, f"no --{name} colour defined")
        return match.group(1)

    def _assert_yellow(self, colour, what):
        hue = self._hue(colour)
        low, high = self.YELLOW_BAND
        self.assertTrue(low <= hue <= high,
                        f"{what} is {colour} at hue {hue:.0f}, outside the "
                        f"yellow band {low}-{high}")

    def test_the_header_font_is_the_header_variable(self):
        rule = self._rule("thead th {")
        self.assertIn("color: var(--header);", rule)
        self.assertNotIn("color: var(--muted);", rule)

    def test_the_header_variable_is_yellow(self):
        self._assert_yellow(self._variable("header"), "--header")

    def test_the_hover_variable_is_yellow(self):
        """Not merely lighter: a pale pink hover must fail this too."""
        self._assert_yellow(self._variable("header-hover"), "--header-hover")

    def test_hover_stays_a_lighter_shade_of_the_same_hue(self):
        """A hover that drifts to another hue reads as a different colour."""
        base = self._hue(self._variable("header"))
        hover = self._hue(self._variable("header-hover"))
        self.assertLess(abs(base - hover), 20,
                        f"hover hue {hover:.0f} is far from base {base:.0f}")
        base_rgb = self._variable("header")[1:]
        hover_rgb = self._variable("header-hover")[1:]
        for name, low, high in (("red", 0, 1), ("green", 2, 3), ("blue", 4, 5)):
            with self.subTest(channel=name):
                self.assertGreaterEqual(
                    int(hover_rgb[low:high], 16), int(base_rgb[low:high], 16),
                    f"hover {name} should not be darker than the base")

    def test_hover_rule_uses_the_hover_variable(self):
        self.assertIn("color: var(--header-hover);",
                      self._rule("thead th.sortable:hover {"))

    def test_the_active_sort_is_still_distinguishable(self):
        """All-yellow headers would leave the sorted column unmarked."""
        self.assertIn("color: var(--accent);",
                      self._rule("thead th.sortable.sorted {"))
        # the accent is blue, so it cannot be mistaken for the header yellow
        self._assert_yellow(self._variable("header"), "--header")
        accent = self._hue("#4f8cff")
        low, high = self.YELLOW_BAND
        self.assertFalse(low <= accent <= high,
                         "the sort accent must stay out of the yellow band")

    def test_the_stock_count_pill_follows_the_header(self):
        self.assertIn("color: var(--header);", self._rule("thead th .col-count {"))

    def test_every_new_variable_is_defined(self):
        html = dashboard.INDEX_HTML
        defined = set(re.findall(r"(--[\w-]+)\s*:", html))
        for selector in ("thead th {", "thead th.sortable:hover {",
                         "thead th .col-count {"):
            used = set(re.findall(r"var\((--[\w-]+)\)", self._rule(selector)))
            self.assertEqual(used - defined, set(),
                             f"{selector} uses undefined {used - defined}")


if __name__ == "__main__":
    unittest.main()
