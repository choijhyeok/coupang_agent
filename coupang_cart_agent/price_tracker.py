"""Price comparison providers: Danawa (cross-mall), Lowchart & GenieAlert (Coupang history).

Each provider fetches price data for a Coupang product and normalizes it
into the common ``PriceHistory`` contract.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime
from typing import Protocol
from urllib.parse import parse_qs, urlparse

import httpx

from .contracts import PriceDataPoint, PriceHistory

logger = logging.getLogger(__name__)

_PRICE_INT_PATTERN = re.compile(r"[0-9][0-9,]+")

# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


class PriceHistoryProvider(Protocol):
    """Fetch price history for a Coupang product."""

    @property
    def source_name(self) -> str: ...

    def get_price_history(
        self, *, product_id: str, product_name: str, product_url: str = "",
    ) -> PriceHistory | None:
        """Return normalized history or ``None`` when the provider has no data."""
        ...


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8",
}


def _parse_price_int(text: str) -> int | None:
    match = _PRICE_INT_PATTERN.search(text)
    if match:
        return int(match.group().replace(",", ""))
    return None


def _extract_coupang_ids(product_url: str) -> tuple[str | None, str | None, str | None]:
    """Extract (productId, itemId, vendorItemId) from a Coupang product URL."""
    if not product_url:
        return None, None, None
    pid_match = re.search(r"/products/([^/?#]+)", product_url)
    product_id = pid_match.group(1) if pid_match else None
    parsed = urlparse(product_url)
    qs = parse_qs(parsed.query)
    item_id = qs.get("itemId", [None])[0]
    vendor_item_id = qs.get("vendorItemId", [None])[0]
    return product_id, item_id, vendor_item_id


# ---------------------------------------------------------------------------
# Provider: Danawa (danawa.com)
# ---------------------------------------------------------------------------


class DanawaProvider:
    """Search Danawa for cross-store price comparison data.

    Two-step approach:
    1. Search ``search.danawa.com`` by product name → find matching pcode
    2. POST to the price comparison AJAX endpoint → extract all mall prices
    """

    source_name = "danawa"

    def __init__(self, *, timeout_seconds: float = 15.0) -> None:
        self._timeout = timeout_seconds

    def get_price_history(
        self, *, product_id: str, product_name: str, product_url: str = "",
    ) -> PriceHistory | None:
        if not product_name:
            return None
        pcode = self._search_pcode(product_name)
        if pcode is None:
            logger.debug("danawa: no pcode found for %r", product_name)
            return None
        mall_prices = self._fetch_mall_prices(pcode)
        if not mall_prices:
            logger.debug("danawa: no mall prices for pcode=%s", pcode)
            return None
        return self._build_history(
            product_id=product_id,
            product_name=product_name,
            mall_prices=mall_prices,
        )

    # -- Step 1: search for pcode --

    def _search_pcode(self, product_name: str) -> str | None:
        """Search Danawa and return the pcode of the first matching product."""
        try:
            response = httpx.get(
                "https://search.danawa.com/dsearch.php",
                params={"k1": product_name},
                headers=_DEFAULT_HEADERS,
                timeout=self._timeout,
                follow_redirects=True,
            )
            if response.status_code != 200:
                return None
        except httpx.HTTPError as exc:
            logger.warning("danawa search error: %s", exc)
            return None

        match = re.search(r'pcode=(\d+)', response.text)
        return match.group(1) if match else None

    # -- Step 2: fetch mall prices via AJAX --

    def _fetch_mall_prices(self, pcode: str) -> dict[str, int]:
        """POST to the price comparison AJAX endpoint and parse mall→price pairs."""
        try:
            response = httpx.post(
                "https://prod.danawa.com/info/ajax/getAllPriceCompareMallList.ajax.php",
                data={"pcode": pcode},
                headers={
                    **_DEFAULT_HEADERS,
                    "Referer": f"https://prod.danawa.com/info/?pcode={pcode}",
                    "X-Requested-With": "XMLHttpRequest",
                },
                timeout=self._timeout,
            )
            if response.status_code != 200:
                return {}
        except httpx.HTTPError as exc:
            logger.warning("danawa ajax error for pcode=%s: %s", pcode, exc)
            return {}

        return self._parse_mall_prices(response.text)

    @staticmethod
    def _parse_mall_prices(html: str) -> dict[str, int]:
        """Extract {mall_name: price_krw} from the AJAX HTML response."""
        prices: dict[str, int] = {}
        # Pattern: alt="MALL_NAME" ... <strong>PRICE</strong>원
        # Each mall block has an img alt="몰이름" followed by a price
        for match in re.finditer(
            r'alt="([^"]+)".*?<a[^>]*class="[^"]*priceCompareBuyLink[^"]*"[^>]*>',
            html,
            re.DOTALL,
        ):
            mall_name = match.group(1).strip()
            # Find the nearest price after this mall block
            after = html[match.end():]
            price_match = re.search(r'([\d,]+)\s*원', after[:500])
            if price_match:
                price = int(price_match.group(1).replace(",", ""))
                if price > 0 and mall_name not in prices:
                    prices[mall_name] = price

        # Fallback: extract from <strong>PRICE</strong>원 patterns near mall refs
        if not prices:
            mall_blocks = re.finditer(r'alt="([^"]+)"[^>]*>', html)
            for mb in mall_blocks:
                mall_name = mb.group(1).strip()
                if not mall_name or len(mall_name) > 30:
                    continue
                after = html[mb.end():]
                price_match = re.search(r'([\d,]+)\s*원', after[:800])
                if price_match:
                    price = int(price_match.group(1).replace(",", ""))
                    if price > 0 and mall_name not in prices:
                        prices[mall_name] = price

        return prices

    @staticmethod
    def _build_history(
        *,
        product_id: str,
        product_name: str,
        mall_prices: dict[str, int],
    ) -> PriceHistory:
        all_prices = list(mall_prices.values())
        lowest = min(all_prices)
        highest = max(all_prices)
        average = sum(all_prices) // len(all_prices)

        # Use 쿠팡 price as current if available, otherwise the lowest
        coupang_price = mall_prices.get("쿠팡") or mall_prices.get("쿠팡 로켓배송")
        current = coupang_price if coupang_price else lowest

        now = datetime.now(UTC)
        price_points = [
            PriceDataPoint(price_krw=price, observed_at=now)
            for price in all_prices
        ]

        return PriceHistory(
            product_id=product_id,
            product_name=product_name,
            current_price_krw=current,
            average_price_krw=average,
            lowest_price_krw=lowest,
            highest_price_krw=highest,
            recent_low_30d_krw=None,
            price_points=price_points,
            source="danawa",
            confidence=0.8,
        )


# ---------------------------------------------------------------------------
# Provider: Lowchart (lowchart.com) — Coupang price history
# ---------------------------------------------------------------------------


class LowchartProvider:
    """Fetch Coupang price history from lowchart.com (SSR).

    URL format: ``https://www.lowchart.com/{productId}-{itemId}``
    Requires both productId and itemId from the Coupang product URL.
    """

    source_name = "lowchart"

    def __init__(self, *, timeout_seconds: float = 15.0) -> None:
        self._timeout = timeout_seconds

    def get_price_history(
        self, *, product_id: str, product_name: str, product_url: str = "",
    ) -> PriceHistory | None:
        pid, item_id, _ = _extract_coupang_ids(product_url)
        if not pid or not item_id:
            logger.debug("lowchart: missing itemId in product_url %s", product_url)
            return None
        url = f"https://www.lowchart.com/{pid}-{item_id}"
        try:
            response = httpx.get(
                url, headers=_DEFAULT_HEADERS, timeout=self._timeout, follow_redirects=True,
            )
            if response.status_code != 200:
                logger.debug("lowchart returned %d for %s", response.status_code, url)
                return None
        except httpx.HTTPError as exc:
            logger.warning("lowchart network error: %s", exc)
            return None

        return self._parse(response.text, product_id=product_id, product_name=product_name)

    @staticmethod
    def _parse(html: str, *, product_id: str, product_name: str) -> PriceHistory | None:
        current: int | None = None
        lowest: int | None = None
        highest: int | None = None

        # Title format: "상품명 - 17,900원 - 로우차트 - 쿠팡 가격 변동 추적"
        title_match = re.search(r"<title[^>]*>(.*?)</title>", html)
        if title_match:
            title_price = _parse_price_int(title_match.group(1))
            if title_price:
                current = title_price

        # Extract labeled prices: "현재", "최저", "최고"
        for kw, setter in [("현재", "current"), ("최저", "lowest"), ("최고", "highest")]:
            found = re.findall(rf"{kw}[^<]*?(\d{{1,3}}(?:,\d{{3}})+)", html)
            if found:
                val = int(found[0].replace(",", ""))
                if setter == "current":
                    current = current or val
                elif setter == "lowest":
                    lowest = val
                elif setter == "highest":
                    highest = val

        if current is None:
            return None

        return PriceHistory(
            product_id=product_id,
            product_name=product_name,
            current_price_krw=current,
            average_price_krw=(current + (lowest or current)) // 2,
            lowest_price_krw=lowest or current,
            highest_price_krw=highest or current,
            source="lowchart",
            confidence=0.9,
        )


# ---------------------------------------------------------------------------
# Provider: GenieAlert (geniealert.co.kr) — Coupang price history
# ---------------------------------------------------------------------------


class GenieAlertProvider:
    """Fetch Coupang price history from geniealert.co.kr (SSR).

    URL format: ``https://geniealert.co.kr/goods/detail/{productId}?itemId={itemId}&vendorItemId={vendorItemId}``
    """

    source_name = "geniealert"

    def __init__(self, *, timeout_seconds: float = 15.0) -> None:
        self._timeout = timeout_seconds

    def get_price_history(
        self, *, product_id: str, product_name: str, product_url: str = "",
    ) -> PriceHistory | None:
        pid, item_id, vendor_item_id = _extract_coupang_ids(product_url)
        if not pid or not item_id:
            logger.debug("geniealert: missing itemId in product_url %s", product_url)
            return None
        url = f"https://geniealert.co.kr/goods/detail/{pid}?itemId={item_id}"
        if vendor_item_id:
            url += f"&vendorItemId={vendor_item_id}"
        try:
            response = httpx.get(
                url, headers=_DEFAULT_HEADERS, timeout=self._timeout, follow_redirects=True,
            )
            if response.status_code != 200:
                logger.debug("geniealert returned %d for %s", response.status_code, url)
                return None
        except httpx.HTTPError as exc:
            logger.warning("geniealert network error: %s", exc)
            return None

        if len(response.text) < 15000:
            return None

        return self._parse(response.text, product_id=product_id, product_name=product_name)

    @staticmethod
    def _parse(html: str, *, product_id: str, product_name: str) -> PriceHistory | None:
        current: int | None = None
        lowest: int | None = None

        # Title format: "상품명 - 최저가 10,900원, 최저가 할인 알림"
        title_match = re.search(r"<title[^>]*>(.*?)</title>", html)
        if title_match:
            title_text = title_match.group(1)
            low_match = re.search(r"최저가\s*([\d,]+)원", title_text)
            if low_match:
                lowest = int(low_match.group(1).replace(",", ""))

        for kw, setter in [("현재", "current"), ("최저", "lowest")]:
            found = re.findall(rf"{kw}[^<]*?(\d{{1,3}}(?:,\d{{3}})+)", html)
            if found:
                val = int(found[0].replace(",", ""))
                if setter == "current":
                    current = current or val
                elif setter == "lowest":
                    lowest = lowest or val

        if current is None and lowest is None:
            return None

        effective_current = current or lowest or 0
        effective_lowest = lowest or current or 0

        return PriceHistory(
            product_id=product_id,
            product_name=product_name,
            current_price_krw=effective_current,
            average_price_krw=effective_current,
            lowest_price_krw=effective_lowest,
            highest_price_krw=effective_current,
            source="geniealert",
            confidence=0.8,
        )


# ---------------------------------------------------------------------------
# Aggregating multi-provider facade
# ---------------------------------------------------------------------------


class AggregatingPriceTracker:
    """Try ALL providers and collect successful results.

    Returns the first successful result via ``get_price_history`` for backward
    compatibility.  Use ``get_all_price_histories`` to fetch from all sources.
    """

    def __init__(self, providers: list[PriceHistoryProvider] | None = None) -> None:
        self._providers: list[PriceHistoryProvider] = providers or [
            DanawaProvider(),
            LowchartProvider(),
            GenieAlertProvider(),
        ]

    def get_price_history(
        self, *, product_id: str, product_name: str, product_url: str = "",
    ) -> PriceHistory | None:
        results = self.get_all_price_histories(
            product_id=product_id, product_name=product_name, product_url=product_url,
        )
        return results[0] if results else None

    def get_all_price_histories(
        self, *, product_id: str, product_name: str, product_url: str = "",
    ) -> list[PriceHistory]:
        results: list[PriceHistory] = []
        for provider in self._providers:
            try:
                result = provider.get_price_history(
                    product_id=product_id, product_name=product_name, product_url=product_url,
                )
                if result is not None:
                    logger.info(
                        "price data for %s from %s (current=%d, low=%d)",
                        product_id,
                        provider.source_name,
                        result.current_price_krw,
                        result.lowest_price_krw,
                    )
                    results.append(result)
            except Exception:
                logger.exception("provider %s failed for %s", provider.source_name, product_id)
        if not results:
            logger.info("no price data for %s from any provider", product_id)
        return results
