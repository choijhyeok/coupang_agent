from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from typing import Protocol

from .contracts import (
    BrowserObservation,
    CartAddFailureReason,
    ObservedCartItem,
    RequestedItem,
    SelectedProduct,
)


@dataclass(slots=True)
class CartVerificationDecision:
    success: bool
    failure_reason: CartAddFailureReason | None
    reason: str
    matched_item_name: str | None = None
    evidence: dict[str, object] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.evidence is None:
            self.evidence = {}


class CartVerificationModel(Protocol):
    def verify(
        self,
        *,
        selection: SelectedProduct,
        observation: BrowserObservation,
        cart_count_before: int | None,
        cart_count_after: int | None,
    ) -> CartVerificationDecision: ...


class DeterministicCartVerifier:
    """Selector-drift-tolerant verifier that checks the observed cart state semantically."""

    def verify(
        self,
        *,
        selection: SelectedProduct,
        observation: BrowserObservation,
        cart_count_before: int | None,
        cart_count_after: int | None,
    ) -> CartVerificationDecision:
        requested_tokens = _semantic_tokens(selection.request_item_name)
        candidate_tokens = _semantic_tokens(selection.candidate.name)
        expected_tokens = requested_tokens | candidate_tokens
        expected_quantity = max(1, selection.quantity)
        cart_items = observation.cart_items
        extraction_mode = "structured_cart_items" if cart_items else "text_fallback"

        best_item: ObservedCartItem | None = None
        best_score = -1
        best_requested_overlap = 0
        best_candidate_overlap = 0
        quantity_match = False
        for item in cart_items:
            score = _cart_item_match_score(item, requested_tokens=requested_tokens, candidate_tokens=candidate_tokens)
            if score > best_score:
                best_score = score
                best_item = item
                best_requested_overlap, best_candidate_overlap = _cart_item_overlap(
                    item,
                    requested_tokens=requested_tokens,
                    candidate_tokens=candidate_tokens,
                )
                quantity_match = item.quantity in (None, expected_quantity)

        if (
            best_item is not None
            and quantity_match
            and _structured_item_is_match(
                requested_tokens=requested_tokens,
                candidate_tokens=candidate_tokens,
                requested_overlap=best_requested_overlap,
                candidate_overlap=best_candidate_overlap,
            )
        ):
            return CartVerificationDecision(
                success=True,
                failure_reason=None,
                reason="장바구니 내용이 요청한 상품과 일치하는 것으로 확인되었습니다.",
                matched_item_name=best_item.name,
                evidence={
                    "verification_method": "deterministic",
                    "extraction_mode": extraction_mode,
                    "match_score": best_score,
                    "requested_overlap": best_requested_overlap,
                    "candidate_overlap": best_candidate_overlap,
                    "matched_item": asdict(best_item),
                    "cart_observation": _observation_evidence(observation),
                },
            )

        fallback_text = _combine_observation_text(observation)
        text_requested_overlap, text_candidate_overlap = _text_overlap(
            fallback_text,
            requested_tokens=requested_tokens,
            candidate_tokens=candidate_tokens,
        )
        if _text_semantic_match(
            requested_tokens=requested_tokens,
            candidate_tokens=candidate_tokens,
            requested_overlap=text_requested_overlap,
            candidate_overlap=text_candidate_overlap,
        ):
            quantity_text = _extract_quantity_text(fallback_text)
            quantity_ok = quantity_text is None or quantity_text == expected_quantity
            if quantity_ok and _cart_count_progressed(cart_count_before, cart_count_after):
                return CartVerificationDecision(
                    success=True,
                    failure_reason=None,
                    reason="장바구니 페이지 텍스트와 화면 증거가 요청한 상품과 일치합니다.",
                    matched_item_name=selection.candidate.name,
                    evidence={
                        "verification_method": "deterministic",
                        "extraction_mode": "text_fallback",
                        "requested_overlap": text_requested_overlap,
                        "candidate_overlap": text_candidate_overlap,
                        "matched_item": {"name": selection.candidate.name, "quantity": quantity_text},
                        "cart_observation": _observation_evidence(observation),
                    },
                )

        if best_item is not None and best_score >= 1:
            return CartVerificationDecision(
                success=False,
                failure_reason=CartAddFailureReason.VERIFICATION_MISMATCH,
                reason=(
                    f"장바구니에서 다른 상품이 확인되었습니다: {best_item.name} "
                    f"(기대 상품: {selection.request_item_name})."
                ),
                matched_item_name=best_item.name,
                evidence={
                    "verification_method": "deterministic",
                    "extraction_mode": extraction_mode,
                    "match_score": best_score,
                    "requested_overlap": best_requested_overlap,
                    "candidate_overlap": best_candidate_overlap,
                    "matched_item": asdict(best_item),
                    "cart_observation": _observation_evidence(observation),
                },
            )

        return CartVerificationDecision(
            success=False,
            failure_reason=CartAddFailureReason.MANUAL_REVIEW_REQUIRED,
            reason=(
                "장바구니에 요청한 상품이 담겼는지 확정할 증거가 부족합니다. "
                "오탐 성공 처리 대신 확인 필요 상태로 반환합니다."
            ),
            evidence={
                "verification_method": "deterministic",
                "extraction_mode": extraction_mode,
                "cart_observation": _observation_evidence(observation),
            },
        )


class AzureOpenAICartVerifier:
    """AOAI-backed multimodal verifier with deterministic fallback."""

    def __init__(
        self,
        *,
        endpoint: str | None,
        api_key: str | None,
        deployment: str | None,
        api_version: str = "2024-12-01-preview",
        timeout_seconds: int = 30,
        fallback: CartVerificationModel | None = None,
    ) -> None:
        self._endpoint = (endpoint or "").rstrip("/")
        self._api_key = api_key or ""
        self._deployment = deployment or ""
        self._api_version = api_version
        self._timeout_seconds = timeout_seconds
        self._fallback = fallback or DeterministicCartVerifier()

    def is_configured(self) -> bool:
        return bool(self._endpoint and self._api_key and self._deployment)

    def verify(
        self,
        *,
        selection: SelectedProduct,
        observation: BrowserObservation,
        cart_count_before: int | None,
        cart_count_after: int | None,
    ) -> CartVerificationDecision:
        fallback = self._fallback.verify(
            selection=selection,
            observation=observation,
            cart_count_before=cart_count_before,
            cart_count_after=cart_count_after,
        )
        if fallback.success:
            fallback.evidence.setdefault("verification_tier", "deterministic_fast_path")
            fallback.evidence.setdefault("aoai_status", "skipped_fast_path_success")
            return fallback
        if fallback.failure_reason == CartAddFailureReason.VERIFICATION_MISMATCH:
            fallback.evidence.setdefault("verification_tier", "deterministic_fast_path")
            fallback.evidence.setdefault("aoai_status", "skipped_fast_path_mismatch")
            return fallback
        if not self.is_configured():
            fallback.evidence.setdefault("aoai_status", "not_configured")
            return fallback

        payload = {
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You verify whether a Coupang add-to-cart action actually placed the requested item in cart. "
                        "Use the cart screenshot, accessibility-style lines, extracted cart items, and HTML/body text together. "
                        "Return strict JSON with keys: verdict, failure_reason, reason, matched_item_name. "
                        "Allowed verdict values: verified, mismatch, review_needed. "
                        "Allowed failure_reason values: verification_mismatch, manual_review_required, unknown."
                    ),
                },
                _build_verification_user_message(
                    selection=selection,
                    observation=observation,
                    cart_count_before=cart_count_before,
                    cart_count_after=cart_count_after,
                ),
            ],
            "response_format": {"type": "json_object"},
        }
        try:
            started = time.perf_counter()
            response = self._post(payload)
            elapsed_ms = round((time.perf_counter() - started) * 1000.0, 2)
            content = _read_message_content(response)
            parsed = json.loads(content)
            verdict = str(parsed.get("verdict", "review_needed")).strip().lower()
            matched_item_name = _optional_text(parsed.get("matched_item_name"))
            reason = _optional_text(parsed.get("reason")) or fallback.reason
            if verdict == "verified":
                return CartVerificationDecision(
                    success=True,
                    failure_reason=None,
                    reason=reason,
                    matched_item_name=matched_item_name,
                    evidence={
                        **fallback.evidence,
                        "verification_method": "azure_openai",
                        "aoai_verdict": verdict,
                        "verification_tier": "aoai_fallback",
                        "aoai_status": "used_review_needed_fallback",
                        "aoai_latency_ms": elapsed_ms,
                    },
                )
            failure_reason = CartAddFailureReason.MANUAL_REVIEW_REQUIRED
            if verdict == "mismatch":
                failure_reason = CartAddFailureReason.VERIFICATION_MISMATCH
            elif parsed.get("failure_reason") not in (None, ""):
                try:
                    failure_reason = CartAddFailureReason(str(parsed["failure_reason"]))
                except ValueError:
                    failure_reason = CartAddFailureReason.MANUAL_REVIEW_REQUIRED
            return CartVerificationDecision(
                success=False,
                failure_reason=failure_reason,
                reason=reason,
                matched_item_name=matched_item_name,
                evidence={
                    **fallback.evidence,
                    "verification_method": "azure_openai",
                    "aoai_verdict": verdict,
                    "verification_tier": "aoai_fallback",
                    "aoai_status": "used_review_needed_fallback",
                    "aoai_latency_ms": elapsed_ms,
                },
            )
        except Exception as exc:
            fallback.evidence.setdefault("aoai_error", str(exc))
            fallback.evidence.setdefault("verification_tier", "deterministic_fallback_after_aoai_error")
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


def _build_verification_user_message(
    *,
    selection: SelectedProduct,
    observation: BrowserObservation,
    cart_count_before: int | None,
    cart_count_after: int | None,
) -> dict[str, object]:
    prompt_payload = {
        "selection": {
            "request_item_name": selection.request_item_name,
            "candidate_name": selection.candidate.name,
            "quantity": selection.quantity,
            "option_hints": selection.option_hints,
        },
        "cart_count_before": cart_count_before,
        "cart_count_after": cart_count_after,
        "cart_observation": _observation_evidence(observation),
    }
    if observation.screenshot_base64:
        return {
            "role": "user",
            "content": [
                {"type": "text", "text": json.dumps(prompt_payload, ensure_ascii=False, default=str)},
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/png;base64,{observation.screenshot_base64}"},
                },
            ],
        }
    return {
        "role": "user",
        "content": json.dumps(prompt_payload, ensure_ascii=False, default=str),
    }


def _observation_evidence(observation: BrowserObservation) -> dict[str, object]:
    return {
        "url": observation.url,
        "title": observation.title,
        "page_kind": observation.page_kind,
        "accessibility_lines": list(observation.accessibility_lines),
        "html_excerpt": observation.html_excerpt,
        "body_text_excerpt": observation.body_text_excerpt,
        "cart_items": [asdict(item) for item in observation.cart_items],
        "screenshot_path": observation.screenshot_path,
        "has_screenshot": bool(observation.screenshot_base64),
    }


def _semantic_tokens(*values: str) -> set[str]:
    tokens: set[str] = set()
    stop_tokens = {"쿠팡", "로켓", "배송", "무료", "국내산", "상품", "추천"}
    for value in values:
        normalized = re.sub(r"\s+", " ", value.lower())
        for token in re.split(r"[^0-9a-z가-힣]+", normalized):
            if len(token) < 2:
                continue
            if token in stop_tokens:
                continue
            tokens.add(token)
    return tokens


def _cart_item_match_score(
    item: ObservedCartItem,
    *,
    requested_tokens: set[str],
    candidate_tokens: set[str],
) -> int:
    requested_overlap, candidate_overlap = _cart_item_overlap(
        item,
        requested_tokens=requested_tokens,
        candidate_tokens=candidate_tokens,
    )
    return (requested_overlap * 3) + candidate_overlap


def _cart_item_overlap(
    item: ObservedCartItem,
    *,
    requested_tokens: set[str],
    candidate_tokens: set[str],
) -> tuple[int, int]:
    item_tokens = _semantic_tokens(
        item.name,
        item.option_summary or "",
        item.package_summary or "",
    )
    return len(requested_tokens & item_tokens), len(candidate_tokens & item_tokens)


def _combine_observation_text(observation: BrowserObservation) -> str:
    parts = [
        observation.body_text_excerpt,
        observation.html_excerpt or "",
        " ".join(observation.accessibility_lines),
    ]
    for item in observation.cart_items:
        parts.append(item.name)
        if item.option_summary:
            parts.append(item.option_summary)
        if item.package_summary:
            parts.append(item.package_summary)
        if item.quantity_text:
            parts.append(item.quantity_text)
    return " ".join(part for part in parts if part)


def _text_semantic_match(
    *,
    requested_tokens: set[str],
    candidate_tokens: set[str],
    requested_overlap: int,
    candidate_overlap: int,
) -> bool:
    return _structured_item_is_match(
        requested_tokens=requested_tokens,
        candidate_tokens=candidate_tokens,
        requested_overlap=requested_overlap,
        candidate_overlap=candidate_overlap,
    )


def _text_overlap(
    text: str,
    *,
    requested_tokens: set[str],
    candidate_tokens: set[str],
) -> tuple[int, int]:
    normalized_text = re.sub(r"\s+", " ", text.lower())
    requested_overlap = sum(1 for token in requested_tokens if token in normalized_text)
    candidate_overlap = sum(1 for token in candidate_tokens if token in normalized_text)
    return requested_overlap, candidate_overlap


def _structured_item_is_match(
    *,
    requested_tokens: set[str],
    candidate_tokens: set[str],
    requested_overlap: int,
    candidate_overlap: int,
) -> bool:
    if requested_overlap < _minimum_requested_overlap(requested_tokens):
        return False
    if candidate_overlap >= 1:
        return True
    return requested_overlap >= max(1, min(3, len(requested_tokens)))


def _minimum_requested_overlap(tokens: set[str]) -> int:
    if len(tokens) <= 2:
        return 1
    return 2


def _extract_quantity_text(text: str) -> int | None:
    patterns = (
        r"수량[^0-9]{0,10}(\d+)",
        r"수량변경[^0-9]{0,10}(\d+)",
        r"수량[^0-9]{0,6}[+-]?\s*(\d+)",
    )
    for pattern in patterns:
        match = re.search(pattern, text)
        if match is not None:
            return max(1, int(match.group(1)))
    return None


def _cart_count_progressed(before: int | None, after: int | None) -> bool:
    if before is None or after is None:
        return True
    return after >= before


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


def _optional_text(value: object) -> str | None:
    if value in (None, ""):
        return None
    text = str(value).strip()
    return text or None
