"""Polymarket CLOB client for order execution.

This client wraps the py-clob-client library with async support,
proper error handling, and retry logic for production use.
"""

import asyncio
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from decimal import ROUND_DOWN, Decimal
from typing import Optional

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


class CLOBClientError(Exception):
    """Error from CLOB API client."""

    pass


class OrderRejectedError(CLOBClientError):
    """Order was rejected by the exchange."""

    pass


class InsufficientLiquidityError(CLOBClientError):
    """Not enough liquidity to execute the order."""

    pass


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
        creds = ApiCreds(
            api_key=self._settings.api_key,
            api_secret=self._settings.api_secret,
            api_passphrase=self._settings.api_passphrase,
        ) if self._settings.api_key else None

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
            self._executor,
            lambda: func(*args, **kwargs)
        )

    # ========== L0 Methods (No Auth) ==========

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
            raw_book.get("bids") if isinstance(raw_book, dict) else getattr(raw_book, "bids", [])
        )
        asks = self._parse_book_levels(
            raw_book.get("asks") if isinstance(raw_book, dict) else getattr(raw_book, "asks", [])
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

    # ========== L2 Methods (Authenticated) ==========

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
                positions.append(PositionInfo(
                    token_id=token_id,
                    market_id="",  # Not provided in basic position data
                    size=size,
                    average_price=avg_price,
                    side=TokenSide.YES,  # Will need market lookup to determine
                ))

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
            order_id = order.get("id") if isinstance(order, dict) else getattr(order, "id", None)
            if order_id and await self.cancel_order(order_id):
                cancelled += 1

        self._log.info("cancelled_all_orders", count=cancelled)
        return cancelled

    # ========== Order Execution ==========

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
            amount_shares = (amount_usd / price).quantize(Decimal("0.01"), rounding=ROUND_DOWN)

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
                }
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

            status = OrderStatus(status_str) if status_str in OrderStatus.__members__ else OrderStatus.LIVE

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

    async def execute_dual_leg_order(
        self,
        yes_token_id: str,
        no_token_id: str,
        amount_usd: Decimal,
        yes_price: Optional[Decimal] = None,
        no_price: Optional[Decimal] = None,
        check_liquidity: bool = True,
    ) -> DualLegOrderResult:
        """Execute a dual-leg arbitrage order (YES + NO).

        Both orders are placed in parallel for speed. This creates
        a hedged position that guarantees $1.00 at resolution.

        Args:
            yes_token_id: YES token ID.
            no_token_id: NO token ID.
            amount_usd: USD amount to trade (split between legs).
            yes_price: Limit price for YES (or best ask + buffer).
            no_price: Limit price for NO (or best ask + buffer).
            check_liquidity: Whether to validate liquidity before trading.

        Returns:
            DualLegOrderResult with both leg results.
        """
        start_time = time.time() * 1000
        market_id = f"{yes_token_id[:8]}..."

        self._log.info(
            "executing_dual_leg",
            yes_token_id=yes_token_id,
            no_token_id=no_token_id,
            amount_usd=str(amount_usd),
        )

        # Fetch order books
        yes_book, no_book = await asyncio.gather(
            self.get_order_book(yes_token_id),
            self.get_order_book(no_token_id),
        )

        pre_yes_depth = yes_book.depth_at_levels(3)
        pre_no_depth = no_book.depth_at_levels(3)

        # Calculate prices
        if yes_price is None:
            yes_price = (yes_book.best_ask or Decimal("0.5")) + PRICE_BUFFER_CENTS
        if no_price is None:
            no_price = (no_book.best_ask or Decimal("0.5")) + PRICE_BUFFER_CENTS

        # Liquidity check
        if check_liquidity:
            await self._validate_liquidity(
                yes_book, no_book, amount_usd, yes_price, no_price
            )

        # Calculate share amounts (split USD evenly)
        half_usd = amount_usd / 2
        yes_shares = (half_usd / yes_price).quantize(Decimal("0.01"), rounding=ROUND_DOWN)
        no_shares = (half_usd / no_price).quantize(Decimal("0.01"), rounding=ROUND_DOWN)

        # Execute both legs in parallel
        yes_result, no_result = await asyncio.gather(
            self.execute_order(
                yes_token_id, OrderSide.BUY,
                amount_shares=yes_shares, price=yes_price
            ),
            self.execute_order(
                no_token_id, OrderSide.BUY,
                amount_shares=no_shares, price=no_price
            ),
        )

        execution_time = time.time() * 1000 - start_time

        result = DualLegOrderResult(
            yes_result=yes_result,
            no_result=no_result,
            market_id=market_id,
            timestamp=datetime.now(timezone.utc),
            pre_execution_yes_depth=pre_yes_depth,
            pre_execution_no_depth=pre_no_depth,
            execution_time_ms=execution_time,
        )

        self._log.info(
            "dual_leg_complete",
            both_filled=result.both_filled,
            has_partial=result.has_partial_fill,
            total_cost=str(result.total_cost),
            guaranteed_pnl=str(result.guaranteed_pnl),
            execution_ms=execution_time,
        )

        return result

    async def _validate_liquidity(
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

        # Calculate available depth
        yes_depth = yes_book.depth_at_levels(3) * LIQUIDITY_PERSISTENCE_ESTIMATE * LIQUIDITY_SAFETY_HAIRCUT
        no_depth = no_book.depth_at_levels(3) * LIQUIDITY_PERSISTENCE_ESTIMATE * LIQUIDITY_SAFETY_HAIRCUT

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

        # Step 1: Try to complete the hedge
        try:
            hedge_price = unfilled_price + (max_slippage_cents / 100)
            hedge_result = await self.execute_order(
                unfilled_token_id,
                OrderSide.BUY,
                amount_shares=filled_shares,
                price=hedge_price,
            )

            if hedge_result.status in (OrderStatus.MATCHED, OrderStatus.FILLED):
                self._log.info("hedge_completed", order_id=hedge_result.order_id)
                return {
                    "action": "hedge_completed",
                    "order": hedge_result,
                }

        except Exception as e:
            self._log.warning("hedge_attempt_failed", error=str(e))

        # Step 2: Exit the filled position
        try:
            exit_price = filled_price - (max_slippage_cents / 100)
            exit_result = await self.execute_order(
                filled_token_id,
                OrderSide.SELL,
                amount_shares=filled_shares,
                price=exit_price,
            )

            if exit_result.status in (OrderStatus.MATCHED, OrderStatus.FILLED):
                self._log.info("position_exited", order_id=exit_result.order_id)
                return {
                    "action": "exited",
                    "order": exit_result,
                }

        except Exception as e:
            self._log.error("exit_failed", error=str(e))

        return {
            "action": "failed",
            "error": "Could not hedge or exit position",
        }
