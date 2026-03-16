from __future__ import annotations

import queue
import re
import shutil
import subprocess
import threading
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from playwright.sync_api import Browser, BrowserContext, Page, Playwright, TimeoutError, sync_playwright

from .cart_executor import (
    AccessDeniedError,
    CartSnapshot,
    CoupangCartPage,
    LoginFailedError,
    LoginRequiredError,
    OptionMismatchError,
    OutOfStockError,
    SecurityChallengeError,
    SessionCredentials,
    UIElementNotFoundError,
)
from .contracts import (
    BrowserAgentAction,
    BrowserAgentActionType,
    BrowserObservation,
    ObservedProduct,
    SelectedProduct,
)
from .live_browser_agent import encode_screenshot_bytes


class DemoCoupangCartPage:
    """Deterministic fake page used for local integration proofs."""

    def __init__(self, *, should_fail: bool) -> None:
        self._should_fail = should_fail
        self._snapshots = 0

    def attach_to_logged_in_session(self, credentials: SessionCredentials | None = None) -> str:
        return "attached_demo_session"

    def assert_logged_in(self) -> None:
        return None

    def open_product(self, product_url: str) -> None:
        return None

    def assert_in_stock(self) -> None:
        if self._should_fail:
            raise OutOfStockError("Selected product is sold out.")

    def select_options(self, selection: SelectedProduct) -> dict[str, str]:
        selected_options = {"quantity": str(selection.quantity)}
        selected_options.update(selection.option_hints)
        return selected_options

    def cart_snapshot(self) -> CartSnapshot:
        self._snapshots += 1
        count = 0 if self._snapshots == 1 else 1
        return CartSnapshot(item_count=count, summary=f"count={count}")

    def add_to_cart(self) -> str:
        return "cart-item-demo"

    def checkout_started(self) -> bool:
        return False

    def observe(
        self,
        *,
        step_index: int,
        last_action_summary: str | None = None,
    ) -> BrowserObservation:
        return BrowserObservation(
            step_index=step_index,
            url="https://www.coupang.com/np/search",
            title="쿠팡 검색",
            page_kind="search_results" if step_index == 1 else "product_page",
            body_text_excerpt="양파 추천 8,900원 평점 4.8 리뷰 1,800 장바구니 담기",
            interactive_elements=["searchbox:검색", "link:양파 추천", "button:장바구니 담기"],
            observed_products=[
                ObservedProduct(
                    name="양파 추천",
                    href="https://www.coupang.com/vp/products/demo-onion",
                    price_text="8,900원",
                    rating_text="4.8",
                    review_count_text="1,800",
                    badges=["Rocket"],
                )
            ],
            selected_product_hint={
                "name": "양파 추천",
                "href": "https://www.coupang.com/vp/products/demo-onion",
                "price_text": "8,900원",
                "rating_text": "4.8",
                "review_count_text": "1,800",
                "badges": ["Rocket"],
            },
            add_to_cart_visible=step_index > 1,
            last_action_summary=last_action_summary,
        )

    def execute_action(self, action: BrowserAgentAction) -> str:
        if action.action_type == BrowserAgentActionType.WAIT:
            return "Waited in demo mode."
        if action.action_type == BrowserAgentActionType.SEARCH:
            return f"Searched for {action.query or ''}."
        if action.action_type == BrowserAgentActionType.CLICK:
            return f"Clicked {action.target_text or action.target_href or 'demo target'}."
        if action.action_type == BrowserAgentActionType.SELECT_OPTION:
            return f"Selected option {action.value or action.target_text or ''}."
        if action.action_type == BrowserAgentActionType.ADD_TO_CART:
            return "Clicked add-to-cart in demo mode."
        return "No-op."


@dataclass(slots=True)
class PlaywrightCoupangSettings:
    login_url: str
    cart_url: str
    headless: bool = True
    storage_state_path: str | None = None
    navigation_timeout_ms: int = 30000


@dataclass(slots=True)
class ChromeCdpSettings:
    chrome_user_data_dir: str
    chrome_profile_directory: str
    remote_debugging_port: int
    copied_user_data_dir: str
    chrome_binary_path: str = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"


@dataclass(slots=True)
class BrowserUseSettings:
    chrome_user_data_dir: str
    chrome_profile_directory: str
    remote_debugging_port: int
    copied_user_data_dir: str
    chrome_binary_path: str = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"


class PlaywrightCoupangCartPage:
    """Real browser automation adapter backed by Playwright."""

    def __init__(self, settings: PlaywrightCoupangSettings) -> None:
        self._settings = settings
        self._playwright_cm: Playwright | None = None
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None

    def close(self) -> None:
        if self._context is not None and self._settings.storage_state_path:
            self._context.storage_state(path=self._settings.storage_state_path)
        if self._browser is not None:
            self._browser.close()
        self._close_playwright_context_manager()
        self._playwright_cm = None
        self._playwright = None
        self._browser = None
        self._context = None
        self._page = None

    def _close_playwright_context_manager(self) -> None:
        if self._playwright_cm is None:
            return
        try:
            self._playwright_cm.__exit__(None, None, None)
        except AttributeError:
            # Playwright can leave the context manager partially initialized when
            # sync usage fails before the connection object is created.
            pass

    def attach_to_logged_in_session(self, credentials: SessionCredentials | None = None) -> str:
        page = self._page_object()
        if page.url and page.url != "about:blank":
            self._assert_no_session_blockers(page)
            if self._is_logged_in(page):
                return self._attached_session_mode()

        page.goto(self._settings.cart_url, wait_until="domcontentloaded")
        page.wait_for_timeout(1500)
        self._assert_no_session_blockers(page)
        if self._is_logged_in(page):
            return self._attached_session_mode()

        raise LoginRequiredError(
            "Attach mode requires an operator-prepared logged-in Coupang session."
        )

    def assert_logged_in(self) -> None:
        page = self._page_object()
        self._assert_no_session_blockers(page)
        if not self._is_logged_in(page):
            raise LoginRequiredError(
                "Attach mode requires an operator-prepared logged-in Coupang session."
            )

    def open_product(self, product_url: str) -> None:
        page = self._page_object()
        page.goto(product_url, wait_until="domcontentloaded")
        page.wait_for_timeout(2500)
        self._assert_no_session_blockers(page)

    def assert_in_stock(self) -> None:
        page = self._page_object()
        self._assert_no_session_blockers(page)
        sold_out_tokens = ("품절", "일시품절", "재입고 알림")
        page_text = page.locator("body").inner_text(timeout=5000)
        if any(token in page_text for token in sold_out_tokens):
            raise OutOfStockError("Selected product is sold out.")

        if not self._find_add_to_cart_button(optional=True):
            raise UIElementNotFoundError("Add-to-cart button not found.")

    def select_options(self, selection: SelectedProduct) -> dict[str, str]:
        selected_options = {"quantity": str(selection.quantity)}
        page = self._page_object()
        self._assert_no_session_blockers(page)
        for _, value in selection.option_hints.items():
            if self._click_first(
                (
                    lambda: page.get_by_role("button", name=value, exact=False),
                    lambda: page.get_by_role("option", name=value, exact=False),
                    lambda: page.get_by_text(value, exact=False),
                )
            ):
                selected_options[value] = value
                continue
            raise OptionMismatchError(f"Requested option value not found: {value}")
        return selected_options

    def cart_snapshot(self) -> CartSnapshot:
        page = self._page_object()
        original_url = page.url
        cart_page = self._context_object().new_page()
        try:
            cart_page.goto(self._settings.cart_url, wait_until="domcontentloaded")
            cart_page.wait_for_timeout(3000)
            self._assert_no_session_blockers(cart_page)
            count = self._extract_cart_count(cart_page)
            return CartSnapshot(item_count=count, summary=f"cart_count={count}")
        finally:
            cart_page.close()
            if original_url:
                page.bring_to_front()

    def add_to_cart(self) -> str:
        button = self._find_add_to_cart_button(optional=False)
        self._click_add_to_cart_button(button)
        page = self._page_object()
        page.wait_for_timeout(1500)
        self._assert_no_session_blockers(page)
        return f"{self._product_slug(self._page_object().url)}:cart-add"

    def checkout_started(self) -> bool:
        page = self._page_object()
        url = page.url.lower()
        return "checkout" in url or "order" in url or "buy" in url

    def observe(
        self,
        *,
        step_index: int,
        last_action_summary: str | None = None,
    ) -> BrowserObservation:
        page = self._page_object()
        blocker_hint = None
        try:
            self._assert_no_session_blockers(page)
        except Exception as exc:
            blocker_hint = str(exc)
        body_text = self._safe_inner_text(page)
        if blocker_hint is None:
            blocker_hint = self._infer_session_blocker_hint(page=page, body_text=body_text)
        snapshot = self._extract_browser_snapshot(page)
        screenshot_dir = Path(".artifacts/browser-agent")
        screenshot_dir.mkdir(parents=True, exist_ok=True)
        screenshot_path = screenshot_dir / f"step-{step_index}.png"
        screenshot_bytes = None
        try:
            screenshot_bytes = page.screenshot(path=str(screenshot_path), type="png")
        except Exception:
            screenshot_path = None
        selected_hint = snapshot.get("selected_product_hint", {})
        observed_products = [
            ObservedProduct(
                name=str(raw.get("name", "")).strip(),
                href=(None if not raw.get("href") else str(raw.get("href"))),
                price_text=(None if not raw.get("price_text") else str(raw.get("price_text"))),
                rating_text=(None if not raw.get("rating_text") else str(raw.get("rating_text"))),
                review_count_text=(
                    None if not raw.get("review_count_text") else str(raw.get("review_count_text"))
                ),
                badges=[str(item) for item in raw.get("badges", [])],
                sold_out=bool(raw.get("sold_out", False)),
            )
            for raw in snapshot.get("observed_products", [])
            if str(raw.get("name", "")).strip()
        ]
        page_kind = self._page_kind(
            url=page.url,
            observed_products=observed_products,
            add_to_cart_visible=bool(snapshot.get("add_to_cart_visible", False)),
            blocker_hint=blocker_hint,
        )
        selected_product_hint = dict(selected_hint) if isinstance(selected_hint, dict) else {}
        available_options = self._normalize_available_options(snapshot.get("available_options", []))
        add_to_cart_visible = bool(snapshot.get("add_to_cart_visible", False))
        if self._is_cart_page_url(page.url):
            observed_products = []
            selected_product_hint = {}
            available_options = []
            add_to_cart_visible = False
        elif page_kind == "search_results":
            selected_product_hint = {}
            available_options = []
            add_to_cart_visible = False

        return BrowserObservation(
            step_index=step_index,
            url=page.url,
            title=self._safe_title(page),
            page_kind=page_kind,
            body_text_excerpt=body_text[:2000],
            accessibility_lines=[str(item) for item in snapshot.get("interactive_elements", [])[:25]],
            html_excerpt=self._safe_html_excerpt(page),
            screenshot_path=None if screenshot_path is None else str(screenshot_path),
            screenshot_base64=encode_screenshot_bytes(screenshot_bytes),
            interactive_elements=[str(item) for item in snapshot.get("interactive_elements", [])[:25]],
            observed_products=observed_products[:8],
            selected_product_hint=selected_product_hint,
            available_options=available_options,
            add_to_cart_visible=add_to_cart_visible,
            blocker_hint=blocker_hint,
            cart_count=self._try_extract_cart_count(page),
            last_action_summary=last_action_summary,
        )

    def execute_action(self, action: BrowserAgentAction) -> str:
        page = self._page_object()
        self._assert_no_session_blockers(page)
        if action.action_type == BrowserAgentActionType.SEARCH:
            query = (action.query or action.target_text or "").strip()
            if not query:
                raise UIElementNotFoundError("Search action did not include a query.")
            self._perform_search(query)
            return f"Searched for {query}."
        if action.action_type == BrowserAgentActionType.CLICK and action.target_href:
            page.goto(action.target_href, wait_until="domcontentloaded")
            page.wait_for_timeout(1500)
            return f"Opened {action.target_href}."
        if action.action_type in (BrowserAgentActionType.CLICK, BrowserAgentActionType.SELECT_OPTION):
            locator = self._locate_action_target(
                target_text=action.target_text or action.value,
                target_role=action.target_role,
                target_href=action.target_href,
            )
            if locator is None:
                raise UIElementNotFoundError("Action target was not found.")
            self._click_add_to_cart_button(locator) if action.action_type == BrowserAgentActionType.CLICK else locator.click()
            page.wait_for_timeout(1500)
            return f"Clicked {action.target_text or action.target_href or action.value or 'target'}."
        if action.action_type == BrowserAgentActionType.ADD_TO_CART:
            button = self._find_add_to_cart_button(optional=False)
            self._click_add_to_cart_button(button)
            page.wait_for_timeout(1500)
            return "Clicked add-to-cart button."
        if action.action_type == BrowserAgentActionType.WAIT:
            wait_seconds = max(0.1, action.wait_seconds or 1.0)
            page.wait_for_timeout(int(wait_seconds * 1000))
            return f"Waited {wait_seconds:.1f}s."
        return "No-op."

    def _page_object(self) -> Page:
        if self._page is None:
            self._playwright_cm = sync_playwright()
            self._playwright = self._playwright_cm.__enter__()
            self._browser = self._playwright.chromium.launch(headless=self._settings.headless)
            context_kwargs: dict[str, object] = {}
            if self._settings.storage_state_path and Path(self._settings.storage_state_path).exists():
                context_kwargs["storage_state"] = self._settings.storage_state_path
            self._context = self._browser.new_context(**context_kwargs)
            self._context.set_default_timeout(self._settings.navigation_timeout_ms)
            self._page = self._context.new_page()
        return self._page

    def _pick_attached_page(self, context: BrowserContext) -> Page:
        for page in reversed(context.pages):
            url = (page.url or "").lower()
            if url and not url.startswith(("chrome://", "devtools://", "chrome-extension://")):
                return page
        return context.pages[0] if context.pages else context.new_page()

    def _context_object(self) -> BrowserContext:
        self._page_object()
        assert self._context is not None
        return self._context

    def _find_add_to_cart_button(self, *, optional: bool) -> object:
        page = self._page_object()
        button = self._first_locator(
            (
                lambda: page.get_by_role("button", name="장바구니 담기", exact=False),
                lambda: page.get_by_role("button", name="장바구니", exact=False),
                lambda: page.locator("button").filter(has_text="장바구니"),
                lambda: page.locator("[class*='cart']").filter(has_text="장바구니"),
            )
        )
        if button is None and not optional:
            raise UIElementNotFoundError("Add-to-cart button not found.")
        return button

    def _extract_cart_count(self, page: Page) -> int:
        self._assert_no_session_blockers(page)
        strategies: tuple[Callable[[], int | None], ...] = (
            lambda: self._count_locator(page.locator("[data-cart-item-id]")),
            lambda: self._count_locator(page.locator("[class*='cart-item']")),
            lambda: self._count_locator(page.locator("input[name='cartId']")),
        )
        for strategy in strategies:
            value = strategy()
            if value is not None and value > 0:
                return value

        body = page.locator("body").inner_text(timeout=5000)
        if "장바구니가 비었습니다" in body:
            return 0
        header_match = re.search(r"장바구니\((\d+)\)", body)
        if header_match is not None:
            return int(header_match.group(1))
        selection_match = re.search(r"\((\d+)\s*/\s*\d+\)", body)
        if selection_match is not None:
            return int(selection_match.group(1))
        raise UIElementNotFoundError("Cart item count could not be determined.")

    def _count_locator(self, locator) -> int | None:
        try:
            return locator.count()
        except TimeoutError:
            return None

    def _perform_search(self, query: str) -> None:
        page = self._page_object()
        locator = self._first_locator(
            (
                lambda: page.get_by_role("searchbox"),
                lambda: page.get_by_role("textbox", name="검색", exact=False),
                lambda: page.locator("input[type='search']"),
                lambda: page.locator("input[placeholder*='검색']"),
                lambda: page.locator("input[name*='q']"),
                lambda: page.locator("input"),
            )
        )
        if locator is not None:
            locator.fill(query)
            locator.press("Enter")
            page.wait_for_timeout(2000)
            return

        encoded_query = urllib.parse.quote(query)
        page.goto(
            f"https://www.coupang.com/np/search?component=&q={encoded_query}",
            wait_until="domcontentloaded",
        )
        page.wait_for_timeout(2000)

    def _locate_action_target(
        self,
        *,
        target_text: str | None,
        target_role: str | None,
        target_href: str | None,
    ):
        page = self._page_object()
        factories: list[Callable[[], object]] = []
        if target_href:
            for href_fragment in self._href_match_fragments(target_href):
                escaped_fragment = href_fragment.replace('"', '\\"')
                factories.append(lambda fragment=escaped_fragment: page.locator(f'a[href*="{fragment}"]'))
        if target_role and target_text:
            factories.append(lambda: page.get_by_role(target_role, name=target_text, exact=False))
        if target_text:
            factories.extend(
                (
                    lambda: page.get_by_text(target_text, exact=False),
                    lambda: page.locator("a").filter(has_text=target_text),
                    lambda: page.locator("button").filter(has_text=target_text),
                    lambda: page.locator("[role='option']").filter(has_text=target_text),
                    lambda: page.locator("label").filter(has_text=target_text),
                )
            )
        return self._first_locator(tuple(factories))

    @staticmethod
    def _href_match_fragments(target_href: str) -> list[str]:
        fragments: list[str] = []
        for candidate in (
            target_href,
            urllib.parse.urlparse(target_href).path,
        ):
            normalized = (candidate or "").strip()
            if normalized and normalized not in fragments:
                fragments.append(normalized)
        return fragments

    def _extract_browser_snapshot(self, page: Page) -> dict[str, object]:
        try:
            payload = page.evaluate(
                """
                () => {
                  const visible = (element) => {
                    if (!element) return false;
                    const style = window.getComputedStyle(element);
                    const rect = element.getBoundingClientRect();
                    return style && style.visibility !== 'hidden' && style.display !== 'none' && rect.width > 0 && rect.height > 0;
                  };
                  const normalize = (value) => (value || '').replace(/\\s+/g, ' ').trim();
                  const interactiveElements = Array.from(
                    document.querySelectorAll("button, a, input, select, option, [role='button'], [role='link'], [role='option'], [role='textbox'], label")
                  )
                    .filter((element) => visible(element))
                    .slice(0, 25)
                    .map((element) => {
                      const role = normalize(element.getAttribute('role')) || element.tagName.toLowerCase();
                      const text = normalize(element.innerText || element.textContent || element.getAttribute('aria-label') || element.getAttribute('placeholder'));
                      return `${role}:${text}`.slice(0, 160);
                    })
                    .filter(Boolean);
                  const observedProducts = [];
                  const seenHref = new Set();
                  for (const anchor of Array.from(document.querySelectorAll("a[href*='/vp/products/']"))) {
                    if (!visible(anchor)) continue;
                    const href = anchor.href;
                    if (!href || seenHref.has(href)) continue;
                    const container = anchor.closest('li, article, section, div') || anchor;
                    const text = normalize(anchor.innerText || anchor.textContent);
                    const containerText = normalize(container.innerText || container.textContent);
                    if (!text || text.length < 4) continue;
                    const priceMatch = containerText.match(/([0-9][0-9,]{2,})\\s*원?/);
                    const ratingMatch = containerText.match(/([0-5](?:\\.[0-9])?)/);
                    const reviewMatch = containerText.match(/(?:리뷰|평점|후기)?\\s*\\(?([0-9][0-9,]*)\\)?/);
                    observedProducts.push({
                      name: text.slice(0, 160),
                      href,
                      price_text: priceMatch ? priceMatch[1] : null,
                      rating_text: ratingMatch ? ratingMatch[1] : null,
                      review_count_text: reviewMatch ? reviewMatch[1] : null,
                      badges: Array.from(container.querySelectorAll('img[alt], [aria-label]'))
                        .map((element) => normalize(element.getAttribute('alt') || element.getAttribute('aria-label')))
                        .filter(Boolean)
                        .slice(0, 4),
                      sold_out: /품절|일시품절|재입고 알림/.test(containerText),
                    });
                    seenHref.add(href);
                    if (observedProducts.length >= 8) break;
                  }
                  const currentUrl = window.location.href;
                  const isProductPage = /\\/vp\\/products\\//.test(currentUrl);
                  const bodyText = normalize(document.body ? document.body.innerText : '');
                  const heading = normalize((document.querySelector('h1') || {}).innerText || '');
                  const priceMatch = bodyText.match(/([0-9][0-9,]{2,})\\s*원?/);
                  const ratingMatch = bodyText.match(/([0-5](?:\\.[0-9])?)/);
                  const reviewMatch = bodyText.match(/(?:리뷰|후기|평점)\\s*\\(?([0-9][0-9,]*)\\)?/);
                  const optionSelectors = [
                    "[class*='option'] button",
                    "[class*='option'] label",
                    "[class*='option'] [role='option']",
                    "[class*='option'] option",
                    "[class*='quantity'] button",
                    "[class*='count'] button",
                    "[class*='count'] label",
                    "[class*='order'] button",
                    "[class*='buy'] button",
                    "select option",
                  ];
                  const optionCandidates = !isProductPage
                    ? []
                    : Array.from(document.querySelectorAll(optionSelectors.join(",")));
                  const availableOptions = !isProductPage
                    ? []
                    : (optionCandidates.length ? optionCandidates : Array.from(
                        document.querySelectorAll("button, option, [role='option'], label")
                      ))
                        .filter((element) => visible(element))
                        .map((element) => normalize(element.innerText || element.textContent || element.getAttribute('aria-label')))
                        .filter((text) => text && text.length <= 80)
                        .filter((text) => !/장바구니|구매|검색|로그인|마이쿠팡/.test(text))
                        .slice(0, 20);
                  const documentText = normalize(document.documentElement ? document.documentElement.textContent : bodyText);
                  const addToCartVisible = Array.from(
                    document.querySelectorAll("button, [role='button']")
                  ).some((element) => {
                    if (!visible(element)) return false;
                    const text = normalize(element.innerText || element.textContent || element.getAttribute('aria-label'));
                    return /장바구니\\s*담기|카트에\\s*담기|담기/.test(text);
                  }) || /장바구니\\s*담기/.test(bodyText) || (isProductPage && /장바구니\\s*담기/.test(documentText));
                  return {
                    interactive_elements: interactiveElements,
                    observed_products: observedProducts,
                    selected_product_hint: isProductPage && heading ? {
                      name: heading,
                      href: currentUrl,
                      price_text: priceMatch ? priceMatch[1] : null,
                      rating_text: ratingMatch ? ratingMatch[1] : null,
                      review_count_text: reviewMatch ? reviewMatch[1] : null,
                      badges: [],
                      sold_out: /품절|일시품절|재입고 알림/.test(bodyText),
                    } : {},
                    available_options: availableOptions,
                    add_to_cart_visible: addToCartVisible,
                  };
                }
                """
            )
            return payload if isinstance(payload, dict) else {}
        except Exception:
            return {}

    def _safe_inner_text(self, page: Page) -> str:
        try:
            return page.locator("body").inner_text(timeout=3000)
        except Exception:
            return ""

    def _safe_title(self, page: Page) -> str:
        try:
            return page.title()
        except Exception:
            return ""

    def _safe_html_excerpt(self, page: Page) -> str | None:
        try:
            return page.content()[:3000]
        except Exception:
            return None

    def _page_kind(
        self,
        *,
        url: str,
        observed_products: list[ObservedProduct],
        add_to_cart_visible: bool,
        blocker_hint: str | None,
    ) -> str:
        if blocker_hint:
            return "session_blocked"
        if self._is_cart_page_url(url):
            return "browse"
        if "/vp/products/" in url:
            return "product_page"
        if observed_products or "search" in url:
            return "search_results"
        if add_to_cart_visible:
            return "product_page"
        return "browse"

    @staticmethod
    def _is_cart_page_url(url: str) -> bool:
        lowered = url.lower()
        return "cart.coupang.com/cartview.pang" in lowered or "/cartview.pang" in lowered

    @staticmethod
    def _normalize_available_options(raw_options) -> list[str]:
        ignored_patterns = (
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
        normalized: list[str] = []
        for item in raw_options:
            text = str(item).strip()
            if not text:
                continue
            if any(pattern in text for pattern in ignored_patterns):
                continue
            if re.search(r"\d+명에게 도움이 됐어요", text):
                continue
            if re.search(r"^(리뷰|상품평|평점)\b", text):
                continue
            if text not in normalized:
                normalized.append(text)
            if len(normalized) >= 12:
                break
        return normalized

    def _infer_session_blocker_hint(self, *, page: Page, body_text: str) -> str | None:
        if self._is_access_denied(page):
            return "Attach mode was blocked by Coupang Access Denied. Use an operator-approved logged-in Chrome session."
        if self._is_security_challenge(page):
            return "Attach mode encountered a Coupang security challenge. Operator re-authentication is required."
        if self._is_login_page(page):
            return "Attach mode reached the Coupang login page. Prepare a logged-in Chrome session before running automation."
        lowered_url = page.url.lower()
        login_prompt_tokens = (
            "로그인을 하시면",
            "로그인하기",
            "로그인이 필요합니다",
        )
        if (
            "cart.coupang.com/cartview.pang" in lowered_url
            or "/cartview.pang" in lowered_url
        ) and any(token in body_text for token in login_prompt_tokens):
            return "Attach mode requires an operator-prepared logged-in Coupang session."
        return None

    def _try_extract_cart_count(self, page: Page) -> int | None:
        try:
            return self._extract_cart_count(page)
        except Exception:
            return None

    def _is_logged_in(self, page: Page | None = None) -> bool:
        active_page = page or self._page_object()
        if not active_page.url or active_page.url == "about:blank":
            return False
        lowered_url = active_page.url.lower()
        if (
            "cart.coupang.com/cartview.pang" in lowered_url
            or "/cartview.pang" in lowered_url
        ) and not self._is_login_page(active_page):
            body = self._safe_inner_text(active_page)
            login_prompt_tokens = (
                "로그인을 하시면",
                "로그인하기",
                "로그인이 필요합니다",
            )
            if any(token in body for token in login_prompt_tokens):
                return False
            return True
        if "login" not in active_page.url.lower():
            return self._first_locator(
                (
                    lambda: active_page.get_by_role("link", name="로그아웃", exact=False),
                    lambda: active_page.get_by_text("마이쿠팡", exact=False),
                    lambda: active_page.get_by_text("로그아웃", exact=False),
                )
            ) is not None
        return self._first_locator(
            (
                lambda: active_page.get_by_role("link", name="로그아웃", exact=False),
                lambda: active_page.get_by_text("마이쿠팡", exact=False),
                lambda: active_page.get_by_text("로그아웃", exact=False),
            )
        ) is not None

    def _is_access_denied(self, page: Page | None = None) -> bool:
        active_page = page or self._page_object()
        if "access denied" in active_page.title().lower():
            return True
        try:
            body = active_page.locator("body").inner_text(timeout=2000)
        except Exception:
            return False
        lowered = body.lower()
        return "access denied" in lowered and "permission" in lowered

    def _is_login_page(self, page: Page | None = None) -> bool:
        active_page = page or self._page_object()
        url = active_page.url.lower()
        if "login.coupang.com" in url or "/login/" in url:
            return True
        title = active_page.title().lower()
        return "login" in title and "coupang" in title

    def _is_security_challenge(self, page: Page | None = None) -> bool:
        active_page = page or self._page_object()
        fragments: list[str] = [active_page.url.lower()]
        try:
            fragments.append(active_page.title().lower())
        except Exception:
            pass
        try:
            fragments.append(active_page.locator("body").inner_text(timeout=2000).lower())
        except Exception:
            pass
        combined = " ".join(fragments)
        challenge_tokens = (
            "captcha",
            "security verification",
            "security check",
            "verify you are human",
            "로봇이 아닙니다",
            "보안문자",
            "보안 인증",
            "본인인증",
            "이상 접근",
            "자동화된 접근",
        )
        return any(token in combined for token in challenge_tokens)

    def _assert_no_session_blockers(self, page: Page | None = None) -> None:
        active_page = page or self._page_object()
        if self._is_access_denied(active_page):
            raise AccessDeniedError(
                "Attach mode was blocked by Coupang Access Denied. Use an operator-approved logged-in Chrome session."
            )
        if self._is_security_challenge(active_page):
            raise SecurityChallengeError(
                "Attach mode encountered a Coupang security challenge. Operator re-authentication is required."
            )
        if self._is_login_page(active_page) and not self._is_logged_in(active_page):
            raise LoginRequiredError(
                "Attach mode reached the Coupang login page. Prepare a logged-in Chrome session before running automation."
            )

    def _attached_session_mode(self) -> str:
        if self._settings.storage_state_path and Path(self._settings.storage_state_path).exists():
            return "attached_storage_state"
        return "attached_browser_session"

    def _click_first(self, factories, *, action=None) -> bool:
        locator = self._first_locator(factories)
        if locator is None:
            return False
        if action is None:
            locator.click()
        else:
            action(locator)
        return True

    def _first_locator(self, factories):
        for factory in factories:
            try:
                locator = factory().first
                locator.wait_for(state="visible", timeout=1500)
                return locator
            except TimeoutError:
                continue
            except Exception:
                continue
        return None

    def _click_add_to_cart_button(self, button) -> None:
        attempts = (
            lambda: button.click(timeout=3000),
            lambda: button.click(timeout=3000, force=True),
            lambda: (button.scroll_into_view_if_needed(timeout=3000), button.click(timeout=3000)),
            lambda: button.evaluate("(element) => element.click()"),
        )
        last_error: Exception | None = None
        for attempt in attempts:
            try:
                attempt()
                return
            except Exception as exc:
                last_error = exc
        assert last_error is not None
        raise last_error

    @staticmethod
    def _product_slug(url: str) -> str:
        trimmed = url.rstrip("/").split("/")[-1]
        return trimmed or "unknown-product"


class ChromeCdpCoupangCartPage(PlaywrightCoupangCartPage):
    """Attach to a locally launched Chrome session over CDP using a copied user profile."""

    def __init__(
        self,
        *,
        settings: PlaywrightCoupangSettings,
        cdp_settings: ChromeCdpSettings,
    ) -> None:
        super().__init__(settings)
        self._cdp_settings = cdp_settings
        self._chrome_process: subprocess.Popen[str] | None = None

    def close(self) -> None:
        try:
            if self._browser is not None:
                self._browser.close()
            self._close_playwright_context_manager()
        finally:
            if self._chrome_process is not None:
                self._chrome_process.terminate()
                try:
                    self._chrome_process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self._chrome_process.kill()
            shutil.rmtree(self._cdp_settings.copied_user_data_dir, ignore_errors=True)
            self._chrome_process = None
            self._playwright_cm = None
            self._playwright = None
            self._browser = None
            self._context = None
            self._page = None

    def _page_object(self) -> Page:
        if self._page is None:
            self._prepare_copied_profile()
            self._chrome_process = subprocess.Popen(
                [
                    self._cdp_settings.chrome_binary_path,
                    f"--user-data-dir={self._cdp_settings.copied_user_data_dir}",
                    f"--profile-directory={self._cdp_settings.chrome_profile_directory}",
                    f"--remote-debugging-port={self._cdp_settings.remote_debugging_port}",
                    "--no-first-run",
                    "--no-default-browser-check",
                    "about:blank",
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                text=True,
            )
            self._playwright_cm = sync_playwright()
            self._playwright = self._playwright_cm.__enter__()
            self._browser = self._connect_browser_with_retry()
            self._context = self._browser.contexts[0]
            self._context.set_default_timeout(self._settings.navigation_timeout_ms)
            self._page = self._pick_attached_page(self._context)
        return self._page

    def _prepare_copied_profile(self) -> None:
        root = Path(self._cdp_settings.chrome_user_data_dir)
        destination = Path(self._cdp_settings.copied_user_data_dir)
        profile_name = self._cdp_settings.chrome_profile_directory
        profile_src = root / profile_name
        profile_dest = destination / profile_name
        destination.mkdir(parents=True, exist_ok=True)
        shutil.copy2(root / "Local State", destination / "Local State")
        shutil.copytree(
            profile_src,
            profile_dest,
            dirs_exist_ok=True,
            ignore=shutil.ignore_patterns(
                "Singleton*",
                "Lockfile",
                "*.lock",
            ),
        )

    def _connect_browser_with_retry(self) -> Browser:
        last_error: Exception | None = None
        endpoint = f"http://127.0.0.1:{self._cdp_settings.remote_debugging_port}"
        for _ in range(20):
            try:
                assert self._playwright is not None
                return self._playwright.chromium.connect_over_cdp(endpoint)
            except Exception as exc:
                last_error = exc
                time.sleep(0.5)
        assert last_error is not None
        raise last_error

    def _attached_session_mode(self) -> str:
        return "attached_cdp_profile"


class ExistingChromeCdpCoupangCartPage(PlaywrightCoupangCartPage):
    """Attach to an already running operator Chrome session over CDP."""

    def __init__(
        self,
        *,
        settings: PlaywrightCoupangSettings,
        remote_debugging_port: int,
    ) -> None:
        super().__init__(settings)
        self._remote_debugging_port = remote_debugging_port
        self._worker_thread: threading.Thread | None = None
        self._worker_queue: queue.Queue[tuple[Callable[[], object] | None, queue.Queue[tuple[bool, object]] | None]] | None = None

    def attach_to_logged_in_session(self, credentials: SessionCredentials | None = None) -> str:
        return str(
            self._run_on_worker(
                lambda: super(ExistingChromeCdpCoupangCartPage, self).attach_to_logged_in_session(credentials)
            )
        )

    def assert_logged_in(self) -> None:
        self._run_on_worker(super(ExistingChromeCdpCoupangCartPage, self).assert_logged_in)

    def open_product(self, product_url: str) -> None:
        self._run_on_worker(
            lambda: super(ExistingChromeCdpCoupangCartPage, self).open_product(product_url)
        )

    def assert_in_stock(self) -> None:
        self._run_on_worker(super(ExistingChromeCdpCoupangCartPage, self).assert_in_stock)

    def select_options(self, selection: SelectedProduct) -> dict[str, str]:
        return dict(
            self._run_on_worker(
                lambda: super(ExistingChromeCdpCoupangCartPage, self).select_options(selection)
            )
        )

    def cart_snapshot(self) -> CartSnapshot:
        snapshot = self._run_on_worker(super(ExistingChromeCdpCoupangCartPage, self).cart_snapshot)
        assert isinstance(snapshot, CartSnapshot)
        return snapshot

    def add_to_cart(self) -> str:
        return str(self._run_on_worker(super(ExistingChromeCdpCoupangCartPage, self).add_to_cart))

    def checkout_started(self) -> bool:
        return bool(self._run_on_worker(super(ExistingChromeCdpCoupangCartPage, self).checkout_started))

    def observe(
        self,
        *,
        step_index: int,
        last_action_summary: str | None = None,
    ) -> BrowserObservation:
        observation = self._run_on_worker(
            lambda: super(ExistingChromeCdpCoupangCartPage, self).observe(
                step_index=step_index,
                last_action_summary=last_action_summary,
            )
        )
        assert isinstance(observation, BrowserObservation)
        return observation

    def execute_action(self, action: BrowserAgentAction) -> str:
        return str(
            self._run_on_worker(
                lambda: super(ExistingChromeCdpCoupangCartPage, self).execute_action(action)
            )
        )

    def close(self) -> None:
        if self._worker_thread is not None and self._worker_thread.is_alive():
            self._run_on_worker(self._close_local_resources)
            assert self._worker_queue is not None
            self._worker_queue.put((None, None))
            self._worker_thread.join(timeout=5)
            self._worker_thread = None
            self._worker_queue = None
            return
        self._close_local_resources()

    def _page_object(self) -> Page:
        if self._page is None:
            self._playwright_cm = sync_playwright()
            self._playwright = self._playwright_cm.__enter__()
            self._browser = self._connect_browser_with_retry()
            if self._browser.contexts:
                self._context = self._browser.contexts[0]
            else:
                self._context = self._browser.new_context()
            self._context.set_default_timeout(self._settings.navigation_timeout_ms)
            self._page = self._pick_attached_page(self._context)
        return self._page

    def _connect_browser_with_retry(self) -> Browser:
        last_error: Exception | None = None
        endpoint = f"http://127.0.0.1:{self._remote_debugging_port}"
        for _ in range(10):
            try:
                assert self._playwright is not None
                return self._playwright.chromium.connect_over_cdp(endpoint)
            except Exception as exc:
                last_error = exc
                time.sleep(0.5)
        assert last_error is not None
        raise LoginFailedError(
            f"Could not attach to an operator Chrome session at {endpoint}. "
            "Start Chrome with --remote-debugging-port and log in to Coupang before running automation."
        ) from last_error

    def _attached_session_mode(self) -> str:
        return "attached_existing_cdp_session"

    def _close_local_resources(self) -> None:
        if self._browser is not None:
            try:
                self._browser.close()
            except Exception:
                pass
        try:
            self._close_playwright_context_manager()
        except Exception:
            pass
        self._playwright_cm = None
        self._playwright = None
        self._browser = None
        self._context = None
        self._page = None

    def _run_on_worker(self, operation: Callable[[], object]) -> object:
        if self._worker_thread is not None and threading.current_thread() is self._worker_thread:
            return operation()
        if self._worker_thread is None or self._worker_queue is None or not self._worker_thread.is_alive():
            self._worker_queue = queue.Queue()
            self._worker_thread = threading.Thread(
                target=self._worker_main,
                args=(self._worker_queue,),
                name="existing-cdp-playwright-worker",
                daemon=True,
            )
            self._worker_thread.start()
        response_queue: queue.Queue[tuple[bool, object]] = queue.Queue(maxsize=1)
        self._worker_queue.put((operation, response_queue))
        success, payload = response_queue.get()
        if success:
            return payload
        assert isinstance(payload, BaseException)
        raise payload

    def _worker_main(
        self,
        work_queue: queue.Queue[tuple[Callable[[], object] | None, queue.Queue[tuple[bool, object]] | None]],
    ) -> None:
        while True:
            operation, response_queue = work_queue.get()
            if operation is None:
                break
            try:
                result = operation()
            except BaseException as exc:
                assert response_queue is not None
                response_queue.put((False, exc))
            else:
                assert response_queue is not None
                response_queue.put((True, result))


class BrowserUseCoupangCartPage(ChromeCdpCoupangCartPage):
    """Real Chrome profile path aligned with browser-use's operator model."""

    def __init__(
        self,
        *,
        settings: PlaywrightCoupangSettings,
        browser_use_settings: BrowserUseSettings,
    ) -> None:
        super().__init__(
            settings=settings,
            cdp_settings=ChromeCdpSettings(
                chrome_user_data_dir=browser_use_settings.chrome_user_data_dir,
                chrome_profile_directory=browser_use_settings.chrome_profile_directory,
                remote_debugging_port=browser_use_settings.remote_debugging_port,
                copied_user_data_dir=browser_use_settings.copied_user_data_dir,
                chrome_binary_path=browser_use_settings.chrome_binary_path,
            ),
        )

    def _attached_session_mode(self) -> str:
        return "attached_browser_use_profile"
