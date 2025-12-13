"""Tests for Polymarket order API usage.

These tests verify that we're using the py-clob-client library correctly,
particularly around order placement and order types (FOK, GTC, etc.).

Regression test for: OrderArgs.__init__() got an unexpected keyword argument 'order_type'
The order_type parameter must be passed to post_order(), not OrderArgs().

Note: We use GTC instead of FOK due to decimal precision bugs in py-clob-client.
See: https://github.com/Polymarket/py-clob-client/issues/121
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
        """Test that our Decimal approach produces clean 2-decimal values."""
        from decimal import Decimal, ROUND_DOWN

        # Test case that was failing: $10 at $0.97 price
        trade_size = Decimal("10.0")
        price = Decimal("0.97")

        # Calculate limit price (add 2 cents, cap at 0.99)
        limit_price = min(price + Decimal("0.02"), Decimal("0.99"))
        limit_price = limit_price.quantize(Decimal("0.01"), rounding=ROUND_DOWN)

        # Calculate shares
        shares = (trade_size / limit_price).quantize(Decimal("0.01"), rounding=ROUND_DOWN)

        # Verify values have max 2 decimal places
        assert limit_price == Decimal("0.99")
        assert shares == Decimal("10.10")

        # Verify the product (maker amount) is clean
        maker_amount = shares * limit_price
        assert maker_amount == Decimal("9.9990")  # 4 decimal places max

        # Convert to float and verify no floating point issues
        price_float = float(limit_price)
        shares_float = float(shares)

        assert f"{price_float:.2f}" == "0.99"
        assert f"{shares_float:.2f}" == "10.10"

    def test_gtc_used_instead_of_fok(self):
        """Verify GTC is used instead of FOK due to precision bugs."""
        import os

        src_dir = os.path.join(os.path.dirname(__file__), '..', 'src')

        files_to_check = [
            'client/polymarket.py',
            'strategies/gabagool.py',
        ]

        for rel_path in files_to_check:
            filepath = os.path.join(src_dir, rel_path)
            if os.path.exists(filepath):
                with open(filepath, 'r') as f:
                    content = f.read()

                # Should NOT have FOK in post_order calls (except in comments)
                # Count actual FOK usage in code (not comments)
                lines = content.split('\n')
                fok_in_code = 0
                for line in lines:
                    # Skip comments
                    code_part = line.split('#')[0]
                    if 'OrderType.FOK' in code_part and 'post_order' in code_part:
                        fok_in_code += 1

                assert fok_in_code == 0, (
                    f"{rel_path} should use GTC instead of FOK in post_order calls"
                )
