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
        self.assertIn("newr 0.1.0", r.stdout)

    def test_version_subcommand(self):
        r = run_cli("version")
        self.assertEqual(r.returncode, 0)
        self.assertIn("newr 0.1.0", r.stdout)

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
        with tempfile.TemporaryDirectory() as d:
            f = write(d, "hello.newr", 'print "Hello, world!"\n')
            r = run_cli("build", str(f))
            self.assertEqual(r.returncode, 1)
            self.assertIn("--check", r.stderr)
            self.assertIn("M2", r.stderr)

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
            self.assertEqual(env["newr"], "0.1.0")
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
