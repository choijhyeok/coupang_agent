from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .contracts import ProductCandidate, RequestedItem, ShoppingRequest, build_requested_item_search_query


def _read_first(raw: Mapping[str, object], *keys: str, default: object = None) -> object:
    for key in keys:
        if key in raw and raw[key] not in (None, ""):
            return raw[key]
    return default


def _coerce_int(value: object, *, default: int = 0) -> int:
    if value in (None, ""):
        return default
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return int(value)
    digits = "".join(character for character in str(value) if character.isdigit() or character == "-")
    return int(digits) if digits not in ("", "-") else default


def _coerce_float(value: object, *, default: float = 0.0) -> float:
    if value in (None, ""):
        return default
    if isinstance(value, (int, float)):
        return float(value)
    filtered = "".join(character for character in str(value) if character.isdigit() or character in ".-")
    return float(filtered) if filtered not in ("", "-", ".", "-.") else default


def _coerce_badges(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    return []


def product_candidate_from_record(raw: ProductCandidate | Mapping[str, object]) -> ProductCandidate:
    """Normalize collector records and captured fixtures into the shared candidate contract."""

    if isinstance(raw, ProductCandidate):
        return raw

    product_id = str(
        _read_first(
            raw,
            "product_id",
            "productId",
            "id",
            "itemId",
            default="",
        )
    ).strip()
    if not product_id:
        raise ValueError("candidate record is missing a product id")

    name = str(
        _read_first(
            raw,
            "name",
            "productName",
            "title",
            "itemName",
            default="",
        )
    ).strip()
    if not name:
        raise ValueError(f"candidate record {product_id} is missing a product name")

    product_url = str(
        _read_first(
            raw,
            "product_url",
            "productUrl",
            "url",
            "productLink",
            default=f"https://www.coupang.com/vp/products/{product_id}",
        )
    ).strip()

    return ProductCandidate(
        product_id=product_id,
        name=name,
        price_krw=max(
            1,
            _coerce_int(
                _read_first(raw, "price_krw", "price", "salePrice", "salesPrice", "finalPrice", default=0)
            ),
        ),
        rating=max(
            0.0,
            min(
                5.0,
                _coerce_float(_read_first(raw, "rating", "reviewRating", "ratingAverage", default=0.0)),
            ),
        ),
        review_count=max(
            0,
            _coerce_int(_read_first(raw, "review_count", "reviewCount", "ratingCount", default=0)),
        ),
        product_url=product_url,
        image_url=(
            str(
                _read_first(
                    raw,
                    "image_url",
                    "imageUrl",
                    "thumbnail",
                    "thumbnailUrl",
                    "image",
                    default="",
                )
            ).strip()
            or None
        ),
        vendor=str(_read_first(raw, "vendor", "vendorName", "sellerName", default="")).strip() or None,
        badges=_coerce_badges(_read_first(raw, "badges", "badgeNames", default=[])),
    )


def product_candidates_from_records(records: list[ProductCandidate | Mapping[str, object]]) -> list[ProductCandidate]:
    return [product_candidate_from_record(record) for record in records]


@dataclass(slots=True)
class DemoCandidateSource:
    """Deterministic candidate source for local demos."""

    def __call__(self, request: ShoppingRequest) -> dict[str, list[ProductCandidate]]:
        candidates_by_item: dict[str, list[ProductCandidate]] = {}
        for index, item in enumerate(request.items, start=1):
            candidates_by_item[item.name] = [
                ProductCandidate(
                    product_id=f"{index}-cheap",
                    name=f"{item.name} 보급형",
                price_krw=5900,
                rating=3.8,
                review_count=19,
                product_url=f"https://www.coupang.com/vp/products/{index}-cheap",
                image_url=f"https://images.example.com/{index}-cheap.jpg",
            ),
            ProductCandidate(
                product_id=f"{index}-balanced",
                name=f"{item.name} 추천",
                price_krw=8900,
                rating=4.8,
                review_count=1800,
                product_url=f"https://www.coupang.com/vp/products/{index}-balanced",
                image_url=f"https://images.example.com/{index}-balanced.jpg",
            ),
            ProductCandidate(
                product_id=f"{index}-premium",
                name=f"{item.name} 프리미엄",
                price_krw=11900,
                rating=4.9,
                review_count=900,
                product_url=f"https://www.coupang.com/vp/products/{index}-premium",
                image_url=f"https://images.example.com/{index}-premium.jpg",
            ),
        ]
        return candidates_by_item


@dataclass(slots=True)
class CapturedCoupangFixtureCandidateSource:
    """Production-shaped source backed by captured collector output stored in the repo."""

    fixture_path: str

    def __call__(self, request: ShoppingRequest) -> dict[str, list[ProductCandidate]]:
        payload = json.loads(Path(self.fixture_path).read_text(encoding="utf-8"))
        records = payload.get("products", [])
        normalized_records = product_candidates_from_records(records)
        if len(normalized_records) < 1:
            raise ValueError("captured candidate fixture did not contain any products")
        return {
            item.name: self._candidates_for_item(item, normalized_records)
            for item in request.items
        }

    @staticmethod
    def _candidates_for_item(
        item: RequestedItem,
        records: list[ProductCandidate],
    ) -> list[ProductCandidate]:
        return [
            ProductCandidate(
                product_id=f"{candidate.product_id}:{item.name}",
                name=candidate.name,
                price_krw=candidate.price_krw,
                rating=candidate.rating,
                review_count=candidate.review_count,
                product_url=candidate.product_url,
                image_url=candidate.image_url,
                vendor=candidate.vendor,
                badges=list(candidate.badges),
            )
            for candidate in records
        ]


@dataclass(slots=True)
class LiveCoupangSearchCandidateSource:
    """Equivalent live adapter for Coupang search pages.

    The parser expects a JSON fixture-like response body with a top-level `products`
    list. In production this can be fed by Scrapling or any other collector that
    returns the same record shape.
    """

    search_endpoint: str
    timeout_seconds: int = 15

    def __call__(self, request: ShoppingRequest) -> dict[str, list[ProductCandidate]]:
        return {item.name: self.search(build_requested_item_search_query(item)) for item in request.items}

    def search(self, query: str) -> list[ProductCandidate]:
        url = self._build_url(query)
        http_request = urllib.request.Request(
            url,
            headers={
                "User-Agent": "Mozilla/5.0",
                "Accept": "application/json,text/html;q=0.9,*/*;q=0.8",
            },
        )
        try:
            with urllib.request.urlopen(http_request, timeout=self.timeout_seconds) as response:
                body = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(
                f"live candidate fetch failed with HTTP {exc.code} for {url}: {detail[:180]}"
            ) from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"live candidate fetch failed for {url}: {exc.reason}") from exc

        payload = json.loads(body)
        products = payload.get("products", [])
        candidates = product_candidates_from_records(products)
        if len(candidates) < 3:
            raise ValueError(f"live candidate source returned only {len(candidates)} candidates for {query!r}")
        return candidates

    def _build_url(self, query: str) -> str:
        separator = "&" if "?" in self.search_endpoint else "?"
        return f"{self.search_endpoint}{separator}q={urllib.parse.quote(query)}"
