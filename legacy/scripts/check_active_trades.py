#!/usr/bin/env python3
"""
Pre-deployment check for active trades.

This script checks if there are any active (unresolved) real trades
that would be at risk if the bot is restarted during deployment.

Exit codes:
    0 - Safe to deploy (no active trades)
    1 - NOT safe to deploy (active trades exist)
    2 - Error checking trades

Usage:
    docker exec polymarket-bot python3 /app/scripts/check_active_trades.py

    # Or via SSH:
    scripts/connect-server.sh "docker exec polymarket-bot python3 /app/scripts/check_active_trades.py"
"""

import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

# Add parent directory for imports
sys.path.insert(0, str(Path(__file__).parent.parent))


async def check_active_trades() -> dict:
    """Check for active trades that haven't resolved yet.

    Returns:
        dict with:
            - safe_to_deploy: bool
            - active_trades: list of trade summaries
            - message: human-readable status
    """
    import aiosqlite

    db_path = Path("/app/data/gabagool.db")

    if not db_path.exists():
        return {
            "safe_to_deploy": True,
            "active_trades": [],
            "message": "No database found - safe to deploy (first run)"
        }

    async with aiosqlite.connect(db_path) as conn:
        conn.row_factory = aiosqlite.Row

        # Find trades that are:
        # 1. Real trades (not dry_run)
        # 2. Status is 'pending' (awaiting resolution)
        # 3. Market hasn't ended yet OR resolved_at is NULL
        now = datetime.now(timezone.utc).isoformat()

        cursor = await conn.execute("""
            SELECT
                id,
                asset,
                created_at,
                market_end_time,
                yes_cost,
                no_cost,
                expected_profit,
                execution_status,
                status
            FROM trades
            WHERE dry_run = 0
              AND status = 'pending'
              AND resolved_at IS NULL
            ORDER BY created_at DESC
        """)

        rows = await cursor.fetchall()
        active_trades = []

        for row in rows:
            trade = dict(row)

            # Parse market_end_time to check if market has ended
            market_end = trade.get("market_end_time")
            if market_end:
                try:
                    # Handle ISO format
                    if "T" in str(market_end):
                        end_dt = datetime.fromisoformat(market_end.replace("Z", "+00:00"))
                    else:
                        end_dt = datetime.fromisoformat(market_end)

                    if end_dt.tzinfo is None:
                        end_dt = end_dt.replace(tzinfo=timezone.utc)

                    now_dt = datetime.now(timezone.utc)
                    time_until_resolution = (end_dt - now_dt).total_seconds()

                    trade["time_until_resolution_seconds"] = time_until_resolution
                    trade["market_ended"] = time_until_resolution < 0
                except (ValueError, TypeError):
                    trade["time_until_resolution_seconds"] = None
                    trade["market_ended"] = None

            active_trades.append({
                "id": trade["id"],
                "asset": trade["asset"],
                "created_at": trade["created_at"],
                "market_end_time": trade["market_end_time"],
                "total_cost": (trade.get("yes_cost") or 0) + (trade.get("no_cost") or 0),
                "expected_profit": trade.get("expected_profit"),
                "execution_status": trade.get("execution_status"),
                "time_until_resolution_seconds": trade.get("time_until_resolution_seconds"),
                "market_ended": trade.get("market_ended"),
            })

        # Filter to trades where market hasn't ended yet (still need monitoring)
        # or where we can't determine the end time
        trades_needing_monitoring = [
            t for t in active_trades
            if t.get("market_ended") is False or t.get("market_ended") is None
        ]

        if not active_trades:
            return {
                "safe_to_deploy": True,
                "active_trades": [],
                "message": "✅ No active trades - safe to deploy"
            }

        if not trades_needing_monitoring:
            return {
                "safe_to_deploy": True,
                "active_trades": active_trades,
                "message": f"✅ {len(active_trades)} trade(s) found but all markets have ended - safe to deploy"
            }

        # Build warning message
        trade_summaries = []
        for t in trades_needing_monitoring:
            time_left = t.get("time_until_resolution_seconds")
            if time_left is not None:
                minutes = int(time_left / 60)
                time_str = f"{minutes}m until resolution"
            else:
                time_str = "unknown resolution time"

            trade_summaries.append(
                f"  - {t['asset']}: ${t['total_cost']:.2f} invested ({time_str})"
            )

        return {
            "safe_to_deploy": False,
            "active_trades": trades_needing_monitoring,
            "all_trades": active_trades,
            "message": (
                f"🚨 NOT SAFE TO DEPLOY - {len(trades_needing_monitoring)} active trade(s):\n"
                + "\n".join(trade_summaries)
            )
        }


def main():
    """Main entry point."""
    try:
        result = asyncio.run(check_active_trades())

        # Print human-readable output
        print(result["message"])

        # Print JSON for programmatic use
        if result["active_trades"]:
            print("\nTrade details (JSON):")
            print(json.dumps(result["active_trades"], indent=2, default=str))

        # Exit with appropriate code
        sys.exit(0 if result["safe_to_deploy"] else 1)

    except Exception as e:
        print(f"❌ Error checking trades: {e}")
        sys.exit(2)


if __name__ == "__main__":
    main()
