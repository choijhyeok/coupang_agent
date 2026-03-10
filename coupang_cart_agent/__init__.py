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

__all__ = [
    "AppConfig",
    "CartAddResult",
    "ConfigError",
    "NotificationPayload",
    "ProductCandidate",
    "RequestedItem",
    "SelectedProduct",
    "ShoppingRequest",
    "load_config",
]
