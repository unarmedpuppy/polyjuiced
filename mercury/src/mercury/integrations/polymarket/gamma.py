"""Polymarket Gamma API client for market discovery.

The Gamma API provides market metadata, pricing, and search functionality.
It is separate from the CLOB API which handles order execution.
"""

import json
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

import httpx
import structlog
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from mercury.integrations.polymarket.types import (
    Market15Min,
    MarketInfo,
    PolymarketSettings,
)

log = structlog.get_logger()

# Retry configuration for transient errors
RETRY_ATTEMPTS = 3
RETRY_WAIT_MIN = 1
RETRY_WAIT_MAX = 10


class GammaClientError(Exception):
    """Error from Gamma API client."""

    pass


class GammaClient:
    """Async HTTP client for Polymarket Gamma API.

    The Gamma API provides:
    - Market listing and search
    - Event (market group) information
    - User position and trade history
    - 15-minute market discovery for arbitrage

    All methods are async and include retry logic for transient failures.
    """

    def __init__(
        self,
        settings: PolymarketSettings,
        timeout: float = 30.0,
    ):
        """Initialize the Gamma client.

        Args:
            settings: Polymarket connection settings.
            timeout: HTTP request timeout in seconds.
        """
        self._base_url = settings.gamma_url.rstrip("/")
        self._timeout = timeout
        self._proxy = settings.http_proxy
        self._client: Optional[httpx.AsyncClient] = None
        self._log = log.bind(component="gamma_client")

    async def connect(self) -> None:
        """Initialize the HTTP client."""
        if self._client is not None:
            return

        transport = None
        if self._proxy:
            transport = httpx.AsyncHTTPTransport(proxy=self._proxy)

        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            timeout=self._timeout,
            transport=transport,
            headers={"Accept": "application/json"},
        )
        self._log.info("gamma_client_connected", base_url=self._base_url)

    async def close(self) -> None:
        """Close the HTTP client."""
        if self._client is not None:
            await self._client.aclose()
            self._client = None
            self._log.info("gamma_client_closed")

    async def __aenter__(self) -> "GammaClient":
        """Async context manager entry."""
        await self.connect()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        """Async context manager exit."""
        await self.close()

    def _ensure_connected(self) -> httpx.AsyncClient:
        """Ensure client is connected and return it."""
        if self._client is None:
            raise GammaClientError("Client not connected. Call connect() first.")
        return self._client

    @retry(
        stop=stop_after_attempt(RETRY_ATTEMPTS),
        wait=wait_exponential(multiplier=1, min=RETRY_WAIT_MIN, max=RETRY_WAIT_MAX),
        retry=retry_if_exception_type((httpx.TransportError, httpx.TimeoutException)),
    )
    async def get_markets(
        self,
        limit: int = 100,
        offset: int = 0,
        active: bool = True,
    ) -> list[dict]:
        """Get a list of markets.

        Args:
            limit: Maximum number of markets to return.
            offset: Pagination offset.
            active: If True, only return active (trading) markets.

        Returns:
            List of market dictionaries from the API.
        """
        client = self._ensure_connected()
        params = {"limit": limit, "offset": offset}
        if active:
            params["active"] = "true"

        response = await client.get("/markets", params=params)
        response.raise_for_status()
        return response.json()

    @retry(
        stop=stop_after_attempt(RETRY_ATTEMPTS),
        wait=wait_exponential(multiplier=1, min=RETRY_WAIT_MIN, max=RETRY_WAIT_MAX),
        retry=retry_if_exception_type((httpx.TransportError, httpx.TimeoutException)),
    )
    async def get_market(self, condition_id: str) -> Optional[dict]:
        """Get a single market by condition ID.

        Args:
            condition_id: The market's condition ID.

        Returns:
            Market dictionary or None if not found.
        """
        client = self._ensure_connected()
        try:
            response = await client.get(f"/markets/{condition_id}")
            if response.status_code == 404:
                return None
            response.raise_for_status()
            return response.json()
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                return None
            raise

    @retry(
        stop=stop_after_attempt(RETRY_ATTEMPTS),
        wait=wait_exponential(multiplier=1, min=RETRY_WAIT_MIN, max=RETRY_WAIT_MAX),
        retry=retry_if_exception_type((httpx.TransportError, httpx.TimeoutException)),
    )
    async def search_markets(self, query: str, limit: int = 20) -> list[dict]:
        """Search markets by query string.

        Args:
            query: Search query (matches against question text).
            limit: Maximum number of results.

        Returns:
            List of matching market dictionaries.
        """
        client = self._ensure_connected()
        response = await client.get(
            "/markets",
            params={"_q": query, "limit": limit}
        )
        response.raise_for_status()
        return response.json()

    @retry(
        stop=stop_after_attempt(RETRY_ATTEMPTS),
        wait=wait_exponential(multiplier=1, min=RETRY_WAIT_MIN, max=RETRY_WAIT_MAX),
        retry=retry_if_exception_type((httpx.TransportError, httpx.TimeoutException)),
    )
    async def get_events(self, limit: int = 100, active: bool = True) -> list[dict]:
        """Get market events (groups of related markets).

        Args:
            limit: Maximum number of events to return.
            active: If True, only return active events.

        Returns:
            List of event dictionaries.
        """
        client = self._ensure_connected()
        params = {"limit": limit}
        if active:
            params["active"] = "true"

        response = await client.get("/events", params=params)
        response.raise_for_status()
        return response.json()

    @retry(
        stop=stop_after_attempt(RETRY_ATTEMPTS),
        wait=wait_exponential(multiplier=1, min=RETRY_WAIT_MIN, max=RETRY_WAIT_MAX),
        retry=retry_if_exception_type((httpx.TransportError, httpx.TimeoutException)),
    )
    async def get_event(self, event_slug: str) -> Optional[dict]:
        """Get a single event by slug.

        Args:
            event_slug: The event's slug identifier.

        Returns:
            Event dictionary or None if not found.
        """
        client = self._ensure_connected()
        try:
            response = await client.get(f"/events/{event_slug}")
            if response.status_code == 404:
                return None
            response.raise_for_status()
            return response.json()
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                return None
            raise

    @retry(
        stop=stop_after_attempt(RETRY_ATTEMPTS),
        wait=wait_exponential(multiplier=1, min=RETRY_WAIT_MIN, max=RETRY_WAIT_MAX),
        retry=retry_if_exception_type((httpx.TransportError, httpx.TimeoutException)),
    )
    async def get_user_positions(self, wallet_address: str) -> list[dict]:
        """Get positions for a wallet address.

        Args:
            wallet_address: Ethereum/Polygon wallet address.

        Returns:
            List of position dictionaries.
        """
        client = self._ensure_connected()
        response = await client.get(
            f"/users/{wallet_address}/positions"
        )
        response.raise_for_status()
        return response.json()

    @retry(
        stop=stop_after_attempt(RETRY_ATTEMPTS),
        wait=wait_exponential(multiplier=1, min=RETRY_WAIT_MIN, max=RETRY_WAIT_MAX),
        retry=retry_if_exception_type((httpx.TransportError, httpx.TimeoutException)),
    )
    async def get_user_trades(
        self,
        wallet_address: str,
        limit: int = 100,
    ) -> list[dict]:
        """Get trade history for a wallet address.

        Args:
            wallet_address: Ethereum/Polygon wallet address.
            limit: Maximum number of trades to return.

        Returns:
            List of trade dictionaries.
        """
        client = self._ensure_connected()
        response = await client.get(
            f"/users/{wallet_address}/trades",
            params={"limit": limit}
        )
        response.raise_for_status()
        return response.json()

    async def find_15min_markets(self, asset: str) -> list[Market15Min]:
        """Find active 15-minute up/down markets for an asset.

        15-minute markets have a predictable slug pattern based on
        Unix timestamps at 900-second (15-minute) boundaries.

        Args:
            asset: Asset symbol (e.g., "BTC", "ETH", "SOL").

        Returns:
            List of Market15Min objects for active markets.
        """
        now = datetime.now(timezone.utc)
        current_slot = int(now.timestamp()) // 900 * 900

        # Look at current slot and next 3 slots
        slot_timestamps = [current_slot + (i * 900) for i in range(4)]

        markets = []
        for slot_ts in slot_timestamps:
            slug = f"{asset.lower()}-updown-15m-{slot_ts}"
            market = await self._fetch_15min_market(slug, asset, slot_ts)
            if market is not None:
                markets.append(market)

        self._log.debug(
            "found_15min_markets",
            asset=asset,
            count=len(markets),
        )
        return markets

    async def _fetch_15min_market(
        self,
        slug: str,
        asset: str,
        end_timestamp: int,
    ) -> Optional[Market15Min]:
        """Fetch a single 15-minute market by slug.

        Args:
            slug: Market slug (e.g., "btc-updown-15m-1737158400").
            asset: Asset symbol.
            end_timestamp: Unix timestamp when market ends.

        Returns:
            Market15Min object or None if not found/invalid.
        """
        client = self._ensure_connected()

        try:
            response = await client.get(
                "/markets",
                params={"slug": slug, "limit": 1}
            )
            response.raise_for_status()
            data = response.json()

            if not data:
                return None

            market = data[0]

            # Parse token IDs from JSON string
            clob_token_ids = json.loads(market.get("clobTokenIds", "[]"))
            outcomes = json.loads(market.get("outcomes", "[]"))
            outcome_prices = json.loads(market.get("outcomePrices", "[]"))

            if len(clob_token_ids) < 2 or len(outcomes) < 2:
                self._log.warning(
                    "invalid_15min_market",
                    slug=slug,
                    tokens=len(clob_token_ids),
                )
                return None

            # Map outcomes to token IDs: "Up" -> YES, "Down" -> NO
            yes_token_id = None
            no_token_id = None
            yes_price = Decimal("0")
            no_price = Decimal("0")

            for i, outcome in enumerate(outcomes):
                token_id = str(clob_token_ids[i])  # Convert to string for precision
                price = Decimal(str(outcome_prices[i])) if i < len(outcome_prices) else Decimal("0")

                if outcome.lower() == "up":
                    yes_token_id = token_id
                    yes_price = price
                elif outcome.lower() == "down":
                    no_token_id = token_id
                    no_price = price

            if yes_token_id is None or no_token_id is None:
                self._log.warning(
                    "missing_tokens_15min_market",
                    slug=slug,
                    outcomes=outcomes,
                )
                return None

            # Calculate times
            end_time = datetime.fromtimestamp(end_timestamp, tz=timezone.utc)
            start_time = datetime.fromtimestamp(end_timestamp - 900, tz=timezone.utc)

            return Market15Min(
                condition_id=market.get("conditionId", ""),
                asset=asset.upper(),
                yes_token_id=yes_token_id,
                no_token_id=no_token_id,
                yes_price=yes_price,
                no_price=no_price,
                start_time=start_time,
                end_time=end_time,
                slug=slug,
            )

        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                return None
            raise
        except (json.JSONDecodeError, KeyError, IndexError) as e:
            self._log.warning(
                "parse_error_15min_market",
                slug=slug,
                error=str(e),
            )
            return None

    def parse_market_info(self, data: dict) -> MarketInfo:
        """Parse a market dictionary into a MarketInfo object.

        Args:
            data: Raw market dictionary from the API.

        Returns:
            Parsed MarketInfo object.
        """
        # Parse token IDs from JSON string
        clob_token_ids = json.loads(data.get("clobTokenIds", "[]"))
        outcomes = json.loads(data.get("outcomes", "[]"))
        outcome_prices = json.loads(data.get("outcomePrices", "[]"))

        # Map to YES/NO
        yes_token_id = ""
        no_token_id = ""
        yes_price = Decimal("0")
        no_price = Decimal("0")

        for i, outcome in enumerate(outcomes):
            token_id = str(clob_token_ids[i]) if i < len(clob_token_ids) else ""
            price = Decimal(str(outcome_prices[i])) if i < len(outcome_prices) else Decimal("0")

            if outcome.lower() in ("yes", "up"):
                yes_token_id = token_id
                yes_price = price
            elif outcome.lower() in ("no", "down"):
                no_token_id = token_id
                no_price = price

        # Parse end date
        end_date = None
        if data.get("endDate"):
            try:
                end_date = datetime.fromisoformat(data["endDate"].replace("Z", "+00:00"))
            except (ValueError, TypeError):
                pass

        return MarketInfo(
            condition_id=data.get("conditionId", ""),
            question_id=data.get("questionId", ""),
            question=data.get("question", ""),
            slug=data.get("slug", ""),
            yes_token_id=yes_token_id,
            no_token_id=no_token_id,
            yes_price=yes_price,
            no_price=no_price,
            active=data.get("active", False),
            closed=data.get("closed", False),
            resolved=data.get("resolved", False),
            resolution=data.get("resolution"),
            end_date=end_date,
            volume=Decimal(str(data.get("volume", 0))),
            liquidity=Decimal(str(data.get("liquidity", 0))),
            event_slug=data.get("eventSlug"),
            event_title=data.get("eventTitle"),
        )
