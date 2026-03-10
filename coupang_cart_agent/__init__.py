"""Shared contracts and service interfaces for the Coupang cart agent."""

from .cart_executor import CoupangCartExecutor, SessionCredentials
from .config import AppConfig, ConfigError, load_config
from .contracts import (
    CartAddFailureReason,
    CartAddResult,
    CartAddStage,
    NotificationPayload,
    ProductCandidate,
    RequestedItem,
    SelectedProduct,
    ShoppingRequest,
)
from .selection import (
    HeuristicProductSelectionService,
    normalize_candidate,
    score_candidate,
    select_best_product,
    summarize_selection_reason,
)
from .notifications import (
    NotificationDeliveryError,
    RetryingNotificationService,
    build_failure_notification_payload,
    build_success_notification_payload,
    format_notification_message,
    summarize_cart_results,
)

__all__ = [
    "AppConfig",
    "CartAddFailureReason",
    "CartAddResult",
    "CartAddStage",
    "ConfigError",
    "CoupangCartExecutor",
    "NotificationPayload",
    "NotificationDeliveryError",
    "ProductCandidate",
    "RequestedItem",
    "RetryingNotificationService",
    "SessionCredentials",
    "SelectedProduct",
    "ShoppingRequest",
    "build_failure_notification_payload",
    "build_success_notification_payload",
    "format_notification_message",
    "HeuristicProductSelectionService",
    "load_config",
    "normalize_candidate",
    "score_candidate",
    "select_best_product",
    "summarize_cart_results",
    "summarize_selection_reason",
]
