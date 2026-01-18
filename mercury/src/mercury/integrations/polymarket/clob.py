"""Polymarket CLOB client for order execution.

This client wraps the py-clob-client library with async support,
proper error handling, and retry logic for production use.

Ported from legacy/src/client/polymarket.py with:
- Refactored execute_dual_leg_order_parallel into smaller methods
- Removed dashboard/metrics coupling
- Added proper error types and retry logic
"""

import asyncio
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import ROUND_DOWN, Decimal
from typing import Any, Optional

import structlog
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from mercury.integrations.polymarket.types import (
    DualLegOrderResult,
    OrderBookData,
    OrderBookLevel,
    OrderResult,
    OrderSide,
    OrderStatus,
    PolymarketSettings,
    PositionInfo,
    TokenSide,
)

log = structlog.get_logger()

# Retry configuration
RETRY_ATTEMPTS = 3
RETRY_WAIT_MIN = 1
RETRY_WAIT_MAX = 10

# Execution parameters
DEFAULT_TIMEOUT_SECONDS = 5.0
PRICE_BUFFER_CENTS = Decimal("0.01")  # Add 1 cent for better fills
LIQUIDITY_PERSISTENCE_ESTIMATE = Decimal("0.40")  # Assume 40% of displayed depth persists
LIQUIDITY_SAFETY_HAIRCUT = Decimal("0.50")  # Only use 50% of estimated available
MAX_DEPTH_CONSUMPTION = Decimal("0.70")  # Reject if consuming > 70% of depth

# Order signing timeout
ORDER_SIGN_TIMEOUT_SECONDS = 2.0


# =============================================================================
# Error Types
# =============================================================================


class CLOBClientError(Exception):
    """Base error from CLOB API client."""

    pass


class ConnectionError(CLOBClientError):
    """Failed to connect to CLOB API."""

    pass


class OrderRejectedError(CLOBClientError):
    """Order was rejected by the exchange."""

    pass


class OrderTimeoutError(CLOBClientError):
    """Order execution timed out."""

    pass


class InsufficientLiquidityError(CLOBClientError):
    """Not enough liquidity to execute the order."""

    pass


class InsufficientBalanceError(CLOBClientError):
    """Not enough balance to execute the order."""

    pass


class ArbitrageInvalidError(CLOBClientError):
    """Arbitrage opportunity is no longer valid (prices sum to >= $1)."""

    pass


class OrderSigningError(CLOBClientError):
    """Failed to sign order cryptographically."""

    pass


class BatchOrderError(CLOBClientError):
    """Failed to submit batch order."""

    pass


# =============================================================================
# Internal Data Types
# =============================================================================


@dataclass
class PreparedOrder:
    """An order ready for signing."""

    token_id: str
    label: str  # "YES" or "NO"
    order_args: Any  # py-clob-client OrderArgs
    shares: Decimal
    price: Decimal
    original_price: Decimal
    start_time_ms: int


@dataclass
class SignedOrderPair:
    """A pair of signed orders ready for batch submission."""

    yes_order: Any  # Signed order object
    no_order: Any  # Signed order object
    yes_prep: PreparedOrder
    no_prep: PreparedOrder
    sign_duration_ms: int


@dataclass
class DualLegExecutionResult:
    """Internal result from dual-leg execution before converting to DualLegOrderResult."""

    yes_result: dict
    no_result: dict
    yes_filled: bool
    no_filled: bool
    yes_size_matched: Decimal
    no_size_matched: Decimal
    execution_time_ms: float


# =============================================================================
# CLOB Client
# =============================================================================


class CLOBClient:
    """Async client for Polymarket CLOB (Central Limit Order Book).

    The CLOB API provides:
    - Order book data (bids/asks)
    - Order placement (limit, market)
    - Order management (cancel, status)
    - Position and balance queries

    This client wraps the synchronous py-clob-client library with
    asyncio support using a thread pool executor.
    """

    def __init__(
        self,
        settings: PolymarketSettings,
        executor: Optional[ThreadPoolExecutor] = None,
    ):
        """Initialize the CLOB client.

        Args:
            settings: Polymarket connection settings including credentials.
            executor: Optional thread pool for async execution.
        """
        self._settings = settings
        self._executor = executor or ThreadPoolExecutor(max_workers=4)
        self._client = None  # py-clob-client ClobClient instance
        self._log = log.bind(component="clob_client")
        self._connected = False

    async def connect(self) -> None:
        """Initialize the underlying CLOB client.

        Must be called before using any other methods.
        """
        if self._connected:
            return

        # Import here to avoid import errors if py-clob-client not installed
        try:
            from py_clob_client.client import ClobClient
            from py_clob_client.clob_types import ApiCreds
        except ImportError:
            raise CLOBClientError(
                "py-clob-client not installed. "
                "Install with: pip install py-clob-client"
            )

        # Create API credentials
        creds = (
            ApiCreds(
                api_key=self._settings.api_key,
                api_secret=self._settings.api_secret,
                api_passphrase=self._settings.api_passphrase,
            )
            if self._settings.api_key
            else None
        )

        # Create the client in a thread (it may do sync initialization)
        def create_client():
            return ClobClient(
                host=self._settings.clob_url.rstrip("/"),
                key=self._settings.private_key,
                chain_id=137,  # Polygon mainnet
                signature_type=self._settings.signature_type,
                funder=self._settings.proxy_wallet,
                creds=creds,
            )

        self._client = await asyncio.get_event_loop().run_in_executor(
            self._executor, create_client
        )
        self._connected = True
        self._log.info("clob_client_connected", url=self._settings.clob_url)

    async def close(self) -> None:
        """Close the client and cleanup resources."""
        self._client = None
        self._connected = False
        self._log.info("clob_client_closed")

    async def __aenter__(self) -> "CLOBClient":
        """Async context manager entry."""
        await self.connect()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        """Async context manager exit."""
        await self.close()

    def _ensure_connected(self):
        """Ensure client is connected."""
        if not self._connected or self._client is None:
            raise CLOBClientError("Client not connected. Call connect() first.")
        return self._client

    async def _run_sync(self, func, *args, **kwargs):
        """Run a synchronous function in the thread pool."""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            self._executor, lambda: func(*args, **kwargs)
        )

    # =========================================================================
    # L0 Methods (No Auth)
    # =========================================================================

    @retry(
        stop=stop_after_attempt(RETRY_ATTEMPTS),
        wait=wait_exponential(multiplier=1, min=RETRY_WAIT_MIN, max=RETRY_WAIT_MAX),
        retry=retry_if_exception_type((ConnectionError, TimeoutError)),
    )
    async def get_order_book(self, token_id: str) -> OrderBookData:
        """Get the current order book for a token.

        Args:
            token_id: The token's ID (string to preserve precision).

        Returns:
            OrderBookData with current bids and asks.
        """
        client = self._ensure_connected()
        raw_book = await self._run_sync(client.get_order_book, token_id)

        # Parse the response (handles both dict and object formats)
        bids = self._parse_book_levels(
            raw_book.get("bids")
            if isinstance(raw_book, dict)
            else getattr(raw_book, "bids", [])
        )
        asks = self._parse_book_levels(
            raw_book.get("asks")
            if isinstance(raw_book, dict)
            else getattr(raw_book, "asks", [])
        )

        # Sort: bids by price descending, asks by price ascending
        bids = tuple(sorted(bids, key=lambda x: x.price, reverse=True))
        asks = tuple(sorted(asks, key=lambda x: x.price))

        return OrderBookData(
            token_id=token_id,
            timestamp=datetime.now(timezone.utc),
            bids=bids,
            asks=asks,
        )

    def _parse_book_levels(self, levels) -> list[OrderBookLevel]:
        """Parse order book levels from API response."""
        if not levels:
            return []

        result = []
        for level in levels:
            if isinstance(level, dict):
                price = Decimal(str(level.get("price", 0)))
                size = Decimal(str(level.get("size", 0)))
            else:
                price = Decimal(str(getattr(level, "price", 0)))
                size = Decimal(str(getattr(level, "size", 0)))

            if size > 0:
                result.append(OrderBookLevel(price=price, size=size))

        return result

    async def get_price(self, token_id: str, side: OrderSide) -> Decimal:
        """Get the current price for a token.

        Args:
            token_id: The token's ID.
            side: BUY gets ask price, SELL gets bid price.

        Returns:
            Current price or 0 if no liquidity.
        """
        book = await self.get_order_book(token_id)
        if side == OrderSide.BUY:
            return book.best_ask or Decimal("0")
        else:
            return book.best_bid or Decimal("0")

    async def get_midpoint(self, token_id: str) -> Optional[Decimal]:
        """Get the midpoint price for a token.

        Args:
            token_id: The token's ID.

        Returns:
            Midpoint price or None if missing bid/ask.
        """
        book = await self.get_order_book(token_id)
        return book.midpoint

    async def get_spread(self, token_id: str) -> dict:
        """Get the bid-ask spread for a token.

        Args:
            token_id: The token's ID.

        Returns:
            Dict with "bid", "ask", "spread" keys.
        """
        book = await self.get_order_book(token_id)
        return {
            "bid": book.best_bid,
            "ask": book.best_ask,
            "spread": book.spread,
        }

    # =========================================================================
    # L2 Methods (Authenticated)
    # =========================================================================

    @retry(
        stop=stop_after_attempt(RETRY_ATTEMPTS),
        wait=wait_exponential(multiplier=1, min=RETRY_WAIT_MIN, max=RETRY_WAIT_MAX),
        retry=retry_if_exception_type((ConnectionError, TimeoutError)),
    )
    async def get_balance(self) -> dict:
        """Get the USDC balance and allowance.

        Returns:
            Dict with "balance" and "allowance" as Decimals.
        """
        client = self._ensure_connected()
        raw = await self._run_sync(client.get_balance_allowance)

        # Convert from 6 decimal places (USDC)
        balance = Decimal(str(raw.get("balance", 0))) / Decimal("1e6")
        allowance = Decimal(str(raw.get("allowance", 0))) / Decimal("1e6")

        return {
            "balance": balance,
            "allowance": allowance,
        }

    @retry(
        stop=stop_after_attempt(RETRY_ATTEMPTS),
        wait=wait_exponential(multiplier=1, min=RETRY_WAIT_MIN, max=RETRY_WAIT_MAX),
        retry=retry_if_exception_type((ConnectionError, TimeoutError)),
    )
    async def get_positions(self) -> list[PositionInfo]:
        """Get all current positions.

        Returns:
            List of PositionInfo objects.
        """
        client = self._ensure_connected()
        raw = await self._run_sync(client.get_positions)

        positions = []
        for pos in raw:
            if isinstance(pos, dict):
                token_id = str(pos.get("asset", ""))
                size = Decimal(str(pos.get("size", 0)))
                avg_price = Decimal(str(pos.get("avgPrice", 0)))
            else:
                token_id = str(getattr(pos, "asset", ""))
                size = Decimal(str(getattr(pos, "size", 0)))
                avg_price = Decimal(str(getattr(pos, "avgPrice", 0)))

            if size > 0:
                positions.append(
                    PositionInfo(
                        token_id=token_id,
                        market_id="",  # Not provided in basic position data
                        size=size,
                        average_price=avg_price,
                        side=TokenSide.YES,  # Will need market lookup to determine
                    )
                )

        return positions

    @retry(
        stop=stop_after_attempt(RETRY_ATTEMPTS),
        wait=wait_exponential(multiplier=1, min=RETRY_WAIT_MIN, max=RETRY_WAIT_MAX),
        retry=retry_if_exception_type((ConnectionError, TimeoutError)),
    )
    async def get_open_orders(self) -> list[dict]:
        """Get all open orders.

        Returns:
            List of order dictionaries.
        """
        client = self._ensure_connected()
        return await self._run_sync(client.get_orders)

    async def cancel_order(self, order_id: str) -> bool:
        """Cancel a single order.

        Args:
            order_id: The order's ID.

        Returns:
            True if cancelled, False otherwise.
        """
        client = self._ensure_connected()
        try:
            await self._run_sync(client.cancel, order_id)
            self._log.info("order_cancelled", order_id=order_id)
            return True
        except Exception as e:
            self._log.warning("cancel_failed", order_id=order_id, error=str(e))
            return False

    async def cancel_all_orders(self) -> int:
        """Cancel all open orders.

        Returns:
            Number of orders cancelled.
        """
        client = self._ensure_connected()
        orders = await self.get_open_orders()
        cancelled = 0

        for order in orders:
            order_id = (
                order.get("id") if isinstance(order, dict) else getattr(order, "id", None)
            )
            if order_id and await self.cancel_order(order_id):
                cancelled += 1

        self._log.info("cancelled_all_orders", count=cancelled)
        return cancelled

    # =========================================================================
    # Single Order Execution
    # =========================================================================

    async def execute_order(
        self,
        token_id: str,
        side: OrderSide,
        amount_usd: Optional[Decimal] = None,
        amount_shares: Optional[Decimal] = None,
        price: Optional[Decimal] = None,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> OrderResult:
        """Execute a single order.

        Either amount_usd or amount_shares must be provided.

        Args:
            token_id: The token's ID.
            side: BUY or SELL.
            amount_usd: USD amount to trade.
            amount_shares: Number of shares to trade.
            price: Limit price (if None, uses current best price + buffer).
            timeout_seconds: Maximum time to wait for fill.

        Returns:
            OrderResult with execution details.
        """
        start_time = time.time() * 1000  # ms

        # Get current price if not specified
        if price is None:
            book = await self.get_order_book(token_id)
            if side == OrderSide.BUY:
                price = (book.best_ask or Decimal("0.5")) + PRICE_BUFFER_CENTS
            else:
                price = (book.best_bid or Decimal("0.5")) - PRICE_BUFFER_CENTS

        # Calculate shares from USD if needed
        if amount_shares is None:
            if amount_usd is None:
                raise ValueError("Either amount_usd or amount_shares must be provided")
            amount_shares = (amount_usd / price).quantize(
                Decimal("0.01"), rounding=ROUND_DOWN
            )

        # Round to 2 decimal places (py-clob-client requirement)
        amount_shares = amount_shares.quantize(Decimal("0.01"), rounding=ROUND_DOWN)
        price = price.quantize(Decimal("0.01"))

        self._log.info(
            "executing_order",
            token_id=token_id,
            side=side.value,
            shares=str(amount_shares),
            price=str(price),
        )

        client = self._ensure_connected()

        try:
            # Create and post order
            order = await self._run_sync(
                client.create_order,
                {
                    "token_id": token_id,
                    "price": float(price),
                    "size": float(amount_shares),
                    "side": side.value,
                },
            )

            response = await self._run_sync(client.post_order, order)
            response_time = time.time() * 1000

            # Parse response
            if isinstance(response, dict):
                order_id = response.get("orderID", response.get("id", ""))
                status_str = response.get("status", "LIVE")
            else:
                order_id = getattr(response, "orderID", getattr(response, "id", ""))
                status_str = getattr(response, "status", "LIVE")

            status = (
                OrderStatus(status_str)
                if status_str in OrderStatus.__members__
                else OrderStatus.LIVE
            )

            # Check for immediate fill
            filled_size = Decimal("0")
            filled_cost = Decimal("0")

            if status in (OrderStatus.MATCHED, OrderStatus.FILLED):
                filled_size = amount_shares
                filled_cost = amount_shares * price

            result = OrderResult(
                order_id=order_id,
                token_id=token_id,
                side=side,
                status=status,
                requested_price=price,
                requested_size=amount_shares,
                filled_size=filled_size,
                filled_cost=filled_cost,
                submit_time_ms=start_time,
                response_time_ms=response_time,
            )

            self._log.info(
                "order_executed",
                order_id=order_id,
                status=status.value,
                filled_size=str(filled_size),
                latency_ms=result.latency_ms,
            )

            return result

        except Exception as e:
            self._log.error(
                "order_failed",
                token_id=token_id,
                side=side.value,
                error=str(e),
            )
            raise OrderRejectedError(f"Order failed: {e}") from e

    # =========================================================================
    # Dual-Leg Order Execution (Refactored)
    # =========================================================================

    async def execute_dual_leg_order(
        self,
        yes_token_id: str,
        no_token_id: str,
        amount_usd: Decimal,
        yes_price: Optional[Decimal] = None,
        no_price: Optional[Decimal] = None,
        price_buffer_cents: Decimal = PRICE_BUFFER_CENTS,
        check_liquidity: bool = True,
        handle_partial_fills: bool = True,
        max_slippage_cents: Decimal = Decimal("2.0"),
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> DualLegOrderResult:
        """Execute a dual-leg arbitrage order (YES + NO) with parallel execution.

        This method coordinates the entire dual-leg execution flow:
        1. Validate arbitrage opportunity
        2. Fetch order books and check liquidity
        3. Prepare and sign both orders in parallel
        4. Submit orders via batch API
        5. Handle partial fills if needed

        Args:
            yes_token_id: YES token ID.
            no_token_id: NO token ID.
            amount_usd: Total USD amount to trade (split between legs).
            yes_price: Limit price for YES (or best ask + buffer).
            no_price: Limit price for NO (or best ask + buffer).
            price_buffer_cents: Additional price buffer for better fills.
            check_liquidity: Whether to validate liquidity before trading.
            handle_partial_fills: Whether to attempt rebalancing on partial fills.
            max_slippage_cents: Maximum slippage for partial fill rebalancing.
            timeout_seconds: Maximum time to wait for execution.

        Returns:
            DualLegOrderResult with both leg results.
        """
        start_time = time.time() * 1000
        market_id = f"{yes_token_id[:8]}..."

        self._log.info(
            "executing_dual_leg",
            yes_token_id=yes_token_id[:20],
            no_token_id=no_token_id[:20],
            amount_usd=str(amount_usd),
        )

        # Step 1: Fetch order books
        yes_book, no_book = await asyncio.gather(
            self.get_order_book(yes_token_id),
            self.get_order_book(no_token_id),
        )

        pre_yes_depth = yes_book.depth_at_levels(3)
        pre_no_depth = no_book.depth_at_levels(3)

        # Step 2: Calculate prices with buffer
        if yes_price is None:
            yes_price = (yes_book.best_ask or Decimal("0.5")) + price_buffer_cents
        else:
            yes_price = yes_price + price_buffer_cents
        yes_price = min(yes_price, Decimal("0.99"))

        if no_price is None:
            no_price = (no_book.best_ask or Decimal("0.5")) + price_buffer_cents
        else:
            no_price = no_price + price_buffer_cents
        no_price = min(no_price, Decimal("0.99"))

        # Step 3: Validate arbitrage opportunity
        self._validate_arbitrage(yes_price, no_price)

        # Step 4: Validate liquidity
        if check_liquidity:
            self._validate_liquidity(
                yes_book, no_book, amount_usd, yes_price, no_price
            )

        # Step 5: Calculate share amounts
        half_usd = amount_usd / 2
        yes_shares = (half_usd / yes_price).quantize(Decimal("0.01"), rounding=ROUND_DOWN)
        no_shares = (half_usd / no_price).quantize(Decimal("0.01"), rounding=ROUND_DOWN)

        # Step 6: Prepare orders
        yes_prep = self._prepare_order(yes_token_id, "YES", yes_shares, yes_price)
        no_prep = self._prepare_order(no_token_id, "NO", no_shares, no_price)

        # Step 7: Sign orders in parallel
        signed_pair = await self._sign_orders_parallel(yes_prep, no_prep)

        # Step 8: Submit batch order
        execution_result = await self._submit_batch_order(
            signed_pair, timeout_seconds
        )

        # Step 9: Handle results
        execution_time = time.time() * 1000 - start_time

        if execution_result.yes_filled and execution_result.no_filled:
            # Both filled - success!
            return self._create_success_result(
                execution_result, market_id, pre_yes_depth, pre_no_depth, execution_time
            )

        # Step 10: Handle partial fills
        if handle_partial_fills and (
            execution_result.yes_filled or execution_result.no_filled
        ):
            rebalance_result = await self._handle_partial_fill(
                execution_result,
                yes_token_id,
                no_token_id,
                yes_price,
                no_price,
                max_slippage_cents,
            )

            if rebalance_result.get("action") == "hedge_completed":
                # Successfully rebalanced
                return self._create_rebalanced_result(
                    execution_result,
                    rebalance_result,
                    market_id,
                    pre_yes_depth,
                    pre_no_depth,
                    execution_time,
                )

        # Cancel any remaining live orders
        await self._cancel_live_orders(execution_result)

        # Return partial fill result
        return self._create_partial_result(
            execution_result, market_id, pre_yes_depth, pre_no_depth, execution_time
        )

    def _validate_arbitrage(
        self, yes_price: Decimal, no_price: Decimal
    ) -> None:
        """Validate that arbitrage is still profitable.

        Raises ArbitrageInvalidError if prices sum to >= $1.00.
        """
        total_cost = yes_price + no_price
        if total_cost >= Decimal("1.0"):
            raise ArbitrageInvalidError(
                f"Arbitrage invalid: prices sum to ${total_cost:.2f} >= $1.00"
            )

    def _validate_liquidity(
        self,
        yes_book: OrderBookData,
        no_book: OrderBookData,
        amount_usd: Decimal,
        yes_price: Decimal,
        no_price: Decimal,
    ) -> None:
        """Validate there's enough liquidity for the trade.

        Raises InsufficientLiquidityError if liquidity is insufficient.
        """
        half_usd = amount_usd / 2
        yes_shares = half_usd / yes_price
        no_shares = half_usd / no_price

        # Calculate available depth with persistence and safety haircuts
        yes_depth = (
            yes_book.depth_at_levels(3)
            * LIQUIDITY_PERSISTENCE_ESTIMATE
            * LIQUIDITY_SAFETY_HAIRCUT
        )
        no_depth = (
            no_book.depth_at_levels(3)
            * LIQUIDITY_PERSISTENCE_ESTIMATE
            * LIQUIDITY_SAFETY_HAIRCUT
        )

        # Check if order would consume too much depth
        if yes_depth > 0 and yes_shares / yes_depth > MAX_DEPTH_CONSUMPTION:
            raise InsufficientLiquidityError(
                f"YES order ({yes_shares:.2f} shares) would consume "
                f"{yes_shares / yes_depth * 100:.1f}% of available depth"
            )

        if no_depth > 0 and no_shares / no_depth > MAX_DEPTH_CONSUMPTION:
            raise InsufficientLiquidityError(
                f"NO order ({no_shares:.2f} shares) would consume "
                f"{no_shares / no_depth * 100:.1f}% of available depth"
            )

        # Check if there's any liquidity at all
        if yes_book.best_ask is None or no_book.best_ask is None:
            raise InsufficientLiquidityError("Missing liquidity on one or both sides")

    def _prepare_order(
        self,
        token_id: str,
        label: str,
        shares: Decimal,
        price: Decimal,
    ) -> PreparedOrder:
        """Prepare an order for signing.

        Args:
            token_id: Token ID.
            label: "YES" or "NO".
            shares: Number of shares.
            price: Limit price.

        Returns:
            PreparedOrder ready for signing.
        """
        from py_clob_client.clob_types import OrderArgs

        # Ensure price has 2 decimal places
        price = price.quantize(Decimal("0.01"), rounding=ROUND_DOWN)

        # Ensure shares produces clean maker_amount
        shares = self._adjust_shares_for_precision(shares, price)

        order_args = OrderArgs(
            token_id=token_id,
            price=float(price),
            size=float(shares),
            side="BUY",
        )

        return PreparedOrder(
            token_id=token_id,
            label=label,
            order_args=order_args,
            shares=shares,
            price=price,
            original_price=price,
            start_time_ms=int(time.time() * 1000),
        )

    def _adjust_shares_for_precision(
        self, shares: Decimal, price: Decimal
    ) -> Decimal:
        """Adjust shares to ensure shares * price has <= 2 decimal places.

        The py-clob-client requires maker_amount (shares * price) to have
        at most 2 decimal places.
        """
        shares = shares.quantize(Decimal("0.01"), rounding=ROUND_DOWN)

        for _ in range(200):
            actual_maker = shares * price
            actual_maker_rounded = actual_maker.quantize(
                Decimal("0.01"), rounding=ROUND_DOWN
            )
            if actual_maker == actual_maker_rounded:
                break
            shares = shares - Decimal("0.01")
            if shares <= 0:
                shares = Decimal("0.01")
                break

        return shares

    async def _sign_orders_parallel(
        self, yes_prep: PreparedOrder, no_prep: PreparedOrder
    ) -> SignedOrderPair:
        """Sign both orders in parallel using ThreadPoolExecutor.

        This overlaps the CPU-bound cryptographic signing operations.

        Args:
            yes_prep: Prepared YES order.
            no_prep: Prepared NO order.

        Returns:
            SignedOrderPair with both signed orders.

        Raises:
            OrderSigningError if signing fails.
        """
        from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError

        client = self._ensure_connected()
        sign_start_ms = int(time.time() * 1000)

        def sign_order(order_args):
            return client.create_order(order_args)

        with ThreadPoolExecutor(max_workers=2) as executor:
            yes_future = executor.submit(sign_order, yes_prep.order_args)
            no_future = executor.submit(sign_order, no_prep.order_args)

            try:
                signed_yes = yes_future.result(timeout=ORDER_SIGN_TIMEOUT_SECONDS)
                signed_no = no_future.result(timeout=ORDER_SIGN_TIMEOUT_SECONDS)
            except FuturesTimeoutError:
                raise OrderSigningError("Order signing timed out")
            except Exception as e:
                raise OrderSigningError(f"Order signing failed: {e}") from e

        sign_duration_ms = int(time.time() * 1000) - sign_start_ms
        self._log.info("orders_signed", duration_ms=sign_duration_ms)

        return SignedOrderPair(
            yes_order=signed_yes,
            no_order=signed_no,
            yes_prep=yes_prep,
            no_prep=no_prep,
            sign_duration_ms=sign_duration_ms,
        )

    async def _submit_batch_order(
        self,
        signed_pair: SignedOrderPair,
        timeout_seconds: float,
    ) -> DualLegExecutionResult:
        """Submit both orders in a single HTTP call using batch API.

        This eliminates one round-trip latency (~100-250ms savings).

        Args:
            signed_pair: Signed order pair.
            timeout_seconds: Timeout for submission.

        Returns:
            DualLegExecutionResult with execution status.

        Raises:
            BatchOrderError if submission fails.
        """
        from py_clob_client.clob_types import OrderType, PostOrdersArgs

        client = self._ensure_connected()
        post_start_ms = int(time.time() * 1000)

        try:
            batch_result = await asyncio.wait_for(
                self._run_sync(
                    client.post_orders,
                    [
                        PostOrdersArgs(order=signed_pair.yes_order, orderType=OrderType.GTC),
                        PostOrdersArgs(order=signed_pair.no_order, orderType=OrderType.GTC),
                    ],
                ),
                timeout=timeout_seconds,
            )
        except asyncio.TimeoutError:
            raise OrderTimeoutError("Batch order posting timed out")
        except Exception as e:
            raise BatchOrderError(f"Batch order failed: {e}") from e

        post_duration_ms = int(time.time() * 1000) - post_start_ms
        self._log.info("batch_submitted", duration_ms=post_duration_ms)

        # Parse batch result
        yes_result, no_result = self._parse_batch_result(batch_result)

        # Add metadata
        yes_result["_prep"] = signed_pair.yes_prep
        no_result["_prep"] = signed_pair.no_prep

        # Check fill status
        yes_status = yes_result.get("status", "").upper()
        no_status = no_result.get("status", "").upper()
        yes_filled = yes_status in ("MATCHED", "FILLED")
        no_filled = no_status in ("MATCHED", "FILLED")

        yes_size_matched = Decimal(
            str(
                yes_result.get("size_matched", 0)
                or yes_result.get("matched_size", 0)
                or 0
            )
        )
        no_size_matched = Decimal(
            str(
                no_result.get("size_matched", 0)
                or no_result.get("matched_size", 0)
                or 0
            )
        )

        # If orders are LIVE, they may still fill
        if yes_status == "LIVE" or no_status == "LIVE":
            yes_filled, no_filled, yes_size_matched, no_size_matched = (
                await self._wait_for_live_orders(
                    yes_result, no_result, yes_filled, no_filled
                )
            )

        return DualLegExecutionResult(
            yes_result=yes_result,
            no_result=no_result,
            yes_filled=yes_filled,
            no_filled=no_filled,
            yes_size_matched=yes_size_matched
            or signed_pair.yes_prep.shares if yes_filled else Decimal("0"),
            no_size_matched=no_size_matched
            or signed_pair.no_prep.shares if no_filled else Decimal("0"),
            execution_time_ms=post_duration_ms,
        )

    def _parse_batch_result(self, batch_result) -> tuple[dict, dict]:
        """Parse batch order result into individual order results."""
        if isinstance(batch_result, list):
            if len(batch_result) >= 2:
                return batch_result[0], batch_result[1]
            elif len(batch_result) == 1:
                return batch_result[0], {"status": "MISSING", "error": "Order not returned"}
            else:
                return (
                    {"status": "EMPTY", "error": "Empty batch response"},
                    {"status": "EMPTY", "error": "Empty batch response"},
                )
        elif isinstance(batch_result, dict):
            if "error" in batch_result:
                return batch_result, batch_result
            return batch_result, {"status": "MISSING", "error": "Only one order returned"}
        else:
            error = {"status": "UNKNOWN", "error": f"Unexpected type: {type(batch_result)}"}
            return error, error

    async def _wait_for_live_orders(
        self,
        yes_result: dict,
        no_result: dict,
        yes_filled: bool,
        no_filled: bool,
    ) -> tuple[bool, bool, Decimal, Decimal]:
        """Wait briefly for LIVE orders to fill.

        Args:
            yes_result: YES order result.
            no_result: NO order result.
            yes_filled: Whether YES is filled.
            no_filled: Whether NO is filled.

        Returns:
            Updated (yes_filled, no_filled, yes_size_matched, no_size_matched).
        """
        yes_status = yes_result.get("status", "").upper()
        no_status = no_result.get("status", "").upper()

        if yes_status != "LIVE" and no_status != "LIVE":
            return yes_filled, no_filled, Decimal("0"), Decimal("0")

        self._log.info(
            "waiting_for_live_orders",
            yes_status=yes_status,
            no_status=no_status,
            wait_seconds=2.0,
        )

        await asyncio.sleep(2.0)

        client = self._ensure_connected()
        yes_size_matched = Decimal("0")
        no_size_matched = Decimal("0")

        try:
            orders = await self._run_sync(client.get_orders)

            yes_order_id = yes_result.get("id") or yes_result.get("order_id")
            no_order_id = no_result.get("id") or no_result.get("order_id")

            for order in orders:
                order_id = order.get("id") if isinstance(order, dict) else getattr(order, "id", None)
                new_status = (
                    order.get("status", "").upper()
                    if isinstance(order, dict)
                    else getattr(order, "status", "").upper()
                )

                if order_id == yes_order_id and new_status in ("MATCHED", "FILLED"):
                    yes_filled = True
                    yes_result["status"] = new_status
                    self._log.info("yes_filled_after_wait", status=new_status)

                if order_id == no_order_id and new_status in ("MATCHED", "FILLED"):
                    no_filled = True
                    no_result["status"] = new_status
                    self._log.info("no_filled_after_wait", status=new_status)

        except Exception as e:
            self._log.warning("failed_to_check_order_status", error=str(e))

        return yes_filled, no_filled, yes_size_matched, no_size_matched

    async def _handle_partial_fill(
        self,
        execution_result: DualLegExecutionResult,
        yes_token_id: str,
        no_token_id: str,
        yes_price: Decimal,
        no_price: Decimal,
        max_slippage_cents: Decimal,
    ) -> dict:
        """Handle a partial fill by trying to complete the hedge.

        Args:
            execution_result: Current execution result.
            yes_token_id: YES token ID.
            no_token_id: NO token ID.
            yes_price: YES price.
            no_price: NO price.
            max_slippage_cents: Maximum slippage for rebalancing.

        Returns:
            Dict with "action" and details.
        """
        filled_leg = "YES" if execution_result.yes_filled else "NO"
        filled_token_id = yes_token_id if execution_result.yes_filled else no_token_id
        unfilled_token_id = no_token_id if execution_result.yes_filled else yes_token_id
        filled_shares = (
            execution_result.yes_size_matched
            if execution_result.yes_filled
            else execution_result.no_size_matched
        )
        filled_price = yes_price if execution_result.yes_filled else no_price
        unfilled_price = no_price if execution_result.yes_filled else yes_price

        self._log.warning(
            "partial_fill_detected",
            filled_leg=filled_leg,
            filled_shares=str(filled_shares),
        )

        return await self.rebalance_partial_fill(
            filled_token_id=filled_token_id,
            unfilled_token_id=unfilled_token_id,
            filled_shares=filled_shares,
            filled_price=filled_price,
            unfilled_price=unfilled_price,
            max_slippage_cents=max_slippage_cents,
        )

    async def _cancel_live_orders(
        self, execution_result: DualLegExecutionResult
    ) -> None:
        """Cancel any remaining LIVE orders."""
        yes_status = execution_result.yes_result.get("status", "").upper()
        no_status = execution_result.no_result.get("status", "").upper()

        if yes_status == "LIVE":
            yes_order_id = execution_result.yes_result.get("id") or execution_result.yes_result.get("order_id")
            if yes_order_id:
                await self.cancel_order(yes_order_id)

        if no_status == "LIVE":
            no_order_id = execution_result.no_result.get("id") or execution_result.no_result.get("order_id")
            if no_order_id:
                await self.cancel_order(no_order_id)

    def _create_success_result(
        self,
        execution_result: DualLegExecutionResult,
        market_id: str,
        pre_yes_depth: Decimal,
        pre_no_depth: Decimal,
        execution_time: float,
    ) -> DualLegOrderResult:
        """Create a successful DualLegOrderResult."""
        yes_prep = execution_result.yes_result.get("_prep")
        no_prep = execution_result.no_result.get("_prep")

        yes_order_result = OrderResult(
            order_id=execution_result.yes_result.get("id", ""),
            token_id=yes_prep.token_id if yes_prep else "",
            side=OrderSide.BUY,
            status=OrderStatus.FILLED,
            requested_price=yes_prep.price if yes_prep else Decimal("0"),
            requested_size=yes_prep.shares if yes_prep else Decimal("0"),
            filled_size=execution_result.yes_size_matched,
            filled_cost=execution_result.yes_size_matched * (yes_prep.price if yes_prep else Decimal("0")),
        )

        no_order_result = OrderResult(
            order_id=execution_result.no_result.get("id", ""),
            token_id=no_prep.token_id if no_prep else "",
            side=OrderSide.BUY,
            status=OrderStatus.FILLED,
            requested_price=no_prep.price if no_prep else Decimal("0"),
            requested_size=no_prep.shares if no_prep else Decimal("0"),
            filled_size=execution_result.no_size_matched,
            filled_cost=execution_result.no_size_matched * (no_prep.price if no_prep else Decimal("0")),
        )

        return DualLegOrderResult(
            yes_result=yes_order_result,
            no_result=no_order_result,
            market_id=market_id,
            timestamp=datetime.now(timezone.utc),
            pre_execution_yes_depth=pre_yes_depth,
            pre_execution_no_depth=pre_no_depth,
            execution_time_ms=execution_time,
        )

    def _create_rebalanced_result(
        self,
        execution_result: DualLegExecutionResult,
        rebalance_result: dict,
        market_id: str,
        pre_yes_depth: Decimal,
        pre_no_depth: Decimal,
        execution_time: float,
    ) -> DualLegOrderResult:
        """Create a DualLegOrderResult after successful rebalancing."""
        # This is similar to success result but with rebalanced data
        return self._create_success_result(
            execution_result, market_id, pre_yes_depth, pre_no_depth, execution_time
        )

    def _create_partial_result(
        self,
        execution_result: DualLegExecutionResult,
        market_id: str,
        pre_yes_depth: Decimal,
        pre_no_depth: Decimal,
        execution_time: float,
    ) -> DualLegOrderResult:
        """Create a partial fill DualLegOrderResult."""
        yes_prep = execution_result.yes_result.get("_prep")
        no_prep = execution_result.no_result.get("_prep")

        yes_order_result = OrderResult(
            order_id=execution_result.yes_result.get("id", ""),
            token_id=yes_prep.token_id if yes_prep else "",
            side=OrderSide.BUY,
            status=OrderStatus.FILLED if execution_result.yes_filled else OrderStatus.CANCELLED,
            requested_price=yes_prep.price if yes_prep else Decimal("0"),
            requested_size=yes_prep.shares if yes_prep else Decimal("0"),
            filled_size=execution_result.yes_size_matched if execution_result.yes_filled else Decimal("0"),
            filled_cost=(
                execution_result.yes_size_matched * (yes_prep.price if yes_prep else Decimal("0"))
                if execution_result.yes_filled
                else Decimal("0")
            ),
        )

        no_order_result = OrderResult(
            order_id=execution_result.no_result.get("id", ""),
            token_id=no_prep.token_id if no_prep else "",
            side=OrderSide.BUY,
            status=OrderStatus.FILLED if execution_result.no_filled else OrderStatus.CANCELLED,
            requested_price=no_prep.price if no_prep else Decimal("0"),
            requested_size=no_prep.shares if no_prep else Decimal("0"),
            filled_size=execution_result.no_size_matched if execution_result.no_filled else Decimal("0"),
            filled_cost=(
                execution_result.no_size_matched * (no_prep.price if no_prep else Decimal("0"))
                if execution_result.no_filled
                else Decimal("0")
            ),
        )

        return DualLegOrderResult(
            yes_result=yes_order_result,
            no_result=no_order_result,
            market_id=market_id,
            timestamp=datetime.now(timezone.utc),
            pre_execution_yes_depth=pre_yes_depth,
            pre_execution_no_depth=pre_no_depth,
            execution_time_ms=execution_time,
        )

    # =========================================================================
    # Partial Fill Rebalancing
    # =========================================================================

    async def rebalance_partial_fill(
        self,
        filled_token_id: str,
        unfilled_token_id: str,
        filled_shares: Decimal,
        filled_price: Decimal,
        unfilled_price: Decimal,
        max_slippage_cents: Decimal = Decimal("2.0"),
    ) -> dict:
        """Attempt to rebalance a partial fill.

        Two-step strategy:
        1. Try to complete the hedge by buying the unfilled side
        2. If that fails, exit the filled position

        Args:
            filled_token_id: Token ID of the filled leg.
            unfilled_token_id: Token ID of the unfilled leg.
            filled_shares: Number of shares filled.
            filled_price: Price at which filled.
            unfilled_price: Expected price for unfilled side.
            max_slippage_cents: Maximum additional slippage to accept.

        Returns:
            Dict with "action" (hedge_completed, exited, failed) and details.
        """
        self._log.info(
            "rebalancing_partial_fill",
            filled_token=filled_token_id[:16],
            unfilled_token=unfilled_token_id[:16],
            filled_shares=str(filled_shares),
        )

        filled_cost = filled_shares * filled_price

        # Step 1: Try to complete the hedge
        try:
            # Get current order book for unfilled side
            unfilled_book = await self.get_order_book(unfilled_token_id)

            if unfilled_book.best_ask is not None:
                best_ask = unfilled_book.best_ask
                ask_size = unfilled_book.best_ask_size

                # Calculate hedge price with slippage
                hedge_price = min(
                    best_ask + (max_slippage_cents / Decimal("100")),
                    Decimal("0.99"),
                )

                # Check if hedge is still profitable
                total_cost_if_hedged = filled_price + hedge_price
                potential_profit = Decimal("1.0") - total_cost_if_hedged

                self._log.info(
                    "checking_hedge_profitability",
                    best_ask=str(best_ask),
                    hedge_price=str(hedge_price),
                    total_cost=str(total_cost_if_hedged),
                    potential_profit=str(potential_profit),
                )

                # Allow 2 cent loss and need 50% liquidity
                if potential_profit >= Decimal("-0.02") and ask_size >= filled_shares * Decimal("0.5"):
                    hedge_result = await self.execute_order(
                        unfilled_token_id,
                        OrderSide.BUY,
                        amount_shares=filled_shares,
                        price=hedge_price,
                    )

                    if hedge_result.status in (OrderStatus.MATCHED, OrderStatus.FILLED):
                        hedge_cost = filled_shares * hedge_price
                        total_cost = filled_cost + hedge_cost
                        expected_profit = filled_shares - total_cost

                        self._log.info(
                            "hedge_completed",
                            order_id=hedge_result.order_id,
                            hedge_cost=str(hedge_cost),
                            total_cost=str(total_cost),
                            expected_profit=str(expected_profit),
                        )

                        return {
                            "action": "hedge_completed",
                            "order": hedge_result,
                            "filled_shares": filled_shares,
                            "hedge_cost": hedge_cost,
                            "total_cost": total_cost,
                            "expected_profit": expected_profit,
                        }

        except Exception as e:
            self._log.warning("hedge_attempt_failed", error=str(e))

        # Step 2: Exit the filled position
        try:
            exit_price = max(
                filled_price - (max_slippage_cents / Decimal("100")),
                Decimal("0.01"),
            )
            exit_result = await self.execute_order(
                filled_token_id,
                OrderSide.SELL,
                amount_shares=filled_shares,
                price=exit_price,
            )

            if exit_result.status in (OrderStatus.MATCHED, OrderStatus.FILLED):
                exit_proceeds = filled_shares * exit_price
                pnl = exit_proceeds - filled_cost

                self._log.info(
                    "position_exited",
                    order_id=exit_result.order_id,
                    exit_proceeds=str(exit_proceeds),
                    pnl=str(pnl),
                )

                return {
                    "action": "exited",
                    "order": exit_result,
                    "exit_proceeds": exit_proceeds,
                    "pnl": pnl,
                }

        except Exception as e:
            self._log.error("exit_failed", error=str(e))

        return {
            "action": "failed",
            "error": "Could not hedge or exit position",
        }
