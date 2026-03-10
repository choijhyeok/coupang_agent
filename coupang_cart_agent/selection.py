from __future__ import annotations

from dataclasses import replace
from math import isclose, log10
from statistics import median

from .contracts import ProductCandidate, RequestedItem, SelectedProduct, ShoppingRequest


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


def score_candidate(candidate: ProductCandidate, *, median_price_krw: float) -> float:
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

    return round(
        rating_score
        + review_score
        + price_score
        - low_rating_penalty
        - low_review_penalty
        - suspiciously_cheap_penalty,
        4,
    )


def summarize_selection_reason(
    candidate: ProductCandidate,
    *,
    score: float,
    median_price_krw: float,
) -> str:
    price_ratio = candidate.price_krw / median_price_krw if median_price_krw else 1.0
    if price_ratio <= 0.95:
        price_note = "below the candidate median price"
    elif price_ratio >= 1.10:
        price_note = "above the candidate median price"
    else:
        price_note = "near the candidate median price"

    return (
        f"Selected for balanced quality: rating {candidate.rating:.1f}/5, "
        f"{candidate.review_count:,} reviews, {candidate.price_krw:,} KRW "
        f"({price_note}), score {score:.2f}."
    )


def select_best_product(
    requested_item: RequestedItem,
    candidates: list[ProductCandidate],
) -> SelectedProduct:
    if len(candidates) < 3:
        raise ValueError("At least 3 candidates are required for reliable product selection.")

    normalized_candidates = [normalize_candidate(candidate) for candidate in candidates]
    median_price_krw = float(median(candidate.price_krw for candidate in normalized_candidates))

    scored_candidates = [
        (
            score_candidate(candidate, median_price_krw=median_price_krw),
            candidate,
        )
        for candidate in normalized_candidates
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
            best_candidate,
            score=best_score,
            median_price_krw=median_price_krw,
        ),
        score=best_score,
    )


class HeuristicProductSelectionService:
    """Protocol-compatible pure selector for product candidates."""

    def select_products(
        self,
        request: ShoppingRequest,
        candidates_by_item: dict[str, list[ProductCandidate]],
    ) -> list[SelectedProduct]:
        selections: list[SelectedProduct] = []
        for item in request.items:
            candidates = candidates_by_item.get(item.name, [])
            selections.append(select_best_product(item, candidates))

        return selections
