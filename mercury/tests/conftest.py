"""
Shared pytest fixtures for Mercury tests.
"""
import pytest
import asyncio
from unittest.mock import MagicMock, AsyncMock


@pytest.fixture(scope="session")
def event_loop():
    """Create an instance of the default event loop for the test session."""
    loop = asyncio.get_event_loop_policy().new_event_loop()
    yield loop
    loop.close()


@pytest.fixture
def mock_config():
    """Mock ConfigManager for unit tests."""
    config = MagicMock()
    config.get.return_value = None
    return config


@pytest.fixture
def mock_event_bus():
    """Mock EventBus for unit tests."""
    bus = MagicMock()
    bus.publish = AsyncMock()
    bus.subscribe = AsyncMock()
    bus.unsubscribe = AsyncMock()
    return bus


@pytest.fixture
def mock_redis():
    """Mock Redis connection for unit tests."""
    redis = MagicMock()
    redis.ping = AsyncMock(return_value=True)
    redis.publish = AsyncMock()
    redis.subscribe = AsyncMock()
    return redis
