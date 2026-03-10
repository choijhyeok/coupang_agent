from __future__ import annotations

import io
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from coupang_cart_agent.cli import main
from coupang_cart_agent.config import ConfigError, load_config
from coupang_cart_agent.contracts import RequestedItem, ShoppingRequest, demo_contract_payload


class FoundationTests(unittest.TestCase):
    def test_demo_contract_payload_contains_required_contracts(self) -> None:
        payload = demo_contract_payload()

        self.assertIn("shopping_request", payload)
        self.assertIn("selected_product", payload)
        self.assertIn("cart_add_result", payload)
        self.assertIn("notification_payload", payload)
        self.assertEqual(payload["shopping_request"]["items"][0]["quantity"], 2)

    def test_load_config_raises_clear_error(self) -> None:
        with self.assertRaises(ConfigError) as context:
            load_config({})

        self.assertIn("TELEGRAM_BOT_TOKEN", str(context.exception))
        self.assertIn("COUPANG_USERNAME", str(context.exception))
        self.assertIn("COUPANG_PASSWORD", str(context.exception))

    def test_load_config_reads_dotenv_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            dotenv_path = Path(tmp_dir) / ".env"
            dotenv_path.write_text(
                "\n".join(
                    [
                        "TELEGRAM_BOT_TOKEN=test-token",
                        "COUPANG_USERNAME=test-user",
                        "COUPANG_PASSWORD=test-password",
                    ]
                ),
                encoding="utf-8",
            )

            config = load_config({}, dotenv_path=dotenv_path)

        self.assertEqual(config.telegram_bot_token, "test-token")
        self.assertEqual(config.coupang_username, "test-user")
        self.assertEqual(config.coupang_password, "test-password")

    def test_load_config_prefers_explicit_env_over_dotenv(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            dotenv_path = Path(tmp_dir) / ".env"
            dotenv_path.write_text(
                "\n".join(
                    [
                        "TELEGRAM_BOT_TOKEN=file-token",
                        "COUPANG_USERNAME=file-user",
                        "COUPANG_PASSWORD=file-password",
                    ]
                ),
                encoding="utf-8",
            )

            config = load_config(
                {
                    "TELEGRAM_BOT_TOKEN": "env-token",
                    "COUPANG_USERNAME": "env-user",
                    "COUPANG_PASSWORD": "env-password",
                },
                dotenv_path=dotenv_path,
            )

        self.assertEqual(config.telegram_bot_token, "env-token")
        self.assertEqual(config.coupang_username, "env-user")
        self.assertEqual(config.coupang_password, "env-password")

    def test_cli_contracts_example_runs(self) -> None:
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            exit_code = main(["contracts-example"])

        self.assertEqual(exit_code, 0)
        self.assertIn("shopping_request", stdout.getvalue())

    def test_cli_check_config_reports_missing_values(self) -> None:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            exit_code = main(["check-config"])

        self.assertEqual(exit_code, 1)
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn("Missing required configuration", stderr.getvalue())

    def test_contract_import_example(self) -> None:
        request = ShoppingRequest(
            user_id="telegram:1",
            chat_id="1",
            items=[RequestedItem(name="물 2L", quantity=1)],
            raw_text="생수 2리터 담아줘",
        )

        self.assertEqual(request.items[0].name, "물 2L")


if __name__ == "__main__":
    unittest.main()
