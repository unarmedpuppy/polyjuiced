"""Liquidity data collector.

Handles periodic depth snapshots and fill logging for building
persistence and slippage models.
"""

import asyncio
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, TYPE_CHECKING

import structlog

from .models import FillRecord, LiquiditySnapshot, DepthLevel

if TYPE_CHECKING:
    from ..client.polymarket import PolymarketClient
    from ..persistence import Database

log = structlog.get_logger()


class LiquidityCollector:
    """Collects liquidity data for analysis.

    This collector:
    1. Takes periodic order book snapshots
    2. Logs fill records when orders execute
    3. Provides methods to query aggregated statistics

    Usage:
        collector = LiquidityCollector(client, database)
        await collector.start()

        # When executing an order:
        fill_record = await collector.log_fill(...)

        # Periodically:
        await collector.take_snapshot(token_id, condition_id, asset)
    """

    def __init__(
        self,
        client: "PolymarketClient",
        database: "Database",
        snapshot_interval_seconds: float = 30.0,
        max_snapshot_levels: int = 10,
    ):
        """Initialize the collector.

        Args:
            client: Polymarket client for fetching order books
            database: Database for persisting data
            snapshot_interval_seconds: How often to take snapshots
            max_snapshot_levels: Max order book levels to capture
        """
        self.client = client
        self.db = database
        self.snapshot_interval = snapshot_interval_seconds
        self.max_levels = max_snapshot_levels

        self._running = False
        self._tokens_to_track: Dict[str, Dict[str, str]] = {}  # {token_id: {condition_id, asset}}
        self._last_snapshot_time: Dict[str, float] = {}

    async def start(self) -> None:
        """Start the collector background task."""
        self._running = True
        log.info(
            "Liquidity collector started",
            interval=f"{self.snapshot_interval}s",
            max_levels=self.max_levels,
        )

    async def stop(self) -> None:
        """Stop the collector."""
        self._running = False
        log.info("Liquidity collector stopped")

    def track_token(self, token_id: str, condition_id: str, asset: str) -> None:
        """Add a token to track for snapshots.

        Args:
            token_id: Token ID to track
            condition_id: Market condition ID
            asset: Asset symbol (BTC, ETH, SOL)
        """
        self._tokens_to_track[token_id] = {
            "condition_id": condition_id,
            "asset": asset,
        }
        log.debug("Tracking token for liquidity", token_id=token_id[:20] + "...", asset=asset)

    def untrack_token(self, token_id: str) -> None:
        """Remove a token from tracking.

        Args:
            token_id: Token ID to stop tracking
        """
        self._tokens_to_track.pop(token_id, None)
        self._last_snapshot_time.pop(token_id, None)

    async def take_snapshot(
        self,
        token_id: str,
        condition_id: str,
        asset: str,
    ) -> Optional[LiquiditySnapshot]:
        """Take a snapshot of the order book.

        Args:
            token_id: Token to snapshot
            condition_id: Market condition ID
            asset: Asset symbol

        Returns:
            LiquiditySnapshot if successful, None otherwise
        """
        try:
            order_book = self.client.get_order_book(token_id)

            snapshot = LiquiditySnapshot.from_order_book(
                order_book=order_book,
                token_id=token_id,
                condition_id=condition_id,
                asset=asset,
                max_levels=self.max_levels,
            )

            # Persist to database
            bid_levels = [[level.price, level.size] for level in snapshot.bid_levels]
            ask_levels = [[level.price, level.size] for level in snapshot.ask_levels]

            await self.db.save_liquidity_snapshot(
                token_id=token_id,
                condition_id=condition_id,
                asset=asset,
                bid_levels=bid_levels,
                ask_levels=ask_levels,
                total_bid_depth=snapshot.total_bid_depth,
                total_ask_depth=snapshot.total_ask_depth,
            )

            log.debug(
                "Liquidity snapshot saved",
                asset=asset,
                ask_depth=f"{snapshot.total_ask_depth:.1f}",
                bid_depth=f"{snapshot.total_bid_depth:.1f}",
            )

            return snapshot

        except Exception as e:
            log.warning("Failed to take liquidity snapshot", error=str(e), asset=asset)
            return None

    async def take_snapshots_for_tracked(self) -> int:
        """Take snapshots for all tracked tokens that are due.

        Returns:
            Number of snapshots taken
        """
        if not self._running:
            return 0

        now = time.time()
        count = 0

        for token_id, info in list(self._tokens_to_track.items()):
            last_time = self._last_snapshot_time.get(token_id, 0)

            if now - last_time >= self.snapshot_interval:
                snapshot = await self.take_snapshot(
                    token_id=token_id,
                    condition_id=info["condition_id"],
                    asset=info["asset"],
                )
                if snapshot:
                    self._last_snapshot_time[token_id] = now
                    count += 1

        return count

    async def log_fill(
        self,
        token_id: str,
        condition_id: str,
        asset: str,
        side: str,
        intended_size: float,
        intended_price: float,
        order_result: dict,
        start_time_ms: int,
        pre_fill_depth: float,
    ) -> Optional[FillRecord]:
        """Log a fill record from order execution.

        Args:
            token_id: Token that was traded
            condition_id: Market condition ID
            asset: Asset symbol
            side: "BUY" or "SELL"
            intended_size: Shares we wanted
            intended_price: Price we wanted
            order_result: Result dict from exchange
            start_time_ms: When order was submitted (ms since epoch)
            pre_fill_depth: Depth before our order

        Returns:
            FillRecord if successful, None otherwise
        """
        try:
            fill_record = FillRecord.from_execution(
                token_id=token_id,
                condition_id=condition_id,
                asset=asset,
                side=side,
                intended_size=intended_size,
                intended_price=intended_price,
                pre_fill_depth=pre_fill_depth,
                order_result=order_result,
                start_time_ms=start_time_ms,
            )

            # Persist to database
            await self.db.save_fill_record(
                token_id=fill_record.token_id,
                condition_id=fill_record.condition_id,
                asset=fill_record.asset,
                side=fill_record.side,
                intended_size=fill_record.intended_size,
                filled_size=fill_record.filled_size,
                intended_price=fill_record.intended_price,
                actual_avg_price=fill_record.actual_avg_price,
                time_to_fill_ms=fill_record.time_to_fill_ms,
                slippage=fill_record.slippage,
                pre_fill_depth=fill_record.pre_fill_depth,
                order_type=fill_record.order_type,
                order_id=fill_record.order_id,
                fill_ratio=fill_record.fill_ratio,
                persistence_ratio=fill_record.persistence_ratio,
            )

            log.info(
                "Fill record logged",
                asset=asset,
                side=side,
                filled=f"{fill_record.filled_size:.2f}/{fill_record.intended_size:.2f}",
                slippage=f"{fill_record.slippage:.4f}",
                time_ms=fill_record.time_to_fill_ms,
            )

            return fill_record

        except Exception as e:
            log.error("Failed to log fill record", error=str(e), asset=asset)
            return None

    async def get_persistence_estimate(
        self,
        token_id: str = None,
        asset: str = None,
        lookback_minutes: int = 60,
        default: float = 0.4,
    ) -> float:
        """Calculate persistence estimate from historical fills.

        Persistence = how much of displayed depth actually fills when touched.
        If displayed depth shows 100 shares but fills only average 40 shares,
        persistence is 0.4.

        Args:
            token_id: Filter by token (optional)
            asset: Filter by asset (optional)
            lookback_minutes: Analysis window
            default: Default if no data available

        Returns:
            Persistence estimate (0-1)
        """
        try:
            stats = await self.db.get_slippage_stats(
                token_id=token_id,
                asset=asset,
                lookback_minutes=lookback_minutes,
            )

            if stats.get("fill_count", 0) < 5:
                # Not enough data, use default
                return default

            avg_persistence = stats.get("avg_persistence_ratio")
            if avg_persistence and avg_persistence > 0:
                # Use 25th percentile (conservative) instead of mean
                # For now, apply a 0.75 multiplier as approximation
                return min(avg_persistence * 0.75, 1.0)

            return default

        except Exception as e:
            log.warning("Failed to calculate persistence", error=str(e))
            return default

    async def get_slippage_estimate(
        self,
        token_id: str = None,
        asset: str = None,
        size: float = 10.0,
        lookback_minutes: int = 60,
        default_per_10_shares: float = 0.01,
    ) -> float:
        """Estimate expected slippage for a given size.

        Args:
            token_id: Filter by token (optional)
            asset: Filter by asset (optional)
            size: Order size in shares
            lookback_minutes: Analysis window
            default_per_10_shares: Default slippage per 10 shares

        Returns:
            Expected slippage in price units
        """
        try:
            stats = await self.db.get_slippage_stats(
                token_id=token_id,
                asset=asset,
                lookback_minutes=lookback_minutes,
            )

            if stats.get("fill_count", 0) < 5:
                # Not enough data, use default
                return (size / 10) * default_per_10_shares

            avg_slippage = stats.get("avg_slippage", 0) or 0
            total_volume = stats.get("total_volume", 0) or 1

            # Scale slippage by size relative to average fill
            avg_fill_size = total_volume / stats.get("fill_count", 1)
            size_ratio = size / avg_fill_size if avg_fill_size > 0 else 1

            # Use 75th percentile (conservative) - approximate with 1.5x multiplier
            return abs(avg_slippage) * size_ratio * 1.5

        except Exception as e:
            log.warning("Failed to estimate slippage", error=str(e))
            return (size / 10) * default_per_10_shares

    async def get_depth_at_time(
        self,
        token_id: str,
        target_time: datetime,
        side: str = "ask",
    ) -> Optional[float]:
        """Get historical depth at a specific time.

        Finds the closest snapshot to the target time and returns depth.

        Args:
            token_id: Token to query
            target_time: Time to look up
            side: "ask" or "bid"

        Returns:
            Depth at that time, or None if no data
        """
        try:
            # Get snapshots around the target time
            snapshots = await self.db.get_recent_snapshots(
                token_id=token_id,
                limit=10,
            )

            if not snapshots:
                return None

            # Find closest snapshot
            closest = None
            closest_diff = float("inf")

            for snapshot in snapshots:
                snap_time = datetime.fromisoformat(snapshot["timestamp"])
                diff = abs((snap_time - target_time).total_seconds())
                if diff < closest_diff:
                    closest_diff = diff
                    closest = snapshot

            if closest:
                if side == "ask":
                    return closest.get("total_ask_depth", 0)
                else:
                    return closest.get("total_bid_depth", 0)

            return None

        except Exception as e:
            log.warning("Failed to get historical depth", error=str(e))
            return None

    async def cleanup_old_data(self, days: int = 30) -> Dict[str, int]:
        """Clean up old liquidity data.

        Args:
            days: Delete data older than this many days

        Returns:
            Dict with counts of deleted records
        """
        result = await self.db.cleanup_old_liquidity_data(days=days)
        if result["fills"] > 0 or result["snapshots"] > 0:
            log.info(
                "Cleaned up old liquidity data",
                fills_deleted=result["fills"],
                snapshots_deleted=result["snapshots"],
            )
        return result
