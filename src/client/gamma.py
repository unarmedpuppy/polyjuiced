"""Gamma API client for market metadata."""

from typing import Any, Dict, List, Optional

import httpx
import structlog

log = structlog.get_logger()


class GammaClient:
    """Client for Polymarket's Gamma API (market metadata)."""

    def __init__(
        self,
        base_url: str = "https://gamma-api.polymarket.com",
        http_proxy: Optional[str] = None,
    ):
        """Initialize the Gamma client.

        Args:
            base_url: Gamma API base URL
            http_proxy: Optional HTTP proxy URL (e.g., http://gluetun:8888)
        """
        self.base_url = base_url.rstrip("/")
        self.http_proxy = http_proxy

        # Configure proxy if provided
        proxy_config = http_proxy if http_proxy else None
        self._client = httpx.AsyncClient(timeout=30.0, proxy=proxy_config)

        if http_proxy:
            log.info("GammaClient using HTTP proxy", proxy=http_proxy)

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

        The 15-minute markets use time-based slugs like:
        - btc-updown-15m-{unix_timestamp}
        - eth-updown-15m-{unix_timestamp}

        These markets are embedded in the Polymarket /crypto/15M page's
        __NEXT_DATA__ JSON, not available through standard APIs.

        Args:
            asset: Asset symbol (BTC, ETH, SOL, XRP)

        Returns:
            List of 15-minute markets with UP/DOWN token IDs
        """
        import json
        import re

        fifteen_min_markets = []
        asset_lower = asset.lower()

        try:
            # Fetch the crypto/15M page
            response = await self._client.get(
                "https://polymarket.com/crypto/15M",
                headers={"User-Agent": "Mozilla/5.0 (compatible; PolymarketBot/1.0)"},
                follow_redirects=True,
            )

            if response.status_code != 200:
                log.warning(f"Failed to fetch /crypto/15M page: {response.status_code}")
                return []

            html = response.text

            # Extract __NEXT_DATA__ JSON from the page
            next_data_match = re.search(
                r'<script id="__NEXT_DATA__"[^>]*>([^<]+)</script>',
                html
            )

            if not next_data_match:
                log.warning("Could not find __NEXT_DATA__ in page")
                return []

            try:
                next_data = json.loads(next_data_match.group(1))
            except json.JSONDecodeError as e:
                log.warning(f"Failed to parse __NEXT_DATA__: {e}")
                return []

            # Navigate to the queries containing market data
            queries = (
                next_data.get("props", {})
                .get("pageProps", {})
                .get("dehydratedState", {})
                .get("queries", [])
            )

            # Track seen condition IDs to avoid duplicates
            seen_condition_ids = set()

            for query in queries:
                state = query.get("state", {})
                if not isinstance(state, dict):
                    continue
                data_obj = state.get("data", {})
                if not isinstance(data_obj, dict):
                    continue
                pages = data_obj.get("pages", [])
                if not isinstance(pages, list):
                    continue
                for page in pages:
                    if not isinstance(page, dict):
                        continue
                    events = page.get("events", [])
                    if not isinstance(events, list):
                        continue
                    for event in events:
                        if not isinstance(event, dict):
                            continue
                        slug = event.get("slug", "")

                        # Filter for the requested asset's 15m markets
                        if not slug.startswith(f"{asset_lower}-updown-15m-"):
                            continue

                        for market in event.get("markets", []):
                            condition_id = market.get("conditionId")

                            # Skip duplicates
                            if not condition_id or condition_id in seen_condition_ids:
                                continue
                            seen_condition_ids.add(condition_id)

                            # Extract token IDs (Up, Down)
                            clob_token_ids = market.get("clobTokenIds", [])
                            outcomes = market.get("outcomes", [])
                            outcome_prices = market.get("outcomePrices", [])

                            if len(clob_token_ids) < 2 or len(outcomes) < 2:
                                continue

                            # Map outcomes to token IDs
                            up_token = None
                            down_token = None
                            up_price = None
                            down_price = None

                            for i, outcome in enumerate(outcomes):
                                outcome_lower = outcome.lower()
                                if outcome_lower == "up":
                                    up_token = clob_token_ids[i]
                                    up_price = float(outcome_prices[i]) if i < len(outcome_prices) else None
                                elif outcome_lower == "down":
                                    down_token = clob_token_ids[i]
                                    down_price = float(outcome_prices[i]) if i < len(outcome_prices) else None

                            if not up_token or not down_token:
                                continue

                            # Extract timestamp from slug (e.g., btc-updown-15m-1765226700)
                            try:
                                end_ts = int(slug.split("-")[-1])
                            except (ValueError, IndexError):
                                end_ts = None

                            market_data = {
                                "condition_id": condition_id,
                                "question": market.get("question", event.get("title", "")),
                                "yes_token_id": up_token,  # "Up" maps to "Yes" for arbitrage
                                "no_token_id": down_token,  # "Down" maps to "No"
                                "up_price": up_price,
                                "down_price": down_price,
                                "end_timestamp": end_ts,
                                "active": True,
                                "accepting_orders": True,  # Assume true if on page
                                "market_slug": slug,
                            }
                            fifteen_min_markets.append(market_data)

        except Exception as e:
            log.error(f"Error finding {asset} 15-min markets", error=str(e))

        log.info(
            f"Found {len(fifteen_min_markets)} active 15-minute {asset} markets"
        )
        return fifteen_min_markets
