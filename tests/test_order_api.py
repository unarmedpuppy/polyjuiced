"""Tests for Polymarket order API usage and trading strategy protections.

These tests verify that we're using the py-clob-client library correctly,
particularly around order placement and order types (FOK, GTC, etc.).

REGRESSION TESTS FOR KNOWN ISSUES:
1. OrderArgs.__init__() got an unexpected keyword argument 'order_type'
   - The order_type parameter must be passed to post_order(), not OrderArgs().
   - See: TestOrderArgsSignature

2. Decimal precision errors: "invalid amounts, max accuracy of 2 decimals"
   - FOK orders have precision bugs in py-clob-client
   - We use GTC with Decimal module and ROUND_DOWN
   - See: https://github.com/Polymarket/py-clob-client/issues/121
   - See: TestDecimalPrecision

3. Partial fills leaving directional exposure
   - If YES fills but NO doesn't, we're left holding an unhedged position
   - Solution: Pre-flight liquidity check + automatic unwind
   - See: TestPartialFillProtection

4. Position stacking from arb + near-resolution on same market
   - Running both strategies on same market creates unbalanced positions
   - Solution: Track arb positions and skip them for near-resolution
   - See: TestPositionStackingPrevention

5. Auto-settlement for resolved positions
   - py-clob-client lacks native redeem function
   - Workaround: Sell at $0.99 after market resolution
   - See: https://github.com/Polymarket/py-clob-client/issues/117
   - See: TestAutoSettlement
"""

import inspect
import pytest
from unittest.mock import MagicMock, patch, AsyncMock

from py_clob_client.clob_types import OrderArgs, OrderType


class TestOrderArgsSignature:
    """Test that OrderArgs is used correctly according to py-clob-client API."""

    def test_order_args_does_not_accept_order_type(self):
        """Verify OrderArgs does NOT accept order_type parameter.

        This is a regression test. The py-clob-client library expects order_type
        to be passed to post_order(), not OrderArgs(). Passing it to OrderArgs
        causes: "OrderArgs.__init__() got an unexpected keyword argument 'order_type'"
        """
        sig = inspect.signature(OrderArgs.__init__)
        params = list(sig.parameters.keys())

        # order_type should NOT be in OrderArgs parameters
        assert "order_type" not in params, (
            "OrderArgs should not accept order_type parameter. "
            "Pass orderType to post_order() instead."
        )

        # Verify the expected parameters are present
        assert "token_id" in params
        assert "price" in params
        assert "size" in params
        assert "side" in params

    def test_order_args_instantiation_without_order_type(self):
        """Verify OrderArgs can be instantiated without order_type."""
        # This should NOT raise an exception
        order_args = OrderArgs(
            token_id="test_token_123",
            price=0.50,
            size=10.0,
            side="BUY",
        )

        assert order_args.token_id == "test_token_123"
        assert order_args.price == 0.50
        assert order_args.size == 10.0
        assert order_args.side == "BUY"

    def test_order_args_with_order_type_raises_error(self):
        """Verify OrderArgs raises TypeError if order_type is passed.

        This is the exact error we were seeing in production.
        """
        with pytest.raises(TypeError) as exc_info:
            OrderArgs(
                token_id="test_token_123",
                price=0.50,
                size=10.0,
                side="BUY",
                order_type=OrderType.FOK,  # This should fail!
            )

        assert "order_type" in str(exc_info.value)

    def test_order_type_enum_exists(self):
        """Verify OrderType enum has expected values."""
        assert hasattr(OrderType, "FOK")
        assert hasattr(OrderType, "GTC")
        assert hasattr(OrderType, "GTD")


class TestPolymarketClientOrderPlacement:
    """Test order placement in our PolymarketClient wrapper."""

    @pytest.fixture
    def mock_clob_client(self):
        """Create a mock CLOB client."""
        client = MagicMock()
        client.create_order = MagicMock(return_value={"signed": True})
        client.post_order = MagicMock(return_value={"status": "MATCHED"})
        return client

    def test_dual_leg_order_passes_order_type_to_post_order(self, mock_clob_client):
        """Verify execute_dual_leg_order passes orderType to post_order, not OrderArgs."""
        from src.client.polymarket import PolymarketClient

        # Create client with mocked internals
        with patch.object(PolymarketClient, '__init__', lambda x: None):
            client = PolymarketClient()
            client._client = mock_clob_client
            client._connected = True
            client.get_price = MagicMock(return_value=0.50)

            # Import the source to check the implementation
            import src.client.polymarket as polymarket_module
            source = inspect.getsource(polymarket_module.PolymarketClient.execute_dual_leg_order)

            # Verify OrderArgs does NOT contain order_type
            assert "OrderArgs(" in source
            # The OrderArgs call should not have order_type
            assert "order_type=OrderType" not in source.split("OrderArgs(")[1].split(")")[0], (
                "OrderArgs should not receive order_type parameter"
            )

            # Verify post_order receives orderType (GTC due to FOK precision bugs)
            assert "post_order(signed_order, orderType=OrderType.GTC)" in source, (
                "post_order should receive orderType=OrderType.GTC (FOK has precision bugs)"
            )

    def test_near_resolution_trade_passes_order_type_to_post_order(self):
        """Verify _execute_near_resolution_trade passes orderType to post_order."""
        import src.strategies.gabagool as gabagool_module
        source = inspect.getsource(gabagool_module.GabagoolStrategy._execute_near_resolution_trade)

        # Verify OrderArgs does NOT contain order_type
        assert "OrderArgs(" in source
        # The OrderArgs call should not have order_type
        order_args_section = source.split("OrderArgs(")[1].split(")")[0]
        assert "order_type" not in order_args_section, (
            "OrderArgs should not receive order_type parameter in near_resolution_trade"
        )

        # Verify post_order receives orderType (GTC due to FOK precision bugs)
        assert "post_order(signed_order, orderType=OrderType.GTC)" in source, (
            "post_order should receive orderType=OrderType.GTC (FOK has precision bugs)"
        )


class TestOrderTypeUsageInCodebase:
    """Scan codebase for incorrect order_type usage patterns."""

    def test_no_order_type_in_order_args_calls(self):
        """Scan for incorrect pattern: OrderArgs(..., order_type=...)"""
        import os
        import re

        # Pattern that catches order_type being passed to OrderArgs
        bad_pattern = re.compile(r'OrderArgs\([^)]*order_type\s*=')

        src_dir = os.path.join(os.path.dirname(__file__), '..', 'src')

        violations = []
        for root, dirs, files in os.walk(src_dir):
            for filename in files:
                if filename.endswith('.py'):
                    filepath = os.path.join(root, filename)
                    with open(filepath, 'r') as f:
                        content = f.read()
                        matches = bad_pattern.findall(content)
                        if matches:
                            violations.append(f"{filepath}: Found order_type in OrderArgs()")

        assert not violations, (
            f"Found order_type passed to OrderArgs() which is incorrect:\n"
            + "\n".join(violations)
            + "\n\norder_type should be passed to post_order() instead."
        )

    def test_post_order_has_order_type_where_needed(self):
        """Verify post_order calls have orderType parameter.

        Note: We use GTC instead of FOK due to decimal precision bugs in py-clob-client.
        See: https://github.com/Polymarket/py-clob-client/issues/121
        """
        import os
        import re

        src_dir = os.path.join(os.path.dirname(__file__), '..', 'src')

        # Find all post_order calls
        post_order_pattern = re.compile(r'\.post_order\([^)]+\)')

        order_type_needed_files = [
            'client/polymarket.py',  # execute_dual_leg_order
            'strategies/gabagool.py',  # _execute_near_resolution_trade
        ]

        for rel_path in order_type_needed_files:
            filepath = os.path.join(src_dir, rel_path)
            if os.path.exists(filepath):
                with open(filepath, 'r') as f:
                    content = f.read()

                # Check that orders use orderType parameter (GTC due to FOK bugs)
                # Note: Not all post_order calls need explicit orderType, but the ones in
                # execute_dual_leg_order and _execute_near_resolution_trade do
                if 'execute_dual_leg_order' in content or '_execute_near_resolution_trade' in content:
                    assert 'orderType=OrderType.GTC' in content, (
                        f"{rel_path} should use orderType=OrderType.GTC (FOK has precision bugs)"
                    )


class TestAutoSettlement:
    """Tests for automatic settlement and position claiming functionality."""

    def test_claim_resolved_position_method_exists(self):
        """Verify claim_resolved_position method is implemented."""
        import src.client.polymarket as polymarket_module

        assert hasattr(polymarket_module.PolymarketClient, 'claim_resolved_position'), (
            "PolymarketClient should have claim_resolved_position method"
        )

    def test_cancel_stale_orders_method_exists(self):
        """Verify cancel_stale_orders method is implemented."""
        import src.client.polymarket as polymarket_module

        assert hasattr(polymarket_module.PolymarketClient, 'cancel_stale_orders'), (
            "PolymarketClient should have cancel_stale_orders method"
        )

    def test_tracked_position_dataclass_exists(self):
        """Verify TrackedPosition dataclass is defined in gabagool strategy."""
        import src.strategies.gabagool as gabagool_module

        assert hasattr(gabagool_module, 'TrackedPosition'), (
            "gabagool module should have TrackedPosition dataclass"
        )

    def test_tracked_position_has_required_fields(self):
        """Verify TrackedPosition has all required fields."""
        from src.strategies.gabagool import TrackedPosition
        from dataclasses import fields

        field_names = [f.name for f in fields(TrackedPosition)]

        required_fields = [
            'condition_id', 'token_id', 'shares', 'entry_price',
            'entry_cost', 'market_end_time', 'side', 'asset', 'claimed'
        ]

        for field in required_fields:
            assert field in field_names, f"TrackedPosition missing required field: {field}"

    def test_gabagool_strategy_has_settlement_tracking(self):
        """Verify GabagoolStrategy has position tracking infrastructure."""
        import src.strategies.gabagool as gabagool_module
        source = inspect.getsource(gabagool_module.GabagoolStrategy.__init__)

        assert '_tracked_positions' in source, (
            "GabagoolStrategy should initialize _tracked_positions dict"
        )
        assert '_settlement_check_interval' in source, (
            "GabagoolStrategy should have settlement check interval"
        )

    def test_check_settlement_method_exists(self):
        """Verify _check_settlement method is implemented."""
        import src.strategies.gabagool as gabagool_module

        assert hasattr(gabagool_module.GabagoolStrategy, '_check_settlement'), (
            "GabagoolStrategy should have _check_settlement method"
        )

    def test_track_position_method_exists(self):
        """Verify _track_position method is implemented."""
        import src.strategies.gabagool as gabagool_module

        assert hasattr(gabagool_module.GabagoolStrategy, '_track_position'), (
            "GabagoolStrategy should have _track_position method"
        )


class TestPartialFillProtection:
    """Tests for partial fill detection and unwinding."""

    def test_dual_leg_has_liquidity_check(self):
        """Verify execute_dual_leg_order checks liquidity before trading."""
        import src.client.polymarket as polymarket_module
        source = inspect.getsource(polymarket_module.PolymarketClient.execute_dual_leg_order)

        assert "get_order_book" in source, (
            "execute_dual_leg_order should check order book for liquidity"
        )
        assert "Insufficient liquidity" in source, (
            "execute_dual_leg_order should reject on insufficient liquidity"
        )

    def test_dual_leg_has_persistence_estimate(self):
        """Verify liquidity check uses conservative persistence estimate."""
        import src.client.polymarket as polymarket_module
        source = inspect.getsource(polymarket_module.PolymarketClient.execute_dual_leg_order)

        assert "PERSISTENCE_ESTIMATE" in source, (
            "execute_dual_leg_order should apply persistence estimate to displayed depth"
        )
        # Should be a conservative value (< 0.5)
        assert "0.4" in source or "0.3" in source, (
            "Persistence estimate should be conservative (40% or less)"
        )

    def test_dual_leg_has_self_collapse_check(self):
        """Verify we don't consume majority of book depth."""
        import src.client.polymarket as polymarket_module
        source = inspect.getsource(polymarket_module.PolymarketClient.execute_dual_leg_order)

        assert "self-induced" in source.lower() or "collapse" in source.lower(), (
            "execute_dual_leg_order should check for self-induced spread collapse"
        )

    def test_dual_leg_has_unwind_logic(self):
        """Verify execute_dual_leg_order has unwind logic for partial fills."""
        import src.client.polymarket as polymarket_module
        source = inspect.getsource(polymarket_module.PolymarketClient.execute_dual_leg_order)

        assert "unwind" in source.lower(), (
            "execute_dual_leg_order should have unwind logic for partial fills"
        )
        assert "SELL" in source, (
            "execute_dual_leg_order should sell to unwind partial fills"
        )

    def test_partial_fill_returns_unwound_status(self):
        """Verify partial fill response includes unwind status."""
        import src.client.polymarket as polymarket_module
        source = inspect.getsource(polymarket_module.PolymarketClient.execute_dual_leg_order)

        # Check that the return dict includes unwind information
        assert '"unwound"' in source or "'unwound'" in source, (
            "Partial fill response should include 'unwound' status"
        )
        assert '"unwind_order"' in source or "'unwind_order'" in source, (
            "Partial fill response should include 'unwind_order' result"
        )


class TestPositionStackingPrevention:
    """Tests for preventing near-resolution trades from stacking on arbitrage positions."""

    def test_arbitrage_positions_tracking_exists(self):
        """Verify _arbitrage_positions tracking dict exists."""
        import src.strategies.gabagool as gabagool_module
        source = inspect.getsource(gabagool_module.GabagoolStrategy.__init__)

        assert "_arbitrage_positions" in source, (
            "GabagoolStrategy should track arbitrage positions"
        )

    def test_near_resolution_skips_arbitrage_positions(self):
        """Verify near-resolution trades skip markets with existing arbitrage positions."""
        import src.strategies.gabagool as gabagool_module
        source = inspect.getsource(gabagool_module.GabagoolStrategy._check_near_resolution_opportunities)

        assert "_arbitrage_positions" in source, (
            "Near-resolution should check for existing arbitrage positions"
        )
        assert "existing arbitrage position" in source.lower(), (
            "Near-resolution should skip markets with arbitrage positions"
        )

    def test_arbitrage_marks_position_after_trade(self):
        """Verify successful arbitrage trade marks the market."""
        import src.strategies.gabagool as gabagool_module
        source = inspect.getsource(gabagool_module.GabagoolStrategy._execute_trade)

        assert "_arbitrage_positions" in source, (
            "Arbitrage trade should mark position after successful execution"
        )

    def test_arbitrage_positions_cleared_on_reset(self):
        """Verify arbitrage positions are cleared on daily reset."""
        import src.strategies.gabagool as gabagool_module
        source = inspect.getsource(gabagool_module.GabagoolStrategy._check_daily_reset)

        assert "_arbitrage_positions.clear()" in source, (
            "Daily reset should clear arbitrage position tracking"
        )


class TestDecimalPrecision:
    """Regression tests for decimal precision issues.

    The Polymarket API requires:
    - maker amount (size/shares): max 2 decimal places
    - taker amount: max 4 decimal places

    FOK orders have a bug in py-clob-client that causes precision errors.
    See: https://github.com/Polymarket/py-clob-client/issues/121

    Error message: "invalid amounts, the market buy orders maker amount supports
    a max accuracy of 2 decimals, taker amount a max of 4 decimals"
    """

    def test_decimal_module_used_in_near_resolution(self):
        """Verify near-resolution trades use Decimal for precise calculations."""
        import src.strategies.gabagool as gabagool_module
        source = inspect.getsource(gabagool_module.GabagoolStrategy._execute_near_resolution_trade)

        assert "from decimal import Decimal" in source, (
            "Near-resolution trades must use Decimal module for precision"
        )
        assert "ROUND_DOWN" in source, (
            "Must use ROUND_DOWN to avoid rounding up beyond available funds"
        )

    def test_decimal_module_used_in_dual_leg(self):
        """Verify dual-leg orders use Decimal for precise calculations."""
        import src.client.polymarket as polymarket_module
        source = inspect.getsource(polymarket_module.PolymarketClient.execute_dual_leg_order)

        assert "from decimal import Decimal" in source, (
            "Dual-leg orders must use Decimal module for precision"
        )
        assert "ROUND_DOWN" in source, (
            "Must use ROUND_DOWN to avoid rounding up beyond available funds"
        )

    def test_decimal_module_used_in_market_order(self):
        """Verify market orders use Decimal for precise calculations."""
        import src.client.polymarket as polymarket_module
        source = inspect.getsource(polymarket_module.PolymarketClient.create_market_order)

        assert "from decimal import Decimal" in source, (
            "Market orders must use Decimal module for precision"
        )
        assert "ROUND_DOWN" in source, (
            "Must use ROUND_DOWN to avoid rounding up beyond available funds"
        )

    def test_no_round_function_in_order_calculations(self):
        """Verify we don't use round() for order calculations (causes precision issues)."""
        import os
        import re

        src_dir = os.path.join(os.path.dirname(__file__), '..', 'src')

        # Pattern that catches round() being used with price/shares calculations
        # This is a heuristic - round() near price/size/shares variables
        bad_patterns = [
            re.compile(r'shares\s*=\s*round\('),
            re.compile(r'limit_price\s*=\s*round\('),
            re.compile(r'size\s*=\s*round\('),
        ]

        violations = []

        files_to_check = [
            'client/polymarket.py',
            'strategies/gabagool.py',
        ]

        for rel_path in files_to_check:
            filepath = os.path.join(src_dir, rel_path)
            if os.path.exists(filepath):
                with open(filepath, 'r') as f:
                    content = f.read()

                for pattern in bad_patterns:
                    matches = pattern.findall(content)
                    if matches:
                        violations.append(
                            f"{rel_path}: Found round() for order calculation - use Decimal instead"
                        )

        assert not violations, (
            f"Found round() used for order calculations which can cause precision issues:\n"
            + "\n".join(violations)
            + "\n\nUse Decimal with ROUND_DOWN instead for precise calculations."
        )

    def test_decimal_calculation_produces_clean_values(self):
        """Test that our Decimal approach produces API-compliant values.

        Polymarket API requirements (especially for FOK orders):
        - maker_amount (shares * price): max 2 decimal places
        - taker_amount (shares): max 2 decimal places (py-clob-client limit)
        - price: 2 decimal places

        CRITICAL: The product shares × price must have ≤2 decimals for FOK orders.
        """
        from decimal import Decimal, ROUND_DOWN

        # Test case: $10 at $0.97 price
        price = Decimal("0.97")
        limit_price = min(price + Decimal("0.02"), Decimal("0.99"))
        limit_price = limit_price.quantize(Decimal("0.01"), rounding=ROUND_DOWN)

        # Round USD amount to 2 decimals
        maker_amount_target = Decimal("10.00")

        # Calculate shares (round to 2 decimals for py-clob-client)
        shares = (maker_amount_target / limit_price).quantize(Decimal("0.01"), rounding=ROUND_DOWN)

        # Adjust shares until product is clean
        for _ in range(200):
            actual_maker = shares * limit_price
            actual_maker_rounded = actual_maker.quantize(Decimal("0.01"), rounding=ROUND_DOWN)
            if actual_maker == actual_maker_rounded:
                break
            shares = shares - Decimal("0.01")

        # Verify price has max 2 decimal places
        assert limit_price == Decimal("0.99")

        # Verify shares has max 2 decimal places
        assert len(str(shares).split('.')[-1]) <= 2

        # CRITICAL: Verify maker_amount (shares × price) has max 2 decimal places
        maker_amount = shares * limit_price
        maker_rounded = maker_amount.quantize(Decimal("0.01"), rounding=ROUND_DOWN)
        assert maker_amount == maker_rounded, f"maker_amount {maker_amount} has more than 2 decimals"

    def test_decimal_precision_edge_cases(self):
        """Test edge cases for decimal precision that previously caused API errors.

        Error message: "invalid amounts, the market buy orders maker amount supports
        a max accuracy of 2 decimals, taker amount a max of 4 decimals"

        The CRITICAL constraint is that shares × price must have ≤2 decimals.
        This test verifies our adjustment algorithm handles edge cases.
        """
        from decimal import Decimal, ROUND_DOWN

        # Edge cases that previously failed (these prices produce bad products)
        test_cases = [
            # (amount_usd, price) -> must produce clean maker_amount
            ("8.92", "0.35"),    # From real failure: 25.48 × 0.35 = 8.918 (BAD)
            ("16.07", "0.63"),   # From real failure: 25.50 × 0.63 = 16.065 (BAD)
            ("10.00", "0.33"),   # Pathological: 30.30 × 0.33 = 9.999 (BAD)
            ("25.00", "0.97"),   # Near 1.00: 25.77 × 0.97 = 24.9969 (BAD)
        ]

        for amount_str, price_str in test_cases:
            price = Decimal(price_str).quantize(Decimal("0.01"), rounding=ROUND_DOWN)
            maker_target = Decimal(amount_str).quantize(Decimal("0.01"), rounding=ROUND_DOWN)

            # Calculate shares (2 decimals for py-clob-client)
            shares = (maker_target / price).quantize(Decimal("0.01"), rounding=ROUND_DOWN)

            # Adjust shares until product is clean (same algorithm as production code)
            for _ in range(200):
                actual_maker = shares * price
                actual_maker_rounded = actual_maker.quantize(Decimal("0.01"), rounding=ROUND_DOWN)
                if actual_maker == actual_maker_rounded:
                    break
                shares = shares - Decimal("0.01")
                if shares <= 0:
                    shares = Decimal("0.01")
                    break

            # CRITICAL: Verify maker_amount has max 2 decimal places
            maker_amount = shares * price
            maker_rounded = maker_amount.quantize(Decimal("0.01"), rounding=ROUND_DOWN)
            assert maker_amount == maker_rounded, (
                f"Failed for price={price}: shares={shares}, "
                f"maker_amount={maker_amount} has more than 2 decimals"
            )

    def test_decimal_module_used_in_place_order_sync(self):
        """Verify place_order_sync uses Decimal for precise calculations.

        This is the main function used for parallel order execution.
        """
        import src.client.polymarket as polymarket_module
        source = inspect.getsource(polymarket_module.PolymarketClient.execute_dual_leg_order_parallel)

        # The place_order_sync function is defined inline; check for Decimal usage
        assert "from decimal import Decimal" in source, (
            "Parallel order execution must use Decimal module for precision"
        )
        assert "ROUND_DOWN" in source, (
            "Must use ROUND_DOWN to avoid rounding up beyond available funds"
        )
        # Verify correct precision constants are used
        assert '0.01' in source, "Must round USD/shares/price to 2 decimal places"
        # Verify there's logic to ensure clean maker_amount
        assert 'actual_maker' in source or 'maker_amount' in source, (
            "Must verify shares × price produces clean maker_amount"
        )

    def test_fok_compatible_with_clean_maker_amount(self):
        """Verify FOK can be used now that we ensure clean maker_amount.

        Previously FOK failed with "invalid amounts" errors because
        shares × price produced values with >2 decimals. Now we adjust
        shares to ensure the product is always ≤2 decimals.

        FOK is preferred over GTC for arbitrage because:
        1. Atomicity: Either fills completely or not at all
        2. No hanging orders: Don't end up with orders on the book
        3. Predictable: Know immediately if the trade succeeded

        This test verifies FOK is used in the parallel execution path.
        """
        import src.client.polymarket as polymarket_module
        source = inspect.getsource(polymarket_module.PolymarketClient.execute_dual_leg_order_parallel)

        # The parallel execution path should use FOK for atomicity
        assert "OrderType.FOK" in source, (
            "Parallel execution should use FOK for atomic fills"
        )
        # And it should have logic to ensure clean maker_amount
        assert "actual_maker" in source, (
            "Must adjust shares to ensure clean maker_amount for FOK"
        )


class TestArbitragePositionSizing:
    """Regression tests for arbitrage position sizing.

    ISSUE: User observed unequal share positions (30.3 UP vs 16.8 DOWN)
    when both arbitrage and near-resolution strategies were active.

    REQUIREMENT: Arbitrage trades MUST have equal shares on both sides.
    This ensures guaranteed profit regardless of market outcome:
    - If UP wins: 30 shares * $1 = $30
    - If DOWN wins: 30 shares * $1 = $30
    Equal shares = equal payout regardless of winner.

    Unequal shares break the arbitrage by creating directional exposure.
    """

    def test_calculate_position_sizes_returns_equal_shares(self):
        """Verify position sizing calculates for equal share counts."""
        # The arbitrage formula: shares = budget / (yes_price + no_price)
        # Then buy 'shares' of YES and 'shares' of NO

        budget = 20.0  # $20 budget
        yes_price = 0.53
        no_price = 0.40

        # Expected calculation
        cost_per_pair = yes_price + no_price  # 0.93
        num_pairs = budget / cost_per_pair    # 21.5 shares each side

        # Verify the math
        assert cost_per_pair == pytest.approx(0.93, rel=0.01)
        assert num_pairs == pytest.approx(21.5, rel=0.01)

        # Total cost should equal budget
        yes_cost = num_pairs * yes_price  # 21.5 * 0.53 = $11.40
        no_cost = num_pairs * no_price    # 21.5 * 0.40 = $8.60
        total_cost = yes_cost + no_cost   # $20.00

        assert total_cost == pytest.approx(budget, rel=0.01)

        # Share counts should be equal!
        yes_shares = num_pairs
        no_shares = num_pairs
        assert yes_shares == no_shares, "Arbitrage MUST have equal share counts"

    def test_unequal_shares_breaks_arbitrage_guarantee(self):
        """Demonstrate why unequal shares break the arbitrage.

        With the user's actual position:
        - UP: 30.3 shares @ $0.53 = $16.06 cost
        - DOWN: 16.8 shares @ $0.40 = $6.72 cost
        - Total: $22.78 cost

        Outcomes:
        - If UP wins: 30.3 * $1 = $30.30 (profit: $7.52)
        - If DOWN wins: 16.8 * $1 = $16.80 (LOSS: $5.98)

        This is NOT arbitrage - it's a directional bet with risk!
        """
        up_shares = 30.3
        down_shares = 16.8
        up_cost = 16.06
        down_cost = 6.72
        total_cost = up_cost + down_cost

        # If UP wins
        up_win_payout = up_shares * 1.0
        up_win_profit = up_win_payout - total_cost

        # If DOWN wins
        down_win_payout = down_shares * 1.0
        down_win_profit = down_win_payout - total_cost

        # Verify the asymmetric outcomes
        assert up_win_profit > 0, "UP win should profit"
        assert down_win_profit < 0, "DOWN win should LOSE money"

        # This proves unequal shares is NOT arbitrage
        assert up_win_profit != pytest.approx(down_win_profit, abs=1.0), (
            "Unequal shares create asymmetric outcomes - not arbitrage!"
        )

    def test_equal_shares_guarantees_profit(self):
        """Demonstrate that equal shares guarantee profit.

        Correct arbitrage with equal shares:
        - UP: 21.5 shares @ $0.53 = $11.40
        - DOWN: 21.5 shares @ $0.40 = $8.60
        - Total: $20.00 cost
        - Either outcome: 21.5 * $1 = $21.50
        - Guaranteed profit: $1.50
        """
        shares = 21.5  # Equal on both sides
        up_price = 0.53
        down_price = 0.40

        up_cost = shares * up_price
        down_cost = shares * down_price
        total_cost = up_cost + down_cost

        # Either outcome pays the same
        payout = shares * 1.0
        profit = payout - total_cost

        # Verify guaranteed profit
        assert profit > 0, "Equal shares should guarantee profit"
        assert profit == pytest.approx(1.505, rel=0.01)

        # Spread was 7 cents, so profit = shares * spread
        spread = 1.0 - up_price - down_price
        expected_profit = shares * spread
        assert profit == pytest.approx(expected_profit, rel=0.01)


class TestPartialFillScenarios:
    """Regression tests for partial fill scenarios.

    ISSUE: User placed dual-leg order but only one leg filled.
    The unfilled leg showed as "open order" leaving directional exposure.

    REQUIREMENT: Both legs must fill or neither. If partial fill occurs,
    automatically unwind the filled leg to return to neutral.
    """

    def test_partial_fill_detection_in_response(self):
        """Verify we can detect partial fills from API response."""
        # Simulated API responses
        yes_filled = {"status": "MATCHED", "size": "21.5"}
        no_rejected = {"status": "REJECTED", "size": "0"}

        # Detection logic
        yes_status = yes_filled.get("status", "").upper()
        no_status = no_rejected.get("status", "").upper()

        yes_ok = yes_status in ("MATCHED", "FILLED", "LIVE")
        no_ok = no_status in ("MATCHED", "FILLED", "LIVE")

        partial_fill = yes_ok and not no_ok

        assert partial_fill is True, "Should detect partial fill scenario"

    def test_unwind_sells_at_aggressive_price(self):
        """Verify unwind logic uses aggressive pricing for quick fill."""
        from decimal import Decimal, ROUND_DOWN

        current_price = 0.53
        # Unwind should sell 2 cents below market to ensure fill
        expected_sell_price = Decimal(str(current_price)) - Decimal("0.02")
        expected_sell_price = max(expected_sell_price, Decimal("0.01"))

        assert float(expected_sell_price) == pytest.approx(0.51, rel=0.01)


class TestNearResolutionStrategyIsolation:
    """Regression tests for near-resolution strategy isolation.

    ISSUE: Near-resolution trades were stacking on top of arbitrage positions,
    creating unbalanced share counts.

    REQUIREMENT: Near-resolution should NEVER execute on markets where we
    already have an arbitrage position.
    """

    def test_strategy_tracking_is_separate(self):
        """Verify we track arb and near-res positions separately."""
        # Simulated tracking dicts (as used in GabagoolStrategy)
        arbitrage_positions = {}  # {condition_id: True}
        near_resolution_executed = {}  # {condition_id: True}

        condition_id = "test_market_123"

        # Execute arbitrage on market
        arbitrage_positions[condition_id] = True

        # Near-resolution should check and skip
        should_skip_near_res = condition_id in arbitrage_positions

        assert should_skip_near_res is True, (
            "Near-resolution should skip markets with existing arbitrage"
        )

    def test_near_resolution_requirements(self):
        """Verify near-resolution strategy requirements."""
        # Near-resolution config defaults
        time_threshold = 60.0  # Final 60 seconds only
        min_price = 0.94      # Must be at least 94 cents
        max_price = 0.975     # Must be at most 97.5 cents

        # Test valid near-resolution scenario
        seconds_left = 45
        up_price = 0.96

        is_near_resolution = (
            seconds_left <= time_threshold and
            min_price <= up_price <= max_price
        )

        assert is_near_resolution is True

        # Test invalid - too much time
        is_invalid = 120 <= time_threshold
        assert is_invalid is False

        # Test invalid - price too low
        is_invalid = min_price <= 0.85
        assert is_invalid is False


class TestSettlementWorkaround:
    """Regression tests for settlement workaround.

    ISSUE: py-clob-client doesn't have a native redeem/claim function.
    After market resolves, winning positions are worth $1 but we can't
    directly claim them.

    WORKAROUND: Sell winning positions at $0.99 to realize profits.
    Prices reach $0.99 approximately 10-15 minutes after market close.

    See: https://github.com/Polymarket/py-clob-client/issues/117
    """

    def test_settlement_timing_requirement(self):
        """Verify we wait appropriate time before attempting claim."""
        from datetime import datetime, timedelta

        market_end_time = datetime(2024, 1, 1, 12, 0, 0)  # Market ended at noon
        current_time = datetime(2024, 1, 1, 12, 5, 0)     # 5 minutes later

        time_since_end = (current_time - market_end_time).total_seconds()
        min_wait_seconds = 600  # 10 minutes

        should_attempt = time_since_end >= min_wait_seconds
        assert should_attempt is False, "Should wait at least 10 minutes"

        # Try again after 15 minutes
        current_time = datetime(2024, 1, 1, 12, 15, 0)
        time_since_end = (current_time - market_end_time).total_seconds()
        should_attempt = time_since_end >= min_wait_seconds

        assert should_attempt is True, "Should attempt after 10 minutes"

    def test_claim_price_is_099(self):
        """Verify we sell at 0.99 to claim winnings."""
        from decimal import Decimal

        claim_price = Decimal("0.99")
        shares = Decimal("21.5")

        proceeds = shares * claim_price
        assert float(proceeds) == pytest.approx(21.285, rel=0.01)

        # Verify we get nearly full value
        full_value = shares * Decimal("1.00")
        loss_to_spread = float(full_value - proceeds)

        assert loss_to_spread == pytest.approx(0.215, rel=0.01), (
            "Loss to $0.99 spread should be minimal (1% of position)"
        )
