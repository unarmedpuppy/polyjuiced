"""Base Strategy Protocol - interface for all trading strategies.

Strategies implement this protocol to integrate with the StrategyEngine.
"""

from abc import abstractmethod
from typing import AsyncIterator, Protocol, runtime_checkable

from mercury.domain.market import OrderBook
from mercury.domain.signal import TradingSignal


@runtime_checkable
class BaseStrategy(Protocol):
    """Protocol that all trading strategies must implement.

    Strategies:
    - Receive market data via on_market_data()
    - Emit trading signals as async generators
    - Are stateless with respect to execution (no order tracking)
    - Can be enabled/disabled at runtime
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Unique strategy identifier.

        Used for configuration, logging, and metrics.
        """
        ...

    @property
    @abstractmethod
    def enabled(self) -> bool:
        """Whether strategy is currently enabled."""
        ...

    @abstractmethod
    async def start(self) -> None:
        """Initialize strategy resources.

        Called when the StrategyEngine starts.
        """
        ...

    @abstractmethod
    async def stop(self) -> None:
        """Cleanup strategy resources.

        Called when the StrategyEngine stops.
        """
        ...

    @abstractmethod
    async def on_market_data(
        self,
        market_id: str,
        book: OrderBook,
    ) -> AsyncIterator[TradingSignal]:
        """Process market data and yield trading signals.

        This is an async generator - strategies can yield 0, 1, or multiple
        signals per market data update.

        Args:
            market_id: The market's condition ID.
            book: Current order book snapshot (YES + NO sides).

        Yields:
            TradingSignal for each trading opportunity detected.
        """
        ...

    @abstractmethod
    def get_subscribed_markets(self) -> list[str]:
        """Return list of market IDs this strategy wants data for.

        The StrategyEngine uses this to route market data to strategies.
        """
        ...

    def enable(self) -> None:
        """Enable the strategy at runtime.

        Default implementation - override if needed.
        """
        pass

    def disable(self) -> None:
        """Disable the strategy at runtime.

        Default implementation - override if needed.
        """
        pass
