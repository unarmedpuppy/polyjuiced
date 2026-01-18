#!/usr/bin/env python3
"""Get current dashboard stats directly from the stats dict."""
import sys
sys.path.insert(0, '/app/src')

from dashboard import stats

print("=" * 50)
print("CURRENT DASHBOARD STATS")
print("=" * 50)

# Show balance/P&L fields
print("\n--- BALANCE/P&L FIELDS ---")
for k, v in sorted(stats.items()):
    k_lower = k.lower()
    if any(term in k_lower for term in ['balance', 'pnl', 'value', 'profit', 'loss']):
        print(f"  {k}: {v}")

print("\n--- ALL STATS ---")
for k, v in sorted(stats.items()):
    if isinstance(v, (int, float, str, bool, type(None))):
        print(f"  {k}: {v}")
