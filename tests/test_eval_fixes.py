"""Tests for the M-01..M-11 must-fix issues and the N-07/N-05/N-03/N-06/
N-09/N-10 nice-to-haves from the cold evaluation (EVAL-M0M1.md).

TDD: these tests were written FIRST (RED) against the broken behaviour,
then the compiler was fixed to make them green.

The ``full()`` helper below is deliberately the COMBINED pipeline:
resolver diagnostics + core-checker diagnostics. A test that asserts a
checker verdict must also assert the resolver diagnostics - the M-04
false-green (``test_contracts_first_are_ok`` passed while the verbatim
spec example failed through the CLI, because the old helper ignored
resolver diagnostics) must not recur. See tests/test_checker.py ``check()``,
which now combines the same way.
"""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from compiler.ast_nodes import BinOp, Call, Ternary
from compiler.checker import check_program
from compiler.discord_check import discord_providers
from compiler.errors import NewrError
from compiler.parser import parse
from compiler.resolver import resolve_source

ROOT = Path(__file__).resolve().parent.parent
LAUNCHER = ROOT / "newr"


def full(src):
    """Parse + resolve + core-check; return COMBINED diagnostics.

    A parse error raises NewrError - it is a diagnostic too, and it
    must be returned here, not lost: dropping it would repeat the
    M-04 false-green pattern at the parse stage.
    """
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


def errs(diags):
    return [d for d in diags if d.is_error()]


def run_cli(*args, cwd=None):
    return subprocess.run([sys.executable, str(LAUNCHER), *args],
                          capture_output=True, text=True, cwd=cwd)


def run_cli_bytes(*args, cwd=None):
    return subprocess.run([sys.executable, str(LAUNCHER), *args],
                          capture_output=True, cwd=cwd)


def expr_of(src):
    """The value expression of the first top-level `let` in ``src``."""
    prog = parse(src)
    return prog.stmts[0].value


# ---------------------------------------------------------------- M-01 depth


class TestDepthGuard(unittest.TestCase):
    def test_600_nested_parens_is_E1001_not_crash(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "deep.newr"
            f.write_text("print " + "("*600 + "1" + ")"*600 + "\n",
                         encoding="utf-8")
            r = run_cli("check", str(f))
            self.assertEqual(r.returncode, 4, r.stdout + r.stderr)
            self.assertIn("E1001", r.stdout)
            self.assertNotIn("Traceback", r.stdout + r.stderr)

    def test_600_term_chain_is_E1001_not_crash(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "chain.newr"
            f.write_text("let x = " + " + ".join(["1"]*600) + "\nprint x\n",
                         encoding="utf-8")
            r = run_cli("check", str(f))
            self.assertEqual(r.returncode, 4, r.stdout + r.stderr)
            self.assertIn("E1001", r.stdout)
            self.assertNotIn("Traceback", r.stdout + r.stderr)

    def test_300_nested_ifs_is_E1001_not_crash(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "ifs.newr"
            src = "".join("  "*i + "if true:\n" for i in range(300))
            src += "  "*300 + 'print "deep"\n'
            f.write_text(src, encoding="utf-8")
            r = run_cli("check", str(f))
            self.assertEqual(r.returncode, 4, r.stdout + r.stderr)
            self.assertIn("E1001", r.stdout)
            self.assertNotIn("Traceback", r.stdout + r.stderr)

    def test_deep_input_json_mode_emits_json(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "deep.newr"
            f.write_text("print " + "("*600 + "1" + ")"*600 + "\n",
                         encoding="utf-8")
            r = run_cli("--json", "check", str(f))
            self.assertEqual(r.returncode, 4, r.stdout + r.stderr)
            self.assertNotIn("Traceback", r.stdout + r.stderr)
            envelope = json.loads(r.stdout)
            self.assertFalse(envelope["ok"])
            self.assertEqual(envelope["error"]["code"], "E1001")

    def test_parser_raises_E1001_for_deep_parens(self):
        with self.assertRaises(NewrError) as ctx:
            parse("print " + "("*600 + "1" + ")"*600 + "\n")
        self.assertEqual(ctx.exception.code, "E1001")


# ---------------------------------------------------------------- M-02 non-UTF-8


class TestNonUtf8(unittest.TestCase):
    def test_non_utf8_file_is_exit_2_naming_the_file(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "bad.newr"
            f.write_bytes(b'\xff\xfeprint "x"\n')
            r = run_cli("check", str(f))
            self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
            self.assertIn("bad.newr", r.stderr)
            self.assertNotIn("Traceback", r.stdout + r.stderr)


# ---------------------------------------------------------------- M-03 T? widening


class TestNullableWidening(unittest.TestCase):
    def test_int_value_assigns_to_int_option(self):
        self.assertEqual(errs(full('let x: int? = 5\nprint x\n')), [])

    def test_str_value_assigns_to_str_option(self):
        self.assertEqual(errs(full('let x: str? = "hi"\nprint x\n')), [])

    def test_fn_returning_int_option_may_return_int(self):
        self.assertEqual(
            errs(full('fn f() -> int?:\n  return 5\nprint f()\n')), [])

    def test_or_default_assigns_to_option(self):
        self.assertEqual(errs(full(
            'import env\n'
            'let s: str? = env.get("HOME") or "d"\nprint s\n')), [])

    def test_nullable_to_non_nullable_still_errors(self):
        d = full('let x: int? = 5\nlet y: int = x\nprint y\n')
        self.assertIn("E2001", codes(d))


# ---------------------------------------------------------------- M-04 ensures result


class TestEnsuresResult(unittest.TestCase):
    def test_verbatim_spec_9_7_example_is_clean(self):
        # The exact SPEC.md SS9.7 example, through the COMBINED
        # pipeline (resolver + checker) - the old checker-only helper
        # passed this while the CLI failed it with E3001.
        d = full('fn withdraw(balance: int, amount: int) -> int:\n'
                 '  requires amount > 0\n'
                 '  requires amount <= balance\n'
                 '  ensures result >= 0\n'
                 '  return balance - amount\n')
        self.assertEqual(errs(d), [])

    def test_result_outside_ensures_is_undefined(self):
        d = full('fn f() -> int:\n  print result\n  return 1\n')
        self.assertIn("E3001", codes(d))

    def test_ensures_result_through_cli(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "w.newr"
            f.write_text(
                'fn withdraw(balance: int, amount: int) -> int:\n'
                '  requires amount > 0\n'
                '  ensures result >= 0\n'
                '  return balance - amount\n', encoding="utf-8")
            r = run_cli("check", str(f))
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)


# ---------------------------------------------------------------- M-05 hex/binary overflow


class TestHexBinaryOverflow(unittest.TestCase):
    def test_hex_above_int64_max_is_E_INT_OVERFLOW(self):
        d = full('let x = 0x8000000000000000\nprint x\n')
        self.assertIn("E_INT_OVERFLOW", codes(d))

    def test_hex_huge_is_E_INT_OVERFLOW(self):
        d = full('let x = 0xFFFFFFFFFFFFFFFFFF\nprint x\n')
        self.assertIn("E_INT_OVERFLOW", codes(d))

    def test_65_bit_binary_is_E_INT_OVERFLOW(self):
        d = full('let x = 0b' + '1'*65 + '\nprint x\n')
        self.assertIn("E_INT_OVERFLOW", codes(d))

    def test_hex_int64_max_is_ok(self):
        self.assertEqual(
            errs(full('let x = 0x7FFFFFFFFFFFFFFF\nprint x\n')), [])


# ---------------------------------------------------------------- M-06 CRLF offsets


class TestCrlfOffsets(unittest.TestCase):
    def test_crlf_json_offsets_match_on_disk_bytes(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "crlf.newr"
            raw = b'print "a"\r\nlet y: int = "s"\r\nprint y\r\n'
            f.write_bytes(raw)
            r = run_cli("check", "--error-format=json", str(f))
            self.assertEqual(r.returncode, 4, r.stdout + r.stderr)
            diags = json.loads(r.stdout)
            self.assertEqual(len(diags), 1)
            # The offending token is the `"s"` literal on line 2; its
            # on-disk byte offset counts the CRLF on line 1.
            want = raw.index(b'"s"')
            self.assertEqual(diags[0]["start_byte"], want)
            self.assertEqual(diags[0]["line"], 2)


# ---------------------------------------------------------------- M-07 lone CR


class TestLoneCr(unittest.TestCase):
    def test_lone_cr_is_E_BAD_LINE_ENDING_through_cli(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "cr.newr"
            f.write_bytes(b'print "a"\rprint "b"\n')
            r = run_cli("check", str(f))
            self.assertEqual(r.returncode, 4, r.stdout + r.stderr)
            self.assertIn("E_BAD_LINE_ENDING", r.stdout)
            self.assertNotIn("Traceback", r.stdout + r.stderr)


# ---------------------------------------------------------------- M-08 int64 min


class TestInt64Min(unittest.TestCase):
    def test_negated_int64_min_is_ok(self):
        self.assertEqual(
            errs(full('let x = -9223372036854775808\nprint x\n')), [])

    def test_parenthesised_negated_int64_min_is_ok(self):
        self.assertEqual(
            errs(full('let x = -(9223372036854775808)\nprint x\n')), [])

    def test_positive_int64_min_magnitude_is_overflow(self):
        d = full('let x = 9223372036854775808\nprint x\n')
        self.assertIn("E_INT_OVERFLOW", codes(d))

    def test_subtraction_does_not_sneak_int64_min_in(self):
        d = full('let x = 0 - 9223372036854775808\nprint x\n')
        self.assertIn("E_INT_OVERFLOW", codes(d))

    def test_negated_hex_int64_min_is_ok(self):
        self.assertEqual(
            errs(full('let x = -0x8000000000000000\nprint x\n')), [])


# ---------------------------------------------------------------- M-09 ?? RHS


class TestCoalesceNullableRhs(unittest.TestCase):
    def test_option_coalesce_option_is_option(self):
        self.assertEqual(errs(full(
            'let a: str? = none\nlet b: str? = "hi"\n'
            'let c: str? = a ?? b\nprint c\n')), [])

    def test_option_chain_coalesces_to_plain(self):
        self.assertEqual(errs(full(
            'let a: str? = none\nlet b: str? = "hi"\n'
            'let c = a ?? b ?? "d"\nprint c\n')), [])

    def test_parenthesised_option_chain_is_ok(self):
        self.assertEqual(errs(full(
            'let a: str? = none\nlet b: str? = "hi"\n'
            'let c = a ?? (b ?? "d")\nprint c\n')), [])

    def test_coalesce_with_none_rhs_is_option(self):
        self.assertEqual(errs(full(
            'let a: str? = none\nlet c: str? = a ?? none\nprint c\n')),
            [])

    def test_coalesce_mismatched_rhs_is_E2001(self):
        d = full('let a: str? = none\nlet c = a ?? 5\nprint c\n')
        self.assertIn("E2001", codes(d))


# ---------------------------------------------------------------- M-10 ?. receiver


class TestOptionalChainReceiver(unittest.TestCase):
    def test_optional_chain_on_non_nullable_field_is_E2001(self):
        d = full('let s = "hi"\nprint s?.upper()\n')
        self.assertIn("E2001", codes(d))

    def test_optional_chain_on_non_nullable_method_is_E2001(self):
        d = full('let xs = [1, 2]\nprint xs?.get(0, 9)\n')
        self.assertIn("E2001", codes(d))

    def test_optional_chain_on_nullable_is_ok(self):
        self.assertEqual(errs(full(
            'let s: str? = "hi"\nprint s?.upper()\n')), [])

    def test_plain_access_on_non_nullable_is_ok(self):
        self.assertEqual(errs(full('let s = "hi"\nprint s.upper()\n')),
                         [])


# ---------------------------------------------------------------- M-11 explain


class TestExplainLexerCodes(unittest.TestCase):
    def test_explain_all_lexer_codes(self):
        for code in ("E_BAD_ESCAPE", "E_BAD_INDENT", "E_BAD_LINE_ENDING",
                     "E_BAD_NUMBER", "E_INT_OVERFLOW", "E_STRAY_BRACE",
                     "E_TAB_INDENT", "E_UNEXPECTED_CHAR",
                     "E_UNTERMINATED_STRING", "E1001"):
            r = run_cli("explain", code)
            self.assertEqual(r.returncode, 0, code)
            self.assertIn(code, r.stdout)

    def test_error_index_matches_explain(self):
        """M-11/M-13: docs/error-index.md and EXPLAIN must list exactly
        the same codes - a code documented but not explainable (or
        vice versa) is a spec/code drift."""
        import re

        from compiler.cli import EXPLAIN

        index = (ROOT / "docs" / "error-index.md").read_text(
            encoding="utf-8")
        indexed = set(re.findall(r"`(E\d+|E_[A-Z_]+|W\d+)`", index))
        self.assertEqual(indexed, set(EXPLAIN),
                         f"index-only: {sorted(indexed - set(EXPLAIN))}; "
                         f"explain-only: {sorted(set(EXPLAIN) - indexed)}")
        self.assertEqual(len(indexed), 38)


# ---------------------------------------------------------------- N-03 precedence


class TestPrecedenceTable(unittest.TestCase):
    def test_dead_binary_bp_dict_is_gone(self):
        import compiler.parser as p
        self.assertFalse(hasattr(p, "_BINARY_BP"))

    def test_precedence_table_matches_spec_9_2(self):
        import compiler.parser as p
        # (kind, value) -> binding power; SPEC.md SS9.2, loosest first.
        want = {
            ("PIPE", None): 1, ("QQ", None): 2,
            ("KEYWORD", "or"): 3, ("KEYWORD", "and"): 4,
            ("EQ", None): 5, ("NEQ", None): 5, ("LT", None): 5,
            ("LTE", None): 5, ("GT", None): 5, ("GTE", None): 5,
            ("KEYWORD", "in"): 5,
            ("PLUS", None): 6, ("MINUS", None): 6,
            ("STAR", None): 7, ("SLASH", None): 7, ("PERCENT", None): 7,
            ("POWER", None): 8,
        }
        self.assertEqual(dict(p.PRECEDENCE), want)


# ---------------------------------------------------------------- N-05 messages


class TestDiagnosticsMessages(unittest.TestCase):
    def test_import_cycle_reports_only_E3004(self):
        with tempfile.TemporaryDirectory() as d:
            a = Path(d) / "a.newr"
            b = Path(d) / "b.newr"
            a.write_text('import "b.newr"\nprint "a"\n', encoding="utf-8")
            b.write_text('import "a.newr"\nprint "b"\n', encoding="utf-8")
            r = run_cli("check", str(a))
            self.assertEqual(r.returncode, 4, r.stdout + r.stderr)
            self.assertIn("E3004", r.stdout)
            self.assertNotIn("E3006", r.stdout)
            self.assertNotIn("W1002", r.stdout)

    def test_option_type_hint_points_at_nullable(self):
        d = full('let x: option[int] = none\nprint x\n')
        self.assertIn("E3001", codes(d))
        e = [x for x in d if x.code == "E3001"][0]
        self.assertIn("T?", e.help or "")

    def test_case_mismatch_import_names_on_disk_file(self):
        with tempfile.TemporaryDirectory() as d:
            main = Path(d) / "main.newr"
            (Path(d) / "Helper.newr").write_text(
                'pub fn hi() -> int:\n  return 1\n', encoding="utf-8")
            main.write_text('import "helper.newr"\nprint Helper.hi()\n',
                            encoding="utf-8")
            r = run_cli("check", str(main))
            self.assertEqual(r.returncode, 4, r.stdout + r.stderr)
            self.assertIn("E3008", r.stdout)
            self.assertIn("Helper.newr", r.stdout)

    def test_let_log_is_E3007_not_E1001(self):
        d = full('let log = 1\nprint log\n')
        self.assertEqual(codes(d), ["E3007"])

    def test_dangling_question_mark_names_the_real_problem(self):
        d = full('let x = 1 ?\n')
        self.assertIn("E2001", codes(d))
        e = [x for x in d if x.code == "E2001"][0]
        self.assertIn("Result", (e.message or "") + (e.help or ""))

    def test_print_call_with_question_mark_gets_hint(self):
        d = full('fn g() -> int:\n  return 1\nprint g()?\n')
        self.assertIn("E1001", codes(d))
        e = [x for x in d if x.code == "E1001"][0]
        self.assertIn("?", e.help or "")


# ---------------------------------------------------------------- N-06 --fast


class TestFastHelp(unittest.TestCase):
    def test_fast_help_names_discord_validation(self):
        r = run_cli("help", "check")
        self.assertEqual(r.returncode, 0)
        self.assertIn("Discord", r.stdout)

    def test_check_fast_skips_type_errors(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "bad.newr"
            f.write_text('let x: float = 5\nprint x\n', encoding="utf-8")
            r = run_cli("check", "--fast", str(f))
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertIn("not verified", r.stderr)


# ---------------------------------------------------------------- N-07 op identity


class TestOpIdentity(unittest.TestCase):
    def test_every_binary_op_is_recorded(self):
        cases = {
            "a + b": "+", "a - b": "-", "a * b": "*", "a / b": "/",
            "a % b": "%", "a ** b": "**", "a == b": "==", "a != b": "!=",
            "a < b": "<", "a <= b": "<=", "a > b": ">", "a >= b": ">=",
            "a and b": "and", "a or b": "or", "a in b": "in",
            "a ?? b": "??",
        }
        for src_expr, want_op in cases.items():
            node = expr_of(f"let x = {src_expr}\n")
            self.assertIsInstance(node, BinOp, src_expr)
            self.assertEqual(node.op, want_op, src_expr)

    def test_pipeline_desugars_to_call_with_piped_first_arg(self):
        node = expr_of("let x = a |> f\n")
        self.assertIsInstance(node, Call)
        self.assertEqual(len(node.args), 1)

    def test_ternary_node_is_built(self):
        node = expr_of("let x = a ? b : c\n")
        self.assertIsInstance(node, Ternary)


# ---------------------------------------------------------------- N-09/N-10 stdlib


class TestStdlibReconciliation(unittest.TestCase):
    def test_input_with_no_args_is_ok(self):
        self.assertEqual(errs(full('let s = input()\nprint s\n')), [])

    def test_list_len_method_is_ok(self):
        self.assertEqual(errs(full('let xs = [1, 2]\nprint xs.len()\n')),
                         [])

    def test_time_format_extra_formats_are_ok(self):
        self.assertEqual(errs(full(
            'import time\n'
            'print time.format(0, "%d/%m/%Y")\n'
            'print time.format(0, "%H:%M:%S")\n')), [])

    def test_named_module_without_import_is_checked(self):
        # math.sqrt takes a float: a str argument must be an error even
        # with no `import math` in the program.
        d = full('print math.sqrt("x")\n')
        self.assertIn("E2001", codes(d))
        self.assertEqual(errs(full('print math.sqrt(4.0)\n')), [])


# ---------------------------------------------------------------- CRLF via CLI


class TestCrlfCli(unittest.TestCase):
    def test_crlf_source_checks_clean(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "crlf.newr"
            f.write_bytes(b'print "Hello, world!"\r\n')
            r = run_cli("check", str(f))
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)


if __name__ == "__main__":
    unittest.main()
