# newr — BUILD REPORT (M0 spec freeze + M1 front end)

**Status: M0 + M1 COMPLETE — 6 November 2026, ~23:15 WIB.**
**M1 fix pass: COMPLETE — 6 November 2026, ~23:52 WIB** (cold evaluation
`EVAL-M0M1.md`: all 14 must-fix issues + all 10 nice-to-haves fixed,
see §11 below).
**Repo:** `~/workspace/newr/` — LOCAL ONLY. No GitHub repo created, nothing
pushed, nothing committed to any remote. `~/workspace/discord-py-skills`
was read only, never modified.
**Version:** 0.1.0 (`compiler/__version__`).

M0 froze the spec (`SPEC.md`, 2,129 lines at freeze; 2,191 after the M1
fix pass) as a **general-purpose
programming language** — one language for all, with `discord` as one
domain module — after the user's 21:49 WIB correction, the 21:50 WIB
all-OS requirement, the 21:58 WIB stability contract, the 21:59 WIB
powerful/faster/thinking principles, the 22:02 WIB "best of all" /
wishlist chapters, and the 22:12 WIB deep research (G1–G20 — the
headline in §7 was corrected in the fix pass: 13 applied, 4
specified-not-implemented, 2 applied-with-contradiction (fixed), 1
applied-but-unobservable).

M1 built the front end in Python (stdlib only): lexer → parser →
AST (byte-offset Spans) → resolver → checker (core + discord) → CLI.
**No emitter exists yet** (M2/M3). M1 was NOT split into M1a/M1b — the
whole front end landed in one milestone, green.

---

## 1. What was built (file list)

### Spec & docs

| File | Lines | What it is |
|---|---|---|
| `SPEC.md` | 2,191 | Frozen v1 spec (M0, revised per G1–G20; 2,129 lines at freeze, 2,191 after the M1 fix pass). See §6 for the numbering gap (§22 missing). |
| `README.md` | 128 | Honest v1 story ("one readable source, seven readable outputs…"), install, targets, status. No "best of all" slogan claim, no performance numbers. Install is future tense: `newr-lang` is not on PyPI yet. |
| `docs/error-index.md` | 79 | The full 38-code error index (SPEC.md §16): 29 numbered + 9 lexer `E_*` codes. A test enforces it matches `EXPLAIN` exactly. |
| `conformance/README.md` | 51 | Conformance plan: Gleam model (one program → every target → compare stdout + exit code), bot mock round-trip corpus, per-target goldens (never hand-edited), shared escape-test corpus, rustc-style UI snapshot tests, baselines blessed only after human review, never auto-blessed. Seeded in M1 (6 ok + 6 err programs, `tests/test_conformance.py`). |
| `conformance/ok/` | 6 files | Programs that must check clean (exit 0, no warnings). |
| `conformance/err/` | 6 files | Programs that must fail (exit 4) with the code in their `# expect: <CODE>` header. |
| `.github/workflows/ci.yml` | 210 | GitHub Actions matrix ubuntu/macos/windows × Python 3.10–3.13, fail-fast false. Jobs: front-end suite, v0 conformance (`python newr check` on the examples), build-zipapp (ubuntu builds once, uploads artifact) + zipapp behavior-diff (matrix downloads the SAME .pyz), golden rebuild check (SHA-256 pins verified this session — see §3). Case-collision step is Linux-only (APFS/NTFS are case-insensitive); `python` (not `python3`) everywhere; encoding grep covers `read_text()` + `open()`. Per-target emitter jobs land M2+. |

### Compiler (`compiler/`, stdlib only, zero POSIX-only code)

| File | Lines | What it is |
|---|---|---|
| `__init__.py` | 7 | Package docstring + `__version__ = "0.1.0"`. |
| `errors.py` | 264 | `Span(file, start_byte, end_byte)` — **raw-file byte offsets** (G10, M-06 fix: a CRLF newline counts 2 bytes in a span); `line_col()`; `MAX_NESTING_DEPTH = 200` (M-01); `Suggestion` (applicability "certain"\|"maybe"); `Diagnostic` (span/code/message/severity/expected/found/help/note/suggestion/fix — no `hint` field); rustc-style `format(source_text)`; `to_json()`; `NewrError` (docstring: recovery is checker/resolver only, parser has none); `CompilerResult` (`.ok`, `.errors`). |
| `lexer.py` | 635 | Mode-stack lexer (normal/string/interp); tokens with raw-file byte-offset spans; 2-space indent (steps exactly +2, dedents land on a stack level); `#` comments + blank lines ignored; CRLF normalized (spans stay raw-byte), lone CR error, BOM stripped (3 raw bytes), tabs banned; ints (decimal/underscores/hex/binary, int64 range check — 2^63 allowed through for `-9223372036854775808`, larger is E_INT_OVERFLOW), floats, exponents; duration literals (`60s/5m/1h/1d` → seconds); strings with escapes, `{{`/`}}`, `{expr}` interpolation (nested strings/dict literals work with a space after `{`); bracket continuation; Unicode identifiers (Thai/Devanagari combining marks lex inside names — v0 command-name rule). Error codes E_BAD_NUMBER, E_BAD_ESCAPE, E_UNTERMINATED_STRING, E_STRAY_BRACE, E_TAB_INDENT, E_BAD_INDENT, E_BAD_LINE_ENDING, E_INT_OVERFLOW, E_UNEXPECTED_CHAR. Keywords include `enum match pub requires ensures`; tokens include `QMARK ?`, `QDOT ?.`, `QQ ??`, `PIPE |>`, `BAR |`. |
| `ast_nodes.py` | 710 | Full AST. `Type` gains `nullable: bool` (`T?`), `Result[T, E]`, `fn(T..) -> R`; `BUILTIN_TYPE_NAMES` (no `option`, plus `Result`, `fn`); `GENERIC_ARITY`; `nullable_of` / `result_of` / `fn_of` helpers. Nodes: Lit, Name, BinOp, UnaryOp, Ternary, Lambda (capture by value), TryExpr, Propagate (statement-level `?`), Call, TCall, Member (**`optional: bool = False`** — the M1 `?.` amendment, see §5), Index, Interp, ListLit, DictLit, EmbedExpr, Let/Const (`pub` flag), Assign, If, For, While, patterns (Lit/Wildcard/Name/Enum/Struct/List/Or), MatchArm, Match, Require, Ensure, Try, Throw, Return, Break, Continue, Pass, Reply, Defer, Edit, Followup, Send, Log, ExprStmt, Button/Select/ModalDecl, Param, Import (`alias`, `bound_name`, `resolved_path`), Struct/StructField, FnDecl, EnumDecl/EnumVariant, BotDecl, OptionDecl, CommandDecl (`effective_body()` desugars v0 `reply:` sugar), EventDecl (`effective_body()`), TaskDecl, StringsDecl, StoreDecl, Program (`files`, `modules`). |
| `parser.py` | 1,853 | Recursive-descent + Pratt parser. `parse(source, filename="<string>") -> Program`, first syntax error raises NewrError E1001 with a byte-offset span. Precedence is the frozen `PRECEDENCE` table (MappingProxyType, SPEC.md §9.2); the dead `_BINARY_BP` dict is gone. Depth guard: `parse_expr` / `parse_block` / `parse_type` stop with E1001 past 200 levels (M-01). `let log` → E3007 (reserved builtin name), like `let print`. | `?` disambiguation: QMARK followed by NEWLINE/DEDENT/EOF/COMMA/RPAREN/RBRACK/COLON is not ternary (left for statement-level propagate); nested `?` (`f(g()?)`) is E1001. `??` mixed with `and`/`or` unparenthesized is a parse error. `print <expr,...>` without parens AND `print(...)`; `log <expr>` → Log, `log.info(...)` an ordinary call. Duration literals only in `task every`/ `timeout:`. Command names joined from IDENT/MINUS/INT tokens; subcommands rejected. `t(...)` → TCall. v0 reply/log sugar into `reply_expr`/`log_expr` (NOT desugared by the parser). `Program.modules` left empty (resolver fills it). |
| `resolver.py` | 996 | Name resolution: imports (cycle-checked, relative to the importing file), duplicate top-level names, `pub` visibility, reserved names, E3001 with rustc named-form similar-name help + machine-applicable fix, E2005 use-before-definition, assignment-target validation. Fills `Import.resolved_path`, `Program.files`, `Program.modules` (**for every module, not just main** — nested imports, see §5). M1 fix pass: `result` is bound in `ensures` conditions (M-04); depth guard past 200 levels → E1001 (M-01); an import cycle reports only E3004 (no E3006/W1002 follow-ons); case-mismatch imports name the on-disk file; non-UTF-8 imported files are E3008. |
| `checker.py` | 2,407 | Core checker. No Discord imports (verified by `test_boundaries.py`, which also asserts the exact by-name exemption list — SPEC.md §2.1). `check_program(program, providers=None) -> CompilerResult`. R1–R11 inference, no implicit numeric coercion, `T?`/`?.`/`??`, Result/`try`/`?`, match exhaustiveness, contracts, value types, operator/index/member/call typing, builtins, one-error-per-function-body recovery. Fills `expr_type` on every expression. M1 fix pass: `T` widens to `T?` (M-03); `??` accepts a `T?`/`none` right side (M-09); `?.` on a non-nullable receiver is E2001 (M-10); `2^63` is an error unless directly negated (M-08); depth guard past 200 levels → E1001 (M-01); named modules work without an import (M-10/N-09); `input()` takes 0–1 args, `list.len()` exists (N-10); `option[int]` gets a `T?` hint (N-05). |
| `discord_check.py` | 1,112 | Discord checker (the only module importing `discord_limits`). `check_discord(program) -> list[Diagnostic]`; `discord_providers(program) -> dict` + `DISCORD_TYPE_PROVIDERS` (entity/store member-type tables the core checker consumes via `providers`). Bot/command/event/option/intent/component/embed/store/strings rules, reply-length estimation, every-path-answers, handler-hang warning. Depth guard past 200 levels (stops silently — the core already reported E1001). |
| `discord_limits.py` | 190 | Pure Discord constants only (command-name regex, description/reply limits, placeholder max expansions, entity field maxes, options/components/embeds limits, intents). No store/strings/task rules. |
| `modules.py` | 149 | Core stdlib signatures: BUILTINS (print/input/len/str/int/float/bool/min/max/abs/round/range/set/ok/err), MODULES (math/time/random/file/env — `env.get -> str?`, `env.args: list[str]` incl. program name at index 0), method tables (list/dict/set/str, `str.chars()`, nullable + Result methods), RESERVED_BUILTIN_NAMES (builtins + math/time/random/file/env/t/discord/**log** — `log` added in M1, see §5). The table is marked APPROXIMATE in its docstring (the checker special-cases some builtins); `input` takes 0–1 args and `list` has `len` (N-09/N-10). |
| `cli.py` | 679 | The `newr` CLI (SPEC.md §19). See §4. |
| `newr` (launcher) | 20 | Executable launcher: adds the checkout dir to sys.path, dispatches to `compiler.cli:main`. Works as `python3 newr …` (and `./newr …` on Unix; the pip console-script form is M4 packaging). |
| `__main__.py` | 9 | zipapp entry point (`python3 -m zipapp . -o newr.pyz`). Same entry point as the launcher. |

### Tests (`tests/`, unittest — pytest is NOT installed in this VM)

| File | Tests | Covers |
|---|---|---|
| `test_lexer.py` | 42 | Tokens, spans (incl. byte offsets for non-ASCII), indent, strings/interp, numbers, durations, CRLF/BOM, error codes, new tokens (`?`/`?.`/`??`/`\|>`/`\|`), leading-comment regression. |
| `test_parser.py` | 71 | Full grammar, precedence, `?` disambiguation, all 5 examples, v0 sugar, error spans. |
| `test_resolver.py` | 45 | Clean programs, E3001/similar-name, E2005, duplicates/reserved, handler bindings, W1001/W1002/W1004, file imports (cycle/missing/unknown/json/`pub`/E3005/E3006/alias), `catch err:`, `log.info`, nested imports. |
| `test_checker.py` | 104 | No-coercion, operators, collections, `T?`/`?.`/`??`, Result/`?`, match, contracts, functions, control flow, builtins, interpolation, recovery, nested imports. |
| `test_discord_check.py` | 52 | Bot/commands/events/options/intents/components/embeds/store/strings, reply length, every-path-answers. |
| `test_boundaries.py` | 6 | Core checker never imports `discord_check`/`discord_limits` (sys.modules + source scan) + the exact by-name exemption list (SPEC.md §2.1): `"discord"` occurs only at the two checker bypasses, the resolver registry + W1002 exemption, and `RESERVED_BUILTIN_NAMES`. |
| `test_v0_compat.py` | 13 | hello_bot.newr + showcase.newr parse+resolve+check with zero errors; invalid v0 files fail with the same verdicts. |
| `test_cli.py` | 25 | End-to-end through the `newr` launcher (subprocess): help/version, check/build/--fast/--check, --error-format=json, --json envelope, --explain, missing file, unknown command, v0 examples, zipapp behavior-diff (built .pyz matches the launcher on all 5 examples + 1 err case). |
| `test_eval_fixes.py` | 54 | M-01..M-11 + N-03/N-05/N-06/N-07/N-09/N-10 regression tests (TDD RED first), incl. the combined-diagnostics `full()` helper, op-identity tests for all binary/logical operators, and the error-index ↔ EXPLAIN equality test. |
| `test_conformance.py` | 2 | Conformance corpus verdicts: every `conformance/ok/*.newr` checks clean, every `conformance/err/*.newr` fails with its `# expect:` code. |
| **Total** | **414** | |

### Examples (`examples/`)

`hello.newr` (`print "Hello, world!"`), `hello_bot.newr` + `showcase.newr`
(v0 bot files, conformance baseline — unchanged), `hello.py` +
`showcase.py` (v0 generated goldens — unchanged, byte-identical reference
for M2), `shop.newr` + `shop_items.newr` (SPEC §21.4: `pub fn price_of`,
bot ShopBot, strings en/id, store economy, commands ping/buy/balance,
events, task 1h).

---

## 2. Test results

```
$ python3 -m unittest discover -s tests
Ran 414 tests in ~16s
OK  (0 failed, 0 skipped)
```

Per-file counts are in §1. The suite was written TDD (RED first, then
GREEN) for every component: lexer, parser, resolver, checker,
discord_check, CLI. pytest is NOT installed in this VM; the suite uses
unittest only. The M1 fix pass added 60 tests (354 → 414): 54 in
`tests/test_eval_fixes.py` (written RED against the broken behaviour
first — 40 of 53 failed on the first run), 3 in `test_boundaries.py`,
1 zipapp diff in `test_cli.py`, 2 in `tests/test_conformance.py`.

**Red-check (this session):** the parser was deliberately broken
(every `BinOp` reported `op="+"`) → **13 failures**; restored from backup
→ **354 green, byte-identical to the pre-break file**. The suite catches
real parser regressions, not just import errors.

**Red-check (M1 fix pass, this session):** the same break was repeated
after the fixes → **25 failures** (13 op-identity + more, incl. the
conformance programs that exercise operators); restored → **414 green,
byte-identical**. The op-identity tests added in the fix pass are what
catch the break now.

---

## 3. Verification (before reporting done)

- **Full suite green:** 414 tests, 0 failed, 0 skipped (§2).
- **v0 examples parse+resolve+check cleanly:** `newr check` on
  `examples/hello_bot.newr` and `examples/showcase.newr` → exit 0, zero
  errors (13 v0-compat tests pin the verdicts for invalid v0 files too:
  bad command name, duplicate command, fn-name collision, bad
  placeholder, reply too long, empty description, unknown key, duplicate
  event, no bot block).
- **All 5 examples check via the CLI:** hello / hello_bot / showcase /
  shop_items → `ok`; shop.newr → `ok, 3 warning(s)` — all legitimate:
  W1010 (privileged intent `members` declared) + W1009 ×2 (reply length
  cannot be verified for the two `t()` replies with `str` interpolations).
  No new warnings vs the M1 baseline.
- **SPEC examples check:** §21.2 `count.newr` and §21.5 `cli.newr`,
  extracted from SPEC.md and run through `newr check` → exit 0.
- **zipapp behavior-diff (repo test, not just smoke):**
  `tests/test_cli.py::TestZipappDiff` builds the zipapp and asserts the
  same verdicts + identical stdout as the launcher on all 5 examples +
  1 err case. CI runs the same check with the ubuntu-built .pyz on all
  three OSes.
- **CI golden pins verified:** `sha256sum examples/hello.py` =
  `1eb3265b…` and `examples/showcase.py` = `a8222f1b…` — exactly the
  hashes pinned in `.github/workflows/ci.yml`. The CI's front-end,
  v0-conformance, and zipapp jobs can now run (they reference the `newr`
  launcher, which exists as of this milestone). The CI matrix itself has
  still never run — Windows/macOS stay UNVERIFIED until it goes green.
- **SPEC.md complete and consistent** with the design docs as corrected
  by the user (general-purpose, all OSes, stability contract,
  powerful/faster/thinking, "best of all"/wishlist with the honesty
  correction, G1–G20 — headline corrected in the fix pass, §7). One
  numbering gap remains: there is no §22 (sections go 21 → 23) —
  pre-existing, left as-is, do not renumber silently in a later
  milestone; record the fix if it is ever made.
- **M1 fix pass verification (EVAL-M0M1.md, §11):** every must-fix and
  nice-to-have re-verified live (CLI + unit tests), incl. the three
  headline bugs (`let x: int? = 5`, 500-deep nesting, verbatim §9.7
  `ensures result` example).

---

## 4. CLI (M1 behaviour — SPEC.md §19)

```
newr [--json] [--error-format=json] <command> [args]
  check <file> [--fast]  verify (parse, resolve, check); writes nothing
  build <file> [--target py|js] [--check] [--fast]
                         M1: --check only; emitters land in M2
  help [command]         show help (bare `newr` prints help, exit 0 — v0 behaviour)
  version / --version    newr 0.1.0
  explain <code>         long-form explanation (also: --explain <code>)
```

- **Exit codes:** 0 success, 1 usage error, 2 environment error
  (unreadable file), 4 build/check error. Warnings never fail the build.
  (v0 had a 3 = network code; M1 has no network operations.)
- **`build` without `--check`** → exit 1: "newr build cannot emit yet —
  emitters land in M2. Use `newr build --check <file>`…".
- **`--fast`** → parse + name resolution only, skips the full type check
  AND all Discord validation, with a stderr warning ("--fast skips the
  full type check and all Discord validation; the result is not
  verified"). Accepted on `check` too, same semantics.
- **`--json`** → stdout is exactly one JSON object, the v0 envelope
  `{ok, command, newr, data, warnings, error}` (`error` = first error as
  `{code, message, hint}`; `warnings` = warning diagnostics;
  `data.diagnostics` = all diagnostics as JSON objects). A crash-class
  input (deep nesting) still emits a JSON envelope with E1001 — never
  a bare traceback.
- **`--error-format=json`** → stdout is a pure JSON array of diagnostics
  (line/col included); the exit code is the verdict.
- **Human mode:** rustc-style diagnostics on stdout + a one-line summary
  (`<file>: ok` / `ok, N warning(s)` / `N error(s), M warning(s)`).
  Diagnostics from imported files render against their own file's source.
- **`--explain`** covers all 38 codes (E1001, E2001–E2010, E3001–E3008,
  W1001–W1010, plus the 9 lexer `E_*` codes) with 1–2 paragraph human
  explanations. The code list is pinned by `docs/error-index.md` and
  the index ↔ EXPLAIN equality test.
- **Composition seam** (for M2/CLI reuse):
  ```python
  resolved = resolve_source(source, str(path))   # or resolve_file(path)
  providers = discord_providers(resolved.program)
  core = check_program(resolved.program, providers=providers)
  discord_diags = check_discord(resolved.program)
  diagnostics = resolved.diagnostics + core.diagnostics + discord_diags
  ```
  Core must run before discord (discord reads `expr_type`, which the
  core checker fills on every expression).

---

## 5. Decisions made

### M0 (spec — from the design docs as corrected by the user)

Recorded in SPEC.md §0 (changelog) and throughout; the headline ones:
general-purpose language (Discord is one module, not the definition);
`while` and recursion granted (the v1 bans are reversed, with reasons);
error model = unchecked exceptions primary + `Result[T, E]` value form
(`try <expr>`, `ok`/`err`, statement-level `?`); file I/O + `env.args`
in v1, HTTP deferred to v1.1 with a dated design sketch; no implicit
numeric coercion; `int` = signed 64-bit, checked overflow = runtime
error, truncating `/`, dividend-sign `%`, div-by-zero = runtime error,
`float` = IEEE-754 binary64; `str` = UTF-8, `len` counts code points, no
integer indexing, `str.chars()`; modules = file-stem binding + `as`
alias + qualified access + `pub` visibility (private by default);
bot/command/event/task must live in the main file; manifest `newr.json`
schema is a DRAFT (required name/version, optional rest), compiler
support M4; integer semantics frozen (§7.5);
inference = local/bidirectional R1–R11 (NOT Hindley–Milner, NOT gradual
typing/`any`); diagnostics = rustc form + Elm tone; generics semantic
decided at M0 (erasure Python/TS/Java/Kotlin, reification C#/Rust/Dart,
unobservable via 3 rules); user generics deferred to v2; stability
contract (core has zero Discord dependencies; if Discord changes, only
the discord module changes; if a target changes, only its emitter
changes); portability chapter (pathlib-style paths, LF+CRLF accepted, no
OS-specific syscalls, module names case-sensitive even on
Windows/macOS, no Unix-only features in v1); distribution = PyPI
`newr-lang` + pipx + zipapp + GitHub Releases + PowerShell installer
(**npm DROPPED** — HIGH RISK / UNVERIFIED, no precedent); standalone
binary (PyInstaller/Nuitka) = post-v1 decision; performance chapter
(incremental + caching design, idiomatic generated code, fast startup —
all UNVERIFIED until benchmarked); thinking-compiler chapter (inference,
smart diagnostics, `newr check` reasoning, `newr fix` for safe fixes —
M4; AI models / code generation / guessing intent explicitly OUT);
"Best of all" chapter renamed "What newr takes from other languages"
(the table stays — features taken, NOT a "best of all" slogan claim,
which the research judged NOT HONEST for v1); wishlist chapter (§24.3)
with every item evaluated v1 / deferred / rejected-with-reason; chie's
19 ideas evaluated (§24.2); fame timeline as a planning horizon (0–1 yr
liked, 1–3 yr used to build, 3–7 yr jobs/teaching, 7–15 yr default
somewhere); Haxe precedent addressed head-on.

### M1 (implementation — what the code decided)

**Ownership (decided during M1, record for M2):**
- E2005 (use before definition) is raised by the **resolver**, not the
  checker — scope order is known there. It applies to locals and to a
  let's own RHS referring to itself; forward references to later
  top-level names are allowed.
- Assignment-target validation lives in the **resolver** (only
  var/param/option are assignable).
- Type-name validation (`x: Nosuch` → E3001) lives in the **checker**.

**Resolver rules refined while testing:**
- Similarity gate **0.6 → 0.7** (SPEC.md §16 updated): the v0 gate was
  0.6, but 0.6 admits false positives once builtins join the candidate
  pool (`coun` matches `round` at 0.667 as well as `count` at 0.889, so
  no single suggestion fires). 0.7 keeps genuine typos, drops the false
  positives. Documented and tested, not claimed "standard".
- **Parameters of `pub` functions do not warn W1001** — they are the
  function's public contract (SPEC.md §5.3 updated). Found when the
  `pub fn price_of(item: str)` fixture warned on its idiomatic unused
  parameter.
- A **single-match similar-name suggestion counts as a use** of the
  suggested binding: `let count = 1` + `print coun` is one E3001, not
  E3001 plus a spurious W1001 for `count`.
- Top-level `let`/`const` are **pre-declared** (forward references
  allowed); the statement walk hides a let's own binding while checking
  its RHS, which is what makes self-reference E2005.
- **`catch err:` may shadow builtins.** SPEC.md §10's own example is
  `catch err:`, but `err` is a builtin name (a Result builder) and the
  resolver rejected it with E3007. Fix: catch variables are transient
  error bindings, not redefinitions — `_declare(allow_builtin=True)`
  for catch names only. Every other declaration still gets E3007.
- **`log` is a builtin name.** SPEC.md §12.1 documents
  `log.info/warn/error(msg)`, but `log` was not in
  `RESERVED_BUILTIN_NAMES`, so `log.info("x")` died in the resolver
  with E3001 before the checker (which special-cases `log`) ever saw
  it. Fix: `log` added to the reserved set in `modules.py`. Bare
  `log <expr>` is a statement (a Log node) and never reaches the
  resolver as a name.
- **Nested imports now work end-to-end.** The resolver filled
  `Program.modules` only for the main file, so an imported file's own
  file-imports were invisible to the checker: a type error through a
  nested import (`a → b → c`) was silently unreported, and a file
  imported twice under two names was checked twice (duplicate
  diagnostics). Fix: the resolver fills `.modules` for **every**
  resolved module, and the checker walks the whole module graph with a
  DFS, one context per program object (deduped by `id(program)`).
  Verified by tests: nested misuse is caught exactly once; a
  double-imported file's error is reported exactly once.
- **SPEC §14 `store.get` wording resolved.** The spec wrote
  `store.get(table, key)` alongside `economy.balance/credit/debit`.
  Implemented (and clarified in SPEC.md §14): `store` stands for the
  declared store's name — the call is `economy.get(...)` for a store
  declared `store economy:`. There is no bare `store` qualifier (a
  fixed namespace could not address two stores with same-named tables).

**Checker rules decided where SPEC.md was silent:**
- `p.x = 5` never reaches the checker — the **parser** rejects it with
  E1001 ("assignment target must be a name"). No checker branch; pinned
  by a test asserting the parser error. (Structs are value types; field
  assignment is an error either way.)
- Top-level bare `return` is allowed (exits the program); `return
  <value>` at top level or in a handler/task body → E2007 (handlers are
  not functions).
- Option required + default → E2008 (a default makes it not required).
- Privileged intents (`members`/`message_content`/`presences`) →
  warning W1010; a *missing required* intent (member events →
  `members`, message → `message_content`) → E2008; no `intents:`
  declared means the computed set is used, no error. Live guild-count
  approval is out of scope.
- Every-path-answers → **E2010**; no-bot-block → **E2008**;
  strings/t() violations → **E2009**; extra t() args: wrong *name* →
  E2009, more args than placeholders (e.g. duplicated arg) → warning
  **W1007**.
- A non-`none` function whose body may finish without `return` → E2001
  naming the function (proved syntactically).
- `T? or T -> T`: `or` is special-cased (nullable LHS + matching RHS
  yields the inner type); bool `or` unchanged.
- Interpolation: entity types (`user`/`guild`/`channel`/`bot`/…) are
  interpolatable; `T?` in interpolation → E2001; other types → E2001
  with a `str(...)` hint.
- W1008 fires when an int-literal `/` flows into a float context; it is
  independent of the E2001 that no-coercion also raises there.
- Recovery: one error per function body (per-body flag); warnings still
  fire after an error in the same body; missing-return is suppressed if
  the body already errored.
- Reply-length expansion: entity fields from `ENTITY_FIELD_MAX`
  (`user.mention` 22, `guild.name` 100, …), scalars int 19 / float 24 /
  bool 5; `t()` templates expand from their args (max over locales);
  unknown-length `str` contributes 0 + W1009.
- **`?.` AST amendment (M1 change):** the parser built a plain
  `Member` for `?.`, so the checker could not distinguish `a.b` from
  `a?.b`. Added `Member.optional: bool = False` (`ast_nodes.py`) and
  set it in the parser's QDOT path. Checker rule: plain `.` on a `T?`
  is E2001; `?.` wraps the field type in `?`; `a?.b.c` short-circuits
  the whole chain (TypeScript behaviour, §9.2). There is no `?[]` in v1.

**Parser decisions (documented in `parser.py`):**
- Lone `|` lexes as `BAR`, not a lexer error — the brief required both
  a lone-`|` lexer error *and* or-patterns (`1 | 2 | 3`), which cannot
  both hold (the lexer cannot see pattern context). A lone `|` in an
  expression is still an error, but E1001 from the parser.
- `??` is `BinOp(op="??")`; `|>` desugars to `Call` (piped value first);
  bare `print a, b` is `Call(Name("print"), …)` in an `ExprStmt`; bare
  `log <expr>` is `Log`, `log.info(…)` an ordinary call. No `Print`
  node exists.
- Unary binding powers follow the brief literally: `-a + b` →
  `-(a + b)`, `not a or b` → `not (a or b)`, `-a ** 2` → `-(a ** 2)`,
  `not a == b` → `not (a == b)`.
- `??`/`and`/`or` mixing is detected across operand boundaries
  (`no_qq`/`no_bool` flags); comparison non-associativity is
  per-parse-call, so `a < b and c < d` is fine.
- Event `ready` + `reply:` is deliberately *not* rejected by the parser
  (the discord checker rejects it with E2008).
- **Lexer bug fixed during M1:** tokenizing any file whose first line
  is a comment or blank hung the lexer forever in `_process_indent`.
  All 5 examples start with comments, so they could never parse. Fix:
  blank/comment lines leave `at_line_start=False`. Regression test
  added (`TestLeadingComments`).
- **shop.newr fix (parent, during furniture review):** the `item:` arg
  was removed from `reply t("buy.poor", …)` — `item` is not in the
  "buy.poor" template. The file on disk is the corrected one.

### New diagnostic codes introduced in M1

| Code | Meaning | Spec |
|---|---|---|
| E2006 | non-exhaustive match (missing enum variants, or no `_` arm) | §9.6 |
| E2007 | `break`/`continue` outside a loop; `return <value>` outside a function | §9.1 |
| E2008 | Discord rule violation (catch-all: no bot block, bad/duplicate command name, fn-name collision, description/reply limits, unknown/missing intent, component/embed/store/option rules) | §13, §14 |
| E2009 | i18n violation (locale missing a key, unknown t() key, t() arg mismatch) | §15 |
| E2010 | every-path-answers violation | §13.2/§13.8 |
| W1006 | unbounded loop in a handler body (interaction token expires after 15 min) | §13.8 (G3) |
| W1007 | t() given more arguments than the template has placeholders | §15 |
| W1008 | (newr-specific) integer division in a float context | §5.1 |
| W1009 | reply length cannot be verified (unknown-length `str` part) | §13.8 |
| W1010 | privileged intent declared | §13.6 |

Starter codes already in SPEC.md §16: E1001, E3001, E3002, E2001, E2002,
E2003, E2004, E2005, W1001–W1005. All 38 codes have long-form
explanations in the CLI (`newr --explain <code>`).

---

## 6. UNVERIFIED items

(Also in SPEC.md §25 — the U1–U23 register. This section is the M1
status of each.)

- **Windows/macOS execution: UNVERIFIED.** This VM is Linux-only.
  The front end is portable-by-construction (pathlib, `encoding="utf-8"`
  on every read, no POSIX-only code, tests use `tempfile` not hardcoded
  `/tmp`), and `.github/workflows/ci.yml` defines the
  ubuntu/macos/windows matrix — but no Windows/macOS run has happened
  here. Completion condition: the CI matrix goes green. Never claim
  cross-platform support as verified from a Linux-only run.
- **Performance: UNVERIFIED.** Compile speed ("feels instant" for small
  projects), generated-code efficiency ("within a reasonable distance
  of hand-written code"), fast startup, `--help` < 100 ms cold — all
  design targets in SPEC.md §4.1, none measured. M2+ MUST benchmark
  hello + economy per target against hand-written equivalents and
  record the numbers honestly. No compile-time figures are cited
  anywhere (the Gleam 131× figure and the TS/mypy daemon figures from
  the research were deliberately NOT cited — U2/U3).
- **Live Discord behaviour of the limits: UNVERIFIED.** All Discord
  limits are static table values from `discord_limits.py`; no live
  Discord call was made to confirm them.
- **`newr fix` payloads: NOT ATTACHED.** Diagnostics carry
  `suggestion`/`help` text, but the checker attaches no machine `Fix`
  yet (only the resolver's similar-name fix). `newr fix` itself is M4.
- **PyPI name `newr-lang`: re-check at publish time.** The name was
  free per the PyPI JSON API 404 on 6 Nov 2026 (U23); names can be
  taken between now and publish.
- **npm: DROPPED from v1 (G13).** No precedent found for distributing a
  Python CLI via `npm i -g`; revisit only if a real precedent appears.
- **Research numbers/quotes (U1–U23):** the numbers in the deep-research
  document (§1–§5) were reported by research agents and their per-claim
  source lists were not persisted. No number from them is quoted in
  SPEC.md or this report unless re-verified against a primary source.
  The full U1–U23 register is in SPEC.md §25.
- **Pip console-script entry point on Windows (PowerShell/cmd):**
  UNVERIFIED — packaging is M4; the `newr` launcher works via
  `python newr …` on any OS with Python 3.10+.
- **Zipapp distribution end-to-end:** repo-tested since the fix pass
  (`tests/test_cli.py::TestZipappDiff` — built .pyz matches the
  launcher on all 5 examples + 1 err case; CI runs the same check with
  the ubuntu-built .pyz on all three OSes). Full distribution (install
  docs, release flow) is still M4.
- **SPEC.md §22:** the section numbering gap (21 → 23) is pre-existing
  and left as-is; see §3.

---

## 7. G1–G20 report (deep research §10 — what was applied)

Honest headline (corrected in the M1 fix pass — the eval found the
"All 20 applied" claim wrong): **13 applied, 4 specified-not-implemented
(spec only, no M1 code), 2 applied-with-contradiction (fixed in the fix
pass), 1 applied-but-unobservable (no emitter in M1).** ("Applied" = in
SPEC.md and, where it touches M1, in code + tests.)

| # | Status | Where |
|---|---|---|
| G1 | **Applied.** Spec rewritten as general-purpose; `store`/economy, `t()`/i18n, embeds, `task`, intents moved into the `discord` module. Core checker SKIPS discord declarations; discord checker validates them; core types expressions inside discord statements but never enforces discord rules. Type-provider injection lets the core checker type entity/store member access without importing discord data. | SPEC §1, §13–§15, §17.1; `checker.py` + `discord_check.py` |
| G2 | **Specified, NOT implemented in M1.** Integer semantics frozen in the spec (§7.5): `int` = signed 64-bit everywhere, checked overflow = runtime error, truncating `/`, dividend-sign `%`, div-by-zero = runtime error, `float` = f64. The *compile-time* half is implemented (int64 literal range, E_INT_OVERFLOW, `-9223372036854775808` representable — M-05/M-08); the *runtime* half (checked overflow at runtime) cannot be verified without an emitter. Conformance corpus must test these (M2). | SPEC §7.5 |
| G3 | **Applied.** `while` + recursion granted; handler-hang is a discord-checker WARNING (W1006), not a core ban. | SPEC §9.4, §13.8 |
| G4 | **Applied.** Closures/first-class functions in v1: lambda syntax, `fn(T..) -> R` types, capture by value. | SPEC §9.5; `ast_nodes.py`, `checker.py` |
| G5 | **Applied.** Unchecked exceptions primary (try/catch/finally + throw); `try <expr>` → `Result[T, error]`; `ok`/`err` builtins; statement-level `?` (Propagate node, parser-enforced outermost-only, nested `?` is E1001). `Result` is a value form in v1, not the primary error model. | SPEC §10; `checker.py` |
| G6 | **Applied.** File I/O + `env.args -> list[str]` (program name at index 0) in v1; HTTP deferred to v1.1 with a dated design sketch (timeouts, allowlist in newr.json, secrets never in source). | SPEC §12.2, §12.3 |
| G7 | **Applied.** No implicit numeric coercion (Gleam-style), including int literals in float positions (`let x: float = 5` is E2001). | SPEC §7.2; `checker.py` |
| G8 | **Applied.** `str` = UTF-8; `len` counts code points; no integer indexing (compile error, hint to `.chars()`); `str.chars() -> list[str]`; dict = insertion order; set = unordered. | SPEC §7.6; `checker.py` |
| G9 | **Applied (partially) / specified-not-implemented (manifest).** Resolution rule + named imports + `pub` visibility + main-file rule for bot/commands. Manifest (`newr.json`) schema is a DRAFT in the spec (required name/version, optional rest); compiler support deferred to M4 with reason (v1 resolution needs no manifest — relative paths only). | SPEC §11; `resolver.py` |
| G10 | **Applied — with a contradiction, fixed in the M1 fix pass.** R1–R11 copied verbatim into SPEC.md §7.4; diagnostics in rustc form (code, span, expected/found, `help:`, `--explain`, JSON output) with Elm tone; spans = byte offsets; similar-name help in rustc named form; recovery rule (one error per function body). The gate was 0.6 in `errors.py`'s docstring and §25 U6 while the code and §16 said 0.7 — all now say 0.7 (changed 0.6 → 0.7 during M1 with reason, see §5; the two stale wordings were fixed in the fix pass). | SPEC §7.4, §16; `errors.py`, `checker.py`, `resolver.py` |
| G11 | **Applied (spec decision) — unobservable in M1.** Generics semantic decided at M0 (§7.7): erasure on Python/TS/Java/Kotlin, reification on C#/Rust/Dart, unobservable in v1 via 3 rules (no runtime type tests on generic params, no reflection in v1, structural `==`). There is no emitter in M1, so erasure vs reification cannot be observed — the decision is recorded, not proven. User generics deferred to v2. | SPEC §7.7 |
| G12 | **Applied.** Cheap flagships in v1: `?.`/`??` (swallow rule settled: `?.` short-circuits only on `none`), pipeline `|>` (Elixir-style, frozen), Rust-style `try` block, typed enums, statement-level `?`, core `match` (statement, exhaustiveness = error), runtime-only contracts, value-type semantics (structs/enums copied, structural `==`, struct field assignment is an error), C-style ternary `cond? a: b`. Precedence table frozen (§9.2). | SPEC §9; `lexer.py`, `parser.py`, `checker.py` |
| G13 | **Applied.** npm DROPPED from v1 distribution (HIGH RISK / UNVERIFIED). v1: PyPI + pipx + zipapp + GitHub Releases + PowerShell installer. | SPEC §17.3 |
| G14 | **Applied (design) / UNVERIFIED (numbers) — specified, not implemented.** Incremental content-hash caching design (§4.1); `check` vs `--fast` modes (implemented in the CLI); `--help` < 100 ms design target; daemon escape hatch. No compile-time figures cited (U2). Benchmarks are M2+ work — no benchmark exists in M1. | SPEC §4.1; `cli.py` |
| G15 | **Applied.** Compiler/LSP/AI boundary copied (§6.5 verbatim) + the 4 things a compiler can never do. LSP is M2+, not M0/M1. AI is out of scope forever. | SPEC §5.3 |
| G16 | **Applied.** §23 renamed "What newr takes from other languages" (table stays, no "best of all" slogan claim); honest v1 story ("one readable source, seven readable outputs, with each target's libraries available"); v1 domain = small cross-platform CLIs/tools; fame timeline as planning horizon; Haxe precedent addressed head-on. | SPEC §1, §23 |
| G17 | **Applied — with a contradiction, fixed in the M1 fix pass.** New repo starts at 0.1.0 (0.5.0 belongs to the old `discord-py-skills` line and never leaks in — the report previously said 0.6.0, which was wrong; the discord-py-skills line is at 0.5.0). | `compiler/__init__.py` |
| G18 | **Applied.** `task` is the v1 concurrency primitive (discord module); no threads/shared mutability in v1; thread-safety promise in the spec (value types are safe to share); concurrency deferred to v1.1+ with a design (structured: async/await or actors — undecided). | SPEC §8.4 |
| G19 | **Applied.** Exactly one absent value, spelled `T?` (e.g. `str?`), value `none`. `option[T]` spelling REMOVED. `T?` methods `.is_some()/.is_none()/.unwrap()`; `or` unwraps with a default. | SPEC §7.2; `ast_nodes.py`, `modules.py`, `checker.py` |
| G20 | **Specified, NOT implemented in M1.** No inline target code in v1 (compiler rule); FFI/binding story spec'd as M1 design work (§20.1: `extern` declarations, per-target bindings, binding manifest verified by the compiler — v1.1 direction). v1 interim: no FFI — extend readable generated code by hand. There is no FFI code in M1 (no emitter to bind to). | SPEC §20.1 |

---

## 8. v0 compatibility (trust the code, flag contradictions)

- **v0 files keep working:** `hello.newr` (the general hello) and the
  v0 bot files `hello_bot.newr` + `showcase.newr` parse+resolve+check
  cleanly through the new front end (zero errors). The v0 bot blocks are
  **sugar over the discord module** (no migration): a file with bot
  blocks implicitly imports `discord`; `reply:`/`log:` sugar desugars
  via `CommandDecl.effective_body()` / `EventDecl.effective_body()`.
  Bot-block position is relaxed (free among top-level items, at most
  one — v0 required it first).
- **v0 rules kept verbatim:** bot name 1–32, exactly one bot, command
  description defaults to the name, empty description/reply errors,
  description ≤100, reply ≤2000 counting placeholders at max expansion
  (user/channel 22, guild 100), ephemeral default false, placeholder
  contexts, fn-name collision rejection (`foo-bar` vs `foo_bar`),
  never silently ignore a line. The one deliberate wording change:
  "did you mean" → rustc named form ("there is a variable with a
  similar name: `count`") — verdicts identical.
- **Generated Python byte-identical: NOT VERIFIABLE in M1.** There is
  no emitter yet, so no generated Python exists to compare. The v0
  goldens (`examples/hello.py`, `examples/showcase.py`) are preserved
  byte-identical in the repo as the M2 reference (SHA-256 pins verified,
  §3). M2 MUST regenerate them from the v0 examples and diff: byte-
  identical, OR changes recorded here with reasons. This is the M2
  exit criterion, not an M1 claim.
- **v0 facts established while reading the code (6 Nov 2026):** the v0
  compiler is one 2,219-line stdlib-only Python script with no
  lexer/parser/AST (two regexes into a plain dict); its test suite
  measured 96 tests (63 language + 33 package manager), 93 pass, 3
  skipped (LiveRoundTripTest needs real discord.py, not importable in
  this VM) — the repo history's "70/70" figure is stale. Whole-repo
  suite: 441 tests, 5 skipped.
- **Design-doc contradictions resolved by trusting the code:** the
  original v1 design doc framed newr as a bot language — superseded by
  the user's general-purpose correction (21:49 WIB) and the deep
  research (G1). The expansion doc's poise version is **0.7.1** (not
  0.6.x); Disnake is NEVER (its CONTRIBUTING bans ALL LLM contributions
  repo-wide, verified verbatim); DSharpPlus is NOT archived.

---

## 9. What M2 needs next (emitters — discord.py P0 first, then discord.js P0)

1. **Python emitter (P0) at byte-parity:** regenerate `hello.py` and
   `showcase.py` from `hello_bot.newr` / `showcase.newr` and diff
   against the v0 goldens in `examples/` — byte-identical, or record
   every change with a reason in this report. The emitter consumes the
   AST + `expr_type` (filled on every expression by `check_program`)
   and `Program.modules` for qualified names.
2. **`?.` signal:** `Member.optional` is the ONLY `?.` signal in the
   AST (there is no `?[]` in v1). The emitter must lower `?.` chains
   with short-circuiting (`a?.b.c` short-circuits the whole chain).
3. **Benchmarks (G14):** hello + economy per target vs hand-written
   equivalents, recorded honestly — numbers, not claims. Same for
   compile speed and startup time.
4. **Conformance corpus** (`conformance/README.md`): seeded in M1 (6 ok
   + 6 err programs, `tests/test_conformance.py`). Grow it in M2: one
   program → every target → compare stdout + exit code; bot mock
   round-trip corpus; shared escape-test corpus (quotes, braces,
   backslashes, newlines, emoji, RTL); rustc-style UI snapshot tests;
   baselines blessed only after human review, never auto-blessed.
5. **Per-target owner + CI job rule:** every shipped target needs a
   named owner + CI job, or it rots to Tier 3 (Haxe lesson). One
   target's failure never blocks another's release.
6. **Divergence table:** where targets cannot be made identical, the
   spec must say so in one place with the weaker behaviour named — a
   "(non-standard)" is a bug with a version attached, not a quirk.
   Integer semantics (§7.5) are the most dangerous drift vector;
   conformance tests must cover them first.
7. **`newr fix` payloads:** attach machine `Fix` data to diagnostics
   (the resolver already does for similar names; the checker does not
   yet). `newr fix` itself is M4.
8. **Deferred from M1 (do not re-decide silently):** full pattern
   usefulness checking (v2, §7.4); infinite-loop detection beyond
   syntactic `while true` without `break`; `newr.json` manifest
   compiler support + `newr init` (M4); `newr fmt` (M4); `newr test`
   (M6/v1.1); LSP (M2+, reusing the v1 checker as a library);
   user-defined generics (v2); `json` module (v1.1); HTTP fetch (v1.1);
   general `db`/`sqlite` module (v1.1); general `i18n` module (future);
   FFI/`extern` bindings (v1.1, §20.1); threads/structured concurrency
   (v1.1+); full TCO explicit-only (v1.1); decorators (v1.1, scoped);
   reflection (later, narrow subset); AI inside the compiler (never).
9. **Spec gaps to close in M2 if they bite:** the §22 numbering gap
   (renumber or leave, but record it); the `store.get` clarification is
   done (§5); `time.format` non-literal formats are unchecked (only
   literal strings are validated against the fixed set).
10. **CI:** the matrix exists (fixed for all 3 OSes in the fix pass —
    Linux-only case-collision step, `python` everywhere, ubuntu-built
    .pyz artifact shared across the matrix); per-target emitter jobs
    land in M2+. Windows/macOS execution stays UNVERIFIED until the
    matrix goes green on those runners.

---

## 10. How to run it (M1)

```bash
cd ~/workspace/newr
python3 -m unittest discover -s tests   # 414 tests, ~16s, pytest NOT needed
python3 newr check examples/hello.newr        # ok
python3 newr check examples/hello_bot.newr    # ok
python3 newr check examples/showcase.newr     # ok
python3 newr check examples/shop.newr         # ok, 3 warning(s)
python3 newr build --check examples/shop.newr # ok, 3 warning(s)
python3 newr --explain E2001                 # long-form explanation
python3 newr check --json examples/hello.newr # --json envelope
```

Python 3.10+ required (matches the PyPI `newr-lang` floor). Stdlib
only — no third-party dependencies in the compiler.

---

## 11. M1 fix pass — cold evaluation EVAL-M0M1.md (6 Nov 2026)

A cold evaluation by four parallel reviewers (compiler internals,
SPEC-vs-code + G1–G20, adversarial testing, tests/CI/docs) found the
M1 front end genuinely good but not ready for M2: 3 crash-class bugs,
2 type-system bugs breaking flagship features, spec/code drift, and
false README/CI claims. Baseline re-verified before the fix pass: 354
tests green. The fix pass (TDD: `tests/test_eval_fixes.py` written RED
first — 40 of 53 failed on the first run) fixed **all 14 must-fix
issues and all 10 nice-to-haves**, no regressions (414 green after).

### Must-fix M-01–M-14

| # | Fix |
|---|---|
| M-01 | Depth guard everywhere: parser (`parse_expr`/`parse_block`/`parse_type`), resolver (`_walk_expr`/`_walk_stmt`), checker (`synth`/`_walk_stmt`), discord checker (stops silently). Limit 200 (`MAX_NESTING_DEPTH` in `errors.py`). Past it: E1001 "too deeply nested", exit 4 — never a RecursionError traceback, and `--json` still emits its envelope (CLI backstop in `main()` + `_check_or_build`). |
| M-02 | Non-UTF-8 source is an environment error (exit 2) naming the file — CLI `_read_source`, resolver import reads, `_source_cache`. No more UnicodeDecodeError traceback. |
| M-03 | `T` is assignable to `T?` (checker `_check` widening, SPEC.md §7.2). The backwards "unwrap" help for the nullable→nullable case is gone. |
| M-04 | `result` is bound in `ensures` conditions (resolver child scope; the checker already bound it). Root cause of the false-green: `tests/test_checker.py::check()` ignored resolver diagnostics — it now returns resolver + checker diagnostics combined, and `tests/test_eval_fixes.py::full()` does the same (incl. parse errors). |
| M-05 | Hex/binary literals get the int64 range check (lexer): values > 2^63 are E_INT_OVERFLOW, like decimal. |
| M-06 | Spans are RAW-file byte offsets: the lexer tracks the raw byte length of every normalized character (CRLF = 2 bytes, BOM = 3). JSON `start_byte` now matches the bytes on disk. CLI reads with `newline=""` so the lexer sees the raw text. |
| M-07 | Lone CR is E_BAD_LINE_ENDING through the CLI (same `newline=""` read — the old `read_text()` universal-newline mode translated CR away before the lexer saw it). |
| M-08 | `-9223372036854775808` is representable: the lexer lets 2^63 through, the checker accepts it only when directly negated (incl. parenthesised), and rejects any other literal > INT64_MAX with E_INT_OVERFLOW. |
| M-09 | `??` rule (SPEC.md §9.2): `T? ?? T` → `T`, `T? ?? T?` → `T?`, `T? ?? none` → `T?`; mismatched RHS is E2001. |
| M-10 | `?.` on a non-nullable receiver is E2001 (checker `_member_field_type` + `_call_member`, SPEC.md §9.2). The nullable-branch recursion carries an `_in_chain` flag so chains after a real `?.` do not false-positive. |
| M-11 | `--explain` covers all 9 lexer codes + E1001's nesting note. The real lexer code set is 9 (E_BAD_ESCAPE, E_BAD_INDENT, E_BAD_LINE_ENDING, E_BAD_NUMBER, E_INT_OVERFLOW, E_STRAY_BRACE, E_TAB_INDENT, E_UNEXPECTED_CHAR, E_UNTERMINATED_STRING) — verified by grep, not guessed. |
| M-12 | CI: case-collision step is Linux-only (`if: runner.os == 'Linux'` — APFS/NTFS are case-insensitive); `python3` → `python` everywhere (setup-python's canonical command; `python3` is unproven on windows-latest); zipapp is built ONCE on ubuntu and uploaded as an artifact, the matrix job downloads that same .pyz (the old "each runner builds its own" proved nothing cross-OS); the encoding grep now covers `read_text()` + `open()` and is non-vacuous (cli.py has both). |
| M-13 | SPEC contradictions resolved: §16 now lists the full 38-code scheme (29 numbered + 9 lexer) with a pointer to `docs/error-index.md`; gate 0.6 → 0.7 in §16, §25 U6, and `errors.py` docstring; manifest §11 is a DRAFT schema (required name/version, optional rest) vs the M4 decision; §17.3 `diagnostics.py` → `errors.py`; §5.2: only the resolver attaches a `fix` in M1; §21.4 shop import moved to the top of the printed example; `examples/hello_bot.newr` header fixed; §12.2 named modules work by name without an import + `time.format` lists all 5 formats; §3.1 documents raw-byte spans; SPEC line-count claim corrected (2,129 at freeze → 2,191 after the fix pass). |
| M-14 | README is honest: lead + Install in future tense (`pip install newr-lang` is planned — PyPI 404 as of 6 Nov 2026), zipapp is a manual smoke test, "proven by CI matrix" → "will be proven", conformance is a seed (not "the single definition" yet), M1 status is COMPLETE, exit code 3 removed. |

### Nice-to-have N-01–N-10

| # | Fix |
|---|---|
| N-01/N-02 | SPEC.md §2.1 documents the by-name exemption: `"discord"` in core occurs only at the two checker bypasses (`_module_member_type`, `_call_module_member`), the resolver's `NAMED_MODULES` registry + W1002 exemption, and `RESERVED_BUILTIN_NAMES`; `ENTITY_TYPE_NAMES` is a deliberate core-owned list. `tests/test_boundaries.py` asserts the exact list (6 tests). |
| N-03 | Dead `_BINARY_BP` deleted; the live table is the frozen `PRECEDENCE` (MappingProxyType, SPEC.md §9.2). |
| N-04 | `errors.py` NewrError docstring + SPEC.md §16 corrected: recovery is checker/resolver only; the parser has NO panic-mode recovery — it stops at the first syntax error. |
| N-05 | Messages: import cycle reports only E3004 (no E3006/W1002 follow-ons); `option[int]` hint points at `T?`; case-mismatch imports name the on-disk file; `let log = 1` is E3007 like `let print = 1`; dangling `?` is diagnosed on the operand first (mentions `Result`); `print g()?` gets a `?` help. |
| N-06 | `--fast` help + stderr warning now say it skips the full type check AND all Discord validation. |
| N-07 | Op-identity tests for `+ - * / % ** == != < <= > >=` and `or`/`in`/`??`, pipeline-desugars-to-Call + ternary-node tests, combined-diagnostics helpers (`full()` in test_eval_fixes.py, `check()` in test_checker.py), `--explain` CLI-tested for all 9 lexer codes + E1001, `check --fast` CLI test, CRLF lexer + CLI tests. |
| N-08 | Launcher not Windows-executable: honest docstring, no action (out of scope for the fix pass). |
| N-09 | `modules.py` docstring marks the table APPROXIMATE (checker special-cases win); named modules work without an import (SPEC.md §12.2). |
| N-10 | `input()` takes 0–1 args (SPEC.md §12.1); `list.len()` added to METHODS (SPEC.md §12.4 already listed it); `time.format` accepts 5 formats (SPEC.md §12.2 now lists all 5). |

### Decisions recorded in the fix pass

- **Depth limit 200, code E1001** — far above anything a human writes,
  far below the interpreter's recursion limit; one code for all four
  stages so the CLI backstop and the guards agree.
- **`??` rule** — see M-09 above; the nullable-RHS case returns `T?`
  (not `T`), because the result can still be `none`.
- **`?.` on non-nullable is an error, not a silent pass** — a
  non-nullable value is never `none`, so the chain is dead code and
  almost certainly a typo for `.` (SPEC.md §9.2).
- **Lexer codes keep their `E_*` names** — documented as the lexer-code
  family in SPEC.md §16, not renumbered into the `E1xxx` scheme.
- **Named modules work without an import** — `math.sqrt(2.0)` checks
  with no `import` line; `import math` is still allowed and changes
  nothing. `discord` stays bypassed (UNKNOWN) in core.
- **G1–G20 headline corrected** — the honest count is 13 applied, 4
  specified-not-implemented (G2 int64 runtime semantics, G9 manifest,
  G14 benchmarks, G20 FFI — spec only, no M1 code), 2
  applied-with-contradiction (G10 gate 0.6/0.7, G17 version 0.5.0 vs
  0.6.0 — both fixed in this pass), 1 applied-but-unobservable (G11
  generics erasure/reification — no emitter in M1 to observe it on).
  See §7 for the corrected rows.
- **M2 entry criteria (from the eval) now met:** (1) M-01–M-05 fixed +
  regression tests; (2) M-06–M-10 fixed; (3) M-11+M-13: all 38 codes in
  SPEC.md §16 with `--explain` coverage, contradictions resolved;
  (4) M-12: CI fixed for all 3 OSes (still never run — the matrix going
  green is the remaining proof); (5) M-14: README future tense;
  (6) N-07: op-identity tests + combined-diagnostics helpers;
  (7) conformance corpus seeded.

---

## 12. Fix round 2 — stress campaign STRESS-REPORT.md (7 Nov 2026)

The stress/endurance campaign (7,000+ `newr check` runs, 57,132 test
executions) found 5 findings: 1 crash, 1 medium bug, 2 minors, 1 perf
cliff. This round fixed **all 5**, TDD (`tests/test_stress_fixes.py`
written RED first — 33 of 38 failed on the first run, plus the
depth-5000 test failing on time), no regressions: **453 tests green**
(baseline 414 + 39 new). Nothing under `stress/` was modified; the
crash inputs are the oracle and still pass through the CLI cleanly.

### F-01 — CRASH-1: file ending in an identifier, no trailing newline (HIGH)

Root cause: `_peek()` returns `""` at EOF, and `""` is not a
character — but two code paths treated it as one. `_is_name_start` /
`_is_name_continue` passed it to `unicodedata.category`, which raises
`TypeError` (raw traceback, exit 1). The same root cause also **hung**
the lexer, found during verification (not in the report): `"" in
"<str>"` is `True` in Python, so four loops never terminated at EOF —
the hex/binary digit loops (`print 0xFF` / `print 0b10` with no
newline), the whitespace loops in `_handle_newline` (trailing spaces
after the last newline) and `_process_indent` (indented blank line at
EOF). Five hang repros, all confirmed live before the fix.

Fix (`compiler/lexer.py`): the name predicates reject any
non-single-character input (`len(ch) != 1`); the four loops got a
`pos < len(source)` guard; the exponent check got the same guard as
hardening (it was safe by luck — the inner digit test fails at EOF —
but `"" in "eE"` is `True`, so the trap was live). A file ending in an
identifier now lexes normally: `printf 'print x'` gives a clean
`error[E3001]: undefined name 'x'`, exit 4. The regex fast path in
`_lex_name` (see F-05) covers ASCII names at EOF on its own; the
guard is what saves Unicode names — proven by
`stress/crashes/fuzz-20261106-1871.newr`, which ends in a stray `é`
and crashed in `_lex_name`'s per-char loop without the guard.

### F-02 — BUG-2: top-level use-before-def silently accepted (MEDIUM)

`print later` before `let later = 1` exited 0, contradicting SPEC.md
SS8 ("top-level statements execute in file order"). Root cause:
`_collect_top_level` pre-declares every top-level `let`/`const` before
the statement walk, so `scope.lookup` found the binding and marked it
used; the `_later_stack` E2005 machinery only fires when lookup
*misses*, which never happened at top level.

Fix (`compiler/resolver.py`): `_resolve_module` sets
`_top_level_pending` (name → declaration statement) around the
top-level statement walk; `_walk_let_const` removes a name from it
once its declaration statement has been walked; `_resolve_name` and
`_resolve_assign_target` report E2005 when the found binding is a
still-pending top-level let/const. The rule applies to statements
that execute in file order only: a use inside a fn/lambda/handler body
is deferred execution and is exempt (`_in_deferred_scope` walks the
scope chain), and fn/struct/enum/store names stay hoisted. The rule
applies uniformly to imported modules too — a `let` RHS executes when
the module loads, in file order, on every target, so a forward
reference between module lets would be a runtime error waiting to
happen. The diagnostic shape matches the existing local E2005:
`print later` / `let later = 1` gives `[E2005, W1001]`, exactly like
the fn-local case with no later use. The resolver docstring and the
`_collect_top_level` comment were updated to state the corrected rule
(the old text claimed a blanket forward-reference allowance).

### F-03 — `1__2` / `1_` lex as valid ints (minor)

Decision: **reject** (strict reading of SPEC.md SS6: "Underscores may
group digits" — an underscore must sit between two digits, so a
leading, trailing, or doubled underscore groups nothing). New
`_check_underscores` in the lexer validates every digit run (decimal,
float, hex, binary): each `_` must have a digit on both sides of the
same run. `1__2`, `1_`, `0x_FF`, `0xFF_`, `0b1__0`, `1_.5`, `1e3_` are
E_BAD_NUMBER; `1_000`, `1_000_000`, `0xF_F`, `0b1_0`, `1_000.5`,
`1e1_0` stay valid. `--explain E_BAD_NUMBER` and
`docs/error-index.md` now mention the rule. No SPEC.md wording change
was needed — the decision follows from the existing sentence.
Deliberately not tested: `1._5` is **not** an underscore case — the
lexer produces INT(1) DOT IDENT(_5), a member access on an int
literal, which the checker rejects with E3001. The underscore rule
only applies inside a number token.

### F-04 — raw NUL byte accepted inside string literals (minor)

Fix (`compiler/lexer.py`): `_lex_string_char` raises E_UNEXPECTED_CHAR
("NUL byte is not allowed in a string literal", help: "write `\u{0}`
for a NUL character") for a raw `\x00` inside a string — the same code
a raw NUL outside a string already gets, and the same treatment as a
raw newline inside a string. The escape route still works:
`"a\u{0}b"` lexes fine. Reasoning, no SPEC.md change: SPEC.md SS6
enumerates the string escapes (`\"`, `\\`, `\n`, `\t`, `\r`,
`\u{XXXX}`); a raw NUL is a control character with no literal form,
so it is rejected like a raw newline. Deliberately left as-is: other
raw control characters (e.g. a literal tab) inside strings are still
allowed — the spec is silent on them and the finding was NUL-only.

### F-05 — nested `if` / blocks go quadratic (MEDIUM)

Correction to the report's hypothesis: the cost is in the **lexer,
not the parser/checker walk**. cProfile on a depth-1000 nested-`if`
file (1.01 MB, 1.77 s): `_Lexer.__init__` per-char CRLF-normalization
loop with a per-char `encode("utf-8")`, `_process_indent` advancing
one space at a time (500k spaces), `_handle_newline` per-char loops.
The file itself is O(depth²) bytes (16.5 MB at depth 4059), so lexing
is linear in input size; the fix is lexer bulk/fast paths, not a walk
bail.

Fix (`compiler/lexer.py`): (a) `__init__` fast path for ASCII sources
with no `\r`: no normalization loop, and `_byte_prefix` is a `range`
(O(1) memory) instead of a per-character list — a 25 MB file would
otherwise build a 25M-entry prefix list; non-ASCII / CRLF sources
keep the exact old slow path. (b) `_advance` newline-free fast path
(`col += len(out)`, no per-char loop). (c) `_process_indent` and
`_handle_newline` scan whitespace runs with a compiled regex
(C-speed); a tab still raises E_TAB_INDENT at the tab's own position.
(d) `_lex_name` matches ASCII name runs with a compiled regex, used
only when the following character is not a name-continue character —
Unicode identifiers fall back to the per-char loop (verified live:
`café`, Thai identifiers still lex as single IDENTs). (e) comment
skips use `str.find` instead of per-char advance. Every fast path is
provably equivalent to the loop it replaces or falls back to it.

Timings (same machine, full `newr check` pipeline unless noted):

| Input | Before | After |
|---|---|---|
| depth-3000 nested `if` (`full()`) | ~14 s | **0.15 s** |
| depth-5000 nested `if` (`full()`) | 40.9 s (measured RED) | **0.32 s** |
| `stress/crashes/depth-if-4059.newr` (CLI) | ~30 s (report) | **0.58 s** |
| `stress/crashes/depth-blocks-4374.newr` (CLI) | ~30 s (report) | **0.80 s** |

All four still end in the clean `error[E1001]: too deeply nested`,
exit 4 — never a traceback or a hang. Depth 5000's 25 MB file is the
honest cost of the input size, not of the nesting.

### Red-check results (each fix temporarily reverted, then restored)

| Fix | Reverted | New tests failing |
|---|---|---|
| F-01 guards + loop guards | name predicates + 4 loop guards | 6/12 (5 EOF hangs by timeout + `fuzz-20261106-1871.newr` TypeError); the ASCII crash tests pass on revert because the `_lex_name` regex fast path covers ASCII names at EOF — the guard is pinned instead by the new non-ASCII test, which fails (TypeError) on guard revert and passes on the fix |
| F-02 pending lets | `_is_pending_top_level` forced False | 6/11 (all E2005 assertions); the 5 allowed-behaviour tests still pass |
| F-03 underscores | `_check_underscores` no-op + old EXPLAIN text | 8/9 (all E_BAD_NUMBER assertions + `--explain`); grouped-underscore positives still pass |
| F-04 NUL | NUL check removed | 2/4 (in-string NUL + corpus CLI); escape + outside-string tests still pass |
| F-05 fast paths | all six fast paths disabled | 2/2 (both timing assertions; 53 s total) |

Every revert was restored from a backup and verified by md5 + a
green re-run of the affected tests before the next step. Full suite
after all restores: **453 tests, OK**.

### Notes from verification

- `stress/crashes/corpus-b_truncated_fn.newr` (`fn f(a: int) -> int:`
  + bare `return`, no newline) does **not** produce E1001: a bare
  `return` is valid syntax (it returns `none`), so the clean
  diagnostics are the checker's E2001 (the fn declares `-> int`) plus
  W1001 for the unused parameter `a`. The tests assert that exact
  pair; the `--json` envelope test asserts E2001 for the same reason.
- The `test_underscore_after_dot_is_E_BAD_NUMBER` case was removed
  during RED: `1._5` is member access, not a malformed number (see
  F-03). The test file records the reasoning in a comment.
- Pre-existing, out of scope, not touched: the E_BAD_NUMBER
  `--explain` text cites `0o17` as an example, but the lexer does not
  support `0o` literals (only `0x`/`0b`) — a doc inaccuracy from M1,
  left for a later pass.
- SPEC.md was **not** changed: no finding forced a spec decision.
  F-02 implements the existing SS8 sentence; F-03 follows from the
  existing SS6 sentence; F-04 follows from the SS6 escape list.

## 13. Fix round 3 — re-stress STRESS-REPORT-2.md (7 Nov 2026)

One new pre-existing crash from the fresh fuzz seed 20261107; fix
round 2's work was fully verified by the re-stress (every round-1
finding gone, corpus 0 mismatches, fuzz seed 20261106 3,000/3,000
clean, depth-if 5000 in 0.49 s, endurance 60/60 green) — the ship
gate stayed closed only because of this one bug.

### CRASH-2: malformed `t(...)` raises raw AttributeError (HIGH)

`print t("k", 1)` and `print t(1)` crash the parser with
`AttributeError: 'Span' object has no attribute 'span'` (exit 1)
instead of the intended clean E1001 (exit 4). Root cause:
`_parse_call` in `compiler/parser.py` (lines 1838, 1842) passed a
`Span` (`func.span` / `args[1].span`) to `self.err()`, which expects
a `Token` and reads `tok.span`. Pre-existing — parser.py was
byte-identical to round 1; round 1's fuzzer never emitted `t(...)`
calls. Reached only when a `t()` call is malformed: a second
positional argument (e.g. `t("buy.ok", item)` — forgetting `item:`),
a non-string first argument, or no arguments at all. The full fuzz
source is `stress/crashes/fuzz-20261107-1429.newr` (a mutated
`shop.newr` where deleting the `:` after `item` turned a named
argument into a second positional argument).

### The fix (compiler/parser.py only)

- New helper `_err_span(span, message, help=None)` next to `err()`:
  builds the `Diagnostic` directly from an already-computed span
  (same code E1001, same message shape). The two call sites now use
  `_err_span(func.span, ...)` and `_err_span(args[1].span, ...)`
  with the diagnostic wording unchanged.
- Grep-verified before and after: those two were the ONLY call
  sites in `compiler/` passing a `.span` to `err()` / `_err_code()`
  (all other `self.err(...)` calls pass tokens). No third site
  existed, so exactly two lines changed plus the helper.

### Tests (new file tests/test_crash2_fix.py, 13 tests, TDD RED first)

- `print t("k", 1)` → exactly `["E1001"]`, message names "named
  arguments", span pinned to the second argument (bytes 13–14).
- `print t(1)` → exactly `["E1001"]`, message names "string key",
  span pinned to the `t` name (bytes 6–7).
- `print t()` (no key at all) → `["E1001"]` via the same path.
- CLI on all three saved repros (both minimized files + fuzz-1429):
  exit 4, `error[E1001]`, no `Traceback`, no `AttributeError`.
- Control cases unchanged: `print t("k", item: 1)` is clean in the
  core pipeline and exits 4 with `error[E2009]` (unknown strings
  key) end to end; a valid `t()` call against a defined `strings`
  block still checks clean.

### Verification

| Check | Result |
|---|---|
| New tests (RED, before fix) | 10/13 failing (3 CLI failures + 7 in-process AttributeError); the 3 control tests already passed |
| New tests (after fix) | 13/13 OK |
| Full suite | **466 tests (453 + 13), OK in 12.7 s** |
| Examples | all ok; `shop.newr` keeps exactly its 3 baseline warnings (W1010×1 + W1009×2) |
| Repros (direct `./newr check`) | `min-crash-t-call-2pos.newr` → clean E1001, exit 4; `min-crash-t-call-badkey.newr` → clean E1001, exit 4; `fuzz-20261107-1429.newr` → clean E1001 at 37:24, exit 4; control → clean E2009, exit 4 |
| Scope | `stress/`, `SPEC.md`, and all pre-existing test files untouched (mtime-checked); only `compiler/parser.py` changed + one new test file |

### Red-check (fix reverted, then restored)

Backup of the fixed `compiler/parser.py` taken first (sha256
`768680cce6b8f9927c8c2c91f6d86572dce1ba2f2a0ac0a0de4379d584707b3a`).
Reverted only the two call sites to `self.err(...)`: new tests
**10/13 failing** — the identical RED shape (3 CLI failures + 7
in-process errors). Restored from the backup, sha256 verified
identical, full suite re-run green (**466 OK**).

### Notes from verification

- One test expectation was corrected with evidence, not weakened:
  the control case `print t("k", item: 1)` does NOT produce E2009
  in the in-process `full()` helper — E2009 comes from the Discord
  checker (`compiler/discord_check.py`), which `full()` does not run
  (resolver + core checker only). The in-process test pins the core
  pipeline as clean (`[]`); the CLI test pins the E2009 end to end.
  The test file records this in a comment.
- Probe artifact, not a bug: `python3 -m compiler.cli check <file>`
  exits 0 silently — `compiler/cli.py` has no
  `if __name__ == "__main__":` block, so the module import just
  exits. The supported entry points are the `./newr` launcher and
  the packaged console script (`compiler.cli:main`); all
  verification in this round used `./newr` (the same invocation as
  `stress/run_stress.py`). Out of scope, not changed.
- SPEC.md was **not** changed: the fix restores the intended E1001
  error path; no spec decision was involved.

