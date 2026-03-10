from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from .contracts import RequestedItem, ShoppingRequest


class TelegramIntakeError(ValueError):
    """Raised when a Telegram message cannot be converted into a ShoppingRequest."""


@dataclass(slots=True)
class TelegramInboundMessage:
    """Normalized Telegram message envelope extracted from an update payload."""

    update_id: int
    user_id: str
    chat_id: str
    text: str


@dataclass(slots=True)
class TelegramIntakeResult:
    """One polling result mapped to either a parsed request or a user-facing error."""

    update_id: int
    chat_id: str | None
    request: ShoppingRequest | None = None
    error_message: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "update_id": self.update_id,
            "chat_id": self.chat_id,
            "request": asdict(self.request) if self.request is not None else None,
            "error_message": self.error_message,
        }


class TelegramBotApiClient:
    """Minimal Telegram Bot API client using long polling over the standard library."""

    def __init__(
        self,
        *,
        token: str,
        base_url: str = "https://api.telegram.org",
        opener: Any | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._opener = opener or urllib.request.urlopen

    def get_updates(self, *, offset: int | None = None, timeout: int = 30) -> list[dict[str, Any]]:
        payload: dict[str, object] = {"timeout": timeout}
        if offset is not None:
            payload["offset"] = offset
        response = self._post("getUpdates", payload)
        return list(response.get("result", []))

    def send_message(self, *, chat_id: str, text: str) -> dict[str, Any]:
        return self._post("sendMessage", {"chat_id": chat_id, "text": text})

    def _post(self, method: str, payload: dict[str, object]) -> dict[str, Any]:
        encoded = urllib.parse.urlencode(payload).encode("utf-8")
        request = urllib.request.Request(
            url=f"{self._base_url}/bot{self._token}/{method}",
            data=encoded,
            method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        try:
            with self._opener(request) as response:
                body = response.read().decode("utf-8")
        except urllib.error.URLError as exc:
            raise RuntimeError(f"Telegram Bot API request failed: {exc}") from exc
        data = json.loads(body)
        if not data.get("ok", False):
            raise RuntimeError(f"Telegram Bot API returned an error for {method}: {body}")
        return data


class TelegramPollingIntakeService:
    """Receive Telegram updates via polling and parse shopping requests."""

    _ORDER_QUANTITY_PATTERN = re.compile(
        r"(?<![0-9])(?P<quantity>\d+)\s*(?P<unit>개|병|봉|팩|캔|세트|입|박스|줄|통)(?![A-Za-z])"
    )
    _MAX_PRICE_PATTERNS = (
        re.compile(r"(?P<price>\d[\d,]*)\s*원\s*이하"),
        re.compile(r"최대\s*(?P<price>\d[\d,]*)\s*원"),
        re.compile(r"예산\s*(?P<price>\d[\d,]*)\s*원"),
    )
    _SUFFIX_PATTERN = re.compile(r"(?:장바구니에\s*)?담아줘[.!?~ ]*$")
    _TRAILING_SEPARATOR_PATTERN = re.compile(r"(?:\n|;|,|\s그리고)\s*$")
    _CONSTRAINT_MARKERS = ("옵션", "조건")

    def __init__(self, client: TelegramBotApiClient | None = None) -> None:
        self._client = client

    def parse_message(self, *, user_id: str, chat_id: str, text: str) -> ShoppingRequest:
        normalized_text = self._normalize_message_text(text)
        item_fragments = self._split_item_fragments(normalized_text)
        items = [self._parse_item_fragment(fragment) for fragment in item_fragments]
        return ShoppingRequest(
            user_id=user_id,
            chat_id=chat_id,
            items=items,
            raw_text=text.strip(),
            request_id=f"telegram-request-{uuid4()}",
            received_at=datetime.now(UTC),
        )

    def extract_inbound_message(self, update: dict[str, Any]) -> TelegramInboundMessage:
        update_id = int(update.get("update_id", 0))
        message = update.get("message")
        if not isinstance(message, dict):
            raise TelegramIntakeError("지원하지 않는 업데이트 형식입니다. 텍스트 메시지를 보내주세요.")

        text = message.get("text")
        chat = message.get("chat") or {}
        sender = message.get("from") or {}
        if not isinstance(text, str) or not text.strip():
            raise TelegramIntakeError("텍스트 메시지로 상품 요청을 보내주세요. 예: 콜라 제로 2개 담아줘")
        chat_id = chat.get("id")
        sender_id = sender.get("id")
        if chat_id is None or sender_id is None:
            raise TelegramIntakeError("텔레그램 메시지 메타데이터가 부족합니다.")

        return TelegramInboundMessage(
            update_id=update_id,
            user_id=f"telegram:{sender_id}",
            chat_id=str(chat_id),
            text=text.strip(),
        )

    def handle_update(self, update: dict[str, Any]) -> TelegramIntakeResult:
        update_id = int(update.get("update_id", 0))
        chat_id = None
        try:
            inbound = self.extract_inbound_message(update)
            chat_id = inbound.chat_id
            request = self.parse_message(
                user_id=inbound.user_id,
                chat_id=inbound.chat_id,
                text=inbound.text,
            )
            request.request_id = f"telegram-update-{inbound.update_id}"
            return TelegramIntakeResult(
                update_id=inbound.update_id,
                chat_id=inbound.chat_id,
                request=request,
            )
        except TelegramIntakeError as exc:
            message = update.get("message")
            if isinstance(message, dict):
                chat = message.get("chat") or {}
                if chat.get("id") is not None:
                    chat_id = str(chat["id"])
            return TelegramIntakeResult(
                update_id=update_id,
                chat_id=chat_id,
                error_message=self.build_error_message(str(exc)),
            )

    def poll_once(self, *, offset: int | None = None, timeout: int = 30) -> list[TelegramIntakeResult]:
        if self._client is None:
            raise RuntimeError("Telegram client is required for polling.")
        updates = self._client.get_updates(offset=offset, timeout=timeout)
        return [self.handle_update(update) for update in updates]

    @staticmethod
    def build_error_message(detail: str) -> str:
        return f"{detail} 형식 예시: 콜라 제로 2개 담아줘"

    def _normalize_message_text(self, text: str) -> str:
        stripped = re.sub(r"[^\S\n]+", " ", text.strip())
        stripped = re.sub(r"\n{2,}", "\n", stripped)
        if not stripped:
            raise TelegramIntakeError("비어 있는 요청입니다.")
        if self._SUFFIX_PATTERN.search(stripped) is None:
            raise TelegramIntakeError("`... 담아줘` 형식으로 요청해주세요.")
        core = self._SUFFIX_PATTERN.sub("", stripped).strip()
        core = re.sub(r"^(장바구니에\s*)", "", core).strip()
        if not core:
            raise TelegramIntakeError("상품명을 포함해서 요청해주세요.")
        if self._TRAILING_SEPARATOR_PATTERN.search(core):
            raise TelegramIntakeError("마지막 상품명 뒤에는 연결어 없이 요청을 끝내주세요.")
        return core

    def _split_item_fragments(self, text: str) -> list[str]:
        fragments = [part.strip(" ,") for part in re.split(r"\s*(?:\n|;| 그리고 )\s*", text) if part.strip(" ,")]
        if not fragments:
            raise TelegramIntakeError("상품명을 포함해서 요청해주세요.")
        return fragments

    def _parse_item_fragment(self, fragment: str) -> RequestedItem:
        working = fragment.strip()
        constraints: list[str] = []
        max_price_krw: int | None = None

        for pattern in self._MAX_PRICE_PATTERNS:
            match = pattern.search(working)
            if match:
                max_price_krw = int(match.group("price").replace(",", ""))
                working = pattern.sub("", working, count=1).strip(" ,")
                break

        working = self._extract_wrapped_constraints(working, constraints)
        working = self._extract_marker_constraints(working, constraints)

        quantity = 1
        quantity_matches = list(self._ORDER_QUANTITY_PATTERN.finditer(working))
        if quantity_matches:
            quantity_match = quantity_matches[-1]
            quantity = int(quantity_match.group("quantity"))
            working = (
                working[: quantity_match.start()] + " " + working[quantity_match.end() :]
            ).strip(" ,")

        name = re.sub(r"\s+", " ", working).strip(" ,")
        if not name:
            raise TelegramIntakeError("상품명을 포함해서 요청해주세요.")
        if quantity <= 0:
            raise TelegramIntakeError("수량은 1개 이상이어야 합니다.")

        deduped_constraints = list(dict.fromkeys(item for item in constraints if item))
        return RequestedItem(
            name=name,
            quantity=quantity,
            constraints=deduped_constraints,
            max_price_krw=max_price_krw,
        )

    def _extract_wrapped_constraints(self, text: str, constraints: list[str]) -> str:
        pattern = re.compile(r"[\(\[](?P<body>[^()\[\]]+)[\)\]]")
        working = text
        while True:
            match = pattern.search(working)
            if match is None:
                break
            self._extend_constraints(constraints, match.group("body"))
            working = (working[: match.start()] + " " + working[match.end() :]).strip(" ,")
        return working

    def _extract_marker_constraints(self, text: str, constraints: list[str]) -> str:
        working = text
        for marker in self._CONSTRAINT_MARKERS:
            match = re.search(rf"\b{marker}\s*[:：]?\s*(.+)$", working)
            if match:
                self._extend_constraints(constraints, match.group(1))
                working = working[: match.start()].strip(" ,")
        return working

    @staticmethod
    def _extend_constraints(constraints: list[str], raw_value: str) -> None:
        parts = re.split(r"\s*(?:,|/|·|및)\s*", raw_value.strip())
        constraints.extend(part for part in parts if part)
