from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from .contracts import (
    CartAddFailureReason,
    CartAddResult,
    CartAddStage,
    SelectedProduct,
)


class LoginFailedError(RuntimeError):
    """Raised when login or session restoration fails."""


class OutOfStockError(RuntimeError):
    """Raised when the target product cannot be added because it is unavailable."""


class OptionMismatchError(RuntimeError):
    """Raised when the requested product options cannot be matched on the page."""


class UIElementNotFoundError(RuntimeError):
    """Raised when a required page element cannot be found."""


@dataclass(slots=True)
class SessionCredentials:
    username: str
    password: str


@dataclass(slots=True)
class CartSnapshot:
    item_count: int
    summary: str = ""


class CoupangCartPage(Protocol):
    """Browser/page seam for the cart executor."""

    def ensure_session(self, credentials: SessionCredentials) -> str: ...

    def open_product(self, product_url: str) -> None: ...

    def assert_in_stock(self) -> None: ...

    def select_options(self, selection: SelectedProduct) -> dict[str, str]: ...

    def cart_snapshot(self) -> CartSnapshot: ...

    def add_to_cart(self) -> str: ...

    def checkout_started(self) -> bool: ...


@dataclass(slots=True)
class AuditEntry:
    stage: CartAddStage
    message: str
    metadata: dict[str, object] = field(default_factory=dict)


class CoupangCartExecutor:
    """Production-shaped executor that stops after the add-to-cart boundary."""

    def __init__(
        self,
        *,
        page: CoupangCartPage,
        credentials: SessionCredentials,
    ) -> None:
        self._page = page
        self._credentials = credentials
        self._audit_entries: list[AuditEntry] = []

    def add_products(self, selections: list[SelectedProduct]) -> list[CartAddResult]:
        return [self._add_single(selection) for selection in selections]

    def audit_log(self) -> list[AuditEntry]:
        return list(self._audit_entries)

    def _add_single(self, selection: SelectedProduct) -> CartAddResult:
        stage = CartAddStage.SESSION
        try:
            session_mode = self._page.ensure_session(self._credentials)
            self._audit(stage, "Session ready", session_mode=session_mode)

            stage = CartAddStage.PRODUCT_PAGE
            self._page.open_product(selection.candidate.product_url)
            self._page.assert_in_stock()
            self._audit(stage, "Product page ready", product_id=selection.candidate.product_id)

            stage = CartAddStage.OPTION_SELECTION
            selected_options = self._page.select_options(selection)
            self._audit(stage, "Options selected", options=selected_options)

            stage = CartAddStage.ADD_TO_CART
            before = self._page.cart_snapshot()
            cart_item_id = self._page.add_to_cart()
            after = self._page.cart_snapshot()
            checkout_attempted = self._page.checkout_started()
            self._audit(
                stage,
                "Add to cart executed",
                cart_item_id=cart_item_id,
                cart_count_before=before.item_count,
                cart_count_after=after.item_count,
                checkout_attempted=checkout_attempted,
            )
            if checkout_attempted:
                return self._failure_result(
                    selection,
                    stage=stage,
                    failure_reason=CartAddFailureReason.CHECKOUT_ATTEMPTED,
                    message="Cart add triggered checkout flow and was stopped.",
                    cart_count_before=before.item_count,
                    cart_count_after=after.item_count,
                    checkout_attempted=True,
                    evidence={
                        "selected_options": selected_options,
                        "session_mode": session_mode,
                        "product_url": selection.candidate.product_url,
                    },
                )

            return CartAddResult(
                success=True,
                cart_item_id=cart_item_id,
                selected_product=selection,
                stage=stage,
                message="Item added to cart.",
                cart_count_before=before.item_count,
                cart_count_after=after.item_count,
                checkout_attempted=False,
                evidence={
                    "selected_options": selected_options,
                    "session_mode": session_mode,
                    "product_url": selection.candidate.product_url,
                },
            )
        except LoginFailedError as exc:
            return self._failure_result(
                selection,
                stage=stage,
                failure_reason=CartAddFailureReason.LOGIN_FAILED,
                message=str(exc),
            )
        except OutOfStockError as exc:
            return self._failure_result(
                selection,
                stage=stage,
                failure_reason=CartAddFailureReason.OUT_OF_STOCK,
                message=str(exc),
            )
        except OptionMismatchError as exc:
            return self._failure_result(
                selection,
                stage=stage,
                failure_reason=CartAddFailureReason.OPTION_MISMATCH,
                message=str(exc),
            )
        except UIElementNotFoundError as exc:
            return self._failure_result(
                selection,
                stage=stage,
                failure_reason=CartAddFailureReason.UI_ELEMENT_NOT_FOUND,
                message=str(exc),
            )
        except Exception as exc:  # pragma: no cover - defensive fallback
            return self._failure_result(
                selection,
                stage=stage,
                failure_reason=CartAddFailureReason.UNKNOWN,
                message=f"Unexpected cart automation error: {exc}",
            )

    def _failure_result(
        self,
        selection: SelectedProduct,
        *,
        stage: CartAddStage,
        failure_reason: CartAddFailureReason,
        message: str,
        cart_count_before: int | None = None,
        cart_count_after: int | None = None,
        checkout_attempted: bool = False,
        evidence: dict[str, object] | None = None,
    ) -> CartAddResult:
        self._audit(
            stage,
            "Cart add failed",
            failure_reason=failure_reason,
            checkout_attempted=checkout_attempted,
        )
        return CartAddResult(
            success=False,
            cart_item_id=None,
            selected_product=selection,
            stage=stage,
            failure_reason=failure_reason,
            message=message,
            cart_count_before=cart_count_before,
            cart_count_after=cart_count_after,
            checkout_attempted=checkout_attempted,
            evidence=evidence or {},
        )

    def _audit(self, stage: CartAddStage, message: str, **metadata: object) -> None:
        self._audit_entries.append(
            AuditEntry(
                stage=stage,
                message=message,
                metadata={key: self._sanitize(value) for key, value in metadata.items()},
            )
        )

    def _sanitize(self, value: object) -> object:
        if isinstance(value, str):
            for secret in (self._credentials.username, self._credentials.password):
                if secret and secret in value:
                    return value.replace(secret, "***")
            return value
        if isinstance(value, dict):
            return {key: self._sanitize(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self._sanitize(item) for item in value]
        return value
