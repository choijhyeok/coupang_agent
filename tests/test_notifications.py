from __future__ import annotations

import sqlite3
import tempfile
import unittest

from coupang_cart_agent.contracts import (
    CartAddResult,
    CartAddStage,
    CartRemoveFailureReason,
    CartRemoveResult,
    CartRemoveStage,
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
    build_cancelled_notification_payload,
    build_failure_notification_payload,
    build_proposal_notification_payload,
    build_remove_notification_payload,
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
            image_url=f"https://images.example.com/{product_id}.jpg",
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
        self.assertIn("<b>코카콜라 제로 355ml x 24</b>", message)
        self.assertIn("16,900원 · 2개", message)
        self.assertIn("<b>삼다수 2L x 6</b>", message)
        self.assertIn("<b>요약</b>: 총 2종, 3개, 40,700원 장바구니 담기 완료", message)

    def test_success_message_keeps_current_cart_results_when_snapshot_context_is_provided(self) -> None:
        payload = build_success_notification_payload(
            chat_id="telegram-chat",
            cart_results=[
                cart_result(
                    product_id="CP-1",
                    name="방금 담은 양파 1kg",
                    price_krw=2620,
                    quantity=1,
                ),
            ],
            cart_snapshot_items=[
                {
                    "product_id": "STALE-1",
                    "name": "오리온 미쯔블랙 시리얼, 360g, 1개",
                    "quantity": 1,
                    "price_krw": 4820,
                    "line_total_krw": 4820,
                },
            ],
            prior_purchases=[
                PriorPurchaseRecord(
                    product_id="CP-1",
                    product_name="방금 담은 양파 1kg",
                    purchase_count=3,
                )
            ],
        )

        message = format_notification_message(payload)

        self.assertEqual(payload.details["cart_item_count"], 1)
        self.assertIn("<b>방금 담은 양파 1kg</b>", message)
        self.assertIn("2,620원 · 1개", message)
        self.assertNotIn("오리온 미쯔블랙 시리얼", message)
        self.assertIn("<i>재구매 참고</i>: 방금 담은 양파 1kg · 이전 구매 3회", message)
        self.assertIn("<b>요약</b>: 총 1종, 1개, 2,620원 장바구니 담기 완료", message)

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
        self.assertIn("<b>단계</b>: <code>cart_add</code>", message)
        self.assertIn("<b>원인</b>: 로그인 세션이 만료되었습니다.", message)
        self.assertIn(
            "<b>상세</b>: 장바구니 담기 버튼을 누르기 전에 세션 검증에서 실패했습니다.",
            message,
        )

    def test_proposal_payload_and_message_include_confirmation_guidance(self) -> None:
        payload = build_proposal_notification_payload(
            chat_id="telegram-chat",
            summary="이전에 양파 300g을 구매하셨고 지금은 500g 2,000원 상품이 더 유리합니다.",
            candidate={
                "name": "곰곰 국내산 양파 500g",
                "price_krw": 2000,
                "option_summary": "500g / 1개",
                "selection_reason": "평점과 리뷰 수가 안정적이고 가격이 낮습니다.",
            },
            image_url="https://images.example.com/onion.jpg",
        )

        message = format_notification_message(payload)

        self.assertEqual(payload.kind, "proposal")
        self.assertIn("<b>추천 상품을 찾았습니다.</b>", message)
        self.assertIn("곰곰 국내산 양파 500g", message)
        self.assertIn("<code>다른 거 보여줘</code>", message)
        self.assertEqual(payload.details["photo"]["url"], "https://images.example.com/onion.jpg")

    def test_proposal_caption_avoids_duplicate_option_and_uses_reason(self) -> None:
        payload = build_proposal_notification_payload(
            chat_id="telegram-chat",
            summary="지금은 Coupang 상도가구 모니 1인용 좌식 소파가 추천됩니다.",
            candidate={
                "name": "상도가구 모니 1인용 좌식 소파, 오트밀, 1개",
                "price_krw": 46450,
                "option_summary": "상도가구 모니 1인용 좌식 소파, 오트밀, 1개",
                "selection_reason": "평점 4.5, 리뷰 2,056개, 가격 46,450원을 기준으로 균형이 좋아 추천드립니다.",
            },
            image_url="https://images.example.com/sofa.jpg",
        )

        caption = payload.details["photo"]["caption"]

        self.assertEqual(caption.count("상도가구 모니 1인용 좌식 소파, 오트밀, 1개"), 1)
        self.assertNotIn("지금은 Coupang", caption)
        self.assertIn("균형이 좋아 추천드립니다.", caption)

    def test_cancelled_payload_formats_plain_message(self) -> None:
        payload = build_cancelled_notification_payload(
            chat_id="telegram-chat",
            summary="이번 추천은 취소했습니다. 새 상품명을 보내주시면 다시 제안드릴게요.",
        )

        self.assertEqual(format_notification_message(payload), payload.summary)

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

        self.assertIn("• 외 ", message)
        self.assertIn("<b>요약</b>: 총 5종, 5개, 15,000원 장바구니 담기 완료", message)
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
            def send_message(self, *, chat_id: str, text: str, parse_mode: str | None = None) -> None:
                calls.append((chat_id, text))

        service = RetryingNotificationService(sender=Adapter(), formatter=NotificationFormatter())
        payload = build_failure_notification_payload(
            chat_id="telegram-chat",
            stage="notify",
            reason="텔레그램 전송에 실패했습니다.",
        )

        service.send(payload)

        self.assertEqual(
            calls,
            [("telegram-chat", "<b>장바구니 담기에 실패했습니다.</b>\n<b>단계</b>: <code>notify</code>\n<b>원인</b>: 텔레그램 전송에 실패했습니다.")],
        )

    def test_retrying_notification_service_sends_only_photo_for_proposal_when_photo_exists(self) -> None:
        deliveries: list[tuple[str, str, str | None]] = []
        messages: list[tuple[str, str]] = []

        class Adapter:
            def send_photo(
                self, *, chat_id: str, photo: str, caption: str | None = None, parse_mode: str | None = None
            ) -> None:
                deliveries.append((chat_id, photo, caption))

            def send_message(self, *, chat_id: str, text: str, parse_mode: str | None = None) -> None:
                messages.append((chat_id, text))

        service = RetryingNotificationService(sender=Adapter(), formatter=NotificationFormatter())
        payload = build_proposal_notification_payload(
            chat_id="telegram-chat",
            summary="양파 후보를 추천드립니다.",
            candidate={
                "name": "곰곰 국내산 양파 500g",
                "price_krw": 2000,
                "option_summary": "500g / 1개",
                "selection_reason": "가격과 리뷰가 균형적입니다.",
            },
            image_url="https://images.example.com/onion.jpg",
        )

        service.send(payload)

        self.assertEqual(deliveries[0][0], "telegram-chat")
        self.assertEqual(deliveries[0][1], "https://images.example.com/onion.jpg")
        self.assertIn("곰곰 국내산 양파 500g", deliveries[0][2] or "")
        self.assertEqual(messages, [])

    def test_telegram_send_message_sender_uses_bot_api_client(self) -> None:
        captured: list[tuple[str, str]] = []

        class FakeClient:
            def send_message(self, *, chat_id: str, text: str, parse_mode: str | None = None) -> dict[str, object]:
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


class RemoveNotificationTests(unittest.TestCase):
    def test_build_remove_notification_payload_success(self) -> None:
        results = [
            CartRemoveResult(
                success=True,
                product_name="양파",
                stage=CartRemoveStage.REMOVE,
                message="제거 완료",
                cart_count_before=2,
                cart_count_after=1,
            )
        ]
        payload = build_remove_notification_payload(chat_id="c1", remove_results=results)
        self.assertTrue(payload.success)
        self.assertEqual(payload.kind, "remove_result")
        self.assertIn("양파", payload.summary)
        self.assertIn("제거", payload.summary)

    def test_build_remove_notification_payload_failure(self) -> None:
        results = [
            CartRemoveResult(
                success=False,
                product_name="양파",
                stage=CartRemoveStage.ITEM_LOCATE,
                message="상품을 찾을 수 없습니다.",
                failure_reason=CartRemoveFailureReason.ITEM_NOT_FOUND,
            )
        ]
        payload = build_remove_notification_payload(chat_id="c1", remove_results=results)
        self.assertFalse(payload.success)
        self.assertIn("실패", payload.summary)

    def test_format_remove_success_message(self) -> None:
        results = [
            CartRemoveResult(
                success=True,
                product_name="양파",
                stage=CartRemoveStage.REMOVE,
                message="제거 완료",
            )
        ]
        payload = build_remove_notification_payload(chat_id="c1", remove_results=results)
        formatter = NotificationFormatter()
        message = formatter.format(payload)
        self.assertIn("제거", message)
        self.assertIn("양파", message)

    def test_format_remove_failure_message(self) -> None:
        results = [
            CartRemoveResult(
                success=False,
                product_name="양파",
                stage=CartRemoveStage.ITEM_LOCATE,
                message="찾을 수 없음",
                failure_reason=CartRemoveFailureReason.ITEM_NOT_FOUND,
            )
        ]
        payload = build_remove_notification_payload(chat_id="c1", remove_results=results)
        formatter = NotificationFormatter()
        message = formatter.format(payload)
        self.assertIn("실패", message)

    def test_build_remove_notification_payload_raises_on_empty(self) -> None:
        with self.assertRaises(ValueError):
            build_remove_notification_payload(chat_id="c1", remove_results=[])


if __name__ == "__main__":
    unittest.main()
