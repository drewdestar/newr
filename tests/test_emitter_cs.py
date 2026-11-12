"""Tests for the C# emitter (compiler/emitter_cs.py, M4).

TDD RED first: written before the emitter existed. The
`emit_cs_src` helper runs the full front-end pipeline (resolve +
core check + discord check) and emits C#; `run_cs_src` also
compiles and executes the emitted program with dotnet and returns
its stdout.

Every test pins a decision from BUILD_REPORT.md §21 (decisions
C1–C13) or a SPEC rule the emitter must preserve — including the
C#-specific traps (checked long via checked{} blocks, `long?`
nullable value types and the OptChainVal/CoalesceVal/ UnwrapOptVal
helpers, `#nullable enable annotations` in the runtime vs
`<Nullable>disable</Nullable>` in the generated project, top-level
statements vs static-class layout, the ternary-default trap in
OptChainVal).
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
RT_CS_SRC = ROOT / "compiler" / "newr_rt.cs"

# dotnet lives in ~/.dotnet (installed for M4); fall back to the
# PATH when it is not there (e.g. CI, where setup-dotnet provides it).
_DOTNET_ROOT = Path(os.environ["DOTNET_ROOT"]) \
    if os.environ.get("DOTNET_ROOT") else (Path.home() / ".dotnet")
DOTNET = shutil.which("dotnet", path=str(_DOTNET_ROOT)) \
    or shutil.which("dotnet")

try:
    from compiler.emitter_cs import EmitterError, emit_program as emit_cs
    HAS_CS_EMITTER = True
except ImportError:
    HAS_CS_EMITTER = False


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


def emit_cs_src(src, filename="<test>.newr", stem="Main"):
    """Emit a checked program to C#; returns the EmitResult."""
    program = full_program(src, filename)
    return emit_cs(program, main_path=filename,
                   sources={filename: src}, output_stem=stem)


def emit_cs_main_text(src, filename="<test>.newr", stem="Main"):
    """The emitted text of the main output file."""
    result = emit_cs_src(src, filename, stem)
    return result.files[result.main_file]


def _csproj_text(stem):
    return (f"<Project Sdk=\"Microsoft.NET.Sdk\">\n"
            f"  <PropertyGroup>\n"
            f"    <OutputType>Exe</OutputType>\n"
            f"    <TargetFramework>net8.0</TargetFramework>\n"
            f"    <Nullable>disable</Nullable>\n"
            f"    <ImplicitUsings>disable</ImplicitUsings>\n"
            f"    <AssemblyName>{stem}</AssemblyName>\n"
            f"  </PropertyGroup>\n"
            f"</Project>\n")


def run_cs_src(src, filename="<test>.newr", stem="Main",
               argv=None, env=None):
    """Emit to a temp dir, copy newr_rt.cs if needed, build + run
    with dotnet.

    Returns (stdout, stderr, returncode).
    """
    result = emit_cs_src(src, filename, stem)
    with tempfile.TemporaryDirectory() as d:
        for name, text in result.files.items():
            (Path(d) / name).write_text(text, encoding="utf-8")
        if result.uses_rt:
            (Path(d) / "newr_rt.cs").write_text(
                RT_CS_SRC.read_text(encoding="utf-8"), encoding="utf-8")
        csproj = Path(d) / (stem + ".csproj")
        if not csproj.exists():
            csproj.write_text(_csproj_text(stem), encoding="utf-8")
        run_env = dict(os.environ)
        run_env["DOTNET_CLI_TELEMETRY_OPTOUT"] = "1"
        if env:
            run_env.update(env)
        proc = subprocess.run(
            [DOTNET, "run", "--project", str(csproj), "--",
             *(argv or [])],
            capture_output=True, text=True, cwd=d, env=run_env)
        return proc.stdout, proc.stderr, proc.returncode


def run_cs_ok(src, filename="<test>.newr", stem="Main",
              argv=None, env=None):
    stdout, stderr, code = run_cs_src(src, filename, stem, argv, env)
    assert code == 0, f"exit {code}\nstdout: {stdout}\nstderr: {stderr}"
    return stdout


@unittest.skipUnless(DOTNET, "dotnet is not installed")
@unittest.skipUnless(HAS_CS_EMITTER, "compiler/emitter_cs.py is missing")
class TestHelloAndHeader(unittest.TestCase):
    def test_hello_emits_and_runs(self):
        text = emit_cs_main_text('print "Hello, world!"\n',
                                 "examples/hello.newr", "Hello")
        self.assertIn('Console.WriteLine("Hello, world!")', text)
        self.assertEqual(run_cs_ok('print "Hello, world!"\n',
                                   "examples/hello.newr", "Hello"),
                         "Hello, world!\n")

    def test_header_names_the_source_file(self):
        text = emit_cs_main_text('print "x"\n', "examples/hello.newr",
                                 "Hello")
        self.assertTrue(
            text.startswith(
                "// Generated by newr from examples/hello.newr "
                "— do not edit by hand.\n"
                "// Edit the .newr file and rebuild with: "
                "newr build examples/hello.newr\n"),
            text[:200])

    def test_no_rt_import_when_unused(self):
        result = emit_cs_src('print "Hello, world!"\n',
                             "examples/hello.newr", "Hello")
        self.assertFalse(result.uses_rt)
        self.assertNotIn("NewrRt", result.files[result.main_file])

    def test_arithmetic_emits_and_runs(self):
        self.assertEqual(
            run_cs_ok('let x = 1 + 2 * 3\nprint x\n'), "7\n")
        self.assertEqual(
            run_cs_ok('print 2 ** 10\n'), "1024\n")
        self.assertEqual(
            run_cs_ok('print 7 / 2\n'), "3\n")
        self.assertEqual(
            run_cs_ok('print 7 % 3\n'), "1\n")
        self.assertEqual(
            run_cs_ok('print -7 / 2\n'), "-3\n")
        self.assertEqual(
            run_cs_ok('print -7 % 2\n'), "-1\n")


class TestInput(unittest.TestCase):
    def test_input_maps_to_runtime_helper(self):
        # SPEC.md §12.1: input() is a core builtin on every target;
        # the C# emitter routes it to NewrRt.Input.
        text = emit_cs_main_text('let s = input("Name? ")\nprint s\n')
        self.assertIn('NewrRt.Input("Name? ")', text)


@unittest.skipUnless(DOTNET, "dotnet is not installed")
class TestDb(unittest.TestCase):
    """db module on C# (SPEC.md section 12.5, M8a): `db.*` calls
    emit to the `NewrDb` class in the emitted file (inline, not
    newr_rt.cs — the runtime file stays dependency-free), over
    Microsoft.Data.Sqlite, and a program using them runs with the
    Sqlite package in its csproj (B8)."""

    def _emit(self, src, stem="Probe"):
        from compiler.db_check import check_db
        resolved = resolve_source(src, "<db-test>.newr")
        diags = list(resolved.diagnostics)
        diags += check_program(
            resolved.program,
            providers=discord_providers(resolved.program)).diagnostics
        diags += check_discord(resolved.program)
        diags += check_db(resolved.program)
        bad = [d for d in diags if d.is_error()]
        self.assertEqual(bad, [], [f"{d.code}: {d.message}" for d in bad])
        return emit_cs(resolved.program, main_path="<db-test>.newr",
                       sources={"<db-test>.newr": src},
                       output_stem=stem)

    def _run(self, src, stem="Probe"):
        result = self._emit(src, stem)
        with tempfile.TemporaryDirectory() as d:
            for name, text in result.files.items():
                (Path(d) / name).write_text(text, encoding="utf-8")
            if result.uses_rt:
                (Path(d) / "newr_rt.cs").write_text(
                    RT_CS_SRC.read_text(encoding="utf-8"),
                    encoding="utf-8")
            run_env = dict(os.environ)
            run_env["DOTNET_CLI_TELEMETRY_OPTOUT"] = "1"
            proc = subprocess.run(
                [DOTNET, "run", "--project",
                 str(Path(d) / (stem + ".csproj"))],
                capture_output=True, text=True, cwd=d, env=run_env)
            return proc.stdout, proc.stderr, proc.returncode

    def test_db_probe_runs(self):
        src = ('struct Todo:\n'
               '  id: int\n'
               '  title: str\n'
               '  done: bool\n'
               '\n'
               'let h = db.open(":memory:")\n'
               'db.exec(h, "CREATE TABLE todos (id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL, done INTEGER NOT NULL)")\n'
               'let n = db.exec(h, "INSERT INTO todos (title, done) VALUES (?, ?)", "beli susu", false)\n'
               'print(n)\n'
               'let id = db.last_id(h)\n'
               'print(id)\n'
               'let todos: list[Todo] = db.query(h, "SELECT id, title, done FROM todos")\n'
               'print(todos)\n'
               'db.begin(h)\n'
               'db.exec(h, "UPDATE todos SET done = ? WHERE id = ?", true, 1)\n'
               'db.commit(h)\n'
               'let todos2: list[Todo] = db.query(h, "SELECT id, title, done FROM todos WHERE done = ?", true)\n'
               'print(todos2)\n'
               'db.begin(h)\n'
               'db.exec(h, "INSERT INTO todos (title, done) VALUES (?, ?)", "rollback me", false)\n'
               'db.rollback(h)\n'
               'let todos3: list[Todo] = db.query(h, "SELECT id, title, done FROM todos")\n'
               'print(todos3)\n'
               'db.close(h)\n')
        out, err, code = self._run(src)
        self.assertEqual(code, 0, err)
        self.assertEqual(
            out,
            "1\n1\n[Todo(id: 1, title: beli susu, done: false)]\n"
            "[Todo(id: 1, title: beli susu, done: true)]\n"
            "[Todo(id: 1, title: beli susu, done: true)]\n")

    def test_db_question_mark_in_literal_not_a_placeholder(self):
        # The `?` inside the 'what?' literal is data, not a
        # placeholder: the rewriter must leave it alone (and the
        # checker counts one placeholder, not two).
        src = ('struct Todo:\n'
               '  id: int\n'
               '  title: str\n'
               '  done: bool\n'
               '\n'
               'let h = db.open(":memory:")\n'
               'db.exec(h, "CREATE TABLE todos (id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL, done INTEGER NOT NULL)")\n'
               'db.exec(h, "INSERT INTO todos (title, done) VALUES (\'what?\', ?)", false)\n'
               'let todos: list[Todo] = db.query(h, "SELECT id, title, done FROM todos")\n'
               'print(todos)\n'
               'db.close(h)\n')
        out, err, code = self._run(src)
        self.assertEqual(code, 0, err)
        self.assertEqual(
            out, "[Todo(id: 1, title: what?, done: false)]\n")

    def test_db_uses_newr_db_helpers(self):
        text = self._emit(
            'let h = db.open(":memory:")\n'
            'db.exec(h, "CREATE TABLE t (v INTEGER)")\n'
            'db.close(h)\n').files["Probe.cs"]
        self.assertIn("NewrDb.Open(", text)
        self.assertIn("NewrDb.Exec(", text)
        self.assertIn("NewrDb.Close(", text)
        self.assertIn("private static class NewrDb", text)

    def test_db_program_csproj_has_sqlite_reference(self):
        result = self._emit(
            'let h = db.open(":memory:")\n'
            'db.close(h)\n')
        csproj = result.files["Probe.csproj"]
        self.assertIn("Microsoft.Data.Sqlite", csproj)
        self.assertIn('Version="8.0.0"', csproj)
        self.assertNotIn("Discord.Net", csproj)

    def test_plain_program_csproj_has_no_package_reference(self):
        result = emit_cs_src('print "hi"\n', stem="Hello")
        self.assertNotIn("PackageReference",
                         result.files["Hello.csproj"])


if __name__ == "__main__":
    unittest.main()
