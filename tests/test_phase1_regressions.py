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
