from __future__ import annotations

import json
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable

from .candidate_sources import DemoCandidateSource
from .cart_adapters import DemoCoupangCartPage
from .cart_executor import CoupangCartExecutor, SessionCredentials
from .integration import CoupangCartAgentFlow
from .notifications import RetryingNotificationService
from .selection import HeuristicProductSelectionService
from .telegram_intake import TelegramPollingIntakeService


class CoupangCartAgentHttpServer:
    """Small operator HTTP surface for health checks and demo smoke tests."""

    def __init__(
        self,
        *,
        host: str,
        port: int,
        db_healthcheck: Callable[[], dict[str, object]] | None = None,
    ) -> None:
        self._host = host
        self._port = port
        self._db_healthcheck = db_healthcheck

    def serve_forever(self) -> None:
        server = ThreadingHTTPServer((self._host, self._port), self._handler_factory())
        server.serve_forever()

    def _handler_factory(self):
        db_healthcheck = self._db_healthcheck

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                if self.path == "/healthz":
                    payload = {
                        "ok": True,
                        "service": "coupang-cart-agent",
                    }
                    if db_healthcheck is not None:
                        try:
                            payload["database"] = db_healthcheck()
                        except Exception as exc:
                            payload["ok"] = False
                            payload["database"] = {
                                "ok": False,
                                "error": str(exc),
                            }
                    self._write_json(200 if payload["ok"] else 503, payload)
                    return

                if self.path == "/smoke/demo":
                    delivered_messages: list[dict[str, str]] = []

                    def sender(chat_id: str, text: str) -> None:
                        delivered_messages.append({"chat_id": chat_id, "text": text})

                    flow = CoupangCartAgentFlow(
                        intake_service=TelegramPollingIntakeService(),
                        candidate_source=DemoCandidateSource(),
                        selection_service=HeuristicProductSelectionService(),
                        cart_service=CoupangCartExecutor(
                            page=DemoCoupangCartPage(should_fail=False),
                            credentials=SessionCredentials(username="demo-user", password="demo-password"),
                        ),
                        notification_service=RetryingNotificationService(sender=sender, max_attempts=1),
                    )
                    result = flow.run_text_request(
                        user_id="telegram:smoke-user",
                        chat_id="smoke-chat",
                        text="양파 1개 담아줘",
                    )
                    self._write_json(
                        200 if result.success else 500,
                        {
                            **result.as_dict(),
                            "delivered_messages": delivered_messages,
                        },
                    )
                    return

                self._write_json(404, {"ok": False, "error": "not_found"})

            def log_message(self, format: str, *args) -> None:  # noqa: A003
                return None

            def _write_json(self, status_code: int, payload: dict[str, object]) -> None:
                body = json.dumps(payload, ensure_ascii=False, indent=2, default=str).encode("utf-8")
                self.send_response(status_code)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        return Handler
