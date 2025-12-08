"""Base strategy class for Polymarket trading strategies."""

from abc import ABC, abstractmethod
from typing import Any, Dict, Optional

import structlog

from ..client.polymarket import PolymarketClient
from ..config import AppConfig

log = structlog.get_logger()


class BaseStrategy(ABC):
    """Abstract base class for trading strategies."""

    def __init__(
        self,
        client: PolymarketClient,
        config: AppConfig,
    ):
        """Initialize strategy.

        Args:
            client: Polymarket client
            config: Application configuration
        """
        self.client = client
        self.config = config
        self._running = False

    @property
    def name(self) -> str:
        """Strategy name."""
        return self.__class__.__name__

    @property
    def is_running(self) -> bool:
        """Check if strategy is running."""
        return self._running

    @abstractmethod
    async def start(self) -> None:
        """Start the strategy."""
        pass

    @abstractmethod
    async def stop(self) -> None:
        """Stop the strategy."""
        pass

    @abstractmethod
    async def on_opportunity(self, opportunity: Any) -> Optional[Dict[str, Any]]:
        """Handle a trading opportunity.

        Args:
            opportunity: Strategy-specific opportunity data

        Returns:
            Trade result or None if no trade executed
        """
        pass

    def log_trade(
        self,
        action: str,
        details: Dict[str, Any],
    ) -> None:
        """Log a trade action.

        Args:
            action: Trade action (e.g., "BUY", "SELL")
            details: Trade details
        """
        log.info(
            f"{self.name} trade",
            action=action,
            **details,
        )
