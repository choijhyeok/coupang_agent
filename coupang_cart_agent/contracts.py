from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import StrEnum


class CartAddStage(StrEnum):
    """Execution stage reached by the cart automation."""

    SESSION = "session"
    PRODUCT_PAGE = "product_page"
    OPTION_SELECTION = "option_selection"
    ADD_TO_CART = "add_to_cart"


class CartAddFailureReason(StrEnum):
    """Classified failure reasons required by the cart automation module."""

    LOGIN_FAILED = "login_failed"
    OUT_OF_STOCK = "out_of_stock"
    OPTION_MISMATCH = "option_mismatch"
    UI_ELEMENT_NOT_FOUND = "ui_element_not_found"
    CHECKOUT_ATTEMPTED = "checkout_attempted"
    UNKNOWN = "unknown"


@dataclass(slots=True)
class RequestedItem:
    """A single product request parsed from a Telegram message."""

    name: str
    quantity: int = 1
    constraints: list[str] = field(default_factory=list)
    max_price_krw: int | None = None


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
class ProductCandidate:
    """Candidate product scored by the selection engine."""

    product_id: str
    name: str
    price_krw: int
    rating: float
    review_count: int
    product_url: str
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


@dataclass(slots=True)
class NotificationPayload:
    """Message content emitted to the notification layer."""

    chat_id: str
    success: bool
    stage: str
    summary: str
    details: dict[str, object] = field(default_factory=dict)


def demo_contract_payload() -> dict[str, object]:
    requested_item = RequestedItem(
        name="Coke Zero 355ml",
        quantity=2,
        constraints=["zero sugar", "can"],
        max_price_krw=18000,
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
            "session_mode": "existing_session",
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
