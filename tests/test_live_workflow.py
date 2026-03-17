from __future__ import annotations

import unittest
from datetime import UTC, datetime

from langgraph.checkpoint.memory import InMemorySaver

from coupang_cart_agent.azure_openai import AzureOpenAIPlanner
from coupang_cart_agent.contracts import (
    CartAddFailureReason,
    CartAddResult,
    CartAddStage,
    IntakeMode,
    ProductCandidate,
    RequestSession,
    SelectedProduct,
    ShoppingRequest,
    ShoppingRequestEnvelope,
)
from coupang_cart_agent.live_workflow import (
    CoupangCartAgentLiveWorkflow,
    InMemoryOperationalStore,
)
from coupang_cart_agent.notifications import RetryingNotificationService


def candidate_source(request: ShoppingRequest) -> dict[str, list[ProductCandidate]]:
    return {
        item.name: [
            ProductCandidate(
                product_id=f"{item.name}-cheap",
                name=f"{item.name} 보급형",
                price_krw=5900,
                rating=3.8,
                review_count=19,
                product_url=f"https://www.coupang.com/vp/products/{item.name}-cheap",
                image_url=f"https://images.example.com/{item.name}-cheap.jpg",
            ),
            ProductCandidate(
                product_id=f"{item.name}-balanced",
                name=f"{item.name} 추천",
                price_krw=8900,
                rating=4.8,
                review_count=1800,
                product_url=f"https://www.coupang.com/vp/products/{item.name}-balanced",
                image_url=f"https://images.example.com/{item.name}-balanced.jpg",
            ),
            ProductCandidate(
                product_id=f"{item.name}-premium",
                name=f"{item.name} 프리미엄",
                price_krw=11900,
                rating=4.9,
                review_count=900,
                product_url=f"https://www.coupang.com/vp/products/{item.name}-premium",
                image_url=f"https://images.example.com/{item.name}-premium.jpg",
            ),
        ]
        for item in request.items
    }


class SuccessCartService:
    def add_products(self, selections: list[SelectedProduct]) -> list[CartAddResult]:
        return [
            CartAddResult(
                success=True,
                cart_item_id=f"cart-{selection.candidate.product_id}",
                selected_product=selection,
                stage=CartAddStage.ADD_TO_CART,
                message="Item added to cart.",
                cart_count_before=0,
                cart_count_after=1,
            )
            for selection in selections
        ]


class FailureCartService:
    def add_products(self, selections: list[SelectedProduct]) -> list[CartAddResult]:
        selection = selections[0]
        return [
            CartAddResult(
                success=False,
                cart_item_id=None,
                selected_product=selection,
                stage=CartAddStage.PRODUCT_PAGE,
                message="품절",
                failure_reason=CartAddFailureReason.OUT_OF_STOCK,
            )
        ]


class RaisingSender:
    def __call__(self, chat_id: str, text: str) -> None:
        raise RuntimeError("Telegram delivery failed")


class DeliveryRecorder:
    def __init__(self) -> None:
        self.messages: list[tuple[str, str]] = []
        self.photos: list[tuple[str, str, str | None]] = []

    def send_message(self, *, chat_id: str, text: str) -> None:
        self.messages.append((chat_id, text))

    def send_photo(self, *, chat_id: str, photo: str, caption: str | None = None) -> None:
        self.photos.append((chat_id, photo, caption))


class LiveWorkflowTests(unittest.TestCase):
    def build_envelope(self, *, request_id: str, text: str) -> ShoppingRequestEnvelope:
        request = ShoppingRequest(
            user_id="telegram:test-user",
            chat_id="telegram-chat",
            items=[],
            raw_text=text,
            request_id=request_id,
            received_at=datetime(2026, 3, 11, 10, 0, tzinfo=UTC),
        )
        from coupang_cart_agent.telegram_intake import TelegramPollingIntakeService

        parsed = TelegramPollingIntakeService().parse_message(
            user_id=request.user_id,
            chat_id=request.chat_id,
            text=text,
        )
        parsed.request_id = request_id
        parsed.received_at = request.received_at
        return ShoppingRequestEnvelope(
            source="telegram",
            mode=IntakeMode.LIVE,
            request=parsed,
            session=RequestSession(
                session_id="telegram-session:telegram-chat:telegram:test-user",
                channel="telegram",
                user_id=parsed.user_id,
                chat_id=parsed.chat_id,
                created_at=parsed.received_at,
                last_message_at=parsed.received_at,
            ),
            inbound_message_id=request_id,
            update_id=1001,
            message_id=1,
            raw_text=text,
            raw_update={"message": {"text": text}},
            metadata={
                "session_id": "telegram-session:telegram-chat:telegram:test-user",
                "follow_up_reply": TelegramPollingIntakeService.classify_follow_up_message(text),
            },
        )

    def test_live_workflow_requires_confirmation_before_cart_execution(self) -> None:
        recorder = DeliveryRecorder()
        store = InMemoryOperationalStore()
        planner = AzureOpenAIPlanner(endpoint=None, api_key=None, deployment=None)
        workflow = CoupangCartAgentLiveWorkflow(
            candidate_source=candidate_source,
            cart_service=SuccessCartService(),
            notification_service=RetryingNotificationService(sender=recorder, max_attempts=1),
            operational_store=store,
            agent_planner=planner,
            checkpointer=InMemorySaver(),
        )

        result = workflow.run_envelope(self.build_envelope(request_id="req-1", text="양파 1개 담아줘"))

        self.assertTrue(result.success)
        self.assertEqual(result.cart_results, [])
        self.assertEqual(store.runs[-1]["conversation_status"], "awaiting_user_confirmation")
        persisted_state = workflow.get_persisted_state(
            thread_id="telegram-session:telegram-chat:telegram:test-user"
        )
        self.assertEqual(persisted_state["thread_id"], "telegram-session:telegram-chat:telegram:test-user")
        self.assertEqual(persisted_state["conversation_status"], "awaiting_user_confirmation")
        self.assertIn("selected_candidate", persisted_state["pending_proposal"])
        self.assertEqual(len(recorder.photos), 1)
        self.assertIn("추천 상품을 찾았습니다.", recorder.messages[-1][1])
        self.assertEqual(store.current_cart_snapshot_by_user, {})

    def test_live_workflow_executes_confirmed_proposal_and_restores_context_for_same_thread(self) -> None:
        recorder = DeliveryRecorder()
        store = InMemoryOperationalStore()
        workflow = CoupangCartAgentLiveWorkflow(
            candidate_source=candidate_source,
            cart_service=SuccessCartService(),
            notification_service=RetryingNotificationService(sender=recorder, max_attempts=1),
            operational_store=store,
            agent_planner=AzureOpenAIPlanner(endpoint=None, api_key=None, deployment=None),
            checkpointer=InMemorySaver(),
        )

        first_result = workflow.run_envelope(self.build_envelope(request_id="req-1", text="양파 1개 담아줘"))
        second_result = workflow.run_envelope(self.build_envelope(request_id="req-2", text="ㅇㅇ 담아줘"))

        self.assertTrue(first_result.success)
        self.assertTrue(second_result.success)
        self.assertEqual(len(store.runs), 2)
        self.assertEqual(store.runs[-1]["conversation_status"], "completed")
        self.assertEqual(store.runs[-1]["success"], True)
        persisted_state = workflow.get_persisted_state(
            thread_id="telegram-session:telegram-chat:telegram:test-user"
        )
        self.assertEqual(persisted_state["conversation_status"], "completed")
        self.assertIn("agent_plan", persisted_state)
        self.assertEqual(len(recorder.photos), 1)
        self.assertIn("장바구니 담기를 완료했습니다.", recorder.messages[-1][1])
        self.assertNotIn("agent_plan", second_result.performance["timings_ms"])
        self.assertIn("notify", second_result.performance["timings_ms"])
        self.assertEqual(store.runs[0]["performance"]["counts"]["planner_call_count"], 1)

    def test_live_workflow_shows_next_candidate_without_cart_mutation(self) -> None:
        recorder = DeliveryRecorder()
        store = InMemoryOperationalStore()
        workflow = CoupangCartAgentLiveWorkflow(
            candidate_source=candidate_source,
            cart_service=SuccessCartService(),
            notification_service=RetryingNotificationService(sender=recorder, max_attempts=1),
            operational_store=store,
            agent_planner=AzureOpenAIPlanner(endpoint=None, api_key=None, deployment=None),
            checkpointer=InMemorySaver(),
        )

        workflow.run_envelope(self.build_envelope(request_id="req-1", text="양파 1개 담아줘"))
        result = workflow.run_envelope(self.build_envelope(request_id="req-2", text="다른 거 보여줘"))

        self.assertTrue(result.success)
        self.assertEqual(result.cart_results, [])
        self.assertEqual(store.runs[-1]["conversation_status"], "awaiting_user_confirmation")
        self.assertEqual(store.current_cart_snapshot_by_user, {})
        self.assertEqual(len(recorder.photos), 2)
        self.assertIn("추천 상품을 찾았습니다.", recorder.messages[-1][1])

    def test_live_workflow_reports_cart_failure_and_preserves_pending_proposal(self) -> None:
        recorder = DeliveryRecorder()
        store = InMemoryOperationalStore()
        workflow = CoupangCartAgentLiveWorkflow(
            candidate_source=candidate_source,
            cart_service=FailureCartService(),
            notification_service=RetryingNotificationService(sender=recorder, max_attempts=1),
            operational_store=store,
            agent_planner=AzureOpenAIPlanner(endpoint=None, api_key=None, deployment=None),
            checkpointer=InMemorySaver(),
        )

        workflow.run_envelope(self.build_envelope(request_id="req-proposal", text="양파 1개 담아줘"))
        result = workflow.run_envelope(self.build_envelope(request_id="req-fail", text="ㅇㅇ 담아줘"))

        self.assertFalse(result.success)
        self.assertEqual(result.failed_stage, "product_page")
        self.assertEqual(store.runs[-1]["failed_stage"], "product_page")
        self.assertEqual(store.runs[-1]["conversation_status"], "awaiting_user_confirmation")
        self.assertTrue(store.runs[-1]["pending_proposal"])
        self.assertIn("장바구니 담기에 실패했습니다.", recorder.messages[-1][1])

    def test_live_workflow_preserves_root_failure_stage_when_notification_send_fails(self) -> None:
        class FailAfterFirstNotification:
            def __init__(self) -> None:
                self.count = 0

            def send_message(self, *, chat_id: str, text: str) -> None:
                self.count += 1
                if self.count >= 2:
                    raise RuntimeError("Telegram delivery failed")

            def send_photo(self, *, chat_id: str, photo: str, caption: str | None = None) -> None:
                return None

        store = InMemoryOperationalStore()
        workflow = CoupangCartAgentLiveWorkflow(
            candidate_source=candidate_source,
            cart_service=FailureCartService(),
            notification_service=RetryingNotificationService(sender=FailAfterFirstNotification(), max_attempts=1),
            operational_store=store,
            agent_planner=AzureOpenAIPlanner(endpoint=None, api_key=None, deployment=None),
            checkpointer=InMemorySaver(),
        )

        workflow.run_envelope(self.build_envelope(request_id="req-proposal", text="양파 1개 담아줘"))
        result = workflow.run_envelope(self.build_envelope(request_id="req-notify-fail", text="ㅇㅇ 담아줘"))

        self.assertFalse(result.success)
        self.assertEqual(result.failed_stage, "product_page")
        self.assertEqual(store.runs[-1]["failed_stage"], "product_page")
        self.assertEqual(store.runs[-1]["failure_message"], "품절")
        self.assertEqual(store.runs[-1]["notification_payload"]["stage"], "notify")


if __name__ == "__main__":
    unittest.main()
