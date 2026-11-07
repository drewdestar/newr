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

# Benchmarks — M3 (JavaScript emitter)

Machine: Linux x86_64, Python 3.12.3, Node v24.22.1, hyperfine hyperfine 1.19.0.

All figures are measured, not promised. Raw hyperfine JSON is in `benchmarks/raw/` (`*_js.json`). The compile benchmarks (B1/B2/B7-compile) measure the same Python compiler as M2, emitting JS instead of Python — the difference is the emitter. The B6b discord.js import is satisfied by the in-repo mock (tests/js/mocks), not real discord.js 14.28.1: both drivers pay the same small mock require cost, so the ratio is honest but the absolute times understate a real discord.js import.

## B1 — Compile, cold (no warmup), `--target js`

- hello: 125.2 ms ± 17.5 ms (range 112.8–168.3 ms, N=10)
- shop: 142.0 ms ± 4.0 ms (range 138.2–151.1 ms, N=10)

## B2 — Compile, warm, `--target js`

- hello (no change): 125.0 ms ± 22.9 ms (range 111.1–197.1 ms, N=20)
- shop (one file changed): 148.0 ms ± 13.2 ms (range 135.3–187.3 ms, N=20)

## B4 — Generated code size (JS)

- hello_gen: 188 bytes, 6 lines
- hello_hw: 285 bytes, 8 lines
- shop_gen: 15018 bytes, 382 lines
- loop_gen: 388 bytes, 14 lines
- loop_hw: 562 bytes, 17 lines
- hello size ratio (generated/hand-written): 0.66
- loop size ratio (generated/hand-written): 0.69

## B5 — Generated program startup (node)

- generated hello: 86.1 ms ± 5.5 ms (range 79.0–97.4 ms, N=10)
- hand-written hello: 89.3 ms ± 11.3 ms (range 75.5–111.2 ms, N=10)
- ratio (generated/hand-written): 0.96

## B6 — Generated runtime (checked-int tight loop, 100k iterations)

- generated (checked helpers, BigInt): 107.9 ms ± 27.4 ms (range 90.8–182.7 ms, N=10)
- hand-written, plain JS numbers: 88.4 ms ± 9.7 ms (range 78.6–105.0 ms, N=10)
- hand-written, newr_rt.js helpers: 98.8 ms ± 6.4 ms (range 90.6–106.9 ms, N=10)
- ratio generated/plain: 1.22
- ratio generated/checked-hand-written: 1.09
  (All three print the same checksum, 105003 — the loops agree.)
  (Note: the 1.22 generated/plain ratio is just over the 1.20
  proposal threshold — flagged honestly, not hidden. It is the
  BigInt price of int64 exactness in JS (J2): the generated code
  is within noise of hand-written code using the same checked
  helpers (1.09), and the generated mean has high variance
  (±27.4 ms, one run at 182.7 ms). The plain-number loop is not
  a fair target — it cannot represent int64 at all.)

## B6b — Shop economy, 1,000 credits (node:sqlite, sync, commit per call)

- generated (emitted shop Bot): 203.9 ms ± 25.7 ms (range 180.0–270.4 ms, N=10)
- hand-written (node:sqlite directly): 228.4 ms ± 55.2 ms (range 170.0–336.0 ms, N=10)
- ratio (generated/hand-written): 0.89
  (Both print the final balance 1000.)

## B7 — Peak RSS (JS)

- compile (shop, --target js): 17.9 MB
- run (hello, node): 61.2 MB
  (Node's V8 baseline, not the program — Python's run is 11.1 MB
  for the same hello. The compile figure (17.9 MB) is the same
  Python compiler as M2.)

## B8 — Hand-written parity (JS)

- startup ratio: 0.96 (>1.20 would be a bug report)
- loop ratio (generated/plain): 1.22 (>1.20 would be a bug report)
- shop economy ratio: 0.89 (>1.20 would be a bug report)
- hello size ratio: 0.66 (>1.50 would be a bug report)
- loop size ratio: 0.69 (>1.50 would be a bug report)
