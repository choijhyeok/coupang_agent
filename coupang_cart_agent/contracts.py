from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import StrEnum


class CartAddStage(StrEnum):
    """Execution stage reached by the cart automation."""

    SESSION = "session"
    PRODUCT_PAGE = "product_page"
    OPTION_SELECTION = "option_selection"
    ADD_TO_CART = "add_to_cart"
    VERIFICATION = "verification"


class CartAddFailureReason(StrEnum):
    """Classified failure reasons required by the cart automation module."""

    LOGIN_FAILED = "login_failed"
    LOGIN_REQUIRED = "login_required"
    SECURITY_CHALLENGE = "security_challenge"
    ACCESS_DENIED = "access_denied"
    OUT_OF_STOCK = "out_of_stock"
    OPTION_MISMATCH = "option_mismatch"
    AMBIGUITY = "ambiguity"
    UI_ELEMENT_NOT_FOUND = "ui_element_not_found"
    PURCHASE_RESTRICTED = "purchase_restricted"
    CHECKOUT_ATTEMPTED = "checkout_attempted"
    VERIFICATION_MISMATCH = "verification_mismatch"
    MANUAL_REVIEW_REQUIRED = "manual_review_required"
    UNKNOWN = "unknown"


@dataclass(slots=True)
class RequestedItem:
    """A single product request parsed from a Telegram message."""

    name: str
    quantity: int = 1
    constraints: list[str] = field(default_factory=list)
    max_price_krw: int | None = None
    explicit_brand: str | None = None
    explicit_unit_size: str | None = None
    explicit_pack_count: int | None = None
    explicit_pack_unit: str | None = None


class IntakeMode(StrEnum):
    """Operational mode for request intake."""

    DEMO = "demo"
    LIVE = "live"


@dataclass(slots=True)
class ShoppingRequest:
    """Normalized shopping request passed into the selection pipeline."""

    user_id: str
    chat_id: str
    items: list[RequestedItem]
    raw_text: str
    request_id: str = "sample-request"
    received_at: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass(slots=True)
class RequestSession:
    """Stable session identity linked to persisted inbound requests."""

    session_id: str
    channel: str
    user_id: str
    chat_id: str
    created_at: datetime
    last_message_at: datetime


@dataclass(slots=True)
class ShoppingRequestEnvelope:
    """Envelope passed into workflow state for production intake."""

    source: str
    mode: IntakeMode
    request: ShoppingRequest
    session: RequestSession
    inbound_message_id: str
    update_id: int | None = None
    message_id: int | None = None
    raw_text: str = ""
    raw_update: dict[str, object] = field(default_factory=dict)
    metadata: dict[str, object] = field(default_factory=dict)

    def as_langgraph_state(self) -> dict[str, object]:
        return {
            "request": asdict(self.request),
            "request_envelope": asdict(self),
        }


@dataclass(slots=True)
class ProductCandidate:
    """Candidate product scored by the selection engine."""

    product_id: str
    name: str
    price_krw: int
    rating: float
    review_count: int
    product_url: str
    image_url: str | None = None
    vendor: str | None = None
    badges: list[str] = field(default_factory=list)


@dataclass(slots=True)
class SelectedProduct:
    """Final product chosen for cart insertion."""

    request_item_name: str
    candidate: ProductCandidate
    quantity: int
    selection_reason: str
    score: float
    option_hints: dict[str, str] = field(default_factory=dict)


@dataclass(slots=True)
class PriorPurchaseRecord:
    """Historical purchase signal loaded before ranking candidates."""

    product_id: str
    product_name: str
    purchase_count: int = 1
    last_purchased_at: datetime | None = None
    satisfaction_rating: float | None = None


@dataclass(slots=True)
class SessionSelectionSignal:
    """Recent session signal that can steer selection for one request."""

    product_id: str
    signal: str
    noted_at: datetime | None = None


@dataclass(slots=True)
class SelectionContext:
    """User and session history used to adjust selection scores."""

    prior_purchases: list[PriorPurchaseRecord] = field(default_factory=list)
    recent_session_signals: list[SessionSelectionSignal] = field(default_factory=list)


@dataclass(slots=True)
class CartAddResult:
    """Result from the Coupang cart automation stage."""

    success: bool
    cart_item_id: str | None
    selected_product: SelectedProduct
    stage: CartAddStage
    message: str
    failure_reason: CartAddFailureReason | None = None
    cart_count_before: int | None = None
    cart_count_after: int | None = None
    checkout_attempted: bool = False
    evidence: dict[str, object] = field(default_factory=dict)


class BrowserAgentActionType(StrEnum):
    """Constrained action space emitted by the browser agent model."""

    SEARCH = "search"
    CLICK = "click"
    SELECT_OPTION = "select_option"
    ADD_TO_CART = "add_to_cart"
    SCROLL = "scroll"
    GO_BACK = "go_back"
    WAIT = "wait"
    STOP = "stop"


@dataclass(slots=True)
class ObservedProduct:
    """Structured product clue extracted from the current browser view."""

    name: str
    href: str | None = None
    price_text: str | None = None
    rating_text: str | None = None
    review_count_text: str | None = None
    badges: list[str] = field(default_factory=list)
    sold_out: bool = False


@dataclass(slots=True)
class ObservedCartItem:
    """Structured cart item clue extracted from a cart or mini-cart view."""

    name: str
    quantity: int | None = None
    quantity_text: str | None = None
    option_summary: str | None = None
    package_summary: str | None = None
    price_text: str | None = None
    badges: list[str] = field(default_factory=list)


@dataclass(slots=True)
class BrowserObservation:
    """Current browser state exposed to the AOAI decision engine."""

    step_index: int
    url: str
    title: str
    page_kind: str
    body_text_excerpt: str
    accessibility_lines: list[str] = field(default_factory=list)
    html_excerpt: str | None = None
    screenshot_path: str | None = None
    screenshot_base64: str | None = None
    interactive_elements: list[str] = field(default_factory=list)
    observed_products: list[ObservedProduct] = field(default_factory=list)
    cart_items: list[ObservedCartItem] = field(default_factory=list)
    selected_product_hint: dict[str, object] = field(default_factory=dict)
    available_options: list[str] = field(default_factory=list)
    add_to_cart_visible: bool = False
    add_to_cart_available: bool = False
    add_to_cart_in_viewport: bool = False
    sticky_add_to_cart_visible: bool = False
    expandable_sections: list[str] = field(default_factory=list)
    purchase_blocked_reason: str | None = None
    blocker_hint: str | None = None
    cart_count: int | None = None
    last_action_summary: str | None = None
    observation_engine: str = "playwright"


@dataclass(slots=True)
class BrowserAgentAction:
    """Model-decided action serialized as strict JSON."""

    action_type: BrowserAgentActionType
    target_text: str | None = None
    target_role: str | None = None
    target_href: str | None = None
    query: str | None = None
    option_label: str | None = None
    value: str | None = None
    scroll_amount: int | None = None
    wait_seconds: float | None = None
    reasoning_summary: str = ""
    blocker_reason: CartAddFailureReason | None = None


@dataclass(slots=True)
class BrowserAgentStep:
    """One observation -> action -> execution cycle."""

    step_index: int
    item_name: str
    observation: BrowserObservation
    action: BrowserAgentAction
    execution_summary: str


@dataclass(slots=True)
class BrowserAgentRun:
    """Complete per-request live browser-agent execution trace."""

    selections: list[SelectedProduct] = field(default_factory=list)
    cart_results: list[CartAddResult] = field(default_factory=list)
    reasoning_summary: str = ""
    last_observation: BrowserObservation | None = None
    steps: list[BrowserAgentStep] = field(default_factory=list)
    performance: dict[str, object] = field(default_factory=dict)


@dataclass(slots=True)
class NotificationPayload:
    """Message content emitted to the notification layer."""

    chat_id: str
    success: bool
    stage: str
    summary: str
    kind: str = "result"
    details: dict[str, object] = field(default_factory=dict)


def demo_contract_payload() -> dict[str, object]:
    requested_item = RequestedItem(
        name="Coke Zero 355ml",
        quantity=2,
        constraints=["zero sugar", "can"],
        max_price_krw=18000,
        explicit_brand="Coke",
        explicit_unit_size="355ml",
    )
    request = ShoppingRequest(
        user_id="telegram:demo-user",
        chat_id="demo-chat",
        items=[requested_item],
        raw_text="콜라 제로 355ml 2개 담아줘",
        request_id="demo-request-001",
    )
    candidate = ProductCandidate(
        product_id="CP-1001",
        name="Coca-Cola Zero 355ml x 24",
        price_krw=16900,
        rating=4.8,
        review_count=12431,
        product_url="https://www.coupang.com/vp/products/CP-1001",
        image_url="https://images.coupangcdn.com/image/demo/coke-zero.jpg",
        vendor="Coupang",
        badges=["Rocket Delivery", "Best Seller"],
    )
    selected = SelectedProduct(
        request_item_name=requested_item.name,
        candidate=candidate,
        quantity=requested_item.quantity,
        selection_reason="Balanced strong rating, large review volume, and in-budget price.",
        score=9.3,
    )
    cart_result = CartAddResult(
        success=True,
        cart_item_id="cart-item-42",
        selected_product=selected,
        stage=CartAddStage.ADD_TO_CART,
        message="Item added to cart.",
        cart_count_before=1,
        cart_count_after=2,
        evidence={
            "session_mode": "attached_browser_session",
            "product_url": candidate.product_url,
        },
    )
    notification = NotificationPayload(
        chat_id=request.chat_id,
        success=True,
        stage="cart_add",
        summary="장바구니 담기가 완료되었습니다.",
        details={
            "request": asdict(request),
            "selected_product": asdict(selected),
            "cart_result": asdict(cart_result),
        },
    )
    return {
        "shopping_request": asdict(request),
        "selected_product": asdict(selected),
        "cart_add_result": asdict(cart_result),
        "notification_payload": asdict(notification),
    }


_SIZE_TOKEN_PATTERN = re.compile(
    r"(?P<value>\d+(?:\.\d+)?)\s*(?P<unit>ml|l|kg|g|L|ML|KG|G)\b"
)


def canonicalize_size_token(value: str | None) -> str | None:
    if value is None:
        return None
    match = _SIZE_TOKEN_PATTERN.search(value)
    if match is None:
        normalized = re.sub(r"\s+", "", value).lower()
        return normalized or None
    return f"{match.group('value')}{match.group('unit').lower()}"


def build_requested_item_search_query(item: RequestedItem) -> str:
    fragments = [item.name]
    if item.explicit_pack_count is not None and item.explicit_pack_unit:
        pack_fragment = f"{item.explicit_pack_count}{item.explicit_pack_unit}"
        if re.sub(r"\s+", "", pack_fragment).lower() not in re.sub(r"\s+", "", item.name).lower():
            fragments.append(pack_fragment)
    fragments.extend(item.constraints)
    return " ".join(fragment for fragment in fragments if fragment).strip()
