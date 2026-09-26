"""Tests for the ORB Low Rejection column.

The rule is a bullish rejection of the opening-range low, gated on the session
having opened with a bullish candle. The gate is the interesting part: without
it the rule is just the ORB rejection's existing bullish branch, so the tests
spend most of their effort on proving the gate actually blocks.

A second theme is the degenerate case. The 09:15 candle *defines* the opening
range, so it trivially satisfies "low tested, close back above it". Left
unchecked it would fire on every bullish session, so the rule requires a later
bar to judge.
"""
import contextlib
import datetime as dt
import importlib.util
import io
import pathlib
import sys
import unittest
from types import SimpleNamespace

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
MODULE_PATH = (REPO_ROOT / "strategies" / "scripts" /
               "EquityOrbLowRejectionSignal15min.py")
SPEC = importlib.util.spec_from_file_location(
    "EquityOrbLowRejectionSignal15min", MODULE_PATH)
assert SPEC and SPEC.loader
orb_low = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = orb_low
SPEC.loader.exec_module(orb_low)

DASH_SPEC = importlib.util.spec_from_file_location(
    "orb_low_rej_dashboard",
    REPO_ROOT / "strategies" / "ui" / "equity_strategy_dashboard.py")
dashboard = importlib.util.module_from_spec(DASH_SPEC)
sys.modules[DASH_SPEC.name] = dashboard
with contextlib.redirect_stdout(io.StringIO()):
    DASH_SPEC.loader.exec_module(dashboard)

IST = orb_low.MARKET_TIMEZONE
DAY = dt.date(2026, 1, 2)
NOW = dt.datetime(2026, 1, 2, 15, 30, tzinfo=IST)


def at(hour, minute, o, h, low, c, day=DAY):
    stamp = dt.datetime.combine(day, dt.time(hour, minute), tzinfo=IST)
    return SimpleNamespace(epoch=int(stamp.timestamp()), open=float(o),
                           high=float(h), low=float(low), close=float(c),
                           volume=1000.0)


def evaluate(candles, now=NOW, restrict=False):
    return orb_low.orb_low_rejection_buy_signal(candles, now, restrict)


def bullish_open(hour=9, minute=15, o=100.0, h=106.0, low=98.0, c=105.0,
                 day=DAY):
    return at(hour, minute, o, h, low, c, day=day)


class RuleTests(unittest.TestCase):
    def test_a_bullish_open_and_a_recovered_low_is_a_buy(self):
        candles = [
            bullish_open(),                 # 9:15 bullish, range low 98
            at(9, 30, 105, 107, 97, 106),    # dips to 97, closes 106
        ]
        signal, details = evaluate(candles)
        self.assertEqual(signal, "CE")
        self.assertEqual(details["trigger"], "ORB_LOW_REJECTION")
        self.assertTrue(details["opening_bullish"])
        self.assertTrue(details["low_tested"])
        self.assertTrue(details["closed_back_above"])
        self.assertTrue(details["judged_bullish"])
        self.assertEqual(details["orb_low"], 98.0)

    def test_a_bearish_open_blocks_the_buy(self):
        """The gate: the same rejection under a bearish open is not a buy."""
        candles = [
            bullish_open(o=100.0, h=104.0, low=96.0, c=97.0),   # bearish
            at(9, 30, 97, 99, 95, 98),                          # rejects 96
        ]
        signal, details = evaluate(candles)
        self.assertEqual(signal, "NONE")
        self.assertFalse(details["opening_bullish"])
        self.assertIn("opening candle is not bullish", details["reason"])

    def test_a_doji_open_blocks_the_buy(self):
        """A doji is not bullish, so the gate holds."""
        candles = [
            bullish_open(o=100.0, h=106.0, low=98.0, c=100.0),
            at(9, 30, 100, 104, 97, 103),
        ]
        self.assertEqual(evaluate(candles)[0], "NONE")

    def test_a_low_above_the_range_does_not_test_it(self):
        candles = [
            bullish_open(low=98.0),
            at(9, 30, 105, 108, 100, 107),   # low 100 never reached 98
        ]
        signal, details = evaluate(candles)
        self.assertEqual(signal, "NONE")
        self.assertFalse(details["low_tested"])
        self.assertIn("never reached", details["reason"])

    def test_a_low_touching_the_range_exactly_still_tests_it(self):
        """The test is <=, so a low printed exactly at the level counts."""
        candles = [
            bullish_open(low=98.0),
            at(9, 30, 105, 108, 98.0, 107),
        ]
        signal, details = evaluate(candles)
        self.assertEqual(signal, "CE")
        self.assertTrue(details["low_tested"])

    def test_a_close_exactly_at_the_range_low_does_not_recover(self):
        """The recovery is strict, so closing on the level is not a recovery."""
        candles = [
            bullish_open(low=98.0),
            at(9, 30, 105, 108, 97, 98.0),
        ]
        signal, details = evaluate(candles)
        self.assertEqual(signal, "NONE")
        self.assertFalse(details["closed_back_above"])
        self.assertIn("did not get back above", details["reason"])

    def test_a_bearish_judged_candle_is_not_a_buy(self):
        """It may reject the low and still close under its own open."""
        candles = [
            bullish_open(low=98.0),
            at(9, 30, 106, 107, 97, 100),    # rejected 98, but closes below 106
        ]
        signal, details = evaluate(candles)
        self.assertEqual(signal, "NONE")
        self.assertTrue(details["low_tested"], "precondition: it did reject")
        self.assertTrue(details["closed_back_above"],
                        "precondition: it did close back above")
        self.assertFalse(details["judged_bullish"])
        self.assertIn("not bullish", details["reason"])

    def test_each_condition_is_reported(self):
        candles = [bullish_open(low=98.0), at(9, 30, 105, 108, 99, 107)]
        _, details = evaluate(candles)
        for key in ("opening_bullish", "low_tested", "closed_back_above",
                    "judged_bullish", "orb_low", "opening_open",
                    "opening_close", "opening_low", "curr_open", "curr_high",
                    "curr_low", "curr_close", "curr_time", "opening_time",
                    "session_day", "session_bars"):
            self.assertIn(key, details)


class DegenerateInputTests(unittest.TestCase):
    def test_the_opening_candle_alone_never_fires(self):
        """It defines the range, so it trivially 'rejects' it.

        Without this the column would show a BUY on every bullish session.
        A single bar is refused on the count check first.
        """
        signal, details = evaluate([bullish_open()])
        self.assertEqual(signal, "NONE")
        self.assertIn("need at least 2", details["reason"])

    def test_a_bearish_opening_candle_alone_never_fires(self):
        signal, details = evaluate([bullish_open(c=97.0)])
        self.assertEqual(signal, "NONE")
        self.assertIn("need at least 2", details["reason"])

    def test_duplicate_opening_candles_never_fire(self):
        """Two bars stamped 09:15 is degenerate input.

        It reaches the check that the judged bar is the opening bar, which a
        well-formed series can never trip: the newest bar of a session is
        always later than 09:15.
        """
        candles = [bullish_open(), bullish_open(c=99.0, low=99.5)]
        signal, details = evaluate(candles)
        self.assertEqual(signal, "NONE")
        self.assertIn("defines the opening range", details["reason"])

    def test_no_candles(self):
        signal, details = evaluate([])
        self.assertEqual(signal, "NONE")
        self.assertEqual(details["session_bars"], 0)

    def test_a_daily_series_cannot_fire(self):
        """One bar per session, so the newest session holds a single bar and
        there is nothing to judge against the opening range."""
        daily = [at(15, 30, 100, 106, 98, 105, day=DAY),
                 at(15, 30, 105, 107, 97, 106, day=DAY + dt.timedelta(days=1))]
        signal, details = evaluate(daily)
        self.assertEqual(signal, "NONE")
        self.assertEqual(details["session_bars"], 1)
        self.assertIn("need at least 2", details["reason"])

    def test_a_missing_opening_candle_is_reported(self):
        candles = [at(9, 30, 105, 107, 99, 106), at(9, 45, 106, 108, 100, 107)]
        signal, details = evaluate(candles)
        self.assertEqual(signal, "NONE")
        self.assertIn("no 09:15 candle", details["reason"])

    def test_a_gap_in_the_opening_bars_is_rejected(self):
        """A series whose first two bars are hours apart is not 15-minute."""
        candles = [bullish_open(), at(11, 0, 105, 107, 99, 106)]
        signal, details = evaluate(candles)
        self.assertEqual(signal, "NONE")
        self.assertIn("not 15-minute bars", details["reason"])

    def test_unsorted_candles_are_ordered_first(self):
        candles = [at(9, 30, 105, 107, 97, 106), bullish_open()]
        self.assertEqual(evaluate(candles)[0], "CE",
                         "the series must be sorted before use")

    def test_only_the_newest_session_is_judged(self):
        """Yesterday's bars must not satisfy today's rule."""
        yesterday = DAY - dt.timedelta(days=1)
        candles = [bullish_open(day=yesterday),
                   at(9, 30, 105, 107, 97, 106, day=yesterday),
                   at(9, 15, 200, 210, 198, 205),   # today's open, far away
                   at(9, 30, 205, 207, 199, 206)]
        signal, details = evaluate(candles)
        self.assertEqual(details["session_day"], DAY.isoformat())
        self.assertEqual(details["session_bars"], 2)
        self.assertEqual(details["orb_low"], 198.0)
        self.assertEqual(signal, "NONE",
                         "today's low 199 cleared today's range low 198")


class SessionSelectionTests(unittest.TestCase):
    def test_it_uses_the_newest_bars_session_by_default(self):
        """A holiday still reports the last session's setup."""
        last_session = DAY - dt.timedelta(days=3)
        saturday = dt.datetime(2026, 1, 3, 10, 0, tzinfo=IST)
        candles = [bullish_open(day=last_session),
                   at(9, 30, 105, 107, 97, 106, day=last_session)]
        signal, details = evaluate(candles, now=saturday)
        self.assertEqual(signal, "CE")
        self.assertEqual(details["session_day"], last_session.isoformat())

    def test_restrict_to_today_keeps_a_stale_session_quiet(self):
        """Live trading wants only the current session, so a day-old setup is
        not a signal."""
        last_session = DAY - dt.timedelta(days=3)
        saturday = dt.datetime(2026, 1, 3, 10, 0, tzinfo=IST)
        candles = [bullish_open(day=last_session),
                   at(9, 30, 105, 107, 97, 106, day=last_session)]
        self.assertEqual(evaluate(candles, now=saturday, restrict=True)[0],
                         "NONE")

    def test_restrict_to_today_accepts_the_current_session(self):
        candles = [bullish_open(), at(9, 30, 105, 107, 97, 106)]
        self.assertEqual(evaluate(candles, now=NOW, restrict=True)[0], "CE")


class OpeningCandleLookupTests(unittest.TestCase):
    def test_it_finds_the_opening_candle_among_later_ones(self):
        bars = [at(9, 15, 100, 106, 98, 105), at(9, 30, 105, 107, 99, 106),
                at(9, 45, 106, 108, 100, 107)]
        found = orb_low.opening_candle(bars)
        self.assertEqual(float(found.low), 98.0)

    def test_it_returns_none_when_there_is_no_opening_candle(self):
        bars = [at(9, 30, 105, 107, 99, 106)]
        self.assertIsNone(orb_low.opening_candle(bars))

    def test_a_missing_first_bar_cannot_shift_the_range(self):
        """Located by clock time, not by position, so a series that starts at
        09:30 must not treat 09:30 as the range."""
        bars = [at(9, 30, 105, 107, 99, 106), at(9, 45, 106, 108, 100, 107)]
        self.assertIsNone(orb_low.opening_candle(bars))


class ColumnWiringTests(unittest.TestCase):
    """The column must be registered, ordered, and read the same module."""

    def setUp(self):
        self.scanner = dashboard.DashboardScanner()
        self.spec = dashboard.STRATEGY_BY_KEY["orb_low_rejection"]

    def test_the_column_is_named_and_labelled(self):
        self.assertEqual(self.spec.column, "ORB Low Rej")
        self.assertIn("opening-range low", self.spec.label)

    def test_it_is_a_buy_column_so_it_is_hidden_from_rejections(self):
        self.assertFalse(self.spec.sell_only)
        sell_only = {s.key for s in dashboard.STRATEGY_SPECS if s.sell_only}
        self.assertNotIn("orb_low_rejection", sell_only)

    def test_it_sweeps_every_bar_of_the_session(self):
        self.assertTrue(dashboard.sweeps_session(self.spec))

    def test_the_registry_resolves_the_modules_own_evaluator(self):
        module = self.scanner.registry.module_for(self.spec)
        self.assertTrue(callable(module.orb_low_rejection_buy_signal))

    def test_the_users_orb_strategy_is_not_edited(self):
        """The rule lives in its own module, so OrbStrategyCallPut.py, which
        is a working file of its own, stays untouched."""
        mine = self.spec.module_path.read_text(encoding="utf-8")
        self.assertIn("orb_low_rejection_buy_signal", mine)
        self.assertIn("ORB_LOW_REJECTION", mine)
        theirs = (REPO_ROOT / "strategies" / "scripts" /
                  "OrbStrategyCallPut.py").read_text(encoding="utf-8")
        for marker in ("orb_low_rejection", "opening_bullish",
                       "ORB_LOW_REJECTION"):
            self.assertNotIn(marker, theirs,
                             f"OrbStrategyCallPut.py now mentions {marker}")

    def test_the_column_renders_a_buy(self):
        candles = [bullish_open(), at(9, 30, 105, 107, 97, 106)]
        module = self.scanner.registry.module_for(self.spec)
        outcomes = self.scanner._evaluate(
            self.spec, module, "NSE:X-EQ", candles, [], [], NOW)
        self.assertEqual(len(outcomes), 1)
        self.assertEqual(outcomes[0][0], "CE")
        cell = dashboard.build_cell(self.spec, outcomes, candles)
        self.assertEqual(cell["state"], dashboard.BUY)

    def test_the_column_renders_nothing_when_the_rule_does_not_fire(self):
        candles = [bullish_open(c=97.0), at(9, 30, 97, 99, 95, 98)]
        module = self.scanner.registry.module_for(self.spec)
        outcomes = self.scanner._evaluate(
            self.spec, module, "NSE:X-EQ", candles, [], [], NOW)
        self.assertEqual(outcomes, [])
        cell = dashboard.build_cell(self.spec, outcomes, candles)
        self.assertEqual(cell["state"], dashboard.NEUTRAL)

    def test_a_blocked_signal_leaves_no_partial_cell(self):
        """An empty outcome must not produce a half-filled cell."""
        candles = [bullish_open(), at(9, 30, 105, 108, 100, 107)]
        module = self.scanner.registry.module_for(self.spec)
        outcomes = self.scanner._evaluate(
            self.spec, module, "NSE:X-EQ", candles, [], [], NOW)
        cell = dashboard.build_cell(self.spec, outcomes, candles)
        self.assertEqual(cell["state"], dashboard.NEUTRAL)
        self.assertEqual(cell["hit_count"], 0)
        self.assertEqual(cell["times_ist"], [])

    def test_it_appears_in_the_intraday_and_indices_tabs(self):
        specs = dashboard.STRATEGY_SPECS
        self.assertFalse(self.spec.hide_in_intraday)
        # the indices tab is the intraday set plus the index-only strategies
        self.assertIn(self.spec.key,
                      {s.key for s in specs if not s.hide_in_intraday})


if __name__ == "__main__":
    unittest.main()
