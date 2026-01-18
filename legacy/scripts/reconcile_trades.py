#!/usr/bin/env python3
"""Reconcile local trade database with Polymarket API.

This script fetches actual trades from the Polymarket Data API and compares
them with the local database. It identifies:
1. Untracked trades (on Polymarket but not in local DB)
2. Missing positions that need to be added to settlement queue
3. Discrepancies between expected and actual execution

Usage:
    python scripts/reconcile_trades.py                    # Show discrepancies
    python scripts/reconcile_trades.py --fix              # Add missing trades to DB
    python scripts/reconcile_trades.py --json             # Output as JSON
    python scripts/reconcile_trades.py --days 7           # Check last 7 days

This is a CRITICAL observability tool. The bot has a bug where partial fills
(first leg executes, second leg fails) are not tracked, resulting in unknown
positions that can expire worthless if not monitored.
"""

import argparse
import asyncio
import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import httpx

# Add src to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

DATA_API_BASE = "https://data-api.polymarket.com"


def get_wallet_address() -> str:
    """Get wallet address from environment."""
    wallet = os.getenv("POLYMARKET_PROXY_WALLET")
    if not wallet:
        # Try to read from .env file
        env_path = Path(__file__).parent.parent / ".env"
        if env_path.exists():
            with open(env_path) as f:
                for line in f:
                    if line.startswith("POLYMARKET_PROXY_WALLET="):
                        wallet = line.split("=", 1)[1].strip().strip('"').strip("'")
                        break
    if not wallet:
        raise ValueError("POLYMARKET_PROXY_WALLET not set")
    return wallet


def fetch_polymarket_trades(
    wallet: str,
    since_timestamp: Optional[int] = None,
    limit: int = 500,
) -> List[Dict[str, Any]]:
    """Fetch trades from Polymarket Data API."""
    all_trades = []
    offset = 0

    while True:
        params = {
            "user": wallet,
            "limit": limit,
            "offset": offset,
        }

        response = httpx.get(
            f"{DATA_API_BASE}/trades",
            params=params,
            timeout=30.0,
        )
        response.raise_for_status()
        trades = response.json()

        if not trades:
            break

        # Filter by timestamp if specified
        if since_timestamp:
            trades = [t for t in trades if t.get("timestamp", 0) >= since_timestamp]
            if not trades:
                break

        all_trades.extend(trades)

        if len(trades) < limit:
            break

        offset += limit

    return all_trades


def get_local_trades(db_path: str) -> List[Dict[str, Any]]:
    """Get trades from local database."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    # Get all trades
    cursor.execute("SELECT * FROM trades ORDER BY created_at DESC")
    trades = [dict(row) for row in cursor.fetchall()]

    conn.close()
    return trades


def get_settlement_queue(db_path: str) -> List[Dict[str, Any]]:
    """Get positions from settlement queue."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    cursor.execute("SELECT * FROM settlement_queue WHERE claimed = 0")
    positions = [dict(row) for row in cursor.fetchall()]

    conn.close()
    return positions


def analyze_polymarket_trades(trades: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Analyze trades and group by market."""
    from collections import defaultdict

    markets = defaultdict(lambda: {
        "up_trades": [],
        "down_trades": [],
        "up_shares": 0.0,
        "down_shares": 0.0,
        "up_cost": 0.0,
        "down_cost": 0.0,
        "title": "",
        "condition_id": "",
    })

    for t in trades:
        cid = t.get("conditionId", "")
        outcome = t.get("outcome", "").lower()
        size = float(t.get("size", 0))
        price = float(t.get("price", 0))
        cost = size * price

        markets[cid]["condition_id"] = cid
        markets[cid]["title"] = t.get("title", "")

        if outcome == "up":
            markets[cid]["up_trades"].append(t)
            markets[cid]["up_shares"] += size
            markets[cid]["up_cost"] += cost
        elif outcome == "down":
            markets[cid]["down_trades"].append(t)
            markets[cid]["down_shares"] += size
            markets[cid]["down_cost"] += cost

    return dict(markets)


def find_untracked_positions(
    polymarket_markets: Dict[str, Any],
    local_trades: List[Dict[str, Any]],
    settlement_queue: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Find positions on Polymarket not tracked locally."""
    # Get condition IDs we're tracking
    tracked_conditions = set()
    for t in local_trades:
        if t.get("condition_id"):
            tracked_conditions.add(t["condition_id"])
    for p in settlement_queue:
        if p.get("condition_id"):
            tracked_conditions.add(p["condition_id"])

    untracked = []
    for cid, data in polymarket_markets.items():
        if cid not in tracked_conditions:
            # This position is on Polymarket but not in our database
            untracked.append({
                "condition_id": cid,
                "title": data["title"],
                "up_shares": data["up_shares"],
                "down_shares": data["down_shares"],
                "up_cost": data["up_cost"],
                "down_cost": data["down_cost"],
                "total_cost": data["up_cost"] + data["down_cost"],
                "is_hedged": data["up_shares"] > 0 and data["down_shares"] > 0,
                "up_trades": len(data["up_trades"]),
                "down_trades": len(data["down_trades"]),
            })

    return untracked


def add_to_settlement_queue(
    db_path: str,
    positions: List[Dict[str, Any]],
    polymarket_markets: Dict[str, Any],
) -> int:
    """Add untracked positions to settlement queue for monitoring."""
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    added = 0

    # Use a past timestamp for reconciled trades (they're already resolved or should be monitored)
    # We set market_end_time to the most recent trade timestamp
    default_end_time = datetime.now().isoformat()

    for pos in positions:
        cid = pos["condition_id"]
        market_data = polymarket_markets.get(cid, {})

        # Get the most recent trade timestamp for this market
        all_trades = market_data.get("up_trades", []) + market_data.get("down_trades", [])
        if all_trades:
            latest_ts = max(t.get("timestamp", 0) for t in all_trades)
            # Market end time is typically shortly after last trade for 15-min markets
            market_end_time = datetime.fromtimestamp(latest_ts).isoformat()
        else:
            market_end_time = default_end_time

        # Add UP position if exists
        if pos["up_shares"] > 0:
            # Get token_id from first trade
            up_trades = market_data.get("up_trades", [])
            if up_trades:
                token_id = str(up_trades[0].get("asset", ""))
                try:
                    cursor.execute("""
                        INSERT OR IGNORE INTO settlement_queue
                        (condition_id, token_id, shares, entry_price, entry_cost,
                         side, asset, trade_id, market_end_time)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (
                        cid,
                        token_id,
                        pos["up_shares"],
                        pos["up_cost"] / pos["up_shares"] if pos["up_shares"] > 0 else 0,
                        pos["up_cost"],
                        "YES",  # Use YES/NO to match schema comment
                        "RECONCILED",
                        f"reconcile_{cid[:8]}_up",
                        market_end_time,
                    ))
                    if cursor.rowcount > 0:
                        added += 1
                except sqlite3.IntegrityError:
                    pass  # Already exists

        # Add DOWN position if exists
        if pos["down_shares"] > 0:
            down_trades = market_data.get("down_trades", [])
            if down_trades:
                token_id = str(down_trades[0].get("asset", ""))
                try:
                    cursor.execute("""
                        INSERT OR IGNORE INTO settlement_queue
                        (condition_id, token_id, shares, entry_price, entry_cost,
                         side, asset, trade_id, market_end_time)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (
                        cid,
                        token_id,
                        pos["down_shares"],
                        pos["down_cost"] / pos["down_shares"] if pos["down_shares"] > 0 else 0,
                        pos["down_cost"],
                        "NO",  # Use YES/NO to match schema comment
                        "RECONCILED",
                        f"reconcile_{cid[:8]}_down",
                        market_end_time,
                    ))
                    if cursor.rowcount > 0:
                        added += 1
                except sqlite3.IntegrityError:
                    pass

    conn.commit()
    conn.close()
    return added


def print_summary(
    polymarket_trades: List[Dict[str, Any]],
    polymarket_markets: Dict[str, Any],
    local_trades: List[Dict[str, Any]],
    settlement_queue: List[Dict[str, Any]],
    untracked: List[Dict[str, Any]],
) -> None:
    """Print reconciliation summary."""
    print("=" * 70)
    print("TRADE RECONCILIATION REPORT")
    print("=" * 70)
    print()

    print("📊 SUMMARY")
    print("-" * 40)
    print(f"Polymarket trades found: {len(polymarket_trades)}")
    print(f"Polymarket markets: {len(polymarket_markets)}")
    print(f"Local trades in DB: {len(local_trades)}")
    print(f"Settlement queue items: {len(settlement_queue)}")
    print(f"UNTRACKED positions: {len(untracked)}")
    print()

    if untracked:
        print("⚠️  UNTRACKED POSITIONS (on Polymarket but NOT in local DB)")
        print("-" * 70)

        total_untracked_value = 0
        for pos in untracked:
            status = "HEDGED" if pos["is_hedged"] else "ONE-SIDED ⚠️"
            print(f"\n{pos['title'][:60]}...")
            print(f"  Condition: {pos['condition_id'][:20]}...")
            print(f"  Status: {status}")
            print(f"  UP: {pos['up_shares']:.1f} shares (${pos['up_cost']:.2f})")
            print(f"  DOWN: {pos['down_shares']:.1f} shares (${pos['down_cost']:.2f})")
            print(f"  Total invested: ${pos['total_cost']:.2f}")
            total_untracked_value += pos["total_cost"]

        print()
        print(f"💰 TOTAL UNTRACKED VALUE: ${total_untracked_value:.2f}")
        print()
        print("⚠️  These positions are NOT being monitored for settlement!")
        print("   Run with --fix to add them to the settlement queue.")
    else:
        print("✅ All positions are tracked! No discrepancies found.")

    # Show tracked positions
    if settlement_queue:
        print()
        print("📋 SETTLEMENT QUEUE (tracked positions)")
        print("-" * 40)
        for p in settlement_queue[:10]:
            print(f"  {p.get('side', '?')} {p.get('shares', 0):.1f} shares @ ${p.get('entry_price', 0):.3f}")


def main():
    parser = argparse.ArgumentParser(
        description="Reconcile local trade database with Polymarket API"
    )
    parser.add_argument(
        "--days",
        type=int,
        default=7,
        help="Number of days to look back (default: 7)",
    )
    parser.add_argument(
        "--fix",
        action="store_true",
        help="Add untracked positions to settlement queue",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output as JSON",
    )
    parser.add_argument(
        "--db",
        type=str,
        default="/app/data/gabagool.db",
        help="Path to database (default: /app/data/gabagool.db)",
    )

    args = parser.parse_args()

    # Get wallet address
    try:
        wallet = get_wallet_address()
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    if not args.json:
        print(f"Wallet: {wallet}")
        print(f"Looking back {args.days} days...")
        print()

    # Calculate timestamp cutoff
    cutoff = datetime.now() - timedelta(days=args.days)
    since_timestamp = int(cutoff.timestamp())

    # Fetch Polymarket trades
    if not args.json:
        print("Fetching trades from Polymarket API...")
    polymarket_trades = fetch_polymarket_trades(wallet, since_timestamp)

    # Analyze Polymarket trades
    polymarket_markets = analyze_polymarket_trades(polymarket_trades)

    # Get local data
    local_trades = get_local_trades(args.db)
    settlement_queue = get_settlement_queue(args.db)

    # Find untracked positions
    untracked = find_untracked_positions(
        polymarket_markets, local_trades, settlement_queue
    )

    if args.json:
        result = {
            "wallet": wallet,
            "days_checked": args.days,
            "polymarket_trades": len(polymarket_trades),
            "polymarket_markets": len(polymarket_markets),
            "local_trades": len(local_trades),
            "settlement_queue": len(settlement_queue),
            "untracked_positions": untracked,
            "untracked_count": len(untracked),
            "total_untracked_value": sum(p["total_cost"] for p in untracked),
        }
        print(json.dumps(result, indent=2))
    else:
        print_summary(
            polymarket_trades,
            polymarket_markets,
            local_trades,
            settlement_queue,
            untracked,
        )

        if args.fix and untracked:
            print()
            print("🔧 FIXING: Adding untracked positions to settlement queue...")
            added = add_to_settlement_queue(args.db, untracked, polymarket_markets)
            print(f"   Added {added} positions to settlement queue")
            print("   These will now be monitored for settlement.")


if __name__ == "__main__":
    main()
