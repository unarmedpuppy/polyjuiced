#!/usr/bin/env python3
"""Pull trading activity from Polymarket for any user.

Usage:
    python scripts/polymarket-user-activity.py gabagool22 --days 7 --output data/gabagool22-trades.csv
    python scripts/polymarket-user-activity.py 0x6031b6eed1c97e853c6e0f03ad3ce3529351f96d --limit 1000

Uses the Polymarket Data API: https://data-api.polymarket.com
"""

import argparse
import csv
import json
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx

DATA_API_BASE = "https://data-api.polymarket.com"


def resolve_username_to_wallet(username: str) -> Optional[str]:
    """Resolve a Polymarket username to wallet address.

    Fetches the profile page and extracts the wallet address from the page data.
    """
    if username.startswith("0x") and len(username) == 42:
        return username  # Already a wallet address

    # For usernames, we need to scrape the profile page to get wallet
    # The Polymarket frontend embeds the wallet in __NEXT_DATA__
    try:
        response = httpx.get(
            f"https://polymarket.com/@{username}",
            follow_redirects=True,
            timeout=30.0,
        )
        response.raise_for_status()

        # Extract wallet from the JSON embedded in the page
        import re
        match = re.search(r'"proxyWallet":"(0x[a-fA-F0-9]{40})"', response.text)
        if match:
            return match.group(1)

        # Try another pattern
        match = re.search(r'"address":"(0x[a-fA-F0-9]{40})"', response.text)
        if match:
            return match.group(1)

    except Exception as e:
        print(f"Error resolving username {username}: {e}", file=sys.stderr)

    return None


def get_user_trades(
    wallet: str,
    limit: int = 100,
    offset: int = 0,
) -> List[Dict[str, Any]]:
    """Fetch trades for a user from the Data API."""
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
    return response.json()


def get_all_trades(
    wallet: str,
    max_trades: int = 10000,
    since_timestamp: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """Fetch all trades for a user, paginating through results."""
    all_trades = []
    offset = 0
    batch_size = 500

    while len(all_trades) < max_trades:
        print(f"Fetching trades... (offset={offset}, total={len(all_trades)})", file=sys.stderr)

        trades = get_user_trades(wallet, limit=batch_size, offset=offset)

        if not trades:
            break

        # Filter by timestamp if specified
        if since_timestamp:
            trades = [t for t in trades if t.get("timestamp", 0) >= since_timestamp]
            if not trades:
                break

        all_trades.extend(trades)

        if len(trades) < batch_size:
            break

        offset += batch_size
        time.sleep(0.5)  # Rate limiting

    return all_trades[:max_trades]


def get_user_positions(wallet: str) -> List[Dict[str, Any]]:
    """Fetch current positions for a user."""
    response = httpx.get(
        f"{DATA_API_BASE}/positions",
        params={"user": wallet},
        timeout=30.0,
    )
    response.raise_for_status()
    return response.json()


def get_user_stats(wallet: str) -> Dict[str, Any]:
    """Fetch profile stats for a user."""
    response = httpx.get(
        f"{DATA_API_BASE}/profile/stats",
        params={"user": wallet},
        timeout=30.0,
    )
    response.raise_for_status()
    return response.json()


def format_trade(trade: Dict[str, Any]) -> Dict[str, Any]:
    """Format a trade record for output."""
    timestamp = trade.get("timestamp", 0)
    dt = datetime.fromtimestamp(timestamp) if timestamp else None

    return {
        "datetime": dt.isoformat() if dt else "",
        "timestamp": timestamp,
        "side": trade.get("side", ""),
        "outcome": trade.get("outcome", ""),
        "title": trade.get("title", ""),
        "size": float(trade.get("size", 0)),
        "price": float(trade.get("price", 0)),
        "value": float(trade.get("size", 0)) * float(trade.get("price", 0)),
        "slug": trade.get("slug", ""),
        "eventSlug": trade.get("eventSlug", ""),
        "conditionId": trade.get("conditionId", ""),
        "transactionHash": trade.get("transactionHash", ""),
        "proxyWallet": trade.get("proxyWallet", ""),
    }


def save_trades_csv(trades: List[Dict[str, Any]], output_path: Path) -> None:
    """Save trades to CSV file."""
    if not trades:
        print("No trades to save", file=sys.stderr)
        return

    formatted = [format_trade(t) for t in trades]

    output_path.parent.mkdir(parents=True, exist_ok=True)

    with open(output_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=formatted[0].keys())
        writer.writeheader()
        writer.writerows(formatted)

    print(f"Saved {len(formatted)} trades to {output_path}", file=sys.stderr)


def save_trades_json(trades: List[Dict[str, Any]], output_path: Path) -> None:
    """Save trades to JSON file."""
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with open(output_path, "w") as f:
        json.dump(trades, f, indent=2)

    print(f"Saved {len(trades)} trades to {output_path}", file=sys.stderr)


def analyze_trades(trades: List[Dict[str, Any]]) -> None:
    """Print analysis summary of trades."""
    if not trades:
        print("No trades to analyze")
        return

    # Basic stats
    total_trades = len(trades)
    buy_trades = [t for t in trades if t.get("side") == "BUY"]
    sell_trades = [t for t in trades if t.get("side") == "SELL"]

    # Volume calculation
    total_volume = sum(
        float(t.get("size", 0)) * float(t.get("price", 0))
        for t in trades
    )

    # Time range
    timestamps = [t.get("timestamp", 0) for t in trades if t.get("timestamp")]
    if timestamps:
        min_ts = min(timestamps)
        max_ts = max(timestamps)
        start_date = datetime.fromtimestamp(min_ts)
        end_date = datetime.fromtimestamp(max_ts)
    else:
        start_date = end_date = None

    # Market breakdown
    markets = {}
    for t in trades:
        market = t.get("title", "Unknown")
        if market not in markets:
            markets[market] = {"count": 0, "volume": 0}
        markets[market]["count"] += 1
        markets[market]["volume"] += float(t.get("size", 0)) * float(t.get("price", 0))

    # Outcome breakdown
    up_trades = [t for t in trades if t.get("outcome", "").lower() == "up"]
    down_trades = [t for t in trades if t.get("outcome", "").lower() == "down"]

    # Price stats
    prices = [float(t.get("price", 0)) for t in trades if t.get("price")]
    avg_price = sum(prices) / len(prices) if prices else 0
    min_price = min(prices) if prices else 0
    max_price = max(prices) if prices else 0

    print("\n" + "=" * 60)
    print("TRADE ANALYSIS SUMMARY")
    print("=" * 60)
    print(f"\nTotal trades: {total_trades}")
    print(f"  - BUY: {len(buy_trades)}")
    print(f"  - SELL: {len(sell_trades)}")
    print(f"\nTotal volume: ${total_volume:,.2f}")

    if start_date and end_date:
        print(f"\nDate range: {start_date.strftime('%Y-%m-%d %H:%M')} to {end_date.strftime('%Y-%m-%d %H:%M')}")
        days = (end_date - start_date).days + 1
        print(f"  ({days} day{'s' if days != 1 else ''})")

    print(f"\nOutcome breakdown:")
    print(f"  - UP positions: {len(up_trades)}")
    print(f"  - DOWN positions: {len(down_trades)}")

    print(f"\nPrice stats:")
    print(f"  - Average: ${avg_price:.3f}")
    print(f"  - Min: ${min_price:.3f}")
    print(f"  - Max: ${max_price:.3f}")

    print(f"\nTop 5 markets by trade count:")
    sorted_markets = sorted(markets.items(), key=lambda x: x[1]["count"], reverse=True)
    for market, data in sorted_markets[:5]:
        print(f"  - {market[:50]}... ({data['count']} trades, ${data['volume']:.2f})")

    print("=" * 60 + "\n")


def main():
    parser = argparse.ArgumentParser(
        description="Pull trading activity from Polymarket for any user"
    )
    parser.add_argument(
        "user",
        help="Username (e.g. gabagool22) or wallet address (0x...)",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=7,
        help="Number of days of history to fetch (default: 7)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=10000,
        help="Maximum number of trades to fetch (default: 10000)",
    )
    parser.add_argument(
        "--output", "-o",
        type=str,
        help="Output file path (CSV or JSON based on extension)",
    )
    parser.add_argument(
        "--analyze",
        action="store_true",
        help="Print analysis summary of trades",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output raw JSON to stdout",
    )

    args = parser.parse_args()

    # Resolve username to wallet
    print(f"Resolving user: {args.user}", file=sys.stderr)
    wallet = resolve_username_to_wallet(args.user)

    if not wallet:
        print(f"Could not resolve user '{args.user}' to wallet address", file=sys.stderr)
        sys.exit(1)

    print(f"Wallet address: {wallet}", file=sys.stderr)

    # Calculate timestamp cutoff
    since_timestamp = None
    if args.days:
        cutoff = datetime.now() - timedelta(days=args.days)
        since_timestamp = int(cutoff.timestamp())
        print(f"Fetching trades since: {cutoff.isoformat()}", file=sys.stderr)

    # Fetch trades
    trades = get_all_trades(
        wallet=wallet,
        max_trades=args.limit,
        since_timestamp=since_timestamp,
    )

    print(f"Fetched {len(trades)} trades", file=sys.stderr)

    # Output
    if args.output:
        output_path = Path(args.output)
        if output_path.suffix.lower() == ".json":
            save_trades_json(trades, output_path)
        else:
            save_trades_csv(trades, output_path)

    if args.analyze:
        analyze_trades(trades)

    if args.json:
        print(json.dumps(trades, indent=2))

    # Default: print summary if no output specified
    if not args.output and not args.json:
        analyze_trades(trades)


if __name__ == "__main__":
    main()
