# newr — Re-Stress Report (after fix round 2)

**Date:** 7 November 2026 (WIB, UTC+7) · **Compiler under test:** `~/workspace/newr/`,
pinned by SHA-256 in `stress/results/compiler-pinned-round2.sha256`
(`lexer.py ad81a4a8…`, `resolver.py eac4dc92…`, `cli.py 7b1ade51…`,
`parser.py 1cd509ef…` — **unchanged from round 1**,
`checker.py bf1977cb…` — unchanged, `SPEC.md` unchanged).
**Suite at test time:** 453 tests (414 + 39 new `test_stress_fixes.py`),
all OK (re-verified by this campaign's endurance loop, 60/60).
**Harness:** same `stress/run_stress.py` + `stress/gen_corpus.py` as
round 1 (no harness changes). No file under `compiler/`, `SPEC.md`
or `tests/` was modified by this campaign — verified by
`sha256sum -c stress/results/compiler-pinned-round2.sha256`
(all pinned files unchanged after the campaign).

## Verdict (one paragraph)

**NOT clean to ship — one new crash, and it is pre-existing.**
All five round-1 findings are verified fixed (the exact inputs that
crashed or misbehaved before now produce clean diagnostics, and the
depth cliff is gone: worst case 0.49 s, down from ~30–41 s). But the
fresh fuzz seed found a **new, pre-existing parser crash** the first
campaign never reached: a malformed `t(...)` call makes the parser
raise a raw `AttributeError` instead of the intended E1001
diagnostic (`parser.py:1838` and `:1842` pass a `Span` where `err()`
expects a `Token`). It is a two-line error-path bug, it is not a
regression from fix round 2 (parser.py is byte-identical to round 1),
and it needs a fix round 3 — then a targeted re-stress (fuzz seed
20261107 + the two minimized repros) — before shipping.

| # | Finding | Severity | Status |
|---|---------|----------|--------|
| 1 | **NEW — CRASH-2:** `t()` error path raises `AttributeError: 'Span' object has no attribute 'span'` (§4) | **High — real crash** | Open, minimized repros saved |
| 2 | Round-1 CRASH-1 (lexer EOF `TypeError`) | was High | **Fixed — verified (§2)** |
| 3 | Round-1 BUG-2 (top-level use-before-def) | was Medium | **Fixed — verified (§2)** |
| 4 | Round-1 `1__2` / `1_` lex as valid ints | was Low | **Fixed — verified (§2)** |
| 5 | Round-1 raw NUL accepted inside strings | was Low | **Fixed — verified (§2)** |
| 6 | Round-1 nested `if` / blocks quadratic (~14 s @ 3000, ~30 s @ ~4060) | was Medium | **Fixed — verified (§3)** |

Everything else held or improved: depth guard at 200 works for
10/10 constructs to depth 5000 (was 8/10); the original fuzz seed
is 3000/3000 clean (was 2996/3000); scale 9/9 clean with the 1 MB
string's peak RSS down from 74 MB to 26 MB; CLI 26/26 with the NUL
case now correctly exiting 4; endurance 60 iterations / 15 min,
suite green 60/60, zero flakiness.

## 1. What was run

| Mode | Scale | Result |
|------|-------|--------|
| Targeted re-checks (§2) | 5 old crash repros + 4 fix-round-2 hang repros, direct `newr check` | All clean, all fast |
| Corpus (`--mode corpus`) | 79 cases, same seed 20261106 | 0 crashes/bad, 0 mismatches (§2) |
| Depth sweeps (`--mode depth`) | 10 constructs × grid 100→5000 | 0 breaking depths (§3) |
| Fuzz (`--mode fuzz`, seed 20261106) | 3,000 programs, 10 s timeout each | **3,000 CLEAN, 0 of everything** (§5) |
| Fuzz (`--mode fuzz`, seed 20261107, NEW) | 3,000 programs, 10 s timeout each | **2,999 CLEAN, 1 CRASH (§4)** |
| Scale (`--mode scale`) | 10k lines, 10k vars, 1 MB string, 100k list, 1000 fns, 10k-char ident, 100k-char line, 500-command bot, 100-file import chain | All CLEAN (§6) |
| CLI robustness (`--mode cli`) | 26 recorded cases (see §7 for the count note) | 0 non-clean (§7) |
| Endurance (`--mode endurance`, 15 min) | Full suite + examples + rotating corpus slice | **60 iterations, suite green 60/60** (§8) |

Total: ~7,750 individual `newr check` invocations, plus 27,180
suite-test executions inside endurance (453 tests × 60 iterations).

## 2. Round-1 findings — all verified fixed

Targeted re-checks first, then the corpus sweep:

| Round-1 input | Round-1 result | Round-2 result |
|---|---|---|
| `stress/crashes/fuzz-20261106-727.newr` (truncated mid-identifier) | CRASH `TypeError` | Clean diagnostic, 1 error, 0 warnings, exit 4 |
| `stress/crashes/fuzz-20261106-1714.newr` | CRASH `TypeError` | Clean diagnostic, 1 error, 0 warnings, exit 4 |
| `stress/crashes/fuzz-20261106-1871.newr` | CRASH `TypeError` | Clean diagnostic, 1 error, 0 warnings, exit 4 |
| `stress/crashes/fuzz-20261106-1908.newr` | CRASH `TypeError` | Clean diagnostic, 1 error, 0 warnings, exit 4 |
| `stress/crashes/corpus-b_truncated_fn.newr` | CRASH `TypeError` | Clean diagnostic, 1 error, 1 warning (E2001+W1001 — bare `return` vs `-> int`, as the fix round predicted), exit 4 |
| `printf 'print x'` (no trailing newline) | CRASH `TypeError`, exit 1 | `error[E3001]: undefined name 'x'`, exit 4 |
| `print later` / `let later = 1` | ok, exit 0 | `error[E2005]: use of 'later' before its definition` + W1001, exit 4 |
| `print 0xFF` / `print 0b10` (no newline — fix-round-2 hang repros) | lexer hang (found during fix round 2) | `ok`, exit 0, instant |
| `print 1   ` / `print 1\n   ` (trailing spaces / indented blank line at EOF — hang repros) | lexer hang (found during fix round 2) | `ok`, exit 0, instant |

Corpus sweep: **79 cases, 0 crashes/bad, 0 expected-mismatches**
(round 1: 1 crash + 3 mismatches). The four named corpus cases
individually: `i_bad_number` CLEAN exit 4; `i_use_before_def`
CLEAN exit 4; `b_truncated_fn` CLEAN exit 4; `b_nul_byte` CLEAN
exit 4 — each now errors exactly as its manifest expects.

## 3. Depth — the cliff is gone

Every construct swept at depths 100, 150, 200, 250, 300, 400, 500,
750, 1000, 1500, 2000, 3000, 5000 (30 s timeout each). No construct
broke, so no bisection was needed anywhere.

| Construct | Round 1 | Round 2: worst wall (any depth) | Round 2: wall at depth 5000 |
|-----------|---------|-------------------------------|------------------------------|
| parens | clean to 5000, ≤0.32 s | 0.267 s | 0.183 s |
| binop chains | clean to 5000, ≤0.34 s | 0.225 s | 0.225 s |
| calls | clean to 5000, ≤0.42 s | 0.228 s | 0.228 s |
| list literals | clean to 5000, ≤0.26 s | 0.243 s | 0.185 s |
| dict literals | clean to 5000, ≤0.25 s | 0.244 s | 0.244 s |
| unary chains | clean to 5000, ≤0.30 s | 0.187 s | 0.142 s |
| ternary chains | clean to 5000, ≤0.38 s | 0.223 s | 0.223 s |
| interpolation | clean to 5000, ≤0.26 s | 0.243 s | 0.243 s |
| **nested `if`** | **~14 s @ 3000, ~30 s @ ~4060, 40.9 s @ 5000** | **0.488 s** | **0.488 s** |
| **nested blocks** | **~14 s @ 3000, ~30 s @ ~4374** | **0.446 s** | **0.446 s** |

Direct re-checks of the round-1 breaking inputs (measured this
campaign, not quoted from the fix round): `depth-if-4059.newr`
**0.36 s** (round 1: ~30 s; fix round claimed 0.58 s — measured
faster here), `depth-blocks-4374.newr` **0.40 s** (round 1: ~30 s;
fix round claimed 0.80 s). Both end in clean `E1001`, exit 4.
The guard still trips at depth 200 everywhere, as before.

## 4. NEW — CRASH-2: `t()` error path raises `AttributeError`

**Trigger:** any malformed `t(...)` call — the parser's special
case for the i18n `t()` function raises a raw Python exception
instead of the intended E1001 diagnostic. Two trigger shapes,
both confirmed live:

```
print t("k", 1)    -> AttributeError, exit 1   (2nd positional arg)
print t(1)         -> AttributeError, exit 1   (non-string first arg)
print t("k", item: 1) -> clean diagnostic, exit 4   (control: valid shape)
```

Saved inputs: `stress/crashes/min-crash-t-call-2pos.newr`
(`print t("k", 1)`, 16 bytes) and
`stress/crashes/min-crash-t-call-badkey.newr` (`print t(1)`,
11 bytes), each with a `.report.json` beside it. Full fuzz source:
`stress/crashes/fuzz-20261107-1429.newr` (a mutated `shop.newr`,
1,717 bytes — the mutation deleted the `:` after `item` on line 37,
turning `t("buy.ok", item, price: ...)` into a second positional
argument).

**Traceback (identical in all three):**

```
File "compiler/parser.py", line 1842, in _parse_call
    self.err(args[1].span,
File "compiler/parser.py", line 132, in err
    raise NewrError(Diagnostic(span=tok.span, code="E1001",
AttributeError: 'Span' object has no attribute 'span'
```

**Root cause:** in `_parse_call`, the `t(...)` special case calls
`self.err(args[1].span, ...)` (line 1842) and
`self.err(func.span, ...)` (line 1838). `err()` expects a **Token**
and reads `tok.span`; but `args[i]` and `func` are **AST nodes**,
whose `.span` is already a `Span` object — and `Span` has no
`.span` attribute. Only these two call sites in `parser.py` pass a
node span to `err()`/`_err_code()` (grep-verified). The intended
behaviour is the E1001 diagnostic the messages describe
("`t(...)` takes only named arguments after the key" /
"`t(...)` takes a string key as its first argument") — the fix is
to build the `Diagnostic` with `span=args[1].span` /
`span=func.span` directly instead of going through `err()`.

**Why round 1 missed it:** the fuzzer's token pool contains no
`t` and its plausible-program generator never emits `t(...)`
calls; the trigger needs a `t()` call in exactly the malformed
shape, which only the mutation generator can produce by editing
an example that already contains `t()` (here, `shop.newr`).
**Pre-existing, not a regression:** `parser.py` is byte-identical
to round 1 (`sha256 1cd509ef…`), and fix round 2 did not touch
it. **Impact:** any program with a mistyped `t()` call crashes
`newr check` with a traceback instead of a diagnostic — the exact
failure class the whole campaign exists to eliminate. A user
writing `t("buy.ok", item)` (forgetting `item:`) hits this.

## 5. Fuzz — original seed fully clean, fresh seed finds CRASH-2

| Seed | Cases | CLEAN | CRASH | TIMEOUT | BAD_JSON | WRONG_EXIT |
|------|-------|-------|-------|---------|----------|------------|
| 20261106 (round-1 inputs) | 3,000 | **3,000** | 0 | 0 | 0 | 0 |
| 20261107 (fresh) | 3,000 | 2,999 | **1** (§4, case 1429) | 0 | 0 | 0 |

Round 1, seed 20261106: 2,996 CLEAN, 4 CRASH (all CRASH-1). The
same 3,000 inputs now produce zero failures of any class — the
fix round 2 fixes are confirmed against the exact inputs that
broke before. The fresh seed's single crash is CRASH-2 (§4);
no other failure class appeared in either run: no hang, no
invalid `--json`, no wrong exit code.

## 6. Scale — all clean, one big memory win

| Case | Round 1 wall / peak | Round 2 wall / peak | Exit |
|------|--------------------|--------------------|------|
| 10k lines | 0.73 s / 39 MB | 0.671 s / 36 MB | 0 |
| 10k vars | 5.5 s / 39 MB | 5.055 s / 36 MB | 0 |
| 1 MB string literal | 1.22 s / **74 MB** | 0.749 s / **26 MB** | 0 |
| 100k-element list | 2.25 s / 91 MB | 1.982 s / 80 MB | 0 |
| 1000 functions | 0.41 s / 23 MB | 0.264 s / 22 MB | 0 |
| 10k-char identifier | 0.20 s / 17 MB | 0.142 s / 15 MB | 0 |
| 100k-char line (25k-term `+` chain) | 0.61 s / 37 MB | 0.609 s / 36 MB | 4 (depth guard, clean) |
| 500-command bot | 0.18 s / 18 MB | 0.163 s / 17 MB | 0 |
| 100-file import chain | 0.16 s / 16 MB | 0.142 s / 16 MB | 0 |

Notes: the 1 MB string's peak RSS fell from 74 MB to 26 MB
(the lexer fast paths hold fewer copies); the 10k-`let` case is
still ~5 s (scope-lookup cost, untouched by fix round 2 — same
as round 1, not a regression). Nothing pathological anywhere.

## 7. CLI robustness — 26 recorded cases, 0 non-clean

Byte-level inputs through `check`, plain and `--json`: non-UTF-8
→ exit 2; CESU-8 → exit 2; empty file → exit 0; BOM → exit 0;
lone CR → exit 4 (`E_BAD_LINE_ENDING`); CRLF with an error →
exit 4; NUL inside a string → **exit 4** (round 1: exit 0 — the
NUL fix working as intended). `--json` valid on every failure
mode. Bad invocations all exit exactly as documented (bad flag /
`check` with no file / `build` without `--check` / `explain
E9999` / unknown command → 1; missing file → 2; `--explain` on a
real lexer code and on `E1001` → 0; bare `newr`, `--fast`, `--json`
on the examples → 0).

**Count note:** round 1 recorded 27 cases, round 2 records 26.
That is not a missing case — the harness only appends a record
for a `--json`-on-error-corpus run when it is non-clean. Round 1's
extra record was `json-b_truncated_fn`, the lexer crash; that
input is now clean, so nothing is appended for it. The underlying
runs are identical (14 byte-level + 26 error-corpus `--json` +
12 invocations).

## 8. Endurance — 60 iterations, 15 minutes, zero flakiness

Full suite + all examples + a rotating quarter of the corpus,
repeated for exactly 15.0 minutes
(`stress/results/endurance-20261106-185106.json`).

- **60 iterations, suite green 60/60** — 453 tests × 60 runs =
  27,180 test executions, zero failures.
- **No slowdown, no memory-growth signal:** iteration wall time
  min 14.3 s, mean 15.07 s, max 18.2 s (one outlier, iteration
  53, system noise — the iterations around it are 14–15 s).
  First five iterations average 15.46 s, last five 14.66 s: flat,
  not creeping. (Mean is higher than round 1's 13.13 s only
  because the suite grew 414 → 453 tests.)
- **All examples clean on every iteration.**
- **`slice_bad` is empty in all 60 iterations** — round 1's
  recurring bad entry (`b_truncated_fn`, 35×) is gone.
- **Verdict on endurance:** the compiler is stable under
  repetition. Nothing degrades, nothing flakes.

## 9. Verdict — what stands between here and shipping

Fix round 2 did its job completely: every round-1 finding is
gone, verified against the exact inputs that produced it, and the
perf cliff is not just fixed but ~60–100× faster. The fresh fuzz
seed did what a fresh seed is for: it found one more pre-existing
bug the first campaign could not reach. **Ship gate: NOT passed.**
One fix round 3 is needed for CRASH-2 (two lines in
`parser.py::_parse_call`, §4), with regression tests for both
trigger shapes, then a targeted re-stress: fuzz seed 20261107
(the seed that found it) plus the two minimized repros, plus a
corpus + depth sanity pass. If that comes back clean, there is
nothing left in either campaign standing in the way of shipping.

## Appendix — reproduction

```sh
cd ~/workspace/newr
# CRASH-2, minimal (both crash, exit 1, AttributeError traceback)
printf 'print t("k", 1)\n' > /tmp/t2.newr && python3 newr check /tmp/t2.newr
printf 'print t(1)\n' > /tmp/t1.newr && python3 newr check /tmp/t1.newr
# control: valid shape -> clean diagnostic, exit 4
printf 'print t("k", item: 1)\n' > /tmp/tok.newr && python3 newr check /tmp/tok.newr
# re-run any mode (same harness as round 1)
python3 stress/run_stress.py --mode corpus
python3 stress/run_stress.py --mode depth
python3 stress/run_stress.py --mode fuzz --fuzz-n 3000 --seed 20261106
python3 stress/run_stress.py --mode fuzz --fuzz-n 3000 --seed 20261107
python3 stress/run_stress.py --mode scale
python3 stress/run_stress.py --mode cli
python3 stress/run_stress.py --mode endurance --endurance-min 15
```

Files: this report `STRESS-REPORT-2.md`; harness + corpus
unchanged under `stress/`; crash inputs `stress/crashes/`
(`fuzz-20261107-1429.newr` + `.report.json`,
`min-crash-t-call-2pos.newr` + `.report.json`,
`min-crash-t-call-badkey.newr` + `.report.json`); per-run JSON
`stress/results/` (`corpus-20261106-182228.json`,
`depth-20261106-182250.json`, `scale-20261106-182300.json`,
`cli-20261106-182309.json`, `fuzz-20261106-182936.json` (seed
20261106), `fuzz-20261106-183602.json` (seed 20261107),
`endurance-20261106-185106.json`); compiler pin
`stress/results/compiler-pinned-round2.sha256`. Nothing under
`compiler/`, `SPEC.md` or `tests/` was modified by this campaign.

## Addendum — targeted re-stress after fix round 3 (7 Nov 2026)

Fix round 3 fixed CRASH-2 (`parser.py` `_err_span` helper + 2 call
sites; 13 new tests; suite 466/466 OK). The targeted re-stress is
reported in full in `STRESS-REPORT-3.md` (189 lines): both CRASH-2
minimized repros and the full fuzz-1429 source now give clean E1001,
exit 4; fuzz seed 20261107 re-runs 3,000/3,000 clean; fresh seed
20261108 also 3,000/3,000 clean; corpus 79/79, depth 10/10 clean to
5000, scale 9/9, CLI 26/26, endurance 57/57 green in 15 min.
**Ship gate: PASSED.** This report's §9 "NOT passed" verdict is
superseded — nothing found in any of the three campaigns stands in
the way of shipping.
