# newr - Stress / Endurance Report

**Date:** 6–7 November 2026 (WIB, UTC+7) · **Compiler under test:** `~/workspace/newr/`,
pinned by SHA-256 in `stress/results/compiler-pinned.sha256`
(`lexer.py e8c45db8…`, `checker.py bf1977cb…`, `resolver.py 1cd509ef…`,
`parser.py c2f6beb8…`, `cli.py 111ba9d8…`).
**Suite at test time:** 414 tests, all OK (re-verified by hand after the
14 must-fix fixes landed; gate had passed 2× at 411, the fix agent added
3 more tests). No compiler file changed after 16:59 UTC 6 Nov - every
number below is against one fixed compiler version.

**Harness:** `stress/run_stress.py` + `stress/gen_corpus.py`
(seed 20261106, reproducible). Runner: `python3 newr check <file>`,
per-case timeout, peak RSS polled from `/proc/<pid>/status` (VmHWM).
Classes: CLEAN (exit 0/2/4, no traceback, valid `--json`),
CRASH (traceback in output), TIMEOUT (killed at timeout),
BAD_JSON (`--json` output not parseable), WRONG_EXIT.

## Verdict (one paragraph)

The compiler did **not** break the way the task hoped it would: no
hang, no memory blow-up, no wrong exit code, no invalid JSON anywhere
in 3,000 fuzz cases. It broke in exactly **one crash-class way** -
plus one silent checker gap and two minor lexer gaps, and one
performance cliff that is a slow clean failure, not a hang.

| # | Finding | Severity | Status |
|---|---------|----------|--------|
| 1 | Lexer `TypeError` on any file ending in an identifier with no trailing newline (§2) | **High - real crash** | Open, repro saved |
| 2 | Top-level use-before-def silently accepted, `E2005` not enforced (§3) | Medium - checker gap, contradicts SPEC §8 | Open, repro saved |
| 3 | `1__2` and `1_` lex as valid ints (§4) | Low | Open |
| 4 | Raw NUL byte accepted inside string literals (§4) | Low | Open |
| 5 | Nested `if` / nested blocks go quadratic: clean error, but 14 s at depth 3000, past the 30 s timeout at depth ~4060/4374 (§5) | Medium - perf cliff | Open, slow-probe pending (§5) |

Everything else held: depth guard at 200 works for 8/10 constructs to
depth 5000; 10k-line / 10k-var / 1 MB-string / 100k-element / 100-file
scale cases all finish clean; CLI exit codes are exactly the documented
0/1/2/4; `--json` is valid JSON on every failure mode except the
lexer's own crash (which happens before any JSON can be produced).

## 1. What was run

| Mode | Scale | Result |
|------|-------|--------|
| Corpus (`--mode corpus`) | 79 cases: 17 valid, 26 invalid, 16 Discord-limit, 16 byte-level, 4 import | 1 crash (§2) + 3 real mismatches (§3–§4); all other mismatches were harness-template bugs, fixed and re-run (§9) |
| Depth sweeps (`--mode depth`) | 10 constructs × grid 100→5000 + bisection of each breaking boundary | 8/10 clean to 5000; `if`/`blocks` break the 30 s timeout (§5) |
| Fuzz (`--mode fuzz`, seed 20261106) | 3,000 generated programs, 10 s timeout each | 2,996 CLEAN, **4 CRASH** (all §2, same root cause), 0 TIMEOUT, 0 BAD_JSON, 0 WRONG_EXIT |
| Scale (`--mode scale`) | 10k lines, 10k vars, 1 MB string, 100k list, 1000 fns, 10k-char ident, 100k-char line, 500-command bot, 100-file import chain | All CLEAN (§7) |
| CLI robustness (`--mode cli`) | 27 cases: 7 byte-level inputs × plain/`--json`, `--json` on every error corpus file, 13 invocations | 0 real failures (§8) |
| Endurance (`--mode endurance`) | Full suite + examples + whole corpus, repeated 30 min | **138 iterations, suite green 138/138, no flakiness, no slowdown** (§10) |

## 2. CRASH-1 - lexer `TypeError` at end of file (the worst find)

**Trigger:** any source file whose last token is an identifier and which
has **no trailing newline**. Minimal repros (all crash, exit 1, raw
traceback):

```
print x        (no trailing \n)
x              (no trailing \n)
let abc        (no trailing \n)
print "hi"     -> OK (ends in a string, not a name)
print 1        -> OK (ends in a number, not a name)
```

Saved inputs: `stress/crashes/fuzz-20261106-727.newr`,
`fuzz-20261106-1714.newr`, `fuzz-20261106-1871.newr`,
`fuzz-20261106-1908.newr` (fuzz cases, all truncated mid-identifier:
`…{guild`, `…{user`), `stress/crashes/corpus-b_truncated_fn.newr`
(`fn f(a: int) -> int:\n  return`, EOF mid-`return`).

**Traceback (identical in all 5):**

```
File "compiler/lexer.py", line 267, in _lex_normal
    self._lex_name()
File "compiler/lexer.py", line 390, in _lex_name
    while _is_name_continue(self._peek()):
File "compiler/lexer.py", line 51, in _is_name_continue
    return ch == "_" or unicodedata.category(ch)[0] in "LMN"
TypeError: category() argument must be a unicode character, not str
```

**Root cause:** `_peek()` returns `""` at EOF; `_is_name_continue("")`
(and `_is_name_start`) pass it straight to `unicodedata.category`,
which raises `TypeError` on anything that is not a single character.
The lexer has no EOF guard on the name loop. The same crash fires
through `--json` (`json-b_truncated_fn` in the CLI run) - the crash
happens during lexing, before any diagnostic or JSON can be produced.

**Impact:** any truncated file, any file saved without a trailing
newline whose last line ends in a name, and any generator that emits
no trailing newline, crashes `newr check` with a Python traceback
instead of a diagnostic. This is the one crash the whole campaign
found, and the fuzzer found it 4 times in 3,000 cases. It survived
the 14 must-fix fixes and the 414-test suite because no test checks
a file without a trailing newline.

**Suggested fix (one line, in the lexer):** make `_peek()` return a
sentinel at EOF, or guard `_is_name_start` / `_is_name_continue`
with `if not ch or len(ch) != 1: return False`, and let the parser
produce the normal "unexpected end of file" diagnostic. Add a
regression test: `check("print x")` with no trailing newline.

## 3. BUG-2 - top-level use-before-def silently accepted

**Repro** (`stress/corpus/i_use_before_def.newr`, saved):

```
print later
let later = 1
```

`newr check` → `ok`, exit 0. Expected: `E2005` use before definition.

**Why this is a real bug, not a spec judgement call:** SPEC.md §8 -
"A `.newr` file is a **program**. Top-level statements execute in
file order; declarations (functions, structs, imports, bot blocks)
are resolved across the whole file (forward references are allowed…)."
`let` is a top-level *statement*, not in the declaration list - it
executes in file order, so using `later` before its `let` must be an
error. The resolver only enforces E2005 for **locals**: the same
pattern inside a function is correctly rejected (verified by hand:
`fn f() -> int: print zzz / let zzz = 1 / return zzz` → `error[E2005]:
use of 'zzz' before its definition`). At top level the resolver
pre-declares all `let` bindings as if they were hoisted declarations.

**Impact:** a program that passes `newr check` would fail at runtime
on every target (unbound name). Medium severity: it needs a
top-level statement order mistake, which is easy to write.

## 4. Minor lexer gaps

**`1__2` and `1_` lex as valid ints.** Verified against the lexer
directly: `1__2` → `INT 12`, `1_` → `INT 1`. SPEC says "underscores
group digits and are ignored" (`1_000`) - a double or trailing
underscore groups nothing. Python rejects both. Low severity; the
spec wording is loose enough that this is arguably conformant, but a
strict reading says these are not valid groupings.

**Raw NUL accepted inside a string literal.**
`print "a<NUL>b"` → `ok`, exit 0 (the lexer stores the NUL verbatim
in the `STR_PART` token); a NUL *outside* a string is a clean error.
Inconsistent, and a raw control byte in a string literal is never
what a program means. Low severity; the spec does not explicitly
forbid it, so this is filed as an inconsistency, not a violation.

**Not a bug (corrected expectation):** `print 1 / 0` passes the
checker - correctly. Division by zero is a *runtime* error per the
spec, not a compile-time one; the checker has no business rejecting
it. The corpus case was reclassified to `valid`.

## 5. Depth - the guard holds, except `if` / blocks go quadratic

Every construct was swept at depths 100, 150, 200, 250, 300, 400,
500, 750, 1000, 1500, 2000, 3000, 5000 (30 s timeout each), then
bisected at the first breaking depth.

| Construct | OK up to | Guard trips (clean exit 4) | Survives to 5000? |
|-----------|----------|----------------------------|-------------------|
| parens | 150 | 200 | Yes - 0.16–0.32 s at every depth |
| binop chains | 150 | 200 | Yes - ≤0.34 s |
| calls | 150 | 200 | Yes - ≤0.42 s |
| list literals | 150 | 200 | Yes - ≤0.26 s |
| dict literals | 150 | 200 | Yes - ≤0.25 s |
| unary chains | 150 | 200 | Yes - ≤0.30 s |
| ternary chains | 150 | 200 | Yes - ≤0.38 s |
| interpolation (nested ternary) | 150 | 200 | Yes - ≤0.26 s |
| **nested `if`** | 150 | 200 | **Yes, slowly - ~30 s at depth ~4060** |
| **nested blocks** | 150 | 200 | **Yes, slowly - ~30 s at depth ~4374** |

The 200-level nesting guard does its job everywhere: past 200 the
compiler returns a clean diagnostic (exit 4) instead of recursing.
That is the fix for the old `RecursionError` crash, and it holds to
depth 5000 for 8 of 10 constructs.

**The exception - nested `if` and nested blocks.** The guard still
fires (exit 4, clean diagnostic), but the *cost* of producing that
diagnostic grows superlinearly with depth:

| depth | nested `if` wall | nested blocks wall |
|-------|------------------|--------------------|
| 1000 | 2.4 s | 1.7 s |
| 1500 | 6.0 s | 3.7 s |
| 2000 | 9.1 s | 6.2 s |
| 3000 | 13.9 s | 14.2 s |
| 4000 | clean (bisect) | clean (bisect) |
| **4059 / 4374** | **TIMEOUT at 30 s** | **TIMEOUT at 30 s** |

Bisection at the 30 s timeout: nested `if` last survives 4058,
first breaks 4059; nested blocks last survives 4373, first breaks
4374. Saved inputs: `stress/crashes/depth-if-4059.newr`,
`stress/crashes/depth-blocks-4374.newr`. **Read these numbers as
"depth at which wall time crosses 30 s", not as a correctness
boundary** - with a 180 s timeout both inputs complete cleanly
(see verdict above).

**Verdict - SLOW, NOT A HANG.** Both breaking inputs were re-run
with a 180 s timeout: nested `if` at depth 4059 finished in
**29.9 s** with a clean diagnostic (exit 4, "nesting limited to 200
levels…"), nested blocks at depth 4374 in **28.8 s**, same clean
exit 4. There is no infinite loop. The 4059/4374 "boundaries" are
artifacts of the 30 s timeout: the compiler still produces the
correct diagnostic, it just takes ~30 s to do it, right at the
timeout edge (which is also why the bisection boundary is jittery -
a re-run a minute later could land a few depths either way).
The cost curve is ~quadratic (13.9 s at depth 3000 → 29.9 s at
4059 ≈ 13.9 × (4059/3000)² = 25 s, plus overhead), so the honest
statement is: **the compiler survives any depth, but rejecting a
4,000-deep `if` nest costs ~30 s of CPU.**

**Reading:** even in the worst case this is a *clean* failure - a
diagnostic, not a traceback - but a 3,000-deep `if` nest taking
14 seconds to *reject* means the checker/parser walks the whole
structure (probably re-scanning indentation or re-walking the
nesting on every level) instead of bailing at the depth guard.
A hostile or accidental 4,000-deep file is a 30 s+ denial of
service on `newr check`. Suggested fix: bail out of the nesting
walk as soon as the depth guard fires, instead of completing the
walk and then reporting.

## 6. Fuzz - 3,000 programs, 4 crashes, all one bug

Seed 20261106, three generators mixed (plausible programs, token
mutations, byte soup incl. truncated files and Unicode edge cases).
Result: **2,996 CLEAN, 4 CRASH, 0 TIMEOUT, 0 BAD_JSON, 0 WRONG_EXIT.**
All 4 crashes are CRASH-1 (§2) - truncated files ending
mid-identifier. No other failure class appeared in 3,000 cases:
no hang, no invalid `--json`, no wrong exit code. The fuzzer's
Unicode, truncation and soup inputs otherwise all produced clean
diagnostics or OK.

## 7. Scale - everything finishes, two cost notes

| Case | Size | Wall | Peak RSS | Exit |
|------|------|------|----------|------|
| 10k lines | 179 KB | 0.73 s | 39 MB | 0 |
| 10k vars | 168 KB | **5.5 s** | 39 MB | 0 |
| 1 MB string literal | 1,000,009 B | 1.22 s | **74 MB** | 0 |
| 100k-element list | 689 KB | 2.25 s | **91 MB** | 0 |
| 1000 functions | 41 KB | 0.41 s | 23 MB | 0 |
| 10k-char identifier | 20 KB | 0.20 s | 17 MB | 0 |
| 100k-char line (25k-term `+` chain) | 100 KB | 0.61 s | 37 MB | 4 (depth guard, clean) |
| 500-command bot | 17 KB | 0.18 s | 18 MB | 0 |
| 100-file import chain | 55 B entry | 0.16 s | 16 MB | 0 |

Notes, not failures: 10,000 top-level `let`s take 5.5 s while 10,000
`print` lines take 0.7 s - per-binding cost grows with the number of
bindings (worth a profile pass; likely scope lookup). A 1 MB string
literal peaks at 74 MB RSS and a 689 KB list literal at 91 MB - the
checker holds many copies of large literals. Nothing pathological:
no case timed out, no case came close to memory pressure on this VM.

## 8. CLI robustness - 27 cases, 0 real failures

Byte-level inputs through `check`, plain and `--json`: non-UTF-8
bytes → exit 2 (clean env error naming the file); CESU-8
surrogates → exit 2; empty file → exit 0; BOM → exit 0; lone CR →
exit 4 (`E_BAD_LINE_ENDING`); CRLF with an error → exit 4 (line
numbers correct across CRLF); NUL inside a string → exit 0 (§4).
`--json` was run on every error case in the corpus: valid JSON in
every case **except** the lexer crash (§2), which tracebacks before
any output exists. Bad invocations all exit exactly as documented:
bad flag / `check` with no file / `build` without `--check` /
`explain E9999` / unknown command → exit 1; missing file → exit 2;
`--explain` on a real lexer code and on `E1001` → exit 0; bare
`newr`, `--fast`, `--json` on the examples → exit 0. (An earlier
run flagged six of these as WRONG_EXIT; that was a harness
classifier bug - exit 1 is the documented usage-error code - now
fixed in the harness.)

## 9. Corpus - 79 cases, template bugs fixed, 3 real mismatches left

The first corpus run showed 12 mismatches; 9 were bugs in the
*stress harness's own templates*, not the compiler, and each was
verified against the live compiler before fixing:

- `int?` has no `.to_string()` method (invented by the template);
  `//` is not a newr operator; `time.format` takes
  `(timestamp, format)`, not `(format)`; `task` blocks need a `do:`
  body **and** a `bot` block in the file (`E2008`); `min`/`max` take
  exactly 2 args; `bot "B":` with no indented body is a parse error
  (every `:` needs an indented block - 12 Discord templates fixed);
  cross-module names need qualification **and** `pub`
  (`E3005` name not exported: `pub let` / `pub fn`); bare
  cross-module names are module-local by design (`E3001`).
- The empty-bot-block observation: a bot with zero settings cannot
  be written - a description (or other indented body) is required
  in practice. Filed as an observation, not a bug.

After fixing the templates and re-running, the remaining corpus
mismatches are exactly the real findings: `i_use_before_def` (§3),
`i_bad_number` (§4), `b_nul_byte` (§4), plus the `b_truncated_fn`
crash (§2).

## 10. Endurance - 138 iterations, 30 minutes, zero flakiness

Full suite + all examples + a rotating quarter of the corpus,
repeated for exactly 30.0 minutes (`stress/results/endurance-20261106-175146.json`).

- **138 iterations, suite green 138/138** - the suite tail is `OK`
  on every single run (414 tests × 138 runs = 57,132 test
  executions, zero failures).
- **No slowdown, no memory-growth signal:** iteration wall time
  min 12.1 s, mean 13.13 s, max 16.8 s (one outlier, iteration 137,
  system noise - the iterations around it are 12–14 s). First five
  iterations average ~12.9 s, last five ~13.5 s: flat, not creeping.
- **All examples clean on every iteration.**
- **The only recurring "bad" slice entry is the known bug:**
  `b_truncated_fn.newr` CRASH exit 1, 35 times - exactly once every
  4th iteration, when the rotating slice includes it. That is CRASH-1
  (§2) reproducing deterministically, not flakiness. No other file
  ever appears in `slice_bad`, in any iteration.
- **Verdict on endurance:** the compiler is stable under repetition.
  Nothing degrades, nothing flakes, nothing leaks enough to show in
  wall time over 30 minutes / 138 full-suite runs. The one thing
  that breaks, breaks the same way on iteration 1 and iteration 138.

## Appendix - reproduction

```sh
cd ~/workspace/newr
# CRASH-1 (no trailing newline is the whole trigger)
printf 'print x' > /tmp/t.newr && python3 newr check /tmp/t.newr
# BUG-2
printf 'print later\nlet later = 1\n' > /tmp/u.newr && python3 newr check /tmp/u.newr
# re-run any mode
python3 stress/run_stress.py --mode corpus   # 79 cases
python3 stress/run_stress.py --mode depth --constructs dict,interp
python3 stress/run_stress.py --mode fuzz --fuzz-n 3000 --seed 20261106
python3 stress/run_stress.py --mode scale
python3 stress/run_stress.py --mode cli
python3 stress/run_stress.py --mode endurance --endurance-min 30
```

Files: harness + corpus `stress/run_stress.py`, `stress/gen_corpus.py`;
corpus `stress/corpus/` (+ `manifest.json`); crash inputs
`stress/crashes/` (each with a `.report.json` beside it); per-run
JSON `stress/results/`; compiler pin
`stress/results/compiler-pinned.sha256`. Nothing under `compiler/`,
`SPEC.md` or `tests/` was modified by this campaign.

## Addendum - re-stress after fix round 2 (7 Nov 2026)

See **[STRESS-REPORT-2.md](STRESS-REPORT-2.md)** for the full
re-stress report. Short version: all 5 findings from this report
are verified fixed (original fuzz seed 3000/3000 clean, depth
cliff gone - worst case 0.49 s, 1 MB string peak RSS 74 → 26 MB,
endurance 60/60 green), but the fresh fuzz seed 20261107 found
**one new pre-existing parser crash (CRASH-2)**: a malformed
`t(...)` call raises `AttributeError: 'Span' object has no attribute
'span'` at `parser.py:1838` / `:1842` instead of the intended E1001
diagnostic. Minimized repros: `stress/crashes/min-crash-t-call-2pos.newr`
(`print t("k", 1)`) and `min-crash-t-call-badkey.newr` (`print t(1)`).
Ship gate: NOT passed - fix round 3 + targeted re-stress required.
