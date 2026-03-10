"""Shared contracts and service interfaces for the Coupang cart agent."""

from .config import AppConfig, ConfigError, load_config
from .contracts import (
    CartAddResult,
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

__all__ = [
    "AppConfig",
    "CartAddResult",
    "ConfigError",
    "NotificationPayload",
    "ProductCandidate",
    "RequestedItem",
    "SelectedProduct",
    "ShoppingRequest",
    "HeuristicProductSelectionService",
    "load_config",
    "normalize_candidate",
    "score_candidate",
    "select_best_product",
    "summarize_selection_reason",
]
