from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

from scrapling.parser import Adaptor


_PRODUCT_LINK_SELECTOR = "a[href*='/vp/products/']"
_CART_TEXT_PATTERN = re.compile(r"(?:장바구니\s*담기|카트에\s*담기|장바구니|카트)", re.IGNORECASE)
_PRICE_PATTERN = re.compile(r"([0-9][0-9,]{2,})\s*원?")
_RATING_PATTERN = re.compile(r"([0-5](?:\.[0-9])?)")
_REVIEW_PATTERN = re.compile(r"(?:리뷰|후기|평점)?\s*\(?([0-9][0-9,]*)\)?")
_QUANTITY_PATTERN = re.compile(r"(?:수량|수량변경)[^0-9]{0,8}(\d+)|(?<![0-9])(\d+)\s*개")
_PURCHASE_RESTRICTED_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(r"로켓프레시.*(장바구니에 담을 수 없|구매가 불가능|주문할 수 없|와우회원만)", re.IGNORECASE),
        "rocket_fresh_restriction",
    ),
    (
        re.compile(r"(와우회원만|와우 회원만).*(구매|주문|장바구니)|구매하려면 와우회원", re.IGNORECASE),
        "wow_membership_restriction",
    ),
    (re.compile(r"(장바구니에 담을 수 없|구매가 불가능|판매가 중단|구매 제한)", re.IGNORECASE), "cart_unsupported"),
    (re.compile(r"(품절|일시품절|재입고 알림)"), "out_of_stock"),
)


class ScraplingObservationAdapter:
    """Scrapling-first extraction for search, detail, and cart observations."""

    def __init__(self, *, storage_path: str = ".artifacts/scrapling-observation.sqlite3") -> None:
        self._storage_path = storage_path
        Path(storage_path).parent.mkdir(parents=True, exist_ok=True)

    def extract(
        self,
        *,
        url: str,
        html: str,
        body_text: str,
        viewport_state: dict[str, object],
    ) -> tuple[dict[str, object], dict[str, object]]:
        adaptor = Adaptor(
            html,
            url=url,
            adaptive=True,
            storage_args={"storage_file": self._storage_path, "url": self._storage_key(url)},
        )
        is_product_page = "/vp/products/" in url
        is_cart_page = "cartview.pang" in url.lower()
        observed_products, search_link_hints = self._extract_products(adaptor=adaptor, url=url)
        cart_items = self._extract_cart_items(adaptor=adaptor, url=url) if is_cart_page else []
        selected_product_hint = (
            self._extract_selected_product_hint(adaptor=adaptor, url=url, body_text=body_text)
            if is_product_page
            else {}
        )
        available_options, option_hints = (
            self._extract_available_options(adaptor=adaptor) if is_product_page else ([], {})
        )
        expandable_sections, expandable_hints = self._extract_expandable_sections(adaptor=adaptor)
        add_to_cart_hint = self._extract_add_to_cart_hint(adaptor=adaptor)
        cta_viewport_state = self._best_viewport_cta(viewport_state)
        purchase_blocked_reason = self._purchase_blocked_reason(body_text, is_product_page=is_product_page)
        add_to_cart_available = add_to_cart_hint is not None
        add_to_cart_in_viewport = bool(cta_viewport_state.get("in_viewport")) if cta_viewport_state else False
        sticky_add_to_cart_visible = bool(cta_viewport_state.get("sticky")) if cta_viewport_state else False

        snapshot = {
            "interactive_elements": [
                str(item).strip()
                for item in viewport_state.get("interactive_elements", [])
                if str(item).strip()
            ][:25],
            "observed_products": observed_products,
            "cart_items": cart_items,
            "selected_product_hint": selected_product_hint,
            "available_options": available_options,
            "expandable_sections": expandable_sections,
            "add_to_cart_visible": add_to_cart_available and add_to_cart_in_viewport,
            "add_to_cart_available": add_to_cart_available,
            "add_to_cart_in_viewport": add_to_cart_in_viewport,
            "sticky_add_to_cart_visible": sticky_add_to_cart_visible,
            "purchase_blocked_reason": purchase_blocked_reason,
            "observation_engine": "scrapling",
        }
        hints = {
            "search_result_links": search_link_hints,
            "option_targets": option_hints,
            "expandable_targets": expandable_hints,
            "add_to_cart": add_to_cart_hint,
        }
        return snapshot, hints

    @staticmethod
    def _storage_key(url: str) -> str:
        if "coupang.com" in url:
            return "https://www.coupang.com"
        return url

    def _extract_products(
        self,
        *,
        adaptor: Adaptor,
        url: str,
    ) -> tuple[list[dict[str, object]], dict[str, dict[str, str]]]:
        linked_hints = self._extract_link_hints(adaptor=adaptor, url=url)
        json_ld_products = self._extract_products_from_json_ld(adaptor=adaptor)
        if json_ld_products:
            products: list[dict[str, object]] = []
            hints: dict[str, dict[str, str]] = {}
            for raw in json_ld_products[:8]:
                href = str(raw.get("href") or "").strip()
                if not href:
                    continue
                products.append(raw)
                matched_hint = self._matching_link_hint(href=href, link_hints=linked_hints)
                if matched_hint is not None:
                    hints[href] = matched_hint
            return products, hints

        products: list[dict[str, object]] = []
        hints = {}
        for anchor in adaptor.css(_PRODUCT_LINK_SELECTOR, identifier="coupang-product-links", adaptive=True, auto_save=True)[:12]:
            href = urljoin(url, str(anchor.attrib.get("href", "")).strip())
            text = self._normalize(anchor.text or anchor.get_all_text())
            if not href or not text or len(text) < 2:
                continue
            container_text = self._best_container_text(anchor)
            badges = self._extract_badges(anchor)
            products.append(
                {
                    "name": text[:160],
                    "href": href,
                    "price_text": self._first_match(_PRICE_PATTERN, container_text),
                    "rating_text": self._first_match(_RATING_PATTERN, container_text),
                    "review_count_text": self._first_match(_REVIEW_PATTERN, container_text),
                    "badges": badges,
                    "sold_out": bool(re.search(r"(품절|일시품절|재입고 알림)", container_text)),
                }
            )
            hints[href] = self._selector_hint(anchor)
            if len(products) >= 8:
                break
        return products, hints

    def _extract_link_hints(
        self,
        *,
        adaptor: Adaptor,
        url: str,
    ) -> dict[str, dict[str, str]]:
        hints: dict[str, dict[str, str]] = {}
        for anchor in adaptor.css(
            _PRODUCT_LINK_SELECTOR,
            identifier="coupang-product-links",
            adaptive=True,
            auto_save=True,
        )[:40]:
            href = urljoin(url, str(anchor.attrib.get("href", "")).strip())
            if href and href not in hints:
                hints[href] = self._selector_hint(anchor)
        return hints

    def _extract_cart_items(self, *, adaptor: Adaptor, url: str) -> list[dict[str, object]]:
        items: list[dict[str, object]] = []
        for anchor in adaptor.css(
            _PRODUCT_LINK_SELECTOR,
            identifier="coupang-cart-product-links",
            adaptive=True,
            auto_save=True,
        )[:12]:
            name = self._normalize(anchor.text or anchor.get_all_text())
            if not name:
                continue
            container_text = self._best_container_text(anchor)
            quantity_match = _QUANTITY_PATTERN.search(container_text)
            quantity_text = quantity_match.group(0) if quantity_match else None
            quantity_raw = None
            if quantity_match:
                quantity_raw = quantity_match.group(1) or quantity_match.group(2)
            supplemental: list[str] = []
            for raw_line in re.split(r"\n+", container_text):
                line = self._normalize(raw_line)
                if line and line != name:
                    supplemental.append(line)
            items.append(
                {
                    "name": name[:160],
                    "quantity": None if not quantity_raw else int(quantity_raw),
                    "quantity_text": quantity_text,
                    "option_summary": next((line for line in supplemental if re.search(r"(옵션|색상|사이즈|용량)", line)), None),
                    "package_summary": next(
                        (line for line in supplemental if re.search(r"\d+\s*(개|입|kg|g|ml|l|L|팩|봉)", line)),
                        None,
                    ),
                    "price_text": self._first_match(_PRICE_PATTERN, container_text),
                    "badges": self._extract_badges(anchor),
                }
            )
            if len(items) >= 8:
                break
        return items

    def _extract_selected_product_hint(
        self,
        *,
        adaptor: Adaptor,
        url: str,
        body_text: str,
    ) -> dict[str, object]:
        product_json_ld = self._extract_product_from_json_ld(adaptor=adaptor)
        if product_json_ld is not None:
            return {
                "name": str(product_json_ld.get("name") or url.rsplit("/", 1)[-1])[:160],
                "href": str(product_json_ld.get("url") or url),
                "price_text": self._normalize((product_json_ld.get("offers") or {}).get("price")),
                "rating_text": self._normalize((product_json_ld.get("aggregateRating") or {}).get("ratingValue")),
                "review_count_text": self._normalize((product_json_ld.get("aggregateRating") or {}).get("reviewCount")),
                "badges": [],
                "sold_out": "OutOfStock" in str((product_json_ld.get("offers") or {}).get("availability") or ""),
            }
        og_title = self._meta_content(adaptor=adaptor, property_name="og:title")
        if og_title:
            cleaned = self._clean_product_title(og_title)
            if cleaned:
                return {
                    "name": cleaned[:160],
                    "href": url,
                    "price_text": self._first_match(_PRICE_PATTERN, body_text),
                    "rating_text": self._first_match(_RATING_PATTERN, body_text),
                    "review_count_text": self._first_match(_REVIEW_PATTERN, body_text),
                    "badges": [],
                    "sold_out": bool(re.search(r"(품절|일시품절|재입고 알림)", body_text)),
                }
        heading = None
        for candidate in adaptor.css(
            "h1, h2, [data-testid*='title'], [class*='title']",
            identifier="coupang-product-title",
            adaptive=True,
            auto_save=True,
        )[:4]:
            text = self._clean_product_title(self._normalize(candidate.text or candidate.get_all_text()))
            if text and len(text) >= 2:
                heading = text[:160]
                break
        return {
            "name": heading or url.rsplit("/", 1)[-1],
            "href": url,
            "price_text": self._first_match(_PRICE_PATTERN, body_text),
            "rating_text": self._first_match(_RATING_PATTERN, body_text),
            "review_count_text": self._first_match(_REVIEW_PATTERN, body_text),
            "badges": [],
            "sold_out": bool(re.search(r"(품절|일시품절|재입고 알림)", body_text)),
        }

    def _extract_available_options(self, *, adaptor: Adaptor) -> tuple[list[str], dict[str, dict[str, str]]]:
        options: list[str] = []
        hints: dict[str, dict[str, str]] = {}
        selectors = (
            "[class*='option'] button, [class*='option'] label, [class*='option'] [role='option'], "
            "[class*='option'] option, [class*='count'] button, select option"
        )
        for candidate in adaptor.css(
            selectors,
            identifier="coupang-product-options",
            adaptive=True,
            auto_save=True,
        )[:30]:
            text = self._normalize(candidate.text or candidate.attrib.get("aria-label"))
            if not text or len(text) > 80 or self._ignore_option_text(text):
                continue
            if text not in options:
                options.append(text)
                hints[text] = self._selector_hint(candidate)
            if len(options) >= 12:
                break
        return options, hints

    def _extract_expandable_sections(self, *, adaptor: Adaptor) -> tuple[list[str], dict[str, dict[str, str]]]:
        sections: list[str] = []
        hints: dict[str, dict[str, str]] = {}
        for candidate in adaptor.css(
            "button, a, [role='button']",
            identifier="coupang-expandable-actions",
            adaptive=True,
            auto_save=True,
        )[:50]:
            text = self._normalize(candidate.text or candidate.attrib.get("aria-label"))
            if text not in {"더보기", "상품정보 더보기", "옵션 펼치기", "펼치기", "자세히 보기"}:
                continue
            if text not in sections:
                sections.append(text)
                hints[text] = self._selector_hint(candidate)
        return sections, hints

    def _extract_add_to_cart_hint(self, *, adaptor: Adaptor) -> dict[str, str] | None:
        for candidate in adaptor.css(
            "button, a, [role='button']",
            identifier="coupang-add-to-cart",
            adaptive=True,
            auto_save=True,
        )[:60]:
            text = self._normalize(candidate.text or candidate.attrib.get("aria-label"))
            if not text or not _CART_TEXT_PATTERN.search(text):
                continue
            return self._selector_hint(candidate) | {"text": text}
        return None

    @staticmethod
    def _best_viewport_cta(viewport_state: dict[str, object]) -> dict[str, object] | None:
        candidates = viewport_state.get("cart_ctas", [])
        if not isinstance(candidates, list):
            return None
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            if candidate.get("disabled"):
                continue
            return candidate
        return None

    @staticmethod
    def _ignore_option_text(text: str) -> bool:
        if re.search(r"^(리뷰|상품평|평점)\b", text):
            return True
        return any(
            token in text
            for token in (
                "전체",
                "쿠폰받기",
                "수량빼기",
                "수량더하기",
                "와우 멤버십으로 할인받기",
                "절약 금액 기준",
                "상품정보 더보기",
                "상품리뷰 운영원칙",
                "자세히 보기",
                "베스트순",
                "최신순",
                "도움이 돼요",
                "도움이 되었어요",
                "문의하기",
                "신고하기",
                "더보기",
            )
        )

    @staticmethod
    def _normalize(value: object) -> str:
        return re.sub(r"\s+", " ", str(value or "")).strip()

    @staticmethod
    def _first_match(pattern: re.Pattern[str], text: str) -> str | None:
        match = pattern.search(text)
        if match is None:
            return None
        return next((group for group in match.groups() if group), match.group(0))

    def _best_container_text(self, element) -> str:
        candidates = [self._normalize(element.text or element.get_all_text())]
        for ancestor in element.iterancestors():
            text = self._normalize(getattr(ancestor, "text", "") or getattr(ancestor, "get_all_text", lambda: "")())
            if text:
                candidates.append(text)
            if getattr(ancestor, "tag", "") in {"li", "article", "section"} and text:
                break
        for candidate in candidates:
            if len(candidate) >= 6:
                return candidate
        return candidates[-1] if candidates else ""

    def _extract_badges(self, element) -> list[str]:
        container = element
        for ancestor in element.iterancestors():
            if getattr(ancestor, "tag", "") in {"li", "article", "section", "div"}:
                container = ancestor
                break
        badges: list[str] = []
        for badge in container.css("img[alt], [aria-label]")[:8]:
            text = self._normalize(badge.attrib.get("alt") or badge.attrib.get("aria-label"))
            if text and text not in badges:
                badges.append(text)
            if len(badges) >= 4:
                break
        return badges

    @staticmethod
    def _selector_hint(selector) -> dict[str, str]:
        hint: dict[str, str] = {}
        css_selector = str(selector.generate_full_css_selector or "").strip()
        xpath_selector = str(selector.generate_full_xpath_selector or "").strip()
        if css_selector:
            hint["css"] = css_selector
        if xpath_selector:
            hint["xpath"] = xpath_selector
        return hint

    @staticmethod
    def _purchase_blocked_reason(body_text: str, *, is_product_page: bool) -> str | None:
        if not is_product_page:
            return None
        for pattern, reason in _PURCHASE_RESTRICTED_PATTERNS:
            if pattern.search(body_text):
                return reason
        return None

    def _extract_products_from_json_ld(self, *, adaptor: Adaptor) -> list[dict[str, object]]:
        products: list[dict[str, object]] = []
        for script in adaptor.css("script[type='application/ld+json']")[:8]:
            text = (script.text or script.get_all_text() or "").strip()
            if not text:
                continue
            try:
                payload = json.loads(text)
            except Exception:
                continue
            for item in self._json_ld_item_list_entries(payload):
                product = item.get("item") if isinstance(item, dict) else None
                if not isinstance(product, dict):
                    continue
                href = str(product.get("url") or "").strip()
                name = str(product.get("name") or "").strip()
                if not href or not name:
                    continue
                offers = product.get("offers") if isinstance(product.get("offers"), dict) else {}
                aggregate = product.get("aggregateRating") if isinstance(product.get("aggregateRating"), dict) else {}
                availability = str(offers.get("availability") or "")
                products.append(
                    {
                        "name": name[:160],
                        "href": href,
                        "price_text": self._normalize(offers.get("price")),
                        "rating_text": self._normalize(aggregate.get("ratingValue")),
                        "review_count_text": self._normalize(aggregate.get("reviewCount")),
                        "badges": [],
                        "sold_out": availability.endswith("OutOfStock"),
                    }
                )
            if products:
                break
        return products

    def _extract_product_from_json_ld(self, *, adaptor: Adaptor) -> dict[str, object] | None:
        for script in adaptor.css("script[type='application/ld+json']")[:8]:
            text = (script.text or script.get_all_text() or "").strip()
            if not text:
                continue
            try:
                payload = json.loads(text)
            except Exception:
                continue
            product = self._find_json_ld_product(payload)
            if product is not None:
                return product
        return None

    def _json_ld_item_list_entries(self, payload: object) -> list[dict[str, object]]:
        if isinstance(payload, list):
            entries: list[dict[str, object]] = []
            for item in payload:
                entries.extend(self._json_ld_item_list_entries(item))
            return entries
        if not isinstance(payload, dict):
            return []
        main_entity = payload.get("mainEntity")
        if isinstance(main_entity, dict):
            entries = main_entity.get("itemListElement")
            if isinstance(entries, list):
                return [entry for entry in entries if isinstance(entry, dict)]
        entries = payload.get("itemListElement")
        if isinstance(entries, list):
            return [entry for entry in entries if isinstance(entry, dict)]
        return []

    def _find_json_ld_product(self, payload: object) -> dict[str, object] | None:
        if isinstance(payload, list):
            for item in payload:
                product = self._find_json_ld_product(item)
                if product is not None:
                    return product
            return None
        if not isinstance(payload, dict):
            return None
        type_name = str(payload.get("@type") or "").lower()
        if type_name == "product" and payload.get("name"):
            return payload
        for value in payload.values():
            product = self._find_json_ld_product(value)
            if product is not None:
                return product
        return None

    @staticmethod
    def _matching_link_hint(href: str, link_hints: dict[str, dict[str, str]]) -> dict[str, str] | None:
        target = urlparse(href)
        target_key = f"{target.path}?{target.query}" if target.query else target.path
        for candidate_href, hint in link_hints.items():
            candidate = urlparse(candidate_href)
            candidate_key = f"{candidate.path}?{candidate.query}" if candidate.query else candidate.path
            if candidate_key == target_key:
                return hint
        for candidate_href, hint in link_hints.items():
            if urlparse(candidate_href).path == target.path:
                return hint
        return None

    def _meta_content(self, *, adaptor: Adaptor, property_name: str) -> str | None:
        for node in adaptor.css(f"meta[property='{property_name}'], meta[name='{property_name}']")[:2]:
            content = self._normalize(node.attrib.get("content"))
            if content:
                return content
        return None

    @staticmethod
    def _clean_product_title(text: str) -> str:
        cleaned = text.strip()
        for separator in (" - ", " | ", " : "):
            if separator in cleaned:
                cleaned = cleaned.split(separator, 1)[0].strip()
        if cleaned in {"다른 고객이 함께 본 상품", "추천", "상품 상세", "브랜드샵"}:
            return ""
        return cleaned
