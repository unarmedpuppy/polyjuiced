"""Unit tests for parallel validation between Mercury and polyjuiced.

These tests verify that the validation framework correctly identifies
matches and discrepancies between the two systems.
"""

import pytest
from datetime import datetime, timezone
from decimal import Decimal

from mercury.validation.parallel_validator import (
    ParallelValidator,
    ComparisonResult,
    SignalComparison,
    PositionSizeComparison,
    RiskDecisionComparison,
    CircuitBreakerComparison,
    ValidationReport,
)


class TestParallelValidator:
    """Tests for ParallelValidator class."""

    @pytest.fixture
    def validator(self) -> ParallelValidator:
        """Create a fresh validator for each test."""
        return ParallelValidator()

    # =========================================================================
    # Signal Detection Tests
    # =========================================================================

    def test_signal_detection_match_both_detect(self, validator: ParallelValidator) -> None:
        """Both systems detect arbitrage with matching configs."""
        result = validator.compare_signal_detection(
            market_id="test-market",
            yes_best_ask=Decimal("0.48"),
            no_best_ask=Decimal("0.48"),
            mercury_config={"min_spread_threshold": "0.015"},
            polyjuiced_config={"min_spread_threshold": 0.015},
        )

        assert result.result == ComparisonResult.MATCH
        assert result.mercury_detected is True
        assert result.polyjuiced_detected is True
        assert result.discrepancy_reason is None

    def test_signal_detection_match_both_reject(self, validator: ParallelValidator) -> None:
        """Both systems reject when spread is below threshold."""
        result = validator.compare_signal_detection(
            market_id="test-market",
            yes_best_ask=Decimal("0.495"),
            no_best_ask=Decimal("0.495"),
            mercury_config={"min_spread_threshold": "0.015"},
            polyjuiced_config={"min_spread_threshold": 0.015},
        )

        assert result.result == ComparisonResult.MATCH
        assert result.mercury_detected is False
        assert result.polyjuiced_detected is False

    def test_signal_detection_match_no_arbitrage(self, validator: ParallelValidator) -> None:
        """Both systems reject when sum >= $1.00."""
        result = validator.compare_signal_detection(
            market_id="test-market",
            yes_best_ask=Decimal("0.50"),
            no_best_ask=Decimal("0.50"),
            mercury_config={"min_spread_threshold": "0.015"},
            polyjuiced_config={"min_spread_threshold": 0.015},
        )

        assert result.result == ComparisonResult.MATCH
        assert result.mercury_detected is False
        assert result.polyjuiced_detected is False

    def test_signal_detection_mismatch_different_thresholds(self, validator: ParallelValidator) -> None:
        """Systems disagree when thresholds differ."""
        # 2¢ spread - Mercury threshold 1.5¢, polyjuiced threshold 2.5¢
        result = validator.compare_signal_detection(
            market_id="test-market",
            yes_best_ask=Decimal("0.49"),
            no_best_ask=Decimal("0.49"),
            mercury_config={"min_spread_threshold": "0.015"},
            polyjuiced_config={"min_spread_threshold": 0.025},
        )

        assert result.result == ComparisonResult.MERCURY_ONLY
        assert result.mercury_detected is True
        assert result.polyjuiced_detected is False
        assert result.discrepancy_reason is not None

    def test_signal_detection_extreme_prices(self, validator: ParallelValidator) -> None:
        """Test with extreme price asymmetry."""
        result = validator.compare_signal_detection(
            market_id="test-market",
            yes_best_ask=Decimal("0.01"),
            no_best_ask=Decimal("0.94"),
            mercury_config={"min_spread_threshold": "0.015"},
            polyjuiced_config={"min_spread_threshold": 0.015},
        )

        assert result.result == ComparisonResult.MATCH
        assert result.mercury_detected is True  # 5¢ spread
        assert result.polyjuiced_detected is True

    # =========================================================================
    # Position Sizing Tests
    # =========================================================================

    def test_position_sizing_match_equal_prices(self, validator: ParallelValidator) -> None:
        """Position sizing matches with equal YES/NO prices."""
        result = validator.compare_position_sizing(
            market_id="test-market",
            budget=Decimal("25"),
            yes_price=Decimal("0.48"),
            no_price=Decimal("0.48"),
            mercury_max_trade_size=Decimal("25"),
            polyjuiced_max_trade_size=25.0,
        )

        assert result.result == ComparisonResult.MATCH
        # With equal prices, amounts should be equal
        assert result.mercury_yes_amount == result.mercury_no_amount
        assert result.yes_amount_diff is not None
        assert result.yes_amount_diff < 0.01

    def test_position_sizing_match_asymmetric_prices(self, validator: ParallelValidator) -> None:
        """Position sizing matches with asymmetric prices."""
        result = validator.compare_position_sizing(
            market_id="test-market",
            budget=Decimal("50"),
            yes_price=Decimal("0.30"),
            no_price=Decimal("0.65"),
            mercury_max_trade_size=Decimal("50"),
            polyjuiced_max_trade_size=50.0,
        )

        assert result.result == ComparisonResult.MATCH
        # Amounts should be different due to different prices
        assert result.mercury_yes_amount != result.mercury_no_amount

    def test_position_sizing_with_size_limit(self, validator: ParallelValidator) -> None:
        """Position sizing respects max trade size limit."""
        result = validator.compare_position_sizing(
            market_id="test-market",
            budget=Decimal("100"),
            yes_price=Decimal("0.48"),
            no_price=Decimal("0.48"),
            mercury_max_trade_size=Decimal("25"),  # Limit kicks in
            polyjuiced_max_trade_size=25.0,
        )

        assert result.result == ComparisonResult.MATCH
        # Neither should exceed $25
        assert result.mercury_yes_amount is not None
        assert float(result.mercury_yes_amount) <= 25.0
        assert result.mercury_no_amount is not None
        assert float(result.mercury_no_amount) <= 25.0

    def test_position_sizing_invalid_prices(self, validator: ParallelValidator) -> None:
        """Position sizing returns zero for invalid prices."""
        result = validator.compare_position_sizing(
            market_id="test-market",
            budget=Decimal("25"),
            yes_price=Decimal("0.50"),
            no_price=Decimal("0.50"),
            mercury_max_trade_size=Decimal("25"),
            polyjuiced_max_trade_size=25.0,
        )

        assert result.result == ComparisonResult.MATCH
        # Both should return zero (no arbitrage possible)
        assert result.mercury_yes_amount == Decimal("0")
        assert result.mercury_no_amount == Decimal("0")
        assert result.polyjuiced_yes_amount == 0.0
        assert result.polyjuiced_no_amount == 0.0

    # =========================================================================
    # Risk Decision Tests
    # =========================================================================

    def test_risk_decision_allow_normal(self, validator: ParallelValidator) -> None:
        """Both systems allow trade in normal conditions."""
        result = validator.compare_risk_decision(
            signal_id="test-signal",
            market_id="test-market",
            signal_size=Decimal("20"),
            daily_pnl=Decimal("0"),
            consecutive_failures=0,
            mercury_limits={
                "max_daily_loss_usd": 100,
                "max_position_size_usd": 25,
                "circuit_breaker_warning_failures": 3,
                "circuit_breaker_caution_failures": 4,
                "circuit_breaker_halt_failures": 5,
                "circuit_breaker_warning_loss": 50,
                "circuit_breaker_caution_loss": 75,
                "circuit_breaker_halt_loss": 100,
            },
            polyjuiced_limits={
                "max_daily_loss_usd": 100,
                "max_trade_size_usd": 25,
                "max_consecutive_failures": 3,
            },
        )

        assert result.result == ComparisonResult.MATCH
        assert result.mercury_allowed is True
        assert result.polyjuiced_allowed is True

    def test_risk_decision_reject_size_exceeds_limit(self, validator: ParallelValidator) -> None:
        """Both systems reject when size exceeds limit."""
        result = validator.compare_risk_decision(
            signal_id="test-signal",
            market_id="test-market",
            signal_size=Decimal("30"),  # Exceeds $25 limit
            daily_pnl=Decimal("0"),
            consecutive_failures=0,
            mercury_limits={
                "max_daily_loss_usd": 100,
                "max_position_size_usd": 25,
                "circuit_breaker_warning_failures": 3,
                "circuit_breaker_caution_failures": 4,
                "circuit_breaker_halt_failures": 5,
                "circuit_breaker_warning_loss": 50,
                "circuit_breaker_caution_loss": 75,
                "circuit_breaker_halt_loss": 100,
            },
            polyjuiced_limits={
                "max_daily_loss_usd": 100,
                "max_trade_size_usd": 25,
                "max_consecutive_failures": 3,
            },
        )

        assert result.result == ComparisonResult.MATCH
        assert result.mercury_allowed is False
        assert result.polyjuiced_allowed is False

    def test_risk_decision_reject_daily_loss(self, validator: ParallelValidator) -> None:
        """Both systems reject when daily loss limit reached."""
        result = validator.compare_risk_decision(
            signal_id="test-signal",
            market_id="test-market",
            signal_size=Decimal("20"),
            daily_pnl=Decimal("-100"),  # At loss limit
            consecutive_failures=0,
            mercury_limits={
                "max_daily_loss_usd": 100,
                "max_position_size_usd": 25,
                "circuit_breaker_warning_failures": 3,
                "circuit_breaker_caution_failures": 4,
                "circuit_breaker_halt_failures": 5,
                "circuit_breaker_warning_loss": 50,
                "circuit_breaker_caution_loss": 75,
                "circuit_breaker_halt_loss": 100,
            },
            polyjuiced_limits={
                "max_daily_loss_usd": 100,
                "max_trade_size_usd": 25,
                "max_consecutive_failures": 3,
            },
        )

        assert result.result == ComparisonResult.MATCH
        assert result.mercury_allowed is False
        assert result.polyjuiced_allowed is False

    def test_risk_decision_reject_circuit_breaker_halt(self, validator: ParallelValidator) -> None:
        """Both systems reject at HALT level."""
        result = validator.compare_risk_decision(
            signal_id="test-signal",
            market_id="test-market",
            signal_size=Decimal("20"),
            daily_pnl=Decimal("0"),
            consecutive_failures=5,  # HALT level
            mercury_limits={
                "max_daily_loss_usd": 100,
                "max_position_size_usd": 25,
                "circuit_breaker_warning_failures": 3,
                "circuit_breaker_caution_failures": 4,
                "circuit_breaker_halt_failures": 5,
                "circuit_breaker_warning_loss": 50,
                "circuit_breaker_caution_loss": 75,
                "circuit_breaker_halt_loss": 100,
            },
            polyjuiced_limits={
                "max_daily_loss_usd": 100,
                "max_trade_size_usd": 25,
                "max_consecutive_failures": 3,
            },
        )

        assert result.result == ComparisonResult.MATCH
        assert result.mercury_allowed is False
        assert result.polyjuiced_allowed is False
        assert result.mercury_circuit_breaker_state == "HALT"
        assert result.polyjuiced_circuit_breaker_level == "HALT"

    # =========================================================================
    # Circuit Breaker Tests
    # =========================================================================

    def test_circuit_breaker_normal_state(self, validator: ParallelValidator) -> None:
        """Both systems in NORMAL state with no failures/losses."""
        result = validator.compare_circuit_breaker(
            trigger_event="normal",
            consecutive_failures=0,
            daily_pnl=Decimal("0"),
            mercury_config={
                "warning_failures": 3,
                "caution_failures": 4,
                "halt_failures": 5,
                "warning_loss": 50,
                "caution_loss": 75,
                "halt_loss": 100,
            },
            polyjuiced_config={
                "max_consecutive_failures": 3,
                "max_daily_loss_usd": 100,
            },
        )

        assert result.result == ComparisonResult.MATCH
        assert result.mercury_state == "NORMAL"
        assert result.polyjuiced_level == "NORMAL"
        assert result.mercury_size_multiplier == 1.0
        assert result.polyjuiced_size_multiplier == 1.0

    def test_circuit_breaker_warning_on_failures(self, validator: ParallelValidator) -> None:
        """Both systems enter WARNING on 3 failures."""
        result = validator.compare_circuit_breaker(
            trigger_event="failure",
            consecutive_failures=3,
            daily_pnl=Decimal("0"),
            mercury_config={
                "warning_failures": 3,
                "caution_failures": 4,
                "halt_failures": 5,
                "warning_loss": 50,
                "caution_loss": 75,
                "halt_loss": 100,
            },
            polyjuiced_config={
                "max_consecutive_failures": 3,
                "max_daily_loss_usd": 100,
            },
        )

        assert result.result == ComparisonResult.MATCH
        assert result.mercury_state == "WARNING"
        assert result.polyjuiced_level == "WARNING"
        assert result.mercury_size_multiplier == 0.5
        assert result.polyjuiced_size_multiplier == 0.5

    def test_circuit_breaker_halt_on_loss(self, validator: ParallelValidator) -> None:
        """Both systems enter HALT on daily loss limit."""
        result = validator.compare_circuit_breaker(
            trigger_event="loss",
            consecutive_failures=0,
            daily_pnl=Decimal("-100"),  # At loss limit
            mercury_config={
                "warning_failures": 3,
                "caution_failures": 4,
                "halt_failures": 5,
                "warning_loss": 50,
                "caution_loss": 75,
                "halt_loss": 100,
            },
            polyjuiced_config={
                "max_consecutive_failures": 3,
                "max_daily_loss_usd": 100,
            },
        )

        assert result.result == ComparisonResult.MATCH
        assert result.mercury_state == "HALT"
        assert result.polyjuiced_level == "HALT"
        assert result.mercury_can_trade is False
        assert result.polyjuiced_can_trade is False

    # =========================================================================
    # Report Generation Tests
    # =========================================================================

    def test_report_generation(self, validator: ParallelValidator) -> None:
        """Test validation report generation."""
        # Run some comparisons
        validator.compare_signal_detection(
            market_id="market-1",
            yes_best_ask=Decimal("0.48"),
            no_best_ask=Decimal("0.48"),
            mercury_config={"min_spread_threshold": "0.015"},
            polyjuiced_config={"min_spread_threshold": 0.015},
        )

        validator.compare_position_sizing(
            market_id="market-1",
            budget=Decimal("25"),
            yes_price=Decimal("0.48"),
            no_price=Decimal("0.48"),
            mercury_max_trade_size=Decimal("25"),
            polyjuiced_max_trade_size=25.0,
        )

        # Finalize report
        report = validator.finalize_report()

        assert report.total_signals_compared == 1
        assert report.total_positions_compared == 1
        assert report.signal_matches == 1
        assert report.position_matches == 1
        assert report.completed_at is not None
        assert report.duration_seconds >= 0
        assert report.overall_match_rate == 100.0

    def test_report_to_dict(self, validator: ParallelValidator) -> None:
        """Test report serialization to dictionary."""
        validator.compare_signal_detection(
            market_id="market-1",
            yes_best_ask=Decimal("0.48"),
            no_best_ask=Decimal("0.48"),
            mercury_config={"min_spread_threshold": "0.015"},
            polyjuiced_config={"min_spread_threshold": 0.015},
        )

        report = validator.finalize_report()
        report_dict = report.to_dict()

        assert "started_at" in report_dict
        assert "completed_at" in report_dict
        assert "summary" in report_dict
        assert "overall_match_rate" in report_dict["summary"]
        assert "signals" in report_dict["summary"]


class TestValidationReport:
    """Tests for ValidationReport class."""

    def test_match_rate_no_comparisons(self) -> None:
        """Match rate is 100% when no comparisons made."""
        report = ValidationReport(started_at=datetime.now(timezone.utc))

        assert report.signal_match_rate == 100.0
        assert report.position_match_rate == 100.0
        assert report.risk_match_rate == 100.0
        assert report.circuit_breaker_match_rate == 100.0
        assert report.overall_match_rate == 100.0

    def test_match_rate_all_matches(self) -> None:
        """Match rate is 100% when all comparisons match."""
        report = ValidationReport(started_at=datetime.now(timezone.utc))
        report.total_signals_compared = 10
        report.signal_matches = 10

        assert report.signal_match_rate == 100.0

    def test_match_rate_some_mismatches(self) -> None:
        """Match rate calculated correctly with mismatches."""
        report = ValidationReport(started_at=datetime.now(timezone.utc))
        report.total_signals_compared = 10
        report.signal_matches = 8
        report.signal_mismatches = 2

        assert report.signal_match_rate == 80.0

    def test_overall_match_rate(self) -> None:
        """Overall match rate aggregates all categories."""
        report = ValidationReport(started_at=datetime.now(timezone.utc))
        report.total_signals_compared = 10
        report.signal_matches = 10
        report.total_positions_compared = 10
        report.position_matches = 8
        report.total_risk_decisions_compared = 10
        report.risk_decision_matches = 9
        report.total_circuit_breaker_compared = 10
        report.circuit_breaker_matches = 10

        # Total: 40 comparisons, 37 matches = 92.5%
        assert report.overall_match_rate == 92.5


class TestComparisonDataClasses:
    """Tests for comparison data classes."""

    def test_signal_comparison_creation(self) -> None:
        """Test SignalComparison creation."""
        comparison = SignalComparison(
            market_id="test-market",
            timestamp=datetime.now(timezone.utc),
            result=ComparisonResult.MATCH,
            mercury_detected=True,
            polyjuiced_detected=True,
        )

        assert comparison.market_id == "test-market"
        assert comparison.result == ComparisonResult.MATCH

    def test_position_size_comparison_creation(self) -> None:
        """Test PositionSizeComparison creation."""
        comparison = PositionSizeComparison(
            market_id="test-market",
            timestamp=datetime.now(timezone.utc),
            budget=Decimal("25"),
            yes_price=Decimal("0.48"),
            no_price=Decimal("0.48"),
            result=ComparisonResult.MATCH,
        )

        assert comparison.market_id == "test-market"
        assert comparison.budget == Decimal("25")

    def test_risk_decision_comparison_creation(self) -> None:
        """Test RiskDecisionComparison creation."""
        comparison = RiskDecisionComparison(
            signal_id="test-signal",
            market_id="test-market",
            timestamp=datetime.now(timezone.utc),
            signal_size=Decimal("20"),
            result=ComparisonResult.MISMATCH,
            mercury_allowed=True,
            polyjuiced_allowed=False,
            discrepancy_reason="Test discrepancy",
        )

        assert comparison.signal_id == "test-signal"
        assert comparison.result == ComparisonResult.MISMATCH

    def test_circuit_breaker_comparison_creation(self) -> None:
        """Test CircuitBreakerComparison creation."""
        comparison = CircuitBreakerComparison(
            timestamp=datetime.now(timezone.utc),
            trigger_event="failure",
            result=ComparisonResult.MATCH,
            mercury_state="WARNING",
            polyjuiced_level="WARNING",
        )

        assert comparison.trigger_event == "failure"
        assert comparison.result == ComparisonResult.MATCH
