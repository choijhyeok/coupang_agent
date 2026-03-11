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
        }


class TelegramLiveWorker:
    """Always-on Telegram polling worker with persisted cursor and pending-envelope replay."""

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
        logger: Callable[[dict[str, object]], None] | None = None,
    ) -> None:
        self._worker_name = worker_name
        self._intake_service = intake_service
        self._intake_repository = intake_repository
        self._workflow_runner = workflow_runner
        self._poll_timeout = poll_timeout
        self._sleep_seconds = sleep_seconds
        self._send_error_response = send_error_response
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
            if self._sleep_seconds > 0:
                time.sleep(self._sleep_seconds)
        return reports

    def run_cycle(self, *, cycle: int, offset: int | None) -> WorkerCycleReport:
        pending_before = len(self._intake_repository.load_pending_envelopes(limit=100))
        intake_results = self._intake_service.poll_once(
            offset=offset,
            timeout=self._poll_timeout,
            mode=IntakeMode.LIVE,
            send_error_response=self._send_error_response,
        )
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
        )
        self._logger({"type": "worker-cycle", **report.as_dict()})
        return report

    @staticmethod
    def _default_logger(payload: dict[str, object]) -> None:
        print(json.dumps(payload, ensure_ascii=False, default=str))
