"""Tests for the Second Candle sell gate.

The gate holds a second-candle sell back unless the close is below the
previous day's high or below ema10. The interesting cases are the boundaries,
the two legs independently, and the fail-open behaviour when neither leg can be
evaluated.
"""
import contextlib
import datetime as dt
import importlib.util
import pathlib
import sys
import unittest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
MODULE_PATH = (REPO_ROOT / "strategies" / "scripts" /
               "SecondCandleSellGate15min.py")
SPEC = importlib.util.spec_from_file_location(
    "SecondCandleSellGate15min", MODULE_PATH)
assert SPEC and SPEC.loader
gate = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = gate
SPEC.loader.exec_module(gate)

IST = gate.MARKET_TIMEZONE
PRIOR = dt.date(2025, 12, 31)
TODAY = dt.date(2026, 1, 2)


def at(day, hour, minute, o, h, l, c):
    return type("Candle", (), {
        "epoch": int(dt.datetime(day.year, day.month, day.day, hour, minute,
                                  tzinfo=IST).timestamp()),
        "open": float(o), "high": float(h), "low": float(l),
        "close": float(c), "volume": 1000.0,
    })()


class SeriesTestCase(unittest.TestCase):
    """A flat-then-jump history, so ema10 is predictable and controllable."""

    def setUp(self):
        self.now = dt.datetime(2026, 1, 2, 15, 30, tzinfo=IST)
        # yesterday's high is 110
        self.daily = [at(PRIOR, 9, 15, 100, 110, 95, 105)]

    def candles(self, final_close, previous_high=110, base=100.0):
        """A flat run at `base`, then one judged bar closing at final_close."""
        bars = [at(TODAY, 9, 15, base, base + 0.5, base - 0.5, base)
                for _ in range(20)]
        bars.append(at(TODAY, 15, 30, base, final_close + 0.5,
                       final_close - 0.5, final_close))
        if previous_high is not None:
            self.daily = [at(PRIOR, 9, 15, 100, previous_high, 95, 105)]
        return bars

    def gate_for(self, candles, daily=None):
        return gate.second_candle_sell_gate(
            candles, self.daily if daily is None else daily, self.now, False)


class GateTests(SeriesTestCase):
    def test_below_the_previous_high_allows_the_sell(self):
        # ema10 sits at the flat base, so only the previous-high leg can pass
        candles = self.candles(105.0, previous_high=110)
        allowed, verdict = self.gate_for(candles)
        self.assertTrue(allowed)
        self.assertTrue(verdict["below_previous_high"])
        self.assertEqual(verdict["gate_previous_high"], 110.0)
        self.assertIn("previous day's high", verdict["reason"])

    def test_below_ema10_allows_the_sell(self):
        # previous high far below, so only the ema10 leg can pass
        candles = self.candles(95.0, previous_high=50.0)
        allowed, verdict = self.gate_for(candles)
        self.assertTrue(allowed)
        self.assertTrue(verdict["below_ema10"])
        self.assertFalse(verdict["below_previous_high"])
        self.assertIn(f"ema{gate.FAST}", verdict["reason"])

    def test_above_both_blocks_the_sell(self):
        candles = self.candles(200.0, previous_high=110)
        allowed, verdict = self.gate_for(candles)
        self.assertFalse(allowed)
        self.assertFalse(verdict["below_previous_high"])
        self.assertFalse(verdict["below_ema10"])
        self.assertIn("not below", verdict["reason"])

    def test_either_leg_alone_is_enough(self):
        """The rule is an either/or, so one leg must carry it."""
        # close 108 under a previous high of 110, but far above the flat ema10
        below_high_only = self.candles(108.0, previous_high=110)
        allowed, verdict = self.gate_for(below_high_only)
        self.assertTrue(allowed)
        self.assertTrue(verdict["below_previous_high"])
        self.assertFalse(verdict["below_ema10"])

        # close 99 under a previous high of 98, so only ema10 can carry it
        below_ema_only = self.candles(99.0, previous_high=98.0)
        allowed, verdict = self.gate_for(below_ema_only)
        self.assertTrue(allowed)
        self.assertTrue(verdict["below_ema10"])
        self.assertFalse(verdict["below_previous_high"])

    def test_closing_exactly_at_the_previous_high_is_blocked(self):
        """"Below" is strict, so an equal close does not pass."""
        candles = self.candles(110.0, previous_high=110)
        allowed, verdict = self.gate_for(candles)
        self.assertEqual(verdict["judged_close"], 110.0)
        self.assertEqual(verdict["gate_previous_high"], 110.0)
        self.assertFalse(verdict["below_previous_high"])
        self.assertFalse(allowed)

    def test_both_legs_passing_is_named(self):
        candles = self.candles(99.0, previous_high=110)
        allowed, verdict = self.gate_for(candles)
        self.assertTrue(allowed)
        self.assertTrue(verdict["below_previous_high"])
        self.assertTrue(verdict["below_ema10"])
        self.assertIn("and", verdict["reason"])

    def test_closing_exactly_at_ema10_is_blocked(self):
        """"Below" is strict, so a close sitting on ema10 does not pass.

        The flat history seeds ema10 at the same value, so the judged bar's
        close lands exactly on it. A "<=" here would wrongly let the sell
        through on the ema10 leg alone, so the previous day's high is set below
        the close to make that the only leg left.
        """
        candles = self.candles(100.0, previous_high=99.0)
        allowed, verdict = self.gate_for(candles)
        self.assertEqual(verdict["judged_close"], 100.0)
        self.assertEqual(verdict["gate_ema10"], 100.0)
        self.assertFalse(verdict["below_ema10"],
                         "a close equal to ema10 is not below it")
        self.assertFalse(verdict["below_previous_high"],
                         "the other leg must fail for this to be a boundary")
        self.assertFalse(allowed)

    def _straddling_candles(self):
        """A judged bar whose close sits between ema10 and ema20.

        A flat history cannot separate the two averages, so this rises
        steadily instead. The close is searched for rather than hard-coded,
        because hand-transcribing an EMA value drifts from the indicator.
        """
        day = dt.date(2025, 10, 1)
        history = [at(day + dt.timedelta(days=i), 15, 30, 100, 101, 99,
                      100.0 + 10.0 * i) for i in range(25)]
        judged_day = day + dt.timedelta(days=25)
        for close in [100.0 + 10.0 * i for i in range(30, -1, -1)]:
            probe = history + [at(judged_day, 15, 30, close, close + 0.5,
                                  close - 0.5, close)]
            closes = [candle.close for candle in probe]
            fast = gate.ema(closes, gate.FAST)[-1]
            slow = gate.ema(closes, gate.FAST + 10)[-1]
            if slow < close < fast:
                return probe, slow
        self.fail("no close straddles the two averages for this fixture")

    def test_the_gate_compares_against_a_ten_period_average(self):
        """A close under ema10 but over ema20 must count as below ema10.

        If the gate averaged a longer window it would read the same close as
        not-below and hold back a sell for the wrong reason, so the fixture
        straddles the two averages and the previous day's high is set out of
        the way.
        """
        candles, slow = self._straddling_candles()
        # a previous day's high far below every close, so it cannot carry this
        daily = [at(dt.date(2025, 10, 1), 15, 30, 1, 0.5, 0.25, 0.75)]
        allowed, verdict = self.gate_for(candles, daily)

        close = verdict["judged_close"]
        self.assertEqual(gate.FAST, 10, "the gate's average must be 10 long")
        self.assertGreater(verdict["gate_ema10"], close,
                           "precondition: the close is under ema10")
        self.assertLess(slow, close,
                        "precondition: the close is over ema20")
        self.assertFalse(verdict["below_previous_high"],
                         "the other leg must fail for this to prove anything")
        self.assertTrue(verdict["below_ema10"])
        self.assertTrue(allowed)

    def test_an_unsorted_series_gives_the_same_verdict(self):
        """Candle order must not decide the gate; the newest bar still wins."""
        candles = self.candles(105.0)
        forward = self.gate_for(candles)[1]
        backward = self.gate_for(list(reversed(candles)))[1]
        for key in ("judged_close", "judged_time", "gate_ema10",
                    "gate_previous_high", "below_ema10", "below_previous_high",
                    "allowed"):
            self.assertEqual(forward[key], backward[key], key)

    def test_the_gate_always_names_its_rule(self):
        _, verdict = self.gate_for(self.candles(105.0))
        self.assertEqual(verdict["rule"], "second_candle_sell_gate")
        for key in ("judged_close", "judged_time", "session_day",
                    "gate_previous_high", "gate_ema10",
                    "below_previous_high", "below_ema10", "allowed",
                    "reason"):
            self.assertIn(key, verdict)

    def test_restrict_to_today_shifts_which_day_counts_as_previous(self):
        """The day asked for decides the previous session, not the bar's.

        On a non-trading day the newest bar is the last session's, so the two
        modes disagree: asking about Saturday counts Friday as previous,
        while judging the Friday bar counts Thursday. That is a different
        high, and a different verdict.
        """
        candles = self.candles(105.0)        # newest bar belongs to TODAY
        thursday = at(dt.date(2025, 12, 31), 9, 15, 100, 500, 95, 105)
        friday = at(TODAY, 9, 15, 100, 106, 95, 105)
        daily = [thursday, friday]
        later = dt.datetime(2026, 1, 3, 10, 0, tzinfo=IST)

        _, asked = gate.second_candle_sell_gate(candles, daily, later, True)
        self.assertEqual(asked["session_day"], "2026-01-03")
        self.assertEqual(asked["gate_previous_high"], 106.0,
                         "asked about the 3rd, the 2nd is the previous session")
        self.assertTrue(asked["below_previous_high"])

        _, judged = gate.second_candle_sell_gate(candles, daily, later, False)
        self.assertEqual(judged["session_day"], "2026-01-02")
        self.assertEqual(judged["gate_previous_high"], 500.0,
                         "the judged bar is the 2nd, so the 31st is previous")
        self.assertNotEqual(asked["gate_previous_high"],
                            judged["gate_previous_high"],
                            "the two modes must actually differ here")

    def test_the_judged_bar_is_the_newest_of_the_series(self):
        """The test is made against the live price, not a fixed candle."""
        candles = self.candles(105.0)
        _, verdict = self.gate_for(candles)
        self.assertEqual(verdict["judged_close"], 105.0)
        self.assertEqual(verdict["judged_time"][11:16], "15:30")


class FailOpenTests(SeriesTestCase):
    """A data gap must never be the reason a signal disappears."""

    def test_no_previous_session_falls_back_to_ema10(self):
        candles = self.candles(99.0)
        allowed, verdict = self.gate_for(
            candles, daily=[at(TODAY, 9, 15, 100, 110, 95, 105)])
        self.assertIsNone(verdict["below_previous_high"])
        self.assertTrue(verdict["below_ema10"])
        self.assertTrue(allowed, "ema10 alone can still carry the sell")

    def test_no_leg_evaluable_allows_the_sell(self):
        # no previous session, and too little history for ema10
        candles = [at(TODAY, 9, 15, 100, 101, 99, 100),
                   at(TODAY, 9, 30, 100, 101, 99, 100)]
        allowed, verdict = self.gate_for(
            candles, daily=[at(TODAY, 9, 15, 100, 110, 95, 105)])
        self.assertIsNone(verdict["below_previous_high"])
        self.assertIsNone(verdict["below_ema10"])
        self.assertTrue(allowed)
        self.assertIn("allowing", verdict["reason"])

    def test_no_candles_allows_the_sell(self):
        allowed, verdict = self.gate_for([])
        self.assertTrue(allowed)
        self.assertIn("nothing to gate", verdict["reason"])

    def test_a_daily_bar_dated_today_is_not_yesterday(self):
        """A same-day daily bar must not act as the previous high."""
        candles = self.candles(105.0, previous_high=110)
        verdict_daily = self.daily + [at(TODAY, 9, 15, 1, 999, 0.5, 998)]
        allowed, verdict = self.gate_for(candles, daily=verdict_daily)
        self.assertEqual(verdict["gate_previous_high"], 110.0)
        self.assertTrue(allowed)

    def test_previous_session_high_skips_later_daily_bars(self):
        self.assertIsNone(gate.previous_session_high([], TODAY))
        self.assertEqual(
            gate.previous_session_high(
                [at(TODAY, 9, 15, 1, 999, 0.5, 998)], TODAY),
            None)
        self.assertEqual(
            gate.previous_session_high(
                [at(PRIOR, 9, 15, 1, 123, 0.5, 2)], TODAY),
            123.0)


@contextlib.contextmanager
def _today_is_the_strategy_module(module, candles_of):
    """Let the strategy's own get_today_candles() see a synthetic session.

    It filters on the real wall-clock date, so without this a fixture dated in
    the past can never make the strategy fire and the gate would be untested.
    """
    original = module.get_today_candles
    module.get_today_candles = candles_of
    try:
        yield
    finally:
        module.get_today_candles = original


class DashboardWiringTests(unittest.TestCase):
    """The second-candle column must run its sells through the gate."""

    @staticmethod
    def _dashboard():
        spec = importlib.util.spec_from_file_location(
            "gate_dashboard",
            REPO_ROOT / "strategies" / "ui" / "equity_strategy_dashboard.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        return module

    def test_the_strategy_script_is_not_edited(self):
        """The gate exists so the user's own strategy file stays untouched."""
        d = self._dashboard()
        source = d.STRATEGY_BY_KEY["second_candle"].module_path.read_text(
            encoding="utf-8")
        self.assertNotIn("second_candle_sell_gate", source)
        self.assertNotIn("SecondCandleSellGate15min", source)

    def test_the_gate_module_is_reachable(self):
        d = self._dashboard()
        module = d.load_module("gate_check",
                               d.SCRIPTS_DIR / "SecondCandleSellGate15min.py")
        self.assertTrue(callable(module.second_candle_sell_gate))

    def test_the_only_pe_source_is_the_bearish_reversal(self):
        """Pins a fact the gate depends on.

        second_candle_breakout_signal returns CE from both its bullish and its
        bearish branch, so the dashboard's second-candle sell can only come
        from bearish_reversal_signal. Two fixtures are needed, because they are
        mutually exclusive: a third candle that breaks above the second's high
        cannot also close below it.
        """
        d = self._dashboard()
        scanner = d.DashboardScanner()
        spec = d.STRATEGY_BY_KEY["second_candle"]
        module = scanner.registry.module_for(spec)

        # the third candle breaks above the second's high: the breakout's CE
        breakout_up = [
            at(TODAY, 9, 15, 100, 101, 99, 100),
            at(TODAY, 9, 30, 100, 110, 99, 109),
            at(TODAY, 9, 45, 109, 115, 108, 114),
        ]
        # the third candle closes below the second's high and is weak: the PE
        reversal = self._reversal_session()

        with _today_is_the_strategy_module(module, candles_of=lambda cs: cs):
            breakout_signal, _ = module.second_candle_breakout_signal(
                breakout_up, breakout_up[1], breakout_up[2])
            breakout_other, _ = module.second_candle_breakout_signal(
                reversal, reversal[1], reversal[2])
            reversal_signal, _ = module.bearish_reversal_signal(
                reversal, reversal[0], reversal[1], reversal[2])

        self.assertEqual(breakout_signal, "CE")
        self.assertEqual(breakout_other, "NONE")
        self.assertEqual(reversal_signal, "PE")
        # the breakout function produced no sell from either fixture
        self.assertNotIn("PE", (breakout_signal, breakout_other))

    def _reversal_session(self):
        """Two bullish opening candles then a weak third: the PE setup."""
        return [
            at(TODAY, 9, 15, 100, 101, 99, 100.5),
            at(TODAY, 9, 30, 100.5, 110, 100, 109),
            at(TODAY, 9, 45, 109, 109.5, 100, 101),
        ]

    def test_a_weak_sell_is_kept_and_a_strong_one_dropped(self):
        """A close below the previous high survives; above both does not."""
        d = self._dashboard()
        scanner = d.DashboardScanner()
        spec = d.STRATEGY_BY_KEY["second_candle"]
        module = scanner.registry.module_for(spec)
        now = dt.datetime(2026, 1, 2, 15, 30, tzinfo=IST)
        candles = self._reversal_session()

        # previous high far above the close of 101, so the sell is kept
        daily_weak = [at(PRIOR, 9, 15, 100, 400, 95, 105)]
        # previous high below the close, and too little history for ema10
        daily_strong = [at(PRIOR, 9, 15, 100, 98, 95, 97)]

        with _today_is_the_strategy_module(module, candles_of=lambda cs: cs):
            raw = module.bearish_reversal_signal(
                candles, candles[0], candles[1], candles[2])[0]
            kept = scanner._evaluate(spec, module, "NSE:X-EQ", candles, [],
                                     daily_weak, now)
            dropped = scanner._evaluate(spec, module, "NSE:X-EQ", candles, [],
                                        daily_strong, now)

        self.assertEqual(raw, "PE", "the strategy itself should fire")
        self.assertTrue(kept, "a close below the previous high is kept")
        self.assertEqual([o[0] for o in kept], ["PE"])
        self.assertIn("below_previous_high", kept[0][1])
        self.assertTrue(kept[0][1]["below_previous_high"])
        self.assertEqual(dropped, [],
                         "a close above both levels must not be shown")

    def test_the_note_explains_why_the_gate_allowed_the_sell(self):
        """build_cell only carries the note, so the reason must ride on it.

        Without this the user sees a second-candle sell with no way to tell it
        had to clear a gate, and cannot audit why it was let through.
        """
        d = self._dashboard()
        scanner = d.DashboardScanner()
        spec = d.STRATEGY_BY_KEY["second_candle"]
        module = scanner.registry.module_for(spec)
        now = dt.datetime(2026, 1, 2, 15, 30, tzinfo=IST)
        candles = self._reversal_session()
        daily_weak = [at(PRIOR, 9, 15, 100, 400, 95, 105)]
        with _today_is_the_strategy_module(module, candles_of=lambda cs: cs):
            outcomes = scanner._evaluate(spec, module, "NSE:X-EQ", candles, [],
                                         daily_weak, now)
        self.assertEqual(len(outcomes), 1)
        note = outcomes[0][2]
        self.assertIn("previous day's high", note)
        # the strategy's own label is still the head of the note
        self.assertTrue(note.startswith(str(outcomes[0][1].get("strategy"))),
                        f"note should lead with the strategy, got {note!r}")

    def test_the_column_always_sweeps_so_the_judged_bar_sets_the_day(self):
        """Pin why the gate's restrict_to_today flag cannot matter here.

        The column sweeps every 15-minute bar, and the sweep hands each
        evaluator the judged bar's own close time rather than the wall clock.
        The judged bar's day is therefore always the day the gate is asked
        about, so the two gate modes agree. If the spec ever stopped sweeping,
        this would change and the call site's choice would start to count.
        """
        d = self._dashboard()
        spec = d.STRATEGY_BY_KEY["second_candle"]
        self.assertTrue(d.sweeps_session(spec))
        self.assertFalse(spec.session_anchored)
        self.assertEqual(spec.bar_seconds, d.CANDLE_SECONDS)

        # the same expression the sweep uses to build its per-bar instant
        for hour, minute in ((9, 15), (12, 0), (15, 15)):
            bar = at(TODAY, hour, minute, 100, 101, 99, 100)
            started = dt.datetime.fromtimestamp(bar.epoch, IST)
            judged = started + dt.timedelta(seconds=d.CANDLE_SECONDS)
            self.assertEqual(judged.date(), started.date(),
                             f"the {hour:02d}:{minute:02d} bar must close on "
                             f"its own day")

    def test_a_gated_sell_is_absent_from_the_rendered_cell(self):
        """End to end: a blocked sell must leave the cell empty, not marked."""
        d = self._dashboard()
        scanner = d.DashboardScanner()
        spec = d.STRATEGY_BY_KEY["second_candle"]
        module = scanner.registry.module_for(spec)
        now = dt.datetime(2026, 1, 2, 15, 30, tzinfo=IST)
        candles = self._reversal_session()
        daily_strong = [at(PRIOR, 9, 15, 100, 98, 95, 97)]
        with _today_is_the_strategy_module(module, candles_of=lambda cs: cs):
            outcomes = scanner._evaluate(spec, module, "NSE:X-EQ", candles, [],
                                         daily_strong, now)
            raw = module.bearish_reversal_signal(
                candles, candles[0], candles[1], candles[2])[0]
        self.assertEqual(raw, "PE")
        cell = d.build_cell(spec, outcomes, candles)
        self.assertEqual(cell["state"], d.NEUTRAL)
        self.assertEqual(cell["hit_count"], 0)

    def test_the_buy_side_is_never_gated(self):
        """Only sells are held back."""
        d = self._dashboard()
        scanner = d.DashboardScanner()
        spec = d.STRATEGY_BY_KEY["second_candle"]
        module = scanner.registry.module_for(spec)
        now = dt.datetime(2026, 1, 2, 15, 30, tzinfo=IST)
        # bullish second candle, third breaks above its high: a CE
        candles = [
            at(TODAY, 9, 15, 100, 101, 99, 100),
            at(TODAY, 9, 30, 100, 110, 99, 109),
            at(TODAY, 9, 45, 109, 115, 108, 114),
        ]
        # a previous high far below, so any gate would block a sell
        daily = [at(PRIOR, 9, 15, 1, 2, 0.5, 1.5)]
        with _today_is_the_strategy_module(module, candles_of=lambda cs: cs):
            raw = module.second_candle_breakout_signal(
                candles, candles[1], candles[2])[0]
            outcomes = scanner._evaluate(spec, module, "NSE:X-EQ", candles, [],
                                         daily, now)
        self.assertEqual(raw, "CE")
        self.assertTrue(outcomes)
        self.assertEqual(outcomes[0][0], "CE")
        # no gate keys leaked onto a buy
        self.assertNotIn("below_previous_high", outcomes[0][1])

    def test_the_gate_does_not_touch_the_strategys_own_logic(self):
        """The strategy module must still return its PE ungated."""
        d = self._dashboard()
        scanner = d.DashboardScanner()
        spec = d.STRATEGY_BY_KEY["second_candle"]
        module = scanner.registry.module_for(spec)
        candles = self._reversal_session()
        with _today_is_the_strategy_module(module, candles_of=lambda cs: cs):
            signal, details = module.bearish_reversal_signal(
                candles, candles[0], candles[1], candles[2])
        self.assertEqual(signal, "PE")
        # the gate's keys are absent: the strategy knows nothing about them
        self.assertNotIn("below_previous_high", details)
        self.assertNotIn("gate_ema10", details)


if __name__ == "__main__":
    unittest.main()
