"""Shop economy benchmark driver (B6b): 1,000 credits through the
EMITTED shop bot's economy methods (goldens/py/shop.py), without a
Discord connection. The Bot is instantiated, the economy DB is
connected in a temp dir, and 1,000 credits of 1 gold are applied
to one user; the final balance is printed as a checksum.

Run: python3 benchmarks/generated/shop_economy.py <dir>
"""

import asyncio
import importlib.util
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent


def load_shop():
    # The emitted shop imports its sibling module shop_items.
    sys.path.insert(0, str(ROOT / "goldens" / "py"))
    spec = importlib.util.spec_from_file_location(
        "shop_golden", ROOT / "goldens" / "py" / "shop.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["shop_golden"] = mod
    spec.loader.exec_module(mod)
    return mod


async def main():
    # Run in a temp dir so the economy.db is fresh (the Bot opens
    # economy.db in the CWD).
    with tempfile.TemporaryDirectory() as d:
        import os
        os.chdir(d)
        mod = load_shop()
        bot = mod.Bot()
        await bot._economy_connect()
        for _ in range(1000):
            await bot.economy_credit("1", 1, key=None)
        print(await bot.economy_balance("1"))
        await bot.economy_db.close()


if __name__ == "__main__":
    asyncio.run(main())
