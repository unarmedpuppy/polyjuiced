"""Liquidity data collection and analysis module.

This module provides infrastructure for collecting fill records and depth snapshots
to build persistence/slippage models for liquidity-aware position sizing.

See docs/LIQUIDITY_SIZING.md for the roadmap and rationale.
"""

from .models import FillRecord, LiquiditySnapshot, DepthLevel
from .collector import LiquidityCollector

__all__ = [
    "FillRecord",
    "LiquiditySnapshot",
    "DepthLevel",
    "LiquidityCollector",
]
