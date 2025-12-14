# E2E Test Harness Implementation Plan

**Created:** December 14, 2025
**Status:** PHASES 1-3 COMPLETE

---

## Overview

Build a comprehensive test harness that enables:
1. **Integration tests** - Full strategy execution with mocked exchange responses
2. **Scenario-based testing** - Deterministic replay of trade scenarios
3. **Sandbox mode** - Optional testing against Polymarket testnet (if available)

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│                        Test Harness                                  │
├─────────────────────────────────────────────────────────────────────┤
│                                                                     │
│  ┌─────────────────┐    ┌─────────────────┐    ┌────────────────┐  │
│  │  Scenario       │    │  Mock Exchange  │    │  Assertions    │  │
│  │  Definitions    │───▶│  Simulator      │───▶│  & Validators  │  │
│  │                 │    │                 │    │                │  │
│  │  - Market state │    │  - Order book   │    │  - Trade state │  │
│  │  - Order results│    │  - Fill logic   │    │  - DB records  │  │
│  │  - Price moves  │    │  - WebSocket    │    │  - Events      │  │
│  └─────────────────┘    └─────────────────┘    └────────────────┘  │
│                                │                                    │
│                                ▼                                    │
│  ┌─────────────────────────────────────────────────────────────┐   │
│  │                    GabagoolStrategy                          │   │
│  │              (Real code, injected dependencies)              │   │
│  └─────────────────────────────────────────────────────────────┘   │
│                                │                                    │
│                                ▼                                    │
│  ┌─────────────────────────────────────────────────────────────┐   │
│  │                    Mock Persistence                          │   │
│  │              (In-memory SQLite or dict)                      │   │
│  └─────────────────────────────────────────────────────────────┘   │
│                                                                     │
└─────────────────────────────────────────────────────────────────────┘
```

---

## Phase 1: Mock Infrastructure

### 1.1 Create Mock Exchange Client

**File:** `tests/fixtures/mock_client.py`

```python
class MockPolymarketClient:
    """Controllable test double for PolymarketClient.

    Features:
    - Configurable order results per token
    - Simulated order book with depth
    - Call tracking for assertions
    - Latency simulation (optional)
    """

    def __init__(self):
        self.is_connected = True
        self._order_books: Dict[str, OrderBook] = {}
        self._order_results: Dict[str, Dict] = {}
        self._balance = {"balance": 1000.0, "allowance": 1000.0}
        self._call_history: List[Dict] = []

    # Configuration methods (for test setup)
    def set_order_book(self, token_id: str, bids: List, asks: List) -> None
    def set_order_result(self, token_id: str, result: Dict) -> None
    def set_balance(self, balance: float, allowance: float) -> None

    # Real interface methods (called by strategy)
    async def get_order_book(self, token_id: str) -> Dict
    async def execute_dual_leg_order_parallel(...) -> Dict
    async def get_balance() -> Dict
    async def cancel_all_orders() -> Dict

    # Assertion helpers
    def assert_order_placed(self, token_id: str, side: str, size: float)
    def assert_no_orders_placed(self)
    def get_call_history(self) -> List[Dict]
```

### 1.2 Create Mock WebSocket

**File:** `tests/fixtures/mock_websocket.py`

```python
class MockPolymarketWebSocket:
    """Controllable WebSocket for testing real-time updates.

    Features:
    - Emit price updates programmatically
    - Track subscriptions
    - Simulate disconnections
    """

    def __init__(self):
        self.is_connected = True
        self._subscriptions: Set[str] = set()
        self._callbacks: Dict[str, List[Callable]] = {}

    # Configuration methods
    def emit_price_update(self, token_id: str, bid: float, ask: float) -> None
    def emit_book_snapshot(self, token_id: str, bids: List, asks: List) -> None
    def simulate_disconnect(self) -> None
    def simulate_reconnect(self) -> None

    # Real interface methods
    def subscribe(self, token_ids: List[str]) -> None
    def on_book_update(self, callback: Callable) -> None
    def on_price_change(self, callback: Callable) -> None
```

### 1.3 Create Mock Database

**File:** `tests/fixtures/mock_database.py`

```python
class MockDatabase:
    """In-memory database for testing persistence.

    Uses real SQLite in-memory mode for schema compatibility.
    """

    def __init__(self):
        self._trades: Dict[str, Dict] = {}
        self._telemetry: Dict[str, Dict] = {}

    async def record_trade(self, trade: Dict) -> str
    async def get_trade(self, trade_id: str) -> Optional[Dict]
    async def get_all_trades(self) -> List[Dict]
    async def save_trade_telemetry(self, telemetry: Dict) -> None

    # Assertion helpers
    def assert_trade_recorded(self, trade_id: str)
    def assert_trade_has_fields(self, trade_id: str, **expected)
    def get_trades_by_status(self, status: str) -> List[Dict]
```

---

## Phase 2: Scenario Framework

### 2.1 Scenario Definition Format

**File:** `tests/fixtures/scenarios.py`

```python
@dataclass
class MarketScenario:
    """Defines initial market conditions."""
    name: str
    asset: str  # BTC, ETH, etc.
    condition_id: str
    yes_token_id: str
    no_token_id: str

    # Order book state
    yes_asks: List[Tuple[float, float]]  # [(price, size), ...]
    yes_bids: List[Tuple[float, float]]
    no_asks: List[Tuple[float, float]]
    no_bids: List[Tuple[float, float]]

    # Market metadata
    end_time: datetime

    @property
    def spread_cents(self) -> float:
        yes_ask = self.yes_asks[0][0] if self.yes_asks else 1.0
        no_ask = self.no_asks[0][0] if self.no_asks else 1.0
        return (1.0 - yes_ask - no_ask) * 100


@dataclass
class ExecutionScenario:
    """Defines how orders execute."""
    name: str
    description: str

    # Order results
    yes_result: str  # MATCHED, LIVE, FAILED, REJECTED
    yes_fill_size: float  # Shares actually filled
    no_result: str
    no_fill_size: float

    # Expected outcomes
    expected_success: bool
    expected_hedge_ratio: float
    expected_execution_status: str  # full_fill, partial_fill, failed
    expected_needs_rebalancing: bool


@dataclass
class PriceMovementScenario:
    """Defines price movements after initial fill."""
    name: str

    # Price changes over time: [(seconds_after, yes_bid, yes_ask, no_bid, no_ask), ...]
    price_timeline: List[Tuple[float, float, float, float, float]]

    # Expected rebalancing behavior
    expected_rebalance_action: Optional[str]  # SELL_YES, BUY_NO, etc.
    expected_rebalance_profit: float


# Pre-defined scenarios
SCENARIOS = {
    # === Perfect Execution ===
    "perfect_fill_3c_spread": ExecutionScenario(
        name="perfect_fill_3c_spread",
        description="Both legs fill perfectly with 3 cent spread",
        yes_result="MATCHED", yes_fill_size=10.42,
        no_result="MATCHED", no_fill_size=10.42,
        expected_success=True,
        expected_hedge_ratio=1.0,
        expected_execution_status="full_fill",
        expected_needs_rebalancing=False,
    ),

    # === Partial Fills ===
    "yes_fills_no_rejected": ExecutionScenario(
        name="yes_fills_no_rejected",
        description="YES fills but NO is rejected (FOK failure)",
        yes_result="MATCHED", yes_fill_size=10.42,
        no_result="FAILED", no_fill_size=0.0,
        expected_success=False,
        expected_hedge_ratio=0.0,
        expected_execution_status="partial_fill",
        expected_needs_rebalancing=True,
    ),

    "partial_fill_60pct_hedge": ExecutionScenario(
        name="partial_fill_60pct_hedge",
        description="YES fills fully, NO fills 60%",
        yes_result="MATCHED", yes_fill_size=10.0,
        no_result="MATCHED", no_fill_size=6.0,
        expected_success=True,
        expected_hedge_ratio=0.6,
        expected_execution_status="partial_fill",
        expected_needs_rebalancing=True,
    ),

    # === Failures ===
    "both_rejected": ExecutionScenario(
        name="both_rejected",
        description="Both orders rejected",
        yes_result="FAILED", yes_fill_size=0.0,
        no_result="FAILED", no_fill_size=0.0,
        expected_success=False,
        expected_hedge_ratio=0.0,
        expected_execution_status="failed",
        expected_needs_rebalancing=False,
    ),

    # === Rebalancing ===
    "rebalance_sell_excess_yes": PriceMovementScenario(
        name="rebalance_sell_excess_yes",
        description="After partial fill, YES price rises enabling profitable sell",
        price_timeline=[
            # (seconds, yes_bid, yes_ask, no_bid, no_ask)
            (0, 0.47, 0.48, 0.48, 0.49),   # Initial
            (30, 0.52, 0.53, 0.45, 0.46),  # YES rises, NO drops
        ],
        expected_rebalance_action="SELL_YES",
        expected_rebalance_profit=0.16,  # 4 shares * $0.04
    ),
}
```

### 2.2 Scenario Runner

**File:** `tests/fixtures/scenario_runner.py`

```python
class ScenarioRunner:
    """Executes test scenarios against the strategy."""

    def __init__(
        self,
        client: MockPolymarketClient,
        ws: MockPolymarketWebSocket,
        db: MockDatabase,
    ):
        self.client = client
        self.ws = ws
        self.db = db
        self.strategy: Optional[GabagoolStrategy] = None

    async def setup_market(self, scenario: MarketScenario) -> None:
        """Configure mocks with market state."""
        self.client.set_order_book(
            scenario.yes_token_id,
            bids=scenario.yes_bids,
            asks=scenario.yes_asks,
        )
        # ... setup NO side, etc.

    async def configure_execution(self, scenario: ExecutionScenario) -> None:
        """Configure how orders will execute."""
        self.client.set_order_result(
            self.market.yes_token_id,
            {"status": scenario.yes_result, "size_matched": scenario.yes_fill_size}
        )
        # ... setup NO side, etc.

    async def run_opportunity(self) -> Dict:
        """Trigger opportunity detection and execution."""
        # Emit price update to trigger detection
        # Wait for execution
        # Return results

    async def simulate_price_movement(
        self,
        scenario: PriceMovementScenario,
    ) -> None:
        """Simulate price movements over time."""
        for seconds, yes_bid, yes_ask, no_bid, no_ask in scenario.price_timeline:
            await asyncio.sleep(seconds / 100)  # Compressed time for tests
            self.ws.emit_price_update(...)

    def assert_execution_result(self, scenario: ExecutionScenario) -> None:
        """Validate execution matched expectations."""
        trade = self.db.get_all_trades()[-1]
        assert trade["execution_status"] == scenario.expected_execution_status
        assert abs(trade["hedge_ratio"] - scenario.expected_hedge_ratio) < 0.01
```

---

## Phase 3: Integration Tests

### 3.1 Test Structure

**Directory:** `tests/integration/`

```
tests/integration/
├── __init__.py
├── conftest.py              # Integration test fixtures
├── test_arbitrage_flow.py   # Complete arbitrage scenarios
├── test_partial_fills.py    # Partial fill handling
├── test_rebalancing.py      # Position rebalancing
├── test_failure_modes.py    # Error handling
├── test_websocket_flow.py   # Real-time updates
└── test_multi_market.py     # Parallel execution
```

### 3.2 Example Test Cases

**File:** `tests/integration/test_arbitrage_flow.py`

```python
import pytest
from tests.fixtures import SCENARIOS, ScenarioRunner

class TestArbitrageExecution:
    """End-to-end arbitrage execution tests."""

    @pytest.fixture
    async def runner(self, mock_client, mock_ws, mock_db, mock_config):
        """Create scenario runner with all mocks."""
        runner = ScenarioRunner(mock_client, mock_ws, mock_db)
        await runner.initialize(mock_config)
        return runner

    @pytest.mark.asyncio
    async def test_perfect_execution_3c_spread(self, runner):
        """Both legs fill perfectly - standard arbitrage."""
        # Setup
        await runner.setup_market(MARKETS["btc_3c_spread"])
        await runner.configure_execution(SCENARIOS["perfect_fill_3c_spread"])

        # Execute
        result = await runner.run_opportunity()

        # Assert
        assert result["success"] is True
        runner.assert_execution_result(SCENARIOS["perfect_fill_3c_spread"])

        # Verify trade recorded correctly
        trade = runner.db.get_all_trades()[-1]
        assert trade["yes_shares"] == 10.42
        assert trade["no_shares"] == 10.42
        assert trade["hedge_ratio"] == 1.0
        assert trade["expected_profit"] > 0

    @pytest.mark.asyncio
    async def test_yes_fills_no_rejected(self, runner):
        """YES fills but NO rejected - should hold position."""
        await runner.setup_market(MARKETS["btc_3c_spread"])
        await runner.configure_execution(SCENARIOS["yes_fills_no_rejected"])

        result = await runner.run_opportunity()

        # Should record as partial fill
        assert result["partial_fill"] is True
        trade = runner.db.get_all_trades()[-1]
        assert trade["execution_status"] == "partial_fill"
        assert trade["yes_shares"] > 0
        assert trade["no_shares"] == 0

        # Should be tracked for rebalancing
        assert runner.strategy._position_manager.get_positions_needing_rebalancing()


class TestLiquidityValidation:
    """Pre-trade liquidity checks."""

    @pytest.mark.asyncio
    async def test_insufficient_liquidity_blocks_trade(self, runner):
        """Should not place orders when liquidity too low."""
        # Setup market with low depth
        await runner.setup_market(MARKETS["btc_low_liquidity"])

        # Attempt trade
        result = await runner.run_opportunity()

        # Should not have placed any orders
        runner.client.assert_no_orders_placed()
        assert len(runner.db.get_all_trades()) == 0


class TestEdgeCases:
    """Edge case handling."""

    @pytest.mark.asyncio
    async def test_spread_disappears_before_execution(self, runner):
        """Spread exists at detection but gone by execution."""
        await runner.setup_market(MARKETS["btc_3c_spread"])

        # Configure to return failed orders (spread gone)
        await runner.configure_execution(SCENARIOS["both_rejected"])

        result = await runner.run_opportunity()

        assert result["success"] is False

    @pytest.mark.asyncio
    async def test_execution_timeout(self, runner):
        """Order placement times out."""
        await runner.setup_market(MARKETS["btc_3c_spread"])

        # Configure client to timeout
        runner.client.set_execution_delay(10.0)  # 10 second delay

        result = await runner.run_opportunity()

        # Should have attempted to cancel
        assert runner.client.cancel_all_orders.called
```

**File:** `tests/integration/test_rebalancing.py`

```python
class TestRebalancingFlow:
    """Position rebalancing integration tests."""

    @pytest.mark.asyncio
    async def test_rebalance_sell_excess_after_partial_fill(self, runner):
        """After partial fill, sell excess when price rises."""
        # Setup partial fill
        await runner.setup_market(MARKETS["btc_3c_spread"])
        await runner.configure_execution(SCENARIOS["partial_fill_60pct_hedge"])
        await runner.run_opportunity()

        # Verify position needs rebalancing
        positions = runner.strategy._position_manager.get_positions_needing_rebalancing()
        assert len(positions) == 1
        assert positions[0].hedge_ratio == 0.6

        # Simulate favorable price movement
        await runner.simulate_price_movement(SCENARIOS["rebalance_sell_excess_yes"])

        # Wait for rebalancing to trigger
        await asyncio.sleep(0.1)

        # Verify rebalancing executed
        position = runner.strategy._position_manager.get_position(positions[0].trade_id)
        assert position.is_balanced
        assert position.hedge_ratio >= 0.8

    @pytest.mark.asyncio
    async def test_rebalance_buy_deficit(self, runner):
        """After partial fill, buy deficit when price drops."""
        # Similar setup but with buy scenario
        pass

    @pytest.mark.asyncio
    async def test_rebalance_respects_time_limit(self, runner):
        """No rebalancing in last 60 seconds before resolution."""
        # Setup market ending soon
        market = MARKETS["btc_3c_spread"]
        market.end_time = datetime.utcnow() + timedelta(seconds=30)

        await runner.setup_market(market)
        await runner.configure_execution(SCENARIOS["partial_fill_60pct_hedge"])
        await runner.run_opportunity()

        # Emit favorable price
        await runner.simulate_price_movement(SCENARIOS["rebalance_sell_excess_yes"])
        await asyncio.sleep(0.1)

        # Should NOT have rebalanced (too close to resolution)
        position = runner.strategy._position_manager.positions.values()[0]
        assert position.needs_rebalancing  # Still unbalanced
        assert len(position.rebalance_history) == 0  # No attempts
```

---

## Phase 4: Telemetry Validation

### 4.1 Timing Assertions

```python
class TestExecutionTelemetry:
    """Validate timing telemetry is recorded correctly."""

    @pytest.mark.asyncio
    async def test_telemetry_timestamps_recorded(self, runner):
        """All timing fields populated."""
        await runner.setup_market(MARKETS["btc_3c_spread"])
        await runner.configure_execution(SCENARIOS["perfect_fill_3c_spread"])
        await runner.run_opportunity()

        telemetry = runner.db.get_all_telemetry()[-1]

        assert telemetry["opportunity_detected_at"] is not None
        assert telemetry["order_placed_at"] is not None
        assert telemetry["order_filled_at"] is not None
        assert telemetry["execution_latency_ms"] > 0
        assert telemetry["fill_latency_ms"] >= 0

    @pytest.mark.asyncio
    async def test_rebalance_telemetry(self, runner):
        """Rebalancing events recorded in telemetry."""
        # Setup and execute partial fill
        # Trigger rebalancing
        # Verify telemetry fields
        pass
```

---

## Implementation Order

### Phase 1: Mock Infrastructure (Priority: HIGH) - COMPLETED
- [x] `tests/fixtures/__init__.py`
- [x] `tests/fixtures/mock_client.py` - MockPolymarketClient
- [x] `tests/fixtures/mock_websocket.py` - MockPolymarketWebSocket
- [x] `tests/fixtures/mock_database.py` - MockDatabase
- [x] Update `tests/conftest.py` with shared fixtures

### Phase 2: Scenario Framework (Priority: HIGH) - COMPLETED
- [x] `tests/fixtures/scenarios.py` - Scenario dataclasses
- [x] `tests/fixtures/scenario_runner.py` - ScenarioRunner class
- Note: Market states merged into scenarios.py

### Phase 3: Integration Tests (Priority: MEDIUM) - COMPLETED
- [x] `tests/integration/__init__.py`
- [x] `tests/integration/conftest.py`
- [x] `tests/integration/test_arbitrage_flow.py`
- [x] `tests/integration/test_partial_fills.py`
- [x] `tests/integration/test_rebalancing.py`
- [x] `tests/integration/test_failure_modes.py`

### Phase 4: Advanced Tests (Priority: LOW)
- [ ] `tests/integration/test_websocket_flow.py`
- [ ] `tests/integration/test_multi_market.py`
- [ ] `tests/integration/test_telemetry.py`

---

## Running Tests

```bash
# Run all tests
pytest

# Run only integration tests
pytest tests/integration/ -v

# Run specific scenario
pytest tests/integration/test_arbitrage_flow.py::TestArbitrageExecution::test_perfect_execution_3c_spread -v

# Run with coverage
pytest tests/integration/ --cov=src --cov-report=html

# Run in parallel (with pytest-xdist)
pytest tests/integration/ -n auto
```

---

## Success Criteria

1. **Coverage**: All execution paths tested
   - Perfect fills
   - Partial fills (YES only, NO only, asymmetric)
   - Complete failures
   - Rebalancing scenarios

2. **Determinism**: Tests are repeatable and don't flake

3. **Speed**: Full integration suite runs in < 30 seconds

4. **Isolation**: Tests don't affect each other

5. **Clarity**: Failures clearly indicate what went wrong

---

## Related Documents

- [REBALANCING_STRATEGY.md](./REBALANCING_STRATEGY.md) - Rebalancing logic
- [STRATEGY_ARCHITECTURE.md](./STRATEGY_ARCHITECTURE.md) - Strategy architecture
- [test_e2e_scenarios.py](../tests/test_e2e_scenarios.py) - Existing scenario tests
