"""
Pytest configuration for performance tests.
"""
import pytest
import asyncio


@pytest.fixture(scope="session")
def event_loop():
    """Create an instance of the default event loop for the test session."""
    loop = asyncio.get_event_loop_policy().new_event_loop()
    yield loop
    loop.close()


@pytest.fixture
def performance_config():
    """Configuration for performance tests."""
    return {
        "target_latency_ms": 100,
        "min_throughput_per_sec": 1000,
        "max_memory_growth_mb": 50,
        "test_duration_seconds": 3,
    }
