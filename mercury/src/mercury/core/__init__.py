"""Core framework infrastructure - config, events, logging, lifecycle, retry."""

from mercury.core.config import ConfigManager
from mercury.core.events import EventBus
from mercury.core.logging import setup_logging
from mercury.core.lifecycle import Startable, Stoppable, HealthCheckable
from mercury.core.retry import (
    RetryConfig,
    RetryContext,
    RetryStats,
    TransientError,
    PermanentError,
    NetworkError,
    RateLimitError,
    TimeoutError,
    ServiceUnavailableError,
    ValidationError,
    AuthenticationError,
    InsufficientFundsError,
    ResourceNotFoundError,
    OrderRejectedError,
    retry_transient,
    retry_with_config,
    retry_network,
    retry_rate_limited,
    is_retryable,
    classify_error,
    wrap_external_error,
)

__all__ = [
    # Config
    "ConfigManager",
    # Events
    "EventBus",
    # Logging
    "setup_logging",
    # Lifecycle
    "Startable",
    "Stoppable",
    "HealthCheckable",
    # Retry - Config
    "RetryConfig",
    "RetryContext",
    "RetryStats",
    # Retry - Errors (Transient)
    "TransientError",
    "NetworkError",
    "RateLimitError",
    "TimeoutError",
    "ServiceUnavailableError",
    # Retry - Errors (Permanent)
    "PermanentError",
    "ValidationError",
    "AuthenticationError",
    "InsufficientFundsError",
    "ResourceNotFoundError",
    "OrderRejectedError",
    # Retry - Decorators
    "retry_transient",
    "retry_with_config",
    "retry_network",
    "retry_rate_limited",
    # Retry - Utilities
    "is_retryable",
    "classify_error",
    "wrap_external_error",
]
