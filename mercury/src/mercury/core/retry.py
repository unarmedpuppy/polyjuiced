"""
Retry logic with exponential backoff for transient failures.

This module provides:
- Error type hierarchy (retryable vs non-retryable)
- Configurable retry decorator using tenacity
- Pre-configured retry strategies for common scenarios

Usage:
    from mercury.core.retry import (
        retry_transient,
        RetryConfig,
        TransientError,
        PermanentError,
    )

    @retry_transient()
    async def fetch_data():
        ...

    # Or with custom config
    config = RetryConfig(max_attempts=5, min_wait=2.0)

    @retry_with_config(config)
    async def fetch_with_custom_retry():
        ...
"""

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from functools import wraps
from typing import Any, Callable, Optional, Type, TypeVar, Union

import structlog
from tenacity import (
    AsyncRetrying,
    RetryCallState,
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
    wait_random_exponential,
)

log = structlog.get_logger()


# =============================================================================
# Error Type Hierarchy
# =============================================================================


class ErrorCategory(str, Enum):
    """Classification of error types for retry decisions."""

    TRANSIENT = "transient"  # Network issues, rate limits - should retry
    PERMANENT = "permanent"  # Bad request, auth failure - should NOT retry
    UNKNOWN = "unknown"  # Unclassified - treat as transient by default


class MercuryError(Exception):
    """Base exception for all Mercury errors."""

    category: ErrorCategory = ErrorCategory.UNKNOWN

    def __init__(self, message: str, cause: Optional[Exception] = None):
        super().__init__(message)
        self.message = message
        self.cause = cause
        self.timestamp = datetime.now(timezone.utc)

    def __str__(self) -> str:
        if self.cause:
            return f"{self.message} (caused by: {self.cause})"
        return self.message


class TransientError(MercuryError):
    """Error that may succeed on retry.

    Examples:
    - Network timeout
    - Rate limit exceeded
    - Service temporarily unavailable
    - Connection reset
    """

    category = ErrorCategory.TRANSIENT


class NetworkError(TransientError):
    """Network-related transient error."""

    pass


class RateLimitError(TransientError):
    """Rate limit exceeded - should retry after backoff."""

    def __init__(
        self,
        message: str,
        retry_after: Optional[float] = None,
        cause: Optional[Exception] = None,
    ):
        super().__init__(message, cause)
        self.retry_after = retry_after


class TimeoutError(TransientError):
    """Operation timed out - may succeed on retry."""

    pass


class ServiceUnavailableError(TransientError):
    """External service is temporarily unavailable."""

    pass


class PermanentError(MercuryError):
    """Error that will NOT succeed on retry.

    Examples:
    - Invalid parameters
    - Authentication failure
    - Insufficient funds
    - Resource not found
    """

    category = ErrorCategory.PERMANENT


class ValidationError(PermanentError):
    """Request validation failed - fix the input."""

    pass


class AuthenticationError(PermanentError):
    """Authentication/authorization failed."""

    pass


class InsufficientFundsError(PermanentError):
    """Not enough balance for the operation."""

    pass


class ResourceNotFoundError(PermanentError):
    """Requested resource does not exist."""

    pass


class OrderRejectedError(PermanentError):
    """Order was rejected by exchange - will not succeed on retry."""

    pass


# =============================================================================
# Retry Configuration
# =============================================================================

# Default values
DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_MIN_WAIT_SECONDS = 1.0
DEFAULT_MAX_WAIT_SECONDS = 30.0
DEFAULT_EXPONENTIAL_MULTIPLIER = 2.0
DEFAULT_JITTER = True


@dataclass
class RetryConfig:
    """Configuration for retry behavior.

    Attributes:
        max_attempts: Maximum number of retry attempts (including initial).
        min_wait_seconds: Minimum wait time between retries.
        max_wait_seconds: Maximum wait time between retries.
        exponential_multiplier: Multiplier for exponential backoff.
        jitter: Whether to add randomness to wait times.
        retry_on: Exception types to retry on (default: TransientError).
        on_retry: Optional callback for retry events.
    """

    max_attempts: int = DEFAULT_MAX_ATTEMPTS
    min_wait_seconds: float = DEFAULT_MIN_WAIT_SECONDS
    max_wait_seconds: float = DEFAULT_MAX_WAIT_SECONDS
    exponential_multiplier: float = DEFAULT_EXPONENTIAL_MULTIPLIER
    jitter: bool = DEFAULT_JITTER
    retry_on: tuple[Type[Exception], ...] = field(
        default_factory=lambda: (TransientError,)
    )
    on_retry: Optional[Callable[[RetryCallState], None]] = None

    @classmethod
    def from_dict(cls, config_dict: dict[str, Any]) -> "RetryConfig":
        """Create RetryConfig from a dictionary (e.g., from ConfigManager).

        Args:
            config_dict: Dictionary with retry configuration.

        Returns:
            RetryConfig instance.
        """
        return cls(
            max_attempts=config_dict.get("max_attempts", DEFAULT_MAX_ATTEMPTS),
            min_wait_seconds=config_dict.get("min_wait_seconds", DEFAULT_MIN_WAIT_SECONDS),
            max_wait_seconds=config_dict.get("max_wait_seconds", DEFAULT_MAX_WAIT_SECONDS),
            exponential_multiplier=config_dict.get(
                "exponential_multiplier", DEFAULT_EXPONENTIAL_MULTIPLIER
            ),
            jitter=config_dict.get("jitter", DEFAULT_JITTER),
        )


@dataclass
class RetryStats:
    """Statistics about retry attempts."""

    total_attempts: int = 0
    successful_attempts: int = 0
    failed_attempts: int = 0
    total_wait_time_seconds: float = 0.0
    last_error: Optional[Exception] = None
    last_attempt_at: Optional[datetime] = None


# =============================================================================
# Retry Decorators
# =============================================================================

F = TypeVar("F", bound=Callable[..., Any])


def _create_retry_callback(
    log_context: Optional[dict[str, Any]] = None,
    on_retry: Optional[Callable[[RetryCallState], None]] = None,
) -> Callable[[RetryCallState], None]:
    """Create a callback for retry events.

    Args:
        log_context: Additional context for logging.
        on_retry: User-provided callback.

    Returns:
        Callback function for retry events.
    """
    context = log_context or {}

    def callback(state: RetryCallState) -> None:
        # Log the retry attempt
        exception = state.outcome.exception() if state.outcome else None
        log.warning(
            "retry_attempt",
            attempt=state.attempt_number,
            error=str(exception) if exception else None,
            error_type=type(exception).__name__ if exception else None,
            wait_seconds=state.next_action.sleep if state.next_action else 0,
            **context,
        )

        # Call user callback if provided
        if on_retry:
            on_retry(state)

    return callback


def retry_transient(
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    min_wait: float = DEFAULT_MIN_WAIT_SECONDS,
    max_wait: float = DEFAULT_MAX_WAIT_SECONDS,
    multiplier: float = DEFAULT_EXPONENTIAL_MULTIPLIER,
    jitter: bool = DEFAULT_JITTER,
    on_retry: Optional[Callable[[RetryCallState], None]] = None,
    log_context: Optional[dict[str, Any]] = None,
) -> Callable[[F], F]:
    """Decorator to retry a function on transient errors with exponential backoff.

    This is the most commonly used retry decorator. It will retry on any
    TransientError (NetworkError, RateLimitError, TimeoutError, etc.) with
    configurable exponential backoff.

    Args:
        max_attempts: Maximum number of attempts (including initial).
        min_wait: Minimum wait time between retries in seconds.
        max_wait: Maximum wait time between retries in seconds.
        multiplier: Multiplier for exponential backoff.
        jitter: Whether to add randomness to wait times.
        on_retry: Optional callback called on each retry.
        log_context: Additional context for log messages.

    Returns:
        Decorator function.

    Example:
        @retry_transient(max_attempts=5)
        async def fetch_market_data():
            ...
    """

    def decorator(func: F) -> F:
        callback = _create_retry_callback(log_context, on_retry)

        # Choose wait strategy based on jitter setting
        if jitter:
            wait_strategy = wait_random_exponential(
                multiplier=multiplier, min=min_wait, max=max_wait
            )
        else:
            wait_strategy = wait_exponential(
                multiplier=multiplier, min=min_wait, max=max_wait
            )

        @wraps(func)
        async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
            async for attempt in AsyncRetrying(
                stop=stop_after_attempt(max_attempts),
                wait=wait_strategy,
                retry=retry_if_exception_type(TransientError),
                before_sleep=callback,
                reraise=True,
            ):
                with attempt:
                    return await func(*args, **kwargs)

        @wraps(func)
        def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
            # For sync functions, use the sync retry decorator
            decorated = retry(
                stop=stop_after_attempt(max_attempts),
                wait=wait_strategy,
                retry=retry_if_exception_type(TransientError),
                before_sleep=callback,
                reraise=True,
            )(func)
            return decorated(*args, **kwargs)

        # Return appropriate wrapper based on function type
        if asyncio.iscoroutinefunction(func):
            return async_wrapper  # type: ignore
        return sync_wrapper  # type: ignore

    return decorator


def retry_with_config(
    config: RetryConfig,
    log_context: Optional[dict[str, Any]] = None,
) -> Callable[[F], F]:
    """Decorator to retry a function using a RetryConfig object.

    Args:
        config: RetryConfig with retry parameters.
        log_context: Additional context for log messages.

    Returns:
        Decorator function.

    Example:
        config = RetryConfig(max_attempts=5, min_wait_seconds=2.0)

        @retry_with_config(config)
        async def fetch_data():
            ...
    """
    return retry_transient(
        max_attempts=config.max_attempts,
        min_wait=config.min_wait_seconds,
        max_wait=config.max_wait_seconds,
        multiplier=config.exponential_multiplier,
        jitter=config.jitter,
        on_retry=config.on_retry,
        log_context=log_context,
    )


def retry_network(
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    min_wait: float = DEFAULT_MIN_WAIT_SECONDS,
    max_wait: float = DEFAULT_MAX_WAIT_SECONDS,
) -> Callable[[F], F]:
    """Decorator specifically for network operations.

    Retries on NetworkError, TimeoutError, and ServiceUnavailableError.

    Args:
        max_attempts: Maximum number of attempts.
        min_wait: Minimum wait time between retries.
        max_wait: Maximum wait time between retries.

    Returns:
        Decorator function.
    """

    def decorator(func: F) -> F:
        callback = _create_retry_callback({"operation": "network"})
        wait_strategy = wait_random_exponential(min=min_wait, max=max_wait)

        @wraps(func)
        async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
            async for attempt in AsyncRetrying(
                stop=stop_after_attempt(max_attempts),
                wait=wait_strategy,
                retry=retry_if_exception_type(
                    (NetworkError, TimeoutError, ServiceUnavailableError)
                ),
                before_sleep=callback,
                reraise=True,
            ):
                with attempt:
                    return await func(*args, **kwargs)

        if asyncio.iscoroutinefunction(func):
            return async_wrapper  # type: ignore
        raise TypeError("retry_network only supports async functions")

    return decorator


def retry_rate_limited(
    max_attempts: int = 5,  # More attempts for rate limits
    min_wait: float = 2.0,  # Longer initial wait
    max_wait: float = 60.0,  # Much longer max wait
) -> Callable[[F], F]:
    """Decorator for rate-limited operations.

    Uses longer backoff times since rate limits usually require
    waiting before retrying.

    Args:
        max_attempts: Maximum number of attempts.
        min_wait: Minimum wait time between retries.
        max_wait: Maximum wait time between retries.

    Returns:
        Decorator function.
    """

    def decorator(func: F) -> F:
        callback = _create_retry_callback({"operation": "rate_limited"})
        wait_strategy = wait_random_exponential(min=min_wait, max=max_wait)

        @wraps(func)
        async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
            async for attempt in AsyncRetrying(
                stop=stop_after_attempt(max_attempts),
                wait=wait_strategy,
                retry=retry_if_exception_type(RateLimitError),
                before_sleep=callback,
                reraise=True,
            ):
                with attempt:
                    return await func(*args, **kwargs)

        if asyncio.iscoroutinefunction(func):
            return async_wrapper  # type: ignore
        raise TypeError("retry_rate_limited only supports async functions")

    return decorator


# =============================================================================
# Error Classification Utilities
# =============================================================================


def is_retryable(error: Exception) -> bool:
    """Check if an error is retryable.

    Args:
        error: The exception to check.

    Returns:
        True if the error should be retried, False otherwise.
    """
    if isinstance(error, PermanentError):
        return False
    if isinstance(error, TransientError):
        return True
    # For non-Mercury errors, check common patterns
    error_str = str(error).lower()
    retryable_patterns = [
        "timeout",
        "timed out",
        "connection",
        "network",
        "rate limit",
        "too many requests",
        "503",
        "502",
        "504",
        "service unavailable",
        "temporarily",
    ]
    return any(pattern in error_str for pattern in retryable_patterns)


def classify_error(error: Exception) -> ErrorCategory:
    """Classify an error into a category.

    Args:
        error: The exception to classify.

    Returns:
        ErrorCategory for the error.
    """
    if isinstance(error, MercuryError):
        return error.category

    # Classify common exception types
    error_str = str(error).lower()
    error_type = type(error).__name__.lower()

    # Permanent error patterns
    permanent_patterns = [
        "invalid",
        "unauthorized",
        "forbidden",
        "not found",
        "bad request",
        "400",
        "401",
        "403",
        "404",
        "insufficient",
        "authentication",
        "permission",
    ]
    if any(pattern in error_str or pattern in error_type for pattern in permanent_patterns):
        return ErrorCategory.PERMANENT

    # Transient error patterns
    transient_patterns = [
        "timeout",
        "timed out",
        "connection",
        "network",
        "rate limit",
        "503",
        "502",
        "504",
        "service unavailable",
        "temporarily",
    ]
    if any(pattern in error_str or pattern in error_type for pattern in transient_patterns):
        return ErrorCategory.TRANSIENT

    return ErrorCategory.UNKNOWN


def wrap_external_error(
    error: Exception,
    context: Optional[str] = None,
) -> Union[TransientError, PermanentError]:
    """Wrap an external error in the appropriate Mercury error type.

    This is useful when calling external libraries that don't use
    Mercury's error hierarchy.

    Args:
        error: The external exception.
        context: Optional context for the error message.

    Returns:
        A TransientError or PermanentError wrapping the original.
    """
    category = classify_error(error)
    message = f"{context}: {error}" if context else str(error)

    if category == ErrorCategory.PERMANENT:
        return PermanentError(message, cause=error)
    return TransientError(message, cause=error)


# =============================================================================
# Retry Context Manager
# =============================================================================


class RetryContext:
    """Context manager for manual retry control.

    Useful when you need more control over retry behavior than
    decorators provide.

    Example:
        async with RetryContext(max_attempts=3) as ctx:
            while ctx.should_retry():
                try:
                    result = await risky_operation()
                    ctx.success()
                    break
                except TransientError as e:
                    await ctx.handle_error(e)
    """

    def __init__(
        self,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        min_wait: float = DEFAULT_MIN_WAIT_SECONDS,
        max_wait: float = DEFAULT_MAX_WAIT_SECONDS,
        multiplier: float = DEFAULT_EXPONENTIAL_MULTIPLIER,
    ):
        self.max_attempts = max_attempts
        self.min_wait = min_wait
        self.max_wait = max_wait
        self.multiplier = multiplier

        self._attempt = 0
        self._succeeded = False
        self._last_error: Optional[Exception] = None
        self._stats = RetryStats()

    async def __aenter__(self) -> "RetryContext":
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> bool:
        return False  # Don't suppress exceptions

    def should_retry(self) -> bool:
        """Check if another retry attempt should be made."""
        return not self._succeeded and self._attempt < self.max_attempts

    async def handle_error(self, error: Exception) -> None:
        """Handle an error and wait before next attempt.

        Args:
            error: The exception that occurred.

        Raises:
            The original error if it's not retryable or max attempts reached.
        """
        self._last_error = error
        self._stats.last_error = error
        self._stats.last_attempt_at = datetime.now(timezone.utc)
        self._stats.failed_attempts += 1

        if not is_retryable(error):
            log.error(
                "non_retryable_error",
                error=str(error),
                error_type=type(error).__name__,
                attempt=self._attempt,
            )
            raise error

        self._attempt += 1
        self._stats.total_attempts = self._attempt

        if self._attempt >= self.max_attempts:
            log.error(
                "max_retries_exceeded",
                error=str(error),
                max_attempts=self.max_attempts,
            )
            raise error

        # Calculate wait time with exponential backoff
        wait_time = min(
            self.min_wait * (self.multiplier ** (self._attempt - 1)),
            self.max_wait,
        )
        self._stats.total_wait_time_seconds += wait_time

        log.warning(
            "retry_after_error",
            error=str(error),
            attempt=self._attempt,
            max_attempts=self.max_attempts,
            wait_seconds=wait_time,
        )

        await asyncio.sleep(wait_time)

    def success(self) -> None:
        """Mark the operation as successful."""
        self._succeeded = True
        self._stats.successful_attempts += 1
        self._stats.total_attempts = self._attempt + 1

    @property
    def attempt(self) -> int:
        """Current attempt number (1-indexed)."""
        return self._attempt + 1

    @property
    def stats(self) -> RetryStats:
        """Get retry statistics."""
        return self._stats
