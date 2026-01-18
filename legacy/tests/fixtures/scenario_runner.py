"""Scenario Runner for E2E Integration Tests.

Orchestrates test execution by:
- Configuring mocks with market state
- Setting up execution results
- Running opportunities through the strategy
- Simulating price movements for rebalancing
- Providing assertion helpers for validation
"""

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Optional

from .mock_client import MockPolymarketClient, MockOrderResult
from .mock_websocket import MockPolymarketWebSocket
from .mock_database import MockDatabase
from .scenarios import (
    MarketScenario,
    ExecutionScenario,
    PriceMovementScenario,
    CompleteScenario,
)


@dataclass
class RunResult:
    """Result of running a scenario."""
    success: bool
    partial_fill: bool = False
    error: Optional[str] = None
    yes_filled_size: float = 0.0
    no_filled_size: float = 0.0
    hedge_ratio: float = 0.0
    trade_id: Optional[str] = None
    execution_time_ms: float = 0.0
    raw_result: Dict[str, Any] = field(default_factory=dict)


class ScenarioRunner:
    """Executes test scenarios against the strategy.

    Usage:
        runner = ScenarioRunner(mock_client, mock_ws, mock_db)
        await runner.setup_market(MARKETS["btc_3c_spread"])
        await runner.configure_execution(EXECUTION_SCENARIOS["perfect_fill"])

        result = await runner.run_opportunity(budget=10.0)

        assert result.success
        runner.assert_execution_matches(EXECUTION_SCENARIOS["perfect_fill"])
    """

    def __init__(
        self,
        client: MockPolymarketClient,
        ws: MockPolymarketWebSocket,
        db: MockDatabase,
        config: Any = None,
    ):
        self.client = client
        self.ws = ws
        self.db = db
        self.config = config

        # Track current market and execution config
        self._market: Optional[MarketScenario] = None
        self._execution: Optional[ExecutionScenario] = None
        self._price_movement: Optional[PriceMovementScenario] = None

        # Results tracking
        self._run_results: List[RunResult] = []
        self._rebalance_results: List[Dict[str, Any]] = []

        # Callbacks for custom behavior
        self._on_opportunity_detected: Optional[Callable] = None
        self._on_trade_executed: Optional[Callable] = None
        self._on_rebalance_triggered: Optional[Callable] = None

        # Timing control
        self._time_compression_factor: float = 100.0  # 1 second = 10ms in tests

    # =========================================================================
    # Setup Methods
    # =========================================================================

    async def setup_market(self, scenario: MarketScenario) -> None:
        """Configure mocks with market state.

        Args:
            scenario: Market scenario to configure
        """
        self._market = scenario

        # Set up YES token order book
        self.client.set_order_book(
            scenario.yes_token_id,
            asks=scenario.yes_asks,
            bids=scenario.yes_bids,
        )

        # Set up NO token order book
        self.client.set_order_book(
            scenario.no_token_id,
            asks=scenario.no_asks,
            bids=scenario.no_bids,
        )

        # Configure WebSocket with default prices
        self.ws.set_default_prices(
            scenario.yes_token_id,
            bid=scenario.yes_bids[0][0] if scenario.yes_bids else 0.47,
            ask=scenario.yes_asks[0][0] if scenario.yes_asks else 0.48,
        )
        self.ws.set_default_prices(
            scenario.no_token_id,
            bid=scenario.no_bids[0][0] if scenario.no_bids else 0.48,
            ask=scenario.no_asks[0][0] if scenario.no_asks else 0.49,
        )

    async def configure_execution(self, scenario: ExecutionScenario) -> None:
        """Configure how orders will execute.

        Args:
            scenario: Execution scenario to configure
        """
        if self._market is None:
            raise RuntimeError("Must call setup_market() before configure_execution()")

        self._execution = scenario

        # Configure YES order result
        self.client.set_order_result(
            self._market.yes_token_id,
            MockOrderResult(
                order_id=f"yes-{self._market.condition_id[:8]}",
                status=scenario.yes_result,
                size=scenario.yes_fill_size,
                price=self._market.yes_best_ask,
                size_matched=scenario.yes_fill_size,
                side="BUY",
                token_id=self._market.yes_token_id,
            ),
        )

        # Configure NO order result
        self.client.set_order_result(
            self._market.no_token_id,
            MockOrderResult(
                order_id=f"no-{self._market.condition_id[:8]}",
                status=scenario.no_result,
                size=scenario.no_fill_size,
                price=self._market.no_best_ask,
                size_matched=scenario.no_fill_size,
                side="BUY",
                token_id=self._market.no_token_id,
            ),
        )

    async def configure_price_movement(self, scenario: PriceMovementScenario) -> None:
        """Configure price movement for rebalancing tests.

        Args:
            scenario: Price movement scenario
        """
        self._price_movement = scenario

    async def setup_complete_scenario(self, scenario: CompleteScenario) -> None:
        """Setup a complete scenario (market + execution + optional price movement).

        Args:
            scenario: Complete test scenario
        """
        await self.setup_market(scenario.market)
        await self.configure_execution(scenario.execution)
        if scenario.price_movement:
            await self.configure_price_movement(scenario.price_movement)

    # =========================================================================
    # Execution Methods
    # =========================================================================

    async def run_opportunity(
        self,
        budget: float = 10.0,
        timeout_seconds: float = 5.0,
    ) -> RunResult:
        """Execute an arbitrage opportunity.

        This simulates what happens when the strategy detects an opportunity
        and executes a dual-leg order.

        Args:
            budget: Budget for the trade in USD
            timeout_seconds: Execution timeout

        Returns:
            RunResult with execution outcome
        """
        if self._market is None:
            raise RuntimeError("Must call setup_market() before run_opportunity()")

        start_time = datetime.utcnow()

        # Calculate order sizes
        yes_price = self._market.yes_best_ask
        no_price = self._market.no_best_ask
        yes_amount_usd = budget / 2
        no_amount_usd = budget / 2

        # Fire callback if registered
        if self._on_opportunity_detected:
            self._on_opportunity_detected(self._market)

        # Execute dual-leg order
        raw_result = await self.client.execute_dual_leg_order_parallel(
            yes_token_id=self._market.yes_token_id,
            no_token_id=self._market.no_token_id,
            yes_amount_usd=yes_amount_usd,
            no_amount_usd=no_amount_usd,
            yes_price=yes_price,
            no_price=no_price,
            timeout_seconds=timeout_seconds,
            condition_id=self._market.condition_id,
            asset=self._market.asset,
        )

        end_time = datetime.utcnow()
        execution_time_ms = (end_time - start_time).total_seconds() * 1000

        # Extract results
        yes_filled = raw_result.get("yes_filled_size", 0.0)
        no_filled = raw_result.get("no_filled_size", 0.0)
        success = raw_result.get("success", False)
        partial_fill = raw_result.get("partial_fill", False)

        # Calculate hedge ratio
        if yes_filled > 0 and no_filled > 0:
            hedge_ratio = min(yes_filled, no_filled) / max(yes_filled, no_filled)
        elif yes_filled == 0 and no_filled == 0:
            hedge_ratio = 0.0
        else:
            hedge_ratio = 0.0  # One-sided fill

        # Record trade in mock database
        trade_id = None
        if yes_filled > 0 or no_filled > 0:
            trade_id = await self.db.record_trade(
                asset=self._market.asset,
                condition_id=self._market.condition_id,
                yes_token_id=self._market.yes_token_id,
                no_token_id=self._market.no_token_id,
                yes_shares=yes_filled,
                no_shares=no_filled,
                yes_price=yes_price,
                no_price=no_price,
                total_cost_usd=yes_filled * yes_price + no_filled * no_price,
                expected_profit=self._calculate_expected_profit(
                    yes_filled, no_filled, yes_price, no_price
                ),
                hedge_ratio=hedge_ratio,
                execution_status=self._determine_execution_status(
                    success, partial_fill, yes_filled, no_filled
                ),
                needs_rebalancing=hedge_ratio < 0.8 and (yes_filled > 0 or no_filled > 0),
            )

            # Fire callback if registered
            if self._on_trade_executed:
                self._on_trade_executed(trade_id, raw_result)

        result = RunResult(
            success=success,
            partial_fill=partial_fill,
            error=raw_result.get("error"),
            yes_filled_size=yes_filled,
            no_filled_size=no_filled,
            hedge_ratio=hedge_ratio,
            trade_id=trade_id,
            execution_time_ms=execution_time_ms,
            raw_result=raw_result,
        )

        self._run_results.append(result)
        return result

    async def simulate_price_movement(
        self,
        scenario: PriceMovementScenario = None,
        emit_to_websocket: bool = True,
    ) -> None:
        """Simulate price movements over time.

        Args:
            scenario: Price movement scenario (uses configured one if None)
            emit_to_websocket: Whether to emit updates via WebSocket
        """
        scenario = scenario or self._price_movement
        if scenario is None:
            raise RuntimeError("No price movement scenario configured")

        if self._market is None:
            raise RuntimeError("Must call setup_market() before simulate_price_movement()")

        prev_time = 0.0
        for seconds, yes_bid, yes_ask, no_bid, no_ask in scenario.price_timeline:
            # Wait for compressed time interval
            wait_seconds = (seconds - prev_time) / self._time_compression_factor
            if wait_seconds > 0:
                await asyncio.sleep(wait_seconds)
            prev_time = seconds

            # Update client order books
            self.client.set_order_book(
                self._market.yes_token_id,
                asks=[(yes_ask, 100)],  # Default size
                bids=[(yes_bid, 100)],
            )
            self.client.set_order_book(
                self._market.no_token_id,
                asks=[(no_ask, 100)],
                bids=[(no_bid, 100)],
            )

            # Emit WebSocket updates if enabled
            if emit_to_websocket:
                self.ws.emit_price_update(
                    self._market.yes_token_id,
                    bid=yes_bid,
                    ask=yes_ask,
                )
                self.ws.emit_price_update(
                    self._market.no_token_id,
                    bid=no_bid,
                    ask=no_ask,
                )

    async def trigger_rebalance_check(self) -> Optional[Dict[str, Any]]:
        """Trigger a rebalance check for any imbalanced positions.

        Returns:
            Rebalance result if executed, None if no rebalancing needed
        """
        # Get positions needing rebalancing
        trades = await self.db.get_trades_needing_rebalancing()

        if not trades:
            return None

        # For each position, check if rebalancing is profitable
        for trade in trades:
            rebalance_result = await self._attempt_rebalance(trade)
            if rebalance_result:
                self._rebalance_results.append(rebalance_result)
                if self._on_rebalance_triggered:
                    self._on_rebalance_triggered(rebalance_result)
                return rebalance_result

        return None

    # =========================================================================
    # Assertion Methods
    # =========================================================================

    def assert_execution_matches(self, scenario: ExecutionScenario) -> None:
        """Validate execution matched expectations.

        Args:
            scenario: Expected execution scenario

        Raises:
            AssertionError: If execution didn't match
        """
        if not self._run_results:
            raise AssertionError("No execution results to validate")

        result = self._run_results[-1]

        # Check success state
        assert result.success == scenario.expected_success, (
            f"Expected success={scenario.expected_success}, got {result.success}"
        )

        # Check hedge ratio (with tolerance)
        if scenario.expected_hedge_ratio > 0:
            assert abs(result.hedge_ratio - scenario.expected_hedge_ratio) < 0.05, (
                f"Expected hedge_ratio≈{scenario.expected_hedge_ratio}, "
                f"got {result.hedge_ratio}"
            )

        # Check if rebalancing needed
        needs_rebalancing = result.hedge_ratio < 0.8 and result.hedge_ratio > 0
        assert needs_rebalancing == scenario.expected_needs_rebalancing, (
            f"Expected needs_rebalancing={scenario.expected_needs_rebalancing}, "
            f"got {needs_rebalancing}"
        )

    def assert_trade_recorded(
        self,
        expected_status: str = None,
        expected_hedge_ratio: float = None,
    ) -> None:
        """Assert a trade was recorded with expected fields.

        Args:
            expected_status: Expected execution status
            expected_hedge_ratio: Expected hedge ratio
        """
        trades = self.db._trades
        if not trades:
            raise AssertionError("No trades recorded")

        trade = list(trades.values())[-1]

        if expected_status:
            assert trade.get("execution_status") == expected_status, (
                f"Expected status={expected_status}, got {trade.get('execution_status')}"
            )

        if expected_hedge_ratio is not None:
            actual = trade.get("hedge_ratio", 0)
            assert abs(actual - expected_hedge_ratio) < 0.05, (
                f"Expected hedge_ratio≈{expected_hedge_ratio}, got {actual}"
            )

    def assert_no_trades_recorded(self) -> None:
        """Assert no trades were recorded."""
        trades = self.db._trades
        if trades:
            raise AssertionError(f"Expected no trades, but {len(trades)} were recorded")

    def assert_rebalance_executed(
        self,
        expected_action: str = None,
        expected_profit_min: float = None,
    ) -> None:
        """Assert rebalancing was executed.

        Args:
            expected_action: Expected action (SELL_YES, BUY_NO, etc.)
            expected_profit_min: Minimum expected profit
        """
        if not self._rebalance_results:
            raise AssertionError("No rebalancing executed")

        result = self._rebalance_results[-1]

        if expected_action:
            assert result.get("action") == expected_action, (
                f"Expected action={expected_action}, got {result.get('action')}"
            )

        if expected_profit_min is not None:
            profit = result.get("profit", 0)
            assert profit >= expected_profit_min, (
                f"Expected profit>={expected_profit_min}, got {profit}"
            )

    def assert_no_rebalancing(self) -> None:
        """Assert no rebalancing was executed."""
        if self._rebalance_results:
            raise AssertionError(
                f"Expected no rebalancing, but {len(self._rebalance_results)} executed"
            )

    def assert_price_movement_matches(
        self,
        scenario: PriceMovementScenario,
    ) -> None:
        """Validate price movement expectations were met.

        Args:
            scenario: Expected price movement scenario
        """
        if scenario.expected_rebalance_action:
            self.assert_rebalance_executed(
                expected_action=scenario.expected_rebalance_action,
                expected_profit_min=scenario.expected_profit_per_share,
            )
        else:
            self.assert_no_rebalancing()

    # =========================================================================
    # Query Methods
    # =========================================================================

    def get_last_result(self) -> Optional[RunResult]:
        """Get the last run result."""
        return self._run_results[-1] if self._run_results else None

    def get_all_results(self) -> List[RunResult]:
        """Get all run results."""
        return self._run_results.copy()

    def get_rebalance_results(self) -> List[Dict[str, Any]]:
        """Get all rebalance results."""
        return self._rebalance_results.copy()

    def get_recorded_trades(self) -> List[Dict[str, Any]]:
        """Get all recorded trades from database."""
        return list(self.db._trades.values())

    # =========================================================================
    # Callback Registration
    # =========================================================================

    def on_opportunity_detected(self, callback: Callable[[MarketScenario], None]) -> None:
        """Register callback for opportunity detection."""
        self._on_opportunity_detected = callback

    def on_trade_executed(self, callback: Callable[[str, Dict], None]) -> None:
        """Register callback for trade execution."""
        self._on_trade_executed = callback

    def on_rebalance_triggered(self, callback: Callable[[Dict], None]) -> None:
        """Register callback for rebalancing."""
        self._on_rebalance_triggered = callback

    # =========================================================================
    # Reset Methods
    # =========================================================================

    def reset(self) -> None:
        """Reset all state between tests."""
        self._market = None
        self._execution = None
        self._price_movement = None
        self._run_results.clear()
        self._rebalance_results.clear()

        self.client.reset()
        self.ws.reset()
        self.db.reset()

    # =========================================================================
    # Helper Methods
    # =========================================================================

    def _calculate_expected_profit(
        self,
        yes_shares: float,
        no_shares: float,
        yes_price: float,
        no_price: float,
    ) -> float:
        """Calculate expected profit from a hedged position."""
        if yes_shares == 0 or no_shares == 0:
            return 0.0

        # Profit = min(yes, no) * $1 - cost
        matched_shares = min(yes_shares, no_shares)
        total_cost = yes_shares * yes_price + no_shares * no_price
        guaranteed_return = matched_shares * 1.0
        return guaranteed_return - total_cost

    def _determine_execution_status(
        self,
        success: bool,
        partial_fill: bool,
        yes_filled: float,
        no_filled: float,
    ) -> str:
        """Determine execution status string."""
        if success:
            return "full_fill"
        elif partial_fill:
            if yes_filled > 0 and no_filled > 0:
                return "partial_fill"
            else:
                return "one_leg_only"
        else:
            return "failed"

    async def _attempt_rebalance(self, trade: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Attempt to rebalance a trade position.

        Args:
            trade: Trade to rebalance

        Returns:
            Rebalance result if executed, None otherwise
        """
        # Get current prices
        yes_token_id = trade.get("yes_token_id")
        no_token_id = trade.get("no_token_id")

        yes_book = self.client._order_books.get(yes_token_id)
        no_book = self.client._order_books.get(no_token_id)

        if not yes_book or not no_book:
            return None

        yes_shares = trade.get("yes_shares", 0)
        no_shares = trade.get("no_shares", 0)
        yes_entry_price = trade.get("yes_price", 0)
        no_entry_price = trade.get("no_price", 0)

        # Determine imbalance
        excess_side = "YES" if yes_shares > no_shares else "NO"
        excess_amount = abs(yes_shares - no_shares)

        if excess_amount < 0.01:  # Already balanced
            return None

        # Check if we can sell excess at a profit
        if excess_side == "YES":
            current_bid = yes_book.best_bid
            entry_price = yes_entry_price
            if current_bid > entry_price:
                profit = (current_bid - entry_price) * excess_amount
                return {
                    "action": "SELL_YES",
                    "shares": excess_amount,
                    "price": current_bid,
                    "profit": profit,
                    "trade_id": trade.get("trade_id"),
                }
        else:
            current_bid = no_book.best_bid
            entry_price = no_entry_price
            if current_bid > entry_price:
                profit = (current_bid - entry_price) * excess_amount
                return {
                    "action": "SELL_NO",
                    "shares": excess_amount,
                    "price": current_bid,
                    "profit": profit,
                    "trade_id": trade.get("trade_id"),
                }

        return None


# =============================================================================
# Factory Functions
# =============================================================================


def create_runner(
    client: MockPolymarketClient = None,
    ws: MockPolymarketWebSocket = None,
    db: MockDatabase = None,
    config: Any = None,
) -> ScenarioRunner:
    """Create a ScenarioRunner with optional dependencies.

    Args:
        client: Mock client (creates new if None)
        ws: Mock WebSocket (creates new if None)
        db: Mock database (creates new if None)
        config: Configuration object

    Returns:
        Configured ScenarioRunner
    """
    return ScenarioRunner(
        client=client or MockPolymarketClient(),
        ws=ws or MockPolymarketWebSocket(),
        db=db or MockDatabase(),
        config=config,
    )


async def run_complete_scenario(
    scenario: CompleteScenario,
    runner: ScenarioRunner = None,
) -> RunResult:
    """Run a complete scenario from start to finish.

    Convenience function for simple test cases.

    Args:
        scenario: Complete scenario to run
        runner: ScenarioRunner to use (creates new if None)

    Returns:
        RunResult from execution
    """
    runner = runner or create_runner()

    await runner.setup_complete_scenario(scenario)
    result = await runner.run_opportunity(budget=scenario.budget)

    # Run price movement if configured
    if scenario.price_movement:
        await runner.simulate_price_movement()
        await runner.trigger_rebalance_check()

    return result
