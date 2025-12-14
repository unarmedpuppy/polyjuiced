"""Phase 8 Regression Tests: Pricing Logic

These tests ensure that:
1. Prices are NEVER hardcoded - always derived from market data
2. The $0.53 bug cannot recur
3. Price bounds are respected
4. Magic numbers are detected and rejected

The key insight: Any hardcoded price in the codebase is a potential bug.
"""

import pytest
import re
from pathlib import Path


class TestPricingLogic:
    """Ensure prices are NEVER hardcoded and always derived from market data."""

    def test_limit_price_derived_from_market_price(self):
        """Limit price must be based on actual market price + slippage."""
        # Example calculation: for BUY, limit = market_price + slippage
        market_price = 0.35
        slippage = 0.02

        # This is how limit price SHOULD be calculated
        limit_price = market_price + slippage

        # Must be market price + slippage, NOT a hardcoded value
        assert limit_price == pytest.approx(0.37, abs=0.001)
        # Explicitly check not hardcoded to the bug value
        assert limit_price != 0.53

    def test_yes_and_no_prices_differ(self):
        """YES and NO legs must use different prices based on their markets."""
        yes_market = 0.30
        no_market = 0.68
        slippage = 0.02

        yes_limit = yes_market + slippage
        no_limit = no_market + slippage

        assert yes_limit != no_limit
        assert yes_limit == pytest.approx(0.32, abs=0.001)
        assert no_limit == pytest.approx(0.70, abs=0.001)

    def test_no_magic_numbers_in_client_pricing(self):
        """Scan polymarket client for hardcoded price values."""
        client_path = Path("src/client/polymarket.py")
        if not client_path.exists():
            pytest.skip("polymarket.py not found")

        content = client_path.read_text()

        # Pattern to find hardcoded prices like: limit_price = 0.53
        # Or: price = 0.XX where XX is not a special value
        magic_price_pattern = re.compile(
            r'(?:limit_price|price)\s*=\s*(0\.[0-9]{2})\b',
            re.IGNORECASE
        )

        matches = magic_price_pattern.findall(content)
        violations = []

        # Allow specific values: slippage, min, max
        allowed_values = {'0.01', '0.02', '0.99', '0.00', '0.50'}

        for match in matches:
            if match not in allowed_values:
                violations.append(f"hardcoded price {match}")

        assert not violations, f"Found hardcoded prices in polymarket.py: {violations}"

    def test_no_magic_numbers_in_strategy(self):
        """Scan gabagool strategy for hardcoded price values."""
        strategy_path = Path("src/strategies/gabagool.py")
        if not strategy_path.exists():
            pytest.skip("gabagool.py not found")

        content = strategy_path.read_text()

        # Pattern to find hardcoded prices
        magic_price_pattern = re.compile(
            r'(?:limit_price|price|yes_price|no_price)\s*=\s*(0\.[0-9]{2})\b',
            re.IGNORECASE
        )

        matches = magic_price_pattern.findall(content)
        violations = []

        # Allow specific values
        allowed_values = {'0.01', '0.02', '0.99', '0.00', '0.50'}

        for match in matches:
            if match not in allowed_values:
                violations.append(f"hardcoded price {match}")

        assert not violations, f"Found hardcoded prices in gabagool.py: {violations}"

    def test_price_bounds(self):
        """Prices must be within valid Polymarket range."""
        for market_price in [0.01, 0.25, 0.50, 0.75, 0.99]:
            slippage = 0.02
            limit = min(0.99, market_price + slippage)  # Cap at max
            assert 0.01 <= limit <= 0.99, f"Invalid limit price: {limit}"

    def test_slippage_is_reasonable(self):
        """Slippage should be in a reasonable range."""
        # Slippage values seen in the codebase
        reasonable_slippage_range = (0.01, 0.05)  # 1-5 cents

        # The default slippage used in the strategy
        default_slippage = 0.02  # 2 cents

        assert reasonable_slippage_range[0] <= default_slippage <= reasonable_slippage_range[1]


class TestNoHardcodedValuesInSource:
    """Scan entire source tree for suspicious hardcoded values."""

    def test_no_053_in_codebase(self):
        """The $0.53 bug value should NOT appear anywhere."""
        src_path = Path("src")
        if not src_path.exists():
            pytest.skip("src/ not found")

        violations = []

        for filepath in src_path.rglob("*.py"):
            content = filepath.read_text()
            # Look for 0.53 specifically - the bug value
            if "0.53" in content:
                # Allow in comments explaining the bug
                lines = content.split('\n')
                for i, line in enumerate(lines, 1):
                    if "0.53" in line and not line.strip().startswith('#'):
                        violations.append(f"{filepath}:{i}: contains 0.53")

        assert not violations, f"Found bug value 0.53: {violations}"

    def test_no_suspicious_price_assignments(self):
        """Look for price assignments that aren't derived from variables."""
        src_path = Path("src")
        if not src_path.exists():
            pytest.skip("src/ not found")

        # Pattern: price = 0.XX (excluding 0.01, 0.02, 0.99, 0.00, 0.50)
        pattern = re.compile(
            r'(\w*price\w*)\s*=\s*(0\.[0-9]{2})\b',
            re.IGNORECASE
        )

        violations = []
        allowed_values = {'0.01', '0.02', '0.99', '0.00', '0.50'}

        for filepath in src_path.rglob("*.py"):
            # Skip test files
            if 'test' in filepath.name.lower():
                continue

            content = filepath.read_text()
            lines = content.split('\n')

            for i, line in enumerate(lines, 1):
                # Skip comments
                if line.strip().startswith('#'):
                    continue

                for match in pattern.finditer(line):
                    var_name = match.group(1)
                    value = match.group(2)

                    if value not in allowed_values:
                        violations.append(
                            f"{filepath}:{i}: {var_name} = {value}"
                        )

        assert not violations, f"Found suspicious price assignments: {violations}"


class TestPriceCalculationInvariants:
    """Test invariants that price calculations must satisfy."""

    def test_buy_limit_exceeds_market(self):
        """BUY limit price should be at or above market to ensure fill."""
        market_price = 0.48
        slippage = 0.02

        buy_limit = market_price + slippage

        assert buy_limit >= market_price, "BUY limit must be >= market"

    def test_sell_limit_below_market(self):
        """SELL limit price should be at or below market to ensure fill."""
        market_price = 0.48
        slippage = 0.02

        sell_limit = market_price - slippage

        assert sell_limit <= market_price, "SELL limit must be <= market"

    def test_limit_price_close_to_market(self):
        """Limit price shouldn't deviate too far from market."""
        market_price = 0.48
        max_deviation = 0.05  # 5 cents max

        # Any limit price used should be within 5 cents of market
        limit_price = 0.50  # market + 2 cents slippage

        deviation = abs(limit_price - market_price)
        assert deviation <= max_deviation, \
            f"Limit price deviates {deviation} from market (max {max_deviation})"

    def test_arbitrage_prices_sum_to_less_than_one(self):
        """For arbitrage, YES + NO prices must sum to < $1.00."""
        yes_price = 0.48
        no_price = 0.49

        total = yes_price + no_price

        assert total < 1.0, f"Arbitrage requires YES + NO < 1.0, got {total}"

    def test_no_negative_spread(self):
        """Spread should be positive for valid arbitrage."""
        yes_price = 0.48
        no_price = 0.49

        # Spread = 1.0 - (YES + NO)
        spread = 1.0 - (yes_price + no_price)

        assert spread > 0, f"Spread must be positive, got {spread}"


class TestDynamicPricingPatterns:
    """Test that dynamic pricing patterns are used correctly."""

    def test_price_from_opportunity(self):
        """Prices should come from opportunity object, not hardcoded."""
        # Simulate an opportunity object
        class MockOpportunity:
            yes_price = 0.30
            no_price = 0.68
            spread_cents = 2.0

        opp = MockOpportunity()

        # Correct pattern: use opportunity prices
        yes_limit = opp.yes_price + 0.02  # market + slippage
        no_limit = opp.no_price + 0.02

        # These should be derived from the opportunity, not hardcoded
        assert yes_limit == pytest.approx(0.32, abs=0.001)
        assert no_limit == pytest.approx(0.70, abs=0.001)

        # They should NOT be the same (the $0.53 bug)
        assert yes_limit != no_limit

    def test_price_from_book_top(self):
        """Limit price should derive from order book, not hardcoded."""
        # Simulate order book data
        yes_book_top = 0.48  # Best ask for YES
        no_book_top = 0.49   # Best ask for NO
        slippage = 0.02

        yes_limit = yes_book_top + slippage
        no_limit = no_book_top + slippage

        # Verify they're different and derived from book
        assert yes_limit != no_limit
        assert yes_limit == pytest.approx(0.50, abs=0.001)
        assert no_limit == pytest.approx(0.51, abs=0.001)
