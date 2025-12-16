#!/usr/bin/env python3
"""Check current market spreads to see if opportunities exist."""
import os
import json
import urllib.request
import sqlite3

def get_env_var(name):
    val = os.getenv(name)
    if val:
        return val
    try:
        with open("/app/.env") as f:
            for line in f:
                if line.startswith(f"{name}="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
    except:
        pass
    return None

def main():
    print("=" * 70)
    print("CURRENT MARKET SPREADS CHECK")
    print("=" * 70)

    # Get min spread config
    min_spread = float(get_env_var("GABAGOOL_MIN_SPREAD") or "0.02")
    print(f"\nMin spread threshold: {min_spread * 100:.1f}¢")

    # Check if we have active markets in the order book tracker
    # by querying the dashboard state
    try:
        req = urllib.request.urlopen('http://127.0.0.1:8080/dashboard/state', timeout=5)
        data = json.loads(req.read())

        markets = data.get('markets', {})
        print(f"\nActive markets being tracked: {len(markets)}")

        if markets:
            print("\nMarket Spreads:")
            print("-" * 70)
            opportunities = 0

            for cid, m in sorted(markets.items(), key=lambda x: x[1].get('asset', '')):
                asset = m.get('asset', '?')
                yes_price = m.get('yes_price', 0) or m.get('up_price', 0)
                no_price = m.get('no_price', 0) or m.get('down_price', 0)

                if yes_price and no_price:
                    total = yes_price + no_price
                    spread = 1.0 - total
                    spread_cents = spread * 100

                    is_opportunity = spread >= min_spread
                    marker = "*** OPPORTUNITY ***" if is_opportunity else ""
                    if is_opportunity:
                        opportunities += 1

                    print(f"  {asset:4} | YES: ${yes_price:.3f} | NO: ${no_price:.3f} | "
                          f"Sum: ${total:.3f} | Spread: {spread_cents:.1f}¢ {marker}")
                else:
                    print(f"  {asset:4} | YES: ${yes_price:.3f} | NO: ${no_price:.3f} | "
                          f"(no prices yet)")

            print("-" * 70)
            print(f"Opportunities meeting threshold (>= {min_spread*100:.1f}¢): {opportunities}")

            if opportunities == 0:
                print("\nNO OPPORTUNITIES - This is why no trades are happening.")
                print("The market spreads are below the minimum threshold.")
        else:
            print("\nWARNING: No markets in dashboard state!")

    except Exception as e:
        print(f"Error fetching dashboard state: {e}")

    # Also check WebSocket status
    print("\n" + "=" * 70)
    print("WEBSOCKET & CONNECTION STATUS")
    print("=" * 70)

    try:
        req = urllib.request.urlopen('http://127.0.0.1:8080/dashboard/state', timeout=5)
        data = json.loads(req.read())
        stats = data.get('stats', {})

        print(f"  WebSocket: {stats.get('websocket', 'UNKNOWN')}")
        print(f"  CLOB Status: {stats.get('clob_status', 'UNKNOWN')}")
        print(f"  Wallet Balance: ${stats.get('wallet_balance', 0):.2f}")
        print(f"  Opportunities Detected: {stats.get('opportunities_detected', 0)}")
        print(f"  Opportunities Executed: {stats.get('opportunities_executed', 0)}")

    except Exception as e:
        print(f"Error: {e}")

if __name__ == '__main__':
    main()
