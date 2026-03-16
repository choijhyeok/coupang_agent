from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol

from .cart_persistence import CartResultStore, build_cart_result_record
from .cart_verification import CartVerificationModel, DeterministicCartVerifier
from .contracts import (
    BrowserObservation,
    CartAddFailureReason,
    CartAddResult,
    CartAddStage,
    SelectedProduct,
)


class LoginFailedError(RuntimeError):
    """Raised when login or session restoration fails."""


class LoginRequiredError(LoginFailedError):
    """Raised when attach mode lands on a login page or the session is expired."""


class SecurityChallengeError(LoginFailedError):
    """Raised when Coupang presents a security or anti-bot challenge."""


class AccessDeniedError(SecurityChallengeError):
    """Raised when Coupang blocks the attached browser session with Access Denied."""


class OutOfStockError(RuntimeError):
    """Raised when the target product cannot be added because it is unavailable."""


class OptionMismatchError(RuntimeError):
    """Raised when the requested product options cannot be matched on the page."""


class UIElementNotFoundError(RuntimeError):
    """Raised when a required page element cannot be found."""


@dataclass(slots=True)
class SessionCredentials:
    username: str | None = None
    password: str | None = None


@dataclass(slots=True)
class CartSnapshot:
    item_count: int
    summary: str = ""


class CoupangCartPage(Protocol):
    """Browser/page seam for the cart executor."""

    def attach_to_logged_in_session(self, credentials: SessionCredentials | None = None) -> str: ...

    def assert_logged_in(self) -> None: ...

    def open_product(self, product_url: str) -> None: ...

    def assert_in_stock(self) -> None: ...

    def select_options(self, selection: SelectedProduct) -> dict[str, str]: ...

    def cart_snapshot(self) -> CartSnapshot: ...

    def add_to_cart(self) -> str: ...

    def checkout_started(self) -> bool: ...

    def observe_cart_verification(self) -> BrowserObservation: ...


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
        credentials: SessionCredentials | None = None,
        result_store: CartResultStore | None = None,
        verifier: CartVerificationModel | None = None,
    ) -> None:
        self._page = page
        self._credentials = credentials
        self._result_store = result_store
        self._verifier = verifier or DeterministicCartVerifier()
        self._audit_entries: list[AuditEntry] = []

    def add_products(self, selections: list[SelectedProduct]) -> list[CartAddResult]:
        return [self._add_single(selection) for selection in selections]

    def audit_log(self) -> list[AuditEntry]:
        return list(self._audit_entries)

    def _add_single(self, selection: SelectedProduct) -> CartAddResult:
        stage = CartAddStage.SESSION
        try:
            session_mode = self._page.attach_to_logged_in_session(self._credentials)
            self._page.assert_logged_in()
            self._audit(stage, "Logged-in session attached", session_mode=session_mode)

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

            stage = CartAddStage.VERIFICATION
            verification_observation = self._page.observe_cart_verification()
            verification = self._verifier.verify(
                selection=selection,
                observation=verification_observation,
                cart_count_before=before.item_count,
                cart_count_after=after.item_count,
            )
            self._audit(
                stage,
                "Post-action cart verification completed",
                verification_success=verification.success,
                verification_failure_reason=verification.failure_reason,
                matched_item_name=verification.matched_item_name,
            )
            if not verification.success:
                return self._failure_result(
                    selection,
                    stage=stage,
                    failure_reason=(
                        verification.failure_reason or CartAddFailureReason.MANUAL_REVIEW_REQUIRED
                    ),
                    message=verification.reason,
                    cart_count_before=before.item_count,
                    cart_count_after=after.item_count,
                    checkout_attempted=False,
                    evidence={
                        "selected_options": selected_options,
                        "session_mode": session_mode,
                        "product_url": selection.candidate.product_url,
                        "recorded_at": datetime.now(UTC).isoformat(),
                        "cart_snapshot_before": before.summary,
                        "cart_snapshot_after": after.summary,
                        "verification": verification.evidence,
                    },
                )

            result = CartAddResult(
                success=True,
                cart_item_id=cart_item_id,
                selected_product=selection,
                stage=stage,
                message="Item added to cart and verified.",
                cart_count_before=before.item_count,
                cart_count_after=after.item_count,
                checkout_attempted=False,
                evidence={
                    "selected_options": selected_options,
                    "session_mode": session_mode,
                    "product_url": selection.candidate.product_url,
                    "recorded_at": datetime.now(UTC).isoformat(),
                    "cart_snapshot_before": before.summary,
                    "cart_snapshot_after": after.summary,
                    "verification": verification.evidence,
                },
            )
            self._persist_result(result)
            return result
        except LoginRequiredError as exc:
            return self._failure_result(
                selection,
                stage=stage,
                failure_reason=CartAddFailureReason.LOGIN_REQUIRED,
                message=str(exc),
            )
        except AccessDeniedError as exc:
            return self._failure_result(
                selection,
                stage=stage,
                failure_reason=CartAddFailureReason.ACCESS_DENIED,
                message=str(exc),
            )
        except SecurityChallengeError as exc:
            return self._failure_result(
                selection,
                stage=stage,
                failure_reason=CartAddFailureReason.SECURITY_CHALLENGE,
                message=str(exc),
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
        result = CartAddResult(
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
        self._persist_result(result)
        return result

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
            credentials = self._credentials or SessionCredentials()
            for secret in (credentials.username, credentials.password):
                if secret and secret in value:
                    return value.replace(secret, "***")
            return value
        if isinstance(value, dict):
            return {key: self._sanitize(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self._sanitize(item) for item in value]
        return value

    def _persist_result(self, result: CartAddResult) -> None:
        if self._result_store is None:
            return
        self._result_store.save(build_cart_result_record(result))
