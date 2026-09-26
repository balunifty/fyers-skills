"""Tests for the lower-high / lower-close sell signal.

Both halves of the rule are strict, and each is checked on its own so removing
one cannot go unnoticed.
"""
import datetime as dt
import importlib.util
import pathlib
import sys
import unittest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
MODULE_PATH = (REPO_ROOT / "strategies" / "scripts" /
               "EquityLowerHighCloseSignal15min.py")
SPEC = importlib.util.spec_from_file_location(
    "EquityLowerHighCloseSignal15min", MODULE_PATH)
assert SPEC and SPEC.loader
lh = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = lh
SPEC.loader.exec_module(lh)

IST = lh.MARKET_TIMEZONE
BASE = dt.datetime(2026, 1, 2, 9, 15, tzinfo=IST)


def bar(index, o, h, l, c):
    return type("Candle", (), {
        "epoch": int((BASE + dt.timedelta(minutes=15 * index)).timestamp()),
        "open": float(o), "high": float(h), "low": float(l),
        "close": float(c), "volume": 1000.0,
    })()


def pair(previous, current, filler=0):
    """Two judged bars, optionally preceded by filler bars."""
    return [bar(i, 100, 101, 99, 100) for i in range(filler)] + [
        bar(filler, *previous), bar(filler + 1, *current)]


class HappyPathTests(unittest.TestCase):
    def test_a_lower_high_and_lower_close_sells(self):
        # previous high 110, this bar's high 108; previous close 105, this 103
        signal, details = lh.lower_high_close_sell_signal(
            pair((100, 110, 95, 105), (100, 108, 97, 103)))
        self.assertEqual(signal, "PE")
        self.assertTrue(details["lower_high"])
        self.assertTrue(details["closed_below_previous"])
        self.assertEqual(details["rule"], "lower_high_close")
        self.assertIn("lower high", details["reason"])

    def test_the_details_report_both_bars(self):
        _, details = lh.lower_high_close_sell_signal(
            pair((100, 110, 95, 105), (100, 108, 97, 103)))
        self.assertEqual(details["previous_high"], 110.0)
        self.assertEqual(details["high"], 108.0)
        self.assertEqual(details["previous_close"], 105.0)
        self.assertEqual(details["close"], 103.0)
        self.assertEqual(details["curr_close"], details["close"])
        self.assertEqual(details["previous_time"][11:16], "09:15")
        self.assertEqual(details["curr_time"][11:16], "09:30")

    def test_the_cell_is_stamped_with_the_judged_bar(self):
        _, details = lh.lower_high_close_sell_signal(
            pair((100, 110, 95, 105), (100, 108, 97, 103)))
        self.assertEqual(details["bar_seconds"], 900)
        self.assertTrue(details["curr_time"].startswith("2026-01-02T09:30"))

    def test_only_the_last_two_bars_are_judged(self):
        """A bar earlier in the series cannot produce the signal."""
        candles = [
            bar(0, 100, 120, 95, 118),    # a big spike, long since past
            bar(1, 118, 119, 100, 101),   # lower high and lower close
            bar(2, 101, 130, 100, 129),   # a new high: must not judge bar 1
        ]
        signal, details = lh.lower_high_close_sell_signal(candles)
        self.assertEqual(signal, "NONE")
        self.assertFalse(details["lower_high"])
        # truncating to bar 1 surfaces the earlier firing
        self.assertEqual(
            lh.lower_high_close_sell_signal(candles[:2])[0], "PE")

    def test_the_bar_seconds_can_be_overridden(self):
        _, details = lh.lower_high_close_sell_signal(
            pair((100, 110, 95, 105), (100, 108, 97, 103)), bar_seconds=300)
        self.assertEqual(details["bar_seconds"], 300)

    def test_an_unordered_series_is_sorted_first(self):
        candles = pair((100, 110, 95, 105), (100, 108, 97, 103))
        self.assertEqual(
            lh.lower_high_close_sell_signal(list(reversed(candles)))[0], "PE")


class RuleTests(unittest.TestCase):
    def test_only_a_lower_high_is_not_enough(self):
        """A lower high that closes higher is not a sell."""
        signal, details = lh.lower_high_close_sell_signal(
            pair((100, 110, 95, 100), (100, 108, 99, 106)))
        self.assertTrue(details["lower_high"])
        self.assertFalse(details["closed_below_previous"])
        self.assertEqual(signal, "NONE")
        self.assertIn("did not fall below", details["reason"])

    def test_only_a_lower_close_is_not_enough(self):
        """A lower close that takes a higher high is not a sell."""
        signal, details = lh.lower_high_close_sell_signal(
            pair((100, 105, 95, 100), (100, 110, 94, 99)))
        self.assertFalse(details["lower_high"])
        self.assertTrue(details["closed_below_previous"])
        self.assertEqual(signal, "NONE")
        self.assertIn("is not below the previous bar", details["reason"])

    def test_a_matching_high_is_not_lower(self):
        """'lower' is strict, so an equal high must not count."""
        signal, details = lh.lower_high_close_sell_signal(
            pair((100, 110, 95, 105), (100, 110, 97, 103)))
        self.assertEqual(details["previous_high"], 110.0)
        self.assertEqual(details["high"], 110.0)
        self.assertFalse(details["lower_high"])
        self.assertEqual(signal, "NONE")

    def test_a_matching_close_is_not_lower(self):
        """'below' is strict, so an equal close must not count."""
        signal, details = lh.lower_high_close_sell_signal(
            pair((100, 110, 95, 105), (100, 108, 97, 105)))
        self.assertEqual(details["previous_close"], 105.0)
        self.assertEqual(details["close"], 105.0)
        self.assertFalse(details["closed_below_previous"])
        self.assertEqual(signal, "NONE")

    def test_a_higher_high_and_higher_close_does_not_sell(self):
        signal, _ = lh.lower_high_close_sell_signal(
            pair((100, 105, 95, 100), (100, 110, 99, 108)))
        self.assertEqual(signal, "NONE")

    def test_a_doji_bar_does_not_sell(self):
        """A flat bar closes at its open and can still be a lower high."""
        signal, details = lh.lower_high_close_sell_signal(
            pair((100, 110, 95, 105), (105, 108, 104, 105)))
        self.assertEqual(details["close"], details["open"])
        self.assertEqual(signal, "NONE")

    def test_a_flat_series_never_sells(self):
        signal, details = lh.lower_high_close_sell_signal(
            [bar(i, 100, 100, 100, 100) for i in range(5)])
        self.assertEqual(signal, "NONE")
        self.assertFalse(details["lower_high"])
        self.assertFalse(details["closed_below_previous"])


class GuardTests(unittest.TestCase):
    def test_no_candles(self):
        signal, details = lh.lower_high_close_sell_signal([])
        self.assertEqual(signal, "NONE")
        self.assertIn("at least 2 candles", details["reason"])
        self.assertIsNone(details["rule"])

    def test_a_single_candle(self):
        signal, details = lh.lower_high_close_sell_signal([bar(0, 1, 2, 0, 1)])
        self.assertEqual(signal, "NONE")
        self.assertIn("at least 2 candles", details["reason"])

    def test_every_return_carries_a_rule_key(self):
        for candles in ([], [bar(0, 1, 2, 0, 1)],
                        pair((100, 110, 95, 105), (100, 108, 97, 103))):
            signal, details = lh.lower_high_close_sell_signal(candles)
            self.assertIn(signal, ("PE", "NONE"))
            self.assertIn("rule", details, len(candles))
            self.assertIn("reason", details, len(candles))


class DashboardColumnTests(unittest.TestCase):
    """The LH column must call the signal module and reach the indices tab."""

    @staticmethod
    def _dashboard():
        spec = importlib.util.spec_from_file_location(
            "lh_dashboard",
            REPO_ROOT / "strategies" / "ui" / "equity_strategy_dashboard.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        return module

    def test_the_column_is_registered(self):
        d = self._dashboard()
        spec = d.STRATEGY_BY_KEY["lower_high_close"]
        self.assertEqual(spec.column, "LH")
        self.assertEqual(spec.kind, "lower_high")
        self.assertEqual(spec.evaluator_name, "lower_high_close_sell_signal")
        self.assertTrue(spec.module_path.exists())

    def test_the_column_calls_the_module_function_itself(self):
        d = self._dashboard()
        scanner = d.DashboardScanner()
        spec = d.STRATEGY_BY_KEY["lower_high_close"]
        module = scanner.registry.module_for(spec)
        self.assertIs(scanner.registry.evaluator_for(spec),
                      module.lower_high_close_sell_signal)

    def test_it_is_a_rejection_so_its_sell_reaches_the_rejections_tab(self):
        d = self._dashboard()
        spec = d.STRATEGY_BY_KEY["lower_high_close"]
        self.assertTrue(spec.sell_only)
        self.assertIn(spec.key, {s.key for s in d.STRATEGY_SPECS
                                 if s.sell_only})

    def test_evaluate_returns_a_sell_outcome(self):
        d = self._dashboard()
        scanner = d.DashboardScanner()
        spec = d.STRATEGY_BY_KEY["lower_high_close"]
        module = scanner.registry.module_for(spec)
        candles = pair((100, 110, 95, 105), (100, 108, 97, 103))
        now = dt.datetime(2026, 1, 2, 15, 30, tzinfo=IST)

        outcomes = scanner._evaluate(spec, module, "NSE:X-EQ",
                                     candles, [], [], now)
        self.assertEqual(len(outcomes), 1)
        self.assertEqual(outcomes[0][0], "PE")
        cell = d.build_cell(spec, outcomes, candles, False)
        self.assertEqual(cell["state"], "SELL")
        self.assertEqual(cell["time_ist"], "09:45")

    def test_evaluate_is_silent_on_a_rising_bar(self):
        d = self._dashboard()
        scanner = d.DashboardScanner()
        spec = d.STRATEGY_BY_KEY["lower_high_close"]
        module = scanner.registry.module_for(spec)
        candles = pair((100, 105, 95, 100), (100, 110, 99, 108))
        now = dt.datetime(2026, 1, 2, 15, 30, tzinfo=IST)
        self.assertEqual(
            scanner._evaluate(spec, module, "NSE:X-EQ", candles, [], [],
                              now), [])

    def test_the_column_judges_every_bar_of_a_session(self):
        d = self._dashboard()
        spec = d.STRATEGY_BY_KEY["lower_high_close"]
        self.assertTrue(d.sweeps_session(spec))

    def test_indices_are_judged_by_the_new_columns_too(self):
        d = self._dashboard()
        scanner = d.DashboardScanner()
        specs, _modules, errors = scanner._resolve_specs(list(d.STRATEGY_SPECS))
        self.assertEqual(errors, [])
        index_keys = {s.key for s in scanner._specs_for_group(specs, "index")}
        for key in ("lower_high_close", "open_ema_stack", "ema_10_cross_20",
                    "index_rejection"):
            self.assertIn(key, index_keys, key)


if __name__ == "__main__":
    unittest.main()
