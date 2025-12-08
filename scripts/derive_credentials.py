#!/usr/bin/env python3
"""
Derive Polymarket API credentials from your private key.

Usage:
    python derive_credentials.py

This will prompt for your private key and derive the API credentials
needed for authenticated CLOB API access.

The output can be added to your .env file:
    POLYMARKET_API_KEY=...
    POLYMARKET_API_SECRET=...
    POLYMARKET_API_PASSPHRASE=...
"""

import getpass
import sys

from py_clob_client.client import ClobClient


def main():
    print("=" * 60)
    print("Polymarket API Credential Derivation Tool")
    print("=" * 60)
    print()
    print("This tool will derive API credentials from your private key.")
    print("Your private key is NOT stored or transmitted anywhere.")
    print()

    # Get private key securely (hidden input)
    private_key = getpass.getpass("Enter your Polygon wallet private key: ")

    if not private_key:
        print("Error: Private key is required")
        sys.exit(1)

    # Clean up the key (remove 0x prefix if present, whitespace)
    private_key = private_key.strip()
    if not private_key.startswith("0x"):
        private_key = "0x" + private_key

    print()
    print("Connecting to Polymarket...")

    try:
        # Create client with private key
        client = ClobClient(
            host="https://clob.polymarket.com",
            key=private_key,
            chain_id=137,  # Polygon Mainnet
        )

        # Test connection
        client.get_ok()
        print("✓ Connected to Polymarket CLOB")

        # Derive API credentials
        print("Deriving API credentials...")
        creds = client.create_or_derive_api_creds()

        print()
        print("=" * 60)
        print("SUCCESS! Add these to your .env file:")
        print("=" * 60)
        print()
        print(f"POLYMARKET_API_KEY={creds.api_key}")
        print(f"POLYMARKET_API_SECRET={creds.api_secret}")
        print(f"POLYMARKET_API_PASSPHRASE={creds.api_passphrase}")
        print()
        print("=" * 60)

        # Verify credentials work
        print("Verifying credentials...")
        client.set_api_creds(creds)
        try:
            orders = client.get_orders()
            print(f"✓ Credentials verified! Found {len(orders)} open orders.")
        except Exception as e:
            print(f"⚠ Credential verification failed: {e}")
            print("  The credentials may still be valid for other operations.")

    except Exception as e:
        print(f"Error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
