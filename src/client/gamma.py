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

        These markets are NOT available through the Gamma API.
        We scrape condition IDs from the Polymarket /crypto/15M page
        and fetch market data from the CLOB API.

        Args:
            asset: Asset symbol (BTC, ETH, SOL)

        Returns:
            List of 15-minute markets with UP/DOWN token IDs
        """
        from datetime import datetime, timezone

        fifteen_min_markets = []
        asset_lower = asset.lower()

        # Get current time and calculate upcoming 15-min windows
        now = datetime.now(timezone.utc)
        current_ts = int(now.timestamp())

        # Round down to the nearest 15-minute boundary (900 seconds = 15 min)
        base_ts = (current_ts // 900) * 900

        # Try current and next few windows
        timestamps_to_try = [
            base_ts + 900,   # Next window end
            base_ts + 1800,  # Window after
            base_ts + 2700,  # Two windows ahead
        ]

        for end_ts in timestamps_to_try:
            market_slug = f"{asset_lower}-updown-15m-{end_ts}"

            try:
                # Fetch market from CLOB API by slug
                # The CLOB doesn't support slug search, so we try to get
                # condition IDs from the Polymarket page
                market = await self._fetch_15min_market_from_clob(market_slug, end_ts)
                if market:
                    fifteen_min_markets.append(market)
            except Exception as e:
                log.debug(f"Market {market_slug} not found: {e}")
                continue

        log.info(
            f"Found {len(fifteen_min_markets)} active 15-minute {asset} markets"
        )
        return fifteen_min_markets

    async def _fetch_15min_market_from_clob(
        self,
        market_slug: str,
        end_ts: int,
    ) -> Optional[Dict[str, Any]]:
        """Fetch a 15-minute market from CLOB API.

        Since CLOB doesn't support slug-based search, we scrape the
        Polymarket /crypto/15M page to find condition IDs.

        Args:
            market_slug: Expected market slug (e.g., btc-updown-15m-1234567890)
            end_ts: Unix timestamp of market end time

        Returns:
            Market data dict or None
        """
        try:
            # Try to fetch the crypto/15M page to find condition IDs
            response = await self._client.get(
                "https://polymarket.com/crypto/15M",
                headers={"User-Agent": "Mozilla/5.0"},
                follow_redirects=True,
            )

            if response.status_code != 200:
                return None

            html = response.text

            # Find condition IDs associated with this market slug
            # The page contains JSON data with condition_id references
            import re

            # Look for the market slug and nearby condition IDs
            # Pattern: condition_id followed by hex string
            slug_pattern = re.escape(market_slug)

            # Find all condition IDs in the page
            condition_ids = re.findall(r'0x[a-f0-9]{64}', html)

            # Also look for the market slug to see if it's present
            if market_slug not in html:
                return None

            # Try to find the condition ID associated with this slug
            # by looking for it in the context around the slug
            slug_idx = html.find(market_slug)
            if slug_idx == -1:
                return None

            # Search in a window around the slug mention
            window_start = max(0, slug_idx - 2000)
            window_end = min(len(html), slug_idx + 2000)
            window = html[window_start:window_end]

            # Find condition IDs in this window
            window_condition_ids = re.findall(r'0x[a-f0-9]{64}', window)

            for condition_id in window_condition_ids:
                # Try to fetch this market from CLOB
                try:
                    clob_response = await self._client.get(
                        f"https://clob.polymarket.com/markets/{condition_id}"
                    )
                    if clob_response.status_code == 200:
                        market_data = clob_response.json()

                        # Verify this is the right market
                        if market_data.get("market_slug") == market_slug:
                            # Check if still accepting orders
                            if not market_data.get("accepting_orders", False):
                                continue

                            tokens = market_data.get("tokens", [])
                            if len(tokens) >= 2:
                                up_token = None
                                down_token = None
                                for token in tokens:
                                    outcome = token.get("outcome", "").lower()
                                    if outcome == "up":
                                        up_token = token.get("token_id")
                                    elif outcome == "down":
                                        down_token = token.get("token_id")

                                if up_token and down_token:
                                    return {
                                        "condition_id": condition_id,
                                        "question": market_data.get("question"),
                                        "yes_token_id": up_token,
                                        "no_token_id": down_token,
                                        "end_date": market_data.get("end_date_iso"),
                                        "end_timestamp": end_ts,
                                        "active": market_data.get("active", True),
                                        "accepting_orders": market_data.get("accepting_orders", False),
                                        "market_slug": market_slug,
                                    }
                except Exception:
                    continue

            return None

        except Exception as e:
            log.debug(f"Error fetching 15min market {market_slug}: {e}")
            return None
