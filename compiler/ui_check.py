"""ui checker for the newr language (SPEC.md SS12.8).

Owns ALL ui rules: the surface in ``ui_limits.UI_MEMBERS``, the
arity and type checks for every `ui.*` call, the handler signature
checks (`fn() -> none` for buttons, `fn(str) -> none` for inputs),
the view signature check for `ui.run` (`fn() -> Widget`), the
one-window rule (at most one of `ui.show` and `ui.run` per program),
the image URL literal check, and the no-window-in-test rule. This
is the ONLY compiler module (besides the CLI, which composes the
checkers, and ``docs.py``, which renders the surface) that imports
``ui_limits`` -- the core checker never does (SPEC.md SS2.1).

Public API
----------
``check_ui(program) -> list[Diagnostic]``
    ui diagnostics for a resolver-output Program (run AFTER the
    core checker, so ``expr_type`` is filled in on expressions).

``check_ui`` also enriches the AST: a constructor call gets
``expr_type`` set to the Widget type, `render` gets str, and
`show`/`run` get none, so the emitters can read the types without
re-deriving them. Emission is refused without the full check
(D27), so the emitters can rely on those fields.

CLI composition seam (see also ``checker.check_program``)::

    resolved = resolve_file(path)                 # or resolve_source
    providers = discord_providers(resolved.program)
    core = check_program(resolved.program, providers=providers)
    discord_diags = check_discord(resolved.program)
    db_diags = check_db(resolved.program)
    http_diags = check_http(resolved.program,
                            allowlist=manifest.http_allowlist)
    json_diags = check_json(resolved.program)
    ui_diags = check_ui(resolved.program)
    diagnostics = (resolved.diagnostics + core.diagnostics
                   + discord_diags + db_diags + http_diags + json_diags
                   + ui_diags)

Error-code map for the codes this module introduces
--------------------------------------------------
E2014  ui rule violation (SPEC.md SS12.8): unknown ui function,
       wrong argument count or type, a non-Widget where a Widget
       is required, a button handler that is not `fn() -> none`,
       an input handler that is not `fn(str) -> none`, a `ui.run`
       argument that is not `fn() -> Widget`, a view function
       passed to `ui.show` or a Widget passed to `ui.run`, more
       than one window call in a program, a `ui.render` argument
       that is not a Widget, an image URL that is not an http(s)
       string literal, or a `ui.show`/`ui.run` call inside a test
       block.
"""

from __future__ import annotations

import difflib
from dataclasses import fields as _dc_fields
from dataclasses import is_dataclass

from . import ast_nodes as A
from .ast_nodes import NONE_TYPE, STR, Type
from .errors import MAX_NESTING_DEPTH, Diagnostic, Span
from .ui_limits import UI_ERROR_CODE, UI_FUNCTIONS, UI_MEMBERS

# How deep the _walk_stmt / _walk_expr recursion may go (M-01): past
# this the walk stops silently, the core checker already reported
# E1001 for this program.
_DEPTH_LIMIT = MAX_NESTING_DEPTH

#: The Widget type, the opaque builtin value type of the ui module
#: (SPEC.md SS12.8). Only `ui.*` constructors produce one.
WIDGET = Type("Widget")


def _is_unknown(t: Type | None) -> bool:
    """True when the core checker left the type unknown (or there is
    no type at all): ui_check stays quiet rather than piling on."""
    return t is None or t.name == "__unknown__"


def _is_widget_type(t: Type | None) -> bool:
    """True when ``t`` is exactly the Widget type (not nullable, no
    type arguments)."""
    return (t is not None and t.name == "Widget"
            and not t.params and not t.nullable)


def _is_list_of_widget(t: Type | None) -> bool:
    """True when ``t`` is exactly `list[Widget]`."""
    return (t is not None and t.name == "list" and not t.nullable
            and len(t.params) == 1 and _is_widget_type(t.params[0]))


def _fn_type_text(params: list[Type], ret: Type) -> str:
    """A fn type as a string, for diagnostics (`fn() -> none`)."""
    inner = ", ".join(str(p) for p in params)
    return f"fn({inner}) -> {ret}"


class UiChecker:
    def __init__(self, program: A.Program) -> None:
        self.program = program
        self.diagnostics: list[Diagnostic] = []
        self._depth = 0
        self._structs: dict[str, A.StructDecl] = {}
        self._enums: dict[str, A.EnumDecl] = {}
        self._functions: dict[str, A.FnDecl] = {}
        self._ui_aliases: set[str] = {"ui"}
        self._return_type: Type | None = None
        # Var name -> declared type (let annotation or parameter),
        # for the Assign expected type.
        self._var_types: dict[str, Type] = {}
        # Var name -> the lambda a let/assign bound it to, for a
        # handler or view passed by name (a block-body lambda cannot
        # be written inline as a call argument: the parser rejects
        # it, so handlers are let-bound lambdas or single-expression
        # inline lambdas). Same reset discipline as _var_types.
        self._lambdas: dict[str, A.Lambda] = {}
        # Window calls (show plus run) in the whole program, for
        # the one-window rule. Program-wide, never reset per body:
        # a second window call is a duplicate wherever it sits.
        self._window_calls = 0
        # True while walking a test block: show/run never terminate,
        # so neither may appear there.
        self._in_test = False
        programs = [program] + list(program.modules.values())
        for prog in programs:
            for st in prog.structs:
                self._structs[st.name] = st
            for en in prog.enums:
                self._enums[en.name] = en
            for fn in prog.functions:
                self._functions[fn.name] = fn
            for imp in prog.imports:
                if imp.module == "ui":
                    self._ui_aliases.add(imp.bound_name)

    # ------------------------------------------------------------ diagnostics

    def _error(self, span: Span, message: str,
               help: str | None = None) -> None:
        self.diagnostics.append(Diagnostic(
            span=span, code=UI_ERROR_CODE, message=message,
            severity="error", help=help))

    # ------------------------------------------------------------ entry point

    def run(self) -> list[Diagnostic]:
        prog = self.program
        for sub in [prog] + list(prog.modules.values()):
            # A test block is lowered to a synthetic function
            # `_newr_test_<n>` in sub.functions sharing the same
            # body list, so skip it here: it is walked below via
            # sub.test_blocks, with _in_test set. Walking it twice
            # would report every ui error in a test block twice
            # and count its window call twice.
            test_fn_names = {tb.func_name for tb in sub.test_blocks}
            for fn in sub.functions:
                if fn.name in test_fn_names:
                    continue
                self._walk_function(fn)
            for tb in sub.test_blocks:
                self._reset_body_state()
                self._return_type = None
                self._in_test = True
                self._walk_body(tb.body)
                self._in_test = False
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
        self._lambdas = {}

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
            pre = (dict(self._var_types), dict(self._lambdas))
            self._walk_body(st.then_body)
            for cond, body in st.elifs:
                self._var_types, self._lambdas = (
                    dict(pre[0]), dict(pre[1]))
                if cond is not None:
                    self._walk_expr(cond, None)
                self._walk_body(body)
            if st.else_body is not None:
                self._var_types, self._lambdas = (
                    dict(pre[0]), dict(pre[1]))
                self._walk_body(st.else_body)
            # Lets inside a branch never escape it.
            self._var_types, self._lambdas = (
                dict(pre[0]), dict(pre[1]))
        elif isinstance(st, (A.For, A.While)):
            if isinstance(st, A.For) and st.iterable is not None:
                self._walk_expr(st.iterable, None)
            if isinstance(st, A.While) and st.cond is not None:
                self._walk_expr(st.cond, None)
            pre = (dict(self._var_types), dict(self._lambdas))
            self._walk_body(st.body)
            self._var_types, self._lambdas = (
                dict(pre[0]), dict(pre[1]))
        elif isinstance(st, A.Try):
            pre = (dict(self._var_types), dict(self._lambdas))
            self._walk_body(st.body)
            for _name, _span, body in st.catches:
                self._var_types, self._lambdas = (
                    dict(pre[0]), dict(pre[1]))
                self._walk_body(body)
            self._var_types, self._lambdas = (
                dict(pre[0]), dict(pre[1]))
            if st.finally_body is not None:
                self._walk_body(st.finally_body)
        elif isinstance(st, A.Match):
            if st.scrutinee is not None:
                self._walk_expr(st.scrutinee, None)
            pre = (dict(self._var_types), dict(self._lambdas))
            for arm in st.arms:
                self._var_types, self._lambdas = (
                    dict(pre[0]), dict(pre[1]))
                if arm.guard is not None:
                    self._walk_expr(arm.guard, None)
                self._walk_body(arm.body)
            self._var_types, self._lambdas = (
                dict(pre[0]), dict(pre[1]))
        elif isinstance(st, (A.Let, A.Const)):
            if st.type_ann is not None:
                self._var_types[st.name] = st.type_ann
            elif st.value is not None:
                # No annotation: the core checker already filled in
                # the value's type, so the name is still known.
                t = getattr(st.value, "expr_type", None)
                if t is not None:
                    self._var_types[st.name] = t
            if st.value is not None:
                self._walk_expr(st.value, st.type_ann)
                if isinstance(st.value, A.Lambda):
                    self._lambdas[st.name] = st.value
        elif isinstance(st, A.Assign):
            expected = self._var_types.get(st.name)
            if st.value is not None:
                self._walk_expr(st.value, expected)
                if isinstance(st.value, A.Lambda):
                    self._lambdas[st.name] = st.value
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
            if self._is_ui_call(expr):
                self._check_ui_call(expr, expected)
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
                    and expr.obj.name in self._ui_aliases:
                self._error(expr.span,
                            f"`ui.{expr.name}` is a function, not a value",
                            help="call it, for example "
                                 f"`ui.{expr.name}(...)`")
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

    # ------------------------------------------------------------ ui calls

    def _is_ui_call(self, expr, name: str | None = None) -> bool:
        """True when ``expr`` is a call of ``ui.<name>`` (or any
        ui member when ``name`` is None), by bare name or by an
        import alias of the ui module."""
        if not isinstance(expr, A.Call):
            return False
        func = expr.func
        if not isinstance(func, A.Member):
            return False
        if not isinstance(func.obj, A.Name):
            return False
        if func.obj.name not in self._ui_aliases:
            return False
        return name is None or func.name == name

    def _check_ui_call(self, call: A.Call,
                       expected: Type | None) -> None:
        name = call.func.name
        if call.kwargs:
            self._error(call.span,
                        f"`ui.{name}` takes no named arguments",
                        help="pass values positionally, in order")
            for _n, value, _s in call.kwargs:
                self._walk_expr(value, None)
            # The call is already malformed: do not pile a
            # spurious arity error on top of it.
            return
        if name not in UI_FUNCTIONS:
            match = difflib.get_close_matches(
                name, sorted(UI_FUNCTIONS), n=1, cutoff=0.7)
            if match:
                help = ("there is a ui function with a similar "
                        f"name: `{match[0]}`")
            else:
                help = ("ui functions: "
                        + ", ".join(f"`{n}`"
                                    for n in sorted(UI_FUNCTIONS)))
            self._error(call.span,
                        f"ui has no function `{name}`",
                        help=help)
            for arg in call.args:
                self._walk_expr(arg, None)
            return
        if name == "text":
            self._check_text(call)
        elif name == "button":
            self._check_button(call)
        elif name == "input":
            self._check_input(call)
        elif name == "list":
            self._check_list(call)
        elif name == "image":
            self._check_image(call)
        elif name == "row":
            self._check_row(call)
        elif name == "column":
            self._check_column(call)
        elif name == "render":
            self._check_render(call)
        elif name == "show":
            self._check_show(call)
        elif name == "run":
            self._check_run(call)

    def _check_arity(self, call: A.Call, name: str,
                     minimum: int, maximum: int | None) -> bool:
        n = len(call.args)
        if n < minimum:
            self._error(call.span,
                        f"`ui.{name}` takes at least {minimum} "
                        f"argument(s), got {n}",
                        help=f"give `ui.{name}` its "
                             f"{UI_MEMBERS[name]['sig']}")
            return False
        if maximum is not None and n > maximum:
            self._error(call.span,
                        f"`ui.{name}` takes at most {maximum} "
                        f"argument(s), got {n}",
                        help=f"give `ui.{name}` its "
                             f"{UI_MEMBERS[name]['sig']}")
            return False
        return True

    def _check_str_arg(self, arg, what: str) -> str | None:
        """A string argument: type-checked when known. Returns the
        literal text when it is a string literal, else None (the
        literal-only checks are skipped for computed values, and a
        bad value throws at runtime)."""
        t = getattr(arg, "expr_type", None)
        if not _is_unknown(t) and (t.name != "str" or t.nullable):
            self._error(arg.span,
                        f"{what} must be a `str`, got `{t}`",
                        help=f"pass {what} as a string")
            return None
        if isinstance(arg, A.Lit) and isinstance(arg.value, str):
            return arg.value
        return None

    def _expr_is_widget(self, expr) -> bool:
        """True when the expression is known to be a Widget: a ui
        constructor call, a Widget variable, or an expression whose
        type the core checker already set to Widget. False for a
        known non-Widget; UNKNOWN stays quiet (the core checker
        already reported, or the type is genuinely unknown)."""
        if self._is_ui_call(expr):
            return True
        if isinstance(expr, A.Name):
            t = self._var_types.get(expr.name)
            if t is not None:
                return _is_widget_type(t)
            return True  # Unknown name: core already reported E3001.
        t = getattr(expr, "expr_type", None)
        if _is_unknown(t):
            return True
        return _is_widget_type(t)

    def _check_widget_arg(self, arg, what: str) -> None:
        """A Widget argument: a lambda or a named function in this
        position is E2014 (a view function passed where a tree
        belongs), a known non-Widget is E2014, an unknown type stays
        quiet."""
        self._walk_expr(arg, None)
        if isinstance(arg, A.Lambda):
            self._error(arg.span,
                        f"{what} must be a Widget, not a function",
                        help="pass a widget tree, for example "
                             "`ui.text(\"hi\")`; a view function goes "
                             "to `ui.run`, not here")
            return
        if isinstance(arg, A.Name):
            if arg.name in self._lambdas or arg.name in self._functions:
                self._error(arg.span,
                            f"{what} must be a Widget, not a function",
                            help="pass a widget tree, for example "
                                 "`ui.text(\"hi\")`; a view function goes "
                                 "to `ui.run`, not here")
                return
            t = self._var_types.get(arg.name)
            if t is not None and not _is_unknown(t) \
                    and not _is_widget_type(t):
                self._error(arg.span,
                            f"{what} must be a Widget, got `{t}`",
                            help="pass a widget built by a `ui.*` "
                                 "constructor")
                return
            return
        t = getattr(arg, "expr_type", None)
        if _is_unknown(t):
            return
        if not _is_widget_type(t):
            self._error(arg.span,
                        f"{what} must be a Widget, got `{t}`",
                        help="pass a widget built by a `ui.*` "
                             "constructor")

    def _check_children_arg(self, arg) -> None:
        """A `list[Widget]` argument: the whole-list type is checked
        when known, and each element of a list literal is checked
        too (a mixed list is E2003 from the core checker, which
        leaves the list type UNKNOWN, so the element check is what
        catches a non-Widget element there)."""
        self._walk_expr(arg, Type("list", (WIDGET,)))
        t = getattr(arg, "expr_type", None)
        if not _is_unknown(t) and not _is_list_of_widget(t):
            self._error(arg.span,
                        f"children must be `list[Widget]`, got `{t}`",
                        help="pass a list of widgets, for example "
                             "`[ui.text(\"a\"), ui.text(\"b\")]`")
            return
        if isinstance(arg, A.ListLit):
            for el in arg.elements:
                if not self._expr_is_widget(el):
                    self._error(el.span,
                                "a child must be a Widget, got "
                                f"`{getattr(el, 'expr_type', None)}`",
                                help="pass widgets built by `ui.*` "
                                     "constructors")

    def _check_callable(self, arg, want_params: list[Type],
                        want_ret: Type, what: str) -> None:
        """A handler or view argument: a lambda (inline or let-bound)
        or a named function whose declared signature matches the
        wanted one. A value of any other kind is E2014."""
        if isinstance(arg, A.Lambda):
            self._check_lambda_signature(arg, want_params, want_ret,
                                         what)
            self._walk_expr(arg, None)
            return
        if isinstance(arg, A.Name):
            lam = self._lambdas.get(arg.name)
            if lam is not None:
                self._check_lambda_signature(lam, want_params, want_ret,
                                             what)
                return
            if arg.name in self._functions:
                fn = self._functions[arg.name]
                self._check_declared_signature(
                    [p.type for p in fn.params], fn.return_type,
                    want_params, want_ret, what, arg.span)
                return
            t = self._var_types.get(arg.name)
            if t is not None:
                if _is_unknown(t):
                    return
                if t.name == "fn" and len(t.params) == \
                        len(want_params) + 1:
                    ok = all(p == w for p, w in
                             zip(t.params[:-1], want_params)) \
                        and t.params[-1] == want_ret
                    if not ok:
                        self._error(arg.span,
                                    f"{what} must be "
                                    f"`{_fn_type_text(want_params, want_ret)}`, "
                                    f"got `{t}`",
                                    help="match the handler signature "
                                         "(SPEC.md SS12.8)")
                    return
                self._error(arg.span,
                            f"{what} must be a function of type "
                            f"`{_fn_type_text(want_params, want_ret)}`, "
                            f"got `{t}`",
                            help="bind a lambda or a named function "
                                 "with that signature first")
                return
            # An unknown name is already E3001/E2005 from the core
            # checker; stay quiet rather than piling on.
            self._walk_expr(arg, None)
            return
        self._error(arg.span,
                    f"{what} must be a function of type "
                    f"`{_fn_type_text(want_params, want_ret)}`",
                    help="write it as a lambda, for example "
                         f"`{_fn_type_text(want_params, want_ret)}: ...`, "
                         "or bind it with a let first")
        self._walk_expr(arg, None)

    def _check_lambda_signature(self, lam: A.Lambda,
                                want_params: list[Type],
                                want_ret: Type, what: str) -> None:
        """The parameter/return types of one handler/view lambda."""
        self._check_declared_signature(
            [p.type for p in lam.params], lam.return_type,
            want_params, want_ret, what, lam.span)

    def _check_declared_signature(self, params: list[Type | None],
                                  ret: Type | None,
                                  want_params: list[Type],
                                  want_ret: Type, what: str,
                                  span: Span) -> None:
        """Declared parameter types and return type against the
        wanted signature."""
        if len(params) != len(want_params):
            self._error(span,
                        f"{what} takes {len(want_params)} parameter(s), "
                        f"got {len(params)}",
                        help=f"write `{_fn_type_text(want_params, want_ret)}`")
            return
        for i, (pt, want) in enumerate(zip(params, want_params)):
            if pt is not None and pt != want:
                self._error(span,
                            f"{what} parameter {i} must be `{want}`, "
                            f"got `{pt}`",
                            help="match the signature (SPEC.md SS12.8)")
        if ret is None:
            self._error(span,
                        f"{what} must declare a return type",
                        help=f"write `{_fn_type_text(want_params, want_ret)}`")
        elif ret != want_ret:
            self._error(span,
                        f"{what} must return `{want_ret}`, got `{ret}`",
                        help="match the signature (SPEC.md SS12.8)")

    # ------------------------------------------------------------ per function

    def _check_text(self, call: A.Call) -> None:
        if not self._check_arity(call, "text", 1, 1):
            return
        self._check_str_arg(call.args[0], "the text")
        self._walk_expr(call.args[0], None)
        call.expr_type = WIDGET

    def _check_button(self, call: A.Call) -> None:
        if not self._check_arity(call, "button", 2, 2):
            return
        self._check_str_arg(call.args[0], "the button label")
        self._walk_expr(call.args[0], None)
        self._check_callable(call.args[1], [], NONE_TYPE,
                             "a button handler")
        call.expr_type = WIDGET

    def _check_input(self, call: A.Call) -> None:
        if not self._check_arity(call, "input", 3, 3):
            return
        self._check_str_arg(call.args[0], "the placeholder")
        self._walk_expr(call.args[0], None)
        self._check_str_arg(call.args[1], "the value")
        self._walk_expr(call.args[1], None)
        self._check_callable(call.args[2], [STR], NONE_TYPE,
                             "an input handler")
        call.expr_type = WIDGET

    def _check_list(self, call: A.Call) -> None:
        if not self._check_arity(call, "list", 1, 1):
            return
        self._check_children_arg(call.args[0])
        call.expr_type = WIDGET

    def _check_image(self, call: A.Call) -> None:
        if not self._check_arity(call, "image", 1, 1):
            return
        url = self._check_str_arg(call.args[0], "the image URL")
        self._walk_expr(call.args[0], None)
        if url is not None and not (
                url.startswith("http://")
                or url.startswith("https://")):
            self._error(call.args[0].span,
                        "an image URL must start with `http://` or "
                        f"`https://`, got `{url}`",
                        help="pass a full http(s) URL, for example "
                             "\"https://example.com/photo.png\"")
        call.expr_type = WIDGET

    def _check_row(self, call: A.Call) -> None:
        if not self._check_arity(call, "row", 1, 1):
            return
        self._check_children_arg(call.args[0])
        call.expr_type = WIDGET

    def _check_column(self, call: A.Call) -> None:
        if not self._check_arity(call, "column", 1, 1):
            return
        self._check_children_arg(call.args[0])
        call.expr_type = WIDGET

    def _check_render(self, call: A.Call) -> None:
        if not self._check_arity(call, "render", 1, 1):
            return
        self._check_widget_arg(call.args[0], "ui.render's argument")
        call.expr_type = STR

    def _check_window(self, call: A.Call) -> None:
        """The rules shared by show and run: at most one window per
        program, and no window inside a test block."""
        self._window_calls += 1
        if self._window_calls > 1:
            self._error(call.span,
                        "a program has one window: `ui.show` and "
                        "`ui.run` may be called at most once, in total",
                        help="build one tree (or one view) and show "
                             "or run it once")
        if self._in_test:
            self._error(call.span,
                        "`ui.show` and `ui.run` never return, so they "
                        "may not appear in a test block",
                        help="a test must terminate; show or run the "
                             "window outside the test")

    def _check_show(self, call: A.Call) -> None:
        if not self._check_arity(call, "show", 1, 1):
            return
        self._check_widget_arg(call.args[0], "ui.show's argument")
        self._check_window(call)
        call.expr_type = NONE_TYPE

    def _check_run(self, call: A.Call) -> None:
        if not self._check_arity(call, "run", 1, 1):
            return
        self._check_callable(call.args[0], [], WIDGET, "a view function")
        self._check_window(call)
        call.expr_type = NONE_TYPE


def check_ui(program: A.Program) -> list[Diagnostic]:
    """ui diagnostics for a resolver-output Program."""
    return UiChecker(program).run()
