"""Settlement Manager - handles position settlement after market resolution.

This service:
- Monitors for resolved markets
- Claims winning positions via CTF redemption
- Manages the settlement queue
- Tracks settlement metrics
"""

import asyncio
import time
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

import structlog

from mercury.core.config import ConfigManager
from mercury.core.events import EventBus
from mercury.core.lifecycle import BaseComponent, HealthCheckResult, HealthStatus
from mercury.integrations.chain.client import PolygonClient
from mercury.integrations.polymarket.gamma import GammaClient
from mercury.integrations.polymarket.types import PolymarketSettings
from mercury.services.state_store import StateStore

log = structlog.get_logger()

# Settlement parameters
DEFAULT_CHECK_INTERVAL = 300  # 5 minutes
MAX_CLAIM_ATTEMPTS = 5


class SettlementManager(BaseComponent):
    """Handles position settlement after market resolution.

    This service:
    1. Monitors the settlement queue for positions to claim
    2. Checks if markets have resolved
    3. Redeems positions via CTF contract
    4. Records settlement results

    Event channels subscribed:
    - position.opened - Track new positions for settlement

    Event channels published:
    - settlement.claimed - Position successfully claimed
    - settlement.failed - Claim attempt failed
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

        # State
        self._should_run = False
        self._check_task: Optional[asyncio.Task] = None
        self._claims_processed = 0
        self._claims_failed = 0

    async def start(self) -> None:
        """Start the settlement manager."""
        self._start_time = time.time()
        self._should_run = True
        self._log.info(
            "starting_settlement_manager",
            check_interval=self._check_interval,
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
        )

    async def health_check(self) -> HealthCheckResult:
        """Check settlement manager health."""
        if not self._should_run:
            return HealthCheckResult(
                status=HealthStatus.UNHEALTHY,
                message="Manager not running",
            )

        return HealthCheckResult(
            status=HealthStatus.HEALTHY,
            message="Settlement monitoring active",
            details={
                "claims_processed": self._claims_processed,
                "claims_failed": self._claims_failed,
            },
        )

    async def check_settlements(self) -> int:
        """Check and process claimable positions.

        Returns:
            Number of claims processed.
        """
        if self._state_store is None:
            return 0

        # Get pending claims
        queue = await self._state_store.get_claimable_positions(MAX_CLAIM_ATTEMPTS)

        if not queue:
            return 0

        self._log.info("checking_settlements", pending=len(queue))

        processed = 0
        for item in queue:
            try:
                result = await self._process_claim(item)
                if result:
                    processed += 1
                    self._claims_processed += 1
                else:
                    self._claims_failed += 1
            except Exception as e:
                self._log.error(
                    "claim_error",
                    position_id=item.get("position_id"),
                    error=str(e),
                )
                self._claims_failed += 1

        return processed

    async def _process_claim(self, queue_item: dict) -> bool:
        """Process a single claim from the queue.

        Args:
            queue_item: Queue item with position and market info.

        Returns:
            True if claim succeeded.
        """
        position_id = queue_item["position_id"]
        market_id = queue_item["market_id"]
        condition_id = queue_item["condition_id"]
        queue_id = queue_item["id"]

        self._log.info(
            "processing_claim",
            position_id=position_id,
            condition_id=condition_id[:16] + "...",
        )

        # Check if market is resolved
        market = await self._gamma_client.get_market(condition_id)
        if market is None:
            self._log.warning("market_not_found", condition_id=condition_id)
            return False

        if not market.get("resolved", False):
            self._log.debug("market_not_resolved", condition_id=condition_id)
            return False

        # Dry run - simulate success
        if self._dry_run:
            self._log.info("dry_run_claim", position_id=position_id)
            await self._state_store.mark_settlement_attempt(queue_id, success=True)

            await self._event_bus.publish("settlement.claimed", {
                "position_id": position_id,
                "market_id": market_id,
                "proceeds": "0",  # Simulated
                "timestamp": datetime.now(timezone.utc).isoformat(),
            })

            return True

        # Execute CTF redemption
        try:
            receipt = await self._polygon_client.redeem_ctf_positions(condition_id)

            if receipt.status:
                await self._state_store.mark_settlement_attempt(queue_id, success=True)

                await self._event_bus.publish("settlement.claimed", {
                    "position_id": position_id,
                    "market_id": market_id,
                    "tx_hash": receipt.tx_hash,
                    "gas_used": receipt.gas_used,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                })

                self._log.info(
                    "claim_successful",
                    position_id=position_id,
                    tx_hash=receipt.tx_hash,
                )
                return True
            else:
                await self._state_store.mark_settlement_attempt(
                    queue_id, success=False, error="Transaction failed"
                )

                await self._event_bus.publish("settlement.failed", {
                    "position_id": position_id,
                    "market_id": market_id,
                    "error": "Transaction failed",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                })

                return False

        except Exception as e:
            await self._state_store.mark_settlement_attempt(
                queue_id, success=False, error=str(e)
            )

            await self._event_bus.publish("settlement.failed", {
                "position_id": position_id,
                "market_id": market_id,
                "error": str(e),
                "timestamp": datetime.now(timezone.utc).isoformat(),
            })

            return False

    async def _check_loop(self) -> None:
        """Periodic settlement check loop."""
        while self._should_run:
            try:
                await self.check_settlements()
            except Exception as e:
                self._log.error("check_loop_error", error=str(e))

            await asyncio.sleep(self._check_interval)

    async def _on_position_opened(self, data: dict) -> None:
        """Handle position opened event - queue for settlement."""
        if self._state_store is None:
            return

        position_id = data.get("position_id")
        market_id = data.get("market_id")

        if not position_id or not market_id:
            return

        # Get condition ID from market
        # For now, use market_id as condition_id
        condition_id = market_id

        await self._state_store.queue_for_settlement(
            position_id=position_id,
            market_id=market_id,
            condition_id=condition_id,
        )

        self._log.info(
            "position_queued_for_settlement",
            position_id=position_id,
            market_id=market_id,
        )
