"""Tests for the db checker (SPEC.md SS12.5, M8a).

Written FIRST (TDD RED). The helper runs the full front end:
resolve -> core check (with discord providers) -> discord check
-> db check.
"""

import unittest

from compiler import ast_nodes as A
from compiler.checker import check_program
from compiler.db_check import check_db
from compiler.discord_check import check_discord, discord_providers
from compiler.resolver import resolve_source


def full(src):
    """All diagnostics from resolve + core check + discord + db."""
    resolved = resolve_source(src)
    diags = list(resolved.diagnostics)
    diags += check_program(resolved.program,
                           providers=discord_providers(resolved.program)
                           ).diagnostics
    diags += check_discord(resolved.program)
    diags += check_db(resolved.program)
    return diags


def codes(diags):
    return [d.code for d in diags]


def errs(diags):
    return [d for d in diags if d.is_error()]


def messages(diags):
    return [d.message for d in errs(diags)]


TODO_STRUCT = """struct Todo:
  id: int
  title: str
  done: bool

"""

OPEN = '  let h = db.open(":memory:")\n'

#: The same open, at top level (no indent): emit-and-run tests use
#: top-level statements, since newr runs the top level and `fn main`
#: is not an entry point.
TOP_OPEN = 'let h = db.open(":memory:")\n'


def main_body(body):
    return TODO_STRUCT + "fn main():\n" + body


class TestDbHappyPath(unittest.TestCase):
    def test_full_happy_path(self):
        src = main_body(
            OPEN
            + '  db.exec(h, "CREATE TABLE todos (id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL, done INTEGER NOT NULL)")\n'
            + '  let n = db.exec(h, "INSERT INTO todos (title, done) VALUES (?, ?)", "beli susu", false)\n'
            + '  print(n)\n'
            + '  let todos: list[Todo] = db.query(h, "SELECT id, title, done FROM todos")\n'
            + '  print(todos)\n'
            + '  db.begin(h)\n'
            + '  db.exec(h, "UPDATE todos SET done = ? WHERE id = ?", true, 1)\n'
            + '  db.commit(h)\n'
            + '  let id = db.last_id(h)\n'
            + '  print(id)\n'
            + '  db.close(h)\n')
        self.assertEqual(errs(full(src)), [])

    def test_db_resolves_without_import(self):
        # Named modules are usable by name (SPEC.md SS12.2); "db"
        # is in RESERVED_BUILTIN_NAMES, so no E3001.
        self.assertEqual(errs(full("fn main():\n" + OPEN)), [])

    def test_explicit_import(self):
        src = "import db\n\nfn main():\n" + OPEN + '  db.close(h)\n'
        self.assertEqual(errs(full(src)), [])

    def test_aliased_import(self):
        src = "import db as d\n\nfn main():\n" \
            + '  let h = d.open(":memory:")\n  d.close(h)\n'
        self.assertEqual(errs(full(src)), [])

    def test_query_in_return_position(self):
        src = TODO_STRUCT + (
            "fn all(h: int) -> list[Todo]:\n"
            '  return db.query(h, "SELECT id, title, done FROM todos")\n'
            "\nfn main():\n" + OPEN
            + "  let todos = all(h)\n  print(todos)\n")
        self.assertEqual(errs(full(src)), [])

    def test_query_as_function_argument(self):
        src = TODO_STRUCT + (
            "fn show(todos: list[Todo]):\n"
            "  print(todos)\n"
            "\nfn main():\n" + OPEN
            + '  show(db.query(h, "SELECT id, title, done FROM todos"))\n')
        self.assertEqual(errs(full(src)), [])

    def test_try_catch_rollback_pattern(self):
        # The canonical pattern: commit in try, rollback in catch.
        # Neither the rollback nor a later begin may be flagged.
        src = main_body(
            OPEN
            + '  db.begin(h)\n'
            + '  try:\n'
            + '    db.exec(h, "INSERT INTO todos (title) VALUES (?)", "x")\n'
            + '    db.commit(h)\n'
            + '  catch e:\n'
            + '    db.rollback(h)\n'
            + '  db.close(h)\n')
        self.assertEqual(errs(full(src)), [])

    def test_if_else_both_begin_is_not_a_double_begin(self):
        # Each branch begins at most once; the branches are
        # exclusive, so no double begin.
        src = main_body(
            OPEN
            + '  if true:\n'
            + '    db.begin(h)\n'
            + '  else:\n'
            + '    db.begin(h)\n'
            + '  db.commit(h)\n')
        self.assertEqual(errs(full(src)), [])

    def test_quoted_question_mark_is_not_a_placeholder(self):
        src = main_body(
            OPEN
            + '  let todos: list[Todo] = db.query(h, "SELECT id FROM todos WHERE title = \'?\'")\n'
            + '  print(todos)\n')
        self.assertEqual(errs(full(src)), [])

    def test_nullable_row_field(self):
        src = ("struct Note:\n"
               "  id: int\n"
               "  body: str?\n"
               "\nfn main():\n" + OPEN
               + '  let notes: list[Note] = db.query(h, "SELECT id, body FROM notes")\n'
               + '  print(notes)\n')
        self.assertEqual(errs(full(src)), [])

    def test_query_sets_expr_type_for_emitters(self):
        src = main_body(
            OPEN
            + '  let todos: list[Todo] = db.query(h, "SELECT id, title, done FROM todos")\n'
            + '  print(todos)\n')
        resolved = resolve_source(src)
        check_program(resolved.program,
                      providers=discord_providers(resolved.program))
        check_db(resolved.program)
        found = []

        def walk(node):
            if isinstance(node, A.Call) and isinstance(node.func, A.Member) \
                    and node.func.name == "query":
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


class TestDbErrors(unittest.TestCase):
    def test_unknown_member(self):
        d = full("fn main():\n" + OPEN + '  db.foo(h)\n')
        self.assertIn("E2011", codes(d))
        self.assertIn("db has no function `foo`", messages(d))

    def test_unknown_member_suggests_similar_name(self):
        d = full("fn main():\n" + OPEN + '  db.opne(":memory:")\n')
        self.assertIn("E2011", codes(d))
        self.assertTrue(any("similar name" in (x.help or "")
                            and "`open`" in (x.help or "")
                            for x in errs(d)))

    def test_open_arity(self):
        d = full("fn main():\n  let h = db.open()\n  print(h)\n")
        self.assertIn("E2011", codes(d))

    def test_exec_needs_handle_and_sql(self):
        d = full("fn main():\n" + OPEN + '  db.exec(h)\n')
        self.assertIn("E2011", codes(d))

    def test_handle_must_be_int(self):
        d = full("fn main():\n"
                 + '  db.exec("not a handle", "SELECT 1")\n')
        self.assertIn("E2011", codes(d))

    def test_sql_must_be_str(self):
        d = full("fn main():\n" + OPEN + '  db.exec(h, 42)\n')
        self.assertIn("E2011", codes(d))

    def test_placeholder_mismatch_too_few_values(self):
        d = full("fn main():\n" + OPEN
                 + '  db.exec(h, "INSERT INTO t VALUES (?, ?)", 1)\n')
        self.assertIn("E2011", codes(d))
        self.assertTrue(any("2 `?` placeholder" in m for m in messages(d)))

    def test_placeholder_mismatch_too_many_values(self):
        d = full("fn main():\n" + OPEN
                 + '  db.exec(h, "SELECT 1", 1, 2)\n')
        self.assertIn("E2011", codes(d))

    def test_bind_none_is_an_error(self):
        d = full("fn main():\n" + OPEN
                 + '  db.exec(h, "INSERT INTO t VALUES (?)", none)\n')
        self.assertIn("E2011", codes(d))

    def test_bind_list_is_an_error(self):
        d = full("fn main():\n" + OPEN
                 + '  db.exec(h, "INSERT INTO t VALUES (?)", [1])\n')
        self.assertIn("E2011", codes(d))

    def test_bind_nullable_is_an_error(self):
        src = ("fn main():\n" + OPEN
               + "  let name: str? = none\n"
               + '  db.exec(h, "INSERT INTO t VALUES (?)", name)\n')
        d = full(src)
        self.assertIn("E2011", codes(d))

    def test_query_with_no_expected_type(self):
        d = full(main_body(
            OPEN
            + '  let todos = db.query(h, "SELECT id FROM todos")\n'
            + '  print(todos)\n'))
        self.assertIn("E2011", codes(d))
        self.assertTrue(any("needs an expected row type" in m
                            for m in messages(d)))

    def test_query_with_wrong_expected_shape(self):
        d = full("fn main():\n" + OPEN
                 + '  let n: int = db.query(h, "SELECT 1")\n'
                 + '  print(n)\n')
        self.assertIn("E2011", codes(d))

    def test_query_with_list_int_expected(self):
        d = full("fn main():\n" + OPEN
                 + '  let ns: list[int] = db.query(h, "SELECT 1")\n'
                 + '  print(ns)\n')
        self.assertIn("E2011", codes(d))

    def test_query_undeclared_struct_is_core_error_only(self):
        # The unknown struct is E3001 from the core checker; db
        # stays quiet rather than piling on.
        d = full("fn main():\n" + OPEN
                 + '  let rows: list[Nope] = db.query(h, "SELECT 1")\n'
                 + '  print(rows)\n')
        self.assertIn("E3001", codes(d))
        self.assertNotIn("E2011", codes(d))

    def test_row_struct_with_list_field(self):
        src = ("struct Bad:\n"
               "  id: int\n"
               "  tags: list[str]\n"
               "\nfn main():\n" + OPEN
               + '  let rows: list[Bad] = db.query(h, "SELECT id, tags FROM t")\n'
               + '  print(rows)\n')
        d = full(src)
        self.assertIn("E2011", codes(d))

    def test_double_begin(self):
        d = full("fn main():\n" + OPEN
                 + '  db.begin(h)\n'
                 + '  db.begin(h)\n')
        self.assertIn("E2011", codes(d))
        self.assertTrue(any("already in a transaction" in m
                            for m in messages(d)))

    def test_use_after_close(self):
        d = full("fn main():\n" + OPEN
                 + '  db.close(h)\n'
                 + '  db.exec(h, "SELECT 1")\n')
        self.assertIn("E2011", codes(d))
        self.assertTrue(any("already closed" in m for m in messages(d)))

    def test_close_twice(self):
        d = full("fn main():\n" + OPEN
                 + '  db.close(h)\n'
                 + '  db.close(h)\n')
        self.assertIn("E2011", codes(d))

    def test_db_function_as_value(self):
        d = full("fn main():\n  let f = db.open\n  print(f)\n")
        self.assertIn("E2011", codes(d))

    def test_named_argument_on_db_call(self):
        d = full("fn main():\n" + OPEN
                 + '  db.exec(h, "SELECT 1", x: 1)\n')
        self.assertIn("E2011", codes(d))


class TestDbDocs(unittest.TestCase):
    """C7: `newr docs` renders the db surface into docs/api.md with
    no omissions."""

    def test_every_db_member_is_documented(self):
        from compiler.db_limits import DB_MEMBERS
        from compiler.docs import api_markdown
        text = api_markdown()
        for name in DB_MEMBERS:
            self.assertIn(f"`db.{name}`", text, f"db.{name} missing")

    def test_every_db_member_has_doc_and_sig(self):
        from compiler.db_limits import DB_MEMBERS
        for name, entry in DB_MEMBERS.items():
            self.assertTrue(entry.get("doc"), f"db.{name} has no doc")
            self.assertTrue(entry.get("sig"), f"db.{name} has no sig")


class TestDbEmitAndRun(unittest.TestCase):
    """B1 regression: db return values must be usable, not just
    checkable. The core checker used to return UNKNOWN for every
    db call, so `let n = db.exec(...)` bound `n` with no type and
    the emitter died with E4001 on `print(n)` even though `newr
    check` passed. The fixed return types in the core checker
    (M8a) make the values real for the emitters."""

    def _run(self, src):
        from pathlib import Path
        import subprocess
        import sys
        import tempfile
        from compiler.emitter import emit_program
        root = Path(__file__).resolve().parent.parent
        resolved = resolve_source(src, "<db-test>.newr")
        diags = list(resolved.diagnostics)
        diags += check_program(
            resolved.program,
            providers=discord_providers(resolved.program)).diagnostics
        diags += check_discord(resolved.program)
        diags += check_db(resolved.program)
        bad = [d for d in diags if d.is_error()]
        self.assertEqual(bad, [], [f"{d.code}: {d.message}" for d in bad])
        result = emit_program(resolved.program,
                              main_path="<db-test>.newr",
                              sources={"<db-test>.newr": src})
        with tempfile.TemporaryDirectory() as d:
            for name, text in result.files.items():
                (Path(d) / name).write_text(text, encoding="utf-8")
            if result.uses_rt:
                (Path(d) / "newr_rt.py").write_text(
                    (root / "compiler" / "newr_rt.py").read_text(
                        encoding="utf-8"), encoding="utf-8")
            proc = subprocess.run(
                [sys.executable, str(Path(d) / result.main_file)],
                capture_output=True, text=True, cwd=d)
            return proc.stdout, proc.stderr, proc.returncode

    def test_exec_result_prints(self):
        # Top-level statements: newr runs the top level, `fn main`
        # is not an entry point, so a probe wrapped in `fn main`
        # would emit but never run.
        src = (TODO_STRUCT
               + TOP_OPEN
               + 'db.exec(h, "CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")\n'
               + 'let n = db.exec(h, "INSERT INTO t (v) VALUES (?)", "x")\n'
               + 'print(n)\n'
               + 'let id = db.last_id(h)\n'
               + 'print(id)\n'
               + 'db.close(h)\n')
        out, err, code = self._run(src)
        self.assertEqual(code, 0, err)
        self.assertEqual(out, "1\n1\n")

    def test_query_rows_print(self):
        src = (TODO_STRUCT
               + TOP_OPEN
               + 'db.exec(h, "CREATE TABLE todos (id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL, done INTEGER NOT NULL)")\n'
               + 'db.exec(h, "INSERT INTO todos (title, done) VALUES (?, ?)", "beli susu", false)\n'
               + 'let todos: list[Todo] = db.query(h, "SELECT id, title, done FROM todos")\n'
               + 'print(todos)\n'
               + 'db.close(h)\n')
        out, err, code = self._run(src)
        self.assertEqual(code, 0, err)
        self.assertIn("beli susu", out)
        self.assertIn("Todo", out)

    def test_transaction_rollback(self):
        src = (TODO_STRUCT
               + TOP_OPEN
               + 'db.exec(h, "CREATE TABLE todos (id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL, done INTEGER NOT NULL)")\n'
               + 'db.exec(h, "INSERT INTO todos (title, done) VALUES (?, ?)", "keep", false)\n'
               + 'db.begin(h)\n'
               + 'db.exec(h, "INSERT INTO todos (title, done) VALUES (?, ?)", "drop", false)\n'
               + 'db.rollback(h)\n'
               + 'let todos: list[Todo] = db.query(h, "SELECT id, title, done FROM todos")\n'
               + 'print(todos)\n'
               + 'db.close(h)\n')
        out, err, code = self._run(src)
        self.assertEqual(code, 0, err)
        self.assertIn("keep", out)
        self.assertNotIn("drop", out)


class TestDbTestBlocks(unittest.TestCase):
    """A test block is lowered to a synthetic `_newr_test_<n>`
    function sharing the body list: db_check must walk it once,
    via test_blocks, not twice (M8d follow-up, ported from
    ui_check.py via http_check.py)."""

    def test_valid_db_in_test_block_has_no_errors(self):
        src = (TODO_STRUCT + 'test "db in test":\n'
               + '  let h = db.open(":memory:")\n'
               + '  db.exec(h, "CREATE TABLE todos (id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL, done INTEGER NOT NULL)")\n'
               + '  db.begin(h)\n'
               + '  let todos: list[Todo] = db.query(h, "SELECT id, title, done FROM todos")\n'
               + '  print(todos)\n'
               + '  db.commit(h)\n'
               + '  db.close(h)\n')
        self.assertEqual(errs(full(src)), [])

    def test_double_begin_in_test_block_reports_once(self):
        # Before the fix this reported twice: the functions loop
        # walked the synthetic `_newr_test_0`, then the
        # test_blocks walk found the second begin again.
        src = ('test "double begin in test":\n'
               + '  let h = db.open(":memory:")\n'
               + '  db.begin(h)\n'
               + '  db.begin(h)\n')
        diags = errs(full(src))
        self.assertEqual(len(diags), 1)
        self.assertEqual(codes(diags), ["E2011"])


if __name__ == "__main__":
    unittest.main()
