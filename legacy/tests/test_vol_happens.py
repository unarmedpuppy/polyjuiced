"""Regression tests for Vol Happens Strategy.

Vol Happens is a volatility/mean reversion strategy that:
1. Buys one side when price drops to $0.48 (with trend filter)
2. Waits for other side to also hit $0.48
3. Completes hedge with equal shares (not dollars)
4. Hard exits unhedged positions at 3:30 remaining

These tests verify:
- Configuration loading from environment
- Entry condition detection (price + trend filter)
- Position state management
- Exit timing logic

See: agents/plans/vol-happens-strategy.md
"""

import pytest
from unittest.mock import MagicMock, AsyncMock, patch
import os
from datetime import datetime, timedelta


class TestVolHappensConfig:
    """Test VolHappensConfig configuration."""

    def test_vol_happens_config_exists(self):
        """Verify VolHappensConfig class exists."""
        from src.config import VolHappensConfig

        config = VolHappensConfig()
        assert config is not None

    def test_vol_happens_config_defaults(self):
        """Verify default configuration values."""
        from src.config import VolHappensConfig

        config = VolHappensConfig()

        # Check defaults match spec
        assert config.enabled is False
        assert config.entry_price_threshold == 0.48
        assert config.trend_filter_threshold == 0.52
        assert config.first_leg_size_usd == 2.00
        assert config.max_position_usd == 5.00
        assert config.exit_time_remaining_seconds == 210.0  # 3:30
        assert config.min_time_to_enter_seconds == 300.0  # 5 min
        assert config.dry_run is True

    def test_vol_happens_config_from_env(self):
        """Verify configuration loads from environment variables."""
        from src.config import VolHappensConfig

        env_vars = {
            "VOL_HAPPENS_ENABLED": "true",
            "VOL_HAPPENS_MARKETS": "BTC,ETH",
            "VOL_HAPPENS_ENTRY_PRICE": "0.45",
            "VOL_HAPPENS_TREND_FILTER": "0.55",
            "VOL_HAPPENS_FIRST_LEG_SIZE": "3.00",
            "VOL_HAPPENS_MAX_POSITION": "10.00",
            "VOL_HAPPENS_EXIT_TIME": "180.0",
            "VOL_HAPPENS_DRY_RUN": "false",
        }

        with patch.dict(os.environ, env_vars):
            config = VolHappensConfig.from_env()

            assert config.enabled is True
            assert config.markets == ["BTC", "ETH"]
            assert config.entry_price_threshold == 0.45
            assert config.trend_filter_threshold == 0.55
            assert config.first_leg_size_usd == 3.00
            assert config.max_position_usd == 10.00
            assert config.exit_time_remaining_seconds == 180.0
            assert config.dry_run is False

    def test_vol_happens_in_app_config(self):
        """Verify VolHappensConfig is included in AppConfig."""
        from src.config import AppConfig

        # Check that vol_happens attribute exists
        assert hasattr(AppConfig, '__dataclass_fields__')
        assert 'vol_happens' in AppConfig.__dataclass_fields__


class TestVolHappensStrategy:
    """Test VolHappensStrategy class."""

    def test_strategy_exists(self):
        """Verify VolHappensStrategy class exists."""
        from src.strategies.vol_happens import VolHappensStrategy

        assert VolHappensStrategy is not None

    def test_strategy_has_required_methods(self):
        """Verify strategy implements required interface."""
        from src.strategies.vol_happens import VolHappensStrategy

        # Check required methods exist
        assert hasattr(VolHappensStrategy, 'start')
        assert hasattr(VolHappensStrategy, 'stop')
        assert hasattr(VolHappensStrategy, 'on_opportunity')

    def test_strategy_id_is_vol_happens(self):
        """Verify strategy ID is 'vol_happens'."""
        from src.strategies.vol_happens import VolHappensStrategy

        assert VolHappensStrategy.STRATEGY_ID == "vol_happens"


class TestVolHappensPosition:
    """Test VolHappensPosition data class."""

    def test_position_state_enum(self):
        """Verify PositionState enum values."""
        from src.strategies.vol_happens import PositionState

        assert PositionState.WAITING_FOR_HEDGE.value == "WAITING_FOR_HEDGE"
        assert PositionState.HEDGED.value == "HEDGED"
        assert PositionState.FORCE_EXIT.value == "FORCE_EXIT"
        assert PositionState.RESOLVED.value == "RESOLVED"
        assert PositionState.CLOSED.value == "CLOSED"

    def test_position_is_hedged_false_initially(self):
        """Verify position is not hedged when first created."""
        from src.strategies.vol_happens import VolHappensPosition
        from datetime import datetime

        # Create mock market
        mock_market = MagicMock()
        mock_market.condition_id = "test123"

        position = VolHappensPosition(
            id="pos1",
            market=mock_market,
            first_leg_side="YES",
            first_leg_shares=4.17,
            first_leg_price=0.48,
            first_leg_cost=2.00,
            first_leg_filled_at=datetime.utcnow(),
        )

        assert position.is_hedged is False
        assert position.total_cost == 2.00
        assert position.spread_captured == 0.0

    def test_position_is_hedged_after_second_leg(self):
        """Verify position is hedged after second leg fills."""
        from src.strategies.vol_happens import VolHappensPosition, PositionState
        from datetime import datetime

        mock_market = MagicMock()
        mock_market.condition_id = "test123"

        position = VolHappensPosition(
            id="pos1",
            market=mock_market,
            first_leg_side="YES",
            first_leg_shares=4.17,
            first_leg_price=0.48,
            first_leg_cost=2.00,
            first_leg_filled_at=datetime.utcnow(),
            # Second leg
            second_leg_shares=4.17,
            second_leg_price=0.46,
            second_leg_cost=1.92,
            second_leg_filled_at=datetime.utcnow(),
            state=PositionState.HEDGED,
        )

        assert position.is_hedged is True
        assert position.total_cost == pytest.approx(3.92, 0.01)
        assert position.spread_captured == pytest.approx(0.06, 0.01)  # 1 - 0.48 - 0.46


class TestEntryConditions:
    """Test entry condition logic."""

    def test_entry_requires_price_at_threshold(self):
        """Entry should only trigger when price <= entry_price_threshold."""
        from src.config import VolHappensConfig

        config = VolHappensConfig()

        # Price at threshold - should enter
        yes_price = 0.48
        no_price = 0.52
        assert yes_price <= config.entry_price_threshold
        assert no_price <= config.trend_filter_threshold

        # Price above threshold - should not enter
        yes_price = 0.50
        assert yes_price > config.entry_price_threshold

    def test_entry_requires_trend_filter(self):
        """Entry should only trigger when other side <= trend_filter_threshold."""
        from src.config import VolHappensConfig

        config = VolHappensConfig()

        # YES at threshold, NO within filter - should enter
        yes_price = 0.48
        no_price = 0.52
        entry_allowed = (
            yes_price <= config.entry_price_threshold and
            no_price <= config.trend_filter_threshold
        )
        assert entry_allowed is True

        # YES at threshold, NO above filter (strong trend) - should NOT enter
        yes_price = 0.48
        no_price = 0.55
        entry_allowed = (
            yes_price <= config.entry_price_threshold and
            no_price <= config.trend_filter_threshold
        )
        assert entry_allowed is False


class TestExitTiming:
    """Test exit timing logic."""

    def test_force_exit_threshold(self):
        """Force exit should trigger when time remaining <= exit_time_remaining_seconds."""
        from src.config import VolHappensConfig

        config = VolHappensConfig()

        # 4 minutes remaining - should NOT force exit (> 3:30)
        time_remaining = 240  # 4 minutes
        should_exit = time_remaining <= config.exit_time_remaining_seconds
        assert should_exit is False

        # 3:30 remaining - should force exit
        time_remaining = 210  # 3:30
        should_exit = time_remaining <= config.exit_time_remaining_seconds
        assert should_exit is True

        # 2 minutes remaining - should definitely force exit
        time_remaining = 120
        should_exit = time_remaining <= config.exit_time_remaining_seconds
        assert should_exit is True

    def test_min_time_to_enter(self):
        """Should not enter when time remaining < min_time_to_enter_seconds."""
        from src.config import VolHappensConfig

        config = VolHappensConfig()

        # 10 minutes remaining - can enter
        time_remaining = 600
        can_enter = time_remaining >= config.min_time_to_enter_seconds
        assert can_enter is True

        # 5 minutes remaining - can enter (exactly at threshold)
        time_remaining = 300
        can_enter = time_remaining >= config.min_time_to_enter_seconds
        assert can_enter is True

        # 4 minutes remaining - cannot enter
        time_remaining = 240
        can_enter = time_remaining >= config.min_time_to_enter_seconds
        assert can_enter is False


class TestStrategyIdColumn:
    """Test strategy_id column in trades table."""

    def test_strategy_id_in_migrations(self):
        """Verify strategy_id column is added via migration."""
        from src.persistence import Database
        import inspect

        # Get the _migrate_schema method source
        source = inspect.getsource(Database._migrate_schema)

        # Should include strategy_id migration
        assert "strategy_id" in source, (
            "_migrate_schema must include strategy_id column migration"
        )

    def test_record_trade_accepts_strategy_id(self):
        """Verify record_trade method accepts strategy_id parameter."""
        from src.persistence import Database
        import inspect

        # Get the record_trade method signature
        sig = inspect.signature(Database.record_trade)
        params = list(sig.parameters.keys())

        assert "strategy_id" in params, (
            "record_trade must accept strategy_id parameter"
        )


class TestEnvTemplate:
    """Test .env.template includes Vol Happens configuration."""

    def test_env_template_has_vol_happens_section(self):
        """Verify .env.template documents Vol Happens config."""
        from pathlib import Path

        template_path = Path(__file__).parent.parent / ".env.template"
        assert template_path.exists(), ".env.template must exist"

        content = template_path.read_text()

        # Check for Vol Happens section
        assert "VOL_HAPPENS" in content, (
            ".env.template must document Vol Happens configuration"
        )
        assert "VOL_HAPPENS_ENABLED" in content
        assert "VOL_HAPPENS_ENTRY_PRICE" in content
        assert "VOL_HAPPENS_TREND_FILTER" in content
        assert "VOL_HAPPENS_EXIT_TIME" in content


class TestStrategyExport:
    """Test VolHappensStrategy is exported from strategies package."""

    def test_strategy_exported(self):
        """Verify VolHappensStrategy is exported from strategies/__init__.py."""
        from src.strategies import VolHappensStrategy

        assert VolHappensStrategy is not None

    def test_strategy_in_all(self):
        """Verify VolHappensStrategy is in __all__."""
        from src import strategies

        assert "VolHappensStrategy" in strategies.__all__
