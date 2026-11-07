// Hand-written equivalent of the emitted hello program.
//
// Written by a human (not generated-then-edited) for the M4
// benchmarks (B4/B5/B8): the same observable behavior as
// `newr build --target cs examples/hello.newr` - print
// "Hello, world!".

using System;

public class hello {
  public static void Main(string[] args) {
    Console.WriteLine("Hello, world!");
  }
}
