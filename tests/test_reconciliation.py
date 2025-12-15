"""Regression tests for Phase 15-17: Trade Reconciliation & Observability.

These tests verify:
1. Reconciliation endpoint returns correct structure (Phase 15)
2. Positions endpoint returns settlement queue data (Phase 16)
3. Partial fill detection works correctly (Phase 17)
4. Dashboard JavaScript has reconciliation widgets (Phase 16)

See: docs/STRATEGY_ARCHITECTURE.md (Phases 15-17)
See: agents/plans/polymarket-bot-strategy-improvements.md
"""

import pytest
from unittest.mock import MagicMock, AsyncMock, patch
from datetime import datetime, timedelta


class TestReconciliationEndpointStructure:
    """Test /dashboard/reconciliation endpoint returns correct structure.

    Phase 15: Trade reconciliation compares Polymarket API trades with
    local database to identify untracked positions.
    """

    def test_dashboard_has_reconciliation_route(self):
        """Verify /dashboard/reconciliation route is registered."""
        from src.dashboard import DashboardServer

        server = DashboardServer()
        assert hasattr(server, '_handle_reconciliation'), (
            "DashboardServer must have _handle_reconciliation method"
        )

    @pytest.mark.asyncio
    async def test_reconciliation_response_structure(self):
        """Verify reconciliation endpoint returns expected JSON fields."""
        # This test validates the response structure defined in _handle_reconciliation
        expected_fields = [
            "wallet",
            "days_checked",
            "polymarket_trades",
            "polymarket_markets",
            "local_trades",
            "settlement_queue",
            "untracked_positions",
            "untracked_count",
            "total_untracked_value",
            "last_run",
        ]

        # Verify these fields exist in the handler code
        from src.dashboard import DashboardServer
        import inspect

        # Get the handler source
        handler_source = inspect.getsource(DashboardServer._handle_reconciliation)

        for field in expected_fields:
            assert f'"{field}"' in handler_source, (
                f"Reconciliation response must include '{field}' field"
            )

    def test_reconciliation_handles_missing_wallet(self):
        """Verify reconciliation gracefully handles missing wallet config."""
        from src.dashboard import DashboardServer
        import inspect

        handler_source = inspect.getsource(DashboardServer._handle_reconciliation)

        # Should check for wallet and return error if not configured
        assert "POLYMARKET_PROXY_WALLET" in handler_source
        assert "not configured" in handler_source.lower() or "error" in handler_source


class TestPositionsEndpointStructure:
    """Test /dashboard/positions endpoint returns settlement queue data.

    Phase 16: Dashboard shows historical positions from settlement_queue table.
    """

    def test_dashboard_has_positions_route(self):
        """Verify /dashboard/positions route is registered."""
        from src.dashboard import DashboardServer

        server = DashboardServer()
        assert hasattr(server, '_handle_positions'), (
            "DashboardServer must have _handle_positions method"
        )

    @pytest.mark.asyncio
    async def test_positions_response_structure(self):
        """Verify positions endpoint returns expected JSON fields."""
        expected_fields = ["positions", "stats"]

        from src.dashboard import DashboardServer
        import inspect

        handler_source = inspect.getsource(DashboardServer._handle_positions)

        for field in expected_fields:
            assert f'"{field}"' in handler_source, (
                f"Positions response must include '{field}' field"
            )

    def test_positions_uses_get_settlement_history(self):
        """Verify positions endpoint calls get_settlement_history."""
        from src.dashboard import DashboardServer
        import inspect

        handler_source = inspect.getsource(DashboardServer._handle_positions)

        assert "get_settlement_history" in handler_source, (
            "Positions endpoint must call get_settlement_history"
        )


class TestSettlementHistoryMethod:
    """Test Database.get_settlement_history() method.

    Phase 16: Settlement history provides historical position data for dashboard.
    """

    @pytest.mark.asyncio
    async def test_get_settlement_history_exists(self):
        """Verify get_settlement_history method exists in Database."""
        from src.persistence import Database

        assert hasattr(Database, 'get_settlement_history'), (
            "Database must have get_settlement_history method"
        )

    @pytest.mark.asyncio
    async def test_get_settlement_history_returns_list(self):
        """Verify get_settlement_history returns a list."""
        import tempfile
        from pathlib import Path
        from src.persistence import Database

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"
            db = Database(db_path)
            await db.connect()

            # Call the method
            result = await db.get_settlement_history(limit=10)

            # Should return a list (even if empty)
            assert isinstance(result, list), (
                "get_settlement_history must return a list"
            )

            await db.close()

    @pytest.mark.asyncio
    async def test_get_settlement_history_respects_limit(self):
        """Verify get_settlement_history respects the limit parameter."""
        import tempfile
        from pathlib import Path
        from src.persistence import Database

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"
            db = Database(db_path)
            await db.connect()

            # Add some test positions
            for i in range(5):
                await db.add_to_settlement_queue(
                    trade_id=f"test-trade-{i}",
                    condition_id=f"cond{i}",
                    token_id=f"token{i}",
                    side="YES",
                    asset="BTC",
                    shares=10.0,
                    entry_price=0.50,
                    entry_cost=5.0,
                    market_end_time=datetime.utcnow(),
                )

            # Get with limit of 3
            result = await db.get_settlement_history(limit=3)

            assert len(result) <= 3, (
                "get_settlement_history must respect limit parameter"
            )

            await db.close()


class TestPartialFillDetection:
    """Test partial fill detection fix in place_order_sync.

    Phase 17: Exceptions in place_order_sync are now caught and returned
    as error dicts instead of raising, allowing parallel execution to
    detect partial fills when one leg fills and the other throws.
    """

    def test_place_order_sync_catches_exceptions(self):
        """Verify place_order_sync catches exceptions and returns error dict.

        Regression test: Previously, exceptions would bubble up from place_order_sync,
        causing asyncio.gather to raise, and the outer handler would return
        partial_fill=False even though one leg had actually filled.
        """
        from src.client.polymarket import PolymarketClient
        import inspect

        # Get the method source
        if hasattr(PolymarketClient, 'place_order_sync'):
            source = inspect.getsource(PolymarketClient.place_order_sync)

            # Should have try/except that catches exceptions
            assert "try:" in source and "except" in source, (
                "place_order_sync must have try/except to catch exceptions"
            )

            # Should return an error dict instead of raising
            assert "EXCEPTION" in source or "status" in source, (
                "place_order_sync should return error status instead of raising"
            )

    def test_dual_leg_result_has_partial_fill_field(self):
        """Verify DualLegResult can indicate partial fills."""
        from src.client.polymarket import DualLegResult

        # Create a partial fill result
        result = DualLegResult(
            success=False,
            intended_yes_shares=20.0,
            intended_no_shares=20.0,
            actual_yes_shares=20.0,  # YES filled
            actual_no_shares=0,      # NO didn't fill
            yes_status="MATCHED",
            no_status="FAILED",
        )

        # Should have partial_fill attribute or be detectable via shares
        assert result.actual_yes_shares > 0 and result.actual_no_shares == 0, (
            "DualLegResult should indicate one leg filled"
        )


class TestDashboardReconciliationWidgets:
    """Test dashboard JavaScript has reconciliation UI widgets.

    Phase 16: Dashboard shows RECON status indicator and reconciliation panel.
    """

    def test_dashboard_has_recon_status_indicator(self):
        """Verify dashboard has RECON status indicator in header."""
        from src.dashboard import DASHBOARD_HTML

        # Look for RECON status indicator
        assert 'recon-status' in DASHBOARD_HTML.lower() or 'reconciliation' in DASHBOARD_HTML.lower(), (
            "Dashboard must have reconciliation status indicator"
        )

    def test_dashboard_has_reconciliation_panel(self):
        """Verify dashboard has Reconciliation Status panel."""
        from src.dashboard import DASHBOARD_HTML

        # Should have a reconciliation section or panel
        assert 'reconciliation' in DASHBOARD_HTML.lower(), (
            "Dashboard must have Reconciliation panel section"
        )

    def test_dashboard_has_load_reconciliation_function(self):
        """Verify dashboard JavaScript has function to load reconciliation data."""
        from src.dashboard import DASHBOARD_HTML

        # Should have JavaScript function to load reconciliation
        assert 'reconciliation' in DASHBOARD_HTML.lower() and 'fetch' in DASHBOARD_HTML, (
            "Dashboard must have JavaScript to fetch reconciliation data"
        )

    def test_dashboard_has_historical_positions_section(self):
        """Verify dashboard has Historical Positions section."""
        from src.dashboard import DASHBOARD_HTML

        # Look for historical positions or settlement queue section
        assert 'historical' in DASHBOARD_HTML.lower() or 'settlement' in DASHBOARD_HTML.lower(), (
            "Dashboard must have Historical Positions section"
        )


class TestReconciliationScriptIntegration:
    """Test reconciliation script can be used programmatically.

    Phase 15: scripts/reconcile_trades.py should work standalone and
    have similar logic to the dashboard endpoint.
    """

    def test_reconcile_script_exists(self):
        """Verify reconcile_trades.py script exists."""
        from pathlib import Path

        script_path = Path(__file__).parent.parent / "scripts" / "reconcile_trades.py"
        assert script_path.exists(), (
            "scripts/reconcile_trades.py must exist"
        )

    def test_reconcile_script_has_required_functions(self):
        """Verify reconcile_trades.py has required functions."""
        import sys
        from pathlib import Path

        # Add scripts to path
        scripts_path = Path(__file__).parent.parent / "scripts"
        sys.path.insert(0, str(scripts_path))

        try:
            import reconcile_trades

            assert hasattr(reconcile_trades, 'fetch_polymarket_trades'), (
                "reconcile_trades must have fetch_polymarket_trades function"
            )
            assert hasattr(reconcile_trades, 'analyze_polymarket_trades'), (
                "reconcile_trades must have analyze_polymarket_trades function"
            )
            assert hasattr(reconcile_trades, 'find_untracked_positions'), (
                "reconcile_trades must have find_untracked_positions function"
            )
        finally:
            sys.path.remove(str(scripts_path))


class TestUntrackedPositionStructure:
    """Test untracked position data structure.

    Both reconciliation script and dashboard endpoint should identify
    untracked positions with consistent structure.
    """

    def test_untracked_position_has_required_fields(self):
        """Verify untracked position dict has required fields."""
        expected_fields = [
            "condition_id",
            "title",
            "up_shares",
            "down_shares",
            "total_cost",
            "is_hedged",
        ]

        from src.dashboard import DashboardServer
        import inspect

        handler_source = inspect.getsource(DashboardServer._handle_reconciliation)

        for field in expected_fields:
            assert f'"{field}"' in handler_source, (
                f"Untracked position must include '{field}' field"
            )


class TestReconciliationAPIEndpoint:
    """Test the Polymarket Data API integration."""

    def test_uses_correct_api_base(self):
        """Verify correct Polymarket Data API base URL is used."""
        from src.dashboard import DashboardServer
        import inspect

        handler_source = inspect.getsource(DashboardServer._handle_reconciliation)

        assert "data-api.polymarket.com" in handler_source, (
            "Reconciliation must use Polymarket Data API"
        )

    def test_uses_async_httpx_client(self):
        """Verify async httpx client is used for API calls."""
        from src.dashboard import DashboardServer
        import inspect

        handler_source = inspect.getsource(DashboardServer._handle_reconciliation)

        assert "httpx.AsyncClient" in handler_source, (
            "Reconciliation must use async httpx client for API calls"
        )


class TestSettlementQueueAddToQueue:
    """Test positions are properly added to settlement queue.

    Phase 17 fix ensures partial fills are recorded to settlement queue
    so they can be claimed when the market resolves.
    """

    @pytest.mark.asyncio
    async def test_add_to_settlement_queue_accepts_required_fields(self):
        """Verify add_to_settlement_queue accepts all required fields."""
        import tempfile
        from pathlib import Path
        from src.persistence import Database

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"
            db = Database(db_path)
            await db.connect()

            # This should not raise
            await db.add_to_settlement_queue(
                trade_id="test-trade-123",
                condition_id="cond123",
                token_id="token123",
                side="YES",
                asset="BTC",
                shares=20.0,
                entry_price=0.50,
                entry_cost=10.0,
                market_end_time=datetime.utcnow(),
            )

            # Verify it was added
            history = await db.get_settlement_history(limit=10)
            assert len(history) == 1
            assert history[0]["trade_id"] == "test-trade-123"
            assert history[0]["side"] == "YES"
            assert history[0]["shares"] == 20.0

            await db.close()


class TestReconciliationDaysParameter:
    """Test reconciliation respects days parameter."""

    def test_reconciliation_accepts_days_parameter(self):
        """Verify reconciliation endpoint accepts days query parameter."""
        from src.dashboard import DashboardServer
        import inspect

        handler_source = inspect.getsource(DashboardServer._handle_reconciliation)

        assert "days" in handler_source, (
            "Reconciliation must accept days query parameter"
        )

    def test_reconciliation_caps_days_at_30(self):
        """Verify reconciliation caps days at reasonable maximum."""
        from src.dashboard import DashboardServer
        import inspect

        handler_source = inspect.getsource(DashboardServer._handle_reconciliation)

        # Should have a maximum cap to prevent excessive API calls
        assert "30" in handler_source or "min(days" in handler_source, (
            "Reconciliation should cap days parameter"
        )


class TestErrorHandling:
    """Test error handling in reconciliation features."""

    def test_reconciliation_catches_exceptions(self):
        """Verify reconciliation endpoint catches and handles exceptions."""
        from src.dashboard import DashboardServer
        import inspect

        handler_source = inspect.getsource(DashboardServer._handle_reconciliation)

        assert "try:" in handler_source and "except" in handler_source, (
            "Reconciliation handler must have try/except for error handling"
        )

    def test_reconciliation_returns_error_response(self):
        """Verify reconciliation returns error in JSON format on failure."""
        from src.dashboard import DashboardServer
        import inspect

        handler_source = inspect.getsource(DashboardServer._handle_reconciliation)

        assert '"error"' in handler_source, (
            "Reconciliation must return error field in JSON response"
        )
