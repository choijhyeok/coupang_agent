from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime
import time
from typing import Protocol, TypedDict

from langgraph.graph import END, START, StateGraph

from .azure_openai import AgentPlan, AgentSearchQuery, AzureOpenAIPlanner
from .cart_executor import (
    AccessDeniedError,
    LoginFailedError,
    LoginRequiredError,
    OptionMismatchError,
    OutOfStockError,
    SecurityChallengeError,
    UIElementNotFoundError,
)
from .contracts import (
    BrowserAgentStep,
    BrowserObservation,
    CartAddFailureReason,
    CartAddResult,
    CartAddStage,
    IntakeMode,
    NotificationPayload,
    PriorPurchaseRecord,
    ProductCandidate,
    RequestSession,
    SelectionContext,
    SessionSelectionSignal,
    SelectedProduct,
    ShoppingRequest,
    ShoppingRequestEnvelope,
)
from .integration import IntegrationRunResult
from .live_browser_agent import CoupangLiveBrowserShoppingAgent
from .notifications import build_failure_notification_payload, build_success_notification_payload
from .selection import HeuristicProductSelectionService
from .services import CoupangCartService, NotificationService


class LiveWorkflowState(TypedDict, total=False):
    thread_id: str
    request: dict[str, object]
    request_envelope: dict[str, object]
    thread_context: dict[str, object]
    selection_context: dict[str, object]
    agent_plan: dict[str, object]
    candidates_by_item: dict[str, list[dict[str, object]]]
    selections: list[dict[str, object]]
    cart_results: list[dict[str, object]]
    agent_steps: list[dict[str, object]]
    agent_reasoning_summary: str
    last_observation: dict[str, object]
    notification_payload: dict[str, object]
    performance: dict[str, object]
    success: bool
    failed_stage: str | None
    failure_message: str | None


class CandidateSource(Protocol):
    def __call__(self, request: ShoppingRequest) -> dict[str, list[ProductCandidate]]: ...


class OperationalStore(Protocol):
    def record_intake(self, *, thread_id: str, envelope: ShoppingRequestEnvelope) -> None: ...

    def load_selection_context(self, *, user_id: str, thread_id: str) -> SelectionContext: ...

    def load_notification_context(self, *, user_id: str) -> dict[str, object]: ...

    def load_thread_context(self, *, thread_id: str) -> dict[str, object]: ...

    def record_run(
        self,
        *,
        thread_id: str,
        envelope: ShoppingRequestEnvelope,
        agent_plan: AgentPlan | None,
        selections: list[SelectedProduct],
        cart_results: list[CartAddResult],
        notification_payload: NotificationPayload | None,
        agent_reasoning_summary: str | None,
        last_observation: dict[str, object] | None,
        agent_steps: list[dict[str, object]] | None,
        performance: dict[str, object] | None,
        success: bool,
        failed_stage: str | None,
        failure_message: str | None,
    ) -> None: ...


@dataclass(slots=True)
class StaticSelectionContextStore:
    context: SelectionContext

    def load(self, request: ShoppingRequest) -> SelectionContext:
        return self.context


class InMemoryOperationalStore:
    """Test-friendly operational store that mirrors the Postgres contract."""

    def __init__(self) -> None:
        self.thread_context_by_id: dict[str, dict[str, object]] = {}
        self.prior_purchases_by_user: dict[str, list[PriorPurchaseRecord]] = {}
        self.recent_signals_by_thread: dict[str, list[SessionSelectionSignal]] = {}
        self.current_cart_snapshot_by_user: dict[str, list[dict[str, object]]] = {}
        self.runs: list[dict[str, object]] = []

    def record_intake(self, *, thread_id: str, envelope: ShoppingRequestEnvelope) -> None:
        self.thread_context_by_id[thread_id] = {
            "thread_id": thread_id,
            "user_id": envelope.request.user_id,
            "chat_id": envelope.request.chat_id,
            "session_id": envelope.session.session_id,
            "last_request_id": envelope.request.request_id,
            "last_status": "received",
            "last_failure_stage": None,
            "updated_at": envelope.request.received_at.isoformat(),
        }

    def load_selection_context(self, *, user_id: str, thread_id: str) -> SelectionContext:
        return SelectionContext(
            prior_purchases=list(self.prior_purchases_by_user.get(user_id, [])),
            recent_session_signals=list(self.recent_signals_by_thread.get(thread_id, [])),
        )

    def load_notification_context(self, *, user_id: str) -> dict[str, object]:
        return {
            "cart_snapshot_items": list(self.current_cart_snapshot_by_user.get(user_id, [])),
            "prior_purchases": list(self.prior_purchases_by_user.get(user_id, [])),
        }

    def load_thread_context(self, *, thread_id: str) -> dict[str, object]:
        return dict(self.thread_context_by_id.get(thread_id, {}))

    def record_run(
        self,
        *,
        thread_id: str,
        envelope: ShoppingRequestEnvelope,
        agent_plan: AgentPlan | None,
        selections: list[SelectedProduct],
        cart_results: list[CartAddResult],
        notification_payload: NotificationPayload | None,
        agent_reasoning_summary: str | None,
        last_observation: dict[str, object] | None,
        agent_steps: list[dict[str, object]] | None,
        performance: dict[str, object] | None,
        success: bool,
        failed_stage: str | None,
        failure_message: str | None,
    ) -> None:
        now = datetime.now(UTC).isoformat()
        self.runs.append(
            {
                "thread_id": thread_id,
                "request_id": envelope.request.request_id,
                "success": success,
                "failed_stage": failed_stage,
                "failure_message": failure_message,
                "notification_payload": None if notification_payload is None else asdict(notification_payload),
                "agent_plan": None if agent_plan is None else agent_plan.as_dict(),
                "agent_reasoning_summary": agent_reasoning_summary,
                "last_observation": last_observation,
                "agent_steps": list(agent_steps or []),
                "performance": dict(performance or {}),
                "selections": [asdict(selection) for selection in selections],
                "cart_results": [asdict(result) for result in cart_results],
                "recorded_at": now,
            }
        )
        self.thread_context_by_id[thread_id] = {
            "thread_id": thread_id,
            "user_id": envelope.request.user_id,
            "chat_id": envelope.request.chat_id,
            "session_id": envelope.session.session_id,
            "last_request_id": envelope.request.request_id,
            "last_status": "success" if success else "failed",
            "last_failure_stage": failed_stage,
            "updated_at": now,
        }
        if success:
            snapshot_items: list[dict[str, object]] = []
            for result in cart_results:
                candidate = result.selected_product.candidate
                snapshot_items.append(
                    {
                        "product_id": candidate.product_id,
                        "name": candidate.name,
                        "quantity": result.selected_product.quantity,
                        "price_krw": candidate.price_krw,
                        "line_total_krw": candidate.price_krw * result.selected_product.quantity,
                        "snapshot_at": now,
                    }
                )
                existing = {record.product_id: record for record in self.prior_purchases_by_user.get(envelope.request.user_id, [])}
                prior = existing.get(candidate.product_id)
                if prior is None:
                    self.prior_purchases_by_user.setdefault(envelope.request.user_id, []).append(
                        PriorPurchaseRecord(
                            product_id=candidate.product_id,
                            product_name=candidate.name,
                            purchase_count=1,
                            last_purchased_at=datetime.now(UTC),
                            satisfaction_rating=candidate.rating,
                        )
                    )
                else:
                    prior.purchase_count += 1
                    prior.last_purchased_at = datetime.now(UTC)
            self.current_cart_snapshot_by_user[envelope.request.user_id] = snapshot_items

        signals = self.recent_signals_by_thread.setdefault(thread_id, [])
        for selection in selections:
            signals.append(
                SessionSelectionSignal(
                    product_id=selection.candidate.product_id,
                    signal="preferred",
                    noted_at=datetime.now(UTC),
                )
            )


class CoupangCartAgentLiveWorkflow:
    """LangGraph live workflow that stitches together the already-implemented modules."""

    def __init__(
        self,
        *,
        candidate_source: CandidateSource,
        cart_service: CoupangCartService,
        notification_service: NotificationService,
        operational_store: OperationalStore,
        agent_planner: AzureOpenAIPlanner,
        shopping_agent: CoupangLiveBrowserShoppingAgent | None = None,
        checkpointer=None,
    ) -> None:
        self._candidate_source = candidate_source
        self._cart_service = cart_service
        self._notification_service = notification_service
        self._operational_store = operational_store
        self._agent_planner = agent_planner
        self._shopping_agent = shopping_agent
        self._graph = self._build_graph(checkpointer=checkpointer)

    def run_envelope(
        self,
        envelope: ShoppingRequestEnvelope,
        *,
        thread_id: str | None = None,
    ) -> IntegrationRunResult:
        active_thread_id = thread_id or envelope.session.session_id
        self._operational_store.record_intake(thread_id=active_thread_id, envelope=envelope)
        state = self._graph.invoke(
            {
                "thread_id": active_thread_id,
                "request": _shopping_request_to_dict(envelope.request),
                "request_envelope": _envelope_to_dict(envelope),
                "candidates_by_item": {},
                "selections": [],
                "cart_results": [],
                "agent_steps": [],
                "agent_reasoning_summary": "",
                "last_observation": {},
                "notification_payload": {},
                "performance": {"timings_ms": {}, "counts": {}},
                "success": False,
                "failed_stage": None,
                "failure_message": None,
            },
            config={"configurable": {"thread_id": active_thread_id}},
        )
        return _integration_result_from_state(state)

    def get_persisted_state(self, *, thread_id: str) -> dict[str, object]:
        snapshot = self._graph.get_state({"configurable": {"thread_id": thread_id}})
        return dict(snapshot.values)

    def _build_graph(self, *, checkpointer=None):
        graph = StateGraph(LiveWorkflowState)
        graph.add_node("load_context", self._load_context_node)
        graph.add_node("agent_plan", self._agent_plan_node)
        graph.add_node("browser_shop", self._browser_shop_node)
        graph.add_node("load_candidates", self._load_candidates_node)
        graph.add_node("select_products", self._select_products_node)
        graph.add_node("add_to_cart", self._add_to_cart_node)
        graph.add_node("notify", self._notify_node)
        graph.add_node("persist", self._persist_node)
        graph.add_edge(START, "load_context")
        graph.add_edge("load_context", "agent_plan")
        graph.add_edge("agent_plan", "browser_shop")
        graph.add_edge("browser_shop", "load_candidates")
        graph.add_edge("load_candidates", "select_products")
        graph.add_edge("select_products", "add_to_cart")
        graph.add_edge("add_to_cart", "notify")
        graph.add_edge("notify", "persist")
        graph.add_edge("persist", END)
        return graph.compile(checkpointer=checkpointer)

    def _load_context_node(self, state: LiveWorkflowState) -> dict[str, object]:
        started = time.perf_counter()
        request = _shopping_request_from_dict(state["request"])
        thread_id = str(state["thread_id"])
        selection_context = self._operational_store.load_selection_context(
            user_id=request.user_id,
            thread_id=thread_id,
        )
        thread_context = self._operational_store.load_thread_context(thread_id=thread_id)
        return {
            "selection_context": _selection_context_to_dict(selection_context),
            "thread_context": thread_context,
            "performance": _updated_workflow_performance(
                state.get("performance"),
                stage="load_context",
                elapsed_seconds=time.perf_counter() - started,
            ),
        }

    def _agent_plan_node(self, state: LiveWorkflowState) -> dict[str, object]:
        if state.get("failed_stage"):
            return {}
        started = time.perf_counter()
        request = _shopping_request_from_dict(state["request"])
        selection_context = _selection_context_from_dict(state.get("selection_context", {}))
        plan = self._agent_planner.plan_request(
            request,
            prior_purchases=selection_context.prior_purchases,
            recent_session_signals=selection_context.recent_session_signals,
        )
        return {
            "agent_plan": plan.as_dict(),
            "performance": _updated_workflow_performance(
                state.get("performance"),
                stage="agent_plan",
                elapsed_seconds=time.perf_counter() - started,
                counts={
                    "planner_call_count": 1,
                    "planner_aoai_call_count": 1 if plan.mode == "azure_openai" else 0,
                },
            ),
        }

    def _browser_shop_node(self, state: LiveWorkflowState) -> dict[str, object]:
        if state.get("failed_stage") or self._shopping_agent is None:
            return {}
        request = _shopping_request_from_dict(state["request"])
        plan = _agent_plan_from_dict(state["agent_plan"])
        started = time.perf_counter()
        try:
            run = self._shopping_agent.run(
                request=request,
                search_queries={query.item_name: query.query for query in plan.search_queries},
                operator_note=plan.operator_note,
                selection_brief=plan.selection_brief,
            )
        except Exception as exc:
            classified = _classified_browser_agent_failure(request=request, exc=exc)
            return {
                "selections": [_selected_product_to_dict(classified.selected_product)],
                "cart_results": [_cart_result_to_dict(classified)],
                "agent_steps": [],
                "agent_reasoning_summary": str(exc),
                "last_observation": {},
                "performance": _updated_workflow_performance(
                    state.get("performance"),
                    stage="browser_shop",
                    elapsed_seconds=time.perf_counter() - started,
                ),
                "success": False,
                "failed_stage": classified.stage.value,
                "failure_message": classified.message,
            }

        first_failure = next((result for result in run.cart_results if not result.success), None)
        return {
            "selections": [asdict(selection) for selection in run.selections],
            "cart_results": [_cart_result_to_dict(result) for result in run.cart_results],
            "agent_steps": [_browser_agent_step_to_dict(step) for step in run.steps],
            "agent_reasoning_summary": run.reasoning_summary,
            "performance": _updated_workflow_performance(
                state.get("performance"),
                stage="browser_shop",
                elapsed_seconds=time.perf_counter() - started,
                counts=run.performance.get("counts", {}),
                nested_timings=run.performance.get("timings_ms", {}),
            ),
            "last_observation": (
                {}
                if run.last_observation is None
                else _browser_observation_to_dict(run.last_observation)
            ),
            "success": first_failure is None and bool(run.cart_results),
            "failed_stage": None if first_failure is None else first_failure.stage.value,
            "failure_message": None if first_failure is None else first_failure.message,
        }

    def _load_candidates_node(self, state: LiveWorkflowState) -> dict[str, object]:
        if state.get("failed_stage") or state.get("cart_results"):
            return {}
        request = _shopping_request_from_dict(state["request"])
        started = time.perf_counter()
        try:
            candidates = self._candidate_source(request)
            return {
                "candidates_by_item": {
                    item_name: [asdict(candidate) for candidate in values]
                    for item_name, values in candidates.items()
                },
                "performance": _updated_workflow_performance(
                    state.get("performance"),
                    stage="load_candidates",
                    elapsed_seconds=time.perf_counter() - started,
                ),
            }
        except Exception as exc:
            return {
                "performance": _updated_workflow_performance(
                    state.get("performance"),
                    stage="load_candidates",
                    elapsed_seconds=time.perf_counter() - started,
                ),
                "failed_stage": "candidate_fetch",
                "failure_message": str(exc),
            }

    def _select_products_node(self, state: LiveWorkflowState) -> dict[str, object]:
        if state.get("failed_stage") or state.get("selections"):
            return {}
        request = _shopping_request_from_dict(state["request"])
        selection_context = _selection_context_from_dict(state.get("selection_context", {}))
        started = time.perf_counter()
        service = HeuristicProductSelectionService(
            context_store=StaticSelectionContextStore(selection_context)
        )
        try:
            selections = service.select_products(
                request,
                {
                    item_name: [_product_candidate_from_dict(candidate) for candidate in values]
                    for item_name, values in state.get("candidates_by_item", {}).items()
                },
            )
            return {
                "selections": [asdict(selection) for selection in selections],
                "performance": _updated_workflow_performance(
                    state.get("performance"),
                    stage="select_products",
                    elapsed_seconds=time.perf_counter() - started,
                ),
            }
        except Exception as exc:
            return {
                "performance": _updated_workflow_performance(
                    state.get("performance"),
                    stage="select_products",
                    elapsed_seconds=time.perf_counter() - started,
                ),
                "failed_stage": "selection",
                "failure_message": str(exc),
            }

    def _add_to_cart_node(self, state: LiveWorkflowState) -> dict[str, object]:
        if state.get("failed_stage") or state.get("cart_results"):
            return {}
        selections = [_selected_product_from_dict(raw) for raw in state.get("selections", [])]
        started = time.perf_counter()
        try:
            results = self._cart_service.add_products(selections)
        except Exception as exc:
            return {
                "performance": _updated_workflow_performance(
                    state.get("performance"),
                    stage="add_to_cart",
                    elapsed_seconds=time.perf_counter() - started,
                ),
                "failed_stage": "cart_add",
                "failure_message": str(exc),
            }

        first_failure = next((result for result in results if not result.success), None)
        return {
            "cart_results": [_cart_result_to_dict(result) for result in results],
            "performance": _updated_workflow_performance(
                state.get("performance"),
                stage="add_to_cart",
                elapsed_seconds=time.perf_counter() - started,
            ),
            "success": first_failure is None,
            "failed_stage": None if first_failure is None else first_failure.stage.value,
            "failure_message": None if first_failure is None else first_failure.message,
        }

    def _notify_node(self, state: LiveWorkflowState) -> dict[str, object]:
        started = time.perf_counter()
        request = _shopping_request_from_dict(state["request"])
        cart_results = [_cart_result_from_dict(raw) for raw in state.get("cart_results", [])]
        payload = None

        if state.get("failed_stage") and not cart_results:
            payload = build_failure_notification_payload(
                chat_id=request.chat_id,
                stage=str(state["failed_stage"]),
                reason="통합 워크플로우가 실패했습니다.",
                detail=str(state.get("failure_message", "")),
            )
        elif cart_results and any(not result.success for result in cart_results):
            failure = next(result for result in cart_results if not result.success)
            failure_reason = (
                "장바구니 담기에 실패했습니다."
                if failure.failure_reason is None
                else f"장바구니 담기에 실패했습니다: {failure.failure_reason.value}"
            )
            payload = build_failure_notification_payload(
                chat_id=request.chat_id,
                stage=failure.stage.value,
                reason=failure_reason,
                detail=failure.message,
            )
        elif cart_results:
            notification_context = self._operational_store.load_notification_context(user_id=request.user_id)
            payload = build_success_notification_payload(
                chat_id=request.chat_id,
                cart_results=cart_results,
                cart_snapshot_items=notification_context.get("cart_snapshot_items"),
                prior_purchases=notification_context.get("prior_purchases"),
            )

        if payload is None:
            return {
                "performance": _updated_workflow_performance(
                    state.get("performance"),
                    stage="notify",
                    elapsed_seconds=time.perf_counter() - started,
                )
            }

        try:
            self._notification_service.send(payload)
            return {
                "notification_payload": _notification_payload_to_dict(payload),
                "performance": _updated_workflow_performance(
                    state.get("performance"),
                    stage="notify",
                    elapsed_seconds=time.perf_counter() - started,
                ),
            }
        except Exception as exc:
            prior_failed_stage = state.get("failed_stage")
            prior_failure_message = state.get("failure_message")
            failure_payload = build_failure_notification_payload(
                chat_id=request.chat_id,
                stage="notify",
                reason="텔레그램 알림 전송에 실패했습니다.",
                detail=str(exc),
            )
            result = {
                "notification_payload": _notification_payload_to_dict(failure_payload),
                "performance": _updated_workflow_performance(
                    state.get("performance"),
                    stage="notify",
                    elapsed_seconds=time.perf_counter() - started,
                ),
                "success": False,
            }
            if prior_failed_stage:
                result["failed_stage"] = prior_failed_stage
                result["failure_message"] = prior_failure_message
            else:
                result["failed_stage"] = "notify"
                result["failure_message"] = str(exc)
            return result

    def _persist_node(self, state: LiveWorkflowState) -> dict[str, object]:
        started = time.perf_counter()
        envelope = _envelope_from_dict(state["request_envelope"])
        selections = [_selected_product_from_dict(raw) for raw in state.get("selections", [])]
        cart_results = [_cart_result_from_dict(raw) for raw in state.get("cart_results", [])]
        notification_payload = (
            None
            if not state.get("notification_payload")
            else _notification_payload_from_dict(state["notification_payload"])
        )
        agent_plan = None if "agent_plan" not in state else _agent_plan_from_dict(state["agent_plan"])
        self._operational_store.record_run(
            thread_id=str(state["thread_id"]),
            envelope=envelope,
            agent_plan=agent_plan,
            selections=selections,
            cart_results=cart_results,
            notification_payload=notification_payload,
            agent_reasoning_summary=(
                None if "agent_reasoning_summary" not in state else str(state["agent_reasoning_summary"])
            ),
            last_observation=(
                None if "last_observation" not in state else dict(state.get("last_observation", {}))
            ),
            agent_steps=(
                None if "agent_steps" not in state else list(state.get("agent_steps", []))
            ),
            performance=_updated_workflow_performance(
                state.get("performance"),
                stage="persist",
                elapsed_seconds=time.perf_counter() - started,
            ),
            success=bool(state.get("success", False)),
            failed_stage=state.get("failed_stage"),
            failure_message=state.get("failure_message"),
        )
        return {}


def _shopping_request_from_dict(raw: dict[str, object]) -> ShoppingRequest:
    return ShoppingRequest(
        user_id=str(raw["user_id"]),
        chat_id=str(raw["chat_id"]),
        items=[
            _requested_item_from_dict(item)
            for item in raw.get("items", [])
            if isinstance(item, dict)
        ],
        raw_text=str(raw["raw_text"]),
        request_id=str(raw["request_id"]),
        received_at=_parse_datetime(raw.get("received_at")),
    )


def _shopping_request_to_dict(request: ShoppingRequest) -> dict[str, object]:
    return {
        "user_id": request.user_id,
        "chat_id": request.chat_id,
        "items": [asdict(item) for item in request.items],
        "raw_text": request.raw_text,
        "request_id": request.request_id,
        "received_at": request.received_at.isoformat(),
    }


def _requested_item_from_dict(raw: dict[str, object]):
    from .contracts import RequestedItem

    return RequestedItem(
        name=str(raw["name"]),
        quantity=max(1, int(raw.get("quantity", 1))),
        constraints=[str(item) for item in raw.get("constraints", [])],
        max_price_krw=(None if raw.get("max_price_krw") is None else int(raw["max_price_krw"])),
        explicit_brand=(None if raw.get("explicit_brand") is None else str(raw["explicit_brand"])),
        explicit_unit_size=(None if raw.get("explicit_unit_size") is None else str(raw["explicit_unit_size"])),
        explicit_pack_count=(None if raw.get("explicit_pack_count") is None else int(raw["explicit_pack_count"])),
        explicit_pack_unit=(None if raw.get("explicit_pack_unit") is None else str(raw["explicit_pack_unit"])),
    )


def _request_session_from_dict(raw: dict[str, object]) -> RequestSession:
    return RequestSession(
        session_id=str(raw["session_id"]),
        channel=str(raw["channel"]),
        user_id=str(raw["user_id"]),
        chat_id=str(raw["chat_id"]),
        created_at=_parse_datetime(raw.get("created_at")),
        last_message_at=_parse_datetime(raw.get("last_message_at")),
    )


def _request_session_to_dict(session: RequestSession) -> dict[str, object]:
    return {
        "session_id": session.session_id,
        "channel": session.channel,
        "user_id": session.user_id,
        "chat_id": session.chat_id,
        "created_at": session.created_at.isoformat(),
        "last_message_at": session.last_message_at.isoformat(),
    }


def _envelope_from_dict(raw: dict[str, object]) -> ShoppingRequestEnvelope:
    return ShoppingRequestEnvelope(
        source=str(raw["source"]),
        mode=IntakeMode(str(raw["mode"])),
        request=_shopping_request_from_dict(raw["request"]),
        session=_request_session_from_dict(raw["session"]),
        inbound_message_id=str(raw["inbound_message_id"]),
        update_id=(None if raw.get("update_id") is None else int(raw["update_id"])),
        message_id=(None if raw.get("message_id") is None else int(raw["message_id"])),
        raw_text=str(raw.get("raw_text", "")),
        raw_update=dict(raw.get("raw_update", {})),
        metadata=dict(raw.get("metadata", {})),
    )


def _envelope_to_dict(envelope: ShoppingRequestEnvelope) -> dict[str, object]:
    return {
        "source": envelope.source,
        "mode": envelope.mode.value,
        "request": _shopping_request_to_dict(envelope.request),
        "session": _request_session_to_dict(envelope.session),
        "inbound_message_id": envelope.inbound_message_id,
        "update_id": envelope.update_id,
        "message_id": envelope.message_id,
        "raw_text": envelope.raw_text,
        "raw_update": envelope.raw_update,
        "metadata": envelope.metadata,
    }


def _product_candidate_from_dict(raw: dict[str, object]) -> ProductCandidate:
    return ProductCandidate(
        product_id=str(raw["product_id"]),
        name=str(raw["name"]),
        price_krw=int(raw["price_krw"]),
        rating=float(raw["rating"]),
        review_count=int(raw["review_count"]),
        product_url=str(raw["product_url"]),
        vendor=(None if raw.get("vendor") in (None, "") else str(raw["vendor"])),
        badges=[str(item) for item in raw.get("badges", [])],
    )


def _product_candidate_to_dict(candidate: ProductCandidate) -> dict[str, object]:
    return {
        "product_id": candidate.product_id,
        "name": candidate.name,
        "price_krw": candidate.price_krw,
        "rating": candidate.rating,
        "review_count": candidate.review_count,
        "product_url": candidate.product_url,
        "vendor": candidate.vendor,
        "badges": list(candidate.badges),
    }


def _selected_product_from_dict(raw: dict[str, object]) -> SelectedProduct:
    return SelectedProduct(
        request_item_name=str(raw["request_item_name"]),
        candidate=_product_candidate_from_dict(raw["candidate"]),
        quantity=max(1, int(raw["quantity"])),
        selection_reason=str(raw["selection_reason"]),
        score=float(raw["score"]),
        option_hints={str(key): str(value) for key, value in raw.get("option_hints", {}).items()},
    )


def _selected_product_to_dict(selection: SelectedProduct) -> dict[str, object]:
    return {
        "request_item_name": selection.request_item_name,
        "candidate": _product_candidate_to_dict(selection.candidate),
        "quantity": selection.quantity,
        "selection_reason": selection.selection_reason,
        "score": selection.score,
        "option_hints": dict(selection.option_hints),
    }


def _cart_result_from_dict(raw: dict[str, object]) -> CartAddResult:
    failure_reason = raw.get("failure_reason")
    return CartAddResult(
        success=bool(raw["success"]),
        cart_item_id=(None if raw.get("cart_item_id") is None else str(raw["cart_item_id"])),
        selected_product=_selected_product_from_dict(raw["selected_product"]),
        stage=CartAddStage(str(raw["stage"])),
        message=str(raw["message"]),
        failure_reason=(
            None if failure_reason in (None, "") else CartAddFailureReason(str(failure_reason))
        ),
        cart_count_before=(
            None if raw.get("cart_count_before") is None else int(raw["cart_count_before"])
        ),
        cart_count_after=(None if raw.get("cart_count_after") is None else int(raw["cart_count_after"])),
        checkout_attempted=bool(raw.get("checkout_attempted", False)),
        evidence=dict(raw.get("evidence", {})),
    )


def _cart_result_to_dict(result: CartAddResult) -> dict[str, object]:
    return {
        "success": result.success,
        "cart_item_id": result.cart_item_id,
        "selected_product": _selected_product_to_dict(result.selected_product),
        "stage": result.stage.value,
        "message": result.message,
        "failure_reason": None if result.failure_reason is None else result.failure_reason.value,
        "cart_count_before": result.cart_count_before,
        "cart_count_after": result.cart_count_after,
        "checkout_attempted": result.checkout_attempted,
        "evidence": dict(result.evidence),
    }


def _notification_payload_from_dict(raw: dict[str, object]) -> NotificationPayload:
    return NotificationPayload(
        chat_id=str(raw["chat_id"]),
        success=bool(raw["success"]),
        stage=str(raw["stage"]),
        summary=str(raw["summary"]),
        details=dict(raw.get("details", {})),
    )


def _notification_payload_to_dict(payload: NotificationPayload) -> dict[str, object]:
    return {
        "chat_id": payload.chat_id,
        "success": payload.success,
        "stage": payload.stage,
        "summary": payload.summary,
        "details": dict(payload.details),
    }


def _browser_observation_to_dict(observation: BrowserObservation) -> dict[str, object]:
    raw = asdict(observation)
    raw["screenshot_base64"] = None
    return raw


def _browser_agent_step_to_dict(step: BrowserAgentStep) -> dict[str, object]:
    return {
        "step_index": step.step_index,
        "item_name": step.item_name,
        "observation": _browser_observation_to_dict(step.observation),
        "action": asdict(step.action),
        "execution_summary": step.execution_summary,
    }


def _classified_browser_agent_failure(
    *,
    request: ShoppingRequest,
    exc: Exception,
) -> CartAddResult:
    failure_reason = CartAddFailureReason.UNKNOWN
    if isinstance(exc, LoginRequiredError):
        failure_reason = CartAddFailureReason.LOGIN_REQUIRED
    elif isinstance(exc, AccessDeniedError):
        failure_reason = CartAddFailureReason.ACCESS_DENIED
    elif isinstance(exc, SecurityChallengeError):
        failure_reason = CartAddFailureReason.SECURITY_CHALLENGE
    elif isinstance(exc, LoginFailedError):
        failure_reason = CartAddFailureReason.LOGIN_FAILED
    elif isinstance(exc, OutOfStockError):
        failure_reason = CartAddFailureReason.OUT_OF_STOCK
    elif isinstance(exc, OptionMismatchError):
        failure_reason = CartAddFailureReason.OPTION_MISMATCH
    elif isinstance(exc, UIElementNotFoundError):
        failure_reason = CartAddFailureReason.UI_ELEMENT_NOT_FOUND

    stage = CartAddStage.SESSION
    if failure_reason == CartAddFailureReason.OUT_OF_STOCK:
        stage = CartAddStage.PRODUCT_PAGE
    elif failure_reason == CartAddFailureReason.OPTION_MISMATCH:
        stage = CartAddStage.OPTION_SELECTION
    elif failure_reason == CartAddFailureReason.UI_ELEMENT_NOT_FOUND:
        stage = CartAddStage.PRODUCT_PAGE

    item = request.items[0]
    fallback_selection = SelectedProduct(
        request_item_name=item.name,
        candidate=ProductCandidate(
            product_id=f"pending-{item.name}",
            name=item.name,
            price_krw=0,
            rating=0.0,
            review_count=0,
            product_url="",
            vendor="Coupang",
        ),
        quantity=item.quantity,
        selection_reason="Browser agent stopped before a product could be selected.",
        score=0.0,
    )
    return CartAddResult(
        success=False,
        cart_item_id=None,
        selected_product=fallback_selection,
        stage=stage,
        message=str(exc),
        failure_reason=failure_reason,
        evidence={"exception_type": exc.__class__.__name__},
    )


def _selection_context_to_dict(context: SelectionContext) -> dict[str, object]:
    return {
        "prior_purchases": [asdict(record) for record in context.prior_purchases],
        "recent_session_signals": [asdict(signal) for signal in context.recent_session_signals],
    }


def _selection_context_from_dict(raw: dict[str, object]) -> SelectionContext:
    prior_purchases = []
    for record in raw.get("prior_purchases", []):
        if not isinstance(record, dict):
            continue
        prior_purchases.append(
            PriorPurchaseRecord(
                product_id=str(record["product_id"]),
                product_name=str(record["product_name"]),
                purchase_count=max(1, int(record.get("purchase_count", 1))),
                last_purchased_at=_parse_datetime(record.get("last_purchased_at")),
                satisfaction_rating=(
                    None
                    if record.get("satisfaction_rating") is None
                    else float(record["satisfaction_rating"])
                ),
            )
        )
    signals = []
    for signal in raw.get("recent_session_signals", []):
        if not isinstance(signal, dict):
            continue
        signals.append(
            SessionSelectionSignal(
                product_id=str(signal["product_id"]),
                signal=str(signal["signal"]),
                noted_at=_parse_datetime(signal.get("noted_at")),
            )
        )
    return SelectionContext(prior_purchases=prior_purchases, recent_session_signals=signals)


def _agent_plan_from_dict(raw: dict[str, object]) -> AgentPlan:
    return AgentPlan(
        mode=str(raw["mode"]),
        search_queries=[
            AgentSearchQuery(item_name=str(item["item_name"]), query=str(item["query"]))
            for item in raw.get("search_queries", [])
            if isinstance(item, dict)
        ],
        operator_note=str(raw["operator_note"]),
        selection_brief=str(raw["selection_brief"]),
        warnings=[str(item) for item in raw.get("warnings", [])],
    )


def _parse_datetime(value: object) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    if value is None:
        return datetime.now(UTC)
    normalized = str(value).replace("Z", "+00:00")
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed


def _updated_workflow_performance(
    current: dict[str, object] | None,
    *,
    stage: str,
    elapsed_seconds: float,
    counts: dict[str, int] | None = None,
    nested_timings: dict[str, object] | None = None,
) -> dict[str, object]:
    base = {"timings_ms": {}, "counts": {}}
    if isinstance(current, dict):
        base["timings_ms"] = dict(current.get("timings_ms", {}))
        base["counts"] = dict(current.get("counts", {}))
    timings = base["timings_ms"]
    timings[stage] = round(float(timings.get(stage, 0.0)) + (elapsed_seconds * 1000.0), 2)
    for key, value in (counts or {}).items():
        base["counts"][key] = int(base["counts"].get(key, 0)) + int(value)
    if nested_timings:
        for key, value in nested_timings.items():
            metric_key = f"browser_agent.{key}"
            timings[metric_key] = round(float(timings.get(metric_key, 0.0)) + float(value), 2)
    return base


def _integration_result_from_state(state: LiveWorkflowState) -> IntegrationRunResult:
    return IntegrationRunResult(
        success=bool(state.get("success", False)),
        request=(
            None
            if "request" not in state
            else _shopping_request_from_dict(state["request"])
        ),
        selections=[_selected_product_from_dict(raw) for raw in state.get("selections", [])],
        cart_results=[_cart_result_from_dict(raw) for raw in state.get("cart_results", [])],
        notification_payload=(
            None
            if "notification_payload" not in state
            else _notification_payload_from_dict(state["notification_payload"])
        ),
        failed_stage=state.get("failed_stage"),
        failure_message=state.get("failure_message"),
        performance=dict(state.get("performance", {})),
    )
