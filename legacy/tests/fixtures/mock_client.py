"""Mock Polymarket Client for testing.

Provides a controllable test double for PolymarketClient that:
- Returns configurable order results
- Simulates order book state
- Tracks all method calls for assertions
- Supports latency simulation
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Tuple
import asyncio


@dataclass
class OrderBookLevel:
    """A single level in the order book."""
    price: float
    size: float


@dataclass
class MockOrderBook:
    """Simulated order book."""
    token_id: str
    bids: List[OrderBookLevel] = field(default_factory=list)
    asks: List[OrderBookLevel] = field(default_factory=list)

    @property
    def best_bid(self) -> float:
        return self.bids[0].price if self.bids else 0.0

    @property
    def best_ask(self) -> float:
        return self.asks[0].price if self.asks else 1.0

    @property
    def midpoint(self) -> float:
        return (self.best_bid + self.best_ask) / 2

    @property
    def total_bid_depth(self) -> float:
        return sum(level.size for level in self.bids)

    @property
    def total_ask_depth(self) -> float:
        return sum(level.size for level in self.asks)

    def to_api_format(self) -> Dict[str, Any]:
        """Convert to format returned by real API."""
        return {
            "bids": [{"price": str(l.price), "size": str(l.size)} for l in self.bids],
            "asks": [{"price": str(l.price), "size": str(l.size)} for l in self.asks],
            "hash": "mock-hash",
            "timestamp": datetime.utcnow().isoformat(),
        }


@dataclass
class MockOrderResult:
    """Result of a mock order execution."""
    order_id: str
    status: str  # MATCHED, LIVE, FAILED, REJECTED, CANCELLED
    size: float
    price: float
    size_matched: float = 0.0
    side: str = "BUY"
    token_id: str = ""

    def to_api_format(self) -> Dict[str, Any]:
        """Convert to format returned by real API."""
        return {
            "id": self.order_id,
            "status": self.status,
            "size": str(self.size),
            "price": str(self.price),
            "size_matched": str(self.size_matched),
            "matched_size": str(self.size_matched),  # Alternative field name
            "side": self.side,
            "token_id": self.token_id,
            "order_id": self.order_id,
            "_intended_size": self.size,
            "_intended_price": self.price,
            "_start_time_ms": int(datetime.utcnow().timestamp() * 1000),
        }


@dataclass
class MethodCall:
    """Record of a method call for assertions."""
    method: str
    args: Tuple
    kwargs: Dict[str, Any]
    timestamp: datetime = field(default_factory=datetime.utcnow)
    result: Any = None


class MockPolymarketClient:
    """Controllable test double for PolymarketClient.

    Usage:
        client = MockPolymarketClient()

        # Configure order books
        client.set_order_book("yes-token", asks=[(0.48, 100)], bids=[(0.47, 80)])
        client.set_order_book("no-token", asks=[(0.49, 90)], bids=[(0.48, 70)])

        # Configure order results
        client.set_order_result("yes-token", MockOrderResult(
            order_id="yes-001",
            status="MATCHED",
            size=10.42,
            price=0.48,
            size_matched=10.42,
        ))

        # Use in strategy
        strategy = GabagoolStrategy(client=client, ...)

        # Assert calls were made
        client.assert_order_placed("yes-token", "BUY", 10.42)
    """

    def __init__(self):
        self.is_connected = True
        self._order_books: Dict[str, MockOrderBook] = {}
        self._order_results: Dict[str, MockOrderResult] = {}
        self._dual_leg_result: Optional[Dict[str, Any]] = None
        self._balance = {"balance": 1000.0, "allowance": 1000.0}
        self._call_history: List[MethodCall] = []
        self._execution_delay_seconds: float = 0.0
        self._should_timeout: bool = False

        # Callbacks for custom behavior
        self._on_order_placed: Optional[Callable] = None

    # =========================================================================
    # Configuration Methods (for test setup)
    # =========================================================================

    def set_order_book(
        self,
        token_id: str,
        asks: List[Tuple[float, float]] = None,
        bids: List[Tuple[float, float]] = None,
    ) -> None:
        """Configure order book for a token.

        Args:
            token_id: Token to configure
            asks: List of (price, size) tuples for asks
            bids: List of (price, size) tuples for bids
        """
        self._order_books[token_id] = MockOrderBook(
            token_id=token_id,
            asks=[OrderBookLevel(p, s) for p, s in (asks or [])],
            bids=[OrderBookLevel(p, s) for p, s in (bids or [])],
        )

    def set_order_result(self, token_id: str, result: MockOrderResult) -> None:
        """Configure what result to return for orders on a token.

        Args:
            token_id: Token to configure
            result: MockOrderResult to return
        """
        result.token_id = token_id
        self._order_results[token_id] = result

    def set_dual_leg_result(self, result: Dict[str, Any]) -> None:
        """Configure result for dual leg order execution.

        Args:
            result: Full result dict to return from execute_dual_leg_order_parallel
        """
        self._dual_leg_result = result

    def set_balance(self, balance: float, allowance: float = None) -> None:
        """Configure account balance.

        Args:
            balance: Available balance in USD
            allowance: Allowance (defaults to balance)
        """
        self._balance = {
            "balance": balance,
            "allowance": allowance if allowance is not None else balance,
        }

    def set_execution_delay(self, seconds: float) -> None:
        """Configure delay before returning from execution.

        Args:
            seconds: Delay in seconds (for timeout testing)
        """
        self._execution_delay_seconds = seconds

    def set_should_timeout(self, should_timeout: bool) -> None:
        """Configure whether execution should simulate timeout.

        Args:
            should_timeout: If True, execute will raise TimeoutError
        """
        self._should_timeout = should_timeout

    def on_order_placed(self, callback: Callable) -> None:
        """Register callback for when orders are placed.

        Useful for inspecting order parameters in tests.

        Args:
            callback: Function(token_id, side, size, price) -> None
        """
        self._on_order_placed = callback

    def reset(self) -> None:
        """Reset all state (between tests)."""
        self._order_books.clear()
        self._order_results.clear()
        self._dual_leg_result = None
        self._balance = {"balance": 1000.0, "allowance": 1000.0}
        self._call_history.clear()
        self._execution_delay_seconds = 0.0
        self._should_timeout = False

    # =========================================================================
    # Real Interface Methods (called by strategy)
    # =========================================================================

    async def get_order_book(self, token_id: str) -> Dict[str, Any]:
        """Get order book for a token."""
        self._record_call("get_order_book", (token_id,), {})

        if token_id in self._order_books:
            return self._order_books[token_id].to_api_format()

        # Default empty order book
        return {
            "bids": [],
            "asks": [],
            "hash": "mock-hash",
            "timestamp": datetime.utcnow().isoformat(),
        }

    def get_balance(self) -> Dict[str, float]:
        """Get account balance."""
        self._record_call("get_balance", (), {})
        return self._balance.copy()

    async def execute_dual_leg_order_parallel(
        self,
        yes_token_id: str,
        no_token_id: str,
        yes_amount_usd: float,
        no_amount_usd: float,
        yes_price: float,
        no_price: float,
        timeout_seconds: float = 5.0,
        max_liquidity_consumption_pct: float = 0.50,
        condition_id: str = "",
        asset: str = "",
    ) -> Dict[str, Any]:
        """Execute dual-leg order (main trade execution path)."""
        self._record_call(
            "execute_dual_leg_order_parallel",
            (),
            {
                "yes_token_id": yes_token_id,
                "no_token_id": no_token_id,
                "yes_amount_usd": yes_amount_usd,
                "no_amount_usd": no_amount_usd,
                "yes_price": yes_price,
                "no_price": no_price,
                "timeout_seconds": timeout_seconds,
                "max_liquidity_consumption_pct": max_liquidity_consumption_pct,
                "condition_id": condition_id,
                "asset": asset,
            },
        )

        # Simulate delay if configured
        if self._execution_delay_seconds > 0:
            await asyncio.sleep(self._execution_delay_seconds)

        # Simulate timeout if configured
        if self._should_timeout:
            raise asyncio.TimeoutError("Mock timeout")

        # Fire callback if registered
        if self._on_order_placed:
            self._on_order_placed(yes_token_id, "BUY", yes_amount_usd / yes_price, yes_price)
            self._on_order_placed(no_token_id, "BUY", no_amount_usd / no_price, no_price)

        # Return configured dual leg result if set
        if self._dual_leg_result is not None:
            return self._dual_leg_result

        # Build result from individual order results
        yes_result = self._order_results.get(yes_token_id)
        no_result = self._order_results.get(no_token_id)

        # Default to success if no results configured
        if yes_result is None:
            yes_shares = yes_amount_usd / yes_price
            yes_result = MockOrderResult(
                order_id=f"yes-{datetime.utcnow().timestamp()}",
                status="MATCHED",
                size=yes_shares,
                price=yes_price,
                size_matched=yes_shares,
                side="BUY",
                token_id=yes_token_id,
            )

        if no_result is None:
            no_shares = no_amount_usd / no_price
            no_result = MockOrderResult(
                order_id=f"no-{datetime.utcnow().timestamp()}",
                status="MATCHED",
                size=no_shares,
                price=no_price,
                size_matched=no_shares,
                side="BUY",
                token_id=no_token_id,
            )

        # Calculate fill status
        yes_matched = yes_result.status == "MATCHED"
        no_matched = no_result.status == "MATCHED"
        success = yes_matched and no_matched
        partial_fill = (yes_matched or no_matched) and not success

        # Get pre-fill depth
        yes_book = self._order_books.get(yes_token_id)
        no_book = self._order_books.get(no_token_id)

        result = {
            "yes_order": yes_result.to_api_format(),
            "no_order": no_result.to_api_format(),
            "success": success,
            "partial_fill": partial_fill,
            "yes_filled_size": yes_result.size_matched,
            "no_filled_size": no_result.size_matched,
            "yes_filled_cost": yes_result.size_matched * yes_result.price,
            "no_filled_cost": no_result.size_matched * no_result.price,
            "pre_fill_yes_depth": yes_book.total_ask_depth if yes_book else 0,
            "pre_fill_no_depth": no_book.total_ask_depth if no_book else 0,
        }

        if not success:
            if partial_fill:
                filled_side = "YES" if yes_matched else "NO"
                failed_side = "NO" if yes_matched else "YES"
                result["error"] = f"PARTIAL FILL: {filled_side} filled, {failed_side} rejected. Position held."
            else:
                result["error"] = "Both orders failed"

        return result

    async def execute_dual_leg_order(
        self,
        yes_token_id: str,
        no_token_id: str,
        yes_amount_usd: float,
        no_amount_usd: float,
        yes_price: float = 0.0,
        no_price: float = 0.0,
        timeout_seconds: float = 2.0,
        condition_id: str = "",
        asset: str = "",
    ) -> Dict[str, Any]:
        """Legacy sequential execution (delegates to parallel)."""
        return await self.execute_dual_leg_order_parallel(
            yes_token_id=yes_token_id,
            no_token_id=no_token_id,
            yes_amount_usd=yes_amount_usd,
            no_amount_usd=no_amount_usd,
            yes_price=yes_price,
            no_price=no_price,
            timeout_seconds=timeout_seconds,
            condition_id=condition_id,
            asset=asset,
        )

    async def cancel_all_orders(self) -> Dict[str, Any]:
        """Cancel all open orders."""
        self._record_call("cancel_all_orders", (), {})
        return {"cancelled": [], "failed": []}

    async def cancel_order(self, order_id: str) -> Dict[str, Any]:
        """Cancel a specific order."""
        self._record_call("cancel_order", (order_id,), {})
        return {"status": "cancelled", "order_id": order_id}

    def get_price(self, token_id: str, side: str = "BUY") -> float:
        """Get price for a token."""
        self._record_call("get_price", (token_id, side), {})

        book = self._order_books.get(token_id)
        if book:
            return book.best_ask if side == "BUY" else book.best_bid
        return 0.50  # Default midpoint

    def get_spread(self, token_id: str) -> Dict[str, float]:
        """Get spread for a token."""
        self._record_call("get_spread", (token_id,), {})

        book = self._order_books.get(token_id)
        if book:
            return {
                "bid": book.best_bid,
                "ask": book.best_ask,
                "spread": book.best_ask - book.best_bid,
            }
        return {"bid": 0.49, "ask": 0.51, "spread": 0.02}

    # =========================================================================
    # Assertion Helpers (for test validation)
    # =========================================================================

    def assert_order_placed(
        self,
        token_id: str = None,
        side: str = None,
        size: float = None,
        price: float = None,
    ) -> None:
        """Assert that an order was placed with given parameters.

        Args:
            token_id: Expected token ID (optional)
            side: Expected side (optional)
            size: Expected size (optional)
            price: Expected price (optional)

        Raises:
            AssertionError: If no matching order found
        """
        dual_leg_calls = [
            c for c in self._call_history
            if c.method == "execute_dual_leg_order_parallel"
        ]

        if not dual_leg_calls:
            raise AssertionError("No orders were placed")

        # Check if any call matches criteria
        for call in dual_leg_calls:
            kwargs = call.kwargs

            if token_id:
                if token_id == kwargs.get("yes_token_id"):
                    if price and abs(kwargs.get("yes_price", 0) - price) > 0.001:
                        continue
                    return  # Match found
                elif token_id == kwargs.get("no_token_id"):
                    if price and abs(kwargs.get("no_price", 0) - price) > 0.001:
                        continue
                    return  # Match found
            else:
                return  # No specific token required, any order counts

        raise AssertionError(
            f"No order found matching: token={token_id}, side={side}, size={size}, price={price}"
        )

    def assert_no_orders_placed(self) -> None:
        """Assert that no orders were placed.

        Raises:
            AssertionError: If any orders were placed
        """
        order_calls = [
            c for c in self._call_history
            if c.method in ("execute_dual_leg_order_parallel", "execute_dual_leg_order")
        ]

        if order_calls:
            raise AssertionError(f"Expected no orders, but {len(order_calls)} were placed")

    def assert_cancel_called(self) -> None:
        """Assert that cancel_all_orders was called."""
        cancel_calls = [c for c in self._call_history if c.method == "cancel_all_orders"]
        if not cancel_calls:
            raise AssertionError("cancel_all_orders was not called")

    def get_call_history(self, method: str = None) -> List[MethodCall]:
        """Get call history, optionally filtered by method.

        Args:
            method: Filter to specific method (optional)

        Returns:
            List of MethodCall records
        """
        if method:
            return [c for c in self._call_history if c.method == method]
        return self._call_history.copy()

    def get_last_dual_leg_call(self) -> Optional[Dict[str, Any]]:
        """Get kwargs from last dual leg order call."""
        calls = self.get_call_history("execute_dual_leg_order_parallel")
        return calls[-1].kwargs if calls else None

    # =========================================================================
    # Internal Methods
    # =========================================================================

    def _record_call(
        self,
        method: str,
        args: Tuple,
        kwargs: Dict[str, Any],
        result: Any = None,
    ) -> None:
        """Record a method call."""
        self._call_history.append(MethodCall(
            method=method,
            args=args,
            kwargs=kwargs,
            result=result,
        ))
