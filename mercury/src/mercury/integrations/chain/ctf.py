"""CTF (Conditional Tokens Framework) client for position redemption.

This module handles CTF contract interactions for Polymarket settlements:
- Redeem winning positions after market resolution
- Query position balances
- Gas estimation for redemptions

The CTF contract is the standard Gnosis Conditional Tokens contract deployed
on Polygon. After a market resolves, winning outcome tokens can be redeemed
for the collateral (USDC) via the redeemPositions() function.

Reference: https://github.com/Polymarket/conditional-token-examples-py
"""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from typing import Any, Optional

import structlog
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

log = structlog.get_logger()


# Polygon mainnet addresses
CTF_ADDRESS = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"  # Conditional Tokens Framework
USDC_ADDRESS = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"  # USDC.e on Polygon


# CTF Contract ABI (minimal for redemption and position queries)
CTF_ABI = [
    # redeemPositions - claim winnings after market resolution
    {
        "inputs": [
            {"name": "collateralToken", "type": "address"},
            {"name": "parentCollectionId", "type": "bytes32"},
            {"name": "conditionId", "type": "bytes32"},
            {"name": "indexSets", "type": "uint256[]"},
        ],
        "name": "redeemPositions",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    },
    # getPositionId - get position token ID from collection
    {
        "inputs": [
            {"name": "collateralToken", "type": "address"},
            {"name": "collectionId", "type": "bytes32"},
        ],
        "name": "getPositionId",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    # balanceOf - ERC1155 balance query
    {
        "inputs": [
            {"name": "owner", "type": "address"},
            {"name": "id", "type": "uint256"},
        ],
        "name": "balanceOf",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    # getOutcomeSlotCount - get number of outcomes for a condition
    {
        "inputs": [{"name": "conditionId", "type": "bytes32"}],
        "name": "getOutcomeSlotCount",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    # payoutNumerators - get payout for each outcome (shows resolution)
    {
        "inputs": [
            {"name": "conditionId", "type": "bytes32"},
            {"name": "outcomeIndex", "type": "uint256"},
        ],
        "name": "payoutNumerators",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    # payoutDenominator - get payout denominator for condition
    {
        "inputs": [{"name": "conditionId", "type": "bytes32"}],
        "name": "payoutDenominator",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
]


class CTFError(Exception):
    """Error from CTF client operations."""

    pass


class TransientCTFError(CTFError):
    """Transient error that can be retried (network issues, etc)."""

    pass


class PermanentCTFError(CTFError):
    """Permanent error that should not be retried (invalid input, etc)."""

    pass


class RedemptionStatus(str, Enum):
    """Status of a redemption attempt."""

    SUCCESS = "success"
    FAILED = "failed"
    NOT_RESOLVED = "not_resolved"
    NO_BALANCE = "no_balance"
    ALREADY_REDEEMED = "already_redeemed"


@dataclass
class RedemptionResult:
    """Result of a CTF position redemption."""

    status: RedemptionStatus
    condition_id: str
    tx_hash: Optional[str] = None
    block_number: Optional[int] = None
    gas_used: Optional[int] = None
    error: Optional[str] = None
    proceeds_wei: Optional[int] = None

    @property
    def success(self) -> bool:
        """Whether the redemption was successful."""
        return self.status == RedemptionStatus.SUCCESS


@dataclass
class PositionBalance:
    """Balance information for a CTF position."""

    condition_id: str
    outcome_index: int
    balance_wei: int
    balance_usdc: Decimal = field(init=False)

    def __post_init__(self):
        # USDC has 6 decimals
        self.balance_usdc = Decimal(str(self.balance_wei)) / Decimal("1000000")


@dataclass
class ConditionInfo:
    """Information about a CTF condition (market)."""

    condition_id: str
    outcome_count: int
    payout_denominator: int
    payouts: list[int]  # payout numerator for each outcome
    is_resolved: bool = field(init=False)
    winning_outcome: Optional[int] = field(init=False)

    def __post_init__(self):
        # A condition is resolved if payout_denominator > 0
        self.is_resolved = self.payout_denominator > 0
        # Find winning outcome (the one with non-zero payout)
        self.winning_outcome = None
        if self.is_resolved:
            for i, payout in enumerate(self.payouts):
                if payout > 0:
                    self.winning_outcome = i
                    break


class CTFClient:
    """Client for interacting with the Conditional Tokens Framework contract.

    This client provides methods to:
    - Redeem winning positions after market resolution
    - Query position balances
    - Check if conditions are resolved
    - Estimate gas for redemption transactions

    All methods are async-compatible, wrapping synchronous web3 calls
    in a thread pool executor.
    """

    def __init__(
        self,
        rpc_url: str,
        private_key: str,
        ctf_address: str = CTF_ADDRESS,
        usdc_address: str = USDC_ADDRESS,
        executor: Optional[ThreadPoolExecutor] = None,
        gas_price_multiplier: float = 1.2,
        default_gas_limit: int = 300000,
    ):
        """Initialize the CTF client.

        Args:
            rpc_url: Polygon RPC endpoint URL.
            private_key: Private key for signing transactions (hex string).
            ctf_address: CTF contract address (defaults to Polymarket CTF).
            usdc_address: USDC contract address (defaults to Polygon USDC.e).
            executor: Optional thread pool for async execution.
            gas_price_multiplier: Multiplier for gas price (default 1.2 = 20% buffer).
            default_gas_limit: Default gas limit for redemption transactions.
        """
        self._rpc_url = rpc_url
        self._private_key = private_key
        self._ctf_address = ctf_address
        self._usdc_address = usdc_address
        self._executor = executor or ThreadPoolExecutor(max_workers=2)
        self._gas_price_multiplier = gas_price_multiplier
        self._default_gas_limit = default_gas_limit
        self._log = log.bind(component="ctf_client")

        # Web3 state (initialized on connect)
        self._w3 = None
        self._account = None
        self._ctf_contract = None
        self._connected = False

    @property
    def address(self) -> Optional[str]:
        """Wallet address."""
        return self._account.address if self._account else None

    @property
    def is_connected(self) -> bool:
        """Whether connected to the RPC."""
        return self._connected and self._w3 is not None

    async def connect(self) -> None:
        """Connect to the Polygon RPC and initialize contracts."""
        if self._connected:
            return

        try:
            from eth_account import Account
            from web3 import Web3

            # Initialize Web3 connection
            self._w3 = Web3(Web3.HTTPProvider(self._rpc_url))

            # Verify connection
            if not await self._run_sync(lambda: self._w3.is_connected()):
                raise CTFError(f"Failed to connect to RPC: {self._rpc_url}")

            # Initialize account from private key
            self._account = Account.from_key(self._private_key)

            # Initialize CTF contract
            ctf_checksum = Web3.to_checksum_address(self._ctf_address)
            self._ctf_contract = self._w3.eth.contract(
                address=ctf_checksum,
                abi=CTF_ABI,
            )

            self._connected = True
            self._log.info(
                "ctf_client_connected",
                rpc=self._rpc_url,
                address=self._account.address,
                ctf_address=ctf_checksum,
            )

        except ImportError:
            raise CTFError("web3 not installed. Install with: pip install web3")

    async def close(self) -> None:
        """Close the client and release resources."""
        self._w3 = None
        self._account = None
        self._ctf_contract = None
        self._connected = False
        self._log.debug("ctf_client_closed")

    async def __aenter__(self) -> "CTFClient":
        """Async context manager entry."""
        await self.connect()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        """Async context manager exit."""
        await self.close()

    async def _run_sync(self, func, *args, **kwargs):
        """Run a synchronous function in the thread pool."""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            self._executor, lambda: func(*args, **kwargs)
        )

    def _ensure_connected(self) -> None:
        """Ensure client is connected."""
        if not self._connected:
            raise CTFError("Client not connected. Call connect() first.")

    def _validate_condition_id(self, condition_id: str) -> bytes:
        """Validate and convert condition ID to bytes32.

        Args:
            condition_id: Condition ID as hex string (with or without 0x prefix).

        Returns:
            Condition ID as bytes32.

        Raises:
            PermanentCTFError: If condition_id is invalid.
        """
        # Remove 0x prefix if present
        if condition_id.startswith("0x"):
            condition_id = condition_id[2:]

        try:
            condition_bytes = bytes.fromhex(condition_id)
        except ValueError:
            raise PermanentCTFError(f"Invalid condition_id hex: {condition_id[:20]}...")

        if len(condition_bytes) != 32:
            raise PermanentCTFError(
                f"Invalid condition_id length: {len(condition_bytes)}, expected 32"
            )

        return condition_bytes

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        retry=retry_if_exception_type(TransientCTFError),
    )
    async def redeem_positions(
        self,
        condition_id: str,
        index_sets: Optional[list[int]] = None,
        gas_limit: Optional[int] = None,
        gas_price_gwei: Optional[float] = None,
    ) -> RedemptionResult:
        """Redeem CTF positions after market resolution.

        This calls the redeemPositions() function on the CTF contract to
        convert winning outcome tokens back to USDC collateral.

        For binary markets (YES/NO), use index_sets=[1, 2] to redeem both
        outcomes - the contract will only transfer USDC for winning positions.

        Args:
            condition_id: Market condition ID (hex string, 32 bytes).
            index_sets: Outcome index sets to redeem. Default [1, 2] for binary.
                       - index_set=1 corresponds to outcome 0 (usually YES)
                       - index_set=2 corresponds to outcome 1 (usually NO)
            gas_limit: Optional gas limit override.
            gas_price_gwei: Optional gas price in Gwei.

        Returns:
            RedemptionResult with transaction details.

        Raises:
            TransientCTFError: For retriable errors (network issues).
            PermanentCTFError: For non-retriable errors (invalid input).
        """
        self._ensure_connected()

        from web3 import Web3

        # Validate condition_id
        condition_bytes = self._validate_condition_id(condition_id)

        # Default to redeeming both outcomes for binary markets
        if index_sets is None:
            index_sets = [1, 2]

        self._log.info(
            "redeeming_positions",
            condition_id=condition_id[:16] + "...",
            index_sets=index_sets,
            wallet=self._account.address,
        )

        try:
            # Build transaction parameters
            parent_collection_id = bytes(32)  # All zeros for root collection
            usdc_checksum = Web3.to_checksum_address(self._usdc_address)

            # Get current nonce and gas price
            nonce = await self._run_sync(
                lambda: self._w3.eth.get_transaction_count(self._account.address)
            )

            if gas_price_gwei is not None:
                gas_price_wei = int(gas_price_gwei * 1e9)
            else:
                current_gas_price = await self._run_sync(
                    lambda: self._w3.eth.gas_price
                )
                gas_price_wei = int(current_gas_price * self._gas_price_multiplier)

            # Build the transaction
            tx_data = self._ctf_contract.functions.redeemPositions(
                usdc_checksum,
                parent_collection_id,
                condition_bytes,
                index_sets,
            )

            tx = await self._run_sync(
                lambda: tx_data.build_transaction(
                    {
                        "from": self._account.address,
                        "nonce": nonce,
                        "gasPrice": gas_price_wei,
                        "gas": gas_limit or self._default_gas_limit,
                    }
                )
            )

            # Estimate gas if not provided
            if gas_limit is None:
                try:
                    gas_estimate = await self._run_sync(
                        lambda: self._w3.eth.estimate_gas(tx)
                    )
                    # Add buffer for safety
                    tx["gas"] = int(gas_estimate * 1.2)
                    self._log.debug(
                        "gas_estimated",
                        estimate=gas_estimate,
                        with_buffer=tx["gas"],
                    )
                except Exception as e:
                    self._log.warning(
                        "gas_estimate_failed",
                        error=str(e),
                        using_default=self._default_gas_limit,
                    )

            # Sign and send transaction
            signed_tx = await self._run_sync(
                lambda: self._w3.eth.account.sign_transaction(tx, self._private_key)
            )

            tx_hash = await self._run_sync(
                lambda: self._w3.eth.send_raw_transaction(signed_tx.rawTransaction)
            )
            tx_hash_hex = tx_hash.hex()

            self._log.info(
                "redemption_tx_submitted",
                tx_hash=tx_hash_hex,
                gas_limit=tx["gas"],
                gas_price_gwei=gas_price_wei / 1e9,
            )

            # Wait for transaction receipt
            receipt = await self._run_sync(
                lambda: self._w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
            )

            if receipt["status"] == 1:
                self._log.info(
                    "redemption_successful",
                    tx_hash=tx_hash_hex,
                    block=receipt["blockNumber"],
                    gas_used=receipt["gasUsed"],
                )

                return RedemptionResult(
                    status=RedemptionStatus.SUCCESS,
                    condition_id=condition_id,
                    tx_hash=tx_hash_hex,
                    block_number=receipt["blockNumber"],
                    gas_used=receipt["gasUsed"],
                )
            else:
                self._log.error(
                    "redemption_tx_reverted",
                    tx_hash=tx_hash_hex,
                    block=receipt["blockNumber"],
                )

                return RedemptionResult(
                    status=RedemptionStatus.FAILED,
                    condition_id=condition_id,
                    tx_hash=tx_hash_hex,
                    block_number=receipt["blockNumber"],
                    gas_used=receipt["gasUsed"],
                    error="Transaction reverted",
                )

        except PermanentCTFError:
            raise
        except Exception as e:
            error_str = str(e)
            self._log.error(
                "redemption_error",
                condition_id=condition_id[:16] + "...",
                error=error_str,
            )

            # Classify error for retry logic
            if "nonce" in error_str.lower() or "timeout" in error_str.lower():
                raise TransientCTFError(f"Transient error: {error_str}")

            return RedemptionResult(
                status=RedemptionStatus.FAILED,
                condition_id=condition_id,
                error=error_str,
            )

    async def get_position_balance(
        self,
        condition_id: str,
        outcome_index: int,
    ) -> PositionBalance:
        """Get balance of a specific outcome position.

        Args:
            condition_id: Market condition ID.
            outcome_index: Outcome index (0 = YES, 1 = NO for binary markets).

        Returns:
            PositionBalance with balance information.
        """
        self._ensure_connected()

        from web3 import Web3

        condition_bytes = self._validate_condition_id(condition_id)

        # Calculate collection ID for this outcome
        # collection_id = keccak256(abi.encodePacked(parentCollectionId, conditionId, indexSet))
        parent_collection_id = bytes(32)
        index_set = 1 << outcome_index  # 1 for outcome 0, 2 for outcome 1, etc.

        # For Polymarket binary markets, we use the simplified position ID calculation
        # Position ID = hash(collateral, collectionId)
        # This matches how Polymarket creates position tokens

        usdc_checksum = Web3.to_checksum_address(self._usdc_address)

        # Get position ID using contract's getPositionId
        # First, calculate collection ID
        collection_id = Web3.solidity_keccak(
            ["bytes32", "bytes32", "uint256"],
            [parent_collection_id, condition_bytes, index_set],
        )

        position_id = await self._run_sync(
            lambda: self._ctf_contract.functions.getPositionId(
                usdc_checksum, collection_id
            ).call()
        )

        # Query balance
        balance = await self._run_sync(
            lambda: self._ctf_contract.functions.balanceOf(
                self._account.address, position_id
            ).call()
        )

        return PositionBalance(
            condition_id=condition_id,
            outcome_index=outcome_index,
            balance_wei=balance,
        )

    async def get_condition_info(self, condition_id: str) -> ConditionInfo:
        """Get information about a condition (market).

        Args:
            condition_id: Market condition ID.

        Returns:
            ConditionInfo with resolution status and payouts.
        """
        self._ensure_connected()

        condition_bytes = self._validate_condition_id(condition_id)

        # Get outcome count
        outcome_count = await self._run_sync(
            lambda: self._ctf_contract.functions.getOutcomeSlotCount(
                condition_bytes
            ).call()
        )

        # Get payout denominator (0 if not resolved)
        payout_denominator = await self._run_sync(
            lambda: self._ctf_contract.functions.payoutDenominator(
                condition_bytes
            ).call()
        )

        # Get payouts for each outcome
        payouts = []
        for i in range(outcome_count):
            payout = await self._run_sync(
                lambda idx=i: self._ctf_contract.functions.payoutNumerators(
                    condition_bytes, idx
                ).call()
            )
            payouts.append(payout)

        return ConditionInfo(
            condition_id=condition_id,
            outcome_count=outcome_count,
            payout_denominator=payout_denominator,
            payouts=payouts,
        )

    async def is_condition_resolved(self, condition_id: str) -> bool:
        """Check if a condition has been resolved.

        Args:
            condition_id: Market condition ID.

        Returns:
            True if resolved, False otherwise.
        """
        info = await self.get_condition_info(condition_id)
        return info.is_resolved

    async def estimate_redemption_gas(
        self,
        condition_id: str,
        index_sets: Optional[list[int]] = None,
    ) -> int:
        """Estimate gas cost for a redemption.

        Args:
            condition_id: Market condition ID.
            index_sets: Outcome index sets to redeem. Default [1, 2].

        Returns:
            Estimated gas units.
        """
        self._ensure_connected()

        from web3 import Web3

        condition_bytes = self._validate_condition_id(condition_id)

        if index_sets is None:
            index_sets = [1, 2]

        parent_collection_id = bytes(32)
        usdc_checksum = Web3.to_checksum_address(self._usdc_address)

        tx_data = self._ctf_contract.functions.redeemPositions(
            usdc_checksum,
            parent_collection_id,
            condition_bytes,
            index_sets,
        )

        tx = await self._run_sync(
            lambda: tx_data.build_transaction(
                {
                    "from": self._account.address,
                    "gas": self._default_gas_limit,
                }
            )
        )

        gas_estimate = await self._run_sync(lambda: self._w3.eth.estimate_gas(tx))

        return gas_estimate

    async def get_gas_price(self) -> Decimal:
        """Get current gas price in Gwei.

        Returns:
            Gas price in Gwei.
        """
        self._ensure_connected()

        wei = await self._run_sync(lambda: self._w3.eth.gas_price)
        return Decimal(str(wei)) / Decimal("1e9")
