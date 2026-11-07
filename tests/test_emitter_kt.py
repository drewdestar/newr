"""Tests for the Kotlin emitter (compiler/emitter_kt.py, M5).

TDD RED first: written before the emitter existed. The
`emit_kt_src` helper runs the full front-end pipeline (resolve +
core check + discord check) and emits Kotlin; `run_kt_src` also
compiles and executes the emitted program with kotlinc/java and
returns its stdout.

Every test pins a decision from the N-list (Kotlin decisions,
the twin of BUILD_REPORT.md §21 K1–K13) or a SPEC rule the
emitter must preserve — including the Kotlin-specific traps
(kotlinc's script needs `java` on PATH, `Long` ints, Python-repr
float rendering via NewrRt.floatStr, capture-by-value via
snapshot locals, one `object` per file named after the stem).

The kotlinc subprocess inherits an env with ~/jdk-21/bin
prepended to PATH: kotlinc is a shell script that invokes
`java`, and the runner's own PATH does not carry it.
"""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from compiler.checker import check_program
from compiler.discord_check import check_discord, discord_providers
from compiler.resolver import resolve_file, resolve_source

ROOT = Path(__file__).resolve().parent.parent
RT_KT_SRC = ROOT / "compiler" / "newr_rt.kt"

# The JDK lives in ~/jdk-21 (installed for M4); fall back to the
# PATH when it is not there (e.g. CI, where setup-java provides it).
_JAVA_HOME = Path(os.environ["JAVA_HOME"]) \
    if os.environ.get("JAVA_HOME") else (Path.home() / "jdk-21")
_JAVA_BIN = _JAVA_HOME / "bin"
JAVA = shutil.which("java", path=str(_JAVA_BIN)) or shutil.which("java")
# kotlinc lives in ~/kotlin (installed for M5); KOTLIN_HOME or PATH.
_KOTLIN_HOME = Path(os.environ["KOTLIN_HOME"]) \
    if os.environ.get("KOTLIN_HOME") else (Path.home() / "kotlin")
_KOTLINC_BIN = _KOTLIN_HOME / "kotlinc" / "bin"
KOTLINC = shutil.which("kotlinc", path=str(_KOTLINC_BIN)) \
    or shutil.which("kotlinc")


def _kt_env(env=None):
    """The subprocess env for kotlinc/java: ~/jdk-21/bin first on
    PATH so kotlinc's script finds `java`."""
    merged = dict(os.environ)
    if env:
        merged.update(env)
    path = merged.get("PATH", "")
    jdk_bin = str(_JAVA_BIN)
    if jdk_bin not in path.split(os.pathsep):
        merged["PATH"] = jdk_bin + os.pathsep + path
    return merged


try:
    from compiler.emitter_kt import EmitterError, emit_program as emit_kt
    HAS_KT_EMITTER = True
except ImportError:
    HAS_KT_EMITTER = False


def full_program(src, filename="<test>.newr"):
    """Resolve + check a source string; assert it is clean."""
    resolved = resolve_source(src, filename)
    diags = list(resolved.diagnostics)
    diags += check_program(
        resolved.program,
        providers=discord_providers(resolved.program)).diagnostics
    diags += check_discord(resolved.program)
    errs = [d for d in diags if d.is_error()]
    assert not errs, [f"{d.code}: {d.message}" for d in errs]
    return resolved.program


def emit_kt_src(src, filename="<test>.newr", stem="Main"):
    """Emit a checked program to Kotlin; returns the EmitResult."""
    program = full_program(src, filename)
    return emit_kt(program, main_path=filename,
                   sources={filename: src}, output_stem=stem)


def emit_kt_main_text(src, filename="<test>.newr", stem="Main"):
    """The emitted text of the main output file."""
    result = emit_kt_src(src, filename, stem)
    return result.files[result.main_file]


def run_kt_src(src, filename="<test>.newr", stem="Main",
               argv=None, env=None, files=None):
    """Emit to a temp dir, copy newr_rt.kt if needed, compile with
    kotlinc and run with java.

    Returns (stdout, stderr, returncode). `files` maps extra
    .newr filenames to sources (for file imports).
    """
    result = emit_kt_src(src, filename, stem)
    with tempfile.TemporaryDirectory() as d:
        for name, text in result.files.items():
            (Path(d) / name).write_text(text, encoding="utf-8")
        if result.uses_rt:
            (Path(d) / "newr_rt.kt").write_text(
                RT_KT_SRC.read_text(encoding="utf-8"), encoding="utf-8")
        kt_files = [str(p) for p in Path(d).glob("*.kt")]
        proc = subprocess.run(
            [KOTLINC, *kt_files, "-include-runtime", "-d",
             str(Path(d) / "app.jar")],
            capture_output=True, text=True, cwd=d, env=_kt_env(env))
        if proc.returncode != 0:
            return "", proc.stderr, proc.returncode
        proc = subprocess.run(
            [JAVA, "-jar", str(Path(d) / "app.jar"), *(argv or [])],
            capture_output=True, text=True, cwd=d, env=_kt_env(env))
        return proc.stdout, proc.stderr, proc.returncode


def run_kt_ok(src, filename="<test>.newr", stem="Main",
              argv=None, env=None):
    stdout, stderr, code = run_kt_src(src, filename, stem, argv, env)
    assert code == 0, f"exit {code}\nstdout: {stdout}\nstderr: {stderr}"
    return stdout


@unittest.skipUnless(JAVA and KOTLINC, "kotlinc/java is not installed")
@unittest.skipUnless(HAS_KT_EMITTER, "compiler/emitter_kt.py is missing")
class TestHelloAndHeader(unittest.TestCase):
    def test_hello_emits_and_runs(self):
        text = emit_kt_main_text('print "Hello, world!"\n',
                                 "examples/hello.newr", "Hello")
        self.assertIn('System.out.println("Hello, world!")', text)
        self.assertEqual(run_kt_ok('print "Hello, world!"\n',
                                   "examples/hello.newr", "Hello"),
                         "Hello, world!\n")

    def test_header_names_the_source_file(self):
        text = emit_kt_main_text('print "x"\n', "examples/hello.newr",
                                 "Hello")
        self.assertTrue(
            text.startswith(
                "// Generated by newr from examples/hello.newr "
                "— do not edit by hand.\n"
                "// Edit the .newr file and rebuild with: "
                "newr build examples/hello.newr\n"),
            text[:200])

    def test_no_rt_when_unused(self):
        result = emit_kt_src('print "Hello, world!"\n',
                             "examples/hello.newr", "Hello")
        self.assertFalse(result.uses_rt)
        self.assertNotIn("NewrRt", result.files[result.main_file])

    def test_object_name_is_the_stem(self):
        text = emit_kt_main_text('print "x"\n', "examples/hello.newr",
                                 "Hello")
        self.assertIn("object Hello {", text)
        self.assertIn("fun main(args: Array<String>) {", text)

    def test_arithmetic_emits_and_runs(self):
        self.assertEqual(
            run_kt_ok('let x = 1 + 2 * 3\nprint x\n'), "7\n")
        self.assertEqual(
            run_kt_ok('print 2 ** 10\n'), "1024\n")
        self.assertEqual(
            run_kt_ok('print 7 / 2\n'), "3\n")
        self.assertEqual(
            run_kt_ok('print 7 % 3\n'), "1\n")
        self.assertEqual(
            run_kt_ok('print -7 / 2\n'), "-3\n")
        self.assertEqual(
            run_kt_ok('print -7 % 2\n'), "-1\n")


if __name__ == "__main__":
    unittest.main()
