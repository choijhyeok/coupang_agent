from __future__ import annotations

import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
import re

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
from .contracts import SelectedProduct


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
        if self._playwright_cm is not None:
            self._playwright_cm.__exit__(None, None, None)
        self._playwright_cm = None
        self._playwright = None
        self._browser = None
        self._context = None
        self._page = None

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

    def _is_logged_in(self, page: Page | None = None) -> bool:
        active_page = page or self._page_object()
        if not active_page.url or active_page.url == "about:blank":
            return False
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
            if self._playwright_cm is not None:
                self._playwright_cm.__exit__(None, None, None)
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
            self._page = self._context.pages[0] if self._context.pages else self._context.new_page()
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
                "Sessions",
                "Session Storage",
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

    def close(self) -> None:
        if self._browser is not None:
            self._browser.close()
        if self._playwright_cm is not None:
            self._playwright_cm.__exit__(None, None, None)
        self._playwright_cm = None
        self._playwright = None
        self._browser = None
        self._context = None
        self._page = None

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
            self._page = self._context.pages[0] if self._context.pages else self._context.new_page()
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
