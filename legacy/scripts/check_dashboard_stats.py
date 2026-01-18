#!/usr/bin/env python3
"""Check dashboard stats for balance/P&L values."""
import urllib.request
import json

def main():
    try:
        req = urllib.request.urlopen('http://localhost:8080/dashboard/state')
        data = json.loads(req.read())

        print("=" * 50)
        print("DASHBOARD STATS - BALANCE/P&L FIELDS")
        print("=" * 50)

        # Show all balance/pnl related fields
        relevant_keys = []
        for k, v in sorted(data.items()):
            k_lower = k.lower()
            if any(term in k_lower for term in ['balance', 'pnl', 'value', 'profit', 'loss']):
                relevant_keys.append(k)
                print(f"{k}: {v}")

        print("\n" + "=" * 50)
        print("ALL STATS (for reference)")
        print("=" * 50)
        for k, v in sorted(data.items()):
            if k not in relevant_keys:
                if isinstance(v, (int, float, str, bool, type(None))):
                    print(f"{k}: {v}")
                else:
                    print(f"{k}: <{type(v).__name__}>")

    except Exception as e:
        print(f"Error: {e}")

if __name__ == '__main__':
    main()
