"""Regression tests for the stress-campaign findings (STRESS-REPORT.md).

Round 2 fixes, TDD: written RED first against the unfixed compiler.

- CRASH-1 (HIGH): a file ending in an identifier with no trailing
  newline crashes the lexer with a raw TypeError traceback
  (``_peek()`` returns ``""`` at EOF and ``_is_name_continue`` passes
  it to ``unicodedata.category``). The same root cause also HANGS the
  lexer: ``"" in "<str>"`` is True in Python, so the hex/binary
  digit loops and the whitespace loops never terminate at EOF
  (``print 0xFF`` / ``print 0b10`` with no newline, trailing spaces
  after the last newline, an indented blank line at EOF).
- BUG-2 (MEDIUM): top-level use-before-def silently accepted
  (``print later`` before ``let later = 1`` exits 0), contradicting
  SPEC.md SS8 ("top-level statements execute in file order").
- Underscore placement: ``1__2`` / ``1_`` / ``0x_FF`` lex as valid
  ints (SPEC.md SS6: underscores group digits).
- NUL byte accepted inside string literals (rejected outside).
- Perf cliff: nested ``if`` / blocks go quadratic (~14 s at depth
  3000, ~30 s at ~4060) because the lexer walks the whole file
  character by character.

The ``full()`` helper is the COMBINED pipeline (resolver + core
checker), copied from tests/test_eval_fixes.py so this file stays
independent. The hang tests run the CLI in a subprocess with a
timeout: on RED they fail with "did not return within 10s" instead
of hanging the suite.
"""

import json
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from compiler.checker import check_program
from compiler.discord_check import discord_providers
from compiler.errors import NewrError
from compiler.resolver import resolve_source

ROOT = Path(__file__).resolve().parent.parent
LAUNCHER = ROOT / "newr"
CRASHES = ROOT / "stress" / "crashes"
CORPUS = ROOT / "stress" / "corpus"


def full(src):
    """Parse + resolve + core-check; return COMBINED diagnostics."""
    try:
        resolved = resolve_source(src)
    except NewrError as exc:
        return [exc.diagnostic]
    diags = list(resolved.diagnostics)
    diags += check_program(
        resolved.program,
        providers=discord_providers(resolved.program)).diagnostics
    return diags


def codes(diags):
    return [d.code for d in diags]


def run_cli(*args, timeout=30):
    return subprocess.run([sys.executable, str(LAUNCHER), *args],
                          capture_output=True, text=True, timeout=timeout)


def run_cli_file(src, *args, timeout=10):
    """Write ``src`` (no trailing newline added) to a temp file and run
    the CLI on it. A lexer hang surfaces as a clean assertion failure
    ("did not return within Xs") instead of hanging the suite."""
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "t.newr"
        p.write_text(src, encoding="utf-8")
        try:
            return run_cli("check", str(p), *args, timeout=timeout)
        except subprocess.TimeoutExpired:
            raise AssertionError(
                f"check did not return within {timeout}s (lexer hang?) "
                f"for source {src!r}")


def nested_if(depth):
    lines = []
    for i in range(depth):
        lines.append("  " * i + "if true:")
    lines.append("  " * depth + "print 1")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- CRASH-1: EOF


class TestCrash1EOF(unittest.TestCase):
    def test_file_ending_in_identifier_no_newline(self):
        # CRASH-1 repro: no traceback, a clean diagnostic instead.
        # `print x` is a complete statement; `x` is undefined (E3001).
        self.assertEqual(codes(full("print x")), ["E3001"])

    def test_single_identifier_no_newline(self):
        self.assertEqual(codes(full("x")), ["E3001"])

    def test_non_ascii_identifier_at_eof_no_newline(self):
        # The name guard matters here, not just the regex fast path:
        # an ASCII name at EOF is covered by the regex, but a Unicode
        # name (e.g. the trailing `é` in stress/crashes/
        # fuzz-20261106-1871.newr) takes the per-char loop, which used
        # to pass "" to unicodedata.category and crash.
        self.assertEqual(codes(full("print café")), ["E3001"])
        self.assertEqual(codes(full("print é")), ["E3001"])

    def test_truncated_let_no_newline(self):
        self.assertEqual(codes(full("let x = ")), ["E1001"])

    def test_truncated_fn_no_newline(self):
        # stress/crashes/corpus-b_truncated_fn.newr, inlined. A bare
        # `return` is valid syntax (it returns `none`), so the clean
        # diagnostics are the checker's E2001 (the fn declares
        # `-> int`) plus W1001 for the unused parameter `a` — not a
        # parse error, and definitely not a traceback.
        self.assertEqual(codes(full("fn f(a: int) -> int:\n  return")),
                         ["W1001", "E2001"])

    def test_truncated_let_name_no_newline(self):
        self.assertEqual(codes(full("let abc")), ["E1001"])

    def test_stress_crash_inputs_exit_4_without_traceback(self):
        # The saved crash inputs are the oracle (read-only): each is a
        # truncated file that must now fail cleanly with exit 4.
        inputs = sorted(CRASHES.glob("fuzz-20261106-*.newr"))
        inputs.append(CRASHES / "corpus-b_truncated_fn.newr")
        self.assertEqual(len(inputs), 5)
        for path in inputs:
            with self.subTest(path=path.name):
                r = run_cli("check", str(path))
                self.assertEqual(r.returncode, 4, r.stdout + r.stderr)
                self.assertNotIn("Traceback", r.stdout + r.stderr)

    def test_stress_crash_input_json_envelope(self):
        r = run_cli("check", "--json",
                    str(CRASHES / "corpus-b_truncated_fn.newr"))
        self.assertEqual(r.returncode, 4)
        env = json.loads(r.stdout)
        self.assertFalse(env["ok"])
        # A bare `return` is valid syntax; the fn declares `-> int`
        # but returns `none`, so the clean diagnostic is E2001.
        self.assertEqual(env["error"]["code"], "E2001")


class TestLexerEOFHang(unittest.TestCase):
    """The same EOF root cause as CRASH-1, but as hangs: ``"" in
    "<str>"`` is True, so the hex/binary/whitespace loops spin
    forever at EOF. Each case must return within 10 s."""

    def test_hex_literal_at_eof_returns(self):
        r = run_cli_file("print 0xFF")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_hex_literal_leading_underscore_at_eof_returns(self):
        # Returns (E_BAD_NUMBER after the underscore fix), never hangs.
        r = run_cli_file("print 0x_FF")
        self.assertEqual(r.returncode, 4, r.stdout + r.stderr)

    def test_binary_literal_at_eof_returns(self):
        r = run_cli_file("print 0b10")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_trailing_spaces_after_last_newline_returns(self):
        r = run_cli_file("print 1\n   ")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_indented_blank_line_at_eof_returns(self):
        # `if true:` with only an indented blank line after it: the
        # block is missing, so E1001 — but the lexer must return.
        r = run_cli_file("if true:\n   ")
        self.assertEqual(r.returncode, 4, r.stdout + r.stderr)
        self.assertNotIn("Traceback", r.stdout + r.stderr)


# ------------------------------------------------------- BUG-2: use before def


class TestTopLevelUseBeforeDef(unittest.TestCase):
    def test_print_before_let_is_E2005(self):
        # stress/corpus/i_use_before_def.newr, inlined. The local
        # E2005 pattern is [E2005, W1001] (see test_resolver:
        # test_use_before_definition_is_E2005 with no later use) — the
        # top-level case matches it, it does not invent a new shape.
        self.assertEqual(codes(full("print later\nlet later = 1\n")),
                         ["E2005", "W1001"])

    def test_assign_before_let_is_E2005(self):
        self.assertEqual(codes(full("later = 1\nlet later = 1\n")),
                         ["E2005", "W1001"])

    def test_use_before_let_inside_top_level_if_is_E2005(self):
        self.assertIn("E2005", codes(full(
            "if true:\n  print later\nlet later = 1\n")))

    def test_let_rhs_may_not_use_later_let(self):
        self.assertIn("E2005", codes(full(
            "let a = later\nlet later = 1\n")))

    def test_print_before_const_is_E2005(self):
        self.assertIn("E2005", codes(full("print c\nconst c = 1\n")))

    def test_top_level_let_self_reference_still_E2005(self):
        self.assertIn("E2005", codes(full("let x = x + 1\n")))

    def test_use_after_let_is_ok(self):
        self.assertEqual(codes(full("let later = 1\nprint later\n")), [])

    def test_fn_body_may_forward_reference_later_let(self):
        # fn bodies are declarations: resolved across the whole file
        # (SPEC.md SS8), so a later top-level let is fine here.
        src = ("fn f() -> int:\n"
               "  print later\n"
               "  return 0\n"
               "let later = 1\n"
               "print f()\n")
        self.assertEqual(codes(full(src)), [])

    def test_lambda_body_may_forward_reference_later_let(self):
        # Same deferred-execution rule for a lambda body at top level.
        src = ("let f = fn() -> int:\n"
               "  print later\n"
               "  return 0\n"
               "let later = 1\n"
               "print f()\n")
        self.assertEqual(codes(full(src)), [])

    def test_fn_forward_reference_still_ok(self):
        src = ("print f()\n"
               "fn f() -> int:\n"
               "  return 1\n")
        self.assertEqual(codes(full(src)), [])

    def test_let_rhs_calling_fn_reading_later_let_is_E2005(self):
        # M2 evaluation must-fix #3 (G-M2-3): a top-level `let` whose
        # RHS calls a function reading a LATER `let` was accepted by
        # the checker but died at runtime with a raw `NameError`.
        # The call happens in file order, before the `let` exists.
        src = ("fn getx() -> int:\n"
               "  return x\n"
               "let a = getx()\n"
               "let x = 42\n"
               "print a\n")
        self.assertIn("E2005", codes(full(src)))

    def test_print_calling_fn_reading_later_let_is_E2005(self):
        # Same hole through any top-level statement, not just a let RHS.
        src = ("fn getx() -> int:\n"
               "  return x\n"
               "print getx()\n"
               "let x = 42\n")
        self.assertIn("E2005", codes(full(src)))

    def test_call_inside_top_level_if_reading_later_let_is_E2005(self):
        src = ("fn getx() -> int:\n"
               "  return x\n"
               "if true:\n"
               "  print getx()\n"
               "let x = 42\n")
        self.assertIn("E2005", codes(full(src)))

    def test_indirect_call_chain_reading_later_let_is_E2005(self):
        # The read is transitive: wrap() calls getx(), which reads x.
        src = ("fn getx() -> int:\n"
               "  return x\n"
               "fn wrap() -> int:\n"
               "  return getx()\n"
               "let a = wrap()\n"
               "let x = 42\n"
               "print a\n")
        self.assertIn("E2005", codes(full(src)))

    def test_mutual_recursion_reading_later_let_is_E2005(self):
        src = ("fn a() -> int:\n"
               "  return b()\n"
               "fn b() -> int:\n"
               "  return x\n"
               "let v = a()\n"
               "let x = 42\n"
               "print v\n")
        self.assertIn("E2005", codes(full(src)))

    def test_fn_assigning_to_later_let_called_early_is_E2005(self):
        # A write is a use too: the assignment would be lost (the
        # later `let` re-initialises the name), so the call is
        # rejected just like a read.
        src = ("fn setx() -> int:\n"
               "  x = 5\n"
               "  return 0\n"
               "let a = setx()\n"
               "let x = 42\n"
               "print a\n")
        self.assertIn("E2005", codes(full(src)))

    def test_fn_reading_later_let_called_after_let_is_ok(self):
        src = ("fn getx() -> int:\n"
               "  return x\n"
               "let x = 42\n"
               "print getx()\n")
        self.assertEqual(codes(full(src)), [])

    def test_fn_reading_later_let_never_called_from_top_level_is_ok(self):
        src = ("fn getx() -> int:\n"
               "  return x\n"
               "let x = 42\n"
               "print x\n")
        self.assertEqual(codes(full(src)), [])

    def test_handler_calling_fn_reading_later_let_is_ok(self):
        # Handlers run after module initialisation, so a handler may
        # call a function reading any top-level `let` — even one
        # declared after the handler block.
        src = ("fn getx() -> int:\n"
               "  return x\n"
               "command ping:\n"
               "  description: \"x\"\n"
               "  do:\n"
               "    print getx()\n"
               "let x = 42\n")
        self.assertEqual(codes(full(src)), [])

    def test_stress_corpus_use_before_def_exits_4(self):
        r = run_cli("check", str(CORPUS / "i_use_before_def.newr"))
        self.assertEqual(r.returncode, 4, r.stdout + r.stderr)
        self.assertIn("E2005", r.stdout + r.stderr)


# ------------------------------------------------------- underscore placement


class TestUnderscorePlacement(unittest.TestCase):
    def test_double_underscore_is_E_BAD_NUMBER(self):
        self.assertEqual(codes(full("print 1__2\n")), ["E_BAD_NUMBER"])

    def test_trailing_underscore_is_E_BAD_NUMBER(self):
        self.assertEqual(codes(full("print 1_\n")), ["E_BAD_NUMBER"])

    def test_leading_underscore_hex_is_E_BAD_NUMBER(self):
        self.assertEqual(codes(full("print 0x_FF\n")), ["E_BAD_NUMBER"])

    def test_trailing_underscore_hex_is_E_BAD_NUMBER(self):
        self.assertEqual(codes(full("print 0xFF_\n")), ["E_BAD_NUMBER"])

    def test_double_underscore_binary_is_E_BAD_NUMBER(self):
        self.assertEqual(codes(full("print 0b1__0\n")), ["E_BAD_NUMBER"])

    def test_underscore_before_dot_is_E_BAD_NUMBER(self):
        self.assertEqual(codes(full("print 1_.5\n")), ["E_BAD_NUMBER"])

    def test_trailing_underscore_exponent_is_E_BAD_NUMBER(self):
        self.assertEqual(codes(full("print 1e3_\n")), ["E_BAD_NUMBER"])

    # NOTE: `1._5` is NOT an underscore case: the lexer produces
    # INT(1) DOT IDENT(_5) — a member access on an int literal — and
    # the checker rejects the nonexistent member with E3001. The
    # underscore rule only applies inside a number token, so there is
    # no E_BAD_NUMBER test for it here.

    def test_grouped_underscores_still_ok(self):
        for src in ("print 1_000\n", "print 1_000_000\n",
                    "print 0xF_F\n", "print 0b1_0\n",
                    "print 1_000.5\n", "print 1e1_0\n"):
            with self.subTest(src=src):
                self.assertEqual(codes(full(src)), [])

    def test_explain_mentions_underscores(self):
        r = run_cli("--explain", "E_BAD_NUMBER")
        self.assertEqual(r.returncode, 0)
        self.assertIn("underscore", r.stdout.lower())


# ---------------------------------------------------------------- NUL in string


class TestNulInString(unittest.TestCase):
    def test_raw_nul_in_string_is_E_UNEXPECTED_CHAR(self):
        self.assertEqual(codes(full('print "a\x00b"\n')),
                         ["E_UNEXPECTED_CHAR"])

    def test_nul_escape_still_ok(self):
        # The escape route is the way to write a NUL; it still works.
        self.assertEqual(codes(full('print "a\\u{0}b"\n')), [])

    def test_raw_nul_outside_string_is_E_UNEXPECTED_CHAR(self):
        self.assertEqual(codes(full("print \x00\n")),
                         ["E_UNEXPECTED_CHAR"])

    def test_stress_corpus_nul_byte_exits_4(self):
        r = run_cli("check", str(CORPUS / "b_nul_byte.newr"))
        self.assertEqual(r.returncode, 4, r.stdout + r.stderr)
        self.assertIn("E_UNEXPECTED_CHAR", r.stdout + r.stderr)


# ------------------------------------------------------------------- perf


class TestNestedIfPerf(unittest.TestCase):
    def test_depth_3000_nested_if_rejects_fast(self):
        src = nested_if(3000)
        start = time.monotonic()
        diags = full(src)
        elapsed = time.monotonic() - start
        self.assertIn("E1001", codes(diags))
        self.assertLess(elapsed, 5.0,
                        f"depth-3000 nested if took {elapsed:.1f}s")

    def test_depth_5000_nested_if_clean_diagnostic(self):
        src = nested_if(5000)
        start = time.monotonic()
        diags = full(src)
        elapsed = time.monotonic() - start
        self.assertIn("E1001", codes(diags))
        self.assertLess(elapsed, 20.0,
                        f"depth-5000 nested if took {elapsed:.1f}s")
