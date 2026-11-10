"""Tests for the ui checker (SPEC.md SS12.8, M8c).

Written FIRST (TDD RED). The helper runs the full front end:
resolve -> core check (with discord providers) -> discord check
-> db check -> http check -> json check -> ui check.
"""

import unittest

from compiler import ast_nodes as A
from compiler.ast_nodes import STR
from compiler.checker import check_program
from compiler.db_check import check_db
from compiler.discord_check import check_discord, discord_providers
from compiler.errors import NewrError
from compiler.http_check import check_http
from compiler.json_check import check_json
from compiler.resolver import resolve_source
from compiler.ui_check import check_ui


def full(src):
    """All diagnostics from resolve + core check + discord + db +
    http + json + ui."""
    resolved = resolve_source(src)
    diags = list(resolved.diagnostics)
    diags += check_program(resolved.program,
                           providers=discord_providers(resolved.program)
                           ).diagnostics
    diags += check_discord(resolved.program)
    diags += check_db(resolved.program)
    diags += check_http(resolved.program)
    diags += check_json(resolved.program)
    diags += check_ui(resolved.program)
    return diags


def core_only(src):
    """Diagnostics from resolve + core check only (no domain
    checker): the ui rules must live in ui_check, never in
    core."""
    resolved = resolve_source(src)
    diags = list(resolved.diagnostics)
    diags += check_program(resolved.program,
                           providers=discord_providers(resolved.program)
                           ).diagnostics
    return diags


def codes(diags):
    return [d.code for d in diags]


def errs(diags):
    return [d for d in diags if d.is_error()]


def messages(diags):
    return [d.message for d in errs(diags)]


def helps(diags):
    return [d.help or "" for d in errs(diags)]


# A view function used by several show/run tests.
VIEW = """fn view() -> Widget:
  return ui.text("hi")

"""


class TestUiOk(unittest.TestCase):
    def test_text_renders(self):
        diags = full('print(ui.render(ui.text("hi")))\n')
        self.assertEqual(errs(diags), [])

    def test_button_inline_lambda_ok(self):
        src = ('ui.button("Tap", fn() -> none: print("tapped"))\n')
        self.assertEqual(errs(full(src)), [])

    def test_button_let_bound_lambda_ok(self):
        src = ('let h = fn() -> none: print("tapped")\n'
               'ui.button("Tap", h)\n')
        self.assertEqual(errs(full(src)), [])

    def test_button_named_function_ok(self):
        src = ('fn on_tap() -> none:\n'
               '  print("tapped")\n'
               'ui.button("Tap", on_tap)\n')
        self.assertEqual(errs(full(src)), [])

    def test_input_handler_ok(self):
        src = ('ui.input("Name", "", '
               'fn(s: str) -> none: print(s))\n')
        self.assertEqual(errs(full(src)), [])

    def test_nested_column_ok(self):
        src = ('let tree = ui.column([\n'
               '  ui.text("a"),\n'
               '  ui.row([ui.text("b"), ui.text("c")]),\n'
               '  ui.list([ui.text("d")]),\n'
               '])\n'
               'print(ui.render(tree))\n')
        self.assertEqual(errs(full(src)), [])

    def test_empty_children_ok(self):
        src = ('print(ui.render(ui.column([])))\n'
               'print(ui.render(ui.list([])))\n')
        self.assertEqual(errs(full(src)), [])

    def test_image_https_ok(self):
        src = ('print(ui.render(ui.image('
               '"https://example.com/a.png")))\n')
        self.assertEqual(errs(full(src)), [])

    def test_show_widget_ok(self):
        self.assertEqual(errs(full('ui.show(ui.text("hi"))\n')), [])

    def test_run_named_view_ok(self):
        self.assertEqual(errs(full(VIEW + 'ui.run(view)\n')), [])

    def test_run_inline_view_ok(self):
        src = ('ui.run(fn() -> Widget: ui.text("hi"))\n')
        self.assertEqual(errs(full(src)), [])

    def test_run_let_bound_view_ok(self):
        src = ('let v = fn() -> Widget: ui.text("hi")\n'
               'ui.run(v)\n')
        self.assertEqual(errs(full(src)), [])

    def test_widget_let_typed_ok(self):
        src = ('let w: Widget = ui.text("hi")\n'
               'print(ui.render(w))\n')
        self.assertEqual(errs(full(src)), [])

    def test_render_result_is_str(self):
        src = ('let html: str = ui.render(ui.text("hi"))\n')
        self.assertEqual(errs(full(src)), [])

    def test_button_keyword_member_parses(self):
        # `button` is a keyword (discord blocks), so `ui.button`
        # only parses because the parser accepts a keyword as a
        # member name after a dot.
        src = ('ui.button("Tap", fn() -> none: print("x"))\n')
        self.assertEqual(errs(full(src)), [])


class TestUiU1ArityAndTypes(unittest.TestCase):
    def test_text_wrong_arg_type(self):
        diags = full('ui.text(42)\n')
        self.assertEqual(codes(errs(diags)), ["E2014"])

    def test_text_missing_arg(self):
        diags = full('ui.text()\n')
        self.assertEqual(codes(errs(diags)), ["E2014"])

    def test_button_missing_handler(self):
        diags = full('ui.button("Tap")\n')
        self.assertEqual(codes(errs(diags)), ["E2014"])

    def test_button_extra_arg(self):
        diags = full('ui.button("Tap", fn() -> none: print("x"), 1)\n')
        self.assertEqual(codes(errs(diags)), ["E2014"])

    def test_list_non_widget_child(self):
        diags = full('ui.list(["a"])\n')
        self.assertEqual(codes(errs(diags)), ["E2014"])
        self.assertIn("list[Widget]", messages(diags)[0])

    def test_list_mixed_child(self):
        diags = full('ui.list([ui.text("a"), "b"])\n')
        self.assertIn("E2014", codes(errs(diags)))

    def test_row_non_widget_child(self):
        diags = full('ui.row([1])\n')
        self.assertEqual(codes(errs(diags)), ["E2014"])

    def test_render_non_widget(self):
        diags = full('print(ui.render(42))\n')
        self.assertEqual(codes(errs(diags)), ["E2014"])

    def test_kwargs_rejected(self):
        diags = full('ui.text(text: "hi")\n')
        self.assertEqual(codes(errs(diags)), ["E2014"])

    def test_unknown_member(self):
        diags = full('ui.card("x")\n')
        self.assertEqual(codes(errs(diags)), ["E2014"])
        self.assertIn("card", messages(diags)[0])

    def test_unknown_member_suggests(self):
        diags = full('ui.tex("hi")\n')
        self.assertEqual(codes(errs(diags)), ["E2014"])
        self.assertIn("text", helps(diags)[0])

    def test_ui_member_not_a_value(self):
        diags = full('let f = ui.text\n')
        self.assertIn("E2014", codes(errs(diags)))


class TestUiU2Handlers(unittest.TestCase):
    def test_button_handler_wrong_params(self):
        src = ('ui.button("x", fn(s: str) -> none: print(s))\n')
        diags = full(src)
        self.assertEqual(codes(errs(diags)), ["E2014"])
        self.assertIn("fn() -> none", helps(diags)[0])

    def test_button_handler_wrong_return(self):
        src = ('ui.button("x", fn() -> int: 1)\n')
        diags = full(src)
        self.assertEqual(codes(errs(diags)), ["E2014"])

    def test_button_handler_missing_return(self):
        src = ('ui.button("x", fn(): print("hi"))\n')
        diags = full(src)
        self.assertEqual(codes(errs(diags)), ["E2014"])

    def test_input_handler_missing_param(self):
        src = ('ui.input("N", "", fn() -> none: print("x"))\n')
        diags = full(src)
        self.assertEqual(codes(errs(diags)), ["E2014"])

    def test_input_handler_wrong_param_type(self):
        src = ('ui.input("N", "", fn(n: int) -> none: print(n))\n')
        diags = full(src)
        self.assertEqual(codes(errs(diags)), ["E2014"])

    def test_input_handler_named_function_wrong_sig(self):
        src = ('fn on_input() -> none:\n'
               '  print("x")\n'
               'ui.input("N", "", on_input)\n')
        diags = full(src)
        self.assertEqual(codes(errs(diags)), ["E2014"])

    def test_handler_not_a_function(self):
        src = ('ui.button("x", 42)\n')
        diags = full(src)
        self.assertEqual(codes(errs(diags)), ["E2014"])
        self.assertIn("fn() -> none", messages(diags)[0])


class TestUiU3ShowRun(unittest.TestCase):
    def test_view_to_show(self):
        src = ('ui.show(fn() -> Widget: ui.text("x"))\n')
        diags = full(src)
        self.assertEqual(codes(errs(diags)), ["E2014"])

    def test_widget_to_run(self):
        src = ('ui.run(ui.text("x"))\n')
        diags = full(src)
        self.assertEqual(codes(errs(diags)), ["E2014"])
        self.assertIn("fn() -> Widget", messages(diags)[0])

    def test_named_view_to_show(self):
        diags = full(VIEW + 'ui.show(view)\n')
        self.assertEqual(codes(errs(diags)), ["E2014"])

    def test_run_view_wrong_return(self):
        src = ('ui.run(fn() -> none: print("x"))\n')
        diags = full(src)
        self.assertEqual(codes(errs(diags)), ["E2014"])

    def test_run_view_wrong_params(self):
        src = ('ui.run(fn(s: str) -> Widget: ui.text(s))\n')
        diags = full(src)
        self.assertEqual(codes(errs(diags)), ["E2014"])


class TestUiU4OneWindow(unittest.TestCase):
    def test_two_shows(self):
        # Only the second show errors (U4, one window per program).
        src = ('ui.show(ui.text("a"))\n'
               'ui.show(ui.text("b"))\n')
        diags = full(src)
        self.assertEqual(codes(errs(diags)), ["E2014"])

    def test_show_and_run(self):
        diags = full(VIEW + 'ui.show(ui.text("a"))\nui.run(view)\n')
        self.assertEqual(codes(errs(diags)), ["E2014"])

    def test_two_runs(self):
        src = ('ui.run(fn() -> Widget: ui.text("a"))\n'
               'ui.run(fn() -> Widget: ui.text("b"))\n')
        diags = full(src)
        self.assertEqual(codes(errs(diags)), ["E2014"])

    def test_show_in_branch_counts(self):
        src = ('if true:\n'
               '  ui.show(ui.text("a"))\n'
               'else:\n'
               '  ui.show(ui.text("b"))\n')
        diags = full(src)
        self.assertEqual(codes(errs(diags)), ["E2014"])


class TestUiU5U6(unittest.TestCase):
    def test_render_str_arg(self):
        diags = full('print(ui.render("hi"))\n')
        self.assertEqual(codes(errs(diags)), ["E2014"])

    def test_image_ftp_url(self):
        diags = full('ui.image("ftp://example.com/a.png")\n')
        self.assertEqual(codes(errs(diags)), ["E2014"])

    def test_image_relative_url(self):
        diags = full('ui.image("/a.png")\n')
        self.assertEqual(codes(errs(diags)), ["E2014"])

    def test_image_http_ok(self):
        diags = full('ui.image("http://example.com/a.png")\n')
        self.assertEqual(errs(diags), [])

    def test_image_computed_url_ok(self):
        src = ('let u = "https://example.com/a.png"\n'
               'ui.image(u)\n')
        self.assertEqual(errs(full(src)), [])


class TestUiU7U8(unittest.TestCase):
    def test_block_lambda_handler_is_e1001(self):
        # A block-body lambda as a call argument does not parse
        # at all, so this is E1001, not E2014 (M8b handler rule).
        # The parser raises instead of returning diagnostics.
        src = ('ui.button("x", fn() -> none {\n'
               '  print("hi")\n'
               '})\n')
        with self.assertRaises(NewrError) as ctx:
            full(src)
        self.assertEqual(ctx.exception.code, "E1001")

    def test_show_in_test_block(self):
        src = ('test "show in test":\n'
               '  ui.show(ui.text("x"))\n')
        diags = full(src)
        self.assertEqual(codes(errs(diags)), ["E2014"])
        self.assertIn("test block", messages(diags)[0])

    def test_run_in_test_block(self):
        src = ('test "run in test":\n'
               '  ui.run(fn() -> Widget: ui.text("x"))\n')
        diags = full(src)
        self.assertEqual(codes(errs(diags)), ["E2014"])

    def test_show_in_test_reports_once(self):
        # The test block is lowered to a synthetic function sharing
        # the body: it must be walked once, with _in_test set, so
        # the error appears exactly once and no spurious one-window
        # error follows.
        src = ('test "show in test":\n'
               '  ui.show(ui.text("x"))\n')
        diags = full(src)
        self.assertEqual(len(errs(diags)), 1)

    def test_render_in_test_ok(self):
        src = ('test "render in test":\n'
               '  let h = ui.render(ui.text("x"))\n'
               '  if h != "<p>x</p>":\n'
               '    throw "render mismatch"\n')
        self.assertEqual(errs(full(src)), [])


class TestUiCoreBoundary(unittest.TestCase):
    def test_no_e2014_from_core(self):
        # A ui error must come from ui_check only, never from the
        # core checker.
        src = ('ui.button("x", fn(s: str) -> none: print(s))\n')
        self.assertNotIn("E2014", codes(core_only(src)))
        self.assertIn("E2014", codes(full(src)))

    def test_core_knows_widget_type(self):
        # `Widget` is a builtin type name, so a typed let validates
        # in the core checker without a ui error.
        src = ('let w: Widget = ui.text("hi")\n')
        self.assertEqual(errs(core_only(src)), [])
        self.assertEqual(errs(full(src)), [])

    def test_ui_calls_enrich_ast(self):
        # The checker sets expr_type on ui calls, so the core
        # checker's types flow through: render is str, show is none.
        resolved = resolve_source(
            'let h = ui.render(ui.text("hi"))\n')
        diags = list(resolved.diagnostics)
        diags += check_program(resolved.program,
                               providers=discord_providers(
                                   resolved.program)).diagnostics
        diags += check_ui(resolved.program)
        self.assertEqual(errs(diags), [])
        prog = resolved.program
        let = prog.stmts[0]
        self.assertIsInstance(let, A.Let)
        self.assertEqual(let.value.expr_type, STR)


if __name__ == "__main__":
    unittest.main()
