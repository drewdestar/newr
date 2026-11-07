// Hand-written shop economy benchmark (B6b comparison).
//
// The same 1,000 credits as benchmarks/generated/shop_economy.cs,
// written by a human with Microsoft.Data.Sqlite directly — the same
// SQL shape the emitted C# bot uses (balances stored as TEXT,
// BEGIN IMMEDIATE / COMMIT per credit, INSERT ... ON CONFLICT for
// the balance, ledger insert per credit). Prints the final balance
// as a checksum.
//
// The `new DiscordSocketClient(...)` below is deliberate
// (M2 fix #6): the generated driver must construct the emitted
// shop Bot, which IS a DiscordSocketClient — its construction
// costs ~200 ms one-time (measured). A bare `typeof` touch only
// loads the assembly (~5 ms), so without the same construction
// here the comparison would measure client construction, not the
// economy loop: the generated credit loop itself is, if anything,
// faster than this driver's. It is a construction only — no
// Discord connection is made.

// The DB lives in the CWD (economy_.db), like the generated
// driver: the runner starts every run in a fresh temp dir, so the
// balance is fresh and both drivers pay the same fsync cost on the
// same filesystem. (Putting the hand-written DB in the temp dir
// while the generated one sat elsewhere would measure fsync
// latency, not the economy loop.)

using System;
using System.IO;
using Discord;
using Discord.WebSocket;
using Microsoft.Data.Sqlite;

public class ShopEconomy {
  public static void Main(string[] args) {
    // Equalise the generated driver's Discord.Net client
    // construction cost (see header).
    _ = new DiscordSocketClient(new DiscordSocketConfig {
      GatewayIntents = GatewayIntents.Guilds | GatewayIntents.GuildMembers,
    });
    // Fresh DB in the CWD (see header).
    foreach (string suffix in new[] { "", "-journal", "-wal", "-shm" }) {
      string f = "economy_.db" + suffix;
      if (File.Exists(f)) {
        File.Delete(f);
      }
    }
    using (SqliteConnection db = new SqliteConnection(
        "Data Source=economy_.db")) {
      db.Open();
      Exec(db, "PRAGMA busy_timeout = 5000");
      Exec(db, "CREATE TABLE IF NOT EXISTS balances (user TEXT PRIMARY KEY, balance TEXT)");
      Exec(db, "CREATE TABLE IF NOT EXISTS ledger (id INTEGER PRIMARY KEY AUTOINCREMENT, user TEXT NOT NULL, amount TEXT NOT NULL, key TEXT UNIQUE, created_at INTEGER NOT NULL)");
      for (int n = 0; n < 1000; n++) {
        Exec(db, "BEGIN IMMEDIATE");
        try {
          long cur;
          using (SqliteCommand cmd = db.CreateCommand()) {
            cmd.CommandText = "SELECT balance FROM balances WHERE user = $user";
            cmd.Parameters.AddWithValue("$user", "1");
            object result = cmd.ExecuteScalar();
            cur = result == null ? 0L : long.Parse((string) result);
          }
          long next = cur + 1;
          using (SqliteCommand cmd = db.CreateCommand()) {
            cmd.CommandText = "INSERT INTO balances (user, balance) VALUES ($user, $balance) ON CONFLICT(user) DO UPDATE SET balance = excluded.balance";
            cmd.Parameters.AddWithValue("$user", "1");
            cmd.Parameters.AddWithValue("$balance", next.ToString());
            cmd.ExecuteNonQuery();
          }
          using (SqliteCommand cmd = db.CreateCommand()) {
            cmd.CommandText = "INSERT INTO ledger (user, amount, key, created_at) VALUES ($user, $amount, $key, $created)";
            cmd.Parameters.AddWithValue("$user", "1");
            cmd.Parameters.AddWithValue("$amount", "1");
            cmd.Parameters.AddWithValue("$key", DBNull.Value);
            cmd.Parameters.AddWithValue("$created", DateTimeOffset.UtcNow.ToUnixTimeSeconds());
            cmd.ExecuteNonQuery();
          }
          Exec(db, "COMMIT");
        } catch (Exception) {
          try {
            Exec(db, "ROLLBACK");
          } catch (Exception) {
            // The original error is the one that matters.
          }
          throw;
        }
      }
      long balance;
      using (SqliteCommand cmd = db.CreateCommand()) {
        cmd.CommandText = "SELECT balance FROM balances WHERE user = $user";
        cmd.Parameters.AddWithValue("$user", "1");
        object result = cmd.ExecuteScalar();
        balance = result == null ? 0L : long.Parse((string) result);
      }
      Console.WriteLine(balance);
    }
  }

  private static void Exec(SqliteConnection db, string sql) {
    using (SqliteCommand cmd = db.CreateCommand()) {
      cmd.CommandText = sql;
      cmd.ExecuteNonQuery();
    }
  }
}
