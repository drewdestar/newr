# newr

newr is a general-purpose programming language. You write one readable
source file, and the compiler will produce readable output in seven
target languages — one readable source, seven readable outputs, with
each target's libraries available. A Discord bot is one use of newr,
through the `discord` module; the language itself is not bot-specific.

Version 0.2.0. Python 3.10+. The compiler is Python standard library only.

## The v1 story

One readable source, seven readable outputs, with each target's libraries
available. The v1 domain is small cross-platform command-line tools.

## Quick example

`examples/hello.newr`:

```newr
print "Hello, world!"
```

Check it:

```sh
newr check examples/hello.newr
```

## Status

- M0: spec frozen (see [SPEC.md](SPEC.md)).
- M1: front end COMPLETE — lexer, parser, AST, resolver, checker, and
  the discord checker. `newr check` verifies programs; `newr build --check`
  runs the same checks.
- M2: Python emitter COMPLETE.
- M3: JavaScript emitter COMPLETE.
- M4: Java + C# emitters COMPLETE — suite 902/902, conformance 74/74
  on all four targets (py, js, java, cs), cold re-evaluation CLEAN.
- M5: Rust + Kotlin + Dart emitters COMPLETE, suite 963/963,
  conformance 74/74 on all seven targets (py, js, java, cs, rs, kt,
  dart), cold re-evaluation CLEAN.
- M6: the toolchain, `newr fmt` (canonical formatting), `newr init`
  (project scaffold + `newr.json`), `newr test` (test functions and
  test blocks), `newr fix` (safe E3001 renames), `newr docs`
  (generated docs from `///` comments). Suite 1209/1209,
  conformance 79/79 on all seven targets. Version 0.2.0.

`newr build --target py|js|java|cs <file.newr>` emits readable code
for the chosen target. See [BUILD_REPORT.md](BUILD_REPORT.md) for
per-milestone verification notes.

## Install

Planned distribution (not published yet — `newr-lang` is not on PyPI
as of 6 Nov 2026, and installing it today will 404):

```sh
pip install newr-lang
```

Planned alongside it: a pipx install, a zipapp single file, GitHub
Releases, and a PowerShell installer. Until then, run it from a
checkout: `python newr check <file.newr>`. The zipapp is a manual
smoke test in M1 (`python -m zipapp . -o newr.pyz`), not a release
artifact yet.

## Usage

```sh
newr check <file.newr>        # parse + resolve + check, print errors and warnings
newr build --check <file.newr> # same checks; writes nothing
newr build --target py|js|java|cs|rs|kt|dart <file.newr> # emit readable code for the target
newr fmt <file.newr>          # rewrite in canonical form (see `newr help fmt`)
newr fix <file.newr>          # apply safe fixes for errors that have one (see `newr help fix`)
newr test <file.newr>         # run the file's tests (see `newr help test`)
newr docs                     # generate docs/api.md and docs/examples.md (see `newr help docs`)
```

`newr build` without `--check` or `--target` is a usage error.
Exit codes: 0 success, 1 usage, 2 environment, 4 build error.
(There is no exit code 3 in M1 — the spec reserves 3 for network errors,
which no M1 command can produce.)

## Documenting your code

A `///` line at the start of a logical line documents the declaration
directly below it:

```newr
/// Doubles a number.
fn double(x: int) -> int:
  return x * 2
```

A `///` block at the top of a file, followed by a blank line, is the
file header. `newr docs` renders the comments into `docs/api.md`
(the stdlib reference) and `docs/examples.md` (one section per
example program). See [docs/guide.md](docs/guide.md).

## The language, briefly

- Types: `int` (signed 64-bit, checked overflow is a runtime error),
  `float` (IEEE-754 binary64), `str`, `bool`, `list[T]`, `dict[K, V]`,
  and more. Nullability is spelled `T?` (for example `str?`); the
  null value is `none`.
- No implicit numeric coercion: `int` does not convert to `float`
  implicitly — use `float(x)`. On ints, `/` truncates toward zero, `%`
  takes the sign of the dividend, and division by zero is a runtime error.
- `while`, recursion, closures and lambdas, ternary `cond ? a : b`,
  `?.` / `??`, pipeline `|>` (the piped value becomes the first
  argument: `a |> f(b)` is `f(a, b)`), typed enums, a core `match`
  statement, runtime-only `requires` / `ensures` contracts,
  statement-level `?` propagation, `try` blocks returning
  `Result[T, error]`, `try` / `catch` / `finally`, `pub` visibility,
  and qualified imports (`import "shop_items.newr"` binds the module
  under its file stem; call it as `shop_items.price_of(...)`).
- The `discord` module is where the bot surface lives: `bot` /
  `command` / `event` declarations, `store` / economy operations,
  `strings` blocks + `t()`, `task`, and embeds. Bot blocks implicitly
  import it. General programs use `file` I/O for persistence in v1;
  a general `db` module is planned for v1.1.

See [SPEC.md](SPEC.md) for the full specification.

## Targets

| Target | Library | Priority | Status |
| --- | --- | --- | --- |
| Python | discord.py 2.7.1 | P0 | done in M2 |
| TypeScript | discord.js ^14.28.1 (Node 24 LTS) | P0 | done in M3 |
| Java | JDA 6.7.0 | P1 | done in M4 |
| C# | Discord.Net 3.20.2 | P1 | done in M4 |
| Rust | serenity 0.12.5 + poise 0.7.1 | P1 | in progress in M5 |
| Kotlin | Kord 0.18.1 | P2 | in progress in M5 |
| Dart | nyxx 6.9.1 | P2 | in progress in M5 |

## Project layout

```
SPEC.md            the language specification (v0.2.0)
compiler/          the compiler front end (lexer, parser, AST, resolver, checker)
tests/             front-end test suite (python -m unittest discover -s tests)
examples/          .newr examples, plus the v0 golden outputs (hello.py, showcase.py)
conformance/       the conformance corpus — seeded in M1 (a small set of
                   ok/err programs with expected verdicts); it becomes
                   the single definition of correct behavior as it grows,
                   once the emitters land in M2
.github/workflows/ CI
```

## Unverified

- Windows and macOS execution is **UNVERIFIED**. This project is
  developed on Linux only; Windows/macOS support will be proven by the
  CI matrix (ubuntu-latest / macos-latest / windows-latest) once it has
  run green on all three OSes — it has not run yet.
- Performance numbers are measured on Linux only, per milestone, in
  [BUILD_REPORT.md](BUILD_REPORT.md) (benchmarks B1–B7). Nothing is
  claimed for other OSes.

## License

MIT — see [LICENSE](LICENSE).
