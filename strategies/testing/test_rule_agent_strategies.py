"""Tests for rule_agent's knowledge base and its delegated strategy dispatch.

Two things are being pinned here.

The knowledge base must not lie. Every entry says whether the engine can
evaluate it, and a test walks the JSON against the engine's own dispatch and
its if/elif chain, so an entry cannot be marked "wired" without something
actually evaluating it. That check is the one that would have caught
STRAT_002/003/004, which sat in the JSON for weeks with no branch at all.

The dispatch must reach the same verdict as the dashboard. The delegated
strategies run the same source file the dashboard column runs, so a fixture is
built by asking each source where it fires and then checked through the agent,
rather than hand-transcribed from the rule.
"""
import contextlib
import datetime as dt
import importlib.util
import io
import json
import pathlib
import re
import sys
import unittest
from zoneinfo import ZoneInfo

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
AGENT_DIR = REPO_ROOT / "strategies" / "agent"
SCRIPTS_DIR = REPO_ROOT / "strategies" / "scripts"
if str(AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AGENT_DIR))

import rule_agent  # noqa: E402

KNOWLEDGE = AGENT_DIR / "knowledge" / "strategies.json"
IST = ZoneInfo("Asia/Kolkata")
BAR = 15 * 60


def load_json():
    return json.loads(KNOWLEDGE.read_text(encoding="utf-8"))


class Candle:
    """The shape the strategy sources read: epoch, open, high, low, close."""

    __slots__ = ("epoch", "open", "high", "low", "close", "volume")

    def __init__(self, epoch, o, h, l, c, v=1000.0):
        self.epoch, self.open, self.high = epoch, o, h
        self.low, self.close, self.volume = l, c, v


def bar(day, hour, minute, o, h, l, c):
    epoch = int(dt.datetime(day.year, day.month, day.day, hour, minute,
                            tzinfo=IST).timestamp())
    return Candle(epoch, float(o), float(h), float(l), float(c))


def module(name):
    return rule_agent.load_strategy_module(f"test_mod_{name}",
                                           SCRIPTS_DIR / name)


# =============================================================================
# The knowledge base
# =============================================================================
class KnowledgeBaseTests(unittest.TestCase):
    def setUp(self):
        self.data = load_json()
        self.entries = self.data["strategies"]

    def test_every_entry_has_the_fields_the_engine_reads(self):
        required = ("id", "name", "type", "instruments", "timeframe",
                    "confidence", "engine_support")
        for entry in self.entries:
            for key in required:
                self.assertIn(key, entry, f"{entry['id']} has no {key}")

    def test_ids_are_unique(self):
        ids = [e["id"] for e in self.entries]
        self.assertEqual(len(ids), len(set(ids)))

    def test_engine_support_is_one_of_two_words(self):
        for entry in self.entries:
            self.assertIn(entry["engine_support"], ("wired", "not_wired"),
                          f"{entry['id']} says {entry['engine_support']!r}")

    def test_anything_not_wired_says_why(self):
        for entry in self.entries:
            if entry["engine_support"] != "not_wired":
                continue
            blocker = entry.get("engine_blocker")
            self.assertTrue(blocker, f"{entry['id']} is not_wired with no reason")
            self.assertGreater(len(blocker), 20,
                               f"{entry['id']}'s blocker is too vague to act on")

    def _chained_ids(self):
        source = (AGENT_DIR / "rule_agent.py").read_text(encoding="utf-8")
        return set(re.findall(r'strat_id == "(STRAT_[0-9]+)"', source))

    def test_a_wired_entry_is_actually_evaluated(self):
        """The lie detector.

        "wired" has to mean something is really evaluating the strategy:
        either a delegation entry, or a branch in the if/elif chain. This is
        the check that STRAT_002/003/004 would have failed.
        """
        chained = self._chained_ids()
        delegated = set(rule_agent.DELEGATED_STRATEGIES)
        for entry in self.entries:
            if entry["engine_support"] != "wired":
                continue
            self.assertTrue(
                entry["id"] in delegated or entry["id"] in chained,
                f"{entry['id']} claims to be wired but nothing evaluates it")

    def test_a_not_wired_entry_is_skipped_even_if_a_branch_exists(self):
        """The knowledge base wins.

        Three pre-existing entries describe 5-minute strategies and do have an
        if/elif branch, but the fetcher only returns 15-minute candles, so the
        branch would evaluate a different rule. The engine must skip them
        rather than emit an alert for a rule it cannot compute.
        """
        chained = self._chained_ids()
        overlapping = [e["id"] for e in self.entries
                       if e["engine_support"] == "not_wired"
                       and e["id"] in chained]
        self.assertTrue(overlapping, "the fixture assumes at least one such id")
        for strategy_id in overlapping:
            self.assertNotIn(strategy_id, rule_agent.DELEGATED_STRATEGIES)

    def test_a_not_wired_entry_is_not_delegated(self):
        for entry in self.entries:
            if entry["engine_support"] != "not_wired":
                continue
            self.assertNotIn(entry["id"], rule_agent.DELEGATED_STRATEGIES,
                             f"{entry['id']} is not_wired but is delegated")

    def test_every_delegated_strategy_exists_in_the_knowledge_base(self):
        ids = {e["id"] for e in self.entries}
        for strategy_id in rule_agent.DELEGATED_STRATEGIES:
            self.assertIn(strategy_id, ids)

    def test_every_delegated_strategy_names_a_real_file_and_function(self):
        for strategy_id, (file_name, function_name, _) in \
                rule_agent.DELEGATED_STRATEGIES.items():
            path = SCRIPTS_DIR / file_name
            self.assertTrue(path.is_file(), f"{strategy_id}: no {file_name}")
            self.assertTrue(
                hasattr(module(file_name), function_name),
                f"{strategy_id}: {file_name} has no {function_name}")

    def test_every_dashboard_column_is_described(self):
        """All 16 columns the dashboard renders must be in the knowledge base."""
        spec = importlib.util.spec_from_file_location(
            "dash_for_kb_test",
            REPO_ROOT / "strategies" / "ui" / "equity_strategy_dashboard.py")
        dashboard = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = dashboard
        # the dashboard logs on import, so keep the test output clean
        with contextlib.redirect_stdout(io.StringIO()):
            spec.loader.exec_module(dashboard)
        columns = {s.column for s in dashboard.STRATEGY_SPECS}
        described = {e["dashboard_column"] for e in self.entries
                     if e.get("dashboard_column")}
        self.assertEqual(
            columns - described, set(),
            "columns with no strategies.json entry: "
            f"{sorted(columns - described)}")

    def test_the_version_and_date_were_bumped(self):
        self.assertEqual(self.data["last_updated"], "2026-09-26")
        self.assertEqual(self.data["version"], "2.0")


# =============================================================================
# Path resolution
# =============================================================================
class PathTests(unittest.TestCase):
    def test_repo_root_is_the_repository(self):
        """It used to be parents[2], one level too high, so every import of
        fyers_client and common_indicators failed and the agent could not
        start at all."""
        self.assertEqual(rule_agent.REPO_ROOT, REPO_ROOT)
        self.assertTrue((rule_agent.REPO_ROOT / "skills" / "fyers-trading" /
                         "scripts" / "fyers_client.py").is_file())
        self.assertTrue((rule_agent.REPO_ROOT / "strategies" / "utils" /
                         "common_indicators.py").is_file())

    def test_the_strategy_scripts_directory_exists(self):
        self.assertTrue(rule_agent.STRATEGY_SCRIPTS_DIR.is_dir())

    def test_the_module_loader_caches(self):
        first = rule_agent.load_strategy_module(
            "test_cache_probe", SCRIPTS_DIR / "EquityLowerHighCloseSignal15min.py")
        second = rule_agent.load_strategy_module(
            "test_cache_probe", SCRIPTS_DIR / "EquityLowerHighCloseSignal15min.py")
        self.assertIs(first, second)


# =============================================================================
# Adapters
# =============================================================================
class AdapterTests(unittest.TestCase):
    def test_a_buy_string_becomes_a_call(self):
        self.assertEqual(rule_agent._side_signal(("BUY", {"rule": "cross_up"})),
                         ("CE", "cross_up"))

    def test_a_sell_string_becomes_a_put(self):
        self.assertEqual(rule_agent._side_signal(("SELL", {"rule": "cross_down"})),
                         ("PE", "cross_down"))

    def test_ce_and_pe_pass_straight_through(self):
        self.assertEqual(rule_agent._side_signal(("CE", {"rule": "r"})),
                         ("CE", "r"))
        self.assertEqual(rule_agent._side_signal(("PE", {"rule": "r"})),
                         ("PE", "r"))

    def test_none_means_no_signal(self):
        for spelling in ("NONE", "none", "", "FLAT"):
            self.assertIsNone(
                rule_agent._side_signal((spelling, {"reason": "x"})),
                f"{spelling!r} should be silent")

    def test_a_boolean_true_is_a_call_and_false_is_silent(self):
        self.assertEqual(rule_agent._side_bool((True, {"reason": "crossed"})),
                         ("CE", "crossed"))
        self.assertIsNone(rule_agent._side_bool((False, {"reason": "no"})))

    def test_an_unrecognised_side_is_treated_as_no_signal(self):
        """Better to stay quiet than to invent a direction."""
        self.assertIsNone(rule_agent._side_signal(("MAYBE", {})))
        self.assertIsNone(rule_agent._side_signal(("UP", {})))

    def test_a_malformed_return_is_treated_as_no_signal(self):
        for bad in (None, "PE", ("PE",), ("PE", {}, "extra")):
            self.assertIsNone(rule_agent._side_signal(bad))
        self.assertIsNone(rule_agent._side_bool(None))

    def test_the_reason_prefers_the_source_wording(self):
        _, reason = rule_agent._side_signal(
            ("SELL", {"rule": "stack_10_20_30", "reason": "close under ema10"}))
        self.assertEqual(reason, "stack_10_20_30 - close under ema10")

    def test_a_reason_is_always_produced(self):
        for details in ({}, {"reason": "only a reason"}, None, "text"):
            self.assertTrue(rule_agent._reason_from(details, "fallback"))


class JudgedAtTests(unittest.TestCase):
    def test_it_is_the_newest_bars_close_not_the_wall_clock(self):
        candles = [bar(dt.date(2026, 1, 2), 9, 15, 10, 11, 9, 10),
                   bar(dt.date(2026, 1, 2), 9, 30, 10, 11, 9, 10)]
        self.assertEqual(rule_agent.judged_at(candles),
                         dt.datetime(2026, 1, 2, 9, 45, tzinfo=IST))

    def test_it_works_on_a_day_with_no_bars(self):
        """A weekend or holiday still judges the last session's newest bar."""
        candles = [bar(dt.date(2026, 1, 2), 15, 15, 10, 11, 9, 10)]
        judged = rule_agent.judged_at(candles)
        self.assertEqual(judged.date(), dt.date(2026, 1, 2))
        self.assertEqual(judged.strftime("%H:%M"), "15:30")

    def test_it_falls_back_to_now_with_no_candles(self):
        self.assertIsNotNone(rule_agent.judged_at([]))


# =============================================================================
# End to end through the engine
# =============================================================================
def find_firing_index(predicate, series):
    """The first prefix length of `series` on which the predicate is true.

    Derived from the source rather than hand-written, because a hand-typed
    fixture for a four-EMA rule drifts from the indicator the moment the rule
    changes.
    """
    for index in range(len(series)):
        try:
            if predicate(series[:index + 1]):
                return index
        except Exception:
            continue
    return None


def walk(seed=7, count=260):
    """A deterministic price path: long enough for a 50-period EMA, and it
    crosses itself often enough that a crossover rule actually fires."""
    closes = [100.0]
    for i in range(1, count):
        drift = ((i * 37 + seed * 11) % 23 - 11) / 4.0
        closes.append(round(max(5.0, closes[-1] + drift), 2))
    day = dt.date(2025, 10, 1)
    bars = []
    for i, close in enumerate(closes):
        stamp = day + dt.timedelta(days=i)
        opened = round(closes[i - 1] if i else close, 2)
        high = round(max(opened, close) + 0.4, 2)
        low = round(min(opened, close) - 0.4, 2)
        bars.append(bar(stamp, 15, 15, opened, high, low, close))
    return bars


class DelegationTests(unittest.TestCase):
    """Drive the engine the way the dashboard's own columns are driven."""

    def setUp(self):
        self.engine = rule_agent.StrategyEngine()
        self.by_id = {s["id"]: s for s in
                      self.engine.strategies["strategies"]}

    def _chained_ids(self):
        return KnowledgeBaseTests()._chained_ids()

    def _fires(self, strategy_id):
        return lambda window: self.engine._evaluate_strategy(
            self.by_id[strategy_id], "NSE:X-EQ", window) is not None

    def test_ema_10_20_fires_through_the_agent(self):
        series = walk(seed=3)
        index = find_firing_index(self._fires("STRAT_012"), series)
        self.assertIsNotNone(index, "no prefix of the walk made it fire")
        signal = self.engine._evaluate_strategy(
            self.by_id["STRAT_012"], "NSE:X-EQ", series[:index + 1])
        self.assertIn(signal.signal_type, ("CE", "PE"),
                      "ema10:20 can fire either way")
        self.assertEqual(signal.strategy_id, "STRAT_012")
        self.assertTrue(signal.reason, "a signal must say why")

    def test_lower_high_fires_through_the_agent(self):
        # a lower high with the close under the prior close is the rule
        firing = [
            bar(dt.date(2026, 1, 2), 9, 15, 100, 110, 99, 105),
            bar(dt.date(2026, 1, 2), 9, 30, 105, 108, 100, 101),
        ]
        signal = self.engine._evaluate_strategy(
            self.by_id["STRAT_013"], "NSE:X-EQ", firing)
        self.assertIsNotNone(signal)
        self.assertEqual(signal.signal_type, "PE")
        self.assertIn("lower high", signal.reason)

    def test_lower_high_stays_silent_on_a_higher_high(self):
        """The negative case: a higher high is the opposite of the rule, so
        this must not be reported."""
        higher_high = [
            bar(dt.date(2026, 1, 2), 9, 15, 100, 105, 99, 100),
            bar(dt.date(2026, 1, 2), 9, 30, 100, 120, 99, 99),
        ]
        self.assertIsNone(self.engine._evaluate_strategy(
            self.by_id["STRAT_013"], "NSE:X-EQ", higher_high))

    def test_lower_high_needs_only_two_bars(self):
        """The 35-bar guard belongs to the hand-written chain. A two-bar rule
        must not be made to wait for 35."""
        two = [
            bar(dt.date(2026, 1, 2), 9, 15, 100, 110, 99, 105),
            bar(dt.date(2026, 1, 2), 9, 30, 105, 108, 100, 101),
        ]
        self.assertIsNotNone(self.engine._evaluate_strategy(
            self.by_id["STRAT_013"], "NSE:X-EQ", two))

    def test_higher_high_close_fires_through_the_agent(self):
        series = [
            bar(dt.date(2026, 1, 2), 9, 15, 100, 105, 99, 104),
            bar(dt.date(2026, 1, 2), 9, 30, 104, 110, 103, 103),
        ]
        signal = self.engine._evaluate_strategy(
            self.by_id["STRAT_018"], "NSE:X-EQ", series)
        self.assertIsNotNone(signal, "a higher high closing under the prior "
                                     "close is a sell")
        self.assertEqual(signal.signal_type, "PE")

    def test_higher_high_low_needs_the_close_under_the_prior_low(self):
        """The two higher-high variants differ only in depth, so the shallower
        one must not fire when the close is under the prior close but not the
        prior low."""
        prev = bar(dt.date(2026, 1, 2), 9, 15, 100, 105, 99, 104)
        shallow = bar(dt.date(2026, 1, 2), 9, 30, 104, 110, 100, 100.5)
        self.assertIsNone(self.engine._evaluate_strategy(
            self.by_id["STRAT_017"], "NSE:X-EQ", [prev, shallow]),
            "close 100.5 is under prev_close 104 but not under prev_low 99")
        deep = bar(dt.date(2026, 1, 2), 9, 30, 104, 110, 98, 98.5)
        signal = self.engine._evaluate_strategy(
            self.by_id["STRAT_017"], "NSE:X-EQ", [prev, deep])
        self.assertIsNotNone(signal, "close 98.5 is under prev_low 99")
        self.assertEqual(signal.signal_type, "PE")

    def test_a_plain_walk_fires_at_least_one_of_the_ema_strategies(self):
        series = walk(seed=11)
        fired = []
        for strategy_id, want in (("STRAT_012", None), ("STRAT_014", "CE"),
                                  ("STRAT_015", "CE")):
            index = find_firing_index(self._fires(strategy_id), series)
            if index is None:
                continue
            signal = self.engine._evaluate_strategy(
                self.by_id[strategy_id], "NSE:X-EQ", series[:index + 1])
            self.assertIsNotNone(signal)
            if want:
                self.assertEqual(signal.signal_type, want,
                                 f"{strategy_id} is buy-only")
            fired.append(strategy_id)
        self.assertTrue(fired, "no EMA strategy fired on the walk, so the "
                               "test is not proving anything")

    def test_a_flat_market_fires_nothing(self):
        """No crossover can happen on a flat series, so this must be silent."""
        flat = [bar(dt.date(2025, 10, 1) + dt.timedelta(days=i), 15, 15,
                    100, 100, 100, 100) for i in range(60)]
        for strategy_id in rule_agent.DELEGATED_STRATEGIES:
            self.assertIsNone(
                self.engine._evaluate_strategy(
                    self.by_id[strategy_id], "NSE:X-EQ", flat),
                f"{strategy_id} fired on a completely flat series")

    def test_an_empty_series_is_handled_by_every_delegated_strategy(self):
        for strategy_id in rule_agent.DELEGATED_STRATEGIES:
            self.assertIsNone(self.engine._evaluate_strategy(
                self.by_id[strategy_id], "NSE:X-EQ", []))

    def test_a_not_wired_strategy_never_produces_a_signal(self):
        blocked = [e for e in self.engine.strategies["strategies"]
                   if e["engine_support"] == "not_wired"]
        self.assertTrue(blocked, "the fixture assumes some are blocked")
        series = walk(seed=5)
        for entry in blocked:
            self.assertIsNone(
                self.engine._evaluate_strategy(entry, "NSE:X-EQ", series),
                f"{entry['id']} is marked not_wired but produced a signal")

    def test_a_wired_strategy_is_not_skipped_by_the_guard(self):
        """The guard must not swallow the strategies that do work."""
        series = walk(seed=17)
        fired = set()
        for entry in self.engine.strategies["strategies"]:
            if entry["engine_support"] != "wired":
                continue
            if self.engine._evaluate_strategy(entry, "NSE:X-EQ", series):
                fired.add(entry["id"])
        self.assertTrue(fired, "the guard is swallowing every wired strategy")

    def test_the_not_wired_guard_reads_the_entry_not_a_hardcoded_list(self):
        """If the guard stopped consulting engine_support, marking an entry
        not_wired would silently do nothing again.

        So find a bar where the strategy's own branch really does fire, then
        show the guard is what suppresses it, and that lifting the flag brings
        the signal back.
        """
        entries = self.engine.strategies["strategies"]
        target = next(e for e in entries
                      if e["engine_support"] == "not_wired" and e["id"]
                      in self._chained_ids())
        unblocked = {**target, "engine_support": "wired"}
        series = walk(seed=19)
        index = find_firing_index(
            lambda w: self.engine._evaluate_strategy(
                unblocked, "NSE:X-EQ", w) is not None, series)
        self.assertIsNotNone(index, "the fixture never makes it fire, so the "
                                    "test would pass for the wrong reason")
        window = series[:index + 1]

        self.assertIsNotNone(
            self.engine._evaluate_strategy(unblocked, "NSE:X-EQ", window),
            "with the flag lifted the branch must fire")
        self.assertIsNone(
            self.engine._evaluate_strategy(target, "NSE:X-EQ", window),
            "with the flag set, the same bar must be suppressed")

    def test_the_price_on_a_delegated_signal_is_the_newest_close(self):
        series = [
            bar(dt.date(2026, 1, 2), 9, 15, 100, 105, 99, 104),
            bar(dt.date(2026, 1, 2), 9, 30, 104, 110, 103, 103),
        ]
        signal = self.engine._evaluate_strategy(
            self.by_id["STRAT_018"], "NSE:X-EQ", series)
        self.assertEqual(signal.current_price, 103.0)

    def test_a_delegated_signal_alert_message_renders(self):
        series = [
            bar(dt.date(2026, 1, 2), 9, 15, 100, 105, 99, 104),
            bar(dt.date(2026, 1, 2), 9, 30, 104, 110, 103, 103),
        ]
        signal = self.engine._evaluate_strategy(
            self.by_id["STRAT_018"], "NSE:X-EQ", series)
        message = signal.to_alert_message()
        # the alert names the strategy, not the id
        self.assertIn(self.by_id["STRAT_018"]["name"], message)
        self.assertIn("PUT", message)
        self.assertIn("NSE:X-EQ", message)
        self.assertIn("103.00", message)

    def test_evaluate_returns_at_most_one_signal_per_strategy(self):
        series = walk(seed=13)
        signals = self.engine.evaluate("NSE:X-EQ", series)
        ids = [s.strategy_id for s in signals]
        self.assertEqual(len(ids), len(set(ids)))


if __name__ == "__main__":
    unittest.main()
