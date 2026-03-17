from __future__ import annotations

import unittest
from datetime import UTC, datetime

from coupang_cart_agent.azure_openai import AzureOpenAIPlanner
from coupang_cart_agent.contracts import (
    CartAddResult,
    CartAddStage,
    CartAddFailureReason,
    IntakeMode,
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
                        message="Cart verification evidence was insufficient to confirm the requested item in cart.",
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
        self.assertFalse(result.success)
        self.assertEqual(result.failed_stage, "verification")
        self.assertEqual(result.cart_results[0].failure_reason, CartAddFailureReason.MANUAL_REVIEW_REQUIRED)
        self.assertFalse(store.runs[-1]["notification_payload"]["success"])
        self.assertIn("manual_review_required", delivered_messages[-1][1])
        self.assertTrue(
            store.runs[-1]["cart_results"][0]["evidence"]["verification"]["cart_observation"]["has_screenshot"]
        )


if __name__ == "__main__":
    unittest.main()
