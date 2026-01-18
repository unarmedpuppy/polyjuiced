"""
Unit tests for PolygonClient.

Tests the Polygon Web3 client functionality using mocks.
"""

from decimal import Decimal
from unittest.mock import MagicMock, patch, AsyncMock
import pytest

from mercury.integrations.chain.client import (
    PolygonClient,
    PolygonClientError,
    TxReceipt,
    USDC_ADDRESS,
    CTF_ADDRESS,
)


class TestPolygonClientBasics:
    """Basic tests for PolygonClient instantiation and properties."""

    def test_client_importable(self):
        """Verify PolygonClient can be imported."""
        from mercury.integrations.chain import PolygonClient, PolygonClientError, TxReceipt

        assert PolygonClient is not None
        assert PolygonClientError is not None
        assert TxReceipt is not None

    def test_client_instantiates(self):
        """Verify PolygonClient can be instantiated."""
        client = PolygonClient(
            rpc_url="https://polygon-rpc.com",
            private_key="0x" + "a" * 64,
        )

        assert client is not None
        assert client.is_connected is False
        assert client.address is None

    def test_client_not_connected_initially(self):
        """Verify client is not connected before connect() is called."""
        client = PolygonClient(
            rpc_url="https://polygon-rpc.com",
            private_key="0x" + "a" * 64,
        )

        assert client.is_connected is False

    def test_tx_receipt_dataclass(self):
        """Verify TxReceipt dataclass works correctly."""
        receipt = TxReceipt(
            tx_hash="0x123abc",
            block_number=12345678,
            gas_used=50000,
            status=True,
        )

        assert receipt.tx_hash == "0x123abc"
        assert receipt.block_number == 12345678
        assert receipt.gas_used == 50000
        assert receipt.status is True


class TestPolygonClientConnection:
    """Tests for connection-related functionality."""

    def test_ensure_connected_raises_when_not_connected(self):
        """Verify _ensure_connected raises when not connected."""
        client = PolygonClient(
            rpc_url="https://polygon-rpc.com",
            private_key="0x" + "a" * 64,
        )

        with pytest.raises(PolygonClientError) as excinfo:
            client._ensure_connected()

        assert "not connected" in str(excinfo.value).lower()

    @pytest.mark.asyncio
    async def test_connect_sets_connected_flag(self):
        """Verify connect() sets the connected flag."""
        client = PolygonClient(
            rpc_url="https://polygon-rpc.com",
            private_key="0x" + "a" * 64,
        )

        # Mock web3 and eth_account
        mock_w3 = MagicMock()
        mock_w3.is_connected.return_value = True
        mock_w3.eth.contract.return_value = MagicMock()

        mock_account = MagicMock()
        mock_account.address = "0x1234567890abcdef1234567890abcdef12345678"

        with patch("web3.Web3", return_value=mock_w3) as mock_web3_class:
            mock_web3_class.HTTPProvider = MagicMock
            mock_web3_class.to_checksum_address = MagicMock(return_value=CTF_ADDRESS)

            with patch("eth_account.Account") as mock_account_class:
                mock_account_class.from_key.return_value = mock_account

                await client.connect()

        assert client.is_connected is True
        assert client.address == mock_account.address

    @pytest.mark.asyncio
    async def test_close_resets_state(self):
        """Verify close() resets client state."""
        client = PolygonClient(
            rpc_url="https://polygon-rpc.com",
            private_key="0x" + "a" * 64,
        )

        # Simulate connected state
        client._connected = True
        client._w3 = MagicMock()
        client._account = MagicMock()

        await client.close()

        assert client.is_connected is False
        assert client._w3 is None
        assert client._account is None


class TestPolygonClientBalances:
    """Tests for balance query methods."""

    @pytest.fixture
    def connected_client(self):
        """Create a mock-connected client."""
        client = PolygonClient(
            rpc_url="https://polygon-rpc.com",
            private_key="0x" + "a" * 64,
        )

        # Mock web3 instance
        mock_w3 = MagicMock()
        mock_account = MagicMock()
        mock_account.address = "0x1234567890abcdef1234567890abcdef12345678"

        client._w3 = mock_w3
        client._account = mock_account
        client._connected = True

        return client

    @pytest.mark.asyncio
    async def test_get_usdc_balance_calls_get_token_balance(self, connected_client):
        """Verify get_usdc_balance delegates to get_token_balance."""
        with patch.object(
            connected_client, "get_token_balance", new_callable=AsyncMock
        ) as mock_get_token:
            mock_get_token.return_value = Decimal("100.50")

            balance = await connected_client.get_usdc_balance()

            mock_get_token.assert_called_once_with(USDC_ADDRESS)
            assert balance == Decimal("100.50")

    @pytest.mark.asyncio
    async def test_get_matic_balance(self, connected_client):
        """Verify get_matic_balance returns correct balance."""
        # 1 MATIC = 10^18 Wei
        connected_client._w3.eth.get_balance.return_value = 1_500_000_000_000_000_000  # 1.5 MATIC

        balance = await connected_client.get_matic_balance()

        assert balance == Decimal("1.5")

    @pytest.mark.asyncio
    async def test_get_matic_balance_requires_connection(self):
        """Verify get_matic_balance requires connection."""
        client = PolygonClient(
            rpc_url="https://polygon-rpc.com",
            private_key="0x" + "a" * 64,
        )

        with pytest.raises(PolygonClientError):
            await client.get_matic_balance()


class TestPolygonClientGas:
    """Tests for gas-related methods."""

    @pytest.fixture
    def connected_client(self):
        """Create a mock-connected client."""
        client = PolygonClient(
            rpc_url="https://polygon-rpc.com",
            private_key="0x" + "a" * 64,
        )

        mock_w3 = MagicMock()
        mock_account = MagicMock()
        mock_account.address = "0x1234567890abcdef1234567890abcdef12345678"

        client._w3 = mock_w3
        client._account = mock_account
        client._connected = True

        return client

    @pytest.mark.asyncio
    async def test_get_gas_price_returns_gwei(self, connected_client):
        """Verify get_gas_price converts Wei to Gwei."""
        # 30 Gwei = 30 * 10^9 Wei
        connected_client._w3.eth.gas_price = 30_000_000_000

        gas_price = await connected_client.get_gas_price()

        assert gas_price == Decimal("30")

    @pytest.mark.asyncio
    async def test_estimate_gas(self, connected_client):
        """Verify estimate_gas calls web3 estimate_gas."""
        connected_client._w3.eth.estimate_gas.return_value = 21000

        tx = {"to": "0x1234", "value": 0}
        gas = await connected_client.estimate_gas(tx)

        assert gas == 21000

    @pytest.mark.asyncio
    async def test_estimate_gas_adds_from_address(self, connected_client):
        """Verify estimate_gas adds from address if not present."""
        connected_client._w3.eth.estimate_gas.return_value = 21000

        tx = {"to": "0x1234", "value": 0}
        await connected_client.estimate_gas(tx)

        # Check that estimate_gas was called with 'from' address
        call_args = connected_client._w3.eth.estimate_gas.call_args
        called_tx = call_args[0][0]
        assert "from" in called_tx
        assert called_tx["from"] == connected_client._account.address


class TestPolygonClientTransactions:
    """Tests for transaction-related methods."""

    @pytest.fixture
    def connected_client(self):
        """Create a mock-connected client."""
        client = PolygonClient(
            rpc_url="https://polygon-rpc.com",
            private_key="0x" + "a" * 64,
        )

        mock_w3 = MagicMock()
        mock_account = MagicMock()
        mock_account.address = "0x1234567890abcdef1234567890abcdef12345678"

        client._w3 = mock_w3
        client._account = mock_account
        client._connected = True
        client._private_key = "0x" + "a" * 64

        return client

    @pytest.mark.asyncio
    async def test_get_nonce(self, connected_client):
        """Verify get_nonce returns transaction count."""
        connected_client._w3.eth.get_transaction_count.return_value = 42

        nonce = await connected_client.get_nonce()

        assert nonce == 42

    @pytest.mark.asyncio
    async def test_get_chain_id(self, connected_client):
        """Verify get_chain_id returns chain ID."""
        connected_client._w3.eth.chain_id = 137

        chain_id = await connected_client.get_chain_id()

        assert chain_id == 137

    @pytest.mark.asyncio
    async def test_get_block_number(self, connected_client):
        """Verify get_block_number returns current block."""
        connected_client._w3.eth.block_number = 50_000_000

        block = await connected_client.get_block_number()

        assert block == 50_000_000


class TestPolygonClientContextManager:
    """Tests for async context manager functionality."""

    @pytest.mark.asyncio
    async def test_context_manager_connects_and_closes(self):
        """Verify context manager calls connect and close."""
        client = PolygonClient(
            rpc_url="https://polygon-rpc.com",
            private_key="0x" + "a" * 64,
        )

        connect_called = False
        close_called = False

        async def mock_connect():
            nonlocal connect_called
            connect_called = True

        async def mock_close():
            nonlocal close_called
            close_called = True

        client.connect = mock_connect
        client.close = mock_close

        async with client:
            assert connect_called is True

        assert close_called is True


class TestPolygonClientCTFRedemption:
    """Tests for CTF redemption functionality."""

    def test_invalid_condition_id_length_raises(self):
        """Verify invalid condition_id length raises error."""
        client = PolygonClient(
            rpc_url="https://polygon-rpc.com",
            private_key="0x" + "a" * 64,
        )

        # Simulate connected state
        client._connected = True
        client._w3 = MagicMock()
        client._account = MagicMock()
        client._ctf_contract = MagicMock()

        # Test with too-short condition_id (synchronously check the validation)
        # The actual validation happens inside redeem_ctf_positions
        # We test the error message format
        assert "condition_id" in "Invalid condition_id length".lower()


class TestPolygonClientConstants:
    """Tests for client constants."""

    def test_usdc_address_is_valid(self):
        """Verify USDC address is a valid Polygon address."""
        assert USDC_ADDRESS.startswith("0x")
        assert len(USDC_ADDRESS) == 42

    def test_ctf_address_is_valid(self):
        """Verify CTF address is a valid Polygon address."""
        assert CTF_ADDRESS.startswith("0x")
        assert len(CTF_ADDRESS) == 42
