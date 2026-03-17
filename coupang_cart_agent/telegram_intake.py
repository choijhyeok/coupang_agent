from __future__ import annotations

import json
import re
import ssl
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import truststore

from .contracts import IntakeMode, RequestedItem, RequestSession, ShoppingRequest, ShoppingRequestEnvelope
from .telegram_persistence import TelegramIntakeRepository


class TelegramIntakeError(ValueError):
    """Raised when a Telegram message cannot be converted into a ShoppingRequest."""


@dataclass(slots=True)
class TelegramInboundMessage:
    """Normalized Telegram message envelope extracted from an update payload."""

    update_id: int
    message_id: int | None
    user_id: str
    chat_id: str
    text: str
    received_at: datetime


@dataclass(slots=True)
class TelegramIntakeResult:
    """One polling result mapped to either a parsed request or a user-facing error."""

    update_id: int
    chat_id: str | None
    request: ShoppingRequest | None = None
    envelope: ShoppingRequestEnvelope | None = None
    error_message: str | None = None
    error_response_sent: bool = False

    def as_dict(self) -> dict[str, object]:
        return {
            "update_id": self.update_id,
            "chat_id": self.chat_id,
            "request": asdict(self.request) if self.request is not None else None,
            "envelope": asdict(self.envelope) if self.envelope is not None else None,
            "error_message": self.error_message,
            "error_response_sent": self.error_response_sent,
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
        self._opener = opener or _build_default_telegram_opener().open

    def get_updates(self, *, offset: int | None = None, timeout: int = 30) -> list[dict[str, Any]]:
        payload: dict[str, object] = {"timeout": timeout}
        if offset is not None:
            payload["offset"] = offset
        response = self._post("getUpdates", payload)
        return list(response.get("result", []))

    def get_me(self) -> dict[str, Any]:
        response = self._post("getMe", {})
        return dict(response.get("result", {}))

    def send_message(self, *, chat_id: str, text: str) -> dict[str, Any]:
        return self._post("sendMessage", {"chat_id": chat_id, "text": text})

    def send_photo(
        self,
        *,
        chat_id: str,
        photo: str,
        caption: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, object] = {"chat_id": chat_id, "photo": photo}
        if caption:
            payload["caption"] = caption
        return self._post("sendPhoto", payload)

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


def _build_default_telegram_opener():
    ssl_context = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ssl_context.load_default_certs()
    return urllib.request.build_opener(urllib.request.HTTPSHandler(context=ssl_context))


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
    _UNIT_SIZE_PATTERN = re.compile(r"(?P<size>\d+(?:\.\d+)?)\s*(?P<unit>ml|l|kg|g)\b", re.IGNORECASE)
    _NON_BRAND_TOKENS = {
        "생수",
        "물",
        "음료",
        "콜라",
        "제로",
        "라면",
        "컵라면",
        "양파",
        "오트밀",
        "두유",
        "휴지",
        "세제",
        "우유",
        "계란",
        "쌀",
        "물티슈",
    }
    _FOLLOW_UP_REPLY_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
        ("cancel", re.compile(r"^(취소|그만|중단)(해줘)?$")),
        ("next", re.compile(r"^(다른\s*거|다른상품|다른 상품)(\s*보여줘|\s*줘)?$")),
        ("reject", re.compile(r"^(아니|아니야|별로|말고)(\s*담아줘)?$")),
        (
            "confirm",
            re.compile(
                r"^(ㅇㅇ|응|네|예|좋아|좋아요|그래|그거|이걸로|진행해줘|담아줘|넣어줘)(\s*담아줘)?$"
            ),
        ),
    )

    def __init__(
        self,
        client: TelegramBotApiClient | None = None,
        repository: TelegramIntakeRepository | None = None,
    ) -> None:
        self._client = client
        self._repository = repository

    def parse_message(self, *, user_id: str, chat_id: str, text: str) -> ShoppingRequest:
        stripped_text = text.strip()
        if self.classify_follow_up_message(stripped_text) is not None:
            return ShoppingRequest(
                user_id=user_id,
                chat_id=chat_id,
                items=[],
                raw_text=stripped_text,
                request_id=f"telegram-request-{uuid4()}",
                received_at=datetime.now(UTC),
            )
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

    def parse_demo_message(self, *, user_id: str, chat_id: str, text: str) -> ShoppingRequest:
        return self.parse_message(user_id=user_id, chat_id=chat_id, text=text)

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
        message_id = message.get("message_id")
        message_date = message.get("date")
        received_at = datetime.now(UTC)
        if isinstance(message_date, int):
            received_at = datetime.fromtimestamp(message_date, tz=UTC)

        return TelegramInboundMessage(
            update_id=update_id,
            message_id=int(message_id) if isinstance(message_id, int) else None,
            user_id=f"telegram:{sender_id}",
            chat_id=str(chat_id),
            text=text.strip(),
            received_at=received_at,
        )

    def handle_update(
        self,
        update: dict[str, Any],
        *,
        mode: IntakeMode = IntakeMode.LIVE,
        send_error_response: bool = True,
    ) -> TelegramIntakeResult:
        update_id = int(update.get("update_id", 0))
        chat_id = None
        error_response_sent = False
        try:
            inbound = self.extract_inbound_message(update)
            chat_id = inbound.chat_id
            request = self.parse_message(
                user_id=inbound.user_id,
                chat_id=inbound.chat_id,
                text=inbound.text,
            )
            request.request_id = f"telegram-update-{inbound.update_id}"
            request.received_at = inbound.received_at
            envelope = self._build_envelope(
                inbound=inbound,
                request=request,
                mode=mode,
                raw_update=update,
            )
            if self._repository is not None:
                self._repository.record_envelope(envelope)
            return TelegramIntakeResult(
                update_id=inbound.update_id,
                chat_id=inbound.chat_id,
                request=request,
                envelope=envelope,
            )
        except TelegramIntakeError as exc:
            context = self._extract_error_context(update)
            if context["chat_id"] is not None:
                chat_id = str(context["chat_id"])
            error_message = self.build_error_message(str(exc))
            session = None
            if self._repository is not None and context["user_id"] and context["chat_id"]:
                session = self._repository.get_or_create_session(
                    user_id=str(context["user_id"]),
                    chat_id=str(context["chat_id"]),
                    occurred_at=context["received_at"],
                )
                self._repository.record_rejected_message(
                    inbound_message_id=self._build_inbound_message_id(
                        update_id=context["update_id"],
                        message_id=context["message_id"],
                    ),
                    session=session,
                    update_id=context["update_id"],
                    message_id=context["message_id"],
                    user_id=str(context["user_id"]),
                    chat_id=str(context["chat_id"]),
                    raw_text=str(context["raw_text"]),
                    error_message=error_message,
                    raw_update=update,
                    occurred_at=context["received_at"],
                    mode=mode.value,
                )
            if send_error_response and self._client is not None and chat_id is not None:
                self._client.send_message(chat_id=chat_id, text=error_message)
                error_response_sent = True
            return TelegramIntakeResult(
                update_id=update_id,
                chat_id=chat_id,
                error_message=error_message,
                error_response_sent=error_response_sent,
            )

    def poll_once(
        self,
        *,
        offset: int | None = None,
        timeout: int = 30,
        mode: IntakeMode = IntakeMode.LIVE,
        send_error_response: bool = True,
    ) -> list[TelegramIntakeResult]:
        if self._client is None:
            raise RuntimeError("Telegram client is required for polling.")
        updates = self._client.get_updates(offset=offset, timeout=timeout)
        return [
            self.handle_update(update, mode=mode, send_error_response=send_error_response)
            for update in updates
        ]

    @staticmethod
    def build_error_message(detail: str) -> str:
        return f"{detail} 형식 예시: 콜라 제로 2개 담아줘"

    def _build_envelope(
        self,
        *,
        inbound: TelegramInboundMessage,
        request: ShoppingRequest,
        mode: IntakeMode,
        raw_update: dict[str, Any],
    ) -> ShoppingRequestEnvelope:
        session = self._build_session(inbound)
        if self._repository is not None:
            session = self._repository.get_or_create_session(
                user_id=inbound.user_id,
                chat_id=inbound.chat_id,
                occurred_at=inbound.received_at,
            )
        return ShoppingRequestEnvelope(
            source="telegram",
            mode=mode,
            request=request,
            session=session,
            inbound_message_id=self._build_inbound_message_id(
                update_id=inbound.update_id,
                message_id=inbound.message_id,
            ),
            update_id=inbound.update_id,
            message_id=inbound.message_id,
            raw_text=inbound.text,
            raw_update=dict(raw_update),
            metadata={
                "chat_id": inbound.chat_id,
                "user_id": inbound.user_id,
                "session_id": session.session_id,
                "follow_up_reply": self.classify_follow_up_message(inbound.text),
            },
        )

    @staticmethod
    def _build_session(inbound: TelegramInboundMessage) -> RequestSession:
        session_id = TelegramIntakeRepository.build_session_id(
            user_id=inbound.user_id,
            chat_id=inbound.chat_id,
        )
        return RequestSession(
            session_id=session_id,
            channel="telegram",
            user_id=inbound.user_id,
            chat_id=inbound.chat_id,
            created_at=inbound.received_at,
            last_message_at=inbound.received_at,
        )

    @staticmethod
    def _build_inbound_message_id(*, update_id: int | None, message_id: int | None) -> str:
        if update_id is not None:
            return f"telegram-update-{update_id}"
        if message_id is not None:
            return f"telegram-message-{message_id}"
        return f"telegram-inbound-{uuid4()}"

    @staticmethod
    def _extract_error_context(update: dict[str, Any]) -> dict[str, Any]:
        message = update.get("message")
        received_at = datetime.now(UTC)
        if not isinstance(message, dict):
            return {
                "update_id": int(update.get("update_id", 0)) or None,
                "message_id": None,
                "user_id": None,
                "chat_id": None,
                "raw_text": "",
                "received_at": received_at,
            }
        message_date = message.get("date")
        if isinstance(message_date, int):
            received_at = datetime.fromtimestamp(message_date, tz=UTC)
        chat = message.get("chat") or {}
        sender = message.get("from") or {}
        sender_id = sender.get("id")
        user_id = f"telegram:{sender_id}" if sender_id is not None else None
        return {
            "update_id": int(update.get("update_id", 0)) or None,
            "message_id": int(message["message_id"]) if isinstance(message.get("message_id"), int) else None,
            "user_id": user_id,
            "chat_id": str(chat["id"]) if chat.get("id") is not None else None,
            "raw_text": message.get("text", ""),
            "received_at": received_at,
        }

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

    @classmethod
    def classify_follow_up_message(cls, text: str) -> str | None:
        normalized = re.sub(r"\s+", "", text.strip().lower())
        if not normalized:
            return None
        for reply_kind, pattern in cls._FOLLOW_UP_REPLY_PATTERNS:
            if pattern.fullmatch(normalized):
                return reply_kind
        return None

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
        quantity_unit: str | None = None
        quantity_matches = list(self._ORDER_QUANTITY_PATTERN.finditer(working))
        if quantity_matches:
            quantity_match = quantity_matches[-1]
            quantity = int(quantity_match.group("quantity"))
            quantity_unit = quantity_match.group("unit")
            working = (
                working[: quantity_match.start()] + " " + working[quantity_match.end() :]
            ).strip(" ,")

        name = re.sub(r"\s+", " ", working).strip(" ,")
        if not name:
            raise TelegramIntakeError("상품명을 포함해서 요청해주세요.")
        if quantity <= 0:
            raise TelegramIntakeError("수량은 1개 이상이어야 합니다.")

        deduped_constraints = list(dict.fromkeys(item for item in constraints if item))
        explicit_unit_size = self._extract_explicit_unit_size(name)
        return RequestedItem(
            name=name,
            quantity=quantity,
            constraints=deduped_constraints,
            max_price_krw=max_price_krw,
            explicit_brand=self._extract_explicit_brand(name),
            explicit_unit_size=explicit_unit_size,
            explicit_pack_count=self._extract_explicit_pack_count(
                quantity=quantity,
                quantity_unit=quantity_unit,
                explicit_unit_size=explicit_unit_size,
            ),
            explicit_pack_unit=self._extract_explicit_pack_unit(
                quantity=quantity,
                quantity_unit=quantity_unit,
                explicit_unit_size=explicit_unit_size,
            ),
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

    @classmethod
    def _extract_explicit_brand(cls, text: str) -> str | None:
        tokens = [token.strip() for token in re.split(r"\s+", text) if token.strip()]
        has_unit_size = any(cls._UNIT_SIZE_PATTERN.fullmatch(token) is not None for token in tokens)
        meaningful_tokens = [
            token
            for token in tokens
            if cls._UNIT_SIZE_PATTERN.fullmatch(token) is None and not token.isdigit()
        ]
        if not meaningful_tokens:
            return None
        brand = meaningful_tokens[0]
        if len(meaningful_tokens) < 2 and not has_unit_size:
            return None
        if brand.lower() in cls._NON_BRAND_TOKENS:
            return None
        return brand

    @classmethod
    def _extract_explicit_unit_size(cls, text: str) -> str | None:
        match = cls._UNIT_SIZE_PATTERN.search(text)
        if match is None:
            return None
        return f"{match.group('size')}{match.group('unit').lower()}"

    @staticmethod
    def _extract_explicit_pack_count(
        *,
        quantity: int,
        quantity_unit: str | None,
        explicit_unit_size: str | None,
    ) -> int | None:
        if quantity_unit in {"박스", "팩", "세트", "입"}:
            return quantity
        if quantity == 1 and quantity_unit in {"개", "병", "봉", "캔", "통"} and explicit_unit_size is not None:
            return quantity
        return None

    @staticmethod
    def _extract_explicit_pack_unit(
        *,
        quantity: int,
        quantity_unit: str | None,
        explicit_unit_size: str | None,
    ) -> str | None:
        if quantity_unit in {"박스", "팩", "세트", "입"}:
            return quantity_unit
        if quantity == 1 and quantity_unit in {"개", "병", "봉", "캔", "통"} and explicit_unit_size is not None:
            return quantity_unit
        return None
