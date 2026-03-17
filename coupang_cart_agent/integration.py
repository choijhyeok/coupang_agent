from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Callable

from .contracts import CartAddResult, NotificationPayload, ProductCandidate, SelectedProduct, ShoppingRequest
from .notifications import build_failure_notification_payload, build_success_notification_payload
from .services import CoupangCartService, NotificationService, ProductSelectionService, TelegramIntakeService

CandidateSource = Callable[[ShoppingRequest], dict[str, list[ProductCandidate]]]


@dataclass(slots=True)
class IntegrationRunResult:
    """Result of one end-to-end cart agent execution."""

    success: bool
    request: ShoppingRequest | None = None
    selections: list[SelectedProduct] = field(default_factory=list)
    cart_results: list[CartAddResult] = field(default_factory=list)
    notification_payload: NotificationPayload | None = None
    failed_stage: str | None = None
    failure_message: str | None = None
    performance: dict[str, object] = field(default_factory=dict)

    def as_dict(self) -> dict[str, object]:
        return {
            "success": self.success,
            "request": asdict(self.request) if self.request is not None else None,
            "selections": [asdict(selection) for selection in self.selections],
            "cart_results": [asdict(result) for result in self.cart_results],
            "notification_payload": (
                asdict(self.notification_payload) if self.notification_payload is not None else None
            ),
            "failed_stage": self.failed_stage,
            "failure_message": self.failure_message,
            "performance": dict(self.performance),
        }


class CoupangCartAgentFlow:
    """Connect intake, selection, cart execution, and notification modules."""

    def __init__(
        self,
        *,
        intake_service: TelegramIntakeService,
        candidate_source: CandidateSource,
        selection_service: ProductSelectionService,
        cart_service: CoupangCartService,
        notification_service: NotificationService,
    ) -> None:
        self._intake_service = intake_service
        self._candidate_source = candidate_source
        self._selection_service = selection_service
        self._cart_service = cart_service
        self._notification_service = notification_service

    def run_text_request(
        self,
        *,
        user_id: str,
        chat_id: str,
        text: str,
    ) -> IntegrationRunResult:
        try:
            request = self._intake_service.parse_message(
                user_id=user_id,
                chat_id=chat_id,
                text=text,
            )
        except Exception as exc:
            payload = build_failure_notification_payload(
                chat_id=chat_id,
                stage="telegram_intake",
                reason="텔레그램 요청을 해석하지 못했습니다.",
                detail=str(exc),
            )
            self._notification_service.send(payload)
            return IntegrationRunResult(
                success=False,
                notification_payload=payload,
                failed_stage="telegram_intake",
                failure_message=str(exc),
            )

        return self.run_request(request)

    def run_request(self, request: ShoppingRequest) -> IntegrationRunResult:
        try:
            candidates_by_item = self._candidate_source(request)
            selections = self._selection_service.select_products(request, candidates_by_item)
        except Exception as exc:
            return self._notify_failure(
                request=request,
                stage="selection",
                reason="상품 선택 단계에서 실패했습니다.",
                detail=str(exc),
            )

        try:
            cart_results = self._cart_service.add_products(selections)
        except Exception as exc:
            return self._notify_failure(
                request=request,
                stage="cart_add",
                reason="장바구니 담기 실행 중 예외가 발생했습니다.",
                detail=str(exc),
                selections=selections,
            )

        first_failure = next((result for result in cart_results if not result.success), None)
        if first_failure is not None:
            reason = self._cart_failure_reason(first_failure)
            payload = build_failure_notification_payload(
                chat_id=request.chat_id,
                stage=first_failure.stage.value,
                reason=reason,
                detail=first_failure.message,
            )
            self._notification_service.send(payload)
            return IntegrationRunResult(
                success=False,
                request=request,
                selections=selections,
                cart_results=cart_results,
                notification_payload=payload,
                failed_stage=first_failure.stage.value,
                failure_message=first_failure.message,
            )

        payload = build_success_notification_payload(
            chat_id=request.chat_id,
            cart_results=cart_results,
        )
        self._notification_service.send(payload)
        return IntegrationRunResult(
            success=True,
            request=request,
            selections=selections,
            cart_results=cart_results,
            notification_payload=payload,
        )

    def _notify_failure(
        self,
        *,
        request: ShoppingRequest,
        stage: str,
        reason: str,
        detail: str,
        selections: list[SelectedProduct] | None = None,
    ) -> IntegrationRunResult:
        payload = build_failure_notification_payload(
            chat_id=request.chat_id,
            stage=stage,
            reason=reason,
            detail=detail,
        )
        self._notification_service.send(payload)
        return IntegrationRunResult(
            success=False,
            request=request,
            selections=[] if selections is None else selections,
            notification_payload=payload,
            failed_stage=stage,
            failure_message=detail,
        )

    @staticmethod
    def _cart_failure_reason(result: CartAddResult) -> str:
        if result.failure_reason is None:
            return "장바구니 담기에 실패했습니다."
        return f"장바구니 담기에 실패했습니다: {result.failure_reason.value}"
