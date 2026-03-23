from __future__ import annotations

import html
import re
import sqlite3
from dataclasses import asdict
from datetime import UTC, datetime
from time import sleep
from typing import Any, Callable, Protocol

from .contracts import CartAddResult, CartRemoveResult, NotificationPayload, PriorPurchaseRecord
from .telegram_intake import TelegramBotApiClient

MAX_NOTIFICATION_LENGTH = 500
MAX_SUCCESS_ITEMS = 3


class NotificationDeliveryError(RuntimeError):
    """Raised when notification delivery exhausts configured retry attempts."""


class NotificationTextSender(Protocol):
    """Transport seam for sending a formatted notification string."""

    def send_message(self, *, chat_id: str, text: str, parse_mode: str | None = None) -> object: ...

    def send_photo(
        self,
        *,
        chat_id: str,
        photo: str,
        caption: str | None = None,
        parse_mode: str | None = None,
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
        elif payload.kind == "remove_result":
            message = _format_remove_message(payload)
        elif payload.kind == "price_assessment":
            message = _format_price_assessment_message(payload)
        else:
            message = (
                _format_success_message(payload, max_length=self._max_length)
                if payload.success
                else _format_failure_message(payload)
            )
        return truncate_message(message, limit=self._max_length)

    @staticmethod
    def parse_mode() -> str:
        return "HTML"


class TelegramSendMessageSender:
    """Adapter that delivers messages through Telegram Bot API sendMessage."""

    def __init__(self, *, client: TelegramBotApiClient) -> None:
        self._client = client

    def send_message(self, *, chat_id: str, text: str, parse_mode: str | None = None) -> dict[str, Any]:
        return self._client.send_message(chat_id=chat_id, text=text, parse_mode=parse_mode)

    def send_photo(
        self,
        *,
        chat_id: str,
        photo: str,
        caption: str | None = None,
        parse_mode: str | None = None,
    ) -> dict[str, Any]:
        return self._client.send_photo(chat_id=chat_id, photo=photo, caption=caption, parse_mode=parse_mode)


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

    # Report the items that were just verified by this run. Persisted cart snapshots can
    # legitimately include older items that predate the current request and would produce
    # wrong-item completion messages if they override the fresh cart_results payload.
    products = [serialize_cart_result(result) for result in cart_results]
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
    price_assessments: list[dict[str, object]] | None = None,
) -> NotificationPayload:
    details: dict[str, object] = {
        "candidate": dict(candidate),
        "alternatives": list(alternatives or []),
    }
    if price_assessments:
        details["price_assessments"] = list(price_assessments)
    if image_url:
        details["photo"] = {
            "url": image_url,
            "caption": _build_proposal_caption(
                summary=summary, candidate=candidate,
                price_assessments=price_assessments,
            ),
        }
    return NotificationPayload(
        chat_id=chat_id,
        success=True,
        stage="proposal_pending",
        summary=summary,
        kind="proposal",
        details=details,
    )


def build_remove_notification_payload(
    *,
    chat_id: str,
    remove_results: list[CartRemoveResult],
) -> NotificationPayload:
    if not remove_results:
        raise ValueError("remove_results must not be empty")
    successes = [r for r in remove_results if r.success]
    failures = [r for r in remove_results if not r.success]
    all_success = len(failures) == 0
    if all_success:
        names = ", ".join(r.product_name for r in successes)
        summary = f"장바구니에서 {names}을(를) 제거했습니다."
    else:
        first_fail = failures[0]
        summary = f"장바구니에서 {first_fail.product_name} 제거에 실패했습니다."
        if first_fail.message:
            summary += f" ({first_fail.message})"
    details: dict[str, object] = {
        "removed_products": [asdict(r) for r in successes],
        "failed_products": [asdict(r) for r in failures],
    }
    return NotificationPayload(
        chat_id=chat_id,
        success=all_success,
        stage="cart_remove",
        summary=summary,
        kind="remove_result",
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


def build_price_assessment_notification_payload(
    *,
    chat_id: str,
    assessments: list[dict[str, object]],
) -> NotificationPayload:
    if not assessments:
        raise ValueError("assessments must not be empty")
    verdicts = [str(a.get("verdict", "")) for a in assessments]
    has_buy_now = "buy_now" in verdicts
    has_wait = "wait" in verdicts
    if has_buy_now:
        summary = "가격 분석 결과: 지금 사는 게 이득인 상품이 있습니다!"
    elif has_wait:
        summary = "가격 분석 결과: 가격이 더 내릴 수 있는 상품이 있습니다."
    else:
        summary = "가격 분석 결과: 현재 적당한 가격대입니다."
    return NotificationPayload(
        chat_id=chat_id,
        success=True,
        stage="price_assessment",
        summary=summary,
        kind="price_assessment",
        details={"assessments": list(assessments)},
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
        parse_mode = self._formatter.parse_mode()
        attempt = 1
        while True:
            try:
                self._dispatch(payload, message, parse_mode=parse_mode)
                return
            except self._retryable_exceptions as exc:
                if attempt >= self._max_attempts:
                    raise NotificationDeliveryError(
                        f"failed to deliver notification after {attempt} attempts"
                    ) from exc
                attempt += 1
                if self._sleep_seconds > 0:
                    self._sleep_func(self._sleep_seconds)

    def _dispatch(self, payload: NotificationPayload, message: str, *, parse_mode: str | None) -> object | None:
        photo_payload = payload.details.get("photo")
        sent_photo = False
        if hasattr(self._sender, "send_photo") and isinstance(photo_payload, dict):
            photo_url = str(photo_payload.get("url", "")).strip()
            caption = photo_payload.get("caption")
            if photo_url:
                self._sender.send_photo(
                    chat_id=payload.chat_id,
                    photo=photo_url,
                    caption=None if caption in (None, "") else str(caption),
                    parse_mode=parse_mode,
                )
                sent_photo = True
        if payload.kind == "proposal" and sent_photo:
            return sent_photo
        if hasattr(self._sender, "send_message"):
            return self._sender.send_message(chat_id=payload.chat_id, text=message, parse_mode=parse_mode)
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
            lines = ["<b>장바구니 담기를 완료했습니다.</b>"]
            lines.extend(product_lines)
            lines.extend(context_lines)
            lines.append(f"<b>요약</b>: {_escape(payload.summary)}")
            message = "\n".join(lines)
            if len(message) <= max_length:
                return message

    lines = ["<b>장바구니 담기를 완료했습니다.</b>", f"<b>요약</b>: {_escape(payload.summary)}"]
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
        product_lines.append(f"• <b>{_escape(name)}</b>\n  {_escape(price)}원 · {_escape(str(quantity))}개")

    remaining = len(products) - len(display_products)
    if remaining > 0:
        product_lines.append(f"• 외 {remaining}건")
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
        matched_lines.append(f"• <i>재구매 참고</i>: {_escape(name)} · 이전 구매 {count}회")
        if len(matched_lines) >= item_limit:
            break
    return matched_lines


def _format_proposal_message(payload: NotificationPayload) -> str:
    candidate = payload.details.get("candidate", {})
    if not isinstance(candidate, dict):
        candidate = {}
    lines = [
        "<b>추천 상품을 찾았습니다.</b>",
        f"<b>상품</b>: {_escape(truncate_text(str(candidate.get('name', '상품 정보 없음')), limit=80))}",
    ]
    option_summary = truncate_text(str(candidate.get("option_summary", "")).strip(), limit=40)
    if option_summary and _normalized_option_summary(option_summary) != _normalized_option_summary(
        str(candidate.get("name", ""))
    ):
        lines.append(f"<b>옵션</b>: {_escape(option_summary)}")
    lines.append(f"<b>가격</b>: {_escape(format_price(int(candidate.get('price_krw', 0))))}원")
    reason = str(candidate.get("selection_reason", payload.summary)).strip()
    reason = " ".join(reason.split())
    lines.append(f"<b>추천 이유</b>: {_escape(reason)}")

    # Include price comparison table from multiple sources
    price_assessments = payload.details.get("price_assessments", [])
    if isinstance(price_assessments, list) and price_assessments:
        lines.append("")
        lines.append("📊 <b>가격 비교</b>")
        # Coupang price from candidate
        coupang_price = int(candidate.get("price_krw", 0))
        if coupang_price > 0:
            lines.append(f"  <b>쿠팡가격</b>: {_escape(format_price(coupang_price))}원")

        # Source-specific price rows
        _SOURCE_LABELS = {"danawa": "다나와", "lowchart": "로우차트", "geniealert": "지니얼럿"}
        for assessment in price_assessments:
            if not isinstance(assessment, dict):
                continue
            source = str(assessment.get("source", ""))
            label = _SOURCE_LABELS.get(source, source)
            lowest = int(assessment.get("lowest_price_krw", 0))
            avg = int(assessment.get("average_price_krw", 0))
            current_src = int(assessment.get("current_price_krw", 0))
            parts = []
            if current_src > 0:
                parts.append(f"현재 {_escape(format_price(current_src))}원")
            if lowest > 0 and lowest != current_src:
                parts.append(f"최저 {_escape(format_price(lowest))}원")
            if avg > 0 and avg != current_src and avg != lowest:
                parts.append(f"평균 {_escape(format_price(avg))}원")
            if parts:
                lines.append(f"  <b>{_escape(label)}</b>: {' · '.join(parts)}")

        # Unified verdict from the best-confidence assessment
        best = max(price_assessments, key=lambda a: float(a.get("discount_pct_vs_avg", 0)) if isinstance(a, dict) else 0)
        if isinstance(best, dict):
            verdict = str(best.get("verdict", ""))
            verdict_label = _VERDICT_LABELS.get(verdict, "")
            if verdict_label:
                lines.append(f"  <b>판정</b>: {verdict_label}")
                verdict_reason = str(best.get("verdict_reason", ""))
                if verdict_reason:
                    lines.append(f"  <i>{_escape(verdict_reason)}</i>")

    lines.append("")
    lines.append(
        "진행: <code>ㅇㅇ 담아줘</code>\n"
        "다른 추천: <code>다른 거 보여줘</code>\n"
        "취소: <code>취소</code>"
    )
    return "\n".join(lines)


def _format_remove_message(payload: NotificationPayload) -> str:
    removed = payload.details.get("removed_products", [])
    failed = payload.details.get("failed_products", [])
    if not isinstance(removed, list):
        removed = []
    if not isinstance(failed, list):
        failed = []
    if payload.success:
        lines = ["<b>장바구니에서 상품을 제거했습니다.</b>"]
        for item in removed:
            if isinstance(item, dict):
                name = truncate_text(str(item.get("product_name", "상품")), limit=40)
                lines.append(f"• {_escape(name)}")
        lines.append(f"<b>요약</b>: {_escape(payload.summary)}")
    else:
        lines = ["<b>장바구니 상품 제거에 실패했습니다.</b>"]
        for item in failed:
            if isinstance(item, dict):
                name = truncate_text(str(item.get("product_name", "상품")), limit=40)
                msg = str(item.get("message", ""))
                line = f"• {_escape(name)}"
                if msg:
                    line += f" — {_escape(truncate_text(msg, limit=80))}"
                lines.append(line)
        lines.append(f"<b>원인</b>: {_escape(payload.summary)}")
    return "\n".join(lines)


def _format_cancelled_message(payload: NotificationPayload) -> str:
    return _escape(payload.summary)


_VERDICT_LABELS = {
    "buy_now": "🟢 지금 사는 게 이득",
    "reasonable": "🟡 적당한 가격",
    "wait": "🔴 기다리는 게 나음",
}

_VERDICT_EMOJIS = {
    "buy_now": "🟢",
    "reasonable": "🟡",
    "wait": "🔴",
}


def _format_price_assessment_message(payload: NotificationPayload) -> str:
    assessments = payload.details.get("assessments", [])
    if not isinstance(assessments, list):
        assessments = []
    lines = ["<b>📊 가격 분석 리포트</b>"]
    for assessment in assessments:
        if not isinstance(assessment, dict):
            continue
        name = truncate_text(str(assessment.get("product_name", "상품")), limit=40)
        verdict = str(assessment.get("verdict", "reasonable"))
        verdict_label = _VERDICT_LABELS.get(verdict, verdict)
        current = int(assessment.get("current_price_krw", 0))
        avg = int(assessment.get("average_price_krw", 0))
        lowest = int(assessment.get("lowest_price_krw", 0))
        recent_low = assessment.get("recent_low_30d_krw")
        reason = truncate_text(str(assessment.get("verdict_reason", "")), limit=120)
        source = str(assessment.get("source", ""))

        lines.append(f"\n<b>{_escape(name)}</b>")
        lines.append(f"  판정: {verdict_label}")
        lines.append(f"  현재가: {_escape(format_price(current))}원")
        lines.append(f"  평균가: {_escape(format_price(avg))}원 · 최저가: {_escape(format_price(lowest))}원")
        if recent_low is not None:
            lines.append(f"  최근 30일 최저: {_escape(format_price(int(recent_low)))}원")
        discount = assessment.get("discount_pct_vs_avg")
        if discount is not None and float(discount) != 0:
            direction = "저렴" if float(discount) > 0 else "비쌈"
            lines.append(f"  평균 대비: {abs(float(discount)):.1f}% {direction}")
        lines.append(f"  <i>{_escape(reason)}</i>")
        if source:
            lines.append(f"  출처: {_escape(source)}")
    return "\n".join(lines)


def _build_proposal_caption(
    *, summary: str, candidate: dict[str, object],
    price_assessments: list[dict[str, object]] | None = None,
) -> str:
    parts = [f"<b>{_escape(truncate_text(str(candidate.get('name', '상품 정보 없음')), limit=80))}</b>"]
    option_summary = truncate_text(str(candidate.get("option_summary", "")).strip(), limit=32)
    if option_summary and _normalized_option_summary(option_summary) != _normalized_option_summary(
        str(candidate.get("name", ""))
    ):
        parts.append(_escape(option_summary))
    parts.append(f"<b>{_escape(format_price(int(candidate.get('price_krw', 0))))}원</b>")
    summary_text = str(summary).strip()
    if "담기를 완료했습니다." in summary_text:
        reason = summary_text
    else:
        reason = str(candidate.get("selection_reason", summary)).strip()
    reason = " ".join(reason.split())
    parts.append(_escape(reason))

    # Price comparison inline (compact for 1024-char caption limit)
    if price_assessments:
        _SRC = {"danawa": "다나와", "lowchart": "로우차트", "geniealert": "지니얼럿"}
        price_lines: list[str] = []
        coupang_price = int(candidate.get("price_krw", 0))
        if coupang_price > 0:
            price_lines.append(f"쿠팡 {format_price(coupang_price)}원")
        for a in price_assessments:
            if not isinstance(a, dict):
                continue
            src = _SRC.get(str(a.get("source", "")), str(a.get("source", "")))
            cur = int(a.get("current_price_krw", 0))
            low = int(a.get("lowest_price_krw", 0))
            if cur > 0:
                txt = f"{src} {format_price(cur)}원"
                if low > 0 and low != cur:
                    txt += f"(최저 {format_price(low)}원)"
                price_lines.append(txt)
        if price_lines:
            parts.append("\n📊 " + " · ".join(price_lines))
        best = max(price_assessments, key=lambda x: float(x.get("discount_pct_vs_avg", 0)) if isinstance(x, dict) else 0)
        if isinstance(best, dict):
            vl = _VERDICT_LABELS.get(str(best.get("verdict", "")), "")
            if vl:
                parts.append(vl)

    parts.append(
        "\n진행: ㅇㅇ 담아줘\n"
        "다른 추천: 다른 거 보여줘\n"
        "취소: 취소"
    )
    return "\n".join(part for part in parts if part)


def _format_failure_message(payload: NotificationPayload) -> str:
    reason = str(payload.details.get("failure_reason", payload.summary))
    detail = payload.details.get("failure_detail")
    lines = [
        "<b>장바구니 담기에 실패했습니다.</b>",
        f"<b>단계</b>: <code>{_escape(payload.stage)}</code>",
        f"<b>원인</b>: {_escape(truncate_text(reason, limit=180))}",
    ]
    if detail:
        lines.append(f"<b>상세</b>: {_escape(truncate_text(str(detail), limit=160))}")
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


def _normalized_option_summary(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()


def _escape(text: str) -> str:
    return html.escape(text, quote=True)


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
