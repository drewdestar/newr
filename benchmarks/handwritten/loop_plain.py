"""Hand-written checked-int tight loop (B6/B8 comparison).

The same loop as benchmarks/generated/loop.newr emits, written by
a human in plain Python (no checked helpers): 100,000 iterations
of integer arithmetic, printing a checksum so the loop cannot be
optimized away.
"""

total = 0
i = 0
while i < 100000:
    total = total + i * 3 - 7
    total = total % 1000003
    i = i + 1
print(total)
