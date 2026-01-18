#!/usr/bin/env python3
"""Comprehensive diagnostic script to understand system state."""
import os
import json
import sqlite3
import urllib.request
from datetime import datetime, timedelta

DB_PATH = '/app/data/gabagool.db'

def get_env_var(name):
    """Get environment variable, checking .env file as fallback."""
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

def section(title):
    print("\n" + "=" * 70)
    print(f"  {title}")
    print("=" * 70)

def main():
    print("=" * 70)
    print("  POLYMARKET BOT - FULL DIAGNOSTIC REPORT")
    print(f"  Generated: {datetime.utcnow().isoformat()}Z")
    print("=" * 70)

    # =========================================================================
    section("1. ENVIRONMENT & CONFIGURATION")
    # =========================================================================

    dry_run = get_env_var("GABAGOOL_DRY_RUN")
    vol_enabled = get_env_var("VOL_HAPPENS_ENABLED")
    arb_enabled = get_env_var("GABAGOOL_ENABLED")
    wallet = get_env_var("POLYMARKET_PROXY_WALLET")

    print(f"GABAGOOL_DRY_RUN: {dry_run}")
    print(f"GABAGOOL_ENABLED: {arb_enabled}")
    print(f"VOL_HAPPENS_ENABLED: {vol_enabled}")
    print(f"Wallet: {wallet[:10]}...{wallet[-6:] if wallet else 'NOT SET'}")

    # =========================================================================
    section("2. DATABASE STATE")
    # =========================================================================

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    # realized_pnl_ledger
    cur = conn.execute("SELECT COUNT(*) as cnt, SUM(pnl_amount) as total FROM realized_pnl_ledger")
    row = cur.fetchone()
    print(f"\nrealized_pnl_ledger:")
    print(f"  Entries: {row['cnt']}")
    print(f"  Total P&L: ${row['total'] or 0:.2f}")

    cur = conn.execute("SELECT pnl_type, COUNT(*), SUM(pnl_amount) FROM realized_pnl_ledger GROUP BY pnl_type")
    for row in cur.fetchall():
        print(f"    - {row[0]}: {row[1]} entries, ${row[2] or 0:.2f}")

    # trades table
    cur = conn.execute("""
        SELECT status, COUNT(*) as cnt, SUM(yes_cost + no_cost) as cost
        FROM trades WHERE dry_run = 0
        GROUP BY status
    """)
    print(f"\ntrades table (real trades by status):")
    for row in cur.fetchall():
        print(f"  {row['status']}: {row['cnt']} trades, ${row['cost'] or 0:.2f} cost")

    cur = conn.execute("SELECT COUNT(*) FROM trades WHERE dry_run = 1")
    dry_count = cur.fetchone()[0]
    print(f"  dry_run trades: {dry_count}")

    # settlement_queue
    cur = conn.execute("""
        SELECT claimed, COUNT(*) as cnt, SUM(entry_cost) as cost
        FROM settlement_queue GROUP BY claimed
    """)
    print(f"\nsettlement_queue:")
    for row in cur.fetchall():
        status = "claimed" if row['claimed'] else "unclaimed"
        print(f"  {status}: {row['cnt']} entries, ${row['cost'] or 0:.2f} entry cost")

    # circuit_breaker_state
    cur = conn.execute("SELECT * FROM circuit_breaker_state WHERE id=1")
    row = cur.fetchone()
    if row:
        print(f"\ncircuit_breaker_state:")
        print(f"  Date: {row['date']}")
        print(f"  Realized P&L: ${row['realized_pnl'] or 0:.2f}")
        print(f"  Trades today: {row['total_trades_today']}")

    conn.close()

    # =========================================================================
    section("3. POLYMARKET API - ACTUAL STATE")
    # =========================================================================

    if not wallet:
        print("ERROR: No wallet configured, skipping API checks")
    else:
        # Get actual positions
        try:
            url = f"https://data-api.polymarket.com/positions?user={wallet}"
            req = urllib.request.Request(url)
            req.add_header("User-Agent", "Mozilla/5.0")
            with urllib.request.urlopen(req, timeout=10) as resp:
                positions = json.loads(resp.read())

            print(f"\nPositions from data-api.polymarket.com:")
            print(f"  Total positions returned: {len(positions)}")

            if positions:
                total_value = sum(float(p.get("value", 0) or (float(p.get("size", 0)) * float(p.get("avgPrice", 0)))) for p in positions)
                print(f"  Total position value: ${total_value:.2f}")

                # Show first few
                for i, p in enumerate(positions[:5]):
                    outcome = p.get("outcome", "?")
                    size = float(p.get("size", 0))
                    title = p.get("title", "?")[:40]
                    print(f"    {i+1}. {outcome} {size:.2f} shares - {title}")
                if len(positions) > 5:
                    print(f"    ... and {len(positions) - 5} more")
        except Exception as e:
            print(f"  ERROR fetching positions: {e}")

        # Get recent trades
        try:
            url = f"https://data-api.polymarket.com/trades?user={wallet}&limit=10"
            req = urllib.request.Request(url)
            req.add_header("User-Agent", "Mozilla/5.0")
            with urllib.request.urlopen(req, timeout=10) as resp:
                trades = json.loads(resp.read())

            print(f"\nRecent trades from data-api.polymarket.com:")
            print(f"  Trades returned: {len(trades)}")

            for t in trades[:5]:
                side = t.get("side", "?")
                outcome = t.get("outcome", "?")
                size = float(t.get("size", 0))
                price = float(t.get("price", 0))
                ts = t.get("timestamp", 0)
                dt = datetime.fromtimestamp(ts) if ts else "?"
                print(f"    {side} {size:.2f} {outcome} @ ${price:.2f} ({dt})")
        except Exception as e:
            print(f"  ERROR fetching trades: {e}")

    # =========================================================================
    section("4. CLOB API - BALANCE & ORDERS")
    # =========================================================================

    # This would require the actual client, which is tricky from a script
    # Instead, let's check what the bot's logs say
    print("\n(CLOB checks require running client - check bot logs)")
    print("Look for: 'Balance: $X.XX' and 'Open orders:' in recent logs")

    # =========================================================================
    section("5. KEY QUESTIONS TO ANSWER")
    # =========================================================================

    print("""
    Based on the data above, answer these questions:

    1. Is dry_run enabled? (If yes, no real trades will execute)

    2. Are there trades in the database?
       - If yes: Were they real or dry_run?
       - If no: Trades aren't being recorded

    3. Does data-api show positions that Polymarket UI doesn't?
       - If yes: API is stale/cached, positions may be resolved

    4. Is circuit_breaker blocking trades?
       - Check if realized_pnl hit daily loss limit

    5. Is Vol Happens interfering with Gabagool?
       - Both enabled could cause issues even with callback fix
    """)

    # =========================================================================
    section("6. RECOMMENDED ACTIONS")
    # =========================================================================

    print("""
    IMMEDIATE:
    1. Set VOL_HAPPENS_ENABLED=false to simplify
    2. Verify GABAGOOL_DRY_RUN=false if you want real trades
    3. Restart the bot after config changes

    THEN:
    4. Watch logs for "Opportunity detected" messages
    5. Watch logs for "Order submitted" messages
    6. Check dashboard for real-time price updates

    IF NO OPPORTUNITIES DETECTED:
    - Markets may not have sufficient spread
    - Check min_spread_threshold config

    IF OPPORTUNITIES DETECTED BUT NO ORDERS:
    - Check circuit_breaker state
    - Check liquidity requirements
    - Check max_trade_size config
    """)

if __name__ == '__main__':
    main()
