// Hand-written checked-int tight loop with the newr runtime (B6).
//
// The same loop as benchmarks/generated/loop.newr emits, written by
// a human using the NewrRt checked helpers directly - the honest
// comparison for "what does the checked-int runtime cost": the
// emitted program should be within noise of this one, and both are
// slower than the plain-int loop (loop_plain.java).
//
// Run with NewrRt.java (compiler/newr_rt.java) on the classpath.

public class loop_checked {
  public static void main(String[] args) {
    long total = 0;
    long i = 0;
    while (i < 100000) {
      total = NewrRt.tmod(NewrRt.sub(NewrRt.add(total, NewrRt.mul(i, 3)), 7), 1000003);
      i = NewrRt.add(i, 1);
    }
    System.out.println(total);
  }
}
