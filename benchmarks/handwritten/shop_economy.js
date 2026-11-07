// Hand-written shop economy benchmark (B6b comparison).
//
// The same 1,000 credits as benchmarks/generated/shop_economy.js,
// written by a human with node:sqlite directly - the same SQL shape
// the emitted bot uses (TEXT balances with BigInt arithmetic in JS,
// BEGIN IMMEDIATE ... COMMIT per call, ledger insert), so the
// comparison is fair. Prints the final balance as a checksum.
//
// The `require("discord.js")` below is deliberate (M2 fix #6, JS
// twin): the generated driver imports the whole emitted shop bot,
// which imports discord.js; without the same import here, hyperfine
// would measure interpreter startup, not the economy loop.

const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { DatabaseSync } = require("node:sqlite");

require("discord.js"); // noqa: equalises the generated driver's import

function main() {
  const d = fs.mkdtempSync(path.join(os.tmpdir(), "newr-shop-hw-"));
  const db = new DatabaseSync(path.join(d, "economy.db"));
  db.exec("PRAGMA busy_timeout = 5000");
  db.exec("CREATE TABLE IF NOT EXISTS balances (user TEXT PRIMARY KEY, balance TEXT)");
  db.exec("CREATE TABLE IF NOT EXISTS ledger (id INTEGER PRIMARY KEY AUTOINCREMENT, user TEXT NOT NULL, amount TEXT NOT NULL, key TEXT UNIQUE, created_at INTEGER NOT NULL)");
  const upsert = db.prepare("INSERT INTO balances (user, balance) VALUES (?, ?) ON CONFLICT(user) DO UPDATE SET balance = excluded.balance");
  const ledger = db.prepare("INSERT INTO ledger (user, amount, key, created_at) VALUES (?, ?, ?, ?)");
  for (let i = 0; i < 1000; i++) {
    db.exec("BEGIN IMMEDIATE");
    try {
      const row = db.prepare("SELECT balance FROM balances WHERE user = ?").get("1");
      const next = (row ? BigInt(row.balance) : 0n) + 1n;
      upsert.run("1", next.toString());
      ledger.run("1", "1", null, Math.floor(Date.now() / 1000));
      db.exec("COMMIT");
    } catch (e) {
      db.exec("ROLLBACK");
      throw e;
    }
  }
  const row = db.prepare("SELECT balance FROM balances WHERE user = ?").get("1");
  console.log(row ? row.balance : "0");
  db.close();
}

main();
