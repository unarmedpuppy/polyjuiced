"""Execution Engine - handles order execution and lifecycle management.

This service:
- Executes trading signals as orders
- Handles dual-leg arbitrage execution
- Manages order lifecycle (submit, fill, cancel)
- Tracks execution latency and slippage
"""

import asyncio
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

import structlog

from mercury.core.config import ConfigManager
from mercury.core.events import EventBus
from mercury.core.lifecycle import BaseComponent, HealthCheckResult, HealthStatus
from mercury.domain.order import OrderRequest, Position, PositionStatus
from mercury.domain.signal import ApprovedSignal, SignalType
from mercury.integrations.polymarket.clob import CLOBClient, InsufficientLiquidityError
from mercury.integrations.polymarket.types import (
    DualLegOrderResult,
    OrderResult,
    OrderSide,
    OrderStatus,
    PolymarketSettings,
)

log = structlog.get_logger()


@dataclass
class ExecutionResult:
    """Result of executing a signal."""

    success: bool
    signal_id: str
    trade_id: Optional[str] = None
    position_id: Optional[str] = None
    yes_filled: Decimal = Decimal("0")
    no_filled: Decimal = Decimal("0")
    total_cost: Decimal = Decimal("0")
    guaranteed_pnl: Decimal = Decimal("0")
    execution_time_ms: Optional[float] = None
    error: Optional[str] = None


class ExecutionEngine(BaseComponent):
    """Executes approved trading signals as orders.

    This service:
    1. Listens for approved signals from RiskManager
    2. Executes orders via CLOBClient
    3. Handles dual-leg arbitrage atomically
    4. Publishes execution results to EventBus

    Event channels subscribed:
    - risk.approved.* - Approved signals to execute

    Event channels published:
    - order.submitted - Order sent to exchange
    - order.filled - Order filled
    - order.rejected - Order rejected
    - position.opened - New position created
    - execution.complete - Execution finished
    """

    def __init__(
        self,
        config: ConfigManager,
        event_bus: EventBus,
        clob_client: Optional[CLOBClient] = None,
    ):
        """Initialize the execution engine.

        Args:
            config: Configuration manager.
            event_bus: EventBus for events.
            clob_client: Optional pre-configured CLOB client.
        """
        super().__init__()
        self._config = config
        self._event_bus = event_bus
        self._log = log.bind(component="execution_engine")

        # CLOB client
        if clob_client is None:
            settings = PolymarketSettings(
                private_key=config.get("polymarket.private_key", ""),
                api_key=config.get("polymarket.api_key", ""),
                api_secret=config.get("polymarket.api_secret", ""),
                api_passphrase=config.get("polymarket.api_passphrase", ""),
            )
            clob_client = CLOBClient(settings)

        self._clob = clob_client

        # Configuration
        self._dry_run = config.get_bool("mercury.dry_run", True)
        self._rebalance_enabled = config.get_bool("execution.rebalance_partial_fills", True)

        # State
        self._pending_orders: dict[str, OrderResult] = {}
        self._should_run = False

    async def start(self) -> None:
        """Start the execution engine."""
        self._start_time = time.time()
        self._should_run = True
        self._log.info("starting_execution_engine", dry_run=self._dry_run)

        # Connect to CLOB
        if not self._dry_run:
            await self._clob.connect()

        # Subscribe to approved signals
        await self._event_bus.subscribe("risk.approved.*", self._on_approved_signal)

        self._log.info("execution_engine_started")

    async def stop(self) -> None:
        """Stop the execution engine."""
        self._should_run = False

        # Cancel pending orders
        if not self._dry_run and self._pending_orders:
            self._log.info("cancelling_pending_orders", count=len(self._pending_orders))
            await self._clob.cancel_all_orders()

        # Close CLOB connection
        await self._clob.close()

        self._log.info("execution_engine_stopped")

    async def health_check(self) -> HealthCheckResult:
        """Check execution engine health."""
        if self._dry_run:
            return HealthCheckResult(
                status=HealthStatus.HEALTHY,
                message="Running in dry-run mode",
            )

        if not self._clob._connected:
            return HealthCheckResult(
                status=HealthStatus.UNHEALTHY,
                message="CLOB client not connected",
            )

        return HealthCheckResult(
            status=HealthStatus.HEALTHY,
            message="Connected and ready",
            details={"pending_orders": len(self._pending_orders)},
        )

    async def execute(self, signal: ApprovedSignal) -> ExecutionResult:
        """Execute an approved trading signal.

        Args:
            signal: The approved signal to execute.

        Returns:
            ExecutionResult with execution details.
        """
        start_time = time.time() * 1000

        self._log.info(
            "executing_signal",
            signal_id=signal.signal_id,
            signal_type=signal.signal_type.value,
            target_size=str(signal.target_size_usd),
            dry_run=self._dry_run,
        )

        # Generate IDs
        trade_id = f"trade-{uuid.uuid4().hex[:8]}"
        position_id = f"pos-{uuid.uuid4().hex[:8]}"

        try:
            if self._dry_run:
                result = await self._execute_dry_run(signal, trade_id, position_id)
            elif signal.signal_type == SignalType.ARBITRAGE:
                result = await self._execute_dual_leg(signal, trade_id, position_id)
            else:
                result = await self._execute_single_leg(signal, trade_id, position_id)

            result.execution_time_ms = time.time() * 1000 - start_time

            # Publish completion
            await self._event_bus.publish("execution.complete", {
                "signal_id": signal.signal_id,
                "trade_id": result.trade_id,
                "success": result.success,
                "total_cost": str(result.total_cost),
                "guaranteed_pnl": str(result.guaranteed_pnl),
                "execution_ms": result.execution_time_ms,
            })

            return result

        except Exception as e:
            self._log.error("execution_failed", signal_id=signal.signal_id, error=str(e))
            return ExecutionResult(
                success=False,
                signal_id=signal.signal_id,
                error=str(e),
                execution_time_ms=time.time() * 1000 - start_time,
            )

    async def _execute_dry_run(
        self,
        signal: ApprovedSignal,
        trade_id: str,
        position_id: str,
    ) -> ExecutionResult:
        """Simulate execution in dry-run mode."""
        await asyncio.sleep(0.1)  # Simulate latency

        # Simulate fill at expected prices
        yes_filled = signal.target_size_usd / 2 / signal.yes_price
        no_filled = signal.target_size_usd / 2 / signal.no_price
        total_cost = signal.target_size_usd
        guaranteed_pnl = min(yes_filled, no_filled) - total_cost

        self._log.info(
            "dry_run_execution",
            trade_id=trade_id,
            yes_filled=str(yes_filled),
            no_filled=str(no_filled),
            guaranteed_pnl=str(guaranteed_pnl),
        )

        # Publish position opened
        await self._event_bus.publish("position.opened", {
            "position_id": position_id,
            "trade_id": trade_id,
            "market_id": signal.market_id,
            "yes_shares": str(yes_filled),
            "no_shares": str(no_filled),
            "cost_basis": str(total_cost),
        })

        return ExecutionResult(
            success=True,
            signal_id=signal.signal_id,
            trade_id=trade_id,
            position_id=position_id,
            yes_filled=yes_filled,
            no_filled=no_filled,
            total_cost=total_cost,
            guaranteed_pnl=guaranteed_pnl,
        )

    async def _execute_dual_leg(
        self,
        signal: ApprovedSignal,
        trade_id: str,
        position_id: str,
    ) -> ExecutionResult:
        """Execute a dual-leg arbitrage order."""
        # Publish order submitted
        await self._event_bus.publish("order.submitted", {
            "trade_id": trade_id,
            "signal_id": signal.signal_id,
            "type": "dual_leg",
        })

        try:
            result = await self._clob.execute_dual_leg_order(
                yes_token_id=signal.yes_token_id,
                no_token_id=signal.no_token_id,
                amount_usd=signal.target_size_usd,
                yes_price=signal.yes_price,
                no_price=signal.no_price,
            )

            # Handle partial fills
            if result.has_partial_fill and self._rebalance_enabled:
                self._log.warning("partial_fill_detected", trade_id=trade_id)
                await self._handle_partial_fill(result, signal)

            # Publish fills
            await self._event_bus.publish("order.filled", {
                "trade_id": trade_id,
                "yes_order_id": result.yes_result.order_id,
                "no_order_id": result.no_result.order_id,
                "yes_filled": str(result.yes_result.filled_size),
                "no_filled": str(result.no_result.filled_size),
                "total_cost": str(result.total_cost),
            })

            # Create position if both filled
            if result.both_filled:
                await self._event_bus.publish("position.opened", {
                    "position_id": position_id,
                    "trade_id": trade_id,
                    "market_id": signal.market_id,
                    "yes_shares": str(result.yes_result.filled_size),
                    "no_shares": str(result.no_result.filled_size),
                    "cost_basis": str(result.total_cost),
                })

            return ExecutionResult(
                success=result.both_filled,
                signal_id=signal.signal_id,
                trade_id=trade_id,
                position_id=position_id if result.both_filled else None,
                yes_filled=result.yes_result.filled_size,
                no_filled=result.no_result.filled_size,
                total_cost=result.total_cost,
                guaranteed_pnl=result.guaranteed_pnl,
                execution_time_ms=result.execution_time_ms,
            )

        except InsufficientLiquidityError as e:
            self._log.warning("insufficient_liquidity", signal_id=signal.signal_id, error=str(e))
            await self._event_bus.publish("order.rejected", {
                "trade_id": trade_id,
                "signal_id": signal.signal_id,
                "reason": str(e),
            })
            return ExecutionResult(
                success=False,
                signal_id=signal.signal_id,
                error=str(e),
            )

    async def _execute_single_leg(
        self,
        signal: ApprovedSignal,
        trade_id: str,
        position_id: str,
    ) -> ExecutionResult:
        """Execute a single-leg directional order."""
        # Determine which token to trade
        if signal.signal_type == SignalType.BUY_YES:
            token_id = signal.yes_token_id
            price = signal.yes_price
        else:
            token_id = signal.no_token_id
            price = signal.no_price

        await self._event_bus.publish("order.submitted", {
            "trade_id": trade_id,
            "signal_id": signal.signal_id,
            "type": "single_leg",
            "token_id": token_id,
        })

        try:
            result = await self._clob.execute_order(
                token_id=token_id,
                side=OrderSide.BUY,
                amount_usd=signal.target_size_usd,
                price=price,
            )

            if result.status in (OrderStatus.FILLED, OrderStatus.MATCHED):
                await self._event_bus.publish("order.filled", {
                    "trade_id": trade_id,
                    "order_id": result.order_id,
                    "filled_size": str(result.filled_size),
                    "filled_cost": str(result.filled_cost),
                })

                return ExecutionResult(
                    success=True,
                    signal_id=signal.signal_id,
                    trade_id=trade_id,
                    yes_filled=result.filled_size if signal.signal_type == SignalType.BUY_YES else Decimal("0"),
                    no_filled=result.filled_size if signal.signal_type == SignalType.BUY_NO else Decimal("0"),
                    total_cost=result.filled_cost,
                )
            else:
                return ExecutionResult(
                    success=False,
                    signal_id=signal.signal_id,
                    error=f"Order status: {result.status.value}",
                )

        except Exception as e:
            await self._event_bus.publish("order.rejected", {
                "trade_id": trade_id,
                "signal_id": signal.signal_id,
                "reason": str(e),
            })
            return ExecutionResult(
                success=False,
                signal_id=signal.signal_id,
                error=str(e),
            )

    async def _handle_partial_fill(
        self,
        result: DualLegOrderResult,
        signal: ApprovedSignal,
    ) -> None:
        """Handle partial fill by attempting rebalance."""
        if result.yes_result.filled_size > 0 and result.no_result.filled_size == 0:
            # YES filled, NO didn't
            rebalance = await self._clob.rebalance_partial_fill(
                filled_token_id=signal.yes_token_id,
                unfilled_token_id=signal.no_token_id,
                filled_shares=result.yes_result.filled_size,
                filled_price=result.yes_result.requested_price,
                unfilled_price=result.no_result.requested_price,
            )
            self._log.info("rebalance_result", action=rebalance.get("action"))

        elif result.no_result.filled_size > 0 and result.yes_result.filled_size == 0:
            # NO filled, YES didn't
            rebalance = await self._clob.rebalance_partial_fill(
                filled_token_id=signal.no_token_id,
                unfilled_token_id=signal.yes_token_id,
                filled_shares=result.no_result.filled_size,
                filled_price=result.no_result.requested_price,
                unfilled_price=result.yes_result.requested_price,
            )
            self._log.info("rebalance_result", action=rebalance.get("action"))

    async def _on_approved_signal(self, data: dict) -> None:
        """Handle approved signal from RiskManager."""
        # Reconstruct ApprovedSignal from event data
        signal = ApprovedSignal(
            signal_id=data["signal_id"],
            original_signal_id=data.get("original_signal_id", data["signal_id"]),
            market_id=data["market_id"],
            signal_type=SignalType(data["signal_type"]),
            target_size_usd=Decimal(str(data["target_size_usd"])),
            yes_price=Decimal(str(data.get("yes_price", 0))),
            no_price=Decimal(str(data.get("no_price", 0))),
            yes_token_id=data.get("yes_token_id", ""),
            no_token_id=data.get("no_token_id", ""),
            approved_at=datetime.now(timezone.utc),
        )

        await self.execute(signal)

    async def cancel_order(self, order_id: str) -> bool:
        """Cancel an open order.

        Args:
            order_id: The order to cancel.

        Returns:
            True if cancelled.
        """
        if self._dry_run:
            self._log.info("dry_run_cancel", order_id=order_id)
            return True

        return await self._clob.cancel_order(order_id)

    def get_open_orders(self) -> list[OrderResult]:
        """Get all pending orders."""
        return list(self._pending_orders.values())
