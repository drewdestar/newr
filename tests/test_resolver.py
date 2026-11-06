"""Tests for the resolver (SPEC.md §11, §17.1).

Name resolution, imports, cycles, visibility, reserved names, and the
W1001/W1002 usage warnings. Most tests build ASTs by hand and call
``resolve_program`` directly (no parser needed); the file-import tests
use ``resolve_file`` with tiny .newr sources in a temp dir.
"""

import tempfile
import unittest
from pathlib import Path

from compiler.ast_nodes import (
    Assign, BinOp, BotDecl, Call, CommandDecl, Const, EventDecl, ExprStmt,
    FnDecl, For, If, Import, Let, Lit, Member, Name, Param, Program, Return,
    StoreDecl, StructDecl, EnumDecl, EnumVariant, TaskDecl, Try, Type,
)
from compiler.errors import line_col
from compiler.resolver import resolve_file, resolve_program, resolve_source


SPAN = None  # set in setUpModule-ish: spans need a file; use a helper


def S():
    from compiler.errors import Span
    return Span("test.newr", 0, 1)


def lit(v=1):
    return Lit(span=S(), value=v)


def nm(n):
    return Name(span=S(), name=n)


def let(name, value=None, pub=False):
    return Let(span=S(), name=name, name_span=S(), value=value or lit(),
               pub=pub)


def const(name, value=None, pub=False):
    return Const(span=S(), name=name, name_span=S(), value=value or lit(),
                 pub=pub)


def fn(name, params=None, body=None, ret=None):
    return FnDecl(span=S(), name=name, params=params or [],
                  return_type=ret, body=body or [])


def param(name, typ=None):
    return Param(name=name, type=typ or Type("int"), span=S())


def prog(**kw):
    base = {"span": S(), "imports": [], "structs": [], "enums": [],
            "functions": [], "commands": [], "events": [], "tasks": [],
            "strings": [], "stores": [], "stmts": []}
    base.update(kw)
    return Program(**base)


def codes(result):
    return [d.code for d in result.diagnostics]


def resolve(p):
    return resolve_program(p, Path("test.newr"))


class TestCleanPrograms(unittest.TestCase):
    def test_clean_program_resolves(self):
        p = prog(functions=[fn("double", [param("x")],
                                [Return(span=S(), value=BinOp(
                                    span=S(), op="*", left=nm("x"),
                                    right=lit(2)))])],
                 stmts=[let("y", Call(span=S(), func=nm("double"),
                                      args=[lit(21)])),
                        ExprStmt(span=S(), value=Call(
                            span=S(), func=nm("print"), args=[nm("y")]))])
        r = resolve(p)
        self.assertEqual(r.diagnostics, [])

    def test_forward_reference_to_later_function_is_ok(self):
        p = prog(functions=[
            fn("a", [], [Return(span=S(), value=Call(
                span=S(), func=nm("b"), args=[]))]),
            fn("b", [], [Return(span=S(), value=lit(1))]),
        ])
        self.assertEqual(resolve(p).diagnostics, [])

    def test_builtin_call_resolves(self):
        p = prog(stmts=[ExprStmt(span=S(), value=Call(
            span=S(), func=nm("print"), args=[lit("hi")]))])
        self.assertEqual(resolve(p).diagnostics, [])

    def test_math_module_call_resolves(self):
        p = prog(imports=[Import(span=S(), module="math")],
                 stmts=[let("x", Call(span=S(),
                                      func=Member(span=S(), obj=nm("math"),
                                                  name="sqrt"),
                                      args=[lit(4)])),
                        ExprStmt(span=S(), value=Call(
                            span=S(), func=nm("print"), args=[nm("x")]))])
        self.assertEqual(resolve(p).diagnostics, [])


class TestUndefinedNames(unittest.TestCase):
    def test_undefined_name_is_E3001(self):
        p = prog(stmts=[ExprStmt(span=S(), value=Call(
            span=S(), func=nm("print"), args=[nm("nosuch")]))])
        r = resolve(p)
        self.assertEqual(codes(r), ["E3001"])
        self.assertEqual(r.diagnostics[0].span.file, "test.newr")
        self.assertIn("nosuch", r.diagnostics[0].message)

    def test_similar_name_help_uses_rustc_named_form(self):
        p = prog(stmts=[let("count", lit(1)),
                        ExprStmt(span=S(), value=Call(
                            span=S(), func=nm("print"), args=[nm("coun")]))])
        r = resolve(p)
        self.assertEqual(codes(r), ["E3001"])
        diag = r.diagnostics[0]
        self.assertIn("count", diag.help or "")
        self.assertNotIn("did you mean", (diag.help or "").lower())
        # Exactly one close match -> a machine-applicable suggestion.
        self.assertIsNotNone(diag.suggestion)
        self.assertEqual(diag.suggestion.applicability, "certain")
        self.assertEqual(diag.suggestion.replacement, "count")

    def test_use_before_definition_is_E2005(self):
        p = prog(functions=[fn("f", [], [
            ExprStmt(span=S(), value=Call(
                span=S(), func=nm("print"), args=[nm("x")])),
            let("x", lit(1)),
            ExprStmt(span=S(), value=Call(
                span=S(), func=nm("print"), args=[nm("x")]))])])
        r = resolve(p)
        self.assertEqual(codes(r), ["E2005"])

    def test_let_rhs_may_not_use_itself(self):
        p = prog(stmts=[let("x", BinOp(span=S(), op="+", left=nm("x"),
                                       right=lit(1)))])
        r = resolve(p)
        self.assertIn("E2005", codes(r))


class TestDuplicatesAndReserved(unittest.TestCase):
    def test_duplicate_top_level_name_is_E3003(self):
        p = prog(functions=[fn("f"), fn("f")])
        self.assertEqual(codes(resolve(p)), ["E3003"])

    def test_duplicate_struct_and_function_name_is_E3003(self):
        p = prog(structs=[StructDecl(span=S(), name="Point", fields=[])],
                 functions=[fn("Point")])
        self.assertEqual(codes(resolve(p)), ["E3003"])

    def test_catch_err_may_shadow_builtin(self):
        # SPEC.md §10's own example is `catch err:` — a catch variable
        # is a transient error binding, not a redefinition of the
        # builtin `err` (a Result builder).
        p = prog(functions=[fn("f", [], [
            Try(span=S(),
                body=[ExprStmt(span=S(), value=Call(
                    span=S(), func=nm("print"), args=[lit("trying")]))],
                catches=[("err", S(), [ExprStmt(span=S(), value=Call(
                    span=S(), func=nm("print"), args=[nm("err")]))])]),
        ])])
        self.assertEqual(resolve(p).diagnostics, [])

    def test_log_info_warn_error_resolve(self):
        # SPEC.md §12.1: `log.info/warn/error(msg)` calls exist, so
        # `log` must resolve as a builtin name.
        for call in ('log.info("x")', 'log.warn("x")',
                     'log.error("x")'):
            r = resolve_source(call + "\n", "<string>")
            self.assertEqual(r.diagnostics, [], call)

    def test_let_named_log_is_E3007(self):
        p = prog(stmts=[let("log", lit(1))])
        self.assertEqual(codes(resolve(p)), ["E3007"])

    def test_fn_named_t_is_E3007(self):
        p = prog(functions=[fn("t")])
        self.assertEqual(codes(resolve(p)), ["E3007"])

    def test_builtin_name_shadowed_is_E3007(self):
        p = prog(stmts=[let("print", lit(1))])
        self.assertEqual(codes(resolve(p)), ["E3007"])

    def test_fn_named_builtin_is_E3007(self):
        p = prog(functions=[fn("len")])
        self.assertEqual(codes(resolve(p)), ["E3007"])

    def test_param_named_user_is_E3007_everywhere(self):
        p = prog(functions=[fn("f", [param("user")])])
        self.assertEqual(codes(resolve(p)), ["E3007"])

    def test_duplicate_option_names_is_E3003(self):
        from compiler.ast_nodes import OptionDecl
        cmd = CommandDecl(span=S(), name="buy", options=[
            OptionDecl(span=S(), name="item", name_span=S(),
                       type=Type("str")),
            OptionDecl(span=S(), name="item", name_span=S(),
                       type=Type("str")),
        ], reply_expr=lit("ok"))
        p = prog(commands=[cmd])
        self.assertIn("E3003", codes(resolve(p)))

    def test_duplicate_enum_variant_is_E3003(self):
        p = prog(enums=[EnumDecl(span=S(), name="Color", variants=[
            EnumVariant(name="Red", span=S()),
            EnumVariant(name="Red", span=S())])])
        self.assertIn("E3003", codes(resolve(p)))


class TestHandlerBindings(unittest.TestCase):
    def _command(self, body):
        return CommandDecl(span=S(), name="ping", body=body)

    def test_command_body_sees_user_guild_channel(self):
        body = [ExprStmt(span=S(), value=Call(
            span=S(), func=nm("print"),
            args=[nm("user"), nm("guild"), nm("channel")]))]
        self.assertEqual(resolve(prog(commands=[self._command(body)]))
                         .diagnostics, [])

    def test_command_body_does_not_see_bot(self):
        body = [ExprStmt(span=S(), value=Call(
            span=S(), func=nm("print"), args=[nm("bot")]))]
        self.assertEqual(codes(resolve(prog(commands=[self._command(body)]))),
                         ["E3001"])

    def test_command_option_is_bound_in_body(self):
        from compiler.ast_nodes import OptionDecl
        cmd = CommandDecl(
            span=S(), name="buy",
            options=[OptionDecl(span=S(), name="item", name_span=S(),
                                type=Type("str"))],
            body=[ExprStmt(span=S(), value=Call(
                span=S(), func=nm("print"), args=[nm("item")]))])
        self.assertEqual(resolve(prog(commands=[cmd])).diagnostics, [])

    def test_let_shadowing_user_in_handler_is_E3007(self):
        body = [let("user", lit("x"))]
        self.assertEqual(codes(resolve(prog(commands=[self._command(body)]))),
                         ["E3007"])

    def test_member_join_body_sees_user_and_guild_not_channel(self):
        body = [ExprStmt(span=S(), value=Call(
            span=S(), func=nm("print"), args=[nm("user"), nm("guild")]))]
        ev = EventDecl(span=S(), kind="member_join", body=body)
        self.assertEqual(resolve(prog(events=[ev])).diagnostics, [])
        body2 = [ExprStmt(span=S(), value=Call(
            span=S(), func=nm("print"), args=[nm("channel")]))]
        ev2 = EventDecl(span=S(), kind="member_join", body=body2)
        self.assertEqual(codes(resolve(prog(events=[ev2]))), ["E3001"])

    def test_task_body_has_no_handler_bindings(self):
        body = [ExprStmt(span=S(), value=Call(
            span=S(), func=nm("print"), args=[nm("user")]))]
        p = prog(tasks=[TaskDecl(span=S(), interval_seconds=3600,
                                 body=body)])
        self.assertEqual(codes(resolve(p)), ["E3001"])

    def test_let_named_user_outside_handler_is_allowed(self):
        p = prog(stmts=[let("user", lit("x")),
                        ExprStmt(span=S(), value=Call(
                            span=S(), func=nm("print"), args=[nm("user")]))])
        self.assertEqual(resolve(p).diagnostics, [])


class TestWarnings(unittest.TestCase):
    def test_unused_variable_is_W1001_warning_not_error(self):
        p = prog(stmts=[let("unused", lit(1))])
        r = resolve(p)
        self.assertTrue(r.ok)
        self.assertEqual(codes(r), ["W1001"])

    def test_unused_param_is_W1001(self):
        p = prog(functions=[fn("f", [param("x")],
                                [Return(span=S(), value=lit(1))])])
        self.assertEqual(codes(resolve(p)), ["W1001"])

    def test_used_param_has_no_warning(self):
        p = prog(functions=[fn("f", [param("x")],
                                [Return(span=S(), value=nm("x"))])])
        self.assertEqual(resolve(p).diagnostics, [])

    def test_shadowing_outer_name_is_W1004(self):
        # The outer x is read after the if, so the only warning is the
        # shadowing one — the outer x is not also "unused".
        p = prog(stmts=[let("x", lit(1)),
                        If(span=S(), cond=lit(True),
                           then_body=[let("x", lit(2)),
                                      ExprStmt(span=S(), value=Call(
                                          span=S(), func=nm("print"),
                                          args=[nm("x")]))]),
                        ExprStmt(span=S(), value=Call(
                            span=S(), func=nm("print"), args=[nm("x")]))])
        r = resolve(p)
        self.assertEqual(codes(r), ["W1004"])

    def test_pub_top_level_let_is_not_warned_unused(self):
        p = prog(stmts=[let("exported", lit(1), pub=True)])
        self.assertEqual(resolve(p).diagnostics, [])


class TestFileImports(unittest.TestCase):
    def _write(self, d, name, text):
        path = Path(d) / name
        path.write_text(text, encoding="utf-8")
        return path

    def test_import_cycle_is_E3004_naming_the_chain(self):
        with tempfile.TemporaryDirectory() as d:
            self._write(d, "a.newr", 'import "b.newr"\n')
            self._write(d, "b.newr", 'import "a.newr"\n')
            r = resolve_file(Path(d) / "a.newr")
            self.assertIn("E3004", codes(r))
            self.assertIn("a.newr", r.diagnostics[0].message)
            self.assertIn("b.newr", r.diagnostics[0].message)

    def test_missing_import_file_is_E3008(self):
        with tempfile.TemporaryDirectory() as d:
            main = self._write(d, "main.newr", 'import "nope.newr"\n')
            r = resolve_file(main)
            self.assertIn("E3008", codes(r))

    def test_unknown_named_module_is_E3008(self):
        with tempfile.TemporaryDirectory() as d:
            main = self._write(d, "main.newr", 'import foo\n')
            r = resolve_file(main)
            self.assertIn("E3008", codes(r))

    def test_json_import_points_at_v1_1(self):
        with tempfile.TemporaryDirectory() as d:
            main = self._write(d, "main.newr", 'import json\n')
            r = resolve_file(main)
            self.assertIn("E3008", codes(r))
            self.assertIn("v1.1", r.diagnostics[0].message)

    def test_pub_access_ok_and_private_access_is_E3005(self):
        with tempfile.TemporaryDirectory() as d:
            self._write(d, "shop_items.newr",
                        "pub fn price_of(item: str) -> int:\n"
                        "  return 118\n"
                        "fn secret() -> int:\n"
                        "  return 0\n")
            main = self._write(
                d, "main.newr",
                'import "shop_items.newr"\n'
                'let p = shop_items.price_of("brick")\n'
                'print p\n')
            r = resolve_file(main)
            self.assertEqual(r.diagnostics, [])
            main2 = self._write(
                d, "main2.newr",
                'import "shop_items.newr"\n'
                'let s = shop_items.secret()\n'
                'print s\n')
            r2 = resolve_file(main2)
            self.assertIn("E3005", codes(r2))

    def test_qualified_unknown_member_is_E3001_with_help(self):
        with tempfile.TemporaryDirectory() as d:
            self._write(d, "shop_items.newr",
                        "pub fn price_of(item: str) -> int:\n"
                        "  return 118\n")
            main = self._write(
                d, "main.newr",
                'import "shop_items.newr"\n'
                'let p = shop_items.price_ofx("brick")\n'
                'print p\n')
            r = resolve_file(main)
            self.assertIn("E3001", codes(r))
            self.assertIn("price_of", r.diagnostics[0].help or "")

    def test_bot_in_imported_file_is_E3006(self):
        with tempfile.TemporaryDirectory() as d:
            self._write(d, "other.newr",
                        'bot "X":\n'
                        '  description: "x"\n')
            main = self._write(d, "main.newr", 'import "other.newr"\n')
            r = resolve_file(main)
            self.assertIn("E3006", codes(r))

    def test_top_level_statement_in_import_is_E3006(self):
        with tempfile.TemporaryDirectory() as d:
            self._write(d, "other.newr", 'print "hi"\n')
            main = self._write(d, "main.newr", 'import "other.newr"\n')
            r = resolve_file(main)
            self.assertIn("E3006", codes(r))

    def test_unused_import_is_W1002(self):
        with tempfile.TemporaryDirectory() as d:
            self._write(d, "other.newr",
                        "pub fn helper() -> int:\n"
                        "  return 1\n")
            main = self._write(d, "main.newr", 'import "other.newr"\n')
            r = resolve_file(main)
            self.assertTrue(r.ok)
            self.assertEqual(codes(r), ["W1002"])

    def test_import_alias_binds_the_alias(self):
        with tempfile.TemporaryDirectory() as d:
            self._write(d, "shop_items.newr",
                        "pub fn price_of(item: str) -> int:\n"
                        "  return 118\n")
            main = self._write(
                d, "main.newr",
                'import "shop_items.newr" as shop\n'
                'let p = shop.price_of("brick")\n'
                'print p\n')
            r = resolve_file(main)
            self.assertEqual(r.diagnostics, [])

    def test_files_list_records_main_then_imports(self):
        with tempfile.TemporaryDirectory() as d:
            self._write(d, "other.newr",
                        "pub fn helper() -> int:\n"
                        "  return 1\n")
            main = self._write(
                d, "main.newr",
                'import "other.newr"\n'
                'let x = other.helper()\n'
                'print x\n')
            r = resolve_file(main)
            self.assertEqual(r.diagnostics, [])
            self.assertEqual(len(r.program.files), 2)
            self.assertTrue(r.program.files[0].endswith("main.newr"))
            self.assertTrue(r.program.files[1].endswith("other.newr"))

    def test_nested_imports_fill_modules_at_each_level(self):
        with tempfile.TemporaryDirectory() as d:
            self._write(d, "c.newr",
                        "pub fn triple(x: int) -> int:\n"
                        "  return x * 3\n")
            self._write(d, "b.newr",
                        'import "c.newr"\n'
                        "pub fn use_it(x: int) -> int:\n"
                        "  return c.triple(x)\n")
            main = self._write(
                d, "main.newr",
                'import "b.newr"\n'
                "let y = b.use_it(2)\n"
                "print y\n")
            r = resolve_file(main)
            self.assertEqual(r.diagnostics, [])
            self.assertEqual(list(r.program.modules), ["b"])
            bprog = r.program.modules["b"]
            self.assertEqual(list(bprog.modules), ["c"])
            self.assertEqual(len(r.program.files), 3)
            self.assertTrue(r.program.files[0].endswith("main.newr"))
            self.assertTrue(r.program.files[1].endswith("b.newr"))
            self.assertTrue(r.program.files[2].endswith("c.newr"))


class TestResolveSource(unittest.TestCase):
    def test_hello_resolves(self):
        r = resolve_source('print "Hello, world!"\n', "<string>")
        self.assertEqual(r.diagnostics, [])

    def test_line_col_of_diagnostic(self):
        r = resolve_source('print nosuch\n', "<string>")
        self.assertEqual(codes(r), ["E3001"])
        src = 'print nosuch\n'
        line, col = line_col(src, r.diagnostics[0].span.start_byte)
        self.assertEqual((line, col), (1, 7))


if __name__ == "__main__":
    unittest.main()
