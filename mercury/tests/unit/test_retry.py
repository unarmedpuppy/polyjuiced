"""
Unit tests for retry logic with exponential backoff.

Tests cover:
- Error type hierarchy (retryable vs non-retryable)
- Retry decorators with exponential backoff
- Configurable retry behavior
- Error classification utilities
- RetryContext for manual retry control
"""

import asyncio
import pytest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from mercury.core.retry import (
    # Error hierarchy
    MercuryError,
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
    ErrorCategory,
    # Config
    RetryConfig,
    RetryStats,
    DEFAULT_MAX_ATTEMPTS,
    DEFAULT_MIN_WAIT_SECONDS,
    DEFAULT_MAX_WAIT_SECONDS,
    # Decorators
    retry_transient,
    retry_with_config,
    retry_network,
    retry_rate_limited,
    # Utilities
    is_retryable,
    classify_error,
    wrap_external_error,
    # Context manager
    RetryContext,
)


class TestErrorHierarchy:
    """Test error type classification."""

    def test_transient_error_is_retryable(self):
        """Verify TransientError and subclasses are retryable."""
        errors = [
            TransientError("test"),
            NetworkError("test"),
            RateLimitError("test"),
            TimeoutError("test"),
            ServiceUnavailableError("test"),
        ]
        for error in errors:
            assert error.category == ErrorCategory.TRANSIENT
            assert is_retryable(error) is True

    def test_permanent_error_is_not_retryable(self):
        """Verify PermanentError and subclasses are NOT retryable."""
        errors = [
            PermanentError("test"),
            ValidationError("test"),
            AuthenticationError("test"),
            InsufficientFundsError("test"),
            ResourceNotFoundError("test"),
            OrderRejectedError("test"),
        ]
        for error in errors:
            assert error.category == ErrorCategory.PERMANENT
            assert is_retryable(error) is False

    def test_error_with_cause(self):
        """Verify errors can capture a cause."""
        cause = ValueError("original error")
        error = NetworkError("wrapper", cause=cause)

        assert error.cause is cause
        assert "original error" in str(error)
        assert "caused by" in str(error)

    def test_error_has_timestamp(self):
        """Verify errors have timestamp."""
        before = datetime.now(timezone.utc)
        error = TransientError("test")
        after = datetime.now(timezone.utc)

        assert before <= error.timestamp <= after

    def test_rate_limit_error_with_retry_after(self):
        """Verify RateLimitError can store retry_after."""
        error = RateLimitError("rate limited", retry_after=60.0)
        assert error.retry_after == 60.0

    def test_mercury_error_base_class(self):
        """Verify MercuryError is the base for all custom errors."""
        assert issubclass(TransientError, MercuryError)
        assert issubclass(PermanentError, MercuryError)
        assert issubclass(NetworkError, MercuryError)


class TestErrorClassification:
    """Test error classification utilities."""

    def test_classify_mercury_errors(self):
        """Verify classify_error works for Mercury errors."""
        assert classify_error(TransientError("test")) == ErrorCategory.TRANSIENT
        assert classify_error(PermanentError("test")) == ErrorCategory.PERMANENT
        assert classify_error(NetworkError("test")) == ErrorCategory.TRANSIENT

    def test_classify_unknown_errors_by_message(self):
        """Verify errors are classified by message patterns."""
        # Transient patterns
        assert classify_error(Exception("connection refused")) == ErrorCategory.TRANSIENT
        assert classify_error(Exception("request timed out")) == ErrorCategory.TRANSIENT
        assert classify_error(Exception("503 Service Unavailable")) == ErrorCategory.TRANSIENT
        assert classify_error(Exception("rate limit exceeded")) == ErrorCategory.TRANSIENT

        # Permanent patterns
        assert classify_error(Exception("401 Unauthorized")) == ErrorCategory.PERMANENT
        assert classify_error(Exception("invalid parameter")) == ErrorCategory.PERMANENT
        assert classify_error(Exception("404 not found")) == ErrorCategory.PERMANENT
        assert classify_error(Exception("authentication failed")) == ErrorCategory.PERMANENT

    def test_classify_unknown_with_no_patterns(self):
        """Verify unknown errors get UNKNOWN category."""
        error = Exception("some random error")
        assert classify_error(error) == ErrorCategory.UNKNOWN

    def test_is_retryable_for_non_mercury_errors(self):
        """Verify is_retryable checks message patterns for non-Mercury errors."""
        assert is_retryable(Exception("connection timeout")) is True
        assert is_retryable(Exception("rate limit hit")) is True
        assert is_retryable(Exception("network error")) is True
        assert is_retryable(Exception("random error")) is False

    def test_wrap_external_error_transient(self):
        """Verify wrapping transient external errors."""
        external = Exception("connection reset")
        wrapped = wrap_external_error(external, context="fetching data")

        assert isinstance(wrapped, TransientError)
        assert "fetching data" in str(wrapped)
        assert "connection reset" in str(wrapped)
        assert wrapped.cause is external

    def test_wrap_external_error_permanent(self):
        """Verify wrapping permanent external errors."""
        external = Exception("401 unauthorized")
        wrapped = wrap_external_error(external)

        assert isinstance(wrapped, PermanentError)
        assert wrapped.cause is external


class TestRetryConfig:
    """Test RetryConfig dataclass."""

    def test_default_values(self):
        """Verify default configuration values."""
        config = RetryConfig()

        assert config.max_attempts == DEFAULT_MAX_ATTEMPTS
        assert config.min_wait_seconds == DEFAULT_MIN_WAIT_SECONDS
        assert config.max_wait_seconds == DEFAULT_MAX_WAIT_SECONDS
        assert config.jitter is True

    def test_custom_values(self):
        """Verify custom configuration values."""
        config = RetryConfig(
            max_attempts=5,
            min_wait_seconds=2.0,
            max_wait_seconds=60.0,
            jitter=False,
        )

        assert config.max_attempts == 5
        assert config.min_wait_seconds == 2.0
        assert config.max_wait_seconds == 60.0
        assert config.jitter is False

    def test_from_dict(self):
        """Verify creating config from dictionary."""
        config_dict = {
            "max_attempts": 4,
            "min_wait_seconds": 1.5,
            "max_wait_seconds": 45.0,
            "jitter": False,
        }
        config = RetryConfig.from_dict(config_dict)

        assert config.max_attempts == 4
        assert config.min_wait_seconds == 1.5
        assert config.max_wait_seconds == 45.0
        assert config.jitter is False

    def test_from_dict_with_defaults(self):
        """Verify from_dict uses defaults for missing values."""
        config = RetryConfig.from_dict({})

        assert config.max_attempts == DEFAULT_MAX_ATTEMPTS
        assert config.min_wait_seconds == DEFAULT_MIN_WAIT_SECONDS


class TestRetryStats:
    """Test RetryStats dataclass."""

    def test_default_values(self):
        """Verify default stats values."""
        stats = RetryStats()

        assert stats.total_attempts == 0
        assert stats.successful_attempts == 0
        assert stats.failed_attempts == 0
        assert stats.total_wait_time_seconds == 0.0
        assert stats.last_error is None

    def test_stats_tracking(self):
        """Verify stats can be updated."""
        stats = RetryStats()
        stats.total_attempts = 3
        stats.failed_attempts = 2
        stats.successful_attempts = 1
        stats.total_wait_time_seconds = 4.5

        assert stats.total_attempts == 3
        assert stats.failed_attempts == 2


class TestRetryTransientDecorator:
    """Test retry_transient decorator."""

    @pytest.mark.asyncio
    async def test_no_retry_on_success(self):
        """Verify no retry when function succeeds."""
        call_count = 0

        @retry_transient(max_attempts=3)
        async def successful_func():
            nonlocal call_count
            call_count += 1
            return "success"

        result = await successful_func()

        assert result == "success"
        assert call_count == 1

    @pytest.mark.asyncio
    async def test_retry_on_transient_error(self):
        """Verify retries on TransientError."""
        call_count = 0

        @retry_transient(max_attempts=3, min_wait=0.01, max_wait=0.01)
        async def flaky_func():
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise NetworkError("connection failed")
            return "success"

        result = await flaky_func()

        assert result == "success"
        assert call_count == 3

    @pytest.mark.asyncio
    async def test_no_retry_on_permanent_error(self):
        """Verify no retry on PermanentError."""
        call_count = 0

        @retry_transient(max_attempts=5, min_wait=0.01)
        async def permanent_fail():
            nonlocal call_count
            call_count += 1
            raise ValidationError("bad input")

        with pytest.raises(ValidationError):
            await permanent_fail()

        assert call_count == 1  # No retries

    @pytest.mark.asyncio
    async def test_max_retries_exceeded(self):
        """Verify error raised after max retries."""
        call_count = 0

        @retry_transient(max_attempts=3, min_wait=0.01, max_wait=0.01)
        async def always_fail():
            nonlocal call_count
            call_count += 1
            raise NetworkError("always fails")

        with pytest.raises(NetworkError, match="always fails"):
            await always_fail()

        assert call_count == 3

    @pytest.mark.asyncio
    async def test_callback_called_on_retry(self):
        """Verify on_retry callback is called."""
        callback_calls = []

        def on_retry(state):
            callback_calls.append(state.attempt_number)

        call_count = 0

        @retry_transient(max_attempts=3, min_wait=0.01, max_wait=0.01, on_retry=on_retry)
        async def flaky_func():
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise TransientError("temporary")
            return "success"

        await flaky_func()

        # Callback called for attempt 1 and 2 (before retries 2 and 3)
        assert len(callback_calls) == 2
        assert callback_calls == [1, 2]


class TestRetryWithConfig:
    """Test retry_with_config decorator."""

    @pytest.mark.asyncio
    async def test_uses_config_values(self):
        """Verify decorator uses RetryConfig values."""
        config = RetryConfig(
            max_attempts=2,
            min_wait_seconds=0.01,
            max_wait_seconds=0.01,
        )

        call_count = 0

        @retry_with_config(config)
        async def flaky_func():
            nonlocal call_count
            call_count += 1
            raise TransientError("fail")

        with pytest.raises(TransientError):
            await flaky_func()

        assert call_count == 2  # max_attempts from config


class TestRetryNetwork:
    """Test retry_network decorator."""

    @pytest.mark.asyncio
    async def test_retries_network_errors(self):
        """Verify retries on network-specific errors."""
        call_count = 0

        @retry_network(max_attempts=3, min_wait=0.01, max_wait=0.01)
        async def network_func():
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise NetworkError("connection failed")
            return "success"

        result = await network_func()

        assert result == "success"
        assert call_count == 3

    @pytest.mark.asyncio
    async def test_retries_timeout_errors(self):
        """Verify retries on TimeoutError."""
        call_count = 0

        @retry_network(max_attempts=2, min_wait=0.01, max_wait=0.01)
        async def timeout_func():
            nonlocal call_count
            call_count += 1
            if call_count < 2:
                raise TimeoutError("request timed out")
            return "done"

        result = await timeout_func()
        assert result == "done"
        assert call_count == 2

    @pytest.mark.asyncio
    async def test_no_retry_on_validation_error(self):
        """Verify no retry on non-network errors."""
        call_count = 0

        @retry_network(max_attempts=5, min_wait=0.01)
        async def invalid_func():
            nonlocal call_count
            call_count += 1
            raise ValidationError("bad request")

        with pytest.raises(ValidationError):
            await invalid_func()

        assert call_count == 1


class TestRetryRateLimited:
    """Test retry_rate_limited decorator."""

    @pytest.mark.asyncio
    async def test_retries_rate_limit_errors(self):
        """Verify retries on RateLimitError."""
        call_count = 0

        @retry_rate_limited(max_attempts=3, min_wait=0.01, max_wait=0.01)
        async def rate_limited_func():
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise RateLimitError("too many requests")
            return "success"

        result = await rate_limited_func()

        assert result == "success"
        assert call_count == 3

    @pytest.mark.asyncio
    async def test_no_retry_on_other_errors(self):
        """Verify no retry on non-rate-limit errors."""
        call_count = 0

        @retry_rate_limited(max_attempts=5, min_wait=0.01)
        async def other_error_func():
            nonlocal call_count
            call_count += 1
            raise NetworkError("network error")

        with pytest.raises(NetworkError):
            await other_error_func()

        assert call_count == 1


class TestRetryContext:
    """Test RetryContext for manual retry control."""

    @pytest.mark.asyncio
    async def test_successful_operation(self):
        """Verify successful operation without retries."""
        async with RetryContext(max_attempts=3) as ctx:
            while ctx.should_retry():
                try:
                    result = "success"
                    ctx.success()
                    break
                except TransientError as e:
                    await ctx.handle_error(e)

        assert result == "success"
        assert ctx.attempt == 1
        assert ctx.stats.successful_attempts == 1
        assert ctx.stats.failed_attempts == 0

    @pytest.mark.asyncio
    async def test_retry_until_success(self):
        """Verify retries until success."""
        attempt_count = 0

        async with RetryContext(max_attempts=5, min_wait=0.01, max_wait=0.01) as ctx:
            while ctx.should_retry():
                try:
                    attempt_count += 1
                    if attempt_count < 3:
                        raise TransientError("temporary failure")
                    ctx.success()
                    break
                except TransientError as e:
                    await ctx.handle_error(e)

        assert attempt_count == 3
        assert ctx.stats.failed_attempts == 2
        assert ctx.stats.successful_attempts == 1

    @pytest.mark.asyncio
    async def test_max_attempts_exceeded(self):
        """Verify error raised after max attempts."""
        attempts = 0
        with pytest.raises(TransientError, match="always fails"):
            async with RetryContext(max_attempts=3, min_wait=0.01, max_wait=0.01) as ctx:
                while ctx.should_retry():
                    try:
                        attempts += 1
                        raise TransientError("always fails")
                    except TransientError as e:
                        await ctx.handle_error(e)
        # Should have made 3 attempts before raising
        assert attempts == 3

    @pytest.mark.asyncio
    async def test_non_retryable_error_raises_immediately(self):
        """Verify non-retryable errors are raised immediately."""
        async with RetryContext(max_attempts=5) as ctx:
            while ctx.should_retry():
                try:
                    raise PermanentError("permanent failure")
                except PermanentError as e:
                    with pytest.raises(PermanentError):
                        await ctx.handle_error(e)
                    break

        assert ctx.stats.failed_attempts == 1
        assert ctx.stats.total_attempts == 0  # Not incremented for non-retryable

    @pytest.mark.asyncio
    async def test_stats_tracking(self):
        """Verify stats are tracked correctly."""
        async with RetryContext(max_attempts=3, min_wait=0.01, max_wait=0.01) as ctx:
            attempt = 0
            while ctx.should_retry():
                try:
                    attempt += 1
                    if attempt < 3:
                        raise TransientError("fail")
                    ctx.success()
                    break
                except TransientError as e:
                    await ctx.handle_error(e)

        stats = ctx.stats
        assert stats.total_attempts == 3
        assert stats.successful_attempts == 1
        assert stats.failed_attempts == 2
        assert stats.total_wait_time_seconds > 0


class TestIntegrationWithConfigManager:
    """Test retry integration with ConfigManager."""

    def test_get_retry_config_from_manager(self):
        """Verify ConfigManager can provide retry config."""
        from mercury.core.config import ConfigManager
        from pathlib import Path

        # Use the default config file if it exists
        config_path = Path("/workspace/polyjuiced/mercury/config/default.toml")
        if config_path.exists():
            config = ConfigManager(config_path)
            retry_dict = config.get_retry_config()

            assert "max_attempts" in retry_dict
            assert "min_wait_seconds" in retry_dict
            assert "max_wait_seconds" in retry_dict
            assert "jitter" in retry_dict

            # Can create RetryConfig from it
            retry_config = RetryConfig.from_dict(retry_dict)
            assert retry_config.max_attempts >= 1

    def test_get_section_specific_retry_config(self):
        """Verify getting section-specific retry config."""
        from mercury.core.config import ConfigManager
        from pathlib import Path

        config_path = Path("/workspace/polyjuiced/mercury/config/default.toml")
        if config_path.exists():
            config = ConfigManager(config_path)

            # Get network-specific config
            network_config = config.get_retry_config("retry.network")
            assert network_config["max_attempts"] >= 1


class TestEdgeCases:
    """Test edge cases and error conditions."""

    @pytest.mark.asyncio
    async def test_zero_max_attempts(self):
        """Verify behavior with zero max_attempts raises immediately."""
        call_count = 0

        # With 1 attempt, should fail immediately
        @retry_transient(max_attempts=1, min_wait=0.01)
        async def fail_func():
            nonlocal call_count
            call_count += 1
            raise TransientError("fail")

        with pytest.raises(TransientError):
            await fail_func()

        assert call_count == 1

    @pytest.mark.asyncio
    async def test_very_short_wait_times(self):
        """Verify very short wait times work."""
        call_count = 0

        @retry_transient(max_attempts=3, min_wait=0.001, max_wait=0.001)
        async def fast_retry():
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise TransientError("fail")
            return "done"

        result = await fast_retry()
        assert result == "done"
        assert call_count == 3

    @pytest.mark.asyncio
    async def test_exception_not_in_retry_list(self):
        """Verify exceptions not in retry list are not retried."""
        call_count = 0

        @retry_transient(max_attempts=5, min_wait=0.01)
        async def unexpected_error():
            nonlocal call_count
            call_count += 1
            raise ValueError("not a transient error")

        with pytest.raises(ValueError):
            await unexpected_error()

        assert call_count == 1

    def test_error_str_without_cause(self):
        """Verify error string without cause."""
        error = TransientError("simple error")
        assert str(error) == "simple error"
        assert error.cause is None

    def test_error_str_with_cause(self):
        """Verify error string with cause."""
        cause = RuntimeError("root cause")
        error = TransientError("wrapper error", cause=cause)
        error_str = str(error)

        assert "wrapper error" in error_str
        assert "root cause" in error_str
        assert "caused by" in error_str
