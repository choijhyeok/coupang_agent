from __future__ import annotations

import io
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from coupang_cart_agent.cart_persistence import SqliteCartResultStore
from coupang_cart_agent.cart_executor import (
    CartSnapshot,
    CoupangCartExecutor,
    LoginFailedError,
    OptionMismatchError,
    OutOfStockError,
    SessionCredentials,
    UIElementNotFoundError,
)
from coupang_cart_agent.cli import main
from coupang_cart_agent.config import ConfigError, load_config
from coupang_cart_agent.contracts import (
    CartAddFailureReason,
    CartAddStage,
    ProductCandidate,
    RequestedItem,
    SelectedProduct,
    ShoppingRequest,
    demo_contract_payload,
)


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
        self.assertEqual(config.cart_db_path, ".data/cart_results.sqlite3")
        self.assertTrue(config.coupang_browser_headless)
        self.assertEqual(config.coupang_browser_launch_mode, "playwright")
        self.assertIsNone(config.coupang_chrome_user_data_dir)
        self.assertEqual(config.coupang_chrome_profile_directory, "Default")
        self.assertEqual(config.coupang_chrome_remote_debugging_port, 9223)
        self.assertEqual(config.coupang_storage_state_path, ".data/coupang-storage-state.json")

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
                    "CART_DB_PATH": "/tmp/cart.sqlite3",
                    "COUPANG_BROWSER_HEADLESS": "false",
                    "COUPANG_BROWSER_LAUNCH_MODE": "cdp_chrome",
                    "COUPANG_CHROME_USER_DATA_DIR": "/tmp/chrome-user-data",
                    "COUPANG_CHROME_PROFILE_DIRECTORY": "Profile 1",
                    "COUPANG_CHROME_REMOTE_DEBUGGING_PORT": "9555",
                    "COUPANG_STORAGE_STATE_PATH": "/tmp/coupang-state.json",
                },
                dotenv_path=dotenv_path,
            )

        self.assertEqual(config.telegram_bot_token, "env-token")
        self.assertEqual(config.coupang_username, "env-user")
        self.assertEqual(config.coupang_password, "env-password")
        self.assertEqual(config.cart_db_path, "/tmp/cart.sqlite3")
        self.assertFalse(config.coupang_browser_headless)
        self.assertEqual(config.coupang_browser_launch_mode, "cdp_chrome")
        self.assertEqual(config.coupang_chrome_user_data_dir, "/tmp/chrome-user-data")
        self.assertEqual(config.coupang_chrome_profile_directory, "Profile 1")
        self.assertEqual(config.coupang_chrome_remote_debugging_port, 9555)
        self.assertEqual(config.coupang_storage_state_path, "/tmp/coupang-state.json")

    def test_cli_contracts_example_runs(self) -> None:
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            exit_code = main(["contracts-example"])

        self.assertEqual(exit_code, 0)
        self.assertIn("shopping_request", stdout.getvalue())

    def test_cli_check_config_reports_missing_values(self) -> None:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with patch.dict(
            os.environ,
            {
                "TELEGRAM_BOT_TOKEN": "",
                "COUPANG_USERNAME": "",
                "COUPANG_PASSWORD": "",
            },
            clear=True,
        ), redirect_stdout(stdout), redirect_stderr(stderr):
            exit_code = main(["check-config"])

        self.assertEqual(exit_code, 1)
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn("Missing required configuration", stderr.getvalue())

    def test_cli_integration_demo_success_runs_end_to_end_proof(self) -> None:
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            exit_code = main(
                [
                    "integration-demo",
                    "콜라 제로 2개 담아줘",
                    "--scenario",
                    "success",
                ]
            )

        self.assertEqual(exit_code, 0)
        output = stdout.getvalue()
        self.assertIn('"success": true', output)
        self.assertIn('"delivered_messages"', output)
        self.assertIn("장바구니 담기를 완료했습니다.", output)

    def test_cli_integration_demo_failure_reports_failure_notification(self) -> None:
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            exit_code = main(
                [
                    "integration-demo",
                    "삼다수 1박스 담아줘",
                    "--scenario",
                    "cart-failure",
                ]
            )

        self.assertEqual(exit_code, 0)
        output = stdout.getvalue()
        self.assertIn('"success": false', output)
        self.assertIn('"failed_stage": "product_page"', output)
        self.assertIn("장바구니 담기에 실패했습니다.", output)

    def test_contract_import_example(self) -> None:
        request = ShoppingRequest(
            user_id="telegram:1",
            chat_id="1",
            items=[RequestedItem(name="물 2L", quantity=1)],
            raw_text="생수 2리터 담아줘",
        )

        self.assertEqual(request.items[0].name, "물 2L")


class FakeCoupangPage:
    def __init__(
        self,
        *,
        session_mode: str = "existing_session",
        before_count: int = 1,
        after_count: int = 2,
        add_to_cart_id: str = "cart-item-42",
        checkout_started: bool = False,
        failure: Exception | None = None,
    ) -> None:
        self.session_mode = session_mode
        self.before_count = before_count
        self.after_count = after_count
        self.add_to_cart_id = add_to_cart_id
        self._checkout_started = checkout_started
        self.failure = failure
        self.calls: list[str] = []

    def ensure_session(self, credentials: SessionCredentials) -> str:
        self.calls.append("ensure_session")
        self._raise_if(LoginFailedError)
        return self.session_mode

    def open_product(self, product_url: str) -> None:
        self.calls.append(f"open_product:{product_url}")
        self._raise_if(UIElementNotFoundError, stage="open_product")

    def assert_in_stock(self) -> None:
        self.calls.append("assert_in_stock")
        self._raise_if(OutOfStockError)

    def select_options(self, selection: SelectedProduct) -> dict[str, str]:
        self.calls.append("select_options")
        self._raise_if(OptionMismatchError)
        self._raise_if(UIElementNotFoundError, stage="select_options")
        return {"size": "355ml", "pack": "24"}

    def cart_snapshot(self) -> CartSnapshot:
        self.calls.append("cart_snapshot")
        if self.calls.count("cart_snapshot") == 1:
            return CartSnapshot(item_count=self.before_count, summary="before")
        return CartSnapshot(item_count=self.after_count, summary="after")

    def add_to_cart(self) -> str:
        self.calls.append("add_to_cart")
        self._raise_if(UIElementNotFoundError, stage="add_to_cart")
        return self.add_to_cart_id

    def checkout_started(self) -> bool:
        self.calls.append("checkout_started")
        return self._checkout_started

    def _raise_if(self, error_type: type[Exception], *, stage: str | None = None) -> None:
        if isinstance(self.failure, error_type):
            if stage is None or getattr(self.failure, "stage", stage) == stage:
                raise self.failure


class CartExecutorTests(unittest.TestCase):
    def setUp(self) -> None:
        candidate = ProductCandidate(
            product_id="CP-1001",
            name="Coca-Cola Zero 355ml x 24",
            price_krw=16900,
            rating=4.8,
            review_count=12431,
            product_url="https://www.coupang.com/vp/products/CP-1001",
            vendor="Coupang",
            badges=["Rocket Delivery"],
        )
        self.selection = SelectedProduct(
            request_item_name="Coke Zero 355ml",
            candidate=candidate,
            quantity=1,
            selection_reason="Strong review signal with acceptable price.",
            score=9.2,
        )
        self.credentials = SessionCredentials(
            username="buyer@example.com",
            password="super-secret-password",
        )

    def test_add_products_success_captures_pre_and_post_cart_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            store = SqliteCartResultStore(Path(tmp_dir) / "cart-results.sqlite3")
            page = FakeCoupangPage(before_count=3, after_count=4)
            executor = CoupangCartExecutor(
                page=page,
                credentials=self.credentials,
                result_store=store,
            )

            result = executor.add_products([self.selection])[0]
            persisted = store.fetch_all()

            self.assertTrue(result.success)
            self.assertEqual(result.stage, CartAddStage.ADD_TO_CART)
            self.assertIsNone(result.failure_reason)
            self.assertEqual(result.cart_count_before, 3)
            self.assertEqual(result.cart_count_after, 4)
            self.assertFalse(result.checkout_attempted)
            self.assertEqual(result.evidence["session_mode"], "existing_session")
            self.assertEqual(len(persisted), 1)
            self.assertEqual(persisted[0]["stage"], "add_to_cart")
            self.assertTrue(persisted[0]["success"])
            self.assertEqual(
                page.calls,
                [
                    "ensure_session",
                    f"open_product:{self.selection.candidate.product_url}",
                    "assert_in_stock",
                    "select_options",
                    "cart_snapshot",
                    "add_to_cart",
                    "cart_snapshot",
                    "checkout_started",
                ],
            )

    def test_add_products_classifies_failures(self) -> None:
        scenarios = [
            (
                "login",
                LoginFailedError("Login failed for configured Coupang account."),
                CartAddFailureReason.LOGIN_FAILED,
                CartAddStage.SESSION,
            ),
            (
                "out_of_stock",
                OutOfStockError("Selected product is sold out."),
                CartAddFailureReason.OUT_OF_STOCK,
                CartAddStage.PRODUCT_PAGE,
            ),
            (
                "option_mismatch",
                OptionMismatchError("Requested option values do not exist on the page."),
                CartAddFailureReason.OPTION_MISMATCH,
                CartAddStage.OPTION_SELECTION,
            ),
            (
                "ui_missing",
                UIElementNotFoundError("Add-to-cart button not found."),
                CartAddFailureReason.UI_ELEMENT_NOT_FOUND,
                CartAddStage.PRODUCT_PAGE,
            ),
        ]
        for name, failure, expected_reason, expected_stage in scenarios:
            with self.subTest(name=name):
                page = FakeCoupangPage(failure=failure)
                executor = CoupangCartExecutor(page=page, credentials=self.credentials)

                result = executor.add_products([self.selection])[0]

                self.assertFalse(result.success)
                self.assertEqual(result.failure_reason, expected_reason)
                self.assertEqual(result.stage, expected_stage)

    def test_executor_redacts_credentials_in_audit_log(self) -> None:
        page = FakeCoupangPage(session_mode="restored session for buyer@example.com")
        executor = CoupangCartExecutor(page=page, credentials=self.credentials)

        executor.add_products([self.selection])

        rendered_entries = [
            f"{entry.message} {entry.metadata}"
            for entry in executor.audit_log()
        ]
        joined = "\n".join(rendered_entries)
        self.assertNotIn(self.credentials.username, joined)
        self.assertNotIn(self.credentials.password, joined)
        self.assertIn("***", joined)

    def test_executor_stops_when_checkout_starts(self) -> None:
        page = FakeCoupangPage(checkout_started=True)
        executor = CoupangCartExecutor(page=page, credentials=self.credentials)

        result = executor.add_products([self.selection])[0]

        self.assertFalse(result.success)
        self.assertEqual(result.failure_reason, CartAddFailureReason.CHECKOUT_ATTEMPTED)
        self.assertTrue(result.checkout_attempted)


if __name__ == "__main__":
    unittest.main()
