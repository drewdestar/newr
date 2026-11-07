"""The JavaScript emitter (M3, SPEC.md §18).

``newr build --target js`` lowers a *checked* program (the front end
has already run: parse -> resolve -> check) to readable JavaScript -
one `.newr` file -> one `.js` file, structure preserved, user names
verbatim, helpers only in `newr_rt.js` (imported when used).

Readability is a requirement, not a nicety: the output is code a
human would write - the JS goldens (`goldens/js/`) are the style
oracle, and the emitter reproduces them byte-for-byte
(BUILD_REPORT.md §17, J1–J13).

The emitter NEVER runs on an unchecked program: the CLI checks first
and exits 4 on errors. If it meets a node without a type (which only
happens on an unchecked program), it raises `EmitterError` - a clean
refusal, never a TypeError traceback.

Module system (J1): CommonJS - `require("./newr_rt.js")` - so an
emitted file runs with plain `node file.js` and no package.json.
Node floor: 24.17.0 (the `node:sqlite` builtin and BigInt are the
only modern features used; the emitted code itself is plain ES2020).

Encoding: every `open()` / `read_text()` in this file passes
`encoding="utf-8"` (CI enforces it).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from . import ast_nodes as A
from .ast_nodes import Type, fn_of
from .emitter import EmitterError, EmitResult
from .errors import Span
from .modules import BUILTINS, METHODS, MODULES

_BUILTIN_NAMES = frozenset(BUILTINS)

# Names the emitter itself may import or define in an emitted file.
# A user name colliding with one of these is mangled with a trailing
# underscore (D20, mirrored from the Python emitter). `math` /
# `random` / `time` / `file` / `env` / `discord` are already reserved
# module names in newr (E3007); the rest are reserved here, by the
# emitter: Node globals, the `newr_rt` helper functions (imported
# bare via a destructured require, so their names are reserved too),
# and the discord.js names imported in bot files.
_RT_HELPERS = frozenset({
    "add", "sub", "mul", "neg", "powInt", "powFloat", "tdiv", "tmod",
    "fmod", "roundHalfAway", "toInt", "toFloat", "toBool", "toStr",
    "floatStr", "checkInt", "asError", "optChain", "coalesce",
    "listGet", "listIndex", "listContains", "setRemove", "unwrapOpt",
    "tryResult", "timeFormat", "fileRead", "fileWrite", "fileAppend",
    "fileExists", "randomInt", "randomChoice", "randomShuffle",
    "eq", "range", "print", "input", "ok", "err",
})
_RT_CLASSES = frozenset({
    "NewrError", "NewrOverflowError", "NewrDivisionByZeroError",
    "Result",
})
_DISCORD_JS = frozenset({
    "Client", "GatewayIntentBits", "Events", "SlashCommandBuilder",
    "EmbedBuilder", "ActionRowBuilder", "ButtonBuilder", "ButtonStyle",
    "StringSelectMenuBuilder", "StringSelectMenuOptionBuilder",
    "ModalBuilder", "TextInputBuilder",
    "TextInputStyle", "REST", "Routes", "Collection", "ComponentType",
    "ChannelType", "PermissionFlagsBits",
})
_EMITTER_RESERVED = frozenset({
    "require", "module", "exports", "process", "console", "Buffer",
    "setTimeout", "setInterval", "clearTimeout", "clearInterval",
    "__dirname", "__filename", "globalThis", "newrRt",
    "DatabaseSync", "bot", "commands", "modalHandlers",
}) | _RT_HELPERS | _RT_CLASSES | _DISCORD_JS

# JavaScript reserved words (ECMAScript 2024) + contextual keywords
# that cannot be used as identifiers in the positions the emitter
# writes. `let`/`const`/`function`/`class` are the load-bearing ones;
# the rest are reserved to keep the emitted code future-proof.
_JS_KEYWORDS = frozenset({
    "await", "break", "case", "catch", "class", "const", "continue",
    "debugger", "default", "delete", "do", "else", "export", "extends",
    "false", "finally", "for", "function", "if", "import", "in",
    "instanceof", "let", "new", "null", "return", "static", "super",
    "switch", "this", "throw", "true", "try", "typeof", "var", "void",
    "while", "with", "yield", "enum", "implements", "interface",
    "package", "private", "protected", "public", "arguments", "eval",
    "of", "get", "set", "from", "async", "as",
})


def _js_str(value: str, quote: str = '"') -> str:
    """Render a newr string value as a JavaScript string literal.

    Double quotes unless the value contains one (then single quotes),
    unless it contains both (then double quotes with escapes).
    Non-ASCII printable characters are emitted literally (UTF-8
    source); control characters use their escapes. A JS string
    literal and a Python one escape the same set here.
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
        elif ch == "\b":
            out.append("\\b")
        elif ch == "\f":
            out.append("\\f")
        elif ch == "\v":
            out.append("\\v")
        elif ch == "\0":
            out.append("\\0")
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
    # Both quote types present: double quotes with the doubles escaped.
    return '"' + body.replace('"', '\\"') + '"'


def _js_tpl_text(text: str) -> str:
    """A literal part of a template literal: backticks and `${`
    escaped, backslashes and control characters escaped like
    `_js_str`. A raw CR/LF never appears (it would be normalised by
    the template literal); the escapes keep the value exact."""
    out = []
    for ch in text:
        if ch == "`":
            out.append("\\`")
        elif ch == "$":
            out.append("\\$")
        elif ch == "\\":
            out.append("\\\\")
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\t":
            out.append("\\t")
        elif ch == "\r":
            out.append("\\r")
        elif ch == "\b":
            out.append("\\b")
        elif ch == "\f":
            out.append("\\f")
        elif ch == "\v":
            out.append("\\v")
        elif ch == "\0":
            out.append("\\0")
        elif ord(ch) < 0x20 or ord(ch) == 0x7F:
            out.append(f"\\x{ord(ch):02x}")
        else:
            out.append(ch)
    return "".join(out)


# JavaScript precedence (higher binds tighter), loosest -> tightest.
# The emitter's precedence-climbing printer uses this table; newr's
# own table (SPEC §9.2) maps onto it, with the traps documented
# where they bite - notably unary `-`: JS forbids `-x ** y`
# (SyntaxError), and `!a == b` is `(!a) == b` in JS, not
# `!(a == b)` as newr's `not` reads it.
_PREC = {
    "ternary": 0,
    "or": 1,
    "and": 2,
    "not": 3,
    "cmp": 4,      # == != < <= > >= in
    "add": 9,      # + -
    "mul": 10,     # * / %
    "unary": 11,   # -x !x (JS level; newr's operand rule differs)
    "pow": 12,     # **
    "primary": 13,  # calls, indexing, member access, atoms
}

_BINOP_PREC = {
    "||": 1, "&&": 2,
    "===": 4, "!==": 4, "<": 4, "<=": 4, ">": 4, ">=": 4, "in": 4,
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


def _needs_eq(t: Type | None) -> bool:
    """True when `==` on a value of this type needs structural
    equality (`rt.eq`) instead of `===`: lists, dicts, sets,
    Results, structs and enums (and nullable forms of them - `eq`
    handles `null` on either side). Primitives, functions, errors
    and Discord entities compare by identity (`===`)."""
    if t is None:
        return False
    if t.name in ("list", "dict", "set", "Result"):
        return True
    return False


# ---------------------------------------------------------------- scopes


@dataclass
class _Binding:
    """One visible name: its JavaScript rendering and its newr type.

    `js` is the JS name (possibly renamed); `interp` is the form
    used inside an interpolation (for handler bindings: the mention /
    name form); `value` is the form used as a value. For ordinary
    variables all three are the JS name. `kind` is one of
    "param" / "local" / "global-let" / "fn" / "struct" / "enum" /
    "module" / "store" / "handler" / "builtin" / "named-module" /
    "file-module". `module` is the real module name for a module
    binding (the `js` name is the alias when the import is aliased).
    """

    js: str
    type: Type | None = None
    kind: str = "local"
    interp: str | None = None
    module: str | None = None

    @property
    def value(self) -> str:
        return self.js

    def interp_form(self) -> str:
        return self.interp if self.interp is not None else self.js


class _JSEmitter:
    """State for one emitted file set (main file + its imports)."""

    def __init__(self, program: A.Program, *, main_path: str,
                 sources: dict[str, str], line_comments: bool = False):
        self.program = program
        self.main_path = main_path
        self.sources = sources
        self.line_comments = line_comments
        # Imports collected while emitting (per output file - reset
        # for each module file).
        self.uses_discord: bool = False
        self.discord_names: set[str] = set()
        self.uses_sqlite: bool = False
        self.rt_helpers: set[str] = set()
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
        # JS names already taken at module level (for collision
        # aware allocation in _bind_module_level).
        self._module_used: set[str] = set()
        # JS names already used in the current JS function scope
        # (for rename allocation in _bind_fresh). Reset per function.
        self._fn_used: set[str] = set()
        # Hoists for the statement being emitted: each is a list of
        # lines (base indent 0) to emit before the statement -
        # hoisted lambda consts.
        self._hoists: list[list[str]] | None = None
        self._hoist_counter = 0
        self._lambda_counter = 0
        self._match_counter = 0
        # Current function context (for `?` and contracts): a stack
        # of dicts {return_type, ensures, in_handler}.
        self._fn_ctx: list[dict] = []
        # Warnings for the caller (stderr): e.g. input() in a bot.
        self.warnings: list[str] = []
        # Whether the `_channelText` helper is needed (set when a
        # `{channel}` interpolation is emitted).
        self._used_channel_text = False
        # Whether the program being emitted has a bot block.
        self._is_bot = program.bot is not None
        # Whether a `log <expr>` statement was emitted (no setup
        # needed in JS - console is ready; the flag is kept for
        # symmetry with the Python emitter).
        self._used_log = False
        # --- bot-emitter state ---
        # Handler-context stack: one dict per handler body being
        # emitted (command / event / task / component / timeout).
        # Keys: "kind", "reply" (how a `reply` is answered),
        # "locale" (the expression for `t()`'s locale argument).
        self._handler_ctx_stack: list[dict] = []
        # Component rows collected while emitting handler bodies
        # (each a list of lines, inserted before the answer they
        # attach to - the JS twin of the Python emitter's D40).
        self._pending_components: list[dict] = []
        self._component_counter = 0
        # The last "answer" line (a Reply / Followup / Edit) emitted
        # in the block currently being built, for component
        # attachment: (index into the block's line list, kind).
        self._last_answer: tuple[int, str] | None = None
        # Counter for unique temp names within a file.
        self._temp_counter = 0

    # ------------------------------------------------------------ errors

    def _fail(self, msg: str, span=None):
        raise EmitterError(msg, span=span)

    def _type_of(self, expr: A.Expr) -> Type:
        """The checker's type for an expression, or a clean refusal.

        A `None` type only happens on an unchecked program (the CLI
        never emits one) or a checker gap. The message names the
        failure and does NOT send the user back to `newr check`:
        check passes on the programs that reach this path, so that
        advice would be a circle. The span is the expression itself.
        """
        t = getattr(expr, "expr_type", None)
        if t is None:
            self._fail(
                "an expression has no type: the emitter cannot handle "
                "it here",
                span=expr.span)
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
        """The JS name for a newr name, before collision checks:
        JS keywords and emitter-reserved names get a trailing
        underscore (D20, JS twin)."""
        if name in _JS_KEYWORDS or name in _EMITTER_RESERVED:
            return name + "_"
        return name

    def _bind(self, name: str, type: Type | None = None,
              kind: str = "local", interp: str | None = None) -> _Binding:
        """Bind `name` in the current scope.

        JS has real block scope (`let`/`const` in an `if` block do
        not clobber the outer name), so - unlike the Python emitter
        - shadowing an enclosing name needs NO renaming: the inner
        binding is a different variable, exactly as newr reads it.
        Only keyword / emitter-reserved mangling applies (D20).
        """
        js = self._mangle(name)
        binding = _Binding(js=js, type=type, kind=kind, interp=interp)
        self._scopes[-1][name] = binding
        return binding

    def _bind_fresh(self, name: str, type: Type | None = None,
                    kind: str = "local") -> _Binding:
        """Bind `name` in the current scope WITHOUT renaming against
        enclosing scopes - for function/lambda parameters, where
        shadowing an enclosing name is the correct semantics. Only
        the current function's used-name set is consulted, so two
        parameters mangling to the same JS name still get distinct
        names (duplicate parameters are a SyntaxError in strict
        mode)."""
        base = self._mangle(name)
        js = base
        n = 1
        while js in self._fn_used:
            js = f"{base}_{n}"
            n += 1
        self._fn_used.add(js)
        binding = _Binding(js=js, type=type, kind=kind)
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
        js = self._mangle(name)
        if js in self._module_used:
            n = 1
            while f"{js}_{n}" in self._module_used:
                n += 1
            js = f"{js}_{n}"
        self._module_used.add(js)
        binding = _Binding(js=js, type=type, kind=kind)
        self._scopes[0][name] = binding
        return binding

    # ------------------------------------------------------------ imports

    def _need_rt(self, helper: str):
        self.rt_helpers.add(helper)

    def _need_discord(self, name: str):
        self.uses_discord = True
        self.discord_names.add(name)

    def _need_sqlite(self):
        self.uses_sqlite = True

    def _render_imports(self) -> list[str]:
        """The require block: discord.js first (third-party), then
        node:sqlite (node builtin), then the runtime (first-party,
        one destructured require - helpers and classes together),
        then file modules. Within a group the names are sorted.
        The runtime is imported only when a helper is used
        (usage-tracked, Gleam-style - D4's JS twin)."""
        lines: list[str] = []
        if self.uses_discord:
            names = ", ".join(sorted(self.discord_names))
            lines.append(
                f"const {{ {names} }} = require(\"discord.js\");")
        if self.uses_sqlite:
            lines.append(
                "const { DatabaseSync } = require(\"node:sqlite\");")
        if self.rt_helpers:
            names = ", ".join(sorted(self.rt_helpers))
            lines.append(
                f"const {{ {names} }} = require(\"./newr_rt.js\");")
        for bound in sorted(self.imported_modules):
            stem = self.imported_modules[bound]
            lines.append(
                f"const {bound} = require(\"./{stem}.js\");")
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

    def _new_component_name(self) -> str:
        self._component_counter += 1
        return f"_newr_c{self._component_counter - 1}"

    # ------------------------------------------------------------ text

    def _line_comment(self, span: Span | None) -> str | None:
        """`// newr:<file>:<line>` for a span (only with --line-comments)."""
        if not self.line_comments or span is None:
            return None
        source = self.sources.get(span.file, "")
        try:
            line, _col = span.line_col(source)[:2]
        except Exception:
            return None
        return f"// newr:{span.file}:{line}"

    # ------------------------------------------------------------ expressions

    def emit_expr(self, node: A.Expr, min_prec: int = 0) -> str:
        """Emit an expression, parenthesised when its precedence is
        below `min_prec`."""
        text, prec = self._emit_expr(node)
        if prec < min_prec:
            return f"({text})"
        return text

    def _emit_expr(self, node: A.Expr) -> tuple[str, int]:
        if isinstance(node, A.Lit):
            return self._emit_lit(node)
        if isinstance(node, A.Name):
            return self._emit_name(node)
        if isinstance(node, A.Interp):
            return self._emit_interp(node)
        if isinstance(node, A.ListLit):
            if not node.elements:
                return "[]", _PREC["primary"]
            return (f"[{', '.join(self.emit_expr(i, 0) for i in node.elements)}]",
                    _PREC["primary"])
        if isinstance(node, A.DictLit):
            if not node.pairs:
                return "new Map()", _PREC["primary"]
            items = ", ".join(
                f"[{self.emit_expr(k, 0)}, {self.emit_expr(v, 0)}]"
                for k, v in node.pairs)
            return f"new Map([{items}])", _PREC["primary"]
        if isinstance(node, A.BinOp):
            return self._emit_binop(node)
        if isinstance(node, A.UnaryOp):
            return self._emit_unary(node)
        if isinstance(node, A.Lambda):
            return self._emit_lambda(node)
        if isinstance(node, A.Ternary):
            cond = self.emit_expr(node.cond, _PREC["or"])
            then = self.emit_expr(node.then_expr, 0)
            els = self.emit_expr(node.else_expr, 0)
            return f"{cond} ? {then} : {els}", _PREC["ternary"]
        if isinstance(node, A.Propagate):
            # `?` is statement-level (D7): the parser only creates it
            # as the outermost postfix of a statement RHS, so it is
            # handled by the statement emitter, never here.
            self._fail("`?` may only appear at the end of a statement "
                       "right-hand side (the emitter found it nested "
                       "inside an expression)")
        if isinstance(node, A.TryExpr):
            self._need_rt("tryResult")
            inner = self.emit_expr(node.expr, 0)
            return f"tryResult(() => {inner})", _PREC["primary"]
        if isinstance(node, A.TCall):
            return self._emit_t_call(node)
        if isinstance(node, A.EmbedExpr):
            return self._emit_embed(node)
        if isinstance(node, (A.Member, A.Index, A.Call)):
            # Any optional link anywhere in the chain routes the
            # WHOLE chain through _emit_chain (D9) - including
            # trailing non-optional links (`a?.b.c`), which must
            # short-circuit too.
            if self._chain_has_optional(node):
                return self._emit_chain(node)
            if isinstance(node, A.Member):
                return self._emit_member(node)
            if isinstance(node, A.Index):
                return self._emit_index(node)
            return self._emit_call(node)
        self._fail(f"internal error: cannot emit {type(node).__name__}")

    def _emit_lit(self, node: A.Lit) -> tuple[str, int]:
        value = node.value
        if value is True:
            return "true", _PREC["primary"]
        if value is False:
            return "false", _PREC["primary"]
        if value is None:
            return "null", _PREC["primary"]
        if isinstance(value, int):
            return f"{value}n", _PREC["primary"]
        if isinstance(value, float):
            # Python repr of a float is also a valid JS number
            # literal ("1e+16", "5e-324", "1.5"); the value is
            # checked int64-safe upstream - a float literal is never
            # the int64-minimum trap (that is an int literal).
            return repr(value), _PREC["primary"]
        if isinstance(value, str):
            return _js_str(value), _PREC["primary"]
        self._fail(f"internal error: cannot emit literal {value!r}")

    def _emit_name(self, node: A.Name) -> tuple[str, int]:
        binding = self._lookup(node.name)
        if binding is not None:
            if binding.kind == "store":
                self._fail(f"a store (`{node.name}`) cannot be used "
                           "as a value", span=node.span)
            if binding.kind in ("module", "file-module"):
                self._fail(
                    f"module `{node.name}` has no value member "
                    f"`{node.name}`", span=node.span)
            if binding.kind in ("struct", "enum"):
                self._fail(
                    f"`{node.name}` is not a value - use a variant "
                    "or construct it", span=node.span)
            # A user fn as a value is the function itself (first-
            # class functions, SPEC §9.5); a local/param/handler
            # binding is its JS name.
            return binding.value, _PREC["primary"]
        if node.name in _BUILTIN_NAMES:
            return self._emit_builtin_value(node.name)
        if node.name in ("math", "time", "random", "file", "env"):
            self._fail(
                f"module `{node.name}` has no value member `{node.name}`",
                span=node.span)
        if node.name == "discord":
            self._fail("module `discord` has no value member `discord`",
                       span=node.span)
        self._fail(f"cannot emit undefined name `{node.name}`",
                   span=node.span)

    def _emit_builtin_value(self, name: str) -> tuple[str, int]:
        # First-class builtin values: `len`/`min`/`max` as a value
        # are small lambdas (a JS `Math.min` does not take BigInts).
        if name == "len":
            return "((x) => x.length ?? x.size)", _PREC["primary"]
        if name == "min":
            return "((a, b) => a < b ? a : b)", _PREC["primary"]
        if name == "max":
            return "((a, b) => a > b ? a : b)", _PREC["primary"]
        if name == "abs":
            self._need_rt("checkInt")
            return ("((x) => typeof x === 'bigint' ? "
                    "checkInt(x < 0n ? -x : x) : Math.abs(x))",
                    _PREC["primary"])
        if name == "round":
            self._need_rt("roundHalfAway")
            return "roundHalfAway", _PREC["primary"]
        if name == "range":
            self._need_rt("range")
            return "range", _PREC["primary"]
        if name == "set":
            return "((xs) => new Set(xs))", _PREC["primary"]
        if name == "ok":
            self._need_rt("ok")
            return "ok", _PREC["primary"]
        if name == "err":
            self._need_rt("err")
            return "err", _PREC["primary"]
        self._fail(f"builtin `{name}` is not a value")

    # ------------------------------------------------------------ interpolation

    def _emit_interp(self, node: A.Interp) -> tuple[str, int]:
        """A newr interpolation becomes a JS template literal.
        Literal parts are escaped (`\\`` and `${`); each part is a
        `${...}` field. Unlike Python, no hoisting is needed for
        backslashes - they live inside the field expression, not in
        the literal text."""
        parts: list[str] = []
        for part in node.parts:
            if part.text is not None:
                parts.append(_js_tpl_text(part.text))
            else:
                parts.append("${" + self._emit_interp_part(part.expr) + "}")
        return "`" + "".join(parts) + "`", _PREC["primary"]

    def _emit_interp_part(self, expr: A.Expr) -> str:
        """One `${...}` field. Bools and floats render through
        `toStr` (Python-repr semantics, J4); ints and strs are bare
        (a BigInt interpolates as its digits; a str is itself);
        handler bindings use their interp form (the mention / name
        form); everything else goes through `toStr` (P8)."""
        if isinstance(expr, A.Name):
            binding = self._lookup(expr.name)
            if binding is not None and binding.interp is not None:
                form = binding.interp_form()
                if "_channelText" in form:
                    # The channel's mention form needs the helper.
                    self._used_channel_text = True
                return form
        t = self._type_of(expr)
        if _is_bool(t):
            self._need_rt("toStr")
            return f"toStr({self.emit_expr(expr, 0)})"
        if _is_float(t):
            self._need_rt("toStr")
            return f"toStr({self.emit_expr(expr, 0)})"
        if _is_int(t) or _is_str(t):
            return self.emit_expr(expr, 0)
        if t.nullable:
            self._need_rt("toStr")
            return f"toStr({self.emit_expr(expr, 0)})"
        if t.name in ("list", "dict", "set", "Result") or \
                t.name in self.structs or t.name in self.enums:
            self._need_rt("toStr")
            return f"toStr({self.emit_expr(expr, 0)})"
        return self.emit_expr(expr, 0)

    # ------------------------------------------------------------ operators

    def _emit_binop(self, node: A.BinOp) -> tuple[str, int]:
        op = node.op
        if op == "??":
            self._need_rt("coalesce")
            left = self.emit_expr(node.left, 0)
            right = self.emit_expr(node.right, 0)
            return f"coalesce({left}, () => {right})", _PREC["primary"]
        if op in ("and", "or"):
            lt = self._type_of(node.left)
            if op == "or" and lt.nullable:
                # `T? or T` (SPEC §7.2): the default when the value
                # is absent. JS `||` would also take the default
                # for a present-but-falsy value (0n, ""), so it is
                # not the same - use coalesce.
                self._need_rt("coalesce")
                left = self.emit_expr(node.left, 0)
                right = self.emit_expr(node.right, 0)
                return f"coalesce({left}, () => {right})", _PREC["primary"]
            prec = _BINOP_PREC["&&" if op == "and" else "||"]
            left = self.emit_expr(node.left, prec)
            right = self.emit_expr(node.right, prec + 1)
            return f"{left} {'&&' if op == 'and' else '||'} {right}", prec
        if op == "in":
            rt = self._type_of(node.right)
            left = self.emit_expr(node.left, _BINOP_PREC["<"])
            if rt.name == "str":
                right = self.emit_expr(node.right, _BINOP_PREC["<"] + 1)
                return f"{right}.includes({left})", _PREC["primary"]
            if rt.name == "list":
                self._need_rt("listContains")
                right = self.emit_expr(node.right, _BINOP_PREC["<"] + 1)
                return f"listContains({right}, {left})", _PREC["primary"]
            if rt.name in ("set", "dict"):
                right = self.emit_expr(node.right, _BINOP_PREC["<"] + 1)
                return f"{right}.has({left})", _PREC["primary"]
            self._fail(
                f"cannot emit `in` for a {rt.name} (the checker should "
                "have rejected this)")
        if op in ("==", "!=", "<", "<=", ">", ">="):
            prec = _BINOP_PREC["===" if op == "==" else
                               "!==" if op == "!=" else op]
            lt = self._type_of(node.left)
            rt = self._type_of(node.right)
            left = self.emit_expr(node.left, prec)
            right = self.emit_expr(node.right, prec + 1)
            jsop = {"==": "===", "!=": "!==", "<": "<",
                    "<=": "<=", ">": ">", ">=": ">="}[op]
            if op in ("==", "!=") and \
                    (_needs_eq(lt) or _needs_eq(rt) or
                     lt.name in self.structs or lt.name in self.enums or
                     rt.name in self.structs or rt.name in self.enums):
                # Structural equality (J6): `eq` is deep for
                # arrays/Maps/Sets/Results/structs, and treats `null`
                # as equal only to `null` - exactly newr's `==` for
                # nullable collection values too.
                self._need_rt("eq")
                expr = f"eq({left}, {right})"
                if op == "!=":
                    expr = f"!{expr}"
                return expr, _PREC["primary"]
            return f"{left} {jsop} {right}", prec
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
                      "%": "tmod", "**": "powInt"}[op]
            self._need_rt(helper)
            return f"{helper}({left}, {right})", _PREC["primary"]
        if op == "%" and _is_float(result):
            self._need_rt("fmod")
            return f"fmod({left}, {right})", _PREC["primary"]
        if op == "/" and _is_float(result):
            # P6: float /0 is an error (JS `/` would give Infinity).
            self._need_rt("fdiv")
            return f"fdiv({left}, {right})", _PREC["primary"]
        if op == "**" and _is_float(result):
            self._need_rt("powFloat")
            return f"powFloat({left}, {right})", _PREC["primary"]
        return f"{left} {op} {right}", prec

    def _emit_unary(self, node: A.UnaryOp) -> tuple[str, int]:
        if node.op == "not":
            # JS `!` binds tighter than newr's `not` (which is
            # looser than `==`): `!a == b` is `(!a) == b` in JS.
            # Parenthesise any operand that is not a primary, so
            # `not a == b` reads `!(a === b)` as newr reads it.
            text, prec = self._emit_expr(node.operand)
            if prec < _PREC["primary"]:
                return f"!({text})", _PREC["primary"]
            return f"!{text}", _PREC["unary"]
        # `-`: the operand binds at `**` level (the newr trap:
        # `-a + b` is `-(a + b)`).
        # D19: the int64 minimum is written `-9223372036854775808`
        # in newr. Its positive magnitude is out of int64 range, so
        # the checker leaves the literal's own type unset - the
        # plain `neg(...)` form would also be wrong (it would check
        # the out-of-range literal first). Emit the minimum directly.
        if isinstance(node.operand, A.Lit) and \
                node.operand.value == 9223372036854775808:
            return "-9223372036854775808n", _PREC["unary"]
        t = self._type_of(node.operand)
        if _is_int(t):
            self._need_rt("neg")
            return f"neg({self.emit_expr(node.operand, 0)})", \
                _PREC["primary"]
        text, _p = self._emit_expr(node.operand)
        operand = self.emit_expr(node.operand, _PREC["pow"])
        # JS traps: `-x ** y` is a SyntaxError, and `--x` is the
        # decrement operator - parenthesise a `**` operand or a
        # unary operand (`-(a ** 2)`, `-(-x)`).
        if (isinstance(node.operand, A.BinOp) and node.operand.op == "**") \
                or isinstance(node.operand, A.UnaryOp):
            return f"-({operand})", _PREC["primary"]
        return f"-{operand}", _PREC["unary"]

    # ------------------------------------------------------------ calls

    def _emit_call(self, node: A.Call) -> tuple[str, int]:
        func = node.func
        if isinstance(func, A.Name):
            return self._emit_name_call(func.name, node)
        if isinstance(func, A.Member):
            obj = func.obj
            if isinstance(obj, A.Name):
                binding = self._lookup(obj.name)
                if binding is not None and binding.kind in (
                        "module", "file-module"):
                    return self._emit_module_call(node, binding)
                if binding is None and obj.name in MODULES:
                    # A named module used without an import
                    # (N-09/N-10): the name is both the real module
                    # and the reference.
                    fake = _Binding(js=obj.name, kind="module",
                                    module=obj.name)
                    return self._emit_module_call(node, fake)
                if binding is not None and binding.kind == "store":
                    return self._emit_store_call(node, binding)
                if obj.name == "log" and func.name in ("info", "warn",
                                                       "error"):
                    return self._emit_log_call(func.name, node)
                if binding is not None and binding.kind == "enum":
                    return self._emit_enum_call(obj.name, func.name, node)
            # §20: qualified imported fielded-variant construction,
            # `m_helper.Shape.Circle(radius: 1.5)` - the declaration
            # comes from the imported module, for field reordering.
            imported = self._imported_enum(func)
            if imported is not None:
                ref, enum_name, variant, decl = imported
                return self._emit_enum_call(enum_name, variant, node,
                                            decl=decl, ref=ref)
            return self._emit_method_call(node, func)
        # A call on an arbitrary expression (a lambda, a call result).
        f = self.emit_expr(func, _PREC["primary"])
        args = [self.emit_expr(a, 0) for a in node.args]
        return f"{f}({', '.join(args)})", _PREC["primary"]

    def _emit_name_call(self, name: str,
                        node: A.Call) -> tuple[str, int]:
        if node.kwargs and name not in self.structs:
            self._fail(f"`{name}` takes no named arguments")
        if name == "print":
            return self._emit_print(node)
        if name == "input":
            if self._is_bot:
                self.warnings.append(
                    "input() in a bot program blocks on stdin - bots "
                    "have no stdin (D21)")
            self._need_rt("input")
            args = [self.emit_expr(a, 0) for a in node.args]
            return f"input({', '.join(args)})", _PREC["primary"]
        if name == "len":
            return self._emit_builtin_call(node, None)
        if name in ("str", "int", "float", "bool"):
            return self._emit_conversion(node)
        if name in ("min", "max"):
            return self._emit_builtin_call(node, None)
        if name == "abs":
            return self._emit_builtin_call(node, None)
        if name == "round":
            self._need_rt("roundHalfAway")
            args = [self.emit_expr(a, 0) for a in node.args]
            return f"roundHalfAway({', '.join(args)})", _PREC["primary"]
        if name == "range":
            self._need_rt("range")
            args = [self.emit_expr(a, 0) for a in node.args]
            return f"range({', '.join(args)})", _PREC["primary"]
        if name == "set":
            arg = self.emit_expr(node.args[0], 0)
            return f"new Set({arg})", _PREC["primary"]
        if name == "ok":
            self._need_rt("ok")
            return f"ok({self.emit_expr(node.args[0], 0)})", \
                _PREC["primary"]
        if name == "err":
            self._need_rt("err")
            return f"err({self.emit_expr(node.args[0], 0)})", \
                _PREC["primary"]
        binding = self._lookup(name)
        if binding is not None and binding.kind == "struct":
            # Struct construction: JS has no kwargs, so keyword
            # arguments are reordered into declared field order
            # (the checker already validated names and types).
            decl = self.structs.get(name)
            if decl is None:
                self._fail(f"internal error: unknown struct `{name}`")
            order = [f.name for f in decl.fields]
            values: dict[str, str] = {}
            for a, f in zip(node.args, order):
                values[f] = self.emit_expr(a, 0)
            for kname, value, _span in node.kwargs:
                values[kname] = self.emit_expr(value, 0)
            parts = [values[f] for f in order if f in values]
            if len(parts) != len(order):
                self._fail(
                    f"internal error: missing field in `{name}` "
                    "construction")
            return (f"new {binding.js}({', '.join(parts)})",
                    _PREC["primary"])
        if binding is not None:
            args = [self.emit_expr(a, 0) for a in node.args]
            kwargs = self._emit_kwargs(node, binding)
            return (f"{binding.js}({', '.join(args + kwargs)})",
                    _PREC["primary"])
        self._fail(f"unknown function `{name}`")

    def _emit_kwargs(self, node: A.Call, binding: _Binding) -> list[str]:
        """Keyword arguments for a user-fn call: JS has no kwargs,
        so they are reordered into position (the checker already
        validated names and types)."""
        if not node.kwargs:
            return []
        fn = self.fns_by_js.get(binding.js)
        if fn is None:
            self._fail(
                "internal error: cannot find fn "
                f"`{binding.js}` for keyword arguments")
        order = [p.name for p in fn.params]
        values: list[str] = [""] * len(order)
        for kname, value, _span in node.kwargs:
            idx = order.index(kname)
            values[idx] = self.emit_expr(value, 0)
        if any(v == "" for v in values):
            self._fail(
                "internal error: missing keyword argument in call")
        return values

    def _emit_print(self, node: A.Call) -> tuple[str, int]:
        """`print` renders every argument with `toStr` (P8) - the
        runtime `print` helper does that mapping internally, so the
        emitted call is bare. A `?` argument propagates first (the
        temp + early return are hoisted before the statement, D7);
        the Python emitter's `_emit_print` calls a
        `_propagate_early_return` that does not exist (a latent M2
        bug - unreachable via the parser, which only creates
        Propagate as a statement RHS); the JS emitter uses the
        working `_propagate_unwrapped` instead. When every
        argument is already a JS string (or there are no
        arguments), `console.log` prints them exactly as the
        runtime helper would (space-separated, no formatting), so
        no runtime import is needed for a plain `print \"...\"`."""
        parts = []
        all_str = True
        for arg in node.args:
            if isinstance(arg, A.Propagate):
                parts.append(self._propagate_unwrapped(arg))
                all_str = False
                continue
            t = self._type_of(arg)
            if not _is_str(t):
                all_str = False
            parts.append(self.emit_expr(arg, 0))
        if all_str:
            return f"console.log({', '.join(parts)})", _PREC["primary"]
        self._need_rt("print")
        return f"print({', '.join(parts)})", _PREC["primary"]

    def _emit_builtin_call(self, node: A.Call,
                           binding: _Binding | None) -> tuple[str, int]:
        name = node.func.name if isinstance(node.func, A.Name) else None
        args = node.args
        if name == "len":
            t = self._type_of(args[0])
            arg = self.emit_expr(args[0], 0)
            # `len` returns an int (BigInt). A str's length is its
            # code-point count (P5), not its UTF-16 length.
            if t.name == "str":
                return f"BigInt([...{arg}].length)", _PREC["primary"]
            if t.name == "list":
                return f"BigInt({arg}.length)", _PREC["primary"]
            if t.name in ("dict", "set"):
                return f"BigInt({arg}.size)", _PREC["primary"]
            return f"BigInt({arg}.length)", _PREC["primary"]
        if name == "min":
            a = self.emit_expr(args[0], 0)
            b = self.emit_expr(args[1], 0)
            return f"({a} < {b} ? {a} : {b})", _PREC["primary"]
        if name == "max":
            a = self.emit_expr(args[0], 0)
            b = self.emit_expr(args[1], 0)
            return f"({a} > {b} ? {a} : {b})", _PREC["primary"]
        if name == "abs":
            t = self._type_of(args[0])
            arg = self.emit_expr(args[0], 0)
            if _is_int(t):
                self._need_rt("checkInt")
                return f"checkInt({arg} < 0n ? -{arg} : {arg})", \
                    _PREC["primary"]
            return f"Math.abs({arg})", _PREC["primary"]
        self._fail(f"internal error: cannot emit builtin `{name}`")

    def _emit_conversion(self, node: A.Call) -> tuple[str, int]:
        name = node.func.name  # str | int | float | bool
        arg = node.args[0]
        t = self._type_of(arg)
        helper = {"str": "toStr", "int": "toInt", "float": "toFloat",
                  "bool": "toBool"}[name]
        text = self.emit_expr(arg, 0)
        if name == "str":
            # A nullable str's runtime value is null when absent -
            # bare rendering would print "null", not newr's "none"
            # (P8), so nullable values go through toStr.
            if _is_str(t) and not t.nullable:
                return text, _PREC["primary"]
            self._need_rt("toStr")
            return f"toStr({text})", _PREC["primary"]
        if name == "int":
            if _is_int(t):
                return text, _PREC["primary"]
            # float (truncation), str (parsing), bool (0/1) - all
            # through toInt, which returns a BigInt in every case.
            self._need_rt("toInt")
            return f"toInt({text})", _PREC["primary"]
        if name == "float":
            if _is_float(t):
                return text, _PREC["primary"]
            if _is_str(t):
                self._need_rt("toFloat")
                return f"toFloat({text})", _PREC["primary"]
            return f"Number({text})", _PREC["primary"]  # int/bool
        # bool
        if _is_bool(t):
            return text, _PREC["primary"]
        if _is_str(t):
            self._need_rt("toBool")
            return f"toBool({text})", _PREC["primary"]
        return f"Boolean({text})", _PREC["primary"]  # int -> 0 is false (P7)

    def _emit_enum_call(self, enum_name: str, variant: str,
                        node: A.Call, decl=None,
                        ref: str | None = None) -> tuple[str, int]:
        """`Shape.Circle(radius: 1.5)` -> the variant class. JS has
        no kwargs, so keyword arguments are reordered into declared
        field order (the checker already validated them). `decl` /
        `ref` serve the §20 qualified imported form
        (`m_helper.Shape.Circle(...)`): the declaration comes from
        the imported module and the class lives under the module
        reference; the local form takes both from this file."""
        js_variant = (f"{ref + '.' if ref else ''}"
                      f"{self._mangle(enum_name)}_"
                      f"{self._mangle(variant)}")
        decl = decl if decl is not None else self.enums.get(enum_name)
        if decl is not None:
            fields = []
            for var in decl.variants:
                if var.name == variant:
                    fields = [f.name for f in var.fields]
                    break
            values: dict[str, str] = {}
            for a, f in zip(node.args, fields):
                values[f] = self.emit_expr(a, 0)
            for kname, value, _span in node.kwargs:
                values[kname] = self.emit_expr(value, 0)
            parts = [values[f] for f in fields if f in values]
            return (f"new {js_variant}({', '.join(parts)})",
                    _PREC["primary"])
        args = [self.emit_expr(a, 0) for a in node.args]
        return f"new {js_variant}({', '.join(args)})", _PREC["primary"]

    def _emit_log_call(self, level: str, node: A.Call) -> tuple[str, int]:
        """`log.info/warn/error(msg)` (P10). In a bot program the
        message goes to the console (J12 - the JS twin of the bot's
        named logger); in a general program it is the root console
        too (console.info/warn/error). `warn` is `console.warn` -
        never the deprecated `console.warn` alias confusion: the
        mapping is info -> info, warn -> warn, error -> error."""
        msg = self.emit_expr(node.args[0], 0)
        return f"console.{level}({msg})", _PREC["primary"]

    def _imported_enum(self, node: A.Member):
        """A qualified imported enum member, `module.Enum.Variant`.

        Returns (ref, enum_name, variant, decl) when the chain is
        exactly that shape, the inner name is an enum declared in
        the imported module, else None. The decl check matters: a
        struct-typed top-level let accessed the same way
        (`m_helper.SomeLet.field`) is NOT an enum member and must
        fall through to the ordinary typed-member path. The decl is
        returned too, so a fielded-variant call can reorder its
        keyword arguments into declared field order (JS has no
        kwargs). The Python twin returns the same triple without
        the decl.
        """
        obj = node.obj
        if not isinstance(obj, A.Member) or not isinstance(obj.obj, A.Name):
            return None
        binding = self._lookup(obj.obj.name)
        if binding is None or binding.kind != "file-module":
            return None
        sub = self.program.modules.get(binding.js)
        if sub is None:
            return None
        decl = next((e for e in sub.enums if e.name == obj.name), None)
        if decl is None:
            return None
        return (binding.js, obj.name, node.name, decl)

    def _emit_member(self, node: A.Member) -> tuple[str, int]:
        obj = node.obj
        if isinstance(obj, A.Name):
            binding = self._lookup(obj.name)
            if binding is not None:
                if binding.kind in ("module", "file-module"):
                    return self._emit_module_member(node, binding)
                if binding.kind == "enum":
                    return self._emit_enum_variant(node, binding)
                if binding.kind == "struct":
                    self._fail(
                        f"struct `{obj.name}` is not a value - construct it",
                        span=node.span)
                if binding.kind == "store":
                    self._fail(
                        f"store `{obj.name}` is not a value - use its "
                        "economy functions", span=node.span)
                if binding.kind == "fn":
                    self._fail(
                        f"function `{obj.name}` is not a value - call it",
                        span=node.span)
            if obj.name in MODULES:
                # A named module used without an import: the name is
                # both the real module and the reference.
                fake = _Binding(js=obj.name, kind="module",
                                module=obj.name)
                return self._emit_module_member(node, fake)
        # §20: qualified imported enum member, `m_helper.Shade.Red`.
        # The inner `m_helper.Shade` has no type of its own (the
        # checker types only the full chain), so resolve the whole
        # chain here from the imported module's enum declaration.
        imported = self._imported_enum(node)
        if imported is not None:
            ref, enum_name, variant, _decl = imported
            js_variant = (f"{ref}.{self._mangle(enum_name)}_"
                          f"{self._mangle(variant)}")
            return f"new {js_variant}()", _PREC["primary"]
        obj_type = self._type_of(obj)
        t = self._type_of(node)
        # §16 (hole 2): a method used as a value is a lambda, not a
        # JS attribute - a BigInt has no `is_some`, an Array has no
        # `len`. A real struct field stays a plain attribute.
        if t.name == "fn" and not self._is_plain_field(obj_type,
                                                       node.name):
            sig = self._method_value_sig(obj_type, node.name)
            if sig is not None:
                return self._emit_method_value(node, obj_type, sig)
        obj_text = self.emit_expr(obj, _PREC["primary"])
        return f"{obj_text}.{self._mangle(node.name)}", _PREC["primary"]

    def _emit_math_member(self, node: A.Member) -> tuple[str, int]:
        if node.name in ("pi", "e"):
            return ("Math.PI" if node.name == "pi" else "Math.E",
                    _PREC["primary"])
        self._fail(f"math has no member `{node.name}`")

    def _emit_enum_variant(self, node: A.Member,
                           binding: _Binding) -> tuple[str, int]:
        enum_name = binding.js
        variant = f"{enum_name}_{node.name}"
        return f"new {variant}()", _PREC["primary"]

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
        inside an `optChain` lambda (D38) - the receiver is proven
        non-none there, so "T" is the receiver itself."""
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
        field) - a plain attribute access, never a method value.
        Provider/entity types have no tables here, so they report
        False and the method-signature lookup decides."""
        decl = self.structs.get(recv_type.name)
        if decl is not None:
            return any(f.name == name for f in decl.fields)
        return False

    def _emit_method_value(self, node: A.Member, obj_type: Type,
                           sig) -> tuple[str, int]:
        """A method used as a value (§16, hole 2): a lambda taking
        the method's parameters, capturing the receiver by value
        (D10). The receiver is snapshotted through an IIFE (J8):
        `((_r) => (a0, ...) => <call>)(obj)`, so the receiver
        expression is evaluated once, when the lambda is created,
        never again at call time. The body is the ordinary method
        call with the receiver replaced by the snapshot name."""
        params, ret = sig
        obj_text = self.emit_expr(node.obj, 0)
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
        arg_names = ", ".join(b.js for b in arg_bindings)
        body, hoists = self._emit_expr_with_hoists(call)
        self._pop_scope()
        arrow = f"({recv.js}) => ({arg_names}) => {body}"
        if hoists:
            # A hoisted block-body lambda: the hoisted const's
            # initializer is the IIFE, and the hoists run inside the
            # arrow body at call time.
            name = self._new_lambda_name()
            lines = [f"const {name} = (({recv.js}) => "
                     f"({arg_names}) => {{"]
            for h in hoists:
                lines.append("  " + h)
            lines.append(f"  return {body};")
            lines.append(f"}})({obj_text});")
            self._hoists_add(lines)
            return name, _PREC["primary"]
        return f"({arrow})({obj_text})", _PREC["primary"]

    def _hoists_add(self, lines: list[str]):
        if self._hoists is not None:
            self._hoists.append(lines)

    def _emit_expr_with_hoists(self, expr: A.Expr) -> tuple[str, list[str]]:
        """Emit an expression, collecting any hoists it produces
        (hoisted lambda consts) and returning them separately."""
        saved = self._hoists
        self._hoists = []
        text, _prec = self._emit_expr(expr)
        hoists = self._hoists
        self._hoists = saved
        flat: list[str] = []
        for lines in hoists:
            flat.extend(lines)
        return text, flat

    # ------------------------------------------------------------ method calls

    def _emit_method_call(self, node: A.Call, func: A.Member) -> tuple[str, int]:
        """Method calls, mirroring the Python emitter's tables
        exactly (SPEC §12 + METHODS in compiler/modules.py). Only
        the methods the checker allows are reachable here."""
        obj = func.obj
        obj_type = self._type_of(obj)
        if node.kwargs:
            self._fail(f"method `.{func.name}` takes no named arguments")
        args = [self.emit_expr(a, 0) for a in node.args]
        obj_text = self.emit_expr(obj, _PREC["primary"])
        name = func.name
        if obj_type.nullable:
            # Nullable methods only (is_some / is_none / unwrap). A
            # `?.` call on a nullable was handled by the chain
            # collector, so func.optional is False here.
            if name == "is_some":
                return f"({obj_text} !== null)", _PREC["primary"]
            if name == "is_none":
                return f"({obj_text} === null)", _PREC["primary"]
            if name == "unwrap":
                self._need_rt("unwrapOpt")
                return f"unwrapOpt({obj_text})", _PREC["primary"]
            self._fail(f"`{obj_type}` has no method `{name}`")
        tname = obj_type.name
        if tname == "str":
            if name == "upper":
                return f"{obj_text}.toUpperCase()", _PREC["primary"]
            if name == "lower":
                return f"{obj_text}.toLowerCase()", _PREC["primary"]
            if name == "strip":
                return f"{obj_text}.trim()", _PREC["primary"]
            if name == "split":
                return f"{obj_text}.split({args[0]})", _PREC["primary"]
            if name == "join":
                # newr `"sep".join(parts)` - JS joins the ARRAY
                # with the separator.
                return f"{args[0]}.join({obj_text})", _PREC["primary"]
            if name == "contains":
                return f"{obj_text}.includes({args[0]})", _PREC["primary"]
            if name == "starts_with":
                return f"{obj_text}.startsWith({args[0]})", _PREC["primary"]
            if name == "ends_with":
                return f"{obj_text}.endsWith({args[0]})", _PREC["primary"]
            if name == "chars":
                # SPEC pin P5: code points, not UTF-16 code units.
                return f"[...{obj_text}]", _PREC["primary"]
        elif tname == "list":
            if name == "append":
                return f"{obj_text}.push({args[0]})", _PREC["primary"]
            if name == "contains":
                self._need_rt("listContains")
                return f"listContains({obj_text}, {args[0]})", \
                    _PREC["primary"]
            if name == "get":
                self._need_rt("listGet")
                if len(args) == 2:
                    return (f"listGet({obj_text}, {args[0]}, {args[1]})",
                            _PREC["primary"])
                return f"listGet({obj_text}, {args[0]})", _PREC["primary"]
            if name == "len":
                return f"BigInt({obj_text}.length)", _PREC["primary"]
        elif tname == "dict":
            if name == "keys":
                return f"[...{obj_text}.keys()]", _PREC["primary"]
            if name == "get":
                if len(args) == 2:
                    return (f"({obj_text}.has({args[0]}) ? "
                            f"{obj_text}.get({args[0]}) : {args[1]})",
                            _PREC["primary"])
                return f"{obj_text}.get({args[0]})", _PREC["primary"]
            if name == "contains":
                return f"{obj_text}.has({args[0]})", _PREC["primary"]
        elif tname == "set":
            if name == "add":
                return f"{obj_text}.add({args[0]})", _PREC["primary"]
            if name == "remove":
                self._need_rt("setRemove")
                return f"setRemove({obj_text}, {args[0]})", _PREC["primary"]
            if name == "contains":
                return f"{obj_text}.has({args[0]})", _PREC["primary"]
        elif tname == "Result":
            if name in ("is_ok", "is_err"):
                # The JS Result exposes isOk/isErr as METHODS
                # (newr_rt.js) - a bare access would be the function
                # itself (whose String() is its source), not the bool.
                method = "isOk" if name == "is_ok" else "isErr"
                return f"{obj_text}.{method}()", _PREC["primary"]
            if name == "unwrap":
                return f"{obj_text}.unwrap()", _PREC["primary"]
            if name == "unwrap_or":
                return f"{obj_text}.unwrapOr({args[0]})", _PREC["primary"]
        # Rebased `?.` chains (D38): inside an `optChain` lambda the
        # receiver is the STRIPPED inner type, so a nullable method
        # (`is_some` / `is_none` / `unwrap`) reaches here on a
        # non-nullable, non-Result receiver. The value is proven
        # non-none inside the lambda, so the answer is constant
        # (true / false / the value itself). The checker rejects
        # these calls on a plain value, so they can only come from
        # a rebased chain.
        if name == "is_some":
            return "true", _PREC["primary"]
        if name == "is_none":
            return "false", _PREC["primary"]
        if name == "unwrap":
            return obj_text, _PREC["primary"]
        self._fail(
            f"`{obj_type}` has no method `{name}` (the checker should "
            "have rejected this)")

    # ------------------------------------------------------------ index

    def _emit_index(self, node: A.Index) -> tuple[str, int]:
        obj_type = self._type_of(node.obj)
        obj_text = self.emit_expr(node.obj, _PREC["primary"])
        idx = self.emit_expr(node.index, 0)
        if obj_type.name == "dict":
            return f"{obj_text}.get({idx})", _PREC["primary"]
        if obj_type.name == "list":
            self._need_rt("listIndex")
            # Indexing is the checked form: a negative or out-of
            # range index raises IndexError (rt listIndex), exactly
            # like newr's `xs[i]`. (`list.get` is the defaulting
            # form - see _emit_method_call.)
            return f"listIndex({obj_text}, {idx})", _PREC["primary"]
        if obj_type.name == "str":
            # newr indexes a string by code point (P5); a JS
            # string index is a UTF-16 code unit. `[...s][i]` is
            # the code-point-correct form.
            return f"[...{obj_text}][Number({idx})]", _PREC["primary"]
        self._fail(f"cannot index a {obj_type.name}")

    # ------------------------------------------------------------ chains

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

    def _emit_chain(self, node: A.Expr) -> tuple[str, int]:
        """`?.` lowering (D9, corrected - the JS twin of the Python
        emitter's `_emit_chain`): one `optChain` call per optional
        link, nested; non-optional links after an optional one fold
        into the nearest lambda body. `a?.b.c` is one call;
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
            # (optChain only calls it then), so the parameter and
            # the rebased base carry the STRIPPED (inner) type -
            # otherwise a method call on it would dispatch as if the
            # value were still nullable.
            inner_type = Type(recv_type.name, params=recv_type.params,
                              nullable=False)
            # §16 (hole 1): a segment must also end before any link
            # whose receiver can be `none` INSIDE the lambda - a
            # nullable field (`o?.inner.name`) or a nullable method
            # result (`r?.unwrap().upper()` on `Result[str?, _]?`).
            # The checker's `expr_type` conflates the `?.`
            # short-circuit with a field's own nullability, so the
            # in-lambda types are walked link by link instead.
            # `in_lambda` maps each link in this segment to its
            # in-lambda result type, so `_rebase` types the clones
            # exactly: a Member link's nullability inside the lambda
            # is its FIELD's nullability, not the checker's chain
            # type - without this, a method after a nullable field
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
            binding = self._bind_fresh("_v", type=inner_type,
                                       kind="param")
            # The rebased base is a Name for the NEWR name `_v` -
            # the lookup returns the binding's JS name (which may be
            # `_v_1` in a nested chain).
            new_base = A.Name(span=links[i].span, name="_v",
                              expr_type=inner_type)
            body_node = self._rebase(seg_end, inner_recv, new_base,
                                     in_lambda)
            self._need_rt("optChain")
            body = self._callable_body(body_node,
                                       f"({binding.js}) => ")
            self._pop_scope()
            cur_text = f"optChain({cur_text}, {body})"
            i = j
        return cur_text, _PREC["primary"]

    def _callable_body(self, expr: A.Expr, prefix: str) -> str:
        """Emit `expr` as an arrow-function body with the given
        prefix (`(v) => `). If the body needs hoists (a nested
        block-body lambda), the arrow becomes a hoisted `const` with
        a block body and its name is returned instead, so the hoisted
        statements run at call time, inside the function, not at the
        enclosing statement. (The JS twin of the Python emitter's
        `_callable_body`.)"""
        saved = self._hoists
        self._hoists = []
        body = self.emit_expr(expr, 0)
        hoists = self._hoists
        self._hoists = saved
        if not hoists:
            return prefix + body
        name = self._new_lambda_name()
        sig = prefix[:-4]  # strip the trailing " => "
        lines = [f"const {name} = {sig} => {{"]
        for h in hoists:
            lines.extend("  " + line for line in h)
        lines.append(f"  return {body};")
        lines.append("};")
        self._hoists.append(lines)
        return name

    def _rebase(self, node, old_base, new_base, in_lambda_types=None):
        """Clone a Member/Index/Call chain, replacing the subtree
        `old_base` (by identity) with `new_base`, and clearing the
        `optional` flags (the optionality is handled by the
        `optChain` wrapper, not inside the lambda body). Types are
        copied onto the clones - the checker already typed the
        original nodes - except when `in_lambda_types` maps a link
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
            # is nullable only because the `?.` short-circuits -
            # inside the lambda the receiver is proven non-none, so
            # the result is the plain (stripped) type. Without this,
            # a later link (`.upper()` in `s?.unwrap().upper()`)
            # dispatches against the nullable type and fails with
            # E4001. Only Call links are stripped: a Member link
            # (`a?.b`) may be nullable because the FIELD itself is
            # nullable, which survives inside the lambda. When the
            # caller supplies the exact in-lambda type (§16), that
            # wins over this heuristic - it is exact where D38
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

        Inside an `optChain` lambda the receiver is proven
        non-none, so this is the link's result type with the `?.`
        short-circuit removed - but a nullable FIELD type or a
        nullable method return survives. Returns None when the type
        cannot be determined (provider/entity types); the caller
        treats None as nullable and splits the segment, which is
        always safe."""
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
            # A field on a provider/entity type (e.g. a Discord
            # member's `name`): the type is unknown - treat as
            # nullable (split the segment).
            return None
        if isinstance(link, A.Index):
            if recv_type.name == "list" and recv_type.params:
                return recv_type.params[0]
            if recv_type.name == "dict" and len(recv_type.params) == 2:
                return recv_type.params[1]
            if recv_type.name == "str":
                return Type("str")
            return None
        if isinstance(link, A.Call) and isinstance(link.func, A.Member):
            sig = self._method_value_sig(recv_type, link.func.name)
            if sig is None:
                return None
            _params, ret = sig
            return ret
        return None

    # ------------------------------------------------------------ lambdas

    def _emit_lambda(self, node: A.Lambda) -> tuple[str, int]:
        """Closures capture by value (D10, J8): free variables are
        snapshotted through an IIFE - `((_s0, _s1) => (params) =>
        body)(v0, v1)` - because a JS default parameter evaluates at
        CALL time and refers to the parameter itself, so Python's
        default-arg snapshot trick does not work in JS. A block body
        cannot be an arrow function body with statements inline, so
        it is hoisted to a `const` immediately before the enclosing
        statement."""
        free = self._lambda_free_vars(node)
        self._push_scope()
        saved_used = self._fn_used
        self._fn_used = set()
        param_bindings = [self._bind_fresh(p.name, type=p.type,
                                           kind="param")
                          for p in node.params]
        # Snapshot parameters: one per free variable, named after the
        # enclosing binding's JS name (so the IIFE argument reads the
        # enclosing value), renamed only on a collision with a
        # declared parameter or another snapshot parameter.
        used_names = {b.js for b in param_bindings}
        snap: list[tuple[str, str, str]] = []  # (newr name, param, rhs)
        for name, b in free:
            # The snapshot parameter is named after the enclosing
            # binding's JS name - unless that is not a valid
            # identifier (a handler binding's value form is an
            # expression, e.g. `interaction.user`), in which case
            # the mangled newr name is the parameter and the value
            # form is the argument.
            js = b.js if b.js.isidentifier() else self._mangle(name)
            if js in used_names:
                n = 1
                while f"{js}_{n}" in used_names:
                    n += 1
                js = f"{js}_{n}"
            used_names.add(js)
            self._fn_used.add(js)
            self._scopes[-1][name] = _Binding(js=js, type=b.type,
                                              kind="param")
            snap.append((name, js, b.value))
        params_text = ", ".join(b.js for b in param_bindings)
        if node.expr_body is not None:
            body, hoists = self._emit_expr_with_hoists(node.expr_body)
            self._fn_used = saved_used
            self._pop_scope()
            if not hoists:
                arrow = f"({params_text}) => {body}"
                if not snap:
                    return arrow, _PREC["primary"]
                snap_params = ", ".join(py for _n, py, _r in snap)
                snap_args = ", ".join(rhs for _n, _p, rhs in snap)
                return (f"(({snap_params}) => {arrow})({snap_args})",
                        _PREC["primary"])
            # The body needed hoists (a nested block-body lambda):
            # the hoisted const's initializer IS the IIFE, so the
            # snapshot parameters are in scope inside the arrow body
            # at call time. (Wrapping the const NAME in an IIFE
            # would not work - the arrow is defined at statement
            # level, outside the IIFE.)
            name = self._new_lambda_name()
            if snap:
                snap_params = ", ".join(py for _n, py, _r in snap)
                snap_args = ", ".join(rhs for _n, _p, rhs in snap)
                lines = [f"const {name} = (({snap_params}) => "
                         f"({params_text}) => {{"]
            else:
                lines = [f"const {name} = ({params_text}) => {{"]
            for h in hoists:
                lines.append("  " + h)
            lines.append(f"  return {body};")
            if snap:
                lines.append(f"}})({snap_args});")
            else:
                lines.append("};")
            self._hoists_add(lines)
            return name, _PREC["primary"]
        # Block body: hoist a const arrow.
        name = self._new_lambda_name()
        self._fn_ctx.append({"return_type": node.return_type,
                             "ensures": [], "in_handler": False})
        body_lines = self._emit_block(node.body or [], 1)
        self._fn_ctx.pop()
        self._fn_used = saved_used
        self._pop_scope()
        if snap:
            snap_params = ", ".join(py for _n, py, _r in snap)
            snap_args = ", ".join(rhs for _n, _p, rhs in snap)
            lines = [f"const {name} = (({snap_params}) => "
                     f"({params_text}) => {{"]
        else:
            lines = [f"const {name} = ({params_text}) => {{"]
        lines.extend(body_lines if body_lines else ["  ;"])
        lines.append("};")
        if snap:
            lines[-1] = f"}})({snap_args});"
        self._hoists_add(lines)
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
            # value - capture by value (D10) requires a snapshot.
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

    # ------------------------------------------------------------ match (D12)

    def _emit_match(self, stmt: A.Match, indent: int) -> list[str]:
        p = "  " * indent
        temp = self._new_match_name()
        scrut = self.emit_expr(stmt.scrutinee, 0)
        lines = [f"{p}const {temp} = {scrut};"]
        arms = []
        scrut_type = self._type_of(stmt.scrutinee)
        for arm in stmt.arms:
            cond, bindings = self._pattern_condition(arm.pattern, temp,
                                                     scrut_type)
            if arm.guard is not None:
                # The guard may reference the pattern's bindings
                # (e.g. `x if x > 100:`) - bind each name to its
                # pattern expression text while emitting it.
                self._push_scope()
                for name, expr_text, typ in bindings:
                    self._scopes[-1][name] = _Binding(
                        js=expr_text, type=typ, kind="local")
                guard = self.emit_expr(arm.guard, 0)
                self._pop_scope()
                cond = f"{cond} && {guard}" if cond else guard
            arms.append((cond, bindings, arm))
        # The last unconditional arm becomes `else:`; otherwise the
        # final `else` throws (defense - the checker proves
        # exhaustiveness, so it is unreachable in a checked program).
        last_uncond = (arms and arms[-1][0] is None)
        for i, (cond, bindings, arm) in enumerate(arms):
            if last_uncond and i == len(arms) - 1:
                lines.append(f"{p}}} else {{")
            elif i == 0:
                lines.append(f"{p}if ({cond}) {{")
            else:
                lines.append(f"{p}}} else if ({cond}) {{")
            self._push_scope()
            for name, expr_text, typ in bindings:
                b = self._bind(name, type=typ, kind="local")
                lines.append(f"{p}  const {b.js} = {expr_text};")
            lines.extend(self._emit_block(arm.body, indent + 1,
                                          new_scope=False))
            self._pop_scope()
        if arms:
            lines.append(f"{p}}}")
        if not last_uncond:
            lines.append(f"{p}else {{")
            self._need_rt("NewrError")
            lines.append(
                f"{p}  throw new NewrError(\"non-exhaustive match\");")
            lines.append(f"{p}}}")
        return lines

    def _pattern_condition(self, pattern, expr_text: str,
                           typ: Type | None):
        """Compile a pattern against the JS expression `expr_text`
        (usually the match temp) and the newr type `typ` of the value
        being matched. Returns (condition_text_or_None, bindings)
        where bindings is a list of (newr name, JS expression text,
        type). A `None` condition means the arm always matches
        (wildcard / name binding)."""
        if isinstance(pattern, A.WildcardPattern):
            return None, []
        if isinstance(pattern, A.NamePattern):
            return None, [(pattern.name, expr_text, typ)]
        if isinstance(pattern, A.LitPattern):
            v = pattern.value
            if v is None:
                return f"{expr_text} === null", []
            if isinstance(v, bool):
                return f"{expr_text} === {'true' if v else 'false'}", []
            if isinstance(v, int):
                return f"{expr_text} === {v}n", []
            if isinstance(v, float):
                return f"{expr_text} === {v!r}", []
            return f"{expr_text} === {_js_str(v)}", []
        if isinstance(pattern, A.EnumPattern):
            js_variant = (f"{self._mangle(pattern.enum_name)}_"
                          f"{self._mangle(pattern.variant)}")
            cond = f"{expr_text} instanceof {js_variant}"
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
                    cond += f" && {sub_cond}"
                bindings.extend(sub_bind)
            return cond, bindings
        if isinstance(pattern, A.StructPattern):
            js_struct = self._mangle(pattern.struct_name)
            cond = f"{expr_text} instanceof {js_struct}"
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
                    cond += f" && {sub_cond}"
                bindings.extend(sub_bind)
            return cond, bindings
        if isinstance(pattern, A.ListPattern):
            cond = (f"Array.isArray({expr_text}) && "
                    f"{expr_text}.length === {len(pattern.elements)}")
            bindings = []
            elem = typ.params[0] if typ is not None and typ.params else None
            for i, sub in enumerate(pattern.elements):
                sub_cond, sub_bind = self._pattern_condition(
                    sub, f"{expr_text}[{i}]", elem)
                if sub_cond:
                    cond += f" && {sub_cond}"
                bindings.extend(sub_bind)
            return cond, bindings
        if isinstance(pattern, A.OrPattern):
            conds = []
            all_bindings: list = []
            for sub in pattern.patterns:
                sub_cond, sub_bind = self._pattern_condition(sub, expr_text,
                                                             typ)
                conds.append(sub_cond if sub_cond else "true")
                all_bindings.append(sub_bind)
            cond = "(" + " || ".join(conds) + ")"
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
                        text = f"{bcond} ? {bexpr} : {text}"
                    bindings.append((name, text, typ))
            return cond, bindings
        self._fail(f"cannot emit pattern {type(pattern).__name__}")

    # ------------------------------------------------------------ propagate (D7)

    def _propagate_unwrapped(self, prop: A.Propagate) -> str:
        """D7: `?` at the end of a statement RHS. Emits the temp
        assignment + the early return as hoisted lines before the
        statement, and returns the text of the unwrapped value.

        A `Result` operand returns the `Err` itself from a
        Result-returning function; a nullable operand returns `null`
        from a nullable-returning function. The mismatched kinds
        (D30) and a nullable `Result` (G-M2-6) are clean
        `EmitterError`s - the checker should have rejected them, and
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
            # handler (a bare `return`) - there is no value to
            # propagate. (A handler that may exit early without
            # answering is the discord checker's E2010, not the
            # emitter's problem.)
            if t.name == "Result" and t.params and not t.nullable:
                self._hoists.append([
                    f"const {temp} = {text};",
                    # §18 fix 1: isErr is a METHOD on the JS Result -
                    # a bare access is the function itself (always
                    # truthy), so `?` early-returned on every value.
                    # The unwrapped value lives in `._value` (there
                    # is no `.value` property).
                    f"if ({temp}.isErr()) {{",
                    "  return;",
                    "}",
                ])
                return f"{temp}._value"
            if t.nullable and t.name != "Result":
                self._hoists.append([
                    f"const {temp} = {text};",
                    f"if ({temp} === null) {{",
                    "  return;",
                    "}",
                ])
                return temp
            if t.name == "Result" and t.nullable:
                self._fail("`?` on a nullable `Result` has no defined "
                           "meaning (G-M2-6) - unwrap it first, then "
                           "propagate the `Result`")
            self._fail("`?` needs a `Result[T, E]` or a `T?` value")
        if t.name == "Result" and t.params:
            if t.nullable:
                self._fail("`?` on a nullable `Result` has no defined "
                           "meaning (G-M2-6) - unwrap it first, then "
                           "propagate the `Result`")
            if ret is None or ret.name != "Result":
                self._fail("`?` on a `Result` value inside a function "
                           "that does not return a `Result` (D30 - the "
                           "checker should have rejected this)")
            self._hoists.append([
                f"const {temp} = {text};",
                # §18 fix 1: isErr is a METHOD on the JS Result - see
                # the handler branch above (bare `.isErr` is always
                # truthy; the value lives in `._value`).
                f"if ({temp}.isErr()) {{",
                f"  return {temp};",
                "}",
            ])
            return f"{temp}._value"
        if t.nullable:
            if ret is None or not ret.nullable:
                self._fail("`?` on a nullable value inside a function "
                           "that does not return a nullable (D30 - the "
                           "checker should have rejected this)")
            self._hoists.append([
                f"const {temp} = {text};",
                f"if ({temp} === null) {{",
                "  return null;",
                "}",
            ])
            return temp
        self._fail("`?` needs a `Result[T, E]` or a `T?` value")

    # ------------------------------------------------------------ module calls

    def _emit_module_call(self, node: A.Call,
                          binding: _Binding) -> tuple[str, int]:
        if binding.kind == "file-module":
            # A function in an imported .newr file: a plain call on
            # the imported module - and the import itself (J1: one
            # `.js` per imported `.newr`, required by its module
            # name). Struct construction through a file module needs
            # `new` (a JS class is not callable without it).
            ref = binding.js
            name = node.func.name
            sub = self.program.modules.get(binding.js)
            if sub is not None:
                for s in sub.structs:
                    if s.name == name:
                        order = [f.name for f in s.fields]
                        values: dict[str, str] = {}
                        for a, f in zip(node.args, order):
                            values[f] = self.emit_expr(a, 0)
                        for kname, value, _span in node.kwargs:
                            values[kname] = self.emit_expr(value, 0)
                        parts = [values[f] for f in order if f in values]
                        return (f"new {ref}.{self._mangle(name)}"
                                f"({', '.join(parts)})", _PREC["primary"])
            args = [self.emit_expr(a, 0) for a in node.args]
            kwargs = self._emit_module_kwargs(node, sub)
            return (f"{ref}.{self._mangle(name)}"
                    f"({', '.join(args + kwargs)})", _PREC["primary"])
        real = binding.module or binding.js
        if real == "file":
            name = node.func.name
            if name in ("read", "write", "append"):
                self._need_rt(f"file{ name.capitalize() }")
                args = [self.emit_expr(a, 0) for a in node.args]
                return (f"file{name.capitalize()}({', '.join(args)})",
                        _PREC["primary"])
            if name == "exists":
                self._need_rt("fileExists")
                return (f"fileExists({self.emit_expr(node.args[0], 0)})",
                        _PREC["primary"])
            self._fail(f"module `file` has no function `{name}`")
        args = [self.emit_expr(a, 0) for a in node.args]
        return self._emit_module_call_text(real, binding.js,
                                           node.func.name, args)

    def _emit_module_kwargs(self, node: A.Call,
                            sub: A.Program | None) -> list[str]:
        """Keyword arguments for a file-module fn call, reordered
        into position using the module's fn declaration."""
        if not node.kwargs or sub is None:
            return []
        fn = next((f for f in sub.functions
                   if f.name == node.func.name), None)
        if fn is None:
            self._fail(
                "internal error: cannot find fn "
                f"`{node.func.name}` for keyword arguments")
        order = [p.name for p in fn.params]
        values: list[str] = [""] * len(order)
        for kname, value, _span in node.kwargs:
            idx = order.index(kname)
            values[idx] = self.emit_expr(value, 0)
        if any(v == "" for v in values):
            self._fail(
                "internal error: missing keyword argument in call")
        return values

    def _emit_module_call_text(self, real: str, ref: str, name: str,
                               args: list[str]) -> tuple[str, int]:
        """The JS call text for a named-module function. `real` is
        the module name (for dispatch); `ref` is the JS reference
        (the alias, when the import is aliased)."""
        if real == "math":
            if name == "sqrt":
                return f"Math.sqrt({args[0]})", _PREC["primary"]
            if name == "floor":
                self._need_rt("toInt")
                return f"toInt(Math.floor({args[0]}))", _PREC["primary"]
            if name == "ceil":
                self._need_rt("toInt")
                return f"toInt(Math.ceil({args[0]}))", _PREC["primary"]
        if real == "time":
            if name == "now":
                self._need_rt("checkInt")
                return ("checkInt(BigInt(Math.floor(Date.now() / 1000)))",
                        _PREC["primary"])
            if name == "format":
                self._need_rt("timeFormat")
                return f"timeFormat({args[0]}, {args[1]})", _PREC["primary"]
        if real == "random":
            if name == "int":
                self._need_rt("randomInt")
                return f"randomInt({args[0]}, {args[1]})", _PREC["primary"]
            if name == "choice":
                self._need_rt("randomChoice")
                return f"randomChoice({args[0]})", _PREC["primary"]
            if name == "shuffle":
                self._need_rt("randomShuffle")
                return f"randomShuffle({args[0]})", _PREC["primary"]
        if real == "env":
            if name == "get":
                if len(args) == 2:
                    return (f"(process.env[{args[0]}] ?? {args[1]})",
                            _PREC["primary"])
                return (f"(process.env[{args[0]}] ?? null)",
                        _PREC["primary"])
        self._fail(f"module `{real}` has no function `{name}`")

    def _emit_module_member(self, node: A.Member,
                            binding: _Binding) -> tuple[str, int]:
        """A named-module member used as a value: a constant, or a
        function (which becomes a lambda, D29). `binding.module` is
        the real module name (for dispatch); `binding.js` is the JS
        reference (the alias, when the import is aliased)."""
        real = binding.module or binding.js
        ref = binding.js
        t = self._type_of(node)
        if t.name == "fn":
            # A module function as a first-class value.
            if real == "file" and node.name in ("read", "write", "append"):
                self._fail(f"`file.{node.name}` cannot be used as a "
                           "value (it needs a statement - the `with` "
                           "block cannot live inside a lambda)")
            params = t.params[:-1]  # the last param is the return type
            names = [f"_a{i}" for i in range(len(params))]
            call, _prec = self._emit_module_call_text(
                real, ref, node.name, names)
            if names:
                return (f"({', '.join(names)}) => {call}",
                        _PREC["primary"])
            return f"() => {call}", _PREC["primary"]
        if real == "env" and node.name == "args":
            return "process.argv.slice(1)", _PREC["primary"]
        if real == "math" and node.name in ("pi", "e"):
            return ("Math.PI" if node.name == "pi" else "Math.E",
                    _PREC["primary"])
        self._fail(f"module `{real}` has no value member `{node.name}`")

    # ------------------------------------------------------------ store calls

    def _store_key_expr(self, arg) -> str:
        """The key expression for a store call: an entity (user /
        member) contributes `String(<expr>.id)` (the key column is a
        str); a str is used as-is."""
        t = self._type_of(arg)
        text = self.emit_expr(arg, 0)
        if t.name in ("user", "member"):
            return f"String({text}.id)"
        if t.name == "str":
            return text
        return f"String({text})"

    def _emit_store_call(self, node: A.Call,
                         binding: _Binding) -> tuple[str, int]:
        """`economy.balance(user)` etc. -> `await bot.<store>_<op>(...)`
        (D17/P11, J9). Every mutation takes its idempotency `key:`;
        the checker enforces that it is present.

        §19 guard (mirrors the Python emitter): the `await` is only
        valid inside an async handler (command / event / task /
        component). At the top level, inside a (sync) top-level fn,
        or inside a lambda there is no async context - emitting the
        `await` would produce a SyntaxError in the generated file.
        Fail cleanly instead of emitting unparseable code."""
        store_name = binding.js
        method = node.func.name
        ctx = self._fn_ctx[-1] if self._fn_ctx else None
        if ctx is None or not ctx.get("in_handler"):
            self._fail(
                f"a store call (`{store_name}.{method}`) is only "
                "possible inside a command, event, or task handler - "
                "top-level functions and statements run outside the "
                "bot's async context",
                span=node.span)
        key_arg = {name: value for name, value, _span in node.kwargs}.get("key")
        key = (self.emit_expr(key_arg, 0) if key_arg is not None else None)
        if method == "balance":
            return (f"await bot.{store_name}_balance("
                    f"{self._store_key_expr(node.args[0])})",
                    _PREC["primary"])
        if method == "credit":
            return (f"await bot.{store_name}_credit("
                    f"{self._store_key_expr(node.args[0])}, "
                    f"{self.emit_expr(node.args[1], 0)}, {key})",
                    _PREC["primary"])
        if method == "debit":
            return (f"await bot.{store_name}_debit("
                    f"{self._store_key_expr(node.args[0])}, "
                    f"{self.emit_expr(node.args[1], 0)}, {key})",
                    _PREC["primary"])
        if method == "transfer":
            return (f"await bot.{store_name}_transfer("
                    f"{self._store_key_expr(node.args[0])}, "
                    f"{self._store_key_expr(node.args[1])}, "
                    f"{self.emit_expr(node.args[2], 0)}, {key})",
                    _PREC["primary"])
        if method == "get":
            table = node.args[0]
            if not isinstance(table, A.Lit) or not isinstance(table.value, str):
                self._fail("`get`'s table must be a string literal "
                           "naming a declared table")
            return (f"await bot.{store_name}_get({self.emit_expr(table, 0)}, "
                    f"{self.emit_expr(node.args[1], 0)})",
                    _PREC["primary"])
        if method == "set":
            table = node.args[0]
            if not isinstance(table, A.Lit) or not isinstance(table.value, str):
                self._fail("`set`'s table must be a string literal "
                           "naming a declared table")
            return (f"await bot.{store_name}_set({self.emit_expr(table, 0)}, "
                    f"{self.emit_expr(node.args[1], 0)})",
                    _PREC["primary"])
        if method == "delete":
            table = node.args[0]
            if not isinstance(table, A.Lit) or not isinstance(table.value, str):
                self._fail("`delete`'s table must be a string literal "
                           "naming a declared table")
            return (f"await bot.{store_name}_delete({self.emit_expr(table, 0)}, "
                    f"{self.emit_expr(node.args[1], 0)})",
                    _PREC["primary"])
        self._fail(f"store `{store_name}` has no operation `{method}`")

    # ------------------------------------------------------------ t() and embeds

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

    def _emit_t_call(self, node: A.TCall) -> tuple[str, int]:
        """`t("key", name: expr, ...)` -> `_t("key", <locale>, {...})`
        (D16, J10). Template placeholders that are handler bindings
        (user, guild, ...) are auto-passed in their interpolation
        form; the rest must be explicit arguments (the checker
        enforces it - E2009)."""
        index, _default = self._strings_index()
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
            kwargs.append(f"{name}: {self.emit_expr(value, 0)}")
        for placeholder in entry.placeholders:
            if placeholder in explicit:
                continue
            binding = self._lookup(placeholder)
            if binding is not None and binding.kind == "handler":
                kwargs.append(f"{placeholder}: {binding.interp_form()}")
                continue
            self._fail(f"`t(\"{node.key}\")` needs an argument for "
                       f"placeholder `{placeholder}` (the checker "
                       "should have rejected it)")
        args = ", ".join([_js_str(node.key), locale,
                          "{" + ", ".join(kwargs) + "}"])
        return f"_t({args})", _PREC["primary"]

    def _emit_embed(self, node: A.EmbedExpr) -> tuple[str, int]:
        # An embed as a value (SPEC §13.7: an `embed:` block is an
        # expression, e.g. `let card: embed = embed: ...`): build it
        # on hoisted lines before the enclosing statement and use
        # the fresh variable name (a fixed `embed` name would be a
        # redeclaration if two embeds appear in one block).
        lines, name = self._emit_embed_lines(node, 0)
        self._hoists.append(lines)
        return name, _PREC["primary"]

    def _emit_embed_lines(self, node: A.EmbedExpr, indent: int) -> tuple[list[str], str]:
        """Build an EmbedBuilder on the lines before the send call
        (the survey verified setTitle / setDescription /
        addFields({name, value, inline}) in the track). Returns the
        lines and the embed variable's name (a fresh temp per
        embed)."""
        p = "  " * indent
        name = self._new_temp_name()
        self._need_discord("EmbedBuilder")
        lines = [f"{p}const {name} = new EmbedBuilder()"]
        for field in node.fields:
            if field.kind == "title":
                lines.append(f"{p}  .setTitle({self.emit_expr(field.value, 0)})")
            elif field.kind == "description":
                lines.append(f"{p}  .setDescription({self.emit_expr(field.value, 0)})")
            elif field.kind == "color":
                lines.append(f"{p}  .setColor({self.emit_expr(field.value, 0)})")
        # The builder chain ends with a semicolon on the last call.
        if len(lines) > 1:
            lines[-1] += ";"
        else:
            lines[0] += ";"
        for field in node.fields:
            if field.kind == "field":
                inline = "true" if field.inline else "false"
                # A field's name is a plain string (not an expression).
                lines.append(f"{p}{name}.addFields({{ name: {_js_str(field.name)}, value: {self.emit_expr(field.value, 0)}, inline: {inline} }});")
        return lines, name

    # ------------------------------------------------------------ log statement

    def _emit_log_stmt(self, stmt: A.Log, indent: int) -> list[str]:
        """`log <expr>` is info level (P10). In JS it is
        `console.info(...)` in both general and bot programs (J12 -
        the JS twin of the bot's named logger and the general
        program's root logger)."""
        p = "  " * indent
        if isinstance(stmt.value, A.Propagate):
            value = self._propagate_unwrapped(stmt.value)
        else:
            value = self.emit_expr(stmt.value, 0)
        self._used_log = True
        return [f"{p}console.info({value});"]

    # ------------------------------------------------------------ statements

    def _emit_block(self, stmts: list, indent: int,
                    new_scope: bool = True) -> list[str]:
        """Emit a list of statements at `indent`. Returns lines with
        the indent prefix applied. Each statement gets its own hoist
        list (temps and hoisted defs go before it, at its indent).

        A component attaches only to an answer in the SAME block
        (D40): the last answer is tracked per block (saved/restored
        around nested blocks), and a Button/Select statement rewrites
        that answer's line to carry the components."""
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
                    # BEFORE the answer they attach to - the answer
                    # references the components variable, so defining
                    # it after the answer would be a ReferenceError.
                    if self._last_answer is not None:
                        idx, kind = self._last_answer
                        added = lines[before:]
                        del lines[before:]
                        lines[idx:idx] = added
                        self._last_answer = (idx + len(added), kind)
                    self._attach_components(lines)
        finally:
            if new_scope:
                self._pop_scope()
            self._last_answer = saved_answer
        return lines

    def _attach_components(self, lines: list[str]):
        """Attach the pending components (set by _emit_button /
        _emit_select) to the block's last answer line (D40, J10):
        the answer gains `components: [...]`. When a component has
        an `on_timeout`, the collector also stores its message so
        `on_timeout` can edit it."""
        if not self._pending_components:
            return
        pending = self._pending_components
        self._pending_components = []
        if self._last_answer is None:
            self._fail("a `button` / `select` must follow the reply "
                       "it attaches to (there is no message to put "
                       "the components on)")
        idx, kind = self._last_answer
        line = lines[idx].rstrip()
        # The answer line ends with `});` (an options object) or
        # `);` (a bare string). Insert `components: [...]` into the
        # options object, or wrap a bare string into one.
        if line.endswith("});"):
            lines[idx] = line[:-3].rstrip() + ", components: [" + \
                ", ".join(c["row"] for c in pending) + "]});"
        elif line.endswith(");"):
            # A bare-string reply: `interaction.reply("text");` ->
            # `interaction.reply({ content: "text", components: [...] });`
            inner = line[:-2]
            # Split off the leading `await ...reply(` / `send(` /
            # `editReply(` prefix.
            open_paren = inner.index("(")
            prefix, arg = inner[:open_paren + 1], inner[open_paren + 1:]
            lines[idx] = (f"{prefix}{{ content: {arg}, components: ["
                          + ", ".join(c["row"] for c in pending)
                          + "] });")
        else:
            self._fail("internal: the answer line does not end with `)`")
        # The message: the collector needs it (createMessageComponent-
        # Collector is called on the message), and an `on_timeout`
        # body edits it. Capture it right after the answer
        # (statements may follow the component). Several components
        # may share one answer (and one message): capture it once,
        # under the first component's var, and alias the rest to it.
        p = line[:len(line) - len(line.lstrip())]
        level = len(p) // 2
        first_var = pending[0]["var"]
        ctx = self._handler_ctx_stack[-1] if self._handler_ctx_stack else {}
        inserted = 0
        if kind == "timeout_edit":
            # A button attached to a timeout-edit reply: the message
            # is already the on_timeout handler's message.
            message_var = ctx.get("message_var")
            if message_var is None:
                self._fail("internal: timeout reply without a message")
        else:
            message_var = f"{first_var}_message"
            if kind in ("followup", "edit", "system_channel",
                        "message_channel"):
                # These calls return the message - capture it
                # directly by rewriting the answer line.
                stripped = lines[idx]
                indent_len = len(stripped) - len(stripped.lstrip())
                lines[idx] = (stripped[:indent_len]
                              + f"const {message_var} = "
                              + stripped[indent_len:])
            else:
                # A reply answers the original response - fetchReply
                # returns it.
                lines.insert(idx + 1,
                             f"{p}const {message_var} = "
                             f"await interaction.fetchReply();")
                inserted += 1
        for c in pending[1:]:
            lines.insert(idx + 1 + inserted,
                         f"{p}const {c['var']}_message = {message_var};")
            inserted += 1
        # The collector (J10): one per answer, routing clicks to the
        # shared componentHandlers map by customId. The `only:` gate
        # lives here - the shared handler is module-level and cannot
        # see the invoking interaction, but this callback closes
        # over it (the starter's author-gate-in-the-handler shape,
        # with a refusal for strangers instead of silence).
        insert_at = idx + 1 + inserted
        self._need_discord("ComponentType")
        collector_var = f"{first_var}_collector"
        types = {c["component_type"] for c in pending}
        if types == {"Button"}:
            ctype = "ComponentType.Button"
        elif types == {"StringSelect"}:
            ctype = "ComponentType.StringSelect"
        else:
            ctype = None  # mixed rows: collect all, route by customId
        timeout_ms = max(c["timeout"] for c in pending) * 1000
        collector_lines = []
        if ctype is not None:
            collector_lines.append(
                f"{p}const {collector_var} = {message_var}"
                f".createMessageComponentCollector({{ componentType: {ctype}, time: {timeout_ms} }});")
        else:
            collector_lines.append(
                f"{p}const {collector_var} = {message_var}"
                f".createMessageComponentCollector({{ time: {timeout_ms} }});")
        collector_lines.append(f"{p}{collector_var}.on(\"collect\", async (i) => {{")
        for c in pending:
            if c["only_kind"] in ("author", "roles"):
                # The gate is per-component (a shared collector may
                # serve several buttons with different `only:`). The
                # 'collect' body is one level in from the collector
                # line, so the gate sits at level + 1.
                collector_lines.extend(self._author_check(
                    c["only_kind"], c["only_roles"], level + 1,
                    custom_id=c["custom"]))
        collector_lines.append(f"{p}  const handler = componentHandlers.get(i.customId);")
        collector_lines.append(f"{p}  if (handler) {{")
        collector_lines.append(f"{p}    await handler(i);")
        collector_lines.append(f"{p}  }}")
        collector_lines.append(f"{p}}});")
        # The timeout: disable every row's children, edit the
        # message, then run each component's on_timeout body (with
        # the message in scope). Only when at least one component
        # has an on_timeout.
        if any(c.get("has_timeout") for c in pending):
            collector_lines.append(f"{p}{collector_var}.on(\"end\", async (_collected, reason) => {{")
            collector_lines.append(f"{p}  if (reason === \"time\") {{")
            for c in pending:
                collector_lines.append(f"{p}    for (const child of {c['row']}.components) {{")
                collector_lines.append(f"{p}      child.setDisabled(true);")
                collector_lines.append(f"{p}    }}")
            rows = ", ".join(c["row"] for c in pending)
            collector_lines.append(f"{p}    await {message_var}.edit({{ components: [{rows}] }});")
            for c in pending:
                if not c.get("has_timeout"):
                    continue
                for ol in (c.get("on_timeout_lines") or []):
                    if ol.strip():
                        collector_lines.append(f"{p}    {ol}")
                    else:
                        collector_lines.append("")
            collector_lines.append(f"{p}  }}")
            collector_lines.append(f"{p}}});")
        lines[insert_at:insert_at] = collector_lines

    def _emit_stmt_with_hoists(self, stmt, indent: int) -> list[str]:
        saved = self._hoists
        self._hoists = []
        lines = self._emit_stmt(stmt, indent)
        hoists = self._hoists
        self._hoists = saved
        prefix = "  " * indent
        out: list[str] = []
        for h in hoists:
            out.extend(prefix + line for line in h)
        out.extend(lines)
        return out

    def _emit_stmt(self, stmt, indent: int) -> list[str]:
        p = "  " * indent
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
            keyword = "const" if isinstance(stmt, A.Const) else "let"
            return [f"{p}{keyword} {binding.js} = {value};"]
        if isinstance(stmt, A.Assign):
            return self._emit_assign(stmt, indent)
        if isinstance(stmt, A.If):
            return self._emit_if(stmt, indent)
        if isinstance(stmt, A.For):
            return self._emit_for(stmt, indent)
        if isinstance(stmt, A.While):
            cond = self.emit_expr(stmt.cond, 0)
            lines = [f"{p}while ({cond}) {{"]
            lines.extend(self._emit_block(stmt.body, indent + 1))
            lines.append(f"{p}}}")
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
            return [f"{p}break;"]
        if isinstance(stmt, A.Continue):
            return [f"{p}continue;"]
        if isinstance(stmt, A.Pass):
            return []
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
            return [f"{p}{text};"] if text else []
        if isinstance(stmt, A.ButtonDecl):
            return self._emit_button(stmt, indent)
        if isinstance(stmt, A.SelectDecl):
            return self._emit_select(stmt, indent)
        if isinstance(stmt, A.ModalDecl):
            return self._emit_modal(stmt, indent)
        self._fail(f"cannot emit statement {type(stmt).__name__}")

    def _emit_assign(self, stmt: A.Assign, indent: int) -> list[str]:
        p = "  " * indent
        binding = self._lookup(stmt.name)
        if binding is None:
            self._fail(f"assignment to unknown name `{stmt.name}`")
        if isinstance(stmt.value, A.Propagate):
            value = self._propagate_unwrapped(stmt.value)
        else:
            value = self.emit_expr(stmt.value, 0)
        if stmt.op == "=":
            return [f"{p}{binding.js} = {value};"]
        t = binding.type
        if _is_int(t):
            helper = {"+=": "add", "-=": "sub", "*=": "mul",
                      "/=": "tdiv"}[stmt.op]
            self._need_rt(helper)
            return [f"{p}{binding.js} = {helper}({binding.js}, {value});"]
        # float / str: bare augmented assignment (no overflow check
        # applies; `+=` on a str is concatenation; BigInt `+=` on an
        # int would also work, but ints go through the helpers above
        # so the overflow check is never skipped).
        return [f"{p}{binding.js} {stmt.op} {value};"]

    def _emit_if(self, stmt: A.If, indent: int) -> list[str]:
        p = "  " * indent
        lines = [f"{p}if ({self.emit_expr(stmt.cond, 0)}) {{"]
        lines.extend(self._emit_block(stmt.then_body, indent + 1))
        for cond, body in stmt.elifs:
            lines.append(f"{p}}} else if ({self.emit_expr(cond, 0)}) {{")
            lines.extend(self._emit_block(body, indent + 1))
        if stmt.else_body is not None:
            lines.append(f"{p}}} else {{")
            lines.extend(self._emit_block(stmt.else_body, indent + 1))
        lines.append(f"{p}}}")
        return lines

    def _emit_for(self, stmt: A.For, indent: int) -> list[str]:
        p = "  " * indent
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
        # (it cannot see the loop variable - `for x in x` reads the
        # enclosing `x`).
        iterable = self.emit_expr(stmt.iterable, 0)
        self._push_scope()
        binding = self._bind(stmt.var, type=elem, kind="local")
        if it_type.name == "dict":
            # A dict iterates its keys (insertion order, like
            # Python's `for k in d`).
            header = f"for (const {binding.js} of {iterable}.keys()) {{"
        else:
            # Lists, sets and strings are all iterable in JS; a
            # string iterates UTF-16 code units - newr iterates code
            # points (P5), so a str loop iterates the spread.
            if it_type.name == "str":
                iterable = f"[...{iterable}]"
            header = f"for (const {binding.js} of {iterable}) {{"
        lines = [f"{p}{header}"]
        lines.extend(self._emit_block(stmt.body, indent + 1,
                                      new_scope=False))
        lines.append(f"{p}}}")
        self._pop_scope()
        return lines

    def _emit_try(self, stmt: A.Try, indent: int) -> list[str]:
        p = "  " * indent
        lines = [f"{p}try {{"]
        lines.extend(self._emit_block(stmt.body, indent + 1))
        if stmt.catches:
            if len(stmt.catches) > 1:
                self._fail("`try` with more than one `catch` is not "
                           "supported in v1 (the first would catch "
                           "everything)")
            cname, _span, cbody = stmt.catches[0]
            exc_var = self._new_temp_name()
            lines.append(f"{p}}} catch ({exc_var}) {{")
            self._push_scope()
            binding = self._bind(cname, type=Type("error"), kind="local")
            self._need_rt("asError")
            lines.append(f"{p}  const {binding.js} = asError({exc_var});")
            lines.extend(self._emit_block(cbody, indent + 1,
                                          new_scope=False))
            self._pop_scope()
        if stmt.finally_body is not None:
            lines.append(f"{p}}} finally {{")
            lines.extend(self._emit_block(stmt.finally_body, indent + 1))
        lines.append(f"{p}}}")
        return lines

    def _emit_throw(self, stmt: A.Throw, indent: int) -> list[str]:
        p = "  " * indent
        t = self._type_of(stmt.value)
        text = self.emit_expr(stmt.value, 0)
        if t.name == "error":
            return [f"{p}throw {text};"]
        if _is_str(t):
            self._need_rt("NewrError")
            return [f"{p}throw new NewrError({text});"]
        self._fail(f"`throw` needs a `str` or an `error`, not `{t}`")

    def _emit_return(self, stmt: A.Return, indent: int) -> list[str]:
        p = "  " * indent
        ctx = self._fn_ctx[-1] if self._fn_ctx else None
        if ctx is None:
            # A bare top-level `return` exits the program (D11).
            if stmt.value is None:
                return [f"{p}process.exit(0);"]
            self._fail("a top-level `return` with a value is not "
                       "supported - a top-level `return` exits the "
                       "program")
        ensures = ctx["ensures"]
        if stmt.value is None:
            if not ensures:
                return [f"{p}return;"]
            lines = [f"{p}_newrResult = null;"]
            lines.extend(self._ensures_check_lines(ensures, indent))
            lines.append(f"{p}return _newrResult;")
            return lines
        if isinstance(stmt.value, A.Propagate):
            # A propagated error returns early; the `ensures` apply
            # only to normal (Ok) returns, not to propagated errors.
            unwrapped = self._propagate_unwrapped(stmt.value)
            return [f"{p}return {unwrapped};"]
        value = self.emit_expr(stmt.value, 0)
        if not ensures:
            return [f"{p}return {value};"]
        lines = [f"{p}_newrResult = {value};"]
        lines.extend(self._ensures_check_lines(ensures, indent))
        lines.append(f"{p}return _newrResult;")
        return lines

    def _ensures_check_lines(self, ensures: list, indent: int) -> list[str]:
        """`if (!(<cond>)) throw new NewrError("ensures failed: <cond>")`
        for each `ensures` clause (D5: explicit `throw`, never
        `assert`). `result` in the condition reads `_newrResult`
        (bound in the function scope when the function has
        `ensures`)."""
        p = "  " * indent
        lines: list[str] = []
        for clause in ensures:
            cond = self.emit_expr(clause.cond, 0)
            msg = cond.replace("_newrResult", "result")
            self._need_rt("NewrError")
            lines.append(f"{p}if (!({cond})) {{")
            lines.append(
                f"{p}  throw new NewrError("
                f"{_js_str('ensures failed: ' + msg)});")
            lines.append(f"{p}}}")
        return lines

    # ------------------------------------------------------------ functions

    def _emit_fn(self, decl: A.FnDecl, indent: int = 0) -> list[str]:
        p = "  " * indent
        binding = self._lookup(decl.name)
        js_name = binding.js if binding is not None else self._mangle(decl.name)
        self._push_scope()
        saved_used = self._fn_used
        self._fn_used = set()
        params = [self._bind_fresh(prm.name, type=prm.type, kind="param")
                  for prm in decl.params]
        sig = ", ".join(b.js for b in params)
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
            # `result` in an `ensures` clause reads `_newrResult`.
            self._scopes[-1]["result"] = _Binding(js="_newrResult",
                                                  type=decl.return_type,
                                                  kind="local")
            self._fn_used.add("_newrResult")
        self._fn_ctx.append({"return_type": decl.return_type,
                             "ensures": ensures, "in_handler": False})
        if requires or ensures:
            # The contract checks below throw NewrError.
            self._need_rt("NewrError")
        lines = [f"{p}function {js_name}({sig}) {{"]
        bp = "  " * (indent + 1)
        body_lines: list[str] = []
        if ensures:
            # `_newrResult` is declared ONCE, at the top of the
            # function - every `return` assigns it (a `const` per
            # return would be a redeclaration SyntaxError).
            body_lines.append(f"{bp}let _newrResult;")
        for req in requires:
            cond = self.emit_expr(req.cond, 0)
            body_lines.append(f"{bp}if (!({cond})) {{")
            body_lines.append(
                f"{bp}  throw new NewrError("
                f"{_js_str('requires failed: ' + cond)});")
            body_lines.append(f"{bp}}}")
        body_lines.extend(self._emit_block(rest, indent + 1,
                                           new_scope=False))
        if ensures and not (rest and isinstance(rest[-1], A.Return)):
            # The implicit return (falling off the end) is also a
            # return - check the `ensures` against `null`.
            body_lines.append(f"{bp}_newrResult = null;")
            body_lines.extend(self._ensures_check_lines(ensures, indent + 1))
            body_lines.append(f"{bp}return _newrResult;")
        if not body_lines:
            body_lines = [f"{bp};"]
        lines.extend(body_lines)
        lines.append(f"{p}}}")
        self._fn_ctx.pop()
        self._fn_used = saved_used
        self._pop_scope()
        return lines

    # ------------------------------------------------------------ types / decls

    def _emit_struct(self, decl: A.StructDecl, indent: int = 0) -> list[str]:
        p = "  " * indent
        js_name = self._mangle(decl.name)
        self._need_rt("toStr")
        lines = [f"{p}class {js_name} {{"]
        if decl.fields:
            params = ", ".join(self._mangle(f.name) for f in decl.fields)
            lines.append(f"{p}  constructor({params}) {{")
            for f in decl.fields:
                lines.append(f"{p}    this.{self._mangle(f.name)} = "
                             f"{self._mangle(f.name)};")
            lines.append(f"{p}    Object.freeze(this);")
            lines.append(f"{p}  }}")
        else:
            lines.append(f"{p}  constructor() {{")
            lines.append(f"{p}    Object.freeze(this);")
            lines.append(f"{p}  }}")
        lines.append("")
        lines.append(f"{p}  _newrRepr() {{")
        if decl.fields:
            parts = ", ".join(
                f"{f.name}: ${{toStr(this.{self._mangle(f.name)})}}"
                for f in decl.fields)
            lines.append(f"{p}    return `{js_name}({parts})`;")
        else:
            lines.append(f"{p}    return `{js_name}()`;")
        lines.append(f"{p}  }}")
        lines.append(f"{p}}}")
        return lines

    def _emit_enum(self, decl: A.EnumDecl, indent: int = 0) -> list[str]:
        p = "  " * indent
        js_enum = self._mangle(decl.name)
        self._need_rt("toStr")
        lines: list[str] = []
        for variant in decl.variants:
            js_variant = f"{js_enum}_{self._mangle(variant.name)}"
            self._module_used.add(js_variant)
            lines.append(f"{p}class {js_variant} {{")
            if variant.fields:
                params = ", ".join(self._mangle(f.name)
                                   for f in variant.fields)
                lines.append(f"{p}  constructor({params}) {{")
                for f in variant.fields:
                    lines.append(f"{p}    this.{self._mangle(f.name)} = "
                                 f"{self._mangle(f.name)};")
                lines.append(f"{p}    Object.freeze(this);")
                lines.append(f"{p}  }}")
            else:
                lines.append(f"{p}  constructor() {{")
                lines.append(f"{p}    Object.freeze(this);")
                lines.append(f"{p}  }}")
            lines.append("")
            lines.append(f"{p}  _newrRepr() {{")
            if variant.fields:
                parts = ", ".join(
                    f"{f.name}: ${{toStr(this.{self._mangle(f.name)})}}"
                    for f in variant.fields)
                lines.append(f"{p}    return `{js_enum}.{variant.name}({parts})`;")
            else:
                lines.append(f"{p}    return `{js_enum}.{variant.name}`;")
            lines.append(f"{p}  }}")
            lines.append(f"{p}}}")
            lines.append("")
        # Drop the trailing blank line the variant loop leaves behind.
        while lines and lines[-1] == "":
            lines.pop()
        return lines

    # ------------------------------------------------------------ file assembly

    def _header(self, main_path: str, description: str | None = None) -> list[str]:
        lines = [
            f"// Generated by newr from {main_path}. Do not edit by hand.",
            f"// Edit the .newr file and rebuild with: newr build {main_path}",
        ]
        if description:
            lines.append(f"// {description}")
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
                self._bind_module_level(bound, kind="module")
                self._scopes[0][bound].module = imp.module
            elif imp.path is not None:
                self._bind_module_level(bound, kind="file-module")
                stem = Path(imp.resolved_path or imp.path).stem
                self.imported_modules[bound] = stem
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
        # The js-name -> fn-decl map (for keyword-argument reordering
        # in _emit_kwargs).
        self.fns_by_js = {}
        for decl in program.functions:
            b = self._lookup(decl.name)
            if b is not None:
                self.fns_by_js[b.js] = decl
        # The top level's used-name set starts with every module-level
        # JS name, so a `let` in a top-level block cannot reuse one
        # (at module level JS has no block scope for the top level -
        # the name would clobber the module binding).
        self._fn_used = set(self._module_used)

    def _emit_general_file(self, program: A.Program, main_path: str):
        """A general (non-bot) program: header, imports, structs/enums,
        functions, then the top-level STATEMENTS in file order (SPEC
        §8). Definitions (structs/enums/functions) are hoisted - a
        function body reads module globals at call time, so emitting
        the `function` before the statements is safe - but the
        statements themselves (lets, assignments, prints, ...) must
        run in the order written. (M2 evaluation #1.) Every file
        with `pub` names gets a `module.exports` block for them -
        in BOTH roles (main and import), so a file module's text
        does not depend on how it was built (the golden test builds
        shop_items.newr both ways); the block is a no-op when there
        are no pub names, and harmless when the main file is run
        directly."""
        self.program = program
        self.fns = {f.name: f for f in program.functions}
        self.structs = {s.name: s for s in program.structs}
        self.enums = {e.name: e for e in program.enums}
        self.stores = {s.name: s for s in program.stores}
        self._register_module_scope(program)
        self._fn_ctx = []
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
        body.extend(self._emit_file_exports(program))
        lines = self._header(main_path)
        lines.append('"use strict";')
        imports = self._render_imports()
        if imports:
            lines.append("")
            lines.extend(imports)
        if body:
            lines.append("")
            lines.extend(body)
        text = "\n".join(lines) + "\n"
        return text, bool(self.rt_helpers)

    def _emit_file_set(self, program: A.Program, main_path: str,
                       is_main: bool = True):
        """Emit one program and every file module it imports (recursively).
        Returns (files, uses_rt). `is_main` marks the top file - the
        only role difference left is that the top file is the entry
        point (a bot's `main()` guard, a general file's top-level
        statements); `pub` exports are emitted for every file (see
        _emit_general_file)."""
        files: dict[str, str] = {}
        text, uses_rt = self._emit_one(program, main_path, is_main=is_main)
        files[Path(main_path).stem + ".js"] = text
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
            # (D2 - so building `examples/shop.newr` and building
            # `examples/shop_items.newr` directly give the same
            # shop_items.js header).
            import os
            sub_path = os.path.relpath(sub_path)
            sub_em = _JSEmitter(sub, main_path=sub_path, sources=self.sources,
                                line_comments=self.line_comments)
            sub_files, sub_rt = sub_em._emit_file_set(sub, sub_path,
                                                      is_main=False)
            files.update(sub_files)
            uses_rt = uses_rt or sub_rt
        return files, uses_rt

    def _emit_one(self, program: A.Program, main_path: str,
                  is_main: bool = True):
        """Emit a single program's file. Returns (text, uses_rt)."""
        self.program = program
        if program.bot is not None:
            return self._emit_bot_file(program, main_path)
        return self._emit_general_file(program, main_path)

    def _emit_file_exports(self, program: A.Program) -> list[str]:
        """The `module.exports` block for a file: every `pub` name,
        in declaration order. `pub let`/`const` values are exported
        through getters, so a live read from the main file sees the
        current value (a plain value copy would freeze the value at
        import time). Empty when the file has no `pub` names."""
        entries: list[str] = []
        for decl in program.structs:
            if decl.pub:
                entries.append(self._mangle(decl.name))
        for decl in program.enums:
            if decl.pub:
                # §20: only the variant classes are exported. The
                # bare enum name has no JS binding (`_emit_enum`
                # defines `Shade_Red` / `Shade_Blue`, never `Shade`),
                # so exporting it made `require` of any file with a
                # `pub enum` throw `ReferenceError: Shade is not
                # defined` at the `module.exports` line. (Python
                # emits the union alias, so its export is real.)
                for variant in decl.variants:
                    entries.append(f"{self._mangle(decl.name)}_"
                                   f"{self._mangle(variant.name)}")
        for decl in program.functions:
            if decl.pub:
                b = self._lookup(decl.name)
                entries.append(b.js if b is not None
                               else self._mangle(decl.name))
        for st in program.stmts:
            if isinstance(st, (A.Let, A.Const)) and st.pub:
                b = self._lookup(st.name)
                js = b.js if b is not None else self._mangle(st.name)
                entries.append(f"get {js}() {{ return {js}; }}")
        if not entries:
            return []
        lines = ["", "module.exports = {"]
        for i, entry in enumerate(entries):
            comma = "," if i < len(entries) - 1 else ""
            lines.append(f"  {entry}{comma}")
        lines.append("};")
        return lines
    # ------------------------------------------------------------ bot emitter

    # Handler bindings: name -> (value form, interpolation form,
    # newr type name). The value form is what a bare name means; the
    # interpolation form is what `{name}` renders as (the mention /
    # name form, per the v0 golden). The JS twin of the Python
    # emitter's _BOT_BINDINGS - discord.js spellings.
    _BOT_BINDINGS: dict[str, dict[str, tuple[str, str | None, str]]] = {
        "command": {
            "user": ("interaction.user",
                     "interaction.user.mention", "user"),
            "guild": ("interaction.guild",
                      "interaction.guild ? interaction.guild.name : 'DM'",
                      "guild"),
            "channel": ("interaction.channel",
                        "_channelText(interaction.channel)", "channel"),
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
            "bot": ("readyClient.user", "readyClient.user.tag", "user"),
        },
        "message": {
            "message": ("message", "message.content", "message"),
            "user": ("message.author", "message.author.mention", "user"),
        },
        "component": {  # a button/select/modal callback has its own
            "user": ("interaction.user",
                     "interaction.user.mention", "user"),
            "guild": ("interaction.guild",
                      "interaction.guild ? interaction.guild.name : 'DM'",
                      "guild"),
            "channel": ("interaction.channel",
                        "_channelText(interaction.channel)", "channel"),
            "interaction": ("interaction", None, "interaction"),
        },
    }

    # discord.py Intents attribute name -> discord.js GatewayIntentBits
    # member (the names newr programs write are the discord.py ones).
    _INTENT_MAP = {
        "guilds": "Guilds",
        "members": "GuildMembers",
        "messages": "GuildMessages",
        "guild_messages": "GuildMessages",
        "message_content": "MessageContent",
        "reactions": "GuildMessageReactions",
        "guild_reactions": "GuildMessageReactions",
        "presences": "GuildPresences",
        "voice_states": "GuildVoiceStates",
        "dm_messages": "DirectMessages",
        "dm_reactions": "DirectMessageReactions",
        "emojis_and_stickers": "GuildEmojisAndStickers",
        "integrations": "GuildIntegrations",
        "webhooks": "GuildWebhooks",
        "invites": "GuildInvites",
        "bans": "GuildModeration",
        "moderation": "GuildModeration",
        "auto_moderation_configuration": "AutoModerationConfiguration",
        "auto_moderation_execution": "AutoModerationExecution",
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
                js=value, type=Type(tname), kind="handler", interp=interp)
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
            js=value, type=Type(tname), kind="handler", interp=interp)

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

    def _component_class_name(self, label: str, suffix: str) -> str:
        """A component handler's function name, from its label/title:
        "Buy again" -> buyAgainHandler. Unique per file (a numeric
        suffix on a repeat)."""
        words = "".join(ch if ch.isalnum() else " " for ch in label).split()
        base = "".join(w.capitalize() for w in words) or "Component"
        base = base[0].lower() + base[1:]
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
        return "_".join(w.lower() for w in words) or "component"

    def _author_check(self, only_kind: str, only_roles, indent: int,
                      custom_id: str | None = None) -> list[str]:
        """The `only:` gate at the top of a component 'collect'
        callback: author / anyone / roles. (Emitted by
        _attach_components, which closes over the invoking
        interaction - the shared handler itself stays author-free,
        so one handler serves every invocation.) `custom_id`
        scopes the gate to one component on a shared collector."""
        p = "  " * indent
        scope = f"i.customId === {_js_str(custom_id)} && " if custom_id else ""
        if only_kind == "author":
            return [
                f"{p}if ({scope}i.user.id !== interaction.user.id) {{",
                f"{p}  await i.reply({{ content: \"This button is not for you.\", flags: MessageFlags.Ephemeral }});",
                f"{p}  return;",
                f"{p}}}",
            ]
        if only_kind == "roles":
            roles = ", ".join(_js_str(r) for r in (only_roles or []))
            return [
                f"{p}if ({scope}!(i.member && [...i.member.roles.cache.values()].some((role) => [{roles}].includes(role.name)))) {{",
                f"{p}  await i.reply({{ content: \"You do not have a role that can use this.\", flags: MessageFlags.Ephemeral }});",
                f"{p}  return;",
                f"{p}}}",
            ]
        return []

    # -- the bot file -------------------------------------------------

    def _emit_bot_file(self, program: A.Program,
                       main_path: str) -> tuple[str, bool]:
        """A bot program -> one discord.js file, following the
        discord.js starter track (~/workspace/discord-skills/tracks/
        discord-js/): header, requires, STRINGS + _t, the Bot class
        (intents, setup, store methods), `bot = new Bot()`, the
        commands array + commandMap, the componentHandlers map, the
        event registrations, the task loops, the dispatcher, the
        _channelText helper, main(). Command registration is a
        SEPARATE file (`<stem>_deploy.js`, the track's standalone
        deploy script) - never on ready (J10)."""
        bot = program.bot
        self.program = program
        self.fns = {f.name: f for f in program.functions}
        self.structs = {s.name: s for s in program.structs}
        self.enums = {e.name: e for e in program.enums}
        self.stores = {s.name: s for s in program.stores}
        self._bot_components = []
        self._register_module_scope(program)
        # The header (D2: the actual input file as passed).
        lines = self._header(main_path, bot.description)
        # The body is emitted first (imports are usage-tracked).
        body: list[str] = []
        # §19: the file's top-level declarations are part of a bot
        # file (SPEC §8, §8.2, §13.7) - structs/enums/functions
        # first (hoisted, as in a general file), then the top-level
        # statements in file order. They run at module load time,
        # before `const bot = new Bot()` and main(); handlers read
        # them at call time. Before this fix the bot file dropped
        # them entirely, so a handler calling a file-module fn died
        # with ReferenceError even though the checker accepted it.
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
        body.extend(self._emit_bot_class(program))
        body.append("")
        body.append("")
        body.append("const bot = new Bot();")
        for cmd in program.commands:
            body.extend(self._emit_command(cmd))
        body.extend(self._close_commands())
        if program.commands:
            body.append("")
            body.append("")
            body.extend(self._emit_dispatch())
        for task in program.tasks:
            body.extend(self._emit_task(task))
        for ev in program.events:
            body.extend(self._emit_event_registration(ev))
        body.append("")
        body.append("")
        if self._used_channel_text:
            body.extend(self._emit_channel_text())
            body.append("")
            body.append("")
        body.extend(self._emit_main())
        body.append("")
        body.append("")
        body.extend(self._emit_bot_exports())
        # The STRINGS dict + _t helper go before the Bot class.
        strings_lines = self._emit_strings(program)
        # Assemble: header, "use strict", imports, strings, body.
        out = list(lines)
        out.append('"use strict";')
        imports = self._render_imports()
        if imports:
            out.append("")
            out.extend(imports)
        if strings_lines:
            out.append("")
            out.extend(strings_lines)
        # The componentHandlers map is always emitted (possibly
        # empty) - _emit_bot_exports exports it unconditionally.
        out.append("")
        out.append("const componentHandlers = new Map([")
        for comp in self._bot_components:
            out.extend(comp)
        out.append("]);")
        out.append("")
        out.append("")
        out.extend(body)
        text = "\n".join(out).rstrip("\n") + "\n"
        return text, bool(self.rt_helpers)

    def _emit_bot_class(self, program: A.Program) -> list[str]:
        """The Bot class: constructor (intents), setup (store
        connects), store methods."""
        lines: list[str] = []
        bot = program.bot
        intents = sorted(self._bot_intents(program))
        intent_bits = []
        for intent in intents:
            bit = self._INTENT_MAP.get(intent)
            if bit is None:
                self._fail(f"unknown intent `{intent}` (no discord.js "
                           "GatewayIntentBits mapping)")
            intent_bits.append(f"GatewayIntentBits.{bit}")
            self._need_discord("GatewayIntentBits")
        self._need_discord("Client")
        lines.append("class Bot extends Client {")
        lines.append("  constructor() {")
        lines.append(f"    super({{ intents: [{', '.join(intent_bits)}] }});")
        for store in program.stores:
            lines.append(f"    this.{store.name}_db = null;")
        lines.append("  }")
        lines.append("")
        lines.append("  async setup() {")
        for store in program.stores:
            lines.append(f"    await this._{store.name}_connect();")
        lines.append("  }")
        # Store methods.
        for store in program.stores:
            lines.extend(self._emit_store_methods(store))
        lines.append("}")
        return lines

    def _emit_event_registration(self, ev: A.EventDecl) -> list[str]:
        """An event -> a `bot.on(...)` / `bot.once(...)` registration
        after the class."""
        lines: list[str] = []
        body = ev.effective_body()
        if ev.kind == "ready":
            lines.append("")
            lines.append("")
            self._need_discord("Events")
            lines.append("bot.once(Events.ClientReady, async (readyClient) => {")
            self._push_handler("ready", ["bot"],
                               reply=None, locale=None)
            try:
                lines.extend(self._emit_block(body, 1, new_scope=False))
            finally:
                self._pop_handler()
            lines.append("});")
            return lines
        if ev.kind in ("member_join", "member_remove"):
            event = ("GuildMemberAdd" if ev.kind == "member_join"
                     else "GuildMemberRemove")
            lines.append("")
            lines.append("")
            self._need_discord("Events")
            lines.append(f"bot.on(Events.{event}, async (member) => {{")
            self._push_handler(ev.kind,
                               ["user", "guild"],
                               reply="system_channel",
                               locale="member.guild.preferredLocale")
            try:
                # A reply in a member event goes to the guild's
                # system channel (the v0 golden), guarded against
                # null.
                inner = self._emit_block(body, 2, new_scope=False)
                lines.append("  const channel = member.guild.systemChannel;")
                lines.append("  if (channel !== null) {")
                lines.extend(inner)
                lines.append("  }")
            finally:
                self._pop_handler()
            lines.append("});")
            return lines
        if ev.kind == "message":
            lines.append("")
            lines.append("")
            self._need_discord("Events")
            lines.append("bot.on(Events.MessageCreate, async (message) => {")
            lines.append("  if (message.author.id === bot.user.id) {")
            lines.append("    return;")
            lines.append("  }")
            self._push_handler("message",
                               ["message", "user"],
                               reply="message_channel",
                               locale="message.guild ? message.guild.preferredLocale : null")
            try:
                lines.extend(self._emit_block(body, 1, new_scope=False))
            finally:
                self._pop_handler()
            lines.append("});")
            return lines
        self._fail(f"unknown event kind `{ev.kind}`")

    def _emit_command(self, cmd: A.CommandDecl) -> list[str]:
        """A command -> an entry in the `commands` array: a
        SlashCommandBuilder + an async execute function. The array
        itself is opened by the first command and closed by the
        last (tracked on the emitter)."""
        if not hasattr(self, "_commands_open"):
            self._commands_open = False
        lines: list[str] = []
        if not self._commands_open:
            self._need_discord("SlashCommandBuilder")
            lines.append("")
            lines.append("")
            lines.append("const commands = [")
            self._commands_open = True
        desc = cmd.description if cmd.description is not None else cmd.name
        lines.append("  {")
        lines.append(f"    data: new SlashCommandBuilder().setName({_js_str(cmd.name)}).setDescription({_js_str(desc)})")
        for opt in cmd.options:
            lines.append(f"      .{self._option_builder(opt)}")
        lines[-1] += ","
        lines.append("    async execute(interaction) {")
        self._push_handler("command",
                           ["user", "guild", "channel", "interaction"],
                           reply="interaction",
                           locale="interaction.locale")
        try:
            lines.extend(self._emit_option_bindings(cmd, 3))
            lines.extend(self._emit_block(cmd.effective_body(), 3,
                                          new_scope=False))
        finally:
            self._pop_handler()
        lines.append("    },")
        lines.append("  },")
        return lines

    def _close_commands(self) -> list[str]:
        if not getattr(self, "_commands_open", False):
            return []
        self._commands_open = False
        return [
            "];",
            "const commandMap = new Map(commands.map((c) => [c.data.name, c]));",
        ]

    def _option_builder(self, opt: A.OptionDecl) -> str:
        """One SlashCommandBuilder option call (J10): the builder
        name follows the option type; min/max become setMinValue /
        setMaxValue; choices become addChoices."""
        t = opt.type
        name = opt.name
        desc = opt.description if opt.description is not None else name
        if t is None or t.name == "str":
            builder = "addStringOption"
        elif t.name == "int":
            builder = "addIntegerOption"
        elif t.name in ("num", "float"):
            builder = "addNumberOption"
        elif t.name == "bool":
            builder = "addBooleanOption"
        elif t.name == "user":
            builder = "addUserOption"
        elif t.name == "member":
            builder = "addUserOption"
        elif t.name == "channel":
            builder = "addChannelOption"
        elif t.name == "role":
            builder = "addRoleOption"
        else:
            self._fail(f"option type `{t}` is not a command option type")
        parts = [f"(opt) => opt.setName({_js_str(name)})",
                 f".setDescription({_js_str(desc)})"]
        if opt.required:
            parts.append(".setRequired(true)")
        if opt.min_value is not None:
            parts.append(f".setMinValue({self._emit_option_bound(opt.min_value)})")
        if opt.max_value is not None:
            parts.append(f".setMaxValue({self._emit_option_bound(opt.max_value)})")
        if opt.choices:
            choices = ", ".join(
                f"{{ name: {self._emit_choice_text(c)}, value: {self._emit_choice_text(c)} }}"
                for c in opt.choices)
            parts.append(f".addChoices({choices})")
        return builder + "(" + "".join(parts) + ")"

    def _emit_option_bound(self, expr) -> str:
        """An option's min/max bound for setMinValue/setMaxValue:
        discord.js takes a JS number, not a BigInt. An int literal
        renders as its digits; anything else goes through
        Number(...) (a float literal is already a number)."""
        if isinstance(expr, A.Lit) and isinstance(expr.value, int):
            return str(expr.value)
        text = self.emit_expr(expr, 0)
        t = self._type_of(expr)
        if _is_int(t):
            return f"Number({text})"
        return text

    def _emit_choice_text(self, expr) -> str:
        return self.emit_expr(expr, 0)

    def _option_type_name(self, opt: A.OptionDecl) -> str:
        t = opt.type
        if t is None:
            return "str"
        return t.name

    def _emit_option_bindings(self, cmd: A.CommandDecl, indent: int) -> list[str]:
        """Bind each command option at the top of `execute`: a
        `const` reading it from `interaction.options`. Int options
        convert the JS number to a BigInt (newr ints are int64);
        optional options without a default stay nullable."""
        p = "  " * indent
        lines: list[str] = []
        for opt in cmd.options:
            name = opt.name
            tname = self._option_type_name(opt)
            getter = {
                "str": "getString", "int": "getInteger",
                "num": "getNumber", "float": "getNumber",
                "bool": "getBoolean", "user": "getUser",
                "member": "getMember", "channel": "getChannel",
                "role": "getRole",
            }.get(tname)
            if getter is None:
                self._fail(f"option type `{tname}` is not a command "
                           "option type")
            binding_type = Type(tname if tname != "num" else "float",
                                nullable=not opt.required
                                and opt.default is None)
            if opt.required:
                raw = f"interaction.options.{getter}({_js_str(name)}, true)"
                if tname == "int":
                    value = f"BigInt({raw})"
                else:
                    value = raw
                lines.append(f"{p}const {self._mangle(name)} = {value};")
                self._scopes[-1][name] = _Binding(
                    js=self._mangle(name), type=binding_type, kind="handler")
            elif opt.default is not None:
                default = self.emit_expr(opt.default, 0)
                raw = f"interaction.options.{getter}({_js_str(name)}) ?? {default}"
                if tname == "int":
                    value = f"BigInt({raw})"
                else:
                    value = raw
                lines.append(f"{p}const {self._mangle(name)} = {value};")
                self._scopes[-1][name] = _Binding(
                    js=self._mangle(name), type=binding_type, kind="handler")
            else:
                # Optional without a default: nullable. The raw
                # value is read once into a temp, then converted
                # (ints) or used as-is.
                temp = self._new_temp_name()
                lines.append(f"{p}const {temp} = "
                             f"interaction.options.{getter}({_js_str(name)});")
                if tname == "int":
                    value = f"{temp} === null ? null : BigInt({temp})"
                else:
                    value = temp
                lines.append(f"{p}const {self._mangle(name)} = {value};")
                self._scopes[-1][name] = _Binding(
                    js=self._mangle(name), type=binding_type, kind="handler")
        return lines

    def _task_fn_name(self, task: A.TaskDecl) -> str:
        """A task's loop-function name, from its interval (a task
        has no name in newr). Two tasks with the same interval get a
        numeric suffix. The name is computed once per task and
        cached - main() and the definition must agree."""
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
        """A task -> a named async function, started from main()
        with setInterval (J10: the track has no tasks.loop - plain
        Node setInterval, guarded against overlap by the async
        callback's own error handling). The interval is in
        milliseconds."""
        name = self._task_fn_name(task)
        lines: list[str] = []
        lines.append("")
        lines.append("")
        lines.append(f"async function {name}() {{")
        self._push_handler("task", [], reply=None, locale=None)
        try:
            lines.extend(self._emit_block(task.body, 1, new_scope=False))
        finally:
            self._pop_handler()
        lines.append("}")
        return lines

    def _emit_dispatch(self) -> list[str]:
        """The InteractionCreate dispatcher + dispatchCommand (the
        survey's single shared dispatcher: there is no
        CommandTree.on_error equivalent in discord.js). Routes
        chat-input commands, buttons, string selects, modal submits
        and autocomplete; command errors get the generic ephemeral
        reply."""
        self._need_discord("Events")
        self._need_discord("MessageFlags")
        lines = [
            "async function dispatchCommand(interaction, command) {",
            "  try {",
            "    await command.execute(interaction);",
            "  } catch (error) {",
            "    console.error(`[dispatch] /${command.data.name} failed:`, error);",
            "    const message = \"Something went wrong running that command. Try again in a moment.\";",
            "    try {",
            "      if (interaction.replied || interaction.deferred) {",
            "        await interaction.followUp({ content: message, flags: MessageFlags.Ephemeral });",
            "      } else {",
            "        await interaction.reply({ content: message, flags: MessageFlags.Ephemeral });",
            "      }",
            "    } catch (replyError) {",
            "      console.error(\"[dispatch] could not send the error reply:\", replyError);",
            "    }",
            "  }",
            "}",
            "",
            "",
            "bot.on(Events.InteractionCreate, async (interaction) => {",
            "  if (interaction.isChatInputCommand()) {",
            "    const command = commandMap.get(interaction.commandName);",
            "    if (!command) {",
            "      return;",
            "    }",
            "    await dispatchCommand(interaction, command);",
            "    return;",
            "  }",
            "  if (interaction.isButton() || interaction.isStringSelectMenu()) {",
            "    const handler = componentHandlers.get(interaction.customId);",
            "    if (!handler) {",
            "      return;",
            "    }",
            "    await handler(interaction);",
            "    return;",
            "  }",
            "  if (interaction.isModalSubmit()) {",
            "    const handler = componentHandlers.get(interaction.customId);",
            "    if (!handler) {",
            "      return;",
            "    }",
            "    await handler(interaction);",
            "    return;",
            "  }",
            "  if (interaction.isAutocomplete()) {",
            "    const command = commandMap.get(interaction.commandName);",
            "    if (!command || typeof command.autocomplete !== \"function\") {",
            "      return;",
            "    }",
            "    await command.autocomplete(interaction);",
            "    return;",
            "  }",
            "});",
        ]
        return lines

    def _emit_channel_text(self) -> list[str]:
        return [
            "function _channelText(channel) {",
            "  // The channel as text: its mention, its name, or \"\".",
            "  if (channel === null || channel === undefined) {",
            "    return \"\";",
            "  }",
            "  return channel.mention || String(channel);",
            "}",
        ]

    def _emit_main(self) -> list[str]:
        lines = [
            "async function main() {",
            "  const token = process.env.DISCORD_TOKEN;",
            "  if (!token) {",
            "    throw new Error(\"set the DISCORD_TOKEN environment variable\");",
            "  }",
            "  await bot.setup();",
            "  await bot.login(token);",
        ]
        for task in self.program.tasks:
            name = self._task_fn_name(task)
            ms = task.interval_seconds * 1000
            lines.append(
                f"  setInterval(() => {{ {name}().catch((e) => console.error(e)); }}, {ms});")
        lines.append("}")
        lines.append("")
        lines.append("")
        lines.append("if (require.main === module) {")
        lines.append("  main().catch((e) => {")
        lines.append("    console.error(e);")
        lines.append("    process.exit(1);")
        lines.append("  });")
        lines.append("}")
        return lines

    def _emit_bot_exports(self) -> list[str]:
        """The bot file exports its handles so tests can drive it
        without a live Discord connection (the file is still a
        program - `node shop.js` runs main() and ignores these)."""
        return [
            "module.exports = { bot, commands, commandMap, componentHandlers };",
        ]

    # -- answer statements (reply / defer / edit / followup / send) ---

    def _answer_parts(self, value, indent: int) -> tuple[list[str], str, bool]:
        """A reply/edit/followup value -> (pre-lines, call argument,
        is_embed). A text value is the argument itself; an embed is
        built on the lines before and the argument is its variable
        name (a fresh temp per embed - two `const embed`s in one
        block would be a redeclaration). A `?` value propagates
        first (the temp + early return are hoisted before the
        statement, D7)."""
        if isinstance(value, A.Propagate):
            unwrapped = self._propagate_unwrapped(value)
            return [], unwrapped, False
        if isinstance(value, A.EmbedExpr):
            lines, name = self._emit_embed_lines(value, indent)
            return lines, name, True
        return [], self.emit_expr(value, 0), False

    def _answer_call(self, kind: str, arg: str, is_embed: bool,
                     ephemeral: bool = False) -> str:
        """The discord.js call text for one answer kind. `arg` is
        the text argument, or the embed variable's name. The
        handler context names a command/component reply
        `"interaction"` (the Python emitter's name for it); it is
        the same `interaction.reply(...)` call as `"reply"`."""
        if kind == "interaction":
            kind = "reply"
        if is_embed:
            content = f"{{ embeds: [{arg}] }}"
        else:
            content = f"{{ content: {arg} }}"
        if kind == "reply":
            if ephemeral:
                self._need_discord("MessageFlags")
                return (f"await interaction.reply({{ content: {arg}, "
                        "flags: MessageFlags.Ephemeral })"
                        if not is_embed else
                        f"await interaction.reply({{ embeds: [{arg}], "
                        "flags: MessageFlags.Ephemeral })")
            return f"await interaction.reply({content})"
        if kind == "edit":
            return f"await interaction.editReply({content})"
        if kind == "followup":
            return f"await interaction.followUp({content})"
        if kind == "system_channel":
            return f"await channel.send({content})"
        if kind == "message_channel":
            return f"await message.channel.send({content})"
        if kind == "timeout_edit":
            var = self._handler_ctx().get("message_var")
            if var is None:
                self._fail("internal: timeout reply without a message")
            return f"await {var}.edit({content})"
        self._fail(f"internal: unknown answer kind `{kind}`")

    def _emit_reply(self, stmt: A.Reply, indent: int) -> list[str]:
        p = "  " * indent
        ctx = self._handler_ctx()
        if ctx["reply"] is None:
            self._fail("a `reply` is not possible here (this handler "
                       "has no interaction or channel to answer on)")
        pre, arg, is_embed = self._answer_parts(stmt.value, indent)
        lines = list(pre)
        if stmt.ephemeral and ctx["reply"] != "interaction":
            self._fail("`ephemeral` only works on an interaction reply")
        kind = ctx["reply"]
        lines.append(f"{p}{self._answer_call(kind, arg, is_embed, stmt.ephemeral)};")
        return lines

    def _emit_defer(self, stmt: A.Defer, indent: int) -> list[str]:
        p = "  " * indent
        if self._handler_ctx()["reply"] != "interaction":
            self._fail("`defer` needs an interaction (a command or "
                       "component handler)")
        return [f"{p}await interaction.deferReply();"]

    def _emit_edit(self, stmt: A.Edit, indent: int) -> list[str]:
        p = "  " * indent
        if self._handler_ctx()["reply"] != "interaction":
            self._fail("`edit` needs an interaction (a command or "
                       "component handler)")
        pre, arg, is_embed = self._answer_parts(stmt.value, indent)
        lines = list(pre)
        lines.append(f"{p}{self._answer_call('edit', arg, is_embed)};")
        return lines

    def _emit_followup(self, stmt: A.Followup, indent: int) -> list[str]:
        p = "  " * indent
        if self._handler_ctx()["reply"] != "interaction":
            self._fail("`followup` needs an interaction (a command or "
                       "component handler)")
        pre, arg, is_embed = self._answer_parts(stmt.value, indent)
        lines = list(pre)
        lines.append(f"{p}{self._answer_call('followup', arg, is_embed)};")
        return lines

    def _emit_send(self, stmt: A.Send, indent: int) -> list[str]:
        p = "  " * indent
        pre, arg, is_embed = self._answer_parts(stmt.value, indent)
        channel = self.emit_expr(stmt.channel, 0)
        lines = list(pre)
        if is_embed:
            lines.append(f"{p}await {channel}.send({{ embeds: [{arg}] }});")
        else:
            lines.append(f"{p}await {channel}.send({{ content: {arg} }});")
        return lines

    # -- strings / _t (D16, J10) ------------------------------------------

    def _emit_strings(self, program: A.Program) -> list[str]:
        """The STRINGS dict + _DEFAULT_LOCALE + the _t helper (only
        when `strings` blocks exist). Lookup: exact locale -> base
        language -> default locale (the first declared). The JS _t
        is a self-contained lookup over the emitted strings (the
        same data the Python _t uses) - NOT i18next: the emitted
        program is a single file with no package.json, and the
        discord.js track has no _t helper to copy (its i18n is M2,
        unported)."""
        if not program.strings:
            return []
        self._need_rt("toStr")
        default = program.strings[0].locale
        lines = [f"const _DEFAULT_LOCALE = {_js_str(default)};",
                 "const STRINGS = {"]
        for decl in program.strings:
            lines.append(f"  {_js_str(decl.locale)}: {{")
            for entry in decl.entries:
                lines.append(f"    {_js_str(entry.key)}: {_js_str(entry.template)},")
            lines.append("  },")
        lines.append("};")
        lines.append("")
        lines.append("")
        lines.append("function _t(key, locale, vars = {}) {")
        lines.append("  const loc = (typeof locale === \"string\" ? locale : null) || _DEFAULT_LOCALE;")
        lines.append("  const template = (STRINGS[loc] || STRINGS[loc.split(\"-\")[0]]")
        lines.append("    || STRINGS[_DEFAULT_LOCALE] || {})[key];")
        lines.append("  if (template === undefined) {")
        lines.append("    return key;")
        lines.append("  }")
        lines.append("  return template.replace(/\\{(\\w+)\\}/g, (_m, name) => toStr(vars[name] ?? null));")
        lines.append("}")
        return lines

    # -- components (button / select / modal, J10) ------------------------

    def _emit_button(self, stmt: A.ButtonDecl, indent: int) -> list[str]:
        p = "  " * indent
        handler = self._component_class_name(stmt.label, "Handler")
        var = self._new_component_name()
        row = f"{var}_row"
        style = stmt.style or "primary"
        if style == "link":
            self._fail("a `link` button cannot have a `do:` body "
                       "(Discord never sends an interaction for it)")
        if stmt.url:
            self._fail("a `link` button cannot have a `do:` body "
                       "(Discord never sends an interaction for it)")
        # The shared handler (registered in componentHandlers, keyed
        # by customId). The author/roles gate lives in the per-answer
        # 'collect' callback (see _attach_components), which closes
        # over the invoking interaction.
        self._need_discord("ButtonBuilder")
        self._need_discord("ButtonStyle")
        self._need_discord("ActionRowBuilder")
        custom = stmt.custom_id or self._component_var_name(stmt.label)
        style_map = {"primary": "Primary", "secondary": "Secondary",
                     "success": "Success", "danger": "Danger"}
        js_style = style_map.get(style)
        if js_style is None:
            self._fail(f"unknown button style `{style}`")
        handler_lines = [f"  [{_js_str(custom)}, async (interaction) => {{"]
        self._push_handler("component",
                           ["user", "guild", "channel", "interaction"],
                           reply="interaction",
                           locale="interaction.locale")
        try:
            handler_lines.extend(self._emit_block(stmt.body, 2,
                                                  new_scope=False))
        finally:
            self._pop_handler()
        handler_lines.append("  }],")
        self._bot_components.append(handler_lines)
        # The row (built in the handler, before the answer it
        # attaches to - _emit_block moves these lines before it).
        timeout = stmt.timeout if stmt.timeout is not None else 180
        on_timeout_lines = None
        if stmt.on_timeout is not None:
            self._push_handler("timeout", [], reply="timeout_edit",
                               locale=None)
            self._handler_ctx_stack[-1]["message_var"] = f"{var}_message"
            try:
                # Emitted at indent 0: _attach_components re-indents
                # the body into the collector's 'end' callback.
                on_timeout_lines = self._emit_block(stmt.on_timeout, 0,
                                                    new_scope=False)
            finally:
                self._pop_handler()
        self._pending_components.append({
            "row": row, "var": var, "custom": custom,
            "has_timeout": stmt.on_timeout is not None,
            "timeout": timeout,
            "component_type": "Button",
            "only_kind": stmt.only_kind, "only_roles": stmt.only_roles,
            "on_timeout_lines": on_timeout_lines,
        })
        return [f"{p}const {row} = new ActionRowBuilder().addComponents(",
                f"{p}  new ButtonBuilder().setCustomId({_js_str(custom)})"
                f".setLabel({_js_str(stmt.label)})"
                f".setStyle(ButtonStyle.{js_style}));"]

    def _emit_select(self, stmt: A.SelectDecl, indent: int) -> list[str]:
        p = "  " * indent
        var = self._new_component_name()
        row = f"{var}_row"
        timeout = stmt.timeout if stmt.timeout is not None else 180
        min_v = stmt.min_values if stmt.min_values is not None else 1
        max_v = stmt.max_values if stmt.max_values is not None else 1
        custom = self._component_var_name(stmt.label)
        self._need_discord("StringSelectMenuBuilder")
        self._need_discord("StringSelectMenuOptionBuilder")
        self._need_discord("ActionRowBuilder")
        options = ", ".join(
            f"new StringSelectMenuOptionBuilder().setLabel({_js_str(o)}).setValue({_js_str(o)})"
            for o in stmt.options)
        handler_lines = [f"  [{_js_str(custom)}, async (interaction) => {{"]
        self._push_handler("component",
                           ["user", "guild", "channel", "interaction"],
                           reply="interaction",
                           locale="interaction.locale")
        try:
            self._handler_binding("values", "interaction.values",
                                  "interaction.values.join(\", \")", "list[str]")
            handler_lines.extend(self._emit_block(stmt.body, 2,
                                                  new_scope=False))
        finally:
            self._pop_handler()
        handler_lines.append("  }],")
        self._bot_components.append(handler_lines)
        on_timeout_lines = None
        if stmt.on_timeout is not None:
            self._push_handler("timeout", [], reply="timeout_edit",
                               locale=None)
            self._handler_ctx_stack[-1]["message_var"] = f"{var}_message"
            try:
                # Emitted at indent 0: _attach_components re-indents
                # the body into the collector's 'end' callback.
                on_timeout_lines = self._emit_block(stmt.on_timeout, 0,
                                                    new_scope=False)
            finally:
                self._pop_handler()
        self._pending_components.append({
            "row": row, "var": var, "custom": custom,
            "has_timeout": stmt.on_timeout is not None,
            "timeout": timeout,
            "component_type": "StringSelect",
            "only_kind": stmt.only_kind, "only_roles": stmt.only_roles,
            "on_timeout_lines": on_timeout_lines,
        })
        return [f"{p}const {row} = new ActionRowBuilder().addComponents(",
                f"{p}  new StringSelectMenuBuilder().setCustomId({_js_str(custom)})"
                f".setPlaceholder({_js_str(stmt.label)})"
                f".setMinValues({min_v}).setMaxValues({max_v})"
                f".addOptions({options}));"]

    def _emit_modal(self, stmt: A.ModalDecl, indent: int) -> list[str]:
        p = "  " * indent
        var = self._new_component_name()
        custom = stmt.custom_id or self._component_var_name(stmt.title)
        self._need_discord("ModalBuilder")
        self._need_discord("TextInputBuilder")
        self._need_discord("TextInputStyle")
        self._need_discord("ActionRowBuilder")
        lines = [f"{p}const {var} = new ModalBuilder().setCustomId({_js_str(custom)}).setTitle({_js_str(stmt.title)})"]
        if stmt.timeout is not None:
            lines[-1] += f".setTimeout({stmt.timeout})"
        for field in stmt.fields:
            style = ("Paragraph" if field.style == "paragraph" else "Short")
            req = "true" if field.required else "false"
            mn = f".setMinLength({field.min_length})" if field.min_length is not None else ""
            mx = f".setMaxLength({field.max_length})" if field.max_length is not None else ""
            lines.append(f"{p}  .addComponents(new ActionRowBuilder().addComponents(")
            lines.append(f"{p}    new TextInputBuilder().setCustomId({_js_str(field.name)}).setLabel({_js_str(field.name)})"
                         f".setStyle(TextInputStyle.{style}).setRequired({req}){mn}{mx}));")
        lines[-1] += ";"
        # The submit handler (registered in componentHandlers).
        handler_lines = [f"  [{_js_str(custom)}, async (interaction) => {{"]
        self._push_handler("component",
                           ["user", "guild", "channel", "interaction"],
                           reply="interaction",
                           locale="interaction.locale")
        try:
            for field in stmt.fields:
                self._handler_binding(
                    field.name,
                    f"interaction.fields.getTextInputValue({_js_str(field.name)})",
                    f"interaction.fields.getTextInputValue({_js_str(field.name)})",
                    "str")
            handler_lines.extend(self._emit_block(stmt.body, 2,
                                                  new_scope=False))
        finally:
            self._pop_handler()
        handler_lines.append("  }],")
        self._bot_components.append(handler_lines)
        lines.append(f"{p}await interaction.showModal({var});")
        return lines

    # -- store methods on Bot (D17/P11, J9) -------------------------------

    _SQL_TYPES = {"int": "TEXT", "str": "TEXT", "float": "REAL",
                  "bool": "INTEGER"}

    def _emit_store_methods(self, store: A.StoreDecl) -> list[str]:
        """The store's methods on the Bot class: a connect (schema
        creation), balance / credit / debit / transfer (the economy
        ops, one transaction each, idempotent on `key`), and
        get / set / delete for general tables. node:sqlite is
        synchronous, so the methods are `async` for shape parity
        with the Python target but the sqlite calls run inline -
        each op is atomic (single-threaded) and wrapped in an
        explicit BEGIN IMMEDIATE...COMMIT for crash atomicity.
        Int columns are stored as TEXT (node:sqlite returns JS
        numbers and throws above 2^53); balance arithmetic runs in
        JS with BigInt + checkInt (J9)."""
        name = store.name
        self._need_sqlite()
        self._need_rt("checkInt")
        lines: list[str] = []
        lines.append("")
        lines.append(f"  async _{name}_connect() {{")
        lines.append(f"    this.{name}_db = new DatabaseSync(\"{name}.db\");")
        lines.append(f"    this.{name}_db.exec(\"PRAGMA busy_timeout = 5000\");")
        for table in store.tables:
            cols = []
            for i, col in enumerate(table.columns):
                sql = self._SQL_TYPES.get(col.type.name, "TEXT")
                pk = " PRIMARY KEY" if i == 0 else ""
                cols.append(f"{col.name} {sql}{pk}")
            cols_sql = ", ".join(cols)
            lines.append(f"    this.{name}_db.exec(\"CREATE TABLE IF NOT EXISTS {table.name} ({cols_sql})\");")
        if store.ledger:
            lines.append(f"    this.{name}_db.exec(\"CREATE TABLE IF NOT EXISTS ledger (id INTEGER PRIMARY KEY AUTOINCREMENT, user TEXT NOT NULL, amount TEXT NOT NULL, key TEXT UNIQUE, created_at INTEGER NOT NULL)\");")
        lines.append("  }")
        # The economy ops (only when the store has a balances table
        # of the required shape - the checker verifies it).
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
            f"  async {n}_balance(userId) {{",
            f"    const row = this.{n}_db.prepare(\"SELECT balance FROM balances WHERE user = ?\").get(userId);",
            "    return row ? BigInt(row.balance) : 0n;",
            "  }",
            "",
            f"  async _{n}_apply(userId, amount, key) {{",
            "    // Idempotent on key: a duplicate key is a replay,",
            "    // not an error (P11). One explicit transaction:",
            "    // the balance + ledger land together or not at all.",
            "    // node:sqlite is synchronous and single-threaded,",
            "    // so no lock is needed - no interleaving is possible.",
            "    if (key !== null && key !== undefined) {",
            f"      const dup = this.{n}_db.prepare(\"SELECT 1 FROM ledger WHERE key = ?\").get(key);",
            "      if (dup) {",
            "        return;",
            "      }",
            "    }",
            f"    this.{n}_db.exec(\"BEGIN IMMEDIATE\");",
            "    try {",
            f"      const row = this.{n}_db.prepare(\"SELECT balance FROM balances WHERE user = ?\").get(userId);",
            "      const cur = row ? BigInt(row.balance) : 0n;",
            "      const next = checkInt(cur + amount);",
            f"      this.{n}_db.prepare(\"INSERT INTO balances (user, balance) VALUES (?, ?) ON CONFLICT(user) DO UPDATE SET balance = excluded.balance\").run(userId, next.toString());",
            f"      this.{n}_db.prepare(\"INSERT INTO ledger (user, amount, key, created_at) VALUES (?, ?, ?, ?)\").run(userId, amount.toString(), key, Math.floor(Date.now() / 1000));",
            f"      this.{n}_db.exec(\"COMMIT\");",
            "    } catch (e) {",
            f"      this.{n}_db.exec(\"ROLLBACK\");",
            "      throw e;",
            "    }",
            "  }",
            "",
            f"  async {n}_credit(userId, amount, key = null) {{",
            f"    await this._{n}_apply(userId, amount, key);",
            "  }",
            "",
            f"  async {n}_debit(userId, amount, key = null) {{",
            "    // The balance check is inside the transaction (no",
            "    // TOCTOU): a concurrent debit cannot slip between",
            "    // the check and the write. A replayed key returns",
            "    // true - the debit already happened.",
            "    if (key !== null && key !== undefined) {",
            f"      const dup = this.{n}_db.prepare(\"SELECT 1 FROM ledger WHERE key = ?\").get(key);",
            "      if (dup) {",
            "        return true;",
            "      }",
            "    }",
            f"    this.{n}_db.exec(\"BEGIN IMMEDIATE\");",
            "    try {",
            f"      const row = this.{n}_db.prepare(\"SELECT balance FROM balances WHERE user = ?\").get(userId);",
            "      const cur = row ? BigInt(row.balance) : 0n;",
            "      if (cur < amount) {",
            f"        this.{n}_db.exec(\"ROLLBACK\");",
            "        return false;",
            "      }",
            "      const next = cur - amount;",
            f"      this.{n}_db.prepare(\"INSERT INTO balances (user, balance) VALUES (?, ?) ON CONFLICT(user) DO UPDATE SET balance = excluded.balance\").run(userId, next.toString());",
            f"      this.{n}_db.prepare(\"INSERT INTO ledger (user, amount, key, created_at) VALUES (?, ?, ?, ?)\").run(userId, (-amount).toString(), key, Math.floor(Date.now() / 1000));",
            f"      this.{n}_db.exec(\"COMMIT\");",
            "    } catch (e) {",
            f"      this.{n}_db.exec(\"ROLLBACK\");",
            "      throw e;",
            "    }",
            "    return true;",
            "  }",
            "",
            f"  async {n}_transfer(fromId, toId, amount, key = null) {{",
            "    // Both legs + the ledger in ONE transaction. A retry",
            "    // heals a partial transfer: each leg is topped up",
            "    // to its intended total (the out-leg debits `amount`,",
            "    // the in-leg credits `amount`), so no money is",
            "    // created or destroyed however the first attempt",
            "    // died - a crash between the legs, or a poisoned",
            "    // ledger row. The leg amounts are read from the",
            "    // ledger (not just presence) and summed in JS with",
            "    // BigInt - never SQL SUM, which would lose precision",
            "    // on TEXT amounts (J9). The leg upserts are computed",
            "    // in JS for the same reason (SQLite `+` on TEXT",
            "    // would go through REAL arithmetic).",
            "    let outDone = 0n;",
            "    let inDone = 0n;",
            "    if (key !== null && key !== undefined) {",
            f"      const outRows = this.{n}_db.prepare(\"SELECT amount FROM ledger WHERE key = ?\").all(key + \":out\");",
            f"      const inRows = this.{n}_db.prepare(\"SELECT amount FROM ledger WHERE key = ?\").all(key + \":in\");",
            "      let outSum = 0n;",
            "      let inSum = 0n;",
            "      for (const r of outRows) { outSum += BigInt(r.amount); }",
            "      for (const r of inRows) { inSum += BigInt(r.amount); }",
            "      outDone = -outSum;",
            "      inDone = inSum;",
            "      if (outDone < 0n) { outDone = 0n; }",
            "      if (outDone > amount) { outDone = amount; }",
            "      if (inDone < 0n) { inDone = 0n; }",
            "      if (inDone > amount) { inDone = amount; }",
            "      if (outDone >= amount && inDone >= amount) {",
            "        return true;",
            "      }",
            "    }",
            "    const outLeft = amount - outDone;",
            "    const inLeft = amount - inDone;",
            f"    this.{n}_db.exec(\"BEGIN IMMEDIATE\");",
            "    try {",
            "      if (outLeft > 0n) {",
            f"        const row = this.{n}_db.prepare(\"SELECT balance FROM balances WHERE user = ?\").get(fromId);",
            "        const cur = row ? BigInt(row.balance) : 0n;",
            "        if (cur < outLeft) {",
            f"          this.{n}_db.exec(\"ROLLBACK\");",
            "          return false;",
            "        }",
            "        const next = cur - outLeft;",
            f"        this.{n}_db.prepare(\"INSERT INTO balances (user, balance) VALUES (?, ?) ON CONFLICT(user) DO UPDATE SET balance = excluded.balance\").run(fromId, next.toString());",
            "        await this._ledgerUpsert(fromId, -outLeft, key === null || key === undefined ? null : key + \":out\");",
            "      }",
            "      if (inLeft > 0n) {",
            f"        const row = this.{n}_db.prepare(\"SELECT balance FROM balances WHERE user = ?\").get(toId);",
            "        const cur = row ? BigInt(row.balance) : 0n;",
            "        const next = checkInt(cur + inLeft);",
            f"        this.{n}_db.prepare(\"INSERT INTO balances (user, balance) VALUES (?, ?) ON CONFLICT(user) DO UPDATE SET balance = excluded.balance\").run(toId, next.toString());",
            "        await this._ledgerUpsert(toId, inLeft, key === null || key === undefined ? null : key + \":in\");",
            "      }",
            f"      this.{n}_db.exec(\"COMMIT\");",
            "    } catch (e) {",
            f"      this.{n}_db.exec(\"ROLLBACK\");",
            "      throw e;",
            "    }",
            "    return true;",
            "  }",
            "",
            "  async _ledgerUpsert(userId, amount, key) {",
            "    // A ledger leg, upserted with JS-side arithmetic",
            "    // (exact BigInt; see _transfer). Called only inside",
            "    // a transaction held by the caller.",
            "    if (key === null || key === undefined) {",
            f"      this.{n}_db.prepare(\"INSERT INTO ledger (user, amount, key, created_at) VALUES (?, ?, ?, ?)\").run(userId, amount.toString(), null, Math.floor(Date.now() / 1000));",
            "      return;",
            "    }",
            f"    const existing = this.{n}_db.prepare(\"SELECT amount FROM ledger WHERE key = ?\").get(key);",
            "    if (existing) {",
            "      const total = BigInt(existing.amount) + amount;",
            f"      this.{n}_db.prepare(\"UPDATE ledger SET amount = ? WHERE key = ?\").run(total.toString(), key);",
            "    } else {",
            f"      this.{n}_db.prepare(\"INSERT INTO ledger (user, amount, key, created_at) VALUES (?, ?, ?, ?)\").run(userId, amount.toString(), key, Math.floor(Date.now() / 1000));",
            "    }",
            "  }",
        ]

    def _emit_table_methods(self, name: str, table: A.TableDecl) -> list[str]:
        """`get` / `set` / `delete` for one declared table. A row is
        a plain object (attribute access works for its fields);
        `set` takes such a row (or a struct - its fields are own
        enumerable properties). Int columns are stored as TEXT and
        converted to BigInt on read, back to strings on write (J9);
        bool columns are 0/1."""
        n = name
        key_col = table.columns[0].name if table.columns else "id"
        cols = [c.name for c in table.columns]
        # Per-column read conversions (from the stored form).
        read_parts = []
        for c in table.columns:
            if c.type.name == "int":
                read_parts.append(f"{c.name}: BigInt(row.{c.name})")
            elif c.type.name == "bool":
                read_parts.append(f"{c.name}: row.{c.name} !== 0")
            else:
                read_parts.append(f"{c.name}: row.{c.name}")
        read_obj = "{ " + ", ".join(read_parts) + " }"
        # Per-column write conversions (to the stored form).
        write_cases = []
        for c in table.columns:
            if c.type.name == "int":
                write_cases.append(
                    f"      if (key === { _js_str(c.name) }) {{ value = value.toString(); }}")
            elif c.type.name == "bool":
                write_cases.append(
                    f"      if (key === { _js_str(c.name) }) {{ value = value ? 1 : 0; }}")
        write_block = "\n".join(write_cases) if write_cases else ""
        lines = [
            "",
            f"  async {n}_get(table, key) {{",
            f"    const row = this.{n}_db.prepare(`SELECT * FROM ${{table}} WHERE {key_col} = ?`).get(key);",
            "    if (!row) {",
            "      return null;",
            "    }",
            f"    return {read_obj};",
            "  }",
            "",
            f"  async {n}_set(table, row) {{",
            "    const data = { ...row };",
            "    const cols = Object.keys(data);",
            "    const placeholders = cols.map(() => \"?\").join(\", \");",
            "    const updates = cols.map((c) => `${c} = excluded.${c}`).join(\", \");",
            "    const values = cols.map((key) => {",
            "      let value = data[key];",
        ]
        if write_block:
            lines.append(write_block)
        lines.extend([
            "      return value;",
            "    });",
            f"    this.{n}_db.prepare(`INSERT INTO ${{table}} (${{cols.join(\", \")}}) VALUES (${{placeholders}}) ON CONFLICT({key_col}) DO UPDATE SET ${{updates}}`).run(...values);",
            "  }",
            "",
            f"  async {n}_delete(table, key) {{",
            f"    this.{n}_db.prepare(`DELETE FROM ${{table}} WHERE {key_col} = ?`).run(key);",
            "  }",
        ])
        return lines

    # -- command registration (the standalone deploy script, J10) ------

    def _emit_deploy_file(self, program: A.Program,
                          bot_filename: str) -> str:
        """`<stem>_deploy.js`: the standalone command-registration
        script (the track's deploy-commands.ts, in plain JS). It
        requires the bot file for its `commands` array (requiring
        the bot file does NOT run main() - main() is guarded by
        `require.main === module`). Reads DISCORD_TOKEN /
        DISCORD_CLIENT_ID / DISCORD_GUILD_ID from the environment;
        a guild deploy is instant (dev), a global deploy takes up
        to an hour to propagate. Run once, then `node <stem>.js`."""
        stem = Path(bot_filename).stem
        # The header names the SOURCE (.newr), like every other
        # emitted file - not the deploy file itself.
        lines = self._header(self.main_path,
                             "Command registration - run once, then run the bot.")
        lines.append('"use strict";')
        lines.append("")
        lines.append("const { REST, Routes } = require(\"discord.js\");")
        lines.append(f"const {{ commands }} = require(\"./{stem}.js\");")
        lines.append("")
        lines.append("async function main() {")
        lines.append("  const token = process.env.DISCORD_TOKEN;")
        lines.append("  const clientId = process.env.DISCORD_CLIENT_ID;")
        lines.append("  const guildId = process.env.DISCORD_GUILD_ID;")
        lines.append("  if (!token || !clientId) {")
        lines.append("    throw new Error(\"set DISCORD_TOKEN and DISCORD_CLIENT_ID\");")
        lines.append("  }")
        lines.append("  const body = commands.map((command) => command.data.toJSON());")
        lines.append("  const rest = new REST({ version: \"10\" }).setToken(token);")
        lines.append("  if (guildId) {")
        lines.append("    await rest.put(Routes.applicationGuildCommands(clientId, guildId), { body });")
        lines.append("    console.log(`Registered ${body.length} command(s) in guild ${guildId}.`);")
        lines.append("  } else {")
        lines.append("    await rest.put(Routes.applicationCommands(clientId), { body });")
        lines.append("    console.log(`Registered ${body.length} command(s) globally.`);")
        lines.append("  }")
        lines.append("}")
        lines.append("")
        lines.append("")
        lines.append("main().catch((e) => {")
        lines.append("  console.error(e);")
        lines.append("  process.exit(1);")
        lines.append("});")
        return "\n".join(lines) + "\n"




# ---------------------------------------------------------------- entry points


def emit_program_js(program: A.Program, *, main_path: str,
                    sources: dict[str, str],
                    line_comments: bool = False) -> EmitResult:
    """Emit a CHECKED program to JavaScript - one `.js` per `.newr` file.

    `main_path` is the input file as passed (D2: the header names
    it); `sources` maps file paths to their text (for diagnostics
    and `--line-comments`). The result's `files` maps an output
    basename (`hello.js`) to its text; `main_file` is the main
    output's basename; `uses_rt` says whether any file imports
    `newr_rt.js` (the caller copies `compiler/newr_rt.js` next to the
    output when it is true, D4's JS twin)."""
    em = _JSEmitter(program, main_path=main_path, sources=sources,
                    line_comments=line_comments)
    files, uses_rt = em._emit_file_set(program, main_path)
    if program.bot is not None:
        # The standalone registration script (J10) rides along as
        # a second output file.
        deploy = em._emit_deploy_file(program, Path(main_path).stem + ".js")
        files[Path(main_path).stem + "_deploy.js"] = deploy
    return EmitResult(files=files,
                      main_file=Path(main_path).stem + ".js",
                      uses_rt=uses_rt)



# `emit_program` is the name the tests and the CLI use; the `_js`
# suffix marks the implementation (the Python emitter's entry is
# `emit_program` in compiler/emitter.py).
emit_program = emit_program_js


def emit_deploy(program: A.Program, bot_filename: str) -> str | None:
    """The standalone command-registration script for a bot program
    (`<stem>_deploy.js`); None for a general program."""
    if program.bot is None:
        return None
    em = _JSEmitter(program, main_path=bot_filename, sources={},
                    line_comments=False)
    return em._emit_deploy_file(program, bot_filename)
