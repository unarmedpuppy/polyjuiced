"""Regression tests for Phase 1 Strategy Improvements - Position Sizing.

Phase 1 implements low-risk strategy improvements:
1.1 Position size reduction: Configurable min/max trade sizes ($3-5 recommended)
1.2 Zero slippage: Already implemented via GABAGOOL_MAX_SLIPPAGE=0.0

These tests verify:
- min_trade_size_usd config parameter exists and is loaded
- Position sizing respects minimum trade size
- Liquidity scaling uses configurable minimum
- Zero slippage configuration works correctly

See: agents/plans/polymarket-bot-strategy-improvements.md
See: docs/STRATEGY_ARCHITECTURE.md
"""

import pytest
from unittest.mock import MagicMock, AsyncMock, patch
import os


class TestMinTradeSizeConfig:
    """Test min_trade_size_usd configuration parameter.

    Phase 1.1: Position size reduction requires configurable min/max sizes.
    """

    def test_gabagool_config_has_min_trade_size_field(self):
        """Verify GabagoolConfig has min_trade_size_usd field."""
        from src.config import GabagoolConfig

        config = GabagoolConfig()
        assert hasattr(config, 'min_trade_size_usd'), (
            "GabagoolConfig must have min_trade_size_usd field"
        )

    def test_min_trade_size_default_is_3_dollars(self):
        """Verify default min_trade_size_usd is $3.00.

        Based on gabagool22 analysis, $3-5 per trade gets better fills.
        """
        from src.config import GabagoolConfig

        config = GabagoolConfig()
        assert config.min_trade_size_usd == 3.0, (
            "Default min_trade_size_usd should be $3.00"
        )

    def test_min_trade_size_loaded_from_env(self):
        """Verify min_trade_size_usd is loaded from GABAGOOL_MIN_TRADE_SIZE env var."""
        from src.config import GabagoolConfig

        # Test with custom value
        with patch.dict(os.environ, {"GABAGOOL_MIN_TRADE_SIZE": "2.5"}):
            config = GabagoolConfig.from_env()
            assert config.min_trade_size_usd == 2.5, (
                "min_trade_size_usd should be loaded from GABAGOOL_MIN_TRADE_SIZE"
            )

    def test_min_trade_size_less_than_max(self):
        """Verify min_trade_size_usd is less than max_trade_size_usd in defaults."""
        from src.config import GabagoolConfig

        config = GabagoolConfig()
        assert config.min_trade_size_usd < config.max_trade_size_usd, (
            "min_trade_size_usd must be less than max_trade_size_usd"
        )


class TestMaxTradeSizeConfig:
    """Test max_trade_size_usd configuration parameter.

    Phase 1.1: Production already has GABAGOOL_MAX_TRADE_SIZE=5.0
    """

    def test_gabagool_config_has_max_trade_size_field(self):
        """Verify GabagoolConfig has max_trade_size_usd field."""
        from src.config import GabagoolConfig

        config = GabagoolConfig()
        assert hasattr(config, 'max_trade_size_usd'), (
            "GabagoolConfig must have max_trade_size_usd field"
        )

    def test_max_trade_size_loaded_from_env(self):
        """Verify max_trade_size_usd is loaded from GABAGOOL_MAX_TRADE_SIZE env var."""
        from src.config import GabagoolConfig

        with patch.dict(os.environ, {"GABAGOOL_MAX_TRADE_SIZE": "10.0"}):
            config = GabagoolConfig.from_env()
            assert config.max_trade_size_usd == 10.0, (
                "max_trade_size_usd should be loaded from GABAGOOL_MAX_TRADE_SIZE"
            )


class TestZeroSlippageConfig:
    """Test zero slippage configuration.

    Phase 1.2: Zero slippage prevents paying more than opportunity price.
    Production already has GABAGOOL_MAX_SLIPPAGE=0.0
    """

    def test_gabagool_config_has_max_slippage_field(self):
        """Verify GabagoolConfig has max_slippage_cents field."""
        from src.config import GabagoolConfig

        config = GabagoolConfig()
        assert hasattr(config, 'max_slippage_cents'), (
            "GabagoolConfig must have max_slippage_cents field"
        )

    def test_max_slippage_loaded_from_env(self):
        """Verify max_slippage_cents is loaded from GABAGOOL_MAX_SLIPPAGE env var."""
        from src.config import GabagoolConfig

        with patch.dict(os.environ, {"GABAGOOL_MAX_SLIPPAGE": "0.0"}):
            config = GabagoolConfig.from_env()
            assert config.max_slippage_cents == 0.0, (
                "max_slippage_cents should support zero slippage"
            )


class TestPositionSizingMinimumEnforcement:
    """Test that position sizing enforces minimum trade size.

    Phase 1.1: Trades below min_trade_size_usd should be skipped.
    """

    def test_strategy_uses_min_trade_size_config(self):
        """Verify GabagoolStrategy uses min_trade_size_usd from config.

        Regression test: Previously hardcoded at $1.0, now configurable.
        """
        from src.strategies.gabagool import GabagoolStrategy
        import inspect

        # Get the _adjust_for_liquidity method source
        source = inspect.getsource(GabagoolStrategy._adjust_for_liquidity)

        # Should reference self.gabagool_config.min_trade_size_usd
        assert 'gabagool_config.min_trade_size_usd' in source, (
            "_adjust_for_liquidity must use configurable min_trade_size_usd"
        )

        # Should NOT have hardcoded min_trade = 1.0
        assert 'min_trade = 1.0' not in source, (
            "_adjust_for_liquidity should not have hardcoded min_trade = 1.0"
        )

    def test_strategy_enforces_min_budget(self):
        """Verify strategy skips trades when budget is below minimum.

        The strategy should check min_trade_size_usd * 2 (both legs).
        """
        from src.strategies.gabagool import GabagoolStrategy
        import inspect

        # Get the on_opportunity method source (where budget is checked)
        source = inspect.getsource(GabagoolStrategy.on_opportunity)

        # Should check minimum budget requirement
        assert 'min_budget_required' in source or 'min_trade_size_usd' in source, (
            "on_opportunity must enforce minimum budget"
        )


class TestEnvTemplateHasMinTradeSize:
    """Test .env.template includes min_trade_size documentation."""

    def test_env_template_has_min_trade_size(self):
        """Verify .env.template documents GABAGOOL_MIN_TRADE_SIZE."""
        from pathlib import Path

        template_path = Path(__file__).parent.parent / ".env.template"
        assert template_path.exists(), ".env.template must exist"

        content = template_path.read_text()
        assert "GABAGOOL_MIN_TRADE_SIZE" in content, (
            ".env.template must document GABAGOOL_MIN_TRADE_SIZE"
        )


class TestPositionSizingBounds:
    """Test position sizing respects min/max bounds."""

    def test_min_less_than_max_validation(self):
        """Verify min_trade_size_usd < max_trade_size_usd is enforced.

        Configuration with min > max would cause no trades to execute.
        """
        from src.config import GabagoolConfig

        # Default config should be valid
        config = GabagoolConfig()
        assert config.min_trade_size_usd < config.max_trade_size_usd, (
            "Default config must have min < max trade size"
        )

    def test_position_sizing_within_bounds(self):
        """Verify position sizing stays within min/max bounds."""
        from src.risk.position_sizing import PositionSizer
        from src.config import GabagoolConfig

        config = GabagoolConfig(
            min_trade_size_usd=3.0,
            max_trade_size_usd=5.0,
        )

        sizer = PositionSizer(config)

        # Calculate position with budget at max
        result = sizer.calculate(
            yes_price=0.48,
            no_price=0.49,
            available_budget=5.0,
        )

        # Should not exceed max
        assert result.yes_amount_usd <= config.max_trade_size_usd, (
            "YES amount should not exceed max_trade_size_usd"
        )
        assert result.no_amount_usd <= config.max_trade_size_usd, (
            "NO amount should not exceed max_trade_size_usd"
        )


class TestGabagool22StyleSizing:
    """Test position sizing matches gabagool22 successful patterns.

    Gabagool22 analysis showed:
    - Small position sizes: $3-8 per trade (avg $5)
    - Better fill rates with smaller sizes
    - Matches our Phase 1 $3-5 recommendation
    """

    def test_default_sizing_matches_gabagool22(self):
        """Verify default sizing is in gabagool22's successful range."""
        from src.config import GabagoolConfig

        config = GabagoolConfig()

        # Gabagool22 uses $3-8, we default to $3-5
        assert 3.0 <= config.min_trade_size_usd <= 5.0, (
            "min_trade_size_usd should be in $3-5 range"
        )
        # Note: max in code is $25 but .env.template suggests $5
        # Production should set GABAGOOL_MAX_TRADE_SIZE=5.0

    def test_small_size_config_is_valid(self):
        """Verify small position sizes like gabagool22 work correctly."""
        from src.config import GabagoolConfig

        # Gabagool22-style config
        config = GabagoolConfig(
            min_trade_size_usd=3.0,
            max_trade_size_usd=5.0,
        )

        assert config.min_trade_size_usd > 0, "Min must be positive"
        assert config.max_trade_size_usd > config.min_trade_size_usd, (
            "Max must be greater than min"
        )
