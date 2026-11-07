// Hand-written shop economy benchmark (B6b comparison).
//
// The same 1,000 credits as benchmarks/generated/shop_economy.java,
// written by a human with plain JDBC + sqlite-jdbc directly — the
// same SQL shape the emitted Java bot uses (balances stored as TEXT,
// BEGIN IMMEDIATE / COMMIT per credit, INSERT ... ON CONFLICT for
// the balance; no ledger row when the key is null, exactly like the
// emitted bot's _economy_apply). Prints the final balance as a
// checksum.
//
// The DB lives in the CWD (economy.db), like the generated driver:
// the runner starts every run in a fresh temp dir, so the balance
// is fresh and both drivers pay the same fsync cost on the same
// filesystem. (An earlier draft put the hand-written DB in /tmp
// while the generated one sat on the home disk — the generated
// driver then looked ~100x slower, purely from fsync latency.)
//
// The `Class.forName("net.dv8tion.jda.api.JDABuilder")` below is
// deliberate (M2 fix #6): the generated driver loads the whole
// emitted shop bot, which loads JDA; without the same class load
// here, hyperfine would measure JVM + JDA startup, not the economy
// loop. It is a load only — no Discord connection is made.

import java.io.File;
import java.sql.Connection;
import java.sql.DriverManager;
import java.sql.PreparedStatement;
import java.sql.ResultSet;
import java.sql.SQLException;
import java.sql.Statement;

public class shop_economy {
  public static void main(String[] args) throws Exception {
    // Equalise the generated driver's JDA load (see header).
    Class.forName("net.dv8tion.jda.api.JDABuilder");
    // Fresh DB in the CWD (see header).
    for (String suffix : new String[] {"", "-journal", "-wal", "-shm"}) {
      new File("economy.db" + suffix).delete();
    }
    Connection db = DriverManager.getConnection("jdbc:sqlite:economy.db");
    try (Statement st = db.createStatement()) {
      st.execute("PRAGMA busy_timeout = 5000");
      st.execute("CREATE TABLE IF NOT EXISTS balances (user TEXT PRIMARY KEY, balance TEXT)");
      st.execute("CREATE TABLE IF NOT EXISTS ledger (id INTEGER PRIMARY KEY AUTOINCREMENT, user TEXT NOT NULL, amount TEXT NOT NULL, key TEXT UNIQUE, created_at INTEGER NOT NULL)");
    }
    for (int n = 0; n < 1000; n++) {
      try (Statement st = db.createStatement()) {
        st.execute("BEGIN IMMEDIATE");
        try {
          long cur = 0L;
          try (PreparedStatement ps = db.prepareStatement(
              "SELECT balance FROM balances WHERE user = ?")) {
            ps.setString(1, "1");
            try (ResultSet rs = ps.executeQuery()) {
              if (rs.next()) {
                cur = Long.parseLong(rs.getString(1));
              }
            }
          }
          long next = cur + 1;
          try (PreparedStatement ps = db.prepareStatement(
              "INSERT INTO balances (user, balance) VALUES (?, ?) "
              + "ON CONFLICT(user) DO UPDATE SET balance = excluded.balance")) {
            ps.setString(1, "1");
            ps.setString(2, Long.toString(next));
            ps.executeUpdate();
          }
          st.execute("COMMIT");
        } catch (SQLException e) {
          try {
            st.execute("ROLLBACK");
          } catch (SQLException ignored) {
            // The original error is the one that matters.
          }
          throw e;
        }
      }
    }
    long balance = 0L;
    try (PreparedStatement ps = db.prepareStatement(
        "SELECT balance FROM balances WHERE user = ?")) {
      ps.setString(1, "1");
      try (ResultSet rs = ps.executeQuery()) {
        if (rs.next()) {
          balance = Long.parseLong(rs.getString(1));
        }
      }
    }
    System.out.println(balance);
    db.close();
  }
}
