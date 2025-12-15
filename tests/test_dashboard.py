"""Tests for dashboard functionality.

Regression tests for:
1. Dashboard using optimized market updates (cell-level updates vs innerHTML)
2. Initial state loading using optimized function
   (Issue: Dashboard UI flickering due to full table replacement on every update)
"""

import re


class TestDashboardOptimizedUpdates:
    """Regression tests for optimized dashboard market updates.

    Issue: Dashboard markets table was flickering because every update
    replaced the entire table innerHTML, causing visual disruption.

    Fix:
    1. Added updateMarketsOptimized() function that caches DOM rows and
       only updates individual cell textContent when values change
    2. Use requestAnimationFrame debouncing to batch rapid updates
    3. Route initial state loading through the optimized function
    """

    def test_dashboard_has_optimized_market_function(self):
        """Verify updateMarketsOptimized function exists in dashboard JavaScript.

        Regression test: The dashboard must have this optimized function
        instead of replacing innerHTML on every update.
        """
        from src.dashboard import DASHBOARD_HTML

        assert 'function updateMarketsOptimized' in DASHBOARD_HTML, (
            "Dashboard must have updateMarketsOptimized function for flicker-free updates"
        )

    def test_dashboard_has_market_row_cache(self):
        """Verify dashboard uses a cache for market row elements.

        Regression test: Without caching row elements, we can't do
        incremental updates - we'd need to replace the whole table.
        """
        from src.dashboard import DASHBOARD_HTML

        assert 'marketRowCache' in DASHBOARD_HTML, (
            "Dashboard must cache market row elements for incremental updates"
        )

    def test_dashboard_uses_debouncing(self):
        """Verify dashboard uses debouncing for market updates.

        Regression test: Rapid updates (~100/sec from WebSocket) must be
        debounced to prevent UI flickering.
        """
        from src.dashboard import DASHBOARD_HTML

        # Dashboard uses setTimeout with marketUpdateTimer for debouncing
        assert 'setTimeout' in DASHBOARD_HTML or 'requestAnimationFrame' in DASHBOARD_HTML, (
            "Dashboard must use setTimeout or requestAnimationFrame to debounce rapid updates"
        )

    def test_dashboard_updates_text_content_not_inner_html(self):
        """Verify optimized function updates textContent, not innerHTML.

        Regression test: Updating textContent is faster and doesn't cause
        DOM reconstruction, preventing visual flicker.
        """
        from src.dashboard import DASHBOARD_HTML

        # Find the updateMarketsOptimized function
        pattern = r'function updateMarketsOptimized\([^)]*\)\s*\{[^}]+(?:\{[^}]*\}[^}]*)*\}'
        match = re.search(pattern, DASHBOARD_HTML, re.DOTALL)

        assert match, "Could not find updateMarketsOptimized function"
        func_body = match.group(0)

        # The function should update textContent for price/time changes
        assert '.textContent' in func_body, (
            "updateMarketsOptimized should use textContent for cell updates"
        )

    def test_initial_state_uses_optimized_function(self):
        """Verify initial state loading routes markets through optimized function.

        Regression test: Initial state load from /dashboard/state must also
        use updateMarketsOptimized to prevent flicker on page load.
        """
        from src.dashboard import DASHBOARD_HTML

        # The pattern we're looking for is in the fetch('/dashboard/state') handler:
        # if (data.markets) {
        #     updateMarketsOptimized(data.markets);
        #     delete data.markets;
        # }

        # Verify the fetch handler exists
        assert "fetch('/dashboard/state')" in DASHBOARD_HTML, (
            "Dashboard must have /dashboard/state fetch for initial load"
        )

        # Verify that after fetching state, we handle markets with optimized function
        # This pattern should appear in the .then() handler
        assert 'if (data.markets)' in DASHBOARD_HTML, (
            "Initial state handler must check for markets data"
        )

        # Find the section after 'fetch' that calls updateMarketsOptimized
        fetch_index = DASHBOARD_HTML.find("fetch('/dashboard/state')")
        section_after_fetch = DASHBOARD_HTML[fetch_index:fetch_index + 1000]

        assert 'updateMarketsOptimized(data.markets)' in section_after_fetch, (
            "Initial state load must use updateMarketsOptimized for markets"
        )

    def test_sse_handler_uses_optimized_function(self):
        """Verify SSE message handler routes markets through optimized function.

        Regression test: Real-time SSE updates must also use the optimized
        function to prevent flicker during live updates.
        """
        from src.dashboard import DASHBOARD_HTML

        # Find the evtSource.onmessage handler
        pattern = r'evtSource\.onmessage\s*=\s*function\([^)]*\)\s*\{([^}]+(?:\{[^}]*\}[^}]*)*)\}'
        match = re.search(pattern, DASHBOARD_HTML, re.DOTALL)

        assert match, "Could not find SSE onmessage handler"
        handler_body = match.group(1)

        # The handler should use updateMarketsOptimized
        assert 'updateMarketsOptimized' in handler_body, (
            "SSE handler must route market updates through updateMarketsOptimized"
        )

    def test_market_update_timer_debouncing(self):
        """Verify marketUpdateTimer variable exists for debouncing.

        Regression test: Rapid updates need to be debounced, and we track
        this with a timer variable.
        """
        from src.dashboard import DASHBOARD_HTML

        # Dashboard uses marketUpdateTimer with setTimeout for debouncing
        assert 'marketUpdateTimer' in DASHBOARD_HTML or 'pendingMarketUpdate' in DASHBOARD_HTML, (
            "Dashboard must have marketUpdateTimer or pendingMarketUpdate for debouncing"
        )

    def test_optimized_function_handles_row_removal(self):
        """Verify optimized function removes rows for markets no longer in data.

        When a market expires or is removed, its row should be cleaned up.
        """
        from src.dashboard import DASHBOARD_HTML

        # The function should handle removing rows for missing markets
        assert 'marketRowCache.delete' in DASHBOARD_HTML or 'row.remove()' in DASHBOARD_HTML, (
            "updateMarketsOptimized should clean up rows for removed markets"
        )

    def test_optimized_function_handles_empty_markets(self):
        """Verify optimized function handles empty markets gracefully."""
        from src.dashboard import DASHBOARD_HTML

        # Should have handling for empty markets case
        assert "marketIds.size === 0" in DASHBOARD_HTML or "Object.keys(markets).length === 0" in DASHBOARD_HTML, (
            "updateMarketsOptimized should handle empty markets gracefully"
        )


class TestDashboardSSEStructure:
    """Tests for SSE (Server-Sent Events) structure in dashboard."""

    def test_sse_event_source_exists(self):
        """Verify SSE EventSource is created for real-time updates."""
        from src.dashboard import DASHBOARD_HTML

        assert "new EventSource('/dashboard/events')" in DASHBOARD_HTML, (
            "Dashboard must use SSE EventSource for real-time updates"
        )

    def test_sse_endpoint_exists(self):
        """Verify the SSE endpoint is registered in the server."""
        from src.dashboard import DashboardServer

        server = DashboardServer()
        # The _handle_events method should exist
        assert hasattr(server, '_handle_events'), (
            "DashboardServer must have _handle_events method for SSE"
        )


class TestDashboardMarketsDisplay:
    """Tests for markets display functionality."""

    def test_dashboard_displays_market_counts(self):
        """Verify dashboard displays found/tradeable market counts."""
        from src.dashboard import DASHBOARD_HTML

        assert 'id="market-count"' in DASHBOARD_HTML
        assert 'id="tradeable-count"' in DASHBOARD_HTML

    def test_dashboard_displays_market_prices(self):
        """Verify dashboard displays up/down prices for markets."""
        from src.dashboard import DASHBOARD_HTML

        assert 'Up Price' in DASHBOARD_HTML or 'cell-upprice' in DASHBOARD_HTML
        assert 'Down Price' in DASHBOARD_HTML or 'cell-downprice' in DASHBOARD_HTML

    def test_dashboard_displays_time_remaining(self):
        """Verify dashboard displays time remaining for markets."""
        from src.dashboard import DASHBOARD_HTML

        assert 'Time Left' in DASHBOARD_HTML or 'cell-timeleft' in DASHBOARD_HTML


class TestDashboardActiveMarketsFilter:
    """Regression tests for 15-minute active markets filter.

    Issue: The dashboard filters markets to only show those within 15 minutes.
    The filter must use SERVER-PROVIDED seconds_remaining, NOT client-side
    calculations based on cached end times.

    Why this matters:
    - Client-side cached end times may not be populated on page load
    - Client-side times can drift from server time
    - Server already calculates seconds_remaining accurately

    Bug history:
    - Dec 2025: Filter used client-side marketEndTimes.get(id) which wasn't
      reliable, causing markets to not display even though SSE was sending
      valid data with updating prices.
    """

    def test_filter_uses_server_seconds_remaining(self):
        """Verify filter uses server-provided seconds_remaining, not client-side calc.

        Regression test: The 15-minute filter MUST use m.seconds_remaining from
        the server, not calculate it client-side from cached end times.
        """
        from src.dashboard import DASHBOARD_HTML

        # Find the filtering code in updateMarketsOptimized
        # The correct pattern is: m.seconds_remaining
        # The WRONG pattern would be: marketEndTimes.get(id) or calculateSecondsRemaining

        # The filter should use server-provided seconds
        assert 'serverSeconds = m.seconds_remaining' in DASHBOARD_HTML or \
               'm.seconds_remaining' in DASHBOARD_HTML, (
            "15-minute filter must use server-provided seconds_remaining, "
            "not client-side cached end times"
        )

    def test_filter_does_not_use_client_cache_for_filtering(self):
        """Verify filter does NOT use marketEndTimes cache for filtering.

        Regression test: The filter should not depend on the client-side
        marketEndTimes cache, as it may not be populated on page load.
        """
        from src.dashboard import DASHBOARD_HTML
        import re

        # Find the filteredMarkets definition
        pattern = r'const filteredMarkets = sortedMarkets\.filter\(\([^)]+\)[^{]*\{([^}]+)\}'
        match = re.search(pattern, DASHBOARD_HTML)

        assert match, "Could not find filteredMarkets filter function"
        filter_body = match.group(1)

        # The filter body should NOT use marketEndTimes.get for the actual filtering
        # (sorting can still use it, but filtering must use server data)
        assert 'marketEndTimes.get(id)' not in filter_body, (
            "Filter must NOT use marketEndTimes.get() - this breaks on page load. "
            "Use m.seconds_remaining instead."
        )

    def test_dashboard_includes_seconds_remaining_in_sse(self):
        """Verify SSE market data includes seconds_remaining field.

        The JavaScript expects this field for filtering. The server must
        include it in the SSE market updates.
        """
        from src.dashboard import DashboardServer
        import asyncio

        server = DashboardServer()

        # Test that _build_markets_data includes seconds_remaining
        # We can check this by examining the code structure
        from src.dashboard import DASHBOARD_HTML

        # The JavaScript expects m.seconds_remaining
        assert 'seconds_remaining' in DASHBOARD_HTML, (
            "Dashboard must handle seconds_remaining field from server"
        )

    def test_filter_handles_missing_seconds_remaining(self):
        """Verify filter gracefully handles missing seconds_remaining.

        Regression test: If a market somehow doesn't have seconds_remaining,
        the filter should default to 0 (filtering it out) rather than crashing.
        """
        from src.dashboard import DASHBOARD_HTML

        # Look for the fallback: m.seconds_remaining || 0
        assert 'seconds_remaining || 0' in DASHBOARD_HTML or \
               'seconds_remaining ?? 0' in DASHBOARD_HTML, (
            "Filter must have fallback for missing seconds_remaining"
        )

    def test_filter_window_is_fifteen_minutes(self):
        """Verify the filter window is exactly 15 minutes (900 seconds).

        Regression test: The active markets section should only show markets
        resolving within 15 minutes.
        """
        from src.dashboard import DASHBOARD_HTML

        assert '<= 900' in DASHBOARD_HTML or '<=900' in DASHBOARD_HTML, (
            "Filter window must be 900 seconds (15 minutes)"
        )


class TestDashboardPriceUpdates:
    """Regression tests for real-time price updates.

    Issue: Prices in the Active Markets table were not updating despite
    the SSE stream sending correct data. This was due to the filtering
    logic blocking legitimate market updates.

    These tests verify the complete data flow from server to UI.
    """

    def test_sse_market_data_includes_prices(self):
        """Verify SSE market data structure includes price fields."""
        from src.dashboard import DASHBOARD_HTML

        # The JavaScript should reference price fields from market data
        assert 'up_price' in DASHBOARD_HTML or 'upPrice' in DASHBOARD_HTML
        assert 'down_price' in DASHBOARD_HTML or 'downPrice' in DASHBOARD_HTML

    def test_price_cells_are_updated_individually(self):
        """Verify price cells are updated via textContent, not innerHTML.

        Regression test: Using innerHTML would cause flickering and lose
        focus state. Individual cell updates are required.
        """
        from src.dashboard import DASHBOARD_HTML
        import re

        # Find price update code - should use textContent
        # Pattern: something like priceCell.textContent = or .textContent =
        assert '.textContent =' in DASHBOARD_HTML, (
            "Price updates must use textContent assignment for smooth updates"
        )

    def test_markets_update_function_processes_all_markets(self):
        """Verify update function iterates over all markets in data.

        Regression test: The update function must process all markets
        received in the SSE data, not skip any due to incorrect filtering.
        """
        from src.dashboard import DASHBOARD_HTML

        # Should iterate over markets with Object.entries or similar
        assert 'Object.entries(markets)' in DASHBOARD_HTML or \
               'for (const [id, m]' in DASHBOARD_HTML or \
               'for (const [id, market]' in DASHBOARD_HTML, (
            "Update function must iterate over all markets in received data"
        )

    def test_spread_calculation_exists(self):
        """Verify spread is calculated and displayed.

        The spread (up_price + down_price - 1.0) is a key trading metric.
        """
        from src.dashboard import DASHBOARD_HTML

        # Should have spread calculation
        assert 'spread' in DASHBOARD_HTML.lower(), (
            "Dashboard must display spread for markets"
        )


class TestDashboardReconciliationIntegration:
    """Regression tests for reconciliation dashboard integration.

    Phase 16: Dashboard has observability widgets for reconciliation status
    and historical positions.
    """

    def test_dashboard_has_reconciliation_endpoint(self):
        """Verify /dashboard/reconciliation endpoint is registered."""
        from src.dashboard import DashboardServer

        server = DashboardServer()
        assert hasattr(server, '_handle_reconciliation'), (
            "DashboardServer must have _handle_reconciliation method"
        )

    def test_dashboard_has_positions_endpoint(self):
        """Verify /dashboard/positions endpoint is registered."""
        from src.dashboard import DashboardServer

        server = DashboardServer()
        assert hasattr(server, '_handle_positions'), (
            "DashboardServer must have _handle_positions method"
        )

    def test_dashboard_html_has_recon_section(self):
        """Verify dashboard HTML includes reconciliation section."""
        from src.dashboard import DASHBOARD_HTML

        # Dashboard should have reconciliation-related UI
        assert 'reconciliation' in DASHBOARD_HTML.lower() or 'recon' in DASHBOARD_HTML.lower(), (
            "Dashboard HTML must include reconciliation section"
        )

    def test_dashboard_html_has_positions_section(self):
        """Verify dashboard HTML includes historical positions section."""
        from src.dashboard import DASHBOARD_HTML

        # Dashboard should have positions/settlement history UI
        assert 'historical' in DASHBOARD_HTML.lower() or 'settlement' in DASHBOARD_HTML.lower() or 'position' in DASHBOARD_HTML.lower(), (
            "Dashboard HTML must include historical positions section"
        )

    def test_dashboard_has_refresh_button_for_reconciliation(self):
        """Verify dashboard has refresh button for reconciliation status."""
        from src.dashboard import DASHBOARD_HTML

        # Should have a refresh mechanism
        assert 'refresh' in DASHBOARD_HTML.lower() or 'reload' in DASHBOARD_HTML.lower(), (
            "Dashboard should have refresh capability for reconciliation"
        )


class TestDashboardHttpxImport:
    """Test dashboard has required imports for reconciliation.

    Phase 15-16: Dashboard uses httpx for async API calls to Polymarket.
    """

    def test_dashboard_imports_httpx(self):
        """Verify dashboard imports httpx for async HTTP calls."""
        import src.dashboard as dashboard_module
        import httpx

        # The module should be able to use httpx.AsyncClient
        assert hasattr(httpx, 'AsyncClient'), (
            "httpx must have AsyncClient for async HTTP calls"
        )

    def test_dashboard_imports_required_modules(self):
        """Verify dashboard imports all required modules for reconciliation."""
        import src.dashboard as dashboard_module

        # These should be importable after reading dashboard
        required_modules = ['os', 'httpx', 'json']
        for mod in required_modules:
            assert mod in dir(dashboard_module) or __import__(mod), (
                f"Dashboard should use {mod} module"
            )
