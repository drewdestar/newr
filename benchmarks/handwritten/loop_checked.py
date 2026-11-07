"""Hand-written checked-int tight loop with the newr runtime (B6).

The same loop as benchmarks/generated/loop.newr emits, written by
a human using the newr_rt checked helpers directly - the honest
comparison for "what does the checked-int runtime cost": the
emitted program should be within noise of this one, and both are
slower than the plain-int loop (loop_plain.py).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "generated"))
from newr_rt import add, mul, sub, tmod

total = 0
i = 0
while i < 100000:
    total = tmod(sub(add(total, mul(i, 3)), 7), 1000003)
    i = add(i, 1)
print(total)
