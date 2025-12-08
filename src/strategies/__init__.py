"""Trading strategies for Polymarket bot."""

from .base import BaseStrategy
from .gabagool import GabagoolStrategy

__all__ = ["BaseStrategy", "GabagoolStrategy"]
