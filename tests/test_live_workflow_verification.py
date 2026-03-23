from __future__ import annotations

import unittest
from datetime import UTC, datetime

from coupang_cart_agent.azure_openai import AzureOpenAIPlanner
from coupang_cart_agent.cart_verification import DeterministicCartVerifier
from coupang_cart_agent.candidate_sources import _candidates_from_observation
from coupang_cart_agent.contracts import (
    BrowserObservation,
    CartAddResult,
    CartAddStage,
    CartAddFailureReason,
    IntakeMode,
    ObservedCartItem,
    ObservedProduct,
    ProductCandidate,
    RequestSession,
    RequestedItem,
    SelectedProduct,
    ShoppingRequest,
    ShoppingRequestEnvelope,
)
from coupang_cart_agent.live_workflow import CoupangCartAgentLiveWorkflow, InMemoryOperationalStore
from coupang_cart_agent.notifications import RetryingNotificationService


class LiveWorkflowVerificationTests(unittest.TestCase):
    def _envelope(self, text: str = "우유 1개 담아줘") -> ShoppingRequestEnvelope:
        follow_up_reply = None
        items = [RequestedItem(name="우유", quantity=1)]
        if text == "ㅇㅇ 담아줘":
            follow_up_reply = "confirm"
            items = []
        request = ShoppingRequest(
            user_id="telegram:test-user",
            chat_id="telegram-chat",
            items=items,
            raw_text=text,
            request_id=f"req-verification-{text}",
            received_at=datetime(2026, 3, 16, 12, 0, tzinfo=UTC),
        )
        return ShoppingRequestEnvelope(
            source="telegram",
            mode=IntakeMode.LIVE,
            request=request,
            session=RequestSession(
                session_id="telegram-session:telegram-chat:telegram:test-user",
                channel="telegram",
                user_id=request.user_id,
                chat_id=request.chat_id,
                created_at=request.received_at,
                last_message_at=request.received_at,
            ),
            inbound_message_id="msg-1",
            update_id=1001,
            message_id=1,
            raw_text=text,
            raw_update={"message": {"text": text}},
            metadata={
                "session_id": "telegram-session:telegram-chat:telegram:test-user",
                "follow_up_reply": follow_up_reply,
            },
        )

    def test_live_workflow_persists_verification_evidence_and_sends_failure_on_mismatch(self) -> None:
        delivered_messages: list[tuple[str, str]] = []

        def sender(chat_id: str, text: str) -> None:
            delivered_messages.append((chat_id, text))

        class VerificationMismatchCartService:
            def add_products(self, selections: list[SelectedProduct]) -> list[CartAddResult]:
                return [
                    CartAddResult(
                        success=False,
                        cart_item_id=None,
                        selected_product=selections[0],
                        stage=CartAddStage.VERIFICATION,
                        message="장바구니에 요청한 상품이 담겼는지 확정할 증거가 부족합니다.",
                        failure_reason=CartAddFailureReason.MANUAL_REVIEW_REQUIRED,
                        evidence={
                            "verification": {
                                "cart_observation": {
                                    "has_screenshot": True,
                                }
                            }
                        },
                    )
                ]

        store = InMemoryOperationalStore()
        workflow = CoupangCartAgentLiveWorkflow(
            candidate_source=lambda request: {
                request.items[0].name: [
                    ProductCandidate(
                        product_id="MILK-1",
                        name="서울우유 1L",
                        price_krw=3200,
                        rating=4.8,
                        review_count=2500,
                        product_url="https://www.coupang.com/vp/products/MILK-1",
                    ),
                    ProductCandidate(
                        product_id="MILK-2",
                        name="서울우유 900ml",
                        price_krw=2900,
                        rating=4.6,
                        review_count=1800,
                        product_url="https://www.coupang.com/vp/products/MILK-2",
                    ),
                    ProductCandidate(
                        product_id="MILK-3",
                        name="매일우유 1L",
                        price_krw=3500,
                        rating=4.7,
                        review_count=1600,
                        product_url="https://www.coupang.com/vp/products/MILK-3",
                    ),
                ]
            },
            cart_service=VerificationMismatchCartService(),
            notification_service=RetryingNotificationService(sender=sender, max_attempts=1),
            operational_store=store,
            agent_planner=AzureOpenAIPlanner(endpoint=None, api_key=None, deployment=None),
            checkpointer=None,
        )

        first_result = workflow.run_envelope(self._envelope())
        result = workflow.run_envelope(self._envelope("ㅇㅇ 담아줘"))

        self.assertTrue(first_result.success)
        self.assertTrue(result.success)
        self.assertEqual(result.failed_stage, "verification")
        self.assertEqual(store.runs[-1]["failed_stage"], "verification")
        self.assertEqual(store.runs[-1]["conversation_status"], "awaiting_user_confirmation")
        self.assertTrue(store.runs[-1]["pending_proposal"])
        self.assertTrue(store.runs[-1]["notification_payload"]["success"])

    def test_deterministic_verifier_treats_pack_count_as_package_not_order_quantity(self) -> None:
        verifier = DeterministicCartVerifier()
        selection = SelectedProduct(
            request_item_name="새우깡",
            candidate=ProductCandidate(
                product_id="shrimp-cracker",
                name="새우깡, 90g, 5개",
                price_krw=5800,
                rating=5.0,
                review_count=1000,
                product_url="https://www.coupang.com/vp/products/shrimp-cracker",
            ),
            quantity=1,
            selection_reason="test",
            score=90.0,
        )
        observation = BrowserObservation(
            step_index=0,
            url="https://cart.coupang.com/cartView.pang",
            title="쿠팡 장바구니",
            page_kind="browse",
            body_text_excerpt="새우깡 옵션: 90g, 5개",
            cart_items=[
                ObservedCartItem(
                    name="새우깡",
                    quantity=None,
                    quantity_text=None,
                    option_summary="옵션: 90g, 5개",
                    package_summary="90g, 5개",
                )
            ],
        )

        result = verifier.verify(
            selection=selection,
            observation=observation,
            cart_count_before=0,
            cart_count_after=1,
        )

        self.assertTrue(result.success)

    def test_live_candidate_discovery_prefers_non_rocket_fresh_products(self) -> None:
        observation = BrowserObservation(
            step_index=1,
            url="https://www.coupang.com/np/search?q=%EC%9A%B0%EC%9C%A0",
            title="검색 결과",
            page_kind="search_results",
            body_text_excerpt="우유 검색 결과",
            observed_products=[
                ObservedProduct(
                    name="곰곰 신선한 1A 우유, 900ml, 1개",
                    href="https://www.coupang.com/vp/products/MILK-FRESH",
                    image_url="https://images.example.com/milk-fresh.jpg",
                    price_text="2,480원",
                    rating_text="5.0",
                    review_count_text="446,404",
                    badges=["로켓프레시"],
                ),
                ObservedProduct(
                    name="서울우유 1L",
                    href="https://www.coupang.com/vp/products/MILK-NORMAL",
                    image_url="https://images.example.com/milk-normal.jpg",
                    price_text="3,100원",
                    rating_text="4.8",
                    review_count_text="12,340",
                    badges=["Rocket"],
                ),
            ],
        )

        candidates = _candidates_from_observation(
            observation=observation,
            fallback_query="우유",
            max_candidates=5,
        )

        self.assertEqual(candidates[0].product_id, "MILK-NORMAL")
        self.assertEqual(candidates[0].image_url, "https://images.example.com/milk-normal.jpg")


if __name__ == "__main__":
    unittest.main()
