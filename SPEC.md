# newr — Language Specification v1

**Status:** SPEC FROZEN (M0), 6 November 2026.
**Version:** newr 0.1.0 (the language line; distinct from `discord-py-skills` 0.5.0).
**Authors:** chie (Muse) for drewdestar/newr (repo not yet created — LOCAL ONLY).

This document is the contract M1–M6 build against. Changing it later needs a
spec amendment, not a quiet edit. Anything marked **UNVERIFIED** is a claim
nobody has proven yet — never cite it as fact.

## 0. Supersession record — read this first

This spec replaces the bot-language design in
`~/workspace/discord-py-skills-research/newr-language-design.md` (§2–§3) and
the "What newr is NOT" section of `newr-universal-expansion.md` (§1), on the
user's direct orders:

- **21:49 WIB 6 Nov 2026:** newr is a **general-purpose programming language**,
  one language for all — like programming languages in general — **NOT** a
  Discord-bot language. A Discord bot is just ONE use of the language.
  Consequences: `while` and recursion bans are reversed (a general language
  needs them); the core gains types, functions, modules, error handling,
  collections, I/O; `discord` becomes ONE domain module (`import discord`).
- **21:50 WIB 6 Nov 2026:** newr must be **powerful and usable on ALL OSes**
  (Linux, macOS, Windows, …). Portability is a spec chapter (§3), not an
  afterthought.
- **21:58 WIB 6 Nov 2026:** **stability contract** — newr is not Discord-focused.
  If Discord changes, newr must not be affected. Core has zero Discord
  dependencies; Discord lives entirely behind the `discord` module (§2).
- **21:59 WIB 6 Nov 2026:** newr must be **POWERFUL, FASTER, and THINKING**
  (§4 performance, §5 thinking compiler).
- **22:02 WIB 6 Nov 2026:** "Best of all" and "Wishlist" chapters (§23, §24) —
  the best features taken from each major language, and the features other
  languages' developers rejected or deferred, each with a verdict. With the
  honesty correction from the deep research: the chapters record the features
  taken, NOT a "best of all" slogan claim (§23).
- **22:12 WIB 6 Nov 2026:** deep research applied (G1–G20,
  `newr-deep-research.md` §10). The grammar decisions in that research
  (closures, ternary, `?.`/`??`, `|>`, enums, `match`, statement-level `?`,
  `T?` nullability, no implicit numeric coercion, byte-offset spans,
  local/bidirectional inference R1–R11, rustc-form diagnostics) are now
  part of this spec; the superseded pre-research decisions are struck
  through in place below rather than deleted silently. npm is dropped
  from v1 distribution (G13).

Where the old design contradicts this spec, **this spec wins**. Where the old
design's *v0-behavior* facts are concerned (what the current 2,219-line
compiler actually does), the **code wins** — see §25 for contradictions found.

## 1. What newr is

**newr is a general-purpose programming language.** One `.newr` source compiles
to a runnable program in a chosen target language (Python, TypeScript, Java,
C#, Rust, Kotlin, Dart — §18). The language is the product; every library and
every target is a backend.

- A plain program is an ordinary program: `print "Hello, world!"` (§21.1).
- A Discord bot is one program shape: `import discord` + bot declarations
  (§13). The bot syntax from v0 keeps working as sugar over the discord
  module (§13.1).
- A file-I/O tool, a CLI, a game, a bot — all are programs in the same
  language, with the same types, control flow, functions, modules, and
  error handling.

**The promise:** write once, run on any target, with identical behavior,
because every emitter compiles the same AST under the same semantic checks.

**What newr is NOT:**

- Not a transpiler of arbitrary code. It does not translate Python to
  TypeScript or vice versa. It compiles *one* language (newr) *to* N
  languages. There is no "import my existing bot" path.
- Not a promise that every language gets a target. A target exists only where
  a real, maintained library/toolchain exists (matrix §18).
- Not a shell, not a build system, not a package manager for the target
  (the generated project uses its own ecosystem's tooling).

**The honest v1 story (G16).** newr's v1 claim is: **"one readable source,
seven readable outputs, with each target's libraries available."** That is
a *portability* story, and it is the only fame claim v1 makes. "Best of
all" is **not** claimed: every language that became famous won by being
best at *one* thing, not best at everything, and "one language, compiles
to all targets" has a 20-year precedent (Haxe) that stayed niche — the
technology worked and the fame did not follow automatically (§23).
**v1 domain (one, named): small cross-platform command-line tools** —
concrete and demonstrable (file I/O + argv + multi-target fits it), with
AI-assisted code generation as the wave to surf (a language that is easy
for models to write correctly: explicit types, small spec, great errors,
verifiable output). v1 does not claim "general-purpose for all" in its
pitch even though the language is general-purpose technically.
**Fame timeline (planning horizon, §23):** v1 0–1 yr liked; v2 1–3 yr
used to build real things; v3 3–7 yr jobs/teaching; v4 7–15 yr default
somewhere. Any plan promising fame in under 3 years ignores all four
precedents (Rust, Go, TypeScript, Python).

### 1.1 Non-negotiables (carried from v0, and from the repo's own rules)

1. **The small program stays small.** Hello is one line; a hello bot is still
   ~21 lines. v1 adds power without taxing the simple case: every v0 file is
   a valid v1 file.
2. **Build-time proof, not runtime hope.** Every limit the compiler can know
   is enforced at build time (Discord limits in §13, types in §7). A build
   that passes must never fail at run time for a reason the compiler could
   have known.
3. **Errors name file + line + column, with a fix** (§16). Similar-name
   suggestions use the rustc named form ("there is a variable with a
   similar name: `count`"), not the folk "did you mean" wording (the v0
   wording is deliberately changed — verdicts identical); add source
   snippets everywhere.
4. **No silent behavior.** Never silently ignore a construct, never silently
   coerce a type, never silently drop an event. Unknown lines are errors.
5. **No escape hatch in v1.** No inline-Python / inline-TS / inline-anything
   blocks. An escape hatch would let every hard mapping be dodged instead of
   solved, and generated code would stop being regenerable. If a program
   needs something the language cannot say, it drops to the host language
   via the skills — same rule as v0, now enforced by the compiler. The
   FFI/binding story — how newr calls existing target libraries — is
   specified as M1 design work (§20.1): the v1.1 direction is `extern`
   declarations with per-target bindings verified by the compiler; the v1
   interim answer is to extend readable generated code by hand.
6. **The generated code stays readable.** A user must be able to open
   `hello.py` / `hello.ts` and understand it. No minified output, no opaque
   runtime blob.
7. **Handler/source wins.** Every emitter mapping must be proven by a test or
   a live run, never by analogy between target languages (the v3 lesson).

## 2. Stability contract — the language is the stable part

This is the architecture principle the user set on 21:58 WIB 6 Nov 2026.

### 2.1 The core language has zero Discord dependencies

The core language — types (§7), control flow (§9), functions (§9), modules
and imports (§11), error handling (§10), I/O (§12), core stdlib (§12) — **must
not depend on Discord in any way**: no Discord names in core syntax, no
Discord constants in core checking, no Discord concepts in core types.

Discord lives **entirely behind the `discord` module** — one hard module
boundary:

- All Discord rules (command-name rule, description/reply limits, placeholder
  contexts, intents, component limits, embed limits, the "every path answers"
  check) are defined in the discord module's own tables and checked by the
  discord module's own checker pass.
- The core compiler never imports the discord module's tables. In the
  reference implementation, `compiler/checker.py` (core) never imports
  `compiler/discord_limits.py` or `compiler/discord_check.py`; only
  `compiler/discord_check.py` does. A test asserts this boundary (no import
  of `discord_*` from core modules).
- **By-name exemption (deliberate, narrow, tested).** The core names
  the string `"discord"` only to *bypass* Discord — never to validate
  it: in `checker.py` it occurs at exactly two sites (`_module_member_type`
  and `_call_module_member`), both of the form `if mod == "discord":
  return UNKNOWN` — "this module is not core's job; leave it to the
  discord checker". In `resolver.py` it occurs in the `NAMED_MODULES`
  registry (the list of known named modules) and in the W1002 exemption
  (`import discord` does not warn when bot blocks exist, because bot
  blocks implicitly import it). In `modules.py` it is one entry of
  `RESERVED_BUILTIN_NAMES` (a module name a program may not redefine).
  `ENTITY_TYPE_NAMES` in `ast_nodes.py` is a deliberate core-owned list
  of entity type names (`user`, `guild`, `channel`, ...) the core needs
  to type-check member access. These are exemptions from the by-name
  rule, not violations of the module boundary: no Discord rule, limit,
  or constant lives in core — only the name that says "not mine".
  `tests/test_boundaries.py` asserts the exact exemption list.
- A program that never mentions Discord compiles identically whether or not
  the discord module exists in the toolchain.

### 2.2 If Discord changes, only the discord module changes

- If Discord changes a limit, an API, an intent, or a component rule, the
  change lands **only** in the discord module's implementation (its limits
  table, its mappings, its emitter code). The language syntax, the core
  types, and user programs do not change.
- If Discord **removes** something, the module's surface shrinks — recorded
  as a **module breaking change, NOT a language change** (versioned as a
  `discord` module major version, not a language major version). User
  programs that used the removed surface fail the build with a clear error
  naming the removal and the replacement; programs that did not use it are
  unaffected.
- The module absorbs the change so user programs do not have to.

**Concrete example.** Discord changes the command-description limit from 100
to 90 characters. What changes: one number in the discord module's limits
table (`discord_limits.py`), and the golden tests that assert it. What does
NOT change: the language syntax, the `description:` key, the type system, any
user program whose descriptions are ≤ 90 characters (they keep compiling
unchanged), and any program that never uses Discord at all.

### 2.3 The same principle applies to every target

- If Python, TypeScript, Java, C#, Rust, Kotlin, or Dart changes, **only that
  target's emitter changes** — the language and user code stay the same.
- The language is the stable contract; backends adapt. A target's breakage is
  a per-target fix, never a language change, unless the spec itself must move
  (and then it moves by amendment, §0).
- A feature that cannot be expressed identically on all shipped targets does
  not ship in v1 — it waits, with a version attached.

## 3. Portability — one language, all OSes

newr is OS-agnostic by construction. The language and the toolchain must work
on Linux, macOS, and Windows.

### 3.1 Source files

- **Line endings:** source files may use LF or CRLF. The lexer normalizes
  CRLF → LF on read. A lone CR (classic Mac line ending) is an error.
  Spans are **raw-file byte offsets** (§16): the lexer counts the CR
  bytes, so a CRLF newline is 2 bytes in a span even though it is 1
  character after normalization — JSON offsets match the bytes on disk.
- **Encoding:** UTF-8, with or without BOM (a BOM, if present, is stripped).
  Non-UTF-8 source is an environment error (exit 2) naming the file.
- **Tabs:** banned for indentation (v0 rule, kept). A tab anywhere in leading
  indentation is an error.

### 3.2 Paths

- **The portable separator is `/`.** Path literals in source (import paths,
  file I/O paths) are written with `/`. The runtime normalizes `/` to the
  host OS separator (this is the `pathlib` rule: the language speaks `/`, the
  runtime speaks the OS).
- **No hardcoded separators in language semantics.** No construct in the
  language assumes `/` or `\` as a filesystem fact. Path *values* are
  strings; path *joining* is done by the runtime, not by string
  concatenation in user code (the `file` module takes path strings and
  normalizes them).
- **Relative paths resolve against the current working directory** for file
  I/O, and against the importing file's directory for `import "path.newr"`.
  Absolute paths work per OS (a Windows absolute path `C:/...` is valid
  source on Windows; it is not portable source, and the compiler does not
  pretend otherwise).

### 3.3 Case sensitivity — explicit rule

- **Module names are case-sensitive on every OS, including Windows and
  macOS.** `import "Shop.newr"` and `import "shop.newr"` are different
  modules. On a case-insensitive filesystem the compiler resolves by exact
  name match against the directory listing and errors if the on-disk name
  differs in case from the import string ("imported `shop.newr` but the file
  on disk is `Shop.newr` — module names are case-sensitive").
- Identifiers, keywords, and locale tags are case-sensitive everywhere.

### 3.4 No OS-specific syscalls in the core language

- **No Unix-only features in v1:** no `fork`, no signals, no Unix
  permissions, no `/proc`, no shell.
- **No subprocess in v1.** There is no `exec`/`shell`/`system` builtin.
  (Rationale: process spawning is the least portable surface in the
  language; a program that needs it drops to the host language. Revisit in
  v1.1 with a portable `process` module if demanded.)
- **Environment variables:** `env.get(name)` / `env.get(name, default)` is
  the only env access, and it is specified portably (§12).
- **File I/O:** `file.read/write/append/exists` are specified portably (§12):
  text files are UTF-8, newlines in written text are normalized to the
  host OS line ending by the runtime (the language writes `\n`; the runtime
  translates), and reading normalizes line endings to `\n`.

### 3.5 Build output — byte-identical on all OSes (DECIDED)

- **`newr build` output is byte-identical on every OS.** Generated code uses
  **LF line endings always**, on Linux, macOS, and Windows alike.
  (Reason: golden files must be identical everywhere, and every target
  toolchain accepts LF. A Windows developer's editor and git handle LF
  without complaint; a generated file that changed line endings per OS would
  make golden tests OS-dependent, which is exactly the drift this spec
  refuses.)
- Source input may be CRLF; output is always LF. This is a compiler
  guarantee, tested in CI on all three OSes.

### 3.6 Distribution — how the compiler reaches every OS

- **Primary distribution: PyPI `newr-lang`** (name verified free 6 Nov 2026
  via the PyPI JSON API). Python 3.10+ on Linux, macOS, and Windows.
  `pip install newr-lang` gives the `newr` command on all three OSes.
- **Single-file distribution: zipapp.** CI builds a single-file zipapp
  (stdlib `zipapp`, `#!/usr/bin/env python3` shebang) from the real package,
  published as `newr`. Same one-file UX as v0, real modules inside.
- **The `newr` CLI must work from PowerShell and cmd on Windows, not only
  bash.** This is satisfied by the pip console-script entry point (pip
  creates the Windows executable wrapper automatically) and by the zipapp
  shebang path on Unix. No `.bat`/`.cmd`/`.ps1` launcher is needed — and
  none is shipped, so there is no second launcher to drift.
- **Standalone binary (PyInstaller/Nuitka): a post-v1 decision, NOT assumed.**
  Reasons it is not the v1 distribution: binary size (tens of MB for a
  stdlib-only compiler), antivirus false positives on Windows for
  PyInstaller binaries, a per-OS build matrix for every release, and the
  zipapp already gives one-file distribution. Revisit after v1 ships, with a
  measured size/startup comparison, if a user asks for it.

### 3.7 Honest limit — Windows/macOS execution is UNVERIFIED here

This VM is Linux-only. **Windows and macOS execution of the compiler and of
generated programs is UNVERIFIED in this run** and must never be claimed as
verified from a Linux-only run. The proof is the CI matrix
(ubuntu-latest / macos-latest / windows-latest, §17.3): the matrix passing is
the completion condition for the cross-platform claim. Design is
portable-by-construction (§3.1–§3.5); evidence comes from CI.

## 4. Performance — FASTER

### 4.1 Compile speed: small projects feel instant

- `newr build` on a small project (a handful of files, the hello bot) must
  feel instant — well under a second on a normal machine. The front end is
  stdlib-only Python with no heavy imports; there is no startup cost beyond
  the interpreter.
- **Incremental compilation + caching, designed from the start.** The v1
  compiler may ship the simple form (rebuild everything; small projects are
  fast enough that this is honest), but the design is fixed now so M4 can
  land it without breaking the CLI contract:
  - Each source file is hashed (SHA-256 of its bytes). The compiler caches
    the parse+resolve+check result per file, keyed by `(content hash,
    compiler version)`, in `.newr/cache/` inside the project directory
    (never in the source tree's versioned files; `.newr/` is git-ignored by
    the scaffold).
  - The import graph is part of the cache key: a file is re-checked when its
    own hash changes **or** when any file it imports (transitively) changes.
  - Cache misses and cache corruption never fail a build — a corrupt or
    stale cache entry is discarded and the file is rebuilt (the cache is a
    performance aid, never a correctness dependency).
  - `newr build --no-cache` forces a full rebuild (for CI and for
    debugging). Cache behavior is observable: `newr build --stats` prints
    files checked vs files reused from cache (M4).
- **Two modes (G14).** `newr check` runs the full front end (parse +
  resolve + full type check) and emits nothing. `newr build --fast` emits
  with only parse + minimal checking (syntax + name resolution, skipping
  the full type check) — an explicit opt-in, and the compiler warns that
  `--fast` output is not fully verified.
- **Startup target (a design target, not a measured claim):** `newr --help`
  in under 100 ms cold, via lazy imports. A persistent compiler daemon is
  a later escape hatch if startup ever becomes the bottleneck.
- **What is UNVERIFIED:** no compile-time numbers exist yet. The first
  benchmark (hello + shop, cold vs cached, on the CI matrix) lands in M2
  with the numbers recorded honestly in BUILD_REPORT.md. Until then, "fast"
  is a design goal, not a measured claim. No compile-time figure from any
  other compiler is cited here (see §25, U2/U3).

### 4.2 Generated code must be efficient

- Emitters produce **idiomatic, non-wasteful code per target**: no
  unnecessary wrappers, no pointless allocations, no generic runtime blob
  that every generated program must carry. The rule is: **generated code
  must be within a reasonable distance of the performance of equivalent
  hand-written code** — a reviewer reading the generated `hello.py` should
  not find a slower way to do the same thing.
- Concretely: no per-call overhead beyond what the target idiom needs
  (a reply is one send call, not a dispatch through three layers); store
  operations use one transaction, not one per statement; string building
  uses the target's normal mechanism (f-strings / template literals /
  `format!` / `StringBuilder` where the target idiom calls for it).
- **M2+ MUST benchmark** the hello and economy (shop) examples per target
  against hand-written equivalents and record the numbers honestly
  (startup time, time per command, memory at idle) in the target's
  BUILD_REPORT. A benchmark that shows generated code materially slower than
  hand-written code is a bug report, not a footnote.
- **UNVERIFIED until measured:** every performance number. No emitter may
  claim to be fast before its benchmark exists.

### 4.3 Fast startup for built programs

- A built program starts fast: **no heavy runtime bootstrap.** Generated
  programs import only what they use (the Python emitter emits only the
  imports the program needs — v0 already does this; the rule is kept for
  every target). There is no newr runtime library that must load before
  `main()` runs; the only shared code is the small per-target helper set
  the emitter writes into the generated file itself.
- The compiler itself starts fast for the same reason (stdlib only, no
  heavy imports at CLI startup).

## 5. Thinking compiler — THINKING

The compiler reasons about the program. It does not guess.

### 5.1 What "thinking" means in v1

1. **Type inference.** The user writes fewer annotations; the checker
   infers. `let price = price_of(item)` infers `int` from the function
   signature; `let xs = [1, 2]` infers `list[int]`. An annotation is required
   only where the type cannot be inferred (an empty collection literal with
   no context, a value from a dynamic source). Ambiguity is an error, never
   a guess (§7.4).
2. **Smart diagnostics.** Every error carries file + line + column, a source
   snippet, a machine code, and a hint (§16). Where the fix is unambiguous
   and safe, the diagnostic also carries a machine-readable `fix` (a span
   and a replacement string) — e.g. an unknown name with exactly one close
   match, an unknown placeholder with exactly one close match, a command
   name with an uppercase letter (fix: lowercase it). The `fix` is data in
   the diagnostic; applying it is a separate, explicit command (§5.2).
3. **`newr check` reasons about the program.** Beyond type errors, the
   checker reports reasoning warnings (they do not fail the build):
   - **unused import** (W1002) — a module imported but never referenced;
   - **unused variable / parameter** (W1001) — a `let`/`const`/parameter
     never read (a variable written and never read is almost always a bug).
     Parameters of a `pub` function do not warn: they are the function's
     public contract, and an unused one is an API choice, not a bug
     signal (decided 6 Nov 2026 during M1, when the `pub fn price_of`
     fixture showed the rule firing on idiomatic public API);
   - **unreachable code** (W1003) — statements after `return`/`throw`/
     `break` in the same block, and statements after a statement that
     always terminates (including after an infinite loop);
   - **dead branch / always-true-or-false literal condition** (W1005) —
     an `if`/`elif`/`while` whose condition is a constant literal
     (`true`/`false`), so one branch can never run; narrow syntactic
     cases only — no general constant folding in v1;
   - **shadowing** (W1004) — a name that shadows an outer-scope name; warn,
     do not forbid (shadowing is legitimate in many languages);
   - **integer-division pitfall** — only when both operands are integer
     literals flowing into a `float` context; labelled newr-specific, not
     a standard lint (no standard lint for this was found);
   - **suspicious comparison** — comparing values of incompatible types
     (this is a type error, not a warning: `1 == "a"` is rejected, because
     it is never what the user meant).
   Assignment in a condition (`if x = 1`) is a *parse* error for free:
   assignment is a statement in newr, not an expression, so it cannot
   appear inside a condition at all.
   `newr check <file>` runs the full front end and prints errors + warnings;
   `newr build --check` runs the same front end plus emit + verify.
4. **Human-readable errors.** Messages are written for the person reading
   them, in plain words, naming the thing and the fix ("command `buy` may
   not answer on some path — every branch of this `if` must end in a
   reply"), not in compiler jargon. Error messages are English in v1
   (localizing them is future work, as in v0).

### 5.2 `newr fix` — scope decided for v1

- `newr fix` applies the safe, unambiguous fixes from §5.1(2) to the source
  files. It only applies a fix when the diagnostic's `fix` field says the
  fix is **unambiguous** (exactly one candidate) and **safe** (a pure rename
  or a pure deletion of a provably-dead token — never a change to program
  meaning). It prints every change it makes, and it refuses to run on a file
  with any error that has no safe fix (it fixes what it can and reports the
  rest — it never half-fixes silently).
- **`newr fix` is NOT in M1.** M1 designs the diagnostic `fix` field;
  in M1 only the resolver attaches one (the similar-name rename,
  E3001) — the checker does not attach fixes yet. The `fix` command
  itself lands in M4 (tooling), once the diagnostics are proven. M1's
  CLI is `build` / `build --check` / `check` / `help` (§19).

### 5.3 What "thinking" is NOT — explicit boundary

- **No AI model inside the compiler.** No LLM, no model weights, no network
  call, no "smart" completion. The compiler reasons from the code in front
  of it, using types, scopes, and the import graph — never from a model.
- **No auto-generating code.** The compiler never writes program logic on
  the user's behalf. `newr fix` applies mechanical renames; it does not
  invent statements, bodies, or handlers.
- **Nothing that guesses user intent.** If the compiler cannot determine
  what the user meant, it says so (an error naming the ambiguity) instead of
  picking a meaning. A compiler that hallucinates a meaning is worse than
  one that asks.
- The boundary is: **reasoning from code is in; guessing intent is out.**

**The compiler / LSP / AI boundary (verbatim from the deep research,
§6.5).** v1 COMPILER promises (testable): lexer/parser/resolver/checker;
local + bidirectional inference (§7.4); structured diagnostics (§16); the
v1 checker list (§5.1). LSP — later milestone (M2+, not M0/M1): reuse the
v1 checker as a library; `publishDiagnostics`, `hover`, `completion`,
`definition`, `codeAction` (apply the compiler's machine-applicable
fixes). No new *reasoning* in the LSP. Do not promise
rename/refactoring, inlay hints, or semantic tokens in the first LSP cut.
Explicitly OUT OF SCOPE for the compiler, forever: natural-language
explanation of errors; code generation from prompts; suggesting a
design/algorithm; judging business correctness; auto-fixing ambiguous
errors without user confirmation; anything requiring a model, a network
call, or training data. The compiler must work offline,
deterministically, with stdlib only. **Things a v1 compiler can NEVER
do:** (1) understand programmer intent — both fixes type-check, only the
human knows which is right; (2) know whether code is "correct" in the
business sense — types prove *shape*, not *meaning*; (3) prove absence
of *all* bugs — runtime errors in the target (e.g. division by zero,
network failure) are outside the source type system unless the language
models them; (4) resolve genuine ambiguity without an annotation — `[]`,
overloaded intent, same-type swapped arguments are
information-theoretically ambiguous; the honest response is a precise
"annotate here" error, not a guess.

## 6. Lexical rules

- **UTF-8 text, `#` comments, blank lines ignored.** A file is blocks; a
  block opens with a line ending in `:` and its body is indented **2 spaces
  per level** (tabs banned, exactly as v0). Blocks end by dedent. Indent
  levels are multiples of 2; an odd indent or a dedent to a level that is
  not on the indent stack is an error.
- **Keywords are English** (as in every mainstream language); **names and
  strings are any language** — Indonesian, Japanese, Arabic, Thai, emoji —
  same as v0.
- **One statement per line** for declarations and statements; expressions
  may span lines only inside brackets (`[...]`, `{...}`, `(...)`).
- **Semicolons: none.** Newlines are the statement separator, as in v0.
- **Line endings:** LF or CRLF accepted (§3.1); lone CR is an error.
- **String literals:** double-quoted only. Escapes: `\"`, `\\`, `\n`, `\t`,
  `\r`, `\u{XXXX}` (Unicode code point, hex, 1–6 digits). Any other escape
  is an error naming the escape. A string may not span lines (a newline
  inside a string literal is an error — use `\n`).
- **Interpolation:** `"Hello {expr}"` — any expression inside `{...}` (§9.3).
  `{{` and `}}` escape to literal `{` and `}`. An unclosed `{` is an error;
  a stray `}` is an error. (v0's `{user}` placeholders are the same
  mechanism: `user` is a binding in scope, interpolated like any other
  expression — §13.4.)
- **Number literals:** decimal ints (`42`, `1_000` — underscores group
  digits and are ignored), hex (`0xFF`), binary (`0b1010`), floats
  (`3.14`, `1e3`, `1.5e-2`). A leading `0x`/`0b` with an invalid digit is
  an error. `int` is a 64-bit signed integer on every target (balances are
  `int`); a literal outside that range is a build error.
- **Duration literals:** a positive integer immediately followed by `s`
  (seconds), `m` (minutes), `h` (hours), or `d` (days) — `60s`, `5m`,
  `1h`, `1d`. Used by `task every ...` and component `timeout:`.
- **Identifiers:** Unicode letters (any script — Latin, Thai, Devanagari,
  Arabic, …), combining marks, digits (not first), and `_` — the lexer
  decides by Unicode category (L/M/N), mirroring v0's Unicode-correct
  command names. Keywords are reserved and may not be used as identifiers
  (full list in §6.1). An identifier that starts with a digit is an error.
- **Comments:** `#` to end of line. There is no block comment in v1
  (documented; add one only if demanded — a block comment is a lexer
  feature, not a language feature). There is also **no doc-comment
  syntax in v1** (no `///`, no `DocComment` token): a `#` comment
  above a declaration is an ordinary comment, never carried into
  emitted code as a docstring — the emitter's "doc comments →
  docstrings" mapping is vacuous until the doc-comment syntax is
  frozen (M6, §24 idea 14).

### 6.1 Keywords (reserved)

`and` `as` `bot` `break` `button` `catch` `command` `const` `continue`
`do` `elif` `else` `embed` `enum` `ensures` `event` `false` `finally`
`fn` `for` `from` `if` `import` `in` `let` `log` `match` `modal` `none`
`not` `only` `or` `pass` `pub` `reply` `requires` `return` `select`
`send` `store` `strings` `struct` `task` `throw` `true` `try` `while`

Contextual words that are **not** reserved (usable as identifiers, but with
their block meaning when they appear in the block position): `description`
`ephemeral` `option` `choices` `default` `required` `min` `max` `style`
`timeout` `id` `url` `roles` `author` `anyone` `values` `table` `ledger`
`field` `inline` `title` `color` `intents` `on_timeout` `text_input`
`defer` `edit` `followup`. (A name that collides with a contextual word in
its own position is a build error naming the collision.)

## 7. Types

### 7.1 Type table (frozen at M0 — anything not here waits for v1.1)

| Type | Meaning |
|---|---|
| `int` | 64-bit signed integer. |
| `float` | Double-precision float. |
| `str` | Unicode string. |
| `bool` | `true` / `false`. |
| `list[T]` | Ordered list of `T`. |
| `dict[str, T]` | String-keyed dictionary of `T`. |
| `set[T]` | Unordered set of `T` (no duplicates). |
| `T?` | A value of type `T`, or `none` — the ONE nullability spelling (§7.2). There is no `option[T]` spelling. |
| `Result[T, E]` | `ok(v)` or `err(e)` — the value form of an error (§10). Exists in v1 as a type, but is NOT the primary error model (exceptions are). |
| `fn(T1, ...) -> R` | A function type — first-class functions and closures (§9.5). |
| `error` | An error value (a message + an optional code). |
| `none` | The type of the `none` literal (absence). |
| `duration` | A duration literal (`60s`, `5m`, `1h`, `1d`) — valid only in `task every ...` and `timeout:` (§6). |
| `<StructName>` | A user-defined struct type (§8.5). |
| `<EnumName>` | A user-defined enum type (§8.6). |
| `user`, `member`, `guild`, `channel`, `role` | Discord entity types (§13.5). Only the fields the language exposes may be read. |
| `interaction` | The interaction in a command/component handler (§13.5). |
| `embed` | An embed value built by an `embed:` block (§13.7). |
| `message` | A message in a `message` event handler (§13.5). |

There is **no `any`**. A type error is a build error with the span.

### 7.2 Type rules

- **No implicit numeric coercion (G7, DECIDED — Gleam-style).** `1 + 2.5`
  is a type error; write `float(1) + 2.5`. `let x: float = 5` is an error;
  write `float(5)` or `5.0`. `1 == 1.0` is a type error (compare like with
  like — §5.1). Every mismatch is an error naming both types and the span.
  Rationale: removing coercion removes a whole class of cross-target
  drift (each target coerces differently); explicit conversion is one
  function call. `/` on two `int`s is truncating int division; `/` on
  floats is float division (§7.5).
- **Collections are invariant in their element type:** `list[int]` is not
  `list[float]`, and `list[int]` is not `list[int?]` — no subtyping in v1.
- **`T?` (exactly one absent value, G19):** `.is_some() -> bool`,
  `.is_none() -> bool`, `.unwrap() -> T` (throws an error if the value is
  `none`). The `or` operator unwraps with a default: `T? or T -> T`
  (`env.get("HOME") or "/tmp"`). A `T?` used where `T` is expected is a
  type error with a hint to unwrap it. Command options that are not
  `required` and have no `default` bind as `T?`. `env.get(name)` returns
  `str?`. `store.get` returns `RowType?`.
- **Function types:** a function's type is its signature
  `fn(param types) -> return type`. Functions ARE first-class values in
  v1 (§9.5): they can be stored in variables, passed as arguments, and
  returned. Closures capture **by value** (a snapshot at creation — this
  matches value-type semantics and avoids loop-variable capture bugs).
- **`void`:** there is no `void` type. A function with no `->` return type
  returns `none` (its body must not `return <expr>` with a value).

### 7.3 Entity field access (discord types — the complete list)

Anything not listed here is a build error: `user.id`, `user.mention`,
`user.name`, `user.bot`; `member.id`, `member.mention`, `member.name`,
`member.guild`; `guild.id`, `guild.name`, `guild.member_count`;
`channel.id`, `channel.name`, `channel.mention`; `role.id`, `role.name`;
`interaction.user`, `interaction.guild`, `interaction.channel`,
`interaction.locale`; `message.content`, `message.author`, `message.channel`.
**Deliberately not exposed in v1:** `user.avatar`, `member.roles`,
`guild.channels`, message history — each needs a fetch and a permission
story; add them one at a time with their own verification.

### 7.4 Type inference — local / bidirectional, NOT Hindley–Milner, NOT gradual typing

newr uses **local / bidirectional inference: annotate function signatures,
infer locals from initialisers + expected (contextual) type, with a small
set of deterministic defaults for literals and empty collections**. That is
what Rust, Go, Kotlin, Swift and TypeScript actually do. **Hindley–Milner
is rejected for v1** (cryptic / mis-localised errors, separate compilation
is complicated by global inference, and it fights multi-target emission,
which needs a stable, named signature at module boundaries). **Gradual
typing / `any` is rejected**: newr has no legacy dynamic code, and `any`
would weaken the multi-target promise (every target would need a dynamic
escape hatch, and diagnostics would go silent exactly where the user needs
them). Borrow only from gradual typing: local inference + contextual
typing + best-common-type for literals.

**Inference rules R1–R11 (normative, copied verbatim from the deep
research, §6.6):**

```
R1.  Function signatures are fully annotated: fn name(p1: T1, …) -> R.
     No inference of parameter or return types. Ever.
R2.  Module-level / exported declarations are fully annotated.
     No inference across module boundaries. An imported name's type
     comes from the exporting module's annotations.
R3.  Local: let x = <expr>            → type(x) = synth(<expr>).
     Local: let x: T = <expr>         → check(<expr>, T); type(x) = T.
     synth() is bottom-up; check() is top-down (expected type flows in).
R4.  Literals: int literal → int; float → float; str → str;
     true/false → bool. (No literal polymorphism in v1.)
R5.  Empty collection with no expected type → error E2002
     "type annotation needed" (E0282 analogue). With an expected
     list[T] / dict[str,T], the empty literal takes that type.
R6.  Non-empty list [e1…en]: all ei must have identical type T
     (synth each, then unify by equality only — no subtyping,
     no best-common supertype). Result: list[T]. Else E2003.
     Same for dict[str,T]: keys must be str, values identical T.
R7.  Call: callee's parameter types are the expected types for
     arguments (check each argument against its parameter type).
     Return type is the callee's declared return type.
R8.  if / while condition: check(cond, bool). No coercion.
R9.  Return: check(<expr>, declared return type R).
R10. A variable's type is fixed at declaration. Reassignment with
     a different type is E2001 (mismatched types), not re-inference.
R11. No generics, no subtyping, no `any`/dynamic type in v1.
     list[T] and dict[str,T] are invariant.
```

Implementation note for M1: two functions, `synth(node) -> Type` and
`check(node, expected: Type) -> None`, plus a scope table from the
resolver. No unification solver, no constraint graph. Every `Type` value
carries the source span it came from, so errors can point at both the
expected-type site and the found site. Ambiguity is an error, never a
guess (§5.3).

**Explicitly deferred (normative, from the deep research §6.8):**
Hindley–Milner / global inference; gradual typing / `any`; generics
inference, subtyping, `never` type; full pattern exhaustiveness /
usefulness; borrow/ownership checking; whole-program dead-function
detection; LSP server; AI features (natural-language explanations, code
generation, intent guessing).

### 7.5 Number semantics (G2 — frozen at M0; the most dangerous drift vector)

- `int` is a **signed 64-bit integer on every target**. Overflow of any
  `int` operation is a **runtime error** on every target (checked
  arithmetic is emitted even where the target would wrap or promote —
  e.g. Python's arbitrary precision and JS's f64 must both be constrained
  to the i64 rule).
- `/` on `int` **truncates toward zero**; `%` takes the **sign of the
  dividend**. Division or modulo by zero is a runtime error.
- `float` is **IEEE-754 binary64 (f64)** on every target.
- The conformance corpus (§18.1) MUST test these rules (overflow,
  truncating division, dividend-sign modulo, division by zero) on every
  target — a target that wraps or promotes silently does not ship.

### 7.6 String / Unicode semantics (G8)

- `str` is **UTF-8**. `len(s)` counts **Unicode code points** (not bytes,
  not grapheme clusters).
- **No integer indexing into strings** (copy Rust): `s[i]` is a compile
  error, with a hint to use `.chars()`. `str.chars() -> list[str]` returns
  one code point per element.
- `dict` iteration is **insertion order**; `set` is **unordered**.

### 7.7 Generics semantic (G11 — decided at M0)

Built-in generics (`list[T]`, `dict[str, T]`, `set[T]`, `T?`,
`Result[T, E]`) exist in v1; **user-defined generics are deferred to v2**.
The runtime semantic is: **erasure on Python / TypeScript / Java /
Kotlin; reification on C# / Rust / Dart**. The difference is
**unobservable in v1** because of three rules: (1) no runtime type tests
on generic parameters (there is no `is` operator in v1 at all); (2) no
reflection on generic types in v1 (reflection is deferred); (3) `==` on
collections is structural value equality, not identity. The per-target
mapping is documented here, not discovered per emitter.

### 7.8 Value-type semantics (G12)

- Structs (§8.5) and enums (§8.6) are **value types**: copied on
  assignment, `==` is structural value equality, there is no identity
  (`is` does not exist). `struct` IS newr's value type — there is no
  separate `value` keyword (Valhalla-style semantics, not the JVM
  performance promise: newr promises value *semantics* in v1; flattening
  / zero-allocation is a per-target optimisation, not a language
  guarantee).
- **Struct field assignment is an error** (`p.x = 5` → error: construct a
  new value instead). This is what makes value semantics honest: a value
  cannot be mutated in place behind another variable's back.

## 8. Program structure and declarations

A `.newr` file is a **program**. Top-level statements execute in file order;
declarations (functions, structs, imports, bot blocks) are resolved across
the whole file (forward references are allowed — the resolver pass handles
them). There is no special `main()` function: top-level executable statements
*are* the main program. (Emitters wrap them in a `main` for targets that
require one — Java/C#/Rust — which is the emitter's job, not the language's.)

### 8.1 Top-level items

| Item | Form | Notes |
|---|---|---|
| Import | `import "path.newr"` / `import <module>` | §11 |
| Function | `fn name(params) -> type:` + body | §9.5 |
| Struct | `struct Name:` + field lines | §8.5 |
| Bot block | `bot "Name":` + keys | §13.1 (discord sugar) |
| Command | `command <name>:` + keys/options/`do:` | §13.2 |
| Event | `event <kind>:` + `log:`/`reply:`/`do:` | §13.3 |
| Enum | `enum Name:` + variants | §8.6 |
| Task | `task every <duration>:` + `do:` | Discord module (§13) — NOT core |
| Strings | `strings <locale>:` + `"key": "template"` | Discord module (§15) — NOT core |
| Store | `store <name>:` + `table`/`ledger` | Discord module (§14) — NOT core |
| Statement | any statement (§9) | top-level executable code |

`fn`, `struct`, `enum`, `import`, `bot`, `command`, `event`, `task`,
`strings`, `store` are top-level only — using one inside a body is an
error. **Visibility:** declarations are **private by default**; `pub`
exports them (`pub fn`, `pub struct`, `pub enum`, top-level `pub let` /
`pub const`). Accessing a non-`pub` name from another module is an error
(§11).

### 8.2 The `bot` block (discord sugar — §13.1)

At most one per file; position is free among top-level items (v0 required it
first — relaxed in v1; v0 files still satisfy it). Keys: `description:`
(optional, lands in the generated header) and `intents:` (optional, §13.6).

### 8.3 Commands and events

See §13.2/§13.3. In short: `command <name>:` with `description:` (≤ 100,
default = name), `reply:` (sugar for a one-reply `do:` body), `ephemeral:`,
`option` lines, and/or a `do:` body. `event ready|member_join|member_remove|
message:` with `log:` / `reply:` sugar or a `do:` body.

### 8.4 Tasks (discord module — scheduled bot work, NOT a general scheduler)

`task` is **discord-module surface** (G1): it is the v1 concurrency
primitive for bots, and general programs have **no built-in scheduler in
v1** (a general `task`/`cron` module is future work). The full rule is in
§13 (discord module); in short:

```
task every 1h:
  do:
    log "Hourly shop check ran"
```

- `task every <duration>:` — durations only (`5m`, `1h`, `1d`). **No cron
  expressions in v1** (cron syntax is a second language; a `task` with a
  cron schedule is v1.1).
- The body is a `do:` block. It may use general statements, `store`, and
  `log`. It may use `send` only if the discord module is in scope (§13.8);
  it may never use interaction replies (`reply`/`defer`/`edit`/`followup`
  are errors in a task body).
- Emitted as `discord.ext.tasks` (py) / `setInterval` with a drift guard
  (js) / the target's scheduler for other targets — the emitter's job.
- **Concurrency model (G18):** v1 has **no threads and no shared mutable
  state**. `task` is scheduled work, not concurrency. Value types are safe
  to share (§7.8); structured concurrency (async/await or actors — the
  decision is not yet made) is deferred to v1.1+ with its own design
  document. The thread-safety promise lives here: nothing in v1 can create
  a data race, because nothing in v1 can share mutable state across
  threads.

### 8.5 Structs (user-defined types)

```
struct Point:
  x: int
  y: int
```

- A `struct` block declares fields (`name: type`, one per line). Field
  types must be complete types (§7.1); a struct may reference another
  struct declared in the same file or an imported file (no cyclic struct
  definitions — a cycle is an error).
- Construction: `Point(x: 1, y: 2)` — named arguments, all fields required,
  no positional construction. Field access: `p.x`.
- Structs are value types (copied on assignment, like a record — §7.8).
  There is no inheritance, no methods on structs in v1 (methods are v1.1).
  **Struct field assignment is an error** (`p.x = 5` → error; construct a
  new value instead — §7.8).

### 8.6 Enums (typed, in v1 — G12)

```
enum Color:
  Red
  Green
  Blue

enum Shape:
  Circle(radius: float)
  Rect(width: float, height: float)
```

- An `enum` block declares variants: bare variants (`Red`) or variants
  with **named associated values** (`Circle(radius: float)`).
- Construction: `Color.Red`, `Shape.Circle(radius: 1.5)` — the enum name
  qualifies the variant. Enum values are value types (§7.8).
- `match` (§9.6) on an enum is exhaustiveness-checked: every variant must
  be covered or a `_` arm must be present, else a build error.

## 9. Statements and expressions

### 9.1 Statements

| Statement | Form | Notes |
|---|---|---|
| `let` | `let name[: type] = expr` | Mutable binding; type inferred or annotated (§7.4). |
| `const` | `const name[: type] = expr` | Immutable; must have an initializer; reassignment is an error. |
| Assignment | `name = expr` / `name += expr` / `-=` / `*=` / `/=` | Augmented assignment is sugar for `name = name <op> expr`. |
| `if` / `elif` / `else` | `if expr:` / `elif expr:` / `else:` + blocks | Condition must be `bool`. |
| `for` | `for name in expr:` + block | Iterates `list[T]`, `set[T]`, `dict[str, T]` (keys), or `range(...)`. |
| `while` | `while expr:` + block | **INCLUDED in v1.** The v1 design's ban is reversed (see §9.4). |
| `try` / `catch` / `finally` | `try:` + block, `catch name:` + block, `finally:` + block | §10. |
| `try` expression | `try <expr>` | §10 — returns `Result[T, error]`; `ok(v)` / `err(e)` build one. |
| `?` propagation | `let x = expr?` / `return expr?` / `expr?` | §10 — statement-level ONLY; a nested `?` (inside a call or a larger expression) is an error. |
| `match` | `match expr:` + arms `pattern [if guard]:` + block | §9.6 — statement in v1; the expression form is v1.1. |
| Contracts | `requires <expr>` / `ensures <expr>` as the first statements of a `fn` body | §9.7 — runtime-only; `result` is bound in `ensures`. |
| `throw` | `throw expr` | §10. |
| `return` | `return [expr]` | In `fn` only (a value must match the declared return type; bare `return` returns `none`). |
| `break` / `continue` | bare | Inside `for`/`while` only. |
| `pass` | bare | No-op (useful for an intentionally empty body). |
| `print` | `print(expr, ...)` | §12.1 — a builtin call, written as a statement. |
| `input` | `let s = input("Name? ")` | §12.1 — a builtin call. |
| `log` | `log expr` | §12.1 — info level; `log.info/warn/error(msg)` calls also exist. |
| `reply` / `defer` / `edit` / `followup` / `send` | §13.8 | Discord handler statements only. |
| `button` / `select` / `modal` | §13.9 | Discord component declarations, handler bodies only. |
| Expression statement | `expr` | A call or any expression, evaluated for its effect. |

### 9.2 Expressions (precedence, loosest → tightest — FROZEN at M0)

| Level | Operators | Notes |
|---|---|---|
| 0 (loosest) | ternary `cond ? a : b` | C-style; `?` is lexed as distinct tokens `?` / `?.` / `??` |
| 1 | pipeline `\|>` | left-associative; `a \|> f` = `f(a)`; `a \|> f(b, c)` = `f(a, b, c)` — the piped value is the FIRST argument; RHS must be a Name or a Call (Elixir-style, frozen) |
| 2 | `??` | `a ?? b`: `a` must be `T?`; `b` may be `T` (result `T`), `T?` (result `T?`), or `none` (result `T?`); `??` may not mix with `and`/`or` unparenthesized (parse error) |
| 3 | `or` | |
| 4 | `and` | |
| 5 | comparison `==` `!=` `<` `<=` `>` `>=` `in` | non-associative — `a < b < c` is an error; parenthesize it |
| 6 | additive `+` `-` | |
| 7 | multiplicative `*` `/` `%` | |
| 8 | power `**` | right-associative |
| 9 | unary `-x`, `not x` | `-x` binds with operand precedence 6, so `-x ** 2` = `-(x ** 2)`; `not` binds with operand precedence 3, so `not a == b` = `not (a == b)` |
| 10 (tightest) | postfix `.name`, `[expr]`, `(args)`, `?.` | |

- **Ternary IS in v1** (G12): C-style `cond ? a : b` — the most widely
  recognized form. (The pre-research decision to exclude it is reversed;
  the old reason — Go has no ternary — is not a reason to remove a
  construct every other target has.)
- **`?.` / `??` — the swallow rule, settled (G12, the rule Python's PEP
  505 could not agree on):** `?.` only short-circuits on `none` (an
  absent value). A missing attribute/field is still a **compile error**
  (fields are statically known). `x?.name` where `x: T?` → the field type
  wrapped in `?`; `a?.b.c()` short-circuits the whole postfix chain after
  the `?.` (TypeScript behavior). `?.` on a NON-nullable receiver is a
  **compile error** (`E2001`): a non-nullable value is never `none`, so
  the chain is dead code and almost certainly a typo for `.`. `?[]` is
  NOT in v1 (rare case; `?.` + explicit handling covers it).
- **String `+` is concatenation; `*` is repetition** (`"ab" * 3` →
  `"ababab"`). `+` on a `str` and a non-`str` is a type error — use
  interpolation or `str(x)`.
- `in`: `x in list` / `x in dict` (key) / `x in set` / `x in str`
  (substring) → `bool`.
- Indexing: `list[int]`, `dict[str]`. **String indexing is a compile
  error** (`s[i]` → use `.chars()` — §7.6). Out-of-range indexing is a
  runtime error (an `error` value thrown — §10).
- Calls support positional and named arguments: `f(a, b, key: v)`.
  Named arguments must match the declared parameter names; a duplicate
  argument (positional + named for the same parameter) is an error.

### 9.3 String interpolation

`"Hello {expr}"` interpolates any expression whose type is `int`, `float`,
`str`, or `bool` (or `T?` of those — `none` renders as an empty
string? **DECIDED: no.** A `T?` in interpolation is a type error
with a hint to unwrap it — silently rendering `none` as `""` is exactly
the silent behavior §1.1(4) forbids). Any other type is a build error with
a hint to convert explicitly (`str(x)`).

### 9.4 `while` and recursion — the bans, reversed, with reasons

The v1 bot-design banned `while` and recursion because handlers run
against a 3-second / 15-minute interaction token and an unbounded loop is
the most likely way to hang a handler. **Both bans are reversed for the
general language**, on the user's order (21:49 WIB 6 Nov 2026):

- **`while` is in v1.** A general-purpose language without `while` is not
  general-purpose. The handler-hang risk is real but it is a *runtime*
  concern, not a language concern: a `while` loop in a handler body is
  allowed, and the 3-second token still applies at runtime (the bot answers
  late or the interaction expires — the same as in the host language). The
  compiler does not bound loops in v1; a loop-bound rule is future work
  (and would be a warning, not a ban).
- **Recursion is in v1.** All targets support recursion natively. A
  function may call itself (directly or through other functions); the
  resolver detects the cycle and allows it (recursion is not an import
  cycle). Target note: Python's default recursion limit applies to
  generated Python code (documented in the Python target page); other
  targets use their native stack.
- The old handler-hang concern moves to the **discord checker as a
  WARNING** (G3): an unbounded loop in a handler body warns (the
  interaction token expires after 15 minutes), but it is not a ban — the
  core language does not bound loops.
- The old reasons are preserved here so the reversal is a decision, not an
  accident.

### 9.5 Functions

```
fn price_of(item: str) -> int:
  if item == "brick":
    return 118
  return 0
```

- `fn name(param: type, ...) -> type:` with a body of statements.
- The return type may be omitted (the function returns `none`).
- Every path through a function with a non-`none` return type must end in
  a `return <expr>` of that type (the checker proves it; a missing return
  is an error naming the function and the path).
- **Closures / first-class functions ARE in v1 (G4).** Lambda syntax:
  `fn(params) -> type:` + an indented block, or `fn(params) -> type:
  <expr>` (single-expression body). Function types are `fn(T1, ...) -> R`.
  Closures capture **by value** (a snapshot at creation — §7.2). `fn`
  declarations are top-level only; nested functions use the lambda form.
  A lambda may be stored in a variable, passed as an argument, or
  returned. (The pre-research "no closures" decision is reversed; the old
  reason — emitter lowering — is an implementation cost, not a language
  reason, and every target has closures natively.)
- **Functions are pure-ish:** they may read `store` and call other
  functions, but they may not send replies (only handler bodies reply —
  the checker enforces this). A function may `throw` (§10).
- Parameter names must not shadow the reserved handler bindings (`user`,
  `guild`, `channel`, `bot`, `interaction`, `message`) — those names are
  reserved in handler scope; a function parameter with one of those names
  is an error only if the function is called from a handler... **DECIDED
  simply: those names are reserved as parameter names everywhere** (a
  parameter named `user` is an error, with a hint to rename it). Simple
  beats clever.

### 9.6 `match` — statement in v1 (G12)

```
match shape:
  Shape.Circle(radius: r) if r > 0:
    print "A circle of radius {r}"
  Shape.Rect(width: w, height: h):
    print "A rectangle {w} by {h}"
  _:
    print "Something else"
```

- Arms: `pattern [if guard]:` + an indented body. Patterns: literals,
  `_` (wildcard), a Name (binding), enum variants with bindings
  (`Shape.Circle(radius: r)`), struct patterns (`Point(x: <pat>,
  y: <pat>)`), fixed-length list patterns, or-patterns (`1 | 2 | 3`).
- **Exhaustiveness:** an enum scrutinee missing variants without a `_`
  arm is an **error**; a non-enum scrutinee without a `_` arm is an
  **error**. (v1 does set-comparison only; full usefulness checking is
  v2 — §7.4 deferred table.)
- The expression form of `match` is v1.1; in v1 `match` is a statement
  (assign inside the arms instead).

### 9.7 Contracts — `requires` / `ensures`, runtime-only (G12)

```
fn withdraw(balance: int, amount: int) -> int:
  requires amount > 0
  requires amount <= balance
  ensures result >= 0
  return balance - amount
```

- `requires <expr>` / `ensures <expr>` are the **first statements** of a
  function body (the checker errors if they appear after other
  statements). `result` is bound in `ensures` (the return value).
- Violations **throw**. Contracts are **always evaluated in v1** (no
  debug/release split, no violation-handler / continuation-mode /
  "assume" semantics — that semantic swamp is what sank C++ contracts).
- The hard part is only defining when checks run; the cross-target
  lowering is trivial (Python `assert`, TS `assert`, Java explicit
  `if throw`, C# `ArgumentException`, Rust `assert!`, Kotlin
  `require`/`check`, Dart `assert`).

## 10. Error handling — unchecked exceptions (primary) + `Result` (value form) (DECIDED, G5)

**Decision: `try` / `catch` / `finally` with `throw` is the primary error
model (unchecked exceptions).** `Result[T, E]` exists in v1 as a *value*
form for code that wants to handle errors as values, with a `try`
expression and a statement-level `?` — but pervasive `Result` is v2, not
the primary model.

```
try:
  let content: str = file.read("data.txt")
catch err:
  log "Could not read data.txt"
  throw err
finally:
  log "done"
```

**Reasons for try/catch as the primary model:**

- Six of the seven targets (Python, TypeScript, Java, C#, Kotlin, Dart)
  have native exceptions with the same shape; try/catch maps to them
  directly, with no wrapper type and no per-call plumbing.
- `Result` as the *primary* model is idiomatic in exactly one target
  (Rust) and awkward in the other six; choosing it would make the common
  case verbose everywhere to serve one target.
- The Rust emitter lowers try/catch to a `Result`-based helper (a runtime
  helper function in the generated Rust code that runs a closure returning
  `Result` and matches on `Err`). The exact lowering is **UNVERIFIED until
  M5** (the Rust emitter proves it); the spec records the approach, not the
  code.

**Rules — statements:**

- `try:` takes a block. `catch <name>:` takes a block; the name binds the
  caught `error` value in that block. `finally:` takes a block. At least
  one of `catch` / `finally` is required (a bare `try:` is an error).
- `throw <expr>` throws an error. The expression must be `str` (becomes an
  error with that message) or `error` (re-thrown as-is). Any other type is
  a type error.
- `error` values have `.message -> str` and `.code -> str?`.
- **Builtins that fail throw** (they do not return sentinel values):
  `file.read` of a missing file, `file.write` to an unwritable path,
  `input` on a closed stream. `economy.debit` on insufficient funds is the
  **one exception** — it returns `bool` (§14), because the v1 design's
  rule ("the error is a value the handler branches on") is kept for
  economy: a failed debit is a normal branch, not an exceptional one.
  Indexing out of range throws. `T?.unwrap()` on `none` throws. Division
  by zero throws.
- An uncaught error in a command handler is answered by the generated
  error handler with the generic message (v0 behavior, kept).

**Rules — the value form (G5):**

- `try <expr>` (single-expression form) returns `Result[T, error]`:
  it Ok-wraps the expression's value, and catches a thrown error into
  `err(e)`. `ok(v)` / `err(e)` are builtins that build a `Result`.
  When inference is ambiguous, an explicit `Result[T, E]` annotation is
  required (the rule Rust could not agree on for 10+ years is decided
  here: **yes, Ok-wrap the final value; yes, require an explicit
  annotation when ambiguous; no heterogeneous blocks in v1**).
- `Result[T, E]` methods (minimal, v1): `.is_ok() -> bool`,
  `.is_err() -> bool`, `.unwrap() -> T` (throws on `err`),
  `.unwrap_or(default: T) -> T`.
- **Statement-level `?` only** — the Go-blog lesson, learned in advance:
  `expr?` where `expr` is `Result[T, E]` or `T?`: on `Err`/`none`, return
  early from the enclosing function (the function's return type must be
  compatible: `Result[?, E]` or `?`); on `Ok`/`Some`, unwrap to `T`. `?`
  may ONLY appear as the **outermost postfix of a statement's RHS**
  (`let`/`const`/assign/expr-statement/`return`) — the parser enforces
  this (`parse_expr` never handles `?`; statement parsers check for a
  trailing `?` and wrap in a `Propagate` node). A nested `?` (e.g.
  `f(g()?)`, `f()? + 1`) is an **error**: a `?` hidden inside a nested
  expression hides an early return from view, and hidden control flow is
  exactly what §1.1(4) forbids.
- `Result` as the **primary** error model (pervasive `Result`, no
  exceptions) is v2 — not v1.

## 11. Modules and imports

- `import "path.newr"` — a file module, resolved at build time from a path
  **relative to the importing file**. It binds the module under its **file
  stem**: `import "shop_items.newr"` binds `shop_items`, and its names
  are accessed **qualified**: `shop_items.price_of(...)`. There is no
  merged namespace (the pre-research merged namespace is replaced — it
  made name collisions silent and cross-module reasoning impossible).
- `import "path.newr" as <name>` — an explicit alias for the bound name.
- `import <name>` — a named module: a stdlib module (`math`, `time`,
  `random`, `file`, `env` — §12) or a domain module (`discord` — §13).
  Named modules are resolved by the compiler, not by the filesystem.
- **Visibility (G9):** declarations are **private by default**. `pub`
  exports them (`pub fn`, `pub struct`, `pub enum`, top-level `pub let` /
  `pub const`). Accessing a non-`pub` name from another module is an
  error naming the declaration and the module that owns it.
- **Bot/command/event/task declarations must live in the main file** (the
  file passed to `newr build`). Imported files contribute functions,
  structs, enums, `strings`, and `store` tables (multi-file commands are
  v1.1).
- **No cycles.** An import cycle is an error naming the cycle chain
  (`a.newr -> b.newr -> a.newr`).
- **Duplicate top-level names are errors** (within one module; the same
  name in two different modules is fine — that is what qualified access
  is for).
- **No wildcards** (`import *` does not exist).
- An imported file must itself be a valid v1 program. There is no package
  registry for `.newr` modules in v1 (a later version can add `newr
  install` for shared modules).
- Imported files are compiled with the importing file (one program, one
  build). The cache (§4.1) tracks the import graph.
- **Manifest (`newr.json`) — DRAFT schema, compiler support in M4:**
  `{ "name": str, "version": str, "target": "py" | "js" | ..., "default_locale": str, "db_path": str }` — `name` and
  `version` are required; `target`, `default_locale`, and `db_path`
  are optional. (Secrets NEVER go in it — the token stays in `.env`.)
  The schema is a draft until M4 decides it (the M4/M6 decision in
  §25 records the same deferral); v1 resolution needs no manifest —
  relative paths only. `newr init` (M4) scaffolds it. (Recorded as "G9
  partially applied; manifest deferred to M4 with reason" in
  BUILD_REPORT.md.)

## 12. Standard library

The stdlib is **modules**, not builtins-in-the-language. Core modules are
available on every target; domain modules are available where their domain
exists. The rule for adding a builtin later: it must be needed by at least
two of the repo's own example programs, and it must have identical semantics
on all shipped targets.

### 12.1 Core builtins (available without import)

| Builtin | Signature | Notes |
|---|---|---|
| `print` | `print(expr, ...)` | Writes the values space-separated + newline to stdout. |
| `input` | `input(prompt: str = "") -> str` | Reads one line from stdin (blocking). |
| `len` | `len(x) -> int` | `str` (characters), `list`, `dict`, `set`. |
| `str` | `str(x) -> str` | Converts `int`/`float`/`bool` to their text form. |
| `int` | `int(x) -> int` | From `str` (parses; throws on invalid) or `float` (truncates). |
| `float` | `float(x) -> float` | From `str` or `int`. |
| `bool` | `bool(x) -> bool` | From `str` (`"true"`/`"false"`) or `int` (0 → false). |
| `min` / `max` | `min(a, b) -> T` / `max(a, b) -> T` | Same-type numbers (or `str`, lexicographic). |
| `abs` | `abs(x) -> T` | Numbers. |
| `round` | `round(x) -> int` / `round(x, n: int) -> float` | |
| `range` | `range(n) -> list[int]` / `range(a, b) -> list[int]` / `range(a, b, step) -> list[int]` | Half-open `[a, b)`. |
| `ok` / `err` | `ok(v) -> Result[T, error]` / `err(e) -> Result[T, error]` | §10 — build a `Result` value. |
| `log` | `log expr` (statement) / `log.info/warn/error(msg)` | Goes to the program's logger. |

### 12.2 Core modules (import by name)

Named modules are available **by name without an import** — `math.sqrt(2)`
works with no `import` line. Writing `import math` is still allowed (and
is the documented style when a file leans on a module heavily); it changes
nothing about what is checked. The one exception is `discord`, which is
imported implicitly by bot blocks (§13.1) and is not core-checked (§2.1).

| Module | Members | Notes |
|---|---|---|
| `math` | `sqrt(x) -> float`, `floor(x) -> int`, `ceil(x) -> int`, `pi -> float`, `e -> float` | `pi`/`e` are constants, not functions. |
| `time` | `now() -> int` (unix seconds), `format(ts: int, fmt: str) -> str` | `fmt` is a small fixed set (`"%Y-%m-%d"`, `"%H:%M"`, `"%Y-%m-%d %H:%M"`, `"%d/%m/%Y"`, `"%H:%M:%S"` — not `strftime`, which is a footgun across locales). |
| `random` | `int(a: int, b: int) -> int`, `choice(list) -> T`, `shuffle(list)` | Seeded by the runtime, not user-seedable in v1. |
| `file` | `read(path: str) -> str`, `write(path: str, content: str)`, `append(path: str, content: str)`, `exists(path: str) -> bool` | Text files, UTF-8 (§3.4). `read`/`write` throw on failure (§10). Paths use `/` (§3.2). |
| `env` | `get(name: str) -> str?`, `get(name: str, default: str) -> str`, `args -> list[str]` | The only env access (§3.4). `env.args` is the command-line arguments **including the program name at index 0** (the Python/Go/Rust/Node convention). |

**`json` is v1.1, not v1.** `json.parse` cannot be typed honestly without
an `any` type (the spec has none, §7.1), and typing it as
`dict[str, str]` would be a lie. It arrives with a typed JSON value (or
with `any`, if a later spec amendment adds one) — documented, not
forgotten.

### 12.3 I/O scope for v1 (DECIDED)

- **In v1:** `print`, `input`, `file.read/write/append/exists`, `env.get`,
  `env.args`.
- **NOT in v1 — HTTP `fetch`.** Reasons: it needs timeouts, secret handling,
  and an allowlist (a language that can phone home to arbitrary URLs is a
  security review, not a builtin); and every target needs an HTTP client
  dependency with different semantics. **Deferred to v1.1 with a dated
  design sketch (G6):** a sandboxed `fetch` with a per-request timeout, an
  allowlist of hosts declared in `newr.json`, secrets never in source
  (env vars only), and a single response type (`str` body + status code).
  Programs that need HTTP in v1 use the host language.
- **NOT in v1 — subprocess/shell, `eval`, crypto builtins, regex.**
  Subprocess: §3.4 (portability). `eval`: never (it defeats the compiler).
  Crypto: a program should never hand-roll hashing/signing — the host
  language's vetted libraries own it. Regex: the classic injection/DoS
  vector in a bot language; revisit with a timeout.
- **No file I/O in the *bot* sense:** a bot's only durable state is
  `store` (§14) — file I/O is for general programs, not for bot state.

### 12.4 String / list / dict / set methods (on values)

| Type | Methods |
|---|---|
| `str` | `upper() -> str`, `lower() -> str`, `strip() -> str`, `split(sep: str) -> list[str]`, `join(list[str]) -> str`, `contains(s: str) -> bool`, `starts_with(s: str) -> bool`, `ends_with(s: str) -> bool`, `chars() -> list[str]` (one Unicode code point per element — §7.6) |
| `list[T]` | `append(x: T)`, `contains(x: T) -> bool`, `len() -> int` (also `len(x)`), `get(i: int, default: T) -> T` |
| `dict[str, T]` | `keys() -> list[str]`, `get(k: str, default: T) -> T`, `contains(k: str) -> bool` |
| `set[T]` | `add(x: T)`, `remove(x: T)`, `contains(x: T) -> bool` |
| `T?` | `is_some() -> bool`, `is_none() -> bool`, `unwrap() -> T` |
| `Result[T, E]` | `is_ok() -> bool`, `is_err() -> bool`, `unwrap() -> T`, `unwrap_or(default: T) -> T` |

`set` construction: `set([1, 2])` (a builtin call on a list). There is no
set-literal syntax in v1 (keeps `{...}` unambiguous for dict literals).

## 13. The `discord` module — one domain module

Everything in this section is **discord-module surface, not core language**.
It lives behind `import discord` and the stability contract (§2.1): if
Discord changes, only this section's tables and checker change.

### 13.1 v0 bot blocks are sugar over the discord module (DECIDED)

**Decision: bot blocks remain as sugar over the discord module.**

- `bot "Name":`, `command <name>:`, `event <kind>:` are declarations of the
  discord module. The parser produces dedicated AST nodes for them
  (`BotDecl`, `CommandDecl`, `EventDecl`); the discord checker validates
  them (§13.2–§13.6); the emitter lowers them to discord-library code.
- Using a bot block **implicitly imports the discord module** (the compiler
  records an implicit import; the user does not have to write it). Writing
  `import discord` explicitly alongside bot blocks is allowed and
  recommended for clarity — it is the same single import, not a duplicate.
- The general-language way to build a bot is exactly this: `import discord`
  (explicit or implicit) + bot declarations. There is no separate "bot
  language" — the bot syntax is the discord module's declaration syntax.
- **Migration path:** there is no migration. v0 files are valid v1 files
  with no changes (§13.10). A future version may add an explicit
  `discord.bot(...)` API; v1 does not need it.

### 13.2 Commands

- **Kept from v0:** `command <name>:` (Discord name rule, unchanged —
  §13.2.1), `description:` (≤ 100, default = name), `reply:` (fixed string
  — sugar for a `do:` body containing one reply), `ephemeral:` on a reply.
- **Options.** `option <name>: <type>` with `(required | default: <lit>,
  choices: [...], min: n, max: n, description: "...")`. Types: `str`,
  `int`, `num` (float — the Discord name for a float option), `bool`,
  `user`, `member`, `channel`, `role`. Limits enforced at build time:
  ≤ 25 options per command, name 1–32 chars, description ≤ 100,
  `choices` ≤ 25, `min`/`max` only on numeric options (`int`/`num`),
  `default` must match the option type. Option values are bound as
  variables of the declared type inside `do:` (an option that is not
  `required` and has no `default` is bound as `T?`).
- **`do:` body.** Statements (§9.1). A command with a `do:` body must
  answer exactly once on every path (the discord checker proves it: every
  branch ends in `reply`/`defer`/`edit`/`followup`, or the build fails with
  "command buy may not answer on some path"). This is the compile-time form
  of the 3-second rule.
- A command must have either `reply:` or `do:` — not both, not neither.
- **Subcommands are v1.1, not v1.** The grammar reserves
  `command <name> <sub>:` but the compiler rejects it in v1 with
  "subcommands arrive in v1.1". Stating this now prevents a half-baked
  implementation.

#### 13.2.1 Command names (v0 rule, kept verbatim)

Command names follow Discord's rule: `^[-_\u02BC\p{L}\p{N}\p{sc=Deva}
\p{sc=Thai}]{1,32}$` with the unicode flag, **lowercase where a lowercase
variant exists**. The reference implementation mirrors discord.py's stdlib
translation (Thai/Devanagari combining marks listed by hand, U+02BC added
explicitly) — the exact regex is in `compiler/discord_limits.py`, ported
from the v0 compiler (`_NEWR_COMMAND_NAME`). An uppercase letter is an
error (with a `fix` to lowercase it — §5.1).

**Fn-name collision (v0 rule, kept):** `foo-bar` and `foo_bar` both
sanitize to `cmd_foo_bar`, so the parser rejects the pair instead of
letting one handler shadow the other.

### 13.3 Events

- **Events in v1:** `ready`, `member_join`, `member_remove`, `message`.
  Each event kind at most once per file (a duplicate is an error).
- Each event block has a `do:` body, or the v0 sugar: `log:` for `ready`,
  `reply:` for `member_join`/`member_remove`. `message` requires `do:`
  (it receives the `message` binding and must answer via `send` — it
  cannot use interaction replies; the checker enforces this).
- `ready` with `reply:` is an error (ready cannot reply);
  `member_join`/`member_remove` with `log:` is an error.
- `message` requires the `message_content` privileged intent (§13.6).

### 13.4 Placeholders are expressions

In v0, `{user}` was a magic name. In v1, **strings interpolate arbitrary
expressions** (§9.3), and the four v0 placeholders remain as **built-in
bindings** in their contexts, so every v0 file still compiles:

| Binding | Context | Type | Notes |
|---|---|---|---|
| `user` | command / member_join / member_remove / message (as `message.author`) | `user` (command) / `member` (member events) | In a command, `user` is the interaction user; in member events, the joining/leaving member. |
| `guild` | command / member_join / member_remove | `guild` | May be absent in a DM — the emitter generates the DM-safe fallback (`interaction.guild.name if interaction.guild else 'DM'` in py; `interaction.guild?.name ?? 'DM'` in js). Entity field access on `guild` is total by this emitter contract (M2 must prove it with the golden files). |
| `channel` | command only | `channel` | |
| `bot` | ready log only | `user` | The bot's own user. |

A placeholder used outside its context is an error naming the allowed
contexts (v0 rule, kept). In the general language this is expressed as:
the binding is simply not in scope outside its context, and the diagnostic
says so ("`{channel}` is not available in a member_join handler — it is
only available in command handlers").

Exposed entity fields: §7.3.

### 13.5 Handler bindings (reserved, in scope inside handler bodies)

`user`, `guild`, `channel`, `bot`, `interaction` (command/component
handlers), `message` (message events), `values: list[str]` (select
handlers), modal field values (modal handlers, bound by field name).
These names are reserved — a user variable may not shadow them in handler
scope (§9.5).

### 13.6 Intents (explicit, derived, validated)

- The `bot` block may declare `intents: [...]`. The compiler computes the
  required set from the constructs used (`member_join`/`member_remove` →
  `members`; `message` event → `message_content`, privileged) and errors
  when the declared set is missing one ("`member_join` needs the members
  intent"). If no `intents:` is declared, the computed set is used
  (v0 derivation, kept).
- Declaring a privileged intent (`members`, `message_content`, `presences`)
  triggers a **warning** naming the 10,000-user rule (Discord requires
  verification/approval for privileged intents in bots in 10,000+ servers —
  the threshold is a Discord-platform rule, not a library rule).
- The generated code declares exactly the computed set in every backend.
  No intent is ever silently on.

### 13.7 Embeds

```
let card: embed = embed:
  title: "Welcome"
  description: "Hello {user}"
  color: 0x5865F2
  field "Rules": "Be kind."
  field "Links": "https://example.com" (inline: true)
```

- An `embed:` block is an expression that builds an `embed` value.
  Fields: `title:`, `description:`, `color:` (an int, 0–0xFFFFFF),
  `field "Name": "Value"` (with optional `(inline: true/false)`).
- Limits enforced at build time (Discord's): title ≤ 256, description
  ≤ 4096, fields ≤ 25, field name ≤ 256, field value ≤ 1024, total
  ≤ 6000 characters, color 0–16777215.
- `reply <embed-expr>` replies with the embed. `reply` accepts `str` or
  `embed` (or a list of embeds — **v1.1**; one embed per reply in v1).

### 13.8 Handler statements

| Statement | Form | Context |
|---|---|---|
| `reply` | `reply <expr>` / `reply <expr> ephemeral` | Command/component handlers. `<expr>` is `str` or `embed`. Length ≤ 2000 enforced with placeholders at max expansion (user 22, guild 100, channel 22 — v0 rule, kept). |
| `defer` | `defer` | Command/component handlers. Shows "thinking", buys 15 min. |
| `edit` | `edit <expr>` | After a `defer` (edits the deferred reply). |
| `followup` | `followup <expr>` | After a reply/defer (a second message). |
| `send` | `send <expr> to <expr>` | `message` events and tasks (sends to a channel). The target expression must be a `channel`. |
| `log` | `log <expr>` | Anywhere (general statement, §12.1). |

`reply`/`defer`/`edit`/`followup` outside a command/component handler
body is a build error. `send` outside a `message` handler or a task body
is a build error. **Handler-hang warning (G3):** an unbounded loop in a
handler body is a warning (the interaction token expires after 15
minutes) — a warning, not a ban; the core language does not bound loops
(§9.4).

### 13.9 Components (inside handler bodies)

```
button "Buy again" (style: primary, id: "buy-again", timeout: 60s):
  only: author
  do:
    reply "Use /buy again, {user}!"
  on_timeout:
    reply "Offer expired."
```

- **Button:** `button "Label" (style: primary|secondary|success|danger|
  link, id: "...", url: "..." for link, timeout: <duration>):` with
  `only: author | roles(["..."]) | anyone` and a `do:` body, plus an
  `on_timeout:` body.
- **Select:** `select "Choose" (options: ["a", "b"], min: 1, max: 1,
  timeout: 60s):` with `only:` and a `do:` body receiving
  `values: list[str]`.
- **Modal:** `modal "Title" (id: "...", timeout: 60s):` with
  `text_input "Name" (style: short|paragraph, required, min_length: n,
  max_length: n):` fields and a `do:` body receiving the field values
  (bound by field name, type `str`).
- Limits enforced at build time: customId ≤ 100 chars, ≤ 5 action rows
  per message, ≤ 5 components per modal, **a timeout is required on every
  collector** (no collector without a timeout — the v0 skill's own rule).
  `only:` defaults to `author` when omitted.

### 13.10 v0 compatibility (DECIDED)

- **Every v0 file is a valid v1 file.** `hello.newr` and `showcase.newr`
  (the two v0 examples) parse, resolve, and check cleanly under this spec.
  They are copied into this repo as `examples/hello_bot.newr` and
  `examples/showcase.newr` and are the conformance baseline for M1.
- The v0 rules kept verbatim: bot name 1–32 chars; exactly one bot block;
  command description defaults to the name; empty description/reply are
  errors; description ≤ 100; reply ≤ 2000 counting placeholders at max
  expansion (user 22, guild 100, channel 22); `ephemeral` default false;
  placeholder contexts; fn-name collision rejection; similar-name
  suggestions in the rustc named form (the v0 "did you mean" wording is
  deliberately changed — verdicts identical); never silently ignore a
  line.
- **Relaxed in v1 (documented, not silent):** the bot block no longer has
  to be first (position free, at most one); top-level statements and other
  declarations may appear in the same file as bot blocks; `event
  member_remove` and `event message` are new kinds.
- **Generated Python for the v0 examples must stay byte-identical** to the
  v0 golden files (`examples/hello.py`, `examples/showcase.py`) — that is
  an **M2** exit criterion (there is no emitter in M1, so M1 cannot prove
  it; M1 proves the front-end verdicts match: both examples parse + check
  cleanly, and every invalid v0 file still fails with the same verdict).
  If M2 changes any byte, the change is recorded with its reason in
  BUILD_REPORT.md. Never silently change output.

## 14. Store and economy (discord module — NOT core)

`store` is **discord-module surface** (G1), not a core-language feature:
it is durable state for bots (balances, ledgers, idempotency keys). A
general program's durable state in v1 is **file I/O** (§12); a general
`db` / `sqlite` module is v1.1. The economy builtins are store operations
of the discord module.

```
store economy:
  table balances (user: str, balance: int)
  ledger true
```

- `store <name>:` declares SQLite-backed tables: `table <name>
  (<col>: <type>, ...)` plus `ledger true/false`. The **first column is the
  key column**. A table's row type is named after the table (a `balances`
  row has fields `user: str, balance: int` — a struct-like type the checker
  knows).
- **Builtins (complete v1 list):**
  - `economy.balance(user) -> int` — requires a `balances` table with
    columns `(user: str, balance: int)`; the checker verifies the shape.
  - `economy.credit(user, delta, key: str)` — add `delta` (may be negative).
  - `economy.debit(user, delta, key: str) -> bool` — subtract `delta`;
    returns `false` (not an exception) on insufficient funds — the error
    is a value the handler branches on (§10).
  - `economy.transfer(from, to, amount, key: str) -> bool` — atomic
    transfer; `false` on insufficient funds.
  - `store.get(table, key) -> RowType?` — `table` is a string
    literal naming a declared table; `key` matches the key column's type.
  - `store.set(table, row)` — `row` is the table's row type.
  - `store.delete(table, key)`.
- **In this list, `store` stands for the declared store's name**: the
  call is `economy.get(...)` for a store declared `store economy:` —
  there is no bare `store` qualifier (a fixed namespace could not
  address two stores with same-named tables). The same applies to
  `economy.balance/credit/debit/transfer` above. (Clarified 6 Nov 2026
  during M1: the spec's `store.get` spelling was ambiguous.)
- **Every mutation takes an idempotency key.** No key = build error. The
  `key:` named argument is mandatory on `credit`/`debit`/`transfer`. This
  is the ledger-as-source-of-truth rule made unskippable: the emitter wraps
  balance+ledger in one transaction; a duplicate key is a replay, not an
  error.
- **Schema migrations in v1: none.** The store file is created on first
  run; changing a `table` declaration after data exists is a build warning
  telling the user to migrate by hand. Real migrations are v1.1.

## 15. i18n — `strings` blocks and `t()` (discord module — NOT core)

`strings` + `t()` are **discord-module surface** (G1): they localize bot
replies per interaction locale. A general `i18n` module for non-bot
programs is future work (it arrives as a module, not as core syntax).
`t` is a reserved builtin name (a user-defined `fn t` is a resolver
error).

```
strings en:
  "buy.ok": "Bought {item} for {price} gold. Balance: {balance}."
strings id:
  "buy.ok": "Membeli {item} seharga {price} gold. Saldo: {balance}."
```

- `strings <locale>:` blocks hold `"key": "template"` pairs. Locales are
  BCP 47 tags (`en`, `id`, `en-US`); the compiler validates that **every
  locale defines the same key set** (a missing key is a build error naming
  the locale and the key — no silent fallback to English at runtime; the
  fallback is decided at build time).
- `t("key", name: expr, ...)` resolves the template for the **interaction's
  locale** (in a bot) or the program's default locale (in a general
  program), interpolating the named arguments. Placeholders inside
  templates are checked like any other interpolation: each `{name}` in the
  template must be either a `t()` argument name or a handler binding in
  scope at the `t()` call site — an unknown `{name}` is a build error.
- Registration-time localization of command *names* is v1.1 — command
  names stay single-locale in v1; only *replies* are localized.
- The (locale, key) cache rule is enforced by construction in generated
  code (per-interaction lookup, never a key-only cache).

## 16. Diagnostics — rustc form, Elm tone (G10)

One error type, one format, everywhere — modelled on rustc's data model
with Elm's tone:

```
error[E2001]: mismatched types
  --> file.newr:12:9
   |
12 |     let x: int = "hello"
   |         ^^^^^^   ^^^^^^^ expected `int`, found `str`
   |
   = help: change the annotation to `str`, or change the value to an int
   = note: `x` was declared as `int` here (file.newr:12:9)
```

Required fields for every diagnostic: `code` (stable — `E1xxx` syntax,
`E2xxx` types, `E3xxx` resolver, `W1xxx` warnings; every code documented
in an error-index file; `newr --explain E2001` prints the long form);
`severity` (`error` | `warning` | `note` | `help`); `span` (**byte
offsets** — `Span(file, start_byte, end_byte)`; line/col are derived at
render time via a `line_col(source, byte_offset)` helper — §4.3 of the
research); `expected` / `found` in user terms, never internal
type-variable names; `help` (optional, one concrete edit); `suggestion`
(optional: `{span, replacement, applicability: "certain" | "maybe"}` —
only `"certain"` suggestions may ever be auto-applied by a future
`--fix` / LSP code action); `--error-format=json` (one JSON object per
diagnostic, same fields, for editor/LSP consumption).
**Recovery rule: at most one error per function body per compile**
(report the first, skip the rest of that body); warnings are
independent. Recovery is a CHECKER/RESOLVER rule only — the PARSER has
no recovery: it stops at the first syntax error. (An earlier draft of
this section said "panic-mode + statement-boundary synchronisation";
that was wrong — the M1 parser aborts on the first syntax error, and
the code agrees.)

The full code scheme is 38 codes: 29 numbered (`E1xxx` syntax,
`E2xxx` types, `E3xxx` resolver, `W1xxx` warnings) + 9 lexer codes in
the descriptive `E_*` family. The complete table lives in
`docs/error-index.md`; `newr --explain <CODE>` prints the long form of
any of them. The numbered codes:

- Syntax: `E1001` syntax error (also "too deeply nested" past 200
  levels).
- Types: `E2001` mismatched types; `E2002` annotation needed;
  `E2003` heterogeneous list/dict literal; `E2004` non-`bool`
  condition; `E2005` use before definition; `E2006` non-exhaustive
  match; `E2007` `break`/`continue`/`return` in the wrong place;
  `E2008` Discord rule violation; `E2009` i18n violation;
  `E2010` not all paths answer.
- Resolver: `E3001` undefined name; `E3002` wrong number of
  arguments; `E3003` duplicate definition; `E3004` import cycle;
  `E3005` name not exported; `E3006` bot/command/event/task in an
  imported file; `E3007` reserved name shadowed; `E3008` unknown
  module / missing file.
- Warnings: `W1001` unused variable; `W1002` unused import; `W1003`
  unreachable code; `W1004` shadowing; `W1005` always-true/false
  literal condition; `W1006` unbounded loop in a handler body;
  `W1007` extra `t()` argument; `W1008` integer division in a float
  context; `W1009` reply length cannot be verified; `W1010`
  privileged intent declared.
- Lexer family: `E_BAD_ESCAPE`, `E_BAD_INDENT`, `E_BAD_LINE_ENDING`,
  `E_BAD_NUMBER`, `E_INT_OVERFLOW`, `E_STRAY_BRACE`, `E_TAB_INDENT`,
  `E_UNEXPECTED_CHAR`, `E_UNTERMINATED_STRING`.

Similar-name suggestions use the rustc **named** form ("there is a
variable with a similar name: `count`"), never the folk "did you mean"
wording — the rustc style guide discourages it, and the v0 wording is
deliberately changed (verdicts identical). The similarity gate is
documented and tested, not claimed as "standard": difflib ratio ≥ 0.7.
The v0 gate was 0.6, but 0.6 admits false positives once builtins join
the candidate pool (`coun` matches `round` at 0.667 as well as `count`
at 0.889, so no single suggestion fires); 0.7 keeps genuine typos and
drops the false positives (decided 6 Nov 2026 during M1). Warnings (privileged intent declared, store schema
changed, unused import/variable, unreachable code, dead branch) print
but do not fail the build. Where the fix is unambiguous and safe, the
diagnostic carries a `fix` (`{span, replacement}`) for `newr fix` (§5.2).
Error messages are English in v1.

## 17. Compiler architecture and distribution

### 17.1 Pipeline

```
source text (.newr)
  → lexer            tokens with byte-offset spans; UTF-8; # comments; 2-space indent tokens (INDENT/DEDENT); CRLF normalized
  → parser           recursive descent + Pratt expressions → AST (typed nodes, every node carries a byte-offset Span)
  → resolver         scopes, imports (cycle check), duplicate top-level names, reserved bindings, `pub` visibility
  → checker (core)   types, inference (R1–R11), general limits — NEVER imports discord_*
  → checker (discord) discord declarations, limits, placeholders, intents, "every path answers",
                     store/economy rules, strings/`t()` validation, task rules, handler-hang warning,
                     idempotency keys on store mutations in handlers — only runs if discord is used
  → emitter (py | js | java | csharp | rust | kotlin | dart | …)   AST → target source (M2+)
  → verifier         per target, native toolchain (compile() / tsc --noEmit / gradle build / dotnet build / cargo check / dart analyze / …)
```

### 17.2 AST (node list — the v1 set)

`Program` · `BotDecl` · `Import` · `StringsDecl` · `StoreDecl` ·
`FnDecl` · `StructDecl` · `EnumDecl` (with `EnumVariant`) ·
`CommandDecl` · `OptionDecl` · `EventDecl` · `TaskDecl` ·
`ButtonDecl` / `SelectDecl` / `ModalDecl` · statements: `Let`, `Const`,
`Assign`, `If`, `For`, `While`, `Try`, `Throw`, `Return`, `Break`,
`Continue`, `Pass`, `Match` (with pattern nodes: literal, `_`, binding,
enum-variant, struct, list, or-pattern), `Require`, `Ensure`, `Reply`,
`Defer`, `Edit`, `Followup`, `Send`, `Log`, `ExprStmt` · expressions:
`Lit`, `Name`, `BinOp`, `UnaryOp`, `Ternary`, `Call`, `Member`, `Index`,
`Interp`, `TCall`, `ListLit`, `DictLit`, `EmbedExpr`, `Lambda`,
`TryExpr`, `Propagate` (statement-level `?`). Declarations carry a
`pub` flag. Every node carries `Span(file, start_byte, end_byte)` —
byte offsets, not line/col (§16).

### 17.3 Distribution and CI

- **Source layout:** the compiler is a real package in the repo
  (`compiler/` — `lexer.py`, `parser.py`, `ast_nodes.py`, `resolver.py`,
  `checker.py`, `discord_check.py`, `discord_limits.py`, `modules.py`,
  `errors.py`, `cli.py`). The `newr` executable is a **thin
  launcher**: if run from a checkout it adds the package dir to
  `sys.path`; otherwise it runs the bundled code.
- **Distribution artifact:** CI builds a **single-file zipapp** (stdlib
  `zipapp`, `#!/usr/bin/env python3` shebang) from the package and
  publishes it as `newr`. A repo test asserts the built zipapp behaves
  identically to the checkout (same `build --check` verdicts on the golden
  examples).
- **PyPI:** `newr-lang` (Python 3.10+). The compiler is stdlib-only, so the
  package has no dependencies.
- **npm: DROPPED from v1 (G13).** The `npm i -g newr-lang` route for a
  Python CLI is HIGH RISK / UNVERIFIED — no precedent was found for an
  npm `bin` pointing at a Python program. v1 distribution is PyPI / pipx
  / zipapp / GitHub Releases + a PowerShell installer. Revisit only if a
  real precedent is found.
- **CI checklist (§3.4 of the research):** `encoding="utf-8"` on every
  `read_text()` / `open()` call (the #1 Windows bug class), CRLF handling in sources,
  case-only module-collision test, no Unix-exec-bit assumptions, and the
  Linux-built `.pyz` runs on the Windows and macOS runners.
- **CI matrix (defined now, run on every push):**
  `ubuntu-latest`, `macos-latest`, `windows-latest` × Python 3.10, 3.11,
  3.12, 3.13. Jobs: front-end suite, v0 conformance, zipapp behavior-diff
  test, golden-file rebuild check. A target-emitter job exists per shipped
  target (M2+); a failure in one target's job never blocks another
  target's release.
- **The CI matrix passing on all three OSes is the completion condition
  for the cross-platform claim (§3.7).** Until it passes, Windows/macOS
  support is UNVERIFIED.

## 18. Compilation targets

| # | Language | Library (target) | Version (live-checked 6 Nov 2026) | Priority | Emitter generates |
|---|---|---|---|---|---|
| 1 | Python | discord.py | **2.7.1** (3 Mar 2026) | **P0** | Single `.py` file; `python3 bot.py`; token via env; verify `compile()` |
| 2 | TypeScript/JS | discord.js | **^14.28.1** (npm latest), Node 24 LTS, TS ~5.9.3 | **P0** | TS project (`package.json`, `src/`); verify `tsc --noEmit` + `toJSON()` |
| 3 | Java | JDA | **6.7.0** (21 Sep 2026) | **P1** | `Main.java` + `build.gradle.kts`; verify `gradle build` |
| 4 | C# | Discord.Net | **3.20.2** (8 Oct 2026) | **P1** | `Program.cs` + `.csproj`; verify `dotnet build` |
| 5 | Rust | serenity + poise | **serenity 0.12.5** + **poise 0.7.1** (12 Oct 2026) | **P1** | `Cargo.toml` + `src/main.rs`; verify `cargo check`; MSRV 1.74 |
| 6 | Kotlin | Kord | **0.18.1** | **P2** | Gradle Kotlin DSL project; verify `gradle build` |
| 7 | Dart | nyxx (+ nyxx_commands) | **nyxx 6.9.1** + `nyxx_commands` 6.1.0 | **P2** | `pubspec.yaml` + `main.dart`; verify `dart analyze` |

Deferred (real library, blocked/niche, with revisit triggers — see the
expansion doc §2): Go (DiscordGo fork 0x00-sys, 0 tags — revisit when it
tags a release), Swift (SwiftDisc 2.5.0, 46★, one maintainer — re-check
~May 2027), C++ (DPP 10.1.6 — codegen complexity, only on user demand),
Elixir/Ruby/PHP (libraries alive, niche demand). **Not viable:** Crystal,
Lua, Javacord (dead 2023). **NEVER:** Disnake (CONTRIBUTING bans ALL LLM
contributions repo-wide, verified verbatim).

Targets now produce **general programs**, not only bots: a program with no
discord module compiles to a plain program in each target (a CLI in Python,
a Node script in TS, a `main()` in Java/C#/Rust/Kotlin/Dart).

- **Per-target owner rule (G16):** every shipped target has a **named
  owner + a CI job**, or it rots to Tier 3 (the Haxe lesson: a target with
  no owner is a target that silently drifts). A target with no owner does
  not ship.
- **Divergence table:** where targets cannot be made identical, this spec
  says so in one place with the weaker behaviour named — a
  "(non-standard)" is a bug with a version attached, not a quirk.

### 18.1 Conformance

- **One conformance corpus.** Every example program builds on **every**
  target, and *behavior* is compared across targets by the same mock
  round-trip corpus: same inputs → same outputs, same balances, same
  button flows. A target that cannot pass the corpus does not ship. The
  corpus lives in `conformance/` and is the single definition of "correct
  behavior".
- **Per-target golden files.** Checked-in expected outputs per example per
  target. A golden file is never hand-edited: it is regenerated by the
  compiler and committed as the test oracle.
- **One shared escape-test corpus.** One adversarial-string corpus
  (quotes, braces, backslashes, newlines, emoji, RTL text) runs through
  **every** emitter.

## 19. Tooling

| Command | In v1 | Notes |
|---|---|---|
| `newr build` | `build [--target py\|js] [--check] [--fast] <file.newr> [-o <out>]` | `--target` selects the emitter (default `py`); without `--check` it emits (M2+). In M1 (no emitter yet) `build` without `--check` is an error telling the user to use `--check` — emitters land in M2. `--fast` emits with only parse + minimal checking (syntax + name resolution), with a warning that the output is not fully verified (§4.1). |
| `newr build --check` | Parse + resolve + check (+ emit + verify from M2) | Writes nothing. Exit 0 on success, 4 on build error. |
| `newr check` | Parse + resolve + check, print errors + warnings | The "thinking" command (§5.1). Exit 0 if no errors (warnings allowed), 4 on error. |
| `newr fix` | **NOT in M1** — M4 | Applies safe, unambiguous diagnostic fixes (§5.2). |
| `newr --explain <code>` | Yes (M1) | Prints the long form of a diagnostic code (§16). |
| `newr --error-format=json` | Yes (M1) | One JSON object per diagnostic, same fields (§16). |
| `newr fmt` | **NOT in v1** — M4/M6 | Canonical formatting, idempotent, `fmt --check` for CI. |
| `newr init` | **NOT in v1** — M4/M6 | Scaffolds a newr project (`main.newr` + `newr.json` + `locales/` + `.env.example` + `.gitignore`). |
| `newr.json` | Schema defined in §11; **compiler support M4** | Project config `{name, version, target, default_locale, db_path}`. Secrets NEVER go in it — the token stays in `.env`. |
| `newr test` | **NOT in v1** — M6 / v1.1 | Built-in testing in the same language; needs the emitter interface stable first (§24.2 #13). |
| `newr --help` / `newr help` | Yes | Usage for every command. |

Error/exit-code contract (from v0, kept): 0 success, 1 usage, 2 environment,
3 network, 4 build; `--json` envelope `{ok, command, newr, data, warnings,
error}` (v0 shape, kept).

## 20. Deliberately NOT in v1

Voice (DAVE minimums make it a project of its own). Prefix commands
(slash-only). HTTP `fetch` (v1.1, §12.3). Subcommands (§13.2, v1.1).
Command-name localization (§15, v1.1). Components V2. Sharding
configuration (deploy concern). Custom emoji / stickers. Threads /
forums / scheduled events. Classes (structs cover the data case — §8.5).
User-defined generics (v2 — §7.7; built-in generics are in v1). Full
pattern exhaustiveness / usefulness (v2 — core `match` is in v1, §9.6).
`json` module (§12.2, v1.1). `newr fix`/`fmt`/`init` (§19, M4/M6).
Subprocess/shell (§3.4). Regex (§12.3). Crypto builtins (§12.3). Threads
/ shared mutability (v1.1+ — §8.4). Reflection splicing / codegen
(later — a narrow `reflect` API first). Full TCO (later — explicit
`@tailrec` / self-recursion first). Parameter decorators (until TC39
settles — §24.3). LSP server (M2+ — §5.3). Borrow/ownership checking
(never — §7.4 deferred table; ownership is a property of one target, not
of the source language). AI features in the compiler (never — §5.3).
`eval` (never — it defeats the compiler). Each has a one-line reason
above; none is forgotten, all are deferred with a version attached (or
rejected, with the reason written here).

### 20.1 FFI / bindings — the escape-hatch question, answered as design work (G20)

"No inline target code in v1" stays as a compiler rule — but the FFI /
binding story is specified as M1 design work, because "who debugs the
generated code?" and "how do I call the libraries I already use?" must
have a v1 answer:

- **v1.1 direction:** `extern` declarations with per-target bindings,
  verified by the compiler against a binding manifest (the DefinitelyTyped
  lesson: bridge first, native libraries later — call existing
  Python/TS/Java libraries from newr from day one).
- **v1 interim answer:** no FFI — extend readable generated code by hand
  (§1.1.6: the generated code stays readable, so hand-extension is
  practical; source maps / round-tripping are the longer-term answer).
- **Never:** inline host-language blocks inside `.newr` source (§1.1.5).

## 21. Examples

### 21.1 Hello — an ordinary program (the canonical hello)

```newr
# hello.newr — a program written in the newr language.
# Build it with: newr build examples/hello.newr
# Then run it with: python examples/hello.py

print "Hello, world!"
```

### 21.2 A program that uses the language (file I/O + functions + while)

```newr
# count.newr — read a file, count the lines longer than 10 characters.
fn is_long(line: str) -> bool:
  return len(line) > 10

let content: str = file.read("notes.txt")
let lines: list[str] = content.split("\n")
let count: int = 0
let i: int = 0
while i < len(lines):
  if is_long(lines[i]):
    count += 1
  i += 1
print "Long lines: {count} of {len(lines)}"
```

### 21.3 A bot — the discord module (v0 hello, unchanged)

```newr
# hello_bot.newr — a bot written in the newr language.
bot "HelloBot":
  description: "A friendly bot that answers in plain words"

command ping:
  description: "Answer with pong"
  reply: "Pong! Hello {user}"

command greet:
  description: "Greet someone in this server"
  reply: "Hello {user}, welcome to {guild}!"
  ephemeral: true

event ready:
  log: "{bot} is online"

event member_join:
  reply: "Welcome to {guild}, {user}!"
```

### 21.4 The shop bot (v1 features: options, functions, store, t(), button)

```newr
# shop.newr — ping + economy buy + button confirm, in newr v1.
import "shop_items.newr"

bot "ShopBot":
  description: "A tiny shop"
  intents: [guilds, members]

strings en:
  "buy.ok": "Bought {item} for {price} gold. Balance: {balance}."
  "buy.poor": "Not enough gold — {item} costs {price}, you have {balance}."
strings id:
  "buy.ok": "Membeli {item} seharga {price} gold. Saldo: {balance}."
  "buy.poor": "Gold tidak cukup — {item} harganya {price}, kamu punya {balance}."

store economy:
  table balances (user: str, balance: int)
  ledger true

# price_of lives in shop_items.newr (pub fn — §11): shop_items.price_of(item).
command ping:
  description: "Answer with pong"
  reply: "Pong! Hello {user}"

command buy:
  description: "Buy an item from the shop"
  option item: str (required, choices: ["brick", "potion"])
  option amount: int (default: 1, min: 1, max: 99)
  do:
    let price: int = shop_items.price_of(item) * amount
    let bal: int = economy.balance(user)
    if bal < price:
      reply t("buy.poor", price: price, balance: bal) ephemeral
    else:
      economy.credit(user, -price, key: "buy-{user.id}-{item}-{amount}")
      reply t("buy.ok", item: item, price: price, balance: bal - price)
      button "Buy again" (style: primary, id: "buy-again", timeout: 60s):
        only: author
        do:
          reply "Use /buy again, {user}!"
        on_timeout:
          reply "Offer expired."

command balance:
  description: "Show your balance"
  do:
    reply "Balance: {economy.balance(user)} gold."

event ready:
  log: "{bot} is online"

event member_join:
  reply: "Welcome to {guild}, {user}!"

task every 1h:
  do:
    log "Hourly shop check ran"
```

### 21.5 A small cross-platform CLI (the v1 domain: argv, enum, match, pipeline)

```newr
# cli.newr — a small cross-platform CLI: argv, enum, match, pipeline.
enum Mode:
  Count
  Shout(text: str)

fn double_all(xs: list[int]) -> list[int]:
  let out: list[int] = []
  for x in xs:
    out.append(x * 2)
  return out

fn mode_of(args: list[str]) -> Mode:
  if len(args) > 1 and args[1] == "shout":
    let word: str = len(args) > 2 ? args[2] : "hey"
    return Mode.Shout(text: word)
  return Mode.Count

let mode: Mode = mode_of(env.args)
match mode:
  Mode.Count:
    let xs: list[int] = [1, 2, 3] |> double_all
    print "Doubled {len(xs)} numbers"
  Mode.Shout(text: word):
    print word.upper()
```

`env.args` includes the program name at index 0 (§12.2). The `match`
is exhaustiveness-checked (§9.6); the pipeline passes the list as the
first argument (§9.2); the ternary picks the default word (§9.2).

## 23. What newr takes from other languages — the design philosophy

newr is not a copy of one language. It is a systematic collection of the
best ideas from all major languages — each taken for a stated reason, each
weighed against what it costs. The table is the philosophy; the rest of
this spec is the execution. **This chapter records the features taken.
It is NOT a claim that newr is "best of all"** — see §1 (the honest v1
story) and §23.2 (the Haxe precedent, addressed head-on).

| Feature in newr | Source language(s) | Why it was taken | Status |
|---|---|---|---|
| Helpful compiler errors: "this is what you meant, this is the fix, this is why" | Rust, Elm | Rust proved a compiler can teach; Elm proved an error can explain the *why*, not just the *where*. newr's diagnostics (§16) are modelled on both. | v1 (§5, §16); `newr fix` in M4 |
| `T?` / null safety without ceremony | Rust, Swift, Kotlin | Rust's `Option` killed the null-pointer crash; Swift/Kotlin proved it can be ergonomic instead of bureaucratic. newr takes `T?` + `or` + `.unwrap()` (§7.2) — the safety, not the ceremony. | v1 |
| Fast compile, one obvious way, readable code | Go, Python | Go proved a compiler can be fast and a language can be small; Python proved indentation + readability scale from beginners to experts. newr's shape is Python's, its compile-speed ambition is Go's. | v1 (§4, §6) |
| Type inference: annotate where it adds clarity, not everywhere | TypeScript, Haskell | TypeScript proved inference + annotations can coexist; Haskell proved inference can be principled. newr infers from literals and signatures (§7.4) and demands an annotation only where inference is impossible. | v1 |
| String interpolation everywhere | Swift, Kotlin, C#, Ruby | Every modern language interpolates; newr makes it the *only* string mechanism (§9.3) — no `format()` vs `%` vs `+` confusion. | v1 |
| Unicode-correct strings by default | Swift, Rust | Strings are Unicode in newr, always (§6, §7.1). No byte-string vs text-string split, no encoding footgun. | v1 |
| One toolchain, zero config to start | Go, Rust (cargo), Dart | `gofmt` + `go build`, `cargo`, `dart` proved one binary can carry fmt/lint/test/docs. newr's CLI is one binary with build/check/fix/fmt/test/docs (§19). | v1: build/check; M4–M6: fmt/fix/test/docs/init |
| No dependency hell: reproducible builds by default | Rust (Cargo.lock), Go (go.sum) | Lockfiles made builds reproducible. newr's answer is stronger in v1: **no external dependencies at all** (stdlib-only compiler, no module registry yet) — dependency hell cannot happen before the registry exists. The lockfile arrives *with* the registry, not after it. | v1 (no deps); lockfile with the module registry, post-v1 (§23.1) |
| Batteries-included stdlib, versioned with the language | Python, Go | Python's stdlib is why people reach for it; Go's is why they stay. newr's stdlib ships with the compiler, one version (§12). | v1 (core); HTTP/JSON/dates in v1.1 (§12.3) |
| Cross-platform from day one, no `#ifdef` hell | Dart/Flutter, Java | Dart proved one codebase can ship everywhere; Java proved "write once, run anywhere" as a philosophy. newr's version: the language is portable by construction (§3) and the *compiler* proves it on three OSes in CI. | v1 (§3) |
| Readable generated code | — (newr's own rule) | No language takes credit for this; it is newr's own non-negotiable (§1.1.6). Generated code must read like code a human would write. | v1 principle; proven at M2+ |
| One language → many targets, same behavior | Java ("write once, run anywhere"), Kotlin Multiplatform | Java's promise was one bytecode everywhere; newr's is one source, N native programs, behavior-identical by conformance corpus (§18.1). | v1 architecture; emitters M2–M5 |
| First-class i18n, not a bolted-on library | — (newr's own) | No mainstream language makes i18n part of the language. newr does (§15): `strings` blocks + `t()` are syntax, checked at build time — in the discord module, where locale-aware replies live. | v1 (discord module, §15) |
| No silent behavior / no hidden control flow | Zig, Rust | Zig's "no hidden control flow" is the clearest statement of the rule; Rust's explicitness is the practice. newr: nothing happens behind the user's back (§1.1.4). | v1 |
| Docs generated from code comments | C# (`///`), Rust (rustdoc), Java (javadoc) | Every mature toolchain generates docs from comments. newr will too — once the doc-comment syntax is frozen. | M6 (§24, idea 14) |
| Small for beginners, scalable for experts | Python, Go | The hello program is one line; the same language builds a shop bot with economy, i18n, and components (§21). | v1 (§1.1.1) |
| Errors as values, propagated ergonomically | Rust (`?`), Go (explicit errors) — minus Go's repetition | Rust's `?` proved errors-as-values can be ergonomic; Go proved explicit errors are honest but verbose. newr takes try/catch (§10) as the primary model, plus `Result[T, E]` as a value form with a `try` expression and a statement-level `?` (§10) — no `if err != nil` repetition, no hidden returns. | v1 |

### 23.1 What newr deliberately did NOT take

Taking the best of all languages also means saying no to the rest, in
writing:

- **From Rust:** not the borrow checker; not `Result` as the *primary*
  error mechanism (§10 — `Result` exists as a value form); not the
  ceremony around nullability (no `match` required to unwrap —
  `.unwrap()` / `or` suffice in v1).
- **From Go:** not the `if err != nil` repetition, not the lack of
  generics history (newr's `list[T]`/`dict[str, T]` are generic from day
  one). (Ternary IS in v1 — §9.2; the old "we agree with Go" line was
  reversed with the general-purpose correction.)
- **From Python:** not dynamic typing (newr is statically typed — §7),
  not the GIL-era concurrency story (see §24, idea 17).
- **From C++/Java:** not the boilerplate (no `public static void main`,
  no semicolons, no mandatory class wrapper — top-level statements *are*
  the program, §8).
- **From JavaScript:** not the `==` vs `===` split, not the callback
  legacy — newr has one equality (`==`, type-checked, §7.2) and no
  callbacks in v1.
- **From Elixir/Erlang:** not "let it crash" as a philosophy — newr fails
  at *build* time instead of at runtime (§1.1.2). Pattern matching: core
  `match` is in v1 (§9.6); full exhaustiveness and custom matchers are
  v2, not taken half-way.

### 23.2 The Haxe precedent, addressed head-on (G16)

"One language, compiles to all targets" has a 20-year precedent: Haxe
(first appeared 14 Nov 2005) compiles one codebase to JavaScript, C++,
Java, JVM, PHP, C#, Python, Lua and Node.js — and after 20 years it is
still niche (described in a Nov 2026 survey as #68, "Cross-platform
games"; 32nd as a first language to learn). Why it stayed niche: no
tech-giant backing, a small team without resources for great tooling,
ecosystem fragmentation across targets, hard debugging of generated code,
and needing "externs" to use each target's native libraries. It
*survived* — survival ≠ fame. **Verdict:** "compiles to all targets"
has historically **lost to "one language, one great ecosystem"** —
developers adopt an *ecosystem* (libraries, jobs, answers, colleagues),
not a language. A transpiler gives you *access* to seven ecosystems and
*ownership* of none. That is why v1 names **one domain** (small
cross-platform CLIs/tools, §1) and why the FFI/binding story (§20.1)
exists: newr must answer "who debugs the generated code?" in v1, or the
migration story collapses. Haxe's exact TIOBE rank in 2025/2026 is
UNVERIFIED (§25, U18).

### 23.3 Fame timeline — planning horizon (G16)

| Phase | Time | What "famous" means | Evidence it happened |
|---|---|---|---|
| v1 | 0–1 yr | People can try it in 10 min and *like* it | Playground shares, GitHub stars, first 100 users who stay |
| v2 | 1–3 yr | People build real things and *show* them | 1 killer app/demo, 1 framework choosing newr, first conference talks |
| v3 | 3–7 yr | People get *jobs* and *teach* it | Job posts, courses, books, a registry with depth in one domain |
| v4 | 7–15 yr | It is *default* somewhere | TIOBE top 20, SO survey top 10 usage, institutional adoption |

Python took 30 years to #1; Rust took 10 years to be loved and is still
climbing in usage; TypeScript took 4 years to its turning point (Angular
2) and 13 years to GitHub #1; Go took 5–7 years to own cloud infra. **Any
plan promising newr fame in under 3 years is ignoring all four
precedents.** What v1 CAN copy in weeks–months (no community needed): a
free Book, a 10-minute Tour, runnable snippets tested by CI so they never
rot, one binary, `fmt`, `test` built in, doc generation, one install
command for Linux/macOS/Windows, a browser playground with all 7 targets
side-by-side, error messages that teach, 10 real complete programs, a
public RFC/proposals repo from day one, a weekly note. What needs
community and time (2–10 years, cannot be faked): registry depth, a
killer app, jobs, books/courses, conferences, institutional trust. The
wave to surf in 2026 is **AI-assisted coding**: a language that is easy
for models to write correctly (explicit types, small spec, great errors,
verifiable output) has a structural advantage GitHub itself now names.

## 24. Wishlist — features people asked for, other languages said no (or "later")

### 24.1 How this chapter works

- **Nothing disappears silently.** Every idea that reaches this chapter
  gets a verdict: **v1** (in this spec), **deferred to <version>** (with
  the reason it waits), or **rejected** (with the reason it will not
  happen). A rejected idea keeps its reason written here, so nobody
  re-litigates it without new evidence.
- **Research arrived.** The deep research pass (started 22:02 WIB,
  delivered 22:12 WIB 6 Nov 2026, `newr-deep-research.md` §7) surveyed
  features requested by many users but rejected or deferred by other
  languages' developers — across Go, Python, Rust, JavaScript/TypeScript,
  Java, C#, Swift, Kotlin, Ruby, PHP, from issue trackers, RFCs, forums,
  and social media. Its findings are merged into §24.3 with a verdict, a
  source, and a date. Later research may add items, change a verdict
  (with a written reason), or add a rejection — it never silently
  deletes an item.
- The verdicts below are chie's, from the spec work so far. The research
  findings may add items, change a verdict (with a written reason), or
  add a rejection — they never silently delete an item.

### 24.2 Chie's 19 ideas — evaluated

| # | Idea | Verdict | Reason |
|---|---|---|---|
| 1 | Errors that actually help (Rust/Elm style) + `newr fix` for safe fixes | **v1** (diagnostics); **M4** (`newr fix`) | Diagnostics with span + hint + machine `fix` field are in v1 (§5.1, §16). The `fix` command applies them in M4 (§5.2) — the command waits until the diagnostics are proven, the *data* does not. |
| 2 | One toolchain, all built-in: fmt, lint, test, package manager, docs — one binary, zero config | **v1** (build/check); **M4–M6** (fmt/fix/test/init/docs) | `build` + `check` (which is also the linter — §5.1 warnings) ship in v1 (§19). `fmt`, `fix`, `test`, `init`, docs generation land M4–M6, in that order — each needs the layer below it to be stable first. The package-manager half (v0's `install`/`update`/`agents`) lands with the newr repo as `newr_cli/`, M4+. |
| 3 | No dependency hell: lockfile + reproducible build as default | **v1** (by absence); **lockfile with the module registry** (post-v1) | v1 has no external dependencies at all — no registry, no versions to resolve, nothing to go wrong (§11, §23). A lockfile is meaningless before a registry exists; it is specified *with* the registry, not bolted on after. |
| 4 | Type inference | **v1** | §7.4. Annotate where it adds clarity; the checker infers the rest; ambiguity is an error, never a guess. |
| 5 | Null safety without verbosity (`T?`-style) | **v1** | `T?` + `or` + `.unwrap()` (§7.2). No `null`, no null-pointer crash, no Rust-length ceremony. The `option[T]` spelling is removed — exactly one absent value, spelled `T?` (G19). |
| 6 | Errors as values with ergonomic propagation (no Go-style repetition) | **v1** | try/catch (§10) as the primary model, plus `Result[T, E]` with a `try` expression and a statement-level `?` (§10) — no `if err != nil` repetition, and no hidden returns (the Go objection is honoured: `?` is statement-level only, never nested). |
| 7 | Unicode-correct strings + interpolation everywhere | **v1** | §6, §9.3. Strings are Unicode; interpolation is the only string mechanism. |
| 8 | First-class i18n (`strings` + `t()`) | **v1** (discord module) | §15. Syntax, not a library; key sets validated at build time — in the discord module, where locale-aware replies live (G1). A general `i18n` module for non-bot programs is future work. |
| 9 | Cross-platform from day one, no `#ifdef` hell | **v1** | §3. Portable by construction; CI matrix is the proof. |
| 10 | Human-readable generated code | **v1** | §1.1.6. Proven by golden files at M2+. |
| 11 | Fast + incremental builds | **v1** (design); **M4** (cache) | §4.1. The cache design is frozen now; the simple form (rebuild everything — fast enough for small projects) ships in v1, the cache lands in M4. Benchmarks in M2. |
| 12 | One language → many targets, same behavior (conformance corpus) | **v1** (architecture); **M2–M5** (emitters) | §18.1. The corpus is the definition of correct behavior; emitters land one at a time against it. |
| 13 | Built-in testing (`newr test`, same language) | **Deferred to M6 / v1.1** | A test framework needs the emitter interface stable first (otherwise tests would be written against a moving target). It also needs a test syntax decision (test blocks? `test` functions?). Both arrive after M3. Not forgotten — it is the first M6 item. |
| 14 | Docs generated from code comments | **Deferred to M6** | Needs a frozen doc-comment syntax (the `///` convention, from C#/Rust) and the docs site. v1 freezes the language; M6 builds the docs pipeline on top of it. |
| 15 | No silent behavior | **v1** | §1.1.4. A non-negotiable, enforced by the checker (unknown lines are errors; no silent coercion). |
| 16 | Truly complete stdlib (HTTP, JSON, dates, files), versioned with the language | **v1** (files, time, math, random, env); **v1.1** (HTTP, JSON, dates) | §12. Files/time/math/random/env ship in v1. HTTP needs a sandboxed `fetch` + allowlist (security review, §12.3); JSON needs a typed JSON value (no `any` in v1, §12.2); dates beyond `time.format` need a date type. All three are v1.1 with their reasons written — not dropped. The stdlib *is* versioned with the language from day one (it ships with the compiler). |
| 17 | Safe-by-default concurrency (structured, no data races) | **Deferred to v1.1+** | v1 is single-threaded by default: `task` (discord module) is scheduled work, not concurrency; there are no threads and no shared mutable state (§8.4). Structured concurrency (async/await? actors? channels?) is a whole design decision: it touches the type system, the emitters (each target's concurrency model is different), and the "every path answers" check. It needs its own design document before a version is attached. It is the biggest open design question in this spec, and it is written here so it is not forgotten. |
| 18 | Stability contract: stable language, adapting backends | **v1** | §2. The core has zero Discord dependencies; a target change touches only its emitter. |
| 19 | Small for beginners, scalable for experts | **v1** | §1.1.1, §21. Hello is one line; the same language scales to a shop bot with economy, i18n, and components. |

### 24.3 Features requested by many, rejected/deferred by other languages — RESEARCH FINDINGS (22:12 WIB 6 Nov 2026)

Source: `newr-deep-research.md` §7 (full agent report, sources accessed
6 Nov 2026). Reaction/comment counts are given only where actually seen;
otherwise UNVERIFIED. Quotes are only from pages actually opened. Each
item has a verdict for newr.

| Feature (source language) | What happened there | Verdict for newr |
|---|---|---|
| None-aware `?.` / `?[]` / `??` (Python, PEP 505 — Deferred, not formally rejected) | PEP 505 (2015) proposed all three; deferred during governance upheaval; co-author later doubted the syntax; 2024–25 revival stalled on whether `a?.b` should swallow a missing attribute, not just `None`. A stdlib scan found up to 678 snippets it could replace. | **v1 — flagship.** Settle the one rule Python could not: `?.` short-circuits on `none` ONLY; a missing attribute is still a compile error (§9.2). |
| Pipeline `\|>` (JavaScript, TC39 Stage 2, stuck ~10 years) | Proposal since 2015, Stage 2, 7,917 stars; stuck on syntax bikeshed (F# vs Hack vs Smart-Mix), not semantics. State of JS 2020: pipe operator was the 4th most-missed feature. | **v1 — flagship.** Pick ONE syntax and freeze it: Elixir-style, piped value is the first argument (§9.2). Indecision, not difficulty, killed it at TC39. |
| `try` blocks (Rust — accepted RFC, still unstable 10+ years) | RFC 243 / 2388; tracking issues #31436 and #154391 (2026). Not stabilised: whether the block Ok-wraps its value, inference gaps, async integration, `Try` trait design. | **v1.** Define what Rust could not agree on: yes, Ok-wrap; yes, explicit `Result[T, E]` annotation when ambiguous; no heterogeneous blocks in v1 (§10). |
| Typed enums (Go #19814, open since 2017, neither accepted nor declined) | "Being simple is, primarily, a function of orthogonality… enums do not fit as orthogonally into Go as they fit into C." Still open; lack of full enums is a top-three Go frustration (secondary). | **v1.** Every target has enums/sealed types; one `enum` with associated values + exhaustiveness (§8.6, §9.6). |
| Multiline lambdas (Python — rejected by design) | Guido (2006): embedding an indentation-based block in the middle of an expression is unacceptable in Python. | **v1 (free differentiator vs Python).** Lambdas take a block body from day one (§9.5). |
| Error-handling shorthand `?` (Go — `check`/`handle` → `try()` → `?`, ALL abandoned June 2025) | golang/go#32437, ~900 comments. Go team's own words: "`try` affected control flow by returning from the enclosing function… thus hiding this control flow from view. This made the proposal unpalatable to many…" For the foreseeable future Go stopped pursuing syntactic error-handling changes. | **v1, statement-only.** Grant `?` only as a visible early-return at statement level, never hidden inside a nested expression — that was the exact objection (§10). |
| `match` / pattern matching (Python PEP 3103 rejected 2007, later `match` in 3.10; TC39 Stage 1 since May 2018) | PEP 3103: "no popular support" at PyCon 2007; semantic trap (optimised dispatch needs side-effect-free cases). TC39: scope is huge (custom matchers, exhaustiveness, guards); no Stage 2 consensus in 8 years. | **v1 core, v2 custom matchers.** Literal/struct/array/or-patterns + guards + exhaustiveness-as-error (§9.6). |
| Records / Tuples / `value` types (TC39 withdrawn Apr 2025; Java Valhalla since 2014) | Records & Tuples withdrawn at Stage 2 (performance: changing `===` would slow every `===`). Valhalla: value objects with only `final` fields, no identity (JEP 401, preview; GA version UNVERIFIED — three secondary sources conflict). | **v1 semantics, perf later.** `struct`/`enum` are value types: copied on assignment, structural `==`, no identity, no field assignment (§7.8). Do NOT promise flattening/zero-allocation on all targets. |
| Contracts `requires` / `ensures` (C++ — removed from C++20, still unresolved in C++26) | P1823R0 (2019): "This is a proposal nobody likes… the feature is not ready… We have no implementation experience." C++26 ballot-failure claim is UNVERIFIED against an ISO source. | **v1, runtime-only.** `requires`/`ensures` as runtime checks that throw, always evaluated in v1, no violation-handler / continuation / "assume" semantics (§9.7). |
| Generics (Go — refused 2009–2021, shipped 1.18 Mar 2022) | Russ Cox, "The Generic Dilemma" (2009): every design picks a cost — no generics (slow programmers), templates (slow compilers), or boxing (slow execution). Four drafts abandoned; contracts draft "too complicated". | **Must grant; biggest spec decision.** Built-in generics in v1; semantic decided at M0 — erasure on Python/TS/Java/Kotlin, reification on C#/Rust/Dart, unobservable in v1 via three rules (§7.7). User-defined generics v2. |
| Explicit TCO / `@tailrec` (Python — rejected by design, no PEP) | Guido (2009): eliminating stack frames for some calls but not others "would certainly confuse many users" and makes debugging harder. | **Deferred to v1.1, explicit-only, self-recursion first.** Never silent/implicit TCO. |
| Decorators (TC39 — demoted Stage 3 → Stage 2.7, May 2026) | Spec finalised but zero native engine shipments in four years; parameter decorators were never in the proposal, breaking NestJS/Angular DI expectations. (Demotion reason is secondary; the Stage 2.7 listing itself was seen.) | **Deferred to v1.1, scoped.** Metadata + wrapping for methods/classes only; no parameter decorators until the spec settles. |
| Static reflection (C++ P2996 — stalled a decade, now in C++26) | Value-based vs type-based reflection, code-injection scope, compile-time cost, 10+ years of competing proposals. | **Deferred — narrow subset only.** Field names, field types, constructor arity at compile time, via a small `reflect` API. No splicing/codegen in v1. |
| Async closures / `async fn` in traits (Rust — long-deferred, now shipped) | RFC 3668; stabilised Rust 1.85 (Feb 2025). `async fn` in traits: RFC 3185 + 3425, stabilised 1.75 (Dec 2023). Remaining gap: Return Type Notation still unstable. | **Deferred to v1.1+.** Design the async model with traits/interfaces in mind, not bolted on. Do not promise `Send` bounds on the returned future until the spec has RTN-equivalent syntax. |

### 24.4 Already-known deferred/rejected items (from this spec's own work)

These are not from the research pass — they are decisions this spec
already took, collected here so the wishlist chapter is the single place
to look:

| Item | Verdict | Reason (where in this spec) |
|---|---|---|
| Full pattern exhaustiveness / usefulness | **Deferred to v2** | Core `match` is in v1 (§9.6); full usefulness checking is Rust's `rustc_pattern_analysis` algorithm — do not reimplement in v1. |
| Generics (user-defined) | **Deferred to v2** | `list[T]`/`dict[str, T]`/`set[T]`/`T?`/`Result[T, E]` are generic from day one (§7.1); *user-defined* generic functions/types wait — they need a generic checker, which is v2 work (§7.7). |
| HTTP `fetch` | **Deferred to v1.1** | §12.3 — sandboxed `fetch` + allowlist, after a security review. |
| `json` module | **Deferred to v1.1** | §12.2 — needs a typed JSON value. |
| Regex | **Deferred (no version)** | §12.3 — injection/DoS vector in a bot language; revisit with a timeout. |
| Subcommands | **Deferred to v1.1** | §13.2. |
| Command-name localization | **Deferred to v1.1** | §15. |
| Components V2 | **Deferred (no version)** | §20 — classic components in v1; V2 needs its own verification per target. |
| Cron expressions for `task` | **Deferred to v1.1** | §8.4 — durations only in v1; cron is a second language. |
| Subprocess / shell | **Deferred (no version)** | §3.4 — the least portable surface; revisit with a portable `process` module if demanded. |
| Voice | **Deferred (no version)** | §20 — DAVE minimums make it a project of its own. |
| Prefix commands | **Rejected** | §20 — slash-only. Discord's own direction is slash commands; prefix commands are legacy in discord.py and do not exist in discord.js. |
| `eval` | **Rejected, permanently** | §12.3 — it defeats the compiler. There is no version where `eval` arrives. |
| Inline host-language blocks (escape hatch) | **Rejected for v1** | §1.1.5 — an escape hatch lets every hard mapping be dodged; generated code would stop being regenerable. Revisited only if a real program cannot be expressed — with evidence, not on principle. |
| Classes / inheritance | **Rejected for v1** | §20 — structs (§8.5) cover the data case; inheritance is a v1.1+ discussion, not a v1 feature. |
| Crypto builtins | **Rejected** | §12.3 — a program should never hand-roll hashing/signing; the host language's vetted libraries own it. |
| Borrow / ownership checking | **Rejected (never)** | §7.4 deferred table — proves memory safety for one target (Rust); newr's other six targets are GC'd; cost is a MIR + dataflow engine. |
| AI features in the compiler | **Rejected (never)** | §5.3 — probabilistic, hallucinates, needs a model/network; the compiler must stay deterministic and offline. |
| Explicit TCO / `@tailrec` | **Deferred to v1.1** | §24.3 — explicit-only, self-recursion first; never silent/implicit TCO. |
| Decorators | **Deferred to v1.1** | §24.3 — scoped: metadata + wrapping for methods/classes only; no parameter decorators until TC39 settles. |
| Reflection splicing / codegen | **Deferred (later)** | §24.3 — narrow `reflect` API first (field names/types, constructor arity). |
| Threads / shared mutability | **Deferred to v1.1+** | §8.4 — `task` (discord module) is the v1 primitive; no threads in v1. |
| Async (closures / `async fn` in traits) | **Deferred to v1.1+** | §24.3 — design async + traits together from day one; no `Send` bounds until the spec has RTN-equivalent syntax. |
| FFI / `extern` bindings | **v1.1 direction** | §20.1 — `extern` declarations with per-target bindings verified by the compiler; v1 interim answer is hand-extended readable generated code. |
| LSP server | **M2+** | §5.3 — needs the checker as a reusable, incremental library. |

## 25. UNVERIFIED items (do not cite as fact)

1. **Windows/macOS execution** of the compiler and of generated programs —
   the CI matrix (§17.3) is the proof; this VM is Linux-only (§3.7).
2. **Rust try/catch lowering** (§10) — the `Result`-helper approach is
   recorded, not proven; M5 proves it.
3. **discord.js placeholder mappings** (`userMention()`, guild DM
   nullability) — verify at M3 build time, not by analogy.
4. **Placeholder mappings for JDA / Discord.Net / serenity / Kord / nyxx**
   — each emitter proves its own live.
5. **Storage-driver semantics per target** (transactions under concurrent
   handlers) — proven per emitter, not assumed from the Python pattern.
6. **Compile-time numbers** (§4.1) and **generated-code benchmark numbers**
   (§4.2) — no measurements exist yet; M2 records them honestly.
7. **Zipapp-from-`npx` behavior** and the exact `newr.json` schema — decided
   in M4/M6.
8. **poise ↔ serenity pin for M5** — read poise's `Cargo.toml` before M5
   codegen starts (poise 0.7.1 is the live-checked version, §18).
9. **npm distribution — RESOLVED, dropped (G13).** The `@drewdestar` npm
   scope question is moot: npm is not a v1 distribution route (no
   precedent for an npm `bin` pointing at a Python program).
10. **Contradictions found between the v1 design doc and the v0 code
    (code wins, flagged here):**
    - The design doc §3.5 says functions "may read `store`" and §3.10 says
      store mutations need idempotency keys — but v0 has no functions and
      no store, so there is no v0 behavior to contradict; the rule is new
      in this spec.
    - The design doc §3.13 example uses `economy.credit(user, -price, key:
      "buy-{user.id}-{item}-{amount}")` — interpolation of `{user.id}`
      inside a named argument. v0 interpolation only allowed the four
      bare placeholders. This spec generalizes interpolation to arbitrary
      expressions (§9.3), which subsumes the v0 form.
    - The design doc §2.3(5) "No escape hatch in v1" and §3.12 "while,
      recursion" bans are **superseded** by the user's 21:49 WIB order
      (general-purpose language) — recorded in §0, not silently dropped.
    - The design doc §3.11 says a `task` body may `send` to "a configured
      channel" — no channel configuration syntax was ever defined. This
      spec defines `send <expr> to <expr>` with an explicit channel
      expression instead (§13.8); there is no "configured channel".
    - The design doc §3.10 `store.get(table, key)` / `set` / `delete`
      signatures were never specified. This spec defines them in §14
      (string-literal table name, row types named after tables).
    - The design doc §3.11 embed syntax (`embed:` blocks +
      `embed.set_*`) was never specified. This spec defines the
      `embed:` block expression in §13.7.
    - The design doc's "poise 0.6.x" is stale — poise is 0.7.1 (§18,
      per the expansion doc's live check).

11. **Research UNVERIFIED register U1–U23** (`newr-deep-research.md`
    §12 — each item states what would settle it; re-verify every number
    against its primary source before quoting externally or writing it
    into this spec):
    - **U1** Topics 1–5 per-claim source lists (not persisted as files).
    - **U2** Gleam v0.26 rebuild 18.443 s → 140.8 ms (131×, 1,400
      modules) — no source persisted; never cite the figure.
    - **U3** TS type-check ~95% of build time; mypy daemon 10×+.
    - **U4** Benchmarks Game ed. 25.03 figures; TechEmpower R23.
    - **U5** HM undecidability citations (Wells 1999 / Henglein 1993).
    - **U6** Similar-name suggestion threshold — settled in this spec
      by decision, not by a standard: difflib ratio ≥ 0.7 (§16). The
      v0 value was 0.6; 0.7 was decided during M1 because 0.6 admits
      false positives once builtins join the candidate pool. No claim
      that it is "standard".
    - **U7** Go compiler-diagnostic philosophy ("deliberately minimal").
    - **U8** Elm "Compiler Errors for Humans" verbatim quotes (fetch
      timed out 6 Nov 2026).
    - **U9** Integer-division lint as a standard practice — none found;
      the newr check is labelled newr-specific (§5.1).
    - **U10** LSP effort estimate (person-weeks) — no number in this
      spec.
    - **U11** PEP 823/824 (PEP 505 split) as drafts.
    - **U12** Valhalla GA version (JDK 25 vs 26 vs 27 vs 28).
    - **U13** C++26 contracts DIS ballot failure (12/8/8).
    - **U14** GitHub reaction/comment totals (#31436, #154391, #32437,
      #19814, #15292).
    - **U15** Decorators demotion reason (May 2026).
    - **U16** Rust still #1 "most admired" in SO 2026 (Elm 94.1%
      contradiction in the same table).
    - **U17** Rust TIOBE top-10 (July 2026).
    - **U18** Haxe TIOBE rank 2025/2026.
    - **U19** DefinitelyTyped current count (6k–15k+ by source).
    - **U20** PYPL Python % in 2026 (29.7% vs ~35.8%).
    - **U21** RustConf founding year / attendance; Go "25 keywords";
      Go SO 2025 usage %.
    - **U22** npm `bin` → Python program precedent — moot, npm dropped
      (item 9 above).
    - **U23** PyPI `newr-lang` free-ness — settled by the expansion doc
      (JSON API 404, 6 Nov 2026); re-check at publish time, not now.
    - **Conflicts noted honestly:** Gleam described as both HM and
      bidirectional — do not cite it as a clean example of either; Rust
      "did you mean" is folk usage (the style guide discourages it);
      Elm examples are reported, not compiler-verified; SO 2026 survey
      contradictory (Rust 67.4% vs Elm 94.1%); TIOBE Nov 2025 #2–#5
      order disputed; Reddit/X not directly retrievable in that session.
