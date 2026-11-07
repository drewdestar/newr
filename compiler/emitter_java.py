"""The Java emitter (M4, SPEC.md §18).

``newr build --target java`` lowers a *checked* program (the front
end has already run: parse -> resolve -> check) to readable Java -
one `.newr` file -> one `.java` file, structure preserved, user names
verbatim, helpers only in `newr_rt.java` (copied next to the output
as `NewrRt.java` when used - javac requires the file name to match
the public class, so the CLI renames on copy, K1).

Readability is a requirement, not a nicety: the output is code a
human would write - the Java goldens (`goldens/java/`) are the style
oracle, and the emitter reproduces them byte-for-byte
(BUILD_REPORT.md §21, K1–K13).

The emitter NEVER runs on an unchecked program: the CLI checks first
and exits 4 on errors. If it meets a node without a type (which only
happens on an unchecked program), it raises `EmitterError` - a clean
refusal, never a TypeError traceback.

Module system (K1): one public class per file, named after the file
stem verbatim (the class name IS the file stem - Java requires the
public class to live in the same-named file). File modules become
their own public classes in their own files; imported names are
qualified `Module.name`. A program with no `main` file stem gets
`Main`. Java floor: 21 LTS (Temurin 21.0.13 verified on the build
VM; the emitted code uses no feature newer than Java 17, so any
recent JDK compiles it).

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
# match a helper are harmless - but the class name `NewrRt` itself,
# the `main` method, its `args` parameter, and the java.lang type
# names the emitter writes (`String`, `Long`, ...) must not be
# shadowed by a user struct/enum/fn name in the same file.
_RT_HELPERS = frozenset({
    "add", "sub", "mul", "neg", "powInt", "powFloat", "tdiv", "tmod",
    "fmod", "fdiv", "roundHalfAway", "toInt", "toFloat", "toBool",
    "toStr", "floatStr", "checkInt", "asError", "optChain", "coalesce",
    "listGet", "listIndex", "listContains", "setRemove", "unwrapOpt",
    "tryResult", "timeFormat", "fileRead", "fileWrite", "fileAppend",
    "fileExists", "randomInt", "randomChoice", "randomShuffle",
    "eq", "range", "print", "input", "ok", "err", "chars", "splitStr",
    "strIndex", "absLong", "lenOf", "dictOf", "envGet", "envArgs",
    "hashOf", "structHash",
})
_RT_CLASSES = frozenset({
    "NewrError", "NewrOverflowError", "NewrDivisionByZeroError",
    "Result", "ThrowingSupplier",
})
_JAVA_LANG_TYPES = frozenset({
    "String", "Object", "Math", "Long", "Double", "Boolean", "Integer",
    "Byte", "Short", "Character", "Float", "Void", "Number", "Class",
    "System", "Thread", "Exception", "RuntimeException", "ArrayList",
    "LinkedList", "HashMap", "HashSet", "LinkedHashMap", "List",
    "Map", "Set", "Collection", "Iterable", "Function", "BiFunction",
    "Supplier", "Consumer", "Comparable", "Cloneable", "Serializable",
    "StringBuilder", "StringBuffer", "Arrays", "Collections",
    "Objects", "BigInteger", "BigDecimal", "StringJoiner",
})
_EMITTER_RESERVED = frozenset({
    "main", "args", "NewrRt", "Bot", "Store", "Fn0", "Fn1", "Fn2",
    "Fn3", "Fn4", "Fn5", "Fn6", "Fn7", "Fn8",
    "store", "economy", "commands", "modalHandlers", "bot",
    # Bot-internal names: handler parameters (the event is always
    # named `event` / `interaction` / `member` / `message` /
    # `readyClient`), the COMMANDS/STRINGS tables, the scheduler,
    # the component maps, and the startup locals. A user name that
    # collides is mangled (D20), so a top-level let can never be
    # shadowed by a handler parameter.
    "event", "interaction", "member", "message", "readyClient",
    "COMMANDS", "STRINGS", "_DEFAULT_LOCALE", "_t", "_channelText",
    "_newrScheduler", "_newrToken", "_newrBuilder",
    "_newrSystemChannel", "_newrAuthorId",
    "_componentAuthors", "buttonHandlers", "selectHandlers",
}) | _RT_CLASSES | _JAVA_LANG_TYPES

# Java reserved words (JLS 21) + literals + contextual keywords the
# emitter writes in positions where they would collide.
_JAVA_KEYWORDS = frozenset({
    "abstract", "assert", "boolean", "break", "byte", "case", "catch",
    "char", "class", "const", "continue", "default", "do", "double",
    "else", "enum", "extends", "final", "finally", "float", "for",
    "goto", "if", "implements", "import", "instanceof", "int",
    "interface", "long", "native", "new", "package", "private",
    "protected", "public", "return", "short", "static", "strictfp",
    "super", "switch", "synchronized", "this", "throw", "throws",
    "transient", "try", "void", "volatile", "while", "true", "false",
    "null", "var", "record", "sealed", "permits", "yield", "when",
    "non-sealed", "open", "module", "requires", "exports", "uses",
    "provides", "to", "with",
})


def _java_str(value: str) -> str:
    """Render a newr string value as a Java string literal.

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


def _java_float(value: float) -> str:
    """Render a float literal. Python repr is also a valid Java
    double literal ("1e+16", "5e-324", "1.5"). The non-finite
    spellings are not literals in Java, so they become the
    Double constants (an out-of-range literal parses to inf in
    Python and would otherwise emit `inf`, a compile error)."""
    if value != value:  # NaN
        return "Double.NaN"
    if value == float("inf"):
        return "Double.POSITIVE_INFINITY"
    if value == float("-inf"):
        return "Double.NEGATIVE_INFINITY"
    return repr(value)


# Java precedence (higher binds tighter), loosest -> tightest.
# The emitter's precedence-climbing printer uses this table; newr's
# own table (SPEC §9.2) maps onto it, with the traps documented
# where they bite - notably unary `-`: `-x ** y` parses as
# `-(x ** y)` in newr, and Java has no `**`, so the power helper
# call binds as a primary; and `!a == b` is `(!a) == b` in Java,
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
    "pow": 12,     # ** (emitted as a helper call - primary)
    "primary": 13,  # calls, indexing, member access, atoms
}

_BINOP_PREC = {
    "||": 1, "&&": 2,
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
    Results, structs and enums (and nullable forms of them - `eq`
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
    """One visible name: its Java rendering and its newr type.

    `java` is the Java name (possibly renamed); `interp` is the form
    used inside an interpolation (for handler bindings: the mention /
    name form); `value` is the form used as a value. For ordinary
    variables all three are the Java name. `kind` is one of
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


class _JavaEmitter:
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
        # Java names already taken at module level (for collision
        # aware allocation in _bind_module_level).
        self._module_used: set[str] = set()
        # Java names already used in the current Java function scope
        # (for rename allocation in _bind_fresh). Reset per function.
        self._fn_used: set[str] = set()
        # Hoists for the statement being emitted: each is a list of
        # lines (base indent 0) to emit before the statement -
        # hoisted lambda consts and lambda snapshot locals.
        self._hoists: list[list[str]] | None = None
        # True while emitting the DIRECT top-level statement list of
        # a file (main() / the module static block). A `let` there
        # assigns the static field instead of declaring a local
        # (K2) - a local would shadow the field and top-level
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
        # Components awaiting attachment (each a dict: var, row,
        # custom, only_kind, only_roles, timeout, has_timeout,
        # on_timeout_lines). Collected while emitting handler
        # bodies and consumed by _attach_components.
        self._pending_components: list[dict] = []
        self._component_counter = 0
        # The last "answer" line (a Reply / Followup / Edit) emitted
        # in the block currently being built, for component
        # attachment: (index into the block's line list, kind).
        self._last_answer: tuple[int, str] | None = None
        # Counter for unique temp names within a file.
        self._temp_counter = 0
        # Extra imports the bot emitter needs (fully-qualified
        # import lines without the trailing semicolon), e.g.
        # java.util.concurrent.* for the scheduler.
        self._extra_imports: set[str] = set()
        # Bot-emission accumulators, filled by _emit_command /
        # _emit_event_registration / _emit_task / _emit_button /
        # _emit_select / _emit_modal and assembled by _emit_bot_file.
        self._bot_command_methods: list[list[str]] = []
        self._bot_command_methods_by_name: dict[str, str] = {}
        self._bot_command_data: list[list[str]] = []
        self._bot_command_method_names: set[str] = set()
        self._bot_components: list[list[str]] = []
        self._bot_component_registrations: list[str] = []
        self._bot_component_kinds: set[str] = set()
        self._bot_component_names: set[str] = set()
        self._bot_event_methods: list[list[str]] = []
        self._bot_task_methods: list[list[str]] = []
        self._task_fn_names: dict[int, str] = {}
        self._task_fn_name_counts: dict[str, int] = {}
        # Whether the emitted class needs the scheduler field
        # (tasks and/or component timeouts) or the per-message
        # author map (any `only: author` component).
        self._uses_scheduler = False
        self._uses_author_map = False
        # Synthesized row structs, one per store table (name ->
        # StructDecl), emitted as nested classes by _emit_bot_file.
        self._table_rows: dict[str, A.StructDecl] = {}
        # Whether the file being emitted is the entry file (set by
        # _emit_file_set before _emit_one).
        self._is_main_file = True

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
        """The Java name for a newr name, before collision checks:
        Java keywords and emitter-reserved names get a trailing
        underscore (D20, Java twin)."""
        if name in _JAVA_KEYWORDS or name in _EMITTER_RESERVED:
            return name + "_"
        return name

    def _bind(self, name: str, type: Type | None = None,
              kind: str = "local", interp: str | None = None) -> _Binding:
        """Bind `name` in the current scope.

        Java has real block scope (`long x` in an `if` block does
        not clobber the outer name), so - unlike the Python emitter
        - shadowing an enclosing name needs NO renaming: the inner
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
        enclosing scopes - for function/lambda parameters, where
        shadowing an enclosing name is the correct semantics. Only
        the current function's used-name set is consulted, so two
        parameters mangling to the same Java name still get distinct
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
        mangling."""
        java = self._mangle(name)
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
        self.uses_discord = True
        self.discord_names.add(name)

    def _need_sqlite(self):
        self.uses_sqlite = True

    def _render_imports(self) -> list[str]:
        """The import block: java.util first (stdlib), then JDA
        (third-party), then the Xerial SQLite driver, then file
        modules (imported by their file stem - the class name IS
        the stem, K1). `java.util.*` is unconditional: emitted code
        uses `Objects.equals`, `Arrays.asList`, `ArrayList` and
        friends even in programs that use no runtime helper at all
        (conformance str_fstring_quotes compiled `Objects.equals`
        with no import in sight). The runtime is imported only when
        a helper is used (usage-tracked, Gleam-style - D4's Java
        twin); it lives in the same (default) package, so it needs
        no import line at all."""
        lines: list[str] = ["import java.util.*;"]
        for name in sorted(self._extra_imports):
            lines.append(f"import {name};")
        if self.uses_sqlite:
            lines.append("import java.sql.*;")
        # Discord names are fully-qualified class names (e.g.
        # net.dv8tion.jda.api.JDABuilder), one import line each.
        for name in sorted(self.discord_names):
            lines.append(f"import {name};")
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

    def _java_type(self, t: Type | None) -> str:
        """The Java type for a newr type in a declaration position
        (a local, parameter, field or return type). Primitives stay
        primitive; nullable value types and every generic argument
        use the boxed form. A `none` return is `void`; a `none`
        value is `Void` (only `null` inhabits it)."""
        if t is None:
            self._fail("an expression has no type: the emitter cannot "
                       "handle it here")
        if t.name == "int":
            return "Long" if t.nullable else "long"
        if t.name == "float":
            return "Double" if t.nullable else "double"
        if t.name == "bool":
            return "Boolean" if t.nullable else "boolean"
        if t.name == "str":
            return "String"
        if t.name == "none":
            return "Void"
        if t.name == "object":
            # The union type of conflicting enum variant fields.
            return "Object"
        if t.name == "error":
            return "NewrRt.NewrError"
        if t.name == "list":
            return f"List<{self._java_boxed_type(t.params[0])}>"
        if t.name == "dict":
            return ("LinkedHashMap<String, "
                    f"{self._java_boxed_type(t.params[1])}>")
        if t.name == "set":
            return f"HashSet<{self._java_boxed_type(t.params[0])}>"
        if t.name == "Result":
            return ("NewrRt.Result<"
                    f"{self._java_boxed_type(t.params[0])}, "
                    f"{self._java_boxed_type(t.params[1])}>")
        if t.name == "fn":
            return self._java_fn_type(t)
        if t.name == "duration":
            return "long"
        if t.name in ("user", "member", "guild", "channel", "role",
                      "interaction", "embed", "message"):
            return self._java_entity_type(t.name)
        # A struct or enum: the (possibly module-qualified) class.
        return self._struct_java_name(t.name)

    def _java_boxed_type(self, t: Type | None) -> str:
        """The type-argument form: always a reference type
        (`long` -> `Long`, `void` never appears here)."""
        if t is None:
            self._fail("an expression has no type: the emitter cannot "
                       "handle it here")
        if t.name == "int":
            return "Long"
        if t.name == "float":
            return "Double"
        if t.name == "bool":
            return "Boolean"
        if t.name == "none":
            return "Void"
        return self._java_type(t)

    def _java_fn_type(self, t: Type) -> str:
        """`fn(A, B) -> R` -> `NewrRt.Fn2<A, B, R>`. Arity 0–8
        (Fn0–Fn8 in the runtime); a fn value of higher arity is a
        clean E4001 - v1 limit, recorded in BUILD_REPORT.md §21."""
        params = t.params[:-1]
        ret = t.params[-1]
        n = len(params)
        if n > 8:
            self._fail(
                f"a function value with {n} parameters is not "
                "supported in the Java emitter (max 8)")
        args = ", ".join(self._java_boxed_type(p) for p in params)
        ret_s = self._java_boxed_type(ret)
        if args:
            return f"NewrRt.Fn{n}<{args}, {ret_s}>"
        return f"NewrRt.Fn0<{ret_s}>"

    def _java_entity_type(self, name: str) -> str:
        """A Discord entity type (SPEC §7.1) -> its JDA class.
        Only reachable in bot files."""
        mapping = {
            "user": "net.dv8tion.jda.api.entities.User",
            "member": "net.dv8tion.jda.api.entities.Member",
            "guild": "net.dv8tion.jda.api.entities.Guild",
            "channel": "net.dv8tion.jda.api.entities.channel.middleman.MessageChannel",
            "role": "net.dv8tion.jda.api.entities.Role",
            "interaction": "net.dv8tion.jda.api.interactions.Interaction",
            "embed": "net.dv8tion.jda.api.entities.MessageEmbed",
            "message": "net.dv8tion.jda.api.entities.Message",
        }
        fq = mapping[name]
        self._need_discord(fq)
        return fq.rsplit(".", 1)[-1]

    def _struct_java_name(self, name: str) -> str:
        """The Java class name for a struct/enum type: bare in its
        own file, `Module.Name` when imported (the binding records
        the module). An imported struct/enum reached only through
        its TYPE (a `let c = helper.Shade.Red` has type `Shade`
        with no module on the Type - the Type class has no module
        field) is found by searching the imported file modules for
        the declaration and qualifying with the module's class
        name - the file STEM, not the import alias (K1;
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
                    return f"{stem}.{self._mangle(name)}"
        return self._mangle(name)

    # ------------------------------------------------------------ expressions

    def emit_expr(self, node: A.Expr, min_prec: int = 0,
                  fn_target: Type | None = None) -> str:
        """Emit an expression, parenthesised when its precedence is
        below `min_prec`. `fn_target` is the expected fn type when
        the expression is a lambda - the context's target type, so
        the lambda needs no cast (Java lambdas are poly
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
                return (f"new ArrayList<{self._java_boxed_type(elem)}>()",
                        _PREC["primary"])
            elem = self._type_of(node).params[0]
            parts = ", ".join(
                self.emit_expr(i, 0,
                               fn_target=elem if _is_fn(elem) else None)
                for i in node.elements)
            # Arrays.asList (not List.of - List.of rejects null
            # elements, and a list[T?] may hold null) with an
            # explicit element-type witness, so a literal of only
            # nulls still has the right type.
            return (f"new ArrayList<>(Arrays.<"
                    f"{self._java_boxed_type(elem)}>asList({parts}))",
                    _PREC["primary"])
        if isinstance(node, A.DictLit):
            if not node.pairs:
                val = self._type_of(node).params[1]
                return ("new LinkedHashMap<String, "
                        f"{self._java_boxed_type(val)}>()",
                        _PREC["primary"])
            val = self._type_of(node).params[1]
            parts = []
            for k, v in node.pairs:
                parts.append(
                    f"{self.emit_expr(k, 0)}, "
                    f"{self.emit_expr(v, 0, fn_target=val if _is_fn(val) else None)}")
            # NewrRt.dictOf (not Map.ofEntries - that rejects null
            # keys and values, and a dict[str, T?] may hold null),
            # with an explicit value-type witness.
            return (f"NewrRt.<{self._java_boxed_type(val)}>"
                    f"dictOf({', '.join(parts)})",
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
            return f"NewrRt.tryResult(() -> {inner})", _PREC["primary"]
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
            # Long.MIN_VALUE has no positive literal in Java
            # (`9223372036854775808L` is a compile error); the
            # unary-minus path special-cases it, and a bare literal
            # that large is rejected by the checker first.
            if value > 9223372036854775807:
                self._fail(
                    "integer literal out of int64 range",
                    span=node.span)
            return f"{value}L", _PREC["primary"]
        if isinstance(value, float):
            return _java_float(value), _PREC["primary"]
        if isinstance(value, str):
            return _java_str(value), _PREC["primary"]
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
                    f"`{node.name}` is not a value - use a variant "
                    "or construct it", span=node.span)
            # A user fn as a value is the function itself (first-
            # class functions, SPEC §9.5); a local/param/handler
            # binding is its Java name. A module-level fn used as a
            # value is a method reference (`Main::fn`).
            if binding.kind == "fn":
                return f"{self.output_stem}::{binding.java}", \
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
        # generic ones (Object in, Long out).
        cast = self._java_fn_type(fn_target) if _is_fn(fn_target) else None
        if name == "len":
            self._need_rt("lenOf")
            form = cast or "NewrRt.Fn1<Object, Long>"
            return (f"(({form}) ((x) -> NewrRt.lenOf(x)))",
                    _PREC["primary"])
        if name == "min":
            form = cast or "NewrRt.Fn2<Long, Long, Long>"
            return (f"(({form}) ((a, b) -> a < b ? a : b))",
                    _PREC["primary"])
        if name == "max":
            form = cast or "NewrRt.Fn2<Long, Long, Long>"
            return (f"(({form}) ((a, b) -> a < b ? b : a))",
                    _PREC["primary"])
        if name == "abs":
            # int -> absLong, float -> Math.abs; the context type
            # decides (the fallback is the int form).
            if fn_target is not None and _is_fn(fn_target) and \
                    _is_float(fn_target.params[0]):
                form = cast or "NewrRt.Fn1<Double, Double>"
                return (f"(({form}) ((x) -> Math.abs(x)))",
                        _PREC["primary"])
            self._need_rt("absLong")
            form = cast or "NewrRt.Fn1<Long, Long>"
            return (f"(({form}) ((x) -> NewrRt.absLong(x)))",
                    _PREC["primary"])
        if name == "round":
            self._need_rt("roundHalfAway")
            return "NewrRt::roundHalfAway", _PREC["primary"]
        if name == "range":
            self._need_rt("range")
            return "NewrRt::range", _PREC["primary"]
        if name == "set":
            form = cast or \
                "NewrRt.Fn1<List<Object>, HashSet<Object>>"
            return (f"(({form}) ((xs) -> new HashSet<>(xs)))",
                    _PREC["primary"])
        if name == "ok":
            self._need_rt("ok")
            return "NewrRt::ok", _PREC["primary"]
        if name == "err":
            self._need_rt("err")
            return "NewrRt::err", _PREC["primary"]
        self._fail(f"builtin `{name}` is not a value")

    # ------------------------------------------------------------ interpolation

    def _emit_interp(self, node: A.Interp) -> tuple[str, int]:
        """A newr interpolation becomes Java string concatenation
        (`"a" + x + "b"`): literal parts are string literals, each
        expression part renders through `_emit_interp_part`."""
        parts: list[str] = []
        for part in node.parts:
            if part.text is not None:
                parts.append(_java_str(part.text))
            else:
                parts.append(self._emit_interp_part(part.expr))
        if not parts:
            return '""', _PREC["primary"]
        # A `+` chain starting with a non-string operand would be
        # ARITHMETIC (`{x}{y}` -> `x + y`), not concatenation - so
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
        `+` would be arithmetic - ints are the only exception
        (they stringify in a `+` chain)."""
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
            return f"NewrRt.toStr({self.emit_expr(expr, 0)})"
        if _is_float(t):
            self._need_rt("toStr")
            return f"NewrRt.toStr({self.emit_expr(expr, 0)})"
        if _is_int(t) or _is_str(t):
            return self.emit_expr(expr, 0)
        if t.nullable:
            self._need_rt("toStr")
            return f"NewrRt.toStr({self.emit_expr(expr, 0)})"
        if t.name in ("list", "dict", "set", "Result") or \
                t.name in self.structs or t.name in self.enums:
            self._need_rt("toStr")
            return f"NewrRt.toStr({self.emit_expr(expr, 0)})"
        return self.emit_expr(expr, 0)

    # ------------------------------------------------------------ operators

    def _emit_binop(self, node: A.BinOp) -> tuple[str, int]:
        op = node.op
        if op == "??":
            left = self.emit_expr(node.left, 0)
            right = self.emit_expr(node.right, 0)
            self._need_rt("coalesce")
            return (f"NewrRt.coalesce({left}, () -> {right})",
                    _PREC["primary"])
        if op in ("and", "or"):
            lt = self._type_of(node.left)
            if op == "or" and lt.nullable:
                # `T? or T` (SPEC §7.2): the default when the value
                # is absent. Java `||` would also take the default
                # for a present-but-falsy value (0L, ""), so it is
                # not the same - use coalesce.
                self._need_rt("coalesce")
                left = self.emit_expr(node.left, 0)
                right = self.emit_expr(node.right, 0)
                return (f"NewrRt.coalesce({left}, () -> {right})",
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
                return f"{right}.contains({left})", _PREC["primary"]
            if rt.name == "list":
                self._need_rt("listContains")
                right = self.emit_expr(node.right, _BINOP_PREC["<"] + 1)
                return (f"NewrRt.listContains({right}, {left})",
                        _PREC["primary"])
            if rt.name == "set":
                right = self.emit_expr(node.right, _BINOP_PREC["<"] + 1)
                return f"{right}.contains({left})", _PREC["primary"]
            if rt.name == "dict":
                right = self.emit_expr(node.right, _BINOP_PREC["<"] + 1)
                return f"{right}.containsKey({left})", _PREC["primary"]
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
                    # `null` as equal only to `null` - exactly
                    # newr's `==` for nullable collection values too.
                    self._need_rt("eq")
                    expr = f"NewrRt.eq({left}, {right})"
                    if op == "!=":
                        expr = f"!{expr}"
                    return expr, _PREC["primary"]
                # `==` on a boxed value is IDENTITY in Java (a
                # nullable long `1000L == 1000L` is false) and on a
                # String it is identity too - use null-safe
                # Objects.equals unless BOTH sides are non-nullable
                # primitives, where bare `==` is correct.
                if lt.nullable or rt.nullable or \
                        _is_str(lt) or _is_str(rt) or \
                        _is_error(lt) or _is_error(rt) or _is_fn(lt) or \
                        _is_fn(rt):
                    expr = f"Objects.equals({left}, {right})"
                    if op == "!=":
                        expr = f"!{expr}"
                    return expr, _PREC["primary"]
                jsop = "==" if op == "==" else "!="
                return f"{left} {jsop} {right}", prec
            # Ordering: strings compare lexicographically
            # (compareTo); numbers compare bare (autounboxing a
            # boxed operand would NPE on null, but the checker only
            # allows ordering on non-nullable values).
            if _is_str(lt) or _is_str(rt):
                cmp = {"<": "< 0", "<=": "<= 0", ">": "> 0",
                       ">=": ">= 0"}[op]
                return f"({left}.compareTo({right}) {cmp})", prec
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
            helper = {"+": "add", "-": "sub", "*": "mul", "/": "tdiv",
                      "%": "tmod", "**": "powInt"}[op]
            self._need_rt(helper)
            return f"NewrRt.{helper}({left}, {right})", _PREC["primary"]
        if op == "%" and _is_float(result):
            self._need_rt("fmod")
            return f"NewrRt.fmod({left}, {right})", _PREC["primary"]
        if op == "/" and _is_float(result):
            # P6: float /0 is an error (Java `/` would give Infinity).
            self._need_rt("fdiv")
            return f"NewrRt.fdiv({left}, {right})", _PREC["primary"]
        if op == "**" and _is_float(result):
            self._need_rt("powFloat")
            return f"NewrRt.powFloat({left}, {right})", _PREC["primary"]
        return f"{left} {op} {right}", prec

    def _emit_unary(self, node: A.UnaryOp) -> tuple[str, int]:
        if node.op == "not":
            # Java `!` binds tighter than newr's `not` (which is
            # looser than `==`): `!a == b` is `(!a) == b` in Java.
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
        # the checker leaves the literal's own type unset - the
        # plain `neg(...)` form would also be wrong (it would check
        # the out-of-range literal first). Emit the minimum directly.
        if isinstance(node.operand, A.Lit) and \
                node.operand.value == 9223372036854775808:
            return "Long.MIN_VALUE", _PREC["unary"]
        t = self._type_of(node.operand)
        if _is_int(t):
            self._need_rt("neg")
            return f"NewrRt.neg({self.emit_expr(node.operand, 0)})", \
                _PREC["primary"]
        operand = self.emit_expr(node.operand, _PREC["pow"])
        # Java traps: `--x` is the decrement operator -
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
                if binding is None and obj.name in MODULES:
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
            # `m_helper.Shape.Circle(radius: 1.5)` - the declaration
            # comes from the imported module, for field reordering.
            imported = self._imported_enum(func)
            if imported is not None:
                ref, enum_name, variant, decl = imported
                return self._emit_enum_call(enum_name, variant, node,
                                            decl=decl, ref=ref)
            return self._emit_method_call(node, func)
        # A call on an arbitrary expression (a lambda, a call result):
        # the callee's fn type decides the invoke method (Fn0.get,
        # Fn1.apply, ...).
        ftype = self._type_of(func)
        arity = len(ftype.params) - 1 if _is_fn(ftype) else 0
        method = "get" if arity == 0 else "apply"
        f = self.emit_expr(func, _PREC["primary"],
                           fn_target=ftype if _is_fn(ftype) else None)
        args = [self.emit_expr(a, 0) for a in node.args]
        return f"{f}.{method}({', '.join(args)})", _PREC["primary"]

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
            return f"NewrRt.input({', '.join(args)})", _PREC["primary"]
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
            return f"NewrRt.roundHalfAway({', '.join(args)})", \
                _PREC["primary"]
        if name == "range":
            self._need_rt("range")
            args = [self.emit_expr(a, 0) for a in node.args]
            return f"NewrRt.range({', '.join(args)})", _PREC["primary"]
        if name == "set":
            arg = self.emit_expr(node.args[0], 0)
            return f"new HashSet<>({arg})", _PREC["primary"]
        if name == "ok":
            self._need_rt("ok")
            t = self._type_of(node)
            # The runtime's ok is `ok<T>(T value)` - ONE type
            # parameter (the error type is fixed to NewrError);
            # emitting two type arguments does not compile
            # (conformance opt_methods).
            return (f"NewrRt.<{self._java_boxed_type(t.params[0])}>ok("
                    f"{self.emit_expr(node.args[0], 0)})",
                    _PREC["primary"])
        if name == "err":
            self._need_rt("err")
            t = self._type_of(node)
            return (f"NewrRt.<{self._java_boxed_type(t.params[0])}>err("
                    f"{self.emit_expr(node.args[0], 0)})",
                    _PREC["primary"])
        binding = self._lookup(name)
        if binding is not None and binding.kind == "struct":
            # Struct construction: Java has no kwargs, so keyword
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
            if binding.kind != "fn" and _is_fn(binding.type):
                # A fn VALUE (a local, parameter or top-level let
                # holding a lambda or another fn): invoke through
                # the FnN interface. A bare `f(x)` would look for
                # a static method named f and does not compile
                # (conformance fn_as_value, fn_lambda_block,
                # fn_lambda_capture, method_as_value). A declared
                # fn (kind "fn") is a static method and takes the
                # plain call below.
                args = [self.emit_expr(a, 0) for a in node.args]
                method = "get" if not args else "apply"
                return (f"{binding.java}.{method}({', '.join(args)})",
                        _PREC["primary"])
            args = [self.emit_expr(a, 0) for a in node.args]
            kwargs = self._emit_kwargs(node, binding)
            return (f"{binding.java}({', '.join(args + kwargs)})",
                    _PREC["primary"])
        self._fail(f"unknown function `{name}`")

    def _emit_kwargs(self, node: A.Call, binding: _Binding) -> list[str]:
        """Keyword arguments for a user-fn call: Java has no kwargs,
        so they are reordered into position (the checker already
        validated names and types)."""
        if not node.kwargs:
            return []
        fn = self.fns_by_java.get(binding.java)
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
        """`print` renders every argument with `toStr` (P8) - the
        runtime `print` helper does that mapping internally, so the
        emitted call is bare. A `?` argument propagates first (the
        temp + early return are hoisted before the statement, D7).
        When every argument is already a Java string (or there are
        no arguments), `System.out.println` prints them exactly as
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
                return "System.out.println()", _PREC["primary"]
            return (f"System.out.println({' + \" \" + '.join(parts)})",
                    _PREC["primary"])
        self._need_rt("print")
        return f"NewrRt.print({', '.join(parts)})", _PREC["primary"]

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
                return f"(long) NewrRt.chars({arg}).size()", \
                    _PREC["primary"]
            if t.name in ("list", "dict", "set"):
                return f"(long) {arg}.size()", _PREC["primary"]
            return f"(long) {arg}.size()", _PREC["primary"]
        if name == "min":
            a = self.emit_expr(args[0], 0)
            b = self.emit_expr(args[1], 0)
            t = self._type_of(args[0])
            if _is_str(t):
                return (f"({a}.compareTo({b}) < 0 ? {a} : {b})",
                        _PREC["primary"])
            return f"({a} < {b} ? {a} : {b})", _PREC["primary"]
        if name == "max":
            a = self.emit_expr(args[0], 0)
            b = self.emit_expr(args[1], 0)
            t = self._type_of(args[0])
            if _is_str(t):
                return (f"({a}.compareTo({b}) < 0 ? {b} : {a})",
                        _PREC["primary"])
            return f"({a} < {b} ? {b} : {a})", _PREC["primary"]
        if name == "abs":
            t = self._type_of(args[0])
            arg = self.emit_expr(args[0], 0)
            if _is_int(t):
                self._need_rt("absLong")
                return f"NewrRt.absLong({arg})", _PREC["primary"]
            return f"Math.abs({arg})", _PREC["primary"]
        self._fail(f"internal error: cannot emit builtin `{name}`")

    def _emit_conversion(self, node: A.Call) -> tuple[str, int]:
        name = node.func.name  # str | int | float | bool
        arg = node.args[0]
        t = self._type_of(arg)
        text = self.emit_expr(arg, 0)
        if name == "str":
            # A nullable str's runtime value is null when absent -
            # bare rendering would print "null", not newr's "none"
            # (P8), so nullable values go through toStr.
            if _is_str(t) and not t.nullable:
                return text, _PREC["primary"]
            self._need_rt("toStr")
            return f"NewrRt.toStr({text})", _PREC["primary"]
        if name == "int":
            if _is_int(t):
                return text, _PREC["primary"]
            # float (truncation), str (parsing), bool (0/1) - all
            # through toInt, which returns a long in every case.
            self._need_rt("toInt")
            return f"NewrRt.toInt({text})", _PREC["primary"]
        if name == "float":
            if _is_float(t):
                return text, _PREC["primary"]
            if _is_str(t):
                self._need_rt("toFloat")
                return f"NewrRt.toFloat({text})", _PREC["primary"]
            if _is_int(t):
                return f"(double) {text}", _PREC["primary"]
            return f"({text} ? 1.0 : 0.0)", _PREC["primary"]  # bool
        # bool
        if _is_bool(t):
            return text, _PREC["primary"]
        if _is_str(t):
            self._need_rt("toBool")
            return f"NewrRt.toBool({text})", _PREC["primary"]
        if _is_int(t):
            return f"({text} != 0L)", _PREC["primary"]
        return f"({text} != 0.0)", _PREC["primary"]  # float

    def _emit_enum_call(self, enum_name: str, variant: str,
                        node: A.Call, decl=None,
                        ref: str | None = None) -> tuple[str, int]:
        """`Shape.Circle(radius: 1.5)` -> the variant factory
        method; a bare variant is the static final instance
        (`Color.Red`). Java has no kwargs, so keyword arguments are
        reordered into declared field order (the checker already
        validated them). `decl` / `ref` serve the §20 qualified
        imported form (`m_helper.Shape.Circle(...)`): the
        declaration comes from the imported module and the class
        lives under the module reference; the local form takes both
        from this file."""
        prefix = (f"{ref}." if ref else "") + self._mangle(enum_name)
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
        """`log.info/warn/error(msg)` (P10, J12's Java twin):
        info -> stdout, warn/error -> stderr, no timestamp."""
        msg = self.emit_expr(node.args[0], 0)
        if level == "info":
            return f"System.out.println({msg})", _PREC["primary"]
        return f"System.err.println({msg})", _PREC["primary"]

    def _file_module_class(self, binding: _Binding) -> str:
        """The Java class name for a file-module import: the file
        STEM, not the import alias (K1 - one public class per file,
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
        keyword arguments into declared field order (Java has no
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
        access needs a cast to the field's declared (boxed) type -
        otherwise a `long` field read as Object would not compile
        where a long is expected."""
        decl = self._find_enum_decl(enum_name)
        if decl is not None and field_type is not None:
            union = self._enum_union_fields(decl)
            utype = union.get(field_name)
            if utype is not None and utype.name == "object":
                return (f"({self._java_boxed_type(field_type)}) "
                        f"{expr_text}.{self._mangle(field_name)}")
        return f"{expr_text}.{self._mangle(field_name)}"

    # Discord entity fields (SPEC §7.3, ENTITY_FIELDS in
    # discord_limits) -> the JDA getter for that field. All names
    # verified live against the JDA 6.7.0 jar with javap.
    _ENTITY_GETTERS: dict[str, dict[str, str]] = {
        "user": {"id": "getIdLong()", "mention": "getAsMention()",
                 "name": "getName()", "bot": "isBot()"},
        "member": {"id": "getIdLong()", "mention": "getAsMention()",
                   "name": "getUser().getName()", "guild": "getGuild()"},
        "guild": {"id": "getIdLong()", "name": "getName()",
                  "member_count": "getMemberCount()"},
        "channel": {"id": "getIdLong()", "name": "getName()",
                    "mention": "getAsMention()"},
        "role": {"id": "getIdLong()", "name": "getName()"},
        "interaction": {"user": "getUser()", "guild": "getGuild()",
                        "channel": "getChannel()",
                        "locale": "getUserLocale().getLocale()"},
        "message": {"content": "getContentRaw()", "author": "getAuthor()",
                    "channel": "getChannel()"},
    }

    def _entity_field_expr(self, obj_text: str, obj_type: Type,
                           field: str) -> str | None:
        """The Java expression for a Discord entity field, or None
        when the type is not an entity type (the ordinary struct
        field path then applies)."""
        table = self._ENTITY_GETTERS.get(obj_type.name)
        if table is None or field not in table:
            return None
        if obj_type.name == "interaction" and field == "channel":
            # Interaction.getChannel() returns Channel; the newr
            # type is MessageChannel (the type a `send ... to`
            # target and the channel binding have).
            self._need_discord(
                "net.dv8tion.jda.api.entities.channel.middleman.MessageChannel")
            return f"((MessageChannel) {obj_text}.getChannel())"
        return f"{obj_text}.{table[field]}"

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
            return (f"{ref}.{self._mangle(enum_name)}."
                    f"{self._mangle(variant)}", _PREC["primary"])
        obj_type = self._type_of(obj)
        t = self._type_of(node)
        # The newr `error` type has exactly two fields (checker
        # ERROR_FIELDS): message (str) and code (str?). Java's
        # NewrError keeps `code` as a public field, but `message`
        # lives in Throwable and is only reachable via getMessage()
        # (the field is private) - a bare `.message` does not
        # compile (conformance try_catch).
        if obj_type.name == "error":
            obj_text = self.emit_expr(obj, _PREC["primary"])
            if node.name == "message":
                return f"{obj_text}.getMessage()", _PREC["primary"]
            return f"{obj_text}.{self._mangle(node.name)}", \
                _PREC["primary"]
        # §16 (hole 2): a method used as a value is a lambda, not a
        # Java attribute - a Long has no `is_some`, an ArrayList has
        # no `len`. A real struct field stays a plain attribute.
        if t.name == "fn" and not self._is_plain_field(obj_type,
                                                       node.name):
            sig = self._method_value_sig(obj_type, node.name)
            if sig is not None:
                return self._emit_method_value(node, obj_type, sig,
                                               fn_target)
        obj_text = self.emit_expr(obj, _PREC["primary"])
        # Discord entity fields (SPEC §7.3) -> JDA getters. The
        # checker types these fields (ENTITY_FIELDS in
        # discord_limits); Java has no `.id` field on a User - the
        # snowflake is getIdLong() (newr `id` is int, so the String
        # getId() would be the wrong type). `interaction.channel`
        # needs a cast: Interaction.getChannel() returns Channel,
        # but the newr type is MessageChannel.
        entity = self._entity_field_expr(obj_text, obj_type, node.name)
        if entity is not None:
            return entity, _PREC["primary"]
        # An enum field whose variants disagree on the type reads
        # through the Object union field - cast to the declared type.
        if self._find_enum_decl(obj_type.name) is not None:
            return (self._enum_field_access(obj_text, obj_type.name,
                                           node.name, t),
                    _PREC["primary"])
        return f"{obj_text}.{self._mangle(node.name)}", _PREC["primary"]

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
        snap_type = self._java_type(obj_type)
        self._hoists_add([f"final {snap_type} {snap_name} = {obj_text};"])
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
        arrow = f"({arg_names}) -> {body}"
        cast = fn_target if _is_fn(fn_target) else self._type_of(node)
        cast_text = self._java_fn_type(cast)
        if hoists:
            # A hoisted block-body lambda: the hoisted const's
            # initializer is the lambda, and the hoists run inside
            # the arrow body at call time.
            name = self._new_lambda_name()
            lines = [f"final {cast_text} {name} = ({arg_names}) -> {{"]
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
                self._need_rt("unwrapOpt")
                return f"NewrRt.unwrapOpt({obj_text})", _PREC["primary"]
            self._fail(f"`{obj_type}` has no method `{name}`")
        tname = obj_type.name
        if tname == "str":
            if name == "upper":
                return f"{obj_text}.toUpperCase()", _PREC["primary"]
            if name == "lower":
                return f"{obj_text}.toLowerCase()", _PREC["primary"]
            if name == "strip":
                # Java's strip() (not trim()) removes Unicode
                # whitespace, matching Python's str.strip().
                return f"{obj_text}.strip()", _PREC["primary"]
            if name == "split":
                self._need_rt("splitStr")
                return f"NewrRt.splitStr({obj_text}, {args[0]})", \
                    _PREC["primary"]
            if name == "join":
                # newr `"sep".join(parts)` - Java joins the LIST
                # with the separator.
                return f"String.join({obj_text}, {args[0]})", \
                    _PREC["primary"]
            if name == "contains":
                return f"{obj_text}.contains({args[0]})", _PREC["primary"]
            if name == "starts_with":
                return f"{obj_text}.startsWith({args[0]})", \
                    _PREC["primary"]
            if name == "ends_with":
                return f"{obj_text}.endsWith({args[0]})", _PREC["primary"]
            if name == "chars":
                # SPEC pin P5: code points, not UTF-16 code units.
                self._need_rt("chars")
                return f"NewrRt.chars({obj_text})", _PREC["primary"]
        elif tname == "list":
            if name == "append":
                return f"{obj_text}.add({args[0]})", _PREC["primary"]
            if name == "contains":
                self._need_rt("listContains")
                return f"NewrRt.listContains({obj_text}, {args[0]})", \
                    _PREC["primary"]
            if name == "get":
                self._need_rt("listGet")
                # listGet returns Object (the runtime is untyped
                # for lists) - cast to the element type, which is
                # the call's own type (modules.py: list.get
                # returns T). The checker guarantees two
                # arguments (index, default); a one-argument
                # `list.get` is rejected there, so there is no
                # two-argument runtime overload to call.
                t = self._type_of(node)
                return (f"(({self._java_boxed_type(t)}) "
                        f"NewrRt.listGet({obj_text}, {args[0]}, "
                        f"{args[1]}))", _PREC["primary"])
            if name == "len":
                return f"(long) {obj_text}.size()", _PREC["primary"]
        elif tname == "dict":
            if name == "keys":
                return f"new ArrayList<>({obj_text}.keySet())", \
                    _PREC["primary"]
            if name == "get":
                if len(args) == 2:
                    return (f"({obj_text}.containsKey({args[0]}) ? "
                            f"{obj_text}.get({args[0]}) : {args[1]})",
                            _PREC["primary"])
                return f"{obj_text}.get({args[0]})", _PREC["primary"]
            if name == "contains":
                return f"{obj_text}.containsKey({args[0]})", \
                    _PREC["primary"]
        elif tname == "set":
            if name == "add":
                return f"{obj_text}.add({args[0]})", _PREC["primary"]
            if name == "remove":
                self._need_rt("setRemove")
                return f"NewrRt.setRemove({obj_text}, {args[0]})", \
                    _PREC["primary"]
            if name == "contains":
                return f"{obj_text}.contains({args[0]})", _PREC["primary"]
        elif tname == "Result":
            if name in ("is_ok", "is_err"):
                # The Java Result exposes isOk/isErr as METHODS
                # (newr_rt.java) - a bare access would be the
                # method itself, not the bool.
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
            # form - see _emit_method_call.) listIndex returns
            # Object, so cast to the element's boxed type - a bare
            # result cannot be assigned to a `long` or compared
            # with `!=` (conformance list_index_error, list_ops,
            # nullable_propagate).
            elem = self._java_boxed_type(obj_type.params[0])
            return (f"(({elem}) NewrRt.listIndex({obj_text}, {idx}))",
                    _PREC["primary"])
        if obj_type.name == "str":
            # newr indexes a string by code point (P5); a Java
            # string index is a UTF-16 code unit.
            self._need_rt("strIndex")
            return f"NewrRt.strIndex({obj_text}, {idx})", _PREC["primary"]
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
        """`?.` lowering (D9, corrected - the Java twin of the JS
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
            # the lookup returns the binding's Java name (which may
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
            cur_text = f"NewrRt.optChain({cur_text}, {body})"
            i = j
        return cur_text, _PREC["primary"]

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
            return f"({param_java}) -> {body}"
        name = self._new_lambda_name()
        fn_type = ("java.util.function.Function<"
                   + self._java_boxed_type(param_type)
                   + ", " + self._java_boxed_type(body_type) + ">")
        lines = [f"final {fn_type} {name} = ({param_java}) -> {{"]
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

    def _emit_lambda(self, node: A.Lambda,
                     fn_target: Type | None = None) -> tuple[str, int]:
        """Closures capture by value (D10, J8): free variables are
        snapshotted into hoisted `final` locals before the enclosing
        statement - a Java lambda may only capture effectively-final
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
            snap_type = self._java_type(b.type)
            self._hoists_add([f"final {snap_type} {snap} = {b.value};"])
            self._scopes[-1][name] = _Binding(java=snap, type=b.type,
                                              kind="param")
        params_text = ", ".join(b.java for b in param_bindings)
        fn_type = self._type_of(node)
        cast = fn_target if _is_fn(fn_target) else fn_type
        cast_text = self._java_fn_type(cast)
        if node.expr_body is not None:
            body, hoists = self._emit_expr_with_hoists(node.expr_body)
            self._fn_used = saved_used
            self._pop_scope()
            if not hoists:
                arrow = f"({params_text}) -> {body}"
                if fn_target is not None and _is_fn(fn_target):
                    return arrow, _PREC["primary"]
                return f"(({cast_text}) ({arrow}))", _PREC["primary"]
            # The body needed hoists (a nested block-body lambda):
            # the hoisted const's initializer IS the lambda, so the
            # snapshot locals are in scope inside the arrow body at
            # call time.
            name = self._new_lambda_name()
            lines = [f"final {cast_text} {name} = ({params_text}) -> {{"]
            for h in hoists:
                lines.append("  " + h)
            lines.append(f"  return {body};")
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
        lines = [f"final {cast_text} {name} = ({params_text}) -> {{"]
        lines.extend(body_lines if body_lines else ["  ;"])
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
        file (main() / the module static block) - see
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
                    # BEFORE the answer they attach to - the answer
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
                # main() (or the module's static block) - a field
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
            keyword = "final " if isinstance(stmt, A.Const) else ""
            return [f"{p}{keyword}{self._java_type(t)} "
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
            helper = {"+=": "add", "-=": "sub", "*=": "mul",
                      "/=": "tdiv"}[stmt.op]
            self._need_rt(helper)
            return [f"{p}{binding.java} = NewrRt.{helper}({binding.java}, "
                    f"{value});"]
        # float / str / %= : bare augmented assignment (no overflow
        # check applies; `+=` on a str is concatenation; `%` on an
        # int cannot overflow - Long.MIN_VALUE % -1 is 0 in Java).
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
        # (it cannot see the loop variable - `for x in x` reads the
        # enclosing `x`).
        iterable = self.emit_expr(stmt.iterable, 0)
        self._push_scope()
        binding = self._bind(stmt.var, type=elem, kind="local")
        elem_text = self._java_type(elem)
        if it_type.name == "dict":
            # A dict iterates its keys (insertion order, like
            # Python's `for k in d`).
            header = (f"for ({elem_text} {binding.java} : "
                      f"{iterable}.keySet()) {{")
        elif it_type.name == "str":
            # A string iterates code points (P5), not UTF-16 code
            # units.
            self._need_rt("chars")
            header = (f"for ({elem_text} {binding.java} : "
                      f"NewrRt.chars({iterable})) {{")
        else:
            header = (f"for ({elem_text} {binding.java} : "
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
            lines.append(f"{p}}} catch (Throwable {exc_var}) {{")
            self._push_scope()
            binding = self._bind(cname, type=Type("error"), kind="local")
            self._need_rt("asError")
            lines.append(f"{p}  final NewrRt.NewrError {binding.java} = "
                         f"NewrRt.asError({exc_var});")
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
                return [f"{p}System.exit(0);"]
            self._fail("a top-level `return` with a value is not "
                       "supported - a top-level `return` exits the "
                       "program")
        ensures = ctx["ensures"]
        ret_type = ctx["return_type"]
        is_void = ret_type is None or _is_none(ret_type)
        if stmt.value is None:
            if not ensures:
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
                f"{_java_str('ensures failed: ' + msg)});")
            lines.append(f"{p}}}")
        return lines

    # ------------------------------------------------------------ log statement

    def _emit_log_stmt(self, stmt: A.Log, indent: int) -> list[str]:
        """`log <expr>` is info level (P10, J12's Java twin:
        info -> stdout)."""
        p = "  " * indent
        if isinstance(stmt.value, A.Propagate):
            value = self._propagate_unwrapped(stmt.value)
        else:
            value = self.emit_expr(stmt.value, 0)
        self._used_log = True
        return [f"{p}System.out.println({value});"]

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
        lines = [f"{p}final {self._java_type(scrut_type)} {temp} = "
                 f"{scrut};"]
        arms = []
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
                        java=expr_text, type=typ, kind="local")
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
                # The binding's type is known from the pattern in
                # almost every case; when it is not (an imported
                # enum's field the emitter could not resolve), `var`
                # infers it from the field access.
                type_text = self._java_type(typ) if typ is not None \
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
        """Compile a pattern against the Java expression `expr_text`
        (usually the match temp) and the newr type `typ` of the value
        being matched. Returns (condition_text_or_None, bindings)
        where bindings is a list of (newr name, Java expression text,
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
                return (f"Objects.equals({expr_text}, "
                        f"{'true' if v else 'false'})", [])
            if isinstance(v, int):
                return f"Objects.equals({expr_text}, {v}L)", []
            if isinstance(v, float):
                return (f"Objects.equals({expr_text}, "
                        f"{_java_float(v)})", [])
            return f"Objects.equals({expr_text}, {_java_str(v)})", []
        if isinstance(pattern, A.EnumPattern):
            # The enum is ONE class; the variant is its discriminant
            # field `_newr_variant` (a null scrutinee matches no
            # variant - the `none` pattern handles it).
            cond = (f"({expr_text} != null && {expr_text}._newr_variant"
                    f".equals({_java_str(pattern.variant)}))")
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
            struct_java = self._struct_java_name(pattern.struct_name)
            cond = f"{expr_text} instanceof {struct_java}"
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
            cond = (f"({expr_text} instanceof ArrayList && "
                    f"{expr_text}.size() == {len(pattern.elements)})")
            bindings = []
            elem = typ.params[0] if typ is not None and typ.params else None
            for i, sub in enumerate(pattern.elements):
                sub_cond, sub_bind = self._pattern_condition(
                    sub, f"{expr_text}.get({i})", elem)
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
        temp_type = self._java_type(t)
        if ctx.get("in_handler"):
            # A handler returns nothing, so `?` just exits the
            # handler (a bare `return`) - there is no value to
            # propagate. (A handler that may exit early without
            # answering is the discord checker's E2010, not the
            # emitter's problem.)
            if t.name == "Result" and t.params and not t.nullable:
                self._hoists_add([
                    f"final {temp_type} {temp} = {text};",
                    # §18 fix 1 (JS twin): isErr is a METHOD on the
                    # Result - a bare access is the method itself
                    # (always truthy), so `?` early-returned on
                    # every value. The unwrapped value lives in
                    # `._value` (there is no `.value` field).
                    f"if ({temp}.isErr()) {{",
                    "  return;",
                    "}",
                ])
                return f"{temp}._value"
            if t.nullable and t.name != "Result":
                self._hoists_add([
                    f"final {temp_type} {temp} = {text};",
                    f"if ({temp} == null) {{",
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
            self._hoists_add([
                f"final {temp_type} {temp} = {text};",
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
            self._hoists_add([
                f"final {temp_type} {temp} = {text};",
                f"if ({temp} == null) {{",
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
            # the imported module - and the import itself (K1: one
            # `.java` per imported `.newr`, named by its module
            # name). Struct construction through a file module needs
            # `new` (a Java class is not callable without it).
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
                        return (f"new {ref}.{self._mangle(name)}"
                                f"({', '.join(parts)})", _PREC["primary"])
            args = [self.emit_expr(a, 0) for a in node.args]
            kwargs = self._emit_module_kwargs(node, sub)
            return (f"{ref}.{self._mangle(name)}"
                    f"({', '.join(args + kwargs)})", _PREC["primary"])
        real = binding.module or binding.java
        if real == "file":
            name = node.func.name
            if name in ("read", "write", "append"):
                self._need_rt(f"file{name.capitalize()}")
                args = [self.emit_expr(a, 0) for a in node.args]
                return (f"NewrRt.file{name.capitalize()}"
                        f"({', '.join(args)})", _PREC["primary"])
            if name == "exists":
                self._need_rt("fileExists")
                return (f"NewrRt.fileExists"
                        f"({self.emit_expr(node.args[0], 0)})",
                        _PREC["primary"])
            self._fail(f"module `file` has no function `{name}`")
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
        """The Java call text for a named-module function. `real` is
        the module name (for dispatch); `ref` is the Java reference
        (the alias, when the import is aliased)."""
        if real == "math":
            if name == "sqrt":
                return f"Math.sqrt({args[0]})", _PREC["primary"]
            if name == "floor":
                self._need_rt("toInt")
                return f"NewrRt.toInt(Math.floor({args[0]}))", \
                    _PREC["primary"]
            if name == "ceil":
                self._need_rt("toInt")
                return f"NewrRt.toInt(Math.ceil({args[0]}))", \
                    _PREC["primary"]
        if real == "time":
            if name == "now":
                self._need_rt("checkInt")
                return ("NewrRt.checkInt(System.currentTimeMillis()"
                        " / 1000L)", _PREC["primary"])
            if name == "format":
                self._need_rt("timeFormat")
                return f"NewrRt.timeFormat({args[0]}, {args[1]})", \
                    _PREC["primary"]
        if real == "random":
            if name == "int":
                self._need_rt("randomInt")
                return f"NewrRt.randomInt({args[0]}, {args[1]})", \
                    _PREC["primary"]
            if name == "choice":
                self._need_rt("randomChoice")
                return f"NewrRt.randomChoice({args[0]})", _PREC["primary"]
            if name == "shuffle":
                self._need_rt("randomShuffle")
                return f"NewrRt.randomShuffle({args[0]})", \
                    _PREC["primary"]
        if real == "env":
            if name == "get":
                self._need_rt("envGet")
                if len(args) == 2:
                    return (f"NewrRt.envGet({args[0]}, {args[1]})",
                            _PREC["primary"])
                return f"NewrRt.envGet({args[0]})", _PREC["primary"]
        self._fail(f"module `{real}` has no function `{name}`")

    def _emit_module_member(self, node: A.Member,
                            binding: _Binding) -> tuple[str, int]:
        """A named-module member used as a value: a constant, or a
        function (which becomes a lambda, D29). `binding.module` is
        the real module name (for dispatch); `binding.java` is the
        Java reference (the alias, when the import is aliased) -
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
                cast_text = self._java_fn_type(t)
                return (f"(({cast_text}) {ref}::"
                        f"{self._mangle(node.name)})",
                        _PREC["primary"])
            # A module function as a first-class value.
            if real == "file" and node.name in ("read", "write", "append"):
                self._fail(f"`file.{node.name}` cannot be used as a "
                           "value (it needs a statement - the `with` "
                           "block cannot live inside a lambda)")
            params = t.params[:-1]  # the last param is the return type
            names = [f"_a{i}" for i in range(len(params))]
            call, _prec = self._emit_module_call_text(
                real, ref, node.name, names)
            cast_text = self._java_fn_type(t)
            if names:
                return (f"(({cast_text}) "
                        f"({', '.join(names)}) -> {call})",
                        _PREC["primary"])
            return f"(({cast_text}) () -> {call})", _PREC["primary"]
        if real == "env" and node.name == "args":
            self._need_rt("envArgs")
            return "NewrRt.envArgs()", _PREC["primary"]
        if real == "math" and node.name in ("pi", "e"):
            return ("Math.PI" if node.name == "pi" else "Math.E",
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
            return f"{text}.getId()"
        if t.name == "str":
            return text
        return f"String.valueOf({text})"

    def _emit_store_call(self, node: A.Call,
                         binding: _Binding) -> tuple[str, int]:
        """`economy.balance(user)` etc. -> `bot.<store>_<op>(...)`
        (D17/P11, J9's Java twin). Every mutation takes its
        idempotency `key:`; the checker enforces that it is present.
        JDBC is synchronous, so there is no `await` - but the §19
        guard still applies: the `bot` instance only exists inside
        a handler, so a store call at the top level, inside a
        (sync) top-level fn, or inside a lambda is a clean E4001
        instead of uncompilable code."""
        # The Bot method is `bot.<store>_<op>` with the RAW store
        # name (the JS/Python spelling - and the name the Bot
        # class's methods use). binding.java is the MANGLED name
        # ("economy" is emitter-reserved, so it would be
        # "economy_") - using it here would call a method that
        # does not exist.
        store_name = (node.func.obj.name
                      if isinstance(node.func, A.Member)
                      and isinstance(node.func.obj, A.Name)
                      else binding.java)
        method = node.func.name
        ctx = self._fn_ctx[-1] if self._fn_ctx else None
        if ctx is None or not ctx.get("in_handler"):
            self._fail(
                f"a store call (`{store_name}.{method}`) is only "
                "possible inside a command, event, or task handler - "
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
            # The Bot method returns Object (one method serves all
            # tables); the checker knows the row type, so cast to
            # the row class - registered in the module scope by
            # _emit_bot_file (the row class IS the table name).
            binding = self._lookup(table.value)
            if binding is None or binding.kind != "struct":
                self._fail(f"unknown table `{table.value}`")
            return (f"({binding.java}) bot.{store_name}_get("
                    f"{self.emit_expr(table, 0)}, "
                    f"{self.emit_expr(node.args[1], 0)})",
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
        (D16, J10's Java twin). Template placeholders that are
        handler bindings (user, guild, ...) are auto-passed in their
        interpolation form; the rest must be explicit arguments (the
        checker enforces it - E2009). The kwargs map is built with
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
            kwargs.append(f"{_java_str(name)}, {self.emit_expr(value, 0)}")
        for placeholder in entry.placeholders:
            if placeholder in explicit:
                continue
            binding = self._lookup(placeholder)
            if binding is not None and binding.kind == "handler":
                kwargs.append(f"{_java_str(placeholder)}, "
                              f"{binding.interp_form()}")
                continue
            self._fail(f"`t(\"{node.key}\")` needs an argument for "
                       f"placeholder `{placeholder}` (the checker "
                       "should have rejected it)")
        self._need_rt("dictOf")
        args = ", ".join([_java_str(node.key), locale,
                          "NewrRt.<Object>dictOf("
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
        """Build a MessageEmbed on the lines before the send call
        (the survey verified setTitle / setDescription / addField /
        setColor in the track). Returns the lines and the embed
        variable's name (a fresh temp per embed)."""
        p = "  " * indent
        builder = self._new_temp_name()
        name = self._new_temp_name()
        self._need_discord("net.dv8tion.jda.api.EmbedBuilder")
        self._need_discord("net.dv8tion.jda.api.entities.MessageEmbed")
        lines = [f"{p}EmbedBuilder {builder} = new EmbedBuilder();"]
        for field in node.fields:
            if field.kind == "title":
                lines.append(f"{p}{builder}.setTitle("
                             f"{self.emit_expr(field.value, 0)});")
            elif field.kind == "description":
                lines.append(f"{p}{builder}.setDescription("
                             f"{self.emit_expr(field.value, 0)});")
            elif field.kind == "color":
                lines.append(f"{p}{builder}.setColor("
                             f"{self.emit_expr(field.value, 0)});")
            elif field.kind == "field":
                inline = "true" if field.inline else "false"
                # A field's name is a plain string (not an expression).
                lines.append(f"{p}{builder}.addField("
                             f"{_java_str(field.name)}, "
                             f"{self.emit_expr(field.value, 0)}, "
                             f"{inline});")
        lines.append(f"{p}MessageEmbed {name} = {builder}.build();")
        return lines, name

    # ------------------------------------------------------------ functions

    def _emit_fn(self, decl: A.FnDecl, indent: int = 0) -> list[str]:
        p = "  " * indent
        binding = self._lookup(decl.name)
        java_name = binding.java if binding is not None \
            else self._mangle(decl.name)
        self._push_scope()
        saved_used = self._fn_used
        self._fn_used = set()
        params = [self._bind_fresh(prm.name, type=prm.type, kind="param")
                  for prm in decl.params]
        sig = ", ".join(f"{self._java_type(b.type)} {b.java}"
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
            else self._java_type(ret)
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
        lines = [f"{p}public static {ret_text} {java_name}({sig}) {{"]
        bp = "  " * (indent + 1)
        body_lines: list[str] = []
        if ensures:
            # `_newrResult` is declared ONCE, at the top of the
            # function - every `return` assigns it (a `final` per
            # return would be a redeclaration). A void function
            # still gets one (of type Void) so its `ensures`
            # clauses can read `result`.
            result_type = "Void" if ret is None or _is_none(ret) \
                else self._java_type(ret)
            body_lines.append(f"{bp}{result_type} _newrResult;")
        for req in requires:
            cond = self.emit_expr(req.cond, 0)
            body_lines.append(f"{bp}if (!({cond})) {{")
            body_lines.append(
                f"{bp}  throw new NewrRt.NewrError("
                f"{_java_str('requires failed: ' + cond)});")
            body_lines.append(f"{bp}}}")
        body_lines.extend(self._emit_block(rest, indent + 1,
                                           new_scope=False))
        if ensures and not (rest and isinstance(rest[-1], A.Return)):
            # The implicit return (falling off the end) is also a
            # return - check the `ensures` against `null`.
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

    def _java_default(self, t: Type) -> str:
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
        union type is Object (name "object" - the emitter casts on
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
        java_name = binding.java if binding is not None \
            else self._mangle(decl.name)
        self._need_rt("toStr")
        self._need_rt("eq")
        self._need_rt("structHash")
        lines = [f"{p}public static class {java_name} {{"]
        for f in decl.fields:
            lines.append(f"{p}  public final {self._java_type(f.type)} "
                         f"{self._mangle(f.name)};")
        if decl.fields:
            params = ", ".join(
                f"{self._java_type(f.type)} {self._mangle(f.name)}"
                for f in decl.fields)
            lines.append(f"{p}  public {java_name}({params}) {{")
            for f in decl.fields:
                lines.append(f"{p}    this.{self._mangle(f.name)} = "
                             f"{self._mangle(f.name)};")
            lines.append(f"{p}  }}")
        else:
            lines.append(f"{p}  public {java_name}() {{")
            lines.append(f"{p}  }}")
        lines.append("")
        lines.append(f"{p}  @Override")
        lines.append(f"{p}  public boolean equals(Object o) {{")
        lines.append(f"{p}    if (this == o) return true;")
        lines.append(f"{p}    if (!(o instanceof {java_name})) return false;")
        if decl.fields:
            lines.append(f"{p}    {java_name} other = ({java_name}) o;")
            cond = " && ".join(
                f"NewrRt.eq(this.{self._mangle(f.name)}, "
                f"other.{self._mangle(f.name)})" for f in decl.fields)
            lines.append(f"{p}    return {cond};")
        else:
            lines.append(f"{p}    return true;")
        lines.append(f"{p}  }}")
        lines.append("")
        lines.append(f"{p}  @Override")
        lines.append(f"{p}  public int hashCode() {{")
        if decl.fields:
            args = ", ".join(f"this.{self._mangle(f.name)}"
                             for f in decl.fields)
            lines.append(f"{p}    return NewrRt.structHash({args});")
        else:
            lines.append(f"{p}    return 1;")
        lines.append(f"{p}  }}")
        lines.append("")
        lines.append(f"{p}  @Override")
        lines.append(f"{p}  public String toString() {{")
        if decl.fields:
            parts = ", ".join(
                f"{f.name}: \" + NewrRt.toStr(this.{self._mangle(f.name)}) + \""
                for f in decl.fields)
            lines.append(f"{p}    return \"{java_name}({parts})\";")
        else:
            lines.append(f"{p}    return \"{java_name}()\";")
        lines.append(f"{p}  }}")
        lines.append(f"{p}}}")
        return lines

    def _emit_enum(self, decl: A.EnumDecl, indent: int = 0) -> list[str]:
        p = "  " * indent
        binding = self._lookup(decl.name)
        java_name = binding.java if binding is not None \
            else self._mangle(decl.name)
        self._need_rt("toStr")
        self._need_rt("eq")
        self._need_rt("structHash")
        union = self._enum_union_fields(decl)
        lines = [f"{p}public static class {java_name} {{"]
        lines.append(f"{p}  public final String _newr_variant;")
        for fname, ftype in union.items():
            lines.append(f"{p}  public final {self._java_type(ftype)} "
                         f"{self._mangle(fname)};")
        # The one constructor takes the discriminant plus every
        # union field (a variant that does not declare a field gets
        # its default).
        ctor_params = ", ".join(
            [f"String variant"] +
            [f"{self._java_type(ft)} {self._mangle(fn)}"
             for fn, ft in union.items()])
        lines.append(f"{p}  private {java_name}({ctor_params}) {{")
        lines.append(f"{p}    this._newr_variant = variant;")
        for fname in union:
            lines.append(f"{p}    this.{self._mangle(fname)} = "
                         f"{self._mangle(fname)};")
        lines.append(f"{p}  }}")
        for variant in decl.variants:
            vname = self._mangle(variant.name)
            if not variant.fields:
                defaults = ", ".join(
                    self._java_default(ft) for ft in union.values())
                args = f"\"{variant.name}\"" + \
                    (f", {defaults}" if defaults else "")
                lines.append(f"{p}  public static final {java_name} "
                             f"{vname} = new {java_name}({args});")
            else:
                params = ", ".join(
                    f"{self._java_type(f.type)} {self._mangle(f.name)}"
                    for f in variant.fields)
                lines.append(f"{p}  public static {java_name} {vname}"
                             f"({params}) {{")
                args = [f"\"{variant.name}\""]
                for fname, ftype in union.items():
                    if fname in {f.name for f in variant.fields}:
                        args.append(self._mangle(fname))
                    else:
                        args.append(self._java_default(ftype))
                lines.append(f"{p}    return new {java_name}"
                             f"({', '.join(args)});")
                lines.append(f"{p}  }}")
        lines.append("")
        lines.append(f"{p}  @Override")
        lines.append(f"{p}  public boolean equals(Object o) {{")
        lines.append(f"{p}    if (this == o) return true;")
        lines.append(f"{p}    if (!(o instanceof {java_name})) return false;")
        lines.append(f"{p}    {java_name} other = ({java_name}) o;")
        conds = [f"this._newr_variant.equals(other._newr_variant)"]
        for fname in union:
            conds.append(
                f"NewrRt.eq(this.{self._mangle(fname)}, "
                f"other.{self._mangle(fname)})")
        lines.append(f"{p}    return {' && '.join(conds)};")
        lines.append(f"{p}  }}")
        lines.append("")
        lines.append(f"{p}  @Override")
        lines.append(f"{p}  public int hashCode() {{")
        args = ", ".join(["this._newr_variant"] +
                         [f"this.{self._mangle(fn)}" for fn in union])
        lines.append(f"{p}    return NewrRt.structHash({args});")
        lines.append(f"{p}  }}")
        lines.append("")
        lines.append(f"{p}  @Override")
        lines.append(f"{p}  public String toString() {{")
        for i, variant in enumerate(decl.variants):
            kw = "if" if i == 0 else "} else if"
            lines.append(f"{p}    {kw} (this._newr_variant.equals("
                         f"{_java_str(variant.name)})) {{")
            if variant.fields:
                parts = ", ".join(
                    f"{f.name}: \" + NewrRt.toStr(this.{self._mangle(f.name)}) + \""
                    for f in variant.fields)
                lines.append(f"{p}      return \"{java_name}.{variant.name}({parts})\";")
            else:
                lines.append(f"{p}      return \"{java_name}.{variant.name}\";")
        lines.append(f"{p}    }}")
        lines.append(f"{p}    return \"{java_name}.?\";")
        lines.append(f"{p}  }}")
        lines.append(f"{p}}}")
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
        # The java-name -> fn-decl map (for keyword-argument reordering
        # in _emit_kwargs).
        self.fns_by_java = {}
        for decl in program.functions:
            b = self._lookup(decl.name)
            if b is not None:
                self.fns_by_java[b.java] = decl
        # The top level's used-name set starts with every module-level
        # Java name, so a `let` in a top-level block cannot reuse one
        # (at module level Java has no block scope for the top level -
        # the name would clobber the module binding).
        self._fn_used = set(self._module_used)

    def _emit_general_file(self, program: A.Program, main_path: str):
        """A general (non-bot) program: header, imports, then the
        public class - static fields for the top-level lets/consts
        (K2: initialized in main(), in file order, so top-level
        functions can read them and side-effecting initializers run
        in order), nested structs/enums, static functions, then
        main() with the top-level STATEMENTS in file order (SPEC
        §8). A file module gets a `static { }` block instead of
        main() (its top-level statements run at class-load time).
        Definitions are hoisted - a function body reads module
        globals at call time, so emitting the `function` before the
        statements is safe - but the statements themselves (lets,
        assignments, prints, ...) must run in the order written.
        (M2 evaluation #1.)"""
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
        # initializers - main() / the static block assigns them in
        # file order).
        for st in program.stmts:
            if isinstance(st, (A.Let, A.Const)):
                b = self._lookup(st.name)
                t = st.type_ann or self._type_or_none(st.value) or \
                    Type("error")
                body.append(f"  public static {self._java_type(t)} "
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
        # Top-level statements: main() for the entry file, a static
        # block for a file module.
        if program.stmts:
            if body:
                body.append("")
            if is_main:
                body.append("  public static void main(String[] args) {")
                body.extend(self._emit_block(program.stmts, 2,
                                             top_level=True))
                body.append("  }")
            else:
                body.append("  static {")
                body.extend(self._emit_block(program.stmts, 2,
                                             top_level=True))
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
        Returns (files, uses_rt). `is_main` marks the top file - the
        only role difference left is that the top file is the entry
        point (a bot's `main()` guard, a general file's top-level
        statements); a file module gets a `static { }` block."""
        files: dict[str, str] = {}
        self._is_main_file = is_main
        text, uses_rt = self._emit_one(program, main_path, is_main=is_main)
        files[self.output_stem + ".java"] = text
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
            # shop_items.java header).
            import os
            sub_path = os.path.relpath(sub_path)
            sub_em = _JavaEmitter(sub, main_path=sub_path,
                                  sources=self.sources,
                                  line_comments=self.line_comments,
                                  output_stem=Path(sub_path).stem)
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


    # ------------------------------------------------------------ bot emitter
    # JDA 6.7.0 bot emission (M4 bot phase). The Java twin of the
    # JS emitter's bot section: the same programs, the same
    # semantics - slash commands via Commands.slash, a Bot class
    # with JDBC store methods (P11), component handlers routed by
    # customId, a standalone deploy program, and a pom.xml.
    #
    # JDA differences from discord.js (verified live against the
    # 6.7.0 jar with javap - the versions are pinned in
    # tests/test_emitter_java_bot.py):
    # - RestActions are async; the emitted code uses `.complete()`
    #   (blocking) so a reply/edit/followup keeps the JS `await`
    #   ordering on the event thread.
    # - There is no message-component collector in JDA core, so a
    #   button/select click routes through a static handler map
    #   (keyed by customId) and the `only:` gate reads the invoking
    #   user from a per-message map filled in when the components
    #   are attached.
    # - Timeouts are scheduled on a shared ScheduledExecutorService
    #   (the collector's `end` callback, JDA-shaped).
    # - Modals use Label components (JDA 6): TextInput has no
    #   label of its own.
    # - Modal timeouts have no JDA equivalent and are ignored
    #   (a documented divergence - the modal still works; it
    #   just never times out).

    # Handler bindings: name -> (value form, interpolation form,
    # newr type name). The Java twin of the JS emitter's
    # _BOT_BINDINGS - JDA spellings. The event parameter is always
    # named `event` (member/message/ready) or `interaction`
    # (command/component); bindings read through it, so no handler
    # local can shadow a top-level name.
    _BOT_BINDINGS: dict[str, dict[str, tuple[str, str | None, str]]] = {
        "command": {
            "user": ("interaction.getUser()",
                     "interaction.getUser().getAsMention()", "user"),
            "guild": ("interaction.getGuild()",
                      # Parenthesised: a bare ternary would escape
                      # the `+` chain it is interpolated into
                      # (`"x" + g != null ? ...` reads as
                      # `("x" + g) != null ? ...`).
                      "(interaction.getGuild() != null ? "
                      "interaction.getGuild().getName() : \"DM\")",
                      "guild"),
            "channel": ("interaction.getChannel()",
                        "_channelText(interaction.getChannel())",
                        "channel"),
            "interaction": ("interaction", None, "interaction"),
        },
        "member_join": {
            "user": ("event.getMember()",
                     "event.getMember().getAsMention()", "member"),
            "guild": ("event.getMember().getGuild()",
                      "event.getMember().getGuild().getName()", "guild"),
        },
        "member_remove": {
            "user": ("event.getMember()",
                     "event.getMember().getAsMention()", "member"),
            "guild": ("event.getMember().getGuild()",
                      "event.getMember().getGuild().getName()", "guild"),
        },
        "ready": {
            "bot": ("event.getJDA().getSelfUser()",
                    "event.getJDA().getSelfUser().getAsTag()", "user"),
        },
        "message": {
            "message": ("event.getMessage()",
                        "event.getMessage().getContentRaw()", "message"),
            "user": ("event.getAuthor()",
                     "event.getAuthor().getAsMention()", "user"),
        },
        "component": {
            "user": ("interaction.getUser()",
                     "interaction.getUser().getAsMention()", "user"),
            "guild": ("interaction.getGuild()",
                      "(interaction.getGuild() != null ? "
                      "interaction.getGuild().getName() : \"DM\")",
                      "guild"),
            "channel": ("interaction.getChannel()",
                        "_channelText(interaction.getChannel())",
                        "channel"),
            "interaction": ("interaction", None, "interaction"),
        },
    }

    # newr intent name -> GatewayIntent constant(s), or None when
    # there is no JDA intent for it (guild lifecycle and
    # integration events arrive with createDefault's defaults, so
    # there is nothing to enable). auto_moderation needs both
    # auto-moderation intents. All names verified live against
    # the JDA 6.7.0 jar.
    _INTENT_MAP: dict[str, str | tuple[str, str] | None] = {
        "guilds": None,
        "members": "GUILD_MEMBERS",
        "moderation": "GUILD_MODERATION",
        "emojis": "GUILD_EXPRESSIONS",
        "integrations": None,
        "webhooks": "GUILD_WEBHOOKS",
        "invites": "GUILD_INVITES",
        "voice_states": "GUILD_VOICE_STATES",
        "presences": "GUILD_PRESENCES",
        "guild_messages": "GUILD_MESSAGES",
        "direct_messages": "DIRECT_MESSAGES",
        "guild_reactions": "GUILD_MESSAGE_REACTIONS",
        "direct_message_reactions": "DIRECT_MESSAGE_REACTIONS",
        "guild_typing": "GUILD_MESSAGE_TYPING",
        "direct_message_typing": "DIRECT_MESSAGE_TYPING",
        "message_content": "MESSAGE_CONTENT",
        "guild_scheduled_events": "SCHEDULED_EVENTS",
        "auto_moderation": ("AUTO_MODERATION_CONFIGURATION",
                            "AUTO_MODERATION_EXECUTION"),
        "guild_polls": "GUILD_MESSAGE_POLLS",
        "direct_message_polls": "DIRECT_MESSAGE_POLLS",
    }

    def _bot_intents(self, program: A.Program) -> list[str]:
        """The GatewayIntent constants to enable, sorted. The
        declared intents, plus the ones the events need (member
        events need members, message events need message_content)
        - the JS emitter's _bot_intents, JDA spellings."""
        intents = set(program.bot.intents or [])
        kinds = {ev.kind for ev in program.events}
        if "member_join" in kinds or "member_remove" in kinds:
            intents.add("members")
        if "message" in kinds:
            intents.add("message_content")
        out: set[str] = set()
        for intent in intents:
            if intent not in self._INTENT_MAP:
                self._fail(f"unknown intent `{intent}` (no "
                           "GatewayIntent mapping)")
            mapped = self._INTENT_MAP[intent]
            if isinstance(mapped, tuple):
                out.update(mapped)
            elif mapped is not None:
                out.add(mapped)
        return sorted(out)

    def _emit_bot_file(self, program: A.Program, main_path: str):
        """The bot program: one public class extending
        ListenerAdapter, with the static members of the general
        emitter (top-level lets, structs, enums, fns), the
        synthesized table row classes, the nested Bot class with
        the JDBC store methods, the COMMANDS table, the component
        handler maps, the execute / component / task methods, the
        ListenerAdapter overrides, and main()."""
        bot = program.bot
        self.program = program
        self.fns = {f.name: f for f in program.functions}
        self.structs = {s.name: s for s in program.structs}
        self.enums = {e.name: e for e in program.enums}
        self.stores = {s.name: s for s in program.stores}
        self._register_module_scope(program)
        self._fn_ctx = []
        self._hoists = []
        self._bot_command_methods = []
        self._bot_command_methods_by_name = {}
        self._bot_command_data = []
        self._bot_command_method_names = set()
        self._bot_components = []
        self._bot_component_registrations = []
        self._bot_component_kinds = set()
        self._bot_component_names = set()
        self._bot_event_methods = []
        self._bot_task_methods = []
        self._task_fn_names = {}
        self._task_fn_name_counts = {}
        self._uses_scheduler = False
        self._uses_author_map = False
        self._pending_components = []
        self._component_counter = 0
        self._table_rows = {}
        self._need_discord(
            "net.dv8tion.jda.api.hooks.ListenerAdapter")
        # A store table becomes a row class, bound in the module
        # scope as a struct so _java_type / _struct_java_name
        # resolve it with no change to the general emitter. A
        # table that collides with a struct, enum, fn, or another
        # table is a clean E4001, not a broken class.
        taken = {s.name for s in program.structs}
        taken |= {e.name for e in program.enums}
        taken |= {f.name for f in program.functions}
        for store in program.stores:
            for table in store.tables:
                if table.name in taken:
                    self._fail(f"store table `{table.name}` collides "
                               "with another declaration")
                taken.add(table.name)
                self._table_rows[table.name] = A.StructDecl(
                    name=table.name, fields=list(table.columns),
                    span=table.span)
                self._bind_module_level(table.name, kind="struct")

        # Declarations: top-level lets, structs, enums, row
        # classes, fns (same shape as _emit_general_file).
        decl_lines: list[str] = []
        for st in program.stmts:
            if isinstance(st, (A.Let, A.Const)):
                b = self._lookup(st.name)
                t = st.type_ann or self._type_or_none(st.value) or \
                    Type("error")
                decl_lines.append(
                    f"  public static {self._java_type(t)} "
                    f"{b.java if b is not None else self._mangle(st.name)};")
        for decl in program.structs:
            decl_lines.extend(self._emit_struct(decl, 1))
        for decl in program.enums:
            decl_lines.extend(self._emit_enum(decl, 1))
        for table_name in self._table_rows:
            decl_lines.extend(
                self._emit_struct(self._table_rows[table_name], 1))
        for decl in program.functions:
            decl_lines.extend(self._emit_fn(decl, 1))

        # The nested Bot class (store methods) + the `bot` field.
        bot_lines = self._emit_bot_class(program)

        # Strings table + _t (only when strings blocks exist).
        strings_lines = self._emit_strings(program)

        # Commands, events, tasks (these fill the accumulators:
        # execute methods, handler methods, registrations,
        # COMMANDS data, task methods, overrides).
        for cmd in program.commands:
            self._emit_command(cmd)
        for ev in program.events:
            self._emit_event_registration(ev)
        for task in program.tasks:
            self._emit_task(task)

        # Assemble the class body.
        body: list[str] = []
        body.extend(decl_lines)
        body.extend(bot_lines)
        body.extend(self._emit_component_maps())
        body.extend(strings_lines)
        body.extend(self._emit_commands_table())
        for method in self._bot_command_methods:
            body.append("")
            body.extend(method)
        for method in self._bot_components:
            body.append("")
            body.extend(method)
        for method in self._bot_task_methods:
            body.append("")
            body.extend(method)
        body.extend(self._emit_dispatch())
        for method in self._bot_event_methods:
            body.append("")
            body.extend(method)
        body.append("")
        body.extend(self._emit_main(program))
        if self._used_channel_text:
            body.append("")
            body.extend(self._emit_channel_text())

        lines = self._header(main_path, bot.description)
        imports = self._render_imports()
        if imports:
            lines.append("")
            lines.extend(imports)
        lines.append("")
        lines.append(f"public class {self.output_stem} "
                     "extends ListenerAdapter {")
        lines.extend(body)
        lines.append("}")
        text = "\n".join(lines) + "\n"
        return text, bool(self.rt_helpers)

    def _emit_component_maps(self) -> list[str]:
        """The static component handler maps and the author map,
        plus the static block that registers each handler by
        customId (method references - the dispatch overrides
        below look them up). Only the maps that are used."""
        if not self._bot_component_registrations:
            return []
        lines: list[str] = []
        if "button" in self._bot_component_kinds:
            self._need_discord(
                "net.dv8tion.jda.api.events.interaction.component.ButtonInteractionEvent")
            lines.append("  private static final Map<String, "
                         "Consumer<ButtonInteractionEvent>> "
                         "buttonHandlers = new LinkedHashMap<>();")
        if "select" in self._bot_component_kinds:
            self._need_discord(
                "net.dv8tion.jda.api.events.interaction.component.StringSelectInteractionEvent")
            lines.append("  private static final Map<String, "
                         "Consumer<StringSelectInteractionEvent>> "
                         "selectHandlers = new LinkedHashMap<>();")
        if "modal" in self._bot_component_kinds:
            self._need_discord(
                "net.dv8tion.jda.api.events.interaction.ModalInteractionEvent")
            lines.append("  private static final Map<String, "
                         "Consumer<ModalInteractionEvent>> "
                         "modalHandlers = new LinkedHashMap<>();")
        if self._uses_author_map:
            self._extra_imports.add("java.util.concurrent.*")
            lines.append("  private static final Map<String, String> "
                         "_componentAuthors = new ConcurrentHashMap<>();")
        if self._uses_scheduler:
            self._extra_imports.add("java.util.concurrent.*")
            lines.append("  private static final ScheduledExecutorService "
                         "_newrScheduler = Executors.newScheduledThreadPool(1);")
        self._extra_imports.add("java.util.function.*")
        lines.append("")
        lines.append("  static {")
        lines.extend(self._bot_component_registrations)
        lines.append("  }")
        lines.append("")
        return lines

    def _emit_commands_table(self) -> list[str]:
        """The COMMANDS table the deploy program registers."""
        if not self._bot_command_data:
            return ["  public static final List<SlashCommandData> "
                    "COMMANDS = List.of();", ""]
        lines = ["  public static final List<SlashCommandData> "
                 "COMMANDS = List.of("]
        for i, entry in enumerate(self._bot_command_data):
            comma = "," if i < len(self._bot_command_data) - 1 else ""
            for j, eline in enumerate(entry):
                suffix = comma if j == len(entry) - 1 else ""
                lines.append(f"    {eline}{suffix}")
        lines.append("  );")
        lines.append("")
        return lines

    def _emit_strings(self, program: A.Program) -> list[str]:
        """The STRINGS table + _t (the Java twin of the Python
        emitter's strings method): exact locale, then base
        language, then the default locale; a missing key returns
        the key itself."""
        if not program.strings:
            return []
        self._need_rt("toStr")
        default = program.strings[0].locale
        lines = [f"  private static final String _DEFAULT_LOCALE = "
                 f"{_java_str(default)};",
                 "  private static final Map<String, Map<String, String>> "
                 "STRINGS = _newrStrings();",
                 "",
                 "  private static Map<String, Map<String, String>> "
                 "_newrStrings() {",
                 "    Map<String, Map<String, String>> m = "
                 "new LinkedHashMap<>();"]
        for decl in program.strings:
            pairs = ", ".join(
                f"{_java_str(e.key)}, {_java_str(e.template)}"
                for e in decl.entries)
            lines.append(f"    m.put({_java_str(decl.locale)}, "
                         f"NewrRt.<String>dictOf({pairs}));")
        lines.append("    return m;")
        lines.append("  }")
        lines.append("")
        lines.append("  private static String _t(String key, "
                     "String locale, Map<String, Object> vars) {")
        lines.append("    String loc = locale != null ? locale : "
                     "_DEFAULT_LOCALE;")
        lines.append("    Map<String, String> table = STRINGS.get(loc);")
        lines.append("    if (table == null && loc.contains(\"-\")) {")
        lines.append("      table = STRINGS.get(loc.split(\"-\")[0]);")
        lines.append("    }")
        lines.append("    if (table == null) {")
        lines.append("      table = STRINGS.get(_DEFAULT_LOCALE);")
        lines.append("    }")
        lines.append("    String template = table != null "
                     "? table.get(key) : null;")
        lines.append("    if (template == null) {")
        lines.append("      return key;")
        lines.append("    }")
        lines.append("    String out = template;")
        lines.append("    for (Map.Entry<String, Object> e : "
                     "vars.entrySet()) {")
        lines.append("      out = out.replace(\"{\" + e.getKey() + \"}\", "
                     "NewrRt.toStr(e.getValue()));")
        lines.append("    }")
        lines.append("    return out;")
        lines.append("  }")
        lines.append("")
        return lines

    def _emit_channel_text(self) -> list[str]:
        """The `{channel}` interpolation: the channel as text is
        its mention, or \"\" when there is none."""
        self._need_discord(
            "net.dv8tion.jda.api.entities.channel.middleman.MessageChannel")
        return [
            "  private static String _channelText(MessageChannel channel) {",
            "    if (channel == null) {",
            "      return \"\";",
            "    }",
            "    return channel.getAsMention();",
            "  }",
        ]

    # ------------------------------------------------------------ store (P11)
    # One long-lived JDBC connection per store (created in
    # setup(), before login), a commit per mutation, the ledger
    # and the balance in one transaction, and a duplicate
    # idempotency key replays instead of double-applying - the
    # Java twin of the JS emitter's _emit_store_methods. All
    # store methods are synchronized: JDA calls listeners on
    # event threads, so a lock is what makes a read-modify-write
    # atomic (the Python emitter's threading.Lock, the JS
    # emitter's single thread). Balances are stored as TEXT and
    # parsed with Long.parseLong; arithmetic goes through
    # NewrRt.add/sub, never raw +/- on longs (checked - a
    # silently wrapped balance is a money bug).

    _SQL_TYPES = {"int": "TEXT", "str": "TEXT", "float": "REAL",
                  "bool": "INTEGER"}

    def _emit_bot_class(self, program: A.Program) -> list[str]:
        if not program.stores:
            return []
        self._need_sqlite()
        lines = ["  private static class Bot {"]
        for store in program.stores:
            lines.append(f"    private Connection {store.name}_db;")
        lines.append("")
        lines.append("    private void setup() {")
        for store in program.stores:
            lines.append(f"      _{store.name}_connect();")
        lines.append("    }")
        for store in program.stores:
            lines.extend(self._emit_store_methods(store))
        lines.append("  }")
        lines.append("")
        lines.append("  private static Bot bot;")
        lines.append("")
        return lines

    def _emit_store_methods(self, store: A.StoreDecl) -> list[str]:
        name = store.name
        lines = ["",
                 f"    private void _{name}_connect() {{",
                 "      try {",
                 "        Class.forName(\"org.sqlite.JDBC\");",
                 f"        {name}_db = DriverManager.getConnection("
                 f"\"jdbc:sqlite:{name}.db\");",
                 f"        try (Statement st = {name}_db.createStatement()) {{",
                 "          st.execute(\"PRAGMA busy_timeout = 5000\");"]
        for table in store.tables:
            cols = ", ".join(
                f"{c.name} {self._SQL_TYPES.get(c.type.name, 'TEXT')}"
                f"{' PRIMARY KEY' if i == 0 else ''}"
                for i, c in enumerate(table.columns))
            lines.append(f"          st.execute(\"CREATE TABLE IF NOT "
                         f"EXISTS {table.name} ({cols})\");")
        if store.ledger:
            lines.append("          st.execute(\"CREATE TABLE IF NOT "
                         "EXISTS ledger (id INTEGER PRIMARY KEY "
                         "AUTOINCREMENT, user TEXT NOT NULL, amount "
                         "TEXT NOT NULL, key TEXT UNIQUE, "
                         "created_at INTEGER NOT NULL)\");")
        lines.append("        }")
        lines.append("      } catch (ClassNotFoundException | "
                     "SQLException e) {")
        lines.append("        throw new RuntimeException(e);")
        lines.append("      }")
        lines.append("    }")
        lines.append("")
        lines.append("    private void _rollback(Statement st) {")
        lines.append("      try {")
        lines.append("        st.execute(\"ROLLBACK\");")
        lines.append("      } catch (SQLException ignored) {")
        lines.append("        // The original error is the one that "
                     "matters.")
        lines.append("      }")
        lines.append("    }")
        tables = {t.name: t for t in store.tables}
        if "balances" in tables:
            lines.extend(self._emit_economy_methods(name))
        for table in store.tables:
            lines.extend(self._emit_table_methods(name, table))
        return lines

    def _jdbc_read_balance(self, db: str, user_expr: str, p: str,
                           var: str = "cur") -> list[str]:
        """Lines declaring `long <var>` with the balance for
        `user_expr` (0 when the row is absent). `var` exists so a
        method that reads two balances (transfer) can use two
        names in the same scope."""
        return [
            f"{p}long {var} = 0L;",
            f"{p}try (PreparedStatement ps = {db}.prepareStatement("
            "\"SELECT balance FROM balances WHERE user = ?\")) {",
            f"{p}  ps.setString(1, {user_expr});",
            f"{p}  try (ResultSet rs = ps.executeQuery()) {{",
            f"{p}    {var} = rs.next() ? Long.parseLong(rs.getString(1)) "
            ": 0L;",
            f"{p}  }}",
            f"{p}}}",
        ]

    def _jdbc_upsert_balance(self, db: str, user_expr: str,
                             next_expr: str, p: str) -> list[str]:
        return [
            f"{p}try (PreparedStatement ps = {db}.prepareStatement("
            "\"INSERT INTO balances (user, balance) VALUES (?, ?) "
            "ON CONFLICT(user) DO UPDATE SET balance = "
            "excluded.balance\")) {",
            f"{p}  ps.setString(1, {user_expr});",
            f"{p}  ps.setString(2, Long.toString({next_expr}));",
            f"{p}  ps.executeUpdate();",
            f"{p}}}",
        ]

    def _jdbc_insert_ledger(self, db: str, user_expr: str,
                            amount_expr: str, key_expr: str, p: str) \
            -> list[str]:
        return [
            f"{p}try (PreparedStatement ps = {db}.prepareStatement("
            "\"INSERT INTO ledger (user, amount, key, created_at) "
            "VALUES (?, ?, ?, ?)\")) {",
            f"{p}  ps.setString(1, {user_expr});",
            f"{p}  ps.setString(2, Long.toString({amount_expr}));",
            f"{p}  ps.setString(3, {key_expr});",
            f"{p}  ps.setLong(4, System.currentTimeMillis() / 1000);",
            f"{p}  ps.executeUpdate();",
            f"{p}}}",
        ]

    def _jdbc_ledger_has_key(self, db: str, key_expr: str, p: str) \
            -> list[str]:
        """Lines declaring `boolean dup` - whether the ledger
        already holds `key_expr`."""
        return [
            f"{p}boolean dup = false;",
            f"{p}try (PreparedStatement ps = {db}.prepareStatement("
            "\"SELECT 1 FROM ledger WHERE key = ?\")) {",
            f"{p}  ps.setString(1, {key_expr});",
            f"{p}  try (ResultSet rs = ps.executeQuery()) {{",
            f"{p}    dup = rs.next();",
            f"{p}  }}",
            f"{p}}}",
        ]

    def _emit_economy_methods(self, name: str) -> list[str]:
        """balance / _apply / credit / debit / transfer for a
        store with a `balances` table. `credit` never refuses;
        `debit` and `transfer` refuse (false) on insufficient
        funds; `transfer` heals the receiver's ledger first
        (the JS emitter's transfer healing, SQL-spelled)."""
        db = f"{name}_db"
        self._need_rt("add")
        self._need_rt("sub")
        lines = ["",
                 f"    synchronized long {name}_balance(String user) {{",
                 "      try {"]
        lines.extend(self._jdbc_read_balance(db, "user", "        "))
        lines.append("        return cur;")
        lines.append("      } catch (SQLException e) {")
        lines.append("        throw new RuntimeException(e);")
        lines.append("      }")
        lines.append("    }")
        # _apply: the shared credit/debit core. A duplicate key
        # replays (void); a debit with insufficient funds rolls
        # back and returns false; the ledger row is written in
        # the same transaction as the balance.
        lines.append("")
        lines.append(f"    synchronized boolean _{name}_apply(String user, "
                     "long delta, String key) {")
        lines.append("      try {")
        lines.append("        if (key != null) {")
        lines.extend(self._jdbc_ledger_has_key(db, "key", "          "))
        lines.append("          if (dup) {")
        lines.append("            return true;")
        lines.append("          }")
        lines.append("        }")
        lines.append(f"        try (Statement st = {db}.createStatement()) {{")
        lines.append("          st.execute(\"BEGIN IMMEDIATE\");")
        lines.append("          try {")
        lines.extend(self._jdbc_read_balance(db, "user", "            "))
        lines.append("            long next = NewrRt.add(cur, delta);")
        lines.append("            if (next < 0L) {")
        lines.append("              st.execute(\"ROLLBACK\");")
        lines.append("              return false;")
        lines.append("            }")
        lines.extend(self._jdbc_upsert_balance(
            db, "user", "next", "            "))
        lines.append("            if (key != null) {")
        lines.extend(self._jdbc_insert_ledger(
            db, "user", "delta", "key", "              "))
        lines.append("            }")
        lines.append("            st.execute(\"COMMIT\");")
        lines.append("            return true;")
        lines.append("          } catch (SQLException e) {")
        lines.append("            _rollback(st);")
        lines.append("            throw new RuntimeException(e);")
        lines.append("          } catch (RuntimeException e) {")
        lines.append("            _rollback(st);")
        lines.append("            throw e;")
        lines.append("          }")
        lines.append("        }")
        lines.append("      } catch (SQLException e) {")
        lines.append("        throw new RuntimeException(e);")
        lines.append("      }")
        lines.append("    }")
        lines.append("")
        lines.append(f"    synchronized void {name}_credit(String user, "
                     "long delta, String key) {")
        lines.append(f"      _{name}_apply(user, delta, key);")
        lines.append("    }")
        lines.append("")
        lines.append(f"    synchronized boolean {name}_debit(String user, "
                     "long delta, String key) {")
        lines.append(f"      return _{name}_apply(user, NewrRt.sub(0L, "
                     "delta), key);")
        lines.append("    }")
        lines.append("")
        lines.append(f"    synchronized boolean {name}_transfer(String from, "
                     "String to, long amount, String key) {")
        lines.append("      try {")
        lines.append("        if (key != null) {")
        lines.extend(self._jdbc_ledger_has_key(db, "key", "          "))
        lines.append("          if (dup) {")
        lines.append("            return true;")
        lines.append("          }")
        lines.append("        }")
        lines.append("        if (amount <= 0L) {")
        lines.append("          return false;")
        lines.append("        }")
        lines.append(f"        try (Statement st = {db}.createStatement()) {{")
        lines.append("          st.execute(\"BEGIN IMMEDIATE\");")
        lines.append("          try {")
        lines.extend(self._jdbc_read_balance(db, "from", "            ",
                                             var="curFrom"))
        lines.append("            long fromBalance = curFrom;")
        lines.append("            if (fromBalance < amount) {")
        lines.append("              st.execute(\"ROLLBACK\");")
        lines.append("              return false;")
        lines.append("            }")
        lines.append("            long nextFrom = NewrRt.sub(fromBalance, "
                     "amount);")
        lines.extend(self._jdbc_upsert_balance(
            db, "from", "nextFrom", "            "))
        lines.append("            if (key != null) {")
        lines.extend(self._jdbc_insert_ledger(
            db, "from", "NewrRt.sub(0L, amount)", "key", "              "))
        lines.append("            }")
        lines.append("            // Heal the receiver's ledger: a "
                     "previous failed transfer may have logged the "
                     "out leg but not the in leg; replay it with "
                     "the opposite key before crediting.")
        lines.append("            boolean healed = false;")
        lines.append("            long sumOut = 0L;")
        lines.append("            long sumIn = 0L;")
        lines.append("            long sumKeys = 0L;")
        lines.append("            try (PreparedStatement ps = "
                     f"{db}.prepareStatement(\"SELECT amount, key "
                     "FROM ledger WHERE user = ?\")) {")
        lines.append("              ps.setString(1, to);")
        lines.append("              try (ResultSet rs = ps.executeQuery()) {")
        lines.append("                while (rs.next()) {")
        lines.append("                  String k = rs.getString(2);")
        lines.append("                  long a = Long.parseLong("
                     "rs.getString(1));")
        lines.append("                  if (k != null && k.endsWith(\":out\")) {")
        lines.append("                    sumOut = NewrRt.add(sumOut, a);")
        lines.append("                  } else if (k != null && "
                     "k.endsWith(\":in\")) {")
        lines.append("                    sumIn = NewrRt.add(sumIn, a);")
        lines.append("                  } else {")
        lines.append("                    sumKeys = NewrRt.add(sumKeys, a);")
        lines.append("                  }")
        lines.append("                }")
        lines.append("              }")
        lines.append("            }")
        lines.append("            if (sumOut > 0L && sumIn == 0L) {")
        lines.append("              long heal = Math.min(amount, sumOut);")
        lines.append("              try (PreparedStatement ps = "
                     f"{db}.prepareStatement(\"INSERT INTO ledger "
                     "(user, amount, key, created_at) VALUES "
                     "(?, ?, ?, ?)\")) {")
        lines.append("                ps.setString(1, to);")
        lines.append("                ps.setString(2, Long.toString(heal));")
        lines.append("                ps.setString(3, key != null "
                     "? key + \":in\" : null);")
        lines.append("                ps.setLong(4, "
                     "System.currentTimeMillis() / 1000);")
        lines.append("                ps.executeUpdate();")
        lines.append("              }")
        lines.append("              healed = true;")
        lines.append("            }")
        lines.extend(self._jdbc_read_balance(db, "to", "            ",
                                             var="curTo"))
        lines.append("            long toBalance = curTo;")
        lines.append("            long nextTo = healed "
                     "? NewrRt.add(toBalance, amount) : toBalance;")
        lines.append("            if (!healed) {")
        lines.extend(self._jdbc_insert_ledger(
            db, "to", "amount", "key != null ? key + \":in\" : null",
            "              "))
        lines.append("            }")
        lines.extend(self._jdbc_upsert_balance(
            db, "to", "nextTo", "            "))
        lines.append("            st.execute(\"COMMIT\");")
        lines.append("            return true;")
        lines.append("          } catch (SQLException e) {")
        lines.append("            _rollback(st);")
        lines.append("            throw new RuntimeException(e);")
        lines.append("          } catch (RuntimeException e) {")
        lines.append("            _rollback(st);")
        lines.append("            throw e;")
        lines.append("          }")
        lines.append("        }")
        lines.append("      } catch (SQLException e) {")
        lines.append("        throw new RuntimeException(e);")
        lines.append("      }")
        lines.append("    }")
        return lines

    def _jdbc_key_setter(self, col_type: str, param: str, value: str,
                         p: str) -> str:
        """The PreparedStatement setter for a key/value of
        `col_type`: `ps.setString(1, ...)` and friends."""
        if col_type == "int":
            return (f"{p}ps.setLong({param}, "
                    f"((Long) {value}).longValue());")
        if col_type == "float":
            return (f"{p}ps.setDouble({param}, "
                    f"((Double) {value}).doubleValue());")
        if col_type == "bool":
            return (f"{p}ps.setInt({param}, "
                    f"((Boolean) {value}).booleanValue() ? 1 : 0);")
        return f"{p}ps.setString({param}, (String) {value});"

    def _jdbc_value_setter(self, col_type: str, param: str, field: str,
                           p: str) -> str:
        """The setter for a row field (an int is stored as its
        decimal TEXT; a bool as 0/1)."""
        if col_type == "int":
            return (f"{p}ps.setString({param}, "
                    f"Long.toString({field}));")
        if col_type == "float":
            return f"{p}ps.setDouble({param}, {field});"
        if col_type == "bool":
            return f"{p}ps.setInt({param}, {field} ? 1 : 0);"
        return f"{p}ps.setString({param}, {field});"

    def _jdbc_value_reader(self, col_type: str, col: str) -> str:
        """The ResultSet read for a row field."""
        if col_type == "int":
            return f"Long.parseLong(rs.getString(\"{col}\"))"
        if col_type == "float":
            return f"rs.getDouble(\"{col}\")"
        if col_type == "bool":
            return f"rs.getInt(\"{col}\") != 0"
        return f"rs.getString(\"{col}\")"

    def _emit_table_methods(self, name: str, table: A.TableDecl) \
            -> list[str]:
        """get / set / delete for one store, one method each
        dispatching on the table name (the JS emitter's shape).
        `get` returns the row object or null; `set` upserts;
        `delete` removes the row."""
        db = f"{name}_db"
        row = self._lookup(table.name)
        row_java = row.java if row is not None else self._mangle(table.name)
        key_col = table.columns[0]
        cols = ", ".join(c.name for c in table.columns)
        placeholders = ", ".join("?" for _ in table.columns)
        set_clause = ", ".join(
            f"{c.name} = excluded.{c.name}" for c in table.columns)
        lines = ["",
                 f"    synchronized Object {name}_get(String table, "
                 "Object key) {"]
        lines.append("      try {")
        lines.append(f"        if (table.equals({_java_str(table.name)})) {{")
        lines.append("          try (PreparedStatement ps = "
                     f"{db}.prepareStatement(\"SELECT * FROM "
                     f"{table.name} WHERE {key_col.name} = ?\")) {{")
        lines.append(self._jdbc_key_setter(
            key_col.type.name, "1", "key", "            "))
        lines.append("            try (ResultSet rs = ps.executeQuery()) {")
        lines.append("              if (!rs.next()) {")
        lines.append("                return null;")
        lines.append("              }")
        reads = ", ".join(
            self._jdbc_value_reader(c.type.name, c.name)
            for c in table.columns)
        lines.append(f"              return new {row_java}({reads});")
        lines.append("            }")
        lines.append("          }")
        lines.append("        }")
        lines.append("        throw new IllegalArgumentException("
                     "\"unknown table: \" + table);")
        lines.append("      } catch (SQLException e) {")
        lines.append("        throw new RuntimeException(e);")
        lines.append("      }")
        lines.append("    }")
        lines.append("")
        lines.append(f"    synchronized void {name}_set(String table, "
                     "Object row) {")
        lines.append("      try {")
        lines.append(f"        if (table.equals({_java_str(table.name)})) {{")
        lines.append(f"          {row_java} r = ({row_java}) row;")
        lines.append("          try (PreparedStatement ps = "
                     f"{db}.prepareStatement(\"INSERT INTO "
                     f"{table.name} ({cols}) VALUES ({placeholders}) "
                     f"ON CONFLICT({key_col.name}) DO UPDATE SET "
                     f"{set_clause}\")) {{")
        for i, c in enumerate(table.columns):
            lines.append(self._jdbc_value_setter(
                c.type.name, str(i + 1), f"r.{self._mangle(c.name)}",
                "            "))
        lines.append("            ps.executeUpdate();")
        lines.append("          }")
        lines.append("          return;")
        lines.append("        }")
        lines.append("        throw new IllegalArgumentException("
                     "\"unknown table: \" + table);")
        lines.append("      } catch (SQLException e) {")
        lines.append("        throw new RuntimeException(e);")
        lines.append("      }")
        lines.append("    }")
        lines.append("")
        lines.append(f"    synchronized void {name}_delete(String table, "
                     "Object key) {")
        lines.append("      try {")
        lines.append(f"        if (table.equals({_java_str(table.name)})) {{")
        lines.append("          try (PreparedStatement ps = "
                     f"{db}.prepareStatement(\"DELETE FROM "
                     f"{table.name} WHERE {key_col.name} = ?\")) {{")
        lines.append(self._jdbc_key_setter(
            key_col.type.name, "1", "key", "            "))
        lines.append("            ps.executeUpdate();")
        lines.append("          }")
        lines.append("          return;")
        lines.append("        }")
        lines.append("        throw new IllegalArgumentException("
                     "\"unknown table: \" + table);")
        lines.append("      } catch (SQLException e) {")
        lines.append("        throw new RuntimeException(e);")
        lines.append("      }")
        lines.append("    }")
        return lines

    def _emit_deploy_file(self, program: A.Program, bot_filename: str):
        """The standalone deploy program: log in, register the
        COMMANDS table (guild when DISCORD_GUILD_ID is set, else
        globally), print how many were registered, shut down.
        Run once, then run the bot."""
        stem = Path(bot_filename).stem
        cls = _java_class_name(stem + "_deploy")
        lines = self._header(self.main_path,
                             "Command registration - run once, "
                             "then run the bot.")
        lines.append("")
        lines.append("import net.dv8tion.jda.api.JDA;")
        lines.append("import net.dv8tion.jda.api.JDABuilder;")
        lines.append("import net.dv8tion.jda.api.entities.Guild;")
        lines.append("")
        lines.append(f"public class {cls} {{")
        lines.append("  public static void main(String[] args) "
                     "throws Exception {")
        lines.append("    String token = System.getenv(\"DISCORD_TOKEN\");")
        lines.append("    String clientId = "
                     "System.getenv(\"DISCORD_CLIENT_ID\");")
        lines.append("    String guildId = "
                     "System.getenv(\"DISCORD_GUILD_ID\");")
        lines.append("    if (token == null || token.isEmpty() || "
                     "clientId == null || clientId.isEmpty()) {")
        lines.append("      throw new IllegalStateException(\"set "
                     "DISCORD_TOKEN and DISCORD_CLIENT_ID\");")
        lines.append("    }")
        lines.append("    JDA jda = JDABuilder.createDefault(token).build();")
        lines.append("    jda.awaitReady();")
        lines.append("    if (guildId != null && !guildId.isEmpty()) {")
        lines.append("      Guild guild = jda.getGuildById(guildId);")
        lines.append("      if (guild == null) {")
        lines.append("        throw new IllegalStateException(\"guild not "
                     "found: \" + guildId);")
        lines.append("      }")
        lines.append(f"      guild.updateCommands().addCommands("
                     f"{self.output_stem}.COMMANDS).complete();")
        lines.append(f"      System.out.println(\"Registered \" + "
                     f"{self.output_stem}.COMMANDS.size() + \" command(s) "
                     "in guild \" + guildId + \".\");")
        lines.append("    } else {")
        lines.append(f"      jda.updateCommands().addCommands("
                     f"{self.output_stem}.COMMANDS).complete();")
        lines.append(f"      System.out.println(\"Registered \" + "
                     f"{self.output_stem}.COMMANDS.size() + \" command(s) "
                     "globally.\");")
        lines.append("    }")
        lines.append("    jda.shutdown();")
        lines.append("  }")
        lines.append("}")
        return "\n".join(lines) + "\n"

    # ------------------------------------------------------------ commands
    _OPTION_GETTER = {"str": "getAsString", "int": "getAsLong",
                      "num": "getAsDouble", "float": "getAsDouble",
                      "bool": "getAsBoolean", "user": "getAsUser",
                      "member": "getAsMember", "channel": "getAsChannel",
                      "role": "getAsRole"}
    _OPTION_JDA_TYPE = {"str": "STRING", "int": "INTEGER",
                        "num": "NUMBER", "float": "NUMBER",
                        "bool": "BOOLEAN", "user": "USER",
                        "member": "USER", "channel": "CHANNEL",
                        "role": "ROLE"}

    def _command_method_name(self, name: str) -> str:
        """`executePing` for `/ping` - unique per file."""
        words = "".join(ch if ch.isalnum() else " " for ch in name).split()
        base = "".join(w.capitalize() for w in words) or "Command"
        method = "execute" + base
        if method in self._bot_command_method_names:
            n = 2
            while f"{method}{n}" in self._bot_command_method_names:
                n += 1
            method = f"{method}{n}"
        self._bot_command_method_names.add(method)
        return method

    def _option_builder(self, opt: A.OptionDecl) -> str:
        """The `new OptionData(...)` for one command option."""
        t = opt.type
        tname = t.name if t is not None else "str"
        otype = self._OPTION_JDA_TYPE.get(tname)
        if otype is None:
            self._fail(f"option `{opt.name}` has a type that cannot "
                       "be a slash-command option")
        desc = opt.description if opt.description is not None else opt.name
        text = (f"new OptionData(OptionType.{otype}, "
                f"{_java_str(opt.name)}, {_java_str(desc)}, "
                f"{'true' if opt.required else 'false'})")
        if opt.min_value is not None:
            text += f".setMinValue({self._emit_option_bound(opt.min_value, tname)})"
        if opt.max_value is not None:
            text += f".setMaxValue({self._emit_option_bound(opt.max_value, tname)})"
        if opt.choices:
            for c in opt.choices:
                text += (f".addChoice({self._emit_choice_name(c, tname)}, "
                         f"{self._emit_choice_value(c, tname)})")
        return text

    def _emit_option_bound(self, expr, tname: str) -> str:
        """An option's min/max bound. setMinValue/setMaxValue are
        overloaded (long vs double), so an int bound must be a
        long literal (`1L`) and a number bound a double literal
        (`1.0`) - a bare `1` would be an ambiguous call."""
        if isinstance(expr, A.Lit):
            if tname == "int":
                return f"{expr.value}L"
            if tname in ("num", "float"):
                return _java_float(float(expr.value))
        text = self.emit_expr(expr, 0)
        if tname in ("num", "float") and self._type_of(expr).name == "int":
            return f"(double) ({text})"
        return text

    def _emit_choice_name(self, expr, tname: str) -> str:
        text = self.emit_expr(expr, 0)
        if tname == "str":
            return text
        return f"String.valueOf({text})"

    def _emit_choice_value(self, expr, tname: str) -> str:
        text = self.emit_expr(expr, 0)
        if tname not in ("str", "int", "num", "float"):
            self._fail("only str / int / num options can have choices")
        return text

    def _option_java_type(self, opt: A.OptionDecl) -> Type:
        tname = opt.type.name if opt.type is not None else "str"
        if tname == "num":
            tname = "float"
        if opt.required:
            return Type(tname)
        # An optional option without a default is null when the
        # user did not pass it; with a default it is never null.
        return Type(tname, nullable=opt.default is None)

    def _emit_option_bindings(self, cmd: A.CommandDecl,
                              indent: int) -> list[str]:
        """The option locals at the top of an execute method:
        required options read straight through; optional ones
        read through a temp so the default (or null) applies."""
        p = "  " * indent
        lines: list[str] = []
        for opt in cmd.options:
            tname = opt.type.name if opt.type is not None else "str"
            getter = self._OPTION_GETTER.get(tname)
            if getter is None:
                self._fail(f"option `{opt.name}` has a type that "
                           "cannot be a slash-command option")
            jtype = self._java_type(self._option_java_type(opt))
            if tname == "channel":
                value = (f"(MessageChannel) interaction.getOption("
                         f"{_java_str(opt.name)}).getAsChannel()")
            else:
                value = (f"interaction.getOption({_java_str(opt.name)})"
                         f".{getter}()")
            if opt.required:
                lines.append(f"{p}{jtype} {self._mangle(opt.name)} = "
                             f"{value};")
            else:
                temp = self._new_temp_name()
                lines.append(f"{p}OptionMapping {temp} = "
                             f"interaction.getOption("
                             f"{_java_str(opt.name)});")
                self._need_discord(
                    "net.dv8tion.jda.api.interactions.commands.OptionMapping")
                if opt.default is not None:
                    default = self.emit_expr(opt.default, 0)
                    lines.append(f"{p}{jtype} {self._mangle(opt.name)} = "
                                 f"{temp} != null ? {value} : {default};")
                else:
                    lines.append(f"{p}{jtype} {self._mangle(opt.name)} = "
                                 f"{temp} != null ? {value} : null;")
            self._handler_binding(
                opt.name, self._mangle(opt.name), None,
                self._option_java_type(opt))
        return lines

    def _emit_command(self, cmd: A.CommandDecl):
        """A slash command: its Commands.slash data (for the
        COMMANDS table) and its execute method."""
        self._need_discord(
            "net.dv8tion.jda.api.interactions.commands.build.Commands")
        self._need_discord(
            "net.dv8tion.jda.api.interactions.commands.build.SlashCommandData")
        self._need_discord(
            "net.dv8tion.jda.api.interactions.commands.build.OptionData")
        self._need_discord(
            "net.dv8tion.jda.api.interactions.commands.OptionType")
        self._need_discord(
            "net.dv8tion.jda.api.events.interaction.command.SlashCommandInteractionEvent")
        desc = cmd.description if cmd.description is not None else cmd.name
        entry = [f"Commands.slash({_java_str(cmd.name)}, "
                 f"{_java_str(desc)})"]
        if cmd.options:
            entry[0] += ".addOptions("
            for i, opt in enumerate(cmd.options):
                comma = "," if i < len(cmd.options) - 1 else ")"
                entry.append(f"      {self._option_builder(opt)}{comma}")
        self._bot_command_data.append(entry)
        method = self._command_method_name(cmd.name)
        self._bot_command_methods_by_name[cmd.name] = method
        lines = [f"  private static void {method}"
                 "(SlashCommandInteractionEvent interaction) {"]
        self._push_handler(
            "command", ["user", "guild", "channel", "interaction"],
            reply="interaction",
            locale="interaction.getUserLocale().getLocale()")
        try:
            lines.extend(self._emit_option_bindings(cmd, 2))
            lines.extend(self._emit_block(cmd.effective_body(), 2,
                                          new_scope=False))
        finally:
            self._pop_handler()
        lines.append("  }")
        self._bot_command_methods.append(lines)

    # ------------------------------------------------------------ events / tasks

    def _emit_event_registration(self, ev: A.EventDecl):
        """A ListenerAdapter override for one event. The system
        channel is a local named `_newrSystemChannel` (not
        `channel` - a Java local with that name would shadow the
        binding in nested blocks); member_remove guards the null
        member first; the message handler ignores the bot's own
        messages."""
        self._need_discord(
            "net.dv8tion.jda.api.hooks.ListenerAdapter")
        if ev.kind == "ready":
            self._need_discord(
                "net.dv8tion.jda.api.events.session.ReadyEvent")
            lines = ["  @Override",
                     "  public void onReady(ReadyEvent event) {"]
            self._push_handler("ready", ["bot"], reply=None, locale=None)
            try:
                lines.extend(self._emit_block(ev.effective_body(), 2,
                                              new_scope=False))
            finally:
                self._pop_handler()
            lines.append("  }")
            self._bot_event_methods.append(lines)
            return
        if ev.kind in ("member_join", "member_remove"):
            self._need_discord(
                "net.dv8tion.jda.api.events.guild.member.GuildMemberJoinEvent")
            self._need_discord(
                "net.dv8tion.jda.api.events.guild.member.GuildMemberRemoveEvent")
            self._need_discord(
                "net.dv8tion.jda.api.entities.channel.middleman.MessageChannel")
            name = ("onGuildMemberJoin" if ev.kind == "member_join"
                    else "onGuildMemberRemove")
            event = ("GuildMemberJoinEvent" if ev.kind == "member_join"
                     else "GuildMemberRemoveEvent")
            lines = ["  @Override",
                     f"  public void {name}({event} event) {{"]
            ctx = self._push_handler(
                ev.kind, ["user", "guild"], reply="system_channel",
                locale="event.getMember().getGuild().getLocale().getLocale()")
            ctx["channel_var"] = "_newrSystemChannel"
            try:
                if ev.kind == "member_remove":
                    lines.append("    if (event.getMember() == null) {")
                    lines.append("      return;")
                    lines.append("    }")
                lines.append("    MessageChannel _newrSystemChannel = "
                             "event.getMember().getGuild()"
                             ".getSystemChannel();")
                lines.append("    if (_newrSystemChannel != null) {")
                lines.extend(self._emit_block(ev.effective_body(), 3,
                                              new_scope=False))
                lines.append("    }")
            finally:
                self._pop_handler()
            lines.append("  }")
            self._bot_event_methods.append(lines)
            return
        if ev.kind == "message":
            self._need_discord(
                "net.dv8tion.jda.api.events.message.MessageReceivedEvent")
            lines = ["  @Override",
                     "  public void onMessageReceived("
                     "MessageReceivedEvent event) {"]
            lines.append("    if (event.getAuthor().getIdLong() == "
                         "event.getJDA().getSelfUser().getIdLong()) {")
            lines.append("      return;")
            lines.append("    }")
            self._push_handler(
                "message", ["message", "user"], reply="message_channel",
                locale="event.getMessage().getGuild() != null ? "
                      "event.getMessage().getGuild().getLocale()"
                      ".getLocale() : null")
            try:
                lines.extend(self._emit_block(ev.effective_body(), 2,
                                              new_scope=False))
            finally:
                self._pop_handler()
            lines.append("  }")
            self._bot_event_methods.append(lines)
            return
        self._fail(f"unknown event kind `{ev.kind}`")

    def _task_fn_name(self, task: A.TaskDecl) -> str:
        """`_every_1h` for a 1h task - the JS emitter's naming
        (a numeric suffix when the same period repeats). The
        name is computed once per task and cached - main() and
        the definition must agree."""
        key = id(task)
        if key in self._task_fn_names:
            return self._task_fn_names[key]
        secs = task.interval_seconds
        if secs % 3600 == 0:
            base = f"_every_{secs // 3600}h"
        elif secs % 60 == 0:
            base = f"_every_{secs // 60}m"
        else:
            base = f"_every_{secs}s"
        count = self._task_fn_name_counts.get(base, 0)
        self._task_fn_name_counts[base] = count + 1
        name = base if count == 0 else f"{base}_{count + 1}"
        self._task_fn_names[key] = name
        return name

    def _emit_task(self, task: A.TaskDecl):
        """A task method (scheduled from main() with
        scheduleAtFixedRate)."""
        name = self._task_fn_name(task)
        lines = [f"  private static void {name}() {{"]
        self._push_handler("task", [], reply=None, locale=None)
        try:
            lines.extend(self._emit_block(task.body, 2,
                                          new_scope=False))
        finally:
            self._pop_handler()
        lines.append("  }")
        self._bot_task_methods.append(lines)
        self._uses_scheduler = True

    def _emit_dispatch(self) -> list[str]:
        """The dispatch overrides: the slash switch (with the
        JS dispatchCommand error reply, acknowledged or not) and
        the per-kind handler-map lookups for components."""
        lines: list[str] = []
        if self._bot_command_data:
            self._need_discord(
                "net.dv8tion.jda.api.events.interaction.command.SlashCommandInteractionEvent")
            lines.append("")
            lines.append("  @Override")
            lines.append("  public void onSlashCommandInteraction("
                         "SlashCommandInteractionEvent interaction) {")
            lines.append("    try {")
            lines.append("      switch (interaction.getName()) {")
            for cmd in self.program.commands:
                method = self._bot_command_methods_by_name[cmd.name]
                lines.append(f"        case {_java_str(cmd.name)}:")
                lines.append(f"          {method}(interaction);")
                lines.append("          break;")
            lines.append("        default:")
            lines.append("          break;")
            lines.append("      }")
            lines.append("    } catch (Exception e) {")
            lines.append("      System.err.println(\"[dispatch] /\" + "
                         "interaction.getName() + \" failed: \" + e);")
            lines.append("      try {")
            lines.append("        String message = \"Something went wrong "
                         "running that command. Try again in a moment.\";")
            lines.append("        if (interaction.isAcknowledged()) {")
            lines.append("          interaction.getHook().sendMessage(message)"
                         ".setEphemeral(true).complete();")
            lines.append("        } else {")
            lines.append("          interaction.reply(message)"
                         ".setEphemeral(true).complete();")
            lines.append("        }")
            lines.append("      } catch (Exception replyError) {")
            lines.append("        System.err.println(\"[dispatch] could not "
                         "send the error reply: \" + replyError);")
            lines.append("      }")
            lines.append("    }")
            lines.append("  }")
        if "button" in self._bot_component_kinds:
            lines.append("")
            lines.append("  @Override")
            lines.append("  public void onButtonInteraction("
                         "ButtonInteractionEvent interaction) {")
            lines.append("    Consumer<ButtonInteractionEvent> handler = "
                         "buttonHandlers.get(interaction.getComponentId());")
            lines.append("    if (handler != null) {")
            lines.append("      handler.accept(interaction);")
            lines.append("    }")
            lines.append("  }")
        if "select" in self._bot_component_kinds:
            lines.append("")
            lines.append("  @Override")
            lines.append("  public void onStringSelectInteraction("
                         "StringSelectInteractionEvent interaction) {")
            lines.append("    Consumer<StringSelectInteractionEvent> handler "
                         "= selectHandlers.get("
                         "interaction.getComponentId());")
            lines.append("    if (handler != null) {")
            lines.append("      handler.accept(interaction);")
            lines.append("    }")
            lines.append("  }")
        if "modal" in self._bot_component_kinds:
            lines.append("")
            lines.append("  @Override")
            lines.append("  public void onModalInteraction("
                         "ModalInteractionEvent interaction) {")
            lines.append("    Consumer<ModalInteractionEvent> handler = "
                         "modalHandlers.get(interaction.getComponentId());")
            lines.append("    if (handler != null) {")
            lines.append("      handler.accept(interaction);")
            lines.append("    }")
            lines.append("  }")
        return lines

    def _emit_main(self, program: A.Program) -> list[str]:
        """main(): the top-level statements first (they run before
        login, like the general emitter), then the token check,
        the store setup, the JDA login, and the task schedules.
        The locals are `_newrToken` / `_newrBuilder` - a top-level
        `let token` is a static field, and a local named `token`
        would shadow it inside main()."""
        lines = ["  public static void main(String[] args) {"]
        self._fn_ctx = []
        lines.extend(self._emit_block(program.stmts, 2, top_level=True))
        lines.append("    String _newrToken = "
                     "System.getenv(\"DISCORD_TOKEN\");")
        lines.append("    if (_newrToken == null || _newrToken.isEmpty()) {")
        lines.append("      throw new IllegalStateException(\"set the "
                     "DISCORD_TOKEN environment variable\");")
        lines.append("    }")
        if program.stores:
            lines.append("    bot = new Bot();")
            lines.append("    bot.setup();")
        self._need_discord("net.dv8tion.jda.api.JDABuilder")
        intents = self._bot_intents(program)
        lines.append("    JDABuilder _newrBuilder = "
                     "JDABuilder.createDefault(_newrToken);")
        if intents:
            self._need_discord(
                "net.dv8tion.jda.api.requests.GatewayIntent")
            lines.append("    _newrBuilder.enableIntents("
                         + ", ".join(f"GatewayIntent.{i}" for i in intents)
                         + ");")
        lines.append(f"    _newrBuilder.addEventListeners(new "
                     f"{self.output_stem}());")
        lines.append("    _newrBuilder.build();")
        for task in program.tasks:
            name = self._task_fn_name(task)
            ms = task.interval_seconds * 1000
            lines.append("    _newrScheduler.scheduleAtFixedRate(() -> {")
            lines.append("      try {")
            lines.append(f"        {name}();")
            lines.append("      } catch (Throwable e) {")
            lines.append("        System.err.println(e);")
            lines.append("      }")
            lines.append(f"    }}, {ms}L, {ms}L, TimeUnit.MILLISECONDS);")
        lines.append("  }")
        return lines

    def _answer_parts(self, value, indent: int):
        """A reply value as (pre-lines, expression, is_embed): an
        embed becomes a local built by _emit_embed_lines; a `?`
        unwraps first (the unwrapped expression, not the
        temporary)."""
        if isinstance(value, A.Propagate):
            unwrapped = self._propagate_unwrapped(value)
            return [], unwrapped, False
        if isinstance(value, A.EmbedExpr):
            lines, name = self._emit_embed_lines(value, indent)
            return lines, name, True
        return [], self.emit_expr(value, 0), False

    def _answer_call(self, kind: str, arg: str, is_embed: bool,
                     ephemeral: bool = False) -> str:
        """The JDA call that answers on `kind`, ending in
        `.complete()` - the blocking equivalent of the JS
        emitter's `await` (the reply happens before the next
        statement, so an edit/followup after a reply is safe).
        `reply` here is the interaction reply; `edit` and
        `followup` go through the hook; the channel kinds send
        to the channel; `timeout_edit` edits the message the
        components were attached to."""
        if kind == "interaction":
            kind = "reply"
        if kind == "reply":
            call = (f"interaction.replyEmbeds({arg})" if is_embed
                    else f"interaction.reply({arg})")
            if ephemeral:
                call += ".setEphemeral(true)"
            return call + ".complete()"
        if kind == "edit":
            if is_embed:
                return (f"interaction.getHook().editOriginalEmbeds({arg})"
                        ".complete()")
            return (f"interaction.getHook().editOriginal({arg})"
                    ".complete()")
        if kind == "followup":
            if is_embed:
                return (f"interaction.getHook().sendMessageEmbeds({arg})"
                        ".complete()")
            return (f"interaction.getHook().sendMessage({arg})"
                    ".complete()")
        if kind == "system_channel":
            var = self._handler_ctx().get("channel_var", "channel")
            if is_embed:
                return f"{var}.sendMessageEmbeds({arg}).complete()"
            return f"{var}.sendMessage({arg}).complete()"
        if kind == "message_channel":
            if is_embed:
                return ("event.getMessage().getChannel()"
                        f".sendMessageEmbeds({arg}).complete()")
            return ("event.getMessage().getChannel()"
                    f".sendMessage({arg}).complete()")
        if kind == "timeout_edit":
            var = self._handler_ctx().get("message_var")
            if var is None:
                self._fail("internal: timeout reply without a message")
            if is_embed:
                return f"{var}.editMessageEmbeds({arg}).complete()"
            return f"{var}.editMessage({arg}).complete()"
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
        call = self._answer_call(ctx["reply"], arg, is_embed,
                                 stmt.ephemeral)
        lines.append(f"{p}{call};")
        return lines

    def _emit_defer(self, stmt, indent: int):
        p = "  " * indent
        if self._handler_ctx()["reply"] != "interaction":
            self._fail("`defer` needs an interaction (a command or "
                       "component handler)")
        return [f"{p}interaction.deferReply().complete();"]

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
            lines.append(f"{p}{channel}.sendMessageEmbeds({arg})"
                         ".complete();")
        else:
            lines.append(f"{p}{channel}.sendMessage({arg}).complete();")
        return lines

    # ------------------------------------------------------------ components

    def _component_method_name(self, label: str, suffix: str) -> str:
        """`buyAgainHandler` for a \"Buy again\" component - unique
        per file (a numeric suffix when the same label repeats)."""
        words = "".join(ch if ch.isalnum() else " " for ch in label).split()
        base = "".join(w.capitalize() for w in words) or "Component"
        base = base[0].lower() + base[1:]
        name = base + suffix
        if name in self._bot_component_names:
            n = 2
            while f"{name}{n}" in self._bot_component_names:
                n += 1
            name = f"{name}{n}"
        self._bot_component_names.add(name)
        return name

    def _component_var_name(self, label: str) -> str:
        words = "".join(ch if ch.isalnum() else " " for ch in label).split()
        return "_".join(w.lower() for w in words) or "component"

    def _new_component_name(self) -> str:
        self._component_counter += 1
        return f"_newr_c{self._component_counter - 1}"

    def _author_id_expr(self, ctx: dict) -> str | None:
        """The invoking user's id in this handler, for the
        `only: author` gate - or None when the handler has no
        user in scope (a ready/task handler)."""
        kind = ctx.get("kind")
        if kind in ("command", "component"):
            return "interaction.getUser().getId()"
        if kind in ("member_join", "member_remove"):
            return "event.getMember().getUser().getId()"
        if kind == "message":
            return "event.getAuthor().getId()"
        return None

    def _author_gate_lines(self, stmt, custom: str, indent: int) \
            -> list[str]:
        """The `only:` gate at the top of a component handler: the
        author gate reads the per-message map (filled in when the
        components are attached); the roles gate reads the
        invoking member's roles. Both answer ephemerally and
        return when the gate fails (the JS messages, verbatim)."""
        p = "  " * indent
        if stmt.only_kind == "author":
            return [
                f"{p}String _newrAuthorId = _componentAuthors.get("
                f"interaction.getMessage().getId() + \":\" + "
                f"{_java_str(custom)});",
                f"{p}if (_newrAuthorId != null && "
                "!interaction.getUser().getId().equals(_newrAuthorId)) {",
                f"{p}  interaction.reply(\"This button is not for you.\")"
                ".setEphemeral(true).complete();",
                f"{p}  return;",
                f"{p}}}",
            ]
        if stmt.only_kind == "roles":
            roles = ", ".join(_java_str(r)
                              for r in (stmt.only_roles or []))
            return [
                f"{p}if (interaction.getMember() == null || "
                "interaction.getMember().getRoles().stream().noneMatch("
                f"role -> java.util.List.of({roles})"
                ".contains(role.getName()))) {",
                f"{p}  interaction.reply(\"You do not have a role that "
                "can use this.\").setEphemeral(true).complete();",
                f"{p}  return;",
                f"{p}}}",
            ]
        return []

    def _emit_button(self, stmt, indent: int):
        p = "  " * indent
        if stmt.style == "link" or stmt.url is not None:
            self._fail("a `link` button cannot have a `do:` body "
                       "(Discord never sends an interaction for it)")
        style = stmt.style or "primary"
        style_method = {"primary": "primary", "secondary": "secondary",
                        "success": "success",
                        "danger": "danger"}.get(style)
        if style_method is None:
            self._fail(f"unknown button style `{style}`")
        var = self._new_component_name()
        row = f"{var}_row"
        custom = stmt.custom_id or self._component_var_name(stmt.label)
        method = self._component_method_name(stmt.label, "Handler")
        self._need_discord(
            "net.dv8tion.jda.api.components.buttons.Button")
        self._need_discord(
            "net.dv8tion.jda.api.components.actionrow.ActionRow")
        self._need_discord(
            "net.dv8tion.jda.api.events.interaction.component.ButtonInteractionEvent")
        handler_lines = [f"  private static void {method}"
                         "(ButtonInteractionEvent interaction) {"]
        self._push_handler(
            "component", ["user", "guild", "channel", "interaction"],
            reply="interaction",
            locale="interaction.getUserLocale().getLocale()")
        try:
            handler_lines.extend(
                self._author_gate_lines(stmt, custom, 2))
            handler_lines.extend(
                self._emit_block(stmt.body, 2, new_scope=False))
        finally:
            self._pop_handler()
        handler_lines.append("  }")
        self._bot_components.append(handler_lines)
        self._bot_component_kinds.add("button")
        self._bot_component_registrations.append(
            f"    buttonHandlers.put({_java_str(custom)}, "
            f"{self.output_stem}::{method});")
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
        timeout = stmt.timeout if stmt.timeout is not None else 180
        self._pending_components.append({
            "var": var, "row": row, "custom": custom,
            "only_kind": stmt.only_kind, "only_roles": stmt.only_roles,
            "timeout": timeout,
            "has_timeout": stmt.on_timeout is not None,
            "on_timeout_lines": on_timeout_lines,
        })
        return [f"{p}ActionRow {row} = ActionRow.of(",
                f"{p}  Button.{style_method}({_java_str(custom)}, "
                f"{_java_str(stmt.label)}));"]

    def _emit_select(self, stmt, indent: int):
        p = "  " * indent
        var = self._new_component_name()
        row = f"{var}_row"
        custom = self._component_var_name(stmt.label)
        method = self._component_method_name(stmt.label, "Handler")
        self._need_discord(
            "net.dv8tion.jda.api.components.selections.StringSelectMenu")
        self._need_discord(
            "net.dv8tion.jda.api.components.selections.SelectOption")
        self._need_discord(
            "net.dv8tion.jda.api.components.actionrow.ActionRow")
        self._need_discord(
            "net.dv8tion.jda.api.events.interaction.component.StringSelectInteractionEvent")
        min_v = stmt.min_values if stmt.min_values is not None else 1
        max_v = stmt.max_values if stmt.max_values is not None else 1
        options = ", ".join(
            f"SelectOption.of({_java_str(o)}, {_java_str(o)})"
            for o in stmt.options)
        handler_lines = [f"  private static void {method}"
                         "(StringSelectInteractionEvent interaction) {"]
        self._push_handler(
            "component", ["user", "guild", "channel", "interaction"],
            reply="interaction",
            locale="interaction.getUserLocale().getLocale()")
        try:
            handler_lines.extend(
                self._author_gate_lines(stmt, custom, 2))
            self._handler_binding(
                "values", "interaction.getValues()",
                "String.join(\", \", interaction.getValues())",
                Type("list", (Type("str"),)))
            handler_lines.extend(
                self._emit_block(stmt.body, 2, new_scope=False))
        finally:
            self._pop_handler()
        handler_lines.append("  }")
        self._bot_components.append(handler_lines)
        self._bot_component_kinds.add("select")
        self._bot_component_registrations.append(
            f"    selectHandlers.put({_java_str(custom)}, "
            f"{self.output_stem}::{method});")
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
        timeout = stmt.timeout if stmt.timeout is not None else 180
        self._pending_components.append({
            "var": var, "row": row, "custom": custom,
            "only_kind": stmt.only_kind, "only_roles": stmt.only_roles,
            "timeout": timeout,
            "has_timeout": stmt.on_timeout is not None,
            "on_timeout_lines": on_timeout_lines,
        })
        return [f"{p}ActionRow {row} = ActionRow.of(",
                f"{p}  StringSelectMenu.create({_java_str(custom)})"
                f".setPlaceholder({_java_str(stmt.label)})"
                f".setMinValues({min_v}).setMaxValues({max_v})"
                f".addOptions({options}).build());"]

    def _emit_modal(self, stmt, indent: int):
        p = "  " * indent
        var = self._new_component_name()
        custom = stmt.custom_id or self._component_var_name(stmt.title)
        method = self._component_method_name(stmt.title, "Handler")
        self._need_discord("net.dv8tion.jda.api.modals.Modal")
        self._need_discord(
            "net.dv8tion.jda.api.components.textinput.TextInput")
        self._need_discord(
            "net.dv8tion.jda.api.components.textinput.TextInputStyle")
        self._need_discord(
            "net.dv8tion.jda.api.components.label.Label")
        self._need_discord(
            "net.dv8tion.jda.api.events.interaction.ModalInteractionEvent")
        lines = [f"{p}Modal {var} = Modal.create({_java_str(custom)}, "
                 f"{_java_str(stmt.title)})"]
        for field in stmt.fields:
            style = ("PARAGRAPH" if field.style == "paragraph"
                     else "SHORT")
            req = "true" if field.required else "false"
            mn = (f".setMinLength({field.min_length})"
                  if field.min_length is not None else "")
            mx = (f".setMaxLength({field.max_length})"
                  if field.max_length is not None else "")
            lines.append(f"{p}  .addComponents(Label.of("
                         f"{_java_str(field.name)}, TextInput.create("
                         f"{_java_str(field.name)}, TextInputStyle.{style})"
                         f".setRequired({req}){mn}{mx}.build()))")
        lines[-1] += ".build();"
        handler_lines = [f"  private static void {method}"
                         "(ModalInteractionEvent interaction) {"]
        self._push_handler(
            "component", ["user", "guild", "channel", "interaction"],
            reply="interaction",
            locale="interaction.getUserLocale().getLocale()")
        try:
            for field in stmt.fields:
                getter = (f"interaction.getValue({_java_str(field.name)})"
                          ".getAsString()")
                self._handler_binding(field.name, getter, getter,
                                      Type("str"))
            handler_lines.extend(
                self._emit_block(stmt.body, 2, new_scope=False))
        finally:
            self._pop_handler()
        handler_lines.append("  }")
        self._bot_components.append(handler_lines)
        self._bot_component_kinds.add("modal")
        self._bot_component_registrations.append(
            f"    modalHandlers.put({_java_str(custom)}, "
            f"{self.output_stem}::{method});")
        lines.append(f"{p}interaction.replyModal({var}).complete();")
        return lines

    def _attach_components(self, lines: list[str]):
        """Attach the pending component rows to the last answer
        line: insert `.addComponents(row, ...)` before
        `.complete()` (`.setComponents(...)` for an edit, which
        only has the setter), extending an existing components
        call when a second component attaches to the same answer.
        Then capture the message (for the author gate and the
        timeout), record the author for `only: author`
        components, and schedule the timeout - disabling the
        rows and running each on_timeout body."""
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
        if not line.endswith(".complete();"):
            self._fail("internal: the answer line does not end with "
                       "`.complete()`")
        comp_call = (".setComponents(" if kind in ("edit", "timeout_edit")
                     else ".addComponents(")
        rows = ", ".join(c["row"] for c in pending)
        existing = line.rfind(comp_call)
        if existing != -1:
            close = line.find(")", existing)
            lines[idx] = line[:close] + ", " + rows + line[close:]
        else:
            lines[idx] = (line[:-len(".complete();")] + comp_call + rows
                          + ").complete();")
        # The message the components live on.
        p = line[:len(line) - len(line.lstrip())]
        first_var = pending[0]["var"]
        ctx = self._handler_ctx_stack[-1] if self._handler_ctx_stack else {}
        inserted = 0
        self._need_discord("net.dv8tion.jda.api.entities.Message")
        if kind == "timeout_edit":
            message_var = ctx.get("message_var")
            if message_var is None:
                self._fail("internal: timeout reply without a message")
        elif kind in ("followup", "edit", "system_channel",
                      "message_channel"):
            message_var = f"{first_var}_message"
            stripped = lines[idx]
            indent_len = len(stripped) - len(stripped.lstrip())
            lines[idx] = (stripped[:indent_len]
                          + f"Message {message_var} = "
                          + stripped[indent_len:])
        else:
            message_var = f"{first_var}_message"
            lines.insert(idx + 1, f"{p}Message {message_var} = "
                         "interaction.getHook().retrieveOriginal()"
                         ".complete();")
            inserted = 1
        # Alias the other components' message vars to the first.
        for c in pending[1:]:
            lines.insert(idx + 1 + inserted,
                         f"{p}Message {c['var']}_message = {message_var};")
            inserted += 1
        # The author gate: record the invoking user per message.
        author_expr = self._author_id_expr(ctx)
        for c in pending:
            if c["only_kind"] == "author":
                if author_expr is None:
                    self._fail("a `button` / `select` with `only: author` "
                               "needs a handler with a user (this "
                               "handler has none)")
                self._uses_author_map = True
                lines.insert(idx + 1 + inserted,
                             f"{p}_componentAuthors.put({message_var}.getId() "
                             f"+ \":\" + {_java_str(c['custom'])}, "
                             f"{author_expr});")
                inserted += 1
        # The timeout: disable the rows, then run each on_timeout
        # body (and forget the author gate for this message).
        if any(c.get("has_timeout") for c in pending):
            timeout = max(c["timeout"] for c in pending)
            self._uses_scheduler = True
            self._extra_imports.add("java.util.concurrent.*")
            sched = [f"{p}_newrScheduler.schedule(() -> {{"]
            sched.append(f"{p}  try {{")
            disabled = ", ".join(f"{c['row']}.asDisabled()"
                                 for c in pending)
            sched.append(f"{p}    {message_var}.editMessageComponents("
                         f"{disabled}).complete();")
            for c in pending:
                if c.get("has_timeout"):
                    for ol in (c.get("on_timeout_lines") or []):
                        if ol.strip():
                            sched.append(f"{p}    {ol}")
                        else:
                            sched.append("")
                    if c["only_kind"] == "author":
                        sched.append(f"{p}    _componentAuthors.remove("
                                     f"{message_var}.getId() + \":\" + "
                                     f"{_java_str(c['custom'])});")
            sched.append(f"{p}  }} catch (Exception e) {{")
            sched.append(f"{p}    System.err.println(\"[timeout] \" + e);")
            sched.append(f"{p}  }}")
            sched.append(f"{p}}}, {timeout}L, TimeUnit.SECONDS);")
            lines[idx + 1 + inserted:idx + 1 + inserted] = sched

    def _handler_ctx(self) -> dict:
        if not self._handler_ctx_stack:
            self._fail("a bot statement was emitted outside a handler")
        return self._handler_ctx_stack[-1]

    def _push_handler(self, kind: str, names: list[str],
                      reply: str | None, locale: str | None):
        """Push a handler context and bind its names into a fresh
        scope. The Java twin of the JS emitter's _BOT_BINDINGS -
        the same names, JDA spellings. The value form is what a
        bare name means; the interpolation form is what `{name}`
        renders as. The event parameter is always named `event`
        (member/message/ready) or `interaction` (command/
        component); bindings read through it, so no handler local
        can shadow a top-level name."""
        ctx = {"kind": kind, "reply": reply, "locale": locale}
        self._handler_ctx_stack.append(ctx)
        self._fn_ctx.append({"return_type": None, "ensures": [],
                             "in_handler": True})
        self._push_scope()
        table = self._BOT_BINDINGS.get(kind, {})
        for name in names:
            value, interp, tname = table[name]
            self._scopes[-1][name] = _Binding(
                java=value, type=Type(tname), kind="handler",
                interp=interp)
        return ctx

    def _pop_handler(self):
        self._pop_scope()
        self._fn_ctx.pop()
        self._handler_ctx_stack.pop()

    def _handler_binding(self, name: str, value: str, interp: str | None,
                         type: Type):
        """Bind a handler-local name (a command option, a select's
        `values`, a modal field) in the current handler scope."""
        self._scopes[-1][name] = _Binding(
            java=value, type=type, kind="handler", interp=interp)


def _java_class_name(stem: str) -> str:
    """Sanitize a file stem into a valid Java class name (the public
    class MUST live in the same-named file, K1). Invalid characters
    become `_`; a leading digit gets a `_` prefix; a Java keyword
    gets a trailing underscore. The CLI uses this for the output
    file name too, so the file and the class always agree."""
    out = []
    for ch in stem:
        if ch.isalnum() or ch in ("_", "$"):
            out.append(ch)
        else:
            out.append("_")
    name = "".join(out) or "Main"
    if name[0].isdigit():
        name = "_" + name
    if name in _JAVA_KEYWORDS:
        name += "_"
    return name


def emit_program_java(program: A.Program, *, main_path: str,
                      sources: dict[str, str],
                      line_comments: bool = False,
                      output_stem: str | None = None) -> EmitResult:
    """Emit a CHECKED program to Java - one `.java` per `.newr` file.

    `main_path` is the input file as passed (D2: the header names
    it); `sources` maps file paths to their text (for diagnostics
    and `--line-comments`). `output_stem` is the `-o` stem (the
    public class name - sanitized to a valid Java identifier). The
    result's `files` maps an output basename (`hello.java`) to its
    text; `main_file` is the main output's basename; `uses_rt` says
    whether any file needs `NewrRt` (the caller copies
    `compiler/newr_rt.java` as `NewrRt.java` next to the output when
    it is true, D4's Java twin)."""
    stem = _java_class_name(output_stem or Path(main_path).stem)
    em = _JavaEmitter(program, main_path=main_path, sources=sources,
                      line_comments=line_comments, output_stem=stem)
    files, uses_rt = em._emit_file_set(program, main_path)
    # `env.args` needs main()'s `args` in the runtime - but only
    # when some file actually uses it (D4: the runtime is copied
    # only when used, so hello stays runtime-free). A module file
    # may use env.args while the main file does not, and the main
    # file is emitted first, so the line is patched in here, after
    # the whole file set is known.
    main_file = stem + ".java"
    if any("NewrRt.envArgs()" in text for text in files.values()):
        main_text = files[main_file]
        anchor = "  public static void main(String[] args) {\n"
        if "setMainArgs(" not in main_text and anchor in main_text:
            files[main_file] = main_text.replace(
                anchor,
                anchor + f"    NewrRt.setMainArgs(\"{stem}\", args);\n",
                1)
    if program.bot is not None:
        # The standalone registration script (J10's Java twin) rides
        # along as a second output file, and the Maven build file
        # (JDA always, sqlite-jdbc only when a store is used - the
        # CLI writes every file in result.files, so the pom lands
        # next to the sources automatically).
        deploy = em._emit_deploy_file(program, stem + ".java")
        files[stem + "_deploy.java"] = deploy
        files["pom.xml"] = _java_bot_pom(stem, uses_sqlite=em.uses_sqlite)
    return EmitResult(files=files,
                      main_file=stem + ".java",
                      uses_rt=uses_rt)


def _java_bot_pom(stem: str, *, uses_sqlite: bool) -> str:
    """The Maven build file for a bot program: JDA 6.7.0, plus
    sqlite-jdbc 3.54.1.0 when a store is used (both versions
    verified live on Maven Central - see
    tests/test_emitter_java_bot.py). Java 17, UTF-8."""
    artifact = "".join(ch.lower() if ch.isalnum() else "-"
                       for ch in stem).strip("-") or "bot"
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        "<!-- Generated by newr. Do not edit by hand. -->",
        '<project xmlns="http://maven.apache.org/POM/4.0.0"',
        '         xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"',
        '         xsi:schemaLocation="http://maven.apache.org/POM/4.0.0 '
        'http://maven.apache.org/xsd/maven-4.0.0.xsd">',
        "  <modelVersion>4.0.0</modelVersion>",
        "  <groupId>newr</groupId>",
        f"  <artifactId>{artifact}</artifactId>",
        "  <version>1.0.0</version>",
        "  <properties>",
        "    <maven.compiler.source>17</maven.compiler.source>",
        "    <maven.compiler.target>17</maven.compiler.target>",
        "    <project.build.sourceEncoding>UTF-8</project.build.sourceEncoding>",
        "  </properties>",
        "  <dependencies>",
        "    <dependency>",
        "      <groupId>net.dv8tion</groupId>",
        "      <artifactId>JDA</artifactId>",
        "      <version>6.7.0</version>",
        "    </dependency>",
    ]
    if uses_sqlite:
        lines += [
            "    <dependency>",
            "      <groupId>org.xerial</groupId>",
            "      <artifactId>sqlite-jdbc</artifactId>",
            "      <version>3.54.1.0</version>",
            "    </dependency>",
        ]
    lines += [
        "  </dependencies>",
        "  <build>",
        "    <plugins>",
        "      <plugin>",
        "        <groupId>org.apache.maven.plugins</groupId>",
        "        <artifactId>maven-compiler-plugin</artifactId>",
        "        <version>3.11.0</version>",
        "        <configuration>",
        "          <release>17</release>",
        "        </configuration>",
        "      </plugin>",
        "    </plugins>",
        "  </build>",
        "</project>",
    ]
    return "\n".join(lines) + "\n"


# `emit_program` is the name the tests and the CLI use; the `_java`
# suffix marks the implementation (the Python emitter's entry is
# `emit_program` in compiler/emitter.py).
emit_program = emit_program_java
