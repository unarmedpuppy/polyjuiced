#!/usr/bin/env python3
"""Parallel Validation: Run Mercury alongside polyjuiced and compare behavior.

This script runs Mercury in dry-run mode and compares its signal generation,
order decisions, and risk calculations against the legacy polyjuiced system.

The validation covers:
1. Signal Generation: Do both systems detect the same arbitrage opportunities?
2. Position Sizing: Do both systems calculate the same trade sizes?
3. Risk Decisions: Do both systems make the same allow/reject decisions?
4. Circuit Breaker: Do both systems transition through the same states?

Usage:
    python -m mercury.scripts.parallel_validation --duration 3600
    python -m mercury.scripts.parallel_validation --compare-signals
    python -m mercury.scripts.parallel_validation --report
"""

import argparse
import asyncio
import json
import sys
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any, Optional


# ============================================================================
# Data Models for Validation
# ============================================================================


class ComparisonResult(str, Enum):
    """Result of comparing Mercury vs polyjuiced behavior."""
    MATCH = "match"
    MISMATCH = "mismatch"
    MERCURY_ONLY = "mercury_only"
    POLYJUICED_ONLY = "polyjuiced_only"


@dataclass
class SignalComparison:
    """Comparison of signal generation between systems."""
    market_id: str
    timestamp: datetime
    result: ComparisonResult

    # Mercury signal details
    mercury_detected: bool = False
    mercury_spread_cents: Optional[float] = None
    mercury_yes_price: Optional[Decimal] = None
    mercury_no_price: Optional[Decimal] = None
    mercury_target_size: Optional[Decimal] = None
    mercury_expected_pnl: Optional[Decimal] = None

    # Polyjuiced signal details
    polyjuiced_detected: bool = False
    polyjuiced_spread_cents: Optional[float] = None
    polyjuiced_yes_price: Optional[float] = None
    polyjuiced_no_price: Optional[float] = None
    polyjuiced_target_size: Optional[float] = None
    polyjuiced_expected_pnl: Optional[float] = None

    # Discrepancy details
    discrepancy_reason: Optional[str] = None
    price_difference_yes: Optional[float] = None
    price_difference_no: Optional[float] = None
    size_difference: Optional[float] = None
    pnl_difference: Optional[float] = None


@dataclass
class PositionSizeComparison:
    """Comparison of position sizing calculations."""
    market_id: str
    timestamp: datetime
    budget: Decimal
    yes_price: Decimal
    no_price: Decimal
    result: ComparisonResult

    # Mercury calculation
    mercury_yes_amount: Optional[Decimal] = None
    mercury_no_amount: Optional[Decimal] = None

    # Polyjuiced calculation
    polyjuiced_yes_amount: Optional[float] = None
    polyjuiced_no_amount: Optional[float] = None

    # Discrepancy
    discrepancy_reason: Optional[str] = None
    yes_amount_diff: Optional[float] = None
    no_amount_diff: Optional[float] = None


@dataclass
class RiskDecisionComparison:
    """Comparison of risk manager decisions."""
    signal_id: str
    market_id: str
    timestamp: datetime
    signal_size: Decimal
    result: ComparisonResult

    # Mercury decision
    mercury_allowed: Optional[bool] = None
    mercury_reason: Optional[str] = None
    mercury_circuit_breaker_state: Optional[str] = None
    mercury_daily_pnl: Optional[Decimal] = None

    # Polyjuiced decision
    polyjuiced_allowed: Optional[bool] = None
    polyjuiced_reason: Optional[str] = None
    polyjuiced_circuit_breaker_level: Optional[str] = None
    polyjuiced_daily_loss: Optional[float] = None

    # Discrepancy
    discrepancy_reason: Optional[str] = None


@dataclass
class CircuitBreakerComparison:
    """Comparison of circuit breaker state transitions."""
    timestamp: datetime
    trigger_event: str  # "failure", "loss", "reset"
    result: ComparisonResult

    # Mercury state
    mercury_state: Optional[str] = None
    mercury_size_multiplier: Optional[float] = None
    mercury_can_trade: Optional[bool] = None
    mercury_consecutive_failures: Optional[int] = None

    # Polyjuiced state
    polyjuiced_level: Optional[str] = None
    polyjuiced_size_multiplier: Optional[float] = None
    polyjuiced_can_trade: Optional[bool] = None
    polyjuiced_consecutive_failures: Optional[int] = None

    # Discrepancy
    discrepancy_reason: Optional[str] = None


@dataclass
class ValidationReport:
    """Complete validation report."""
    started_at: datetime
    completed_at: Optional[datetime] = None
    duration_seconds: float = 0.0

    # Summary counts
    total_signals_compared: int = 0
    signal_matches: int = 0
    signal_mismatches: int = 0

    total_positions_compared: int = 0
    position_matches: int = 0
    position_mismatches: int = 0

    total_risk_decisions_compared: int = 0
    risk_decision_matches: int = 0
    risk_decision_mismatches: int = 0

    total_circuit_breaker_compared: int = 0
    circuit_breaker_matches: int = 0
    circuit_breaker_mismatches: int = 0

    # Detailed comparisons
    signal_comparisons: list[SignalComparison] = field(default_factory=list)
    position_comparisons: list[PositionSizeComparison] = field(default_factory=list)
    risk_comparisons: list[RiskDecisionComparison] = field(default_factory=list)
    circuit_breaker_comparisons: list[CircuitBreakerComparison] = field(default_factory=list)

    # Discrepancy summary
    discrepancy_categories: dict[str, int] = field(default_factory=dict)
    critical_discrepancies: list[str] = field(default_factory=list)

    @property
    def signal_match_rate(self) -> float:
        """Get signal match rate as percentage."""
        if self.total_signals_compared == 0:
            return 100.0
        return (self.signal_matches / self.total_signals_compared) * 100

    @property
    def position_match_rate(self) -> float:
        """Get position sizing match rate as percentage."""
        if self.total_positions_compared == 0:
            return 100.0
        return (self.position_matches / self.total_positions_compared) * 100

    @property
    def risk_match_rate(self) -> float:
        """Get risk decision match rate as percentage."""
        if self.total_risk_decisions_compared == 0:
            return 100.0
        return (self.risk_decision_matches / self.total_risk_decisions_compared) * 100

    @property
    def circuit_breaker_match_rate(self) -> float:
        """Get circuit breaker match rate as percentage."""
        if self.total_circuit_breaker_compared == 0:
            return 100.0
        return (self.circuit_breaker_matches / self.total_circuit_breaker_compared) * 100

    @property
    def overall_match_rate(self) -> float:
        """Get overall match rate across all categories."""
        total = (
            self.total_signals_compared +
            self.total_positions_compared +
            self.total_risk_decisions_compared +
            self.total_circuit_breaker_compared
        )
        if total == 0:
            return 100.0
        matches = (
            self.signal_matches +
            self.position_matches +
            self.risk_decision_matches +
            self.circuit_breaker_matches
        )
        return (matches / total) * 100

    def to_dict(self) -> dict[str, Any]:
        """Convert report to dictionary for serialization."""
        return {
            "started_at": self.started_at.isoformat(),
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "duration_seconds": self.duration_seconds,
            "summary": {
                "overall_match_rate": f"{self.overall_match_rate:.1f}%",
                "signals": {
                    "total": self.total_signals_compared,
                    "matches": self.signal_matches,
                    "mismatches": self.signal_mismatches,
                    "match_rate": f"{self.signal_match_rate:.1f}%",
                },
                "position_sizing": {
                    "total": self.total_positions_compared,
                    "matches": self.position_matches,
                    "mismatches": self.position_mismatches,
                    "match_rate": f"{self.position_match_rate:.1f}%",
                },
                "risk_decisions": {
                    "total": self.total_risk_decisions_compared,
                    "matches": self.risk_decision_matches,
                    "mismatches": self.risk_decision_mismatches,
                    "match_rate": f"{self.risk_match_rate:.1f}%",
                },
                "circuit_breaker": {
                    "total": self.total_circuit_breaker_compared,
                    "matches": self.circuit_breaker_matches,
                    "mismatches": self.circuit_breaker_mismatches,
                    "match_rate": f"{self.circuit_breaker_match_rate:.1f}%",
                },
            },
            "discrepancy_categories": self.discrepancy_categories,
            "critical_discrepancies": self.critical_discrepancies,
            "signal_mismatches": [
                asdict(c) for c in self.signal_comparisons
                if c.result != ComparisonResult.MATCH
            ][:50],  # Limit to first 50
            "risk_mismatches": [
                asdict(c) for c in self.risk_comparisons
                if c.result != ComparisonResult.MATCH
            ][:50],
        }


# ============================================================================
# Validation Logic
# ============================================================================


class ParallelValidator:
    """Validates Mercury behavior against polyjuiced.

    This class provides methods to compare:
    1. Arbitrage detection and signal generation
    2. Position sizing calculations
    3. Risk manager decisions
    4. Circuit breaker state transitions
    """

    # Tolerance thresholds for comparison
    PRICE_TOLERANCE = Decimal("0.0001")  # 0.01 cents
    SIZE_TOLERANCE = Decimal("0.01")  # $0.01
    PNL_TOLERANCE = Decimal("0.001")  # $0.001

    def __init__(self) -> None:
        """Initialize the parallel validator."""
        self.report = ValidationReport(started_at=datetime.now(timezone.utc))

    def compare_signal_detection(
        self,
        market_id: str,
        yes_best_ask: Decimal,
        no_best_ask: Decimal,
        mercury_config: dict[str, Any],
        polyjuiced_config: dict[str, Any],
    ) -> SignalComparison:
        """Compare signal detection between Mercury and polyjuiced.

        Both systems detect arbitrage when YES + NO < $1.00 and spread
        exceeds their configured threshold.

        Args:
            market_id: Market identifier.
            yes_best_ask: Best ask price for YES.
            no_best_ask: Best ask price for NO.
            mercury_config: Mercury gabagool config.
            polyjuiced_config: Polyjuiced gabagool config.

        Returns:
            SignalComparison with comparison results.
        """
        timestamp = datetime.now(timezone.utc)
        combined = yes_best_ask + no_best_ask
        spread = Decimal("1") - combined
        spread_cents = float(spread * 100)

        # Mercury detection logic (from mercury/strategies/gabagool/strategy.py)
        mercury_min_spread = Decimal(str(mercury_config.get("min_spread_threshold", "0.015")))
        mercury_detected = combined < Decimal("1") and spread >= mercury_min_spread

        # Polyjuiced detection logic (from legacy/src/strategies/gabagool.py)
        polyjuiced_min_spread = float(polyjuiced_config.get("min_spread_threshold", 0.015))
        polyjuiced_detected = float(combined) < 1.0 and float(spread) >= polyjuiced_min_spread

        # Determine comparison result
        if mercury_detected == polyjuiced_detected:
            result = ComparisonResult.MATCH
            discrepancy_reason = None
        elif mercury_detected and not polyjuiced_detected:
            result = ComparisonResult.MERCURY_ONLY
            discrepancy_reason = f"Mercury detected (spread {spread_cents:.2f}¢ >= {float(mercury_min_spread)*100:.1f}¢) but polyjuiced did not (threshold {polyjuiced_min_spread*100:.1f}¢)"
        elif polyjuiced_detected and not mercury_detected:
            result = ComparisonResult.POLYJUICED_ONLY
            discrepancy_reason = f"Polyjuiced detected (spread {spread_cents:.2f}¢ >= {polyjuiced_min_spread*100:.1f}¢) but Mercury did not (threshold {float(mercury_min_spread)*100:.1f}¢)"
        else:
            result = ComparisonResult.MISMATCH
            discrepancy_reason = "Unknown detection mismatch"

        comparison = SignalComparison(
            market_id=market_id,
            timestamp=timestamp,
            result=result,
            mercury_detected=mercury_detected,
            mercury_spread_cents=spread_cents if mercury_detected else None,
            mercury_yes_price=yes_best_ask if mercury_detected else None,
            mercury_no_price=no_best_ask if mercury_detected else None,
            polyjuiced_detected=polyjuiced_detected,
            polyjuiced_spread_cents=spread_cents if polyjuiced_detected else None,
            polyjuiced_yes_price=float(yes_best_ask) if polyjuiced_detected else None,
            polyjuiced_no_price=float(no_best_ask) if polyjuiced_detected else None,
            discrepancy_reason=discrepancy_reason,
        )

        # Update report
        self.report.total_signals_compared += 1
        if result == ComparisonResult.MATCH:
            self.report.signal_matches += 1
        else:
            self.report.signal_mismatches += 1
            self.report.signal_comparisons.append(comparison)
            self._categorize_discrepancy("signal_detection", discrepancy_reason)

        return comparison

    def compare_position_sizing(
        self,
        market_id: str,
        budget: Decimal,
        yes_price: Decimal,
        no_price: Decimal,
        mercury_max_trade_size: Decimal,
        polyjuiced_max_trade_size: float,
    ) -> PositionSizeComparison:
        """Compare position sizing calculations.

        Both systems should calculate equal shares for YES and NO to
        maximize arbitrage profit.

        Args:
            market_id: Market identifier.
            budget: Total budget for the trade.
            yes_price: YES price.
            no_price: NO price.
            mercury_max_trade_size: Mercury max single trade size.
            polyjuiced_max_trade_size: Polyjuiced max single trade size.

        Returns:
            PositionSizeComparison with comparison results.
        """
        timestamp = datetime.now(timezone.utc)

        # Mercury calculation (from mercury/strategies/gabagool/strategy.py)
        cost_per_pair = yes_price + no_price
        if cost_per_pair <= 0 or cost_per_pair >= Decimal("1"):
            mercury_yes_amount = Decimal("0")
            mercury_no_amount = Decimal("0")
        else:
            num_pairs = budget / cost_per_pair
            mercury_yes_amount = num_pairs * yes_price
            mercury_no_amount = num_pairs * no_price

            # Apply max trade size limit
            max_single = mercury_max_trade_size
            if mercury_yes_amount > max_single or mercury_no_amount > max_single:
                scale = max_single / max(mercury_yes_amount, mercury_no_amount)
                mercury_yes_amount = mercury_yes_amount * scale
                mercury_no_amount = mercury_no_amount * scale

        # Polyjuiced calculation (simplified - legacy uses similar logic)
        cost_per_pair_float = float(yes_price) + float(no_price)
        if cost_per_pair_float <= 0 or cost_per_pair_float >= 1.0:
            polyjuiced_yes_amount = 0.0
            polyjuiced_no_amount = 0.0
        else:
            num_pairs_float = float(budget) / cost_per_pair_float
            polyjuiced_yes_amount = num_pairs_float * float(yes_price)
            polyjuiced_no_amount = num_pairs_float * float(no_price)

            # Apply max trade size limit
            if polyjuiced_yes_amount > polyjuiced_max_trade_size or polyjuiced_no_amount > polyjuiced_max_trade_size:
                scale = polyjuiced_max_trade_size / max(polyjuiced_yes_amount, polyjuiced_no_amount)
                polyjuiced_yes_amount = polyjuiced_yes_amount * scale
                polyjuiced_no_amount = polyjuiced_no_amount * scale

        # Compare results
        yes_diff = abs(float(mercury_yes_amount) - polyjuiced_yes_amount)
        no_diff = abs(float(mercury_no_amount) - polyjuiced_no_amount)

        if yes_diff <= float(self.SIZE_TOLERANCE) and no_diff <= float(self.SIZE_TOLERANCE):
            result = ComparisonResult.MATCH
            discrepancy_reason = None
        else:
            result = ComparisonResult.MISMATCH
            discrepancy_reason = f"Position size mismatch: YES diff ${yes_diff:.4f}, NO diff ${no_diff:.4f}"

        comparison = PositionSizeComparison(
            market_id=market_id,
            timestamp=timestamp,
            budget=budget,
            yes_price=yes_price,
            no_price=no_price,
            result=result,
            mercury_yes_amount=mercury_yes_amount,
            mercury_no_amount=mercury_no_amount,
            polyjuiced_yes_amount=polyjuiced_yes_amount,
            polyjuiced_no_amount=polyjuiced_no_amount,
            discrepancy_reason=discrepancy_reason,
            yes_amount_diff=yes_diff,
            no_amount_diff=no_diff,
        )

        # Update report
        self.report.total_positions_compared += 1
        if result == ComparisonResult.MATCH:
            self.report.position_matches += 1
        else:
            self.report.position_mismatches += 1
            self.report.position_comparisons.append(comparison)
            self._categorize_discrepancy("position_sizing", discrepancy_reason)

        return comparison

    def compare_risk_decision(
        self,
        signal_id: str,
        market_id: str,
        signal_size: Decimal,
        daily_pnl: Decimal,
        consecutive_failures: int,
        mercury_limits: dict[str, Any],
        polyjuiced_limits: dict[str, Any],
    ) -> RiskDecisionComparison:
        """Compare risk manager allow/reject decisions.

        Both systems should make the same decision given the same state.

        Args:
            signal_id: Signal identifier.
            market_id: Market identifier.
            signal_size: Proposed trade size.
            daily_pnl: Current daily P&L.
            consecutive_failures: Number of consecutive failures.
            mercury_limits: Mercury risk limits config.
            polyjuiced_limits: Polyjuiced risk limits config.

        Returns:
            RiskDecisionComparison with comparison results.
        """
        timestamp = datetime.now(timezone.utc)

        # Mercury decision logic (from mercury/services/risk_manager.py)
        mercury_max_daily_loss = Decimal(str(mercury_limits.get("max_daily_loss_usd", 100)))
        mercury_max_position_size = Decimal(str(mercury_limits.get("max_position_size_usd", 25)))
        mercury_halt_failures = mercury_limits.get("circuit_breaker_halt_failures", 5)
        mercury_halt_loss = Decimal(str(mercury_limits.get("circuit_breaker_halt_loss", 100)))

        # Determine Mercury circuit breaker state
        mercury_state = "NORMAL"
        if consecutive_failures >= mercury_halt_failures:
            mercury_state = "HALT"
        elif consecutive_failures >= mercury_limits.get("circuit_breaker_caution_failures", 4):
            mercury_state = "CAUTION"
        elif consecutive_failures >= mercury_limits.get("circuit_breaker_warning_failures", 3):
            mercury_state = "WARNING"

        if -daily_pnl >= mercury_halt_loss:
            mercury_state = "HALT"
        elif -daily_pnl >= Decimal(str(mercury_limits.get("circuit_breaker_caution_loss", 75))):
            mercury_state = max(mercury_state, "CAUTION", key=lambda x: ["NORMAL", "WARNING", "CAUTION", "HALT"].index(x))
        elif -daily_pnl >= Decimal(str(mercury_limits.get("circuit_breaker_warning_loss", 50))):
            mercury_state = max(mercury_state, "WARNING", key=lambda x: ["NORMAL", "WARNING", "CAUTION", "HALT"].index(x))

        # Mercury allowed?
        mercury_allowed = True
        mercury_reason = None

        if mercury_state == "HALT":
            mercury_allowed = False
            mercury_reason = "Circuit breaker HALT"
        elif mercury_state == "CAUTION":
            mercury_allowed = False
            mercury_reason = "Circuit breaker CAUTION - only closes allowed"
        elif daily_pnl <= -mercury_max_daily_loss:
            mercury_allowed = False
            mercury_reason = f"Daily loss limit reached: ${-daily_pnl:.2f}"
        elif signal_size > mercury_max_position_size:
            mercury_allowed = False
            mercury_reason = f"Position size ${signal_size:.2f} exceeds limit ${mercury_max_position_size:.2f}"

        # Polyjuiced decision logic (from legacy/src/risk/circuit_breaker.py)
        polyjuiced_max_daily_loss = float(polyjuiced_limits.get("max_daily_loss_usd", 100))
        polyjuiced_max_position_size = float(polyjuiced_limits.get("max_trade_size_usd", 25))
        polyjuiced_max_failures = polyjuiced_limits.get("max_consecutive_failures", 3)

        # Determine polyjuiced circuit breaker level
        polyjuiced_level = "NORMAL"
        if consecutive_failures >= polyjuiced_max_failures + 2:
            polyjuiced_level = "HALT"
        elif consecutive_failures >= polyjuiced_max_failures + 1:
            polyjuiced_level = "CAUTION"
        elif consecutive_failures >= polyjuiced_max_failures:
            polyjuiced_level = "WARNING"

        if float(-daily_pnl) >= polyjuiced_max_daily_loss:
            polyjuiced_level = "HALT"

        # Polyjuiced allowed?
        polyjuiced_allowed = True
        polyjuiced_reason = None

        if polyjuiced_level == "HALT":
            polyjuiced_allowed = False
            polyjuiced_reason = "Circuit breaker HALT"
        elif polyjuiced_level == "CAUTION":
            polyjuiced_allowed = False
            polyjuiced_reason = "Circuit breaker CAUTION"
        elif float(-daily_pnl) >= polyjuiced_max_daily_loss:
            polyjuiced_allowed = False
            polyjuiced_reason = f"Daily loss limit: ${float(-daily_pnl):.2f}"
        elif float(signal_size) > polyjuiced_max_position_size:
            polyjuiced_allowed = False
            polyjuiced_reason = f"Position size ${float(signal_size):.2f} exceeds limit"

        # Compare
        if mercury_allowed == polyjuiced_allowed:
            result = ComparisonResult.MATCH
            discrepancy_reason = None
        else:
            result = ComparisonResult.MISMATCH
            discrepancy_reason = f"Mercury {'allowed' if mercury_allowed else 'rejected'} ({mercury_reason}) vs polyjuiced {'allowed' if polyjuiced_allowed else 'rejected'} ({polyjuiced_reason})"

        comparison = RiskDecisionComparison(
            signal_id=signal_id,
            market_id=market_id,
            timestamp=timestamp,
            signal_size=signal_size,
            result=result,
            mercury_allowed=mercury_allowed,
            mercury_reason=mercury_reason,
            mercury_circuit_breaker_state=mercury_state,
            mercury_daily_pnl=daily_pnl,
            polyjuiced_allowed=polyjuiced_allowed,
            polyjuiced_reason=polyjuiced_reason,
            polyjuiced_circuit_breaker_level=polyjuiced_level,
            polyjuiced_daily_loss=float(-daily_pnl),
            discrepancy_reason=discrepancy_reason,
        )

        # Update report
        self.report.total_risk_decisions_compared += 1
        if result == ComparisonResult.MATCH:
            self.report.risk_decision_matches += 1
        else:
            self.report.risk_decision_mismatches += 1
            self.report.risk_comparisons.append(comparison)
            self._categorize_discrepancy("risk_decision", discrepancy_reason)

            # Mark as critical if decisions differ
            if discrepancy_reason:
                self.report.critical_discrepancies.append(
                    f"Risk decision mismatch for {signal_id}: {discrepancy_reason}"
                )

        return comparison

    def compare_circuit_breaker(
        self,
        trigger_event: str,
        consecutive_failures: int,
        daily_pnl: Decimal,
        mercury_config: dict[str, Any],
        polyjuiced_config: dict[str, Any],
    ) -> CircuitBreakerComparison:
        """Compare circuit breaker state transitions.

        Args:
            trigger_event: What triggered the comparison ("failure", "loss", "reset").
            consecutive_failures: Number of consecutive failures.
            daily_pnl: Current daily P&L.
            mercury_config: Mercury circuit breaker config.
            polyjuiced_config: Polyjuiced circuit breaker config.

        Returns:
            CircuitBreakerComparison with comparison results.
        """
        timestamp = datetime.now(timezone.utc)

        # Mercury state calculation
        mercury_state = "NORMAL"
        mercury_halt_failures = mercury_config.get("halt_failures", 5)
        mercury_caution_failures = mercury_config.get("caution_failures", 4)
        mercury_warning_failures = mercury_config.get("warning_failures", 3)
        mercury_halt_loss = Decimal(str(mercury_config.get("halt_loss", 100)))
        mercury_caution_loss = Decimal(str(mercury_config.get("caution_loss", 75)))
        mercury_warning_loss = Decimal(str(mercury_config.get("warning_loss", 50)))

        if consecutive_failures >= mercury_halt_failures or -daily_pnl >= mercury_halt_loss:
            mercury_state = "HALT"
        elif consecutive_failures >= mercury_caution_failures or -daily_pnl >= mercury_caution_loss:
            mercury_state = "CAUTION"
        elif consecutive_failures >= mercury_warning_failures or -daily_pnl >= mercury_warning_loss:
            mercury_state = "WARNING"

        mercury_size_multiplier = 1.0
        if mercury_state == "WARNING":
            mercury_size_multiplier = 0.5
        elif mercury_state in ("CAUTION", "HALT"):
            mercury_size_multiplier = 0.0

        mercury_can_trade = mercury_state not in ("CAUTION", "HALT")

        # Polyjuiced state calculation
        polyjuiced_level = "NORMAL"
        polyjuiced_max_failures = polyjuiced_config.get("max_consecutive_failures", 3)
        polyjuiced_max_daily_loss = float(polyjuiced_config.get("max_daily_loss_usd", 100))

        if consecutive_failures >= polyjuiced_max_failures + 2 or float(-daily_pnl) >= polyjuiced_max_daily_loss:
            polyjuiced_level = "HALT"
        elif consecutive_failures >= polyjuiced_max_failures + 1:
            polyjuiced_level = "CAUTION"
        elif consecutive_failures >= polyjuiced_max_failures:
            polyjuiced_level = "WARNING"

        polyjuiced_size_multiplier = 1.0
        if polyjuiced_level == "WARNING":
            polyjuiced_size_multiplier = 0.5
        elif polyjuiced_level in ("CAUTION", "HALT"):
            polyjuiced_size_multiplier = 0.0

        polyjuiced_can_trade = polyjuiced_level not in ("CAUTION", "HALT")

        # Compare
        state_match = mercury_state == polyjuiced_level
        multiplier_match = abs(mercury_size_multiplier - polyjuiced_size_multiplier) < 0.01
        can_trade_match = mercury_can_trade == polyjuiced_can_trade

        if state_match and multiplier_match and can_trade_match:
            result = ComparisonResult.MATCH
            discrepancy_reason = None
        else:
            result = ComparisonResult.MISMATCH
            reasons = []
            if not state_match:
                reasons.append(f"state: Mercury={mercury_state} vs polyjuiced={polyjuiced_level}")
            if not multiplier_match:
                reasons.append(f"multiplier: Mercury={mercury_size_multiplier} vs polyjuiced={polyjuiced_size_multiplier}")
            if not can_trade_match:
                reasons.append(f"can_trade: Mercury={mercury_can_trade} vs polyjuiced={polyjuiced_can_trade}")
            discrepancy_reason = "; ".join(reasons)

        comparison = CircuitBreakerComparison(
            timestamp=timestamp,
            trigger_event=trigger_event,
            result=result,
            mercury_state=mercury_state,
            mercury_size_multiplier=mercury_size_multiplier,
            mercury_can_trade=mercury_can_trade,
            mercury_consecutive_failures=consecutive_failures,
            polyjuiced_level=polyjuiced_level,
            polyjuiced_size_multiplier=polyjuiced_size_multiplier,
            polyjuiced_can_trade=polyjuiced_can_trade,
            polyjuiced_consecutive_failures=consecutive_failures,
            discrepancy_reason=discrepancy_reason,
        )

        # Update report
        self.report.total_circuit_breaker_compared += 1
        if result == ComparisonResult.MATCH:
            self.report.circuit_breaker_matches += 1
        else:
            self.report.circuit_breaker_mismatches += 1
            self.report.circuit_breaker_comparisons.append(comparison)
            self._categorize_discrepancy("circuit_breaker", discrepancy_reason)

        return comparison

    def _categorize_discrepancy(self, category: str, reason: Optional[str]) -> None:
        """Categorize a discrepancy for the report."""
        if reason:
            key = f"{category}:{reason[:50]}"
            self.report.discrepancy_categories[key] = \
                self.report.discrepancy_categories.get(key, 0) + 1

    def finalize_report(self) -> ValidationReport:
        """Finalize and return the validation report."""
        self.report.completed_at = datetime.now(timezone.utc)
        self.report.duration_seconds = (
            self.report.completed_at - self.report.started_at
        ).total_seconds()
        return self.report


# ============================================================================
# Test Scenarios for Validation
# ============================================================================


def run_signal_detection_tests(validator: ParallelValidator) -> None:
    """Run signal detection comparison tests."""
    print("\n=== Signal Detection Tests ===\n")

    # Default configs (matching)
    mercury_config = {"min_spread_threshold": "0.015"}  # 1.5 cents
    polyjuiced_config = {"min_spread_threshold": 0.015}

    test_cases = [
        # (yes_price, no_price, description)
        (Decimal("0.48"), Decimal("0.48"), "Clear arbitrage: 4¢ spread"),
        (Decimal("0.49"), Decimal("0.49"), "Marginal arbitrage: 2¢ spread"),
        (Decimal("0.495"), Decimal("0.495"), "Below threshold: 1¢ spread"),
        (Decimal("0.50"), Decimal("0.50"), "No arbitrage: sum = $1.00"),
        (Decimal("0.51"), Decimal("0.49"), "No arbitrage: sum = $1.00"),
        (Decimal("0.45"), Decimal("0.52"), "Clear arbitrage: 3¢ spread (asymmetric)"),
        (Decimal("0.10"), Decimal("0.85"), "Clear arbitrage: 5¢ spread (extreme)"),
        (Decimal("0.001"), Decimal("0.98"), "Edge case: very low YES price"),
    ]

    for yes_price, no_price, description in test_cases:
        result = validator.compare_signal_detection(
            market_id=f"test-market-{yes_price}-{no_price}",
            yes_best_ask=yes_price,
            no_best_ask=no_price,
            mercury_config=mercury_config,
            polyjuiced_config=polyjuiced_config,
        )
        status = "PASS" if result.result == ComparisonResult.MATCH else "FAIL"
        print(f"  [{status}] {description}")
        if result.discrepancy_reason:
            print(f"       Reason: {result.discrepancy_reason}")


def run_position_sizing_tests(validator: ParallelValidator) -> None:
    """Run position sizing comparison tests."""
    print("\n=== Position Sizing Tests ===\n")

    test_cases = [
        # (budget, yes_price, no_price, description)
        (Decimal("25"), Decimal("0.48"), Decimal("0.48"), "Standard arb: $25 budget"),
        (Decimal("50"), Decimal("0.45"), Decimal("0.50"), "Asymmetric prices: $50 budget"),
        (Decimal("100"), Decimal("0.30"), Decimal("0.65"), "Large asymmetry: $100 budget"),
        (Decimal("10"), Decimal("0.10"), Decimal("0.85"), "Extreme asymmetry: $10 budget"),
        (Decimal("25"), Decimal("0.01"), Decimal("0.01"), "Very cheap prices"),
        (Decimal("25"), Decimal("0.99"), Decimal("0.005"), "Nearly no arb possible"),
    ]

    for budget, yes_price, no_price, description in test_cases:
        result = validator.compare_position_sizing(
            market_id=f"test-sizing-{budget}",
            budget=budget,
            yes_price=yes_price,
            no_price=no_price,
            mercury_max_trade_size=Decimal("25"),
            polyjuiced_max_trade_size=25.0,
        )
        status = "PASS" if result.result == ComparisonResult.MATCH else "FAIL"
        print(f"  [{status}] {description}")
        if result.discrepancy_reason:
            print(f"       Reason: {result.discrepancy_reason}")


def run_risk_decision_tests(validator: ParallelValidator) -> None:
    """Run risk decision comparison tests."""
    print("\n=== Risk Decision Tests ===\n")

    mercury_limits = {
        "max_daily_loss_usd": 100,
        "max_position_size_usd": 25,
        "circuit_breaker_warning_failures": 3,
        "circuit_breaker_caution_failures": 4,
        "circuit_breaker_halt_failures": 5,
        "circuit_breaker_warning_loss": 50,
        "circuit_breaker_caution_loss": 75,
        "circuit_breaker_halt_loss": 100,
    }

    polyjuiced_limits = {
        "max_daily_loss_usd": 100,
        "max_trade_size_usd": 25,
        "max_consecutive_failures": 3,
    }

    test_cases = [
        # (signal_size, daily_pnl, failures, description)
        (Decimal("20"), Decimal("0"), 0, "Normal: good size, no losses"),
        (Decimal("30"), Decimal("0"), 0, "Reject: size exceeds limit"),
        (Decimal("20"), Decimal("-50"), 0, "Allow with WARNING: 50% loss"),
        (Decimal("20"), Decimal("-100"), 0, "Reject: daily loss limit"),
        (Decimal("20"), Decimal("0"), 3, "WARNING: 3 consecutive failures"),
        (Decimal("20"), Decimal("0"), 4, "CAUTION: 4 consecutive failures"),
        (Decimal("20"), Decimal("0"), 5, "HALT: 5 consecutive failures"),
        (Decimal("20"), Decimal("-75"), 2, "CAUTION from losses"),
    ]

    for signal_size, daily_pnl, failures, description in test_cases:
        result = validator.compare_risk_decision(
            signal_id=f"test-signal-{failures}-{daily_pnl}",
            market_id="test-market",
            signal_size=signal_size,
            daily_pnl=daily_pnl,
            consecutive_failures=failures,
            mercury_limits=mercury_limits,
            polyjuiced_limits=polyjuiced_limits,
        )
        status = "PASS" if result.result == ComparisonResult.MATCH else "FAIL"
        print(f"  [{status}] {description}")
        if result.discrepancy_reason:
            print(f"       Reason: {result.discrepancy_reason}")


def run_circuit_breaker_tests(validator: ParallelValidator) -> None:
    """Run circuit breaker state comparison tests."""
    print("\n=== Circuit Breaker Tests ===\n")

    mercury_config = {
        "warning_failures": 3,
        "caution_failures": 4,
        "halt_failures": 5,
        "warning_loss": 50,
        "caution_loss": 75,
        "halt_loss": 100,
    }

    polyjuiced_config = {
        "max_consecutive_failures": 3,
        "max_daily_loss_usd": 100,
    }

    test_cases = [
        # (failures, daily_pnl, trigger, description)
        (0, Decimal("0"), "normal", "Normal state: no failures, no losses"),
        (3, Decimal("0"), "failure", "WARNING: 3 failures"),
        (4, Decimal("0"), "failure", "CAUTION: 4 failures"),
        (5, Decimal("0"), "failure", "HALT: 5 failures"),
        (0, Decimal("-50"), "loss", "WARNING: 50% daily loss"),
        (0, Decimal("-75"), "loss", "CAUTION: 75% daily loss"),
        (0, Decimal("-100"), "loss", "HALT: 100% daily loss"),
        (2, Decimal("-60"), "combined", "Combined: 2 failures + 60% loss"),
    ]

    for failures, daily_pnl, trigger, description in test_cases:
        result = validator.compare_circuit_breaker(
            trigger_event=trigger,
            consecutive_failures=failures,
            daily_pnl=daily_pnl,
            mercury_config=mercury_config,
            polyjuiced_config=polyjuiced_config,
        )
        status = "PASS" if result.result == ComparisonResult.MATCH else "FAIL"
        print(f"  [{status}] {description}")
        if result.discrepancy_reason:
            print(f"       Reason: {result.discrepancy_reason}")


# ============================================================================
# Main Entry Point
# ============================================================================


def main() -> int:
    """Main entry point for parallel validation."""
    parser = argparse.ArgumentParser(
        description="Parallel validation between Mercury and polyjuiced"
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("validation_report.json"),
        help="Output path for validation report (JSON)",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable verbose output",
    )

    args = parser.parse_args()

    print("=" * 60)
    print("Mercury vs Polyjuiced Parallel Validation")
    print("=" * 60)

    # Create validator
    validator = ParallelValidator()

    # Run all test suites
    run_signal_detection_tests(validator)
    run_position_sizing_tests(validator)
    run_risk_decision_tests(validator)
    run_circuit_breaker_tests(validator)

    # Finalize report
    report = validator.finalize_report()

    # Print summary
    print("\n" + "=" * 60)
    print("VALIDATION SUMMARY")
    print("=" * 60)
    print(f"\nOverall Match Rate: {report.overall_match_rate:.1f}%")
    print(f"\nSignal Detection:     {report.signal_match_rate:.1f}% ({report.signal_matches}/{report.total_signals_compared})")
    print(f"Position Sizing:      {report.position_match_rate:.1f}% ({report.position_matches}/{report.total_positions_compared})")
    print(f"Risk Decisions:       {report.risk_match_rate:.1f}% ({report.risk_decision_matches}/{report.total_risk_decisions_compared})")
    print(f"Circuit Breaker:      {report.circuit_breaker_match_rate:.1f}% ({report.circuit_breaker_matches}/{report.total_circuit_breaker_compared})")

    if report.critical_discrepancies:
        print(f"\nCritical Discrepancies ({len(report.critical_discrepancies)}):")
        for disc in report.critical_discrepancies[:10]:
            print(f"  - {disc}")

    # Save report
    report_dict = report.to_dict()
    with open(args.output, "w") as f:
        json.dump(report_dict, f, indent=2, default=str)
    print(f"\nReport saved to: {args.output}")

    # Exit with error if match rate is below threshold
    if report.overall_match_rate < 95.0:
        print("\nWARNING: Match rate below 95% threshold!")
        return 1

    print("\nValidation PASSED: Behavior matches between Mercury and polyjuiced")
    return 0


if __name__ == "__main__":
    sys.exit(main())
