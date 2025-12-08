"""Gamma API client for market metadata."""

from typing import Any, Dict, List, Optional

import httpx
import structlog

log = structlog.get_logger()


class GammaClient:
    """Client for Polymarket's Gamma API (market metadata)."""

    def __init__(self, base_url: str = "https://gamma-api.polymarket.com"):
        """Initialize the Gamma client.

        Args:
            base_url: Gamma API base URL
        """
        self.base_url = base_url.rstrip("/")
        self._client = httpx.AsyncClient(timeout=30.0)

    async def close(self) -> None:
        """Close the HTTP client."""
        await self._client.aclose()

    async def get_markets(
        self,
        limit: int = 100,
        offset: int = 0,
        active: bool = True,
    ) -> List[Dict[str, Any]]:
        """Get list of markets.

        Args:
            limit: Maximum number of markets to return
            offset: Pagination offset
            active: Only return active markets

        Returns:
            List of market data
        """
        params = {
            "limit": limit,
            "offset": offset,
            "active": str(active).lower(),
        }

        response = await self._client.get(
            f"{self.base_url}/markets",
            params=params,
        )
        response.raise_for_status()
        return response.json()

    async def get_market(self, condition_id: str) -> Optional[Dict[str, Any]]:
        """Get specific market by condition ID.

        Args:
            condition_id: Market condition ID

        Returns:
            Market data or None if not found
        """
        try:
            response = await self._client.get(
                f"{self.base_url}/markets/{condition_id}"
            )
            response.raise_for_status()
            return response.json()
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                return None
            raise

    async def search_markets(
        self,
        query: str,
        limit: int = 20,
    ) -> List[Dict[str, Any]]:
        """Search markets by query string.

        Args:
            query: Search query
            limit: Maximum results

        Returns:
            List of matching markets
        """
        params = {
            "q": query,
            "limit": limit,
        }

        response = await self._client.get(
            f"{self.base_url}/markets",
            params=params,
        )
        response.raise_for_status()
        return response.json()

    async def get_events(
        self,
        limit: int = 100,
        active: bool = True,
    ) -> List[Dict[str, Any]]:
        """Get list of events (groups of related markets).

        Args:
            limit: Maximum number of events
            active: Only return active events

        Returns:
            List of event data
        """
        params = {
            "limit": limit,
            "active": str(active).lower(),
        }

        response = await self._client.get(
            f"{self.base_url}/events",
            params=params,
        )
        response.raise_for_status()
        return response.json()

    async def get_event(self, event_slug: str) -> Optional[Dict[str, Any]]:
        """Get specific event by slug.

        Args:
            event_slug: Event slug/ID

        Returns:
            Event data or None
        """
        try:
            response = await self._client.get(
                f"{self.base_url}/events/{event_slug}"
            )
            response.raise_for_status()
            return response.json()
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                return None
            raise

    async def get_user_positions(
        self,
        wallet_address: str,
    ) -> List[Dict[str, Any]]:
        """Get positions for a specific wallet address.

        Args:
            wallet_address: Ethereum/Polygon wallet address

        Returns:
            List of positions
        """
        response = await self._client.get(
            f"{self.base_url}/users/{wallet_address}/positions"
        )
        response.raise_for_status()
        return response.json()

    async def get_user_trades(
        self,
        wallet_address: str,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        """Get trade history for a specific wallet.

        Args:
            wallet_address: Ethereum/Polygon wallet address
            limit: Maximum trades to return

        Returns:
            List of trades
        """
        params = {"limit": limit}

        response = await self._client.get(
            f"{self.base_url}/users/{wallet_address}/trades",
            params=params,
        )
        response.raise_for_status()
        return response.json()

    async def find_15min_markets(
        self,
        asset: str = "BTC",
    ) -> List[Dict[str, Any]]:
        """Find active 15-minute up/down markets for an asset.

        Args:
            asset: Asset symbol (BTC, ETH)

        Returns:
            List of 15-minute markets with YES/NO token IDs
        """
        # Search for 15-minute markets
        query = f"{asset} Up or Down"
        markets = await self.search_markets(query, limit=50)

        # Filter to only include 15-minute markets
        fifteen_min_markets = []
        for market in markets:
            title = market.get("question", "").lower()
            # Check if it's a 15-minute market
            if "15" in title and ("minute" in title or "min" in title):
                # Extract token IDs
                tokens = market.get("tokens", [])
                if len(tokens) >= 2:
                    yes_token = None
                    no_token = None
                    for token in tokens:
                        outcome = token.get("outcome", "").upper()
                        if outcome in ["YES", "UP"]:
                            yes_token = token.get("token_id")
                        elif outcome in ["NO", "DOWN"]:
                            no_token = token.get("token_id")

                    if yes_token and no_token:
                        fifteen_min_markets.append({
                            "condition_id": market.get("condition_id"),
                            "question": market.get("question"),
                            "yes_token_id": yes_token,
                            "no_token_id": no_token,
                            "end_date": market.get("end_date_iso"),
                            "active": market.get("active", True),
                        })

        log.info(
            f"Found {len(fifteen_min_markets)} active 15-minute {asset} markets"
        )
        return fifteen_min_markets
