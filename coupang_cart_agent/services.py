from __future__ import annotations

from typing import Protocol

from .contracts import (
    CartAddResult,
    NotificationPayload,
    ProductCandidate,
    SelectionContext,
    SelectedProduct,
    ShoppingRequest,
)


class TelegramIntakeService(Protocol):
    """Parse inbound Telegram text into a normalized shopping request."""

    def parse_message(self, *, user_id: str, chat_id: str, text: str) -> ShoppingRequest: ...


class ProductSelectionService(Protocol):
    """Choose the best candidate using rating, review count, and price signals."""

    def select_products(
        self,
        request: ShoppingRequest,
        candidates_by_item: dict[str, list[ProductCandidate]],
    ) -> list[SelectedProduct]: ...


class SelectionContextStore(Protocol):
    """Load prior purchase and recent session context for selection."""

    def load(self, request: ShoppingRequest) -> SelectionContext: ...


class CoupangCartService(Protocol):
    """Add selected products to a Coupang cart without advancing to checkout."""

    def add_products(self, selections: list[SelectedProduct]) -> list[CartAddResult]: ...


class NotificationService(Protocol):
    """Deliver concise Telegram notifications for success and failure outcomes."""

    def send(self, payload: NotificationPayload) -> None: ...
