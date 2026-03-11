from __future__ import annotations

import sqlite3
import tempfile
import unittest

from coupang_cart_agent.contracts import (
    CartAddResult,
    CartAddStage,
    PriorPurchaseRecord,
    ProductCandidate,
    SelectedProduct,
)
from coupang_cart_agent.notifications import (
    NotificationFormatter,
    NotificationDeliveryError,
    RetryingNotificationService,
    SQLiteNotificationContextStore,
    TelegramSendMessageSender,
    build_failure_notification_payload,
    build_success_notification_payload,
    format_notification_message,
)


def cart_result(
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


class NotificationTests(unittest.TestCase):
    def test_success_payload_and_message_include_product_price_quantity_and_summary(self) -> None:
        payload = build_success_notification_payload(
            chat_id="telegram-chat",
            cart_results=[
                cart_result(
                    product_id="CP-1",
                    name="코카콜라 제로 355ml x 24",
                    price_krw=16900,
                    quantity=2,
                ),
                cart_result(
                    product_id="CP-2",
                    name="삼다수 2L x 6",
                    price_krw=6900,
                    quantity=1,
                ),
            ],
        )

        message = format_notification_message(payload)

        self.assertTrue(payload.success)
        self.assertEqual(payload.stage, CartAddStage.ADD_TO_CART.value)
        self.assertEqual(payload.details["cart_item_count"], 2)
        self.assertIn("코카콜라 제로 355ml x 24 / 16,900원 / 2개", message)
        self.assertIn("삼다수 2L x 6 / 6,900원 / 1개", message)
        self.assertIn("요약: 총 2종, 3개, 40,700원 장바구니 담기 완료", message)

    def test_success_message_uses_db_snapshot_and_prior_purchase_context_when_provided(self) -> None:
        payload = build_success_notification_payload(
            chat_id="telegram-chat",
            cart_results=[
                cart_result(
                    product_id="CP-1",
                    name="임시 상품명",
                    price_krw=1000,
                    quantity=1,
                ),
            ],
            cart_snapshot_items=[
                {
                    "product_id": "CP-1",
                    "name": "코카콜라 제로 355ml x 24",
                    "quantity": 2,
                    "price_krw": 16900,
                    "line_total_krw": 33800,
                },
                {
                    "product_id": "CP-2",
                    "name": "삼다수 2L x 6",
                    "quantity": 1,
                    "price_krw": 6900,
                    "line_total_krw": 6900,
                },
            ],
            prior_purchases=[
                PriorPurchaseRecord(
                    product_id="CP-1",
                    product_name="코카콜라 제로 355ml x 24",
                    purchase_count=3,
                )
            ],
        )

        message = format_notification_message(payload)

        self.assertEqual(payload.details["cart_item_count"], 2)
        self.assertIn("코카콜라 제로 355ml x 24 / 16,900원 / 2개", message)
        self.assertIn("삼다수 2L x 6 / 6,900원 / 1개", message)
        self.assertIn("재구매 참고: 코카콜라 제로 355ml x 24 / 이전 구매 3회", message)
        self.assertIn("요약: 총 2종, 3개, 40,700원 장바구니 담기 완료", message)

    def test_failure_payload_and_message_include_stage_reason_and_detail(self) -> None:
        payload = build_failure_notification_payload(
            chat_id="telegram-chat",
            stage="cart_add",
            reason="로그인 세션이 만료되었습니다.",
            detail="장바구니 담기 버튼을 누르기 전에 세션 검증에서 실패했습니다.",
        )

        message = format_notification_message(payload)

        self.assertFalse(payload.success)
        self.assertEqual(payload.summary, "로그인 세션이 만료되었습니다.")
        self.assertEqual(payload.details["failure_reason"], "로그인 세션이 만료되었습니다.")
        self.assertIn("단계: cart_add", message)
        self.assertIn("원인: 로그인 세션이 만료되었습니다.", message)
        self.assertIn(
            "상세: 장바구니 담기 버튼을 누르기 전에 세션 검증에서 실패했습니다.",
            message,
        )

    def test_success_message_limits_item_lines_and_total_length(self) -> None:
        payload = build_success_notification_payload(
            chat_id="telegram-chat",
            cart_results=[
                cart_result(
                    product_id=f"CP-{index}",
                    name=f"아주 긴 상품명 {index} " + ("테스트 " * 10),
                    price_krw=1000 * index,
                    quantity=1,
                )
                for index in range(1, 6)
            ],
        )

        message = format_notification_message(payload, max_length=180)

        self.assertIn("- 외 ", message)
        self.assertIn("요약: 총 5종, 5개, 15,000원 장바구니 담기 완료", message)
        self.assertLessEqual(len(message), 180)

    def test_retrying_notification_service_retries_retryable_failures(self) -> None:
        calls: list[tuple[str, str]] = []
        attempts = {"count": 0}

        def sender(chat_id: str, text: str) -> None:
            attempts["count"] += 1
            if attempts["count"] < 3:
                raise TimeoutError("temporary")
            calls.append((chat_id, text))

        service = RetryingNotificationService(sender=sender, max_attempts=3)
        payload = build_failure_notification_payload(
            chat_id="telegram-chat",
            stage="selection",
            reason="후보 상품을 찾지 못했습니다.",
        )

        service.send(payload)

        self.assertEqual(attempts["count"], 3)
        self.assertEqual(calls[0][0], "telegram-chat")
        self.assertIn("후보 상품을 찾지 못했습니다.", calls[0][1])

    def test_retrying_notification_service_raises_after_retry_budget_exhausted(self) -> None:
        def sender(chat_id: str, text: str) -> None:
            raise ConnectionError("network down")

        service = RetryingNotificationService(sender=sender, max_attempts=2)
        payload = build_failure_notification_payload(
            chat_id="telegram-chat",
            stage="notify",
            reason="텔레그램 전송에 실패했습니다.",
        )

        with self.assertRaises(NotificationDeliveryError):
            service.send(payload)

    def test_retrying_notification_service_supports_sender_adapter_objects(self) -> None:
        calls: list[tuple[str, str]] = []

        class Adapter:
            def send_message(self, *, chat_id: str, text: str) -> None:
                calls.append((chat_id, text))

        service = RetryingNotificationService(sender=Adapter(), formatter=NotificationFormatter())
        payload = build_failure_notification_payload(
            chat_id="telegram-chat",
            stage="notify",
            reason="텔레그램 전송에 실패했습니다.",
        )

        service.send(payload)

        self.assertEqual(calls, [("telegram-chat", "장바구니 담기에 실패했습니다.\n단계: notify\n원인: 텔레그램 전송에 실패했습니다.")])

    def test_telegram_send_message_sender_uses_bot_api_client(self) -> None:
        captured: list[tuple[str, str]] = []

        class FakeClient:
            def send_message(self, *, chat_id: str, text: str) -> dict[str, object]:
                captured.append((chat_id, text))
                return {"ok": True}

        sender = TelegramSendMessageSender(client=FakeClient())
        result = sender.send_message(chat_id="telegram-chat", text="테스트")

        self.assertEqual(captured, [("telegram-chat", "테스트")])
        self.assertEqual(result, {"ok": True})

    def test_sqlite_notification_context_store_loads_snapshot_and_prior_purchase_rows(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".sqlite3") as temp_db:
            connection = sqlite3.connect(temp_db.name)
            connection.executescript(
                """
                CREATE TABLE current_cart_snapshot_items (
                    user_id TEXT NOT NULL,
                    product_id TEXT NOT NULL,
                    product_name TEXT NOT NULL,
                    quantity INTEGER NOT NULL,
                    unit_price_krw INTEGER NOT NULL,
                    total_price_krw INTEGER,
                    snapshot_at TEXT NOT NULL
                );
                CREATE TABLE prior_purchases (
                    user_id TEXT NOT NULL,
                    product_id TEXT NOT NULL,
                    product_name TEXT NOT NULL,
                    purchase_count INTEGER NOT NULL,
                    last_purchased_at TEXT,
                    satisfaction_rating REAL
                );
                INSERT INTO current_cart_snapshot_items (
                    user_id, product_id, product_name, quantity, unit_price_krw, total_price_krw, snapshot_at
                ) VALUES
                    ('telegram:db-user', 'CP-1', '코카콜라 제로 355ml x 24', 2, 16900, 33800, '2026-03-11T10:00:00+00:00'),
                    ('telegram:db-user', 'CP-2', '삼다수 2L x 6', 1, 6900, 6900, '2026-03-11T10:00:00+00:00'),
                    ('telegram:db-user', 'CP-OLD', '이전 스냅샷', 1, 1000, 1000, '2026-03-10T10:00:00+00:00');
                INSERT INTO prior_purchases (
                    user_id, product_id, product_name, purchase_count, last_purchased_at, satisfaction_rating
                ) VALUES
                    ('telegram:db-user', 'CP-1', '코카콜라 제로 355ml x 24', 3, '2026-03-01T10:00:00+00:00', 4.5);
                """
            )
            connection.commit()
            connection.close()

            store = SQLiteNotificationContextStore(database_path=temp_db.name)
            context = store.load(user_id="telegram:db-user")

        self.assertEqual(len(context["cart_snapshot_items"]), 2)
        self.assertEqual(context["cart_snapshot_items"][0]["name"], "삼다수 2L x 6")
        self.assertEqual(context["cart_snapshot_items"][1]["line_total_krw"], 33800)
        self.assertEqual(len(context["prior_purchases"]), 1)
        self.assertEqual(context["prior_purchases"][0].product_id, "CP-1")


if __name__ == "__main__":
    unittest.main()
