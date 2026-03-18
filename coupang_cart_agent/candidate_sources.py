from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .contracts import (
    BrowserAgentAction,
    BrowserAgentActionType,
    BrowserObservation,
    ObservedProduct,
    ProductCandidate,
    RequestedItem,
    ShoppingRequest,
    build_requested_item_search_query,
)


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

    source_mode = "demo"

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
    source_mode: str = "debug_fixture"

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
    source_mode: str = "debug_search_endpoint"

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


@dataclass(slots=True)
class LiveBrowserDiscoveryCandidateSource:
    """Primary live candidate source backed by the attached browser session."""

    driver: Any
    max_candidates_per_item: int = 5
    source_mode: str = "live_browser"

    def __call__(self, request: ShoppingRequest) -> dict[str, list[ProductCandidate]]:
        return self.load_candidates(request, search_queries_by_item=None)

    def load_candidates(
        self,
        request: ShoppingRequest,
        *,
        search_queries_by_item: Mapping[str, str] | None,
    ) -> dict[str, list[ProductCandidate]]:
        self.driver.attach_to_logged_in_session(None)
        self.driver.assert_logged_in()
        discovered: dict[str, list[ProductCandidate]] = {}
        for index, item in enumerate(request.items, start=1):
            query = str((search_queries_by_item or {}).get(item.name) or build_requested_item_search_query(item)).strip()
            self.driver.execute_action(
                BrowserAgentAction(
                    action_type=BrowserAgentActionType.SEARCH,
                    query=query,
                    reasoning_summary="Discover live proposal candidates from the attached browser session.",
                )
            )
            observation = self.driver.observe(
                step_index=index,
                last_action_summary=f"search:{query}",
            )
            candidates = _candidates_from_observation(
                observation=observation,
                fallback_query=query,
                max_candidates=self.max_candidates_per_item,
            )
            if not candidates:
                raise RuntimeError(
                    f"live browser candidate discovery returned no usable products for {item.name!r}"
                )
            discovered[item.name] = [
                ProductCandidate(
                    product_id=candidate.product_id,
                    name=candidate.name,
                    price_krw=candidate.price_krw,
                    rating=candidate.rating,
                    review_count=candidate.review_count,
                    product_url=candidate.product_url,
                    image_url=candidate.image_url,
                    vendor=candidate.vendor,
                    badges=list(candidate.badges),
                )
                for candidate in candidates
            ]
        return discovered


def _candidates_from_observation(
    *,
    observation: BrowserObservation,
    fallback_query: str,
    max_candidates: int,
) -> list[ProductCandidate]:
    ranked = sorted(
        [product for product in observation.observed_products if product.name.strip() and not product.sold_out],
        key=lambda product: (
            _text_match_score(product, fallback_query),
            _rating_from_text(product.rating_text),
            _review_count_from_text(product.review_count_text),
            -_price_from_text(product.price_text),
        ),
        reverse=True,
    )
    return [
        ProductCandidate(
            product_id=_product_id_from_href(product.href, fallback_name=product.name),
            name=product.name.strip(),
            price_krw=max(1, _price_from_text(product.price_text)),
            rating=max(0.0, min(5.0, _rating_from_text(product.rating_text))),
            review_count=max(0, _review_count_from_text(product.review_count_text)),
            product_url=(product.href or observation.url or f"https://www.coupang.com/np/search?q={urllib.parse.quote(fallback_query)}"),
            image_url=None,
            vendor="Coupang",
            badges=list(product.badges),
        )
        for product in ranked[:max(1, max_candidates)]
    ]


def _price_from_text(value: str | None) -> int:
    if not value:
        return 0
    digits = "".join(character for character in value if character.isdigit())
    return int(digits) if digits else 0


def _rating_from_text(value: str | None) -> float:
    if not value:
        return 0.0
    filtered = "".join(character for character in value if character.isdigit() or character == ".")
    try:
        return float(filtered) if filtered else 0.0
    except ValueError:
        return 0.0


def _review_count_from_text(value: str | None) -> int:
    if not value:
        return 0
    digits = "".join(character for character in value if character.isdigit())
    return int(digits) if digits else 0


def _product_id_from_href(href: str | None, *, fallback_name: str) -> str:
    if href:
        parsed = urllib.parse.urlparse(href)
        parts = [segment for segment in parsed.path.split("/") if segment]
        if "products" in parts:
            index = parts.index("products")
            if index + 1 < len(parts):
                return parts[index + 1]
    slug = "-".join(token for token in fallback_name.lower().split() if token)
    return slug or "observed-product"


def _text_match_score(product: ObservedProduct, query: str) -> int:
    lowered_name = product.name.lower()
    tokens = [token for token in urllib.parse.unquote(query).lower().split() if token]
    return sum(10 if token in lowered_name else 0 for token in tokens)
