// Shop economy benchmark driver (B6b): 1,000 credits through the
// EMITTED shop bot's economy methods (goldens/java/shop.java), without a
// Discord connection. The Bot is instantiated, the economy DB is
// connected in a temp dir, and 1,000 credits of 1 gold are applied
// to one user; the final balance is printed as a checksum.
//
// The emitted Bot is a private nested class (`private static class
// Bot` inside `shop`), so the driver reaches it by reflection -
// that is the only way to drive it from outside the emitted file,
// and the reflective lookup happens once, outside the timed loop
// (the 1,000 credit calls go through cached Method objects).
//
// Run from a scratch dir: the Bot opens economy.db in the CWD, so
// the driver deletes any stale economy.db* there first (a fresh
// balance every run). The emitted shop's static init starts a
// non-daemon scheduler thread, so the driver exits explicitly
// (System.exit) once the balance is printed.
//
// Compile with shop.java + shop_items.java + NewrRt.java and the
// JDA + sqlite-jdbc jars on the classpath; run with the same
// classpath. Run: java -cp <dir>:<jda>:<sqlite> shop_economy

import java.io.File;
import java.lang.reflect.Constructor;
import java.lang.reflect.Method;

public class shop_economy {
  public static void main(String[] args) throws Exception {
    // Fresh DB: the emitted Bot opens economy.db in the CWD.
    for (String suffix : new String[] {"", "-journal", "-wal", "-shm"}) {
      new File("economy.db" + suffix).delete();
    }
    Class<?> botClass = Class.forName("shop$Bot");
    Constructor<?> ctor = botClass.getDeclaredConstructor();
    ctor.setAccessible(true);
    Object bot = ctor.newInstance();
    Method connect = botClass.getDeclaredMethod("_economy_connect");
    connect.setAccessible(true);
    connect.invoke(bot);
    Method credit = botClass.getDeclaredMethod(
        "economy_credit", String.class, long.class, String.class);
    credit.setAccessible(true);
    for (int n = 0; n < 1000; n++) {
      credit.invoke(bot, "1", 1L, null);
    }
    Method balance = botClass.getDeclaredMethod(
        "economy_balance", String.class);
    balance.setAccessible(true);
    System.out.println(balance.invoke(bot, "1"));
    System.out.flush();
    // The emitted shop's static init starts a non-daemon scheduler
    // thread; exit explicitly so the JVM does not hang here.
    System.exit(0);
  }
}
