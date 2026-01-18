"""Gabagool arbitrage strategy for binary markets.

This strategy exploits temporary mispricing in binary markets where:
- YES + NO should sum to $1.00
- When sum < $1.00, buying both guarantees profit
- Profit = $1.00 - (YES_cost + NO_cost)

Named after the successful Polymarket trader @gabagool22.
"""

from mercury.strategies.gabagool.config import GabagoolConfig
from mercury.strategies.gabagool.strategy import GabagoolStrategy

__all__ = ["GabagoolStrategy", "GabagoolConfig"]
