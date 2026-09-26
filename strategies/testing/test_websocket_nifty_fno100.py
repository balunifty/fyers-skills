"""The websocket collector must end its own process, not idle until killed.

It is run by a scheduled task whose only sign of life is the task state. The
SDK it uses starts non-daemon threads, and its message thread waits on a
condition variable that nothing signals once the socket is gone, so the process
used to sit in Running for the whole weekend: not working, and unable to stop.
These tests pin the two things that fix that, plus the close time they share
with every other script in the repo.
"""

import ast
import datetime as dt
import importlib.util
import pathlib
import re
import sys
import unittest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "strategies" / "utils" / "websocketNiftyfno100.py"

spec = importlib.util.spec_from_file_location("ws_collector", SCRIPT_PATH)
ws = importlib.util.module_from_spec(spec)
# dataclass resolves annotations through sys.modules, so register before exec.
sys.modules[spec.name] = ws
spec.loader.exec_module(ws)

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))


def at(day, hour, minute):
    return dt.datetime(2026, 9, day, hour, minute, tzinfo=IST)


class CloseTimeIsUniformAcrossTheRepoTests(unittest.TestCase):
    """Every script must agree on when the session ends.

    The repo carried three answers at once - 15:15, 15:30 and 15:45 - so a rule
    judged by one script and a price stamped by another could disagree about
    which bar was the last of the day. Each is only a few characters, which is
    exactly why they drifted: nothing failed when one changed.

    The scan is by parsed assignment, not by text, so a commented-out override
    and a mention inside a docstring are not mistaken for the real constant.
    """

    ROOTS = ("strategies", "skills")
    EXPECTED = (15, 30)

    def _declarations(self):
        found = []
        for root in self.ROOTS:
            for path in sorted((REPO_ROOT / root).rglob("*.py")):
                if "testing" in path.parts or "__pycache__" in path.parts:
                    continue
                try:
                    tree = ast.parse(path.read_text(encoding="utf-8"))
                except (OSError, SyntaxError):
                    continue
                relative = path.relative_to(REPO_ROOT)
                for node in ast.walk(tree):
                    if not isinstance(node, ast.Assign):
                        continue
                    names = [t.id for t in node.targets
                             if isinstance(t, ast.Name)]
                    if not any(n in ("MARKET_CLOSE", "market_close")
                               for n in names):
                        continue
                    value = node.value
                    if (isinstance(value, ast.Call)
                            and isinstance(value.func, ast.Attribute)
                            and value.func.attr == "time"
                            and len(value.args) == 2
                            and all(isinstance(a, ast.Constant)
                                    and isinstance(a.value, int)
                                    for a in value.args)):
                        found.append(
                            (str(relative), node.lineno,
                             value.args[0].value, value.args[1].value))
        return found

    def test_the_scan_finds_every_declaration(self):
        """If this finds too few, the test below would pass while checking
        almost nothing."""
        found = self._declarations()
        self.assertGreaterEqual(
            len(found), 12,
            f"expected the session close to be declared across the repo, "
            f"found only {len(found)}: {found}",
        )

    def test_the_scan_reaches_every_directory_that_declares_a_close(self):
        """A count alone would not notice a whole directory going missing."""
        scanned = {path for path, _line, _h, _m in self._declarations()}
        for expected in (
            r"strategies\scripts\OrbStrategyCallPut.py",
            r"strategies\scripts\SecondCandleBreakout.py",
            r"strategies\scripts\IndexRejectionStrategy.py",
            r"strategies\scripts\BuyCallOptionEma10_50Crossover.py",
            r"strategies\scripts\BuyCallOption102030EmaCrossover5min.py",
            r"strategies\scripts\EquityNifty50Ema10crossover50-15min.py",
            r"strategies\scripts\R1PrevHighRejectionStrategy.py",
            r"strategies\agent\rule_agent.py",
            r"strategies\agent\trading_agent.py",
            r"strategies\ui\equity_strategy_dashboard.py",
            r"strategies\utils\websocketNiftyfno100.py",
        ):
            self.assertIn(expected, scanned, f"{expected} was not scanned")

    def test_no_script_declares_a_different_close(self):
        wrong = [
            f"{path}:{line} closes at {hour:02d}:{minute:02d}"
            for path, line, hour, minute in self._declarations()
            if (hour, minute) != self.EXPECTED
        ]
        self.assertEqual(
            wrong, [],
            "the NSE cash session ends at 15:30; these disagree:\n  "
            + "\n  ".join(wrong),
        )

    def test_a_commented_out_override_is_not_mistaken_for_the_constant(self):
        """EquityNifty50Ema10crossover50-15min.py keeps a 23:59 test override.

        It is commented out on purpose, so it must not be read as a second
        session close - and it must stay commented out.
        """
        path = REPO_ROOT / "strategies" / "scripts" / \
            "EquityNifty50Ema10crossover50-15min.py"
        self.assertIn("# MARKET_CLOSE = dt.time(23, 59)", path.read_text("utf-8"))
        for _path, _line, hour, minute in self._declarations():
            self.assertNotEqual((hour, minute), (23, 59))

    def test_a_comment_restating_the_close_agrees_with_the_code(self):
        """The Nifty50 script labels its own constant in a trailing comment.

        A comment is invisible to the parsed scan above, so it can contradict the
        code it sits on without anything failing.
        """
        path = REPO_ROOT / "strategies" / "scripts" / \
            "EquityNifty50Ema10crossover50-15min.py"
        self.assertIn("# IST market close (prod: 15:30)", path.read_text("utf-8"))

    def test_a_comment_deriving_a_time_from_the_close_still_adds_up(self):
        """IndexRejectionStrategy closes at 15:10, described as N min before.

        That comment said "5 min" when the close was 15:15 and became wrong the
        moment the close moved to 15:30. Nothing failed, because a comment
        cannot fail. The arithmetic is checked instead.
        """
        pattern = re.compile(
            r"(?P<hour>\d{1,2}):(?P<minute>\d{2})\s*"
            r"\((?P<gap>\d+)\s*min before the (?P<ch>\d{1,2}):(?P<cm>\d{2}) close\)")
        checked = 0
        for root in self.ROOTS:
            for path in sorted((REPO_ROOT / root).rglob("*.py")):
                if "testing" in path.parts or "__pycache__" in path.parts:
                    continue
                text = path.read_text(encoding="utf-8", errors="replace")
                for match in pattern.finditer(text):
                    checked += 1
                    stated = int(match["hour"]) * 60 + int(match["minute"])
                    close = int(match["ch"]) * 60 + int(match["cm"])
                    self.assertEqual(
                        close - stated, int(match["gap"]),
                        f"{path.name} says {match['hour']}:{match['minute']} is "
                        f"{match['gap']} min before the "
                        f"{match['ch']}:{match['cm']} close, which is not true",
                    )
                    self.assertEqual(
                        (match["ch"], match["cm"]), ("15", "30"),
                        f"{path.name} derives a time from a close that is not "
                        f"15:30",
                    )
        self.assertGreater(checked, 0, "no such comment was found to check")


class SessionPhaseTests(unittest.TestCase):
    def test_the_close_is_the_real_nse_close(self):
        self.assertEqual(ws.MARKET_CLOSE, dt.time(15, 30))

    def test_a_weekday_inside_the_session_is_open(self):
        for moment in (at(24, 9, 15), at(24, 11, 0), at(24, 15, 29)):
            self.assertEqual(ws.session_phase(moment), "open", moment)

    def test_saturday_and_sunday_are_the_weekend(self):
        # 2026-09-26 is a Saturday and 2026-09-27 a Sunday.
        self.assertEqual(at(26, 10, 0).weekday(), 5)
        self.assertEqual(ws.session_phase(at(26, 10, 0)), "weekend")
        self.assertEqual(ws.session_phase(at(27, 10, 0)), "weekend")

    def test_a_weekend_mid_session_is_still_the_weekend(self):
        """The clock must not be able to talk a closed day into streaming."""
        self.assertEqual(ws.session_phase(at(26, 10, 0)), "weekend")

    def test_before_the_open_and_after_the_close_are_distinguished(self):
        self.assertEqual(ws.session_phase(at(24, 9, 14)), "pre-open")
        self.assertEqual(ws.session_phase(at(24, 15, 31)), "closed")

    def test_the_boundaries_are_inclusive(self):
        """A tick stamped exactly at the open or the close still counts."""
        self.assertEqual(ws.session_phase(at(24, 9, 15)), "open")
        self.assertEqual(ws.session_phase(at(24, 15, 30)), "open")

    def test_the_close_is_the_real_nse_close(self):
        """15:45 used to let a quarter-hour past the close through.

        That opened a 15:30 candle in this database that no other script
        produces, because the dashboard and the REST history both stop at 15:30.
        """
        self.assertEqual(ws.MARKET_CLOSE, dt.time(15, 30))
        self.assertEqual(ws.MARKET_OPEN, dt.time(9, 15))


class CloseTimeAgreesWithTheDashboardTests(unittest.TestCase):
    """The collector and the dashboard stamp the same last bar.

    The repo-wide uniformity is asserted by CloseTimeIsUniformAcrossTheRepoTests;
    this keeps the pair that actually shares a candle store honest on its own.
    """

    DASHBOARD = REPO_ROOT / "strategies" / "ui" / "equity_strategy_dashboard.py"

    def test_the_collector_and_the_dashboard_close_at_the_same_minute(self):
        def declared(path):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in tree.body:
                if not isinstance(node, ast.Assign):
                    continue
                if not any(isinstance(t, ast.Name) and t.id == "MARKET_CLOSE"
                           for t in node.targets):
                    continue
                call = node.value
                if (isinstance(call, ast.Call)
                        and isinstance(call.func, ast.Attribute)
                        and call.func.attr == "time"
                        and len(call.args) == 2):
                    return call.args[0].value, call.args[1].value
            self.fail(f"no MARKET_CLOSE = dt.time(h, m) in {path.name}")

        self.assertEqual(
            declared(self.DASHBOARD),
            declared(SCRIPT_PATH),
            "the collector and the dashboard would stamp a different last bar",
        )

    def test_the_collector_uses_the_real_nse_close(self):
        """15:45 used to let a quarter-hour past the close through.

        That opened a 15:30 candle in this database that no other script
        produces, because the dashboard and the REST history both stop at 15:30.
        """
        self.assertEqual(ws.MARKET_CLOSE, dt.time(15, 30))
        self.assertEqual(ws.MARKET_OPEN, dt.time(9, 15))


class ShutdownTests(unittest.TestCase):
    """The process has to be able to finish, and must close the socket to do it."""

    @staticmethod
    def _tree():
        return ast.parse(SCRIPT_PATH.read_text(encoding="utf-8"))

    @classmethod
    def _function(cls, name):
        return next(
            node for node in ast.walk(cls._tree())
            if isinstance(node, ast.FunctionDef) and node.name == name)

    @staticmethod
    def _called_attributes(node):
        return {
            child.func.attr
            for child in ast.walk(node)
            if isinstance(child, ast.Call)
            and isinstance(child.func, ast.Attribute)
        }

    @staticmethod
    def _called_names(node):
        return {
            child.func.id
            for child in ast.walk(node)
            if isinstance(child, ast.Call) and isinstance(child.func, ast.Name)
        }

    def test_the_collector_never_calls_the_nonexistent_disconnect(self):
        """socket.disconnect() does not exist on FyersDataSocket.

        Both calls it used to make raised AttributeError and were swallowed by a
        bare except, so the five-reconnect stop never fired and errors piled up
        past it in the log.
        """
        self.assertNotIn("disconnect", self._called_attributes(self._tree()))

    def test_a_skip_is_written_to_the_log_not_just_stdout(self):
        """pythonw.exe has no console, so a bare print explains nothing.

        The task scheduler can only report that a run happened. The reason it
        produced no candles has to be on disk or the run looks like a failure.
        """
        main = self._function("main")
        self.assertIn("log_event", self._called_names(main))
        source = SCRIPT_PATH.read_text(encoding="utf-8")
        gate = source.index('if phase in ("weekend", "closed"):')
        token = source.index("token = load_token()")
        self.assertIn("log_event(", source[gate:token])

    def test_the_socket_is_closed_before_returning(self):
        source = SCRIPT_PATH.read_text(encoding="utf-8")
        close_at = source.index("socket.close_connection()")
        self.assertIn("while stop_reason[0] is None:", source)
        # The close must come after the wait, not instead of it.
        self.assertGreater(close_at, source.index("while stop_reason[0] is None:"))

    def test_the_run_waits_for_the_session_to_end_rather_than_returning(self):
        """A bare connect() returns at once, leaving the task looking idle."""
        source = SCRIPT_PATH.read_text(encoding="utf-8")
        self.assertIn("socket.connect()", source)
        self.assertIn("time.sleep(15)", source)
        self.assertIn('stop_reason[0] = f"session ended ({phase})"', source)

    def test_a_weekend_run_exits_before_touching_the_token(self):
        """No point loading credentials or dialling the exchange."""
        source = SCRIPT_PATH.read_text(encoding="utf-8")
        gate = source.index('if phase in ("weekend", "closed"):')
        token = source.index("token = load_token()")
        self.assertLess(gate, token)
        self.assertIn("return 0", source[gate:token])

    def test_a_permanently_failing_socket_is_not_left_reconnecting_forever(self):
        """Five errors must end the run, so the repeat can restart it.

        The comparison itself is checked, not just the flag being set: a
        condition that never trips would reconnect until the scheduler's
        12-hour limit killed the process.
        """
        source = SCRIPT_PATH.read_text(encoding="utf-8")
        self.assertIn("MAX_RECONNECTS = 5", source)
        self.assertIn("if reconnect_count[0] >= MAX_RECONNECTS:", source)
        self.assertIn("reconnect_count[0] += 1", source)
        self.assertIn('stop_reason[0] = f"{MAX_RECONNECTS} consecutive errors"',
                      source)

    def test_the_error_callback_does_not_close_the_socket_itself(self):
        """close_connection() joins the thread the callback runs on.

        Checked on the parsed call rather than the text, so the comment
        explaining why cannot be mistaken for the call it warns against.
        """
        callback = self._function("on_error")
        called = self._called_attributes(callback)
        self.assertNotIn("close_connection", called)
        self.assertTrue(
            any(isinstance(node, ast.Assign) for node in ast.walk(callback)),
            "on_error should flag a stop reason for the main thread to act on",
        )


class TickFilterTests(unittest.TestCase):
    """A tick outside the session must not reach the store, whatever the clock.

    The process-level gate stops the collector starting at all, but a tick that
    arrives while running is filtered separately: a socket that delivers a
    weekend or post-close tick would otherwise open a candle nothing else has.
    """

    def _on_message(self):
        tree = ast.parse(SCRIPT_PATH.read_text(encoding="utf-8"))
        return next(
            node for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == "on_message")

    def test_the_tick_handler_defers_to_session_phase(self):
        handler = self._on_message()
        called = {
            child.func.id
            for child in ast.walk(handler)
            if isinstance(child, ast.Call) and isinstance(child.func, ast.Name)
        }
        self.assertIn(
            "session_phase", called,
            "on_message must gate ticks with session_phase, not its own clock "
            "check that can drift from the process-level one",
        )

    def test_the_store_is_only_written_after_the_phase_check(self):
        """A guard that runs after store.update would filter nothing."""
        handler = self._on_message()
        names = [
            child.func.attr
            for child in ast.walk(handler)
            if isinstance(child, ast.Call) and isinstance(child.func, ast.Attribute)
        ]
        self.assertIn("update", names)
        source = ast.unparse(handler)
        self.assertLess(
            source.index("session_phase"), source.index(".update("),
            "store.update runs before the session check",
        )

    def test_out_of_session_ticks_are_dropped(self):
        cases = [
            (at(24, 9, 14), "pre-open"),
            (at(24, 15, 31), "closed"),
            (at(26, 11, 0), "weekend"),
        ]
        for moment, expected in cases:
            self.assertNotEqual(ws.session_phase(moment), "open", moment)
            self.assertEqual(ws.session_phase(moment), expected)

    def test_an_in_session_tick_is_kept(self):
        self.assertEqual(ws.session_phase(at(24, 10, 30)), "open")


if __name__ == "__main__":
    unittest.main()
