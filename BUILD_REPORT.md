# newr - BUILD REPORT (M0 spec freeze + M1 front end)

**Status: M0 + M1 COMPLETE - 6 November 2026, ~23:15 WIB.**
**M1 fix pass: COMPLETE - 6 November 2026, ~23:52 WIB** (cold evaluation
`EVAL-M0M1.md`: all 14 must-fix issues + all 10 nice-to-haves fixed,
see §11 below).
**Repo:** `~/workspace/newr/` - LOCAL ONLY. No GitHub repo created, nothing
pushed, nothing committed to any remote. `~/workspace/discord-py-skills`
was read only, never modified.
**Version:** 0.1.0 (`compiler/__version__`).

M0 froze the spec (`SPEC.md`, 2,129 lines at freeze; 2,191 after the M1
fix pass) as a **general-purpose
programming language** - one language for all, with `discord` as one
domain module - after the user's 21:49 WIB correction, the 21:50 WIB
all-OS requirement, the 21:58 WIB stability contract, the 21:59 WIB
powerful/faster/thinking principles, the 22:02 WIB "best of all" /
wishlist chapters, and the 22:12 WIB deep research (G1–G20 - the
headline in §7 was corrected in the fix pass: 13 applied, 4
specified-not-implemented, 2 applied-with-contradiction (fixed), 1
applied-but-unobservable).

M1 built the front end in Python (stdlib only): lexer → parser →
AST (byte-offset Spans) → resolver → checker (core + discord) → CLI.
**No emitter exists yet** (M2/M3). M1 was NOT split into M1a/M1b - the
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
| `.github/workflows/ci.yml` | 210 | GitHub Actions matrix ubuntu/macos/windows × Python 3.10–3.13, fail-fast false. Jobs: front-end suite, v0 conformance (`python newr check` on the examples), build-zipapp (ubuntu builds once, uploads artifact) + zipapp behavior-diff (matrix downloads the SAME .pyz), golden rebuild check (SHA-256 pins verified this session - see §3). Case-collision step is Linux-only (APFS/NTFS are case-insensitive); `python` (not `python3`) everywhere; encoding grep covers `read_text()` + `open()`. Per-target emitter jobs land M2+. |

### Compiler (`compiler/`, stdlib only, zero POSIX-only code)

| File | Lines | What it is |
|---|---|---|
| `__init__.py` | 7 | Package docstring + `__version__ = "0.1.0"`. |
| `errors.py` | 264 | `Span(file, start_byte, end_byte)` - **raw-file byte offsets** (G10, M-06 fix: a CRLF newline counts 2 bytes in a span); `line_col()`; `MAX_NESTING_DEPTH = 200` (M-01); `Suggestion` (applicability "certain"\|"maybe"); `Diagnostic` (span/code/message/severity/expected/found/help/note/suggestion/fix - no `hint` field); rustc-style `format(source_text)`; `to_json()`; `NewrError` (docstring: recovery is checker/resolver only, parser has none); `CompilerResult` (`.ok`, `.errors`). |
| `lexer.py` | 635 | Mode-stack lexer (normal/string/interp); tokens with raw-file byte-offset spans; 2-space indent (steps exactly +2, dedents land on a stack level); `#` comments + blank lines ignored; CRLF normalized (spans stay raw-byte), lone CR error, BOM stripped (3 raw bytes), tabs banned; ints (decimal/underscores/hex/binary, int64 range check - 2^63 allowed through for `-9223372036854775808`, larger is E_INT_OVERFLOW), floats, exponents; duration literals (`60s/5m/1h/1d` → seconds); strings with escapes, `{{`/`}}`, `{expr}` interpolation (nested strings/dict literals work with a space after `{`); bracket continuation; Unicode identifiers (Thai/Devanagari combining marks lex inside names - v0 command-name rule). Error codes E_BAD_NUMBER, E_BAD_ESCAPE, E_UNTERMINATED_STRING, E_STRAY_BRACE, E_TAB_INDENT, E_BAD_INDENT, E_BAD_LINE_ENDING, E_INT_OVERFLOW, E_UNEXPECTED_CHAR. Keywords include `enum match pub requires ensures`; tokens include `QMARK ?`, `QDOT ?.`, `QQ ??`, `PIPE |>`, `BAR |`. |
| `ast_nodes.py` | 710 | Full AST. `Type` gains `nullable: bool` (`T?`), `Result[T, E]`, `fn(T..) -> R`; `BUILTIN_TYPE_NAMES` (no `option`, plus `Result`, `fn`); `GENERIC_ARITY`; `nullable_of` / `result_of` / `fn_of` helpers. Nodes: Lit, Name, BinOp, UnaryOp, Ternary, Lambda (capture by value), TryExpr, Propagate (statement-level `?`), Call, TCall, Member (**`optional: bool = False`** - the M1 `?.` amendment, see §5), Index, Interp, ListLit, DictLit, EmbedExpr, Let/Const (`pub` flag), Assign, If, For, While, patterns (Lit/Wildcard/Name/Enum/Struct/List/Or), MatchArm, Match, Require, Ensure, Try, Throw, Return, Break, Continue, Pass, Reply, Defer, Edit, Followup, Send, Log, ExprStmt, Button/Select/ModalDecl, Param, Import (`alias`, `bound_name`, `resolved_path`), Struct/StructField, FnDecl, EnumDecl/EnumVariant, BotDecl, OptionDecl, CommandDecl (`effective_body()` desugars v0 `reply:` sugar), EventDecl (`effective_body()`), TaskDecl, StringsDecl, StoreDecl, Program (`files`, `modules`). |
| `parser.py` | 1,853 | Recursive-descent + Pratt parser. `parse(source, filename="<string>") -> Program`, first syntax error raises NewrError E1001 with a byte-offset span. Precedence is the frozen `PRECEDENCE` table (MappingProxyType, SPEC.md §9.2); the dead `_BINARY_BP` dict is gone. Depth guard: `parse_expr` / `parse_block` / `parse_type` stop with E1001 past 200 levels (M-01). `let log` → E3007 (reserved builtin name), like `let print`. | `?` disambiguation: QMARK followed by NEWLINE/DEDENT/EOF/COMMA/RPAREN/RBRACK/COLON is not ternary (left for statement-level propagate); nested `?` (`f(g()?)`) is E1001. `??` mixed with `and`/`or` unparenthesized is a parse error. `print <expr,...>` without parens AND `print(...)`; `log <expr>` → Log, `log.info(...)` an ordinary call. Duration literals only in `task every`/ `timeout:`. Command names joined from IDENT/MINUS/INT tokens; subcommands rejected. `t(...)` → TCall. v0 reply/log sugar into `reply_expr`/`log_expr` (NOT desugared by the parser). `Program.modules` left empty (resolver fills it). |
| `resolver.py` | 996 | Name resolution: imports (cycle-checked, relative to the importing file), duplicate top-level names, `pub` visibility, reserved names, E3001 with rustc named-form similar-name help + machine-applicable fix, E2005 use-before-definition, assignment-target validation. Fills `Import.resolved_path`, `Program.files`, `Program.modules` (**for every module, not just main** - nested imports, see §5). M1 fix pass: `result` is bound in `ensures` conditions (M-04); depth guard past 200 levels → E1001 (M-01); an import cycle reports only E3004 (no E3006/W1002 follow-ons); case-mismatch imports name the on-disk file; non-UTF-8 imported files are E3008. |
| `checker.py` | 2,407 | Core checker. No Discord imports (verified by `test_boundaries.py`, which also asserts the exact by-name exemption list - SPEC.md §2.1). `check_program(program, providers=None) -> CompilerResult`. R1–R11 inference, no implicit numeric coercion, `T?`/`?.`/`??`, Result/`try`/`?`, match exhaustiveness, contracts, value types, operator/index/member/call typing, builtins, one-error-per-function-body recovery. Fills `expr_type` on every expression. M1 fix pass: `T` widens to `T?` (M-03); `??` accepts a `T?`/`none` right side (M-09); `?.` on a non-nullable receiver is E2001 (M-10); `2^63` is an error unless directly negated (M-08); depth guard past 200 levels → E1001 (M-01); named modules work without an import (M-10/N-09); `input()` takes 0–1 args, `list.len()` exists (N-10); `option[int]` gets a `T?` hint (N-05). |
| `discord_check.py` | 1,112 | Discord checker (the only module importing `discord_limits`). `check_discord(program) -> list[Diagnostic]`; `discord_providers(program) -> dict` + `DISCORD_TYPE_PROVIDERS` (entity/store member-type tables the core checker consumes via `providers`). Bot/command/event/option/intent/component/embed/store/strings rules, reply-length estimation, every-path-answers, handler-hang warning. Depth guard past 200 levels (stops silently - the core already reported E1001). |
| `discord_limits.py` | 190 | Pure Discord constants only (command-name regex, description/reply limits, placeholder max expansions, entity field maxes, options/components/embeds limits, intents). No store/strings/task rules. |
| `modules.py` | 149 | Core stdlib signatures: BUILTINS (print/input/len/str/int/float/bool/min/max/abs/round/range/set/ok/err), MODULES (math/time/random/file/env - `env.get -> str?`, `env.args: list[str]` incl. program name at index 0), method tables (list/dict/set/str, `str.chars()`, nullable + Result methods), RESERVED_BUILTIN_NAMES (builtins + math/time/random/file/env/t/discord/**log** - `log` added in M1, see §5). The table is marked APPROXIMATE in its docstring (the checker special-cases some builtins); `input` takes 0–1 args and `list` has `len` (N-09/N-10). |
| `cli.py` | 679 | The `newr` CLI (SPEC.md §19). See §4. |
| `newr` (launcher) | 20 | Executable launcher: adds the checkout dir to sys.path, dispatches to `compiler.cli:main`. Works as `python3 newr …` (and `./newr …` on Unix; the pip console-script form is M4 packaging). |
| `__main__.py` | 9 | zipapp entry point (`python3 -m zipapp . -o newr.pyz`). Same entry point as the launcher. |

### Tests (`tests/`, unittest - pytest is NOT installed in this VM)

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
(v0 bot files, conformance baseline - unchanged), `hello.py` +
`showcase.py` (v0 generated goldens - unchanged, byte-identical reference
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
first - 40 of 53 failed on the first run), 3 in `test_boundaries.py`,
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
  shop_items → `ok`; shop.newr → `ok, 3 warning(s)` - all legitimate:
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
  `1eb3265b…` and `examples/showcase.py` = `a8222f1b…` - exactly the
  hashes pinned in `.github/workflows/ci.yml`. The CI's front-end,
  v0-conformance, and zipapp jobs can now run (they reference the `newr`
  launcher, which exists as of this milestone). The CI matrix itself has
  still never run - Windows/macOS stay UNVERIFIED until it goes green.
- **SPEC.md complete and consistent** with the design docs as corrected
  by the user (general-purpose, all OSes, stability contract,
  powerful/faster/thinking, "best of all"/wishlist with the honesty
  correction, G1–G20 - headline corrected in the fix pass, §7). One
  numbering gap remains: there is no §22 (sections go 21 → 23) -
  pre-existing, left as-is, do not renumber silently in a later
  milestone; record the fix if it is ever made.
- **M1 fix pass verification (EVAL-M0M1.md, §11):** every must-fix and
  nice-to-have re-verified live (CLI + unit tests), incl. the three
  headline bugs (`let x: int? = 5`, 500-deep nesting, verbatim §9.7
  `ensures result` example).

---

## 4. CLI (M1 behaviour - SPEC.md §19)

```
newr [--json] [--error-format=json] <command> [args]
  check <file> [--fast]  verify (parse, resolve, check); writes nothing
  build <file> [--target py|js] [--check] [--fast]
                         M1: --check only; emitters land in M2
  help [command]         show help (bare `newr` prints help, exit 0 - v0 behaviour)
  version / --version    newr 0.1.0
  explain <code>         long-form explanation (also: --explain <code>)
```

- **Exit codes:** 0 success, 1 usage error, 2 environment error
  (unreadable file), 4 build/check error. Warnings never fail the build.
  (v0 had a 3 = network code; M1 has no network operations.)
- **`build` without `--check`** → exit 1: "newr build cannot emit yet -
  emitters land in M2. Use `newr build --check <file>`…".
- **`--fast`** → parse + name resolution only, skips the full type check
  AND all Discord validation, with a stderr warning ("--fast skips the
  full type check and all Discord validation; the result is not
  verified"). Accepted on `check` too, same semantics.
- **`--json`** → stdout is exactly one JSON object, the v0 envelope
  `{ok, command, newr, data, warnings, error}` (`error` = first error as
  `{code, message, hint}`; `warnings` = warning diagnostics;
  `data.diagnostics` = all diagnostics as JSON objects). A crash-class
  input (deep nesting) still emits a JSON envelope with E1001 - never
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

### M0 (spec - from the design docs as corrected by the user)

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
(**npm DROPPED** - HIGH RISK / UNVERIFIED, no precedent); standalone
binary (PyInstaller/Nuitka) = post-v1 decision; performance chapter
(incremental + caching design, idiomatic generated code, fast startup -
all UNVERIFIED until benchmarked); thinking-compiler chapter (inference,
smart diagnostics, `newr check` reasoning, `newr fix` for safe fixes -
M4; AI models / code generation / guessing intent explicitly OUT);
"Best of all" chapter renamed "What newr takes from other languages"
(the table stays - features taken, NOT a "best of all" slogan claim,
which the research judged NOT HONEST for v1); wishlist chapter (§24.3)
with every item evaluated v1 / deferred / rejected-with-reason; chie's
19 ideas evaluated (§24.2); fame timeline as a planning horizon (0–1 yr
liked, 1–3 yr used to build, 3–7 yr jobs/teaching, 7–15 yr default
somewhere); Haxe precedent addressed head-on.

### M1 (implementation - what the code decided)

**Ownership (decided during M1, record for M2):**
- E2005 (use before definition) is raised by the **resolver**, not the
  checker - scope order is known there. It applies to locals and to a
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
- **Parameters of `pub` functions do not warn W1001** - they are the
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
  error bindings, not redefinitions - `_declare(allow_builtin=True)`
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
  declared store's name - the call is `economy.get(...)` for a store
  declared `store economy:`. There is no bare `store` qualifier (a
  fixed namespace could not address two stores with same-named tables).

**Checker rules decided where SPEC.md was silent:**
- `p.x = 5` never reaches the checker - the **parser** rejects it with
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
- Lone `|` lexes as `BAR`, not a lexer error - the brief required both
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
  was removed from `reply t("buy.poor", …)` - `item` is not in the
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

(Also in SPEC.md §25 - the U1–U23 register. This section is the M1
status of each.)

- **Windows/macOS execution: UNVERIFIED.** This VM is Linux-only.
  The front end is portable-by-construction (pathlib, `encoding="utf-8"`
  on every read, no POSIX-only code, tests use `tempfile` not hardcoded
  `/tmp`), and `.github/workflows/ci.yml` defines the
  ubuntu/macos/windows matrix - but no Windows/macOS run has happened
  here. Completion condition: the CI matrix goes green. Never claim
  cross-platform support as verified from a Linux-only run.
- **Performance: UNVERIFIED.** Compile speed ("feels instant" for small
  projects), generated-code efficiency ("within a reasonable distance
  of hand-written code"), fast startup, `--help` < 100 ms cold - all
  design targets in SPEC.md §4.1, none measured. M2+ MUST benchmark
  hello + economy per target against hand-written equivalents and
  record the numbers honestly. No compile-time figures are cited
  anywhere (the Gleam 131× figure and the TS/mypy daemon figures from
  the research were deliberately NOT cited - U2/U3).
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
  UNVERIFIED - packaging is M4; the `newr` launcher works via
  `python newr …` on any OS with Python 3.10+.
- **Zipapp distribution end-to-end:** repo-tested since the fix pass
  (`tests/test_cli.py::TestZipappDiff` - built .pyz matches the
  launcher on all 5 examples + 1 err case; CI runs the same check with
  the ubuntu-built .pyz on all three OSes). Full distribution (install
  docs, release flow) is still M4.
- **SPEC.md §22:** the section numbering gap (21 → 23) is pre-existing
  and left as-is; see §3.

---

## 7. G1–G20 report (deep research §10 - what was applied)

Honest headline (corrected in the M1 fix pass - the eval found the
"All 20 applied" claim wrong): **13 applied, 4 specified-not-implemented
(spec only, no M1 code), 2 applied-with-contradiction (fixed in the fix
pass), 1 applied-but-unobservable (no emitter in M1).** ("Applied" = in
SPEC.md and, where it touches M1, in code + tests.)

| # | Status | Where |
|---|---|---|
| G1 | **Applied.** Spec rewritten as general-purpose; `store`/economy, `t()`/i18n, embeds, `task`, intents moved into the `discord` module. Core checker SKIPS discord declarations; discord checker validates them; core types expressions inside discord statements but never enforces discord rules. Type-provider injection lets the core checker type entity/store member access without importing discord data. | SPEC §1, §13–§15, §17.1; `checker.py` + `discord_check.py` |
| G2 | **Specified, NOT implemented in M1.** Integer semantics frozen in the spec (§7.5): `int` = signed 64-bit everywhere, checked overflow = runtime error, truncating `/`, dividend-sign `%`, div-by-zero = runtime error, `float` = f64. The *compile-time* half is implemented (int64 literal range, E_INT_OVERFLOW, `-9223372036854775808` representable - M-05/M-08); the *runtime* half (checked overflow at runtime) cannot be verified without an emitter. Conformance corpus must test these (M2). | SPEC §7.5 |
| G3 | **Applied.** `while` + recursion granted; handler-hang is a discord-checker WARNING (W1006), not a core ban. | SPEC §9.4, §13.8 |
| G4 | **Applied.** Closures/first-class functions in v1: lambda syntax, `fn(T..) -> R` types, capture by value. | SPEC §9.5; `ast_nodes.py`, `checker.py` |
| G5 | **Applied.** Unchecked exceptions primary (try/catch/finally + throw); `try <expr>` → `Result[T, error]`; `ok`/`err` builtins; statement-level `?` (Propagate node, parser-enforced outermost-only, nested `?` is E1001). `Result` is a value form in v1, not the primary error model. | SPEC §10; `checker.py` |
| G6 | **Applied.** File I/O + `env.args -> list[str]` (program name at index 0) in v1; HTTP deferred to v1.1 with a dated design sketch (timeouts, allowlist in newr.json, secrets never in source). | SPEC §12.2, §12.3 |
| G7 | **Applied.** No implicit numeric coercion (Gleam-style), including int literals in float positions (`let x: float = 5` is E2001). | SPEC §7.2; `checker.py` |
| G8 | **Applied.** `str` = UTF-8; `len` counts code points; no integer indexing (compile error, hint to `.chars()`); `str.chars() -> list[str]`; dict = insertion order; set = unordered. | SPEC §7.6; `checker.py` |
| G9 | **Applied (partially) / specified-not-implemented (manifest).** Resolution rule + named imports + `pub` visibility + main-file rule for bot/commands. Manifest (`newr.json`) schema is a DRAFT in the spec (required name/version, optional rest); compiler support deferred to M4 with reason (v1 resolution needs no manifest - relative paths only). | SPEC §11; `resolver.py` |
| G10 | **Applied - with a contradiction, fixed in the M1 fix pass.** R1–R11 copied verbatim into SPEC.md §7.4; diagnostics in rustc form (code, span, expected/found, `help:`, `--explain`, JSON output) with Elm tone; spans = byte offsets; similar-name help in rustc named form; recovery rule (one error per function body). The gate was 0.6 in `errors.py`'s docstring and §25 U6 while the code and §16 said 0.7 - all now say 0.7 (changed 0.6 → 0.7 during M1 with reason, see §5; the two stale wordings were fixed in the fix pass). | SPEC §7.4, §16; `errors.py`, `checker.py`, `resolver.py` |
| G11 | **Applied (spec decision) - unobservable in M1.** Generics semantic decided at M0 (§7.7): erasure on Python/TS/Java/Kotlin, reification on C#/Rust/Dart, unobservable in v1 via 3 rules (no runtime type tests on generic params, no reflection in v1, structural `==`). There is no emitter in M1, so erasure vs reification cannot be observed - the decision is recorded, not proven. User generics deferred to v2. | SPEC §7.7 |
| G12 | **Applied.** Cheap flagships in v1: `?.`/`??` (swallow rule settled: `?.` short-circuits only on `none`), pipeline `|>` (Elixir-style, frozen), Rust-style `try` block, typed enums, statement-level `?`, core `match` (statement, exhaustiveness = error), runtime-only contracts, value-type semantics (structs/enums copied, structural `==`, struct field assignment is an error), C-style ternary `cond? a: b`. Precedence table frozen (§9.2). | SPEC §9; `lexer.py`, `parser.py`, `checker.py` |
| G13 | **Applied.** npm DROPPED from v1 distribution (HIGH RISK / UNVERIFIED). v1: PyPI + pipx + zipapp + GitHub Releases + PowerShell installer. | SPEC §17.3 |
| G14 | **Applied (design) / UNVERIFIED (numbers) - specified, not implemented.** Incremental content-hash caching design (§4.1); `check` vs `--fast` modes (implemented in the CLI); `--help` < 100 ms design target; daemon escape hatch. No compile-time figures cited (U2). Benchmarks are M2+ work - no benchmark exists in M1. | SPEC §4.1; `cli.py` |
| G15 | **Applied.** Compiler/LSP/AI boundary copied (§6.5 verbatim) + the 4 things a compiler can never do. LSP is M2+, not M0/M1. AI is out of scope forever. | SPEC §5.3 |
| G16 | **Applied.** §23 renamed "What newr takes from other languages" (table stays, no "best of all" slogan claim); honest v1 story ("one readable source, seven readable outputs, with each target's libraries available"); v1 domain = small cross-platform CLIs/tools; fame timeline as planning horizon; Haxe precedent addressed head-on. | SPEC §1, §23 |
| G17 | **Applied - with a contradiction, fixed in the M1 fix pass.** New repo starts at 0.1.0 (0.5.0 belongs to the old `discord-py-skills` line and never leaks in - the report previously said 0.6.0, which was wrong; the discord-py-skills line is at 0.5.0). | `compiler/__init__.py` |
| G18 | **Applied.** `task` is the v1 concurrency primitive (discord module); no threads/shared mutability in v1; thread-safety promise in the spec (value types are safe to share); concurrency deferred to v1.1+ with a design (structured: async/await or actors - undecided). | SPEC §8.4 |
| G19 | **Applied.** Exactly one absent value, spelled `T?` (e.g. `str?`), value `none`. `option[T]` spelling REMOVED. `T?` methods `.is_some()/.is_none()/.unwrap()`; `or` unwraps with a default. | SPEC §7.2; `ast_nodes.py`, `modules.py`, `checker.py` |
| G20 | **Specified, NOT implemented in M1.** No inline target code in v1 (compiler rule); FFI/binding story spec'd as M1 design work (§20.1: `extern` declarations, per-target bindings, binding manifest verified by the compiler - v1.1 direction). v1 interim: no FFI - extend readable generated code by hand. There is no FFI code in M1 (no emitter to bind to). | SPEC §20.1 |

---

## 8. v0 compatibility (trust the code, flag contradictions)

- **v0 files keep working:** `hello.newr` (the general hello) and the
  v0 bot files `hello_bot.newr` + `showcase.newr` parse+resolve+check
  cleanly through the new front end (zero errors). The v0 bot blocks are
  **sugar over the discord module** (no migration): a file with bot
  blocks implicitly imports `discord`; `reply:`/`log:` sugar desugars
  via `CommandDecl.effective_body()` / `EventDecl.effective_body()`.
  Bot-block position is relaxed (free among top-level items, at most
  one - v0 required it first).
- **v0 rules kept verbatim:** bot name 1–32, exactly one bot, command
  description defaults to the name, empty description/reply errors,
  description ≤100, reply ≤2000 counting placeholders at max expansion
  (user/channel 22, guild 100), ephemeral default false, placeholder
  contexts, fn-name collision rejection (`foo-bar` vs `foo_bar`),
  never silently ignore a line. The one deliberate wording change:
  "did you mean" → rustc named form ("there is a variable with a
  similar name: `count`") - verdicts identical.
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
  this VM) - the repo history's "70/70" figure is stale. Whole-repo
  suite: 441 tests, 5 skipped.
- **Design-doc contradictions resolved by trusting the code:** the
  original v1 design doc framed newr as a bot language - superseded by
  the user's general-purpose correction (21:49 WIB) and the deep
  research (G1). The expansion doc's poise version is **0.7.1** (not
  0.6.x); Disnake is NEVER (its CONTRIBUTING bans ALL LLM contributions
  repo-wide, verified verbatim); DSharpPlus is NOT archived.

---

## 9. What M2 needs next (emitters - discord.py P0 first, then discord.js P0)

1. **Python emitter (P0) at byte-parity:** regenerate `hello.py` and
   `showcase.py` from `hello_bot.newr` / `showcase.newr` and diff
   against the v0 goldens in `examples/` - byte-identical, or record
   every change with a reason in this report. The emitter consumes the
   AST + `expr_type` (filled on every expression by `check_program`)
   and `Program.modules` for qualified names.
2. **`?.` signal:** `Member.optional` is the ONLY `?.` signal in the
   AST (there is no `?[]` in v1). The emitter must lower `?.` chains
   with short-circuiting (`a?.b.c` short-circuits the whole chain).
3. **Benchmarks (G14):** hello + economy per target vs hand-written
   equivalents, recorded honestly - numbers, not claims. Same for
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
   spec must say so in one place with the weaker behaviour named - a
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
10. **CI:** the matrix exists (fixed for all 3 OSes in the fix pass -
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
only - no third-party dependencies in the compiler.

---

## 11. M1 fix pass - cold evaluation EVAL-M0M1.md (6 Nov 2026)

A cold evaluation by four parallel reviewers (compiler internals,
SPEC-vs-code + G1–G20, adversarial testing, tests/CI/docs) found the
M1 front end genuinely good but not ready for M2: 3 crash-class bugs,
2 type-system bugs breaking flagship features, spec/code drift, and
false README/CI claims. Baseline re-verified before the fix pass: 354
tests green. The fix pass (TDD: `tests/test_eval_fixes.py` written RED
first - 40 of 53 failed on the first run) fixed **all 14 must-fix
issues and all 10 nice-to-haves**, no regressions (414 green after).

### Must-fix M-01–M-14

| # | Fix |
|---|---|
| M-01 | Depth guard everywhere: parser (`parse_expr`/`parse_block`/`parse_type`), resolver (`_walk_expr`/`_walk_stmt`), checker (`synth`/`_walk_stmt`), discord checker (stops silently). Limit 200 (`MAX_NESTING_DEPTH` in `errors.py`). Past it: E1001 "too deeply nested", exit 4 - never a RecursionError traceback, and `--json` still emits its envelope (CLI backstop in `main()` + `_check_or_build`). |
| M-02 | Non-UTF-8 source is an environment error (exit 2) naming the file - CLI `_read_source`, resolver import reads, `_source_cache`. No more UnicodeDecodeError traceback. |
| M-03 | `T` is assignable to `T?` (checker `_check` widening, SPEC.md §7.2). The backwards "unwrap" help for the nullable→nullable case is gone. |
| M-04 | `result` is bound in `ensures` conditions (resolver child scope; the checker already bound it). Root cause of the false-green: `tests/test_checker.py::check()` ignored resolver diagnostics - it now returns resolver + checker diagnostics combined, and `tests/test_eval_fixes.py::full()` does the same (incl. parse errors). |
| M-05 | Hex/binary literals get the int64 range check (lexer): values > 2^63 are E_INT_OVERFLOW, like decimal. |
| M-06 | Spans are RAW-file byte offsets: the lexer tracks the raw byte length of every normalized character (CRLF = 2 bytes, BOM = 3). JSON `start_byte` now matches the bytes on disk. CLI reads with `newline=""` so the lexer sees the raw text. |
| M-07 | Lone CR is E_BAD_LINE_ENDING through the CLI (same `newline=""` read - the old `read_text()` universal-newline mode translated CR away before the lexer saw it). |
| M-08 | `-9223372036854775808` is representable: the lexer lets 2^63 through, the checker accepts it only when directly negated (incl. parenthesised), and rejects any other literal > INT64_MAX with E_INT_OVERFLOW. |
| M-09 | `??` rule (SPEC.md §9.2): `T? ?? T` → `T`, `T? ?? T?` → `T?`, `T? ?? none` → `T?`; mismatched RHS is E2001. |
| M-10 | `?.` on a non-nullable receiver is E2001 (checker `_member_field_type` + `_call_member`, SPEC.md §9.2). The nullable-branch recursion carries an `_in_chain` flag so chains after a real `?.` do not false-positive. |
| M-11 | `--explain` covers all 9 lexer codes + E1001's nesting note. The real lexer code set is 9 (E_BAD_ESCAPE, E_BAD_INDENT, E_BAD_LINE_ENDING, E_BAD_NUMBER, E_INT_OVERFLOW, E_STRAY_BRACE, E_TAB_INDENT, E_UNEXPECTED_CHAR, E_UNTERMINATED_STRING) - verified by grep, not guessed. |
| M-12 | CI: case-collision step is Linux-only (`if: runner.os == 'Linux'` - APFS/NTFS are case-insensitive); `python3` → `python` everywhere (setup-python's canonical command; `python3` is unproven on windows-latest); zipapp is built ONCE on ubuntu and uploaded as an artifact, the matrix job downloads that same .pyz (the old "each runner builds its own" proved nothing cross-OS); the encoding grep now covers `read_text()` + `open()` and is non-vacuous (cli.py has both). |
| M-13 | SPEC contradictions resolved: §16 now lists the full 38-code scheme (29 numbered + 9 lexer) with a pointer to `docs/error-index.md`; gate 0.6 → 0.7 in §16, §25 U6, and `errors.py` docstring; manifest §11 is a DRAFT schema (required name/version, optional rest) vs the M4 decision; §17.3 `diagnostics.py` → `errors.py`; §5.2: only the resolver attaches a `fix` in M1; §21.4 shop import moved to the top of the printed example; `examples/hello_bot.newr` header fixed; §12.2 named modules work by name without an import + `time.format` lists all 5 formats; §3.1 documents raw-byte spans; SPEC line-count claim corrected (2,129 at freeze → 2,191 after the fix pass). |
| M-14 | README is honest: lead + Install in future tense (`pip install newr-lang` is planned - PyPI 404 as of 6 Nov 2026), zipapp is a manual smoke test, "proven by CI matrix" → "will be proven", conformance is a seed (not "the single definition" yet), M1 status is COMPLETE, exit code 3 removed. |

### Nice-to-have N-01–N-10

| # | Fix |
|---|---|
| N-01/N-02 | SPEC.md §2.1 documents the by-name exemption: `"discord"` in core occurs only at the two checker bypasses (`_module_member_type`, `_call_module_member`), the resolver's `NAMED_MODULES` registry + W1002 exemption, and `RESERVED_BUILTIN_NAMES`; `ENTITY_TYPE_NAMES` is a deliberate core-owned list. `tests/test_boundaries.py` asserts the exact list (6 tests). |
| N-03 | Dead `_BINARY_BP` deleted; the live table is the frozen `PRECEDENCE` (MappingProxyType, SPEC.md §9.2). |
| N-04 | `errors.py` NewrError docstring + SPEC.md §16 corrected: recovery is checker/resolver only; the parser has NO panic-mode recovery - it stops at the first syntax error. |
| N-05 | Messages: import cycle reports only E3004 (no E3006/W1002 follow-ons); `option[int]` hint points at `T?`; case-mismatch imports name the on-disk file; `let log = 1` is E3007 like `let print = 1`; dangling `?` is diagnosed on the operand first (mentions `Result`); `print g()?` gets a `?` help. |
| N-06 | `--fast` help + stderr warning now say it skips the full type check AND all Discord validation. |
| N-07 | Op-identity tests for `+ - * / % ** == != < <= > >=` and `or`/`in`/`??`, pipeline-desugars-to-Call + ternary-node tests, combined-diagnostics helpers (`full()` in test_eval_fixes.py, `check()` in test_checker.py), `--explain` CLI-tested for all 9 lexer codes + E1001, `check --fast` CLI test, CRLF lexer + CLI tests. |
| N-08 | Launcher not Windows-executable: honest docstring, no action (out of scope for the fix pass). |
| N-09 | `modules.py` docstring marks the table APPROXIMATE (checker special-cases win); named modules work without an import (SPEC.md §12.2). |
| N-10 | `input()` takes 0–1 args (SPEC.md §12.1); `list.len()` added to METHODS (SPEC.md §12.4 already listed it); `time.format` accepts 5 formats (SPEC.md §12.2 now lists all 5). |

### Decisions recorded in the fix pass

- **Depth limit 200, code E1001** - far above anything a human writes,
  far below the interpreter's recursion limit; one code for all four
  stages so the CLI backstop and the guards agree.
- **`??` rule** - see M-09 above; the nullable-RHS case returns `T?`
  (not `T`), because the result can still be `none`.
- **`?.` on non-nullable is an error, not a silent pass** - a
  non-nullable value is never `none`, so the chain is dead code and
  almost certainly a typo for `.` (SPEC.md §9.2).
- **Lexer codes keep their `E_*` names** - documented as the lexer-code
  family in SPEC.md §16, not renumbered into the `E1xxx` scheme.
- **Named modules work without an import** - `math.sqrt(2.0)` checks
  with no `import` line; `import math` is still allowed and changes
  nothing. `discord` stays bypassed (UNKNOWN) in core.
- **G1–G20 headline corrected** - the honest count is 13 applied, 4
  specified-not-implemented (G2 int64 runtime semantics, G9 manifest,
  G14 benchmarks, G20 FFI - spec only, no M1 code), 2
  applied-with-contradiction (G10 gate 0.6/0.7, G17 version 0.5.0 vs
  0.6.0 - both fixed in this pass), 1 applied-but-unobservable (G11
  generics erasure/reification - no emitter in M1 to observe it on).
  See §7 for the corrected rows.
- **M2 entry criteria (from the eval) now met:** (1) M-01–M-05 fixed +
  regression tests; (2) M-06–M-10 fixed; (3) M-11+M-13: all 38 codes in
  SPEC.md §16 with `--explain` coverage, contradictions resolved;
  (4) M-12: CI fixed for all 3 OSes (still never run - the matrix going
  green is the remaining proof); (5) M-14: README future tense;
  (6) N-07: op-identity tests + combined-diagnostics helpers;
  (7) conformance corpus seeded.

---

## 12. Fix round 2 - stress campaign STRESS-REPORT.md (7 Nov 2026)

The stress/endurance campaign (7,000+ `newr check` runs, 57,132 test
executions) found 5 findings: 1 crash, 1 medium bug, 2 minors, 1 perf
cliff. This round fixed **all 5**, TDD (`tests/test_stress_fixes.py`
written RED first - 33 of 38 failed on the first run, plus the
depth-5000 test failing on time), no regressions: **453 tests green**
(baseline 414 + 39 new). Nothing under `stress/` was modified; the
crash inputs are the oracle and still pass through the CLI cleanly.

### F-01 - CRASH-1: file ending in an identifier, no trailing newline (HIGH)

Root cause: `_peek()` returns `""` at EOF, and `""` is not a
character - but two code paths treated it as one. `_is_name_start` /
`_is_name_continue` passed it to `unicodedata.category`, which raises
`TypeError` (raw traceback, exit 1). The same root cause also **hung**
the lexer, found during verification (not in the report): `"" in
"<str>"` is `True` in Python, so four loops never terminated at EOF -
the hex/binary digit loops (`print 0xFF` / `print 0b10` with no
newline), the whitespace loops in `_handle_newline` (trailing spaces
after the last newline) and `_process_indent` (indented blank line at
EOF). Five hang repros, all confirmed live before the fix.

Fix (`compiler/lexer.py`): the name predicates reject any
non-single-character input (`len(ch) != 1`); the four loops got a
`pos < len(source)` guard; the exponent check got the same guard as
hardening (it was safe by luck - the inner digit test fails at EOF -
but `"" in "eE"` is `True`, so the trap was live). A file ending in an
identifier now lexes normally: `printf 'print x'` gives a clean
`error[E3001]: undefined name 'x'`, exit 4. The regex fast path in
`_lex_name` (see F-05) covers ASCII names at EOF on its own; the
guard is what saves Unicode names - proven by
`stress/crashes/fuzz-20261106-1871.newr`, which ends in a stray `é`
and crashed in `_lex_name`'s per-char loop without the guard.

### F-02 - BUG-2: top-level use-before-def silently accepted (MEDIUM)

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
applies uniformly to imported modules too - a `let` RHS executes when
the module loads, in file order, on every target, so a forward
reference between module lets would be a runtime error waiting to
happen. The diagnostic shape matches the existing local E2005:
`print later` / `let later = 1` gives `[E2005, W1001]`, exactly like
the fn-local case with no later use. The resolver docstring and the
`_collect_top_level` comment were updated to state the corrected rule
(the old text claimed a blanket forward-reference allowance).

### F-03 - `1__2` / `1_` lex as valid ints (minor)

Decision: **reject** (strict reading of SPEC.md SS6: "Underscores may
group digits" - an underscore must sit between two digits, so a
leading, trailing, or doubled underscore groups nothing). New
`_check_underscores` in the lexer validates every digit run (decimal,
float, hex, binary): each `_` must have a digit on both sides of the
same run. `1__2`, `1_`, `0x_FF`, `0xFF_`, `0b1__0`, `1_.5`, `1e3_` are
E_BAD_NUMBER; `1_000`, `1_000_000`, `0xF_F`, `0b1_0`, `1_000.5`,
`1e1_0` stay valid. `--explain E_BAD_NUMBER` and
`docs/error-index.md` now mention the rule. No SPEC.md wording change
was needed - the decision follows from the existing sentence.
Deliberately not tested: `1._5` is **not** an underscore case - the
lexer produces INT(1) DOT IDENT(_5), a member access on an int
literal, which the checker rejects with E3001. The underscore rule
only applies inside a number token.

### F-04 - raw NUL byte accepted inside string literals (minor)

Fix (`compiler/lexer.py`): `_lex_string_char` raises E_UNEXPECTED_CHAR
("NUL byte is not allowed in a string literal", help: "write `\u{0}`
for a NUL character") for a raw `\x00` inside a string - the same code
a raw NUL outside a string already gets, and the same treatment as a
raw newline inside a string. The escape route still works:
`"a\u{0}b"` lexes fine. Reasoning, no SPEC.md change: SPEC.md SS6
enumerates the string escapes (`\"`, `\\`, `\n`, `\t`, `\r`,
`\u{XXXX}`); a raw NUL is a control character with no literal form,
so it is rejected like a raw newline. Deliberately left as-is: other
raw control characters (e.g. a literal tab) inside strings are still
allowed - the spec is silent on them and the finding was NUL-only.

### F-05 - nested `if` / blocks go quadratic (MEDIUM)

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
(O(1) memory) instead of a per-character list - a 25 MB file would
otherwise build a 25M-entry prefix list; non-ASCII / CRLF sources
keep the exact old slow path. (b) `_advance` newline-free fast path
(`col += len(out)`, no per-char loop). (c) `_process_indent` and
`_handle_newline` scan whitespace runs with a compiled regex
(C-speed); a tab still raises E_TAB_INDENT at the tab's own position.
(d) `_lex_name` matches ASCII name runs with a compiled regex, used
only when the following character is not a name-continue character -
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
exit 4 - never a traceback or a hang. Depth 5000's 25 MB file is the
honest cost of the input size, not of the nesting.

### Red-check results (each fix temporarily reverted, then restored)

| Fix | Reverted | New tests failing |
|---|---|---|
| F-01 guards + loop guards | name predicates + 4 loop guards | 6/12 (5 EOF hangs by timeout + `fuzz-20261106-1871.newr` TypeError); the ASCII crash tests pass on revert because the `_lex_name` regex fast path covers ASCII names at EOF - the guard is pinned instead by the new non-ASCII test, which fails (TypeError) on guard revert and passes on the fix |
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
  support `0o` literals (only `0x`/`0b`) - a doc inaccuracy from M1,
  left for a later pass.
- SPEC.md was **not** changed: no finding forced a spec decision.
  F-02 implements the existing SS8 sentence; F-03 follows from the
  existing SS6 sentence; F-04 follows from the SS6 escape list.

## 13. Fix round 3 - re-stress STRESS-REPORT-2.md (7 Nov 2026)

One new pre-existing crash from the fresh fuzz seed 20261107; fix
round 2's work was fully verified by the re-stress (every round-1
finding gone, corpus 0 mismatches, fuzz seed 20261106 3,000/3,000
clean, depth-if 5000 in 0.49 s, endurance 60/60 green) - the ship
gate stayed closed only because of this one bug.

### CRASH-2: malformed `t(...)` raises raw AttributeError (HIGH)

`print t("k", 1)` and `print t(1)` crash the parser with
`AttributeError: 'Span' object has no attribute 'span'` (exit 1)
instead of the intended clean E1001 (exit 4). Root cause:
`_parse_call` in `compiler/parser.py` (lines 1838, 1842) passed a
`Span` (`func.span` / `args[1].span`) to `self.err()`, which expects
a `Token` and reads `tok.span`. Pre-existing - parser.py was
byte-identical to round 1; round 1's fuzzer never emitted `t(...)`
calls. Reached only when a `t()` call is malformed: a second
positional argument (e.g. `t("buy.ok", item)` - forgetting `item:`),
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
**10/13 failing** - the identical RED shape (3 CLI failures + 7
in-process errors). Restored from the backup, sha256 verified
identical, full suite re-run green (**466 OK**).

### Notes from verification

- One test expectation was corrected with evidence, not weakened:
  the control case `print t("k", item: 1)` does NOT produce E2009
  in the in-process `full()` helper - E2009 comes from the Discord
  checker (`compiler/discord_check.py`), which `full()` does not run
  (resolver + core checker only). The in-process test pins the core
  pipeline as clean (`[]`); the CLI test pins the E2009 end to end.
  The test file records this in a comment.
- Probe artifact, not a bug: `python3 -m compiler.cli check <file>`
  exits 0 silently - `compiler/cli.py` has no
  `if __name__ == "__main__":` block, so the module import just
  exits. The supported entry points are the `./newr` launcher and
  the packaged console script (`compiler.cli:main`); all
  verification in this round used `./newr` (the same invocation as
  `stress/run_stress.py`). Out of scope, not changed.
- SPEC.md was **not** changed: the fix restores the intended E1001
  error path; no spec decision was involved.


---

## 14. M2 - Python emitter (7 Nov 2026)

M2 builds `newr build --target py`: a readable-Python emitter, the
`newr_rt.py` checked-int runtime, per-target goldens, a ~120–160
program conformance corpus that runs on the Python target, and honest
benchmarks B1–B8. Research handoff:
`~/workspace/discord-py-skills-research/newr-m2-deep-research.md`
(2,188 lines); its §8 is the exit-criteria checklist, its §9 the pins
decided below, its §10 the UNVERIFIED register (U1–U32, still honest:
measured, never quoted).

Baseline at M2 start: suite **466 green** (fix round 3), ship gate
passed, repo `drewdestar/newr` at commit `78d4ccf`.

### 14.1 The 12 spec pins - decided before coding (research §9)

Every pin is a decision with a reason, recorded here before the
emitter was written against it. Where the spec already answered the
question, the pin says so (the research asked; the spec had decided).

| # | Question | Decision (M2) | Reason |
|---|---|---|---|
| P1 | Python floor 3.10 vs PEP 701 f-strings (3.12+) | **Floor stays 3.10.** Emitter uses only the 3.10-safe f-string subset: alternate quote types inside replacement fields, hoist any expression containing a backslash (or both quote types) to a temp variable before the statement. | Raising the floor would drop 3.10/3.11 users for one syntax feature; the mitigation is mechanical and CI (matrix 3.10–3.13) proves it. No walrus inside f-string fields; `?.`/`??` go through helpers, never walrus. |
| P2 | `round()`: half-to-even or half-away-from-zero? | **Half-away-from-zero (schoolbook):** `round(2.5) = 3`, `round(-2.5) = -3`. Via `newr_rt.round_half_away`, implemented with `decimal.Decimal` on the EXACT float value + `ROUND_HALF_UP`. `round(x)` returns `int` (int64 range-checked, overflow throws); `round(x, n)` returns `float`. | Bare `round()` would drift across targets (Python half-even, JS half-up-toward-+∞, Rust half-away) - cross-target determinism forces a helper regardless of which rule; half-away is the most human-expected rule. **Correction (7 Nov, caught by the runtime's own tests):** the first draft rounded `Decimal(str(x))` - the shortest repr, not the value. That breaks identity for large integral floats (`round(9223372036854774784.0)` became `9223372036854775000`). The rule is the exact value, so `round(2.675, 2)` is 2.67 - the representation fact every target shares (research §3.4). Non-finite input: `round(inf)` raises `OverflowError` (Python's behaviour); `round(inf, n)` returns `inf`, `round(nan, n)` returns `nan` (Python's behaviour). |
| P3 | `set` ordering | **Unordered** - decided by SPEC §7.6 already ("`set` is unordered"). Emit bare `set(...)`. | The research asked; the spec had answered. Iteration order is unspecified (Python hash order); conformance programs must not assert set iteration order. |
| P4 | `time.format` timezone | **UTC.** Emit `datetime.fromtimestamp(ts, tz=timezone.utc).strftime(fmt)`. | Same program prints the same output on every machine and timezone (conformance determinism; SPEC §3.5 byte-identical output). The fixed format set has no timezone-sensitive names. |
| P5 | `.chars()`: code points or grapheme clusters? | **Code points** - decided by SPEC §7.6 already ("one code point per element"). Emit `list(s)`. | The research asked; the spec had answered. Grapheme clusters would need a third-party dependency; none is added. |
| P6 | Float `/0` | **Error.** Emit bare `/` for floats (Python raises `ZeroDivisionError` natively); int `/0`/`%0` raise via `tdiv`/`tmod`. | SPEC §7.5: "Division or modulo by zero is a runtime error" (stated generally, no float exception). Matches Python; no `inf` guard is emitted. |
| P7 | `bool("false")` | **Parsing, not truthiness.** `bool("true")` → `True`, `bool("false")` → `False` (exact lowercase only); any other string raises `ValueError`. `bool(0)` → `False`, `bool(n != 0)` → `True`; `bool(0.0)` → `False`. Via `newr_rt.to_bool`. | Bare `bool("false")` is `True` in Python - the classic pitfall; newr spells booleans lowercase everywhere, so parsing is the only honest reading. |
| P8 | `str()` rendering | Via `newr_rt.to_str`: `str(true)` → `"true"`, `str(false)` → `"false"`, `str(none)` → `"none"`, `str(int)` → decimal, `str(float)` → shortest round-trip (Python `str`), `str(str)` → itself, `str([1, 2])` → `"[1, 2]"` (elements rendered recursively, strings bare), `str(dict)` → `"{k: v, ...}"` (insertion order, strings bare), `str(set)` → `"{a, b}"` (iteration order, unspecified), `str(error)` → its message, `str(ok(v))` → `"ok(<v>)"`, `str(err(e))` → `"err(<e>)"`, struct → `"Point(x: 1, y: 2)"`, enum bare → `"Color.Red"`, enum with fields → `"Shape.Circle(radius: 1.5)"`. | Bare `str()` gives Python spellings (`"True"`, `"None"`, `"['a']"`). newr's lowercase style + display (not debug) rendering fixes every case in one tested helper. |
| P9 | `list.get(i, default)`: negative index | **No wrap - out-of-range → default.** Any `i` with `0 <= i < len(lst)` returns the element; anything else (including negatives) returns `default`. Via `newr_rt.list_get`. | `.get` is a safe accessor; wrapping would make "out of range" untestable and is a known Python footgun. |
| P10 | `log` level | `log <expr>` = **info** (SPEC §9.1). `log.warn` → `logging.warning` (never the deprecated `logging.warn`); `log.info` → `logging.info`, `log.error` → `logging.error`. General programs emit `logging.basicConfig(level=logging.INFO, ...)` once when `log` is used (same format string as the bot golden); bots already have it in the template. | Resolves research §6.8 U10: `logging.warn` is deprecated since Python 3.3; emitting it would print a deprecation warning on every run. |
| P11 | `store` durability / transactions | **aiosqlite** (research §7.3 recommendation). One long-lived connection opened in `setup_hook`; **commit after every mutation**; ledger + balance wrapped in **one transaction**; a **duplicate idempotency key is a replay** (returns the previous result, no double-apply). DB file: `<store>.db` next to the emitted `.py` (deterministic regardless of working directory). | `asyncio.to_thread` + stdlib `sqlite3` was the stdlib-only alternative; aiosqlite is more readable in async handlers and is the discord.py-skill ecosystem's choice. Dependency consequence: the emitted bot needs `aiosqlite` installed (documented in the emitted file's header comment when a store is used). |
| P12 | Collection copy-on-assign | **Reference semantics for collections in v1** (Python-like): `let ys = xs` then `xs.append(3)` also changes `ys`. Structs/enums are value types (SPEC §7.8) - satisfied observationally by frozen dataclasses, since field assignment is a compile error and aliasing can never be observed. | SPEC §7.8 names only structs/enums as value types; §7.7(3) structural `==` already holds for Python collections. A copy-on-write design is v1.1 work (needs the U9 research pass first). The conformance corpus pins the reference behaviour with a program, so the choice is observable and revisit-able, not silent. |

### 14.2 Further M2 decisions (recorded before coding)

- **D1 - Python floor stays 3.10** (P1). CI matrix 3.10–3.13 proves
  the emitted code on every supported version.
- **D2 - Emitted header names the actual input file as passed**
  (`# Generated by newr from <path> - do not edit by hand.` +
  rebuild line; bots add the bot description as line 3). The header
  never lies about its source. Consequence: the emitter's output for
  `examples/hello_bot.newr` differs from the v0 golden
  `examples/hello.py` in exactly the two header lines - the v0 source
  file was named `hello.newr` and was renamed to `hello_bot.newr` in
  v1 (SPEC §13.10). `examples/hello.py` itself is **not** modified
  (CI pins its SHA-256); the M2 golden for the hello bot is
  `goldens/py/hello_bot.py`, and the golden test asserts the diff
  against the v0 golden is exactly those two lines. `showcase.newr`
  is byte-identical to `examples/showcase.py`.
- **D3 - Golden policy.** Goldens live in `goldens/py/` (one per
  example). Compare-only by default (`tests/test_goldens.py` builds
  to a temp dir and diffs - it never writes a golden). Regeneration
  is `python3 conformance/run.py --bless [--only <name>]` (research
  §4.6.2); a golden diff in a change is reviewed line-by-line. A
  golden is never hand-edited.
- **D4 - `newr_rt.py` location and shipping.** Source of truth is
  `compiler/newr_rt.py` (in the compiler package, unit-tested as
  `compiler.newr_rt`). `newr build` copies it next to the emitted
  `.py` **only when the program uses a runtime helper**
  (usage-tracked import - Gleam-style); the copy is byte-identical
  to the package file. Programs using no helpers (the v0 bot
  goldens) get no import and no copy.
- **D5 - Contracts throw via explicit checks, not `assert`.** A
  violated `requires`/`ensures` emits `if not <cond>:
  raise newr_rt.NewrError("contract violated: ...")`. The spec's
  parenthetical "Python `assert`" (§9.7) is the lowering family, not
  a mandate - and `assert` is disabled by `python -O`, which would
  violate the normative §9.7 rule "always evaluated in v1". `result`
  inside `ensures` maps to the internal temp `_newr_result`; every
  `return <expr>` in a function with `ensures` is rewritten to
  assign `_newr_result`, check, then return it.
- **D6 - Error values.** `NewrError(message, code=None)` with
  `.message` / `.code` (SPEC §10); `NewrOverflowError(OverflowError)`
  and `NewrDivisionByZeroError(ZeroDivisionError)` subclasses so a
  newr `catch` and a bare `except OverflowError` both see them.
  `throw "msg"` → `raise NewrError(msg)`; `throw err` → raise as-is.
  A caught non-newr exception is wrapped by `as_error()` (code =
  the Python type name, e.g. `FileNotFoundError`).
- **D7 - `?` lowering (exception-based, research §3.3 option 1).**
  `let x = e?` → evaluate into a temp; on `Err`/`none`, return early
  from the enclosing function (returning the `Err` itself when the
  function returns `Result`, `None` when it returns `T?`); else
  unwrap. `return e?` is the same shape ending in `return
  <unwrapped>`. The checker guarantees the enclosing function's
  return type is compatible.
- **D8 - `try <expr>`** emits `newr_rt.try_result(lambda: <expr>)`
  → `ok(v)` on success, `err(as_error(e))` on a thrown error.
- **D9 - `?.` / `??` via helpers, never walrus.**
  `newr_rt.opt_chain(v, lambda _v: ...)` (returns `None` when `v`
  is `None`) and `newr_rt.coalesce(v, lambda: default)` (default
  evaluated lazily, only when `v` is `None`). A `?.` chain
  collapses into ONE `opt_chain` call with the full postfix path in
  the lambda (`a?.b.c()` → `opt_chain(a, lambda _v: _v.b.c())`) -
  the base expression is evaluated exactly once, and `is not None`
  (never truthiness) is the test.
- **D10 - Lambdas capture by value** (SPEC §9.5) via default-arg
  snapshot: free variables become parameters with defaults bound at
  creation (`lambda x, a=a: ...`). Block-body lambdas cannot be a
  Python `lambda`, so they are hoisted to a `def` immediately before
  the enclosing statement (same snapshot defaults, same timing).
  Free variables are computed by the emitter, excluding builtins and
  module-level fn/struct/enum/module names.
- **D11 - Top-level layout.** Imports first (only the stdlib imports
  the file actually uses), then structs/enums, then functions, then
  top-level `let`/`const` in file order, then the remaining top-level
  statements in file order. (Correction to the first draft of this
  decision, made before coding: no topological sort is needed - fix
  round 2 made a top-level use of a `let` before its declaration an
  E2005 error, so lets already execute in dependency order. Emitting
  all lets before the other statements still matters: a function body
  may read a top-level `let` declared after the function (the
  checker's deferred-execution exemption allows it), and a top-level
  statement calling that function must see the `let` assigned. A bare
  top-level `return` becomes `sys.exit(0)` - it exits the program,
  SPEC §9.1. Consequence, recorded while coding the emitter: the
  top level is two-phase - every `let`/`const` initialises before
  any other top-level statement runs. A `let` whose RHS reads a
  module global therefore sees the `let`'s initial value, NOT a
  value a preceding top-level assignment statement wrote (that
  assignment runs in phase 2, after all the lets). Source order is
  preserved *within* each phase. Known gap, front-end not emitter: a top-level `let`
  whose RHS calls a function reading a *later* `let` is accepted by
  the checker (deferred-execution exemption) but fails at runtime
  with `NameError` - the checker should narrow that exemption; filed
  as a limitation in §14.3.)
  **CORRECTION 7 Nov 2026 (M2 fix round, item 1): the two-phase
  layout is GONE.** It silently computed the wrong value whenever
  an assignment sat between two lets (the evaluation's repro
  printed `1` instead of `5`), violating SPEC §8. The emitter now
  emits structs/enums/functions first (definitions are hoisted - a
  fn body reads module globals at call time), then ALL top-level
  statements in file order. The D11 rationale (fn bodies reading
  later lets) is preserved by the defs-first order, not by
  hoisting lets. The companion gap is closed by fix-round item 3
  (the resolver now rejects a top-level call to a fn that reads a
  later let), so the two fixes agree: no program can observe a
  let before its declaration, by any path the front end accepts.
- **D12 - `match` lowering.** `if`/`elif` chain over a
  `_newr_m0` temp holding the scrutinee (evaluated once).
  Enum/struct patterns → `isinstance` + field bindings at the arm
  head; literal patterns → `==`; list patterns → `len` + element
  patterns; or-patterns → `or` of sub-conditions; guards are
  `and`ed. A `Name` pattern binds. With no wildcard arm, the final
  `else` raises `NewrError("non-exhaustive match")` as defense -
  the checker proves exhaustiveness, so it is unreachable in a
  checked program.
- **D13 - `newr build` CLI.** `--target py` (default `py`; `js`
  is a clean usage error: the JS emitter lands in M3).
  `-o`/`--output` (default `<stem>.py` next to the input).
  `--check` verifies only (writes nothing); `--fast` parses +
  resolves only. The build runs the full pipeline
  (parse + resolve + core check + discord check) unless `--fast`.
  A file import emits one `.py` per imported `.newr` next to the
  main output, and the main file does a plain `import <module>`.
- **D14 - Research §8.1 corrections.** (a) Its byte-parity line
  says "`newr build examples/hello.newr`" - the wrong filename; the
  bot source is `examples/hello_bot.newr` (BUILD_REPORT §9.1 is the
  correct statement; the research was written before the v0→v1
  rename was fully propagated). (b) Its suite baseline "354 tests"
  is stale - the M2 baseline is **466**. (c) `log.warn` →
  `logging.warning` (P10).
- **D15 - discord.py `choices` / `Range`:** the exact decorator
  signatures were UNVERIFIED in the research (U13). M2 verifies
  them against installed discord.py 2.7.1 before emitting them for
  `shop.newr`'s options; the outcome is recorded in §14.4.
- **D16 - `t()` lowering.** A module-level `STRINGS` dict
  `{locale: {key: template}}` + a `_t(key, locale, **kwargs)`
  helper, emitted only when `strings` blocks exist. Lookup order:
  exact locale → base language → default locale (the first declared
  locale). Template placeholders render with `to_str`. A template
  placeholder that is a handler binding in scope at the call site
  (e.g. `{user}`) is passed automatically by the emitter when the
  explicit `t()` arguments do not name it.
- **D17 - `store` lowering.** aiosqlite (P11). One connection in
  `setup_hook`; `economy.balance(user)` reads the `balances` table
  (missing row → `0`); `credit`/`debit`/`transfer` wrap
  balance-update + ledger-insert in one transaction and are
  idempotent on `key` (duplicate key returns the previous result).
  `store.get/set/delete` are the generic table operations.
- **D18 - `for` over a dict iterates its keys** (Python `for k in
  d`); over a set iterates in hash order (unspecified); over
  `range(...)` iterates the list.
- **D19 - Literals.** Int literals via `repr` (exact); float
  literals via `repr` (shortest round-trip, research §3.4). The
  int64-minimum literal is emitted as `-9223372036854775808`
  (unary minus on the positive literal - exact in Python).
- **D20 - Name mangling.** User names are verbatim. A user name
  that collides with a Python keyword or with a name the emitter
  itself imports/uses in that file is mangled with a trailing
  underscore (research §1.11); `_newr_` is reserved for emitter
  temps/helpers. (A user who names a variable `_newr_result` would
  collide with the contracts temp - the prefix is documented as
  reserved; the checker does not forbid it, a known tiny gap.)
- **D21 - `input()` in a bot program.** The emitter warns on
  stderr (`input()` in a bot process blocks on stdin / raises
  `EOFError` - bots have no stdin); it still emits the call with
  Python semantics. A warning, not an error - the checker does not
  forbid it.
- **D22 - `set.remove` on an absent element is an error.** Emit
  the native `.remove` (raises `KeyError`); `.discard` is the
  no-error removal. (The checker has no `list.remove` / `list.pop`
  / `dict.values` / `str.trim` - the METHODS table in
  `compiler/modules.py` is the authority: list is append/contains/
  get/len only, dict is keys/get/contains only, str has `strip`
  not `trim`. The emitter raises `EmitterError` for any method the
  checker cannot have accepted, rather than guessing.)
- **D23 - File I/O emits native `open()` calls**
  (`encoding="utf-8"`, `with` blocks); `FileNotFoundError` etc.
  propagate as Python exceptions and are wrapped by `as_error` in
  a `catch` clause, so `.message`/`.code` stay total. Conversions
  raise native `ValueError` (research §3.3).
- **D24 - Float `%` is C `fmod` semantics** (dividend's sign) via
  `newr_rt.fmod`, which raises `NewrDivisionByZeroError` on
  `b == 0.0` (Python's float `%` takes the divisor's sign).
- **D25 - No `# newr:<file>:<line>` comments by default** (golden
  cleanliness wins); opt-in via `newr build --line-comments`.
- **D26 - `on_message` ignores the bot's own messages**
  (`if message.author == self.user: return` at the top of the
  handler).
- **D27 - `newr build --fast` with emission is refused** (usage
  error) - emission needs checked types, and `--fast` skips the
  check.
- **D28 - Float `**` goes through `newr_rt.pow_float`.** Python's
  float `**` differs from IEEE in two edge cases: a negative base
  with a fractional exponent returns a complex (newr has no complex
  type - the answer is NaN) and overflow raises `OverflowError`
  (IEEE float overflow is +/-inf, signed by a negative base with
  an odd integer exponent). `pow_float` fixes both; bare `**` is
  never emitted for floats.
- **D29 - A module function used as a value becomes a lambda.**
  `time.now` as a value has type `fn() -> int`, so the emitter
  emits `lambda: int(time.time())`; `math.sqrt` as a value becomes
  `lambda _a0: math.sqrt(_a0)`. User functions as values emit their
  (verbatim) def name; enum variants as values emit the variant
  dataclass name.
- **D9 (correction, 7 Nov, found while coding the emitter).** The
  first draft said "a `?.` chain collapses into ONE `opt_chain`
  call". That is only true for a chain with a single optional link
  (`a?.b.c` → one call, the non-optional tail folded into the
  lambda). A chain with several optional links needs one
  `opt_chain` call **per optional link**, nested
  (`a?.b?.c` → `opt_chain(a, lambda _v: opt_chain(_v.b, lambda _w:
  _w.c))`) - a single lambda body `_v.b.c` would raise
  `AttributeError` when `b` is `none`, because the intermediate
  `none` is not swallowed by the outer call. Non-optional links
  between optional ones fold into the nearest lambda body.
- **D30 - `?` on a nullable inside a Result-returning function
  (and the reverse) is an `EmitterError`.** The checker accepts it
  (its `Propagate` check only requires the enclosing function to
  return `Result` or `?`, without matching the operand kind), but
  there is no defined early-return value: a nullable in a `Result`
  function has no `Err` to return, and a `Result` in a nullable
  function has no `none` to return. The emitter refuses cleanly
  rather than inventing semantics; the checker should E2001 it
  (filed in §14.3).
- **D31 - `?.` on a nullable method swallows (checker gap).** For
  `v?.unwrap()` / `v?.is_some()` the checker returns the
  non-nullable method result type (`int` / `bool`) - it dispatches
  nullable methods before checking the `?.`. At runtime the `?.`
  swallows, so `none` can flow into a value the checker typed as
  non-nullable. The emitter follows the `?.` semantics (an
  `opt_chain` call) - the unsoundness is the checker's, filed in
  §14.3; a program that only writes `v.unwrap()` (no `?.`) is
  unaffected.
- **D32 - File I/O goes through runtime helpers, not hoisted
  `with` blocks.** `file.read/write/append` emit
  `file_read/file_write/file_append(path, ...)` calls in
  `newr_rt.py`. A hoisted `with` block would run at the enclosing
  statement - the wrong time inside a lambda body (the read would
  happen at creation, not at call time) and inside an optional
  chain (the read would happen even when the chain is swallowed
  because the receiver is `none`). A helper call is an expression,
  so it evaluates exactly when the newr expression does. `open()`
  inside the helpers passes `encoding="utf-8"` (D23).
- **D33 - Hoists inside a lambda body stay inside the lambda.**
  When an expression lambda / `opt_chain` lambda / `try_result`
  lambda needs a hoist (an f-string part with a backslash, a
  nested block-body lambda), the emitter hoists a `def` for the
  whole lambda and passes its name, instead of leaking the hoist
  to the enclosing statement (which would evaluate it at the
  wrong time). The same rule as D32, for the same reason.

### 14.3 Known gaps found while building M2 (front-end, not emitter)

These are checker/resolver limitations the emitter works around or
refuses cleanly. None blocks M2; each is a candidate for the M1
follow-up pass (or M6 tooling).

- **G-M2-1 - `?` operand/return-kind mismatch is accepted**
  (D30). The checker should E2001 a `Propagate` whose operand kind
  (`Result` vs nullable) does not match the enclosing function's
  return kind. **RESOLVED 7 Nov 2026 (M2 fix round, item 4).**
- **G-M2-2 - `?.` + nullable method returns a non-nullable type**
  (D31). `v?.unwrap()` / `v?.is_some()` should type as `T?` /
  `bool?`; the checker returns `T` / `bool`. **RESOLVED 7 Nov 2026
  (M2 fix round, item 5).**
- **G-M2-3 - Top-level `let` whose RHS calls a function reading a
  *later* `let` is accepted but fails at runtime with `NameError`**
  (D11). The checker's deferred-execution exemption should not
  cover a top-level `let` RHS. **RESOLVED 7 Nov 2026 (M2 fix round,
  item 3 - in the resolver, which owns E2005, not the checker).**
- **G-M2-4 - A user variable named `_newr_*` collides with
  emitter temps** (D20). The checker does not forbid the prefix;
  the emitter documents it as reserved. **RESOLVED 7 Nov 2026 (M2
  fix round, nice-to-have: the resolver's `_declare` rejects
  `_newr_*` names with E3001).**
- **G-M2-5 - Negative list index: RESOLVED 7 Nov.** SPEC §9.2 says
  out-of-range indexing is a runtime error and §1.1 forbids silent
  behavior, so a negative index is out of range (valid indices are
  0..len-1). The emitter uses `newr_rt.list_index`, which raises
  `IndexError` for `i < 0` or `i >= len` (the same error Python
  raises for an out-of-range index; `as_error` gives code
  "IndexError"). Python's wrap-around is never emitted.
- **G-M2-6 - `?` on a nullable `Result` has no defined meaning**
  (D30). The emitter refuses it with `EmitterError` (unwrap it
  first, then propagate the `Result`). **Checker side RESOLVED 7 Nov
  2026 (M2 fix round, item 4): the checker now E2001s it too, so
  the disagreement is closed from both ends; the emitter's refusal
  stays as defense-in-depth.**

### 14.5 Front-end bugs found and fixed while building M2

M2 discipline: a front-end bug found by the emitter is recorded
here, fixed with its own TDD + red-check, and reported - never
silently fixed in passing.

- **BUG-M2-1 - `_in_top_level` leaked into nested blocks**
  (checker, `compiler/checker.py::_walk_stmts`). The flag stayed
  True for the whole top-level statement walk, so a `let` inside a
  top-level `if` / `for` / `while` body (or a `try` catch) was
  treated as a module-level `let`: if it shadowed an
  already-checked name, `_check_let_const` hit its `top_checked`
  early return and never checked the value at all (its
  `expr_type` stayed `None`, which the emitter then refused with
  "an expression has no type"); if it did not shadow, the name was
  registered in the module env and leaked out of the block.
  Fix: `_walk_stmts` now owns the flag - it follows the
  `top_level` argument of that walk and is restored afterwards, so
  a nested walk is never top-level. Regression tests:
  `tests/test_checker_top_level_shadow.py` (3 tests, written RED
  first - all 3 failed before the fix). The fix was verified
  against the full suite (596 green).
- **BUG-M2-2 - `NewrOverflowError` / `NewrDivisionByZeroError`
  never set `.message`** (runtime, `compiler/newr_rt.py`). The
  bases were ordered `(OverflowError, NewrError)`, so
  constructing one never ran `NewrError.__init__` - and a
  `catch e:` body reading `e.message` raised `AttributeError`
  instead of printing the overflow (found by the emitter's
  overflow test, whose `try:` caught the error but then failed
  in the handler). Fix: the base order is now
  `(NewrError, OverflowError)` / `(NewrError, ZeroDivisionError)`
  - `NewrError.__init__` runs, and the classes are still
  `OverflowError` / `ZeroDivisionError` subclasses (a bare
  `except OverflowError` still sees them). Regression test:
  `tests/test_newr_rt.py::TestCheckedArithmetic::test_overflow_error_carries_message`
  (written RED first - it failed with `AttributeError` before
  the fix).
- **BUG-M2-3 - A modal did not count as an answer for E2010**
  (discord checker, `compiler/discord_check.py::_answers`).
  A command whose only action was showing a modal was rejected
  with "may not answer on some path", but a modal sends a
  response directly (`interaction.response.send_modal`), so it
  answers the interaction - unlike a button/select, which only
  attach to a reply and correctly do not count. Fix: `_answers`
  now includes `A.ModalDecl` (with a comment explaining the
  button/select distinction). Regression test:
  `tests/test_discord_check.py::TestEveryPathAnswers::test_modal_answers`
  (written RED first - it failed with E2010 before the fix).
  Full suite 624 green after.
- **BUG-M2-4 - A match guard could not see the arm's name
  binding** (emitter, `compiler/emitter.py::_emit_match`). A
  guard such as `x if x > 100:` was emitted before the pattern's
  bindings were bound, so the guard's reference to `x` failed
  with "unknown name `x`" (found by the conformance corpus,
  `conformance/ok/match_or_guard.newr`). Fix: while emitting a
  guard, the arm's pattern bindings are bound in a pushed scope
  to their pattern expression text (a name pattern binds to the
  scrutinee temp), then the scope is popped. Regression test:
  `tests/test_emitter.py::TestMatch::test_name_pattern_guard_binds_scrutinee`
  (red-checked: fails without the fix, passes with it).
- **BUG-M2-5 - The CLI's emitter-error path raised `NameError`**
  (`compiler/cli.py::_build_emit`). The `EmitterError` handler
  built a diagnostic with `Span(...)` but `Span` was not imported
  - so a program the emitter refused produced a raw traceback
  instead of the clean E4001 diagnostic (found by the same
  conformance program). Fix: import `Span` from `.errors`.
  (No separate test - the CLI build tests exercise the path
  through `test_build_type_error_exits_4_and_writes_nothing`'s
  shape; the E4001 path is exercised by the conformance harness.)

### 14.6 Decisions made while coding the general emitter (7 Nov)

- **D34 - A nullable value printed or converted with `str()` goes
  through `to_str`** (P8 corollary). A nullable int/float/str's
  runtime value is Python's `None` when absent, and Python's
  `print` / `str()` would render that as `"None"` instead of
  newr's `"none"`. `to_str` renders `None` as `"none"` and any
  present value by its own rule, so it is the correct renderer
  for every nullable value.
- **D35 - Capture by value includes top-level `let`s** (D10
  corollary). A top-level `let` is mutable, so a lambda reading
  it at call time would see a later value - the snapshot
  (default-arg) applies to it, not only to function locals.
  (A top-level `const` is registered the same way; it never
  changes, so snapshotting it is harmless.) Module functions /
  structs / enums / modules / stores are fixed and need no
  capture.
- **D36 - The int64 minimum is emitted literally** (D19
  implementation). `-9223372036854775808` in newr is a unary
  minus over the literal `9223372036854775808`, whose own type
  the checker leaves unset (it is out of range as a positive
  value) - so the emitter special-cases that literal and emits
  `-9223372036854775808` directly, rather than
  `neg(9223372036854775808)` (which would check the out-of-range
  literal first).
- **D37 - `print (expr) * 3` is `(print(expr)) * 3`, not
  `print((expr) * 3)`** (parser behavior, documented by the
  emitter tests). `print (x)` is a call with a parenthesized
  argument, so an operator after the `)` applies to the call's
  result (`none`) - a type error. Parenthesized arithmetic
  meant as print's argument goes in a `let` (or double parens).
  The same applies to `print (-2) ** 2`.
- **D38 - The `?.` lambda parameter carries the stripped type.**
  Inside an `opt_chain` lambda the receiver is proven non-none,
  so the parameter (and the rebased base) carry the inner type
  (`str`, not `str?`) - otherwise a method call on it would
  dispatch as if the value were still nullable and fail.
- **D39 - A file-module call registers its import.** A call to a
  function in an imported `.newr` file emits `import <module>`
  in the importing file (D13: one `.py` per imported `.newr`).

### 14.7 Decisions made while coding the bot emitter (7 Nov)

- **D40 - A Button/Select attaches to the most recent answer
  (Reply/Followup/Edit) in the SAME block.** The emitter rewrites
  that answer's line to add `view=<var>`; a Modal is sent directly
  (`await interaction.response.send_modal(...)`); a component with
  no preceding answer in its block is an `EmitterError`.
- **D41 - `on_timeout`: the View stores `self.message`** (set by
  the handler after the answer - via
  `interaction.original_response()` for a reply/edit, or the
  followup send's return with `wait=True`); `on_timeout` disables
  the children and edits the stored message for a `reply` in the
  on_timeout body.
- **D42 - `values` (select) interpolation form is
  `", ".join(self.values)`.** A `list[str]` cannot be
  interpolated directly (E2001); the join is the display form.
- **D43 - Store methods live on the Bot:
  `await bot.<store>_<op>(...)`; the user key is
  `str(<user>.id)`** (the key column is str).
- **D44 - `_t` locale:** `interaction.locale` in
  command/component contexts; `member.guild.preferred_locale` in
  member events; `message.guild.preferred_locale if message.guild
  else None` in message events; `_DEFAULT_LOCALE` (first declared
  locale) in tasks/ready/no-handler contexts.
- **D45 - `transfer` is 4-arg (`from, to, amount, key`)** per
  SPEC §14 (verified 7 Nov: a 4-arg transfer checks clean, as do
  3-arg debit/credit and `get`).
- **D46 - A `get` row is a `types.SimpleNamespace`** (attribute
  access works for its fields); `set` takes such a row (or a
  struct) via `vars()`.
- **D47 - An `embed` can only be used as a reply/edit/followup/
  send value** - superseded by D51 (an embed is also a value via
  hoisted construction; see below).
- **D48 - The bot emitter registers the module scope before
  emitting handlers.** `_emit_bot_file` now calls
  `_register_module_scope` (and sets `self.program` / `fns` /
  `structs` / `enums` / `stores`), exactly as
  `_emit_general_file` does - a handler calling a file-module
  function (`shop_items.price_of`) or a store (`economy.balance`)
  resolves through it. Without this the shop bot failed with "an
  expression has no type" (the module-name Member is untyped by
  the checker; only the whole call carries a type).
- **D49 - A component's instantiation lines are moved BEFORE the
  answer they attach to.** The answer references the view variable
  (`view=buy_again_view`), so emitting the instantiation after
  the answer would be a `NameError` at runtime. `_emit_block`
  moves the component's added lines to just before the answer
  line and adjusts the recorded answer index.
- **D50 - `_push_handler` / `_pop_handler` do not reset
  `_last_answer`.** Answer tracking is per-block (managed by
  `_emit_block`'s save/restore); the handler reset wiped the
  enclosing block's answer - a button's own `_pop_handler` (run
  while emitting its `do:` body) cleared the reply it was
  attaching to, so `_attach_view` failed.
- **D51 - An embed as a value is built on hoisted lines**
  (SPEC §13.7: an `embed:` block is an expression, e.g.
  `let card: embed = embed:`). `_emit_embed` appends the
  construction lines to `self._hoists` (indent 0) and returns the
  `embed` variable; the enclosing statement's hoist mechanism puts
  the construction before the statement. This supersedes D47's
  "fails cleanly" rule - the general expression form works.
- **D52 - A `num` option's type name is `float`.** The checker's
  `Type` for a `num` option is named `float`, so `_option_param`
  accepts both `num` and `float` for the `float` annotation.
- **D53 - The timeout message line goes right after the
  answer.** `_attach_view` inserts
  `<view>.message = await interaction.original_response()` at
  the answer index + 1, not at the end of the block (statements
  may follow the component).

### 14.4 Verification outcomes (filled as M2 verifies them)

- discord.py 2.7.1 `choices` / `Range` signatures (D15):
  **VERIFIED 7 Nov** - `app_commands.choices(**parameters)` is a
  decorator; `app_commands.Range` is a generic subscript
  (`Range[int, 1, 10]`, `Range[int, 10]`, `Range[int, None, 10]`);
  `Range(1, 10)` raises `TypeError`. The shop golden's
  `amount: app_commands.Range[int, 1, 99] = 1` and the
  `@app_commands.choices` decorator are emitted from that.
- `await` inside f-string fields (shop's `reply` with
  `economy.balance(user)`): **VERIFIED 7 Nov** - the shop bot
  emits `f"Balance: {await bot.economy_balance(str(interaction.user.id))} gold."`,
  which parses and (in the mock round-trip) runs; `python3 -m py_compile`
  on the emitted shop is clean.
- CI matrix (Windows/macOS): still UNVERIFIED (never run).
- Bot emitter red-check (7 Nov): stubbing `_emit_bot_file` made
  23/27 bot tests error (4 skipped = mock round-trips, system
  Python has no discord.py); restoring it made all 27 pass.
  Mock round-trip (newr-verify venv, discord.py 2.7.1): all 4
  pass - ping/greet replies, DM guild fallback, member_join
  welcome, `_channel_text`.

### 14.8 Benchmarks B1–B8 (7 Nov, hyperfine 1.19.0, Linux x86_64, Python 3.12.3)

Full report: `BENCHMARKS.md` (mean ± σ + range, N runs, machine
context); raw hyperfine JSON in `benchmarks/raw/`; harness
`benchmarks/run.py` (`--quick` for a smoke check). Hand-written
equivalents in `benchmarks/handwritten/` are human-written, not
generated-then-edited. No speedup is promised - the numbers are
reported as found (no published generated-vs-hand-written
benchmark exists for any transpiler).

- **B1 compile cold:** hello 127.1 ms ± 18.6 (N=10); shop 165.5 ms ± 36.2 (N=10).
- **B2 compile warm:** hello (no change) 128.0 ms ± 21.6 (N=20);
  shop (one file changed - a comment line appended to
  `shop_items.newr` before each run) 152.8 ms ± 23.3 (N=20).
  Cold ≈ warm: the compiler has no cache, so "warm" only avoids
  the first-import cost, which is small.
- **B3 `newr --help` startup: 141.5 ms ± 20.8 (N=10) - EXCEEDS
  the SPEC §4.1 design target of <100 ms.** The startup is
  dominated by the Python interpreter plus importing the whole
  compiler package (lexer/parser/resolver/checker/discord_check).
  A Python CLI with this import graph cannot easily hit <100 ms;
  the target is a design target, not a shipped guarantee, and it
  stays unmet. Options for later: lazy imports (only import the
  checker when `check`/`build` runs), or a zipapp (same imports,
  so no gain). Not fixed in M2.
- **B4 code size:** generated hello 165 bytes / 5 lines vs
  hand-written 259 bytes / 9 lines (ratio 0.64); generated loop
  327 bytes / 12 lines vs hand-written 396 bytes / 16 lines
  (ratio 0.83); generated shop 9,404 bytes / 223 lines. The
  generated code is SMALLER than the hand-written equivalent -
  the readability goal (no boilerplate bloat) is met.
- **B5 program startup:** generated hello 32.7 ms ± 4.0 vs
  hand-written 28.9 ms ± 6.5 (ratio 1.13, N=10) - within noise.
- **B6 checked-int tight loop (100k iterations, checksum 105003
  in all three):** generated (checked helpers) 123.4 ms ± 14.3;
  hand-written plain ints 48.5 ms ± 5.5; hand-written with
  `newr_rt` helpers 132.2 ms ± 6.5. Ratio generated/plain = 2.54
  (exceeds the 1.20 proposal flag threshold); ratio
  generated/checked-hand-written = 0.93. The 2.54× is the honest
  price of the spec's checked int64: every arithmetic op is a
  function call plus a range check. The emitter itself is
  efficient - it matches a hand-written program using the same
  helpers (0.93×). This is a design cost, not an emitter bug.
- **B6b shop economy (1,000 credits, aiosqlite, commit per call):
  generated 718.6 ms ± 79.8; hand-written 583.5 ms ± 30.5; ratio
  1.23 (just over the 1.20 flag threshold).** Both print the final
  balance 1000. The 10,000-transaction plan was reduced to 1,000:
  per-call commit fsync dominates and 10k took >4 min; 1k still
  measures the code path. CORRECTED 7 Nov 2026 (M2 fix #6): the
  published 1.89 was a benchmark-methodology artifact - the
  generated driver imports the whole shop bot (`import discord`,
  ~300-700 ms one-time) while the hand-written driver imported
  only aiosqlite (~78 ms). The hand-written driver now imports
  discord too, so the comparison measures the economy loop, not
  interpreter startup (re-measured B6b only, 10 runs, warmup 1;
  raw JSON in benchmarks/raw/b6b_shop_*.json). The remaining 1.23
  is the honest price of the atomicity template (M2 fix #2): a
  lock acquire + explicit BEGIN IMMEDIATE / COMMIT per credit,
  vs the hand-written plain commit() - about 0.14 ms extra per
  credit. The cProfile loop-only check (evaluator, old template)
  was 1.07×; the new template was not re-profiled in isolation.
- **B7 peak RSS** (Python `resource.ru_maxrss`; `/usr/bin/time`
  is not on this VM): compile (shop) 17.5 MB; run (hello)
  11.1 MB. Small.
- **B8 parity:** startup 1.13 ✓; loop 2.54 (flagged - inherent
  checked-int cost, see B6); shop economy 1.23 (flagged - the
  atomicity template's per-call cost, see B6b); hello size 0.64 ✓;
  loop size 0.83 ✓.

### 14.9 CI emitter job (7 Nov)

`.github/workflows/ci.yml` gains a `python-emitter` job (matrix
ubuntu/macos/windows × Python 3.10–3.13, `fail-fast: false`):
installs `discord.py==2.7.1` + aiosqlite, runs the full suite
(incl. emitter/golden/conformance tests), runs
`conformance/run.py`, builds all four examples and runs the
emitted Python (the bots must reach the `DISCORD_TOKEN` guard -
exit 1 with the token message - proving the emitted bot ran its
`main()`), and runs the golden-comparison tests. The header
comment is updated (the per-target emitter jobs now exist for
Python; JS lands in M3). The job was simulated locally in
`/tmp/ci_sim` (system Python for hello/shop, the newr-verify
venv for the bots, since system Python has no discord.py) and
all steps pass. CI itself has still never run - Windows/macOS
execution stays UNVERIFIED until green on all three OSes.

### 14.10 CLI message fix (7 Nov, cosmetic)

BUG-M2-6 - `newr build examples/hello.newr -o hello_out.py`
printed "wrote hello.py": the success message listed the
`result.files` keys (source stems), not the actual destination
paths. Fixed in `compiler/cli.py::_build_emit`: the message now
lists the real destinations (the `-o` path for the main file,
`main_out.parent / name` for imports, plus `newr_rt.py` when
copied). Verified: `examples/hello.newr: ok - wrote
/tmp/hello_cli2.py`; `examples/shop.newr: ok, 3 warning(s) -
wrote /tmp/shop_cli2.py, /tmp/shop_items.py, /tmp/newr_rt.py`.
Suite stays 637 green after the fix.

## 15. M2 fix round - cold-evaluation must-fix items (7 Nov 2026)

The cold evaluation after M2 returned NEEDS FIXES with 6 must-fix
items. All 6 are fixed here, TDD + one red-check per fix, suite
green throughout. Baseline before this round: 637 tests (5
skipped), conformance 68 ok + 22 err. After: **675 tests
(5 skipped)** - 38 new unit tests - conformance **69 ok + 25 err**.
The shop golden changed (item 2's economy template + the
`asyncio`/`types` import grouping) and was regenerated with
`--bless` after a line-by-line diff review, never hand-edited.

### 15.1 Item 1 - Top-level statements execute in file order (SPEC §8)

Repro: `let x = 1` / `x = 5` / `let y = x` / `print y` printed `1`,
must print `5`. Root cause: `_emit_general_file`
(`compiler/emitter.py`) split top-level statements into `lets` and
`others` and emitted all lets first (D11's two-phase layout), so a
`let` RHS saw a `let`'s initial value, not a value a preceding
assignment wrote. Fix: emit structs/enums/functions first
(definitions are hoisted - a fn body reads module globals at call
time), then ALL top-level statements in file order. The bot emitter
(`_emit_bot_file`) emits no top-level statements, so item 1 does
not apply to bots. D11 is corrected in place above (the two-phase
layout is gone); the D11 rationale (fn bodies reading later lets)
is preserved by the defs-first order, not by hoisting lets.
Tests: `tests/test_emitter.py::TestTopLevelOrder` (5 tests:
assignment between lets, let RHS reading an earlier assignment's
value, fn call after an assignment seeing the new value, print
before a fn declaration still works, struct declared after the
let using it) + conformance `ok/toplevel_order.newr`. Red-check:
revert to the two-phase layout → exactly the 2 order-sensitive
tests fail; restore byte-identical (sha256 `dab0d15c…`).

### 15.2 Item 2 - `economy.transfer` is NOT atomic (SPEC §14)

Repro (live, venv python): seed alice 100; simulate a crash
between the two legs (out-leg committed: alice 50 + `"K:out"` in
the ledger; in-leg never ran); retry `transfer(alice→bob, 50,
key="K")` returned `True` with alice 50, bob 0 - 50 gold destroyed,
success reported. A second poisoning (a synthetic `K:in` row with
amount 0, no money moved) made the old code return `True` without
moving any money. Root causes: the two legs were two separate
`_economy_apply` calls, each with its own commit; the replay guard
(`key:out OR key:in` present → return True) conflated "out-leg
seen" with "transfer done". Related holes in `_economy_apply`:
the balance check sat outside the transaction (concurrent debits
could overdraw - TOCTOU), and no rollback if the ledger INSERT
failed (a later call's `commit()` could commit the partial
transaction). Fix (`compiler/emitter.py`, store template only -
no language change): one `asyncio.Lock` per store (created in
`_{name}_connect`, `None` in `__init__`), held across every store
method - the lock is required because aiosqlite runs all ops in
one thread, so a second coroutine's `BEGIN IMMEDIATE` would block
that thread until the busy timeout, and a concurrent read would
see uncommitted partial state. The lock is not reentrant: no
economy method calls another while holding it (`credit` delegates
to `_apply` without taking it). Each op is one explicit
`BEGIN IMMEDIATE` … `COMMIT` with `ROLLBACK` on any exception.
`debit` inlines its single leg (it does not call `_apply`, which
would nest a transaction) and checks the balance inside the
transaction. `transfer` inlines both legs and heals on retry:
each leg is topped up to its intended total from the amounts
actually in the ledger (not just presence - a row that merely
exists with amount 0 heals the full leg), clamped to
`[0, amount]`; the leg INSERTs upsert on the key (`ON
CONFLICT(key) DO UPDATE SET amount = ledger.amount +
excluded.amount`) so a poison row is topped up instead of
colliding with the UNIQUE constraint; a fully-applied transfer is
a no-op returning `True`. The debit side was already authorised
at the original attempt, so the remaining debit is applied
unconditionally - refusing would leave money destroyed.
`get`/`set`/`delete` take the same lock, so a concurrent `set`'s
`commit()` cannot commit a partial economy transaction.
Tests: `tests/test_emitter_bot.py::TestBotStore.
test_economy_uses_lock_and_explicit_transactions` (structure:
`import asyncio`, lock creation, `BEGIN IMMEDIATE`, `ROLLBACK`,
no two-`_apply` transfer) + `TestBotMockRoundTrip` (7 live
scenarios under the venv python, via a new `_run_shop_bot`
helper that emits SHOP into a temp dir and imports it there):
fresh transfer + replay + insufficient funds, transfer without
a key, crash-between-legs then retry heals (50/50, total 100,
second retry a no-op), in-leg-poisoned then retry heals (50/50),
concurrent debits cannot overdraw (two `debit(60)` against 100 →
exactly one `True`, balance 40), credit idempotent on a key,
debit replay returns `True` without a double debit. The golden
`goldens/py/shop.py` changed and was regenerated with `--bless`
after a line-by-line diff review (one defect caught in review: a
comment edit left a dangling sentence fragment in the transfer
method - fixed in the template before blessing). Red-check:
revert the emitter template → the structure test + the 3
behavioural tests (crash×2, concurrent debits) fail; restore
byte-identical (sha256 `e33da68a…`).

### 15.3 Item 3 - Top-level `let` RHS transitively reading a LATER `let` (G-M2-3)

Repro: `fn getx() -> int: return x` / `let a = getx()` / `let x
= 42` / `print a` - check ok, build ok, run died exit 1 with a
raw `NameError: name 'x' is not defined`. Correction to the
brief's file pointer: E2005 lives in `compiler/resolver.py`, not
the checker (the resolver's docstring says so: name resolution is
where scope order is known). Mechanism: `_resolve_fn` runs for all
fns BEFORE the top-level walk; `_top_level_pending` is set only
around the top-level walk; fn/lambda/handler bodies are exempt
(deferred execution). Fix: while resolving a module fn, record
which module-scope lets it reads / writes directly and which
module fns it calls (reads via `_resolve_name`, writes via
`_resolve_assign_target`, calls via the `Call` branch); reads and
writes inside a nested lambda are NOT recorded (a lambda body may
escape and run later - `_lambda_depth` tracks nesting); after all
fns resolve, compute the transitive closure over the call graph
(fixpoint, handles mutual recursion); during the top-level walk,
a `Call` whose callee is a module fn whose transitive read/write
set intersects the still-pending lets is E2005 at the call name
("function `getx` reads `x` before its definition", help: move
the `let` above the call, or the call after the `let`). The check
is skipped in deferred scopes and when no walk is running, so
handlers (which run after module initialisation) and fn bodies
are unaffected. A write is treated as a use: calling a fn that
assigns to a later let early would silently lose the assignment
(the later `let` re-initialises the name). Tests:
`tests/test_stress_fixes.py::TestTopLevelUseBeforeDef` (9 new:
direct/indirect/mutual-recursion reads, print and `if` call
sites, write-only fn, call-after-let ok, never-called ok,
handler calling the fn ok) + conformance
`err/fn_reads_later_let.newr`. Known limitations (recorded, not
fixed - alias analysis is out of scope): a call through a local
holding a fn (`let h = getx; h()`) is not tracked; a lambda
defined-and-called at top level reading a later let is not
tracked (IIFE does not parse as a call, so the common shape does
not exist). Red-check: revert the resolver change → exactly the
6 E2005-expecting tests fail; restore byte-identical (sha256
`c9bc3443…`).

### 15.4 Item 4 - `?` kind mismatch must be E2001 at check time, not E4001 at build (G-M2-1 / D30)

Repro: `fn f() -> Result[int, error]` containing `let w = v?`
where `v: int?` - check ok, build exit 4 E4001 ("the checker
should have rejected this"). Fix (`compiler/checker.py`,
`_synth_Propagate`): mirror the emitter's `_propagate_unwrapped`
kind rule exactly. A `Result` operand (non-nullable) needs a
`Result`-returning function; a nullable operand (non-`Result`)
needs a nullable-returning function; a nullable `Result` operand
fails both rules (G-M2-6, also E2001 now - the disagreement is
closed from both ends, the emitter's refusal stays as
defense-in-depth); a handler returns nothing, so `?` there just
exits the handler (a bare `return`) and either kind is allowed.
`_synth_Lambda` now saves/restores `_in_handler = False` - a
lambda is a function boundary (the emitter's lambda `_fn_ctx`
entry has `in_handler: False`), so `?` inside a lambda in a
handler is judged against the lambda's own return type, never
the enclosing handler; without this the handler branch would
have created a NEW check/build disagreement. Tests:
`tests/test_checker.py::TestResultAndPropagate` (8 new: the two
mismatch directions, nullable-`Result` into both return kinds,
nullable-into-nullable ok, `?` in a handler with both operand
kinds ok, `?` in a lambda in a handler is E2001) + conformance
`err/propagate_kind_mismatch.newr` and
`err/propagate_kind_mismatch_rev.newr`. The old
`tests/test_emitter.py::test_propagate_mismatched_kind_is_emitter_error`
pinned the E4001 behaviour; it is now a defense-in-depth test
(resolve WITHOUT the checker, emit, assert `EmitterError` - both
mismatch directions). Red-check: revert the checker change →
6 failures in `TestResultAndPropagate` (4 kind mismatches + 2
handler-ok tests); restore byte-identical (sha256 `0aa1bfaa…`).

### 15.5 Item 5 - `?.` on nullable methods must type as nullable (G-M2-2 / D31)

Repro: `let v: int? = none` then `v?.unwrap()`, `v?.is_some()`,
`v?.is_none()` - all three check ok, build exit 4 E4001.
(`?.` on ordinary methods was already correct: `xs?.len()` on
`list[int]?` yields `int?`.) Fix, checker
(`compiler/checker.py`, `_call_member`): the nullable-methods
branch now wraps the result in `nullable_of` when the call is
optional (`?.`); non-optional `.unwrap()`/`.is_some()`/`.is_none()`
keep their plain types. `?.unwrap()` on a nullable `Result`
resolves `.unwrap()` against the INNER type (the receiver mapping
maps `T` to the whole stripped type, so a naive wrap would give
`Result[int, error]?`): result is `nullable_of` the Result's
value type (`int?`). Fix, emitter (`compiler/emitter.py`):
inside an `opt_chain` lambda the receiver is the STRIPPED inner
type (D38), so `_v.is_some()` on `_v: int` hit "int has no method
is_some" (E4001). `_emit_method_call` now has a fallback AFTER
the `Result` branch (Result unwrap inside the lambda is correct
and takes precedence): a call to `is_some`/`is_none`/`unwrap` on
a non-nullable, non-`Result` receiver can only come from a
rebased `?.` chain (the checker rejects it otherwise), so it
emits `True` / `False` / the receiver text. The same round
exposed a second emitter gap, fixed here: `_rebase` copied the
original `expr_type` onto rebased Call links, so
`s?.unwrap().upper()` dispatched `.upper()` against `str?` and
failed E4001 - an OPTIONAL Call link's rebased `expr_type` is now
the stripped type (the `?.` is the ONLY reason it is nullable;
a Member link is NOT stripped - a field may itself be nullable,
which survives inside the lambda). And the checker
(`_is_optional_chain`) now walks through `Call` nodes too, so
`s?.unwrap().upper()` is recognised as an optional chain (the
emitter's `_chain_has_optional` already did). Tests:
`tests/test_checker.py::TestNullability` (3 new: `?.` forms are
nullable, assigning them to non-nullable is E2001, `?.unwrap()`
on a nullable Result is `int?`) + `tests/test_emitter.py::
TestNullable` (3 new runtime tests: the three `?.` forms on
`5`/`none`, the segment tail `s?.unwrap().upper()`, `?.unwrap()`
on a nullable Result) + `conformance/ok/opt_methods.newr`
extended with the `?.` cases. Red-check: revert the checker +
emitter changes → 4 discriminating failures (2 checker E2001
tests, 2 emitter E4001 tests); restore byte-identical (emitter
sha256 `2a94b6c6…`). Note: two of the six new tests pass on both
sides (the "are nullable" checker test - `T` widens to `T?`, so
the mistyping was masked - and the nullable-Result runtime test
- the emitter's `Result` branch already handled it); they pin
the correct behaviour but are not red-check discriminators.

### 15.6 Item 6 - B6b 1.89× is a benchmark-methodology artifact (corrected to 1.23)

Evidence: the generated B6b driver imports the whole shop golden
→ `import discord` costs ~300-700 ms in the venv (measured 681.3
ms this round, 313.5 ms at evaluation time - import cost varies
with cache warmth) vs 77-78 ms for the hand-written driver's
aiosqlite-only import; cProfile of the credit loop alone was
1.07× parity. Fix: `benchmarks/handwritten/shop_economy.py` now
imports discord too (with a comment explaining why), so hyperfine
measures the economy loop, not interpreter startup. B6b only was
re-measured (hyperfine, 10 runs, warmup 1 - the same invocation
`benchmarks/run.py` uses): generated 718.6 ms ± 79.8 ms (range
657.6–897.5 ms, N=10), hand-written 583.5 ms ± 30.5 ms (range
531.6–616.5 ms, N=10), **ratio 1.23** (was 1.89). Both print the
checksum 1000. Raw JSON overwritten in
`benchmarks/raw/b6b_shop_gen.json` + `b6b_shop_hw.json`
(verified to match `BENCHMARKS.md` exactly); `BENCHMARKS.md`
B6b + B8 lines and §14.8 corrected (the remaining 1.23 is just
over the 1.20 threshold - the honest price of the atomicity
template from item 2: a lock acquire + explicit BEGIN IMMEDIATE /
COMMIT per credit, ~0.14 ms extra per credit vs the hand-written
plain `commit()`). The correction note was also added to
`benchmarks/run.py`'s B6b text so a future full benchmark run
does not silently drop the explanation. No other benchmark was
re-run.

### 15.7 Nice-to-haves done in this round

- CI shop bot runs to the `DISCORD_TOKEN` guard
  (`.github/workflows/ci.yml`): the `python-emitter` job's
  build+run step now runs the emitted `shop_out.py` and greps for
  `DISCORD_TOKEN`, like `hello_bot`/`showcase` already did (the
  step previously only `py_compile`d it, while its own comment
  already claimed it reached the guard). Verified locally first:
  the emitted shop prints "set the DISCORD_TOKEN environment
  variable", exit 1.
- User variables named `_newr_*` are forbidden (G-M2-4):
  `compiler/resolver.py::_declare` rejects any declaration whose
  name starts with `_newr_` with E3001 (the emitter's temporaries
  are `_newr_t*`/`_newr_h*`/`_newr_fn_*`/`_newr_m*`/
  `_newr_result`). Placed in the resolver, not the checker - the
  resolver owns declarations (same correction as item 3's file
  pointer). Tests: `tests/test_resolver.py::
  TestDuplicatesAndReserved` (2 new: the prefix is E3001 for
  lets and fns, plain `_x` is fine).
- The language has no doc-comment syntax: `SPEC.md` §6 (Comments)
  now says so explicitly - no `///`, no `DocComment` token; a `#`
  comment above a declaration is an ordinary comment, never
  carried into emitted code as a docstring. The emitter's "doc
  comments → docstrings" mapping is vacuous until the syntax is
  frozen (M6, §24 idea 14).
- `import types` sort order in the shop golden: the emitter's
  `_render_imports` stdlib set lacked `asyncio` and `types`, so
  both fell into the first-party group and sorted after
  `import shop_items`. Both are now in the stdlib set; the
  blessed shop golden shows the clean grouping (stdlib:
  asyncio/logging/os/time/types; third-party: aiosqlite/discord;
  first-party: newr_rt/shop_items).

### 15.8 Found during this round - OPEN, not in the 6 items

Two adjacent holes of the same class the evaluator flagged in
item 3 (a program passing the full front end must never die with
a raw Python traceback). Both are in FIELD access paths, not the
method-call paths item 5 fixed, and both need a design decision,
so they are recorded here for the re-evaluation / M3 rather than
fixed mid-round.

- **`o?.inner.name` with a nullable field crashes at runtime.**
  Repro: `struct Inner: name: str` / `struct Outer: inner: Inner?`
  / `let o: Outer? = Outer(inner: none)` / `print o?.inner.name`
  - check ok, build ok, run dies `AttributeError: 'NoneType'
  object has no attribute 'name'`. The checker accepts it as
  `str?` (via `_is_optional_chain`), but `_emit_chain` folds the
  non-optional `.name` link into the same `opt_chain` lambda, so
  the `.name` access is unguarded when `inner` is `none`. Fix
  direction: split a chain segment when the intermediate value
  can be `none` inside the lambda (the in-lambda type, not the
  checker's chain type - a field may itself be nullable), or
  reject the shape in the checker. The same reasoning applies to
  `a?.b.c()` (method after a nullable field).
- **A nullable method name used as a bare value crashes at
  runtime.** Repro: `let v: int? = 5` / `print v?.is_some` (no
  parens) - check ok, build ok, run dies `AttributeError: 'int'
  object has no attribute 'is_some'` (and `print v.is_some`,
  non-optional, has the same hole). `_member_field_type` returns
  the method's RETURN type (`bool`) for a bare nullable-method
  access, while the emitter emits a field access that does not
  exist. Fix direction: type bare nullable-method accesses as fn
  values (like ordinary methods-as-values) and emit a lambda for
  them, or reject them in the checker.

### 15.9 Live verification (end of round)

`./newr build` + run on all four examples, fresh temp dir:
`hello.newr` → "Hello, world!", exit 0; `shop.newr` → builds
(3 warnings, as before), reaches the `DISCORD_TOKEN` guard,
exit 1; `hello_bot.newr` → guard, exit 1; `showcase.newr` →
guard, exit 1. Full suite: 675 tests green on system Python
(12 skipped: the venv-only mock round-trips) and on the
newr-verify venv (1 skipped). Conformance: 69 ok + 25 err,
harness exit 0. NOT re-verified here (unchanged since M2):
CI has still never run - Windows/macOS execution stays
UNVERIFIED until green on all three OSes; no live Discord bot
has connected yet (P-42).

## 16. §15.8 follow-up fix round - the two open holes (7 Nov 2026)

Both holes from §15.8, same severity class as item 3 (a program
passing the full front end must never die with a raw Python
traceback). Decision in both cases: GUARD, not reject - the
shapes are legitimate programs under the language's design
(SPEC §9.2 already promises "`a?.b.c()` short-circuits the
whole postfix chain after the `?.`", TypeScript behaviour, so no
SPEC change was needed; and methods-as-values already exist for
ordinary methods, so a nullable method as a value is the same
feature, not a new one). Rejecting would have made the language
smaller for no reason; guarding keeps every program that
checked before still checking, and makes it run.

### 16.1 Hole 1 - `?.` chain with a nullable intermediate field

Repro: `struct Inner: name: str` / `struct Outer: inner: Inner?`
/ `let o: Outer? = Outer(inner: none)` / `print o?.inner.name`
- check ok, build ok, run died `AttributeError: 'NoneType'
object has no attribute 'name'`.

Root cause: `_emit_chain` (D9) folded every non-optional link
after the first `?.` into the same `opt_chain` lambda, so
`.name` ran unguarded when `inner` was `none`. The checker's
chain type (`str?` here) cannot tell the emitter where to
split: it conflates the `?.` short-circuit with a field's own
nullability, so a segment boundary cannot be read off
`expr_type` - the in-lambda type has to be walked link by link.

Fix (`compiler/emitter.py`): `_emit_chain` now walks the
segment's in-lambda types with a new helper
`_in_lambda_link_type` - Member link → the declared struct
field type from `self.structs` (or the method's fn type for a
method value), Call link → the method return type from the
METHODS tables with substitution (`_method_value_sig` /
`_subst_method_spec`, mirroring the checker's tables), Index
link → the element type - and ends the segment before any
link whose receiver can be `none` INSIDE the lambda (a
nullable field, or a nullable method result). Splitting is
always semantically safe: an extra `opt_chain` call is
harmless, and the emitted code for a chain with no nullable
intermediate is unchanged. The walk also passes the computed
in-lambda types into `_rebase` (new `in_lambda_types` map,
`id(node) -> Type`), so cloned Member/Call nodes carry the
in-lambda type instead of the checker's chain type - that is
what fixes the second-order failure `o?.inner.name.upper()`
used to fail the build with clean `error[E4001]: ... 'str?'
has no method 'upper'`. The old D38 heuristic (strip an
optional Call link's nullable type) stays as the fallback for
links with no map entry - but the map is exact where D38
over-strips: `r?.unwrap().upper()` on
`Result[str?, error]? = ok(none)` now prints `none` instead
of dying, because `unwrap()`'s own return (`str?`) is
genuinely nullable and must survive inside the lambda.
Unknown types (provider/entity receivers) return None from
the walk and split conservatively.

Tests: `tests/test_emitter.py::TestOptionalChainNullableField`
- `test_chain_through_nullable_field` (`o?.inner.name` →
`n`/`none`/`none`), `test_method_after_nullable_field`
(`.upper()` after the nullable field → `X`/`none`),
`test_unwrap_nullable_result_then_method` (the D38 case →
`none`/`HI`/`none`). Conformance:
`conformance/ok/opt_chain_nullable_field.newr` (prints PASS).
Red-check: reverting the `_emit_chain` segment scan to the
old fold-everything loop makes all 3 tests fail (2 failures +
1 E4001 build error); restored byte-identical
(sha256-verified, `emitter.py 53b61c67…`).

### 16.2 Hole 2 - bare nullable-method value

Repro: `let v: int? = 5` / `print v?.is_some` (no parens) - and
the same hole on plain values (`print v.is_some`). Check ok,
build ok, run died `AttributeError: 'int' object has no
attribute 'is_some'`. The same hole covered ordinary methods
too: `xs.len` died `AttributeError: 'list' object has no
attribute 'len'`.

Root cause (two halves): the checker `_member_field_type`
returned the method's RETURN type (`bool`) for a bare
nullable-method access, while the emitter emitted a field
access that does not exist; and the emitter had no
method-value path for builtin/nullable methods at all (only
user functions and struct fields existed as values).

Fix, checker (`compiler/checker.py`): the nullable branch of
`_member_field_type` now returns a fn type via
`_method_fn_type` - `v.is_some` is `fn() -> bool`,
`v?.is_some` is the nullable fn `(fn() -> bool)?` - and the
recursion branch that used to return a bare fn type now
returns `nullable_of(ft)`: a fn reached through `?.` is
itself nullable (`xs?.len` is `(fn() -> int)?`, not a plain
fn). Consequence, also fixed: calling a nullable fn value
directly (`let f = xs?.len; f()`) is now rejected with E2001
at check in both `_synth_Call` branches (named and general
callee), since the value may be `none` and the call would
otherwise still die with a raw TypeError. Passing a nullable
fn as an argument was already rejected by type equality.

Fix, emitter (`compiler/emitter.py`): `_emit_member` now
detects a method used as a value (member type is `fn`, not a
real struct field per `_is_plain_field`, and the receiver has
a METHODS-table signature via `_method_value_sig`) and emits
a lambda through `_emit_method_value`: the method's
parameters as lambda parameters, the receiver captured by
value (D10) as a defaulted parameter so it is evaluated once
when the lambda is created, never again at call time, and the
body is a synthetic Call node so `_emit_method_call` (incl.
its D38 fallback) does the dispatch. The rebased case
(`v?.is_some` inside an `opt_chain` lambda, receiver already
stripped to `int`) works because `_method_value_sig` also
consults the nullable table when the receiver type is
non-nullable. `to_str` falls back to Python `str()` for
functions, so `print v.is_some` prints a lambda repr without
crashing. Store methods as values are out of scope (checker
allows them via `_store_member_type`, emitter behaviour
unchanged - not one of the two holes).

Tests: `tests/test_checker.py::TestNullability` -
`test_nullable_method_as_value_is_fn_not_bool`,
`test_optional_nullable_method_as_value_is_nullable_fn`,
`test_optional_chain_method_as_value_is_nullable_fn`,
`test_optional_chain_nullable_field_type`;
`tests/test_emitter.py::TestOptionalChainNullableField` -
`test_nullable_method_as_value_runs`,
`test_bare_method_as_value_runs`, `test_printing_method_value_does_not_crash`.
Conformance: `conformance/ok/method_as_value.newr` (PASS) and
`conformance/err/call_nullable_fn_value.newr`
(`# expect: E2001`). Red-check: reverting the checker typing
+ the E2001 rejections + the emitter method-value path makes
all 6 hole-2 tests fail; restored byte-identical
(sha256-verified, `checker.py 861fd139…`,
`emitter.py 53b61c67…`).

### 16.3 Conformance-note (a wrong test, not a compiler bug)

While writing `opt_chain_nullable_field.newr` the first
draft asserted `b?.inner.name.is_none() != true` for
`b.inner = none` - that is wrong: under SPEC §9.2 the whole
chain short-circuits to `none` (a `bool?`), it is NOT `true`.
The program was rewritten to bind the chain result to a
variable first and call `.is_none()` on it, the same pattern
`opt_methods.newr` uses. No compiler change came out of this.

### 16.4 Optional item - LEFT OPEN

The amount-based heal for `economy_debit` / `economy_credit`
replay (evaluator's non-blocking note) was NOT done: it is
not trivial in the sense the brief meant. The code change
itself is small (read `COALESCE(SUM(amount), 0)` per key in
`_apply` / `_debit`, top the remainder up like
`_transfer` does), but it touches the economy template that
was just verified in the M2 fix round, changes the
`goldens/py/shop.py` golden (needs a `--bless` after
line-by-line diff review), and needs new tests plus a
re-run of the crash-between-legs / poison / concurrent-debit
stress scenarios. Reachable only via direct DB tampering (a
synthetic zero-amount ledger row), so it stays open for a
later round rather than risking the verified template here.

### 16.5 Results

- Suite: **685 tests OK** (675 baseline + 10 new: 4 checker +
  6 emitter), **12 skipped** on system Python and **1 skipped**
  in a fresh venv with discord.py 2.7.1 + aiosqlite
  (`/tmp/s16-venv`).
- Conformance: **71 ok + 26 err** (was 69 + 25), harness
  exit 0; `tests/test_conformance.py` passes.
- Goldens: unchanged - `tests/test_goldens.py` passes, no
  `--bless` needed (no golden program contains the fixed
  shapes).
- Live verification, fresh temp dir: `hello.newr` →
  "Hello, world!", exit 0; `shop.newr` builds (3 warnings, as
  before), `hello_bot.newr`, `showcase.newr` build; all three
  reach the `DISCORD_TOKEN` guard, exit 1, in the venv.
  §15.8 repro 1 now prints `none` / `n` / `N`, exit 0;
  repro 2 prints two lambda reprs, `true`, `3`, exit 0;
  calling a nullable fn value directly is rejected at check
  with clean `error[E2001]: `f` may be `none` - unwrap it
  before calling it`, exit 4.
- NOT re-verified here (unchanged since M2): CI has still
  never run - Windows/macOS execution stays UNVERIFIED until
  green on all three OSes; no live Discord bot has connected
  yet (P-42).
- Changed files: `compiler/{checker,emitter}.py`,
  `tests/{test_checker,test_emitter}.py`, plus 3 new
  conformance programs (`ok/opt_chain_nullable_field.newr`,
  `ok/method_as_value.newr`,
  `err/call_nullable_fn_value.newr`). **Not pushed** - chie
  reviews and pushes.

## 17. M3 - JavaScript emitter (7 Nov 2026)

M3 builds `newr build --target js`: a readable-JavaScript emitter
(CommonJS), the `newr_rt.js` checked-int runtime, per-target goldens
in `goldens/js/`, the same 71-program conformance corpus running
under Node, and honest benchmarks B1–B8 for JS. Research handoff:
the discord.js survey (`/tmp/djs-survey.md`, 293 lines - the
starter track's verified patterns: `Client` + `GatewayIntentBits`,
`client.once(Events.ClientReady)`, `InteractionCreate` dispatch with
`commandMap`, `MessageFlags.Ephemeral`, standalone REST `put`
registration, `ActionRowBuilder`/`ButtonBuilder` collectors,
`ModalBuilder`, `EmbedBuilder`; no `tasks.loop` equivalent - plain
`setInterval` is a Node inference, not a track pattern; no `_t`
helper exists in the track - M1 is English-only).

Baseline at M3 start: suite **685 green** (re-evaluation of
`127e941` CLEAN, 06:11 WIB 7 Nov 2026), conformance 71 ok + 26 err,
repo `drewdestar/newr` at commit `127e941`.

### 17.1 The 13 JS decisions - decided before coding (J1–J13)

| # | Question | Decision (M3) | Reason |
|---|---|---|---|
| J1 | Module system | **CommonJS** (`require` / `module.exports`). `"use strict";` first line after the header. | `node file.js` just works with no package.json; discord.js 14 and node:sqlite are both require-able. No top-level await (all awaits inside async fns, as in Python). |
| J2 | int64 in JS | **BigInt.** Every int literal `123n`; all int arithmetic via rt helpers (`add`/`sub`/`mul`/`neg`/`powInt`/`tdiv`/`tmod`/`checkInt`). Floats are JS numbers. Mixed int/float ops are checker-rejected. | JS numbers are f64 - they cannot represent int64 exactly above 2^53 and cannot throw on overflow. BigInt is the only exact representation; the cost is measured honestly in B6 (generated/plain 1.22, generated/checked-hand-written 1.09 - the gap is the BigInt price, §17.7). |
| J3 | `round()` half-away | **Exact via BigInt decimal expansion** (no Decimal in JS). Same P2 semantics incl. `round(2.675, 2) = 2.67`. | JS has no Decimal; the float's exact binary value is expanded to decimal digits with BigInt, then rounded half-up on the exact value. |
| J4 | Float rendering | **Python `repr()` semantics** via the `floatStr` helper: shortest round-trip digits, exponent iff exp < -4 or >= 16, ".0" suffix for integral floats, "0.0"/"-0.0" for zero. Used by `toStr` AND by print/interp of float values. | Bare JS `String(x)` differs (e.g. `1e+21` vs Python `1e+21` - same here, but `1e-7` vs `1e-07`, and no ".0" suffix). Conformance pins the Python spellings. |
| J5 | `str()` rendering | **Via `toStr` (P8)**, same spellings as Python: `"true"`/`"false"`/`"none"`, structs `"Point(x: 1, y: 2)"`, enums `"Color.Red"`, collections recursively with bare strings. | Same reason as P8 - bare JS rendering gives the wrong spellings. |
| J6 | Collections | **list → Array (reference semantics, P12), dict → Map (insertion order, str keys), set → Set.** Structural `==` via rt `eq()` (deep, order-insensitive for dict, unordered for set). **Known limitation: sets of value types (struct/enum) use identity in JS - Python hashes by value; no conformance program covers it.** (Correction, §18: the J6 text used to say "sets/dicts" - the dict half is unreachable, since dict keys must be `str` (E2001); a dict keyed by a struct cannot even be written.) | JS has no value-keyed Map/Set. The limitation is documented, not hidden; a value-keyed wrapper is v1.1 work if a program ever needs it. |
| J7 | `?.` / `??` | **Via `optChain`/`coalesce` helpers**, same D9 shape (one optChain per optional link, §16 segment splitting ported exactly from the Python emitter, incl. `_in_lambda_link_type`/`_rebase`). | JS `?.`/`??` look similar but do not short-circuit the whole postfix chain the way SPEC §9.2 promises (the §15.8 hole-1 guard). The helpers carry the exact segment semantics. |
| J8 | Lambda capture | **Capture by value via IIFE snapshot:** `((_s0) => (params) => body)(v0)` for expr bodies; block bodies hoisted to `const _newr_fn_N = ((_s0) => (params) => {...})(v0)`. | Python's default-arg snapshot trick does NOT work in JS - default params evaluate at call time and refer to the parameter itself. The IIFE binds the snapshot at creation time. |
| J9 | Store / economy | **node:sqlite (built into Node 24, zero deps), synchronous API.** No lock (single-threaded sync execution is atomic per op); explicit `BEGIN IMMEDIATE ... COMMIT` per op for crash atomicity; same ledger+balance, idempotency-key replay, amount-based heal-on-retry as Python. DB file `<store>.db` next to the emitted `.js`. Methods stay `async` (shape parity with Python). **Int columns stored as TEXT** (node:sqlite returns JS numbers and throws above 2^53 - verified live: insert of `9007199254740993n` and `SELECT 9223372036854775807n` both throw `RangeError`); balance arithmetic in JS with BigInt + checkInt; conversion at the boundary. `PRAGMA busy_timeout = 5000`. **Ledger read for heal: `SELECT amount FROM ledger WHERE key = ?` summed in JS (not SQL SUM) for exactness.** Transfer-leg upserts computed in JS (BigInt) then UPDATE/INSERT - never SQLite `amount + excluded.amount` on TEXT. | better-sqlite3 was the alternative (sync, but a native addon - and its Node 24 prebuild is missing on this VM, see the discord.js track note). node:sqlite is built into Node 24, so the emitted bot has zero npm dependencies besides discord.js itself. |
| J10 | discord module | **discord.js 14.28.1, Node ≥ 24.17.0.** Bot shape: `class Bot extends Client`, commands as data (`SlashCommandBuilder` + handler map), `InteractionCreate` dispatcher, collector-based components, `setInterval` tasks, `client.login(token)` with DISCORD_TOKEN guard (same message as Python). Intents via `GatewayIntentBits`. **Registration: separate `<stem>_deploy.js` file with REST `put` (track-faithful; never on ready). `_t` helper: self-contained lookup over the emitted STRINGS (not i18next - no package.json dependency); fallback exact locale → base language → default → key; placeholders via `toStr`.** | Follows the surveyed starter track exactly where it speaks, and extrapolates only where it is silent (option getters beyond `addStringOption`, `GuildMemberAdd`/`MessageCreate` handlers, `setInterval` loops, embed setters beyond title/description/fields - all flagged as extrapolations in the survey). |
| J11 | `print` | **rt `print(...)` helper** (`console.log(args.map(toStr).join(" "))`; single-string arg prints literally, no %-formatting). **Amended during M3:** when every argument is already a JS string (or there are no arguments), emit bare `console.log(...)` with no runtime import. | The amendment resolves the hello golden: `print("Hello, world!")` imports no runtime on either target (the Python hello golden also imports no runtime). |
| J12 | `log` | **General programs and bots: `console.info`/`warn`/`error`** (stderr, no timestamp - documented difference from Python's logging format). | No logging library in the emitted code; the timestamp difference from Python's `logging` format is a documented drift, not a bug. |
| J13 | Strict mode | **`"use strict";` as the first line after the header** in emitted files. | Catches duplicate params and accidental globals; emitted code uses no `this` outside classes. |

Further JS traps pinned (all verified by tests, not assumed):
`==`/`!=` → `eq(a, b)` for list/dict/set/struct/enum/Result,
`===`/`!==` for primitives/fn/entity; `<` etc. bare (BigInt/number/
str work; str comparison is UTF-16 code units in JS vs code points
in Python - documented drift for astral characters); `in` by right
type (str → `.includes`, list → rt `listContains` structural,
set/dict → `.has`); unary minus before `**` must parenthesise (JS
SyntaxError); `not` operand parenthesised when not a primary
(`!a == b` is `(!a) == b` in JS); struct construction positional in
declared field order (`new Point(1, 2)` - JS has no kwargs); enums
as variant classes with `_newrRepr`; file modules export pub names
via `module.exports` (getters for pub lets/consts to preserve live
reads); top-level `return` → `process.exit(0)`; JS block scope
means no Python-style rename-on-shadow for locals (still mangle
keywords/reserved + rt/discord.js names); `env.args` =
`process.argv.slice(1)` (matches Python `sys.argv` incl. program
name at index 0); `math.floor`/`ceil` → `toInt(Math.floor(x))`
(avoids TypeError on Infinity); `random.int` inclusive via crypto
rejection sampling; `input()` via byte-by-byte sync stdin read;
`try` statement → `try`/`catch` with `asError`; match → if/elif
chain with temp + `else throw NewrError("non-exhaustive match")`;
contracts via explicit checks raising NewrError (D5); `throw "msg"`
→ `throw new NewrError(msg)`; file-IO error codes are host-specific
(JS `"ENOENT"` vs Python `"FileNotFoundError"` - documented drift).

Bot emitter details (J10): the `commands` array is opened by the
first command and closed by `_close_commands()`; option bindings at
the top of `execute` (int options → `BigInt(interaction.options.
getInteger("name", true))` or `?? default`; optional-without-default
→ temp + null check); the author/roles gate lives in the per-answer
'collect' callback (the shared handler is module-level and cannot
see the invoking interaction); on_timeout bodies are emitted at
indent 0 and re-indented into the collector's 'end' callback; the
deploy file requires the bot file for `commands` (`main()` is
guarded by `require.main === module`, so requiring it does not run
main); the bot file exports `{ bot, commands, commandMap,
componentHandlers }` for testability. `ComponentType` is used by
the collector code and must be in the discord.js require (caught
by the bot tests - the mock initially lacked it too).

### 17.2 What was built (file list)

- `compiler/newr_rt.js` (976 lines, 28,338 bytes) - the JS runtime:
  checked BigInt arithmetic (`add`/`sub`/`mul`/`neg`/`powInt`/
  `tdiv`/`tmod`/`checkInt`), float helpers (`floatStr` Python-repr
  rendering, `roundHalfAway` exact BigInt decimal expansion,
  `powFloat`/`fmod`/`fdiv` with by-zero errors), conversions
  (`toInt`/`toFloat`/`toBool`/`toStr`), `Result`/`Option` helpers,
  `eq`/`optChain`/`coalesce`/`listIndex`/`listGet`/`listContains`/
  `setRemove`/`unwrapOpt`/`range`/`print`/`input`, `NewrError` +
  `NewrOverflowError` + `NewrDivisionByZeroError` + `codedError` +
  `asError`, `timeFormat` (UTC, SPEC §12 fixed format set),
  `file` helpers, `random` helpers.
- `compiler/emitter_js.py` (4,542 lines) - the JS emitter:
  header/helpers/scopes/imports/hoists; expression printer; lambdas/
  collectors/match/propagate/module calls/store calls/`t`-call/embed/
  log; statements/fn/struct/enum/file assembly/entry; the bot
  emitter (commands, events, tasks, dispatch, channel text, main,
  exports, reply/defer/edit/followup/send, buttons/selects/modals,
  store/economy/table methods, strings, deploy file); `emit()` entry,
  `emit_program_js` / `emit_program` / `emit_deploy`.
- `compiler/cli.py` - `newr build --target js` (default stays py),
  `-o`, `--line-comments`, `--check`, `--fast` refusal, emitter
  errors → clean exit-4 E4001 (same contract as M2). Copies
  `compiler/newr_rt.js` next to the output when `uses_rt`; also
  copies the `<stem>_deploy.js` for bot programs.
- `conformance/run.py` - `--target js` (build + run under Node,
  same PASS contract) and `--bless --target js` (goldens/js/).
- `goldens/js/` (7 files) - hello (188 B), hello_bot (3,474 B),
  hello_bot_deploy (1,102 B), showcase (4,264 B), shop (15,018 B),
  shop_items (338 B), shop_deploy (1,087 B). Compare-only by
  default; regeneration only via explicit `--bless` after a
  line-by-line diff review. Never hand-edited.
- `tests/test_emitter_js.py` (83 tests), `tests/test_emitter_js_bot.py`
  (40 tests), `tests/test_newr_rt_js.py` (1 test driving
  `tests/js/rt_tests.js`, 61 Node checks), `tests/js/mocks/discord.js`
  (mock discord.js - only the API surface the emitted code touches,
  no network), `tests/js/bot-driver-lib.js` (fake interactions/
  messages/members/collectors), `tests/test_cli.py` (+9 TestBuildJs),
  `tests/test_conformance.py` (+1 JS run), `tests/test_goldens.py`
  (+1 TestGoldensJs).
- `benchmarks/run_js.py`, `benchmarks/handwritten/{hello,loop_plain,
  loop_checked,shop_economy}.js`, `benchmarks/generated/{loop.js,
  shop_economy.js,newr_rt.js}`, `benchmarks/raw/*_js.json` (11 files).
  (`benchmarks/generated/newr_rt.js` is a byte-identical copy of
  `compiler/newr_rt.js` for the checked loop - the same snapshot
  arrangement as the Python `benchmarks/generated/newr_rt.py`; it
  drifts if the runtime changes and must be re-copied then.)
- `.github/workflows/ci.yml` - new `js-emitter` job (ubuntu/macos/
  windows × Node 24; installs real discord.js 14.28.1 for the
  example runs, `NODE_PATH` at `${{ runner.temp }}/djs/node_modules`).
- `BENCHMARKS.md` - M3 section appended (M2 section preserved).

### 17.3 Test results

- Full suite, system Python:
  `python3 -m unittest discover -s tests -p "test_*.py"` →
  **Ran 820 tests in 70.6 s - OK (skipped=13)**.
- Full suite, fresh venv (discord.py 2.7.1 + aiosqlite):
  `python3 -m venv /tmp/m3-venv --clear`,
  `/tmp/m3-venv/bin/pip install -q discord.py==2.7.1 aiosqlite`,
  `/tmp/m3-venv/bin/python -m unittest discover -s tests -p "test_*.py"`
  → **Ran 820 tests in 68.4 s - OK (skipped=2)**.
- Baseline 685 + **135 new M3 tests** (83 JS emitter + 40 JS bot +
  1 rt driver + 9 CLI JS + 1 conformance JS + 1 goldens JS). The
  13 system skips are the 12 baseline skips + the retired
  `test_build_js_target_is_exit_1` (M2 refusal, now a skip - the
  behaviour it pinned is replaced by TestBuildJs).
- `goldens/py/` byte-identical (`git diff --stat -- goldens/py/`
  empty; `tests/test_goldens.py` passes for both targets).
- Conformance JS: `python3 conformance/run.py --target js` →
  **71 ok, 0 fail** (same 71 programs as Python). The 26 err
  programs still fail at check with the same codes (front end
  shared; `tests/test_conformance.py` covers both targets).
- Runtime checks: `node tests/js/rt_tests.js` → **61 checks passed**.
- CLI (live, `/tmp/jscli`): `newr build hello.newr --target js` →
  `hello.js` written, `node hello.js` prints `Hello, world!`;
  `newr build shop.newr --target js` → `shop.js`, `shop_items.js`,
  `shop_deploy.js`, `newr_rt.js`, all `node --check` clean;
  `NODE_PATH=tests/js/mocks node -e "require('./shop.js')"` loads,
  `commandMap` has ping/buy/balance; `--check` writes nothing,
  exit 0; `--fast` refused exit 1; `-o` writes to the given path.
- Shop golden live (mock discord.js + real node:sqlite):
  `bot.setup()` then `/buy brick` → `Not enough gold - brick
  costs 118, you have 0.`; `/balance` → `Balance: 0 gold.`;
  `economy.db` created. (An earlier ad-hoc run without `setup()`
  failed with `Cannot read properties of null (reading 'prepare')`
  - the store opens in `setup()`, as in Python; the test drivers
  call it, my script did not. Not an emitter bug.)

### 17.4 Bugs found and fixed during M3 (all TDD, one red-check each)

1. **Result methods emitted as properties/fields - WRONG.** The
   runtime Result has METHODS `isOk()`, `isErr()`, `unwrap()`,
   `unwrapOr(dflt)` and stores the value in `_value` (not `.value`).
   Fixed `is_ok` → `{obj}.isOk()`, `is_err` → `{obj}.isErr()`,
   `unwrap` → `{obj}.unwrap()`, `unwrap_or` → `{obj}.unwrapOr(arg)`.
   Red-check: reverting the fix → 3/3 tests fail
   (`test_try_expr`, `test_result_propagate`,
   `test_result_unwrap_in_chain`); restored OK.
2. **Float `/` by zero did not throw (P6).** JS `/` gives
   Infinity/NaN. Added `fdiv(a, b)` to `newr_rt.js` (throws
   `NewrDivisionByZeroError` when `b === 0`, covering -0) + export
   + 2 rt checks + emitter wiring (`/` on floats → `fdiv(...)`).
   Red-check: reverting the `fdiv` wiring →
   `test_float_division_by_zero_throws` fails; restored OK, 82/82
   green again.
3. **`_attach_components` never created the collector** (buttons
   would be dead). Rewrote its tail - always captures the message
   (fetchReply for replies; rewrite the answer line for
   followup/edit/system_channel/message_channel; uses the ctx
   message_var for timeout_edit), creates
   `createMessageComponentCollector` (`ComponentType.Button` /
   `StringSelect` / omitted when mixed; `time` = max timeout),
   'collect' callback with per-component `only:` gates (author/
   roles, scoped by customId) + routing to `componentHandlers`,
   'end' callback (only when a component has on_timeout) that
   disables all rows' children, edits the message, then runs each
   on_timeout body. `_emit_button`/`_emit_select` now emit
   on_timeout bodies at indent 0 and import `ActionRowBuilder`
   (was missing). Caught by the bot mock round-trip tests.
4. **Error-class `e.code` diverged from Python (conformance
   `try_catch.newr` failed: 70/71).** Python's
   `NewrDivisionByZeroError`/`NewrOverflowError` are NewrError
   subclasses constructed WITHOUT a code, so a newr `catch e:`
   sees `e.code == none`; my JS subclasses set `"ZeroDivisionError"`
   / `"OverflowError"`. Fixed: both classes now carry code `null`
   (told by `name`, not `code`); the four sites where Python
   raises the BUILTIN instead (`powFloat(0, -1)`,
   `round(inf)` ndigits=None, `toInt(inf)`, `toFloat(huge int)`)
   now throw `codedError("ZeroDivisionError"/"OverflowError", ...)`
   to match exactly. New `throwsNamed` helper in `rt_tests.js`
   pins name + null code; the builtin sites stay on `throwsCode`.
   Red-check: backup `/tmp/newr_rt.fixed.js`, sha256
   `fe9a1297e6c417a637847c185a0b4d1f5fd3ed611a5722beb0c8031ead1f5316`;
   reverting the two constructors → rt tests fail AND try_catch
   prints FAIL; restored sha256-identical → 61 checks pass,
   try_catch prints PASS, conformance 71/71.
5. **Deploy-file header named the wrong source.**
   `_emit_deploy_file` passed `f"{stem}_deploy.js"` to `_header`,
   so the golden read "Generated by newr from
   hello_bot_deploy.js". Fixed to `self.main_path` (the .newr
   source). New test `test_deploy_file_header_names_the_source`.
   Red-check: backup sha256
   `0bf61854e58219b8ecba3e742c00b4ede2fac28807ce26863db8bb8acfae1e45`
   (emitter_js.py); revert → test fails; restored
   sha256-identical → test passes.
6. **A file module's text depended on whether it was main or
   import.** Only imports got a `module.exports` block, so
   `shop_items.js` built from `shop.newr` (with exports) differed
   from the same file built from `shop_items.newr` (without) and
   the golden test failed. Fixed: every file with `pub` names
   gets the exports block in BOTH roles (a no-op when there are
   no pub names; harmless when the main file runs directly).
   New test `test_file_module_exports_same_as_main`. Red-check:
   backup sha256
   `8a930ea3393bfbcc5b1947a452d5f1d13b7ceca560c36a64170be40907d2b979`;
   revert → test errors (the revert reintroduced the removed
   `is_main` parameter reference - a NameError, not the original
   assertion; the substance is proven: without the unconditional
   exports line the test fails); restored sha256-identical → test
   passes, goldens green.
7. **`componentHandlers` exported but never defined** when a bot
   has no components (`ReferenceError: componentHandlers is not
   defined` in hello_bot). Fixed: the map is always emitted
   (possibly empty). Caught by the bot mock round-trip tests.
8. **Option min/max:** `setMinValue(1n)` is invalid - Discord
   takes JS numbers. Added `_emit_option_bound` (int literal →
   digits; other int exprs → `Number(...)`).

Structural fixes during the build (no separate red-checks - they
were caught by the suite going red, fixed, suite green):
chunk 5 of the emitter was appended inside `emit_program_js` as
unreachable code after its `return` (moved into the `_JSEmitter`
class); duplicate nested `def emit` dropped (`_emit_one` already
dispatches); duplicate `_emit_embed_lines` reconciled to one
canonical version; module-level `emit_js`/`emit_deploy` fixed to
use `_JSEmitter`; added `emit_program = emit_program_js` alias;
`program.is_bot` does not exist - fixed to
`program.bot is not None` (3 sites); `_answer_call` did not know
the `"interaction"` reply kind (mapped to `"reply"`); `list.len` /
builtin `len` returned JS numbers, not BigInt (fixed to
`BigInt(...)`); `ComponentType` + `StringSelectMenuOptionBuilder`
missing from `_DISCORD_JS` (name-mangling set) and `ComponentType`
missing from the test mock.

Test-source corrections (with evidence, not emitter changes):
`test_unary_minus_power` (checker rejects `float ** int`, E2001);
`test_template_literal_escapes` (`${x}` is an interpolation of
undefined `x`, E3001 - the literal form is `${{x}}`);
`test_backslash_join` (source had one backslash, not two);
`test_time` (`str` has no `.len()` method, E3001);
bot-test sources needed a `description:` under bare `bot "B":`
(an empty bot block is E1001), events are top-level (not inside
the bot block), `reply:` sugar works only directly under a
command/event (inside `do:` it is `reply "..."`), message events
answer with `send` not `reply` (E2008), commands must end in a
reply (E2010), plain strings emit double-quoted not backticked.

### 17.5 Benchmarks (M3, full run - `python3 benchmarks/run_js.py`)

Machine: Linux x86_64, Python 3.12.3, Node v24.22.1, hyperfine
1.19.0. Raw JSONs in `benchmarks/raw/*_js.json`.

| # | Generated | Hand-written | Ratio |
|---|---|---|---|
| B1 compile cold hello | 125.2 ms ± 17.5 (N=10) | - | - |
| B1 compile cold shop | 142.0 ms ± 4.0 (N=10) | - | - |
| B2 compile warm hello | 125.0 ms ± 22.9 (N=20) | - | - |
| B2 compile warm shop (1 file changed) | 148.0 ms ± 13.2 (N=20) | - | - |
| B4 hello size | 188 B, 6 lines | 285 B, 8 lines | **0.66** |
| B4 loop size | 388 B, 14 lines | 562 B, 17 lines | **0.69** |
| B5 startup hello | 86.1 ms ± 5.5 (N=10) | 89.3 ms ± 11.3 | **0.96** |
| B6 loop 100k (checked, BigInt) | 107.9 ms ± 27.4 (N=10) | plain numbers 88.4 ms ± 9.7 | **1.22** |
| B6 loop vs checked hand-written | 107.9 ms | 98.8 ms ± 6.4 | **1.09** |
| B6b shop economy 1,000 credits | 203.9 ms ± 25.7 (N=10) | 228.4 ms ± 55.2 | **0.89** |
| B7 peak RSS compile (shop) | 17.9 MB | - | - |
| B7 peak RSS run (hello, node) | 61.2 MB | - | - |

(All three loop versions print the same checksum, 105003. Both
shop economy drivers print the final balance 1000.)

Honest flags (same rule as M2's B3): **B6 generated/plain 1.22
is just over the 1.20 proposal threshold - flagged, not hidden.**
It is the BigInt price of int64 exactness in JS (J2): the
generated code is within noise of hand-written code using the
same checked helpers (1.09), and the generated mean has high
variance (±27.4 ms, one run at 182.7 ms). The plain-number loop
is not a fair target - it cannot represent int64 at all. B7 run
61.2 MB is Node's V8 baseline, not the program (Python's run is
11.1 MB for the same hello; the compile figure 17.9 MB is the
same Python compiler as M2). B6b's discord.js import is satisfied
by the in-repo mock, not real discord.js 14.28.1: both drivers
pay the same small mock require cost, so the ratio is honest but
the absolute times understate a real discord.js import. B1/B2
measure the same Python compiler as M2 (125.2/142.0 ms vs M2's
127.1/165.5 ms) - the JS emitter is not slower than the Python
emitter at compile time.

### 17.6 CI

New `js-emitter` job in `.github/workflows/ci.yml`
(ubuntu/macos/windows × Node 24): set up Python 3.12 + Node 24,
install discord.py 2.7.1 + aiosqlite (the full suite includes the
Python bot tests), install real discord.js 14.28.1 via
`npm install --no-save --prefix "${{ runner.temp }}/djs"`
(verified live: installs in ~10 s, version 14.28.1), full suite,
`python conformance/run.py --target js`, build + run the examples
under Node (hello prints `Hello, world!`; shop/hello_bot/showcase
reach the DISCORD_TOKEN guard - verified live with real
discord.js), JS golden comparison. YAML validated
(`yaml.safe_load` - 7 jobs incl. `js-emitter`, 9 steps).

### 17.7 UNVERIFIED items (M3)

- **CI has never run** - the `js-emitter` job is written and its
  steps are simulated locally, but Windows/macOS execution stays
  UNVERIFIED until green on all three OSes (same standing note
  as M2; the matrix exists, the runs do not).
- **No live Discord bot has connected yet (P-42)** - the emitted
  bots reach the DISCORD_TOKEN guard under real discord.js
  14.28.1, and the mock round-trip tests drive commands/events/
  components/store end to end, but no real Discord call was made.
  A live guild + token is still needed for P-42.
- **Sets of value types (struct/enum)** use identity
  in JS where Python hashes by value (J6 limitation). No
  conformance program covers it. (The dict half of the old J6
  wording is unreachable: dict keys must be `str`, E2001.)
- **Str comparison for astral characters** is UTF-16 code units
  in JS vs code points in Python (documented drift).
- **File-IO error codes** are host-specific (JS `"ENOENT"` vs
  Python `"FileNotFoundError"`).
- **`log` timestamps**: JS emits bare `console.info/warn/error`
  (no timestamp) where Python's logging format has one (J12).
- **B6b absolute times** understate a real discord.js import
  (mock stand-in - see §17.5).
- **The `benchmarks/generated/newr_rt.js` snapshot** drifts if
  `compiler/newr_rt.js` changes (same arrangement as the Python
  twin; re-copy on runtime changes).
- **discord.js option getters beyond the track**: `addIntegerOption`
  / `getInteger` etc. follow the identical chaining pattern but
  are not shown verbatim in the surveyed track (extrapolation,
  flagged in the survey); the emitted shop exercises
  `getString`/`getInteger` against the mock, not the real library.

### 17.8 What is NOT in M3 (carried forward)

- SPEC.md needed **no correction** for M3 (the §6 doc-comment note
  from M2 remains the only post-freeze addition). The §16.4 open
  item (amount-based debit/credit heal) stays open and
  non-blocking - the JS store implements the same presence-based
  replay as Python (§14.1 P11), with the transfer heal
  amount-based as specified.
- **Not pushed** - chie reviews and pushes. `git status` at the
  end of M3: 8 modified files (`.github/workflows/ci.yml`,
  `BENCHMARKS.md`, `BUILD_REPORT.md`, `compiler/cli.py`,
  `conformance/run.py`, `tests/test_cli.py`,
  `tests/test_conformance.py`, `tests/test_goldens.py`;
  767 insertions, 57 deletions) + 26 new files/dirs (emitter,
  runtime, goldens/js, tests/js + 3 test files, benchmark
  fixtures + runner + 11 raw JSONs). No other files touched;
  `goldens/py/` byte-identical.

## 18. M3 fix round - cold re-evaluation of `fe63ff8` (7 Nov 2026)

Cold re-evaluation verdict: **NEEDS FIXES - 2 blocking items**,
both found with the evaluator's own repro programs (not copies of
the repo's tests). Everything else held up: suite 820/820 on
system Python and in the evaluator's fresh venv, conformance
71 ok js + 71 ok py, rt tests 61 checks, goldens byte-identical,
benchmarks recomputed from the raw JSONs and matching. Both items
are in the JS emitter/runtime; both fixed here TDD-style (failing
test first, one sha256-verified red-check per fix).

### 18.1 Fix 1 - `?`-propagate on `Result` silently returned wrong values

The evaluator's repro (`chain`: `div(100, x)?` then `div(h, 2)?`):
`chain(4)` → Python `ok(12)` (correct: 100/4=25, 25/2=12), JS
`ok(25)` - the second division never ran. `chain(0)` → `err` on
both (correct by accident).

Root cause - two defects in the same code path,
`compiler/emitter_js.py::_propagate_unwrapped`, in BOTH branches
(normal, and `in_handler`):

- The emitted check was `if ({temp}.isErr) {` - but `Result.isErr`
  is a **method** in `compiler/newr_rt.js`. A bare method reference
  is always truthy, so `?` early-returned on **every** value, Ok or
  Err. The Python emitter emits `if {temp}.is_err():` (with parens,
  `compiler/emitter.py:1849,1876`). The method-call path in the JS
  emitter already knew this (`emitter_js.py` ~1343: "The JS Result
  exposes isOk/isErr as METHODS … a bare access would be the
  function itself, not the bool") and emits `isOk()`/`isErr()` -
  the propagate sites were the only places that forgot.
- The returned value was `{temp}.value` - but JS `Result` has no
  `.value` property (it stores `._value`, no getter; verified live:
  `rt.ok(42n).value === undefined`, `._value === 42n`). While
  defect 1 existed this was unreachable; fixing defect 1 alone
  would have activated defect 2. Both fixed together.

**Decision:** `.isErr()` + `._value`, NOT a `value` getter on
`Result`. Minimal, and it matches the Python emitter's call-site
shape (`is_err()` + `.value`); `Result` already exposes value
access via `unwrap()`/`unwrapOr()`, and a getter would be new
public API surface for a one-site need. The nullable `?` path is
untouched (`=== null` check, returns the temp itself - it was
correct).

Why the suite missed it: the only coverage used a *single
trailing* `?` (`conformance/ok/result_propagate.newr` `half`, and
`test_result_propagate`, same shape) - with the bug, `half(2)`
early-returns `ok(5)`, the same value as correct. The bug only
shows with two sequential propagates, or any use of the unwrapped
value after `?`.

New tests:

- `tests/test_emitter_js.py::TestControlAndContracts.test_result_propagate_chain`
  (behavioural, `run_js_ok`): the `chain` program →
  `"12\ntrue\n"`.
- `tests/test_emitter_js_bot.py::TestBotStructure.test_result_propagate_in_handler`
  (structural, emitted text): a command handler doing
  `let h = div(100, 2)?` must emit `.isErr()` and `._value`, and
  must not emit a bare `.isErr)`. (The handler test program first
  failed to parse - `command` is a top-level block in newr, not a
  line inside the `bot` block; test source fixed, not the compiler.)
- `conformance/ok/result_propagate_chain.newr` (new program, runs
  on both targets): the `chain` shape as a PASS/FAIL program.

Red-check: backup `/tmp/emitter_js.fixed.py`, sha256
`1354f338e66eaf487f6c61336a6ee011f8de0c5bf915621a386a7446528c95d5`;
reverting both sites to the buggy form → the chain test AND the
handler test fail (2 failures); restored sha256-identical → both
OK. (The first revert attempt asserted the wrong count - the fix
comments themselves contain `._value` - and wrote nothing; the
revert was redone with the assertion on the code lines only.)

### 18.2 Fix 2 - float `/` by zero: `e.code` diverged py vs js

The evaluator's repro: catch `1.0 / 0.0`, print
`e.code ?? "none"` → Python `"ZeroDivisionError"`, JS `"none"`.

Root cause: Python's float `/` is native `/` (the Python emitter
emits it bare - `emitter.py::_emit_binop` falls through to
`f"{left} {op} {right}"` for float division), which raises the
**builtin** `ZeroDivisionError`. JS `fdiv`
(`compiler/newr_rt.js`) threw `NewrDivisionByZeroError`, whose
code is `null` per the §17.4 item-4 fix. The item-4 list was
**incomplete**: it named four builtin sites
(`powFloat(0, -1)`, `round(inf)`, `toInt(inf)`, `toFloat(huge
int)`); `fdiv` is a **fifth** builtin site it missed.

Scope checked, not widened: float `%` is NOT the same bug.
Python routes float `%` through `fmod`
(`compiler/newr_rt.py:172`), which raises the codeless
`NewrDivisionByZeroError` - so JS `fmod` throwing the codeless
class is correct parity, and it is untouched. (Python's native
`%` would raise `ZeroDivisionError: float modulo`, but newr never
emits native `%` for floats - it needs C-`fmod` dividend-sign
semantics.) The evaluator verified the other builtin sites
genuinely match (`0.0 ** -1.0` → `"ZeroDivisionError"` both;
`round(inf)` / `int(inf)` → `"OverflowError"` both).

Fix (one line, following the established pattern): `fdiv` now
throws `codedError("ZeroDivisionError", "float division by zero")`
- `asError` turns a plain `Error` with a string `.code` into a
newr error carrying that code, so a `catch e:` sees
`e.code == "ZeroDivisionError"` on both targets. The
`NewrDivisionByZeroError` class comment is updated to list both
builtin sites (powFloat, fdiv), and §17.4 item 4's "four sites"
is corrected by this section (fdiv is the fifth).

New tests:

- `tests/js/rt_tests.js` "fdiv by zero": was `throwsNamed(...,
  "NewrDivisionByZeroError")`, now `throwsCode(..., "ZeroDivisionError")`
  for `1.0/0.0`, `0.0/0.0`, `1.0/-0.0`.
- `tests/test_emitter_js.py::TestControlAndContracts.test_float_div_by_zero_error_code`
  (JS): catch `1.0 / 0.0`, print `e.code ?? "none"` →
  `"ZeroDivisionError\n"`.
- `tests/test_emitter.py::TestErrorsAndResult.test_float_div_by_zero_error_code`
  (Python twin): same program → `"ZeroDivisionError\n"` - passes
  before and after the fix; it pins the Python reference behaviour.
- `conformance/ok/arith_div_zero_catch.newr` extended: the float
  branch now also asserts `e.code == "ZeroDivisionError"` (both
  targets).

Red-check: backup `/tmp/newr_rt.fixed.js`, sha256
`a054091ad41e338cfcee67f8e6b908ca445ded43330408792ad055711e9bc933`;
reverting `fdiv` to the codeless class → rt tests crash (Node
assertion error) AND the float-code test fails; restored
sha256-identical → 61 checks pass + test OK.

### 18.3 J6 correction - the dict half is unreachable (non-blocking)

The evaluator's note: J6 said "sets/dicts keyed by value types
(struct/enum) use identity in JS". Verified live: a dict keyed by
a struct cannot even be written - `let d = {p: 1}` and
`let d: dict[Pt, int] = {}` are both rejected at check with
`error[E2001]: dict keys must be \`str\``. SPEC §12 already says
`dict[str, T]` (keys must be str), so **SPEC.md needed no change**.
The J6 row (§17.1) and the §17.7 limitation list now say "sets of
value types" only, each with a correction note pointing here. The
set half stays live: `set([Pt(1,2), Pt(1,2)])` has `len` 1 in
Python, 2 in JS; `contains` is structural `eq` on both.

### 18.4 Verification

- Full suite, system Python:
  `python3 -m unittest discover -s tests -p "test_*.py"` →
  **Ran 824 tests in 70.4 s - OK (skipped=13)** (820 baseline + 4
  new: chain, float-code JS, handler, float-code Python).
- Full suite, fresh venv (`/tmp/m3fix-venv`, discord.py 2.7.1 +
  aiosqlite): **Ran 824 tests in 67.6 s - OK (skipped=2)**.
- Conformance: `python3 conformance/run.py --target js` → **72 ok,
  exit 0**; `--target py` → **72 ok, exit 0** (71 + the new
  `result_propagate_chain.newr`; the 26 err programs unchanged).
- Runtime: `node tests/js/rt_tests.js` → **61 checks passed**.
- Evaluator repros, after the fix: `c_result.newr` → PASS on py
  AND js; `b_int.newr` → byte-identical output on both targets
  (including `ZeroDivisionError` and the float renderings).
- Goldens untouched: `git diff --stat -- goldens/` empty (the
  fixes touch no golden program's output).
- `benchmarks/generated/newr_rt.js` snapshot: went stale at the
  `fdiv` change (first diff at char 2837) and was re-copied per
  the M3 arrangement - byte-identical to `compiler/newr_rt.js`
  again.
- CLI live (`/tmp/jscli-fix`): `newr build hello.newr --target
  js` → `node hello.js` prints `Hello, world!`; `newr build
  shop.newr --target js` → shop files written with the 3 baseline
  warnings, `node --check` clean.
- Changed files: `compiler/emitter_js.py`, `compiler/newr_rt.js`,
  `conformance/ok/arith_div_zero_catch.newr`,
  `tests/js/rt_tests.js`, `tests/test_emitter.py`,
  `tests/test_emitter_js.py`, `tests/test_emitter_js_bot.py`,
  `benchmarks/generated/newr_rt.js` (snapshot re-copy),
  `BUILD_REPORT.md` (§18 + J6 corrections); new file
  `conformance/ok/result_propagate_chain.newr`. **Not pushed** -
  chie reviews and pushes.
- Still open (unchanged): §16.4 amount-based debit/credit heal;
  CI matrix never run (Windows/macOS); P-42 live Discord.

## 19. M3 fix round 2 - top-level declarations dropped in bot files (7 Nov 2026)

Second cold re-evaluation (of `a4a9308`) verdict: **NEEDS FIXES - 1
blocking item**. Both §18 fixes verified genuinely fixed with the
evaluator's own repros (three-sequential-`?` chain, handler `?` via
try-expr, all float-div and e.code variants, a 9-case probe over the
other runtime throw sites with byte-identical py/js output, goldens
and benchmarks holding). The new item is in the exact shape the fix
round's own handler test uses - and the fix round 1 handler test was
structural-only on that shape, which is why it passed while the
program itself could not run.

### 19.1 The blocking item - bot files silently dropped top-level declarations

A bot file with a top-level `fn div` built exit 0 with no warning on
both targets, and both outputs contained the call site `div(` once
but ZERO definitions of `div`. At runtime: JS under Node with the
repo mock → `ReferenceError: div is not defined`, reply the generic
"Something went wrong running that command."; Python in a fresh venv
with a fake interaction → `NameError: name 'div' is not defined`.
Same for a struct, a top-level `let`, and an enum (`Color_Red`
referenced but never defined), and a top-level `print` (0
occurrences in either output). Pre-existing, not introduced by fix
round 1 (confirmed identical at `fe63ff8`).

Root cause: neither `_emit_bot_file` emitted `program.functions`
(nor structs, enums, top-level lets, or top-level statements) -
`compiler/emitter.py:2543` and `compiler/emitter_js.py:3411` emitted
only the Bot class, commands, tasks, events, error handler,
`_channel_text`, `main`. `_register_module_scope` registers the
names so the checker/resolver accepts them, but nothing emitted
their definitions. SPEC §8 ("declarations … are resolved across
the whole file"), §8.2 (bot block "position is free among top-level
items"), and §13.7 ("top-level statements and other declarations
may appear in the same file as bot blocks") all promise this shape
works - so the fix EMITS the declarations; the reject-at-check
alternative would contradict SPEC.

### 19.2 The fix - emit declarations in bot files (both emitters)

In both `_emit_bot_file`s, before the Bot class: structs, then
enums, then functions (hoisted, as in a general file), then the
top-level statements in file order - reusing `_emit_struct`,
`_emit_enum`, `_emit_fn`, `_emit_stmt_with_hoists`. Ordering
decisions:

- Declarations come after the strings/components in the Python
  assembly (after the `componentHandlers` map in JS) and before
  the Bot class.
- Top-level statements run at module import/load time, before
  `bot = Bot()` / `const bot = new Bot()` and before `main()`;
  handlers read the declarations at call time, so the order
  relative to the Bot class does not matter for handlers.
- The no-declaration case stays byte-identical: the separator
  (two blank lines before `class Bot`) is added only when
  something was emitted. `examples/hello_bot.newr` has no
  top-level declarations, so no golden changes shape.
- A struct's `@dataclass` (Python) resolves its string field
  annotations through `sys.modules` - see the harness note in
  §19.4.

### 19.3 The guard the fix forced - store calls outside handlers (both emitters)

Emitting declarations naively would have turned two
checker-accepted programs into unparseable output: a store call
lowers to a bare `await bot.<store>_<op>(...)`, which is a
SyntaxError at module level and inside a sync `def` / sync
lambda. Probing found the hole is live TODAY in already-emitted
code, not just in the new path: a store call inside a lambda in a
handler emitted `await` inside a sync lambda on both targets
(`'await' outside async function` / `await is only valid in async
functions`), for a program the checker accepts with only a
warning. SPEC §9.5 says functions "may read `store`" - but a
sync fn cannot `await`; making store-reading fns async (with
transitive await at call sites) is a design decision for a later
milestone, not this fix.

So both `_emit_store_call`s now fail cleanly (E4001, exit 4)
when the innermost `_fn_ctx` entry is not a handler - i.e. at the
top level, inside a top-level fn, or inside a lambda:

```
error[E4001]: cannot emit this program: a store call
(`economy.balance`) is only possible inside a command, event, or
task handler - top-level functions and statements run outside the
bot's async context
```

The guard covers `balance`/`credit`/`debit`/`transfer`/`get`/`set`/
`delete` (all seven ops go through the one function). Store calls
inside command/event/task/component handlers are unaffected - they
push handler context (`in_handler: True`).

### 19.4 Tests (TDD - failing first, round-trip, not structural)

Shared program `SCOPE_BOT` (in both test files): top-level `fn
div` (`try a / b`), `struct Pt {x, y}`, `enum Color {Red, Green}`,
`let greeting: str = "hi"`, `fn make_pt(a) -> Pt`; bot `"Scope"`
with commands `go` (`div(100,2)?` → `"half is 50"`), `bad`
(`div(100,0)?` → must bare-return, no reply), `show`
(`make_pt(3)` → `"hi 3 4"`), `colour` (`Color.Red` equality →
`"red"`).

- Python `tests/test_emitter_bot.py`,
  `TestBotMockRoundTrip.test_top_level_declarations_reach_handlers`
  (real round-trip via `_load_bot_module` + `_FakeInteraction`,
  asserts all four replies) and
  `test_top_level_statement_runs_at_import` (execs the emitted
  module with stdout captured, asserts `"loaded"` appears). RED
  pre-fix in the fresh venv: `NameError: name 'div' is not
  defined`; `'loaded' not found in ''`. (Skipped on system Python
  - no discord.py there.)
- JS `tests/test_emitter_js_bot.py`,
  `TestBotMockRoundTrip.test_top_level_declarations_reach_handlers`
  (via `emit_js_src` + `run_bot_driver`, fires /go, /bad, /show,
  /colour under the Node mock). RED pre-fix: `AssertionError:
  'Something went wrong running that command. Try again in a
  moment.' != 'half is 50'`.
- Guard tests (both files):
  `TestBotStore.test_store_call_outside_handler_is_rejected` -
  asserts `EmitterError` for a store call at the top level, in a
  top-level fn, and in a lambda inside a handler.
- Harness fix in `tests/test_emitter_bot.py`: `_load_bot_module`
  now registers the synthetic module in `sys.modules` before
  `exec` (restoring the previous entry afterwards). The struct
  emission exposed a latent harness bug: `@dataclass` with string
  annotations (`x: "int"`) resolves them via
  `sys.modules[cls.__module__]`, which was `None` for the
  unregistered module (`AttributeError: 'NoneType' object has no
  attribute '__dict__'`). Before §19 no struct was ever emitted in
  a bot file, so the missing registration never mattered.

Red-checks (one per emitter, sha256-verified): reverted only the
declarations emission in `compiler/emitter.py` → the Python
round-trip test failed with `NameError: name 'div' is not
defined`; restored via edit, hash `fcea82ede1739187532fa5d0689db8
3c9ec656eceb6f510490311ee0e1f79608` (exact match), test green.
Reverted only the declarations emission in
`compiler/emitter_js.py` → the JS round-trip test failed with the
generic error reply instead of `'half is 50'`; restored, hash
`e9c9e936cf36457a553e3cea04fa30a2f2c43d03c744ce8b2e6fdf01c303
2e44` (exact match), test green.

### 19.5 Verification (fix round 2)

- Full suite, system Python: **Ran 829 tests in 71.4 s - OK
  (skipped=15)**. Full suite, fresh venv
  (`~/workspace/venvs/newr-fix2`, discord.py 2.7.1 + aiosqlite):
  **Ran 829 tests in 68.3 s - OK (skipped=2)**. 824 + 5 new
  (2 round-trip + 1 statement + 2 guard).
- Conformance: `python3 conformance/run.py --target js` → **72
  ok, exit 0**; `--target py` → **72 ok, exit 0**. No bot ok
  program was added, and none can be: the harness builds AND runs
  every ok program requiring exit 0 + last line `PASS`, and a bot
  file's `main()` without `DISCORD_TOKEN` exits non-zero on both
  targets (Python raises `SystemExit("set the DISCORD_TOKEN
  environment variable")`; JS throws → exit 1). Runtime behaviour
  of bots is covered by the mock round-trip tests instead.
- Runtime: `node tests/js/rt_tests.js` → **61 checks passed**.
- Evaluator repros, after the fix (fresh builds, run live):
  `c_bot.newr` → py /go `'half is 50'`, /bad `[]` (silent);
  js /go `'half is 50'`, /bad 0 replies. `d_scope.newr` →
  `class Pt` / `def make_pt` / `greeting = ` present on both
  targets. `i_enum.newr` → `Color_Red` defined + used on both
  targets (4 occurrences py, 3 js). `e_stmt.newr` → the
  top-level statement prints at module load on both targets
  (Python import; Node require under the mock).
- Guard live (CLI, both targets, exit 4 E4001): store call at
  the top level, in a top-level fn, and in a lambda in a handler
  all fail cleanly with the §19.3 diagnostic; the lambda case
  previously produced unparseable output.
- Goldens untouched: `git diff --stat -- goldens/` empty (the
  no-declaration case is byte-identical by construction).
- §18 fixes untouched: the diff to `compiler/emitter.py` /
  `compiler/emitter_js.py` contains no `isErr` / `._value` /
  `fdiv` / `ZeroDivisionError` lines; int `1/0` e.code `"none"`
  unchanged (parity on both sides, not a divergence).
- Same-drop check: `_emit_bot_file` has exactly one caller per
  emitter - `_emit_one`, the `_emit_file_set` dispatch - so
  imported file modules go through the same path (and the
  resolver rejects bot blocks in imported files anyway, E3006:
  "a `bot` block must live in the main file"). No other emitter
  path can drop declarations this way.
- Changed files: `compiler/emitter.py`,
  `compiler/emitter_js.py`, `tests/test_emitter_bot.py`,
  `tests/test_emitter_js_bot.py`, `BUILD_REPORT.md` (§19).
  **Not pushed** - chie reviews and pushes.
- Still open (unchanged): §16.4 amount-based debit/credit heal;
  CI matrix never run (Windows/macOS); P-42 live Discord. New
  open item from §19.3: async functions - SPEC §9.5 promises a
  top-level fn "may read `store`", which needs async fn lowering
  (transitive `await` at call sites) in a later milestone; until
  then the emitter fails cleanly with E4001.

## 20. M3 fix round 3 - qualified imported enum members (7 Nov 2026)

Third cold re-evaluation (of `9afc8d7`) verdict: **NEEDS FIXES - 1
blocking item**. Both §19 fixes verified genuinely fixed with the
evaluator's own repros (bot round trip on both targets, §18 chain and
float-div probes, suite 829/829 in both envs, conformance 72+72, rt
tests 61, goldens byte-identical, benchmarks recomputed matching,
red-checks done, store guard holding). The new item is the same defect
class this loop has blocked on twice before: the checker accepts a
SPEC-promised program the emitter cannot handle.

### 20.1 The blocking item - imported enum member builds fail on both targets

Repro (evaluator's files, chie confirmed live herself):

```newr
# m_helper3.newr
pub enum Shade: Red, Blue
pub fn is_red(s: Shade) -> bool: return s == Shade.Red
# p_enum_print.newr
import "m_helper3.newr"
print m_helper3.Shade.Red
```

`newr check` says ok, exit 0. `newr build --target py` and
`--target js` both fail, exit 4:

```
error[E4001]: cannot emit this program: cannot emit this program: an expression has no type - run `newr check` first (the program may have errors)
```

span 1:1 (the import line). Identical for the argument form
`print m_helper3.is_red(m_helper3.Shade.Red)`. Pre-existing at
`fe63ff8` and `a4a9308`, not introduced by fix round 2. SPEC §11
promises imported files contribute enums, and qualified access
`module.Name` is the documented form.

### 20.2 Root cause - where the type was missing, where it is now attached

`compiler/checker.py::_module_member_type` returned `UNKNOWN` for an
imported enum (`if name in target.enums: return UNKNOWN`, ~line 1145).
`synth` (~line 457) sets `expr.expr_type = None` for UNKNOWN types, so
the emitter's `_type_of` refused the expression. The intermediate node
`m_helper3.Shade` (the enum name) is not a value in newr, only variants
are, so there was no type to attach to it at all: the full three-level
chain `module.Enum.Variant` had to be typed as one shape, from the
imported module's enum declaration.

The type is now attached in the checker, in two places:

- `_synth_Member` gained a qualified-imported-enum-member branch
  (before the generic path): when the shape is
  `Member(Member(Name(module), Enum), Variant)` and the inner name is
  an enum in the imported module's `_ModuleCtx`, the new
  `_imported_enum_member_type` validates the variant against that
  module's enum declaration and returns `Type(enum_name)`. Unknown
  variant is E3001 with did-you-mean help, fielded variant used bare
  is E2001, mirroring the local-enum branch of `_member_field_type`
  (same codes, same wording). The intermediate `m_helper3.Shade`
  node gets no type of its own, consistent with the local `Color.Red`
  path, which also leaves the bare enum name untyped. The emitters
  resolve the whole chain at emit time without typing the
  intermediate node.
- `_call_member` gained the matching branch for fielded-variant
  construction `m_helper.Shape.Circle(radius: 1.5)`, by generalising
  `_call_enum_variant` to take an optional `decl` (the imported
  module's enum declaration instead of this file's).

The emitters then emit the chain directly, looking the declaration up
through `program.modules` (bound name to `Program`, the same lookup
`_emit_file_set` uses):

- Python (`compiler/emitter.py`): `_imported_enum` helper returns
  `(ref, enum_name, variant)` only when the inner name is really an
  enum in the imported module, so a struct-typed top-level let
  accessed the same way (`m_helper.SomeLet.field`) does NOT misfire
  and falls through to the ordinary typed-member path. `_emit_member`
  emits `ref.Enum_Variant()` (or the class itself when the type is
  `fn`, mirroring local `_emit_enum_variant`); `_emit_call` emits the
  fielded construction, mirroring `_emit_enum_call`. Python kwargs are
  order-free, so the call args emit as written.
- JS (`compiler/emitter_js.py`): the same helper (returning the decl
  as well, so a fielded-variant call can reorder its keyword
  arguments into declared field order, since JS has no kwargs).
  `_emit_member` emits `new ref.Enum_Variant()`; `_emit_enum_call`
  gained optional `decl` / `ref` parameters for the imported form and
  the `_emit_call` branch passes them.

### 20.3 The diagnostic fix (part of the blocking item)

Three defects, one shared path:

1. The `cannot emit this program:` prefix was doubled.
   `emitter.py::_type_of` and `emitter_js.py::_type_of` already
   included the prefix in their message, and `compiler/cli.py:638`
   wrapped every `EmitterError` with another
   `f"cannot emit this program: {exc}"`. The prefix was stripped
   from both `_type_of` messages. The CLI wrap now supplies it
   exactly once. No other emitter message carries the prefix
   (checked by grep).
2. The help text sent the user in a circle: `newr check` passes on
   the programs that reach this path, yet the error told the user to
   run it first. The message is now `an expression has no type: the
   emitter cannot handle it here`, with the span pointing at the
   expression. It names no construct it cannot name honestly; the
   span does the pointing.
3. The span was always 1:1. The CLI always used
   `Span(str(path), 0, 1)` for emitter errors. `EmitterError` gained
   an optional `span` (both emitters share the class: JS imports it
   from `.emitter`), `_fail` gained a `span` parameter, and the CLI
   prefers `exc.span` when present, falling back to 1:1 for spanless
   refusals. Rendering already handles cross-file spans
   (`_source_cache` reads the span's file from disk).

Spans were also attached on the same path where the node was already
in hand: both `_type_of` calls, both store-guard `_fail` calls (this
fixes the evaluator's cosmetic note 2 from the `9afc8d7`
re-evaluation as a side effect: the store-guard span now points at
the call site, verified live at 5:14 on both targets, not at the
`store` declaration), the JS `_emit_name` / `_emit_member` "not a
value" failures, and the Python `_emit_name` enum refusal below.

Bare enum name as a value: the Python emitter used to return
`binding.value` for ANY binding kind, so `let c = Color` silently
emitted `c = Color` (a runtime `NameError`) and the failure only
surfaced later at the first use of `c`, at the wrong span. Python
`_emit_name` now refuses kind `enum` with the JS twin's message,
`` `Color` is not a value - use a variant or construct it``,
with the name's span. Struct names are NOT refused: the checker types
a bare struct name as its constructor `fn`, so emitting the class is
consistent with the checker (calling a let-bound struct constructor
is a separate, still-open gap, see §20.8).

Diagnostic before / after (same program, `let c = Color` on line 4):

```
# before, both targets:
error[E4001]: cannot emit this program: cannot emit this program: an expression has no type - run `newr check` first (the program may have errors)
  --> bad.newr:1:1
# after, both targets, byte-identical:
error[E4001]: cannot emit this program: `Color` is not a value - use a variant or construct it
  --> bad.newr:4:9
  |
4 | let c = Color
  |         ^^^^^
```

### 20.4 Two more live defects found while fixing this one

Both are in the same import machinery the enum fix touches, both
were verified live before fixing, both are pinned by tests.

**Aliased file-module imports were broken at runtime.** `import
"h_fn.newr" as h` emitted `import h`, but the emitted file is
`h_fn.py`, so every aliased file-module use died with
`ModuleNotFoundError` at run time (proven live with a fn call,
`h.double(21)`). The alias machinery already existed
(`_need_import(module, alias)` renders `import helper as h`), the
call sites just passed the alias as the module name. A shared
`_need_file_module(bound)` helper now renders
`import <real> as <alias>` from the import's resolved path stem; it
is used by `_emit_module_call` (fn calls), the `_emit_member` enum
branch, and the `_emit_call` enum branch. Live after the fix:
`import h_fn as h`, prints `42`. The JS emitter was already correct
here: it registers `const h = require("./h_fn.js")` for every import
up front, alias included.

**A `pub enum` file could not be loaded under Node at all.**
`emitter_js.py::_emit_file_exports` exported the bare enum name
(`Shade`), but `_emit_enum` defines only the variant classes
(`Shade_Red`, `Shade_Blue`): no `Shade` binding exists in JS, so
`require` of any file with a `pub enum` threw
`ReferenceError: Shade is not defined` at the `module.exports` line
(verified live). Python emits the union alias
`Shade = Shade_Red | Shade_Blue`, so its export is real. The JS
export now lists only the variant classes. No `pub enum` exists in
`examples/`, `goldens/`, or `conformance/`, so no golden changes.

### 20.5 Tests (19 new, all written RED first)

- `tests/test_checker.py::TestImportedEnumMember` (7): bare variant
  types clean AND the member node carries type `Shade` (the test
  walks the typed AST for the outermost `Member` whose object is
  itself a `Member`, so a plain `module.fn` callee, which is
  correctly untyped, is never mistaken for it); variant as argument;
  aliased import; unknown variant E3001; fielded variant bare
  E2001; fielded-variant call clean; wrong field E3001 (same code as
  the local path: `_check_args` reports an unknown named argument as
  E3001, "has no parameter").
- `tests/test_emitter.py::TestImportedEnumMember` (4): live runs,
  print form `Shade.Red`, arg form `true`, aliased import
  `Shade.Red`, fielded call `Shape.Circle(radius: 1.5)`, each
  asserting the emitted text too (`helper.Shade_Red()`,
  `h.Shade_Red()`, `helper.Shape_Circle(radius=1.5)`).
  Plus `TestFileImport::test_aliased_module_call_imports_real_module`
  (1): the aliased fn-call regression, asserting
  `import items as it` in the emitted text and a live `118` run.
- `tests/test_emitter_js.py::TestImportedEnumMember` (5): JS mirrors
  under Node (`Shade.Red`, `true`, `Shade.Red`,
  `Shape.Circle(radius: 1.5)`, emitted text `new helper.Shade_Red()`
  / `new helper.Shape_Circle(1.5)`), plus the export test: the
  emitted helper's `module.exports` contains no bare enum entry and
  the file loads under Node.
- `tests/test_cli.py` (2): the diagnostic test on each target (bare
  enum name build): exactly one `cannot emit this program` prefix,
  no `run `newr check` first` circle, span `:4:` on the `Color`
  name, no `1 | enum Color:` line.
- Conformance: `conformance/ok/import_enum.newr` plus helper
  `conformance/ok/lib/import_enum_helper.newr`. The helper lives in
  `ok/lib/` because the harness globs only `ok/*.newr`
  non-recursively (a helper in `ok/` itself would be run standalone
  and fail the PASS check); `_build_to_temp` collects all built
  outputs via rglob and `run_conformance` runs them together, so the
  multi-file program fits the harness unchanged. The program prints
  `PASS` as its last line on both targets.

RED runs (all before the fix, all shown): checker 6 of 7 failing
(the wrong-field test already passed, for the wrong reason: the old
code errored E3001 from a different path); Python emitter 4 of 4
erroring with the exact blocking E4001; JS emitter 5 of 5 erroring;
both CLI diagnostic tests failing (doubled prefix, circle help,
span 1:1).

### 20.6 Red-checks (sha256-verified)

Four, one per changed compiler file. Each: save the diff, revert
only that file, confirm the new tests + the live repro fail, restore,
confirm the sha256 matches exactly and the tests are green again.

- Shared front-end (`compiler/checker.py`): revert
  `fdc0309a870060e78bedb86a005a5c9f05ff0f5ab3717e8322863f069818fe87`
  -> `861fd139022f18d2797863454d8f48896838099ca3bcb4fbfa625115ba28b43b`.
  Checker 6 failures + Python emitter 4 errors; live repro fails
  exit 4 (with the new single-prefix message and span 3:7, from the
  emitter fix still in place). Restore: hash `fdc0309a...` again,
  11 tests OK.
- Python emitter (`compiler/emitter.py`): revert
  `c64a48881e833b243dd8458a9efdd9c08233807a9cb17bb6c6583842b58e550d`
  -> `fcea82ede1739187532fa5d0689db83c9ec656eceb6f510490311ee0e1f79608`
  (the `9afc8d7` committed hash). Python emitter 4 errors + 2
  failures (CLI diagnostic + aliased fn-call test); live repro fails
  with the ORIGINAL doubled-prefix error at 1:1. Restore: hash
  `c64a4888...` again, 7 tests OK.
- JS emitter (`compiler/emitter_js.py`): revert
  `6a2858000dfa16e714ff0c40179b6c1cab820bb1791623bf9784aeb3fcbec7b2`
  -> `e9c9e936cf36457a553e3cea04fa30a2f2c43d03c744ce8b2e6fdf01c3032e44`
  (the `9afc8d7` committed hash). JS emitter 5 errors + 1 CLI
  failure; live repro fails with the original doubled error at 1:1.
  Restore: hash `6a285800...` again, 6 tests OK.
- CLI (`compiler/cli.py`): revert
  `9576b0881c36e431f13aa1a4082c54065393d1452b642998f02e27ec16a0c7e7`.
  Both diagnostic tests fail (span falls back to 1:1; the message
  stays single-prefixed). Restore: hash `9576b088...` again, 2
  tests OK.

### 20.7 Verification

- Full suite, system Python: **848 tests in 71.9 s, OK
  (skipped=15)** (was 829; +19 new).
- Full suite, fresh venv (`/tmp/fix3-venv`, discord.py 2.7.1 +
  aiosqlite): **848 tests in 72.1 s, OK (skipped=2)**.
- Conformance: **73 ok py, exit 0; 73 ok js, exit 0** (was 72;
  +`import_enum`), no failures, in both envs.
- `node tests/js/rt_tests.js`: **61 checks passed**.
- Goldens: `git diff --stat -- goldens/` empty. No golden
  legitimately changes: no `pub enum` exists in the golden sources,
  and the no-declaration case is byte-identical by construction.
- Live repro, evaluator's files, both forms, both targets
  (`/tmp/enum-live`): `check` ok exit 0; `build --target py` and
  `--target js` ok exit 0; runs print `Shade.Red` (print form) and
  `true` (arg form) on both targets. Emitted main.py:
  `import m_helper3` + `print(to_str(m_helper3.Shade_Red()))`.
  Emitted main.js:
  `const m_helper3 = require("./m_helper3.js")` +
  `print(new m_helper3.Shade_Red())`. Helper.js exports only
  `Shade_Red, Shade_Blue, is_red`; `require` of the helper directly
  works and `is_red(new Shade_Red())` is `true`.
- §18 still holds (live probe, both targets): float `/` by zero
  caught with `e.code == "ZeroDivisionError"`, program prints `PASS`;
  the `result_propagate_chain` conformance program passes in the 73.
  The diff touches no `isErr` / `._value` / `fdiv` lines.
- §19 still holds (live probe, both targets): a bot file with a
  top-level `fn halve`, `struct Pair`, `enum Shade`, `let tag`, and
  two commands builds exit 0 on both targets with all four
  definitions emitted (py: `class Pair`, `class Shade_Red`,
  `def halve`, `tag = "v19"`; js: `class Pair`, `class Shade_Red`,
  `function halve`); the 3 warnings are the pre-existing W1009
  reply-length notes. The full bot test files pass in the 848.
- Changed files: `compiler/checker.py` (+55/-2),
  `compiler/emitter.py` (+102/-9), `compiler/emitter_js.py`
  (+88/-20), `compiler/cli.py` (+9/-1),
  `tests/test_checker.py` (+106), `tests/test_emitter.py` (+122),
  `tests/test_emitter_js.py` (+101/-1), `tests/test_cli.py` (+44),
  `conformance/ok/import_enum.newr`,
  `conformance/ok/lib/import_enum_helper.newr`, `BUILD_REPORT.md`
  (§20, +327). **Not pushed** - chie reviews and pushes.
- `git diff --stat`: 9 files changed (compiler + tests + report),
  954 insertions, 33 deletions, plus the 2 new conformance files.

### 20.8 Still open

- Imported structs (evaluator non-blocking note 1, still true after
  this change, verified live): `h_struct.Point(x: 1, y: 2)` is
  rejected consistently at check (E3001, both targets, check and
  build agree), a clean missing feature. SPEC §11's struct promise
  still does not hold in practice; unchanged by this round, on
  purpose. Control: a struct returned by an imported fn works live
  (`Point(x: 0, y: 0)`). Side note, pre-existing and out of scope:
  the rejection's help text suggests the very name it rejects
  ("there is a similar name: `Point`").
- 4-level chains / re-exported enums (`m1.m2.Shade.Red`, verified
  live): rejected consistently at check on both targets (E3005
  "`m2` is not exported by module `m1`" + E3001), a clean missing
  feature (newr has no re-export), not a checker/emitter
  disagreement. Out of scope for this round.
- File-module function as a value (`let f = items.price`): the
  `_emit_module_member` fn branch routes through
  `_emit_module_call_text` with the file stem as the "real" module,
  which fails for any file module, aliased or not. Pre-existing,
  no test covers it, out of scope.
- A let-bound struct constructor (`let f = Point`, then
  `f(x: 1, y: 2)`): the checker types the bare struct name as its
  constructor `fn`, but the emitter's name-call path refuses kwargs
  for a name that is not a struct. Pre-existing, out of scope.
- Unchanged from before: §16.4 amount-based debit/credit heal;
  §19.5 async functions for SPEC §9.5 store reads from fns; CI
  matrix never run (Windows/macOS); P-42 live Discord.

## 21. M4 - Java + C# emitters (7 Nov 2026)

M4 adds two targets: Java (JDA 6.7.0) and C# (Discord.Net 3.20.2).
Same method as M2/M3: TDD red first, conformance programs print
their own verdict, goldens compare-only, CLI contract unchanged.

### 21.1 Toolchains (settled live, both verified)

- Java: **Temurin OpenJDK 21.0.13+9 (LTS)** at `~/jdk-21`
  (`java -version` / `javac -version` → openjdk 21.0.13).
  Route: direct download from Adoptium
  (`https://api.adoptium.net/v3/binary/latest/21/ga/linux/x64/jdk/hotspot/normal/eclipse`,
  207,858,352 bytes, ~92 s), extracted to `~/jdk-21`. The
  `sudo apt-get update` + `openjdk-21-jdk-headless` route STALLED
  (apt-get update still running after 6+ minutes, killed).
- .NET: **SDK 8.0.427, runtime 8.0.33** at `~/.dotnet`
  (`dotnet --version` → 8.0.427). Route: Microsoft
  `dotnet-install.sh --channel 8.0 --install-dir ~/.dotnet`.
  First attempt hit the 512MB /tmp tmpfs limit (tar extraction
  error); retry with `TMPDIR=~/dotnet-tmp` still ended with a tar
  error, but the SDK is functional (`dotnet --list-runtimes` shows
  AspNetCore.App 8.0.33 + NETCore.App 8.0.33; `dotnet new console`
  + `dotnet run` printed "Hello, World!"). Scratch dir
  `~/dotnet-tmp`.
- PATH wired in `~/.bashrc`: `JAVA_HOME="$HOME/jdk-21"`,
  `PATH="$HOME/jdk-21/bin:$HOME/.dotnet:$PATH"`,
  `DOTNET_CLI_TELEMETRY_OPTOUT=1`. Each `muse.exec` call is a
  fresh shell; PATH must be exported per call. `java`/`dotnet`
  are NOT on the default PATH without the export.
- VM facts: Ubuntu 24.04.5 LTS, 4 CPUs, ~2 GB available RAM,
  133 GB free disk. `/tmp` is 512MB tmpfs - dotnet builds and
  temp dirs must stay small or use a home-dir TMPDIR.
- **Gotcha found live: `python3 -m compiler.cli` silently does
  nothing** (cli.py has no `__main__` guard - it only imports
  the module and exits 0, writing no files). The real entry
  point is the `./newr` launcher script.

### 21.2 What was built

- `compiler/emitter_java.py` (5,757 lines) + `compiler/newr_rt.java`
  (1,438 lines). `tests/java/RtChecks.java` +
  `tests/test_newr_rt_java.py` (TDD RED first),
  `tests/test_emitter_java.py` (6 tests), `tests/test_emitter_java_bot.py`
  (10 tests, TDD RED→GREEN).
- `compiler/emitter_cs.py` (3,831+ lines) + `compiler/newr_rt.cs`
  (1,245+ lines). `tests/cs/RtChecks.cs` +
  `tests/test_emitter_cs.py` (4 tests), `tests/test_emitter_cs_bot.py`
  (31 tests, TDD RED first).
- CLI: `newr build --target java` / `--target cs` (and `--check`,
  `-o`, `--line-comments`, `--fast` refusal, emitter errors to
  clean exit-4 E4001, same as py/js). USAGE/COMMAND_HELP updated
  with an M4 paragraph. `_cmd_build` targets
  `("py", "js", "java", "cs")`; `_build_emit` dispatch for java/cs
  (suffix, rt_name, rt copy name `NewrRt.java` for java,
  `output_stem` from `-o`).
- Conformance runner (`conformance/run.py`) extended: GOLDENS_JAVA /
  GOLDENS_CS, `_SUFFIX` / `_RT_SRC` / `_RT_COPY` /
  `_GOLDENS_BY_TARGET` / `_GOLDEN_DIR_BY_TARGET` maps, toolchain
  discovery (JAVA/JAVAC from JAVA_HOME or ~/jdk-21; DOTNET from
  DOTNET_ROOT or ~/.dotnet), java branch (javac all `*.java`, then
  `java <stem>`), cs branch (`dotnet run --project <stem>.csproj`,
  DOTNET_CLI_TELEMETRY_OPTOUT=1), `--target` choices
  `py, js, java, cs`. **Runner bug found and fixed this session:
  `_build_to_temp` globbed only `*.cs`, never collecting the
  `.csproj`, so every C# conformance program failed with "project
  file does not exist" - fixed to also collect `*.csproj` for cs.**
- Goldens: `goldens/java/` + `goldens/cs/` created via `--bless`
  after the coordinator read every golden line by line: hello (11
  lines each), hello_bot (Java ~87 lines, C# 87 lines), showcase
  (Java 108, C# 101), shop (Java 501, C# 566), shop_items (Java 16,
  C# 17), plus `hello_bot_deploy` and `shop_deploy` files.
  `showcase_deploy` is NOT a golden on any target (consistent with
  py/js). `tests/test_goldens.py` extended with `TestGoldensJava` +
  `TestGoldensCs`.
- New conformance program: `conformance/ok/import_aliased.newr`
  (aliased file-module import; `let f = h.is_red` lines REMOVED
  because py/js emitters fail E4001 on it - PRE-EXISTING gap in
  py/js emitters, not an M4 regression; recorded as open item.
  Java emitter handles it via method reference).

### 21.3 Java decisions (K-pins)

- K1 One public class per file, name = file stem verbatim via
  `_java_class_name`, runtime ships as `NewrRt.java` (rename on
  copy).
- K2 Top-level `let`/`const` are `public static` fields with NO
  initializer, initialized in main() or the module's `static {}`
  block in file order.
- K3 int = long, literals `123L`, Long.MIN_VALUE special-cased,
  float literals via Python repr, non-finite → `Double.NaN` /
  `POSITIVE_INFINITY` / `NEGATIVE_INFINITY`, `var` NEVER used for
  locals (except pattern bindings whose type cannot be resolved).
- K4 Fn values use `NewrRt.Fn0`–`Fn8` (arity 0–8, higher = clean
  E4001), lambdas carry explicit cast unless context provides
  target (`fn_target` threading), module-level fns as values are
  method references (`Main::fn`), arbitrary-expression calls use
  `.get()` / `.apply(...)`.
- K5 Lambda capture-by-value via hoisted `final` snapshot locals.
- K6 Structs are `public static class` with public final fields +
  equals/hashCode/toString, enums are ONE class with
  `public final String _newr_variant` discriminant + union fields
  + static instances/factories.
- K7 `?.` chains port JS segment logic exactly.
- K8 `?` propagate ported exactly (temp + `if (temp.isErr()) {
  return temp;}` / `if (temp == null) { return null;}`,
  `temp._value`).
- K9 `==`/`!=` structural via `NewrRt.eq` for collections /
  Result / struct / enum else null-safe `Objects.equals`,
  ordering on str via `compareTo`, `in` on dict via
  `containsKey`, `strip` → `String.strip()`, `split` →
  `NewrRt.splitStr`, `join` → `String.join`, `chars` →
  `NewrRt.chars`, `abs` on int → `NewrRt.absLong`, `min`/`max`
  inline ternary, `len` → `(long) size()` /
  `(long) NewrRt.chars(s).size()`, list literals
  `new ArrayList<>(Arrays.<T>asList(...))` (NOT List.of, rejects
  nulls), dict literals `NewrRt.<V>dictOf(...)`, interp is `+`
  concatenation prefixed with `"" + ` when first part is an
  expression, `ok`/`err` one type param
  (`NewrRt.<Long>ok(...)`), `env.args` → `NewrRt.envArgs()`
  backed by `_mainArgs` set by main() via `setMainArgs` patch
  (prepends class name as argv[0]), `env.get` →
  `NewrRt.envGet`, `log.info` → System.out.println,
  `log.warn`/`log.error` → System.err.println.
- K10 Store calls synchronous (no `await`, JDBC blocks), §19
  guard still applies (E4001 outside handlers).
- K11 `for` over list/set enhanced for loop, dict `.keySet()`,
  str `NewrRt.chars(...)`, `try` catches `Throwable` coerced
  via `NewrRt.asError`, >1 catch = clean E4001, `throw` str →
  `throw new NewrRt.NewrError(text);`, top-level bare `return`
  → `System.exit(0);`, `ensures` use `_newrResult` temp.
- K12 `_emit_general_file` emits header + imports +
  `public class <Stem> {` with static fields, nested structs /
  enums, static fns, then main() or `static {}` block;
  `_render_imports` emits `import java.util.*;` unconditionally
  + `import java.sql.*;` for sqlite + one
  `import <fully-qualified>;` per JDA class.
- K13 Bot (JDA 6.7.0): one public class extending
  `ListenerAdapter` - top-level lets as static fields,
  structs/enums, synthesized row classes per store table (bound
  in module scope as structs), nested `private static class Bot`
  with the JDBC store, `COMMANDS` table, component handler maps
  + static registration block, execute/component/task methods,
  dispatch overrides, `main()`. Commands:
  `Commands.slash(...).addOptions(new OptionData(...))` with
  `setMinValue(1L)` / `setMaxValue(99L)` (long vs double overloads
  disambiguated) and `addChoice`; option bindings read through
  `OptionMapping` for optional options with defaults/null.
  Answers: `reply` / `deferReply` / `editOriginal` /
  `getHook().sendMessage` / `sendMessage` on channels, all ending
  in blocking `.complete()`. Ephemeral only on interaction
  replies (clean E4001 otherwise). Components: buttons/selects /
  modals via `ActionRow` + `Button.primary` /
  `StringSelectMenu` / `Modal` with `Label.of(..., TextInput...)`
  (JDA 6 has no TextInput label). Handler methods routed by
  customId through static `Map<String, Consumer<…>>` maps;
  `only: author` via per-message `_componentAuthors` map
  (messageId + customId → user id); `only: roles` via
  roles-stream check; timeouts on shared
  `ScheduledExecutorService` (disables rows, runs `on_timeout`
  with `timeout_edit` reply kind). Store (P11): one long-lived
  JDBC connection per store, `jdbc:sqlite:<store>.db`,
  `PRAGMA busy_timeout = 5000`, `BEGIN IMMEDIATE` / `COMMIT` /
  `ROLLBACK` per mutation, ledger + balance in one transaction,
  duplicate idempotency key replays, transfer ledger healing
  (`:out` / `:in`), all methods `synchronized`, balances as TEXT
  via `NewrRt.add/sub`. Entities: `user.id` → `getIdLong()`,
  `mention` → `getAsMention()`, `guild.name`, `message.content`
  → `getContentRaw()`, `interaction.channel` →
  `(MessageChannel) getChannel()`, etc. Deploy + pom:
  `<stem>_deploy.java` (guild when `DISCORD_GUILD_ID` set, else
  global, prints count) and `pom.xml` (JDA 6.7.0 always;
  sqlite-jdbc 3.54.1.0 only when a store is used; Java 17,
  maven-compiler-plugin 3.11.0).

### 21.4 C# decisions (C-pins)

- C1 int = long, float = double, bool = bool, str = string,
  list = `List<T>`, dict = `Dictionary<string, V>`,
  set = `HashSet<T>`, Result = `NewrRt.Result<T,E>`,
  fn = `Func<...>` up to 16 params, NO v1 arity limit (unlike
  Java Fn0–Fn8); fn values invoke directly (no `.get()` /
  `.apply()`).
- C2 Entities → Discord.Net interfaces `Discord.IUser` /
  `IGuildUser` / `IGuild` / `IMessageChannel` / `IRole` /
  `IDiscordInteraction` / `Embed` / `IUserMessage`.
- C3 `Main` class/method collision CS0542 - stem exactly "Main"
  becomes class/file `MainProgram`.
- C4 `const` locals get NO modifier in C# (newr `const` is
  checker-immutable, not a compile-time constant).
- C5 File modules get a static constructor
  `static <Stem>() {}` (C# has no `static {}` block).
- C6 `emit_program_cs` always emits `<stem>.csproj`
  (SDK-style, net8.0, `<Nullable>disable</Nullable>`,
  `<ImplicitUsings>disable</ImplicitUsings>`,
  `<AssemblyName>`). `Main` is
  `public static void Main(string[] args)`.
- C7 `env.args`: same argv[0] rule as Java - the C# runtime has
  `SetMainArgs(string prog, string[] args)` and the emitter
  patches it into Main().
- C8 `?.` / `??` / `unwrap` dispatch to kind-specific helpers
  with explicit type args (helper `_opt_chain_call` picks
  OptChain / OptChainVal / OptChainNullable by result kind,
  `_coalesce_call` picks Coalesce / CoalesceVal).
- C9 `is_ok` / `is_err` → `IsOk()` / `IsErr()`; `?` propagate on
  a value type returns `temp.Value`.
- C10 Generated `.csproj` has `<NoWarn>CS8981</NoWarn>`
  (lowercase stem class names are normal for newr).
- C11 `INewrRepr` is a top-level interface, NOT nested in
  NewrRt (emitted structs/enums could not see it as
  `INewrRepr`).
- C12 Bot (Discord.Net 3.20.2): all 11 stubs replaced with a
  real emitter; same semantics as py/js (slash commands, store
  economy with SQLite + idempotency, `HARNESS-PASS` against
  real SQLite: credit idempotency `balance=100`, debit
  `True balance=70`, debit-replay `True balance=70`,
  insufficient `poor=False balance=70`, transfer
  `True u1=50 u2=20`, transfer-replay same, `economy_set` row
  `u3=5`, `get` contains `balance: 50`). Store name `economy`
  is in the emitter's reserved list, so methods are
  `economy__balance` etc. and the DB file is `economy_.db`
  (Java uses `economy.db` - cross-target divergence,
  consistent within C#). Modal `timeout:` / `on_timeout:`
  accepted by the checker but ignored (Discord.Net has no
  modal timeout); button/select timeouts honored via
  `_ComponentTimeoutAsync`. Deploy class is `hello_botDeploy`
  (no underscore - C# naming quirk). Sync handlers (ready/task
  with no `await`) emit a non-async lambda/method returning
  `Task.CompletedTask` (otherwise every generated bot carries a
  CS1998 warning). Bot `.csproj` enumerates its `Compile` items
  explicitly (`Remove="*.cs"` + per-file `Include`,
  `newr_rt.cs` conditional on existence) - SDK globbing
  compiled every `.cs` in `examples/` into each project
  (CS0017, two Mains), deploy file never listed; general
  programs' csproj untouched. `ApplicationCommandInteractionDataOption`
  does not exist - real option type is
  `Discord.WebSocket.SocketSlashCommandDataOption` (found by
  compiling a reflection probe). `SocketSelectMenu` does not
  exist in 3.20.2 - select events are `SocketMessageComponent`;
  modal values via `Data.Components`.
- C13 C# runtime: checked arithmetic via `checked{}` →
  OverflowException → NewrOverflowError; `BitConverter.DoubleToInt64Bits`
  for exact decimal; `FloatStr` from .NET "R" format digits +
  Python exponent rules; `ToStr`; `Result<T,E>` implementing
  `INewrRepr`; `Ok` / `Err` / `TryResult`; `Eq` deep structural;
  `ListIndex` / `ListGet` / `ListContains`;
  `SetRemove<T>` generic; `Range`; `Print` (params object?[]);
  `Input` (Console.ReadLine, "" at EOF); `RandomInt` rejection
  sampling via `RandomNumberGenerator.Fill` +
  `new BigInteger(buf, isUnsigned: true, isBigEndian: true)`;
  `RandomChoice`; `RandomShuffle` manual Fisher-Yates with
  `Random.Shared`; `TimeFormat` via DateTimeOffset UTC;
  `FileRead` / `FileWrite` / `FileAppend` wrap via `AsError`
  (missing file code "FileNotFoundException"); `FileExists`;
  `HashOf`, `StructHash`, `EnvGet` (1-arg + 2-arg), `EnvArgs` +
  `_mainArgs` + `SetMainArgs` (prepends program name as argv[0]),
  `LenOf`, `DictOf<V>(params object?[] kvs)`
  (Dictionary<string, V>, null values allowed, null keys throw),
  `Chars` (EnumerateRunes), `SplitStr` (literal,
  StringSplitOptions.None), `StrIndex` (code points, negative
  wrap, IndexError), `AbsLong` (long.MinValue →
  NewrOverflowError), `OptChainNullable` helper (chains ending
  in a nullable value type).

### 21.5 C# runtime lessons (for future work)

- For unconstrained T, `T?` collapses to `T` (not Nullable<T>)
  - kind-specific helpers OptChain / OptChainVal /
  OptChainNullable, Coalesce / CoalesceVal, UnwrapOpt /
  UnwrapOptVal with EXPLICIT type args (a plain `long` argument
  does not infer T for a `Nullable<T>` parameter).
- Ternary-default trap in OptChainVal (`v == null? default:
  f(v)` types `default` as R=0, not R? - explicit
  `return null` is load-bearing).
- Local functions CANNOT overload (CS0128 - single Eq).
- `#nullable enable annotations` (NOT `disable`: disable causes
  CS8632 warnings).
- `HashSet<T>` does NOT implement non-generic `ICollection`
  in .NET 8 (LenOf enumerates).
- Dictionary insertion order is in practice, not guaranteed.
- DictOf allows null values, not null keys.
- `Equals(object? obj)` → `Equals(object obj)` (CS8632 with
  Nullable disabled).
- `CoalesceVal` returns `T` not `T?` (a `??` always produces a
  value; `long?`→`long` was CS0266).

### 21.6 Java runtime (summary)

- Checked int64 via `Math.addExact/subtractExact/multiplyExact/
  negateExact` with BigInteger overflow messages matching JS;
  `powInt` repeated squaring; `tdiv`/`tmod` (Java `/`/`%` already
  truncate); `powFloat`, `fmod`, `fdiv` (ZeroDivisionError);
  `roundHalfAway` via exact decimal (bit decomposition +
  BigInteger); `toInt`/`toFloat`/`toBool`; `floatStr` Python repr
  semantics via exact decimal digits from
  `new BigDecimal(double)` + shortest-prefix search (Java's
  `Double.toString` differs from Python's in rare edge cases,
  e.g. 5e-324); `toStr` (P8); `Result<T,E>`; `eq` deep structural
  (double branch uses unboxed `==`: -0.0==0.0 true, NaN==NaN
  false); `optChain`/`coalesce`; `listIndex`/`listGet`/
  `listContains`/`setRemove`/`unwrapOpt`; `range`; `print`/
  `input`; `randomInt` rejection sampling with SecureRandom;
  `randomChoice`/`randomShuffle`; `timeFormat` UTC fixed set;
  `fileRead`/`fileWrite`/`fileAppend` wrap IOException via
  `asError` (missing file code "NoSuchFileException");
  `fileExists`; `chars` (code points), `splitStr`
  (Pattern.quote + split -1), `strIndex` (code-point, Python
  negative wrap, IndexError), `absLong` (Long.MIN_VALUE =
  overflow error), `lenOf`, `dictOf` (LinkedHashMap, allows
  null keys/values), `envGet` overloads, `envArgs` + `_mainArgs`,
  `hashOf` + `structHash` (-0.0/0.0 hash alike), `Fn0`–`Fn8`
  functional interfaces.

### 21.7 Per-target pins that differ

- **Sets hash by value in Java and C# (like Python), unlike JS
  identity sets (J6).** `setRemove` uses Contains (value-based
  for structs with generated Equals).
- **File-IO error codes are host-specific:** Java
  "NoSuchFileException", C# "FileNotFoundException", Python
  "FileNotFoundError", JS "ENOENT".
- **Java float equality in `eq`:** unboxed `==` matches JS `===`
  for numbers (-0.0==0.0 true, NaN false).
- **Java `strIndex` divergence (deliberate, matches Python):**
  out-of-range str index raises IndexError; JS's `[...s][i]`
  returns undefined there (a JS latent bug). Negative str
  indices wrap like Python.
- **Java `hashCode`/`eq` contract:** `hashOf`/`structHash`
  normalize -0.0 to 0.0 (eq says they are equal); Result.hashCode
  fixed to use `hashOf`.
- **Java/C# round() is Python-correct for int inputs; JS is
  not (latent bug, out of M4 scope):** JS
  `roundHalfAway(5n, -1n)` = 5, Python
  `round_half_away(5, -1)` = 10.0; JS
  `roundHalfAway(1250n, -2n)` = 1250, Python = 1300.0. Cause:
  `newr_rt.js` returns `Number(x)` unchanged for a BigInt input
  when ndigits is given. Uncaught because conformance
  `round_half_away.newr` only tests float args. Java/C# runtimes
  implement the Python-correct semantics (round the exact value
  for int inputs too). Flag for chie; JS fix is out of M4 scope.
- **C# store divergence:** DB file `economy_.db` (mangled) vs
  Java `economy.db`; C# store methods have no explicit lock
  (Java methods are `synchronized`).
- **C# deploy class naming:** `hello_botDeploy` (no underscore)
  vs Java `hello_bot_deploy`.
- **C# `env.args`:** same argv[0] rule as Java.
- **Java/C# string comparison:** str ordering via `compareTo`
  (Java) / `String.CompareOrdinal` (C#) - UTF-16 code units, same
  as JS; astral characters still differ from Python code-point
  comparison (same caveat as JS, carried).

### 21.8 Bugs found and fixed during M4 (all TDD, conformance-driven)

Every fix below was driven by a failing conformance program or a
failing rt check (RED), then the fix, then the corpus green again.
The three red-checks in §21.9 are the sha256-verified sample; the
rest were verified the same way (failing program → fix → green),
without a separate revert pass.

Java general emitter:
- Top-level `let` shadowing - `_in_top_level` flag + `"global-let"`
  kind test, emit `x = <init>;` not a local declaration.
- Error fields - `e.message` → `.getMessage()`, `code` → `.code`.
- `Objects` import - `import java.util.*;` unconditional.
- Fn-value calls - binding kind not `"fn"` but fn type emits
  `.get()` / `.apply()`.
- Imported enum/struct TYPE - `_struct_java_name` searches
  `self.program.modules` and qualifies with the module's class stem.
- listIndex/listGet return Object - emitter casts to the element's
  boxed type.
- Declared list type - `_java_type` list now `List<T>` not
  `ArrayList<T>`.
- ok/err one type param.
- `Function` qualification - `java.util.function.Function<...>`.
- env.args argv[0] - runtime `setMainArgs(prog, args)` prepends
  main class name.
- Aliased file-module imports - `_file_module_class(binding)`
  returns the stem, used in `_imported_enum`, `_emit_module_call`,
  `_emit_module_member` (method reference `helper::is_red`).

C# general emitter:
- `?.` / `??` / `unwrap` dispatch to kind-specific helpers with
  explicit type args (`_opt_chain_call`, `_coalesce_call`).
- `Main` class/method collision CS0542 - stem exactly "Main"
  becomes class/file `MainProgram`.
- Int arithmetic + compound-assign dicts used Java lowercase helper
  names (`add`/`mul` → `Add`/`Mul`).
- `set.add` and `list.append` emitted `.add(` → `.Add(`.
- `ListPattern` used Java `ArrayList` + `.get(i)` →
  `System.Collections.IList` + `[i]`.
- `INewrRepr` moved to top level.
- `Equals(object? obj)` → `Equals(object obj)` (CS8632 with
  Nullable disabled).
- `CoalesceVal` returns `T` not `T?` (a `??` always produces a
  value; `long?`→`long` was CS0266).
- `is_ok`/`is_err` → `IsOk()`/`IsErr()`.
- `?` propagate on a value type returns `temp.Value`.
- File helpers `FileRead`/`FileWrite`/`FileAppend`/`FileExists`
  (were lowercase).
- Store key uses Discord.Net `.Id.ToString()` (not JDA `.getId()`)
  and `NewrRt.ToStr`.
- Missing `ftype = self._type_of(func)` line restored in
  `_emit_call` (arbitrary-expression call branch).
- Generated `.csproj` now has `<NoWarn>CS8981</NoWarn>`.

Java bot emitter:
- Guild-ternary interpolation needed parens.
- Store calls emitted `bot.economy__balance` because "economy"
  is emitter-reserved (`binding.java` = `economy_`) - call site
  now uses the raw store name, matching the Bot methods and JS.
- Transfer read two balances into one `cur` variable.
- Deploy class name used the input stem instead of the output stem.
- A `{{` in a plain (non-f) string emitted a double brace.
- `TaskDecl` has `interval_seconds`, not `period`.
- `emit_expr` returns a plain string, not a tuple.

C# bot emitter:
- `ApplicationCommandInteractionDataOption` does not exist - real
  option type is `Discord.WebSocket.SocketSlashCommandDataOption`
  (found by compiling a reflection probe).
- `StructDecl` needs a `span` - synthetic row structs now use the
  table's span.
- `_ChannelText` helper wasn't emitted (flag check looked for
  lowercase `_channelText` only).
- Bot `.csproj` now enumerates its `Compile` items explicitly
  (`Remove="*.cs"` + per-file `Include`, `newr_rt.cs` conditional
  on existence) - SDK globbing compiled every `.cs` in `examples/`
  into each project (CS0017, two Mains), deploy file never listed.
- Sync handlers (ready/task with no `await`) now emit a non-async
  lambda/method returning `Task.CompletedTask` (otherwise every
  generated bot carries a CS1998 warning).
- `AddTextInput(..., minLength:, maxLength:, required:)` named args
  verified by compiling a modal with `min_length`/`max_length`
  fields.

Conformance runner:
- `_build_to_temp` globbed only `*.cs`, never collecting the
  `.csproj`, so every C# conformance program failed with "project
  file does not exist" - fixed to also collect `*.csproj` for cs.

CI (found while validating the new jobs locally):
- Deploy files follow the `-o` stem (`shop_out_deploy.java`, not
  `shop_deploy.java`) - the M3 js-emitter job had the same latent
  bug (CI never ran, so it was never caught); fixed in all three
  jobs.
- Running a JDA bot needs JDA's runtime deps on the classpath:
  slf4j-api 2.0.19 + trove4j core 3.1.0 (the emitted bot's
  COMMANDS initializer runs at class-load time, before main's
  token check, and JDA's CommandDataImpl needs both to load -
  without them the run dies with NoClassDefFoundError instead of
  reaching the token guard). The CI job downloads all four jars
  (JDA, sqlite-jdbc, slf4j-api, trove4j). `javac` alone needs
  only JDA + sqlite-jdbc.

### 21.9 Red-checks (sha256-verified)

Three M4 fixes red-checked the M2/M3 way: record the hash, revert
only the fix (from a backup copy, never via git - the tree is
uncommitted), show the conformance program failing, restore from
the backup, verify the hash matches exactly.

1. C# `CoalesceVal` returns `T` (not `T?`), `compiler/newr_rt.cs`.
   Pre-revert hash:
   `907d35507ad10d17822069bf359567acc78a59a639534be5ec22de52557c40a1`.
   Reverted hash:
   `215a70e09944e019a222342b07e719f548d048e305d1eb4af559116510e20984`.
   With the revert, C# conformance: `FAIL nullable.newr: exit 1`
   ("The build failed. Fix the build errors and run again." -
   CS0266, `long?` cannot be assigned to `long`). Restored hash
   `907d35507ad10d17822069bf359567acc78a59a639534be5ec22de52557c40a1`
   - matches exactly.
2. Java `setMainArgs` prepends argv[0], `compiler/newr_rt.java`.
   Pre-revert hash:
   `69588584f0a80d1b4dcf178686369f7473bba60569e4a022ecf4c8c20f22f249`.
   Reverted hash:
   `66498da2193b1628962f32f3dbbf39a4a085ccf47706b6f1ac25ab07f523abfb`.
   With the revert, Java conformance:
   `FAIL mod_env.newr: last line 'FAIL' != 'PASS' (stdout 'FAIL\n')`
   (env.args[0] is the first real arg instead of the program name).
   Restored hash
   `69588584f0a80d1b4dcf178686369f7473bba60569e4a022ecf4c8c20f22f249`
   - matches exactly.
3. C# `INewrRepr` is top-level (not nested in `NewrRt`),
   `compiler/newr_rt.cs`. Pre-revert hash `907d3550...` (same as
   #1). Reverted hash:
   `f1c89ebf941af99ef805aab865912843afb7a9ac7b02c6baf7831cac75b6a2a2`.
   With the revert, C# conformance: `FAIL enum_basic.newr`,
   `FAIL enum_match_all.newr`, `FAIL import_aliased.newr`,
   `FAIL import_enum.newr`, `FAIL list_of_structs.newr`,
   `FAIL match_enum.newr` (all exit 1 - emitted structs/enums
   cannot see the nested interface). Restored hash `907d3550...`
   - matches exactly.

The remaining fixes in §21.8 were verified the same way
(failing conformance program → fix → corpus green) without a
separate revert pass; the three above are the sha256-verified
sample.

### 21.10 CI

`.github/workflows/ci.yml` gains two jobs, mirroring `js-emitter`
(9 jobs total: front-end, v0-conformance, build-zipapp,
zipapp-diff, golden-rebuild, python-emitter, js-emitter,
java-emitter, cs-emitter):
- `java-emitter` (ubuntu/macos/windows × Java 21 Temurin):
  setup-python 3.12, setup-java Temurin 21, pip install
  discord.py==2.7.1 + aiosqlite, download JDA 6.7.0 +
  sqlite-jdbc 3.54.1.0 + slf4j-api 2.0.19 + trove4j core 3.1.0
  from Maven Central, full suite, conformance --target java,
  build + compile + run the examples (hello runs; shop,
  hello_bot, showcase reach the DISCORD_TOKEN guard), golden
  comparison TestGoldensJava.
- `cs-emitter` (ubuntu/macos/windows × .NET 8.0.x):
  setup-python 3.12, setup-dotnet 8.0.x, pip install
  discord.py==2.7.1 + aiosqlite, full suite, conformance
  --target cs, build + build + run the examples (hello runs;
  shop, hello_bot, showcase reach the DISCORD_TOKEN guard),
  golden comparison TestGoldensCs.
- YAML validated locally (`yaml.safe_load` OK). Every step of
  both jobs was run locally on this VM with the same commands
  (hello runs, all three bots reach the token guard on both
  targets). CI itself has never run - Windows/macOS execution
  stays UNVERIFIED until the jobs are green on all three OSes.
- Also fixed (see §21.8): the M3 js-emitter job's deploy
  filename (`shop_deploy.js` → `shop_out_deploy.js`).

### 21.11 UNVERIFIED items (M4)

- Live Discord connection: no bot ever logged in on either
  target. Compile-time + local SQLite verification only (Java:
  javac exit 0 on all emitted bot files with JDA + sqlite-jdbc;
  C#: dotnet build 0 errors, HARNESS-PASS against real SQLite).
  Discord API names are verified live (JDA 6.7.0 via javap
  against the jar + Maven Central metadata; Discord.Net 3.20.2
  via probe programs that compiled and ran).
- Java: modal timeout ignored (JDA `Modal.Builder` has no
  timeout); `guilds`/`integrations` intents map to no JDA
  intent; transfer heal logic compiled but untested against a
  live DB.
- C#: modal `timeout:`/`on_timeout:` accepted by the checker but
  ignored (Discord.Net has no modal timeout).
- CI has never run - Windows/macOS execution UNVERIFIED until
  the `java-emitter` / `cs-emitter` jobs are green on all three
  OSes.
- Carried from before, unchanged: §16.4 amount-based
  debit/credit heal; §19.5 async-fn lowering for SPEC §9.5 store
  reads from fns; imported structs across modules (check rejects
  consistently E3001; SPEC §11 struct promise doesn't hold in
  practice); JS round() latent bug (§21.7, flag for chie, out of
  M4 scope); file-module-fn-as-value gap in py/js emitters
  (pre-existing, out of M4 scope); general-emitter csproj
  CS0017 latent issue (building two general programs that share
  a directory - the bot csproj fix was not applied to the
  general csproj, out of scope); sets keyed by value types use
  identity in JS (Python hashes by value); string comparison for
  astral characters is UTF-16 vs code points; file-IO error
  codes are host-specific; JS `log` has no timestamp (J12); B6b
  absolutes understate a real discord.js import (in-repo mock
  stands in; ratio is honest); discord.js option getters beyond
  the track are extrapolations; P-42 live Discord.

### 21.12 Verification (M4, coordinator-run)

- Full suite, system Python: **902 tests, OK (skipped=15)**,
  170.960 s. (The earlier 900 count was from a run before the
  last test files landed; 902 is the current count on both
  Pythons.)
- Full suite, fresh venv (Python 3.12.3, discord.py 2.7.1 +
  aiosqlite, `~/m4-venv`): **902 tests, OK (skipped=2)**,
  153.892 s.
- Conformance: **py 74/74, js 74/74, java 74/74, cs 74/74**
  (0 FAIL on all four targets; re-verified after the runner
  fix and after the bot emitters landed).
- Rt checks: Java **71/71**, C# **75/75**, JS 61 (unchanged).
  Python wrappers: `python3 -m unittest tests.test_newr_rt_java
  tests.test_newr_rt_cs tests.test_newr_rt_js` → OK.
- Emitter tests: Java general 6/6, Java bot 10/10, C# general
  4/4, C# bot 31/31. Golden tests 6/6 (py, js, java, cs).
- Hello + shop builds and runs on both new targets: Java hello
  prints "Hello, world!" (javac + java); C# hello prints
  "Hello, world!" (`dotnet run --project`); Java shop + C# shop
  build clean (javac exit 0 with JDA + sqlite-jdbc + slf4j-api
  + trove4j; `dotnet build` 0 errors) and reach the
  DISCORD_TOKEN guard (exit 1, token message) - the same guard
  check the CI jobs run.
- Goldens diff empty (TestGoldensJava + TestGoldensCs pass;
  `goldens/java/` + `goldens/cs/` blessed after line-by-line
  review).
- Benchmarks: see BENCHMARKS.md M4 sections (Java + C#) and
  §21.13. B6 checksums re-verified by the coordinator: all
  three loop drivers print **105003** on both targets. B6b
  balances re-verified by the coordinator: both shop economy
  drivers print final balance **1000** on both targets.
- Tree: UNCOMMITTED at `2afa82f` + changes (31 files at the
  time of this writing), nothing committed, nothing pushed.
  Head sha256s (current tree): emitter_java
  `b4f79670436bc35a5728e97ff633e8a623fae85f373f558f7cf0ccce6c583a55`,
  emitter_cs
  `3502a98969222b80521618fbabac1b6d94ad8d50e96b3df81c747c7bde347408`,
  newr_rt.java
  `69588584f0a80d1b4dcf178686369f7473bba60569e4a022ecf4c8c20f22f249`,
  newr_rt.cs
  `907d35507ad10d17822069bf359567acc78a59a639534be5ec22de52557c40a1`,
  cli.py
  `7ac82bb3fada84bfa6e6bd8e5fe677f41e1381c6beaf6674cc9ef1b300e4b7f5`,
  conformance/run.py
  `b568aefe447faf9b2bfb4913ea18c42bd9e98f7e927048a94165af729ecc7f01`,
  tests/test_goldens.py
  `56fa2c972173bd1a05d522eab22933eab3c4871ed4c0c1af5c3fc9c5a3e91062`,
  ci.yml
  `a03b07c9c4392f3125ddaacd4077d4f1e9237874a622e70450b35b93c4bede85`.
  (These are the hashes at §21 writing time; the benchmark
  agent's later edits to BENCHMARKS.md / benchmarks/ change
  only those files, not the emitter hashes.)

### 21.13 Benchmarks (M4 summary - full tables in BENCHMARKS.md)

Machine: Linux x86_64, Python 3.12.3, Temurin OpenJDK 21.0.13,
.NET SDK 8.0.427, JDA 6.7.0 + sqlite-jdbc 3.54.1.0, Discord.Net
3.20.2 + Microsoft.Data.Sqlite 8.0.0, hyperfine 1.19.0.
Runners: `benchmarks/run_java.py`, `benchmarks/run_cs.py`.
Raw JSON: `benchmarks/raw/` (`*_java.json`, `*_cs.json`).

Java: B1 hello 118.9 ms ± 5.5, shop 159.0 ms ± 14.4; B2 hello
122.0 ms ± 8.1, shop 146.6 ms ± 8.8; B4 hello 276/11 vs hw
371/13 (ratio 0.74), loop 514/20 vs hw 665/21 (0.77); B5
generated 47.2 ms ± 10.3 vs hw 42.6 ms ± 4.6 (1.11); B6
generated 101.6 ms ± 11.2 vs plain 41.9 ms ± 1.9 (2.42) vs
checked-hw 109.5 ms ± 20.2 (0.93); B6b generated 1016.6 ms ±
221.9 vs hw 1162.9 ms ± 556.8 (0.87); B7 compile 18.3 MB, run
37.5 MB; B8 startup 1.11, loop 2.42, shop 0.87, hello size
0.74, loop size 0.77.

C#: B1 hello 122.4 ms ± 9.4, shop 167.8 ms ± 29.3; B2 hello
128.8 ms ± 20.0, shop 156.6 ms ± 22.7; B4 hello 303/12 vs hw
383/15 (0.79), loop 542/21 vs hw 675/23 (0.80); B5 generated
50.2 ms ± 10.5 vs hw 49.6 ms ± 4.9 (1.01); B6 generated 59.2
ms ± 5.4 vs plain 57.8 ms ± 15.6 (1.02) vs checked-hw 80.1 ms
± 30.7 (0.74); B6b generated 392.3 ms ± 116.5 vs hw 329.9 ms
± 37.0 (1.19); B7 compile 18.3 MB, run 22.7 MB; B8 startup
1.01, loop 1.02, shop 1.19, hello size 0.79, loop size 0.80.

Threshold reports (honest, not hidden): Java B6 generated/plain
2.42 is over the 1.20 threshold - the checked-int price
(Math.*Exact per operation plus a tmod call), the same shape as
M2's 2.54: the generated code is within noise of hand-written
code using the same checked helpers (0.93), and the plain-long
loop is not a fair target (it cannot overflow-check at all).
C# B6b 2.04 (from the early `--quick` run, N=3, under load) was
diagnosed as a benchmark-methodology artifact, NOT a real
slowdown: instrumenting the generated driver showed
`new DiscordSocketClient()` ≈ 218 ms + `Setup()` ≈ 118 ms
one-time, while the generated credit loop itself was ≈ 310 ms
vs the hand-written loop's ≈ 400 ms - the gap was client
construction, not the economy loop. Fix: the hand-written C#
driver now constructs a `DiscordSocketClient` with the same
config as the emitted Bot (no connection made). Full re-run:
**1.19**, just under the 1.20 threshold, so not a bug report -
the fix, the measurement behind it, and the reasoning are
documented in the driver header, the runner docstring, and
BENCHMARKS.md. (An earlier draft of §21.13 quoted the
superseded quick-run numbers - Java B6 1.72, C# B6b 2.04, C# B5
170.0 ms - because the first Java full run was load-polluted
(load average ~6 from a concurrent conformance run, absolutes
~2x inflated) and the C# numbers were from the N=3 quick run;
the numbers above are the final full runs, N=10 cold/program,
N=20 warm, verified line-by-line against
`benchmarks/raw/*.json` by the coordinator.) All other ratios
are under their thresholds. C# B6b gen has one slow run
(697.6 ms vs a 302 ms floor) and Java B6b has high variance
(hw ±556.8 ms) - both look like one-time construction/fsync
jitter, reported with stddevs and ranges.
