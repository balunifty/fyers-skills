"""Tests for the ORB second-candle gap-down/ema10 sell companion.

The rule, as specified: the session's second 15-minute candle opens below the
first candle's close and closes below ema10.

The gap reference is the first candle's **close**, and that choice is pinned
here deliberately, because the two neighbouring readings give different
strategies: against the first candle's open the rule is much weaker, and
against its low no symbol in the last session's universe fired at all. MCX
satisfies both the close and the open reading, so the data cannot settle it and
a test has to.
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
               "EquityOpenRangeOpeningSignals15min.py")
SPEC = importlib.util.spec_from_file_location(
    "orb_opening_signals_for_gap_test", MODULE_PATH)
assert SPEC and SPEC.loader
orb = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = orb
SPEC.loader.exec_module(orb)

DASH_SPEC = importlib.util.spec_from_file_location(
    "orb_gap_dashboard",
    REPO_ROOT / "strategies" / "ui" / "equity_strategy_dashboard.py")
dashboard = importlib.util.module_from_spec(DASH_SPEC)
sys.modules[DASH_SPEC.name] = dashboard
with contextlib.redirect_stdout(io.StringIO()):
    DASH_SPEC.loader.exec_module(dashboard)

IST = orb.MARKET_TIMEZONE
DAY = dt.date(2026, 1, 2)
NOW = dt.datetime(2026, 1, 2, 15, 30, tzinfo=IST)


def at(hour, minute, o, h, l, c, day=DAY):
    stamp = dt.datetime.combine(day, dt.time(hour, minute), tzinfo=IST)
    return SimpleNamespace(epoch=int(stamp.timestamp()), open=float(o),
                           high=float(h), low=float(l), close=float(c),
                           volume=1000.0)


BASE_PRICE = 100.0


def _flat_bar(day, close):
    stamp = dt.datetime.combine(day, dt.time(15, 15), tzinfo=IST)
    return SimpleNamespace(epoch=int(stamp.timestamp()),
                           open=float(close), high=float(close) + 0.5,
                           low=float(close) - 0.5, close=float(close),
                           volume=1000.0)


def history(count=40, price=BASE_PRICE, day=dt.date(2025, 10, 1)):
    """Prior bars at a flat price, so ema10 exists at the second candle.

    A short history leaves ema10 undefined at 09:30, which is the rule's
    fail-safe, so the fixture has to carry real history. Flat rather than
    trending keeps the expected average readable.
    """
    return [_flat_bar(day + dt.timedelta(days=i), price) for i in range(count)]


def session(first_o, first_h, first_l, first_c,
            second_o, second_h, second_l, second_c, quiet=23, day=DAY):
    """A flat prior history, the session's first two candles, then quiet bars.

    The trailing bars hold the second candle's close so the average barely
    drifts, keeping each expected verdict exact.
    """
    bars = history()
    bars.append(at(9, 15, first_o, first_h, first_l, first_c, day=day))
    bars.append(at(9, 30, second_o, second_h, second_l, second_c, day=day))
    for i in range(quiet):
        hour = 9 + (2 + i) // 4
        minute = ((2 + i) % 4) * 15
        bars.append(at(hour, minute, second_c, second_c + 1, second_c - 1,
                       second_c, day=day))
    return bars


def mcx_shaped():
    """A bullish first candle, then a second that gaps below its close.

    The second open sits above the first candle's *open* and below its *close*,
    so only a rule measuring from the close fires. That is the reading the
    user chose, and the test asserts the gap is genuinely measured that way.
    """
    return session(99, 101, 98, 100, 99.5, 100, 96, 99)


def evaluate(candles, now=NOW, restrict=False):
    return orb.second_candle_gap_ema_sell_signal(candles, now, restrict)


class RuleTests(unittest.TestCase):
    """The MCX shape: a small gap down from the first close, closing under
    ema10."""

    def firing(self):
        # the first candle closes 100 on a flat 100 history; the second opens
        # 99.5, below that close, and closes 99, under the ema10 it computes
        return mcx_shaped()

    def test_a_gap_below_the_first_close_closing_under_ema10_is_a_sell(self):
        signal, details = evaluate(self.firing())
        self.assertEqual(signal, "PE")
        self.assertTrue(details["gapped_down"])
        self.assertTrue(details["below_ema10"])
        self.assertIsNotNone(details["ema10"])
        self.assertEqual(details["first_close"], 100.0)
        self.assertEqual(details["second_open"], 99.5)
        self.assertLess(details["second_close"], details["ema10"])

    def test_the_gap_is_measured_from_the_first_candles_close(self):
        """The reference price, pinned.

        This fixture gaps below the first candle's close while staying above
        its open, so a rule measuring from the open would not fire.
        """
        signal, details = evaluate(self.firing())
        self.assertTrue(details["gapped_down"])
        self.assertGreaterEqual(details["second_open"], details["first_open"],
                                "precondition: above the first candle's open")
        self.assertLess(details["second_open"], details["first_close"])
        self.assertEqual(signal, "PE",
                         "measuring from the first open would not fire here")

    def test_the_gap_must_be_strict(self):
        """An open printed exactly at the first close is not a gap."""
        signal, details = evaluate(
            session(99, 101, 98, 100, 100, 100, 96, 99))
        self.assertEqual(signal, "NONE")
        self.assertFalse(details["gapped_down"])
        self.assertIn("did not open below", details["reason"])

    def test_no_gap_but_under_ema10_is_not_a_sell(self):
        """Only the first condition can be missing without the second also
        being tested, so build the case explicitly."""
        signal, details = evaluate(
            session(99, 101, 98, 100, 100.5, 101, 96, 99))
        self.assertEqual(signal, "NONE")
        self.assertFalse(details["gapped_down"])
        self.assertTrue(details["below_ema10"],
                        "precondition: it did close under ema10")
        self.assertIn("did not open below", details["reason"])

    def test_a_gap_that_closes_back_above_ema10_is_not_a_sell(self):
        signal, details = evaluate(
            session(99, 101, 98, 100, 99.5, 101, 96, 105))
        self.assertEqual(signal, "NONE")
        self.assertTrue(details["gapped_down"], "precondition: it did gap")
        self.assertFalse(details["below_ema10"])
        self.assertIn("not under ema10", details["reason"])

    def test_a_close_exactly_on_ema10_is_not_under_it(self):
        """Under is strict."""
        # a flat 100 history makes a close of exactly 100 sit exactly on ema10
        signal, details = evaluate(
            session(99, 101, 98, 100, 99.5, 100, 96, 100))
        self.assertEqual(details["ema10"], 100.0,
                         "the fixture must land on the average")
        self.assertEqual(details["second_close"], 100.0)
        self.assertTrue(details["gapped_down"], "precondition: it did gap")
        self.assertFalse(details["below_ema10"],
                         "closing on the average is not under it")
        self.assertEqual(signal, "NONE")

    def test_the_gap_size_is_reported(self):
        _, details = evaluate(self.firing())
        self.assertEqual(details["gap"], -0.5)
        self.assertEqual(details["gap_percent"], -0.5)

    def test_the_average_is_a_ten_period_one(self):
        """Pinned with a falling history, which separates the periods.

        On a flat history a ten- and a twenty-period average are the same
        number, so a rule quietly averaging longer would pass every other test
        here. A falling history puts ema10 below ema20, and a close placed
        between them is under neither in the way that matters: the ten-period
        average says "not under it", the twenty-period one says "under it", so
        the period alone decides the verdict.
        """
        falling = [_flat_bar(dt.date(2025, 10, 1) + dt.timedelta(days=i),
                             140.0 - i) for i in range(40)]
        closes = [bar.close for bar in falling]
        self.assertLess(orb.ema(closes, 10)[-1], orb.ema(closes, 20)[-1],
                        "precondition: a falling history separates them")

        # The rule averages the whole series, so the first candle is a bar too
        # and has to be in the probe. Find a second-candle close that sits
        # above ema10 but below ema20, given that first close.
        first_close = round(closes[-1] - 0.5, 4)
        probe_base = closes + [first_close]
        chosen = None
        for step in range(1, 600):
            candidate = round(closes[-1] + step * 0.1, 4)
            probe = probe_base + [candidate]
            if orb.ema(probe, 10)[-1] < candidate < orb.ema(probe, 20)[-1]:
                chosen = candidate
                break
        self.assertIsNotNone(chosen,
                             "no close separates the two averages here")

        bars = falling + [
            at(9, 15, first_close + 2, first_close + 3, first_close - 1,
               first_close),
            at(9, 30, first_close - 1, first_close, chosen - 1, chosen),
        ]
        signal, details = evaluate(bars)
        self.assertEqual(details["second_close"], chosen)
        self.assertTrue(details["gapped_down"], "precondition: it did gap")
        self.assertFalse(details["below_ema10"],
                         "the close is above ema10, so this must not fire")
        self.assertEqual(signal, "NONE")

    def test_the_average_is_read_at_the_second_candle(self):
        """Not the newest bar's: the session runs on and drags the average."""
        quiet = session(99, 101, 98, 100, 99.5, 100, 96, 99)
        _signal, details = evaluate(quiet)
        closes = [bar.close for bar in quiet]
        averages = orb.ema(closes, 10)
        index = next(i for i, bar in enumerate(quiet)
                     if bar.epoch == at(9, 30, 0, 0, 0, 0).epoch)
        # the module reports the average rounded for display
        self.assertAlmostEqual(details["ema10"], round(averages[index], 4),
                               places=4,
                               msg="the reported average must be the second "
                                   "candle's")
        self.assertNotAlmostEqual(details["ema10"],
                                  round(averages[-1], 4), places=2,
                                  msg="the fixture must distinguish the two "
                                      "bars")

    def test_ema10_sits_between_the_two_closes(self):
        """The relationship the rule depends on, not a literal average.

        The exact value belongs to the shared indicator's smoothing, so pinning
        a number here would break the moment that is retuned.
        """
        _, details = evaluate(self.firing())
        self.assertGreater(details["first_close"], details["ema10"],
                           "the average lags a flat history")
        self.assertGreater(details["ema10"], details["second_close"],
                           "which is what puts the close under it")
        self.assertTrue(details["below_ema10"])
        self.assertEqual(details["strategy"], "ORB_SECOND_CANDLE_GAP_EMA")

    def test_each_condition_is_reported(self):
        _, details = evaluate(self.firing())
        for key in ("first_open", "first_high", "first_low", "first_close",
                    "first_time", "second_open", "second_high", "second_low",
                    "second_close", "second_time", "gap", "gap_percent",
                    "gapped_down", "ema10", "below_ema10", "session_day",
                    "session_bars", "curr_time", "bar_seconds"):
            self.assertIn(key, details)

    def test_the_cell_time_is_the_second_candles_close(self):
        _, details = evaluate(self.firing())
        self.assertEqual(details["curr_time"], "2026-01-02T09:30:00+05:30")
        self.assertEqual(details["bar_seconds"], 900)
        stamped = dashboard.bar_close_time_ist(
            details, [], 900, False)
        self.assertEqual(stamped, "09:45",
                         "the 09:30 candle closes at 09:45")


class DegenerateInputTests(unittest.TestCase):
    def test_one_candle_is_not_enough(self):
        bars = history() + [at(9, 15, 99, 101, 98, 100)]
        signal, details = evaluate(bars)
        self.assertEqual(signal, "NONE")
        self.assertIn("fewer than 2", details["reason"])

    def test_no_candles(self):
        signal, details = evaluate([])
        self.assertEqual(signal, "NONE")
        self.assertIn("fewer than 2", details["reason"])

    def test_no_history_leaves_the_rule_unable_to_judge(self):
        """Without an ema10 the rule must not judge, not judge on a guess."""
        signal, details = evaluate(
            [at(9, 15, 101, 103, 98, 100), at(9, 30, 99.5, 100, 96, 97)])
        self.assertEqual(signal, "NONE")
        self.assertIsNone(details["ema10"])
        self.assertIsNone(details["below_ema10"])
        self.assertIn("insufficient history", details["reason"])

    def test_a_sparse_series_is_rejected(self):
        bars = history() + [at(9, 15, 99, 101, 98, 100),
                            at(11, 0, 99.5, 100, 96, 99)]
        signal, details = evaluate(bars)
        self.assertEqual(signal, "NONE")
        self.assertIn("not 15-minute bars", details["reason"])

    def test_only_the_opening_candle_of_a_session(self):
        bars = history() + [at(9, 15, 99, 101, 98, 100)]
        self.assertEqual(evaluate(bars)[0], "NONE")

    def test_yesterdays_candles_do_not_count(self):
        """Yesterday gapped; today does not, so the rule must read today."""
        last = DAY - dt.timedelta(days=1)
        bars = history()
        # yesterday: second candle opens below the first close
        bars.append(at(9, 15, 99, 101, 98, 100, day=last))
        bars.append(at(9, 30, 99.5, 100, 96, 99, day=last))
        # today: the second candle opens above the first close
        bars.append(at(9, 15, 99, 101, 98, 100))
        bars.append(at(9, 30, 100.5, 102, 97, 99))
        signal, details = evaluate(bars)
        self.assertEqual(details["session_day"], DAY.isoformat())
        self.assertEqual(details["session_bars"], 2,
                         "only today's two bars, not yesterday's as well")
        self.assertEqual(details["first_close"], 100.0)
        self.assertEqual(details["second_open"], 100.5)
        self.assertFalse(details["gapped_down"],
                         "today's second open is above today's first close")
        self.assertEqual(signal, "NONE")

    def test_restrict_to_today_keeps_a_stale_session_quiet(self):
        last = DAY - dt.timedelta(days=1)
        saturday = dt.datetime(2026, 1, 3, 10, 0, tzinfo=IST)
        bars = history()
        bars.append(at(9, 15, 99, 101, 98, 100, day=last))
        bars.append(at(9, 30, 99.5, 100, 96, 99, day=last))
        self.assertEqual(evaluate(bars, now=saturday, restrict=True)[0],
                         "NONE")

    def test_a_holiday_still_reports_the_last_session(self):
        last = DAY - dt.timedelta(days=1)
        saturday = dt.datetime(2026, 1, 3, 10, 0, tzinfo=IST)
        bars = history()
        bars.append(at(9, 15, 99, 101, 98, 100, day=last))
        bars.append(at(9, 30, 99.5, 100, 96, 99, day=last))
        signal, details = evaluate(bars, now=saturday)
        self.assertEqual(details["session_day"], last.isoformat())
        self.assertEqual(signal, "PE")


class WiringTests(unittest.TestCase):
    def setUp(self):
        self.names = [entry[2] for entry in dashboard.ORB_COMPANION_SIGNALS]

    def test_the_companion_is_registered(self):
        self.assertIn("second_candle_gap_ema_sell_signal", self.names)
        entry = next(e for e in dashboard.ORB_COMPANION_SIGNALS
                     if e[2] == "second_candle_gap_ema_sell_signal")
        self.assertEqual(entry[1], "EquityOpenRangeOpeningSignals15min.py")
        # needs no daily series, so it takes three arguments
        self.assertFalse(entry[3])

    def test_the_companions_keep_their_established_order(self):
        """Buy rules first, then the sells, as the column has always read."""
        self.assertEqual(self.names, [
            "second_candle_prev_high_buy_signal",
            "first_candle_gap_down_sell_signal",
            "first_candle_small_body_sell_signal",
            "second_candle_gap_ema_sell_signal",
        ])

    def test_the_orb_column_runs_it(self):
        scanner = dashboard.DashboardScanner()
        spec = dashboard.STRATEGY_BY_KEY["orb"]
        module = scanner.registry.module_for(spec)
        candles = mcx_shaped()
        outcomes = scanner._evaluate(spec, module, "NSE:X-EQ", candles, [],
                                     [], NOW)
        signals = [outcome[0] for outcome in outcomes]
        self.assertIn("PE", signals,
                      "the ORB column must carry the new sell")
        gap = next(o for o in outcomes
                   if o[1].get("strategy") == "ORB_SECOND_CANDLE_GAP_EMA")
        self.assertEqual(gap[0], "PE")
        self.assertEqual(gap[1]["bar_seconds"], 900)

    def test_the_orb_column_omits_it_when_the_rule_does_not_fire(self):
        scanner = dashboard.DashboardScanner()
        spec = dashboard.STRATEGY_BY_KEY["orb"]
        module = scanner.registry.module_for(spec)
        candles = session(99, 101, 98, 100, 100.5, 101, 96, 105)
        outcomes = scanner._evaluate(spec, module, "NSE:X-EQ", candles, [],
                                     [], NOW)
        strategies = {o[1].get("strategy") for o in outcomes}
        self.assertNotIn("ORB_SECOND_CANDLE_GAP_EMA", strategies)

    def test_the_users_orb_strategy_is_not_edited(self):
        """The rule lives with the other companions, so the working file of its
        own stays untouched."""
        theirs = (REPO_ROOT / "strategies" / "scripts" /
                  "OrbStrategyCallPut.py").read_text(encoding="utf-8")
        for marker in ("second_candle_gap_ema", "ORB_SECOND_CANDLE_GAP_EMA",
                       "gap_ema"):
            self.assertNotIn(marker, theirs)


if __name__ == "__main__":
    unittest.main()
