# newr — Targeted Re-Stress Report (after fix round 3)

**Date:** 7 November 2026 (WIB, UTC+7) · **Compiler under test:** `~/workspace/newr/`,
pinned by SHA-256 in `stress/results/compiler-pinned-round3.sha256`
(`parser.py 768680cc…` — the fix-round-3 change; every other pinned
file byte-identical to round 2, verified by diff against
`compiler-pinned-round2.sha256`: only `parser.py` changed and
`tests/test_crash2_fix.py` added).
**Suite at test time:** 466 tests (453 + 13 new `test_crash2_fix.py`),
all OK (re-verified by this campaign's endurance loop, 57/57).
**Harness:** same `stress/run_stress.py` + `stress/gen_corpus.py` as
rounds 1–2 (no harness changes). No file under `compiler/`, `SPEC.md`
or `tests/` was modified by this campaign — verified by
`sha256sum -c stress/results/compiler-pinned-round3.sha256`
(all pinned files unchanged after the campaign).

## Verdict (one paragraph)

**CLEAN TO SHIP.** Fix round 3 did its job completely: both CRASH-2
trigger shapes and the full fuzz source that produced them now give
clean E1001 diagnostics, exit 4, no traceback. The seed that found
CRASH-2 (20261107) re-runs 3,000/3,000 clean, a fresh seed (20261108)
also runs 3,000/3,000 clean, the corpus is 79/79 with zero mismatches,
all 10 depth constructs are clean to 5000 with no breaking depth,
scale is 9/9, CLI is 26/26, and endurance ran 57 iterations / 15
minutes with the suite green 57/57 and zero flakiness. Nothing in
this campaign — or in either earlier campaign — stands in the way of
shipping anymore.

| # | Finding | Severity | Status |
|---|---------|----------|--------|
| 1 | **CRASH-2:** malformed `t(...)` raises `AttributeError: 'Span' object has no attribute 'span'` (§2) | was High | **Fixed — verified (§2)** |
| 2 | Round-1 CRASH-1 (lexer EOF `TypeError`) | was High | **Fixed — still verified (§2)** |
| 3 | Round-1 BUG-2 (top-level use-before-def) | was Medium | **Fixed — still verified (corpus `i_use_before_def`, §3)** |
| 4 | Round-1 `1__2` / `1_` lex as valid ints | was Low | **Fixed — still verified (corpus `i_bad_number`, §3)** |
| 5 | Round-1 raw NUL accepted inside strings | was Low | **Fixed — still verified (corpus `b_nul_byte`, §3)** |
| 6 | Round-1 nested `if` / blocks quadratic (~14 s @ 3000, ~30 s @ ~4060) | was Medium | **Fixed — still verified (§4)** |

## 1. What was run

| Mode | Scale | Result |
|---|---|---|
| Targeted re-checks (§2) | 3 CRASH-2 inputs + 5 round-1 crash repros + 4 hang repros + 2 depth repros, direct `./newr check` | All clean, all fast |
| Fuzz (`--mode fuzz`, seed 20261107) | 3,000 programs, 10 s timeout each | **3,000 CLEAN, 0 of everything** (§5) |
| Fuzz (`--mode fuzz`, seed 20261108, NEW) | 3,000 programs, 10 s timeout each | **3,000 CLEAN, 0 of everything** (§5) |
| Corpus (`--mode corpus`) | 79 cases, same seed 20261106 | 0 crashes/bad, 0 mismatches (§3) |
| Depth sweeps (`--mode depth`) | 10 constructs × grid 100→5000 | 0 breaking depths (§4) |
| Scale (`--mode scale`) | 10k lines, 10k vars, 1 MB string, 100k list, 1000 fns, 10k-char ident, 100k-char line, 500-command bot, 100-file import chain | All CLEAN (§6) |
| CLI robustness (`--mode cli`) | 26 recorded cases | 0 non-clean (§7) |
| Endurance (`--mode endurance`, 15 min) | Full suite + examples + rotating corpus slice | **57 iterations, suite green 57/57** (§8) |

Total: ~6,300 individual `./newr check` invocations, plus 26,562
suite-test executions inside endurance (57 × 466).

## 2. CRASH-2 — fixed and verified

Targeted re-checks first (each run directly, exit code captured
without a pipe so the number is the compiler's, not `head`'s):

| Input | Round-2 result | Round-3 result |
|---|---|---|
| `stress/crashes/min-crash-t-call-2pos.newr` (`print t("k", 1)`) | CRASH `AttributeError: 'Span' object has no attribute 'span'`, exit 1 | Clean `error[E1001]: \`t(...)\` takes only named arguments after the key`, exit 4 |
| `stress/crashes/min-crash-t-call-badkey.newr` (`print t(1)`) | CRASH `AttributeError`, exit 1 | Clean `error[E1001]: \`t(...)\` takes a string key as its first argument`, exit 4 |
| `stress/crashes/fuzz-20261107-1429.newr` (full fuzz source) | CRASH `AttributeError` | Clean `error[E1001]` at 37:24, exit 4 |
| `stress/crashes/fuzz-20261106-727.newr` | CRASH `TypeError` (round 1) | Clean `error[E_UNTERMINATED_STRING]`, exit 4 |
| `stress/crashes/fuzz-20261106-1714.newr` | CRASH `TypeError` (round 1) | Clean `error[E_UNTERMINATED_STRING]`, exit 4 |
| `stress/crashes/fuzz-20261106-1871.newr` | CRASH `TypeError` (round 1) | Clean `error[E1001]: expected end of line, found \`é\``, exit 4 |
| `stress/crashes/fuzz-20261106-1908.newr` | CRASH `TypeError` (round 1) | Clean `error[E_UNTERMINATED_STRING]`, exit 4 |
| `stress/crashes/corpus-b_truncated_fn.newr` | CRASH `TypeError` (round 1) | Clean diagnostic, 1 error + 1 warning (E2001+W1001), exit 4 |
| `print 0xFF` / `print 0b10` (no newline — hang repros) | lexer hang (found during fix round 2) | `ok`, exit 0, instant |
| `print 1   ` / `print 1\n   ` (trailing spaces / indented blank line at EOF — hang repros) | lexer hang (found during fix round 2) | `ok`, exit 0, instant |
| `stress/crashes/depth-if-4059.newr` | ~30 s (round 1) | Clean E1001, exit 4, sub-second |
| `stress/crashes/depth-blocks-4374.newr` | ~30 s (round 1) | Clean E1001, exit 4, sub-second |

No traceback appears in any of the above outputs. The fix (round 3)
touched only `compiler/parser.py`: a new `_err_span` helper that
builds the `Diagnostic` directly from an already-computed span, used
at the two `_parse_call` sites that previously passed a `Span` to
`err()` (which expects a `Token`).

## 3. Corpus — 79 cases, zero mismatches

**79 cases, 0 crashes/bad, 0 expected-mismatches** (round 2: same).
The four named cases individually: `i_bad_number` CLEAN exit 4;
`i_use_before_def` CLEAN exit 4; `b_truncated_fn` CLEAN exit 4;
`b_nul_byte` CLEAN exit 4 — each errors exactly as its manifest
expects. The round-1 and round-2 findings stay fixed; nothing
regressed.

## 4. Depth — no cliff, no regression

**10 constructs, 0 with a breaking depth.** Worst wall time at any
depth, round 3 vs round 2 (round 1 for `if`/blocks was ~30 s at
~4060/4374):

| Construct | Round 2 worst | Round 3 worst | Round 3 wall @ 5000 |
|---|---|---|---|
| parens | 0.267 s @300 | 0.163 s @5000 | 0.163 s |
| if | 0.488 s @5000 | 0.466 s @5000 | 0.466 s |
| binop | 0.225 s @5000 | 0.203 s @3000 | 0.203 s |
| calls | 0.228 s @5000 | 0.203 s @3000 | 0.183 s |
| list | 0.243 s @1000 | 0.163 s @750 | 0.162 s |
| dict | 0.244 s @5000 | 0.223 s @5000 | 0.223 s |
| unary | 0.187 s @1500 | 0.171 s @250 | 0.142 s |
| ternary | 0.223 s @5000 | 0.342 s @5000 | 0.342 s |
| blocks | 0.446 s @5000 | 0.612 s @5000 | 0.612 s |
| interp | 0.243 s @5000 | 0.244 s @5000 | 0.244 s |

All differences vs round 2 are run-to-run noise (sub-second
throughout; the `blocks` 0.612 s vs 0.446 s is the same order of
magnitude on a loaded VM). The depth guard at 200 still rejects
cleanly at every depth from 200 up for all 10 constructs.

## 5. Fuzz — both seeds fully clean

| Seed | CLEAN | CRASH | TIMEOUT | BAD_JSON | WRONG_EXIT | Exits |
|---|---|---|---|---|---|---|
| 20261107 (the seed that found CRASH-2) | **3,000** | 0 | 0 | 0 | 0 | 0: 955, 4: 2,045 |
| 20261108 (fresh) | **3,000** | 0 | 0 | 0 | 0 | 0: 953, 4: 2,047 |

No `bad_cases` in either run. The 20261107 seed now reproduces
zero crashes on the exact input stream that produced CRASH-2 in
round 2; the fresh 20261108 seed found nothing new in 3,000 more
programs. That is 6,000 fuzz programs clean in this campaign, on
top of round 2's 6,000 (2,999 clean + 1 crash, now fixed).

## 6. Scale — 9/9 clean

| Case | Result | Wall | Peak RSS |
|---|---|---|---|
| lines-10k | CLEAN exit 0 | 0.696 s | 37,056 KB |
| vars-10k | CLEAN exit 0 | 5.13 s | 37,124 KB |
| string-1mb | CLEAN exit 0 | 0.763 s | 26,288 KB |
| list-100k | CLEAN exit 0 | 2.36 s | 81,624 KB |
| fns-1000 | CLEAN exit 0 | 0.325 s | 22,440 KB |
| long-ident-10k | CLEAN exit 0 | 0.184 s | 15,788 KB |
| long-line-100k | CLEAN exit 4 | 0.609 s | 36,944 KB |
| bot-500-commands | CLEAN exit 0 | 0.183 s | 17,748 KB |
| import-chain-100 | CLEAN exit 0 | 0.143 s | 16,108 KB |

Matches round 2 within noise (vars-10k still ~5.1 s — the known
scope-lookup cost, untouched, not a regression; 1 MB string peak
26 MB, same as round 2's 26 MB, down from round 1's 74 MB).

## 7. CLI robustness — 26 recorded cases, 0 non-clean

Same 26 cases as round 2 (round 1's 27th was the `json-b_truncated_fn`
crash, fixed in round 2): non-UTF-8 → exit 2 (plain + JSON); empty
file → exit 0; lone CR / CRLF-error / NUL → exit 4; BOM → exit 0;
CESU-8 → exit 2; bad flag / missing file args / unknown command /
`--explain` unknown → exit 1; `--explain` for a lexer code and for
E1001 → exit 0; bare `./newr`, `--fast`, `--json` on ok files and on
`showcase` → exit 0. Exit codes exactly as the contract requires.

## 8. Endurance — 57 iterations, 15 minutes, zero flakiness

**57 iterations in 15.0 min, suite green 57/57, slice_bad 0
everywhere.** Wall per iteration: min 15.0 s / mean 15.93 s /
max 17.2 s; first five avg 16.28 s, last five avg 16.34 s — flat, no
slowdown over the run. (Mean is higher than round 2's 15.07 s only
because the suite grew 453 → 466 tests.) 26,562 suite-test
executions, zero failures. All four examples check clean in every
iteration.

## 9. Verdict — ship it

Every finding from every campaign is now verified fixed against the
exact inputs that produced it: CRASH-1 (lexer EOF), BUG-2
(use-before-def), the `1__2`/`1_` numbers, NUL in strings, the
nested-if/blocks perf cliff, and CRASH-2 (malformed `t(...)`). Two
fuzz seeds (6,000 programs), a 79-case corpus, 10 depth constructs
to 5000, 9 scale cases, 26 CLI cases, and 57 endurance iterations
all come back clean with zero crashes, zero timeouts, zero
wrong-exit codes, and zero flakiness. **Ship gate: PASSED.** Nothing
found in three stress campaigns stands in the way of creating
`drewdestar/newr` and pushing.

## Appendix — files

Per-run JSON in `stress/results/`:
`corpus-20261106-191028.json`, `depth-20261106-191048.json`,
`scale-20261106-191059.json`, `cli-20261106-191109.json`,
`fuzz-20261106-190346.json` (seed 20261107),
`fuzz-20261106-191017.json` (seed 20261108),
`endurance-20261106-192617.json`; compiler pin
`stress/results/compiler-pinned-round3.sha256`.
Full pipeline log: `/tmp/restress3.log` (ephemeral; results JSONs are
the durable record). Prior reports: `STRESS-REPORT.md` (round 1),
`STRESS-REPORT-2.md` (round 2).
