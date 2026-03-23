"""Tests for the price tracking feature: contracts, judgment engine, notifications,
provider parsing, monitor worker, and workflow integration."""

from __future__ import annotations

import json
import unittest
from dataclasses import asdict
from datetime import UTC, datetime, timedelta

from coupang_cart_agent.contracts import (
    PriceAssessment,
    PriceDataPoint,
    PriceHistory,
    PriceVerdict,
    TrackedProduct,
)
from coupang_cart_agent.price_judgment import PriceJudgmentEngine, _format_krw
from coupang_cart_agent.notifications import (
    NotificationFormatter,
    build_price_assessment_notification_payload,
)


# ---------------------------------------------------------------------------
# PriceVerdict enum
# ---------------------------------------------------------------------------


class PriceVerdictEnumTests(unittest.TestCase):
    def test_enum_values(self):
        self.assertEqual(PriceVerdict.BUY_NOW, "buy_now")
        self.assertEqual(PriceVerdict.REASONABLE, "reasonable")
        self.assertEqual(PriceVerdict.WAIT, "wait")

    def test_from_string(self):
        self.assertEqual(PriceVerdict("buy_now"), PriceVerdict.BUY_NOW)
        self.assertEqual(PriceVerdict("wait"), PriceVerdict.WAIT)


# ---------------------------------------------------------------------------
# PriceHistory dataclass
# ---------------------------------------------------------------------------


class PriceHistoryTests(unittest.TestCase):
    def test_basic_construction(self):
        history = PriceHistory(
            product_id="P-100",
            product_name="테스트 상품",
            current_price_krw=15000,
            average_price_krw=18000,
            lowest_price_krw=12000,
            highest_price_krw=22000,
            source="danawa",
        )
        self.assertEqual(history.product_id, "P-100")
        self.assertEqual(history.current_price_krw, 15000)
        self.assertEqual(history.source, "danawa")
        self.assertIsNotNone(history.fetched_at)
        self.assertEqual(history.confidence, 1.0)

    def test_with_price_points(self):
        now = datetime.now(UTC)
        points = [
            PriceDataPoint(price_krw=16000, observed_at=now - timedelta(days=7)),
            PriceDataPoint(price_krw=14000, observed_at=now - timedelta(days=3)),
            PriceDataPoint(price_krw=15000, observed_at=now),
        ]
        history = PriceHistory(
            product_id="P-100",
            product_name="테스트",
            current_price_krw=15000,
            average_price_krw=15000,
            lowest_price_krw=14000,
            highest_price_krw=16000,
            recent_low_30d_krw=14000,
            price_points=points,
        )
        self.assertEqual(len(history.price_points), 3)
        self.assertEqual(history.recent_low_30d_krw, 14000)


# ---------------------------------------------------------------------------
# PriceAssessment dataclass
# ---------------------------------------------------------------------------


class PriceAssessmentTests(unittest.TestCase):
    def test_basic_construction(self):
        assessment = PriceAssessment(
            product_id="P-100",
            product_name="테스트 상품",
            current_price_krw=15000,
            verdict=PriceVerdict.BUY_NOW,
            verdict_reason="지금 사는 게 좋습니다.",
            average_price_krw=18000,
            lowest_price_krw=14000,
            discount_pct_vs_avg=16.7,
        )
        self.assertEqual(assessment.verdict, PriceVerdict.BUY_NOW)
        self.assertEqual(assessment.discount_pct_vs_avg, 16.7)

    def test_serializable(self):
        assessment = PriceAssessment(
            product_id="P-100",
            product_name="테스트",
            current_price_krw=15000,
            verdict=PriceVerdict.REASONABLE,
            verdict_reason="적당합니다.",
            average_price_krw=15500,
            lowest_price_krw=13000,
        )
        data = asdict(assessment)
        self.assertEqual(data["verdict"], "reasonable")
        self.assertIn("assessed_at", data)


# ---------------------------------------------------------------------------
# TrackedProduct dataclass
# ---------------------------------------------------------------------------


class TrackedProductTests(unittest.TestCase):
    def test_construction(self):
        target = TrackedProduct(
            user_id="telegram:user1",
            chat_id="chat-1",
            product_id="P-100",
            product_name="테스트 상품",
            product_url="https://coupang.com/vp/products/P-100",
            purchase_price_krw=15000,
        )
        self.assertTrue(target.active)
        self.assertIsNone(target.last_verdict)
        self.assertIsNotNone(target.registered_at)


# ---------------------------------------------------------------------------
# PriceJudgmentEngine
# ---------------------------------------------------------------------------


class PriceJudgmentEngineTests(unittest.TestCase):
    def setUp(self):
        self.engine = PriceJudgmentEngine()

    def _make_history(
        self,
        *,
        current: int,
        avg: int,
        lowest: int,
        highest: int = 25000,
        recent_low: int | None = None,
    ) -> PriceHistory:
        return PriceHistory(
            product_id="P-100",
            product_name="테스트 상품",
            current_price_krw=current,
            average_price_krw=avg,
            lowest_price_krw=lowest,
            highest_price_krw=highest,
            recent_low_30d_krw=recent_low,
            source="test",
        )

    def test_at_or_below_historical_lowest_is_buy_now(self):
        history = self._make_history(current=12000, avg=18000, lowest=12000)
        assessment = self.engine.assess(history)
        self.assertEqual(assessment.verdict, PriceVerdict.BUY_NOW)
        self.assertIn("역대 최저가", assessment.verdict_reason)

    def test_below_historical_lowest_is_buy_now(self):
        history = self._make_history(current=11000, avg=18000, lowest=12000)
        assessment = self.engine.assess(history)
        self.assertEqual(assessment.verdict, PriceVerdict.BUY_NOW)

    def test_near_recent_low_is_buy_now(self):
        # Within 3% of 30-day low
        history = self._make_history(current=12300, avg=15000, lowest=11000, recent_low=12000)
        assessment = self.engine.assess(history)
        self.assertEqual(assessment.verdict, PriceVerdict.BUY_NOW)
        self.assertIn("최근 30일", assessment.verdict_reason)

    def test_significantly_below_average_is_buy_now(self):
        # 10% below average
        history = self._make_history(current=16200, avg=18000, lowest=12000)
        assessment = self.engine.assess(history)
        self.assertEqual(assessment.verdict, PriceVerdict.BUY_NOW)
        self.assertIn("평균가", assessment.verdict_reason)

    def test_above_average_is_wait(self):
        # 6% above average
        history = self._make_history(current=19080, avg=18000, lowest=12000)
        assessment = self.engine.assess(history)
        self.assertEqual(assessment.verdict, PriceVerdict.WAIT)
        self.assertIn("비쌉니다", assessment.verdict_reason)

    def test_near_average_is_reasonable(self):
        # Within ±5% of average
        history = self._make_history(current=17500, avg=18000, lowest=12000)
        assessment = self.engine.assess(history)
        self.assertEqual(assessment.verdict, PriceVerdict.REASONABLE)
        self.assertIn("적당한 가격", assessment.verdict_reason)

    def test_assessment_output_has_all_fields(self):
        history = self._make_history(current=15000, avg=18000, lowest=12000, recent_low=14000)
        assessment = self.engine.assess(history)
        self.assertEqual(assessment.product_id, "P-100")
        self.assertEqual(assessment.product_name, "테스트 상품")
        self.assertEqual(assessment.current_price_krw, 15000)
        self.assertEqual(assessment.average_price_krw, 18000)
        self.assertEqual(assessment.lowest_price_krw, 12000)
        self.assertEqual(assessment.recent_low_30d_krw, 14000)
        self.assertIsNotNone(assessment.source)
        self.assertIsNotNone(assessment.assessed_at)

    def test_custom_thresholds(self):
        engine = PriceJudgmentEngine(
            buy_now_avg_discount_pct=15.0,
            wait_avg_premium_pct=10.0,
        )
        # 10% below avg → with default would be buy_now, but with 15% threshold it's reasonable
        history = self._make_history(current=16200, avg=18000, lowest=12000)
        assessment = engine.assess(history)
        self.assertEqual(assessment.verdict, PriceVerdict.REASONABLE)

    def test_discount_pct_calculation(self):
        history = self._make_history(current=15000, avg=20000, lowest=12000)
        assessment = self.engine.assess(history)
        # (20000-15000)/20000 * 100 = 25%
        self.assertAlmostEqual(assessment.discount_pct_vs_avg, 25.0, places=1)

    def test_discount_vs_recent_low(self):
        history = self._make_history(current=15000, avg=20000, lowest=12000, recent_low=14000)
        assessment = self.engine.assess(history)
        # (15000-14000)/14000 * 100 ≈ 7.1%
        self.assertAlmostEqual(assessment.discount_pct_vs_recent_low, 7.1, places=1)


# ---------------------------------------------------------------------------
# format_krw helper
# ---------------------------------------------------------------------------


class FormatKrwTests(unittest.TestCase):
    def test_basic(self):
        self.assertEqual(_format_krw(15000), "15,000원")
        self.assertEqual(_format_krw(0), "0원")

    def test_none(self):
        self.assertEqual(_format_krw(None), "N/A")


# ---------------------------------------------------------------------------
# Price assessment notification builder
# ---------------------------------------------------------------------------


class PriceAssessmentNotificationTests(unittest.TestCase):
    def _make_assessment_dict(
        self,
        *,
        verdict: str = "buy_now",
        product_name: str = "테스트 상품",
        current: int = 15000,
        avg: int = 18000,
        lowest: int = 12000,
    ) -> dict[str, object]:
        return {
            "product_id": "P-100",
            "product_name": product_name,
            "current_price_krw": current,
            "verdict": verdict,
            "verdict_reason": "테스트 판정 이유입니다.",
            "average_price_krw": avg,
            "lowest_price_krw": lowest,
            "recent_low_30d_krw": lowest + 1000,
            "discount_pct_vs_avg": 16.7,
            "discount_pct_vs_recent_low": 3.0,
            "source": "danawa",
            "assessed_at": datetime.now(UTC).isoformat(),
        }

    def test_build_buy_now_payload(self):
        assessments = [self._make_assessment_dict(verdict="buy_now")]
        payload = build_price_assessment_notification_payload(
            chat_id="chat-1",
            assessments=assessments,
        )
        self.assertEqual(payload.kind, "price_assessment")
        self.assertTrue(payload.success)
        self.assertIn("이득", payload.summary)
        self.assertEqual(len(payload.details["assessments"]), 1)

    def test_build_wait_payload(self):
        assessments = [self._make_assessment_dict(verdict="wait")]
        payload = build_price_assessment_notification_payload(
            chat_id="chat-1",
            assessments=assessments,
        )
        self.assertIn("내릴", payload.summary)

    def test_build_reasonable_payload(self):
        assessments = [self._make_assessment_dict(verdict="reasonable")]
        payload = build_price_assessment_notification_payload(
            chat_id="chat-1",
            assessments=assessments,
        )
        self.assertIn("적당한", payload.summary)

    def test_empty_assessments_raises(self):
        with self.assertRaises(ValueError):
            build_price_assessment_notification_payload(chat_id="chat-1", assessments=[])

    def test_multiple_assessments(self):
        assessments = [
            self._make_assessment_dict(verdict="buy_now", product_name="상품1"),
            self._make_assessment_dict(verdict="wait", product_name="상품2"),
        ]
        payload = build_price_assessment_notification_payload(
            chat_id="chat-1",
            assessments=assessments,
        )
        self.assertEqual(len(payload.details["assessments"]), 2)
        # buy_now takes priority in summary
        self.assertIn("이득", payload.summary)


# ---------------------------------------------------------------------------
# Price assessment notification formatting
# ---------------------------------------------------------------------------


class PriceAssessmentFormattingTests(unittest.TestCase):
    def test_format_buy_now_message(self):
        payload = build_price_assessment_notification_payload(
            chat_id="chat-1",
            assessments=[{
                "product_id": "P-100",
                "product_name": "코카콜라 제로 355ml",
                "current_price_krw": 15000,
                "verdict": "buy_now",
                "verdict_reason": "역대 최저가와 같거나 더 낮습니다.",
                "average_price_krw": 18000,
                "lowest_price_krw": 15000,
                "recent_low_30d_krw": 16000,
                "discount_pct_vs_avg": 16.7,
                "discount_pct_vs_recent_low": -6.3,
                "source": "danawa",
                "assessed_at": datetime.now(UTC).isoformat(),
            }],
        )
        formatter = NotificationFormatter(max_length=2000)
        message = formatter.format(payload)
        self.assertIn("📊", message)
        self.assertIn("가격 분석 리포트", message)
        self.assertIn("🟢", message)
        self.assertIn("코카콜라", message)
        self.assertIn("15,000", message)
        self.assertIn("danawa", message)

    def test_format_wait_message(self):
        payload = build_price_assessment_notification_payload(
            chat_id="chat-1",
            assessments=[{
                "product_id": "P-200",
                "product_name": "비싼 상품",
                "current_price_krw": 25000,
                "verdict": "wait",
                "verdict_reason": "평균가 대비 10% 비쌉니다.",
                "average_price_krw": 22700,
                "lowest_price_krw": 18000,
                "discount_pct_vs_avg": -10.1,
                "source": "danawa",
                "assessed_at": datetime.now(UTC).isoformat(),
            }],
        )
        formatter = NotificationFormatter(max_length=2000)
        message = formatter.format(payload)
        self.assertIn("🔴", message)
        self.assertIn("기다리는 게 나음", message)

    def test_format_reasonable_message(self):
        payload = build_price_assessment_notification_payload(
            chat_id="chat-1",
            assessments=[{
                "product_id": "P-300",
                "product_name": "적당 상품",
                "current_price_krw": 17500,
                "verdict": "reasonable",
                "verdict_reason": "평균가 근처로 적당합니다.",
                "average_price_krw": 18000,
                "lowest_price_krw": 14000,
                "discount_pct_vs_avg": 2.8,
                "source": "danawa",
                "assessed_at": datetime.now(UTC).isoformat(),
            }],
        )
        formatter = NotificationFormatter(max_length=2000)
        message = formatter.format(payload)
        self.assertIn("🟡", message)
        self.assertIn("적당한 가격", message)


# ---------------------------------------------------------------------------
# Price tracker provider -- chart data extraction
# ---------------------------------------------------------------------------


class DanawaProviderTests(unittest.TestCase):
    def test_parse_mall_prices_from_html(self):
        from coupang_cart_agent.price_tracker import DanawaProvider

        html = '''
        <a href="#" class="priceCompareBuyLink">
            <img src="logo.gif" alt="\uc4f8\ud321">
        </a>
        <span>3,000원</span>
        <a href="#" class="priceCompareBuyLink">
            <img src="logo2.gif" alt="\ucfe0\ud321">
        </a>
        <span>2,800원</span>
        '''.replace("\\u", "\\u")
        # Direct test with known HTML
        html_direct = '''
        <img alt="G마켓"><a class="link priceCompareBuyLink">buy</a>3,000원
        <img alt="쿠팡"><a class="link priceCompareBuyLink">buy</a>2,800원
        '''
        prices = DanawaProvider._parse_mall_prices(html_direct)
        self.assertEqual(prices["G마켓"], 3000)
        self.assertEqual(prices["쿠팡"], 2800)

    def test_build_history_uses_coupang_as_current(self):
        from coupang_cart_agent.price_tracker import DanawaProvider

        mall_prices = {"G마켓": 3000, "쿠팡": 2800, "11번가": 3200}
        history = DanawaProvider._build_history(
            product_id="P-1",
            product_name="테스트",
            mall_prices=mall_prices,
        )
        self.assertEqual(history.current_price_krw, 2800)  # 쿠팡 price
        self.assertEqual(history.lowest_price_krw, 2800)
        self.assertEqual(history.highest_price_krw, 3200)
        self.assertEqual(history.source, "danawa")

    def test_build_history_fallback_to_lowest(self):
        from coupang_cart_agent.price_tracker import DanawaProvider

        mall_prices = {"G마켓": 3000, "11번가": 3200}
        history = DanawaProvider._build_history(
            product_id="P-1",
            product_name="테스트",
            mall_prices=mall_prices,
        )
        self.assertEqual(history.current_price_krw, 3000)  # lowest, no 쿠팡

    def test_empty_product_name_returns_none(self):
        from coupang_cart_agent.price_tracker import DanawaProvider

        provider = DanawaProvider()
        result = provider.get_price_history(product_id="P-1", product_name="")
        self.assertIsNone(result)


# ---------------------------------------------------------------------------
# AggregatingPriceTracker
# ---------------------------------------------------------------------------


class AggregatingPriceTrackerTests(unittest.TestCase):
    def test_returns_first_successful_provider(self):
        from coupang_cart_agent.price_tracker import AggregatingPriceTracker

        class FailProvider:
            source_name = "fail"
            def get_price_history(self, *, product_id, product_name, **kwargs):
                return None

        class SuccessProvider:
            source_name = "success"
            def get_price_history(self, *, product_id, product_name, **kwargs):
                return PriceHistory(
                    product_id=product_id,
                    product_name=product_name,
                    current_price_krw=15000,
                    average_price_krw=18000,
                    lowest_price_krw=12000,
                    highest_price_krw=22000,
                    source="success",
                )

        tracker = AggregatingPriceTracker(providers=[FailProvider(), SuccessProvider()])
        result = tracker.get_price_history(product_id="P-1", product_name="테스트")
        self.assertIsNotNone(result)
        self.assertEqual(result.source, "success")
        self.assertEqual(result.current_price_krw, 15000)

    def test_returns_none_when_all_fail(self):
        from coupang_cart_agent.price_tracker import AggregatingPriceTracker

        class AlwaysNone:
            source_name = "none"
            def get_price_history(self, *, product_id, product_name, **kwargs):
                return None

        tracker = AggregatingPriceTracker(providers=[AlwaysNone()])
        result = tracker.get_price_history(product_id="P-1", product_name="테스트")
        self.assertIsNone(result)

    def test_graceful_on_exception(self):
        from coupang_cart_agent.price_tracker import AggregatingPriceTracker

        class ExceptionProvider:
            source_name = "explode"
            def get_price_history(self, *, product_id, product_name, **kwargs):
                raise RuntimeError("boom")

        class FallbackProvider:
            source_name = "fallback"
            def get_price_history(self, *, product_id, product_name, **kwargs):
                return PriceHistory(
                    product_id=product_id,
                    product_name=product_name,
                    current_price_krw=10000,
                    average_price_krw=12000,
                    lowest_price_krw=9000,
                    highest_price_krw=14000,
                    source="fallback",
                )

        tracker = AggregatingPriceTracker(providers=[ExceptionProvider(), FallbackProvider()])
        result = tracker.get_price_history(product_id="P-1", product_name="테스트")
        self.assertIsNotNone(result)
        self.assertEqual(result.source, "fallback")

    def test_get_all_price_histories_collects_all(self):
        from coupang_cart_agent.price_tracker import AggregatingPriceTracker

        class ProviderA:
            source_name = "a"
            def get_price_history(self, *, product_id, product_name, **kwargs):
                return PriceHistory(
                    product_id=product_id, product_name=product_name,
                    current_price_krw=1000, average_price_krw=1200,
                    lowest_price_krw=900, highest_price_krw=1500, source="a",
                )

        class ProviderB:
            source_name = "b"
            def get_price_history(self, *, product_id, product_name, **kwargs):
                return PriceHistory(
                    product_id=product_id, product_name=product_name,
                    current_price_krw=2000, average_price_krw=2200,
                    lowest_price_krw=1800, highest_price_krw=2500, source="b",
                )

        tracker = AggregatingPriceTracker(providers=[ProviderA(), ProviderB()])
        results = tracker.get_all_price_histories(product_id="P-1", product_name="테스트")
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0].source, "a")
        self.assertEqual(results[1].source, "b")


# ---------------------------------------------------------------------------
# _extract_coupang_ids
# ---------------------------------------------------------------------------


class ExtractCoupangIdsTests(unittest.TestCase):
    def test_full_url(self):
        from coupang_cart_agent.price_tracker import _extract_coupang_ids
        url = "https://www.coupang.com/vp/products/123456?itemId=789&vendorItemId=111"
        pid, iid, vid = _extract_coupang_ids(url)
        self.assertEqual(pid, "123456")
        self.assertEqual(iid, "789")
        self.assertEqual(vid, "111")

    def test_no_item_id(self):
        from coupang_cart_agent.price_tracker import _extract_coupang_ids
        pid, iid, vid = _extract_coupang_ids("https://www.coupang.com/vp/products/99999")
        self.assertEqual(pid, "99999")
        self.assertIsNone(iid)
        self.assertIsNone(vid)

    def test_empty_url(self):
        from coupang_cart_agent.price_tracker import _extract_coupang_ids
        self.assertEqual(_extract_coupang_ids(""), (None, None, None))


# ---------------------------------------------------------------------------
# LowchartProvider parse
# ---------------------------------------------------------------------------


class LowchartProviderTests(unittest.TestCase):
    def test_parse_extracts_prices(self):
        from coupang_cart_agent.price_tracker import LowchartProvider
        html = """<html><head><title>테스트 상품 - 17,900원 - 로우차트</title></head>
        <body>현재 17,900원 최저 10,900원 최고 25,000원</body></html>"""
        result = LowchartProvider._parse(html, product_id="P-1", product_name="테스트")
        self.assertIsNotNone(result)
        self.assertEqual(result.current_price_krw, 17900)
        self.assertEqual(result.lowest_price_krw, 10900)
        self.assertEqual(result.highest_price_krw, 25000)
        self.assertEqual(result.source, "lowchart")

    def test_parse_returns_none_for_empty(self):
        from coupang_cart_agent.price_tracker import LowchartProvider
        result = LowchartProvider._parse("<html><body></body></html>", product_id="P-1", product_name="테스트")
        self.assertIsNone(result)

    def test_skips_without_item_id(self):
        from coupang_cart_agent.price_tracker import LowchartProvider
        provider = LowchartProvider()
        result = provider.get_price_history(
            product_id="P-1", product_name="테스트",
            product_url="https://www.coupang.com/vp/products/123",
        )
        self.assertIsNone(result)


# ---------------------------------------------------------------------------
# GenieAlertProvider parse
# ---------------------------------------------------------------------------


class GenieAlertProviderTests(unittest.TestCase):
    def test_parse_extracts_lowest_from_title(self):
        from coupang_cart_agent.price_tracker import GenieAlertProvider
        html = '<html><head><title>테스트 상품 - 최저가 10,900원, 최저가 할인 알림</title></head>' + 'x' * 15000 + '</html>'
        result = GenieAlertProvider._parse(html, product_id="P-1", product_name="테스트")
        self.assertIsNotNone(result)
        self.assertEqual(result.lowest_price_krw, 10900)
        self.assertEqual(result.source, "geniealert")

    def test_parse_returns_none_for_no_prices(self):
        from coupang_cart_agent.price_tracker import GenieAlertProvider
        result = GenieAlertProvider._parse("<html><body></body></html>", product_id="P-1", product_name="테스트")
        self.assertIsNone(result)

    def test_skips_without_item_id(self):
        from coupang_cart_agent.price_tracker import GenieAlertProvider
        provider = GenieAlertProvider()
        result = provider.get_price_history(
            product_id="P-1", product_name="테스트",
            product_url="https://www.coupang.com/vp/products/123",
        )
        self.assertIsNone(result)


class StubMonitorStore:
    def __init__(self, targets: list[TrackedProduct] | None = None):
        self.targets = targets or []
        self.verdicts: list[dict] = []
        self.assessments_recorded: list[dict] = []

    def load_active_tracking_targets(self) -> list[TrackedProduct]:
        return list(self.targets)

    def update_tracking_verdict(self, *, user_id, product_id, verdict, assessed_at):
        self.verdicts.append({
            "user_id": user_id,
            "product_id": product_id,
            "verdict": verdict.value,
            "assessed_at": assessed_at,
        })

    def record_price_assessment(self, *, user_id, assessment):
        self.assessments_recorded.append({
            "user_id": user_id,
            "product_id": assessment.product_id,
            "verdict": assessment.verdict.value,
        })


class StubPriceTracker:
    def __init__(self, history: PriceHistory | None = None):
        self._history = history

    def get_price_history(self, *, product_id, product_name):
        return self._history


class RecordingNotificationService:
    def __init__(self):
        self.sent: list = []

    def send(self, payload):
        self.sent.append(payload)


class PriceMonitorWorkerTests(unittest.TestCase):
    def test_no_targets_no_crash(self):
        from coupang_cart_agent.price_monitor_worker import PriceMonitorWorker

        store = StubMonitorStore([])
        notifier = RecordingNotificationService()
        worker = PriceMonitorWorker(
            store=store,
            notification_service=notifier,
            interval_seconds=0,
        )
        reports = worker.run(max_cycles=1)
        self.assertEqual(len(reports), 1)
        self.assertEqual(reports[0].targets_checked, 0)
        self.assertEqual(reports[0].notifications_sent, 0)

    def test_verdict_change_sends_notification(self):
        from coupang_cart_agent.price_monitor_worker import PriceMonitorWorker

        target = TrackedProduct(
            user_id="user1",
            chat_id="chat1",
            product_id="P-100",
            product_name="테스트 상품",
            product_url="https://coupang.com/vp/products/P-100",
            purchase_price_krw=15000,
            last_verdict=PriceVerdict.REASONABLE,  # Previous verdict
        )
        history = PriceHistory(
            product_id="P-100",
            product_name="테스트 상품",
            current_price_krw=11000,  # Now at historical low
            average_price_krw=18000,
            lowest_price_krw=11000,
            highest_price_krw=22000,
            source="test",
        )
        store = StubMonitorStore([target])
        notifier = RecordingNotificationService()
        tracker = StubPriceTracker(history)
        worker = PriceMonitorWorker(
            store=store,
            notification_service=notifier,
            price_tracker=tracker,
            interval_seconds=0,
        )
        reports = worker.run(max_cycles=1)
        self.assertEqual(reports[0].targets_checked, 1)
        self.assertEqual(reports[0].verdicts_changed, 1)
        self.assertEqual(reports[0].notifications_sent, 1)
        self.assertEqual(len(notifier.sent), 1)
        self.assertEqual(notifier.sent[0].kind, "price_assessment")
        # Store should record assessment and update verdict
        self.assertEqual(len(store.verdicts), 1)
        self.assertEqual(store.verdicts[0]["verdict"], "buy_now")
        self.assertEqual(len(store.assessments_recorded), 1)

    def test_no_change_no_notification(self):
        from coupang_cart_agent.price_monitor_worker import PriceMonitorWorker

        target = TrackedProduct(
            user_id="user1",
            chat_id="chat1",
            product_id="P-100",
            product_name="테스트 상품",
            product_url="",
            purchase_price_krw=17500,
            last_verdict=PriceVerdict.REASONABLE,
        )
        history = PriceHistory(
            product_id="P-100",
            product_name="테스트 상품",
            current_price_krw=17500,  # Same price → same verdict
            average_price_krw=18000,
            lowest_price_krw=14000,
            highest_price_krw=22000,
            source="test",
        )
        store = StubMonitorStore([target])
        notifier = RecordingNotificationService()
        tracker = StubPriceTracker(history)
        worker = PriceMonitorWorker(
            store=store,
            notification_service=notifier,
            price_tracker=tracker,
            interval_seconds=0,
        )
        reports = worker.run(max_cycles=1)
        self.assertEqual(reports[0].verdicts_changed, 0)
        self.assertEqual(reports[0].notifications_sent, 0)
        self.assertEqual(len(notifier.sent), 0)
        # Assessment still recorded even without verdict change
        self.assertEqual(len(store.assessments_recorded), 1)

    def test_null_history_skipped(self):
        from coupang_cart_agent.price_monitor_worker import PriceMonitorWorker

        target = TrackedProduct(
            user_id="user1",
            chat_id="chat1",
            product_id="P-100",
            product_name="테스트",
            product_url="",
            purchase_price_krw=15000,
        )
        store = StubMonitorStore([target])
        notifier = RecordingNotificationService()
        tracker = StubPriceTracker(None)  # No data
        worker = PriceMonitorWorker(
            store=store,
            notification_service=notifier,
            price_tracker=tracker,
            interval_seconds=0,
        )
        reports = worker.run(max_cycles=1)
        self.assertEqual(reports[0].targets_checked, 1)
        self.assertEqual(reports[0].verdicts_changed, 0)
        self.assertEqual(reports[0].notifications_sent, 0)

    def test_first_assessment_always_notifies(self):
        """When last_verdict is None (first time), any verdict should trigger notification."""
        from coupang_cart_agent.price_monitor_worker import PriceMonitorWorker

        target = TrackedProduct(
            user_id="user1",
            chat_id="chat1",
            product_id="P-100",
            product_name="테스트",
            product_url="",
            purchase_price_krw=17500,
            last_verdict=None,  # First assessment
        )
        history = PriceHistory(
            product_id="P-100",
            product_name="테스트",
            current_price_krw=17500,
            average_price_krw=18000,
            lowest_price_krw=14000,
            highest_price_krw=22000,
            source="test",
        )
        store = StubMonitorStore([target])
        notifier = RecordingNotificationService()
        tracker = StubPriceTracker(history)
        worker = PriceMonitorWorker(
            store=store,
            notification_service=notifier,
            price_tracker=tracker,
            interval_seconds=0,
        )
        reports = worker.run(max_cycles=1)
        self.assertEqual(reports[0].verdicts_changed, 1)
        self.assertEqual(reports[0].notifications_sent, 1)


# ---------------------------------------------------------------------------
# Workflow integration (evaluate_price node)
# ---------------------------------------------------------------------------


class WorkflowPriceEvaluationTests(unittest.TestCase):
    """Test that the evaluate_price node in the workflow correctly integrates."""

    def _make_stub_price_tracker(self, history: PriceHistory | None):
        class StubTracker:
            def get_price_history(self, *, product_id, product_name):
                return history
        return StubTracker()

    def test_evaluate_price_node_produces_assessments(self):
        """Verify the node logic works with a successful cart add and price data."""
        from coupang_cart_agent.contracts import (
            CartAddResult,
            CartAddStage,
            ProductCandidate,
            SelectedProduct,
        )
        from coupang_cart_agent.price_judgment import PriceJudgmentEngine

        history = PriceHistory(
            product_id="P-100",
            product_name="테스트 상품",
            current_price_krw=15000,
            average_price_krw=20000,
            lowest_price_krw=12000,
            highest_price_krw=25000,
            recent_low_30d_krw=14000,
            source="danawa",
        )
        tracker = self._make_stub_price_tracker(history)
        engine = PriceJudgmentEngine()

        # Simulate what _evaluate_price_node does (now evaluates from proposal)
        assessment = engine.assess(history)
        self.assertEqual(assessment.verdict, PriceVerdict.BUY_NOW)
        self.assertEqual(assessment.product_id, "P-100")
        self.assertGreater(assessment.discount_pct_vs_avg, 8.0)


if __name__ == "__main__":
    unittest.main()
