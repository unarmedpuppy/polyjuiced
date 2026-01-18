"""Unit tests for Polymarket WebSocket client."""

import asyncio
import json
import time
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mercury.core.lifecycle import HealthStatus
from mercury.integrations.polymarket.types import PolymarketSettings
from mercury.integrations.polymarket.websocket import (
    ConnectionMetrics,
    HeartbeatState,
    PolymarketWebSocket,
    SubscriptionEntry,
    SubscriptionState,
    STALE_THRESHOLD,
)


@pytest.fixture
def mock_settings():
    """Create mock Polymarket settings."""
    return PolymarketSettings(
        private_key="0x" + "1" * 64,
        ws_url="wss://test.example.com/ws/market",
    )


@pytest.fixture
def mock_event_bus():
    """Create mock EventBus."""
    bus = MagicMock()
    bus.publish = AsyncMock()
    bus.subscribe = AsyncMock()
    return bus


@pytest.fixture
def mock_metrics():
    """Create mock MetricsEmitter."""
    metrics = MagicMock()
    metrics.update_websocket_status = MagicMock()
    metrics.record_websocket_reconnect = MagicMock()
    return metrics


@pytest.fixture
def ws_client(mock_settings, mock_event_bus, mock_metrics):
    """Create WebSocket client instance for testing."""
    return PolymarketWebSocket(
        settings=mock_settings,
        event_bus=mock_event_bus,
        metrics=mock_metrics,
    )


class TestHeartbeatState:
    """Tests for HeartbeatState dataclass."""

    def test_is_healthy_with_no_missed_pongs(self):
        state = HeartbeatState(missed_pongs=0)
        assert state.is_healthy is True

    def test_is_healthy_with_one_missed_pong(self):
        state = HeartbeatState(missed_pongs=1)
        assert state.is_healthy is True

    def test_is_unhealthy_with_two_missed_pongs(self):
        state = HeartbeatState(missed_pongs=2)
        assert state.is_healthy is False

    def test_seconds_since_pong_zero_when_no_pong(self):
        state = HeartbeatState(last_pong_received=0)
        assert state.seconds_since_pong == 0.0

    def test_seconds_since_pong_calculated(self):
        now = time.time()
        state = HeartbeatState(last_pong_received=now - 30)
        assert 29 <= state.seconds_since_pong <= 31

    def test_seconds_since_message_zero_when_no_message(self):
        state = HeartbeatState(last_message_received=0)
        assert state.seconds_since_message == 0.0

    def test_seconds_since_message_calculated(self):
        now = time.time()
        state = HeartbeatState(last_message_received=now - 15)
        assert 14 <= state.seconds_since_message <= 16


class TestConnectionMetrics:
    """Tests for ConnectionMetrics dataclass."""

    def test_reset_clears_counters(self):
        metrics = ConnectionMetrics(
            messages_received=100,
            messages_parsed=95,
            parse_errors=5,
            price_updates=50,
            book_updates=40,
            reconnect_count=2,  # Should NOT be reset
        )
        metrics.reset()

        assert metrics.messages_received == 0
        assert metrics.messages_parsed == 0
        assert metrics.parse_errors == 0
        assert metrics.price_updates == 0
        assert metrics.book_updates == 0
        # reconnect_count should persist
        assert metrics.reconnect_count == 2


class TestSubscriptionEntry:
    """Tests for SubscriptionEntry dataclass."""

    def test_default_state_is_pending(self):
        entry = SubscriptionEntry(token_id="abc123")
        assert entry.state == SubscriptionState.PENDING

    def test_timestamps_initially_none(self):
        entry = SubscriptionEntry(token_id="abc123")
        assert entry.confirmed_at is None
        assert entry.last_message_at is None


class TestPolymarketWebSocket:
    """Tests for PolymarketWebSocket class."""

    def test_initialization(self, ws_client, mock_settings):
        """Test client initializes with correct defaults."""
        assert ws_client._ws_url == mock_settings.ws_url
        assert ws_client._ws is None
        assert ws_client._should_run is False
        assert len(ws_client._subscriptions) == 0

    def test_is_connected_false_when_no_ws(self, ws_client):
        """Test is_connected returns False when no WebSocket."""
        assert ws_client.is_connected is False

    def test_is_connected_false_when_ws_closed(self, ws_client):
        """Test is_connected returns False when WebSocket closed."""
        mock_ws = MagicMock()
        mock_ws.open = False
        ws_client._ws = mock_ws
        assert ws_client.is_connected is False

    def test_is_connected_true_when_ws_open(self, ws_client):
        """Test is_connected returns True when WebSocket open."""
        mock_ws = MagicMock()
        mock_ws.open = True
        ws_client._ws = mock_ws
        assert ws_client.is_connected is True

    def test_active_subscriptions_empty_initially(self, ws_client):
        """Test active_subscriptions returns empty set initially."""
        assert ws_client.active_subscriptions == set()

    def test_active_subscriptions_returns_active_only(self, ws_client):
        """Test active_subscriptions filters by state."""
        ws_client._subscriptions = {
            "token1": SubscriptionEntry(token_id="token1", state=SubscriptionState.ACTIVE),
            "token2": SubscriptionEntry(token_id="token2", state=SubscriptionState.PENDING),
            "token3": SubscriptionEntry(token_id="token3", state=SubscriptionState.ACTIVE),
        }
        assert ws_client.active_subscriptions == {"token1", "token3"}

    def test_pending_subscriptions_returns_pending_only(self, ws_client):
        """Test pending_subscriptions filters by state."""
        ws_client._subscriptions = {
            "token1": SubscriptionEntry(token_id="token1", state=SubscriptionState.ACTIVE),
            "token2": SubscriptionEntry(token_id="token2", state=SubscriptionState.PENDING),
        }
        assert ws_client.pending_subscriptions == {"token2"}

    @pytest.mark.asyncio
    async def test_health_check_unhealthy_when_not_running(self, ws_client):
        """Test health_check returns unhealthy when client not running."""
        result = await ws_client.health_check()
        assert result.status == HealthStatus.UNHEALTHY
        assert "not running" in result.message.lower()

    @pytest.mark.asyncio
    async def test_health_check_unhealthy_when_disconnected(self, ws_client):
        """Test health_check returns unhealthy when disconnected."""
        ws_client._should_run = True
        result = await ws_client.health_check()
        assert result.status == HealthStatus.UNHEALTHY
        assert "not connected" in result.message.lower()

    @pytest.mark.asyncio
    async def test_health_check_degraded_when_heartbeat_unhealthy(self, ws_client):
        """Test health_check returns degraded when heartbeat fails."""
        ws_client._should_run = True
        mock_ws = MagicMock()
        mock_ws.open = True
        ws_client._ws = mock_ws
        ws_client._heartbeat.missed_pongs = 2
        ws_client._heartbeat.last_message_received = time.time()

        result = await ws_client.health_check()
        assert result.status == HealthStatus.DEGRADED
        assert "heartbeat" in result.message.lower()

    @pytest.mark.asyncio
    async def test_health_check_degraded_when_stale(self, ws_client):
        """Test health_check returns degraded when connection stale."""
        ws_client._should_run = True
        mock_ws = MagicMock()
        mock_ws.open = True
        ws_client._ws = mock_ws
        ws_client._heartbeat.last_message_received = time.time() - STALE_THRESHOLD - 10

        result = await ws_client.health_check()
        assert result.status == HealthStatus.DEGRADED
        assert "no messages" in result.message.lower()

    @pytest.mark.asyncio
    async def test_health_check_healthy_when_connected(self, ws_client):
        """Test health_check returns healthy when connected and receiving."""
        ws_client._should_run = True
        mock_ws = MagicMock()
        mock_ws.open = True
        ws_client._ws = mock_ws
        ws_client._heartbeat.last_message_received = time.time()
        ws_client._heartbeat.missed_pongs = 0

        result = await ws_client.health_check()
        assert result.status == HealthStatus.HEALTHY
        assert "connected" in result.message.lower()

    @pytest.mark.asyncio
    async def test_subscribe_creates_pending_entry(self, ws_client):
        """Test subscribe creates pending subscription entry."""
        await ws_client.subscribe(["token1", "token2"])

        assert "token1" in ws_client._subscriptions
        assert "token2" in ws_client._subscriptions
        assert ws_client._subscriptions["token1"].state == SubscriptionState.PENDING
        assert ws_client._subscriptions["token2"].state == SubscriptionState.PENDING

    @pytest.mark.asyncio
    async def test_subscribe_ignores_existing(self, ws_client):
        """Test subscribe doesn't duplicate existing subscriptions."""
        ws_client._subscriptions["token1"] = SubscriptionEntry(
            token_id="token1",
            state=SubscriptionState.ACTIVE,
        )

        await ws_client.subscribe(["token1"])
        # Should still be active, not reset to pending
        assert ws_client._subscriptions["token1"].state == SubscriptionState.ACTIVE

    @pytest.mark.asyncio
    async def test_subscribe_sends_message_when_connected(self, ws_client):
        """Test subscribe sends WebSocket message when connected."""
        mock_ws = MagicMock()
        mock_ws.open = True
        mock_ws.send = AsyncMock()
        ws_client._ws = mock_ws

        await ws_client.subscribe(["token1"])

        mock_ws.send.assert_called_once()
        sent_data = json.loads(mock_ws.send.call_args[0][0])
        assert sent_data["type"] == "market"
        assert "token1" in sent_data["assets_ids"]

    @pytest.mark.asyncio
    async def test_unsubscribe_removes_entry(self, ws_client):
        """Test unsubscribe removes subscription entry."""
        ws_client._subscriptions["token1"] = SubscriptionEntry(
            token_id="token1",
            state=SubscriptionState.ACTIVE,
        )

        await ws_client.unsubscribe(["token1"])

        assert "token1" not in ws_client._subscriptions

    @pytest.mark.asyncio
    async def test_process_message_handles_pong(self, ws_client):
        """Test PONG message updates heartbeat state."""
        ws_client._heartbeat.missed_pongs = 1
        initial_pong_count = ws_client._heartbeat.pong_count

        await ws_client._process_message("PONG")

        assert ws_client._heartbeat.pong_count == initial_pong_count + 1
        assert ws_client._heartbeat.missed_pongs == 0

    @pytest.mark.asyncio
    async def test_process_message_handles_ping(self, ws_client):
        """Test PING message sends PONG response."""
        mock_ws = MagicMock()
        mock_ws.send = AsyncMock()
        ws_client._ws = mock_ws

        await ws_client._process_message("PING")

        mock_ws.send.assert_called_once_with("PONG")

    @pytest.mark.asyncio
    async def test_process_message_handles_price_changes(self, ws_client, mock_event_bus):
        """Test price_changes message publishes to EventBus."""
        message = json.dumps({
            "price_changes": [
                {"asset_id": "token123", "best_bid": "0.50", "best_ask": "0.52"}
            ]
        })

        await ws_client._process_message(message)

        mock_event_bus.publish.assert_called()
        call_args = mock_event_bus.publish.call_args_list
        # Should have published to market.price.token123
        assert any("market.price.token123" in str(call) for call in call_args)

    @pytest.mark.asyncio
    async def test_process_message_handles_book_snapshot(self, ws_client, mock_event_bus):
        """Test book snapshot message publishes to EventBus."""
        message = json.dumps({
            "asset_id": "token456",
            "bids": [{"price": "0.49", "size": "100"}],
            "asks": [{"price": "0.51", "size": "100"}],
        })

        await ws_client._process_message(message)

        mock_event_bus.publish.assert_called()
        call_args = mock_event_bus.publish.call_args_list
        assert any("market.book.token456" in str(call) for call in call_args)

    @pytest.mark.asyncio
    async def test_handle_subscription_confirmed_updates_state(self, ws_client):
        """Test subscription confirmation updates entry state."""
        ws_client._subscriptions["token789"] = SubscriptionEntry(
            token_id="token789",
            state=SubscriptionState.PENDING,
        )

        ws_client._handle_subscription_confirmed({
            "type": "subscribed",
            "assets_ids": ["token789"]
        })

        assert ws_client._subscriptions["token789"].state == SubscriptionState.ACTIVE
        assert ws_client._subscriptions["token789"].confirmed_at is not None

    @pytest.mark.asyncio
    async def test_handle_price_change_updates_subscription_state(self, ws_client, mock_event_bus):
        """Test receiving price data confirms pending subscription."""
        ws_client._subscriptions["token999"] = SubscriptionEntry(
            token_id="token999",
            state=SubscriptionState.PENDING,
        )

        await ws_client._handle_price_change({
            "asset_id": "token999",
            "best_bid": "0.45",
            "best_ask": "0.55",
        })

        assert ws_client._subscriptions["token999"].state == SubscriptionState.ACTIVE
        assert ws_client._subscriptions["token999"].last_message_at is not None

    @pytest.mark.asyncio
    async def test_handle_price_change_parses_formats(self, ws_client, mock_event_bus):
        """Test price change handles multiple formats."""
        # Format 1: best_bid/best_ask
        await ws_client._handle_price_change({
            "asset_id": "token1",
            "best_bid": "0.50",
            "best_ask": "0.52",
        })
        mock_event_bus.publish.assert_called()

        mock_event_bus.reset_mock()

        # Format 2: bid/ask
        await ws_client._handle_price_change({
            "token_id": "token2",
            "bid": "0.48",
            "ask": "0.54",
        })
        mock_event_bus.publish.assert_called()

        mock_event_bus.reset_mock()

        # Format 3: price with side
        await ws_client._handle_price_change({
            "asset_id": "token3",
            "price": "0.51",
            "side": "bid",
        })
        mock_event_bus.publish.assert_called()

    def test_parse_levels_dict_format(self, ws_client):
        """Test _parse_levels handles dict format."""
        levels = [
            {"price": "0.50", "size": "100"},
            {"price": "0.51", "size": "200"},
        ]
        result = ws_client._parse_levels(levels)

        assert len(result) == 2
        assert result[0].price == Decimal("0.50")
        assert result[0].size == Decimal("100")

    def test_parse_levels_list_format(self, ws_client):
        """Test _parse_levels handles list format."""
        levels = [
            [0.50, 100],
            [0.51, 200],
        ]
        result = ws_client._parse_levels(levels)

        assert len(result) == 2
        assert result[0].price == Decimal("0.50")
        assert result[0].size == Decimal("100")

    def test_parse_levels_filters_zero_size(self, ws_client):
        """Test _parse_levels filters out zero-size levels."""
        levels = [
            {"price": "0.50", "size": "100"},
            {"price": "0.51", "size": "0"},  # Should be filtered
        ]
        result = ws_client._parse_levels(levels)

        assert len(result) == 1

    def test_get_subscription_info(self, ws_client):
        """Test get_subscription_info returns expected structure."""
        now = time.time()
        ws_client._subscriptions = {
            "active1": SubscriptionEntry(
                token_id="active1",
                state=SubscriptionState.ACTIVE,
                confirmed_at=now,
                last_message_at=now,
            ),
            "pending1": SubscriptionEntry(
                token_id="pending1",
                state=SubscriptionState.PENDING,
                subscribed_at=now,
            ),
        }
        ws_client._conn_metrics.messages_received = 50
        ws_client._heartbeat.ping_count = 10

        info = ws_client.get_subscription_info()

        assert len(info["active"]) == 1
        assert info["active"][0]["token_id"] == "active1"
        assert len(info["pending"]) == 1
        assert info["pending"][0]["token_id"] == "pending1"
        assert info["connection_metrics"]["messages_received"] == 50
        assert info["heartbeat"]["ping_count"] == 10

    @pytest.mark.asyncio
    async def test_connect_publishes_event(self, ws_client, mock_event_bus, mock_metrics):
        """Test connect publishes connection event to EventBus."""
        with patch("websockets.connect", new_callable=AsyncMock) as mock_connect:
            mock_ws = MagicMock()
            mock_ws.open = True
            mock_connect.return_value = mock_ws

            await ws_client._connect()

            # Check EventBus received connection event
            mock_event_bus.publish.assert_called()
            call_args = mock_event_bus.publish.call_args_list
            assert any("market.ws.connected" in str(call) for call in call_args)

            # Check metrics were updated
            mock_metrics.update_websocket_status.assert_called_with(True)

    @pytest.mark.asyncio
    async def test_handle_disconnect_increments_reconnect_count(
        self, ws_client, mock_event_bus, mock_metrics
    ):
        """Test _handle_disconnect increments reconnect counter and publishes event."""
        ws_client._should_run = False  # Prevent actual sleep
        initial_count = ws_client._conn_metrics.reconnect_count

        await ws_client._handle_disconnect()

        assert ws_client._conn_metrics.reconnect_count == initial_count + 1
        mock_metrics.record_websocket_reconnect.assert_called_once()

        # Check disconnection event published
        call_args = mock_event_bus.publish.call_args_list
        assert any("market.ws.disconnected" in str(call) for call in call_args)

    @pytest.mark.asyncio
    async def test_start_sets_should_run_flag(self, ws_client):
        """Test start sets _should_run flag."""
        # Mock the tasks to prevent actual execution
        with patch.object(ws_client, "_message_loop", new_callable=AsyncMock):
            with patch.object(ws_client, "_heartbeat_loop", new_callable=AsyncMock):
                await ws_client.start()

        assert ws_client._should_run is True

    @pytest.mark.asyncio
    async def test_stop_clears_should_run_flag(self, ws_client):
        """Test stop clears _should_run flag."""
        ws_client._should_run = True
        ws_client._message_task = None
        ws_client._heartbeat_task = None

        await ws_client.stop()

        assert ws_client._should_run is False
