from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import asdict
from pathlib import Path

from .candidate_sources import CapturedCoupangFixtureCandidateSource, DemoCandidateSource
from .config import ConfigError, load_config, load_telegram_bot_token
from .contracts import (
    CartAddResult,
    CartAddStage,
    IntakeMode,
    ProductCandidate,
    RequestedItem,
    SelectedProduct,
    ShoppingRequest,
)
from .cart_executor import CartSnapshot, CoupangCartExecutor, OutOfStockError, SessionCredentials
from .integration import CoupangCartAgentFlow
from .notifications import (
    RetryingNotificationService,
    SQLiteNotificationContextStore,
    TelegramSendMessageSender,
    build_failure_notification_payload,
    build_success_notification_payload,
)
from .selection import HeuristicProductSelectionService
from .contracts import demo_contract_payload
from .telegram_intake import TelegramBotApiClient, TelegramPollingIntakeService
from .telegram_persistence import TelegramIntakeRepository


def _build_live_intake_service(*, token: str, db_path: str) -> TelegramPollingIntakeService:
    return TelegramPollingIntakeService(
        TelegramBotApiClient(token=token),
        TelegramIntakeRepository(db_path),
    )


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
        service = _build_live_intake_service(token=token, db_path=parsed.db_path)
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

    if command == "capture-telegram-live-request":
        parser = argparse.ArgumentParser(prog="python -m coupang_cart_agent capture-telegram-live-request")
        parser.add_argument("--offset", type=int, default=None)
        parser.add_argument("--timeout", type=int, default=30)
        parser.add_argument("--max-attempts", type=int, default=10)
        parser.add_argument("--sleep-seconds", type=float, default=0.0)
        parser.add_argument("--db-path", default=".artifacts/telegram_intake.sqlite3")
        parser.add_argument("--skip-error-response", action="store_true")
        parsed = parser.parse_args(args[1:])
        try:
            token = load_telegram_bot_token()
        except ConfigError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        service = _build_live_intake_service(token=token, db_path=parsed.db_path)
        next_offset = parsed.offset
        attempts: list[dict[str, object]] = []
        captured_result = None
        for attempt_number in range(1, parsed.max_attempts + 1):
            results = service.poll_once(
                offset=next_offset,
                timeout=parsed.timeout,
                mode=IntakeMode.LIVE,
                send_error_response=not parsed.skip_error_response,
            )
            highest_update_id = max((result.update_id for result in results), default=None)
            if highest_update_id is not None:
                next_offset = highest_update_id + 1
            attempts.append(
                {
                    "attempt": attempt_number,
                    "offset": next_offset,
                    "result_count": len(results),
                }
            )
            if results:
                captured_result = results[0]
                break
            if parsed.sleep_seconds > 0 and attempt_number < parsed.max_attempts:
                time.sleep(parsed.sleep_seconds)

        print(
            json.dumps(
                {
                    "mode": "live-capture",
                    "db_path": parsed.db_path,
                    "attempts": attempts,
                    "captured": captured_result.as_dict() if captured_result is not None else None,
                },
                ensure_ascii=False,
                indent=2,
                default=str,
            )
        )
        return 0 if captured_result is not None else 2

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
            candidate_source=DemoCandidateSource(),
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

    if command == "show-captured-candidates":
        parser = argparse.ArgumentParser(prog="python -m coupang_cart_agent show-captured-candidates")
        parser.add_argument(
            "--fixture-path",
            default=str(Path("tests/fixtures/coupang_search_onion_fixture.json")),
        )
        parser.add_argument("--item-name", default="양파")
        parsed = parser.parse_args(args[1:])
        source = CapturedCoupangFixtureCandidateSource(fixture_path=parsed.fixture_path)
        request = ShoppingRequest(
            user_id="telegram:cli-user",
            chat_id="cli-chat",
            items=[RequestedItem(name=parsed.item_name)],
            raw_text=f"{parsed.item_name} 담아줘",
            request_id="captured-fixture-demo",
        )
        result = source(request)
        print(json.dumps({key: [asdict(candidate) for candidate in value] for key, value in result.items()}, ensure_ascii=False, indent=2))
        return 0

    if command == "send-telegram-notification":
        parser = argparse.ArgumentParser(prog="python -m coupang_cart_agent send-telegram-notification")
        parser.add_argument("--chat-id", required=True)
        parser.add_argument("--scenario", choices=("success", "failure"), default="success")
        parser.add_argument("--user-id", default="telegram:cli-user")
        parser.add_argument("--database-path")
        parser.add_argument("--failure-stage", default="cart_add")
        parser.add_argument("--failure-reason", default="장바구니 담기 중 예기치 못한 오류가 발생했습니다.")
        parser.add_argument("--failure-detail", default=None)
        parsed = parser.parse_args(args[1:])

        try:
            telegram_bot_token = load_telegram_bot_token()
        except ConfigError as exc:
            print(str(exc), file=sys.stderr)
            return 1

        notification_service = RetryingNotificationService(
            sender=TelegramSendMessageSender(
                client=TelegramBotApiClient(token=telegram_bot_token)
            ),
            max_attempts=3,
        )

        if parsed.scenario == "failure":
            payload = build_failure_notification_payload(
                chat_id=parsed.chat_id,
                stage=parsed.failure_stage,
                reason=parsed.failure_reason,
                detail=parsed.failure_detail,
            )
        else:
            contract_demo = demo_contract_payload()
            cart_result_data = contract_demo["cart_add_result"]
            selected = cart_result_data["selected_product"]
            candidate = selected["candidate"]
            demo_results = [
                build_demo_cart_result(
                    product_id=str(candidate["product_id"]),
                    name=str(candidate["name"]),
                    price_krw=int(candidate["price_krw"]),
                    quantity=int(selected["quantity"]),
                )
            ]
            cart_snapshot_items = None
            prior_purchases = None
            if parsed.database_path:
                context_store = SQLiteNotificationContextStore(database_path=parsed.database_path)
                notification_context = context_store.load(user_id=parsed.user_id)
                cart_snapshot_items = notification_context["cart_snapshot_items"]
                prior_purchases = notification_context["prior_purchases"]
            payload = build_success_notification_payload(
                chat_id=parsed.chat_id,
                cart_results=demo_results,
                cart_snapshot_items=cart_snapshot_items,
                prior_purchases=prior_purchases,
            )

        notification_service.send(payload)
        print(json.dumps(asdict(payload), ensure_ascii=False, indent=2, default=str))
        return 0

    print(
        "Usage: python -m coupang_cart_agent "
        "[contracts-example|check-config|parse-telegram-message|poll-telegram-once|capture-telegram-live-request|integration-demo|show-captured-candidates|send-telegram-notification]",
        file=sys.stderr,
    )
    return 1


def build_demo_cart_result(
    *,
    product_id: str,
    name: str,
    price_krw: int,
    quantity: int,
) -> CartAddResult:
    selected_product = SelectedProduct(
        request_item_name=name,
        candidate=ProductCandidate(
            product_id=product_id,
            name=name,
            price_krw=price_krw,
            rating=4.8,
            review_count=1200,
            product_url=f"https://www.coupang.com/vp/products/{product_id}",
        ),
        quantity=quantity,
        selection_reason="Balanced rating, reviews, and price.",
        score=8.4,
    )
    return CartAddResult(
        success=True,
        cart_item_id=f"cart-{product_id}",
        selected_product=selected_product,
        stage=CartAddStage.ADD_TO_CART,
        message="Item added to cart.",
    )
