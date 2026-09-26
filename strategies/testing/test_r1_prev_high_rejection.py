import datetime as dt
import importlib.util
import json
import pathlib
import sys
import tempfile
import unittest
from unittest.mock import patch

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
MODULE_PATH = REPO_ROOT / "strategies" / "scripts" / "R1PrevHighRejectionStrategy.py"
sys.path.insert(0, str(REPO_ROOT / "skills" / "fyers-trading" / "scripts"))
sys.path.insert(0, str(REPO_ROOT / "strategies" / "config"))
sys.path.insert(0, str(REPO_ROOT / "strategies" / "utils"))
SPEC = importlib.util.spec_from_file_location("r1_prev_high_rejection", MODULE_PATH)
assert SPEC and SPEC.loader
strategy = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = strategy
SPEC.loader.exec_module(strategy)

TZ = strategy.MARKET_TIMEZONE
R1 = strategy.LEVEL_R1
PREV_HIGH = strategy.LEVEL_PREV_HIGH


def at(day, hour, minute, o, h, low, c, volume=1000):
    stamp = dt.datetime.combine(day, dt.time(hour, minute), tzinfo=TZ)
    return strategy.Candle(
        epoch=int(stamp.timestamp()), open=o, high=h, low=low, close=c, volume=volume
    )


def sessions_before(today, count=4):
    """Previous weekday sessions, oldest first."""
    days, day = [], today - dt.timedelta(days=1)
    while len(days) < count:
        if day.weekday() < 5:
            days.append(day)
        day -= dt.timedelta(days=1)
    return list(reversed(days))


def daily_series(today, high=110.0, low=100.0, close=105.0, count=4):
    return [
        at(day, 15, 30, 100.0, high, low, close)
        for day in sessions_before(today, count)
    ]


def intraday(today, high, low, close, bars=4):
    """`bars` consecutive 15-minute bars starting at 09:15."""
    start = dt.datetime.combine(today, dt.time(9, 15), tzinfo=TZ)
    out = []
    for i in range(bars):
        stamp = start + dt.timedelta(minutes=15 * i)
        out.append(strategy.Candle(
            epoch=int(stamp.timestamp()), open=close, high=high, low=low,
            close=close, volume=1000,
        ))
    return out


class R1FormulaTests(unittest.TestCase):
    def test_classic_r1_is_two_pivot_minus_low(self):
        # P = (110 + 100 + 105) / 3 = 105 -> R1 = 2*105 - 100 = 110
        self.assertAlmostEqual(strategy.classic_r1(110, 100, 105), 110.0)

    def test_r1_matches_a_known_pivot_table(self):
        # H=2250 L=2175 C=2200 -> P=2208.33 -> R1 = 2241.67
        self.assertAlmostEqual(strategy.classic_r1(2250, 2175, 2200), 2241.666666, places=4)

    def test_resolve_level_selects_the_right_level(self):
        reference = strategy.Candle(epoch=0, open=1, high=112.0, low=98.0, close=104.0, volume=1)
        self.assertAlmostEqual(strategy.resolve_level(R1, reference), 111.3333333, places=4)
        self.assertEqual(strategy.resolve_level(PREV_HIGH, reference), 112.0)


class PreviousSessionTests(unittest.TestCase):
    def test_previous_session_skips_today_and_weekends(self):
        today = dt.date(2026, 1, 5)  # Monday
        daily = [
            at(dt.date(2026, 1, 1), 15, 30, 1, 100, 90, 95),   # Thursday
            at(dt.date(2026, 1, 2), 15, 30, 1, 110, 100, 105),  # Friday
            at(today, 15, 30, 1, 115, 105, 112),               # today (must be ignored)
        ]
        found = strategy.previous_session(daily, today)
        self.assertEqual(dt.datetime.fromtimestamp(found.epoch, TZ).date(),
                         dt.date(2026, 1, 2))

    def test_previous_session_returns_none_without_history(self):
        today = dt.date(2026, 1, 5)
        self.assertIsNone(strategy.previous_session([], today))
        only_today = [at(today, 15, 30, 1, 100, 90, 95)]
        self.assertIsNone(strategy.previous_session(only_today, today))


class RejectionSignalTests(unittest.TestCase):
    def setUp(self):
        self.today = dt.date(2026, 1, 2)
        self.now = dt.datetime.combine(self.today, dt.time(11, 0), tzinfo=TZ)
        # previous session H=110 L=100 C=105 -> P=105 -> R1=110, prevHigh=110
        self.daily = daily_series(self.today, 110, 100, 105)

    def test_rejection_above_r1_returns_pe(self):
        bars = intraday(self.today, 113.0, 105.0, 107.0)
        signal, details = strategy.level_rejection_signal(bars, self.daily, R1, self.now)

        self.assertEqual(signal, "PE")
        self.assertEqual(details["level"], 110.0)
        self.assertEqual(details["previous_day"],
                         self.daily[-1] and dt.datetime.fromtimestamp(
                             self.daily[-1].epoch, TZ).date().isoformat())

    def test_bounce_below_r1_returns_ce(self):
        bars = intraday(self.today, 111.0, 104.0, 110.5)
        signal, details = strategy.level_rejection_signal(bars, self.daily, R1, self.now)

        self.assertEqual(signal, "CE")
        self.assertEqual(details["level"], 110.0)

    def test_previous_high_rejection_returns_pe(self):
        bars = intraday(self.today, 113.0, 105.0, 107.0)
        signal, details = strategy.level_rejection_signal(
            bars, self.daily, PREV_HIGH, self.now)

        self.assertEqual(signal, "PE")
        self.assertEqual(details["level"], 110.0)
        self.assertEqual(details["level_kind"], PREV_HIGH)

    def test_no_signal_when_the_level_is_never_tested(self):
        bars = intraday(self.today, 104.0, 99.0, 103.0)
        signal, details = strategy.level_rejection_signal(bars, self.daily, R1, self.now)
        self.assertEqual(signal, "NONE")
        self.assertEqual(details["level"], 110.0)

    def test_a_graze_of_the_level_is_not_a_rejection(self):
        # high only just touches 110 and the close stays above it
        bars = intraday(self.today, 110.01, 106.0, 109.0)
        signal, _ = strategy.level_rejection_signal(bars, self.daily, R1, self.now)
        self.assertEqual(signal, "NONE")

    def test_minimum_breach_is_required(self):
        # 0.04% breach is below MIN_REJECTION_PERCENT (0.05%)
        level = 110.0
        shallow_high = level * (1 + 0.0004)
        bars = intraday(self.today, shallow_high, 106.0, 107.0)
        signal, _ = strategy.level_rejection_signal(bars, self.daily, R1, self.now)
        self.assertEqual(signal, "NONE", "a shallow poke must not count as rejection")

    def test_requires_enough_completed_bars_today(self):
        bars = intraday(self.today, 113.0, 105.0, 107.0, bars=2)
        signal, details = strategy.level_rejection_signal(bars, self.daily, R1, self.now)
        self.assertEqual(signal, "NONE")
        self.assertIn("completed bars today", details["reason"])

    def test_bars_from_a_previous_day_are_ignored(self):
        yesterday = self.today - dt.timedelta(days=1)
        bars = [at(yesterday, 11, 0, 106, 113, 105, 107)] * 4
        signal, details = strategy.level_rejection_signal(bars, self.daily, R1, self.now)
        self.assertEqual(signal, "NONE")
        self.assertIn("completed bars today", details["reason"])

    def test_missing_daily_history_returns_none(self):
        bars = intraday(self.today, 113.0, 105.0, 107.0)
        signal, details = strategy.level_rejection_signal(bars, [], R1, self.now)
        self.assertEqual(signal, "NONE")
        self.assertIn("insufficient candles", details["reason"])

    def test_r1_and_prev_high_diverge_when_low_is_below_high(self):
        # H=112 L=98 C=104 -> R1=111.33, prevHigh=112.00
        daily = daily_series(self.today, 112, 98, 104)
        bars = intraday(self.today, 111.8, 110.0, 111.0)
        r1_signal, _ = strategy.level_rejection_signal(bars, daily, R1, self.now)
        high_signal, _ = strategy.level_rejection_signal(bars, daily, PREV_HIGH, self.now)
        self.assertEqual(r1_signal, "PE")
        self.assertEqual(high_signal, "NONE")


class CandleParsingTests(unittest.TestCase):
    def test_a_bar_closes_exactly_on_the_boundary_and_is_kept(self):
        today = dt.date(2026, 1, 2)
        now = dt.datetime.combine(today, dt.time(10, 0), tzinfo=TZ)
        rows = [
            [int(dt.datetime.combine(today, dt.time(9, 15), tzinfo=TZ).timestamp()),
             100, 101, 99, 100, 10],
            [int(dt.datetime.combine(today, dt.time(9, 30), tzinfo=TZ).timestamp()),
             100, 101, 99, 100, 10],
            # closes exactly at 10:00, so it is complete and must be kept
            [int(dt.datetime.combine(today, dt.time(9, 45), tzinfo=TZ).timestamp()),
             100, 105, 99, 104, 10],
        ]
        candles = strategy.fetch_candles(
            _FakeClient({"s": "ok", "candles": rows}), strategy.ApiRateLimiter(1000),
            "NSE:X-EQ", "15", 5, now,
        )
        self.assertEqual(len(candles), 3)
        self.assertEqual(dt.datetime.fromtimestamp(candles[-1].epoch, TZ).strftime("%H:%M"),
                         "09:45")

    def test_a_still_forming_bar_is_dropped(self):
        today = dt.date(2026, 1, 2)
        now = dt.datetime.combine(today, dt.time(9, 59), tzinfo=TZ)
        rows = [
            [int(dt.datetime.combine(today, dt.time(9, 15), tzinfo=TZ).timestamp()),
             100, 101, 99, 100, 10],
            [int(dt.datetime.combine(today, dt.time(9, 30), tzinfo=TZ).timestamp()),
             100, 101, 99, 100, 10],
            # closes at 10:00, still forming at 09:59
            [int(dt.datetime.combine(today, dt.time(9, 45), tzinfo=TZ).timestamp()),
             100, 105, 99, 104, 10],
        ]
        candles = strategy.fetch_candles(
            _FakeClient({"s": "ok", "candles": rows}), strategy.ApiRateLimiter(1000),
            "NSE:X-EQ", "15", 5, now,
        )
        self.assertEqual(len(candles), 2)
        self.assertEqual(dt.datetime.fromtimestamp(candles[-1].epoch, TZ).strftime("%H:%M"),
                         "09:30")

    def test_daily_bars_for_today_are_dropped(self):
        today = dt.date(2026, 1, 2)
        now = dt.datetime.combine(today, dt.time(10, 0), tzinfo=TZ)
        rows = [
            [int(dt.datetime.combine(today - dt.timedelta(days=1), dt.time(15, 30),
                                     tzinfo=TZ).timestamp()), 100, 110, 100, 105, 10],
            [int(dt.datetime.combine(today, dt.time(15, 30), tzinfo=TZ).timestamp()),
             105, 115, 104, 112, 10],
        ]
        candles = strategy.fetch_candles(
            _FakeClient({"s": "ok", "candles": rows}), strategy.ApiRateLimiter(1000),
            "NSE:X-EQ", "D", 5, now,
        )
        self.assertEqual(len(candles), 1)
        self.assertEqual(candles[0].close, 105.0)

    def test_failed_history_raises(self):
        with self.assertRaises(RuntimeError):
            strategy.fetch_candles(
                _FakeClient({"s": "error", "candles": []}),
                strategy.ApiRateLimiter(1000), "NSE:X-EQ", "15", 5,
                dt.datetime(2026, 1, 2, 10, 0, tzinfo=TZ),
            )


class _FakeClient:
    def __init__(self, response, held=None):
        self.response = response
        self.orders = []
        self.quotes_map = {}
        # (symbol, netQty) pairs the broker reports as open
        self.held = list(held or [])

    def history(self, *args, **kwargs):
        return self.response

    def quotes(self, symbols):
        return {"s": "ok", "d": [{"v": {"lp": self.quotes_map.get(symbols[0], 0)}}]}

    def positions(self):
        return {"netPositions": [
            {"symbol": sym, "netQty": qty} for sym, qty in self.held
        ]}

    def place_order(self, order, dry_run=True, meta=None, **kwargs):
        self.orders.append((order, dry_run, meta))
        return {"s": "dry_run" if dry_run else "ok", "id": f"ord-{len(self.orders)}"}


class StrikeTests(unittest.TestCase):
    def test_round_to_strike(self):
        self.assertEqual(strategy.round_to_strike(2478.0, 50), 2500)
        self.assertEqual(strategy.round_to_strike(2475.0, 50), 2500)
        self.assertEqual(strategy.round_to_strike(2474.0, 50), 2450)

    def test_strike_step_from_chain(self):
        chains = [{"strike_price": s} for s in (2400, 2450, 2500, 2550)]
        self.assertEqual(strategy.get_strike_step(chains), 50)

    def test_strike_step_falls_back_when_chain_is_thin(self):
        self.assertEqual(strategy.get_strike_step([{"strike_price": 2400}]), 50)
        self.assertEqual(strategy.get_strike_step([]), 50)


class StopLossTests(unittest.TestCase):
    def test_stop_uses_tighter_of_fixed_and_trailing(self):
        with tempfile.TemporaryDirectory() as tmp:
            positions_path = pathlib.Path(tmp) / "positions.json"
            positions_path.write_text(json.dumps({
                "p1": {"symbol": "NSE:OPT", "entry_price": 100.0, "highest_ltp": 100.0}
            }))
            client = _FakeClient({"s": "ok"}, held=[("NSE:OPT", 150)])
            client.quotes_map["NSE:OPT"] = 100.0

            with (
                patch.object(strategy, "POSITIONS_PATH", positions_path),
                patch.object(strategy, "LIVE", False),
                patch.object(strategy, "LOG_PATH", pathlib.Path(tmp) / "s.log"),
                patch.object(strategy, "get_stop_loss_percent", return_value=0.015),
                patch.object(strategy, "get_trailing_stop_loss_percent", return_value=0.005),
            ):
                strategy.monitor_stop_loss(client, strategy.ApiRateLimiter(1000))

            # 100 <= 100 * (1 - 1.5%) = 98.5 is False, so no exit yet
            self.assertEqual(client.orders, [])
            self.assertIn("p1", json.loads(positions_path.read_text()))

    def test_stop_triggers_and_forgets_the_position(self):
        with tempfile.TemporaryDirectory() as tmp:
            positions_path = pathlib.Path(tmp) / "positions.json"
            positions_path.write_text(json.dumps({
                "p1": {"symbol": "NSE:OPT", "entry_price": 100.0, "highest_ltp": 100.0}
            }))
            client = _FakeClient({"s": "ok"}, held=[("NSE:OPT", 150)])
            client.quotes_map["NSE:OPT"] = 97.0

            with (
                patch.object(strategy, "POSITIONS_PATH", positions_path),
                patch.object(strategy, "LIVE", False),
                patch.object(strategy, "LOG_PATH", pathlib.Path(tmp) / "s.log"),
                patch.object(strategy, "ENTERED_PATH", pathlib.Path(tmp) / "entered.json"),
                patch.object(strategy, "get_stop_loss_percent", return_value=0.015),
                patch.object(strategy, "get_trailing_stop_loss_percent", return_value=0.005),
            ):
                strategy.monitor_stop_loss(client, strategy.ApiRateLimiter(1000))

            self.assertEqual(len(client.orders), 1)
            order, dry_run, meta = client.orders[0]
            self.assertEqual(order["side"], -1)
            self.assertEqual(order["type"], 2)
            self.assertTrue(dry_run)
            self.assertEqual(meta["signal"], "EXIT")
            self.assertEqual(json.loads(positions_path.read_text()), {})

    def test_trailing_stop_follows_the_peak_not_the_entry(self):
        with tempfile.TemporaryDirectory() as tmp:
            positions_path = pathlib.Path(tmp) / "positions.json"
            # the option peaked at 120, well above the 100 entry
            positions_path.write_text(json.dumps({
                "p1": {"symbol": "NSE:OPT", "entry_price": 100.0, "highest_ltp": 120.0}
            }))
            client = _FakeClient({"s": "ok"}, held=[("NSE:OPT", 150)])
            # 5% trailing from the 120 peak sits at 114, so 113 breaches it
            client.quotes_map["NSE:OPT"] = 113.0

            with (
                patch.object(strategy, "POSITIONS_PATH", positions_path),
                patch.object(strategy, "LIVE", False),
                patch.object(strategy, "LOG_PATH", pathlib.Path(tmp) / "s.log"),
                patch.object(strategy, "ENTERED_PATH", pathlib.Path(tmp) / "entered.json"),
                patch.object(strategy, "get_stop_loss_percent", return_value=0.015),
                patch.object(strategy, "get_trailing_stop_loss_percent", return_value=0.05),
            ):
                strategy.monitor_stop_loss(client, strategy.ApiRateLimiter(1000))

            self.assertEqual(len(client.orders), 1,
                             "trailing stop from the 120 peak should have fired at 113")
            self.assertEqual(json.loads(positions_path.read_text()), {})

    def test_fixed_stop_does_not_fire_while_trailing_is_looser(self):
        with tempfile.TemporaryDirectory() as tmp:
            positions_path = pathlib.Path(tmp) / "positions.json"
            positions_path.write_text(json.dumps({
                "p1": {"symbol": "NSE:OPT", "entry_price": 100.0, "highest_ltp": 100.0}
            }))
            client = _FakeClient({"s": "ok"}, held=[("NSE:OPT", 150)])
            # 1.5% fixed stop = 98.5; a very wide 25% trailing stop = 75, so the
            # fixed stop is tighter and must govern
            client.quotes_map["NSE:OPT"] = 98.0

            with (
                patch.object(strategy, "POSITIONS_PATH", positions_path),
                patch.object(strategy, "LIVE", False),
                patch.object(strategy, "LOG_PATH", pathlib.Path(tmp) / "s.log"),
                patch.object(strategy, "ENTERED_PATH", pathlib.Path(tmp) / "entered.json"),
                patch.object(strategy, "get_stop_loss_percent", return_value=0.015),
                patch.object(strategy, "get_trailing_stop_loss_percent", return_value=0.25),
            ):
                strategy.monitor_stop_loss(client, strategy.ApiRateLimiter(1000))

            self.assertEqual(len(client.orders), 1)

    def test_highest_ltp_is_persisted_when_price_rises(self):
        with tempfile.TemporaryDirectory() as tmp:
            positions_path = pathlib.Path(tmp) / "positions.json"
            positions_path.write_text(json.dumps({
                "p1": {"symbol": "NSE:OPT", "entry_price": 100.0, "highest_ltp": 100.0}
            }))
            client = _FakeClient({"s": "ok"}, held=[("NSE:OPT", 150)])
            client.quotes_map["NSE:OPT"] = 110.0

            with (
                patch.object(strategy, "POSITIONS_PATH", positions_path),
                patch.object(strategy, "LIVE", False),
                patch.object(strategy, "LOG_PATH", pathlib.Path(tmp) / "s.log"),
                patch.object(strategy, "get_stop_loss_percent", return_value=0.015),
                patch.object(strategy, "get_trailing_stop_loss_percent", return_value=0.005),
            ):
                strategy.monitor_stop_loss(client, strategy.ApiRateLimiter(1000))

            stored = json.loads(positions_path.read_text())
            self.assertEqual(stored["p1"]["highest_ltp"], 110.0)
            self.assertEqual(client.orders, [], "well above the stop, so no exit")


class StateTests(unittest.TestCase):
    def test_entered_state_round_trips(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "entered.json"
            with patch.object(strategy, "ENTERED_PATH", path):
                self.assertEqual(strategy.load_entered(), set())
                strategy.save_entered({"NSE:A-EQ", "NSE:B-EQ"})
                self.assertEqual(strategy.load_entered(), {"NSE:A-EQ", "NSE:B-EQ"})

    def test_corrupt_state_files_fall_back_to_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            broken = pathlib.Path(tmp) / "broken.json"
            broken.write_text("{not json")
            with patch.object(strategy, "ENTERED_PATH", broken), \
                 patch.object(strategy, "POSITIONS_PATH", broken):
                self.assertEqual(strategy.load_entered(), set())
                self.assertEqual(strategy.load_positions(), {})

    def test_entered_is_cleared_on_a_new_day(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "entered.json"
            path.write_text(json.dumps(["2020-01-01 NSE:OLD-EQ"]))
            with patch.object(strategy, "ENTERED_PATH", path):
                strategy.clear_entered_on_new_day()
                self.assertEqual(json.loads(path.read_text()), [])

    def test_entered_is_kept_within_the_same_day(self):
        with tempfile.TemporaryDirectory() as tmp:
            today = dt.date.today().isoformat()
            path = pathlib.Path(tmp) / "entered.json"
            path.write_text(json.dumps([f"{today} NSE:TODAY-EQ"]))
            with patch.object(strategy, "ENTERED_PATH", path):
                strategy.clear_entered_on_new_day()
                self.assertEqual(len(json.loads(path.read_text())), 1)

    def test_read_stocks_skips_comments_and_blanks(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "stocks.txt"
            path.write_text("# comment\n\nNSE:A-EQ\nNSE:B-EQ\nNSE:A-EQ\n")
            with patch.object(strategy, "STOCKS_PATH", path):
                self.assertEqual(strategy.read_stocks(), ["NSE:A-EQ", "NSE:B-EQ"])


class SafetyTests(unittest.TestCase):
    def test_dry_run_is_the_default(self):
        self.assertFalse(strategy.LIVE, "new strategies must default to dry-run")

    def test_order_tag_is_within_the_broker_limit(self):
        tag = f"r1_{PREV_HIGH}_{'pe'}"[:20]
        self.assertLessEqual(len(tag), 20)

    def test_freshness_never_places_an_order(self):
        source = MODULE_PATH.read_text(encoding="utf-8")
        self.assertIn("dry_run=not LIVE", source)
        self.assertIn("require_place_order_enabled", (
            REPO_ROOT / "strategies" / "config" / "order_config.py"
        ).read_text(encoding="utf-8"))

    def test_minimum_bars_guard_is_configured(self):
        self.assertGreaterEqual(strategy.MIN_BARS_TODAY, 3)
        self.assertGreater(strategy.MIN_REJECTION_PERCENT, 0)


class DojiDetectionTests(unittest.TestCase):
    def test_a_small_body_relative_to_range_is_a_doji(self):
        bar = strategy.Candle(epoch=0, open=100.0, high=110.0, low=90.0,
                              close=101.0, volume=1)
        # body 1.0 / range 20.0 = 5% < 10%
        self.assertTrue(strategy.is_doji(bar))

    def test_a_large_body_is_not_a_doji(self):
        bar = strategy.Candle(epoch=0, open=100.0, high=110.0, low=90.0,
                              close=108.0, volume=1)
        self.assertFalse(strategy.is_doji(bar))

    def test_exactly_at_the_threshold_is_not_a_doji(self):
        bar = strategy.Candle(epoch=0, open=100.0, high=110.0, low=90.0,
                              close=102.0, volume=1)
        # body 2.0 / range 20.0 = 10%, and the test is strictly below
        self.assertFalse(strategy.is_doji(bar))

    def test_zero_range_bar_is_not_a_doji(self):
        flat = strategy.Candle(epoch=0, open=100.0, high=100.0, low=100.0,
                               close=100.0, volume=1)
        self.assertFalse(strategy.is_doji(flat))

    def test_no_upper_wick_detection(self):
        exact = strategy.Candle(epoch=0, open=100.0, high=100.0, low=98.0,
                                close=99.0, volume=1)
        self.assertTrue(strategy.has_no_upper_wick(exact))
        wick = strategy.Candle(epoch=0, open=100.0, high=102.0, low=98.0,
                               close=99.0, volume=1)
        self.assertFalse(strategy.has_no_upper_wick(wick))
        # a 0.01% overhang is still treated as no wick
        tiny = strategy.Candle(epoch=0, open=100.0, high=100.01, low=98.0,
                               close=99.0, volume=1)
        self.assertTrue(strategy.has_no_upper_wick(tiny))


class DojiRejectionSignalTests(unittest.TestCase):
    def setUp(self):
        self.today = dt.date(2026, 1, 2)
        self.now = dt.datetime.combine(self.today, dt.time(11, 0), tzinfo=TZ)
        # a doji: O=105 H=110 L=100 C=106  (body 1 / range 10 = 10%? -> use 106.5)
        self.doji = strategy.Candle(
            epoch=int(dt.datetime.combine(self.today, dt.time(10, 15), tzinfo=TZ).timestamp()),
            open=105.0, high=110.0, low=100.0, close=106.0, volume=1000)
        # body 1.0 / range 10.0 = 10% -> NOT a doji at the strict threshold
        self.doji = strategy.Candle(
            epoch=int(dt.datetime.combine(self.today, dt.time(10, 15), tzinfo=TZ).timestamp()),
            open=105.0, high=110.0, low=100.0, close=105.5, volume=1000)
        # body 0.5 / range 10.0 = 5% -> a doji

    def _bars(self, current_bar, filler_bars=2):
        base = int(dt.datetime.combine(self.today, dt.time(9, 15), tzinfo=TZ).timestamp())
        filler = [
            strategy.Candle(epoch=base + 900 * i, open=103.0, high=104.0, low=102.0,
                            close=103.5, volume=1000)
            for i in range(filler_bars)
        ]
        # filler then the doji then the current bar
        return filler + [self.doji, current_bar]

    def test_doji_breakout_fail_is_the_most_specific_trigger(self):
        # opens at/above the doji high, no upper wick, closes below the doji low
        bar = strategy.Candle(
            epoch=int(dt.datetime.combine(self.today, dt.time(10, 30), tzinfo=TZ).timestamp()),
            open=110.0, high=110.0, low=99.0, close=99.5, volume=1000)
        signal, details = strategy.doji_rejection_signal(self._bars(bar), self.now)

        self.assertEqual(signal, "PE")
        self.assertEqual(details["trigger"], strategy.DOJI_BREAKOUT_FAIL)
        self.assertTrue(details["opens_above_doji_high"])
        self.assertTrue(details["no_upper_wick"])

    def test_doji_open_high_trigger(self):
        # no upper wick and closes below the doji low, but did not open above
        bar = strategy.Candle(
            epoch=int(dt.datetime.combine(self.today, dt.time(10, 30), tzinfo=TZ).timestamp()),
            open=104.0, high=104.0, low=98.0, close=99.0, volume=1000)
        signal, details = strategy.doji_rejection_signal(self._bars(bar), self.now)

        self.assertEqual(signal, "PE")
        self.assertEqual(details["trigger"], strategy.DOJI_OPEN_HIGH)
        self.assertFalse(details["opens_above_doji_high"])

    def test_doji_rejection_trigger_without_wick_requirement(self):
        # has an upper wick, so the first two cannot match
        bar = strategy.Candle(
            epoch=int(dt.datetime.combine(self.today, dt.time(10, 30), tzinfo=TZ).timestamp()),
            open=104.0, high=111.0, low=99.0, close=99.5, volume=1000)
        signal, details = strategy.doji_rejection_signal(self._bars(bar), self.now)

        self.assertEqual(signal, "PE")
        self.assertEqual(details["trigger"], strategy.DOJI_REJECTION)
        self.assertFalse(details["no_upper_wick"])

    def test_no_signal_when_the_close_holds_above_the_doji_low(self):
        bar = strategy.Candle(
            epoch=int(dt.datetime.combine(self.today, dt.time(10, 30), tzinfo=TZ).timestamp()),
            open=104.0, high=105.0, low=101.0, close=103.0, volume=1000)
        signal, details = strategy.doji_rejection_signal(self._bars(bar), self.now)
        self.assertEqual(signal, "NONE")
        self.assertIn("no doji rejection trigger matched", details["reason"])

    def test_no_signal_when_the_previous_bar_is_not_a_doji(self):
        strong = strategy.Candle(
            epoch=self.doji.epoch, open=100.0, high=110.0, low=99.0, close=109.0, volume=1)
        bar = strategy.Candle(
            epoch=int(dt.datetime.combine(self.today, dt.time(10, 30), tzinfo=TZ).timestamp()),
            open=110.0, high=110.0, low=98.0, close=99.0, volume=1000)
        signal, details = strategy.doji_rejection_signal(
            self._bars(bar)[:-1] + [strong, bar], self.now)
        self.assertEqual(signal, "NONE")
        self.assertIn("not a doji", details["reason"])
        self.assertIn("doji_body_ratio", details)

    def test_no_signal_without_enough_bars_today(self):
        bar = strategy.Candle(
            epoch=int(dt.datetime.combine(self.today, dt.time(10, 30), tzinfo=TZ).timestamp()),
            open=110.0, high=110.0, low=99.0, close=99.5, volume=1000)
        signal, details = strategy.doji_rejection_signal([self.doji, bar], self.now)
        self.assertEqual(signal, "NONE")
        self.assertIn("completed bars", details["reason"])

    def test_bars_from_a_previous_day_are_ignored_when_restricted(self):
        yesterday = self.today - dt.timedelta(days=1)
        stale = [
            strategy.Candle(
                epoch=int(dt.datetime.combine(yesterday, dt.time(10, 30), tzinfo=TZ).timestamp()),
                open=110.0, high=110.0, low=99.0, close=99.5, volume=1000)
        ]
        signal, _ = strategy.doji_rejection_signal([self.doji] + stale, self.now)
        self.assertEqual(signal, "NONE")

    def test_unrestricted_mode_uses_the_newest_bars_regardless_of_date(self):
        yesterday = self.today - dt.timedelta(days=1)
        stale_doji = strategy.Candle(
            epoch=int(dt.datetime.combine(yesterday, dt.time(10, 15), tzinfo=TZ).timestamp()),
            open=105.0, high=110.0, low=100.0, close=105.5, volume=1000)
        stale_bar = strategy.Candle(
            epoch=int(dt.datetime.combine(yesterday, dt.time(10, 30), tzinfo=TZ).timestamp()),
            open=110.0, high=110.0, low=99.0, close=99.5, volume=1000)
        signal, details = strategy.doji_rejection_signal(
            [stale_doji, stale_bar], self.now, restrict_to_today=False)
        self.assertEqual(signal, "PE")
        self.assertEqual(details["trigger"], strategy.DOJI_BREAKOUT_FAIL)

    def test_every_trigger_buys_pe_only(self):
        epoch = int(dt.datetime.combine(
            self.today, dt.time(10, 30), tzinfo=TZ).timestamp())
        for bar in (
            strategy.Candle(epoch=epoch, open=110.0, high=110.0, low=99.0, close=99.5, volume=1),
            strategy.Candle(epoch=epoch, open=104.0, high=104.0, low=98.0, close=99.0, volume=1),
            strategy.Candle(epoch=epoch, open=104.0, high=111.0, low=99.0, close=99.5, volume=1),
        ):
            signal, details = strategy.doji_rejection_signal(self._bars(bar), self.now)
            self.assertEqual(signal, "PE", details.get("reason", ""))
            self.assertIn(details["trigger"], (
                strategy.DOJI_BREAKOUT_FAIL,
                strategy.DOJI_OPEN_HIGH,
                strategy.DOJI_REJECTION,
            ))

    def test_details_report_the_signal_bar_time_for_the_dashboard(self):
        bar = strategy.Candle(
            epoch=int(dt.datetime.combine(self.today, dt.time(10, 30), tzinfo=TZ).timestamp()),
            open=110.0, high=110.0, low=99.0, close=99.5, volume=1000)
        _, details = strategy.doji_rejection_signal(self._bars(bar), self.now)
        self.assertIn("curr_time", details)
        self.assertTrue(details["curr_time"].startswith("2026-01-02T10:30"))


class HigherHighRejectionTests(unittest.TestCase):
    def setUp(self):
        self.today = dt.date(2026, 1, 2)
        self.now = dt.datetime.combine(self.today, dt.time(11, 0), tzinfo=TZ)
        base = int(dt.datetime.combine(self.today, dt.time(9, 15), tzinfo=TZ).timestamp())
        self.prev_epoch = base + 900 * 2      # 09:45
        self.curr_epoch = base + 900 * 3      # 10:00
        self.filler = [
            strategy.Candle(epoch=base + 900 * i, open=103.0, high=104.0, low=102.0,
                            close=103.5, volume=1000)
            for i in range(2)
        ]

    def _prev(self, o, h, low, c):
        return strategy.Candle(epoch=self.prev_epoch, open=o, high=h, low=low,
                               close=c, volume=1000)

    def _curr(self, o, h, low, c):
        return strategy.Candle(epoch=self.curr_epoch, open=o, high=h, low=low,
                               close=c, volume=1000)

    def _bars(self, previous, current):
        return self.filler + [previous, current]

    def test_higher_high_closing_below_prev_low_is_pe(self):
        bars = self._bars(self._prev(100.0, 110.0, 99.0, 105.0),
                          self._curr(104.0, 112.0, 98.0, 98.5))
        signal, details = strategy.higher_high_low_rejection_signal(bars, self.now)

        self.assertEqual(signal, "PE")
        self.assertEqual(details["trigger"], "HIGHER_HIGH_LOW_CLOSE")
        self.assertEqual(details["threshold"], "prev_low")
        self.assertTrue(details["higher_high"])
        self.assertTrue(details["close_below"])

    def test_close_below_prev_close_but_above_prev_low_only_triggers_the_loose_variant(self):
        # prev low 99, prev close 105; current closes 102 -> below close, above low
        bars = self._bars(self._prev(100.0, 110.0, 99.0, 105.0),
                          self._curr(104.0, 112.0, 101.0, 102.0))

        strict, strict_details = strategy.higher_high_low_rejection_signal(bars, self.now)
        loose, loose_details = strategy.higher_high_close_rejection_signal(bars, self.now)

        self.assertEqual(strict, "NONE")
        self.assertIn("did not fall below", strict_details["reason"])
        self.assertEqual(loose, "PE")
        self.assertEqual(loose_details["trigger"], "HIGHER_HIGH_CLOSE")
        self.assertEqual(loose_details["threshold"], "prev_close")

    def test_no_signal_when_the_high_is_not_higher(self):
        bars = self._bars(self._prev(100.0, 115.0, 99.0, 105.0),
                          self._curr(104.0, 112.0, 98.0, 98.5))
        signal, details = strategy.higher_high_low_rejection_signal(bars, self.now)

        self.assertEqual(signal, "NONE")
        self.assertIn("not a higher high", details["reason"])

    def test_no_signal_when_the_close_holds_up(self):
        bars = self._bars(self._prev(100.0, 110.0, 99.0, 105.0),
                          self._curr(104.0, 112.0, 100.0, 104.0))
        signal, _ = strategy.higher_high_low_rejection_signal(bars, self.now)
        self.assertEqual(signal, "NONE")

    def test_equal_highs_do_not_count_as_a_higher_high(self):
        bars = self._bars(self._prev(100.0, 110.0, 99.0, 105.0),
                          self._curr(104.0, 110.0, 98.0, 98.5))
        signal, details = strategy.higher_high_low_rejection_signal(bars, self.now)
        self.assertEqual(signal, "NONE")
        self.assertIn("not a higher high", details["reason"])

    def test_both_variants_are_sell_only(self):
        bars = self._bars(self._prev(100.0, 110.0, 99.0, 105.0),
                          self._curr(104.0, 112.0, 98.0, 98.5))
        self.assertEqual(strategy.higher_high_low_rejection_signal(bars, self.now)[0], "PE")
        self.assertEqual(strategy.higher_high_close_rejection_signal(bars, self.now)[0], "PE")
        # a bar that only fails the strict test still buys PE in both
        bars2 = self._bars(self._prev(100.0, 110.0, 99.0, 105.0),
                           self._curr(104.0, 112.0, 101.0, 102.0))
        self.assertEqual(strategy.higher_high_close_rejection_signal(bars2, self.now)[0], "PE")

    def test_needs_enough_bars_today(self):
        bars = self._bars(self._prev(100.0, 110.0, 99.0, 105.0),
                          self._curr(104.0, 112.0, 98.0, 98.5))
        signal, details = strategy.higher_high_low_rejection_signal(
            [bars[-2], bars[-1]], self.now)
        self.assertEqual(signal, "NONE")
        self.assertIn("completed bars", details["reason"])

    def test_stale_bars_are_ignored_when_restricted(self):
        yesterday = self.today - dt.timedelta(days=1)

        def stale_bar(hour, minute, o, h, low, c):
            stamp = dt.datetime.combine(yesterday, dt.time(hour, minute), tzinfo=TZ)
            return strategy.Candle(epoch=int(stamp.timestamp()), open=o, high=h,
                                   low=low, close=c, volume=1000)

        stale = [
            stale_bar(9, 45, 100.0, 110.0, 99.0, 105.0),
            stale_bar(10, 0, 104.0, 112.0, 98.0, 98.5),
        ]
        signal, _ = strategy.higher_high_low_rejection_signal(stale, self.now)
        self.assertEqual(signal, "NONE")
        # the same bars do count when the date restriction is lifted
        signal, _ = strategy.higher_high_low_rejection_signal(
            stale, self.now, restrict_to_today=False)
        self.assertEqual(signal, "PE")

    def test_unrestricted_mode_uses_the_newest_bars(self):
        bars = self._bars(self._prev(100.0, 110.0, 99.0, 105.0),
                          self._curr(104.0, 112.0, 98.0, 98.5))
        signal, _ = strategy.higher_high_low_rejection_signal(
            bars, self.now, restrict_to_today=False)
        self.assertEqual(signal, "PE")

    def test_details_carry_the_signal_bar_time(self):
        bars = self._bars(self._prev(100.0, 110.0, 99.0, 105.0),
                          self._curr(104.0, 112.0, 98.0, 98.5))
        _, details = strategy.higher_high_low_rejection_signal(bars, self.now)
        self.assertTrue(details["curr_time"].startswith("2026-01-02T10:00"))
        self.assertTrue(details["prev_time"].startswith("2026-01-02T09:45"))


if __name__ == "__main__":
    unittest.main()
