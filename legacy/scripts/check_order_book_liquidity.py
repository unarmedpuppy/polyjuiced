#!/usr/bin/env python3
"""Check actual order book liquidity for monitored markets.

This script fetches real order books and shows what's actually available,
helping diagnose why liquidity checks are failing.
"""
import os
import json
import urllib.request

def get_env_var(name):
    """Get environment variable."""
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

def fetch_order_book(token_id):
    """Fetch order book from CLOB API."""
    url = f"https://clob.polymarket.com/book?token_id={token_id}"
    req = urllib.request.Request(url)
    req.add_header("User-Agent", "Mozilla/5.0")
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read())

def main():
    print("=" * 70)
    print("ORDER BOOK LIQUIDITY DIAGNOSTIC")
    print("=" * 70)

    print("Using direct CLOB API calls...\n")

    # Get dashboard state for monitored markets
    try:
        req = urllib.request.urlopen('http://127.0.0.1:8080/dashboard/state', timeout=5)
        data = json.loads(req.read())
        markets = data.get('markets', {})
        print(f"Monitored markets: {len(markets)}\n")
    except Exception as e:
        print(f"Failed to get dashboard state: {e}")
        return

    if not markets:
        print("No markets being monitored!")
        return

    # For each market, fetch and analyze order book
    for cid, m in list(markets.items())[:5]:  # Limit to first 5
        asset = m.get('asset', '?')
        yes_token = m.get('yes_token_id')
        no_token = m.get('no_token_id')
        ws_yes_price = m.get('yes_price', 0) or m.get('up_price', 0)
        ws_no_price = m.get('no_price', 0) or m.get('down_price', 0)

        print("-" * 70)
        print(f"Market: {asset}")
        print(f"  WebSocket prices: YES=${ws_yes_price:.3f}, NO=${ws_no_price:.3f}")

        if not yes_token or not no_token:
            print(f"  ✗ Missing token IDs")
            continue

        # Fetch order books
        try:
            yes_book = fetch_order_book(yes_token)
            no_book = fetch_order_book(no_token)
        except Exception as e:
            print(f"  ✗ Failed to fetch order book: {e}")
            continue

        # Analyze YES book
        print(f"\n  YES Order Book:")
        yes_asks = yes_book.get('asks', []) if isinstance(yes_book, dict) else getattr(yes_book, 'asks', []) or []
        yes_bids = yes_book.get('bids', []) if isinstance(yes_book, dict) else getattr(yes_book, 'bids', []) or []

        print(f"    Total asks (sells available to us): {len(yes_asks)}")
        print(f"    Total bids (buys available): {len(yes_bids)}")

        if yes_asks:
            print(f"    Top 3 asks (we buy from these):")
            for i, ask in enumerate(yes_asks[:3]):
                price = float(ask.get("price", 0) if isinstance(ask, dict) else getattr(ask, "price", 0))
                size = float(ask.get("size", 0) if isinstance(ask, dict) else getattr(ask, "size", 0))
                usd_value = price * size
                at_ws_price = "✓ AT/BELOW WS PRICE" if price <= ws_yes_price else f"ABOVE (need <= {ws_yes_price:.3f})"
                print(f"      ${price:.3f} x {size:.1f} shares = ${usd_value:.2f} {at_ws_price}")

            # Calculate total available at WebSocket price
            total_at_price = sum(
                float(a.get("size", 0) if isinstance(a, dict) else getattr(a, "size", 0))
                for a in yes_asks
                if float(a.get("price", 0) if isinstance(a, dict) else getattr(a, "price", 0)) <= ws_yes_price
            )
            print(f"    Total available at WS price (${ws_yes_price:.3f}): {total_at_price:.1f} shares")
        else:
            print(f"    ✗ NO ASKS - nobody selling YES tokens!")

        # Analyze NO book
        print(f"\n  NO Order Book:")
        no_asks = no_book.get('asks', []) if isinstance(no_book, dict) else getattr(no_book, 'asks', []) or []
        no_bids = no_book.get('bids', []) if isinstance(no_book, dict) else getattr(no_book, 'bids', []) or []

        print(f"    Total asks (sells available to us): {len(no_asks)}")
        print(f"    Total bids (buys available): {len(no_bids)}")

        if no_asks:
            print(f"    Top 3 asks (we buy from these):")
            for i, ask in enumerate(no_asks[:3]):
                price = float(ask.get("price", 0) if isinstance(ask, dict) else getattr(ask, "price", 0))
                size = float(ask.get("size", 0) if isinstance(ask, dict) else getattr(ask, "size", 0))
                usd_value = price * size
                at_ws_price = "✓ AT/BELOW WS PRICE" if price <= ws_no_price else f"ABOVE (need <= {ws_no_price:.3f})"
                print(f"      ${price:.3f} x {size:.1f} shares = ${usd_value:.2f} {at_ws_price}")

            # Calculate total available at WebSocket price
            total_at_price = sum(
                float(a.get("size", 0) if isinstance(a, dict) else getattr(a, "size", 0))
                for a in no_asks
                if float(a.get("price", 0) if isinstance(a, dict) else getattr(a, "price", 0)) <= ws_no_price
            )
            print(f"    Total available at WS price (${ws_no_price:.3f}): {total_at_price:.1f} shares")
        else:
            print(f"    ✗ NO ASKS - nobody selling NO tokens!")

        # Summary for this market
        print(f"\n  DIAGNOSIS:")
        if ws_yes_price and ws_no_price:
            spread = 1.0 - (ws_yes_price + ws_no_price)
            print(f"    Spread: {spread*100:.1f}¢")

            yes_liq_at_price = sum(
                float(a.get("size", 0) if isinstance(a, dict) else getattr(a, "size", 0))
                for a in yes_asks
                if float(a.get("price", 0) if isinstance(a, dict) else getattr(a, "price", 0)) <= ws_yes_price
            ) if yes_asks else 0

            no_liq_at_price = sum(
                float(a.get("size", 0) if isinstance(a, dict) else getattr(a, "size", 0))
                for a in no_asks
                if float(a.get("price", 0) if isinstance(a, dict) else getattr(a, "price", 0)) <= ws_no_price
            ) if no_asks else 0

            if yes_liq_at_price == 0 or no_liq_at_price == 0:
                print(f"    ✗ WOULD FAIL LIQUIDITY CHECK")
                if yes_liq_at_price == 0:
                    if yes_asks:
                        best_ask = min(float(a.get("price", 0) if isinstance(a, dict) else getattr(a, "price", 0)) for a in yes_asks)
                        print(f"      YES: Best ask is ${best_ask:.3f} but WS shows ${ws_yes_price:.3f}")
                        print(f"      The WebSocket price is stale or best offer moved!")
                    else:
                        print(f"      YES: No asks at all in order book")
                if no_liq_at_price == 0:
                    if no_asks:
                        best_ask = min(float(a.get("price", 0) if isinstance(a, dict) else getattr(a, "price", 0)) for a in no_asks)
                        print(f"      NO: Best ask is ${best_ask:.3f} but WS shows ${ws_no_price:.3f}")
                        print(f"      The WebSocket price is stale or best offer moved!")
                    else:
                        print(f"      NO: No asks at all in order book")
            else:
                min_liq = min(yes_liq_at_price, no_liq_at_price)
                min_trade = float(get_env_var("GABAGOOL_MIN_TRADE_SIZE") or "2.0")
                print(f"    ✓ Liquidity available: YES={yes_liq_at_price:.1f}, NO={no_liq_at_price:.1f}")
                print(f"    Min trade size: ${min_trade:.2f}")

    print("\n" + "=" * 70)
    print("CONCLUSION")
    print("=" * 70)
    print("""
If markets show "Best ask is $X but WS shows $Y":
- WebSocket prices are being parsed incorrectly, OR
- Prices moved between detection and execution (very fast markets)

If markets show "No asks at all":
- Very illiquid market OR
- Order book fetch is failing

POTENTIAL FIXES:
1. Use fresh order book prices for execution instead of WebSocket prices
2. Add price buffer (accept asks up to X% above WebSocket price)
3. Re-check opportunity using fresh order book before executing
""")

if __name__ == '__main__':
    main()
