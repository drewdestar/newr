# Cold evaluation — newr M0 + M1

**Date:** Fri 2026-11-06 ~23:25 WIB
**Evaluator:** independent cold audit (not involved in building). Four parallel reviewers: compiler internals, SPEC-vs-code + G1–G20, adversarial testing, tests/CI/docs. Baseline re-run by the coordinator: `python3 -m unittest discover -s tests` → **354 tests, OK**; `newr check` on all 5 `examples/*.newr` → all `ok` (shop.newr: ok + 3 warnings, as claimed).
**Scope:** `~/workspace/newr/` — SPEC.md (2,135 lines by `wc -l`, not 2,129 as claimed), BUILD_REPORT.md, README.md, `compiler/` (errors, lexer, ast_nodes, parser, resolver, checker, discord_check, discord_limits, modules, cli), `tests/`, `examples/`, `.github/workflows/ci.yml`, `conformance/`.
**Method:** fresh read + live execution. Adversarial inputs live in `/tmp/newr-adv/` (88 files, not in the repo). Nothing in `~/workspace/newr/` was modified except this file.

## Overall verdict

**M0/M1 is genuinely good front-end engineering with real test coverage — and it is NOT ready for M2 as-is.** The core (lexer/parser/resolver/checker) is implemented, not stubbed: diagnostics are rustc-style, the thinking-compiler warnings are all real, Discord limit arithmetic is exact, and the 354-test suite plus the 13-failure red-check both replicate exactly under independent re-running.

But the audit found **3 crash-class bugs** (raw Python tracebacks, exit 1, on ordinary adversarial input), **2 type-system bugs that make flagship features (`T?`, `ensures result`) broken through the real CLI**, and a pattern of **spec/code drift** (24 diagnostic codes missing from SPEC §16, several internal spec contradictions, README/CI claims that are false as written). The most damning single finding: a **false-green test** — `test_contracts_first_are_ok` passes while the verbatim spec example it covers fails through the CLI, because the test helper ignores resolver diagnostics.

**Recommendation: fix the must-fix list below before M2.** M2 (Python emitter) built on top of a checker that rejects `let x: int? = 5` and crashes on 500 nested parens would bake those bugs into generated code and the conformance corpus.

---

## Verdict per area

### 1. General-purpose language — PASS, with one honest caveat

The spec is genuinely general-purpose (§0, §1): types, `let`/`const`, `if`/`for`/`while`, functions + recursion (verified live: recursive `fact` checks `ok`), modules/imports, error handling (`try`/`catch`, `Result`, `?`), collections, strings, math, I/O (`print`/`file`/`env`). `discord` is one module; core imports contain zero Discord data (`checker.py:32–42` imports only stdlib/`ast_nodes`/`errors`/`modules`; `discord_check.py:57` is the only importer of `discord_limits`; boundary test enforces it).

**Caveat:** integer semantics (§7.5: int64, checked overflow, truncating `/`, dividend-sign `%`, div-by-zero = runtime error) is **spec-frozen only**. The code enforces just the int-literal range (`lexer.py:60–61,436–438`, `E_INT_OVERFLOW` verified live). `1/0`, `9223372036854775807 + 1` pass `newr check` cleanly — correct for M1 (no emitter, no const-eval), but G2's bare "Applied" overstates it. Runtime enforcement is M2/conformance work, and `conformance/` currently contains only a README (see §5).

### 2. Cross-platform — PASS, with 3 real bugs

No POSIX-only code anywhere: zero `subprocess`/`fcntl`/`signal`/`/dev/`/`os.unlink`/`os.environ`; all I/O via `pathlib.Path.read_text(encoding="utf-8")`; import resolution via `pathlib` (`resolver.py:342`). CRLF accepted, BOM stripped, lone-CR detection exists in the lexer. CI matrix (ubuntu/macos/windows × Python 3.10–3.13) is real in the file — **but it has never run** (no git repo in `~/workspace/newr/`, nothing pushed), and as written it **cannot go green on macOS/Windows** (see M-12).

Bugs:
- **M-06 (must-fix): CRLF byte offsets wrong in JSON output.** Spans are computed on CRLF-normalised text (`lexer.py:110–117`) but `cli.py` serialises against the raw file. Verified live: line-2 `y` reported `start_byte 22`, on-disk byte is 23 (off by one per preceding CRLF line). The `errors.py:15–19` "spans are byte offsets" contract holds only for LF files. Any LSP/`--fix` consumer trusting JSON offsets on a CRLF file edits the wrong byte.
- **M-07 (must-fix): lone CR silently accepted through the CLI.** The lexer raises `E_BAD_LINE_ENDING`, but `Path.read_text()` universal-newline translation converts `\r` → `\n` before the lexer sees it, so `print "a"\rprint "b"` checks `ok`. The lexer unit test passes; the real CLI path defeats it. SPEC §3.1 says a lone CR "is an error".
- **N-08 (nice-to-have):** the `newr` launcher is an extensionless shebang file — only runs on Windows as `python newr ...`. Its docstring says so honestly; the packaged form is console-script/zipapp. SPEC §3.7 already labels Windows/macOS execution UNVERIFIED.

### 3. Stability contract — MOSTLY REAL, 2 leaks

A Discord **limit/rule** change (lengths, intents, command-name regex) is contained to `discord_limits.py` + `discord_check.py` — REAL, test-enforced (`tests/test_boundaries.py`). A Discord **surface** change is not fully contained:
- **N-01:** core checker hardcodes the string `"discord"` twice (`checker.py:973–974`, `checker.py:1333` — bypass returning `UNKNOWN` / synthesising args), plus `resolver.py:68` (`NAMED_MODULES` includes `"discord"`) and `resolver.py:556–561` (special-case `import discord` unused-import exemption).
- **N-02:** Discord entity type *names* live in core AST (`ast_nodes.py:63–68` `ENTITY_TYPE_NAMES = {"user","member","guild","channel","role","interaction","embed","message"}`; `checker.py:51` mirrors it). Fields are correctly injected via providers, but adding/removing an entity *type* touches core. The boundary test explicitly exempts this ("the test targets imports, not words", `tests/test_boundaries.py:1–8`) — i.e. the test was written around the leak rather than the leak being fixed.

A target-language change touching only its emitter: **UNVERIFIABLE in M1** — no emitter exists yet, so the claim is structural (emitters land M2+) rather than proven. Honest as a design statement, not a verified fact.

### 4. Performance + thinking compiler — THINKING: REAL. PERFORMANCE: SPEC-ONLY (honestly labelled)

**Thinking compiler is real — every claimed reasoning feature is implemented and tested:**
W1001 unused var/param/const/option (`resolver.py:547–555`, skips `pub`), W1002 unused import (`resolver.py:556–565`), W1003 unreachable code (`checker.py:1657–1681` + `_stmt_terminates`, incl. `while true:` without `break`), W1005 dead branch/literal condition (`checker.py:1771–1777`, bool-`Lit` only — narrow by design per SPEC §5.1), W1004 shadowing (`resolver.py:472`), W1008 int-division pitfall (`checker.py:402–414`, only int literals into float context — narrow), E2010 not-all-paths-answer (`discord_check.py:572–603`), W1006 unbounded loop in handler, W1009 reply-length-unverifiable, W1010 privileged intent, one-error-per-body recovery (`checker.py:132–148`). No STUB/ABSENT among §5.1 warnings.

**Caveats:**
- **Parser has NO error recovery at all** (`parser.py:114–116` `err()` raises immediately). First syntax error aborts the whole compile. `errors.py:224–228` ("first error per function body") describes checker/resolver recovery only — a reader could reasonably expect per-body parse errors, which do not exist.
- **Performance/incremental is spec-only.** Grep for `hashlib|sha256|cache` in `compiler/*.py`: the only hit is `cli.py:320–334` `_source_cache`, a per-run in-memory dict for rendering diagnostics. No content hashing, no per-module cache, no `--no-cache`/`--stats`. Zero benchmark/timing measurements anywhere. This is **honestly disclosed** (SPEC §4.1, §4.2, §24.2 #11; BUILD_REPORT §6; README "No performance numbers are claimed anywhere") — credit where due. The design (SHA-256 per file, `(content hash, compiler version)` key, import-graph invalidation) exists only as spec prose; cache lands M4, benchmarks M2.
- **`--fast` undersells its skip (N-06):** it skips both `check_program` AND `check_discord` (`cli.py:305–313`), but help text says only "skips the full type check" — a `--fast` pass says nothing about E2008/E2010/W1006/W1009/W1010 either. The stderr warning ("not verified") covers it in spirit.
- **`Fix` field: real as data, absent as a command.** Populated only by the resolver's similar-name path (`resolver.py:201`); the checker never attaches one (zero `fix=` hits in `checker.py`). No `newr fix` subcommand — matches SPEC §5.2 (M4), but SPEC §5.2's "M1 designs the diagnostic `fix` field and populates it where the fix is safe" overstates it.

**Diagnostics are genuinely rustc-style:** byte-offset `Span`, `Diagnostic` with `code`/`expected`/`found`/`help`/`note`/`suggestion` (applicability certain/maybe) / `fix`, human render (`error[E2001]`, `--> file:line:col`, carets, `= help:`), JSON output (`--error-format=json`, `--json` envelope). **Hole: only 29 of 38 emitted codes have `--explain` text** (M-11). The 9 lexer codes (`E_BAD_ESCAPE`, `E_BAD_INDENT`, `E_BAD_LINE_ENDING`, `E_BAD_NUMBER`, `E_INT_OVERFLOW`, `E_STRAY_BRACE`, `E_TAB_INDENT`, `E_UNEXPECTED_CHAR`, `E_UNTERMINATED_STRING`) are un-numbered `E_*` strings that break the documented scheme (`errors.py:26–27`: `E1xxx` syntax / `E2xxx` types / `E3xxx` resolver / `W1xxx` warnings). Verified: `newr explain E_BAD_INDENT` → exit 1, "unknown diagnostic code".

### 5. Best-of-all + wishlist (§23, §24) — PASS, honestly versioned

§23 renamed "What newr takes from other languages", slogan explicitly disclaimed; README contains no "best of all" slogan. §24 wishlist: all 19 of chie's ideas evaluated (§24.2), 24 deferred/rejected items (§24.4) with reasons, nothing silently dropped. **§24.3 is real content** (SPEC.md:1986–2009): a 14-row research-findings table with sources, dates and verdicts — the merge the M0 brief owed is actually done. §24.4 deferrals are versioned (match/enums/user-generics/`?`/HTTP/JSON → v1.1; regex/subprocess/voice/Components V2 → no version; prefix commands/eval/escape-hatch/ternary/classes/crypto → rejected with reasons).

**BUT the spec contradicts itself in at least 3 places (M-13):**
- Similarity gate: §16 + `resolver.py:87` say **0.7**; §25 U6 says **0.6** ("the v0 value"). `errors.py:27–29` docstring also still says 0.6. Code uses 0.7.
- Manifest: §11 says the `newr.json` schema is "defined now"; §25 item 7 says the exact schema is "decided in M4/M6". (And the §11 "schema" is one vague inline line with no required/optional, no formats, unexplained `db_path`.)
- Phantom artefacts: SPEC §17.3 lists `diagnostics.py` in the compiler layout — the real file is `errors.py`; §16 promises "every code documented in an error-index file" — no such file exists (`--explain` dict in `cli.py` is the de facto index); §17.3 claims "a repo test asserts the built zipapp behaves identically to the checkout" — **no zipapp test exists** (zero hits for `zipapp` in `tests/`).

**Conformance is README-only (M-14):** `conformance/` holds exactly one file, `README.md` (44 lines). No programs, no goldens, no escape-test corpus. README.md's project-layout line calls it "the conformance corpus — the single definition of correct behavior" — false; there is no corpus and therefore no definition of correct behaviour beyond the spec and unit tests. BUILD_REPORT §1 honestly calls it a "Conformance plan".

### 6. Correctness — 354/354 green, but the suite has thin spots and the CLI crashes

Baseline re-verified by three reviewers independently: 354 tests OK, per-file counts exact (lexer 42, parser 71, resolver 45, checker 104, discord_check 52, boundaries 3, v0_compat 13, CLI 24). The **red-check replicates exactly**: forcing every `BinOp` to `op="+"` in a `/tmp` copy → **FAILED (failures=13)**, the exact count claimed. Caveat: only ~6 parser tests assert `.op` directly, and only for special ops (`??`, `**`, `-`, `==`, `in`); **no test asserts the op for plain `+`, `*`, `/`, `%`, `<`, `>`, `and`, `or`**. With no evaluator in M1, a parser bug swapping `-` for `*` on ints would be invisible. 7 of the 13 red-check failures are checker knock-ons, not direct op assertions.

**Thin spots (N-07):** 34 of 56 `assertEqual(errs(...))` assertions are just `assertEqual(errs(r), [])` ("this program is clean") — they pass vacuously if the checker silently skips a construct. Error recovery: 2 tests. Resolver cycles: 1 test. `--explain`: CLI tests exercise only 2 of 29 codes (E2001, E3001). No CLI test for `check --fast`. v0 invalid-file tests use inline strings, not real v0 files, and the "same verdicts as the v0 compiler" comparison rests on the author's knowledge — v0 code is not in this repo.

**What held up under adversarial attack:** Discord boundary arithmetic is exact (reply 2000 passes / 2001 fails E2008; description 100/101; placeholder max-expansion 1977+1+22=2000 passes, 1978 fails); match exhaustiveness (E2006), no-coercion, hetero/empty-list, contract position, import cycle/missing/duplicate errors all correct; CLI exit-code contract clean (0/1/2/4) on every non-crashing edge; Unicode identifiers (RTL Hebrew, combining marks), BOM, CRLF, `\u{…}` escapes, `catch err:` all behave per spec; `count` correctly NOT a reserved name.

**Crashes and bugs found — see must-fix list (M-01–M-05, M-08–M-10).**

### 7. G1–G20 — headline overstates; detail mostly honest

Per-item verdicts (evidence in BUILD_REPORT §7, verified against spec/code):

| # | Verdict | Note |
|---|---|---|
| G1 | APPLIED | General-purpose rewrite; core walks discord decls only for typing (`checker.py:2138`), enforces no Discord rules. Boundary test exists. |
| G2 | **SPEC-ONLY** | Int64 frozen in spec §7.5; code enforces literal range only. Overflow/truncation/div-by-zero have zero implementation (no emitter). "Applied" overstates. |
| G3 | APPLIED | `while`, recursion, W1006. Verified live. |
| G4 | APPLIED (typing/syntax) | Closures/`fn` types in checker; capture-by-value spec-only, unobservable without emitter. |
| G5 | APPLIED | `Propagate`/`?`, nested `?` parse error. |
| G6 | APPLIED | `file`/`env` in `modules.py:72–78`; HTTP deferred with dated sketch. Note: named modules need no `import` in practice (§21.2/§21.5 pass without one) vs §11/§12.2 "import by name" wording — ambiguity, not failure. |
| G7 | APPLIED | No coercion; verified live (`let x: float = 5` → E2001 + `float(5)` hint). |
| G8 | APPLIED (checker) | `s[0]` → E2001 + `.chars()` hint. Code-point `len` is runtime semantics, spec-only. |
| G9 | **PARTIAL — honestly labelled** | Resolution/`pub`/named imports real; manifest schema one vague line; compiler support M4. |
| G10 | APPLIED, 1 contradiction | R1–R11, byte spans, gate 0.7 — but §25 U6 still says 0.6. |
| G11 | APPLIED (spec-only by nature) | Generics erasure/reification strategy; nothing observable without emitters. |
| G12 | APPLIED (mostly) | `\|>`, `??`, `?.`, match exhaustiveness E2006 (verified live), contracts type-checked. "Runtime-only contracts" enforcement spec-only. |
| G13 | APPLIED | PyPI/pipx/zipapp/Releases/PowerShell, no npm. No code aspect. |
| G14 | **PARTIAL — design only** | All targets UNVERIFIED; **zero benchmark numbers anywhere**. BUILD_REPORT's "Applied (design) / UNVERIFIED (numbers)" label is honest. |
| G15 | APPLIED | Compiler/LSP/AI boundary §5.3; LSP M2+, correctly absent. |
| G16 | APPLIED | §23 rename, Haxe head-on, fame timeline; README has no slogan. |
| G17 | APPLIED, trivial mismatch | `__init__.py:7` → `0.1.0`. SPEC line 4 says old line is 0.5.0; BUILD_REPORT G17 row says 0.6.0. |
| G18 | APPLIED (spec) | `task` in discord checker; zero "thread" hits in compiler. Thread-safety promise spec-only. |
| G19 | APPLIED | `T?` spelling, methods at `checker.py:1257`. |
| G20 | **PARTIAL / vacuous** | §20.1 design present, but no `extern` syntax and no inline-target-code construct exists for the compiler to reject — nothing enforces it yet. "Compiler rule" overstates. |

**Bottom line on G1–G20:** 13 applied, 4 partial/spec-only (G2, G9, G14, G20 — of which G9/G14 are honestly labelled), 2 applied-with-contradiction (G10, G17), 1 applied-but-unobservable (G11). The headline "All 20 applied" (§7) overstates it; the honest headline is "13 applied, 4 specified-not-implemented, 3 with caveats".

---

## Must-fix before M2 (14)

### M-01 — Parser/CLI crashes with raw `RecursionError` on deeply nested input (CRASH)
No depth limit anywhere (`parser.py`/`lexer.py`/`checker.py`); `cli.py:main` catches only `_UsageError`/`_EnvError`. Verified live, exit 1 + traceback (not the documented exit 4):
- 500 nested parens → `RecursionError` in `parser.py:_parse_prefix` (400 passes; threshold ~481–500).
- 500-term `1 + 1 + …` chain → `RecursionError` in `resolver.py:_walk_expr` / `checker.py:_synth_BinOp` (450 passes).
- 260 nested `if true:` → `RecursionError` in `checker.py:_walk_block/_walk_If` (240 passes).
- `--fast` doesn't help (parser crashes first); `--json` / `--error-format=json` emit **no JSON at all**, just the traceback.
**Fix:** add a nesting-depth guard in the parser (and ideally resolver/checker walks) producing a clean "too deeply nested" diagnostic (E1001 family), AND/OR catch `RecursionError` in `cli.py:main` and convert it to exit 4 with a diagnostic. Add regression tests at the boundary (e.g. 600 parens, 600-term chain, 300 nested `if`s).

### M-02 — Non-UTF-8 source file crashes with `UnicodeDecodeError` traceback (CRASH)
`_read_source` (`cli.py`) catches only `OSError`, not `UnicodeDecodeError`, even though SPEC §3.1 requires "an error naming the file". Verified: file with `\xff\xfe` bytes → traceback, exit 1.
**Fix:** catch `UnicodeDecodeError` in `_read_source`, report a clean error naming the file, exit 2 (environment error per §19).

### M-03 — `T` is not assignable to `T?` (type-system bug, most severe)
`let x: int? = 5`, `let x: str? = "hi"`, `fn f() -> int?: return 5`, `let s: str? = env.get("HOME") or "d"` all fail E2001 ("mismatched types, expected 'int?', found 'int'") with the backwards help "unwrap the value first…". Only `none` and already-nullable values can inhabit `T?`. The suite never catches it: it only ever tests `int? = none` and `?.` chains. Nullable types are nearly unusable for producing values.
**Fix:** in `checker.py:_check`, accept a `T` value where `T?` is expected (widening). Add tests: `int? = 5`, `str? = "hi"`, `-> int?` return, `or`-default assigned to `T?`.

### M-04 — `ensures result` is unbound through the CLI; the covering test is false-green (BUG)
The verbatim SPEC §9.7 `withdraw` example (`ensures result >= 0`) fails with resolver `E3001: undefined name 'result'`, contradicting the spec ("`result` is bound in `ensures`"). `test_contracts_first_are_ok` passes only because the test helper asserts **checker diagnostics only and ignores resolver diagnostics** — a false-green test masking a CLI-visible bug. Same masking pattern may hide other resolver-vs-checker gaps.
**Fix:** bind `result` in the resolver scope for `ensures` clauses (and audit the test helpers: every test that asserts diagnostics must assert the *combined* resolver+checker diagnostics, or explicitly justify ignoring one). Add a CLI-level test running the §9.7 example end-to-end.

### M-05 — Hex/binary integer literals skip the overflow check (BUG)
`0x8000000000000000`, `0xFFFFFFFFFFFFFFFFFF`, 65-bit binary literals all pass `newr check`; the same values in decimal correctly fail `E_INT_OVERFLOW`. Only the decimal path in `lexer.py:_lex_number` range-checks.
**Fix:** apply the int64 range check in the hex/binary branches too. Add tests for `0x8000000000000000` and 65-bit binary.

### M-06 — CRLF byte offsets wrong in JSON output (BUG)
See §2. Spans computed on normalised text, serialised against the raw file; off by one per preceding CRLF line (verified live: `start_byte 22` vs true 23).
**Fix:** compute spans on raw bytes, or normalise the source before the CLI reads it for rendering (single source of truth for offsets). Add a test: CRLF file with an error on line 2+, assert JSON offsets match on-disk bytes.

### M-07 — Lone CR silently accepted through the CLI (WRONG VERDICT)
See §2. `Path.read_text()` universal-newline translation converts `\r` → `\n` before the lexer sees it.
**Fix:** read with `newline=""` (no translation) so the lexer's `E_BAD_LINE_ENDING` can fire, or detect `\r` in `_read_source`. Add a CLI-level test (lexer test alone is insufficient — it already passes while the CLI path defeats it).

### M-08 — int64 minimum `-9223372036854775808` unrepresentable (BUG)
Fails `E_INT_OVERFLOW` because the literal `9223372036854775808` is range-checked before unary minus is applied. `int` is signed 64-bit (§7.5); −2⁶³ must be representable.
**Fix:** allow the literal `9223372036854775808` when directly negated by unary minus (or range-check after folding unary minus on literals).

### M-09 — `??` rejects a nullable right-hand side (BUG)
`a ?? b` with both operands `str?` fails E2001 ("expected `str`, found `str?`", span on `b`); `a ?? b ?? "d"` fails the same way, parenthesised or not. Combined with M-03 there is **no way** to coalesce two optionals.
**Fix:** decide the rule in SPEC §9.4 (`T? ?? T?` → `T?`, or `T? ?? T` → `T`) and implement it in the checker; at minimum the error must not point at a legal-looking RHS. Add tests for `T? ?? T?`, chains, and parenthesised chains.

### M-10 — `?.` on a non-nullable receiver silently accepted (WRONG VERDICT)
`"hi"?.upper()` passes `newr check`; only a nonexistent method name produces an error. `?.` is null-safe access on `T?`; on a non-nullable receiver it should be an error (or at least a warning).
**Fix:** checker: `?.` on a non-nullable receiver → E2001 (or W-warning if the spec prefers leniency — but decide it in the spec, don't stay silent). Add tests.

### M-11 — 9 lexer diagnostic codes have no `--explain` text and break the code scheme (DOC GAP)
See §4. `E_BAD_ESCAPE`, `E_BAD_INDENT`, `E_BAD_LINE_ENDING`, `E_BAD_NUMBER`, `E_INT_OVERFLOW`, `E_STRAY_BRACE`, `E_TAB_INDENT`, `E_UNEXPECTED_CHAR`, `E_UNTERMINATED_STRING` — emitted, surfaced, but `newr explain <code>` → exit 1 "unknown diagnostic code".
**Fix:** either renumber them into the `E1xxx` scheme and add `--explain` entries + SPEC §16 entries, or document the `E_*` lexer-code family in SPEC §16 and add explain text. Also fix the stale `errors.py:27–29` docstring (says gate 0.6; code is 0.7).

### M-12 — CI as written cannot go green on macOS/Windows; Windows `python3` unproven (CI BUG)
`.github/workflows/ci.yml` (173 lines, real matrix, golden hashes verified exact: `hello.py` `1eb3265b…`, `showcase.py` `a8222f1b…`):
- **Case-collision step guarantees red on macOS/Windows:** writes `casecheck/Foo.newr` and `casecheck/foo.newr`, asserts both exist. APFS (default) and NTFS are case-insensitive — the second write overwrites the first and the assert fails. The comment even says "fails on case-insensitive filesystems — that is the point". Make it Linux-only or expect-failure.
- **`python3` on windows-latest unproven:** v0-conformance, zipapp-diff, golden-rebuild, CRLF steps invoke `python3` under `shell: bash`; Windows Python provides `python.exe`, not `python3.exe`, by default. Whether `actions/setup-python@v5` shims `python3` on Windows is not established anywhere in the repo.
- **Zipapp cross-OS claim false as written:** header says "a Linux-built .pyz runs on the Windows and macOS runners", but the job carries the full matrix — each runner builds its own `.pyz` locally; no Linux-built artifact is ever transferred or executed cross-OS. Single-artifact portability is not tested. (Also `zipapp -p "/usr/bin/env python3"` is a Unix-only shebang; harmless as `python3 newr.pyz`, meaningless on Windows.)
- Minor: the `encoding="utf-8"` grep check is vacuous — `compiler/` contains zero `open(` calls (all reads are `Path.read_text(encoding="utf-8")`), so it passes while testing nothing.
**Fix:** scope the case-collision step to Linux (or mark expect-failure), prove-or-replace `python3` on Windows (use `python` or a shim step), and either actually transfer one Linux-built `.pyz` to the other runners or drop the cross-OS claim. CI has never run (no repo) — the matrix is unproven until GitHub Actions runs it.

### M-13 — SPEC.md internal contradictions + missing code documentation (DOC GAP)
- Gate 0.6 vs 0.7 (§16 + `resolver.py:87` say 0.7; §25 U6 says 0.6; `errors.py` docstring says 0.6).
- Manifest "defined now" (§11) vs "decided in M4/M6" (§25 item 7).
- `diagnostics.py` listed in §17.3 compiler layout — file is `errors.py`.
- No error-index file despite §16 promising one.
- **15 codes emitted by the code have NO entry anywhere in SPEC.md:** E2006–E2010, E3003–E3008, W1006–W1010 (exist only in `cli.py:142–269` explain texts + call sites). BUILD_REPORT §5's "Spec" column (§9.6, §13, §14, §13.8, §15) cites sections that describe the rules but never name the codes — misleading. SPEC §16 lists only a 14-code "starter set"; all 14 are implemented, but the other 15 + 9 lexer codes are undocumented.
- SPEC line count: claims 2,129, actual 2,135.
- `fix` field: SPEC §5.2 overstates ("populates it where the fix is safe") — only the resolver attaches one.
- README stale vs BUILD_REPORT: "M1: front end in progress" vs "M1 COMPLETE"; exit code "3 network" listed though M1 has no network operations and `cli.py` only ever returns 0/1/2/4.
- §21.4 shop is not self-contained as printed (import placed after the `bot` block; extracted to `/tmp` it fails E3008 because the import resolves against `/tmp`). Extraction artifact, but §21.4 cannot be verified from the printed text alone. Also `examples/hello_bot.newr` header says `# hello.newr` (wrong filename), and §21.2/§21.5 use `file.read`/`env.args` with no import while §11/§12.2 describe named modules as imported by name.
**Fix:** reconcile §16 (document all 38 codes or the code scheme), fix the three contradictions, rename `diagnostics.py` → `errors.py` in §17.3 (or create the index file), correct the line count, update README status + exit codes, fix the §21.4 import placement and the `hello_bot.newr` header comment.

### M-14 — README over-claims present capabilities; conformance dir mislabelled (DOC GAP)
- README lead: "the compiler produces readable output in seven target languages — one readable source, seven readable outputs". Present tense, stated as current fact. **No emitter exists in M1** — the same file later says "emits nothing yet" / "emitter lands in M2+". The lead contradicts the Status section.
- Install section: `pip install newr-lang` + "pipx install, zipapp single file, GitHub Releases, PowerShell installer". None exist: PyPI returns 404 (re-checked today), no GitHub repo at all, no releases, no installer; zipapp exists only as a manual smoke test (full distribution is M4).
- "Windows/macOS support is proven by the CI matrix" — the matrix has never run; should say "will be proven by".
- `conformance/` called "the conformance corpus — the single definition of correct behavior" in README's layout — there is no corpus (see §5).
- Targets table (discord.py 2.7.1, discord.js ^14.28.1, JDA 6.7.0, Discord.Net 3.20.2, serenity 0.12.5 + poise 0.7.1, Kord 0.18.1, nyxx 6.9.1) is acceptable as a roadmap with "emitter lands M2+" in the Status column, but none of these versions is pinned by any working integration yet.
**Fix:** rewrite the README lead + install section in future tense / "planned", fix the conformance line, fix the "proven by" wording. BUILD_REPORT §6's honesty is the model here — copy its tense.

---

## Nice-to-have (10)

### N-01 — Discord leaks by name in core (`checker.py:973–974,1333`; `resolver.py:68,556–561`)
See §3. "Zero Discord dependencies" is true at import level, false at string level. **Fix:** route the `discord`-module bypass through the same provider-injection mechanism used for entity fields, or document the by-name exemption in SPEC §2 as an accepted boundary (with the boundary test updated to assert the exemption list, not exempt "words").

### N-02 — Discord entity type names live in core AST (`ast_nodes.py:63–68`)
See §3. Adding/removing an entity type touches core. **Fix:** move `ENTITY_TYPE_NAMES` behind the provider interface, or document it as a deliberate core-owned list.

### N-03 — Duplicated, unfrozen precedence table (`parser.py:53–55` vs `parser.py:1464–1483`)
Module-level `_BINARY_BP` (`{"PIPE": 1, "QQ": 2}`) is **never referenced** — dead code. The live table is the `_binary_bp` method. Neither is frozen. Precedence logic itself is sound (`**` right-assoc, comparisons non-associative with explicit error, `??`/`and`/`or` mixing rejected without parens). **Fix:** delete the dead dict, freeze the live table (e.g. `MappingProxyType`/tuple), add a test asserting the table matches SPEC §9.

### N-04 — Parser error recovery absent; `errors.py` wording misleads
See §4. First syntax error aborts the whole compile. **Fix (M2+ or later):** panic-mode recovery with synchronisation, or at minimum reword `errors.py:224–228` to say recovery applies to checker/resolver only, not the parser.

### N-05 — Misleading messages (minor, grouped)
- Import cycles emit spurious `E3006` (top-level statements not allowed in imported file) + `W1002` (unused import) alongside the real E3004 — noise caused by the cycle itself. Suppress follow-on diagnostics when a cycle is reported.
- Dangling `?` (`let x = 1 ?`) misdiagnosed as wrong-return-type propagate (E2001); `print g()?` → bare "expected end of line, found `?`" with no hint `?` is the issue.
- `let log = 1` → E1001 parse error ("expected a variable name, found keyword 'log'") while `let print/len/math = 1` give E3007 — inconsistent handling of the same reserved-name rule. (`log` IS in `RESERVED_BUILTIN_NAMES`, `modules.py:47–50` — confirmed.)
- `option[int]` → `E3001: unknown type 'option'` with no hint to use `T?`.
- Case-mismatch import: `import "helper.newr"` when only `Helper.newr` exists gives generic E3008; §3.3 mandates naming the on-disk file that differs only in case.

### N-06 — `--fast` help undersells the skip
See §4. Help text should say it skips the type check AND all Discord validation.

### N-07 — Test-suite thin spots
Plain-operator identity untested (`+`/`*`/`/`/`%`/`<`/`>`/`and`/`or` — no test asserts `.op`); 34 pass-if-clean assertions; error recovery 2 tests; resolver cycles 1 test; `--explain` CLI-tested for 2/29 codes; no CLI test for `check --fast`; CRLF lexer-only. **Fix:** add op-identity tests for every binary operator, convert key pass-if-clean tests into positive assertions (e.g. assert the typed AST node exists), add `--explain` CLI tests for all codes, add `check --fast` and CRLF-through-CLI tests.

### N-08 — `newr` launcher not Windows-executable as-is
See §2. Honest docstring; packaged form is console-script/zipapp. No action until M4 packaging — just don't claim otherwise.

### N-09 — Stdlib spec table is loose by construction (`modules.py`)
`BUILTINS` uses string sentinels (`"any"`, `"any-num"`, `"lenable"`, `"T"`) collapsed to `UNKNOWN`; real checking is special-cased in `_call_builtin`/`_call_sig`. `set`'s signature embeds string `"T"` where a `Type` is expected; `input`'s spec is odd (`params: [STR]` + `optional: [STR]` — prompt simultaneously required and optional; `_call_sig` treats it as 1–2 args, so `input()` with 0 args is accepted despite `params` saying 1). Cosmetic, but the table is not a faithful spec of the checker.
**Fix:** tighten the table or mark it explicitly as approximate; decide `input()` arity in the spec.

### N-10 — Two SPEC-vs-code stdlib contradictions
- **`list.len()` claimed by SPEC, rejected by the compiler:** SPEC.md:1168 lists `list[T]` methods as `append, contains, len(), get`; `METHODS["list"]` (`modules.py:100–104`) has only `append/contains/get`. Verified live: `xs.len()` → `error[E3001]: 'list[int]' has no method 'len'` (`len(xs)` builtin works). **Fix:** add the method or correct the spec.
- **`time.format` accepts 2 formats the SPEC doesn't list:** SPEC.md:1132 fixes 3 formats; `TIME_FORMATS` (`modules.py:75`) has 5, adding `"%d/%m/%Y"` and `"%H:%M:%S"` (verified live: accepted, exit 0). The checker enforces the set, so code is self-consistent — the spec is stale (or the code over-permissive). **Fix:** reconcile (5 formats in the spec, or remove 2 from the code).

---

## What the BUILD_REPORT gets right (for balance)

- Test counts exact per file; 354 green re-verified; red-check 13 failures replicated exactly in a `/tmp` copy.
- Golden SHA-256 pins exact (`hello.py` `1eb3265b…`, `showcase.py` `a8222f1b…`).
- v0 examples clean with exactly the claimed warnings; §22 gap honestly disclosed (§3, §6, §9 item 9).
- §6 is honest about Windows/macOS, performance, live Discord limits, `newr fix` payloads, PyPI name.
- Similarity gate 0.6→0.7 in code with rationale; `Member.optional` exists; `store.get` ambiguity resolved in SPEC.md:1416–1425; `pub fn` params don't warn W1001; §21.2 `count.newr` and §21.5 `cli.newr` extracted from SPEC both check exit 0; PyPI `newr-lang` still 404 (re-checked).
- **Unverifiable from the files (not false, just unprovable):** "restored byte-identical" (no git repo, no backup artifact), "tests written TDD RED first" (no version history), §8 v0 facts (no v0 code in this repo), Disnake/poise external claims.

## Suggested M2 entry criteria (from this audit)

1. M-01–M-05 fixed + regression tests (crashes, `T?` widening, `ensures result`, hex overflow).
2. M-06–M-10 fixed (CRLF offsets, lone CR, int64 min, `??` RHS, `?.` receiver).
3. M-11 + M-13: all 38 codes documented in SPEC §16 with `--explain` coverage; contradictions resolved.
4. M-12: CI green on all 3 OSes at least once (or the case-collision step scoped to Linux + Windows `python3` proven).
5. M-14: README rewritten to future tense for emitters/install.
6. N-07: op-identity tests + combined-diagnostics test helpers (the M-04 false-green must not recur).
7. Then: conformance corpus seeded (even a small one) before the emitter lands, so M2 has something to prove byte-parity against — right now there is no corpus for the M2 exit criterion to measure.
