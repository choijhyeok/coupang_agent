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
    "NotificationPayload",
    "ProductCandidate",
    "RequestedItem",
    "SessionCredentials",
    "SelectedProduct",
    "ShoppingRequest",
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
    "summarize_selection_reason",
]
