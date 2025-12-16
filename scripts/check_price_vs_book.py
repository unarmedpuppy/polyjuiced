#!/usr/bin/env python3
"""Compare WebSocket prices vs actual order book prices.

This diagnoses why liquidity checks fail by showing the mismatch
between what we think prices are vs what's actually in the order book.
"""
import json
import urllib.request

def fetch_order_book(token_id):
    """Fetch order book from CLOB API."""
    url = f"https://clob.polymarket.com/book?token_id={token_id}"
    req = urllib.request.Request(url)
    req.add_header("User-Agent", "Mozilla/5.0")
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read())

def main():
    print("=" * 70)
    print("PRICE VS ORDER BOOK COMPARISON")
    print("=" * 70)

    # Get dashboard state
    try:
        req = urllib.request.urlopen('http://127.0.0.1:8080/dashboard/state', timeout=5)
        data = json.loads(req.read())
        markets = data.get('markets', {})
        print(f"Monitored markets: {len(markets)}\n")
    except Exception as e:
        print(f"Failed to get dashboard state: {e}")
        return

    # We need to get token IDs from somewhere
    # Let's get them from logs or the market finder
    # For now, let's look at the recent opportunity that failed

    # From the logs, we know this token was used:
    # YES: 38293234940467616049200726120961180603341397123322595419005981538833107460192
    # NO: 58833768272753606050670613248257876490314049281388571150577440498829786910907

    test_cases = [
        {
            "name": "BTC (from recent logs)",
            "yes_token": "38293234940467616049200726120961180603341397123322595419005981538833107460192",
            "no_token": "58833768272753606050670613248257876490314049281388571150577440498829786910907",
        },
        {
            "name": "Current order book example",
            "yes_token": "87189671634987942593832507180304298278222774136291370204806037857368282301150",
            "no_token": "107062299137840304808433372188787616457919811025533687014838027707145678245074",
        },
    ]

    for case in test_cases:
        print("-" * 70)
        print(f"Checking: {case['name']}")
        print("-" * 70)

        try:
            yes_book = fetch_order_book(case['yes_token'])
            no_book = fetch_order_book(case['no_token'])
        except Exception as e:
            print(f"  ERROR: {e}")
            continue

        yes_asks = yes_book.get('asks', [])
        no_asks = no_book.get('asks', [])
        yes_bids = yes_book.get('bids', [])
        no_bids = no_book.get('bids', [])

        if yes_asks:
            best_yes_ask = min(float(a['price']) for a in yes_asks)
        else:
            best_yes_ask = None

        if no_asks:
            best_no_ask = min(float(a['price']) for a in no_asks)
        else:
            best_no_ask = None

        if yes_bids:
            best_yes_bid = max(float(b['price']) for b in yes_bids)
        else:
            best_yes_bid = None

        if no_bids:
            best_no_bid = max(float(b['price']) for b in no_bids)
        else:
            best_no_bid = None

        print(f"\n  YES Token Order Book:")
        print(f"    Best BID (someone willing to buy): ${best_yes_bid:.2f}" if best_yes_bid else "    No bids")
        print(f"    Best ASK (someone willing to sell): ${best_yes_ask:.2f}" if best_yes_ask else "    No asks")

        print(f"\n  NO Token Order Book:")
        print(f"    Best BID (someone willing to buy): ${best_no_bid:.2f}" if best_no_bid else "    No bids")
        print(f"    Best ASK (someone willing to sell): ${best_no_ask:.2f}" if best_no_ask else "    No asks")

        # To buy YES, we need to take from asks (pay the ask price)
        # To buy NO, we need to take from asks (pay the ask price)
        if best_yes_ask and best_no_ask:
            total_cost = best_yes_ask + best_no_ask
            spread_cents = (1.0 - total_cost) * 100
            print(f"\n  ARBITRAGE ANALYSIS (using best ASK prices):")
            print(f"    Cost to buy YES: ${best_yes_ask:.2f}")
            print(f"    Cost to buy NO:  ${best_no_ask:.2f}")
            print(f"    Total cost:      ${total_cost:.2f}")
            print(f"    Profit margin:   {spread_cents:.1f}¢")
            if spread_cents > 0:
                print(f"    OPPORTUNITY EXISTS!")
            else:
                print(f"    NO OPPORTUNITY (would lose money)")

        # Check liquidity at those prices
        if yes_asks and best_yes_ask:
            liq_at_best_yes = sum(float(a['size']) for a in yes_asks if float(a['price']) == best_yes_ask)
            print(f"\n  Liquidity at best YES ask (${best_yes_ask:.2f}): {liq_at_best_yes:.1f} shares")

        if no_asks and best_no_ask:
            liq_at_best_no = sum(float(a['size']) for a in no_asks if float(a['price']) == best_no_ask)
            print(f"  Liquidity at best NO ask (${best_no_ask:.2f}): {liq_at_best_no:.1f} shares")

        print()

    print("=" * 70)
    print("DIAGNOSIS")
    print("=" * 70)
    print("""
The WebSocket likely reports BID prices (what others will pay for our tokens)
but for BUYING we need ASK prices (what we must pay to get tokens).

If WebSocket shows YES=$0.08 and NO=$0.89:
- That might be the BID prices (what we could SELL at)
- But to BUY, we need ASK prices which are typically higher

FIX: Use order book ASK prices when calculating actual trade costs,
not WebSocket prices (which may be bids or midpoints).
""")

if __name__ == '__main__':
    main()
