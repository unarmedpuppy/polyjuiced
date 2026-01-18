#!/usr/bin/env python3
"""Check positions from the dashboard API."""
import urllib.request
import json

def main():
    try:
        req = urllib.request.urlopen('http://127.0.0.1:8080/dashboard/positions?limit=100')
        data = json.loads(req.read())

        print("=" * 60)
        print("POSITIONS FROM DASHBOARD API")
        print("=" * 60)

        # Check what data we have
        if isinstance(data, dict):
            print(f"\nTop-level keys: {list(data.keys())}")

            positions = data.get('positions', data.get('data', []))
            if positions:
                print(f"\nTotal positions: {len(positions)}")
                total_cost = 0
                total_value = 0

                print("\nPositions (first 10):")
                for i, p in enumerate(positions[:10]):
                    cost = p.get('total_cost', 0) or p.get('cost', 0) or 0
                    value = p.get('current_value', 0) or p.get('value', 0) or 0
                    total_cost += cost
                    total_value += value
                    print(f"  {i+1}. cost=${cost:.2f}, value=${value:.2f}, {p.get('question', p.get('asset', 'N/A'))[:50]}")

                # Sum all
                for p in positions[10:]:
                    total_cost += p.get('total_cost', 0) or p.get('cost', 0) or 0
                    total_value += p.get('current_value', 0) or p.get('value', 0) or 0

                print(f"\nTOTALS:")
                print(f"  Total cost: ${total_cost:.2f}")
                print(f"  Total value: ${total_value:.2f}")
                print(f"  Unrealized P&L: ${total_value - total_cost:.2f}")

            # Check for other interesting fields
            for key in ['total_cost', 'total_value', 'untracked_count', 'total_untracked_value']:
                if key in data:
                    print(f"\n{key}: {data[key]}")

        elif isinstance(data, list):
            print(f"\nGot list with {len(data)} items")
            if data:
                print(f"First item keys: {list(data[0].keys()) if isinstance(data[0], dict) else 'N/A'}")

    except Exception as e:
        print(f"Error: {e}")
        import traceback
        traceback.print_exc()

if __name__ == '__main__':
    main()
