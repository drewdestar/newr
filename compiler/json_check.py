"""json checker for the newr language (SPEC.md SS12.7).

Owns ALL json rules: the surface in ``json_limits.JSON_MEMBERS``,
the encodable-type check for ``json.encode`` (struct, list, dict,
scalar, or nullable of those, recursively; enums refused), and the
expected-type inference for ``json.decode`` (the call site must
carry the type to decode into). This is the ONLY compiler module
(besides the CLI, which composes the checkers, and ``docs.py``,
which renders the surface) that imports ``json_limits`` -- the core
checker never does (SPEC.md SS2.1).

Public API
----------
``check_json(program) -> list[Diagnostic]``
    json diagnostics for a resolver-output Program (run AFTER the
    core checker, so ``expr_type`` is filled in on expressions).

``check_json`` also enriches the AST: a ``json.decode`` call gets
``expr_type`` set to the expected type on the Call node, and a
``json.encode`` call gets ``expr_type`` set to ``str``, so the
emitters can read the types without re-deriving them. Emission is
refused without the full check (D27), so the emitters can rely on
that field.

CLI composition seam (see also ``checker.check_program``)::

    resolved = resolve_file(path)                 # or resolve_source
    providers = discord_providers(resolved.program)
    core = check_program(resolved.program, providers=providers)
    discord_diags = check_discord(resolved.program)
    db_diags = check_db(resolved.program)
    http_diags = check_http(resolved.program,
                            allowlist=manifest.http_allowlist)
    json_diags = check_json(resolved.program)
    diagnostics = (resolved.diagnostics + core.diagnostics
                   + discord_diags + db_diags + http_diags + json_diags)

Error-code map for the codes this module introduces
--------------------------------------------------
E2013  json rule violation (SPEC.md SS12.7): unknown json member,
       wrong argument count or type, a value that is not encodable
       (an enum, a set, a function, a Result, or a field of one of
       those), a ``json.decode`` with no expected type (or an
       expected type that is not decodable), or a ``json.decode``
       argument that is not a str.
"""

from __future__ import annotations

import difflib
from dataclasses import fields as _dc_fields
from dataclasses import is_dataclass

from . import ast_nodes as A
from .ast_nodes import (
    BUILTIN_TYPE_NAMES, STR, Type,
)
from .errors import MAX_NESTING_DEPTH, Diagnostic, Span
from .json_limits import (
    JSON_ERROR_CODE, JSON_FUNCTIONS, JSON_MEMBERS, JSON_SCALAR_TYPES,
)

# How deep the _walk_stmt / _walk_expr recursion may go (M-01): past
# this the walk stops silently, the core checker already reported
# E1001 for this program.
_DEPTH_LIMIT = MAX_NESTING_DEPTH


class JsonChecker:
    def __init__(self, program: A.Program) -> None:
        self.program = program
        self.diagnostics: list[Diagnostic] = []
        self._depth = 0
        self._structs: dict[str, A.StructDecl] = {}
        self._enums: dict[str, A.EnumDecl] = {}
        self._functions: dict[str, A.FnDecl] = {}
        self._json_aliases: set[str] = {"json"}
        self._return_type: Type | None = None
        # Var name -> declared type (let annotation or parameter),
        # for the Assign expected type.
        self._var_types: dict[str, Type] = {}
        programs = [program] + list(program.modules.values())
        for prog in programs:
            for st in prog.structs:
                self._structs[st.name] = st
            for en in prog.enums:
                self._enums[en.name] = en
            for fn in prog.functions:
                self._functions[fn.name] = fn
            for imp in prog.imports:
                if imp.module == "json":
                    self._json_aliases.add(imp.bound_name)

    # ------------------------------------------------------------ diagnostics

    def _error(self, span: Span, message: str,
               help: str | None = None) -> None:
        self.diagnostics.append(Diagnostic(
            span=span, code=JSON_ERROR_CODE, message=message,
            severity="error", help=help))

    # ------------------------------------------------------------ entry point

    def run(self) -> list[Diagnostic]:
        prog = self.program
        for sub in [prog] + list(prog.modules.values()):
            # A test block is lowered to a synthetic function
            # `_newr_test_<n>` in sub.functions sharing the same
            # body list, so skip it here: it is walked below via
            # sub.test_blocks. Walking it twice would report every
            # json error in a test block twice. (http_check.py has
            # the same skip; unlike ui, json has no rule that
            # depends on being inside a test block, so there is no
            # _in_test flag to set here.)
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
        self._var_types = {}

    def _walk_function(self, fn: A.FnDecl) -> None:
        self._reset_body_state()
        for p in fn.params:
            self._var_types[p.name] = p.type
        self._return_type = fn.return_type
        self._walk_body(fn.body)
        self._return_type = None

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
            pre = dict(self._var_types)
            ends = []
            self._var_types = dict(pre)
            self._walk_body(st.then_body)
            ends.append(dict(self._var_types))
            for cond, body in st.elifs:
                self._var_types = dict(pre)
                if cond is not None:
                    self._walk_expr(cond, None)
                self._walk_body(body)
                ends.append(dict(self._var_types))
            if st.else_body is not None:
                self._var_types = dict(pre)
                self._walk_body(st.else_body)
                ends.append(dict(self._var_types))
            else:
                ends.append(pre)
            # Lets inside a branch never escape it.
            self._var_types = dict(pre)
        elif isinstance(st, (A.For, A.While)):
            if isinstance(st, A.For) and st.iterable is not None:
                self._walk_expr(st.iterable, None)
            if isinstance(st, A.While) and st.cond is not None:
                self._walk_expr(st.cond, None)
            pre = dict(self._var_types)
            self._walk_body(st.body)
            self._var_types = dict(pre)
        elif isinstance(st, A.Try):
            pre = dict(self._var_types)
            self._walk_body(st.body)
            for _name, _span, body in st.catches:
                self._var_types = dict(pre)
                self._walk_body(body)
            self._var_types = dict(pre)
            if st.finally_body is not None:
                self._walk_body(st.finally_body)
        elif isinstance(st, A.Match):
            if st.scrutinee is not None:
                self._walk_expr(st.scrutinee, None)
            pre = dict(self._var_types)
            for arm in st.arms:
                self._var_types = dict(pre)
                if arm.guard is not None:
                    self._walk_expr(arm.guard, None)
                self._walk_body(arm.body)
            self._var_types = dict(pre)
        elif isinstance(st, (A.Let, A.Const)):
            if st.type_ann is not None:
                self._var_types[st.name] = st.type_ann
            elif st.value is not None:
                # No annotation: the core checker already filled in
                # the value's type, so the name is still known (a
                # let-bound non-lambda handler, for example, must be
                # told apart from an unknown name).
                t = getattr(st.value, "expr_type", None)
                if t is not None:
                    self._var_types[st.name] = t
            if st.value is not None:
                self._walk_expr(st.value, st.type_ann)
        elif isinstance(st, A.Assign):
            expected = self._var_types.get(st.name)
            if st.value is not None:
                self._walk_expr(st.value, expected)
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
            if self._is_json_call(expr):
                self._check_json_call(expr, expected)
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
                    and expr.obj.name in self._json_aliases:
                self._error(expr.span,
                            f"`json.{expr.name}` is a function, not a value",
                            help="call it, for example "
                                 f"`json.{expr.name}(...)`")
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

    # ------------------------------------------------------------ json calls

    def _is_json_call(self, expr, name: str | None = None) -> bool:
        """True when ``expr`` is a call of ``json.<name>`` (or any
        json member when ``name`` is None), by bare name or by an
        import alias of the json module."""
        if not isinstance(expr, A.Call):
            return False
        func = expr.func
        if not isinstance(func, A.Member):
            return False
        if not isinstance(func.obj, A.Name):
            return False
        if func.obj.name not in self._json_aliases:
            return False
        return name is None or func.name == name

    def _check_json_call(self, call: A.Call,
                         expected: Type | None) -> None:
        name = call.func.name
        if call.kwargs:
            self._error(call.span,
                        f"`json.{name}` takes no named arguments",
                        help="pass values positionally, in order")
            for _n, value, _s in call.kwargs:
                self._walk_expr(value, None)
        if name not in JSON_FUNCTIONS:
            match = difflib.get_close_matches(
                name, sorted(JSON_FUNCTIONS), n=1, cutoff=0.7)
            if match:
                help = ("there is a json function with a similar "
                        f"name: `{match[0]}`")
            else:
                help = ("json functions: "
                        + ", ".join(f"`{n}`"
                                    for n in sorted(JSON_FUNCTIONS)))
            self._error(call.span,
                        f"json has no function `{name}`",
                        help=help)
            for arg in call.args:
                self._walk_expr(arg, None)
            return
        if name == "encode":
            self._check_encode(call)
        elif name == "decode":
            self._check_decode(call, expected)

    def _check_arity(self, call: A.Call, name: str,
                     minimum: int, maximum: int | None) -> bool:
        n = len(call.args)
        if n < minimum:
            self._error(call.span,
                        f"`json.{name}` takes at least {minimum} "
                        f"argument(s), got {n}",
                        help=f"give `json.{name}` its "
                             f"{JSON_MEMBERS[name]['sig']}")
            return False
        if maximum is not None and n > maximum:
            self._error(call.span,
                        f"`json.{name}` takes exactly {maximum} "
                        f"argument(s), got {n}",
                        help=f"give `json.{name}` its "
                             f"{JSON_MEMBERS[name]['sig']}")
            return False
        return True

    # ------------------------------------------------------------ encodability

    def _codable_problem(self, t: Type, where: str,
                         visiting: frozenset) -> str | None:
        """The reason type ``t`` cannot be encoded/decoded, or None
        when it can. ``where`` names the value in the diagnostic
        ("the value", "field `title` of struct `Todo`"). A struct
        name already being visited is allowed: a recursive struct
        type is codable, only its finite values are ever encoded.
        An undeclared struct or enum name is already E3001 from the
        core checker; stay quiet (None) rather than piling on."""
        if t is None:
            return None
        if t.nullable:
            inner = Type(t.name, t.params, nullable=False)
            return self._codable_problem(inner, where, visiting)
        if t.name in JSON_SCALAR_TYPES:
            return None
        if t.name == "none":
            return None
        if t.name in self._enums:
            return (f"{where} has type `{t}`, an enum, which cannot "
                    "be encoded as JSON in M8b (encode an enum field "
                    "by hand, or wait for enum encoding)")
        if t.name == "list" and len(t.params) == 1:
            return self._codable_problem(t.params[0], where, visiting)
        if t.name == "dict" and len(t.params) == 2:
            if t.params[0].name != "str":
                return (f"{where} has type `{t}`: dict keys must be "
                        "str for JSON")
            return self._codable_problem(t.params[1], where, visiting)
        if t.name in BUILTIN_TYPE_NAMES:
            return (f"{where} has type `{t}`, which cannot be "
                    "encoded as JSON (only structs, lists, dicts, "
                    "scalars, and nullables of those)")
        if t.params:
            return (f"{where} has type `{t}`, which cannot be "
                    "encoded as JSON")
        short = t.name.rsplit(".", 1)[-1] if "." in t.name else t.name
        decl = self._structs.get(t.name) or self._structs.get(short)
        if decl is None:
            return None
        if decl.name in visiting:
            return None
        visiting = visiting | {decl.name}
        for field in decl.fields:
            problem = self._codable_problem(
                field.type, f"field `{field.name}` of struct "
                f"`{decl.name}`", visiting)
            if problem is not None:
                return problem
        return None

    # ------------------------------------------------------------ encode/decode

    def _check_encode(self, call: A.Call) -> None:
        if not self._check_arity(call, "encode", 1, 1):
            return
        arg = call.args[0]
        self._walk_expr(arg, None)
        t = getattr(arg, "expr_type", None)
        if t is not None:
            problem = self._codable_problem(t, "the value", frozenset())
            if problem is not None:
                self._error(arg.span,
                            f"`json.encode`: {problem}",
                            help="encode a struct, list, dict, scalar, "
                                 "or nullable of those")
        call.expr_type = STR

    def _check_decode(self, call: A.Call,
                      expected: Type | None) -> None:
        if not self._check_arity(call, "decode", 1, 1):
            return
        arg = call.args[0]
        self._walk_expr(arg, None)
        t = getattr(arg, "expr_type", None)
        if t is not None and (t.name != "str" or t.nullable):
            self._error(arg.span,
                        f"`json.decode` takes a JSON string, got `{t}`",
                        help="pass the text to decode, for example "
                             "`json.decode(resp.body)`")
        if expected is None:
            self._error(call.span,
                        "`json.decode` needs an expected type to "
                        "decode into",
                        help="annotate the let with the type to decode "
                             "into, for example `let todos: list[Todo] = "
                             "json.decode(text)`")
            return
        if expected.name == "Result" and expected.params:
            expected = expected.params[0]
        problem = self._codable_problem(expected, "the expected type",
                                        frozenset())
        if problem is not None:
            self._error(call.span,
                        f"`json.decode`: {problem}",
                        help="decode into a struct, list, dict, scalar, "
                             "or nullable of those")
            return
        call.expr_type = expected


def check_json(program: A.Program) -> list[Diagnostic]:
    """json diagnostics for a resolver-output Program."""
    return JsonChecker(program).run()
