"""Hand-written shop economy benchmark (B6b comparison).

The same 1,000 credits as benchmarks/generated/shop_economy.py,
written by a human with aiosqlite directly - the same SQL shape
the emitted bot uses (INSERT ... ON CONFLICT for the balance,
ledger insert, commit per call), so the comparison is fair.
Prints the final balance as a checksum.

The `import discord` below is deliberate (M2 fix #6): the
generated driver imports the whole emitted shop bot, which imports
discord (~300-700 ms one-time cost); without the same import here,
hyperfine would measure interpreter startup, not the economy loop.
"""

import asyncio
import sys
import tempfile
from pathlib import Path

import aiosqlite
import discord  # noqa: F401  (equalises the generated driver's import)


async def main():
    with tempfile.TemporaryDirectory() as d:
        db = await aiosqlite.connect(str(Path(d) / "economy.db"))
        await db.execute(
            "CREATE TABLE IF NOT EXISTS balances "
            "(user TEXT PRIMARY KEY, balance INTEGER)")
        await db.execute(
            "CREATE TABLE IF NOT EXISTS ledger "
            "(id INTEGER PRIMARY KEY AUTOINCREMENT, user TEXT NOT NULL, "
            "amount INTEGER NOT NULL, key TEXT UNIQUE, "
            "created_at INTEGER NOT NULL)")
        await db.commit()
        for _ in range(1000):
            await db.execute(
                "INSERT INTO balances (user, balance) VALUES (?, ?) "
                "ON CONFLICT(user) DO UPDATE SET "
                "balance = balance + excluded.balance", ("1", 1))
            await db.execute(
                "INSERT INTO ledger (user, amount, key, created_at) "
                "VALUES (?, ?, ?, ?)", ("1", 1, None, 0))
            await db.commit()
        async with db.execute(
                "SELECT balance FROM balances WHERE user = ?",
                ("1",)) as cursor:
            row = await cursor.fetchone()
        print(row[0] if row is not None else 0)
        await db.close()


if __name__ == "__main__":
    asyncio.run(main())
