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
from .integration import CoupangCartAgentFlow, IntegrationRunResult
from .telegram_intake import (
    TelegramBotApiClient,
    TelegramInboundMessage,
    TelegramIntakeError,
    TelegramIntakeResult,
    TelegramPollingIntakeService,
)

__all__ = [
    "AppConfig",
    "CartAddFailureReason",
    "CartAddResult",
    "CartAddStage",
    "ConfigError",
    "CoupangCartExecutor",
    "CoupangCartAgentFlow",
    "NotificationPayload",
    "NotificationDeliveryError",
    "ProductCandidate",
    "RequestedItem",
    "RetryingNotificationService",
    "IntegrationRunResult",
    "SessionCredentials",
    "SelectedProduct",
    "ShoppingRequest",
    "build_failure_notification_payload",
    "build_success_notification_payload",
    "format_notification_message",
    "HeuristicProductSelectionService",
    "TelegramBotApiClient",
    "TelegramInboundMessage",
    "TelegramIntakeError",
    "TelegramIntakeResult",
    "TelegramPollingIntakeService",
    "load_config",
    "normalize_candidate",
    "score_candidate",
    "select_best_product",
    "summarize_cart_results",
    "summarize_selection_reason",
]
