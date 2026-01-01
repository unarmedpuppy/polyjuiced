"""Polymarket CLOB client wrapper."""

import asyncio
import time
from decimal import Decimal
from typing import Any, Callable, Dict, List, Optional, TYPE_CHECKING

import structlog
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds, MarketOrderArgs, OrderArgs, OrderType
from tenacity import retry, stop_after_attempt, wait_exponential
from web3 import Web3

# web3.py v7+ renamed geth_poa_middleware to ExtraDataToPOAMiddleware
try:
    from web3.middleware import ExtraDataToPOAMiddleware as poa_middleware
except ImportError:
    from web3.middleware import geth_poa_middleware as poa_middleware

from ..config import PolymarketSettings

# Conditional Tokens Framework contract ABI (redeemPositions function only)
# See: https://github.com/Polymarket/conditional-token-examples-py
CTF_REDEEM_ABI = [
    {
        "constant": False,
        "inputs": [
            {"name": "collateralToken", "type": "address"},
            {"name": "parentCollectionId", "type": "bytes32"},
            {"name": "conditionId", "type": "bytes32"},
            {"name": "indexSets", "type": "uint256[]"},
        ],
        "name": "redeemPositions",
        "outputs": [],
        "payable": False,
        "stateMutability": "nonpayable",
        "type": "function",
    }
]

# Polygon mainnet contract addresses
CTF_ADDRESS = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"  # Conditional Tokens Framework
USDC_ADDRESS = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"  # USDC on Polygon

if TYPE_CHECKING:
    from ..liquidity.collector import LiquidityCollector

log = structlog.get_logger()


class PolymarketClient:
    """Wrapper around py-clob-client with async support and error handling."""

    def __init__(self, settings: PolymarketSettings):
        self.settings = settings
        self._client: Optional[ClobClient] = None
        self._connected = False
        self._liquidity_collector: Optional["LiquidityCollector"] = None
        self._w3: Optional[Web3] = None
        self._ctf_contract = None

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
        partial_fill_exit_enabled: bool = False,  # Phase 5: Exit partial fills immediately
        partial_fill_max_slippage_cents: float = 2.0,  # Phase 5: Max slippage on exit
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
                # Phase 5: Rebalance partial fill - try to complete hedge, then exit if needed
                yes_filled = yes_size_matched or float(yes_result.get("_intended_size", 0))
                yes_filled_cost = yes_filled * yes_price

                # Cancel any LIVE NO order (shouldn't happen with FOK, but defensive)
                no_order_id = no_result.get("id") or no_result.get("order_id")
                if no_status == "LIVE" and no_order_id:
                    try:
                        self._client.cancel(no_order_id)
                        log.info("Cancelled LIVE NO order", order_id=no_order_id[:20] + "...")
                    except Exception as cancel_err:
                        log.warning("Failed to cancel LIVE NO order", error=str(cancel_err))

                # Phase 5: Rebalance partial fill - try to complete hedge, then exit if needed
                if partial_fill_exit_enabled and yes_filled > 0:
                    log.error(
                        "PARTIAL FILL: YES filled, NO did not - REBALANCING",
                        filled_leg="YES",
                        filled_shares=yes_filled,
                        filled_cost=f"${yes_filled_cost:.2f}",
                        unfilled_status=no_status,
                        rebalance_strategy="1) Try to buy missing leg, 2) If fail, sell filled leg",
                    )

                    # Rebalance: try to complete hedge first, then exit if needed
                    rebalance_result = await self.rebalance_partial_fill(
                        filled_token_id=yes_token_id,
                        unfilled_token_id=no_token_id,
                        filled_shares=yes_filled,
                        filled_price=yes_price,
                        unfilled_price=no_price,
                        max_slippage_cents=partial_fill_max_slippage_cents,
                    )

                    action = rebalance_result.get("action", "unknown")
                    pnl = rebalance_result.get("pnl", rebalance_result.get("expected_profit", 0))

                    return {
                        "yes_order": yes_result,
                        "no_order": no_result,
                        "success": action == "hedge_completed",  # Success if we completed the hedge!
                        "partial_fill": True,
                        "partial_fill_rebalanced": True,  # Phase 5: Mark that we rebalanced
                        "rebalance_result": rebalance_result,  # Phase 5: Include rebalance result
                        "rebalance_action": action,  # hedge_completed, exited, or exit_failed
                        "yes_filled_size": yes_filled,
                        "no_filled_size": 0.0,
                        "yes_filled_cost": yes_filled_cost,
                        "no_filled_cost": 0.0,
                        "error": f"PARTIAL FILL {action.upper()}: YES filled. Action: {action}. P&L: ${pnl:.2f}",
                        # Phase 5: Liquidity data captured before execution
                        "pre_fill_yes_depth": pre_fill_yes_depth,
                        "pre_fill_no_depth": pre_fill_no_depth,
                    }
                else:
                    # Legacy behavior: Hold position until resolution (50/50 gamble)
                    log.error(
                        "PARTIAL FILL: YES filled but NO did not",
                        yes_status=yes_status,
                        no_status=no_status,
                        no_result=no_result,
                        note="Rebalance disabled or no shares - position held until resolution (RISK!).",
                    )

                    # Return partial fill data for strategy to record
                    return {
                        "yes_order": yes_result,
                        "no_order": no_result,
                        "success": False,
                        "partial_fill": True,
                        "yes_filled_size": yes_filled,
                        "no_filled_size": 0.0,
                        "yes_filled_cost": yes_filled_cost,
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
        price_buffer_cents: float = 1.0,  # Phase 4: Price buffer for better fills
        partial_fill_exit_enabled: bool = False,  # Phase 5: Exit partial fills immediately
        partial_fill_max_slippage_cents: float = 2.0,  # Phase 5: Max slippage on exit
    ) -> Dict[str, Any]:
        """Execute YES and NO orders in PARALLEL for true atomic execution.

        Phase 4 (Dec 17, 2025): Added price_buffer_cents parameter.
        Orders are placed at (price + buffer) to improve fill rates while
        maintaining profitability (as long as buffer < spread).

        Phase 5 (Dec 17, 2025): Added partial_fill_exit_enabled parameter.
        When one leg fills but the other doesn't, we now IMMEDIATELY exit
        the filled position to avoid unhedged directional exposure. This
        converts a 50/50 gamble on market resolution into a small, predictable
        spread loss.

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

        def place_order_sync(token_id: str, amount_usd: float, label: str, limit_price: float, price_buffer_cents: float = 1.0) -> Dict[str, Any]:
            """Place a single order with aggressive GTC pricing for better fills.

            Phase 4 (Dec 17, 2025): Changed from FOK to GTC with price buffer.
            - FOK was failing too often due to price movements
            - GTC with +1¢ buffer gives better fill rates while maintaining profitability
            - Orders that don't fill immediately stay on book briefly

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
                # Add price buffer for better fill rates (configurable, default +1¢)
                # This trades a small amount of profit for much higher fill rate
                buffered_price = limit_price + (price_buffer_cents / 100.0)
                # Cap at $0.99 (can't buy for more than $1)
                buffered_price = min(buffered_price, 0.99)

                # Polymarket API requirements for market BUY orders:
                # - maker_amount (USD you pay): max 2 decimal places
                # - taker_amount (shares you receive): max 2 decimal places (py-clob-client limit)
                # - price: 2 decimal places
                #
                # Since shares × price rarely produces clean results (e.g., 25.48 × 0.35 = 8.918),
                # we work BACKWARDS: round maker_amount first, then calculate shares.

                # Ensure price has 2 decimal places
                price_d = Decimal(str(buffered_price)).quantize(Decimal("0.01"), rounding=ROUND_DOWN)

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
                    f"Placing {label} GTC order with +{price_buffer_cents:.0f}¢ buffer",
                    original_price=f"${limit_price:.2f}",
                    buffered_price=f"${final_price:.2f}",
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
                # Phase 4: Use GTC instead of FOK for better fill rates
                # GTC orders sit on book if not filled immediately, giving more chances to fill
                # FOK was failing too often due to price movements during execution
                result = self._client.post_order(signed_order, orderType=OrderType.GTC)

                result["_intended_size"] = shares
                result["_intended_price"] = final_price
                result["_original_price"] = limit_price
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
            # Phase 4: Pass price_buffer_cents for better fill rates
            yes_task = asyncio.create_task(
                asyncio.to_thread(place_order_sync, yes_token_id, yes_amount_usd, "YES", yes_price, price_buffer_cents)
            )
            no_task = asyncio.create_task(
                asyncio.to_thread(place_order_sync, no_token_id, no_amount_usd, "NO", no_price, price_buffer_cents)
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

            # Case 2: One or both didn't fill immediately (went LIVE instead of MATCHED)
            # Phase 4 Fix (2025-12-17): Wait for LIVE orders to fill before cancelling
            #
            # With GTC orders (we use GTC instead of FOK due to py-clob-client bugs):
            # - MATCHED = filled completely
            # - LIVE = order sitting on book, may fill shortly
            #
            # Strategy: If one order is MATCHED and the other is LIVE, wait up to 2 seconds
            # for the LIVE order to fill. This handles cases where our order is at a good
            # price but just needs time to match.

            # First, check if we have a partial situation (one MATCHED, one LIVE)
            yes_is_live = yes_status == "LIVE"
            no_is_live = no_status == "LIVE"

            # If either order is LIVE, wait briefly for it to fill
            if yes_is_live or no_is_live:
                log.info(
                    "LIVE order(s) detected, waiting for fill...",
                    yes_status=yes_status,
                    no_status=no_status,
                    wait_seconds=2.0,
                )

                # Wait and re-check status
                await asyncio.sleep(2.0)

                # Re-fetch order status
                try:
                    if yes_is_live and yes_order_id:
                        # Check if YES order filled
                        orders = self._client.get_orders()
                        for order in orders:
                            if order.get("id") == yes_order_id:
                                new_status = order.get("status", "").upper()
                                if new_status in ("MATCHED", "FILLED"):
                                    yes_status = new_status
                                    yes_filled = True
                                    yes_result["status"] = new_status
                                    log.info("YES order filled after wait", new_status=new_status)
                                break

                    if no_is_live and no_order_id:
                        # Check if NO order filled
                        orders = self._client.get_orders()
                        for order in orders:
                            if order.get("id") == no_order_id:
                                new_status = order.get("status", "").upper()
                                if new_status in ("MATCHED", "FILLED"):
                                    no_status = new_status
                                    no_filled = True
                                    no_result["status"] = new_status
                                    log.info("NO order filled after wait", new_status=new_status)
                                break
                except Exception as e:
                    log.warning("Failed to re-check order status", error=str(e))

                # If both now filled, return success
                if yes_filled and no_filled:
                    yes_size_matched = float(yes_result.get("size_matched", 0) or yes_result.get("matched_size", 0) or yes_result.get("_intended_size", 0))
                    no_size_matched = float(no_result.get("size_matched", 0) or no_result.get("matched_size", 0) or no_result.get("_intended_size", 0))
                    log.info(
                        "Both legs filled after wait (parallel)",
                        yes_size=yes_size_matched,
                        no_size=no_size_matched,
                    )
                    return {
                        "yes_order": yes_result,
                        "no_order": no_result,
                        "success": True,
                        "partial_fill": False,
                        "yes_filled_size": yes_size_matched,
                        "no_filled_size": no_size_matched,
                        "pre_fill_yes_depth": pre_fill_yes_depth,
                        "pre_fill_no_depth": pre_fill_no_depth,
                    }

            # If we get here, one or both orders still didn't fill
            # CRITICAL: We do NOT attempt to "unwind" MATCHED positions because:
            # 1. Selling creates a NEW trade, not an unwind
            # 2. Selling at market creates additional losses (slippage)
            # 3. The strategy records partial fills properly (Phase 2)
            # 4. Better to hold the position than take guaranteed loss
            #
            # Action: Cancel any remaining LIVE orders and return accurate fill data.

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

            # Log partial fill for monitoring
            if partial_fill:
                filled_leg = "YES" if yes_filled else "NO"
                unfilled_leg = "NO" if yes_filled else "YES"
                filled_shares = yes_size_matched if yes_filled else no_size_matched
                filled_cost = yes_filled_cost if yes_filled else no_filled_cost
                filled_token_id = yes_token_id if yes_filled else no_token_id
                unfilled_token_id = no_token_id if yes_filled else yes_token_id
                filled_price = yes_price if yes_filled else no_price
                unfilled_price = no_price if yes_filled else yes_price

                # Phase 5: Rebalance partial fill - try to complete hedge, then exit if needed
                if partial_fill_exit_enabled and filled_shares > 0:
                    log.error(
                        f"PARTIAL FILL: {filled_leg} filled, {unfilled_leg} did not - REBALANCING",
                        filled_leg=filled_leg,
                        filled_shares=filled_shares,
                        filled_cost=f"${filled_cost:.2f}",
                        unfilled_status=no_status if yes_filled else yes_status,
                        rebalance_strategy="1) Try to buy missing leg, 2) If fail, sell filled leg",
                    )

                    # Rebalance: try to complete hedge first, then exit if needed
                    rebalance_result = await self.rebalance_partial_fill(
                        filled_token_id=filled_token_id,
                        unfilled_token_id=unfilled_token_id,
                        filled_shares=filled_shares,
                        filled_price=filled_price,
                        unfilled_price=unfilled_price,
                        max_slippage_cents=partial_fill_max_slippage_cents,
                    )

                    action = rebalance_result.get("action", "unknown")
                    pnl = rebalance_result.get("pnl", rebalance_result.get("expected_profit", 0))

                    return {
                        "yes_order": yes_result,
                        "no_order": no_result,
                        "success": action == "hedge_completed",  # Success if we completed the hedge!
                        "partial_fill": True,
                        "partial_fill_rebalanced": True,  # Phase 5: Mark that we rebalanced
                        "rebalance_result": rebalance_result,  # Phase 5: Include rebalance result
                        "rebalance_action": action,  # hedge_completed, exited, or exit_failed
                        "yes_filled_size": yes_size_matched if yes_filled else 0.0,
                        "no_filled_size": no_size_matched if no_filled else 0.0,
                        "yes_filled_cost": yes_filled_cost,
                        "no_filled_cost": no_filled_cost,
                        "error": f"PARTIAL FILL {action.upper()}: {filled_leg} filled. Action: {action}. P&L: ${pnl:.2f}",
                        "pre_fill_yes_depth": pre_fill_yes_depth,
                        "pre_fill_no_depth": pre_fill_no_depth,
                    }
                else:
                    # Legacy behavior: Hold position until resolution (50/50 gamble)
                    log.error(
                        f"PARTIAL FILL: {filled_leg} filled, {unfilled_leg} did not",
                        filled_leg=filled_leg,
                        filled_shares=filled_shares,
                        unfilled_status=no_status if yes_filled else yes_status,
                        note="Rebalance disabled or no shares - position held until resolution (RISK!).",
                    )

            return {
                "yes_order": yes_result,
                "no_order": no_result,
                "success": False,
                "partial_fill": partial_fill,
                "partial_fill_exited": False,  # Phase 5: Not exited
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

    def _get_web3(self) -> Web3:
        """Get or create web3 connection for direct contract calls."""
        if self._w3 is None:
            rpc_url = self.settings.polygon_rpc_url
            self._w3 = Web3(Web3.HTTPProvider(rpc_url))
            self._w3.middleware_onion.inject(poa_middleware, layer=0)
            
            ctf_address = Web3.to_checksum_address(CTF_ADDRESS)
            self._ctf_contract = self._w3.eth.contract(
                address=ctf_address, 
                abi=CTF_REDEEM_ABI
            )
            log.info("Web3 connection established for redemptions", rpc=rpc_url)
        return self._w3

    async def redeem_positions_direct(
        self,
        condition_id: str,
        timeout_seconds: float = 60.0,
    ) -> Dict[str, Any]:
        """Redeem winning positions directly via CTF smart contract.
        
        This calls redeemPositions() on the Conditional Tokens Framework contract
        to convert winning outcome tokens back to USDC. This is the proper way to
        claim winnings - much more reliable than trying to sell at 0.99.
        
        See: https://github.com/Polymarket/conditional-token-examples-py
        
        Args:
            condition_id: The market's condition ID (bytes32 hex string)
            timeout_seconds: Timeout for transaction confirmation
            
        Returns:
            Dict with 'success', 'tx_hash', 'error' keys
        """
        log.info("Redeeming position via direct contract call", condition_id=condition_id[:20] + "...")
        
        try:
            w3 = self._get_web3()
            
            account = w3.eth.account.from_key(self.settings.private_key)
            usdc_address = Web3.to_checksum_address(USDC_ADDRESS)
            
            parent_collection_id = bytes(32)
            condition_bytes = Web3.to_bytes(hexstr=condition_id)
            index_sets = [1, 2]
            
            nonce = w3.eth.get_transaction_count(account.address)
            gas_price = w3.eth.gas_price
            
            txn = self._ctf_contract.functions.redeemPositions(
                usdc_address,
                parent_collection_id,
                condition_bytes,
                index_sets,
            ).build_transaction({
                'from': account.address,
                'nonce': nonce,
                'gas': 300000,
                'gasPrice': int(gas_price * 1.2),
            })
            
            signed_txn = w3.eth.account.sign_transaction(txn, self.settings.private_key)
            tx_hash = w3.eth.send_raw_transaction(signed_txn.raw_transaction)
            tx_hash_hex = tx_hash.hex()
            
            log.info("Redemption transaction sent", tx_hash=tx_hash_hex)
            
            receipt = await asyncio.wait_for(
                asyncio.to_thread(
                    lambda: w3.eth.wait_for_transaction_receipt(tx_hash, timeout=timeout_seconds)
                ),
                timeout=timeout_seconds + 5,
            )
            
            if receipt['status'] == 1:
                log.info(
                    "Position redeemed successfully",
                    tx_hash=tx_hash_hex,
                    gas_used=receipt['gasUsed'],
                )
                return {
                    "success": True,
                    "tx_hash": tx_hash_hex,
                    "gas_used": receipt['gasUsed'],
                }
            else:
                log.error("Redemption transaction failed", tx_hash=tx_hash_hex)
                return {
                    "success": False,
                    "tx_hash": tx_hash_hex,
                    "error": "Transaction reverted",
                }
                
        except asyncio.TimeoutError:
            log.error("Redemption transaction timed out")
            return {"success": False, "tx_hash": None, "error": "timeout"}
        except Exception as e:
            log.error("Failed to redeem position", error=str(e))
            return {"success": False, "tx_hash": None, "error": str(e)}

    async def rebalance_partial_fill(
        self,
        filled_token_id: str,
        unfilled_token_id: str,
        filled_shares: float,
        filled_price: float,
        unfilled_price: float,
        max_slippage_cents: float = 2.0,
        timeout_seconds: float = 10.0,
    ) -> Dict[str, Any]:
        """Rebalance a partial fill by completing the hedge or exiting.

        When one leg of an arbitrage fills but the other doesn't, we try to:
        1. FIRST: Buy the missing leg to complete the hedge (capture arb profit)
        2. FALLBACK: If that fails, sell the filled leg to exit (small spread loss)

        Phase 5 Fix (Dec 17, 2025): Rebalance partial fills to avoid unhedged exposure.

        Args:
            filled_token_id: Token ID that filled (e.g., YES)
            unfilled_token_id: Token ID that didn't fill (e.g., NO)
            filled_shares: Number of shares that filled
            filled_price: Price we paid for filled shares
            unfilled_price: Original target price for unfilled side
            max_slippage_cents: Max slippage to accept (default 2¢)
            timeout_seconds: Timeout for order execution

        Returns:
            Dict with 'success', 'action', 'shares_bought', 'shares_sold', 'pnl', etc.
        """
        from decimal import Decimal, ROUND_DOWN

        filled_cost = filled_shares * filled_price

        log.info(
            "PARTIAL FILL REBALANCE: Attempting to complete hedge",
            filled_token=filled_token_id[:20] + "...",
            unfilled_token=unfilled_token_id[:20] + "...",
            filled_shares=filled_shares,
            filled_price=f"${filled_price:.2f}",
            filled_cost=f"${filled_cost:.2f}",
        )

        try:
            # ============================================================
            # STEP 1: Try to buy the missing leg to complete the hedge
            # ============================================================
            unfilled_book = self.get_order_book(unfilled_token_id)

            # Handle both dict and OrderBookSummary object
            if hasattr(unfilled_book, "asks"):
                unfilled_asks = unfilled_book.asks or []
            else:
                unfilled_asks = unfilled_book.get("asks", [])

            if unfilled_asks:
                # Get best ask price
                def get_price(level):
                    if hasattr(level, "price"):
                        return float(level.price or 0)
                    return float(level.get("price", 0))

                def get_size(level):
                    if hasattr(level, "size"):
                        return float(level.size or 0)
                    return float(level.get("size", 0))

                best_ask = get_price(unfilled_asks[0])
                ask_size = get_size(unfilled_asks[0])

                # Add slippage buffer to improve fill rate
                slippage = max_slippage_cents / 100.0
                buy_price = min(best_ask + slippage, 0.99)

                # Check if buying at this price still makes sense for arbitrage
                # Total cost = filled_price + buy_price should be < 1.00
                total_cost_if_hedged = filled_price + buy_price
                potential_profit = 1.0 - total_cost_if_hedged

                log.info(
                    "Checking if hedge completion is profitable",
                    best_ask=f"${best_ask:.4f}",
                    buy_price_with_slippage=f"${buy_price:.4f}",
                    total_cost_if_hedged=f"${total_cost_if_hedged:.4f}",
                    potential_profit_per_share=f"${potential_profit:.4f}",
                    ask_size_available=ask_size,
                )

                # Only try to complete hedge if it's still profitable (or at least break-even)
                if potential_profit >= -0.02 and ask_size >= filled_shares * 0.5:  # Allow 2¢ loss, need 50% liquidity
                    log.info(
                        "ATTEMPTING HEDGE COMPLETION: Buying missing leg",
                        shares_to_buy=filled_shares,
                        buy_price=f"${buy_price:.2f}",
                    )

                    # Calculate order parameters
                    shares_d = Decimal(str(filled_shares)).quantize(Decimal("0.01"), rounding=ROUND_DOWN)
                    buy_price_d = Decimal(str(buy_price)).quantize(Decimal("0.01"), rounding=ROUND_DOWN)

                    order_args = OrderArgs(
                        token_id=unfilled_token_id,
                        price=float(buy_price_d),
                        size=float(shares_d),
                        side="BUY",
                    )

                    signed_order = self._client.create_order(order_args)
                    result = await asyncio.wait_for(
                        asyncio.to_thread(
                            lambda: self._client.post_order(signed_order, orderType=OrderType.GTC)
                        ),
                        timeout=timeout_seconds,
                    )

                    status = result.get("status", "").upper()

                    if status in ("MATCHED", "FILLED"):
                        # SUCCESS! We completed the hedge
                        buy_cost = float(shares_d * buy_price_d)
                        total_cost = filled_cost + buy_cost
                        expected_profit = (float(shares_d) * 1.0) - total_cost  # $1 payout - total cost

                        log.info(
                            "HEDGE COMPLETED SUCCESSFULLY!",
                            filled_shares=filled_shares,
                            filled_cost=f"${filled_cost:.2f}",
                            hedge_shares=float(shares_d),
                            hedge_cost=f"${buy_cost:.2f}",
                            total_cost=f"${total_cost:.2f}",
                            expected_profit=f"${expected_profit:.2f}",
                        )

                        return {
                            "success": True,
                            "action": "hedge_completed",
                            "filled_shares": filled_shares,
                            "hedge_shares": float(shares_d),
                            "filled_cost": filled_cost,
                            "hedge_cost": buy_cost,
                            "total_cost": total_cost,
                            "expected_profit": expected_profit,
                            "hedge_order": result,
                        }

                    elif status == "LIVE":
                        # Order went live, wait briefly
                        log.info("Hedge order went LIVE, waiting 2s...")
                        await asyncio.sleep(2.0)

                        order_id = result.get("id") or result.get("order_id")
                        if order_id:
                            try:
                                self._client.cancel(order_id)
                                log.info("Cancelled unfilled hedge order")
                            except Exception:
                                pass

                        log.warning("Hedge order did not fill, falling back to exit")
                    else:
                        log.warning("Hedge order rejected", status=status)
                else:
                    log.info(
                        "Hedge completion not viable",
                        reason="unprofitable or insufficient liquidity",
                        potential_profit=f"${potential_profit:.4f}",
                        ask_size=ask_size,
                        needed=filled_shares,
                    )
            else:
                log.warning("No asks available on unfilled side - cannot complete hedge")

            # ============================================================
            # STEP 2: FALLBACK - Sell the filled leg to exit
            # ============================================================
            log.info(
                "FALLBACK: Exiting filled position by selling",
                shares_to_sell=filled_shares,
            )

            filled_book = self.get_order_book(filled_token_id)

            # Handle both dict and OrderBookSummary object
            if hasattr(filled_book, "bids"):
                filled_bids = filled_book.bids or []
            else:
                filled_bids = filled_book.get("bids", [])

            if not filled_bids:
                log.error("No bids available - cannot exit position")
                return {
                    "success": False,
                    "action": "exit_failed",
                    "error": "No bids in order book for filled side",
                    "filled_shares": filled_shares,
                    "filled_cost": filled_cost,
                }

            def get_price(level):
                if hasattr(level, "price"):
                    return float(level.price or 0)
                return float(level.get("price", 0))

            best_bid = get_price(filled_bids[0])
            slippage = max_slippage_cents / 100.0
            sell_price = max(best_bid - slippage, 0.01)

            log.info(
                "Placing exit sell order",
                best_bid=f"${best_bid:.4f}",
                sell_price=f"${sell_price:.2f}",
                shares=filled_shares,
            )

            shares_d = Decimal(str(filled_shares)).quantize(Decimal("0.01"), rounding=ROUND_DOWN)
            sell_price_d = Decimal(str(sell_price)).quantize(Decimal("0.01"), rounding=ROUND_DOWN)

            order_args = OrderArgs(
                token_id=filled_token_id,
                price=float(sell_price_d),
                size=float(shares_d),
                side="SELL",
            )

            signed_order = self._client.create_order(order_args)
            result = await asyncio.wait_for(
                asyncio.to_thread(
                    lambda: self._client.post_order(signed_order, orderType=OrderType.GTC)
                ),
                timeout=timeout_seconds,
            )

            status = result.get("status", "").upper()

            if status in ("MATCHED", "FILLED"):
                exit_proceeds = float(shares_d * sell_price_d)
                pnl = exit_proceeds - filled_cost

                log.info(
                    "EXIT SUCCESSFUL: Position sold",
                    exit_proceeds=f"${exit_proceeds:.2f}",
                    filled_cost=f"${filled_cost:.2f}",
                    pnl=f"${pnl:.2f}",
                )

                return {
                    "success": True,
                    "action": "exited",
                    "filled_shares": filled_shares,
                    "exit_shares": float(shares_d),
                    "filled_cost": filled_cost,
                    "exit_proceeds": exit_proceeds,
                    "pnl": pnl,
                    "exit_order": result,
                }

            elif status == "LIVE":
                log.warning("Exit order went LIVE, waiting 2s...")
                await asyncio.sleep(2.0)

                order_id = result.get("id") or result.get("order_id")
                if order_id:
                    try:
                        self._client.cancel(order_id)
                        log.info("Cancelled unfilled exit order")
                    except Exception:
                        pass

                return {
                    "success": False,
                    "action": "exit_failed",
                    "error": "Exit order went LIVE, cancelled. Position still held.",
                    "filled_shares": filled_shares,
                    "filled_cost": filled_cost,
                }

            else:
                return {
                    "success": False,
                    "action": "exit_failed",
                    "error": f"Exit order status: {status}",
                    "filled_shares": filled_shares,
                    "filled_cost": filled_cost,
                }

        except asyncio.TimeoutError:
            log.error("Rebalance operation timed out")
            return {
                "success": False,
                "action": "timeout",
                "error": "Rebalance timed out",
                "filled_shares": filled_shares,
                "filled_cost": filled_cost,
            }
        except Exception as e:
            log.error("Failed to rebalance partial fill", error=str(e))
            return {
                "success": False,
                "action": "error",
                "error": str(e),
                "filled_shares": filled_shares,
                "filled_cost": filled_cost,
            }
