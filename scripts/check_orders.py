#!/usr/bin/env python3
"""Check open orders on Polymarket."""

import os
import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds

client = ClobClient(
    host=os.getenv('POLYMARKET_CLOB_HTTP_URL', 'https://clob.polymarket.com'),
    key=os.getenv('POLYMARKET_PRIVATE_KEY'),
    chain_id=137,
    signature_type=int(os.getenv('POLYMARKET_SIGNATURE_TYPE', '1')),
    funder=os.getenv('POLYMARKET_PROXY_WALLET') or None,
)

if os.getenv('POLYMARKET_API_KEY'):
    creds = ApiCreds(
        api_key=os.getenv('POLYMARKET_API_KEY'),
        api_secret=os.getenv('POLYMARKET_API_SECRET'),
        api_passphrase=os.getenv('POLYMARKET_API_PASSPHRASE'),
    )
    client.set_api_creds(creds)

client.get_ok()
print("Connected!")

# Get balance
from py_clob_client.clob_types import BalanceAllowanceParams
params = BalanceAllowanceParams(
    asset_type="COLLATERAL",
    signature_type=int(os.getenv('POLYMARKET_SIGNATURE_TYPE', '1'))
)
balance_info = client.get_balance_allowance(params)
balance = float(balance_info.get('balance', 0)) / 1e6
print(f"\nBalance: ${balance:.2f}")

# Get open orders
print("\nOpen orders:")
orders = client.get_orders()
for o in orders:
    print(f"  {o}")

if not orders:
    print("  (none)")

# Get recent trades
print("\nRecent trades:")
trades = client.get_trades()
for t in trades[:5]:  # Last 5 trades
    print(f"  {t}")

if not trades:
    print("  (none)")
