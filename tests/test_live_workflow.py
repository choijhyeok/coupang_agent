from __future__ import annotations

import unittest
from datetime import UTC, datetime

from langgraph.checkpoint.memory import InMemorySaver

from coupang_cart_agent.azure_openai import AzureOpenAIPlanner, ConversationIntent
from coupang_cart_agent.candidate_sources import LiveBrowserDiscoveryCandidateSource
from coupang_cart_agent.contracts import (
    BrowserObservation,
    ObservedProduct,
    CartAddFailureReason,
    CartAddResult,
    CartAddStage,
    CartRemoveFailureReason,
    CartRemoveResult,
    CartRemoveStage,
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

    def send_message(self, *, chat_id: str, text: str, parse_mode: str | None = None) -> None:
        self.messages.append((chat_id, text))

    def send_photo(
        self, *, chat_id: str, photo: str, caption: str | None = None, parse_mode: str | None = None
    ) -> None:
        self.photos.append((chat_id, photo, caption))


class FailingShoppingAgent:
    def run(self, **kwargs):
        raise AssertionError("shopping agent should not run before confirmation")


_REMOVE_KEYWORDS = ("빼줘", "제거해줘", "삭제해줘", "제외해줘", "빼", "제거해", "삭제해", "제외해")


class StubConversationInterpreter:
    def __init__(self, decision: str) -> None:
        self.decision = decision

    def classify(self, *, raw_text, has_pending_proposal, request_items, thread_context):
        # Simulate structured-output LLM: detect remove intent from text
        if any(kw in raw_text for kw in _REMOVE_KEYWORDS):
            return ConversationIntent(decision="remove_request", reason="stubbed-remove")
        if has_pending_proposal:
            return ConversationIntent(decision=self.decision, reason="stubbed")
        return ConversationIntent(decision="new_request", reason="stubbed")

    def summarize_conversation(self, *, previous_summary, current_run):
        return f"stub: {current_run.get('raw_text', '')}"


class BrowserDiscoveryDriver:
    def __init__(self) -> None:
        self.actions: list[str] = []
        self.queries: list[str] = []

    def attach_to_logged_in_session(self, credentials=None) -> str:
        self.actions.append("attach")
        return "attached_browser_use_profile"

    def assert_logged_in(self) -> None:
        self.actions.append("assert_logged_in")

    def execute_action(self, action) -> str:
        self.actions.append(action.action_type.value)
        self.queries.append(action.query or "")
        return f"searched:{action.query}"

    def observe(self, *, step_index: int, last_action_summary: str | None = None) -> BrowserObservation:
        return BrowserObservation(
            step_index=step_index,
            url="https://www.coupang.com/np/search?q=%EC%96%91%ED%8C%8C",
            title="검색 결과",
            page_kind="search_results",
            body_text_excerpt="양파 추천",
            observed_products=[
                ObservedProduct(
                    name="곰곰 국내산 양파 1kg",
                    href="https://www.coupang.com/vp/products/ONION-1KG",
                    price_text="4,980원",
                    rating_text="4.8",
                    review_count_text="12,345",
                    badges=["Rocket"],
                ),
                ObservedProduct(
                    name="곰곰 국내산 양파 3kg",
                    href="https://www.coupang.com/vp/products/ONION-3KG",
                    price_text="8,980원",
                    rating_text="4.9",
                    review_count_text="25,000",
                    badges=["Rocket"],
                ),
            ],
            interactive_elements=["searchbox:검색", "link:곰곰 국내산 양파 3kg"],
        )


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
            shopping_agent=FailingShoppingAgent(),
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
        self.assertEqual(recorder.photos[0][1], "https://images.example.com/양파-balanced.jpg")
        self.assertEqual(recorder.messages, [])
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

    def test_live_workflow_advances_to_next_item_for_multi_item_request(self) -> None:
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

        proposal_result = workflow.run_envelope(self.build_envelope(request_id="req-multi-1", text="의자랑 쇼파 담아줘"))
        first_confirm_result = workflow.run_envelope(self.build_envelope(request_id="req-multi-2", text="ㅇㅇ 담아줘"))
        second_confirm_result = workflow.run_envelope(self.build_envelope(request_id="req-multi-3", text="ㅇㅇ 담아줘"))

        self.assertTrue(proposal_result.success)
        self.assertTrue(first_confirm_result.success)
        self.assertTrue(second_confirm_result.success)
        self.assertEqual(store.runs[0]["pending_proposal"]["request_item_name"], "의자")
        self.assertEqual(store.runs[1]["conversation_status"], "awaiting_user_confirmation")
        self.assertEqual(store.runs[1]["pending_proposal"]["request_item_name"], "쇼파")
        self.assertIn("다음 상품을 이어서 추천드립니다.", recorder.photos[-1][2] or "")
        self.assertEqual(store.runs[2]["conversation_status"], "completed")
        self.assertIn("장바구니 담기를 완료했습니다.", recorder.messages[-1][1])

    def test_live_workflow_uses_llm_conversation_interpreter_for_pending_proposal_follow_up(self) -> None:
        recorder = DeliveryRecorder()
        store = InMemoryOperationalStore()
        workflow = CoupangCartAgentLiveWorkflow(
            candidate_source=candidate_source,
            cart_service=SuccessCartService(),
            notification_service=RetryingNotificationService(sender=recorder, max_attempts=1),
            operational_store=store,
            agent_planner=AzureOpenAIPlanner(endpoint=None, api_key=None, deployment=None),
            conversation_interpreter=StubConversationInterpreter("next"),
            checkpointer=InMemorySaver(),
        )

        workflow.run_envelope(self.build_envelope(request_id="req-llm-1", text="쇼파 담아줘"))
        result = workflow.run_envelope(self.build_envelope(request_id="req-llm-2", text="아까 쇼파 다른 걸로"))

        self.assertTrue(result.success)
        self.assertEqual(store.runs[-1]["user_decision"], "next")
        self.assertEqual(store.runs[-1]["conversation_status"], "awaiting_user_confirmation")

    def test_live_workflow_persists_live_browser_candidate_source_mode(self) -> None:
        recorder = DeliveryRecorder()
        store = InMemoryOperationalStore()
        workflow = CoupangCartAgentLiveWorkflow(
            candidate_source=LiveBrowserDiscoveryCandidateSource(driver=BrowserDiscoveryDriver()),
            cart_service=SuccessCartService(),
            notification_service=RetryingNotificationService(sender=recorder, max_attempts=1),
            operational_store=store,
            agent_planner=AzureOpenAIPlanner(endpoint=None, api_key=None, deployment=None),
            checkpointer=InMemorySaver(),
        )

        proposal_result = workflow.run_envelope(self.build_envelope(request_id="req-1", text="양파 담아줘"))
        confirm_result = workflow.run_envelope(self.build_envelope(request_id="req-2", text="ㅇㅇ 담아줘"))

        self.assertTrue(proposal_result.success)
        self.assertTrue(confirm_result.success)
        self.assertEqual(store.runs[0]["pending_proposal"]["candidate_source_mode"], "live_browser")
        self.assertEqual(store.runs[0]["pending_proposal"]["selected_candidate"]["candidate_source_mode"], "live_browser")
        self.assertEqual(
            store.runs[1]["cart_results"][0]["selected_product"]["candidate"]["product_id"],
            store.runs[0]["pending_proposal"]["selected_candidate"]["product_id"],
        )
        persisted_state = workflow.get_persisted_state(
            thread_id="telegram-session:telegram-chat:telegram:test-user"
        )
        self.assertEqual(persisted_state["candidate_source_mode"], "live_browser")

    def test_live_workflow_success_notification_uses_confirmed_selection_not_stale_snapshot(self) -> None:
        recorder = DeliveryRecorder()
        store = InMemoryOperationalStore()
        store.current_cart_snapshot_by_user["telegram:test-user"] = [
            {
                "product_id": "STALE-1",
                "name": "오리온 미쯔블랙 시리얼, 360g, 1개",
                "quantity": 1,
                "price_krw": 4820,
                "line_total_krw": 4820,
                "snapshot_at": "2026-03-18T00:00:00+00:00",
            }
        ]
        workflow = CoupangCartAgentLiveWorkflow(
            candidate_source=LiveBrowserDiscoveryCandidateSource(driver=BrowserDiscoveryDriver()),
            cart_service=SuccessCartService(),
            notification_service=RetryingNotificationService(sender=recorder, max_attempts=1),
            operational_store=store,
            agent_planner=AzureOpenAIPlanner(endpoint=None, api_key=None, deployment=None),
            checkpointer=InMemorySaver(),
        )

        workflow.run_envelope(self.build_envelope(request_id="req-1", text="양파 담아줘"))
        result = workflow.run_envelope(self.build_envelope(request_id="req-2", text="ㅇㅇ 담아줘"))

        self.assertTrue(result.success)
        self.assertIn("장바구니 담기를 완료했습니다.", recorder.messages[-1][1])
        self.assertIn("곰곰 국내산 양파", recorder.messages[-1][1])
        self.assertNotIn("오리온 미쯔블랙 시리얼", recorder.messages[-1][1])

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
        self.assertEqual(len(recorder.messages), 0)

    def test_live_browser_candidate_source_discovers_candidates_without_fixture(self) -> None:
        source = LiveBrowserDiscoveryCandidateSource(driver=BrowserDiscoveryDriver())
        request = self.build_envelope(request_id="req-source", text="양파 1개 담아줘").request

        candidates = source.load_candidates(
            request,
            search_queries_by_item={"양파": "양파"},
        )

        self.assertEqual(list(candidates.keys()), ["양파"])
        self.assertGreaterEqual(len(candidates["양파"]), 2)
        self.assertEqual(candidates["양파"][0].product_id, "ONION-3KG")
        self.assertEqual(source.source_mode, "live_browser")

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

        self.assertTrue(result.success)
        self.assertEqual(result.failed_stage, "product_page")
        self.assertEqual(store.runs[-1]["failed_stage"], "product_page")
        self.assertEqual(store.runs[-1]["conversation_status"], "awaiting_user_confirmation")
        self.assertTrue(store.runs[-1]["pending_proposal"])
        self.assertIn("차선책", recorder.photos[-1][2] or "")
        self.assertNotEqual(
            store.runs[-1]["pending_proposal"]["selected_candidate"]["product_id"],
            store.runs[0]["pending_proposal"]["selected_candidate"]["product_id"],
        )

    def test_live_workflow_preserves_root_failure_stage_when_notification_send_fails(self) -> None:
        class FailAfterFirstNotification:
            def __init__(self) -> None:
                self.count = 0

            def send_message(self, *, chat_id: str, text: str, parse_mode: str | None = None) -> None:
                return None

            def send_photo(
                self, *, chat_id: str, photo: str, caption: str | None = None, parse_mode: str | None = None
            ) -> None:
                self.count += 1
                if self.count >= 2:
                    raise RuntimeError("Telegram delivery failed")

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


class SuccessCartRemoveService:
    def remove_products(self, product_names: list[str]) -> list[CartRemoveResult]:
        return [
            CartRemoveResult(
                success=True,
                product_name=name,
                stage=CartRemoveStage.REMOVE,
                message="상품을 장바구니에서 제거했습니다.",
                cart_count_before=1,
                cart_count_after=0,
            )
            for name in product_names
        ]


class FailureCartRemoveService:
    def remove_products(self, product_names: list[str]) -> list[CartRemoveResult]:
        return [
            CartRemoveResult(
                success=False,
                product_name=name,
                stage=CartRemoveStage.ITEM_LOCATE,
                message="장바구니에서 상품을 찾을 수 없습니다.",
                failure_reason=CartRemoveFailureReason.ITEM_NOT_FOUND,
            )
            for name in product_names
        ]


class CartRemovalWorkflowTests(unittest.TestCase):
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

    def test_remove_request_triggers_cart_removal_and_sends_notification(self) -> None:
        recorder = DeliveryRecorder()
        store = InMemoryOperationalStore()
        workflow = CoupangCartAgentLiveWorkflow(
            candidate_source=candidate_source,
            cart_service=SuccessCartService(),
            cart_remove_service=SuccessCartRemoveService(),
            notification_service=RetryingNotificationService(sender=recorder, max_attempts=1),
            operational_store=store,
            agent_planner=AzureOpenAIPlanner(endpoint=None, api_key=None, deployment=None),
            checkpointer=InMemorySaver(),
        )

        result = workflow.run_envelope(self.build_envelope(request_id="req-remove-1", text="양파 빼줘"))

        self.assertTrue(result.success)
        self.assertEqual(len(recorder.messages), 1)
        notification_text = recorder.messages[0][1]
        self.assertIn("제거", notification_text)

    def test_remove_request_failure_reports_correctly(self) -> None:
        recorder = DeliveryRecorder()
        store = InMemoryOperationalStore()
        workflow = CoupangCartAgentLiveWorkflow(
            candidate_source=candidate_source,
            cart_service=SuccessCartService(),
            cart_remove_service=FailureCartRemoveService(),
            notification_service=RetryingNotificationService(sender=recorder, max_attempts=1),
            operational_store=store,
            agent_planner=AzureOpenAIPlanner(endpoint=None, api_key=None, deployment=None),
            checkpointer=InMemorySaver(),
        )

        result = workflow.run_envelope(self.build_envelope(request_id="req-remove-fail", text="양파 제거해줘"))

        self.assertFalse(result.success)
        self.assertEqual(len(recorder.messages), 1)
        notification_text = recorder.messages[0][1]
        self.assertIn("실패", notification_text)

    def test_remove_request_without_service_skips_removal(self) -> None:
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

        result = workflow.run_envelope(self.build_envelope(request_id="req-remove-no-svc", text="양파 빼줘"))

        self.assertTrue(result.success)

    def test_remove_request_classified_by_llm_structured_output(self) -> None:
        """LLM structured output correctly classifies '책상 빼줘' as remove_request.

        The StubConversationInterpreter simulates pydantic-constrained LLM output:
        when the message contains remove keywords, it returns remove_request.
        """
        recorder = DeliveryRecorder()
        store = InMemoryOperationalStore()
        workflow = CoupangCartAgentLiveWorkflow(
            candidate_source=candidate_source,
            cart_service=SuccessCartService(),
            cart_remove_service=SuccessCartRemoveService(),
            notification_service=RetryingNotificationService(sender=recorder, max_attempts=1),
            operational_store=store,
            agent_planner=AzureOpenAIPlanner(endpoint=None, api_key=None, deployment=None),
            conversation_interpreter=StubConversationInterpreter("new_request"),
            checkpointer=InMemorySaver(),
        )

        result = workflow.run_envelope(self.build_envelope(request_id="req-llm-remove", text="책상 빼줘"))

        self.assertTrue(result.success)
        self.assertEqual(store.runs[-1]["user_decision"], "remove_request")
        self.assertEqual(len(recorder.messages), 1)
        self.assertIn("제거", recorder.messages[0][1])

    def test_remove_request_falls_back_to_rules_when_no_llm(self) -> None:
        """Without an LLM interpreter, rule-based fallback classifies remove correctly."""
        recorder = DeliveryRecorder()
        store = InMemoryOperationalStore()
        workflow = CoupangCartAgentLiveWorkflow(
            candidate_source=candidate_source,
            cart_service=SuccessCartService(),
            cart_remove_service=SuccessCartRemoveService(),
            notification_service=RetryingNotificationService(sender=recorder, max_attempts=1),
            operational_store=store,
            agent_planner=AzureOpenAIPlanner(endpoint=None, api_key=None, deployment=None),
            checkpointer=InMemorySaver(),
        )

        result = workflow.run_envelope(self.build_envelope(request_id="req-rule-remove", text="책상 빼줘"))

        self.assertTrue(result.success)
        self.assertEqual(store.runs[-1]["user_decision"], "remove_request")


if __name__ == "__main__":
    unittest.main()
