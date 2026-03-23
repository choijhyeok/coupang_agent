"""Rule-based price judgment engine.

Compares the current product price against historical averages and recent
lows to produce a human-readable :class:`PriceAssessment` with a
:class:`PriceVerdict` (buy_now / reasonable / wait).
"""

from __future__ import annotations

from datetime import UTC, datetime

from .contracts import PriceAssessment, PriceHistory, PriceVerdict


# Thresholds (configurable via constructor)
_DEFAULT_BUY_NOW_AVG_DISCOUNT_PCT = 8.0    # ≥8% below average → buy_now
_DEFAULT_BUY_NOW_RECENT_LOW_PCT = 3.0      # within 3% of 30-day low → buy_now
_DEFAULT_WAIT_AVG_PREMIUM_PCT = 5.0        # ≥5% above average → wait


class PriceJudgmentEngine:
    """Stateless engine that transforms a :class:`PriceHistory` into a :class:`PriceAssessment`."""

    def __init__(
        self,
        *,
        buy_now_avg_discount_pct: float = _DEFAULT_BUY_NOW_AVG_DISCOUNT_PCT,
        buy_now_recent_low_pct: float = _DEFAULT_BUY_NOW_RECENT_LOW_PCT,
        wait_avg_premium_pct: float = _DEFAULT_WAIT_AVG_PREMIUM_PCT,
    ) -> None:
        self._buy_now_avg_discount_pct = buy_now_avg_discount_pct
        self._buy_now_recent_low_pct = buy_now_recent_low_pct
        self._wait_avg_premium_pct = wait_avg_premium_pct

    def assess(self, history: PriceHistory) -> PriceAssessment:
        current = history.current_price_krw
        avg = history.average_price_krw or current
        lowest = history.lowest_price_krw or current
        recent_low = history.recent_low_30d_krw

        # discount % vs average (positive = cheaper than average)
        discount_vs_avg = ((avg - current) / avg * 100) if avg > 0 else 0.0

        # discount % vs 30-day recent low (how close to recent low)
        discount_vs_recent_low: float | None = None
        if recent_low is not None and recent_low > 0:
            discount_vs_recent_low = ((current - recent_low) / recent_low * 100)

        verdict, reason = self._determine_verdict(
            current=current,
            avg=avg,
            lowest=lowest,
            recent_low=recent_low,
            discount_vs_avg=discount_vs_avg,
            discount_vs_recent_low=discount_vs_recent_low,
        )

        return PriceAssessment(
            product_id=history.product_id,
            product_name=history.product_name,
            current_price_krw=current,
            verdict=verdict,
            verdict_reason=reason,
            average_price_krw=avg,
            lowest_price_krw=lowest,
            recent_low_30d_krw=recent_low,
            discount_pct_vs_avg=round(discount_vs_avg, 1),
            discount_pct_vs_recent_low=(
                None if discount_vs_recent_low is None else round(discount_vs_recent_low, 1)
            ),
            source=history.source,
            assessed_at=datetime.now(UTC),
        )

    def _determine_verdict(
        self,
        *,
        current: int,
        avg: int,
        lowest: int,
        recent_low: int | None,
        discount_vs_avg: float,
        discount_vs_recent_low: float | None,
    ) -> tuple[PriceVerdict, str]:
        reasons: list[str] = []

        # Rule 1: Near or below historical lowest → strong buy signal
        if current <= lowest:
            return PriceVerdict.BUY_NOW, "역대 최저가와 같거나 더 낮습니다. 지금 사는 게 이득입니다."

        # Rule 2: Within threshold of 30-day recent low → buy_now
        if discount_vs_recent_low is not None and discount_vs_recent_low <= self._buy_now_recent_low_pct:
            reasons.append(
                f"최근 30일 최저가({_format_krw(recent_low)})와 "
                f"{abs(discount_vs_recent_low):.1f}% 차이로 거의 최저가 수준입니다."
            )

        # Rule 3: Significantly below average → buy_now
        if discount_vs_avg >= self._buy_now_avg_discount_pct:
            reasons.append(
                f"평균가({_format_krw(avg)}) 대비 {discount_vs_avg:.1f}% 저렴합니다."
            )

        if reasons:
            return PriceVerdict.BUY_NOW, " ".join(reasons) + " 지금 구매를 추천합니다."

        # Rule 4: Above average → wait
        if discount_vs_avg <= -self._wait_avg_premium_pct:
            premium = abs(discount_vs_avg)
            return (
                PriceVerdict.WAIT,
                f"평균가({_format_krw(avg)}) 대비 {premium:.1f}% 비쌉니다. "
                f"가격이 내릴 때까지 기다리는 게 나을 수 있습니다.",
            )

        # Fallback: reasonable
        return (
            PriceVerdict.REASONABLE,
            f"현재가({_format_krw(current)})는 평균가({_format_krw(avg)}) 근처로 적당한 가격입니다.",
        )


def _format_krw(value: int | None) -> str:
    if value is None:
        return "N/A"
    return f"{value:,}원"
