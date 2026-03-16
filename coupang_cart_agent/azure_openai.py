from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field

from .contracts import PriorPurchaseRecord, SessionSelectionSignal, ShoppingRequest, build_requested_item_search_query


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
