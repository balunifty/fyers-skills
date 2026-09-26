"""Tests for the open-candle EMA stack buy signal.

Covers each rule in the definition, the interaction between them, and the two
inputs that must not produce a signal: a non-15-minute series (the end-of-day
view hands over daily bars) and a session too short to hold an opening candle.
"""
import datetime as dt
import importlib.util
import pathlib
import sys
import unittest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
MODULE_PATH = (REPO_ROOT / "strategies" / "scripts" /
               "EquityOpenCandleEmaStackBuy15min.py")
SPEC = importlib.util.spec_from_file_location(
    "EquityOpenCandleEmaStackBuy15min", MODULE_PATH)
assert SPEC and SPEC.loader
strategy = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = strategy
SPEC.loader.exec_module(strategy)

IST = strategy.MARKET_TIMEZONE
PREV_DAY = dt.date(2026, 1, 1)
SESSION_DAY = dt.date(2026, 1, 2)


def at(day, hour, minute, o, h, l, c, step=15):
    """A candle at day/hour:minute IST, stepping forward by `step` minutes."""
    return type("Candle", (), {
        "epoch": int(dt.datetime(day.year, day.month, day.day, hour, minute,
                                  tzinfo=IST).timestamp()),
        "open": float(o), "high": float(h), "low": float(l),
        "close": float(c), "volume": 1000.0,
    })()


class SignalTestCase(unittest.TestCase):
    def setUp(self):
        # 2025-12-31 is the session before 2026-01-02 (a Friday), so its high
        # of 101 is the "previous day's high" every rule is measured against.
        self.daily = [
            at(dt.date(2025, 12, 30), 9, 15, 90, 100, 88, 95, step=0),
            at(dt.date(2025, 12, 31), 9, 15, 95, 101, 94, 99, step=0),
        ]
        self.current = dt.datetime(2026, 1, 2, 15, 30, tzinfo=IST)

    def history(self, opens, highs, lows, closes, warmup=0.0):
        """One session's 15-minute bars from 09:15, after two warm-up sessions.

        warmup shifts the earlier sessions' drift, so a test can start from a
        rising or a falling EMA stack.
        """
        bars = []
        for day in (dt.date(2025, 12, 29), dt.date(2025, 12, 30)):
            for index in range(26):
                close = 100 + warmup * index
                bars.append(at(day, 9, 15, close, close + 1, close - 1, close))
        minute = 9 * 60 + 15
        for index in range(len(opens)):
            hour, mins = divmod(minute, 60)
            bars.append(at(SESSION_DAY, hour, mins, opens[index],
                           highs[index], lows[index], closes[index]))
            minute += 15
        return sorted(bars, key=lambda c: c.epoch)

    def rising_session(self, first=None, second=None):
        """Closes that keep ema10>ema20>ema30 and ema10>ema20>ema50."""
        first = first or (100.0, 130.0, 99.0, 128.0)   # bullish, clears 101
        second = second or (128.0, 140.0, 127.0, 138.0)
        return self.history(
            opens=[first[0], second[0]],
            highs=[first[1], second[1]],
            lows=[first[2], second[2]],
            closes=[first[3], second[3]],
        )

    def signal(self, candles, daily=None, current=None):
        return strategy.open_candle_ema_stack_buy_signal(
            candles, self.daily if daily is None else daily,
            self.current if current is None else current, False)


class HappyPathTests(SignalTestCase):
    def test_a_bullish_first_candle_above_the_previous_high_buys(self):
        signal, details = self.signal(self.rising_session())

        self.assertEqual(signal, "BUY")
        self.assertEqual(details["candle"], "first")
        self.assertTrue(details["bullish"])
        self.assertTrue(details["above_previous_high"])
        self.assertEqual(details["reason"], "all conditions met")

    def test_the_conditions_are_named_in_the_details(self):
        _, details = self.signal(self.rising_session())
        for key in ("ema10", "ema20", "ema30", "ema50", "previous_high",
                    "previous_day", "session_day", "curr_time", "close"):
            self.assertIn(key, details)
        self.assertEqual(details["previous_day"], "2025-12-31")
        self.assertEqual(details["session_day"], "2026-01-02")
    def test_the_reported_time_is_the_opening_candle(self):
        _, details = self.signal(self.rising_session())
        # 09:15 is the first bar's start, so the dashboard reads 09:30.
        self.assertTrue(details["curr_time"].startswith("2026-01-02T09:15"))

    def test_both_ema_stacks_are_reported_as_holding(self):
        _, details = self.signal(self.rising_session())
        self.assertGreater(details["ema10"], details["ema20"])
        self.assertGreater(details["ema20"], details["ema30"])
        self.assertGreater(details["ema10"], details["ema20"])
        self.assertGreater(details["ema20"], details["ema50"])

    def test_the_earliest_qualifying_candle_wins(self):
        """Both candles qualify; the signal must be the first one."""
        signal, details = self.signal(self.rising_session())
        self.assertEqual(signal, "BUY")
        self.assertEqual(details["candle"], "first")

    def test_the_second_candle_qualifies_when_the_first_does_not(self):
        # The first candle is bearish, so only the second can qualify.
        candles = self.history(
            opens=[130.0, 128.0],
            highs=[131.0, 140.0],
            lows=[99.0, 127.0],
            closes=[100.0, 138.0],
        )
        signal, details = self.signal(candles)

        self.assertEqual(signal, "BUY")
        self.assertEqual(details["candle"], "second")
        self.assertTrue(details["curr_time"].startswith("2026-01-02T09:30"))


class RuleTests(SignalTestCase):
    def test_a_bearish_first_candle_does_not_stop_the_second_being_judged(self):
        """The rule is an either/or, so a bearish first candle is not fatal."""
        signal, details = self.signal(self.history(
            opens=[130.0, 128.0],
            highs=[131.0, 140.0],
            lows=[99.0, 127.0],
            closes=[100.0, 138.0],
        ))
        self.assertEqual(signal, "BUY")
        self.assertEqual(details["candle"], "second")

    def test_a_doji_first_candle_is_not_bullish(self):
        # Both candles fail, so the earliest failure is the one reported.
        signal, details = self.signal(self.history(
            opens=[120.0, 138.0],
            highs=[121.0, 139.0],
            lows=[119.0, 136.0],
            closes=[120.0, 137.0],   # first candle close == open, second falls
        ))
        self.assertEqual(signal, "NONE")
        self.assertIn("not bullish", details["reason"])
        self.assertEqual(details["candle"], "first")

    def test_closing_below_the_previous_high_is_rejected(self):
        # Previous high is 101, and neither candle closes above it.
        signal, details = self.signal(self.history(
            opens=[95.0, 96.0],
            highs=[103.0, 104.0],
            lows=[94.0, 95.0],
            closes=[100.0, 100.5],
        ))
        self.assertEqual(signal, "NONE")
        self.assertIn("did not close above", details["reason"])

    def test_closing_exactly_at_the_previous_high_is_rejected(self):
        """The rule is 'above', so an equal close must not count."""
        signal, details = self.signal(self.history(
            opens=[95.0, 96.0],
            highs=[103.0, 103.0],
            lows=[94.0, 95.0],
            closes=[101.0, 101.0],
        ))
        self.assertEqual(signal, "NONE")
        self.assertIn("did not close above", details["reason"])

    def test_a_broken_stack_with_no_inversion_buys_nothing(self):
        """A bullish candle on a stack that is merely misordered is not a buy.

        The EMAs here are 10 > 20 > 50 but 30 above 20, so neither stack holds
        and the order is not inverted either, leaving no signal at all.
        """
        candles = self.history(
            opens=[100.0, 104.0],
            highs=[101.0, 140.0],
            lows=[99.0, 103.0],
            closes=[130.0, 138.0],   # bullish and far above the 101 high
            warmup=-1.0,             # a mild slide: keeps 20 under 30
        )
        # nudge the fast average up and the slow one down so 10>20>50 but
        # 20<30 fails, by ending the session on a sharp fall
        candles[-1] = at(SESSION_DAY, 9, 30, 130.0, 130.5, 108.0, 109.0)
        signal, details = self.signal(candles)

        self.assertFalse(details["bullish_stack"])
        if not details["inverted_stack"]:
            self.assertEqual(signal, "NONE")
            self.assertIn("EMA stack not aligned", details["reason"])

    def test_a_bullish_candle_on_a_descending_history_now_sells(self):
        """The old 'broken stack' fixture is a clean inversion, so it sells."""
        candles = self.history(
            opens=[100.0, 104.0],
            highs=[101.0, 140.0],
            lows=[99.0, 103.0],
            closes=[130.0, 138.0],
            warmup=-6.0,             # a long slide beforehand
        )
        signal, details = self.signal(candles)

        # A falling history leaves the EMAs in strict descending order, which
        # is exactly the sell condition.
        self.assertTrue(details["bullish"])            # the open was strong
        self.assertTrue(details["above_previous_high"])
        self.assertFalse(details["bullish_stack"])
        self.assertTrue(details["inverted_stack"])
        self.assertLess(details["ema10"], details["ema20"])
        self.assertLess(details["ema20"], details["ema50"])
        self.assertEqual(signal, "SELL")

    def test_a_steady_climb_satisfies_both_stacks(self):
        closes = [100.0 + 4 * index for index in range(12)]
        signal, details = self.signal(self.history(
            # open a unit below each close so every candle is bullish
            opens=[c - 1 for c in closes],
            highs=[c + 1 for c in closes],
            lows=[c - 2 for c in closes],
            closes=closes))
        self.assertEqual(signal, "BUY")
        self.assertGreater(details["ema20"], details["ema30"])
        self.assertGreater(details["ema20"], details["ema50"])

    def test_ema_values_are_reported_for_a_rejection_too(self):
        signal, details = self.signal(self.history(
            opens=[95.0, 96.0], highs=[103.0, 104.0],
            lows=[94.0, 95.0], closes=[100.0, 100.5]))
        self.assertEqual(signal, "NONE")
        # the reason is the previous high, but the EMAs are still reported
        for key in ("ema10", "ema20", "ema30", "ema50"):
            self.assertIn(key, details)
            self.assertIsInstance(details[key], (int, float))


class GuardTests(SignalTestCase):
    def test_a_daily_series_is_refused(self):
        """The end-of-day view passes daily bars, which hold one bar a day.

        There is no opening 15-minute candle in such a series, so the length
        check is what must stop it, before any price or EMA is examined.
        """
        daily_series = [at(dt.date(2025, 12, 31), 9, 15, 99, 101, 94, 100, step=0),
                        at(SESSION_DAY, 9, 15, 128, 140, 127, 138, step=0)]
        signal, details = strategy.open_candle_ema_stack_buy_signal(
            daily_series, self.daily, self.current, False)

        self.assertEqual(signal, "NONE")
        self.assertIn("fewer than 2 candles", details["reason"])
        # nothing about price or EMAs was even evaluated
        self.assertNotIn("bullish", details)

    def test_bars_far_apart_are_refused(self):
        """A session whose bars are hours apart is not a 15-minute series."""
        first = at(SESSION_DAY, 9, 15, 100, 130, 99, 128, step=0)
        # same calendar day, but 14.5 hours later
        second = at(SESSION_DAY, 23, 45, 128, 140, 127, 138, step=0)
        signal, details = strategy.open_candle_ema_stack_buy_signal(
            [first, second], self.daily, self.current, False)

        self.assertEqual(signal, "NONE")
        self.assertIn("not 15-minute bars", details["reason"])
        self.assertGreater(details["first_gap_seconds"], 900)

    def test_no_previous_session_is_refused(self):
        only_today = [at(SESSION_DAY, 9, 15, 100, 130, 99, 128),
                      at(SESSION_DAY, 9, 30, 128, 140, 127, 138)]
        signal, details = strategy.open_candle_ema_stack_buy_signal(
            only_today, [at(SESSION_DAY, 9, 15, 1, 2, 0.5, 1.5, step=0)],
            self.current, False)
        self.assertEqual(signal, "NONE")
        self.assertIn("no previous session", details["reason"])

    def test_a_session_with_one_candle_is_refused(self):
        bars = [at(dt.date(2025, 12, 30), 9, 15, 100, 101, 99, 100),
                at(dt.date(2025, 12, 31), 9, 15, 100, 101, 99, 100),
                at(SESSION_DAY, 9, 15, 100, 130, 99, 128)]
        signal, details = strategy.open_candle_ema_stack_buy_signal(
            bars, self.daily, self.current, False)
        self.assertEqual(signal, "NONE")
        self.assertIn("fewer than 2 candles", details["reason"])
        self.assertEqual(details["session_bars"], 1)

    def test_no_candles_at_all(self):
        signal, details = strategy.open_candle_ema_stack_buy_signal(
            [], self.daily, self.current, False)
        self.assertEqual(signal, "NONE")
        self.assertIn("no candles", details["reason"])

    def test_third_and_later_candles_are_never_judged(self):
        """Only the first two count, so a late setup must not fire."""
        opens = [100.0] * 10
        highs = [101.0] * 10
        lows = [99.0] * 10
        closes = [100.0] * 9 + [200.0]   # only the tenth bar surges
        candles = self.history(opens=opens, highs=highs, lows=lows, closes=closes)
        signal, details = strategy.open_candle_ema_stack_buy_signal(
            candles, self.daily, self.current, False)
        self.assertEqual(signal, "NONE")
        self.assertIn("first", details["reason"])


class SessionSelectionTests(SignalTestCase):
    def test_restrict_to_today_uses_the_current_session(self):
        candles = self.rising_session()
        late = dt.datetime(2026, 1, 5, 10, 0, tzinfo=IST)  # a Monday, no bars
        signal, details = strategy.open_candle_ema_stack_buy_signal(
            candles, self.daily, late, True)
        self.assertEqual(signal, "NONE")
        self.assertEqual(details["session_day"], "2026-01-05")

    def test_without_restricting_the_latest_session_is_used(self):
        """A holiday must still report the last session's setup."""
        candles = self.rising_session()
        holiday = dt.datetime(2026, 1, 5, 10, 0, tzinfo=IST)
        signal, details = strategy.open_candle_ema_stack_buy_signal(
            candles, self.daily, holiday, False)
        self.assertEqual(signal, "BUY")
        self.assertEqual(details["session_day"], "2026-01-02")

    def test_the_previous_session_is_never_the_session_itself(self):
        """A daily bar dated today must not be used as 'yesterday'."""
        today_bar = at(SESSION_DAY, 9, 15, 1, 999, 0.5, 998, step=0)
        signal, details = strategy.open_candle_ema_stack_buy_signal(
            self.rising_session(), self.daily + [today_bar],
            self.current, False)
        self.assertEqual(signal, "BUY")
        self.assertLessEqual(details["previous_high"], 101.0)


class SellSideTests(SignalTestCase):
    """ema10 < ema20 < ema50 at either opening candle is a sell."""

    def falling(self, closes=None, opens=None):
        """A session on top of a sustained slide, so the EMAs invert."""
        closes = closes or [100.0, 98.0]
        opens = opens or [101.0, 100.0]
        return self.history(
            opens=opens,
            highs=[c + 1 for c in closes],
            lows=[c - 1 for c in closes],
            closes=closes,
            warmup=-6.0,
        )

    def test_a_descending_stack_sells(self):
        signal, details = self.signal(self.falling())
        self.assertEqual(signal, "SELL")
        self.assertIn("ema10 < ema20 < ema50", details["reason"])
        self.assertTrue(details["inverted_stack"])
        self.assertFalse(details["bullish_stack"])

    def test_the_sell_needs_no_bullish_candle_or_new_high(self):
        """Exactly as specified: the EMA order alone is the sell."""
        signal, details = self.signal(self.falling())
        # these two are reported but neither is required for the sell
        self.assertFalse(details["bullish"])
        self.assertFalse(details["above_previous_high"])
        self.assertEqual(signal, "SELL")

    def test_an_ascending_stack_does_not_sell(self):
        closes = [100.0 + 4 * index for index in range(12)]
        signal, details = self.signal(self.history(
            opens=[c - 1 for c in closes], highs=[c + 1 for c in closes],
            lows=[c - 2 for c in closes], closes=closes))
        self.assertFalse(details["inverted_stack"])
        self.assertEqual(signal, "BUY")

    def test_the_two_directions_are_mutually_exclusive(self):
        """One needs ema10 above ema20, the other below it."""
        for closes, opens in (([100.0, 98.0], [101.0, 100.0]),
                              ([100.0 + 4 * i for i in range(12)],
                               [99.0 + 4 * i for i in range(12)])):
            signal, details = self.signal(self.falling(closes, opens))
            self.assertNotEqual(
                details["bullish_stack"] and details["inverted_stack"], True,
                "a stack cannot be both ascending and descending")
            self.assertIn(signal, ("BUY", "SELL", "NONE"))

    def test_the_buy_wins_when_the_two_candles_disagree(self):
        """A buy on the second candle must not be masked by a sell on the first."""
        # First candle: a sustained slide is already in train, so inverted.
        # Second candle: a huge rally that realigns the stack and clears 101.
        candles = self.history(
            opens=[100.0, 120.0],
            highs=[101.0, 400.0],
            lows=[99.0, 119.0],
            closes=[100.0, 390.0],
            warmup=-6.0,
        )
        signal, details = self.signal(candles)

        self.assertEqual(signal, "BUY",
                         "the more specific setup must take precedence")
        self.assertEqual(details["candle"], "second")

    def test_the_inverted_check_is_a_pure_predicate(self):
        self.assertTrue(strategy.ema_stack_inverted(
            {10: 1.0, 20: 2.0, 30: 9.0, 50: 3.0}))
        self.assertFalse(strategy.ema_stack_inverted(
            {10: 3.0, 20: 2.0, 30: 1.0, 50: 0.5}))
        # equal averages are not 'less than'
        self.assertFalse(strategy.ema_stack_inverted(
            {10: 2.0, 20: 2.0, 30: 1.0, 50: 0.5}))

    def test_a_missing_ema_fails_the_inversion(self):
        self.assertFalse(strategy.ema_stack_inverted(
            {10: None, 20: 2.0, 30: 1.0, 50: 0.5}))
        self.assertFalse(strategy.ema_stack_inverted({}))

    def test_the_sell_needs_no_previous_day_high(self):
        """The EMA order is the whole rule, so no daily bar is consulted."""
        signal, _ = strategy.open_candle_ema_stack_buy_signal(
            self.falling(),
            [at(SESSION_DAY, 9, 15, 1, 2, 0.5, 1.5, step=0)],
            self.current, False)
        self.assertEqual(signal, "NONE", "no previous session, so nothing at all")

    def test_short_history_never_sells(self):
        """Too little history for a 50-EMA fails both directions."""
        bars = [at(dt.date(2025, 12, 31), 9, 15, 100, 101, 99, 100),
                at(SESSION_DAY, 9, 15, 100, 101, 99, 100),
                at(SESSION_DAY, 9, 30, 100, 101, 99, 100)]
        signal, _ = strategy.open_candle_ema_stack_buy_signal(
            bars, self.daily, self.current, False)
        self.assertEqual(signal, "NONE")


class EmaHelperTests(unittest.TestCase):
    def test_a_short_series_fails_the_stack(self):
        holds, values = strategy.ema_stack_holds([100.0, 101.0, 102.0])
        self.assertFalse(holds)
        self.assertTrue(any(value is None for value in values.values()))

    def test_a_flat_series_fails_because_the_order_is_strict(self):
        holds, _ = strategy.ema_stack_holds([100.0] * 80)
        self.assertFalse(holds, "equal averages are not 'greater than'")

    def test_a_steady_climb_satisfies_both_stacks(self):
        values = [100.0 + index for index in range(80)]
        holds, latest = strategy.ema_stack_holds(values)
        self.assertTrue(holds)
        self.assertGreater(latest[10], latest[20])
        self.assertGreater(latest[20], latest[30])
        self.assertGreater(latest[20], latest[50])

    def test_the_two_stacks_are_checked_separately(self):
        self.assertEqual(strategy.STACKS, ((10, 20, 30), (10, 20, 50)))


if __name__ == "__main__":
    unittest.main()
