"""Market finder for 15-minute BTC/ETH up/down markets."""

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Dict, List, Optional

import structlog

from ..client.gamma import GammaClient

if TYPE_CHECKING:
    from ..persistence import Database

log = structlog.get_logger()


@dataclass
class Market15Min:
    """Represents a 15-minute up/down market."""

    condition_id: str
    question: str
    asset: str  # "BTC" or "ETH"
    yes_token_id: str
    no_token_id: str
    start_time: datetime
    end_time: datetime
    active: bool = True

    @property
    def time_remaining(self) -> timedelta:
        """Time remaining until market resolution."""
        return self.end_time - datetime.utcnow()

    @property
    def seconds_remaining(self) -> float:
        """Seconds remaining until market resolution."""
        return max(0, self.time_remaining.total_seconds())

    @property
    def is_tradeable(self) -> bool:
        """Check if market is still tradeable (> 60 seconds remaining)."""
        return self.active and self.seconds_remaining > 60


class MarketFinder:
    """Finds and tracks active 15-minute up/down markets."""

    def __init__(self, gamma_client: GammaClient, db: Optional["Database"] = None):
        """Initialize market finder.

        Args:
            gamma_client: Gamma API client
            db: Optional database for persisting market history
        """
        self.gamma = gamma_client
        self._db = db
        self._cache: Dict[str, Market15Min] = {}
        self._last_refresh: Optional[datetime] = None
        self._cache_ttl = timedelta(minutes=1)

    @property
    def all_discovered_markets(self) -> List[Market15Min]:
        """Return all discovered markets (including expired ones).

        This is useful for dashboard display to show market status.
        """
        return list(self._cache.values())

    async def find_active_markets(
        self,
        assets: List[str] = None,
    ) -> List[Market15Min]:
        """Find all active 15-minute markets.

        Args:
            assets: List of assets to search for (default: ["BTC", "ETH"])

        Returns:
            List of active 15-minute markets
        """
        if assets is None:
            assets = ["BTC", "ETH"]

        # Check cache freshness
        now = datetime.utcnow()
        if (
            self._last_refresh
            and now - self._last_refresh < self._cache_ttl
        ):
            # Return cached markets that are still active
            return [m for m in self._cache.values() if m.is_tradeable]

        # Refresh market list
        all_markets = []
        for asset in assets:
            markets = await self._find_markets_for_asset(asset)
            all_markets.extend(markets)

        # Update cache
        self._cache = {m.condition_id: m for m in all_markets}
        self._last_refresh = now

        # Persist newly discovered markets to database
        if self._db:
            for market in all_markets:
                try:
                    await self._db.save_market(
                        condition_id=market.condition_id,
                        question=market.question,
                        asset=market.asset,
                        start_time=market.start_time,
                        end_time=market.end_time,
                        yes_token_id=market.yes_token_id,
                        no_token_id=market.no_token_id,
                    )
                except Exception as e:
                    log.debug("Failed to save market to DB", error=str(e))

        log.info(
            "Refreshed market cache",
            total_markets=len(all_markets),
            tradeable=len([m for m in all_markets if m.is_tradeable]),
        )

        return [m for m in all_markets if m.is_tradeable]

    async def _find_markets_for_asset(self, asset: str) -> List[Market15Min]:
        """Find 15-minute markets for a specific asset.

        Args:
            asset: Asset symbol (BTC, ETH)

        Returns:
            List of markets for this asset
        """
        markets = []

        try:
            # Search for 15-minute markets
            raw_markets = await self.gamma.find_15min_markets(asset)

            for raw in raw_markets:
                market = self._parse_market(raw, asset)
                if market:
                    markets.append(market)

        except Exception as e:
            log.error(f"Error finding {asset} 15-min markets", error=str(e))

        return markets

    def _parse_market(
        self,
        raw: Dict,
        asset: str,
    ) -> Optional[Market15Min]:
        """Parse raw market data into Market15Min.

        Args:
            raw: Raw market data from Gamma API / page scrape
            asset: Asset symbol

        Returns:
            Market15Min instance or None if parsing fails
        """
        try:
            question = raw.get("question", "")
            end_timestamp = raw.get("end_timestamp")

            # If we have an end_timestamp, use it directly
            if end_timestamp:
                end_time = datetime.utcfromtimestamp(end_timestamp)
                # 15-minute markets start 15 minutes before end
                start_time = end_time - timedelta(minutes=15)
            else:
                # Fall back to parsing from question string
                times = self._parse_time_from_question(question)
                if not times:
                    return None
                start_time, end_time = times

            return Market15Min(
                condition_id=raw.get("condition_id", ""),
                question=question,
                asset=asset,
                yes_token_id=raw.get("yes_token_id", ""),
                no_token_id=raw.get("no_token_id", ""),
                start_time=start_time,
                end_time=end_time,
                active=raw.get("active", True),
            )

        except Exception as e:
            log.warning("Failed to parse market", error=str(e), raw=raw)
            return None

    def _parse_time_from_question(
        self,
        question: str,
    ) -> Optional[tuple]:
        """Parse start/end times from market question.

        Args:
            question: Market question string

        Returns:
            Tuple of (start_time, end_time) or None
        """
        # Pattern: "December 7, 3:00AM-3:15AM ET"
        # or "December 7, 10:30PM-10:45PM ET"
        pattern = r"(\w+ \d+),?\s*(\d{1,2}:\d{2}(?:AM|PM))-(\d{1,2}:\d{2}(?:AM|PM))\s*ET"
        match = re.search(pattern, question, re.IGNORECASE)

        if not match:
            return None

        try:
            date_str = match.group(1)
            start_str = match.group(2)
            end_str = match.group(3)

            # Get current year
            year = datetime.utcnow().year

            # Parse date
            # Add year to date string
            full_date = f"{date_str} {year}"
            date_parsed = datetime.strptime(full_date, "%B %d %Y")

            # Parse times
            start_time = datetime.strptime(start_str, "%I:%M%p")
            end_time = datetime.strptime(end_str, "%I:%M%p")

            # Combine date and time
            start_dt = date_parsed.replace(
                hour=start_time.hour,
                minute=start_time.minute,
            )
            end_dt = date_parsed.replace(
                hour=end_time.hour,
                minute=end_time.minute,
            )

            # Handle day boundary (e.g., 11:45PM - 12:00AM)
            if end_dt < start_dt:
                end_dt += timedelta(days=1)

            # Convert from ET to UTC (ET is UTC-5 or UTC-4 for DST)
            # Using UTC-5 for simplicity
            start_dt += timedelta(hours=5)
            end_dt += timedelta(hours=5)

            return (start_dt, end_dt)

        except Exception as e:
            log.warning("Failed to parse time from question", error=str(e))
            return None

    async def get_next_market(self, asset: str = "BTC") -> Optional[Market15Min]:
        """Get the next upcoming market for an asset.

        Args:
            asset: Asset symbol

        Returns:
            Next market to trade, or None
        """
        markets = await self.find_active_markets([asset])

        if not markets:
            return None

        # Sort by end time, get the one ending soonest that's still tradeable
        tradeable = [m for m in markets if m.is_tradeable]
        if not tradeable:
            return None

        return min(tradeable, key=lambda m: m.end_time)

    async def get_current_market(self, asset: str = "BTC") -> Optional[Market15Min]:
        """Get the currently active market for an asset.

        Args:
            asset: Asset symbol

        Returns:
            Currently active market, or None
        """
        now = datetime.utcnow()
        markets = await self.find_active_markets([asset])

        for market in markets:
            if market.start_time <= now <= market.end_time:
                return market

        return None
