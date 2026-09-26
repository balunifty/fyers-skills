"""Tests for the first-candle small-body ORB sell, and volume sorting.

The small-body rule is a ratio, so the boundaries matter more than usual:
exactly 20% must not sell, and a candle with no range cannot express a ratio.
"""
import datetime as dt
import importlib.util
import pathlib
import sys
import unittest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]

SIGNAL_PATH = (REPO_ROOT / "strategies" / "scripts" /
               "EquityOpenRangeOpeningSignals15min.py")
_spec = importlib.util.spec_from_file_location(
    "EquityOpenRangeOpeningSignals15min", SIGNAL_PATH)
assert _spec and _spec.loader
signals = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = signals
_spec.loader.exec_module(signals)

IST = signals.MARKET_TIMEZONE
TODAY = dt.date(2026, 1, 2)


def at(day, hour, minute, o, h, l, c, v=1000):
    return type("Candle", (), {
        "epoch": int(dt.datetime(day.year, day.month, day.day, hour, minute,
                                  tzinfo=IST).timestamp()),
        "open": float(o), "high": float(h), "low": float(l),
        "close": float(c), "volume": float(v),
    })()


def session(first, second=(100.0, 101.0, 99.0, 100.0)):
    return [
        at(dt.date(2025, 12, 30), 9, 15, 90, 92, 89, 91),
        at(dt.date(2025, 12, 31), 9, 15, 95, 96, 94, 95),
        at(TODAY, 9, 15, *first),
        at(TODAY, 9, 30, *second),
    ]


def sell(candles, current=None):
    return signals.first_candle_small_body_sell_signal(
        candles, current or dt.datetime(2026, 1, 2, 15, 30, tzinfo=IST), False)


class SmallBodyTests(unittest.TestCase):
    def test_a_doji_first_candle_sells(self):
        """Body 0 against a 10-wide range is 0%, well under the limit."""
        signal, details = sell(session((100.0, 105.0, 95.0, 100.0)))
        self.assertEqual(signal, "PE")
        self.assertEqual(details["body"], 0.0)
        self.assertEqual(details["range"], 10.0)
        self.assertEqual(details["body_ratio"], 0.0)
        self.assertIn("under 20%", details["reason"])

    def test_a_small_body_sells(self):
        # body 1.5 against a range of 10 is 15%, under the limit.
        signal, details = sell(session((100.0, 105.0, 95.0, 101.5)))
        self.assertEqual(signal, "PE")
        self.assertAlmostEqual(details["body_ratio"], 0.15, places=4)

    def test_exactly_twenty_percent_does_not_sell(self):
        """The rule is 'less than', so the boundary itself must not fire."""
        signal, details = sell(session((100.0, 105.0, 95.0, 102.0)))
        self.assertEqual(details["body_ratio"], 0.2)
        self.assertEqual(signal, "NONE")
        self.assertIn("not under 20%", details["reason"])

    def test_just_under_twenty_percent_sells(self):
        signal, details = sell(session((100.0, 105.0, 95.0, 101.9)))
        self.assertLess(details["body_ratio"], 0.2)
        self.assertEqual(signal, "PE")

    def test_a_large_body_does_not_sell(self):
        # body 8 against a range of 10 is 80%.
        signal, details = sell(session((100.0, 105.0, 95.0, 108.0)))
        self.assertEqual(signal, "NONE")
        self.assertEqual(details["body_ratio"], 0.8)

    def test_a_down_candle_with_a_small_body_also_sells(self):
        """The body is absolute, so direction does not enter the ratio."""
        signal, details = sell(session((100.0, 105.0, 95.0, 98.5)))
        self.assertEqual(details["body"], 1.5)
        self.assertEqual(signal, "PE")

    def test_a_candle_with_no_range_is_not_judged(self):
        """A zero range cannot express a ratio, so no signal."""
        signal, details = sell(session((100.0, 100.0, 100.0, 100.0)))
        self.assertIsNone(details["body_ratio"])
        self.assertEqual(signal, "NONE")
        self.assertIn("no high-low range", details["reason"])

    def test_the_ratio_uses_the_candles_own_range(self):
        """A small body inside a small range still counts as small."""
        # body 0.2 against a range of 1.0 is 20% exactly, so it must not sell.
        signal, details = sell(session((100.0, 100.5, 99.5, 100.2)))
        self.assertAlmostEqual(details["body_ratio"], 0.2, places=4)
        self.assertEqual(signal, "NONE")

    def test_only_the_first_candle_is_judged(self):
        """A large first body and a small second body must not sell."""
        signal, details = sell(session(
            first=(100.0, 110.0, 90.0, 108.0),      # body 8 over range 20 = 40%
            second=(108.0, 108.1, 107.9, 108.0),    # body ~0%
        ))
        self.assertEqual(details["first_close"], 108.0)
        self.assertEqual(details["body_ratio"], 0.4)
        self.assertEqual(signal, "NONE")

    def test_the_cell_is_stamped_with_the_first_candle(self):
        _, details = sell(session((100.0, 105.0, 95.0, 100.0)))
        self.assertTrue(details["curr_time"].startswith("2026-01-02T09:15"))
        self.assertEqual(details["curr_close"], details["first_close"])
        self.assertEqual(details["bar_seconds"], 900)

    def test_a_daily_series_is_refused(self):
        daily_only = [at(dt.date(2025, 12, 31), 9, 15, 99, 101, 99, 100),
                      at(TODAY, 9, 15, 100, 100, 100, 100)]
        signal, details = sell(daily_only)
        self.assertEqual(signal, "NONE")
        self.assertIn("fewer than 2 candles", details["reason"])

    def test_a_session_with_one_candle_is_refused(self):
        signal, details = sell([
            at(dt.date(2025, 12, 31), 9, 15, 95, 96, 94, 95),
            at(TODAY, 9, 15, 100, 100, 100, 100),
        ])
        self.assertEqual(signal, "NONE")
        self.assertIn("fewer than 2 candles", details["reason"])

    def test_bars_far_apart_are_refused(self):
        signal, details = sell([
            at(dt.date(2025, 12, 31), 9, 15, 95, 96, 94, 95),
            at(TODAY, 9, 15, 100, 100, 100, 100),
            at(TODAY, 23, 45, 100, 101, 99, 100),
        ])
        self.assertEqual(signal, "NONE")
        self.assertIn("not 15-minute bars", details["reason"])

    def test_no_previous_session_is_needed(self):
        """Unlike the second-candle rule, this one reads no daily bar."""
        self.assertEqual(sell(session((100.0, 105.0, 95.0, 100.0)))[0], "PE")

    def test_the_limit_is_twenty_percent(self):
        self.assertEqual(signals.SMALL_BODY_RATIO, 0.20)

    def test_a_holiday_reports_the_latest_session(self):
        holiday = dt.datetime(2026, 1, 5, 10, 0, tzinfo=IST)
        signal, details = sell(session((100.0, 105.0, 95.0, 100.0)), holiday)
        self.assertEqual(signal, "PE")
        self.assertEqual(details["session_day"], "2026-01-02")

    def test_restrict_to_today_uses_the_current_session(self):
        later = dt.datetime(2026, 1, 5, 10, 0, tzinfo=IST)
        signal, details = signals.first_candle_small_body_sell_signal(
            session((100.0, 105.0, 95.0, 100.0)), later, True)
        self.assertEqual(signal, "NONE")
        self.assertEqual(details["session_day"], "2026-01-05")


class GapDownTests(unittest.TestCase):
    """First candle gaps below yesterday's close and falls further: sell."""

    def setUp(self):
        # yesterday closed at 99, so the gap is measured against that.
        self.daily = [at(dt.date(2025, 12, 31), 9, 15, 95, 101, 94, 99)]
        self.current = dt.datetime(2026, 1, 2, 15, 30, tzinfo=IST)

    def signal(self, first, second=(100.0, 101.0, 99.0, 100.0), daily=None):
        return signals.first_candle_gap_down_sell_signal(
            session(first, second),
            self.daily if daily is None else daily,
            self.current, False)

    def test_a_gap_down_that_falls_sells(self):
        # opens 98, below yesterday's 99, and closes 97, below its own open
        signal, details = self.signal((98.0, 98.5, 96.5, 97.0))
        self.assertEqual(signal, "PE")
        self.assertTrue(details["gapped_down"])
        self.assertTrue(details["closed_below_open"])
        self.assertEqual(details["previous_close"], 99.0)
        self.assertEqual(details["gap"], -1.0)
        self.assertIn("below yesterday", details["reason"])

    def test_opening_exactly_at_yesterdays_close_is_not_a_gap(self):
        """The rule is 'below', so an equal open must not count."""
        signal, details = self.signal((99.0, 99.5, 97.0, 97.5))
        self.assertEqual(details["previous_close"], 99.0)
        self.assertFalse(details["gapped_down"])
        self.assertEqual(signal, "NONE")
        self.assertIn("did not open below", details["reason"])

    def test_a_gap_down_that_recovers_does_not_sell(self):
        # opens 98 below 99, but closes 100, above its own open
        signal, details = self.signal((98.0, 101.0, 97.5, 100.0))
        self.assertTrue(details["gapped_down"])
        self.assertFalse(details["closed_below_open"])
        self.assertEqual(signal, "NONE")
        self.assertIn("recovered", details["reason"])

    def test_a_bearish_candle_that_did_not_gap_does_not_sell(self):
        # opens 100, above yesterday's 99, and closes 98
        signal, details = self.signal((100.0, 100.5, 97.5, 98.0))
        self.assertFalse(details["gapped_down"])
        self.assertTrue(details["closed_below_open"])
        self.assertEqual(signal, "NONE")
        self.assertIn("did not open below", details["reason"])

    def test_closing_equal_to_the_open_does_not_sell(self):
        """'close < open' is strict, so a doji after a gap is not a sell."""
        signal, details = self.signal((98.0, 99.0, 97.0, 98.0))
        self.assertTrue(details["gapped_down"])
        self.assertEqual(signal, "NONE")
        self.assertIn("recovered", details["reason"])

    def test_only_the_first_candle_is_judged(self):
        """A recovery on the second candle cannot undo the first candle."""
        signal, details = self.signal(
            first=(98.0, 98.5, 96.5, 97.0),
            second=(97.0, 105.0, 96.0, 104.0),
        )
        self.assertEqual(signal, "PE")
        self.assertEqual(details["first_close"], 97.0)

    def test_the_cell_is_stamped_with_the_first_candle(self):
        _, details = self.signal((98.0, 98.5, 96.5, 97.0))
        self.assertTrue(details["curr_time"].startswith("2026-01-02T09:15"))
        self.assertEqual(details["curr_close"], details["first_close"])
        self.assertEqual(details["bar_seconds"], 900)

    def test_the_gap_size_is_reported(self):
        _, details = self.signal((98.0, 98.5, 96.5, 97.0))
        self.assertEqual(details["gap"], -1.0)
        self.assertAlmostEqual(details["gap_percent"], -1.0101, places=3)

    def test_no_previous_session_is_refused(self):
        signal, details = self.signal(
            (98.0, 98.5, 96.5, 97.0),
            daily=[at(TODAY, 9, 15, 1, 2, 0.5, 1.5)])
        self.assertEqual(signal, "NONE")
        self.assertIn("no previous session", details["reason"])

    def test_a_daily_bar_dated_today_is_not_yesterday(self):
        """A same-day daily bar must not act as the previous close."""
        signal, details = self.signal(
            (98.0, 98.5, 96.5, 97.0),
            daily=self.daily + [at(TODAY, 9, 15, 1, 999, 0.5, 998)])
        self.assertEqual(details["previous_close"], 99.0)
        self.assertEqual(signal, "PE")

    def test_a_daily_series_is_refused(self):
        daily_only = [at(dt.date(2025, 12, 31), 9, 15, 99, 101, 94, 100),
                      at(TODAY, 9, 15, 98, 99, 97, 98)]
        signal, details = signals.first_candle_gap_down_sell_signal(
            daily_only, self.daily, self.current, False)
        self.assertEqual(signal, "NONE")
        self.assertIn("fewer than 2 candles", details["reason"])

    def test_bars_far_apart_are_refused(self):
        candles = [
            at(dt.date(2025, 12, 31), 9, 15, 95, 96, 94, 95),
            at(TODAY, 9, 15, 98, 98.5, 96.5, 97.0),
            at(TODAY, 23, 45, 97, 98, 96, 97),
        ]
        signal, details = signals.first_candle_gap_down_sell_signal(
            candles, self.daily, self.current, False)
        self.assertEqual(signal, "NONE")
        self.assertIn("not 15-minute bars", details["reason"])

    def test_a_holiday_reports_the_latest_session(self):
        holiday = dt.datetime(2026, 1, 5, 10, 0, tzinfo=IST)
        signal, details = signals.first_candle_gap_down_sell_signal(
            session((98.0, 98.5, 96.5, 97.0)), self.daily, holiday, False)
        self.assertEqual(signal, "PE")
        self.assertEqual(details["session_day"], "2026-01-02")

    def test_restrict_to_today_uses_the_current_session(self):
        later = dt.datetime(2026, 1, 5, 10, 0, tzinfo=IST)
        signal, details = signals.first_candle_gap_down_sell_signal(
            session((98.0, 98.5, 96.5, 97.0)), self.daily, later, True)
        self.assertEqual(signal, "NONE")
        self.assertEqual(details["session_day"], "2026-01-05")


class DashboardOrbColumnTests(unittest.TestCase):
    """The ORB column must report the small-body sell."""

    @staticmethod
    def _dashboard():
        spec = importlib.util.spec_from_file_location(
            "small_body_dashboard",
            REPO_ROOT / "strategies" / "ui" / "equity_strategy_dashboard.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        return module

    def test_all_companions_are_registered_in_priority_order(self):
        d = self._dashboard()
        entries = d.ORB_COMPANION_SIGNALS
        self.assertEqual([entry[2] for entry in entries], [
            "second_candle_prev_high_buy_signal",
            "first_candle_gap_down_sell_signal",
            "first_candle_small_body_sell_signal",
            "second_candle_gap_ema_sell_signal",
        ])
        # the two rules that read yesterday's close or high need the daily
        # series; the small-body and gap/ema rules are self-contained
        self.assertEqual([entry[3] for entry in entries],
                         [True, True, False, False])

    def test_the_buy_is_listed_first_so_it_wins_a_tie(self):
        """build_cell keeps the first firing outcome, so order is priority."""
        d = self._dashboard()
        buy = next(e[2] for e in d.ORB_COMPANION_SIGNALS
                   if e[2] == "second_candle_prev_high_buy_signal")
        self.assertEqual(d.ORB_COMPANION_SIGNALS[0][2], buy)

    def test_the_orb_column_reports_the_gap_down_sell(self):
        d = self._dashboard()
        scanner = d.DashboardScanner()
        spec = d.STRATEGY_BY_KEY["orb"]
        module = scanner.registry.module_for(spec)
        # opens 98, below yesterday's 99 close, and falls to 97
        candles = session((98.0, 98.5, 96.5, 97.0))
        daily = [at(dt.date(2025, 12, 31), 9, 15, 95, 101, 94, 99)]
        now = dt.datetime(2026, 1, 2, 15, 30, tzinfo=IST)

        outcomes = scanner._evaluate(
            spec, module, "NSE:X-EQ", candles, [], daily, now)

        reported = [o for o in outcomes
                    if o[1].get("strategy") == "ORB_FIRST_CANDLE_GAP_DOWN"]
        self.assertEqual(len(reported), 1,
                         [o[1].get("strategy") for o in outcomes])
        self.assertEqual(reported[0][0], "PE")
        cell = d.build_cell(spec, [reported[0]], candles, False)
        self.assertEqual(cell["state"], "SELL")
        self.assertEqual(cell["time_ist"], "09:30")

    def test_a_daily_view_reports_no_gap_down_sell(self):
        d = self._dashboard()
        scanner = d.DashboardScanner()
        spec = d.STRATEGY_BY_KEY["orb"]
        module = scanner.registry.module_for(spec)
        daily_only = [at(dt.date(2025, 12, 31), 9, 15, 99, 101, 94, 100),
                      at(TODAY, 9, 15, 98, 99, 97, 98)]
        now = dt.datetime(2026, 1, 2, 15, 30, tzinfo=IST)
        outcomes = scanner._evaluate(
            spec, module, "NSE:X-EQ", daily_only, [], daily_only, now)
        self.assertEqual(
            [o for o in outcomes
             if o[1].get("strategy") == "ORB_FIRST_CANDLE_GAP_DOWN"], [])

    def test_the_gap_down_sell_outranks_the_small_body_sell(self):
        """Both are sells, so the cell shows one; the gap-down is the clearer."""
        d = self._dashboard()
        scanner = d.DashboardScanner()
        spec = d.STRATEGY_BY_KEY["orb"]
        module = scanner.registry.module_for(spec)
        # opens 98, below yesterday's 99, drifts down to 97.9 on a long wick:
        # a small body (0.1 of a 4.0 range) and a failed gap-down open
        candles = session((98.0, 100.0, 96.0, 97.9))
        daily = [at(dt.date(2025, 12, 31), 9, 15, 95, 101, 94, 99)]
        now = dt.datetime(2026, 1, 2, 15, 30, tzinfo=IST)

        outcomes = scanner._evaluate(
            spec, module, "NSE:X-EQ", candles, [], daily, now)
        order = [o[1].get("strategy") for o in outcomes]

        self.assertIn("ORB_FIRST_CANDLE_GAP_DOWN", order)
        self.assertIn("ORB_FIRST_CANDLE_SMALL_BODY", order)
        self.assertLess(order.index("ORB_FIRST_CANDLE_GAP_DOWN"),
                        order.index("ORB_FIRST_CANDLE_SMALL_BODY"))
        cell = d.build_cell(spec, outcomes, candles, False)
        self.assertEqual(cell["state"], "SELL")
        self.assertEqual(cell["note"], "ORB_FIRST_CANDLE_GAP_DOWN")

    def test_every_companion_module_exists_and_exposes_its_evaluator(self):
        d = self._dashboard()
        for module_name, file_name, evaluator_name, _ in d.ORB_COMPANION_SIGNALS:
            path = d.SCRIPTS_DIR / file_name
            self.assertTrue(path.exists(), path)
            module = d.load_module(module_name, path)
            self.assertTrue(callable(getattr(module, evaluator_name)))

    def test_the_orb_script_is_still_not_edited(self):
        d = self._dashboard()
        source = d.STRATEGY_BY_KEY["orb"].module_path.read_text(encoding="utf-8")
        self.assertNotIn("first_candle_small_body_sell_signal", source)
        self.assertNotIn("EquityOpenRangeOpeningSignals15min", source)

    def test_the_orb_column_reports_the_small_body_sell(self):
        d = self._dashboard()
        scanner = d.DashboardScanner()
        spec = d.STRATEGY_BY_KEY["orb"]
        module = scanner.registry.module_for(spec)
        candles = session((100.0, 105.0, 95.0, 100.0))   # doji first candle
        daily = [at(dt.date(2025, 12, 31), 9, 15, 95, 101, 94, 99)]
        now = dt.datetime(2026, 1, 2, 15, 30, tzinfo=IST)

        outcomes = scanner._evaluate(
            spec, module, "NSE:X-EQ", candles, [], daily, now)

        reported = [o for o in outcomes
                    if o[1].get("strategy") == "ORB_FIRST_CANDLE_SMALL_BODY"]
        self.assertEqual(len(reported), 1, [o[1].get("strategy") for o in outcomes])
        self.assertEqual(reported[0][0], "PE")
        # the cell is stamped with the first candle's 09:30 close
        cell = d.build_cell(spec, [reported[0]], candles, False)
        self.assertEqual(cell["time_ist"], "09:30")
        self.assertEqual(cell["state"], "SELL")

    def test_a_buy_takes_precedence_over_the_small_body_sell(self):
        """A cell holds one state, so the rarer buy wins a tie.

        NSE:COALINDIA-EQ on 2026-09-25 is a real example: its first candle had
        a 6.6% body, which is a small-body sell, while its second candle closed
        above both the first close and the previous day's high, which is a buy.
        The column shows the buy, because the companion order evaluates the
        second-candle rule first and build_cell keeps the first firing outcome.
        """
        d = self._dashboard()
        scanner = d.DashboardScanner()
        spec = d.STRATEGY_BY_KEY["orb"]
        module = scanner.registry.module_for(spec)
        # first candle: body 0.2 over a 10 range = 2%, a small-body sell
        # second candle: closes well above the first close and the 101 high
        candles = session(
            first=(100.0, 105.0, 95.0, 100.2),
            second=(100.5, 110.0, 100.4, 109.0),
        )
        daily = [at(dt.date(2025, 12, 31), 9, 15, 95, 101, 94, 99)]
        now = dt.datetime(2026, 1, 2, 15, 30, tzinfo=IST)

        # both rules do fire on this symbol
        small, small_details = sell(candles)
        self.assertEqual(small, "PE")
        second, _ = signals.second_candle_prev_high_buy_signal(
            candles, daily, now, False)
        self.assertEqual(second, "CE")

        outcomes = scanner._evaluate(
            spec, module, "NSE:X-EQ", candles, [], daily, now)
        order = [o[1].get("strategy") for o in outcomes]
        self.assertEqual(order[0], "ORB_SECOND_CANDLE_PREV_HIGH")
        self.assertIn("ORB_FIRST_CANDLE_SMALL_BODY", order)

        cell = d.build_cell(spec, outcomes, candles, False)
        self.assertEqual(cell["state"], "BUY")
        self.assertEqual(cell["time_ist"], "09:45")
        self.assertEqual(small_details["body_ratio"], 0.02)

    def test_a_daily_view_reports_no_small_body_sell(self):
        d = self._dashboard()
        scanner = d.DashboardScanner()
        spec = d.STRATEGY_BY_KEY["orb"]
        module = scanner.registry.module_for(spec)
        daily_only = [at(dt.date(2025, 12, 31), 9, 15, 99, 101, 99, 100),
                      at(TODAY, 9, 15, 100, 100, 100, 100)]
        now = dt.datetime(2026, 1, 2, 15, 30, tzinfo=IST)

        outcomes = scanner._evaluate(
            spec, module, "NSE:X-EQ", daily_only, [], daily_only, now)

        self.assertEqual(
            [o for o in outcomes
             if o[1].get("strategy") == "ORB_FIRST_CANDLE_SMALL_BODY"], [])


if __name__ == "__main__":
    unittest.main()
