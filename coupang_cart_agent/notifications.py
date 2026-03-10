from __future__ import annotations

from dataclasses import asdict
from time import sleep
from typing import Callable

from .contracts import CartAddResult, NotificationPayload

MAX_NOTIFICATION_LENGTH = 500
MAX_SUCCESS_ITEMS = 3


class NotificationDeliveryError(RuntimeError):
    """Raised when notification delivery exhausts configured retry attempts."""


def build_success_notification_payload(
    *,
    chat_id: str,
    cart_results: list[CartAddResult],
) -> NotificationPayload:
    if not cart_results:
        raise ValueError("cart_results must not be empty")

    summary = summarize_cart_results(cart_results)
    products = [serialize_cart_result(result) for result in cart_results]
    return NotificationPayload(
        chat_id=chat_id,
        success=True,
        stage=str(cart_results[-1].stage),
        summary=summary,
        details={
            "products": products,
            "cart_item_count": len(products),
        },
    )


def build_failure_notification_payload(
    *,
    chat_id: str,
    stage: str,
    reason: str,
    detail: str | None = None,
) -> NotificationPayload:
    summary = truncate_text(reason.strip(), limit=120)
    details: dict[str, object] = {
        "failure_reason": reason.strip(),
    }
    if detail:
        details["failure_detail"] = detail.strip()

    return NotificationPayload(
        chat_id=chat_id,
        success=False,
        stage=stage,
        summary=summary,
        details=details,
    )


def format_notification_message(
    payload: NotificationPayload,
    *,
    max_length: int = MAX_NOTIFICATION_LENGTH,
) -> str:
    message = (
        _format_success_message(payload, max_length=max_length)
        if payload.success
        else _format_failure_message(payload)
    )
    return truncate_message(message, limit=max_length)


class RetryingNotificationService:
    """Send formatted notifications with bounded retries for transient failures."""

    def __init__(
        self,
        *,
        sender: Callable[[str, str], None],
        max_attempts: int = 3,
        retryable_exceptions: tuple[type[BaseException], ...] = (
            TimeoutError,
            ConnectionError,
        ),
        sleep_seconds: float = 0.0,
        sleep_func: Callable[[float], None] = sleep,
    ) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        self._sender = sender
        self._max_attempts = max_attempts
        self._retryable_exceptions = retryable_exceptions
        self._sleep_seconds = sleep_seconds
        self._sleep_func = sleep_func

    def send(self, payload: NotificationPayload) -> None:
        message = format_notification_message(payload)
        attempt = 1
        while True:
            try:
                self._sender(payload.chat_id, message)
                return
            except self._retryable_exceptions as exc:
                if attempt >= self._max_attempts:
                    raise NotificationDeliveryError(
                        f"failed to deliver notification after {attempt} attempts"
                    ) from exc
                attempt += 1
                if self._sleep_seconds > 0:
                    self._sleep_func(self._sleep_seconds)


def summarize_cart_results(cart_results: list[CartAddResult]) -> str:
    total_items = len(cart_results)
    total_quantity = sum(result.selected_product.quantity for result in cart_results)
    total_price = sum(
        result.selected_product.candidate.price_krw * result.selected_product.quantity
        for result in cart_results
    )
    return (
        f"총 {total_items}종, {total_quantity}개, "
        f"{format_price(total_price)}원 장바구니 담기 완료"
    )


def serialize_cart_result(result: CartAddResult) -> dict[str, object]:
    selected = result.selected_product
    candidate = selected.candidate
    return {
        "name": candidate.name,
        "price_krw": candidate.price_krw,
        "quantity": selected.quantity,
        "selection_reason": selected.selection_reason,
        "cart_result": asdict(result),
    }


def _format_success_message(payload: NotificationPayload, *, max_length: int) -> str:
    products = payload.details.get("products", [])
    normalized_products = products if isinstance(products, list) else []

    for name_limit in (40, 28, 20, 14, 10):
        for item_limit in (MAX_SUCCESS_ITEMS, 2, 1, 0):
            product_lines = _build_product_lines(
                normalized_products,
                name_limit=name_limit,
                item_limit=item_limit,
            )
            lines = ["장바구니 담기를 완료했습니다."]
            lines.extend(product_lines)
            lines.append(f"요약: {payload.summary}")
            message = "\n".join(lines)
            if len(message) <= max_length:
                return message

    lines = ["장바구니 담기를 완료했습니다.", f"요약: {payload.summary}"]
    return "\n".join(lines)


def _build_product_lines(
    products: list[object],
    *,
    name_limit: int,
    item_limit: int,
) -> list[str]:
    product_lines: list[str] = []
    display_products = products[:item_limit]
    for product in display_products:
        if not isinstance(product, dict):
            continue
        name = truncate_text(str(product.get("name", "상품 정보 없음")), limit=name_limit)
        price = format_price(int(product.get("price_krw", 0)))
        quantity = int(product.get("quantity", 1))
        product_lines.append(f"- {name} / {price}원 / {quantity}개")

    remaining = len(products) - len(display_products)
    if remaining > 0:
        product_lines.append(f"- 외 {remaining}건")
    return product_lines


def _format_failure_message(payload: NotificationPayload) -> str:
    reason = str(payload.details.get("failure_reason", payload.summary))
    detail = payload.details.get("failure_detail")
    lines = [
        "장바구니 담기에 실패했습니다.",
        f"단계: {payload.stage}",
        f"원인: {truncate_text(reason, limit=180)}",
    ]
    if detail:
        lines.append(f"상세: {truncate_text(str(detail), limit=160)}")
    return "\n".join(lines)


def truncate_message(message: str, *, limit: int) -> str:
    if len(message) <= limit:
        return message

    truncated = message[: max(0, limit - 4)].rstrip()
    return f"{truncated}..."


def truncate_text(text: str, *, limit: int) -> str:
    compact = " ".join(text.split())
    if len(compact) <= limit:
        return compact
    return f"{compact[: max(0, limit - 3)].rstrip()}..."


def format_price(value: int) -> str:
    return f"{value:,}"
