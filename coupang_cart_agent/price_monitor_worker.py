"""Periodic price monitoring worker.

Runs on a configurable interval (default: 3 minutes for demo).  Each cycle
it loads active tracking targets from PostgreSQL, fetches fresh price history,
re-evaluates the verdict, and sends a Telegram notification only when the
verdict has changed since the last assessment.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Callable

from .contracts import PriceAssessment, PriceVerdict, TrackedProduct
from .notifications import (
    NotificationPayload,
    build_price_assessment_notification_payload,
)
from .price_judgment import PriceJudgmentEngine
from .price_tracker import AggregatingPriceTracker, PriceHistoryProvider
from .services import NotificationService

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class PriceMonitorCycleReport:
    cycle: int
    targets_checked: int
    verdicts_changed: int
    notifications_sent: int
    errors: int
    cycle_duration_ms: float

    def as_dict(self) -> dict[str, object]:
        return {
            "cycle": self.cycle,
            "targets_checked": self.targets_checked,
            "verdicts_changed": self.verdicts_changed,
            "notifications_sent": self.notifications_sent,
            "errors": self.errors,
            "cycle_duration_ms": self.cycle_duration_ms,
        }


class PriceMonitorStore:
    """Subset of OperationalStore used by the price monitor."""

    def load_active_tracking_targets(self) -> list[TrackedProduct]: ...
    def update_tracking_verdict(self, *, user_id: str, product_id: str, verdict: PriceVerdict, assessed_at: datetime) -> None: ...
    def record_price_assessment(self, *, user_id: str, assessment: PriceAssessment) -> None: ...


class PriceMonitorWorker:
    """Always-on worker that periodically checks prices and notifies on verdict changes."""

    def __init__(
        self,
        *,
        store: PriceMonitorStore,
        notification_service: NotificationService,
        price_tracker: AggregatingPriceTracker | None = None,
        judgment_engine: PriceJudgmentEngine | None = None,
        interval_seconds: float = 180.0,
        logger_fn: Callable[[dict[str, object]], None] | None = None,
    ) -> None:
        self._store = store
        self._notification_service = notification_service
        self._price_tracker = price_tracker or AggregatingPriceTracker()
        self._judgment_engine = judgment_engine or PriceJudgmentEngine()
        self._interval_seconds = interval_seconds
        self._logger_fn = logger_fn or (lambda d: logger.info("%s", d))

    def run(self, *, max_cycles: int | None = None) -> list[PriceMonitorCycleReport]:
        reports: list[PriceMonitorCycleReport] = []
        cycle = 0
        while max_cycles is None or cycle < max_cycles:
            cycle += 1
            report = self.run_cycle(cycle=cycle)
            reports.append(report)
            self._logger_fn({"event": "price_monitor_cycle", **report.as_dict()})
            if max_cycles is not None and cycle >= max_cycles:
                break
            if self._interval_seconds > 0:
                time.sleep(self._interval_seconds)
        return reports

    def run_cycle(self, *, cycle: int) -> PriceMonitorCycleReport:
        started = time.perf_counter()
        targets = self._store.load_active_tracking_targets()
        verdicts_changed = 0
        notifications_sent = 0
        errors = 0

        # Group targets by (user_id, chat_id) so we can batch-notify per user
        by_user: dict[tuple[str, str], list[tuple[TrackedProduct, PriceAssessment]]] = {}

        for target in targets:
            try:
                history = self._price_tracker.get_price_history(
                    product_id=target.product_id,
                    product_name=target.product_name,
                )
                if history is None:
                    continue

                assessment = self._judgment_engine.assess(history)

                # Record assessment
                self._store.record_price_assessment(
                    user_id=target.user_id,
                    assessment=assessment,
                )

                # Check if verdict changed
                verdict_changed = target.last_verdict is None or target.last_verdict != assessment.verdict
                if verdict_changed:
                    verdicts_changed += 1
                    self._store.update_tracking_verdict(
                        user_id=target.user_id,
                        product_id=target.product_id,
                        verdict=assessment.verdict,
                        assessed_at=assessment.assessed_at,
                    )
                    key = (target.user_id, target.chat_id)
                    by_user.setdefault(key, []).append((target, assessment))

            except Exception:
                logger.exception("error checking price for %s/%s", target.user_id, target.product_id)
                errors += 1

        # Send notifications grouped by user
        for (user_id, chat_id), items in by_user.items():
            try:
                assessment_dicts = [
                    {
                        "product_id": a.product_id,
                        "product_name": a.product_name,
                        "current_price_krw": a.current_price_krw,
                        "verdict": a.verdict.value,
                        "verdict_reason": a.verdict_reason,
                        "average_price_krw": a.average_price_krw,
                        "lowest_price_krw": a.lowest_price_krw,
                        "recent_low_30d_krw": a.recent_low_30d_krw,
                        "discount_pct_vs_avg": a.discount_pct_vs_avg,
                        "discount_pct_vs_recent_low": a.discount_pct_vs_recent_low,
                        "source": a.source,
                        "assessed_at": a.assessed_at.isoformat(),
                    }
                    for _, a in items
                ]
                payload = build_price_assessment_notification_payload(
                    chat_id=chat_id,
                    assessments=assessment_dicts,
                )
                self._notification_service.send(payload)
                notifications_sent += 1
            except Exception:
                logger.exception("failed to send price notification to %s", chat_id)
                errors += 1

        elapsed_ms = (time.perf_counter() - started) * 1000
        return PriceMonitorCycleReport(
            cycle=cycle,
            targets_checked=len(targets),
            verdicts_changed=verdicts_changed,
            notifications_sent=notifications_sent,
            errors=errors,
            cycle_duration_ms=round(elapsed_ms, 1),
        )
