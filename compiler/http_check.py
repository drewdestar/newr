"""http checker for the newr language (SPEC.md SS12.6).

Owns ALL http rules: the surface in ``http_limits.HTTP_MEMBERS``,
the method/path/URL literal checks, the duplicate-route check, the
handler struct shape check (a parameter struct with exactly the
five HttpRequest fields, a return struct with exactly the three
HttpResponse fields), the fetch expected-type inference (the
response struct, resolved from the expected type at the call site),
the timeout and headers arity checks, and the allowlist check for
literal URLs. This is the ONLY compiler module (besides the CLI,
which composes the checkers, and ``docs.py``, which renders the
surface) that imports ``http_limits`` -- the core checker never does
(SPEC.md SS2.1).

Public API
----------
``check_http(program, allowlist=None) -> list[Diagnostic]``
    http diagnostics for a resolver-output Program (run AFTER the
    core checker, so ``expr_type`` is filled in on expressions).
    ``allowlist`` is the manifest's ``http_allowlist`` (a list of
    bare hostnames); None means the default localhost-only list
    (``http_limits.DEFAULT_HTTP_ALLOWLIST``).

``check_http`` also enriches the AST: a fetch call whose response
type it resolves gets ``expr_type`` set to the response struct type
on the Call node, so the emitters can read the response type
without re-deriving it. A ``route`` call's handler lambda keeps its
own declared parameter and return types; the emitters read the
request struct from the lambda. Emission is refused without the
full check (D27), so the emitters can rely on that field.

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
E2012  http rule violation (SPEC.md SS12.6): unknown http member,
       wrong argument count or type, a method literal that is not
       one of GET/POST/PUT/PATCH/DELETE/HEAD/OPTIONS, a path
       literal without a leading "/", a duplicate route, a handler
       that is not a lambda or whose parameter/return structs do
       not have exactly the HttpRequest/HttpResponse fields, a
       fetch with no expected response type (or a response type
       that is not a struct with exactly the HttpResponse fields),
       a timeout of 0 or negative, headers that are not
       ``dict[str, str]``, a URL literal without an http(s) scheme
       or host, userinfo in a URL, or a URL host outside the
       allowlist.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import fields as _dc_fields
from dataclasses import is_dataclass
from urllib.parse import urlparse

from . import ast_nodes as A
from .ast_nodes import (
    BUILTIN_TYPE_NAMES, INT, NONE_TYPE, STR, Type,
)
from .errors import MAX_NESTING_DEPTH, Diagnostic, Span
from .http_limits import (
    DEFAULT_HTTP_ALLOWLIST, DEFAULT_HTTP_TIMEOUT_SECONDS,
    HTTP_ERROR_CODE, HTTP_FUNCTIONS, HTTP_MEMBERS, HTTP_METHODS,
    HTTP_REQUEST_FIELDS, HTTP_RESPONSE_FIELDS,
)

# How deep the _walk_stmt / _walk_expr recursion may go (M-01): past
# this the walk stops silently, the core checker already reported
# E1001 for this program.
_DEPTH_LIMIT = MAX_NESTING_DEPTH


def _int_literal(arg) -> int | None:
    """The int value of an int literal, including a negated one:
    `-5` parses as unary minus over a literal, not as a literal
    itself. None when the argument is not an int literal (a computed
    value gets no static check, and a bad value throws at runtime)."""
    if isinstance(arg, A.Lit) and isinstance(arg.value, int) \
            and not isinstance(arg.value, bool):
        return arg.value
    if isinstance(arg, A.UnaryOp) and arg.op == "-" \
            and isinstance(arg.operand, A.Lit) \
            and isinstance(arg.operand.value, int) \
            and not isinstance(arg.operand.value, bool):
        return -arg.operand.value
    return None

# A host is a bare hostname for the allowlist: no scheme, no port,
# no userinfo, no path. The manifest key holds these, and the
# checker compares a URL's parsed host against them exactly
# (case-insensitive).
_HOST_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def _type_matches(t: Type, spec: object) -> bool:
    """True when type ``t`` is exactly the spec: a bare name
    ("str"), or a tuple (container, element, ...) for
    ``dict[str, str]``. Nullable types never match: the request
    and response fields are all non-nullable."""
    if isinstance(spec, str):
        return t.name == spec and not t.params and not t.nullable
    if isinstance(spec, tuple) and spec:
        if t.name != spec[0] or t.nullable:
            return False
        if len(t.params) != len(spec) - 1:
            return False
        return all(_type_matches(p, s)
                   for p, s in zip(t.params, spec[1:]))
    return False


def _spec_text(spec: object) -> str:
    """The spec as a type string, for diagnostics ("dict[str, str]")."""
    if isinstance(spec, str):
        return spec
    if isinstance(spec, tuple) and spec:
        inner = ", ".join(_spec_text(s) for s in spec[1:])
        return f"{spec[0]}[{inner}]"
    return "?"


class HttpChecker:
    def __init__(self, program: A.Program,
                 allowlist=None) -> None:
        self.program = program
        self.diagnostics: list[Diagnostic] = []
        self._depth = 0
        self._structs: dict[str, A.StructDecl] = {}
        self._enums: dict[str, A.EnumDecl] = {}
        self._functions: dict[str, A.FnDecl] = {}
        self._http_aliases: set[str] = {"http"}
        self._return_type: Type | None = None
        # Var name -> declared type (let annotation or parameter),
        # for the Assign expected type.
        self._var_types: dict[str, Type] = {}
        # Var name -> the lambda a let/assign bound it to, for a
        # route handler passed by name (a block-body lambda cannot
        # be written inline as a call argument: the parser rejects
        # it, so handlers are let-bound lambdas or single-expression
        # inline lambdas). Same reset discipline as _var_types.
        self._lambdas: dict[str, A.Lambda] = {}
        # (method, path) pairs already registered in this program,
        # for the duplicate-route check. Program-wide, never reset
        # per body: a route registered twice is a duplicate wherever
        # the two registrations sit.
        self._routes: set[tuple[str, str]] = set()
        if allowlist is None:
            self._allowlist: tuple[str, ...] = DEFAULT_HTTP_ALLOWLIST
        else:
            self._allowlist = tuple(h.lower() for h in allowlist)
        programs = [program] + list(program.modules.values())
        for prog in programs:
            for st in prog.structs:
                self._structs[st.name] = st
            for en in prog.enums:
                self._enums[en.name] = en
            for fn in prog.functions:
                self._functions[fn.name] = fn
            for imp in prog.imports:
                if imp.module == "http":
                    self._http_aliases.add(imp.bound_name)

    # ------------------------------------------------------------ diagnostics

    def _error(self, span: Span, message: str,
               help: str | None = None) -> None:
        self.diagnostics.append(Diagnostic(
            span=span, code=HTTP_ERROR_CODE, message=message,
            severity="error", help=help))

    # ------------------------------------------------------------ entry point

    def run(self) -> list[Diagnostic]:
        prog = self.program
        for sub in [prog] + list(prog.modules.values()):
            for fn in sub.functions:
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
                # the value's type, so the name is still known (a
                # let-bound non-lambda handler, for example, must be
                # told apart from an unknown name).
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
            if self._is_http_call(expr):
                self._check_http_call(expr, expected)
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
                    and expr.obj.name in self._http_aliases:
                self._error(expr.span,
                            f"`http.{expr.name}` is a function, not a value",
                            help="call it, for example "
                                 f"`http.{expr.name}(...)`")
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

    # ------------------------------------------------------------ http calls

    def _is_http_call(self, expr, name: str | None = None) -> bool:
        """True when ``expr`` is a call of ``http.<name>`` (or any
        http member when ``name`` is None), by bare name or by an
        import alias of the http module."""
        if not isinstance(expr, A.Call):
            return False
        func = expr.func
        if not isinstance(func, A.Member):
            return False
        if not isinstance(func.obj, A.Name):
            return False
        if func.obj.name not in self._http_aliases:
            return False
        return name is None or func.name == name

    def _check_http_call(self, call: A.Call,
                         expected: Type | None) -> None:
        name = call.func.name
        if call.kwargs:
            self._error(call.span,
                        f"`http.{name}` takes no named arguments",
                        help="pass values positionally, in order")
            for _n, value, _s in call.kwargs:
                self._walk_expr(value, None)
        if name not in HTTP_FUNCTIONS:
            match = difflib.get_close_matches(
                name, sorted(HTTP_FUNCTIONS), n=1, cutoff=0.7)
            if match:
                help = ("there is an http function with a similar "
                        f"name: `{match[0]}`")
            else:
                help = ("http functions: "
                        + ", ".join(f"`{n}`"
                                    for n in sorted(HTTP_FUNCTIONS)))
            self._error(call.span,
                        f"http has no function `{name}`",
                        help=help)
            for arg in call.args:
                self._walk_expr(arg, None)
            return
        if name == "route":
            self._check_route(call)
        elif name == "serve":
            self._check_serve(call)
        elif name == "get":
            self._check_get(call, expected)
        elif name == "post":
            self._check_post(call, expected)
        elif name == "request":
            self._check_request(call, expected)

    def _check_arity(self, call: A.Call, name: str,
                     minimum: int, maximum: int | None) -> bool:
        n = len(call.args)
        if n < minimum:
            self._error(call.span,
                        f"`http.{name}` takes at least {minimum} "
                        f"argument(s), got {n}",
                        help=f"give `http.{name}` its "
                             f"{HTTP_MEMBERS[name]['sig']}")
            return False
        if maximum is not None and n > maximum:
            self._error(call.span,
                        f"`http.{name}` takes at most {maximum} "
                        f"argument(s), got {n}",
                        help=f"give `http.{name}` its "
                             f"{HTTP_MEMBERS[name]['sig']}")
            return False
        return True

    def _check_str_arg(self, arg, what: str) -> str | None:
        """A string argument: type-checked when known. Returns the
        literal text when it is a string literal, else None (the
        literal-only checks are skipped for computed values, and a
        bad value throws at runtime)."""
        t = getattr(arg, "expr_type", None)
        if t is not None and (t.name != "str" or t.nullable):
            self._error(arg.span,
                        f"{what} must be a `str`, got `{t}`",
                        help=f"pass {what} as a string")
            return None
        if isinstance(arg, A.Lit) and isinstance(arg.value, str):
            return arg.value
        return None

    def _check_method_literal(self, method: str | None,
                              span: Span) -> None:
        """A literal method must be one of HTTP_METHODS, uppercase."""
        if method is None:
            return
        if method not in HTTP_METHODS:
            self._error(span,
                        f"http method must be one of "
                        f"{', '.join(sorted(HTTP_METHODS))}, "
                        f"got `{method}`",
                        help="methods are uppercase: GET, POST, PUT, "
                             "PATCH, DELETE, HEAD, OPTIONS")

    def _check_path_literal(self, path: str | None,
                            span: Span) -> None:
        """A literal route path must start with "/"."""
        if path is None:
            return
        if not path.startswith("/"):
            self._error(span,
                        f"route path must start with `/`, got `{path}`",
                        help="write the path with a leading slash, "
                             "for example `\"/todos\"`")

    def _check_url_literal(self, url: str | None,
                           span: Span) -> None:
        """A literal fetch URL: an http(s) scheme, a host, no
        userinfo, and a host on the allowlist."""
        if url is None:
            return
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            self._error(span,
                        f"fetch URL must start with `http://` or "
                        f"`https://`, got `{url}`",
                        help="write the full URL, for example "
                             "\"http://127.0.0.1:8080/todos\"")
            return
        if not parsed.hostname:
            self._error(span,
                        f"fetch URL has no host: `{url}`",
                        help="write the full URL with a host")
            return
        if parsed.username is not None:
            self._error(span,
                        "a URL must not carry userinfo "
                        f"(`{parsed.username}@...`)",
                        help="secrets never go in a URL; read the "
                             "token from env with `env.get` and pass "
                             "it as a header")
            return
        host = parsed.hostname.lower()
        if host not in self._allowlist:
            allowed = ", ".join(self._allowlist)
            self._error(span,
                        f"fetch host `{host}` is not on the allowlist "
                        f"({allowed})",
                        help="add the host to `http_allowlist` in "
                             "newr.json, or fetch a localhost URL")

    def _check_timeout_arg(self, arg) -> None:
        """A timeout argument: an int (when known), positive when
        literal."""
        self._walk_expr(arg, None)
        t = getattr(arg, "expr_type", None)
        if t is not None and (t.name != "int" or t.nullable):
            self._error(arg.span,
                        f"the timeout must be an `int` (seconds), "
                        f"got `{t}`",
                        help="pass whole seconds, for example `10`")
            return
        value = _int_literal(arg)
        if value is not None and value <= 0:
            self._error(arg.span,
                        "the timeout must be a positive number "
                        f"of seconds, got `{value}`",
                        help="pass whole seconds, or leave the "
                             "timeout out for the default "
                             f"{DEFAULT_HTTP_TIMEOUT_SECONDS}")

    def _check_headers_arg(self, arg) -> None:
        """A headers argument must be `dict[str, str]` (when known)."""
        self._walk_expr(arg, None)
        t = getattr(arg, "expr_type", None)
        if t is None:
            return
        if t.name != "dict" or t.nullable or len(t.params) != 2 \
                or t.params[0].name != "str" \
                or t.params[1].name != "str":
            self._error(arg.span,
                        f"headers must be `dict[str, str]`, got `{t}`",
                        help="pass headers as a dict of strings, for "
                             "example `{\"Authorization\": \"Bearer \" + token}`")

    def _check_struct_shape(self, t: Type,
                            fields: tuple[tuple[str, object], ...],
                            what: str, span: Span) -> bool:
        """True when ``t`` names a declared struct with exactly the
        given fields (name and type each). Reports E2012 naming the
        first mismatch and returns False. An undeclared struct name
        is already E3001 from the core checker; stay quiet and
        return False rather than piling on. ``span`` is the call
        span, the fallback when a type carries no span of its own."""
        if t.nullable or t.name in BUILTIN_TYPE_NAMES or t.params:
            self._error(t.span or span,
                        f"{what} must be a struct, got `{t}`",
                        help=f"declare a struct with the {what} fields "
                             "(SPEC.md SS12.6)")
            return False
        short = t.name.rsplit(".", 1)[-1] if "." in t.name else t.name
        if t.name in self._enums or short in self._enums:
            self._error(t.span or span,
                        f"{what} must be a struct, got enum `{t}`",
                        help=f"declare a struct with the {what} fields "
                             "(SPEC.md SS12.6)")
            return False
        decl = self._structs.get(t.name)
        if decl is None and "." in t.name:
            decl = self._structs.get(t.name.rsplit(".", 1)[-1])
        if decl is None:
            return False
        want = dict(fields)
        have = {f.name: f.type for f in decl.fields}
        for name, spec in fields:
            if name not in have:
                self._error(decl.span,
                            f"{what} struct `{decl.name}` is missing "
                            f"field `{name}: {_spec_text(spec)}`",
                            help=f"declare all {len(fields)} fields: "
                                 + ", ".join(
                                     f"`{n}: {_spec_text(s)}`"
                                     for n, s in fields))
                return False
            if not _type_matches(have[name], spec):
                self._error(have[name].span or span,
                            f"{what} struct `{decl.name}` field "
                            f"`{name}` must be `{_spec_text(spec)}`, "
                            f"got `{have[name]}`",
                            help="the request and response field types "
                                 "are fixed (SPEC.md SS12.6)")
                return False
        for name in have:
            if name not in want:
                self._error(decl.span,
                            f"{what} struct `{decl.name}` has extra "
                            f"field `{name}`",
                            help="the runtime cannot fill extra "
                                 "fields, so they are not allowed "
                                 "(SPEC.md SS12.6)")
                return False
        return True

    def _check_route(self, call: A.Call) -> None:
        if not self._check_arity(call, "route", 3, 3):
            return
        method_arg, path_arg, handler_arg = call.args
        method = self._check_str_arg(method_arg, "the route method")
        self._walk_expr(method_arg, None)
        self._check_method_literal(method, method_arg.span)
        path = self._check_str_arg(path_arg, "the route path")
        self._walk_expr(path_arg, None)
        self._check_path_literal(path, path_arg.span)
        if method is not None and path is not None \
                and method in HTTP_METHODS and path.startswith("/"):
            key = (method, path)
            if key in self._routes:
                self._error(call.span,
                            f"duplicate route `{method} {path}`",
                            help="each method and path pair may be "
                                 "registered once")
            else:
                self._routes.add(key)
        self._check_handler(handler_arg)
        call.expr_type = NONE_TYPE

    def _check_handler(self, arg) -> None:
        """A route handler must be a lambda whose one parameter is a
        struct with exactly the HttpRequest fields and whose return
        type is a struct with exactly the HttpResponse fields. The
        handler is an inline lambda (single-expression form: a
        block-body lambda cannot be written as a call argument, the
        parser rejects it) or a let-bound lambda variable, which is
        the shape multi-statement handlers take. The lambda body is
        still walked when inline, so nested http calls inside a
        handler are checked; a let-bound lambda was already walked
        at its let, so it is validated but not walked again."""
        if isinstance(arg, A.Lambda):
            self._validate_handler_lambda(arg)
            self._walk_expr(arg, None)
            return
        if isinstance(arg, A.Name):
            lam = self._lambdas.get(arg.name)
            if lam is not None:
                self._validate_handler_lambda(lam)
                return
            if arg.name in self._functions:
                self._error(arg.span,
                            "a route handler must be a lambda, not a "
                            f"named function `{arg.name}`",
                            help="write the handler as a lambda, for "
                                 "example `let h = fn(req: HttpRequest) "
                                 "-> HttpResponse: ...` then "
                                 "`http.route(\"GET\", \"/path\", h)`")
                return
            if arg.name in self._var_types:
                self._error(arg.span,
                            "a route handler must be a lambda, "
                            f"`{arg.name}` is not one",
                            help="bind a lambda to the name first, for "
                                 "example `let h = fn(req: HttpRequest) "
                                 "-> HttpResponse: ...`")
                return
            # An unknown name is already E3001/E2005 from the core
            # checker; stay quiet rather than piling on.
            self._walk_expr(arg, None)
            return
        self._error(arg.span,
                    "a route handler must be a lambda",
                    help="write the handler inline, for example "
                         "`fn(req: HttpRequest) -> HttpResponse: ...`, "
                         "or bind it with a let first")
        self._walk_expr(arg, None)

    def _validate_handler_lambda(self, lam: A.Lambda) -> None:
        """The parameter/return struct shapes of one handler
        lambda."""
        if len(lam.params) != 1:
            self._error(lam.span,
                        "a route handler takes exactly one parameter "
                        f"(the request), got {len(lam.params)}",
                        help="write `fn(req: HttpRequest) -> "
                             "HttpResponse: ...`")
        else:
            param = lam.params[0]
            self._check_struct_shape(param.type, HTTP_REQUEST_FIELDS,
                                     "the request", lam.span)
        if lam.return_type is None:
            self._error(lam.span,
                        "a route handler must declare a response struct "
                        "return type",
                        help="write `fn(req: HttpRequest) -> "
                             "HttpResponse: ...`")
        else:
            self._check_struct_shape(lam.return_type,
                                     HTTP_RESPONSE_FIELDS, "the response",
                                     lam.span)

    def _check_serve(self, call: A.Call) -> None:
        if not self._check_arity(call, "serve", 1, 2):
            return
        port_arg = call.args[0]
        self._walk_expr(port_arg, None)
        t = getattr(port_arg, "expr_type", None)
        if t is not None and (t.name != "int" or t.nullable):
            self._error(port_arg.span,
                        f"the port must be an `int`, got `{t}`",
                        help="pass a port number, for example `8080`, "
                             "or `0` for an ephemeral port")
        else:
            value = _int_literal(port_arg)
            if value is not None and (value < 0 or value > 65535):
                self._error(port_arg.span,
                            "the port must be 0 to 65535, got "
                            f"`{value}`",
                            help="pass a port number, for example "
                                 "`8080`, or `0` for an ephemeral port")
        if len(call.args) == 2:
            host_arg = call.args[1]
            self._check_str_arg(host_arg, "the host")
            self._walk_expr(host_arg, None)
        call.expr_type = NONE_TYPE

    def _check_fetch_tail(self, call: A.Call, name: str,
                          first: int) -> None:
        """The shared tail of get/post/request after the URL (and
        body/method) args: optional timeout, then optional headers."""
        rest = call.args[first:]
        if len(rest) >= 1:
            self._check_timeout_arg(rest[0])
        if len(rest) >= 2:
            self._check_headers_arg(rest[1])

    def _check_get(self, call: A.Call,
                   expected: Type | None) -> None:
        if not self._check_arity(call, "get", 1, 3):
            return
        url = self._check_str_arg(call.args[0], "the URL")
        self._walk_expr(call.args[0], None)
        self._check_url_literal(url, call.args[0].span)
        self._check_fetch_tail(call, "get", 1)
        self._resolve_response(call, expected)

    def _check_post(self, call: A.Call,
                    expected: Type | None) -> None:
        if not self._check_arity(call, "post", 2, 4):
            return
        url = self._check_str_arg(call.args[0], "the URL")
        self._walk_expr(call.args[0], None)
        self._check_url_literal(url, call.args[0].span)
        body_arg = call.args[1]
        self._check_str_arg(body_arg, "the body")
        self._walk_expr(body_arg, None)
        self._check_fetch_tail(call, "post", 2)
        self._resolve_response(call, expected)

    def _check_request(self, call: A.Call,
                       expected: Type | None) -> None:
        if not self._check_arity(call, "request", 3, 5):
            return
        method_arg = call.args[0]
        method = self._check_str_arg(method_arg, "the method")
        self._walk_expr(method_arg, None)
        self._check_method_literal(method, method_arg.span)
        url = self._check_str_arg(call.args[1], "the URL")
        self._walk_expr(call.args[1], None)
        self._check_url_literal(url, call.args[1].span)
        body_arg = call.args[2]
        self._check_str_arg(body_arg, "the body")
        self._walk_expr(body_arg, None)
        self._check_fetch_tail(call, "request", 3)
        self._resolve_response(call, expected)

    def _resolve_response(self, call: A.Call,
                          expected: Type | None) -> None:
        """Resolve the response struct for a fetch call from the
        expected type, and set ``call.expr_type`` to the struct type
        for the emitters. The call site must carry the expected type
        (a let annotation, a return type, or a function argument
        type)."""
        if expected is None:
            self._error(call.span,
                        "`http.get`/`post`/`request` needs an expected "
                        "response type",
                        help="annotate the let with `HttpResponse`, "
                             "for example `let resp: HttpResponse = "
                             "http.get(url)`")
            return
        if expected.name == "Result" and expected.params:
            expected = expected.params[0]
        if self._check_struct_shape(expected, HTTP_RESPONSE_FIELDS,
                                    "the response", call.span):
            call.expr_type = Type(expected.name)


def check_http(program: A.Program,
               allowlist=None) -> list[Diagnostic]:
    """http diagnostics for a resolver-output Program."""
    return HttpChecker(program, allowlist=allowlist).run()
