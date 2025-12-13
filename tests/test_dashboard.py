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

    def test_dashboard_uses_request_animation_frame(self):
        """Verify dashboard uses requestAnimationFrame for debouncing.

        Regression test: Rapid updates (~100/sec from WebSocket) must be
        debounced to the browser's ~60fps refresh rate.
        """
        from src.dashboard import DASHBOARD_HTML

        assert 'requestAnimationFrame' in DASHBOARD_HTML, (
            "Dashboard must use requestAnimationFrame to debounce rapid updates"
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

    def test_pending_market_update_debouncing(self):
        """Verify pendingMarketUpdate variable exists for debouncing.

        Regression test: Rapid updates need to be debounced, and we track
        this with a pending update flag/ID.
        """
        from src.dashboard import DASHBOARD_HTML

        assert 'pendingMarketUpdate' in DASHBOARD_HTML, (
            "Dashboard must have pendingMarketUpdate for debouncing"
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
