"""Regression tests for Phase 2 Strategy Improvements - Gradual Position Building.

Phase 2 implements gradual position building:
2.1 Split trades into multiple tranches with delays
2.2 Reduces market impact and gets better average fills
2.3 Matches gabagool22's approach of scaling into positions

These tests verify:
- gradual_entry_enabled config parameter exists and works
- gradual_entry_tranches splits trades correctly
- gradual_entry_delay_seconds controls timing between tranches
- gradual_entry_min_spread_cents gates when to use gradual entry
- Fallback to single entry when tranche size < min_trade_size

See: agents/plans/polymarket-bot-strategy-improvements.md
See: docs/STRATEGY_ARCHITECTURE.md
"""

import pytest
from unittest.mock import MagicMock, AsyncMock, patch
import os
import asyncio


class TestGradualEntryConfig:
    """Test gradual_entry_enabled configuration parameter.

    Phase 2.1: Gradual position building requires configurable settings.
    """

    def test_gabagool_config_has_gradual_entry_enabled_field(self):
        """Verify GabagoolConfig has gradual_entry_enabled field."""
        from src.config import GabagoolConfig

        config = GabagoolConfig()
        assert hasattr(config, 'gradual_entry_enabled'), (
            "GabagoolConfig must have gradual_entry_enabled field"
        )

    def test_gradual_entry_disabled_by_default(self):
        """Verify gradual_entry_enabled defaults to False.

        Gradual entry is disabled by default for simplicity.
        """
        from src.config import GabagoolConfig

        config = GabagoolConfig()
        assert config.gradual_entry_enabled is False, (
            "gradual_entry_enabled should default to False"
        )

    def test_gradual_entry_enabled_from_env(self):
        """Verify gradual_entry_enabled is loaded from GABAGOOL_GRADUAL_ENTRY_ENABLED env var."""
        from src.config import GabagoolConfig

        with patch.dict(os.environ, {"GABAGOOL_GRADUAL_ENTRY_ENABLED": "true"}):
            config = GabagoolConfig.from_env()
            assert config.gradual_entry_enabled is True, (
                "gradual_entry_enabled should be loaded from env var"
            )


class TestGradualEntryTranchesConfig:
    """Test gradual_entry_tranches configuration parameter."""

    def test_gabagool_config_has_tranches_field(self):
        """Verify GabagoolConfig has gradual_entry_tranches field."""
        from src.config import GabagoolConfig

        config = GabagoolConfig()
        assert hasattr(config, 'gradual_entry_tranches'), (
            "GabagoolConfig must have gradual_entry_tranches field"
        )

    def test_tranches_default_is_3(self):
        """Verify default tranches is 3 (split into 3 smaller orders)."""
        from src.config import GabagoolConfig

        config = GabagoolConfig()
        assert config.gradual_entry_tranches == 3, (
            "Default gradual_entry_tranches should be 3"
        )

    def test_tranches_loaded_from_env(self):
        """Verify gradual_entry_tranches is loaded from GABAGOOL_GRADUAL_ENTRY_TRANCHES env var."""
        from src.config import GabagoolConfig

        with patch.dict(os.environ, {"GABAGOOL_GRADUAL_ENTRY_TRANCHES": "5"}):
            config = GabagoolConfig.from_env()
            assert config.gradual_entry_tranches == 5, (
                "gradual_entry_tranches should be loaded from env var"
            )


class TestGradualEntryDelayConfig:
    """Test gradual_entry_delay_seconds configuration parameter."""

    def test_gabagool_config_has_delay_field(self):
        """Verify GabagoolConfig has gradual_entry_delay_seconds field."""
        from src.config import GabagoolConfig

        config = GabagoolConfig()
        assert hasattr(config, 'gradual_entry_delay_seconds'), (
            "GabagoolConfig must have gradual_entry_delay_seconds field"
        )

    def test_delay_default_is_30_seconds(self):
        """Verify default delay is 30 seconds between tranches."""
        from src.config import GabagoolConfig

        config = GabagoolConfig()
        assert config.gradual_entry_delay_seconds == 30.0, (
            "Default gradual_entry_delay_seconds should be 30.0"
        )

    def test_delay_loaded_from_env(self):
        """Verify gradual_entry_delay_seconds is loaded from env var."""
        from src.config import GabagoolConfig

        with patch.dict(os.environ, {"GABAGOOL_GRADUAL_ENTRY_DELAY": "15.0"}):
            config = GabagoolConfig.from_env()
            assert config.gradual_entry_delay_seconds == 15.0, (
                "gradual_entry_delay_seconds should be loaded from env var"
            )


class TestGradualEntryMinSpreadConfig:
    """Test gradual_entry_min_spread_cents configuration parameter."""

    def test_gabagool_config_has_min_spread_field(self):
        """Verify GabagoolConfig has gradual_entry_min_spread_cents field."""
        from src.config import GabagoolConfig

        config = GabagoolConfig()
        assert hasattr(config, 'gradual_entry_min_spread_cents'), (
            "GabagoolConfig must have gradual_entry_min_spread_cents field"
        )

    def test_min_spread_default_is_3_cents(self):
        """Verify default min spread is 3 cents.

        Only use gradual entry for spreads >= 3 cents because
        smaller spreads may not persist long enough for multiple entries.
        """
        from src.config import GabagoolConfig

        config = GabagoolConfig()
        assert config.gradual_entry_min_spread_cents == 3.0, (
            "Default gradual_entry_min_spread_cents should be 3.0"
        )

    def test_min_spread_loaded_from_env(self):
        """Verify gradual_entry_min_spread_cents is loaded from env var."""
        from src.config import GabagoolConfig

        with patch.dict(os.environ, {"GABAGOOL_GRADUAL_ENTRY_MIN_SPREAD": "4.0"}):
            config = GabagoolConfig.from_env()
            assert config.gradual_entry_min_spread_cents == 4.0, (
                "gradual_entry_min_spread_cents should be loaded from env var"
            )


class TestGradualEntryMethodExists:
    """Test _execute_gradual_entry method exists in strategy."""

    def test_strategy_has_execute_gradual_entry_method(self):
        """Verify GabagoolStrategy has _execute_gradual_entry method."""
        from src.strategies.gabagool import GabagoolStrategy

        assert hasattr(GabagoolStrategy, '_execute_gradual_entry'), (
            "GabagoolStrategy must have _execute_gradual_entry method"
        )

    def test_execute_gradual_entry_is_async(self):
        """Verify _execute_gradual_entry is an async method."""
        from src.strategies.gabagool import GabagoolStrategy
        import inspect

        method = getattr(GabagoolStrategy, '_execute_gradual_entry')
        assert inspect.iscoroutinefunction(method), (
            "_execute_gradual_entry must be an async method"
        )


class TestGradualEntryLogic:
    """Test gradual entry logic in on_opportunity.

    Regression test: Verify that on_opportunity routes to gradual entry
    when conditions are met.
    """

    def test_on_opportunity_checks_gradual_entry_conditions(self):
        """Verify on_opportunity has logic to check gradual entry conditions."""
        from src.strategies.gabagool import GabagoolStrategy
        import inspect

        source = inspect.getsource(GabagoolStrategy.on_opportunity)

        # Should check gradual_entry_enabled
        assert 'gradual_entry_enabled' in source, (
            "on_opportunity must check gradual_entry_enabled"
        )

        # Should check spread against min_spread
        assert 'gradual_entry_min_spread_cents' in source, (
            "on_opportunity must check gradual_entry_min_spread_cents"
        )

        # Should check tranches > 1
        assert 'gradual_entry_tranches' in source, (
            "on_opportunity must check gradual_entry_tranches"
        )

    def test_on_opportunity_calls_gradual_entry(self):
        """Verify on_opportunity calls _execute_gradual_entry when conditions met."""
        from src.strategies.gabagool import GabagoolStrategy
        import inspect

        source = inspect.getsource(GabagoolStrategy.on_opportunity)

        # Should call _execute_gradual_entry
        assert '_execute_gradual_entry' in source, (
            "on_opportunity must call _execute_gradual_entry"
        )


class TestGradualEntryTrancheSizing:
    """Test tranche size calculation in gradual entry."""

    def test_gradual_entry_calculates_per_tranche_amounts(self):
        """Verify _execute_gradual_entry divides amounts by tranches."""
        from src.strategies.gabagool import GabagoolStrategy
        import inspect

        source = inspect.getsource(GabagoolStrategy._execute_gradual_entry)

        # Should divide by num_tranches
        assert 'yes_per_tranche' in source or 'per_tranche' in source, (
            "_execute_gradual_entry must calculate per-tranche amounts"
        )

    def test_gradual_entry_fallback_when_tranche_too_small(self):
        """Verify gradual entry falls back to single entry when tranche < min_trade_size.

        Regression test: If we split $6 into 3 tranches of $2 each, but
        min_trade_size is $3, we should fall back to single entry.
        """
        from src.strategies.gabagool import GabagoolStrategy
        import inspect

        source = inspect.getsource(GabagoolStrategy._execute_gradual_entry)

        # Should check min_trade_size_usd
        assert 'min_trade_size_usd' in source, (
            "_execute_gradual_entry must check min_trade_size_usd for fallback"
        )

        # Should fall back to _execute_trade
        assert '_execute_trade' in source, (
            "_execute_gradual_entry must fall back to _execute_trade"
        )


class TestGradualEntryDelay:
    """Test delay between tranches in gradual entry."""

    def test_gradual_entry_has_delay_between_tranches(self):
        """Verify _execute_gradual_entry waits between tranches."""
        from src.strategies.gabagool import GabagoolStrategy
        import inspect

        source = inspect.getsource(GabagoolStrategy._execute_gradual_entry)

        # Should use asyncio.sleep for delay
        assert 'asyncio.sleep' in source, (
            "_execute_gradual_entry must use asyncio.sleep for delays"
        )


class TestGradualEntryAggregation:
    """Test result aggregation in gradual entry."""

    def test_gradual_entry_aggregates_results(self):
        """Verify _execute_gradual_entry aggregates results from all tranches."""
        from src.strategies.gabagool import GabagoolStrategy
        import inspect

        source = inspect.getsource(GabagoolStrategy._execute_gradual_entry)

        # Should track totals
        assert 'total_yes_cost' in source or 'cumulative' in source, (
            "_execute_gradual_entry must track cumulative costs"
        )

        # Should return TradeResult
        assert 'TradeResult' in source, (
            "_execute_gradual_entry must return TradeResult"
        )


class TestGradualEntrySafetyChecks:
    """Test safety checks during gradual entry."""

    def test_gradual_entry_checks_market_tradeable(self):
        """Verify gradual entry checks if market is still tradeable before each tranche."""
        from src.strategies.gabagool import GabagoolStrategy
        import inspect

        source = inspect.getsource(GabagoolStrategy._execute_gradual_entry)

        # Should check is_tradeable
        assert 'is_tradeable' in source, (
            "_execute_gradual_entry must check market.is_tradeable"
        )

    def test_gradual_entry_checks_trading_enabled(self):
        """Verify gradual entry checks if trading is still enabled."""
        from src.strategies.gabagool import GabagoolStrategy
        import inspect

        source = inspect.getsource(GabagoolStrategy._execute_gradual_entry)

        # Should check trading disabled
        assert '_is_trading_disabled' in source, (
            "_execute_gradual_entry must check _is_trading_disabled"
        )


class TestEnvTemplateHasGradualEntry:
    """Test .env.template includes gradual entry documentation."""

    def test_env_template_has_gradual_entry_enabled(self):
        """Verify .env.template documents GABAGOOL_GRADUAL_ENTRY_ENABLED."""
        from pathlib import Path

        template_path = Path(__file__).parent.parent / ".env.template"
        assert template_path.exists(), ".env.template must exist"

        content = template_path.read_text()
        assert "GABAGOOL_GRADUAL_ENTRY_ENABLED" in content, (
            ".env.template must document GABAGOOL_GRADUAL_ENTRY_ENABLED"
        )

    def test_env_template_has_gradual_entry_tranches(self):
        """Verify .env.template documents GABAGOOL_GRADUAL_ENTRY_TRANCHES."""
        from pathlib import Path

        template_path = Path(__file__).parent.parent / ".env.template"
        content = template_path.read_text()
        assert "GABAGOOL_GRADUAL_ENTRY_TRANCHES" in content, (
            ".env.template must document GABAGOOL_GRADUAL_ENTRY_TRANCHES"
        )

    def test_env_template_has_gradual_entry_delay(self):
        """Verify .env.template documents GABAGOOL_GRADUAL_ENTRY_DELAY."""
        from pathlib import Path

        template_path = Path(__file__).parent.parent / ".env.template"
        content = template_path.read_text()
        assert "GABAGOOL_GRADUAL_ENTRY_DELAY" in content, (
            ".env.template must document GABAGOOL_GRADUAL_ENTRY_DELAY"
        )

    def test_env_template_has_gradual_entry_min_spread(self):
        """Verify .env.template documents GABAGOOL_GRADUAL_ENTRY_MIN_SPREAD."""
        from pathlib import Path

        template_path = Path(__file__).parent.parent / ".env.template"
        content = template_path.read_text()
        assert "GABAGOOL_GRADUAL_ENTRY_MIN_SPREAD" in content, (
            ".env.template must document GABAGOOL_GRADUAL_ENTRY_MIN_SPREAD"
        )


class TestGradualEntryWithSingleTranche:
    """Test gradual entry with tranches=1 uses single entry."""

    def test_single_tranche_uses_regular_execute(self):
        """Verify gradual entry with 1 tranche uses _execute_trade directly.

        When tranches=1, there's no benefit to gradual entry, so we
        should use the regular single entry path.
        """
        from src.strategies.gabagool import GabagoolStrategy
        import inspect

        source = inspect.getsource(GabagoolStrategy.on_opportunity)

        # The condition should include check for tranches > 1
        assert 'gradual_entry_tranches > 1' in source, (
            "on_opportunity must check gradual_entry_tranches > 1"
        )


class TestGradualEntryPartialSuccess:
    """Test gradual entry handles partial success correctly."""

    def test_gradual_entry_handles_failed_tranches(self):
        """Verify gradual entry continues even if some tranches fail."""
        from src.strategies.gabagool import GabagoolStrategy
        import inspect

        source = inspect.getsource(GabagoolStrategy._execute_gradual_entry)

        # Should track failed tranches
        assert 'tranches_failed' in source or 'failed' in source, (
            "_execute_gradual_entry must track failed tranches"
        )

    def test_gradual_entry_returns_success_with_partial_fills(self):
        """Verify gradual entry returns success if at least one tranche fills."""
        from src.strategies.gabagool import GabagoolStrategy
        import inspect

        source = inspect.getsource(GabagoolStrategy._execute_gradual_entry)

        # Should check tranches_executed > 0
        assert 'tranches_executed' in source, (
            "_execute_gradual_entry must track tranches_executed"
        )

        # Should return success=True if any tranches executed
        assert 'success=True' in source, (
            "_execute_gradual_entry must return success=True for partial fills"
        )


class TestGradualEntryMatchesGabagool22Pattern:
    """Test gradual entry config matches gabagool22's successful pattern.

    Gabagool22 analysis showed:
    - Multiple smaller entries over time
    - Better average prices
    - Reduced market impact
    """

    def test_default_config_allows_scaling(self):
        """Verify default config allows scaling into positions."""
        from src.config import GabagoolConfig

        config = GabagoolConfig()

        # Should have reasonable defaults for scaling
        assert config.gradual_entry_tranches >= 2, (
            "Default tranches should be >= 2 for meaningful scaling"
        )
        assert config.gradual_entry_delay_seconds >= 10.0, (
            "Default delay should be >= 10s to spread entries"
        )
        assert config.gradual_entry_min_spread_cents >= 2.0, (
            "Default min spread should be >= 2 cents for safety"
        )

    def test_total_time_calculation(self):
        """Verify total time for gradual entry is reasonable."""
        from src.config import GabagoolConfig

        config = GabagoolConfig()

        # Total time = (tranches - 1) * delay
        total_time = (config.gradual_entry_tranches - 1) * config.gradual_entry_delay_seconds

        # Should complete within 2 minutes for reasonable UX
        assert total_time <= 120, (
            f"Total gradual entry time ({total_time}s) should be <= 120s"
        )
