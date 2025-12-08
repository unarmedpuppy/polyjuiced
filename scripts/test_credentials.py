#!/usr/bin/env python3
"""Test API credentials."""

from src.config import AppConfig
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds

config = AppConfig.load()
print("Creating client...")
print(f"Private key present: {bool(config.polymarket.private_key)}")
print(f"API key present: {bool(config.polymarket.api_key)}")

client = ClobClient(
    host="https://clob.polymarket.com",
    key=config.polymarket.private_key,
    chain_id=137,
    signature_type=config.polymarket.signature_type,
    funder=config.polymarket.proxy_wallet or None,
)

print("Setting API creds...")
creds = ApiCreds(
    api_key=config.polymarket.api_key,
    api_secret=config.polymarket.api_secret,
    api_passphrase=config.polymarket.api_passphrase,
)
client.set_api_creds(creds)

print("Testing get_orders...")
try:
    orders = client.get_orders()
    print(f"Success! Found {len(orders)} orders")
except Exception as e:
    print(f"Error: {e}")
