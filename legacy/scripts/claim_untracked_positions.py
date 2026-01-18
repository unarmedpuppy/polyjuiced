#!/usr/bin/env python3
"""Claim winnings from untracked resolved positions.

This script:
1. Fetches all wallet positions from Polymarket API
2. Identifies resolved markets (market has ended)
3. Determines the winning outcome by checking current prices (~$0.99 = winner)
4. Sells winning shares to claim USDC proceeds

Usage:
    # Dry run (show what would be claimed)
    python scripts/claim_untracked_positions.py --dry-run

    # Actually claim
    python scripts/claim_untracked_positions.py
"""

import asyncio
import os
import sys
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_DOWN
from pathlib import Path

import httpx

# Add src to path - must add parent for proper imports
src_path = Path(__file__).parent.parent / "src"
sys.path.insert(0, str(src_path.parent))  # Add apps/polymarket-bot
sys.path.insert(0, str(src_path))          # Add apps/polymarket-bot/src

from src.client.polymarket import PolymarketClient
from src.config import load_config
import structlog

log = structlog.get_logger()


async def get_wallet_positions(wallet: str, days: int = 14) -> dict:
    """Fetch all positions from Polymarket data API."""
    DATA_API_BASE = "https://data-api.polymarket.com"
    cutoff = datetime.now() - timedelta(days=days)
    since_timestamp = int(cutoff.timestamp())

    all_trades = []
    offset = 0
    limit = 500

    async with httpx.AsyncClient(timeout=30.0) as client:
        while True:
            params = {
                "user": wallet,
                "limit": limit,
                "offset": offset,
            }
            response = await client.get(f"{DATA_API_BASE}/trades", params=params)
            response.raise_for_status()
            trades = response.json()

            if not trades:
                break

            # Filter by timestamp
            trades = [t for t in trades if t.get("timestamp", 0) >= since_timestamp]
            if not trades:
                break

            all_trades.extend(trades)

            if len(trades) < limit:
                break

            offset += limit

    # Aggregate by condition_id
    positions = {}
    for t in all_trades:
        cid = t.get("conditionId", "")
        if cid not in positions:
            positions[cid] = {
                "condition_id": cid,
                "title": t.get("title", ""),
                "up_shares": 0.0,
                "down_shares": 0.0,
                "up_token_id": None,
                "down_token_id": None,
                "up_cost": 0.0,
                "down_cost": 0.0,
            }

        outcome = t.get("outcome", "").lower()
        size = float(t.get("size", 0))
        price = float(t.get("price", 0))
        cost = size * price
        token_id = t.get("asset", "")  # Token ID is in the 'asset' field

        # Handle buy vs sell
        side = t.get("side", "").upper()
        if side == "SELL":
            size = -size
            cost = -cost

        if outcome == "up":
            positions[cid]["up_shares"] += size
            positions[cid]["up_cost"] += cost
            if token_id:
                positions[cid]["up_token_id"] = token_id
        elif outcome == "down":
            positions[cid]["down_shares"] += size
            positions[cid]["down_cost"] += cost
            if token_id:
                positions[cid]["down_token_id"] = token_id

    # Filter to positions with shares > 0
    return {
        cid: p for cid, p in positions.items()
        if p["up_shares"] > 0.01 or p["down_shares"] > 0.01
    }


async def get_market_resolution(client: PolymarketClient, token_id: str) -> dict:
    """Check if a market is resolved by looking at current prices.

    After resolution:
    - Winning side trades at ~$0.99
    - Losing side trades at ~$0.01
    """
    try:
        book = await client.get_order_book(token_id)

        # Get best bid (what we'd sell at)
        bids = book.get("bids", [])
        if not bids:
            return {"resolved": False, "is_winner": False, "best_bid": 0}

        best_bid = float(bids[0].get("price", 0))

        # If best bid is >= 0.95, this is the winning side
        # If best bid is <= 0.05, this is the losing side
        if best_bid >= 0.95:
            return {"resolved": True, "is_winner": True, "best_bid": best_bid}
        elif best_bid <= 0.05:
            return {"resolved": True, "is_winner": False, "best_bid": best_bid}
        else:
            return {"resolved": False, "is_winner": False, "best_bid": best_bid}

    except Exception as e:
        log.error("Failed to check market resolution", token_id=token_id[:20], error=str(e))
        return {"resolved": False, "is_winner": False, "best_bid": 0, "error": str(e)}


async def claim_position(client: PolymarketClient, token_id: str, shares: float, dry_run: bool = True) -> dict:
    """Claim a resolved position by selling at $0.99."""
    if dry_run:
        proceeds = shares * 0.99
        return {
            "success": True,
            "proceeds": proceeds,
            "dry_run": True,
        }

    return await client.claim_resolved_position(token_id, shares)


async def main():
    import argparse
    parser = argparse.ArgumentParser(description="Claim untracked resolved positions")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be claimed without executing")
    parser.add_argument("--days", type=int, default=14, help="Days of history to check (default: 14)")
    args = parser.parse_args()

    # Load config and get wallet
    config = load_config()
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
        print("ERROR: POLYMARKET_PROXY_WALLET not configured")
        sys.exit(1)

    print(f"{'='*60}")
    print(f"  CLAIM UNTRACKED POSITIONS {'(DRY RUN)' if args.dry_run else ''}")
    print(f"{'='*60}")
    print(f"Wallet: {wallet}")
    print(f"Checking last {args.days} days of trades...")
    print()

    # Initialize client
    client = PolymarketClient(config.polymarket)
    await client.connect()

    # Get all positions
    print("Fetching wallet positions from Polymarket...")
    positions = await get_wallet_positions(wallet, args.days)
    print(f"Found {len(positions)} positions with shares\n")

    # Check each position for resolution
    claimable = []
    total_potential_proceeds = 0.0

    for cid, pos in positions.items():
        title = pos["title"][:50] if pos["title"] else cid[:20]

        # Check UP side
        if pos["up_shares"] > 0.01 and pos["up_token_id"]:
            resolution = await get_market_resolution(client, pos["up_token_id"])
            if resolution["resolved"] and resolution["is_winner"]:
                proceeds = pos["up_shares"] * 0.99
                claimable.append({
                    "title": pos["title"],
                    "side": "UP",
                    "shares": pos["up_shares"],
                    "token_id": pos["up_token_id"],
                    "best_bid": resolution["best_bid"],
                    "proceeds": proceeds,
                })
                total_potential_proceeds += proceeds
                print(f"✅ CLAIMABLE: {title}")
                print(f"   UP: {pos['up_shares']:.2f} shares @ ${resolution['best_bid']:.2f} = ${proceeds:.2f}")
            elif resolution["resolved"]:
                print(f"❌ LOST: {title}")
                print(f"   UP: {pos['up_shares']:.2f} shares (worthless)")

        # Check DOWN side
        if pos["down_shares"] > 0.01 and pos["down_token_id"]:
            resolution = await get_market_resolution(client, pos["down_token_id"])
            if resolution["resolved"] and resolution["is_winner"]:
                proceeds = pos["down_shares"] * 0.99
                claimable.append({
                    "title": pos["title"],
                    "side": "DOWN",
                    "shares": pos["down_shares"],
                    "token_id": pos["down_token_id"],
                    "best_bid": resolution["best_bid"],
                    "proceeds": proceeds,
                })
                total_potential_proceeds += proceeds
                print(f"✅ CLAIMABLE: {title}")
                print(f"   DOWN: {pos['down_shares']:.2f} shares @ ${resolution['best_bid']:.2f} = ${proceeds:.2f}")
            elif resolution["resolved"]:
                print(f"❌ LOST: {title}")
                print(f"   DOWN: {pos['down_shares']:.2f} shares (worthless)")

        # Small delay to avoid rate limiting
        await asyncio.sleep(0.1)

    print()
    print(f"{'='*60}")
    print(f"  SUMMARY")
    print(f"{'='*60}")
    print(f"Claimable positions: {len(claimable)}")
    print(f"Potential proceeds: ${total_potential_proceeds:.2f}")
    print()

    if not claimable:
        print("No positions to claim.")
        return

    if args.dry_run:
        print("DRY RUN - No orders placed.")
        print("Run without --dry-run to actually claim.")
        return

    # Actually claim
    print("Claiming positions...")
    print()

    total_claimed = 0.0
    for pos in claimable:
        print(f"Claiming {pos['side']} {pos['shares']:.2f} shares of {pos['title'][:40]}...")
        result = await claim_position(client, pos["token_id"], pos["shares"], dry_run=False)

        if result["success"]:
            total_claimed += result["proceeds"]
            print(f"  ✅ Claimed ${result['proceeds']:.2f}")
        else:
            print(f"  ❌ Failed: {result.get('error', 'Unknown error')}")

        await asyncio.sleep(0.5)  # Rate limit

    print()
    print(f"{'='*60}")
    print(f"  COMPLETE")
    print(f"{'='*60}")
    print(f"Total claimed: ${total_claimed:.2f}")


if __name__ == "__main__":
    asyncio.run(main())
