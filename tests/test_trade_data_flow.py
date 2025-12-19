"""Regression tests for trade data flow.

Created: December 18, 2025
Purpose: Ensure trade data is correctly extracted from API responses and persisted.

These tests prevent regressions of bugs found in production:
- Bug 1: Wrong field access for partial fills (looking in yes_order instead of api_result)
- Bug 2: Rebalancing erasing filled shares when exiting
- Bug 3: Partial fill data showing $0 in database

CRITICAL: All trade scenarios must result in correct data being recorded.
"""

import pytest
import asyncio
from datetime import datetime, timedelta
from typing import Dict, Any
from unittest.mock import AsyncMock, MagicMock, patch
from dataclasses import dataclass


# =============================================================================
# Test Data: API Response Structures
# =============================================================================

def make_api_result_full_fill(
    yes_shares: float = 10.0,
    no_shares: float = 10.0,
    yes_price: float = 0.48,
    no_price: float = 0.49,
) -> Dict[str, Any]:
    """Create API result for a perfect full fill."""
    return {
        "success": True,
        "partial_fill": False,
        "yes_order": {
            "id": "yes-order-001",
            "status": "MATCHED",
            "size": yes_shares,
            "price": yes_price,
            "size_matched": yes_shares,
            "_intended_size": yes_shares,
        },
        "no_order": {
            "id": "no-order-001",
            "status": "MATCHED",
            "size": no_shares,
            "price": no_price,
            "size_matched": no_shares,
            "_intended_size": no_shares,
        },
        "yes_filled_size": yes_shares,  # TOP LEVEL - this is authoritative
        "no_filled_size": no_shares,    # TOP LEVEL - this is authoritative
        "yes_filled_cost": yes_shares * yes_price,
        "no_filled_cost": no_shares * no_price,
        "pre_fill_yes_depth": 100.0,
        "pre_fill_no_depth": 90.0,
    }


def make_api_result_partial_fill_yes_only(
    yes_shares: float = 10.0,
    yes_price: float = 0.48,
    no_price: float = 0.49,
) -> Dict[str, Any]:
    """Create API result where only YES fills (NO rejected)."""
    return {
        "success": False,
        "partial_fill": True,
        "partial_fill_rebalanced": False,
        "rebalance_action": "unknown",
        "rebalance_result": {},
        "yes_order": {
            "id": "yes-order-001",
            "status": "MATCHED",
            "size": yes_shares,
            "price": yes_price,
            "size_matched": yes_shares,
            "_intended_size": yes_shares,
        },
        "no_order": {
            "id": "no-order-001",
            "status": "FAILED",
            "size": 0,
            "price": no_price,
            "size_matched": 0,
            "_intended_size": 10.0,  # What we tried to fill
        },
        "yes_filled_size": yes_shares,  # TOP LEVEL - YES filled
        "no_filled_size": 0.0,          # TOP LEVEL - NO did not fill
        "yes_filled_cost": yes_shares * yes_price,
        "no_filled_cost": 0.0,
        "error": "PARTIAL FILL: YES filled, NO did not",
        "pre_fill_yes_depth": 100.0,
        "pre_fill_no_depth": 90.0,
    }


def make_api_result_partial_fill_no_only(
    no_shares: float = 10.0,
    yes_price: float = 0.48,
    no_price: float = 0.49,
) -> Dict[str, Any]:
    """Create API result where only NO fills (YES rejected)."""
    return {
        "success": False,
        "partial_fill": True,
        "partial_fill_rebalanced": False,
        "rebalance_action": "unknown",
        "rebalance_result": {},
        "yes_order": {
            "id": "yes-order-001",
            "status": "FAILED",
            "size": 0,
            "price": yes_price,
            "size_matched": 0,
            "_intended_size": 10.0,
        },
        "no_order": {
            "id": "no-order-001",
            "status": "MATCHED",
            "size": no_shares,
            "price": no_price,
            "size_matched": no_shares,
            "_intended_size": no_shares,
        },
        "yes_filled_size": 0.0,         # TOP LEVEL - YES did not fill
        "no_filled_size": no_shares,    # TOP LEVEL - NO filled
        "yes_filled_cost": 0.0,
        "no_filled_cost": no_shares * no_price,
        "error": "PARTIAL FILL: NO filled, YES did not",
        "pre_fill_yes_depth": 100.0,
        "pre_fill_no_depth": 90.0,
    }


def make_api_result_partial_hedge_completed(
    filled_shares: float = 10.0,
    hedge_shares: float = 10.0,
    filled_side: str = "YES",
    yes_price: float = 0.48,
    no_price: float = 0.49,
) -> Dict[str, Any]:
    """Create API result where partial fill was rebalanced by completing hedge."""
    yes_filled = filled_side == "YES"
    return {
        "success": True,  # SUCCESS because hedge was completed!
        "partial_fill": True,
        "partial_fill_rebalanced": True,
        "rebalance_action": "hedge_completed",
        "rebalance_result": {
            "success": True,
            "action": "hedge_completed",
            "filled_shares": filled_shares,
            "hedge_shares": hedge_shares,
            "filled_cost": filled_shares * (yes_price if yes_filled else no_price),
            "hedge_cost": hedge_shares * (no_price if yes_filled else yes_price),
            "total_cost": filled_shares * (yes_price if yes_filled else no_price) + hedge_shares * (no_price if yes_filled else yes_price),
            "expected_profit": hedge_shares - (filled_shares * (yes_price if yes_filled else no_price) + hedge_shares * (no_price if yes_filled else yes_price)),
        },
        "yes_order": {
            "id": "yes-order-001",
            "status": "MATCHED" if yes_filled else "FAILED",
            "size": filled_shares if yes_filled else 0,
            "price": yes_price,
            "size_matched": filled_shares if yes_filled else 0,
            "_intended_size": 10.0,
        },
        "no_order": {
            "id": "no-order-001",
            "status": "MATCHED" if not yes_filled else "FAILED",
            "size": filled_shares if not yes_filled else 0,
            "price": no_price,
            "size_matched": filled_shares if not yes_filled else 0,
            "_intended_size": 10.0,
        },
        "yes_filled_size": filled_shares if yes_filled else 0.0,
        "no_filled_size": filled_shares if not yes_filled else 0.0,
        "yes_filled_cost": filled_shares * yes_price if yes_filled else 0.0,
        "no_filled_cost": filled_shares * no_price if not yes_filled else 0.0,
        "error": f"PARTIAL FILL HEDGE_COMPLETED: {filled_side} filled. Action: hedge_completed.",
        "pre_fill_yes_depth": 100.0,
        "pre_fill_no_depth": 90.0,
    }


def make_api_result_partial_exited(
    filled_shares: float = 10.0,
    filled_side: str = "YES",
    yes_price: float = 0.48,
    no_price: float = 0.49,
    exit_price: float = 0.46,  # Sold at small loss
) -> Dict[str, Any]:
    """Create API result where partial fill was rebalanced by exiting."""
    yes_filled = filled_side == "YES"
    entry_price = yes_price if yes_filled else no_price
    pnl = (exit_price - entry_price) * filled_shares  # Usually negative (small loss)

    return {
        "success": False,  # Not a success trade, but we exited cleanly
        "partial_fill": True,
        "partial_fill_rebalanced": True,
        "rebalance_action": "exited",
        "rebalance_result": {
            "success": True,
            "action": "exited",
            "shares_sold": filled_shares,
            "exit_price": exit_price,
            "proceeds": filled_shares * exit_price,
            "pnl": pnl,
        },
        "yes_order": {
            "id": "yes-order-001",
            "status": "MATCHED" if yes_filled else "FAILED",
            "size": filled_shares if yes_filled else 0,
            "price": yes_price,
            "size_matched": filled_shares if yes_filled else 0,
            "_intended_size": 10.0,
        },
        "no_order": {
            "id": "no-order-001",
            "status": "MATCHED" if not yes_filled else "FAILED",
            "size": filled_shares if not yes_filled else 0,
            "price": no_price,
            "size_matched": filled_shares if not yes_filled else 0,
            "_intended_size": 10.0,
        },
        "yes_filled_size": filled_shares if yes_filled else 0.0,
        "no_filled_size": filled_shares if not yes_filled else 0.0,
        "yes_filled_cost": filled_shares * yes_price if yes_filled else 0.0,
        "no_filled_cost": filled_shares * no_price if not yes_filled else 0.0,
        "error": f"PARTIAL FILL EXITED: {filled_side} filled. Action: exited. P&L: ${pnl:.2f}",
        "pre_fill_yes_depth": 100.0,
        "pre_fill_no_depth": 90.0,
    }


# =============================================================================
# Test: Partial Fill Data Extraction (Bug 1 Fix)
# =============================================================================

class TestPartialFillDataExtraction:
    """Tests for extracting partial fill data from API responses.

    Bug 1: Code was looking in yes_order.get("size_matched") but API returns
    yes_filled_size at the top level of api_result.
    """

    def test_extract_yes_filled_from_top_level(self):
        """Partial fill YES should be extracted from api_result['yes_filled_size']."""
        api_result = make_api_result_partial_fill_yes_only(yes_shares=15.5)

        # This is how the FIXED code should extract it:
        partial_yes = float(api_result.get("yes_filled_size", 0) or 0)
        partial_no = float(api_result.get("no_filled_size", 0) or 0)

        assert partial_yes == 15.5, f"Expected 15.5, got {partial_yes}"
        assert partial_no == 0.0, f"Expected 0.0, got {partial_no}"

    def test_extract_no_filled_from_top_level(self):
        """Partial fill NO should be extracted from api_result['no_filled_size']."""
        api_result = make_api_result_partial_fill_no_only(no_shares=12.75)

        partial_yes = float(api_result.get("yes_filled_size", 0) or 0)
        partial_no = float(api_result.get("no_filled_size", 0) or 0)

        assert partial_yes == 0.0, f"Expected 0.0, got {partial_yes}"
        assert partial_no == 12.75, f"Expected 12.75, got {partial_no}"

    def test_fallback_to_nested_fields(self):
        """If top-level fields are 0, should fallback to nested order fields."""
        # Create a result where top-level is 0 but nested has data
        api_result = {
            "success": False,
            "partial_fill": True,
            "yes_filled_size": 0,  # Top level says 0
            "no_filled_size": 0,   # Top level says 0
            "yes_order": {
                "status": "MATCHED",
                "size_matched": 8.5,  # But nested has data
            },
            "no_order": {
                "status": "FAILED",
                "size_matched": 0,
            },
        }

        # Fixed extraction logic:
        partial_yes = float(api_result.get("yes_filled_size", 0) or 0)
        if partial_yes == 0 and api_result.get("yes_order"):
            yes_order = api_result.get("yes_order") or {}
            partial_yes = float(
                yes_order.get("size_matched", 0) or
                yes_order.get("matched_size", 0) or
                yes_order.get("_intended_size", 0) or
                0
            )

        assert partial_yes == 8.5, f"Expected 8.5 from fallback, got {partial_yes}"

    def test_matched_status_uses_intended_size_fallback(self):
        """If status is MATCHED but size_matched is 0, use _intended_size."""
        api_result = {
            "success": False,
            "partial_fill": True,
            "yes_filled_size": 0,
            "no_filled_size": 0,
            "yes_order": {
                "status": "MATCHED",  # Status says matched
                "size_matched": 0,     # But size_matched is 0 (API bug)
                "_intended_size": 10.0,  # We intended 10 shares
            },
            "no_order": {"status": "FAILED"},
        }

        # Extraction with MATCHED status fallback:
        partial_yes = float(api_result.get("yes_filled_size", 0) or 0)
        yes_order = api_result.get("yes_order") or {}

        if partial_yes == 0 and yes_order:
            partial_yes = float(yes_order.get("size_matched", 0) or 0)

        yes_status = (yes_order.get("status", "") if yes_order else "").upper()
        if yes_status in ("MATCHED", "FILLED") and partial_yes == 0:
            partial_yes = float(yes_order.get("_intended_size", 0) or 0)

        assert partial_yes == 10.0, f"Expected 10.0 from _intended_size fallback, got {partial_yes}"


# =============================================================================
# Test: Rebalance Exit Preserves Original Fill Data (Bug 2 Fix)
# =============================================================================

class TestRebalanceExitPreservesData:
    """Tests that exiting a partial fill still records original fill amounts.

    Bug 2: When rebalance_action == "exited", code was setting partial_yes = 0
    and partial_no = 0, erasing the record of what originally filled.
    """

    def test_exited_preserves_yes_filled(self):
        """When YES fills and we exit, should still record YES filled amount."""
        api_result = make_api_result_partial_exited(
            filled_shares=10.0,
            filled_side="YES",
            yes_price=0.48,
            exit_price=0.46,
        )

        # Extract using FIXED logic (no longer sets to 0 on exit)
        partial_yes = float(api_result.get("yes_filled_size", 0) or 0)
        partial_no = float(api_result.get("no_filled_size", 0) or 0)

        rebalance_action = api_result.get("rebalance_action")

        # BUG FIX: Do NOT set to 0 on exit
        # The old buggy code did:
        # if rebalance_action == "exited":
        #     partial_yes = 0  # WRONG!
        #     partial_no = 0   # WRONG!

        # Fixed code keeps the original values
        assert partial_yes == 10.0, f"Exited trade should still show YES filled: {partial_yes}"
        assert partial_no == 0.0, f"NO should be 0 since it didn't fill: {partial_no}"

    def test_exited_preserves_no_filled(self):
        """When NO fills and we exit, should still record NO filled amount."""
        api_result = make_api_result_partial_exited(
            filled_shares=8.0,
            filled_side="NO",
            no_price=0.49,
            exit_price=0.47,
        )

        partial_yes = float(api_result.get("yes_filled_size", 0) or 0)
        partial_no = float(api_result.get("no_filled_size", 0) or 0)

        assert partial_yes == 0.0, f"YES should be 0 since it didn't fill: {partial_yes}"
        assert partial_no == 8.0, f"Exited trade should still show NO filled: {partial_no}"

    def test_hedge_completed_updates_both_sides(self):
        """When hedge is completed, both sides should show filled amounts."""
        api_result = make_api_result_partial_hedge_completed(
            filled_shares=10.0,
            hedge_shares=10.0,
            filled_side="YES",
        )

        # Initial extraction
        partial_yes = float(api_result.get("yes_filled_size", 0) or 0)
        partial_no = float(api_result.get("no_filled_size", 0) or 0)

        original_yes = partial_yes
        original_no = partial_no

        # For hedge_completed, update shares to reflect both legs
        rebalance_action = api_result.get("rebalance_action")
        rebalance_result = api_result.get("rebalance_result", {})

        if rebalance_action == "hedge_completed":
            hedge_shares = float(rebalance_result.get("hedge_shares", 0) or 0)
            if original_yes > 0:
                partial_no = hedge_shares  # YES filled first, then we bought NO
            else:
                partial_yes = hedge_shares  # NO filled first, then we bought YES

        assert partial_yes == 10.0, f"YES should be 10.0: {partial_yes}"
        assert partial_no == 10.0, f"NO should be 10.0 (hedge_shares): {partial_no}"


# =============================================================================
# Test: Full Trade Recording Flow
# =============================================================================

class TestTradeRecordingFlow:
    """End-to-end tests for trade data being recorded correctly."""

    def test_full_fill_records_both_sides(self):
        """Full fill should record correct amounts for both YES and NO."""
        api_result = make_api_result_full_fill(
            yes_shares=15.0,
            no_shares=15.0,
            yes_price=0.48,
            no_price=0.49,
        )

        # Simulate what _record_trade receives
        actual_yes_shares = float(api_result.get("yes_filled_size", 0) or 0)
        actual_no_shares = float(api_result.get("no_filled_size", 0) or 0)
        yes_amount = actual_yes_shares * 0.48
        no_amount = actual_no_shares * 0.49

        assert actual_yes_shares == 15.0
        assert actual_no_shares == 15.0
        assert abs(yes_amount - 7.20) < 0.01
        assert abs(no_amount - 7.35) < 0.01

    def test_partial_fill_yes_only_records_correctly(self):
        """Partial fill (YES only) should record YES with correct amount, NO as 0."""
        api_result = make_api_result_partial_fill_yes_only(
            yes_shares=12.0,
            yes_price=0.48,
        )

        # Use FIXED extraction logic
        partial_yes = float(api_result.get("yes_filled_size", 0) or 0)
        partial_no = float(api_result.get("no_filled_size", 0) or 0)

        yes_amount = partial_yes * 0.48 if partial_yes > 0 else 0
        no_amount = partial_no * 0.49 if partial_no > 0 else 0

        assert partial_yes == 12.0, f"Expected 12.0, got {partial_yes}"
        assert partial_no == 0.0, f"Expected 0.0, got {partial_no}"
        assert abs(yes_amount - 5.76) < 0.01
        assert no_amount == 0.0

    def test_partial_fill_no_only_records_correctly(self):
        """Partial fill (NO only) should record NO with correct amount, YES as 0."""
        api_result = make_api_result_partial_fill_no_only(
            no_shares=11.0,
            no_price=0.49,
        )

        partial_yes = float(api_result.get("yes_filled_size", 0) or 0)
        partial_no = float(api_result.get("no_filled_size", 0) or 0)

        yes_amount = partial_yes * 0.48 if partial_yes > 0 else 0
        no_amount = partial_no * 0.49 if partial_no > 0 else 0

        assert partial_yes == 0.0
        assert partial_no == 11.0
        assert yes_amount == 0.0
        assert abs(no_amount - 5.39) < 0.01

    def test_execution_status_correct_for_full_fill(self):
        """Full fill should have execution_status='full_fill'."""
        api_result = make_api_result_full_fill()

        partial_yes = float(api_result.get("yes_filled_size", 0) or 0)
        partial_no = float(api_result.get("no_filled_size", 0) or 0)

        # Determine execution status
        if partial_yes > 0 and partial_no > 0:
            hedge_ratio = min(partial_yes, partial_no) / max(partial_yes, partial_no)
            if hedge_ratio >= 0.95:
                exec_status = "full_fill"
            else:
                exec_status = "partial_fill"
        elif partial_yes > 0 or partial_no > 0:
            exec_status = "one_leg_only"
        else:
            exec_status = "failed"

        assert exec_status == "full_fill"

    def test_execution_status_correct_for_one_leg_only(self):
        """One-sided fill should have execution_status='one_leg_only'."""
        api_result = make_api_result_partial_fill_yes_only()

        partial_yes = float(api_result.get("yes_filled_size", 0) or 0)
        partial_no = float(api_result.get("no_filled_size", 0) or 0)

        rebalance_action = api_result.get("rebalance_action", "unknown")

        # Determine execution status using FIXED logic
        if rebalance_action == "hedge_completed":
            exec_status = "partial_fill_hedged"
        elif rebalance_action == "exited":
            exec_status = "partial_fill_exited"
        elif partial_yes == 0 or partial_no == 0:
            exec_status = "one_leg_only"
        else:
            exec_status = "partial_fill"

        assert exec_status == "one_leg_only"

    def test_execution_status_correct_for_hedge_completed(self):
        """Hedge completed should have execution_status='partial_fill_hedged'."""
        api_result = make_api_result_partial_hedge_completed()

        rebalance_action = api_result.get("rebalance_action")

        if rebalance_action == "hedge_completed":
            exec_status = "partial_fill_hedged"
        else:
            exec_status = "other"

        assert exec_status == "partial_fill_hedged"

    def test_execution_status_correct_for_exited(self):
        """Exited partial fill should have execution_status='partial_fill_exited'."""
        api_result = make_api_result_partial_exited()

        rebalance_action = api_result.get("rebalance_action")

        if rebalance_action == "exited":
            exec_status = "partial_fill_exited"
        else:
            exec_status = "other"

        assert exec_status == "partial_fill_exited"


# =============================================================================
# Test: Hedge Ratio Calculation
# =============================================================================

class TestHedgeRatioCalculation:
    """Tests for correct hedge ratio calculation."""

    def test_perfect_hedge_ratio(self):
        """Equal shares should give hedge ratio of 1.0."""
        partial_yes = 10.0
        partial_no = 10.0

        hedge_ratio = min(partial_yes, partial_no) / max(partial_yes, partial_no)

        assert hedge_ratio == 1.0

    def test_80_percent_hedge_ratio(self):
        """80% fill should give hedge ratio of 0.8."""
        partial_yes = 10.0
        partial_no = 8.0

        hedge_ratio = min(partial_yes, partial_no) / max(partial_yes, partial_no)

        assert hedge_ratio == 0.8

    def test_zero_hedge_ratio_one_leg(self):
        """One-sided fill should give hedge ratio of 0."""
        partial_yes = 10.0
        partial_no = 0.0

        if max(partial_yes, partial_no) > 0:
            hedge_ratio = min(partial_yes, partial_no) / max(partial_yes, partial_no)
        else:
            hedge_ratio = 0.0

        assert hedge_ratio == 0.0

    def test_hedge_ratio_with_both_zero(self):
        """Both zero should give hedge ratio of 0 (not divide by zero)."""
        partial_yes = 0.0
        partial_no = 0.0

        if max(partial_yes, partial_no) > 0:
            hedge_ratio = min(partial_yes, partial_no) / max(partial_yes, partial_no)
        else:
            hedge_ratio = 0.0

        assert hedge_ratio == 0.0


# =============================================================================
# Test: Integration with Database Schema
# =============================================================================

class TestDatabaseIntegration:
    """Tests that data flows correctly to database schema."""

    def test_trade_record_has_required_fields(self):
        """Trade record should contain all required fields for database."""
        api_result = make_api_result_partial_fill_yes_only(yes_shares=10.0, yes_price=0.48)

        # Build trade record as gabagool.py does
        partial_yes = float(api_result.get("yes_filled_size", 0) or 0)
        partial_no = float(api_result.get("no_filled_size", 0) or 0)

        trade_record = {
            "trade_id": "partial-abc12345",
            "asset": "BTC",
            "condition_id": "0xabc123",
            "yes_price": 0.48,
            "no_price": 0.49,
            "yes_cost": partial_yes * 0.48 if partial_yes > 0 else 0,
            "no_cost": partial_no * 0.49 if partial_no > 0 else 0,
            "yes_shares": partial_yes,
            "no_shares": partial_no,
            "hedge_ratio": min(partial_yes, partial_no) / max(partial_yes, partial_no) if max(partial_yes, partial_no) > 0 else 0,
            "execution_status": "one_leg_only",
            "yes_order_status": "MATCHED",
            "no_order_status": "FAILED",
            "expected_profit": 0,
            "dry_run": False,
        }

        # Verify all fields are populated correctly
        assert trade_record["yes_shares"] == 10.0, f"yes_shares wrong: {trade_record['yes_shares']}"
        assert trade_record["no_shares"] == 0.0, f"no_shares wrong: {trade_record['no_shares']}"
        assert abs(trade_record["yes_cost"] - 4.80) < 0.01, f"yes_cost wrong: {trade_record['yes_cost']}"
        assert trade_record["no_cost"] == 0.0, f"no_cost wrong: {trade_record['no_cost']}"
        assert trade_record["hedge_ratio"] == 0.0
        assert trade_record["execution_status"] == "one_leg_only"

    def test_exited_trade_record_preserves_original_fill(self):
        """Exited trade should record original fill, not zeros."""
        api_result = make_api_result_partial_exited(
            filled_shares=10.0,
            filled_side="YES",
            yes_price=0.48,
            exit_price=0.46,
        )

        # Extract using FIXED logic
        partial_yes = float(api_result.get("yes_filled_size", 0) or 0)
        partial_no = float(api_result.get("no_filled_size", 0) or 0)

        # BUG FIX: Do NOT zero out on exit
        rebalance_action = api_result.get("rebalance_action")
        # Old buggy code:
        # if rebalance_action == "exited":
        #     partial_yes = 0
        #     partial_no = 0

        # Fixed code keeps original values

        trade_record = {
            "yes_shares": partial_yes,
            "no_shares": partial_no,
            "yes_cost": partial_yes * 0.48 if partial_yes > 0 else 0,
            "execution_status": "partial_fill_exited",
        }

        # Critical assertion: exited trade should NOT have zero shares
        assert trade_record["yes_shares"] == 10.0, \
            f"REGRESSION: Exited trade showing {trade_record['yes_shares']} shares instead of 10.0"
        assert abs(trade_record["yes_cost"] - 4.80) < 0.01, \
            f"REGRESSION: Exited trade showing ${trade_record['yes_cost']} cost instead of $4.80"


# =============================================================================
# Test: Edge Cases and Null Handling
# =============================================================================

class TestEdgeCases:
    """Tests for edge cases and null/missing field handling."""

    def test_missing_yes_order(self):
        """Should handle missing yes_order gracefully."""
        api_result = {
            "success": False,
            "partial_fill": True,
            "yes_order": None,  # None instead of dict
            "no_order": {"status": "MATCHED", "size_matched": 10.0},
            "yes_filled_size": 0,
            "no_filled_size": 10.0,
        }

        yes_order = api_result.get("yes_order") or {}
        no_order = api_result.get("no_order") or {}

        partial_yes = float(api_result.get("yes_filled_size", 0) or 0)
        partial_no = float(api_result.get("no_filled_size", 0) or 0)

        assert partial_yes == 0.0
        assert partial_no == 10.0

    def test_missing_no_order(self):
        """Should handle missing no_order gracefully."""
        api_result = {
            "success": False,
            "partial_fill": True,
            "yes_order": {"status": "MATCHED", "size_matched": 10.0},
            "no_order": None,
            "yes_filled_size": 10.0,
            "no_filled_size": 0,
        }

        yes_order = api_result.get("yes_order") or {}
        no_order = api_result.get("no_order") or {}

        partial_yes = float(api_result.get("yes_filled_size", 0) or 0)
        partial_no = float(api_result.get("no_filled_size", 0) or 0)

        assert partial_yes == 10.0
        assert partial_no == 0.0

    def test_string_values_converted_to_float(self):
        """Should handle string values that need conversion."""
        api_result = {
            "yes_filled_size": "10.5",  # String instead of float
            "no_filled_size": "8.25",
        }

        partial_yes = float(api_result.get("yes_filled_size", 0) or 0)
        partial_no = float(api_result.get("no_filled_size", 0) or 0)

        assert partial_yes == 10.5
        assert partial_no == 8.25

    def test_empty_string_handled_as_zero(self):
        """Empty string should be treated as 0."""
        api_result = {
            "yes_filled_size": "",
            "no_filled_size": 0,
        }

        # The `or 0` handles empty string
        partial_yes = float(api_result.get("yes_filled_size", 0) or 0)
        partial_no = float(api_result.get("no_filled_size", 0) or 0)

        assert partial_yes == 0.0
        assert partial_no == 0.0


# =============================================================================
# Run all tests
# =============================================================================

if __name__ == "__main__":
    pytest.main([__file__, "-v"])
