"""Tests for the `newr` CLI (SPEC.md §19).

End-to-end through the `newr` launcher via subprocess (sys.executable),
so the launcher itself is tested too. Fixtures live in
TemporaryDirectory (no hardcoded /tmp); the repo's own examples are
used for the v0-compat checks.
"""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LAUNCHER = ROOT / "newr"


def run_cli(*args, cwd=None):
    return subprocess.run([sys.executable, str(LAUNCHER), *args],
                          capture_output=True, text=True, cwd=cwd)


def write(d, name, text):
    path = Path(d) / name
    path.write_text(text, encoding="utf-8")
    return path


class TestHelpAndVersion(unittest.TestCase):
    def test_help_exits_0_and_names_commands(self):
        r = run_cli("help")
        self.assertEqual(r.returncode, 0)
        for word in ("check", "build", "version", "explain"):
            self.assertIn(word, r.stdout)

    def test_no_args_prints_help_exit_0(self):
        # v0 behaviour: bare `newr` prints help and exits 0.
        r = run_cli()
        self.assertEqual(r.returncode, 0)
        self.assertIn("Usage", r.stdout)

    def test_version_flag(self):
        r = run_cli("--version")
        self.assertEqual(r.returncode, 0)
        self.assertIn("newr 0.2.0", r.stdout)

    def test_version_subcommand(self):
        r = run_cli("version")
        self.assertEqual(r.returncode, 0)
        self.assertIn("newr 0.2.0", r.stdout)

    def test_help_for_one_command(self):
        r = run_cli("help", "check")
        self.assertEqual(r.returncode, 0)
        self.assertIn("check", r.stdout)

    def test_unknown_command_is_exit_1(self):
        r = run_cli("frobnicate")
        self.assertEqual(r.returncode, 1)
        self.assertIn("unknown command", r.stderr)

    def test_help_unknown_topic_is_exit_1(self):
        r = run_cli("help", "frobnicate")
        self.assertEqual(r.returncode, 1)


class TestCheck(unittest.TestCase):
    def test_check_clean_file_exits_0(self):
        with tempfile.TemporaryDirectory() as d:
            f = write(d, "hello.newr", 'print "Hello, world!"\n')
            r = run_cli("check", str(f))
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertIn("ok", r.stdout)

    def test_check_type_error_exits_4_with_code(self):
        with tempfile.TemporaryDirectory() as d:
            f = write(d, "bad.newr", "let x: float = 5\nprint x\n")
            r = run_cli("check", str(f))
            self.assertEqual(r.returncode, 4)
            self.assertIn("E2001", r.stdout)
            self.assertIn("float", r.stdout)

    def test_check_reports_warnings_but_exits_0(self):
        with tempfile.TemporaryDirectory() as d:
            f = write(d, "warn.newr", "let unused = 1\n")
            r = run_cli("check", str(f))
            self.assertEqual(r.returncode, 0)
            self.assertIn("W1001", r.stdout)

    def test_check_missing_file_is_exit_2(self):
        with tempfile.TemporaryDirectory() as d:
            r = run_cli("check", str(Path(d) / "nope.newr"))
            self.assertEqual(r.returncode, 2)
            self.assertIn("nope.newr", r.stderr)

    def test_check_no_file_arg_is_exit_1(self):
        r = run_cli("check")
        self.assertEqual(r.returncode, 1)
        self.assertIn("check", r.stderr)

    def test_check_v0_examples(self):
        for name in ("hello_bot.newr", "showcase.newr"):
            r = run_cli("check", str(ROOT / "examples" / name))
            self.assertEqual(r.returncode, 0, name + ": " + r.stdout)


class TestBuild(unittest.TestCase):
    def test_build_check_clean_file_exits_0(self):
        with tempfile.TemporaryDirectory() as d:
            f = write(d, "hello.newr", 'print "Hello, world!"\n')
            r = run_cli("build", "--check", str(f))
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_build_without_check_is_exit_1_with_guidance(self):
        # M2: `build` without --check now EMITS (the M1 guidance is
        # obsolete). This test is replaced by test_build_emits_py.
        raise unittest.SkipTest("M1-only; replaced by test_build_emits_py")

    def test_build_emits_py_next_to_input(self):
        with tempfile.TemporaryDirectory() as d:
            f = write(d, "hello.newr", 'print "Hello, world!"\n')
            r = run_cli("build", str(f))
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            out = Path(d) / "hello.py"
            self.assertTrue(out.is_file())
            self.assertIn("Hello, world!", out.read_text(encoding="utf-8"))
            # The emitted program runs.
            run = subprocess.run([sys.executable, str(out)],
                                 capture_output=True, text=True)
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertEqual(run.stdout, "Hello, world!\n")

    def test_build_output_flag(self):
        with tempfile.TemporaryDirectory() as d:
            f = write(d, "hello.newr", 'print "Hello, world!"\n')
            out = Path(d) / "sub" / "greeting.py"
            r = run_cli("build", str(f), "-o", str(out))
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertTrue(out.is_file())

    def test_build_js_target_is_exit_1(self):
        # M3: `--target js` now EMITS (the M2 refusal is obsolete).
        # This test is replaced by TestBuildJs below.
        raise unittest.SkipTest("M2-only; replaced by TestBuildJs")

    def test_build_fast_emit_is_exit_1(self):
        # D27: --fast + emission is refused (emission needs checked
        # types).
        with tempfile.TemporaryDirectory() as d:
            f = write(d, "hello.newr", 'print "Hello, world!"\n')
            r = run_cli("build", "--fast", str(f))
            self.assertEqual(r.returncode, 1)
            self.assertIn("--fast", r.stderr)

    def test_build_type_error_exits_4_and_writes_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            f = write(d, "bad.newr", "let x: float = 5\nprint x\n")
            r = run_cli("build", str(f))
            self.assertEqual(r.returncode, 4)
            self.assertFalse((Path(d) / "bad.py").exists())

    def test_build_untyped_expression_error_is_single_and_pointed(self):
        # §20: an emitter refusal must name the failing construct
        # once, point at the expression itself, and never send the
        # user back to `newr check` (which passes on this program).
        # A bare enum name has no type: `check` accepts it, the
        # emitter refuses it.
        src = ("enum Color:\n"
               "  Red\n"
               "\n"
               "let c = Color\n"
               "print c\n")
        with tempfile.TemporaryDirectory() as d:
            f = write(d, "bad.newr", src)
            r = run_cli("build", str(f))
            self.assertEqual(r.returncode, 4)
            self.assertFalse((Path(d) / "bad.py").exists())
            self.assertEqual(
                r.stdout.count("cannot emit this program"), 1,
                r.stdout)
            self.assertNotIn("run `newr check` first", r.stdout)
            # The span is the `Color` on line 4, not the file start.
            self.assertIn(":4:", r.stdout, r.stdout)
            self.assertNotIn("1 | enum Color:", r.stdout)

    def test_build_copies_runtime_when_used(self):
        with tempfile.TemporaryDirectory() as d:
            # int arithmetic needs the checked helpers (uses_rt).
            f = write(d, "calc.newr",
                      "let x: int = 2 + 3\nprint x\n")
            r = run_cli("build", str(f))
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertTrue((Path(d) / "newr_rt.py").is_file())
            run = subprocess.run([sys.executable, str(Path(d) / "calc.py")],
                                 capture_output=True, text=True)
            self.assertEqual(run.stdout, "5\n")

    def test_build_no_runtime_when_unused(self):
        with tempfile.TemporaryDirectory() as d:
            f = write(d, "hello.newr", 'print "Hello, world!"\n')
            r = run_cli("build", str(f))
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertFalse((Path(d) / "newr_rt.py").exists())

    def test_build_file_import_emits_both_files(self):
        with tempfile.TemporaryDirectory() as d:
            write(d, "items.newr",
                  'pub fn price(item: str) -> int:\n'
                  '  if item == "brick":\n'
                  '    return 118\n'
                  '  return 0\n')
            f = write(d, "main.newr",
                      'import "items.newr"\nprint items.price("brick")\n')
            r = run_cli("build", str(f))
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertTrue((Path(d) / "main.py").is_file())
            self.assertTrue((Path(d) / "items.py").is_file())
            run = subprocess.run([sys.executable, str(Path(d) / "main.py")],
                                 capture_output=True, text=True, cwd=d)
            self.assertEqual(run.stdout, "118\n", run.stderr)

    def test_build_unknown_target_is_exit_1(self):
        with tempfile.TemporaryDirectory() as d:
            f = write(d, "hello.newr", 'print "Hello, world!"\n')
            r = run_cli("build", "--target", "go", "--check", str(f))
            self.assertEqual(r.returncode, 1)

    def test_build_fast_check_skips_full_type_check(self):
        with tempfile.TemporaryDirectory() as d:
            f = write(d, "bad.newr", "let x: float = 5\nprint x\n")
            full = run_cli("check", str(f))
            self.assertEqual(full.returncode, 4)
            fast = run_cli("build", "--fast", "--check", str(f))
            self.assertEqual(fast.returncode, 0, fast.stdout + fast.stderr)
            self.assertIn("not verified", fast.stderr)


class TestBuildJs(unittest.TestCase):
    """`newr build --target js` (M3): same contract as the Python
    target — emit, -o, --check, --fast refusal, exit-4 on errors."""

    def test_build_js_emits_js_next_to_input_and_runs(self):
        with tempfile.TemporaryDirectory() as d:
            f = write(d, "hello.newr", 'print "Hello, world!"\n')
            r = run_cli("build", "--target", "js", str(f))
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            out = Path(d) / "hello.js"
            self.assertTrue(out.is_file())
            self.assertIn("Hello, world!", out.read_text(encoding="utf-8"))
            run = subprocess.run(["node", str(out)],
                                 capture_output=True, text=True)
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertEqual(run.stdout, "Hello, world!\n")

    def test_build_js_output_flag(self):
        with tempfile.TemporaryDirectory() as d:
            f = write(d, "hello.newr", 'print "Hello, world!"\n')
            out = Path(d) / "sub" / "greeting.js"
            r = run_cli("build", "--target", "js", str(f), "-o", str(out))
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertTrue(out.is_file())

    def test_build_js_copies_runtime_when_used(self):
        with tempfile.TemporaryDirectory() as d:
            # int arithmetic needs the checked helpers (uses_rt).
            f = write(d, "calc.newr",
                      "let x: int = 2 + 3\nprint x\n")
            r = run_cli("build", "--target", "js", str(f))
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertTrue((Path(d) / "newr_rt.js").is_file())
            run = subprocess.run(["node", str(Path(d) / "calc.js")],
                                 capture_output=True, text=True)
            self.assertEqual(run.stdout, "5\n")

    def test_build_js_no_runtime_when_unused(self):
        with tempfile.TemporaryDirectory() as d:
            f = write(d, "hello.newr", 'print "Hello, world!"\n')
            r = run_cli("build", "--target", "js", str(f))
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertFalse((Path(d) / "newr_rt.js").exists())

    def test_build_js_file_import_emits_both_files(self):
        with tempfile.TemporaryDirectory() as d:
            write(d, "items.newr",
                  'pub fn price(item: str) -> int:\n'
                  '  if item == "brick":\n'
                  '    return 118\n'
                  '  return 0\n')
            f = write(d, "main.newr",
                      'import "items.newr"\nprint items.price("brick")\n')
            r = run_cli("build", "--target", "js", str(f))
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertTrue((Path(d) / "main.js").is_file())
            self.assertTrue((Path(d) / "items.js").is_file())
            run = subprocess.run(["node", str(Path(d) / "main.js")],
                                 capture_output=True, text=True, cwd=d)
            self.assertEqual(run.stdout, "118\n", run.stderr)

    def test_build_js_type_error_exits_4_and_writes_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            f = write(d, "bad.newr", "let x: float = 5\nprint x\n")
            r = run_cli("build", "--target", "js", str(f))
            self.assertEqual(r.returncode, 4)
            self.assertFalse((Path(d) / "bad.js").exists())

    def test_build_js_untyped_expression_error_is_single_and_pointed(self):
        # §20 twin of the Python diagnostic test: one prefix, no
        # "run check first" circle, span on the failing expression.
        src = ("enum Color:\n"
               "  Red\n"
               "\n"
               "let c = Color\n"
               "print c\n")
        with tempfile.TemporaryDirectory() as d:
            f = write(d, "bad.newr", src)
            r = run_cli("build", "--target", "js", str(f))
            self.assertEqual(r.returncode, 4)
            self.assertFalse((Path(d) / "bad.js").exists())
            self.assertEqual(
                r.stdout.count("cannot emit this program"), 1,
                r.stdout)
            self.assertNotIn("run `newr check` first", r.stdout)
            self.assertIn(":4:", r.stdout, r.stdout)
            self.assertNotIn("1 | enum Color:", r.stdout)

    def test_build_js_fast_emit_is_exit_1(self):
        with tempfile.TemporaryDirectory() as d:
            f = write(d, "hello.newr", 'print "Hello, world!"\n')
            r = run_cli("build", "--target", "js", "--fast", str(f))
            self.assertEqual(r.returncode, 1)
            self.assertIn("--fast", r.stderr)

    def test_build_js_check_writes_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            f = write(d, "hello.newr", 'print "Hello, world!"\n')
            r = run_cli("build", "--target", "js", "--check", str(f))
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertFalse((Path(d) / "hello.js").exists())

    def test_build_js_bot_also_writes_deploy_file(self):
        # The standalone registration script rides along for bots.
        src = (ROOT / "examples" / "hello_bot.newr").read_text(
            encoding="utf-8")
        with tempfile.TemporaryDirectory() as d:
            f = write(d, "hello_bot.newr", src)
            r = run_cli("build", "--target", "js", str(f))
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertTrue((Path(d) / "hello_bot.js").is_file())
            self.assertTrue((Path(d) / "hello_bot_deploy.js").is_file())
            check = subprocess.run(
                ["node", "--check", str(Path(d) / "hello_bot_deploy.js")],
                capture_output=True, text=True)
            self.assertEqual(check.returncode, 0, check.stderr)


class TestMachineOutput(unittest.TestCase):
    def test_error_format_json(self):
        with tempfile.TemporaryDirectory() as d:
            f = write(d, "bad.newr", "let x: float = 5\nprint x\n")
            r = run_cli("check", "--error-format=json", str(f))
            self.assertEqual(r.returncode, 4)
            data = json.loads(r.stdout)
            self.assertIsInstance(data, list)
            self.assertEqual(data[0]["code"], "E2001")
            self.assertEqual(data[0]["line"], 1)

    def test_json_envelope_clean(self):
        with tempfile.TemporaryDirectory() as d:
            f = write(d, "hello.newr", 'print "Hello, world!"\n')
            r = run_cli("check", "--json", str(f))
            self.assertEqual(r.returncode, 0)
            env = json.loads(r.stdout)
            self.assertEqual(
                sorted(env), ["command", "data", "error", "newr", "ok",
                              "warnings"])
            self.assertTrue(env["ok"])
            self.assertEqual(env["command"], "check")
            self.assertEqual(env["newr"], "0.2.0")
            self.assertIsNone(env["error"])

    def test_json_envelope_error(self):
        with tempfile.TemporaryDirectory() as d:
            f = write(d, "bad.newr", "let x: float = 5\nprint x\n")
            r = run_cli("check", "--json", str(f))
            self.assertEqual(r.returncode, 4)
            env = json.loads(r.stdout)
            self.assertFalse(env["ok"])
            self.assertEqual(env["error"]["code"], "E2001")


class TestExplain(unittest.TestCase):
    def test_explain_known_code(self):
        r = run_cli("--explain", "E2001")
        self.assertEqual(r.returncode, 0)
        self.assertIn("E2001", r.stdout)
        self.assertIn("mismatched types", r.stdout.lower())

    def test_explain_subcommand_form(self):
        r = run_cli("explain", "E3001")
        self.assertEqual(r.returncode, 0)
        self.assertIn("E3001", r.stdout)

    def test_explain_unknown_code_is_exit_1(self):
        r = run_cli("--explain", "E9999")
        self.assertEqual(r.returncode, 1)

    def test_explain_missing_code_is_exit_1(self):
        r = run_cli("--explain")
        self.assertEqual(r.returncode, 1)


class TestZipappDiff(unittest.TestCase):
    """SPEC.md §17.3: the built zipapp must behave identically to the
    checkout launcher — same `check` verdicts on the same programs.

    This is the repo-side half of the CI zipapp-diff job (which runs
    the ubuntu-built .pyz on all three OSes)."""

    def test_zipapp_matches_launcher_verdicts(self):
        with tempfile.TemporaryDirectory() as d:
            pyz = Path(d) / "newr.pyz"
            build = subprocess.run(
                [sys.executable, "-m", "zipapp", str(ROOT), "-o",
                 str(pyz), "-p", "/usr/bin/env python3"],
                capture_output=True, text=True)
            self.assertEqual(build.returncode, 0, build.stderr)
            cases = [
                ("examples/hello_bot.newr", 0),
                ("examples/showcase.newr", 0),
                ("examples/hello.newr", 0),
                ("examples/shop.newr", 0),
                ("conformance/err/type-mismatch.newr", 4),
            ]
            for rel, want in cases:
                target = str(ROOT / rel)
                via_launcher = run_cli("check", target)
                via_zipapp = subprocess.run(
                    [sys.executable, str(pyz), "check", target],
                    capture_output=True, text=True)
                self.assertEqual(via_launcher.returncode, want,
                                 f"launcher {rel}: {via_launcher.stdout}")
                self.assertEqual(via_zipapp.returncode, want,
                                 f"zipapp {rel}: {via_zipapp.stdout}")
                self.assertEqual(via_zipapp.stdout, via_launcher.stdout,
                                 f"zipapp output differs from launcher "
                                 f"for {rel}")


if __name__ == "__main__":
    unittest.main()
