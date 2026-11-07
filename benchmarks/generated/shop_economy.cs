// Shop economy benchmark driver (B6b): 1,000 credits through the
// EMITTED shop bot's economy methods (goldens/cs/shop.cs), without a
// Discord connection. The Bot is instantiated, the economy DB is
// connected in a temp dir, and 1,000 credits of 1 gold are applied
// to one user; the final balance is printed as a checksum.
//
// Run from a scratch dir: the Bot opens economy_.db in the CWD, so
// the driver deletes any stale economy_.db* there first (a fresh
// balance every run).
//
// Built with shop.cs + shop_items.cs + newr_rt.cs and the
// Discord.Net + Microsoft.Data.Sqlite packages; run the built dll
// with `dotnet <dll>` (never plain `dotnet run`, which would
// measure the build).

using System;
using System.IO;

public class ShopEconomy {
  public static void Main(string[] args) {
    // Fresh DB: the emitted Bot opens economy_.db in the CWD.
    foreach (string suffix in new[] { "", "-journal", "-wal", "-shm" }) {
      string f = "economy_.db" + suffix;
      if (File.Exists(f)) {
        File.Delete(f);
      }
    }
    shop.Bot bot = new shop.Bot();
    bot.Setup();
    for (int n = 0; n < 1000; n++) {
      bot.economy__credit("1", 1L, null);
    }
    Console.WriteLine(bot.economy__balance("1"));
  }
}
