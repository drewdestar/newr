"""The `newr` command-line tool (SPEC.md §19).

M1: front end only — ``check`` and ``build --check`` run the pipeline
parse -> resolve -> check (core + discord) and report diagnostics. There
are no emitters yet, so ``build`` without ``--check`` is an error that
tells the user to use ``--check`` (emitters land in M2).

Exit codes (SPEC.md §19, v0 shape kept): 0 success, 1 usage error,
2 environment error (unreadable file), 4 build/check error (diagnostics
contain errors). Warnings never fail the build.

Output modes:
- human (default): rustc-style diagnostics on stdout + a one-line
  summary; usage/environment errors on stderr.
- ``--error-format=json``: stdout is a JSON array of diagnostics.
- ``--json`` (v0 global flag): stdout is exactly one JSON object, the
  envelope ``{ok, command, newr, data, warnings, error}``.

``--fast`` (build only in spirit, accepted on check too): parse + name
resolution only — the full type check AND all Discord validation are
skipped. The run prints a warning that the result is not verified.

No POSIX-only code: paths go through pathlib; reads use
``encoding="utf-8"``.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from . import __version__
from .checker import check_program
from .fmt import format_source
from .init import (
    InitError,
    ManifestError,
    VALID_TARGETS,
    default_target_for,
    scaffold,
)
from .db_check import check_db
from .discord_check import check_discord, discord_providers
from .errors import Diagnostic, NewrError, Span
from .resolver import resolve_source

VERSION = __version__

USAGE = """Usage: newr [--json] [--error-format=json] <command> [args]

  check <file> [--fast]  verify a .newr program (parse, resolve, check);
                         writes nothing
  build <file> [--target py|js|java|cs|rs|kt|dart] [-o <out>] [--line-comments] [--check] [--fast]
                         build a .newr program into runnable Python
                         (default: the target in newr.json, else py),
                         JavaScript (--target js),
                         Java (--target java), C# (--target cs),
                         Rust (--target rs), Kotlin (--target kt) or
                         Dart (--target dart)
  fmt <file>... [--check]
                         rewrite .newr files in canonical form
                         (parse only; --check reports files that need
                         formatting instead of rewriting them)
  fix <file>... [--dry-run]
                         apply safe, unambiguous diagnostic fixes
                         (SPEC.md section 5.2); --dry-run prints the
                         fixes without rewriting anything
  init [dir] [--name <name>] [--target py|js|java|cs|rs|kt|dart]
                         scaffold a new project (main.newr + newr.json
                         + locales/ + .env.example + .gitignore);
                         refuses when newr.json or main.newr exists
  test [paths]... [--target py|js|java|cs|rs|kt|dart]
                         run `fn test_*()` functions through one
                         target's emitter; a path is a .newr file
                         or a directory (all .newr files under it);
                         with no paths, every .newr file in the
                         current directory
  docs [--out <dir>]     generate the docs (stdlib API reference
                         from compiler/modules.py plus one section
                         per program in examples/ from its `///`
                         doc comments); writes api.md and
                         examples.md into docs/ by default
  help [command]         show this help, or help for one command
  version                print the newr version
  explain <code>         explain a diagnostic code (also: --explain <code>)

Global flags:
  --json             machine-readable output: stdout is exactly one JSON
                     object (the envelope), diagnostics go in data
  --error-format=json
                     diagnostics as a JSON array on stdout (no --json)
  --explain <code>   explain a diagnostic code
  -h, --help         show this help
  -v, --version      print the newr version

Exit codes: 0 success; 1 usage error; 2 environment error (unreadable
file); 4 build/check error (the program has errors). Warnings never
fail the build.
"""

COMMAND_HELP = {
    "check": """Usage: newr check <file> [--fast]

Verify a .newr program: parse it, resolve names and imports, and run
the core checker plus the discord checker. Writes nothing. Exit 0 when
the program has no errors (warnings are fine), 4 when it has errors,
2 when the file cannot be read.

  --fast   parse + name resolution only; skips the full type check AND
           all Discord validation. The result is not verified — say so
           in the output.
""",
    "build": """Usage: newr build <file> [--target py|js|java|cs|rs|kt|dart] [-o <out>] [--line-comments] [--check] [--fast]

Build a .newr program into runnable code for one target (Python and
TypeScript are P0, Java/C#/Rust P1, Kotlin/Dart P2). When --target is
omitted, the target comes from newr.json (SPEC.md section 11),
searched upward from the program's directory; without a manifest the
default is py, as before. A malformed manifest, or a target value
that is not one of the seven targets, is an error naming the file.

M2: `--target py` (the default) emits Python — one `.py` per `.newr`
file, written next to the input (or to `-o` for the main file), plus a
copy of the runtime module `newr_rt.py` when the program uses a
runtime helper. M3: `--target js` emits JavaScript (CommonJS, one
`.js` per `.newr` file) the same way, plus `newr_rt.js` when used —
and a standalone `<stem>_deploy.js` for bot programs (run it once to
register the slash commands, then `node <stem>.js`). M4: `--target
java` emits Java (one `.java` per `.newr` file, one public class per
file named after the file stem, plus `NewrRt.java` when used — the
runtime source is `newr_rt.java`, renamed on copy because javac
requires the file name to match the public class; run with
`javac *.java && java <Stem>`) and `--target cs` emits C# (one `.cs`
per `.newr` file plus `newr_rt.cs` when used, run with
`dotnet run --project <stem>.csproj`); bot programs also get a
standalone `<stem>_deploy.java` / `<stem>_deploy.cs`. M5: `--target
rs` emits Rust (one `.rs` per `.newr` file plus `newr_rt.rs` when
used, a `Cargo.toml` rides along so the program runs with
`cargo run`; bot programs also get a standalone `<stem>_deploy.rs`),
`--target kt` emits Kotlin (one `.kt` per `.newr` file plus
`newr_rt.kt` when used, run with `kotlinc *.kt -include-runtime
-d <stem>.jar && java -jar <stem>.jar`; bot programs also get a
standalone `<stem>_deploy.kt`), and `--target dart` emits Dart (one
`.dart` per `.newr` file plus `newr_rt.dart` when used, run with
`dart <stem>.dart`; bot programs also get a standalone
`<stem>_deploy.dart`). `--check` runs the same pipeline as
`newr check` and writes nothing.
`--line-comments` adds `# newr:<file>:<line>` comments to the emitted
code. `--fast` parses + resolves only, skipping the full type check
AND all Discord validation — it cannot be combined with emission
(use `--fast --check` to verify only).
""",
    "version": """Usage: newr version

Print the newr version (currently 0.2.0).
""",
    "explain": """Usage: newr explain <code>   (also: newr --explain <code>)

Print the long-form explanation of a diagnostic code, e.g. E2001.
""",
    "help": """Usage: newr help [command]

Show the general help, or help for one command.
""",
    "fmt": """Usage: newr fmt <file>... [--check]

Rewrite .newr files in the canonical form (SPEC.md section 19): 2-space
indent, single spaces around operators, literals and durations keep
their source spelling, `#` and `///` comments are preserved, `print` is written
without parentheses, and the file ends with exactly one newline.
Formatting is idempotent: fmt(fmt(x)) == fmt(x).

Only the parser runs: a file with type errors still formats as long as
it parses. Exit 0 when every file is formatted, 4 when a file has a
parse error or (with --check) needs formatting, 2 when a file cannot be
read, 1 on a usage error.

  --check   do not rewrite anything; print `<path>: ok` or
            `<path>: needs formatting` per file and exit 4 when any
            file needs formatting (for CI).
""",
    "fix": """Usage: newr fix <file>... [--dry-run]

Apply the safe, unambiguous fixes the diagnostics carry (SPEC.md
section 5.2): an undefined name with exactly one close match is
renamed to that name (E3001). The fix is data in the diagnostic; the
command never invents a fix of its own. Every change is printed, and
the fixed text is verified clean before anything is written.

A file with any error that has no safe fix is refused whole: it is
not touched at all, and the blocking errors are printed. Warnings
never block a fix. Exit 0 when every file is fixed or already clean,
4 when a file is refused or has a parse error (or, with --dry-run,
when any file would change), 2 when a file cannot be read, 1 on a
usage error.

  --dry-run   do not rewrite anything; print the fixes that would be
              applied per file and exit 4 when any file would change
              (for CI, like `fmt --check`).
""",
    "init": """Usage: newr init [dir] [--name <name>] [--target py|js|java|cs|rs|kt|dart]

Scaffold a new project in [dir] (the current directory when omitted;
created when missing): main.newr (a hello program), newr.json (the
project manifest, SPEC.md section 11), locales/ (empty for now, v1
strings are inline `strings` blocks, section 15), .env.example (the
secrets template; only bot programs need it), and .gitignore.

The name defaults to the directory name; the target defaults to py
and becomes the manifest's `target`, which `newr build` then uses
when --target is omitted. Exit 0 when the project is created, 1 when
the target is unknown, when more than one directory is given, or
when newr.json or main.newr already exists in the directory (init
never overwrites existing work), 2 when a file cannot be written.

  --name <name>     project name in newr.json (default: directory name)
  --target <target> default build target in newr.json (default: py)
""",
    "test": """Usage: newr test [paths]... [--target py|js|java|cs|rs|kt|dart]

Run the tests of .newr programs: `fn test_*()` functions and
`test "display name":` blocks, in source order across both forms.
Each file is parsed, resolved, and checked like `newr build`,
then built for one target in a temporary directory and executed,
with one try/catch around each test so a failure is reported and
the run continues. A test asserts with the language's own
machinery: `if ...: throw "message"` or a leading `requires
<condition>` in the test body. Test names must be unique within
a file: a block and a `test_` function sharing one name is a
build error.

A path is a .newr file or a directory (every .newr file under it,
recursively); with no paths, every .newr file in the current
directory runs. The target defaults to newr.json (searched upward
from the first path, else the current directory), else py.
Bot programs are skipped, never executed: they need a live Discord
token to start. A file with no tests passes with "no tests".
Exit 0 when every test passes, 4 when a program has a
build error or any test fails, 2 when a file cannot be read or
the target's toolchain is missing, 1 on a usage error.
""",
    "docs": """Usage: newr docs [--out <dir>]

Generate the documentation for this checkout: the stdlib API
reference from compiler/modules.py (SPEC.md section 12) and one
section per program in examples/ from the `///` doc comments in
the source (SPEC.md section 6). Writes api.md, and examples.md
when an examples/ directory exists in the current directory.
The default output directory is docs/.

The generated files are deterministic: running the command twice
produces identical bytes. Exit 0 on success, 2 when the output
directory cannot be written, 1 on a usage error.
""",
}

# ---------------------------------------------------------------- explain
# Long-form explanations for every diagnostic code (SPEC.md §16).
# One or two short paragraphs each: what it means, why it fires, how to
# fix it. Written for humans, not generated from the codes.

EXPLAIN: dict[str, str] = {
    "E1001": """E1001: syntax error.

The parser could not make sense of this line. Check for a missing
colon after a block header, an indent that is not exactly 2 spaces per
level, an unclosed string or bracket, or a keyword in the wrong place.
The span points at the offending token.

This code is also used for "too deeply nested": expressions, blocks,
or types nested more than 200 levels deep are rejected instead of
crashing the compiler. Split the expression or block into smaller
pieces.""",
    "E_BAD_ESCAPE": """E_BAD_ESCAPE: unknown or malformed escape.

Only these escapes are allowed in a string: \\" \\\\ \\n \\t \\r and
\\u{XXXX} (a Unicode code point in braces). Check the escape after
the backslash — for \\u{...} the braces and the hex digits are both
required.""",
    "E_BAD_INDENT": """E_BAD_INDENT: wrong indentation.

Indent by exactly 2 spaces per level, and dedent only to a level
the block was opened at. Tabs are never allowed (see
E_TAB_INDENT).""",
    "E_BAD_LINE_ENDING": """E_BAD_LINE_ENDING: lone carriage return.

A lone CR (carriage return) is not a line ending in newr. Lines end
with LF, or with CRLF. Convert the file's line endings to LF or
CRLF.""",
    "E_BAD_NUMBER": """E_BAD_NUMBER: malformed number literal.

The digits after 0x / 0b / 0o are not valid for that base, or the
number has no digits at all (e.g. `0x` with nothing after it).
Underscores group digits, so one must sit between two digits
(`1_000`, `0xF_F`): a leading, trailing, or doubled underscore
(`1__2`, `1_`, `0x_FF`) is an error. Write a valid literal, e.g.
`0xFF`, `0b1010`, `0o17`.""",
    "E_INT_OVERFLOW": """E_INT_OVERFLOW: integer literal out of range.

`int` is a 64-bit signed integer on every target: -9223372036854775808
to 9223372036854775807. This literal is outside that range. The one
exception is the minimum itself, written as `-9223372036854775808`
(the minus is part of the literal there). Use a smaller literal, or
`float`.""",
    "E_STRAY_BRACE": """E_STRAY_BRACE: stray } in a string.

A `}` inside a string starts an interpolation only as `${...}`; a
lone `}` is an error. Write `}}` for a literal `}`.""",
    "E_TAB_INDENT": """E_TAB_INDENT: tab indentation.

Tabs are not allowed for indentation in newr — indent by exactly 2
spaces per level.""",
    "E_UNEXPECTED_CHAR": """E_UNEXPECTED_CHAR: unexpected character.

The character here is not part of the newr language. Check for a
typo, or for a character pasted from elsewhere (for example a
non-ASCII quote).""",
    "E_UNTERMINATED_STRING": """E_UNTERMINATED_STRING: unterminated string.

A string was opened but the file (or the line) ended before its
closing quote. Add the closing `"` on the same line (a newline
inside a string must be written as \\n).""",
    "E2001": """E2001: mismatched types.

The value here has a different type from the one that was expected.
newr has no implicit numeric coercion: an int is never silently a
float — write float(5) or 5.0 to convert explicitly. The diagnostic
carries expected/found, so the mismatch is named, not guessed.""",
    "E2002": """E2002: type annotation needed.

An empty list, dict, or set has no element type for the checker to
infer — there is nothing in it to infer from. Write the type
explicitly, e.g. `let xs: list[int] = []`.""",
    "E2003": """E2003: heterogeneous list/dict.

Every element of a list (and every key, and every value, of a dict)
must have the identical type. newr does not unify or coerce element
types. Split the collection, or convert the elements to one type
first.""",
    "E2004": """E2004: non-boolean condition.

An if/while condition or a match guard must be bool. If the condition
is a comparison, check the operator; if it is a value, compare it
explicitly (e.g. `if x != 0:` instead of `if x:`).""",
    "E2005": """E2005: use before definition.

A local variable was used before its `let` in the same block. Move
the `let` above the use. Module-level names (functions, structs,
top-level lets) may be forward-referenced, so this only fires for
locals — and for a let's own right-hand side referring to itself
(`let x = x + 1`).""",
    "E2006": """E2006: non-exhaustive match.

The match does not cover every possible value. For an enum scrutinee,
add arms for the missing variants; for any other scrutinee, add a `_`
arm. Exhaustiveness is an error in newr, not a warning: a match that
can fall through is a bug waiting to happen.""",
    "E2007": """E2007: break/continue/return in the wrong place.

`break` and `continue` only make sense inside a loop, and `return`
with a value only inside a function. A bare `return` at the top level
exits the program; command/event handlers are not functions, so
`return <value>` is not allowed in them either.""",
    "E2008": """E2008: Discord rule violation.

A rule of the discord module was broken: a bad or uppercase command
name, a duplicate command, a description or reply over the length
limit, a missing component timeout, a duplicate custom_id, an unknown
intent, a store schema problem, and so on. The message names the
rule. Only discord-module rules live here — core language errors use
other codes, so a Discord API change can never move a core error.""",
    "E2009": """E2009: i18n violation.

A strings/t() rule was broken: a locale is missing a key that another
locale defines, a t() key does not exist, or a t() call's arguments
do not match the template's placeholders. Every locale must define
the same key set.""",
    "E2010": """E2010: not all paths answer.

A command handler can finish without answering the interaction. Every
branch of a handler must end in a reply, defer, edit, or followup (or
throw). An `if` counts only with an `else` whose branches all answer;
`for`/`while` never count, because they may run zero times.""",
    "E2011": """E2011: db module rule violation.

A rule of the db module was broken: an unknown db function, the wrong
number of arguments, a handle or SQL argument of the wrong type, a
bound value that is not an int, float, str, or bool, a placeholder
count mismatch between the SQL and the bound values, a `db.query`
with no expected row type (annotate the let with `list[RowStruct]`),
a row struct field that cannot be read from a row, a second
`db.begin` on a handle already in a transaction, or a use of a closed
handle. The diagnostic names the rule and the fix.""",
    "E3001": """E3001: undefined name.

There is no binding with this name in scope. Check the spelling — the
help names a similar name when one exists (difflib ratio >= 0.7) and
the diagnostic carries a machine-applicable fix for it. A module name
used as a value, and an unknown type in an annotation, are reported
with this code too.""",
    "E3002": """E3002: wrong number of arguments.

The call passes a different number of arguments from the function's
signature. Check the signature; keyword arguments must name real
parameters.""",
    "E3003": """E3003: duplicate definition.

Two definitions in the same scope share a name. Rename one of them.
Shadowing a name from an *outer* scope is only a warning (W1004);
same-scope duplicates are errors.""",
    "E3004": """E3004: import cycle.

Module A imports module B, which (directly or through others) imports
A again. Break the cycle by moving the shared declarations into a
third module that both import.""",
    "E3005": """E3005: name not exported.

The name exists in the imported module but is not marked `pub`.
Add `pub` to the declaration in the imported module, or use an
exported name.""",
    "E3006": """E3006: bot/command/event/task in an imported file.

Imported files contribute functions, structs, enums, strings, and
store tables only. Bot blocks, commands, events, tasks, and top-level
statements must live in the main file — the file passed to
`newr build`.""",
    "E3007": """E3007: reserved name shadowed.

Builtin names (print, len, t, err, log, ...) and handler bindings
(user, guild, channel, bot, interaction, message, values) cannot be
redefined. One exception: catch variables may shadow builtins —
`catch err:` is the spec's own idiom.""",
    "E3008": """E3008: unknown module / missing file.

The import cannot be resolved. Named modules in v1: math, time,
random, file, env, discord. `json` arrives in v1.1. File imports
resolve relative to the importing file — check the path and the
spelling.""",
    "W1001": """W1001: unused variable / parameter / option.

A `let`/`const`, parameter, or command option is never read. An
unused binding is almost always a bug — a leftover, or a use that was
typo'd (see also E3001's similar-name help). Parameters of `pub`
functions do not warn: they are public API. Warnings never fail the
build.""",
    "W1002": """W1002: unused import.

A module was imported but never referenced. Remove the import.
`import discord` does not warn when the program has bot blocks —
they implicitly import the discord module.""",
    "W1003": """W1003: unreachable code.

Statements after `return`, `throw`, `break`, or `continue` in the
same block never run. Move them, or remove them.""",
    "W1004": """W1004: shadowing.

A declaration hides a name from an outer scope. It is legal, but
easy to misread — rename it if the shadowing is not intentional.""",
    "W1005": """W1005: always-true/false literal condition.

An if/elif/while condition is a constant literal (`true`/`false`),
so one branch can never run. If the literal is a placeholder, replace
it with the real condition; if the branch is dead, remove it.""",
    "W1006": """W1006: unbounded loop in a handler body.

A handler that loops forever (or until a condition that may never
arrive) will outlive the interaction token, which expires after 15
minutes. This is a warning, not a ban — the core language allows
`while`; the discord checker only warns that a handler is the wrong
place for an unbounded loop.""",
    "W1007": """W1007: extra t() argument.

A t() call was given more arguments than its template has
placeholders. The extras are ignored. (An argument whose *name* is
not a template placeholder is an error, E2009, not this warning.)""",
    "W1008": """W1008: (newr-specific warning) integer division in a float
context.

`1 / 2` is truncating integer division (0), not 0.5 — and it is
flowing into a float context, where 0.5 was almost certainly meant.
Write `1.0 / 2.0` or `float(1) / 2`. This warning is newr-specific:
no other language is being quoted as doing it.""",
    "W1009": """W1009: reply length cannot be verified.

Part of a reply is an unknown-length `str`, so the 2000-character
reply limit could not be checked statically. The reply may still be
fine at runtime — but if the value can be long, guard it or shorten
the fixed part.""",
    "W1010": """W1010: privileged intent declared.

A privileged intent (members, message_content, presences) was
declared. Discord requires verification/approval for privileged
intents in bots in 10,000+ servers. If the bot will stay small, the
intent is fine; if it will grow, plan the verification early.""",
}

# ---------------------------------------------------------------- pipeline


def _read_source(path: Path) -> str:
    """Read a source file as UTF-8, or raise an environment error.

    ``newline=""`` keeps the raw bytes' line endings in the text, so
    the lexer's raw-byte spans (M-06) and its lone-CR detection (M-07)
    see the file exactly as it is on disk. A non-UTF-8 file is an
    environment error (exit 2), not a traceback (M-02).
    """
    try:
        with open(path, encoding="utf-8", newline="") as fh:
            return fh.read()
    except UnicodeDecodeError as exc:
        raise _EnvError(
            f"cannot read {path}: not valid UTF-8 "
            f"(SPEC.md §3.1 requires UTF-8; {exc})") from exc
    except OSError as exc:
        raise _EnvError(f"cannot read {path}: {exc}") from exc


class _UsageError(Exception):
    """Exit 1: bad command, flags, or arguments."""


class _EnvError(Exception):
    """Exit 2: the environment (missing/unreadable file)."""


def _run_pipeline(path: Path, *, fast: bool) -> tuple[
        object | None, list[Diagnostic], str]:
    """Parse + resolve (+ check, unless ``fast``).

    Returns (program, diagnostics, source_text). Parse errors raise
    NewrError carrying the single diagnostic. The file is read once;
    ``resolve_source`` parses that text (imports resolve relative to
    the file's path).
    """
    source = _read_source(path)
    resolved = resolve_source(source, str(path))
    diagnostics: list[Diagnostic] = list(resolved.diagnostics)
    if not fast:
        providers = discord_providers(resolved.program)
        core = check_program(resolved.program, providers=providers)
        diagnostics += core.diagnostics
        diagnostics += check_discord(resolved.program)
        diagnostics += check_db(resolved.program)
    return resolved.program, diagnostics, source


def _diag_json(diag: Diagnostic, source: str) -> dict:
    return diag.to_json(source)


def _source_cache(main_path: Path, main_source: str):
    """file -> source text, for rendering diagnostics (which may come
    from imported files, not just the main file)."""
    cache: dict[str, str] = {str(main_path): main_source}

    def get_source(file: str) -> str:
        if file in cache:
            return cache[file]
        try:
            with open(file, encoding="utf-8", newline="") as fh:
                text = fh.read()
        except (OSError, UnicodeDecodeError):
            text = ""
        cache[file] = text
        return text

    return get_source


def _envelope(command: str, data: dict | None,
              diagnostics: list[Diagnostic]) -> dict:
    """The --json envelope (v0 shape kept)."""
    errors = [d for d in diagnostics if d.is_error()]
    warnings = [d for d in diagnostics if not d.is_error()]
    first = errors[0] if errors else None
    return {
        "ok": not errors,
        "command": command,
        "newr": VERSION,
        "data": data,
        "warnings": [w.to_json() for w in warnings],
        "error": None if first is None else {
            "code": first.code,
            "message": first.message,
            "hint": first.help or "",
        },
    }


def _emit_diagnostics(diagnostics: list[Diagnostic], get_source, *,
                      json_mode: bool, error_format_json: bool) -> None:
    """Print diagnostics in the requested format (stdout only)."""
    if json_mode:
        return  # the envelope carries them; see _emit_envelope
    if error_format_json:
        print(json.dumps([d.to_json(get_source(d.span.file))
                          for d in diagnostics]))
        return
    for d in diagnostics:
        print(d.format(get_source(d.span.file)))


def _check_or_build(args: list[str], *, command: str,
                    json_mode: bool, error_format_json: bool,
                    fast: bool) -> int:
    """Shared body of `check` and `build --check`."""
    if fast:
        print("warning: --fast skips the full type check and all "
              "Discord validation; the result is not verified",
              file=sys.stderr)
    unknown = [a for a in args if a.startswith("-")]
    if unknown:
        raise _UsageError(f"unknown flag(s) for `newr {command}`: "
                          + ", ".join(unknown))
    positional = [a for a in args if not a.startswith("-")]
    if len(positional) != 1:
        raise _UsageError(
            f"`newr {command}` needs exactly one .newr file "
            f"(got {len(positional)})")
    path = Path(positional[0])
    try:
        program, diagnostics, source = _run_pipeline(path, fast=fast)
    except NewrError as exc:
        diagnostics = [exc.diagnostic]
        try:
            source = _read_source(path)
        except _EnvError:
            source = ""
        program = None
    except RecursionError:
        # M-01 backstop: the depth guards should catch deep nesting
        # first, but if anything still recurses (e.g. a pathological
        # struct cycle), the failure is still a clean E1001, exit 4 —
        # never a raw traceback, and --json still gets its envelope.
        diagnostics = [Diagnostic(
            span=Span(str(path), 0, 1), code="E1001",
            message="too deeply nested",
            help="split the expression or block into smaller pieces — "
                 "newr limits nesting to 200 levels so a bad input "
                 "cannot exhaust the stack")]
        try:
            source = _read_source(path)
        except _EnvError:
            source = ""
        program = None
    return _finish(command, path, program, diagnostics, source,
                   json_mode=json_mode,
                   error_format_json=error_format_json)


def _finish(command: str, path: Path, program: object | None,
            diagnostics: list[Diagnostic], source: str, *,
            json_mode: bool, error_format_json: bool) -> int:
    errors = [d for d in diagnostics if d.is_error()]
    warnings = [d for d in diagnostics if not d.is_error()]
    get_source = _source_cache(path, source)
    if json_mode:
        files = []
        if program is not None:
            files = list(getattr(program, "files", []))
        data = {
            "file": str(path),
            "files": files,
            "diagnostics": [d.to_json(get_source(d.span.file))
                            for d in diagnostics],
        }
        print(json.dumps(_envelope(command, data, diagnostics)))
        return 0 if not errors else 4
    _emit_diagnostics(diagnostics, get_source, json_mode=json_mode,
                      error_format_json=error_format_json)
    if error_format_json:
        # stdout is pure JSON in this mode; the exit code is the verdict.
        return 0 if not errors else 4
    if errors:
        print(f"{path}: {len(errors)} error(s), "
              f"{len(warnings)} warning(s)")
        return 4
    if warnings:
        print(f"{path}: ok, {len(warnings)} warning(s)")
    else:
        print(f"{path}: ok")
    return 0


# ---------------------------------------------------------------- commands


def _cmd_fmt(args: list[str], *, json_mode: bool,
             error_format_json: bool) -> int:
    """`newr fmt <file>... [--check]`: rewrite files canonically.

    Parse only (no resolve/check), so a file with type errors still
    formats. Files are processed in order; a parse error is reported
    for that file and the rest still run. An unreadable file aborts
    the run with exit 2, like `check` and `build`.
    """
    check_only = "--check" in args
    args = [a for a in args if a != "--check"]
    unknown = [a for a in args if a.startswith("-")]
    if unknown:
        raise _UsageError(f"unknown flag(s) for `newr fmt`: "
                          + ", ".join(unknown))
    if not args:
        raise _UsageError("`newr fmt` needs at least one .newr file")
    files: list[dict] = []
    diagnostics: list[Diagnostic] = []
    sources: dict[str, str] = {}
    failed = False
    for name in args:
        path = Path(name)
        source = _read_source(path)
        sources[str(path)] = source
        try:
            formatted = format_source(source, str(path))
        except NewrError as exc:
            diagnostics.append(exc.diagnostic)
            failed = True
            if not json_mode and not error_format_json:
                print(exc.diagnostic.format(source))
                print(f"{path}: 1 error(s), 0 warning(s)")
            continue
        except RecursionError:
            diagnostics.append(Diagnostic(
                span=Span(str(path), 0, 1), code="E1001",
                message="too deeply nested",
                help="split the expression or block into smaller "
                     "pieces, newr limits nesting to 200 levels so "
                     "a bad input cannot exhaust the stack"))
            failed = True
            continue
        changed = formatted != source
        if check_only:
            files.append({"file": str(path), "changed": changed})
            if changed:
                failed = True
            if not json_mode and not error_format_json:
                print(f"{path}: "
                      f"{'needs formatting' if changed else 'ok'}")
            continue
        if changed:
            try:
                with open(path, "w", encoding="utf-8",
                          newline="") as fh:
                    fh.write(formatted)
            except OSError as exc:
                raise _EnvError(
                    f"cannot write {path}: {exc}") from exc
        files.append({"file": str(path), "changed": changed})
        if not json_mode and not error_format_json:
            print(f"{path}: {'formatted' if changed else 'ok'}")
    if json_mode:
        data = {"files": files,
                "diagnostics": [d.to_json(sources.get(d.span.file, ""))
                                for d in diagnostics]}
        envelope = _envelope("fmt", data, diagnostics)
        if failed and envelope["ok"]:
            # --check with files needing formatting fails with no
            # diagnostic; the envelope must agree with the exit code.
            envelope["ok"] = False
            envelope["error"] = {
                "code": "FMT",
                "message": "one or more files need formatting",
                "hint": "run `newr fmt` to rewrite them",
            }
        print(json.dumps(envelope))
        return 4 if failed else 0
    if error_format_json:
        print(json.dumps([d.to_json(sources.get(d.span.file, ""))
                          for d in diagnostics]))
        return 4 if failed else 0
    return 4 if failed else 0


def _cmd_fix(args: list[str], *, json_mode: bool,
             error_format_json: bool) -> int:
    """`newr fix <file>... [--dry-run]`: apply diagnostic fixes.

    Runs the full front end per file and applies the fixes the
    diagnostics carry (compiler/fix.py). Files are processed in
    order; a refused or unparsable file is reported and the rest
    still run. An unreadable file aborts the run with exit 2, like
    `check` and `build`.
    """
    from .fix import FixRefused, fix_text

    dry_run = "--dry-run" in args
    args = [a for a in args if a != "--dry-run"]
    unknown = [a for a in args if a.startswith("-")]
    if unknown:
        raise _UsageError(f"unknown flag(s) for `newr fix`: "
                          + ", ".join(unknown))
    if not args:
        raise _UsageError("`newr fix` needs at least one .newr file")
    files: list[dict] = []
    diagnostics: list[Diagnostic] = []
    sources: dict[str, str] = {}
    failed = False
    for name in args:
        path = Path(name)
        source = _read_source(path)
        sources[str(path)] = source
        try:
            new_source, applied = fix_text(source, str(path))
        except NewrError as exc:
            diagnostics.append(exc.diagnostic)
            failed = True
            if not json_mode and not error_format_json:
                print(exc.diagnostic.format(source))
                print(f"{path}: cannot fix (parse error)")
            files.append({"file": str(path), "changed": False,
                          "fixes": []})
            continue
        except FixRefused as exc:
            diagnostics.extend(exc.diagnostics)
            failed = True
            if not json_mode and not error_format_json:
                for diag in exc.diagnostics:
                    print(diag.format(source))
                if exc.reason:
                    print(f"{path}: refused, {exc.reason}")
                else:
                    print(f"{path}: refused, "
                          f"{len(exc.diagnostics)} error(s) "
                          f"have no safe fix")
            files.append({"file": str(path), "changed": False,
                          "fixes": []})
            continue
        changed = new_source != source
        fixes = [{"code": change.code, "line": change.line,
                  "col": change.col, "old": change.old,
                  "new": change.new} for change in applied]
        if dry_run and changed:
            failed = True
        if changed and not dry_run:
            try:
                with open(path, "w", encoding="utf-8",
                          newline="") as fh:
                    fh.write(new_source)
            except OSError as exc:
                raise _EnvError(
                    f"cannot write {path}: {exc}") from exc
        files.append({"file": str(path), "changed": changed,
                      "fixes": fixes})
        if not json_mode and not error_format_json:
            for change in applied:
                print(f"{path}:{change.line}:{change.col}: "
                      f"{change.code} `{change.old}` -> "
                      f"`{change.new}`")
            if not applied:
                print(f"{path}: ok")
            elif dry_run:
                print(f"{path}: would fix {len(applied)} error(s)")
            else:
                print(f"{path}: fixed {len(applied)} error(s)")
    if json_mode:
        data = {"files": files,
                "diagnostics": [d.to_json(sources.get(d.span.file, ""))
                                for d in diagnostics]}
        envelope = _envelope("fix", data, diagnostics)
        if failed and envelope["ok"]:
            # --dry-run with files that would change fails with no
            # diagnostic; the envelope must agree with the exit code
            # (same rule as `fmt --check` in _cmd_fmt).
            envelope["ok"] = False
            envelope["error"] = {
                "code": "FIX",
                "message": "one or more files could not be fixed",
                "hint": "run `newr check` to see the blocking errors",
            }
        print(json.dumps(envelope))
        return 4 if failed else 0
    if error_format_json:
        print(json.dumps([d.to_json(sources.get(d.span.file, ""))
                          for d in diagnostics]))
        return 4 if failed else 0
    return 4 if failed else 0


def _cmd_init(args: list[str], *, json_mode: bool,
              error_format_json: bool) -> int:
    """`newr init [dir] [--name <name>] [--target <target>]`: scaffold
    a project (SPEC.md section 11 and section 19).

    Refuses when `newr.json` or `main.newr` already exists in the
    target directory, so init never overwrites existing work. A
    directory that does not exist is created.
    """
    name = None
    target = "py"
    rest: list[str] = []
    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--name" and i + 1 < len(args):
            name = args[i + 1]
            i += 2
        elif arg.startswith("--name="):
            name = arg.split("=", 1)[1]
            i += 1
        elif arg == "--target" and i + 1 < len(args):
            target = args[i + 1]
            i += 2
        elif arg.startswith("--target="):
            target = arg.split("=", 1)[1]
            i += 1
        else:
            rest.append(arg)
            i += 1
    unknown = [a for a in rest if a.startswith("-")]
    if unknown:
        raise _UsageError("unknown flag(s) for `newr init`: "
                          + ", ".join(unknown))
    if len(rest) > 1:
        raise _UsageError(
            "`newr init` takes at most one directory "
            f"(got {len(rest)})")
    directory = Path(rest[0]) if rest else Path(".")
    try:
        created = scaffold(directory, name=name, target=target)
    except InitError as exc:
        raise _UsageError(str(exc)) from exc
    except OSError as exc:
        raise _EnvError(f"cannot scaffold {directory}: {exc}") from exc
    if json_mode:
        manifest = json.loads(
            (directory / "newr.json").read_text(encoding="utf-8"))
        data = {"dir": str(directory),
                "name": manifest["name"],
                "files": [str(p) for p in created]}
        print(json.dumps(_envelope("init", data, [])))
        return 0
    if error_format_json:
        print("[]")
        return 0
    for path in created:
        print(f"created {path}")
    main_display = (directory / "main.newr"
                    if str(directory) != "." else Path("main.newr"))
    print(f"next: newr build {main_display}")
    return 0


def _cmd_docs(args: list[str], *, json_mode: bool,
              error_format_json: bool) -> int:
    """`newr docs [--out <dir>]`: generate the documentation.

    Writes the stdlib API reference (from compiler/modules.py) as
    api.md, and one section per program in examples/ (from the
    `///` doc comments in the source) as examples.md, into the
    output directory (docs/ by default). See compiler/docs.py.
    """
    from .docs import generate_docs

    out = Path("docs")
    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--out" and i + 1 < len(args):
            out = Path(args[i + 1])
            i += 2
        elif arg.startswith("--out="):
            out = Path(arg.split("=", 1)[1])
            i += 1
        elif arg.startswith("-"):
            raise _UsageError(f"unknown flag(s) for `newr docs`: {arg}")
        else:
            raise _UsageError(
                f"unexpected argument for `newr docs`: {arg}")
    try:
        written = generate_docs(out, Path.cwd())
    except OSError as exc:
        raise _EnvError(f"cannot write docs to {out}: {exc}") from exc
    data = {"files": [str(out / name) for name in written]}
    if json_mode:
        print(json.dumps(_envelope("docs", data, [])))
        return 0
    if error_format_json:
        print("[]")
        return 0
    for name in written:
        print(f"wrote {out / name}")
    return 0


def _cmd_check(args: list[str], *, json_mode: bool,
               error_format_json: bool) -> int:
    fast = "--fast" in args
    args = [a for a in args if a != "--fast"]
    return _check_or_build(args, command="check", json_mode=json_mode,
                           error_format_json=error_format_json, fast=fast)


def _cmd_test(args: list[str], *, json_mode: bool,
              error_format_json: bool) -> int:
    """`newr test [paths]... [--target ...]`: run `fn test_*()`
    functions through one target's emitter (SPEC.md section 19,
    section 24.2 idea 13). See compiler/test.py for the runner."""
    from .test import (
        TestEnvError as _TestEnvError,
        collect_paths,
        exit_code as _test_exit_code,
        format_human,
        run_tests,
        to_json_data,
    )
    target = None
    target_given = False
    rest: list[str] = []
    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--target" and i + 1 < len(args):
            target = args[i + 1]
            target_given = True
            i += 2
        elif arg.startswith("--target="):
            target = arg.split("=", 1)[1]
            target_given = True
            i += 1
        else:
            rest.append(arg)
            i += 1
    if target_given and target not in VALID_TARGETS:
        raise _UsageError(
            f"unknown target `{target}` (targets: "
            f"{', '.join(VALID_TARGETS)})")
    unknown = [a for a in rest if a.startswith("-")]
    if unknown:
        raise _UsageError("unknown flag(s) for `newr test`: "
                          + ", ".join(unknown))
    if target is None:
        # No explicit --target: the default comes from newr.json
        # (SPEC.md section 11), searched upward from the first
        # path, else the current directory; without a manifest it
        # stays py, as for `newr build`.
        anchor = Path(rest[0]) if rest else Path.cwd() / "main.newr"
        try:
            target = default_target_for(anchor)
        except ManifestError as exc:
            raise _UsageError(str(exc)) from exc
    try:
        paths = collect_paths(rest)
    except _TestEnvError as exc:
        raise _EnvError(str(exc)) from exc
    if not paths:
        if json_mode:
            print(json.dumps(_envelope(
                "test", {"files": [], "passed": 0, "failed": 0,
                         "not_run": 0, "build_errors": 0}, [])))
            return 0
        if error_format_json:
            print("[]")
            return 0
        print("no .newr files found")
        return 0
    try:
        results = run_tests(paths, target)
    except _TestEnvError as exc:
        raise _EnvError(str(exc)) from exc
    code = _test_exit_code(results)
    if json_mode:
        data = to_json_data(results)
        envelope = _envelope("test", data, [])
        if code != 0 and envelope["ok"]:
            # A test failure carries no diagnostic; the envelope
            # must still agree with the exit code (same rule as
            # `fmt --check` in _cmd_fmt).
            failing = next(
                (r for r in results
                 if r.error or r.failed or r.not_run or r.run_error),
                None)
            if failing is not None and failing.diagnostics_json:
                first = failing.diagnostics_json[0]
                envelope["error"] = {
                    "code": first.get("code", "E4001"),
                    "message": first.get("message", ""),
                    "hint": first.get("help", ""),
                }
            elif failing is not None:
                envelope["error"] = {
                    "code": "TEST_FAILED",
                    "message": (f"{failing.failed} test(s) failed "
                                f"in {failing.file}"),
                    "hint": "run `newr test` without --json for "
                            "the per-test report",
                }
            envelope["ok"] = False
        print(json.dumps(envelope))
        return code
    if error_format_json:
        diags = [d for r in results for d in r.diagnostics_json]
        print(json.dumps(diags))
        return code
    print(format_human(results))
    return code


def _cmd_build(args: list[str], *, json_mode: bool,
               error_format_json: bool) -> int:
    target = None
    target_given = False
    output = None
    line_comments = False
    rest: list[str] = []
    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--target" and i + 1 < len(args):
            target = args[i + 1]
            target_given = True
            i += 2
        elif arg.startswith("--target="):
            target = arg.split("=", 1)[1]
            target_given = True
            i += 1
        elif arg in ("-o", "--output") and i + 1 < len(args):
            output = args[i + 1]
            i += 2
        elif arg.startswith("--output="):
            output = arg.split("=", 1)[1]
            i += 1
        elif arg == "--line-comments":
            line_comments = True
            i += 1
        else:
            rest.append(arg)
            i += 1
    if target_given and target not in VALID_TARGETS:
        raise _UsageError(
            f"unknown target `{target}` (targets: "
            f"{', '.join(VALID_TARGETS)})")
    fast = "--fast" in rest
    rest = [a for a in rest if a != "--fast"]
    if "--check" in rest:
        rest = [a for a in rest if a != "--check"]
        return _check_or_build(rest, command="build", json_mode=json_mode,
                               error_format_json=error_format_json,
                               fast=fast)
    if fast:
        # D27: --fast + emission is refused — emission needs
        # checked types.
        raise _UsageError(
            "`newr build --fast` cannot emit — emission needs the "
            "full type check. Use `newr build --fast --check` to "
            "verify only, or drop `--fast` to build.")
    if target is None:
        # No explicit --target: the default comes from newr.json
        # (SPEC.md section 11), searched upward from the program's
        # directory; without a manifest it stays py, as before.
        positional = [a for a in rest if not a.startswith("-")]
        if len(positional) != 1:
            raise _UsageError(
                "`newr build` needs exactly one .newr file "
                f"(got {len(positional)})")
        try:
            target = default_target_for(Path(positional[0]))
        except ManifestError as exc:
            raise _UsageError(str(exc)) from exc
    return _build_emit(rest, output=output, line_comments=line_comments,
                       target=target, json_mode=json_mode,
                       error_format_json=error_format_json)


def _build_emit(args: list[str], *, output: str | None,
                line_comments: bool, target: str = "py",
                json_mode: bool,
                error_format_json: bool) -> int:
    """`newr build <file>` — verify, then emit Python (M2),
    JavaScript (M3), Java (M4), C# (M4), Rust (M5), Kotlin (M5) or
    Dart (M5)."""
    unknown = [a for a in args if a.startswith("-")]
    if unknown:
        raise _UsageError("unknown flag(s) for `newr build`: "
                          + ", ".join(unknown))
    positional = [a for a in args if not a.startswith("-")]
    if len(positional) != 1:
        raise _UsageError(
            f"`newr build` needs exactly one .newr file "
            f"(got {len(positional)})")
    path = Path(positional[0])
    try:
        program, diagnostics, source = _run_pipeline(path, fast=False)
    except NewrError as exc:
        diagnostics = [exc.diagnostic]
        try:
            source = _read_source(path)
        except _EnvError:
            source = ""
        program = None
    errors = [d for d in diagnostics if d.is_error()]
    get_source = _source_cache(path, source)
    if errors or program is None:
        # Same reporting as check (exit 4, no files written).
        return _finish("build", path, program, diagnostics, source,
                       json_mode=json_mode,
                       error_format_json=error_format_json)
    # Emit. An emitter error is a clean exit-4 diagnostic, never a
    # traceback (the program checked clean, so this is an emitter
    # limitation, not a user error — say so).
    if target == "js":
        from .emitter_js import EmitterError, emit_program
        suffix = ".js"
        rt_name = "newr_rt.js"
        rt_copy_name = rt_name
    elif target == "java":
        from .emitter_java import EmitterError, emit_program
        suffix = ".java"
        rt_name = "newr_rt.java"
        # javac requires the file name to match the public class, so
        # the runtime is copied as NewrRt.java (K1).
        rt_copy_name = "NewrRt.java"
    elif target == "cs":
        from .emitter_cs import EmitterError, emit_program
        suffix = ".cs"
        rt_name = "newr_rt.cs"
        rt_copy_name = rt_name
    elif target == "rs":
        from .emitter_rs import EmitterError, emit_program
        suffix = ".rs"
        rt_name = "newr_rt.rs"
        rt_copy_name = rt_name
    elif target == "kt":
        from .emitter_kt import EmitterError, emit_program
        suffix = ".kt"
        rt_name = "newr_rt.kt"
        rt_copy_name = rt_name
    elif target == "dart":
        from .emitter_dart import EmitterError, emit_program
        suffix = ".dart"
        rt_name = "newr_rt.dart"
        rt_copy_name = rt_name
    else:
        from .emitter import EmitterError, emit_program
        suffix = ".py"
        rt_name = "newr_rt.py"
        rt_copy_name = rt_name
    try:
        sources = {f: get_source(f) for f in program.files}
        if target in ("java", "cs", "rs", "kt", "dart"):
            # The output stem is the public class name (Java) / the
            # project name (C#) / the binary name (Rust) / the main
            # file stem (Kotlin, Dart); it comes from -o when given.
            output_stem = Path(output).stem if output else None
            result = emit_program(program, main_path=str(path),
                                  sources=sources,
                                  line_comments=line_comments,
                                  output_stem=output_stem)
        else:
            result = emit_program(program, main_path=str(path),
                                  sources=sources,
                                  line_comments=line_comments)
    except EmitterError as exc:
        # §20: the emitter attaches the span of the expression it
        # could not emit when it knows it (its `_fail` calls pass
        # one); the CLI renders that span instead of the file start,
        # so the diagnostic points at the use site. A spanless
        # refusal keeps the old 1:1 fallback. The "cannot emit this
        # program:" prefix is added here, and only here — the
        # emitter messages no longer carry it, so it cannot double.
        span = getattr(exc, "span", None) or Span(str(path), 0, 1)
        diag = Diagnostic(span=span, code="E4001",
                          message=f"cannot emit this program: {exc}",
                          help="the emitter does not support this "
                               "construct yet — see BUILD_REPORT.md §14")
        return _finish("build", path, program, [diag], source,
                       json_mode=json_mode,
                       error_format_json=error_format_json)
    out_dir = Path(output).parent if output else path.parent
    out_dir.mkdir(parents=True, exist_ok=True)
    main_out = (Path(output) if output
                else path.with_suffix(suffix))
    if target in ("java", "rs", "kt", "dart") and output:
        # The Java main file MUST be named after its public class
        # (K1) — the -o path controls the directory, but the file
        # name comes from the emitter (the sanitized stem). Same for
        # Rust/Kotlin/Dart: the main file is stem-named, so -o only
        # picks the directory.
        main_out = main_out.parent / result.main_file
    for name, text in result.files.items():
        if output and name == result.main_file:
            dest = main_out
        else:
            dest = main_out.parent / name
        dest.write_text(text, encoding="utf-8")
    if result.uses_rt:
        # Copy compiler/newr_rt.py (or newr_rt.js / newr_rt.java /
        # newr_rt.cs) next to the output (D4) — read via the package
        # so the zipapp build keeps working.
        import importlib.resources as _res
        try:
            rt_text = (_res.files("compiler") / rt_name
                       ).read_text(encoding="utf-8")
        except Exception:
            rt_text = (Path(__file__).parent / rt_name
                       ).read_text(encoding="utf-8")
        (main_out.parent / rt_copy_name).write_text(
            rt_text, encoding="utf-8")
    if json_mode:
        data = {"file": str(path),
                "files": sorted(str(main_out.parent / n)
                                for n in result.files),
                "diagnostics": [d.to_json(get_source(d.span.file))
                                for d in diagnostics]}
        print(json.dumps(_envelope("build", data, diagnostics)))
        return 0
    warnings = [d for d in diagnostics if not d.is_error()]
    # Report the ACTUAL destination paths (the -o path for the main
    # file, main_out.parent / name for imports), not the source stems.
    dests = []
    for name in sorted(result.files):
        if output and name == result.main_file:
            dests.append(str(main_out))
        else:
            dests.append(str(main_out.parent / name))
    if result.uses_rt:
        dests.append(str(main_out.parent / rt_copy_name))
    names = ", ".join(dests)
    if warnings:
        print(f"{path}: ok, {len(warnings)} warning(s) — "
              f"wrote {names}")
    else:
        print(f"{path}: ok — wrote {names}")
    return 0


def _cmd_help(args: list[str]) -> int:
    if not args:
        print(USAGE)
        return 0
    topic = args[0]
    text = COMMAND_HELP.get(topic)
    if text is None:
        raise _UsageError(f"unknown command `{topic}`. Run `newr help`.")
    print(text)
    return 0


def _cmd_version() -> int:
    print(f"newr {VERSION}")
    return 0


def _cmd_explain(code: str) -> int:
    text = EXPLAIN.get(code)
    if text is None:
        raise _UsageError(
            f"unknown diagnostic code `{code}`. Run `newr --explain E2001`.")
    print(text)
    return 0


# ---------------------------------------------------------------- entry point


def main(argv: list[str] | None = None) -> int:
    """Run the CLI; return the process exit code."""
    argv = list(sys.argv[1:] if argv is None else argv)
    json_mode = "--json" in argv
    argv = [a for a in argv if a != "--json"]
    try:
        error_format_json = False
        rest: list[str] = []
        i = 0
        while i < len(argv):
            arg = argv[i]
            if arg == "--error-format=json":
                error_format_json = True
                i += 1
            elif arg == "--error-format":
                if i + 1 >= len(argv):
                    raise _UsageError(
                        "`--error-format` needs a value, e.g. "
                        "`--error-format=json`")
                if argv[i + 1] != "json":
                    raise _UsageError(
                        "--error-format only supports `json` in M1")
                error_format_json = True
                i += 2
            else:
                rest.append(arg)
                i += 1
        return _dispatch(rest, json_mode=json_mode,
                         error_format_json=error_format_json)
    except _UsageError as exc:
        print(f"newr: {exc}", file=sys.stderr)
        if json_mode:
            print(json.dumps(_envelope("usage", None, [])))
        return 1
    except _EnvError as exc:
        print(f"newr: {exc}", file=sys.stderr)
        if json_mode:
            print(json.dumps(_envelope("environment", None, [])))
        return 2
    except RecursionError:
        # M-01 backstop for any path outside _check_or_build: a clean
        # E1001 diagnostic, exit 4 — never a raw traceback.
        diag = Diagnostic(span=Span("newr", 0, 1), code="E1001",
                          message="too deeply nested",
                          help="split the expression or block into "
                               "smaller pieces — newr limits nesting to "
                               "200 levels so a bad input cannot exhaust "
                               "the stack")
        print(f"newr: {diag.message}", file=sys.stderr)
        if json_mode:
            print(json.dumps(_envelope("error", None, [diag])))
        elif error_format_json:
            print(json.dumps([diag.to_json("")]))
        return 4


def _dispatch(argv: list[str], *, json_mode: bool,
              error_format_json: bool) -> int:
    # --explain is a global flag (also available as a subcommand).
    if argv and argv[0] == "--explain":
        if len(argv) < 2:
            raise _UsageError("`newr --explain` needs a code, e.g. E2001")
        return _cmd_explain(argv[1])
    if not argv or argv[0] in ("-h", "--help"):
        print(USAGE)
        return 0
    cmd, args = argv[0], argv[1:]
    if cmd in ("-v", "--version"):
        if json_mode:
            print(json.dumps(_envelope(
                "version", {"version": VERSION}, [])))
            return 0
        return _cmd_version()
    if cmd == "help":
        return _cmd_help(args)
    if cmd == "version":
        if json_mode:
            print(json.dumps(_envelope(
                "version", {"version": VERSION}, [])))
            return 0
        return _cmd_version()
    if cmd == "explain":
        if len(args) < 1:
            raise _UsageError("`newr explain` needs a code, e.g. E2001")
        return _cmd_explain(args[0])
    if cmd == "check":
        return _cmd_check(args, json_mode=json_mode,
                          error_format_json=error_format_json)
    if cmd == "build":
        return _cmd_build(args, json_mode=json_mode,
                          error_format_json=error_format_json)
    if cmd == "fmt":
        return _cmd_fmt(args, json_mode=json_mode,
                        error_format_json=error_format_json)
    if cmd == "fix":
        return _cmd_fix(args, json_mode=json_mode,
                        error_format_json=error_format_json)
    if cmd == "init":
        return _cmd_init(args, json_mode=json_mode,
                         error_format_json=error_format_json)
    if cmd == "test":
        return _cmd_test(args, json_mode=json_mode,
                         error_format_json=error_format_json)
    if cmd == "docs":
        return _cmd_docs(args, json_mode=json_mode,
                         error_format_json=error_format_json)
    raise _UsageError(f"unknown command `{cmd}`. Run `newr help`.")
