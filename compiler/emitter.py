"""The Python emitter (M2, SPEC.md §18).

``newr build --target py`` lowers a *checked* program (the front end
has already run: parse -> resolve -> check) to readable Python — one
`.newr` file -> one `.py` file, structure preserved, user names
verbatim, helpers only in `newr_rt.py` (imported when used).

Readability is a requirement, not a nicety: the output is code a
human would write — the v0 goldens (`examples/hello.py`,
`examples/showcase.py`) are the style oracle, and the emitter
reproduces them byte-for-byte (BUILD_REPORT.md §14, D2).

The emitter NEVER runs on an unchecked program: the CLI checks first
and exits 4 on errors. If it meets a node without a type (which only
happens on an unchecked program), it raises `EmitterError` — a clean
refusal, never an AttributeError traceback.

Python floor: 3.10 (P1). The emitter uses only the 3.10-safe f-string
subset — alternate quotes inside replacement fields, and any
expression containing a backslash is hoisted to a temp variable
before the statement.

Encoding: every `open()` / `read_text()` in this file passes
`encoding="utf-8"` (CI enforces it).
"""

from __future__ import annotations

import keyword
from dataclasses import dataclass, field
from pathlib import Path

from . import ast_nodes as A
from .ast_nodes import Type, fn_of
from .errors import Span
from .modules import METHODS, MODULES


class EmitterError(Exception):
    """The program cannot be emitted (unchecked input, an unsupported
    construct, or a checker gap the emitter refuses to paper over).
    The CLI reports this cleanly; it is never a traceback."""


@dataclass
class EmitResult:
    """What `emit_program` produced.

    `files` maps an output filename (basename, e.g. `hello.py`) to
    its text. `main_file` is the main output's filename. `uses_rt`
    says whether any file imports `newr_rt` — the caller copies
    `compiler/newr_rt.py` next to the output when it is true.
    """

    files: dict[str, str]
    main_file: str
    uses_rt: bool = False


# ---------------------------------------------------------------- helpers


# Names the emitter itself may import or define in an emitted file.
# A user name colliding with one of these is mangled with a trailing
# underscore (D20). `math` / `random` / `time` / `file` / `env` /
# `discord` are already reserved module names in newr (E3007); `sys`,
# `os` and `logging` are reserved here, by the emitter. The `newr_rt`
# helper FUNCTIONS are imported bare (`from newr_rt import add, ...`,
# pinned by tests/test_emitter.py::test_rt_import_is_usage_tracked),
# so their names are reserved too — a user fn named `add` becomes
# `add_`, or it would shadow the imported helper. (The runtime's
# CLASSES are referenced module-qualified — `newr_rt.NewrError`,
# pinned by test_contract_check_is_not_assert — so their names need
# no reservation; `newr_rt` itself is reserved.)
_RT_HELPERS = frozenset({
    "add", "sub", "mul", "neg", "pow_int", "pow_float", "tdiv", "tmod",
    "fmod", "round_half_away", "to_int", "to_float", "to_bool", "to_str",
    "check_int", "as_error", "opt_chain", "coalesce", "list_get",
    "list_index", "unwrap_opt", "try_result", "time_format", "ok", "err",
})
_EMITTER_RESERVED = frozenset({
    "sys", "os", "logging", "math", "random", "time", "datetime",
    "pathlib", "asyncio", "discord", "aiosqlite", "newr_rt",
    "annotations",
}) | _RT_HELPERS

# Python keywords (keyword.kwlist) + soft keywords that cannot be
# used as identifiers in the positions the emitter writes.
_PY_KEYWORDS = frozenset(keyword.kwlist) | {"match", "case", "type"}


def _py_str(value: str, quote: str = '"') -> str:
    """Render a newr string value as a Python string literal.

    Double quotes unless the value contains one (then single quotes),
    unless it contains both (then `repr`, which escapes). Non-ASCII
    printable characters are emitted literally (UTF-8 source);
    control characters use their escapes.
    """
    out = []
    for ch in value:
        o = ord(ch)
        if ch == "\\":
            out.append("\\\\")
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\t":
            out.append("\\t")
        elif ch == "\r":
            out.append("\\r")
        elif o < 0x20 or o == 0x7F:
            out.append(f"\\x{o:02x}")
        else:
            out.append(ch)
    body = "".join(out)
    if quote == '"' and '"' not in body:
        return f'"{body}"'
    if quote == "'" and "'" not in body:
        return f"'{body}'"
    if '"' not in body:
        return f'"{body}"'
    if "'" not in body:
        return f"'{body}'"
    return repr(value)


def _py_fstr_text(text: str, quote: str = '"') -> str:
    """A literal part of an f-string: braces doubled, escapes as in
    `_py_str` (backslashes are fine here — they are in the literal
    part, not a replacement field). The delimiter quote is escaped
    (``\\"`` / ``\\'``) so a literal quote cannot end the string."""
    out = []
    for ch in text:
        if ch == "{":
            out.append("{{")
        elif ch == "}":
            out.append("}}")
        elif ch == quote:
            out.append("\\" + quote)
        elif ch == "\\":
            out.append("\\\\")
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\t":
            out.append("\\t")
        elif ch == "\r":
            out.append("\\r")
        elif ord(ch) < 0x20 or ord(ch) == 0x7F:
            out.append(f"\\x{ord(ch):02x}")
        else:
            out.append(ch)
    return "".join(out)


# Python precedence (higher binds tighter), loosest -> tightest.
# The emitter's precedence-climbing printer uses this table; newr's
# own table (SPEC §9.2) maps onto it, with the traps documented
# where they bite (notably unary `-`: newr `-a + b` is `-(a + b)`).
_PREC = {
    "ternary": 0,
    "or": 1,
    "and": 2,
    "not": 3,
    "cmp": 4,      # == != < <= > >= in
    "add": 9,      # + -
    "mul": 10,     # * / %
    "unary": 11,   # -x (Python level; newr's operand rule differs)
    "pow": 12,     # **
    "primary": 13,  # calls, indexing, attribute access, atoms
}

_BINOP_PREC = {
    "or": 1, "and": 2,
    "==": 4, "!=": 4, "<": 4, "<=": 4, ">": 4, ">=": 4, "in": 4,
    "+": 9, "-": 9, "*": 10, "/": 10, "%": 10, "**": 12,
}


def _is_int(t: Type | None) -> bool:
    return t is not None and t.name == "int" and not t.nullable


def _is_float(t: Type | None) -> bool:
    return t is not None and t.name == "float" and not t.nullable


def _is_str(t: Type | None) -> bool:
    return t is not None and t.name == "str" and not t.nullable


def _is_bool(t: Type | None) -> bool:
    return t is not None and t.name == "bool" and not t.nullable


# ---------------------------------------------------------------- scopes


@dataclass
class _Binding:
    """One visible name: its Python rendering and its newr type.

    `py` is the Python name (possibly renamed); `interp` is the form
    used inside an interpolation (for handler bindings: the mention /
    name form); `value` is the form used as a value. For ordinary
    variables all three are the Python name. `kind` is one of
    "param" / "local" / "global-let" / "fn" / "struct" / "enum" /
    "module" / "store" / "handler" / "builtin" / "named-module" /
    "file-module". `module` is the real module name for a module
    binding (the `py` name is the alias when the import is aliased).
    """

    py: str
    type: Type | None = None
    kind: str = "local"
    interp: str | None = None
    module: str | None = None

    @property
    def value(self) -> str:
        return self.py

    def interp_form(self) -> str:
        return self.interp if self.interp is not None else self.py


class _Emitter:
    """State for one emitted file set (main file + its imports)."""

    def __init__(self, program: A.Program, *, main_path: str,
                 sources: dict[str, str], line_comments: bool = False):
        self.program = program
        self.main_path = main_path
        self.sources = sources
        self.line_comments = line_comments
        # Imports collected while emitting (per output file — reset
        # for each module file).
        self.plain_imports: set[str] = set()
        self.plain_import_aliases: dict[str, str] = {}
        self.from_imports: dict[str, set[str]] = {}
        self.rt_helpers: set[str] = set()
        self.rt_module_used: bool = False  # `newr_rt.<Class>` referenced
        # Module-level declarations of the file being emitted.
        self.fns: dict[str, A.FnDecl] = {}
        self.structs: dict[str, A.StructDecl] = {}
        self.enums: dict[str, A.EnumDecl] = {}
        self.stores: dict[str, A.StoreDecl] = {}
        self.module_lets: dict[str, Type | None] = {}
        self.imported_modules: dict[str, str] = {}  # bound name -> stem
        # Scope stack: list of dicts (newr name -> _Binding). The
        # bottom scope is the module scope of the file being emitted.
        self._scopes: list[dict[str, _Binding]] = []
        # Python names already taken at module level (for collision
        # aware allocation in _bind_module_level).
        self._module_used: set[str] = set()
        # Python names already used in the current Python function
        # scope (for rename allocation). Reset per function.
        self._fn_used: set[str] = set()
        # `global` names for the function being emitted (collected
        # while emitting its body; inserted at the top afterwards).
        self._fn_globals: set[str] = set()
        # Hoists for the statement being emitted: each is a list of
        # lines (base indent 0) to emit before the statement —
        # f-string temp assignments and hoisted lambda defs.
        self._hoists: list[list[str]] | None = None
        self._hoist_counter = 0
        self._lambda_counter = 0
        self._match_counter = 0
        # Current function context (for `?` and contracts): a stack
        # of dicts {return_type, ensures, in_handler}.
        self._fn_ctx: list[dict] = []
        # Warnings for the caller (stderr): e.g. input() in a bot.
        self.warnings: list[str] = []
        # Whether the program being emitted has a bot block.
        self._is_bot = program.bot is not None
        # Whether a `log <expr>` statement was emitted (a general
        # program then configures logging once, P10).
        self._used_log = False
        # --- bot-emitter state (part 4b) ---
        # Handler-context stack: one dict per handler body being
        # emitted (command / event / task / component / timeout).
        # Keys: "kind", "reply" (how a `reply` is answered),
        # "locale" (the expression for `t()`'s locale argument).
        self._handler_ctx_stack: list[dict] = []
        # Component class definitions collected while emitting
        # handler bodies (each a list of lines, inserted at module
        # level before the command functions).
        self._bot_components: list[list[str]] = []
        self._bot_component_counter = 0
        # The last "answer" line (a Reply / Followup / Edit) emitted
        # in the block currently being built, for component
        # attachment (D40): (index into the block's line list, kind).
        self._last_answer: tuple[int, str] | None = None
        # The view a Button/Select just created, waiting to be
        # attached to the block's last answer (D40).
        self._pending_view: str | None = None
        self._pending_view_timeout = False
        # Counter for unique temp names within a file.
        self._temp_counter = 0

    # ------------------------------------------------------------ errors

    def _fail(self, msg: str):
        raise EmitterError(msg)

    def _type_of(self, expr: A.Expr) -> Type:
        """The checker's type for an expression, or a clean refusal.

        A `None` type only happens on an unchecked program (the CLI
        never emits one); the error says to run `newr check` first.
        """
        t = getattr(expr, "expr_type", None)
        if t is None:
            self._fail(
                "cannot emit this program: an expression has no type — "
                "run `newr check` first (the program may have errors)")
        return t

    def _type_or_none(self, expr: A.Expr) -> Type | None:
        return getattr(expr, "expr_type", None)

    # ------------------------------------------------------------ scopes

    def _push_scope(self):
        self._scopes.append({})

    def _pop_scope(self):
        self._scopes.pop()

    def _lookup(self, name: str) -> _Binding | None:
        for scope in reversed(self._scopes):
            if name in scope:
                return scope[name]
        return None

    def _mangle(self, name: str) -> str:
        """The Python name for a newr name, before collision checks:
        Python keywords and emitter-reserved names get a trailing
        underscore (D20)."""
        if name in _PY_KEYWORDS or name in _EMITTER_RESERVED:
            return name + "_"
        return name

    def _bind(self, name: str, type: Type | None = None,
              kind: str = "local", interp: str | None = None) -> _Binding:
        """Bind `name` in the current scope, renaming on shadow.

        If the name is already visible in an enclosing scope of this
        Python function (or is a keyword / emitter-reserved name),
        the new binding gets a fresh name (`x_1`, `x_2`, ...). This
        is what makes newr block scoping correct in Python, where an
        assignment anywhere in a function makes the name local to the
        whole function: a `let x = 2` inside an `if` must not clobber
        the outer `x` after the block.
        """
        base = self._mangle(name)
        py = base
        if self._lookup(name) is not None or py in self._fn_used:
            n = 1
            while f"{base}_{n}" in self._fn_used:
                n += 1
            py = f"{base}_{n}"
        self._fn_used.add(py)
        binding = _Binding(py=py, type=type, kind=kind, interp=interp)
        self._scopes[-1][name] = binding
        return binding

    def _bind_fresh(self, name: str, type: Type | None = None,
                    kind: str = "local") -> _Binding:
        """Bind `name` in the current scope WITHOUT renaming against
        enclosing scopes — for function/lambda parameters, where
        shadowing an enclosing name is the correct semantics (the
        parameter is a new function-local scope). Only the current
        function's used-name set is consulted, so two parameters
        mangling to the same Python name still get distinct names."""
        base = self._mangle(name)
        py = base
        n = 1
        while py in self._fn_used:
            py = f"{base}_{n}"
            n += 1
        self._fn_used.add(py)
        binding = _Binding(py=py, type=type, kind=kind)
        self._scopes[-1][name] = binding
        return binding

    def _bind_module_level(self, name: str, type: Type | None = None,
                           kind: str = "local") -> _Binding:
        """Bind a name in the module scope. Keyword/reserved mangling
        applies (D20), and the allocation is collision-aware: if the
        mangled name is already taken at module level (e.g. a user
        fn `add` mangled to `add_` colliding with a user fn `add_`),
        a numeric suffix is appended. The checker forbids duplicate
        newr names, so a collision here only ever comes from
        mangling."""
        py = self._mangle(name)
        if py in self._module_used:
            n = 1
            while f"{py}_{n}" in self._module_used:
                n += 1
            py = f"{py}_{n}"
        self._module_used.add(py)
        binding = _Binding(py=py, type=type, kind=kind)
        self._scopes[0][name] = binding
        return binding

    # ------------------------------------------------------------ imports

    def _need_import(self, module: str, alias: str | None = None):
        self.plain_imports.add(module)
        if alias is not None and alias != module:
            self.plain_import_aliases[module] = alias

    def _need_from(self, module: str, name: str):
        self.from_imports.setdefault(module, set()).add(name)

    def _need_rt(self, helper: str):
        self.rt_helpers.add(helper)

    def _need_rt_class(self):
        """A runtime CLASS is referenced (`newr_rt.NewrError`,
        `newr_rt.Result`) — the file needs `import newr_rt`."""
        self.rt_module_used = True

    def _render_imports(self) -> list[str]:
        """The import block, isort-style: stdlib group, blank line,
        third-party group, blank line, first-party group (newr_rt +
        file modules). Within a group, sorted by module; a plain
        `import x` sorts before `from x import ...` of the same
        module. The runtime is imported two ways (both pinned by
        tests): `import newr_rt` when a class is referenced
        (`newr_rt.NewrError`) and `from newr_rt import <helpers>`
        for the bare helper calls."""
        stdlib = {"sys", "os", "math", "random", "time", "logging",
                  "datetime", "pathlib", "typing", "dataclasses",
                  "decimal", "asyncio", "types"}
        third_party = {"discord", "discord.ext", "aiosqlite"}
        groups: list[list[str]] = [[], [], []]
        # (group, module, kind, text): kind 0 = plain import, 1 = from.
        entries: list[tuple[int, str, int, str]] = []
        for mod in sorted(self.plain_imports):
            grp = 0 if mod in stdlib else (1 if mod in third_party else 2)
            alias = self.plain_import_aliases.get(mod)
            text = f"import {mod} as {alias}" if alias else f"import {mod}"
            entries.append((grp, mod, 0, text))
        for mod in sorted(self.from_imports):
            grp = 0 if mod in stdlib else (1 if mod in third_party else 2)
            names = sorted(self.from_imports[mod])
            entries.append((grp, mod, 1,
                            f"from {mod} import {', '.join(names)}"))
        if self.rt_module_used:
            entries.append((2, "newr_rt", 0, "import newr_rt"))
        if self.rt_helpers:
            entries.append(
                (2, "newr_rt", 1,
                 f"from newr_rt import {', '.join(sorted(self.rt_helpers))}"))
        for grp, mod, kind, text in sorted(entries, key=lambda e: (e[0], e[1], e[2], e[3])):
            groups[grp].append(text)
        lines: list[str] = []
        for group in groups:
            if not group:
                continue
            if lines:
                lines.append("")
            lines.extend(group)
        return lines

    # ------------------------------------------------------------ hoists

    def _new_hoist_name(self) -> str:
        self._hoist_counter += 1
        return f"_newr_h{self._hoist_counter - 1}"

    def _new_temp_name(self) -> str:
        self._temp_counter += 1
        return f"_newr_t{self._temp_counter - 1}"

    def _new_lambda_name(self) -> str:
        self._lambda_counter += 1
        return f"_newr_fn_{self._lambda_counter - 1}"

    def _new_match_name(self) -> str:
        self._match_counter += 1
        return f"_newr_m{self._match_counter - 1}"

    # ------------------------------------------------------------ text

    def _line_comment(self, span: Span | None) -> str | None:
        """`# newr:<file>:<line>` for a span (only with --line-comments)."""
        if not self.line_comments or span is None:
            return None
        source = self.sources.get(span.file, "")
        try:
            line, _col = span.line_col(source)[:2]
        except Exception:
            return None
        return f"# newr:{span.file}:{line}"

    # ------------------------------------------------------------ expressions

    def emit_expr(self, node: A.Expr, min_prec: int = 0) -> str:
        """Emit an expression, parenthesised if it binds looser than
        `min_prec` (precedence climbing over the Python table)."""
        text, prec = self._emit_expr(node)
        if prec < min_prec:
            return f"({text})"
        return text

    def _emit_expr(self, node: A.Expr) -> tuple[str, int]:
        """Emit an expression; returns (text, precedence of its top
        construct). Atoms and calls are primaries (13); a lambda is
        the loosest (0)."""
        if isinstance(node, A.Lit):
            return self._emit_lit(node), _PREC["primary"]
        if isinstance(node, A.Name):
            return self._emit_name(node), _PREC["primary"]
        if isinstance(node, A.ListLit):
            items = [self.emit_expr(e, 0) for e in node.elements]
            return "[" + ", ".join(items) + "]", _PREC["primary"]
        if isinstance(node, A.DictLit):
            pairs = [f"{self.emit_expr(k, 0)}: {self.emit_expr(v, 0)}"
                     for k, v in node.pairs]
            return "{" + ", ".join(pairs) + "}", _PREC["primary"]
        if isinstance(node, A.Interp):
            # _emit_interp already returns (text, prec).
            return self._emit_interp(node, '"')
        if isinstance(node, A.Lambda):
            # _emit_lambda already returns (text, prec).
            return self._emit_lambda(node)
        if isinstance(node, A.TryExpr):
            self._need_rt("try_result")
            inner = self.emit_expr(node.expr, 0)
            return f"try_result(lambda: {inner})", _PREC["primary"]
        if isinstance(node, A.Propagate):
            # `?` is statement-level (D7): the parser only creates it
            # as the outermost postfix of a statement RHS, so it is
            # handled by the statement emitter, never here.
            self._fail("`?` may only appear at the end of a statement "
                       "right-hand side (the emitter found it nested "
                       "inside an expression)")
        if isinstance(node, A.TCall):
            return self._emit_tcall(node), _PREC["primary"]
        if isinstance(node, A.EmbedExpr):
            return self._emit_embed(node), _PREC["primary"]
        if isinstance(node, A.BinOp):
            return self._emit_binop(node)
        if isinstance(node, A.UnaryOp):
            return self._emit_unary(node)
        if isinstance(node, A.Ternary):
            cond = self.emit_expr(node.cond, 1)
            then = self.emit_expr(node.then_expr, 1)
            els = self.emit_expr(node.else_expr, 0)
            return f"{then} if {cond} else {els}", 0
        if isinstance(node, (A.Member, A.Index, A.Call)):
            if self._chain_has_optional(node):
                return self._emit_chain(node), _PREC["primary"]
            if isinstance(node, A.Member):
                return self._emit_member(node), _PREC["primary"]
            if isinstance(node, A.Index):
                return self._emit_index(node), _PREC["primary"]
            return self._emit_call(node), _PREC["primary"]
        self._fail(f"cannot emit node {type(node).__name__}")

    # ------------------------------------------------------------ atoms

    def _emit_lit(self, node: A.Lit) -> str:
        v = node.value
        if v is None:
            return "None"
        if isinstance(v, bool):
            return "True" if v else "False"
        if isinstance(v, int):
            return repr(v)
        if isinstance(v, float):
            return repr(v)
        if isinstance(v, str):
            return _py_str(v)
        self._fail(f"cannot emit literal {v!r}")

    def _emit_name(self, node: A.Name) -> str:
        binding = self._lookup(node.name)
        if binding is not None:
            if binding.kind == "store":
                self._fail(f"a store (`{node.name}`) cannot be used "
                           "as a value")
            return binding.value
        # A builtin used as a first-class value: only the ones Python
        # already implements identically are emittable as names.
        if node.name in ("min", "max", "len"):
            return node.name
        if node.name in ("print", "str", "int", "float", "bool", "abs",
                         "round", "range", "set", "ok", "err", "input",
                         "t"):
            self._fail(f"builtin `{node.name}` cannot be used as a "
                       "value (its newr meaning is not a Python "
                       "function)")
        self._fail(f"unknown name `{node.name}` (the program was not "
                   "checked, or the emitter lost a binding)")

    # ------------------------------------------------------------ operators

    def _emit_binop(self, node: A.BinOp) -> tuple[str, int]:
        op = node.op
        if op == "??":
            self._need_rt("coalesce")
            left = self.emit_expr(node.left, 0)
            right = self.emit_expr(node.right, 0)
            return (f"coalesce({left}, lambda: {right})",
                    _PREC["primary"])
        if op in ("and", "or"):
            lt = self._type_of(node.left)
            if op == "or" and lt.nullable:
                # `T? or T` (SPEC §7.2): the default when the value is
                # absent. Python's `or` would also take the default
                # for a present-but-falsy value (0, ""), so it is
                # not the same — use coalesce.
                self._need_rt("coalesce")
                left = self.emit_expr(node.left, 0)
                right = self.emit_expr(node.right, 0)
                return (f"coalesce({left}, lambda: {right})",
                        _PREC["primary"])
            prec = _BINOP_PREC[op]
            left = self.emit_expr(node.left, prec)
            right = self.emit_expr(node.right, prec + 1)
            return f"{left} {op} {right}", prec
        if op == "in":
            left = self.emit_expr(node.left, _BINOP_PREC["in"])
            right = self.emit_expr(node.right, _BINOP_PREC["in"] + 1)
            return f"{left} in {right}", _BINOP_PREC["in"]
        if op in ("==", "!=", "<", "<=", ">", ">="):
            prec = _BINOP_PREC[op]
            left = self.emit_expr(node.left, prec)
            right = self.emit_expr(node.right, prec + 1)
            return f"{left} {op} {right}", prec
        # Arithmetic: the result type decides checked vs bare.
        result = self._type_of(node)
        prec = _BINOP_PREC[op]
        if op == "**":
            left = self.emit_expr(node.left, prec + 1)
            right = self.emit_expr(node.right, prec)
        else:
            left = self.emit_expr(node.left, prec)
            right = self.emit_expr(node.right, prec + 1)
        if _is_int(result):
            helper = {"+": "add", "-": "sub", "*": "mul", "/": "tdiv",
                      "%": "tmod", "**": "pow_int"}[op]
            self._need_rt(helper)
            return f"{helper}({left}, {right})", _PREC["primary"]
        if op == "%" and _is_float(result):
            self._need_rt("fmod")
            return f"fmod({left}, {right})", _PREC["primary"]
        if op == "**" and _is_float(result):
            self._need_rt("pow_float")
            return f"pow_float({left}, {right})", _PREC["primary"]
        return f"{left} {op} {right}", prec

    def _emit_unary(self, node: A.UnaryOp) -> tuple[str, int]:
        if node.op == "not":
            operand = self.emit_expr(node.operand, _PREC["cmp"])
            return f"not {operand}", _PREC["unary"]
        # `-`: the operand binds at `**` level (the newr trap: `-a + b`
        # is `-(a + b)`, so the operand is emitted with min-prec 12).
        # D19: the int64 minimum is written `-9223372036854775808`
        # in newr. Its positive magnitude (9223372036854775808) is
        # out of int64 range, so the checker leaves the literal's
        # own type unset — the plain `neg(...)` form would also be
        # wrong (it would check the out-of-range literal first).
        # Emit the minimum directly.
        if isinstance(node.operand, A.Lit) and \
                node.operand.value == 9223372036854775808:
            return "-9223372036854775808", _PREC["unary"]
        t = self._type_of(node.operand)
        if _is_int(t):
            self._need_rt("neg")
            return f"neg({self.emit_expr(node.operand, 0)})", \
                _PREC["primary"]
        operand = self.emit_expr(node.operand, _PREC["pow"])
        return f"-{operand}", _PREC["unary"]

    # ------------------------------------------------------------ interpolation

    def _emit_interp(self, node: A.Interp, quote: str = '"') -> tuple[str, int]:
        parts: list[str] = []
        for part in node.parts:
            if part.text is not None:
                parts.append(_py_fstr_text(part.text, quote))
            else:
                parts.append("{" + self._emit_interp_part(part.expr, quote) + "}")
        return f"f{quote}{''.join(parts)}{quote}", _PREC["primary"]

    def _emit_interp_part(self, expr: A.Expr, quote: str) -> str:
        """One replacement field. Bools render as `true`/`false`
        (via `to_str`); ints/floats/strs are bare; handler bindings
        use their interp form (the mention / name form)."""
        if isinstance(expr, A.Name):
            binding = self._lookup(expr.name)
            if binding is not None and binding.interp is not None:
                text = binding.interp_form()
                return self._interp_field_or_hoist(text, quote)
        t = self._type_of(expr)
        if _is_bool(t):
            self._need_rt("to_str")
            text = f"to_str({self.emit_expr(expr, 0)})"
        elif t.name in ("int", "float", "str"):
            text = self.emit_expr(expr, 0)
        elif t.name in ("user", "member", "guild", "channel", "message",
                        "interaction", "role"):
            # An entity value renders as its string form (the f-string
            # calls str() on it); a bare entity Name was handled above
            # via its interp form.
            text = self.emit_expr(expr, 0)
        else:
            self._fail(f"cannot interpolate a value of type `{t}` "
                       "(interpolation takes int, float, str, bool, "
                       "or a Discord entity — use `str(x)` for the "
                       "rest)")
        return self._interp_field_or_hoist(text, quote)

    def _interp_field_or_hoist(self, text: str, quote: str) -> str:
        """P1: a backslash in a replacement field is a syntax error
        on Python 3.10–3.11, so a field whose text contains one is
        hoisted to a temp variable before the statement. (A field
        containing the f-string's delimiter quote is impossible: a
        string literal part already alternates quotes, and a nested
        f-string uses the other quote.)"""
        if "\\" in text:
            name = self._new_hoist_name()
            self._hoists.append([f"{name} = {text}"])
            return name
        return text

    # ------------------------------------------------------------ member / index

    def _method_receiver_mapping(self, recv_type: Type) -> dict:
        """Type-argument mapping for a method table lookup, mirroring
        the checker's `_receiver_mapping`."""
        if recv_type.name == "list" and recv_type.params:
            return {"T": recv_type.params[0]}
        if recv_type.name == "set" and recv_type.params:
            return {"T": recv_type.params[0]}
        if recv_type.name == "dict" and len(recv_type.params) == 2:
            return {"K": recv_type.params[0], "V": recv_type.params[1]}
        if recv_type.name == "Result" and len(recv_type.params) == 2:
            return {"T": recv_type.params[0], "E": recv_type.params[1]}
        return {}

    def _subst_method_spec(self, spec, mapping: dict):
        """Substitute a METHODS-table spec (a Type, or a "T"/"K"/"V"
        string) with the receiver's type arguments. Returns None
        for the unrepresentable specs ("any", "lenable", "...")."""
        if isinstance(spec, str):
            if spec in mapping:
                return mapping[spec]
            return None
        if isinstance(spec, Type):
            if spec.name in mapping and not spec.params:
                return mapping[spec.name]
            params = tuple(self._subst_method_spec(p, mapping)
                           for p in spec.params)
            if any(p is None for p in params):
                return None
            return Type(spec.name, params, nullable=spec.nullable)
        return None

    def _method_value_sig(self, recv_type: Type, name: str):
        """`(params, ret)` for a method of `recv_type`, or None.

        Mirrors the checker's METHODS tables with the receiver's
        type arguments substituted. A nullable method's "T" is the
        receiver's inner type; a method found only in the nullable
        table on a NON-nullable receiver is a rebased `?.` method
        inside an `opt_chain` lambda (D38) — the receiver is proven
        non-none there, so "T" is the receiver itself.
        """
        table = None
        mapping: dict = {}
        if recv_type.nullable and name in METHODS.get("nullable", {}):
            table = METHODS["nullable"]
            mapping = {"T": Type(recv_type.name,
                                 params=recv_type.params,
                                 nullable=False)}
        elif recv_type.name in METHODS \
                and name in METHODS[recv_type.name]:
            table = METHODS[recv_type.name]
            mapping = self._method_receiver_mapping(recv_type)
        elif name in METHODS.get("nullable", {}):
            table = METHODS["nullable"]
            mapping = {"T": recv_type}
        if table is None:
            return None
        sig = table[name]
        params = [self._subst_method_spec(p, mapping)
                  for p in sig.get("params", [])]
        ret = self._subst_method_spec(sig.get("return"), mapping)
        return params, ret

    def _is_plain_field(self, recv_type: Type, name: str) -> bool:
        """True when `name` is a real field of `recv_type` (a struct
        field, or an enum variant) — a plain attribute access, never
        a method value. Provider/entity types have no tables here,
        so they report False and the method-signature lookup decides.
        """
        decl = self.structs.get(recv_type.name)
        if decl is not None:
            return any(f.name == name for f in decl.fields)
        return False

    def _emit_method_value(self, node: A.Member, obj_type: Type,
                           sig) -> str:
        """A method used as a value (§16, hole 2): a lambda taking
        the method's parameters, capturing the receiver by value
        (D10) as a defaulted parameter — the receiver expression is
        evaluated once, when the lambda is created, never again at
        call time. The body is the ordinary method call with the
        receiver replaced by the snapshot name."""
        params, ret = sig
        obj_text = self.emit_expr(node.obj, _PREC["primary"])
        self._push_scope()
        recv = self._bind_fresh("_r", type=obj_type, kind="param")
        arg_bindings = []
        arg_nodes = []
        for i, ptype in enumerate(params):
            b = self._bind_fresh(f"_a{i}", type=ptype, kind="param")
            arg_bindings.append(b)
            arg_nodes.append(A.Name(span=node.span, name=f"_a{i}",
                                    expr_type=ptype))
        recv_name = A.Name(span=node.span, name="_r",
                           expr_type=obj_type)
        member = A.Member(span=node.span, obj=recv_name,
                          name=node.name, optional=False,
                          expr_type=self._type_of(node))
        call = A.Call(span=node.span, func=member, args=arg_nodes,
                      kwargs=[], expr_type=ret)
        sig_text = ", ".join(b.py for b in arg_bindings)
        if sig_text:
            sig_text += ", "
        sig_text += f"{recv.py}={obj_text}"
        body = self._callable_body(call, f"lambda {sig_text}: ")
        self._pop_scope()
        return body

    def _emit_member(self, node: A.Member) -> str:
        obj = node.obj
        if isinstance(obj, A.Name):
            binding = self._lookup(obj.name)
            if binding is not None and binding.kind in ("named-module",
                                                        "file-module"):
                return self._emit_module_member(node, binding)
            if binding is not None and binding.kind == "enum":
                return self._emit_enum_variant(obj.name, node)
            if binding is None and obj.name in MODULES:
                # A named module used without an import (N-09/N-10):
                # the name is both the real module and the reference.
                fake = _Binding(py=obj.name, kind="named-module",
                                module=obj.name)
                return self._emit_module_member(node, fake)
        obj_type = self._type_of(obj)
        t = self._type_of(node)
        # §16 (hole 2): a method used as a value is a lambda, not a
        # Python attribute — `int` has no `is_some`, `list` has no
        # `len`. A real struct field stays a plain attribute.
        if t.name == "fn" and not self._is_plain_field(obj_type,
                                                       node.name):
            sig = self._method_value_sig(obj_type, node.name)
            if sig is not None:
                return self._emit_method_value(node, obj_type, sig)
        obj_text = self.emit_expr(obj, _PREC["primary"])
        return f"{obj_text}.{node.name}"

    def _emit_module_member(self, node: A.Member,
                            binding: _Binding) -> str:
        """A named-module member used as a value: a constant, or a
        function (which becomes a lambda, D29). `binding.module` is
        the real module name (for dispatch); `binding.py` is the
        Python reference (the alias, when the import is aliased)."""
        real = binding.module or binding.py
        ref = binding.py
        t = self._type_of(node)
        if t.name == "fn":
            # A module function as a first-class value.
            if real == "file" and node.name in ("read", "write", "append"):
                self._fail(f"`file.{node.name}` cannot be used as a "
                           "value (it needs a statement — the `with` "
                           "block cannot live inside a lambda)")
            params = t.params[:-1]  # the last param is the return type
            names = [f"_a{i}" for i in range(len(params))]
            call = self._emit_module_call_text(real, ref, node.name, names)
            if names:
                return f"lambda {', '.join(names)}: {call}"
            return f"lambda: {call}"
        if real == "env" and node.name == "args":
            self._need_import("sys")
            return "sys.argv"
        if real == "math" and node.name in ("pi", "e"):
            self._need_import("math", alias=ref)
            return f"{ref}.{node.name}"
        self._fail(f"module `{real}` has no value member `{node.name}`")

    def _emit_enum_variant(self, enum_name: str, node: A.Member) -> str:
        """`Color.Red` — a bare variant value is an instance of its
        variant dataclass; a variant with fields, used as a value,
        is the dataclass itself (a constructor)."""
        t = self._type_of(node)
        py_enum = self._mangle(enum_name)
        py_variant = f"{py_enum}_{self._mangle(node.name)}"
        if t.name == "fn":
            return py_variant
        return f"{py_variant}()"

    def _emit_index(self, node: A.Index) -> str:
        obj_type = self._type_of(node.obj)
        obj = self.emit_expr(node.obj, _PREC["primary"])
        idx = self.emit_expr(node.index, 0)
        if obj_type.name == "list":
            # Negative is out of range (G-M2-5): never Python's wrap.
            self._need_rt("list_index")
            return f"list_index({obj}, {idx})"
        if obj_type.name == "dict":
            return f"{obj}[{idx}]"
        self._fail(f"cannot index a value of type `{obj_type}`")

    # ------------------------------------------------------------ calls

    def _emit_call_args(self, node: A.Call) -> str:
        parts = [self.emit_expr(a, 0) for a in node.args]
        for name, value, _span in node.kwargs:
            parts.append(f"{self._mangle(name)}={self.emit_expr(value, 0)}")
        return ", ".join(parts)

    def _emit_call(self, node: A.Call) -> str:
        func = node.func
        if isinstance(func, A.Name):
            return self._emit_name_call(func.name, node)
        if isinstance(func, A.Member):
            if func.optional:
                # The chain collector handles every optional call;
                # reaching here is an emitter bug.
                self._fail("internal: an optional call reached the "
                           "plain call emitter")
            obj = func.obj
            if isinstance(obj, A.Name):
                binding = self._lookup(obj.name)
                if binding is not None and binding.kind in (
                        "named-module", "file-module"):
                    return self._emit_module_call(func.name, node, binding)
                if binding is None and obj.name in MODULES:
                    # A named module used without an import (N-09/N-10).
                    fake = _Binding(py=obj.name, kind="named-module",
                                    module=obj.name)
                    return self._emit_module_call(func.name, node, fake)
                if binding is not None and binding.kind == "store":
                    return self._emit_store_call(obj.name, func.name, node)
                if obj.name == "log" and func.name in ("info", "warn",
                                                       "error"):
                    return self._emit_log_call(func.name, node)
                if binding is not None and binding.kind == "enum":
                    return self._emit_enum_call(obj.name, func.name, node)
            return self._emit_method_call(func, node)
        # A call on an arbitrary expression (a lambda, a call result).
        f = self.emit_expr(func, _PREC["primary"])
        return f"{f}({self._emit_call_args(node)})"

    def _emit_name_call(self, name: str, node: A.Call) -> str:
        if node.kwargs and name not in self.structs:
            self._fail(f"`{name}` takes no named arguments")
        if name == "print":
            return self._emit_print(node)
        if name == "input":
            if self._is_bot:
                self.warnings.append(
                    "input() in a bot program blocks on stdin — bots "
                    "have no stdin (D21)")
            args = [self.emit_expr(a, 0) for a in node.args]
            return f"input({', '.join(args)})"
        if name == "len":
            return f"len({self.emit_expr(node.args[0], 0)})"
        if name in ("str", "int", "float", "bool"):
            return self._emit_conversion(name, node.args[0])
        if name in ("min", "max"):
            args = [self.emit_expr(a, 0) for a in node.args]
            return f"{name}({', '.join(args)})"
        if name == "abs":
            t = self._type_of(node.args[0])
            arg = self.emit_expr(node.args[0], 0)
            if _is_int(t):
                self._need_rt("check_int")
                return f"check_int(abs({arg}))"
            return f"abs({arg})"
        if name == "round":
            self._need_rt("round_half_away")
            args = [self.emit_expr(a, 0) for a in node.args]
            return f"round_half_away({', '.join(args)})"
        if name == "range":
            args = [self.emit_expr(a, 0) for a in node.args]
            return f"list(range({', '.join(args)}))"
        if name == "set":
            return f"set({self.emit_expr(node.args[0], 0)})"
        if name == "ok":
            self._need_rt("ok")
            return f"ok({self.emit_expr(node.args[0], 0)})"
        if name == "err":
            self._need_rt("err")
            return f"err({self.emit_expr(node.args[0], 0)})"
        binding = self._lookup(name)
        if binding is not None and binding.kind == "struct":
            parts = [self.emit_expr(a, 0) for a in node.args]
            for kname, value, _span in node.kwargs:
                parts.append(f"{self._mangle(kname)}="
                             f"{self.emit_expr(value, 0)}")
            return f"{binding.py}({', '.join(parts)})"
        if binding is not None:
            return f"{binding.py}({self._emit_call_args(node)})"
        self._fail(f"unknown function `{name}`")

    def _emit_print(self, node: A.Call) -> str:
        """`print` renders every argument with `to_str`, except
        statically str/int/float values, which are bare (P8). A `?`
        argument propagates first (the temp + early return are
        hoisted before the statement, D7)."""
        parts = []
        for arg in node.args:
            if isinstance(arg, A.Propagate):
                _lines, unwrapped = self._propagate_early_return(arg)
                parts.append(unwrapped)
                continue
            t = self._type_of(arg)
            text = self.emit_expr(arg, 0)
            # A nullable value must go through to_str even when its
            # inner type is int/float/str — its runtime value is
            # Python's None when absent, and print would render that
            # as "None" instead of newr's "none" (P8).
            if _is_bool(t) or t.nullable or \
                    t.name not in ("str", "int", "float"):
                self._need_rt("to_str")
                text = f"to_str({text})"
            parts.append(text)
        return f"print({', '.join(parts)})"

    def _emit_conversion(self, name: str, arg: A.Expr) -> str:
        t = self._type_of(arg)
        text = self.emit_expr(arg, 0)
        if name == "str":
            # A nullable str's runtime value is None when absent —
            # bare `str(None)` would print "None", not newr's "none"
            # (P8), so nullable values go through to_str.
            if _is_str(t) and not t.nullable:
                return text
            self._need_rt("to_str")
            return f"to_str({text})"
        if name == "int":
            if _is_int(t):
                return text
            if _is_float(t) or _is_str(t):
                self._need_rt("to_int")
                return f"to_int({text})"
            return f"int({text})"  # bool -> 0/1
        if name == "float":
            if _is_float(t):
                return text
            if _is_str(t):
                self._need_rt("to_float")
                return f"to_float({text})"
            return f"float({text})"  # int/bool
        # bool
        if _is_bool(t):
            return text
        if _is_str(t):
            self._need_rt("to_bool")
            return f"to_bool({text})"
        return f"bool({text})"  # int -> 0 is False (P7)

    def _emit_log_call(self, level: str, node: A.Call) -> str:
        """`log.info/warn/error(msg)` (P10). `warn` is `warning` —
        never the deprecated `logging.warn`. In a bot program the
        message goes to the bot's named logger (`log.warning(...)`);
        in a general program it goes to the root logger."""
        py_level = "warning" if level == "warn" else level
        msg = self.emit_expr(node.args[0], 0)
        if self._is_bot:
            return f"log.{py_level}({msg})"
        self._need_import("logging")
        return f"logging.{py_level}({msg})"

    # ------------------------------------------------------------ module calls

    def _emit_module_call_text(self, real: str, ref: str, name: str,
                               args: list[str]) -> str:
        """The Python call text for a named-module function (not a
        file module, not a file op — those are handled by their
        callers). `real` is the module name (for dispatch); `ref` is
        the Python reference (the alias, when the import is aliased)."""
        alias = ref if ref != real else None
        if real == "math":
            self._need_import("math", alias=alias)
            if name == "sqrt":
                return f"{ref}.sqrt({args[0]})"
            if name == "floor":
                self._need_rt("check_int")
                return f"check_int({ref}.floor({args[0]}))"
            if name == "ceil":
                self._need_rt("check_int")
                return f"check_int({ref}.ceil({args[0]}))"
        if real == "time":
            if name == "now":
                self._need_import("time", alias=alias)
                return f"int({ref}.time())"
            if name == "format":
                self._need_rt("time_format")
                return f"time_format({args[0]}, {args[1]})"
        if real == "random":
            self._need_import("random", alias=alias)
            if name == "int":
                return f"{ref}.randint({args[0]}, {args[1]})"
            if name == "choice":
                return f"{ref}.choice({args[0]})"
            if name == "shuffle":
                return f"{ref}.shuffle({args[0]})"
        if real == "env":
            self._need_import("os")
            if name == "get":
                return f"os.getenv({', '.join(args)})"
        self._fail(f"module `{real}` has no function `{name}`")

    def _emit_module_call(self, name: str, node: A.Call,
                          binding: _Binding) -> str:
        if binding.kind == "file-module":
            # A function in an imported .newr file: a plain call on
            # the imported module — and the import itself (D13: one
            # `.py` per imported `.newr`, imported by its module name).
            self._need_import(binding.py)
            return (f"{binding.py}.{self._mangle(name)}"
                    f"({self._emit_call_args(node)})")
        real = binding.module or binding.py
        if real == "file":
            if name in ("read", "write", "append"):
                self._need_rt(f"file_{name}")
                args = [self.emit_expr(a, 0) for a in node.args]
                return f"file_{name}({', '.join(args)})"
            if name == "exists":
                self._need_import("os")
                return f"os.path.exists({self.emit_expr(node.args[0], 0)})"
            self._fail(f"module `file` has no function `{name}`")
        args = [self.emit_expr(a, 0) for a in node.args]
        return self._emit_module_call_text(real, binding.py, name, args)

    def _emit_enum_call(self, enum_name: str, variant: str,
                        node: A.Call) -> str:
        """`Shape.Circle(radius: 1.5)` -> the variant dataclass."""
        py_variant = f"{self._mangle(enum_name)}_{self._mangle(variant)}"
        return f"{py_variant}({self._emit_call_args(node)})"

    # ------------------------------------------------------------ methods

    def _emit_method_call(self, func: A.Member, node: A.Call) -> str:
        obj = func.obj
        obj_type = self._type_of(obj)
        name = func.name
        if node.kwargs:
            self._fail(f"method `.{name}` takes no named arguments")
        args = [self.emit_expr(a, 0) for a in node.args]
        obj_text = self.emit_expr(obj, _PREC["primary"])
        if obj_type.nullable:
            # Nullable methods only (is_some / is_none / unwrap). A
            # `?.` call on a nullable was handled by the chain
            # collector, so func.optional is False here.
            if name == "is_some":
                return f"({obj_text} is not None)"
            if name == "is_none":
                return f"({obj_text} is None)"
            if name == "unwrap":
                self._need_rt("unwrap_opt")
                return f"unwrap_opt({obj_text})"
            self._fail(f"`{obj_type}` has no method `{name}`")
        tname = obj_type.name
        if tname == "str":
            if name in ("upper", "lower", "strip"):
                return f"{obj_text}.{name}()"
            if name == "split":
                return f"{obj_text}.split({args[0]})"
            if name == "join":
                return f"{obj_text}.join({args[0]})"
            if name == "contains":
                return f"({args[0]} in {obj_text})"
            if name == "starts_with":
                return f"{obj_text}.startswith({args[0]})"
            if name == "ends_with":
                return f"{obj_text}.endswith({args[0]})"
            if name == "chars":
                return f"list({obj_text})"
        elif tname == "list":
            if name == "append":
                return f"{obj_text}.append({args[0]})"
            if name == "contains":
                return f"({args[0]} in {obj_text})"
            if name == "get":
                self._need_rt("list_get")
                return f"list_get({obj_text}, {args[0]}, {args[1]})"
            if name == "len":
                return f"len({obj_text})"
        elif tname == "dict":
            if name == "keys":
                return f"list({obj_text}.keys())"
            if name == "get":
                return f"{obj_text}.get({args[0]}, {args[1]})"
            if name == "contains":
                return f"({args[0]} in {obj_text})"
        elif tname == "set":
            if name == "add":
                return f"{obj_text}.add({args[0]})"
            if name == "remove":
                return f"{obj_text}.remove({args[0]})"
            if name == "contains":
                return f"({args[0]} in {obj_text})"
        elif tname == "Result":
            if name in ("is_ok", "is_err", "unwrap"):
                return f"{obj_text}.{name}()"
            if name == "unwrap_or":
                return f"{obj_text}.unwrap_or({args[0]})"
        # Rebased `?.` chains (D38): inside an `opt_chain` lambda the
        # receiver is the STRIPPED inner type, so a nullable method
        # (`is_some` / `is_none` / `unwrap`) reaches here on a
        # non-nullable, non-Result receiver. The value is proven
        # non-none inside the lambda, so the answer is constant
        # (True / False / the value itself). The checker rejects
        # these calls on a plain value, so they can only come from
        # a rebased chain.
        if name == "is_some":
            return "True"
        if name == "is_none":
            return "False"
        if name == "unwrap":
            return obj_text
        self._fail(f"`{obj_type}` has no method `{name}`")

    # ------------------------------------------------------------ optional chains

    def _link_is_optional(self, link) -> bool:
        if isinstance(link, A.Member):
            return link.optional
        if isinstance(link, A.Call) and isinstance(link.func, A.Member):
            return link.func.optional
        return False  # Index: `?[]` is not in v1 (SPEC §9.2)

    def _link_receiver(self, link):
        if isinstance(link, (A.Member, A.Index)):
            return link.obj
        return link.func.obj  # a Call whose func is a Member

    def _chain_has_optional(self, node) -> bool:
        cur = node
        while isinstance(cur, (A.Member, A.Index)) or (
                isinstance(cur, A.Call)
                and isinstance(cur.func, A.Member)):
            if self._link_is_optional(cur):
                return True
            cur = self._link_receiver(cur)
        return False

    def _rebase(self, node, old_base, new_base, in_lambda_types=None):
        """Clone a Member/Index/Call chain, replacing the subtree
        `old_base` (by identity) with `new_base`, and clearing the
        `optional` flags (the optionality is handled by the
        `opt_chain` wrapper, not inside the lambda body). Types are
        copied onto the clones — the checker already typed the
        original nodes — except when `in_lambda_types` maps a link
        to its in-lambda type (§16, hole 1): the checker's chain
        type conflates the `?.` short-circuit with a field's own
        nullability, and inside the lambda only the latter survives,
        so the map's type is the exact one for dispatch."""
        if node is old_base:
            return new_base
        if isinstance(node, A.Member):
            expr_type = node.expr_type
            if in_lambda_types is not None \
                    and in_lambda_types.get(id(node)) is not None:
                expr_type = in_lambda_types[id(node)]
            return A.Member(
                span=node.span,
                obj=self._rebase(node.obj, old_base, new_base,
                                 in_lambda_types),
                name=node.name, optional=False,
                expr_type=expr_type)
        if isinstance(node, A.Index):
            expr_type = node.expr_type
            if in_lambda_types is not None \
                    and in_lambda_types.get(id(node)) is not None:
                expr_type = in_lambda_types[id(node)]
            return A.Index(
                span=node.span,
                obj=self._rebase(node.obj, old_base, new_base,
                                 in_lambda_types),
                index=node.index, expr_type=expr_type)
        if isinstance(node, A.Call) and isinstance(node.func, A.Member):
            func = A.Member(
                span=node.func.span,
                obj=self._rebase(node.func.obj, old_base, new_base,
                                 in_lambda_types),
                name=node.func.name, optional=False,
                expr_type=node.func.expr_type)
            # D38: the result of an OPTIONAL call link (`s?.unwrap()`)
            # is nullable only because the `?.` short-circuits — inside
            # the lambda the receiver is proven non-none, so the
            # result is the plain (stripped) type. Without this, a
            # later link (`.upper()` in `s?.unwrap().upper()`)
            # dispatches against the nullable type and fails with
            # E4001. Only Call links are stripped: a Member link
            # (`a?.b`) may be nullable because the FIELD itself is
            # nullable, which survives inside the lambda. When the
            # caller supplies the exact in-lambda type (§16), that
            # wins over this heuristic — it is exact where D38
            # over-strips (a method whose own return type is
            # nullable, e.g. `unwrap()` on `Result[str?, _]`).
            expr_type = node.expr_type
            if in_lambda_types is not None \
                    and in_lambda_types.get(id(node)) is not None:
                expr_type = in_lambda_types[id(node)]
            elif node.func.optional and expr_type is not None \
                    and expr_type.nullable:
                expr_type = Type(expr_type.name,
                                 params=expr_type.params, nullable=False)
            return A.Call(span=node.span, func=func, args=node.args,
                          kwargs=node.kwargs, expr_type=expr_type)
        return node

    def _in_lambda_link_type(self, link, recv_type: Type):
        """In-lambda result type of a chain link (§16, hole 1).

        Inside an `opt_chain` lambda the receiver is proven
        non-none, so this is the link's result type with the `?.`
        short-circuit removed — but a nullable FIELD type or a
        nullable method return survives. Returns None when the type
        cannot be determined (provider/entity types); the caller
        treats None as nullable and splits the segment, which is
        always safe.
        """
        if isinstance(link, A.Member):
            decl = self.structs.get(recv_type.name)
            if decl is not None:
                for f in decl.fields:
                    if f.name == link.name:
                        return f.type
                return None  # not a field; structs have no methods
            sig = self._method_value_sig(recv_type, link.name)
            if sig is not None:
                params, ret = sig
                return fn_of(params, ret)
            return None
        if isinstance(link, A.Call) and isinstance(link.func, A.Member):
            sig = self._method_value_sig(recv_type, link.func.name)
            if sig is None:
                return None
            _params, ret = sig
            return ret
        if isinstance(link, A.Index):
            if recv_type.name == "list" and recv_type.params:
                return recv_type.params[0]
            if recv_type.name == "dict" and len(recv_type.params) == 2:
                return recv_type.params[1]
            return None
        return None

    def _emit_chain(self, node) -> str:
        """`?.` lowering (D9, corrected): one `opt_chain` call per
        optional link, nested; non-optional links after an optional
        one fold into the nearest lambda body. `a?.b.c` is one call;
        `a?.b?.c` is two. The lambda body is the original sub-chain
        rebased onto the lambda parameter, so method dispatch,
        indexing and conversions inside it are exactly the normal
        ones."""
        links: list = []
        cur = node
        while isinstance(cur, (A.Member, A.Index)) or (
                isinstance(cur, A.Call)
                and isinstance(cur.func, A.Member)):
            links.append(cur)
            cur = self._link_receiver(cur)
        base = cur
        links.reverse()  # base outward
        first_opt = next(i for i, link in enumerate(links)
                         if self._link_is_optional(link))
        if first_opt == 0:
            cur_text = self.emit_expr(base, _PREC["primary"])
        else:
            # Leading non-optional links: emit the node for the last
            # of them normally (its receiver is the base / the
            # previous link's node).
            cur_text = self.emit_expr(links[first_opt - 1],
                                      _PREC["primary"])
        i = first_opt
        while i < len(links):
            inner_recv = self._link_receiver(links[i])
            recv_type = self._type_of(inner_recv)
            # Inside the lambda the receiver is proven non-none
            # (opt_chain only calls it then), so the parameter and
            # the rebased base carry the STRIPPED (inner) type —
            # otherwise a method call on it would dispatch as if the
            # value were still nullable.
            inner_type = Type(recv_type.name, params=recv_type.params,
                              nullable=False)
            # §16 (hole 1): a segment must also end before any link
            # whose receiver can be `none` INSIDE the lambda — a
            # nullable field (`o?.inner.name`) or a nullable method
            # result (`r?.unwrap().upper()` on `Result[str?, _]?`).
            # The checker's `expr_type` conflates the `?.`
            # short-circuit with a field's own nullability, so the
            # in-lambda types are walked link by link instead.
            # `in_lambda` maps each link in this segment to its
            # in-lambda result type, so `_rebase` types the clones
            # exactly: a Member link's nullability inside the lambda
            # is its FIELD's nullability, not the checker's chain
            # type — without this, a method after a nullable field
            # dispatches against `T?` and fails with E4001.
            in_lambda: dict[int, Type | None] = {}
            cur_type = self._in_lambda_link_type(links[i], inner_type)
            in_lambda[id(links[i])] = cur_type
            j = i + 1
            while j < len(links):
                if self._link_is_optional(links[j]):
                    break
                if cur_type is None or cur_type.nullable:
                    break
                cur_type = self._in_lambda_link_type(links[j], cur_type)
                in_lambda[id(links[j])] = cur_type
                j += 1
            seg_end = links[j - 1]
            self._push_scope()
            binding = self._bind_fresh("_v", type=inner_type, kind="param")
            # The rebased base is a Name for the NEWR name `_v` — the
            # lookup returns the binding's Python name (which may be
            # `_v_1` in a nested chain).
            new_base = A.Name(span=links[i].span, name="_v",
                              expr_type=inner_type)
            body_node = self._rebase(seg_end, inner_recv, new_base,
                                     in_lambda)
            self._need_rt("opt_chain")
            body = self._callable_body(body_node, f"lambda {binding.py}: ")
            self._pop_scope()
            cur_text = f"opt_chain({cur_text}, {body})"
            i = j
        return cur_text

    def _callable_body(self, expr: A.Expr, prefix: str) -> str:
        """Emit `expr` as a lambda body with the given prefix
        (`lambda ...: `). If the body needs hoists (D33 — an f-string
        part with a backslash, a nested block-body lambda), the
        lambda becomes a hoisted `def` and its name is returned
        instead, so the hoisted statements run at call time, inside
        the function, not at the enclosing statement."""
        saved = self._hoists
        self._hoists = []
        body = self.emit_expr(expr, 0)
        hoists = self._hoists
        self._hoists = saved
        if not hoists:
            return prefix + body
        name = self._new_lambda_name()
        sig = prefix[len("lambda "):-2]  # the parameter list
        lines = [f"def {name}({sig}):"]
        for h in hoists:
            lines.extend("    " + line for line in h)
        lines.append(f"    return {body}")
        self._hoists.append(lines)
        return name

    # ------------------------------------------------------------ lambdas

    def _emit_lambda(self, node: A.Lambda) -> tuple[str, int]:
        """Closures capture by value (D10): free variables become
        parameters with defaults bound at creation. A block body
        cannot be a Python `lambda`, so it is hoisted to a `def`
        immediately before the enclosing statement."""
        free = self._lambda_free_vars(node)
        self._push_scope()
        saved_used = self._fn_used
        self._fn_used = set()
        param_bindings = [self._bind_fresh(p.name, type=p.type,
                                           kind="param")
                          for p in node.params]
        # Snapshot parameters: one per free variable, named after the
        # enclosing binding's Python name (so the default `a=a` reads
        # the enclosing value), renamed only on a collision with a
        # declared parameter or another snapshot parameter.
        used_names = {b.py for b in param_bindings}
        snap: list[tuple[str, str, str]] = []  # (newr name, param, rhs)
        for name, b in free:
            # The snapshot parameter is named after the enclosing
            # binding's Python name (so the default `a=a` reads the
            # enclosing value) — unless that is not a valid
            # identifier (a handler binding's value form is an
            # expression, e.g. `interaction.user`), in which case
            # the mangled newr name is the parameter and the value
            # form is the default.
            py = b.py if b.py.isidentifier() else self._mangle(name)
            if py in used_names:
                n = 1
                while f"{py}_{n}" in used_names:
                    n += 1
                py = f"{py}_{n}"
            used_names.add(py)
            self._fn_used.add(py)
            self._scopes[-1][name] = _Binding(py=py, type=b.type,
                                              kind="param")
            snap.append((name, py, b.py))
        sig_parts = [b.py for b in param_bindings]
        sig_parts += [f"{py}={rhs}" for _name, py, rhs in snap]
        sig = ", ".join(sig_parts)
        if node.expr_body is not None:
            body = self._callable_body(node.expr_body, f"lambda {sig}: ")
            self._fn_used = saved_used
            self._pop_scope()
            return body, 0
        # Block body: hoist a def.
        name = self._new_lambda_name()
        self._fn_ctx.append({"return_type": node.return_type,
                             "ensures": [], "in_handler": False})
        saved_globals = self._fn_globals
        self._fn_globals = set()
        body_lines = self._emit_block(node.body or [], 1)
        if self._fn_globals:
            names = ", ".join(sorted(self._fn_globals))
            body_lines.insert(0, f"global {names}")
        self._fn_globals = saved_globals
        self._fn_ctx.pop()
        self._fn_used = saved_used
        self._pop_scope()
        lines = [f"def {name}({sig}):"]
        lines.extend(body_lines if body_lines else ["    pass"])
        self._hoists.append(lines)
        return name, _PREC["primary"]

    def _lambda_free_vars(self, node: A.Lambda) -> list[tuple[str, _Binding]]:
        """Names the lambda reads from enclosing function scopes
        (D10). The walker collects every referenced name not bound
        inside the lambda; the filter keeps only those that resolve
        to a function-local binding in the current scope stack
        (module-level names are globals, read at call time; builtins
        need no capture)."""
        bound = {p.name for p in node.params}
        refs: set[str] = set()
        if node.expr_body is not None:
            self._collect_refs(node.expr_body, bound, refs)
        elif node.body is not None:
            for st in node.body:
                self._collect_stmt_refs(st, bound, refs)
        free: list[tuple[str, _Binding]] = []
        for name in sorted(refs):
            b = self._lookup(name)
            # "global-let" is included: a top-level `let` is mutable,
            # so a lambda reading it at call time would see a LATER
            # value — capture by value (D10) requires a snapshot.
            # (A top-level `const` is registered the same way; it
            # never changes, so snapshotting it is harmless.) Module
            # functions / structs / enums / modules / stores are
            # fixed, so they need no capture; builtins need none.
            if b is not None and b.kind in ("local", "param", "handler",
                                            "global-let"):
                free.append((name, b))
        return free

    def _collect_refs(self, expr, bound: set, refs: set):
        if expr is None:
            return
        if isinstance(expr, A.Name):
            if expr.name not in bound:
                refs.add(expr.name)
            return
        if isinstance(expr, A.Lit):
            return
        if isinstance(expr, A.BinOp):
            self._collect_refs(expr.left, bound, refs)
            self._collect_refs(expr.right, bound, refs)
            return
        if isinstance(expr, A.UnaryOp):
            self._collect_refs(expr.operand, bound, refs)
            return
        if isinstance(expr, A.Ternary):
            self._collect_refs(expr.cond, bound, refs)
            self._collect_refs(expr.then_expr, bound, refs)
            self._collect_refs(expr.else_expr, bound, refs)
            return
        if isinstance(expr, A.Lambda):
            inner = set(bound) | {p.name for p in expr.params}
            if expr.expr_body is not None:
                self._collect_refs(expr.expr_body, inner, refs)
            elif expr.body is not None:
                for st in expr.body:
                    self._collect_stmt_refs(st, inner, refs)
            return
        if isinstance(expr, A.TryExpr):
            self._collect_refs(expr.expr, bound, refs)
            return
        if isinstance(expr, A.Propagate):
            self._collect_refs(expr.expr, bound, refs)
            return
        if isinstance(expr, A.Call):
            self._collect_refs(expr.func, bound, refs)
            for a in expr.args:
                self._collect_refs(a, bound, refs)
            for _n, v, _s in expr.kwargs:
                self._collect_refs(v, bound, refs)
            return
        if isinstance(expr, A.TCall):
            for _n, v, _s in expr.args:
                self._collect_refs(v, bound, refs)
            return
        if isinstance(expr, A.Member):
            self._collect_refs(expr.obj, bound, refs)
            return
        if isinstance(expr, A.Index):
            self._collect_refs(expr.obj, bound, refs)
            self._collect_refs(expr.index, bound, refs)
            return
        if isinstance(expr, A.Interp):
            for part in expr.parts:
                if part.expr is not None:
                    self._collect_refs(part.expr, bound, refs)
            return
        if isinstance(expr, A.ListLit):
            for e in expr.elements:
                self._collect_refs(e, bound, refs)
            return
        if isinstance(expr, A.DictLit):
            for k, v in expr.pairs:
                self._collect_refs(k, bound, refs)
                self._collect_refs(v, bound, refs)
            return
        if isinstance(expr, A.EmbedExpr):
            for f in expr.fields:
                self._collect_refs(f.value, bound, refs)
            return

    def _collect_stmt_refs(self, stmt, bound: set, refs: set):
        if isinstance(stmt, (A.Let, A.Const)):
            self._collect_refs(stmt.value, bound, refs)
            bound.add(stmt.name)
            return
        if isinstance(stmt, A.Assign):
            if stmt.name not in bound:
                refs.add(stmt.name)
            self._collect_refs(stmt.value, bound, refs)
            return
        if isinstance(stmt, A.If):
            self._collect_refs(stmt.cond, bound, refs)
            for st in stmt.then_body:
                self._collect_stmt_refs(st, set(bound), refs)
            for cond, body in stmt.elifs:
                self._collect_refs(cond, bound, refs)
                for st in body:
                    self._collect_stmt_refs(st, set(bound), refs)
            if stmt.else_body is not None:
                for st in stmt.else_body:
                    self._collect_stmt_refs(st, set(bound), refs)
            return
        if isinstance(stmt, A.For):
            self._collect_refs(stmt.iterable, bound, refs)
            inner = set(bound) | {stmt.var}
            for st in stmt.body:
                self._collect_stmt_refs(st, inner, refs)
            return
        if isinstance(stmt, A.While):
            self._collect_refs(stmt.cond, bound, refs)
            for st in stmt.body:
                self._collect_stmt_refs(st, set(bound), refs)
            return
        if isinstance(stmt, A.Match):
            self._collect_refs(stmt.scrutinee, bound, refs)
            for arm in stmt.arms:
                inner = set(bound) | self._pattern_names(arm.pattern)
                if arm.guard is not None:
                    self._collect_refs(arm.guard, inner, refs)
                for st in arm.body:
                    self._collect_stmt_refs(st, inner, refs)
            return
        if isinstance(stmt, (A.Require, A.Ensure)):
            self._collect_refs(stmt.cond, bound, refs)
            return
        if isinstance(stmt, A.Try):
            for st in stmt.body:
                self._collect_stmt_refs(st, set(bound), refs)
            for cname, _span, cbody in stmt.catches:
                inner = set(bound) | {cname}
                for st in cbody:
                    self._collect_stmt_refs(st, inner, refs)
            if stmt.finally_body is not None:
                for st in stmt.finally_body:
                    self._collect_stmt_refs(st, set(bound), refs)
            return
        if isinstance(stmt, (A.Throw, A.Return, A.ExprStmt, A.Reply,
                             A.Edit, A.Followup, A.Log)):
            self._collect_refs(stmt.value, bound, refs)
            return
        if isinstance(stmt, A.Send):
            self._collect_refs(stmt.value, bound, refs)
            self._collect_refs(stmt.channel, bound, refs)
            return
        # Button / Select / Modal / Defer / Break / Continue / Pass:
        # no references a lambda can capture (component bodies are
        # separate scopes the checker keeps out of lambdas).
        return

    def _pattern_names(self, pattern) -> set:
        if pattern is None:
            return set()
        if isinstance(pattern, A.NamePattern):
            return {pattern.name}
        if isinstance(pattern, A.EnumPattern):
            names = set()
            for _f, sub, _s in pattern.bindings:
                names |= self._pattern_names(sub)
            return names
        if isinstance(pattern, A.StructPattern):
            names = set()
            for _f, sub, _s in pattern.fields:
                names |= self._pattern_names(sub)
            return names
        if isinstance(pattern, A.ListPattern):
            names = set()
            for sub in pattern.elements:
                names |= self._pattern_names(sub)
            return names
        if isinstance(pattern, A.OrPattern):
            names = set()
            for sub in pattern.patterns:
                names |= self._pattern_names(sub)
            return names
        return set()

    # ------------------------------------------------------------ statements

    def _emit_block(self, stmts: list, indent: int,
                    new_scope: bool = True) -> list[str]:
        """Emit a list of statements at `indent`. Returns lines with
        the indent prefix applied. Each statement gets its own hoist
        list (temps and hoisted defs go before it, at its indent).

        A component attaches only to an answer in the SAME block
        (D40): the last answer is tracked per block (saved/restored
        around nested blocks), and a Button/Select statement rewrites
        that answer's line to carry the view."""
        if new_scope:
            self._push_scope()
        saved_answer = self._last_answer
        self._last_answer = None
        lines: list[str] = []
        try:
            for st in stmts:
                before = len(lines)
                lines.extend(self._emit_stmt_with_hoists(st, indent))
                if isinstance(st, (A.Reply, A.Followup, A.Edit)):
                    kind = ("reply" if isinstance(st, A.Reply)
                            else "followup" if isinstance(st, A.Followup)
                            else "edit")
                    self._last_answer = (len(lines) - 1, kind)
                elif isinstance(st, (A.ButtonDecl, A.SelectDecl)):
                    # The component's instantiation lines must come
                    # BEFORE the answer they attach to — the answer
                    # references the view variable, so defining it
                    # after the answer would be a NameError.
                    if self._last_answer is not None:
                        idx, kind = self._last_answer
                        added = lines[before:]
                        del lines[before:]
                        lines[idx:idx] = added
                        self._last_answer = (idx + len(added), kind)
                    self._attach_view(lines)
        finally:
            if new_scope:
                self._pop_scope()
            self._last_answer = saved_answer
        return lines

    def _attach_view(self, lines: list[str]):
        """Attach the pending view (set by _emit_button/_emit_select)
        to the block's last answer line (D40): the answer gains
        `view=<var>`. When the component has an `on_timeout`, the
        view also stores its message so `on_timeout` can edit it."""
        if self._pending_view is None:
            return
        view = self._pending_view
        has_timeout = self._pending_view_timeout
        self._pending_view = None
        self._pending_view_timeout = False
        if self._last_answer is None:
            self._fail("a `button` / `select` must follow the reply "
                       "it attaches to (there is no message to put "
                       "the view on)")
        idx, kind = self._last_answer
        line = lines[idx].rstrip()
        if not line.endswith(")"):
            self._fail("internal: the answer line does not end with `)`")
        lines[idx] = line[:-1] + f", view={view})"
        if not has_timeout:
            return
        p = line[:len(line) - len(line.lstrip())]
        if kind == "followup":
            # The followup's message is the send's return (wait=True).
            lines[idx] = (lines[idx].replace(
                "await interaction.followup.send(",
                f"{view}.message = await interaction.followup.send(",
                1).rstrip()[:-1] + ", wait=True)")
        else:
            # A reply / edit answers the original response — the
            # message line goes right after the answer, not at the
            # end of the block (statements may follow the component).
            lines.insert(idx + 1, f"{p}{view}.message = await "
                         "interaction.original_response()")

    def _emit_stmt_with_hoists(self, stmt, indent: int) -> list[str]:
        saved = self._hoists
        self._hoists = []
        lines = self._emit_stmt(stmt, indent)
        hoists = self._hoists
        self._hoists = saved
        prefix = "    " * indent
        out: list[str] = []
        for h in hoists:
            out.extend(prefix + line for line in h)
        out.extend(lines)
        return out

    def _propagate_unwrapped(self, prop: A.Propagate) -> str:
        """D7: `?` at the end of a statement RHS. Emits the temp
        assignment + the early return as hoisted lines before the
        statement, and returns the text of the unwrapped value.

        A `Result` operand returns the `Err` itself from a
        Result-returning function; a nullable operand returns `None`
        from a nullable-returning function. The mismatched kinds
        (D30) and a nullable `Result` (G-M2-6) are clean
        `EmitterError`s — the checker should have rejected them, and
        there is no defined early-return value to invent."""
        operand = prop.expr
        t = self._type_of(operand)
        ctx = self._fn_ctx[-1] if self._fn_ctx else None
        if ctx is None:
            self._fail("`?` outside a function (the checker should "
                       "have rejected it)")
        ret = ctx["return_type"]
        temp = self._new_temp_name()
        text = self.emit_expr(operand, 0)
        if ctx.get("in_handler"):
            # A handler returns nothing, so `?` just exits the
            # handler (a bare `return`) — there is no value to
            # propagate. (A handler that may exit early without
            # answering is the discord checker's E2010, not the
            # emitter's problem.)
            if t.name == "Result" and t.params and not t.nullable:
                self._hoists.append([
                    f"{temp} = {text}",
                    f"if {temp}.is_err():",
                    "    return",
                ])
                return f"{temp}.unwrap()"
            if t.nullable and t.name != "Result":
                self._hoists.append([
                    f"{temp} = {text}",
                    f"if {temp} is None:",
                    "    return",
                ])
                return temp
            if t.name == "Result" and t.nullable:
                self._fail("`?` on a nullable `Result` has no defined "
                           "meaning (G-M2-6) — unwrap it first, then "
                           "propagate the `Result`")
            self._fail("`?` needs a `Result[T, E]` or a `T?` value")
        if t.name == "Result" and t.params:
            if t.nullable:
                self._fail("`?` on a nullable `Result` has no defined "
                           "meaning (G-M2-6) — unwrap it first, then "
                           "propagate the `Result`")
            if ret is None or ret.name != "Result":
                self._fail("`?` on a `Result` value inside a function "
                           "that does not return a `Result` (D30 — the "
                           "checker should have rejected this)")
            self._hoists.append([
                f"{temp} = {text}",
                f"if {temp}.is_err():",
                f"    return {temp}",
            ])
            return f"{temp}.unwrap()"
        if t.nullable:
            if ret is None or not ret.nullable:
                self._fail("`?` on a nullable value inside a function "
                           "that does not return a nullable (D30 — the "
                           "checker should have rejected this)")
            self._hoists.append([
                f"{temp} = {text}",
                f"if {temp} is None:",
                "    return None",
            ])
            return temp
        self._fail("`?` needs a `Result[T, E]` or a `T?` value")

    def _emit_stmt(self, stmt, indent: int) -> list[str]:
        p = "    " * indent
        if isinstance(stmt, (A.Let, A.Const)):
            is_top = indent == 0 and not self._fn_ctx
            if isinstance(stmt.value, A.Propagate):
                value = self._propagate_unwrapped(stmt.value)
            else:
                value = self.emit_expr(stmt.value, 0)
            if is_top:
                binding = self._lookup(stmt.name)
                if binding is None:
                    self._fail(f"internal: top-level `{stmt.name}` was "
                               "not registered")
            else:
                t = stmt.type_ann
                if t is None:
                    t = self._type_of(stmt.value)
                binding = self._bind(stmt.name, type=t, kind="local")
            return [f"{p}{binding.py} = {value}"]
        if isinstance(stmt, A.Assign):
            return self._emit_assign(stmt, indent)
        if isinstance(stmt, A.If):
            return self._emit_if(stmt, indent)
        if isinstance(stmt, A.For):
            return self._emit_for(stmt, indent)
        if isinstance(stmt, A.While):
            cond = self.emit_expr(stmt.cond, 0)
            lines = [f"{p}while {cond}:"]
            lines.extend(self._emit_block(stmt.body, indent + 1))
            return lines
        if isinstance(stmt, A.Match):
            return self._emit_match(stmt, indent)
        if isinstance(stmt, (A.Require, A.Ensure)):
            self._fail("`requires` / `ensures` may only appear at the "
                       "start of a function body (the function emitter "
                       "handles them there)")
        if isinstance(stmt, A.Try):
            return self._emit_try(stmt, indent)
        if isinstance(stmt, A.Throw):
            return self._emit_throw(stmt, indent)
        if isinstance(stmt, A.Return):
            return self._emit_return(stmt, indent)
        if isinstance(stmt, A.Break):
            return [f"{p}break"]
        if isinstance(stmt, A.Continue):
            return [f"{p}continue"]
        if isinstance(stmt, A.Pass):
            return [f"{p}pass"]
        if isinstance(stmt, A.Reply):
            return self._emit_reply(stmt, indent)
        if isinstance(stmt, A.Defer):
            return self._emit_defer(stmt, indent)
        if isinstance(stmt, A.Edit):
            return self._emit_edit(stmt, indent)
        if isinstance(stmt, A.Followup):
            return self._emit_followup(stmt, indent)
        if isinstance(stmt, A.Send):
            return self._emit_send(stmt, indent)
        if isinstance(stmt, A.Log):
            return self._emit_log_stmt(stmt, indent)
        if isinstance(stmt, A.ExprStmt):
            if isinstance(stmt.value, A.Propagate):
                self._propagate_unwrapped(stmt.value)
                return []
            text = self.emit_expr(stmt.value, 0)
            return [f"{p}{text}"] if text else []
        if isinstance(stmt, A.ButtonDecl):
            return self._emit_button(stmt, indent)
        if isinstance(stmt, A.SelectDecl):
            return self._emit_select(stmt, indent)
        if isinstance(stmt, A.ModalDecl):
            return self._emit_modal(stmt, indent)
        self._fail(f"cannot emit statement {type(stmt).__name__}")

    def _emit_assign(self, stmt: A.Assign, indent: int) -> list[str]:
        p = "    " * indent
        binding = self._lookup(stmt.name)
        if binding is None:
            self._fail(f"assignment to unknown name `{stmt.name}`")
        if isinstance(stmt.value, A.Propagate):
            value = self._propagate_unwrapped(stmt.value)
        else:
            value = self.emit_expr(stmt.value, 0)
        # An assignment to a module-level `let` from inside a
        # function needs a `global` declaration (inserted at the top
        # of the function by the function emitter).
        if binding.kind == "global-let" and self._fn_ctx:
            self._fn_globals.add(binding.py)
        if stmt.op == "=":
            return [f"{p}{binding.py} = {value}"]
        t = binding.type
        if _is_int(t):
            helper = {"+=": "add", "-=": "sub", "*=": "mul",
                      "/=": "tdiv"}[stmt.op]
            self._need_rt(helper)
            return [f"{p}{binding.py} = {helper}({binding.py}, {value})"]
        # float / str: bare augmented assignment (no overflow check
        # applies; `+=` on a str is concatenation).
        return [f"{p}{binding.py} {stmt.op} {value}"]

    def _emit_if(self, stmt: A.If, indent: int) -> list[str]:
        p = "    " * indent
        lines = [f"{p}if {self.emit_expr(stmt.cond, 0)}:"]
        lines.extend(self._emit_block(stmt.then_body, indent + 1))
        for cond, body in stmt.elifs:
            lines.append(f"{p}elif {self.emit_expr(cond, 0)}:")
            lines.extend(self._emit_block(body, indent + 1))
        if stmt.else_body is not None:
            lines.append(f"{p}else:")
            lines.extend(self._emit_block(stmt.else_body, indent + 1))
        return lines

    def _emit_for(self, stmt: A.For, indent: int) -> list[str]:
        p = "    " * indent
        it_type = self._type_of(stmt.iterable)
        if it_type.name == "list" and it_type.params:
            elem = it_type.params[0]
        elif it_type.name == "set" and it_type.params:
            elem = it_type.params[0]
        elif it_type.name == "dict":
            elem = Type("str")
        elif it_type.name == "str":
            elem = Type("str")
        else:
            self._fail(f"cannot iterate over a `{it_type}` value")
        # The iterable is emitted BEFORE the loop variable is bound
        # (it cannot see the loop variable — `for x in x` reads the
        # enclosing `x`).
        iterable = self.emit_expr(stmt.iterable, 0)
        self._push_scope()
        binding = self._bind(stmt.var, type=elem, kind="local")
        lines = [f"{p}for {binding.py} in {iterable}:"]
        lines.extend(self._emit_block(stmt.body, indent + 1,
                                      new_scope=False))
        self._pop_scope()
        return lines

    def _emit_try(self, stmt: A.Try, indent: int) -> list[str]:
        p = "    " * indent
        lines = [f"{p}try:"]
        lines.extend(self._emit_block(stmt.body, indent + 1))
        if stmt.catches:
            if len(stmt.catches) > 1:
                self._fail("`try` with more than one `catch` is not "
                           "supported in v1 (the first would catch "
                           "everything)")
            cname, _span, cbody = stmt.catches[0]
            exc_var = self._new_temp_name()
            lines.append(f"{p}except Exception as {exc_var}:")
            self._push_scope()
            binding = self._bind(cname, type=Type("error"), kind="local")
            self._need_rt("as_error")
            lines.append(f"{p}    {binding.py} = as_error({exc_var})")
            lines.extend(self._emit_block(cbody, indent + 1,
                                          new_scope=False))
            self._pop_scope()
        if stmt.finally_body is not None:
            lines.append(f"{p}finally:")
            lines.extend(self._emit_block(stmt.finally_body, indent + 1))
        return lines

    def _emit_throw(self, stmt: A.Throw, indent: int) -> list[str]:
        p = "    " * indent
        t = self._type_of(stmt.value)
        text = self.emit_expr(stmt.value, 0)
        if t.name == "error":
            return [f"{p}raise {text}"]
        if _is_str(t):
            self._need_rt_class()
            return [f"{p}raise newr_rt.NewrError({text})"]
        self._fail(f"`throw` needs a `str` or an `error`, not `{t}`")

    def _emit_return(self, stmt: A.Return, indent: int) -> list[str]:
        p = "    " * indent
        ctx = self._fn_ctx[-1] if self._fn_ctx else None
        if ctx is None:
            # A bare top-level `return` exits the program (D11).
            if stmt.value is None:
                self._need_import("sys")
                return [f"{p}sys.exit(0)"]
            self._fail("a top-level `return` with a value is not "
                       "supported — a top-level `return` exits the "
                       "program")
        ensures = ctx["ensures"]
        if stmt.value is None:
            if not ensures:
                return [f"{p}return"]
            lines = [f"{p}_newr_result = None"]
            lines.extend(self._ensures_check_lines(ensures, indent))
            lines.append(f"{p}return _newr_result")
            return lines
        if isinstance(stmt.value, A.Propagate):
            # A propagated error returns early; the `ensures` apply
            # only to normal (Ok) returns, not to propagated errors.
            unwrapped = self._propagate_unwrapped(stmt.value)
            return [f"{p}return {unwrapped}"]
        value = self.emit_expr(stmt.value, 0)
        if not ensures:
            return [f"{p}return {value}"]
        lines = [f"{p}_newr_result = {value}"]
        lines.extend(self._ensures_check_lines(ensures, indent))
        lines.append(f"{p}return _newr_result")
        return lines

    def _ensures_check_lines(self, ensures: list, indent: int) -> list[str]:
        """`if not <cond>: raise NewrError("ensures failed: <cond>")`
        for each `ensures` clause (D5: explicit `raise`, never
        `assert`). `result` in the condition reads `_newr_result`
        (bound in the function scope when the function has
        `ensures`)."""
        p = "    " * indent
        lines: list[str] = []
        for clause in ensures:
            cond = self.emit_expr(clause.cond, 0)
            msg = cond.replace("_newr_result", "result")
            lines.append(f"{p}if not {cond}:")
            lines.append(
                f"{p}    raise newr_rt.NewrError("
                f"{_py_str('ensures failed: ' + msg)})")
        return lines

    # ------------------------------------------------------------ match (D12)

    def _emit_match(self, stmt: A.Match, indent: int) -> list[str]:
        p = "    " * indent
        temp = self._new_match_name()
        scrut = self.emit_expr(stmt.scrutinee, 0)
        lines = [f"{p}{temp} = {scrut}"]
        arms = []
        scrut_type = self._type_of(stmt.scrutinee)
        for arm in stmt.arms:
            cond, bindings = self._pattern_condition(arm.pattern, temp,
                                                     scrut_type)
            if arm.guard is not None:
                # The guard may reference the pattern's bindings
                # (e.g. `x if x > 100:`) — bind each name to its
                # pattern expression text while emitting it.
                self._push_scope()
                for name, expr_text, typ in bindings:
                    self._scopes[-1][name] = _Binding(
                        py=expr_text, type=typ, kind="local")
                guard = self.emit_expr(arm.guard, 0)
                self._pop_scope()
                cond = f"{cond} and {guard}" if cond else guard
            arms.append((cond, bindings, arm))
        # The last unconditional arm becomes `else:`; otherwise the
        # final `else` raises (defense — the checker proves
        # exhaustiveness, so it is unreachable in a checked program).
        last_uncond = (arms and arms[-1][0] is None)
        for i, (cond, bindings, arm) in enumerate(arms):
            if last_uncond and i == len(arms) - 1:
                lines.append(f"{p}else:")
            elif i == 0:
                lines.append(f"{p}if {cond}:")
            else:
                lines.append(f"{p}elif {cond}:")
            self._push_scope()
            for name, expr_text, typ in bindings:
                b = self._bind(name, type=typ, kind="local")
                lines.append(f"{p}    {b.py} = {expr_text}")
            lines.extend(self._emit_block(arm.body, indent + 1,
                                          new_scope=False))
            self._pop_scope()
        if not last_uncond:
            lines.append(f"{p}else:")
            self._need_rt_class()
            lines.append(
                f"{p}    raise newr_rt.NewrError(\"non-exhaustive match\")")
        return lines

    def _pattern_condition(self, pattern, expr_text: str,
                           typ: Type | None):
        """Compile a pattern against the Python expression `expr_text`
        (usually the match temp) and the newr type `typ` of the value
        being matched. Returns (condition_text_or_None, bindings)
        where bindings is a list of (newr name, Python expression
        text, type). A `None` condition means the arm always matches
        (wildcard / name binding)."""
        if isinstance(pattern, A.WildcardPattern):
            return None, []
        if isinstance(pattern, A.NamePattern):
            return None, [(pattern.name, expr_text, typ)]
        if isinstance(pattern, A.LitPattern):
            v = pattern.value
            if v is None:
                return f"{expr_text} is None", []
            if isinstance(v, bool):
                return f"{expr_text} is {v}", []
            return f"{expr_text} == {_py_str(v) if isinstance(v, str) else repr(v)}", []
        if isinstance(pattern, A.EnumPattern):
            py_variant = (f"{self._mangle(pattern.enum_name)}_"
                          f"{self._mangle(pattern.variant)}")
            cond = f"isinstance({expr_text}, {py_variant})"
            bindings: list = []
            enum = self.enums.get(pattern.enum_name)
            fields = {}
            if enum is not None:
                for var in enum.variants:
                    if var.name == pattern.variant:
                        fields = {f.name: f.type for f in var.fields}
            for fname, sub, _span in pattern.bindings:
                sub_cond, sub_bind = self._pattern_condition(
                    sub, f"{expr_text}.{self._mangle(fname)}",
                    fields.get(fname))
                if sub_cond:
                    cond += f" and {sub_cond}"
                bindings.extend(sub_bind)
            return cond, bindings
        if isinstance(pattern, A.StructPattern):
            py_struct = self._mangle(pattern.struct_name)
            cond = f"isinstance({expr_text}, {py_struct})"
            bindings = []
            struct = self.structs.get(pattern.struct_name)
            fields = {}
            if struct is not None:
                fields = {f.name: f.type for f in struct.fields}
            for fname, sub, _span in pattern.fields:
                sub_cond, sub_bind = self._pattern_condition(
                    sub, f"{expr_text}.{self._mangle(fname)}",
                    fields.get(fname))
                if sub_cond:
                    cond += f" and {sub_cond}"
                bindings.extend(sub_bind)
            return cond, bindings
        if isinstance(pattern, A.ListPattern):
            cond = (f"isinstance({expr_text}, list) and "
                    f"len({expr_text}) == {len(pattern.elements)}")
            bindings = []
            elem = typ.params[0] if typ is not None and typ.params else None
            for i, sub in enumerate(pattern.elements):
                sub_cond, sub_bind = self._pattern_condition(
                    sub, f"{expr_text}[{i}]", elem)
                if sub_cond:
                    cond += f" and {sub_cond}"
                bindings.extend(sub_bind)
            return cond, bindings
        if isinstance(pattern, A.OrPattern):
            conds = []
            all_bindings: list = []
            for sub in pattern.patterns:
                sub_cond, sub_bind = self._pattern_condition(sub, expr_text,
                                                             typ)
                conds.append(sub_cond if sub_cond else "True")
                all_bindings.append(sub_bind)
            cond = "(" + " or ".join(conds) + ")"
            # A name bound by several branches is the value from the
            # branch that matched (a nested ternary over the branch
            # conditions); the checker requires every branch to bind
            # the same names.
            merged: dict[str, tuple[str, object]] = {}
            for sub_cond, sub_bind in zip(conds, all_bindings):
                for name, expr, typ in sub_bind:
                    if name not in merged:
                        merged[name] = (expr, typ)
                        merged[name + "\x00conds"] = [(sub_cond, expr)]
                    else:
                        merged[name + "\x00conds"].append((sub_cond, expr))
            bindings = []
            for name, (expr, typ) in list(merged.items()):
                if "\x00" in name:
                    continue
                branches = merged[name + "\x00conds"]
                if len(branches) == 1:
                    bindings.append((name, expr, typ))
                else:
                    text = branches[-1][1]
                    for bcond, bexpr in reversed(branches[:-1]):
                        text = f"{bexpr} if {bcond} else {text}"
                    bindings.append((name, text, typ))
            return cond, bindings
        self._fail(f"cannot emit pattern {type(pattern).__name__}")

    # ------------------------------------------------------------ functions

    def _emit_fn(self, decl: A.FnDecl, indent: int = 0) -> list[str]:
        p = "    " * indent
        binding = self._lookup(decl.name)
        py_name = binding.py if binding is not None else self._mangle(decl.name)
        self._push_scope()
        saved_used = self._fn_used
        self._fn_used = set()
        params = [self._bind_fresh(prm.name, type=prm.type, kind="param")
                  for prm in decl.params]
        sig = ", ".join(b.py for b in params)
        requires: list = []
        ensures: list = []
        i = 0
        body = decl.body
        while i < len(body) and isinstance(body[i], (A.Require, A.Ensure)):
            if isinstance(body[i], A.Require):
                requires.append(body[i])
            else:
                ensures.append(body[i])
            i += 1
        rest = body[i:]
        if ensures:
            # `result` in an `ensures` clause reads `_newr_result`.
            self._scopes[-1]["result"] = _Binding(py="_newr_result",
                                                  type=decl.return_type,
                                                  kind="local")
            self._fn_used.add("_newr_result")
        self._fn_ctx.append({"return_type": decl.return_type,
                             "ensures": ensures, "in_handler": False})
        saved_globals = self._fn_globals
        self._fn_globals = set()
        if requires or ensures:
            # The contract checks below raise newr_rt.NewrError.
            self._need_rt_class()
        lines = [f"{p}def {py_name}({sig}):"]
        bp = "    " * (indent + 1)
        body_lines: list[str] = []
        for req in requires:
            cond = self.emit_expr(req.cond, 0)
            body_lines.append(f"{bp}if not {cond}:")
            body_lines.append(
                f"{bp}    raise newr_rt.NewrError("
                f"{_py_str('requires failed: ' + cond)})")
        body_lines.extend(self._emit_block(rest, indent + 1,
                                           new_scope=False))
        if ensures and not (rest and isinstance(rest[-1], A.Return)):
            # The implicit return (falling off the end) is also a
            # return — check the `ensures` against `None`.
            body_lines.append(f"{bp}_newr_result = None")
            body_lines.extend(self._ensures_check_lines(ensures, indent + 1))
            body_lines.append(f"{bp}return _newr_result")
        if self._fn_globals:
            names = ", ".join(sorted(self._fn_globals))
            body_lines.insert(0, f"{bp}global {names}")
        if not body_lines:
            body_lines = [f"{bp}pass"]
        lines.extend(body_lines)
        self._fn_globals = saved_globals
        self._fn_ctx.pop()
        self._fn_used = saved_used
        self._pop_scope()
        return lines

    # ------------------------------------------------------------ types / decls

    def _py_type(self, t: Type) -> str:
        """A newr type as a Python annotation. Struct/enum field
        annotations are emitted QUOTED (see _emit_struct), so the
        names are never evaluated — forward references and cycles
        are safe."""
        if t is None:
            return "None"
        base: str
        if t.name == "int":
            base = "int"
        elif t.name == "float":
            base = "float"
        elif t.name == "str":
            base = "str"
        elif t.name == "bool":
            base = "bool"
        elif t.name == "none":
            base = "None"
        elif t.name == "error":
            base = "newr_rt.NewrError"
        elif t.name == "duration":
            base = "int"  # seconds; durations only occur in task/timeout
        elif t.name == "list":
            base = f"list[{self._py_type(t.params[0])}]"
        elif t.name == "dict":
            base = (f"dict[{self._py_type(t.params[0])}, "
                    f"{self._py_type(t.params[1])}]")
        elif t.name == "set":
            base = f"set[{self._py_type(t.params[0])}]"
        elif t.name == "Result":
            base = "newr_rt.Result"
        elif t.name == "fn":
            params = ", ".join(self._py_type(p) for p in t.params[:-1])
            ret = self._py_type(t.params[-1]) if t.params else "None"
            self._need_from("collections.abc", "Callable")
            base = f"Callable[[{params}], {ret}]"
        else:
            base = self._mangle(t.name)
        if t.nullable:
            return f"{base} | None"
        return base

    def _emit_struct(self, decl: A.StructDecl, indent: int = 0) -> list[str]:
        p = "    " * indent
        py_name = self._mangle(decl.name)
        self._need_import("dataclasses")
        self._need_rt("to_str")
        lines = [f"{p}@dataclasses.dataclass(frozen=True)",
                 f"{p}class {py_name}:"]
        if not decl.fields:
            lines.append(f"{p}    pass")
        for f in decl.fields:
            # Quoted annotations: never evaluated, so a field typed
            # as a later struct (or a cycle) is safe.
            lines.append(f"{p}    {self._mangle(f.name)}: "
                         f"{_py_str(self._py_type(f.type))}")
        lines.append("")
        lines.append(f"{p}    def _newr_repr(self) -> str:")
        if decl.fields:
            parts = ", ".join(
                f"{f.name}: {{to_str(self.{self._mangle(f.name)})}}"
                for f in decl.fields)
            lines.append(f"{p}        return f\"{py_name}({parts})\"")
        else:
            lines.append(f"{p}        return \"{py_name}()\"")
        return lines

    def _emit_enum(self, decl: A.EnumDecl, indent: int = 0) -> list[str]:
        p = "    " * indent
        py_enum = self._mangle(decl.name)
        self._need_import("dataclasses")
        self._need_rt("to_str")
        lines: list[str] = []
        for variant in decl.variants:
            py_variant = f"{py_enum}_{self._mangle(variant.name)}"
            self._module_used.add(py_variant)
            lines.append(f"{p}@dataclasses.dataclass(frozen=True)")
            lines.append(f"{p}class {py_variant}:")
            if not variant.fields:
                lines.append(f"{p}    pass")
            for f in variant.fields:
                lines.append(f"{p}    {self._mangle(f.name)}: "
                             f"{_py_str(self._py_type(f.type))}")
            lines.append("")
            lines.append(f"{p}    def _newr_repr(self) -> str:")
            if variant.fields:
                parts = ", ".join(
                    f"{f.name}: {{to_str(self.{self._mangle(f.name)})}}"
                    for f in variant.fields)
                lines.append(
                    f"{p}        return f\"{py_enum}.{variant.name}({parts})\"")
            else:
                lines.append(f"{p}        return \"{py_enum}.{variant.name}\"")
            lines.append("")
        # The enum type itself is the union of its variants.
        members = [f"{py_enum}_{self._mangle(v.name)}"
                   for v in decl.variants]
        lines.append(f"{p}{py_enum} = {' | '.join(members)}")
        return lines

    def _emit_log_stmt(self, stmt: A.Log, indent: int) -> list[str]:
        """`log <expr>` is info level (P10). In a bot program it is
        the bot's named logger (`log.info(...)`); in a general
        program it is the root logger (`logging.info(...)`, with a
        `basicConfig` emitted once in the header)."""
        p = "    " * indent
        if isinstance(stmt.value, A.Propagate):
            value = self._propagate_unwrapped(stmt.value)
        else:
            value = self.emit_expr(stmt.value, 0)
        if self._is_bot:
            return [f"{p}log.info({value})"]
        self._used_log = True
        self._need_import("logging")
        return [f"{p}logging.info({value})"]

    # ------------------------------------------------------------ bot emitter

    # Handler bindings: name -> (value form, interpolation form,
    # newr type name). The value form is what a bare name means; the
    # interpolation form is what `{name}` renders as (the mention /
    # name form, per the v0 golden).
    _BOT_BINDINGS: dict[str, dict[str, tuple[str, str | None, str]]] = {
        "command": {
            "user": ("interaction.user",
                     "interaction.user.mention", "user"),
            "guild": ("interaction.guild",
                      "interaction.guild.name if interaction.guild else 'DM'",
                      "guild"),
            "channel": ("interaction.channel",
                        "_channel_text(interaction.channel)", "channel"),
            "interaction": ("interaction", None, "interaction"),
        },
        "member_join": {
            "user": ("member", "member.mention", "member"),
            "guild": ("member.guild", "member.guild.name", "guild"),
        },
        "member_remove": {
            "user": ("member", "member.mention", "member"),
            "guild": ("member.guild", "member.guild.name", "guild"),
        },
        "ready": {
            "bot": ("self.user", "self.user", "user"),
        },
        "message": {
            "message": ("message", "message.content", "message"),
            "user": ("message.author", "message.author.mention", "user"),
        },
        "component": {  # a button/select/modal callback has its own
            "user": ("interaction.user",
                     "interaction.user.mention", "user"),
            "guild": ("interaction.guild",
                      "interaction.guild.name if interaction.guild else 'DM'",
                      "guild"),
            "channel": ("interaction.channel",
                        "_channel_text(interaction.channel)", "channel"),
            "interaction": ("interaction", None, "interaction"),
        },
    }

    def _handler_ctx(self) -> dict:
        if not self._handler_ctx_stack:
            self._fail("a bot statement was emitted outside a handler")
        return self._handler_ctx_stack[-1]

    def _push_handler(self, kind: str, names: list[str],
                      reply: str | None, locale: str | None):
        """Push a handler scope: the named bindings (looked up in
        _BOT_BINDINGS[kind] by name) and a context dict describing
        how `reply` is answered and which locale `t()` uses."""
        ctx = {"kind": kind, "reply": reply, "locale": locale}
        self._handler_ctx_stack.append(ctx)
        # A handler is a function returning nothing, so `?` inside
        # one exits the handler with a bare `return` (see
        # _propagate_unwrapped).
        self._fn_ctx.append({"return_type": None, "ensures": [],
                             "in_handler": True})
        self._push_scope()
        table = self._BOT_BINDINGS.get(kind, {})
        for name in names:
            value, interp, tname = table[name]
            self._scopes[-1][name] = _Binding(
                py=value, type=Type(tname), kind="handler", interp=interp)
        return ctx

    def _pop_handler(self):
        self._pop_scope()
        self._fn_ctx.pop()
        self._handler_ctx_stack.pop()

    def _handler_binding(self, name: str, value: str, interp: str | None,
                         tname: str):
        """Bind one extra handler name (an option, a select's
        `values`, a modal field) in the current handler scope."""
        self._scopes[-1][name] = _Binding(
            py=value, type=Type(tname), kind="handler", interp=interp)

    # -- the bot file -------------------------------------------------

    def _bot_intents(self, program: A.Program) -> set[str]:
        """Intents: declared ones, plus members iff a member event
        is present, plus message_content iff a message event is
        present (the v0 rule)."""
        intents = set(program.bot.intents or [])
        kinds = {ev.kind for ev in program.events}
        if "member_join" in kinds or "member_remove" in kinds:
            intents.add("members")
        if "message" in kinds:
            intents.add("message_content")
        return intents

    def _emit_bot_file(self, program: A.Program,
                       main_path: str) -> tuple[str, bool]:
        """A bot program -> one discord.py file, following the v0
        golden template (examples/hello.py): header, imports, logging
        setup, STRINGS + _t, the Bot class (intents, setup_hook,
        event methods, store methods), `bot = Bot()`, module-level
        command functions, task loops, the @bot.tree.error handler,
        _channel_text, main()."""
        self._need_import("logging")
        self._need_import("os")
        self._need_import("discord")
        self._need_from("discord", "app_commands")
        self._need_from("discord.ext", "commands")
        if program.stores:
            self._need_import("aiosqlite")
        if program.tasks:
            self._need_from("discord.ext", "tasks")
        bot = program.bot
        bot_name = bot.name
        self.program = program
        self.fns = {f.name: f for f in program.functions}
        self.structs = {s.name: s for s in program.structs}
        self.enums = {e.name: e for e in program.enums}
        self.stores = {s.name: s for s in program.stores}
        # The module scope (imports, structs, enums, fns, lets,
        # stores) must be registered before any handler body is
        # emitted — a handler calling a file-module function
        # (`shop_items.price_of`) or a store (`economy.balance`)
        # resolves through it, exactly as in a general program.
        self._register_module_scope(program)
        # The header (D2: the actual input file as passed).
        lines = self._header(main_path, bot.description)
        # The body is emitted first (imports are usage-tracked).
        body: list[str] = []
        # §19: the file's top-level declarations are part of a bot
        # file (SPEC §8, §8.2, §13.7) — structs/enums/functions
        # first (hoisted, as in a general file), then the top-level
        # statements in file order. They run at module import time,
        # before `bot = Bot()` and main(); handlers read them at
        # call time. Before this fix the bot file dropped them
        # entirely, so a handler calling a file-module fn died with
        # NameError even though the checker accepted the program.
        for decl in program.structs:
            body.extend(self._emit_struct(decl))
            body.append("")
        for decl in program.enums:
            body.extend(self._emit_enum(decl))
            body.append("")
        for decl in program.functions:
            body.extend(self._emit_fn(decl))
            body.append("")
        for st in program.stmts:
            body.extend(self._emit_stmt_with_hoists(st, 0))
        # Two blank lines before the class when declarations were
        # emitted (the no-declaration case stays byte-identical).
        if body:
            while body and body[-1] == "":
                body.pop()
            body.append("")
            body.append("")
        body.append("class Bot(commands.Bot):")
        body.extend(self._emit_bot_class(program))
        body.append("")
        body.append("")
        body.append("bot = Bot()")
        # Component classes (collected while emitting the handlers)
        # go before the command functions.
        for cmd in program.commands:
            body.extend(self._emit_command(cmd))
        for task in program.tasks:
            body.extend(self._emit_task(task))
        if program.commands:
            body.append("")
            body.append("")
            body.extend(self._emit_error_handler())
        body.append("")
        body.append("")
        body.extend(self._emit_channel_text())
        body.append("")
        body.append("")
        body.extend(self._emit_main())
        # The STRINGS dict + _t helper go before the Bot class.
        strings_lines = self._emit_strings(program)
        # Assemble: header, imports, logging setup, strings, body.
        out = list(lines)
        imports = self._render_imports()
        if imports:
            out.append("")
            out.extend(imports)
        out.append("")
        out.append("logging.basicConfig(")
        out.append("    level=logging.INFO,")
        out.append('    format="%(asctime)s %(levelname)s %(name)s: %(message)s",')
        out.append(")")
        out.append(f'log = logging.getLogger("{bot_name.lower()}")')
        if strings_lines:
            out.append("")
            out.extend(strings_lines)
        if self._bot_components:
            out.append("")
            for comp in self._bot_components:
                out.extend(comp)
                out.append("")
                out.append("")
        out.append("")
        out.append("")
        out.extend(body)
        text = "\n".join(out).rstrip("\n") + "\n"
        return text, bool(self.rt_helpers or self.rt_module_used)

    def _emit_bot_class(self, program: A.Program) -> list[str]:
        """The Bot class body (indented one level): __init__,
        setup_hook, event methods, store methods."""
        lines: list[str] = []
        bot = program.bot
        lines.append("    def __init__(self) -> None:")
        lines.append("        intents = discord.Intents.default()")
        for intent in sorted(self._bot_intents(program)):
            lines.append(f"        intents.{intent} = True")
        lines.append('        super().__init__(command_prefix="!", '
                     "intents=intents)")
        for store in program.stores:
            lines.append(f"        self.{store.name}_db = None")
            lines.append(f"        self.{store.name}_lock = None")
        lines.append("")
        lines.append("    async def setup_hook(self) -> None:")
        for store in program.stores:
            lines.append(f"        await self._{store.name}_connect()")
        for task in program.tasks:
            lines.append(f"        {self._task_fn_name(task)}.start()")
        lines.append("        await self.tree.sync()")
        lines.append('        log.info("Commands synced globally.")')
        # Event methods.
        for ev in program.events:
            lines.extend(self._emit_event_method(ev))
        # Store methods.
        for store in program.stores:
            lines.extend(self._emit_store_methods(store))
        return lines

    def _emit_event_method(self, ev: A.EventDecl) -> list[str]:
        """An event -> a Bot method (on_ready / on_member_join /
        on_member_remove / on_message)."""
        lines: list[str] = []
        body = ev.effective_body()
        if ev.kind == "ready":
            lines.append("")
            lines.append("    async def on_ready(self) -> None:")
            self._push_handler("ready", ["bot"],
                               reply=None, locale=None)
            try:
                lines.extend(self._emit_block(body, 2, new_scope=False))
            finally:
                self._pop_handler()
            return lines
        if ev.kind in ("member_join", "member_remove"):
            meth = ("on_member_join" if ev.kind == "member_join"
                    else "on_member_remove")
            lines.append("")
            lines.append(f"    async def {meth}(self, member: "
                         "discord.Member) -> None:")
            self._push_handler(ev.kind,
                               ["user", "guild"],
                               reply="system_channel",
                               locale="member.guild.preferred_locale")
            try:
                # A reply in a member event goes to the guild's
                # system channel (the v0 golden), guarded against
                # None.
                inner = self._emit_block(body, 3, new_scope=False)
                lines.append("        channel = member.guild.system_channel")
                lines.append("        if channel is not None:")
                lines.extend(inner)
            finally:
                self._pop_handler()
            return lines
        if ev.kind == "message":
            lines.append("")
            lines.append("    async def on_message(self, message: "
                         "discord.Message) -> None:")
            lines.append("        if message.author == self.user:")
            lines.append("            return")
            self._push_handler("message",
                               ["message", "user"],
                               reply="message_channel",
                               locale="message.guild.preferred_locale "
                                      "if message.guild else None")
            try:
                lines.extend(self._emit_block(body, 2, new_scope=False))
            finally:
                self._pop_handler()
            return lines
        self._fail(f"unknown event kind `{ev.kind}`")

    def _emit_command(self, cmd: A.CommandDecl) -> list[str]:
        """A command -> a module-level `async def cmd_<name>` with
        @bot.tree.command (+ @app_commands.describe / .choices)."""
        lines: list[str] = []
        lines.append("")
        lines.append("")
        desc = cmd.description if cmd.description is not None else cmd.name
        lines.append(f'@bot.tree.command(name="{cmd.name}", '
                     f'description="{desc}")')
        # describe (only for options with a description, or any
        # option — the v0 shop golden describes every option).
        if cmd.options:
            parts = []
            for opt in cmd.options:
                d = opt.description if opt.description is not None \
                    else opt.name
                parts.append(f'{opt.name}="{d}"')
            lines.append("@app_commands.describe("
                         + ", ".join(parts) + ")")
        # choices (D15: a decorator per option with choices).
        for opt in cmd.options:
            if opt.choices:
                choices = ", ".join(
                    "app_commands.Choice(name="
                    f"{self._emit_expr_text(c)}, value="
                    f"{self._emit_expr_text(c)})"
                    for c in opt.choices)
                lines.append(f"@app_commands.choices({opt.name}=[{choices}])")
        # The signature.
        params = ["interaction: discord.Interaction"]
        for opt in cmd.options:
            params.append(self._option_param(opt))
        sig = ", ".join(params)
        fn_name = "cmd_" + cmd.name.replace("-", "_")
        lines.append(f"async def {fn_name}({sig}) -> None:")
        self._push_handler("command",
                           ["user", "guild", "channel", "interaction"],
                           reply="interaction",
                           locale="interaction.locale")
        try:
            for opt in cmd.options:
                self._handler_binding(
                    opt.name, opt.name, None,
                    self._option_type_name(opt))
            lines.extend(self._emit_block(cmd.effective_body(), 1,
                                          new_scope=False))
        finally:
            self._pop_handler()
        return lines

    def _option_type_name(self, opt: A.OptionDecl) -> str:
        t = opt.type
        if t is None:
            return "str"
        return t.name

    def _option_param(self, opt: A.OptionDecl) -> str:
        """One command-option parameter: the discord.py annotation
        (Range for min/max — D15, the generic-subscript form) and
        the default (None for an optional option without one)."""
        t = opt.type
        if t is None:
            ann = "str"
        elif t.name == "str":
            ann = "str"
        elif t.name == "int":
            ann = "int"
        elif t.name == "num" or t.name == "float":
            ann = "float"
        elif t.name == "bool":
            ann = "bool"
        elif t.name == "user" or t.name == "member":
            ann = "discord.Member"
        elif t.name == "channel":
            ann = "discord.abc.GuildChannel"
        elif t.name == "role":
            ann = "discord.Role"
        else:
            self._fail(f"option type `{t}` is not a command option type")
        # min/max -> Range (D15: app_commands.Range[int, min, max]).
        if opt.min_value is not None or opt.max_value is not None:
            mn = self._range_bound(opt.min_value)
            mx = self._range_bound(opt.max_value)
            ann = f"app_commands.Range[{ann}, {mn}, {mx}]"
        if opt.required:
            return f"{opt.name}: {ann}"
        if opt.default is not None:
            default = self.emit_expr(opt.default, 0)
            return f"{opt.name}: {ann} = {default}"
        # Optional with no default: None (the type is nullable).
        return f"{opt.name}: {ann} | None = None"

    def _range_bound(self, expr) -> str:
        if expr is None:
            return "None"
        return self.emit_expr(expr, 0)

    def _emit_expr_text(self, expr) -> str:
        """An expression as text (for a choice value / default)."""
        return self.emit_expr(expr, 0)

    def _task_fn_name(self, task: A.TaskDecl) -> str:
        """A task's loop-function name, from its interval (a task
        has no name in newr). Two tasks with the same interval get a
        numeric suffix. The name is computed once per task and
        cached — setup_hook and the definition must agree."""
        if not hasattr(self, "_task_names_by_id"):
            self._task_names_by_id: dict[int, str] = {}
            self._task_name_counts: dict[str, int] = {}
        key = id(task)
        if key in self._task_names_by_id:
            return self._task_names_by_id[key]
        secs = task.interval_seconds
        if secs % 3600 == 0:
            base = f"_every_{secs // 3600}h"
        elif secs % 60 == 0:
            base = f"_every_{secs // 60}m"
        else:
            base = f"_every_{secs}s"
        n = self._task_name_counts.get(base, 0)
        self._task_name_counts[base] = n + 1
        name = base if n == 0 else f"{base}_{n + 1}"
        self._task_names_by_id[key] = name
        return name

    def _emit_task(self, task: A.TaskDecl) -> list[str]:
        """A task -> a @tasks.loop function + a before_loop that
        waits for the bot to be ready. The loop's interval is the
        largest whole unit (hours > minutes > seconds)."""
        secs = task.interval_seconds
        if secs % 3600 == 0:
            interval = f"hours={secs // 3600}.0"
        elif secs % 60 == 0:
            interval = f"minutes={secs // 60}.0"
        else:
            interval = f"seconds={secs}.0"
        name = self._task_fn_name(task)
        lines: list[str] = []
        lines.append("")
        lines.append("")
        lines.append(f"@tasks.loop({interval})")
        lines.append(f"async def {name}() -> None:")
        self._push_handler("task", [], reply=None, locale=None)
        try:
            lines.extend(self._emit_block(task.body, 1, new_scope=False))
        finally:
            self._pop_handler()
        lines.append("")
        lines.append("")
        lines.append(f"@{name}.before_loop")
        lines.append(f"async def {name}_wait_until_ready() -> None:")
        lines.append("    await bot.wait_until_ready()")
        return lines

    def _emit_error_handler(self) -> list[str]:
        """@bot.tree.error — the v0 golden's handler, verbatim in
        shape (only when commands exist)."""
        return [
            "@bot.tree.error",
            "async def on_app_command_error(interaction: discord.Interaction,",
            "                               error: app_commands.AppCommandError) -> None:",
            '    log.warning("Command failed: %s", error)',
            '    message = "Sorry, that command hit a problem. Try again."',
            "    try:",
            "        if interaction.response.is_done():",
            "            await interaction.followup.send(message, ephemeral=True)",
            "        else:",
            "            await interaction.response.send_message(message, ephemeral=True)",
            "    except discord.HTTPException:",
            '        log.warning("Could not answer the failed command.")',
        ]

    def _emit_channel_text(self) -> list[str]:
        return [
            "def _channel_text(channel) -> str:",
            '    """The channel as text: its mention, its name, or ""."""',
            "    if channel is None:",
            '        return ""',
            '    return getattr(channel, "mention", None) or str(channel)',
        ]

    def _emit_main(self) -> list[str]:
        return [
            "def main() -> None:",
            '    token = os.environ.get("DISCORD_TOKEN")',
            "    if not token:",
            '        raise SystemExit("set the DISCORD_TOKEN environment variable")',
            "    bot.run(token)",
            "",
            "",
            'if __name__ == "__main__":',
            "    main()",
        ]

    # -- answer statements (reply / defer / edit / followup / send) ---

    def _answer_parts(self, value, indent: int) -> tuple[list[str], str]:
        """A reply/edit/followup value -> (pre-lines, call argument).
        A text value is the argument itself; an embed is built on
        the lines before and passed as `embed=embed`. A `?` value
        propagates first (the temp + early return are hoisted before
        the statement, D7)."""
        p = "    " * indent
        if isinstance(value, A.Propagate):
            unwrapped = self._propagate_unwrapped(value)
            return [], unwrapped
        if isinstance(value, A.EmbedExpr):
            lines = self._emit_embed_lines(value, indent)
            return lines, "embed=embed"
        return [], self.emit_expr(value, 0)

    def _emit_reply(self, stmt: A.Reply, indent: int) -> list[str]:
        p = "    " * indent
        ctx = self._handler_ctx()
        if ctx["reply"] is None:
            self._fail("a `reply` is not possible here (this handler "
                       "has no interaction or channel to answer on)")
        pre, arg = self._answer_parts(stmt.value, indent)
        lines = list(pre)
        if stmt.ephemeral and ctx["reply"] != "interaction":
            self._fail("`ephemeral` only works on an interaction reply")
        eph = ", ephemeral=True" if stmt.ephemeral else ""
        if ctx["reply"] == "interaction":
            lines.append(f"{p}await interaction.response.send_message({arg}{eph})")
        elif ctx["reply"] == "system_channel":
            lines.append(f"{p}await channel.send({arg})")
        elif ctx["reply"] == "message_channel":
            lines.append(f"{p}await message.channel.send({arg})")
        elif ctx["reply"] == "timeout_edit":
            lines.append(f"{p}await self.message.edit(content={arg})")
        return lines

    def _emit_defer(self, stmt: A.Defer, indent: int) -> list[str]:
        p = "    " * indent
        if self._handler_ctx()["reply"] != "interaction":
            self._fail("`defer` needs an interaction (a command or "
                       "component handler)")
        return [f"{p}await interaction.response.defer()"]

    def _emit_edit(self, stmt: A.Edit, indent: int) -> list[str]:
        p = "    " * indent
        if self._handler_ctx()["reply"] != "interaction":
            self._fail("`edit` needs an interaction (a command or "
                       "component handler)")
        pre, arg = self._answer_parts(stmt.value, indent)
        lines = list(pre)
        if arg.startswith("embed="):
            lines.append(f"{p}await interaction.edit_original_response({arg})")
        else:
            lines.append(f"{p}await interaction.edit_original_response(content={arg})")
        return lines

    def _emit_followup(self, stmt: A.Followup, indent: int) -> list[str]:
        p = "    " * indent
        if self._handler_ctx()["reply"] != "interaction":
            self._fail("`followup` needs an interaction (a command or "
                       "component handler)")
        pre, arg = self._answer_parts(stmt.value, indent)
        lines = list(pre)
        lines.append(f"{p}await interaction.followup.send({arg})")
        return lines

    def _emit_send(self, stmt: A.Send, indent: int) -> list[str]:
        p = "    " * indent
        pre, arg = self._answer_parts(stmt.value, indent)
        channel = self.emit_expr(stmt.channel, 0)
        lines = list(pre)
        lines.append(f"{p}await {channel}.send({arg})")
        return lines

    # -- t() and embeds ------------------------------------------------

    def _strings_index(self) -> tuple[dict[str, dict[str, A.StringsEntry]], str]:
        """All strings blocks (main program + imported modules):
        locale -> key -> entry; and the default locale (the first
        declared, D16)."""
        index: dict[str, dict[str, A.StringsEntry]] = {}
        order: list[str] = []
        programs = [self.program] + list(self.program.modules.values())
        for prog in programs:
            for decl in prog.strings:
                if decl.locale not in index:
                    index[decl.locale] = {}
                    order.append(decl.locale)
                for entry in decl.entries:
                    index[decl.locale][entry.key] = entry
        return index, (order[0] if order else "en")

    def _emit_tcall(self, node: A.TCall) -> str:
        """`t("key", name: expr, ...)` -> `_t("key", <locale>, ...)`
        (D16). Template placeholders that are handler bindings
        (user, guild, ...) are auto-passed in their interpolation
        form; the rest must be explicit arguments (the checker
        enforces it — E2009)."""
        index, default = self._strings_index()
        entry = None
        for locale_entries in index.values():
            if node.key in locale_entries:
                entry = locale_entries[node.key]
                break
        if entry is None:
            self._fail(f"unknown strings key `{node.key}` (the checker "
                       "should have rejected it)")
        locale = (self._handler_ctx_stack[-1]["locale"]
                  if self._handler_ctx_stack else None) or "_DEFAULT_LOCALE"
        explicit = {name: value for name, value, _span in node.args}
        kwargs: list[str] = []
        for name, value in explicit.items():
            kwargs.append(f"{name}={self.emit_expr(value, 0)}")
        for placeholder in entry.placeholders:
            if placeholder in explicit:
                continue
            binding = self._lookup(placeholder)
            if binding is not None and binding.kind == "handler":
                kwargs.append(f"{placeholder}={binding.interp_form()}")
                continue
            self._fail(f"`t(\"{node.key}\")` needs an argument for "
                       f"placeholder `{placeholder}` (the checker "
                       "should have rejected it)")
        args = ", ".join([f'"{node.key}"', locale] + kwargs)
        return f"_t({args})"

    def _emit_embed(self, node: A.EmbedExpr) -> str:
        # An embed as a value (SPEC §13.7: an `embed:` block is an
        # expression, e.g. `let card: embed = embed: ...`): build it
        # on hoisted lines before the enclosing statement and use
        # the `embed` variable.
        self._hoists.append(self._emit_embed_lines(node, 0))
        return "embed"

    def _emit_embed_lines(self, node: A.EmbedExpr, indent: int) -> list[str]:
        """Build a discord.Embed on the lines before the send call.
        Field values are emitted as expressions; a `color:` is a
        literal int (the checker validates the range)."""
        p = "    " * indent
        lines = [f"{p}embed = discord.Embed("]
        for field in node.fields:
            if field.kind == "title":
                lines.append(f"{p}    title={self.emit_expr(field.value, 0)},")
            elif field.kind == "description":
                lines.append(f"{p}    description={self.emit_expr(field.value, 0)},")
            elif field.kind == "color":
                lines.append(f"{p}    color={self.emit_expr(field.value, 0)},")
        lines.append(f"{p})")
        for field in node.fields:
            if field.kind == "field":
                inline = "True" if field.inline else "False"
                # A field's name is a plain string (not an expression).
                lines.append(f"{p}embed.add_field(name={field.name!r}, value={self.emit_expr(field.value, 0)}, inline={inline})")
        return lines

    # -- store calls ----------------------------------------------------

    def _store_key_expr(self, arg) -> str:
        """The key expression for a store call: an entity (user /
        member) contributes `str(<expr>.id)` (the key column is a
        str); a str is used as-is."""
        t = self._type_of(arg)
        text = self.emit_expr(arg, 0)
        if t.name in ("user", "member"):
            return f"str({text}.id)"
        if t.name == "str":
            return text
        return f"str({text})"

    def _emit_store_call(self, store_name: str, method: str,
                         node: A.Call) -> str:
        """`economy.balance(user)` etc. -> `await bot.<store>_<op>(...)`
        (D17/P11). Every mutation takes its idempotency `key:`; the
        checker enforces that it is present.

        §19 guard: the `await` is only valid inside an async handler
        (command / event / task / component). At the top level,
        inside a (sync) top-level fn, or inside a lambda there is no
        async context — emitting the `await` would produce a
        SyntaxError in the generated file (proven live: a store call
        inside a lambda in a handler emitted `await` inside a sync
        lambda on both targets). Fail cleanly instead of emitting
        unparseable code."""
        ctx = self._fn_ctx[-1] if self._fn_ctx else None
        if ctx is None or not ctx.get("in_handler"):
            self._fail(
                f"a store call (`{store_name}.{method}`) is only "
                "possible inside a command, event, or task handler — "
                "top-level functions and statements run outside the "
                "bot's async context")
        key_arg = {name: value for name, value, _span in node.kwargs}.get("key")
        key = (self.emit_expr(key_arg, 0) if key_arg is not None else None)
        if method == "balance":
            return (f"await bot.{store_name}_balance("
                    f"{self._store_key_expr(node.args[0])})")
        if method == "credit":
            return (f"await bot.{store_name}_credit("
                    f"{self._store_key_expr(node.args[0])}, "
                    f"{self.emit_expr(node.args[1], 0)}, key={key})")
        if method == "debit":
            return (f"await bot.{store_name}_debit("
                    f"{self._store_key_expr(node.args[0])}, "
                    f"{self.emit_expr(node.args[1], 0)}, key={key})")
        if method == "transfer":
            return (f"await bot.{store_name}_transfer("
                    f"{self._store_key_expr(node.args[0])}, "
                    f"{self._store_key_expr(node.args[1])}, "
                    f"{self.emit_expr(node.args[2], 0)}, key={key})")
        if method == "get":
            table = node.args[0]
            if not isinstance(table, A.Lit) or not isinstance(table.value, str):
                self._fail("`get`'s table must be a string literal "
                           "naming a declared table")
            return (f"await bot.{store_name}_get({self.emit_expr(table, 0)}, "
                    f"{self.emit_expr(node.args[1], 0)})")
        if method == "set":
            table = node.args[0]
            if not isinstance(table, A.Lit) or not isinstance(table.value, str):
                self._fail("`set`'s table must be a string literal "
                           "naming a declared table")
            return (f"await bot.{store_name}_set({self.emit_expr(table, 0)}, "
                    f"{self.emit_expr(node.args[1], 0)})")
        if method == "delete":
            table = node.args[0]
            if not isinstance(table, A.Lit) or not isinstance(table.value, str):
                self._fail("`delete`'s table must be a string literal "
                           "naming a declared table")
            return (f"await bot.{store_name}_delete({self.emit_expr(table, 0)}, "
                    f"{self.emit_expr(node.args[1], 0)})")
        self._fail(f"store `{store_name}` has no operation `{method}`")

    # -- components (button / select / modal) -------------------------

    def _component_class_name(self, label: str, suffix: str) -> str:
        """A component's class name, from its label/title: "Buy again"
        -> BuyAgainView; "Pick one" -> PickOneSelect. Unique per
        file (a numeric suffix on a repeat)."""
        words = "".join(ch if ch.isalnum() else " " for ch in label).split()
        base = "".join(w.capitalize() for w in words) or "Component"
        name = base + suffix
        if not hasattr(self, "_component_names"):
            self._component_names: set[str] = set()
        if name in self._component_names:
            n = 2
            while f"{name}{n}" in self._component_names:
                n += 1
            name = f"{name}{n}"
        self._component_names.add(name)
        return name

    def _component_var_name(self, label: str) -> str:
        words = "".join(ch if ch.isalnum() else " " for ch in label).split()
        return "_".join(w.lower() for w in words) + "_view" or "view"

    def _author_check(self, only_kind: str, only_roles, indent: int) -> list[str]:
        """The `only:` gate at the top of a component callback:
        author / anyone / roles."""
        p = "    " * indent
        if only_kind == "author":
            return [
                f"{p}if self._author_id is not None and interaction.user.id != self._author_id:",
                f'{p}    await interaction.response.send_message("This button is not for you.", ephemeral=True)',
                f"{p}    return",
            ]
        if only_kind == "roles":
            roles = ", ".join(repr(r) for r in (only_roles or []))
            return [
                f"{p}if not any(role.name in [{roles}] for role in getattr(interaction.user, \"roles\", [])):",
                f'{p}    await interaction.response.send_message("You do not have a role that can use this.", ephemeral=True)',
                f"{p}    return",
            ]
        return []

    def _emit_button(self, stmt: A.ButtonDecl, indent: int) -> list[str]:
        p = "    " * indent
        cls = self._component_class_name(stmt.label, "View")
        var = self._component_var_name(stmt.label)
        style = stmt.style or "primary"
        if style == "link":
            self._fail("a `link` button cannot have a `do:` body "
                       "(Discord never sends an interaction for it)")
        # The View class (registered for module level).
        lines = [f"class {cls}(discord.ui.View):"]
        lines.append("    def __init__(self, author_id: int | None = None) -> None:")
        timeout = stmt.timeout if stmt.timeout is not None else 180
        lines.append(f"        super().__init__(timeout={timeout})")
        lines.append("        self._author_id = author_id")
        lines.append("        self.message = None")
        lines.append("")
        custom = f', custom_id="{stmt.custom_id}"' if stmt.custom_id else ""
        lines.append(f'    @discord.ui.button(label="{stmt.label}", style=discord.ButtonStyle.{style}{custom})')
        lines.append("    async def _on_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:")
        self._push_handler("component",
                           ["user", "guild", "channel", "interaction"],
                           reply="interaction",
                           locale="interaction.locale")
        try:
            lines.extend(self._author_check(stmt.only_kind, stmt.only_roles, 2))
            lines.extend(self._emit_block(stmt.body, 2, new_scope=False))
        finally:
            self._pop_handler()
        if stmt.on_timeout is not None:
            lines.append("")
            lines.append("    async def on_timeout(self) -> None:")
            lines.append("        for child in self.children:")
            lines.append("            child.disabled = True")
            lines.append("        if self.message is not None:")
            self._push_handler("timeout", [], reply="timeout_edit",
                               locale=None)
            try:
                lines.extend(self._emit_block(stmt.on_timeout, 3,
                                              new_scope=False))
            finally:
                self._pop_handler()
        self._bot_components.append(lines)
        # The instantiation (in the handler); the view is attached to
        # the most recent answer by _emit_block (D40).
        self._pending_view = var
        self._pending_view_timeout = stmt.on_timeout is not None
        return [f"{p}{var} = {cls}(interaction.user.id)"]

    def _emit_select(self, stmt: A.SelectDecl, indent: int) -> list[str]:
        p = "    " * indent
        cls = self._component_class_name(stmt.label, "Select")
        view_cls = self._component_class_name(stmt.label, "View")
        var = self._component_var_name(stmt.label)
        timeout = stmt.timeout if stmt.timeout is not None else 180
        min_v = stmt.min_values if stmt.min_values is not None else 1
        max_v = stmt.max_values if stmt.max_values is not None else 1
        options = ", ".join(
            f'discord.SelectOption(label="{o}", value="{o}")'
            for o in stmt.options)
        # The Select class.
        lines = [f"class {cls}(discord.ui.Select):"]
        lines.append("    def __init__(self, author_id: int | None = None) -> None:")
        lines.append(f"        super().__init__(custom_id=\"{view_cls.lower()}\", placeholder=\"{stmt.label}\", min_values={min_v}, max_values={max_v}, options=[{options}])")
        lines.append("        self._author_id = author_id")
        lines.append("")
        lines.append("    async def callback(self, interaction: discord.Interaction) -> None:")
        self._push_handler("component",
                           ["user", "guild", "channel", "interaction"],
                           reply="interaction",
                           locale="interaction.locale")
        try:
            self._handler_binding("values", "self.values",
                                  '", ".join(self.values)', "list[str]")
            lines.extend(self._author_check(stmt.only_kind, stmt.only_roles, 2))
            lines.extend(self._emit_block(stmt.body, 2, new_scope=False))
        finally:
            self._pop_handler()
        self._bot_components.append(lines)
        # The View that holds it (a Select cannot be sent alone).
        view_lines = [f"class {view_cls}(discord.ui.View):"]
        view_lines.append("    def __init__(self, author_id: int | None = None) -> None:")
        view_lines.append(f"        super().__init__(timeout={timeout})")
        view_lines.append("        self._author_id = author_id")
        view_lines.append("        self.message = None")
        view_lines.append(f"        self.add_item({cls}(author_id))")
        if stmt.on_timeout is not None:
            view_lines.append("")
            view_lines.append("    async def on_timeout(self) -> None:")
            view_lines.append("        for child in self.children:")
            view_lines.append("            child.disabled = True")
            view_lines.append("        if self.message is not None:")
            self._push_handler("timeout", [], reply="timeout_edit",
                               locale=None)
            try:
                view_lines.extend(self._emit_block(stmt.on_timeout, 3,
                                                   new_scope=False))
            finally:
                self._pop_handler()
        self._bot_components.append(view_lines)
        self._pending_view = var
        self._pending_view_timeout = stmt.on_timeout is not None
        return [f"{p}{var} = {view_cls}(interaction.user.id)"]

    def _emit_modal(self, stmt: A.ModalDecl, indent: int) -> list[str]:
        p = "    " * indent
        cls = self._component_class_name(stmt.title, "Modal")
        lines = [f"class {cls}(discord.ui.Modal):"]
        title = f'title="{stmt.title}"'
        timeout = f", timeout={stmt.timeout}" if stmt.timeout is not None else ""
        custom = f', custom_id="{stmt.custom_id}"' if stmt.custom_id else ""
        lines.append("    def __init__(self) -> None:")
        lines.append(f"        super().__init__({title}{timeout}{custom})")
        lines.append("")
        for field in stmt.fields:
            style = ("paragraph" if field.style == "paragraph"
                     else "short")
            req = "True" if field.required else "False"
            mn = f", min_length={field.min_length}" if field.min_length is not None else ""
            mx = f", max_length={field.max_length}" if field.max_length is not None else ""
            lines.append(f"    {field.name} = discord.ui.TextInput(label=\"{field.name}\", style=discord.TextStyle.{style}, required={req}{mn}{mx})")
        lines.append("")
        lines.append("    async def on_submit(self, interaction: discord.Interaction) -> None:")
        self._push_handler("component",
                           ["user", "guild", "channel", "interaction"],
                           reply="interaction",
                           locale="interaction.locale")
        try:
            for field in stmt.fields:
                self._handler_binding(field.name, f"self.{field.name}.value",
                                      f"self.{field.name}.value", "str")
            lines.extend(self._emit_block(stmt.body, 2, new_scope=False))
        finally:
            self._pop_handler()
        if stmt.on_timeout is not None:
            lines.append("")
            lines.append("    async def on_timeout(self) -> None:")
            self._push_handler("timeout", [], reply="timeout_edit",
                               locale=None)
            try:
                lines.extend(self._emit_block(stmt.on_timeout, 2,
                                              new_scope=False))
            finally:
                self._pop_handler()
        self._bot_components.append(lines)
        return [f"{p}await interaction.response.send_modal({cls}())"]

    # -- store methods on Bot (D17/P11) ---------------------------------

    _SQL_TYPES = {"int": "INTEGER", "str": "TEXT", "float": "REAL",
                  "bool": "INTEGER"}

    def _emit_store_methods(self, store: A.StoreDecl) -> list[str]:
        """The store's methods on the Bot class: a connect (schema
        creation), balance / credit / debit / transfer (the economy
        ops, one transaction each, idempotent on `key`), and
        get / set / delete for general tables."""
        name = store.name
        if store.ledger:
            # The ledger's created_at uses time.time().
            self._need_import("time")
        # get / set return rows as types.SimpleNamespace.
        self._need_import("types")
        lines: list[str] = []
        lines.append("")
        lines.append(f"    async def _{name}_connect(self) -> None:")
        lines.append(f'        self.{name}_db = await aiosqlite.connect("{name}.db")')
        # The economy ops serialise on this lock (see
        # _emit_economy_methods): aiosqlite runs all ops in one
        # thread, so a second coroutine's BEGIN IMMEDIATE would
        # block that thread until the busy timeout.
        self._need_import("asyncio")
        lines.append(f"        self.{name}_lock = asyncio.Lock()")
        for table in store.tables:
            cols = []
            for i, col in enumerate(table.columns):
                sql = self._SQL_TYPES.get(col.type.name, "TEXT")
                pk = " PRIMARY KEY" if i == 0 else ""
                cols.append(f"{col.name} {sql}{pk}")
            cols_sql = ", ".join(cols)
            lines.append(f'        await self.{name}_db.execute("CREATE TABLE IF NOT EXISTS {table.name} ({cols_sql})")')
        if store.ledger:
            lines.append(f'        await self.{name}_db.execute("CREATE TABLE IF NOT EXISTS ledger (id INTEGER PRIMARY KEY AUTOINCREMENT, user TEXT NOT NULL, amount INTEGER NOT NULL, key TEXT UNIQUE, created_at INTEGER NOT NULL)")')
        lines.append(f"        await self.{name}_db.commit()")
        # The economy ops (only when the store has a balances table
        # of the required shape — the checker verifies it).
        tables = {t.name: t for t in store.tables}
        if "balances" in tables:
            lines.extend(self._emit_economy_methods(name))
        for table in store.tables:
            lines.extend(self._emit_table_methods(name, table))
        return lines

    def _emit_economy_methods(self, name: str) -> list[str]:
        n = name
        return [
            "",
            f"    async def {n}_balance(self, user_id: str) -> int:",
            f"        async with self.{n}_lock:",
            f"            async with self.{n}_db.execute(\"SELECT balance FROM balances WHERE user = ?\", (user_id,)) as cursor:",
            "                row = await cursor.fetchone()",
            "            return row[0] if row is not None else 0",
            "",
            f"    async def _{n}_apply(self, user_id: str, amount: int, key: str | None) -> None:",
            "        # Idempotent on key: a duplicate key is a replay,",
            "        # not an error (P11). One explicit transaction:",
            "        # the balance + ledger land together or not at all,",
            "        # and a failed ledger INSERT rolls the balance",
            "        # change back instead of leaving it for a later",
            "        # commit() to commit. The lock serialises the ops",
            "        # (and the reads): aiosqlite runs all ops in one",
            "        # thread, so a second coroutine's BEGIN IMMEDIATE",
            "        # would block that thread until the busy timeout,",
            "        # and a concurrent read would see uncommitted",
            "        # partial state. The lock is not reentrant: no",
            "        # economy method may call another while holding it",
            "        # (credit delegates to _apply without taking it).",
            f"        async with self.{n}_lock:",
            "            if key is not None:",
            f"                async with self.{n}_db.execute(\"SELECT 1 FROM ledger WHERE key = ?\", (key,)) as cursor:",
            "                    if await cursor.fetchone() is not None:",
            "                        return",
            f"            await self.{n}_db.execute(\"BEGIN IMMEDIATE\")",
            "            try:",
            f"                await self.{n}_db.execute(\"INSERT INTO balances (user, balance) VALUES (?, ?) ON CONFLICT(user) DO UPDATE SET balance = balance + excluded.balance\", (user_id, amount))",
            f"                await self.{n}_db.execute(\"INSERT INTO ledger (user, amount, key, created_at) VALUES (?, ?, ?, ?)\", (user_id, amount, key, int(time.time())))",
            f"                await self.{n}_db.execute(\"COMMIT\")",
            "            except Exception:",
            f"                await self.{n}_db.execute(\"ROLLBACK\")",
            "                raise",
            "",
            f"    async def {n}_credit(self, user_id: str, amount: int, key: str | None = None) -> None:",
            f"        await self._{n}_apply(user_id, amount, key)",
            "",
            f"    async def {n}_debit(self, user_id: str, amount: int, key: str | None = None) -> bool:",
            "        # The balance check is inside the transaction (no",
            "        # TOCTOU): a concurrent debit cannot slip between",
            "        # the check and the write. A replayed key returns",
            "        # True — the debit already happened.",
            f"        async with self.{n}_lock:",
            "            if key is not None:",
            f"                async with self.{n}_db.execute(\"SELECT 1 FROM ledger WHERE key = ?\", (key,)) as cursor:",
            "                    if await cursor.fetchone() is not None:",
            "                        return True",
            f"            await self.{n}_db.execute(\"BEGIN IMMEDIATE\")",
            "            try:",
            f"                async with self.{n}_db.execute(\"SELECT balance FROM balances WHERE user = ?\", (user_id,)) as cursor:",
            "                    row = await cursor.fetchone()",
            "                if (row[0] if row is not None else 0) < amount:",
            f"                    await self.{n}_db.execute(\"ROLLBACK\")",
            "                    return False",
            f"                await self.{n}_db.execute(\"INSERT INTO balances (user, balance) VALUES (?, ?) ON CONFLICT(user) DO UPDATE SET balance = balance + excluded.balance\", (user_id, -amount))",
            f"                await self.{n}_db.execute(\"INSERT INTO ledger (user, amount, key, created_at) VALUES (?, ?, ?, ?)\", (user_id, -amount, key, int(time.time())))",
            f"                await self.{n}_db.execute(\"COMMIT\")",
            "            except Exception:",
            f"                await self.{n}_db.execute(\"ROLLBACK\")",
            "                raise",
            "            return True",
            "",
            f"    async def {n}_transfer(self, from_id: str, to_id: str, amount: int, key: str | None = None) -> bool:",
            "        # Both legs + the ledger in ONE transaction. A retry",
            "        # heals a partial transfer: each leg is topped up",
            "        # to its intended total (the out-leg debits `amount`,",
            "        # the in-leg credits `amount`), so no money is",
            "        # created or destroyed however the first attempt",
            "        # died — a crash between the legs, or a poisoned",
            "        # ledger row. The leg INSERTs upsert on the key:",
            "        # a row that merely exists (amount 0) is topped up",
            "        # instead of colliding with the UNIQUE constraint.",
            "        # The debit side was already authorised at the",
            "        # original attempt, so the remaining debit is",
            "        # applied unconditionally; refusing would leave",
            "        # money destroyed. The per-leg amounts are read from",
            "        # the ledger (not just presence): a row that merely",
            "        # exists with amount 0 heals the full leg.",
            f"        async with self.{n}_lock:",
            "            out_done = 0",
            "            in_done = 0",
            "            if key is not None:",
            f"                async with self.{n}_db.execute(\"SELECT COALESCE(SUM(amount), 0) FROM ledger WHERE key = ?\", (f\"{{key}}:out\",)) as cursor:",
            "                    out_done = -(await cursor.fetchone())[0]",
            f"                async with self.{n}_db.execute(\"SELECT COALESCE(SUM(amount), 0) FROM ledger WHERE key = ?\", (f\"{{key}}:in\",)) as cursor:",
            "                    in_done = (await cursor.fetchone())[0]",
            "                out_done = max(0, min(amount, out_done))",
            "                in_done = max(0, min(amount, in_done))",
            "                if out_done >= amount and in_done >= amount:",
            "                    return True",
            "            out_left = amount - out_done",
            "            in_left = amount - in_done",
            f"            await self.{n}_db.execute(\"BEGIN IMMEDIATE\")",
            "            try:",
            "                if out_left > 0:",
            f"                    async with self.{n}_db.execute(\"SELECT balance FROM balances WHERE user = ?\", (from_id,)) as cursor:",
            "                        row = await cursor.fetchone()",
            "                    if (row[0] if row is not None else 0) < out_left:",
            f"                        await self.{n}_db.execute(\"ROLLBACK\")",
            "                        return False",
            f"                    await self.{n}_db.execute(\"INSERT INTO balances (user, balance) VALUES (?, ?) ON CONFLICT(user) DO UPDATE SET balance = balance + excluded.balance\", (from_id, -out_left))",
            f"                    await self.{n}_db.execute(\"INSERT INTO ledger (user, amount, key, created_at) VALUES (?, ?, ?, ?) ON CONFLICT(key) DO UPDATE SET amount = ledger.amount + excluded.amount\", (from_id, -out_left, f\"{{key}}:out\" if key is not None else None, int(time.time())))",
            "                if in_left > 0:",
            f"                    await self.{n}_db.execute(\"INSERT INTO balances (user, balance) VALUES (?, ?) ON CONFLICT(user) DO UPDATE SET balance = balance + excluded.balance\", (to_id, in_left))",
            f"                    await self.{n}_db.execute(\"INSERT INTO ledger (user, amount, key, created_at) VALUES (?, ?, ?, ?) ON CONFLICT(key) DO UPDATE SET amount = ledger.amount + excluded.amount\", (to_id, in_left, f\"{{key}}:in\" if key is not None else None, int(time.time())))",
            f"                await self.{n}_db.execute(\"COMMIT\")",
            "            except Exception:",
            f"                await self.{n}_db.execute(\"ROLLBACK\")",
            "                raise",
            "            return True",
        ]

    def _emit_table_methods(self, name: str, table: A.TableDecl) -> list[str]:
        """`get` / `set` / `delete` for one declared table. A row is
        a SimpleNamespace (attribute access works for its fields);
        `set` takes such a row (or a struct) via vars(). All three
        take the store lock: a `set` / `delete` commit() running
        concurrently with an economy transaction would commit that
        transaction part-way through."""
        n = name
        key_col = table.columns[0].name if table.columns else "id"
        return [
            "",
            f"    async def {n}_get(self, table: str, key) -> types.SimpleNamespace | None:",
            f"        async with self.{n}_lock:",
            f"            async with self.{n}_db.execute(f\"SELECT * FROM {{table}} WHERE {key_col} = ?\", (key,)) as cursor:",
            "                row = await cursor.fetchone()",
            "                if row is None:",
            "                    return None",
            "                cols = [d[0] for d in cursor.description]",
            "                return types.SimpleNamespace(**dict(zip(cols, row)))",
            "",
            f"    async def {n}_set(self, table: str, row) -> None:",
            f"        async with self.{n}_lock:",
            "            data = vars(row)",
            "            cols = \", \".join(data)",
            "            placeholders = \", \".join(\"?\" * len(data))",
            f"            updates = \", \".join(f\"{{c}} = excluded.{{c}}\" for c in data)",
            f"            await self.{n}_db.execute(f\"INSERT INTO {{table}} ({{cols}}) VALUES ({{placeholders}}) ON CONFLICT({key_col}) DO UPDATE SET {{updates}}\", tuple(data.values()))",
            f"            await self.{n}_db.commit()",
            "",
            f"    async def {n}_delete(self, table: str, key) -> None:",
            f"        async with self.{n}_lock:",
            f"            await self.{n}_db.execute(f\"DELETE FROM {{table}} WHERE {key_col} = ?\", (key,))",
            f"            await self.{n}_db.commit()",
        ]

    # -- strings / _t (D16) ---------------------------------------------

    def _emit_strings(self, program: A.Program) -> list[str]:
        """The STRINGS dict + _DEFAULT_LOCALE + the _t helper (only
        when `strings` blocks exist). Lookup: exact locale -> base
        language -> default locale (the first declared)."""
        if not program.strings:
            return []
        self._need_rt("to_str")
        default = program.strings[0].locale
        lines = [f'_DEFAULT_LOCALE = "{default}"', "STRINGS = {"]
        for decl in program.strings:
            lines.append(f'    "{decl.locale}": {{')
            for entry in decl.entries:
                lines.append(f'        "{entry.key}": "{entry.template}",')
            lines.append("    },")
        lines.append("}")
        lines.append("")
        lines.append("")
        lines.append("def _t(key, locale, **kwargs) -> str:")
        lines.append('    loc = getattr(locale, "value", locale) or _DEFAULT_LOCALE')
        lines.append("    template = (STRINGS.get(loc) or STRINGS.get(str(loc).split(\"-\")[0])")
        lines.append("                or STRINGS.get(_DEFAULT_LOCALE, {})).get(key)")
        lines.append("    if template is None:")
        lines.append("        return key")
        lines.append("    return template.format(**{k: to_str(v) for k, v in kwargs.items()})")
        return lines

    def _emit_file_set(self, program: A.Program, main_path: str):
        """Emit one program and every file module it imports (recursively).
        Returns (files, uses_rt)."""
        files: dict[str, str] = {}
        text, uses_rt = self._emit_one(program, main_path)
        files[Path(main_path).stem + ".py"] = text
        for imp in program.imports:
            if imp.path is None:
                continue
            bound = imp.bound_name
            sub = program.modules.get(bound)
            if sub is None:
                continue  # the resolver would have errored; unreachable
            sub_path = imp.resolved_path or imp.path
            # The header names the file as the user would see it:
            # relative to the working directory when it is under it
            # (D2 — so building `examples/shop.newr` and building
            # `examples/shop_items.newr` directly give the same
            # shop_items.py header).
            import os
            sub_path = os.path.relpath(sub_path)
            sub_em = _Emitter(sub, main_path=sub_path, sources=self.sources,
                              line_comments=self.line_comments)
            sub_files, sub_rt = sub_em._emit_file_set(sub, sub_path)
            files.update(sub_files)
            uses_rt = uses_rt or sub_rt
        return files, uses_rt


    def _emit_one(self, program: A.Program, main_path: str):
        """Emit a single program's file. Returns (text, uses_rt)."""
        self.program = program
        if program.bot is not None:
            return self._emit_bot_file(program, main_path)
        return self._emit_general_file(program, main_path)


    def _header(self, main_path: str, description: str | None = None) -> list[str]:
        lines = [
            f"# Generated by newr from {main_path} — do not edit by hand.",
            f"# Edit the .newr file and rebuild with: newr build {main_path}",
        ]
        if description:
            lines.append(f"# {description}")
        return lines


    def _register_module_scope(self, program: A.Program):
        """Register every module-level name of a program in the module
        scope (the bottom of the scope stack), in a deterministic order:
        imports, structs, enums, functions, top-level lets/consts (in
        file order), stores. The registration happens before any
        emission, so a function body may reference a later `let` (the
        checker's deferred-execution exemption) and the top-level
        statements see every name."""
        self._push_scope()
        for imp in program.imports:
            bound = imp.bound_name
            if imp.module is not None:
                self._bind_module_level(bound, kind="named-module")
                self._scopes[0][bound].module = imp.module
            elif imp.path is not None:
                self._bind_module_level(bound, kind="file-module")
        for decl in program.structs:
            self._bind_module_level(decl.name, kind="struct")
        for decl in program.enums:
            self._bind_module_level(decl.name, kind="enum")
        for decl in program.functions:
            ret = decl.return_type if decl.return_type is not None else Type("none")
            params = tuple(p.type for p in decl.params) + (ret,)
            self._bind_module_level(
                decl.name, type=Type("fn", params=params), kind="fn")
        for st in program.stmts:
            if isinstance(st, (A.Let, A.Const)):
                t = st.type_ann
                if t is None:
                    t = self._type_or_none(st.value)
                self._bind_module_level(st.name, type=t, kind="global-let")
        for decl in program.stores:
            self._bind_module_level(decl.name, kind="store")
        # The top level's used-name set starts with every module-level
        # Python name, so a `let` in a top-level block cannot reuse one
        # (at module level Python has no block scope — the name would
        # clobber the module global).
        self._fn_used = set(self._module_used)


    def _emit_general_file(self, program: A.Program, main_path: str):
        """A general (non-bot) program: header, imports, structs/enums,
        functions, then the top-level STATEMENTS in file order (SPEC
        §8). Definitions (structs/enums/functions) are hoisted — a
        function body reads module globals at call time, so emitting
        the `def` before the statements is safe — but the statements
        themselves (lets, assignments, prints, ...) must run in the
        order written. The old two-phase layout (all lets, then the
        other statements) silently computed the wrong value whenever
        an assignment sat between two lets (M2 evaluation #1)."""
        self.program = program
        self.fns = {f.name: f for f in program.functions}
        self.structs = {s.name: s for s in program.structs}
        self.enums = {e.name: e for e in program.enums}
        self.stores = {s.name: s for s in program.stores}
        self._register_module_scope(program)
        self._fn_ctx = []
        self._fn_globals = set()
        self._hoists = []
        body: list[str] = []
        for decl in program.structs:
            body.extend(self._emit_struct(decl))
            body.append("")
        for decl in program.enums:
            body.extend(self._emit_enum(decl))
            body.append("")
        for decl in program.functions:
            body.extend(self._emit_fn(decl))
            body.append("")
        for st in program.stmts:
            body.extend(self._emit_stmt_with_hoists(st, 0))
        # Drop the trailing blank line the decl loop leaves behind.
        while body and body[-1] == "":
            body.pop()
        lines = self._header(main_path)
        if self._used_log:
            # P10: a general program using `log` configures logging
            # once. (Registered before the imports are rendered.)
            self._need_import("logging")
        imports = self._render_imports()
        if imports:
            lines.append("")
            lines.extend(imports)
        if self._used_log:
            lines.append("")
            lines.append('logging.basicConfig(')
            lines.append('    level=logging.INFO,')
            lines.append('    format="%(asctime)s %(levelname)s %(name)s: %(message)s",')
            lines.append(')')
        if body:
            lines.append("")
            lines.extend(body)
        text = "\n".join(lines) + "\n"
        return text, bool(self.rt_helpers or self.rt_module_used)



# ---------------------------------------------------------------- entry points


def emit_program(program: A.Program, *, main_path: str,
                 sources: dict[str, str],
                 line_comments: bool = False) -> EmitResult:
    """Emit a CHECKED program to Python — one `.py` per `.newr` file.

    `main_path` is the input file as passed (D2: the header names
    it); `sources` maps file paths to their text (for diagnostics
    and `--line-comments`). The result's `files` maps an output
    basename (`hello.py`) to its text; `main_file` is the main
    output's basename; `uses_rt` says whether any file imports
    `newr_rt` (the caller copies `compiler/newr_rt.py` next to the
    output when it is true, D4)."""
    from pathlib import Path
    em = _Emitter(program, main_path=main_path, sources=sources,
                  line_comments=line_comments)
    files, uses_rt = em._emit_file_set(program, main_path)
    return EmitResult(files=files,
                      main_file=Path(main_path).stem + ".py",
                      uses_rt=uses_rt)
