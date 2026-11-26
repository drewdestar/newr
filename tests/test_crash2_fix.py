"""Regression tests for CRASH-2 (STRESS-REPORT-2.md §4).

A malformed ``t(...)`` call crashes the parser with a raw
``AttributeError: 'Span' object has no attribute 'span'`` instead of
the intended clean E1001 diagnostic: ``_parse_call`` in
``compiler/parser.py`` passes a ``Span`` (``func.span`` /
``args[1].span``) to ``self.err()``, which expects a ``Token`` and
reads ``tok.span``. Pre-existing (parser.py byte-identical to round
1); round 1's fuzzer never emitted ``t(...)`` calls, so it was only
reached by the fresh fuzz seed 20261107 (case 1429: a mutated
``shop.newr`` where deleting the ``:`` after ``item`` turned a named
argument into a second positional argument).

TDD: written RED first against the unfixed compiler - the ``full()``
tests raise the raw AttributeError (not NewrError) and the CLI tests
see exit 1 with a traceback instead of exit 4 with ``error[E1001]``.

The ``full()`` helper is the COMBINED pipeline (resolver + core
checker), copied from tests/test_stress_fixes.py so this file stays
independent.
"""

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from compiler.checker import check_program
from compiler.discord_check import discord_providers
from compiler.errors import NewrError
from compiler.resolver import resolve_source

ROOT = Path(__file__).resolve().parent.parent
LAUNCHER = ROOT / "newr"
CRASHES = ROOT / "stress" / "crashes"


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


class TestTCrashSecondPositional(unittest.TestCase):
    """``print t("k", 1)`` - a second positional arg after the key."""

    def test_second_positional_is_E1001(self):
        self.assertEqual(codes(full('print t("k", 1)\n')), ["E1001"])

    def test_second_positional_message_names_the_rule(self):
        diags = full('print t("k", 1)\n')
        self.assertEqual(len(diags), 1)
        self.assertIn("named arguments", diags[0].message)

    def test_second_positional_span_points_at_second_arg(self):
        # `print t("k", 1)`: the `1` starts at byte 13.
        diags = full('print t("k", 1)\n')
        self.assertEqual(len(diags), 1)
        self.assertEqual(diags[0].span.start_byte, 13)
        self.assertEqual(diags[0].span.end_byte, 14)

    def test_minimized_repro_exits_4_with_clean_E1001(self):
        r = run_cli("check",
                    str(CRASHES / "min-crash-t-call-2pos.newr"))
        out = r.stdout + r.stderr
        self.assertEqual(r.returncode, 4, out)
        self.assertIn("error[E1001]", out)
        self.assertNotIn("Traceback", out)
        self.assertNotIn("AttributeError", out)


class TestTCrashNonStringKey(unittest.TestCase):
    """``print t(1)`` - a non-string first argument."""

    def test_non_string_key_is_E1001(self):
        self.assertEqual(codes(full("print t(1)\n")), ["E1001"])

    def test_non_string_key_message_names_the_rule(self):
        diags = full("print t(1)\n")
        self.assertEqual(len(diags), 1)
        self.assertIn("string key", diags[0].message)

    def test_non_string_key_span_points_at_t(self):
        # `print t(1)`: the `t` name starts at byte 6.
        diags = full("print t(1)\n")
        self.assertEqual(len(diags), 1)
        self.assertEqual(diags[0].span.start_byte, 6)
        self.assertEqual(diags[0].span.end_byte, 7)

    def test_minimized_repro_exits_4_with_clean_E1001(self):
        r = run_cli("check",
                    str(CRASHES / "min-crash-t-call-badkey.newr"))
        out = r.stdout + r.stderr
        self.assertEqual(r.returncode, 4, out)
        self.assertIn("error[E1001]", out)
        self.assertNotIn("Traceback", out)
        self.assertNotIn("AttributeError", out)

    def test_fuzz_source_1429_exits_4_with_clean_E1001(self):
        r = run_cli("check", str(CRASHES / "fuzz-20261107-1429.newr"))
        out = r.stdout + r.stderr
        self.assertEqual(r.returncode, 4, out)
        self.assertIn("error[E1001]", out)
        self.assertNotIn("Traceback", out)
        self.assertNotIn("AttributeError", out)


class TestTCallControlCases(unittest.TestCase):
    """Inputs that must keep their current (clean) behaviour."""

    def test_named_arg_after_key_is_clean_in_core_pipeline(self):
        # NOTE: the E2009 for the unknown key `k` comes from the
        # Discord checker (compiler/discord_check.py), which the
        # in-process `full()` helper does not run - it runs resolver
        # + core checker only. The core pipeline must report nothing
        # for a well-formed `t()` call; the CLI test below pins the
        # E2009 end to end.
        self.assertEqual(codes(full('print t("k", item: 1)\n')), [])

    def test_named_arg_after_key_cli_exits_4(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "t.newr"
            p.write_text('print t("k", item: 1)\n', encoding="utf-8")
            r = run_cli("check", str(p))
        out = r.stdout + r.stderr
        self.assertEqual(r.returncode, 4, out)
        self.assertIn("error[E2009]", out)
        self.assertNotIn("Traceback", out)

    def test_valid_t_call_still_ok(self):
        src = ('strings en:\n'
               '  "buy.ok": "Bought {item}."\n'
               'print t("buy.ok", item: "sword")\n')
        self.assertEqual(codes(full(src)), [])

    def test_t_call_with_no_args_is_E1001(self):
        # `t()` with no key at all hits the same error path
        # (``func.span``) - it must also be a clean E1001.
        self.assertEqual(codes(full("print t()\n")), ["E1001"])


if __name__ == "__main__":
    unittest.main()
