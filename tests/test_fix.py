"""`newr fix` tests (SPEC.md section 5.2, M6 Phase 2d).

`newr fix` applies the safe, unambiguous fixes the diagnostics carry
(a span plus a replacement). In M6 the only fix producer is the
resolver (E3001 similar-name rename, exactly one close match); the
checker attaches no fixes yet. The command itself is general: any
diagnostic with a `fix` field is applied, so checker-attached fixes
(v1.1) work through the same code path with no change here.

Rules pinned by these tests:

- A file whose errors all have fixes is rewritten, every change is
  reported, and the fixed text is verified clean before it is
  written (a fix that leaves errors behind refuses instead).
- A file with any error that has no safe fix is refused whole: the
  file is not touched at all, not even for the errors that do have
  fixes. Refusal is loud, never a silent half-fix.
- Warnings never block a fix (they carry no fix and never fail the
  build). Diagnostics from other files (imports) are not this file's
  business: they neither block nor get applied here.
- Fixes are byte-span replacements, so non-ASCII source is handled
  (spans are byte offsets, SPEC.md section 16).
"""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from compiler.errors import Diagnostic, Fix, NewrError, Span  # noqa: E402
from compiler.fix import (  # noqa: E402
    AppliedFix,
    FixRefused,
    apply_fixes,
    fix_text,
    front_end_diagnostics,
)

ROOT = Path(__file__).resolve().parent.parent
LAUNCHER = ROOT / "newr"

# One typo with exactly one close match: the E3001 the resolver
# attaches a fix to.
TYPO = "let count = 1\nprint coun\n"
TYPO_FIXED = "let count = 1\nprint count\n"

# Two typos of two different names: both get a fix.
TWO_TYPOS = "let count = 1\nlet total = 2\nprint coun\nprint totl\n"
TWO_TYPOS_FIXED = "let count = 1\nlet total = 2\nprint count\nprint total\n"

# `coun` is close to both `count` and `counts`: ambiguous, no fix.
AMBIGUOUS = "let count = 1\nlet counts = 2\nprint coun\n"

# A type error has no safe fix (only the resolver attaches fixes).
TYPE_ERROR = 'let x: int = "hello"\n'

# One fixable typo plus one unfixable error: the whole file refuses.
TYPO_AND_TYPE_ERROR = 'let count = 1\nprint coun\nlet x: int = "hello"\n'

# Warnings only: an unused binding warns (W1001) but never fails.
WARNINGS_ONLY = 'let unused = 1\nprint "hi"\n'

# Non-ASCII text before the typo: spans are byte offsets, so the
# replacement must slice bytes, not characters.
NON_ASCII = 'print "h\u00e9llo \U0001f600"\nlet count = 1\nprint coun\n'
NON_ASCII_FIXED = 'print "h\u00e9llo \U0001f600"\nlet count = 1\nprint count\n'


def _run_cli(*argv):
    return subprocess.run(
        [sys.executable, str(LAUNCHER), *argv],
        capture_output=True, text=True)


def _diag(code, start, end, fix=None, severity="error", filename="t.newr"):
    return Diagnostic(
        span=Span(filename, start, end), code=code,
        message=f"test {code}", severity=severity, fix=fix)


class FixTextTestCase(unittest.TestCase):
    """`fix_text`: the whole front-end round trip on source text."""

    def test_single_typo_is_fixed(self):
        new_source, applied = fix_text(TYPO, "t.newr")
        self.assertEqual(new_source, TYPO_FIXED)
        self.assertEqual(len(applied), 1)
        change = applied[0]
        self.assertIsInstance(change, AppliedFix)
        self.assertEqual(change.code, "E3001")
        self.assertEqual((change.line, change.col), (2, 7))
        self.assertEqual(change.old, "coun")
        self.assertEqual(change.new, "count")

    def test_two_typos_are_both_fixed(self):
        new_source, applied = fix_text(TWO_TYPOS, "t.newr")
        self.assertEqual(new_source, TWO_TYPOS_FIXED)
        self.assertEqual(len(applied), 2)
        self.assertEqual([a.old for a in applied], ["coun", "totl"])
        self.assertEqual([a.new for a in applied], ["count", "total"])

    def test_same_typo_twice_is_fixed_twice(self):
        source = "let count = 1\nprint coun\nprint coun\n"
        new_source, applied = fix_text(source, "t.newr")
        self.assertEqual(new_source, "let count = 1\nprint count\nprint count\n")
        self.assertEqual(len(applied), 2)

    def test_clean_file_is_unchanged(self):
        new_source, applied = fix_text(TYPO_FIXED, "t.newr")
        self.assertEqual(new_source, TYPO_FIXED)
        self.assertEqual(applied, [])

    def test_warnings_do_not_block(self):
        new_source, applied = fix_text(WARNINGS_ONLY, "t.newr")
        self.assertEqual(new_source, WARNINGS_ONLY)
        self.assertEqual(applied, [])

    def test_ambiguous_names_refuse(self):
        with self.assertRaises(FixRefused) as ctx:
            fix_text(AMBIGUOUS, "t.newr")
        codes = [d.code for d in ctx.exception.diagnostics]
        self.assertIn("E3001", codes)
        self.assertTrue(all(d.fix is None for d in ctx.exception.diagnostics))

    def test_type_error_refuses(self):
        with self.assertRaises(FixRefused) as ctx:
            fix_text(TYPE_ERROR, "t.newr")
        codes = [d.code for d in ctx.exception.diagnostics]
        self.assertIn("E2001", codes)

    def test_typo_plus_type_error_refuses_whole_file(self):
        # Even though the typo has a fix, the unfixable error refuses
        # the whole file: no half-fix.
        with self.assertRaises(FixRefused) as ctx:
            fix_text(TYPO_AND_TYPE_ERROR, "t.newr")
        codes = [d.code for d in ctx.exception.diagnostics]
        self.assertIn("E2001", codes)
        self.assertIn("E3001", codes)

    def test_parse_error_raises(self):
        with self.assertRaises(NewrError):
            fix_text("let x = \n", "t.newr")

    def test_non_ascii_source_is_fixed(self):
        new_source, applied = fix_text(NON_ASCII, "t.newr")
        self.assertEqual(new_source, NON_ASCII_FIXED)
        self.assertEqual(len(applied), 1)
        self.assertEqual(applied[0].old, "coun")

    def test_fixed_text_verifies_clean(self):
        # The fixed text passes the full front end: fix_text only
        # returns when the result has no errors left in this file.
        new_source, _applied = fix_text(TYPO, "t.newr")
        remaining = [d for d in front_end_diagnostics(new_source, "t.newr")
                     if d.is_error()]
        self.assertEqual(remaining, [])


class ApplyFixesTestCase(unittest.TestCase):
    """`apply_fixes`: the pure span-replacement core, no front end."""

    def test_applies_one_fix(self):
        diags = [_diag("E3001", 20, 24,
                       fix=Fix(Span("t.newr", 20, 24), "count"))]
        new_source, applied = apply_fixes(TYPO, diags, filename="t.newr")
        self.assertEqual(new_source, TYPO_FIXED)
        self.assertEqual(len(applied), 1)

    def test_unfixable_error_refuses(self):
        diags = [_diag("E2001", 0, 3)]
        with self.assertRaises(FixRefused):
            apply_fixes("let x", diags, filename="t.newr")

    def test_warning_is_ignored(self):
        diags = [_diag("W1001", 0, 3, severity="warning")]
        new_source, applied = apply_fixes("let x", diags, filename="t.newr")
        self.assertEqual(new_source, "let x")
        self.assertEqual(applied, [])

    def test_other_file_diagnostics_are_ignored(self):
        # An error in an imported file neither blocks this file nor
        # gets applied here; fixing it is that file's own `newr fix`.
        diags = [
            _diag("E3001", 20, 24,
                  fix=Fix(Span("t.newr", 20, 24), "count")),
            _diag("E2001", 0, 3, filename="other.newr"),
        ]
        new_source, applied = apply_fixes(TYPO, diags, filename="t.newr")
        self.assertEqual(new_source, TYPO_FIXED)
        self.assertEqual(len(applied), 1)

    def test_other_file_fix_is_not_applied(self):
        diags = [_diag("E3001", 0, 4,
                       fix=Fix(Span("other.newr", 0, 4), "count"),
                       filename="other.newr")]
        new_source, applied = apply_fixes(TYPO, diags, filename="t.newr")
        self.assertEqual(new_source, TYPO)
        self.assertEqual(applied, [])

    def test_overlapping_fixes_refuse(self):
        diags = [
            _diag("E3001", 20, 24,
                  fix=Fix(Span("t.newr", 20, 24), "count")),
            _diag("E3001", 22, 26,
                  fix=Fix(Span("t.newr", 22, 26), "count")),
        ]
        with self.assertRaises(FixRefused):
            apply_fixes(TYPO, diags, filename="t.newr")

    def test_out_of_range_span_refuses(self):
        diags = [_diag("E3001", 0, 999,
                       fix=Fix(Span("t.newr", 0, 999), "count"))]
        with self.assertRaises(FixRefused):
            apply_fixes(TYPO, diags, filename="t.newr")


class FixCliTestCase(unittest.TestCase):
    """`newr fix` through the real CLI, in a temp directory."""

    def _write(self, directory, name, source):
        path = Path(directory) / name
        path.write_text(source, encoding="utf-8")
        return path

    def test_cli_fixes_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(tmp, "t.newr", TYPO)
            result = _run_cli("fix", str(path))
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(path.read_text(encoding="utf-8"), TYPO_FIXED)
            self.assertIn("coun", result.stdout)
            self.assertIn("count", result.stdout)
            self.assertIn("fixed 1 error", result.stdout)

    def test_cli_fixed_file_then_checks_clean(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(tmp, "t.newr", TYPO)
            self.assertEqual(_run_cli("fix", str(path)).returncode, 0)
            check = _run_cli("check", str(path))
            self.assertEqual(check.returncode, 0, check.stdout + check.stderr)

    def test_cli_dry_run_changes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(tmp, "t.newr", TYPO)
            result = _run_cli("fix", "--dry-run", str(path))
            self.assertEqual(result.returncode, 4, result.stdout + result.stderr)
            self.assertEqual(path.read_text(encoding="utf-8"), TYPO)
            self.assertIn("would fix 1 error", result.stdout)

    def test_cli_dry_run_on_clean_file_is_ok(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(tmp, "t.newr", TYPO_FIXED)
            result = _run_cli("fix", "--dry-run", str(path))
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("ok", result.stdout)

    def test_cli_refuses_unfixable_and_leaves_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(tmp, "t.newr", TYPO_AND_TYPE_ERROR)
            result = _run_cli("fix", str(path))
            self.assertEqual(result.returncode, 4, result.stdout + result.stderr)
            self.assertEqual(path.read_text(encoding="utf-8"),
                             TYPO_AND_TYPE_ERROR)
            self.assertIn("E2001", result.stdout)

    def test_cli_clean_file_is_ok(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(tmp, "t.newr", TYPO_FIXED)
            result = _run_cli("fix", str(path))
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("ok", result.stdout)
            self.assertEqual(path.read_text(encoding="utf-8"), TYPO_FIXED)

    def test_cli_parse_error_leaves_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(tmp, "t.newr", "let x = \n")
            result = _run_cli("fix", str(path))
            self.assertEqual(result.returncode, 4, result.stdout + result.stderr)
            self.assertEqual(path.read_text(encoding="utf-8"), "let x = \n")

    def test_cli_no_args_is_usage_error(self):
        result = _run_cli("fix")
        self.assertEqual(result.returncode, 1)

    def test_cli_unknown_flag_is_usage_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(tmp, "t.newr", TYPO)
            result = _run_cli("fix", "--nope", str(path))
            self.assertEqual(result.returncode, 1)
            self.assertEqual(path.read_text(encoding="utf-8"), TYPO)

    def test_cli_missing_file_is_env_error(self):
        result = _run_cli("fix", "no-such-file.newr")
        self.assertEqual(result.returncode, 2)

    def test_cli_multiple_files_continue_after_refusal(self):
        with tempfile.TemporaryDirectory() as tmp:
            good = self._write(tmp, "good.newr", TYPO)
            bad = self._write(tmp, "bad.newr", TYPE_ERROR)
            result = _run_cli("fix", str(good), str(bad))
            self.assertEqual(result.returncode, 4, result.stdout + result.stderr)
            self.assertEqual(good.read_text(encoding="utf-8"), TYPO_FIXED)
            self.assertEqual(bad.read_text(encoding="utf-8"), TYPE_ERROR)

    def test_cli_json_reports_fixes(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(tmp, "t.newr", TYPO)
            result = _run_cli("--json", "fix", str(path))
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            payload = json.loads(result.stdout)
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["command"], "fix")
            files = payload["data"]["files"]
            self.assertEqual(len(files), 1)
            self.assertTrue(files[0]["changed"])
            self.assertEqual(len(files[0]["fixes"]), 1)
            self.assertEqual(files[0]["fixes"][0]["code"], "E3001")
            self.assertEqual(files[0]["fixes"][0]["old"], "coun")
            self.assertEqual(files[0]["fixes"][0]["new"], "count")
            self.assertEqual(path.read_text(encoding="utf-8"), TYPO_FIXED)

    def test_cli_json_refusal_is_not_ok(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(tmp, "t.newr", TYPE_ERROR)
            result = _run_cli("--json", "fix", str(path))
            self.assertEqual(result.returncode, 4, result.stdout + result.stderr)
            payload = json.loads(result.stdout)
            self.assertFalse(payload["ok"])
            self.assertEqual(path.read_text(encoding="utf-8"), TYPE_ERROR)


if __name__ == "__main__":
    unittest.main()
