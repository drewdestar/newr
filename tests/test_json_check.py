"""Tests for the json checker (SPEC.md SS12.7, M8b).

Written FIRST (TDD RED). The helper runs the full front end:
resolve -> core check (with discord providers) -> discord check
-> db check -> http check -> json check.
"""

import unittest

from compiler import ast_nodes as A
from compiler.checker import check_program
from compiler.db_check import check_db
from compiler.discord_check import check_discord, discord_providers
from compiler.http_check import check_http
from compiler.json_check import check_json
from compiler.resolver import resolve_source


def full(src, allowlist=None):
    """All diagnostics from resolve + core check + discord + db +
    http + json."""
    resolved = resolve_source(src)
    diags = list(resolved.diagnostics)
    diags += check_program(resolved.program,
                           providers=discord_providers(resolved.program)
                           ).diagnostics
    diags += check_discord(resolved.program)
    diags += check_db(resolved.program)
    diags += check_http(resolved.program, allowlist=allowlist)
    diags += check_json(resolved.program)
    return diags


def core_only(src):
    """Diagnostics from resolve + core check only (no domain
    checker): the json rules must live in json_check, never in
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


TODO_STRUCT = """struct Todo:
  id: int
  title: str
  done: bool

"""


def main_body(body):
    return TODO_STRUCT + "fn main():\n" + body


class TestJsonHappyPath(unittest.TestCase):
    def test_full_happy_path(self):
        src = main_body(
            '  let todos: list[Todo] = json.decode("[]")\n'
            '  let text: str = json.encode(todos)\n'
            '  print(text)\n')
        self.assertEqual(errs(full(src)), [])

    def test_json_resolves_without_import(self):
        # Named modules are usable by name (SPEC.md SS12.2); "json"
        # is in RESERVED_BUILTIN_NAMES, so no E3001.
        src = main_body('  let text: str = json.encode(42)\n'
                        '  print(text)\n')
        self.assertEqual(errs(full(src)), [])

    def test_explicit_import(self):
        src = "import json\n\n" + main_body(
            '  let text: str = json.encode(42)\n'
            '  print(text)\n')
        self.assertEqual(errs(full(src)), [])

    def test_aliased_import(self):
        src = "import json as j\n\n" + main_body(
            '  let text: str = j.encode(42)\n'
            '  print(text)\n')
        self.assertEqual(errs(full(src)), [])

    def test_encode_scalars(self):
        src = main_body(
            '  print(json.encode(42))\n'
            '  print(json.encode(1.5))\n'
            '  print(json.encode("hi"))\n'
            '  print(json.encode(true))\n')
        self.assertEqual(errs(full(src)), [])

    def test_encode_list_and_dict(self):
        src = main_body(
            '  let xs: list[int] = [1, 2, 3]\n'
            '  print(json.encode(xs))\n'
            '  let d: dict[str, str] = {"a": "b"}\n'
            '  print(json.encode(d))\n')
        self.assertEqual(errs(full(src)), [])

    def test_encode_nested_struct(self):
        src = (TODO_STRUCT
               + "struct Shelf:\n"
               "  name: str\n"
               "  todos: list[Todo]\n"
               "\nfn main():\n"
               '  let todos: list[Todo] = json.decode("[]")\n'
               '  let shelf = Shelf(name: "s", todos: todos)\n'
               '  print(json.encode(shelf))\n')
        self.assertEqual(errs(full(src)), [])

    def test_encode_nullable(self):
        src = ("struct Note:\n"
               "  id: int\n"
               "  body: str?\n"
               "\nfn main():\n"
               '  let note = Note(id: 1, body: none)\n'
               '  print(json.encode(note))\n')
        self.assertEqual(errs(full(src)), [])

    def test_decode_scalars(self):
        src = main_body(
            '  let n: int = json.decode("42")\n'
            '  let f: float = json.decode("1.5")\n'
            '  let s: str = json.decode("\\"hi\\"")\n'
            '  let b: bool = json.decode("true")\n'
            '  print(n + 1)\n')
        self.assertEqual(errs(full(src)), [])

    def test_decode_list_and_dict(self):
        src = main_body(
            '  let xs: list[int] = json.decode("[1, 2]")\n'
            '  let d: dict[str, int] = json.decode("{}")\n'
            '  print(xs)\n'
            '  print(d)\n')
        # "{}" needs its braces escaped in a newr string: "{{}}".
        src = src.replace('json.decode("{}")', 'json.decode("{{}}")')
        self.assertEqual(errs(full(src)), [])

    def test_decode_nullable(self):
        src = ("struct Note:\n"
               "  id: int\n"
               "  body: str?\n"
               "\nfn main():\n"
               '  let note: Note? = json.decode("null")\n'
               '  print(note)\n')
        self.assertEqual(errs(full(src)), [])

    def test_decode_in_return_position(self):
        src = TODO_STRUCT + (
            "fn load(text: str) -> list[Todo]:\n"
            "  return json.decode(text)\n"
            "\nfn main():\n"
            '  let todos = load("[]")\n'
            '  print(todos)\n')
        self.assertEqual(errs(full(src)), [])

    def test_decode_as_function_argument(self):
        src = TODO_STRUCT + (
            "fn show(todos: list[Todo]):\n"
            "  print(todos)\n"
            "\nfn main():\n"
            '  show(json.decode("[]"))\n')
        self.assertEqual(errs(full(src)), [])

    def test_try_decode_with_annotation(self):
        # M8b follow-up (dart agent find): `try json.decode(...)`
        # with a Result annotation checks clean; the expected type
        # is read through the TryExpr.
        src = main_body(
            '  let tried: Result[list[Todo], error] = '
            'try json.decode("[]")\n'
            '  print(tried.is_ok())\n')
        self.assertEqual(errs(full(src)), [])

    def test_encode_sets_expr_type_for_emitters(self):
        src = main_body('  let text: str = json.encode(42)\n'
                        '  print(text)\n')
        resolved = resolve_source(src)
        check_program(resolved.program,
                      providers=discord_providers(resolved.program))
        check_json(resolved.program)
        found = []

        def walk(node):
            if isinstance(node, A.Call) and isinstance(node.func, A.Member) \
                    and node.func.name == "encode":
                found.append(node)
            if isinstance(node, (A.Expr, A.Stmt)):
                for value in vars(node).values():
                    walk(value)
            elif isinstance(node, (list, tuple)):
                for item in node:
                    walk(item)

        walk(resolved.program.functions[0].body)
        self.assertEqual(len(found), 1)
        self.assertEqual(str(found[0].expr_type), "str")

    def test_decode_sets_expr_type_for_emitters(self):
        src = main_body(
            '  let todos: list[Todo] = json.decode("[]")\n'
            '  print(todos)\n')
        resolved = resolve_source(src)
        check_program(resolved.program,
                      providers=discord_providers(resolved.program))
        check_json(resolved.program)
        found = []

        def walk(node):
            if isinstance(node, A.Call) and isinstance(node.func, A.Member) \
                    and node.func.name == "decode":
                found.append(node)
            if isinstance(node, (A.Expr, A.Stmt)):
                for value in vars(node).values():
                    walk(value)
            elif isinstance(node, (list, tuple)):
                for item in node:
                    walk(item)

        walk(resolved.program.functions[0].body)
        self.assertEqual(len(found), 1)
        self.assertEqual(str(found[0].expr_type), "list[Todo]")


class TestJsonErrors(unittest.TestCase):
    def test_unknown_member(self):
        d = full(main_body('  print(json.parse("[]"))\n'))
        self.assertIn("E2013", codes(d))
        self.assertIn("json has no function `parse`", messages(d))

    def test_unknown_member_suggests_similar_name(self):
        d = full(main_body('  print(json.encod(42))\n'))
        self.assertIn("E2013", codes(d))
        self.assertTrue(any("`encode`" in h for h in helps(d)))

    def test_named_argument_on_json_call(self):
        d = full(main_body('  print(json.encode(value: 42))\n'))
        self.assertIn("E2013", codes(d))

    def test_encode_arity(self):
        d = full(main_body('  print(json.encode())\n'))
        self.assertIn("E2013", codes(d))
        d = full(main_body('  print(json.encode(1, 2))\n'))
        self.assertIn("E2013", codes(d))

    def test_decode_arity(self):
        d = full(main_body('  print(json.decode())\n'))
        self.assertIn("E2013", codes(d))

    def test_encode_enum(self):
        src = """enum Color:
  Red
  Green

fn main():
  print(json.encode(Color.Red))
"""
        d = full(src)
        self.assertIn("E2013", codes(d))
        self.assertTrue(any("enum" in m for m in messages(d)))

    def test_encode_struct_with_enum_field(self):
        src = """enum Color:
  Red
  Green

struct Paint:
  name: str
  color: Color

fn main():
  let p = Paint(name: "x", color: Color.Red)
  print(json.encode(p))
"""
        d = full(src)
        self.assertIn("E2013", codes(d))
        self.assertTrue(any("field `color`" in m for m in messages(d)))

    def test_encode_set(self):
        src = main_body(
            '  let xs = set([1, 2])\n'
            '  print(json.encode(xs))\n')
        d = full(src)
        self.assertIn("E2013", codes(d))

    def test_encode_function_value(self):
        src = main_body(
            '  let f = fn(n: int) -> int: n + 1\n'
            '  print(json.encode(f))\n')
        d = full(src)
        self.assertIn("E2013", codes(d))

    def test_encode_result(self):
        src = main_body(
            '  let r = ok(42)\n'
            '  print(json.encode(r))\n')
        d = full(src)
        self.assertIn("E2013", codes(d))

    def test_decode_with_no_expected_type(self):
        d = full(main_body('  let x = json.decode("[]")\n'
                           '  print(x)\n'))
        self.assertIn("E2013", codes(d))
        self.assertTrue(any("needs an expected type" in m
                            for m in messages(d)))

    def test_decode_with_enum_expected_type(self):
        src = """enum Color:
  Red
  Green

fn main():
  let c: Color = json.decode("\\"Red\\"")
  print(c)
"""
        d = full(src)
        self.assertIn("E2013", codes(d))

    def test_decode_with_set_expected_type(self):
        src = main_body(
            '  let xs: set[int] = json.decode("[1, 2]")\n'
            '  print(xs)\n')
        d = full(src)
        self.assertIn("E2013", codes(d))

    def test_decode_struct_with_enum_field(self):
        src = """enum Color:
  Red
  Green

struct Paint:
  name: str
  color: Color

fn main():
  let p: Paint = json.decode("{}")
  print(p)
"""
        src = src.replace('json.decode("{}")', 'json.decode("{{}}")')
        d = full(src)
        self.assertIn("E2013", codes(d))

    def test_decode_arg_not_str(self):
        d = full(main_body(
            '  let xs: list[int] = json.decode(42)\n'
            '  print(xs)\n'))
        self.assertIn("E2013", codes(d))

    def test_json_as_value(self):
        d = full(main_body('  print(json.encode)\n'))
        self.assertIn("E2013", codes(d))


class TestJsonCoreBoundary(unittest.TestCase):
    """The core checker never reports a json rule: it returns
    UNKNOWN for json calls, and json_check does the real work."""

    def test_core_alone_is_quiet_for_valid_json(self):
        src = main_body(
            '  let todos: list[Todo] = json.decode("[]")\n'
            '  let text: str = json.encode(todos)\n'
            '  print(text)\n')
        self.assertEqual(errs(core_only(src)), [])

    def test_core_alone_is_quiet_for_invalid_json(self):
        # A decode with no expected type is E2013 from json_check
        # only; core must not report it.
        src = main_body('  let x = json.decode("[]")\n'
                        '  print(x)\n')
        self.assertEqual(errs(core_only(src)), [])
        self.assertIn("E2013", codes(full(src)))


class TestJsonDocs(unittest.TestCase):
    """C6: `newr docs` renders the json surface into docs/api.md with
    no omissions."""

    def test_every_json_member_is_documented(self):
        from compiler.docs import api_markdown
        from compiler.json_limits import JSON_MEMBERS
        text = api_markdown()
        for name in JSON_MEMBERS:
            self.assertIn(f"`json.{name}`", text, f"json.{name} missing")

    def test_every_json_member_has_doc_and_sig(self):
        from compiler.json_limits import JSON_MEMBERS
        for name, entry in JSON_MEMBERS.items():
            self.assertTrue(entry.get("doc"), f"json.{name} has no doc")
            self.assertTrue(entry.get("sig"), f"json.{name} has no sig")


class TestJsonTestBlocks(unittest.TestCase):
    """A test block is lowered to a synthetic `_newr_test_<n>`
    function sharing the body list: json_check must walk it once,
    via test_blocks, not twice (M8d follow-up, ported from
    ui_check.py via http_check.py)."""

    def test_valid_json_in_test_block_has_no_errors(self):
        src = ('test "json in test":\n'
               '  let s = json.encode([1, 2, 3])\n'
               '  let xs: list[int] = json.decode(s)\n'
               '  print(xs)\n')
        self.assertEqual(errs(full(src)), [])

    def test_encode_enum_in_test_block_reports_once(self):
        # Before the fix this reported twice: the functions loop
        # walked the synthetic `_newr_test_0`, then the
        # test_blocks walk found the encode again.
        src = """enum Color:
  Red
  Green

test "encode enum in test":
  let c = Color.Red
  let s = json.encode(c)
  print(s)
"""
        diags = errs(full(src))
        self.assertEqual(len(diags), 1)
        self.assertEqual(codes(diags), ["E2013"])


if __name__ == "__main__":
    unittest.main()
