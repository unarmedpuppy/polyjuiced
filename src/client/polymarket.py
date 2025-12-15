"""Polymarket CLOB client wrapper."""

import asyncio
import time
from decimal import Decimal
from typing import Any, Callable, Dict, List, Optional, TYPE_CHECKING

import structlog
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds, MarketOrderArgs, OrderArgs, OrderType
from tenacity import retry, stop_after_attempt, wait_exponential

from ..config import PolymarketSettings

if TYPE_CHECKING:
    from ..liquidity.collector import LiquidityCollector

log = structlog.get_logger()


class PolymarketClient:
    """Wrapper around py-clob-client with async support and error handling."""

    def __init__(self, settings: PolymarketSettings):
        """Initialize the Polymarket client.

        Args:
            settings: Polymarket configuration settings
        """
        self.settings = settings
        self._client: Optional[ClobClient] = None
        self._connected = False
        self._liquidity_collector: Optional["LiquidityCollector"] = None

    def set_liquidity_collector(self, collector: "LiquidityCollector") -> None:
        """Set the liquidity collector for fill logging.

        Args:
            collector: LiquidityCollector instance
        """
        self._liquidity_collector = collector
        log.info("Liquidity collector attached to client")

    def connect(self) -> bool:
        """Establish connection to Polymarket CLOB.

        Returns:
            True if connection successful
        """
        try:
            self._client = ClobClient(
                host=self.settings.clob_http_url,
                key=self.settings.private_key,
                chain_id=137,  # Polygon Mainnet
                signature_type=self.settings.signature_type,
                funder=self.settings.proxy_wallet or None,
            )

            # Set API credentials if available
            if self.settings.api_key:
                creds = ApiCreds(
                    api_key=self.settings.api_key,
                    api_secret=self.settings.api_secret,
                    api_passphrase=self.settings.api_passphrase,
                )
                self._client.set_api_creds(creds)

            # Test connection
            self._client.get_ok()
            self._connected = True
            log.info("Connected to Polymarket CLOB")
            return True

        except Exception as e:
            log.error("Failed to connect to Polymarket", error=str(e))
            self._connected = False
            return False

    @property
    def is_connected(self) -> bool:
        """Check if client is connected."""
        return self._connected and self._client is not None

    def _ensure_connected(self) -> None:
        """Ensure client is connected, raise if not."""
        if not self.is_connected:
            raise RuntimeError("Client not connected. Call connect() first.")

    # =========================================================================
    # L0 Methods (Public, no auth required)
    # =========================================================================

    def get_markets(self) -> List[Dict[str, Any]]:
        """Get all available markets.

        Returns:
            List of market data dictionaries
        """
        self._ensure_connected()
        return self._client.get_simplified_markets()

    def get_market(self, condition_id: str) -> Dict[str, Any]:
        """Get specific market by condition ID.

        Args:
            condition_id: The market condition ID

        Returns:
            Market data dictionary
        """
        self._ensure_connected()
        return self._client.get_market(condition_id)

    def get_order_book(self, token_id: str) -> Dict[str, Any]:
        """Get order book for a specific token.

        Args:
            token_id: The YES or NO token ID

        Returns:
            Order book with bids and asks
        """
        self._ensure_connected()
        return self._client.get_order_book(token_id)

    def get_price(self, token_id: str, side: str = "buy") -> float:
        """Get current price for a token.

        Args:
            token_id: The YES or NO token ID
            side: "buy" or "sell"

        Returns:
            Current price (0.0 to 1.0)
        """
        self._ensure_connected()
        return float(self._client.get_price(token_id, side))

    def get_midpoint(self, token_id: str) -> float:
        """Get midpoint price for a token.

        Args:
            token_id: The YES or NO token ID

        Returns:
            Midpoint price (0.0 to 1.0)
        """
        self._ensure_connected()
        return float(self._client.get_midpoint(token_id))

    def get_spread(self, token_id: str) -> Dict[str, float]:
        """Get bid-ask spread for a token.

        Args:
            token_id: The YES or NO token ID

        Returns:
            Dict with 'bid', 'ask', 'spread' keys
        """
        self._ensure_connected()
        spread_data = self._client.get_spread(token_id)
        return {
            "bid": float(spread_data.get("bid", 0)),
            "ask": float(spread_data.get("ask", 0)),
            "spread": float(spread_data.get("spread", 0)),
        }

    # =========================================================================
    # L2 Methods (Authenticated, requires API credentials)
    # =========================================================================

    def get_balance(self) -> Dict[str, Any]:
        """Get wallet balance and allowance.

        Returns:
            Dictionary with 'balance' and 'allowance' keys (values in USDC)
        """
        self._ensure_connected()
        try:
            from py_clob_client.clob_types import AssetType, BalanceAllowanceParams
            # Must pass params object with COLLATERAL asset type for USDC balance
            params = BalanceAllowanceParams(
                asset_type=AssetType.COLLATERAL,
                signature_type=self.settings.signature_type,
            )
            result = self._client.get_balance_allowance(params)
            # Convert from raw values (6 decimals for USDC) to human-readable
            balance = float(result.get("balance", 0)) / 1e6
            allowance = float(result.get("allowance", 0)) / 1e6
            return {
                "balance": balance,
                "allowance": allowance,
            }
        except Exception as e:
            log.warning("Failed to get balance", error=str(e))
            return {"balance": 0.0, "allowance": 0.0}

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
    )
    def create_market_order(
        self,
        token_id: str,
        amount_usd: float,
        side: str,
        price: float = None,
    ) -> Dict[str, Any]:
        """Execute a market order using an aggressive limit order.

        Note: Market orders have a decimal precision bug in py-clob-client.
        We use limit orders at slightly aggressive prices as a workaround.
        See: https://github.com/Polymarket/py-clob-client/issues/121

        Args:
            token_id: The YES or NO token ID
            amount_usd: Dollar amount to spend
            side: "BUY" or "SELL"
            price: Current market price (required for calculating shares)

        Returns:
            Order result dictionary from exchange
        """
        self._ensure_connected()

        # Get current price if not provided
        if price is None:
            try:
                price = self.get_price(token_id, side.lower())
            except Exception:
                price = 0.50  # Default to 50/50

        from decimal import Decimal, ROUND_DOWN

        # Polymarket requires (especially for FOK orders):
        # - maker_amount (USD): 2 decimal places max
        # - taker_amount (shares): 2 decimal places (py-clob-client limit)
        # - price: 2 decimal places

        price_d = Decimal(str(price))

        # Use aggressive limit price to ensure fill (max 2 decimals)
        if side.upper() == "BUY":
            limit_price_d = min(price_d + Decimal("0.02"), Decimal("0.99"))
        else:
            limit_price_d = max(price_d - Decimal("0.02"), Decimal("0.01"))
        limit_price_d = limit_price_d.quantize(Decimal("0.01"), rounding=ROUND_DOWN)

        # Round USD amount to 2 decimals - this IS our target maker_amount
        maker_amount_d = Decimal(str(amount_usd)).quantize(Decimal("0.01"), rounding=ROUND_DOWN)

        # Calculate shares from clean maker_amount (round to 2 decimals for py-clob-client)
        shares_d = (maker_amount_d / limit_price_d).quantize(Decimal("0.01"), rounding=ROUND_DOWN)

        # Ensure shares × price produces a clean maker_amount (≤2 decimals)
        for _ in range(200):
            actual_maker = shares_d * limit_price_d
            actual_maker_rounded = actual_maker.quantize(Decimal("0.01"), rounding=ROUND_DOWN)
            if actual_maker == actual_maker_rounded:
                break
            shares_d = shares_d - Decimal("0.01")
            if shares_d <= 0:
                shares_d = Decimal("0.01")
                break

        limit_price = float(limit_price_d)
        shares = float(shares_d)

        log.info(
            "Placing aggressive limit order (workaround for market order bug)",
            token_id=token_id,
            amount_usd=float(amount_d),
            side=side,
            price=f"{limit_price:.2f}",
            shares=f"{shares:.4f}",
        )

        order_args = OrderArgs(
            token_id=token_id,
            price=limit_price,
            size=shares,
            side=side.upper(),
        )

        # create_order returns a SignedOrder - we must POST it to execute
        signed_order = self._client.create_order(order_args)
        log.info("Order signed, posting to exchange...")
        result = self._client.post_order(signed_order)
        log.info("Order posted", result=result)
        return result

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
    )
    def create_limit_order(
        self,
        token_id: str,
        price: float,
        size: float,
        side: str,
        order_type: str = "GTC",
    ) -> Dict[str, Any]:
        """Place a limit order.

        Args:
            token_id: The YES or NO token ID
            price: Limit price (0.0 to 1.0)
            size: Number of shares
            side: "BUY" or "SELL"
            order_type: "GTC" (Good-Till-Canceled) or "FOK" (Fill-or-Kill)

        Returns:
            Order result dictionary
        """
        self._ensure_connected()
        log.info(
            "Placing limit order",
            token_id=token_id,
            price=price,
            size=size,
            side=side,
            order_type=order_type,
        )

        ot = OrderType.GTC if order_type == "GTC" else OrderType.FOK

        order_args = OrderArgs(
            token_id=token_id,
            price=price,
            size=size,
            side=side.upper(),
            order_type=ot,
        )

        # create_order returns a SignedOrder - we must POST it to execute
        signed_order = self._client.create_order(order_args)
        log.info("Order signed, posting to exchange...")
        result = self._client.post_order(signed_order)
        log.info("Order posted", result=result)
        return result

    def cancel_order(self, order_id: str) -> Dict[str, Any]:
        """Cancel an open order.

        Args:
            order_id: The order ID to cancel

        Returns:
            Cancellation result
        """
        self._ensure_connected()
        log.info("Cancelling order", order_id=order_id)
        return self._client.cancel(order_id)

    def cancel_all_orders(self) -> Dict[str, Any]:
        """Cancel all open orders.

        Returns:
            Cancellation result
        """
        self._ensure_connected()
        log.info("Cancelling all orders")
        return self._client.cancel_all()

    def get_orders(self) -> List[Dict[str, Any]]:
        """Get all open orders.

        Returns:
            List of open orders
        """
        self._ensure_connected()
        return self._client.get_orders()

    def get_trades(self) -> List[Dict[str, Any]]:
        """Get trade history.

        Returns:
            List of historical trades
        """
        self._ensure_connected()
        return self._client.get_trades()

    # =========================================================================
    # Single-Leg Execution (for directional trades)
    # =========================================================================

    async def execute_single_order(
        self,
        token_id: str,
        side: str,
        amount_usd: float = None,
        amount_shares: float = None,
        timeout_seconds: float = 0.5,
    ) -> Dict[str, Any]:
        """Execute a single order for directional trades.

        Args:
            token_id: The YES or NO token ID
            side: "BUY" or "SELL"
            amount_usd: Dollar amount (for BUY orders)
            amount_shares: Share amount (for SELL orders)
            timeout_seconds: Timeout for order placement

        Returns:
            Dict with 'order', 'success' keys
        """
        log.info(
            "Executing single order",
            token_id=token_id,
            side=side,
            amount_usd=amount_usd,
            amount_shares=amount_shares,
        )

        async def place_order():
            if side.upper() == "BUY":
                return self.create_market_order(token_id, amount_usd, "BUY")
            else:
                # For SELL, use limit order at current price to ensure fill
                # Get current price and sell at slightly below market
                try:
                    price = self.get_price(token_id, "sell")
                    # Sell at 1 tick below to ensure fill
                    sell_price = max(0.01, price - 0.01)
                    return self.create_limit_order(
                        token_id=token_id,
                        price=sell_price,
                        size=amount_shares,
                        side="SELL",
                        order_type="FOK",  # Fill-or-Kill for immediate execution
                    )
                except Exception as e:
                    log.error("Failed to get sell price", error=str(e))
                    raise

        try:
            result = await asyncio.wait_for(
                asyncio.to_thread(place_order),
                timeout=timeout_seconds,
            )

            return {
                "order": result,
                "success": True,
            }

        except asyncio.TimeoutError:
            log.error("Single order timed out")
            return {
                "order": None,
                "success": False,
                "error": "timeout",
            }

        except Exception as e:
            log.error("Single order failed", error=str(e))
            return {
                "order": None,
                "success": False,
                "error": str(e),
            }

    # =========================================================================
    # Dual-Leg Execution (for Gabagool strategy)
    # =========================================================================

    async def execute_dual_leg_order(
        self,
        yes_token_id: str,
        no_token_id: str,
        yes_amount_usd: float,
        no_amount_usd: float,
        yes_price: float = 0.0,  # EXACT limit price (0 = fetch from book - DEPRECATED)
        no_price: float = 0.0,   # EXACT limit price (0 = fetch from book - DEPRECATED)
        timeout_seconds: float = 2.0,
        condition_id: str = "",
        asset: str = "",
    ) -> Dict[str, Any]:
        """Execute YES and NO orders for arbitrage with fill-or-kill semantics.

        DEPRECATED: Use execute_dual_leg_order_parallel() instead.
        This sequential method is kept for backwards compatibility.

        CRITICAL: For arbitrage, we MUST get both legs filled or neither.
        A partial fill (one side only) creates an unhedged directional position
        which defeats the purpose of arbitrage.

        Strategy:
        1. Pre-flight check: verify liquidity on both sides
        2. Place YES order first (GTC with aggressive price)
        3. If YES fills, immediately place NO order
        4. If NO fails after YES succeeded: Cancel LIVE NO orders, return partial fill data
           (Phase 4: NO unwind attempts - positions held until resolution)
        5. If YES fails, don't place NO at all

        Args:
            yes_token_id: YES token ID
            no_token_id: NO token ID
            yes_amount_usd: Amount to spend on YES
            no_amount_usd: Amount to spend on NO
            yes_price: EXACT limit price for YES (0 = fetch from book - DEPRECATED)
            no_price: EXACT limit price for NO (0 = fetch from book - DEPRECATED)
            timeout_seconds: Timeout for order placement
            condition_id: Market condition ID (for fill logging)
            asset: Asset symbol (for fill logging)

        Returns:
            Dict with 'yes_order', 'no_order', 'success', 'partial_fill' keys
        """
        log.warning(
            "Using DEPRECATED sequential execution - switch to parallel execution",
            yes_amount=yes_amount_usd,
            no_amount=no_amount_usd,
        )

        # Pre-flight liquidity check
        # NOTE: This is a basic check. See docs/LIQUIDITY_SIZING.md for limitations
        # and roadmap to professional-grade sizing.
        #
        # Conservative assumptions until we have fill data:
        # - PERSISTENCE_ESTIMATE = 0.4 (assume 40% of displayed depth persists)
        # - SAFETY_HAIRCUT = 0.5 (only use 50% of calculated max)
        PERSISTENCE_ESTIMATE = 0.4
        SAFETY_HAIRCUT = 0.5

        try:
            yes_book = self.get_order_book(yes_token_id)
            no_book = self.get_order_book(no_token_id)

            # Check if there are asks (sellers) we can buy from
            yes_asks = yes_book.get("asks", [])
            no_asks = no_book.get("asks", [])

            if not yes_asks or not no_asks:
                log.warning(
                    "Insufficient liquidity - no asks on one or both sides",
                    yes_asks=len(yes_asks),
                    no_asks=len(no_asks),
                )
                return {
                    "yes_order": None,
                    "no_order": None,
                    "success": False,
                    "partial_fill": False,
                    "error": "Insufficient liquidity - no asks available",
                    # Phase 5: Liquidity data (empty book)
                    "pre_fill_yes_depth": 0.0,
                    "pre_fill_no_depth": 0.0,
                }

            # Estimate available liquidity at top of book
            # Apply persistence estimate - displayed depth often vanishes on touch
            yes_displayed = sum(float(ask.get("size", 0)) for ask in yes_asks[:3])
            no_displayed = sum(float(ask.get("size", 0)) for ask in no_asks[:3])
            yes_liquidity = yes_displayed * PERSISTENCE_ESTIMATE
            no_liquidity = no_displayed * PERSISTENCE_ESTIMATE

            # Calculate shares we need
            yes_price = float(yes_asks[0].get("price", 0.5))
            no_price = float(no_asks[0].get("price", 0.5))
            yes_shares_needed = yes_amount_usd / yes_price if yes_price > 0 else 0
            no_shares_needed = no_amount_usd / no_price if no_price > 0 else 0

            # Check 1: Basic liquidity threshold (with safety haircut)
            min_liquidity_ratio = SAFETY_HAIRCUT
            if yes_liquidity < yes_shares_needed * min_liquidity_ratio:
                log.warning(
                    "Insufficient YES liquidity (persistence-adjusted)",
                    displayed=f"{yes_displayed:.1f}",
                    persistent=f"{yes_liquidity:.1f}",
                    needed=f"{yes_shares_needed:.1f}",
                )
                return {
                    "yes_order": None,
                    "no_order": None,
                    "success": False,
                    "partial_fill": False,
                    "error": f"Insufficient YES liquidity: {yes_liquidity:.1f} persistent (from {yes_displayed:.1f} displayed) < {yes_shares_needed:.1f} needed",
                    # Phase 5: Liquidity data (rejected due to insufficient depth)
                    "pre_fill_yes_depth": yes_displayed,
                    "pre_fill_no_depth": no_displayed,
                }

            if no_liquidity < no_shares_needed * min_liquidity_ratio:
                log.warning(
                    "Insufficient NO liquidity (persistence-adjusted)",
                    displayed=f"{no_displayed:.1f}",
                    persistent=f"{no_liquidity:.1f}",
                    needed=f"{no_shares_needed:.1f}",
                )
                return {
                    "yes_order": None,
                    "no_order": None,
                    "success": False,
                    "partial_fill": False,
                    "error": f"Insufficient NO liquidity: {no_liquidity:.1f} persistent (from {no_displayed:.1f} displayed) < {no_shares_needed:.1f} needed",
                    # Phase 5: Liquidity data (rejected due to insufficient depth)
                    "pre_fill_yes_depth": yes_displayed,
                    "pre_fill_no_depth": no_displayed,
                }

            # Check 2: Self-induced spread collapse
            # If our order would consume all liquidity, we're not doing arbitrage
            # - we're creating the spread for someone else
            if yes_shares_needed > yes_displayed * 0.7 or no_shares_needed > no_displayed * 0.7:
                log.warning(
                    "Order would consume majority of book depth (self-induced collapse)",
                    yes_pct=f"{yes_shares_needed/yes_displayed*100:.0f}%",
                    no_pct=f"{no_shares_needed/no_displayed*100:.0f}%",
                )
                return {
                    "yes_order": None,
                    "no_order": None,
                    "success": False,
                    "partial_fill": False,
                    "error": f"Order too large relative to book (would consume {max(yes_shares_needed/yes_displayed, no_shares_needed/no_displayed)*100:.0f}% of depth)",
                    # Phase 5: Liquidity data (rejected due to consumption limit)
                    "pre_fill_yes_depth": yes_displayed,
                    "pre_fill_no_depth": no_displayed,
                }

            log.info(
                "Liquidity check passed",
                yes_displayed=f"{yes_displayed:.1f}",
                yes_persistent=f"{yes_liquidity:.1f}",
                no_displayed=f"{no_displayed:.1f}",
                no_persistent=f"{no_liquidity:.1f}",
            )

            # Store pre-fill depth for liquidity logging
            pre_fill_yes_depth = yes_displayed
            pre_fill_no_depth = no_displayed

        except Exception as e:
            log.warning("Liquidity check failed, proceeding anyway", error=str(e))
            pre_fill_yes_depth = 0.0
            pre_fill_no_depth = 0.0

        def place_fok_order(token_id: str, amount_usd: float, label: str) -> Dict[str, Any]:
            """Place a Fill-or-Kill order.

            DEPRECATED: This function is part of the deprecated sequential execution.
            Use execute_dual_leg_order_parallel() instead which uses exact pricing.

            WARNING: This function adds 3¢ slippage which destroys arbitrage profit!
            It should NOT be used for arbitrage. Only kept for backwards compatibility.

            Note: FOK orders have decimal precision bugs in py-clob-client.
            We use GTC instead which works. For arbitrage, we accept the risk
            that orders might not fill immediately but the aggressive pricing
            should ensure quick fills.
            See: https://github.com/Polymarket/py-clob-client/issues/121

            Returns:
                Dict with order result plus _intended_size, _intended_price, _start_time_ms
            """
            from decimal import Decimal, ROUND_DOWN

            start_time_ms = int(time.time() * 1000)

            try:
                price = self.get_price(token_id, "buy")
            except Exception:
                price = 0.50

            # Polymarket requires:
            # - maker_amount (USD): 2 decimal places max
            # - taker_amount (shares): 2 decimal places (py-clob-client limit)
            # - price: 2 decimal places

            price_d = Decimal(str(price))

            # DEPRECATED: This 3¢ slippage destroys arbitrage profit!
            # This is kept for backwards compatibility only.
            # Use execute_dual_leg_order_parallel() which uses exact pricing.
            limit_price_d = min(price_d + Decimal("0.03"), Decimal("0.99"))
            limit_price_d = limit_price_d.quantize(Decimal("0.01"), rounding=ROUND_DOWN)

            # Round USD amount to 2 decimals - this IS our target maker_amount
            maker_amount_d = Decimal(str(amount_usd)).quantize(Decimal("0.01"), rounding=ROUND_DOWN)

            # Calculate shares (round to 2 decimals for py-clob-client)
            shares_d = (maker_amount_d / limit_price_d).quantize(Decimal("0.01"), rounding=ROUND_DOWN)

            # Ensure shares × price produces a clean maker_amount (≤2 decimals)
            for _ in range(200):
                actual_maker = shares_d * limit_price_d
                actual_maker_rounded = actual_maker.quantize(Decimal("0.01"), rounding=ROUND_DOWN)
                if actual_maker == actual_maker_rounded:
                    break
                shares_d = shares_d - Decimal("0.01")
                if shares_d <= 0:
                    shares_d = Decimal("0.01")
                    break

            limit_price = float(limit_price_d)
            shares = float(shares_d)

            log.info(
                f"Placing {label} GTC order (FOK has precision bugs)",
                token_id=token_id[:20] + "...",
                price=f"{limit_price:.2f}",
                shares=f"{shares:.4f}",
            )

            order_args = OrderArgs(
                token_id=token_id,
                price=limit_price,
                size=shares,
                side="BUY",
            )

            signed_order = self._client.create_order(order_args)
            # Use GTC instead of FOK due to decimal precision bugs
            result = self._client.post_order(signed_order, orderType=OrderType.GTC)

            # Add metadata for fill logging
            result["_intended_size"] = shares
            result["_intended_price"] = limit_price
            result["_start_time_ms"] = start_time_ms

            return result

        try:
            # Step 1: Place YES order with FOK
            yes_result = await asyncio.wait_for(
                asyncio.to_thread(place_fok_order, yes_token_id, yes_amount_usd, "YES"),
                timeout=timeout_seconds,
            )

            # Check if YES order filled
            # IMPORTANT: LIVE means order is on the book waiting, NOT filled
            # Only MATCHED/FILLED indicate actual execution
            yes_status = yes_result.get("status", "").upper()
            yes_filled = yes_status in ("MATCHED", "FILLED")

            # Also check size_matched for partial fills
            yes_size_matched = float(yes_result.get("size_matched", 0) or yes_result.get("matched_size", 0) or 0)
            yes_intended_size = float(yes_result.get("_intended_size", 0) or yes_result.get("size", 0) or 0)

            # If status is LIVE, the order is sitting on the book - not filled yet
            if yes_status == "LIVE":
                log.warning(
                    "YES order went LIVE (on book) instead of filling immediately - cancelling",
                    status=yes_status,
                    size_matched=yes_size_matched,
                    intended_size=yes_intended_size,
                )
                # Cancel the unfilled order
                yes_order_id = yes_result.get("id") or yes_result.get("order_id")
                if yes_order_id:
                    try:
                        self._client.cancel(yes_order_id)
                        log.info("Cancelled unfilled YES order", order_id=yes_order_id[:20] + "...")
                    except Exception as cancel_err:
                        log.warning("Failed to cancel YES order", error=str(cancel_err))

                return {
                    "yes_order": yes_result,
                    "no_order": None,
                    "success": False,
                    "partial_fill": False,
                    "error": f"YES order went LIVE instead of filling - insufficient liquidity at price",
                    # Phase 5: Liquidity data captured before execution
                    "pre_fill_yes_depth": pre_fill_yes_depth,
                    "pre_fill_no_depth": pre_fill_no_depth,
                }

            if not yes_filled:
                log.warning(
                    "YES order did not fill",
                    status=yes_status,
                    result=yes_result,
                )
                return {
                    "yes_order": yes_result,
                    "no_order": None,
                    "success": False,
                    "partial_fill": False,
                    "error": f"YES order rejected: {yes_status}",
                    # Phase 5: Liquidity data captured before execution
                    "pre_fill_yes_depth": pre_fill_yes_depth,
                    "pre_fill_no_depth": pre_fill_no_depth,
                }

            log.info("YES order filled", status=yes_status, size_matched=yes_size_matched)

            # Step 2: YES filled, now place NO order
            no_result = await asyncio.wait_for(
                asyncio.to_thread(place_fok_order, no_token_id, no_amount_usd, "NO"),
                timeout=timeout_seconds,
            )

            # Check if NO order filled
            # IMPORTANT: LIVE means order is on the book waiting, NOT filled
            no_status = no_result.get("status", "").upper()
            no_filled = no_status in ("MATCHED", "FILLED")
            no_size_matched = float(no_result.get("size_matched", 0) or no_result.get("matched_size", 0) or 0)

            # If NO order went LIVE or didn't fill, we have a partial fill situation
            if no_status == "LIVE" or not no_filled:
                # CRITICAL: YES filled but NO didn't - we have a partial fill!
                #
                # Phase 4 Fix (2025-12-14): DO NOT attempt to "unwind"
                # Unwinding by selling creates a NEW trade and guarantees loss.
                # Strategy will record the partial fill (Phase 2) and hold position.
                log.error(
                    "PARTIAL FILL: YES filled but NO did not",
                    yes_status=yes_status,
                    no_status=no_status,
                    no_result=no_result,
                    note="Strategy will record partial fill. Position held until resolution.",
                )

                # Cancel any LIVE NO order (shouldn't happen with FOK, but defensive)
                no_order_id = no_result.get("id") or no_result.get("order_id")
                if no_status == "LIVE" and no_order_id:
                    try:
                        self._client.cancel(no_order_id)
                        log.info("Cancelled LIVE NO order", order_id=no_order_id[:20] + "...")
                    except Exception as cancel_err:
                        log.warning("Failed to cancel LIVE NO order", error=str(cancel_err))

                # Return partial fill data for strategy to record
                # Note: We do NOT try to unwind - that creates more problems
                yes_filled = yes_size_matched or float(yes_result.get("_intended_size", 0))
                return {
                    "yes_order": yes_result,
                    "no_order": no_result,
                    "success": False,
                    "partial_fill": True,
                    "yes_filled_size": yes_filled,
                    "no_filled_size": 0.0,
                    "yes_filled_cost": yes_filled * yes_price,
                    "no_filled_cost": 0.0,
                    "error": f"PARTIAL FILL: YES filled ({yes_status}), NO rejected ({no_status}). Position held.",
                    # Phase 5: Liquidity data captured before execution
                    "pre_fill_yes_depth": pre_fill_yes_depth,
                    "pre_fill_no_depth": pre_fill_no_depth,
                }

            # Both legs filled successfully!
            log.info(
                "Both legs filled successfully",
                yes_status=yes_status,
                yes_size_matched=yes_size_matched,
                no_status=no_status,
                no_size_matched=no_size_matched,
            )

            # Log fills to liquidity collector if available
            if self._liquidity_collector:
                try:
                    # Log YES fill
                    await self._liquidity_collector.log_fill(
                        token_id=yes_token_id,
                        condition_id=condition_id,
                        asset=asset,
                        side="BUY",
                        intended_size=yes_result.get("_intended_size", 0),
                        intended_price=yes_result.get("_intended_price", 0),
                        order_result=yes_result,
                        start_time_ms=yes_result.get("_start_time_ms", int(time.time() * 1000)),
                        pre_fill_depth=pre_fill_yes_depth,
                    )
                    # Log NO fill
                    await self._liquidity_collector.log_fill(
                        token_id=no_token_id,
                        condition_id=condition_id,
                        asset=asset,
                        side="BUY",
                        intended_size=no_result.get("_intended_size", 0),
                        intended_price=no_result.get("_intended_price", 0),
                        order_result=no_result,
                        start_time_ms=no_result.get("_start_time_ms", int(time.time() * 1000)),
                        pre_fill_depth=pre_fill_no_depth,
                    )
                except Exception as fill_log_err:
                    log.warning("Failed to log fills", error=str(fill_log_err))

            return {
                "yes_order": yes_result,
                "no_order": no_result,
                "success": True,
                "partial_fill": False,
                # Phase 5: Liquidity data captured before execution
                "pre_fill_yes_depth": pre_fill_yes_depth,
                "pre_fill_no_depth": pre_fill_no_depth,
            }

        except asyncio.TimeoutError:
            log.error("Dual-leg order timed out")
            # Cancel any pending orders
            try:
                self.cancel_all_orders()
            except Exception:
                pass
            return {
                "yes_order": None,
                "no_order": None,
                "success": False,
                "partial_fill": False,
                "error": "timeout",
                # Phase 5: Liquidity data (may not be captured if exception occurred early)
                "pre_fill_yes_depth": locals().get("pre_fill_yes_depth", 0.0),
                "pre_fill_no_depth": locals().get("pre_fill_no_depth", 0.0),
            }

        except Exception as e:
            log.error("Dual-leg order failed", error=str(e))
            # Cancel any pending orders
            try:
                self.cancel_all_orders()
            except Exception:
                pass
            return {
                "yes_order": None,
                "no_order": None,
                "success": False,
                "partial_fill": False,
                "error": str(e),
                # Phase 5: Liquidity data (may not be captured if exception occurred early)
                "pre_fill_yes_depth": locals().get("pre_fill_yes_depth", 0.0),
                "pre_fill_no_depth": locals().get("pre_fill_no_depth", 0.0),
            }

    async def execute_dual_leg_order_parallel(
        self,
        yes_token_id: str,
        no_token_id: str,
        yes_amount_usd: float,
        no_amount_usd: float,
        yes_price: float,  # EXACT limit price from opportunity detection
        no_price: float,   # EXACT limit price from opportunity detection
        timeout_seconds: float = 5.0,
        max_liquidity_consumption_pct: float = 0.50,
        condition_id: str = "",
        asset: str = "",
    ) -> Dict[str, Any]:
        """Execute YES and NO orders in PARALLEL for true atomic execution.

        IMPORTANT: Prices are EXACT limit prices. NO slippage is added.
        If we can't fill at these prices, we don't take the trade.
        The goal is precision execution, not guaranteed fills.

        This provides better atomicity because:
        1. Both orders hit the book at nearly the same time
        2. Less time for market conditions to change between legs
        3. Cleaner failure mode - if either fails, we cancel both

        Args:
            yes_token_id: YES token ID
            no_token_id: NO token ID
            yes_amount_usd: Amount to spend on YES
            no_amount_usd: Amount to spend on NO
            yes_price: EXACT limit price for YES (from opportunity, no slippage)
            no_price: EXACT limit price for NO (from opportunity, no slippage)
            timeout_seconds: Timeout for both orders to fill
            max_liquidity_consumption_pct: Max % of displayed liquidity to consume
            condition_id: Market condition ID (for logging)
            asset: Asset symbol (for logging)

        Returns:
            Dict with 'yes_order', 'no_order', 'success', 'partial_fill' keys
        """
        # Validate arbitrage still makes sense BEFORE any execution
        total_cost = yes_price + no_price
        expected_profit_per_share = 1.0 - total_cost

        if total_cost >= 1.0:
            log.warning(
                "Arbitrage INVALID - total cost >= $1.00, rejecting trade",
                yes_price=f"${yes_price:.2f}",
                no_price=f"${no_price:.2f}",
                total_cost=f"${total_cost:.2f}",
            )
            return {
                "yes_order": None,
                "no_order": None,
                "success": False,
                "partial_fill": False,
                "error": f"Arbitrage invalidated - prices sum to ${total_cost:.2f} >= $1.00",
                # Phase 5: Liquidity data (not captured - early rejection)
                "pre_fill_yes_depth": 0.0,
                "pre_fill_no_depth": 0.0,
            }

        log.info(
            "Executing PARALLEL dual-leg arbitrage with EXACT pricing (no slippage)",
            yes_amount=yes_amount_usd,
            no_amount=no_amount_usd,
            yes_limit=f"${yes_price:.2f}",
            no_limit=f"${no_price:.2f}",
            total_cost=f"${total_cost:.2f}",
            expected_profit_per_share=f"${expected_profit_per_share:.2f}",
            timeout=timeout_seconds,
        )

        # Pre-flight liquidity check with configurable consumption limit
        # NOTE: We use the passed-in prices (yes_price, no_price) for share calculations,
        # NOT prices fetched from order book here. The prices come from opportunity detection.
        try:
            yes_book = self.get_order_book(yes_token_id)
            no_book = self.get_order_book(no_token_id)

            # Handle both dict and OrderBookSummary object from py-clob-client
            # py-clob-client now returns OrderBookSummary objects with .asks/.bids attributes
            if hasattr(yes_book, "asks"):
                yes_asks = yes_book.asks or []
            else:
                yes_asks = yes_book.get("asks", [])

            if hasattr(no_book, "asks"):
                no_asks = no_book.asks or []
            else:
                no_asks = no_book.get("asks", [])

            if not yes_asks or not no_asks:
                log.warning("Insufficient liquidity - no asks on one or both sides")
                return {
                    "yes_order": None,
                    "no_order": None,
                    "success": False,
                    "partial_fill": False,
                    "error": "Insufficient liquidity - no asks available",
                    # Phase 5: Liquidity data (empty book - no depth available)
                    "pre_fill_yes_depth": 0.0,
                    "pre_fill_no_depth": 0.0,
                }

            # Calculate available liquidity (top 3 levels)
            # Handle both dict and OrderBookLevel objects from py-clob-client
            def get_ask_size(ask):
                if hasattr(ask, "size"):
                    return float(ask.size or 0)
                return float(ask.get("size", 0))

            yes_displayed = sum(get_ask_size(ask) for ask in yes_asks[:3])
            no_displayed = sum(get_ask_size(ask) for ask in no_asks[:3])

            # Use the PASSED-IN prices for share calculations (from opportunity detection)
            yes_shares_needed = yes_amount_usd / yes_price if yes_price > 0 else 0
            no_shares_needed = no_amount_usd / no_price if no_price > 0 else 0

            # Enforce max liquidity consumption
            max_yes_shares = yes_displayed * max_liquidity_consumption_pct
            max_no_shares = no_displayed * max_liquidity_consumption_pct

            if yes_shares_needed > max_yes_shares:
                log.warning(
                    "YES order would consume too much liquidity",
                    needed=f"{yes_shares_needed:.1f}",
                    max_allowed=f"{max_yes_shares:.1f}",
                    pct=f"{max_liquidity_consumption_pct*100:.0f}%",
                )
                return {
                    "yes_order": None,
                    "no_order": None,
                    "success": False,
                    "partial_fill": False,
                    "error": f"YES order would consume {yes_shares_needed/yes_displayed*100:.0f}% of liquidity (max {max_liquidity_consumption_pct*100:.0f}%)",
                    # Phase 5: Liquidity data (rejected due to consumption limit)
                    "pre_fill_yes_depth": yes_displayed,
                    "pre_fill_no_depth": no_displayed,
                }

            if no_shares_needed > max_no_shares:
                log.warning(
                    "NO order would consume too much liquidity",
                    needed=f"{no_shares_needed:.1f}",
                    max_allowed=f"{max_no_shares:.1f}",
                    pct=f"{max_liquidity_consumption_pct*100:.0f}%",
                )
                return {
                    "yes_order": None,
                    "no_order": None,
                    "success": False,
                    "partial_fill": False,
                    "error": f"NO order would consume {no_shares_needed/no_displayed*100:.0f}% of liquidity (max {max_liquidity_consumption_pct*100:.0f}%)",
                    # Phase 5: Liquidity data (rejected due to consumption limit)
                    "pre_fill_yes_depth": yes_displayed,
                    "pre_fill_no_depth": no_displayed,
                }

            log.info(
                "Liquidity check passed for parallel execution",
                yes_consumption=f"{yes_shares_needed/yes_displayed*100:.0f}%",
                no_consumption=f"{no_shares_needed/no_displayed*100:.0f}%",
            )

            pre_fill_yes_depth = yes_displayed
            pre_fill_no_depth = no_displayed

        except Exception as e:
            log.warning("Liquidity check failed, proceeding anyway", error=str(e))
            pre_fill_yes_depth = 0.0
            pre_fill_no_depth = 0.0

        def place_order_sync(token_id: str, amount_usd: float, label: str, limit_price: float) -> Dict[str, Any]:
            """Place a single order at EXACT limit price. No slippage, no re-fetching.

            CRITICAL: The limit_price is used EXACTLY as provided.
            - DO NOT add slippage - the arbitrage profit depends on exact prices
            - DO NOT re-fetch price from API - we trust the opportunity detection
            - If we can't fill at this price, we simply don't fill (that's OK)

            Polymarket decimal requirements:
            - maker_amount (USD cost): max 2 decimal places
            - taker_amount (shares): max 4 decimal places
            - price: 2 decimal places

            Returns dict with order info, or error info if order fails.
            NEVER raises - always returns a dict so parallel execution can detect partial fills.
            """
            from decimal import Decimal, ROUND_DOWN

            start_time_ms = int(time.time() * 1000)

            try:
                # Polymarket API requirements for market BUY orders (especially FOK):
                # - maker_amount (USD you pay): max 2 decimal places
                # - taker_amount (shares you receive): max 2 decimal places (py-clob-client limit)
                # - price: 2 decimal places
                #
                # CRITICAL: FOK orders require maker_amount (shares × price) to have ≤2 decimals.
                # Since shares × price rarely produces clean results (e.g., 25.48 × 0.35 = 8.918),
                # we work BACKWARDS: round maker_amount first, then calculate shares.

                # Ensure price has 2 decimal places
                price_d = Decimal(str(limit_price)).quantize(Decimal("0.01"), rounding=ROUND_DOWN)

                # Round USD amount to 2 decimals - this IS our maker_amount (guaranteed clean)
                maker_amount_d = Decimal(str(amount_usd)).quantize(Decimal("0.01"), rounding=ROUND_DOWN)

                # Calculate shares from clean maker_amount
                # Round to 2 decimals (py-clob-client limit) with ROUND_DOWN
                shares_d = (maker_amount_d / price_d).quantize(Decimal("0.01"), rounding=ROUND_DOWN)

                # Recalculate actual maker_amount from rounded shares (this is what API sees)
                actual_maker = shares_d * price_d

                # If the actual maker has more than 2 decimals, reduce shares until clean
                # Worst case is ~100 iterations for pathological prices like 0.97
                for _ in range(200):
                    actual_maker = shares_d * price_d
                    actual_maker_rounded = actual_maker.quantize(Decimal("0.01"), rounding=ROUND_DOWN)
                    if actual_maker == actual_maker_rounded:
                        break  # Clean!
                    shares_d = shares_d - Decimal("0.01")
                    if shares_d <= 0:
                        shares_d = Decimal("0.01")  # Minimum viable shares
                        break

                final_price = float(price_d)
                shares = float(shares_d)
                final_maker_amount = float(actual_maker)

                log.info(
                    f"Placing {label} order at EXACT limit (no slippage)",
                    limit_price=f"${final_price:.2f}",
                    shares=f"{shares:.2f}",
                    maker_amount=f"${final_maker_amount:.2f}",
                )

                order_args = OrderArgs(
                    token_id=token_id,
                    price=final_price,
                    size=shares,
                    side="BUY",
                )

                signed_order = self._client.create_order(order_args)
                # Use FOK (Fill-or-Kill) for atomicity - either fill completely or not at all
                result = self._client.post_order(signed_order, orderType=OrderType.FOK)

                result["_intended_size"] = shares
                result["_intended_price"] = final_price
                result["_start_time_ms"] = start_time_ms
                result["_label"] = label

                return result

            except Exception as e:
                # CRITICAL: Return error dict instead of raising, so parallel execution
                # can detect partial fills (when one order succeeds and one fails)
                log.warning(
                    f"{label} order failed with exception",
                    error=str(e),
                    token_id=token_id[:20] + "...",
                )
                return {
                    "status": "EXCEPTION",
                    "error": str(e),
                    "_intended_size": locals().get("shares", 0),
                    "_intended_price": locals().get("final_price", limit_price),
                    "_start_time_ms": start_time_ms,
                    "_label": label,
                    "size_matched": 0,
                }

        try:
            # PARALLEL EXECUTION: Place both orders simultaneously
            yes_task = asyncio.create_task(
                asyncio.to_thread(place_order_sync, yes_token_id, yes_amount_usd, "YES", yes_price)
            )
            no_task = asyncio.create_task(
                asyncio.to_thread(place_order_sync, no_token_id, no_amount_usd, "NO", no_price)
            )

            # Wait for both with timeout
            try:
                yes_result, no_result = await asyncio.wait_for(
                    asyncio.gather(yes_task, no_task),
                    timeout=timeout_seconds,
                )
            except asyncio.TimeoutError:
                log.error("Parallel order placement timed out")
                # Cancel any pending tasks
                yes_task.cancel()
                no_task.cancel()
                # Try to cancel any orders that might have been placed
                try:
                    self.cancel_all_orders()
                except Exception:
                    pass
                return {
                    "yes_order": None,
                    "no_order": None,
                    "success": False,
                    "partial_fill": False,
                    "error": "Parallel order placement timed out",
                }

            # Check fill status for both orders
            yes_status = yes_result.get("status", "").upper()
            no_status = no_result.get("status", "").upper()
            yes_filled = yes_status in ("MATCHED", "FILLED")
            no_filled = no_status in ("MATCHED", "FILLED")

            yes_order_id = yes_result.get("id") or yes_result.get("order_id")
            no_order_id = no_result.get("id") or no_result.get("order_id")

            log.info(
                "Parallel order results",
                yes_status=yes_status,
                no_status=no_status,
                yes_filled=yes_filled,
                no_filled=no_filled,
            )

            # Case 1: Both filled immediately - success!
            if yes_filled and no_filled:
                yes_size_matched = float(yes_result.get("size_matched", 0) or yes_result.get("matched_size", 0) or 0)
                no_size_matched = float(no_result.get("size_matched", 0) or no_result.get("matched_size", 0) or 0)
                log.info(
                    "Both legs filled successfully (parallel)",
                    yes_size=yes_size_matched,
                    no_size=no_size_matched,
                )
                return {
                    "yes_order": yes_result,
                    "no_order": no_result,
                    "success": True,
                    "partial_fill": False,
                    "yes_filled_size": yes_size_matched or float(yes_result.get("_intended_size", 0)),
                    "no_filled_size": no_size_matched or float(no_result.get("_intended_size", 0)),
                    # Phase 5: Liquidity data captured before execution
                    "pre_fill_yes_depth": pre_fill_yes_depth,
                    "pre_fill_no_depth": pre_fill_no_depth,
                }

            # Case 2: One or both didn't fill (went LIVE instead of MATCHED)
            # Phase 4 Fix (2025-12-14): Proper handling of partial fills
            #
            # With GTC orders (we use GTC instead of FOK due to py-clob-client bugs):
            # - MATCHED = filled completely
            # - LIVE = order sitting on book, not filled yet
            #
            # CRITICAL: We do NOT attempt to "unwind" MATCHED positions because:
            # 1. Selling creates a NEW trade, not an unwind
            # 2. Selling at market creates additional losses (slippage)
            # 3. The strategy records partial fills properly (Phase 2)
            # 4. Better to hold the position than take guaranteed loss
            #
            # Action: Cancel any LIVE orders and return accurate fill data for strategy.

            # Determine what actually filled
            yes_size_matched = float(yes_result.get("size_matched", 0) or yes_result.get("matched_size", 0) or 0)
            no_size_matched = float(no_result.get("size_matched", 0) or no_result.get("matched_size", 0) or 0)

            # Calculate costs based on what actually filled
            yes_filled_cost = yes_size_matched * yes_price if yes_filled else 0.0
            no_filled_cost = no_size_matched * no_price if no_filled else 0.0

            partial_fill = (yes_filled and not no_filled) or (no_filled and not yes_filled)

            log.warning(
                "Orders did not fill atomically",
                yes_status=yes_status,
                no_status=no_status,
                yes_filled=yes_filled,
                no_filled=no_filled,
                yes_size_matched=yes_size_matched,
                no_size_matched=no_size_matched,
                partial_fill=partial_fill,
            )

            # Cancel any LIVE orders (GTC orders may sit on book if not filled)
            # Note: We do NOT cancel MATCHED orders - those are complete fills
            if yes_status == "LIVE" and yes_order_id:
                try:
                    self._client.cancel(yes_order_id)
                    log.info("Cancelled LIVE YES order", order_id=yes_order_id[:20] + "...")
                except Exception as e:
                    log.warning("Failed to cancel LIVE YES order", error=str(e))

            if no_status == "LIVE" and no_order_id:
                try:
                    self._client.cancel(no_order_id)
                    log.info("Cancelled LIVE NO order", order_id=no_order_id[:20] + "...")
                except Exception as e:
                    log.warning("Failed to cancel LIVE NO order", error=str(e))

            # Log partial fill for monitoring (but don't try to unwind!)
            if partial_fill:
                filled_leg = "YES" if yes_filled else "NO"
                unfilled_leg = "NO" if yes_filled else "YES"
                filled_shares = yes_size_matched if yes_filled else no_size_matched
                log.error(
                    f"PARTIAL FILL: {filled_leg} filled, {unfilled_leg} did not",
                    filled_leg=filled_leg,
                    filled_shares=filled_shares,
                    unfilled_status=no_status if yes_filled else yes_status,
                    note="Strategy will record partial fill. Position will be held until resolution.",
                )

            return {
                "yes_order": yes_result,
                "no_order": no_result,
                "success": False,
                "partial_fill": partial_fill,
                "yes_filled_size": yes_size_matched if yes_filled else 0.0,
                "no_filled_size": no_size_matched if no_filled else 0.0,
                "yes_filled_cost": yes_filled_cost,
                "no_filled_cost": no_filled_cost,
                "error": f"Orders did not fill atomically (YES:{yes_status}, NO:{no_status})",
                # Phase 5: Liquidity data captured before execution
                "pre_fill_yes_depth": pre_fill_yes_depth,
                "pre_fill_no_depth": pre_fill_no_depth,
            }

        except Exception as e:
            log.error("Parallel dual-leg order failed", error=str(e))
            try:
                self.cancel_all_orders()
            except Exception:
                pass
            return {
                "yes_order": None,
                "no_order": None,
                "success": False,
                "partial_fill": False,
                "error": str(e),
                # Phase 5: Liquidity data (may not be captured if exception occurred early)
                "pre_fill_yes_depth": locals().get("pre_fill_yes_depth", 0.0),
                "pre_fill_no_depth": locals().get("pre_fill_no_depth", 0.0),
            }

    # =========================================================================
    # API Key Management
    # =========================================================================

    def derive_api_credentials(self) -> Optional[ApiCreds]:
        """Derive API credentials from private key.

        Returns:
            ApiCreds if successful, None otherwise
        """
        self._ensure_connected()
        try:
            creds = self._client.create_or_derive_api_creds()
            log.info("Derived API credentials successfully")
            return creds
        except Exception as e:
            log.error("Failed to derive API credentials", error=str(e))
            return None

    # =========================================================================
    # Market Resolution
    # =========================================================================

    def _get_market_resolution_sync(self, condition_id: str) -> Optional[Dict[str, Any]]:
        """Synchronous implementation of get_market_resolution."""
        self._ensure_connected()
        try:
            market = self._client.get_market(condition_id)
            # Check if market has resolution data
            if market.get("resolved") or market.get("resolution_source"):
                return {
                    "resolved": True,
                    "outcome": market.get("outcome"),
                    "resolution_time": market.get("resolution_time"),
                }
            return None
        except Exception as e:
            log.error("Failed to get market resolution", error=str(e))
            return None

    async def get_market_resolution(self, condition_id: str) -> Optional[Dict[str, Any]]:
        """Get resolution status for a market (async).

        Args:
            condition_id: The market condition ID

        Returns:
            Resolution data if resolved, None if not yet resolved
        """
        return await asyncio.to_thread(self._get_market_resolution_sync, condition_id)

    # =========================================================================
    # Async wrappers for main.py compatibility
    # =========================================================================

    async def connect(self) -> bool:
        """Async wrapper for connect."""
        return await asyncio.to_thread(self._connect_sync)

    def _connect_sync(self) -> bool:
        """Synchronous connect implementation."""
        try:
            self._client = ClobClient(
                host=self.settings.clob_http_url,
                key=self.settings.private_key,
                chain_id=137,  # Polygon Mainnet
                signature_type=self.settings.signature_type,
                funder=self.settings.proxy_wallet or None,
            )

            # Set API credentials if available
            if self.settings.api_key:
                creds = ApiCreds(
                    api_key=self.settings.api_key,
                    api_secret=self.settings.api_secret,
                    api_passphrase=self.settings.api_passphrase,
                )
                self._client.set_api_creds(creds)

            # Test connection
            self._client.get_ok()
            self._connected = True
            log.info("Connected to Polymarket CLOB")
            return True

        except Exception as e:
            log.error("Failed to connect to Polymarket", error=str(e))
            self._connected = False
            return False

    async def disconnect(self) -> None:
        """Disconnect from Polymarket."""
        self._connected = False
        self._client = None
        log.info("Disconnected from Polymarket CLOB")

    # =========================================================================
    # Position & Order Management for Auto-Settlement
    # =========================================================================

    def get_open_orders(self) -> List[Dict[str, Any]]:
        """Get all open orders.

        Returns:
            List of open orders with their details
        """
        self._ensure_connected()
        try:
            return self._client.get_orders()
        except Exception as e:
            log.error("Failed to get open orders", error=str(e))
            return []

    def get_trade_history(self, limit: int = 100) -> List[Dict[str, Any]]:
        """Get recent trade history.

        Args:
            limit: Maximum number of trades to return

        Returns:
            List of historical trades
        """
        self._ensure_connected()
        try:
            # The py-clob-client get_trades returns trades
            return self._client.get_trades()
        except Exception as e:
            log.error("Failed to get trade history", error=str(e))
            return []

    async def cancel_stale_orders(self, active_market_ids: set) -> Dict[str, Any]:
        """Cancel orders for markets that are no longer active.

        This should be called periodically to clean up unfilled GTC orders
        for markets that have ended.

        Args:
            active_market_ids: Set of condition IDs for currently active markets

        Returns:
            Dict with 'cancelled' count and 'errors' list
        """
        log.info("Checking for stale orders to cancel")
        result = {"cancelled": 0, "errors": []}

        try:
            orders = self.get_open_orders()
            if not orders:
                return result

            for order in orders:
                # Get the asset_id (token_id) from the order
                asset_id = order.get("asset_id") or order.get("token_id")
                order_id = order.get("id") or order.get("order_id")

                if not order_id:
                    continue

                # Check if this order's market is still active
                # Orders may have market info or we need to track it separately
                market_id = order.get("market") or order.get("condition_id")

                # If we can't determine the market, skip
                if market_id and market_id not in active_market_ids:
                    log.info(
                        "Cancelling stale order for ended market",
                        order_id=order_id[:20] + "..." if order_id else None,
                        market_id=market_id[:20] + "..." if market_id else None,
                    )
                    try:
                        self._client.cancel(order_id)
                        result["cancelled"] += 1
                    except Exception as e:
                        result["errors"].append(f"Failed to cancel {order_id}: {str(e)}")

            log.info(
                "Stale order cleanup complete",
                cancelled=result["cancelled"],
                errors=len(result["errors"]),
            )

        except Exception as e:
            log.error("Error during stale order cleanup", error=str(e))
            result["errors"].append(str(e))

        return result

    async def claim_resolved_position(
        self,
        token_id: str,
        shares: float,
        timeout_seconds: float = 10.0,
    ) -> Dict[str, Any]:
        """Claim proceeds from a resolved market by selling at ~0.99.

        After a market resolves, winning positions can be sold at 0.99 to claim
        the USDC proceeds. This is a workaround since the py-clob-client doesn't
        have a native redeem function.

        See: https://github.com/Polymarket/py-clob-client/issues/117

        Args:
            token_id: The winning token ID to sell
            shares: Number of shares to sell
            timeout_seconds: Timeout for order execution

        Returns:
            Dict with 'success', 'proceeds', and 'error' keys
        """
        from decimal import Decimal, ROUND_DOWN

        log.info(
            "Claiming resolved position by selling at 0.99",
            token_id=token_id[:20] + "...",
            shares=shares,
        )

        try:
            # Sell at 0.99 (the max realistic price for a won position)
            # Using Decimal for precision
            shares_d = Decimal(str(shares)).quantize(Decimal("0.01"), rounding=ROUND_DOWN)
            sell_price = Decimal("0.99")

            shares_float = float(shares_d)

            order_args = OrderArgs(
                token_id=token_id,
                price=float(sell_price),
                size=shares_float,
                side="SELL",
            )

            # Use GTC order
            signed_order = self._client.create_order(order_args)
            result = await asyncio.wait_for(
                asyncio.to_thread(
                    lambda: self._client.post_order(signed_order, orderType=OrderType.GTC)
                ),
                timeout=timeout_seconds,
            )

            status = result.get("status", "").upper()
            if status in ("MATCHED", "FILLED", "LIVE"):
                proceeds = float(shares_d * sell_price)
                log.info(
                    "Position claimed successfully",
                    shares=shares_float,
                    proceeds=f"${proceeds:.2f}",
                )
                return {
                    "success": True,
                    "proceeds": proceeds,
                    "order": result,
                }
            else:
                log.warning(
                    "Claim order not filled",
                    status=status,
                    result=result,
                )
                return {
                    "success": False,
                    "proceeds": 0.0,
                    "error": f"Order status: {status}",
                }

        except asyncio.TimeoutError:
            log.error("Claim order timed out")
            return {"success": False, "proceeds": 0.0, "error": "timeout"}
        except Exception as e:
            log.error("Failed to claim position", error=str(e))
            return {"success": False, "proceeds": 0.0, "error": str(e)}
