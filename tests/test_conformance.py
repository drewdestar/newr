"""Conformance corpus tests (SPEC.md §17.3, M-14).

The corpus in ``conformance/`` is seeded in M1: a small set of programs
with expected verdicts. ``ok/`` programs must check clean (exit 0, no
warnings); ``err/`` programs must fail with exit 4 and the code named
in their ``# expect: <CODE>`` header line.

This is the seed, not the full corpus: it becomes "the single definition
of correct behavior" as it grows, once the emitters land in M2 and the
same programs are run through every target (Gleam model, see
conformance/README.md).
"""

import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LAUNCHER = ROOT / "newr"


def _check(path: Path) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(LAUNCHER), "check",
                           str(path)],
                          capture_output=True, text=True)


def _expected_code(path: Path) -> str:
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("# expect:"):
            return line.split(":", 1)[1].strip()
    raise AssertionError(f"{path} has no `# expect: <CODE>` header")


class TestConformanceOk(unittest.TestCase):
    def test_ok_programs_check_clean(self):
        ok_dir = ROOT / "conformance" / "ok"
        files = sorted(ok_dir.glob("*.newr"))
        self.assertTrue(files, "conformance/ok/ is empty")
        for path in files:
            with self.subTest(program=path.name):
                r = _check(path)
                self.assertEqual(r.returncode, 0,
                                 f"{path.name}: {r.stdout}{r.stderr}")
                self.assertIn(": ok", r.stdout)


class TestConformanceErr(unittest.TestCase):
    def test_err_programs_fail_with_expected_code(self):
        err_dir = ROOT / "conformance" / "err"
        files = sorted(err_dir.glob("*.newr"))
        self.assertTrue(files, "conformance/err/ is empty")
        for path in files:
            with self.subTest(program=path.name):
                code = _expected_code(path)
                r = _check(path)
                self.assertEqual(r.returncode, 4,
                                 f"{path.name}: {r.stdout}{r.stderr}")
                self.assertIn(code, r.stdout,
                              f"{path.name}: expected {code} in output")


class TestConformanceRun(unittest.TestCase):
    def test_ok_programs_build_run_and_print_pass(self):
        # M2: the harness (conformance/run.py) builds every ok
        # program, runs the emitted Python, and requires exit 0
        # with PASS as the last stdout line (each program
        # self-verifies). This is the full corpus, not just the
        # check verdicts.
        r = subprocess.run([sys.executable, str(ROOT / "conformance"
                                                / "run.py")],
                           capture_output=True, text=True, cwd=ROOT)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("ok ", r.stdout)

    def test_ok_programs_build_run_and_print_pass_js(self):
        # M3: the same corpus on the JavaScript target - built with
        # `--target js`, run under Node, same PASS contract.
        r = subprocess.run([sys.executable, str(ROOT / "conformance"
                                                / "run.py"),
                            "--target", "js"],
                           capture_output=True, text=True, cwd=ROOT)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("ok ", r.stdout)


if __name__ == "__main__":
    unittest.main()
