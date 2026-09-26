"""Tests for the Double Bottom column.

The rule is a bullish candle that rejected a level:

    close > open
    AND ( low <= the previous trading day's low  AND close > it
          OR low <= the opening-range low         AND close > it )

What the tests spend their effort on is everything around that test, because a
rule this loose is only useful if the surrounding machinery is exact:

- a strict ``>``, so a doji is not a signal and closing on a level is not a
  recovery,
- each leg tested alone, so neither can be doing all the work,
- a missing daily series leaving the previous-day leg *untested* rather than
  passed, which is the case the rule_agent is in,
- the judged bar being strictly after the opening candle, which defines the
  range low and so would trivially "reject" it,
- a 15-minute series, not a daily one,
- the same session the newest bar belongs to, so yesterday's bars cannot carry
  the signal.
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
               "EquityDoubleBottomBullishSignal15min.py")
SPEC = importlib.util.spec_from_file_location(
    "EquityDoubleBottomBullishSignal15min", MODULE_PATH)
assert SPEC and SPEC.loader
double_bottom = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = double_bottom
SPEC.loader.exec_module(double_bottom)

DASH_SPEC = importlib.util.spec_from_file_location(
    "double_bottom_dashboard",
    REPO_ROOT / "strategies" / "ui" / "equity_strategy_dashboard.py")
dashboard = importlib.util.module_from_spec(DASH_SPEC)
sys.modules[DASH_SPEC.name] = dashboard
with contextlib.redirect_stdout(io.StringIO()):
    DASH_SPEC.loader.exec_module(dashboard)

IST = double_bottom.MARKET_TIMEZONE
DAY = dt.date(2026, 1, 2)
NOW = dt.datetime(2026, 1, 2, 15, 30, tzinfo=IST)


def at(hour, minute, o, h, low, c, day=DAY):
    stamp = dt.datetime.combine(day, dt.time(hour, minute), tzinfo=IST)
    return SimpleNamespace(epoch=int(stamp.timestamp()), open=float(o),
                           high=float(h), low=float(low), close=float(c),
                           volume=1000.0)


#: The previous trading day's daily bar: low 90.
PRIOR_DAY = DAY - dt.timedelta(days=1)


def daily_bar(o=95.0, h=100.0, low=90.0, c=98.0, day=PRIOR_DAY):
    return at(15, 30, o, h, low, c, day=day)


def session(open_=100.0, high=106.0, low=98.0, close=100.0, minute=15):
    """A session whose 09:15 candle is the opening range."""
    return [at(9, 15, open_, high, low, close)]


def evaluate(candles, daily=None, now=NOW, restrict=False):
    if daily is None:
        daily = [daily_bar()]
    return double_bottom.double_bottom_bullish_signal(
        candles, daily, now, restrict)


def judged(open_, high, low, close, minute=30):
    """A session whose opening range low is 98, plus the bar being judged."""
    return session() + [at(9, minute, open_, high, low, close)]


class RuleTests(unittest.TestCase):
    """The two conditions, and each leg of the rejection on its own."""

    def test_a_bullish_candle_rejecting_both_levels_is_a_buy(self):
        signal, details = evaluate(judged(100, 106, 89.5, 105))
        self.assertEqual(signal, "CE")
        self.assertEqual(details["rejected"], "previous_day_low and orb_low")
        self.assertTrue(details["previous_day_rejection"])
        self.assertTrue(details["orb_low_rejection"])
        self.assertEqual(details["prev_day_low"], 90.0)
        self.assertEqual(details["orb_low"], 98.0)

    def test_the_previous_days_low_alone_carries_it(self):
        """A dip under the prior low, never reaching the opening-range low.

        The judged low 99 sits above the range low 98 and below the prior low
        100, so only the prior low can carry it.
        """
        signal, details = evaluate(judged(100, 106, 99, 105),
                                   daily=[daily_bar(low=100.0)])
        self.assertEqual(signal, "CE")
        self.assertEqual(details["rejected"], "previous_day_low")
        self.assertEqual(details["prev_day_low"], 100.0)
        self.assertTrue(details["previous_day_rejection"])
        self.assertFalse(details["orb_low_rejection"],
                         "precondition: the opening-range low was not reached")

    def test_the_opening_range_low_alone_carries_it(self):
        """The common case: a dip into the range, with the prior low far below."""
        signal, details = evaluate(judged(100, 106, 97, 105),
                                   daily=[daily_bar(low=50.0)])
        self.assertEqual(signal, "CE")
        self.assertEqual(details["rejected"], "orb_low")
        self.assertTrue(details["orb_low_rejection"])
        self.assertFalse(details["previous_day_rejection"],
                         "precondition: the prior low was not reached")

    def test_a_bullish_candle_that_rejects_nothing_is_not_a_buy(self):
        signal, details = evaluate(judged(100, 106, 99, 105))
        self.assertEqual(signal, "NONE")
        self.assertIsNone(details["rejected"])
        self.assertFalse(details["previous_day_rejection"])
        self.assertFalse(details["orb_low_rejection"])
        self.assertIn("reached neither", details["reason"])

    def test_a_rejection_without_a_bullish_body_is_not_a_buy(self):
        """Reaching a level and closing above it is not enough on its own."""
        signal, details = evaluate(judged(106, 107, 89.5, 100))
        self.assertEqual(signal, "NONE")
        self.assertFalse(details["bullish"])
        self.assertTrue(details["orb_low_rejection"],
                        "precondition: it did reject a level")
        self.assertIn("not above open", details["reason"])

    def test_a_doji_is_not_a_buy(self):
        """The test is strict, so an unchanged candle is not bullish."""
        signal, details = evaluate(judged(100, 106, 89.5, 100))
        self.assertEqual(signal, "NONE")
        self.assertFalse(details["bullish"])
        self.assertEqual(details["body"], 0.0)

    def test_a_doji_body_can_still_reject_a_level(self):
        """The rejection is reported even when the body blocks the buy, so the
        cell's reason can say which leg was close to firing."""
        _, details = evaluate(judged(100, 106, 89.5, 100))
        self.assertTrue(details["orb_low_rejection"])
        self.assertTrue(details["previous_day_rejection"])

    def test_the_low_touching_a_level_exactly_still_rejects_it(self):
        """The test is <=, so a low printed exactly at the level counts."""
        signal, details = evaluate(judged(100, 106, 98.0, 105))
        self.assertEqual(signal, "CE")
        self.assertEqual(details["rejected"], "orb_low")
        self.assertTrue(details["orb_low_rejection"])

    def test_a_close_exactly_on_a_level_does_not_reject_it(self):
        """The recovery is strict, so closing on the level is not a rejection.

        The bar's close is 98, printed exactly on the opening-range low, and
        the prior low is set at 99 so that leg fails too: nothing may carry
        the signal and the reason must name the missed recovery.
        """
        signal, details = evaluate(judged(96, 106, 89.5, 98.0),
                                   daily=[daily_bar(low=99.0)])
        self.assertTrue(details["bullish"],
                        "precondition: the body is still bullish")
        self.assertFalse(details["orb_low_rejection"],
                         "closing exactly on the level is not above it")
        self.assertFalse(details["previous_day_rejection"],
                         "the prior low sits above the close, so it fails too")
        self.assertEqual(signal, "NONE")
        self.assertIsNone(details["rejected"])
        self.assertIn("did not get back above", details["reason"])

    def test_the_newest_bar_decides_not_the_newest_that_matched(self):
        """A qualifying bar then a plain one must read as not qualifying."""
        candles = judged(100, 106, 89.5, 105) + [at(9, 45, 105, 106, 99, 100)]
        signal, details = evaluate(candles)
        self.assertEqual(signal, "NONE")
        self.assertEqual(details["curr_close"], 100.0)
        self.assertEqual(details["curr_open"], 105.0)

    def test_the_reason_names_the_level_it_rejected(self):
        for judged_bar, daily, expected in (
            (judged(100, 106, 99, 105), [daily_bar(low=100.0)],
             "previous day's low"),
            (judged(100, 106, 97, 105), [daily_bar(low=50.0)],
             "opening range low"),
        ):
            _, details = evaluate(judged_bar, daily=daily)
            self.assertIn(expected, details["reason"])

    def test_each_condition_is_reported(self):
        _, details = evaluate(judged(100, 106, 89.5, 105))
        for key in ("bullish", "body", "curr_open", "curr_high", "curr_low",
                    "curr_close", "curr_time", "session_day", "session_bars",
                    "prev_day_low", "prev_day", "prev_day_low_available",
                    "orb_low", "orb_low_available", "previous_day_rejection",
                    "orb_low_rejection", "rejected"):
            self.assertIn(key, details)


class MissingLevelTests(unittest.TestCase):
    """A level that cannot be read must be reported untested, never passed."""

    def test_no_daily_series_leaves_the_previous_day_leg_untested(self):
        """The case the rule_agent is in: it has 15-minute candles only."""
        signal, details = evaluate(judged(100, 106, 89.5, 105), daily=[])
        self.assertEqual(signal, "CE")
        self.assertEqual(details["rejected"], "orb_low",
                         "only the reading level may carry the signal")
        self.assertIsNone(details["prev_day_low"])
        self.assertFalse(details["prev_day_low_available"])
        self.assertIsNone(details["previous_day_rejection"])
        self.assertTrue(details["orb_low_rejection"])

    def test_a_missing_daily_series_does_not_widen_the_signal(self):
        """A bar that would have needed the prior low must not fire without it."""
        signal, details = evaluate(judged(100, 106, 89.5, 105), daily=[])
        # it did reach 89.5, which is under the prior low of 90, but that level
        # is unknown here, so the signal rests on the range low alone
        self.assertIsNone(details["previous_day_rejection"])
        self.assertIn("opening range low", details["reason"])

    def test_a_bar_rejecting_only_the_prior_low_is_silent_without_daily(self):
        """The bar that needed the prior low must not fire without it."""
        signal, details = evaluate(judged(100, 106, 99, 105),
                                   daily=[daily_bar(low=100.0)])
        self.assertEqual(signal, "CE")
        self.assertEqual(details["rejected"], "previous_day_low")
        silent, quiet = evaluate(judged(100, 106, 99, 105), daily=[])
        self.assertEqual(silent, "NONE",
                         "the only level that carried it is now unknown")
        self.assertIn("reached neither", quiet["reason"])

    def test_a_same_day_daily_bar_is_not_the_previous_day(self):
        """Today's own range is not the previous session's low."""
        signal, details = evaluate(judged(100, 106, 89.5, 105),
                                   daily=[daily_bar(low=50.0, day=DAY)])
        self.assertFalse(details["prev_day_low_available"])
        self.assertIsNone(details["prev_day"])
        self.assertEqual(details["rejected"], "orb_low")

    def test_a_daily_series_holds_the_most_recent_prior_bar(self):
        older = daily_bar(low=50.0, day=PRIOR_DAY - dt.timedelta(days=3))
        signal, details = evaluate(judged(100, 106, 99, 105),
                                   daily=[older, daily_bar(low=100.0)])
        self.assertEqual(details["prev_day_low"], 100.0,
                         "the most recent bar before the session wins")
        self.assertEqual(details["rejected"], "previous_day_low")

    def test_no_opening_candle_leaves_the_range_leg_untested(self):
        """The judged bar is the only one, so there is no 09:15 candle."""
        signal, details = evaluate([at(9, 30, 100, 106, 89.5, 105)])
        self.assertFalse(details["orb_low_available"])
        self.assertIsNone(details["orb_low_rejection"])
        self.assertEqual(signal, "CE")
        self.assertEqual(details["rejected"], "previous_day_low")

    def test_no_level_at_all_is_reported_plainly(self):
        signal, details = evaluate([at(9, 30, 100, 106, 99, 105)], daily=[])
        self.assertEqual(signal, "NONE")
        self.assertIn("neither", details["reason"])


class InputTests(unittest.TestCase):
    def test_no_candles(self):
        signal, details = evaluate([])
        self.assertEqual(signal, "NONE")
        self.assertEqual(details["session_bars"], 0)
        self.assertIn("no candles", details["reason"])

    def test_a_sparse_intraday_series_is_rejected(self):
        """Two same-session bars hours apart are not 15-minute bars."""
        candles = [at(9, 15, 100, 106, 98, 100), at(11, 0, 100, 107, 97, 105)]
        signal, details = evaluate(candles)
        self.assertEqual(signal, "NONE")
        self.assertIn("not 15-minute bars", details["reason"])

    def test_unsorted_candles_are_ordered_first(self):
        candles = judged(100, 106, 89.5, 105)
        candles = list(reversed(candles))
        signal, details = evaluate(candles)
        self.assertEqual(details["curr_time"][11:16], "09:30")
        self.assertEqual(signal, "CE")
        # and the opening-range low still came from the 09:15 candle
        self.assertEqual(details["orb_low"], 98.0)


class SessionSelectionTests(unittest.TestCase):
    def test_it_judges_the_newest_bars_session(self):
        """A holiday still reports the last session's candle."""
        last = DAY - dt.timedelta(days=3)
        saturday = dt.datetime(2026, 1, 3, 10, 0, tzinfo=IST)
        candles = [at(9, 15, 100, 106, 98, 100, day=last),
                   at(9, 30, 100, 106, 97, 105, day=last)]
        signal, details = evaluate(candles, now=saturday)
        self.assertEqual(details["session_day"], last.isoformat())
        self.assertEqual(details["rejected"], "orb_low")
        self.assertEqual(signal, "CE")

    def test_yesterdays_bullish_bar_cannot_carry_the_signal(self):
        """Yesterday's qualifying bar must not stand in for today's.

        Today's only bar is a doji, so the newest bar in the series is
        unqualified and the column must read empty.
        """
        last = DAY - dt.timedelta(days=1)
        candles = [at(9, 15, 100, 106, 98, 97, day=last),   # bullish, rejected
                   at(9, 15, 100, 106, 98, 100),            # today's range
                   at(9, 30, 100, 106, 99, 100)]            # bearish
        signal, details = evaluate(candles)
        self.assertEqual(details["session_bars"], 2,
                         "only today's two bars, not yesterday's as well")
        self.assertEqual(details["session_day"], DAY.isoformat())
        self.assertFalse(details["bullish"],
                         "the judged bar is today's, and it is bearish")
        self.assertEqual(signal, "NONE")
        self.assertIn("not above open", details["reason"])

    def test_restrict_to_today_keeps_a_stale_session_quiet(self):
        last = DAY - dt.timedelta(days=3)
        saturday = dt.datetime(2026, 1, 3, 10, 0, tzinfo=IST)
        candles = [at(9, 15, 100, 106, 98, 100, day=last),
                   at(9, 30, 100, 106, 97, 105, day=last)]
        self.assertEqual(evaluate(candles, now=saturday, restrict=True)[0],
                         "NONE")

    def test_restrict_to_today_accepts_the_current_session(self):
        self.assertEqual(evaluate(judged(100, 106, 97, 105), now=NOW,
                                  restrict=True)[0], "CE")


class ColumnWiringTests(unittest.TestCase):
    def setUp(self):
        self.scanner = dashboard.DashboardScanner()
        self.spec = dashboard.STRATEGY_BY_KEY["double_bottom"]

    def test_the_column_is_named_and_labelled(self):
        self.assertEqual(self.spec.column, "Double Bottom")
        self.assertIn("closed above its open", self.spec.label)

    def test_it_is_swept_with_the_other_15_minute_columns(self):
        """So the cell shows the newest bar that fired, not only the close."""
        self.assertTrue(dashboard.sweeps_session(self.spec))

    def test_it_is_a_buy_column_so_it_is_hidden_from_rejections(self):
        self.assertFalse(self.spec.sell_only)
        sell_only = {s.key for s in dashboard.STRATEGY_SPECS if s.sell_only}
        self.assertNotIn("double_bottom", sell_only)

    def test_it_declares_the_daily_dependency(self):
        """The previous session's low lives in the daily series, so the spec
        must say it needs one."""
        self.assertTrue(self.spec.needs_daily)

    def test_it_is_shown_for_stocks_and_for_indices(self):
        """No universe restriction, and not hidden from the intraday tab, so it
        lands in the stock tabs and the indices tab alike."""
        self.assertEqual(self.spec.universe, "fno")
        self.assertFalse(self.spec.hide_in_intraday)
        intraday_set = {s.key for s in dashboard.STRATEGY_SPECS
                        if not s.hide_in_intraday}
        self.assertIn("double_bottom", intraday_set)
        index_set = intraday_set | {s.key for s in dashboard.STRATEGY_SPECS
                                    if s.universe == "index_config"}
        self.assertIn("double_bottom", index_set)

    def test_the_registry_resolves_the_modules_own_evaluator(self):
        module = self.scanner.registry.module_for(self.spec)
        self.assertTrue(callable(module.double_bottom_bullish_signal))

    def test_the_column_produces_a_buy_from_the_opening_range_low(self):
        candles = judged(100, 106, 97, 105)   # bullish, rejected the range low
        module = self.scanner.registry.module_for(self.spec)
        outcomes = self.scanner._evaluate(
            self.spec, module, "NSE:X-EQ", candles, [], [daily_bar(low=50.0)],
            NOW)
        self.assertEqual(len(outcomes), 1)
        self.assertEqual(outcomes[0][0], "CE")
        self.assertEqual(outcomes[0][1]["rejected"], "orb_low")
        cell = dashboard.build_cell(self.spec, outcomes, candles)
        self.assertEqual(cell["state"], dashboard.BUY)
        self.assertEqual(cell["time_ist"], "09:45")

    def test_the_column_produces_a_buy_from_the_previous_days_low(self):
        """The other leg, with the prior low between the range low and price."""
        candles = judged(100, 106, 99, 105)
        module = self.scanner.registry.module_for(self.spec)
        outcomes = self.scanner._evaluate(
            self.spec, module, "NSE:X-EQ", candles, [], [daily_bar(low=100.0)],
            NOW)
        self.assertEqual(len(outcomes), 1)
        self.assertEqual(outcomes[0][1]["rejected"], "previous_day_low")
        cell = dashboard.build_cell(self.spec, outcomes, candles)
        self.assertEqual(cell["state"], dashboard.BUY)

    def test_the_column_produces_nothing_when_the_rule_does_not_fire(self):
        candles = judged(106, 107, 99, 100)   # newest bar is bearish
        module = self.scanner.registry.module_for(self.spec)
        outcomes = self.scanner._evaluate(
            self.spec, module, "NSE:X-EQ", candles, [], [daily_bar(low=50.0)],
            NOW)
        self.assertEqual(outcomes, [])

    def test_the_users_orb_strategy_is_not_edited(self):
        """The rule lives in its own module, so OrbStrategyCallPut.py, which is
        a working file of its own, stays untouched. Its stricter double bottom
        stays exactly where it was."""
        theirs = (REPO_ROOT / "strategies" / "scripts" /
                  "OrbStrategyCallPut.py").read_text(encoding="utf-8")
        for marker in ("double_bottom_bullish", "DOUBLE_BOTTOM_BULLISH",
                       "EquityDoubleBottom"):
            self.assertNotIn(marker, theirs)
        # and it still holds the original pattern
        self.assertIn("double_bottom_rejection_signal", theirs)
        self.assertIn("DOUBLE_BOTTOM_REJECTION", theirs)

    def test_the_new_rule_is_looser_than_the_orb_one(self):
        """Documented as a deliberate difference, so pin it.

        The ORB double bottom wants the close above HMA(21), the low to test
        the intraday low so far, and RSI trending up. This one wants a bullish
        body and a rejected level, so a plain bar with no indicator history at
        all satisfies it.
        """
        candles = judged(100, 106, 97, 105)
        self.assertEqual(len(candles), 2, "no history to speak of")
        signal, details = evaluate(candles, daily=[daily_bar(low=50.0)])
        self.assertEqual(signal, "CE")
        self.assertEqual(details["rejected"], "orb_low")


class DegenerateOpeningBarTests(unittest.TestCase):
    """The opening candle defines the range low, so it cannot reject it.

    The sweep judges every bar after the session's first, so this is not
    reached from the dashboard's own path. It is still the rule's contract, and
    a caller that judges the opening bar directly must not get a signal from a
    comparison the bar makes with itself.
    """

    def test_a_bullish_opening_candle_alone_does_not_fire(self):
        signal, details = evaluate([at(9, 15, 100, 106, 98, 105)])
        self.assertEqual(signal, "NONE")
        self.assertIn("defines the opening-range low", details["reason"])

    def test_a_bearish_opening_candle_alone_does_not_fire(self):
        signal, details = evaluate([at(9, 15, 100, 106, 98, 100)])
        self.assertEqual(signal, "NONE")
        self.assertIn("defines the opening-range low", details["reason"])

    def test_a_later_candle_does_judge_the_opening_range_low(self):
        """The very next bar is the first that can reject the level."""
        signal, details = evaluate(judged(100, 106, 97, 105))
        self.assertEqual(signal, "CE")
        self.assertEqual(details["orb_low"], 98.0)


class SweptCellTests(unittest.TestCase):
    """The rendered cell is a merged session cell, not a single bar's."""

    def setUp(self):
        self.scanner = dashboard.DashboardScanner()
        self.spec = dashboard.STRATEGY_BY_KEY["double_bottom"]
        self.module = self.scanner.registry.module_for(self.spec)

    def _cell(self, candles, daily):
        rows = candles
        start, stop = dashboard.session_bar_bounds(rows)
        return dashboard.merge_session_cells([
            dashboard.build_cell(
                self.spec,
                self.scanner._evaluate(self.spec, self.module, "NSE:X-EQ",
                                       rows[:index + 1], [], daily,
                                       dt.datetime.fromtimestamp(
                                           rows[index].epoch, IST)
                                       + dt.timedelta(seconds=900)),
                rows[:index + 1])
            for index in range(start + 1, stop)
        ])

    def test_the_column_is_swept(self):
        self.assertTrue(dashboard.sweeps_session(self.spec))

    def test_the_cell_shows_the_newest_firing_bar_and_a_count(self):
        """Three qualifying bars: the newest is shown, all three are counted."""
        candles = [
            at(9, 15, 100, 106, 98, 100),   # opening range, low 98
            at(9, 30, 100, 106, 97, 105),   # fires
            at(9, 45, 105, 106, 97, 100),   # no
            at(10, 0, 100, 106, 97, 105),   # fires
            at(10, 15, 105, 106, 97, 100),  # no
            at(10, 30, 100, 106, 97, 105),  # fires
        ]
        cell = self._cell(candles, [daily_bar(low=50.0)])
        self.assertEqual(cell["state"], dashboard.BUY)
        self.assertEqual(cell["time_ist"], "10:45",
                         "the newest bar that fired, not the newest bar")
        self.assertEqual(cell["hit_count"], 3)
        self.assertEqual(cell["times_ist"], ["09:45", "10:15", "10:45"])

    def test_a_signal_off_the_latest_bar_is_marked_not_live(self):
        candles = [
            at(9, 15, 100, 106, 98, 100),
            at(9, 30, 100, 106, 97, 105),   # fires
            at(9, 45, 105, 106, 97, 100),   # newest bar: no
        ]
        cell = self._cell(candles, [daily_bar(low=50.0)])
        self.assertEqual(cell["state"], dashboard.BUY)
        self.assertEqual(cell["time_ist"], "09:45")
        self.assertFalse(cell["on_latest_bar"],
                         "the pattern is no longer forming on the newest bar")

    def test_a_session_with_no_firing_bar_is_empty(self):
        candles = [
            at(9, 15, 100, 106, 98, 100),
            at(9, 30, 100, 106, 99, 100),   # bullish but never dips to 98
        ]
        cell = self._cell(candles, [daily_bar(low=50.0)])
        self.assertEqual(cell["state"], dashboard.NEUTRAL)
        self.assertEqual(cell["hit_count"], 0)

    def test_an_intraday_signal_is_reported_at_its_own_time(self):
        """The whole reason for sweeping: a 13:30 reading must not be
        restamped to the close of the session.

        The 13:15 bar qualifies and the two bars after it do not, so the cell
        has to name 13:30 and mark the pattern as no longer forming.
        """
        quiet_times = ["09:15", "09:30", "09:45", "10:00", "10:15", "10:30",
                       "10:45", "11:00", "11:15", "11:30", "11:45", "12:00",
                       "12:15", "12:30", "12:45", "13:00"]
        session = [at(int(hhmm[:2]), int(hhmm[3:]), 100, 106, 98, 100)
                   for hhmm in quiet_times]
        session.append(at(13, 15, 100, 106, 97, 105))   # closes 13:30, fires
        session.append(at(13, 30, 105, 106, 99, 100))   # quiet
        session.append(at(13, 45, 105, 106, 99, 100))   # quiet, the newest
        cell = self._cell(session, [daily_bar(low=50.0)])
        self.assertEqual(cell["state"], dashboard.BUY)
        self.assertEqual(cell["time_ist"], "13:30")
        self.assertEqual(cell["hit_count"], 1)
        self.assertFalse(cell["on_latest_bar"],
                         "the session ran on past the firing bar")


class AgentPartialEvaluationTests(unittest.TestCase):
    """The agent has no daily series, so one of the two legs is untested.

    The rule reports the leg it could not test rather than treating the
    missing level as a passed one, and the agent's knowledge-base entry says
    so, or the agent's signals would read as the full rule.
    """

    @staticmethod
    def _agent():
        agent_dir = REPO_ROOT / "strategies" / "agent"
        if str(agent_dir) not in sys.path:
            sys.path.insert(0, str(agent_dir))
        import rule_agent
        return rule_agent

    def test_the_agent_passes_an_empty_daily_series(self):
        agent = self._agent()
        self.assertEqual(agent.NO_DAILY, [])
        self.assertIn("STRAT_030", agent.DELEGATED_STRATEGIES)
        _file, function, _adapter = agent.DELEGATED_STRATEGIES["STRAT_030"]
        self.assertEqual(function, "double_bottom_bullish_signal")

    def test_the_knowledge_base_says_only_one_leg_is_live(self):
        agent = self._agent()
        engine = agent.StrategyEngine()
        entry = next(e for e in engine.strategies["strategies"]
                     if e["id"] == "STRAT_030")
        self.assertEqual(entry["engine_support"], "wired")
        self.assertIn("opening-range leg", entry["engine_note"])
        self.assertIn("daily", entry["engine_note"])

    def test_a_signal_needing_the_previous_days_low_never_fires(self):
        """The bar's low of 99 is above the range low 98, so the prior low was
        the only leg that could carry it."""
        agent = self._agent()
        engine = agent.StrategyEngine()
        entry = next(e for e in engine.strategies["strategies"]
                     if e["id"] == "STRAT_030")
        self.assertIsNone(
            engine._evaluate_strategy(entry, "NSE:X-EQ",
                                      judged(100, 106, 99, 105)),
            "the prior low is unknown here, so it must not be assumed")

    def test_a_signal_the_range_low_can_carry_still_works(self):
        agent = self._agent()
        engine = agent.StrategyEngine()
        entry = next(e for e in engine.strategies["strategies"]
                     if e["id"] == "STRAT_030")
        signal = engine._evaluate_strategy(
            entry, "NSE:X-EQ", judged(100, 106, 97, 105))
        self.assertIsNotNone(signal)
        self.assertEqual(signal.signal_type, "CE")

    def test_the_agent_judges_one_bar_while_the_column_sweeps(self):
        """The knowledge base must not imply the agent walks the session."""
        agent = self._agent()
        engine = agent.StrategyEngine()
        entry = next(e for e in engine.strategies["strategies"]
                     if e["id"] == "STRAT_030")
        self.assertIn("single bar", entry["engine_note"])


if __name__ == "__main__":
    unittest.main()
