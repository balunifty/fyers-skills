"""Tests for the EMA 10 against EMA 20 signals.

The rule is a crossing, not a state, so the tests concentrate on the
boundary: a stack that is already bullish must stay quiet.
"""
import datetime as dt
import importlib.util
import pathlib
import sys
import unittest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
MODULE_PATH = (REPO_ROOT / "strategies" / "scripts" /
               "EquityEma10_20_Signals15min.py")
SPEC = importlib.util.spec_from_file_location(
    "EquityEma10_20_Signals15min", MODULE_PATH)
assert SPEC and SPEC.loader
cross = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = cross
SPEC.loader.exec_module(cross)

IST = cross.MARKET_TIMEZONE
BASE_DAY = dt.datetime(2026, 1, 1, 9, 15, tzinfo=IST)


def series(closes):
    """A 15-minute series from a rising-then-falling price path."""
    return [
        type("Candle", (), {
            "epoch": int((BASE_DAY + dt.timedelta(minutes=15 * index)
                          ).timestamp()),
            "open": close, "high": close + 1, "low": close - 1,
            "close": close, "volume": 1000.0,
        })()
        for index, close in enumerate(closes)
    ]


def falling_then_rising():
    """A downtrend that turns up: ema10 crosses above ema20 somewhere inside."""
    closes = [100.0 - index for index in range(40)]
    closes += [60.0 + index * 3 for index in range(40)]
    return series(closes)


def crossed_series():
    """The same path, truncated so its last bar IS the crossing bar.

    Derived from the indicator rather than hand-picked, so the fixture cannot
    drift away from what the strategy actually does.
    """
    candles = falling_then_rising()
    closes = [candle.close for candle in candles]
    fast = cross.ema(closes, cross.FAST)
    slow = cross.ema(closes, cross.SLOW)
    for index in range(1, len(closes)):
        if None in (fast[index], slow[index], fast[index - 1], slow[index - 1]):
            continue
        if fast[index - 1] <= slow[index - 1] and fast[index] > slow[index]:
            return candles[:index + 1]
    raise AssertionError("the fixture never produces a crossover")


def crossed_down_series():
    """A rise that turns down, truncated so its last bar IS the down-cross.

    Derived from the indicator rather than hand-picked, so the fixture cannot
    drift away from what the strategy actually does.
    """
    candles = series([100.0 + index for index in range(40)]
                     + [140.0 - index * 3 for index in range(40)])
    closes = [candle.close for candle in candles]
    fast = cross.ema(closes, cross.FAST)
    slow = cross.ema(closes, cross.SLOW)
    for index in range(1, len(closes)):
        if None in (fast[index], slow[index], fast[index - 1], slow[index - 1]):
            continue
        if fast[index - 1] >= slow[index - 1] and fast[index] < slow[index]:
            return candles[:index + 1]
    raise AssertionError("the fixture never produces a down-cross")


class RuleIsolationTests(unittest.TestCase):
    """One rule at a time, so no single check can be deleted unnoticed.

    Each fixture was searched for so that exactly the named condition holds and
    the others do not. Every test asserts that precondition first, so a fixture
    that stops describing what it claims fails loudly rather than passing
    vacuously.
    """

    #: ema10<ema20<ema30 holds, ema50 does not, close below ema10.
    STACK30_ONLY = [
       100.0, 106.0, 112.0, 118.0, 124.0, 130.0, 136.0, 142.0, 148.0, 154.0,
       160.0, 166.0, 172.0, 178.0, 184.0, 190.0, 196.0, 202.0, 208.0, 214.0,
       220.0, 226.0, 232.0, 238.0, 244.0, 236.0, 228.0, 220.0, 212.0, 204.0,
       196.0, 188.0, 180.0, 172.0, 164.0, 156.0, 148.0, 140.0, 132.0, 124.0,
       116.0, 108.0, 100.0, 92.0, 84.0, 76.0, 68.0, 60.0, 52.0, 44.0, 36.0,
       28.0, 20.0, 12.0, 4.0, 1.0, 1.0, 1.0, 1.0, 4.0, 7.0, 10.0, 13.0, 16.0,
       19.0, 22.0, 25.0, 28.0, 31.0, 34.0, 37.0, 40.0, 43.0, 46.0, 49.0,
       52.0, 55.0, 58.0, 61.0, 64.0, 67.0, 70.0, 73.0, 76.0, 79.0, 82.0,
       85.0, 86.0, 87.0, 88.0, 89.0, 90.0, 91.0, 92.0, 93.0, 94.0, 95.0,
       96.0, 97.0, 98.0, 99.0, 100.0, 101.0, 102.0, 103.0, 104.0, 105.0,
       106.0, 107.0, 108.0, 109.0, 110.0, 111.0, 112.0, 113.0, 114.0, 115.0,
       116.0, 115.0, 114.0, 113.0, 112.0, 111.0, 110.0, 109.0, 108.0, 107.0,
       106.0, 105.0, 104.0, 103.0, 102.0, 101.0, 100.0, 99.0, 98.0, 97.0,
       96.0, 95.0, 94.0, 93.0,
    ]

    #: ema10<ema20<ema50 holds, ema30 does not, close below ema10.
    STACK50_ONLY = [
       100.0, 106.0, 112.0, 118.0, 124.0, 130.0, 136.0, 142.0, 148.0, 154.0,
       160.0, 166.0, 172.0, 178.0, 184.0, 190.0, 196.0, 202.0, 208.0, 214.0,
       220.0, 226.0, 232.0, 238.0, 244.0, 236.0, 228.0, 220.0, 212.0, 204.0,
       196.0, 188.0, 180.0, 172.0, 164.0, 156.0, 148.0, 140.0, 132.0, 124.0,
       116.0, 108.0, 100.0, 92.0, 84.0, 76.0, 68.0, 60.0, 52.0, 44.0, 36.0,
       28.0, 20.0, 20.0, 20.0, 20.0, 20.0, 20.0, 20.0, 20.0, 20.0, 20.0,
       20.0, 20.0, 20.0, 20.0, 20.0, 20.0, 20.0, 20.0, 20.0, 20.0, 20.0,
       20.0, 20.0, 20.0, 20.0, 20.0, 20.0, 20.0, 20.0, 24.0, 28.0, 32.0,
       36.0, 40.0, 44.0, 48.0, 52.0, 56.0, 60.0, 64.0, 68.0, 72.0, 76.0,
       80.0, 84.0, 88.0, 92.0, 96.0, 100.0, 104.0, 98.0, 92.0, 86.0, 80.0,
       74.0, 68.0, 62.0, 56.0, 50.0, 44.0, 38.0,
    ]

    #: 60 bars: ema10<ema20<ema50 holds, ema30 does not, but the close is
    #: both stacks hold but the close is ABOVE ema10.
    STACK50_BUT_CLOSE_HIGH = [
       100.0, 111.0, 122.0, 133.0, 144.0, 155.0, 166.0, 177.0, 188.0, 199.0,
       210.0, 221.0, 232.0, 243.0, 254.0, 265.0, 276.0, 287.0, 298.0, 309.0,
       320.0, 331.0, 342.0, 341.0, 340.0, 339.0, 338.0, 337.0, 340.0, 343.0,
       346.0, 349.0, 352.0, 355.0, 358.0, 361.0, 364.0, 358.0, 352.0, 346.0,
       340.0, 334.0, 328.0, 322.0, 316.0, 310.0, 304.0, 298.0, 292.0, 286.0,
       280.0, 274.0, 268.0, 262.0, 256.0, 250.0, 244.0, 238.0, 232.0, 226.0,
       220.0, 214.0, 218.0, 222.0, 226.0, 230.0, 234.0, 238.0, 242.0, 246.0,
       250.0,
    ]

    #: no stack holds, yet the close is below ema10.
    NO_STACK_CLOSE_LOW = [
       100.0, 100.0, 100.0, 100.0, 100.0, 100.0, 100.0, 100.0, 100.0, 100.0,
       100.0, 100.0, 100.0, 100.0, 100.0, 100.0, 100.0, 100.0, 100.0, 100.0,
       100.0, 100.0, 100.0, 100.0, 100.0, 100.0, 100.0, 100.0, 100.0, 100.0,
       102.0, 104.0, 106.0, 108.0, 110.0, 112.0, 114.0, 116.0, 118.0, 120.0,
       122.0, 124.0, 126.0, 128.0, 130.0, 132.0, 134.0, 136.0, 138.0, 140.0,
       142.0, 144.0, 146.0, 148.0, 150.0, 152.0, 154.0, 156.0, 162.0, 168.0,
       174.0, 168.0, 162.0, 156.0, 150.0, 144.0, 138.0,
    ]

    #: 55 bars: ema10 crosses below ema20 on the last bar while both bearish
    #: a down-cross on the last bar, with both stacks already true.
    DOWN_CROSS_WITH_STATE = [
       100.0, 100.0, 100.0, 100.0, 100.0, 100.0, 100.0, 100.0, 100.0, 100.0,
       100.0, 100.0, 100.0, 100.0, 100.0, 100.0, 100.0, 100.0, 100.0, 100.0,
       100.0, 100.0, 100.0, 100.0, 100.0, 106.0, 112.0, 118.0, 124.0, 130.0,
       136.0, 142.0, 148.0, 154.0, 160.0, 166.0, 160.0, 154.0, 148.0, 142.0,
       136.0, 130.0, 124.0, 118.0, 112.0, 106.0, 100.0, 94.0, 88.0, 82.0,
       76.0, 70.0, 64.0, 58.0, 52.0, 46.0, 40.0, 34.0, 28.0, 22.0, 16.0,
       10.0, 4.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 9.0, 17.0, 25.0, 33.0,
       41.0, 49.0, 57.0, 65.0, 57.0, 49.0, 41.0, 33.0, 25.0, 17.0,
    ]

    def _judge(self, closes):
        return cross.ema10_ema20_signal(series(closes))

    def test_the_30_stack_rule_sells_on_its_own(self):
        signal, details = self._judge(self.STACK30_ONLY)
        # precondition: only the 30 stack, with the close below ema10
        self.assertTrue(details["stack_10_20_30"])
        self.assertFalse(details["stack_10_20_50"])
        self.assertTrue(details["close_below_ema10"])
        self.assertFalse(details["crossed_up"])
        self.assertFalse(details["crossed_down"])
        self.assertEqual(signal, "SELL")
        self.assertEqual(details["rule"], "stack_10_20_30")
        self.assertIn("ema30", details["reason"])

    def test_the_50_stack_rule_sells_on_its_own(self):
        signal, details = self._judge(self.STACK50_ONLY)
        # precondition: only the 50 stack, with the close below ema10
        self.assertTrue(details["stack_10_20_50"])
        self.assertFalse(details["stack_10_20_30"])
        self.assertTrue(details["close_below_ema10"])
        self.assertEqual(signal, "SELL")
        self.assertEqual(details["rule"], "stack_10_20_50")
        self.assertIn("ema50", details["reason"])

    def test_the_50_stack_needs_the_close_below_ema10(self):
        signal, details = self._judge(self.STACK50_BUT_CLOSE_HIGH)
        # precondition: the 50 stack holds but the close does not qualify
        self.assertTrue(details["stack_10_20_50"])
        self.assertFalse(details["close_below_ema10"])
        self.assertFalse(details["crossed_up"])
        self.assertFalse(details["crossed_down"])
        self.assertEqual(signal, "NONE")
        self.assertIsNone(details["rule"])

    def test_a_close_below_ema10_alone_is_not_enough(self):
        signal, details = self._judge(self.NO_STACK_CLOSE_LOW)
        # precondition: the close qualifies but no stack holds
        self.assertTrue(details["close_below_ema10"])
        self.assertFalse(details["stack_10_20_30"])
        self.assertFalse(details["stack_10_20_50"])
        self.assertEqual(signal, "NONE")

    def test_a_crossing_outranks_the_bearish_states(self):
        signal, details = self._judge(self.DOWN_CROSS_WITH_STATE)
        # precondition: a down-cross with both stacks already true
        self.assertTrue(details["crossed_down"])
        self.assertTrue(details["stack_10_20_30"])
        self.assertTrue(details["close_below_ema10"])
        self.assertEqual(signal, "SELL")
        self.assertEqual(details["rule"], "cross_down",
                         "the crossing is the event and must be reported")

    def test_the_30_stack_is_checked_before_the_50_stack(self):
        """With both true, the first listed rule is the one named."""
        _, details = self._judge(self.DOWN_CROSS_WITH_STATE)
        self.assertTrue(details["stack_10_20_30"])
        self.assertTrue(details["stack_10_20_50"])

    def test_the_stack_comparison_is_strict(self):
        """Equal averages satisfy neither '<' nor '>', so a flat series is quiet."""
        signal, details = self._judge([100.0] * 60)
        self.assertEqual(details["ema10"], details["ema20"])
        self.assertEqual(signal, "NONE")
        self.assertFalse(details["stack_10_20_30"])
        self.assertFalse(details["stack_10_20_50"])

    def test_every_return_carries_a_rule_key(self):
        """A consistent details shape, so a caller can always read it."""
        for closes in ([], [100.0], [100.0] * 10, [100.0] * 60,
                       self.STACK30_ONLY, self.STACK50_ONLY):
            signal, details = cross.ema10_ema20_signal(series(closes))
            self.assertIn(signal, ("BUY", "SELL", "NONE"))
            self.assertIn("rule", details, len(closes))
            self.assertIn("reason", details, len(closes))


class HappyPathTests(unittest.TestCase):
    def test_a_fresh_upcross_buys(self):
        signal, details = cross.ema10_ema20_signal(crossed_series())
        self.assertEqual(signal, "BUY")
        self.assertTrue(details["crossed_up"])
        self.assertIn("crossed above", details["reason"])

    def test_the_values_around_the_cross_are_reported(self):
        _, details = cross.ema10_ema20_signal(crossed_series())
        # the bar being judged crossed, so it is above; the bar before was not
        self.assertGreater(details["ema10"], details["ema20"])
        self.assertLessEqual(details["previous_ema10"], details["previous_ema20"])

    def test_the_signal_carries_the_price_and_the_bar(self):
        _, details = cross.ema10_ema20_signal(crossed_series())
        for key in ("curr_close", "curr_open", "curr_high", "curr_low",
                    "open", "high", "low", "close", "previous_close",
                    "candle_epoch", "curr_time", "bar_seconds"):
            self.assertIn(key, details)
        self.assertEqual(details["bar_seconds"], 900)

    def test_the_cell_is_stamped_with_the_crossing_bar(self):
        """The cell must read the bar that crossed, not the newest bar."""
        candles = crossed_series()
        _, details = cross.ema10_ema20_signal(candles)
        self.assertEqual(details["candle_epoch"], candles[-1].epoch)

    def test_only_the_last_bar_is_judged(self):
        """Truncating the series judges an earlier bar, which is the sweep.

        Dropping the crossing bar leaves the one before it, which is still
        below, so nothing fires. That is the whole point of a crossing: the
        same series one bar earlier was not a signal.
        """
        candles = crossed_series()
        self.assertEqual(
            cross.ema10_ema20_signal(candles)[0], "BUY")
        self.assertEqual(
            cross.ema10_ema20_signal(candles[:-1])[0], "NONE")


class NotACrossTests(unittest.TestCase):
    def test_a_stack_already_above_does_not_refire(self):
        """The point of requiring the previous bar to be below.

        A steadily rising series also has a close above ema10, so neither
        bearish state rule can apply either, and the column stays quiet.
        """
        closes = [100.0 + index for index in range(60)]
        signal, details = cross.ema10_ema20_signal(series(closes))
        self.assertEqual(signal, "NONE")
        self.assertFalse(details["crossed_up"])
        self.assertFalse(details["crossed_down"])
        self.assertIsNone(details["rule"])

    def test_a_falling_stack_now_sells(self):
        """A steady fall satisfies both bearish state rules, so it sells."""
        closes = [300.0 - index for index in range(60)]
        signal, details = cross.ema10_ema20_signal(series(closes))
        self.assertEqual(signal, "SELL")
        self.assertLess(details["ema10"], details["ema20"])
        self.assertTrue(details["stack_10_20_30"])
        self.assertTrue(details["stack_10_20_50"])
        self.assertTrue(details["close_below_ema10"])
        # 10<20<30 is checked first, so it is the rule named
        self.assertEqual(details["rule"], "stack_10_20_30")

    def test_a_flat_series_never_signals(self):
        """Equal averages satisfy no rule: not 'below' and not 'above'."""
        closes = [100.0] * 60
        signal, details = cross.ema10_ema20_signal(series(closes))
        self.assertEqual(signal, "NONE")
        self.assertEqual(details["ema10"], details["ema20"])
        self.assertFalse(details["close_below_ema10"])

    def test_a_down_cross_sells_rather_than_buying(self):
        signal, details = cross.ema10_ema20_signal(crossed_down_series())
        self.assertEqual(signal, "SELL")
        self.assertTrue(details["crossed_down"])
        self.assertFalse(details["crossed_up"])
        self.assertEqual(details["rule"], "cross_down")
        # a crossing is an event, so it outranks the two state rules
        self.assertIn("crossed below", details["reason"])

    def test_the_previous_bar_must_be_on_the_other_side(self):
        _, details = cross.ema10_ema20_signal(crossed_series())
        # previous bar was below, current bar is above: a genuine crossing
        self.assertLess(details["previous_ema10"], details["previous_ema20"])
        self.assertGreater(details["ema10"], details["ema20"])


class GuardTests(unittest.TestCase):
    def test_no_candles(self):
        signal, details = cross.ema10_ema20_signal([])
        self.assertEqual(signal, "NONE")
        self.assertIn("at least 2 candles", details["reason"])

    def test_a_single_candle(self):
        signal, details = cross.ema10_ema20_signal(series([100.0]))
        self.assertEqual(signal, "NONE")
        self.assertIn("at least 2 candles", details["reason"])

    def test_too_little_history_for_the_slow_ema(self):
        signal, details = cross.ema10_ema20_signal(
            series([100.0, 101.0, 102.0, 103.0]))
        self.assertEqual(signal, "NONE")
        self.assertIn("insufficient history", details["reason"])

    def test_exactly_enough_history_is_judged(self):
        """ema20 needs 20 values, and a comparison needs one more.

        At exactly 20 bars ema(closes, 20)[-1] exists but [-2] is still None,
        because the series is seeded with an SMA of the first 20 values. The
        guard has to notice that, or comparing the two raises a TypeError.
        """
        for count in (19, 20, 21):
            closes = [100.0 - index for index in range(count)]
            signal, details = cross.ema10_ema20_signal(series(closes))
            self.assertIn(signal, ("BUY", "SELL", "NONE"), count)
            if count < 21:
                self.assertEqual(signal, "NONE", count)
                self.assertIn("insufficient history", details["reason"], count)
            else:
                self.assertNotIn("insufficient history",
                                 details["reason"], count)

    def test_no_rule_raises_at_any_series_length(self):
        """The session sweep judges every prefix, including the short ones."""
        closes = [100.0 - index for index in range(40)]
        closes += [60.0 + index * 3 for index in range(40)]
        for count in range(1, len(closes) + 1):
            signal, details = cross.ema10_ema20_signal(series(closes[:count]))
            self.assertIn(signal, ("BUY", "SELL", "NONE"), count)
            self.assertIn("rule", details, count)

    def test_the_judged_bar_is_the_newest_by_time(self):
        """Sorting matters, so assert the epoch it judged rather than the state.

        A V-shaped path crosses in both orderings, so comparing the returned
        signal would prove nothing. What the sort guarantees is that the bar
        judged is the newest by timestamp, not merely the last one supplied.
        """
        candles = crossed_series()
        newest = max(candle.epoch for candle in candles)
        _, ordered = cross.ema10_ema20_signal(candles)
        _, shuffled = cross.ema10_ema20_signal(list(reversed(candles)))
        self.assertEqual(ordered["candle_epoch"], newest)
        self.assertEqual(shuffled["candle_epoch"], newest)
        # and the state is unaffected, because the two orderings are the same
        # series of prices read the other way round
        self.assertEqual(ordered["candle_epoch"], shuffled["candle_epoch"])

    def test_the_bar_seconds_can_be_overridden(self):
        """A caller aggregating timeframes stamps the cell itself."""
        _, details = cross.ema10_ema20_signal(
            crossed_series(), bar_seconds=300)
        self.assertEqual(details["bar_seconds"], 300)


class DashboardColumnTests(unittest.TestCase):
    """The column must call the module's own function."""

    @staticmethod
    def _dashboard():
        spec = importlib.util.spec_from_file_location(
            "ema_20_dashboard",
            REPO_ROOT / "strategies" / "ui" / "equity_strategy_dashboard.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        return module

    def test_the_column_is_registered(self):
        d = self._dashboard()
        spec = d.STRATEGY_BY_KEY["ema_10_cross_20"]
        self.assertEqual(spec.column, "ema10:20 signals")
        self.assertEqual(spec.kind, "ema_cross")
        self.assertEqual(spec.evaluator_name, "ema10_ema20_signal")
        self.assertTrue(spec.module_path.exists())

    def test_the_label_names_every_rule(self):
        """Three of the four rules are sells, so the label must say so."""
        d = self._dashboard()
        label = d.STRATEGY_BY_KEY["ema_10_cross_20"].label
        self.assertIn("crossing above ema20", label)
        self.assertIn("crossing below ema20", label)
        self.assertIn("ema10<ema20<ema30", label)
        self.assertIn("ema10<ema20<ema50", label)

    def test_it_is_not_in_the_rejections_tab(self):
        """Left out deliberately: the bearish state rules fire very often.

        Measured across the universe they account for the overwhelming
        majority of firings, so adding this column to the sell-only tab would
        swamp it. Revisit if the rules are made event-based.
        """
        d = self._dashboard()
        spec = d.STRATEGY_BY_KEY["ema_10_cross_20"]
        self.assertFalse(spec.sell_only)
        self.assertNotIn(spec.key, {s.key for s in d.STRATEGY_SPECS
                                    if s.sell_only})
        self.assertNotIn(spec.key, {s.key for s in d.STRATEGY_SPECS
                                    if s.sell_only})

    def test_the_column_calls_the_module_function_itself(self):
        d = self._dashboard()
        scanner = d.DashboardScanner()
        spec = d.STRATEGY_BY_KEY["ema_10_cross_20"]
        module = scanner.registry.module_for(spec)
        self.assertIs(scanner.registry.evaluator_for(spec),
                      module.ema10_ema20_signal)

    def test_it_is_judged_on_every_bar_of_the_session(self):
        """A crossing can happen at any time, so it must be swept."""
        d = self._dashboard()
        spec = d.STRATEGY_BY_KEY["ema_10_cross_20"]
        self.assertFalse(spec.session_anchored)
        self.assertTrue(d.sweeps_session(spec))

    def test_the_session_open_bar_is_judged(self):
        """A crossing on the 09:15 bar must not be dropped.

        The sweep skips the session's first bar for candle-pattern strategies,
        because their previous bar would be yesterday's close. That reasoning
        does not apply to an indicator, and COALINDIA-EQ on 2026-09-25 crossed
        exactly at the open, so skipping it hid a real signal.
        """
        d = self._dashboard()
        self.assertTrue(d.STRATEGY_BY_KEY["ema_10_cross_20"].judge_session_open)

    def test_candle_pattern_strategies_still_skip_the_open_bar(self):
        """The skip is deliberate for them, and must survive the new flag."""
        d = self._dashboard()
        for key in ("doji_rejection", "higher_high_rejection",
                    "r1_rejection", "prev_high_rejection"):
            spec = d.STRATEGY_BY_KEY[key]
            self.assertTrue(d.sweeps_session(spec), key)
            self.assertFalse(spec.judge_session_open, key)

    def test_a_cross_on_the_first_bar_is_reported_by_the_sweep(self):
        """End-to-end: a crossing placed on the 09:15 bar still surfaces."""
        d = self._dashboard()
        scanner = d.DashboardScanner()
        spec = d.STRATEGY_BY_KEY["ema_10_cross_20"]
        module = scanner.registry.module_for(spec)
        candles = crossed_series()
        # rebase the whole series onto consecutive days so the crossing bar is
        # the first bar of the final session
        rebased = []
        for candle in candles:
            rebased.append(type("C", (), dict(
                epoch=candle.epoch, open=candle.open, high=candle.high,
                low=candle.low, close=candle.close, volume=candle.volume))())
        start = len(rebased) - 1
        # push every earlier bar two days back so only the last is "today"
        two_days = 2 * 24 * 3600
        for index in range(start):
            rebased[index].epoch -= two_days
        rebased = sorted(rebased, key=lambda c: c.epoch)
        now = dt.datetime(2026, 1, 2, 15, 30, tzinfo=IST)

        self.assertEqual(d.session_bar_bounds(rebased)[0], len(rebased) - 1)
        cells = scanner._build_cells(
            [spec], {spec.key: module}, "NSE:X-EQ",
            rebased, [], [], now, sweep_session=True)
        self.assertEqual(cells[spec.key]["state"], "BUY",
                         "the 09:15 crossing was dropped by the sweep")
        self.assertTrue(cells[spec.key]["on_latest_bar"])

    def test_evaluate_returns_a_buy_outcome(self):
        d = self._dashboard()
        scanner = d.DashboardScanner()
        spec = d.STRATEGY_BY_KEY["ema_10_cross_20"]
        module = scanner.registry.module_for(spec)
        now = dt.datetime(2026, 1, 2, 15, 30, tzinfo=IST)

        outcomes = scanner._evaluate(spec, module, "NSE:X-EQ",
                                     crossed_series(), [], [], now)
        self.assertEqual(len(outcomes), 1)
        self.assertEqual(outcomes[0][0], "BUY")

    def test_evaluate_is_silent_on_a_rising_stack(self):
        d = self._dashboard()
        scanner = d.DashboardScanner()
        spec = d.STRATEGY_BY_KEY["ema_10_cross_20"]
        module = scanner.registry.module_for(spec)
        now = dt.datetime(2026, 1, 2, 15, 30, tzinfo=IST)
        steady = series([100.0 + index for index in range(60)])

        self.assertEqual(
            scanner._evaluate(spec, module, "NSE:X-EQ", steady, [], [], now), [])

    def test_the_sweep_reports_a_cross_from_mid_session(self):
        """A crossing at 11:00 must still be listed at the close."""
        d = self._dashboard()
        scanner = d.DashboardScanner()
        spec = d.STRATEGY_BY_KEY["ema_10_cross_20"]
        module = scanner.registry.module_for(spec)
        candles = crossed_series()
        # add quiet bars after the cross so it is no longer the newest bar
        quiet = candles + [type("Candle", (), {
            "epoch": int((BASE_DAY + dt.timedelta(minutes=15 * len(candles))
                          ).timestamp()),
            "open": 200.0, "high": 201.0, "low": 199.0, "close": 200.0,
            "volume": 1000.0})() for _ in range(3)]
        now = dt.datetime(2026, 1, 2, 15, 30, tzinfo=IST)

        cells = scanner._build_cells(
            [spec], {spec.key: module}, "NSE:X-EQ",
            quiet, [], [], now, sweep_session=True)

        cell = cells[spec.key]
        self.assertEqual(cell["state"], "BUY")
        self.assertGreaterEqual(cell["hit_count"], 1)
        self.assertIn("times_ist", cell)

    def test_the_column_appears_in_the_wide_tabs(self):
        d = self._dashboard()
        specs = list(d.STRATEGY_SPECS)
        intraday_keys = {s.key for s in specs if not s.hide_in_intraday}
        self.assertIn("ema_10_cross_20", intraday_keys)
        self.assertNotIn("ema_10_cross_20",
                         {s.key for s in specs if s.hide_in_intraday})


if __name__ == "__main__":
    unittest.main()
