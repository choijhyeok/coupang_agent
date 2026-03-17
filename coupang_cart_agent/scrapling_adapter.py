from __future__ import annotations

import re
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

from scrapling.parser import Adaptor


_PRODUCT_LINK_SELECTOR = "a[href*='/vp/products/']"
_CART_TEXT_PATTERN = re.compile(r"(?:장바구니\s*담기|카트에\s*담기|장바구니|카트)", re.IGNORECASE)
_PRICE_PATTERN = re.compile(r"([0-9][0-9,]{2,})\s*원?")
_RATING_PATTERN = re.compile(r"([0-5](?:\.[0-9])?)")
_REVIEW_PATTERN = re.compile(r"(?:리뷰|후기|평점)?\s*\(?([0-9][0-9,]*)\)?")
_QUANTITY_PATTERN = re.compile(r"(?:수량|수량변경)[^0-9]{0,8}(\d+)|(?<![0-9])(\d+)\s*개")
_PURCHASE_RESTRICTED_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"로켓프레시.*(장바구니|주문|와우)", re.IGNORECASE), "rocket_fresh_restriction"),
    (re.compile(r"와우.*(회원|전용|가능)", re.IGNORECASE), "wow_membership_restriction"),
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
        purchase_blocked_reason = self._purchase_blocked_reason(body_text)
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
        products: list[dict[str, object]] = []
        hints: dict[str, dict[str, str]] = {}
        for anchor in adaptor.css(
            _PRODUCT_LINK_SELECTOR,
            identifier="coupang-product-links",
            adaptive=True,
            auto_save=True,
        )[:12]:
            href = urljoin(url, str(anchor.attrib.get("href", "")).strip())
            text = self._normalize(anchor.text)
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

    def _extract_cart_items(self, *, adaptor: Adaptor, url: str) -> list[dict[str, object]]:
        items: list[dict[str, object]] = []
        for anchor in adaptor.css(
            _PRODUCT_LINK_SELECTOR,
            identifier="coupang-cart-product-links",
            adaptive=True,
            auto_save=True,
        )[:12]:
            name = self._normalize(anchor.text)
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
        heading = None
        for candidate in adaptor.css(
            "h1, h2, [data-testid*='title'], [class*='title']",
            identifier="coupang-product-title",
            adaptive=True,
            auto_save=True,
        )[:4]:
            text = self._normalize(candidate.text)
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
        candidates = [self._normalize(element.text)]
        for ancestor in element.iterancestors():
            text = self._normalize(getattr(ancestor, "text", ""))
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
    def _purchase_blocked_reason(body_text: str) -> str | None:
        for pattern, reason in _PURCHASE_RESTRICTED_PATTERNS:
            if pattern.search(body_text):
                return reason
        return None
