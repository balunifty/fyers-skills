import importlib.util
import io
import pathlib
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch


MODULE_PATH = pathlib.Path(__file__).with_name("run.py")
sys.path.insert(0, str(MODULE_PATH.parents[2]))
sys.path.insert(0, str(MODULE_PATH.parents[2] / "skills" / "fyers-trading" / "scripts"))
SPEC = importlib.util.spec_from_file_location("ema_rsi_atm_call_run", MODULE_PATH)
assert SPEC and SPEC.loader
run = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = run
SPEC.loader.exec_module(run)

SAMPLE_PATH = pathlib.Path(__file__).with_name("sample_place_order.py")
SAMPLE_SPEC = importlib.util.spec_from_file_location("sample_place_order", SAMPLE_PATH)
assert SAMPLE_SPEC and SAMPLE_SPEC.loader
sample = importlib.util.module_from_spec(SAMPLE_SPEC)
sys.modules[SAMPLE_SPEC.name] = sample
SAMPLE_SPEC.loader.exec_module(sample)


class IndicatorTests(unittest.TestCase):
    def test_ema_seed_and_length(self):
        self.assertEqual(run.ema([1, 2, 3, 4], 3), [None, None, 2.0, 3.0])

    def test_rsi_returns_warmup_values(self):
        values = run.rsi(list(range(1, 20)), 5)
        self.assertEqual(values[:5], [None] * 5)
        self.assertEqual(values[-1], 100.0)

    def test_upper_wick_condition_is_strict(self):
        candles = [run.Candle(index, 100, 101, 99, 100.5, 1) for index in range(40)]
        signal, snapshot = run.entry_signal(candles)
        self.assertFalse(signal)
        self.assertIn("conditions", snapshot)


class SampleOrderTests(unittest.TestCase):
    def test_stocks_file_is_read_and_order_defaults_are_valid(self):
        class FakeClient:
            def __init__(self):
                self.orders = []

            def quotes(self, symbols):
                return {"s": "ok", "d": [{"v": {"lp": 995.13}}]}

            def place_order(self, order, dry_run):
                self.orders.append((order, dry_run))
                return {"s": "dry_run", "code": 0, "message": "dry-run"}

        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as stocks:
            stocks.write("# ignored\nNSE:SBIN-EQ\n")
            stocks_path = pathlib.Path(stocks.name)
        try:
            fake_client = FakeClient()
            with tempfile.NamedTemporaryFile(suffix=".log", delete=False) as test_log:
                test_log_path = pathlib.Path(test_log.name)
            try:
                with patch.object(sample, "LOG_PATH", test_log_path):
                    with patch.object(sample, "FyersClient", return_value=fake_client):
                        with patch.object(sys, "argv", ["sample_place_order.py", "--stocks", str(stocks_path)]):
                            self.assertEqual(sample.main(), 0)
            finally:
                test_log_path.unlink()
            order, dry_run = fake_client.orders[0]
            self.assertEqual(order["symbol"], "NSE:SBIN-EQ")
            self.assertEqual(order["qty"], sample.DEFAULT_QTY)
            self.assertEqual(order["type"], 1)
            self.assertEqual(order["limitPrice"], 995.1)
            self.assertEqual(order["orderTag"], "sampletest")
            self.assertTrue(dry_run)
        finally:
            stocks_path.unlink()

    def test_response_statuses_are_explicit(self):
        output = io.StringIO()
        with redirect_stdout(output):
            sample.print_order_result("NSE:SBIN-EQ", {"s": "dry_run"})
            sample.print_order_result("NSE:SBIN-EQ", {"s": "ok", "id": "OID123", "message": "submitted"})
        self.assertIn("DRY-RUN: NSE:SBIN-EQ - no order sent", output.getvalue())
        self.assertIn("PLACED: NSE:SBIN-EQ - order_id=OID123", output.getvalue())


if __name__ == "__main__":
    unittest.main()