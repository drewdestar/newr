"""Formatter tests (SPEC.md section 19, M6 Phase 2a).

`newr fmt` rewrites a program in the canonical form: 2-space indent,
single spaces around operators, literals and durations keep their
source spelling, `#` comments are preserved, `print` is written
without parentheses, and the file ends with exactly one newline.
Formatting is idempotent, and a formatted program parses to the same
AST (spans aside) as the original.
"""

import json
import subprocess
import sys
import tempfile
import unittest
from dataclasses import fields, is_dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from compiler.errors import Span  # noqa: E402
from compiler.fmt import (  # noqa: E402
    format_program,
    format_source,
    is_formatted,
)
from compiler.parser import parse  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
LAUNCHER = ROOT / "newr"

# Files whose source cannot round-trip byte-for-byte through fmt, with
# the reason. They are still idempotent and AST-identical after fmt.
# - pipeline.newr / fn_pipeline_chain.newr: the parser desugars
#   `x |> f` to the call `f(x)` at parse time (compiler/parser.py),
#   so the AST keeps no record of `|>` and fmt emits the call form.
#   The files stay in pipeline form to keep `|>` conformance coverage.
CORPUS_EXCEPTIONS = {
    "conformance/ok/pipeline.newr",
    "conformance/ok/fn_pipeline_chain.newr",
}


def _strip_spans(node):
    """A copy of an AST with every Span replaced by None."""
    if isinstance(node, Span):
        # Spans hide inside tuples too: a call's kwargs entries are
        # (name, value, span), a try's catches are (name, span,
        # body). The tuple branch below recurses element by element,
        # and without this check a Span element would be copied
        # instead of cleared, because Span itself is a dataclass.
        return None
    if is_dataclass(node) and not isinstance(node, type):
        values = {}
        deferred = {}
        for f in fields(node):
            value = getattr(node, f.name)
            stripped = _strip_spans(value)
            # init=False fields (e.g. Node.doc, the `///` doc comment)
            # are not constructor parameters: set them after.
            if f.init:
                values[f.name] = stripped
            else:
                deferred[f.name] = stripped
        copy = type(node)(**values)
        for name, stripped in deferred.items():
            setattr(copy, name, stripped)
        return copy
    if isinstance(node, list):
        return [_strip_spans(x) for x in node]
    if isinstance(node, tuple):
        return tuple(_strip_spans(x) for x in node)
    if isinstance(node, dict):
        return {k: _strip_spans(v) for k, v in node.items()}
    return node


def _corpus_files():
    files = sorted((ROOT / "examples").glob("*.newr"))
    files += sorted((ROOT / "conformance" / "ok").rglob("*.newr"))
    return files


def _run_cli(*argv):
    return subprocess.run(
        [sys.executable, str(LAUNCHER), *argv],
        capture_output=True, text=True)


class FormatTestCase(unittest.TestCase):
    """Helpers shared by the formatter unit tests."""

    def assert_formats_to(self, source, expected):
        self.assertEqual(format_source(source, "<test>"), expected)

    def assert_roundtrip(self, source):
        """fmt keeps the AST (spans aside) and is idempotent."""
        formatted = format_source(source, "<test>")
        self.assertEqual(_strip_spans(parse(source, "<test>")),
                         _strip_spans(parse(formatted, "<test>")))
        self.assertEqual(format_source(formatted, "<test>"), formatted)


class TestExpressions(FormatTestCase):

    def test_binary_spacing_and_precedence(self):
        self.assert_formats_to("let x = 1+2*3\n", "let x = 1 + 2 * 3\n")
        self.assert_formats_to("let x = (1+2)*3\n",
                               "let x = (1 + 2) * 3\n")
        self.assert_formats_to("let x = 1*(2+3)\n",
                               "let x = 1 * (2 + 3)\n")
        self.assert_formats_to("let x = a**b**c\n", "let x = a ** b ** c\n")
        self.assert_formats_to("let x = (a**b)**c\n",
                               "let x = (a ** b) ** c\n")

    def test_comparisons_are_non_associative(self):
        self.assert_formats_to("let x = (a==b)==c\n",
                               "let x = (a == b) == c\n")
        self.assert_formats_to("let x = a==(b==c)\n",
                               "let x = a == (b == c)\n")
        self.assert_formats_to("let x = a<b and b<c\n",
                               "let x = a < b and b < c\n")

    def test_unary_minus_binding(self):
        # `-a + b` is `-(a + b)` in newr (SPEC section 9.2): no parens
        # are added, and none are needed, as the left child of `!=`.
        self.assert_formats_to("if -a+b != -5:\n  pass\n",
                               "if -a + b != -5:\n  pass\n")
        self.assert_formats_to("if -7/2 != -3:\n  pass\n",
                               "if -7 / 2 != -3:\n  pass\n")
        # But `(-a) + b` is a different AST and keeps its parens.
        self.assert_formats_to("let x = (-a)+b\n", "let x = (-a) + b\n")
        # `-(a + b)` normalizes to the same AST's short spelling.
        self.assert_formats_to("let x = -(a+b)\n", "let x = -a + b\n")
        self.assert_formats_to("let x = -a**2\n", "let x = -a ** 2\n")

    def test_not_binding(self):
        self.assert_formats_to("let x = not(a and b)\n",
                               "let x = not a and b\n")
        self.assert_formats_to("let x = not a==b\n",
                               "let x = not a == b\n")
        # `(not a) and b` is a different AST and keeps its parens.
        self.assert_formats_to("let x = (not a) and b\n",
                               "let x = (not a) and b\n")
        self.assert_formats_to("let x = not(a or b)\n",
                               "let x = not a or b\n")

    def test_coalesce_mixing(self):
        self.assert_formats_to("let x = (a??b) and c\n",
                               "let x = (a ?? b) and c\n")
        self.assert_formats_to("let x = a and (b??c)\n",
                               "let x = a and (b ?? c)\n")
        self.assert_formats_to("let x = a??b\n", "let x = a ?? b\n")

    def test_ternary(self):
        self.assert_formats_to("let x = a?b:c\n", "let x = a ? b : c\n")
        self.assert_formats_to("let x = (a?b:c)+d\n",
                               "let x = (a ? b : c) + d\n")
        self.assert_formats_to("let x = a+(b?c:d)\n",
                               "let x = a + (b ? c : d)\n")
        self.assert_formats_to("let x = (a?b:c)?d:e\n",
                               "let x = (a ? b : c) ? d : e\n")

    def test_lambda_and_try_as_operands(self):
        self.assert_formats_to("let x = (fn(n: int): n+1)(2)\n",
                               "let x = (fn(n: int): n + 1)(2)\n")
        self.assert_formats_to("let x = (try f())+1\n",
                               "let x = (try f()) + 1\n")
        self.assert_formats_to("let x = (a+b).x\n",
                               "let x = (a + b).x\n")

    def test_pipeline_is_a_call_in_the_ast(self):
        # The parser desugars `x |> f` to `f(x)`; fmt emits the call.
        self.assert_formats_to("let y = 5 |> double\n",
                               "let y = double(5)\n")
        self.assert_formats_to("let y = 5 |> double |> inc\n",
                               "let y = inc(double(5))\n")

    def test_print_has_no_parens(self):
        self.assert_formats_to('print("x")\n', 'print "x"\n')
        self.assert_formats_to('print()\n', 'print\n')
        self.assert_formats_to('print(a,b)\n', 'print a, b\n')
        self.assert_formats_to('print "x"\n', 'print "x"\n')

    def test_calls_members_indexes(self):
        self.assert_formats_to("let x = f(a,b,c: 1)\n",
                               "let x = f(a, b, c: 1)\n")
        self.assert_formats_to("let x = a.b.c\n", "let x = a.b.c\n")
        self.assert_formats_to("let x = a?.b\n", "let x = a?.b\n")
        self.assert_formats_to("let x = a[0]\n", "let x = a[0]\n")
        self.assert_formats_to("let x = [1,2,3]\n", "let x = [1, 2, 3]\n")
        self.assert_formats_to('let x = {"a": 1}\n',
                               'let x = {"a": 1}\n')
        self.assert_formats_to("let x = []\n", "let x = []\n")
        self.assert_formats_to("let x = {}\n", "let x = {}\n")

    def test_literals_keep_source_spelling(self):
        self.assert_formats_to("let x = 0x10\n", "let x = 0x10\n")
        self.assert_formats_to("let x = 1_000\n", "let x = 1_000\n")
        self.assert_formats_to("let x = 1e3\n", "let x = 1e3\n")
        self.assert_formats_to("let x = 0b101\n", "let x = 0b101\n")
        self.assert_formats_to("let x = 2.0\n", "let x = 2.0\n")

    def test_strings_keep_source_spelling(self):
        self.assert_formats_to('let s = "a\\nb\\"c\\\\d"\n',
                               'let s = "a\\nb\\"c\\\\d"\n')
        self.assert_formats_to('let s = "a {{b}} {c}"\n',
                               'let s = "a {{b}} {c}"\n')
        self.assert_formats_to('let s = "{a} and {b}"\n',
                               'let s = "{a} and {b}"\n')

    def test_block_lambda(self):
        source = ("let f = fn(x: int) -> int:\n"
                  "  return x + 1\n"
                  "let y = f(2)\n")
        self.assert_formats_to(source, source)
        self.assert_roundtrip(source)

    def test_embed(self):
        source = ('let e = embed:\n'
                  '  title: "Hi"\n'
                  '  field "Name": "Value" (inline: true)\n')
        self.assert_formats_to(source, source)
        self.assert_roundtrip(source)

    def test_propagate(self):
        self.assert_formats_to("let x = f()?\n", "let x = f()?\n")
        self.assert_roundtrip("let x = f()?\n")


class TestStatements(FormatTestCase):

    def test_if_elif_else(self):
        source = ("if a:\n"
                  "  print \"a\"\n"
                  "elif b:\n"
                  "  print \"b\"\n"
                  "else:\n"
                  "  print \"c\"\n")
        self.assert_formats_to(source, source)
        self.assert_roundtrip(source)

    def test_for_while(self):
        source = ("for x in xs:\n"
                  "  print x\n"
                  "while n > 0:\n"
                  "  n = n - 1\n")
        self.assert_formats_to(source, source)
        self.assert_roundtrip(source)

    def test_match_patterns(self):
        source = ("match x:\n"
                  "  1:\n"
                  '    print "one"\n'
                  "  Point(x: a, y: b) if a > 0:\n"
                  '    print "point"\n'
                  "  Shape.Circle(radius: r):\n"
                  '    print "circle"\n'
                  "  [a, b]:\n"
                  '    print "pair"\n'
                  "  2 | 3:\n"
                  '    print "two or three"\n'
                  "  _:\n"
                  '    print "other"\n')
        self.assert_formats_to(source, source)
        self.assert_roundtrip(source)

    def test_try_catch_finally(self):
        source = ("try:\n"
                  "  f()\n"
                  "catch e:\n"
                  "  print e\n"
                  "finally:\n"
                  "  print \"done\"\n")
        self.assert_formats_to(source, source)
        self.assert_roundtrip(source)

    def test_contracts_have_no_colon(self):
        source = ("fn twice(n: int) -> int:\n"
                  "  requires n > 0\n"
                  "  ensures result > n\n"
                  "  return n * 2\n")
        self.assert_formats_to(source, source)
        self.assert_roundtrip(source)

    def test_control_keywords(self):
        source = ("fn f() -> int:\n"
                  "  return 1\n"
                  "  throw \"x\"\n"
                  "  break\n"
                  "  continue\n"
                  "  pass\n"
                  "  defer\n")
        self.assert_formats_to(source, source)
        self.assert_roundtrip(source)

    def test_reply_send_log(self):
        source = ("reply \"hi\" ephemeral\n"
                  "send \"hi\" to channel\n"
                  "log \"hi\"\n"
                  "edit \"hi\"\n"
                  "followup \"hi\"\n")
        self.assert_formats_to(source, source)
        self.assert_roundtrip(source)


class TestDeclarations(FormatTestCase):

    def test_struct_enum_fn_import(self):
        source = ('import "other.newr"\n'
                  "import math\n"
                  "import math as m\n"
                  "\n"
                  "pub struct Point:\n"
                  "  x: int\n"
                  "  y: int\n"
                  "\n"
                  "enum Shape:\n"
                  "  Circle(radius: float)\n"
                  "  Square\n"
                  "\n"
                  "pub fn area(s: Shape) -> float:\n"
                  "  return 0.0\n")
        self.assert_formats_to(source, source)
        self.assert_roundtrip(source)

    def test_struct_literal_kwargs_roundtrip(self):
        # _strip_spans must clear the spans inside kwargs tuples:
        # formatting `Count(n:0, m:1)` inserts a space after the
        # first colon, shifting the second kwarg name one byte, and
        # the stripped parses must still be equal. Before the fix
        # the kwarg name spans survived the strip (Span is a
        # dataclass, so the tuple branch copied them), and the
        # roundtrip failed on that 1-byte shift.
        source = ("struct Count:\n"
                  "  n: int\n"
                  "  m: int\n"
                  "\n"
                  "let c = Count(n:0, m:1)\n")
        self.assert_formats_to(
            source,
            source.replace("Count(n:0, m:1)", "Count(n: 0, m: 1)"))
        self.assert_roundtrip(source)
        self.assert_roundtrip(
            "struct Count:\n"
            "  n: int\n"
            "\n"
            "let c=Count(n: 0)\n")

    def test_bot_command_event_task(self):
        source = ('bot "MyBot":\n'
                  '  description: "A bot"\n'
                  "  intents: [guilds, members]\n"
                  "\n"
                  "command ping:\n"
                  '  description: "Ping"\n'
                  "  option name: str (required, default: \"x\", "
                  "choices: [\"a\", \"b\"], min: 1, max: 5, "
                  'description: "The name")\n'
                  '  reply: "Pong {name}"\n'
                  "  ephemeral: true\n"
                  "\n"
                  "command shout:\n"
                  "  do:\n"
                  '    print "shout"\n'
                  "\n"
                  "event ready:\n"
                  '  log: "ready"\n'
                  "\n"
                  "event message:\n"
                  '  reply: "hi"\n'
                  "\n"
                  "event member_join:\n"
                  "  do:\n"
                  '    print "bye"\n'
                  "\n"
                  "task every 60s:\n"
                  "  do:\n"
                  '    log "tick"\n')
        self.assert_formats_to(source, source)
        self.assert_roundtrip(source)

    def test_task_duration_spelling_is_kept(self):
        self.assert_formats_to("task every 1h:\n  do:\n    log \"x\"\n",
                               "task every 1h:\n  do:\n    log \"x\"\n")
        self.assert_formats_to("task every 90s:\n  do:\n    log \"x\"\n",
                               "task every 90s:\n  do:\n    log \"x\"\n")

    def test_strings_templates(self):
        # A template's `{{literal}}` spelling is recorded by the
        # parser as the placeholder `literal`, so fmt normalizes it
        # to the single-brace spelling (same AST, same placeholders).
        source = ('strings en:\n'
                  '  "hello": "Hello {name}"\n'
                  '  "brace": "A {{literal}} brace"\n')
        expected = ('strings en:\n'
                    '  "hello": "Hello {name}"\n'
                    '  "brace": "A {literal} brace"\n')
        self.assert_formats_to(source, expected)
        self.assert_roundtrip(source)

    def test_store(self):
        source = ("store economy:\n"
                  "  table balances (user: str, balance: int)\n"
                  "  ledger true\n")
        self.assert_formats_to(source, source)
        self.assert_roundtrip(source)

    def test_components(self):
        source = ('button "Click" (style: primary, id: "click", '
                  "timeout: 60s):\n"
                  "  only: author\n"
                  "  do:\n"
                  '    print "clicked"\n'
                  "  on_timeout:\n"
                  '    print "too slow"\n'
                  "\n"
                  'select "Pick" (options: ["a", "b"], min: 1, '
                  "max: 2, timeout: 30s):\n"
                  "  only: roles([\"admin\"])\n"
                  "  do:\n"
                  '    print "picked"\n'
                  "\n"
                  'modal "Form" (id: "form"):\n'
                  '  text_input "Name" (style: short, required, '
                  "min_length: 1, max_length: 50):\n"
                  "  do:\n"
                  '    print "submitted"\n')
        self.assert_formats_to(source, source)
        self.assert_roundtrip(source)

    def test_ephemeral_false_and_ledger_false_are_omitted(self):
        self.assert_formats_to("command ping:\n"
                               '  reply: "pong"\n',
                               "command ping:\n"
                               '  reply: "pong"\n')
        self.assert_formats_to("store s:\n"
                               "  table t (a: int)\n",
                               "store s:\n"
                               "  table t (a: int)\n")


class TestTestBlocks(FormatTestCase):
    """`test "name":` blocks (SPEC.md section 8.7, M6 Phase 2c)."""

    def test_block_formats_canonically(self):
        source = ('test "adds two numbers":\n'
                  "  requires 1 + 1 == 2\n")
        self.assert_formats_to(source, source)
        self.assert_roundtrip(source)

    def test_mixed_fn_and_block_order_is_kept(self):
        source = ("fn test_first():\n"
                  "  pass\n"
                  "\n"
                  'test "in the middle":\n'
                  "  pass\n"
                  "\n"
                  "fn test_last():\n"
                  "  pass\n")
        self.assert_formats_to(source, source)
        self.assert_roundtrip(source)

    def test_synthetic_function_is_never_printed(self):
        source = ('test "adds two numbers":\n'
                  "  requires 1 + 1 == 2\n")
        formatted = format_source(source, "<test>")
        self.assertNotIn("_newr_test_", formatted)
        self.assertIn('test "adds two numbers":', formatted)

    def test_name_with_a_quote_roundtrips(self):
        source = ('test "say \\"hi\\"":\n'
                  "  pass\n")
        self.assert_formats_to(source, source)
        prog = parse(format_source(source, "<test>"), "<test>")
        self.assertEqual(prog.test_blocks[0].name, 'say "hi"')


class TestCommentsAndBlankLines(FormatTestCase):

    def test_header_comments_are_kept(self):
        source = ("# A header comment.\n"
                  "# Second line.\n"
                  "\n"
                  'print "hi"\n')
        self.assert_formats_to(source, source)

    def test_comment_between_items(self):
        source = ('print "a"\n'
                  "\n"
                  "# A note about the next line.\n"
                  'print "b"\n')
        self.assert_formats_to(source, source)

    def test_comment_inside_a_block(self):
        source = ("fn f():\n"
                  "  # A note inside the body.\n"
                  "  let x = 1\n"
                  "  print x\n")
        self.assert_formats_to(source, source)

    def test_trailing_comment_stays_on_its_line(self):
        source = "let x = 1  # the answer\n"
        self.assert_formats_to(source, source)

    def test_comment_at_end_of_file(self):
        source = 'print "hi"\n\n# The end.\n'
        self.assert_formats_to(source, source)

    def test_blank_lines_collapse(self):
        self.assert_formats_to('print "a"\n\n\n\nprint "b"\n',
                               'print "a"\n\nprint "b"\n')

    def test_no_blank_after_block_header(self):
        self.assert_formats_to("fn f():\n\n  let x = 1\n",
                               "fn f():\n  let x = 1\n")

    def test_top_level_order_is_kept(self):
        source = ('print "first"\n'
                  "\n"
                  "fn f():\n"
                  "  return 1\n"
                  "\n"
                  'print "last"\n')
        self.assert_formats_to(source, source)

    def test_file_ends_with_one_newline(self):
        self.assert_formats_to('print "hi"', 'print "hi"\n')
        self.assert_formats_to('print "hi"\n\n\n', 'print "hi"\n')
        self.assert_formats_to("", "")

    def test_crlf_is_normalized_to_lf(self):
        self.assert_formats_to('print "hi"\r\n', 'print "hi"\n')
        self.assertEqual(is_formatted('print "hi"\r\n'), False)
        self.assertEqual(is_formatted('print "hi"\n'), True)


class TestNoSource(FormatTestCase):
    """format_program without source renders from values."""

    def test_literals_render_from_values(self):
        program = parse('let a = 0x10\nlet b = 1_000\n'
                        'let c = "x\\"y"\nlet d = 2.5\n', "<test>")
        out = format_program(program, source=None)
        self.assertIn("let a = 16\n", out)
        self.assertIn("let b = 1000\n", out)
        self.assertIn('let c = "x\\"y"\n', out)
        self.assertIn("let d = 2.5\n", out)

    def test_duration_renders_as_seconds(self):
        program = parse("task every 1h:\n  do:\n    log \"x\"\n", "<test>")
        out = format_program(program, source=None)
        self.assertIn("task every 3600s:\n", out)

    def test_no_source_roundtrip(self):
        source = "let x = 1 + 2\nprint x\n"
        program = parse(source, "<test>")
        out = format_program(program, source=None)
        self.assertEqual(_strip_spans(parse(source, "<test>")),
                         _strip_spans(parse(out, "<test>")))


class TestCorpus(FormatTestCase):
    """The whole repo corpus: examples/ + conformance/ok/."""

    def test_every_corpus_file_formats_clean_or_is_an_exception(self):
        unclean = []
        for path in _corpus_files():
            rel = str(path.relative_to(ROOT))
            source = path.read_text(encoding="utf-8")
            formatted = format_source(source, rel)
            if formatted != source and rel not in CORPUS_EXCEPTIONS:
                unclean.append(rel)
        self.assertEqual(unclean, [])

    def test_corpus_idempotency(self):
        for path in _corpus_files():
            rel = str(path.relative_to(ROOT))
            source = path.read_text(encoding="utf-8")
            once = format_source(source, rel)
            twice = format_source(once, rel)
            self.assertEqual(once, twice, f"{rel} is not idempotent")

    def test_corpus_ast_roundtrip(self):
        for path in _corpus_files():
            rel = str(path.relative_to(ROOT))
            source = path.read_text(encoding="utf-8")
            formatted = format_source(source, rel)
            self.assertEqual(
                _strip_spans(parse(source, rel)),
                _strip_spans(parse(formatted, rel)),
                f"{rel} changes meaning when formatted")


class TestCLI(unittest.TestCase):

    def _write_temp(self, directory, name, text):
        path = Path(directory) / name
        path.write_text(text, encoding="utf-8")
        return path

    def test_fmt_rewrites_a_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_temp(tmp, "a.newr", "let x=1+2\n")
            result = _run_cli("fmt", str(path))
            self.assertEqual(result.returncode, 0, result.stdout)
            self.assertIn("formatted", result.stdout)
            self.assertEqual(path.read_text(encoding="utf-8"),
                             "let x = 1 + 2\n")

    def test_fmt_ok_when_already_formatted(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_temp(tmp, "a.newr", "let x = 1 + 2\n")
            result = _run_cli("fmt", str(path))
            self.assertEqual(result.returncode, 0, result.stdout)
            self.assertIn("ok", result.stdout)

    def test_fmt_check_clean_and_dirty(self):
        with tempfile.TemporaryDirectory() as tmp:
            clean = self._write_temp(tmp, "clean.newr", "let x = 1\n")
            dirty = self._write_temp(tmp, "dirty.newr", "let x=1\n")
            result = _run_cli("fmt", "--check", str(clean))
            self.assertEqual(result.returncode, 0, result.stdout)
            self.assertIn("ok", result.stdout)
            result = _run_cli("fmt", "--check", str(dirty))
            self.assertEqual(result.returncode, 4, result.stdout)
            self.assertIn("needs formatting", result.stdout)
            # --check rewrites nothing.
            self.assertEqual(dirty.read_text(encoding="utf-8"),
                             "let x=1\n")

    def test_fmt_parse_error_exits_4(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_temp(tmp, "bad.newr", "let x = \n")
            result = _run_cli("fmt", str(path))
            self.assertEqual(result.returncode, 4, result.stdout)
            self.assertIn("E1001", result.stdout)

    def test_fmt_missing_file_exits_2(self):
        result = _run_cli("fmt", "no-such-file.newr")
        self.assertEqual(result.returncode, 2)

    def test_fmt_no_files_exits_1(self):
        result = _run_cli("fmt")
        self.assertEqual(result.returncode, 1)

    def test_fmt_unknown_flag_exits_1(self):
        result = _run_cli("fmt", "--bogus", "a.newr")
        self.assertEqual(result.returncode, 1)

    def test_fmt_json_envelope(self):
        with tempfile.TemporaryDirectory() as tmp:
            dirty = self._write_temp(tmp, "dirty.newr", "let x=1\n")
            result = _run_cli("--json", "fmt", "--check", str(dirty))
            self.assertEqual(result.returncode, 4, result.stdout)
            envelope = json.loads(result.stdout)
            self.assertFalse(envelope["ok"])
            self.assertEqual(envelope["command"], "fmt")
            self.assertTrue(envelope["data"]["files"][0]["changed"])
            clean = self._write_temp(tmp, "clean.newr", "let x = 1\n")
            result = _run_cli("--json", "fmt", "--check", str(clean))
            self.assertEqual(result.returncode, 0, result.stdout)
            envelope = json.loads(result.stdout)
            self.assertTrue(envelope["ok"])
            self.assertFalse(envelope["data"]["files"][0]["changed"])

    def test_fmt_processes_later_files_after_a_parse_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = self._write_temp(tmp, "bad.newr", "let x = \n")
            good = self._write_temp(tmp, "good.newr", "let x=1\n")
            result = _run_cli("fmt", str(bad), str(good))
            self.assertEqual(result.returncode, 4, result.stdout)
            self.assertEqual(good.read_text(encoding="utf-8"),
                             "let x = 1\n")

    def test_help_mentions_fmt(self):
        result = _run_cli("help", "fmt")
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("fmt", result.stdout)
        self.assertIn("--check", result.stdout)
        result = _run_cli("help")
        self.assertIn("fmt", result.stdout)


if __name__ == "__main__":
    unittest.main()
