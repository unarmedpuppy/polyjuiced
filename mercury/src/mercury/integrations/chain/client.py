"""Polygon chain client for on-chain interactions.

This client handles direct chain interactions including:
- CTF (Conditional Tokens Framework) redemptions
- Token balance queries
- Transaction submission
"""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

import structlog

log = structlog.get_logger()

# Polygon addresses (mainnet)
USDC_ADDRESS = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"  # USDC.e on Polygon
CTF_ADDRESS = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"  # Polymarket CTF

# CTF ABI (minimal for redemption)
CTF_ABI = [
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
]

# ERC20 ABI (minimal for balance)
ERC20_ABI = [
    {
        "inputs": [{"name": "account", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "decimals",
        "outputs": [{"name": "", "type": "uint8"}],
        "stateMutability": "view",
        "type": "function",
    },
]


class PolygonClientError(Exception):
    """Error from Polygon client."""

    pass


@dataclass
class TxReceipt:
    """Transaction receipt."""

    tx_hash: str
    block_number: int
    gas_used: int
    status: bool  # True = success


class PolygonClient:
    """Client for Polygon chain interactions.

    Uses web3.py for direct chain interactions including:
    - CTF redemptions after market resolution
    - Token balance queries
    - Transaction submission and monitoring

    All methods are async-wrapped for compatibility.
    """

    def __init__(
        self,
        rpc_url: str,
        private_key: str,
        executor: Optional[ThreadPoolExecutor] = None,
    ):
        """Initialize the Polygon client.

        Args:
            rpc_url: Polygon RPC URL.
            private_key: Wallet private key for signing transactions.
            executor: Optional thread pool for async execution.
        """
        self._rpc_url = rpc_url
        self._private_key = private_key
        self._executor = executor or ThreadPoolExecutor(max_workers=2)
        self._log = log.bind(component="polygon_client")

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
        """Whether connected to RPC."""
        return self._connected and self._w3 is not None

    async def connect(self) -> None:
        """Connect to Polygon RPC."""
        if self._connected:
            return

        try:
            from web3 import Web3
            from eth_account import Account

            # Initialize Web3
            self._w3 = Web3(Web3.HTTPProvider(self._rpc_url))

            # Verify connection
            if not await self._run_sync(lambda: self._w3.is_connected()):
                raise PolygonClientError(f"Failed to connect to {self._rpc_url}")

            # Set up account
            self._account = Account.from_key(self._private_key)

            # Set up CTF contract
            self._ctf_contract = self._w3.eth.contract(
                address=Web3.to_checksum_address(CTF_ADDRESS),
                abi=CTF_ABI,
            )

            self._connected = True
            self._log.info(
                "polygon_connected",
                rpc=self._rpc_url,
                address=self._account.address,
            )

        except ImportError:
            raise PolygonClientError(
                "web3 not installed. Install with: pip install web3"
            )

    async def close(self) -> None:
        """Close the client."""
        self._w3 = None
        self._account = None
        self._ctf_contract = None
        self._connected = False

    async def __aenter__(self) -> "PolygonClient":
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
            self._executor,
            lambda: func(*args, **kwargs)
        )

    def _ensure_connected(self):
        """Ensure client is connected."""
        if not self._connected:
            raise PolygonClientError("Client not connected. Call connect() first.")

    async def get_usdc_balance(self) -> Decimal:
        """Get USDC balance for the wallet.

        Returns:
            USDC balance as Decimal.
        """
        self._ensure_connected()
        return await self.get_token_balance(USDC_ADDRESS)

    async def get_token_balance(self, token_address: str) -> Decimal:
        """Get ERC20 token balance.

        Args:
            token_address: Token contract address.

        Returns:
            Token balance as Decimal (adjusted for decimals).
        """
        self._ensure_connected()

        from web3 import Web3

        contract = self._w3.eth.contract(
            address=Web3.to_checksum_address(token_address),
            abi=ERC20_ABI,
        )

        # Get balance and decimals
        raw_balance = await self._run_sync(
            contract.functions.balanceOf(self._account.address).call
        )
        decimals = await self._run_sync(
            contract.functions.decimals().call
        )

        # Convert to Decimal
        balance = Decimal(str(raw_balance)) / (10 ** decimals)
        return balance

    async def redeem_ctf_positions(
        self,
        condition_id: str,
        index_sets: Optional[list[int]] = None,
    ) -> TxReceipt:
        """Redeem CTF positions after market resolution.

        This claims winnings from resolved markets by calling the
        CTF redeemPositions function.

        Args:
            condition_id: The market's condition ID (hex string).
            index_sets: Which outcomes to redeem. Default [1, 2] for both.

        Returns:
            TxReceipt with transaction details.
        """
        self._ensure_connected()

        from web3 import Web3

        if index_sets is None:
            index_sets = [1, 2]  # Redeem both YES and NO

        # Convert condition_id to bytes32
        if condition_id.startswith("0x"):
            condition_id = condition_id[2:]

        condition_bytes = bytes.fromhex(condition_id)
        if len(condition_bytes) != 32:
            raise PolygonClientError(
                f"Invalid condition_id length: {len(condition_bytes)}, expected 32"
            )

        self._log.info(
            "redeeming_positions",
            condition_id=condition_id[:16] + "...",
            index_sets=index_sets,
        )

        # Build transaction
        parent_collection_id = bytes(32)  # All zeros
        usdc_checksum = Web3.to_checksum_address(USDC_ADDRESS)

        tx_data = self._ctf_contract.functions.redeemPositions(
            usdc_checksum,
            parent_collection_id,
            condition_bytes,
            index_sets,
        )

        # Get gas estimate and nonce
        nonce = await self._run_sync(
            lambda: self._w3.eth.get_transaction_count(self._account.address)
        )

        gas_price = await self._run_sync(lambda: self._w3.eth.gas_price)

        # Build the transaction
        tx = await self._run_sync(
            lambda: tx_data.build_transaction({
                "from": self._account.address,
                "nonce": nonce,
                "gasPrice": gas_price,
                "gas": 200000,  # Estimate, will be refined
            })
        )

        # Estimate gas
        try:
            gas_estimate = await self._run_sync(
                lambda: self._w3.eth.estimate_gas(tx)
            )
            tx["gas"] = int(gas_estimate * 1.2)  # 20% buffer
        except Exception as e:
            self._log.warning("gas_estimate_failed", error=str(e))

        # Sign and send
        signed = await self._run_sync(
            lambda: self._w3.eth.account.sign_transaction(tx, self._private_key)
        )

        tx_hash = await self._run_sync(
            lambda: self._w3.eth.send_raw_transaction(signed.rawTransaction)
        )

        self._log.info("tx_submitted", tx_hash=tx_hash.hex())

        # Wait for receipt
        receipt = await self._run_sync(
            lambda: self._w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
        )

        result = TxReceipt(
            tx_hash=tx_hash.hex(),
            block_number=receipt["blockNumber"],
            gas_used=receipt["gasUsed"],
            status=receipt["status"] == 1,
        )

        self._log.info(
            "redemption_complete",
            tx_hash=result.tx_hash,
            status="success" if result.status else "failed",
            gas_used=result.gas_used,
        )

        return result

    async def get_block_number(self) -> int:
        """Get current block number.

        Returns:
            Current block number.
        """
        self._ensure_connected()
        return await self._run_sync(lambda: self._w3.eth.block_number)

    async def get_gas_price(self) -> Decimal:
        """Get current gas price in Gwei.

        Returns:
            Gas price in Gwei.
        """
        self._ensure_connected()
        wei = await self._run_sync(lambda: self._w3.eth.gas_price)
        return Decimal(str(wei)) / Decimal("1e9")  # Wei to Gwei

    async def get_matic_balance(self) -> Decimal:
        """Get native MATIC balance for the wallet.

        MATIC is required for gas payments on Polygon.

        Returns:
            MATIC balance as Decimal (in MATIC, not Wei).
        """
        self._ensure_connected()
        wei = await self._run_sync(
            lambda: self._w3.eth.get_balance(self._account.address)
        )
        return Decimal(str(wei)) / Decimal("1e18")  # Wei to MATIC

    async def estimate_gas(self, tx: dict) -> int:
        """Estimate gas for a transaction.

        Args:
            tx: Transaction dict with 'to', 'data', etc.

        Returns:
            Estimated gas units.
        """
        self._ensure_connected()

        # Ensure 'from' is set
        if "from" not in tx:
            tx = {**tx, "from": self._account.address}

        return await self._run_sync(lambda: self._w3.eth.estimate_gas(tx))

    async def send_transaction(
        self,
        to: str,
        value: int = 0,
        data: bytes = b"",
        gas: Optional[int] = None,
        gas_price: Optional[int] = None,
        nonce: Optional[int] = None,
    ) -> TxReceipt:
        """Send a generic transaction.

        Args:
            to: Recipient address.
            value: Amount of MATIC to send (in Wei).
            data: Transaction data (for contract calls).
            gas: Gas limit. If None, will be estimated.
            gas_price: Gas price in Wei. If None, uses current gas price.
            nonce: Transaction nonce. If None, fetched from chain.

        Returns:
            TxReceipt with transaction details.
        """
        self._ensure_connected()

        from web3 import Web3

        to_checksum = Web3.to_checksum_address(to)

        # Get nonce if not provided
        if nonce is None:
            nonce = await self._run_sync(
                lambda: self._w3.eth.get_transaction_count(self._account.address)
            )

        # Get gas price if not provided
        if gas_price is None:
            gas_price = await self._run_sync(lambda: self._w3.eth.gas_price)

        # Build transaction
        tx = {
            "from": self._account.address,
            "to": to_checksum,
            "value": value,
            "nonce": nonce,
            "gasPrice": gas_price,
            "chainId": 137,  # Polygon mainnet
        }

        if data:
            tx["data"] = data

        # Estimate gas if not provided
        if gas is None:
            try:
                gas_estimate = await self._run_sync(
                    lambda: self._w3.eth.estimate_gas(tx)
                )
                gas = int(gas_estimate * 1.2)  # 20% buffer
            except Exception as e:
                self._log.warning("gas_estimate_failed", error=str(e))
                gas = 21000  # Default for simple transfers

        tx["gas"] = gas

        self._log.info(
            "sending_transaction",
            to=to_checksum,
            value=value,
            gas=gas,
        )

        # Sign and send
        signed = await self._run_sync(
            lambda: self._w3.eth.account.sign_transaction(tx, self._private_key)
        )

        tx_hash = await self._run_sync(
            lambda: self._w3.eth.send_raw_transaction(signed.rawTransaction)
        )

        self._log.info("tx_submitted", tx_hash=tx_hash.hex())

        # Wait for receipt
        receipt = await self._run_sync(
            lambda: self._w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
        )

        result = TxReceipt(
            tx_hash=tx_hash.hex(),
            block_number=receipt["blockNumber"],
            gas_used=receipt["gasUsed"],
            status=receipt["status"] == 1,
        )

        self._log.info(
            "tx_complete",
            tx_hash=result.tx_hash,
            status="success" if result.status else "failed",
            gas_used=result.gas_used,
        )

        return result

    async def get_nonce(self) -> int:
        """Get current nonce for the wallet.

        Returns:
            Current transaction count (nonce).
        """
        self._ensure_connected()
        return await self._run_sync(
            lambda: self._w3.eth.get_transaction_count(self._account.address)
        )

    async def get_chain_id(self) -> int:
        """Get the chain ID.

        Returns:
            Chain ID (137 for Polygon mainnet).
        """
        self._ensure_connected()
        return await self._run_sync(lambda: self._w3.eth.chain_id)
