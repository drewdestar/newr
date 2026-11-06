# newr

newr is a general-purpose programming language. You write one readable
source file, and the compiler will produce readable output in seven
target languages — one readable source, seven readable outputs, with
each target's libraries available. A Discord bot is one use of newr,
through the `discord` module; the language itself is not bot-specific.

Version 0.1.0. Python 3.10+. The compiler is Python standard library only.

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
  the discord checker, with 411 tests green. `newr check` verifies
  programs; `newr build --check` runs the same checks.

The compiler currently does `build --check` / `check` only and emits
nothing yet. Emitters land in M2+.

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
```

`newr build` without `--check` is an error in M1: there is no emitter
yet. Exit codes: 0 success, 1 usage, 2 environment, 4 build error.
(There is no exit code 3 in M1 — the spec reserves 3 for network errors,
which no M1 command can produce.)

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
| Python | discord.py 2.7.1 | P0 | emitter lands M2+ |
| TypeScript | discord.js ^14.28.1 (Node 24 LTS) | P0 | emitter lands M2+ |
| Java | JDA 6.7.0 | P1 | emitter lands M2+ |
| C# | Discord.Net 3.20.2 | P1 | emitter lands M2+ |
| Rust | serenity 0.12.5 + poise 0.7.1 | P1 | emitter lands M2+ |
| Kotlin | Kord 0.18.1 | P2 | emitter lands M2+ |
| Dart | nyxx 6.9.1 | P2 | emitter lands M2+ |

## Project layout

```
SPEC.md            the language specification (v0.1.0, frozen at M0)
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
- No performance numbers are claimed anywhere. None have been measured.

## License

MIT — see [LICENSE](LICENSE).
