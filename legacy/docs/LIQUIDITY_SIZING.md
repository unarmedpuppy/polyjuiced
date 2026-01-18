# Liquidity-Aware Position Sizing

## Status

**Phase 1 Data Collection: IMPLEMENTED (Dec 2024)**

The bot now automatically collects liquidity data for building persistence and slippage models:

- **Fill Records** - Every order execution is logged with slippage data, fill ratios, and timing
- **Depth Snapshots** - Order book depth is captured every 30 seconds for all active markets
- **Database Tables** - `fill_records` and `liquidity_snapshots` in SQLite

**Next Steps:**
1. Run the bot for ~1 week to accumulate enough data
2. Analyze collected data to calculate actual persistence and slippage curves
3. Replace conservative defaults with data-driven estimates

Query data with:
```python
# Get fill statistics
stats = await db.get_slippage_stats(asset="BTC", lookback_minutes=60)

# Get depth statistics
depth = await db.get_depth_stats(asset="ETH", lookback_minutes=60)

# Get raw fill records
fills = await db.get_recent_fills(limit=100)
```

---

## Current Limitations

The current implementation uses a basic liquidity check:
```python
# Current approach (naive)
yes_liquidity = sum(float(ask.get("size", 0)) for ask in yes_asks[:3])
if yes_liquidity < yes_shares_needed * 0.8:
    reject()
```

This is vulnerable to:
1. **Phantom liquidity** - Orders pulled on touch
2. **Slippage** - Not modeled, assumes best-case
3. **Queue position** - Ignored entirely
4. **Self-induced collapse** - Our order consumes the spread

## Professional-Grade Sizing Model

### Required Data Collection

Before sizing can be robust, we need historical data:

```python
@dataclass
class LiquiditySnapshot:
    timestamp: datetime
    token_id: str
    bid_levels: List[Tuple[float, float]]  # [(price, size), ...]
    ask_levels: List[Tuple[float, float]]

@dataclass
class FillRecord:
    timestamp: datetime
    token_id: str
    intended_size: float
    filled_size: float
    intended_price: float
    actual_avg_price: float
    time_to_fill_ms: int
    slippage: float  # actual_price - intended_price
```

### Depth Persistence Weighting

Weight depth by how often it persists when touched:

```python
def calculate_persistent_depth(token_id: str, lookback_minutes: int = 60) -> float:
    """
    Calculate depth weighted by historical persistence.

    If displayed depth of 100 shares typically drops to 30 when touched,
    the persistent depth is 30, not 100.
    """
    snapshots = get_recent_snapshots(token_id, lookback_minutes)
    fills = get_recent_fills(token_id, lookback_minutes)

    # For each fill, compare pre-fill depth to actual execution
    persistence_ratios = []
    for fill in fills:
        pre_fill_depth = get_depth_at_time(snapshots, fill.timestamp - 1s)
        persistence_ratio = fill.filled_size / pre_fill_depth
        persistence_ratios.append(persistence_ratio)

    # Use conservative estimate (25th percentile)
    return np.percentile(persistence_ratios, 25) if persistence_ratios else 0.3
```

### Slippage Curve Model

Build slippage model from historical fills:

```python
def estimate_slippage(token_id: str, size: float) -> float:
    """
    Estimate slippage for a given size based on historical fills.

    Returns expected slippage in cents.
    """
    fills = get_recent_fills(token_id)

    # Group fills by size bucket
    size_buckets = [5, 10, 20, 50, 100]  # shares
    slippage_by_bucket = defaultdict(list)

    for fill in fills:
        bucket = min(b for b in size_buckets if fill.filled_size <= b)
        slippage_by_bucket[bucket].append(fill.slippage)

    # Find our bucket and return 75th percentile slippage (conservative)
    our_bucket = min(b for b in size_buckets if size <= b)
    if slippage_by_bucket[our_bucket]:
        return np.percentile(slippage_by_bucket[our_bucket], 75)

    # Default: assume 1 cent slippage per 10 shares
    return (size / 10) * 0.01
```

### Robust Position Sizing

```python
def calculate_max_position(
    token_id: str,
    current_price: float,
    target_edge: float,  # e.g., 0.02 for 2 cent spread
    max_loss_usd: float,
) -> float:
    """
    Calculate maximum position size that maintains edge after costs.

    This is the CORRECT approach:
    Max position = min(
        volume executable at (target_edge - fees - slippage - buffer),
        max_loss_usd / worst_case_loss_per_share
    )
    """
    # Get current book
    book = get_order_book(token_id)
    asks = book.get("asks", [])

    if not asks:
        return 0.0

    # Calculate persistent depth
    persistence = calculate_persistent_depth(token_id)
    displayed_depth = sum(float(a["size"]) for a in asks[:5])
    persistent_depth = displayed_depth * persistence

    # Calculate cumulative depth until spread is gone
    edge_remaining = target_edge
    executable_size = 0.0

    for price, size in asks:
        level_cost = price - current_price  # cost to lift this level
        if edge_remaining - level_cost <= 0:
            break  # no more edge

        executable_at_level = size * persistence
        executable_size += executable_at_level
        edge_remaining -= level_cost * (executable_at_level / executable_size)

    # Subtract estimated slippage
    expected_slippage = estimate_slippage(token_id, executable_size)
    if expected_slippage >= target_edge:
        return 0.0  # slippage eats all edge

    # Apply safety haircut (40% of theoretical)
    safe_size = executable_size * 0.6

    # Cap by max loss
    worst_case_loss = current_price  # lose entire position
    max_by_loss = max_loss_usd / worst_case_loss

    return min(safe_size, max_by_loss, persistent_depth)
```

### Regime Detection

Detect low-liquidity regimes and reduce size:

```python
def detect_liquidity_regime(token_id: str) -> str:
    """
    Classify current liquidity regime.

    Returns: "normal", "thin", "crisis"
    """
    recent_depth = get_recent_depth_samples(token_id, minutes=15)
    historical_depth = get_historical_depth_baseline(token_id)

    current_ratio = recent_depth.mean() / historical_depth.mean()
    current_volatility = recent_depth.std() / recent_depth.mean()

    if current_ratio < 0.3 or current_volatility > 0.5:
        return "crisis"  # depth collapsed or highly unstable
    elif current_ratio < 0.6:
        return "thin"    # reduced liquidity
    else:
        return "normal"

def get_regime_multiplier(regime: str) -> float:
    """Position size multiplier by regime."""
    return {
        "normal": 1.0,
        "thin": 0.5,
        "crisis": 0.0,  # don't trade
    }[regime]
```

## Implementation Roadmap

### Phase 1: Data Collection (Required First) - IMPLEMENTED (Dec 13, 2024)
- [x] Log all order book snapshots with timestamps (`src/liquidity/collector.py`)
- [x] Log all fill records with slippage data (`src/liquidity/models.py`)
- [x] Build historical database (`src/persistence.py` - `fill_records` and `liquidity_snapshots` tables)
- [x] Wire up collector in `main.py` and attach to strategy
- [x] Add `_take_liquidity_snapshots()` method to gabagool strategy (every 30 seconds)
- [x] Log fills from `execute_dual_leg_order()` with pre-fill depth and timing
- [ ] Accumulate ~1 week of data (running now, need time to collect)

### Phase 2: Basic Sizing Improvements
- [ ] Implement persistence weighting
- [ ] Add simple slippage estimate (1 cent per 10 shares fallback)
- [ ] Add self-induced spread check

### Phase 3: Full Model
- [ ] Train slippage curve from fill history
- [ ] Implement regime detection
- [ ] Add per-venue risk caps

## Current Workaround

Until Phase 1 data is collected, use conservative defaults:

```python
# Conservative sizing (current implementation gap)
PERSISTENCE_ESTIMATE = 0.4  # Assume 40% of displayed depth persists
SLIPPAGE_PER_10_SHARES = 0.01  # 1 cent per 10 shares
SAFETY_HAIRCUT = 0.5  # Only use 50% of calculated max
```

## Key Questions to Answer

1. **What is actual fill rate on Polymarket 15-min markets?**
   - Need fill data to know

2. **How stable is displayed depth?**
   - Need time-series snapshots to measure

3. **What is typical slippage by size?**
   - Need fill records with price improvement/degradation

Without this data, any sizing is a guess.

## References

- Market Microstructure: Maureen O'Hara
- Optimal Execution: Almgren & Chriss
- The Microstructure of Market Making: de Jong & Rindi
