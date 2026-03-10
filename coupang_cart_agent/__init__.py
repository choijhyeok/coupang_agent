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
    "load_config",
]
