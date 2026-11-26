// Hand-written checked-int tight loop with the newr runtime (B6).
//
// The same loop as benchmarks/generated/loop.newr emits, written by
// a human using the NewrRt checked helpers directly - the honest
// comparison for "what does the checked-int runtime cost": the
// emitted program should be within noise of this one, and both are
// slower than the plain-int loop (loop_plain.cs).
//
// Built with newr_rt.cs (compiler/newr_rt.cs) in the same project.

using System;

public class loop_checked {
  public static void Main(string[] args) {
    long total = 0;
    long i = 0;
    while (i < 100000) {
      total = NewrRt.Tmod(NewrRt.Sub(NewrRt.Add(total, NewrRt.Mul(i, 3)), 7), 1000003);
      i = NewrRt.Add(i, 1);
    }
    Console.WriteLine(total);
  }
}
