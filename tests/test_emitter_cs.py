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


if __name__ == "__main__":
    unittest.main()
