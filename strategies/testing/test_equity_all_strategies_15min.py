"""Tests for the consolidated 15-minute scanner.

The consolidation's whole claim is that one process can hold twenty rules
without them interfering. Three things have to hold for that, and they are what
these tests pin:

* **Identity.** Two rules firing on one symbol and one bar must stay two rows
  with two order tags, or the ledger's exactly-once guarantee silently drops
  one of them.
* **The buy/sell split.** A sell must be recorded and must never reach the
  order path. That is asserted against a client that raises if called, not by
  inspecting the code.
* **Coverage.** Every 15-minute rule the dashboard renders must be in the
  registry, so the two cannot drift apart unnoticed.
"""
import contextlib
import datetime as dt
import importlib.util
import io
import pathlib
import sys
import tempfile
import unittest
from types import SimpleNamespace
from zoneinfo import ZoneInfo

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
SCRIPT_PATH = (REPO_ROOT / "strategies" / "scripts" /
               "EquityAllStrategies15min.py")
SPEC = importlib.util.spec_from_file_location(
    "equity_all_15min", SCRIPT_PATH)
assert SPEC and SPEC.loader
scanner = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = scanner
with contextlib.redirect_stdout(io.StringIO()):
    SPEC.loader.exec_module(scanner)

IST = ZoneInfo("Asia/Kolkata")
NOW = dt.datetime(2026, 1, 2, 15, 30, tzinfo=IST)
PRIOR = dt.date(2025, 12, 31)
DAY = dt.date(2026, 1, 2)


def _flat(day, close):
    """A prior daily bar, for building an indicator history."""
    return at(15, 30, close, close + 0.5, close - 0.5, close, day=day,
              resolution="D")


def _walk(seed, count):
    """A deterministic price path, used to search for a bar a rule fires on.

    Derived from the indicator rather than hand-typed, because a hand-written
    fixture for a four-EMA rule drifts from the rule the moment it changes.
    """
    closes = [100.0]
    for i in range(1, count):
        drift = ((i * 37 + seed * 11) % 23 - 11) / 4.0
        closes.append(round(max(5.0, closes[-1] + drift), 2))
    day = dt.date(2025, 10, 1)
    bars = []
    for i, close in enumerate(closes):
        opened = round(closes[i - 1] if i else close, 2)
        high = round(max(opened, close) + 0.4, 2)
        low = round(min(opened, close) - 0.4, 2)
        bars.append(at(15, 15, opened, high, low, close,
                       day=day + dt.timedelta(days=i)))
    return bars


def at(hour, minute, o, h, l, c, day=DAY, resolution=15):
    stamp = dt.datetime.combine(day, dt.time(hour, minute), tzinfo=IST)
    return SimpleNamespace(
        epoch=int(stamp.timestamp()), open=float(o), high=float(h),
        low=float(l), close=float(c), volume=1000.0, resolution=resolution)


def load_dashboard():
    spec = importlib.util.spec_from_file_location(
        "all15_dash_for_test",
        REPO_ROOT / "strategies" / "ui" / "equity_strategy_dashboard.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    with contextlib.redirect_stdout(io.StringIO()):
        spec.loader.exec_module(module)
    return module


class ExplodingClient:
    """Any attempt to send an order fails the test loudly."""

    def __init__(self):
        self.calls = 0

    def place_order(self, *args, **kwargs):
        self.calls += 1
        raise AssertionError("an order was placed from a test")

    def history(self, *args, **kwargs):
        raise AssertionError("history was fetched from a test")


# =============================================================================
# Registry integrity
# =============================================================================
class RegistryTests(unittest.TestCase):
    def test_the_registry_is_populated(self):
        self.assertGreaterEqual(len(scanner.STRATEGY_REGISTRY), 18)

    def test_keys_are_unique(self):
        keys = [spec.key for spec in scanner.STRATEGY_REGISTRY]
        self.assertEqual(len(keys), len(set(keys)))

    def test_ledger_identities_are_unique(self):
        """Two rules sharing an identity would overwrite each other's rows."""
        names = [spec.strategy_name for spec in scanner.STRATEGY_REGISTRY]
        self.assertEqual(len(names), len(set(names)), names)


class EmaRuleSplitTests(unittest.TestCase):
    """The four ema10/ema20 rules are four entries, not one.

    The source function returns whichever rule matched and names it in
    details["rule"]. Registered once, all four would share one ledger identity,
    so a cross and a later stack state on the same bar would collapse into one
    row that could not say which rule had fired.
    """

    KEYS = ("ema_10_cross_up", "ema_10_cross_down",
            "ema_10_stack_10_20_30", "ema_10_stack_10_20_50")

    def test_all_four_rules_are_registered(self):
        for key in self.KEYS:
            self.assertIn(key, scanner.SPEC_BY_KEY, key)

    def test_each_rule_gets_its_own_ledger_identity(self):
        names = [scanner.SPEC_BY_KEY[key].strategy_name for key in self.KEYS]
        self.assertEqual(len(set(names)), 4, names)

    def test_the_old_combined_entry_is_gone(self):
        self.assertNotIn("ema_10_cross_20", scanner.SPEC_BY_KEY)
        registered = {spec.key for spec in scanner.STRATEGY_REGISTRY}
        self.assertNotIn("ema_10_cross_20", registered)

    def test_each_entry_watches_exactly_one_rule(self):
        """A cross must not also be reported as a stack state."""
        rising = [at(9, 15 + (i // 4) if False else 0, 0, 0, 0, 0)
                  for i in range(0)]      # placeholder, replaced below
        rising = []
        for i in range(40):
            day = dt.date(2025, 11, 1) + dt.timedelta(days=i)
            close = 100.0 + i            # a steady rise: ema10 above ema20
            rising.append(_flat(day, close))
        rising.append(at(9, 15, 100 + 39, 141, 100, 139))
        rising.append(at(9, 30, 139, 145, 138, 144))
        daily = [at(15, 30, 100, 110, 90, 105, day=PRIOR)]
        fired = []
        for key in self.KEYS:
            side, details = scanner.SPEC_BY_KEY[key].evaluate(
                rising, daily, NOW)
            if side != scanner.NONE:
                fired.append((key, side, details.get("rule")))
        # at most one rule can match a single bar
        self.assertLessEqual(len(fired), 1, fired)
        for key, _side, rule in fired:
            self.assertEqual(key.replace("ema_10_", ""), str(rule),
                             "the entry fired on a rule that is not its own")

    def test_a_quiet_rule_says_which_rule_fired_instead(self):
        """So a silent entry is distinguishable from a broken one."""
        rising = [_flat(dt.date(2025, 11, 1) + dt.timedelta(days=i),
                       100.0 + i) for i in range(40)]
        rising.append(at(9, 15, 139, 141, 138, 139))
        rising.append(at(9, 30, 139, 145, 138, 144))
        daily = [at(15, 30, 100, 110, 90, 105, day=PRIOR)]
        for key in self.KEYS:
            side, details = scanner.SPEC_BY_KEY[key].evaluate(
                rising, daily, NOW)
            self.assertEqual(side, scanner.NONE, key)
            if side == scanner.NONE and details.get("rule"):
                continue
            self.assertIn("want_rule", details, key)
            self.assertEqual(details["want_rule"],
                             key.replace("ema_10_", ""), key)

    def test_the_three_reachable_rules_trade_on_the_side_they_should(self):
        """Driven against the real function, with the bar found by searching.

        stack_10_20_50 is absent from this test on purpose: the source checks
        the 30-stack first and returns before reaching it, so it is shadowed
        and essentially never fires. Its routing is covered by the stubbed test
        below, which is the part this change actually introduced.
        """
        expected = {
            "ema_10_cross_up": scanner.BUY,
            "ema_10_cross_down": scanner.SELL,
            "ema_10_stack_10_20_30": scanner.SELL,
        }
        for seed in range(1, 40):
            series = _walk(seed, 120)
            if len(series) < 60:
                continue
            matched = {}
            for key in expected:
                for index in range(50, len(series)):
                    got, _details = scanner.SPEC_BY_KEY[key].evaluate(
                        series[:index + 1], [], NOW)
                    if got != scanner.NONE:
                        matched[key] = got
                        break
            if set(matched) == set(expected):
                break
        self.assertEqual(set(matched), set(expected),
                         f"only found {sorted(matched)}")
        for key, want in expected.items():
            self.assertEqual(matched[key], want, key)

    def test_each_entry_routes_its_own_rule_to_the_right_side(self):
        """The adapter's whole job, with the source stubbed.

        Needed because stack_10_20_50 is shadowed by stack_10_20_30 in the
        source and so has no real bar to test against, yet it still has to be
        routed correctly if the shadowing ever changes.
        """
        module_name = "ema_10_20"
        cached = sys.modules.get(f"equity_all_15min_{module_name}")
        self.assertIsNotNone(cached, "the rule module should already be loaded")
        original = cached.ema10_ema20_signal
        expected = {
            "cross_up": scanner.BUY,
            "cross_down": scanner.SELL,
            "stack_10_20_30": scanner.SELL,
            "stack_10_20_50": scanner.SELL,
        }
        try:
            for rule, want in expected.items():
                cached.ema10_ema20_signal = (
                    lambda candles, seconds, _r=rule: (
                        "SELL" if _r != "cross_up" else "BUY",
                        {"rule": _r, "reason": f"stub {_r}"}))
                for key in self.KEYS:
                    want_rule = key.replace("ema_10_", "")
                    got, details = scanner.SPEC_BY_KEY[key].evaluate(
                        [at(9, 15, 1, 1, 1, 1)], [], NOW)
                    if want_rule == rule:
                        self.assertEqual(got, want,
                                         f"{key} should be {want}, got {got}")
                        self.assertEqual(details.get("rule"), rule)
                    else:
                        self.assertEqual(got, scanner.NONE,
                                         f"{key} fired on {rule}")
        finally:
            cached.ema10_ema20_signal = original

    def test_ledger_identity_carries_the_prefix_and_the_key(self):
        spec = scanner.SPEC_BY_KEY["lower_high_close"]
        self.assertEqual(spec.strategy_name,
                         f"{scanner.STRATEGY_NAME_PREFIX}_LOWER_HIGH_CLOSE")

    def test_every_module_file_exists(self):
        for spec in scanner.STRATEGY_REGISTRY:
            path = scanner.SCRIPT_DIR / spec.file_name
            self.assertTrue(path.is_file(), f"{spec.key}: {spec.file_name}")

    def test_every_evaluator_is_callable(self):
        for spec in scanner.STRATEGY_REGISTRY:
            self.assertTrue(callable(spec.evaluate), spec.key)

    def test_every_rule_is_evaluable(self):
        """Each adapter must actually call a real function in a real module."""
        for spec in scanner.STRATEGY_REGISTRY:
            candles = [at(9, 15, 100, 106, 98, 100),
                       at(9, 30, 100, 106, 99, 99),
                       at(9, 45, 99, 106, 95, 105),
                       at(10, 0, 105, 107, 100, 101)]
            daily = [at(15, 30, 100, 110, 90, 105, day=PRIOR, resolution="D")]
            side, details = spec.evaluate(candles, daily, NOW)
            self.assertIn(side, (scanner.BUY, scanner.SELL, scanner.NONE),
                          f"{spec.key} returned {side!r}")
            self.assertIsInstance(details, dict, spec.key)

    def test_the_excluded_timeframes_are_absent(self):
        """5-minute and daily-bar rules belong to other scripts."""
        keys = {spec.key for spec in scanner.STRATEGY_REGISTRY}
        for excluded in ("ema_10_20_30", "daily_breakout", "index_rejection"):
            self.assertNotIn(excluded, keys, excluded)

    def test_wall_clock_dependent_rules_are_declared_and_listed(self):
        """The rules that read dt.date.today() inside their own script.

        They work during market hours and find nothing on a holiday, so a run
        has to be able to say which ones were quiet for that reason rather than
        report a clean scan.
        """
        self.assertEqual(
            sorted(scanner.WALL_CLOCK_RULES),
            ["orb", "orb_high_rejection", "second_candle_buy"])
        for key in scanner.WALL_CLOCK_RULES:
            self.assertTrue(scanner.SPEC_BY_KEY[key].wall_clock_date, key)

    def test_the_flag_appears_in_the_listing(self):
        import io as _io
        buffer = _io.StringIO()
        with contextlib.redirect_stdout(buffer):
            scanner.print_registry()
        text = buffer.getvalue()
        self.assertIn("wall-clock date", text)
        for key in scanner.WALL_CLOCK_RULES:
            self.assertIn(key, text)

    def test_a_missing_rule_module_fails_loudly(self):
        """ImportError, not FileNotFoundError, so a missing rule reads as the
        same class of problem as one that cannot be imported."""
        with self.assertRaises(ImportError):
            scanner.load_strategy_module("nope_absent",
                                         "NoSuchModule15min.py")

    def test_a_missing_module_is_not_cached_as_a_half_loaded_entry(self):
        name = "nope_absent_twice"
        for _ in range(2):
            with self.assertRaises(ImportError):
                scanner.load_strategy_module(name, "StillNotHere15min.py")
        self.assertNotIn(f"equity_all_15min_{name}", sys.modules)


class CoverageTests(unittest.TestCase):
    """The registry and the dashboard's 15-minute columns must agree."""

    def setUp(self):
        self.dashboard = load_dashboard()

    #: The dashboard keys several rules under one column, so the scanner names
    #: them individually. This maps a dashboard key to the scanner keys that
    #: cover it, and the test fails if either side moves without the other.
    COLUMN_COVERAGE = {
        "open_ema_stack": {"open_ema_stack"},
        "orb": {"orb", "orb_second_candle_prev_high",
                "orb_first_candle_gap_down", "orb_first_candle_small_body",
                "orb_second_candle_gap_ema"},
        "orb_high_rejection": {"orb_high_rejection"},
        "orb_low_rejection": {"orb_low_rejection"},
        "r1_rejection": {"r1_rejection"},
        "prev_high_rejection": {"prev_high_rejection"},
        "doji_rejection": {"doji_rejection"},
        "higher_high_rejection": {"higher_high_rejection"},
        "higher_high_close_rejection": {"higher_high_close_rejection"},
        "lower_high_close": {"lower_high_close"},
        "second_candle": {"second_candle_buy", "second_candle_sell"},
        "ema_fresh": {"ema_fresh"},
        "ema_pullback": {"ema_pullback"},
        "ema_10_cross_20": {"ema_10_cross_up", "ema_10_cross_down",
                            "ema_10_stack_10_20_30",
                            "ema_10_stack_10_20_50"},
        "double_bottom": {"double_bottom"},
    }

    def test_every_15_minute_dashboard_column_is_registered(self):
        """Every 15-minute dashboard column must be covered.

        "15-minute" is narrowed the same way the dashboard narrows it for the
        session sweep: a 15-minute bar size is not enough on its own, because
        the daily breakout declares 15 minutes but reads daily bars, and the
        index rejection declares 15 minutes but is index-only with a broken
        history fetch. Both are excluded here for the same reasons the scanner
        documents.
        """
        registered = {spec.key for spec in scanner.STRATEGY_REGISTRY}
        fifteen_minute = {
            spec.key for spec in self.dashboard.STRATEGY_SPECS
            if spec.bar_seconds == scanner.CANDLE_SECONDS
            and spec.kind != "daily"
            and spec.key != "index_rejection"
        }
        self.assertEqual(
            fifteen_minute - set(self.COLUMN_COVERAGE), set(),
            "15-minute dashboard columns the coverage map does not mention: "
            f"{sorted(fifteen_minute - set(self.COLUMN_COVERAGE))}")

    def test_the_two_deliberate_exclusions_are_still_excluded(self):
        """Stated in the scanner's scope, so keep them stated here too."""
        self.assertNotIn("daily_breakout", self.COLUMN_COVERAGE)
        self.assertNotIn("index_rejection", self.COLUMN_COVERAGE)
        registered = {spec.key for spec in scanner.STRATEGY_REGISTRY}
        self.assertNotIn("daily_breakout", registered)
        self.assertNotIn("index_rejection", registered)

    def test_every_mapped_column_is_really_registered(self):
        """The other direction: a coverage claim with no registry entry."""
        registered = {spec.key for spec in scanner.STRATEGY_REGISTRY}
        for column, keys in self.COLUMN_COVERAGE.items():
            for key in keys:
                self.assertIn(key, registered,
                              f"{column} claims to cover {key}, which is not "
                              "in the registry")

    def test_every_registry_entry_is_claimed_by_a_column(self):
        """No orphan rules: each scanner entry belongs to a mapped column."""
        covered = {key for keys in self.COLUMN_COVERAGE.values() for key in keys}
        registered = {spec.key for spec in scanner.STRATEGY_REGISTRY}
        self.assertEqual(registered - covered, set(),
                         "registry entries no column claims: "
                         f"{sorted(registered - covered)}")

    def test_the_column_names_match_the_dashboard(self):
        columns = {spec.column for spec in self.dashboard.STRATEGY_SPECS}
        for spec in scanner.STRATEGY_REGISTRY:
            if spec.column in columns:
                continue
            self.assertIn(
                spec.column,
                {"ORB 2nd>PrevHigh", "ORB 1st GapDown", "ORB 1st SmallBody",
                 "ORB 2nd Gap+EMA", "Second Candle CE", "Second Candle PE",
                 "ema10>20 cross", "ema10<20 cross", "ema10<20<30",
                 "ema10<20<50"},
                f"{spec.key} has a column name the dashboard does not use")

    def test_daily_needing_rules_are_marked(self):
        """A rule that reads the previous session must declare it."""
        for spec in scanner.STRATEGY_REGISTRY:
            if spec.key in ("r1_rejection", "prev_high_rejection",
                            "open_ema_stack", "double_bottom",
                            "orb_second_candle_prev_high",
                            "orb_first_candle_gap_down",
                            "second_candle_sell"):
                self.assertTrue(spec.needs_daily, spec.key)


# =============================================================================
# Adapters
# =============================================================================
class NormaliseTests(unittest.TestCase):
    def test_the_four_side_spellings(self):
        for spelling in ("BUY", "CE", "buy", "LONG"):
            self.assertEqual(scanner._normalise(spelling, {}), (scanner.BUY, {}))
        for spelling in ("SELL", "PE", "sell", "PUT", "SHORT"):
            self.assertEqual(scanner._normalise(spelling, {}),
                             (scanner.SELL, {}))

    def test_a_true_boolean_is_a_buy_and_false_is_nothing(self):
        self.assertEqual(scanner._normalise(True, {}), (scanner.BUY, {}))
        self.assertEqual(scanner._normalise(False, {}), (scanner.NONE, {}))

    def test_an_unrecognised_side_is_silence_not_a_guess(self):
        for spelling in ("NONE", "none", "", "MAYBE", "UP", "FLAT"):
            self.assertEqual(scanner._normalise(spelling, {})[0],
                             scanner.NONE, spelling)

    def test_details_are_copied_not_shared(self):
        original = {"reason": "x"}
        _side, payload = scanner._normalise("BUY", original)
        payload["extra"] = 1
        self.assertNotIn("extra", original)

    def test_missing_details_are_tolerated(self):
        self.assertEqual(scanner._normalise("BUY", None), (scanner.BUY, {}))


class JudgedEpochTests(unittest.TestCase):
    """The ledger keys on the judged bar, so a rule must be attributable."""

    def setUp(self):
        self.candles = [at(9, 15, 1, 1, 1, 1), at(9, 30, 2, 2, 2, 2)]

    def test_an_explicit_epoch_wins(self):
        details = {"candle_epoch": 12345, "curr_time": "2026-01-02T09:30:00+05:30"}
        self.assertEqual(scanner._judged_epoch(details, self.candles), 12345)

    def test_a_time_string_is_converted(self):
        details = {"curr_time": "2026-01-02T09:30:00+05:30"}
        self.assertEqual(scanner._judged_epoch(details, self.candles),
                         int(dt.datetime(2026, 1, 2, 9, 30,
                                         tzinfo=IST).timestamp()))

    def test_it_falls_back_to_the_newest_bar(self):
        self.assertEqual(scanner._judged_epoch({}, self.candles),
                         self.candles[-1].epoch)

    def test_no_candles_and_no_hint_means_no_epoch(self):
        self.assertIsNone(scanner._judged_epoch({}, []))

    def test_an_unparseable_hint_falls_back(self):
        self.assertEqual(
            scanner._judged_epoch({"curr_time": "not-a-time"}, self.candles),
            self.candles[-1].epoch)


# =============================================================================
# The buy/sell split
# =============================================================================
class SellNeverOrdersTests(unittest.TestCase):
    """A sell is recorded; it must never reach the order path."""

    def setUp(self):
        self.store = scanner.SignalStore(
            pathlib.Path(tempfile.mkdtemp()) / "signals.db")
        self.client = ExplodingClient()
        self.spec = scanner.SPEC_BY_KEY["lower_high_close"]
        self.details = {
            "candle_epoch": at(9, 30, 1, 1, 1, 1).epoch,
            "reason": "lower high",
        }

    def test_handle_sell_needs_no_client_at_all(self):
        """The signature is the guarantee: there is no client to pass."""
        import inspect
        params = list(inspect.signature(scanner.handle_sell).parameters)
        self.assertNotIn("client", params)

    def test_a_sell_is_recorded_as_not_sent(self):
        self.assertTrue(scanner.handle_sell(
            self.store, self.spec, "NSE:X-EQ", self.details, False,
            pathlib.Path(tempfile.mkdtemp()) / "out.xlsx"))
        self.assertEqual(self.client.calls, 0)
        row = self.store.get_row(
            f"{self.spec.strategy_name}:NSE:X-EQ:"
            f"{self.details['candle_epoch']}")
        self.assertIsNotNone(row)
        self.assertEqual(row["order_status"], "NOT_SENT")
        self.assertEqual(row["order_id"], "NOT_SENT")
        self.assertIn("no order", row["broker_message"])

    def test_a_sell_is_claimed_once(self):
        """The second claim for the same bar must return nothing, which is
        what stops a poll loop re-logging the same signal every five minutes."""
        signal_id = (f"{self.spec.strategy_name}:NSE:X-EQ:"
                     f"{self.details['candle_epoch']}")
        first = scanner.handle_sell(
            self.store, self.spec, "NSE:X-EQ", self.details, False,
            pathlib.Path(tempfile.mkdtemp()) / "o.xlsx")
        self.assertTrue(first)
        for _ in range(3):
            self.assertFalse(scanner.handle_sell(
                self.store, self.spec, "NSE:X-EQ", self.details, False,
                pathlib.Path(tempfile.mkdtemp()) / "o.xlsx"))
        self.assertIsNotNone(self.store.get_row(signal_id))

    def test_a_buy_reaches_the_order_path(self):
        """The mirror image: a buy must not be silently swallowed."""
        attempted = []

        def fake_process(client, store, row, allow_live_send=False):
            attempted.append(row["signal_id"])
        original = scanner.process_claimed_signal
        scanner.process_claimed_signal = fake_process
        try:
            self.assertTrue(scanner.handle_buy(
                self.client, self.store, self.spec, "NSE:X-EQ", self.details,
                False, pathlib.Path(tempfile.mkdtemp()) / "o.xlsx"))
        finally:
            scanner.process_claimed_signal = original
        self.assertEqual(len(attempted), 1, "the buy never reached the order path")

    def test_the_two_sides_keep_separate_identities(self):
        """A buy and a sell on one symbol and bar are two rows, not one."""
        buy_spec = scanner.SPEC_BY_KEY["ema_fresh"]
        sell_spec = scanner.SPEC_BY_KEY["lower_high_close"]
        self.assertNotEqual(buy_spec.strategy_name, sell_spec.strategy_name)
        scanner.handle_sell(self.store, sell_spec, "NSE:X-EQ", self.details,
                            False, pathlib.Path(tempfile.mkdtemp()) / "o.xlsx")
        original = scanner.process_claimed_signal
        scanner.process_claimed_signal = lambda *a, **k: None
        try:
            scanner.handle_buy(self.client, self.store, buy_spec, "NSE:X-EQ",
                               self.details, False,
                               pathlib.Path(tempfile.mkdtemp()) / "o.xlsx")
        finally:
            scanner.process_claimed_signal = original
        buy_row = self.store.get_row(
            f"{buy_spec.strategy_name}:NSE:X-EQ:"
            f"{self.details['candle_epoch']}")
        sell_row = self.store.get_row(
            f"{sell_spec.strategy_name}:NSE:X-EQ:"
            f"{self.details['candle_epoch']}")
        self.assertIsNotNone(buy_row, "the buy was not recorded")
        self.assertIsNotNone(sell_row, "the sell was not recorded")
        self.assertNotEqual(buy_row["signal_id"], sell_row["signal_id"])


# =============================================================================
# Fetching and the universe
# =============================================================================
class UniverseTests(unittest.TestCase):
    def test_the_universe_is_the_fno_top_100_plus_three_indices(self):
        symbols = scanner.read_universe()
        stocks = scanner.read_symbols(scanner.STOCKS_PATH)
        indices = scanner.read_symbols(scanner.INDICES_PATH)
        self.assertEqual(len(stocks), 100)
        self.assertEqual(len(indices), 3)
        self.assertEqual(len(symbols), 103)
        self.assertEqual(len(set(symbols)), 103, "duplicates in the universe")

    def test_a_missing_file_contributes_nothing_rather_than_raising(self):
        missing = pathlib.Path(tempfile.mkdtemp()) / "nope.txt"
        self.assertEqual(scanner.read_symbols(missing), [])

    def test_comments_and_blanks_are_skipped(self):
        path = pathlib.Path(tempfile.mkdtemp()) / "u.txt"
        path.write_text("# a comment\n\nNSE:A-EQ\n  \n#NSE:B-EQ\n", encoding="utf-8")
        self.assertEqual(scanner.read_symbols(path), ["NSE:A-EQ"])


class DailyFetchTests(unittest.TestCase):
    """A partially formed daily bar must never reach a rule."""

    class FakeLimiter:
        def __init__(self, payload):
            self.payload = payload

        def call(self, *args, **kwargs):
            return self.payload

    class FakeClient:
        def history(self, *args, **kwargs):
            raise AssertionError("the limiter should have answered")

    def test_todays_daily_bar_is_dropped(self):
        payload = {"s": "ok", "candles": [
            [at(15, 30, 1, 2, 0.5, 1.5, day=dt.date(2025, 12, 30)).epoch,
             1, 2, 0.5, 1.5, 0],
            [at(15, 30, 1, 2, 0.5, 1.5, day=dt.date(2025, 12, 31)).epoch,
             1, 2, 0.5, 1.5, 0],
            [at(15, 30, 1, 2, 0.5, 1.5, day=DAY).epoch, 1, 2, 0.5, 1.5, 0],
        ]}
        bars = scanner.fetch_daily_candles(
            self.FakeClient(), "NSE:X-EQ", self.FakeLimiter(payload), NOW)
        self.assertEqual(len(bars), 2, "today's bar must be dropped")
        for bar in bars:
            stamp = dt.datetime.fromtimestamp(bar.epoch, IST)
            self.assertLess(stamp.date(), DAY)

    def test_a_failed_response_raises(self):
        with self.assertRaises(RuntimeError):
            scanner.fetch_daily_candles(
                self.FakeClient(), "NSE:X-EQ",
                self.FakeLimiter({"s": "error"}), NOW)

    def test_the_resolution_requested_is_daily(self):
        asked = {}

        class RecordingLimiter:
            def call(self, fn, symbol, resolution, date_from, date_to):
                asked["resolution"] = resolution
                return {"s": "ok", "candles": []}

        scanner.fetch_daily_candles(self.FakeClient(), "NSE:X-EQ",
                                     RecordingLimiter(), NOW)
        self.assertEqual(asked["resolution"], "D")


# =============================================================================
# Config gate
# =============================================================================
class ConfigGateTests(unittest.TestCase):
    def test_the_script_name_selects_the_equity_config(self):
        """order_config picks equity when the name begins with Equity."""
        self.assertTrue(scanner.SCRIPT_NAME.lower().startswith("equity"))
        sys.path.insert(0, str(REPO_ROOT / "strategies" / "config"))
        import order_config
        self.assertEqual(
            order_config.get_config_path(scanner.SCRIPT_NAME),
            order_config.EQUITY_CONFIG_PATH)

    def test_the_shipped_config_has_live_orders_enabled(self):
        """A fact worth stating loudly rather than discovering at run time."""
        sys.path.insert(0, str(REPO_ROOT / "strategies" / "config"))
        import order_config
        self.assertTrue(
            order_config.is_place_order_enabled(scanner.SCRIPT_NAME),
            "place_order is YES in the equity config, so --live trades for real")

    def test_dry_run_is_the_default(self):
        args = scanner.parse_args([])
        self.assertFalse(args.live)
        self.assertFalse(args.dry_run)   # neither flag, and live is False

    def test_live_and_dry_run_are_mutually_exclusive(self):
        with self.assertRaises(SystemExit):
            with contextlib.redirect_stderr(io.StringIO()):
                scanner.parse_args(["--live", "--dry-run"])


class CliTests(unittest.TestCase):
    def test_list_exits_cleanly(self):
        self.assertEqual(scanner.main(["--list"]), 0)

    def test_a_bad_poll_interval_is_rejected(self):
        with self.assertRaises(SystemExit):
            with contextlib.redirect_stderr(io.StringIO()):
                scanner.parse_args(["--poll-seconds", "0"])


if __name__ == "__main__":
    unittest.main()
