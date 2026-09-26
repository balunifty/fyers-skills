import json
import pathlib
import sys
import tempfile
import unittest
import urllib.error
import threading
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock, patch

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "skills" / "fyers-trading" / "scripts"))
sys.path.insert(0, str(REPO_ROOT / "strategies" / "config"))

import fyers_client  # noqa: E402
import order_config  # noqa: E402
import trade_logger  # noqa: E402


class OrderConfigTests(unittest.TestCase):
    def setUp(self):
        temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(temporary_directory.cleanup)
        self.state_directory = pathlib.Path(temporary_directory.name) / "order_state"
        state_patcher = patch.object(order_config, "ORDER_STATE_DIR", self.state_directory)
        state_patcher.start()
        self.addCleanup(state_patcher.stop)

        self.client = order_config.ConfigGatedFyersClient(
            token={"app_id": "test-app", "access_token": "test-token"},
            strategy_name="test_strategy",
            script_name="FnoTest.py",
        )
        self.client._request = Mock(
            return_value={"s": "ok", "code": 1101, "message": "submitted", "id": "OID123"}
        )
        self.order = {
            "symbol": "NSE:SBIN-EQ",
            "qty": 1,
            "type": 2,
            "side": 1,
            "productType": "INTRADAY",
        }

    def write_config(self, path: pathlib.Path, value: str, limit: int = 5) -> None:
        path.write_text(
            json.dumps({
                "place_order": value,
                "max_stocks_per_day": limit,
                "qty": 3,
                "stop_loss": 2.5,
                "trailing_stop_loss": 0.75,
            }),
            encoding="utf-8",
        )

    def test_trade_settings_are_read_from_config(self):
        with tempfile.TemporaryDirectory() as directory:
            config_path = pathlib.Path(directory) / "config.json"
            self.write_config(config_path, "NO")
            with patch.object(order_config, "FNO_CONFIG_PATH", config_path):
                self.assertEqual(order_config.get_entry_qty("FnoTest.py"), 3)
                self.assertAlmostEqual(order_config.get_stop_loss_percent("FnoTest.py"), 0.025)
                self.assertAlmostEqual(
                    order_config.get_trailing_stop_loss_percent("FnoTest.py"), 0.0075
                )

    def test_script_name_selects_equity_or_fno_config(self):
        with tempfile.TemporaryDirectory() as directory:
            config_dir = pathlib.Path(directory)
            equity_path = config_dir / "equity.json"
            fno_path = config_dir / "fno.json"
            equity_path.write_text(json.dumps({"qty": 2}), encoding="utf-8")
            fno_path.write_text(json.dumps({"qty": 7}), encoding="utf-8")
            with (
                patch.object(order_config, "EQUITY_CONFIG_PATH", equity_path),
                patch.object(order_config, "FNO_CONFIG_PATH", fno_path),
            ):
                self.assertEqual(
                    order_config.get_config_path("EquityStrategy.py"), equity_path
                )
                self.assertEqual(order_config.get_config_path("FnoStrategy.py"), fno_path)
                self.assertEqual(order_config.get_entry_qty("EquityStrategy.py"), 2)
                self.assertEqual(order_config.get_entry_qty("FnoStrategy.py"), 7)

    def test_no_blocks_live_order_before_request(self):
        with tempfile.TemporaryDirectory() as directory:
            config_path = pathlib.Path(directory) / "config.json"
            self.write_config(config_path, "NO")
            with patch.object(order_config, "FNO_CONFIG_PATH", config_path):
                with self.assertRaises(order_config.OrderPlacementDisabledError):
                    self.client.place_order(self.order, dry_run=False, validate_symbol=False)

        self.client._request.assert_not_called()

    def test_yes_allows_live_order(self):
        with tempfile.TemporaryDirectory() as directory:
            config_path = pathlib.Path(directory) / "config.json"
            self.write_config(config_path, "YES")
            with patch.object(order_config, "FNO_CONFIG_PATH", config_path):
                with patch.object(trade_logger, "log_order"):
                    result = self.client.place_order(
                        self.order, dry_run=False, validate_symbol=False
                    )

        self.assertEqual(result["s"], "ok")
        self.client._request.assert_called_once()
        self.assertEqual(self.client._request.call_args.args[0], "POST")
        self.assertTrue(self.client._request.call_args.args[1].endswith("/orders/sync"))

    def test_order_write_can_disable_transient_retries(self):
        with tempfile.TemporaryDirectory() as directory:
            config_path = pathlib.Path(directory) / "config.json"
            self.write_config(config_path, "YES")
            with patch.object(order_config, "FNO_CONFIG_PATH", config_path):
                with patch.object(trade_logger, "log_order"):
                    self.client.place_order(
                        self.order,
                        dry_run=False,
                        validate_symbol=False,
                        retry_transient=False,
                    )

        self.assertFalse(self.client._request.call_args.kwargs["retry_transient"])

    def test_non_idempotent_network_failure_is_not_retried(self):
        client = fyers_client.FyersClient(
            token={"app_id": "test-app", "access_token": "test-token"}
        )
        with (
            patch(
                "urllib.request.urlopen",
                side_effect=urllib.error.URLError("connection reset"),
            ) as urlopen,
            patch("time.sleep") as sleep,
        ):
            with self.assertRaises(fyers_client.AmbiguousOrderError):
                client._request(
                    "POST",
                    "https://example.invalid/orders",
                    {},
                    retry_transient=False,
                )

        self.assertEqual(urlopen.call_count, 1)
        sleep.assert_not_called()

    def test_config_is_reloaded_between_attempts(self):
        with tempfile.TemporaryDirectory() as directory:
            config_path = pathlib.Path(directory) / "config.json"
            self.write_config(config_path, "NO")
            with patch.object(order_config, "FNO_CONFIG_PATH", config_path):
                with self.assertRaises(order_config.OrderPlacementDisabledError):
                    self.client.place_order(self.order, dry_run=False, validate_symbol=False)

                self.write_config(config_path, "YES")
                with patch.object(trade_logger, "log_order"):
                    self.client.place_order(
                        self.order, dry_run=False, validate_symbol=False
                    )

        self.client._request.assert_called_once()

    def test_meta_strategy_uses_its_own_daily_cap_state(self):
        with tempfile.TemporaryDirectory() as directory:
            config_path = pathlib.Path(directory) / "config.json"
            self.write_config(config_path, "YES", limit=1)
            with (
                patch.object(order_config, "FNO_CONFIG_PATH", config_path),
                patch.object(trade_logger, "log_order"),
            ):
                self.client.place_order(
                    {**self.order, "symbol": "NSE:ONE-EQ"},
                    dry_run=False,
                    validate_symbol=False,
                    meta={"strategy": "StrategyB", "signal": "ENTRY"},
                )
                with self.assertRaises(order_config.DailyStockLimitError):
                    self.client.place_order(
                        {**self.order, "symbol": "NSE:TWO-EQ"},
                        dry_run=False,
                        validate_symbol=False,
                        meta={"strategy": "StrategyB", "signal": "ENTRY"},
                    )

        self.assertTrue(
            (self.state_directory / "StrategyB.json").exists()
        )

    def test_dry_run_does_not_require_yes_or_consume_stock(self):
        with tempfile.TemporaryDirectory() as directory:
            config_path = pathlib.Path(directory) / "config.json"
            self.write_config(config_path, "NO")
            with patch.object(order_config, "FNO_CONFIG_PATH", config_path):
                with patch.object(trade_logger, "log_order"):
                    for _ in range(6):
                        result = self.client.place_order(
                            self.order, dry_run=True, validate_symbol=False
                        )

        self.assertEqual(result["s"], "dry_run")
        self.client._request.assert_not_called()
        self.assertFalse(self.state_directory.exists())

    def test_daily_limit_allows_five_unique_stocks_then_blocks(self):
        with tempfile.TemporaryDirectory() as directory:
            config_path = pathlib.Path(directory) / "config.json"
            self.write_config(config_path, "YES", limit=5)
            with patch.object(order_config, "FNO_CONFIG_PATH", config_path):
                with patch.object(trade_logger, "log_order"):
                    for index in range(5):
                        order = {**self.order, "symbol": f"NSE:STOCK{index}-EQ"}
                        self.client.place_order(order, dry_run=False, validate_symbol=False)

                    sixth_order = {**self.order, "symbol": "NSE:STOCK5-EQ"}
                    with self.assertRaises(order_config.DailyStockLimitError):
                        self.client.place_order(
                            sixth_order, dry_run=False, validate_symbol=False
                        )

        self.assertEqual(self.client._request.call_count, 5)

    def test_daily_limit_is_serialized_across_process_style_clients(self):
        with tempfile.TemporaryDirectory() as directory:
            config_path = pathlib.Path(directory) / "config.json"
            self.write_config(config_path, "YES", limit=1)
            clients = [
                order_config.ConfigGatedFyersClient(
                    token={"app_id": "test-app", "access_token": "test-token"},
                    strategy_name="concurrent_strategy",
                    script_name="FnoTest.py",
                )
                for _ in range(2)
            ]
            for client in clients:
                client._request = Mock(
                    return_value={"s": "ok", "code": 1101, "message": "submitted", "id": "OID"}
                )
            barrier = threading.Barrier(2)

            def attempt(item):
                client, symbol = item
                barrier.wait()
                try:
                    client.place_order(
                        {**self.order, "symbol": symbol},
                        dry_run=False,
                        validate_symbol=False,
                    )
                    return "placed"
                except order_config.DailyStockLimitError:
                    return "blocked"

            with (
                patch.object(order_config, "FNO_CONFIG_PATH", config_path),
                patch.object(trade_logger, "log_order"),
            ):
                with ThreadPoolExecutor(max_workers=2) as executor:
                    results = list(executor.map(
                        attempt,
                        [(clients[0], "NSE:ONE-EQ"), (clients[1], "NSE:TWO-EQ")],
                    ))
            state = json.loads(
                (self.state_directory / "concurrent_strategy.json").read_text(encoding="utf-8")
            )

        self.assertCountEqual(results, ["placed", "blocked"])
        self.assertEqual(len(state["symbols"]), 1)

    def test_repeated_symbol_does_not_consume_another_stock(self):
        with tempfile.TemporaryDirectory() as directory:
            config_path = pathlib.Path(directory) / "config.json"
            self.write_config(config_path, "YES", limit=1)
            with patch.object(order_config, "FNO_CONFIG_PATH", config_path):
                with patch.object(trade_logger, "log_order"):
                    for _ in range(3):
                        self.client.place_order(
                            self.order, dry_run=False, validate_symbol=False
                        )

        self.assertEqual(self.client._request.call_count, 3)

    def test_exit_order_does_not_consume_entry_stock_slot(self):
        with tempfile.TemporaryDirectory() as directory:
            config_path = pathlib.Path(directory) / "config.json"
            self.write_config(config_path, "YES", limit=1)
            with patch.object(order_config, "FNO_CONFIG_PATH", config_path):
                with patch.object(trade_logger, "log_order"):
                    entry = self.client.place_order(
                        self.order, dry_run=False, validate_symbol=False
                    )
                    exit_order = {
                        **self.order,
                        "symbol": "NSE:SBIN-EQ",
                        "side": -1,
                    }
                    self.client.place_order(
                        exit_order,
                        dry_run=False,
                        validate_symbol=False,
                        meta={"signal": "STOP_LOSS"},
                    )

        self.assertEqual(entry["s"], "ok")
        self.assertEqual(self.client._request.call_count, 2)

    def test_no_also_blocks_live_position_exit(self):
        with tempfile.TemporaryDirectory() as directory:
            config_path = pathlib.Path(directory) / "config.json"
            self.write_config(config_path, "NO")
            with patch.object(order_config, "FNO_CONFIG_PATH", config_path):
                with self.assertRaises(order_config.OrderPlacementDisabledError):
                    self.client.exit_position("position-1", dry_run=False)

        self.client._request.assert_not_called()


if __name__ == "__main__":
    unittest.main()
