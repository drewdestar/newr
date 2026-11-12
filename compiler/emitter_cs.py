"""The C# emitter (M4, SPEC.md §18).

``newr build --target cs`` lowers a *checked* program (the front
end has already run: parse -> resolve -> check) to readable C# —
one `.newr` file -> one `.cs` file, structure preserved, user names
verbatim, helpers only in `newr_rt.cs` (copied next to the output
when used, D4's C# twin), plus a `<stem>.csproj` (console app,
net8.0, Nullable disabled).

Readability is a requirement, not a nicety: the output is code a
human would write — the C# goldens (`goldens/cs/`) are the style
oracle, and the emitter reproduces them byte-for-byte
(BUILD_REPORT.md §21, C1–C13).

The emitter NEVER runs on an unchecked program: the CLI checks first
and exits 4 on errors. If it meets a node without a type (which only
happens on an unchecked program), it raises `EmitterError` — a clean
refusal, never a TypeError traceback.

Module system (K1): one public class per file, named after the file
stem verbatim (the class name IS the file stem — C# requires the
public class to live in the same-named file, same as C#). File
modules become their own public classes in their own files;
imported names are qualified `Module.name`. A program with no
`main` file stem gets `Main`. C# floor: .NET 8 LTS (SDK 8.0.427
verified on the build VM; the emitted code uses no feature newer
than C# 12).

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

# Names the emitter itself defines in an emitted file. A user name
# colliding with one of these is mangled with a trailing underscore
# (D20, mirrored from the Python/JS emitters). The `NewrRt` runtime
# helpers are called qualified (`NewrRt.add`), so user names that
# match a helper are harmless — but the class name `NewrRt` itself,
# the `main` method, its `args` parameter, and the java.lang type
# names the emitter writes (`String`, `Long`, ...) must not be
# shadowed by a user struct/enum/fn name in the same file.
_RT_HELPERS = frozenset({
    "Add", "Sub", "Mul", "Neg", "PowInt", "PowFloat", "Tdiv", "Tmod",
    "Fmod", "Fdiv", "RoundHalfAway", "ToInt", "ToFloat", "ToBool",
    "ToStr", "FloatStr", "CheckInt", "AsError", "OptChain",
    "OptChainVal", "OptChainNullable", "Coalesce", "CoalesceVal", "ListGet", "ListIndex",
    "ListContains", "SetRemove", "UnwrapOpt", "UnwrapOptVal",
    "TryResult", "TimeFormat", "FileRead", "FileWrite", "FileAppend",
    "FileExists", "RandomInt", "RandomChoice", "RandomShuffle",
    "Eq", "Range", "Print", "Input", "Ok", "Err", "Chars", "SplitStr",
    "StrIndex", "AbsLong", "LenOf", "DictOf", "EnvGet", "EnvArgs",
    "HashOf", "StructHash", "SetMainArgs",
})
_RT_CLASSES = frozenset({
    "NewrError", "NewrOverflowError", "NewrDivisionByZeroError",
    "Result", "INewrRepr",
})
_CS_LANG_TYPES = frozenset({
    "string", "object", "Math", "long", "double", "bool", "int",
    "float", "decimal", "char", "byte", "short", "uint", "ulong",
    "ushort", "sbyte", "void", "dynamic", "var", "nint", "nuint",
    "System", "Thread", "Exception", "List", "Dictionary",
    "HashSet", "LinkedList", "HashMap", "Collection", "Func",
    "Action", "Predicate", "StringBuilder", "StringBuffer",
    "BigInteger", "BigDecimal", "DateTime", "DateTimeOffset",
    "TimeSpan", "Guid", "Array", "Enumerable", "Console",
    "Environment", "Convert", "Type",
})
_EMITTER_RESERVED = frozenset({
    "Main", "args", "NewrRt", "Bot", "Store",
    "store", "economy", "commands", "modalHandlers", "bot",
}) | _RT_CLASSES | _CS_LANG_TYPES

# C# reserved words (C# 12) + contextual keywords the emitter
# writes in positions where they would collide.
_CS_KEYWORDS = frozenset({
    "abstract", "as", "base", "bool", "break", "byte", "case",
    "catch", "char", "checked", "class", "const", "continue",
    "decimal", "default", "delegate", "do", "double", "else",
    "enum", "event", "explicit", "extern", "false", "finally",
    "fixed", "float", "for", "foreach", "goto", "if", "implicit",
    "in", "int", "interface", "internal", "is", "lock", "long",
    "namespace", "new", "null", "object", "operator", "out",
    "override", "params", "private", "protected", "public",
    "readonly", "ref", "return", "sbyte", "sealed", "short",
    "sizeof", "stackalloc", "static", "string", "struct", "switch",
    "this", "throw", "true", "try", "typeof", "uint", "ulong",
    "unchecked", "unsafe", "ushort", "using", "virtual", "void",
    "volatile", "while", "add", "alias", "ascending", "async",
    "await", "by", "descending", "dynamic", "equals", "from",
    "get", "global", "group", "into", "join", "let", "nameof",
    "not", "notnull", "on", "or", "orderby", "partial", "record",
    "remove", "required", "select", "set", "unmanaged", "value",
    "var", "when", "where", "with", "yield",
})


def _cs_str(value: str) -> str:
    """Render a newr string value as a C# string literal.

    Always double quotes. Non-ASCII printable characters are
    emitted literally (UTF-8 source); control characters use their
    escapes. `\\uXXXX` covers the rest.
    """
    out = []
    for ch in value:
        o = ord(ch)
        if ch == "\\":
            out.append("\\\\")
        elif ch == '"':
            out.append('\\"')
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
        elif o < 0x20 or o == 0x7F:
            out.append(f"\\u{o:04x}")
        else:
            out.append(ch)
    return '"' + "".join(out) + '"'


def _cs_float(value: float) -> str:
    """Render a float literal. Python repr is also a valid C#
    double literal ("1e+16", "5e-324", "1.5"). The non-finite
    spellings are not literals in C#, so they become the
    Double constants (an out-of-range literal parses to inf in
    Python and would otherwise emit `inf`, a compile error)."""
    if value != value:  # NaN
        return "double.NaN"
    if value == float("inf"):
        return "double.PositiveInfinity"
    if value == float("-inf"):
        return "double.NegativeInfinity"
    return repr(value)


# C# precedence (higher binds tighter), loosest -> tightest.
# The emitter's precedence-climbing printer uses this table; newr's
# own table (SPEC §9.2) maps onto it, with the traps documented
# where they bite — notably unary `-`: `-x ** y` parses as
# `-(x ** y)` in newr, and C# has no `**`, so the power helper
# call binds as a primary; and `!a == b` is `(!a) == b` in C#,
# not `!(a == b)` as newr's `not` reads it.
_PREC = {
    "ternary": 0,
    "or": 1,
    "and": 2,
    "not": 3,
    "cmp": 4,      # == != < <= > >= in
    "add": 9,      # + -
    "mul": 10,     # * / %
    "unary": 11,   # -x !x
    "pow": 12,     # ** (emitted as a helper call — primary)
    "primary": 13,  # calls, indexing, member access, atoms
}

_BINOP_PREC = {
    "||": 1, "&&": 2,
    "==": 4, "!=": 4, "<": 4, "<=": 4, ">": 4, ">=": 4, "in": 4,
    "+": 9, "-": 9, "*": 10, "/": 10, "%": 10, "**": 12,
}


def _is_int(t: Type | None) -> bool:
    return t is not None and t.name == "int" and not t.nullable


def _is_cs_value_type(t: Type | None) -> bool:
    """True when the type is a C# VALUE type (a struct): int,
    float, bool and duration all lower to `long`/`double`/`bool`.
    Everything else (str, list, dict, set, struct, enum, error,
    Result, fn, object) lowers to a reference type — the emitted
    structs/enums are classes, not C# structs. The nullable
    helpers (OptChain/Coalesce/UnwrapOpt) need this split: a
    value type's nullable form is `Nullable<T>` (a different
    type from `T`), while a reference type's nullable form is
    the same type with a nullable annotation."""
    return t is not None and t.name in ("int", "float", "bool",
                                        "duration")


def _is_float(t: Type | None) -> bool:
    return t is not None and t.name == "float" and not t.nullable


def _is_str(t: Type | None) -> bool:
    return t is not None and t.name == "str" and not t.nullable


def _is_bool(t: Type | None) -> bool:
    return t is not None and t.name == "bool" and not t.nullable


def _is_none(t: Type | None) -> bool:
    return t is not None and t.name == "none" and not t.nullable


def _is_error(t: Type | None) -> bool:
    return t is not None and t.name == "error" and not t.nullable


def _is_result(t: Type | None) -> bool:
    return t is not None and t.name == "Result"


def _is_nullable(t: Type | None) -> bool:
    return t is not None and t.nullable


def _is_list(t: Type | None) -> bool:
    return t is not None and t.name == "list"


def _is_dict(t: Type | None) -> bool:
    return t is not None and t.name == "dict"


def _is_set(t: Type | None) -> bool:
    return t is not None and t.name == "set"


def _is_fn(t: Type | None) -> bool:
    return t is not None and t.name == "fn"


def _is_struct_or_enum(t: Type | None) -> bool:
    if t is None or t.name in (
            "int", "float", "str", "bool", "list", "dict", "set",
            "Result", "fn", "error", "none", "duration"):
        return False
    return True


def _needs_eq(t: Type | None) -> bool:
    """True when `==` on a value of this type needs structural
    equality (`NewrRt.eq`) instead of `==`: lists, dicts, sets,
    Results, structs and enums (and nullable forms of them — `eq`
    handles `null` on either side). Primitives, functions, errors
    and Discord entities compare by identity (`==`)."""
    if t is None:
        return False
    if t.name in ("list", "dict", "set", "Result"):
        return True
    if _is_struct_or_enum(t):
        return True
    return False


# ---------------------------------------------------------------- scopes


@dataclass
class _Binding:
    """One visible name: its C# rendering and its newr type.

    `java` is the C# name (possibly renamed); `interp` is the form
    used inside an interpolation (for handler bindings: the mention /
    name form); `value` is the form used as a value. For ordinary
    variables all three are the C# name. `kind` is one of
    "param" / "local" / "global-let" / "fn" / "struct" / "enum" /
    "module" / "store" / "handler" / "builtin" / "named-module" /
    "file-module". `module` is the real module name for a module
    binding (the `java` name is the alias when the import is
    aliased).
    """

    java: str
    type: Type | None = None
    kind: str = "local"
    interp: str | None = None
    module: str | None = None

    @property
    def value(self) -> str:
        return self.java

    def interp_form(self) -> str:
        return self.interp if self.interp is not None else self.java


class _CSEmitter:
    """State for one emitted file set (main file + its imports)."""

    def __init__(self, program: A.Program, *, main_path: str,
                 sources: dict[str, str], line_comments: bool = False,
                 output_stem: str | None = None):
        self.program = program
        self.main_path = main_path
        self.sources = sources
        self.line_comments = line_comments
        # The file stem of the main output file (the public class
        # name). The CLI passes the `-o` stem; the default is the
        # main source stem, falling back to "Main".
        self.output_stem = output_stem or "Main"
        # Imports collected while emitting (per output file — reset
        # for each module file).
        self.uses_discord: bool = False
        self.discord_names: set[str] = set()
        self.uses_sqlite: bool = False
        # This file uses `db.*` (set by `_emit_db_call`) — the
        # `NewrDb` helper class is emitted into the file only then.
        self._file_uses_db: bool = False
        self.uses_http: bool = False
        # This file uses `http.*` (set by `_emit_http_call`), the
        # `NewrHttp` helper class is emitted into the file only then.
        self._file_uses_http: bool = False
        self.uses_json: bool = False
        # This file uses `json.*` (set by `_emit_json_call`), the
        # `NewrJson` helper class is emitted into the file only then.
        self._file_uses_json: bool = False
        self.uses_ui: bool = False
        # This file uses `ui.*` (set by `_emit_ui_call`), the
        # `NewrUi` helper class is emitted into the file only then.
        self._file_uses_ui: bool = False
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
        # C# names already taken at module level (for collision
        # aware allocation in _bind_module_level).
        self._module_used: set[str] = set()
        # C# names already used in the current C# function scope
        # (for rename allocation in _bind_fresh). Reset per function.
        self._fn_used: set[str] = set()
        # Hoists for the statement being emitted: each is a list of
        # lines (base indent 0) to emit before the statement —
        # hoisted lambda consts and lambda snapshot locals.
        self._hoists: list[list[str]] | None = None
        # True while emitting the DIRECT top-level statement list of
        # a file (main() / the module static block). A `let` there
        # assigns the static field instead of declaring a local
        # (K2) — a local would shadow the field and top-level
        # functions would read the still-zero field (conformance
        # toplevel_order). Nested blocks reset it (their `let` is a
        # genuine local, even when it shadows a module name).
        self._in_top_level = False
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
        # Whether a `log <expr>` statement was emitted.
        self._used_log = False
        # --- bot-emitter state ---
        # Handler-context stack: one dict per handler body being
        # emitted (command / event / task / component / timeout).
        # Keys: "kind", "reply" (how a `reply` is answered),
        # "locale" (the expression for `t()`'s locale argument).
        self._handler_ctx_stack: list[dict] = []
        # Component rows collected while emitting handler bodies
        # (each a list of lines, inserted before the answer they
        # attach to — the C# twin of the Python emitter's D40).
        self._pending_components: list[dict] = []
        self._component_counter = 0
        # The last "answer" line (a Reply / Followup / Edit) emitted
        # in the block currently being built, for component
        # attachment: (index into the block's line list, kind).
        self._last_answer: tuple[int, str] | None = None
        # Counter for unique temp names within a file.
        self._temp_counter = 0
        # Whether the file being emitted is the entry file (set by
        # _emit_file_set before _emit_one).
        self._is_main_file = True
        # The entry file's class name, for a file module (None on
        # the entry file itself). A file module reads the shared
        # `NewrUi` helper as `<root>.NewrUi` (M9 L2 twin of the rs
        # E0308 fix): the helper lives once, in the entry file.
        # Set by _emit_file_set when it creates sub-emitters.
        self._root_stem: str | None = None
        # Whether any file in the transitive import closure uses
        # `ui.*` (M9 L2): the entry file emits `NewrUi` once when
        # any file uses it, so a module's `<root>.NewrUi`
        # reference resolves. Filled by _emit_file_set BEFORE the
        # entry file is emitted.
        self._closure_uses_ui: bool = False

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
        """The C# name for a newr name, before collision checks:
        C# keywords and emitter-reserved names get a trailing
        underscore (D20, C# twin)."""
        if name in _CS_KEYWORDS or name in _EMITTER_RESERVED:
            return name + "_"
        return name

    def _member_name(self, name: str, class_name: str) -> str:
        """A member name inside class `class_name` (M9 L2): C#
        forbids a member with the same name as its enclosing type
        (CS0542), so a colliding name gets a trailing underscore
        after keyword/reserved mangling. The definition site
        (`_bind_module_level`, class `self.output_stem`) and every
        cross-file use site (class = the imported file's stem)
        compute the name from this one rule, so they agree."""
        java = self._mangle(name)
        if java == class_name:
            java += "_"
        return java

    def _ui_ref(self) -> str:
        """The `NewrUi` helper reference for this file (M9 L2):
        bare on the entry file, `<root>.NewrUi` on a file module,
        where `<root>` is the entry file's class. Every `NewrUi`
        use (the Widget type, the constructors, `Render`, `Show`,
        `Run`) goes through here, so all files share the one
        Widget type emitted in the entry file."""
        if self._root_stem is None:
            return "NewrUi"
        return f"{self._root_stem}.NewrUi"

    def _bind(self, name: str, type: Type | None = None,
              kind: str = "local", interp: str | None = None) -> _Binding:
        """Bind `name` in the current scope.

        C# has real block scope (`long x` in an `if` block does
        not clobber the outer name), so — unlike the Python emitter
        — shadowing an enclosing name needs NO renaming: the inner
        binding is a different variable, exactly as newr reads it.
        Only keyword / emitter-reserved mangling applies (D20).
        """
        java = self._mangle(name)
        binding = _Binding(java=java, type=type, kind=kind, interp=interp)
        self._scopes[-1][name] = binding
        return binding

    def _bind_fresh(self, name: str, type: Type | None = None,
                    kind: str = "local") -> _Binding:
        """Bind `name` in the current scope WITHOUT renaming against
        enclosing scopes — for function/lambda parameters, where
        shadowing an enclosing name is the correct semantics. Only
        the current function's used-name set is consulted, so two
        parameters mangling to the same C# name still get distinct
        names (duplicate parameters are a compile error)."""
        base = self._mangle(name)
        java = base
        n = 1
        while java in self._fn_used:
            java = f"{base}_{n}"
            n += 1
        self._fn_used.add(java)
        binding = _Binding(java=java, type=type, kind=kind)
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
        mangling. A member named the same as its enclosing class
        is also mangled (M9 L2, CS0542) — except an import binding,
        which is not a member of the class (its java name is the
        lookup key for `imported_modules`)."""
        if kind in ("module", "file-module"):
            java = self._mangle(name)
        else:
            java = self._member_name(name, self.output_stem)
        if java in self._module_used:
            n = 1
            while f"{java}_{n}" in self._module_used:
                n += 1
            java = f"{java}_{n}"
        self._module_used.add(java)
        binding = _Binding(java=java, type=type, kind=kind)
        self._scopes[0][name] = binding
        return binding

    # ------------------------------------------------------------ imports

    def _need_rt(self, helper: str):
        self.rt_helpers.add(helper)

    def _need_discord(self, name: str):
        # `name` is a fully-qualified Discord.Net type
        # (e.g. "Discord.IUser"); the `using` needs only its
        # namespace ("Discord").
        self.uses_discord = True
        self.discord_names.add(name.rsplit(".", 1)[0])

    def _need_sqlite(self):
        self.uses_sqlite = True

    def _need_http(self):
        self.uses_http = True
        self._file_uses_http = True

    def _need_json(self):
        self.uses_json = True
        self._file_uses_json = True

    def _need_ui(self):
        self.uses_ui = True
        self._file_uses_ui = True

    def _render_imports(self) -> list[str]:
        """The `using` block: `System` and
        `System.Collections.Generic` are unconditional (emitted code
        uses `List<>`, `Dictionary<,>`, `HashSet<>`, `Func<>`,
        `Console` and `DateTimeOffset` even in programs that use no
        runtime helper at all). Discord namespaces are added when
        a bot file uses them (one `using` per namespace). The
        runtime lives in the global namespace, so it needs no
        `using` at all."""
        lines: list[str] = ["using System;",
                            "using System.Collections.Generic;"]
        if self._is_bot:
            lines.append("using System.Threading.Tasks;")
        if self.uses_sqlite:
            lines.append("using Microsoft.Data.Sqlite;")
        if self.uses_http:
            # HttpListener, HttpClient, threads, streams: stdlib only,
            # no package reference is added for http/json.
            lines.append("using System.Net;")
            lines.append("using System.Net.Http;")
            lines.append("using System.Net.Sockets;")
            lines.append("using System.Threading;")
            lines.append("using System.Threading.Tasks;")
            lines.append("using System.IO;")
        if self.uses_ui:
            # Avalonia (the ui toolkit, SPEC.md section 12.8): the
            # inline NewrUi helper builds controls in code, no XAML.
            lines.append("using Avalonia;")
            lines.append("using Avalonia.Controls;")
            lines.append("using Avalonia.Controls.ApplicationLifetimes;")
            lines.append("using Avalonia.Input;")
            lines.append("using Avalonia.Layout;")
            lines.append("using Avalonia.Themes.Fluent;")
        if self.uses_http or self.uses_json or self.uses_ui:
            # The inline NewrHttp/NewrJson helpers use JSON parsing,
            # reflection for struct construction, and text encoding.
            lines.append("using System.Text;")
            lines.append("using System.Text.Json;")
            lines.append("using System.Reflection;")
            lines.append("using System.Globalization;")
        for ns in sorted(self.discord_names):
            lines.append(f"using {ns};")
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

    # ------------------------------------------------------------ types

    def _cs_type(self, t: Type | None) -> str:
        """The C# type for a newr type in a declaration position
        (a local, parameter, field or return type). C# has no
        separate boxed classes: `long`/`double`/`bool` serve both
        as the value type and the generic argument, and a nullable
        value type is `long?`/`double?`/`bool?` (NOT a separate
        class — the `T?`-collapses-to-`T` lesson from the runtime
        applies to the emitter too: a `long?` local is a
        `Nullable<long>`, and passing it where a `long` is
        expected is a compile error). A `none` return is `void`;
        a `none` VALUE is `object` (only `null` inhabits it —
        C# has no `Void` variable type)."""
        if t is None:
            self._fail("an expression has no type: the emitter cannot "
                       "handle it here")
        if t.name == "int":
            return "long?" if t.nullable else "long"
        if t.name == "float":
            return "double?" if t.nullable else "double"
        if t.name == "bool":
            return "bool?" if t.nullable else "bool"
        if t.name == "str":
            return "string"
        if t.name == "none":
            return "object"
        if t.name == "object":
            # The union type of conflicting enum variant fields.
            return "object"
        if t.name == "__unknown__":
            # The checker leaves `__unknown__` params on a bare
            # builtin used as a value (`let f = len`); it is not a
            # real type. In a declaration position it must become
            # the generic object, matching the generic fallback in
            # _emit_builtin_value (type_model.newr, cs 74/75,
            # 7 Nov). The first fix only patched the value
            # expression; the field type is rendered here, so the
            # build still failed until this was added.
            return "object"
        if t.name == "error":
            return "NewrRt.NewrError"
        if t.name == "list":
            return f"List<{self._cs_boxed_type(t.params[0])}>"
        if t.name == "dict":
            return ("Dictionary<string, "
                    f"{self._cs_boxed_type(t.params[1])}>")
        if t.name == "set":
            return f"HashSet<{self._cs_boxed_type(t.params[0])}>"
        if t.name == "Result":
            return ("NewrRt.Result<"
                    f"{self._cs_boxed_type(t.params[0])}, "
                    f"{self._cs_boxed_type(t.params[1])}>")
        if t.name == "fn":
            return self._cs_fn_type(t)
        if t.name == "duration":
            return "long"
        if t.name in ("user", "member", "guild", "channel", "role",
                      "interaction", "embed", "message"):
            return self._cs_entity_type(t.name)
        if t.name == "Widget":
            # The ui module's opaque value type (SPEC.md section
            # 12.8): the Widget model class inside the inline
            # NewrUi helper, emitted once in the entry file (M9
            # L2) — a file module reads it as `<root>.NewrUi`.
            return f"{self._ui_ref()}.Widget"
        # A struct or enum: the (possibly module-qualified) class.
        return self._struct_cs_name(t.name)

    def _cs_boxed_type(self, t: Type | None) -> str:
        """The type-argument form. In C# this is the same as the
        declaration form for primitives (`long` is both the value
        type and the generic argument); a nullable value type in a
        generic argument is `long?` (a `Nullable<long>`), which
        is exactly what a `T?` element needs."""
        if t is None:
            self._fail("an expression has no type: the emitter cannot "
                       "handle it here")
        if t.name == "none":
            return "object"
        return self._cs_type(t)

    def _cs_fn_type(self, t: Type) -> str:
        """`fn(A, B) -> R` -> `Func<A, B, R>`. C#'s `Func` covers
        up to 16 parameters, so there is no v1 arity limit (unlike
        C#'s Fn0–Fn8). A fn value is invoked directly (`f(args)`,
        delegates are callable in C#) — no `.get()`/`.apply()`."""
        params = t.params[:-1]
        ret = t.params[-1]
        args = ", ".join(self._cs_boxed_type(p) for p in params)
        ret_s = self._cs_boxed_type(ret)
        if args:
            return f"Func<{args}, {ret_s}>"
        return f"Func<{ret_s}>"

    def _cs_entity_type(self, name: str) -> str:
        """A Discord entity type (SPEC §7.1) -> its Discord.Net
        interface/class. Only reachable in bot files."""
        mapping = {
            "user": "Discord.IUser",
            "member": "Discord.IGuildUser",
            "guild": "Discord.IGuild",
            "channel": "Discord.IMessageChannel",
            "role": "Discord.IRole",
            "interaction": "Discord.IDiscordInteraction",
            "embed": "Discord.Embed",
            "message": "Discord.IUserMessage",
        }
        fq = mapping[name]
        self._need_discord(fq)
        return fq.rsplit(".", 1)[-1]

    def _struct_cs_name(self, name: str) -> str:
        """The C# class name for a struct/enum type: bare in its
        own file, `Module.Name` when imported (the binding records
        the module). An imported struct/enum reached only through
        its TYPE (a `let c = helper.Shade.Red` has type `Shade`
        with no module on the Type — the Type class has no module
        field) is found by searching the imported file modules for
        the declaration and qualifying with the module's class
        name — the file STEM, not the import alias (K1;
        conformance import_enum)."""
        binding = self._lookup(name)
        if binding is not None and binding.kind in ("struct", "enum"):
            if binding.module:
                return f"{binding.module}.{binding.java}"
            return binding.java
        for bound, sub in self.program.modules.items():
            for decl in list(sub.structs) + list(sub.enums):
                if decl.name == name:
                    stem = self.imported_modules.get(bound, bound)
                    return f"{stem}.{self._member_name(name, stem)}"
        return self._mangle(name)

    # ------------------------------------------------------------ expressions

    def emit_expr(self, node: A.Expr, min_prec: int = 0,
                  fn_target: Type | None = None) -> str:
        """Emit an expression, parenthesised when its precedence is
        below `min_prec`. `fn_target` is the expected fn type when
        the expression is a lambda — the context's target type, so
        the lambda needs no cast (C# lambdas are poly
        expressions)."""
        text, prec = self._emit_expr(node, fn_target)
        if prec < min_prec:
            return f"({text})"
        return text

    def _emit_expr(self, node: A.Expr,
                   fn_target: Type | None = None) -> tuple[str, int]:
        if isinstance(node, A.Lit):
            return self._emit_lit(node)
        if isinstance(node, A.Name):
            return self._emit_name(node, fn_target)
        if isinstance(node, A.Interp):
            return self._emit_interp(node)
        if isinstance(node, A.ListLit):
            if not node.elements:
                elem = self._type_of(node).params[0]
                return (f"new List<{self._cs_boxed_type(elem)}>()",
                        _PREC["primary"])
            elem = self._type_of(node).params[0]
            parts = ", ".join(
                self.emit_expr(i, 0,
                               fn_target=elem if _is_fn(elem) else None)
                for i in node.elements)
            # A collection expression (not `new List<T>(...)` over
            # an array — a collection initializer keeps null
            # elements, and a list[T?] may hold null) with an
            # explicit element type, so a literal of only nulls
            # still has the right type.
            return (f"new List<{self._cs_boxed_type(elem)}> "
                    f"{{ {parts} }}",
                    _PREC["primary"])
        if isinstance(node, A.DictLit):
            if not node.pairs:
                val = self._type_of(node).params[1]
                return ("new Dictionary<string, "
                        f"{self._cs_boxed_type(val)}>()",
                        _PREC["primary"])
            val = self._type_of(node).params[1]
            parts = []
            for k, v in node.pairs:
                parts.append(
                    f"{self.emit_expr(k, 0)}, "
                    f"{self.emit_expr(v, 0, fn_target=val if _is_fn(val) else None)}")
            # NewrRt.DictOf (not a collection initializer over
            # KeyValuePair — that rejects null values for a
            # Dictionary<string, T?> only when the VALUE type is a
            # non-nullable struct; DictOf takes object? pairs and
            # casts, so nulls always work), with an explicit
            # value-type witness.
            return (f"NewrRt.DictOf<{self._cs_boxed_type(val)}>"
                    f"({', '.join(parts)})",
                    _PREC["primary"])
        if isinstance(node, A.BinOp):
            return self._emit_binop(node)
        if isinstance(node, A.UnaryOp):
            return self._emit_unary(node)
        if isinstance(node, A.Lambda):
            return self._emit_lambda(node, fn_target)
        if isinstance(node, A.Ternary):
            cond = self.emit_expr(node.cond, _PREC["or"])
            t = self._type_of(node)
            tgt = t if _is_fn(t) else None
            then = self.emit_expr(node.then_expr, 0, fn_target=tgt)
            els = self.emit_expr(node.else_expr, 0, fn_target=tgt)
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
            inner_t = self._type_of(node).params[0]
            inner = self.emit_expr(
                node.expr, 0,
                fn_target=inner_t if _is_fn(inner_t) else None)
            return f"NewrRt.TryResult(() => {inner})", _PREC["primary"]
        if isinstance(node, A.TCall):
            return self._emit_t_call(node)
        if isinstance(node, A.EmbedExpr):
            return self._emit_embed(node)
        if isinstance(node, (A.Member, A.Index, A.Call)):
            # Any optional link anywhere in the chain routes the
            # WHOLE chain through _emit_chain (D9) — including
            # trailing non-optional links (`a?.b.c`), which must
            # short-circuit too.
            if self._chain_has_optional(node):
                return self._emit_chain(node)
            if isinstance(node, A.Member):
                return self._emit_member(node, fn_target)
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
            # long.MinValue has no positive literal in C#
            # (`9223372036854775808L` is a compile error); the
            # unary-minus path special-cases it, and a bare literal
            # that large is rejected by the checker first.
            if value > 9223372036854775807:
                self._fail(
                    "integer literal out of int64 range",
                    span=node.span)
            return f"{value}L", _PREC["primary"]
        if isinstance(value, float):
            return _cs_float(value), _PREC["primary"]
        if isinstance(value, str):
            return _cs_str(value), _PREC["primary"]
        self._fail(f"internal error: cannot emit literal {value!r}")

    def _emit_name(self, node: A.Name,
                   fn_target: Type | None = None) -> tuple[str, int]:
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
                    f"`{node.name}` is not a value — use a variant "
                    "or construct it", span=node.span)
            # A user fn as a value is the function itself (first-
            # class functions, SPEC §9.5); a local/param/handler
            # binding is its C# name. A module-level fn used as a
            # value is a method reference (`Main::fn`).
            if binding.kind == "fn":
                return f"{self.output_stem}.{binding.java}", \
                    _PREC["primary"]
            return binding.value, _PREC["primary"]
        if node.name in _BUILTIN_NAMES:
            return self._emit_builtin_value(node.name, fn_target)
        if node.name in ("math", "time", "random", "file", "env"):
            self._fail(
                f"module `{node.name}` has no value member `{node.name}`",
                span=node.span)
        if node.name == "discord":
            self._fail("module `discord` has no value member `discord`",
                       span=node.span)
        self._fail(f"cannot emit undefined name `{node.name}`",
                   span=node.span)

    def _emit_builtin_value(self, name: str,
                            fn_target: Type | None = None) -> tuple[str, int]:
        # First-class builtin values: `len`/`min`/`max` as a value
        # are small lambdas. The cast type comes from the context
        # (fn_target) when known; the fallback forms are the
        # generic ones (Object in, Long out). A bare builtin's type
        # carries `__unknown__` params (the checker never resolves
        # them without a call), and that is not a real type: fall
        # back to the generic form instead of emitting `__unknown__`
        # into the output (type_model.newr, cs 74/75, 7 Nov).
        known = _is_fn(fn_target) and not any(
            p.name == "__unknown__" for p in fn_target.params)
        cast = self._cs_fn_type(fn_target) if known else None
        if name == "len":
            self._need_rt("lenOf")
            form = cast or "Func<object, long>"
            return (f"(({form}) ((x) => NewrRt.LenOf(x)))",
                    _PREC["primary"])
        if name == "min":
            form = cast or "Func<long, long, long>"
            return (f"(({form}) ((a, b) => a < b ? a : b))",
                    _PREC["primary"])
        if name == "max":
            form = cast or "Func<long, long, long>"
            return (f"(({form}) ((a, b) => a < b ? b : a))",
                    _PREC["primary"])
        if name == "abs":
            # int -> absLong, float -> Math.Abs; the context type
            # decides (the fallback is the int form).
            if fn_target is not None and _is_fn(fn_target) and \
                    _is_float(fn_target.params[0]):
                form = cast or "Func<double, double>"
                return (f"(({form}) ((x) => Math.Abs(x)))",
                        _PREC["primary"])
            self._need_rt("absLong")
            form = cast or "Func<long, long>"
            return (f"(({form}) ((x) => NewrRt.AbsLong(x)))",
                    _PREC["primary"])
        if name == "round":
            self._need_rt("roundHalfAway")
            return "NewrRt.RoundHalfAway", _PREC["primary"]
        if name == "range":
            self._need_rt("range")
            return "NewrRt.Range", _PREC["primary"]
        if name == "set":
            form = cast or \
                "Func<List<object>, HashSet<object>>"
            return (f"(({form}) ((xs) => new HashSet<object>(xs)))",
                    _PREC["primary"])
        if name == "ok":
            self._need_rt("ok")
            return "NewrRt.Ok", _PREC["primary"]
        if name == "err":
            self._need_rt("err")
            return "NewrRt.Err", _PREC["primary"]
        self._fail(f"builtin `{name}` is not a value")

    # ------------------------------------------------------------ interpolation

    def _emit_interp(self, node: A.Interp) -> tuple[str, int]:
        """A newr interpolation becomes C# string concatenation
        (`"a" + x + "b"`): literal parts are string literals, each
        expression part renders through `_emit_interp_part`."""
        parts: list[str] = []
        for part in node.parts:
            if part.text is not None:
                parts.append(_cs_str(part.text))
            else:
                parts.append(self._emit_interp_part(part.expr))
        if not parts:
            return '""', _PREC["primary"]
        # A `+` chain starting with a non-string operand would be
        # ARITHMETIC (`{x}{y}` -> `x + y`), not concatenation — so
        # an interpolation whose first part is an expression starts
        # with `"" + `.
        if node.parts[0].text is None:
            parts.insert(0, '""')
        return " + ".join(parts), _PREC["add"]

    def _emit_interp_part(self, expr: A.Expr) -> str:
        """One `+ ...` field. Bools and floats render through
        `toStr` (Python-repr semantics, J4); ints and strs are bare
        (a long concatenates as its digits; a str is itself);
        handler bindings use their interp form (the mention / name
        form); everything else goes through `toStr` (P8). A bare
        non-string operand of `+` must be wrapped in `toStr` or the
        `+` would be arithmetic — ints are the only exception
        (they stringify in a `+` chain)."""
        if isinstance(expr, A.Name):
            binding = self._lookup(expr.name)
            if binding is not None and binding.interp is not None:
                form = binding.interp_form()
                if "_channelText" in form or "_ChannelText" in form:
                    # The channel's mention form needs the helper
                    # (the bot form is _ChannelText, capital C).
                    self._used_channel_text = True
                return form
        t = self._type_of(expr)
        if _is_bool(t):
            self._need_rt("toStr")
            return f"NewrRt.ToStr({self.emit_expr(expr, 0)})"
        if _is_float(t):
            self._need_rt("toStr")
            return f"NewrRt.ToStr({self.emit_expr(expr, 0)})"
        if _is_int(t) or _is_str(t):
            return self.emit_expr(expr, 0)
        if t.nullable:
            self._need_rt("toStr")
            return f"NewrRt.ToStr({self.emit_expr(expr, 0)})"
        if t.name in ("list", "dict", "set", "Result") or \
                t.name in self.structs or t.name in self.enums:
            self._need_rt("toStr")
            return f"NewrRt.ToStr({self.emit_expr(expr, 0)})"
        return self.emit_expr(expr, 0)

    # ------------------------------------------------------------ operators

    def _emit_binop(self, node: A.BinOp) -> tuple[str, int]:
        op = node.op
        if op == "??":
            left = self.emit_expr(node.left, 0)
            right = self.emit_expr(node.right, 0)
            return self._coalesce_call(left, right,
                                       self._type_of(node)), _PREC["primary"]
        if op in ("and", "or"):
            lt = self._type_of(node.left)
            if op == "or" and lt.nullable:
                # `T? or T` (SPEC §7.2): the default when the value
                # is absent. C# `||` would also take the default
                # for a present-but-falsy value (0L, ""), so it is
                # not the same — use coalesce.
                left = self.emit_expr(node.left, 0)
                right = self.emit_expr(node.right, 0)
                return (self._coalesce_call(left, right,
                                            self._type_of(node)),
                        _PREC["primary"])
            prec = _BINOP_PREC["&&" if op == "and" else "||"]
            left = self.emit_expr(node.left, prec)
            right = self.emit_expr(node.right, prec + 1)
            return f"{left} {'&&' if op == 'and' else '||'} {right}", prec
        if op == "in":
            rt = self._type_of(node.right)
            left = self.emit_expr(node.left, _BINOP_PREC["<"])
            if rt.name == "str":
                right = self.emit_expr(node.right, _BINOP_PREC["<"] + 1)
                return f"{right}.Contains({left})", _PREC["primary"]
            if rt.name == "list":
                self._need_rt("listContains")
                right = self.emit_expr(node.right, _BINOP_PREC["<"] + 1)
                return (f"NewrRt.ListContains({right}, {left})",
                        _PREC["primary"])
            if rt.name == "set":
                right = self.emit_expr(node.right, _BINOP_PREC["<"] + 1)
                return f"{right}.Contains({left})", _PREC["primary"]
            if rt.name == "dict":
                right = self.emit_expr(node.right, _BINOP_PREC["<"] + 1)
                return f"{right}.ContainsKey({left})", _PREC["primary"]
            self._fail(
                f"cannot emit `in` for a {rt.name} (the checker should "
                "have rejected this)")
        if op in ("==", "!=", "<", "<=", ">", ">="):
            prec = _BINOP_PREC["==" if op == "==" else
                               "!=" if op == "!=" else op]
            lt = self._type_of(node.left)
            rt = self._type_of(node.right)
            left = self.emit_expr(node.left, prec)
            right = self.emit_expr(node.right, prec + 1)
            if op in ("==", "!="):
                if _needs_eq(lt) or _needs_eq(rt) or \
                        lt.name in self.structs or lt.name in self.enums or \
                        rt.name in self.structs or rt.name in self.enums:
                    # Structural equality (K6): `eq` is deep for
                    # lists/Maps/Sets/Results/structs, and treats
                    # `null` as equal only to `null` — exactly
                    # newr's `==` for nullable collection values too.
                    self._need_rt("eq")
                    expr = f"NewrRt.Eq({left}, {right})"
                    if op == "!=":
                        expr = f"!{expr}"
                    return expr, _PREC["primary"]
                # `==` on a boxed value is IDENTITY in C# (a
                # nullable long `1000L == 1000L` is false) and on a
                # String it is identity too — use null-safe
                # Objects.equals unless BOTH sides are non-nullable
                # primitives, where bare `==` is correct.
                if lt.nullable or rt.nullable or \
                        _is_str(lt) or _is_str(rt) or \
                        _is_error(lt) or _is_error(rt) or _is_fn(lt) or \
                        _is_fn(rt):
                    expr = f"object.Equals({left}, {right})"
                    if op == "!=":
                        expr = f"!{expr}"
                    return expr, _PREC["primary"]
                jsop = "==" if op == "==" else "!="
                return f"{left} {jsop} {right}", prec
            # Ordering: strings compare lexicographically by code
            # point (NewrRt.StrCmp; string.CompareOrdinal compares
            # UTF-16 code units, so an astral character sorts before
            # U+FFFD, and the culture-sensitive CompareTo is wrong
            # too); numbers compare bare (autounboxing a boxed
            # operand would NPE on null, but the checker only allows
            # ordering on non-nullable values).
            if _is_str(lt) or _is_str(rt):
                self._need_rt("strCmp")
                cmp = {"<": "< 0", "<=": "<= 0", ">": "> 0",
                       ">=": ">= 0"}[op]
                return (f"(NewrRt.StrCmp({left}, {right}) "
                        f"{cmp})", prec)
            jsop = {"<": "<", "<=": "<=", ">": ">", ">=": ">="}[op]
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
            helper = {"+": "Add", "-": "Sub", "*": "Mul", "/": "Tdiv",
                      "%": "Tmod", "**": "PowInt"}[op]
            self._need_rt(helper)
            return f"NewrRt.{helper}({left}, {right})", _PREC["primary"]
        if op == "%" and _is_float(result):
            self._need_rt("fmod")
            return f"NewrRt.Fmod({left}, {right})", _PREC["primary"]
        if op == "/" and _is_float(result):
            # P6: float /0 is an error (C# `/` would give Infinity).
            self._need_rt("fdiv")
            return f"NewrRt.Fdiv({left}, {right})", _PREC["primary"]
        if op == "**" and _is_float(result):
            self._need_rt("powFloat")
            return f"NewrRt.PowFloat({left}, {right})", _PREC["primary"]
        return f"{left} {op} {right}", prec

    def _emit_unary(self, node: A.UnaryOp) -> tuple[str, int]:
        if node.op == "not":
            # C# `!` binds tighter than newr's `not` (which is
            # looser than `==`): `!a == b` is `(!a) == b` in C#.
            # Parenthesise any operand that is not a primary, so
            # `not a == b` reads `!(a == b)` as newr reads it.
            text, prec = self._emit_expr(node.operand)
            if prec < _PREC["primary"]:
                return f"!({text})", _PREC["primary"]
            return f"!{text}", _PREC["unary"]
        # `-`: the operand binds at `**` level (the newr trap:
        # `-a + b` is `-(a + b)`).
        # D19: the int64 minimum is written `-9223372036854775808`
        # in newr. Its positive magnitude is out of int64 range, so
        # the checker leaves the literal's own type unset — the
        # plain `neg(...)` form would also be wrong (it would check
        # the out-of-range literal first). Emit the minimum directly.
        if isinstance(node.operand, A.Lit) and \
                node.operand.value == 9223372036854775808:
            return "long.MinValue", _PREC["unary"]
        t = self._type_of(node.operand)
        if _is_int(t):
            self._need_rt("neg")
            return f"NewrRt.Neg({self.emit_expr(node.operand, 0)})", \
                _PREC["primary"]
        operand = self.emit_expr(node.operand, _PREC["pow"])
        # C# traps: `--x` is the decrement operator —
        # parenthesise a `**` operand or a unary operand
        # (`-(powInt(a, 2))`, `-(-x)`).
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
                if binding is None and (obj.name in MODULES
                                       or obj.name in ("db", "http",
                                                       "json", "ui")):
                    # A named module used without an import
                    # (N-09/N-10): the name is both the real module
                    # and the reference.
                    fake = _Binding(java=obj.name, kind="module",
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
            # `m_helper.Shape.Circle(radius: 1.5)` — the declaration
            # comes from the imported module, for field reordering.
            imported = self._imported_enum(func)
            if imported is not None:
                ref, enum_name, variant, decl = imported
                return self._emit_enum_call(enum_name, variant, node,
                                            decl=decl, ref=ref)
            return self._emit_method_call(node, func)
        # A call on an arbitrary expression (a lambda, a call result):
        # C# delegates are callable with ordinary call syntax, so
        # the callee is invoked directly — no `.get()`/`.apply()`
        # (that was Java's FnN interface).
        ftype = self._type_of(func)
        f = self.emit_expr(func, _PREC["primary"],
                           fn_target=ftype if _is_fn(ftype) else None)
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
                    "input() in a bot program blocks on stdin — bots "
                    "have no stdin (D21)")
            self._need_rt("input")
            args = [self.emit_expr(a, 0) for a in node.args]
            return f"NewrRt.Input({', '.join(args)})", _PREC["primary"]
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
            return f"NewrRt.RoundHalfAway({', '.join(args)})", \
                _PREC["primary"]
        if name == "range":
            self._need_rt("range")
            args = [self.emit_expr(a, 0) for a in node.args]
            return f"NewrRt.Range({', '.join(args)})", _PREC["primary"]
        if name == "set":
            t = self._type_of(node)
            elem = t.params[0] if t.params else Type("object")
            arg = self.emit_expr(node.args[0], 0)
            return (f"new HashSet<{self._cs_boxed_type(elem)}>({arg})",
                    _PREC["primary"])
        if name == "ok":
            self._need_rt("ok")
            t = self._type_of(node)
            # The runtime's ok is `ok<T>(T value)` — ONE type
            # parameter (the error type is fixed to NewrError);
            # emitting two type arguments does not compile
            # (conformance opt_methods).
            return (f"NewrRt.Ok<{self._cs_boxed_type(t.params[0])}>("
                    f"{self.emit_expr(node.args[0], 0)})",
                    _PREC["primary"])
        if name == "err":
            self._need_rt("err")
            t = self._type_of(node)
            return (f"NewrRt.Err<{self._cs_boxed_type(t.params[0])}>("
                    f"{self.emit_expr(node.args[0], 0)})",
                    _PREC["primary"])
        binding = self._lookup(name)
        if binding is not None and binding.kind == "struct":
            # Struct construction: C# has no kwargs, so keyword
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
            return (f"new {binding.java}({', '.join(parts)})",
                    _PREC["primary"])
        if binding is not None:
            # A fn VALUE (a local/param/let holding a lambda) and a
            # declared fn (kind "fn", a static method) are both
            # invoked with ordinary call syntax in C# — delegates
            # are callable, so no `.get()`/`.apply()` branch is
            # needed (unlike C#'s FnN interface).
            args = [self.emit_expr(a, 0) for a in node.args]
            kwargs = self._emit_kwargs(node, binding)
            return (f"{binding.java}({', '.join(args + kwargs)})",
                    _PREC["primary"])
        self._fail(f"unknown function `{name}`")

    def _emit_kwargs(self, node: A.Call, binding: _Binding) -> list[str]:
        """Keyword arguments for a user-fn call: C# has no kwargs,
        so they are reordered into position (the checker already
        validated names and types)."""
        if not node.kwargs:
            return []
        fn = self.fns_by_cs.get(binding.java)
        if fn is None:
            self._fail(
                "internal error: cannot find fn "
                f"`{binding.java}` for keyword arguments")
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
        """`print` renders every argument with `toStr` (P8) — the
        runtime `print` helper does that mapping internally, so the
        emitted call is bare. A `?` argument propagates first (the
        temp + early return are hoisted before the statement, D7).
        When every argument is already a C# string (or there are
        no arguments), `Console.WriteLine` prints them exactly as
        the runtime helper would (space-separated, no formatting),
        so no runtime import is needed for a plain `print "..."`.
        Multiple string arguments are joined with `" "`."""
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
            if not parts:
                return "Console.WriteLine()", _PREC["primary"]
            return (f"Console.WriteLine({' + \" \" + '.join(parts)})",
                    _PREC["primary"])
        self._need_rt("print")
        return f"NewrRt.Print({', '.join(parts)})", _PREC["primary"]

    def _emit_builtin_call(self, node: A.Call,
                           binding: _Binding | None) -> tuple[str, int]:
        name = node.func.name if isinstance(node.func, A.Name) else None
        args = node.args
        if name == "len":
            t = self._type_of(args[0])
            arg = self.emit_expr(args[0], 0)
            # `len` returns an int (long). A str's length is its
            # code-point count (P5), not its UTF-16 length.
            if t.name == "str":
                self._need_rt("chars")
                return f"(long) NewrRt.Chars({arg}).Count", \
                    _PREC["primary"]
            if t.name in ("list", "dict", "set"):
                return f"(long) {arg}.Count", _PREC["primary"]
            return f"(long) {arg}.Count", _PREC["primary"]
        if name == "min":
            a = self.emit_expr(args[0], 0)
            b = self.emit_expr(args[1], 0)
            t = self._type_of(args[0])
            if _is_str(t):
                self._need_rt("strCmp")
                return (f"(NewrRt.StrCmp({a}, {b}) < 0 "
                        f"? {a} : {b})",
                        _PREC["primary"])
            return f"({a} < {b} ? {a} : {b})", _PREC["primary"]
        if name == "max":
            a = self.emit_expr(args[0], 0)
            b = self.emit_expr(args[1], 0)
            t = self._type_of(args[0])
            if _is_str(t):
                self._need_rt("strCmp")
                return (f"(NewrRt.StrCmp({a}, {b}) < 0 "
                        f"? {b} : {a})",
                        _PREC["primary"])
            return f"({a} < {b} ? {b} : {a})", _PREC["primary"]
        if name == "abs":
            t = self._type_of(args[0])
            arg = self.emit_expr(args[0], 0)
            if _is_int(t):
                self._need_rt("absLong")
                return f"NewrRt.AbsLong({arg})", _PREC["primary"]
            return f"Math.Abs({arg})", _PREC["primary"]
        self._fail(f"internal error: cannot emit builtin `{name}`")

    def _emit_conversion(self, node: A.Call) -> tuple[str, int]:
        name = node.func.name  # str | int | float | bool
        arg = node.args[0]
        t = self._type_of(arg)
        text = self.emit_expr(arg, 0)
        if name == "str":
            # A nullable str's runtime value is null when absent —
            # bare rendering would print "null", not newr's "none"
            # (P8), so nullable values go through toStr.
            if _is_str(t) and not t.nullable:
                return text, _PREC["primary"]
            self._need_rt("toStr")
            return f"NewrRt.ToStr({text})", _PREC["primary"]
        if name == "int":
            if _is_int(t):
                return text, _PREC["primary"]
            # float (truncation), str (parsing), bool (0/1) — all
            # through toInt, which returns a long in every case.
            self._need_rt("toInt")
            return f"NewrRt.ToInt({text})", _PREC["primary"]
        if name == "float":
            if _is_float(t):
                return text, _PREC["primary"]
            if _is_str(t):
                self._need_rt("toFloat")
                return f"NewrRt.ToFloat({text})", _PREC["primary"]
            if _is_int(t):
                return f"(double) {text}", _PREC["primary"]
            return f"({text} ? 1.0 : 0.0)", _PREC["primary"]  # bool
        # bool
        if _is_bool(t):
            return text, _PREC["primary"]
        if _is_str(t):
            self._need_rt("toBool")
            return f"NewrRt.ToBool({text})", _PREC["primary"]
        if _is_int(t):
            return f"({text} != 0L)", _PREC["primary"]
        return f"({text} != 0.0)", _PREC["primary"]  # float

    def _emit_enum_call(self, enum_name: str, variant: str,
                        node: A.Call, decl=None,
                        ref: str | None = None) -> tuple[str, int]:
        """`Shape.Circle(radius: 1.5)` -> the variant factory
        method; a bare variant is the static final instance
        (`Color.Red`). C# has no kwargs, so keyword arguments are
        reordered into declared field order (the checker already
        validated them). `decl` / `ref` serve the §20 qualified
        imported form (`m_helper.Shape.Circle(...)`): the
        declaration comes from the imported module and the class
        lives under the module reference; the local form takes both
        from this file."""
        prefix = (f"{ref}." if ref else "") + self._member_name(
            enum_name, ref if ref else self.output_stem)
        decl = decl if decl is not None else self.enums.get(enum_name)
        if decl is not None:
            fields = []
            for var in decl.variants:
                if var.name == variant:
                    fields = [f.name for f in var.fields]
                    break
            if not fields:
                # A bare variant (no fields): the static instance.
                return f"{prefix}.{self._mangle(variant)}", \
                    _PREC["primary"]
            values: dict[str, str] = {}
            for a, f in zip(node.args, fields):
                values[f] = self.emit_expr(a, 0)
            for kname, value, _span in node.kwargs:
                values[kname] = self.emit_expr(value, 0)
            parts = [values[f] for f in fields if f in values]
            return (f"{prefix}.{self._mangle(variant)}"
                    f"({', '.join(parts)})", _PREC["primary"])
        args = [self.emit_expr(a, 0) for a in node.args]
        return (f"{prefix}.{self._mangle(variant)}"
                f"({', '.join(args)})", _PREC["primary"])

    def _emit_log_call(self, level: str, node: A.Call) -> tuple[str, int]:
        """`log.info/warn/error(msg)` (P10, J12's C# twin):
        info -> stdout, warn/error -> stderr, no timestamp."""
        msg = self.emit_expr(node.args[0], 0)
        if level == "info":
            return f"Console.WriteLine({msg})", _PREC["primary"]
        return f"Console.Error.WriteLine({msg})", _PREC["primary"]

    def _file_module_class(self, binding: _Binding) -> str:
        """The C# class name for a file-module import: the file
        STEM, not the import alias (K1 — one public class per file,
        named after the stem). For `import "h_fn.newr" as h` the
        class is `h_fn`, so every reference to the module must use
        the stem; using the alias emits `h.…` against a class named
        `h_fn` and does not compile."""
        if binding.kind == "file-module":
            return self.imported_modules.get(binding.java, binding.java)
        return binding.java

    def _imported_enum(self, node: A.Member):
        """A qualified imported enum member, `module.Enum.Variant`.

        Returns (ref, enum_name, variant, decl) when the chain is
        exactly that shape, the inner name is an enum declared in
        the imported module, else None. The decl check matters: a
        struct-typed top-level let accessed the same way
        (`m_helper.SomeLet.field`) is NOT an enum member and must
        fall through to the ordinary typed-member path. The decl is
        returned too, so a fielded-variant call can reorder its
        keyword arguments into declared field order (C# has no
        kwargs)."""
        obj = node.obj
        if not isinstance(obj, A.Member) or not isinstance(obj.obj, A.Name):
            return None
        binding = self._lookup(obj.obj.name)
        if binding is None or binding.kind != "file-module":
            return None
        sub = self.program.modules.get(binding.java)
        if sub is None:
            return None
        decl = next((e for e in sub.enums if e.name == obj.name), None)
        if decl is None:
            return None
        return (self._file_module_class(binding), obj.name, node.name,
                decl)

    def _enum_field_access(self, expr_text: str, enum_name: str,
                           field_name: str, field_type: Type | None) -> str:
        """`expr.field` for an enum field. When variants disagree
        on the field's type, the union field is Object and the
        access needs a cast to the field's declared (boxed) type —
        otherwise a `long` field read as Object would not compile
        where a long is expected."""
        decl = self._find_enum_decl(enum_name)
        if decl is not None and field_type is not None:
            union = self._enum_union_fields(decl)
            utype = union.get(field_name)
            if utype is not None and utype.name == "object":
                return (f"({self._cs_boxed_type(field_type)}) "
                        f"{expr_text}.{self._mangle(field_name)}")
        return f"{expr_text}.{self._mangle(field_name)}"

    def _emit_member(self, node: A.Member,
                     fn_target: Type | None = None) -> tuple[str, int]:
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
                        f"struct `{obj.name}` is not a value — construct it",
                        span=node.span)
                if binding.kind == "store":
                    self._fail(
                        f"store `{obj.name}` is not a value — use its "
                        "economy functions", span=node.span)
                if binding.kind == "fn":
                    self._fail(
                        f"function `{obj.name}` is not a value — call it",
                        span=node.span)
            if obj.name in MODULES:
                # A named module used without an import: the name is
                # both the real module and the reference.
                fake = _Binding(java=obj.name, kind="module",
                                module=obj.name)
                return self._emit_module_member(node, fake)
        # §20: qualified imported enum member, `m_helper.Shade.Red`.
        # The inner `m_helper.Shade` has no type of its own (the
        # checker types only the full chain), so resolve the whole
        # chain here from the imported module's enum declaration.
        imported = self._imported_enum(node)
        if imported is not None:
            ref, enum_name, variant, _decl = imported
            return (f"{ref}.{self._member_name(enum_name, ref)}."
                    f"{self._mangle(variant)}", _PREC["primary"])
        obj_type = self._type_of(obj)
        t = self._type_of(node)
        # The newr `error` type has exactly two fields (checker
        # ERROR_FIELDS): message (str) and code (str?). C#'s
        # NewrError keeps `code` as a public field, but `message`
        # lives in Exception and is only reachable via getMessage()
        # (the field is private) — a bare `.message` does not
        # compile (conformance try_catch).
        if obj_type.name == "error":
            obj_text = self.emit_expr(obj, _PREC["primary"])
            if node.name == "message":
                return f"{obj_text}.Message", _PREC["primary"]
            return f"{obj_text}.Code", _PREC["primary"]
        # §16 (hole 2): a method used as a value is a lambda, not a
        # C# attribute — a Long has no `is_some`, an ArrayList has
        # no `len`. A real struct field stays a plain attribute.
        if t.name == "fn" and not self._is_plain_field(obj_type,
                                                       node.name):
            sig = self._method_value_sig(obj_type, node.name)
            if sig is not None:
                return self._emit_method_value(node, obj_type, sig,
                                               fn_target)
        obj_text = self.emit_expr(obj, _PREC["primary"])
        # A Discord entity field (SPEC §7.1, discord_limits
        # ENTITY_FIELDS): newr's lowercase field names map onto
        # Discord.Net's PascalCase properties, and an `id` is a
        # newr `int` (a long) while Discord.Net's `Id` is a ulong
        # (so it is cast). Only when the receiver is really an
        # entity — a user struct/enum with the same name keeps its
        # plain field access (a struct named `user` is legal newr).
        if obj_type.name in ("user", "member", "guild", "channel",
                             "role", "interaction", "message") and \
                obj_type.name not in self.structs and \
                obj_type.name not in self.enums:
            mapped = self._entity_field(obj_type.name, node.name,
                                        obj_text)
            if mapped is not None:
                return mapped, _PREC["primary"]
            self._fail(f"`{obj_type.name}` has no field "
                       f"`{node.name}`")
        # An enum field whose variants disagree on the type reads
        # through the Object union field — cast to the declared type.
        if self._find_enum_decl(obj_type.name) is not None:
            return (self._enum_field_access(obj_text, obj_type.name,
                                           node.name, t),
                    _PREC["primary"])
        return f"{obj_text}.{self._mangle(node.name)}", _PREC["primary"]

    def _entity_field(self, type_name: str, field: str,
                      obj_text: str) -> str | None:
        """One Discord entity field -> its Discord.Net spelling
        (verified live against Discord.Net 3.20.2). `None` means the
        field does not exist on the entity (the caller fails
        cleanly). `id` is a newr `int`, so a ulong `Id` is cast to
        long; `name` is `Username` on users/members and `Name`
        elsewhere; a channel's mention has no Discord.Net property,
        so it is built as `<#id>`; `member_count` lives only on
        SocketGuild, so the guild is cast (every guild value the
        emitter produces IS a SocketGuild at runtime)."""
        if type_name == "user":
            mapping = {
                "id": f"(long) ({obj_text}.Id)",
                "mention": f"{obj_text}.Mention",
                "name": f"{obj_text}.Username",
                "bot": f"{obj_text}.IsBot",
            }
        elif type_name == "member":
            mapping = {
                "id": f"(long) ({obj_text}.Id)",
                "mention": f"{obj_text}.Mention",
                "name": f"{obj_text}.Username",
                "guild": f"{obj_text}.Guild",
            }
        elif type_name == "guild":
            mapping = {
                "id": f"(long) ({obj_text}.Id)",
                "name": f"{obj_text}.Name",
                "member_count": f"((SocketGuild) {obj_text}).MemberCount",
            }
            if field == "member_count":
                self._need_discord(
                    "Discord.WebSocket.SocketGuild")
        elif type_name == "channel":
            mapping = {
                "id": f"(long) ({obj_text}.Id)",
                "name": f"{obj_text}.Name",
                "mention": f"(\"<#\" + {obj_text}.Id.ToString() + \">\")",
            }
        elif type_name == "role":
            mapping = {
                "id": f"(long) ({obj_text}.Id)",
                "name": f"{obj_text}.Name",
            }
        elif type_name == "interaction":
            self._need_discord(
                "Discord.WebSocket.SocketGuildChannel")
            mapping = {
                "user": f"{obj_text}.User",
                "guild": f"({obj_text}.Channel as "
                         "SocketGuildChannel)?.Guild",
                "channel": f"{obj_text}.Channel",
                "locale": f"{obj_text}.UserLocale",
            }
        elif type_name == "message":
            mapping = {
                "content": f"{obj_text}.Content",
                "author": f"{obj_text}.Author",
                "channel": f"{obj_text}.Channel",
            }
        else:
            return None
        return mapping.get(field)

    def _emit_math_member(self, node: A.Member) -> tuple[str, int]:
        if node.name in ("pi", "e"):
            return ("Math.PI" if node.name == "pi" else "Math.E",
                    _PREC["primary"])
        self._fail(f"math has no member `{node.name}`")

    def _emit_enum_variant(self, node: A.Member,
                           binding: _Binding) -> tuple[str, int]:
        prefix = ((binding.module + ".") if binding.module else "") + \
            binding.java
        return f"{prefix}.{self._mangle(node.name)}", _PREC["primary"]

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
        inside an `optChain` lambda (D38) — the receiver is proven
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
        field) — a plain attribute access, never a method value.
        Provider/entity types have no tables here, so they report
        False and the method-signature lookup decides."""
        decl = self.structs.get(recv_type.name)
        if decl is not None:
            return any(f.name == name for f in decl.fields)
        return False

    def _emit_method_value(self, node: A.Member, obj_type: Type,
                           sig, fn_target: Type | None = None) -> tuple[str, int]:
        """A method used as a value (§16, hole 2): a lambda taking
        the method's parameters, capturing the receiver by value
        (D10). The receiver is snapshotted into a hoisted `final`
        local (so the receiver expression is evaluated once, when
        the lambda is created, never again at call time), and the
        lambda body is the ordinary method call on the snapshot.
        The lambda is cast to its fn type unless the context
        already provides one."""
        params, ret = sig
        obj_text = self.emit_expr(node.obj, 0)
        snap_name = self._new_temp_name()
        snap_type = self._cs_type(obj_type)
        self._hoists_add([f"{snap_type} {snap_name} = {obj_text};"])
        self._push_scope()
        arg_bindings = []
        arg_nodes = []
        for i, ptype in enumerate(params):
            b = self._bind_fresh(f"_a{i}", type=ptype, kind="param")
            arg_bindings.append(b)
            arg_nodes.append(A.Name(span=node.span, name=f"_a{i}",
                                    expr_type=ptype))
        recv_binding = _Binding(java=snap_name, type=obj_type,
                                kind="param")
        self._scopes[-1]["_r"] = recv_binding
        recv_name = A.Name(span=node.span, name="_r",
                           expr_type=obj_type)
        member = A.Member(span=node.span, obj=recv_name,
                          name=node.name, optional=False,
                          expr_type=self._type_of(node))
        call = A.Call(span=node.span, func=member, args=arg_nodes,
                      kwargs=[], expr_type=ret)
        body, hoists = self._emit_expr_with_hoists(call)
        self._pop_scope()
        arg_names = ", ".join(b.java for b in arg_bindings)
        arrow = f"({arg_names}) => {body}"
        cast = fn_target if _is_fn(fn_target) else self._type_of(node)
        cast_text = self._cs_fn_type(cast)
        if hoists:
            # A hoisted block-body lambda: the hoisted const's
            # initializer is the lambda, and the hoists run inside
            # the arrow body at call time.
            name = self._new_lambda_name()
            lines = [f"{cast_text} {name} = ({arg_names}) => {{"]
            for h in hoists:
                lines.append("  " + h)
            lines.append(f"  return {body};")
            lines.append("};")
            self._hoists_add(lines)
            return name, _PREC["primary"]
        return f"(({cast_text}) ({arrow}))", _PREC["primary"]

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
                return f"({obj_text} != null)", _PREC["primary"]
            if name == "is_none":
                return f"({obj_text} == null)", _PREC["primary"]
            if name == "unwrap":
                # `T?.unwrap()` — the runtime has two names by
                # value kind (the class/struct overload pair for
                # one name resolves unpredictably): UnwrapOpt for
                # reference types, UnwrapOptVal for value types.
                # The explicit type argument is load-bearing: a
                # plain `long` argument does not infer T for a
                # `Nullable<T>` parameter.
                self._need_rt("unwrapOpt")
                stripped = Type(obj_type.name, params=obj_type.params,
                                nullable=False)
                if _is_cs_value_type(stripped):
                    return (f"NewrRt.UnwrapOptVal<"
                            f"{self._cs_boxed_type(stripped)}>"
                            f"({obj_text})", _PREC["primary"])
                return (f"NewrRt.UnwrapOpt<"
                        f"{self._cs_boxed_type(stripped)}>"
                        f"({obj_text})", _PREC["primary"])
            self._fail(f"`{obj_type}` has no method `{name}`")
        tname = obj_type.name
        if tname == "str":
            if name == "upper":
                return f"{obj_text}.ToUpper()", _PREC["primary"]
            if name == "lower":
                return f"{obj_text}.ToLower()", _PREC["primary"]
            if name == "strip":
                # C#'s strip() (not trim()) removes Unicode
                # whitespace, matching Python's str.Trim().
                return f"{obj_text}.Trim()", _PREC["primary"]
            if name == "split":
                self._need_rt("splitStr")
                return f"NewrRt.SplitStr({obj_text}, {args[0]})", \
                    _PREC["primary"]
            if name == "join":
                # newr `"sep".join(parts)` — C# joins the LIST
                # with the separator.
                return f"string.Join({obj_text}, {args[0]})", \
                    _PREC["primary"]
            if name == "contains":
                return f"{obj_text}.Contains({args[0]})", _PREC["primary"]
            if name == "starts_with":
                return f"{obj_text}.StartsWith({args[0]})", \
                    _PREC["primary"]
            if name == "ends_with":
                return f"{obj_text}.EndsWith({args[0]})", _PREC["primary"]
            if name == "chars":
                # SPEC pin P5: code points, not UTF-16 code units.
                self._need_rt("chars")
                return f"NewrRt.Chars({obj_text})", _PREC["primary"]
        elif tname == "list":
            if name == "append":
                return f"{obj_text}.Add({args[0]})", _PREC["primary"]
            if name == "contains":
                self._need_rt("listContains")
                return f"NewrRt.ListContains({obj_text}, {args[0]})", \
                    _PREC["primary"]
            if name == "get":
                self._need_rt("listGet")
                # listGet returns Object (the runtime is untyped
                # for lists) — cast to the element type, which is
                # the call's own type (modules.py: list.get
                # returns T). The checker guarantees two
                # arguments (index, default); a one-argument
                # `list.get` is rejected there, so there is no
                # two-argument runtime overload to call.
                t = self._type_of(node)
                return (f"(({self._cs_boxed_type(t)}) "
                        f"NewrRt.ListGet({obj_text}, {args[0]}, "
                        f"{args[1]}))", _PREC["primary"])
            if name == "len":
                return f"(long) {obj_text}.Count", _PREC["primary"]
        elif tname == "dict":
            if name == "keys":
                return f"new List<string>({obj_text}.Keys)", \
                    _PREC["primary"]
            if name == "get":
                if len(args) == 2:
                    return (f"({obj_text}.ContainsKey({args[0]}) ? "
                            f"{obj_text}[{args[0]}] : {args[1]})",
                            _PREC["primary"])
                return f"{obj_text}[{args[0]}]", _PREC["primary"]
            if name == "contains":
                return f"{obj_text}.ContainsKey({args[0]})", \
                    _PREC["primary"]
        elif tname == "set":
            if name == "add":
                return f"{obj_text}.Add({args[0]})", _PREC["primary"]
            if name == "remove":
                self._need_rt("setRemove")
                return f"NewrRt.SetRemove({obj_text}, {args[0]})", \
                    _PREC["primary"]
            if name == "contains":
                return f"{obj_text}.Contains({args[0]})", _PREC["primary"]
        elif tname == "Result":
            if name in ("is_ok", "is_err"):
                # The C# Result exposes isOk/isErr as METHODS
                # (newr_rt.cs) — a bare access would be the
                # method itself, not the bool.
                method = "IsOk" if name == "is_ok" else "IsErr"
                return f"{obj_text}.{method}()", _PREC["primary"]
            if name == "unwrap":
                return f"{obj_text}.Unwrap()", _PREC["primary"]
            if name == "unwrap_or":
                return f"{obj_text}.UnwrapOr({args[0]})", _PREC["primary"]
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
            return f"{obj_text}[{idx}]", _PREC["primary"]
        if obj_type.name == "list":
            self._need_rt("listIndex")
            # Indexing is the checked form: a negative or out-of
            # range index raises IndexError (rt listIndex), exactly
            # like newr's `xs[i]`. (`list.get` is the defaulting
            # form — see _emit_method_call.) listIndex returns
            # Object, so cast to the element's boxed type — a bare
            # result cannot be assigned to a `long` or compared
            # with `!=` (conformance list_index_error, list_ops,
            # nullable_propagate).
            elem = self._cs_boxed_type(obj_type.params[0])
            return (f"(({elem}) NewrRt.ListIndex({obj_text}, {idx}))",
                    _PREC["primary"])
        if obj_type.name == "str":
            # newr indexes a string by code point (P5); a C#
            # string index is a UTF-16 code unit.
            self._need_rt("strIndex")
            return f"NewrRt.StrIndex({obj_text}, {idx})", _PREC["primary"]
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
        """`?.` lowering (D9, corrected — the C# twin of the JS
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
            # the rebased base carry the STRIPPED (inner) type —
            # otherwise a method call on it would dispatch as if the
            # value were still nullable.
            inner_type = Type(recv_type.name, params=recv_type.params,
                              nullable=False)
            # §16 (hole 1): a segment must also end before any link
            # whose receiver can be `none` INSIDE the lambda — a
            # nullable field (`o?.inner.name`) or a nullable method
            # result (`r?.Unwrap().upper()` on `Result[str?, _]?`).
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
            binding = self._bind_fresh("_v", type=inner_type,
                                       kind="param")
            # The rebased base is a Name for the NEWR name `_v` —
            # the lookup returns the binding's C# name (which may
            # be `_v_1` in a nested chain).
            new_base = A.Name(span=links[i].span, name="_v",
                              expr_type=inner_type)
            body_node = self._rebase(seg_end, inner_recv, new_base,
                                     in_lambda)
            self._need_rt("optChain")
            body_type = in_lambda.get(id(seg_end)) or self._type_of(node)
            body = self._callable_body(body_node, binding.java,
                                       inner_type, body_type)
            self._pop_scope()
            cur_text = self._opt_chain_call(cur_text, body,
                                            inner_type, body_type)
            i = j
        return cur_text, _PREC["primary"]

    def _opt_chain_call(self, recv_text: str, body: str,
                        recv_type: Type, result_type: Type) -> str:
        """The OptChain call for one `?.` segment, with explicit
        type arguments (type inference cannot be trusted here: a
        plain `long` argument does not infer T for a `Nullable<T>`
        parameter, and the lambda's return type is ambiguous
        without them). The name depends on the RESULT kind:
        OptChain for a reference result, OptChainVal for a
        (non-nullable) value result, OptChainNullable for a
        nullable value result. The receiver kind (class/struct)
        is resolved by the overload pair from the explicit T."""
        recv_s = self._cs_boxed_type(
            Type(recv_type.name, params=recv_type.params,
                 nullable=False))
        res_s = self._cs_boxed_type(
            Type(result_type.name, params=result_type.params,
                 nullable=False))
        if _is_cs_value_type(result_type):
            if result_type.nullable:
                self._need_rt("optChainNullable")
                return (f"NewrRt.OptChainNullable<{recv_s}, {res_s}>"
                        f"({recv_text}, {body})")
            self._need_rt("optChainVal")
            return (f"NewrRt.OptChainVal<{recv_s}, {res_s}>"
                    f"({recv_text}, {body})")
        self._need_rt("optChain")
        return (f"NewrRt.OptChain<{recv_s}, {res_s}>"
                f"({recv_text}, {body})")

    def _coalesce_call(self, left: str, right: str,
                       result_type: Type) -> str:
        """The `??` / `T? or T` call, with an explicit type
        argument. CoalesceVal for a value result, Coalesce for a
        reference result (the class/struct overload pair for one
        name resolves unpredictably, so the two names exist)."""
        res_s = self._cs_boxed_type(
            Type(result_type.name, params=result_type.params,
                 nullable=False))
        if _is_cs_value_type(result_type):
            self._need_rt("coalesceVal")
            return (f"NewrRt.CoalesceVal<{res_s}>"
                    f"({left}, () => {right})")
        self._need_rt("coalesce")
        return (f"NewrRt.Coalesce<{res_s}>({left}, () => {right})")

    def _callable_body(self, expr: A.Expr, param_java: str,
                       param_type: Type, body_type: Type) -> str:
        """Emit `expr` as a lambda body `(_v) -> ...`. If the body
        needs hoists (a nested block-body lambda), the lambda becomes
        a hoisted `final Function<...>` const and its name is
        returned instead, so the hoisted statements run at call time,
        inside the function, not at the enclosing statement."""
        saved = self._hoists
        self._hoists = []
        body = self.emit_expr(expr, 0)
        hoists = self._hoists
        self._hoists = saved
        if not hoists:
            return f"({param_java}) => {body}"
        name = self._new_lambda_name()
        fn_type = ("Func<"
                   + self._cs_boxed_type(param_type)
                   + ", " + self._cs_boxed_type(body_type) + ">")
        lines = [f"{fn_type} {name} = ({param_java}) => {{"]
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
            # D38: the result of an OPTIONAL call link (`s?.Unwrap()`)
            # is nullable only because the `?.` short-circuits —
            # inside the lambda the receiver is proven non-none, so
            # the result is the plain (stripped) type. Without this,
            # a later link (`.upper()` in `s?.Unwrap().upper()`)
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

        Inside an `optChain` lambda the receiver is proven
        non-none, so this is the link's result type with the `?.`
        short-circuit removed — but a nullable FIELD type or a
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
            # member's `name`): the type is unknown — treat as
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

    def _emit_lambda(self, node: A.Lambda,
                     fn_target: Type | None = None) -> tuple[str, int]:
        """Closures capture by value (D10, J8): free variables are
        snapshotted into hoisted `final` locals before the enclosing
        statement — a C# lambda may only capture effectively-final
        locals, so a snapshot local (never reassigned) is exactly
        the captured value. A block body cannot be an inline lambda
        body with statements, so it is hoisted to a `final`
        const immediately before the enclosing statement. An inline
        lambda needs no cast when the context provides its fn type
        (fn_target); otherwise it is cast to its own fn type."""
        free = self._lambda_free_vars(node)
        self._push_scope()
        saved_used = self._fn_used
        self._fn_used = set()
        param_bindings = [self._bind_fresh(p.name, type=p.type,
                                           kind="param")
                          for p in node.params]
        # Snapshot locals: one per free variable, hoisted before the
        # enclosing statement (so the lambda captures the value at
        # creation, not a later reassignment).
        used_names = {b.java for b in param_bindings}
        for name, b in free:
            snap = self._new_temp_name()
            snap_type = self._cs_type(b.type)
            self._hoists_add([f"{snap_type} {snap} = {b.value};"])
            self._scopes[-1][name] = _Binding(java=snap, type=b.type,
                                              kind="param")
        params_text = ", ".join(b.java for b in param_bindings)
        fn_type = self._type_of(node)
        cast = fn_target if _is_fn(fn_target) else fn_type
        cast_text = self._cs_fn_type(cast)
        # A `-> none` lambda is `Func<object>` in C# (there is no
        # void-returning Func), and a void body does not convert
        # to it — `() => Console.WriteLine(...)` is CS0029/CS1662
        # (proven by probe: a plain `let h = fn() -> none:
        # print("tapped")` failed to build). A none-returning
        # lambda is therefore emitted as a block that runs the body
        # as a statement and returns null. This is what makes ui
        # handlers (`fn() -> none: print(...)`) compile at all.
        ret_none = node.return_type is None or \
            _is_none(node.return_type)
        if node.expr_body is not None:
            body, hoists = self._emit_expr_with_hoists(node.expr_body)
            self._fn_used = saved_used
            self._pop_scope()
            if not hoists:
                if ret_none:
                    arrow = (f"({params_text}) => {{ {body}; "
                             f"return null; }}")
                else:
                    arrow = f"({params_text}) => {body}"
                if fn_target is not None and _is_fn(fn_target):
                    return arrow, _PREC["primary"]
                return f"(({cast_text}) ({arrow}))", _PREC["primary"]
            # The body needed hoists (a nested block-body lambda):
            # the hoisted const's initializer IS the lambda, so the
            # snapshot locals are in scope inside the arrow body at
            # call time.
            name = self._new_lambda_name()
            lines = [f"{cast_text} {name} = ({params_text}) => {{"]
            for h in hoists:
                lines.append("  " + h)
            if ret_none:
                lines.append(f"  {body};")
                lines.append("  return null;")
            else:
                lines.append(f"  return {body};")
            lines.append("};")
            self._hoists_add(lines)
            return name, _PREC["primary"]
        # Block body: hoist a const arrow.
        name = self._new_lambda_name()
        self._fn_ctx.append({"return_type": node.return_type,
                             "ensures": [], "in_handler": False,
                             "in_lambda": True})
        body_lines = self._emit_block(node.body or [], 1)
        self._fn_ctx.pop()
        self._fn_used = saved_used
        self._pop_scope()
        lines = [f"{cast_text} {name} = ({params_text}) => {{"]
        lines.extend(body_lines if body_lines else ["  ;"])
        if ret_none:
            lines.append("  return null;")
        lines.append("};")
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
                    new_scope: bool = True,
                    top_level: bool = False) -> list[str]:
        """Emit a list of statements at `indent`. Returns lines with
        the indent prefix applied. Each statement gets its own hoist
        list (temps and hoisted defs go before it, at its indent).

        A component attaches only to an answer in the SAME block
        (D40): the last answer is tracked per block (saved/restored
        around nested blocks), and a Button/Select statement rewrites
        that answer's line to carry the components.

        `top_level` marks the direct top-level statement list of a
        file (main() / the module static block) — see
        `_in_top_level`; nested blocks always pass False."""
        if new_scope:
            self._push_scope()
        saved_answer = self._last_answer
        self._last_answer = None
        saved_top = self._in_top_level
        self._in_top_level = top_level
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
                    # references the components variable, so defining
                    # it after the answer would be a compile error.
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
            self._in_top_level = saved_top
        return lines

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
            binding = self._lookup(stmt.name)
            is_top = (self._in_top_level and not self._fn_ctx
                      and binding is not None
                      and binding.kind == "global-let")
            if isinstance(stmt.value, A.Propagate):
                value = self._propagate_unwrapped(stmt.value)
            else:
                tgt = binding.type if binding is not None and \
                    _is_fn(binding.type) else None
                value = self.emit_expr(stmt.value, 0, fn_target=tgt)
            if is_top:
                # A top-level `let`/`const` is a static field of the
                # class (so top-level functions can read it), and
                # its initializer runs here, in file order, inside
                # main() (or the module's static block) — a field
                # initializer at declaration time would run before
                # main() and break file order for side-effecting
                # initializers (K2). Emitting a local declaration
                # here would shadow the field and top-level
                # functions would read the still-zero field.
                return [f"{p}{binding.java} = {value};"]
            t = stmt.type_ann
            if t is None:
                t = self._type_of(stmt.value)
            binding = self._bind(stmt.name, type=t, kind="local")
            keyword = ""  # C# has no `final` locals; a newr `const` is
            # immutable by the checker, not by the C# compiler
            # (`const` would demand a compile-time constant)
            return [f"{p}{keyword}{self._cs_type(t)} "
                    f"{binding.java} = {value};"]
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
            tgt = binding.type if _is_fn(binding.type) else None
            value = self.emit_expr(stmt.value, 0, fn_target=tgt)
        if stmt.op == "=":
            return [f"{p}{binding.java} = {value};"]
        t = binding.type
        if _is_int(t):
            helper = {"+=": "Add", "-=": "Sub", "*=": "Mul",
                      "/=": "Tdiv"}[stmt.op]
            self._need_rt(helper)
            return [f"{p}{binding.java} = NewrRt.{helper}({binding.java}, "
                    f"{value});"]
        # float / str / %= : bare augmented assignment (no overflow
        # check applies; `+=` on a str is concatenation; `%` on an
        # int cannot overflow — long.MinValue % -1 is 0 in C#).
        return [f"{p}{binding.java} {stmt.op} {value};"]

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
        # (it cannot see the loop variable — `for x in x` reads the
        # enclosing `x`).
        iterable = self.emit_expr(stmt.iterable, 0)
        self._push_scope()
        binding = self._bind(stmt.var, type=elem, kind="local")
        elem_text = self._cs_type(elem)
        if it_type.name == "dict":
            # A dict iterates its keys (insertion order, like
            # Python's `for k in d`).
            header = (f"foreach ({elem_text} {binding.java} in "
                      f"{iterable}.Keys) {{")
        elif it_type.name == "str":
            # A string iterates code points (P5), not UTF-16 code
            # units.
            self._need_rt("chars")
            header = (f"foreach ({elem_text} {binding.java} in "
                      f"NewrRt.Chars({iterable})) {{")
        else:
            header = (f"foreach ({elem_text} {binding.java} in "
                      f"{iterable}) {{")
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
            lines.append(f"{p}}} catch (Exception {exc_var}) {{")
            self._push_scope()
            binding = self._bind(cname, type=Type("error"), kind="local")
            self._need_rt("asError")
            lines.append(f"{p}  NewrRt.NewrError {binding.java} = "
                         f"NewrRt.AsError({exc_var});")
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
            return [f"{p}throw new NewrRt.NewrError({text});"]
        self._fail(f"`throw` needs a `str` or an `error`, not `{t}`")

    def _emit_return(self, stmt: A.Return, indent: int) -> list[str]:
        p = "  " * indent
        ctx = self._fn_ctx[-1] if self._fn_ctx else None
        if ctx is None:
            # A bare top-level `return` exits the program (D11).
            if stmt.value is None:
                return [f"{p}return;"]
            self._fail("a top-level `return` with a value is not "
                       "supported — a top-level `return` exits the "
                       "program")
        ensures = ctx["ensures"]
        ret_type = ctx["return_type"]
        is_void = ret_type is None or _is_none(ret_type)
        if stmt.value is None:
            if not ensures:
                if ctx.get("in_lambda") and is_void:
                    # A bare `return;` inside a block-body lambda
                    # whose type is Func<object>: a void return does
                    # not convert, so return null instead.
                    return [f"{p}return null;"]
                return [f"{p}return;"]
            lines = [f"{p}_newrResult = null;"]
            lines.extend(self._ensures_check_lines(ensures, indent))
            lines.append(f"{p}return;" if is_void
                         else f"{p}return _newrResult;")
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
                f"{p}  throw new NewrRt.NewrError("
                f"{_cs_str('ensures failed: ' + msg)});")
            lines.append(f"{p}}}")
        return lines

    # ------------------------------------------------------------ log statement

    def _emit_log_stmt(self, stmt: A.Log, indent: int) -> list[str]:
        """`log <expr>` is info level (P10, J12's C# twin:
        info -> stdout)."""
        p = "  " * indent
        if isinstance(stmt.value, A.Propagate):
            value = self._propagate_unwrapped(stmt.value)
        else:
            value = self.emit_expr(stmt.value, 0)
        self._used_log = True
        return [f"{p}Console.WriteLine({value});"]

    # ------------------------------------------------------------ match (D12)

    def _find_struct_decl(self, name: str):
        decl = self.structs.get(name)
        if decl is not None:
            return decl
        for sub in self.program.modules.values():
            for s in sub.structs:
                if s.name == name:
                    return s
        return None

    def _find_enum_decl(self, name: str):
        decl = self.enums.get(name)
        if decl is not None:
            return decl
        for sub in self.program.modules.values():
            for e in sub.enums:
                if e.name == name:
                    return e
        return None

    def _emit_match(self, stmt: A.Match, indent: int) -> list[str]:
        p = "  " * indent
        temp = self._new_match_name()
        scrut = self.emit_expr(stmt.scrutinee, 0)
        scrut_type = self._type_of(stmt.scrutinee)
        lines = [f"{p}{self._cs_type(scrut_type)} {temp} = "
                 f"{scrut};"]
        arms = []
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
                        java=expr_text, type=typ, kind="local")
                guard = self.emit_expr(arm.guard, 0)
                self._pop_scope()
                cond = f"{cond} && {guard}" if cond else guard
            arms.append((cond, bindings, arm))
        # The last unconditional arm becomes `else:`; otherwise the
        # final `else` throws (defense — the checker proves
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
                # The binding's type is known from the pattern in
                # almost every case; when it is not (an imported
                # enum's field the emitter could not resolve), `var`
                # infers it from the field access.
                type_text = self._cs_type(typ) if typ is not None \
                    else "var"
                lines.append(f"{p}  {type_text} {b.java} = {expr_text};")
            lines.extend(self._emit_block(arm.body, indent + 1,
                                          new_scope=False))
            self._pop_scope()
        if arms:
            lines.append(f"{p}}}")
        if not last_uncond:
            lines.append(f"{p}else {{")
            self._need_rt("NewrError")
            lines.append(
                f"{p}  throw new NewrRt.NewrError(\"non-exhaustive match\");")
            lines.append(f"{p}}}")
        return lines

    def _pattern_condition(self, pattern, expr_text: str,
                           typ: Type | None):
        """Compile a pattern against the C# expression `expr_text`
        (usually the match temp) and the newr type `typ` of the value
        being matched. Returns (condition_text_or_None, bindings)
        where bindings is a list of (newr name, C# expression text,
        type). A `None` condition means the arm always matches
        (wildcard / name binding)."""
        if isinstance(pattern, A.WildcardPattern):
            return None, []
        if isinstance(pattern, A.NamePattern):
            return None, [(pattern.name, expr_text, typ)]
        if isinstance(pattern, A.LitPattern):
            v = pattern.value
            if v is None:
                return f"{expr_text} == null", []
            if isinstance(v, bool):
                return (f"object.Equals({expr_text}, "
                        f"{'true' if v else 'false'})", [])
            if isinstance(v, int):
                return f"object.Equals({expr_text}, {v}L)", []
            if isinstance(v, float):
                return (f"object.Equals({expr_text}, "
                        f"{_cs_float(v)})", [])
            return f"object.Equals({expr_text}, {_cs_str(v)})", []
        if isinstance(pattern, A.EnumPattern):
            # The enum is ONE class; the variant is its discriminant
            # field `_newr_variant` (a null scrutinee matches no
            # variant — the `none` pattern handles it).
            cond = (f"({expr_text} != null && {expr_text}._newr_variant"
                    f".Equals({_cs_str(pattern.variant)}))")
            bindings: list = []
            enum = self._find_enum_decl(pattern.enum_name)
            fields = {}
            if enum is not None:
                for var in enum.variants:
                    if var.name == pattern.variant:
                        fields = {f.name: f.type for f in var.fields}
            for fname, sub, _span in pattern.bindings:
                sub_cond, sub_bind = self._pattern_condition(
                    sub, self._enum_field_access(
                        expr_text, pattern.enum_name, fname,
                        fields.get(fname)),
                    fields.get(fname))
                if sub_cond:
                    cond += f" && {sub_cond}"
                bindings.extend(sub_bind)
            return cond, bindings
        if isinstance(pattern, A.StructPattern):
            struct_java = self._struct_cs_name(pattern.struct_name)
            cond = f"{expr_text} is {struct_java}"
            bindings = []
            struct = self._find_struct_decl(pattern.struct_name)
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
            # C# lists are `List<T>`: the runtime check is the
            # non-generic `IList` (any `List<T>` implements it,
            # and `null is IList` is false, so this is also the
            # null guard), and element access is the `[i]` indexer
            # (Java's `.get(i)` does not exist in C#).
            cond = (f"({expr_text} is System.Collections.IList && "
                    f"((System.Collections.IList){expr_text}).Count "
                    f"== {len(pattern.elements)})")
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
        temp_type = self._cs_type(t)
        if ctx.get("in_handler"):
            # A handler returns nothing, so `?` just exits the
            # handler (a bare `return`) — there is no value to
            # propagate. (A handler that may exit early without
            # answering is the discord checker's E2010, not the
            # emitter's problem.)
            if t.name == "Result" and t.params and not t.nullable:
                self._hoists_add([
                    f"{temp_type} {temp} = {text};",
                    # §18 fix 1 (JS twin): isErr is a METHOD on the
                    # Result — a bare access is the method itself
                    # (always truthy), so `?` early-returned on
                    # every value. The unwrapped value lives in
                    # `._value` (there is no `.value` field).
                    f"if ({temp}.IsErr()) {{",
                    "  return;",
                    "}",
                ])
                return f"{temp}._value"
            if t.nullable and t.name != "Result":
                self._hoists_add([
                    f"{temp_type} {temp} = {text};",
                    f"if ({temp} == null) {{",
                    "  return;",
                    "}",
                ])
                # A nullable VALUE type unwraps to `.Value` (after
                # the null check it is known non-null): a bare
                # `long?` cannot be assigned to a `long` (CS0266).
                # A reference type needs no unwrap.
                if _is_cs_value_type(t):
                    return f"{temp}.Value"
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
            self._hoists_add([
                f"{temp_type} {temp} = {text};",
                f"if ({temp}.IsErr()) {{",
                f"  return {temp};",
                "}",
            ])
            return f"{temp}._value"
        if t.nullable:
            if ret is None or not ret.nullable:
                self._fail("`?` on a nullable value inside a function "
                           "that does not return a nullable (D30 — the "
                           "checker should have rejected this)")
            self._hoists_add([
                f"{temp_type} {temp} = {text};",
                f"if ({temp} == null) {{",
                "  return null;",
                "}",
            ])
            if _is_cs_value_type(t):
                return f"{temp}.Value"
            return temp
        self._fail("`?` needs a `Result[T, E]` or a `T?` value")

    # ------------------------------------------------------------ module calls

    def _emit_module_call(self, node: A.Call,
                          binding: _Binding) -> tuple[str, int]:
        if binding.kind == "file-module":
            # A function in an imported .newr file: a plain call on
            # the imported module — and the import itself (K1: one
            # `.java` per imported `.newr`, named by its module
            # name). Struct construction through a file module needs
            # `new` (a C# class is not callable without it).
            ref = self._file_module_class(binding)
            name = node.func.name
            sub = self.program.modules.get(binding.java)
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
                        return (f"new {ref}.{self._member_name(name, ref)}"
                                f"({', '.join(parts)})", _PREC["primary"])
            args = [self.emit_expr(a, 0) for a in node.args]
            kwargs = self._emit_module_kwargs(node, sub)
            return (f"{ref}.{self._member_name(name, ref)}"
                    f"({', '.join(args + kwargs)})", _PREC["primary"])
        real = binding.module or binding.java
        if real == "file":
            name = node.func.name
            if name in ("read", "write", "append"):
                self._need_rt(f"file{name.capitalize()}")
                args = [self.emit_expr(a, 0) for a in node.args]
                return (f"NewrRt.File{name.capitalize()}"
                        f"({', '.join(args)})", _PREC["primary"])
            if name == "exists":
                self._need_rt("fileExists")
                return (f"NewrRt.FileExists"
                        f"({self.emit_expr(node.args[0], 0)})",
                        _PREC["primary"])
            self._fail(f"module `file` has no function `{name}`")
        if real == "db":
            args = [self.emit_expr(a, 0) for a in node.args]
            return self._emit_db_call(node, args)
        if real == "http":
            args = [self.emit_expr(a, 0) for a in node.args]
            return self._emit_http_call(node, args)
        if real == "json":
            args = [self.emit_expr(a, 0) for a in node.args]
            return self._emit_json_call(node, args)
        if real == "ui":
            return self._emit_ui_call(node)
        args = [self.emit_expr(a, 0) for a in node.args]
        return self._emit_module_call_text(real, binding.java,
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
        """The C# call text for a named-module function. `real` is
        the module name (for dispatch); `ref` is the C# reference
        (the alias, when the import is aliased)."""
        if real == "math":
            if name == "sqrt":
                return f"Math.Sqrt({args[0]})", _PREC["primary"]
            if name == "floor":
                self._need_rt("toInt")
                return f"NewrRt.ToInt(Math.Floor({args[0]}))", \
                    _PREC["primary"]
            if name == "ceil":
                self._need_rt("toInt")
                return f"NewrRt.ToInt(Math.Ceiling({args[0]}))", \
                    _PREC["primary"]
        if real == "time":
            if name == "now":
                # .NET's Unix seconds, no runtime helper needed
                # (DateTimeOffset already returns a long).
                return ("DateTimeOffset.UtcNow.ToUnixTimeSeconds()",
                        _PREC["primary"])
            if name == "format":
                self._need_rt("timeFormat")
                return f"NewrRt.TimeFormat({args[0]}, {args[1]})", \
                    _PREC["primary"]
        if real == "random":
            if name == "int":
                self._need_rt("randomInt")
                return f"NewrRt.RandomInt({args[0]}, {args[1]})", \
                    _PREC["primary"]
            if name == "choice":
                self._need_rt("randomChoice")
                return f"NewrRt.RandomChoice({args[0]})", _PREC["primary"]
            if name == "shuffle":
                self._need_rt("randomShuffle")
                return f"NewrRt.RandomShuffle({args[0]})", \
                    _PREC["primary"]
        if real == "env":
            if name == "get":
                self._need_rt("envGet")
                if len(args) == 2:
                    return (f"NewrRt.EnvGet({args[0]}, {args[1]})",
                            _PREC["primary"])
                return f"NewrRt.EnvGet({args[0]})", _PREC["primary"]
        self._fail(f"module `{real}` has no function `{name}`")

    def _emit_db_call(self, node: A.Call,
                      args: list[str]) -> tuple[str, int]:
        """`db.*` calls (SPEC.md section 12.5): the helpers live in
        the `NewrDb` class emitted into this file (see
        `_emit_db_class`) — inline, not in newr_rt.cs, because the
        runtime file is dependency-free and a program that never
        uses `db` must not need the Sqlite package. Handles are
        longs, fd-style. The checker (db_check) has already
        validated the call, so the arity here is trusted.
        `db.query` maps each converted `object[]` row to the row
        struct through `QueryAs<T>`."""
        name = node.func.name
        self._need_sqlite()
        self._need_rt("NewrError")
        self._file_uses_db = True
        if name == "open":
            return f"NewrDb.Open({args[0]})", _PREC["primary"]
        if name == "exec":
            rest = ", ".join(args[2:])
            if rest:
                return (f"NewrDb.Exec({args[0]}, {args[1]}, {rest})",
                        _PREC["primary"])
            return f"NewrDb.Exec({args[0]}, {args[1]})", _PREC["primary"]
        if name == "query":
            cs_struct, decl = self._db_row_struct(node)
            names = [_cs_str(f.name) for f in decl.fields]
            types = [_cs_str(self._db_field_type(f.type))
                     for f in decl.fields]
            casts = ", ".join(
                f"({self._cs_type(f.type)}) row[{i}]"
                for i, f in enumerate(decl.fields))
            names_lit = "new string[] {" + ", ".join(names) + "}"
            types_lit = "new string[] {" + ", ".join(types) + "}"
            rest = ", ".join(args[2:])
            return (f"NewrDb.QueryAs<{cs_struct}>({args[0]}, {args[1]}, "
                    f"new object[] {{{rest}}}, {names_lit}, {types_lit}, "
                    f"row => new {cs_struct}({casts}))",
                    _PREC["primary"])
        if name == "begin":
            return f"NewrDb.Begin({args[0]})", _PREC["primary"]
        if name == "commit":
            return f"NewrDb.Commit({args[0]})", _PREC["primary"]
        if name == "rollback":
            return f"NewrDb.Rollback({args[0]})", _PREC["primary"]
        if name == "last_id":
            return f"NewrDb.LastId({args[0]})", _PREC["primary"]
        if name == "close":
            return f"NewrDb.Close({args[0]})", _PREC["primary"]
        self._fail(f"module `db` has no function `{name}`")

    def _db_row_struct(self, node: A.Call) -> tuple[str, A.StructDecl]:
        """The (C# constructor, decl) of the row struct for a
        `db.query` call. The row type comes from `node.expr_type`,
        set by db_check to `list[Row]`. A struct from an imported
        file module resolves to its qualified class name."""
        t = self._type_of(node)
        if t.name != "list" or not t.params:
            self._fail("internal: db.query without a list[Row] type")
        row_name = t.params[0].name
        decl = self.structs.get(row_name)
        if decl is not None:
            return self._struct_cs_name(row_name), decl
        for bound, sub in self.program.modules.items():
            for s in sub.structs:
                if s.name == row_name:
                    return self._struct_cs_name(row_name), s
        self._fail(f"db.query: row struct `{row_name}` not found")

    @staticmethod
    def _db_field_type(t: Type) -> str:
        """The runtime type string for a row field: the base name,
        with `?` when nullable. Row fields are only int/float/str/
        bool (db_check enforces it), so no deeper shape is needed."""
        return t.name + ("?" if t.nullable else "")

    # ------------------------------------------------------------ http/json

    def _struct_decl(self, name: str) -> A.StructDecl:
        """The StructDecl for a struct type name (full name, then
        the short name after a dot, then imported file modules),
        mirroring the Python emitter's `_struct_decl`."""
        short = name.rsplit(".", 1)[-1] if "." in name else name
        decl = self.structs.get(name) or self.structs.get(short)
        if decl is not None:
            return decl
        for sub in self.program.modules.values():
            for s in sub.structs:
                if s.name == short:
                    return s
        self._fail(f"struct `{name}` not found")

    def _shape_literal(self, t: Type) -> str:
        """C# source for the shape descriptor of a static type
        (SPEC.md sections 12.6, 12.7), mirroring the Python
        emitter's `_shape_literal`: a scalar is `"int"` /
        `"float"` / `"str"` / `"bool"` / `"none"`, a nullable is
        `new object[] {"?", inner}`, a list is
        `new object[] {"list", elem}`, a dict is
        `new object[] {"dict", value}`, a struct is
        `new object[] {"struct", Name, new object[] { new object[]
        {field, shape}, ... }}` in declaration order, where Name
        is the struct's short name (the runtime resolves it by
        reflection). The runtime walks the shape for json.encode /
        decode and for building the http request/response structs."""
        if t.nullable:
            inner = Type(t.name, t.params)
            return ("new object[] {\"?\", "
                    f"{self._shape_literal(inner)}}}")
        if t.name in ("int", "float", "str", "bool", "none"):
            return _cs_str(t.name)
        if t.name == "list":
            if len(t.params) != 1:
                self._fail("internal: list type without one element "
                           f"type: `{t}`")
            return ("new object[] {\"list\", "
                    f"{self._shape_literal(t.params[0])}}}")
        if t.name == "dict":
            if len(t.params) != 2:
                self._fail("internal: dict type without two params: "
                           f"`{t}`")
            return ("new object[] {\"dict\", "
                    f"{self._shape_literal(t.params[1])}}}")
        if t.name in ("fn", "Result", "set", "error", "duration"):
            self._fail(f"internal: type `{t}` has no JSON shape")
        decl = self._struct_decl(t.name)
        short = t.name.rsplit(".", 1)[-1] if "." in t.name else t.name
        fields = ", ".join(
            "new object[] {" + _cs_str(f.name) + ", "
            + self._shape_literal(f.type) + "}"
            for f in decl.fields)
        return ("new object[] {\"struct\", " + _cs_str(short)
                + ", new object[] {" + fields + "}}")

    def _http_handler_request_type(self, handler_arg: A.Expr) -> Type:
        """The request struct type of a route handler: the parameter
        type of an inline lambda, or the first parameter of the fn
        type of a let-bound lambda variable (the checker tracks
        let-bound lambdas the same way, in http_check._lambdas).
        A named function is not a handler in M8b."""
        if isinstance(handler_arg, A.Lambda):
            if len(handler_arg.params) != 1:
                self._fail("a route handler takes exactly one "
                           "parameter (the request)",
                           span=handler_arg.span)
            return handler_arg.params[0].type
        if isinstance(handler_arg, A.Name):
            binding = self._lookup(handler_arg.name)
            if binding is not None and binding.type is not None:
                t = binding.type
                if t.name == "fn" and t.params:
                    return t.params[0]
            self._fail(f"route handler `{handler_arg.name}` is not a "
                       "lambda", span=handler_arg.span)
        self._fail("a route handler must be a lambda",
                   span=handler_arg.span)

    def _emit_http_call(self, node: A.Call,
                        args: list[str]) -> tuple[str, int]:
        """`http.*` calls (SPEC.md section 12.6): the helpers live
        in the `NewrHttp` class emitted into this file (see
        `_emit_http_class`), inline, not in newr_rt.cs, because
        the runtime file is dependency-free and a program that
        never uses `http` must not need a new package. The checker
        (http_check) has already validated the call, so the arity
        here is trusted. Fetch calls carry the response struct
        shape from `node.expr_type`, set by http_check; route calls
        carry the request struct shape from the handler lambda's
        parameter type."""
        name = node.func.name
        self._need_http()
        self._need_rt("NewrError")
        if name == "route":
            req_type = self._http_handler_request_type(node.args[2])
            shape = self._shape_literal(req_type)
            return (f"NewrHttp.Route({args[0]}, {args[1]}, {args[2]}, "
                    f"{shape})", _PREC["primary"])
        if name == "serve":
            return f"NewrHttp.Serve({', '.join(args)})", _PREC["primary"]
        if name in ("get", "post", "request"):
            t = self._type_of(node)
            shape = self._shape_literal(t)
            resp_cs = self._cs_type(t)
            if name == "get":
                timeout = args[1] if len(args) > 1 else "10"
                headers = args[2] if len(args) > 2 else "null"
                return (f"({resp_cs}) NewrHttp.Get({args[0]}, {timeout}, "
                        f"{headers}, {shape})", _PREC["primary"])
            if name == "post":
                timeout = args[2] if len(args) > 2 else "10"
                headers = args[3] if len(args) > 3 else "null"
                return (f"({resp_cs}) NewrHttp.Post({args[0]}, {args[1]}, "
                        f"{timeout}, {headers}, {shape})",
                        _PREC["primary"])
            timeout = args[3] if len(args) > 3 else "10"
            headers = args[4] if len(args) > 4 else "null"
            return (f"({resp_cs}) NewrHttp.Request({args[0]}, {args[1]}, "
                    f"{args[2]}, {timeout}, {headers}, {shape})",
                    _PREC["primary"])
        self._fail(f"module `http` has no function `{name}`")

    def _emit_json_call(self, node: A.Call,
                        args: list[str]) -> tuple[str, int]:
        """`json.*` calls (SPEC.md section 12.7): the helpers live
        in the `NewrJson` class emitted into this file (see
        `_emit_json_class`). `json.encode` takes its shape from
        the argument's static type; `json.decode` takes its shape
        from `node.expr_type`, the expected type set by json_check."""
        name = node.func.name
        self._need_json()
        self._need_rt("NewrError")
        if name == "encode":
            shape = self._shape_literal(self._type_of(node.args[0]))
            return (f"NewrJson.Encode({args[0]}, {shape})",
                    _PREC["primary"])
        if name == "decode":
            t = self._type_of(node)
            shape = self._shape_literal(t)
            return (f"NewrJson.Decode<{self._cs_boxed_type(t)}>"
                    f"({args[0]}, {shape})", _PREC["primary"])
        self._fail(f"module `json` has no function `{name}`")

    def _emit_ui_call(self, node: A.Call) -> tuple[str, int]:
        """`ui.*` calls (SPEC.md section 12.8): the helpers live
        in the `NewrUi` class emitted into this file (see
        `_emit_ui_class`), inline, not in newr_rt.cs, because the
        runtime file is dependency-free and a program that never
        uses `ui` must not need Avalonia. The checker (ui_check)
        has already validated the call, so the arity here is
        trusted. Constructors build a `NewrUi.Widget`; `render`
        is the pure HTML core; `show` and `run` open the Avalonia
        window. Handler arguments are the fn values the
        call-site emitter already produced, with one C# wrinkle
        Java does not have: a `void` method (a named `fn() ->
        none`) and a `void` lambda body do NOT convert to
        `Func<object>`, so a named function used as a handler is
        wrapped in a lambda that calls it and returns null (the
        inline-lambda case is fixed in `_emit_lambda` instead,
        where every none-returning lambda is wrapped the same
        way). A let-bound lambda variable already has the
        `Func<object>` / `Func<string, object>` type, so it
        passes through unwrapped; a named view function returns
        a Widget, so its method group converts to
        `Func<NewrUi.Widget>` directly."""
        name = node.func.name
        self._need_ui()
        if name not in ("text", "button", "input", "list", "image",
                        "row", "column", "render", "show", "run"):
            self._fail(f"module `ui` has no function `{name}`")
        # Handler argument index per call: button takes its
        # handler second, input third, run takes the view first.
        handler_idx = {"button": 1, "input": 2, "run": 0}.get(name)
        args: list[str] = []
        for i, arg in enumerate(node.args):
            if i == handler_idx and isinstance(arg, A.Name):
                binding = self._lookup(arg.name)
                if binding is not None and binding.kind == "fn":
                    # A named function as a handler or view: wrap
                    # it, because a void method group does not
                    # convert to Func<object>. The fn type's params
                    # (all but the last, which is the return type)
                    # give the wrapper's arity.
                    fn_type = binding.type
                    n_params = (len(fn_type.params) - 1
                                if fn_type is not None
                                and _is_fn(fn_type) else 0)
                    params = [self._new_temp_name()
                              for _ in range(n_params)]
                    plist = ", ".join(params)
                    target = self.emit_expr(arg, 0)
                    if fn_type is not None and _is_fn(fn_type) \
                            and _is_none(fn_type.params[-1]):
                        args.append(
                            f"({plist}) => {{ {target}({plist}); "
                            f"return null; }}")
                    else:
                        args.append(
                            f"({plist}) => {target}({plist})")
                    continue
            args.append(self.emit_expr(arg, 0))
        method = {"text": "Text", "button": "Button",
                  "input": "Input", "list": "List",
                  "image": "Image", "row": "Row",
                  "column": "Column", "render": "Render",
                  "show": "Show", "run": "Run"}[name]
        return (f"{self._ui_ref()}.{method}({', '.join(args)})",
                _PREC["primary"])

    def _emit_ui_class(self) -> list[str]:
        """The `NewrUi` helper class, emitted into a file that uses
        `ui.*` (at indent 1, nested in the program class). The
        Widget model class plus the constructors, the pure HTML
        render core (byte-identical to the Python reference in
        newr_rt.py, SPEC.md section 12.8), and the Avalonia
        window: `Show` builds one static window and blocks until
        it closes; `Run` re-invokes the view after every handler
        call and rebuilds the window content from the new tree.
        The class is public, not private like NewrHttp/NewrJson:
        Widget values leak into public signatures (a top-level
        `let w: Widget` is a public static field, a `fn view()
        -> Widget` is a public static method), and a private
        nested type there is CS0052. The window is built in code
        (no XAML files): the App subclass adds the Fluent theme
        in Initialize and builds the window in
        OnFrameworkInitializationCompleted. An image widget shows
        a placeholder label with the URL (the Python reference
        does the same; a load failure is not an error, SPEC.md
        section 12.8 U6). State lives outside the view (a db
        handle or an http backend); the view re-reads it each
        time, so the handlers never capture mutable state."""
        return [
            "  public static class NewrUi {",
            "    public sealed class Widget {",
            "      public string Kind { get; }",
            "      public string Text { get; }",
            "      public string Label { get; }",
            "      public Func<object> ButtonHandler { get; }",
            "      public Func<string, object> InputHandler { get; }",
            "      public string Placeholder { get; }",
            "      public string Value { get; }",
            "      public string Url { get; }",
            "      public List<Widget> Children { get; }",
            "",
            "      private Widget(string kind, string text,",
            "          string label, Func<object> buttonHandler,",
            "          Func<string, object> inputHandler,",
            "          string placeholder, string value, string url,",
            "          List<Widget> children) {",
            "        Kind = kind;",
            "        Text = text;",
            "        Label = label;",
            "        ButtonHandler = buttonHandler;",
            "        InputHandler = inputHandler;",
            "        Placeholder = placeholder;",
            "        Value = value;",
            "        Url = url;",
            "        Children = children;",
            "      }",
            "",
            "      internal static Widget MakeText(string text) {",
            "        return new Widget(\"text\", text, \"\", null, null,",
            "            \"\", \"\", \"\", new List<Widget>());",
            "      }",
            "",
            "      internal static Widget MakeButton(string label,",
            "          Func<object> handler) {",
            "        return new Widget(\"button\", \"\", label, handler,",
            "            null, \"\", \"\", \"\", new List<Widget>());",
            "      }",
            "",
            "      internal static Widget MakeInput(string placeholder,",
            "          string value, Func<string, object> handler) {",
            "        return new Widget(\"input\", \"\", \"\", null, handler,",
            "            placeholder, value, \"\", new List<Widget>());",
            "      }",
            "",
            "      internal static Widget MakeList(List<Widget> children) {",
            "        return new Widget(\"list\", \"\", \"\", null, null,",
            "            \"\", \"\", \"\", new List<Widget>(children));",
            "      }",
            "",
            "      internal static Widget MakeImage(string url) {",
            "        return new Widget(\"image\", \"\", \"\", null, null,",
            "            \"\", \"\", url, new List<Widget>());",
            "      }",
            "",
            "      internal static Widget MakeRow(List<Widget> children) {",
            "        return new Widget(\"row\", \"\", \"\", null, null,",
            "            \"\", \"\", \"\", new List<Widget>(children));",
            "      }",
            "",
            "      internal static Widget MakeColumn(",
            "          List<Widget> children) {",
            "        return new Widget(\"column\", \"\", \"\", null, null,",
            "            \"\", \"\", \"\", new List<Widget>(children));",
            "      }",
            "    }",
            "",
            "    public static Widget Text(string text) {",
            "      return Widget.MakeText(text);",
            "    }",
            "",
            "    public static Widget Button(string label,",
            "        Func<object> handler) {",
            "      return Widget.MakeButton(label, handler);",
            "    }",
            "",
            "    public static Widget Input(string placeholder,",
            "        string value, Func<string, object> handler) {",
            "      return Widget.MakeInput(placeholder, value, handler);",
            "    }",
            "",
            "    public static Widget List(List<Widget> children) {",
            "      return Widget.MakeList(children);",
            "    }",
            "",
            "    public static Widget Image(string url) {",
            "      return Widget.MakeImage(url);",
            "    }",
            "",
            "    public static Widget Row(List<Widget> children) {",
            "      return Widget.MakeRow(children);",
            "    }",
            "",
            "    public static Widget Column(List<Widget> children) {",
            "      return Widget.MakeColumn(children);",
            "    }",
            "",
            "    public static string Escape(string s) {",
            "      return s.Replace(\"&\", \"&amp;\").Replace(\"<\", \"&lt;\")",
            "          .Replace(\">\", \"&gt;\").Replace(\"\\\"\", \"&quot;\");",
            "    }",
            "",
            "    public static string Render(Widget w) {",
            "      int buttons = 0;",
            "      int inputs = 0;",
            "      return RenderWidget(w, ref buttons, ref inputs);",
            "    }",
            "",
            "    private static string RenderWidget(Widget w,",
            "        ref int buttons, ref int inputs) {",
            "      switch (w.Kind) {",
            "        case \"text\":",
            "          return \"<p>\" + Escape(w.Text) + \"</p>\";",
            "        case \"button\": {",
            "          int n = buttons++;",
            "          return \"<button data-action=\\\"\" + n + \"\\\">\"",
            "              + Escape(w.Label) + \"</button>\";",
            "        }",
            "        case \"input\": {",
            "          int n = inputs++;",
            "          return \"<input placeholder=\\\"\"",
            "              + Escape(w.Placeholder) + \"\\\" value=\\\"\"",
            "              + Escape(w.Value) + \"\\\" data-input=\\\"\"",
            "              + n + \"\\\">\";",
            "        }",
            "        case \"list\": {",
            "          StringBuilder sb = new StringBuilder();",
            "          sb.Append(\"<ul>\");",
            "          foreach (Widget c in w.Children) {",
            "            sb.Append(\"<li>\");",
            "            sb.Append(RenderWidget(c, ref buttons,",
            "                ref inputs));",
            "            sb.Append(\"</li>\");",
            "          }",
            "          sb.Append(\"</ul>\");",
            "          return sb.ToString();",
            "        }",
            "        case \"image\":",
            "          return \"<img src=\\\"\" + Escape(w.Url)",
            "              + \"\\\" alt=\\\"\\\">\";",
            "        case \"row\": {",
            "          StringBuilder sb = new StringBuilder();",
            "          sb.Append(\"<div data-layout=\\\"row\\\">\");",
            "          foreach (Widget c in w.Children) {",
            "            sb.Append(RenderWidget(c, ref buttons,",
            "                ref inputs));",
            "          }",
            "          sb.Append(\"</div>\");",
            "          return sb.ToString();",
            "        }",
            "        case \"column\": {",
            "          StringBuilder sb = new StringBuilder();",
            "          sb.Append(\"<div data-layout=\\\"column\\\">\");",
            "          foreach (Widget c in w.Children) {",
            "            sb.Append(RenderWidget(c, ref buttons,",
            "                ref inputs));",
            "          }",
            "          sb.Append(\"</div>\");",
            "          return sb.ToString();",
            "        }",
            "        default:",
            "          throw new InvalidOperationException(",
            "              \"ui: unknown widget kind \" + w.Kind);",
            "      }",
            "    }",
            "",
            "    private static Func<Widget> _view;",
            "    private static Widget _root;",
            "    private static Window _window;",
            "    private static bool _rerendering;",
            "",
            "    public static void Show(Widget root) {",
            "      _view = null;",
            "      _root = root;",
            "      AppBuilder.Configure<UiApp>().UsePlatformDetect()",
            "          .StartWithClassicDesktopLifetime(",
            "              Array.Empty<string>());",
            "    }",
            "",
            "    public static void Run(Func<Widget> view) {",
            "      _view = view;",
            "      _root = null;",
            "      AppBuilder.Configure<UiApp>().UsePlatformDetect()",
            "          .StartWithClassicDesktopLifetime(",
            "              Array.Empty<string>());",
            "    }",
            "",
            "    private static void Rerender() {",
            "      if (_window == null || _view == null || _rerendering) {",
            "        return;",
            "      }",
            "      _rerendering = true;",
            "      try {",
            "        _window.Content = Build(_view(), Rerender);",
            "      } finally {",
            "        _rerendering = false;",
            "      }",
            "    }",
            "",
            "    private static Control Build(Widget w, Action rerender) {",
            "      switch (w.Kind) {",
            "        case \"text\":",
            "          return new TextBlock { Text = w.Text };",
            "        case \"button\": {",
            "          Button b = new Button { Content = w.Label };",
            "          Func<object> handler = w.ButtonHandler;",
            "          b.Click += (_, _) => {",
            "            handler();",
            "            if (rerender != null) {",
            "              rerender();",
            "            }",
            "          };",
            "          return b;",
            "        }",
            "        case \"input\": {",
            "          TextBox t = new TextBox {",
            "            PlaceholderText = w.Placeholder, Text = w.Value };",
            "          Func<string, object> handler = w.InputHandler;",
            "          t.KeyDown += (_, e) => {",
            "            if (e.Key == Key.Return) {",
            "              handler(t.Text ?? \"\");",
            "              if (rerender != null) {",
            "                rerender();",
            "              }",
            "            }",
            "          };",
            "          t.LostFocus += (_, _) => {",
            "            handler(t.Text ?? \"\");",
            "            if (rerender != null) {",
            "              rerender();",
            "            }",
            "          };",
            "          return t;",
            "        }",
            "        case \"list\": {",
            "          StackPanel p = new StackPanel {",
            "            Orientation = Orientation.Vertical };",
            "          foreach (Widget c in w.Children) {",
            "            p.Children.Add(Build(c, rerender));",
            "          }",
            "          return p;",
            "        }",
            "        case \"image\":",
            "          return new TextBlock {",
            "            Text = \"[image: \" + w.Url + \"]\" };",
            "        case \"row\": {",
            "          StackPanel p = new StackPanel {",
            "            Orientation = Orientation.Horizontal };",
            "          foreach (Widget c in w.Children) {",
            "            p.Children.Add(Build(c, rerender));",
            "          }",
            "          return p;",
            "        }",
            "        case \"column\": {",
            "          StackPanel p = new StackPanel {",
            "            Orientation = Orientation.Vertical };",
            "          foreach (Widget c in w.Children) {",
            "            p.Children.Add(Build(c, rerender));",
            "          }",
            "          return p;",
            "        }",
            "        default:",
            "          throw new InvalidOperationException(",
            "              \"ui: unknown widget kind \" + w.Kind);",
            "      }",
            "    }",
            "",
            "    private sealed class UiApp : Application {",
            "      public UiApp() {",
            "      }",
            "",
            "      public override void Initialize() {",
            "        Styles.Add(new FluentTheme());",
            "      }",
            "",
            "      public override void OnFrameworkInitializationCompleted() {",
            "        if (ApplicationLifetime",
            "            is IClassicDesktopStyleApplicationLifetime desktop) {",
            "          Window window = new Window {",
            "            Title = \"newr\", Width = 400, Height = 300 };",
            "          if (_view != null) {",
            "            _window = window;",
            "            window.Content = Build(_view(), Rerender);",
            "          } else {",
            "            window.Content = Build(_root, null);",
            "          }",
            "          desktop.MainWindow = window;",
            "        }",
            "        base.OnFrameworkInitializationCompleted();",
            "      }",
            "    }",
            "  }",
        ]

    def _emit_db_class(self) -> list[str]:
        """The `NewrDb` helper class, emitted into a file that uses
        `db.*` (at indent 1, nested in the program class). The int
        handle registry plus open/exec/query/begin/commit/rollback/
        last_id/close over Microsoft.Data.Sqlite (the store's
        library, 8.0.0 — the same sync API the store uses).
        Conversion mirrors the Python `_db_value` (newr_rt.py) and
        the Java `_dbValue` (newr_rt.java): NULL lands only in a
        nullable field, `?` binds are positional (the Nth added
        parameter binds the Nth `?`), bool binds as 0/1, a value
        that cannot convert throws instead of silently producing
        a wrong value. `Query` looks rows up by column NAME (a
        missing column throws, extra columns are ignored)."""
        return [
            "  private static class NewrDb {",
            "    private static readonly Dictionary<long, SqliteConnection> Conns = new();",
            "    private static long NextId = 1;",
            "",
            "    private static SqliteConnection Conn(long handle) {",
            "      if (!Conns.TryGetValue(handle, out SqliteConnection conn)) {",
            "        throw new NewrRt.NewrError(\"db: unknown or closed handle \" + handle);",
            "      }",
            "      return conn;",
            "    }",
            "",
            "    private static object BindArg(object value) {",
            "      if (value == null) {",
            "        return DBNull.Value;",
            "      }",
            "      if (value is bool b) {",
            "        return b ? 1L : 0L;",
            "      }",
            "      return value;",
            "    }",
            "",
            "    private static object DbValue(object value, string typeStr) {",
            "      bool nullable = typeStr.EndsWith(\"?\");",
            "      string baseType = nullable ? typeStr.Substring(0, typeStr.Length - 1) : typeStr;",
            "      if (value == null || value is DBNull) {",
            "        if (nullable) {",
            "          return null;",
            "        }",
            "        throw new NewrRt.NewrError(\"db: NULL in non-nullable \" + baseType + \" field\");",
            "      }",
            "      switch (baseType) {",
            "        case \"int\":",
            "          if (value is bool) {",
            "            throw new NewrRt.NewrError(\"db: cannot convert bool to int\");",
            "          }",
            "          if (value is long l) {",
            "            return l;",
            "          }",
            "          if (value is double d) {",
            "            if (d != Math.Floor(d) || d < (double) long.MinValue || d > (double) long.MaxValue) {",
            "              throw new NewrRt.NewrError(\"db: cannot convert \" + value + \" to int\");",
            "            }",
            "            return (long) d;",
            "          }",
            "          if (value is string s) {",
            "            if (long.TryParse(s.Trim(), out long parsed)) {",
            "              return parsed;",
            "            }",
            "            throw new NewrRt.NewrError(\"db: cannot convert \" + value + \" to int\");",
            "          }",
            "          throw new NewrRt.NewrError(\"db: cannot convert \" + value.GetType().Name + \" to int\");",
            "        case \"float\":",
            "          if (value is bool) {",
            "            throw new NewrRt.NewrError(\"db: cannot convert bool to float\");",
            "          }",
            "          if (value is long li) {",
            "            return (double) li;",
            "          }",
            "          if (value is double dd) {",
            "            return dd;",
            "          }",
            "          if (value is string ss) {",
            "            if (double.TryParse(ss.Trim(), System.Globalization.NumberStyles.Float, System.Globalization.CultureInfo.InvariantCulture, out double parsedF)) {",
            "              return parsedF;",
            "            }",
            "            throw new NewrRt.NewrError(\"db: cannot convert \" + value + \" to float\");",
            "          }",
            "          throw new NewrRt.NewrError(\"db: cannot convert \" + value.GetType().Name + \" to float\");",
            "        case \"str\":",
            "          if (value is bool bb) {",
            "            return bb ? \"true\" : \"false\";",
            "          }",
            "          if (value is string str) {",
            "            return str;",
            "          }",
            "          return NewrRt.ToStr(value);",
            "        case \"bool\":",
            "          if (value is bool b2) {",
            "            return b2;",
            "          }",
            "          if (value is long v) {",
            "            if (v == 0) {",
            "              return false;",
            "            }",
            "            if (v == 1) {",
            "              return true;",
            "            }",
            "            throw new NewrRt.NewrError(\"db: cannot convert \" + value + \" to bool\");",
            "          }",
            "          if (value is double dv) {",
            "            long w = (long) dv;",
            "            if (w == 0) {",
            "              return false;",
            "            }",
            "            if (w == 1) {",
            "              return true;",
            "            }",
            "            throw new NewrRt.NewrError(\"db: cannot convert \" + value + \" to bool\");",
            "          }",
            "          if (value is string s2) {",
            "            if (s2 == \"0\") {",
            "              return false;",
            "            }",
            "            if (s2 == \"1\") {",
            "              return true;",
            "            }",
            "            throw new NewrRt.NewrError(\"db: cannot convert \" + value + \" to bool\");",
            "          }",
            "          throw new NewrRt.NewrError(\"db: cannot convert \" + value.GetType().Name + \" to bool\");",
            "        default:",
            "          throw new NewrRt.NewrError(\"db: unknown field type \" + typeStr);",
            "      }",
            "    }",
            "",
            "    private static SqliteCommand Prepare(SqliteConnection conn, string sql, object[] args) {",
            "      string rewritten = RewritePlaceholders(sql, out int count);",
            "      if (args.Length != count) {",
            "        throw new NewrRt.NewrError(\"db: SQL has \" + count + \" `?` placeholder(s) but \" + args.Length + \" bound value(s)\");",
            "      }",
            "      SqliteCommand cmd = conn.CreateCommand();",
            "      cmd.CommandText = rewritten;",
            "      for (int i = 0; i < args.Length; i++) {",
            "        cmd.Parameters.AddWithValue(\"?\" + (i + 1), BindArg(args[i]));",
            "      }",
            "      return cmd;",
            "    }",
            "",
            "    private static string RewritePlaceholders(string sql, out int count) {",
            "      // Microsoft.Data.Sqlite binds `?` only by number: an",
            "      // unnamed `?` cannot take a named parameter (it errors",
            "      // \"Must add values for the following parameters\"). So",
            "      // each bare `?` outside quoted text becomes `?N`, and",
            "      // the Nth argument binds `?N` — the same positional",
            "      // rule the other targets get from their drivers for",
            "      // free. A `?N` already in the SQL is kept as-is",
            "      // (SQLite numbers a bare `?` one above the largest",
            "      // number seen so far), and `?` inside single- or",
            "      // double-quoted text is a literal question mark, not a",
            "      // placeholder (the same rule as the checker's",
            "      // `_count_placeholders` in db_check.py).",
            "      var text = new System.Text.StringBuilder();",
            "      int max = 0;",
            "      bool inSingle = false;",
            "      bool inDouble = false;",
            "      int i = 0;",
            "      while (i < sql.Length) {",
            "        char c = sql[i];",
            "        if (inSingle) {",
            "          text.Append(c);",
            "          if (c == '\\'') {",
            "            if (i + 1 < sql.Length && sql[i + 1] == '\\'') {",
            "              text.Append(sql[i + 1]);",
            "              i += 2;",
            "              continue;",
            "            }",
            "            inSingle = false;",
            "          }",
            "          i++;",
            "          continue;",
            "        }",
            "        if (inDouble) {",
            "          text.Append(c);",
            "          if (c == '\"') {",
            "            inDouble = false;",
            "          }",
            "          i++;",
            "          continue;",
            "        }",
            "        if (c == '\\'') {",
            "          inSingle = true;",
            "          text.Append(c);",
            "          i++;",
            "          continue;",
            "        }",
            "        if (c == '\"') {",
            "          inDouble = true;",
            "          text.Append(c);",
            "          i++;",
            "          continue;",
            "        }",
            "        if (c == '?') {",
            "          int j = i + 1;",
            "          int num = 0;",
            "          while (j < sql.Length && sql[j] >= '0' && sql[j] <= '9') {",
            "            num = num * 10 + (sql[j] - '0');",
            "            j++;",
            "          }",
            "          if (j > i + 1) {",
            "            if (num > max) {",
            "              max = num;",
            "            }",
            "            text.Append(sql.Substring(i, j - i));",
            "            i = j;",
            "            continue;",
            "          }",
            "          max++;",
            "          text.Append(\"?\" + max);",
            "          i++;",
            "          continue;",
            "        }",
            "        text.Append(c);",
            "        i++;",
            "      }",
            "      count = max;",
            "      return text.ToString();",
            "    }",
            "",
            "    public static long Open(string path) {",
            "      SqliteConnection conn = new SqliteConnection(\"Data Source=\" + path);",
            "      conn.Open();",
            "      using (SqliteCommand cmd = conn.CreateCommand()) {",
            "        cmd.CommandText = \"PRAGMA foreign_keys = ON\";",
            "        cmd.ExecuteNonQuery();",
            "        cmd.CommandText = \"PRAGMA busy_timeout = 5000\";",
            "        cmd.ExecuteNonQuery();",
            "      }",
            "      long handle = NextId++;",
            "      Conns[handle] = conn;",
            "      return handle;",
            "    }",
            "",
            "    public static long Exec(long handle, string sql, params object[] args) {",
            "      using (SqliteCommand cmd = Prepare(Conn(handle), sql, args)) {",
            "        int affected = cmd.ExecuteNonQuery();",
            "        return affected >= 0 ? affected : 0;",
            "      }",
            "    }",
            "",
            "    public static List<object[]> Query(long handle, string sql, object[] args, string[] names, string[] types) {",
            "      using (SqliteCommand cmd = Prepare(Conn(handle), sql, args)) {",
            "        using (SqliteDataReader reader = cmd.ExecuteReader()) {",
            "          List<object[]> rows = new();",
            "          while (reader.Read()) {",
            "            Dictionary<string, object> byName = new();",
            "            for (int i = 0; i < reader.FieldCount; i++) {",
            "              object v = reader.GetValue(i);",
            "              byName[reader.GetName(i)] = v is DBNull ? null : v;",
            "            }",
            "            object[] row = new object[names.Length];",
            "            for (int i = 0; i < names.Length; i++) {",
            "              if (!byName.TryGetValue(names[i], out object raw)) {",
            "                throw new NewrRt.NewrError(\"db: column '\" + names[i] + \"' missing from query result\");",
            "              }",
            "              row[i] = DbValue(raw, types[i]);",
            "            }",
            "            rows.Add(row);",
            "          }",
            "          return rows;",
            "        }",
            "      }",
            "    }",
            "",
            "    public static List<T> QueryAs<T>(long handle, string sql, object[] args, string[] names, string[] types, Func<object[], T> map) {",
            "      List<object[]> rows = Query(handle, sql, args, names, types);",
            "      List<T> result = new();",
            "      foreach (object[] row in rows) {",
            "        result.Add(map(row));",
            "      }",
            "      return result;",
            "    }",
            "",
            "    public static void Begin(long handle) {",
            "      using (SqliteCommand cmd = Conn(handle).CreateCommand()) {",
            "        cmd.CommandText = \"BEGIN IMMEDIATE\";",
            "        cmd.ExecuteNonQuery();",
            "      }",
            "    }",
            "",
            "    public static void Commit(long handle) {",
            "      using (SqliteCommand cmd = Conn(handle).CreateCommand()) {",
            "        cmd.CommandText = \"COMMIT\";",
            "        cmd.ExecuteNonQuery();",
            "      }",
            "    }",
            "",
            "    public static void Rollback(long handle) {",
            "      using (SqliteCommand cmd = Conn(handle).CreateCommand()) {",
            "        cmd.CommandText = \"ROLLBACK\";",
            "        cmd.ExecuteNonQuery();",
            "      }",
            "    }",
            "",
            "    public static long LastId(long handle) {",
            "      using (SqliteCommand cmd = Conn(handle).CreateCommand()) {",
            "        cmd.CommandText = \"SELECT last_insert_rowid()\";",
            "        return (long) cmd.ExecuteScalar();",
            "      }",
            "    }",
            "",
            "    public static void Close(long handle) {",
            "      SqliteConnection conn = Conn(handle);",
            "      conn.Close();",
            "      Conns.Remove(handle);",
            "    }",
            "  }",
        ]

    def _emit_json_class(self) -> list[str]:
        """The `NewrJson` helper class, emitted into a file that uses
        `json.*` (at indent 1, nested in the program class). Typed
        JSON over System.Text.Json: the emitter passes a recursive
        shape descriptor (see `_shape_literal`), the helper walks
        it for both directions. Structs are built by reflection
        from the struct name in the shape (the emitted struct
        classes are nested in the program class, so a lookup by
        name in the executing assembly finds them; field values
        are read the same way, trying the plain field name then
        the mangled name with a trailing underscore). Floats use
        the shortest round-trip decimal with ".0" for integral
        floats (the "R" format gives plain decimal inside the
        pinned range; System.Text.Json would print "1" for 1.0).
        Decode distinguishes int from float by the JSON syntax
        (the raw text), so 1.0 into an int field always throws,
        and an integer outside int64 throws. Duplicate keys keep
        the last value, trailing data and empty input throw, a
        missing struct field throws, an extra field is ignored,
        null lands only in a nullable shape. Strings are written
        as UTF-8 with no \\u escapes (UnsafeRelaxedJsonEscaping).
        `BuildFromShape` / `FieldValue` are shared with NewrHttp:
        they build a struct from plain (non-JSON) field values
        and read one field back."""
        return [
            "  private static class NewrJson {",
            "    private static readonly Dictionary<string, Type> StructCache = new Dictionary<string, Type>();",
            "",
            "    public static string Encode(object value, object shape) {",
            "      StringBuilder sb = new StringBuilder();",
            "      WriteValue(sb, value, shape);",
            "      return sb.ToString();",
            "    }",
            "",
            "    public static T Decode<T>(string text, object shape) {",
            "      if (text == null) {",
            "        throw new NewrRt.NewrError(\"json: decode takes a string\");",
            "      }",
            "      JsonElement root;",
            "      try {",
            "        using (JsonDocument doc = JsonDocument.Parse(text)) {",
            "          root = doc.RootElement.Clone();",
            "        }",
            "      } catch (JsonException e) {",
            "        throw new NewrRt.NewrError(\"json: invalid JSON: \" + e.Message);",
            "      }",
            "      return (T) ReadValue(root, shape);",
            "    }",
            "",
            "    public static object BuildFromShape(object shape, Dictionary<string, object> values) {",
            "      if (!(shape is object[] parts) || parts.Length != 3 || !Equals(parts[0], \"struct\")) {",
            "        throw new NewrRt.NewrError(\"http: internal: expected a struct shape\");",
            "      }",
            "      object[] fields = (object[]) parts[2];",
            "      object[] args = new object[fields.Length];",
            "      for (int i = 0; i < fields.Length; i++) {",
            "        object[] field = (object[]) fields[i];",
            "        string name = (string) field[0];",
            "        if (!values.TryGetValue(name, out object v)) {",
            "          throw new NewrRt.NewrError(\"http: missing field \" + name);",
            "        }",
            "        args[i] = ConvertPlain(v, field[1]);",
            "      }",
            "      return BuildStruct((string) parts[1], args);",
            "    }",
            "",
            "    public static object FieldValue(object obj, string fieldName) {",
            "      if (obj == null) {",
            "        throw new NewrRt.NewrError(\"json: missing field \" + fieldName);",
            "      }",
            "      Type t = obj.GetType();",
            "      FieldInfo f = t.GetField(fieldName, BindingFlags.Public | BindingFlags.Instance);",
            "      if (f == null) {",
            "        f = t.GetField(fieldName + \"_\", BindingFlags.Public | BindingFlags.Instance);",
            "      }",
            "      if (f == null) {",
            "        throw new NewrRt.NewrError(\"json: missing field \" + fieldName);",
            "      }",
            "      return f.GetValue(obj);",
            "    }",
            "",
            "    private static string FmtDouble(double d) {",
            "      if (double.IsNaN(d) || double.IsInfinity(d)) {",
            "        throw new NewrRt.NewrError(\"json: cannot encode a non-finite float\");",
            "      }",
            "      string s = d.ToString(\"R\", CultureInfo.InvariantCulture);",
            "      if (s.IndexOf('.') < 0 && s.IndexOf('E') < 0 && s.IndexOf('e') < 0) {",
            "        s += \".0\";",
            "      }",
            "      return s;",
            "    }",
            "",
            "    private static string Quote(string s) {",
            "      return JsonSerializer.Serialize(s, new JsonSerializerOptions { Encoder = System.Text.Encodings.Web.JavaScriptEncoder.UnsafeRelaxedJsonEscaping });",
            "    }",
            "",
            "    private static Type FindStruct(string name) {",
            "      if (StructCache.TryGetValue(name, out Type found)) {",
            "        return found;",
            "      }",
            "      foreach (Type t in Assembly.GetExecutingAssembly().GetTypes()) {",
            "        if (t.Name == name) {",
            "          StructCache[name] = t;",
            "          return t;",
            "        }",
            "      }",
            "      throw new NewrRt.NewrError(\"json: unknown struct \" + name);",
            "    }",
            "",
            "    private static object BuildStruct(string name, object[] args) {",
            "      Type t = FindStruct(name);",
            "      try {",
            "        return Activator.CreateInstance(t, args);",
            "      } catch (Exception e) {",
            "        throw new NewrRt.NewrError(\"json: cannot build \" + name + \": \" + e.Message);",
            "      }",
            "    }",
            "",
            "    private static Type TypeForShape(object shape) {",
            "      if (shape is string scalar) {",
            "        switch (scalar) {",
            "          case \"int\": return typeof(long);",
            "          case \"float\": return typeof(double);",
            "          case \"str\": return typeof(string);",
            "          case \"bool\": return typeof(bool);",
            "          case \"none\": return typeof(object);",
            "          default: throw new NewrRt.NewrError(\"json: unknown shape \" + scalar);",
            "        }",
            "      }",
            "      object[] parts = (object[]) shape;",
            "      string kind = (string) parts[0];",
            "      if (kind == \"?\") {",
            "        Type inner = TypeForShape(parts[1]);",
            "        if (inner == typeof(long)) {",
            "          return typeof(long?);",
            "        }",
            "        if (inner == typeof(double)) {",
            "          return typeof(double?);",
            "        }",
            "        if (inner == typeof(bool)) {",
            "          return typeof(bool?);",
            "        }",
            "        return inner;",
            "      }",
            "      if (kind == \"list\") {",
            "        return typeof(List<>).MakeGenericType(TypeForShape(parts[1]));",
            "      }",
            "      if (kind == \"dict\") {",
            "        return typeof(Dictionary<,>).MakeGenericType(typeof(string), TypeForShape(parts[1]));",
            "      }",
            "      if (kind == \"struct\") {",
            "        return FindStruct((string) parts[1]);",
            "      }",
            "      throw new NewrRt.NewrError(\"json: unknown shape\");",
            "    }",
            "",
            "    private static void WriteValue(StringBuilder sb, object value, object shape) {",
            "      if (shape is string scalar) {",
            "        switch (scalar) {",
            "          case \"int\": {",
            "            if (value is bool) {",
            "              throw new NewrRt.NewrError(\"json: expected int, got bool\");",
            "            }",
            "            if (value is long l) {",
            "              sb.Append(l.ToString(CultureInfo.InvariantCulture));",
            "              return;",
            "            }",
            "            if (value is int iv) {",
            "              sb.Append(iv.ToString(CultureInfo.InvariantCulture));",
            "              return;",
            "            }",
            "            throw new NewrRt.NewrError(\"json: expected int\");",
            "          }",
            "          case \"float\": {",
            "            if (value is bool) {",
            "              throw new NewrRt.NewrError(\"json: expected float, got bool\");",
            "            }",
            "            if (value is double d) {",
            "              sb.Append(FmtDouble(d));",
            "              return;",
            "            }",
            "            if (value is long lv) {",
            "              sb.Append(FmtDouble((double) lv));",
            "              return;",
            "            }",
            "            if (value is int iv2) {",
            "              sb.Append(FmtDouble((double) iv2));",
            "              return;",
            "            }",
            "            throw new NewrRt.NewrError(\"json: expected float\");",
            "          }",
            "          case \"str\": {",
            "            if (value is string s) {",
            "              sb.Append(Quote(s));",
            "              return;",
            "            }",
            "            throw new NewrRt.NewrError(\"json: expected str\");",
            "          }",
            "          case \"bool\": {",
            "            if (value is bool b) {",
            "              sb.Append(b ? \"true\" : \"false\");",
            "              return;",
            "            }",
            "            throw new NewrRt.NewrError(\"json: expected bool\");",
            "          }",
            "          case \"none\": {",
            "            if (value != null) {",
            "              throw new NewrRt.NewrError(\"json: expected null\");",
            "            }",
            "            sb.Append(\"null\");",
            "            return;",
            "          }",
            "          default:",
            "            throw new NewrRt.NewrError(\"json: unknown shape \" + scalar);",
            "        }",
            "      }",
            "      object[] parts = (object[]) shape;",
            "      string kind = (string) parts[0];",
            "      if (kind == \"?\") {",
            "        if (value == null) {",
            "          sb.Append(\"null\");",
            "          return;",
            "        }",
            "        WriteValue(sb, value, parts[1]);",
            "        return;",
            "      }",
            "      if (kind == \"list\") {",
            "        if (value is string) {",
            "          throw new NewrRt.NewrError(\"json: expected list\");",
            "        }",
            "        if (!(value is System.Collections.IEnumerable items)) {",
            "          throw new NewrRt.NewrError(\"json: expected list\");",
            "        }",
            "        sb.Append('[');",
            "        bool first = true;",
            "        foreach (object item in items) {",
            "          if (!first) {",
            "            sb.Append(',');",
            "          }",
            "          first = false;",
            "          WriteValue(sb, item, parts[1]);",
            "        }",
            "        sb.Append(']');",
            "        return;",
            "      }",
            "      if (kind == \"dict\") {",
            "        if (!(value is System.Collections.IDictionary dict)) {",
            "          throw new NewrRt.NewrError(\"json: expected dict\");",
            "        }",
            "        sb.Append('{');",
            "        bool first = true;",
            "        foreach (System.Collections.DictionaryEntry entry in dict) {",
            "          if (!(entry.Key is string key)) {",
            "            throw new NewrRt.NewrError(\"json: dict keys must be str\");",
            "          }",
            "          if (!first) {",
            "            sb.Append(',');",
            "          }",
            "          first = false;",
            "          sb.Append(Quote(key));",
            "          sb.Append(':');",
            "          WriteValue(sb, entry.Value, parts[1]);",
            "        }",
            "        sb.Append('}');",
            "        return;",
            "      }",
            "      if (kind == \"struct\") {",
            "        sb.Append('{');",
            "        object[] fields = (object[]) parts[2];",
            "        for (int i = 0; i < fields.Length; i++) {",
            "          object[] field = (object[]) fields[i];",
            "          string name = (string) field[0];",
            "          if (i > 0) {",
            "            sb.Append(',');",
            "          }",
            "          sb.Append(Quote(name));",
            "          sb.Append(':');",
            "          WriteValue(sb, FieldValue(value, name), field[1]);",
            "        }",
            "        sb.Append('}');",
            "        return;",
            "      }",
            "      throw new NewrRt.NewrError(\"json: unknown shape\");",
            "    }",
            "",
            "    private static object ReadValue(JsonElement el, object shape) {",
            "      if (shape is string scalar) {",
            "        switch (scalar) {",
            "          case \"int\": {",
            "            if (el.ValueKind != JsonValueKind.Number) {",
            "              throw new NewrRt.NewrError(\"json: expected int\");",
            "            }",
            "            string raw = el.GetRawText();",
            "            if (raw.IndexOf('.') >= 0 || raw.IndexOf('e') >= 0 || raw.IndexOf('E') >= 0) {",
            "              throw new NewrRt.NewrError(\"json: expected int, got a float\");",
            "            }",
            "            if (!el.TryGetInt64(out long v)) {",
            "              throw new NewrRt.NewrError(\"json: integer outside int64 range\");",
            "            }",
            "            return v;",
            "          }",
            "          case \"float\": {",
            "            if (el.ValueKind != JsonValueKind.Number) {",
            "              throw new NewrRt.NewrError(\"json: expected float\");",
            "            }",
            "            string raw = el.GetRawText();",
            "            if (raw.IndexOf('.') >= 0 || raw.IndexOf('e') >= 0 || raw.IndexOf('E') >= 0) {",
            "              return el.GetDouble();",
            "            }",
            "            if (!el.TryGetInt64(out long v)) {",
            "              throw new NewrRt.NewrError(\"json: integer too large for float\");",
            "            }",
            "            return (double) v;",
            "          }",
            "          case \"str\": {",
            "            if (el.ValueKind != JsonValueKind.String) {",
            "              throw new NewrRt.NewrError(\"json: expected str\");",
            "            }",
            "            return el.GetString();",
            "          }",
            "          case \"bool\": {",
            "            if (el.ValueKind == JsonValueKind.True) {",
            "              return true;",
            "            }",
            "            if (el.ValueKind == JsonValueKind.False) {",
            "              return false;",
            "            }",
            "            throw new NewrRt.NewrError(\"json: expected bool\");",
            "          }",
            "          case \"none\": {",
            "            if (el.ValueKind != JsonValueKind.Null) {",
            "              throw new NewrRt.NewrError(\"json: expected null\");",
            "            }",
            "            return null;",
            "          }",
            "          default:",
            "            throw new NewrRt.NewrError(\"json: unknown shape \" + scalar);",
            "        }",
            "      }",
            "      object[] parts = (object[]) shape;",
            "      string kind = (string) parts[0];",
            "      if (kind == \"?\") {",
            "        if (el.ValueKind == JsonValueKind.Null) {",
            "          return null;",
            "        }",
            "        return ReadValue(el, parts[1]);",
            "      }",
            "      if (kind == \"list\") {",
            "        if (el.ValueKind != JsonValueKind.Array) {",
            "          throw new NewrRt.NewrError(\"json: expected list\");",
            "        }",
            "        Type elemType = TypeForShape(parts[1]);",
            "        Type listType = typeof(List<>).MakeGenericType(elemType);",
            "        System.Collections.IList list = (System.Collections.IList) Activator.CreateInstance(listType);",
            "        foreach (JsonElement item in el.EnumerateArray()) {",
            "          list.Add(ReadValue(item, parts[1]));",
            "        }",
            "        return list;",
            "      }",
            "      if (kind == \"dict\") {",
            "        if (el.ValueKind != JsonValueKind.Object) {",
            "          throw new NewrRt.NewrError(\"json: expected dict\");",
            "        }",
            "        Type valueType = TypeForShape(parts[1]);",
            "        Type dictType = typeof(Dictionary<,>).MakeGenericType(typeof(string), valueType);",
            "        System.Collections.IDictionary dict = (System.Collections.IDictionary) Activator.CreateInstance(dictType);",
            "        foreach (JsonProperty prop in el.EnumerateObject()) {",
            "          dict[prop.Name] = ReadValue(prop.Value, parts[1]);",
            "        }",
            "        return dict;",
            "      }",
            "      if (kind == \"struct\") {",
            "        if (el.ValueKind != JsonValueKind.Object) {",
            "          throw new NewrRt.NewrError(\"json: expected object\");",
            "        }",
            "        Dictionary<string, JsonElement> props = new Dictionary<string, JsonElement>();",
            "        foreach (JsonProperty prop in el.EnumerateObject()) {",
            "          props[prop.Name] = prop.Value;",
            "        }",
            "        object[] fields = (object[]) parts[2];",
            "        object[] args = new object[fields.Length];",
            "        for (int i = 0; i < fields.Length; i++) {",
            "          object[] field = (object[]) fields[i];",
            "          string name = (string) field[0];",
            "          if (!props.TryGetValue(name, out JsonElement v)) {",
            "            throw new NewrRt.NewrError(\"json: missing field \" + name);",
            "          }",
            "          args[i] = ReadValue(v, field[1]);",
            "        }",
            "        return BuildStruct((string) parts[1], args);",
            "      }",
            "      throw new NewrRt.NewrError(\"json: unknown shape\");",
            "    }",
            "",
            "    private static object ConvertPlain(object value, object shape) {",
            "      if (shape is string scalar) {",
            "        switch (scalar) {",
            "          case \"int\": {",
            "            if (value is long l) {",
            "              return l;",
            "            }",
            "            if (value is int iv) {",
            "              return (long) iv;",
            "            }",
            "            throw new NewrRt.NewrError(\"json: expected int\");",
            "          }",
            "          case \"float\": {",
            "            if (value is double d) {",
            "              return d;",
            "            }",
            "            if (value is long lv) {",
            "              return (double) lv;",
            "            }",
            "            if (value is int iv2) {",
            "              return (double) iv2;",
            "            }",
            "            throw new NewrRt.NewrError(\"json: expected float\");",
            "          }",
            "          case \"str\": {",
            "            if (value is string s) {",
            "              return s;",
            "            }",
            "            throw new NewrRt.NewrError(\"json: expected str\");",
            "          }",
            "          case \"bool\": {",
            "            if (value is bool b) {",
            "              return b;",
            "            }",
            "            throw new NewrRt.NewrError(\"json: expected bool\");",
            "          }",
            "          case \"none\": {",
            "            if (value == null) {",
            "              return null;",
            "            }",
            "            throw new NewrRt.NewrError(\"json: expected null\");",
            "          }",
            "          default:",
            "            throw new NewrRt.NewrError(\"json: unknown shape \" + scalar);",
            "        }",
            "      }",
            "      object[] parts = (object[]) shape;",
            "      string kind = (string) parts[0];",
            "      if (kind == \"?\") {",
            "        if (value == null) {",
            "          return null;",
            "        }",
            "        return ConvertPlain(value, parts[1]);",
            "      }",
            "      if (kind == \"list\") {",
            "        if (value is string) {",
            "          throw new NewrRt.NewrError(\"json: expected list\");",
            "        }",
            "        if (!(value is System.Collections.IEnumerable items)) {",
            "          throw new NewrRt.NewrError(\"json: expected list\");",
            "        }",
            "        Type elemType = TypeForShape(parts[1]);",
            "        Type listType = typeof(List<>).MakeGenericType(elemType);",
            "        System.Collections.IList list = (System.Collections.IList) Activator.CreateInstance(listType);",
            "        foreach (object item in items) {",
            "          list.Add(ConvertPlain(item, parts[1]));",
            "        }",
            "        return list;",
            "      }",
            "      if (kind == \"dict\") {",
            "        if (!(value is System.Collections.IDictionary source)) {",
            "          throw new NewrRt.NewrError(\"json: expected dict\");",
            "        }",
            "        Type valueType = TypeForShape(parts[1]);",
            "        Type dictType = typeof(Dictionary<,>).MakeGenericType(typeof(string), valueType);",
            "        System.Collections.IDictionary dict = (System.Collections.IDictionary) Activator.CreateInstance(dictType);",
            "        foreach (System.Collections.DictionaryEntry entry in source) {",
            "          dict[(string) entry.Key] = ConvertPlain(entry.Value, parts[1]);",
            "        }",
            "        return dict;",
            "      }",
            "      if (kind == \"struct\") {",
            "        if (!(value is System.Collections.IDictionary fields)) {",
            "          throw new NewrRt.NewrError(\"json: expected object\");",
            "        }",
            "        object[] fieldShapes = (object[]) parts[2];",
            "        object[] args = new object[fieldShapes.Length];",
            "        for (int i = 0; i < fieldShapes.Length; i++) {",
            "          object[] field = (object[]) fieldShapes[i];",
            "          string name = (string) field[0];",
            "          if (!fields.Contains(name)) {",
            "            throw new NewrRt.NewrError(\"json: missing field \" + name);",
            "          }",
            "          args[i] = ConvertPlain(fields[name], field[1]);",
            "        }",
            "        return BuildStruct((string) parts[1], args);",
            "      }",
            "      throw new NewrRt.NewrError(\"json: unknown shape\");",
            "    }",
            "  }",
        ]

    def _emit_http_class(self) -> list[str]:
        """The `NewrHttp` helper class, emitted into a file that uses
        `http.*` (at indent 1, nested in the program class). The
        server is System.Net.HttpListener (one thread per request),
        the client is System.Net.Http.HttpClient, JSON goes through
        NewrJson. Routes are exact-match only; no route, or a method
        mismatch, returns 404 with an empty body. The server reads
        the whole body before calling the handler; a non-UTF-8 body,
        a handler exception, or an out-of-range status surfaces as
        500 with an empty body and the server stays alive. Serve
        prints `listening on <host>:<port>` as its first line (the
        real port when 0: HttpListener rejects port 0 in a prefix,
        so a TcpListener probe allocates the ephemeral port first)
        and blocks forever. Query strings parse into dict[str, str]
        with the last value winning for a repeated key, "+" as a
        space, and %XX as UTF-8 bytes. Fetch validates the method,
        the timeout (whole seconds, default 10, connect plus body),
        the URL (http/https, a host, no userinfo), and the default
        localhost allowlist (localhost, 127.0.0.1) only; the manifest
        allowlist is a static checker concern. POST/request set
        Content-Type: application/json unless the program sets one;
        GET sets none. A 4xx/5xx status is a response struct, not a
        throw; an unreachable host, a timeout, a bad URL, or a
        disallowed host throws NewrError. The fetch connects through
        a SocketsHttpHandler whose ConnectCallback opens an explicit
        IPv4 socket: the handler default (a dual-mode socket) has
        its connect redirected to the VM egress proxy, which cannot
        reach a loopback server, while an IPv4 socket connects
        direct (verified live on this VM)."""
        return [
            "  private static class NewrHttp {",
            "    private static readonly string[] Methods = { \"GET\", \"POST\", \"PUT\", \"PATCH\", \"DELETE\", \"HEAD\", \"OPTIONS\" };",
            "    private static readonly string[] Allowlist = { \"localhost\", \"127.0.0.1\" };",
            "",
            "    private sealed class RouteEntry {",
            "      public string Method;",
            "      public string Path;",
            "      public Delegate Handler;",
            "      public object Shape;",
            "    }",
            "",
            "    private static readonly List<RouteEntry> Routes = new List<RouteEntry>();",
            "",
            "    public static void Route(string method, string path, Delegate handler, object shape) {",
            "      if (method == null || Array.IndexOf(Methods, method) < 0) {",
            "        throw new NewrRt.NewrError(\"http: invalid method \" + method);",
            "      }",
            "      if (path == null || !path.StartsWith(\"/\")) {",
            "        throw new NewrRt.NewrError(\"http: route path must start with '/', got \" + path);",
            "      }",
            "      if (handler == null) {",
            "        throw new NewrRt.NewrError(\"http: route handler must be callable\");",
            "      }",
            "      Routes.Add(new RouteEntry { Method = method, Path = path, Handler = handler, Shape = shape });",
            "    }",
            "",
            "    public static void Serve(long port, string host = \"127.0.0.1\") {",
            "      if (port < 0 || port > 65535) {",
            "        throw new NewrRt.NewrError(\"http: port must be 0 to 65535, got \" + port);",
            "      }",
            "      if (host == null) {",
            "        throw new NewrRt.NewrError(\"http: host must be a str\");",
            "      }",
            "      long realPort = port;",
            "      if (realPort == 0) {",
            "        realPort = FreePort(host);",
            "      }",
            "      string prefixHost = host == \"0.0.0.0\" ? \"+\" : host;",
            "      HttpListener listener = new HttpListener();",
            "      listener.Prefixes.Add(\"http://\" + prefixHost + \":\" + realPort + \"/\");",
            "      listener.Start();",
            "      Console.WriteLine(\"listening on \" + host + \":\" + realPort);",
            "      Console.Out.Flush();",
            "      while (true) {",
            "        HttpListenerContext ctx = listener.GetContext();",
            "        Thread t = new Thread(() => Handle(ctx));",
            "        t.IsBackground = true;",
            "        t.Start();",
            "      }",
            "    }",
            "",
            "    private static long FreePort(string host) {",
            "      IPAddress addr;",
            "      if (host == \"0.0.0.0\") {",
            "        addr = IPAddress.Any;",
            "      } else if (host == \"localhost\") {",
            "        addr = IPAddress.Loopback;",
            "      } else {",
            "        try {",
            "          addr = IPAddress.Parse(host);",
            "        } catch (FormatException) {",
            "          throw new NewrRt.NewrError(\"http: invalid host \" + host);",
            "        }",
            "      }",
            "      TcpListener probe = new TcpListener(addr, 0);",
            "      probe.Start();",
            "      long port = ((IPEndPoint) probe.LocalEndpoint).Port;",
            "      probe.Stop();",
            "      return port;",
            "    }",
            "",
            "    private static void Handle(HttpListenerContext ctx) {",
            "      try {",
            "        string path = ctx.Request.Url.AbsolutePath;",
            "        Dictionary<string, string> query = ParseQuery(ctx.Request.Url.Query);",
            "        byte[] raw;",
            "        using (MemoryStream ms = new MemoryStream()) {",
            "          ctx.Request.InputStream.CopyTo(ms);",
            "          raw = ms.ToArray();",
            "        }",
            "        string body;",
            "        try {",
            "          body = new UTF8Encoding(false, true).GetString(raw);",
            "        } catch (DecoderFallbackException) {",
            "          Send(ctx, 500, null, new byte[0]);",
            "          return;",
            "        }",
            "        Dictionary<string, string> headers = new Dictionary<string, string>();",
            "        foreach (string key in ctx.Request.Headers.AllKeys) {",
            "          headers[key] = ctx.Request.Headers[key];",
            "        }",
            "        RouteEntry found = null;",
            "        foreach (RouteEntry r in Routes) {",
            "          if (r.Method == ctx.Request.HttpMethod && r.Path == path) {",
            "            found = r;",
            "            break;",
            "          }",
            "        }",
            "        if (found == null) {",
            "          Send(ctx, 404, null, new byte[0]);",
            "          return;",
            "        }",
            "        Dictionary<string, object> values = new Dictionary<string, object>();",
            "        values[\"method\"] = ctx.Request.HttpMethod;",
            "        values[\"path\"] = path;",
            "        values[\"headers\"] = headers;",
            "        values[\"body\"] = body;",
            "        values[\"query\"] = query;",
            "        object req = NewrJson.BuildFromShape(found.Shape, values);",
            "        object resp;",
            "        try {",
            "          resp = found.Handler.DynamicInvoke(req);",
            "        } catch (TargetInvocationException e) {",
            "          throw e.InnerException ?? (Exception) e;",
            "        }",
            "        object statusObj = NewrJson.FieldValue(resp, \"status\");",
            "        if (!(statusObj is long status)) {",
            "          throw new NewrRt.NewrError(\"http: response status must be an int\");",
            "        }",
            "        if (status < 100 || status > 599) {",
            "          throw new NewrRt.NewrError(\"http: response status must be 100 to 599, got \" + status);",
            "        }",
            "        object headersObj = NewrJson.FieldValue(resp, \"headers\");",
            "        if (!(headersObj is Dictionary<string, string> respHeaders)) {",
            "          throw new NewrRt.NewrError(\"http: response headers must be a dict\");",
            "        }",
            "        object bodyObj = NewrJson.FieldValue(resp, \"body\");",
            "        if (!(bodyObj is string respBody)) {",
            "          throw new NewrRt.NewrError(\"http: response body must be a str\");",
            "        }",
            "        Send(ctx, (int) status, respHeaders, Encoding.UTF8.GetBytes(respBody));",
            "      } catch (Exception) {",
            "        try {",
            "          Send(ctx, 500, null, new byte[0]);",
            "        } catch (Exception) {",
            "        }",
            "      } finally {",
            "        try {",
            "          ctx.Response.Close();",
            "        } catch (Exception) {",
            "        }",
            "      }",
            "    }",
            "",
            "    private static void Send(HttpListenerContext ctx, int status, Dictionary<string, string> headers, byte[] body) {",
            "      HttpListenerResponse r = ctx.Response;",
            "      r.StatusCode = status;",
            "      if (headers != null) {",
            "        foreach (KeyValuePair<string, string> kv in headers) {",
            "          if (string.Equals(kv.Key, \"Content-Length\", StringComparison.OrdinalIgnoreCase)) {",
            "            continue;",
            "          }",
            "          if (string.Equals(kv.Key, \"Content-Type\", StringComparison.OrdinalIgnoreCase)) {",
            "            r.ContentType = kv.Value;",
            "            continue;",
            "          }",
            "          try {",
            "            r.Headers[kv.Key] = kv.Value;",
            "          } catch (Exception) {",
            "          }",
            "        }",
            "      }",
            "      r.ContentLength64 = body.Length;",
            "      if (ctx.Request.HttpMethod != \"HEAD\" && body.Length > 0) {",
            "        r.OutputStream.Write(body, 0, body.Length);",
            "      }",
            "    }",
            "",
            "    private static Dictionary<string, string> ParseQuery(string rawQuery) {",
            "      Dictionary<string, string> query = new Dictionary<string, string>();",
            "      if (string.IsNullOrEmpty(rawQuery)) {",
            "        return query;",
            "      }",
            "      string q = rawQuery.StartsWith(\"?\") ? rawQuery.Substring(1) : rawQuery;",
            "      foreach (string pair in q.Split('&')) {",
            "        if (pair.Length == 0) {",
            "          continue;",
            "        }",
            "        int eq = pair.IndexOf('=');",
            "        string k = eq < 0 ? pair : pair.Substring(0, eq);",
            "        string v = eq < 0 ? \"\" : pair.Substring(eq + 1);",
            "        query[DecodeQuery(k)] = DecodeQuery(v);",
            "      }",
            "      return query;",
            "    }",
            "",
            "    private static bool IsHex(char c) {",
            "      return (c >= '0' && c <= '9') || (c >= 'a' && c <= 'f') || (c >= 'A' && c <= 'F');",
            "    }",
            "",
            "    private static int HexVal(char c) {",
            "      if (c >= '0' && c <= '9') {",
            "        return c - '0';",
            "      }",
            "      if (c >= 'a' && c <= 'f') {",
            "        return c - 'a' + 10;",
            "      }",
            "      return c - 'A' + 10;",
            "    }",
            "",
            "    private static string DecodeQuery(string s) {",
            "      List<byte> bytes = new List<byte>();",
            "      for (int i = 0; i < s.Length; i++) {",
            "        char c = s[i];",
            "        if (c == '+') {",
            "          bytes.Add((byte) ' ');",
            "        } else if (c == '%' && i + 2 < s.Length && IsHex(s[i + 1]) && IsHex(s[i + 2])) {",
            "          bytes.Add((byte) (HexVal(s[i + 1]) * 16 + HexVal(s[i + 2])));",
            "          i += 2;",
            "        } else {",
            "          foreach (byte b in Encoding.UTF8.GetBytes(new char[] { c })) {",
            "            bytes.Add(b);",
            "          }",
            "        }",
            "      }",
            "      return Encoding.UTF8.GetString(bytes.ToArray());",
            "    }",
            "",
            "    public static object Get(string url, long timeout, Dictionary<string, string> headers, object shape) {",
            "      return Fetch(\"GET\", url, null, timeout, headers, shape);",
            "    }",
            "",
            "    public static object Post(string url, string body, long timeout, Dictionary<string, string> headers, object shape) {",
            "      return Fetch(\"POST\", url, body, timeout, headers, shape);",
            "    }",
            "",
            "    public static object Request(string method, string url, string body, long timeout, Dictionary<string, string> headers, object shape) {",
            "      return Fetch(method, url, body, timeout, headers, shape);",
            "    }",
            "",
            "    private static bool HasHeader(Dictionary<string, string> headers, string name) {",
            "      if (headers == null) {",
            "        return false;",
            "      }",
            "      foreach (string key in headers.Keys) {",
            "        if (string.Equals(key, name, StringComparison.OrdinalIgnoreCase)) {",
            "          return true;",
            "        }",
            "      }",
            "      return false;",
            "    }",
            "",
            "    private static string HeaderValue(Dictionary<string, string> headers, string name) {",
            "      foreach (KeyValuePair<string, string> kv in headers) {",
            "        if (string.Equals(kv.Key, name, StringComparison.OrdinalIgnoreCase)) {",
            "          return kv.Value;",
            "        }",
            "      }",
            "      return null;",
            "    }",
            "",
            "    private static Stream ConnectDirect(SocketsHttpConnectionContext ctx, CancellationToken ct) {",
            "      // The connect for a fetch: an explicit IPv4 socket,",
            "      // connected synchronously. A dual-mode socket (the",
            "      // SocketsHttpHandler default) has its connect",
            "      // redirected to the VM egress proxy, which cannot",
            "      // reach a loopback server; an IPv4 socket connects",
            "      // direct (verified live on this VM). The fetch",
            "      // allowlist is loopback only, so IPv4 is always",
            "      // right here. The proxy is off too: a loopback",
            "      // fetch must never go through a proxy.",
            "      Socket s = new Socket(AddressFamily.InterNetwork, SocketType.Stream, ProtocolType.Tcp);",
            "      try {",
            "        s.Connect(ctx.DnsEndPoint.Host, ctx.DnsEndPoint.Port);",
            "      } catch (Exception) {",
            "        s.Dispose();",
            "        throw;",
            "      }",
            "      return new NetworkStream(s, true);",
            "    }",
            "",
            "    public static object Fetch(string method, string url, string body, long timeout, Dictionary<string, string> headers, object shape) {",
            "      if (method == null || Array.IndexOf(Methods, method) < 0) {",
            "        throw new NewrRt.NewrError(\"http: invalid method \" + method);",
            "      }",
            "      if (timeout <= 0 || timeout > 2147483) {",
            "        throw new NewrRt.NewrError(\"http: timeout must be a positive number of seconds, got \" + timeout);",
            "      }",
            "      if (url == null) {",
            "        throw new NewrRt.NewrError(\"http: URL must be a str\");",
            "      }",
            "      Uri uri;",
            "      try {",
            "        uri = new Uri(url);",
            "      } catch (UriFormatException e) {",
            "        throw new NewrRt.NewrError(\"http: invalid URL \" + url + \": \" + e.Message);",
            "      }",
            "      if (uri.Scheme != \"http\" && uri.Scheme != \"https\") {",
            "        throw new NewrRt.NewrError(\"http: fetch URL must start with http:// or https://, got \" + url);",
            "      }",
            "      if (string.IsNullOrEmpty(uri.Host)) {",
            "        throw new NewrRt.NewrError(\"http: fetch URL has no host: \" + url);",
            "      }",
            "      if (!string.IsNullOrEmpty(uri.UserInfo)) {",
            "        throw new NewrRt.NewrError(\"http: a URL must not carry userinfo; read the token from env and pass it as a header\");",
            "      }",
            "      string host = uri.Host.ToLowerInvariant();",
            "      if (Array.IndexOf(Allowlist, host) < 0) {",
            "        throw new NewrRt.NewrError(\"http: fetch host \" + host + \" is not on the allowlist (localhost, 127.0.0.1)\");",
            "      }",
            "      if (shape == null) {",
            "        throw new NewrRt.NewrError(\"http: internal: fetch without a response shape\");",
            "      }",
            "      byte[] data = null;",
            "      string contentType = null;",
            "      if (body != null) {",
            "        data = Encoding.UTF8.GetBytes(body);",
            "        contentType = HasHeader(headers, \"Content-Type\") ? HeaderValue(headers, \"Content-Type\") : \"application/json\";",
            "      }",
            "      TimeSpan span;",
            "      try {",
            "        span = TimeSpan.FromSeconds((double) timeout);",
            "      } catch (Exception) {",
            "        throw new NewrRt.NewrError(\"http: timeout out of range: \" + timeout);",
            "      }",
            "      using (CancellationTokenSource cts = new CancellationTokenSource(span)) {",
            "        SocketsHttpHandler handler = new SocketsHttpHandler();",
            "        handler.UseProxy = false;",
            "        handler.ConnectCallback = (ctx, ct) => new ValueTask<Stream>(ConnectDirect(ctx, ct));",
            "        using (HttpClient client = new HttpClient(handler)) {",
            "          client.Timeout = span;",
            "          using (HttpRequestMessage req = new HttpRequestMessage(new HttpMethod(method), uri)) {",
            "            if (headers != null) {",
            "              foreach (KeyValuePair<string, string> kv in headers) {",
            "                if (string.Equals(kv.Key, \"Content-Type\", StringComparison.OrdinalIgnoreCase)) {",
            "                  continue;",
            "                }",
            "                req.Headers.TryAddWithoutValidation(kv.Key, kv.Value);",
            "              }",
            "            }",
            "            if (data != null) {",
            "              ByteArrayContent content = new ByteArrayContent(data);",
            "              try {",
            "                content.Headers.ContentType = System.Net.Http.Headers.MediaTypeHeaderValue.Parse(contentType);",
            "              } catch (Exception) {",
            "                throw new NewrRt.NewrError(\"http: invalid Content-Type: \" + contentType);",
            "              }",
            "              req.Content = content;",
            "            } else if (HasHeader(headers, \"Content-Type\")) {",
            "              req.Headers.TryAddWithoutValidation(\"Content-Type\", HeaderValue(headers, \"Content-Type\"));",
            "            }",
            "            HttpResponseMessage resp;",
            "            try {",
            "              resp = client.SendAsync(req, cts.Token).GetAwaiter().GetResult();",
            "            } catch (Exception e) {",
            "              throw new NewrRt.NewrError(\"http: fetch \" + method + \" \" + url + \" failed: \" + e.Message);",
            "            }",
            "            using (resp) {",
            "              int status = (int) resp.StatusCode;",
            "              Dictionary<string, string> respHeaders = new Dictionary<string, string>();",
            "              foreach (var h in resp.Headers) {",
            "                respHeaders[h.Key] = string.Join(\", \", h.Value);",
            "              }",
            "              foreach (var h in resp.Content.Headers) {",
            "                respHeaders[h.Key] = string.Join(\", \", h.Value);",
            "              }",
            "              byte[] raw;",
            "              try {",
            "                raw = resp.Content.ReadAsByteArrayAsync(cts.Token).GetAwaiter().GetResult();",
            "              } catch (Exception e) {",
            "                throw new NewrRt.NewrError(\"http: fetch \" + method + \" \" + url + \" failed: \" + e.Message);",
            "              }",
            "              string text;",
            "              try {",
            "                text = new UTF8Encoding(false, true).GetString(raw);",
            "              } catch (DecoderFallbackException) {",
            "                throw new NewrRt.NewrError(\"http: response body is not valid UTF-8\");",
            "              }",
            "              Dictionary<string, object> values = new Dictionary<string, object>();",
            "              values[\"status\"] = (long) status;",
            "              values[\"headers\"] = respHeaders;",
            "              values[\"body\"] = text;",
            "              return NewrJson.BuildFromShape(shape, values);",
            "            }",
            "          }",
            "        }",
            "      }",
            "    }",
            "  }",
        ]

    def _emit_module_member(self, node: A.Member,
                            binding: _Binding) -> tuple[str, int]:
        """A named-module member used as a value: a constant, or a
        function (which becomes a lambda, D29). `binding.module` is
        the real module name (for dispatch); `binding.java` is the
        C# reference (the alias, when the import is aliased) —
        except for a file module, whose class name is the file stem
        (`_file_module_class`)."""
        real = binding.module or binding.java
        ref = self._file_module_class(binding)
        t = self._type_of(node)
        if t.name == "fn":
            if binding.kind == "file-module":
                # A function in an imported .newr file, used as a
                # value: a method reference to the module's static
                # method, cast to its fn type.
                cast_text = self._cs_fn_type(t)
                return (f"(({cast_text}) {ref}."
                        f"{self._member_name(node.name, ref)})",
                        _PREC["primary"])
            # A module function as a first-class value.
            if real == "file" and node.name in ("read", "write", "append"):
                self._fail(f"`file.{node.name}` cannot be used as a "
                           "value (it needs a statement — the `with` "
                           "block cannot live inside a lambda)")
            params = t.params[:-1]  # the last param is the return type
            names = [f"_a{i}" for i in range(len(params))]
            call, _prec = self._emit_module_call_text(
                real, ref, node.name, names)
            cast_text = self._cs_fn_type(t)
            if names:
                return (f"(({cast_text}) "
                        f"({', '.join(names)}) => {call})",
                        _PREC["primary"])
            return f"(({cast_text}) () => {call})", _PREC["primary"]
        if real == "env" and node.name == "args":
            self._need_rt("envArgs")
            return "NewrRt.EnvArgs()", _PREC["primary"]
        if real == "math" and node.name in ("pi", "e"):
            return ("Math.PI" if node.name == "pi" else "Math.E",
                    _PREC["primary"])
        if binding.kind == "file-module":
            # An imported `pub const` / `pub let` used as a value
            # (M9 L6): a qualified read of the defining module's
            # `public static` field, through `_member_name` (a
            # const named the same as its class is CS0542 too,
            # M9 L2). A non-pub const never reaches here (the
            # checker rejects it with E3005), but the `pub` check
            # keeps the emitter honest if it ever does.
            sub = self.program.modules.get(binding.java)
            if sub is not None:
                decl = next((s for s in sub.stmts
                             if isinstance(s, (A.Let, A.Const))
                             and s.name == node.name and s.pub), None)
                if decl is not None:
                    return (f"{ref}.{self._member_name(node.name, ref)}",
                            _PREC["primary"])
        self._fail(f"module `{real}` has no value member `{node.name}`")

    # ------------------------------------------------------------ store calls

    def _store_key_expr(self, arg) -> str:
        """The key expression for a store call: an entity (user /
        member) contributes its id string (the key column is a
        str); a str is used as-is."""
        t = self._type_of(arg)
        text = self.emit_expr(arg, 0)
        if t.name in ("user", "member"):
            # Discord.Net: the id is the `Id` property (ulong),
            # not JDA's `getId()` method.
            return f"{text}.Id.ToString()"
        if t.name == "str":
            return text
        self._need_rt("toStr")
        return f"NewrRt.ToStr({text})"

    def _emit_store_call(self, node: A.Call,
                         binding: _Binding) -> tuple[str, int]:
        """`economy.balance(user)` etc. -> `bot.<store>_<op>(...)`
        (D17/P11, J9's C# twin). Every mutation takes its
        idempotency `key:`; the checker enforces that it is present.
        Microsoft.Data.Sqlite is synchronous, so there is no
        `await` — but the §19 guard still applies: the `bot`
        instance only exists inside a handler, so a store call at
        the top level, inside a (sync) top-level fn, or inside a
        lambda is a clean E4001 instead of uncompilable code."""
        store_name = binding.java
        method = node.func.name
        ctx = self._fn_ctx[-1] if self._fn_ctx else None
        if ctx is None or not ctx.get("in_handler"):
            self._fail(
                f"a store call (`{store_name}.{method}`) is only "
                "possible inside a command, event, or task handler — "
                "top-level functions and statements run outside the "
                "bot's handler context",
                span=node.span)
        key_arg = {name: value for name, value, _span in node.kwargs}.get("key")
        key = (self.emit_expr(key_arg, 0) if key_arg is not None else None)
        if method == "balance":
            return (f"bot.{store_name}_balance("
                    f"{self._store_key_expr(node.args[0])})",
                    _PREC["primary"])
        if method == "credit":
            return (f"bot.{store_name}_credit("
                    f"{self._store_key_expr(node.args[0])}, "
                    f"{self.emit_expr(node.args[1], 0)}, {key})",
                    _PREC["primary"])
        if method == "debit":
            return (f"bot.{store_name}_debit("
                    f"{self._store_key_expr(node.args[0])}, "
                    f"{self.emit_expr(node.args[1], 0)}, {key})",
                    _PREC["primary"])
        if method == "transfer":
            return (f"bot.{store_name}_transfer("
                    f"{self._store_key_expr(node.args[0])}, "
                    f"{self._store_key_expr(node.args[1])}, "
                    f"{self.emit_expr(node.args[2], 0)}, {key})",
                    _PREC["primary"])
        if method == "get":
            table = node.args[0]
            if not isinstance(table, A.Lit) or not isinstance(table.value, str):
                self._fail("`get`'s table must be a string literal "
                           "naming a declared table")
            # The Bot method returns object (one method serves every
            # table); the call site casts to the table's row class
            # (a synthetic struct emitted by _emit_bot_file, so the
            # name resolves there). A null row casts to null.
            row_binding = self._lookup(table.value)
            row_cs = row_binding.java if row_binding is not None \
                and row_binding.kind == "struct" \
                else self._mangle(table.value)
            return (f"(({row_cs}) bot.{store_name}_get("
                    f"{self.emit_expr(table, 0)}, "
                    f"{self.emit_expr(node.args[1], 0)}))",
                    _PREC["primary"])
        if method == "set":
            table = node.args[0]
            if not isinstance(table, A.Lit) or not isinstance(table.value, str):
                self._fail("`set`'s table must be a string literal "
                           "naming a declared table")
            return (f"bot.{store_name}_set({self.emit_expr(table, 0)}, "
                    f"{self.emit_expr(node.args[1], 0)})",
                    _PREC["primary"])
        if method == "delete":
            table = node.args[0]
            if not isinstance(table, A.Lit) or not isinstance(table.value, str):
                self._fail("`delete`'s table must be a string literal "
                           "naming a declared table")
            return (f"bot.{store_name}_delete({self.emit_expr(table, 0)}, "
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
        (D16, J10's C# twin). Template placeholders that are
        handler bindings (user, guild, ...) are auto-passed in their
        interpolation form; the rest must be explicit arguments (the
        checker enforces it — E2009). The kwargs map is built with
        `dictOf` (Map.of would reject null values)."""
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
            kwargs.append(f"{_cs_str(name)}, {self.emit_expr(value, 0)}")
        for placeholder in entry.placeholders:
            if placeholder in explicit:
                continue
            binding = self._lookup(placeholder)
            if binding is not None and binding.kind == "handler":
                kwargs.append(f"{_cs_str(placeholder)}, "
                              f"{binding.interp_form()}")
                continue
            self._fail(f"`t(\"{node.key}\")` needs an argument for "
                       f"placeholder `{placeholder}` (the checker "
                       "should have rejected it)")
        self._need_rt("dictOf")
        args = ", ".join([_cs_str(node.key), locale,
                          "NewrRt.DictOf<object>("
                          + ", ".join(kwargs) + ")"])
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
        """Build a Discord.Net Embed on the lines before the send
        call (verified live against Discord.Net 3.20.2: EmbedBuilder
        WithTitle / WithDescription / WithColor / AddField, Build()
        -> Embed). Returns the lines and the embed variable's name
        (a fresh temp per embed)."""
        p = "  " * indent
        builder = self._new_temp_name()
        name = self._new_temp_name()
        self._need_discord("Discord.EmbedBuilder")
        self._need_discord("Discord.Embed")
        lines = [f"{p}EmbedBuilder {builder} = new EmbedBuilder();"]
        for field in node.fields:
            if field.kind == "title":
                lines.append(f"{p}{builder}.WithTitle("
                             f"{self.emit_expr(field.value, 0)});")
            elif field.kind == "description":
                lines.append(f"{p}{builder}.WithDescription("
                             f"{self.emit_expr(field.value, 0)});")
            elif field.kind == "color":
                # A newr color is an int (a long in C#); Discord's
                # Color takes a uint.
                lines.append(f"{p}{builder}.WithColor(new Color((uint) "
                             f"{self.emit_expr(field.value, 0)}));")
            elif field.kind == "field":
                inline = "true" if field.inline else "false"
                # A field's name is a plain string (not an expression).
                lines.append(f"{p}{builder}.AddField("
                             f"{_cs_str(field.name)}, "
                             f"{self.emit_expr(field.value, 0)}, "
                             f"{inline});")
        lines.append(f"{p}Embed {name} = {builder}.Build();")
        return lines, name

    # ------------------------------------------------------------ functions

    def _emit_fn(self, decl: A.FnDecl, indent: int = 0) -> list[str]:
        p = "  " * indent
        binding = self._lookup(decl.name)
        cs_name = binding.java if binding is not None \
            else self._mangle(decl.name)
        self._push_scope()
        saved_used = self._fn_used
        self._fn_used = set()
        params = [self._bind_fresh(prm.name, type=prm.type, kind="param")
                  for prm in decl.params]
        sig = ", ".join(f"{self._cs_type(b.type)} {b.java}"
                        for b in params)
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
        ret = decl.return_type
        ret_text = "void" if ret is None or _is_none(ret) \
            else self._cs_type(ret)
        if ensures:
            # `result` in an `ensures` clause reads `_newrResult`.
            self._scopes[-1]["result"] = _Binding(java="_newrResult",
                                                  type=ret,
                                                  kind="local")
            self._fn_used.add("_newrResult")
        self._fn_ctx.append({"return_type": ret,
                             "ensures": ensures, "in_handler": False})
        if requires or ensures:
            # The contract checks below throw NewrError.
            self._need_rt("NewrError")
        lines = [f"{p}public static {ret_text} {cs_name}({sig}) {{"]
        bp = "  " * (indent + 1)
        body_lines: list[str] = []
        if ensures:
            # `_newrResult` is declared ONCE, at the top of the
            # function — every `return` assigns it (a `final` per
            # return would be a redeclaration). A void function
            # still gets one (of type Void) so its `ensures`
            # clauses can read `result`.
            result_type = "object" if ret is None or _is_none(ret) \
                else self._cs_type(ret)
            body_lines.append(f"{bp}{result_type} _newrResult;")
        for req in requires:
            cond = self.emit_expr(req.cond, 0)
            body_lines.append(f"{bp}if (!({cond})) {{")
            body_lines.append(
                f"{bp}  throw new NewrRt.NewrError("
                f"{_cs_str('requires failed: ' + cond)});")
            body_lines.append(f"{bp}}}")
        body_lines.extend(self._emit_block(rest, indent + 1,
                                           new_scope=False))
        if ensures and not (rest and isinstance(rest[-1], A.Return)):
            # The implicit return (falling off the end) is also a
            # return — check the `ensures` against `null`.
            body_lines.append(f"{bp}_newrResult = null;")
            body_lines.extend(self._ensures_check_lines(ensures, indent + 1))
            if ret is None or _is_none(ret):
                body_lines.append(f"{bp}return;")
            else:
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

    def _cs_default(self, t: Type) -> str:
        """The default value for a field of type `t` (a variant
        that does not declare the field)."""
        if _is_int(t):
            return "0L"
        if _is_float(t):
            return "0.0"
        if _is_bool(t):
            return "false"
        return "null"

    def _enum_union_fields(self, decl: A.EnumDecl) -> dict[str, Type]:
        """The union of all variants' fields: name -> type. When
        two variants share a field name with DIFFERENT types, the
        union type is Object (name "object" — the emitter casts on
        access; the field's declared type still wins in the
        variant's factory method and in match bindings)."""
        union: dict[str, Type | None] = {}
        for var in decl.variants:
            for f in var.fields:
                if f.name in union:
                    if union[f.name] is not None and union[f.name] != f.type:
                        union[f.name] = None  # conflicting types
                else:
                    union[f.name] = f.type
        return {name: (t if t is not None else Type("object"))
                for name, t in union.items()}

    def _emit_struct(self, decl: A.StructDecl, indent: int = 0) -> list[str]:
        p = "  " * indent
        binding = self._lookup(decl.name)
        cs_name = binding.java if binding is not None \
            else self._mangle(decl.name)
        self._need_rt("toStr")
        self._need_rt("eq")
        self._need_rt("structHash")
        # A nested class in C# is not tied to an outer instance, so
        # no `static` qualifier (unlike C#'s static nested
        # class); `sealed` because newr structs are not inherited.
        # The class implements INewrRepr so the runtime's ToStr
        # renders it with the newr spelling; ToString returns the
        # same spelling (for Console.WriteLine and string
        # interpolation).
        lines = [f"{p}public sealed class {cs_name} : INewrRepr {{"]
        for f in decl.fields:
            lines.append(f"{p}  public readonly {self._cs_type(f.type)} "
                         f"{self._mangle(f.name)};")
        if decl.fields:
            params = ", ".join(
                f"{self._cs_type(f.type)} {self._mangle(f.name)}"
                for f in decl.fields)
            lines.append(f"{p}  public {cs_name}({params}) {{")
            for f in decl.fields:
                lines.append(f"{p}    this.{self._mangle(f.name)} = "
                             f"{self._mangle(f.name)};")
            lines.append(f"{p}  }}")
        else:
            lines.append(f"{p}  public {cs_name}() {{")
            lines.append(f"{p}  }}")
        lines.append("")
        lines.append(f"{p}  public override bool Equals(object obj) {{")
        lines.append(f"{p}    if (ReferenceEquals(this, obj)) return true;")
        lines.append(f"{p}    if (obj is not {cs_name} other) return false;")
        if decl.fields:
            cond = " && ".join(
                f"NewrRt.Eq(this.{self._mangle(f.name)}, "
                f"other.{self._mangle(f.name)})" for f in decl.fields)
            lines.append(f"{p}    return {cond};")
        else:
            lines.append(f"{p}    return true;")
        lines.append(f"{p}  }}")
        lines.append("")
        lines.append(f"{p}  public override int GetHashCode() {{")
        if decl.fields:
            args = ", ".join(f"this.{self._mangle(f.name)}"
                             for f in decl.fields)
            lines.append(f"{p}    return NewrRt.StructHash({args});")
        else:
            lines.append(f"{p}    return 1;")
        lines.append(f"{p}  }}")
        lines.append("")
        lines.append(f"{p}  public string NewrRepr() {{")
        if decl.fields:
            parts = ", ".join(
                f"{f.name}: \" + NewrRt.ToStr(this.{self._mangle(f.name)}) + \""
                for f in decl.fields)
            lines.append(f"{p}    return \"{cs_name}({parts})\";")
        else:
            lines.append(f"{p}    return \"{cs_name}()\";")
        lines.append(f"{p}  }}")
        lines.append("")
        lines.append(f"{p}  public override string ToString() => NewrRepr();")
        lines.append(f"{p}}}")
        return lines

    def _emit_enum(self, decl: A.EnumDecl, indent: int = 0) -> list[str]:
        p = "  " * indent
        binding = self._lookup(decl.name)
        cs_name = binding.java if binding is not None \
            else self._mangle(decl.name)
        self._need_rt("toStr")
        self._need_rt("eq")
        self._need_rt("structHash")
        union = self._enum_union_fields(decl)
        lines = [f"{p}public sealed class {cs_name} : INewrRepr {{"]
        lines.append(f"{p}  public readonly string _newr_variant;")
        for fname, ftype in union.items():
            lines.append(f"{p}  public readonly {self._cs_type(ftype)} "
                         f"{self._mangle(fname)};")
        # The one constructor takes the discriminant plus every
        # union field (a variant that does not declare a field gets
        # its default).
        ctor_params = ", ".join(
            [f"string variant"] +
            [f"{self._cs_type(ft)} {self._mangle(fn)}"
             for fn, ft in union.items()])
        lines.append(f"{p}  private {cs_name}({ctor_params}) {{")
        lines.append(f"{p}    this._newr_variant = variant;")
        for fname in union:
            lines.append(f"{p}    this.{self._mangle(fname)} = "
                         f"{self._mangle(fname)};")
        lines.append(f"{p}  }}")
        for variant in decl.variants:
            vname = self._mangle(variant.name)
            if not variant.fields:
                defaults = ", ".join(
                    self._cs_default(ft) for ft in union.values())
                args = f"\"{variant.name}\"" + \
                    (f", {defaults}" if defaults else "")
                lines.append(f"{p}  public static readonly {cs_name} "
                             f"{vname} = new {cs_name}({args});")
            else:
                params = ", ".join(
                    f"{self._cs_type(f.type)} {self._mangle(f.name)}"
                    for f in variant.fields)
                lines.append(f"{p}  public static {cs_name} {vname}"
                             f"({params}) {{")
                args = [f"\"{variant.name}\""]
                for fname, ftype in union.items():
                    if fname in {f.name for f in variant.fields}:
                        args.append(self._mangle(fname))
                    else:
                        args.append(self._cs_default(ftype))
                lines.append(f"{p}    return new {cs_name}"
                             f"({', '.join(args)});")
                lines.append(f"{p}  }}")
        lines.append("")
        lines.append(f"{p}  public override bool Equals(object obj) {{")
        lines.append(f"{p}    if (ReferenceEquals(this, obj)) return true;")
        lines.append(f"{p}    if (obj is not {cs_name} other) return false;")
        conds = [f"this._newr_variant.Equals(other._newr_variant)"]
        for fname in union:
            conds.append(
                f"NewrRt.Eq(this.{self._mangle(fname)}, "
                f"other.{self._mangle(fname)})")
        lines.append(f"{p}    return {' && '.join(conds)};")
        lines.append(f"{p}  }}")
        lines.append("")
        lines.append(f"{p}  public override int GetHashCode() {{")
        args = ", ".join(["this._newr_variant"] +
                         [f"this.{self._mangle(fn)}" for fn in union])
        lines.append(f"{p}    return NewrRt.StructHash({args});")
        lines.append(f"{p}  }}")
        lines.append("")
        lines.append(f"{p}  public string NewrRepr() {{")
        for i, variant in enumerate(decl.variants):
            kw = "if" if i == 0 else "} else if"
            lines.append(f"{p}    {kw} (this._newr_variant.Equals("
                         f"{_cs_str(variant.name)})) {{")
            if variant.fields:
                parts = ", ".join(
                    f"{f.name}: \" + NewrRt.ToStr(this.{self._mangle(f.name)}) + \""
                    for f in variant.fields)
                lines.append(f"{p}      return \"{cs_name}.{variant.name}({parts})\";")
            else:
                lines.append(f"{p}      return \"{cs_name}.{variant.name}\";")
        lines.append(f"{p}    }}")
        lines.append(f"{p}    return \"{cs_name}.?\";")
        lines.append(f"{p}  }}")
        lines.append("")
        lines.append(f"{p}  public override string ToString() => NewrRepr();")
        lines.append(f"{p}}}")
        return lines
        lines.append(f"{p}}}")
        return lines

    # ------------------------------------------------------------ file assembly

    def _header(self, main_path: str, description: str | None = None) -> list[str]:
        lines = [
            f"// Generated by newr from {main_path} — do not edit by hand.",
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
        # The java-name -> fn-decl map (for keyword-argument reordering
        # in _emit_kwargs).
        self.fns_by_cs = {}
        for decl in program.functions:
            b = self._lookup(decl.name)
            if b is not None:
                self.fns_by_cs[b.java] = decl
        # The top level's used-name set starts with every module-level
        # C# name, so a `let` in a top-level block cannot reuse one
        # (at module level C# has no block scope for the top level —
        # the name would clobber the module binding).
        self._fn_used = set(self._module_used)

    def _emit_general_file(self, program: A.Program, main_path: str):
        """A general (non-bot) program: header, usings, then the
        public class — static fields for the top-level lets/consts
        (K2: initialized in Main(), in file order, so top-level
        functions can read them and side-effecting initializers run
        in order), nested structs/enums, static functions, then
        Main() with the top-level STATEMENTS in file order (SPEC
        §8). A file module gets a static CONSTRUCTOR instead of
        Main() (its top-level statements run at class-load time —
        C# has no `static { }` block). Definitions are hoisted — a
        function body reads module globals at call time, so
        emitting the `function` before the statements is safe — but
        the statements themselves (lets, assignments, prints, ...)
        must run in the order written. (M2 evaluation #1.)"""
        self.program = program
        self.fns = {f.name: f for f in program.functions}
        self.structs = {s.name: s for s in program.structs}
        self.enums = {e.name: e for e in program.enums}
        self.stores = {s.name: s for s in program.stores}
        self._register_module_scope(program)
        self._fn_ctx = []
        self._hoists = []
        is_main = self._is_main_file
        body: list[str] = []
        # Static fields for the top-level lets/consts (no
        # initializers — Main() / the static constructor assigns
        # them in file order).
        for st in program.stmts:
            if isinstance(st, (A.Let, A.Const)):
                b = self._lookup(st.name)
                t = st.type_ann or self._type_or_none(st.value) or \
                    Type("error")
                body.append(f"  public static {self._cs_type(t)} "
                            f"{b.java if b is not None else self._mangle(st.name)};")
        for decl in program.structs:
            if body:
                body.append("")
            body.extend(self._emit_struct(decl, 1))
        for decl in program.enums:
            if body:
                body.append("")
            body.extend(self._emit_enum(decl, 1))
        for decl in program.functions:
            if body:
                body.append("")
            body.extend(self._emit_fn(decl, 1))
        # Top-level statements are emitted first (into a temp
        # list): a `db.*` call inside them — or inside a function
        # above — sets `_file_uses_db`, and the `NewrDb` helper
        # class is emitted only then, before Main() (or the static
        # constructor for a file module).
        stmt_lines = (self._emit_block(program.stmts, 2, top_level=True)
                      if program.stmts else [])
        if self._file_uses_db:
            if body:
                body.append("")
            body.extend(self._emit_db_class())
        if self._file_uses_http:
            if body:
                body.append("")
            body.extend(self._emit_http_class())
        if self._file_uses_json:
            if body:
                body.append("")
            body.extend(self._emit_json_class())
        if is_main and (self._file_uses_ui or self._closure_uses_ui):
            # The `NewrUi` helper lives once, in the entry file
            # (M9 L2): a file module reads it as
            # `<root>.NewrUi` and never emits its own copy, so
            # every file shares the one Widget type.
            if body:
                body.append("")
            body.extend(self._emit_ui_class())
        # Top-level statements: Main() for the entry file, a static
        # constructor for a file module.
        if program.stmts:
            if body:
                body.append("")
            if is_main:
                if self._file_uses_ui or self._closure_uses_ui:
                    # Avalonia windows want an STA entry point
                    # (the proof's Program.cs carries it too).
                    body.append("  [STAThread]")
                body.append("  public static void Main(string[] args) {")
                body.extend(stmt_lines)
                body.append("  }")
            else:
                body.append(f"  static {self.output_stem}() {{")
                body.extend(stmt_lines)
                body.append("  }")
        lines = self._header(main_path)
        imports = self._render_imports()
        if imports:
            lines.append("")
            lines.extend(imports)
        lines.append("")
        lines.append(f"public class {self.output_stem} {{")
        lines.extend(body)
        lines.append("}")
        text = "\n".join(lines) + "\n"
        return text, bool(self.rt_helpers)

    def _emit_file_set(self, program: A.Program, main_path: str,
                       is_main: bool = True):
        """Emit one program and every file module it imports (recursively).
        Returns (files, uses_rt). `is_main` marks the top file — the
        only role difference left is that the top file is the entry
        point (a bot's `Main()` guard, a general file's top-level
        statements); a file module gets a static constructor."""
        files: dict[str, str] = {}
        self._is_main_file = is_main
        # Walk the imports FIRST (M9 L2, same as the rs emitter):
        # the entry file is emitted after the whole closure is
        # known, so it can emit `NewrUi` once when any file uses
        # ui. Each sub-emitter learns the entry file's class name
        # (`_root_stem`), so its `NewrUi` references qualify
        # through it instead of inlining its own Widget type
        # (CS1503, the C# twin of rs E0308).
        root_stem = self._root_stem or self.output_stem
        uses_rt = False
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
            # shop_items.java header).
            import os
            sub_path = os.path.relpath(sub_path)
            sub_em = _CSEmitter(sub, main_path=sub_path,
                                  sources=self.sources,
                                  line_comments=self.line_comments,
                                  output_stem=Path(sub_path).stem)
            sub_em._root_stem = root_stem
            sub_files, sub_rt = sub_em._emit_file_set(sub, sub_path,
                                                      is_main=False)
            files.update(sub_files)
            uses_rt = uses_rt or sub_rt
            self._closure_uses_ui = (
                self._closure_uses_ui or sub_em._file_uses_ui
                or sub_em._closure_uses_ui)
            # A `db.*` call in a file module needs the Sqlite
            # package in the csproj too (the gate reads only the
            # main emitter — see emit_program_cs).
            self.uses_sqlite = self.uses_sqlite or sub_em.uses_sqlite
            # Same for a `ui.*` call in a file module: the Avalonia
            # packages in the csproj come from the main emitter.
            self.uses_ui = self.uses_ui or sub_em.uses_ui
        text, main_rt = self._emit_one(program, main_path, is_main=is_main)
        files[self.output_stem + ".cs"] = text
        uses_rt = uses_rt or main_rt
        return files, uses_rt

    def _emit_one(self, program: A.Program, main_path: str,
                  is_main: bool = True):
        """Emit a single program's file. Returns (text, uses_rt)."""
        self.program = program
        if program.bot is not None:
            return self._emit_bot_file(program, main_path)
        return self._emit_general_file(program, main_path)


    # ------------------------------------------------------------ bot emitter
    # The Discord.Net bot emitter (M4 bot phase): _emit_bot_file,
    # _emit_deploy_file, _emit_reply/_emit_defer/_emit_edit/
    # _emit_followup/_emit_send, _emit_button/_emit_select/
    # _emit_modal, _attach_components, _push_handler/_handler_ctx.

    # -- the bot file -------------------------------------------------

    # SQL column type per newr column type (J9's C# twin): ints are
    # stored as TEXT (Microsoft.Data.Sqlite returns a long only
    # below 2^53 for REAL; TEXT + long.Parse is exact at int64).
    _SQL_TYPES = {"int": "TEXT", "str": "TEXT", "float": "REAL",
                  "bool": "INTEGER"}

    def _emit_bot_file(self, program: A.Program,
                       main_path: str) -> tuple[str, bool]:
        """A bot program -> one Discord.Net file: header, usings,
        the public class (top-level structs/enums/functions, the
        nested `Bot : DiscordSocketClient`, the `Commands` list, the
        component/modal handler maps, the `_t` helper, the
        dispatcher helpers, the task methods, `Main()`). Command
        registration is a SEPARATE file (`<stem>_deploy.cs`, the
        Discord.Net twin of the JS standalone deploy script) —
        never on ready. `Main()` logs in with DISCORD_TOKEN and
        runs until killed (Task.Delay(-1))."""
        bot = program.bot
        self.program = program
        self.fns = {f.name: f for f in program.functions}
        self.structs = {s.name: s for s in program.structs}
        self.enums = {e.name: e for e in program.enums}
        self.stores = {s.name: s for s in program.stores}
        self._bot_components = []
        self._component_names: set[str] = set()
        self._task_names_by_id: dict[int, str] = {}
        self._task_name_counts: dict[str, int] = {}
        self._used_opt = False
        self._used_modal_value = False
        self._has_components = False
        self._used_component_timeout = False
        self._register_module_scope(program)
        # Row classes for store tables (synthetic structs): a
        # `store.get` returns the table's row type, so the row
        # needs a real class — and a row is constructed like any
        # struct (`balances(user: "u", balance: 5)`). The synthetic
        # decls are registered (not added to program.structs: the
        # caller's program is not mutated).
        row_structs: list[A.StructDecl] = []
        for store in program.stores:
            for table in store.tables:
                if table.name not in self.structs:
                    row_structs.append(A.StructDecl(
                        name=table.name,
                        fields=list(table.columns),
                        span=table.span))
        for rs in row_structs:
            self._bind_module_level(rs.name, kind="struct")
            self.structs[rs.name] = rs
        self._fn_ctx = []
        self._hoists = []
        body: list[str] = []
        # Static fields for the top-level lets/consts (K2, as in a
        # general file: initialized in Main(), in file order).
        for st in program.stmts:
            if isinstance(st, (A.Let, A.Const)):
                b = self._lookup(st.name)
                t = st.type_ann or self._type_or_none(st.value) or \
                    Type("error")
                body.append(f"  public static {self._cs_type(t)} "
                            f"{b.java if b is not None else self._mangle(st.name)};")
        for rs in row_structs:
            if body:
                body.append("")
            body.extend(self._emit_struct(rs, 1))
        for decl in program.structs:
            if body:
                body.append("")
            body.extend(self._emit_struct(decl, 1))
        for decl in program.enums:
            if body:
                body.append("")
            body.extend(self._emit_enum(decl, 1))
        for decl in program.functions:
            if body:
                body.append("")
            body.extend(self._emit_fn(decl, 1))
        if body:
            body.append("")
        body.extend(self._emit_bot_class(program))
        body.append("")
        body.append("  public static Bot bot;")
        if program.commands:
            body.append("")
            body.extend(self._emit_commands_field(program))
        for task in program.tasks:
            body.append("")
            body.extend(self._emit_task(task))
        # Everything below is emitted AFTER the handlers above (the
        # component maps, strings, dispatch helpers and Main are
        # collected while emitting them): the insertion point is
        # recorded here, then filled in once Main is built.
        insert_at = len(body)
        main_lines = self._emit_bot_main(program)
        insert_lines: list[str] = []
        comp_entries = [c for c in self._bot_components
                        if c["kind"] == "component"]
        modal_entries = [c for c in self._bot_components
                         if c["kind"] == "modal"]
        if comp_entries:
            self._need_discord(
                "Discord.WebSocket.SocketMessageComponent")
            insert_lines.append("")
            insert_lines.append("  public static Dictionary<string, Func<SocketMessageComponent, Task>> ComponentHandlers = new Dictionary<string, Func<SocketMessageComponent, Task>> {")
            for c in comp_entries:
                insert_lines.extend(c["lines"])
            insert_lines.append("  };")
            insert_lines.append("  public static Dictionary<ulong, ulong> _componentAuthors = new Dictionary<ulong, ulong>();")
            insert_lines.append("  public static Dictionary<string, (string Kind, List<string> Roles)> _componentGates = new Dictionary<string, (string Kind, List<string> Roles)>();")
        if modal_entries:
            self._need_discord("Discord.WebSocket.SocketModal")
            insert_lines.append("")
            insert_lines.append("  public static Dictionary<string, Func<SocketModal, Task>> ModalHandlers = new Dictionary<string, Func<SocketModal, Task>> {")
            for c in modal_entries:
                insert_lines.extend(c["lines"])
            insert_lines.append("  };")
        strings_lines = self._emit_strings(program)
        if strings_lines:
            insert_lines.append("")
            insert_lines.extend(strings_lines)
        if self._used_channel_text:
            self._need_discord("Discord.IMessageChannel")
            insert_lines.append("")
            insert_lines.extend(self._emit_channel_text())
        if self._used_opt:
            self._need_discord(
                "Discord.WebSocket.SocketSlashCommandDataOption")
            self._need_discord("Discord.WebSocket.SocketSlashCommand")
            insert_lines.append("")
            insert_lines.append("  private static SocketSlashCommandDataOption _Opt(SocketSlashCommand interaction, string name) {")
            insert_lines.append("    foreach (var o in interaction.Data.Options) {")
            insert_lines.append("      if (o.Name == name) {")
            insert_lines.append("        return o;")
            insert_lines.append("      }")
            insert_lines.append("    }")
            insert_lines.append("    return null;")
            insert_lines.append("  }")
        if self._used_modal_value:
            self._need_discord("Discord.WebSocket.SocketModal")
            insert_lines.append("")
            insert_lines.append("  private static string _ModalValue(SocketModal interaction, string customId) {")
            insert_lines.append("    foreach (var component in interaction.Data.Components) {")
            insert_lines.append("      if (component.CustomId == customId) {")
            insert_lines.append("        return component.Value;")
            insert_lines.append("      }")
            insert_lines.append("    }")
            insert_lines.append("    return \"\";")
            insert_lines.append("  }")
        if self._has_components:
            self._need_discord(
                "Discord.WebSocket.SocketMessageComponent")
            self._need_discord("Discord.WebSocket.SocketGuildUser")
            insert_lines.append("")
            insert_lines.extend(self._emit_gate_ok())
            insert_lines.append("")
            insert_lines.extend(self._emit_dispatch_component())
        if self._used_component_timeout:
            self._need_discord("Discord.IUserMessage")
            self._need_discord("Discord.ComponentBuilder")
            self._need_discord("Discord.ActionRowBuilder")
            self._need_discord("Discord.IMessageComponent")
            self._need_discord("Discord.ButtonBuilder")
            self._need_discord("Discord.SelectMenuBuilder")
            insert_lines.append("")
            insert_lines.extend(self._emit_component_timeout())
        if program.commands:
            self._need_discord("Discord.WebSocket.SocketSlashCommand")
            insert_lines.append("")
            insert_lines.extend(self._emit_dispatch_command())
        body[insert_at:insert_at] = insert_lines
        body.append("")
        body.extend(main_lines)
        lines = self._header(main_path, bot.description)
        imports = self._render_imports()
        if imports:
            lines.append("")
            lines.extend(imports)
        lines.append("")
        lines.append(f"public class {self.output_stem} {{")
        lines.extend(body)
        lines.append("}")
        text = "\n".join(lines) + "\n"
        return text, bool(self.rt_helpers)

    def _emit_bot_class(self, program: A.Program) -> list[str]:
        """The nested Bot class: constructor (intents), the
        per-store SQLite connections, Setup (store connects), the
        shared SQLite helpers, and the store methods."""
        lines: list[str] = []
        intents = sorted(self._bot_intents(program))
        intent_bits = []
        for intent in intents:
            bit = self._INTENT_MAP.get(intent)
            if bit is None:
                self._fail(f"unknown intent `{intent}` (no Discord.Net "
                           "GatewayIntents mapping)")
            intent_bits.append(f"GatewayIntents.{bit}")
            self._need_discord("Discord.GatewayIntents")
        self._need_discord("Discord.WebSocket.DiscordSocketClient")
        self._need_discord("Discord.WebSocket.DiscordSocketConfig")
        intents_expr = (" | ".join(intent_bits) if intent_bits
                        else "GatewayIntents.None")
        lines.append("  public class Bot : DiscordSocketClient {")
        lines.append("    public Bot() : base(new DiscordSocketConfig { GatewayIntents = " + intents_expr + " }) {")
        lines.append("    }")
        if program.stores:
            self._need_sqlite()
            for store in program.stores:
                binding = self._lookup(store.name)
                sname = binding.java if binding is not None \
                    else self._mangle(store.name)
                lines.append(f"    public SqliteConnection {sname}_db;")
            lines.append("")
            lines.append("    private static void _Exec(SqliteConnection db, string sql) {")
            lines.append("      using (SqliteCommand cmd = db.CreateCommand()) {")
            lines.append("        cmd.CommandText = sql;")
            lines.append("        cmd.ExecuteNonQuery();")
            lines.append("      }")
            lines.append("    }")
            lines.append("")
            lines.append("    private static void _TryRollback(SqliteConnection db) {")
            lines.append("      try {")
            lines.append("        _Exec(db, \"ROLLBACK\");")
            lines.append("      } catch (Exception) {")
            lines.append("      }")
            lines.append("    }")
            lines.append("")
            lines.append("    private static string _KeyText(object key) {")
            lines.append("      if (key == null) {")
            lines.append("        return null;")
            lines.append("      }")
            lines.append("      return key.ToString();")
            lines.append("    }")
            lines.append("")
            lines.append("    public void Setup() {")
            for store in program.stores:
                binding = self._lookup(store.name)
                sname = binding.java if binding is not None \
                    else self._mangle(store.name)
                lines.append(f"      _{sname}_connect();")
            lines.append("    }")
            for store in program.stores:
                lines.extend(self._emit_store_methods(store))
        else:
            lines.append("")
            lines.append("    public void Setup() {")
            lines.append("    }")
        lines.append("  }")
        return lines

    def _emit_store_methods(self, store: A.StoreDecl) -> list[str]:
        """The store's methods on the Bot class: a connect (schema
        creation), balance / credit / debit / transfer (the economy
        ops, one transaction each, idempotent on `key`), and
        get / set / delete for general tables."""
        binding = self._lookup(store.name)
        name = binding.java if binding is not None \
            else self._mangle(store.name)
        self._need_sqlite()
        lines: list[str] = []
        lines.append("")
        lines.append(f"    private void _{name}_connect() {{")
        lines.append(f"      {name}_db = new SqliteConnection(\"Data Source={name}.db\");")
        lines.append(f"      {name}_db.Open();")
        lines.append(f"      _Exec({name}_db, \"PRAGMA busy_timeout = 5000\");")
        for table in store.tables:
            cols = []
            for i, col in enumerate(table.columns):
                sql = self._SQL_TYPES.get(col.type.name, "TEXT")
                pk = " PRIMARY KEY" if i == 0 else ""
                cols.append(f"{col.name} {sql}{pk}")
            cols_sql = ", ".join(cols)
            lines.append(f"      _Exec({name}_db, \"CREATE TABLE IF NOT EXISTS {table.name} ({cols_sql})\");")
        if store.ledger:
            lines.append(f"      _Exec({name}_db, \"CREATE TABLE IF NOT EXISTS ledger (id INTEGER PRIMARY KEY AUTOINCREMENT, user TEXT NOT NULL, amount TEXT NOT NULL, key TEXT UNIQUE, created_at INTEGER NOT NULL)\");")
        lines.append("    }")
        tables = {t.name: t for t in store.tables}
        if "balances" in tables:
            lines.extend(self._emit_economy_methods(name))
        lines.extend(self._emit_table_methods(name, store))
        return lines

    def _emit_economy_methods(self, name: str) -> list[str]:
        """balance / credit / debit / transfer + the private
        _apply / _ledger_upsert / _ledger_amounts helpers (P11,
        J9's C# twin). Microsoft.Data.Sqlite is synchronous, so
        the methods are plain sync methods (the C# store calls
        emit no `await`); each mutation is one explicit
        BEGIN IMMEDIATE...COMMIT, and the balance + ledger land
        together or not at all. Int columns are stored as TEXT;
        balance arithmetic runs through NewrRt.Add/Sub (checked,
        int64) — never SQL arithmetic on TEXT."""
        n = name
        self._need_rt("add")
        self._need_rt("sub")
        self._need_rt("neg")
        lines: list[str] = []
        lines.append("")
        lines.append(f"    public long {n}_balance(string userId) {{")
        lines.append(f"      using (SqliteCommand cmd = {n}_db.CreateCommand()) {{")
        lines.append("        cmd.CommandText = \"SELECT balance FROM balances WHERE user = $user\";")
        lines.append("        cmd.Parameters.AddWithValue(\"$user\", userId);")
        lines.append("        object result = cmd.ExecuteScalar();")
        lines.append("        if (result == null) {")
        lines.append("          return 0L;")
        lines.append("        }")
        lines.append("        return long.Parse((string) result);")
        lines.append("      }")
        lines.append("    }")
        lines.append("")
        lines.append(f"    private void _{n}_apply(string userId, long amount, string key) {{")
        lines.append("      // Idempotent on key: a duplicate key is a replay,")
        lines.append("      // not an error (P11). One explicit transaction:")
        lines.append("      // the balance + ledger land together or not at all.")
        lines.append("      if (key != null) {")
        lines.append(f"        using (SqliteCommand cmd = {n}_db.CreateCommand()) {{")
        lines.append("          cmd.CommandText = \"SELECT 1 FROM ledger WHERE key = $key\";")
        lines.append("          cmd.Parameters.AddWithValue(\"$key\", key);")
        lines.append("          if (cmd.ExecuteScalar() != null) {")
        lines.append("            return;")
        lines.append("          }")
        lines.append("        }")
        lines.append("      }")
        lines.append(f"      _Exec({n}_db, \"BEGIN IMMEDIATE\");")
        lines.append("      try {")
        lines.append(f"        long cur = {n}_balance(userId);")
        lines.append("        long next = NewrRt.Add(cur, amount);")
        lines.append(f"        using (SqliteCommand cmd = {n}_db.CreateCommand()) {{")
        lines.append("          cmd.CommandText = \"INSERT INTO balances (user, balance) VALUES ($user, $balance) ON CONFLICT(user) DO UPDATE SET balance = excluded.balance\";")
        lines.append("          cmd.Parameters.AddWithValue(\"$user\", userId);")
        lines.append("          cmd.Parameters.AddWithValue(\"$balance\", next.ToString());")
        lines.append("          cmd.ExecuteNonQuery();")
        lines.append("        }")
        lines.append(f"        using (SqliteCommand cmd = {n}_db.CreateCommand()) {{")
        lines.append("          cmd.CommandText = \"INSERT INTO ledger (user, amount, key, created_at) VALUES ($user, $amount, $key, $created)\";")
        lines.append("          cmd.Parameters.AddWithValue(\"$user\", userId);")
        lines.append("          cmd.Parameters.AddWithValue(\"$amount\", amount.ToString());")
        lines.append("          cmd.Parameters.AddWithValue(\"$key\", (object) key ?? DBNull.Value);")
        lines.append("          cmd.Parameters.AddWithValue(\"$created\", DateTimeOffset.UtcNow.ToUnixTimeSeconds());")
        lines.append("          cmd.ExecuteNonQuery();")
        lines.append("        }")
        lines.append(f"        _Exec({n}_db, \"COMMIT\");")
        lines.append("      } catch (Exception) {")
        lines.append(f"        _TryRollback({n}_db);")
        lines.append("        throw;")
        lines.append("      }")
        lines.append("    }")
        lines.append("")
        lines.append(f"    public void {n}_credit(string userId, long amount, string key) {{")
        lines.append(f"      _{n}_apply(userId, amount, key);")
        lines.append("    }")
        lines.append("")
        lines.append(f"    public bool {n}_debit(string userId, long amount, string key) {{")
        lines.append("      // The balance check is inside the transaction (no")
        lines.append("      // TOCTOU): a concurrent debit cannot slip between")
        lines.append("      // the check and the write. A replayed key returns")
        lines.append("      // true — the debit already happened.")
        lines.append("      if (key != null) {")
        lines.append(f"        using (SqliteCommand cmd = {n}_db.CreateCommand()) {{")
        lines.append("          cmd.CommandText = \"SELECT 1 FROM ledger WHERE key = $key\";")
        lines.append("          cmd.Parameters.AddWithValue(\"$key\", key);")
        lines.append("          if (cmd.ExecuteScalar() != null) {")
        lines.append("            return true;")
        lines.append("          }")
        lines.append("        }")
        lines.append("      }")
        lines.append(f"      _Exec({n}_db, \"BEGIN IMMEDIATE\");")
        lines.append("      try {")
        lines.append(f"        long cur = {n}_balance(userId);")
        lines.append("        if (cur < amount) {")
        lines.append(f"          _TryRollback({n}_db);")
        lines.append("          return false;")
        lines.append("        }")
        lines.append("        long next = NewrRt.Sub(cur, amount);")
        lines.append(f"        using (SqliteCommand cmd = {n}_db.CreateCommand()) {{")
        lines.append("          cmd.CommandText = \"INSERT INTO balances (user, balance) VALUES ($user, $balance) ON CONFLICT(user) DO UPDATE SET balance = excluded.balance\";")
        lines.append("          cmd.Parameters.AddWithValue(\"$user\", userId);")
        lines.append("          cmd.Parameters.AddWithValue(\"$balance\", next.ToString());")
        lines.append("          cmd.ExecuteNonQuery();")
        lines.append("        }")
        lines.append(f"        using (SqliteCommand cmd = {n}_db.CreateCommand()) {{")
        lines.append("          cmd.CommandText = \"INSERT INTO ledger (user, amount, key, created_at) VALUES ($user, $amount, $key, $created)\";")
        lines.append("          cmd.Parameters.AddWithValue(\"$user\", userId);")
        lines.append("          cmd.Parameters.AddWithValue(\"$amount\", NewrRt.Neg(amount).ToString());")
        lines.append("          cmd.Parameters.AddWithValue(\"$key\", (object) key ?? DBNull.Value);")
        lines.append("          cmd.Parameters.AddWithValue(\"$created\", DateTimeOffset.UtcNow.ToUnixTimeSeconds());")
        lines.append("          cmd.ExecuteNonQuery();")
        lines.append("        }")
        lines.append(f"        _Exec({n}_db, \"COMMIT\");")
        lines.append("      } catch (Exception) {")
        lines.append(f"        _TryRollback({n}_db);")
        lines.append("        throw;")
        lines.append("      }")
        lines.append("      return true;")
        lines.append("    }")
        lines.append("")
        lines.append(f"    public bool {n}_transfer(string fromId, string toId, long amount, string key) {{")
        lines.append("      // Both legs + the ledger in ONE transaction. A retry")
        lines.append("      // heals a partial transfer: each leg is topped up")
        lines.append("      // to its intended total (the out-leg debits `amount`,")
        lines.append("      // the in-leg credits `amount`), so no money is")
        lines.append("      // created or destroyed however the first attempt")
        lines.append("      // died. The leg amounts are read from the")
        lines.append("      // ledger (not just presence) and summed in C#")
        lines.append("      // with checked long arithmetic — never SQL SUM,")
        lines.append("      // which would lose precision on TEXT amounts (J9).")
        lines.append("      long outDone = 0L;")
        lines.append("      long inDone = 0L;")
        lines.append("      if (key != null) {")
        lines.append(f"        long outSum = 0L;")
        lines.append(f"        foreach (string a in _{n}_ledger_amounts(key + \":out\")) {{")
        lines.append("          outSum = NewrRt.Add(outSum, long.Parse(a));")
        lines.append("        }")
        lines.append(f"        long inSum = 0L;")
        lines.append(f"        foreach (string a in _{n}_ledger_amounts(key + \":in\")) {{")
        lines.append("          inSum = NewrRt.Add(inSum, long.Parse(a));")
        lines.append("        }")
        lines.append("        outDone = NewrRt.Sub(0L, outSum);")
        lines.append("        inDone = inSum;")
        lines.append("        if (outDone < 0L) { outDone = 0L; }")
        lines.append("        if (outDone > amount) { outDone = amount; }")
        lines.append("        if (inDone < 0L) { inDone = 0L; }")
        lines.append("        if (inDone > amount) { inDone = amount; }")
        lines.append("        if (outDone >= amount && inDone >= amount) {")
        lines.append("          return true;")
        lines.append("        }")
        lines.append("      }")
        lines.append("      long outLeft = NewrRt.Sub(amount, outDone);")
        lines.append("      long inLeft = NewrRt.Sub(amount, inDone);")
        lines.append(f"      _Exec({n}_db, \"BEGIN IMMEDIATE\");")
        lines.append("      try {")
        lines.append("        if (outLeft > 0L) {")
        lines.append(f"          long cur = {n}_balance(fromId);")
        lines.append("          if (cur < outLeft) {")
        lines.append(f"            _TryRollback({n}_db);")
        lines.append("            return false;")
        lines.append("          }")
        lines.append("          long next = NewrRt.Sub(cur, outLeft);")
        lines.append(f"          using (SqliteCommand cmd = {n}_db.CreateCommand()) {{")
        lines.append("            cmd.CommandText = \"INSERT INTO balances (user, balance) VALUES ($user, $balance) ON CONFLICT(user) DO UPDATE SET balance = excluded.balance\";")
        lines.append("            cmd.Parameters.AddWithValue(\"$user\", fromId);")
        lines.append("            cmd.Parameters.AddWithValue(\"$balance\", next.ToString());")
        lines.append("            cmd.ExecuteNonQuery();")
        lines.append("          }")
        lines.append(f"          _{n}_ledger_upsert(fromId, NewrRt.Neg(outLeft), key == null ? null : key + \":out\");")
        lines.append("        }")
        lines.append("        if (inLeft > 0L) {")
        lines.append(f"          long cur = {n}_balance(toId);")
        lines.append("          long next = NewrRt.Add(cur, inLeft);")
        lines.append(f"          using (SqliteCommand cmd = {n}_db.CreateCommand()) {{")
        lines.append("            cmd.CommandText = \"INSERT INTO balances (user, balance) VALUES ($user, $balance) ON CONFLICT(user) DO UPDATE SET balance = excluded.balance\";")
        lines.append("            cmd.Parameters.AddWithValue(\"$user\", toId);")
        lines.append("            cmd.Parameters.AddWithValue(\"$balance\", next.ToString());")
        lines.append("            cmd.ExecuteNonQuery();")
        lines.append("          }")
        lines.append(f"          _{n}_ledger_upsert(toId, inLeft, key == null ? null : key + \":in\");")
        lines.append("        }")
        lines.append(f"        _Exec({n}_db, \"COMMIT\");")
        lines.append("      } catch (Exception) {")
        lines.append(f"        _TryRollback({n}_db);")
        lines.append("        throw;")
        lines.append("      }")
        lines.append("      return true;")
        lines.append("    }")
        lines.append("")
        lines.append(f"    private List<string> _{n}_ledger_amounts(string key) {{")
        lines.append("      List<string> amounts = new List<string>();")
        lines.append(f"      using (SqliteCommand cmd = {n}_db.CreateCommand()) {{")
        lines.append("        cmd.CommandText = \"SELECT amount FROM ledger WHERE key = $key\";")
        lines.append("        cmd.Parameters.AddWithValue(\"$key\", key);")
        lines.append("        using (SqliteDataReader reader = cmd.ExecuteReader()) {")
        lines.append("          while (reader.Read()) {")
        lines.append("            amounts.Add(reader.GetString(0));")
        lines.append("          }")
        lines.append("        }")
        lines.append("      }")
        lines.append("      return amounts;")
        lines.append("    }")
        lines.append("")
        lines.append(f"    private void _{n}_ledger_upsert(string userId, long amount, string key) {{")
        lines.append("      // A ledger leg, upserted with C#-side arithmetic")
        lines.append("      // (exact long; see _transfer). Called only inside")
        lines.append("      // a transaction held by the caller.")
        lines.append("      if (key == null) {")
        lines.append(f"        using (SqliteCommand cmd = {n}_db.CreateCommand()) {{")
        lines.append("          cmd.CommandText = \"INSERT INTO ledger (user, amount, key, created_at) VALUES ($user, $amount, $key, $created)\";")
        lines.append("          cmd.Parameters.AddWithValue(\"$user\", userId);")
        lines.append("          cmd.Parameters.AddWithValue(\"$amount\", amount.ToString());")
        lines.append("          cmd.Parameters.AddWithValue(\"$key\", DBNull.Value);")
        lines.append("          cmd.Parameters.AddWithValue(\"$created\", DateTimeOffset.UtcNow.ToUnixTimeSeconds());")
        lines.append("          cmd.ExecuteNonQuery();")
        lines.append("        }")
        lines.append("        return;")
        lines.append("      }")
        lines.append("      string existing = null;")
        lines.append(f"      using (SqliteCommand cmd = {n}_db.CreateCommand()) {{")
        lines.append("        cmd.CommandText = \"SELECT amount FROM ledger WHERE key = $key\";")
        lines.append("        cmd.Parameters.AddWithValue(\"$key\", key);")
        lines.append("        object result = cmd.ExecuteScalar();")
        lines.append("        if (result != null) {")
        lines.append("          existing = (string) result;")
        lines.append("        }")
        lines.append("      }")
        lines.append("      if (existing != null) {")
        lines.append("        long total = NewrRt.Add(long.Parse(existing), amount);")
        lines.append(f"        using (SqliteCommand cmd = {n}_db.CreateCommand()) {{")
        lines.append("          cmd.CommandText = \"UPDATE ledger SET amount = $amount WHERE key = $key\";")
        lines.append("          cmd.Parameters.AddWithValue(\"$amount\", total.ToString());")
        lines.append("          cmd.Parameters.AddWithValue(\"$key\", key);")
        lines.append("          cmd.ExecuteNonQuery();")
        lines.append("        }")
        lines.append("      } else {")
        lines.append(f"        using (SqliteCommand cmd = {n}_db.CreateCommand()) {{")
        lines.append("          cmd.CommandText = \"INSERT INTO ledger (user, amount, key, created_at) VALUES ($user, $amount, $key, $created)\";")
        lines.append("          cmd.Parameters.AddWithValue(\"$user\", userId);")
        lines.append("          cmd.Parameters.AddWithValue(\"$amount\", amount.ToString());")
        lines.append("          cmd.Parameters.AddWithValue(\"$key\", key);")
        lines.append("          cmd.Parameters.AddWithValue(\"$created\", DateTimeOffset.UtcNow.ToUnixTimeSeconds());")
        lines.append("          cmd.ExecuteNonQuery();")
        lines.append("        }")
        lines.append("      }")
        lines.append("    }")
        return lines

    def _emit_table_methods(self, name: str,
                            store: A.StoreDecl) -> list[str]:
        """`get` / `set` / `delete` for one store (one method each,
        dispatching on the table name — the table is a checked
        string literal). A row is the table's row class (a synthetic
        struct emitted by _emit_bot_file); `get` returns it boxed as
        object (the call site casts), or null when there is no row.
        Int columns are stored as TEXT and parsed on read, bool
        columns are 0/1."""
        n = name
        lines: list[str] = []
        lines.append("")
        lines.append(f"    public object {n}_get(string table, object key) {{")
        for table in store.tables:
            row_binding = self._lookup(table.name)
            row_cs = row_binding.java if row_binding is not None \
                and row_binding.kind == "struct" \
                else self._mangle(table.name)
            key_col = table.columns[0].name if table.columns \
                else "id"
            reads = []
            for i, col in enumerate(table.columns):
                if col.type.name == "int":
                    reads.append(f"long.Parse(reader.GetString({i}))")
                elif col.type.name == "float":
                    reads.append(f"reader.GetDouble({i})")
                elif col.type.name == "bool":
                    reads.append(f"reader.GetInt64({i}) != 0L")
                else:
                    reads.append(f"reader.GetString({i})")
            lines.append(f"      if (table == {_cs_str(table.name)}) {{")
            lines.append(f"        using (SqliteCommand cmd = {n}_db.CreateCommand()) {{")
            lines.append(f"          cmd.CommandText = \"SELECT * FROM {table.name} WHERE {key_col} = $key\";")
            lines.append("          cmd.Parameters.AddWithValue(\"$key\", _KeyText(key));")
            lines.append("          using (SqliteDataReader reader = cmd.ExecuteReader()) {")
            lines.append("            if (!reader.Read()) {")
            lines.append("              return null;")
            lines.append("            }")
            lines.append(f"            return new {row_cs}({', '.join(reads)});")
            lines.append("          }")
            lines.append("        }")
            lines.append("      }")
        lines.append("      return null;")
        lines.append("    }")
        lines.append("")
        lines.append(f"    public void {n}_set(string table, object row) {{")
        for table in store.tables:
            row_binding = self._lookup(table.name)
            row_cs = row_binding.java if row_binding is not None \
                and row_binding.kind == "struct" \
                else self._mangle(table.name)
            key_col = table.columns[0].name if table.columns \
                else "id"
            cols = [c.name for c in table.columns]
            updates = ", ".join(f"{c} = excluded.{c}" for c in cols)
            lines.append(f"      if (table == {_cs_str(table.name)}) {{")
            lines.append(f"        {row_cs} r = ({row_cs}) row;")
            lines.append(f"        using (SqliteCommand cmd = {n}_db.CreateCommand()) {{")
            lines.append(f"          cmd.CommandText = \"INSERT INTO {table.name} ({', '.join(cols)}) VALUES ({', '.join('$' + c for c in cols)}) ON CONFLICT({key_col}) DO UPDATE SET {updates}\";")
            for col in table.columns:
                if col.type.name == "int":
                    value = f"r.{self._mangle(col.name)}.ToString()"
                elif col.type.name == "bool":
                    value = f"(r.{self._mangle(col.name)} ? 1L : 0L)"
                else:
                    value = f"r.{self._mangle(col.name)}"
                lines.append(f"          cmd.Parameters.AddWithValue(\"${col.name}\", {value});")
            lines.append("          cmd.ExecuteNonQuery();")
            lines.append("        }")
            lines.append("        return;")
            lines.append("      }")
        lines.append("    }")
        lines.append("")
        lines.append(f"    public void {n}_delete(string table, object key) {{")
        lines.append("      string keyCol = null;")
        for table in store.tables:
            key_col = table.columns[0].name if table.columns \
                else "id"
            lines.append(f"      if (table == {_cs_str(table.name)}) {{")
            lines.append(f"        keyCol = {_cs_str(key_col)};")
            lines.append("      }")
        lines.append("      if (keyCol == null) {")
        lines.append("        return;")
        lines.append("      }")
        lines.append(f"      using (SqliteCommand cmd = {n}_db.CreateCommand()) {{")
        lines.append("        cmd.CommandText = \"DELETE FROM \" + table + \" WHERE \" + keyCol + \" = $key\";")
        lines.append("        cmd.Parameters.AddWithValue(\"$key\", _KeyText(key));")
        lines.append("        cmd.ExecuteNonQuery();")
        lines.append("      }")
        lines.append("    }")
        return lines

    # -- commands ---------------------------------------------------

    def _emit_commands_field(self, program: A.Program) -> list[str]:
        """The `Commands` list: one (SlashCommandBuilder, async
        handler lambda) tuple per command. The deploy file reads
        the builders from here; the dispatcher reads the handlers."""
        self._need_discord("Discord.SlashCommandBuilder")
        lines = ["  public static List<(SlashCommandBuilder Data, Func<SocketSlashCommand, Task> Execute)> Commands = new List<(SlashCommandBuilder Data, Func<SocketSlashCommand, Task> Execute)> {"]
        for cmd in program.commands:
            lines.extend(self._emit_command(cmd, 2))
        lines.append("  };")
        return lines

    def _emit_command(self, cmd: A.CommandDecl, indent: int) -> list[str]:
        """One command -> one tuple entry: a SlashCommandBuilder +
        an async execute lambda."""
        p = "  " * indent
        self._need_discord("Discord.WebSocket.SocketSlashCommand")
        desc = cmd.description if cmd.description is not None else cmd.name
        builder = (f"new SlashCommandBuilder().WithName({_cs_str(cmd.name)})"
                   f".WithDescription({_cs_str(desc)})")
        for opt in cmd.options:
            builder += self._option_builder(opt)
        lines = [f"{p}({builder}, async (SocketSlashCommand interaction) => {{"]
        self._push_handler("command",
                           ["user", "guild", "channel", "interaction"],
                           reply="interaction",
                           locale="interaction.UserLocale")
        try:
            lines.extend(self._emit_option_bindings(cmd, indent + 1))
            lines.extend(self._emit_block(cmd.effective_body(), indent + 1,
                                          new_scope=False))
        finally:
            self._pop_handler()
        lines.append(f"{p}}}),")
        return lines

    def _option_builder(self, opt: A.OptionDecl) -> str:
        """One SlashCommandBuilder option call: the AddOption
        overload follows the option type; min/max and choices need
        a SlashCommandOptionBuilder (WithMinValue / WithMaxValue /
        AddChoice — all verified live against Discord.Net 3.20.2)."""
        t = opt.type
        name = opt.name
        desc = opt.description if opt.description is not None else name
        tname = t.name if t is not None else "str"
        type_map = {
            "str": "String", "int": "Integer", "num": "Number",
            "float": "Number", "bool": "Boolean", "user": "User",
            "member": "User", "channel": "Channel", "role": "Role",
        }
        cs_type = type_map.get(tname)
        if cs_type is None:
            self._fail(f"option type `{tname}` is not a command "
                       "option type")
        self._need_discord("Discord.ApplicationCommandOptionType")
        if opt.min_value is None and opt.max_value is None \
                and not opt.choices:
            parts = [f".AddOption({_cs_str(name)}, "
                     f"ApplicationCommandOptionType.{cs_type}, "
                     f"{_cs_str(desc)}"]
            if opt.required:
                parts.append(", isRequired: true")
            parts.append(")")
            return "".join(parts)
        self._need_discord("Discord.SlashCommandOptionBuilder")
        parts = [f".AddOption(new SlashCommandOptionBuilder()"
                 f".WithName({_cs_str(name)})"
                 f".WithDescription({_cs_str(desc)})"
                 f".WithType(ApplicationCommandOptionType.{cs_type})"]
        if opt.required:
            parts.append(".WithRequired(true)")
        if opt.min_value is not None:
            parts.append(
                f".WithMinValue({self._emit_option_bound(opt.min_value)})")
        if opt.max_value is not None:
            parts.append(
                f".WithMaxValue({self._emit_option_bound(opt.max_value)})")
        if opt.choices:
            for c in opt.choices:
                parts.append(
                    f".AddChoice({self._emit_choice_text(c)}, "
                    f"{self._emit_choice_text(c)})")
        parts.append(")")
        return "".join(parts)

    def _emit_option_bound(self, expr) -> str:
        """An option's min/max bound for WithMinValue/WithMaxValue:
        an int literal renders as its digits (the overload takes a
        long); anything else is already a long/double expression."""
        if isinstance(expr, A.Lit) and isinstance(expr.value, int):
            return str(expr.value)
        return self.emit_expr(expr, 0)

    def _emit_choice_text(self, expr) -> str:
        return self.emit_expr(expr, 0)

    def _option_type_name(self, opt: A.OptionDecl) -> str:
        t = opt.type
        if t is None:
            return "str"
        return t.name

    def _emit_option_bindings(self, cmd: A.CommandDecl,
                              indent: int) -> list[str]:
        """Bind each command option at the top of `execute`: a
        local reading it from `interaction.Data.Options` via the
        `_Opt` helper. Int options stay long (newr ints are int64);
        optional options without a default stay nullable."""
        p = "  " * indent
        self._used_opt = True
        lines: list[str] = []
        for opt in cmd.options:
            name = opt.name
            tname = self._option_type_name(opt)
            cs_tname = "float" if tname == "num" else tname
            binding_type = Type(cs_tname,
                                nullable=not opt.required
                                and opt.default is None)
            mangled = self._mangle(name)

            def convert(temp: str, _tname=tname) -> str:
                if _tname == "int":
                    return f"(long) {temp}.Value"
                if _tname in ("num", "float"):
                    return f"(double) {temp}.Value"
                if _tname == "bool":
                    return f"(bool) {temp}.Value"
                if _tname == "user":
                    self._need_discord("Discord.IUser")
                    return f"(IUser) {temp}.Value"
                if _tname == "member":
                    self._need_discord(
                        "Discord.WebSocket.SocketGuildChannel")
                    self._need_discord("Discord.IUser")
                    return (f"((interaction.Channel as "
                            f"SocketGuildChannel)?.Guild.GetUser("
                            f"((IUser) {temp}.Value).Id))")
                if _tname == "channel":
                    self._need_discord("Discord.IMessageChannel")
                    return f"(IMessageChannel) {temp}.Value"
                if _tname == "role":
                    self._need_discord("Discord.IRole")
                    return f"(IRole) {temp}.Value"
                return f"(string) {temp}.Value"

            if tname in ("user", "member"):
                interp = f"{mangled}.Mention"
            elif tname == "channel":
                interp = f"_ChannelText({mangled})"
                self._used_channel_text = True
            else:
                interp = None
            temp = self._new_temp_name()
            lines.append(f"{p}SocketSlashCommandDataOption {temp} = _Opt(interaction, {_cs_str(name)});")
            cs_type = self._cs_type(binding_type)
            if opt.required:
                lines.append(f"{p}{cs_type} {mangled} = "
                             f"{convert(temp)};")
            elif opt.default is not None:
                default = self.emit_expr(opt.default, 0)
                lines.append(f"{p}{cs_type} {mangled} = {temp} == null "
                             f"? {default} : {convert(temp)};")
            else:
                lines.append(f"{p}{cs_type} {mangled} = {temp} == null "
                             f"? null : {convert(temp)};")
            self._handler_binding(name, mangled, interp, binding_type)
        return lines

    # -- events / tasks / Main ------------------------------------------

    def _emit_event_registration(self, ev: A.EventDecl,
                                 indent: int) -> list[str]:
        """An event -> a `bot.<Event> += ...` registration inside
        Main()."""
        p = "  " * indent
        body = ev.effective_body()
        if ev.kind == "ready":
            self._push_handler("ready", ["bot"],
                               reply=None, locale=None)
            try:
                body_lines = self._emit_block(body, indent + 1,
                                              new_scope=False)
            finally:
                self._pop_handler()
            # A handler with no await is emitted as a plain
            # (non-async) lambda returning Task.CompletedTask —
            # an `async` lambda with no await is a CS1998 warning
            # in every generated bot.
            if any("await " in ln for ln in body_lines):
                lines = [f"{p}bot.Ready += async () => {{"]
            else:
                lines = [f"{p}bot.Ready += () => {{"]
                body_lines.append(f"{p}  return Task.CompletedTask;")
            lines.extend(body_lines)
            lines.append(f"{p}}};")
            return lines
        if ev.kind in ("member_join", "member_remove"):
            event = ("UserJoined" if ev.kind == "member_join"
                     else "UserLeft")
            self._need_discord("Discord.WebSocket.SocketGuildUser")
            self._need_discord("Discord.IMessageChannel")
            self._push_handler(ev.kind,
                               ["user", "guild"],
                               reply="system_channel",
                               locale="member.Guild.PreferredLocale")
            try:
                # A reply in a member event goes to the guild's
                # system channel (the v0 golden), guarded against
                # null.
                body_lines = self._emit_block(body, indent + 2,
                                              new_scope=False)
            finally:
                self._pop_handler()
            if any("await " in ln for ln in body_lines):
                lines = [f"{p}bot.{event} += async (SocketGuildUser member) => {{"]
            else:
                lines = [f"{p}bot.{event} += (SocketGuildUser member) => {{"]
            lines.append(f"{p}  IMessageChannel channel = member.Guild.SystemChannel;")
            lines.append(f"{p}  if (channel != null) {{")
            lines.extend(body_lines)
            lines.append(f"{p}  }}")
            if not any("await " in ln for ln in body_lines):
                lines.append(f"{p}  return Task.CompletedTask;")
            lines.append(f"{p}}};")
            return lines
        if ev.kind == "message":
            self._need_discord("Discord.WebSocket.SocketMessage")
            self._push_handler("message",
                               ["message", "user"],
                               reply="message_channel",
                               locale=None)
            try:
                body_lines = self._emit_block(body, indent + 1,
                                              new_scope=False)
            finally:
                self._pop_handler()
            # A sync lambda cannot `return;` (it must return the
            # Task), so the self-message guard returns
            # Task.CompletedTask in the sync form.
            if any("await " in ln for ln in body_lines):
                lines = [f"{p}bot.MessageReceived += async (SocketMessage message) => {{"]
                guard_return = "return;"
            else:
                lines = [f"{p}bot.MessageReceived += (SocketMessage message) => {{"]
                guard_return = "return Task.CompletedTask;"
            lines.append(f"{p}  if (message.Author.Id == bot.CurrentUser.Id) {{")
            lines.append(f"{p}    {guard_return}")
            lines.append(f"{p}  }}")
            lines.extend(body_lines)
            if "Task.CompletedTask" in guard_return:
                lines.append(f"{p}  return Task.CompletedTask;")
            lines.append(f"{p}}};")
            return lines
        self._fail(f"unknown event kind `{ev.kind}`")

    def _emit_task(self, task: A.TaskDecl) -> list[str]:
        """A task -> a named private static async method, started
        from Main() with a Task.Run loop (a Task.Delay per interval,
        guarded against overlap by the loop itself — Discord.Net
        has no tasks.loop, so this is the plain-Timer twin of the
        JS setInterval)."""
        name = self._task_fn_name(task)
        lines: list[str] = []
        self._push_handler("task", [], reply=None, locale=None)
        try:
            body_lines = self._emit_block(task.body, 2,
                                          new_scope=False)
        finally:
            self._pop_handler()
        # As for events: a task with no await is a plain Task
        # method returning Task.CompletedTask (no CS1998).
        if any("await " in ln for ln in body_lines):
            lines.append(f"  private static async Task {name}() {{")
        else:
            lines.append(f"  private static Task {name}() {{")
            body_lines.append("    return Task.CompletedTask;")
        lines.extend(body_lines)
        lines.append("  }")
        return lines

    def _emit_bot_main(self, program: A.Program) -> list[str]:
        """Main(): the top-level statements (in file order), the
        token check, bot setup, the event registrations, login +
        start, the task loops, then Task.Delay(-1) (run forever)."""
        self._need_discord("Discord.TokenType")
        lines = ["  public static async Task Main(string[] args) {"]
        lines.extend(self._emit_block(program.stmts, 2, top_level=True))
        lines.append("    string token = Environment.GetEnvironmentVariable(\"DISCORD_TOKEN\");")
        lines.append("    if (token == null || token == \"\") {")
        lines.append("      throw new Exception(\"set the DISCORD_TOKEN environment variable\");")
        lines.append("    }")
        lines.append("    bot = new Bot();")
        lines.append("    bot.Setup();")
        for ev in program.events:
            lines.extend(self._emit_event_registration(ev, 2))
        if program.commands:
            self._need_discord("Discord.WebSocket.SocketSlashCommand")
            lines.append("    bot.SlashCommandExecuted += async (SocketSlashCommand interaction) => {")
            lines.append("      await _DispatchCommand(interaction);")
            lines.append("    };")
        if self._has_components:
            self._need_discord(
                "Discord.WebSocket.SocketMessageComponent")
            lines.append("    bot.ButtonExecuted += async (SocketMessageComponent interaction) => {")
            lines.append("      await _DispatchComponent(interaction);")
            lines.append("    };")
            lines.append("    bot.SelectMenuExecuted += async (SocketMessageComponent interaction) => {")
            lines.append("      await _DispatchComponent(interaction);")
            lines.append("    };")
        if any(c["kind"] == "modal" for c in self._bot_components):
            self._need_discord("Discord.WebSocket.SocketModal")
            lines.append("    bot.ModalSubmitted += async (SocketModal interaction) => {")
            lines.append("      if (ModalHandlers.TryGetValue(interaction.Data.CustomId, out var handler)) {")
            lines.append("        await handler(interaction);")
            lines.append("      }")
            lines.append("    };")
        lines.append("    await bot.LoginAsync(TokenType.Bot, token);")
        lines.append("    await bot.StartAsync();")
        for task in program.tasks:
            name = self._task_fn_name(task)
            ms = task.interval_seconds * 1000
            lines.append("    _ = Task.Run(async () => {")
            lines.append("      while (true) {")
            lines.append(f"        await Task.Delay({ms});")
            lines.append("        try {")
            lines.append(f"          await {name}();")
            lines.append("        } catch (Exception e) {")
            lines.append("          Console.Error.WriteLine(e.ToString());")
            lines.append("        }")
            lines.append("      }")
            lines.append("    });")
        lines.append("    await Task.Delay(-1);")
        lines.append("  }")
        return lines

    # -- strings / _t (D16) ---------------------------------------------

    def _emit_strings(self, program: A.Program) -> list[str]:
        """The STRINGS dict + _DEFAULT_LOCALE + the _t helper (only
        when `strings` blocks exist). Lookup: exact locale -> base
        language -> default locale (the first declared)."""
        if not program.strings:
            return []
        index, default = self._strings_index()
        self._need_rt("toStr")
        lines = [f"  private static string _DEFAULT_LOCALE = {_cs_str(default)};"]
        lines.append("  private static Dictionary<string, Dictionary<string, string>> STRINGS = new Dictionary<string, Dictionary<string, string>> {")
        for locale, entries in index.items():
            lines.append(f"    [{_cs_str(locale)}] = new Dictionary<string, string> {{")
            for key, entry in entries.items():
                lines.append(f"      [{_cs_str(key)}] = {_cs_str(entry.template)},")
            lines.append("    },")
        lines.append("  };")
        lines.append("")
        lines.append("  private static string _t(string key, string locale, Dictionary<string, object> vars) {")
        lines.append("    string loc = locale ?? _DEFAULT_LOCALE;")
        lines.append("    Dictionary<string, string> table = null;")
        lines.append("    if (!STRINGS.TryGetValue(loc, out table)) {")
        lines.append("      string baseLoc = loc.Split('-')[0];")
        lines.append("      if (!STRINGS.TryGetValue(baseLoc, out table)) {")
        lines.append("        STRINGS.TryGetValue(_DEFAULT_LOCALE, out table);")
        lines.append("      }")
        lines.append("    }")
        lines.append("    if (table == null || !table.TryGetValue(key, out string template)) {")
        lines.append("      return key;")
        lines.append("    }")
        lines.append("    string result = template;")
        lines.append("    foreach (KeyValuePair<string, object> kv in vars) {")
        lines.append('      result = result.Replace("{" + kv.Key + "}", NewrRt.ToStr(kv.Value));')
        lines.append("    }")
        lines.append("    return result;")
        lines.append("  }")
        return lines

    def _emit_channel_text(self) -> list[str]:
        return [
            "  private static string _ChannelText(IMessageChannel channel) {",
            "    // The channel as text: its mention, or \"\".",
            "    if (channel == null) {",
            "      return \"\";",
            "    }",
            "    return \"<#\" + channel.Id.ToString() + \">\";",
            "  }",
        ]

    # -- dispatch -------------------------------------------------------

    def _emit_dispatch_command(self) -> list[str]:
        """The slash-command dispatcher (the single shared
        dispatcher: command errors get the generic ephemeral
        reply, via followup when the interaction was already
        answered)."""
        return [
            "  private static async Task _DispatchCommand(SocketSlashCommand interaction) {",
            "    foreach (var cmd in Commands) {",
            "      if (cmd.Data.Name == interaction.CommandName) {",
            "        try {",
            "          await cmd.Execute(interaction);",
            "        } catch (Exception e) {",
            "          Console.Error.WriteLine(\"[dispatch] /\" + interaction.CommandName + \" failed: \" + e.ToString());",
            "          string message = \"Something went wrong running that command. Try again in a moment.\";",
            "          try {",
            "            if (interaction.HasResponded) {",
            "              await interaction.FollowupAsync(message, ephemeral: true);",
            "            } else {",
            "              await interaction.RespondAsync(message, ephemeral: true);",
            "            }",
            "          } catch (Exception replyError) {",
            "            Console.Error.WriteLine(\"[dispatch] could not send the error reply: \" + replyError.ToString());",
            "          }",
            "        }",
            "        return;",
            "      }",
            "    }",
            "  }",
        ]

    def _emit_gate_ok(self) -> list[str]:
        """The `only:` gate for a component interaction: author /
        anyone / roles. The invoking interaction's author is looked
        up by message id (registered in _componentAuthors when the
        components were attached); the gate itself is per customId
        (registered in _componentGates)."""
        return [
            "  private static async Task<bool> _GateOk(SocketMessageComponent interaction) {",
            "    if (!_componentGates.TryGetValue(interaction.Data.CustomId, out var gate)) {",
            "      return true;",
            "    }",
            "    if (gate.Kind == \"author\") {",
            "      if (_componentAuthors.TryGetValue(interaction.Message.Id, out var author) && interaction.User.Id != author) {",
            "        await interaction.RespondAsync(\"This button is not for you.\", ephemeral: true);",
            "        return false;",
            "      }",
            "      return true;",
            "    }",
            "    if (gate.Kind == \"roles\") {",
            "      SocketGuildUser guildUser = interaction.User as SocketGuildUser;",
            "      bool ok = false;",
            "      if (guildUser != null) {",
            "        foreach (var role in guildUser.Roles) {",
            "          foreach (string want in gate.Roles) {",
            "            if (role.Name == want) {",
            "              ok = true;",
            "            }",
            "          }",
            "        }",
            "      }",
            "      if (!ok) {",
            "        await interaction.RespondAsync(\"You do not have a role that can use this.\", ephemeral: true);",
            "        return false;",
            "      }",
            "      return true;",
            "    }",
            "    return true;",
            "  }",
        ]

    def _emit_dispatch_component(self) -> list[str]:
        """The button/select dispatcher: the `only:` gate, then the
        handler registered for the customId."""
        return [
            "  private static async Task _DispatchComponent(SocketMessageComponent interaction) {",
            "    try {",
            "      if (!await _GateOk(interaction)) {",
            "        return;",
            "      }",
            "      if (ComponentHandlers.TryGetValue(interaction.Data.CustomId, out var handler)) {",
            "        await handler(interaction);",
            "      }",
            "    } catch (Exception e) {",
            "      Console.Error.WriteLine(\"[dispatch] component failed: \" + e.ToString());",
            "    }",
            "  }",
        ]

    def _emit_component_timeout(self) -> list[str]:
        """The per-answer timeout task (the C# twin of the JS
        collector's `end` callback): after the timeout, disable
        every component on the message, edit it, then run each
        component's `on_timeout` body."""
        return [
            "  private static async Task _ComponentTimeoutAsync(IUserMessage message, ComponentBuilder components, int timeoutSeconds, List<Func<Task>> onTimeout) {",
            "    await Task.Delay(timeoutSeconds * 1000);",
            "    foreach (var row in components.ActionRows) {",
            "      foreach (var component in row.Components) {",
            "        if (component is ButtonBuilder button) {",
            "          button.IsDisabled = true;",
            "        } else if (component is SelectMenuBuilder select) {",
            "          select.IsDisabled = true;",
            "        }",
            "      }",
            "    }",
            "    try {",
            "      await message.ModifyAsync(m => { m.Components = components.Build(); });",
            "    } catch (Exception) {",
            "    }",
            "    foreach (Func<Task> f in onTimeout) {",
            "      try {",
            "        await f();",
            "      } catch (Exception e) {",
            "        Console.Error.WriteLine(e.ToString());",
            "      }",
            "    }",
            "  }",
        ]

    # -- command registration (the standalone deploy script) ----------

    def _emit_deploy_file(self, program: A.Program,
                          bot_filename: str) -> str:
        """`<stem>_deploy.cs`: the standalone command-registration
        program (the Discord.Net twin of the JS deploy script). It
        reads the command builders from the bot class (`<stem>.
        Commands` — the bot file's `Main` never runs here) and bulk
        overwrites the application's commands via DiscordRestClient:
        guild-scoped when DISCORD_GUILD_ID is set (instant, dev),
        global otherwise (up to an hour to propagate). Run once,
        then run the bot.

        The file is standalone: compile it together with the bot
        file + newr_rt.cs in a console project referencing
        Discord.Net + Microsoft.Data.Sqlite, with
        `<StartupObject><stem>Deploy</StartupObject>` (it has its
        own Main; the bot project excludes this file so the bot
        still builds with a single entry point)."""
        stem = self.output_stem
        lines = self._header(self.main_path,
                             "Command registration — run once, then run the bot.")
        lines.append("")
        lines.append("using System;")
        lines.append("using System.Collections.Generic;")
        lines.append("using System.Threading.Tasks;")
        lines.append("using Discord;")
        lines.append("using Discord.Rest;")
        lines.append("")
        lines.append(f"public class {stem}Deploy {{")
        lines.append("  public static async Task Main(string[] args) {")
        lines.append("    string token = Environment.GetEnvironmentVariable(\"DISCORD_TOKEN\");")
        lines.append("    string clientId = Environment.GetEnvironmentVariable(\"DISCORD_CLIENT_ID\");")
        lines.append("    string guildId = Environment.GetEnvironmentVariable(\"DISCORD_GUILD_ID\");")
        lines.append("    if (token == null || token == \"\" || clientId == null || clientId == \"\") {")
        lines.append("      throw new Exception(\"set DISCORD_TOKEN and DISCORD_CLIENT_ID\");")
        lines.append("    }")
        lines.append("    List<ApplicationCommandProperties> body = new List<ApplicationCommandProperties>();")
        if program.commands:
            lines.append(f"    foreach (var cmd in {stem}.Commands) {{")
            lines.append("      body.Add(cmd.Data.Build());")
            lines.append("    }")
        lines.append("    DiscordRestClient rest = new DiscordRestClient();")
        lines.append("    await rest.LoginAsync(TokenType.Bot, token);")
        lines.append("    if (guildId != null && guildId != \"\") {")
        lines.append("      await rest.BulkOverwriteGuildCommands(body.ToArray(), ulong.Parse(guildId));")
        lines.append("      Console.WriteLine(\"Registered \" + body.Count.ToString() + \" command(s) in guild \" + guildId + \".\");")
        lines.append("    } else {")
        lines.append("      await rest.BulkOverwriteGlobalCommands(body.ToArray());")
        lines.append("      Console.WriteLine(\"Registered \" + body.Count.ToString() + \" command(s) globally.\");")
        lines.append("    }")
        lines.append("  }")
        lines.append("}")
        return "\n".join(lines) + "\n"

    # -- answer statements (reply / defer / edit / followup / send) ---

    def _answer_parts(self, value, indent: int) -> tuple[list[str], str, bool]:
        """A reply/edit/followup value -> (pre-lines, call argument,
        is_embed). A text value is the argument itself; an embed is
        built on the lines before and the argument is its variable
        name (a fresh temp per embed). A `?` value propagates
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
        """The Discord.Net call text for one answer kind. `arg` is
        the text argument, or the embed variable's name. The
        handler context names a command/component reply
        `"interaction"` (the same `RespondAsync` call as `"reply"`).
        Verified live against Discord.Net 3.20.2: RespondAsync /
        ModifyOriginalResponseAsync / FollowupAsync /
        SendMessageAsync / ModifyAsync, and the `embed:` /
        `ephemeral:` named arguments."""
        if kind == "interaction":
            kind = "reply"
        if kind == "reply":
            if is_embed:
                if ephemeral:
                    return (f"await interaction.RespondAsync(embed: {arg}, "
                            "ephemeral: true)")
                return f"await interaction.RespondAsync(embed: {arg})"
            if ephemeral:
                return (f"await interaction.RespondAsync({arg}, "
                        "ephemeral: true)")
            return f"await interaction.RespondAsync({arg})"
        if kind == "edit":
            # The lambda parameter is a fresh temp (a fixed name
            # could collide with a user variable); _attach_components
            # finds it again from the `=> {` marker.
            param = self._new_temp_name()
            if is_embed:
                return (f"await interaction.ModifyOriginalResponseAsync("
                        f"{param} => {{ {param}.Embed = {arg}; }})")
            return (f"await interaction.ModifyOriginalResponseAsync("
                    f"{param} => {{ {param}.Content = {arg}; }})")
        if kind == "followup":
            if is_embed:
                return f"await interaction.FollowupAsync(embed: {arg})"
            return f"await interaction.FollowupAsync({arg})"
        if kind == "system_channel":
            if is_embed:
                return f"await channel.SendMessageAsync(embed: {arg})"
            return f"await channel.SendMessageAsync({arg})"
        if kind == "message_channel":
            if is_embed:
                return f"await message.Channel.SendMessageAsync(embed: {arg})"
            return f"await message.Channel.SendMessageAsync({arg})"
        if kind == "timeout_edit":
            var = self._handler_ctx().get("message_var")
            if var is None:
                self._fail("internal: timeout reply without a message")
            param = self._new_temp_name()
            if is_embed:
                return (f"await {var}.ModifyAsync("
                        f"{param} => {{ {param}.Embed = {arg}; }})")
            return (f"await {var}.ModifyAsync("
                    f"{param} => {{ {param}.Content = {arg}; }})")
        self._fail(f"internal: unknown answer kind `{kind}`")

    def _emit_reply(self, stmt, indent: int):
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

    def _emit_defer(self, stmt, indent: int):
        p = "  " * indent
        if self._handler_ctx()["reply"] != "interaction":
            self._fail("`defer` needs an interaction (a command or "
                       "component handler)")
        return [f"{p}await interaction.DeferAsync();"]

    def _emit_edit(self, stmt, indent: int):
        p = "  " * indent
        if self._handler_ctx()["reply"] != "interaction":
            self._fail("`edit` needs an interaction (a command or "
                       "component handler)")
        pre, arg, is_embed = self._answer_parts(stmt.value, indent)
        lines = list(pre)
        lines.append(f"{p}{self._answer_call('edit', arg, is_embed)};")
        return lines

    def _emit_followup(self, stmt, indent: int):
        p = "  " * indent
        if self._handler_ctx()["reply"] != "interaction":
            self._fail("`followup` needs an interaction (a command or "
                       "component handler)")
        pre, arg, is_embed = self._answer_parts(stmt.value, indent)
        lines = list(pre)
        lines.append(f"{p}{self._answer_call('followup', arg, is_embed)};")
        return lines

    def _emit_send(self, stmt, indent: int):
        p = "  " * indent
        pre, arg, is_embed = self._answer_parts(stmt.value, indent)
        channel = self.emit_expr(stmt.channel, 0)
        lines = list(pre)
        if is_embed:
            lines.append(f"{p}await {channel}.SendMessageAsync(embed: {arg});")
        else:
            lines.append(f"{p}await {channel}.SendMessageAsync({arg});")
        return lines

    # -- components (button / select / modal) ------------------------

    def _emit_button(self, stmt: A.ButtonDecl, indent: int) -> list[str]:
        p = "  " * indent
        var = self._new_component_name()
        style = stmt.style or "primary"
        if style == "link":
            self._fail("a `link` button cannot have a `do:` body "
                       "(Discord never sends an interaction for it)")
        if stmt.url:
            self._fail("a `link` button cannot have a `do:` body "
                       "(Discord never sends an interaction for it)")
        self._need_discord("Discord.ComponentBuilder")
        self._need_discord("Discord.ButtonStyle")
        self._need_discord("Discord.WebSocket.SocketMessageComponent")
        custom = stmt.custom_id or self._component_var_name(stmt.label)
        style_map = {"primary": "Primary", "secondary": "Secondary",
                     "success": "Success", "danger": "Danger"}
        cs_style = style_map.get(style)
        if cs_style is None:
            self._fail(f"unknown button style `{style}`")
        # The shared handler (registered in ComponentHandlers, keyed
        # by customId). The author/roles gate lives in _GateOk (see
        # _attach_components), which looks the author up by message.
        handler_lines = [f"    [{_cs_str(custom)}] = async (SocketMessageComponent interaction) => {{"]
        self._push_handler("component",
                           ["user", "guild", "channel", "interaction"],
                           reply="interaction",
                           locale="interaction.UserLocale")
        try:
            handler_lines.extend(self._emit_block(stmt.body, 3,
                                                  new_scope=False))
        finally:
            self._pop_handler()
        handler_lines.append("    },")
        self._bot_components.append({"kind": "component",
                                     "custom": custom,
                                     "lines": handler_lines})
        self._has_components = True
        timeout = stmt.timeout if stmt.timeout is not None else 180
        on_timeout_lines = None
        if stmt.on_timeout is not None:
            self._push_handler("timeout", [], reply="timeout_edit",
                               locale=None)
            self._handler_ctx_stack[-1]["message_var"] = f"{var}_message"
            try:
                # Emitted at indent 0: _attach_components re-indents
                # the body into the timeout task's lambda.
                on_timeout_lines = self._emit_block(stmt.on_timeout, 0,
                                                    new_scope=False)
            finally:
                self._pop_handler()
        self._pending_components.append({
            "var": var, "custom": custom,
            "has_timeout": stmt.on_timeout is not None,
            "timeout": timeout,
            "component_type": "Button",
            "only_kind": stmt.only_kind, "only_roles": stmt.only_roles,
            "on_timeout_lines": on_timeout_lines,
        })
        # The builder (built in the handler, before the answer it
        # attaches to — _emit_block moves these lines before it).
        return [f"{p}ComponentBuilder {var} = new ComponentBuilder().WithButton({_cs_str(stmt.label)}, {_cs_str(custom)}, ButtonStyle.{cs_style});"]

    def _emit_select(self, stmt: A.SelectDecl, indent: int) -> list[str]:
        p = "  " * indent
        var = self._new_component_name()
        timeout = stmt.timeout if stmt.timeout is not None else 180
        min_v = stmt.min_values if stmt.min_values is not None else 1
        max_v = stmt.max_values if stmt.max_values is not None else 1
        custom = self._component_var_name(stmt.label)
        self._need_discord("Discord.ComponentBuilder")
        self._need_discord("Discord.SelectMenuOptionBuilder")
        self._need_discord("Discord.WebSocket.SocketMessageComponent")
        options = ", ".join(
            f"new SelectMenuOptionBuilder().WithLabel({_cs_str(o)}).WithValue({_cs_str(o)})"
            for o in stmt.options)
        handler_lines = [f"    [{_cs_str(custom)}] = async (SocketMessageComponent interaction) => {{"]
        self._push_handler("component",
                           ["user", "guild", "channel", "interaction"],
                           reply="interaction",
                           locale="interaction.UserLocale")
        try:
            self._handler_binding(
                "values",
                "new List<string>(interaction.Data.Values)",
                "string.Join(\", \", interaction.Data.Values)",
                Type("list", (Type("str"),)))
            handler_lines.extend(self._emit_block(stmt.body, 3,
                                                  new_scope=False))
        finally:
            self._pop_handler()
        handler_lines.append("    },")
        self._bot_components.append({"kind": "component",
                                     "custom": custom,
                                     "lines": handler_lines})
        self._has_components = True
        on_timeout_lines = None
        if stmt.on_timeout is not None:
            self._push_handler("timeout", [], reply="timeout_edit",
                               locale=None)
            self._handler_ctx_stack[-1]["message_var"] = f"{var}_message"
            try:
                on_timeout_lines = self._emit_block(stmt.on_timeout, 0,
                                                    new_scope=False)
            finally:
                self._pop_handler()
        self._pending_components.append({
            "var": var, "custom": custom,
            "has_timeout": stmt.on_timeout is not None,
            "timeout": timeout,
            "component_type": "StringSelect",
            "only_kind": stmt.only_kind, "only_roles": stmt.only_roles,
            "on_timeout_lines": on_timeout_lines,
        })
        return [f"{p}ComponentBuilder {var} = new ComponentBuilder().WithSelectMenu({_cs_str(custom)}, new List<SelectMenuOptionBuilder> {{ {options} }}, {_cs_str(stmt.label)}, minValues: {min_v}, maxValues: {max_v});"]

    def _emit_modal(self, stmt: A.ModalDecl, indent: int) -> list[str]:
        p = "  " * indent
        var = self._new_component_name()
        custom = stmt.custom_id or self._component_var_name(stmt.title)
        self._need_discord("Discord.ModalBuilder")
        self._need_discord("Discord.TextInputStyle")
        self._need_discord("Discord.WebSocket.SocketModal")
        lines = [f"{p}ModalBuilder {var} = new ModalBuilder().WithTitle({_cs_str(stmt.title)}).WithCustomId({_cs_str(custom)});"]
        for field in stmt.fields:
            style = ("Paragraph" if field.style == "paragraph" else "Short")
            req = "true" if field.required else "false"
            mn = f", minLength: {field.min_length}" if field.min_length is not None else ""
            mx = f", maxLength: {field.max_length}" if field.max_length is not None else ""
            lines.append(f"{p}{var}.AddTextInput({_cs_str(field.name)}, {_cs_str(field.name)}, TextInputStyle.{style}, required: {req}{mn}{mx});")
        # The submit handler (registered in ModalHandlers). A modal
        # has no Discord.Net timeout (unlike a button/select), so a
        # `timeout:` / `on_timeout:` on a modal is accepted by the
        # checker and ignored here.
        handler_lines = [f"    [{_cs_str(custom)}] = async (SocketModal interaction) => {{"]
        self._push_handler("modal",
                           ["user", "guild", "channel", "interaction"],
                           reply="interaction",
                           locale="interaction.UserLocale")
        try:
            for field in stmt.fields:
                self._used_modal_value = True
                self._handler_binding(
                    field.name,
                    f"_ModalValue(interaction, {_cs_str(field.name)})",
                    f"_ModalValue(interaction, {_cs_str(field.name)})",
                    Type("str"))
            handler_lines.extend(self._emit_block(stmt.body, 3,
                                                  new_scope=False))
        finally:
            self._pop_handler()
        handler_lines.append("    },")
        self._bot_components.append({"kind": "modal", "custom": custom,
                                     "lines": handler_lines})
        lines.append(f"{p}await interaction.RespondWithModalAsync({var}.Build());")
        return lines

    def _attach_components(self, lines: list[str]):
        """Attach the pending components (set by _emit_button /
        _emit_select) to the block's last answer line: the answer
        gains `components: <builder>.Build()`. The message is
        captured right after the answer (statements may follow the
        component), the author + gates are registered for the
        dispatcher, and — when at least one component has an
        `on_timeout` — a timeout task is started (the C# twin of
        the JS collector's `end` callback)."""
        if not self._pending_components:
            return
        pending = self._pending_components
        self._pending_components = []
        if self._last_answer is None:
            self._fail("a `button` / `select` must follow the reply "
                       "it attaches to (there is no message to put "
                       "the components on)")
        idx, _kind = self._last_answer
        line = lines[idx].rstrip()
        p = line[:len(line) - len(line.lstrip())]
        # The combined ComponentBuilder (one per component, merged
        # by ActionRow — a reply takes a single MessageComponent).
        if len(pending) == 1:
            combined = pending[0]["var"]
            combined_lines: list[str] = []
        else:
            combined = self._new_temp_name()
            combined_lines = [f"{p}ComponentBuilder {combined} = new ComponentBuilder();"]
            for c in pending:
                combined_lines.append(f"{p}{combined}.ActionRows.AddRange({c['var']}.ActionRows);")
        # Rewrite the answer line to carry the components. The form
        # depends on the CALL, not the statement kind — a reply in
        # a member event is a SendMessageAsync, not a RespondAsync.
        if "ModifyOriginalResponseAsync(" in line or "ModifyAsync(" in line:
            marker = "=> { "
            pos = line.index(marker) + len(marker)
            open_paren = line.rindex("(", 0, pos)
            param = line[open_paren + 1:line.index(" =>", open_paren)].strip()
            lines[idx] = line[:pos] + f"{param}.Components = {combined}.Build(); " + line[pos:]
        elif line.endswith(");"):
            lines[idx] = line[:-2] + f", components: {combined}.Build());"
        else:
            self._fail("internal: the answer line does not end with `)`")
        # The combined builder lines go before the answer.
        lines[idx:idx] = combined_lines
        idx += len(combined_lines)
        # Capture the message: several components may share one
        # answer (and one message) — capture it once, under the
        # first component's var, and alias the rest to it.
        first_var = pending[0]["var"]
        inserted = 0
        answer = lines[idx]
        if "ModifyAsync(" in answer and \
                "ModifyOriginalResponseAsync(" not in answer:
            # A button attached to a timeout-edit reply: the message
            # is already the on_timeout handler's message.
            message_var = self._handler_ctx().get("message_var")
            if message_var is None:
                self._fail("internal: timeout reply without a message")
        elif "SendMessageAsync(" in answer or "FollowupAsync(" in answer \
                or "ModifyOriginalResponseAsync(" in answer:
            # These calls return the message — capture it directly
            # by rewriting the answer line.
            message_var = f"{first_var}_message"
            stripped = lines[idx]
            indent_len = len(stripped) - len(stripped.lstrip())
            lines[idx] = (stripped[:indent_len]
                          + f"IUserMessage {message_var} = "
                          + stripped[indent_len:])
        else:
            # A reply answers the original response — fetchReply
            # returns it (GetOriginalResponseAsync in Discord.Net).
            message_var = f"{first_var}_message"
            lines.insert(idx + 1,
                         f"{p}IUserMessage {message_var} = "
                         f"await interaction.GetOriginalResponseAsync();")
            inserted += 1
        for c in pending[1:]:
            lines.insert(idx + 1 + inserted,
                         f"{p}IUserMessage {c['var']}_message = {message_var};")
            inserted += 1
        # Register the author + gates (the dispatcher looks them up
        # by message id / customId). The author is the handler's
        # `user` binding (interaction.User / member / message.Author).
        author_binding = self._lookup("user")
        if author_binding is not None:
            lines.insert(idx + 1 + inserted,
                         f"{p}_componentAuthors[{message_var}.Id] = "
                         f"{author_binding.value}.Id;")
            inserted += 1
        for c in pending:
            roles = ", ".join(_cs_str(r) for r in (c["only_roles"] or []))
            lines.insert(idx + 1 + inserted,
                         f"{p}_componentGates[{_cs_str(c['custom'])}] = "
                         f"(\"{c['only_kind']}\", new List<string> {{ {roles} }});")
            inserted += 1
        # The timeout: disable the components, edit the message,
        # then run each component's on_timeout body. Only when at
        # least one component has an on_timeout (the JS collector's
        # `end` callback is only emitted then).
        if any(c.get("has_timeout") for c in pending):
            self._used_component_timeout = True
            timeout = max(c["timeout"] for c in pending)
            timeout_var = f"{first_var}_timeout"
            timeout_lines = [f"{p}List<Func<Task>> {timeout_var} = new List<Func<Task>> {{"]
            for c in pending:
                if not c.get("has_timeout"):
                    continue
                timeout_lines.append(f"{p}  async () => {{")
                for ol in (c.get("on_timeout_lines") or []):
                    if ol.strip():
                        timeout_lines.append(f"{p}    {ol}")
                    else:
                        timeout_lines.append("")
                timeout_lines.append(f"{p}  }},")
            timeout_lines.append(f"{p}}};")
            timeout_lines.append(f"{p}_ = _ComponentTimeoutAsync({message_var}, {combined}, {timeout}, {timeout_var});")
            lines[idx + 1 + inserted:idx + 1 + inserted] = timeout_lines

    # Handler bindings: name -> (value form, interpolation form,
    # newr type name). The value form is what a bare name means; the
    # interpolation form is what `{name}` renders as (the mention /
    # name form, per the v0 golden). The C# twin of the JS emitter's
    # _BOT_BINDINGS — Discord.Net spellings, verified live against
    # Discord.Net 3.20.2 (SocketSlashCommand has no `Guild` property:
    # the guild comes from the channel cast; `UserLocale` is the
    # interaction locale).
    _BOT_BINDINGS: dict[str, dict[str, tuple[str, str | None, str]]] = {
        "command": {
            "user": ("interaction.User",
                     "interaction.User.Mention", "user"),
            "guild": ("(interaction.Channel as SocketGuildChannel)"
                      "?.Guild",
                      "((interaction.Channel as SocketGuildChannel)"
                      "?.Guild != null ? ((SocketGuildChannel) "
                      "interaction.Channel).Guild.Name : \"DM\")",
                      "guild"),
            "channel": ("interaction.Channel",
                        "_ChannelText(interaction.Channel)", "channel"),
            "interaction": ("interaction", None, "interaction"),
        },
        "member_join": {
            "user": ("member", "member.Mention", "member"),
            "guild": ("member.Guild", "member.Guild.Name", "guild"),
        },
        "member_remove": {
            "user": ("member", "member.Mention", "member"),
            "guild": ("member.Guild", "member.Guild.Name", "guild"),
        },
        "ready": {
            "bot": ("bot.CurrentUser", "bot.CurrentUser.Username",
                    "user"),
        },
        "message": {
            "message": ("message", "message.Content", "message"),
            "user": ("message.Author", "message.Author.Mention", "user"),
        },
        "component": {  # a button/select callback has its own
            "user": ("interaction.User",
                     "interaction.User.Mention", "user"),
            "guild": ("(interaction.Channel as SocketGuildChannel)"
                      "?.Guild",
                      "((interaction.Channel as SocketGuildChannel)"
                      "?.Guild != null ? ((SocketGuildChannel) "
                      "interaction.Channel).Guild.Name : \"DM\")",
                      "guild"),
            "channel": ("interaction.Channel",
                        "_ChannelText(interaction.Channel)", "channel"),
            "interaction": ("interaction", None, "interaction"),
        },
        "modal": {  # a modal submit callback (a SocketModal)
            "user": ("interaction.User",
                     "interaction.User.Mention", "user"),
            "guild": ("(interaction.Channel as SocketGuildChannel)"
                      "?.Guild",
                      "((interaction.Channel as SocketGuildChannel)"
                      "?.Guild != null ? ((SocketGuildChannel) "
                      "interaction.Channel).Guild.Name : \"DM\")",
                      "guild"),
            "channel": ("interaction.Channel",
                        "_ChannelText(interaction.Channel)", "channel"),
            "interaction": ("interaction", None, "interaction"),
        },
    }

    # newr intent name -> Discord.Net GatewayIntents member (the
    # names newr programs write are the discord.py ones, as in JS).
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
        self._fn_ctx.append({"return_type": None, "ensures": [],
                             "in_handler": True})
        self._push_scope()
        table = self._BOT_BINDINGS.get(kind, {})
        for name in names:
            if name not in table:
                self._fail(f"internal: no binding for `{name}` in a "
                           f"`{kind}` handler")
            value, interp, tname = table[name]
            self._scopes[-1][name] = _Binding(
                java=value, type=Type(tname), kind="handler",
                interp=interp)
        if kind in ("command", "component", "modal"):
            # The guild binding reads the channel as a guild channel.
            self._need_discord("Discord.WebSocket.SocketGuildChannel")
        return ctx

    def _pop_handler(self):
        self._pop_scope()
        self._fn_ctx.pop()
        self._handler_ctx_stack.pop()

    def _handler_binding(self, name: str, value: str,
                         interp: str | None, t: Type):
        """Bind one extra handler name (an option, a select's
        `values`, a modal field) in the current handler scope."""
        self._scopes[-1][name] = _Binding(
            java=value, type=t, kind="handler", interp=interp)

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

    def _component_var_name(self, label: str) -> str:
        words = "".join(ch if ch.isalnum() else " " for ch in label).split()
        return "_".join(w.lower() for w in words) or "component"

    def _task_fn_name(self, task: A.TaskDecl) -> str:
        """A task's loop-function name, from its interval (a task
        has no name in newr). Two tasks with the same interval get a
        numeric suffix. The name is computed once per task and
        cached — Main() and the definition must agree."""
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


def _cs_class_name(stem: str) -> str:
    """Sanitize a file stem into a valid C# class name. Invalid
    characters become `_`; a leading digit gets a `_` prefix; a
    C# keyword gets a trailing underscore. (C# identifiers do NOT
    allow `$`, unlike Java's — a `$` in a stem becomes `_`.) Unlike
    Java, the file name need NOT match the class name — but the
    emitter keeps them in agreement (the class name IS the file
    stem, sanitized), so a stem of `Main` renames BOTH: C#
    forbids a member with the same name as its enclosing type
    (CS0542), so a class named `Main` cannot contain the
    entry-point method `Main`. The CLI uses this for the output
    file name too, so the file and the class always agree."""
    out = []
    for ch in stem:
        if ch.isalnum() or ch == "_":
            out.append(ch)
        else:
            out.append("_")
    name = "".join(out) or "Main"
    if name[0].isdigit():
        name = "_" + name
    if name in _CS_KEYWORDS:
        name += "_"
    if name == "Main":
        # CS0542: a class named `Main` cannot contain the
        # entry-point method `Main`. The file is renamed with
        # the class (MainProgram.cs), and `dotnet run` finds the
        # entry point by method name, not by class name.
        name = "MainProgram"
    return name


def emit_program_cs(program: A.Program, *, main_path: str,
                      sources: dict[str, str],
                      line_comments: bool = False,
                      output_stem: str | None = None) -> EmitResult:
    """Emit a CHECKED program to C# — one `.cs` per `.newr` file,
    plus a `<stem>.csproj` (console app, net8.0, Nullable disabled
    — the generated code relies on reference types being
    nullable-capable, matching newr's `T?` for reference types).

    `main_path` is the input file as passed (D2: the header names
    it); `sources` maps file paths to their text (for diagnostics
    and `--line-comments`). `output_stem` is the `-o` stem (the
    public class name — sanitized to a valid C# identifier). The
    result's `files` maps an output basename (`hello.cs`) to its
    text; `main_file` is the main output's basename; `uses_rt` says
    whether any file needs `NewrRt` (the caller copies
    `compiler/newr_rt.cs` next to the output when it is true, D4's
    C# twin)."""
    stem = _cs_class_name(output_stem or Path(main_path).stem)
    em = _CSEmitter(program, main_path=main_path, sources=sources,
                      line_comments=line_comments, output_stem=stem)
    files, uses_rt = em._emit_file_set(program, main_path)
    # `env.args` needs Main()'s `args` in the runtime — but only
    # when some file actually uses it (D4: the runtime is copied
    # only when used, so hello stays runtime-free). A module file
    # may use env.args while the main file does not, and the main
    # file is emitted first, so the line is patched in here, after
    # the whole file set is known. SetMainArgs prepends the class
    # name as argv[0] (newr's env.args is sys.argv 1:1 — Python:
    # sys.argv, JS: process.argv.slice(1) — and .NET's Main args
    # exclude the program name).
    main_file = stem + ".cs"
    if any("NewrRt.EnvArgs()" in text for text in files.values()):
        main_text = files[main_file]
        anchor = "  public static void Main(string[] args) {\n"
        if "SetMainArgs(" not in main_text and anchor in main_text:
            files[main_file] = main_text.replace(
                anchor,
                anchor + f"    NewrRt.SetMainArgs(\"{stem}\", args);\n",
                1)
    # The project file: always emitted (a C# program needs one to
    # build/run with dotnet). Nullable is disabled — the generated
    # code uses plain reference types for newr's nullable
    # references (a `str?` is a `string` that may be null).
    # CS8981 is suppressed: the class name IS the file stem (K1),
    # and a lowercase stem (hello.newr -> class hello) is normal
    # for newr — the warning is about a hypothetical future
    # language reservation, not a real problem.
    # A bot program also references Discord.Net (the bot library,
    # 3.20.2 — verified live on the build VM) and
    # Microsoft.Data.Sqlite (the store backend, 8.0.0 — the 8.x
    # line matching .NET 8). Its Compile items are enumerated
    # explicitly instead of globbed: the SDK's default glob
    # compiles EVERY .cs in the output directory, so building two
    # bots that share a directory (examples/hello_bot.cs +
    # examples/shop.cs) would give each project the other's Main
    # (CS0017). The deploy file is never listed — it has its own
    # Main and is built separately (see _emit_deploy_file).
    # newr_rt.cs is listed conditionally: the caller copies it
    # next to the output only when the program uses the runtime.
    # CS9057 is suppressed for ui programs only: Avalonia 12.1.6
    # ships Roslyn 4.14 analyzers, and SDK 8.0.427 runs Roslyn
    # 4.11, so a code-only Avalonia app (an Application subclass
    # in .cs, no XAML) warns on every build — proven by probe: a
    # hand-written code-only app warns the same way, while the
    # proof's XAML app builds with 0 warnings. The analyzers
    # still run; only the version-skew warning is silenced.
    nowarn = "CS8981;CS9057" if em.uses_ui else "CS8981"
    csproj_lines = [
        "<Project Sdk=\"Microsoft.NET.Sdk\">",
        "  <PropertyGroup>",
        "    <OutputType>Exe</OutputType>",
        "    <TargetFramework>net8.0</TargetFramework>",
        "    <Nullable>disable</Nullable>",
        "    <ImplicitUsings>disable</ImplicitUsings>",
        f"    <NoWarn>{nowarn}</NoWarn>",
        f"    <AssemblyName>{stem}</AssemblyName>",
        "  </PropertyGroup>",
    ]
    # The package references: a bot program needs Discord.Net +
    # Microsoft.Data.Sqlite (the store backend); a plain program
    # that uses `db.*` needs Microsoft.Data.Sqlite alone (B8 — the
    # gate is `bot is not None or uses_sqlite`, not `bot is not
    # None`). A program that uses neither gets no references at
    # all, so it stays dependency-free.
    if program.bot is not None or em.uses_sqlite:
        csproj_lines += [
            "  <ItemGroup>",
        ]
        if program.bot is not None:
            csproj_lines.append(
                "    <PackageReference Include=\"Discord.Net\" "
                "Version=\"3.20.2\" />")
        csproj_lines += [
            "    <PackageReference Include=\"Microsoft.Data.Sqlite\" "
            "Version=\"8.0.0\" />",
            "  </ItemGroup>",
        ]
    if em.uses_ui:
        # Avalonia 12.1.6 plus Avalonia.Desktop 12.1.6 plus
        # Avalonia.Themes.Fluent 12.1.6 (SPEC.md section 12.8):
        # the three references of the official template set,
        # proven on the build VM (M8c Avalonia proof). Fluent is
        # required: the Avalonia package alone does not contain
        # FluentTheme. Restore needs the packages in the global
        # packages cache or reachable from nuget.org; on this VM
        # the proof seeded a local feed and cache (see the M8c
        # Avalonia proof), which `dotnet restore` uses when
        # NUGET_PACKAGES points at it.
        csproj_lines += [
            "  <ItemGroup>",
            "    <PackageReference Include=\"Avalonia\" "
            "Version=\"12.1.6\" />",
            "    <PackageReference Include=\"Avalonia.Desktop\" "
            "Version=\"12.1.6\" />",
            "    <PackageReference Include=\"Avalonia.Themes.Fluent\" "
            "Version=\"12.1.6\" />",
            "  </ItemGroup>",
        ]
    if program.bot is not None:
        csproj_lines += [
            "  <ItemGroup>",
            "    <Compile Remove=\"*.cs\" />",
        ]
        for name in sorted(files):
            if name.endswith(".cs") and not name.endswith("_deploy.cs"):
                csproj_lines.append(f"    <Compile Include=\"{name}\" />")
        csproj_lines += [
            "    <Compile Include=\"newr_rt.cs\" "
            "Condition=\"Exists('newr_rt.cs')\" />",
            "  </ItemGroup>",
        ]
    csproj_lines.append("</Project>")
    files[stem + ".csproj"] = "\n".join(csproj_lines) + "\n"
    if program.bot is not None:
        # The standalone registration script (J10's C# twin) rides
        # along as a second output file.
        deploy = em._emit_deploy_file(program, Path(main_path).stem + ".cs")
        files[stem + "_deploy.cs"] = deploy
    return EmitResult(files=files,
                      main_file=stem + ".cs",
                      uses_rt=uses_rt)


# `emit_program` is the name the tests and the CLI use; the `_cs`
# suffix marks the implementation (the Python emitter's entry is
# `emit_program` in compiler/emitter.py).
emit_program = emit_program_cs
