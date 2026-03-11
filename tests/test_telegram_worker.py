from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from coupang_cart_agent.contracts import IntakeMode, RequestSession, ShoppingRequest, ShoppingRequestEnvelope
from coupang_cart_agent.integration import IntegrationRunResult
from coupang_cart_agent.telegram_intake import TelegramIntakeResult
from coupang_cart_agent.telegram_persistence import TelegramIntakeRepository
from coupang_cart_agent.telegram_worker import TelegramLiveWorker


class _StubIntakeService:
    def __init__(
        self,
        results_by_cycle: list[list[TelegramIntakeResult]],
        *,
        repository: TelegramIntakeRepository | None = None,
    ) -> None:
        self._results_by_cycle = list(results_by_cycle)
        self._repository = repository
        self.calls: list[dict[str, object]] = []

    def poll_once(self, *, offset, timeout, mode, send_error_response):
        self.calls.append(
            {
                "offset": offset,
                "timeout": timeout,
                "mode": mode.value,
                "send_error_response": send_error_response,
            }
        )
        if not self._results_by_cycle:
            return []
        results = self._results_by_cycle.pop(0)
        if self._repository is not None:
            for result in results:
                if result.envelope is not None:
                    self._repository.record_envelope(result.envelope)
        return results


class _StubWorkflow:
    def __init__(self, *, should_fail: bool = False) -> None:
        self.should_fail = should_fail
        self.calls: list[str] = []

    def run_envelope(self, envelope: ShoppingRequestEnvelope, *, thread_id: str | None = None) -> IntegrationRunResult:
        self.calls.append(envelope.inbound_message_id)
        return IntegrationRunResult(
            success=not self.should_fail,
            request=envelope.request,
            selections=[],
            cart_results=[],
            notification_payload=None,
            failed_stage=None if not self.should_fail else "cart_add",
            failure_message=None if not self.should_fail else "blocked",
        )


def _build_envelope(*, update_id: int) -> ShoppingRequestEnvelope:
    received_at = datetime(2026, 3, 11, 7, 0, tzinfo=UTC)
    request = ShoppingRequest(
        user_id="telegram:123",
        chat_id="456",
        items=[],
        raw_text="콜라 담아줘",
        request_id=f"telegram-update-{update_id}",
        received_at=received_at,
    )
    return ShoppingRequestEnvelope(
        source="telegram",
        mode=IntakeMode.LIVE,
        request=request,
        session=RequestSession(
            session_id="telegram-session:456:telegram:123",
            channel="telegram",
            user_id="telegram:123",
            chat_id="456",
            created_at=received_at,
            last_message_at=received_at,
        ),
        inbound_message_id=f"telegram-update-{update_id}",
        update_id=update_id,
        message_id=update_id,
        raw_text=request.raw_text,
        raw_update={"update_id": update_id},
        metadata={"session_id": "telegram-session:456:telegram:123"},
    )


class TelegramWorkerTests(unittest.TestCase):
    def test_worker_processes_pending_messages_and_persists_cursor(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            repository = TelegramIntakeRepository(Path(tmp_dir) / "intake.sqlite3")
            envelope = _build_envelope(update_id=101)
            repository.record_envelope(envelope)
            intake_service = _StubIntakeService(
                [
                    [
                        TelegramIntakeResult(
                            update_id=102,
                            chat_id="456",
                            request=envelope.request,
                            envelope=_build_envelope(update_id=102),
                        )
                    ]
                ],
                repository=repository,
            )
            workflow = _StubWorkflow()
            events: list[dict[str, object]] = []
            worker = TelegramLiveWorker(
                worker_name="worker-a",
                intake_service=intake_service,
                intake_repository=repository,
                workflow_runner=workflow,
                poll_timeout=5,
                sleep_seconds=0,
                logger=events.append,
            )

            reports = worker.run(max_cycles=1)

            self.assertEqual(len(reports), 1)
            self.assertEqual(reports[0].processed_count, 2)
            self.assertEqual(repository.load_worker_cursor(worker_name="worker-a"), 103)
            inbound_messages = repository.list_inbound_messages()
            self.assertEqual([row["workflow_status"] for row in inbound_messages], ["completed", "completed"])
            self.assertEqual(workflow.calls, ["telegram-update-101", "telegram-update-102"])
            self.assertEqual(intake_service.calls[0]["mode"], "live")
            self.assertTrue(any(event["type"] == "worker-cycle" for event in events))

    def test_worker_restores_processing_message_after_restart(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            repository = TelegramIntakeRepository(Path(tmp_dir) / "intake.sqlite3")
            envelope = _build_envelope(update_id=201)
            repository.record_envelope(envelope)
            repository.mark_envelope_processing(inbound_message_id=envelope.inbound_message_id)
            repository.save_worker_cursor(worker_name="worker-b", next_offset=202, last_update_id=201)

            intake_service = _StubIntakeService([[]])
            workflow = _StubWorkflow(should_fail=True)
            worker = TelegramLiveWorker(
                worker_name="worker-b",
                intake_service=intake_service,
                intake_repository=repository,
                workflow_runner=workflow,
                poll_timeout=5,
                sleep_seconds=0,
            )

            reports = worker.run(max_cycles=1)

            self.assertEqual(reports[0].offset, 202)
            inbound_messages = repository.list_inbound_messages()
            self.assertEqual(inbound_messages[0]["workflow_status"], "completed")
            self.assertEqual(inbound_messages[0]["workflow_error"], "cart_add: blocked")


if __name__ == "__main__":
    unittest.main()
