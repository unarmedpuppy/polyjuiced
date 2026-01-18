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

        Uses direct Gamma API queries with calculated timestamps instead of
        page scraping, providing real-time market data without lag.

        The 15-minute markets use time-based slugs like:
        - btc-updown-15m-{unix_timestamp}
        - eth-updown-15m-{unix_timestamp}

        Args:
            asset: Asset symbol (BTC, ETH)

        Returns:
            List of 15-minute markets with UP/DOWN token IDs
        """
        import json
        import time

        fifteen_min_markets = []
        asset_lower = asset.lower()

        try:
            # Calculate current and upcoming market slots
            # Markets are aligned to 15-minute boundaries (900 seconds)
            current_ts = int(time.time())
            slot_duration = 900  # 15 minutes in seconds

            # Generate timestamps for current slot and next 3 slots
            # This ensures we find upcoming tradeable markets
            slot_timestamps = []
            for i in range(4):  # Current + next 3 slots
                slot_ts = ((current_ts // slot_duration) + i) * slot_duration
                slot_timestamps.append(slot_ts)

            # Query each slot via Gamma API
            seen_condition_ids = set()

            for slot_ts in slot_timestamps:
                slug = f"{asset_lower}-updown-15m-{slot_ts}"

                try:
                    response = await self._client.get(
                        f"{self.base_url}/markets/slug/{slug}",
                        timeout=10.0,
                    )

                    if response.status_code != 200:
                        log.debug(f"Market not found for slot {slot_ts}", slug=slug)
                        continue

                    market = response.json()

                    # Skip if we've seen this market
                    condition_id = market.get("conditionId")
                    if not condition_id or condition_id in seen_condition_ids:
                        continue
                    seen_condition_ids.add(condition_id)

                    # Parse clobTokenIds (JSON string)
                    clob_token_ids_raw = market.get("clobTokenIds", "[]")
                    if isinstance(clob_token_ids_raw, str):
                        clob_token_ids = json.loads(clob_token_ids_raw)
                    else:
                        clob_token_ids = clob_token_ids_raw

                    # Parse outcomes (JSON string)
                    outcomes_raw = market.get("outcomes", "[]")
                    if isinstance(outcomes_raw, str):
                        outcomes = json.loads(outcomes_raw)
                    else:
                        outcomes = outcomes_raw

                    # Parse outcome prices (JSON string)
                    prices_raw = market.get("outcomePrices", "[]")
                    if isinstance(prices_raw, str):
                        outcome_prices = json.loads(prices_raw)
                    else:
                        outcome_prices = prices_raw

                    if len(clob_token_ids) < 2 or len(outcomes) < 2:
                        continue

                    # Map outcomes to token IDs ("Up" is index 0, "Down" is index 1)
                    up_token = None
                    down_token = None
                    up_price = None
                    down_price = None

                    for i, outcome in enumerate(outcomes):
                        outcome_lower = outcome.lower() if isinstance(outcome, str) else ""
                        if outcome_lower == "up":
                            up_token = clob_token_ids[i] if i < len(clob_token_ids) else None
                            up_price = float(outcome_prices[i]) if i < len(outcome_prices) else None
                        elif outcome_lower == "down":
                            down_token = clob_token_ids[i] if i < len(clob_token_ids) else None
                            down_price = float(outcome_prices[i]) if i < len(outcome_prices) else None

                    if not up_token or not down_token:
                        continue

                    # Parse end time
                    end_date_str = market.get("endDate", "")
                    end_ts = slot_ts  # Default to slot timestamp
                    if end_date_str:
                        try:
                            from datetime import datetime
                            # Parse ISO format: 2025-12-09T05:15:00Z
                            end_dt = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
                            end_ts = int(end_dt.timestamp())
                        except Exception:
                            pass

                    market_data = {
                        "condition_id": condition_id,
                        "question": market.get("question", ""),
                        "yes_token_id": up_token,  # "Up" maps to "Yes" for arbitrage
                        "no_token_id": down_token,  # "Down" maps to "No"
                        "up_price": up_price,
                        "down_price": down_price,
                        "end_timestamp": end_ts,
                        "active": market.get("active", True),
                        "accepting_orders": market.get("acceptingOrders", True),
                        "market_slug": slug,
                    }
                    fifteen_min_markets.append(market_data)

                except Exception as e:
                    log.debug(f"Error fetching market slot {slot_ts}", error=str(e))
                    continue

        except Exception as e:
            log.error(f"Error finding {asset} 15-min markets", error=str(e))

        log.info(
            f"Found {len(fifteen_min_markets)} active 15-minute {asset} markets",
            slots_checked=len(slot_timestamps) if 'slot_timestamps' in dir() else 0,
        )
        return fifteen_min_markets
