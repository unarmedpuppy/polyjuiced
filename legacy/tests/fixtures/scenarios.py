"""Test Scenarios for E2E Testing.

Defines pre-built scenarios for testing:
- Market states (order books, prices)
- Execution outcomes (fills, partial fills, failures)
- Price movements for rebalancing
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple


# =============================================================================
# Market Scenarios
# =============================================================================

@dataclass
class MarketScenario:
    """Defines initial market conditions for testing."""
    name: str
    description: str
    asset: str
    condition_id: str
    yes_token_id: str
    no_token_id: str
    slug: str

    # Order book state: [(price, size), ...]
    yes_asks: List[Tuple[float, float]] = field(default_factory=list)
    yes_bids: List[Tuple[float, float]] = field(default_factory=list)
    no_asks: List[Tuple[float, float]] = field(default_factory=list)
    no_bids: List[Tuple[float, float]] = field(default_factory=list)

    # Market timing
    minutes_until_resolution: int = 10

    @property
    def spread_cents(self) -> float:
        """Calculate spread in cents."""
        yes_ask = self.yes_asks[0][0] if self.yes_asks else 1.0
        no_ask = self.no_asks[0][0] if self.no_asks else 1.0
        return (1.0 - yes_ask - no_ask) * 100

    @property
    def yes_best_ask(self) -> float:
        return self.yes_asks[0][0] if self.yes_asks else 0.50

    @property
    def no_best_ask(self) -> float:
        return self.no_asks[0][0] if self.no_asks else 0.50

    @property
    def yes_depth(self) -> float:
        return sum(size for _, size in self.yes_asks)

    @property
    def no_depth(self) -> float:
        return sum(size for _, size in self.no_asks)

    @property
    def end_time(self) -> datetime:
        return datetime.utcnow() + timedelta(minutes=self.minutes_until_resolution)


# Pre-defined market scenarios
MARKETS: Dict[str, MarketScenario] = {
    # === Standard Arbitrage Markets ===
    "btc_3c_spread": MarketScenario(
        name="btc_3c_spread",
        description="BTC 15min market with 3 cent spread - profitable arb",
        asset="BTC",
        condition_id="0xbtc-condition-3c",
        yes_token_id="btc-yes-token-3c",
        no_token_id="btc-no-token-3c",
        slug="btc-15min-updown",
        yes_asks=[(0.48, 100), (0.49, 80), (0.50, 60)],
        yes_bids=[(0.47, 120), (0.46, 100)],
        no_asks=[(0.49, 90), (0.50, 70), (0.51, 50)],
        no_bids=[(0.48, 110), (0.47, 90)],
    ),

    "btc_4c_spread": MarketScenario(
        name="btc_4c_spread",
        description="BTC 15min market with 4 cent spread - better arb",
        asset="BTC",
        condition_id="0xbtc-condition-4c",
        yes_token_id="btc-yes-token-4c",
        no_token_id="btc-no-token-4c",
        slug="btc-15min-updown-4c",
        yes_asks=[(0.47, 100), (0.48, 80)],
        yes_bids=[(0.46, 120)],
        no_asks=[(0.49, 100), (0.50, 80)],
        no_bids=[(0.48, 120)],
    ),

    "eth_3c_spread": MarketScenario(
        name="eth_3c_spread",
        description="ETH 15min market with 3 cent spread",
        asset="ETH",
        condition_id="0xeth-condition-3c",
        yes_token_id="eth-yes-token-3c",
        no_token_id="eth-no-token-3c",
        slug="eth-15min-updown",
        yes_asks=[(0.48, 80), (0.49, 60)],
        yes_bids=[(0.47, 100)],
        no_asks=[(0.49, 70), (0.50, 50)],
        no_bids=[(0.48, 90)],
    ),

    # === Edge Case Markets ===
    "btc_low_liquidity": MarketScenario(
        name="btc_low_liquidity",
        description="BTC market with low liquidity - should fail pre-flight",
        asset="BTC",
        condition_id="0xbtc-condition-low-liq",
        yes_token_id="btc-yes-token-low-liq",
        no_token_id="btc-no-token-low-liq",
        slug="btc-15min-low-liq",
        yes_asks=[(0.48, 5)],  # Only 5 shares available
        yes_bids=[(0.47, 5)],
        no_asks=[(0.49, 3)],  # Only 3 shares available
        no_bids=[(0.48, 3)],
    ),

    "btc_no_spread": MarketScenario(
        name="btc_no_spread",
        description="BTC market with no arbitrage spread",
        asset="BTC",
        condition_id="0xbtc-condition-no-spread",
        yes_token_id="btc-yes-token-no-spread",
        no_token_id="btc-no-token-no-spread",
        slug="btc-15min-no-spread",
        yes_asks=[(0.51, 100)],
        yes_bids=[(0.50, 100)],
        no_asks=[(0.51, 100)],  # YES + NO = 1.02, negative spread
        no_bids=[(0.50, 100)],
    ),

    "btc_ending_soon": MarketScenario(
        name="btc_ending_soon",
        description="BTC market ending in 30 seconds",
        asset="BTC",
        condition_id="0xbtc-condition-ending",
        yes_token_id="btc-yes-token-ending",
        no_token_id="btc-no-token-ending",
        slug="btc-15min-ending",
        yes_asks=[(0.48, 100)],
        yes_bids=[(0.47, 100)],
        no_asks=[(0.49, 100)],
        no_bids=[(0.48, 100)],
        minutes_until_resolution=0,  # Will set to 30 seconds
    ),

    # === Asymmetric Markets ===
    "btc_deep_yes_shallow_no": MarketScenario(
        name="btc_deep_yes_shallow_no",
        description="YES has deep liquidity, NO is shallow",
        asset="BTC",
        condition_id="0xbtc-condition-asymmetric",
        yes_token_id="btc-yes-token-asym",
        no_token_id="btc-no-token-asym",
        slug="btc-15min-asymmetric",
        yes_asks=[(0.48, 500), (0.49, 300), (0.50, 200)],  # Deep
        yes_bids=[(0.47, 400)],
        no_asks=[(0.49, 20)],  # Shallow - likely to partial fill
        no_bids=[(0.48, 15)],
    ),
}


# =============================================================================
# Execution Scenarios
# =============================================================================

@dataclass
class ExecutionScenario:
    """Defines how orders execute for testing."""
    name: str
    description: str

    # Order results
    yes_result: str  # MATCHED, LIVE, FAILED, REJECTED
    yes_fill_size: float
    no_result: str
    no_fill_size: float

    # Expected outcomes
    expected_success: bool
    expected_hedge_ratio: float
    expected_execution_status: str  # full_fill, partial_fill, one_leg_only, failed
    expected_needs_rebalancing: bool


# Pre-defined execution scenarios
EXECUTION_SCENARIOS: Dict[str, ExecutionScenario] = {
    # === Perfect Execution ===
    "perfect_fill": ExecutionScenario(
        name="perfect_fill",
        description="Both legs fill completely at expected prices",
        yes_result="MATCHED",
        yes_fill_size=10.42,
        no_result="MATCHED",
        no_fill_size=10.42,
        expected_success=True,
        expected_hedge_ratio=1.0,
        expected_execution_status="full_fill",
        expected_needs_rebalancing=False,
    ),

    "perfect_fill_large": ExecutionScenario(
        name="perfect_fill_large",
        description="Large order fills completely",
        yes_result="MATCHED",
        yes_fill_size=50.0,
        no_result="MATCHED",
        no_fill_size=50.0,
        expected_success=True,
        expected_hedge_ratio=1.0,
        expected_execution_status="full_fill",
        expected_needs_rebalancing=False,
    ),

    # === Partial Fills ===
    "yes_fills_no_rejected": ExecutionScenario(
        name="yes_fills_no_rejected",
        description="YES fills but NO is rejected (FOK failure)",
        yes_result="MATCHED",
        yes_fill_size=10.42,
        no_result="FAILED",
        no_fill_size=0.0,
        expected_success=False,
        expected_hedge_ratio=0.0,
        expected_execution_status="partial_fill",
        expected_needs_rebalancing=True,
    ),

    "no_fills_yes_rejected": ExecutionScenario(
        name="no_fills_yes_rejected",
        description="NO fills but YES is rejected",
        yes_result="FAILED",
        yes_fill_size=0.0,
        no_result="MATCHED",
        no_fill_size=10.42,
        expected_success=False,
        expected_hedge_ratio=0.0,
        expected_execution_status="partial_fill",
        expected_needs_rebalancing=True,
    ),

    "partial_fill_80pct": ExecutionScenario(
        name="partial_fill_80pct",
        description="Both fill but NO only 80% - at threshold",
        yes_result="MATCHED",
        yes_fill_size=10.0,
        no_result="MATCHED",
        no_fill_size=8.0,
        expected_success=True,
        expected_hedge_ratio=0.8,
        expected_execution_status="partial_fill",
        expected_needs_rebalancing=False,  # Exactly at threshold
    ),

    "partial_fill_60pct": ExecutionScenario(
        name="partial_fill_60pct",
        description="Both fill but NO only 60% - needs rebalancing",
        yes_result="MATCHED",
        yes_fill_size=10.0,
        no_result="MATCHED",
        no_fill_size=6.0,
        expected_success=True,
        expected_hedge_ratio=0.6,
        expected_execution_status="partial_fill",
        expected_needs_rebalancing=True,
    ),

    "partial_fill_40pct": ExecutionScenario(
        name="partial_fill_40pct",
        description="Severely imbalanced fill - 40% hedge",
        yes_result="MATCHED",
        yes_fill_size=10.0,
        no_result="MATCHED",
        no_fill_size=4.0,
        expected_success=True,
        expected_hedge_ratio=0.4,
        expected_execution_status="partial_fill",
        expected_needs_rebalancing=True,
    ),

    # === Complete Failures ===
    "both_rejected": ExecutionScenario(
        name="both_rejected",
        description="Both orders rejected",
        yes_result="FAILED",
        yes_fill_size=0.0,
        no_result="FAILED",
        no_fill_size=0.0,
        expected_success=False,
        expected_hedge_ratio=0.0,
        expected_execution_status="failed",
        expected_needs_rebalancing=False,
    ),

    "both_live_then_cancelled": ExecutionScenario(
        name="both_live_then_cancelled",
        description="Both go LIVE but don't fill, get cancelled",
        yes_result="LIVE",
        yes_fill_size=0.0,
        no_result="LIVE",
        no_fill_size=0.0,
        expected_success=False,
        expected_hedge_ratio=0.0,
        expected_execution_status="failed",
        expected_needs_rebalancing=False,
    ),
}


# =============================================================================
# Price Movement Scenarios (for rebalancing)
# =============================================================================

@dataclass
class PriceMovementScenario:
    """Defines price movements over time for rebalancing tests."""
    name: str
    description: str

    # Price timeline: [(seconds_after, yes_bid, yes_ask, no_bid, no_ask), ...]
    price_timeline: List[Tuple[float, float, float, float, float]]

    # Expected rebalancing behavior
    expected_rebalance_action: Optional[str]  # SELL_YES, BUY_NO, SELL_NO, BUY_YES, None
    expected_profit_per_share: float = 0.0


# Pre-defined rebalancing scenarios
REBALANCING_SCENARIOS: Dict[str, PriceMovementScenario] = {
    # === Sell Excess Scenarios ===
    "sell_excess_yes_profitable": PriceMovementScenario(
        name="sell_excess_yes_profitable",
        description="YES price rises after partial fill - can sell excess at profit",
        price_timeline=[
            # (seconds, yes_bid, yes_ask, no_bid, no_ask)
            (0.0, 0.47, 0.48, 0.48, 0.49),   # Initial (entry YES @ 0.48)
            (1.0, 0.50, 0.51, 0.46, 0.47),   # YES rises
            (2.0, 0.52, 0.53, 0.45, 0.46),   # YES rises more - profitable to sell
        ],
        expected_rebalance_action="SELL_YES",
        expected_profit_per_share=0.04,  # 0.52 - 0.48 entry
    ),

    "sell_excess_no_profitable": PriceMovementScenario(
        name="sell_excess_no_profitable",
        description="NO price rises after partial fill - can sell excess at profit",
        price_timeline=[
            (0.0, 0.48, 0.49, 0.47, 0.48),   # Initial (entry NO @ 0.48)
            (1.0, 0.46, 0.47, 0.50, 0.51),   # NO rises
            (2.0, 0.44, 0.45, 0.53, 0.54),   # NO rises more - profitable to sell
        ],
        expected_rebalance_action="SELL_NO",
        expected_profit_per_share=0.05,  # 0.53 - 0.48 entry
    ),

    # === Buy Deficit Scenarios ===
    "buy_deficit_no_cheap": PriceMovementScenario(
        name="buy_deficit_no_cheap",
        description="NO price drops - can buy deficit cheaply",
        price_timeline=[
            (0.0, 0.47, 0.48, 0.48, 0.49),   # Initial
            (1.0, 0.49, 0.50, 0.46, 0.47),   # NO drops
            (2.0, 0.51, 0.52, 0.42, 0.43),   # NO drops more - cheap to buy
        ],
        expected_rebalance_action="BUY_NO",
        expected_profit_per_share=0.06,  # Entry was 0.49, now 0.43
    ),

    "buy_deficit_yes_cheap": PriceMovementScenario(
        name="buy_deficit_yes_cheap",
        description="YES price drops - can buy deficit cheaply",
        price_timeline=[
            (0.0, 0.47, 0.48, 0.48, 0.49),   # Initial
            (1.0, 0.44, 0.45, 0.52, 0.53),   # YES drops
            (2.0, 0.40, 0.41, 0.56, 0.57),   # YES drops more - cheap to buy
        ],
        expected_rebalance_action="BUY_YES",
        expected_profit_per_share=0.07,
    ),

    # === No Opportunity Scenarios ===
    "prices_unchanged": PriceMovementScenario(
        name="prices_unchanged",
        description="Prices stay the same - no rebalancing opportunity",
        price_timeline=[
            (0.0, 0.47, 0.48, 0.48, 0.49),
            (1.0, 0.47, 0.48, 0.48, 0.49),
            (2.0, 0.47, 0.48, 0.48, 0.49),
        ],
        expected_rebalance_action=None,
        expected_profit_per_share=0.0,
    ),

    "prices_move_against": PriceMovementScenario(
        name="prices_move_against",
        description="Prices move against us - no profitable rebalancing",
        price_timeline=[
            (0.0, 0.47, 0.48, 0.48, 0.49),   # Initial (YES entry 0.48)
            (1.0, 0.45, 0.46, 0.50, 0.51),   # YES drops, NO rises - bad for selling YES
            (2.0, 0.43, 0.44, 0.52, 0.53),   # Worse
        ],
        expected_rebalance_action=None,  # Selling YES would be a loss
        expected_profit_per_share=0.0,
    ),

    # === Volatile Market ===
    "volatile_eventually_profitable": PriceMovementScenario(
        name="volatile_eventually_profitable",
        description="Prices oscillate but eventually become profitable",
        price_timeline=[
            (0.0, 0.47, 0.48, 0.48, 0.49),
            (0.5, 0.45, 0.46, 0.50, 0.51),   # Bad
            (1.0, 0.49, 0.50, 0.47, 0.48),   # Better
            (1.5, 0.46, 0.47, 0.49, 0.50),   # Bad again
            (2.0, 0.52, 0.53, 0.44, 0.45),   # Good! Sell YES
        ],
        expected_rebalance_action="SELL_YES",
        expected_profit_per_share=0.04,
    ),
}


# =============================================================================
# Complete Test Scenarios (Market + Execution + Movement)
# =============================================================================

@dataclass
class CompleteScenario:
    """Combines market, execution, and price movement for full E2E test."""
    name: str
    description: str
    market: MarketScenario
    execution: ExecutionScenario
    price_movement: Optional[PriceMovementScenario] = None

    # Budget for the trade
    budget: float = 10.0

    # Expected final state
    expected_final_hedge_ratio: float = 0.0
    expected_trade_count: int = 1
    expected_rebalance_count: int = 0


# Pre-built complete scenarios
COMPLETE_SCENARIOS: Dict[str, CompleteScenario] = {
    "standard_arb_success": CompleteScenario(
        name="standard_arb_success",
        description="Standard successful arbitrage trade",
        market=MARKETS["btc_3c_spread"],
        execution=EXECUTION_SCENARIOS["perfect_fill"],
        budget=10.0,
        expected_final_hedge_ratio=1.0,
        expected_trade_count=1,
        expected_rebalance_count=0,
    ),

    "partial_fill_then_rebalance": CompleteScenario(
        name="partial_fill_then_rebalance",
        description="Partial fill followed by successful rebalancing",
        market=MARKETS["btc_3c_spread"],
        execution=EXECUTION_SCENARIOS["partial_fill_60pct"],
        price_movement=REBALANCING_SCENARIOS["sell_excess_yes_profitable"],
        budget=10.0,
        expected_final_hedge_ratio=1.0,  # After rebalancing
        expected_trade_count=1,
        expected_rebalance_count=1,
    ),

    "one_leg_fills_hold_to_resolution": CompleteScenario(
        name="one_leg_fills_hold_to_resolution",
        description="One leg fills, no rebalancing opportunity, hold to resolution",
        market=MARKETS["btc_3c_spread"],
        execution=EXECUTION_SCENARIOS["yes_fills_no_rejected"],
        price_movement=REBALANCING_SCENARIOS["prices_unchanged"],
        budget=10.0,
        expected_final_hedge_ratio=0.0,  # Stays imbalanced
        expected_trade_count=1,
        expected_rebalance_count=0,
    ),

    "low_liquidity_no_trade": CompleteScenario(
        name="low_liquidity_no_trade",
        description="Low liquidity prevents trade",
        market=MARKETS["btc_low_liquidity"],
        execution=EXECUTION_SCENARIOS["both_rejected"],  # Won't even try
        budget=10.0,
        expected_final_hedge_ratio=0.0,
        expected_trade_count=0,  # No trade placed
        expected_rebalance_count=0,
    ),
}
