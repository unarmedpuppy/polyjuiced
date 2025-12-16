#!/usr/bin/env python3
"""Check portfolio from Polymarket data API."""
import os
import json
import urllib.request

# Get wallet from environment
wallet = os.getenv("POLYMARKET_PROXY_WALLET")

if not wallet:
    # Try .env file
    try:
        with open("/app/.env") as f:
            for line in f:
                if line.startswith("POLYMARKET_PROXY_WALLET="):
                    wallet = line.split("=", 1)[1].strip().strip('"').strip("'")
                    break
    except:
        pass

if not wallet:
    print("ERROR: POLYMARKET_PROXY_WALLET not set")
    exit(1)

print("=" * 60)
print("PORTFOLIO CHECK")
print(f"Wallet: {wallet[:10]}...{wallet[-6:]}")
print("=" * 60)

# Check positions from data API
try:
    url = f"https://data-api.polymarket.com/positions?user={wallet}"
    req = urllib.request.Request(url)
    req.add_header("User-Agent", "Mozilla/5.0")
    with urllib.request.urlopen(req) as resp:
        positions = json.loads(resp.read())

    print(f"\nOpen Positions: {len(positions)}")

    if positions:
        total_value = 0.0
        print("\nPositions:")
        print("-" * 60)

        for i, pos in enumerate(positions[:20]):
            outcome = pos.get("outcome", "?")
            size = float(pos.get("size", 0))
            avg_price = float(pos.get("avgPrice", 0))
            market_value = float(pos.get("value", 0)) or (size * avg_price)
            question = pos.get("title", pos.get("question", "?"))[:50]

            total_value += market_value
            print(f"{i+1}. {outcome} | {size:.2f} shares @ ${avg_price:.2f} = ${market_value:.2f}")
            print(f"   {question}")

        if len(positions) > 20:
            print(f"\n... and {len(positions) - 20} more positions")
            for pos in positions[20:]:
                total_value += float(pos.get("value", 0)) or (float(pos.get("size", 0)) * float(pos.get("avgPrice", 0)))

        print("\n" + "=" * 60)
        print(f"TOTAL POSITION VALUE: ${total_value:.2f}")
    else:
        print("\nNo open positions found.")

except Exception as e:
    print(f"Error fetching positions: {e}")
    import traceback
    traceback.print_exc()

# Check recent trades
print("\n" + "=" * 60)
print("RECENT TRADES (last 7 days)")
print("=" * 60)

try:
    from datetime import datetime, timedelta
    cutoff = int((datetime.now() - timedelta(days=7)).timestamp())
    url = f"https://data-api.polymarket.com/trades?user={wallet}&limit=20"
    req = urllib.request.Request(url)
    req.add_header("User-Agent", "Mozilla/5.0")
    with urllib.request.urlopen(req) as resp:
        trades = json.loads(resp.read())

    print(f"\nRecent trades: {len(trades)}")
    for t in trades[:10]:
        side = t.get("side", "?")
        outcome = t.get("outcome", "?")
        size = float(t.get("size", 0))
        price = float(t.get("price", 0))
        ts = t.get("timestamp")
        print(f"  {side} {size:.2f} {outcome} @ ${price:.2f} (ts: {ts})")

except Exception as e:
    print(f"Error fetching trades: {e}")
