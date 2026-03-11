"""Shared contracts and service interfaces for the Coupang cart agent."""

from .azure_openai import AgentPlan, AgentSearchQuery, AzureOpenAIPlanner
from .cart_executor import CoupangCartExecutor, SessionCredentials
from .cart_adapters import BrowserUseCoupangCartPage, BrowserUseSettings
from .config import AppConfig, ConfigError, load_config
from .contracts import (
    CartAddFailureReason,
    CartAddResult,
    CartAddStage,
    IntakeMode,
    NotificationPayload,
    PriorPurchaseRecord,
    ProductCandidate,
    RequestedItem,
    RequestSession,
    SelectionContext,
    SessionSelectionSignal,
    SelectedProduct,
    ShoppingRequest,
    ShoppingRequestEnvelope,
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
    NotificationFormatter,
    NotificationDeliveryError,
    RetryingNotificationService,
    SQLiteNotificationContextStore,
    TelegramSendMessageSender,
    build_failure_notification_payload,
    build_success_notification_payload,
    format_notification_message,
    summarize_cart_results,
)
from .integration import CoupangCartAgentFlow, IntegrationRunResult
from .live_workflow import CoupangCartAgentLiveWorkflow, InMemoryOperationalStore
from .postgres_store import PostgresOperationalStore
from .telegram_intake import (
    TelegramBotApiClient,
    TelegramInboundMessage,
    TelegramIntakeError,
    TelegramIntakeResult,
    TelegramPollingIntakeService,
)
from .telegram_persistence import TelegramIntakeRepository
from .telegram_worker import TelegramLiveWorker

__all__ = [
    "AppConfig",
    "AgentPlan",
    "AgentSearchQuery",
    "AzureOpenAIPlanner",
    "CartAddFailureReason",
    "CartAddResult",
    "CartAddStage",
    "BrowserUseCoupangCartPage",
    "BrowserUseSettings",
    "ConfigError",
    "CapturedCoupangFixtureCandidateSource",
    "CoupangCartExecutor",
    "CoupangCartAgentFlow",
    "CoupangCartAgentLiveWorkflow",
    "IntakeMode",
    "DemoCandidateSource",
    "InMemorySelectionContextStore",
    "InMemoryOperationalStore",
    "LiveCoupangSearchCandidateSource",
    "NotificationPayload",
    "NotificationFormatter",
    "NotificationDeliveryError",
    "PriorPurchaseRecord",
    "ProductCandidate",
    "PostgresOperationalStore",
    "RequestedItem",
    "RequestSession",
    "RetryingNotificationService",
    "SQLiteNotificationContextStore",
    "IntegrationRunResult",
    "SQLiteSelectionContextStore",
    "SessionCredentials",
    "SelectionContext",
    "SessionSelectionSignal",
    "SelectedProduct",
    "ShoppingRequest",
    "ShoppingRequestEnvelope",
    "build_failure_notification_payload",
    "build_success_notification_payload",
    "format_notification_message",
    "HeuristicProductSelectionService",
    "TelegramBotApiClient",
    "TelegramSendMessageSender",
    "TelegramInboundMessage",
    "TelegramIntakeError",
    "TelegramIntakeResult",
    "TelegramIntakeRepository",
    "TelegramLiveWorker",
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
