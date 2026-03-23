from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from typing import Literal

from pydantic import BaseModel, Field

from .contracts import (
    PriorPurchaseRecord,
    RequestedItem,
    SessionSelectionSignal,
    ShoppingRequest,
    build_requested_item_search_query,
)


# ---------------------------------------------------------------------------
# Pydantic structured-output schema for conversation intent classification
# ---------------------------------------------------------------------------

_VALID_DECISIONS = ("confirm", "reject", "next", "cancel", "new_request", "remove_request")


class RewrittenQuery(BaseModel):
    item_name: str = Field(description="The product name for the search query")
    query: str = Field(description="The rewritten Coupang search query")


class ConversationClassification(BaseModel):
    """LLM-produced structured classification of a user message."""

    decision: Literal[
        "confirm", "reject", "next", "cancel", "new_request", "remove_request"
    ] = Field(
        description=(
            "confirm: 사용자가 추천을 수락. "
            "reject: 추천 거절. "
            "next: 다른 후보 요청. "
            "cancel: 대화 취소. "
            "new_request: 새 상품 장바구니 담기 요청. "
            "remove_request: 장바구니에서 상품 제거 요청 (빼줘/제거해줘/삭제해줘/제외해줘)."
        )
    )
    reason: str = Field(
        default="",
        description="한 줄 근거",
    )
    rewritten_queries: list[RewrittenQuery] = Field(
        default_factory=list,
        description="decision이 new_request일 때만 대화 맥락을 반영한 검색 쿼리 목록. 그 외에는 빈 리스트.",
    )
    conversation_summary: str = Field(
        default="",
        description="이번 턴까지 포함한 대화 요약 (한국어, ≤120자)",
    )


def _conversation_classification_json_schema() -> dict:
    """Build the strict JSON schema object for Azure OpenAI response_format."""
    schema = ConversationClassification.model_json_schema()
    return {
        "type": "json_schema",
        "json_schema": {
            "name": "ConversationClassification",
            "strict": True,
            "schema": _make_strict(schema),
        },
    }


def _make_strict(schema: dict) -> dict:
    """Recursively set additionalProperties: false for strict mode."""
    schema = dict(schema)
    if schema.get("type") == "object":
        schema["additionalProperties"] = False
        props = schema.get("properties", {})
        schema["properties"] = {
            k: _make_strict(v) for k, v in props.items()
        }
        # strict mode requires all properties in 'required'
        schema["required"] = list(props.keys())
    if "$defs" in schema:
        schema["$defs"] = {
            k: _make_strict(v) for k, v in schema["$defs"].items()
        }
    items = schema.get("items")
    if isinstance(items, dict):
        schema["items"] = _make_strict(items)
    return schema


@dataclass(slots=True)
class AgentSearchQuery:
    item_name: str
    query: str


@dataclass(slots=True)
class AgentPlan:
    mode: str
    search_queries: list[AgentSearchQuery]
    operator_note: str
    selection_brief: str
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(slots=True)
class ParsedTelegramRequest:
    items: list[RequestedItem]
    warnings: list[str] = field(default_factory=list)


@dataclass(slots=True)
class ConversationIntent:
    decision: str
    reason: str = ""
    rewritten_queries: list[AgentSearchQuery] = field(default_factory=list)
    conversation_summary: str = ""


class AzureOpenAIPlanner:
    """Small Azure OpenAI adapter for the live agent-planning node."""

    def __init__(
        self,
        *,
        endpoint: str | None,
        api_key: str | None,
        deployment: str | None,
        api_version: str = "2024-12-01-preview",
        timeout_seconds: int = 30,
    ) -> None:
        self._endpoint = (endpoint or "").rstrip("/")
        self._api_key = api_key or ""
        self._deployment = deployment or ""
        self._api_version = api_version
        self._timeout_seconds = timeout_seconds

    def is_configured(self) -> bool:
        return bool(self._endpoint and self._api_key and self._deployment)

    def plan_request(
        self,
        request: ShoppingRequest,
        *,
        prior_purchases: list[PriorPurchaseRecord] | None = None,
        recent_session_signals: list[SessionSelectionSignal] | None = None,
    ) -> AgentPlan:
        fallback = self._fallback_plan(
            request,
            prior_purchases=prior_purchases or [],
            recent_session_signals=recent_session_signals or [],
        )
        if not self.is_configured():
            fallback.warnings.append("Azure OpenAI configuration is incomplete; using deterministic fallback plan.")
            return fallback

        payload = {
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You are an operations planner for a Telegram-to-Coupang cart workflow. "
                        "Return strict JSON with keys search_queries, operator_note, selection_brief. "
                        "search_queries must be a list of objects with item_name and query. "
                        "Treat explicit brand, unit-size, and pack-size constraints as hard requirements."
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "request": {
                                "user_id": request.user_id,
                                "chat_id": request.chat_id,
                                "request_id": request.request_id,
                                "raw_text": request.raw_text,
                                "items": [asdict(item) for item in request.items],
                            },
                            "prior_purchases": [asdict(record) for record in prior_purchases or []],
                            "recent_session_signals": [asdict(signal) for signal in recent_session_signals or []],
                        },
                        ensure_ascii=False,
                        default=str,
                    ),
                },
            ],
            "response_format": {"type": "json_object"},
        }

        try:
            response = self._post(payload)
            content = _read_message_content(response)
            parsed = json.loads(content)
            search_queries = [
                AgentSearchQuery(
                    item_name=str(raw.get("item_name", "")).strip(),
                    query=str(raw.get("query", "")).strip(),
                )
                for raw in parsed.get("search_queries", [])
                if str(raw.get("item_name", "")).strip() and str(raw.get("query", "")).strip()
            ]
            if not search_queries:
                raise ValueError("Azure OpenAI response did not include any usable search_queries")
            return AgentPlan(
                mode="azure_openai",
                search_queries=search_queries,
                operator_note=str(parsed.get("operator_note", "")).strip() or fallback.operator_note,
                selection_brief=str(parsed.get("selection_brief", "")).strip() or fallback.selection_brief,
                warnings=[],
            )
        except Exception as exc:
            fallback.warnings.append(f"Azure OpenAI planning failed: {exc}")
            return fallback

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

    @staticmethod
    def _fallback_plan(
        request: ShoppingRequest,
        *,
        prior_purchases: list[PriorPurchaseRecord],
        recent_session_signals: list[SessionSelectionSignal],
    ) -> AgentPlan:
        prior_product_names = ", ".join(record.product_name for record in prior_purchases[:2])
        recent_signals = ", ".join(signal.signal for signal in recent_session_signals[:2])
        search_queries = []
        for item in request.items:
            search_queries.append(
                AgentSearchQuery(
                    item_name=item.name,
                    query=build_requested_item_search_query(item),
                )
            )

        note_fragments = ["Prefer high-rating, high-review products without blindly picking the cheapest option."]
        if prior_product_names:
            note_fragments.append(f"Prior purchases available: {prior_product_names}.")
        if recent_signals:
            note_fragments.append(f"Recent session signals: {recent_signals}.")

        return AgentPlan(
            mode="fallback",
            search_queries=search_queries,
            operator_note=" ".join(note_fragments),
            selection_brief=(
                "Use request terms as search queries, keep explicit brand and pack/unit-size constraints as hard "
                "filters, then rank matching candidates with rating, reviews, and price."
            ),
            warnings=[],
        )


class AzureOpenAIRequestParser:
    """Optional structured-output parser for conversational Telegram shopping requests."""

    def __init__(
        self,
        *,
        endpoint: str | None,
        api_key: str | None,
        deployment: str | None,
        api_version: str = "2024-12-01-preview",
        timeout_seconds: int = 30,
    ) -> None:
        self._endpoint = (endpoint or "").rstrip("/")
        self._api_key = api_key or ""
        self._deployment = deployment or ""
        self._api_version = api_version
        self._timeout_seconds = timeout_seconds

    def is_configured(self) -> bool:
        return bool(self._endpoint and self._api_key and self._deployment)

    def parse_items(self, *, raw_text: str, normalized_text: str) -> ParsedTelegramRequest | None:
        if not self.is_configured():
            return None
        payload = {
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You parse Korean Telegram shopping requests into strict JSON. "
                        "Return a JSON object with one key: items. "
                        "items must be a list of objects with keys: "
                        "name, quantity, constraints, max_price_krw, explicit_brand, explicit_unit_size, "
                        "explicit_pack_count, explicit_pack_unit. "
                        "Split conversational multi-item requests like '의자랑 쇼파도 담아줘' into separate items. "
                        "Do not include any explanatory prose."
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "raw_text": raw_text,
                            "normalized_text": normalized_text,
                        },
                        ensure_ascii=False,
                    ),
                },
            ],
            "response_format": {"type": "json_object"},
        }
        response = self._post(payload)
        parsed = json.loads(_read_message_content(response))
        raw_items = parsed.get("items", [])
        if not isinstance(raw_items, list):
            raise ValueError("Azure OpenAI request parser returned non-list items")
        items: list[RequestedItem] = []
        for raw in raw_items:
            if not isinstance(raw, dict):
                continue
            name = str(raw.get("name", "")).strip()
            if not name:
                continue
            constraints = raw.get("constraints", [])
            items.append(
                RequestedItem(
                    name=name,
                    quantity=max(1, int(raw.get("quantity", 1))),
                    constraints=[
                        str(value).strip()
                        for value in constraints
                        if str(value).strip()
                    ]
                    if isinstance(constraints, list)
                    else [],
                    max_price_krw=(
                        None
                        if raw.get("max_price_krw") in (None, "", 0)
                        else int(raw["max_price_krw"])
                    ),
                    explicit_brand=_none_if_blank(raw.get("explicit_brand")),
                    explicit_unit_size=_none_if_blank(raw.get("explicit_unit_size")),
                    explicit_pack_count=(
                        None
                        if raw.get("explicit_pack_count") in (None, "", 0)
                        else int(raw["explicit_pack_count"])
                    ),
                    explicit_pack_unit=_none_if_blank(raw.get("explicit_pack_unit")),
                )
            )
        if not items:
            raise ValueError("Azure OpenAI request parser returned no usable items")
        return ParsedTelegramRequest(items=items)

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


def _none_if_blank(value: object) -> str | None:
    text = str(value or "").strip()
    return text or None


class AzureOpenAIConversationInterpreter:
    """Interpret follow-up user intent using current proposal state and recent thread history."""

    def __init__(
        self,
        *,
        endpoint: str | None,
        api_key: str | None,
        deployment: str | None,
        api_version: str = "2024-12-01-preview",
        timeout_seconds: int = 30,
    ) -> None:
        self._endpoint = (endpoint or "").rstrip("/")
        self._api_key = api_key or ""
        self._deployment = deployment or ""
        self._api_version = api_version
        self._timeout_seconds = timeout_seconds

    def is_configured(self) -> bool:
        return bool(self._endpoint and self._api_key and self._deployment)

    def classify(
        self,
        *,
        raw_text: str,
        has_pending_proposal: bool,
        request_items: list[dict[str, object]],
        thread_context: dict[str, object],
    ) -> ConversationIntent | None:
        if not self.is_configured():
            return None
        payload = {
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You are a unified context interpreter for a Korean Telegram shopping assistant.\n"
                        "Classify the user message into exactly one of the allowed decisions.\n\n"
                        "Decision guide:\n"
                        "- confirm: 사용자가 현재 추천 상품을 수락 (ㅇㅇ 담아줘, 네, 좋아, 진행해줘)\n"
                        "- reject: 추천 거절 (아니, 별로, 말고)\n"
                        "- next: 다른 후보 요청 (다른 거 보여줘, 다른 상품)\n"
                        "- cancel: 대화 취소 (취소, 그만)\n"
                        "- new_request: 새 상품을 장바구니에 담아달라는 요청 (XX 담아줘, XX 넣어줘)\n"
                        "- remove_request: 장바구니에서 상품을 빼달라는 요청 "
                        "(XX 빼줘, XX 제거해줘, XX 삭제해줘, XX 제외해줘, XX 장바구니에서 빼줘)\n\n"
                        "IMPORTANT: '빼줘', '제거해줘', '삭제해줘', '제외해줘'는 반드시 remove_request입니다. "
                        "'담아줘', '넣어줘'는 new_request입니다. 이 둘을 혼동하지 마세요.\n\n"
                        "rewritten_queries: decision이 new_request일 때만 대화 맥락을 반영한 검색 쿼리를 생성. "
                        "그 외 decision에는 빈 리스트를 반환.\n"
                        "conversation_summary: 이번 턴까지 포함한 대화 요약 (한국어, ≤120자)."
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "raw_text": raw_text,
                            "has_pending_proposal": has_pending_proposal,
                            "request_items": request_items,
                            "thread_context": thread_context,
                        },
                        ensure_ascii=False,
                        default=str,
                    ),
                },
            ],
            "response_format": _conversation_classification_json_schema(),
        }
        response = self._post(payload)
        raw_content = _read_message_content(response)
        classification = ConversationClassification.model_validate_json(raw_content)
        return ConversationIntent(
            decision=classification.decision,
            reason=classification.reason,
            rewritten_queries=[
                AgentSearchQuery(item_name=q.item_name, query=q.query)
                for q in classification.rewritten_queries
                if q.item_name.strip() and q.query.strip()
            ],
            conversation_summary=classification.conversation_summary,
        )

    def summarize_conversation(
        self,
        *,
        previous_summary: str,
        current_run: dict[str, object],
    ) -> str:
        """Compress conversation history into a single summary for the next turn."""
        if not self.is_configured():
            return _deterministic_conversation_summary(previous_summary, current_run)
        payload = {
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You compress shopping conversation history into a single compact Korean summary "
                        "(≤200 chars). Return strict JSON with one key: conversation_summary. "
                        "Include: what was requested, what was proposed, user decisions, and current status. "
                        "Do not include prose outside JSON."
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "previous_summary": previous_summary,
                            "current_run": current_run,
                        },
                        ensure_ascii=False,
                        default=str,
                    ),
                },
            ],
            "response_format": {"type": "json_object"},
        }
        try:
            response = self._post(payload)
            parsed = json.loads(_read_message_content(response))
            summary = str(parsed.get("conversation_summary", "")).strip()
            return summary if summary else _deterministic_conversation_summary(previous_summary, current_run)
        except Exception:
            return _deterministic_conversation_summary(previous_summary, current_run)

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


def _deterministic_conversation_summary(previous_summary: str, current_run: dict[str, object]) -> str:
    raw_text = str(current_run.get("raw_text", "")).strip()
    status = str(current_run.get("conversation_status", ""))
    decision = current_run.get("user_decision") or ""
    parts: list[str] = []
    if previous_summary:
        parts.append(previous_summary.rstrip("。."))
    turn_desc = raw_text[:40] if raw_text else ""
    if decision and decision != "new_request":
        turn_desc = f"{turn_desc}→{decision}" if turn_desc else decision
    if status:
        turn_desc = f"{turn_desc}({status})" if turn_desc else status
    if turn_desc:
        parts.append(turn_desc)
    summary = " → ".join(parts)
    return summary[:200] if summary else ""
