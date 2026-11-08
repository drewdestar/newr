"""Parser tests (SPEC.md §6-§16). Seams: parse(source, filename) -> Program.

Tests verify behavior through the public interface only: the AST shape
(node types, names, key fields) and NewrError (code E1001, byte-offset
span) for syntax errors. Spans are checked via line_col from
compiler.errors — line/col are derived, never stored on nodes.
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from compiler.ast_nodes import (  # noqa: E402
    Assign, BinOp, BotDecl, Break, Call, CommandDecl, Const, Continue,
    DictLit, EmbedExpr, EnumDecl, EventDecl, ExprStmt, FnDecl, For,
    If, Import, Interp, Let, ListLit, Lit, Log, Match, Member, Name,
    Pass, Propagate, Reply, Require, Return, StoreDecl, StringsDecl, StructDecl,
    TaskDecl, TCall, Ternary, TestBlock, Throw, Try, TryExpr, UnaryOp, While,
)
from compiler.errors import NewrError, Span, line_col  # noqa: E402
from compiler.parser import parse  # noqa: E402

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"


def parse_file(name):
    return parse((EXAMPLES / name).read_text(encoding="utf-8"), name)


def err(src, code="E1001"):
    """Assert parse(src) raises NewrError with the given code; return it."""
    with unittest.TestCase().assertRaises(NewrError) as ctx:
        parse(src)
    exc = ctx.exception
    assert exc.code == code, f"expected {code}, got {exc.code}: {exc}"
    return exc


def span_line(src, exc):
    return line_col(src, exc.span.start_byte)


class TestExamples(unittest.TestCase):
    def test_hello(self):
        prog = parse_file("hello.newr")
        self.assertEqual(len(prog.stmts), 1)
        stmt = prog.stmts[0]
        self.assertIsInstance(stmt, ExprStmt)
        self.assertIsInstance(stmt.value, Call)
        self.assertIsInstance(stmt.value.func, Name)
        self.assertEqual(stmt.value.func.name, "print")
        self.assertEqual(len(stmt.value.args), 1)
        self.assertIsInstance(stmt.value.args[0], Lit)
        self.assertEqual(stmt.value.args[0].value, "Hello, world!")

    def test_hello_bot(self):
        prog = parse_file("hello_bot.newr")
        self.assertIsInstance(prog.bot, BotDecl)
        self.assertEqual(prog.bot.name, "HelloBot")
        self.assertEqual(prog.bot.description,
                         "A friendly bot that answers in plain words")
        self.assertEqual([c.name for c in prog.commands],
                         ["ping", "greet"])
        ping = prog.commands[0]
        self.assertEqual(ping.description, "Answer with pong")
        self.assertIsNotNone(ping.reply_expr)
        self.assertIsInstance(ping.reply_expr, Interp)
        parts = ping.reply_expr.parts
        self.assertEqual(parts[0].text, "Pong! Hello ")
        self.assertIsInstance(parts[1].expr, Name)
        self.assertEqual(parts[1].expr.name, "user")
        self.assertIsNone(ping.body)
        self.assertEqual(len(ping.effective_body()), 1)
        self.assertIsInstance(ping.effective_body()[0], Reply)
        greet = prog.commands[1]
        self.assertTrue(greet.ephemeral)
        self.assertEqual([e.kind for e in prog.events],
                         ["ready", "member_join"])
        ready = prog.events[0]
        self.assertIsNotNone(ready.log_expr)
        self.assertIsInstance(ready.log_expr, Interp)
        self.assertEqual(len(ready.effective_body()), 1)
        self.assertIsInstance(ready.effective_body()[0], Log)

    def test_showcase(self):
        prog = parse_file("showcase.newr")
        self.assertEqual(prog.bot.name, "ShowcaseBot")
        self.assertEqual([c.name for c in prog.commands],
                         ["ping", "whereami", "secret", "rules"])
        rules = prog.commands[3]
        self.assertIsNone(rules.description)  # defaults to the name
        self.assertIsNotNone(rules.reply_expr)
        self.assertTrue(prog.commands[2].ephemeral)

    def test_shop(self):
        prog = parse_file("shop.newr")
        self.assertEqual(len(prog.imports), 1)
        self.assertEqual(prog.imports[0].path, "shop_items.newr")
        self.assertEqual(prog.imports[0].bound_name, "shop_items")
        self.assertEqual(prog.bot.intents, ["guilds", "members"])
        self.assertEqual([s.locale for s in prog.strings], ["en", "id"])
        en = prog.strings[0]
        self.assertEqual([e.key for e in en.entries],
                         ["buy.ok", "buy.poor"])
        self.assertEqual(en.entries[0].placeholders,
                         ["item", "price", "balance"])
        self.assertEqual(len(prog.stores), 1)
        store = prog.stores[0]
        self.assertEqual(store.name, "economy")
        self.assertTrue(store.ledger)
        self.assertEqual(store.tables[0].name, "balances")
        self.assertEqual([c.name for c in store.tables[0].columns],
                         ["user", "balance"])
        buy = [c for c in prog.commands if c.name == "buy"][0]
        self.assertEqual([o.name for o in buy.options],
                         ["item", "amount"])
        self.assertTrue(buy.options[0].required)
        self.assertEqual([e.value for e in buy.options[0].choices or []],
                         ["brick", "potion"])
        self.assertIsNotNone(buy.body)
        self.assertEqual(len(prog.tasks), 1)
        self.assertEqual(prog.tasks[0].interval_seconds, 3600)

    def test_shop_items(self):
        prog = parse_file("shop_items.newr")
        self.assertEqual(len(prog.functions), 1)
        fn = prog.functions[0]
        self.assertEqual(fn.name, "price_of")
        self.assertTrue(fn.pub)
        self.assertEqual(fn.return_type.name, "int")
        self.assertEqual(len(fn.body), 3)


class TestTopLevel(unittest.TestCase):
    def test_import_forms(self):
        prog = parse('import "a/b.newr" as foo\nimport math\nimport discord\n')
        self.assertEqual(prog.imports[0].path, "a/b.newr")
        self.assertEqual(prog.imports[0].alias, "foo")
        self.assertEqual(prog.imports[0].bound_name, "foo")
        self.assertEqual(prog.imports[1].module, "math")
        self.assertEqual(prog.imports[2].module, "discord")

    def test_pub_fn_struct_enum_let_const(self):
        prog = parse("pub fn f() -> int:\n  return 1\n"
                     "pub struct P:\n  x: int\n"
                     "pub enum C:\n  Red\n"
                     "pub let x = 1\npub const y = 2\n")
        self.assertTrue(prog.functions[0].pub)
        self.assertTrue(prog.structs[0].pub)
        self.assertTrue(prog.enums[0].pub)
        self.assertTrue(prog.stmts[0].pub)
        self.assertTrue(prog.stmts[1].pub)
        self.assertIsInstance(prog.stmts[0], Let)
        self.assertIsInstance(prog.stmts[1], Const)

    def test_fn_params_and_return(self):
        prog = parse("fn add(a: int, b: int) -> int:\n  return a + b\n"
                     "fn noop():\n  pass\n")
        fn = prog.functions[0]
        self.assertEqual([p.name for p in fn.params], ["a", "b"])
        self.assertEqual(fn.params[0].type.name, "int")
        self.assertEqual(fn.return_type.name, "int")
        self.assertIsNone(prog.functions[1].return_type)

    def test_struct_fields(self):
        prog = parse("struct Point:\n  x: int\n  y: float\n")
        self.assertEqual(prog.structs[0].name, "Point")
        self.assertEqual([(f.name, f.type.name)
                          for f in prog.structs[0].fields],
                         [("x", "int"), ("y", "float")])

    def test_enum_variants(self):
        prog = parse("enum Color:\n  Red\n  Green\n"
                     "enum Shape:\n  Circle(radius: float)\n"
                     "  Rect(width: float, height: float)\n")
        self.assertEqual([v.name for v in prog.enums[0].variants],
                         ["Red", "Green"])
        circle = prog.enums[1].variants[0]
        self.assertEqual(circle.name, "Circle")
        self.assertEqual(circle.fields[0].name, "radius")
        self.assertEqual(circle.fields[0].type.name, "float")

    def test_top_level_statements_in_order(self):
        prog = parse("let a = 1\nlet b = 2\nprint a\n")
        self.assertEqual([s.name for s in prog.stmts
                          if isinstance(s, Let)], ["a", "b"])
        self.assertEqual(len(prog.stmts), 3)

    def test_indented_line_outside_block_is_error(self):
        exc = err("  x\n")
        self.assertEqual(span_line("  x\n", exc), (1, 1))

    def test_unknown_top_level_line_is_error(self):
        exc = err("frobnicate x\n")
        # Span points at the offending token (`x`), on line 1.
        self.assertEqual(span_line("frobnicate x\n", exc)[0], 1)

    def test_missing_colon_is_error(self):
        src = "fn f()\n  pass\n"
        exc = err(src)
        # Span points at the end of the header line, on line 1.
        self.assertEqual(span_line(src, exc)[0], 1)

    def test_decl_inside_body_is_error(self):
        src = "fn f():\n  fn g():\n    pass\n"
        exc = err(src)
        self.assertEqual(span_line(src, exc), (2, 3))


class TestTestBlocks(unittest.TestCase):
    """`test "name":` blocks (SPEC.md section 8.7, M6 Phase 2c)."""

    def test_block_parses_with_name_and_body(self):
        prog = parse("test \"adds two numbers\":\n"
                     "  requires 1 + 1 == 2\n")
        self.assertEqual(len(prog.test_blocks), 1)
        block = prog.test_blocks[0]
        self.assertIsInstance(block, TestBlock)
        self.assertEqual(block.name, "adds two numbers")
        self.assertEqual(block.func_name, "_newr_test_0")
        self.assertEqual(len(block.body), 1)
        self.assertIsInstance(block.body[0], Require)

    def test_block_lowers_to_a_synthetic_function(self):
        # The synthetic fn shares the block's body list, so the
        # resolver, checker, and emitters see a plain fn.
        prog = parse("test \"one\":\n  pass\ntest \"two\":\n  pass\n")
        self.assertEqual([tb.func_name for tb in prog.test_blocks],
                         ["_newr_test_0", "_newr_test_1"])
        synth = [fn for fn in prog.functions
                 if fn.name.startswith("_newr_test_")]
        self.assertEqual([fn.name for fn in synth],
                         ["_newr_test_0", "_newr_test_1"])
        self.assertIs(synth[0].body, prog.test_blocks[0].body)
        self.assertIs(synth[1].body, prog.test_blocks[1].body)
        self.assertEqual(synth[0].params, [])
        self.assertIsNone(synth[0].return_type)

    def test_block_and_fn_share_one_program(self):
        prog = parse("fn test_a():\n  pass\n"
                     "test \"block b\":\n  pass\n")
        self.assertEqual([fn.name for fn in prog.functions],
                         ["test_a", "_newr_test_0"])
        self.assertEqual([tb.name for tb in prog.test_blocks],
                         ["block b"])

    def test_empty_name_is_error(self):
        exc = err("test \"\":\n  pass\n")
        self.assertIn("non-empty", exc.diagnostic.message)

    def test_newline_in_name_is_error(self):
        src = "test \"a\\nb\":\n  pass\n"
        exc = err(src)
        self.assertIn("newline", exc.diagnostic.message)

    def test_interpolated_name_is_error(self):
        exc = err("test \"a {x} b\":\n  pass\n")
        self.assertIn("plain string", exc.diagnostic.message)

    def test_missing_colon_is_error(self):
        src = "test \"x\"\n  pass\n"
        exc = err(src)
        self.assertEqual(span_line(src, exc)[0], 1)

    def test_block_inside_body_is_error(self):
        src = "fn outer():\n  test \"nested\":\n    pass\n"
        exc = err(src)
        self.assertIn("top level", exc.diagnostic.message)
        self.assertEqual(span_line(src, exc), (2, 3))

    def test_test_is_reserved_as_an_identifier(self):
        # `test` is a keyword now: it cannot name a variable.
        exc = err("let test = 1\n")
        self.assertEqual(span_line("let test = 1\n", exc)[0], 1)


class TestCommandsEventsTasks(unittest.TestCase):
    def test_command_name_with_dash_and_digits(self):
        prog = parse("command foo-bar:\n  reply: \"hi\"\n"
                     "command 123abc:\n  reply: \"hi\"\n")
        self.assertEqual([c.name for c in prog.commands],
                         ["foo-bar", "123abc"])

    def test_subcommand_is_error(self):
        src = "command a b:\n  reply: \"hi\"\n"
        exc = err(src)
        self.assertIn("v1.1", str(exc))
        # Span points at the second name (`b`), on line 1.
        self.assertEqual(span_line(src, exc)[0], 1)

    def test_command_needs_reply_or_do(self):
        src = "command a:\n  description: \"x\"\n"
        exc = err(src)
        self.assertEqual(span_line(src, exc), (1, 1))

    def test_command_reply_and_do_is_error(self):
        src = ("command a:\n  reply: \"hi\"\n  do:\n    reply \"hi\"\n")
        exc = err(src)
        # Span points at the `do:` line (line 3).
        self.assertEqual(span_line(src, exc)[0], 3)

    def test_command_do_body(self):
        prog = parse("command a:\n  do:\n    reply \"hi\"\n")
        self.assertIsNotNone(prog.commands[0].body)
        self.assertIsInstance(prog.commands[0].body[0], Reply)

    def test_option_modifiers(self):
        prog = parse(
            "command buy:\n"
            "  option item: str (required, choices: [\"a\"], "
            "description: \"pick\")\n"
            "  option n: num (default: 1.5, min: 0, max: 9)\n"
            "  option who: user\n"
            "  do:\n    reply \"ok\"\n")
        opts = prog.commands[0].options
        self.assertEqual(opts[0].type.name, "str")
        self.assertTrue(opts[0].required)
        self.assertEqual(opts[0].description, "pick")
        self.assertEqual(opts[1].type.name, "float")  # num -> float
        self.assertEqual(opts[1].default.value, 1.5)
        self.assertEqual(opts[1].min_value.value, 0)
        self.assertEqual(opts[1].max_value.value, 9)
        self.assertEqual(opts[2].type.name, "user")

    def test_event_kinds(self):
        prog = parse("event message:\n  do:\n    send \"hi\" to channel\n"
                     "event member_remove:\n  reply: \"bye {user}\"\n")
        self.assertEqual([e.kind for e in prog.events],
                         ["message", "member_remove"])
        self.assertIsNotNone(prog.events[0].body)

    def test_task_duration(self):
        prog = parse("task every 5m:\n  do:\n    log \"tick\"\n")
        self.assertEqual(prog.tasks[0].interval_seconds, 300)

    def test_locale_names(self):
        prog = parse("strings en-US:\n  \"k\": \"v\"\n")
        self.assertEqual(prog.strings[0].locale, "en-US")
        self.assertIsInstance(prog.strings[0], StringsDecl)


class TestStatements(unittest.TestCase):
    def test_let_const_assign(self):
        prog = parse("let x: int = 1\nconst y = 2\nx += 3\nx = 4\n")
        let = prog.stmts[0]
        self.assertIsInstance(let, Let)
        self.assertEqual(let.type_ann.name, "int")
        self.assertIsInstance(prog.stmts[1], Const)
        self.assertEqual(prog.stmts[2].op, "+=")
        self.assertEqual(prog.stmts[3].op, "=")

    def test_if_elif_else(self):
        prog = parse("if a:\n  pass\nelif b:\n  pass\nelse:\n  pass\n")
        node = prog.stmts[0]
        self.assertIsInstance(node, If)
        self.assertEqual(len(node.elifs), 1)
        self.assertIsNotNone(node.else_body)

    def test_for_while(self):
        prog = parse("for x in xs:\n  pass\nwhile true:\n  break\n")
        self.assertIsInstance(prog.stmts[0], For)
        self.assertEqual(prog.stmts[0].var, "x")
        self.assertIsInstance(prog.stmts[1], While)

    def test_try_catch_finally(self):
        prog = parse("try:\n  pass\ncatch err:\n  log \"e\"\nfinally:\n  pass\n")
        node = prog.stmts[0]
        self.assertIsInstance(node, Try)
        self.assertEqual(node.catches[0][0], "err")
        self.assertIsNotNone(node.finally_body)

    def test_try_needs_catch_or_finally(self):
        src = "try:\n  pass\n"
        exc = err(src)
        self.assertEqual(span_line(src, exc), (1, 1))

    def test_throw_return_break_continue_pass(self):
        prog = parse("fn f() -> int:\n  throw \"x\"\n  return 1\n"
                     "  break\n  continue\n  pass\n")
        body = prog.functions[0].body
        self.assertIsInstance(body[0], Throw)
        self.assertIsInstance(body[1], Return)
        self.assertIsInstance(body[2], Break)
        self.assertIsInstance(body[3], Continue)
        self.assertIsInstance(body[4], Pass)

    def test_bare_return(self):
        prog = parse("fn f():\n  return\n")
        self.assertIsNone(prog.functions[0].body[0].value)

    def test_match_arms(self):
        prog = parse(
            "match shape:\n"
            "  Shape.Circle(radius: r) if r > 0:\n    print \"circle\"\n"
            "  Shape.Rect(width: w, height: h):\n    print \"rect\"\n"
            "  1 | 2 | 3:\n    print \"small\"\n"
            "  [a, b]:\n    print \"pair\"\n"
            "  Point(x: 1, y: y):\n    print \"point\"\n"
            "  _:\n    print \"other\"\n"
            "  name:\n    print name\n")
        node = prog.stmts[0]
        self.assertIsInstance(node, Match)
        self.assertEqual(len(node.arms), 7)
        arm0 = node.arms[0]
        self.assertEqual(arm0.pattern.enum_name, "Shape")
        self.assertEqual(arm0.pattern.variant, "Circle")
        self.assertEqual(arm0.pattern.bindings[0][0], "radius")
        self.assertEqual(arm0.pattern.bindings[0][1].name, "r")
        self.assertIsNotNone(arm0.guard)
        self.assertEqual(len(node.arms[2].pattern.patterns), 3)
        self.assertEqual(len(node.arms[3].pattern.elements), 2)
        self.assertEqual(node.arms[4].pattern.struct_name, "Point")
        self.assertEqual(node.arms[5].pattern.__class__.__name__,
                         "WildcardPattern")
        self.assertEqual(node.arms[6].pattern.name, "name")

    def test_requires_ensures(self):
        prog = parse("fn f(x: int) -> int:\n  requires x > 0\n"
                     "  ensures result >= 0\n  return x\n")
        body = prog.functions[0].body
        self.assertEqual(body[0].__class__.__name__, "Require")
        self.assertEqual(body[1].__class__.__name__, "Ensure")

    def test_reply_defer_edit_followup_send_log(self):
        prog = parse("reply \"hi\" ephemeral\ndefer\nedit \"x\"\n"
                     "followup \"y\"\nsend \"z\" to chan\nlog \"l\"\n")
        self.assertIsInstance(prog.stmts[0], Reply)
        self.assertTrue(prog.stmts[0].ephemeral)
        self.assertEqual(prog.stmts[1].__class__.__name__, "Defer")
        self.assertEqual(prog.stmts[2].__class__.__name__, "Edit")
        self.assertEqual(prog.stmts[3].__class__.__name__, "Followup")
        self.assertEqual(prog.stmts[4].__class__.__name__, "Send")
        self.assertIsInstance(prog.stmts[5], Log)

    def test_log_calls(self):
        prog = parse("log.info(\"a\")\nlog.warn(\"b\")\nlog.error(\"c\")\n")
        for stmt in prog.stmts:
            self.assertIsInstance(stmt, ExprStmt)
            self.assertIsInstance(stmt.value, Call)
            self.assertIsInstance(stmt.value.func, Member)

    def test_print_forms(self):
        prog = parse('print "Hello, world!"\nprint("a", "b")\nprint x, y\n')
        for stmt in prog.stmts:
            self.assertIsInstance(stmt, ExprStmt)
            call = stmt.value
            self.assertIsInstance(call, Call)
            self.assertEqual(call.func.name, "print")
        self.assertEqual(len(prog.stmts[0].value.args), 1)
        self.assertEqual(len(prog.stmts[1].value.args), 2)
        self.assertEqual(len(prog.stmts[2].value.args), 2)


class TestComponents(unittest.TestCase):
    def test_button(self):
        prog = parse(
            "command a:\n  do:\n"
            "    button \"Buy\" (style: primary, id: \"b1\", timeout: 60s):\n"
            "      only: roles([\"admin\"])\n"
            "      do:\n        reply \"ok\"\n"
            "      on_timeout:\n        reply \"expired\"\n")
        btn = prog.commands[0].body[0]
        self.assertEqual(btn.label, "Buy")
        self.assertEqual(btn.style, "primary")
        self.assertEqual(btn.custom_id, "b1")
        self.assertEqual(btn.timeout, 60)
        self.assertEqual(btn.only_kind, "roles")
        self.assertEqual(btn.only_roles, ["admin"])
        self.assertEqual(len(btn.body), 1)
        self.assertEqual(len(btn.on_timeout), 1)

    def test_button_only_author_default(self):
        prog = parse(
            "command a:\n  do:\n"
            "    button \"B\" (style: link, url: \"https://x.example\", "
            "timeout: 30s):\n"
            "      do:\n        reply \"ok\"\n")
        btn = prog.commands[0].body[0]
        self.assertEqual(btn.only_kind, "author")
        self.assertEqual(btn.url, "https://x.example")

    def test_select(self):
        prog = parse(
            "command a:\n  do:\n"
            "    select \"Pick\" (options: [\"a\", \"b\"], min: 1, max: 2, "
            "timeout: 60s):\n"
            "      only: anyone\n"
            "      do:\n        reply \"ok\"\n")
        sel = prog.commands[0].body[0]
        self.assertEqual(sel.options, ["a", "b"])
        self.assertEqual(sel.min_values, 1)
        self.assertEqual(sel.max_values, 2)
        self.assertEqual(sel.only_kind, "anyone")

    def test_modal(self):
        prog = parse(
            "command a:\n  do:\n"
            "    modal \"Form\" (id: \"f1\", timeout: 120s):\n"
            "      text_input \"Name\" (style: short, required, "
            "min_length: 1, max_length: 50):\n"
            "      text_input \"Bio\" (style: paragraph):\n"
            "      do:\n        reply \"ok\"\n")
        modal = prog.commands[0].body[0]
        self.assertEqual(modal.title, "Form")
        self.assertEqual(modal.custom_id, "f1")
        self.assertEqual(len(modal.fields), 2)
        self.assertEqual(modal.fields[0].name, "Name")
        self.assertEqual(modal.fields[0].style, "short")
        self.assertTrue(modal.fields[0].required)
        self.assertEqual(modal.fields[0].min_length, 1)
        self.assertEqual(modal.fields[0].max_length, 50)
        self.assertFalse(modal.fields[1].required)


class TestExpressions(unittest.TestCase):
    def test_precedence_pipeline(self):
        prog = parse("let x = [1, 2, 3] |> double_all\n")
        val = prog.stmts[0].value
        self.assertIsInstance(val, Call)
        self.assertEqual(val.func.name, "double_all")
        self.assertIsInstance(val.args[0], ListLit)

    def test_pipeline_with_args(self):
        prog = parse("let x = a |> f(b, c)\n")
        val = prog.stmts[0].value
        self.assertIsInstance(val, Call)
        self.assertEqual(val.func.name, "f")
        self.assertEqual(len(val.args), 3)
        self.assertEqual(val.args[0].name, "a")
        self.assertEqual(val.args[1].name, "b")

    def test_pipeline_rhs_must_be_name_or_call(self):
        src = "let x = a |> b.c\n"
        exc = err(src)
        self.assertEqual(span_line(src, exc)[0], 1)

    def test_ternary(self):
        prog = parse("let x = a ? b : c\n")
        val = prog.stmts[0].value
        self.assertIsInstance(val, Ternary)
        self.assertEqual(val.cond.name, "a")
        self.assertEqual(val.then_expr.name, "b")
        self.assertEqual(val.else_expr.name, "c")

    def test_coalesce(self):
        prog = parse("let x = a ?? b\n")
        val = prog.stmts[0].value
        self.assertIsInstance(val, BinOp)
        self.assertEqual(val.op, "??")

    def test_coalesce_mixed_with_or_is_error(self):
        for src in ("let x = a ?? b or c\n", "let x = a or b ?? c\n",
                    "let x = a ?? b and c\n"):
            exc = err(src)
            self.assertEqual(span_line(src, exc)[0], 1)

    def test_coalesce_mixed_inside_parens_is_ok(self):
        prog = parse("let x = (a ?? b) or c\n")
        self.assertIsInstance(prog.stmts[0].value, BinOp)

    def test_comparison_non_associative(self):
        src = "let x = a < b < c\n"
        exc = err(src)
        # Span points at the second `<`, on line 1.
        self.assertEqual(span_line(src, exc)[0], 1)

    def test_power_right_assoc(self):
        prog = parse("let x = a ** b ** c\n")
        val = prog.stmts[0].value
        self.assertEqual(val.op, "**")
        self.assertEqual(val.right.op, "**")

    def test_unary_minus_power(self):
        prog = parse("let x = -a ** 2\n")
        val = prog.stmts[0].value
        self.assertIsInstance(val, UnaryOp)
        self.assertEqual(val.op, "-")
        self.assertIsInstance(val.operand, BinOp)
        self.assertEqual(val.operand.op, "**")

    def test_not_comparison(self):
        prog = parse("let x = not a == b\n")
        val = prog.stmts[0].value
        self.assertIsInstance(val, UnaryOp)
        self.assertEqual(val.op, "not")
        self.assertIsInstance(val.operand, BinOp)
        self.assertEqual(val.operand.op, "==")

    def test_postfix_chain_and_optional(self):
        prog = parse("let x = a.b[0].c()\nlet y = a?.b.c()\n")
        val = prog.stmts[0].value
        self.assertIsInstance(val, Call)
        self.assertIsInstance(val.func, Member)
        self.assertEqual(val.func.name, "c")
        val2 = prog.stmts[1].value
        self.assertIsInstance(val2, Call)
        self.assertIsInstance(val2.func, Member)

    def test_calls_positional_and_named(self):
        prog = parse("let x = f(a, b, key: v)\n")
        val = prog.stmts[0].value
        self.assertIsInstance(val, Call)
        self.assertEqual(len(val.args), 2)
        self.assertEqual(val.kwargs[0][0], "key")

    def test_struct_and_enum_construction(self):
        prog = parse("let p = Point(x: 1, y: 2)\n"
                     "let c = Color.Red\n"
                     "let s = Shape.Circle(radius: 1.5)\n")
        self.assertIsInstance(prog.stmts[0].value, Call)
        self.assertIsInstance(prog.stmts[1].value, Member)
        self.assertIsInstance(prog.stmts[2].value, Call)
        self.assertIsInstance(prog.stmts[2].value.func, Member)

    def test_list_dict_literals_and_in(self):
        prog = parse("let xs = [1, 2]\nlet d = {\"k\": 1}\n"
                     "let b = x in xs\n")
        self.assertIsInstance(prog.stmts[0].value, ListLit)
        self.assertIsInstance(prog.stmts[1].value, DictLit)
        self.assertEqual(prog.stmts[2].value.op, "in")

    def test_interpolation(self):
        prog = parse('let s = "Hello {user}, {x + 1}!"\n')
        val = prog.stmts[0].value
        self.assertIsInstance(val, Interp)
        self.assertEqual(val.parts[0].text, "Hello ")
        self.assertEqual(val.parts[1].expr.name, "user")
        prog2 = parse('let s = "plain"\n')
        self.assertIsInstance(prog2.stmts[0].value, Lit)

    def test_tcall(self):
        prog = parse('let s = t("buy.ok", item: item, price: 5)\n')
        val = prog.stmts[0].value
        self.assertIsInstance(val, TCall)
        self.assertEqual(val.key, "buy.ok")
        self.assertEqual([a[0] for a in val.args], ["item", "price"])

    def test_embed(self):
        prog = parse(
            "let card: embed = embed:\n"
            "  title: \"Welcome\"\n"
            "  description: \"Hi {user}\"\n"
            "  color: 0x5865F2\n"
            "  field \"Rules\": \"Be kind.\"\n"
            "  field \"Links\": \"https://example.com\" (inline: true)\n")
        val = prog.stmts[0].value
        self.assertIsInstance(val, EmbedExpr)
        self.assertEqual([f.kind for f in val.fields],
                         ["title", "description", "color", "field", "field"])
        self.assertEqual(val.fields[3].name, "Rules")
        self.assertTrue(val.fields[4].inline)

    def test_lambda_block_and_expr(self):
        prog = parse("let f = fn(x: int) -> int:\n  return x + 1\n"
                     "let g = fn(x: int) -> int: x * 2\n")
        from compiler.ast_nodes import Lambda
        self.assertIsInstance(prog.stmts[0].value, Lambda)
        self.assertIsNotNone(prog.stmts[0].value.body)
        self.assertIsNotNone(prog.stmts[1].value.expr_body)

    def test_try_expr(self):
        prog = parse("let r = try file.read(\"x\")\n")
        self.assertIsInstance(prog.stmts[0].value, TryExpr)

    def test_duration_in_bad_position_is_error(self):
        src = "let x = 60s\n"
        exc = err(src)
        self.assertIn("duration", str(exc).lower())
        self.assertEqual(span_line(src, exc)[0], 1)

    def test_lone_bar_in_expression_is_error(self):
        src = "let x = a | b\n"
        exc = err(src)
        self.assertEqual(span_line(src, exc)[0], 1)


class TestPropagate(unittest.TestCase):
    def test_propagate_let_return_exprstmt(self):
        prog = parse("fn f() -> int:\n  let x = g()?\n  return h()?\n"
                     "  k()?\n")
        body = prog.functions[0].body
        self.assertIsInstance(body[0].value, Propagate)
        self.assertIsInstance(body[1].value, Propagate)
        self.assertIsInstance(body[2].value, Propagate)

    def test_propagate_assign(self):
        prog = parse("x = g()?\n")
        self.assertIsInstance(prog.stmts[0].value, Propagate)

    def test_nested_propagate_in_call_is_error(self):
        src = "let x = f(g()?)\n"
        exc = err(src)
        self.assertEqual(span_line(src, exc)[0], 1)

    def test_propagate_in_binary_is_error(self):
        src = "let x = f()? + 1\n"
        exc = err(src)
        self.assertEqual(span_line(src, exc)[0], 1)

    def test_ternary_not_propagate(self):
        prog = parse("let x = a ? b : c\n")
        self.assertIsInstance(prog.stmts[0].value, Ternary)
        prog2 = parse("let x = a?\n")
        self.assertIsInstance(prog2.stmts[0].value, Propagate)


class TestTypes(unittest.TestCase):
    def test_types(self):
        prog = parse(
            "fn f(a: list[int], b: dict[str, int], c: set[str], "
            "d: int?, e: Result[int, error], "
            "g: fn(int, str) -> bool, h: duration, i: user) -> none:\n"
            "  pass\n")
        params = prog.functions[0].params
        self.assertEqual(str(params[0].type), "list[int]")
        self.assertEqual(str(params[1].type), "dict[str, int]")
        self.assertEqual(str(params[2].type), "set[str]")
        self.assertEqual(str(params[3].type), "int?")
        self.assertEqual(str(params[4].type), "Result[int, error]")
        self.assertEqual(str(params[5].type), "fn(int, str) -> bool")
        self.assertEqual(str(params[6].type), "duration")
        self.assertEqual(str(params[7].type), "user")

    def test_nullable_in_type_is_not_propagate(self):
        prog = parse("let x: int? = none\n")
        self.assertEqual(prog.stmts[0].type_ann.nullable, True)
        self.assertIsInstance(prog.stmts[0].value, Lit)


class TestSpans(unittest.TestCase):
    def test_spans_are_byte_offsets(self):
        src = "let x = 42\n"
        prog = parse(src)
        node = prog.stmts[0]
        self.assertTrue(0 <= node.span.start_byte < node.span.end_byte)
        self.assertEqual(line_col(src, node.span.start_byte), (1, 1))
        lit = node.value
        self.assertEqual((lit.span.start_byte, lit.span.end_byte), (8, 10))

    def test_error_span_points_at_offending_token(self):
        src = "let x = 1\nlet y = a < b < c\n"
        exc = err(src)
        # The second "<" is on line 2.
        self.assertEqual(span_line(src, exc)[0], 2)


if __name__ == "__main__":
    unittest.main()
