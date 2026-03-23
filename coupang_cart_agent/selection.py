from __future__ import annotations

import re
from dataclasses import dataclass
from dataclasses import replace
from math import isclose, log10
from statistics import median

from .contracts import (
    ProductCandidate,
    RequestedItem,
    SelectedProduct,
    SelectionContext,
    ShoppingRequest,
    canonicalize_size_token,
)
from .services import SelectionContextStore


_CANDIDATE_PACK_PATTERN = re.compile(
    r"(?P<count>\d+)\s*(?P<unit>개입|개|병|봉|팩|캔|세트|입|박스|줄|통)\b"
)


@dataclass(slots=True)
class ConstraintMatch:
    compliant: bool
    mismatches: list[str]


def normalize_candidate(candidate: ProductCandidate) -> ProductCandidate:
    """Clamp malformed candidate values into a deterministic scoring shape."""

    return replace(
        candidate,
        name=candidate.name.strip(),
        price_krw=max(1, candidate.price_krw),
        rating=min(max(candidate.rating, 0.0), 5.0),
        review_count=max(0, candidate.review_count),
        product_url=candidate.product_url.strip(),
        vendor=candidate.vendor.strip() if candidate.vendor else None,
        badges=[badge.strip() for badge in candidate.badges if badge.strip()],
    )


def score_candidate(
    candidate: ProductCandidate,
    *,
    median_price_krw: float,
    context: SelectionContext | None = None,
) -> float:
    """Balance quality, confidence, and price without blindly picking the cheapest item."""

    rating_score = (candidate.rating / 5.0) * 60.0
    review_score = min(log10(candidate.review_count + 1) / 4.0, 1.0) * 25.0

    price_ratio = candidate.price_krw / median_price_krw if median_price_krw else 1.0
    price_score = max(-18.0, min(12.0, (1.0 - price_ratio) * 35.0))

    low_rating_penalty = max(0.0, (4.2 - candidate.rating) * 18.0)
    low_review_penalty = 0.0
    if candidate.review_count < 30:
        low_review_penalty = 8.0
    elif candidate.review_count < 100:
        low_review_penalty = 4.0

    suspiciously_cheap_penalty = 0.0
    if price_ratio < 0.72 and (candidate.rating < 4.3 or candidate.review_count < 200):
        suspiciously_cheap_penalty = 10.0

    context_adjustment = 0.0
    if context is not None:
        prior_purchase = next(
            (record for record in context.prior_purchases if record.product_id == candidate.product_id),
            None,
        )
        if prior_purchase is not None:
            context_adjustment += min(prior_purchase.purchase_count, 3) * 1.5
            if prior_purchase.satisfaction_rating is not None:
                context_adjustment += (prior_purchase.satisfaction_rating - 3.0) * 2.0

        for signal in context.recent_session_signals:
            if signal.product_id != candidate.product_id:
                continue
            normalized_signal = signal.signal.strip().lower()
            if normalized_signal in {"preferred", "repeat", "liked"}:
                context_adjustment += 3.0
            elif normalized_signal in {"avoid", "rejected", "disliked"}:
                context_adjustment -= 6.0

    base_score = (
        rating_score
        + review_score
        + price_score
        - low_rating_penalty
        - low_review_penalty
        - suspiciously_cheap_penalty
    )
    return round(base_score + context_adjustment, 4)


def summarize_selection_reason(
    requested_item: RequestedItem,
    candidate: ProductCandidate,
    *,
    score: float,
    median_price_krw: float,
    context: SelectionContext | None = None,
) -> str:
    price_ratio = candidate.price_krw / median_price_krw if median_price_krw else 1.0
    if price_ratio <= 0.95:
        price_note = "후보 중간가보다 저렴함"
    elif price_ratio >= 1.10:
        price_note = "후보 중간가보다 비쌈"
    else:
        price_note = "후보 중간가와 비슷함"

    context_fragments: list[str] = []
    if context is not None:
        prior_purchase = next(
            (record for record in context.prior_purchases if record.product_id == candidate.product_id),
            None,
        )
        if prior_purchase is not None:
            context_fragments.append(f"이전 구매 이력 {prior_purchase.purchase_count}회")

        for signal in context.recent_session_signals:
            if signal.product_id != candidate.product_id:
                continue
            normalized_signal = signal.signal.strip().lower()
            if normalized_signal in {"preferred", "repeat", "liked"}:
                context_fragments.append("최근 대화에서 선호 신호가 있었음")
            elif normalized_signal in {"avoid", "rejected", "disliked"}:
                context_fragments.append("최근 대화에서 비선호 신호가 있었음")

    summary = (
        f"평점 {candidate.rating:.1f}/5, 리뷰 {candidate.review_count:,}개, "
        f"가격 {candidate.price_krw:,}원을 기준으로 품질과 가격의 균형이 좋아 추천했습니다 "
        f"({price_note}, 점수 {score:.2f})."
    )
    explicit_constraints = _format_explicit_constraints(requested_item)
    if explicit_constraints:
        summary += f" 요청 조건 반영: {explicit_constraints}."
    if context_fragments:
        summary += " 참고 맥락: " + ", ".join(context_fragments) + "."
    return summary


def select_best_product(
    requested_item: RequestedItem,
    candidates: list[ProductCandidate],
    *,
    context: SelectionContext | None = None,
) -> SelectedProduct:
    if len(candidates) < 3:
        raise ValueError("At least 3 candidates are required for reliable product selection.")

    normalized_candidates = [normalize_candidate(candidate) for candidate in candidates]
    compliant_candidates: list[ProductCandidate] = []
    mismatch_summaries: list[str] = []
    for candidate in normalized_candidates:
        match = _match_explicit_constraints(requested_item, candidate)
        if match.compliant:
            compliant_candidates.append(candidate)
            continue
        mismatch_summaries.append(f"{candidate.name}: {', '.join(match.mismatches)}")

    if not compliant_candidates:
        raise ValueError(
            "No candidates satisfied the explicit request constraints. "
            + "; ".join(mismatch_summaries[:3])
        )

    median_price_krw = float(median(candidate.price_krw for candidate in compliant_candidates))

    scored_candidates = [
        (
            score_candidate(candidate, median_price_krw=median_price_krw, context=context),
            candidate,
        )
        for candidate in compliant_candidates
    ]
    scored_candidates.sort(
        key=lambda item: (
            item[0],
            item[1].rating,
            item[1].review_count,
            -item[1].price_krw,
        ),
        reverse=True,
    )

    best_score, best_candidate = scored_candidates[0]
    if len(scored_candidates) > 1 and isclose(best_score, scored_candidates[1][0], abs_tol=0.01):
        tied_candidates = [item for item in scored_candidates if isclose(item[0], best_score, abs_tol=0.01)]
        best_score, best_candidate = max(
            tied_candidates,
            key=lambda item: (item[1].rating, item[1].review_count, -item[1].price_krw),
        )

    return SelectedProduct(
        request_item_name=requested_item.name,
        candidate=best_candidate,
        quantity=requested_item.quantity,
        selection_reason=summarize_selection_reason(
            requested_item,
            best_candidate,
            score=best_score,
            median_price_krw=median_price_krw,
            context=context,
        ),
        score=best_score,
    )


class HeuristicProductSelectionService:
    """Protocol-compatible pure selector for product candidates."""

    def __init__(self, *, context_store: SelectionContextStore | None = None) -> None:
        self._context_store = context_store

    def select_products(
        self,
        request: ShoppingRequest,
        candidates_by_item: dict[str, list[ProductCandidate]],
    ) -> list[SelectedProduct]:
        context = self._context_store.load(request) if self._context_store is not None else None
        selections: list[SelectedProduct] = []
        for item in request.items:
            candidates = candidates_by_item.get(item.name, [])
            selections.append(select_best_product(item, candidates, context=context))

        return selections


def _match_explicit_constraints(requested_item: RequestedItem, candidate: ProductCandidate) -> ConstraintMatch:
    mismatches: list[str] = []
    candidate_name = _normalize_match_text(candidate.name)

    if requested_item.explicit_brand and _normalize_match_text(requested_item.explicit_brand) not in candidate_name:
        mismatches.append(f"brand mismatch for {requested_item.explicit_brand}")

    requested_size = canonicalize_size_token(requested_item.explicit_unit_size)
    candidate_size = _extract_candidate_unit_size(candidate.name)
    if requested_size is not None:
        if candidate_size != requested_size:
            mismatches.append(f"unit-size mismatch for {requested_item.explicit_unit_size}")

    if requested_item.explicit_pack_count is not None:
        candidate_pack_count, candidate_pack_unit = _extract_candidate_pack(candidate.name)
        normalized_requested_unit = _normalize_pack_unit(requested_item.explicit_pack_unit)
        if candidate_pack_count != requested_item.explicit_pack_count:
            mismatches.append(
                f"pack mismatch for {requested_item.explicit_pack_count}{requested_item.explicit_pack_unit or ''}"
            )
        elif normalized_requested_unit is not None and candidate_pack_unit != normalized_requested_unit:
            mismatches.append(
                f"pack unit mismatch for {requested_item.explicit_pack_count}{requested_item.explicit_pack_unit}"
            )

    return ConstraintMatch(compliant=not mismatches, mismatches=mismatches)


def _extract_candidate_unit_size(name: str) -> str | None:
    return canonicalize_size_token(name)


def _extract_candidate_pack(name: str) -> tuple[int | None, str | None]:
    matches = list(_CANDIDATE_PACK_PATTERN.finditer(name))
    if not matches:
        x_match = re.search(r"[xX]\s*(?P<count>\d+)\b", name)
        if x_match is None:
            return None, None
        return int(x_match.group("count")), None
    match = matches[-1]
    return int(match.group("count")), _normalize_pack_unit(match.group("unit"))


def _normalize_pack_unit(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip().lower()
    if normalized == "개입":
        return "개"
    return normalized or None


def _normalize_match_text(value: str) -> str:
    return re.sub(r"[^0-9a-z가-힣]+", "", value.lower())


def _format_explicit_constraints(requested_item: RequestedItem) -> str:
    fragments: list[str] = []
    if requested_item.explicit_brand:
        fragments.append(f"brand {requested_item.explicit_brand}")
    if requested_item.explicit_unit_size:
        fragments.append(f"size {requested_item.explicit_unit_size}")
    if requested_item.explicit_pack_count is not None and requested_item.explicit_pack_unit:
        fragments.append(f"pack {requested_item.explicit_pack_count}{requested_item.explicit_pack_unit}")
    return ", ".join(fragments)
