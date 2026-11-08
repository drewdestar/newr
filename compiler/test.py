"""`newr test`: run tests through one target's emitter
(SPEC.md section 19, section 24.2 idea 13, M6 Phase 2c).

Two test forms share one runner (the user's call):

1. Test functions: plain functions whose name starts with
   ``test_``, discovered by the prefix. No new syntax.
2. Test blocks: ``test "display name":`` plus an indented body
   (SPEC.md section 8.7). The name is a plain string, so it may
   contain spaces. The parser lowers a block to a synthetic
   function named ``_newr_test_<n>`` with the same body; the
   runner reports the display name and calls the synthetic name.

A test asserts with the machinery the language already has:

    fn test_add():
      if add(1, 2) != 3:
        throw "add(1, 2) should be 3"

    test "adds two numbers":
      requires add(2, 2) == 4

Discovery: the main file's test functions and test blocks, in
source order across both forms, so a file that mixes them runs
them in the order they are written. Tests in imported files
are not discovered (the file passed to `newr test` owns its
tests). A test function takes no parameters: the harness calls
it with none, so a `test_` function with parameters is a build
error naming the function. Test names must be unique within a
file (two blocks, or a block and a `test_` function, sharing one
name is a build error, E3003): the runner reports tests by
name, so one name cannot mean two tests.

How a run works: the file is parsed, resolved, and checked exactly
like `newr build`. The runner then appends a small harness to the
program's top-level statements, one try/catch per test:

    print "NEWR_TEST_BEGIN test_add"
    try:
      test_add()
      print "NEWR_TEST_PASS test_add"
    catch err:
      print "NEWR_TEST_FAIL test_add"
      print err.message

A failing test is caught, reported, and the run continues with the
next test, so one failure never hides the rest. The program is
emitted to a temporary directory (never next to the source) and run
with the target's usual command; the runner reads the markers back
out of stdout. The program runs with its working directory set to
the test file's directory, so relative `file.read` paths in a test
behave the way they do for the program itself.

Output lines that start with ``NEWR_TEST_`` are the runner's own
markers; a test that prints such a line will confuse the report,
so treat the prefix as reserved.

Exit codes (SPEC.md section 19): 0 when every test passes (or a
file has no tests, or a bot program is skipped, see below), 4 when
the program has a build error or any test fails, 2 when a file
cannot be read or the target's toolchain is missing, 1 on a usage
error. A test failure exits 4, the same code this repo already uses
for "the thing you asked to verify is not in the desired state"
(`newr check` on a program with errors, `newr fmt --check` on a
file that needs formatting).

Two honest limits, by design:

- Bot programs (a `bot` block, or any `command` / `event` /
  `task`) are skipped, not run: the emitted program needs a live
  Discord token to start, and a test run must not log a bot in.
  The skip is printed, it is not a silent pass.
- A file with no tests prints "no tests" and passes
  (exit 0), the same rule cargo test uses: an empty suite is not a
  failure.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from . import ast_nodes as A
from .checker import check_program
from .discord_check import check_discord, discord_providers
from .errors import Diagnostic, NewrError, Span
from .init import VALID_TARGETS
from .resolver import resolve_source

# The test runner's own markers, printed by the synthesized harness.
BEGIN_PREFIX = "NEWR_TEST_BEGIN "
PASS_PREFIX = "NEWR_TEST_PASS "
FAIL_PREFIX = "NEWR_TEST_FAIL "

# One run (compile + execute) of one test file gets this long before
# the runner kills it and reports the running test as failed.
TEST_TIMEOUT_SECONDS = 120

TEST_PREFIX = "test_"

_SUFFIX = {"py": ".py", "js": ".js", "java": ".java", "cs": ".cs",
           "rs": ".rs", "kt": ".kt", "dart": ".dart"}
_RT_SRC = {"py": "newr_rt.py", "js": "newr_rt.js",
           "java": "newr_rt.java", "cs": "newr_rt.cs",
           "rs": "newr_rt.rs", "kt": "newr_rt.kt",
           "dart": "newr_rt.dart"}
_RT_COPY = {"py": "newr_rt.py", "js": "newr_rt.js",
            "java": "NewrRt.java", "cs": "newr_rt.cs",
            "rs": "newr_rt.rs", "kt": "newr_rt.kt",
            "dart": "newr_rt.dart"}


class TestUsageError(Exception):
    """Exit 1: bad flags, paths, or target for `newr test`."""


class TestEnvError(Exception):
    """Exit 2: unreadable file, or a missing toolchain."""


@dataclass
class TestCase:
    """One discovered test: its name and its outcome."""

    name: str
    status: str = "not_run"  # pass | fail | not_run | skipped
    message: str = ""


@dataclass
class DiscoveredTest:
    """One discovered test: its display name, the callable the
    harness invokes, and which form it is. For a test function
    the display name and the callable are the same (`test_add`);
    for a test block the display name is the block's string
    ("adds two numbers") and the callable is its synthetic
    function (`_newr_test_0`)."""

    name: str
    call_name: str
    kind: str  # "fn" | "block"
    span: Span
    fn: A.FnDecl | None = None


# ---------------------------------------------------------------- discovery


def discover_tests(program: A.Program) -> list[DiscoveredTest]:
    """The tests of a program: main-file `test_` functions and
    `test` blocks, in source order across both forms. Imported
    modules are not searched."""
    found: list[DiscoveredTest] = []
    for fn in program.functions:
        if fn.name.startswith(TEST_PREFIX):
            found.append(DiscoveredTest(
                name=fn.name, call_name=fn.name, kind="fn",
                span=fn.span, fn=fn))
    for tb in program.test_blocks:
        found.append(DiscoveredTest(
            name=tb.name, call_name=tb.func_name, kind="block",
            span=tb.span))
    found.sort(key=lambda t: t.span.start_byte)
    return found


def validate_tests(tests: list[DiscoveredTest]) -> list[str]:
    """Errors that keep a test from running: a test function takes
    no parameters, because the harness calls it with none. Test
    blocks have no parameters by construction, so there is
    nothing to validate for them."""
    errors = []
    for test in tests:
        if test.kind != "fn" or test.fn is None:
            continue
        if test.fn.params:
            errors.append(
                f"test function `{test.name}` takes "
                f"{len(test.fn.params)} parameter(s); test functions "
                f"take none")
    return errors


@dataclass
class FileResult:
    """The outcome of running one .newr file through one target."""

    file: str
    target: str
    tests: list[TestCase] = field(default_factory=list)
    # Build error text (the program failed to check or emit), or "".
    error: str = ""
    # Skip reason (a bot program, or a file with no tests), or "".
    skipped: str = ""
    # stderr tail shown when the run itself failed before any test.
    run_error: str = ""
    # Build-error diagnostics in --error-format=json form.
    diagnostics_json: list[dict] = field(default_factory=list)

    @property
    def passed(self) -> int:
        return sum(1 for t in self.tests if t.status == "pass")

    @property
    def failed(self) -> int:
        return sum(1 for t in self.tests if t.status == "fail")

    @property
    def not_run(self) -> int:
        return sum(1 for t in self.tests if t.status == "not_run")

    @property
    def ok(self) -> bool:
        return not self.error and self.failed == 0 and self.not_run == 0


# ---------------------------------------------------------------- discovery

# (discover_tests and validate_tests live above, next to
# DiscoveredTest, so the discovery contract sits with its type.)

def is_bot_program(program: A.Program) -> bool:
    """True when the program needs Discord to run (a bot block, or
    any command / event / task). Such a program is skipped, never
    executed by the test runner."""
    return (program.bot is not None or bool(program.commands)
            or bool(program.events) or bool(program.tasks))


# ---------------------------------------------------------------- harness


def _print_stmt(text: str, span: Span) -> A.ExprStmt:
    return A.ExprStmt(
        span=span,
        value=A.Call(span=span, func=A.Name(span=span, name="print"),
                     args=[A.Lit(span=span, value=text)]))


def _call_stmt(name: str, span: Span) -> A.ExprStmt:
    return A.ExprStmt(
        span=span,
        value=A.Call(span=span, func=A.Name(span=span, name=name),
                     args=[]))


def synthesize_harness(program: A.Program,
                       tests: list[DiscoveredTest]) -> None:
    """Append the test harness to the program's top-level statements,
    in place, one try/catch per test (see the module docstring).
    The program's own statements run first; the harness runs after
    them, in discovery order. The markers carry the display name;
    the call invokes the test's callable (the function itself, or
    a test block's synthetic function)."""
    for test in tests:
        span = test.span
        program.stmts.append(_print_stmt(BEGIN_PREFIX + test.name, span))
        program.stmts.append(A.Try(
            span=span,
            body=[_call_stmt(test.call_name, span),
                  _print_stmt(PASS_PREFIX + test.name, span)],
            catches=[("err", span,
                      [_print_stmt(FAIL_PREFIX + test.name, span),
                       _print_stmt_err(span)])],
            finally_body=None))


def _print_stmt_err(span: Span) -> A.ExprStmt:
    """`print err.message`: the caught error's message, one line per
    the runtime (a message with newlines prints as several lines;
    the parser below collects them all)."""
    return A.ExprStmt(
        span=span,
        value=A.Call(
            span=span, func=A.Name(span=span, name="print"),
            args=[A.Member(span=span,
                           obj=A.Name(span=span, name="err"),
                           name="message")]))


# ---------------------------------------------------------------- pipeline


def _read_source(path: Path) -> str:
    """Read a source file as UTF-8 (same rule as the CLI: a
    non-UTF-8 file is an environment error, not a traceback)."""
    try:
        with open(path, encoding="utf-8", newline="") as fh:
            return fh.read()
    except UnicodeDecodeError as exc:
        raise TestEnvError(
            f"cannot read {path}: not valid UTF-8 "
            f"(SPEC.md section 3.1 requires UTF-8; {exc})") from exc
    except OSError as exc:
        raise TestEnvError(f"cannot read {path}: {exc}") from exc


def _resolve_program(path: Path) -> tuple[A.Program, list[Diagnostic],
                                          str]:
    """Parse + resolve one file (no type check yet). Parse errors
    raise NewrError; the caller turns them into a FileResult with
    error text (exit 4), never a traceback."""
    source = _read_source(path)
    resolved = resolve_source(source, str(path))
    return resolved.program, list(resolved.diagnostics), source


def _check_full(program: A.Program) -> list[Diagnostic]:
    """The full check of a program (core + discord), like
    `newr build`. Runs after the harness is synthesized, so the
    checker's inferred types cover the harness nodes too: the
    emitters read those types, and a harness node with no type
    cannot be emitted."""
    providers = discord_providers(program)
    core = check_program(program, providers=providers)
    return list(core.diagnostics) + check_discord(program)


def _diag_text(diag: Diagnostic, source: str) -> str:
    return diag.format(source).strip()


# ---------------------------------------------------------------- emit


def _emit_to_dir(program: A.Program, main_path: Path, target: str,
                 out_dir: Path) -> str:
    """Emit the program for one target into out_dir (created when
    missing). Returns the main output's file name. The runtime is
    copied next to the output when the program uses it. Mirrors the
    CLI's `_build_emit` dispatch; an emitter refusal is a build
    error naming the construct."""
    if target == "js":
        from .emitter_js import EmitterError, emit_program
    elif target == "java":
        from .emitter_java import EmitterError, emit_program
    elif target == "cs":
        from .emitter_cs import EmitterError, emit_program
    elif target == "rs":
        from .emitter_rs import EmitterError, emit_program
    elif target == "kt":
        from .emitter_kt import EmitterError, emit_program
    elif target == "dart":
        from .emitter_dart import EmitterError, emit_program
    else:
        from .emitter import EmitterError, emit_program
    sources = {}
    for name in program.files:
        try:
            with open(name, encoding="utf-8", newline="") as fh:
                sources[name] = fh.read()
        except (OSError, UnicodeDecodeError):
            sources[name] = ""
    try:
        if target in ("java", "cs", "rs", "kt", "dart"):
            result = emit_program(program, main_path=str(main_path),
                                  sources=sources, line_comments=False,
                                  output_stem=None)
        else:
            result = emit_program(program, main_path=str(main_path),
                                  sources=sources, line_comments=False)
    except EmitterError as exc:
        span = getattr(exc, "span", None) or Span(str(main_path), 0, 1)
        raise NewrError(Diagnostic(
            span=span, code="E4001",
            message=f"cannot emit this program: {exc}",
            help="the emitter does not support this construct yet "
                 "- see BUILD_REPORT.md section 14")) from exc
    out_dir.mkdir(parents=True, exist_ok=True)
    for name, text in result.files.items():
        (out_dir / name).write_text(text, encoding="utf-8")
    if result.uses_rt:
        rt_name = _RT_SRC[target]
        rt_copy = _RT_COPY[target]
        try:
            import importlib.resources as _res
            rt_text = (_res.files("compiler") / rt_name
                       ).read_text(encoding="utf-8")
        except Exception:
            rt_text = (Path(__file__).parent / rt_name
                       ).read_text(encoding="utf-8")
        (out_dir / rt_copy).write_text(rt_text, encoding="utf-8")
    return result.main_file


# ---------------------------------------------------------------- run


def _which(name: str, env_var: str, home_sub: str) -> str | None:
    """Find a toolchain binary: the env var's directory first, then
    ~/home_sub, then the PATH (same rule as conformance/run.py)."""
    if os.environ.get(env_var):
        found = shutil.which(name, path=str(Path(os.environ[env_var])))
        if found:
            return found
    found = shutil.which(name, path=str(Path.home() / home_sub))
    if found:
        return found
    return shutil.which(name)


def _tool_or_error(name: str, env_var: str, home_sub: str,
                   what: str) -> str:
    found = _which(name, env_var, home_sub)
    if found is None:
        raise TestEnvError(
            f"cannot run target: no {what} found "
            f"({env_var} / ~/{home_sub} / PATH)")
    return found


def _run_command(target: str, out_dir: Path, main_file: str,
                 stem: str) -> tuple[list[str], str | None, dict]:
    """The command that runs the emitted program, plus the directory
    it must run in (or None for anywhere) and extra env vars.
    Mirrors conformance/run.py's per-target commands."""
    main = out_dir / main_file
    if target == "py":
        return [sys.executable, str(main)], None, {}
    if target == "js":
        node = _tool_or_error("node", "NODE_HOME", "", "node")
        return [node, str(main)], None, {}
    if target == "java":
        javac = _tool_or_error("javac", "JAVA_HOME",
                               "jdk-21/bin", "javac (JDK)")
        java = _tool_or_error("java", "JAVA_HOME",
                              "jdk-21/bin", "java (JDK)")
        _compile(out_dir, [javac, *[str(p) for p in
                                     out_dir.glob("*.java")]])
        return [java, "-cp", str(out_dir), stem], None, {}
    if target == "cs":
        dotnet = _tool_or_error("dotnet", "DOTNET_ROOT",
                                ".dotnet", "dotnet")
        csproj = next(out_dir.glob("*.csproj"), None)
        if csproj is None:
            raise TestEnvError("cannot run target cs: no .csproj "
                               "was emitted")
        env = {"DOTNET_CLI_TELEMETRY_OPTOUT": "1"}
        return [dotnet, "run", "--project", str(csproj)], None, env
    if target == "rs":
        if (out_dir / "Cargo.toml").exists():
            cargo = _tool_or_error("cargo", "CARGO_HOME",
                                   ".cargo/bin", "cargo")
            return [cargo, "run", "--quiet"], str(out_dir), {}
        rustc = _tool_or_error("rustc", "CARGO_HOME",
                               ".cargo/bin", "rustc")
        binary = out_dir / stem
        _compile(out_dir, [rustc, str(main), "-o", str(binary)])
        return [str(binary)], None, {}
    if target == "kt":
        kotlinc = _tool_or_error("kotlinc", "KOTLIN_HOME",
                                 "kotlin/kotlinc/bin", "kotlinc")
        java = _tool_or_error("java", "JAVA_HOME",
                              "jdk-21/bin", "java (JDK)")
        jar = out_dir / "app.jar"
        _compile(out_dir, [kotlinc, *[str(p) for p in
                                       out_dir.glob("*.kt")],
                           "-include-runtime", "-d", str(jar)])
        return [java, "-jar", str(jar)], None, {}
    # dart
    dart = _tool_or_error("dart", "DART_HOME",
                          "dart-sdk/dart-sdk/bin", "dart")
    if (out_dir / "pubspec.yaml").exists():
        _run([dart, "pub", "get"], cwd=str(out_dir),
             timeout=TEST_TIMEOUT_SECONDS)
        return [dart, "run", str(main)], None, {}
    return [dart, str(main)], None, {}


def _run(argv: list[str], *, cwd: str | None,
         timeout: int, env: dict | None = None) -> subprocess.CompletedProcess:
    run_env = dict(os.environ)
    if env:
        run_env.update(env)
    return subprocess.run(argv, capture_output=True, text=True,
                          cwd=cwd, timeout=timeout, env=run_env)


def _compile(out_dir: Path, argv: list[str]) -> None:
    """A compile step (javac / rustc / kotlinc / dart pub get):
    a failure is a build error, reported with the compiler output."""
    try:
        result = _run(argv, cwd=str(out_dir),
                      timeout=TEST_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        raise TestEnvError(
            f"timed out after {TEST_TIMEOUT_SECONDS}s running "
            f"{' '.join(argv[:1])} (compile step)") from None
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise NewrError(Diagnostic(
            span=Span("", 0, 1), code="E4001",
            message=f"cannot run this program: "
                    f"{' '.join(argv[:1])} failed "
                    f"(exit {result.returncode})",
            help=detail[-500:] if detail else ""))


# ---------------------------------------------------------------- markers


def parse_markers(stdout: str, tests: list[DiscoveredTest],
                  failure_detail: str = "") -> list[TestCase]:
    """Read the harness markers back out of stdout. A test is pass
    when its PASS marker appears, fail when its FAIL marker appears
    (the lines after the marker, up to the next marker, are the
    thrown message), fail when its BEGIN appears with no PASS or
    FAIL (the program died inside it; the failure detail, usually
    the stderr tail, says why), and not_run when its BEGIN never
    appears. Markers are keyed by display name: a test block's
    name may contain spaces, and the marker is the whole rest of
    the line."""
    cases: dict[str, TestCase] = {
        test.name: TestCase(name=test.name) for test in tests}
    order = [test.name for test in tests]
    # (kind, name, following lines) per marker, in stdout order.
    events: list[tuple[str, str, list[str]]] = []
    current: tuple[str, str, list[str]] | None = None
    for line in stdout.splitlines():
        kind = None
        name = ""
        for prefix, marker in ((BEGIN_PREFIX, "begin"),
                               (PASS_PREFIX, "pass"),
                               (FAIL_PREFIX, "fail")):
            if line.startswith(prefix):
                kind = marker
                name = line[len(prefix):].strip()
                break
        if kind is not None:
            if current is not None:
                events.append(current)
            current = (kind, name, [])
        elif current is not None:
            current[2].append(line)
    if current is not None:
        events.append(current)
    seen_begin: set[str] = set()
    for kind, name, following in events:
        if name not in cases:
            continue
        case = cases[name]
        if kind == "begin":
            seen_begin.add(name)
        elif kind == "pass":
            case.status = "pass"
        elif kind == "fail":
            case.status = "fail"
            case.message = "\n".join(following).strip()
    for name in order:
        case = cases[name]
        if case.status == "pass" or case.status == "fail":
            continue
        if name in seen_begin:
            case.status = "fail"
            case.message = (
                "the test did not finish: the program exited or "
                "crashed during it"
                + (f":\n{failure_detail}" if failure_detail else ""))
    return [cases[name] for name in order]


def _stderr_tail(stderr: str, lines: int = 10) -> str:
    kept = [ln for ln in stderr.splitlines() if ln.strip()]
    return "\n".join(kept[-lines:]) if kept else ""


# ---------------------------------------------------------------- one file


def run_test_file(path: Path, target: str, *,
                  timeout: int = TEST_TIMEOUT_SECONDS) -> FileResult:
    """Run one .newr file's tests through one target. Every failure
    mode is a value in the result, never an exception, except
    unreadable files and missing toolchains (TestEnvError, exit 2)."""
    result = FileResult(file=str(path), target=target)
    try:
        program, diagnostics, source = _resolve_program(path)
    except NewrError as exc:
        try:
            source = _read_source(path)
        except TestEnvError:
            source = ""
        result.error = _diag_text(exc.diagnostic, source)
        result.diagnostics_json = [exc.diagnostic.to_json(source)]
        return result
    errors = [d for d in diagnostics if d.is_error()]
    if errors:
        result.error = "\n".join(
            _diag_text(d, source) for d in errors)
        result.diagnostics_json = [d.to_json(source) for d in errors]
        return result
    tests = discover_tests(program)
    param_errors = validate_tests(tests)
    if param_errors:
        result.error = "; ".join(param_errors)
        return result
    if tests:
        # The harness is checked together with the program, so the
        # checker fills in the harness nodes' types (the emitters
        # need them) and any harness mistake is a build error.
        synthesize_harness(program, tests)
    diagnostics = _check_full(program)
    errors = [d for d in diagnostics if d.is_error()]
    if errors:
        result.error = "\n".join(
            _diag_text(d, source) for d in errors)
        result.diagnostics_json = [d.to_json(source) for d in errors]
        return result
    if not tests:
        result.skipped = "no tests"
        return result
    if is_bot_program(program):
        result.skipped = (
            "bot program: tests need a live Discord token to run, "
            "so the program is not executed")
        result.tests = [TestCase(name=test.name, status="skipped")
                        for test in tests]
        return result
    with tempfile.TemporaryDirectory(prefix="newr-test-") as tmp:
        out_dir = Path(tmp)
        try:
            main_file = _emit_to_dir(program, path, target, out_dir)
        except NewrError as exc:
            result.error = _diag_text(exc.diagnostic, source)
            return result
        try:
            argv, run_cwd, extra_env = _run_command(
                target, out_dir, main_file, path.stem)
        except TestEnvError:
            raise
        except NewrError as exc:
            result.error = _diag_text(exc.diagnostic, source)
            return result
        try:
            run = _run(argv, cwd=run_cwd or str(path.parent),
                        timeout=timeout, env=extra_env)
        except subprocess.TimeoutExpired:
            cases = parse_markers("", tests)
            for case in cases:
                if case.status == "not_run":
                    case.status = "fail"
                    case.message = (
                        f"timed out after {timeout}s")
            result.tests = cases
            return result
        cases = parse_markers(run.stdout, tests,
                              failure_detail=_stderr_tail(run.stderr))
        result.tests = cases
        if run.returncode != 0:
            if all(c.status == "pass" for c in cases):
                # Everything passed but the program still exited
                # non-zero (its own top-level code failed after
                # the harness): a run error, not a test failure.
                result.run_error = (
                    f"the program exited with code "
                    f"{run.returncode} after all tests passed"
                    + (f":\n{_stderr_tail(run.stderr)}"
                       if run.stderr.strip() else ""))
            elif not any(c.status in ("fail", "pass")
                         for c in cases):
                # Nothing ran at all: the program died before the
                # harness (its own top-level code threw). The tests
                # stay not_run; the run error drives exit 4.
                result.run_error = (
                    f"the program failed before running any test "
                    f"(exit {run.returncode})"
                    + (f":\n{_stderr_tail(run.stderr)}"
                       if run.stderr.strip() else ""))
            # Otherwise some test failed (or passed) and the rest
            # never ran, because the process died during the
            # failing test. The failed one already carries the
            # message; the rest stay not_run, which is honest:
            # they never ran, they did not fail.
        else:
            for case in cases:
                if case.status == "not_run":
                    # Exit 0 but the harness never reached this
                    # test: the program's own top-level code exited
                    # first (a bare top-level return, for example).
                    case.status = "fail"
                    case.message = (
                        "the test harness did not reach this test: "
                        "the program's top-level code exited first")
        return result


# ---------------------------------------------------------------- paths


def collect_paths(args: list[str]) -> list[Path]:
    """`newr test` with no paths tests every .newr file in the
    current directory, in name order. A file argument is used as
    given; a directory argument contributes every .newr file under
    it, recursively, in name order."""
    if not args:
        return sorted(Path(".").glob("*.newr"))
    found: list[Path] = []
    for name in args:
        path = Path(name)
        if not path.exists():
            raise TestEnvError(f"no such file or directory: {path}")
        if path.is_dir():
            found.extend(sorted(path.rglob("*.newr")))
        else:
            found.append(path)
    return found


def run_tests(paths: list[Path], target: str, *,
              timeout: int = TEST_TIMEOUT_SECONDS) -> list[FileResult]:
    """Run every file's tests through one target, in path order.
    A file that fails to build still lets the rest run, so one
    report covers the whole run."""
    return [run_test_file(path, target, timeout=timeout)
            for path in paths]


# ---------------------------------------------------------------- reporting


def format_human(results: list[FileResult]) -> str:
    """The human-readable report, one line per test plus a summary
    per file. A failing test prints its thrown message, indented."""
    lines: list[str] = []
    for result in results:
        if result.error:
            lines.append(f"{result.file}: build error")
            for line in result.error.splitlines():
                lines.append(f"  {line}")
            continue
        if result.skipped and not result.tests:
            lines.append(f"{result.file}: {result.skipped}")
            continue
        if result.skipped:
            lines.append(f"{result.file}: {result.skipped}")
        for case in result.tests:
            mark = {"pass": "ok", "fail": "FAIL",
                    "not_run": "not run",
                    "skipped": "skipped"}.get(case.status, case.status)
            lines.append(f"{result.file}:{case.name} ... {mark}")
            if case.status == "fail" and case.message:
                for line in case.message.splitlines():
                    lines.append(f"  {line}")
        if result.run_error:
            lines.append(f"{result.file}: {result.run_error}")
        total = len(result.tests)
        lines.append(
            f"{result.file}: {result.passed} passed, "
            f"{result.failed} failed, {result.not_run} not run"
            + (f" ({total} tests)" if total else ""))
    passed = sum(r.passed for r in results)
    failed = sum(r.failed for r in results)
    not_run = sum(r.not_run for r in results)
    build_errors = sum(1 for r in results if r.error)
    lines.append(
        f"total: {passed} passed, {failed} failed, "
        f"{not_run} not run, {build_errors} build error(s)")
    return "\n".join(lines)


def to_json_data(results: list[FileResult]) -> dict:
    """The --json data object: per-file results plus totals."""
    files = []
    for result in results:
        files.append({
            "file": result.file,
            "target": result.target,
            "tests": [{"name": t.name, "status": t.status,
                       "message": t.message} for t in result.tests],
            "passed": result.passed,
            "failed": result.failed,
            "not_run": result.not_run,
            "error": result.error or None,
            "skipped": result.skipped or None,
            "run_error": result.run_error or None,
        })
    return {
        "files": files,
        "passed": sum(r.passed for r in results),
        "failed": sum(r.failed for r in results),
        "not_run": sum(r.not_run for r in results),
        "build_errors": sum(1 for r in results if r.error),
    }


def exit_code(results: list[FileResult]) -> int:
    """0 when every test passes (empty suites and skipped bot
    programs pass); 4 when any build error, test failure, or
    un-run test remains."""
    if any(r.error for r in results):
        return 4
    if any(r.failed or r.not_run for r in results):
        return 4
    if any(r.run_error for r in results):
        return 4
    return 0
