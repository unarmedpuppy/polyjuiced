#!/usr/bin/env python3
"""Clear stale pending trades from database."""

import asyncio
import aiosqlite

async def main():
    conn = await aiosqlite.connect('/app/data/gabagool.db')
    cursor = await conn.execute("SELECT COUNT(*) FROM trades WHERE status = 'pending'")
    count = (await cursor.fetchone())[0]
    print(f"Found {count} pending trades")

    await conn.execute("DELETE FROM trades WHERE status = 'pending'")
    await conn.commit()

    cursor = await conn.execute("SELECT COUNT(*) FROM trades WHERE status = 'pending'")
    count = (await cursor.fetchone())[0]
    print(f"After delete: {count} pending trades")
    await conn.close()

if __name__ == "__main__":
    asyncio.run(main())
