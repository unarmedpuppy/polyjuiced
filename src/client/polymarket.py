"""Polymarket CLOB client wrapper."""

import asyncio
from decimal import Decimal
from typing import Any, Dict, List, Optional

import structlog
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds, OrderArgs, OrderType
from tenacity import retry, stop_after_attempt, wait_exponential

from ..config import PolymarketSettings

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

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
    )
    def create_market_order(
        self,
        token_id: str,
        amount_usd: float,
        side: str,
    ) -> Dict[str, Any]:
        """Execute a market order.

        Args:
            token_id: The YES or NO token ID
            amount_usd: Dollar amount to spend
            side: "BUY" or "SELL"

        Returns:
            Order result dictionary
        """
        self._ensure_connected()
        log.info(
            "Placing market order",
            token_id=token_id,
            amount=amount_usd,
            side=side,
        )
        return self._client.create_market_order(
            token_id=token_id,
            amount=amount_usd,
            side=side.upper(),
        )

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

        return self._client.create_order(order_args)

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
    # Dual-Leg Execution (for Gabagool strategy)
    # =========================================================================

    async def execute_dual_leg_order(
        self,
        yes_token_id: str,
        no_token_id: str,
        yes_amount_usd: float,
        no_amount_usd: float,
        timeout_seconds: float = 0.5,
    ) -> Dict[str, Any]:
        """Execute YES and NO orders concurrently for arbitrage.

        Args:
            yes_token_id: YES token ID
            no_token_id: NO token ID
            yes_amount_usd: Amount to spend on YES
            no_amount_usd: Amount to spend on NO
            timeout_seconds: Timeout for order placement

        Returns:
            Dict with 'yes_order', 'no_order', 'success' keys
        """
        log.info(
            "Executing dual-leg order",
            yes_amount=yes_amount_usd,
            no_amount=no_amount_usd,
        )

        async def place_yes():
            return self.create_market_order(yes_token_id, yes_amount_usd, "BUY")

        async def place_no():
            return self.create_market_order(no_token_id, no_amount_usd, "BUY")

        try:
            # Execute both orders concurrently with timeout
            yes_result, no_result = await asyncio.wait_for(
                asyncio.gather(
                    asyncio.to_thread(place_yes),
                    asyncio.to_thread(place_no),
                ),
                timeout=timeout_seconds,
            )

            return {
                "yes_order": yes_result,
                "no_order": no_result,
                "success": True,
            }

        except asyncio.TimeoutError:
            log.error("Dual-leg order timed out")
            # Try to cancel any pending orders
            try:
                self.cancel_all_orders()
            except Exception:
                pass
            return {
                "yes_order": None,
                "no_order": None,
                "success": False,
                "error": "timeout",
            }

        except Exception as e:
            log.error("Dual-leg order failed", error=str(e))
            return {
                "yes_order": None,
                "no_order": None,
                "success": False,
                "error": str(e),
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

    def get_market_resolution(self, condition_id: str) -> Optional[Dict[str, Any]]:
        """Get resolution status for a market.

        Args:
            condition_id: The market condition ID

        Returns:
            Resolution data if resolved, None if not yet resolved
        """
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
