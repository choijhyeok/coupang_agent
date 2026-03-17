from __future__ import annotations

import unittest
from datetime import UTC, datetime

from coupang_cart_agent.azure_openai import AgentPlan, AgentSearchQuery
from coupang_cart_agent.contracts import (
    BrowserAgentAction,
    BrowserAgentActionType,
    BrowserObservation,
    CartAddFailureReason,
    IntakeMode,
    ObservedCartItem,
    ObservedProduct,
    RequestSession,
    RequestedItem,
    ShoppingRequest,
    ShoppingRequestEnvelope,
)
from coupang_cart_agent.live_browser_agent import (
    CoupangLiveBrowserShoppingAgent,
    DeterministicBrowserAgentModel,
)
from coupang_cart_agent.live_workflow import CoupangCartAgentLiveWorkflow, InMemoryOperationalStore
from coupang_cart_agent.notifications import RetryingNotificationService
from coupang_cart_agent.cart_executor import LoginRequiredError


class FakeCartSnapshot:
    def __init__(self, item_count: int) -> None:
        self.item_count = item_count
        self.summary = f"cart_count={item_count}"


class SequencedBrowserDriver:
    def __init__(
        self,
        observations: list[BrowserObservation],
        *,
        checkout_started: bool = False,
        verification_observations: list[BrowserObservation] | None = None,
    ) -> None:
        self._observations = observations
        self._observe_calls = 0
        self._cart_snapshots = [FakeCartSnapshot(0), FakeCartSnapshot(1)]
        self._snapshot_calls = 0
        self._checkout_started = checkout_started
        self._verification_observations = verification_observations or []
        self._verification_calls = 0
        self.executed_actions: list[str] = []
        self.executed_action_objects: list[object] = []

    def attach_to_logged_in_session(self, credentials=None) -> str:
        return "attached_browser_use_profile"

    def assert_logged_in(self) -> None:
        return None

    def cart_snapshot(self) -> FakeCartSnapshot:
        index = min(self._snapshot_calls, len(self._cart_snapshots) - 1)
        self._snapshot_calls += 1
        return self._cart_snapshots[index]

    def checkout_started(self) -> bool:
        return self._checkout_started

    def observe(self, *, step_index: int, last_action_summary: str | None = None) -> BrowserObservation:
        index = min(self._observe_calls, len(self._observations) - 1)
        self._observe_calls += 1
        return self._observations[index]

    def observe_cart_verification(self) -> BrowserObservation:
        if self._verification_observations:
            index = min(self._verification_calls, len(self._verification_observations) - 1)
            self._verification_calls += 1
            return self._verification_observations[index]
        source = self._observations[-1]
        selected_name = str(source.selected_product_hint.get("name") or source.body_text_excerpt or "검증 상품").strip()
        quantity = self._cart_snapshots[-1].item_count or 1
        return BrowserObservation(
            step_index=0,
            url="https://cart.coupang.com/cartView.pang",
            title="쿠팡 장바구니",
            page_kind="browse",
            body_text_excerpt=f"{selected_name} 수량 {quantity}",
            accessibility_lines=[f"link:{selected_name}", f"button:수량 {quantity}"],
            screenshot_base64="ZmFrZS1jYXJ0LXNuYXBzaG90",
            interactive_elements=[f"link:{selected_name}", f"button:수량 {quantity}"],
            cart_items=[
                ObservedCartItem(
                    name=selected_name,
                    quantity=quantity,
                    quantity_text=f"{quantity}개",
                )
            ],
            cart_count=quantity,
        )

    def execute_action(self, action) -> str:
        self.executed_action_objects.append(action)
        self.executed_actions.append(action.action_type.value)
        return f"executed:{action.action_type.value}"


class StaticPlanner:
    def plan_request(self, request, *, prior_purchases=None, recent_session_signals=None):
        return AgentPlan(
            mode="test",
            search_queries=[AgentSearchQuery(item_name=item.name, query=f"{item.name} 쿠팡") for item in request.items],
            operator_note="Prefer strong rating and reviews.",
            selection_brief="Search live and add to cart safely.",
            warnings=[],
        )


class StaticShoppingAgent:
    def __init__(self, result) -> None:
        self.result = result
        self.calls = 0

    def run(self, *, request, search_queries, operator_note, selection_brief):
        self.calls += 1
        return self.result


class RaisingShoppingAgent:
    def __init__(self, exc: Exception) -> None:
        self.exc = exc

    def run(self, *, request, search_queries, operator_note, selection_brief):
        raise self.exc


class StaticDecisionModel:
    def __init__(self, action) -> None:
        self.action = action
        self._fallback = DeterministicBrowserAgentModel()

    def decide(self, *, context, observation):
        if observation.page_kind != "browse":
            return self._fallback.decide(context=context, observation=observation)
        return self.action


class LiveBrowserAgentTests(unittest.TestCase):
    def _request(
        self,
        *,
        text: str,
        item_name: str = "콜라 제로",
        constraints: list[str] | None = None,
    ) -> ShoppingRequest:
        return ShoppingRequest(
            user_id="telegram:test-user",
            chat_id="telegram-chat",
            items=[RequestedItem(name=item_name, quantity=1, constraints=constraints or [])],
            raw_text=text,
            request_id="req-live-browser",
            received_at=datetime(2026, 3, 12, 10, 0, tzinfo=UTC),
        )

    def test_agent_runs_search_to_cart_without_fixed_url(self) -> None:
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
                    url="https://www.coupang.com/np/search?q=%EC%BD%9C%EB%9D%BC",
                    title="검색 결과",
                    page_kind="search_results",
                    body_text_excerpt="콜라 제로 추천 16,900원 평점 4.8 리뷰 12,431",
                    interactive_elements=["link:코카콜라 제로 355ml x 24"],
                    observed_products=[
                        ObservedProduct(
                            name="코카콜라 제로 355ml x 24",
                            href="https://www.coupang.com/vp/products/CP-1001",
                            price_text="16,900원",
                            rating_text="4.8",
                            review_count_text="12,431",
                            badges=["Rocket"],
                        )
                    ],
                ),
                BrowserObservation(
                    step_index=3,
                    url="https://www.coupang.com/vp/products/CP-1001",
                    title="상품 상세",
                    page_kind="product_page",
                    body_text_excerpt="코카콜라 제로 355ml x 24 장바구니 담기",
                    interactive_elements=["button:장바구니 담기"],
                    selected_product_hint={
                        "name": "코카콜라 제로 355ml x 24",
                        "href": "https://www.coupang.com/vp/products/CP-1001",
                        "price_text": "16,900원",
                        "rating_text": "4.8",
                        "review_count_text": "12,431",
                        "badges": ["Rocket"],
                    },
                    add_to_cart_visible=True,
                ),
            ]
        )
        agent = CoupangLiveBrowserShoppingAgent(
            driver=driver,
            model=DeterministicBrowserAgentModel(),
        )

        result = agent.run(
            request=self._request(text="콜라 제로 1개 담아줘"),
            search_queries={"콜라 제로": "콜라 제로 쿠팡"},
            operator_note="Prefer highly rated products.",
            selection_brief="Search live and add to cart.",
        )

        self.assertEqual(driver.executed_actions, ["search", "click", "add_to_cart"])
        self.assertTrue(result.cart_results[0].success)
        self.assertEqual(result.selections[0].candidate.product_id, "CP-1001")
        self.assertEqual(result.cart_results[0].cart_count_before, 0)
        self.assertEqual(result.cart_results[0].cart_count_after, 1)

    def test_agent_stops_on_option_ambiguity(self) -> None:
        driver = SequencedBrowserDriver(
            [
                BrowserObservation(
                    step_index=1,
                    url="https://www.coupang.com/np/search?q=%EB%85%B8%ED%8A%B8%EB%B6%81",
                    title="검색 결과",
                    page_kind="search_results",
                    body_text_excerpt="게이밍 노트북 추천",
                    interactive_elements=["link:게이밍 노트북"],
                    observed_products=[
                        ObservedProduct(
                            name="게이밍 노트북",
                            href="https://www.coupang.com/vp/products/LAPTOP-1",
                            price_text="1,299,000원",
                            rating_text="4.7",
                            review_count_text="820",
                        )
                    ],
                ),
                BrowserObservation(
                    step_index=2,
                    url="https://www.coupang.com/vp/products/LAPTOP-1",
                    title="상품 상세",
                    page_kind="product_page",
                    body_text_excerpt="옵션 선택이 필요합니다.",
                    interactive_elements=["button:Black", "button:White"],
                    selected_product_hint={
                        "name": "게이밍 노트북",
                        "href": "https://www.coupang.com/vp/products/LAPTOP-1",
                        "price_text": "1,299,000원",
                        "rating_text": "4.7",
                        "review_count_text": "820",
                    },
                    available_options=["Black", "White"],
                ),
            ]
        )
        agent = CoupangLiveBrowserShoppingAgent(
            driver=driver,
            model=DeterministicBrowserAgentModel(),
        )

        result = agent.run(
            request=self._request(
                text="빨간색 게이밍 노트북 1개 담아줘",
                item_name="게이밍 노트북",
                constraints=["red"],
            ),
            search_queries={"게이밍 노트북": "게이밍 노트북"},
            operator_note="Avoid ambiguous options.",
            selection_brief="Stop safely when the option match is unclear.",
        )

        self.assertFalse(result.cart_results[0].success)
        self.assertEqual(result.cart_results[0].failure_reason, CartAddFailureReason.AMBIGUITY)
        self.assertEqual(result.cart_results[0].stage.value, "option_selection")

    def test_agent_selects_quantity_option_when_request_quantity_matches(self) -> None:
        driver = SequencedBrowserDriver(
            [
                BrowserObservation(
                    step_index=1,
                    url="https://www.coupang.com/vp/products/ONION-300G",
                    title="상품 상세",
                    page_kind="product_page",
                    body_text_excerpt="수량 옵션이 있습니다.",
                    interactive_elements=["button:1개", "button:2개"],
                    selected_product_hint={
                        "name": "한끼 양파(대), 300g, 1개",
                        "href": "https://www.coupang.com/vp/products/ONION-300G",
                    },
                    available_options=["1개", "2개"],
                ),
                BrowserObservation(
                    step_index=2,
                    url="https://www.coupang.com/vp/products/ONION-300G",
                    title="상품 상세",
                    page_kind="product_page",
                    body_text_excerpt="장바구니 담기",
                    interactive_elements=["button:장바구니 담기"],
                    selected_product_hint={
                        "name": "한끼 양파(대), 300g, 1개",
                        "href": "https://www.coupang.com/vp/products/ONION-300G",
                    },
                    add_to_cart_visible=True,
                ),
            ]
        )
        agent = CoupangLiveBrowserShoppingAgent(
            driver=driver,
            model=DeterministicBrowserAgentModel(),
        )

        result = agent.run(
            request=self._request(text="한끼 양파 300g 1개 담아줘", item_name="한끼 양파 300g"),
            search_queries={"한끼 양파 300g": "한끼 양파 300g"},
            operator_note="Use quantity options when they match the request.",
            selection_brief="Select the matching quantity before add-to-cart.",
        )

        self.assertEqual(driver.executed_actions, ["select_option", "add_to_cart"])
        self.assertTrue(result.cart_results[0].success)

    def test_agent_prefers_in_stock_search_result_when_sold_out_result_scores_higher(self) -> None:
        driver = SequencedBrowserDriver(
            [
                BrowserObservation(
                    step_index=1,
                    url="https://www.coupang.com/np/search?q=%EC%83%9D%EC%88%98",
                    title="검색 결과",
                    page_kind="search_results",
                    body_text_excerpt="생수 추천",
                    interactive_elements=["link:품절 프리미엄 생수", "link:재고 있음 생수"],
                    observed_products=[
                        ObservedProduct(
                            name="품절 프리미엄 생수",
                            href="https://www.coupang.com/vp/products/WATER-SOLD-OUT",
                            price_text="12,900원",
                            rating_text="4.9",
                            review_count_text="8,200",
                            sold_out=True,
                        ),
                        ObservedProduct(
                            name="재고 있음 생수",
                            href="https://www.coupang.com/vp/products/WATER-IN-STOCK",
                            price_text="13,900원",
                            rating_text="4.8",
                            review_count_text="7,900",
                        ),
                    ],
                ),
                BrowserObservation(
                    step_index=2,
                    url="https://www.coupang.com/vp/products/WATER-IN-STOCK",
                    title="상품 상세",
                    page_kind="product_page",
                    body_text_excerpt="장바구니 담기",
                    interactive_elements=["button:장바구니 담기"],
                    selected_product_hint={
                        "name": "재고 있음 생수",
                        "href": "https://www.coupang.com/vp/products/WATER-IN-STOCK",
                        "price_text": "13,900원",
                        "rating_text": "4.8",
                        "review_count_text": "7,900",
                    },
                    add_to_cart_visible=True,
                ),
            ]
        )
        agent = CoupangLiveBrowserShoppingAgent(
            driver=driver,
            model=DeterministicBrowserAgentModel(),
        )

        result = agent.run(
            request=self._request(text="생수 1개 담아줘", item_name="생수"),
            search_queries={"생수": "생수 쿠팡"},
            operator_note="Avoid sold out products.",
            selection_brief="Prefer items that can be added to cart immediately.",
        )

        self.assertEqual(driver.executed_actions, ["click", "add_to_cart"])
        self.assertTrue(result.cart_results[0].success)
        self.assertEqual(result.selections[0].candidate.product_id, "WATER-IN-STOCK")

    def test_agent_does_not_stop_on_search_page_sold_out_copy_when_in_stock_results_exist(self) -> None:
        driver = SequencedBrowserDriver(
            [
                BrowserObservation(
                    step_index=1,
                    url="https://www.coupang.com/np/search?q=%EC%96%91%ED%8C%8C",
                    title="검색 결과",
                    page_kind="search_results",
                    body_text_excerpt="한끼 양파 1개 품절임박 5 재고 있음 양파 1개",
                    interactive_elements=["link:한끼 양파 1개", "link:재고 있음 양파 1개"],
                    observed_products=[
                        ObservedProduct(
                            name="한끼 양파 1개",
                            href="https://www.coupang.com/vp/products/ONION-1",
                            price_text="1,260원",
                            rating_text="4.9",
                            review_count_text="25,924",
                        ),
                        ObservedProduct(
                            name="한끼 양파 6개",
                            href="https://www.coupang.com/vp/products/ONION-6",
                            price_text="6,830원",
                            rating_text="4.9",
                            review_count_text="25,924",
                            sold_out=True,
                        ),
                    ],
                ),
                BrowserObservation(
                    step_index=2,
                    url="https://www.coupang.com/vp/products/ONION-1",
                    title="상품 상세",
                    page_kind="product_page",
                    body_text_excerpt="장바구니 담기",
                    interactive_elements=["button:장바구니 담기"],
                    selected_product_hint={
                        "name": "한끼 양파 1개",
                        "href": "https://www.coupang.com/vp/products/ONION-1",
                    },
                    add_to_cart_visible=True,
                ),
            ]
        )
        agent = CoupangLiveBrowserShoppingAgent(
            driver=driver,
            model=DeterministicBrowserAgentModel(),
        )

        result = agent.run(
            request=self._request(text="양파 1개 담아줘", item_name="양파"),
            search_queries={"양파": "양파"},
            operator_note="Ignore sold-out copy on search result pages when an in-stock result is available.",
            selection_brief="Continue into the best in-stock search result.",
        )

        self.assertEqual(driver.executed_actions, ["click", "add_to_cart"])
        self.assertTrue(result.cart_results[0].success)

    def test_agent_prefers_requested_non_ad_product_over_better_scored_ad(self) -> None:
        driver = SequencedBrowserDriver(
            [
                BrowserObservation(
                    step_index=1,
                    url="https://www.coupang.com/np/search?q=%ED%95%9C%EB%81%BC+%EC%96%91%ED%8C%8C+300g",
                    title="검색 결과",
                    page_kind="search_results",
                    body_text_excerpt="한끼 양파 300g 검색 결과",
                    interactive_elements=["link:광고 양파 5kg", "link:한끼 양파 300g"],
                    observed_products=[
                        ObservedProduct(
                            name="광고 양파 5kg AD",
                            href=(
                                "https://www.coupang.com/vp/products/ONION-AD"
                                "?sourceType=srp_product_ads"
                            ),
                            price_text="17,500원",
                            rating_text="5.0",
                            review_count_text="5,706",
                        ),
                        ObservedProduct(
                            name="한끼 양파(대), 300g, 1개",
                            href="https://www.coupang.com/vp/products/ONION-300G",
                            price_text="1,260원",
                            rating_text="4.9",
                            review_count_text="25,924",
                        ),
                    ],
                ),
                BrowserObservation(
                    step_index=2,
                    url="https://www.coupang.com/vp/products/ONION-300G",
                    title="상품 상세",
                    page_kind="product_page",
                    body_text_excerpt="장바구니 담기",
                    interactive_elements=["button:장바구니 담기"],
                    selected_product_hint={
                        "name": "한끼 양파(대), 300g, 1개",
                        "href": "https://www.coupang.com/vp/products/ONION-300G",
                        "price_text": "1,260원",
                        "rating_text": "4.9",
                        "review_count_text": "25,924",
                    },
                    add_to_cart_visible=True,
                ),
            ]
        )
        agent = CoupangLiveBrowserShoppingAgent(
            driver=driver,
            model=DeterministicBrowserAgentModel(),
        )

        result = agent.run(
            request=self._request(text="한끼 양파 300g 1개 담아줘", item_name="한끼 양파 300g"),
            search_queries={"한끼 양파 300g": "한끼 양파 300g"},
            operator_note="Prefer the exact requested size and avoid ad-only mismatches.",
            selection_brief="Choose the most relevant non-ad product for the request.",
        )

        self.assertEqual(driver.executed_actions, ["click", "add_to_cart"])
        self.assertTrue(result.cart_results[0].success)
        self.assertEqual(result.selections[0].candidate.product_id, "ONION-300G")

    def test_agent_falls_back_to_item_name_when_planned_search_query_does_not_preserve_it(self) -> None:
        driver = SequencedBrowserDriver(
            [
                BrowserObservation(
                    step_index=1,
                    url="https://www.coupang.com",
                    title="쿠팡",
                    page_kind="browse",
                    body_text_excerpt="검색 시작 전",
                ),
                BrowserObservation(
                    step_index=2,
                    url="https://www.coupang.com/np/search?q=%ED%95%9C%EB%81%BC+%EC%96%91%ED%8C%8C+300g",
                    title="검색 결과",
                    page_kind="search_results",
                    body_text_excerpt="검색 결과 없음",
                ),
            ]
        )
        agent = CoupangLiveBrowserShoppingAgent(
            driver=driver,
            model=DeterministicBrowserAgentModel(),
            max_steps_per_item=2,
        )

        result = agent.run(
            request=self._request(text="한끼 양파 300g 1개 담아줘", item_name="한끼 양파 300g"),
            search_queries={"한끼 양파 300g": "한끼 양파 소용량 300g"},
            operator_note="Keep search queries grounded in the request item.",
            selection_brief="Do not drift away from the item name during search.",
        )

        self.assertEqual(driver.executed_actions[0], "search")
        self.assertEqual(driver.executed_action_objects[0].query, "한끼 양파 300g")
        self.assertFalse(result.cart_results[0].success)

    def test_agent_forces_search_when_model_stops_from_cart_browse_context(self) -> None:
        driver = SequencedBrowserDriver(
            [
                BrowserObservation(
                    step_index=1,
                    url="https://cart.coupang.com/cartView.pang",
                    title="쿠팡! | 장바구니",
                    page_kind="browse",
                    body_text_excerpt="장바구니(1) 몽베스트 생수 옵션: 2L, 6개",
                    interactive_elements=["button:총 1개 상품 구매하기"],
                    cart_count=1,
                ),
                BrowserObservation(
                    step_index=2,
                    url="https://www.coupang.com/np/search?q=%EC%83%9D%EC%88%98+2L",
                    title="검색 결과",
                    page_kind="search_results",
                    body_text_excerpt="생수 2L 검색 결과",
                    observed_products=[
                        ObservedProduct(
                            name="생수 2L 6개",
                            href="https://www.coupang.com/vp/products/WATER-2L",
                            price_text="5,400원",
                            rating_text="4.8",
                            review_count_text="405,145",
                        )
                    ],
                ),
                BrowserObservation(
                    step_index=3,
                    url="https://www.coupang.com/vp/products/WATER-2L",
                    title="상품 상세",
                    page_kind="product_page",
                    body_text_excerpt="장바구니 담기",
                    add_to_cart_visible=True,
                    selected_product_hint={
                        "name": "생수 2L 6개",
                        "href": "https://www.coupang.com/vp/products/WATER-2L",
                    },
                ),
            ]
        )
        agent = CoupangLiveBrowserShoppingAgent(
            driver=driver,
            model=StaticDecisionModel(
                action=BrowserAgentAction(
                    action_type=BrowserAgentActionType.STOP,
                    blocker_reason=CartAddFailureReason.OPTION_MISMATCH,
                    reasoning_summary="Stop on existing cart state.",
                )
            ),
        )

        result = agent.run(
            request=self._request(text="생수 2L 1개 담아줘", item_name="생수 2L"),
            search_queries={"생수 2L": "생수 2L"},
            operator_note="Start a fresh search instead of reusing cart contents.",
            selection_brief="Cart page is only a starting point for live search.",
        )

        self.assertEqual(driver.executed_actions[0], "search")
        self.assertTrue(result.cart_results[0].success)

    def test_agent_classifies_selected_product_sold_out_hint_as_out_of_stock(self) -> None:
        driver = SequencedBrowserDriver(
            [
                BrowserObservation(
                    step_index=1,
                    url="https://www.coupang.com/vp/products/SOLD-OUT-1",
                    title="상품 상세",
                    page_kind="product_page",
                    body_text_excerpt="상품 상세 페이지",
                    interactive_elements=["text:품절"],
                    selected_product_hint={
                        "name": "품절 상품",
                        "href": "https://www.coupang.com/vp/products/SOLD-OUT-1",
                        "price_text": "9,900원",
                        "rating_text": "4.7",
                        "review_count_text": "100",
                        "sold_out": True,
                    },
                ),
            ]
        )
        agent = CoupangLiveBrowserShoppingAgent(
            driver=driver,
            model=DeterministicBrowserAgentModel(),
        )

        result = agent.run(
            request=self._request(text="품절 상품 1개 담아줘", item_name="품절 상품"),
            search_queries={"품절 상품": "품절 상품 쿠팡"},
            operator_note="Stop on sold-out detail pages.",
            selection_brief="Classify sold-out detail pages safely.",
        )

        self.assertFalse(result.cart_results[0].success)
        self.assertEqual(result.cart_results[0].failure_reason, CartAddFailureReason.OUT_OF_STOCK)
        self.assertEqual(result.cart_results[0].stage.value, "product_page")

    def test_agent_scrolls_when_add_to_cart_exists_below_fold(self) -> None:
        driver = SequencedBrowserDriver(
            [
                BrowserObservation(
                    step_index=1,
                    url="https://www.coupang.com/vp/products/CEREAL-1",
                    title="상품 상세",
                    page_kind="product_page",
                    body_text_excerpt="시리얼 상품 상세",
                    interactive_elements=["button:더보기"],
                    selected_product_hint={
                        "name": "켈로그 시리얼 550g",
                        "href": "https://www.coupang.com/vp/products/CEREAL-1",
                    },
                    add_to_cart_available=True,
                    add_to_cart_visible=False,
                    add_to_cart_in_viewport=False,
                    observation_engine="scrapling",
                ),
                BrowserObservation(
                    step_index=2,
                    url="https://www.coupang.com/vp/products/CEREAL-1",
                    title="상품 상세",
                    page_kind="product_page",
                    body_text_excerpt="켈로그 시리얼 550g 장바구니 담기",
                    interactive_elements=["button:장바구니 담기"],
                    selected_product_hint={
                        "name": "켈로그 시리얼 550g",
                        "href": "https://www.coupang.com/vp/products/CEREAL-1",
                    },
                    add_to_cart_available=True,
                    add_to_cart_visible=True,
                    add_to_cart_in_viewport=True,
                    observation_engine="scrapling",
                ),
            ]
        )
        agent = CoupangLiveBrowserShoppingAgent(driver=driver, model=DeterministicBrowserAgentModel())

        result = agent.run(
            request=self._request(text="시리얼 1개 담아줘", item_name="시리얼"),
            search_queries={"시리얼": "시리얼"},
            operator_note="Scroll when the CTA exists below the fold.",
            selection_brief="Recover by scrolling and reobserving before giving up.",
        )

        self.assertEqual(driver.executed_actions, ["scroll", "add_to_cart"])
        self.assertTrue(result.cart_results[0].success)
        self.assertEqual(result.steps[0].action.action_type, BrowserAgentActionType.SCROLL)

    def test_agent_replans_to_substitute_when_first_product_is_purchase_restricted(self) -> None:
        driver = SequencedBrowserDriver(
            [
                BrowserObservation(
                    step_index=1,
                    url="https://www.coupang.com/np/search?q=%EC%8B%9C%EB%A6%AC%EC%96%BC",
                    title="검색 결과",
                    page_kind="search_results",
                    body_text_excerpt="시리얼 검색 결과",
                    observed_products=[
                        ObservedProduct(
                            name="제한 시리얼 500g",
                            href="https://www.coupang.com/vp/products/CEREAL-RESTRICTED",
                            price_text="7,900원",
                            rating_text="4.9",
                            review_count_text="500",
                        ),
                        ObservedProduct(
                            name="대체 시리얼 550g",
                            href="https://www.coupang.com/vp/products/CEREAL-ALT",
                            price_text="8,400원",
                            rating_text="4.8",
                            review_count_text="1400",
                        ),
                    ],
                    observation_engine="scrapling",
                ),
                BrowserObservation(
                    step_index=2,
                    url="https://www.coupang.com/vp/products/CEREAL-RESTRICTED",
                    title="상품 상세",
                    page_kind="product_page",
                    body_text_excerpt="로켓프레시 상품은 장바구니에 담을 수 없습니다.",
                    selected_product_hint={
                        "name": "제한 시리얼 500g",
                        "href": "https://www.coupang.com/vp/products/CEREAL-RESTRICTED",
                    },
                    purchase_blocked_reason="rocket_fresh_restriction",
                    observation_engine="scrapling",
                ),
                BrowserObservation(
                    step_index=3,
                    url="https://www.coupang.com/np/search?q=%EC%8B%9C%EB%A6%AC%EC%96%BC",
                    title="검색 결과",
                    page_kind="search_results",
                    body_text_excerpt="시리얼 검색 결과",
                    observed_products=[
                        ObservedProduct(
                            name="제한 시리얼 500g",
                            href="https://www.coupang.com/vp/products/CEREAL-RESTRICTED",
                            price_text="7,900원",
                            rating_text="4.9",
                            review_count_text="500",
                        ),
                        ObservedProduct(
                            name="대체 시리얼 550g",
                            href="https://www.coupang.com/vp/products/CEREAL-ALT",
                            price_text="8,400원",
                            rating_text="4.8",
                            review_count_text="1400",
                        ),
                    ],
                    observation_engine="scrapling",
                ),
                BrowserObservation(
                    step_index=4,
                    url="https://www.coupang.com/vp/products/CEREAL-ALT",
                    title="상품 상세",
                    page_kind="product_page",
                    body_text_excerpt="대체 시리얼 550g 장바구니 담기",
                    selected_product_hint={
                        "name": "대체 시리얼 550g",
                        "href": "https://www.coupang.com/vp/products/CEREAL-ALT",
                    },
                    add_to_cart_available=True,
                    add_to_cart_visible=True,
                    observation_engine="scrapling",
                ),
            ]
        )
        agent = CoupangLiveBrowserShoppingAgent(driver=driver, model=DeterministicBrowserAgentModel())

        result = agent.run(
            request=self._request(text="시리얼 1개 담아줘", item_name="시리얼"),
            search_queries={"시리얼": "시리얼"},
            operator_note="Retry with a substitute when the first product cannot be added to cart.",
            selection_brief="Preserve item intent across substitute recovery.",
        )

        self.assertEqual(driver.executed_actions, ["click", "go_back", "click", "add_to_cart"])
        self.assertTrue(result.cart_results[0].success)
        self.assertEqual(result.selections[0].candidate.product_id, "CEREAL-ALT")

    def test_agent_blocks_false_success_and_recovers_after_wrong_cart_verification(self) -> None:
        driver = SequencedBrowserDriver(
            [
                BrowserObservation(
                    step_index=1,
                    url="https://www.coupang.com/np/search?q=%EC%8B%9C%EB%A6%AC%EC%96%BC",
                    title="검색 결과",
                    page_kind="search_results",
                    body_text_excerpt="시리얼 검색 결과",
                    observed_products=[
                        ObservedProduct(
                            name="시리얼 500g",
                            href="https://www.coupang.com/vp/products/CEREAL-1",
                            price_text="7,900원",
                            rating_text="4.8",
                            review_count_text="900",
                        ),
                        ObservedProduct(
                            name="시리얼 550g",
                            href="https://www.coupang.com/vp/products/CEREAL-2",
                            price_text="8,400원",
                            rating_text="4.8",
                            review_count_text="1300",
                        ),
                    ],
                    observation_engine="scrapling",
                ),
                BrowserObservation(
                    step_index=2,
                    url="https://www.coupang.com/vp/products/CEREAL-1",
                    title="상품 상세",
                    page_kind="product_page",
                    body_text_excerpt="시리얼 500g 장바구니 담기",
                    selected_product_hint={
                        "name": "시리얼 500g",
                        "href": "https://www.coupang.com/vp/products/CEREAL-1",
                    },
                    add_to_cart_available=True,
                    add_to_cart_visible=True,
                    observation_engine="scrapling",
                ),
                BrowserObservation(
                    step_index=3,
                    url="https://www.coupang.com/np/search?q=%EC%8B%9C%EB%A6%AC%EC%96%BC",
                    title="검색 결과",
                    page_kind="search_results",
                    body_text_excerpt="시리얼 검색 결과",
                    observed_products=[
                        ObservedProduct(
                            name="시리얼 500g",
                            href="https://www.coupang.com/vp/products/CEREAL-1",
                            price_text="7,900원",
                            rating_text="4.8",
                            review_count_text="900",
                        ),
                        ObservedProduct(
                            name="시리얼 550g",
                            href="https://www.coupang.com/vp/products/CEREAL-2",
                            price_text="8,400원",
                            rating_text="4.8",
                            review_count_text="1300",
                        ),
                    ],
                    observation_engine="scrapling",
                ),
                BrowserObservation(
                    step_index=4,
                    url="https://www.coupang.com/vp/products/CEREAL-2",
                    title="상품 상세",
                    page_kind="product_page",
                    body_text_excerpt="시리얼 550g 장바구니 담기",
                    selected_product_hint={
                        "name": "시리얼 550g",
                        "href": "https://www.coupang.com/vp/products/CEREAL-2",
                    },
                    add_to_cart_available=True,
                    add_to_cart_visible=True,
                    observation_engine="scrapling",
                ),
            ],
            verification_observations=[
                BrowserObservation(
                    step_index=0,
                    url="https://cart.coupang.com/cartView.pang",
                    title="쿠팡 장바구니",
                    page_kind="browse",
                    body_text_excerpt="양파 추천 수량 1",
                    cart_items=[ObservedCartItem(name="양파 추천", quantity=1, quantity_text="1개")],
                    screenshot_base64="ZmFrZS1jYXJ0LXNuYXBzaG90",
                    observation_engine="scrapling",
                ),
                BrowserObservation(
                    step_index=0,
                    url="https://cart.coupang.com/cartView.pang",
                    title="쿠팡 장바구니",
                    page_kind="browse",
                    body_text_excerpt="시리얼 550g 수량 1",
                    cart_items=[ObservedCartItem(name="시리얼 550g", quantity=1, quantity_text="1개")],
                    screenshot_base64="ZmFrZS1jYXJ0LXNuYXBzaG90",
                    observation_engine="scrapling",
                ),
            ],
        )
        agent = CoupangLiveBrowserShoppingAgent(driver=driver, model=DeterministicBrowserAgentModel())

        result = agent.run(
            request=self._request(text="시리얼 1개 담아줘", item_name="시리얼"),
            search_queries={"시리얼": "시리얼"},
            operator_note="Never send success until the requested category is verified in cart.",
            selection_brief="Recover from wrong-cart verification by trying a substitute result.",
        )

        self.assertEqual(driver.executed_actions, ["click", "add_to_cart", "go_back", "click", "add_to_cart"])
        self.assertTrue(result.cart_results[0].success)
        self.assertEqual(result.cart_results[0].evidence["goal_check"]["matched_item"]["name"], "시리얼 550g")


class BrowserWorkflowIntegrationTests(unittest.TestCase):
    def _envelope(self) -> ShoppingRequestEnvelope:
        request = ShoppingRequest(
            user_id="telegram:test-user",
            chat_id="telegram-chat",
            items=[RequestedItem(name="양파", quantity=1)],
            raw_text="양파 1개 담아줘",
            request_id="req-workflow-live",
            received_at=datetime(2026, 3, 12, 11, 0, tzinfo=UTC),
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
            inbound_message_id=request.request_id,
            raw_text=request.raw_text,
            raw_update={},
            metadata={"session_id": "telegram-session:telegram-chat:telegram:test-user"},
        )

    def test_live_workflow_prefers_browser_agent_over_candidate_source(self) -> None:
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
                    url="https://www.coupang.com/np/search?q=%EC%96%91%ED%8C%8C",
                    title="검색 결과",
                    page_kind="search_results",
                    body_text_excerpt="양파 추천",
                    interactive_elements=["link:곰곰 국내산 양파"],
                    observed_products=[
                        ObservedProduct(
                            name="곰곰 국내산 양파, 3kg, 1개",
                            href="https://www.coupang.com/vp/products/ONION-1",
                            price_text="8,900원",
                            rating_text="4.7",
                            review_count_text="1,532",
                        )
                    ],
                ),
                BrowserObservation(
                    step_index=3,
                    url="https://www.coupang.com/vp/products/ONION-1",
                    title="상품 상세",
                    page_kind="product_page",
                    body_text_excerpt="장바구니 담기",
                    interactive_elements=["button:장바구니 담기"],
                    selected_product_hint={
                        "name": "곰곰 국내산 양파, 3kg, 1개",
                        "href": "https://www.coupang.com/vp/products/ONION-1",
                        "price_text": "8,900원",
                        "rating_text": "4.7",
                        "review_count_text": "1,532",
                    },
                    add_to_cart_visible=True,
                ),
            ]
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
            agent_planner=StaticPlanner(),
            shopping_agent=shopping_agent,
            checkpointer=None,
        )

        result = workflow.run_envelope(self._envelope())

        self.assertTrue(result.success)
        self.assertEqual(driver.executed_actions, ["search", "click", "add_to_cart"])
        self.assertIn("장바구니 담기를 완료했습니다.", delivered_messages[0][1])
        self.assertEqual(store.runs[-1]["agent_reasoning_summary"], "Completed browser-guided shopping flow.")

    def test_live_workflow_classifies_browser_agent_login_blocker(self) -> None:
        delivered_messages: list[tuple[str, str]] = []

        def sender(chat_id: str, text: str) -> None:
            delivered_messages.append((chat_id, text))

        store = InMemoryOperationalStore()
        workflow = CoupangCartAgentLiveWorkflow(
            candidate_source=lambda request: {},
            cart_service=None,
            notification_service=RetryingNotificationService(sender=sender, max_attempts=1),
            operational_store=store,
            agent_planner=StaticPlanner(),
            shopping_agent=RaisingShoppingAgent(
                LoginRequiredError("Attach mode requires an operator-prepared logged-in Coupang session.")
            ),
            checkpointer=None,
        )

        result = workflow.run_envelope(self._envelope())

        self.assertFalse(result.success)
        self.assertEqual(result.failed_stage, "session")
        self.assertEqual(result.cart_results[0].failure_reason, CartAddFailureReason.LOGIN_REQUIRED)
        self.assertIn("login_required", delivered_messages[0][1])


if __name__ == "__main__":
    unittest.main()
