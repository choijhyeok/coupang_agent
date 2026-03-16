from __future__ import annotations

import base64
import json
import re
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from typing import Protocol

from .contracts import (
    BrowserAgentAction,
    BrowserAgentActionType,
    BrowserAgentRun,
    BrowserAgentStep,
    BrowserObservation,
    CartAddFailureReason,
    CartAddResult,
    CartAddStage,
    ObservedProduct,
    ProductCandidate,
    RequestedItem,
    SelectedProduct,
    ShoppingRequest,
)
from .cart_verification import CartVerificationModel, DeterministicCartVerifier


class BrowserAgentDriver(Protocol):
    def attach_to_logged_in_session(self, credentials=None) -> str: ...

    def assert_logged_in(self) -> None: ...

    def cart_snapshot(self): ...

    def checkout_started(self) -> bool: ...

    def observe(
        self,
        *,
        step_index: int,
        last_action_summary: str | None = None,
    ) -> BrowserObservation: ...

    def observe_cart_verification(self) -> BrowserObservation: ...

    def execute_action(self, action: BrowserAgentAction) -> str: ...


@dataclass(slots=True)
class BrowserAgentContext:
    request: ShoppingRequest
    item: RequestedItem
    search_query: str
    operator_note: str
    selection_brief: str
    prior_steps: list[BrowserAgentStep]


class BrowserAgentModel(Protocol):
    def decide(self, *, context: BrowserAgentContext, observation: BrowserObservation) -> BrowserAgentAction: ...


class AzureOpenAIBrowserAgent:
    """AOAI-backed browser action decider with strict JSON output."""

    def __init__(
        self,
        *,
        endpoint: str | None,
        api_key: str | None,
        deployment: str | None,
        api_version: str = "2024-12-01-preview",
        timeout_seconds: int = 30,
        fallback_model: BrowserAgentModel | None = None,
    ) -> None:
        self._endpoint = (endpoint or "").rstrip("/")
        self._api_key = api_key or ""
        self._deployment = deployment or ""
        self._api_version = api_version
        self._timeout_seconds = timeout_seconds
        self._fallback_model = fallback_model or DeterministicBrowserAgentModel()

    def is_configured(self) -> bool:
        return bool(self._endpoint and self._api_key and self._deployment)

    def decide(self, *, context: BrowserAgentContext, observation: BrowserObservation) -> BrowserAgentAction:
        if not self.is_configured():
            return self._fallback_model.decide(context=context, observation=observation)

        payload = {
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You are a browser shopping agent for Coupang. "
                        "Never checkout or pay. "
                        "Return strict JSON with keys: action_type, target_text, target_role, target_href, "
                        "query, option_label, value, wait_seconds, reasoning_summary, blocker_reason. "
                        "Allowed action_type values: search, click, select_option, add_to_cart, wait, stop. "
                        "Use blocker_reason only when action_type=stop. "
                        "Allowed blocker_reason values: login_required, security_challenge, access_denied, "
                        "out_of_stock, option_mismatch, ambiguity, ui_element_not_found, unknown."
                    ),
                },
                _build_user_message(context=context, observation=observation),
            ],
            "response_format": {"type": "json_object"},
        }

        try:
            response = self._post(payload)
            content = _read_message_content(response)
            raw = json.loads(content)
            return _browser_action_from_dict(raw)
        except Exception:
            return self._fallback_model.decide(context=context, observation=observation)

    def _post(self, payload: dict[str, object]) -> dict[str, object]:
        url = (
            f"{self._endpoint}/openai/deployments/{self._deployment}/chat/completions"
            f"?api-version={self._api_version}"
        )
        request = urllib.request.Request(
            url=url,
            data=json.dumps(payload).encode("utf-8"),
            method="POST",
            headers={
                "Content-Type": "application/json",
                "api-key": self._api_key,
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout_seconds) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Azure OpenAI returned HTTP {exc.code}: {detail[:240]}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"Azure OpenAI request failed: {exc.reason}") from exc


class DeterministicBrowserAgentModel:
    """Test-friendly fallback model that follows the constrained action schema."""

    def decide(self, *, context: BrowserAgentContext, observation: BrowserObservation) -> BrowserAgentAction:
        blocker_reason = _classify_blocker_hint(observation.blocker_hint or "")
        if blocker_reason is not None:
            return BrowserAgentAction(
                action_type=BrowserAgentActionType.STOP,
                blocker_reason=blocker_reason,
                reasoning_summary=f"Blocked by page state: {observation.blocker_hint}",
            )
        if _observation_indicates_out_of_stock(observation):
            return BrowserAgentAction(
                action_type=BrowserAgentActionType.STOP,
                blocker_reason=CartAddFailureReason.OUT_OF_STOCK,
                reasoning_summary="Current product state indicates the item is sold out.",
            )
        if observation.available_options:
            matched = _match_option(context.item, observation.available_options)
            if matched is not None:
                return BrowserAgentAction(
                    action_type=BrowserAgentActionType.SELECT_OPTION,
                    target_text=matched,
                    option_label=matched,
                    value=matched,
                    reasoning_summary="Select the visible option that matches the request constraints.",
                )
            return BrowserAgentAction(
                action_type=BrowserAgentActionType.STOP,
                blocker_reason=CartAddFailureReason.AMBIGUITY,
                reasoning_summary="Visible options do not map cleanly to the request constraints.",
            )

        if observation.add_to_cart_visible:
            return BrowserAgentAction(
                action_type=BrowserAgentActionType.ADD_TO_CART,
                target_text="장바구니 담기",
                target_role="button",
                reasoning_summary="Product page is ready for add-to-cart.",
            )

        if observation.page_kind == "search_results" and observation.observed_products:
            chosen = _rank_observed_products(
                observation.observed_products,
                preferred_terms=[context.item.name, context.search_query],
            )[0]
            return BrowserAgentAction(
                action_type=BrowserAgentActionType.CLICK,
                target_text=chosen.name,
                target_href=chosen.href,
                target_role="link",
                reasoning_summary="Open the best visible search result using rating, reviews, and price clues.",
            )

        if observation.page_kind != "search_results":
            return BrowserAgentAction(
                action_type=BrowserAgentActionType.SEARCH,
                query=context.search_query,
                reasoning_summary="Start from search to avoid relying on fixed URLs.",
            )

        return BrowserAgentAction(
            action_type=BrowserAgentActionType.WAIT,
            wait_seconds=1.0,
            reasoning_summary="Wait briefly for the page to stabilize before the next observation.",
        )


class CoupangLiveBrowserShoppingAgent:
    """Observation-driven search-to-cart loop using a constrained model action schema."""

    def __init__(
        self,
        *,
        driver: BrowserAgentDriver,
        model: BrowserAgentModel,
        cart_verifier: CartVerificationModel | None = None,
        max_steps_per_item: int = 8,
    ) -> None:
        self._driver = driver
        self._model = model
        self._cart_verifier = cart_verifier or DeterministicCartVerifier()
        self._max_steps_per_item = max_steps_per_item

    def run(
        self,
        *,
        request: ShoppingRequest,
        search_queries: dict[str, str],
        operator_note: str,
        selection_brief: str,
    ) -> BrowserAgentRun:
        session_mode = self._driver.attach_to_logged_in_session(None)
        self._driver.assert_logged_in()
        steps: list[BrowserAgentStep] = []
        selections: list[SelectedProduct] = []
        cart_results: list[CartAddResult] = []
        last_observation: BrowserObservation | None = None

        for item in request.items:
            selected_product: SelectedProduct | None = None
            selected_options: dict[str, str] = {}
            search_query = _coerce_search_query(item, search_queries.get(item.name, item.name))

            for step_index in range(1, self._max_steps_per_item + 1):
                observation = self._driver.observe(
                    step_index=len(steps) + 1,
                    last_action_summary=None if not steps else steps[-1].execution_summary,
                )
                last_observation = observation
                context = BrowserAgentContext(
                    request=request,
                    item=item,
                    search_query=search_query,
                    operator_note=operator_note,
                    selection_brief=selection_brief,
                    prior_steps=[step for step in steps if step.item_name == item.name],
                )
                action = self._model.decide(context=context, observation=observation)
                action = _coerce_action_for_context(action=action, context=context, observation=observation)

                if action.action_type == BrowserAgentActionType.STOP:
                    failure_reason = action.blocker_reason or _classify_observation(observation)
                    selection = selected_product or _selection_from_observation(
                        item=item,
                        observation=observation,
                        reasoning_summary=action.reasoning_summary,
                        option_hints=selected_options,
                    )
                    cart_results.append(
                        _failure_result(
                            selection=selection,
                            stage=_stage_from_observation(observation),
                            failure_reason=failure_reason,
                            message=action.reasoning_summary or observation.blocker_hint or "Browser agent stopped.",
                            session_mode=session_mode,
                            observation=observation,
                        )
                    )
                    steps.append(
                        BrowserAgentStep(
                            step_index=len(steps) + 1,
                            item_name=item.name,
                            observation=observation,
                            action=action,
                            execution_summary="Agent stopped due to blocker or ambiguity.",
                        )
                    )
                    return BrowserAgentRun(
                        selections=selections + [selection],
                        cart_results=cart_results,
                        reasoning_summary=action.reasoning_summary,
                        last_observation=last_observation,
                        steps=steps,
                    )

                if action.action_type == BrowserAgentActionType.ADD_TO_CART:
                    before = self._driver.cart_snapshot()
                    execution_summary = self._driver.execute_action(action)
                    after = self._driver.cart_snapshot()
                    checkout_attempted = self._driver.checkout_started()
                    selected_product = selected_product or _selection_from_observation(
                        item=item,
                        observation=observation,
                        reasoning_summary=action.reasoning_summary,
                        option_hints=selected_options,
                    )
                    steps.append(
                        BrowserAgentStep(
                            step_index=len(steps) + 1,
                            item_name=item.name,
                            observation=observation,
                            action=action,
                            execution_summary=execution_summary,
                        )
                    )
                    if checkout_attempted:
                        cart_results.append(
                            _failure_result(
                                selection=selected_product,
                                stage=CartAddStage.ADD_TO_CART,
                                failure_reason=CartAddFailureReason.CHECKOUT_ATTEMPTED,
                                message="Cart add triggered checkout flow and was stopped.",
                                session_mode=session_mode,
                                observation=observation,
                                cart_count_before=before.item_count,
                                cart_count_after=after.item_count,
                                checkout_attempted=True,
                                selected_options=selected_options,
                            )
                        )
                        return BrowserAgentRun(
                            selections=selections + [selected_product],
                            cart_results=cart_results,
                            reasoning_summary=action.reasoning_summary,
                            last_observation=last_observation,
                            steps=steps,
                        )

                    verification_observation = self._driver.observe_cart_verification()
                    verification = self._cart_verifier.verify(
                        selection=selected_product,
                        observation=verification_observation,
                        cart_count_before=before.item_count,
                        cart_count_after=after.item_count,
                    )
                    if not verification.success:
                        cart_results.append(
                            CartAddResult(
                                success=False,
                                cart_item_id=None,
                                selected_product=selected_product,
                                stage=CartAddStage.VERIFICATION,
                                message=verification.reason,
                                failure_reason=(
                                    verification.failure_reason
                                    or CartAddFailureReason.MANUAL_REVIEW_REQUIRED
                                ),
                                cart_count_before=before.item_count,
                                cart_count_after=after.item_count,
                                checkout_attempted=False,
                                evidence={
                                    "session_mode": session_mode,
                                    "selected_options": selected_options,
                                    "reasoning_summary": action.reasoning_summary,
                                    "last_observation": asdict(observation),
                                    "verification": verification.evidence,
                                },
                            )
                        )
                        return BrowserAgentRun(
                            selections=selections + [selected_product],
                            cart_results=cart_results,
                            reasoning_summary=verification.reason,
                            last_observation=verification_observation,
                            steps=steps,
                        )

                    cart_results.append(
                        CartAddResult(
                            success=True,
                            cart_item_id=_cart_item_id_from_selection(selected_product),
                            selected_product=selected_product,
                            stage=CartAddStage.VERIFICATION,
                            message="Item added to cart and verified.",
                            cart_count_before=before.item_count,
                            cart_count_after=after.item_count,
                            checkout_attempted=False,
                            evidence={
                                "session_mode": session_mode,
                                "selected_options": selected_options,
                                "reasoning_summary": action.reasoning_summary,
                                "last_observation": asdict(observation),
                                "verification": verification.evidence,
                            },
                        )
                    )
                    selections.append(selected_product)
                    break

                execution_summary = self._driver.execute_action(action)
                steps.append(
                    BrowserAgentStep(
                        step_index=len(steps) + 1,
                        item_name=item.name,
                        observation=observation,
                        action=action,
                        execution_summary=execution_summary,
                    )
                )
                if action.action_type == BrowserAgentActionType.SELECT_OPTION:
                    key = action.option_label or action.target_text or action.value or f"option-{len(selected_options) + 1}"
                    selected_options[key] = action.value or action.target_text or key
                if action.action_type == BrowserAgentActionType.CLICK:
                    selected_product = _selection_from_observation(
                        item=item,
                        observation=observation,
                        reasoning_summary=action.reasoning_summary,
                        option_hints=selected_options,
                        target_text=action.target_text,
                        target_href=action.target_href,
                    )
            else:
                failure_observation = last_observation or BrowserObservation(
                    step_index=len(steps) + 1,
                    url="",
                    title="",
                    page_kind="unknown",
                    body_text_excerpt="",
                )
                selection = selected_product or _selection_from_observation(
                    item=item,
                    observation=failure_observation,
                    reasoning_summary="Agent step budget exhausted.",
                    option_hints=selected_options,
                )
                cart_results.append(
                    _failure_result(
                        selection=selection,
                        stage=_stage_from_observation(failure_observation),
                        failure_reason=CartAddFailureReason.UNKNOWN,
                        message="Browser agent exhausted the maximum step budget.",
                        session_mode=session_mode,
                        observation=failure_observation,
                    )
                )
                return BrowserAgentRun(
                    selections=selections + [selection],
                    cart_results=cart_results,
                    reasoning_summary="Browser agent exhausted the maximum step budget.",
                    last_observation=failure_observation,
                    steps=steps,
                )

        return BrowserAgentRun(
            selections=selections,
            cart_results=cart_results,
            reasoning_summary="Completed browser-guided shopping flow.",
            last_observation=last_observation,
            steps=steps,
        )


def _build_user_message(
    *,
    context: BrowserAgentContext,
    observation: BrowserObservation,
) -> dict[str, object]:
    prompt_payload = {
        "request_id": context.request.request_id,
        "item": asdict(context.item),
        "search_query": context.search_query,
        "operator_note": context.operator_note,
        "selection_brief": context.selection_brief,
        "prior_steps": [
            {
                "step_index": step.step_index,
                "action_type": step.action.action_type.value,
                "reasoning_summary": step.action.reasoning_summary,
                "execution_summary": step.execution_summary,
            }
            for step in context.prior_steps[-4:]
        ],
        "observation": asdict(observation),
    }
    if observation.screenshot_base64:
        return {
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": json.dumps(prompt_payload, ensure_ascii=False, default=str),
                },
                {
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:image/png;base64,{observation.screenshot_base64}",
                    },
                },
            ],
        }
    return {
        "role": "user",
        "content": json.dumps(prompt_payload, ensure_ascii=False, default=str),
    }


def _browser_action_from_dict(raw: dict[str, object]) -> BrowserAgentAction:
    action_type = BrowserAgentActionType(str(raw.get("action_type", "stop")))
    blocker_raw = raw.get("blocker_reason")
    blocker_reason = None
    if blocker_raw not in (None, ""):
        blocker_reason = CartAddFailureReason(str(blocker_raw))
    wait_seconds = raw.get("wait_seconds")
    return BrowserAgentAction(
        action_type=action_type,
        target_text=_optional_text(raw.get("target_text")),
        target_role=_optional_text(raw.get("target_role")),
        target_href=_optional_text(raw.get("target_href")),
        query=_optional_text(raw.get("query")),
        option_label=_optional_text(raw.get("option_label")),
        value=_optional_text(raw.get("value")),
        wait_seconds=None if wait_seconds in (None, "") else float(wait_seconds),
        reasoning_summary=str(raw.get("reasoning_summary", "")).strip(),
        blocker_reason=blocker_reason,
    )


def _optional_text(value: object) -> str | None:
    if value in (None, ""):
        return None
    text = str(value).strip()
    return text or None


def _coerce_action_for_context(
    *,
    action: BrowserAgentAction,
    context: BrowserAgentContext,
    observation: BrowserObservation,
) -> BrowserAgentAction:
    if _should_force_search_from_cart_browse(action=action, observation=observation):
        return BrowserAgentAction(
            action_type=BrowserAgentActionType.SEARCH,
            query=context.search_query,
            reasoning_summary=(
                "Start a fresh search from cart/browse context to avoid reusing existing cart contents "
                "as the active shopping target."
            ),
        )
    return action


def _should_force_search_from_cart_browse(
    *,
    action: BrowserAgentAction,
    observation: BrowserObservation,
) -> bool:
    if observation.blocker_hint:
        return False
    if observation.page_kind != "browse":
        return False
    lowered_url = observation.url.lower()
    if "cart.coupang.com/cartview.pang" not in lowered_url and "/cartview.pang" not in lowered_url:
        return False
    return action.action_type in (
        BrowserAgentActionType.STOP,
        BrowserAgentActionType.WAIT,
        BrowserAgentActionType.CLICK,
    )


def _classify_observation(observation: BrowserObservation) -> CartAddFailureReason:
    if observation.blocker_hint:
        blocker_reason = _classify_blocker_hint(observation.blocker_hint)
        if blocker_reason is not None:
            return blocker_reason
    if _observation_indicates_out_of_stock(observation):
        return CartAddFailureReason.OUT_OF_STOCK
    lowered = observation.body_text_excerpt.lower()
    if any(token in lowered for token in ("품절", "일시품절", "재입고 알림")):
        return CartAddFailureReason.OUT_OF_STOCK
    if observation.available_options:
        return CartAddFailureReason.AMBIGUITY
    if observation.add_to_cart_visible:
        return CartAddFailureReason.UNKNOWN
    return CartAddFailureReason.UI_ELEMENT_NOT_FOUND


def _classify_blocker_hint(blocker_hint: str) -> CartAddFailureReason | None:
    lowered = blocker_hint.lower()
    if "access denied" in lowered:
        return CartAddFailureReason.ACCESS_DENIED
    if any(token in lowered for token in ("security", "captcha", "보안", "로봇")):
        return CartAddFailureReason.SECURITY_CHALLENGE
    if "login" in lowered or "로그인" in lowered:
        return CartAddFailureReason.LOGIN_REQUIRED
    if "품절" in blocker_hint:
        return CartAddFailureReason.OUT_OF_STOCK
    return None


def _observation_indicates_out_of_stock(observation: BrowserObservation) -> bool:
    if observation.selected_product_hint.get("sold_out"):
        return True
    if observation.page_kind == "search_results":
        return False
    if any(product.sold_out for product in observation.observed_products):
        available_products = [product for product in observation.observed_products if not product.sold_out]
        if not available_products:
            return True
    lowered = observation.body_text_excerpt.lower()
    return any(token in lowered for token in ("품절", "일시품절", "재입고 알림"))


def _stage_from_observation(observation: BrowserObservation) -> CartAddStage:
    if observation.page_kind == "session_blocked":
        return CartAddStage.SESSION
    if observation.available_options:
        return CartAddStage.OPTION_SELECTION
    if observation.add_to_cart_visible:
        return CartAddStage.ADD_TO_CART
    return CartAddStage.PRODUCT_PAGE


def _selection_from_observation(
    *,
    item: RequestedItem,
    observation: BrowserObservation,
    reasoning_summary: str,
    option_hints: dict[str, str],
    target_text: str | None = None,
    target_href: str | None = None,
) -> SelectedProduct:
    product = _pick_observed_product(
        observation=observation,
        target_text=target_text,
        target_href=target_href,
        fallback_name=item.name,
    )
    candidate = ProductCandidate(
        product_id=_product_id_from_href(product.href, fallback_name=product.name),
        name=product.name,
        price_krw=_price_from_text(product.price_text),
        rating=_rating_from_text(product.rating_text),
        review_count=_review_count_from_text(product.review_count_text),
        product_url=product.href or observation.url,
        vendor="Coupang",
        badges=list(product.badges),
    )
    return SelectedProduct(
        request_item_name=item.name,
        candidate=candidate,
        quantity=item.quantity,
        selection_reason=reasoning_summary or "Selected from live browser observation.",
        score=0.0,
        option_hints=dict(option_hints),
    )


def _pick_observed_product(
    *,
    observation: BrowserObservation,
    target_text: str | None,
    target_href: str | None,
    fallback_name: str,
) -> ObservedProduct:
    selected_hint = observation.selected_product_hint
    if selected_hint:
        return ObservedProduct(
            name=str(selected_hint.get("name") or fallback_name),
            href=_optional_text(selected_hint.get("href")) or observation.url,
            price_text=_optional_text(selected_hint.get("price_text")),
            rating_text=_optional_text(selected_hint.get("rating_text")),
            review_count_text=_optional_text(selected_hint.get("review_count_text")),
            badges=[str(item) for item in selected_hint.get("badges", [])],
            sold_out=bool(selected_hint.get("sold_out", False)),
        )
    for product in observation.observed_products:
        if target_href and product.href and target_href in product.href:
            return product
        if target_text and target_text in product.name:
            return product
    if observation.observed_products:
        return observation.observed_products[0]
    return ObservedProduct(name=fallback_name, href=observation.url)


def _rank_observed_products(
    products: list[ObservedProduct],
    *,
    preferred_terms: list[str] | None = None,
) -> list[ObservedProduct]:
    return sorted(
        products,
        key=lambda product: (
            0 if product.sold_out else 1,
            0 if _is_ad_product(product) else 1,
            _text_match_score(product, preferred_terms or []),
            _rating_from_text(product.rating_text),
            _review_count_from_text(product.review_count_text),
            -_price_from_text(product.price_text),
        ),
        reverse=True,
    )


def _is_ad_product(product: ObservedProduct) -> bool:
    lowered_name = product.name.lower()
    lowered_href = (product.href or "").lower()
    return " ad" in lowered_name or lowered_name.endswith("ad") or "sourcetype=srp_product_ads" in lowered_href


def _text_match_score(product: ObservedProduct, preferred_terms: list[str]) -> int:
    lowered_name = product.name.lower()
    score = 0
    for term in preferred_terms:
        normalized = term.lower().strip()
        if not normalized:
            continue
        if normalized in lowered_name:
            score += 100
        tokens = [token for token in re.split(r"[^0-9a-z가-힣]+", normalized) if token]
        score += sum(1 for token in tokens if token in lowered_name)
    return score


def _match_option(item: RequestedItem, available_options: list[str]) -> str | None:
    quantity_patterns = (
        f"{item.quantity}개",
        f"{item.quantity} 개",
        f"{item.quantity}입",
        f"{item.quantity} 입",
    )
    for option in available_options:
        lowered = option.lower()
        if any(pattern in lowered for pattern in quantity_patterns):
            return option

    lowered_constraints = [constraint.lower() for constraint in item.constraints]
    for option in available_options:
        lowered = option.lower()
        if any(constraint in lowered for constraint in lowered_constraints):
            return option
    return available_options[0] if len(available_options) == 1 else None


def _coerce_search_query(item: RequestedItem, raw_query: str | None) -> str:
    item_name = item.name.strip()
    candidate = (raw_query or "").strip()
    if not candidate:
        return item_name
    normalized_item = re.sub(r"\s+", " ", item_name.lower())
    normalized_candidate = re.sub(r"\s+", " ", candidate.lower())
    if normalized_item and normalized_item not in normalized_candidate:
        return item_name
    return candidate


def _price_from_text(value: str | None) -> int:
    if not value:
        return 0
    digits = "".join(character for character in value if character.isdigit())
    return int(digits) if digits else 0


def _rating_from_text(value: str | None) -> float:
    if not value:
        return 0.0
    match = re.search(r"(\d+(?:\.\d+)?)", value)
    return float(match.group(1)) if match else 0.0


def _review_count_from_text(value: str | None) -> int:
    if not value:
        return 0
    digits = "".join(character for character in value if character.isdigit())
    return int(digits) if digits else 0


def _product_id_from_href(href: str | None, *, fallback_name: str) -> str:
    if href:
        match = re.search(r"/products/([^/?#]+)", href)
        if match:
            return match.group(1)
    slug = re.sub(r"[^a-z0-9]+", "-", fallback_name.lower()).strip("-")
    return slug or "observed-product"


def _cart_item_id_from_selection(selection: SelectedProduct) -> str:
    return f"{selection.candidate.product_id}:cart-add"


def _failure_result(
    *,
    selection: SelectedProduct,
    stage: CartAddStage,
    failure_reason: CartAddFailureReason,
    message: str,
    session_mode: str,
    observation: BrowserObservation,
    cart_count_before: int | None = None,
    cart_count_after: int | None = None,
    checkout_attempted: bool = False,
    selected_options: dict[str, str] | None = None,
) -> CartAddResult:
    return CartAddResult(
        success=False,
        cart_item_id=None,
        selected_product=selection,
        stage=stage,
        message=message,
        failure_reason=failure_reason,
        cart_count_before=cart_count_before,
        cart_count_after=cart_count_after,
        checkout_attempted=checkout_attempted,
        evidence={
            "session_mode": session_mode,
            "selected_options": selected_options or {},
            "last_observation": asdict(observation),
        },
    )


def _read_message_content(response: dict[str, object]) -> str:
    choices = response.get("choices", [])
    if not isinstance(choices, list) or not choices:
        raise ValueError("Azure OpenAI response did not include choices")
    first_choice = choices[0]
    if not isinstance(first_choice, dict):
        raise ValueError("Azure OpenAI response choice was not an object")
    message = first_choice.get("message", {})
    if not isinstance(message, dict):
        raise ValueError("Azure OpenAI response message was not an object")
    content = message.get("content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        text_parts: list[str] = []
        for part in content:
            if not isinstance(part, dict):
                continue
            if part.get("type") == "text":
                text = part.get("text", "")
                if isinstance(text, str):
                    text_parts.append(text)
        if text_parts:
            return "".join(text_parts)
    raise ValueError("Azure OpenAI response message content was empty")


def encode_screenshot_bytes(raw: bytes | None) -> str | None:
    if not raw:
        return None
    return base64.b64encode(raw).decode("ascii")
