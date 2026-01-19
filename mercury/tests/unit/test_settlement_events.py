"""Unit tests for settlement event dataclasses."""
import pytest
from datetime import datetime, timezone, timedelta
from decimal import Decimal

from mercury.domain.events import SettlementClaimedEvent, SettlementFailedEvent


class TestSettlementClaimedEvent:
    """Test SettlementClaimedEvent dataclass."""

    def test_create_basic_event(self):
        """Test creating a basic settlement claimed event."""
        event = SettlementClaimedEvent.create(
            position_id="pos-123",
            market_id="market-456",
            condition_id="cond-789",
            resolution="YES",
            proceeds=Decimal("10.00"),
            profit=Decimal("5.50"),
            side="YES",
        )

        assert event.position_id == "pos-123"
        assert event.market_id == "market-456"
        assert event.condition_id == "cond-789"
        assert event.resolution == "YES"
        assert event.proceeds == "10.00"
        assert event.profit == "5.50"
        assert event.side == "YES"
        assert event.dry_run is False
        assert event.attempts == 1
        assert event.tx_hash is None
        assert event.gas_used is None

    def test_create_with_transaction_details(self):
        """Test creating event with transaction details."""
        event = SettlementClaimedEvent.create(
            position_id="pos-123",
            market_id="market-456",
            condition_id="cond-789",
            resolution="NO",
            proceeds=Decimal("10.00"),
            profit=Decimal("4.50"),
            side="NO",
            tx_hash="0xabc123",
            gas_used=150000,
            attempts=2,
        )

        assert event.tx_hash == "0xabc123"
        assert event.gas_used == 150000
        assert event.attempts == 2

    def test_create_dry_run_event(self):
        """Test creating dry run event."""
        event = SettlementClaimedEvent.create(
            position_id="pos-123",
            market_id="market-456",
            condition_id="cond-789",
            resolution="YES",
            proceeds=Decimal("10.00"),
            profit=Decimal("5.50"),
            side="YES",
            dry_run=True,
        )

        assert event.dry_run is True
        assert event.tx_hash is None

    def test_create_with_custom_timestamp(self):
        """Test creating event with custom timestamp."""
        custom_time = datetime(2025, 1, 15, 12, 0, 0, tzinfo=timezone.utc)
        event = SettlementClaimedEvent.create(
            position_id="pos-123",
            market_id="market-456",
            condition_id="cond-789",
            resolution="YES",
            proceeds=Decimal("10.00"),
            profit=Decimal("5.50"),
            side="YES",
            timestamp=custom_time,
        )

        assert "2025-01-15T12:00:00" in event.timestamp

    def test_to_dict(self):
        """Test converting event to dictionary."""
        event = SettlementClaimedEvent.create(
            position_id="pos-123",
            market_id="market-456",
            condition_id="cond-789",
            resolution="YES",
            proceeds=Decimal("10.00"),
            profit=Decimal("5.50"),
            side="YES",
            tx_hash="0xabc123",
            gas_used=150000,
            dry_run=False,
            attempts=3,
        )

        d = event.to_dict()

        assert d["position_id"] == "pos-123"
        assert d["market_id"] == "market-456"
        assert d["condition_id"] == "cond-789"
        assert d["resolution"] == "YES"
        assert d["proceeds"] == "10.00"
        assert d["profit"] == "5.50"
        assert d["side"] == "YES"
        assert d["tx_hash"] == "0xabc123"
        assert d["gas_used"] == 150000
        assert d["dry_run"] is False
        assert d["attempts"] == 3
        assert "timestamp" in d

    def test_losing_position_event(self):
        """Test creating event for losing position."""
        event = SettlementClaimedEvent.create(
            position_id="pos-456",
            market_id="market-789",
            condition_id="cond-012",
            resolution="NO",
            proceeds=Decimal("0.00"),
            profit=Decimal("-4.50"),
            side="YES",  # YES side lost when market resolved NO
        )

        assert event.proceeds == "0.00"
        assert event.profit == "-4.50"

    def test_event_is_frozen(self):
        """Test that event is immutable."""
        event = SettlementClaimedEvent.create(
            position_id="pos-123",
            market_id="market-456",
            condition_id="cond-789",
            resolution="YES",
            proceeds=Decimal("10.00"),
            profit=Decimal("5.50"),
            side="YES",
        )

        with pytest.raises(AttributeError):
            event.position_id = "changed"


class TestSettlementFailedEvent:
    """Test SettlementFailedEvent dataclass."""

    def test_create_basic_event(self):
        """Test creating a basic settlement failed event."""
        event = SettlementFailedEvent.create(
            position_id="pos-123",
            reason="Network error",
            attempt_count=1,
        )

        assert event.position_id == "pos-123"
        assert event.reason == "Network error"
        assert event.attempt_count == 1
        assert event.is_permanent is False
        assert event.market_id is None
        assert event.condition_id is None
        assert event.max_attempts == 5

    def test_create_with_full_details(self):
        """Test creating event with all details."""
        next_retry = datetime.now(timezone.utc) + timedelta(minutes=5)
        event = SettlementFailedEvent.create(
            position_id="pos-123",
            reason="Transaction reverted",
            attempt_count=3,
            market_id="market-456",
            condition_id="cond-789",
            max_attempts=5,
            next_retry_at=next_retry,
        )

        assert event.position_id == "pos-123"
        assert event.reason == "Transaction reverted"
        assert event.attempt_count == 3
        assert event.market_id == "market-456"
        assert event.condition_id == "cond-789"
        assert event.max_attempts == 5
        assert event.is_permanent is False
        assert event.next_retry_at is not None

    def test_permanent_failure(self):
        """Test that is_permanent is set when max attempts reached."""
        event = SettlementFailedEvent.create(
            position_id="pos-123",
            reason="Repeated contract errors",
            attempt_count=5,
            max_attempts=5,
        )

        assert event.is_permanent is True
        assert event.attempt_count == 5

    def test_not_permanent_before_max(self):
        """Test that is_permanent is False before max attempts."""
        event = SettlementFailedEvent.create(
            position_id="pos-123",
            reason="Temporary error",
            attempt_count=4,
            max_attempts=5,
        )

        assert event.is_permanent is False

    def test_to_dict(self):
        """Test converting event to dictionary."""
        next_retry = datetime(2025, 1, 15, 12, 30, 0, tzinfo=timezone.utc)
        event = SettlementFailedEvent.create(
            position_id="pos-123",
            reason="Gas price too high",
            attempt_count=2,
            market_id="market-456",
            condition_id="cond-789",
            max_attempts=5,
            next_retry_at=next_retry,
        )

        d = event.to_dict()

        assert d["position_id"] == "pos-123"
        assert d["reason"] == "Gas price too high"
        assert d["attempt_count"] == 2
        assert d["market_id"] == "market-456"
        assert d["condition_id"] == "cond-789"
        assert d["max_attempts"] == 5
        assert d["is_permanent"] is False
        assert d["next_retry_at"] is not None
        assert "timestamp" in d

    def test_to_dict_without_optional_fields(self):
        """Test to_dict when optional fields are None."""
        event = SettlementFailedEvent.create(
            position_id="pos-123",
            reason="Unknown error",
            attempt_count=1,
        )

        d = event.to_dict()

        assert d["market_id"] is None
        assert d["condition_id"] is None
        assert d["next_retry_at"] is None

    def test_custom_max_attempts(self):
        """Test event with custom max attempts."""
        event = SettlementFailedEvent.create(
            position_id="pos-123",
            reason="Error",
            attempt_count=3,
            max_attempts=3,  # Custom lower max
        )

        assert event.max_attempts == 3
        assert event.is_permanent is True  # 3 >= 3

    def test_event_is_frozen(self):
        """Test that event is immutable."""
        event = SettlementFailedEvent.create(
            position_id="pos-123",
            reason="Error",
            attempt_count=1,
        )

        with pytest.raises(AttributeError):
            event.reason = "changed"


class TestSettlementEventIntegration:
    """Test settlement events work together for typical scenarios."""

    def test_claim_success_after_retry(self):
        """Test typical flow: failure then success."""
        # First attempt fails
        failed_event = SettlementFailedEvent.create(
            position_id="pos-123",
            reason="Network timeout",
            attempt_count=1,
            market_id="market-456",
            condition_id="cond-789",
        )

        assert failed_event.is_permanent is False
        assert failed_event.attempt_count == 1

        # Second attempt succeeds
        claimed_event = SettlementClaimedEvent.create(
            position_id="pos-123",
            market_id="market-456",
            condition_id="cond-789",
            resolution="YES",
            proceeds=Decimal("10.00"),
            profit=Decimal("5.50"),
            side="YES",
            attempts=2,  # This was the second attempt
        )

        assert claimed_event.attempts == 2

    def test_permanent_failure_after_max_retries(self):
        """Test permanent failure after exhausting retries."""
        # Simulate failures up to max
        for attempt in range(1, 6):
            event = SettlementFailedEvent.create(
                position_id="pos-123",
                reason=f"Error attempt {attempt}",
                attempt_count=attempt,
                max_attempts=5,
            )

            if attempt < 5:
                assert event.is_permanent is False
            else:
                assert event.is_permanent is True

    def test_event_data_matches_dictionary(self):
        """Test that dataclass fields match dictionary keys."""
        claimed = SettlementClaimedEvent.create(
            position_id="pos-123",
            market_id="market-456",
            condition_id="cond-789",
            resolution="YES",
            proceeds=Decimal("10.00"),
            profit=Decimal("5.50"),
            side="YES",
        )

        d = claimed.to_dict()

        # All dict keys should be valid event attributes
        for key in d:
            assert hasattr(claimed, key), f"Event missing attribute: {key}"
