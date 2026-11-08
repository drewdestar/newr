"""Core checker for the newr language (SPEC.md SS5.1, SS7, SS9, SS10, SS16).

Types expressions (bidirectional inference R1-R11), enforces the
no-implicit-numeric-coercion rule (G7), nullability (``T?`` / ``?.`` /
``??``), ``Result`` / statement-level ``?``, match exhaustiveness,
contracts, value-type rules, and the W1003/W1005/W1008 warnings.

This module is the CORE language: it must never import anything
Discord (``discord_check`` / ``discord_limits``). Discord entity and
store member types arrive through the ``providers`` parameter of
``check_program`` -- a dict of type-provider tables, normally built
by ``discord_check.discord_providers(program)``:

    providers = {
        "fields":  {type_name: {field_name: Type, ...}, ...},
        "methods": {type_name: {method_name: sig, ...}, ...},
        "stores":  {store_name: {"tables": {table_name:
                       {"columns": [(name, Type), ...],
                        "row_type": Type, "key": str}}}},
    }

``check_program`` returns a ``CompilerResult`` whose diagnostics are
in deterministic walk order. Recovery (SPEC.md SS16): at most ONE
error per function body per compile -- once a body has an error,
further *errors* from that body are suppressed (warnings still fire).
Top-level statements count as one body; each command/event/task
handler body counts as one body; each lambda body counts as one body.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass

from . import ast_nodes as A
from .ast_nodes import (
    BOOL, ERROR_TYPE, FLOAT, INT, NONE_TYPE, STR, Type, fn_of, list_of,
    nullable_of, result_of,
)
from .errors import Diagnostic, Span
from .errors import CompilerResult, MAX_NESTING_DEPTH
from .lexer import INT64_MAX
from .modules import BUILTINS, METHODS, MODULES, TIME_FORMATS

SIMILARITY_CUTOFF = 0.7

#: Poison type: an expression whose type is unknown because an error
#: was already reported (or resolution failed). Any check against it
#: passes, so one bad expression does not cascade into more errors.
UNKNOWN = Type("__unknown__")

#: Entity type names (SPEC.md SS7.1) -- core types, not Discord data.
ENTITY_TYPE_NAMES = set(A.ENTITY_TYPE_NAMES)

#: Fields on the ``error`` type (SPEC.md SS10).
ERROR_FIELDS = {"message": STR, "code": nullable_of(STR)}


def _is_unknown(t: Type | None) -> bool:
    return t is None or t.name == "__unknown__"


def _strip_nullable(t: Type) -> Type:
    return Type(t.name, t.params, span=t.span, nullable=False)


@dataclass
class _Binding:
    type: Type
    kind: str  # var | param | const | option | fn | struct | enum |
    #            module | store | handler | builtin
    span: Span


class _ModuleCtx:
    """Per-module type environment (one per file in the program)."""

    def __init__(self, program: A.Program):
        self.program = program
        self.structs: dict[str, A.StructDecl] = {
            s.name: s for s in program.structs}
        self.enums: dict[str, A.EnumDecl] = {
            e.name: e for e in program.enums}
        self.functions: dict[str, A.FnDecl] = {
            f.name: f for f in program.functions}
        self.top_lets: dict[str, A.Let | A.Const] = {}
        for st in program.stmts:
            if isinstance(st, (A.Let, A.Const)):
                self.top_lets[st.name] = st
        self.stores: dict[str, A.StoreDecl] = {
            s.name: s for s in program.stores}
        # Top-level env: name -> binding (fns, stores, modules, lets).
        self.env: dict[str, _Binding] = {}
        self.top_checked: set[str] = set()
        # Bound name -> _ModuleCtx (file module) or "named:<module>".
        self.imports: dict[str, object] = {}

    def fn_type(self, fn: A.FnDecl) -> Type:
        params = [p.type for p in fn.params]
        ret = fn.return_type if fn.return_type is not None else NONE_TYPE
        return fn_of(params, ret)


class Checker:
    def __init__(self, program: A.Program, providers: dict | None = None):
        self.program = program
        providers = providers or {}
        self._prov_fields: dict[str, dict[str, Type]] = {
            "error": dict(ERROR_FIELDS)}
        for name, fields in (providers.get("fields") or {}).items():
            self._prov_fields.setdefault(name, {}).update(fields)
        self._prov_methods: dict[str, dict] = dict(
            providers.get("methods") or {})
        self._prov_stores: dict = dict(providers.get("stores") or {})
        self.diagnostics: list[Diagnostic] = []
        # Body-error flags for the recovery rule: a stack of [bool].
        self._bodies: list[list[bool]] = []
        # Scope stack (locals) and the module being walked.
        self._scopes: list[dict[str, _Binding]] = []
        self._ctx: _ModuleCtx | None = None
        self._modules: list[_ModuleCtx] = []
        self._module_by_program: dict[int, _ModuleCtx] = {}
        # Walk context (saved/restored around nested walks).
        self._return_type: Type | None = None  # None = not in a fn
        self._fn_name: str | None = None
        self._loop_depth = 0
        self._in_handler = False
        self._in_top_level = False
        self._resolving_top: set[str] = set()
        # Nesting depth of the synth/walk recursion (M-01): a program
        # nesting expressions or blocks deeper than MAX_NESTING_DEPTH
        # gets E1001 instead of crashing with RecursionError.
        self._depth = 0

    # ------------------------------------------------------------ diagnostics

    def _error(self, span: Span, code: str, message: str,
               expected: str | None = None, found: str | None = None,
               help: str | None = None, note: str | None = None) -> bool:
        """Append an error unless this body already has one (recovery).

        Returns True if the diagnostic was recorded.
        """
        if self._bodies and self._bodies[-1][0]:
            return False
        if self._bodies:
            self._bodies[-1][0] = True
        self.diagnostics.append(Diagnostic(
            span=span, code=code, message=message, severity="error",
            expected=expected, found=found, help=help, note=note))
        return True

    def _warn(self, span: Span, code: str, message: str,
              help: str | None = None) -> None:
        self.diagnostics.append(Diagnostic(
            span=span, code=code, message=message, severity="warning",
            help=help))

    def _similar_help(self, name: str, candidates: list[str]) -> str | None:
        close = difflib.get_close_matches(name, candidates, n=3,
                                          cutoff=SIMILARITY_CUTOFF)
        if len(close) == 1:
            return f"there is a similar name: `{close[0]}`"
        if close:
            return "there are similar names: " + ", ".join(
                f"`{c}`" for c in close)
        return None

    def _push_body(self) -> None:
        self._bodies.append([False])

    def _pop_body(self) -> None:
        self._bodies.pop()

    # ------------------------------------------------------------ entry point

    def run(self) -> CompilerResult:
        main_ctx = _ModuleCtx(self.program)
        self._module_by_program[id(self.program)] = main_ctx
        self._modules.append(main_ctx)
        # Walk the whole module graph, not just the main file's imports:
        # nested file imports (a -> b -> c) get their own contexts too.
        # One context per program object, so a file imported twice under
        # two names (or by two parents) is checked exactly once.
        stack = [main_ctx]
        while stack:
            ctx = stack.pop()
            for bound, sub in ctx.program.modules.items():
                existing = self._module_by_program.get(id(sub))
                if existing is not None:
                    ctx.imports[bound] = existing
                    continue
                sub_ctx = _ModuleCtx(sub)
                self._module_by_program[id(sub)] = sub_ctx
                self._modules.append(sub_ctx)
                ctx.imports[bound] = sub_ctx
                stack.append(sub_ctx)
            for imp in ctx.program.imports:
                if imp.module is not None:
                    ctx.imports[imp.bound_name] = f"named:{imp.module}"
        for ctx in self._modules:
            self._setup_module_env(ctx)
        for ctx in self._modules:
            self._validate_decls(ctx)
        # Walk each module: top-level statements, then functions, then
        # (main module) commands / events / tasks.
        for ctx in self._modules:
            self._ctx = ctx
            self._push_body()
            self._in_top_level = True
            try:
                self._walk_stmts(ctx.program.stmts, top_level=True)
            finally:
                self._in_top_level = False
                self._scopes = []
            self._pop_body()
            for fn in ctx.program.functions:
                self._walk_fn(fn)
            if ctx is main_ctx:
                for cmd in ctx.program.commands:
                    self._walk_command(cmd)
                for ev in ctx.program.events:
                    self._walk_event(ev)
                for task in ctx.program.tasks:
                    self._walk_task(task)
        return CompilerResult(program=self.program,
                              diagnostics=self.diagnostics)

    def _setup_module_env(self, ctx: _ModuleCtx) -> None:
        for name, fn in ctx.functions.items():
            ctx.env[name] = _Binding(ctx.fn_type(fn), "fn", fn.span)
        for name, st in ctx.structs.items():
            ctx.env[name] = _Binding(Type(name), "struct", st.span)
        for name, en in ctx.enums.items():
            ctx.env[name] = _Binding(Type(name), "enum", en.span)
        for name, store in ctx.stores.items():
            ctx.env[name] = _Binding(Type("__store__"), "store", store.span)
        for bound, target in ctx.imports.items():
            span = ctx.program.span
            ctx.env[bound] = _Binding(Type("__module__"), "module", span)

    def _validate_decls(self, ctx: _ModuleCtx) -> None:
        self._ctx = ctx
        for st in ctx.program.structs:
            for f in st.fields:
                self._validate_type(f.type, f.span)
        for en in ctx.program.enums:
            for v in en.variants:
                for f in v.fields:
                    self._validate_type(f.type, f.span)
        for fn in ctx.program.functions:
            for p in fn.params:
                self._validate_type(p.type, p.span)
            if fn.return_type is not None:
                self._validate_type(fn.return_type, fn.span)
        # Test names must be unique within a file: the runner
        # reports tests by name, so two tests with one name (two
        # blocks, or a block and a `test_` function) would be
        # indistinguishable in the report. A block name that
        # matches a non-test function is fine: only `test_` names
        # are tests.
        seen_tests: dict[str, str] = {}
        for fn in ctx.program.functions:
            if fn.name.startswith("test_"):
                seen_tests[fn.name] = "test function"
        for tb in ctx.program.test_blocks:
            if tb.name in seen_tests:
                self._error(
                    tb.span, "E3003",
                    f"duplicate test name `{tb.name}`",
                    help=f"a {seen_tests[tb.name]} is already named "
                         f"`{tb.name}` in this file; test names must "
                         f"be unique within a file so the runner can "
                         f"tell them apart, so rename one of them")
            else:
                seen_tests[tb.name] = "test block"
        for store in ctx.program.stores:
            for table in store.tables:
                for col in table.columns:
                    self._validate_type(col.type, col.span)
        self._check_struct_cycles(ctx)

    def _check_struct_cycles(self, ctx: _ModuleCtx) -> None:
        graph: dict[str, list[str]] = {}
        for name, st in ctx.structs.items():
            refs = [f.type.name for f in st.fields
                    if f.type.name in ctx.structs]
            graph[name] = refs
        state: dict[str, int] = {}  # 0 unvisited, 1 in stack, 2 done

        def visit(name: str, path: list[str]) -> None:
            state[name] = 1
            for nxt in graph.get(name, []):
                if state.get(nxt) == 1:
                    cycle = " -> ".join(path + [name, nxt])
                    decl = ctx.structs[name]
                    self._error(decl.span, "E2001",
                                f"cyclic struct definition: {cycle}",
                                help="structs are value types; a struct "
                                     "cannot contain itself, directly or "
                                     "indirectly")
                    return
                if state.get(nxt, 0) == 0:
                    visit(nxt, path + [name])
            state[name] = 2

        for name in graph:
            if state.get(name, 0) == 0:
                visit(name, [])

    # ------------------------------------------------------------ type names

    def _validate_type(self, t: Type, span: Span) -> None:
        ctx = self._ctx
        if t.name in A.BUILTIN_TYPE_NAMES:
            arity = A.GENERIC_ARITY.get(t.name)
            if arity is not None and len(t.params) != arity:
                self._error(span, "E2001",
                            f"`{t}` takes {arity} type argument(s)",
                            expected=str(t), found=str(t))
                return
            if t.name == "dict" and t.params and t.params[0] != STR:
                self._error(t.params[0].span or span, "E2001",
                            "dict keys must be `str`",
                            expected="str", found=str(t.params[0]),
                            help="dicts are string-keyed in newr")
            for p in t.params:
                if isinstance(p, Type):
                    self._validate_type(p, p.span or span)
            return
        if t.name in ENTITY_TYPE_NAMES or t.name == "embed":
            if t.params:
                self._error(span, "E2001",
                            f"type `{t.name}` takes no type arguments")
            return
        if ctx is not None and t.name in ctx.structs:
            if t.params:
                self._error(span, "E2001",
                            f"struct `{t.name}` takes no type arguments")
            return
        if ctx is not None and t.name in ctx.enums:
            if t.params:
                self._error(span, "E2001",
                            f"enum `{t.name}` takes no type arguments")
            return
        if ctx is not None:
            for target in ctx.imports.values():
                if isinstance(target, _ModuleCtx) and (
                        t.name in target.structs or t.name in target.enums):
                    return
        if t.name == "option":
            # N-05: `option[int]` is the spelling from other languages;
            # in newr nullability is `T?`.
            self._error(span, "E3001", "unknown type `option`",
                        help="nullability is spelled `T?` in newr — "
                             "write `int?` instead of `option[int]`")
            return
        self._error(span, "E3001", f"unknown type `{t.name}`",
                    help=f"declare a struct or enum named `{t.name}`, "
                         "or use a built-in type like `int`, `str`, "
                         "`list[int]`")

    # ------------------------------------------------------------ scopes

    def _lookup(self, name: str) -> _Binding | None:
        for scope in reversed(self._scopes):
            if name in scope:
                return scope[name]
        ctx = self._ctx
        if ctx is not None and name in ctx.env:
            binding = ctx.env[name]
            if binding.kind in ("var", "const") and name in ctx.top_lets:
                self._resolve_top_let(name)
                return ctx.env.get(name)
            return binding
        return None

    def _resolve_top_let(self, name: str) -> None:
        ctx = self._ctx
        if ctx is None or name in ctx.top_checked or name in self._resolving_top:
            return
        node = ctx.top_lets.get(name)
        if node is None:
            return
        self._resolving_top.add(name)
        saved_bodies = self._bodies
        saved_scopes = self._scopes
        self._bodies = [[False]]
        self._scopes = []
        try:
            self._check_let_const(node, top_level=True)
        finally:
            self._bodies = saved_bodies
            self._scopes = saved_scopes
            self._resolving_top.discard(name)

    def _declare_local(self, name: str, typ: Type, kind: str,
                       span: Span) -> None:
        if not self._scopes:
            self._scopes.append({})
        self._scopes[-1][name] = _Binding(typ, kind, span)

    # ------------------------------------------------------------ check / synth

    def _check(self, expr: A.Expr, expected: Type) -> Type:
        """Check ``expr`` against ``expected``; return its type."""
        if isinstance(expr, A.BinOp) and expr.op == "/" and \
                expected == FLOAT:
            self._maybe_int_div_warning(expr)
        t = self.synth(expr, expected)
        if _is_unknown(t) or _is_unknown(expected):
            return t
        if isinstance(expr, A.Lit) and expr.value is None and \
                expected.nullable:
            # R4: `none` is compatible with any `T?` in check position.
            return t
        if expected.nullable and t == _strip_nullable(expected):
            # R4b (M-03): a `T` value widens to `T?` — a nullable
            # type is inhabited by its inner values as well as `none`.
            return t
        if t != expected:
            help = None
            if {t.name, expected.name} == {"int", "float"}:
                help = ("convert explicitly, e.g. `float(5)` or write "
                        "`5.0` instead of `5`")
            elif t.nullable and not expected.nullable:
                help = ("unwrap the value first, e.g. with `.unwrap()` "
                        "or `?? <default>`")
            else:
                help = (f"change the value to `{expected}`, or change "
                        f"the annotation to `{t}`")
            self._error(expr.span, "E2001", "mismatched types",
                        expected=str(expected), found=str(t), help=help)
            return UNKNOWN
        return t

    def _maybe_int_div_warning(self, expr: A.BinOp) -> None:
        if isinstance(expr.left, A.Lit) and isinstance(expr.left.value, int) \
                and not isinstance(expr.left.value, bool) \
                and isinstance(expr.right, A.Lit) \
                and isinstance(expr.right.value, int) \
                and not isinstance(expr.right.value, bool):
            self._warn(expr.span, "W1008",
                       "(newr-specific warning) integer division in a "
                       "float context: `1 / 2` is truncating integer "
                       "division, so this is 0, not 0.5",
                       help="write `1.0 / 2.0` (or `float(1) / 2.0`) "
                            "for float division")

    def _expect_bool(self, expr: A.Expr, what: str = "condition") -> None:
        t = self.synth(expr, BOOL)
        if _is_unknown(t):
            return
        if t != BOOL:
            self._error(expr.span, "E2004",
                        f"{what} must be `bool`",
                        expected="bool", found=str(t),
                        help="conditions are `bool` in newr; there is no "
                             "truthiness for other types")

    def synth(self, expr: A.Expr | None, expected: Type | None = None) -> Type:
        if expr is None:
            return UNKNOWN
        # M-01: stop deep nesting with E1001 instead of RecursionError.
        self._depth += 1
        if self._depth > MAX_NESTING_DEPTH:
            self._depth -= 1
            self._error(expr.span, "E1001", "too deeply nested",
                        help="split the expression into smaller pieces — "
                             "newr limits nesting to 200 levels so a bad "
                             "input cannot exhaust the stack")
            return UNKNOWN
        method = getattr(self, "_synth_" + type(expr).__name__, None)
        if method is None:  # pragma: no cover - parser builds known nodes
            self._depth -= 1
            raise AssertionError(f"no synth for {type(expr)!r}")
        t = method(expr, expected)
        expr.expr_type = t if not _is_unknown(t) else None
        self._depth -= 1
        return t

    # -- literals and names

    def _synth_Lit(self, expr: A.Lit, expected) -> Type:
        v = expr.value
        if isinstance(v, bool):
            return BOOL
        if isinstance(v, int):
            # The lexer lets 2**63 through so `-9223372036854775808`
            # can be written (M-08); any other literal above the int64
            # maximum is an error here, unless the unary-minus case
            # below consumed it first.
            if v > INT64_MAX:
                self._error(expr.span, "E_INT_OVERFLOW",
                            f"integer literal {v} is outside the 64-bit "
                            "signed range",
                            help="int is a 64-bit signed integer on every "
                                 "target")
                return UNKNOWN
            return INT
        if isinstance(v, float):
            return FLOAT
        if isinstance(v, str):
            return STR
        if v is None:
            return NONE_TYPE
        return UNKNOWN

    def _synth_Name(self, expr: A.Name, expected) -> Type:
        binding = self._lookup(expr.name)
        if binding is not None:
            if binding.kind == "module":
                return UNKNOWN  # resolver errors; do not cascade
            if binding.kind == "struct":
                decl = self._ctx.structs.get(expr.name) if self._ctx else None
                if decl is not None:
                    params = [f.type for f in decl.fields]
                    return fn_of(params, Type(expr.name))
                return UNKNOWN
            if binding.kind == "enum":
                return UNKNOWN
            return binding.type
        if expr.name in BUILTINS:
            spec = BUILTINS[expr.name]
            return self._builtin_fn_type(expr.name, spec, expected)
        if expr.name == "log":
            return fn_of([STR], NONE_TYPE)
        # Undefined names are the resolver's E3001; stay silent here.
        return UNKNOWN

    def _builtin_fn_type(self, name: str, spec: dict,
                         expected: Type | None = None) -> Type:
        # R2 (a): use-site inference. When the expected type is a
        # non-nullable fn type, build the builtin's fn type from it,
        # after validating arity and that the expected param types
        # are ones the builtin accepts. Anything else keeps the old
        # behavior (fn with __unknown__ params), so the unannotated
        # form and invalid annotations still fail closed.
        if expected is not None and expected.name == "fn" \
                and expected.params and not expected.nullable:
            inferred = self._infer_builtin_fn_type(name, expected)
            if inferred is not None:
                return inferred
        params: list[Type] = []
        for p in spec.get("params", []):
            if isinstance(p, Type):
                params.append(p)
            elif p in ("...", "any", "any-num", "lenable"):
                params.append(UNKNOWN)
            elif p == "T":
                params.append(UNKNOWN)
            else:
                params.append(UNKNOWN)
        ret = spec.get("return")
        if isinstance(ret, Type):
            ret_t = ret
        elif ret == "T":
            ret_t = UNKNOWN
        else:
            ret_t = UNKNOWN
        return fn_of(params, ret_t)

    def _infer_builtin_fn_type(self, name: str,
                               expected: Type) -> Type | None:
        """Build a bare builtin's fn type from an expected fn type.

        Returns None when the builtin does not accept the expected
        shape (wrong arity, or a param type the builtin cannot
        take); the caller then falls back to the __unknown__ form,
        which fails closed in _check. The return types mirror the
        call path exactly (len -> int, min/max -> T, abs -> the
        arg type, round -> int with 1 arg and float with 2,
        range -> list[int]). Only the six builtins whose spec
        params are generic get inference; the rest keep today's
        behavior exactly.
        """
        params = list(expected.params[:-1])
        if any(p.nullable for p in params):
            return None
        if name == "len":
            if len(params) != 1:
                return None
            if params[0].name not in ("str", "list", "dict", "set"):
                return None
            return fn_of(params, INT)
        if name in ("min", "max"):
            if len(params) != 2 or params[0] != params[1]:
                return None
            return fn_of(params, params[0])
        if name == "abs":
            if len(params) != 1:
                return None
            if params[0].name not in ("int", "float"):
                return None
            return fn_of(params, params[0])
        if name == "round":
            if len(params) not in (1, 2):
                return None
            if params[0].name not in ("int", "float"):
                return None
            if len(params) == 2 and params[1].name != "int":
                return None
            return fn_of(params, INT if len(params) == 1 else FLOAT)
        if name == "range":
            if len(params) not in (1, 2, 3):
                return None
            if any(p.name != "int" for p in params):
                return None
            return fn_of(params, list_of(INT))
        return None

    # -- operators

    def _synth_BinOp(self, expr: A.BinOp, expected) -> Type:
        op = expr.op
        if op == "??":
            lt = self.synth(expr.left)
            if _is_unknown(lt):
                self.synth(expr.right)
                return UNKNOWN
            if not lt.nullable:
                self._error(expr.span, "E2001",
                            "`??` needs a nullable value on the left",
                            expected="T?", found=str(lt),
                            help="the left side must be `T?`; use plain "
                                 "`or` for non-nullable values")
                self.synth(expr.right)
                return UNKNOWN
            inner = _strip_nullable(lt)
            # M-09 (SPEC.md SS9.2): the right side may be `T` (result
            # `T`), `T?` (result `T?`), or `none` (result `T?`).
            if isinstance(expr.right, A.Lit) and expr.right.value is None:
                return lt
            rt = self.synth(expr.right)
            if _is_unknown(rt):
                return UNKNOWN
            if rt == inner:
                return inner
            if rt == lt:
                return lt
            self._error(expr.right.span, "E2001", "mismatched types",
                        expected=f"`{inner}` or `{lt}`", found=str(rt),
                        help=f"the right side of `??` must be `{inner}` "
                             f"or `{lt}`")
            return UNKNOWN
        if op in ("and", "or"):
            lt = self.synth(expr.left)
            rt = self.synth(expr.right)
            if _is_unknown(lt) or _is_unknown(rt):
                return UNKNOWN
            if op == "or" and lt.nullable:
                # T? or T -> T (SPEC.md SS7.2).
                inner = _strip_nullable(lt)
                if rt == inner:
                    return inner
                self._error(expr.span, "E2001", "mismatched types",
                            expected=str(inner), found=str(rt),
                            help="a `T? or T` expression needs the "
                                 "default to have type `T`")
                return UNKNOWN
            if lt == BOOL and rt == BOOL:
                return BOOL
            bad = lt if lt != BOOL else rt
            self._error(expr.span, "E2001",
                        f"`{op}` needs `bool` operands",
                        expected="bool", found=str(bad),
                        help="write a boolean expression on both sides")
            return UNKNOWN
        if op == "in":
            lt = self.synth(expr.left)
            rt = self.synth(expr.right)
            if _is_unknown(lt) or _is_unknown(rt):
                return UNKNOWN
            ok = False
            if rt == STR:
                ok = lt == STR
            elif rt.name == "list" and rt.params:
                ok = lt == rt.params[0]
            elif rt.name == "set" and rt.params:
                ok = lt == rt.params[0]
            elif rt.name == "dict" and rt.params:
                ok = lt == STR
            if not ok:
                self._error(expr.span, "E2001",
                            f"cannot test `{lt}` for membership in `{rt}`",
                            expected=f"an element of `{rt}`", found=str(lt),
                            help="`in` works for list/dict/set membership "
                                 "and for substrings of a `str`")
                return UNKNOWN
            return BOOL
        if op in ("==", "!="):
            lt = self.synth(expr.left)
            rt = self.synth(expr.right)
            if _is_unknown(lt) or _is_unknown(rt):
                return UNKNOWN
            if lt != rt:
                help = None
                if {lt.name, rt.name} == {"int", "float"}:
                    help = ("compare like with like: convert explicitly, "
                            "e.g. `float(1) == 1.0`")
                self._error(expr.span, "E2001", "mismatched types",
                            expected=str(lt), found=str(rt), help=help)
                return UNKNOWN
            return BOOL
        if op in ("<", "<=", ">", ">="):
            lt = self.synth(expr.left)
            rt = self.synth(expr.right)
            if _is_unknown(lt) or _is_unknown(rt):
                return UNKNOWN
            if lt == rt and lt.name in ("int", "float", "str"):
                return BOOL
            self._error(expr.span, "E2001",
                        f"`{op}` needs two values of the same type "
                        "(`int`, `float`, or `str`)",
                        expected=str(lt), found=str(rt))
            return UNKNOWN
        # Arithmetic: + - * / % **
        lt = self.synth(expr.left)
        rt = self.synth(expr.right)
        if _is_unknown(lt) or _is_unknown(rt):
            return UNKNOWN
        if op == "+" and lt == STR and rt == STR:
            return STR
        if op == "*" and ((lt == STR and rt == INT) or
                          (lt == INT and rt == STR)):
            return STR
        if lt == rt and lt.name in ("int", "float"):
            if op == "/" and expected == FLOAT:
                self._maybe_int_div_warning(expr)
            return lt
        help = None
        if {lt.name, rt.name} == {"int", "float"}:
            help = ("convert explicitly, e.g. `float(5) + 2.5` or "
                    "`5.0 + 2.5`")
        elif lt == STR or rt == STR:
            help = "use interpolation, like `\"{x}\"` -- `+` only joins two `str` values"
        self._error(expr.span, "E2001", "mismatched types",
                    expected=str(lt), found=str(rt), help=help)
        return UNKNOWN

    def _synth_UnaryOp(self, expr: A.UnaryOp, expected) -> Type:
        if expr.op == "-" and isinstance(expr.operand, A.Lit) and \
                isinstance(expr.operand.value, int) and \
                not isinstance(expr.operand.value, bool) and \
                expr.operand.value == INT64_MAX + 1:
            # M-08: `-9223372036854775808` is the int64 minimum. The
            # literal alone is out of range, but directly negated it
            # is exactly representable — accept it without routing the
            # literal through _synth_Lit (which would reject it).
            return INT
        t = self.synth(expr.operand)
        if _is_unknown(t):
            return UNKNOWN
        if expr.op == "-":
            if t.name in ("int", "float"):
                return t
            self._error(expr.span, "E2001",
                        "unary `-` needs a number",
                        expected="int or float", found=str(t))
            return UNKNOWN
        if expr.op == "not":
            if t == BOOL:
                return BOOL
            self._error(expr.span, "E2001",
                        "`not` needs a `bool`",
                        expected="bool", found=str(t))
            return UNKNOWN
        return UNKNOWN

    def _synth_Ternary(self, expr: A.Ternary, expected) -> Type:
        self._expect_bool(expr.cond, "ternary condition")
        tt = self.synth(expr.then_expr)
        et = self.synth(expr.else_expr)
        if _is_unknown(tt) or _is_unknown(et):
            return UNKNOWN
        if tt != et:
            self._error(expr.span, "E2001",
                        "ternary branches must have the same type",
                        expected=str(tt), found=str(et))
            return UNKNOWN
        return tt

    def _synth_Lambda(self, expr: A.Lambda, expected) -> Type:
        for p in expr.params:
            self._validate_type(p.type, p.span)
        ret = expr.return_type if expr.return_type is not None else NONE_TYPE
        if expr.return_type is not None:
            self._validate_type(expr.return_type, expr.span)
        saved_return, saved_fn = self._return_type, self._fn_name
        saved_handler = self._in_handler
        self._return_type, self._fn_name = ret, None
        # A lambda is a function boundary (like the emitter's
        # `_fn_ctx` entry with `in_handler: False`): `?` inside it
        # is judged against the lambda's own return type, never
        # against the enclosing handler.
        self._in_handler = False
        self._push_body()
        self._scopes.append({})
        for p in expr.params:
            self._declare_local(p.name, p.type, "param", p.span)
        if expr.expr_body is not None:
            self._check(expr.expr_body, ret)
        elif expr.body is not None:
            self._walk_stmts(expr.body)
            if ret != NONE_TYPE and not self._body_terminates(expr.body):
                self._error(expr.span, "E2001",
                            "a lambda returning a value must end in a "
                            f"`return` of type `{ret}`",
                            expected=str(ret), found="none")
        self._scopes.pop()
        self._pop_body()
        self._return_type, self._fn_name = saved_return, saved_fn
        self._in_handler = saved_handler
        return fn_of([p.type for p in expr.params], ret)

    def _synth_TryExpr(self, expr: A.TryExpr, expected) -> Type:
        t = self.synth(expr.expr)
        if _is_unknown(t):
            return UNKNOWN
        return result_of(t, ERROR_TYPE)

    def _synth_Propagate(self, expr: A.Propagate, expected) -> Type:
        t = self.synth(expr.expr)
        if _is_unknown(t):
            return UNKNOWN
        # N-05: diagnose the OPERAND first. A `?` after a plain value
        # (`let x = 1 ?`) is an operand problem, not a return-type
        # problem — reporting the return type first misleads.
        is_result = t.name == "Result" and bool(t.params) and not t.nullable
        is_nullable = t.nullable and t.name != "Result"
        if t.name == "Result" and t.nullable:
            # A nullable Result fails BOTH kind rules (G-M2-6): it is
            # neither a plain Result nor a plain nullable.
            self._error(expr.span, "E2001",
                        "`?` on a nullable `Result` has no defined "
                        "meaning",
                        expected="Result[T, E] or T?", found=str(t),
                        help="unwrap it first, then propagate the "
                             "`Result`")
            return UNKNOWN
        if is_result:
            ok = t.params[0]
        elif is_nullable:
            ok = _strip_nullable(t)
        else:
            self._error(expr.span, "E2001",
                        "`?` needs a `Result[T, E]` or a `T?` value",
                        expected="Result[T, E] or T?", found=str(t),
                        help="only a fallible value can be propagated "
                             "with `?` — a plain value has nothing to "
                             "propagate")
            return UNKNOWN
        # The kind rule (D30, mirrored exactly from the emitter's
        # `_propagate_unwrapped`): a Result operand needs a
        # Result-returning function; a nullable operand needs a
        # nullable-returning function. A handler returns nothing, so
        # `?` there just exits the handler (a bare `return`) — either
        # kind is allowed.
        if self._in_handler:
            return ok
        if self._return_type is None:
            self._error(expr.span, "E2001",
                        "`?` may only be used inside a function that "
                        "returns `Result[?, E]` or a `?` type",
                        help="move this into a function returning "
                             "`Result[T, error]`, or handle the error here")
            return UNKNOWN
        if is_result and self._return_type.name != "Result":
            self._error(expr.span, "E2001",
                        "`?` on a `Result` value inside a function "
                        "that does not return a `Result`",
                        expected="Result[?, error]",
                        found=str(self._return_type),
                        help="change the function's return type to "
                             "`Result[?, error]`, or handle the error "
                             "here instead of using `?`")
            return UNKNOWN
        if is_nullable and not self._return_type.nullable:
            self._error(expr.span, "E2001",
                        "`?` on a nullable value inside a function "
                        "that does not return a nullable",
                        expected="T?",
                        found=str(self._return_type),
                        help="change the function's return type to a "
                             "`?` type, or handle the `none` case here "
                             "instead of using `?`")
            return UNKNOWN
        return ok

    # -- collections, strings, embeds

    def _synth_ListLit(self, expr: A.ListLit, expected) -> Type:
        elem_expected = None
        if expected is not None and expected.name == "list" and expected.params:
            elem_expected = expected.params[0]
        if not expr.elements:
            if elem_expected is not None:
                return list_of(elem_expected)
            self._error(expr.span, "E2002",
                        "type annotation needed for an empty list",
                        help="write the type, e.g. `let xs: list[int] = []`")
            return UNKNOWN
        first = self.synth(expr.elements[0], elem_expected)
        for el in expr.elements[1:]:
            t = self.synth(el, elem_expected)
            if _is_unknown(t) or _is_unknown(first):
                continue
            if t != first:
                self._error(el.span, "E2003",
                            "all elements of a list must have the same type",
                            expected=str(first), found=str(t),
                            help="lists are homogeneous in newr; convert "
                                 "the elements to one type first")
                return UNKNOWN
        if _is_unknown(first):
            return UNKNOWN
        return list_of(first)

    def _synth_DictLit(self, expr: A.DictLit, expected) -> Type:
        val_expected = None
        if expected is not None and expected.name == "dict" and len(expected.params) == 2:
            val_expected = expected.params[1]
        if not expr.pairs:
            if val_expected is not None:
                return Type("dict", (STR, val_expected))
            self._error(expr.span, "E2002",
                        "type annotation needed for an empty dict",
                        help="write the type, e.g. "
                             "`let d: dict[str, int] = {}`")
            return UNKNOWN
        first_val = None
        for key, value in expr.pairs:
            kt = self.synth(key)
            if not _is_unknown(kt) and kt != STR:
                self._error(key.span, "E2001",
                            "dict keys must be `str`",
                            expected="str", found=str(kt))
                return UNKNOWN
            vt = self.synth(value, val_expected)
            if _is_unknown(vt):
                continue
            if first_val is None:
                first_val = vt
            elif vt != first_val:
                self._error(value.span, "E2003",
                            "all values of a dict must have the same type",
                            expected=str(first_val), found=str(vt))
                return UNKNOWN
        if first_val is None:
            return UNKNOWN
        return Type("dict", (STR, first_val))

    def _synth_Interp(self, expr: A.Interp, expected) -> Type:
        for part in expr.parts:
            if part.expr is None:
                continue
            t = self.synth(part.expr)
            if _is_unknown(t):
                continue
            if t.nullable:
                self._error(part.expr.span, "E2001",
                            "a `T?` value cannot be interpolated",
                            expected=str(_strip_nullable(t)), found=str(t),
                            help="unwrap it first, e.g. with `.unwrap()` "
                                 "or `?? <default>`")
                continue
            if t.name in ("int", "float", "str", "bool") or \
                    t.name in ENTITY_TYPE_NAMES:
                continue
            self._error(part.expr.span, "E2001",
                        f"a `{t}` value cannot be interpolated",
                        expected="int, float, str, or bool", found=str(t),
                        help=f"convert it explicitly with `str(...)`")
        return STR

    def _synth_EmbedExpr(self, expr: A.EmbedExpr, expected) -> Type:
        for f in expr.fields:
            if f.value is None:
                continue
            if f.kind in ("title", "description"):
                self._check(f.value, STR)
            elif f.kind == "color":
                self._check(f.value, INT)
            elif f.kind == "field":
                self._check(f.value, STR)
        return Type("embed")

    def _synth_TCall(self, expr: A.TCall, expected) -> Type:
        # Key / argument-name validation is the discord checker's job
        # (SPEC.md SS15); the core only types the argument values.
        for _name, value, _span in expr.args:
            self.synth(value)
        return STR

    # -- member / index / call

    def _member_field_type(self, obj_type: Type, name: str, span: Span,
                           optional: bool, obj_expr: A.Expr,
                           _in_chain: bool = False) -> Type:
        """Type of a field access on a value of ``obj_type``.

        ``_in_chain`` is True when this call recursed from a nullable
        receiver: the `?.` was already checked against the OUTER
        receiver, so a non-nullable INNER type is not an error here.
        """
        if _is_unknown(obj_type):
            return UNKNOWN
        if obj_type.nullable:
            inner = _strip_nullable(obj_type)
            if name in METHODS.get("nullable", {}):
                # §16 (hole 2): a bare nullable-method name is a
                # FUNCTION value, not its return type — `v.is_some`
                # is `fn() -> bool`, `v?.is_some` is the nullable
                # fn `(fn() -> bool)?`. (Calls go through
                # `_call_member`, which reads the METHODS table
                # directly, so this only types bare accesses.)
                fn_t = self._method_fn_type(
                    obj_type, name, METHODS["nullable"][name])
                if optional:
                    return nullable_of(fn_t)
                return fn_t
            if optional or self._is_optional_chain(obj_expr):
                ft = self._member_field_type(inner, name, span, True,
                                             obj_expr, _in_chain=True)
                if _is_unknown(ft):
                    return UNKNOWN
                # §16 (hole 2): a fn value reached through `?.` is
                # itself nullable — `xs?.len` is `(fn() -> int)?`,
                # not a plain fn. Returning it unwrapped lied about
                # the type, and calling it died with a raw
                # TypeError when the receiver was `none`.
                return nullable_of(ft)
            self._error(span, "E2001",
                        f"cannot access `.{name}` on a `{obj_type}` value",
                        expected=str(inner), found=str(obj_type),
                        help=f"use `?.{name}` to short-circuit on `none`, "
                             "or unwrap the value first")
            return UNKNOWN
        # M-10 (SPEC.md SS9.2): `?.` on a non-nullable receiver is an
        # error — a non-nullable value is never `none`, so the chain
        # is dead code and almost certainly a typo for `.`.
        if optional and not _in_chain:
            self._error(span, "E2001",
                        f"`?.` on a non-nullable `{obj_type}` value",
                        expected=str(obj_type), found=str(obj_type),
                        help=f"use `.{name}` here — `?.` is only for "
                             "`T?` values; a non-nullable value is never "
                             "`none`")
            return UNKNOWN
        # Struct fields.
        if self._ctx is not None and obj_type.name in self._ctx.structs:
            decl = self._ctx.structs[obj_type.name]
            for f in decl.fields:
                if f.name == name:
                    return f.type
            help = self._similar_help(name, [f.name for f in decl.fields])
            self._error(span, "E3001",
                        f"struct `{obj_type.name}` has no field `{name}`",
                        help=help)
            return UNKNOWN
        # Provider fields (entity types, store row types, `error`).
        fields = self._prov_fields.get(obj_type.name)
        if fields is not None and name in fields:
            return fields[name]
        if fields is not None:
            help = self._similar_help(name, list(fields))
            self._error(span, "E3001",
                        f"`{obj_type.name}` has no field `{name}`",
                        help=help)
            return UNKNOWN
        # A method name used as a value: return its function type.
        methods = self._methods_for(obj_type)
        if name in methods:
            return self._method_fn_type(obj_type, name, methods[name])
        # Enum bare variant: `Color.Red` as a value.
        if self._ctx is not None and obj_type.name in self._ctx.enums:
            decl = self._ctx.enums[obj_type.name]
            for v in decl.variants:
                if v.name == name:
                    if v.fields:
                        self._error(span, "E2001",
                                    f"variant `{name}` needs its fields, "
                                    f"like `{obj_type.name}.{name}(...)`",
                                    help="give the associated values")
                        return UNKNOWN
                    return obj_type
            help = self._similar_help(name, [v.name for v in decl.variants])
            self._error(span, "E3001",
                        f"enum `{obj_type.name}` has no variant `{name}`",
                        help=help)
            return UNKNOWN
        candidates = list(methods)
        if fields is not None:
            candidates += list(fields)
        help = self._similar_help(name, candidates)
        self._error(span, "E3001",
                    f"`{obj_type}` has no field or method `{name}`",
                    help=help)
        return UNKNOWN

    def _is_optional_chain(self, expr: A.Expr) -> bool:
        if isinstance(expr, A.Member):
            return expr.optional or self._is_optional_chain(expr.obj)
        if isinstance(expr, A.Call) and isinstance(expr.func, A.Member):
            # A method call in the chain: `s?.unwrap().upper()` —
            # the receiver of `.upper()` is the `?.unwrap()` call.
            return expr.func.optional or self._is_optional_chain(
                expr.func.obj)
        return False

    def _methods_for(self, obj_type: Type) -> dict:
        if _is_unknown(obj_type):
            return {}
        if obj_type.nullable:
            return METHODS.get("nullable", {})
        if obj_type.name == "Result":
            return METHODS.get("Result", {})
        if obj_type.name in METHODS:
            return METHODS[obj_type.name]
        return self._prov_methods.get(obj_type.name, {})

    def _method_fn_type(self, obj_type: Type, name: str,
                        sig: dict) -> Type:
        mapping = self._receiver_mapping(obj_type)
        params = [self._subst_spec(p, mapping) for p in sig.get("params", [])]
        ret = sig.get("return")
        ret_t = self._subst_spec(ret, mapping) if ret is not None else UNKNOWN
        return fn_of(params, ret_t)

    def _receiver_mapping(self, obj_type: Type) -> dict:
        if obj_type.nullable:
            return {"T": _strip_nullable(obj_type)}
        if obj_type.name == "list" and obj_type.params:
            return {"T": obj_type.params[0]}
        if obj_type.name == "set" and obj_type.params:
            return {"T": obj_type.params[0]}
        if obj_type.name == "dict" and len(obj_type.params) == 2:
            return {"K": obj_type.params[0], "V": obj_type.params[1]}
        if obj_type.name == "Result" and len(obj_type.params) == 2:
            return {"T": obj_type.params[0], "E": obj_type.params[1]}
        return {}

    def _subst_spec(self, spec, mapping: dict) -> Type:
        if isinstance(spec, str):
            if spec in mapping:
                return mapping[spec]
            if spec in ("any", "any-num", "lenable", "..."):
                return UNKNOWN
            return UNKNOWN
        if isinstance(spec, Type):
            if spec.name in mapping and not spec.params:
                return mapping[spec.name]
            params = tuple(self._subst_spec(p, mapping)
                           for p in spec.params)
            return Type(spec.name, params, span=spec.span,
                        nullable=spec.nullable)
        return UNKNOWN

    def _synth_Member(self, expr: A.Member, expected) -> Type:
        obj = expr.obj
        # Module members: `math.pi`, `shop.price_of` (as a value).
        if isinstance(obj, A.Name):
            binding = self._lookup(obj.name)
            if binding is not None and binding.kind == "module":
                return self._module_member_type(obj.name, expr.name,
                                                expr.span, as_value=True)
            if binding is None and obj.name in MODULES:
                # N-09/N-10: named modules are usable by name without
                # an import (SPEC.md SS12.2); the import is optional.
                return self._module_member_type(obj.name, expr.name,
                                                expr.span, as_value=True)
            if binding is not None and binding.kind == "store":
                return self._store_member_type(obj.name, expr.name,
                                               expr.span, as_value=True)
            if obj.name == "log":
                return UNKNOWN
        # Enum construction as a value: `Color.Red`.
        if isinstance(obj, A.Name) and self._ctx is not None and \
                obj.name in self._ctx.enums:
            return self._member_field_type(Type(obj.name), expr.name,
                                           expr.span, expr.optional, obj)
        # §20: qualified imported enum member — `m_helper.Shade.Red`.
        # The enum name itself is not a value (like a local bare enum
        # name, which the emitter also refuses), so only the full
        # chain is typed, from the imported module's enum
        # declaration. The intermediate `m_helper.Shade` node gets no
        # type of its own — the local `Color.Red` path does not type
        # its `Color` either — and the emitters resolve the whole
        # chain at emit time without typing the intermediate node.
        if isinstance(obj, A.Member) and isinstance(obj.obj, A.Name):
            binding = self._lookup(obj.obj.name)
            if binding is not None and binding.kind == "module":
                target = self._ctx.imports.get(obj.obj.name) \
                    if self._ctx else None
                if isinstance(target, _ModuleCtx) and \
                        obj.name in target.enums:
                    return self._imported_enum_member_type(
                        target.enums[obj.name], obj.name, expr)
        obj_type = self.synth(obj)
        if _is_unknown(obj_type):
            return UNKNOWN
        return self._member_field_type(obj_type, expr.name, expr.span,
                                       expr.optional, obj)

    def _imported_enum_member_type(self, decl: A.EnumDecl, enum_name: str,
                                   expr: A.Member) -> Type:
        """Type of `module.Enum.Variant` as a value, mirroring the
        local enum branch of `_member_field_type` (same codes, same
        wording): a bare variant is the enum type, a fielded variant
        used bare is E2001, an unknown variant is E3001."""
        for v in decl.variants:
            if v.name == expr.name:
                if v.fields:
                    self._error(expr.span, "E2001",
                                f"variant `{expr.name}` needs its fields, "
                                f"like `{enum_name}.{expr.name}(...)`",
                                help="give the associated values")
                    return UNKNOWN
                return Type(enum_name)
        help = self._similar_help(expr.name,
                                  [v.name for v in decl.variants])
        self._error(expr.span, "E3001",
                    f"enum `{enum_name}` has no variant `{expr.name}`",
                    help=help)
        return UNKNOWN

    def _module_member_type(self, bound: str, name: str, span: Span,
                            as_value: bool) -> Type:
        ctx = self._ctx
        target = ctx.imports.get(bound) if ctx else None
        mod = None
        if isinstance(target, str) and target.startswith("named:"):
            mod = target[len("named:"):]
        elif target is None and bound in MODULES:
            # N-09/N-10: named modules are usable by name without an
            # import (SPEC.md SS12.2); the import is optional.
            mod = bound
        if mod is not None:
            if mod == "discord":
                return UNKNOWN  # discord module surface: not core's job
            members = MODULES.get(mod, {})
            if name in members:
                sig = members[name]
                if "const" in sig:
                    return sig["const"]
                return self._sig_fn_type(sig, {})
            help = self._similar_help(name, list(members))
            self._error(span, "E3001",
                        f"module `{mod}` has no member `{name}`",
                        help=help)
            return UNKNOWN
        if isinstance(target, _ModuleCtx):
            if name in target.functions:
                return target.fn_type(target.functions[name])
            if name in target.top_lets:
                self._resolve_top_let_in(target, name)
                b = target.env.get(name)
                return b.type if b is not None else UNKNOWN
            if name in target.structs:
                decl = target.structs[name]
                return fn_of([f.type for f in decl.fields], Type(name))
            if name in target.enums:
                return UNKNOWN
            exported = [n for n, f in target.functions.items()] + \
                       [n for n in target.top_lets] + \
                       [n for n in target.structs] + \
                       [n for n in target.enums]
            help = self._similar_help(name, exported)
            self._error(span, "E3001",
                        f"module `{bound}` has no member `{name}`",
                        help=help)
            return UNKNOWN
        return UNKNOWN

    def _resolve_top_let_in(self, ctx: _ModuleCtx, name: str) -> None:
        saved_ctx, saved_bodies, saved_scopes = \
            self._ctx, self._bodies, self._scopes
        self._ctx = ctx
        self._bodies = [[False]]
        self._scopes = []
        try:
            if name not in ctx.top_checked:
                node = ctx.top_lets.get(name)
                if node is not None:
                    self._check_let_const(node, top_level=True)
        finally:
            self._ctx, self._bodies, self._scopes = \
                saved_ctx, saved_bodies, saved_scopes

    def _sig_fn_type(self, sig: dict, mapping: dict) -> Type:
        params = [self._subst_spec(p, mapping) for p in sig.get("params", [])]
        ret = sig.get("return")
        if isinstance(ret, Type) or isinstance(ret, str):
            ret_t = self._subst_spec(ret, mapping)
        else:
            ret_t = UNKNOWN
        return fn_of(params, ret_t)

    def _store_member_type(self, store_name: str, name: str, span: Span,
                           as_value: bool) -> Type:
        info = self._prov_stores.get(store_name)
        if info is None:
            return UNKNOWN
        if name in ("balance",):
            return fn_of([UNKNOWN], INT)
        if name in ("credit",):
            return fn_of([UNKNOWN, INT, STR], NONE_TYPE)
        if name in ("debit", "transfer"):
            return fn_of([UNKNOWN, INT, STR], BOOL)
        if name == "get":
            return fn_of([STR, UNKNOWN], UNKNOWN)
        if name in ("set", "delete"):
            return fn_of([STR, UNKNOWN], NONE_TYPE)
        help = self._similar_help(
            name, ["balance", "credit", "debit", "transfer",
                   "get", "set", "delete"])
        self._error(span, "E3001",
                    f"store `{store_name}` has no operation `{name}`",
                    help=help)
        return UNKNOWN

    def _synth_Index(self, expr: A.Index, expected) -> Type:
        obj_type = self.synth(expr.obj)
        idx_type = self.synth(expr.index)
        if _is_unknown(obj_type) or _is_unknown(idx_type):
            return UNKNOWN
        if obj_type.name == "list" and obj_type.params:
            if idx_type != INT:
                self._error(expr.index.span, "E2001",
                            "a list index must be `int`",
                            expected="int", found=str(idx_type))
                return UNKNOWN
            return obj_type.params[0]
        if obj_type.name == "dict" and len(obj_type.params) == 2:
            if idx_type != STR:
                self._error(expr.index.span, "E2001",
                            "a dict key must be `str`",
                            expected="str", found=str(idx_type))
                return UNKNOWN
            return obj_type.params[1]
        if obj_type == STR:
            self._error(expr.span, "E2001",
                        "a `str` cannot be indexed by an integer",
                        expected="list[str]", found="str",
                        help="use `.chars()` to get the list of "
                             "characters, then index that")
            return UNKNOWN
        if obj_type.name == "set":
            self._error(expr.span, "E2001",
                        "a `set` cannot be indexed",
                        help="a set is unordered; iterate over it with "
                             "`for`, or use `.contains()`")
            return UNKNOWN
        self._error(expr.span, "E2001",
                    f"a `{obj_type}` value cannot be indexed",
                    expected="list[T] or dict[str, T]", found=str(obj_type))
        return UNKNOWN

    # -- calls

    def _check_args(self, params: list[tuple[str, Type]], args: list,
                    kwargs: list, span: Span, what: str) -> None:
        """Check positional + named args against ``params`` (name, type)."""
        seen: set[str] = set()
        by_name = {name: t for name, t in params}
        for i, arg in enumerate(args):
            if i >= len(params):
                self._error(arg.span, "E3002",
                            f"`{what}` takes {len(params)} argument(s), "
                            f"got {len(args)}",
                            help="remove the extra argument(s)")
                self.synth(arg)
                continue
            name, ptype = params[i]
            seen.add(name)
            self._check(arg, ptype)
        for name, value, name_span in kwargs:
            if name in seen:
                self._error(name_span, "E3002",
                            f"argument `{name}` was given twice",
                            help="pass it once, positionally or by name")
                self.synth(value)
                continue
            if name not in by_name:
                help = self._similar_help(name, list(by_name))
                self._error(name_span, "E3001",
                            f"`{what}` has no parameter `{name}`",
                            help=help)
                self.synth(value)
                continue
            seen.add(name)
            self._check(value, by_name[name])
        for name, ptype in params:
            if name not in seen:
                self._error(span, "E3002",
                            f"`{what}` is missing argument `{name}`",
                            help=f"pass `{name}: ...`")
                return

    def _synth_Call(self, expr: A.Call, expected) -> Type:
        func = expr.func
        # Builtins by bare name.
        if isinstance(func, A.Name):
            name = func.name
            if name in BUILTINS:
                return self._call_builtin(name, BUILTINS[name], expr,
                                          expected)
            if name == "log":
                for arg in expr.args:
                    self.synth(arg)
                for _n, v, _s in expr.kwargs:
                    self.synth(v)
                return NONE_TYPE
            if name == "t":  # pragma: no cover - parser makes TCall
                return STR
            binding = self._lookup(name)
            if binding is not None and binding.kind == "fn":
                fn = self._ctx.functions[name]
                params = [(p.name, p.type) for p in fn.params]
                self._check_args(params, expr.args, expr.kwargs,
                                 expr.span, name)
                return fn.return_type if fn.return_type is not None \
                    else NONE_TYPE
            if binding is not None and binding.kind == "struct":
                return self._call_struct(name, expr)
            if binding is not None and binding.kind == "enum":
                self._error(expr.span, "E2001",
                            f"use `{name}.<Variant>` to build an enum value",
                            help="an enum value names its variant")
                for arg in expr.args:
                    self.synth(arg)
                return UNKNOWN
            if binding is not None and binding.kind == "module":
                self._error(expr.span, "E2001",
                            f"module `{name}` cannot be called",
                            help="call a function from the module, e.g. "
                                 f"`{name}.<function>(...)`")
                for arg in expr.args:
                    self.synth(arg)
                return UNKNOWN
            if binding is not None:
                # A variable holding a function value.
                ft = binding.type
                if ft.name == "fn" and ft.nullable:
                    # §16 (hole 2): a nullable fn value may be
                    # `none` — calling it directly would die with a
                    # raw TypeError, so it is an error at check.
                    self._error(expr.span, "E2001",
                                f"`{name}` may be `none` — unwrap it "
                                "before calling it",
                                expected="fn(...) -> ...", found=str(ft),
                                help="use `.unwrap()` to get the "
                                     "function first")
                    for arg in expr.args:
                        self.synth(arg)
                    return UNKNOWN
                if ft.name == "fn" and ft.params:
                    params = [(f"arg{i}", p)
                              for i, p in enumerate(ft.params[:-1])]
                    self._check_args(params, expr.args, expr.kwargs,
                                     expr.span, name)
                    return ft.params[-1]
                self._error(expr.span, "E2001",
                            f"`{name}` is not a function",
                            expected="fn(...) -> ...", found=str(ft),
                            help="only functions can be called")
                for arg in expr.args:
                    self.synth(arg)
                return UNKNOWN
            # Undefined name: the resolver already reported E3001.
            for arg in expr.args:
                self.synth(arg)
            for _n, v, _s in expr.kwargs:
                self.synth(v)
            return UNKNOWN
        # Member callee: module / store / enum-variant / method / log.
        if isinstance(func, A.Member):
            return self._call_member(func, expr, expected)
        # Any other callee expression (lambda, parenthesised fn value).
        ft = self.synth(func)
        if _is_unknown(ft):
            for arg in expr.args:
                self.synth(arg)
            return UNKNOWN
        if ft.name == "fn" and ft.nullable:
            # §16 (hole 2): same rule as for a named fn value — a
            # nullable fn may be `none`, so calling it directly is
            # an error at check, not a raw TypeError at runtime.
            self._error(expr.span, "E2001",
                        "this function value may be `none` — unwrap "
                        "it before calling it",
                        expected="fn(...) -> ...", found=str(ft),
                        help="use `.unwrap()` to get the function "
                             "first")
            for arg in expr.args:
                self.synth(arg)
            return UNKNOWN
        if ft.name != "fn" or not ft.params:
            self._error(expr.span, "E2001",
                        "this value is not a function",
                        expected="fn(...) -> ...", found=str(ft),
                        help="only functions can be called")
            for arg in expr.args:
                self.synth(arg)
            return UNKNOWN
        params = [(f"arg{i}", p) for i, p in enumerate(ft.params[:-1])]
        self._check_args(params, expr.args, expr.kwargs, expr.span,
                         "function")
        return ft.params[-1]

    def _call_struct(self, name: str, expr: A.Call) -> Type:
        decl = self._ctx.structs[name]
        fields = [(f.name, f.type) for f in decl.fields]
        if expr.args:
            self._error(expr.args[0].span, "E3002",
                        f"struct `{name}` is built with named arguments "
                        "only, like `Point(x: 1, y: 2)`",
                        help="remove the positional arguments")
            for arg in expr.args:
                self.synth(arg)
        self._check_args(fields, [], expr.kwargs, expr.span, name)
        return Type(name)

    def _call_member(self, func: A.Member, expr: A.Call,
                     expected) -> Type:
        obj = func.obj
        if isinstance(obj, A.Name):
            binding = self._lookup(obj.name)
            if binding is not None and binding.kind == "module":
                return self._call_module_member(obj.name, func.name, expr)
            if binding is None and obj.name in MODULES:
                # N-09/N-10: named modules are usable by name without
                # an import (SPEC.md SS12.2); the import is optional.
                return self._call_module_member(obj.name, func.name, expr)
            if binding is not None and binding.kind == "store":
                return self._call_store(obj.name, func.name, expr)
            if obj.name == "log" and func.name in ("info", "warn", "error"):
                if len(expr.args) != 1 or expr.kwargs:
                    self._error(expr.span, "E3002",
                                f"`log.{func.name}` takes exactly one "
                                "message argument")
                for arg in expr.args:
                    self._check(arg, STR)
                return NONE_TYPE
            if self._ctx is not None and obj.name in self._ctx.enums:
                return self._call_enum_variant(obj.name, func.name, expr)
        # §20: qualified imported fielded-variant construction —
        # `m_helper.Shape.Circle(radius: 1.5)`. Mirrors the local
        # enum branch above, but takes the declaration from the
        # imported module's context instead of this file's.
        if isinstance(obj, A.Member) and isinstance(obj.obj, A.Name):
            binding = self._lookup(obj.obj.name)
            if binding is not None and binding.kind == "module":
                target = self._ctx.imports.get(obj.obj.name) \
                    if self._ctx else None
                if isinstance(target, _ModuleCtx) and \
                        obj.name in target.enums:
                    return self._call_enum_variant(
                        obj.name, func.name, expr,
                        decl=target.enums[obj.name])
        # Method call on a value.
        obj_type = self.synth(obj)
        if _is_unknown(obj_type):
            for arg in expr.args:
                self.synth(arg)
            return UNKNOWN
        # Methods defined on T? itself (is_some / is_none / unwrap).
        if obj_type.nullable and func.name in METHODS.get("nullable", {}):
            sig = METHODS["nullable"][func.name]
            ret = self._call_method_sig(obj_type, func.name, sig, expr)
            if _is_unknown(ret):
                return ret
            if not func.optional:
                # `.unwrap()` / `.is_some()` / `.is_none()` on a
                # nullable: the plain (non-optional) form.
                return ret
            # `?.is_some()` / `?.is_none()` / `?.unwrap()`: the chain
            # short-circuits on `none`, so the result is nullable —
            # EXCEPT `?.unwrap()` on a nullable Result, where the
            # `?` unwraps the outer nullable and `.unwrap()` unwraps
            # the Result inside, so the result is the Result's value
            # type, still nullable.
            if func.name == "unwrap":
                inner = _strip_nullable(obj_type)
                if inner.name == "Result" and inner.params:
                    return nullable_of(inner.params[0])
            return nullable_of(ret)
        if obj_type.nullable:
            # `?.method()` (or a chain after `?.`) unwraps the result.
            inner = _strip_nullable(obj_type)
            methods = self._methods_for(inner)
            if func.name in methods:
                if not func.optional and not self._is_optional_chain(obj):
                    self._error(func.span, "E2001",
                                f"cannot call `.{func.name}` on a "
                                f"`{obj_type}` value",
                                expected=str(inner), found=str(obj_type),
                                help=f"use `?.{func.name}(...)` or unwrap "
                                     "the value first")
                    for arg in expr.args:
                        self.synth(arg)
                    return UNKNOWN
                sig = methods[func.name]
                ret = self._call_method_sig(inner, func.name, sig, expr)
                if _is_unknown(ret) or ret.name == "fn":
                    return ret
                return nullable_of(ret)
            help = self._similar_help(func.name, list(methods))
            self._error(func.span, "E3001",
                        f"`{obj_type}` has no method `{func.name}`",
                        help=help)
            for arg in expr.args:
                self.synth(arg)
            return UNKNOWN
        methods = self._methods_for(obj_type)
        # M-10 (SPEC.md SS9.2): `?.` on a non-nullable receiver is an
        # error — same rule as field access in _member_field_type.
        if func.optional:
            self._error(func.span, "E2001",
                        f"`?.` on a non-nullable `{obj_type}` value",
                        expected=str(obj_type), found=str(obj_type),
                        help=f"use `.{func.name}(...)` here — `?.` is "
                             "only for `T?` values; a non-nullable value "
                             "is never `none`")
            for arg in expr.args:
                self.synth(arg)
            return UNKNOWN
        if func.name not in methods:
            help = self._similar_help(func.name, list(methods))
            self._error(func.span, "E3001",
                        f"`{obj_type}` has no method `{func.name}`",
                        help=help)
            for arg in expr.args:
                self.synth(arg)
            return UNKNOWN
        return self._call_method_sig(obj_type, func.name,
                                     methods[func.name], expr)

    def _call_method_sig(self, obj_type: Type, name: str, sig: dict,
                         expr: A.Call) -> Type:
        mapping = self._receiver_mapping(obj_type)
        # Resolve "T" from the first argument where the receiver does
        # not already fix it (e.g. list.get(i, default: T)).
        arg_types = [self.synth(a) for a in expr.args]
        for spec in sig.get("params", []):
            if spec == "T" and arg_types and "T" not in mapping:
                mapping["T"] = arg_types[0]
        params = []
        for i, spec in enumerate(sig.get("params", [])):
            if isinstance(spec, str) and spec in ("any", "any-num",
                                                  "lenable"):
                params.append((f"arg{i}", UNKNOWN))
            else:
                params.append((f"arg{i}", self._subst_spec(spec, mapping)))
        self._check_args(params, expr.args, expr.kwargs, expr.span,
                         f".{name}")
        ret = sig.get("return")
        if isinstance(ret, str):
            if ret in mapping:
                return mapping[ret]
            return UNKNOWN
        if isinstance(ret, Type):
            return self._subst_spec(ret, mapping)
        return UNKNOWN

    def _call_module_member(self, bound: str, name: str,
                            expr: A.Call) -> Type:
        ctx = self._ctx
        target = ctx.imports.get(bound) if ctx else None
        mod = None
        if isinstance(target, str) and target.startswith("named:"):
            mod = target[len("named:"):]
        elif target is None and bound in MODULES:
            # N-09/N-10: named modules are usable by name without an
            # import (SPEC.md SS12.2); the import is optional.
            mod = bound
        if mod is not None:
            if mod == "discord":
                for arg in expr.args:
                    self.synth(arg)
                return UNKNOWN
            members = MODULES.get(mod, {})
            if name not in members:
                help = self._similar_help(name, list(members))
                self._error(expr.span, "E3001",
                            f"module `{mod}` has no member `{name}`",
                            help=help)
                for arg in expr.args:
                    self.synth(arg)
                return UNKNOWN
            sig = members[name]
            if "const" in sig:
                self._error(expr.span, "E2001",
                            f"`{mod}.{name}` is a constant, not a function",
                            help="use it without `(...)`")
                return UNKNOWN
            return self._call_sig(sig, expr, f"{mod}.{name}")
        if isinstance(target, _ModuleCtx):
            if name in target.functions:
                fn = target.functions[name]
                params = [(p.name, p.type) for p in fn.params]
                self._check_args(params, expr.args, expr.kwargs,
                                 expr.span, f"{bound}.{name}")
                return fn.return_type if fn.return_type is not None \
                    else NONE_TYPE
            if name in target.top_lets:
                self._error(expr.span, "E2001",
                            f"`{bound}.{name}` is a value, not a function",
                            help="use it without `(...)`")
                for arg in expr.args:
                    self.synth(arg)
                return UNKNOWN
            exported = list(target.functions) + list(target.top_lets) + \
                list(target.structs) + list(target.enums)
            help = self._similar_help(name, exported)
            self._error(expr.span, "E3001",
                        f"module `{bound}` has no member `{name}`",
                        help=help)
            for arg in expr.args:
                self.synth(arg)
            return UNKNOWN
        for arg in expr.args:
            self.synth(arg)
        return UNKNOWN

    def _call_sig(self, sig: dict, expr: A.Call, what: str) -> Type:
        """Call a MODULES/BUILTINS signature dict."""
        params_spec = sig.get("params", [])
        arg_types = [self.synth(a) for a in expr.args]
        for _n, v, _s in expr.kwargs:
            self.synth(v)
        mapping: dict = {}
        for spec, at in zip(params_spec, arg_types):
            if spec == "T" and "T" not in mapping:
                mapping["T"] = at
            elif isinstance(spec, Type) and spec.params:
                for p in spec.params:
                    if isinstance(p, str) and p == "T" and \
                            at.name == "list" and at.params:
                        mapping["T"] = at.params[0]
                    elif isinstance(p, Type) and p.name == "T" and \
                            at.name == "list" and at.params:
                        mapping["T"] = at.params[0]
        # time.format: the format must be one of the fixed set.
        if what == "time.format" and len(expr.args) >= 2 and \
                isinstance(expr.args[1], A.Lit) and \
                isinstance(expr.args[1].value, str) and \
                expr.args[1].value not in TIME_FORMATS:
            self._error(expr.args[1].span, "E2001",
                        f"unknown time format `{expr.args[1].value}`",
                        help="formats are " + ", ".join(sorted(TIME_FORMATS)))
            return UNKNOWN
        optional = sig.get("optional", [])
        total = len(params_spec) + len(optional)
        if not (len(params_spec) <= len(expr.args) <= total) \
                and not sig.get("variadic"):
            self._error(expr.span, "E3002",
                        f"`{what}` takes {len(params_spec)}"
                        + (f" to {total}" if optional else "")
                        + f" argument(s), got {len(expr.args)}")
            return UNKNOWN
        if expr.kwargs:
            self._error(expr.kwargs[0][2], "E3001",
                        f"`{what}` takes no named arguments")
            return UNKNOWN
        for i, spec in enumerate(params_spec):
            if i >= len(arg_types):
                break
            self._check_arg_spec(spec, expr.args[i], arg_types[i], mapping)
        for j, spec in enumerate(optional):
            i = len(params_spec) + j
            if i < len(arg_types):
                self._check_arg_spec(spec, expr.args[i], arg_types[i],
                                     mapping)
        ret = sig.get("return")
        if len(arg_types) > len(params_spec) and \
                "overload_return" in sig:
            return sig["overload_return"]
        if isinstance(ret, str):
            if ret == "T":
                return mapping.get("T", UNKNOWN)
            if ret == "any-num":
                return arg_types[0] if arg_types else UNKNOWN
            return UNKNOWN
        if isinstance(ret, Type):
            return self._subst_spec(ret, mapping)
        return UNKNOWN

    def _check_arg_spec(self, spec, arg, arg_type: Type,
                        mapping: dict) -> None:
        if spec == "any":
            return
        if spec == "any-num":
            if not _is_unknown(arg_type) and arg_type.name not in (
                    "int", "float"):
                self._error(arg.span, "E2001",
                            "this argument must be a number",
                            expected="int or float", found=str(arg_type))
            return
        if spec == "lenable":
            if not _is_unknown(arg_type) and arg_type.name not in (
                    "str", "list", "dict", "set"):
                self._error(arg.span, "E2001",
                            "`len` needs a `str`, `list`, `dict`, or `set`",
                            expected="str, list, dict, or set",
                            found=str(arg_type))
            return
        if spec == "T":
            return  # T is defined by this very argument
        if isinstance(spec, Type):
            expected = self._subst_spec(spec, mapping)
            if _is_unknown(expected):
                return
            self._check(arg, expected)
            return
        if isinstance(spec, str):
            expected = self._subst_spec(spec, mapping)
            if not _is_unknown(expected):
                self._check(arg, expected)

    def _call_builtin(self, name: str, spec: dict, expr: A.Call,
                      expected) -> Type:
        if name == "print":
            for arg in expr.args:
                self.synth(arg)
            for _n, v, _s in expr.kwargs:
                self.synth(v)
            return NONE_TYPE
        if name == "ok":
            if expected is not None and expected.name == "Result" and \
                    expected.params:
                t = expected.params[0]
                if expr.args:
                    self._check(expr.args[0], t)
                return expected
            if not expr.args:
                self._error(expr.span, "E3002",
                            "`ok` takes one argument")
                return UNKNOWN
            t = self.synth(expr.args[0])
            return result_of(t, ERROR_TYPE)
        if name == "err":
            if expected is not None and expected.name == "Result" and \
                    expected.params:
                if expr.args:
                    self._check(expr.args[0], ERROR_TYPE)
                return expected
            if expr.args:
                self._check(expr.args[0], ERROR_TYPE)
            return result_of(UNKNOWN, ERROR_TYPE)
        if name == "set":
            if not expr.args:
                self._error(expr.span, "E3002",
                            "`set` takes one list argument")
                return UNKNOWN
            t = self.synth(expr.args[0])
            if _is_unknown(t):
                return UNKNOWN
            if t.name != "list" or not t.params:
                self._error(expr.args[0].span, "E2001",
                            "`set` takes a list",
                            expected="list[T]", found=str(t))
                return UNKNOWN
            return Type("set", (t.params[0],))
        if name == "float" and len(expr.args) == 1 and \
                isinstance(expr.args[0], A.BinOp) and \
                expr.args[0].op == "/":
            self._maybe_int_div_warning(expr.args[0])
        if name == "range":
            # range(n) / range(a, b) / range(a, b, step): all ints.
            for arg in expr.args:
                self._check(arg, INT)
            if not (1 <= len(expr.args) <= 3):
                self._error(expr.span, "E3002",
                            "`range` takes 1 to 3 arguments")
                return UNKNOWN
            return list_of(INT)
        return self._call_sig(spec, expr, name)

    def _call_enum_variant(self, enum_name: str, variant: str,
                           expr: A.Call, decl=None) -> Type:
        decl = decl if decl is not None else self._ctx.enums[enum_name]
        found = None
        for v in decl.variants:
            if v.name == variant:
                found = v
                break
        if found is None:
            help = self._similar_help(variant,
                                      [v.name for v in decl.variants])
            self._error(expr.span, "E3001",
                        f"enum `{enum_name}` has no variant `{variant}`",
                        help=help)
            for arg in expr.args:
                self.synth(arg)
            return UNKNOWN
        fields = [(f.name, f.type) for f in found.fields]
        if expr.args:
            self._error(expr.args[0].span, "E3002",
                        f"enum variant `{variant}` takes named fields only",
                        help="write `Shape.Circle(radius: 1.5)`")
            for arg in expr.args:
                self.synth(arg)
        self._check_args(fields, [], expr.kwargs, expr.span,
                         f"{enum_name}.{variant}")
        return Type(enum_name)

    def _call_store(self, store_name: str, name: str, expr: A.Call) -> Type:
        info = self._prov_stores.get(store_name)
        if info is None:
            for arg in expr.args:
                self.synth(arg)
            return UNKNOWN
        tables = info.get("tables", {})
        if name == "balance":
            if len(expr.args) != 1 or expr.kwargs:
                self._error(expr.span, "E3002",
                            "`economy.balance` takes one argument (the user)")
                for arg in expr.args:
                    self.synth(arg)
                return INT
            self.synth(expr.args[0])
            return INT
        if name in ("credit", "debit", "transfer"):
            key_args = [k for k, _v, _s in expr.kwargs if k == "key"]
            if not key_args:
                self._error(expr.span, "E3002",
                            f"`economy.{name}` needs an idempotency key "
                            "(`key: \"...\"`)",
                            help="every store mutation takes a `key:` "
                                 "named argument (SPEC.md SS14)")
            for _k, v, s in expr.kwargs:
                if _k == "key":
                    self._check(v, STR)
                else:
                    self._error(s, "E3001",
                                f"`economy.{name}` has no parameter `{_k}`")
                    self.synth(v)
            n_pos = 3 if name == "transfer" else 2
            if len(expr.args) != n_pos:
                self._error(expr.span, "E3002",
                            f"`economy.{name}` takes {n_pos} positional "
                            f"arguments, got {len(expr.args)}")
            for arg in expr.args:
                self.synth(arg)
            return BOOL if name in ("debit", "transfer") else NONE_TYPE
        if name in ("get", "set", "delete"):
            if not expr.args or not isinstance(expr.args[0], A.Lit) or \
                    not isinstance(expr.args[0].value, str):
                self._error(expr.span, "E2001",
                            f"`{store_name}.{name}` takes a table name "
                            "(a string literal) as its first argument")
                for arg in expr.args:
                    self.synth(arg)
                return UNKNOWN
            table_name = expr.args[0].value
            if table_name not in tables:
                help = self._similar_help(table_name, list(tables))
                self._error(expr.args[0].span, "E3001",
                            f"store `{store_name}` has no table "
                            f"`{table_name}`",
                            help=help)
                for arg in expr.args[1:]:
                    self.synth(arg)
                return UNKNOWN
            table = tables[table_name]
            cols = table["columns"]
            if name == "get":
                if len(expr.args) != 2:
                    self._error(expr.span, "E3002",
                                "`store.get` takes a table and a key")
                    return UNKNOWN
                if cols:
                    self._check(expr.args[1], cols[0][1])
                return nullable_of(table["row_type"])
            if name == "set":
                if len(expr.args) != 2:
                    self._error(expr.span, "E3002",
                                "`store.set` takes a table and a row")
                    return NONE_TYPE
                self._check(expr.args[1], table["row_type"])
                return NONE_TYPE
            if len(expr.args) != 2:
                self._error(expr.span, "E3002",
                            "`store.delete` takes a table and a key")
                return NONE_TYPE
            if cols:
                self._check(expr.args[1], cols[0][1])
            return NONE_TYPE
        help = self._similar_help(
            name, ["balance", "credit", "debit", "transfer",
                   "get", "set", "delete"])
        self._error(expr.span, "E3001",
                    f"store `{store_name}` has no operation `{name}`",
                    help=help)
        for arg in expr.args:
            self.synth(arg)
        return UNKNOWN

    # ------------------------------------------------------------ statements

    def _walk_stmts(self, stmts: list, top_level: bool = False,
                    allow_contracts: bool = False) -> None:
        # BUG-M2-1: `_in_top_level` is a flag, not a parameter —
        # nested bodies walked from a top-level statement (an
        # `if` / `for` / `while` body, a `try` catch ...) used to
        # inherit it, so a `let` inside one was treated as a
        # module-level `let`. If it shadowed an already-checked
        # name, `_check_let_const` hit its `top_checked` early
        # return and never checked the value at all (its
        # `expr_type` stayed None, which the emitter then refused
        # to emit). The flag follows the `top_level` argument of
        # THIS walk and is restored afterwards, so a nested walk
        # is never top-level.
        was_top = self._in_top_level
        self._in_top_level = top_level
        try:
            seen_non_contract = False
            terminated = False
            for i, st in enumerate(stmts):
                if terminated:
                    self._warn(st.span, "W1003",
                               "unreachable code",
                               help="this statement can never run -- remove it, "
                                    "or move it before the `return` / `throw` / "
                                    "`break` above")
                if isinstance(st, (A.Require, A.Ensure)):
                    if not allow_contracts or seen_non_contract or \
                            self._return_type is None:
                        self._error(st.span, "E2001",
                                    "`requires` / `ensures` must be the first "
                                    "statements of a function body",
                                    help="move the contract to the top of the "
                                         "function body")
                    self._walk_contract(st)
                    if isinstance(st, A.Require):
                        pass
                    # A contract is not a "non-contract" statement.
                else:
                    seen_non_contract = True
                self._walk_stmt(st)
                if self._stmt_terminates(st):
                    terminated = True
        finally:
            self._in_top_level = was_top

    def _walk_contract(self, st) -> None:
        if isinstance(st, A.Ensure) and self._return_type is not None:
            self._scopes.append({"result": _Binding(
                self._return_type, "param", st.span)})
            try:
                self._expect_bool(st.cond, "ensures condition")
            finally:
                self._scopes.pop()
        else:
            self._expect_bool(st.cond, "requires condition")

    def _walk_stmt(self, st) -> None:
        # M-01: stop deep nesting with E1001 instead of RecursionError.
        self._depth += 1
        if self._depth > MAX_NESTING_DEPTH:
            self._depth -= 1
            self._error(st.span, "E1001", "too deeply nested",
                        help="split the block into smaller pieces — newr "
                             "limits nesting to 200 levels so a bad input "
                             "cannot exhaust the stack")
            return
        method = getattr(self, "_walk_" + type(st).__name__, None)
        if method is None:  # pragma: no cover - parser builds known nodes
            self._depth -= 1
            raise AssertionError(f"no walk for {type(st)!r}")
        method(st)
        self._depth -= 1

    def _check_let_const(self, st, top_level: bool = False) -> None:
        ctx = self._ctx
        top_level = top_level or self._in_top_level
        if top_level and ctx is not None and st.name in ctx.top_checked:
            return
        if st.type_ann is not None:
            self._validate_type(st.type_ann, st.type_ann.span or st.span)
        if st.type_ann is not None:
            t = self._check(st.value, st.type_ann)
            typ = st.type_ann
        else:
            t = self.synth(st.value)
            typ = t if not _is_unknown(t) else UNKNOWN
        kind = "const" if isinstance(st, A.Const) else "var"
        if top_level and ctx is not None:
            ctx.env[st.name] = _Binding(typ, kind,
                                        st.name_span or st.span)
            ctx.top_checked.add(st.name)
        else:
            self._declare_local(st.name, typ, kind,
                                st.name_span or st.span)

    def _walk_Let(self, st: A.Let) -> None:
        self._check_let_const(st)

    def _walk_Const(self, st: A.Const) -> None:
        self._check_let_const(st)

    def _walk_Assign(self, st: A.Assign) -> None:
        binding = self._lookup(st.name)
        if binding is None:
            self.synth(st.value)
            return
        if binding.kind == "const":
            self._error(st.name_span or st.span, "E2001",
                        f"cannot assign to constant `{st.name}`",
                        help="a `const` never changes; use `let` for a "
                             "mutable binding")
            self.synth(st.value)
            return
        if st.op == "=":
            self._check(st.value, binding.type)
            return
        # Augmented assignment: name = name <op> expr.
        op = st.op[0]
        vt = self.synth(st.value)
        if _is_unknown(vt) or _is_unknown(binding.type):
            return
        if binding.type == vt and binding.type.name in ("int", "float"):
            return
        if op == "+" and binding.type == STR and vt == STR:
            return
        self._error(st.span, "E2001", "mismatched types",
                    expected=str(binding.type), found=str(vt),
                    help=f"`{st.name} {st.op} ...` needs a `{binding.type}` "
                         "value on the right")

    def _walk_If(self, st: A.If) -> None:
        self._expect_bool(st.cond, "`if` condition")
        self._warn_literal_cond(st.cond)
        self._walk_block(st.then_body)
        for cond, body in st.elifs:
            self._expect_bool(cond, "`elif` condition")
            self._warn_literal_cond(cond)
            self._walk_block(body)
        if st.else_body is not None:
            self._walk_block(st.else_body)

    def _warn_literal_cond(self, cond) -> None:
        if isinstance(cond, A.Lit) and isinstance(cond.value, bool):
            word = "always true" if cond.value else "always false"
            self._warn(cond.span, "W1005",
                       f"this condition is {word}",
                       help="one branch of this `if` / `while` can never "
                            "run -- remove the dead branch")

    def _walk_block(self, body: list) -> bool:
        self._scopes.append({})
        try:
            self._walk_stmts(body)
            return self._body_terminates(body)
        finally:
            self._scopes.pop()

    def _walk_For(self, st: A.For) -> None:
        it = self.synth(st.iterable)
        var_type = UNKNOWN
        if not _is_unknown(it):
            if it.name == "list" and it.params:
                var_type = it.params[0]
            elif it.name == "set" and it.params:
                var_type = it.params[0]
            elif it.name == "dict" and len(it.params) == 2:
                var_type = STR
            elif it == STR:
                var_type = STR
            else:
                self._error(st.iterable.span, "E2001",
                            f"cannot iterate over `{it}`",
                            expected="list[T], set[T], dict[str, T], or str",
                            found=str(it),
                            help="use `range(n)` for a counted loop")
        self._scopes.append({})
        self._declare_local(st.var, var_type, "var",
                            st.var_span or st.span)
        saved_loop = self._loop_depth
        self._loop_depth += 1
        try:
            self._walk_stmts(st.body)
        finally:
            self._loop_depth = saved_loop
            self._scopes.pop()

    def _walk_While(self, st: A.While) -> None:
        self._expect_bool(st.cond, "`while` condition")
        self._warn_literal_cond(st.cond)
        saved_loop = self._loop_depth
        self._loop_depth += 1
        try:
            self._walk_block(st.body)
        finally:
            self._loop_depth = saved_loop

    def _walk_Try(self, st: A.Try) -> None:
        self._walk_block(st.body)
        for name, span, body in st.catches:
            self._scopes.append({})
            self._declare_local(name, ERROR_TYPE, "var", span)
            try:
                self._walk_stmts(body)
            finally:
                self._scopes.pop()
        if st.finally_body is not None:
            self._walk_block(st.finally_body)

    def _walk_Throw(self, st: A.Throw) -> None:
        t = self.synth(st.value)
        if not _is_unknown(t) and t != STR and t != ERROR_TYPE:
            self._error(st.value.span, "E2001",
                        "`throw` needs a `str` message or an `error` value",
                        expected="str or error", found=str(t),
                        help="throw a message string, like "
                             "`throw \"out of gold\"`, or re-throw a caught "
                             "error")

    def _walk_Return(self, st: A.Return) -> None:
        if st.value is None:
            if self._return_type is not None and \
                    self._return_type != NONE_TYPE:
                self._error(st.span, "E2001",
                            f"this function must return a value of type "
                            f"`{self._return_type}`",
                            expected=str(self._return_type), found="none",
                            help="write `return <value>`")
            return
        if self._return_type is None:
            self._error(st.span, "E2007",
                        "`return` with a value is only allowed inside a "
                        "function",
                        help="a top-level `return` takes no value; move "
                             "the value into a function")
            self.synth(st.value)
            return
        self._check(st.value, self._return_type)

    def _walk_Break(self, st: A.Break) -> None:
        if self._loop_depth == 0:
            self._error(st.span, "E2007",
                        "`break` outside a loop",
                        help="`break` only makes sense inside a `for` or "
                             "`while` body")

    def _walk_Continue(self, st: A.Continue) -> None:
        if self._loop_depth == 0:
            self._error(st.span, "E2007",
                        "`continue` outside a loop",
                        help="`continue` only makes sense inside a `for` "
                             "or `while` body")

    def _walk_Pass(self, st: A.Pass) -> None:
        return None

    def _walk_Match(self, st: A.Match) -> None:
        scrut = self.synth(st.scrutinee)
        covered: set[str] = set()
        has_wildcard = False
        for arm in st.arms:
            self._scopes.append({})
            try:
                if arm.pattern is not None:
                    self._check_pattern(arm.pattern, scrut, covered)
                    if isinstance(arm.pattern, (A.WildcardPattern,
                                                A.NamePattern)):
                        has_wildcard = True
                    if isinstance(arm.pattern, A.OrPattern):
                        for p in arm.pattern.patterns:
                            if isinstance(p, (A.WildcardPattern,
                                             A.NamePattern)):
                                has_wildcard = True
                if arm.guard is not None:
                    self._expect_bool(arm.guard, "match guard")
                self._walk_stmts(arm.body)
            finally:
                self._scopes.pop()
        if _is_unknown(scrut):
            return
        if scrut.name in (self._ctx.enums if self._ctx else {}):
            decl = self._ctx.enums[scrut.name]
            missing = [v.name for v in decl.variants
                       if v.name not in covered]
            if missing and not has_wildcard:
                self._error(st.span, "E2006",
                            f"match on `{scrut.name}` is not exhaustive: "
                            "missing variant(s) "
                            + ", ".join(f"`{m}`" for m in missing),
                            help="add an arm for each missing variant, or "
                                 "a `_` arm")
        elif not has_wildcard:
            self._error(st.span, "E2006",
                        "match is not exhaustive: a non-enum scrutinee "
                        "needs a `_` arm",
                        help="add a `_:` arm that handles every other value")

    def _check_pattern(self, pat, scrut_type: Type,
                       covered: set[str]) -> None:
        if isinstance(pat, A.WildcardPattern):
            return
        if isinstance(pat, A.NamePattern):
            self._declare_local(pat.name, scrut_type, "var",
                                pat.name_span or pat.span)
            return
        if isinstance(pat, A.LitPattern):
            lit_t = self._synth_Lit(A.Lit(span=pat.span, value=pat.value),
                                    None)
            if pat.value is None:
                if not _is_unknown(scrut_type) and not scrut_type.nullable \
                        and scrut_type != NONE_TYPE:
                    self._error(pat.span, "E2001",
                                "a `none` pattern needs a nullable scrutinee",
                                expected="T?", found=str(scrut_type))
                return
            if not _is_unknown(scrut_type) and not _is_unknown(lit_t) and \
                    lit_t != scrut_type:
                self._error(pat.span, "E2001",
                            "pattern type does not match the scrutinee",
                            expected=str(scrut_type), found=str(lit_t))
            return
        if isinstance(pat, A.EnumPattern):
            if self._ctx is None or pat.enum_name not in self._ctx.enums:
                if self._ctx is not None and pat.enum_name not in (
                        self._ctx.enums):
                    self._error(pat.span, "E3001",
                                f"unknown enum `{pat.enum_name}`")
                return
            decl = self._ctx.enums[pat.enum_name]
            if not _is_unknown(scrut_type) and scrut_type.name != pat.enum_name:
                self._error(pat.span, "E2001",
                            "pattern enum does not match the scrutinee",
                            expected=str(scrut_type),
                            found=pat.enum_name)
                return
            variant = None
            for v in decl.variants:
                if v.name == pat.variant:
                    variant = v
                    break
            if variant is None:
                help = self._similar_help(
                    pat.variant, [v.name for v in decl.variants])
                self._error(pat.span, "E3001",
                            f"enum `{pat.enum_name}` has no variant "
                            f"`{pat.variant}`",
                            help=help)
                return
            covered.add(pat.variant)
            fields = {f.name: f.type for f in variant.fields}
            for fname, sub, fspan in pat.bindings:
                if fname not in fields:
                    help = self._similar_help(fname, list(fields))
                    self._error(fspan, "E3001",
                                f"variant `{pat.variant}` has no field "
                                f"`{fname}`",
                                help=help)
                    continue
                self._check_pattern(sub, fields[fname], covered)
            return
        if isinstance(pat, A.StructPattern):
            if self._ctx is None or pat.struct_name not in self._ctx.structs:
                self._error(pat.span, "E3001",
                            f"unknown struct `{pat.struct_name}`")
                return
            decl = self._ctx.structs[pat.struct_name]
            if not _is_unknown(scrut_type) and \
                    scrut_type.name != pat.struct_name:
                self._error(pat.span, "E2001",
                            "pattern struct does not match the scrutinee",
                            expected=str(scrut_type),
                            found=pat.struct_name)
                return
            fields = {f.name: f.type for f in decl.fields}
            for fname, sub, fspan in pat.fields:
                if fname not in fields:
                    help = self._similar_help(fname, list(fields))
                    self._error(fspan, "E3001",
                                f"struct `{pat.struct_name}` has no field "
                                f"`{fname}`",
                                help=help)
                    continue
                self._check_pattern(sub, fields[fname], covered)
            return
        if isinstance(pat, A.ListPattern):
            if not _is_unknown(scrut_type) and scrut_type.name != "list":
                self._error(pat.span, "E2001",
                            "a list pattern needs a `list` scrutinee",
                            expected="list[T]", found=str(scrut_type))
                return
            elem = scrut_type.params[0] if scrut_type.params else UNKNOWN
            for sub in pat.elements:
                self._check_pattern(sub, elem, covered)
            return
        if isinstance(pat, A.OrPattern):
            for sub in pat.patterns:
                self._check_pattern(sub, scrut_type, covered)
            return

    def _walk_Require(self, st: A.Require) -> None:
        return None  # handled in _walk_stmts / _walk_contract

    def _walk_Ensure(self, st: A.Ensure) -> None:
        return None  # handled in _walk_stmts / _walk_contract

    def _walk_Reply(self, st: A.Reply) -> None:
        t = self.synth(st.value)
        if not _is_unknown(t) and t != STR and t.name != "embed":
            self._error(st.value.span, "E2001",
                        "`reply` needs a `str` or an `embed`",
                        expected="str or embed", found=str(t),
                        help="reply with text, or with an `embed:` value")

    def _walk_Defer(self, st: A.Defer) -> None:
        return None

    def _walk_Edit(self, st: A.Edit) -> None:
        t = self.synth(st.value)
        if not _is_unknown(t) and t != STR and t.name != "embed":
            self._error(st.value.span, "E2001",
                        "`edit` needs a `str` or an `embed`",
                        expected="str or embed", found=str(t))

    def _walk_Followup(self, st: A.Followup) -> None:
        t = self.synth(st.value)
        if not _is_unknown(t) and t != STR and t.name != "embed":
            self._error(st.value.span, "E2001",
                        "`followup` needs a `str` or an `embed`",
                        expected="str or embed", found=str(t))

    def _walk_Send(self, st: A.Send) -> None:
        t = self.synth(st.value)
        if not _is_unknown(t) and t != STR and t.name != "embed":
            self._error(st.value.span, "E2001",
                        "`send` needs a `str` or an `embed`",
                        expected="str or embed", found=str(t))
        ct = self.synth(st.channel)
        if not _is_unknown(ct) and ct.name != "channel":
            self._error(st.channel.span, "E2001",
                        "`send ... to ...` needs a `channel` target",
                        expected="channel", found=str(ct))

    def _walk_Log(self, st: A.Log) -> None:
        self.synth(st.value)

    def _walk_ExprStmt(self, st: A.ExprStmt) -> None:
        self.synth(st.value)

    def _walk_ButtonDecl(self, st: A.ButtonDecl) -> None:
        self._walk_component(st, extra={})

    def _walk_SelectDecl(self, st: A.SelectDecl) -> None:
        self._walk_component(st, extra={"values": list_of(STR)})

    def _walk_ModalDecl(self, st: A.ModalDecl) -> None:
        extra = {f.name: STR for f in st.fields}
        self._walk_component(st, extra=extra)

    def _walk_component(self, st, extra: dict) -> None:
        saved_handler = self._in_handler
        self._in_handler = True
        self._scopes.append({})
        for name, typ in extra.items():
            self._declare_local(name, typ, "handler", st.span)
        try:
            self._push_body()
            self._walk_stmts(st.body)
            self._pop_body()
            if st.on_timeout is not None:
                self._push_body()
                self._walk_stmts(st.on_timeout)
                self._pop_body()
        finally:
            self._scopes.pop()
            self._in_handler = saved_handler

    # ------------------------------------------------------------ declarations

    def _walk_fn(self, fn: A.FnDecl) -> None:
        ret = fn.return_type if fn.return_type is not None else NONE_TYPE
        saved_return, saved_fn, saved_handler = \
            self._return_type, self._fn_name, self._in_handler
        self._return_type, self._fn_name, self._in_handler = \
            ret, fn.name, False
        self._push_body()
        self._scopes.append({})
        for p in fn.params:
            self._declare_local(p.name, p.type, "param", p.span)
        try:
            self._walk_stmts(fn.body, allow_contracts=True)
        finally:
            self._scopes.pop()
            body_errored = self._bodies[-1][0]
            self._pop_body()
            self._return_type, self._fn_name, self._in_handler = \
                saved_return, saved_fn, saved_handler
        if ret != NONE_TYPE and not body_errored and \
                not self._body_terminates(fn.body):
            # The missing-return error belongs to the fn body, but the
            # body has already been popped -- and the recovery rule
            # says a body that already errored reports nothing more.
            self.diagnostics.append(Diagnostic(
                span=fn.span, code="E2001", severity="error",
                message=f"function `{fn.name}` may finish without "
                        f"returning a value of type `{ret}`",
                expected=str(ret), found="none",
                help="end every path through the function with "
                     "`return <value>`"))

    def _walk_command(self, cmd: A.CommandDecl) -> None:
        for opt in cmd.options:
            if opt.type is not None:
                self._validate_type(opt.type, opt.type.span or cmd.span)
            if opt.default is not None and opt.type is not None:
                self._check(opt.default, opt.type)
            if opt.choices:
                for choice in opt.choices:
                    if opt.type is not None:
                        self._check(choice, opt.type)
            if opt.min_value is not None:
                self.synth(opt.min_value)
            if opt.max_value is not None:
                self.synth(opt.max_value)
        saved_return, saved_handler = self._return_type, self._in_handler
        self._return_type, self._in_handler = None, True
        self._push_body()
        self._scopes.append({})
        self._declare_local("user", Type("user"), "handler", cmd.span)
        self._declare_local("guild", Type("guild"), "handler", cmd.span)
        self._declare_local("channel", Type("channel"), "handler", cmd.span)
        self._declare_local("interaction", Type("interaction"), "handler",
                            cmd.span)
        for opt in cmd.options:
            if opt.type is None:
                continue
            typ = opt.type
            if not opt.required and opt.default is None:
                typ = nullable_of(opt.type)
            self._declare_local(opt.name, typ, "option",
                                opt.name_span or cmd.span)
        try:
            self._walk_stmts(cmd.effective_body())
        finally:
            self._scopes.pop()
            self._pop_body()
            self._return_type, self._in_handler = saved_return, saved_handler

    def _walk_event(self, ev: A.EventDecl) -> None:
        bindings: dict[str, Type] = {}
        if ev.kind in ("member_join", "member_remove"):
            bindings = {"user": Type("member"), "guild": Type("guild")}
        elif ev.kind == "ready":
            bindings = {"bot": Type("user")}
        elif ev.kind == "message":
            bindings = {"message": Type("message"), "user": Type("user")}
        saved_return, saved_handler = self._return_type, self._in_handler
        self._return_type, self._in_handler = None, True
        self._push_body()
        self._scopes.append({})
        for name, typ in bindings.items():
            self._declare_local(name, typ, "handler", ev.span)
        try:
            self._walk_stmts(ev.effective_body())
        finally:
            self._scopes.pop()
            self._pop_body()
            self._return_type, self._in_handler = saved_return, saved_handler

    def _walk_task(self, task: A.TaskDecl) -> None:
        saved_return, saved_handler = self._return_type, self._in_handler
        self._return_type, self._in_handler = None, False
        self._push_body()
        try:
            self._walk_stmts(task.body)
        finally:
            self._pop_body()
            self._return_type, self._in_handler = saved_return, saved_handler

    # ------------------------------------------------------------ termination

    def _body_terminates(self, body: list) -> bool:
        return any(self._stmt_terminates(st) for st in body)

    def _stmt_terminates(self, st) -> bool:
        if isinstance(st, (A.Return, A.Throw, A.Break, A.Continue)):
            return True
        if isinstance(st, A.If):
            if st.else_body is None:
                return False
            if not self._body_terminates(st.then_body):
                return False
            if not self._body_terminates(st.else_body):
                return False
            return all(self._body_terminates(b) for _c, b in st.elifs)
        if isinstance(st, A.Match):
            return all(self._body_terminates(a.body) for a in st.arms) \
                and bool(st.arms)
        if isinstance(st, A.Try):
            if not self._body_terminates(st.body):
                return False
            return all(self._body_terminates(b)
                       for _n, _s, b in st.catches) and bool(st.catches)
        if isinstance(st, A.While):
            # `while true:` with no `break` anywhere reachable never
            # returns (SPEC.md SS5.1, unreachable-code rule).
            if isinstance(st.cond, A.Lit) and st.cond.value is True:
                return not self._contains_break(st.body)
            return False
        return False

    def _contains_break(self, stmts: list) -> bool:
        for st in stmts:
            if isinstance(st, A.Break):
                return True
            if isinstance(st, (A.For, A.While)):
                continue  # a break inside a nested loop is not ours
            if isinstance(st, A.If):
                if self._contains_break(st.then_body):
                    return True
                if any(self._contains_break(b) for _c, b in st.elifs):
                    return True
                if st.else_body is not None and \
                        self._contains_break(st.else_body):
                    return True
            if isinstance(st, A.Try):
                if self._contains_break(st.body):
                    return True
                if any(self._contains_break(b)
                       for _n, _s, b in st.catches):
                    return True
                if st.finally_body is not None and \
                        self._contains_break(st.finally_body):
                    return True
            if isinstance(st, A.Match):
                if any(self._contains_break(a.body) for a in st.arms):
                    return True
        return False


# ---------------------------------------------------------------- public API


def check_program(program: A.Program, providers: dict | None = None) \
        -> CompilerResult:
    """Type-check a resolver-output ``Program``.

    ``providers`` is an optional dict of type-provider tables for
    entity/store member access (see the module docstring); normally
    built by ``discord_check.discord_providers(program)``. Returns a
    ``CompilerResult`` with diagnostics in deterministic walk order.
    """
    return Checker(program, providers).run()
