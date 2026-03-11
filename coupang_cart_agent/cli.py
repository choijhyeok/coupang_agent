from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict

from .config import ConfigError, load_config, load_telegram_bot_token
from .contracts import IntakeMode, ProductCandidate, SelectedProduct
from .cart_executor import CartSnapshot, CoupangCartExecutor, OutOfStockError, SessionCredentials
from .integration import CoupangCartAgentFlow
from .notifications import RetryingNotificationService
from .selection import HeuristicProductSelectionService
from .contracts import demo_contract_payload
from .telegram_intake import TelegramBotApiClient, TelegramPollingIntakeService
from .telegram_persistence import TelegramIntakeRepository


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    command = args[0] if args else "help"

    if command == "contracts-example":
        print(json.dumps(demo_contract_payload(), ensure_ascii=False, indent=2, default=str))
        return 0

    if command == "check-config":
        try:
            config = load_config()
        except ConfigError as exc:
            print(str(exc), file=sys.stderr)
            return 1

        print(
            json.dumps(
                {
                    "telegram_bot_token_set": bool(config.telegram_bot_token),
                    "coupang_username": config.coupang_username,
                    "coupang_login_url": config.coupang_login_url,
                    "coupang_cart_url": config.coupang_cart_url,
                    "default_currency": config.default_currency,
                },
                indent=2,
            )
        )
        return 0

    if command == "parse-telegram-message":
        parser = argparse.ArgumentParser(prog="python -m coupang_cart_agent parse-telegram-message")
        parser.add_argument("text")
        parser.add_argument("--user-id", default="telegram:cli-user")
        parser.add_argument("--chat-id", default="cli-chat")
        parsed = parser.parse_args(args[1:])
        service = TelegramPollingIntakeService()
        request = service.parse_demo_message(
            user_id=parsed.user_id,
            chat_id=parsed.chat_id,
            text=parsed.text,
        )
        print(json.dumps(asdict(request), ensure_ascii=False, indent=2, default=str))
        return 0

    if command == "poll-telegram-once":
        parser = argparse.ArgumentParser(prog="python -m coupang_cart_agent poll-telegram-once")
        parser.add_argument("--offset", type=int, default=None)
        parser.add_argument("--timeout", type=int, default=1)
        parser.add_argument("--db-path", default=".artifacts/telegram_intake.sqlite3")
        parser.add_argument("--skip-error-response", action="store_true")
        parsed = parser.parse_args(args[1:])
        try:
            token = load_telegram_bot_token()
        except ConfigError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        service = TelegramPollingIntakeService(
            TelegramBotApiClient(token=token),
            TelegramIntakeRepository(parsed.db_path),
        )
        results = service.poll_once(
            offset=parsed.offset,
            timeout=parsed.timeout,
            mode=IntakeMode.LIVE,
            send_error_response=not parsed.skip_error_response,
        )
        print(
            json.dumps(
                {
                    "mode": "live",
                    "db_path": parsed.db_path,
                    "results": [result.as_dict() for result in results],
                },
                ensure_ascii=False,
                indent=2,
                default=str,
            )
        )
        return 0

    if command == "integration-demo":
        parser = argparse.ArgumentParser(prog="python -m coupang_cart_agent integration-demo")
        parser.add_argument("text")
        parser.add_argument("--scenario", choices=("success", "cart-failure"), default="success")
        parser.add_argument("--user-id", default="telegram:cli-user")
        parser.add_argument("--chat-id", default="cli-chat")
        parsed = parser.parse_args(args[1:])

        delivered_messages: list[dict[str, str]] = []

        def sender(chat_id: str, text: str) -> None:
            delivered_messages.append({"chat_id": chat_id, "text": text})

        def candidate_source(request):
            candidates_by_item = {}
            for index, item in enumerate(request.items, start=1):
                candidates_by_item[item.name] = [
                    ProductCandidate(
                        product_id=f"{index}-cheap",
                        name=f"{item.name} 보급형",
                        price_krw=5900,
                        rating=3.8,
                        review_count=19,
                        product_url=f"https://www.coupang.com/vp/products/{index}-cheap",
                    ),
                    ProductCandidate(
                        product_id=f"{index}-balanced",
                        name=f"{item.name} 추천",
                        price_krw=8900,
                        rating=4.8,
                        review_count=1800,
                        product_url=f"https://www.coupang.com/vp/products/{index}-balanced",
                    ),
                    ProductCandidate(
                        product_id=f"{index}-premium",
                        name=f"{item.name} 프리미엄",
                        price_krw=11900,
                        rating=4.9,
                        review_count=900,
                        product_url=f"https://www.coupang.com/vp/products/{index}-premium",
                    ),
                ]
            return candidates_by_item

        class DemoPage:
            def __init__(self, *, should_fail: bool) -> None:
                self._should_fail = should_fail
                self._snapshots = 0

            def ensure_session(self, credentials: SessionCredentials) -> str:
                return "existing_session"

            def open_product(self, product_url: str) -> None:
                return None

            def assert_in_stock(self) -> None:
                if self._should_fail:
                    raise OutOfStockError("Selected product is sold out.")

            def select_options(self, selection: SelectedProduct) -> dict[str, str]:
                return {"quantity": str(selection.quantity)}

            def cart_snapshot(self) -> CartSnapshot:
                self._snapshots += 1
                count = 0 if self._snapshots == 1 else 1
                return CartSnapshot(item_count=count, summary=f"count={count}")

            def add_to_cart(self) -> str:
                return "cart-item-demo"

            def checkout_started(self) -> bool:
                return False

        flow = CoupangCartAgentFlow(
            intake_service=TelegramPollingIntakeService(),
            candidate_source=candidate_source,
            selection_service=HeuristicProductSelectionService(),
            cart_service=CoupangCartExecutor(
                page=DemoPage(should_fail=parsed.scenario == "cart-failure"),
                credentials=SessionCredentials(
                    username="demo-user",
                    password="demo-password",
                ),
            ),
            notification_service=RetryingNotificationService(sender=sender, max_attempts=1),
        )
        result = flow.run_text_request(
            user_id=parsed.user_id,
            chat_id=parsed.chat_id,
            text=parsed.text,
        )
        print(
            json.dumps(
                {
                    **result.as_dict(),
                    "delivered_messages": delivered_messages,
                },
                ensure_ascii=False,
                indent=2,
                default=str,
            )
        )
        return 0

    print(
        "Usage: python -m coupang_cart_agent "
        "[contracts-example|check-config|parse-telegram-message|poll-telegram-once|integration-demo]",
        file=sys.stderr,
    )
    return 1
