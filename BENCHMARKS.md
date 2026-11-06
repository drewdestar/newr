# Benchmarks — M2 (Python emitter)

Machine: Linux x86_64, Python 3.12.3, hyperfine hyperfine 1.19.0.

All figures are measured, not promised. Raw hyperfine JSON is in `benchmarks/raw/`. No published generated-vs-hand-written benchmark exists for any transpiler — these numbers are the first such measurement for newr, and they are reported as found.

## B1 — Compile, cold (no warmup)

- hello: 127.1 ms ± 18.6 ms (range 110.9–170.9 ms, N=10)
- shop: 165.5 ms ± 36.2 ms (range 135.7–258.0 ms, N=10)

## B2 — Compile, warm

- hello (no change): 128.0 ms ± 21.6 ms (range 111.2–193.0 ms, N=20)
- shop (one file changed): 152.8 ms ± 23.3 ms (range 133.5–234.9 ms, N=20)

## B3 — `newr --help` startup (SPEC §4.1 target <100 ms)

- 141.5 ms ± 20.8 ms (range 111.2–177.7 ms, N=10)

## B4 — Generated code size

- hello_gen: 165 bytes, 5 lines
- hello_hw: 259 bytes, 9 lines
- shop_gen: 9404 bytes, 223 lines
- loop_gen: 327 bytes, 12 lines
- loop_hw: 396 bytes, 16 lines
- hello size ratio (generated/hand-written): 0.64
- loop size ratio (generated/hand-written): 0.83

## B5 — Generated program startup

- generated hello: 32.7 ms ± 4.0 ms (range 25.6–36.9 ms, N=10)
- hand-written hello: 28.9 ms ± 6.5 ms (range 24.4–45.3 ms, N=10)
- ratio (generated/hand-written): 1.13

## B6 — Generated runtime (checked-int tight loop, 100k iterations)

- generated (checked helpers): 123.4 ms ± 14.3 ms (range 113.4–161.8 ms, N=10)
- hand-written, plain ints: 48.5 ms ± 5.5 ms (range 44.7–62.7 ms, N=10)
- hand-written, newr_rt helpers: 132.2 ms ± 6.5 ms (range 124.9–145.0 ms, N=10)
- ratio generated/plain: 2.54
- ratio generated/checked-hand-written: 0.93
  (All three print the same checksum, 105003 — the loops agree.)

## B6b — Shop economy, 1,000 credits (aiosqlite, commit per call)

- generated (emitted shop Bot): 718.6 ms ± 79.8 ms (range 657.6–897.5 ms, N=10)
- hand-written (aiosqlite directly): 583.5 ms ± 30.5 ms (range 531.6–616.5 ms, N=10)
- ratio (generated/hand-written): 1.23
  (10,000 was reduced to 1,000: per-call commit fsync dominates and 10k took >4 min; 1k still measures the code path. Both print the final balance 1000.)
  (Corrected 7 Nov 2026, M2 fix #6: the published 1.89 was a benchmark-methodology artifact — the generated driver imports the whole shop bot (`import discord`, ~300-700 ms one-time) while the hand-written driver imported only aiosqlite (~78 ms). The hand-written driver now imports discord too, so the comparison measures the economy loop, not interpreter startup. The remaining 1.23 is just over the 1.20 threshold: the price is the atomicity template — a lock acquire + explicit BEGIN IMMEDIATE / COMMIT per credit, vs the hand-written plain commit().)

## B7 — Peak RSS

- compile (shop): 17.5 MB
- run (hello): 11.1 MB

## B8 — Hand-written parity

- startup ratio: 1.13 (>1.20 would be a bug report)
- loop ratio (generated/plain): 2.54 (>1.20 would be a bug report)
- shop economy ratio: 1.23 (>1.20 would be a bug report)
- hello size ratio: 0.64 (>1.50 would be a bug report)
- loop size ratio: 0.83 (>1.50 would be a bug report)
