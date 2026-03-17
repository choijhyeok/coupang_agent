from __future__ import annotations

import sqlite3
from dataclasses import asdict
from datetime import UTC, datetime
from time import sleep
from typing import Any, Callable, Protocol

from .contracts import CartAddResult, NotificationPayload, PriorPurchaseRecord
from .telegram_intake import TelegramBotApiClient

MAX_NOTIFICATION_LENGTH = 500
MAX_SUCCESS_ITEMS = 3


class NotificationDeliveryError(RuntimeError):
    """Raised when notification delivery exhausts configured retry attempts."""


class NotificationTextSender(Protocol):
    """Transport seam for sending a formatted notification string."""

    def send_message(self, *, chat_id: str, text: str) -> object: ...

    def send_photo(
        self,
        *,
        chat_id: str,
        photo: str,
        caption: str | None = None,
    ) -> object: ...


class NotificationFormatter:
    """Bounded formatter that renders a user-facing Telegram message from a payload."""

    def __init__(self, *, max_length: int = MAX_NOTIFICATION_LENGTH) -> None:
        self._max_length = max_length

    def format(self, payload: NotificationPayload) -> str:
        if payload.kind == "proposal":
            message = _format_proposal_message(payload)
        elif payload.kind == "cancelled":
            message = _format_cancelled_message(payload)
        else:
            message = (
                _format_success_message(payload, max_length=self._max_length)
                if payload.success
                else _format_failure_message(payload)
            )
        return truncate_message(message, limit=self._max_length)


class TelegramSendMessageSender:
    """Adapter that delivers messages through Telegram Bot API sendMessage."""

    def __init__(self, *, client: TelegramBotApiClient) -> None:
        self._client = client

    def send_message(self, *, chat_id: str, text: str) -> dict[str, Any]:
        return self._client.send_message(chat_id=chat_id, text=text)

    def send_photo(
        self,
        *,
        chat_id: str,
        photo: str,
        caption: str | None = None,
    ) -> dict[str, Any]:
        return self._client.send_photo(chat_id=chat_id, photo=photo, caption=caption)


class SQLiteNotificationContextStore:
    """Read current cart snapshot and prior purchase context from SQLite."""

    def __init__(self, *, database_path: str) -> None:
        self._database_path = database_path

    def load(self, *, user_id: str) -> dict[str, object]:
        with sqlite3.connect(self._database_path) as connection:
            connection.row_factory = sqlite3.Row
            cart_snapshot_items = self._load_cart_snapshot_items(connection, user_id=user_id)
            prior_purchases = self._load_prior_purchases(connection, user_id=user_id)
        return {
            "cart_snapshot_items": cart_snapshot_items,
            "prior_purchases": prior_purchases,
        }

    @staticmethod
    def _load_cart_snapshot_items(
        connection: sqlite3.Connection,
        *,
        user_id: str,
    ) -> list[dict[str, object]]:
        rows = connection.execute(
            """
            SELECT product_id, product_name, quantity, unit_price_krw, total_price_krw, snapshot_at
            FROM current_cart_snapshot_items
            WHERE user_id = ?
              AND snapshot_at = (
                SELECT MAX(snapshot_at)
                FROM current_cart_snapshot_items
                WHERE user_id = ?
              )
            ORDER BY product_name ASC, product_id ASC
            """,
            (user_id, user_id),
        ).fetchall()
        return [
            {
                "product_id": str(row["product_id"]),
                "name": str(row["product_name"]),
                "quantity": max(1, int(row["quantity"])),
                "price_krw": int(row["unit_price_krw"]),
                "line_total_krw": int(
                    row["total_price_krw"]
                    if row["total_price_krw"] is not None
                    else int(row["unit_price_krw"]) * max(1, int(row["quantity"]))
                ),
                "snapshot_at": str(row["snapshot_at"]),
            }
            for row in rows
        ]

    @staticmethod
    def _load_prior_purchases(
        connection: sqlite3.Connection,
        *,
        user_id: str,
    ) -> list[PriorPurchaseRecord]:
        rows = connection.execute(
            """
            SELECT product_id, product_name, purchase_count, last_purchased_at, satisfaction_rating
            FROM prior_purchases
            WHERE user_id = ?
            ORDER BY COALESCE(last_purchased_at, '') DESC, purchase_count DESC, product_id ASC
            LIMIT 5
            """,
            (user_id,),
        ).fetchall()
        return [
            PriorPurchaseRecord(
                product_id=str(row["product_id"]),
                product_name=str(row["product_name"]),
                purchase_count=max(1, int(row["purchase_count"])),
                last_purchased_at=_parse_timestamp(row["last_purchased_at"]),
                satisfaction_rating=(
                    None if row["satisfaction_rating"] is None else float(row["satisfaction_rating"])
                ),
            )
            for row in rows
        ]


def build_success_notification_payload(
    *,
    chat_id: str,
    cart_results: list[CartAddResult],
    cart_snapshot_items: list[dict[str, object]] | None = None,
    prior_purchases: list[PriorPurchaseRecord] | None = None,
) -> NotificationPayload:
    if not cart_results:
        raise ValueError("cart_results must not be empty")

    products = (
        normalize_snapshot_items(cart_snapshot_items)
        if cart_snapshot_items
        else [serialize_cart_result(result) for result in cart_results]
    )
    summary = summarize_cart_results(cart_results, cart_snapshot_items=products)
    return NotificationPayload(
        chat_id=chat_id,
        success=True,
        stage=str(cart_results[-1].stage),
        summary=summary,
        details={
            "products": products,
            "cart_item_count": len(products),
            "prior_purchases": [serialize_prior_purchase(record) for record in prior_purchases or []],
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
        kind="result",
        details=details,
    )


def build_proposal_notification_payload(
    *,
    chat_id: str,
    summary: str,
    candidate: dict[str, object],
    alternatives: list[dict[str, object]] | None = None,
    image_url: str | None = None,
) -> NotificationPayload:
    details: dict[str, object] = {
        "candidate": dict(candidate),
        "alternatives": list(alternatives or []),
    }
    if image_url:
        details["photo"] = {
            "url": image_url,
            "caption": _build_proposal_caption(summary=summary, candidate=candidate),
        }
    return NotificationPayload(
        chat_id=chat_id,
        success=True,
        stage="proposal_pending",
        summary=summary,
        kind="proposal",
        details=details,
    )


def build_cancelled_notification_payload(
    *,
    chat_id: str,
    summary: str,
) -> NotificationPayload:
    return NotificationPayload(
        chat_id=chat_id,
        success=True,
        stage="cancelled",
        summary=summary,
        kind="cancelled",
    )


def format_notification_message(
    payload: NotificationPayload,
    *,
    max_length: int = MAX_NOTIFICATION_LENGTH,
) -> str:
    return NotificationFormatter(max_length=max_length).format(payload)


class RetryingNotificationService:
    """Send formatted notifications with bounded retries for transient failures."""

    def __init__(
        self,
        *,
        sender: NotificationTextSender | Callable[[str, str], object | None],
        formatter: NotificationFormatter | None = None,
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
        self._formatter = formatter or NotificationFormatter()
        self._max_attempts = max_attempts
        self._retryable_exceptions = retryable_exceptions
        self._sleep_seconds = sleep_seconds
        self._sleep_func = sleep_func

    def send(self, payload: NotificationPayload) -> None:
        message = self._formatter.format(payload)
        attempt = 1
        while True:
            try:
                self._dispatch(payload, message)
                return
            except self._retryable_exceptions as exc:
                if attempt >= self._max_attempts:
                    raise NotificationDeliveryError(
                        f"failed to deliver notification after {attempt} attempts"
                    ) from exc
                attempt += 1
                if self._sleep_seconds > 0:
                    self._sleep_func(self._sleep_seconds)

    def _dispatch(self, payload: NotificationPayload, message: str) -> object | None:
        photo_payload = payload.details.get("photo")
        if hasattr(self._sender, "send_photo") and isinstance(photo_payload, dict):
            photo_url = str(photo_payload.get("url", "")).strip()
            caption = photo_payload.get("caption")
            if photo_url:
                self._sender.send_photo(
                    chat_id=payload.chat_id,
                    photo=photo_url,
                    caption=None if caption in (None, "") else str(caption),
                )
        if hasattr(self._sender, "send_message"):
            return self._sender.send_message(chat_id=payload.chat_id, text=message)
        return self._sender(payload.chat_id, message)


def summarize_cart_results(
    cart_results: list[CartAddResult],
    *,
    cart_snapshot_items: list[dict[str, object]] | None = None,
) -> str:
    normalized_items = normalize_snapshot_items(cart_snapshot_items)
    if normalized_items:
        total_items = len(normalized_items)
        total_quantity = sum(int(item["quantity"]) for item in normalized_items)
        total_price = sum(int(item["line_total_krw"]) for item in normalized_items)
    else:
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
        "product_id": candidate.product_id,
        "name": candidate.name,
        "price_krw": candidate.price_krw,
        "quantity": selected.quantity,
        "line_total_krw": candidate.price_krw * selected.quantity,
        "selection_reason": selected.selection_reason,
        "cart_result": asdict(result),
    }


def _format_success_message(payload: NotificationPayload, *, max_length: int) -> str:
    products = payload.details.get("products", [])
    normalized_products = products if isinstance(products, list) else []
    prior_purchases = payload.details.get("prior_purchases", [])
    normalized_prior_purchases = prior_purchases if isinstance(prior_purchases, list) else []

    for name_limit in (40, 28, 20, 14, 10):
        for item_limit in (MAX_SUCCESS_ITEMS, 2, 1, 0):
            product_lines = _build_product_lines(
                normalized_products,
                name_limit=name_limit,
                item_limit=item_limit,
            )
            context_lines = _build_prior_purchase_lines(
                normalized_products,
                normalized_prior_purchases,
                item_limit=2,
                name_limit=name_limit,
            )
            lines = ["장바구니 담기를 완료했습니다."]
            lines.extend(product_lines)
            lines.extend(context_lines)
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


def _build_prior_purchase_lines(
    products: list[object],
    prior_purchases: list[object],
    *,
    item_limit: int,
    name_limit: int,
) -> list[str]:
    product_ids = {
        str(product.get("product_id"))
        for product in products
        if isinstance(product, dict) and product.get("product_id")
    }
    matched_lines: list[str] = []
    for purchase in prior_purchases:
        if not isinstance(purchase, dict):
            continue
        product_id = str(purchase.get("product_id", ""))
        if product_id and product_id not in product_ids:
            continue
        name = truncate_text(str(purchase.get("product_name", "이전 구매 상품")), limit=name_limit)
        count = max(1, int(purchase.get("purchase_count", 1)))
        matched_lines.append(f"재구매 참고: {name} / 이전 구매 {count}회")
        if len(matched_lines) >= item_limit:
            break
    return matched_lines


def _format_proposal_message(payload: NotificationPayload) -> str:
    candidate = payload.details.get("candidate", {})
    if not isinstance(candidate, dict):
        candidate = {}
    lines = [
        "추천 상품을 찾았습니다.",
        f"상품: {truncate_text(str(candidate.get('name', '상품 정보 없음')), limit=80)}",
    ]
    option_summary = truncate_text(str(candidate.get("option_summary", "")).strip(), limit=40)
    if option_summary:
        lines.append(f"옵션: {option_summary}")
    lines.append(f"가격: {format_price(int(candidate.get('price_krw', 0)))}원")
    reason = truncate_text(str(candidate.get("selection_reason", payload.summary)), limit=180)
    lines.append(f"추천 이유: {reason}")
    lines.append("이대로 진행하려면 `ㅇㅇ 담아줘`, 다른 추천은 `다른 거 보여줘`, 취소는 `취소`라고 답해주세요.")
    return "\n".join(lines)


def _format_cancelled_message(payload: NotificationPayload) -> str:
    return payload.summary


def _build_proposal_caption(*, summary: str, candidate: dict[str, object]) -> str:
    parts = [truncate_text(str(candidate.get("name", "상품 정보 없음")), limit=80)]
    option_summary = truncate_text(str(candidate.get("option_summary", "")).strip(), limit=32)
    if option_summary:
        parts.append(option_summary)
    parts.append(f"{format_price(int(candidate.get('price_krw', 0)))}원")
    parts.append(truncate_text(summary, limit=120))
    return "\n".join(part for part in parts if part)


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


def normalize_snapshot_items(items: list[dict[str, object]] | None) -> list[dict[str, object]]:
    normalized_items: list[dict[str, object]] = []
    for item in items or []:
        quantity = max(1, int(item.get("quantity", 1)))
        price_krw = int(item.get("price_krw", item.get("unit_price_krw", 0)))
        line_total = int(item.get("line_total_krw", price_krw * quantity))
        normalized_items.append(
            {
                "product_id": str(item.get("product_id", "")),
                "name": str(item.get("name", item.get("product_name", "상품 정보 없음"))),
                "price_krw": price_krw,
                "quantity": quantity,
                "line_total_krw": line_total,
                "snapshot_at": item.get("snapshot_at"),
            }
        )
    return normalized_items


def serialize_prior_purchase(record: PriorPurchaseRecord) -> dict[str, object]:
    return {
        "product_id": record.product_id,
        "product_name": record.product_name,
        "purchase_count": record.purchase_count,
        "last_purchased_at": (
            None if record.last_purchased_at is None else record.last_purchased_at.isoformat()
        ),
        "satisfaction_rating": record.satisfaction_rating,
    }


def _parse_timestamp(value: object) -> datetime | None:
    if value in (None, ""):
        return None
    normalized = str(value).replace("Z", "+00:00")
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed
