from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Callable, Protocol

from .contracts import IntakeMode, ShoppingRequestEnvelope
from .integration import IntegrationRunResult
from .telegram_intake import TelegramIntakeResult, TelegramPollingIntakeService
from .telegram_persistence import TelegramIntakeRepository


class WorkflowRunner(Protocol):
    def run_envelope(self, envelope: ShoppingRequestEnvelope, *, thread_id: str | None = None) -> IntegrationRunResult: ...


@dataclass(slots=True)
class WorkerCycleReport:
    cycle: int
    worker_name: str
    offset: int | None
    next_offset: int | None
    intake_result_count: int
    processed_count: int
    success_count: int
    failure_count: int
    pending_before: int
    pending_after: int
    poll_duration_ms: float
    processing_duration_ms: float
    cycle_duration_ms: float

    def as_dict(self) -> dict[str, object]:
        return {
            "cycle": self.cycle,
            "worker_name": self.worker_name,
            "offset": self.offset,
            "next_offset": self.next_offset,
            "intake_result_count": self.intake_result_count,
            "processed_count": self.processed_count,
            "success_count": self.success_count,
            "failure_count": self.failure_count,
            "pending_before": self.pending_before,
            "pending_after": self.pending_after,
            "poll_duration_ms": self.poll_duration_ms,
            "processing_duration_ms": self.processing_duration_ms,
            "cycle_duration_ms": self.cycle_duration_ms,
        }


class TelegramLiveWorker:
    """Always-on Telegram polling worker with persisted cursor and pending-envelope replay.

    When *price_monitor* is supplied the worker will run a price-check cycle
    every *price_monitor_interval_seconds* between Telegram poll cycles,
    eliminating the need for a separate price-monitor process.
    """

    def __init__(
        self,
        *,
        worker_name: str,
        intake_service: TelegramPollingIntakeService,
        intake_repository: TelegramIntakeRepository,
        workflow_runner: WorkflowRunner,
        poll_timeout: int = 30,
        sleep_seconds: float = 1.0,
        send_error_response: bool = True,
        price_monitor: object | None = None,
        price_monitor_interval_seconds: float = 180.0,
        logger: Callable[[dict[str, object]], None] | None = None,
    ) -> None:
        self._worker_name = worker_name
        self._intake_service = intake_service
        self._intake_repository = intake_repository
        self._workflow_runner = workflow_runner
        self._poll_timeout = poll_timeout
        self._sleep_seconds = sleep_seconds
        self._send_error_response = send_error_response
        self._price_monitor = price_monitor
        self._price_monitor_interval = price_monitor_interval_seconds
        self._last_price_check: float = 0.0
        self._logger = logger or self._default_logger

    def run(
        self,
        *,
        offset: int | None = None,
        max_cycles: int | None = None,
    ) -> list[WorkerCycleReport]:
        reports: list[WorkerCycleReport] = []
        next_offset = offset if offset is not None else self._intake_repository.load_worker_cursor(worker_name=self._worker_name)
        cycle = 0
        while max_cycles is None or cycle < max_cycles:
            cycle += 1
            report = self.run_cycle(cycle=cycle, offset=next_offset)
            reports.append(report)
            next_offset = report.next_offset
            if max_cycles is not None and cycle >= max_cycles:
                break
            self._maybe_run_price_monitor(cycle=cycle)
            if self._sleep_seconds > 0:
                time.sleep(self._sleep_seconds)
        return reports

    def _maybe_run_price_monitor(self, *, cycle: int) -> None:
        """Run a price-monitor cycle if the interval has elapsed."""
        if self._price_monitor is None:
            return
        now = time.monotonic()
        if now - self._last_price_check < self._price_monitor_interval:
            return
        self._last_price_check = now
        try:
            report = self._price_monitor.run_cycle(cycle=cycle)
            self._logger({"type": "price-monitor-cycle", **report.as_dict()})
        except Exception as exc:
            self._logger({"type": "price-monitor-error", "error": str(exc)})

    def run_cycle(self, *, cycle: int, offset: int | None) -> WorkerCycleReport:
        cycle_started = time.perf_counter()
        pending_before = len(self._intake_repository.load_pending_envelopes(limit=100))
        poll_started = time.perf_counter()
        intake_results = self._intake_service.poll_once(
            offset=offset,
            timeout=self._poll_timeout,
            mode=IntakeMode.LIVE,
            send_error_response=self._send_error_response,
        )
        poll_duration_ms = round((time.perf_counter() - poll_started) * 1000.0, 2)
        highest_update_id = max((result.update_id for result in intake_results), default=None)
        next_offset = offset if highest_update_id is None else highest_update_id + 1
        self._intake_repository.save_worker_cursor(
            worker_name=self._worker_name,
            next_offset=next_offset,
            last_update_id=highest_update_id,
            last_result_json={
                "cycle": cycle,
                "offset": offset,
                "next_offset": next_offset,
                "intake_results": [result.as_dict() for result in intake_results],
            },
        )

        success_count = 0
        failure_count = 0
        processed_count = 0
        processing_started = time.perf_counter()
        for envelope in self._intake_repository.load_pending_envelopes(limit=max(1, len(intake_results) + pending_before + 5)):
            processed_count += 1
            self._intake_repository.mark_envelope_processing(inbound_message_id=envelope.inbound_message_id)
            try:
                result = self._workflow_runner.run_envelope(envelope, thread_id=envelope.session.session_id)
            except Exception as exc:
                failure_count += 1
                self._intake_repository.mark_envelope_failed(
                    inbound_message_id=envelope.inbound_message_id,
                    workflow_error=str(exc),
                )
                self._logger(
                    {
                        "type": "worker-envelope-failed",
                        "worker_name": self._worker_name,
                        "inbound_message_id": envelope.inbound_message_id,
                        "thread_id": envelope.session.session_id,
                        "error": str(exc),
                    }
                )
                continue

            if result.success:
                success_count += 1
                workflow_error = None
            else:
                failure_count += 1
                workflow_error = f"{result.failed_stage}: {result.failure_message}"
            self._intake_repository.mark_envelope_completed(
                inbound_message_id=envelope.inbound_message_id,
                workflow_error=workflow_error,
            )
            self._logger(
                {
                    "type": "worker-envelope-processed",
                    "worker_name": self._worker_name,
                    "inbound_message_id": envelope.inbound_message_id,
                    "thread_id": envelope.session.session_id,
                    "success": result.success,
                    "failed_stage": result.failed_stage,
                }
            )

        pending_after = len(self._intake_repository.load_pending_envelopes(limit=100))
        processing_duration_ms = round((time.perf_counter() - processing_started) * 1000.0, 2)
        cycle_duration_ms = round((time.perf_counter() - cycle_started) * 1000.0, 2)
        report = WorkerCycleReport(
            cycle=cycle,
            worker_name=self._worker_name,
            offset=offset,
            next_offset=next_offset,
            intake_result_count=len(intake_results),
            processed_count=processed_count,
            success_count=success_count,
            failure_count=failure_count,
            pending_before=pending_before,
            pending_after=pending_after,
            poll_duration_ms=poll_duration_ms,
            processing_duration_ms=processing_duration_ms,
            cycle_duration_ms=cycle_duration_ms,
        )
        self._logger({"type": "worker-cycle", **report.as_dict()})
        return report

    @staticmethod
    def _default_logger(payload: dict[str, object]) -> None:
        print(json.dumps(payload, ensure_ascii=False, default=str))
