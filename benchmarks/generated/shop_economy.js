// Shop economy benchmark driver (B6b): 1,000 credits through the
// EMITTED shop bot's economy methods (goldens/js/shop.js), without a
// Discord connection. The Bot is instantiated, the economy DB is
// connected in a temp dir, and 1,000 credits of 1 gold are applied
// to one user; the final balance is printed as a checksum.
//
// Run: NODE_PATH=<discord.js> node benchmarks/generated/shop_economy.js
// (discord.js is required by the emitted shop bot; the mock in
// tests/js/mocks stands in for it here — see BENCHMARKS.md.)

const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const ROOT = path.resolve(__dirname, "..", "..");

async function main() {
  // Run in a temp dir so the economy.db is fresh (the Bot opens
  // economy.db in the CWD). Stage the emitted shop + its sibling
  // module + the runtime next to it, like a real build does.
  const d = fs.mkdtempSync(path.join(os.tmpdir(), "newr-shop-"));
  for (const f of ["shop.js", "shop_items.js"]) {
    fs.copyFileSync(path.join(ROOT, "goldens", "js", f),
                    path.join(d, f));
  }
  fs.copyFileSync(path.join(ROOT, "compiler", "newr_rt.js"),
                  path.join(d, "newr_rt.js"));
  process.chdir(d);
  // The emitted bot file exports the `bot` instance (not the
  // class — unlike the Python twin, which exports the class).
  const { bot } = require(path.join(d, "shop.js"));
  await bot._economy_connect();
  for (let i = 0; i < 1000; i++) {
    await bot.economy_credit("1", 1n, null);
  }
  console.log((await bot.economy_balance("1")).toString());
  bot.economy_db.close();
}

main().catch((e) => {
  console.error(e);
  process.exit(1);
});
