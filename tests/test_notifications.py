from __future__ import annotations

import unittest

from coupang_cart_agent.contracts import CartAddResult, ProductCandidate, SelectedProduct
from coupang_cart_agent.notifications import (
    NotificationDeliveryError,
    RetryingNotificationService,
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
        self.assertEqual(payload.stage, "notify_success")
        self.assertEqual(payload.details["cart_item_count"], 2)
        self.assertIn("코카콜라 제로 355ml x 24 / 16,900원 / 2개", message)
        self.assertIn("삼다수 2L x 6 / 6,900원 / 1개", message)
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


if __name__ == "__main__":
    unittest.main()
