"""Mock Database for testing persistence.

Provides an in-memory database implementation that:
- Tracks all recorded trades
- Stores telemetry data
- Supports query operations
- Provides assertion helpers
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional
import uuid


@dataclass
class MockTrade:
    """Represents a trade record."""
    id: str
    created_at: datetime
    asset: str
    market_slug: str
    condition_id: str

    # Prices
    yes_price: float
    no_price: float
    yes_cost: float
    no_cost: float
    spread: float
    expected_profit: float

    # Execution
    yes_shares: float = 0.0
    no_shares: float = 0.0
    hedge_ratio: float = 0.0
    execution_status: str = "unknown"
    yes_order_status: str = ""
    no_order_status: str = ""

    # Liquidity
    yes_liquidity_at_price: float = 0.0
    no_liquidity_at_price: float = 0.0
    yes_book_depth_total: float = 0.0
    no_book_depth_total: float = 0.0

    # Resolution
    resolved_at: Optional[datetime] = None
    actual_profit: Optional[float] = None
    status: str = "pending"
    dry_run: bool = False
    market_end_time: str = ""

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary format."""
        return {
            "id": self.id,
            "created_at": self.created_at.isoformat(),
            "asset": self.asset,
            "market_slug": self.market_slug,
            "condition_id": self.condition_id,
            "yes_price": self.yes_price,
            "no_price": self.no_price,
            "yes_cost": self.yes_cost,
            "no_cost": self.no_cost,
            "spread": self.spread,
            "expected_profit": self.expected_profit,
            "yes_shares": self.yes_shares,
            "no_shares": self.no_shares,
            "hedge_ratio": self.hedge_ratio,
            "execution_status": self.execution_status,
            "yes_order_status": self.yes_order_status,
            "no_order_status": self.no_order_status,
            "yes_liquidity_at_price": self.yes_liquidity_at_price,
            "no_liquidity_at_price": self.no_liquidity_at_price,
            "yes_book_depth_total": self.yes_book_depth_total,
            "no_book_depth_total": self.no_book_depth_total,
            "resolved_at": self.resolved_at.isoformat() if self.resolved_at else None,
            "actual_profit": self.actual_profit,
            "status": self.status,
            "dry_run": self.dry_run,
            "market_end_time": self.market_end_time,
        }


@dataclass
class MockTelemetry:
    """Represents telemetry data."""
    trade_id: str
    opportunity_detected_at: Optional[datetime] = None
    opportunity_spread: float = 0.0
    opportunity_yes_price: float = 0.0
    opportunity_no_price: float = 0.0
    order_placed_at: Optional[datetime] = None
    order_filled_at: Optional[datetime] = None
    execution_latency_ms: Optional[float] = None
    fill_latency_ms: Optional[float] = None
    initial_yes_shares: float = 0.0
    initial_no_shares: float = 0.0
    initial_hedge_ratio: float = 0.0
    rebalance_started_at: Optional[datetime] = None
    rebalance_attempts: int = 0
    position_balanced_at: Optional[datetime] = None
    resolved_at: Optional[datetime] = None
    final_yes_shares: float = 0.0
    final_no_shares: float = 0.0
    final_hedge_ratio: float = 0.0
    actual_profit: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary format."""
        return {
            "trade_id": self.trade_id,
            "opportunity_detected_at": self.opportunity_detected_at.isoformat() if self.opportunity_detected_at else None,
            "opportunity_spread": self.opportunity_spread,
            "opportunity_yes_price": self.opportunity_yes_price,
            "opportunity_no_price": self.opportunity_no_price,
            "order_placed_at": self.order_placed_at.isoformat() if self.order_placed_at else None,
            "order_filled_at": self.order_filled_at.isoformat() if self.order_filled_at else None,
            "execution_latency_ms": self.execution_latency_ms,
            "fill_latency_ms": self.fill_latency_ms,
            "initial_yes_shares": self.initial_yes_shares,
            "initial_no_shares": self.initial_no_shares,
            "initial_hedge_ratio": self.initial_hedge_ratio,
            "rebalance_started_at": self.rebalance_started_at.isoformat() if self.rebalance_started_at else None,
            "rebalance_attempts": self.rebalance_attempts,
            "position_balanced_at": self.position_balanced_at.isoformat() if self.position_balanced_at else None,
            "resolved_at": self.resolved_at.isoformat() if self.resolved_at else None,
            "final_yes_shares": self.final_yes_shares,
            "final_no_shares": self.final_no_shares,
            "final_hedge_ratio": self.final_hedge_ratio,
            "actual_profit": self.actual_profit,
        }


@dataclass
class MockRebalanceTrade:
    """Represents a rebalancing trade record."""
    id: int
    trade_id: str
    attempted_at: datetime
    action: str  # SELL_YES, BUY_NO, etc.
    shares: float
    price: float
    status: str  # SUCCESS, FAILED, PARTIAL
    filled_shares: float = 0.0
    profit: float = 0.0
    error: Optional[str] = None
    order_id: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary format."""
        return {
            "id": self.id,
            "trade_id": self.trade_id,
            "attempted_at": self.attempted_at.isoformat(),
            "action": self.action,
            "shares": self.shares,
            "price": self.price,
            "status": self.status,
            "filled_shares": self.filled_shares,
            "profit": self.profit,
            "error": self.error,
            "order_id": self.order_id,
        }


class MockDatabase:
    """In-memory database for testing.

    Usage:
        db = MockDatabase()

        # Strategy records a trade
        await db.record_trade(trade_data)

        # In test, verify trade was recorded
        db.assert_trade_recorded("trade-001")
        db.assert_trade_has_fields("trade-001", hedge_ratio=1.0, status="pending")

        # Get all trades
        trades = db.get_all_trades()
    """

    def __init__(self):
        self._trades: Dict[str, MockTrade] = {}
        self._telemetry: Dict[str, MockTelemetry] = {}
        self._rebalance_trades: List[MockRebalanceTrade] = []
        self._next_rebalance_id = 1
        self._connected = True

    # =========================================================================
    # Configuration Methods
    # =========================================================================

    def reset(self) -> None:
        """Reset all data (between tests)."""
        self._trades.clear()
        self._telemetry.clear()
        self._rebalance_trades.clear()
        self._next_rebalance_id = 1

    def set_connected(self, connected: bool) -> None:
        """Set connection state."""
        self._connected = connected

    # =========================================================================
    # Trade Recording (matches real Database interface)
    # =========================================================================

    async def connect(self) -> None:
        """Simulate database connection."""
        self._connected = True

    async def close(self) -> None:
        """Simulate database close."""
        self._connected = False

    async def record_trade(
        self,
        trade_id: str = None,
        asset: str = "",
        market_slug: str = "",
        condition_id: str = "",
        yes_price: float = 0.0,
        no_price: float = 0.0,
        yes_cost: float = 0.0,
        no_cost: float = 0.0,
        spread: float = 0.0,
        expected_profit: float = 0.0,
        yes_shares: float = 0.0,
        no_shares: float = 0.0,
        hedge_ratio: float = 0.0,
        execution_status: str = "unknown",
        yes_order_status: str = "",
        no_order_status: str = "",
        dry_run: bool = False,
        market_end_time: str = "",
        **kwargs,
    ) -> str:
        """Record a trade.

        Args:
            trade_id: Unique trade ID (generated if not provided)
            ... (all trade fields)

        Returns:
            Trade ID
        """
        if trade_id is None:
            trade_id = f"trade-{uuid.uuid4().hex[:8]}"

        trade = MockTrade(
            id=trade_id,
            created_at=datetime.utcnow(),
            asset=asset,
            market_slug=market_slug,
            condition_id=condition_id,
            yes_price=yes_price,
            no_price=no_price,
            yes_cost=yes_cost,
            no_cost=no_cost,
            spread=spread,
            expected_profit=expected_profit,
            yes_shares=yes_shares,
            no_shares=no_shares,
            hedge_ratio=hedge_ratio,
            execution_status=execution_status,
            yes_order_status=yes_order_status,
            no_order_status=no_order_status,
            dry_run=dry_run,
            market_end_time=market_end_time,
            yes_liquidity_at_price=kwargs.get("yes_liquidity_at_price", 0.0),
            no_liquidity_at_price=kwargs.get("no_liquidity_at_price", 0.0),
            yes_book_depth_total=kwargs.get("yes_book_depth_total", 0.0),
            no_book_depth_total=kwargs.get("no_book_depth_total", 0.0),
        )

        self._trades[trade_id] = trade
        return trade_id

    async def get_trade(self, trade_id: str) -> Optional[Dict[str, Any]]:
        """Get a trade by ID."""
        trade = self._trades.get(trade_id)
        return trade.to_dict() if trade else None

    async def update_trade(self, trade_id: str, **updates) -> None:
        """Update a trade."""
        if trade_id in self._trades:
            trade = self._trades[trade_id]
            for key, value in updates.items():
                if hasattr(trade, key):
                    setattr(trade, key, value)

    async def get_pending_trades(self) -> List[Dict[str, Any]]:
        """Get all pending trades."""
        return [
            t.to_dict() for t in self._trades.values()
            if t.status == "pending"
        ]

    # =========================================================================
    # Telemetry Operations
    # =========================================================================

    async def save_trade_telemetry(self, telemetry: Dict[str, Any]) -> None:
        """Save trade telemetry."""
        trade_id = telemetry.get("trade_id")
        if not trade_id:
            return

        self._telemetry[trade_id] = MockTelemetry(
            trade_id=trade_id,
            opportunity_detected_at=_parse_datetime(telemetry.get("opportunity_detected_at")),
            opportunity_spread=telemetry.get("opportunity_spread", 0.0),
            opportunity_yes_price=telemetry.get("opportunity_yes_price", 0.0),
            opportunity_no_price=telemetry.get("opportunity_no_price", 0.0),
            order_placed_at=_parse_datetime(telemetry.get("order_placed_at")),
            order_filled_at=_parse_datetime(telemetry.get("order_filled_at")),
            execution_latency_ms=telemetry.get("execution_latency_ms"),
            fill_latency_ms=telemetry.get("fill_latency_ms"),
            initial_yes_shares=telemetry.get("initial_yes_shares", 0.0),
            initial_no_shares=telemetry.get("initial_no_shares", 0.0),
            initial_hedge_ratio=telemetry.get("initial_hedge_ratio", 0.0),
            rebalance_started_at=_parse_datetime(telemetry.get("rebalance_started_at")),
            rebalance_attempts=telemetry.get("rebalance_attempts", 0),
            position_balanced_at=_parse_datetime(telemetry.get("position_balanced_at")),
            resolved_at=_parse_datetime(telemetry.get("resolved_at")),
            final_yes_shares=telemetry.get("final_yes_shares", 0.0),
            final_no_shares=telemetry.get("final_no_shares", 0.0),
            final_hedge_ratio=telemetry.get("final_hedge_ratio", 0.0),
            actual_profit=telemetry.get("actual_profit", 0.0),
        )

    async def get_trade_telemetry(self, trade_id: str) -> Optional[Dict[str, Any]]:
        """Get telemetry for a trade."""
        telemetry = self._telemetry.get(trade_id)
        return telemetry.to_dict() if telemetry else None

    # =========================================================================
    # Rebalancing Operations
    # =========================================================================

    async def save_rebalance_trade(
        self,
        trade_id: str,
        attempted_at: str,
        action: str,
        shares: float,
        price: float,
        status: str,
        filled_shares: float = 0,
        profit: float = 0,
        error: str = None,
        order_id: str = None,
    ) -> int:
        """Save a rebalancing trade."""
        rebalance = MockRebalanceTrade(
            id=self._next_rebalance_id,
            trade_id=trade_id,
            attempted_at=_parse_datetime(attempted_at) or datetime.utcnow(),
            action=action,
            shares=shares,
            price=price,
            status=status,
            filled_shares=filled_shares,
            profit=profit,
            error=error,
            order_id=order_id,
        )
        self._rebalance_trades.append(rebalance)
        self._next_rebalance_id += 1
        return rebalance.id

    async def get_rebalance_trades(self, trade_id: str) -> List[Dict[str, Any]]:
        """Get all rebalancing trades for a position."""
        return [
            r.to_dict() for r in self._rebalance_trades
            if r.trade_id == trade_id
        ]

    # =========================================================================
    # Query Operations
    # =========================================================================

    def get_all_trades(self) -> List[Dict[str, Any]]:
        """Get all trades as dicts."""
        return [t.to_dict() for t in self._trades.values()]

    def get_trades_by_asset(self, asset: str) -> List[Dict[str, Any]]:
        """Get trades filtered by asset."""
        return [
            t.to_dict() for t in self._trades.values()
            if t.asset == asset
        ]

    def get_trades_by_status(self, status: str) -> List[Dict[str, Any]]:
        """Get trades filtered by status."""
        return [
            t.to_dict() for t in self._trades.values()
            if t.status == status
        ]

    def get_trades_by_execution_status(self, execution_status: str) -> List[Dict[str, Any]]:
        """Get trades filtered by execution status."""
        return [
            t.to_dict() for t in self._trades.values()
            if t.execution_status == execution_status
        ]

    def get_all_telemetry(self) -> List[Dict[str, Any]]:
        """Get all telemetry records."""
        return [t.to_dict() for t in self._telemetry.values()]

    def get_all_rebalance_trades(self) -> List[Dict[str, Any]]:
        """Get all rebalancing trades."""
        return [r.to_dict() for r in self._rebalance_trades]

    # =========================================================================
    # Assertion Helpers
    # =========================================================================

    def assert_trade_recorded(self, trade_id: str) -> None:
        """Assert that a trade was recorded.

        Raises:
            AssertionError: If trade not found
        """
        if trade_id not in self._trades:
            raise AssertionError(
                f"Trade {trade_id} not recorded. "
                f"Recorded trades: {list(self._trades.keys())}"
            )

    def assert_trade_has_fields(self, trade_id: str, **expected) -> None:
        """Assert that a trade has specific field values.

        Args:
            trade_id: Trade to check
            **expected: Field name/value pairs to verify

        Raises:
            AssertionError: If any field doesn't match
        """
        if trade_id not in self._trades:
            raise AssertionError(f"Trade {trade_id} not found")

        trade = self._trades[trade_id]
        trade_dict = trade.to_dict()

        for field, expected_value in expected.items():
            actual_value = trade_dict.get(field)

            # Handle float comparison
            if isinstance(expected_value, float):
                if abs(actual_value - expected_value) > 0.001:
                    raise AssertionError(
                        f"Trade {trade_id} field '{field}': "
                        f"expected {expected_value}, got {actual_value}"
                    )
            else:
                if actual_value != expected_value:
                    raise AssertionError(
                        f"Trade {trade_id} field '{field}': "
                        f"expected {expected_value}, got {actual_value}"
                    )

    def assert_no_trades_recorded(self) -> None:
        """Assert that no trades were recorded."""
        if self._trades:
            raise AssertionError(
                f"Expected no trades, but {len(self._trades)} were recorded"
            )

    def assert_trade_count(self, expected: int) -> None:
        """Assert the number of trades recorded."""
        actual = len(self._trades)
        if actual != expected:
            raise AssertionError(f"Expected {expected} trades, got {actual}")

    def assert_telemetry_recorded(self, trade_id: str) -> None:
        """Assert telemetry was recorded for a trade."""
        if trade_id not in self._telemetry:
            raise AssertionError(f"Telemetry not recorded for trade {trade_id}")

    def assert_rebalance_recorded(self, trade_id: str, action: str = None) -> None:
        """Assert a rebalancing trade was recorded.

        Args:
            trade_id: Parent trade ID
            action: Optional action to filter by
        """
        matching = [
            r for r in self._rebalance_trades
            if r.trade_id == trade_id and (action is None or r.action == action)
        ]
        if not matching:
            raise AssertionError(
                f"No rebalancing trade found for {trade_id}"
                + (f" with action {action}" if action else "")
            )


def _parse_datetime(value: Any) -> Optional[datetime]:
    """Parse a datetime from various formats."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None
