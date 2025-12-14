"""Shared pytest fixtures for Polymarket bot tests.

This file provides common fixtures used across all test modules:
- Mock clients (exchange, WebSocket, database)
- Configuration fixtures
- Market and scenario fixtures
"""

import pytest
import asyncio
from datetime import datetime, timedelta
from unittest.mock import MagicMock, AsyncMock
from dataclasses import dataclass
from typing import Dict, Any

# Import mock fixtures
from tests.fixtures.mock_client import MockPolymarketClient, MockOrderResult
from tests.fixtures.mock_websocket import MockPolymarketWebSocket
from tests.fixtures.mock_database import MockDatabase
from tests.fixtures.scenarios import (
    MARKETS,
    EXECUTION_SCENARIOS,
    REBALANCING_SCENARIOS,
    COMPLETE_SCENARIOS,
    MarketScenario,
    ExecutionScenario,
)
from tests.fixtures.scenario_runner import ScenarioRunner, create_runner


# =============================================================================
# Event Loop Configuration
# =============================================================================

@pytest.fixture(scope="session")
def event_loop():
    """Create event loop for async tests."""
    loop = asyncio.get_event_loop_policy().new_event_loop()
    yield loop
    loop.close()


# =============================================================================
# Mock Client Fixtures
# =============================================================================

@pytest.fixture
def mock_client() -> MockPolymarketClient:
    """Create a fresh MockPolymarketClient for each test."""
    client = MockPolymarketClient()
    yield client
    client.reset()


@pytest.fixture
def mock_ws() -> MockPolymarketWebSocket:
    """Create a fresh MockPolymarketWebSocket for each test."""
    ws = MockPolymarketWebSocket()
    yield ws
    ws.reset()


@pytest.fixture
def mock_db() -> MockDatabase:
    """Create a fresh MockDatabase for each test."""
    db = MockDatabase()
    yield db
    db.reset()


# =============================================================================
# Configuration Fixtures
# =============================================================================

@dataclass
class MockGabagoolConfig:
    """Mock configuration for GabagoolStrategy."""
    enabled: bool = True
    dry_run: bool = False
    min_spread_threshold: float = 0.02  # 2 cents
    max_trade_size_usd: float = 10.0
    max_daily_trades: int = 100
    max_daily_exposure_usd: float = 1000.0
    min_hedge_ratio: float = 0.80
    parallel_execution_enabled: bool = True
    parallel_fill_timeout_seconds: float = 5.0
    max_liquidity_consumption_pct: float = 0.50
    liquidity_buffer_pct: float = 2.0
    balance_sizing_enabled: bool = False
    balance_sizing_pct: float = 0.25


@dataclass
class MockConfig:
    """Mock full configuration."""
    gabagool: MockGabagoolConfig = None

    def __post_init__(self):
        if self.gabagool is None:
            self.gabagool = MockGabagoolConfig()


@pytest.fixture
def mock_config() -> MockConfig:
    """Create default mock configuration."""
    return MockConfig()


@pytest.fixture
def mock_config_dry_run() -> MockConfig:
    """Create mock config with dry_run enabled."""
    config = MockConfig()
    config.gabagool.dry_run = True
    return config


@pytest.fixture
def mock_config_parallel() -> MockConfig:
    """Create mock config with parallel execution."""
    config = MockConfig()
    config.gabagool.parallel_execution_enabled = True
    config.gabagool.parallel_fill_timeout_seconds = 5.0
    return config


# =============================================================================
# Market Fixtures
# =============================================================================

@pytest.fixture
def btc_market() -> MarketScenario:
    """Standard BTC market with 3 cent spread."""
    return MARKETS["btc_3c_spread"]


@pytest.fixture
def eth_market() -> MarketScenario:
    """Standard ETH market with 3 cent spread."""
    return MARKETS["eth_3c_spread"]


@pytest.fixture
def low_liquidity_market() -> MarketScenario:
    """Market with low liquidity for failure testing."""
    return MARKETS["btc_low_liquidity"]


@pytest.fixture
def ending_soon_market() -> MarketScenario:
    """Market ending in 30 seconds."""
    market = MARKETS["btc_ending_soon"]
    # Override end time to be 30 seconds from now
    return market


# =============================================================================
# Execution Scenario Fixtures
# =============================================================================

@pytest.fixture
def perfect_fill() -> ExecutionScenario:
    """Perfect fill scenario."""
    return EXECUTION_SCENARIOS["perfect_fill"]


@pytest.fixture
def partial_fill_60pct() -> ExecutionScenario:
    """60% hedge ratio partial fill."""
    return EXECUTION_SCENARIOS["partial_fill_60pct"]


@pytest.fixture
def yes_fills_no_rejected() -> ExecutionScenario:
    """YES fills, NO rejected."""
    return EXECUTION_SCENARIOS["yes_fills_no_rejected"]


@pytest.fixture
def both_rejected() -> ExecutionScenario:
    """Both orders rejected."""
    return EXECUTION_SCENARIOS["both_rejected"]


# =============================================================================
# Mock Market Object
# =============================================================================

@dataclass
class MockMarket15Min:
    """Mock Market15Min object for testing."""
    asset: str
    condition_id: str
    yes_token_id: str
    no_token_id: str
    slug: str
    end_time: datetime
    question: str = ""

    @property
    def seconds_remaining(self) -> float:
        return (self.end_time - datetime.utcnow()).total_seconds()


@pytest.fixture
def mock_market(btc_market: MarketScenario) -> MockMarket15Min:
    """Create mock Market15Min from scenario."""
    return MockMarket15Min(
        asset=btc_market.asset,
        condition_id=btc_market.condition_id,
        yes_token_id=btc_market.yes_token_id,
        no_token_id=btc_market.no_token_id,
        slug=btc_market.slug,
        end_time=btc_market.end_time,
        question=f"Will {btc_market.asset} go up in the next 15 minutes?",
    )


# =============================================================================
# Configured Client Fixtures
# =============================================================================

@pytest.fixture
def client_with_btc_market(mock_client: MockPolymarketClient, btc_market: MarketScenario):
    """Client configured with BTC market order books."""
    mock_client.set_order_book(
        btc_market.yes_token_id,
        asks=btc_market.yes_asks,
        bids=btc_market.yes_bids,
    )
    mock_client.set_order_book(
        btc_market.no_token_id,
        asks=btc_market.no_asks,
        bids=btc_market.no_bids,
    )
    return mock_client


@pytest.fixture
def client_perfect_fill(
    client_with_btc_market: MockPolymarketClient,
    btc_market: MarketScenario,
    perfect_fill: ExecutionScenario,
):
    """Client configured for perfect fill execution."""
    client_with_btc_market.set_order_result(
        btc_market.yes_token_id,
        MockOrderResult(
            order_id="yes-order-001",
            status=perfect_fill.yes_result,
            size=perfect_fill.yes_fill_size,
            price=btc_market.yes_best_ask,
            size_matched=perfect_fill.yes_fill_size,
            side="BUY",
            token_id=btc_market.yes_token_id,
        ),
    )
    client_with_btc_market.set_order_result(
        btc_market.no_token_id,
        MockOrderResult(
            order_id="no-order-001",
            status=perfect_fill.no_result,
            size=perfect_fill.no_fill_size,
            price=btc_market.no_best_ask,
            size_matched=perfect_fill.no_fill_size,
            side="BUY",
            token_id=btc_market.no_token_id,
        ),
    )
    return client_with_btc_market


@pytest.fixture
def client_partial_fill(
    client_with_btc_market: MockPolymarketClient,
    btc_market: MarketScenario,
    partial_fill_60pct: ExecutionScenario,
):
    """Client configured for 60% partial fill."""
    client_with_btc_market.set_order_result(
        btc_market.yes_token_id,
        MockOrderResult(
            order_id="yes-order-001",
            status=partial_fill_60pct.yes_result,
            size=partial_fill_60pct.yes_fill_size,
            price=btc_market.yes_best_ask,
            size_matched=partial_fill_60pct.yes_fill_size,
            side="BUY",
            token_id=btc_market.yes_token_id,
        ),
    )
    client_with_btc_market.set_order_result(
        btc_market.no_token_id,
        MockOrderResult(
            order_id="no-order-001",
            status=partial_fill_60pct.no_result,
            size=partial_fill_60pct.no_fill_size,
            price=btc_market.no_best_ask,
            size_matched=partial_fill_60pct.no_fill_size,
            side="BUY",
            token_id=btc_market.no_token_id,
        ),
    )
    return client_with_btc_market


# =============================================================================
# Helper Functions
# =============================================================================

def configure_client_for_scenario(
    client: MockPolymarketClient,
    market: MarketScenario,
    execution: ExecutionScenario,
) -> None:
    """Configure a client for a specific test scenario.

    Args:
        client: MockPolymarketClient to configure
        market: Market scenario with order books
        execution: Execution scenario with results
    """
    # Set order books
    client.set_order_book(
        market.yes_token_id,
        asks=market.yes_asks,
        bids=market.yes_bids,
    )
    client.set_order_book(
        market.no_token_id,
        asks=market.no_asks,
        bids=market.no_bids,
    )

    # Set order results
    client.set_order_result(
        market.yes_token_id,
        MockOrderResult(
            order_id=f"yes-{market.condition_id[:8]}",
            status=execution.yes_result,
            size=execution.yes_fill_size,
            price=market.yes_best_ask,
            size_matched=execution.yes_fill_size,
            side="BUY",
            token_id=market.yes_token_id,
        ),
    )
    client.set_order_result(
        market.no_token_id,
        MockOrderResult(
            order_id=f"no-{market.condition_id[:8]}",
            status=execution.no_result,
            size=execution.no_fill_size,
            price=market.no_best_ask,
            size_matched=execution.no_fill_size,
            side="BUY",
            token_id=market.no_token_id,
        ),
    )


def create_mock_market_from_scenario(scenario: MarketScenario) -> MockMarket15Min:
    """Create a MockMarket15Min from a MarketScenario."""
    return MockMarket15Min(
        asset=scenario.asset,
        condition_id=scenario.condition_id,
        yes_token_id=scenario.yes_token_id,
        no_token_id=scenario.no_token_id,
        slug=scenario.slug,
        end_time=scenario.end_time,
    )


# =============================================================================
# Scenario Runner Fixtures
# =============================================================================

@pytest.fixture
def scenario_runner(
    mock_client: MockPolymarketClient,
    mock_ws: MockPolymarketWebSocket,
    mock_db: MockDatabase,
    mock_config: MockConfig,
) -> ScenarioRunner:
    """Create a ScenarioRunner with all mock dependencies.

    This fixture provides a fully configured runner for E2E testing.
    """
    runner = ScenarioRunner(
        client=mock_client,
        ws=mock_ws,
        db=mock_db,
        config=mock_config,
    )
    yield runner
    runner.reset()


@pytest.fixture
def runner_with_btc_market(
    scenario_runner: ScenarioRunner,
    btc_market: MarketScenario,
) -> ScenarioRunner:
    """ScenarioRunner pre-configured with BTC market."""
    import asyncio
    asyncio.get_event_loop().run_until_complete(
        scenario_runner.setup_market(btc_market)
    )
    return scenario_runner


@pytest.fixture
def runner_perfect_fill(
    scenario_runner: ScenarioRunner,
    btc_market: MarketScenario,
    perfect_fill: ExecutionScenario,
) -> ScenarioRunner:
    """ScenarioRunner configured for perfect fill scenario."""
    import asyncio
    loop = asyncio.get_event_loop()
    loop.run_until_complete(scenario_runner.setup_market(btc_market))
    loop.run_until_complete(scenario_runner.configure_execution(perfect_fill))
    return scenario_runner


@pytest.fixture
def runner_partial_fill(
    scenario_runner: ScenarioRunner,
    btc_market: MarketScenario,
    partial_fill_60pct: ExecutionScenario,
) -> ScenarioRunner:
    """ScenarioRunner configured for partial fill scenario."""
    import asyncio
    loop = asyncio.get_event_loop()
    loop.run_until_complete(scenario_runner.setup_market(btc_market))
    loop.run_until_complete(scenario_runner.configure_execution(partial_fill_60pct))
    return scenario_runner
