"""Test fixtures for Polymarket bot integration tests.

This package provides:
- Mock clients for exchange API
- Mock WebSocket for real-time updates
- Mock database for persistence
- Pre-defined test scenarios
- ScenarioRunner for orchestrating E2E tests
"""

from .mock_client import MockPolymarketClient, MockOrderResult, MockOrderBook
from .mock_websocket import MockPolymarketWebSocket
from .mock_database import MockDatabase
from .scenarios import (
    MarketScenario,
    ExecutionScenario,
    PriceMovementScenario,
    CompleteScenario,
    MARKETS,
    EXECUTION_SCENARIOS,
    REBALANCING_SCENARIOS,
    COMPLETE_SCENARIOS,
)
from .scenario_runner import (
    ScenarioRunner,
    RunResult,
    create_runner,
    run_complete_scenario,
)

__all__ = [
    # Mocks
    "MockPolymarketClient",
    "MockOrderResult",
    "MockOrderBook",
    "MockPolymarketWebSocket",
    "MockDatabase",
    # Scenarios
    "MarketScenario",
    "ExecutionScenario",
    "PriceMovementScenario",
    "CompleteScenario",
    "MARKETS",
    "EXECUTION_SCENARIOS",
    "REBALANCING_SCENARIOS",
    "COMPLETE_SCENARIOS",
    # Runner
    "ScenarioRunner",
    "RunResult",
    "create_runner",
    "run_complete_scenario",
]
