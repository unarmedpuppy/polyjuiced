"""Settlement Manager - handles position settlement after market resolution.

This service:
- Monitors for resolved markets via Gamma API
- Tracks positions pending settlement in the queue
- Checks market resolution status and waits for resolution window
- Claims winning positions via CTF redemption
- Manages the settlement queue state transitions:
  pending -> claimable (after market resolves + wait period) -> claimed

Ported queue logic from legacy/src/persistence.py settlement_queue handling.
"""

import asyncio
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Optional

import structlog

from mercury.core.config import ConfigManager
from mercury.core.events import EventBus
from mercury.core.lifecycle import BaseComponent, HealthCheckResult, HealthStatus
from mercury.integrations.chain.client import PolygonClient
from mercury.integrations.polymarket.gamma import GammaClient
from mercury.integrations.polymarket.types import MarketInfo, PolymarketSettings
from mercury.services.state_store import Position, SettlementQueueEntry, StateStore

log = structlog.get_logger()

# Settlement parameters
DEFAULT_CHECK_INTERVAL = 300  # 5 minutes
MAX_CLAIM_ATTEMPTS = 5
# Wait time after market end before attempting claims (allows resolution to settle)
DEFAULT_RESOLUTION_WAIT_SECONDS = 600  # 10 minutes


@dataclass
class SettlementResult:
    """Result of a settlement attempt."""

    success: bool
    position_id: str
    condition_id: str
    proceeds: Optional[Decimal] = None
    profit: Optional[Decimal] = None
    error: Optional[str] = None
    tx_hash: Optional[str] = None
    resolution: Optional[str] = None  # "YES" or "NO"


class SettlementManager(BaseComponent):
    """Handles position settlement after market resolution.

    This service:
    1. Monitors the settlement queue for positions to claim
    2. Checks if markets have resolved via Gamma API
    3. Waits for resolution window before attempting claims
    4. Redeems positions via CTF contract
    5. Records settlement results and P&L

    Settlement Queue State Machine:
    - pending: Position opened, waiting for market resolution
    - claimable: Market resolved, waiting for claim execution
    - claimed: Position successfully redeemed
    - failed: Claim permanently failed after max attempts

    Event channels subscribed:
    - position.opened - Track new positions for settlement
    - order.filled - Track fills to update settlement queue

    Event channels published:
    - settlement.claimed - Position successfully claimed
    - settlement.failed - Claim attempt failed
    - settlement.queued - New position queued for settlement
    - settlement.claimable - Position ready for claiming
    """

    def __init__(
        self,
        config: ConfigManager,
        event_bus: EventBus,
        state_store: Optional[StateStore] = None,
        gamma_client: Optional[GammaClient] = None,
        polygon_client: Optional[PolygonClient] = None,
    ):
        """Initialize the settlement manager.

        Args:
            config: Configuration manager.
            event_bus: EventBus for events.
            state_store: StateStore for persistence.
            gamma_client: GammaClient for market queries.
            polygon_client: PolygonClient for chain interactions.
        """
        super().__init__()
        self._config = config
        self._event_bus = event_bus
        self._log = log.bind(component="settlement_manager")

        # Dependencies
        self._state_store = state_store
        self._gamma_client = gamma_client
        self._polygon_client = polygon_client

        # Configuration
        self._check_interval = config.get_int(
            "settlement.check_interval_seconds",
            DEFAULT_CHECK_INTERVAL
        )
        self._dry_run = config.get_bool("mercury.dry_run", True)
        self._resolution_wait = config.get_int(
            "settlement.resolution_wait_seconds",
            DEFAULT_RESOLUTION_WAIT_SECONDS
        )
        self._max_claim_attempts = config.get_int(
            "settlement.max_claim_attempts",
            MAX_CLAIM_ATTEMPTS
        )

        # State
        self._should_run = False
        self._check_task: Optional[asyncio.Task] = None
        self._claims_processed = 0
        self._claims_failed = 0
        self._positions_queued = 0
        self._markets_checked = 0

        # Cache for market resolution status (condition_id -> MarketInfo)
        self._resolution_cache: dict[str, MarketInfo] = {}

    async def start(self) -> None:
        """Start the settlement manager."""
        self._start_time = time.time()
        self._should_run = True
        self._log.info(
            "starting_settlement_manager",
            check_interval=self._check_interval,
            resolution_wait=self._resolution_wait,
            max_attempts=self._max_claim_attempts,
            dry_run=self._dry_run,
        )

        # Initialize clients if not provided
        if self._gamma_client is None:
            settings = PolymarketSettings(
                private_key=self._config.get("polymarket.private_key", ""),
            )
            self._gamma_client = GammaClient(settings)
            await self._gamma_client.connect()

        if self._polygon_client is None and not self._dry_run:
            self._polygon_client = PolygonClient(
                rpc_url=self._config.get("polygon.rpc_url", "https://polygon-rpc.com"),
                private_key=self._config.get("polymarket.private_key", ""),
            )
            await self._polygon_client.connect()

        # Subscribe to events
        await self._event_bus.subscribe("position.opened", self._on_position_opened)
        await self._event_bus.subscribe("order.filled", self._on_order_filled)

        # Start check loop
        self._check_task = asyncio.create_task(self._check_loop())

        self._log.info("settlement_manager_started")

    async def stop(self) -> None:
        """Stop the settlement manager."""
        self._should_run = False

        if self._check_task:
            self._check_task.cancel()
            try:
                await self._check_task
            except asyncio.CancelledError:
                pass

        if self._gamma_client:
            await self._gamma_client.close()

        if self._polygon_client:
            await self._polygon_client.close()

        self._log.info(
            "settlement_manager_stopped",
            claims_processed=self._claims_processed,
            claims_failed=self._claims_failed,
            positions_queued=self._positions_queued,
            markets_checked=self._markets_checked,
        )

    async def health_check(self) -> HealthCheckResult:
        """Check settlement manager health."""
        if not self._should_run:
            return HealthCheckResult(
                status=HealthStatus.UNHEALTHY,
                message="Manager not running",
            )

        # Get settlement stats from state store
        queue_stats = {}
        if self._state_store:
            try:
                queue_stats = await self._state_store.get_settlement_stats()
            except Exception as e:
                self._log.debug("failed_to_get_settlement_stats", error=str(e))

        return HealthCheckResult(
            status=HealthStatus.HEALTHY,
            message="Settlement monitoring active",
            details={
                "claims_processed": self._claims_processed,
                "claims_failed": self._claims_failed,
                "positions_queued": self._positions_queued,
                "markets_checked": self._markets_checked,
                "queue_total": queue_stats.get("total_positions", 0),
                "queue_unclaimed": queue_stats.get("unclaimed", 0),
                "total_claim_profit": queue_stats.get("total_claim_profit", 0),
            },
        )

    async def check_settlements(self) -> int:
        """Check and process claimable positions.

        This method:
        1. Gets positions from settlement queue that are past the resolution wait period
        2. Checks if each market has resolved via Gamma API
        3. Processes claims for resolved markets
        4. Updates queue state based on results

        Returns:
            Number of claims processed.
        """
        if self._state_store is None:
            return 0

        # Get positions that are past the resolution wait period
        # This returns Position objects from the settlement queue
        queue = await self._state_store.get_claimable_positions(
            max_attempts=self._max_claim_attempts,
            min_time_since_end_seconds=self._resolution_wait,
        )

        if not queue:
            return 0

        self._log.info("checking_settlements", pending=len(queue))

        processed = 0
        for position in queue:
            try:
                # Get the settlement queue entry for full details
                entry = await self._state_store.get_settlement_queue_entry(position.position_id)
                if entry is None:
                    self._log.warning("settlement_entry_not_found", position_id=position.position_id)
                    continue

                result = await self._process_claim(entry)
                if result.success:
                    processed += 1
                    self._claims_processed += 1
                else:
                    self._claims_failed += 1
            except Exception as e:
                self._log.error(
                    "claim_error",
                    position_id=position.position_id,
                    error=str(e),
                )
                self._claims_failed += 1
                # Record the failed attempt
                await self._state_store.record_claim_attempt(
                    position.position_id, error=str(e)
                )

        return processed

    async def _check_market_resolution(self, condition_id: str) -> Optional[MarketInfo]:
        """Check if a market has resolved via Gamma API.

        Uses cache to avoid repeated API calls for the same market.

        Args:
            condition_id: Market condition ID.

        Returns:
            MarketInfo if resolved, None otherwise.
        """
        self._markets_checked += 1

        # Check cache first
        if condition_id in self._resolution_cache:
            cached = self._resolution_cache[condition_id]
            if cached.resolved:
                return cached

        # Fetch from Gamma API
        try:
            market_info = await self._gamma_client.get_market_info(
                condition_id, use_cache=False
            )

            if market_info is None:
                self._log.warning("market_not_found", condition_id=condition_id[:16] + "...")
                return None

            # Cache resolved markets (they won't change)
            if market_info.resolved:
                self._resolution_cache[condition_id] = market_info
                self._log.info(
                    "market_resolved",
                    condition_id=condition_id[:16] + "...",
                    resolution=market_info.resolution,
                )

            return market_info if market_info.resolved else None

        except Exception as e:
            self._log.error(
                "market_resolution_check_failed",
                condition_id=condition_id[:16] + "...",
                error=str(e),
            )
            return None

    def _calculate_settlement_proceeds(
        self,
        entry: SettlementQueueEntry,
        market_info: MarketInfo,
    ) -> tuple[Decimal, Decimal]:
        """Calculate settlement proceeds and profit.

        For a winning position:
        - Each share is worth $1.00
        - Profit = $1.00 * shares - entry_cost

        For a losing position:
        - Shares are worth $0.00
        - Profit = -entry_cost (total loss)

        Args:
            entry: Settlement queue entry.
            market_info: Resolved market info.

        Returns:
            Tuple of (proceeds, profit).
        """
        shares = entry.shares or entry.size
        entry_cost = entry.cost_basis

        # Determine if this is a winning position
        resolution = market_info.resolution
        side = entry.side.upper() if entry.side else ""

        # YES wins if resolution is "YES", NO wins if resolution is "NO"
        is_winner = (
            (side == "YES" and resolution == "YES") or
            (side == "NO" and resolution == "NO")
        )

        if is_winner:
            # Winning shares are worth $1.00 each
            proceeds = shares
            profit = proceeds - entry_cost
        else:
            # Losing shares are worthless
            proceeds = Decimal("0")
            profit = -entry_cost

        return proceeds, profit

    async def _process_claim(self, entry: SettlementQueueEntry) -> SettlementResult:
        """Process a single claim from the queue.

        Args:
            entry: Settlement queue entry with position and market info.

        Returns:
            SettlementResult with claim outcome.
        """
        position_id = entry.position_id
        condition_id = entry.condition_id or entry.market_id

        self._log.info(
            "processing_claim",
            position_id=position_id,
            condition_id=condition_id[:16] + "..." if len(condition_id) > 16 else condition_id,
            side=entry.side,
            shares=str(entry.shares or entry.size),
        )

        # Check if market is resolved
        market_info = await self._check_market_resolution(condition_id)
        if market_info is None:
            self._log.debug("market_not_resolved_yet", condition_id=condition_id[:16] + "...")
            return SettlementResult(
                success=False,
                position_id=position_id,
                condition_id=condition_id,
                error="Market not yet resolved",
            )

        # Calculate proceeds and profit
        proceeds, profit = self._calculate_settlement_proceeds(entry, market_info)

        self._log.info(
            "settlement_calculated",
            position_id=position_id,
            resolution=market_info.resolution,
            proceeds=str(proceeds),
            profit=str(profit),
            is_winner=profit > 0,
        )

        # Dry run - simulate success
        if self._dry_run:
            self._log.info("dry_run_claim", position_id=position_id)
            await self._state_store.mark_claimed(position_id, proceeds, profit)

            await self._event_bus.publish("settlement.claimed", {
                "position_id": position_id,
                "market_id": entry.market_id,
                "condition_id": condition_id,
                "resolution": market_info.resolution,
                "proceeds": str(proceeds),
                "profit": str(profit),
                "side": entry.side,
                "dry_run": True,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            })

            return SettlementResult(
                success=True,
                position_id=position_id,
                condition_id=condition_id,
                proceeds=proceeds,
                profit=profit,
                resolution=market_info.resolution,
            )

        # Execute CTF redemption on chain
        try:
            receipt = await self._polygon_client.redeem_ctf_positions(condition_id)

            if receipt.status:
                await self._state_store.mark_claimed(position_id, proceeds, profit)

                await self._event_bus.publish("settlement.claimed", {
                    "position_id": position_id,
                    "market_id": entry.market_id,
                    "condition_id": condition_id,
                    "resolution": market_info.resolution,
                    "proceeds": str(proceeds),
                    "profit": str(profit),
                    "side": entry.side,
                    "tx_hash": receipt.tx_hash,
                    "gas_used": receipt.gas_used,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                })

                self._log.info(
                    "claim_successful",
                    position_id=position_id,
                    tx_hash=receipt.tx_hash,
                    profit=str(profit),
                )

                return SettlementResult(
                    success=True,
                    position_id=position_id,
                    condition_id=condition_id,
                    proceeds=proceeds,
                    profit=profit,
                    tx_hash=receipt.tx_hash,
                    resolution=market_info.resolution,
                )
            else:
                error_msg = "Transaction failed"
                await self._state_store.record_claim_attempt(position_id, error=error_msg)

                await self._event_bus.publish("settlement.failed", {
                    "position_id": position_id,
                    "market_id": entry.market_id,
                    "condition_id": condition_id,
                    "error": error_msg,
                    "attempt": entry.claim_attempts + 1,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                })

                return SettlementResult(
                    success=False,
                    position_id=position_id,
                    condition_id=condition_id,
                    error=error_msg,
                )

        except Exception as e:
            error_msg = str(e)
            await self._state_store.record_claim_attempt(position_id, error=error_msg)

            # Mark as permanently failed if max attempts reached
            if entry.claim_attempts + 1 >= self._max_claim_attempts:
                await self._state_store.mark_settlement_failed(
                    position_id, f"Max attempts reached: {error_msg}"
                )

            await self._event_bus.publish("settlement.failed", {
                "position_id": position_id,
                "market_id": entry.market_id,
                "condition_id": condition_id,
                "error": error_msg,
                "attempt": entry.claim_attempts + 1,
                "max_attempts": self._max_claim_attempts,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            })

            return SettlementResult(
                success=False,
                position_id=position_id,
                condition_id=condition_id,
                error=error_msg,
            )

    async def _check_loop(self) -> None:
        """Periodic settlement check loop."""
        while self._should_run:
            try:
                await self.check_settlements()
            except Exception as e:
                self._log.error("check_loop_error", error=str(e))

            await asyncio.sleep(self._check_interval)

    async def _on_position_opened(self, data: dict) -> None:
        """Handle position opened event - queue for settlement.

        Expected event data:
        - position_id: Unique position identifier
        - market_id: Market identifier (condition_id)
        - side: "YES" or "NO"
        - size: Number of shares
        - entry_price: Price per share
        - asset: Asset symbol (optional)
        - token_id: Token ID (optional)
        - market_end_time: When market ends (optional)
        """
        if self._state_store is None:
            return

        position_id = data.get("position_id")
        market_id = data.get("market_id")

        if not position_id or not market_id:
            return

        # Build Position object from event data
        position = Position(
            position_id=position_id,
            market_id=market_id,
            strategy=data.get("strategy", "unknown"),
            side=data.get("side", "YES"),
            size=Decimal(str(data.get("size", 0))),
            entry_price=Decimal(str(data.get("entry_price", 0))),
        )

        # Get condition ID from event or use market_id
        condition_id = data.get("condition_id", market_id)

        # Parse market_end_time if provided
        market_end_time = None
        if data.get("market_end_time"):
            try:
                if isinstance(data["market_end_time"], datetime):
                    market_end_time = data["market_end_time"]
                else:
                    market_end_time = datetime.fromisoformat(
                        str(data["market_end_time"]).replace("Z", "+00:00")
                    )
            except (ValueError, TypeError):
                pass

        await self._state_store.queue_for_settlement(
            position=position,
            condition_id=condition_id,
            token_id=data.get("token_id"),
            asset=data.get("asset"),
            market_end_time=market_end_time,
        )

        self._positions_queued += 1

        self._log.info(
            "position_queued_for_settlement",
            position_id=position_id,
            market_id=market_id,
            side=data.get("side"),
            size=str(data.get("size")),
        )

        # Emit queued event
        await self._event_bus.publish("settlement.queued", {
            "position_id": position_id,
            "market_id": market_id,
            "condition_id": condition_id,
            "side": data.get("side"),
            "size": str(data.get("size")),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })

    async def _on_order_filled(self, data: dict) -> None:
        """Handle order filled event - update settlement queue if needed.

        This is used to track fills that add to positions in the settlement queue.
        Useful for tracking additional fills on existing positions.

        Expected event data:
        - position_id: Position identifier (if updating existing)
        - market_id: Market identifier
        - side: "YES" or "NO"
        - filled_size: Size of the fill
        - fill_price: Price of the fill
        """
        if self._state_store is None:
            return

        position_id = data.get("position_id")
        if not position_id:
            return

        # Check if this position is already in the settlement queue
        entry = await self._state_store.get_settlement_queue_entry(position_id)
        if entry is None:
            # Not in queue yet, will be added via position.opened
            return

        # Log the fill for tracking
        self._log.debug(
            "fill_recorded_for_settlement",
            position_id=position_id,
            filled_size=data.get("filled_size"),
            fill_price=data.get("fill_price"),
        )

    # ============ Public Queue Management Methods ============

    async def get_settlement_queue(
        self,
        status: Optional[str] = None,
        include_claimed: bool = False,
        limit: int = 100,
    ) -> list[SettlementQueueEntry]:
        """Get settlement queue entries.

        Args:
            status: Filter by status (pending, claimed, failed).
            include_claimed: Include claimed entries.
            limit: Maximum entries to return.

        Returns:
            List of queue entries.
        """
        if self._state_store is None:
            return []

        return await self._state_store.get_settlement_queue(
            status=status,
            include_claimed=include_claimed,
            limit=limit,
        )

    async def get_failed_claims(
        self,
        min_attempts: int = 1,
        limit: int = 100,
    ) -> list[SettlementQueueEntry]:
        """Get positions with failed claim attempts.

        Args:
            min_attempts: Minimum failed attempts to include.
            limit: Maximum entries to return.

        Returns:
            List of failed claim entries.
        """
        if self._state_store is None:
            return []

        return await self._state_store.get_failed_claims(
            min_attempts=min_attempts,
            limit=limit,
        )

    async def retry_failed_claim(self, position_id: str) -> bool:
        """Retry a failed claim.

        Resets the status back to pending for another attempt.

        Args:
            position_id: Position ID to retry.

        Returns:
            True if reset succeeded.
        """
        if self._state_store is None:
            return False

        result = await self._state_store.retry_failed_claim(position_id)

        if result:
            self._log.info("claim_retry_requested", position_id=position_id)

        return result

    async def force_check_market(self, condition_id: str) -> Optional[MarketInfo]:
        """Force a market resolution check (bypasses cache).

        Useful for debugging or manual checks.

        Args:
            condition_id: Market condition ID.

        Returns:
            MarketInfo if resolved, None otherwise.
        """
        # Clear from cache first
        self._resolution_cache.pop(condition_id, None)

        return await self._check_market_resolution(condition_id)

    def clear_resolution_cache(self) -> int:
        """Clear the resolution cache.

        Returns:
            Number of entries cleared.
        """
        count = len(self._resolution_cache)
        self._resolution_cache.clear()
        return count
