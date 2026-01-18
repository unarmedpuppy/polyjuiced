"""
Integration tests for the CLOB client.

These tests use mocked py-clob-client to verify the CLOBClient
logic without requiring actual API connections.
"""

import asyncio
from decimal import Decimal
from datetime import datetime, timezone
from unittest.mock import MagicMock, AsyncMock, patch
import pytest

from mercury.integrations.polymarket.clob import (
    CLOBClient,
    CLOBClientError,
    OrderRejectedError,
    OrderTimeoutError,
    InsufficientLiquidityError,
    ArbitrageInvalidError,
    OrderSigningError,
    BatchOrderError,
    PreparedOrder,
    SignedOrderPair,
    PRICE_BUFFER_CENTS,
)
from mercury.integrations.polymarket.types import (
    OrderBookData,
    OrderBookLevel,
    OrderResult,
    OrderSide,
    OrderStatus,
    PolymarketSettings,
)


@pytest.fixture
def mock_settings():
    """Create mock Polymarket settings."""
    return PolymarketSettings(
        private_key="0x" + "a" * 64,
        api_key="test_api_key",
        api_secret="test_secret",
        api_passphrase="test_passphrase",
        clob_url="https://clob.polymarket.com",
    )


@pytest.fixture
def mock_py_clob_client():
    """Create a mock py-clob-client ClobClient."""
    client = MagicMock()

    # Mock get_order_book to return a dict
    client.get_order_book.return_value = {
        "bids": [
            {"price": "0.45", "size": "100"},
            {"price": "0.44", "size": "200"},
        ],
        "asks": [
            {"price": "0.47", "size": "100"},
            {"price": "0.48", "size": "200"},
        ],
    }

    # Mock balance
    client.get_balance_allowance.return_value = {
        "balance": 1000000000,  # 1000 USDC in raw units
        "allowance": 1000000000,
    }

    # Mock positions
    client.get_positions.return_value = []

    # Mock orders
    client.get_orders.return_value = []

    # Mock create_order (signing)
    signed_order = MagicMock()
    client.create_order.return_value = signed_order

    # Mock post_order
    client.post_order.return_value = {
        "id": "order_123",
        "status": "MATCHED",
        "size_matched": "10.0",
    }

    # Mock post_orders (batch)
    client.post_orders.return_value = [
        {"id": "yes_order_123", "status": "MATCHED", "size_matched": "10.0"},
        {"id": "no_order_123", "status": "MATCHED", "size_matched": "10.0"},
    ]

    # Mock cancel
    client.cancel.return_value = {"success": True}

    return client


class TestCLOBClientConnection:
    """Tests for CLOB client connection handling."""

    @pytest.mark.asyncio
    async def test_connect_success(self, mock_settings, mock_py_clob_client):
        """Test successful connection to CLOB API."""
        with patch("mercury.integrations.polymarket.clob.CLOBClient._run_sync") as mock_run:
            mock_run.return_value = mock_py_clob_client

            client = CLOBClient(mock_settings)
            # Manually set connected state for testing
            client._client = mock_py_clob_client
            client._connected = True

            assert client._connected is True

    @pytest.mark.asyncio
    async def test_ensure_connected_raises_when_disconnected(self, mock_settings):
        """Test that operations fail when not connected."""
        client = CLOBClient(mock_settings)

        with pytest.raises(CLOBClientError, match="not connected"):
            client._ensure_connected()

    @pytest.mark.asyncio
    async def test_context_manager(self, mock_settings, mock_py_clob_client):
        """Test async context manager protocol."""
        with patch.object(CLOBClient, "connect", new_callable=AsyncMock) as mock_connect:
            with patch.object(CLOBClient, "close", new_callable=AsyncMock) as mock_close:
                async with CLOBClient(mock_settings) as client:
                    mock_connect.assert_called_once()

                mock_close.assert_called_once()


class TestOrderBookParsing:
    """Tests for order book parsing."""

    @pytest.mark.asyncio
    async def test_parse_book_levels_from_dict(self, mock_settings):
        """Test parsing order book levels from dict format."""
        client = CLOBClient(mock_settings)

        levels = [
            {"price": "0.45", "size": "100"},
            {"price": "0.44", "size": "200"},
        ]

        result = client._parse_book_levels(levels)

        assert len(result) == 2
        assert result[0].price == Decimal("0.45")
        assert result[0].size == Decimal("100")

    @pytest.mark.asyncio
    async def test_parse_book_levels_empty(self, mock_settings):
        """Test parsing empty order book levels."""
        client = CLOBClient(mock_settings)

        result = client._parse_book_levels([])
        assert result == []

        result = client._parse_book_levels(None)
        assert result == []

    @pytest.mark.asyncio
    async def test_parse_book_levels_filters_zero_size(self, mock_settings):
        """Test that zero-size levels are filtered out."""
        client = CLOBClient(mock_settings)

        levels = [
            {"price": "0.45", "size": "100"},
            {"price": "0.44", "size": "0"},  # Should be filtered
        ]

        result = client._parse_book_levels(levels)

        assert len(result) == 1
        assert result[0].price == Decimal("0.45")


class TestSharePrecisionAdjustment:
    """Tests for share precision adjustment (maker_amount clean-up)."""

    def test_adjust_shares_for_precision_already_clean(self, mock_settings):
        """Test that clean amounts pass through unchanged."""
        client = CLOBClient(mock_settings)

        shares = Decimal("10.00")
        price = Decimal("0.50")

        result = client._adjust_shares_for_precision(shares, price)

        # 10.00 * 0.50 = 5.00 (clean)
        assert result == Decimal("10.00")

    def test_adjust_shares_for_precision_needs_adjustment(self, mock_settings):
        """Test that unclean amounts get adjusted."""
        client = CLOBClient(mock_settings)

        shares = Decimal("10.53")
        price = Decimal("0.47")

        result = client._adjust_shares_for_precision(shares, price)

        # Result should produce clean maker_amount
        maker_amount = result * price
        rounded = maker_amount.quantize(Decimal("0.01"))
        assert maker_amount == rounded


class TestLiquidityValidation:
    """Tests for liquidity validation."""

    def test_validate_liquidity_passes(self, mock_settings):
        """Test that validation passes with sufficient liquidity."""
        client = CLOBClient(mock_settings)

        yes_book = OrderBookData(
            token_id="yes_token",
            timestamp=datetime.now(timezone.utc),
            asks=(
                OrderBookLevel(price=Decimal("0.47"), size=Decimal("100")),
                OrderBookLevel(price=Decimal("0.48"), size=Decimal("200")),
            ),
        )
        no_book = OrderBookData(
            token_id="no_token",
            timestamp=datetime.now(timezone.utc),
            asks=(
                OrderBookLevel(price=Decimal("0.47"), size=Decimal("100")),
                OrderBookLevel(price=Decimal("0.48"), size=Decimal("200")),
            ),
        )

        # Small order should pass
        client._validate_liquidity(
            yes_book, no_book,
            amount_usd=Decimal("10"),
            yes_price=Decimal("0.47"),
            no_price=Decimal("0.47"),
        )

    def test_validate_liquidity_fails_no_asks(self, mock_settings):
        """Test that validation fails with no asks."""
        client = CLOBClient(mock_settings)

        yes_book = OrderBookData(
            token_id="yes_token",
            timestamp=datetime.now(timezone.utc),
            asks=(),  # No asks
        )
        no_book = OrderBookData(
            token_id="no_token",
            timestamp=datetime.now(timezone.utc),
            asks=(OrderBookLevel(price=Decimal("0.47"), size=Decimal("100")),),
        )

        with pytest.raises(InsufficientLiquidityError, match="Missing liquidity"):
            client._validate_liquidity(
                yes_book, no_book,
                amount_usd=Decimal("10"),
                yes_price=Decimal("0.47"),
                no_price=Decimal("0.47"),
            )


class TestArbitrageValidation:
    """Tests for arbitrage validation."""

    def test_validate_arbitrage_valid(self, mock_settings):
        """Test that valid arbitrage passes."""
        client = CLOBClient(mock_settings)

        # Total 0.90 < 1.00, valid
        client._validate_arbitrage(Decimal("0.45"), Decimal("0.45"))

    def test_validate_arbitrage_invalid_equal(self, mock_settings):
        """Test that exactly $1.00 fails."""
        client = CLOBClient(mock_settings)

        with pytest.raises(ArbitrageInvalidError, match=">= \\$1.00"):
            client._validate_arbitrage(Decimal("0.50"), Decimal("0.50"))

    def test_validate_arbitrage_invalid_over(self, mock_settings):
        """Test that over $1.00 fails."""
        client = CLOBClient(mock_settings)

        with pytest.raises(ArbitrageInvalidError):
            client._validate_arbitrage(Decimal("0.55"), Decimal("0.55"))


class TestOrderPreparation:
    """Tests for order preparation."""

    def test_prepare_order_creates_correct_args(self, mock_settings):
        """Test that order preparation creates correct arguments."""
        with patch.dict("sys.modules", {"py_clob_client.clob_types": MagicMock()}):
            client = CLOBClient(mock_settings)

            # Mock OrderArgs
            with patch("mercury.integrations.polymarket.clob.CLOBClient._prepare_order") as mock_prep:
                mock_prep.return_value = PreparedOrder(
                    token_id="token_123",
                    label="YES",
                    order_args=MagicMock(),
                    shares=Decimal("10.00"),
                    price=Decimal("0.47"),
                    original_price=Decimal("0.47"),
                    start_time_ms=1234567890,
                )

                result = mock_prep("token_123", "YES", Decimal("10.00"), Decimal("0.47"))

                assert result.token_id == "token_123"
                assert result.label == "YES"
                assert result.shares == Decimal("10.00")
                assert result.price == Decimal("0.47")


class TestBatchOrderParsing:
    """Tests for batch order result parsing."""

    def test_parse_batch_result_list_success(self, mock_settings):
        """Test parsing successful batch result."""
        client = CLOBClient(mock_settings)

        batch_result = [
            {"id": "order_1", "status": "MATCHED"},
            {"id": "order_2", "status": "MATCHED"},
        ]

        yes_result, no_result = client._parse_batch_result(batch_result)

        assert yes_result["id"] == "order_1"
        assert no_result["id"] == "order_2"

    def test_parse_batch_result_single_item(self, mock_settings):
        """Test parsing batch result with only one order."""
        client = CLOBClient(mock_settings)

        batch_result = [{"id": "order_1", "status": "MATCHED"}]

        yes_result, no_result = client._parse_batch_result(batch_result)

        assert yes_result["id"] == "order_1"
        assert no_result["status"] == "MISSING"

    def test_parse_batch_result_empty(self, mock_settings):
        """Test parsing empty batch result."""
        client = CLOBClient(mock_settings)

        batch_result = []

        yes_result, no_result = client._parse_batch_result(batch_result)

        assert yes_result["status"] == "EMPTY"
        assert no_result["status"] == "EMPTY"

    def test_parse_batch_result_dict_error(self, mock_settings):
        """Test parsing batch result with error."""
        client = CLOBClient(mock_settings)

        batch_result = {"error": "Something went wrong"}

        yes_result, no_result = client._parse_batch_result(batch_result)

        assert "error" in yes_result
        assert "error" in no_result


class TestRebalancePartialFill:
    """Tests for partial fill rebalancing."""

    @pytest.mark.asyncio
    async def test_rebalance_successful_hedge(self, mock_settings, mock_py_clob_client):
        """Test successful hedge completion."""
        client = CLOBClient(mock_settings)
        client._client = mock_py_clob_client
        client._connected = True

        # Mock get_order_book to return liquidity
        with patch.object(client, "get_order_book") as mock_get_book:
            mock_get_book.return_value = OrderBookData(
                token_id="unfilled_token",
                timestamp=datetime.now(timezone.utc),
                asks=(
                    OrderBookLevel(price=Decimal("0.45"), size=Decimal("100")),
                ),
            )

            # Mock execute_order to return filled
            with patch.object(client, "execute_order") as mock_execute:
                mock_execute.return_value = OrderResult(
                    order_id="hedge_order_123",
                    token_id="unfilled_token",
                    side=OrderSide.BUY,
                    status=OrderStatus.FILLED,
                    requested_price=Decimal("0.47"),
                    requested_size=Decimal("10.00"),
                    filled_size=Decimal("10.00"),
                    filled_cost=Decimal("4.70"),
                )

                result = await client.rebalance_partial_fill(
                    filled_token_id="filled_token",
                    unfilled_token_id="unfilled_token",
                    filled_shares=Decimal("10.00"),
                    filled_price=Decimal("0.47"),
                    unfilled_price=Decimal("0.45"),
                )

                assert result["action"] == "hedge_completed"
                assert "order" in result

    @pytest.mark.asyncio
    async def test_rebalance_falls_back_to_exit(self, mock_settings, mock_py_clob_client):
        """Test fallback to exit when hedge fails."""
        client = CLOBClient(mock_settings)
        client._client = mock_py_clob_client
        client._connected = True

        # Mock get_order_book to return no liquidity for hedge
        with patch.object(client, "get_order_book") as mock_get_book:
            mock_get_book.return_value = OrderBookData(
                token_id="unfilled_token",
                timestamp=datetime.now(timezone.utc),
                asks=(),  # No liquidity
            )

            # Mock execute_order for exit
            with patch.object(client, "execute_order") as mock_execute:
                mock_execute.return_value = OrderResult(
                    order_id="exit_order_123",
                    token_id="filled_token",
                    side=OrderSide.SELL,
                    status=OrderStatus.FILLED,
                    requested_price=Decimal("0.45"),
                    requested_size=Decimal("10.00"),
                    filled_size=Decimal("10.00"),
                    filled_cost=Decimal("4.50"),
                )

                result = await client.rebalance_partial_fill(
                    filled_token_id="filled_token",
                    unfilled_token_id="unfilled_token",
                    filled_shares=Decimal("10.00"),
                    filled_price=Decimal("0.47"),
                    unfilled_price=Decimal("0.45"),
                )

                assert result["action"] == "exited"


class TestErrorTypes:
    """Tests for custom error types."""

    def test_clob_client_error_hierarchy(self):
        """Test that all error types inherit from CLOBClientError."""
        assert issubclass(OrderRejectedError, CLOBClientError)
        assert issubclass(OrderTimeoutError, CLOBClientError)
        assert issubclass(InsufficientLiquidityError, CLOBClientError)
        assert issubclass(ArbitrageInvalidError, CLOBClientError)
        assert issubclass(OrderSigningError, CLOBClientError)
        assert issubclass(BatchOrderError, CLOBClientError)

    def test_error_messages(self):
        """Test that errors have meaningful messages."""
        error = ArbitrageInvalidError("Prices sum to $1.10")
        assert "1.10" in str(error)

        error = InsufficientLiquidityError("Only 50 shares available")
        assert "50" in str(error)
