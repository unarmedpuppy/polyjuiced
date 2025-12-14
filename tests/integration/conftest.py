"""Integration test fixtures.

This module provides fixtures specific to integration testing,
building on the shared fixtures from tests/conftest.py.
"""

import pytest
import asyncio
from typing import AsyncGenerator

from tests.fixtures import (
    MockPolymarketClient,
    MockPolymarketWebSocket,
    MockDatabase,
    ScenarioRunner,
    MARKETS,
    EXECUTION_SCENARIOS,
    REBALANCING_SCENARIOS,
    COMPLETE_SCENARIOS,
)


# =============================================================================
# Async Test Support
# =============================================================================

@pytest.fixture(scope="function")
def event_loop():
    """Create a new event loop for each test function."""
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


# =============================================================================
# Complete Scenario Fixtures
# =============================================================================

@pytest.fixture
def complete_scenario_success():
    """Standard successful arbitrage scenario."""
    return COMPLETE_SCENARIOS["standard_arb_success"]


@pytest.fixture
def complete_scenario_partial_rebalance():
    """Partial fill with rebalancing opportunity."""
    return COMPLETE_SCENARIOS["partial_fill_then_rebalance"]


@pytest.fixture
def complete_scenario_one_leg():
    """One leg fills, hold to resolution."""
    return COMPLETE_SCENARIOS["one_leg_fills_hold_to_resolution"]


@pytest.fixture
def complete_scenario_low_liquidity():
    """Low liquidity prevents trade."""
    return COMPLETE_SCENARIOS["low_liquidity_no_trade"]


# =============================================================================
# Pre-configured Runners
# =============================================================================

@pytest.fixture
async def runner_for_success(
    mock_client: MockPolymarketClient,
    mock_ws: MockPolymarketWebSocket,
    mock_db: MockDatabase,
) -> AsyncGenerator[ScenarioRunner, None]:
    """Runner configured for successful trade execution."""
    runner = ScenarioRunner(mock_client, mock_ws, mock_db)
    await runner.setup_market(MARKETS["btc_3c_spread"])
    await runner.configure_execution(EXECUTION_SCENARIOS["perfect_fill"])
    yield runner
    runner.reset()


@pytest.fixture
async def runner_for_partial_fill(
    mock_client: MockPolymarketClient,
    mock_ws: MockPolymarketWebSocket,
    mock_db: MockDatabase,
) -> AsyncGenerator[ScenarioRunner, None]:
    """Runner configured for partial fill scenario."""
    runner = ScenarioRunner(mock_client, mock_ws, mock_db)
    await runner.setup_market(MARKETS["btc_3c_spread"])
    await runner.configure_execution(EXECUTION_SCENARIOS["partial_fill_60pct"])
    await runner.configure_price_movement(REBALANCING_SCENARIOS["sell_excess_yes_profitable"])
    yield runner
    runner.reset()


@pytest.fixture
async def runner_for_failure(
    mock_client: MockPolymarketClient,
    mock_ws: MockPolymarketWebSocket,
    mock_db: MockDatabase,
) -> AsyncGenerator[ScenarioRunner, None]:
    """Runner configured for order failure scenario."""
    runner = ScenarioRunner(mock_client, mock_ws, mock_db)
    await runner.setup_market(MARKETS["btc_3c_spread"])
    await runner.configure_execution(EXECUTION_SCENARIOS["both_rejected"])
    yield runner
    runner.reset()


# =============================================================================
# Market Variant Fixtures
# =============================================================================

@pytest.fixture
def all_markets():
    """All available market scenarios."""
    return MARKETS


@pytest.fixture
def all_execution_scenarios():
    """All available execution scenarios."""
    return EXECUTION_SCENARIOS


@pytest.fixture
def all_rebalancing_scenarios():
    """All available rebalancing scenarios."""
    return REBALANCING_SCENARIOS
