from __future__ import annotations

import unittest
from datetime import UTC, datetime

from coupang_cart_agent.azure_openai import AzureOpenAIPlanner
from coupang_cart_agent.contracts import (
    BrowserObservation,
    CartAddFailureReason,
    IntakeMode,
    ObservedCartItem,
    RequestSession,
    RequestedItem,
    SelectedProduct,
    ShoppingRequest,
    ShoppingRequestEnvelope,
)
from coupang_cart_agent.live_browser_agent import CoupangLiveBrowserShoppingAgent, DeterministicBrowserAgentModel
from coupang_cart_agent.live_workflow import CoupangCartAgentLiveWorkflow, InMemoryOperationalStore
from coupang_cart_agent.notifications import RetryingNotificationService
from tests.test_live_browser_agent import SequencedBrowserDriver


class LiveWorkflowVerificationTests(unittest.TestCase):
    def _envelope(self, text: str = "우유 1개 담아줘") -> ShoppingRequestEnvelope:
        request = ShoppingRequest(
            user_id="telegram:test-user",
            chat_id="telegram-chat",
            items=[RequestedItem(name="우유", quantity=1)],
            raw_text=text,
            request_id="req-verification",
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
            metadata={"session_id": "telegram-session:telegram-chat:telegram:test-user"},
        )

    def test_live_workflow_persists_verification_evidence_and_sends_failure_on_mismatch(self) -> None:
        delivered_messages: list[tuple[str, str]] = []

        def sender(chat_id: str, text: str) -> None:
            delivered_messages.append((chat_id, text))

        driver = SequencedBrowserDriver(
            [
                BrowserObservation(
                    step_index=1,
                    url="https://www.coupang.com",
                    title="쿠팡",
                    page_kind="browse",
                    body_text_excerpt="검색창이 보입니다.",
                    interactive_elements=["searchbox:검색"],
                ),
                BrowserObservation(
                    step_index=2,
                    url="https://www.coupang.com/np/search?q=%EC%9A%B0%EC%9C%A0",
                    title="검색 결과",
                    page_kind="search_results",
                    body_text_excerpt="서울우유 1L",
                    interactive_elements=["link:서울우유 1L"],
                    observed_products=[],
                ),
                BrowserObservation(
                    step_index=3,
                    url="https://www.coupang.com/vp/products/MILK-1",
                    title="상품 상세",
                    page_kind="product_page",
                    body_text_excerpt="서울우유 1L 장바구니 담기",
                    interactive_elements=["button:장바구니 담기"],
                    selected_product_hint={
                        "name": "서울우유 1L",
                        "href": "https://www.coupang.com/vp/products/MILK-1",
                    },
                    add_to_cart_visible=True,
                ),
            ]
        )
        driver.observe_cart_verification = lambda: BrowserObservation(  # type: ignore[method-assign]
            step_index=0,
            url="https://cart.coupang.com/cartView.pang",
            title="쿠팡 장바구니",
            page_kind="browse",
            body_text_excerpt="양파 추천 수량 1",
            accessibility_lines=["link:양파 추천", "button:수량 1"],
            screenshot_base64="ZmFrZS1jYXJ0LXNuYXBzaG90",
            cart_items=[ObservedCartItem(name="양파 추천", quantity=1, quantity_text="1개")],
            cart_count=1,
        )
        shopping_agent = CoupangLiveBrowserShoppingAgent(
            driver=driver,
            model=DeterministicBrowserAgentModel(),
        )
        store = InMemoryOperationalStore()
        workflow = CoupangCartAgentLiveWorkflow(
            candidate_source=lambda request: (_ for _ in ()).throw(RuntimeError("candidate source should not be called")),
            cart_service=None,
            notification_service=RetryingNotificationService(sender=sender, max_attempts=1),
            operational_store=store,
            agent_planner=AzureOpenAIPlanner(endpoint=None, api_key=None, deployment=None),
            shopping_agent=shopping_agent,
            checkpointer=None,
        )

        result = workflow.run_envelope(self._envelope())

        self.assertFalse(result.success)
        self.assertEqual(result.failed_stage, "verification")
        self.assertEqual(result.cart_results[0].failure_reason, CartAddFailureReason.MANUAL_REVIEW_REQUIRED)
        self.assertFalse(store.runs[-1]["notification_payload"]["success"])
        self.assertIn("manual_review_required", delivered_messages[0][1])
        self.assertTrue(
            store.runs[-1]["cart_results"][0]["evidence"]["verification"]["cart_observation"]["has_screenshot"]
        )


if __name__ == "__main__":
    unittest.main()
