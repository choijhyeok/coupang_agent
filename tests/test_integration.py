from __future__ import annotations

import unittest

from coupang_cart_agent.cart_executor import (
    CartSnapshot,
    CoupangCartExecutor,
    OutOfStockError,
    SessionCredentials,
)
from coupang_cart_agent.contracts import ProductCandidate, SelectedProduct, ShoppingRequest
from coupang_cart_agent.integration import CoupangCartAgentFlow
from coupang_cart_agent.notifications import RetryingNotificationService
from coupang_cart_agent.selection import HeuristicProductSelectionService
from coupang_cart_agent.telegram_intake import TelegramPollingIntakeService


class FakeCoupangPage:
    def __init__(self, *, failure: Exception | None = None) -> None:
        self.failure = failure
        self.calls: list[str] = []

    def ensure_session(self, credentials: SessionCredentials) -> str:
        self.calls.append("ensure_session")
        return "existing_session"

    def open_product(self, product_url: str) -> None:
        self.calls.append(f"open_product:{product_url}")

    def assert_in_stock(self) -> None:
        self.calls.append("assert_in_stock")
        if self.failure is not None:
            raise self.failure

    def select_options(self, selection: SelectedProduct) -> dict[str, str]:
        self.calls.append("select_options")
        return {"quantity": str(selection.quantity)}

    def cart_snapshot(self) -> CartSnapshot:
        self.calls.append("cart_snapshot")
        count = 0 if self.calls.count("cart_snapshot") == 1 else 1
        return CartSnapshot(item_count=count, summary=f"count={count}")

    def add_to_cart(self) -> str:
        self.calls.append("add_to_cart")
        return "cart-item-demo"

    def checkout_started(self) -> bool:
        self.calls.append("checkout_started")
        return False


def candidate_source(request: ShoppingRequest) -> dict[str, list[ProductCandidate]]:
    candidates_by_item: dict[str, list[ProductCandidate]] = {}
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


class IntegrationFlowTests(unittest.TestCase):
    def build_flow(self, *, failure: Exception | None = None) -> tuple[CoupangCartAgentFlow, list[tuple[str, str]]]:
        delivered_messages: list[tuple[str, str]] = []

        def sender(chat_id: str, text: str) -> None:
            delivered_messages.append((chat_id, text))

        flow = CoupangCartAgentFlow(
            intake_service=TelegramPollingIntakeService(),
            candidate_source=candidate_source,
            selection_service=HeuristicProductSelectionService(),
            cart_service=CoupangCartExecutor(
                page=FakeCoupangPage(failure=failure),
                credentials=SessionCredentials(username="buyer@example.com", password="secret"),
            ),
            notification_service=RetryingNotificationService(sender=sender, max_attempts=1),
        )
        return flow, delivered_messages

    def test_run_text_request_successfully_connects_all_modules(self) -> None:
        flow, delivered_messages = self.build_flow()

        result = flow.run_text_request(
            user_id="telegram:demo-user",
            chat_id="demo-chat",
            text="콜라 제로 2개 담아줘",
        )

        self.assertTrue(result.success)
        self.assertIsNotNone(result.request)
        self.assertEqual(result.request.items[0].name, "콜라 제로")
        self.assertEqual(result.selections[0].candidate.product_id, "1-balanced")
        self.assertTrue(result.cart_results[0].success)
        self.assertTrue(result.notification_payload.success)
        self.assertEqual(len(delivered_messages), 1)
        self.assertEqual(delivered_messages[0][0], "demo-chat")
        self.assertIn("장바구니 담기를 완료했습니다.", delivered_messages[0][1])
        self.assertIn("콜라 제로 추천 / 8,900원 / 2개", delivered_messages[0][1])

    def test_run_text_request_reports_cart_failure_and_notifies_user(self) -> None:
        flow, delivered_messages = self.build_flow(
            failure=OutOfStockError("Selected product is sold out."),
        )

        result = flow.run_text_request(
            user_id="telegram:demo-user",
            chat_id="demo-chat",
            text="삼다수 1박스 담아줘",
        )

        self.assertFalse(result.success)
        self.assertEqual(result.failed_stage, "product_page")
        self.assertEqual(result.cart_results[0].success, False)
        self.assertEqual(result.cart_results[0].failure_reason.value, "out_of_stock")
        self.assertFalse(result.notification_payload.success)
        self.assertEqual(len(delivered_messages), 1)
        self.assertIn("장바구니 담기에 실패했습니다.", delivered_messages[0][1])
        self.assertIn("단계: product_page", delivered_messages[0][1])
        self.assertIn("out_of_stock", delivered_messages[0][1])


if __name__ == "__main__":
    unittest.main()
