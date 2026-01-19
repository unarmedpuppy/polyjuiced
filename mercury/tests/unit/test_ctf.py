"""
Unit tests for CTF (Conditional Tokens Framework) client.

Tests the CTF contract interaction functionality using mocks.
"""

from decimal import Decimal
from unittest.mock import MagicMock, patch, AsyncMock
import pytest

from mercury.integrations.chain.ctf import (
    CTFClient,
    CTFError,
    TransientCTFError,
    PermanentCTFError,
    RedemptionResult,
    RedemptionStatus,
    PositionBalance,
    ConditionInfo,
    CTF_ADDRESS,
    USDC_ADDRESS,
)


# Test condition ID (64 hex chars = 32 bytes)
VALID_CONDITION_ID = "0x" + "a" * 64
VALID_CONDITION_ID_NO_PREFIX = "a" * 64


class TestCTFClientBasics:
    """Basic tests for CTFClient instantiation and properties."""

    def test_client_importable(self):
        """Verify CTFClient can be imported."""
        from mercury.integrations.chain.ctf import (
            CTFClient,
            CTFError,
            TransientCTFError,
            PermanentCTFError,
            RedemptionResult,
            RedemptionStatus,
        )

        assert CTFClient is not None
        assert CTFError is not None
        assert TransientCTFError is not None
        assert PermanentCTFError is not None
        assert RedemptionResult is not None
        assert RedemptionStatus is not None

    def test_client_instantiates(self):
        """Verify CTFClient can be instantiated."""
        client = CTFClient(
            rpc_url="https://polygon-rpc.com",
            private_key="0x" + "a" * 64,
        )

        assert client is not None
        assert client.is_connected is False
        assert client.address is None

    def test_client_uses_default_addresses(self):
        """Verify client uses correct default contract addresses."""
        client = CTFClient(
            rpc_url="https://polygon-rpc.com",
            private_key="0x" + "a" * 64,
        )

        assert client._ctf_address == CTF_ADDRESS
        assert client._usdc_address == USDC_ADDRESS

    def test_client_accepts_custom_addresses(self):
        """Verify client accepts custom contract addresses."""
        custom_ctf = "0x" + "b" * 40
        custom_usdc = "0x" + "c" * 40

        client = CTFClient(
            rpc_url="https://polygon-rpc.com",
            private_key="0x" + "a" * 64,
            ctf_address=custom_ctf,
            usdc_address=custom_usdc,
        )

        assert client._ctf_address == custom_ctf
        assert client._usdc_address == custom_usdc

    def test_client_gas_settings(self):
        """Verify client accepts gas configuration."""
        client = CTFClient(
            rpc_url="https://polygon-rpc.com",
            private_key="0x" + "a" * 64,
            gas_price_multiplier=1.5,
            default_gas_limit=500000,
        )

        assert client._gas_price_multiplier == 1.5
        assert client._default_gas_limit == 500000


class TestCTFClientDataClasses:
    """Tests for data classes."""

    def test_redemption_result_success(self):
        """Verify RedemptionResult with success status."""
        result = RedemptionResult(
            status=RedemptionStatus.SUCCESS,
            condition_id=VALID_CONDITION_ID,
            tx_hash="0x123abc",
            block_number=12345678,
            gas_used=150000,
        )

        assert result.success is True
        assert result.status == RedemptionStatus.SUCCESS
        assert result.tx_hash == "0x123abc"
        assert result.block_number == 12345678
        assert result.gas_used == 150000
        assert result.error is None

    def test_redemption_result_failed(self):
        """Verify RedemptionResult with failed status."""
        result = RedemptionResult(
            status=RedemptionStatus.FAILED,
            condition_id=VALID_CONDITION_ID,
            error="Transaction reverted",
        )

        assert result.success is False
        assert result.status == RedemptionStatus.FAILED
        assert result.error == "Transaction reverted"

    def test_redemption_status_enum_values(self):
        """Verify RedemptionStatus enum values."""
        assert RedemptionStatus.SUCCESS == "success"
        assert RedemptionStatus.FAILED == "failed"
        assert RedemptionStatus.NOT_RESOLVED == "not_resolved"
        assert RedemptionStatus.NO_BALANCE == "no_balance"
        assert RedemptionStatus.ALREADY_REDEEMED == "already_redeemed"

    def test_position_balance_usdc_conversion(self):
        """Verify PositionBalance converts wei to USDC correctly."""
        # 10 USDC = 10 * 10^6 wei (USDC has 6 decimals)
        balance = PositionBalance(
            condition_id=VALID_CONDITION_ID,
            outcome_index=0,
            balance_wei=10_000_000,
        )

        assert balance.balance_wei == 10_000_000
        assert balance.balance_usdc == Decimal("10")

    def test_position_balance_fractional(self):
        """Verify PositionBalance handles fractional USDC."""
        # 1.5 USDC = 1500000 wei
        balance = PositionBalance(
            condition_id=VALID_CONDITION_ID,
            outcome_index=1,
            balance_wei=1_500_000,
        )

        assert balance.balance_usdc == Decimal("1.5")

    def test_condition_info_resolved(self):
        """Verify ConditionInfo detects resolved condition."""
        info = ConditionInfo(
            condition_id=VALID_CONDITION_ID,
            outcome_count=2,
            payout_denominator=1,  # Non-zero means resolved
            payouts=[1, 0],  # Outcome 0 wins
        )

        assert info.is_resolved is True
        assert info.winning_outcome == 0

    def test_condition_info_unresolved(self):
        """Verify ConditionInfo detects unresolved condition."""
        info = ConditionInfo(
            condition_id=VALID_CONDITION_ID,
            outcome_count=2,
            payout_denominator=0,  # Zero means not resolved
            payouts=[0, 0],
        )

        assert info.is_resolved is False
        assert info.winning_outcome is None

    def test_condition_info_outcome_1_wins(self):
        """Verify ConditionInfo correctly identifies outcome 1 winning."""
        info = ConditionInfo(
            condition_id=VALID_CONDITION_ID,
            outcome_count=2,
            payout_denominator=1,
            payouts=[0, 1],  # Outcome 1 (NO) wins
        )

        assert info.is_resolved is True
        assert info.winning_outcome == 1


class TestCTFClientConnection:
    """Tests for connection-related functionality."""

    def test_ensure_connected_raises_when_not_connected(self):
        """Verify _ensure_connected raises when not connected."""
        client = CTFClient(
            rpc_url="https://polygon-rpc.com",
            private_key="0x" + "a" * 64,
        )

        with pytest.raises(CTFError) as excinfo:
            client._ensure_connected()

        assert "not connected" in str(excinfo.value).lower()

    @pytest.mark.asyncio
    async def test_connect_sets_connected_flag(self):
        """Verify connect() sets the connected flag."""
        client = CTFClient(
            rpc_url="https://polygon-rpc.com",
            private_key="0x" + "a" * 64,
        )

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
    async def test_connect_idempotent(self):
        """Verify connect() is idempotent (can be called multiple times)."""
        client = CTFClient(
            rpc_url="https://polygon-rpc.com",
            private_key="0x" + "a" * 64,
        )

        # Simulate already connected
        client._connected = True
        client._w3 = MagicMock()

        # Should not raise or change state
        await client.connect()

        assert client.is_connected is True

    @pytest.mark.asyncio
    async def test_close_resets_state(self):
        """Verify close() resets client state."""
        client = CTFClient(
            rpc_url="https://polygon-rpc.com",
            private_key="0x" + "a" * 64,
        )

        # Simulate connected state
        client._connected = True
        client._w3 = MagicMock()
        client._account = MagicMock()
        client._ctf_contract = MagicMock()

        await client.close()

        assert client.is_connected is False
        assert client._w3 is None
        assert client._account is None
        assert client._ctf_contract is None

    @pytest.mark.asyncio
    async def test_context_manager(self):
        """Verify async context manager calls connect and close."""
        client = CTFClient(
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


class TestCTFClientValidation:
    """Tests for input validation."""

    def test_validate_condition_id_with_prefix(self):
        """Verify condition_id validation with 0x prefix."""
        client = CTFClient(
            rpc_url="https://polygon-rpc.com",
            private_key="0x" + "a" * 64,
        )

        result = client._validate_condition_id(VALID_CONDITION_ID)

        assert len(result) == 32
        assert isinstance(result, bytes)

    def test_validate_condition_id_without_prefix(self):
        """Verify condition_id validation without 0x prefix."""
        client = CTFClient(
            rpc_url="https://polygon-rpc.com",
            private_key="0x" + "a" * 64,
        )

        result = client._validate_condition_id(VALID_CONDITION_ID_NO_PREFIX)

        assert len(result) == 32
        assert isinstance(result, bytes)

    def test_validate_condition_id_too_short(self):
        """Verify validation rejects too-short condition_id."""
        client = CTFClient(
            rpc_url="https://polygon-rpc.com",
            private_key="0x" + "a" * 64,
        )

        with pytest.raises(PermanentCTFError) as excinfo:
            client._validate_condition_id("0x" + "a" * 32)  # Only 16 bytes

        assert "length" in str(excinfo.value).lower()

    def test_validate_condition_id_invalid_hex(self):
        """Verify validation rejects invalid hex."""
        client = CTFClient(
            rpc_url="https://polygon-rpc.com",
            private_key="0x" + "a" * 64,
        )

        with pytest.raises(PermanentCTFError) as excinfo:
            client._validate_condition_id("0x" + "g" * 64)  # Invalid hex

        assert "invalid" in str(excinfo.value).lower()


class TestCTFClientRedemption:
    """Tests for redemption functionality."""

    @pytest.fixture
    def connected_client(self):
        """Create a mock-connected CTF client."""
        client = CTFClient(
            rpc_url="https://polygon-rpc.com",
            private_key="0x" + "a" * 64,
        )

        mock_w3 = MagicMock()
        mock_account = MagicMock()
        mock_account.address = "0x1234567890abcdef1234567890abcdef12345678"
        mock_ctf = MagicMock()

        client._w3 = mock_w3
        client._account = mock_account
        client._ctf_contract = mock_ctf
        client._connected = True

        return client

    @pytest.mark.asyncio
    async def test_redeem_positions_requires_connection(self):
        """Verify redeem_positions requires connection."""
        client = CTFClient(
            rpc_url="https://polygon-rpc.com",
            private_key="0x" + "a" * 64,
        )

        with pytest.raises(CTFError) as excinfo:
            await client.redeem_positions(VALID_CONDITION_ID)

        assert "not connected" in str(excinfo.value).lower()

    @pytest.mark.asyncio
    async def test_redeem_positions_validates_condition_id(self, connected_client):
        """Verify redeem_positions validates condition_id."""
        with pytest.raises(PermanentCTFError):
            await connected_client.redeem_positions("invalid")

    @pytest.mark.asyncio
    async def test_redeem_positions_default_index_sets(self, connected_client):
        """Verify redeem_positions uses default index_sets [1, 2]."""
        # Setup mocks for successful transaction
        connected_client._w3.eth.get_transaction_count.return_value = 1
        connected_client._w3.eth.gas_price = 30_000_000_000
        connected_client._w3.eth.estimate_gas.return_value = 150000

        mock_tx_data = MagicMock()
        mock_tx_data.build_transaction.return_value = {
            "from": connected_client._account.address,
            "nonce": 1,
            "gas": 150000,
            "gasPrice": 30_000_000_000,
        }
        connected_client._ctf_contract.functions.redeemPositions.return_value = (
            mock_tx_data
        )

        mock_signed = MagicMock()
        mock_signed.rawTransaction = b"signed_tx_data"
        connected_client._w3.eth.account.sign_transaction.return_value = mock_signed

        mock_tx_hash = MagicMock()
        mock_tx_hash.hex.return_value = "0x" + "f" * 64
        connected_client._w3.eth.send_raw_transaction.return_value = mock_tx_hash

        connected_client._w3.eth.wait_for_transaction_receipt.return_value = {
            "status": 1,
            "blockNumber": 50000000,
            "gasUsed": 140000,
        }

        with patch("web3.Web3") as mock_web3:
            mock_web3.to_checksum_address.return_value = USDC_ADDRESS

            result = await connected_client.redeem_positions(VALID_CONDITION_ID)

        # Verify redeemPositions was called with default index_sets
        call_args = (
            connected_client._ctf_contract.functions.redeemPositions.call_args
        )
        assert call_args[0][3] == [1, 2]  # index_sets parameter

        assert result.success is True
        assert result.status == RedemptionStatus.SUCCESS

    @pytest.mark.asyncio
    async def test_redeem_positions_custom_index_sets(self, connected_client):
        """Verify redeem_positions accepts custom index_sets."""
        # Setup mocks
        connected_client._w3.eth.get_transaction_count.return_value = 1
        connected_client._w3.eth.gas_price = 30_000_000_000
        connected_client._w3.eth.estimate_gas.return_value = 150000

        mock_tx_data = MagicMock()
        mock_tx_data.build_transaction.return_value = {
            "from": connected_client._account.address,
            "nonce": 1,
            "gas": 150000,
            "gasPrice": 30_000_000_000,
        }
        connected_client._ctf_contract.functions.redeemPositions.return_value = (
            mock_tx_data
        )

        mock_signed = MagicMock()
        mock_signed.rawTransaction = b"signed_tx_data"
        connected_client._w3.eth.account.sign_transaction.return_value = mock_signed

        mock_tx_hash = MagicMock()
        mock_tx_hash.hex.return_value = "0x" + "f" * 64
        connected_client._w3.eth.send_raw_transaction.return_value = mock_tx_hash

        connected_client._w3.eth.wait_for_transaction_receipt.return_value = {
            "status": 1,
            "blockNumber": 50000000,
            "gasUsed": 140000,
        }

        with patch("web3.Web3") as mock_web3:
            mock_web3.to_checksum_address.return_value = USDC_ADDRESS

            await connected_client.redeem_positions(
                VALID_CONDITION_ID, index_sets=[1]
            )

        call_args = (
            connected_client._ctf_contract.functions.redeemPositions.call_args
        )
        assert call_args[0][3] == [1]

    @pytest.mark.asyncio
    async def test_redeem_positions_reverted_transaction(self, connected_client):
        """Verify handling of reverted transaction."""
        connected_client._w3.eth.get_transaction_count.return_value = 1
        connected_client._w3.eth.gas_price = 30_000_000_000
        connected_client._w3.eth.estimate_gas.return_value = 150000

        mock_tx_data = MagicMock()
        mock_tx_data.build_transaction.return_value = {
            "from": connected_client._account.address,
            "nonce": 1,
            "gas": 150000,
            "gasPrice": 30_000_000_000,
        }
        connected_client._ctf_contract.functions.redeemPositions.return_value = (
            mock_tx_data
        )

        mock_signed = MagicMock()
        mock_signed.rawTransaction = b"signed_tx_data"
        connected_client._w3.eth.account.sign_transaction.return_value = mock_signed

        mock_tx_hash = MagicMock()
        mock_tx_hash.hex.return_value = "0x" + "f" * 64
        connected_client._w3.eth.send_raw_transaction.return_value = mock_tx_hash

        # Status 0 = reverted
        connected_client._w3.eth.wait_for_transaction_receipt.return_value = {
            "status": 0,
            "blockNumber": 50000000,
            "gasUsed": 140000,
        }

        with patch("web3.Web3") as mock_web3:
            mock_web3.to_checksum_address.return_value = USDC_ADDRESS

            result = await connected_client.redeem_positions(VALID_CONDITION_ID)

        assert result.success is False
        assert result.status == RedemptionStatus.FAILED
        assert "reverted" in result.error.lower()


class TestCTFClientQueries:
    """Tests for query methods."""

    @pytest.fixture
    def connected_client(self):
        """Create a mock-connected CTF client."""
        client = CTFClient(
            rpc_url="https://polygon-rpc.com",
            private_key="0x" + "a" * 64,
        )

        mock_w3 = MagicMock()
        mock_account = MagicMock()
        mock_account.address = "0x1234567890abcdef1234567890abcdef12345678"
        mock_ctf = MagicMock()

        client._w3 = mock_w3
        client._account = mock_account
        client._ctf_contract = mock_ctf
        client._connected = True

        return client

    @pytest.mark.asyncio
    async def test_get_condition_info(self, connected_client):
        """Verify get_condition_info returns correct data."""
        # Mock contract calls
        connected_client._ctf_contract.functions.getOutcomeSlotCount.return_value.call.return_value = 2
        connected_client._ctf_contract.functions.payoutDenominator.return_value.call.return_value = 1
        connected_client._ctf_contract.functions.payoutNumerators.return_value.call.side_effect = [
            1,
            0,
        ]

        info = await connected_client.get_condition_info(VALID_CONDITION_ID)

        assert info.outcome_count == 2
        assert info.payout_denominator == 1
        assert info.payouts == [1, 0]
        assert info.is_resolved is True
        assert info.winning_outcome == 0

    @pytest.mark.asyncio
    async def test_is_condition_resolved_true(self, connected_client):
        """Verify is_condition_resolved returns True for resolved condition."""
        connected_client._ctf_contract.functions.getOutcomeSlotCount.return_value.call.return_value = 2
        connected_client._ctf_contract.functions.payoutDenominator.return_value.call.return_value = 1
        connected_client._ctf_contract.functions.payoutNumerators.return_value.call.side_effect = [
            1,
            0,
        ]

        result = await connected_client.is_condition_resolved(VALID_CONDITION_ID)

        assert result is True

    @pytest.mark.asyncio
    async def test_is_condition_resolved_false(self, connected_client):
        """Verify is_condition_resolved returns False for unresolved condition."""
        connected_client._ctf_contract.functions.getOutcomeSlotCount.return_value.call.return_value = 2
        connected_client._ctf_contract.functions.payoutDenominator.return_value.call.return_value = 0
        connected_client._ctf_contract.functions.payoutNumerators.return_value.call.side_effect = [
            0,
            0,
        ]

        result = await connected_client.is_condition_resolved(VALID_CONDITION_ID)

        assert result is False

    @pytest.mark.asyncio
    async def test_get_gas_price(self, connected_client):
        """Verify get_gas_price returns correct Gwei value."""
        # 30 Gwei = 30 * 10^9 Wei
        connected_client._w3.eth.gas_price = 30_000_000_000

        gas_price = await connected_client.get_gas_price()

        assert gas_price == Decimal("30")


class TestCTFClientConstants:
    """Tests for module constants."""

    def test_ctf_address_is_valid(self):
        """Verify CTF address is a valid Polygon address."""
        assert CTF_ADDRESS.startswith("0x")
        assert len(CTF_ADDRESS) == 42

    def test_usdc_address_is_valid(self):
        """Verify USDC address is a valid Polygon address."""
        assert USDC_ADDRESS.startswith("0x")
        assert len(USDC_ADDRESS) == 42

    def test_ctf_address_matches_polygon_mainnet(self):
        """Verify CTF address matches known Polymarket CTF contract."""
        expected = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
        assert CTF_ADDRESS == expected

    def test_usdc_address_matches_polygon_mainnet(self):
        """Verify USDC address matches Polygon USDC.e."""
        expected = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
        assert USDC_ADDRESS == expected


class TestCTFErrorHierarchy:
    """Tests for error class hierarchy."""

    def test_transient_error_is_ctf_error(self):
        """Verify TransientCTFError inherits from CTFError."""
        assert issubclass(TransientCTFError, CTFError)

    def test_permanent_error_is_ctf_error(self):
        """Verify PermanentCTFError inherits from CTFError."""
        assert issubclass(PermanentCTFError, CTFError)

    def test_transient_error_can_be_raised(self):
        """Verify TransientCTFError can be raised and caught."""
        with pytest.raises(TransientCTFError):
            raise TransientCTFError("Network timeout")

    def test_permanent_error_can_be_raised(self):
        """Verify PermanentCTFError can be raised and caught."""
        with pytest.raises(PermanentCTFError):
            raise PermanentCTFError("Invalid input")

    def test_catch_ctf_error_catches_transient(self):
        """Verify catching CTFError catches TransientCTFError."""
        with pytest.raises(CTFError):
            raise TransientCTFError("Network issue")

    def test_catch_ctf_error_catches_permanent(self):
        """Verify catching CTFError catches PermanentCTFError."""
        with pytest.raises(CTFError):
            raise PermanentCTFError("Bad input")
