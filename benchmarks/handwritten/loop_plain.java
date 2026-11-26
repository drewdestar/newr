// Hand-written checked-int tight loop (B6/B8 comparison).
//
// The same loop as benchmarks/generated/loop.newr emits, written by
// a human in plain Java (no checked helpers): 100,000 iterations
// of integer arithmetic, printing a checksum so the loop cannot be
// optimized away. Java `%` already takes the dividend's sign, like
// newr's `%` (tmod) - and the values stay far below 2**63, so plain
// longs are exact here.

public class loop_plain {
  public static void main(String[] args) {
    long total = 0;
    long i = 0;
    while (i < 100000) {
      total = (total + i * 3 - 7) % 1000003;
      i = i + 1;
    }
    System.out.println(total);
  }
}
