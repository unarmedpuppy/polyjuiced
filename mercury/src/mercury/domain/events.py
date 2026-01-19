"""Event payload dataclasses for EventBus publishing.

These dataclasses define the structure of events published by services
to the Redis EventBus. They are designed to be serialized to JSON and
consumed by any subscriber.

Event Channel Naming Convention:
- market.orderbook.{market_id} - Order book snapshots
- market.trade.{market_id} - Trade events
- market.stale.{market_id} - Stale data alerts
- market.fresh.{market_id} - Fresh data recovery notifications
- settlement.claimed - Position successfully claimed
- settlement.failed - Claim attempt failed
- settlement.queued - New position queued for settlement
- settlement.alert - Alert for repeated claim failures
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional


@dataclass(frozen=True)
class OrderBookSnapshotEvent:
    """Order book snapshot event payload.

    Published to: market.orderbook.{market_id}

    This event is emitted whenever the order book state changes for a market.
    It contains the best bid/ask prices for both YES and NO outcomes, along
    with derived metrics like combined ask and arbitrage spread.

    Attributes:
        market_id: The market's condition ID.
        timestamp: When this snapshot was captured (ISO format).
        yes_best_bid: Best bid price for YES outcome (None if no bids).
        yes_best_ask: Best ask price for YES outcome (None if no asks).
        no_best_bid: Best bid price for NO outcome (None if no bids).
        no_best_ask: Best ask price for NO outcome (None if no asks).
        combined_ask: Sum of yes_best_ask + no_best_ask (None if either missing).
        arbitrage_spread_cents: (1 - combined_ask) * 100 in cents (None if no arb).
        yes_bid_size: Size available at yes best bid.
        yes_ask_size: Size available at yes best ask.
        no_bid_size: Size available at no best bid.
        no_ask_size: Size available at no best ask.
        sequence: Monotonically increasing sequence number for ordering.
    """

    market_id: str
    timestamp: str  # ISO format string for JSON serialization
    yes_best_bid: Optional[str] = None  # String for Decimal serialization
    yes_best_ask: Optional[str] = None
    no_best_bid: Optional[str] = None
    no_best_ask: Optional[str] = None
    combined_ask: Optional[str] = None
    arbitrage_spread_cents: Optional[str] = None
    yes_bid_size: Optional[str] = None
    yes_ask_size: Optional[str] = None
    no_bid_size: Optional[str] = None
    no_ask_size: Optional[str] = None
    sequence: int = 0

    @classmethod
    def from_market_book(
        cls,
        market_id: str,
        yes_best_bid: Optional[Decimal],
        yes_best_ask: Optional[Decimal],
        no_best_bid: Optional[Decimal],
        no_best_ask: Optional[Decimal],
        yes_bid_size: Optional[Decimal] = None,
        yes_ask_size: Optional[Decimal] = None,
        no_bid_size: Optional[Decimal] = None,
        no_ask_size: Optional[Decimal] = None,
        sequence: int = 0,
        timestamp: Optional[datetime] = None,
    ) -> "OrderBookSnapshotEvent":
        """Create an OrderBookSnapshotEvent from market book data.

        Args:
            market_id: The market's condition ID.
            yes_best_bid: Best bid for YES outcome.
            yes_best_ask: Best ask for YES outcome.
            no_best_bid: Best bid for NO outcome.
            no_best_ask: Best ask for NO outcome.
            yes_bid_size: Size at yes best bid.
            yes_ask_size: Size at yes best ask.
            no_bid_size: Size at no best bid.
            no_ask_size: Size at no best ask.
            sequence: Sequence number.
            timestamp: Event timestamp (defaults to now).

        Returns:
            OrderBookSnapshotEvent instance.
        """
        ts = timestamp or datetime.now(timezone.utc)

        # Calculate derived fields
        combined_ask: Optional[Decimal] = None
        arbitrage_spread_cents: Optional[Decimal] = None

        if yes_best_ask is not None and no_best_ask is not None:
            combined_ask = yes_best_ask + no_best_ask
            # Arbitrage spread = 1 - combined_ask (positive = opportunity)
            spread = Decimal("1") - combined_ask
            arbitrage_spread_cents = spread * Decimal("100")

        return cls(
            market_id=market_id,
            timestamp=ts.isoformat(),
            yes_best_bid=str(yes_best_bid) if yes_best_bid is not None else None,
            yes_best_ask=str(yes_best_ask) if yes_best_ask is not None else None,
            no_best_bid=str(no_best_bid) if no_best_bid is not None else None,
            no_best_ask=str(no_best_ask) if no_best_ask is not None else None,
            combined_ask=str(combined_ask) if combined_ask is not None else None,
            arbitrage_spread_cents=str(arbitrage_spread_cents) if arbitrage_spread_cents is not None else None,
            yes_bid_size=str(yes_bid_size) if yes_bid_size is not None else None,
            yes_ask_size=str(yes_ask_size) if yes_ask_size is not None else None,
            no_bid_size=str(no_bid_size) if no_bid_size is not None else None,
            no_ask_size=str(no_ask_size) if no_ask_size is not None else None,
            sequence=sequence,
        )


@dataclass(frozen=True)
class TradeEvent:
    """Trade event payload.

    Published to: market.trade.{market_id}

    This event is emitted when a trade is detected on the market.
    Trade detection can come from WebSocket trade feeds or order book changes.

    Attributes:
        market_id: The market's condition ID.
        timestamp: When the trade occurred (ISO format).
        token_id: The token that was traded (YES or NO token ID).
        side: Trade side from taker's perspective ("buy" or "sell").
        price: Trade execution price.
        size: Trade size in tokens.
        trade_id: Unique trade identifier (if available from exchange).
        maker_order_id: Maker order ID (if available).
        taker_order_id: Taker order ID (if available).
    """

    market_id: str
    timestamp: str  # ISO format string
    token_id: str
    side: str  # "buy" or "sell"
    price: str  # String for Decimal serialization
    size: str
    trade_id: Optional[str] = None
    maker_order_id: Optional[str] = None
    taker_order_id: Optional[str] = None

    @classmethod
    def create(
        cls,
        market_id: str,
        token_id: str,
        side: str,
        price: Decimal,
        size: Decimal,
        trade_id: Optional[str] = None,
        maker_order_id: Optional[str] = None,
        taker_order_id: Optional[str] = None,
        timestamp: Optional[datetime] = None,
    ) -> "TradeEvent":
        """Create a TradeEvent.

        Args:
            market_id: The market's condition ID.
            token_id: The token that was traded.
            side: Trade side ("buy" or "sell").
            price: Trade execution price.
            size: Trade size.
            trade_id: Optional trade identifier.
            maker_order_id: Optional maker order ID.
            taker_order_id: Optional taker order ID.
            timestamp: Event timestamp (defaults to now).

        Returns:
            TradeEvent instance.

        Raises:
            ValueError: If side is not "buy" or "sell".
        """
        if side not in ("buy", "sell"):
            raise ValueError(f"side must be 'buy' or 'sell', got '{side}'")

        ts = timestamp or datetime.now(timezone.utc)

        return cls(
            market_id=market_id,
            timestamp=ts.isoformat(),
            token_id=token_id,
            side=side,
            price=str(price),
            size=str(size),
            trade_id=trade_id,
            maker_order_id=maker_order_id,
            taker_order_id=taker_order_id,
        )


@dataclass(frozen=True)
class StaleAlert:
    """Stale data alert event payload.

    Published to: market.stale.{market_id}

    This event is emitted when a market's data becomes stale (no updates
    received within the configured threshold, default 10 seconds).

    Strategies should monitor this channel and pause trading when a market
    becomes stale to avoid making decisions on outdated data.

    Attributes:
        market_id: The market's condition ID.
        timestamp: When staleness was detected (ISO format).
        age_seconds: How long since the last update (seconds).
        threshold_seconds: The configured staleness threshold (seconds).
        last_update_timestamp: When the last update was received (ISO format).
    """

    market_id: str
    timestamp: str  # ISO format string
    age_seconds: float
    threshold_seconds: float
    last_update_timestamp: Optional[str] = None  # ISO format, None if never updated

    @classmethod
    def create(
        cls,
        market_id: str,
        age_seconds: float,
        threshold_seconds: float,
        last_update_time: Optional[float] = None,
        timestamp: Optional[datetime] = None,
    ) -> "StaleAlert":
        """Create a StaleAlert.

        Args:
            market_id: The market's condition ID.
            age_seconds: How long since the last update.
            threshold_seconds: The staleness threshold.
            last_update_time: Unix timestamp of last update (optional).
            timestamp: Event timestamp (defaults to now).

        Returns:
            StaleAlert instance.
        """
        ts = timestamp or datetime.now(timezone.utc)

        last_update_iso: Optional[str] = None
        if last_update_time is not None and last_update_time > 0:
            last_update_iso = datetime.fromtimestamp(
                last_update_time, tz=timezone.utc
            ).isoformat()

        return cls(
            market_id=market_id,
            timestamp=ts.isoformat(),
            age_seconds=age_seconds,
            threshold_seconds=threshold_seconds,
            last_update_timestamp=last_update_iso,
        )


@dataclass(frozen=True)
class FreshAlert:
    """Fresh data recovery event payload.

    Published to: market.fresh.{market_id}

    This event is emitted when a previously stale market starts receiving
    data again. Strategies can use this to resume trading on the market.

    Attributes:
        market_id: The market's condition ID.
        timestamp: When freshness was detected (ISO format).
        age_seconds: Current age of data (seconds since last update).
        stale_duration_seconds: How long the market was stale (seconds).
    """

    market_id: str
    timestamp: str  # ISO format string
    age_seconds: float
    stale_duration_seconds: Optional[float] = None

    @classmethod
    def create(
        cls,
        market_id: str,
        age_seconds: float,
        stale_duration_seconds: Optional[float] = None,
        timestamp: Optional[datetime] = None,
    ) -> "FreshAlert":
        """Create a FreshAlert.

        Args:
            market_id: The market's condition ID.
            age_seconds: Current age of data.
            stale_duration_seconds: How long the market was stale.
            timestamp: Event timestamp (defaults to now).

        Returns:
            FreshAlert instance.
        """
        ts = timestamp or datetime.now(timezone.utc)

        return cls(
            market_id=market_id,
            timestamp=ts.isoformat(),
            age_seconds=age_seconds,
            stale_duration_seconds=stale_duration_seconds,
        )


@dataclass(frozen=True)
class SettlementClaimedEvent:
    """Settlement claimed event payload.

    Published to: settlement.claimed

    This event is emitted when a position is successfully claimed after
    market resolution. Contains the proceeds and profit from the settlement.

    Attributes:
        position_id: Unique position identifier.
        market_id: Market identifier.
        condition_id: Market condition ID (for CTF redemption).
        resolution: Market resolution ("YES" or "NO").
        proceeds: Settlement proceeds in USD (string for Decimal serialization).
        profit: Settlement profit/loss in USD (can be negative).
        side: Position side ("YES" or "NO").
        timestamp: When the claim was processed (ISO format).
        tx_hash: Transaction hash (if on-chain, None for dry run).
        gas_used: Gas used for the transaction (if on-chain).
        dry_run: Whether this was a simulated claim.
        attempts: Number of attempts before successful claim.
    """

    position_id: str
    market_id: str
    condition_id: str
    resolution: str
    proceeds: str  # String for Decimal serialization
    profit: str
    side: str
    timestamp: str  # ISO format string
    tx_hash: Optional[str] = None
    gas_used: Optional[int] = None
    dry_run: bool = False
    attempts: int = 1

    @classmethod
    def create(
        cls,
        position_id: str,
        market_id: str,
        condition_id: str,
        resolution: str,
        proceeds: Decimal,
        profit: Decimal,
        side: str,
        tx_hash: Optional[str] = None,
        gas_used: Optional[int] = None,
        dry_run: bool = False,
        attempts: int = 1,
        timestamp: Optional[datetime] = None,
    ) -> "SettlementClaimedEvent":
        """Create a SettlementClaimedEvent.

        Args:
            position_id: Unique position identifier.
            market_id: Market identifier.
            condition_id: Market condition ID.
            resolution: Market resolution ("YES" or "NO").
            proceeds: Settlement proceeds in USD.
            profit: Settlement profit/loss in USD.
            side: Position side ("YES" or "NO").
            tx_hash: Transaction hash (if on-chain).
            gas_used: Gas used (if on-chain).
            dry_run: Whether this was a simulated claim.
            attempts: Number of attempts before success.
            timestamp: Event timestamp (defaults to now).

        Returns:
            SettlementClaimedEvent instance.
        """
        ts = timestamp or datetime.now(timezone.utc)

        return cls(
            position_id=position_id,
            market_id=market_id,
            condition_id=condition_id,
            resolution=resolution,
            proceeds=str(proceeds),
            profit=str(profit),
            side=side,
            timestamp=ts.isoformat(),
            tx_hash=tx_hash,
            gas_used=gas_used,
            dry_run=dry_run,
            attempts=attempts,
        )

    def to_dict(self) -> dict:
        """Convert to dictionary for event publishing.

        Returns:
            Dictionary representation suitable for JSON serialization.
        """
        return {
            "position_id": self.position_id,
            "market_id": self.market_id,
            "condition_id": self.condition_id,
            "resolution": self.resolution,
            "proceeds": self.proceeds,
            "profit": self.profit,
            "side": self.side,
            "timestamp": self.timestamp,
            "tx_hash": self.tx_hash,
            "gas_used": self.gas_used,
            "dry_run": self.dry_run,
            "attempts": self.attempts,
        }


@dataclass(frozen=True)
class SettlementFailedEvent:
    """Settlement failed event payload.

    Published to: settlement.failed

    This event is emitted when a claim attempt fails. Contains the error
    reason and current attempt count for retry tracking.

    Attributes:
        position_id: Unique position identifier.
        market_id: Market identifier.
        condition_id: Market condition ID.
        reason: Error message describing the failure.
        attempt_count: Current attempt number (after this failure).
        max_attempts: Maximum allowed attempts before permanent failure.
        timestamp: When the failure occurred (ISO format).
        is_permanent: Whether this failure is permanent (max attempts reached).
        next_retry_at: When the next retry will be attempted (ISO format).
    """

    position_id: str
    reason: str
    attempt_count: int
    timestamp: str  # ISO format string
    market_id: Optional[str] = None
    condition_id: Optional[str] = None
    max_attempts: int = 5
    is_permanent: bool = False
    next_retry_at: Optional[str] = None

    @classmethod
    def create(
        cls,
        position_id: str,
        reason: str,
        attempt_count: int,
        market_id: Optional[str] = None,
        condition_id: Optional[str] = None,
        max_attempts: int = 5,
        next_retry_at: Optional[datetime] = None,
        timestamp: Optional[datetime] = None,
    ) -> "SettlementFailedEvent":
        """Create a SettlementFailedEvent.

        Args:
            position_id: Unique position identifier.
            reason: Error message describing the failure.
            attempt_count: Current attempt number.
            market_id: Market identifier.
            condition_id: Market condition ID.
            max_attempts: Maximum allowed attempts.
            next_retry_at: When the next retry will be attempted.
            timestamp: Event timestamp (defaults to now).

        Returns:
            SettlementFailedEvent instance.
        """
        ts = timestamp or datetime.now(timezone.utc)
        is_permanent = attempt_count >= max_attempts

        return cls(
            position_id=position_id,
            reason=reason,
            attempt_count=attempt_count,
            timestamp=ts.isoformat(),
            market_id=market_id,
            condition_id=condition_id,
            max_attempts=max_attempts,
            is_permanent=is_permanent,
            next_retry_at=next_retry_at.isoformat() if next_retry_at else None,
        )

    def to_dict(self) -> dict:
        """Convert to dictionary for event publishing.

        Returns:
            Dictionary representation suitable for JSON serialization.
        """
        return {
            "position_id": self.position_id,
            "market_id": self.market_id,
            "condition_id": self.condition_id,
            "reason": self.reason,
            "attempt_count": self.attempt_count,
            "max_attempts": self.max_attempts,
            "is_permanent": self.is_permanent,
            "next_retry_at": self.next_retry_at,
            "timestamp": self.timestamp,
        }
