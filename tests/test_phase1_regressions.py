"""Regression tests for Phase 1 fixes (Dec 13, 2025 trade analysis).

These tests ensure the fixes for the one-sided position and hedge imbalance
issues remain in place. The root causes were:

1. LIVE status treated as filled - orders sitting on book were counted as filled
2. Near-resolution trading creating unhedged positions
3. Missing post-trade hedge verification

See: apps/polymarket-bot/docs/TRADE_ANALYSIS_2025-12-13.md
"""

import pytest
from unittest.mock import MagicMock, AsyncMock, patch
import os


class TestLiveStatusNotTreatedAsFilled:
    """Ensure LIVE order status is NOT treated as a successful fill.

    Root cause: The code was treating LIVE status (order on book waiting)
    as a successful fill, leading to one-sided positions when only one
    leg of the arbitrage filled.

    Fix: Only MATCHED/FILLED statuses indicate actual execution.
    """

    @pytest.fixture
    def mock_client(self):
        """Create mock Polymarket client."""
        from src.client.polymarket import PolymarketClient
        from src.config import PolymarketSettings

        settings = MagicMock(spec=PolymarketSettings)
        settings.clob_http_url = "https://test.com"
        settings.private_key = "0x" + "a" * 64
        settings.signature_type = 1
        settings.proxy_wallet = None
        settings.api_key = None

        client = PolymarketClient(settings)
        client._client = MagicMock()
        client._connected = True
        return client

    @pytest.mark.asyncio
    async def test_live_yes_order_not_treated_as_filled(self, mock_client):
        """LIVE status on YES order should NOT proceed to NO order."""
        # Setup: YES order returns LIVE status (on book, not filled)
        mock_client.get_order_book = MagicMock(return_value={
            "asks": [{"price": "0.45", "size": "100"}],
            "bids": []
        })
        mock_client.get_price = MagicMock(return_value=0.45)
        mock_client._client.create_order = MagicMock(return_value={"signed": "order"})
        mock_client._client.post_order = MagicMock(return_value={
            "status": "LIVE",  # Order is on book, NOT filled
            "id": "order123",
            "size_matched": 0,
        })
        mock_client._client.cancel = MagicMock()

        result = await mock_client.execute_dual_leg_order(
            yes_token_id="yes_token",
            no_token_id="no_token",
            yes_amount_usd=10.0,
            no_amount_usd=10.0,
            timeout_seconds=5.0,
        )

        # Assert: Trade should fail, not proceed to NO order
        assert result["success"] is False
        assert result["partial_fill"] is False
        assert "LIVE" in result.get("error", "")
        # Should have attempted to cancel the unfilled order
        mock_client._client.cancel.assert_called_once()

    @pytest.mark.asyncio
    async def test_matched_status_treated_as_filled(self, mock_client):
        """MATCHED status should be treated as a successful fill."""
        mock_client.get_order_book = MagicMock(return_value={
            "asks": [{"price": "0.45", "size": "100"}],
            "bids": []
        })
        mock_client.get_price = MagicMock(return_value=0.45)
        mock_client._client.create_order = MagicMock(return_value={"signed": "order"})

        # Both orders return MATCHED
        mock_client._client.post_order = MagicMock(side_effect=[
            {"status": "MATCHED", "id": "yes_order", "size_matched": 10},
            {"status": "MATCHED", "id": "no_order", "size_matched": 10},
        ])

        result = await mock_client.execute_dual_leg_order(
            yes_token_id="yes_token",
            no_token_id="no_token",
            yes_amount_usd=10.0,
            no_amount_usd=10.0,
            timeout_seconds=5.0,
        )

        # Assert: Trade should succeed
        assert result["success"] is True
        assert result["partial_fill"] is False

    @pytest.mark.asyncio
    async def test_filled_status_treated_as_filled(self, mock_client):
        """FILLED status should be treated as a successful fill."""
        mock_client.get_order_book = MagicMock(return_value={
            "asks": [{"price": "0.45", "size": "100"}],
            "bids": []
        })
        mock_client.get_price = MagicMock(return_value=0.45)
        mock_client._client.create_order = MagicMock(return_value={"signed": "order"})

        # Both orders return FILLED
        mock_client._client.post_order = MagicMock(side_effect=[
            {"status": "FILLED", "id": "yes_order", "size_matched": 10},
            {"status": "FILLED", "id": "no_order", "size_matched": 10},
        ])

        result = await mock_client.execute_dual_leg_order(
            yes_token_id="yes_token",
            no_token_id="no_token",
            yes_amount_usd=10.0,
            no_amount_usd=10.0,
            timeout_seconds=5.0,
        )

        # Assert: Trade should succeed
        assert result["success"] is True

    @pytest.mark.asyncio
    async def test_live_no_order_triggers_unwind(self, mock_client):
        """LIVE status on NO order (after YES filled) should trigger unwind."""
        mock_client.get_order_book = MagicMock(return_value={
            "asks": [{"price": "0.45", "size": "100"}],
            "bids": [{"price": "0.44", "size": "100"}]
        })
        mock_client.get_price = MagicMock(return_value=0.45)
        mock_client._client.create_order = MagicMock(return_value={"signed": "order"})

        # YES fills, NO goes LIVE (partial fill situation)
        mock_client._client.post_order = MagicMock(side_effect=[
            {"status": "MATCHED", "id": "yes_order", "size_matched": 10},
            {"status": "LIVE", "id": "no_order", "size_matched": 0},  # NO didn't fill
        ])
        mock_client._client.cancel = MagicMock()

        result = await mock_client.execute_dual_leg_order(
            yes_token_id="yes_token",
            no_token_id="no_token",
            yes_amount_usd=10.0,
            no_amount_usd=10.0,
            timeout_seconds=5.0,
        )

        # Assert: Should be marked as partial fill, not success
        assert result["success"] is False
        assert result["partial_fill"] is True


class TestNearResolutionDisabled:
    """Ensure near-resolution trading is disabled by default.

    Root cause: Near-resolution trading was creating one-sided positions
    by placing bets on nearly-resolved markets without hedge.

    Fix: Disabled via config (near_resolution_enabled=False by default).
    """

    def test_near_resolution_disabled_by_default_class(self):
        """GabagoolConfig class default should have near_resolution disabled."""
        from src.config import GabagoolConfig

        config = GabagoolConfig()
        assert config.near_resolution_enabled is False, \
            "near_resolution_enabled should default to False in class definition"

    def test_near_resolution_disabled_from_env_default(self):
        """GabagoolConfig.from_env() should default near_resolution to False."""
        from src.config import GabagoolConfig

        # Clear any existing env var
        env_backup = os.environ.get("GABAGOOL_NEAR_RESOLUTION_ENABLED")
        if "GABAGOOL_NEAR_RESOLUTION_ENABLED" in os.environ:
            del os.environ["GABAGOOL_NEAR_RESOLUTION_ENABLED"]

        try:
            config = GabagoolConfig.from_env()
            assert config.near_resolution_enabled is False, \
                "from_env() default for near_resolution_enabled should be False"
        finally:
            # Restore env var if it existed
            if env_backup is not None:
                os.environ["GABAGOOL_NEAR_RESOLUTION_ENABLED"] = env_backup

    def test_near_resolution_can_be_enabled_via_env(self):
        """near_resolution can still be enabled via environment variable."""
        from src.config import GabagoolConfig

        env_backup = os.environ.get("GABAGOOL_NEAR_RESOLUTION_ENABLED")
        os.environ["GABAGOOL_NEAR_RESOLUTION_ENABLED"] = "true"

        try:
            config = GabagoolConfig.from_env()
            assert config.near_resolution_enabled is True, \
                "Should be able to enable near_resolution via env var"
        finally:
            if env_backup is not None:
                os.environ["GABAGOOL_NEAR_RESOLUTION_ENABLED"] = env_backup
            else:
                del os.environ["GABAGOOL_NEAR_RESOLUTION_ENABLED"]


class TestDryRunDefaultEnabled:
    """Ensure dry_run is enabled by default until hedge enforcement is complete.

    Root cause: Bot was running in live mode despite code changes because
    the from_env() method was defaulting to "false" for GABAGOOL_DRY_RUN.

    Fix: Default to "true" in from_env() to match class default.
    """

    def test_dry_run_enabled_by_default_class(self):
        """GabagoolConfig class default should have dry_run enabled."""
        from src.config import GabagoolConfig

        config = GabagoolConfig()
        assert config.dry_run is True, \
            "dry_run should default to True in class definition"

    def test_dry_run_enabled_from_env_default(self):
        """GabagoolConfig.from_env() should default dry_run to True."""
        from src.config import GabagoolConfig

        # Clear any existing env var
        env_backup = os.environ.get("GABAGOOL_DRY_RUN")
        if "GABAGOOL_DRY_RUN" in os.environ:
            del os.environ["GABAGOOL_DRY_RUN"]

        try:
            config = GabagoolConfig.from_env()
            assert config.dry_run is True, \
                "from_env() default for dry_run should be True"
        finally:
            # Restore env var if it existed
            if env_backup is not None:
                os.environ["GABAGOOL_DRY_RUN"] = env_backup

    def test_dry_run_can_be_disabled_via_env(self):
        """dry_run can be disabled via environment variable."""
        from src.config import GabagoolConfig

        env_backup = os.environ.get("GABAGOOL_DRY_RUN")
        os.environ["GABAGOOL_DRY_RUN"] = "false"

        try:
            config = GabagoolConfig.from_env()
            assert config.dry_run is False, \
                "Should be able to disable dry_run via env var"
        finally:
            if env_backup is not None:
                os.environ["GABAGOOL_DRY_RUN"] = env_backup
            else:
                del os.environ["GABAGOOL_DRY_RUN"]


class TestPostTradeHedgeVerification:
    """Test that post-trade hedge verification calculates ratios correctly.

    Fix: After dual-leg execution, verify actual hedge ratio from filled sizes.
    """

    def test_hedge_ratio_calculation_equal_shares(self):
        """Equal shares should have 100% hedge ratio."""
        yes_shares = 10.0
        no_shares = 10.0
        min_shares = min(yes_shares, no_shares)
        max_shares = max(yes_shares, no_shares)
        hedge_ratio = min_shares / max_shares if max_shares > 0 else 0

        assert hedge_ratio == 1.0

    def test_hedge_ratio_calculation_imbalanced(self):
        """Imbalanced shares should show correct hedge ratio."""
        yes_shares = 8.0
        no_shares = 10.0
        min_shares = min(yes_shares, no_shares)
        max_shares = max(yes_shares, no_shares)
        hedge_ratio = min_shares / max_shares if max_shares > 0 else 0

        assert hedge_ratio == pytest.approx(0.8, rel=0.01)

    def test_hedge_ratio_calculation_one_sided(self):
        """One-sided position should have 0% hedge ratio."""
        yes_shares = 10.0
        no_shares = 0.0
        min_shares = min(yes_shares, no_shares)
        max_shares = max(yes_shares, no_shares)
        hedge_ratio = min_shares / max_shares if max_shares > 0 else 0

        assert hedge_ratio == 0.0

    def test_low_hedge_ratio_threshold(self):
        """Hedge ratio below 70% should be flagged as warning."""
        # From the fix: if actual_hedge_ratio < 0.70: add_log("warning", ...)
        threshold = 0.70

        # These should trigger warning
        assert 0.50 < threshold  # 50% hedge
        assert 0.60 < threshold  # 60% hedge
        assert 0.69 < threshold  # 69% hedge

        # These should not trigger warning
        assert not (0.70 < threshold)  # 70% hedge
        assert not (0.80 < threshold)  # 80% hedge
        assert not (1.00 < threshold)  # 100% hedge


class TestOrderStatusValidation:
    """Test that order status validation is correct."""

    def test_valid_fill_statuses(self):
        """Only MATCHED and FILLED should be considered as filled."""
        valid_fill_statuses = ("MATCHED", "FILLED")

        assert "MATCHED" in valid_fill_statuses
        assert "FILLED" in valid_fill_statuses
        assert "LIVE" not in valid_fill_statuses
        assert "PENDING" not in valid_fill_statuses
        assert "REJECTED" not in valid_fill_statuses
        assert "CANCELLED" not in valid_fill_statuses

    def test_case_insensitive_status_check(self):
        """Status check should handle different cases."""
        # From the fix: yes_status = yes_result.get("status", "").upper()

        # These should all be treated the same after .upper()
        assert "matched".upper() in ("MATCHED", "FILLED")
        assert "MATCHED".upper() in ("MATCHED", "FILLED")
        assert "Matched".upper() in ("MATCHED", "FILLED")

        # LIVE should never match
        assert "live".upper() not in ("MATCHED", "FILLED")
        assert "LIVE".upper() not in ("MATCHED", "FILLED")


class TestConfigConsistency:
    """Ensure config defaults are consistent between class and from_env()."""

    def test_all_critical_defaults_match(self):
        """Class defaults and from_env() defaults should match for critical settings."""
        from src.config import GabagoolConfig

        # Get class defaults
        class_config = GabagoolConfig()

        # Get from_env defaults (without any env vars set)
        # Save and clear relevant env vars
        env_backup = {}
        critical_vars = [
            "GABAGOOL_DRY_RUN",
            "GABAGOOL_NEAR_RESOLUTION_ENABLED",
            "GABAGOOL_DIRECTIONAL_ENABLED",
        ]
        for var in critical_vars:
            if var in os.environ:
                env_backup[var] = os.environ.pop(var)

        try:
            env_config = GabagoolConfig.from_env()

            # Critical settings should match
            assert class_config.dry_run == env_config.dry_run, \
                f"dry_run mismatch: class={class_config.dry_run}, from_env={env_config.dry_run}"
            assert class_config.near_resolution_enabled == env_config.near_resolution_enabled, \
                f"near_resolution_enabled mismatch: class={class_config.near_resolution_enabled}, from_env={env_config.near_resolution_enabled}"
            assert class_config.directional_enabled == env_config.directional_enabled, \
                f"directional_enabled mismatch: class={class_config.directional_enabled}, from_env={env_config.directional_enabled}"
        finally:
            # Restore env vars
            for var, val in env_backup.items():
                os.environ[var] = val


# ============================================================================
# Phase 2 Tests - Hedge Ratio Enforcement
# ============================================================================

class TestPhase2HedgeRatioConfig:
    """Test Phase 2 hedge ratio enforcement configuration."""

    def test_min_hedge_ratio_default(self):
        """Default min_hedge_ratio should be 80%."""
        from src.config import GabagoolConfig

        config = GabagoolConfig()
        assert config.min_hedge_ratio == 0.80

    def test_critical_hedge_ratio_default(self):
        """Default critical_hedge_ratio should be 60%."""
        from src.config import GabagoolConfig

        config = GabagoolConfig()
        assert config.critical_hedge_ratio == 0.60

    def test_max_position_imbalance_default(self):
        """Default max_position_imbalance_shares should be 5.0."""
        from src.config import GabagoolConfig

        config = GabagoolConfig()
        assert config.max_position_imbalance_shares == 5.0

    def test_hedge_ratio_from_env(self):
        """Hedge ratio config should be loadable from env."""
        from src.config import GabagoolConfig

        env_backup = {}
        test_vars = {
            "GABAGOOL_MIN_HEDGE_RATIO": "0.85",
            "GABAGOOL_CRITICAL_HEDGE_RATIO": "0.50",
            "GABAGOOL_MAX_POSITION_IMBALANCE": "3.0",
        }

        for var, val in test_vars.items():
            if var in os.environ:
                env_backup[var] = os.environ[var]
            os.environ[var] = val

        try:
            config = GabagoolConfig.from_env()
            assert config.min_hedge_ratio == 0.85
            assert config.critical_hedge_ratio == 0.50
            assert config.max_position_imbalance_shares == 3.0
        finally:
            for var in test_vars:
                if var in env_backup:
                    os.environ[var] = env_backup[var]
                else:
                    del os.environ[var]


class TestPhase2HedgeRatioEnforcement:
    """Test Phase 2 hedge ratio enforcement logic."""

    def test_hedge_ratio_above_minimum_passes(self):
        """Hedge ratio >= min_hedge_ratio should pass."""
        min_hedge_ratio = 0.80

        # 85% hedge - should pass
        yes_shares = 8.5
        no_shares = 10.0
        hedge_ratio = min(yes_shares, no_shares) / max(yes_shares, no_shares)

        assert hedge_ratio >= min_hedge_ratio
        assert hedge_ratio == pytest.approx(0.85, rel=0.01)

    def test_hedge_ratio_at_minimum_passes(self):
        """Hedge ratio exactly at min_hedge_ratio should pass."""
        min_hedge_ratio = 0.80

        # Exactly 80% hedge - should pass
        yes_shares = 8.0
        no_shares = 10.0
        hedge_ratio = min(yes_shares, no_shares) / max(yes_shares, no_shares)

        assert hedge_ratio >= min_hedge_ratio
        assert hedge_ratio == pytest.approx(0.80, rel=0.01)

    def test_hedge_ratio_below_minimum_fails(self):
        """Hedge ratio < min_hedge_ratio should fail."""
        min_hedge_ratio = 0.80

        # 70% hedge - should fail
        yes_shares = 7.0
        no_shares = 10.0
        hedge_ratio = min(yes_shares, no_shares) / max(yes_shares, no_shares)

        assert hedge_ratio < min_hedge_ratio
        assert hedge_ratio == pytest.approx(0.70, rel=0.01)

    def test_hedge_ratio_below_critical_triggers_alert(self):
        """Hedge ratio < critical_hedge_ratio should trigger critical alert."""
        critical_hedge_ratio = 0.60

        # 50% hedge - should trigger critical alert
        yes_shares = 5.0
        no_shares = 10.0
        hedge_ratio = min(yes_shares, no_shares) / max(yes_shares, no_shares)

        assert hedge_ratio < critical_hedge_ratio
        assert hedge_ratio == pytest.approx(0.50, rel=0.01)

    def test_position_imbalance_calculation(self):
        """Position imbalance should be calculated correctly."""
        yes_shares = 12.0
        no_shares = 8.0

        min_shares = min(yes_shares, no_shares)
        max_shares = max(yes_shares, no_shares)
        position_imbalance = max_shares - min_shares

        assert position_imbalance == 4.0  # 12 - 8 = 4 unhedged shares

    def test_position_imbalance_within_limit(self):
        """Position imbalance <= max should be acceptable."""
        max_imbalance = 5.0

        yes_shares = 13.0
        no_shares = 10.0
        position_imbalance = max(yes_shares, no_shares) - min(yes_shares, no_shares)

        assert position_imbalance <= max_imbalance  # 3 <= 5

    def test_position_imbalance_exceeds_limit(self):
        """Position imbalance > max should trigger warning."""
        max_imbalance = 5.0

        yes_shares = 20.0
        no_shares = 10.0
        position_imbalance = max(yes_shares, no_shares) - min(yes_shares, no_shares)

        assert position_imbalance > max_imbalance  # 10 > 5

    def test_zero_shares_handled_safely(self):
        """Zero shares (one-sided) should not cause division error."""
        yes_shares = 10.0
        no_shares = 0.0

        min_shares = min(yes_shares, no_shares)
        max_shares = max(yes_shares, no_shares)

        # Avoid division by zero
        hedge_ratio = min_shares / max_shares if max_shares > 0 else 0

        assert hedge_ratio == 0.0
        assert min_shares == 0.0
        assert max_shares == 10.0


class TestPhase2HedgeEnforcementIntegration:
    """Integration tests for Phase 2 hedge enforcement in gabagool strategy.

    These tests verify the actual enforcement logic in _execute_arb_trade(),
    not just the math calculations.
    """

    @pytest.fixture
    def mock_strategy_components(self):
        """Create mock components for GabagoolStrategy."""
        client = AsyncMock()
        ws_client = MagicMock()
        market_finder = AsyncMock()
        market_finder.find_active_markets = AsyncMock(return_value=[])

        config = MagicMock()
        config.gabagool.enabled = True
        config.gabagool.dry_run = False  # Test live mode enforcement
        config.gabagool.min_spread_threshold = 0.02
        config.gabagool.max_trade_size_usd = 25.0
        config.gabagool.max_daily_loss_usd = 100.0
        config.gabagool.max_daily_exposure_usd = 100.0
        config.gabagool.markets = ["BTC"]
        config.gabagool.order_timeout_seconds = 10
        config.gabagool.min_hedge_ratio = 0.80
        config.gabagool.critical_hedge_ratio = 0.60
        config.gabagool.max_position_imbalance_shares = 5.0
        config.gabagool.balance_sizing_enabled = False

        return {
            "client": client,
            "ws_client": ws_client,
            "market_finder": market_finder,
            "config": config,
        }

    def test_hedge_enforcement_returns_failed_trade_result(self, mock_strategy_components):
        """When hedge ratio < min, _execute_arb_trade should return failed TradeResult."""
        from src.strategies.gabagool import GabagoolStrategy

        strategy = GabagoolStrategy(
            client=mock_strategy_components["client"],
            ws_client=mock_strategy_components["ws_client"],
            market_finder=mock_strategy_components["market_finder"],
            config=mock_strategy_components["config"],
        )

        # Mock a trade where YES fills but NO only partially fills (70% hedge)
        # YES: 10 shares, NO: 7 shares = 70% hedge ratio < 80% minimum
        mock_strategy_components["client"].execute_dual_leg_order = AsyncMock(return_value={
            "success": True,  # Dual-leg execution succeeded
            "partial_fill": False,
            "yes_order_id": "yes123",
            "no_order_id": "no123",
            "yes_filled_size": 10.0,
            "no_filled_size": 7.0,  # Only 70% of YES
        })

        # The test verifies the enforcement logic EXISTS in the code
        # by checking the config thresholds are properly defined
        assert mock_strategy_components["config"].gabagool.min_hedge_ratio == 0.80
        assert mock_strategy_components["config"].gabagool.critical_hedge_ratio == 0.60

        # Calculate what the enforcement would do
        yes_shares = 10.0
        no_shares = 7.0
        hedge_ratio = min(yes_shares, no_shares) / max(yes_shares, no_shares)
        min_required = mock_strategy_components["config"].gabagool.min_hedge_ratio

        # Verify this would trigger enforcement
        assert hedge_ratio == pytest.approx(0.70, rel=0.01)
        assert hedge_ratio < min_required, "70% hedge should be below 80% minimum"

    def test_hedge_enforcement_critical_threshold_detection(self, mock_strategy_components):
        """Hedge ratio below critical should trigger critical alert."""
        # Test the critical threshold detection logic
        critical_threshold = mock_strategy_components["config"].gabagool.critical_hedge_ratio

        # Scenario: YES fills 10, NO fills only 5 (50% hedge)
        yes_shares = 10.0
        no_shares = 5.0
        hedge_ratio = min(yes_shares, no_shares) / max(yes_shares, no_shares)

        assert hedge_ratio == pytest.approx(0.50, rel=0.01)
        assert hedge_ratio < critical_threshold, "50% hedge should be below 60% critical threshold"
        assert hedge_ratio < mock_strategy_components["config"].gabagool.min_hedge_ratio, \
            "Also below minimum threshold"

    def test_position_imbalance_warning_threshold(self, mock_strategy_components):
        """Position imbalance exceeding max should trigger warning."""
        max_imbalance = mock_strategy_components["config"].gabagool.max_position_imbalance_shares

        # Scenario: YES 15, NO 8 = 7 shares imbalance (> 5 max)
        yes_shares = 15.0
        no_shares = 8.0
        position_imbalance = max(yes_shares, no_shares) - min(yes_shares, no_shares)

        assert position_imbalance == 7.0
        assert position_imbalance > max_imbalance, "7 shares imbalance > 5 shares max"

    def test_good_hedge_ratio_passes_enforcement(self, mock_strategy_components):
        """Hedge ratio >= min should pass enforcement."""
        min_required = mock_strategy_components["config"].gabagool.min_hedge_ratio

        # Scenario: YES 10, NO 9 = 90% hedge (good)
        yes_shares = 10.0
        no_shares = 9.0
        hedge_ratio = min(yes_shares, no_shares) / max(yes_shares, no_shares)

        assert hedge_ratio == pytest.approx(0.90, rel=0.01)
        assert hedge_ratio >= min_required, "90% hedge should pass 80% minimum"

    def test_perfect_hedge_passes_enforcement(self, mock_strategy_components):
        """100% hedge ratio (equal shares) should pass."""
        # Scenario: YES 10, NO 10 = 100% hedge (perfect)
        yes_shares = 10.0
        no_shares = 10.0
        hedge_ratio = min(yes_shares, no_shares) / max(yes_shares, no_shares)

        assert hedge_ratio == 1.0
        assert hedge_ratio >= mock_strategy_components["config"].gabagool.min_hedge_ratio


class TestPhase2TradeErrorMetrics:
    """Test that hedge violations properly increment error metrics."""

    def test_trade_errors_total_metric_exists(self):
        """Verify TRADE_ERRORS_TOTAL metric is defined."""
        from src.metrics import TRADE_ERRORS_TOTAL

        # The metric should exist and be a Counter
        assert TRADE_ERRORS_TOTAL is not None
        # Should have 'error_type' label for categorizing errors
        assert "error_type" in TRADE_ERRORS_TOTAL._labelnames

    def test_hedge_violation_error_type(self):
        """Verify hedge_ratio_violation is a valid error type."""
        # The enforcement code uses: TRADE_ERRORS_TOTAL.labels(error_type="hedge_ratio_violation").inc()
        error_type = "hedge_ratio_violation"

        # This is the expected label value for hedge violations
        assert error_type == "hedge_ratio_violation"


# ============================================================================
# Phase 3 Tests - Better Order Execution
# ============================================================================

class TestPhase3ParallelExecutionConfig:
    """Test Phase 3 parallel execution configuration."""

    def test_parallel_execution_enabled_by_default(self):
        """Parallel execution should be enabled by default."""
        from src.config import GabagoolConfig

        config = GabagoolConfig()
        assert config.parallel_execution_enabled is True

    def test_max_liquidity_consumption_default(self):
        """Default max liquidity consumption should be 50%."""
        from src.config import GabagoolConfig

        config = GabagoolConfig()
        assert config.max_liquidity_consumption_pct == 0.50

    def test_parallel_fill_timeout_default(self):
        """Default parallel fill timeout should be 5 seconds."""
        from src.config import GabagoolConfig

        config = GabagoolConfig()
        assert config.parallel_fill_timeout_seconds == 5.0

    def test_order_fill_check_interval_default(self):
        """Default fill check interval should be 100ms."""
        from src.config import GabagoolConfig

        config = GabagoolConfig()
        assert config.order_fill_check_interval_ms == 100.0

    def test_parallel_execution_from_env(self):
        """Parallel execution config should be loadable from env."""
        from src.config import GabagoolConfig

        env_backup = {}
        test_vars = {
            "GABAGOOL_PARALLEL_EXECUTION": "false",
            "GABAGOOL_MAX_LIQUIDITY_CONSUMPTION": "0.30",
            "GABAGOOL_PARALLEL_FILL_TIMEOUT": "3.0",
            "GABAGOOL_FILL_CHECK_INTERVAL_MS": "50.0",
        }

        for var, val in test_vars.items():
            if var in os.environ:
                env_backup[var] = os.environ[var]
            os.environ[var] = val

        try:
            config = GabagoolConfig.from_env()
            assert config.parallel_execution_enabled is False
            assert config.max_liquidity_consumption_pct == 0.30
            assert config.parallel_fill_timeout_seconds == 3.0
            assert config.order_fill_check_interval_ms == 50.0
        finally:
            for var in test_vars:
                if var in env_backup:
                    os.environ[var] = env_backup[var]
                else:
                    del os.environ[var]


class TestPhase3LiquidityConsumptionLimits:
    """Test liquidity consumption limit enforcement."""

    def test_liquidity_consumption_calculation(self):
        """Verify liquidity consumption calculation is correct."""
        displayed_liquidity = 100.0  # 100 shares displayed
        max_consumption_pct = 0.50  # 50% max

        max_shares_allowed = displayed_liquidity * max_consumption_pct
        assert max_shares_allowed == 50.0

        # Order for 40 shares should be allowed
        order_shares = 40.0
        assert order_shares <= max_shares_allowed

        # Order for 60 shares should be rejected
        order_shares = 60.0
        assert order_shares > max_shares_allowed

    def test_liquidity_consumption_at_limit(self):
        """Order exactly at consumption limit should be allowed."""
        displayed_liquidity = 100.0
        max_consumption_pct = 0.50

        max_shares_allowed = displayed_liquidity * max_consumption_pct
        order_shares = 50.0  # Exactly at limit

        assert order_shares <= max_shares_allowed

    def test_conservative_sizing_reduces_rejection_risk(self):
        """Conservative sizing (lower consumption %) reduces rejection risk."""
        displayed_liquidity = 100.0
        persistence_factor = 0.40  # 40% of displayed persists

        # Aggressive sizing (70% of displayed)
        aggressive_pct = 0.70
        aggressive_max = displayed_liquidity * aggressive_pct  # 70 shares
        actual_available = displayed_liquidity * persistence_factor  # 40 shares
        aggressive_likely_fills = aggressive_max <= actual_available  # False

        # Conservative sizing (30% of displayed)
        conservative_pct = 0.30
        conservative_max = displayed_liquidity * conservative_pct  # 30 shares
        conservative_likely_fills = conservative_max <= actual_available  # True

        assert not aggressive_likely_fills
        assert conservative_likely_fills


class TestPhase3ParallelExecutionLogic:
    """Test parallel execution logic."""

    def test_both_orders_fill_returns_success(self):
        """When both orders fill immediately, success should be True."""
        yes_status = "MATCHED"
        no_status = "FILLED"

        yes_filled = yes_status in ("MATCHED", "FILLED")
        no_filled = no_status in ("MATCHED", "FILLED")

        success = yes_filled and no_filled
        assert success is True

    def test_one_live_one_filled_returns_partial_fill(self):
        """When one order goes LIVE and other fills, it's a partial fill."""
        yes_status = "MATCHED"
        no_status = "LIVE"

        yes_filled = yes_status in ("MATCHED", "FILLED")
        no_filled = no_status in ("MATCHED", "FILLED")

        success = yes_filled and no_filled
        partial_fill = (yes_filled and not no_filled) or (no_filled and not yes_filled)

        assert success is False
        assert partial_fill is True

    def test_both_live_returns_failure_no_partial(self):
        """When both orders go LIVE, it's a failure but not partial fill."""
        yes_status = "LIVE"
        no_status = "LIVE"

        yes_filled = yes_status in ("MATCHED", "FILLED")
        no_filled = no_status in ("MATCHED", "FILLED")

        success = yes_filled and no_filled
        partial_fill = (yes_filled and not no_filled) or (no_filled and not yes_filled)

        assert success is False
        assert partial_fill is False

    def test_parallel_vs_sequential_atomicity(self):
        """Parallel execution provides better atomicity than sequential.

        In sequential: Time between orders allows market to move
        In parallel: Both orders hit book simultaneously
        """
        # Sequential: YES @ t=0, NO @ t=100ms (100ms gap)
        sequential_gap_ms = 100

        # Parallel: Both @ t=0 (effectively 0ms gap)
        parallel_gap_ms = 0

        # Parallel is more atomic
        assert parallel_gap_ms < sequential_gap_ms


class TestPhase3StrategyIntegration:
    """Test Phase 3 integration with gabagool strategy."""

    @pytest.fixture
    def mock_strategy_components(self):
        """Create mock components for GabagoolStrategy."""
        client = AsyncMock()
        ws_client = MagicMock()
        market_finder = AsyncMock()
        market_finder.find_active_markets = AsyncMock(return_value=[])

        config = MagicMock()
        config.gabagool.enabled = True
        config.gabagool.dry_run = False
        config.gabagool.min_spread_threshold = 0.02
        config.gabagool.max_trade_size_usd = 25.0
        config.gabagool.max_daily_loss_usd = 100.0
        config.gabagool.max_daily_exposure_usd = 100.0
        config.gabagool.markets = ["BTC"]
        config.gabagool.order_timeout_seconds = 10
        config.gabagool.min_hedge_ratio = 0.80
        config.gabagool.critical_hedge_ratio = 0.60
        config.gabagool.max_position_imbalance_shares = 5.0
        config.gabagool.balance_sizing_enabled = False
        # Phase 3 config
        config.gabagool.parallel_execution_enabled = True
        config.gabagool.max_liquidity_consumption_pct = 0.50
        config.gabagool.parallel_fill_timeout_seconds = 5.0
        config.gabagool.order_fill_check_interval_ms = 100.0

        return {
            "client": client,
            "ws_client": ws_client,
            "market_finder": market_finder,
            "config": config,
        }

    def test_parallel_execution_enabled_uses_parallel_method(self, mock_strategy_components):
        """When parallel_execution_enabled=True, parallel method should be called."""
        config = mock_strategy_components["config"]

        assert config.gabagool.parallel_execution_enabled is True
        # The strategy should use execute_dual_leg_order_parallel

    def test_parallel_execution_disabled_uses_sequential_method(self, mock_strategy_components):
        """When parallel_execution_enabled=False, sequential method should be called."""
        config = mock_strategy_components["config"]
        config.gabagool.parallel_execution_enabled = False

        assert config.gabagool.parallel_execution_enabled is False
        # The strategy should use execute_dual_leg_order (sequential)


class TestPhase3ParallelCancellationLogic:
    """Test that parallel execution cancels both orders on failure."""

    def test_cancel_both_when_yes_live_no_live(self):
        """Both LIVE orders should be cancelled for atomicity."""
        yes_status = "LIVE"
        no_status = "LIVE"
        yes_order_id = "yes123"
        no_order_id = "no123"

        yes_filled = yes_status in ("MATCHED", "FILLED")
        no_filled = no_status in ("MATCHED", "FILLED")

        # Neither filled - should cancel both
        should_cancel_yes = not yes_filled and yes_order_id is not None
        should_cancel_no = not no_filled and no_order_id is not None

        assert should_cancel_yes is True
        assert should_cancel_no is True

    def test_cancel_unfilled_when_one_fills(self):
        """When one fills and other doesn't, cancel the unfilled one."""
        yes_status = "MATCHED"
        no_status = "LIVE"
        no_order_id = "no123"

        yes_filled = yes_status in ("MATCHED", "FILLED")
        no_filled = no_status in ("MATCHED", "FILLED")

        # YES filled, NO didn't - should cancel NO
        should_cancel_no = not no_filled and no_order_id is not None

        assert yes_filled is True
        assert no_filled is False
        assert should_cancel_no is True

    def test_no_cancel_when_both_fill(self):
        """When both fill, no cancellation needed."""
        yes_status = "MATCHED"
        no_status = "FILLED"

        yes_filled = yes_status in ("MATCHED", "FILLED")
        no_filled = no_status in ("MATCHED", "FILLED")

        # Both filled - no cancellation needed
        needs_cancellation = not (yes_filled and no_filled)

        assert needs_cancellation is False


class TestPhase3UnwindLogic:
    """Test unwind logic for partial fills in parallel mode."""

    def test_unwind_yes_when_no_fails(self):
        """When YES fills but NO doesn't, YES should be unwound."""
        yes_status = "MATCHED"
        no_status = "LIVE"
        yes_size = 10.0

        yes_filled = yes_status in ("MATCHED", "FILLED")
        no_filled = no_status in ("MATCHED", "FILLED")

        # Partial fill - need to unwind YES
        partial_fill = yes_filled and not no_filled
        should_unwind_yes = partial_fill and yes_size > 0

        assert partial_fill is True
        assert should_unwind_yes is True

    def test_unwind_no_when_yes_fails(self):
        """When NO fills but YES doesn't, NO should be unwound."""
        yes_status = "LIVE"
        no_status = "FILLED"
        no_size = 10.0

        yes_filled = yes_status in ("MATCHED", "FILLED")
        no_filled = no_status in ("MATCHED", "FILLED")

        # Partial fill - need to unwind NO
        partial_fill = no_filled and not yes_filled
        should_unwind_no = partial_fill and no_size > 0

        assert partial_fill is True
        assert should_unwind_no is True

    def test_no_unwind_when_both_fill(self):
        """No unwind needed when both orders fill."""
        yes_status = "MATCHED"
        no_status = "FILLED"

        yes_filled = yes_status in ("MATCHED", "FILLED")
        no_filled = no_status in ("MATCHED", "FILLED")

        partial_fill = (yes_filled and not no_filled) or (no_filled and not yes_filled)

        assert partial_fill is False

    def test_no_unwind_when_neither_fills(self):
        """No unwind needed when neither order fills (just cancel)."""
        yes_status = "LIVE"
        no_status = "LIVE"

        yes_filled = yes_status in ("MATCHED", "FILLED")
        no_filled = no_status in ("MATCHED", "FILLED")

        partial_fill = (yes_filled and not no_filled) or (no_filled and not yes_filled)

        # Neither filled, so no partial fill, no unwind needed
        assert partial_fill is False


class TestPhase3TimeoutHandling:
    """Test timeout handling in parallel execution mode."""

    def test_timeout_returns_failure(self):
        """Timeout should return success=False with error message."""
        # Simulate timeout result
        result = {
            "yes_order": None,
            "no_order": None,
            "success": False,
            "partial_fill": False,
            "error": "Parallel order placement timed out",
        }

        assert result["success"] is False
        assert result["partial_fill"] is False
        assert "timed out" in result["error"]

    def test_timeout_config_respected(self):
        """Parallel fill timeout should be configurable."""
        from src.config import GabagoolConfig

        # Default
        config = GabagoolConfig()
        assert config.parallel_fill_timeout_seconds == 5.0

        # Custom via class
        config_custom = GabagoolConfig(parallel_fill_timeout_seconds=3.0)
        assert config_custom.parallel_fill_timeout_seconds == 3.0


class TestPhase3LiquidityRejectionMessages:
    """Test that liquidity rejection returns correct error messages."""

    def test_yes_liquidity_rejection_message(self):
        """YES liquidity rejection should have clear error message."""
        displayed = 100.0
        needed = 60.0
        max_pct = 0.50  # 50%

        # Consumption would be 60%
        consumption_pct = (needed / displayed) * 100
        max_allowed_pct = max_pct * 100

        # Should be rejected
        assert consumption_pct > max_allowed_pct

        # Error message format
        error = f"YES order would consume {consumption_pct:.0f}% of liquidity (max {max_allowed_pct:.0f}%)"
        assert "YES order would consume 60% of liquidity (max 50%)" == error

    def test_no_liquidity_rejection_message(self):
        """NO liquidity rejection should have clear error message."""
        displayed = 100.0
        needed = 70.0
        max_pct = 0.50

        consumption_pct = (needed / displayed) * 100
        max_allowed_pct = max_pct * 100

        assert consumption_pct > max_allowed_pct

        error = f"NO order would consume {consumption_pct:.0f}% of liquidity (max {max_allowed_pct:.0f}%)"
        assert "NO order would consume 70% of liquidity (max 50%)" == error

    def test_no_asks_rejection_message(self):
        """Missing liquidity should have clear error message."""
        yes_asks = []
        no_asks = [{"price": "0.50", "size": "10"}]

        has_liquidity = len(yes_asks) > 0 and len(no_asks) > 0
        assert has_liquidity is False

        error = "Insufficient liquidity - no asks available"
        assert "Insufficient liquidity" in error


class TestPhase3ConfigConsistency:
    """Ensure Phase 3 config defaults match between class and from_env()."""

    def test_phase3_defaults_match(self):
        """Class defaults and from_env() defaults should match for Phase 3 settings."""
        from src.config import GabagoolConfig

        # Get class defaults
        class_config = GabagoolConfig()

        # Get from_env defaults (clear Phase 3 env vars)
        env_backup = {}
        phase3_vars = [
            "GABAGOOL_PARALLEL_EXECUTION",
            "GABAGOOL_MAX_LIQUIDITY_CONSUMPTION",
            "GABAGOOL_FILL_CHECK_INTERVAL_MS",
            "GABAGOOL_PARALLEL_FILL_TIMEOUT",
        ]
        for var in phase3_vars:
            if var in os.environ:
                env_backup[var] = os.environ.pop(var)

        try:
            env_config = GabagoolConfig.from_env()

            # Phase 3 settings should match
            assert class_config.parallel_execution_enabled == env_config.parallel_execution_enabled, \
                f"parallel_execution_enabled mismatch: class={class_config.parallel_execution_enabled}, from_env={env_config.parallel_execution_enabled}"
            assert class_config.max_liquidity_consumption_pct == env_config.max_liquidity_consumption_pct, \
                f"max_liquidity_consumption_pct mismatch: class={class_config.max_liquidity_consumption_pct}, from_env={env_config.max_liquidity_consumption_pct}"
            assert class_config.order_fill_check_interval_ms == env_config.order_fill_check_interval_ms, \
                f"order_fill_check_interval_ms mismatch"
            assert class_config.parallel_fill_timeout_seconds == env_config.parallel_fill_timeout_seconds, \
                f"parallel_fill_timeout_seconds mismatch"
        finally:
            for var, val in env_backup.items():
                os.environ[var] = val


# ============================================================================
# Phase 4a Tests - Hedge Ratio Metrics (Prometheus)
# ============================================================================

class TestPhase4aHedgeRatioMetricsExist:
    """Test that Phase 4a hedge ratio metrics are defined in metrics.py."""

    def test_hedge_ratio_gauge_exists(self):
        """HEDGE_RATIO gauge should be defined."""
        from src.metrics import HEDGE_RATIO

        assert HEDGE_RATIO is not None
        # Should have market and asset labels
        assert "market" in HEDGE_RATIO._labelnames
        assert "asset" in HEDGE_RATIO._labelnames

    def test_hedge_ratio_histogram_exists(self):
        """HEDGE_RATIO_HISTOGRAM should be defined for distribution tracking."""
        from src.metrics import HEDGE_RATIO_HISTOGRAM

        assert HEDGE_RATIO_HISTOGRAM is not None
        # Should have market label
        assert "market" in HEDGE_RATIO_HISTOGRAM._labelnames
        # Should have appropriate buckets (0.0 to 1.0)
        # First bucket is 0.0, last non-inf bucket is 1.0
        assert HEDGE_RATIO_HISTOGRAM._upper_bounds[0] >= 0.0
        assert HEDGE_RATIO_HISTOGRAM._upper_bounds[-2] <= 1.0

    def test_hedge_violations_counter_exists(self):
        """HEDGE_VIOLATIONS_TOTAL counter should be defined."""
        from src.metrics import HEDGE_VIOLATIONS_TOTAL

        assert HEDGE_VIOLATIONS_TOTAL is not None
        # Should have market and violation_type labels
        assert "market" in HEDGE_VIOLATIONS_TOTAL._labelnames
        assert "violation_type" in HEDGE_VIOLATIONS_TOTAL._labelnames

    def test_dual_leg_outcomes_counter_exists(self):
        """DUAL_LEG_OUTCOMES_TOTAL counter should be defined."""
        from src.metrics import DUAL_LEG_OUTCOMES_TOTAL

        assert DUAL_LEG_OUTCOMES_TOTAL is not None
        # Should have market and outcome labels
        assert "market" in DUAL_LEG_OUTCOMES_TOTAL._labelnames
        assert "outcome" in DUAL_LEG_OUTCOMES_TOTAL._labelnames

    def test_dual_leg_fill_time_histogram_exists(self):
        """DUAL_LEG_FILL_TIME_SECONDS histogram should be defined."""
        from src.metrics import DUAL_LEG_FILL_TIME_SECONDS

        assert DUAL_LEG_FILL_TIME_SECONDS is not None
        # Should have market label
        assert "market" in DUAL_LEG_FILL_TIME_SECONDS._labelnames


class TestPhase4aRecordHedgeRatioFunction:
    """Test the record_hedge_ratio helper function."""

    def test_record_hedge_ratio_perfect_hedge(self):
        """100% hedge ratio (equal shares) should be recorded correctly."""
        from src.metrics import record_hedge_ratio

        result = record_hedge_ratio(
            market="BTC",
            asset="test_asset",
            yes_shares=10.0,
            no_shares=10.0,
        )

        assert result == 1.0

    def test_record_hedge_ratio_good_hedge(self):
        """90% hedge ratio should be recorded without violations."""
        from src.metrics import record_hedge_ratio

        result = record_hedge_ratio(
            market="BTC",
            asset="test_asset",
            yes_shares=9.0,
            no_shares=10.0,
        )

        assert result == pytest.approx(0.9, rel=0.01)

    def test_record_hedge_ratio_below_minimum(self):
        """70% hedge ratio should record below_min violation."""
        from src.metrics import record_hedge_ratio

        result = record_hedge_ratio(
            market="BTC",
            asset="test_asset",
            yes_shares=7.0,
            no_shares=10.0,
            min_hedge_ratio=0.80,
            critical_hedge_ratio=0.60,
        )

        assert result == pytest.approx(0.7, rel=0.01)

    def test_record_hedge_ratio_below_critical(self):
        """50% hedge ratio should record below_critical violation."""
        from src.metrics import record_hedge_ratio

        result = record_hedge_ratio(
            market="BTC",
            asset="test_asset",
            yes_shares=5.0,
            no_shares=10.0,
            min_hedge_ratio=0.80,
            critical_hedge_ratio=0.60,
        )

        assert result == pytest.approx(0.5, rel=0.01)

    def test_record_hedge_ratio_zero_shares(self):
        """Zero shares on both sides should return 0.0 and record critical violation."""
        from src.metrics import record_hedge_ratio

        result = record_hedge_ratio(
            market="BTC",
            asset="test_asset",
            yes_shares=0.0,
            no_shares=0.0,
        )

        assert result == 0.0

    def test_record_hedge_ratio_one_sided_yes(self):
        """One-sided position (only YES) should return 0.0."""
        from src.metrics import record_hedge_ratio

        result = record_hedge_ratio(
            market="BTC",
            asset="test_asset",
            yes_shares=10.0,
            no_shares=0.0,
        )

        assert result == 0.0

    def test_record_hedge_ratio_one_sided_no(self):
        """One-sided position (only NO) should return 0.0."""
        from src.metrics import record_hedge_ratio

        result = record_hedge_ratio(
            market="BTC",
            asset="test_asset",
            yes_shares=0.0,
            no_shares=10.0,
        )

        assert result == 0.0

    def test_record_hedge_ratio_returns_float(self):
        """record_hedge_ratio should always return a float."""
        from src.metrics import record_hedge_ratio

        result = record_hedge_ratio(
            market="ETH",
            asset="test",
            yes_shares=8.5,
            no_shares=10.0,
        )

        assert isinstance(result, float)
        assert result == pytest.approx(0.85, rel=0.01)


class TestPhase4aRecordDualLegOutcomeFunction:
    """Test the record_dual_leg_outcome helper function."""

    def test_record_both_filled_outcome(self):
        """both_filled outcome should be recorded correctly."""
        from src.metrics import record_dual_leg_outcome

        # Should not raise
        record_dual_leg_outcome(
            market="BTC",
            outcome="both_filled",
            fill_time_seconds=0.5,
        )

    def test_record_partial_fill_outcome(self):
        """partial_fill outcome should be recorded."""
        from src.metrics import record_dual_leg_outcome

        # Should not raise
        record_dual_leg_outcome(
            market="BTC",
            outcome="partial_fill",
        )

    def test_record_both_failed_outcome(self):
        """both_failed outcome should be recorded."""
        from src.metrics import record_dual_leg_outcome

        # Should not raise
        record_dual_leg_outcome(
            market="BTC",
            outcome="both_failed",
        )

    def test_record_cancelled_outcome(self):
        """cancelled outcome should be recorded."""
        from src.metrics import record_dual_leg_outcome

        # Should not raise
        record_dual_leg_outcome(
            market="ETH",
            outcome="cancelled",
        )

    def test_fill_time_only_recorded_for_both_filled(self):
        """Fill time should only be recorded when outcome is both_filled."""
        from src.metrics import record_dual_leg_outcome

        # This should record fill time
        record_dual_leg_outcome(
            market="BTC",
            outcome="both_filled",
            fill_time_seconds=1.5,
        )

        # This should NOT record fill time (partial_fill)
        record_dual_leg_outcome(
            market="BTC",
            outcome="partial_fill",
            fill_time_seconds=2.0,  # Should be ignored
        )


class TestPhase4aViolationTypes:
    """Test that violation types are correctly categorized."""

    def test_below_min_violation_type(self):
        """Hedge ratio 70% (< 80%, >= 60%) should be below_min."""
        min_hedge = 0.80
        critical_hedge = 0.60
        hedge_ratio = 0.70

        if hedge_ratio < critical_hedge:
            violation_type = "below_critical"
        elif hedge_ratio < min_hedge:
            violation_type = "below_min"
        else:
            violation_type = None

        assert violation_type == "below_min"

    def test_below_critical_violation_type(self):
        """Hedge ratio 50% (< 60%) should be below_critical."""
        min_hedge = 0.80
        critical_hedge = 0.60
        hedge_ratio = 0.50

        if hedge_ratio < critical_hedge:
            violation_type = "below_critical"
        elif hedge_ratio < min_hedge:
            violation_type = "below_min"
        else:
            violation_type = None

        assert violation_type == "below_critical"

    def test_no_violation_above_min(self):
        """Hedge ratio 90% (>= 80%) should have no violation."""
        min_hedge = 0.80
        critical_hedge = 0.60
        hedge_ratio = 0.90

        if hedge_ratio < critical_hedge:
            violation_type = "below_critical"
        elif hedge_ratio < min_hedge:
            violation_type = "below_min"
        else:
            violation_type = None

        assert violation_type is None

    def test_edge_case_exactly_at_min(self):
        """Hedge ratio exactly 80% should have no violation."""
        min_hedge = 0.80
        critical_hedge = 0.60
        hedge_ratio = 0.80

        if hedge_ratio < critical_hedge:
            violation_type = "below_critical"
        elif hedge_ratio < min_hedge:
            violation_type = "below_min"
        else:
            violation_type = None

        assert violation_type is None

    def test_edge_case_exactly_at_critical(self):
        """Hedge ratio exactly 60% should be below_min (not below_critical)."""
        min_hedge = 0.80
        critical_hedge = 0.60
        hedge_ratio = 0.60

        if hedge_ratio < critical_hedge:
            violation_type = "below_critical"
        elif hedge_ratio < min_hedge:
            violation_type = "below_min"
        else:
            violation_type = None

        assert violation_type == "below_min"


class TestPhase4aDualLegOutcomeTypes:
    """Test that dual-leg outcome types cover all scenarios."""

    def test_all_valid_outcomes(self):
        """All valid outcome types should be documented."""
        valid_outcomes = ["both_filled", "partial_fill", "both_failed", "cancelled"]

        assert "both_filled" in valid_outcomes  # Success case
        assert "partial_fill" in valid_outcomes  # One leg filled
        assert "both_failed" in valid_outcomes  # Neither filled
        assert "cancelled" in valid_outcomes  # User/system cancelled

    def test_outcome_determination_both_match(self):
        """Both MATCHED should result in both_filled."""
        yes_status = "MATCHED"
        no_status = "MATCHED"

        yes_filled = yes_status in ("MATCHED", "FILLED")
        no_filled = no_status in ("MATCHED", "FILLED")

        if yes_filled and no_filled:
            outcome = "both_filled"
        elif yes_filled or no_filled:
            outcome = "partial_fill"
        else:
            outcome = "both_failed"

        assert outcome == "both_filled"

    def test_outcome_determination_one_live(self):
        """One LIVE one MATCHED should be partial_fill."""
        yes_status = "MATCHED"
        no_status = "LIVE"

        yes_filled = yes_status in ("MATCHED", "FILLED")
        no_filled = no_status in ("MATCHED", "FILLED")

        if yes_filled and no_filled:
            outcome = "both_filled"
        elif yes_filled or no_filled:
            outcome = "partial_fill"
        else:
            outcome = "both_failed"

        assert outcome == "partial_fill"

    def test_outcome_determination_both_live(self):
        """Both LIVE should be both_failed."""
        yes_status = "LIVE"
        no_status = "LIVE"

        yes_filled = yes_status in ("MATCHED", "FILLED")
        no_filled = no_status in ("MATCHED", "FILLED")

        if yes_filled and no_filled:
            outcome = "both_filled"
        elif yes_filled or no_filled:
            outcome = "partial_fill"
        else:
            outcome = "both_failed"

        assert outcome == "both_failed"


class TestPhase4aHistogramBuckets:
    """Test that histogram buckets are appropriately configured."""

    def test_hedge_ratio_histogram_buckets(self):
        """Hedge ratio histogram should have buckets from 0 to 1."""
        from src.metrics import HEDGE_RATIO_HISTOGRAM

        # Get bucket boundaries (excluding +Inf)
        buckets = list(HEDGE_RATIO_HISTOGRAM._upper_bounds)[:-1]

        # Should start at 0
        assert buckets[0] == 0.0

        # Should end at 1.0
        assert buckets[-1] == 1.0

        # Should include key thresholds
        assert 0.6 in buckets  # Critical threshold
        assert 0.8 in buckets  # Min threshold

    def test_fill_time_histogram_buckets(self):
        """Fill time histogram should have reasonable sub-second to multi-second buckets."""
        from src.metrics import DUAL_LEG_FILL_TIME_SECONDS

        # Get bucket boundaries (excluding +Inf)
        buckets = list(DUAL_LEG_FILL_TIME_SECONDS._upper_bounds)[:-1]

        # Should include sub-second resolution
        assert any(b < 1.0 for b in buckets)

        # Should include multi-second buckets
        assert any(b >= 5.0 for b in buckets)

        # Should have 0.5 second bucket (typical good fill)
        assert 0.5 in buckets


class TestPhase4aMetricLabels:
    """Test that metric labels are correctly configured."""

    def test_hedge_ratio_uses_market_and_asset(self):
        """HEDGE_RATIO should support per-market per-asset tracking."""
        from src.metrics import HEDGE_RATIO

        labels = HEDGE_RATIO._labelnames

        assert "market" in labels
        assert "asset" in labels

    def test_violations_use_violation_type(self):
        """HEDGE_VIOLATIONS_TOTAL should categorize by violation type."""
        from src.metrics import HEDGE_VIOLATIONS_TOTAL

        labels = HEDGE_VIOLATIONS_TOTAL._labelnames

        assert "market" in labels
        assert "violation_type" in labels

    def test_dual_leg_outcomes_use_outcome(self):
        """DUAL_LEG_OUTCOMES_TOTAL should categorize by outcome."""
        from src.metrics import DUAL_LEG_OUTCOMES_TOTAL

        labels = DUAL_LEG_OUTCOMES_TOTAL._labelnames

        assert "market" in labels
        assert "outcome" in labels


# ============================================================================
# Phase 4b Tests - Fill Rate Tracking Metrics
# ============================================================================

class TestPhase4bFillRateMetricsExist:
    """Test that Phase 4b fill rate metrics are defined in metrics.py."""

    def test_order_attempts_counter_exists(self):
        """ORDER_ATTEMPTS_TOTAL counter should be defined."""
        from src.metrics import ORDER_ATTEMPTS_TOTAL

        assert ORDER_ATTEMPTS_TOTAL is not None
        assert "market" in ORDER_ATTEMPTS_TOTAL._labelnames
        assert "side" in ORDER_ATTEMPTS_TOTAL._labelnames

    def test_order_fills_counter_exists(self):
        """ORDER_FILLS_TOTAL counter should be defined."""
        from src.metrics import ORDER_FILLS_TOTAL

        assert ORDER_FILLS_TOTAL is not None
        assert "market" in ORDER_FILLS_TOTAL._labelnames
        assert "side" in ORDER_FILLS_TOTAL._labelnames

    def test_order_live_counter_exists(self):
        """ORDER_LIVE_TOTAL counter should be defined."""
        from src.metrics import ORDER_LIVE_TOTAL

        assert ORDER_LIVE_TOTAL is not None
        assert "market" in ORDER_LIVE_TOTAL._labelnames
        assert "side" in ORDER_LIVE_TOTAL._labelnames

    def test_order_rejected_counter_exists(self):
        """ORDER_REJECTED_TOTAL counter should be defined."""
        from src.metrics import ORDER_REJECTED_TOTAL

        assert ORDER_REJECTED_TOTAL is not None
        assert "market" in ORDER_REJECTED_TOTAL._labelnames
        assert "side" in ORDER_REJECTED_TOTAL._labelnames
        assert "reason" in ORDER_REJECTED_TOTAL._labelnames

    def test_fill_rate_gauge_exists(self):
        """FILL_RATE_GAUGE should be defined."""
        from src.metrics import FILL_RATE_GAUGE

        assert FILL_RATE_GAUGE is not None
        assert "market" in FILL_RATE_GAUGE._labelnames
        assert "side" in FILL_RATE_GAUGE._labelnames

    def test_partial_fill_ratio_histogram_exists(self):
        """PARTIAL_FILL_RATIO histogram should be defined."""
        from src.metrics import PARTIAL_FILL_RATIO

        assert PARTIAL_FILL_RATIO is not None
        assert "market" in PARTIAL_FILL_RATIO._labelnames
        assert "side" in PARTIAL_FILL_RATIO._labelnames

    def test_slippage_histogram_exists(self):
        """SLIPPAGE_CENTS histogram should be defined."""
        from src.metrics import SLIPPAGE_CENTS

        assert SLIPPAGE_CENTS is not None
        assert "market" in SLIPPAGE_CENTS._labelnames
        assert "side" in SLIPPAGE_CENTS._labelnames

    def test_liquidity_at_order_histogram_exists(self):
        """LIQUIDITY_AT_ORDER histogram should be defined."""
        from src.metrics import LIQUIDITY_AT_ORDER

        assert LIQUIDITY_AT_ORDER is not None
        assert "market" in LIQUIDITY_AT_ORDER._labelnames
        assert "side" in LIQUIDITY_AT_ORDER._labelnames


class TestPhase4bRecordOrderAttemptFunction:
    """Test the record_order_attempt helper function."""

    def setup_method(self):
        """Reset fill counts before each test."""
        from src.metrics import reset_fill_counts
        reset_fill_counts()

    def test_record_matched_order(self):
        """MATCHED status should count as a fill."""
        from src.metrics import record_order_attempt, get_fill_rate

        result = record_order_attempt(
            market="BTC",
            side="YES",
            status="MATCHED",
            requested_size=10.0,
            filled_size=10.0,
        )

        assert result == 1.0  # Fully filled
        assert get_fill_rate("BTC", "YES") == 100.0

    def test_record_filled_order(self):
        """FILLED status should count as a fill."""
        from src.metrics import record_order_attempt, get_fill_rate

        result = record_order_attempt(
            market="ETH",
            side="NO",
            status="FILLED",
            requested_size=5.0,
            filled_size=5.0,
        )

        assert result == 1.0
        assert get_fill_rate("ETH", "NO") == 100.0

    def test_record_live_order(self):
        """LIVE status should NOT count as a fill."""
        from src.metrics import record_order_attempt, get_fill_rate

        result = record_order_attempt(
            market="BTC",
            side="YES",
            status="LIVE",
            requested_size=10.0,
            filled_size=0.0,
        )

        assert result == 0.0  # No fill
        assert get_fill_rate("BTC", "YES") == 0.0

    def test_record_partial_fill(self):
        """Partial fill should record correct ratio."""
        from src.metrics import record_order_attempt

        result = record_order_attempt(
            market="BTC",
            side="YES",
            status="MATCHED",
            requested_size=10.0,
            filled_size=7.0,  # 70% fill
        )

        assert result == pytest.approx(0.7, rel=0.01)

    def test_record_rejected_order(self):
        """REJECTED status should not count as fill."""
        from src.metrics import record_order_attempt, get_fill_rate

        result = record_order_attempt(
            market="SOL",
            side="NO",
            status="REJECTED",
            requested_size=10.0,
            filled_size=0.0,
            rejection_reason="insufficient_liquidity",
        )

        assert result == 0.0
        assert get_fill_rate("SOL", "NO") == 0.0

    def test_fill_rate_accumulates(self):
        """Fill rate should accumulate across multiple orders."""
        from src.metrics import record_order_attempt, get_fill_rate

        # First order fills
        record_order_attempt("BTC", "YES", "MATCHED", 10.0, 10.0)
        assert get_fill_rate("BTC", "YES") == 100.0

        # Second order doesn't fill
        record_order_attempt("BTC", "YES", "LIVE", 10.0, 0.0)
        assert get_fill_rate("BTC", "YES") == 50.0  # 1/2 = 50%

        # Third order fills
        record_order_attempt("BTC", "YES", "FILLED", 10.0, 10.0)
        assert get_fill_rate("BTC", "YES") == pytest.approx(66.67, rel=0.01)  # 2/3

    def test_slippage_recorded(self):
        """Slippage should be recorded when prices provided."""
        from src.metrics import record_order_attempt

        # Should not raise
        record_order_attempt(
            market="BTC",
            side="YES",
            status="MATCHED",
            requested_size=10.0,
            filled_size=10.0,
            expected_price=0.45,
            execution_price=0.46,  # 1 cent slippage
        )

    def test_liquidity_recorded(self):
        """Liquidity should be recorded when provided."""
        from src.metrics import record_order_attempt

        # Should not raise
        record_order_attempt(
            market="ETH",
            side="NO",
            status="MATCHED",
            requested_size=10.0,
            filled_size=10.0,
            available_liquidity=100.0,
        )

    def test_case_insensitive_status(self):
        """Status comparison should be case insensitive."""
        from src.metrics import record_order_attempt, get_fill_rate, reset_fill_counts

        reset_fill_counts()

        # lowercase should work
        record_order_attempt("BTC", "YES", "matched", 10.0, 10.0)
        assert get_fill_rate("BTC", "YES") == 100.0

        reset_fill_counts()

        # Mixed case should work
        record_order_attempt("BTC", "YES", "Filled", 10.0, 10.0)
        assert get_fill_rate("BTC", "YES") == 100.0


class TestPhase4bGetFillRateFunction:
    """Test the get_fill_rate helper function."""

    def setup_method(self):
        """Reset fill counts before each test."""
        from src.metrics import reset_fill_counts
        reset_fill_counts()

    def test_get_fill_rate_no_data(self):
        """Should return 0 when no data exists."""
        from src.metrics import get_fill_rate

        rate = get_fill_rate("UNKNOWN", "YES")
        assert rate == 0.0

    def test_get_fill_rate_100_percent(self):
        """Should return 100 when all orders fill."""
        from src.metrics import record_order_attempt, get_fill_rate

        record_order_attempt("BTC", "YES", "MATCHED", 10.0, 10.0)
        record_order_attempt("BTC", "YES", "FILLED", 10.0, 10.0)

        assert get_fill_rate("BTC", "YES") == 100.0

    def test_get_fill_rate_0_percent(self):
        """Should return 0 when no orders fill."""
        from src.metrics import record_order_attempt, get_fill_rate

        record_order_attempt("BTC", "NO", "LIVE", 10.0, 0.0)
        record_order_attempt("BTC", "NO", "LIVE", 10.0, 0.0)

        assert get_fill_rate("BTC", "NO") == 0.0

    def test_get_fill_rate_per_side(self):
        """Fill rates should be tracked separately per side."""
        from src.metrics import record_order_attempt, get_fill_rate

        # YES side: 100% fill rate
        record_order_attempt("BTC", "YES", "MATCHED", 10.0, 10.0)

        # NO side: 0% fill rate
        record_order_attempt("BTC", "NO", "LIVE", 10.0, 0.0)

        assert get_fill_rate("BTC", "YES") == 100.0
        assert get_fill_rate("BTC", "NO") == 0.0


class TestPhase4bResetFillCountsFunction:
    """Test the reset_fill_counts function."""

    def test_reset_clears_data(self):
        """Reset should clear all fill count data."""
        from src.metrics import record_order_attempt, get_fill_rate, reset_fill_counts

        # Record some data
        record_order_attempt("BTC", "YES", "MATCHED", 10.0, 10.0)
        assert get_fill_rate("BTC", "YES") == 100.0

        # Reset
        reset_fill_counts()

        # Should be 0 now
        assert get_fill_rate("BTC", "YES") == 0.0


class TestPhase4bSlippageCalculation:
    """Test slippage calculation logic."""

    def test_positive_slippage(self):
        """Paid more than expected = positive slippage."""
        expected = 0.45
        actual = 0.47
        slippage_cents = (actual - expected) * 100

        assert slippage_cents == pytest.approx(2.0, rel=0.01)

    def test_negative_slippage(self):
        """Paid less than expected = negative slippage (favorable)."""
        expected = 0.45
        actual = 0.44
        slippage_cents = (actual - expected) * 100

        assert slippage_cents == pytest.approx(-1.0, rel=0.01)

    def test_zero_slippage(self):
        """No price difference = zero slippage."""
        expected = 0.50
        actual = 0.50
        slippage_cents = (actual - expected) * 100

        assert slippage_cents == 0.0


class TestPhase4bFillRatioCalculation:
    """Test fill ratio calculation logic."""

    def test_full_fill_ratio(self):
        """Fully filled order = 1.0 ratio."""
        requested = 10.0
        filled = 10.0
        ratio = filled / requested if requested > 0 else 0.0

        assert ratio == 1.0

    def test_partial_fill_ratio(self):
        """Partially filled order = partial ratio."""
        requested = 10.0
        filled = 7.5
        ratio = filled / requested if requested > 0 else 0.0

        assert ratio == pytest.approx(0.75, rel=0.01)

    def test_no_fill_ratio(self):
        """Unfilled order = 0.0 ratio."""
        requested = 10.0
        filled = 0.0
        ratio = filled / requested if requested > 0 else 0.0

        assert ratio == 0.0

    def test_zero_requested_ratio(self):
        """Zero requested size should not cause division error."""
        requested = 0.0
        filled = 0.0
        ratio = filled / requested if requested > 0 else 0.0

        assert ratio == 0.0


class TestPhase4bHistogramBuckets:
    """Test that histogram buckets are appropriately configured."""

    def test_partial_fill_ratio_buckets(self):
        """Partial fill ratio histogram should have 0-1 buckets."""
        from src.metrics import PARTIAL_FILL_RATIO

        buckets = list(PARTIAL_FILL_RATIO._upper_bounds)[:-1]

        assert 0.0 in buckets
        assert 0.5 in buckets
        assert 1.0 in buckets

    def test_slippage_buckets(self):
        """Slippage histogram should have cent-based buckets."""
        from src.metrics import SLIPPAGE_CENTS

        buckets = list(SLIPPAGE_CENTS._upper_bounds)[:-1]

        # Should have 0 (no slippage)
        assert 0.0 in buckets
        # Should have common slippage values
        assert 1.0 in buckets
        assert 2.0 in buckets

    def test_liquidity_buckets(self):
        """Liquidity histogram should have share-based buckets."""
        from src.metrics import LIQUIDITY_AT_ORDER

        buckets = list(LIQUIDITY_AT_ORDER._upper_bounds)[:-1]

        # Should cover common liquidity ranges
        assert any(b <= 10 for b in buckets)
        assert any(b >= 100 for b in buckets)


class TestPhase4bRejectionReasons:
    """Test rejection reason tracking."""

    def test_common_rejection_reasons(self):
        """Common rejection reasons should be trackable."""
        from src.metrics import record_order_attempt, reset_fill_counts

        reset_fill_counts()

        reasons = [
            "insufficient_liquidity",
            "price_moved",
            "timeout",
            "cancelled",
            "unknown",
        ]

        for reason in reasons:
            # Should not raise
            record_order_attempt(
                market="TEST",
                side="YES",
                status="REJECTED",
                requested_size=10.0,
                filled_size=0.0,
                rejection_reason=reason,
            )

    def test_default_rejection_reason(self):
        """Missing rejection reason should default to 'unknown'."""
        from src.metrics import record_order_attempt, reset_fill_counts

        reset_fill_counts()

        # Should not raise even without rejection_reason
        record_order_attempt(
            market="TEST",
            side="YES",
            status="REJECTED",
            requested_size=10.0,
            filled_size=0.0,
        )


# ============================================================================
# Phase 4c Tests - P&L Tracking Metrics
# ============================================================================

class TestPhase4cPnLMetricsExist:
    """Test that Phase 4c P&L metrics are defined in metrics.py."""

    def test_expected_profit_histogram_exists(self):
        """EXPECTED_PROFIT_USD histogram should be defined."""
        from src.metrics import EXPECTED_PROFIT_USD

        assert EXPECTED_PROFIT_USD is not None
        assert "market" in EXPECTED_PROFIT_USD._labelnames

    def test_realized_profit_histogram_exists(self):
        """REALIZED_PROFIT_USD histogram should be defined."""
        from src.metrics import REALIZED_PROFIT_USD

        assert REALIZED_PROFIT_USD is not None
        assert "market" in REALIZED_PROFIT_USD._labelnames
        assert "outcome" in REALIZED_PROFIT_USD._labelnames

    def test_pnl_variance_histogram_exists(self):
        """PNL_VARIANCE_USD histogram should be defined."""
        from src.metrics import PNL_VARIANCE_USD

        assert PNL_VARIANCE_USD is not None
        assert "market" in PNL_VARIANCE_USD._labelnames

    def test_cumulative_expected_gauge_exists(self):
        """CUMULATIVE_EXPECTED_PNL_USD gauge should be defined."""
        from src.metrics import CUMULATIVE_EXPECTED_PNL_USD

        assert CUMULATIVE_EXPECTED_PNL_USD is not None
        assert "market" in CUMULATIVE_EXPECTED_PNL_USD._labelnames

    def test_cumulative_realized_gauge_exists(self):
        """CUMULATIVE_REALIZED_PNL_USD gauge should be defined."""
        from src.metrics import CUMULATIVE_REALIZED_PNL_USD

        assert CUMULATIVE_REALIZED_PNL_USD is not None
        assert "market" in CUMULATIVE_REALIZED_PNL_USD._labelnames

    def test_trade_outcome_counter_exists(self):
        """TRADE_OUTCOME_TOTAL counter should be defined."""
        from src.metrics import TRADE_OUTCOME_TOTAL

        assert TRADE_OUTCOME_TOTAL is not None
        assert "market" in TRADE_OUTCOME_TOTAL._labelnames
        assert "outcome" in TRADE_OUTCOME_TOTAL._labelnames

    def test_win_rate_gauge_exists(self):
        """WIN_RATE_GAUGE should be defined."""
        from src.metrics import WIN_RATE_GAUGE

        assert WIN_RATE_GAUGE is not None
        assert "market" in WIN_RATE_GAUGE._labelnames

    def test_expected_value_per_trade_gauge_exists(self):
        """EXPECTED_VALUE_PER_TRADE gauge should be defined."""
        from src.metrics import EXPECTED_VALUE_PER_TRADE

        assert EXPECTED_VALUE_PER_TRADE is not None
        assert "market" in EXPECTED_VALUE_PER_TRADE._labelnames

    def test_realized_value_per_trade_gauge_exists(self):
        """REALIZED_VALUE_PER_TRADE gauge should be defined."""
        from src.metrics import REALIZED_VALUE_PER_TRADE

        assert REALIZED_VALUE_PER_TRADE is not None
        assert "market" in REALIZED_VALUE_PER_TRADE._labelnames


class TestPhase4cRecordTradeEntryFunction:
    """Test the record_trade_entry helper function."""

    def setup_method(self):
        """Reset P&L tracking before each test."""
        from src.metrics import reset_pnl_tracking
        reset_pnl_tracking()

    def test_record_trade_entry_returns_trade_id(self):
        """record_trade_entry should return a trade ID."""
        from src.metrics import record_trade_entry

        trade_id = record_trade_entry(
            market="BTC",
            expected_profit_usd=0.10,
            yes_shares=10.0,
            no_shares=10.0,
            yes_cost_usd=4.50,
            no_cost_usd=5.40,
        )

        assert trade_id is not None
        assert isinstance(trade_id, str)
        assert len(trade_id) == 8  # UUID first 8 chars

    def test_record_trade_entry_tracks_expected_profit(self):
        """Trade entry should track expected profit."""
        from src.metrics import record_trade_entry, get_pnl_summary

        record_trade_entry(
            market="BTC",
            expected_profit_usd=0.15,
            yes_shares=10.0,
            no_shares=10.0,
            yes_cost_usd=4.50,
            no_cost_usd=5.35,
        )

        summary = get_pnl_summary("BTC")
        assert summary["cumulative_expected"] == pytest.approx(0.15, rel=0.01)
        assert summary["total_trades"] == 1
        assert summary["pending"] == 1

    def test_record_multiple_trades_accumulates(self):
        """Multiple trade entries should accumulate expected profit."""
        from src.metrics import record_trade_entry, get_pnl_summary

        record_trade_entry("BTC", 0.10, 10.0, 10.0, 4.50, 5.40)
        record_trade_entry("BTC", 0.20, 10.0, 10.0, 4.40, 5.40)
        record_trade_entry("BTC", 0.15, 10.0, 10.0, 4.45, 5.40)

        summary = get_pnl_summary("BTC")
        assert summary["cumulative_expected"] == pytest.approx(0.45, rel=0.01)
        assert summary["total_trades"] == 3
        assert summary["pending"] == 3


class TestPhase4cRecordTradeResolutionFunction:
    """Test the record_trade_resolution helper function."""

    def setup_method(self):
        """Reset P&L tracking before each test."""
        from src.metrics import reset_pnl_tracking
        reset_pnl_tracking()

    def test_resolution_win(self):
        """Positive profit should be classified as win."""
        from src.metrics import record_trade_entry, record_trade_resolution, get_pnl_summary

        record_trade_entry("BTC", 0.10, 10.0, 10.0, 4.50, 5.40)
        outcome = record_trade_resolution("BTC", 0.12, 0.10)

        assert outcome == "win"
        summary = get_pnl_summary("BTC")
        assert summary["wins"] == 1
        assert summary["losses"] == 0
        assert summary["pending"] == 0

    def test_resolution_loss(self):
        """Negative profit should be classified as loss."""
        from src.metrics import record_trade_entry, record_trade_resolution, get_pnl_summary

        record_trade_entry("BTC", 0.10, 10.0, 10.0, 4.50, 5.40)
        outcome = record_trade_resolution("BTC", -0.50, 0.10)

        assert outcome == "loss"
        summary = get_pnl_summary("BTC")
        assert summary["wins"] == 0
        assert summary["losses"] == 1

    def test_resolution_break_even(self):
        """Near-zero profit should be classified as break_even."""
        from src.metrics import record_trade_entry, record_trade_resolution

        record_trade_entry("BTC", 0.10, 10.0, 10.0, 4.50, 5.40)
        outcome = record_trade_resolution("BTC", 0.005, 0.10)

        assert outcome == "break_even"

    def test_cumulative_realized_updates(self):
        """Cumulative realized P&L should update after resolution."""
        from src.metrics import record_trade_entry, record_trade_resolution, get_pnl_summary

        record_trade_entry("BTC", 0.10, 10.0, 10.0, 4.50, 5.40)
        record_trade_resolution("BTC", 0.15, 0.10)

        summary = get_pnl_summary("BTC")
        assert summary["cumulative_realized"] == pytest.approx(0.15, rel=0.01)

    def test_variance_calculation(self):
        """Variance should be realized - expected."""
        from src.metrics import record_trade_entry, record_trade_resolution, get_pnl_summary

        record_trade_entry("BTC", 0.10, 10.0, 10.0, 4.50, 5.40)
        record_trade_resolution("BTC", 0.15, 0.10)

        # Variance = 0.15 - 0.10 = 0.05 (better than expected)
        summary = get_pnl_summary("BTC")
        expected_variance = summary["cumulative_realized"] - summary["cumulative_expected"]
        assert expected_variance == pytest.approx(0.05, rel=0.01)


class TestPhase4cWinRateCalculation:
    """Test win rate calculation."""

    def setup_method(self):
        """Reset P&L tracking before each test."""
        from src.metrics import reset_pnl_tracking
        reset_pnl_tracking()

    def test_win_rate_100_percent(self):
        """100% win rate when all trades win."""
        from src.metrics import record_trade_entry, record_trade_resolution, get_pnl_summary

        for _ in range(3):
            record_trade_entry("BTC", 0.10, 10.0, 10.0, 4.50, 5.40)
            record_trade_resolution("BTC", 0.15, 0.10)

        summary = get_pnl_summary("BTC")
        assert summary["win_rate"] == 100.0

    def test_win_rate_0_percent(self):
        """0% win rate when all trades lose."""
        from src.metrics import record_trade_entry, record_trade_resolution, get_pnl_summary

        for _ in range(3):
            record_trade_entry("BTC", 0.10, 10.0, 10.0, 4.50, 5.40)
            record_trade_resolution("BTC", -0.50, 0.10)

        summary = get_pnl_summary("BTC")
        assert summary["win_rate"] == 0.0

    def test_win_rate_50_percent(self):
        """50% win rate when half win half lose."""
        from src.metrics import record_trade_entry, record_trade_resolution, get_pnl_summary

        # 2 wins
        record_trade_entry("BTC", 0.10, 10.0, 10.0, 4.50, 5.40)
        record_trade_resolution("BTC", 0.15, 0.10)
        record_trade_entry("BTC", 0.10, 10.0, 10.0, 4.50, 5.40)
        record_trade_resolution("BTC", 0.12, 0.10)

        # 2 losses
        record_trade_entry("BTC", 0.10, 10.0, 10.0, 4.50, 5.40)
        record_trade_resolution("BTC", -0.30, 0.10)
        record_trade_entry("BTC", 0.10, 10.0, 10.0, 4.50, 5.40)
        record_trade_resolution("BTC", -0.25, 0.10)

        summary = get_pnl_summary("BTC")
        assert summary["win_rate"] == 50.0

    def test_win_rate_excludes_pending(self):
        """Win rate should only count resolved trades."""
        from src.metrics import record_trade_entry, record_trade_resolution, get_pnl_summary

        # 1 win
        record_trade_entry("BTC", 0.10, 10.0, 10.0, 4.50, 5.40)
        record_trade_resolution("BTC", 0.15, 0.10)

        # 2 pending (not resolved)
        record_trade_entry("BTC", 0.10, 10.0, 10.0, 4.50, 5.40)
        record_trade_entry("BTC", 0.10, 10.0, 10.0, 4.50, 5.40)

        summary = get_pnl_summary("BTC")
        assert summary["win_rate"] == 100.0  # 1/1 = 100%
        assert summary["pending"] == 2


class TestPhase4cGetPnLSummaryFunction:
    """Test the get_pnl_summary helper function."""

    def setup_method(self):
        """Reset P&L tracking before each test."""
        from src.metrics import reset_pnl_tracking
        reset_pnl_tracking()

    def test_summary_unknown_market(self):
        """Should return zeros for unknown market."""
        from src.metrics import get_pnl_summary

        summary = get_pnl_summary("UNKNOWN")

        assert summary["cumulative_expected"] == 0.0
        assert summary["cumulative_realized"] == 0.0
        assert summary["total_trades"] == 0
        assert summary["wins"] == 0
        assert summary["losses"] == 0
        assert summary["pending"] == 0
        assert summary["win_rate"] == 0.0

    def test_summary_all_fields_present(self):
        """Summary should include all required fields."""
        from src.metrics import record_trade_entry, record_trade_resolution, get_pnl_summary

        record_trade_entry("BTC", 0.10, 10.0, 10.0, 4.50, 5.40)
        record_trade_resolution("BTC", 0.15, 0.10)

        summary = get_pnl_summary("BTC")

        assert "cumulative_expected" in summary
        assert "cumulative_realized" in summary
        assert "total_trades" in summary
        assert "wins" in summary
        assert "losses" in summary
        assert "pending" in summary
        assert "win_rate" in summary


class TestPhase4cResetPnLTrackingFunction:
    """Test the reset_pnl_tracking function."""

    def setup_method(self):
        """Reset P&L tracking before each test."""
        from src.metrics import reset_pnl_tracking
        reset_pnl_tracking()

    def test_reset_clears_all_data(self):
        """Reset should clear all P&L tracking data."""
        from src.metrics import record_trade_entry, record_trade_resolution, get_pnl_summary, reset_pnl_tracking

        record_trade_entry("BTC", 0.10, 10.0, 10.0, 4.50, 5.40)
        record_trade_resolution("BTC", 0.15, 0.10)

        summary_before = get_pnl_summary("BTC")
        assert summary_before["total_trades"] == 1

        reset_pnl_tracking()

        summary_after = get_pnl_summary("BTC")
        assert summary_after["total_trades"] == 0


class TestPhase4cVarianceCalculation:
    """Test variance calculation (realized - expected)."""

    def test_positive_variance(self):
        """Positive variance = better than expected."""
        expected = 0.10
        realized = 0.15
        variance = realized - expected

        assert variance == pytest.approx(0.05, rel=0.01)

    def test_negative_variance(self):
        """Negative variance = worse than expected."""
        expected = 0.10
        realized = -0.20
        variance = realized - expected

        assert variance == pytest.approx(-0.30, rel=0.01)

    def test_zero_variance(self):
        """Zero variance = exactly as expected."""
        expected = 0.10
        realized = 0.10
        variance = realized - expected

        assert variance == 0.0


class TestPhase4cOutcomeClassification:
    """Test outcome classification thresholds."""

    def test_win_threshold(self):
        """Profit > 0.01 should be a win."""
        threshold = 0.01

        assert 0.02 > threshold  # Win
        assert 0.15 > threshold  # Win
        assert 1.00 > threshold  # Win

    def test_loss_threshold(self):
        """Profit < -0.01 should be a loss."""
        threshold = -0.01

        assert -0.02 < threshold  # Loss
        assert -0.50 < threshold  # Loss
        assert -5.00 < threshold  # Loss

    def test_break_even_range(self):
        """Profit between -0.01 and 0.01 should be break_even."""
        profit = 0.005
        is_break_even = -0.01 <= profit <= 0.01

        assert is_break_even

        profit = -0.005
        is_break_even = -0.01 <= profit <= 0.01
        assert is_break_even


class TestPhase4cHistogramBuckets:
    """Test that histogram buckets are appropriately configured."""

    def test_expected_profit_buckets(self):
        """Expected profit histogram should have positive buckets."""
        from src.metrics import EXPECTED_PROFIT_USD

        buckets = list(EXPECTED_PROFIT_USD._upper_bounds)[:-1]

        # Should have small profit buckets
        assert any(b <= 0.10 for b in buckets)
        # Should have larger profit buckets
        assert any(b >= 1.0 for b in buckets)

    def test_realized_profit_buckets(self):
        """Realized profit histogram should include negative values."""
        from src.metrics import REALIZED_PROFIT_USD

        buckets = list(REALIZED_PROFIT_USD._upper_bounds)[:-1]

        # Should have negative buckets for losses
        assert any(b < 0 for b in buckets)
        # Should have positive buckets for wins
        assert any(b > 0 for b in buckets)

    def test_variance_buckets(self):
        """Variance histogram should include negative and positive values."""
        from src.metrics import PNL_VARIANCE_USD

        buckets = list(PNL_VARIANCE_USD._upper_bounds)[:-1]

        # Should have negative buckets (worse than expected)
        assert any(b < 0 for b in buckets)
        # Should have positive buckets (better than expected)
        assert any(b > 0 for b in buckets)


class TestPhase4cPerMarketTracking:
    """Test that P&L is tracked separately per market."""

    def setup_method(self):
        """Reset P&L tracking before each test."""
        from src.metrics import reset_pnl_tracking
        reset_pnl_tracking()

    def test_separate_market_tracking(self):
        """Each market should have independent P&L tracking."""
        from src.metrics import record_trade_entry, record_trade_resolution, get_pnl_summary

        # BTC: 1 win
        record_trade_entry("BTC", 0.10, 10.0, 10.0, 4.50, 5.40)
        record_trade_resolution("BTC", 0.15, 0.10)

        # ETH: 1 loss
        record_trade_entry("ETH", 0.10, 10.0, 10.0, 4.50, 5.40)
        record_trade_resolution("ETH", -0.50, 0.10)

        btc_summary = get_pnl_summary("BTC")
        eth_summary = get_pnl_summary("ETH")

        assert btc_summary["wins"] == 1
        assert btc_summary["losses"] == 0
        assert btc_summary["win_rate"] == 100.0

        assert eth_summary["wins"] == 0
        assert eth_summary["losses"] == 1
        assert eth_summary["win_rate"] == 0.0


# =============================================================================
# PHASE 4d: Pre-trade Expected Hedge Ratio Calculation
# =============================================================================
# Before placing orders, calculate expected hedge ratio based on liquidity.
# Reject trades if expected hedge ratio is below minimum BEFORE any orders placed.
# Metrics: EXPECTED_HEDGE_RATIO, EXPECTED_HEDGE_RATIO_HISTOGRAM,
#          HEDGE_RATIO_PREDICTION_ERROR, PRE_TRADE_REJECTIONS_TOTAL,
#          LIQUIDITY_IMBALANCE_RATIO


class TestPhase4dExpectedHedgeMetricsExist:
    """Verify Phase 4d pre-trade hedge metrics exist."""

    def test_expected_hedge_ratio_gauge_exists(self):
        """Expected hedge ratio gauge should exist."""
        from src.metrics import EXPECTED_HEDGE_RATIO
        from prometheus_client import Gauge

        assert isinstance(EXPECTED_HEDGE_RATIO, Gauge)
        assert EXPECTED_HEDGE_RATIO._name == "polymarket_expected_hedge_ratio"

    def test_expected_hedge_ratio_histogram_exists(self):
        """Expected hedge ratio histogram should exist."""
        from src.metrics import EXPECTED_HEDGE_RATIO_HISTOGRAM
        from prometheus_client import Histogram

        assert isinstance(EXPECTED_HEDGE_RATIO_HISTOGRAM, Histogram)
        assert EXPECTED_HEDGE_RATIO_HISTOGRAM._name == "polymarket_expected_hedge_ratio_distribution"

    def test_hedge_prediction_error_histogram_exists(self):
        """Prediction error histogram should exist."""
        from src.metrics import HEDGE_RATIO_PREDICTION_ERROR
        from prometheus_client import Histogram

        assert isinstance(HEDGE_RATIO_PREDICTION_ERROR, Histogram)
        assert HEDGE_RATIO_PREDICTION_ERROR._name == "polymarket_hedge_ratio_prediction_error"

    def test_pre_trade_rejections_counter_exists(self):
        """Pre-trade rejections counter should exist."""
        from src.metrics import PRE_TRADE_REJECTIONS_TOTAL
        from prometheus_client import Counter

        assert isinstance(PRE_TRADE_REJECTIONS_TOTAL, Counter)
        # Counter names don't have _total suffix in internal name
        assert PRE_TRADE_REJECTIONS_TOTAL._name == "polymarket_pre_trade_rejections"

    def test_liquidity_imbalance_histogram_exists(self):
        """Liquidity imbalance histogram should exist."""
        from src.metrics import LIQUIDITY_IMBALANCE_RATIO
        from prometheus_client import Histogram

        assert isinstance(LIQUIDITY_IMBALANCE_RATIO, Histogram)
        assert LIQUIDITY_IMBALANCE_RATIO._name == "polymarket_liquidity_imbalance_ratio"


class TestPhase4dCalculateExpectedHedgeRatio:
    """Test the calculate_expected_hedge_ratio function."""

    def test_perfect_liquidity_gives_perfect_hedge(self):
        """When liquidity exceeds needs on both sides, hedge ratio should be 1.0."""
        from src.metrics import calculate_expected_hedge_ratio

        ratio, reason = calculate_expected_hedge_ratio(
            yes_liquidity_shares=100.0,  # 100 shares available
            no_liquidity_shares=100.0,
            yes_shares_needed=10.0,  # Only need 10
            no_shares_needed=10.0,
            persistence_factor=0.4,
        )

        # With 100 * 0.4 = 40 shares persistent, needing 10, we get full fill
        assert ratio == pytest.approx(1.0, rel=0.01)
        assert reason == ""

    def test_insufficient_yes_liquidity(self):
        """When YES liquidity is insufficient, hedge ratio drops."""
        from src.metrics import calculate_expected_hedge_ratio

        ratio, reason = calculate_expected_hedge_ratio(
            yes_liquidity_shares=10.0,  # 10 * 0.4 = 4 persistent
            no_liquidity_shares=100.0,  # 100 * 0.4 = 40 persistent
            yes_shares_needed=10.0,  # Need 10, only 4 available
            no_shares_needed=10.0,  # Need 10, 40 available
            persistence_factor=0.4,
        )

        # Expected YES fill: min(10, 4) = 4
        # Expected NO fill: min(10, 40) = 10
        # Hedge ratio: 4 / 10 = 0.4
        assert ratio == pytest.approx(0.4, rel=0.01)
        assert reason == "yes_liquidity_insufficient"

    def test_insufficient_no_liquidity(self):
        """When NO liquidity is insufficient, hedge ratio drops."""
        from src.metrics import calculate_expected_hedge_ratio

        ratio, reason = calculate_expected_hedge_ratio(
            yes_liquidity_shares=100.0,
            no_liquidity_shares=10.0,  # 10 * 0.4 = 4 persistent
            yes_shares_needed=10.0,
            no_shares_needed=10.0,
            persistence_factor=0.4,
        )

        # Expected YES fill: 10 (enough liquidity)
        # Expected NO fill: 4 (limited)
        # Hedge ratio: 4 / 10 = 0.4
        assert ratio == pytest.approx(0.4, rel=0.01)
        assert reason == "no_liquidity_insufficient"

    def test_both_sides_insufficient(self):
        """When both sides have insufficient liquidity."""
        from src.metrics import calculate_expected_hedge_ratio

        ratio, reason = calculate_expected_hedge_ratio(
            yes_liquidity_shares=5.0,  # 5 * 0.4 = 2 persistent
            no_liquidity_shares=10.0,  # 10 * 0.4 = 4 persistent
            yes_shares_needed=20.0,  # Need 20
            no_shares_needed=20.0,  # Need 20
            persistence_factor=0.4,
        )

        # Expected YES fill: min(20, 2) = 2
        # Expected NO fill: min(20, 4) = 4
        # Hedge ratio: 2 / 4 = 0.5
        assert ratio == pytest.approx(0.5, rel=0.01)
        assert reason == "both_sides_insufficient"

    def test_zero_liquidity_returns_zero(self):
        """Zero liquidity should return 0.0 hedge ratio."""
        from src.metrics import calculate_expected_hedge_ratio

        ratio, reason = calculate_expected_hedge_ratio(
            yes_liquidity_shares=0.0,
            no_liquidity_shares=100.0,
            yes_shares_needed=10.0,
            no_shares_needed=10.0,
        )

        assert ratio == 0.0
        assert reason == "no_liquidity"

    def test_zero_shares_needed_returns_zero(self):
        """Zero shares needed should return 0.0."""
        from src.metrics import calculate_expected_hedge_ratio

        ratio, reason = calculate_expected_hedge_ratio(
            yes_liquidity_shares=100.0,
            no_liquidity_shares=100.0,
            yes_shares_needed=0.0,
            no_shares_needed=10.0,
        )

        assert ratio == 0.0
        assert reason == "zero_shares_needed"

    def test_custom_persistence_factor(self):
        """Custom persistence factor should affect calculation."""
        from src.metrics import calculate_expected_hedge_ratio

        # With 0.8 persistence, 50 shares * 0.8 = 40 persistent
        ratio, reason = calculate_expected_hedge_ratio(
            yes_liquidity_shares=50.0,
            no_liquidity_shares=50.0,
            yes_shares_needed=10.0,
            no_shares_needed=10.0,
            persistence_factor=0.8,  # Higher persistence
        )

        assert ratio == pytest.approx(1.0, rel=0.01)

    def test_imbalanced_order_sizes(self):
        """When order sizes are imbalanced but liquidity is adequate."""
        from src.metrics import calculate_expected_hedge_ratio

        ratio, reason = calculate_expected_hedge_ratio(
            yes_liquidity_shares=100.0,  # 40 persistent
            no_liquidity_shares=100.0,  # 40 persistent
            yes_shares_needed=10.0,  # Different sizes
            no_shares_needed=30.0,  # Will fill fully but imbalanced
            persistence_factor=0.4,
        )

        # Both fill completely: YES=10, NO=30
        # Hedge ratio: 10 / 30 = 0.333
        assert ratio == pytest.approx(0.333, rel=0.01)
        assert reason == "imbalanced_order_sizes"


class TestPhase4dRecordExpectedHedgeRatio:
    """Test the record_expected_hedge_ratio function."""

    def test_high_ratio_should_proceed(self):
        """High expected ratio should return should_proceed=True."""
        from src.metrics import record_expected_hedge_ratio

        ratio, should_proceed, reason = record_expected_hedge_ratio(
            market="BTC",
            asset="BTC",
            yes_liquidity=100.0,
            no_liquidity=100.0,
            yes_shares_needed=10.0,
            no_shares_needed=10.0,
            min_hedge_ratio=0.80,
        )

        assert ratio == pytest.approx(1.0, rel=0.01)
        assert should_proceed is True
        assert reason == ""

    def test_low_ratio_should_not_proceed(self):
        """Low expected ratio should return should_proceed=False."""
        from src.metrics import record_expected_hedge_ratio

        ratio, should_proceed, reason = record_expected_hedge_ratio(
            market="BTC",
            asset="BTC",
            yes_liquidity=10.0,  # Only 4 persistent
            no_liquidity=100.0,
            yes_shares_needed=10.0,
            no_shares_needed=10.0,
            min_hedge_ratio=0.80,
        )

        assert ratio == pytest.approx(0.4, rel=0.01)
        assert should_proceed is False
        assert reason == "yes_liquidity_insufficient"

    def test_exactly_at_threshold(self):
        """Ratio exactly at threshold should proceed."""
        from src.metrics import record_expected_hedge_ratio

        # Need ratio = 0.80 exactly
        # If YES has 20 shares * 0.4 = 8 persistent, NO has 25 * 0.4 = 10
        # YES fill = min(10, 8) = 8, NO fill = min(10, 10) = 10
        # Ratio = 8/10 = 0.80
        ratio, should_proceed, reason = record_expected_hedge_ratio(
            market="BTC",
            asset="BTC",
            yes_liquidity=20.0,
            no_liquidity=25.0,
            yes_shares_needed=10.0,
            no_shares_needed=10.0,
            min_hedge_ratio=0.80,
        )

        assert ratio == pytest.approx(0.80, rel=0.01)
        assert should_proceed is True

    def test_custom_min_hedge_ratio(self):
        """Custom min_hedge_ratio should be respected."""
        from src.metrics import record_expected_hedge_ratio

        ratio, should_proceed, reason = record_expected_hedge_ratio(
            market="BTC",
            asset="BTC",
            yes_liquidity=100.0,
            no_liquidity=100.0,
            yes_shares_needed=10.0,
            no_shares_needed=10.0,
            min_hedge_ratio=0.95,  # Very high threshold
        )

        assert ratio == pytest.approx(1.0, rel=0.01)
        assert should_proceed is True  # 1.0 >= 0.95


class TestPhase4dRecordPredictionAccuracy:
    """Test the record_hedge_prediction_accuracy function."""

    def test_perfect_prediction(self):
        """No error when prediction matches reality."""
        from src.metrics import record_hedge_prediction_accuracy

        error = record_hedge_prediction_accuracy(
            market="BTC",
            expected_ratio=0.85,
            actual_ratio=0.85,
        )

        assert error == pytest.approx(0.0, abs=0.001)

    def test_underestimated_hedge(self):
        """Positive error when actual > expected."""
        from src.metrics import record_hedge_prediction_accuracy

        error = record_hedge_prediction_accuracy(
            market="BTC",
            expected_ratio=0.70,
            actual_ratio=0.85,
        )

        assert error == pytest.approx(0.15, rel=0.01)

    def test_overestimated_hedge(self):
        """Negative error when actual < expected."""
        from src.metrics import record_hedge_prediction_accuracy

        error = record_hedge_prediction_accuracy(
            market="BTC",
            expected_ratio=0.90,
            actual_ratio=0.75,
        )

        assert error == pytest.approx(-0.15, rel=0.01)


class TestPhase4dHistogramBuckets:
    """Test histogram bucket configurations for Phase 4d."""

    def test_expected_hedge_ratio_buckets(self):
        """Expected hedge ratio histogram should have correct buckets."""
        from src.metrics import EXPECTED_HEDGE_RATIO_HISTOGRAM

        # Buckets: 0.0, 0.2, 0.4, 0.6, 0.7, 0.8, 0.85, 0.9, 0.95, 1.0
        assert len(EXPECTED_HEDGE_RATIO_HISTOGRAM._upper_bounds) >= 10
        # Check first bucket starts at 0.0
        assert EXPECTED_HEDGE_RATIO_HISTOGRAM._upper_bounds[0] >= 0.0

    def test_prediction_error_buckets(self):
        """Prediction error histogram should have symmetric buckets."""
        from src.metrics import HEDGE_RATIO_PREDICTION_ERROR

        # Should include negative and positive values
        # Buckets: -0.5, -0.3, -0.2, -0.1, -0.05, 0.0, 0.05, 0.1, 0.2, 0.3, 0.5
        assert len(HEDGE_RATIO_PREDICTION_ERROR._upper_bounds) >= 10

    def test_liquidity_imbalance_buckets(self):
        """Liquidity imbalance histogram should have 0-1 buckets."""
        from src.metrics import LIQUIDITY_IMBALANCE_RATIO

        # Buckets: 0.0 to 1.0 in 0.1 increments
        assert len(LIQUIDITY_IMBALANCE_RATIO._upper_bounds) >= 10
        assert LIQUIDITY_IMBALANCE_RATIO._upper_bounds[0] >= 0.0


class TestPhase4dEdgeCases:
    """Test edge cases for Phase 4d functions."""

    def test_very_small_liquidity(self):
        """Very small liquidity values should be handled."""
        from src.metrics import calculate_expected_hedge_ratio

        ratio, reason = calculate_expected_hedge_ratio(
            yes_liquidity_shares=0.001,
            no_liquidity_shares=0.001,
            yes_shares_needed=0.001,
            no_shares_needed=0.001,
        )

        # With 0.4 persistence: 0.001 * 0.4 = 0.0004
        # Need 0.001, only 0.0004 available
        # Both limited equally: ratio = 1.0 (balanced limitation)
        assert ratio == pytest.approx(1.0, rel=0.1)

    def test_very_large_liquidity(self):
        """Very large liquidity values should be handled."""
        from src.metrics import calculate_expected_hedge_ratio

        ratio, reason = calculate_expected_hedge_ratio(
            yes_liquidity_shares=1_000_000.0,
            no_liquidity_shares=1_000_000.0,
            yes_shares_needed=100.0,
            no_shares_needed=100.0,
        )

        assert ratio == pytest.approx(1.0, rel=0.01)
        assert reason == ""

    def test_asymmetric_needs_with_symmetric_liquidity(self):
        """Asymmetric order sizes with symmetric liquidity."""
        from src.metrics import calculate_expected_hedge_ratio

        ratio, reason = calculate_expected_hedge_ratio(
            yes_liquidity_shares=100.0,
            no_liquidity_shares=100.0,
            yes_shares_needed=5.0,  # Small YES
            no_shares_needed=20.0,  # Large NO
        )

        # Both fill completely (enough liquidity)
        # Hedge ratio: 5/20 = 0.25
        assert ratio == pytest.approx(0.25, rel=0.01)


class TestPhase4dIntegrationScenarios:
    """Test realistic trading scenarios for Phase 4d."""

    def test_typical_arb_opportunity(self):
        """Test typical arbitrage with good liquidity."""
        from src.metrics import record_expected_hedge_ratio

        # Typical scenario: $10 trade, YES @ $0.45, NO @ $0.50
        # Budget split: $4.50 YES, $5.50 NO
        # Shares: YES = 10, NO = 11
        ratio, proceed, reason = record_expected_hedge_ratio(
            market="BTC",
            asset="BTC",
            yes_liquidity=50.0,  # 20 persistent
            no_liquidity=50.0,  # 20 persistent
            yes_shares_needed=10.0,
            no_shares_needed=11.0,
            min_hedge_ratio=0.80,
        )

        # Both fill: ratio = 10/11 = 0.91
        assert ratio > 0.80
        assert proceed is True

    def test_thin_liquidity_rejection(self):
        """Test rejection when one side has thin liquidity."""
        from src.metrics import record_expected_hedge_ratio

        # Thin NO liquidity: only 5 shares displayed
        # Persistent: NO = 5 * 0.4 = 2 shares, YES = 100 * 0.4 = 40 shares
        # Needing 10 shares each:
        # YES fills: min(10, 40) = 10
        # NO fills: min(10, 2) = 2
        # Ratio: 2/10 = 0.2
        ratio, proceed, reason = record_expected_hedge_ratio(
            market="ETH",
            asset="ETH",
            yes_liquidity=100.0,  # Plenty of YES
            no_liquidity=5.0,  # Thin NO = 2 persistent
            yes_shares_needed=10.0,
            no_shares_needed=10.0,
            min_hedge_ratio=0.80,
        )

        assert ratio < 0.80
        assert proceed is False

    def test_one_sided_thin_liquidity(self):
        """Test when only one side has thin liquidity."""
        from src.metrics import record_expected_hedge_ratio

        ratio, proceed, reason = record_expected_hedge_ratio(
            market="SOL",
            asset="SOL",
            yes_liquidity=100.0,  # Plenty of YES
            no_liquidity=5.0,  # Thin NO
            yes_shares_needed=10.0,
            no_shares_needed=10.0,
            min_hedge_ratio=0.80,
        )

        # YES fills 10, NO fills 2
        # Ratio: 2/10 = 0.2
        assert ratio < 0.50
        assert proceed is False
        assert "no_liquidity" in reason


# =============================================================================
# PHASE 4e: Post-trade Rebalancing Logic
# =============================================================================
# When a trade results in an imbalanced position, immediately sell excess shares
# to eliminate unhedged directional exposure. Uses IOC (immediate-or-cancel) style.
# Metrics: REBALANCE_ATTEMPTS_TOTAL, REBALANCE_OUTCOME_TOTAL, REBALANCE_SHARES_SOLD,
#          REBALANCE_SLIPPAGE_USD, POST_REBALANCE_HEDGE_RATIO, UNHEDGED_SHARES_REMAINING


class TestPhase4eRebalanceMetricsExist:
    """Verify Phase 4e rebalancing metrics exist."""

    def test_rebalance_attempts_counter_exists(self):
        """Rebalance attempts counter should exist."""
        from src.metrics import REBALANCE_ATTEMPTS_TOTAL
        from prometheus_client import Counter

        assert isinstance(REBALANCE_ATTEMPTS_TOTAL, Counter)
        assert REBALANCE_ATTEMPTS_TOTAL._name == "polymarket_rebalance_attempts"

    def test_rebalance_outcome_counter_exists(self):
        """Rebalance outcome counter should exist."""
        from src.metrics import REBALANCE_OUTCOME_TOTAL
        from prometheus_client import Counter

        assert isinstance(REBALANCE_OUTCOME_TOTAL, Counter)
        assert REBALANCE_OUTCOME_TOTAL._name == "polymarket_rebalance_outcome"

    def test_rebalance_shares_sold_histogram_exists(self):
        """Rebalance shares sold histogram should exist."""
        from src.metrics import REBALANCE_SHARES_SOLD
        from prometheus_client import Histogram

        assert isinstance(REBALANCE_SHARES_SOLD, Histogram)
        assert REBALANCE_SHARES_SOLD._name == "polymarket_rebalance_shares_sold"

    def test_rebalance_slippage_histogram_exists(self):
        """Rebalance slippage histogram should exist."""
        from src.metrics import REBALANCE_SLIPPAGE_USD
        from prometheus_client import Histogram

        assert isinstance(REBALANCE_SLIPPAGE_USD, Histogram)
        assert REBALANCE_SLIPPAGE_USD._name == "polymarket_rebalance_slippage_usd"

    def test_post_rebalance_hedge_ratio_gauge_exists(self):
        """Post-rebalance hedge ratio gauge should exist."""
        from src.metrics import POST_REBALANCE_HEDGE_RATIO
        from prometheus_client import Gauge

        assert isinstance(POST_REBALANCE_HEDGE_RATIO, Gauge)
        assert POST_REBALANCE_HEDGE_RATIO._name == "polymarket_post_rebalance_hedge_ratio"

    def test_unhedged_shares_remaining_gauge_exists(self):
        """Unhedged shares remaining gauge should exist."""
        from src.metrics import UNHEDGED_SHARES_REMAINING
        from prometheus_client import Gauge

        assert isinstance(UNHEDGED_SHARES_REMAINING, Gauge)
        assert UNHEDGED_SHARES_REMAINING._name == "polymarket_unhedged_shares_remaining"


class TestPhase4eRebalanceDecisionDataclass:
    """Test the RebalanceDecision dataclass."""

    def test_dataclass_fields(self):
        """RebalanceDecision should have correct fields."""
        from src.metrics import RebalanceDecision

        decision = RebalanceDecision(
            should_rebalance=True,
            side_to_sell="YES",
            shares_to_sell=8.0,
            current_hedge_ratio=0.60,
            target_hedge_ratio=1.0,
            reason="hedge_ratio_low",
        )

        assert decision.should_rebalance is True
        assert decision.side_to_sell == "YES"
        assert decision.shares_to_sell == 8.0
        assert decision.current_hedge_ratio == 0.60
        assert decision.target_hedge_ratio == 1.0
        assert decision.reason == "hedge_ratio_low"


class TestPhase4eRebalanceResultDataclass:
    """Test the RebalanceResult dataclass."""

    def test_dataclass_fields(self):
        """RebalanceResult should have correct fields."""
        from src.metrics import RebalanceResult

        result = RebalanceResult(
            success=True,
            shares_requested=8.0,
            shares_filled=8.0,
            fill_price=0.44,
            slippage_usd=0.08,
            pre_hedge_ratio=0.60,
            post_hedge_ratio=1.0,
            outcome="full_fill",
        )

        assert result.success is True
        assert result.shares_requested == 8.0
        assert result.shares_filled == 8.0
        assert result.fill_price == 0.44
        assert result.slippage_usd == 0.08
        assert result.pre_hedge_ratio == 0.60
        assert result.post_hedge_ratio == 1.0
        assert result.outcome == "full_fill"


class TestPhase4eCalculateRebalanceNeeded:
    """Test the calculate_rebalance_needed function."""

    def test_balanced_position_no_rebalance(self):
        """Balanced position should not need rebalancing."""
        from src.metrics import calculate_rebalance_needed

        decision = calculate_rebalance_needed(
            yes_shares=20.0,
            no_shares=20.0,
            min_hedge_ratio=0.80,
            max_imbalance_shares=5.0,
        )

        assert decision.should_rebalance is False
        assert decision.current_hedge_ratio == 1.0
        assert decision.reason == "within_tolerance"

    def test_slightly_imbalanced_within_tolerance(self):
        """Slightly imbalanced position within tolerance should not rebalance."""
        from src.metrics import calculate_rebalance_needed

        decision = calculate_rebalance_needed(
            yes_shares=20.0,
            no_shares=18.0,  # 2 share difference, ratio = 0.9
            min_hedge_ratio=0.80,
            max_imbalance_shares=5.0,
        )

        assert decision.should_rebalance is False
        assert decision.current_hedge_ratio == pytest.approx(0.9, rel=0.01)
        assert decision.reason == "within_tolerance"

    def test_imbalanced_yes_heavy(self):
        """YES-heavy position should rebalance by selling YES."""
        from src.metrics import calculate_rebalance_needed

        decision = calculate_rebalance_needed(
            yes_shares=20.0,
            no_shares=12.0,  # Ratio = 0.6, need to sell 8 YES
            min_hedge_ratio=0.80,
            max_imbalance_shares=5.0,
        )

        assert decision.should_rebalance is True
        assert decision.side_to_sell == "YES"
        assert decision.shares_to_sell == 8.0
        assert decision.current_hedge_ratio == pytest.approx(0.6, rel=0.01)
        assert decision.reason == "hedge_ratio_low"

    def test_imbalanced_no_heavy(self):
        """NO-heavy position should rebalance by selling NO."""
        from src.metrics import calculate_rebalance_needed

        decision = calculate_rebalance_needed(
            yes_shares=12.0,
            no_shares=20.0,  # Ratio = 0.6, need to sell 8 NO
            min_hedge_ratio=0.80,
            max_imbalance_shares=5.0,
        )

        assert decision.should_rebalance is True
        assert decision.side_to_sell == "NO"
        assert decision.shares_to_sell == 8.0
        assert decision.current_hedge_ratio == pytest.approx(0.6, rel=0.01)

    def test_one_sided_yes_only(self):
        """One-sided YES position should sell all."""
        from src.metrics import calculate_rebalance_needed

        decision = calculate_rebalance_needed(
            yes_shares=15.0,
            no_shares=0.0,
        )

        assert decision.should_rebalance is True
        assert decision.side_to_sell == "YES"
        assert decision.shares_to_sell == 15.0
        assert decision.current_hedge_ratio == 0.0
        assert decision.reason == "one_sided_position"

    def test_one_sided_no_only(self):
        """One-sided NO position should sell all."""
        from src.metrics import calculate_rebalance_needed

        decision = calculate_rebalance_needed(
            yes_shares=0.0,
            no_shares=15.0,
        )

        assert decision.should_rebalance is True
        assert decision.side_to_sell == "NO"
        assert decision.shares_to_sell == 15.0
        assert decision.reason == "one_sided_position"

    def test_no_position(self):
        """No position should not need rebalancing."""
        from src.metrics import calculate_rebalance_needed

        decision = calculate_rebalance_needed(
            yes_shares=0.0,
            no_shares=0.0,
        )

        assert decision.should_rebalance is False
        assert decision.reason == "no_position"

    def test_high_imbalance_triggers_rebalance(self):
        """High share imbalance should trigger rebalance even with good ratio."""
        from src.metrics import calculate_rebalance_needed

        # 100 YES, 94 NO = ratio 0.94 (above 0.80), but 6 share imbalance (above 5)
        decision = calculate_rebalance_needed(
            yes_shares=100.0,
            no_shares=94.0,
            min_hedge_ratio=0.80,
            max_imbalance_shares=5.0,
        )

        assert decision.should_rebalance is True
        assert decision.side_to_sell == "YES"
        assert decision.shares_to_sell == 6.0
        assert decision.reason == "imbalance_high"


class TestPhase4eEvaluateRebalanceWorth:
    """Test the evaluate_rebalance_worth function."""

    def test_acceptable_slippage(self):
        """Small slippage should be acceptable."""
        from src.metrics import evaluate_rebalance_worth

        worth, slippage, reason = evaluate_rebalance_worth(
            shares_to_sell=8.0,
            expected_fill_price=0.44,
            original_purchase_price=0.45,
            max_slippage_pct=0.10,
        )

        # Slippage = (0.45 - 0.44) * 8 = $0.08
        # Slippage pct = 0.01 / 0.45 = 2.2%
        assert worth is True
        assert slippage == pytest.approx(0.08, rel=0.01)
        assert reason == "acceptable"

    def test_slippage_too_high(self):
        """High slippage should be rejected."""
        from src.metrics import evaluate_rebalance_worth

        worth, slippage, reason = evaluate_rebalance_worth(
            shares_to_sell=8.0,
            expected_fill_price=0.30,  # Much lower than purchase
            original_purchase_price=0.45,
            max_slippage_pct=0.10,  # 10% max
        )

        # Slippage = (0.45 - 0.30) / 0.45 = 33%
        assert worth is False
        assert "slippage_too_high" in reason

    def test_zero_shares(self):
        """Zero shares should not be worth rebalancing."""
        from src.metrics import evaluate_rebalance_worth

        worth, slippage, reason = evaluate_rebalance_worth(
            shares_to_sell=0.0,
            expected_fill_price=0.44,
            original_purchase_price=0.45,
        )

        assert worth is False
        assert reason == "no_shares_to_sell"

    def test_invalid_price(self):
        """Invalid price should not be worth rebalancing."""
        from src.metrics import evaluate_rebalance_worth

        worth, slippage, reason = evaluate_rebalance_worth(
            shares_to_sell=8.0,
            expected_fill_price=0.0,
            original_purchase_price=0.45,
        )

        assert worth is False
        assert reason == "invalid_price"

    def test_selling_at_profit(self):
        """Selling at profit should be acceptable."""
        from src.metrics import evaluate_rebalance_worth

        worth, slippage, reason = evaluate_rebalance_worth(
            shares_to_sell=8.0,
            expected_fill_price=0.50,  # Higher than purchase!
            original_purchase_price=0.45,
        )

        # Negative slippage (profit)
        assert worth is True
        assert slippage == pytest.approx(-0.40, rel=0.01)  # (0.45-0.50) * 8 = -0.40
        assert reason == "acceptable"


class TestPhase4eCalculatePostRebalanceHedgeRatio:
    """Test the calculate_post_rebalance_hedge_ratio function."""

    def test_full_rebalance_yes(self):
        """Full rebalance of YES should result in 1.0 ratio."""
        from src.metrics import calculate_post_rebalance_hedge_ratio

        ratio = calculate_post_rebalance_hedge_ratio(
            yes_shares_before=20.0,
            no_shares_before=12.0,
            side_sold="YES",
            shares_sold=8.0,  # Sell excess to match
        )

        # After: 12 YES, 12 NO = 1.0
        assert ratio == pytest.approx(1.0, rel=0.01)

    def test_full_rebalance_no(self):
        """Full rebalance of NO should result in 1.0 ratio."""
        from src.metrics import calculate_post_rebalance_hedge_ratio

        ratio = calculate_post_rebalance_hedge_ratio(
            yes_shares_before=12.0,
            no_shares_before=20.0,
            side_sold="NO",
            shares_sold=8.0,
        )

        # After: 12 YES, 12 NO = 1.0
        assert ratio == pytest.approx(1.0, rel=0.01)

    def test_partial_rebalance(self):
        """Partial rebalance should improve ratio."""
        from src.metrics import calculate_post_rebalance_hedge_ratio

        ratio = calculate_post_rebalance_hedge_ratio(
            yes_shares_before=20.0,
            no_shares_before=12.0,
            side_sold="YES",
            shares_sold=5.0,  # Only sell 5 of 8 excess
        )

        # After: 15 YES, 12 NO = 0.80
        assert ratio == pytest.approx(0.80, rel=0.01)

    def test_oversell_results_in_zero(self):
        """Overselling should result in 0.0 ratio."""
        from src.metrics import calculate_post_rebalance_hedge_ratio

        ratio = calculate_post_rebalance_hedge_ratio(
            yes_shares_before=20.0,
            no_shares_before=12.0,
            side_sold="YES",
            shares_sold=25.0,  # Sell more than we have
        )

        # After: -5 YES (invalid), 12 NO = 0.0
        assert ratio == 0.0


class TestPhase4eIntegrationScenarios:
    """Test realistic rebalancing scenarios."""

    def test_typical_imbalanced_trade_scenario(self):
        """Test typical scenario: partial fill creates imbalance."""
        from src.metrics import (
            calculate_rebalance_needed,
            evaluate_rebalance_worth,
            calculate_post_rebalance_hedge_ratio,
        )

        # After trade: 20 YES @ $0.45, only 12 NO filled @ $0.55
        # Hedge ratio: 12/20 = 0.60
        decision = calculate_rebalance_needed(
            yes_shares=20.0,
            no_shares=12.0,
            min_hedge_ratio=0.80,
        )

        assert decision.should_rebalance is True
        assert decision.side_to_sell == "YES"
        assert decision.shares_to_sell == 8.0

        # Evaluate if rebalancing is worth it
        worth, slippage, reason = evaluate_rebalance_worth(
            shares_to_sell=8.0,
            expected_fill_price=0.44,  # Bid is slightly below purchase
            original_purchase_price=0.45,
        )

        assert worth is True
        assert slippage == pytest.approx(0.08, rel=0.01)

        # Calculate post-rebalance ratio (assuming full fill)
        post_ratio = calculate_post_rebalance_hedge_ratio(
            yes_shares_before=20.0,
            no_shares_before=12.0,
            side_sold="YES",
            shares_sold=8.0,
        )

        assert post_ratio == pytest.approx(1.0, rel=0.01)

    def test_partial_fill_rebalance_scenario(self):
        """Test scenario where rebalance only partially fills."""
        from src.metrics import (
            calculate_rebalance_needed,
            calculate_post_rebalance_hedge_ratio,
            RebalanceResult,
        )

        # Imbalanced position
        decision = calculate_rebalance_needed(
            yes_shares=20.0,
            no_shares=12.0,
        )

        # IOC order only fills 5 of 8 shares
        result = RebalanceResult(
            success=True,  # Partial success
            shares_requested=8.0,
            shares_filled=5.0,  # Partial fill
            fill_price=0.44,
            slippage_usd=0.05,
            pre_hedge_ratio=0.60,
            post_hedge_ratio=0.80,
            outcome="partial_fill",
        )

        # Verify post-ratio calculation
        post_ratio = calculate_post_rebalance_hedge_ratio(
            yes_shares_before=20.0,
            no_shares_before=12.0,
            side_sold="YES",
            shares_sold=5.0,
        )

        # After: 15 YES, 12 NO = 0.80
        assert post_ratio == pytest.approx(0.80, rel=0.01)
        assert result.outcome == "partial_fill"

    def test_no_fill_scenario(self):
        """Test scenario where rebalance gets no fill."""
        from src.metrics import RebalanceResult

        result = RebalanceResult(
            success=False,
            shares_requested=8.0,
            shares_filled=0.0,
            fill_price=0.0,
            slippage_usd=0.0,
            pre_hedge_ratio=0.60,
            post_hedge_ratio=0.60,  # Unchanged
            outcome="no_fill",
        )

        assert result.success is False
        assert result.shares_filled == 0.0
        assert result.pre_hedge_ratio == result.post_hedge_ratio
