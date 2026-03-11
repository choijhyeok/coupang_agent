"""Shared contracts and service interfaces for the Coupang cart agent."""

from .cart_executor import CoupangCartExecutor, SessionCredentials
from .config import AppConfig, ConfigError, load_config
from .contracts import (
    CartAddFailureReason,
    CartAddResult,
    CartAddStage,
    NotificationPayload,
    PriorPurchaseRecord,
    ProductCandidate,
    RequestedItem,
    SelectionContext,
    SessionSelectionSignal,
    SelectedProduct,
    ShoppingRequest,
)
from .candidate_sources import (
    CapturedCoupangFixtureCandidateSource,
    DemoCandidateSource,
    LiveCoupangSearchCandidateSource,
    product_candidate_from_record,
    product_candidates_from_records,
)
from .selection import (
    HeuristicProductSelectionService,
    normalize_candidate,
    score_candidate,
    select_best_product,
    summarize_selection_reason,
)
from .selection_context import InMemorySelectionContextStore, SQLiteSelectionContextStore
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
    "CapturedCoupangFixtureCandidateSource",
    "CoupangCartExecutor",
    "CoupangCartAgentFlow",
    "DemoCandidateSource",
    "InMemorySelectionContextStore",
    "LiveCoupangSearchCandidateSource",
    "NotificationPayload",
    "NotificationDeliveryError",
    "PriorPurchaseRecord",
    "ProductCandidate",
    "RequestedItem",
    "RetryingNotificationService",
    "IntegrationRunResult",
    "SQLiteSelectionContextStore",
    "SessionCredentials",
    "SelectionContext",
    "SessionSelectionSignal",
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
    "product_candidate_from_record",
    "product_candidates_from_records",
    "score_candidate",
    "select_best_product",
    "summarize_cart_results",
    "summarize_selection_reason",
]
