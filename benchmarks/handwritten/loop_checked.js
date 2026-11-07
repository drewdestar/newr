// Hand-written checked-int tight loop with the newr runtime (B6).
//
// The same loop as benchmarks/generated/loop.newr emits, written by
// a human using the newr_rt.js checked helpers directly — the honest
// comparison for "what does the checked-int runtime cost": the
// emitted program should be within noise of this one, and both are
// slower than the plain-number loop (loop_plain.js).

const { add, mul, sub, tmod } = require("../generated/newr_rt.js");

let total = 0n;
let i = 0n;
while (i < 100000n) {
  total = tmod(sub(add(total, mul(i, 3n)), 7n), 1000003n);
  i = add(i, 1n);
}
console.log(total.toString());
