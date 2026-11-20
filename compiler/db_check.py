"""db checker for the newr language (SPEC.md SS12.5).

Owns ALL db rules: the surface in ``db_limits.DB_MEMBERS``, the
``?`` placeholder count for literal SQL, the variadic argument type
check, the expected-type inference for ``db.query`` (struct fields
matched by name), and the transaction pairing rule (a second
``db.begin`` without commit or rollback is an error). This is the
ONLY compiler module (besides the CLI, which composes the checkers,
and ``docs.py``, which renders the surface) that imports
``db_limits`` -- the core checker never does (SPEC.md SS2.1).

Public API
----------
``check_db(program) -> list[Diagnostic]``
    db diagnostics for a resolver-output Program (run AFTER the
    core checker, so ``expr_type`` is filled in on expressions).

``check_db`` also enriches the AST: a ``db.query`` call whose row
type it resolves gets ``expr_type = list[RowStruct]`` set on the
Call node, so the emitters can read the row type without
re-deriving it. Emission is refused without the full check
(D27), so the emitters can rely on that field.

CLI composition seam (see also ``checker.check_program``)::

    resolved = resolve_file(path)                 # or resolve_source
    providers = discord_providers(resolved.program)
    core = check_program(resolved.program, providers=providers)
    discord_diags = check_discord(resolved.program)
    db_diags = check_db(resolved.program)
    diagnostics = resolved.diagnostics + core.diagnostics + discord_diags + db_diags

Error-code map for the codes this module introduces
--------------------------------------------------
E2011  db rule violation (SPEC.md SS12.5): unknown db member, wrong
       argument count or type, a non-bindable bound value, a
       placeholder count mismatch, a ``db.query`` with no expected
       row type (or a row type that is not a declared struct, or a
       struct field that cannot be read from a row), a second
       ``db.begin`` on a handle that is already in a transaction,
       or a use of a closed handle.
"""

from __future__ import annotations

import difflib
from dataclasses import fields as _dc_fields
from dataclasses import is_dataclass

from . import ast_nodes as A
from .ast_nodes import (
    BUILTIN_TYPE_NAMES, INT, NONE_TYPE, Type, list_of,
)
from .db_limits import (
    DB_BINDABLE_TYPES, DB_ERROR_CODE, DB_FUNCTIONS, DB_MEMBERS,
    DB_ROW_FIELD_TYPES,
)
from .errors import MAX_NESTING_DEPTH, Diagnostic, Span

# How deep the _walk_stmt / _walk_expr recursion may go (M-01): past
# this the walk stops silently, the core checker already reported
# E1001 for this program.
_DEPTH_LIMIT = MAX_NESTING_DEPTH


def _count_placeholders(sql: str) -> int:
    """Count ``?`` placeholders in ``sql``, skipping ``?`` inside
    single- or double-quoted SQL text (a literal question mark, not
    a placeholder). ``''`` is the SQL escape for a quote inside a
    quoted string."""
    count = 0
    i = 0
    n = len(sql)
    in_single = False
    in_double = False
    while i < n:
        c = sql[i]
        if in_single:
            if c == "'":
                if i + 1 < n and sql[i + 1] == "'":
                    i += 2
                    continue
                in_single = False
        elif in_double:
            if c == '"':
                in_double = False
        elif c == "'":
            in_single = True
        elif c == '"':
            in_double = True
        elif c == "?":
            count += 1
        i += 1
    return count


class DbChecker:
    def __init__(self, program: A.Program):
        self.program = program
        self.diagnostics: list[Diagnostic] = []
        self._depth = 0
        self._structs: dict[str, A.StructDecl] = {}
        self._functions: dict[str, A.FnDecl] = {}
        self._db_aliases: set[str] = {"db"}
        self._return_type: Type | None = None
        # Per-body handle state, reset for every function / test
        # block / top-level walk: var name -> in a transaction.
        self._handles: dict[str, bool] = {}
        # Var names whose handle was closed in this body.
        self._closed: set[str] = set()
        # Var name -> declared type (let annotation or parameter),
        # for the Assign expected type and for handle aliasing.
        self._var_types: dict[str, Type] = {}
        programs = [program] + list(program.modules.values())
        for prog in programs:
            for st in prog.structs:
                self._structs[st.name] = st
            for fn in prog.functions:
                self._functions[fn.name] = fn
            for imp in prog.imports:
                if imp.module == "db":
                    self._db_aliases.add(imp.bound_name)

    # ------------------------------------------------------------ diagnostics

    def _error(self, span: Span, message: str,
               help: str | None = None) -> None:
        self.diagnostics.append(Diagnostic(
            span=span, code=DB_ERROR_CODE, message=message,
            severity="error", help=help))

    # ------------------------------------------------------------ entry point

    def run(self) -> list[Diagnostic]:
        prog = self.program
        for sub in [prog] + list(prog.modules.values()):
            # A test block is lowered to a synthetic function
            # `_newr_test_<n>` in sub.functions sharing the same
            # body list, so skip it here: it is walked below via
            # sub.test_blocks. Walking it twice would report every
            # db error in a test block twice. (http_check.py has
            # the same skip; unlike ui, db has no rule that depends
            # on being inside a test block, so there is no _in_test
            # flag to set here.)
            test_fn_names = {tb.func_name for tb in sub.test_blocks}
            for fn in sub.functions:
                if fn.name in test_fn_names:
                    continue
                self._walk_function(fn)
            for tb in sub.test_blocks:
                self._reset_body_state()
                self._return_type = None
                self._walk_body(tb.body)
            self._reset_body_state()
            self._return_type = None
            for st in sub.stmts:
                self._walk_stmt(st)
        for cmd in prog.commands:
            self._reset_body_state()
            self._return_type = None
            self._walk_body(cmd.effective_body())
        for ev in prog.events:
            self._reset_body_state()
            self._return_type = None
            self._walk_body(ev.effective_body())
        for task in prog.tasks:
            self._reset_body_state()
            self._return_type = None
            self._walk_body(task.body)
        return self.diagnostics

    def _reset_body_state(self) -> None:
        self._handles = {}
        self._closed = set()
        self._var_types = {}

    def _walk_function(self, fn: A.FnDecl) -> None:
        self._reset_body_state()
        for p in fn.params:
            self._var_types[p.name] = p.type
        self._return_type = fn.return_type
        self._walk_body(fn.body)
        self._return_type = None

    # ------------------------------------------------------------ handle state

    def _snapshot(self) -> tuple[dict[str, bool], set[str],
                                 dict[str, Type]]:
        return (dict(self._handles), set(self._closed),
                dict(self._var_types))

    def _restore(self, snap: tuple[dict[str, bool], set[str],
                                   dict[str, Type]]) -> None:
        self._handles, self._closed, self._var_types = (
            dict(snap[0]), set(snap[1]), dict(snap[2]))

    def _merge(self, ends: list[tuple[dict[str, bool], set[str],
                                      dict[str, Type]]],
               pre: tuple[dict[str, bool], set[str],
                          dict[str, Type]]) -> None:
        """Merge the end states of exclusive branches (if arms, loop
        taken-or-not, try body-or-catch). A handle is in a
        transaction afterwards only when EVERY branch agrees it is;
        a handle is closed afterwards only when EVERY branch closed
        it. Anything a branch disagrees on becomes unknown, so a
        later ``db.begin`` is never flagged on a maybe. Lets inside
        a branch never escape it, so var types reset to ``pre``."""
        if not ends:
            self._restore(pre)
            return
        merged_handles: dict[str, bool] = {}
        names = set(ends[0][0])
        for snap in ends[1:]:
            names &= set(snap[0])
        for name in names:
            values = {snap[0][name] for snap in ends}
            if len(values) == 1:
                merged_handles[name] = values.pop()
        merged_closed = set(ends[0][1])
        for snap in ends[1:]:
            merged_closed &= snap[1]
        self._handles = merged_handles
        self._closed = merged_closed
        self._var_types = dict(pre[2])

    def _register_handle(self, name: str, value: A.Expr | None) -> None:
        """After ``let name = <value>`` / ``name = <value>``: record a
        handle opened by ``db.open``, or an alias of a known handle."""
        if value is None:
            return
        if self._is_db_call(value, "open"):
            self._handles[name] = False
            self._closed.discard(name)
            return
        if isinstance(value, A.Name):
            if value.name in self._handles:
                self._handles[name] = self._handles[value.name]
                self._closed.discard(name)
            elif value.name in self._closed:
                self._handles.pop(name, None)
                self._closed.add(name)

    # ------------------------------------------------------------ walking

    def _walk_body(self, body: list) -> None:
        for st in body:
            self._walk_stmt(st)

    def _walk_stmt(self, st) -> None:
        # M-01: stop silently past the nesting limit, the core
        # checker already reported E1001 for this program.
        self._depth += 1
        if self._depth > _DEPTH_LIMIT:
            self._depth -= 1
            return
        try:
            self._walk_stmt_inner(st)
        finally:
            self._depth -= 1

    def _walk_stmt_inner(self, st) -> None:
        if isinstance(st, A.If):
            if st.cond is not None:
                self._walk_expr(st.cond, None)
            pre = self._snapshot()
            ends = []
            self._restore(pre)
            self._walk_body(st.then_body)
            ends.append(self._snapshot())
            for cond, body in st.elifs:
                self._restore(pre)
                if cond is not None:
                    self._walk_expr(cond, None)
                self._walk_body(body)
                ends.append(self._snapshot())
            if st.else_body is not None:
                self._restore(pre)
                self._walk_body(st.else_body)
                ends.append(self._snapshot())
            else:
                ends.append(pre)
            self._merge(ends, pre)
        elif isinstance(st, (A.For, A.While)):
            if isinstance(st, A.For) and st.iterable is not None:
                self._walk_expr(st.iterable, None)
            if isinstance(st, A.While) and st.cond is not None:
                self._walk_expr(st.cond, None)
            # A loop body runs zero or more times: the state after
            # is the merge of taking it and skipping it.
            pre = self._snapshot()
            self._restore(pre)
            self._walk_body(st.body)
            self._merge([pre, self._snapshot()], pre)
        elif isinstance(st, A.Try):
            pre = self._snapshot()
            self._restore(pre)
            self._walk_body(st.body)
            ends = [self._snapshot()]
            for _name, _span, body in st.catches:
                self._restore(pre)
                self._walk_body(body)
                ends.append(self._snapshot())
            self._merge(ends, pre)
            if st.finally_body is not None:
                # `finally` always runs last, on top of the merged
                # body-or-catch state.
                self._walk_body(st.finally_body)
        elif isinstance(st, A.Match):
            if st.scrutinee is not None:
                self._walk_expr(st.scrutinee, None)
            pre = self._snapshot()
            ends = []
            for arm in st.arms:
                self._restore(pre)
                if arm.guard is not None:
                    self._walk_expr(arm.guard, None)
                self._walk_body(arm.body)
                ends.append(self._snapshot())
            self._merge(ends, pre) if ends else self._restore(pre)
        elif isinstance(st, (A.Let, A.Const)):
            if st.type_ann is not None:
                self._var_types[st.name] = st.type_ann
            if st.value is not None:
                self._walk_expr(st.value, st.type_ann)
                self._register_handle(st.name, st.value)
        elif isinstance(st, A.Assign):
            expected = self._var_types.get(st.name)
            if st.value is not None:
                self._walk_expr(st.value, expected)
                self._register_handle(st.name, st.value)
        elif isinstance(st, A.Return):
            if st.value is not None:
                self._walk_expr(st.value, self._return_type)
        elif isinstance(st, A.ExprStmt):
            if st.value is not None:
                self._walk_expr(st.value, None)
        elif isinstance(st, (A.Reply, A.Edit, A.Followup)):
            if st.value is not None:
                self._walk_expr(st.value, None)
        elif isinstance(st, A.Send):
            if st.value is not None:
                self._walk_expr(st.value, None)
            if st.channel is not None:
                self._walk_expr(st.channel, None)
        elif isinstance(st, A.Throw):
            if st.value is not None:
                self._walk_expr(st.value, None)
        elif isinstance(st, A.Log):
            if st.value is not None:
                self._walk_expr(st.value, None)
        elif isinstance(st, (A.Require, A.Ensure)):
            if st.cond is not None:
                self._walk_expr(st.cond, None)
        else:
            # Any other statement (components, break, continue,
            # pass): walk whatever expressions it carries.
            self._walk_generic(st)

    def _walk_expr(self, expr, expected: Type | None) -> None:
        # M-01: stop silently past the nesting limit, the core
        # checker already reported E1001 for this program.
        self._depth += 1
        if self._depth > _DEPTH_LIMIT:
            self._depth -= 1
            return
        try:
            self._walk_expr_inner(expr, expected)
        finally:
            self._depth -= 1

    def _walk_expr_inner(self, expr, expected: Type | None) -> None:
        if expr is None:
            return
        if isinstance(expr, A.Call):
            if self._is_db_call(expr):
                self._check_db_call(expr, expected)
                return
            func = expr.func
            if isinstance(func, A.Name) and func.name in self._functions:
                fn = self._functions[func.name]
                for i, arg in enumerate(expr.args):
                    arg_expected = None
                    if i < len(fn.params):
                        arg_expected = fn.params[i].type
                    self._walk_expr(arg, arg_expected)
                for _name, value, _span in expr.kwargs:
                    self._walk_expr(value, None)
                return
            self._walk_expr(func, None)
            for arg in expr.args:
                self._walk_expr(arg, None)
            for _name, value, _span in expr.kwargs:
                self._walk_expr(value, None)
        elif isinstance(expr, A.Member):
            if isinstance(expr.obj, A.Name) \
                    and expr.obj.name in self._db_aliases:
                self._error(expr.span,
                            f"`db.{expr.name}` is a function, not a value",
                            help="call it, for example "
                                 f"`db.{expr.name}(...)`")
                return
            if expr.obj is not None:
                self._walk_expr(expr.obj, None)
        elif isinstance(expr, A.TCall):
            for _name, value, _span in expr.args:
                self._walk_expr(value, None)
        elif isinstance(expr, A.BinOp):
            self._walk_expr(expr.left, None)
            self._walk_expr(expr.right, None)
        elif isinstance(expr, A.UnaryOp):
            self._walk_expr(expr.operand, None)
        elif isinstance(expr, A.Ternary):
            self._walk_expr(expr.cond, None)
            self._walk_expr(expr.then_expr, expected)
            self._walk_expr(expr.else_expr, expected)
        elif isinstance(expr, A.TryExpr):
            self._walk_expr(expr.expr, expected)
        elif isinstance(expr, A.Propagate):
            self._walk_expr(expr.expr, expected)
        elif isinstance(expr, A.Lambda):
            saved = self._return_type
            self._return_type = expr.return_type
            try:
                if expr.expr_body is not None:
                    self._walk_expr(expr.expr_body, expr.return_type)
                if expr.body is not None:
                    self._walk_body(expr.body)
            finally:
                self._return_type = saved
        elif isinstance(expr, A.Index):
            self._walk_expr(expr.obj, None)
            self._walk_expr(expr.index, None)
        elif isinstance(expr, A.Interp):
            for part in expr.parts:
                if part.expr is not None:
                    self._walk_expr(part.expr, None)
        elif isinstance(expr, A.ListLit):
            elem_expected = None
            if expected is not None and expected.name == "list" \
                    and expected.params:
                elem_expected = expected.params[0]
            for el in expr.elements:
                self._walk_expr(el, elem_expected)
        elif isinstance(expr, A.DictLit):
            for key, value in expr.pairs:
                self._walk_expr(key, None)
                self._walk_expr(value, None)
        elif isinstance(expr, A.EmbedExpr):
            for field in expr.fields:
                if field.value is not None:
                    self._walk_expr(field.value, None)
        elif isinstance(expr, (A.Lit, A.Name)):
            return
        else:
            self._walk_generic(expr)

    def _walk_generic(self, node) -> None:
        """Walk whatever expressions a node carries, for node types
        with no explicit case above (component declarations and any
        future statement). Dataclass fields are scanned; spans and
        types are skipped, everything else recurses."""
        if node is None or isinstance(node, (Span, Type)):
            return
        if isinstance(node, A.Expr):
            self._walk_expr(node, None)
            return
        if isinstance(node, A.Stmt):
            self._walk_stmt(node)
            return
        if isinstance(node, (list, tuple)):
            for item in node:
                self._walk_generic(item)
            return
        if is_dataclass(node):
            for f in _dc_fields(node):
                self._walk_generic(getattr(node, f.name))

    # ------------------------------------------------------------ db calls

    def _is_db_call(self, expr, name: str | None = None) -> bool:
        """True when ``expr`` is a call of ``db.<name>`` (or any db
        member when ``name`` is None), by bare name or by an import
        alias of the db module."""
        if not isinstance(expr, A.Call):
            return False
        func = expr.func
        if not isinstance(func, A.Member):
            return False
        if not isinstance(func.obj, A.Name):
            return False
        if func.obj.name not in self._db_aliases:
            return False
        return name is None or func.name == name

    def _check_db_call(self, call: A.Call,
                       expected: Type | None) -> None:
        name = call.func.name
        if call.kwargs:
            self._error(call.span,
                        f"`db.{name}` takes no named arguments",
                        help="pass values positionally, in order")
            for _n, value, _s in call.kwargs:
                self._walk_expr(value, None)
        if name not in DB_FUNCTIONS:
            match = difflib.get_close_matches(
                name, sorted(DB_FUNCTIONS), n=1, cutoff=0.7)
            if match:
                help = ("there is a db function with a similar name: "
                        f"`{match[0]}`")
            else:
                help = ("db functions: "
                        + ", ".join(f"`{n}`" for n in sorted(DB_FUNCTIONS)))
            self._error(call.span,
                        f"db has no function `{name}`",
                        help=help)
            for arg in call.args:
                self._walk_expr(arg, None)
            return
        if name == "open":
            self._check_open(call)
        elif name == "exec":
            self._check_exec(call)
        elif name == "query":
            self._check_query(call, expected)
        elif name == "begin":
            self._check_begin(call)
        elif name == "commit":
            self._check_commit(call)
        elif name == "rollback":
            self._check_rollback(call)
        elif name == "last_id":
            self._check_last_id(call)
        elif name == "close":
            self._check_close(call)

    def _check_arity(self, call: A.Call, name: str,
                     minimum: int, maximum: int | None) -> bool:
        n = len(call.args)
        if n < minimum:
            self._error(call.span,
                        f"`db.{name}` takes at least {minimum} "
                        f"argument(s), got {n}",
                        help=f"give `db.{name}` its "
                             f"{DB_MEMBERS[name]['sig']}")
            return False
        if maximum is not None and n > maximum:
            self._error(call.span,
                        f"`db.{name}` takes exactly {maximum} "
                        f"argument(s), got {n}",
                        help=f"give `db.{name}` its "
                             f"{DB_MEMBERS[name]['sig']}")
            return False
        return True

    def _check_handle_arg(self, arg) -> None:
        """A handle argument must be an int (when its type is known)
        and must not be a handle this body already closed."""
        if isinstance(arg, A.Name) and arg.name in self._closed:
            self._error(arg.span,
                        f"handle `{arg.name}` is already closed",
                        help="open a new handle with `db.open`, or do "
                             "not use this one after `db.close`")
        t = getattr(arg, "expr_type", None)
        if t is not None and (t.name != "int" or t.nullable):
            self._error(arg.span,
                        f"a db handle must be an `int`, got `{t}`",
                        help="pass the handle `db.open` returned")

    def _check_sql_arg(self, arg) -> str | None:
        """A SQL argument must be a str (when its type is known).
        Returns the literal text when the SQL is a string literal,
        else None (the `?` count check is skipped for computed
        SQL, and a mismatch throws at runtime)."""
        t = getattr(arg, "expr_type", None)
        if t is not None and (t.name != "str" or t.nullable):
            self._error(arg.span,
                        f"SQL must be a `str`, got `{t}`",
                        help="pass the statement as a string literal")
            return None
        if isinstance(arg, A.Lit) and isinstance(arg.value, str):
            return arg.value
        return None

    def _check_bind_args(self, args) -> None:
        for arg in args:
            self._walk_expr(arg, None)
            t = getattr(arg, "expr_type", None)
            if t is None:
                continue
            if t.nullable or t.name not in DB_BINDABLE_TYPES:
                self._error(arg.span,
                            f"a bound value must be `int`, `float`, "
                            f"`str`, or `bool`, got `{t}`",
                            help="unwrap a nullable first, and bind "
                                 "lists, dicts, structs, or `none` "
                                 "another way (SPEC.md SS12.5)")

    def _check_placeholders(self, call: A.Call, sql: str | None,
                            n_args: int) -> None:
        if sql is None:
            return
        have = _count_placeholders(sql)
        if have != n_args:
            self._error(call.span,
                        f"SQL has {have} `?` placeholder(s) but "
                        f"{n_args} value(s) were bound",
                        help="give one bound value per `?`, in order")

    def _check_open(self, call: A.Call) -> None:
        if not self._check_arity(call, "open", 1, 1):
            return
        arg = call.args[0]
        self._walk_expr(arg, None)
        t = getattr(arg, "expr_type", None)
        if t is not None and (t.name != "str" or t.nullable):
            self._error(arg.span,
                        f"the database path must be a `str`, got `{t}`",
                        help="pass a file path, or `\":memory:\"`")
        call.expr_type = INT

    def _check_exec(self, call: A.Call) -> None:
        if not self._check_arity(call, "exec", 2, None):
            return
        self._check_handle_arg(call.args[0])
        self._walk_expr(call.args[0], None)
        sql = self._check_sql_arg(call.args[1])
        self._walk_expr(call.args[1], None)
        self._check_placeholders(call, sql, len(call.args) - 2)
        self._check_bind_args(call.args[2:])
        call.expr_type = INT

    def _check_query(self, call: A.Call,
                     expected: Type | None) -> None:
        if not self._check_arity(call, "query", 2, None):
            return
        self._check_handle_arg(call.args[0])
        self._walk_expr(call.args[0], None)
        sql = self._check_sql_arg(call.args[1])
        self._walk_expr(call.args[1], None)
        self._check_placeholders(call, sql, len(call.args) - 2)
        self._check_bind_args(call.args[2:])
        self._resolve_query_row(call, expected)

    def _resolve_query_row(self, call: A.Call,
                           expected: Type | None) -> None:
        """Resolve the row struct for ``db.query`` from the expected
        type, and set ``call.expr_type`` to the full ``list[Row]``
        type for the emitters. The call site must carry the
        expected type (a let annotation, a return type, or a
        function argument type)."""
        if expected is None:
            self._error(call.span,
                        "`db.query` needs an expected row type",
                        help="annotate the let with `list[RowStruct]`, "
                             "for example "
                             "`let todos: list[Todo] = db.query(...)`")
            return
        if expected.name == "Result" and expected.params:
            expected = expected.params[0]
        if expected.name != "list" or len(expected.params) != 1:
            self._error(call.span,
                        "`db.query` must be used as `list[RowStruct]`, "
                        f"got `{expected}`",
                        help="annotate the let with `list[RowStruct]`, "
                             "for example "
                             "`let todos: list[Todo] = db.query(...)`")
            return
        row = expected.params[0]
        if row.nullable or row.name in BUILTIN_TYPE_NAMES \
                or row.params:
            self._error(call.span,
                        "`db.query` returns `list[RowStruct]` where "
                        f"`RowStruct` is a struct, got `{row}`",
                        help="declare a struct for the row and name it "
                             "in the annotation")
            return
        decl = self._structs.get(row.name)
        if decl is None and "." in row.name:
            decl = self._structs.get(row.name.rsplit(".", 1)[-1])
        if decl is None:
            # An unknown struct name is already E3001 from the core
            # checker; stay quiet rather than piling on.
            return
        for field in decl.fields:
            base = field.type.name
            if base not in DB_ROW_FIELD_TYPES:
                self._error(field.span,
                            f"row struct `{decl.name}` field "
                            f"`{field.name}` has type `{field.type}`, "
                            "which cannot be read from a row",
                            help="row struct fields must be `int`, "
                                 "`float`, `str`, or `bool` (or "
                                 "nullable)")
                return
        call.expr_type = list_of(Type(row.name))

    def _check_begin(self, call: A.Call) -> None:
        if not self._check_arity(call, "begin", 1, 1):
            return
        arg = call.args[0]
        self._check_handle_arg(arg)
        self._walk_expr(arg, None)
        if isinstance(arg, A.Name) and arg.name in self._handles:
            if self._handles[arg.name]:
                self._error(call.span,
                            f"handle `{arg.name}` is already in a "
                            "transaction",
                            help="commit or roll back before beginning "
                                 "again")
            else:
                self._handles[arg.name] = True
        call.expr_type = NONE_TYPE

    def _check_commit(self, call: A.Call) -> None:
        if not self._check_arity(call, "commit", 1, 1):
            return
        arg = call.args[0]
        self._check_handle_arg(arg)
        self._walk_expr(arg, None)
        if isinstance(arg, A.Name) and arg.name in self._handles:
            self._handles[arg.name] = False
        call.expr_type = NONE_TYPE

    def _check_rollback(self, call: A.Call) -> None:
        if not self._check_arity(call, "rollback", 1, 1):
            return
        arg = call.args[0]
        self._check_handle_arg(arg)
        self._walk_expr(arg, None)
        if isinstance(arg, A.Name) and arg.name in self._handles:
            self._handles[arg.name] = False
        call.expr_type = NONE_TYPE

    def _check_last_id(self, call: A.Call) -> None:
        if not self._check_arity(call, "last_id", 1, 1):
            return
        arg = call.args[0]
        self._check_handle_arg(arg)
        self._walk_expr(arg, None)
        call.expr_type = INT

    def _check_close(self, call: A.Call) -> None:
        if not self._check_arity(call, "close", 1, 1):
            return
        arg = call.args[0]
        self._check_handle_arg(arg)
        self._walk_expr(arg, None)
        if isinstance(arg, A.Name):
            self._handles.pop(arg.name, None)
            self._closed.add(arg.name)
        call.expr_type = NONE_TYPE


def check_db(program: A.Program) -> list[Diagnostic]:
    """db diagnostics for a resolver-output Program."""
    return DbChecker(program).run()
