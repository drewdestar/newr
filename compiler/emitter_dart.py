"""The Java emitter (M4, SPEC.md §18).

``newr build --target dart`` lowers a *checked* program (the front
end has already run: parse -> resolve -> check) to readable Java -
one `.newr` file -> one `.dart` file, structure preserved, user names
verbatim, helpers only in `newr_rt.dart` (copied next to the output
as `NewrRt.dart` when used - javac requires the file name to match
the public class, so the CLI renames on copy, K1).

Readability is a requirement, not a nicety: the output is code a
human would write - the Java goldens (`goldens/dart/`) are the style
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

from dataclasses import dataclass, field, fields as _dc_fields
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
# the `main` method, its `args` parameter, and the dart.lang type
# names the emitter writes (`String`, `int`, ...) must not be
# shadowed by a user struct/enum/fn name in the same file.
_RT_HELPERS = frozenset({
    "add", "sub", "mul", "neg", "powInt", "powFloat", "tdiv", "tmod",
    "fmod", "fdiv", "roundHalfAway", "toInt", "toFloat", "toBool",
    "toStr", "floatStr", "checkInt", "asError", "optChain", "coalesce",
    "listGet", "listIndex", "listContains", "setRemove", "unwrapOpt",
    "tryResult", "timeFormat", "fileRead", "fileWrite", "fileAppend",
    "fileExists", "randomInt", "randomChoice", "randomShuffle",
    "eq", "range", "print", "input", "ok", "err", "chars", "splitStr",
    "strIndex", "strCmp", "absLong", "lenOf", "dictOf", "envGet", "envArgs",
    "hashOf", "structHash", "roundHalfAwayN", "printErr",
})
_RT_CLASSES = frozenset({
    "NewrError", "NewrOverflowError", "NewrDivisionByZeroError",
    "Result", "NewrHttp", "NewrJson", "Widget",
})
_DART_CORE_TYPES = frozenset({
    "String", "Object", "int", "double", "bool", "num", "List",
    "Map", "Set", "Function", "Future", "Stream", "Duration",
    "DateTime", "Null", "Never", "dynamic", "Iterable", "Comparable",
    "Exception", "Error", "Type", "Symbol", "RegExp", "Uri",
    "BigInt",
})
_EMITTER_RESERVED = frozenset({
    "main", "args", "NewrRt", "Bot", "Store", "store", "economy",
    "commands", "modalHandlers", "bot",
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
    "_newrBot", "_newrGuild", "newrCommandBuilders",
    "_newrPassInt", "_newrPassDouble", "_newrClient", "_newrCommands",
    "_componentAuthors", "buttonHandlers", "selectHandlers",
    "newrInit",
}) | _RT_CLASSES | _DART_CORE_TYPES

# Dart reserved words + contextual keywords the emitter writes in
# positions where they would collide.
_DART_KEYWORDS = frozenset({
    "abstract", "as", "assert", "async", "await", "base", "break",
    "case", "catch", "class", "const", "continue", "covariant",
    "default", "deferred", "do", "dynamic", "else", "enum", "export",
    "extends", "extension", "external", "factory", "false", "final",
    "finally", "for", "Function", "get", "hide", "if", "implements",
    "import", "in", "interface", "is", "late", "library", "mixin",
    "new", "null", "of", "on", "operator", "part", "required",
    "rethrow", "return", "sealed", "set", "show", "static", "super",
    "switch", "sync", "this", "throw", "true", "try", "type",
    "typedef", "var", "void", "when", "while", "with", "yield",
})


def _dart_str(value: str) -> str:
    """Render a newr string value as a Dart string literal.

    Always double quotes. Non-ASCII printable characters are
    emitted literally (UTF-8 source); control characters use their
    escapes. `$` is escaped (Dart would interpolate it); `\\b` /
    `\\f` are NOT valid Dart escapes, so they become `\\x08` /
    `\\x0c`.
    """
    out = []
    for ch in value:
        o = ord(ch)
        if ch == "\\":
            out.append("\\\\")
        elif ch == '"':
            out.append('\\"')
        elif ch == "$":
            out.append("\\$")
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\t":
            out.append("\\t")
        elif ch == "\r":
            out.append("\\r")
        elif ch == "\b":
            out.append("\\x08")
        elif ch == "\f":
            out.append("\\x0c")
        elif o < 0x20 or o == 0x7F:
            out.append(f"\\x{o:02x}")
        else:
            out.append(ch)
    return '"' + "".join(out) + '"'

def _dart_float(value: float) -> str:
    """Render a float literal. Python repr is also a valid Java
    double literal ("1e+16", "5e-324", "1.5"). The non-finite
    spellings are not literals in Java, so they become the
    double constants (an out-of-range literal parses to inf in
    Python and would otherwise emit `inf`, a compile error)."""
    if value != value:  # NaN
        return "double.nan"
    if value == float("inf"):
        return "double.infinity"
    if value == float("-inf"):
        return "double.negativeInfinity"
    return repr(value)


# ------------------------------------------------------------ http async scan
# Dart's `dart:io` HTTP APIs are async only (there is no sync
# HttpClient/HttpServer), while newr `http.get/post/request/serve`
# read as sync calls. The emitter bridges the gap by making the
# call sites async: a user fn that (transitively) fetches or
# serves is emitted as `Future<T> name(...) async`, its call sites
# get `await`, and main()/newrInit() become async when their
# statements fetch, serve, or call such a fn. `http.route` and
# `json.*` are sync and need none of this. The scan below is
# purely syntactic (it runs on the checked AST before emission);
# a lambda that fetches may go async only where its Future fn
# type is known: as the handler argument of http.route, or as a
# let-bound lambda (its declared type is Future-wrapped and its
# call sites await it). Any other lambda (an argument to another
# fn, a value in a collection) fails loudly instead of emitting
# broken Dart.

_HTTP_ASYNC_METHODS = frozenset({"get", "post", "request", "serve"})


def _dart_is_async_http_call(node: A.Call, program: A.Program,
                             async_fns: set[str],
                             async_lambdas: set[str],
                             memo: dict) -> bool:
    """True when `node` is a call that must be awaited: an
    `http.get/post/request/serve` call, a call to a user fn in
    `async_fns`, a call to a let-bound async lambda in
    `async_lambdas`, or a call to an async fn of an imported file
    module."""
    func = node.func
    if isinstance(func, A.Member) and isinstance(func.obj, A.Name):
        if func.obj.name == "http" and func.name in _HTTP_ASYNC_METHODS:
            return True
        sub = program.modules.get(func.obj.name)
        if sub is not None:
            sub_fns, _sub_lams = _dart_async_info(sub, memo)
            if func.name in sub_fns:
                return True
        return False
    if isinstance(func, A.Name):
        return func.name in async_fns or func.name in async_lambdas
    return False


def _dart_walk_needs_async(node, program: A.Program,
                           async_fns: set[str], async_lambdas: set[str],
                           memo: dict, descend_lambda: bool) -> bool:
    """True when `node` (an expr, stmt, or list of them) directly
    contains a call that must be awaited. With `descend_lambda`
    False, lambda bodies are opaque (a lambda is its own async
    unit); with True, the outermost lambda's body is scanned but
    nested lambdas stay opaque."""
    if node is None or isinstance(node, (str, int, float, bool)):
        return False
    if isinstance(node, (list, tuple, set)):
        return any(_dart_walk_needs_async(x, program, async_fns,
                                          async_lambdas, memo,
                                          descend_lambda)
                   for x in node)
    if isinstance(node, dict):
        return any(_dart_walk_needs_async(v, program, async_fns,
                                          async_lambdas, memo,
                                          descend_lambda)
                   for v in node.values())
    if isinstance(node, A.Lambda):
        if not descend_lambda:
            return False
        body: object = node.expr_body if node.expr_body is not None \
            else node.body
        return _dart_walk_needs_async(body, program, async_fns,
                                      async_lambdas, memo, False)
    if isinstance(node, A.Call):
        if _dart_is_async_http_call(node, program, async_fns,
                                    async_lambdas, memo):
            return True
    fields = getattr(node, "__dataclass_fields__", None)
    if fields is not None:
        return any(_dart_walk_needs_async(getattr(node, f.name),
                                          program, async_fns,
                                          async_lambdas, memo,
                                          descend_lambda)
                   for f in _dc_fields(node))
    return False


def _dart_async_info(program: A.Program,
                     memo: dict | None = None
                     ) -> tuple[set[str], set[str]]:
    """(async_fns, async_lambda_names) for one program: the user
    fns that must be emitted async, and the let-bound route-handler
    lambdas that must be emitted async. Computed to a fixed point
    (a fn calling an async fn is async; a handler lambda calling
    an async fn is async), memoized per program id in `memo`."""
    if memo is None:
        memo = {}
    key = id(program)
    if key in memo:
        return memo[key]
    memo[key] = (set(), set())
    async_fns: set[str] = set()
    async_lambdas: set[str] = set()
    # Let-bound lambdas by name (first binding wins), for the
    # route-handler scan below.
    lambdas: dict[str, A.Lambda] = {}

    def collect_lambdas(node) -> None:
        if node is None or isinstance(node, (str, int, float, bool)):
            return
        if isinstance(node, (list, tuple, set)):
            for x in node:
                collect_lambdas(x)
            return
        if isinstance(node, dict):
            for v in node.values():
                collect_lambdas(v)
            return
        if isinstance(node, (A.Let, A.Const)) \
                and isinstance(node.value, A.Lambda):
            lambdas.setdefault(node.name, node.value)
        fields = getattr(node, "__dataclass_fields__", None)
        if fields is not None:
            for f in _dc_fields(node):
                collect_lambdas(getattr(node, f.name))

    collect_lambdas(program.stmts)
    for fn in program.functions:
        collect_lambdas(fn.body)
    changed = True
    while changed:
        changed = False
        for fn in program.functions:
            if fn.name in async_fns:
                continue
            if _dart_walk_needs_async(fn.body, program, async_fns,
                                      async_lambdas, memo, False):
                async_fns.add(fn.name)
                changed = True
        # Let-bound lambdas whose body needs awaiting (a route
        # handler, or any stored lambda that fetches): their
        # declared type becomes the Future fn type, and their
        # call sites await them.
        for name, lam in lambdas.items():
            if name in async_lambdas:
                continue
            if _dart_walk_needs_async(lam, program, async_fns,
                                      async_lambdas, memo, True):
                async_lambdas.add(name)
                changed = True
    result = (async_fns, async_lambdas)
    memo[key] = result
    return result


def _dart_entry_needs_async(program: A.Program,
                            memo: dict | None = None) -> bool:
    """True when a file's entry point (main() or newrInit()) must
    be async: its own top-level statements need awaiting, or an
    imported file module it initializes needs an async newrInit()."""
    if memo is None:
        memo = {}
    async_fns, async_lambdas = _dart_async_info(program, memo)
    if _dart_walk_needs_async(program.stmts, program, async_fns,
                              async_lambdas, memo, False):
        return True
    for imp in program.imports:
        if imp.path is None:
            continue
        sub = program.modules.get(imp.bound_name)
        if sub is None or not sub.stmts:
            continue
        if _dart_entry_needs_async(sub, memo):
            return True
    return False


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

    `dart` is the Java name (possibly renamed); `interp` is the form
    used inside an interpolation (for handler bindings: the mention /
    name form); `value` is the form used as a value. For ordinary
    variables all three are the Java name. `kind` is one of
    "param" / "local" / "global-let" / "fn" / "struct" / "enum" /
    "module" / "store" / "handler" / "builtin" / "named-module" /
    "file-module". `module` is the real module name for a module
    binding (the `dart` name is the alias when the import is
    aliased).
    """

    dart: str
    type: Type | None = None
    kind: str = "local"
    interp: str | None = None
    module: str | None = None

    @property
    def value(self) -> str:
        return self.dart

    def interp_form(self) -> str:
        return self.interp if self.interp is not None else self.dart


class _DartEmitter:
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
        # This file uses `db.*` (set by `_emit_db_call`) — the
        # `NewrDb` helper class is emitted into the file only then.
        self._file_uses_db: bool = False
        self.uses_http: bool = False
        # This file uses `http.*` (set by `_emit_http_call`) — the
        # `NewrHttp` helper class is emitted into the file only then.
        self._file_uses_http: bool = False
        self.uses_json: bool = False
        # This file uses `json.*` (set by `_emit_json_call`) — the
        # `NewrJson` helper class is emitted into the file only then.
        self._file_uses_json: bool = False
        # Async-scan results for the file being emitted (computed
        # in `_emit_one`): user fns that must be emitted async,
        # let-bound lambdas that must be emitted async, and
        # whether main()/newrInit() must be async.
        self._async_fns: set[str] = set()
        self._async_lambda_names: set[str] = set()
        self._entry_async: bool = False
        # Async context stack for emission: True while emitting
        # inside an async fn / async main / async lambda (where
        # `await` is legal). A fetch/serve call emitted with the
        # top False is an emitter bug or an unsupported lambda,
        # and fails loudly instead of emitting broken Dart.
        self._async_ctx: list[bool] = [False]
        # Depth of async-lambda emission: while emitting the
        # handler argument of http.route, or the value of a
        # let-bound lambda that fetches, the lambda may go async;
        # anywhere else it fails with a clear message.
        self._async_lambda_depth: int = 0
        # Memo for per-program async info (keyed by id(program)),
        # shared across the file set so a file-module fn's
        # async-ness is computed once.
        self._async_memo: dict = {}
        self.uses_math: bool = False
        # File-module imports referenced by this file:
        # bound name -> file stem (for `import '<stem>.dart'
        # as <bound>;`).
        self.dart_imports: dict[str, str] = {}
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
        # dart.util.concurrent.* for the scheduler.
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
        if name in _DART_KEYWORDS or name in _EMITTER_RESERVED:
            return name + "_"
        return name

    def _bind(self, name: str, type: Type | None = None,
              kind: str = "local", interp: str | None = None) -> _Binding:
        """Bind `name` in the current scope.

        Java has real block scope (`int x` in an `if` block does
        not clobber the outer name), so - unlike the Python emitter
        - shadowing an enclosing name needs NO renaming: the inner
        binding is a different variable, exactly as newr reads it.
        Only keyword / emitter-reserved mangling applies (D20).
        """
        dart = self._mangle(name)
        binding = _Binding(dart=dart, type=type, kind=kind, interp=interp)
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
        dart = base
        n = 1
        while dart in self._fn_used:
            dart = f"{base}_{n}"
            n += 1
        self._fn_used.add(dart)
        binding = _Binding(dart=dart, type=type, kind=kind)
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
        dart = self._mangle(name)
        if dart in self._module_used:
            n = 1
            while f"{dart}_{n}" in self._module_used:
                n += 1
            dart = f"{dart}_{n}"
        self._module_used.add(dart)
        binding = _Binding(dart=dart, type=type, kind=kind)
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

    def _need_http(self):
        # dart:io for the server/client plus dart:convert for
        # utf8 (bodies are UTF-8 text) and dart:async for the
        # fetch timeout (TimeoutException, Future.timeout). The
        # helpers use NewrError and NewrRt.floatStr from
        # newr_rt.dart, so the runtime file must be copied (the
        # "floatStr" marker). The http helpers also build response
        # structs through NewrJson (buildFromShape/readField), so
        # using http implies using json: the NewrJson class must be
        # emitted too.
        self.uses_http = True
        self._file_uses_http = True
        self.uses_json = True
        self._file_uses_json = True
        self.uses_io = True
        self.uses_convert = True
        self.uses_async = True
        self._need_rt("floatStr")

    def _need_json(self):
        # dart:convert for jsonEncode/jsonDecode (string escaping
        # and the JSON parse); the encoder uses NewrRt.floatStr
        # for the pinned float rule, so the runtime file must be
        # copied.
        self.uses_json = True
        self._file_uses_json = True
        self.uses_convert = True
        self._need_rt("floatStr")

    def _need_math(self):
        self.uses_math = True

    def _render_imports(self) -> list[str]:
        """The import block, Dart style: `dart:` imports first,
        then `package:` imports, then the runtime (`newr_rt.dart`,
        only when a helper is used - usage-tracked, Gleam-style),
        then file modules (`import '<stem>.dart' as <bound>;` -
        the bound name is the reference in the emitted code)."""
        lines: list[str] = []
        if getattr(self, "uses_io", False):
            lines.append("import 'dart:io';")
        if getattr(self, "uses_http", False):
            # A second import of dart:io under the `io` prefix:
            # the `NewrHttp` helpers must name dart:io's
            # HttpRequest/HttpResponse, which collide with the
            # user's own HttpRequest/HttpResponse structs (the
            # local structs shadow the unprefixed import).
            lines.append("import 'dart:io' as io;")
        if getattr(self, "uses_convert", False):
            lines.append("import 'dart:convert';")
        if getattr(self, "uses_async", False):
            lines.append("import 'dart:async';")
        if self.uses_math:
            lines.append("import 'dart:math' as math;")
        if self.uses_discord:
            lines.append("import 'package:nyxx/nyxx.dart';")
            lines.append("import 'package:nyxx_commands/nyxx_commands.dart';")
        if self.uses_sqlite:
            lines.append("import 'package:sqlite3/sqlite3.dart';")
        if self.rt_helpers:
            lines.append("import 'newr_rt.dart';")
        for bound in sorted(self.dart_imports):
            stem = self.dart_imports[bound]
            lines.append(f"import '{stem}.dart' as {bound};")
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

    def _dart_type(self, t: Type | None) -> str:
        """The Dart type for a newr type in a declaration position
        (a local, parameter, field or return type). Dart has no
        boxing distinction: `int` works in generics directly.
        Nullable forms take `?`. A `none` value is `Null` (only
        `null` inhabits it); a `none` RETURN is `void`, handled by
        the fn emitter, never here."""
        if t is None:
            self._fail("an expression has no type: the emitter cannot "
                       "handle it here")
        q = "?" if t.nullable else ""
        if t.name == "int":
            return "int" + q
        if t.name == "float":
            return "double" + q
        if t.name == "bool":
            return "bool" + q
        if t.name == "str":
            return "String" + q
        if t.name == "none":
            return "Null"
        if t.name == "object":
            # The union type of conflicting enum variant fields.
            return "Object?"
        if t.name == "__unknown__":
            # The checker leaves `__unknown__` params on a bare
            # builtin used as a value (`let f = len`); it is not a
            # real type. In a declaration position it must become
            # the generic Object?, matching the generic fallback in
            # _emit_builtin_value (type_model.newr, dart 74/75,
            # 7 Nov). Mirrors the java/cs Object/object fix.
            return "Object?"
        if t.name == "error":
            return "NewrError" + q
        if t.name == "Widget":
            # The opaque ui value type (SPEC.md section 12.8): the
            # `Widget` class in newr_rt.dart. The marker forces the
            # runtime import when a Widget appears in a type
            # position with no ui call in the same file.
            self._need_rt("uiText")
            return "Widget" + q
        if t.name == "list":
            return f"List<{self._dart_type(t.params[0])}>" + q
        if t.name == "dict":
            return ("Map<String, "
                    f"{self._dart_type(t.params[1])}>" + q)
        if t.name == "set":
            return f"Set<{self._dart_type(t.params[0])}>" + q
        if t.name == "Result":
            return ("Result<"
                    f"{self._dart_type(t.params[0])}, "
                    f"{self._dart_type(t.params[1])}>" + q)
        if t.name == "fn":
            return self._dart_fn_type(t)
        if t.name == "duration":
            return "int" + q
        if t.name in ("user", "member", "guild", "channel", "role",
                      "interaction", "embed", "message"):
            return self._dart_entity_type(t.name) + q
        # A struct or enum: the (possibly module-qualified) class.
        return self._struct_dart_name(t.name) + q

    def _dart_fn_type(self, t: Type) -> str:
        """`fn(A, B) -> R` -> `R Function(A, B)`. Dart function
        types have no arity limit (unlike Java's Fn0–Fn8), so no
        E4001 here. A `none` return is `void`."""
        params = t.params[:-1]
        ret = t.params[-1]
        args = ", ".join(self._dart_type(p) for p in params)
        ret_s = "void" if _is_none(ret) else self._dart_type(ret)
        text = f"{ret_s} Function({args})"
        if t.nullable:
            text += "?"
        return text

    def _dart_fn_type_async(self, t: Type) -> str:
        """The async twin of `_dart_fn_type`: `fn(A, B) -> R` ->
        `Future<R> Function(A, B)`, for a lambda or fn whose body
        awaits (an http fetch/serve). A `none` return is
        `Future<void>`."""
        params = t.params[:-1]
        ret = t.params[-1]
        args = ", ".join(self._dart_type(p) for p in params)
        ret_s = "void" if _is_none(ret) else self._dart_type(ret)
        return f"Future<{ret_s}> Function({args})"

    def _dart_entity_type(self, name: str) -> str:
        """A Discord entity type (SPEC §7.1) -> its nyxx class.
        Only reachable in bot files. (Verified live against
        nyxx 6.9.1 - see tests/test_emitter_dart.py.)"""
        mapping = {
            "user": "User",
            "member": "Member",
            "guild": "Guild",
            # The sendable text channel (a channel option is a
            # GuildTextChannel, which IS a TextChannel).
            "channel": "TextChannel",
            "role": "Role",
            # The common supertype of the command / component /
            # modal interactions (each has its own concrete type,
            # so no single one is right here).
            "interaction": "Interaction<dynamic>",
            # MessageBuilder.embeds takes builders directly, so
            # the newr embed type IS the builder.
            "embed": "EmbedBuilder",
            "message": "Message",
        }
        self._need_discord(mapping[name])
        return mapping[name]

    def _struct_dart_name(self, name: str) -> str:
        """The Dart class name for a struct/enum type: bare in its
        own file, `<bound>.Name` when imported (the import prefix
        is the BOUND name - the alias when the import is aliased -
        and the import line is recorded, so the file compiles).
        An imported struct/enum reached only through its TYPE is
        found by searching the imported file modules for the
        declaration (conformance import_enum)."""
        binding = self._lookup(name)
        if binding is not None and binding.kind in ("struct", "enum"):
            if binding.module:
                return f"{binding.module}.{binding.dart}"
            return binding.dart
        for bound, sub in self.program.modules.items():
            for decl in list(sub.structs) + list(sub.enums):
                if decl.name == name:
                    stem = self.imported_modules.get(bound, bound)
                    self.dart_imports[bound] = stem
                    return f"{bound}.{self._mangle(name)}"
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
                return (f"<{self._dart_type(elem)}>[]",
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
            return (f"<{self._dart_type(elem)}>[{parts}]",
                    _PREC["primary"])
        if isinstance(node, A.DictLit):
            if not node.pairs:
                val = self._type_of(node).params[1]
                return (f"<String, {self._dart_type(val)}>{{}}",
                        _PREC["primary"])
            val = self._type_of(node).params[1]
            parts = []
            for k, v in node.pairs:
                parts.append(
                    f"{self.emit_expr(k, 0)}: "
                    f"{self.emit_expr(v, 0, fn_target=val if _is_fn(val) else None)}")
            # A Dart map literal: insertion order, null values
            # allowed when V is nullable.
            return (f"<String, {self._dart_type(val)}>"
                    f"{{{', '.join(parts)}}}",
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
            inner_t = self._type_of(node).params[0]
            if _dart_walk_needs_async(node.expr, self.program,
                                      self._async_fns,
                                      self._async_lambda_names,
                                      self._async_memo, False):
                # `try http.get(...)`: the fetch awaits, so the
                # sync `tryResult(() => ...)` closure cannot hold
                # it. Inline the async equivalent (an async IIFE
                # that Ok-wraps the value and catches into err).
                self._need_rt("ok")
                self._need_rt("err")
                self._need_rt("asError")
                self._async_ctx.append(True)
                try:
                    inner = self.emit_expr(
                        node.expr, 0,
                        fn_target=inner_t if _is_fn(inner_t) else None)
                finally:
                    self._async_ctx.pop()
                # Explicit type arguments on ok/err: without them
                # the two branches unify to Result<dynamic, ...>
                # (the err branch carries no value to infer T
                # from) and the assignment to Result[T, ...]
                # fails. inner_t is the Ok type.
                dart_t = self._dart_type(inner_t)
                return (f"(await (() async {{ try {{ return "
                        f"NewrRt.ok<{dart_t}>({inner}); }} catch (e) "
                        f"{{ return NewrRt.err<{dart_t}>"
                        f"(NewrRt.asError(e)); }} }})())",
                        _PREC["primary"])
            self._need_rt("tryResult")
            inner = self.emit_expr(
                node.expr, 0,
                fn_target=inner_t if _is_fn(inner_t) else None)
            return f"NewrRt.tryResult(() => {inner})", _PREC["primary"]
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
            # -9223372036854775808 has no positive literal in Java
            # (`9223372036854775808L` is a compile error); the
            # unary-minus path special-cases it, and a bare literal
            # that large is rejected by the checker first.
            if value > 9223372036854775807:
                self._fail(
                    "integer literal out of int64 range",
                    span=node.span)
            return f"{value}", _PREC["primary"]
        if isinstance(value, float):
            return _dart_float(value), _PREC["primary"]
        if isinstance(value, str):
            return _dart_str(value), _PREC["primary"]
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
                return binding.value, _PREC["primary"]
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
        # are small lambdas with explicitly typed parameters (D1:
        # Dart lambdas carry their types, so no cast is ever
        # needed). The parameter types come from the context
        # (fn_target) when known; the fallback forms are the int
        # ones.
        def _param_types(n, fallback):
            if _is_fn(fn_target) and len(fn_target.params) - 1 == n:
                if any(p.name == "__unknown__"
                       for p in fn_target.params):
                    # A bare builtin's type carries `__unknown__`
                    # params (the checker never resolves them
                    # without a call); fall back to the generic
                    # form instead of emitting `__unknown__`
                    # (type_model.newr, dart 74/75, 7 Nov).
                    return fallback
                return [self._dart_type(p)
                        for p in fn_target.params[:-1]]
            return fallback

        if name == "len":
            self._need_rt("lenOf")
            return ("((Object? x) => NewrRt.lenOf(x))",
                    _PREC["primary"])
        if name == "min":
            a, b = _param_types(2, ["int", "int"])
            if a == "String":
                self._need_rt("strCmp")
                return (("((String x, String y) => "
                         "NewrRt.strCmp(x, y) < 0 ? x : y)"),
                        _PREC["primary"])
            return (f"(({a} x, {b} y) => x < y ? x : y)",
                    _PREC["primary"])
        if name == "max":
            a, b = _param_types(2, ["int", "int"])
            if a == "String":
                self._need_rt("strCmp")
                return (("((String x, String y) => "
                         "NewrRt.strCmp(x, y) < 0 ? y : x)"),
                        _PREC["primary"])
            return (f"(({a} x, {b} y) => x < y ? y : x)",
                    _PREC["primary"])
        if name == "abs":
            # int -> absLong, float -> .abs(); the context type
            # decides (the fallback is the int form).
            if fn_target is not None and _is_fn(fn_target) and \
                    _is_float(fn_target.params[0]):
                return ("((double x) => x.abs())", _PREC["primary"])
            self._need_rt("absLong")
            return ("((int x) => NewrRt.absLong(x))", _PREC["primary"])
        if name == "round":
            # round(x) -> int vs round(x, n) -> double are two
            # runtime methods (Dart has no overloading); the
            # context's arity decides which tear-off this is.
            if _is_fn(fn_target) and len(fn_target.params) - 1 == 2:
                self._need_rt("roundHalfAwayN")
                return "NewrRt.roundHalfAwayN", _PREC["primary"]
            self._need_rt("roundHalfAway")
            return "NewrRt.roundHalfAway", _PREC["primary"]
        if name == "range":
            self._need_rt("range")
            return "NewrRt.range", _PREC["primary"]
        if name == "set":
            return ("((List<Object?> xs) => Set.of(xs))",
                    _PREC["primary"])
        if name == "ok":
            self._need_rt("ok")
            return "NewrRt.ok", _PREC["primary"]
        if name == "err":
            self._need_rt("err")
            return "NewrRt.err", _PREC["primary"]
        self._fail(f"builtin `{name}` is not a value")

    # ------------------------------------------------------------ interpolation

    def _emit_interp(self, node: A.Interp) -> tuple[str, int]:
        """A newr interpolation becomes Java string concatenation
        (`"a" + x + "b"`): literal parts are string literals, each
        expression part renders through `_emit_interp_part`."""
        parts: list[str] = []
        for part in node.parts:
            if part.text is not None:
                parts.append(_dart_str(part.text))
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
        (a int concatenates as its digits; a str is itself);
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
        if _is_str(t):
            return self.emit_expr(expr, 0)
        if _is_int(t):
            self._need_rt("toStr")
            return f"NewrRt.toStr({self.emit_expr(expr, 0)})"
        if t.nullable:
            self._need_rt("toStr")
            return f"NewrRt.toStr({self.emit_expr(expr, 0)})"
        if t.name in ("list", "dict", "set", "Result") or \
                t.name in self.structs or t.name in self.enums:
            self._need_rt("toStr")
            return f"NewrRt.toStr({self.emit_expr(expr, 0)})"
        # A Discord entity (or anything else): render through
        # toStr - Dart has no implicit toString in a `+` chain.
        self._need_rt("toStr")
        return f"NewrRt.toStr({self.emit_expr(expr, 0)})"

    # ------------------------------------------------------------ operators

    def _emit_binop(self, node: A.BinOp) -> tuple[str, int]:
        op = node.op
        if op == "??":
            left = self.emit_expr(node.left, 0)
            right = self.emit_expr(node.right, 0)
            self._need_rt("coalesce")
            return (f"NewrRt.coalesce({left}, () => {right})",
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
                return (f"NewrRt.coalesce({left}, () => {right})",
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
                # nullable int `1000L == 1000L` is false) and on a
                # String it is identity too - use null-safe
                # Objects.equals unless BOTH sides are non-nullable
                # primitives, where bare `==` is correct.
                if lt.nullable or rt.nullable or \
                        _is_str(lt) or _is_str(rt) or \
                        _is_error(lt) or _is_error(rt) or _is_fn(lt) or \
                        _is_fn(rt):
                    expr = f"({left} == {right})"
                    if op == "!=":
                        expr = f"!{expr}"
                    return expr, _PREC["primary"]
                jsop = "==" if op == "==" else "!="
                return f"{left} {jsop} {right}", prec
            # Ordering: strings compare by Unicode code point
            # (NewrRt.strCmp); numbers compare bare. Dart's
            # String.compareTo compares UTF-16 code units, which
            # orders an astral character below BMP characters at
            # U+E000 and above (L11, type_model.newr, 7 Nov).
            if _is_str(lt) or _is_str(rt):
                self._need_rt("strCmp")
                cmp = {"<": "< 0", "<=": "<= 0", ">": "> 0",
                       ">=": ">= 0"}[op]
                return f"(NewrRt.strCmp({left}, {right}) {cmp})", prec
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
            return "-9223372036854775808", _PREC["unary"]
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
                if binding is None and (obj.name in MODULES
                                       or obj.name in ("db", "http",
                                                      "json", "ui")):
                    # A named module used without an import
                    # (N-09/N-10): the name is both the real module
                    # and the reference.
                    fake = _Binding(dart=obj.name, kind="module",
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
            args = [self.emit_expr(a, 0) for a in node.args]
            if len(args) == 2:
                self._need_rt("roundHalfAwayN")
                return f"NewrRt.roundHalfAwayN({', '.join(args)})", \
                    _PREC["primary"]
            self._need_rt("roundHalfAway")
            return f"NewrRt.roundHalfAway({', '.join(args)})", \
                _PREC["primary"]
        if name == "range":
            self._need_rt("range")
            args = [self.emit_expr(a, 0) for a in node.args]
            return f"NewrRt.range({', '.join(args)})", _PREC["primary"]
        if name == "set":
            arg = self.emit_expr(node.args[0], 0)
            return f"Set.of({arg})", _PREC["primary"]
        if name == "ok":
            self._need_rt("ok")
            t = self._type_of(node)
            # The runtime's ok is `ok<T>(T value)` - ONE type
            # parameter (the error type is fixed to NewrError);
            # emitting two type arguments does not compile
            # (conformance opt_methods).
            return (f"NewrRt.ok<{self._dart_type(t.params[0])}>("
                    f"{self.emit_expr(node.args[0], 0)})",
                    _PREC["primary"])
        if name == "err":
            self._need_rt("err")
            t = self._type_of(node)
            return (f"NewrRt.err<{self._dart_type(t.params[0])}>("
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
            parts = [f"{f}: {values[f]}"
                     for f in order if f in values]
            if len(parts) != len(order):
                self._fail(
                    f"internal error: missing field in `{name}` "
                    "construction")
            return (f"{binding.dart}({', '.join(parts)})",
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
                call = f"{binding.dart}({', '.join(args)})"
                if name in self._async_lambda_names:
                    # A let-bound async lambda (one that fetches):
                    # await its Future result.
                    return (f"(await {call})", _PREC["primary"])
                return (call, _PREC["primary"])
            args = [self.emit_expr(a, 0) for a in node.args]
            kwargs = self._emit_kwargs(node, binding)
            call = f"{binding.dart}({', '.join(args + kwargs)})"
            if name in self._async_fns:
                # A user fn that fetches or serves is emitted
                # async, so its call sites await it (parenthesised:
                # `await f().field` would read as awaiting the
                # field access, not the call).
                return (f"(await {call})", _PREC["primary"])
            return (call, _PREC["primary"])
        self._fail(f"unknown function `{name}`")

    def _emit_kwargs(self, node: A.Call, binding: _Binding) -> list[str]:
        """Keyword arguments for a user-fn call: Java has no kwargs,
        so they are reordered into position (the checker already
        validated names and types)."""
        if not node.kwargs:
            return []
        fn = self.fns_by_dart.get(binding.dart)
        if fn is None:
            self._fail(
                "internal error: cannot find fn "
                f"`{binding.dart}` for keyword arguments")
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
        no arguments), `print` prints them exactly as
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
                return "print('')", _PREC["primary"]
            return (f"print({' + \" \" + '.join(parts)})",
                    _PREC["primary"])
        self._need_rt("print")
        return f"NewrRt.print([{', '.join(parts)}])", _PREC["primary"]

    def _emit_builtin_call(self, node: A.Call,
                           binding: _Binding | None) -> tuple[str, int]:
        name = node.func.name if isinstance(node.func, A.Name) else None
        args = node.args
        if name == "len":
            t = self._type_of(args[0])
            arg = self.emit_expr(args[0], 0)
            # `len` returns an int (int). A str's length is its
            # code-point count (P5), not its UTF-16 length.
            if t.name == "str":
                self._need_rt("chars")
                return f"NewrRt.chars({arg}).length", \
                    _PREC["primary"]
            if t.name in ("list", "dict", "set"):
                return f"{arg}.length", _PREC["primary"]
            return f"{arg}.length", _PREC["primary"]
        if name == "min":
            a = self.emit_expr(args[0], 0)
            b = self.emit_expr(args[1], 0)
            t = self._type_of(args[0])
            if _is_str(t):
                self._need_rt("strCmp")
                return (f"(NewrRt.strCmp({a}, {b}) < 0 ? {a} : {b})",
                        _PREC["primary"])
            return f"({a} < {b} ? {a} : {b})", _PREC["primary"]
        if name == "max":
            a = self.emit_expr(args[0], 0)
            b = self.emit_expr(args[1], 0)
            t = self._type_of(args[0])
            if _is_str(t):
                self._need_rt("strCmp")
                return (f"(NewrRt.strCmp({a}, {b}) < 0 ? {b} : {a})",
                        _PREC["primary"])
            return f"({a} < {b} ? {b} : {a})", _PREC["primary"]
        if name == "abs":
            t = self._type_of(args[0])
            arg = self.emit_expr(args[0], 0)
            if _is_int(t):
                self._need_rt("absLong")
                return f"NewrRt.absLong({arg})", _PREC["primary"]
            return f"({arg}).abs()", _PREC["primary"]
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
            # through toInt, which returns a int in every case.
            self._need_rt("toInt")
            return f"NewrRt.toInt({text})", _PREC["primary"]
        if name == "float":
            if _is_float(t):
                return text, _PREC["primary"]
            if _is_str(t):
                self._need_rt("toFloat")
                return f"NewrRt.toFloat({text})", _PREC["primary"]
            if _is_int(t):
                return f"{text}.toDouble()", _PREC["primary"]
            return f"({text} ? 1.0 : 0.0)", _PREC["primary"]  # bool
        # bool
        if _is_bool(t):
            return text, _PREC["primary"]
        if _is_str(t):
            self._need_rt("toBool")
            return f"NewrRt.toBool({text})", _PREC["primary"]
        if _is_int(t):
            return f"({text} != 0)", _PREC["primary"]
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
            parts = [f"{f}: {values[f]}"
                     for f in fields if f in values]
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
            return f"print({msg})", _PREC["primary"]
        return f"NewrRt.printErr({msg})", _PREC["primary"]

    def _file_module_class(self, binding: _Binding) -> str:
        """The Dart reference for a file-module import: the BOUND
        name (the alias when the import is aliased) - the import
        line `import '<stem>.dart' as <bound>;` is recorded here,
        so the emitted file compiles. (Java used the file STEM as
        the class name; Dart uses the import prefix.)"""
        if binding.kind == "file-module":
            stem = self.imported_modules.get(binding.dart, binding.dart)
            self.dart_imports[binding.dart] = stem
            return binding.dart
        return binding.dart

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
        sub = self.program.modules.get(binding.dart)
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
        otherwise a `int` field read as Object would not compile
        where a int is expected."""
        decl = self._find_enum_decl(enum_name)
        if decl is not None and field_type is not None:
            union = self._enum_union_fields(decl)
            utype = union.get(field_name)
            if utype is not None and utype.name == "object":
                return (f"({expr_text}.{self._mangle(field_name)} as "
                        f"{self._dart_type(field_type)})")
            if not field_type.nullable:
                declared_by_all = all(
                    field_name in [f.name for f in v.fields]
                    for v in decl.variants)
                if not declared_by_all:
                    return (f"{expr_text}.{self._mangle(field_name)}!")
        return f"{expr_text}.{self._mangle(field_name)}"

    # Discord entity fields (SPEC §7.3, ENTITY_FIELDS in
    # discord_limits) => the JDA getter for that field. All names
    # verified live against the JDA 6.7.0 jar with javap.
    # Discord entity fields (SPEC §7.3) -> nyxx expressions.
    # Values are templates with a `{obj}` placeholder for the
    # object expression. `channel.name` reads through the
    # _channelName helper (only guild text channels have a name
    # in nyxx); `member.guild` and `interaction.guild` are NOT
    # here - they need an async fetch, so _entity_field_expr
    # fails cleanly on them instead.
    _ENTITY_GETTERS: dict[str, dict[str, str]] = {
        "user": {"id": "{obj}.id",
                 "mention": "\"<@\" + {obj}.id.toString() + \">\"",
                 "name": "{obj}.username",
                 "bot": "{obj}.isBot"},
        "member": {"id": "{obj}.id",
                   "mention": "\"<@\" + {obj}.id.toString() + \">\"",
                   "name": "({obj}.user?.username ?? '')"},
        "guild": {"id": "{obj}.id",
                  "name": "{obj}.name",
                  "member_count": "({obj}.approximateMemberCount ?? 0)"},
        "channel": {"id": "{obj}.id",
                    "name": "_channelName({obj})",
                    "mention": "\"<#\" + {obj}.id.toString() + \">\""},
        "role": {"id": "{obj}.id",
                 "name": "{obj}.name"},
        "interaction": {"user": "({obj}.user!)",
                        "channel": "({obj}.channel as TextChannel)",
                        "locale": "({obj}.locale?.identifier ?? \"en\")"},
        "message": {"content": "{obj}.content",
                    "author": "({obj}.author as User)",
                    "channel": "({obj}.channel as TextChannel)"},
    }

    def _entity_field_expr(self, obj_text: str, obj_type: Type,
                           field: str, span=None) -> str | None:
        """The nyxx expression for a Discord entity field, or None
        when the type is not an entity type (the ordinary struct
        field path then applies)."""
        if obj_type.name in ("member", "interaction") \
                and field == "guild":
            # No sync nyxx equivalent: reading a member's or an
            # interaction's guild needs an async fetch, and an
            # expression cannot await. Use the `guild` binding.
            self._fail(f"`{obj_type.name}.guild` cannot be read "
                       "synchronously in Dart (nyxx needs an async "
                       "fetch for it) - use the `guild` binding "
                       "instead", span=span)
        table = self._ENTITY_GETTERS.get(obj_type.name)
        if table is None or field not in table:
            return None
        expr = table[field].format(obj=obj_text)
        if "_channelName" in expr:
            self._used_channel_name = True
        if "_channelText" in expr:
            self._used_channel_text = True
        return expr

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
            if obj.name in MODULES or obj.name in ("db", "http",
                                                   "json", "ui"):
                # A named module used without an import: the name is
                # both the real module and the reference.
                fake = _Binding(dart=obj.name, kind="module",
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
                return f"{obj_text}.message", _PREC["primary"]
            return f"{obj_text}.{self._mangle(node.name)}", \
                _PREC["primary"]
        # §16 (hole 2): a method used as a value is a lambda, not a
        # Java attribute - a int has no `is_some`, an ArrayList has
        # no `len`. A real struct field stays a plain attribute.
        if t.name == "fn" and not self._is_plain_field(obj_type,
                                                       node.name):
            sig = self._method_value_sig(obj_type, node.name)
            if sig is not None:
                return self._emit_method_value(node, obj_type, sig,
                                               fn_target)
        obj_text = self.emit_expr(obj, _PREC["primary"])
        # Discord entity fields (SPEC §7.3) => nyxx getters.
        # The checker types these fields (ENTITY_FIELDS in
        # discord_limits). A newr `id` is an int, and a nyxx
        # Snowflake prints as its digits, so `.id` is the right
        # type; `interaction.channel` and `message.channel` need
        # a cast (nyxx types them as partial channels, but the
        # newr type is the sendable text channel).
        entity = self._entity_field_expr(obj_text, obj_type, node.name, node.span)
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
            binding.dart
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
        snap_type = self._dart_type(obj_type)
        self._hoists_add([f"final {snap_type} {snap_name} = {obj_text};"])
        self._push_scope()
        arg_bindings = []
        arg_nodes = []
        for i, ptype in enumerate(params):
            b = self._bind_fresh(f"_a{i}", type=ptype, kind="param")
            arg_bindings.append(b)
            arg_nodes.append(A.Name(span=node.span, name=f"_a{i}",
                                    expr_type=ptype))
        recv_binding = _Binding(dart=snap_name, type=obj_type,
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
        arg_names = ", ".join(
            f"{self._dart_type(b.type)} {b.dart}" for b in arg_bindings)
        arrow = f"({arg_names}) => {body}"
        cast = fn_target if _is_fn(fn_target) else self._type_of(node)
        cast_text = self._dart_fn_type(cast)
        if hoists:
            # A hoisted block-body lambda: the hoisted const's
            # initializer is the lambda, and the hoists run inside
            # the arrow body at call time.
            name = self._new_lambda_name()
            lines = [f"final {cast_text} {name} = ({arg_names}) {{"]
            for h in hoists:
                lines.append("  " + h)
            lines.append(f"  return {body};")
            lines.append("};")
            self._hoists_add(lines)
            return name, _PREC["primary"]
        return arrow, _PREC["primary"]

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
                return f"{obj_text}.trim()", _PREC["primary"]
            if name == "split":
                self._need_rt("splitStr")
                return f"NewrRt.splitStr({obj_text}, {args[0]})", \
                    _PREC["primary"]
            if name == "join":
                # newr `"sep".join(parts)` - Java joins the LIST
                # with the separator.
                return f"{args[0]}.join({obj_text})", \
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
                return (f"NewrRt.listGet<{self._dart_type(t)}>"
                        f"({obj_text}, {args[0]}, {args[1]})",
                        _PREC["primary"])
            if name == "len":
                return f"{obj_text}.length", _PREC["primary"]
        elif tname == "dict":
            if name == "keys":
                return f"{obj_text}.keys.toList()", \
                    _PREC["primary"]
            if name == "get":
                if len(args) == 2:
                    expr = (f"({obj_text}.containsKey({args[0]}) ? "
                            f"{obj_text}[{args[0]}] : {args[1]})")
                    # The index returns V? - cast the ternary to
                    # the call's type so a non-nullable assignment
                    # compiles (the key exists in the taken branch,
                    # so the value is never null there when V is
                    # non-nullable).
                    t = self._type_of(node)
                    if t is not None and not t.nullable:
                        expr = f"({expr} as {self._dart_type(t)})"
                    return expr, _PREC["primary"]
                return f"{obj_text}[{args[0]}]", _PREC["primary"]
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
                # (newr_rt.dart) - a bare access would be the
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
            # result cannot be assigned to a `int` or compared
            # with `!=` (conformance list_index_error, list_ops,
            # nullable_propagate).
            elem = self._dart_type(obj_type.params[0])
            return (f"NewrRt.listIndex<{elem}>({obj_text}, {idx})",
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
            body = self._callable_body(body_node, binding.dart,
                                       inner_type, body_type)
            self._pop_scope()
            cur_text = f"NewrRt.optChain({cur_text}, {body})"
            i = j
        return cur_text, _PREC["primary"]

    def _callable_body(self, expr: A.Expr, param_dart: str,
                       param_type: Type, body_type: Type) -> str:
        """Emit `expr` as a lambda body `(<type> _v) => ...` (the
        parameter carries its type, D1 - no cast). If the body
        needs hoists (a nested block-body lambda), the lambda
        becomes a hoisted `final` const and its name is returned
        instead, so the hoisted statements run at call time,
        inside the function, not at the enclosing statement."""
        saved = self._hoists
        self._hoists = []
        body = self.emit_expr(expr, 0)
        hoists = self._hoists
        self._hoists = saved
        param_text = f"{self._dart_type(param_type)} {param_dart}"
        if not hoists:
            return f"({param_text}) => {body}"
        name = self._new_lambda_name()
        fn_type = (f"{self._dart_type(body_type)} "
                   f"Function({self._dart_type(param_type)})")
        lines = [f"final {fn_type} {name} = ({param_text}) {{"]
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
        (fn_target); otherwise it is cast to its own fn type. A
        lambda that fetches or serves is emitted `async` (its
        result type becomes a Future) - only where its Future fn
        type is known (a route-handler argument, or a let-bound
        lambda whose declared type is Future-wrapped)."""
        needs_async = _dart_walk_needs_async(
            node, self.program, self._async_fns,
            self._async_lambda_names, self._async_memo, True)
        if needs_async and self._async_lambda_depth == 0:
            self._fail("http fetch/serve inside a lambda is only "
                       "supported for a route handler argument or "
                       "a let-bound lambda - move the fetch into a "
                       "named function",
                       span=node.span)
        self._async_ctx.append(needs_async)
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
        used_names = {b.dart for b in param_bindings}
        for name, b in free:
            snap = self._new_temp_name()
            snap_type = self._dart_type(b.type)
            self._hoists_add([f"final {snap_type} {snap} = {b.value};"])
            self._scopes[-1][name] = _Binding(dart=snap, type=b.type,
                                              kind="param")
        params_text = ", ".join(
            f"{self._dart_type(b.type)} {b.dart}" for b in param_bindings)
        fn_type = self._type_of(node)
        cast = fn_target if _is_fn(fn_target) else fn_type
        cast_text = self._dart_fn_type_async(cast) if needs_async \
            else self._dart_fn_type(cast)
        async_kw = " async" if needs_async else ""
        if node.expr_body is not None:
            body, hoists = self._emit_expr_with_hoists(node.expr_body)
            self._fn_used = saved_used
            self._pop_scope()
            if not hoists:
                arrow = f"({params_text}){async_kw} => {body}"
                self._async_ctx.pop()
                return arrow, _PREC["primary"]
            # The body needed hoists (a nested block-body lambda):
            # the hoisted const's initializer IS the lambda, so the
            # snapshot locals are in scope inside the arrow body at
            # call time.
            name = self._new_lambda_name()
            lines = [f"final {cast_text} {name} = "
                     f"({params_text}){async_kw} {{"]
            for h in hoists:
                lines.append("  " + h)
            lines.append(f"  return {body};")
            lines.append("};")
            self._hoists_add(lines)
            self._async_ctx.pop()
            return name, _PREC["primary"]
        # Block body: hoist a const arrow.
        name = self._new_lambda_name()
        self._fn_ctx.append({"return_type": node.return_type,
                             "ensures": [], "in_handler": False})
        body_lines = self._emit_block(node.body or [], 1)
        self._fn_ctx.pop()
        self._fn_used = saved_used
        self._pop_scope()
        lines = [f"final {cast_text} {name} = "
                 f"({params_text}){async_kw} {{"]
        lines.extend(body_lines if body_lines else ["  ;"])
        lines.append("};")
        self._hoists_add(lines)
        self._async_ctx.pop()
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
            elif isinstance(stmt.value, A.Lambda) \
                    and stmt.name in self._async_lambda_names:
                # A let-bound lambda that fetches: emit it in
                # async-lambda context (so it may go async) and
                # declare it with its Future fn type.
                tgt = binding.type if binding is not None and \
                    _is_fn(binding.type) else None
                self._async_lambda_depth += 1
                try:
                    value = self.emit_expr(stmt.value, 0, fn_target=tgt)
                finally:
                    self._async_lambda_depth -= 1
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
                return [f"{p}{binding.dart} = {value};"]
            t = stmt.type_ann
            if t is None:
                t = self._type_of(stmt.value)
            binding = self._bind(stmt.name, type=t, kind="local")
            keyword = "final " if isinstance(stmt, A.Const) else ""
            decl_type = self._dart_type(t)
            if isinstance(stmt.value, A.Lambda) \
                    and stmt.name in self._async_lambda_names \
                    and t.name == "fn":
                decl_type = self._dart_fn_type_async(t)
            return [f"{p}{keyword}{decl_type} "
                    f"{binding.dart} = {value};"]
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
            return [f"{p}{binding.dart} = {value};"]
        t = binding.type
        if _is_int(t):
            if stmt.op == "%=":
                self._need_rt("tmod")
                return [f"{p}{binding.dart} = NewrRt.tmod("
                        f"{binding.dart}, {value});"]
            helper = {"+=": "add", "-=": "sub", "*=": "mul",
                      "/=": "tdiv"}[stmt.op]
            self._need_rt(helper)
            return [f"{p}{binding.dart} = NewrRt.{helper}({binding.dart}, "
                    f"{value});"]
        if stmt.op == "%=":
            self._need_rt("fmod")
            return [f"{p}{binding.dart} = NewrRt.fmod("
                    f"{binding.dart}, {value});"]
        # float / str / %= : bare augmented assignment (no overflow
        # check applies; `+=` on a str is concatenation; `%` on an
        # int cannot overflow - -9223372036854775808 % -1 is 0 in Java).
        return [f"{p}{binding.dart} {stmt.op} {value};"]

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
        elem_text = self._dart_type(elem)
        if it_type.name == "dict":
            # A dict iterates its keys (insertion order, like
            # Python's `for k in d`).
            header = (f"for ({elem_text} {binding.dart} in "
                      f"{iterable}.keys) {{")
        elif it_type.name == "str":
            # A string iterates code points (P5), not UTF-16 code
            # units.
            self._need_rt("chars")
            header = (f"for ({elem_text} {binding.dart} in "
                      f"NewrRt.chars({iterable})) {{")
        else:
            header = (f"for ({elem_text} {binding.dart} in "
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
            lines.append(f"{p}}} catch ({exc_var}) {{")
            self._push_scope()
            binding = self._bind(cname, type=Type("error"), kind="local")
            self._need_rt("asError")
            lines.append(f"{p}  final NewrError {binding.dart} = "
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
            return [f"{p}throw new NewrError({text});"]
        self._fail(f"`throw` needs a `str` or an `error`, not `{t}`")

    def _emit_return(self, stmt: A.Return, indent: int) -> list[str]:
        p = "  " * indent
        ctx = self._fn_ctx[-1] if self._fn_ctx else None
        if ctx is None:
            # A bare top-level `return` exits the program (D11).
            if stmt.value is None:
                return [f"{p}return;"]
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
                f"{p}  throw new NewrError("
                f"{_dart_str('ensures failed: ' + msg)});")
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
        return [f"{p}print({value});"]

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
        lines = [f"{p}final {self._dart_type(scrut_type)} {temp} = "
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
                        dart=expr_text, type=typ, kind="local")
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
                type_text = self._dart_type(typ) if typ is not None \
                    else "var"
                lines.append(f"{p}  {type_text} {b.dart} = {expr_text};")
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
                return (f"({expr_text} == "
                        f"{'true' if v else 'false'})", [])
            if isinstance(v, int):
                return f"({expr_text} == {v})", []
            if isinstance(v, float):
                return (f"({expr_text} == "
                        f"{_dart_float(v)})", [])
            return f"({expr_text} == {_dart_str(v)})", []
        if isinstance(pattern, A.EnumPattern):
            # The enum is ONE class; the variant is its discriminant
            # field `_newr_variant` (a null scrutinee matches no
            # variant - the `none` pattern handles it).
            cond = (f"({expr_text} != null && {expr_text}._newr_variant"
                    f" == {_dart_str(pattern.variant)})")
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
            struct_java = self._struct_dart_name(pattern.struct_name)
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
            cond = (f"({expr_text} is List && "
                    f"{expr_text}.length == {len(pattern.elements)})")
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
        temp_type = self._dart_type(t)
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
                return f"{temp}.unwrap()"
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
            return f"{temp}.unwrap()"
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
            # `.dart` per imported `.newr`, named by its module
            # name). Struct construction through a file module needs
            # `new` (a Java class is not callable without it).
            ref = self._file_module_class(binding)
            name = node.func.name
            sub = self.program.modules.get(binding.dart)
            if sub is not None:
                for s in sub.structs:
                    if s.name == name:
                        order = [f.name for f in s.fields]
                        values: dict[str, str] = {}
                        for a, f in zip(node.args, order):
                            values[f] = self.emit_expr(a, 0)
                        for kname, value, _span in node.kwargs:
                            values[kname] = self.emit_expr(value, 0)
                        parts = [f"{f}: {values[f]}"
                                 for f in order if f in values]
                        return (f"{ref}.{self._mangle(name)}"
                                f"({', '.join(parts)})", _PREC["primary"])
            args = [self.emit_expr(a, 0) for a in node.args]
            kwargs = self._emit_module_kwargs(node, sub)
            call = (f"{ref}.{self._mangle(name)}"
                    f"({', '.join(args + kwargs)})")
            if sub is not None:
                sub_fns, _sub_lams = _dart_async_info(
                    sub, self._async_memo)
                if name in sub_fns:
                    # A fn of an imported file module that fetches
                    # or serves is emitted async there, so await it
                    # here (parenthesised, like every awaited call).
                    return (f"(await {call})", _PREC["primary"])
            return (call, _PREC["primary"])
        real = binding.module or binding.dart
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
        if real == "db":
            args = [self.emit_expr(a, 0) for a in node.args]
            return self._emit_db_call(node, args)
        if real == "http":
            return self._emit_http_call(node)
        if real == "json":
            return self._emit_json_call(node)
        if real == "ui":
            return self._emit_ui_call(node)
        args = [self.emit_expr(a, 0) for a in node.args]
        return self._emit_module_call_text(real, binding.dart,
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
                self._need_math()
                return f"math.sqrt({args[0]})", _PREC["primary"]
            if name == "floor":
                # dart:math has no top-level floor - it is the
                # double method, returning int.
                return f"({args[0]}).floor()", _PREC["primary"]
            if name == "ceil":
                return f"({args[0]}).ceil()", _PREC["primary"]
        if real == "time":
            if name == "now":
                # Milliseconds since epoch, truncated to seconds;
                # always in int64 range.
                return ("DateTime.now().millisecondsSinceEpoch ~/ 1000",
                        _PREC["primary"])
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
                    return (f"NewrRt.envGet({args[0]}, {args[1]})!",
                            _PREC["primary"])
                return f"NewrRt.envGet({args[0]})", _PREC["primary"]
        self._fail(f"module `{real}` has no function `{name}`")

    def _emit_db_call(self, node: A.Call,
                      args: list[str]) -> tuple[str, int]:
        """`db.*` calls (SPEC.md section 12.5): the helpers live in
        the `NewrDb` class emitted into this file (see
        `_emit_db_class`) — inline, not in newr_rt.dart, because
        the runtime file is dependency-free and a program that
        never uses `db` must not need package:sqlite3 or a
        pubspec. Handles are ints, fd-style. The checker
        (db_check) has already validated the call, so the arity
        here is trusted. `db.query` maps each converted row to
        the row struct through `queryAs<T>`."""
        name = node.func.name
        self._need_sqlite()
        self._need_rt("toStr")
        self._file_uses_db = True
        if name == "open":
            return f"NewrDb.open({args[0]})", _PREC["primary"]
        if name == "exec":
            rest = ", ".join(args[2:])
            return (f"NewrDb.exec({args[0]}, {args[1]}, [{rest}])",
                    _PREC["primary"])
        if name == "query":
            dart_struct, decl = self._db_row_struct(node)
            names = [_dart_str(f.name) for f in decl.fields]
            types = [_dart_str(self._db_field_type(f.type))
                     for f in decl.fields]
            fields = ", ".join(
                f"{self._mangle(f.name)}: "
                f"row[{i}] as {self._dart_type(f.type)}"
                for i, f in enumerate(decl.fields))
            names_lit = "[" + ", ".join(names) + "]"
            types_lit = "[" + ", ".join(types) + "]"
            rest = ", ".join(args[2:])
            return (f"NewrDb.queryAs<{dart_struct}>({args[0]}, "
                    f"{args[1]}, [{rest}], {names_lit}, {types_lit}, "
                    f"(row) => {dart_struct}({fields}))",
                    _PREC["primary"])
        if name == "begin":
            return f"NewrDb.begin({args[0]})", _PREC["primary"]
        if name == "commit":
            return f"NewrDb.commit({args[0]})", _PREC["primary"]
        if name == "rollback":
            return f"NewrDb.rollback({args[0]})", _PREC["primary"]
        if name == "last_id":
            return f"NewrDb.lastId({args[0]})", _PREC["primary"]
        if name == "close":
            return f"NewrDb.close({args[0]})", _PREC["primary"]
        self._fail(f"module `db` has no function `{name}`")

    def _db_row_struct(self, node: A.Call) -> tuple[str, A.StructDecl]:
        """The (Dart constructor, decl) of the row struct for a
        `db.query` call. The row type comes from `node.expr_type`,
        set by db_check to `list[Row]`. A struct from an imported
        file module resolves to its qualified class name."""
        t = self._type_of(node)
        if t.name != "list" or not t.params:
            self._fail("internal: db.query without a list[Row] type")
        row_name = t.params[0].name
        decl = self.structs.get(row_name)
        if decl is not None:
            return self._struct_dart_name(row_name), decl
        for bound, sub in self.program.modules.items():
            for s in sub.structs:
                if s.name == row_name:
                    return self._struct_dart_name(row_name), s
        self._fail(f"db.query: row struct `{row_name}` not found")

    @staticmethod
    def _db_field_type(t: Type) -> str:
        """The runtime type string for a row field: the base name,
        with `?` when nullable. Row fields are only int/float/str/
        bool (db_check enforces it), so no deeper shape is needed."""
        return t.name + ("?" if t.nullable else "")

    def _emit_db_class(self) -> list[str]:
        """The `NewrDb` helper class, emitted into a file that uses
        `db.*` (at top level, before `void main`). The int handle
        registry plus open/exec/query/begin/commit/rollback/
        last_id/close over package:sqlite3 (the store's library,
        3.9.0 — the same sync API the store uses). Conversion
        mirrors the Python `_db_value` (newr_rt.py): NULL lands
        only in a nullable field, `?` binds are positional (the
        Nth argument binds the Nth `?` — the sqlite3 package
        binds a bool as 0/1 itself), a value that cannot convert
        throws instead of silently producing a wrong value.
        `query` looks rows up by column NAME (a missing column
        throws, extra columns are ignored)."""
        return [
            "class NewrDb {",
            "  static final Map<int, Database> _conns = {};",
            "  static int _nextId = 1;",
            "",
            "  static Database _conn(int handle) {",
            "    final conn = _conns[handle];",
            "    if (conn == null) {",
            "      throw NewrError('db: unknown or closed handle $handle');",
            "    }",
            "    return conn;",
            "  }",
            "",
            "  static Object? _dbValue(Object? value, String typeStr) {",
            "    final nullable = typeStr.endsWith('?');",
            "    final base = nullable",
            "        ? typeStr.substring(0, typeStr.length - 1)",
            "        : typeStr;",
            "    if (value == null) {",
            "      if (nullable) {",
            "        return null;",
            "      }",
            "      throw NewrError('db: NULL in non-nullable $base field');",
            "    }",
            "    switch (base) {",
            "      case 'int':",
            "        if (value is bool) {",
            "          throw NewrError('db: cannot convert bool to int');",
            "        }",
            "        if (value is int) {",
            "          return value;",
            "        }",
            "        if (value is double) {",
            "          if (value != value.floorToDouble()",
            "              || value < -9223372036854775808.0",
            "              || value > 9223372036854775807.0) {",
            "            throw NewrError('db: cannot convert $value to int');",
            "          }",
            "          return value.toInt();",
            "        }",
            "        if (value is String) {",
            "          final parsed = int.tryParse(value.trim());",
            "          if (parsed != null) {",
            "            return parsed;",
            "          }",
            "          throw NewrError(\"db: cannot convert '$value' to int\");",
            "        }",
            "        throw NewrError('db: cannot convert '",
            "            '${value.runtimeType} to int');",
            "      case 'float':",
            "        if (value is bool) {",
            "          throw NewrError('db: cannot convert bool to float');",
            "        }",
            "        if (value is int) {",
            "          return value.toDouble();",
            "        }",
            "        if (value is double) {",
            "          return value;",
            "        }",
            "        if (value is String) {",
            "          final parsed = double.tryParse(value.trim());",
            "          if (parsed != null) {",
            "            return parsed;",
            "          }",
            "          throw NewrError(",
            "              \"db: cannot convert '$value' to float\");",
            "        }",
            "        throw NewrError('db: cannot convert '",
            "            '${value.runtimeType} to float');",
            "      case 'str':",
            "        if (value is bool) {",
            "          return value ? 'true' : 'false';",
            "        }",
            "        if (value is String) {",
            "          return value;",
            "        }",
            "        if (value is int) {",
            "          return value.toString();",
            "        }",
            "        if (value is double) {",
            "          return NewrRt.toStr(value);",
            "        }",
            "        throw NewrError('db: cannot convert '",
            "            '${value.runtimeType} to str');",
            "      case 'bool':",
            "        if (value is bool) {",
            "          return value;",
            "        }",
            "        if (value is int) {",
            "          if (value == 0) {",
            "            return false;",
            "          }",
            "          if (value == 1) {",
            "            return true;",
            "          }",
            "          throw NewrError('db: cannot convert $value to bool');",
            "        }",
            "        if (value is double) {",
            "          final w = value.toInt();",
            "          if (w == 0) {",
            "            return false;",
            "          }",
            "          if (w == 1) {",
            "            return true;",
            "          }",
            "          throw NewrError('db: cannot convert $value to bool');",
            "        }",
            "        if (value is String) {",
            "          if (value == '0') {",
            "            return false;",
            "          }",
            "          if (value == '1') {",
            "            return true;",
            "          }",
            "          throw NewrError(",
            "              \"db: cannot convert '$value' to bool\");",
            "        }",
            "        throw NewrError('db: cannot convert '",
            "            '${value.runtimeType} to bool');",
            "      default:",
            "        throw NewrError('db: unknown field type $typeStr');",
            "    }",
            "  }",
            "",
            "  static int open(String path) {",
            "    final db = sqlite3.open(path);",
            "    db.execute('PRAGMA foreign_keys = ON');",
            "    db.execute('PRAGMA busy_timeout = 5000');",
            "    final handle = _nextId;",
            "    _nextId += 1;",
            "    _conns[handle] = db;",
            "    return handle;",
            "  }",
            "",
            "  static int exec(int handle, String sql, List<Object?> args) {",
            "    final db = _conn(handle);",
            "    db.execute(sql, args);",
            "    return db.getUpdatedRows();",
            "  }",
            "",
            "  static List<List<Object?>> query(int handle, String sql,",
            "      List<Object?> args, List<String> names, List<String> types) {",
            "    final db = _conn(handle);",
            "    final result = db.select(sql, args);",
            "    final rows = <List<Object?>>[];",
            "    for (final row in result) {",
            "      final outRow = <Object?>[];",
            "      for (var i = 0; i < names.length; i++) {",
            "        if (!result.columnNames.contains(names[i])) {",
            "          throw NewrError(",
            "              \"db: column '${names[i]}' missing from query result\");",
            "        }",
            "        outRow.add(_dbValue(row[names[i]], types[i]));",
            "      }",
            "      rows.add(outRow);",
            "    }",
            "    return rows;",
            "  }",
            "",
            "  static List<T> queryAs<T>(int handle, String sql,",
            "      List<Object?> args, List<String> names, List<String> types,",
            "      T Function(List<Object?>) map) {",
            "    return query(handle, sql, args, names, types).map(map).toList();",
            "  }",
            "",
            "  static void begin(int handle) {",
            "    _conn(handle).execute('BEGIN IMMEDIATE');",
            "  }",
            "",
            "  static void commit(int handle) {",
            "    _conn(handle).execute('COMMIT');",
            "  }",
            "",
            "  static void rollback(int handle) {",
            "    _conn(handle).execute('ROLLBACK');",
            "  }",
            "",
            "  static int lastId(int handle) {",
            "    return _conn(handle).lastInsertRowId;",
            "  }",
            "",
            "  static void close(int handle) {",
            "    final db = _conns.remove(handle);",
            "    if (db == null) {",
            "      throw NewrError('db: unknown or closed handle $handle');",
            "    }",
            "    db.dispose();",
            "  }",
            "}",
        ]

    # ------------------------------------------------------------ http/json
    # shapes

    def _await_call(self, call: str) -> str:
        """Parenthesised `await` for an http fetch/serve call. The
        parens matter: a bare `await f().field` reads as awaiting
        the field access, not the call. Fails loudly when the call
        is emitted outside an async context (a sync fn the scan
        missed, or a lambda that is not a route handler or a
        let-bound lambda)."""
        if not self._async_ctx or not self._async_ctx[-1]:
            self._fail("http fetch/serve needs an async context "
                       "(top-level statements, or a function that "
                       "calls it); a lambda that fetches must be a "
                       "route handler argument or a let-bound lambda")
        return f"(await {call})"

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
        """Dart source for the shape descriptor of a static type
        (SPEC.md sections 12.6, 12.7), mirroring the Python
        emitter's `_shape_literal`: a scalar is `"int"` /
        `"float"` / `"str"` / `"bool"` / `"none"`, a nullable is
        `["?", inner]`, a list is `["list", elem, converter]`, a
        dict is `["dict", value, converter]`, a struct is
        `["struct", factory, [[field, shape, getter], ...]]` in
        declaration order. The list/dict converters re-type the
        decoded values (a decoded `List<Object?>` must become a
        `List<Todo>` at runtime, or a later `as List<Todo>` cast
        fails: Dart generics are reified). The struct factory
        builds the struct positionally from converted values;
        each getter reads one field, for json.encode."""
        if t.nullable:
            inner = Type(t.name, t.params)
            return f'["?", {self._shape_literal(inner)}]'
        if t.name in ("int", "float", "str", "bool", "none"):
            return _dart_str(t.name)
        if t.name == "list":
            if len(t.params) != 1:
                self._fail(f"internal: list type without one element "
                           f"type: `{t}`")
            elem = t.params[0]
            conv = (f"(List<Object?> v) => v.map((e) => "
                    f"{self._shape_value_expr(elem, 'e')}).toList()")
            return f'["list", {self._shape_literal(elem)}, {conv}]'
        if t.name == "dict":
            if len(t.params) != 2:
                self._fail(f"internal: dict type without two params: "
                           f"`{t}`")
            val = t.params[1]
            conv = (f"(Map<String, Object?> v) => v.map((k, v2) => "
                    f"MapEntry(k, "
                    f"{self._shape_value_expr(val, 'v2')}))")
            return f'["dict", {self._shape_literal(val)}, {conv}]'
        if t.name in ("fn", "Result", "set", "error", "duration"):
            self._fail(f"internal: type `{t}` has no JSON shape")
        decl = self._struct_decl(t.name)
        dart_name = self._struct_dart_name(t.name)
        factory = (f"(List<Object?> v) => {dart_name}(" + ", ".join(
            f"{self._mangle(f.name)}: "
            f"{self._shape_value_expr(f.type, f'v[{i}]')}"
            for i, f in enumerate(decl.fields)) + ")")
        fields = ", ".join(
            f"[{_dart_str(f.name)}, {self._shape_literal(f.type)}, "
            f"(Object? v) => (v as {dart_name}).{self._mangle(f.name)}]"
            for f in decl.fields)
        return f'["struct", {factory}, [{fields}]]'

    def _shape_value_expr(self, t: Type, var: str) -> str:
        """The Dart expression converting the decoded value in
        `var` (statically Object?) to the newr type `t`: a cast
        for scalars and structs, a re-typing `.map(...).toList()`
        for lists (and `.map` for dicts), recursing so nested
        collections come out with their exact runtime types."""
        if t.nullable:
            inner = Type(t.name, t.params)
            return (f"({var} == null ? null : "
                    f"{self._shape_value_expr(inner, var)})")
        if t.name in ("int", "float", "str", "bool"):
            return f"{var} as {self._dart_type(t)}"
        if t.name == "none":
            return "null"
        if t.name == "list":
            elem = t.params[0]
            return (f"({var} as List).map((e) => "
                    f"{self._shape_value_expr(elem, 'e')}).toList()")
        if t.name == "dict":
            val = t.params[1]
            return (f"({var} as Map).map((k, v2) => MapEntry("
                    f"k as String, "
                    f"{self._shape_value_expr(val, 'v2')}))")
        return f"{var} as {self._struct_dart_name(t.name)}"

    def _http_handler_types(self, handler_arg: A.Expr
                            ) -> tuple[Type, Type]:
        """The (request type, response type) of a route handler:
        the parameter and return types of an inline lambda, or
        the first and last params of the fn type of a let-bound
        lambda variable (the checker tracks let-bound lambdas the
        same way, in http_check._lambdas). A named function is not
        a handler in M8b."""
        if isinstance(handler_arg, A.Lambda):
            if len(handler_arg.params) != 1:
                self._fail("a route handler takes exactly one "
                           "parameter (the request)",
                           span=handler_arg.span)
            if handler_arg.return_type is None:
                self._fail("a route handler must declare a response "
                           "struct return type",
                           span=handler_arg.span)
            return handler_arg.params[0].type, handler_arg.return_type
        if isinstance(handler_arg, A.Name):
            binding = self._lookup(handler_arg.name)
            if binding is not None and binding.type is not None:
                t = binding.type
                if t.name == "fn" and t.params:
                    return t.params[0], t.params[-1]
            self._fail(f"route handler `{handler_arg.name}` is not a "
                       "lambda", span=handler_arg.span)
        self._fail("a route handler must be a lambda",
                   span=handler_arg.span)

    def _emit_http_call(self, node: A.Call) -> tuple[str, int]:
        """`http.*` calls (SPEC.md section 12.6): the helpers live
        in the `NewrHttp` class emitted into this file (see
        `_emit_http_class`) — inline, not in newr_rt.dart, because
        the runtime file is dependency-free. The checker
        (http_check) has already validated the call, so the arity
        here is trusted. Fetch calls carry the response struct
        shape from `node.expr_type`, set by http_check; route
        calls carry the request/response struct shapes from the
        handler lambda's parameter and return types. Fetch and
        serve are awaited (dart:io is async only); route is sync."""
        name = node.func.name
        if name == "route":
            method = self.emit_expr(node.args[0], 0)
            path = self.emit_expr(node.args[1], 0)
            req_type, resp_type = self._http_handler_types(node.args[2])
            req_shape = self._shape_literal(req_type)
            resp_shape = self._shape_literal(resp_type)
            self._need_http()
            self._async_lambda_depth += 1
            try:
                handler = self.emit_expr(node.args[2], 0)
            finally:
                self._async_lambda_depth -= 1
            return (f"NewrHttp.route({method}, {path}, {handler}, "
                    f"{req_shape}, {resp_shape})", _PREC["primary"])
        if name == "serve":
            args = [self.emit_expr(a, 0) for a in node.args]
            self._need_http()
            call = f"NewrHttp.serve({', '.join(args)})"
            return (self._await_call(call), _PREC["primary"])
        if name in ("get", "post", "request"):
            t = node.expr_type
            if t is None:
                self._fail(f"internal: http.{name} without a "
                           "response type")
            shape = self._shape_literal(t)
            args = [self.emit_expr(a, 0) for a in node.args]
            self._need_http()
            dart_t = self._dart_type(t)
            if name == "get":
                timeout = args[1] if len(args) > 1 else "10"
                headers = args[2] if len(args) > 2 else "null"
                call = (f"NewrHttp.get<{dart_t}>({args[0]}, {timeout}, "
                        f"{headers}, {shape})")
            elif name == "post":
                timeout = args[2] if len(args) > 2 else "10"
                headers = args[3] if len(args) > 3 else "null"
                call = (f"NewrHttp.post<{dart_t}>({args[0]}, {args[1]}, "
                        f"{timeout}, {headers}, {shape})")
            else:
                timeout = args[3] if len(args) > 3 else "10"
                headers = args[4] if len(args) > 4 else "null"
                call = (f"NewrHttp.request<{dart_t}>({args[0]}, "
                        f"{args[1]}, {args[2]}, {timeout}, {headers}, "
                        f"{shape})")
            return (self._await_call(call), _PREC["primary"])
        self._fail(f"module `http` has no function `{name}`")

    def _emit_json_call(self, node: A.Call) -> tuple[str, int]:
        """`json.*` calls (SPEC.md section 12.7): the helpers live
        in the `NewrJson` class emitted into this file (see
        `_emit_json_class`) — inline, not in newr_rt.dart.
        `json.encode` takes its shape from the argument's static
        type; `json.decode` takes its shape from `node.expr_type`,
        the expected type set by json_check. Both are sync
        (dart:convert is sync), so no await is involved."""
        name = node.func.name
        if name == "encode":
            shape = self._shape_literal(self._type_of(node.args[0]))
            arg = self.emit_expr(node.args[0], 0)
            self._need_json()
            return f"NewrJson.encode({arg}, {shape})", _PREC["primary"]
        if name == "decode":
            t = node.expr_type
            if t is None:
                self._fail("internal: json.decode without an "
                           "expected type")
            shape = self._shape_literal(t)
            arg = self.emit_expr(node.args[0], 0)
            self._need_json()
            dart_t = self._dart_type(t)
            return (f"NewrJson.decode<{dart_t}>({arg}, {shape})",
                    _PREC["primary"])
        self._fail(f"module `json` has no function `{name}`")

    def _emit_ui_call(self, node: A.Call) -> tuple[str, int]:
        """`ui.*` calls (SPEC.md section 12.8): the helpers live
        in newr_rt.dart as `NewrRt.ui*` static methods, and the
        `Widget` class there is the opaque widget value. The
        checker (ui_check) has already validated the call, so the
        arity here is trusted. Constructors build a Widget;
        `render` is the pure HTML core; `show` and `run` write
        the HTML sink on dart (no native window in M8c)."""
        name = node.func.name
        methods = {
            "text": "uiText",
            "button": "uiButton",
            "input": "uiInput",
            "list": "uiList",
            "image": "uiImage",
            "row": "uiRow",
            "column": "uiColumn",
            "render": "uiRender",
            "show": "uiShow",
            "run": "uiRun",
        }
        if name in methods:
            args = [self.emit_expr(a, 0) for a in node.args]
            self._need_rt(methods[name])
            return (f"NewrRt.{methods[name]}({', '.join(args)})",
                    _PREC["primary"])
        self._fail(f"module `ui` has no function `{name}`")

    def _emit_json_class(self) -> list[str]:
        """The `NewrJson` helper class, emitted into a file that
        uses `json.*` (at top level, before `void main`). Typed
        JSON over dart:convert: encode is compact with UTF-8
        output (no spaces, no \\u escapes), floats use
        NewrRt.floatStr (shortest round-trip, ".0" for integral
        floats), decode walks the shape (struct fields match by
        name, a missing field throws, an extra field is ignored,
        null only into a nullable expected type, a JSON float
        into an int field always throws, an integer into a float
        field is fine, trailing data throws, an empty string
        throws, duplicate keys last wins, an integer outside
        int64 throws: dart:convert parses it as a double, so the
        int branch rejects doubles outright)."""
        return [
            "class NewrJson {",
            "  static String encode(Object? value, Object? shape) {",
            "    final sb = StringBuffer();",
            "    _writeValue(sb, value, shape);",
            "    return sb.toString();",
            "  }",
            "",
            "  static String _jsonString(String value) {",
            "    return jsonEncode(value);",
            "  }",
            "",
            "  static void _writeValue(StringBuffer sb, Object? value,",
            "      Object? shape) {",
            "    if (shape is String) {",
            "      switch (shape) {",
            "        case 'int':",
            "          if (value is! int) {",
            "            throw NewrError(",
            "                'json: expected int, got '",
            "                '${value.runtimeType}');",
            "          }",
            "          sb.write(value.toString());",
            "          return;",
            "        case 'float':",
            "          if (value is bool) {",
            "            throw NewrError('json: expected float, got bool');",
            "          }",
            "          if (value is int) {",
            "            sb.write(NewrRt.floatStr(value.toDouble()));",
            "            return;",
            "          }",
            "          if (value is double) {",
            "            if (!value.isFinite) {",
            "              throw NewrError(",
            "                  'json: cannot encode non-finite float');",
            "            }",
            "            sb.write(NewrRt.floatStr(value));",
            "            return;",
            "          }",
            "          throw NewrError(",
            "              'json: expected float, got '",
            "              '${value.runtimeType}');",
            "        case 'str':",
            "          if (value is! String) {",
            "            throw NewrError(",
            "                'json: expected str, got '",
            "                '${value.runtimeType}');",
            "          }",
            "          sb.write(_jsonString(value));",
            "          return;",
            "        case 'bool':",
            "          if (value is! bool) {",
            "            throw NewrError(",
            "                'json: expected bool, got '",
            "                '${value.runtimeType}');",
            "          }",
            "          sb.write(value ? 'true' : 'false');",
            "          return;",
            "        case 'none':",
            "          if (value != null) {",
            "            throw NewrError('json: expected null');",
            "          }",
            "          sb.write('null');",
            "          return;",
            "        default:",
            "          throw NewrError(\"json: unknown shape '$shape'\");",
            "      }",
            "    }",
            "    final list = shape as List<Object?>;",
            "    final kind = list[0] as String;",
            "    if (kind == '?') {",
            "      if (value == null) {",
            "        sb.write('null');",
            "        return;",
            "      }",
            "      _writeValue(sb, value, list[1]);",
            "      return;",
            "    }",
            "    if (kind == 'list') {",
            "      if (value is! List) {",
            "        throw NewrError(",
            "            'json: expected list, got '",
            "            '${value.runtimeType}');",
            "      }",
            "      sb.write('[');",
            "      for (var i = 0; i < value.length; i++) {",
            "        if (i > 0) sb.write(',');",
            "        _writeValue(sb, value[i], list[1]);",
            "      }",
            "      sb.write(']');",
            "      return;",
            "    }",
            "    if (kind == 'dict') {",
            "      if (value is! Map) {",
            "        throw NewrError(",
            "            'json: expected dict, got '",
            "            '${value.runtimeType}');",
            "      }",
            "      sb.write('{');",
            "      var first = true;",
            "      for (final e in value.entries) {",
            "        if (e.key is! String) {",
            "          throw NewrError('json: dict keys must be str');",
            "        }",
            "        if (!first) sb.write(',');",
            "        first = false;",
            "        sb.write(_jsonString(e.key as String));",
            "        sb.write(':');",
            "        _writeValue(sb, e.value, list[1]);",
            "      }",
            "      sb.write('}');",
            "      return;",
            "    }",
            "    if (kind == 'struct') {",
            "      if (value == null) {",
            "        throw NewrError('json: expected object, got null');",
            "      }",
            "      final fields = list[2] as List<Object?>;",
            "      sb.write('{');",
            "      for (var i = 0; i < fields.length; i++) {",
            "        final entry = fields[i] as List<Object?>;",
            "        if (i > 0) sb.write(',');",
            "        sb.write(_jsonString(entry[0] as String));",
            "        sb.write(':');",
            "        final getter = entry[2] as Object? Function(Object?);",
            "        _writeValue(sb, getter(value), entry[1]);",
            "      }",
            "      sb.write('}');",
            "      return;",
            "    }",
            "    throw NewrError(\"json: unknown shape '$shape'\");",
            "  }",
            "",
            "  static T decode<T>(String text, Object? shape) {",
            "    Object? parsed;",
            "    try {",
            "      parsed = jsonDecode(text);",
            "    } on FormatException catch (e) {",
            "      throw NewrError('json: invalid JSON: $e');",
            "    }",
            "    return _decodeValue(parsed, shape) as T;",
            "  }",
            "",
            "  static Object? _decodeValue(Object? value, Object? shape) {",
            "    if (shape is String) {",
            "      switch (shape) {",
            "        case 'int':",
            "          if (value is! int) {",
            "            throw NewrError(",
            "                'json: expected int, got '",
            "                '${value.runtimeType}');",
            "          }",
            "          return value;",
            "        case 'float':",
            "          if (value is bool) {",
            "            throw NewrError('json: expected float, got bool');",
            "          }",
            "          if (value is int) {",
            "            return value.toDouble();",
            "          }",
            "          if (value is double) {",
            "            return value;",
            "          }",
            "          throw NewrError(",
            "              'json: expected float, got '",
            "              '${value.runtimeType}');",
            "        case 'str':",
            "          if (value is! String) {",
            "            throw NewrError(",
            "                'json: expected str, got '",
            "                '${value.runtimeType}');",
            "          }",
            "          return value;",
            "        case 'bool':",
            "          if (value is! bool) {",
            "            throw NewrError(",
            "                'json: expected bool, got '",
            "                '${value.runtimeType}');",
            "          }",
            "          return value;",
            "        case 'none':",
            "          if (value != null) {",
            "            throw NewrError('json: expected null');",
            "          }",
            "          return null;",
            "        default:",
            "          throw NewrError(\"json: unknown shape '$shape'\");",
            "      }",
            "    }",
            "    final list = shape as List<Object?>;",
            "    final kind = list[0] as String;",
            "    if (kind == '?') {",
            "      if (value == null) {",
            "        return null;",
            "      }",
            "      return _decodeValue(value, list[1]);",
            "    }",
            "    if (kind == 'list') {",
            "      if (value is! List) {",
            "        throw NewrError(",
            "            'json: expected list, got '",
            "            '${value.runtimeType}');",
            "      }",
            "      final raw = <Object?>[];",
            "      for (final e in value) {",
            "        raw.add(_decodeValue(e, list[1]));",
            "      }",
            "      final convert =",
            "          list[2] as List<Object?> Function(List<Object?>);",
            "      return convert(raw);",
            "    }",
            "    if (kind == 'dict') {",
            "      if (value is! Map) {",
            "        throw NewrError(",
            "            'json: expected dict, got '",
            "            '${value.runtimeType}');",
            "      }",
            "      final raw = <String, Object?>{};",
            "      for (final e in value.entries) {",
            "        raw[e.key as String] = _decodeValue(e.value, list[1]);",
            "      }",
            "      final convert = list[2] as Map<String, Object?>",
            "          Function(Map<String, Object?>);",
            "      return convert(raw);",
            "    }",
            "    if (kind == 'struct') {",
            "      if (value is! Map) {",
            "        throw NewrError(",
            "            'json: expected object, got '",
            "            '${value.runtimeType}');",
            "      }",
            "      final fields = list[2] as List<Object?>;",
            "      final args = <Object?>[];",
            "      for (final f in fields) {",
            "        final entry = f as List<Object?>;",
            "        final name = entry[0] as String;",
            "        if (!value.containsKey(name)) {",
            "          throw NewrError(\"json: missing field '$name'\");",
            "        }",
            "        args.add(_decodeValue(value[name], entry[1]));",
            "      }",
            "      final factory =",
            "          list[1] as Object? Function(List<Object?>);",
            "      return factory(args);",
            "    }",
            "    throw NewrError(\"json: unknown shape '$shape'\");",
            "  }",
            "",
            "  static Object? buildFromShape(List<Object?> shape,",
            "      Map<String, Object?> values) {",
            "    if (shape.length != 3 || shape[0] != 'struct') {",
            "      throw NewrError('http: internal: expected a struct shape');",
            "    }",
            "    final fields = shape[2] as List<Object?>;",
            "    final args = <Object?>[];",
            "    for (final f in fields) {",
            "      final entry = f as List<Object?>;",
            "      final name = entry[0] as String;",
            "      if (!values.containsKey(name)) {",
            "        throw NewrError(\"http: missing field '$name'\");",
            "      }",
            "      args.add(_decodeValue(values[name], entry[1]));",
            "    }",
            "    final factory =",
            "        shape[1] as Object? Function(List<Object?>);",
            "    return factory(args);",
            "  }",
            "",
            "  static Object? readField(Object? value, List<Object?> shape,",
            "      String name) {",
            "    final fields = shape[2] as List<Object?>;",
            "    for (final f in fields) {",
            "      final entry = f as List<Object?>;",
            "      if (entry[0] == name) {",
            "        final getter = entry[2] as Object? Function(Object?);",
            "        return getter(value);",
            "      }",
            "    }",
            "    throw NewrError(\"http: internal: no field '$name' \"",
            "        'in response shape');",
            "  }",
            "}",
        ]

    def _emit_http_class(self) -> list[str]:
        """The `NewrHttp` helper class, emitted into a file that
        uses `http.*` (at top level, before `void main`). Sync
        newr calls over async dart:io: the server is an
        `HttpServer` (bind, print the listening line, then
        `await for` each request), the client is an `HttpClient`
        with a whole-request timeout. Routes are exact-match only
        (no route or a method mismatch is 404 with an empty
        body); query strings parse into dict[str, str] (a repeated
        key keeps the last value, "+" decodes as a space, %XX
        decodes as UTF-8); the whole body is read before the
        handler runs; status is validated 100 to 599 and
        Content-Length is always set; handler exceptions, an
        invalid status, and non-UTF-8 bodies surface as 500 with
        an empty body and the server stays alive. Fetch returns
        the response struct for 4xx/5xx (not a throw); an
        unreachable host, a timeout, a bad URL, or a host outside
        the default localhost allowlist throws. POST/request set
        Content-Type: application/json unless the program sets
        one; GET sets none. The runtime enforces the default
        localhost allowlist only; the manifest allowlist is a
        static checker concern."""
        return [
            "class NewrHttp {",
            "  static const List<String> _methods = [",
            "    'GET', 'POST', 'PUT', 'PATCH', 'DELETE', 'HEAD', 'OPTIONS',",
            "  ];",
            "  static const List<String> _allowlist = [",
            "    'localhost', '127.0.0.1',",
            "  ];",
            "  static final List<List<Object?>> _routes = [];",
            "",
            "  static void route(String method, String path, dynamic handler,",
            "      List<Object?> reqShape, List<Object?> respShape) {",
            "    if (!_methods.contains(method)) {",
            "      throw NewrError(\"http: invalid method '$method'\");",
            "    }",
            "    if (!path.startsWith('/')) {",
            "      throw NewrError(",
            "          \"http: route path must start with '/', got '$path'\");",
            "    }",
            "    if (handler is! Function) {",
            "      throw NewrError('http: route handler must be callable');",
            "    }",
            "    _routes.add([method, path, handler, reqShape, respShape]);",
            "  }",
            "",
            "  static Future<void> serve(int port,",
            "      [String host = '127.0.0.1']) async {",
            "    if (port < 0 || port > 65535) {",
            "      throw NewrError('http: port must be 0 to 65535, got $port');",
            "    }",
            "    final server = await io.HttpServer.bind(host, port);",
            "    print('listening on $host:${server.port}');",
            "    await io.stdout.flush();",
            "    await for (final io.HttpRequest req in server) {",
            "      await _handle(req);",
            "    }",
            "  }",
            "",
            "  static Future<void> _send(io.HttpResponse resp, int status,",
            "      Map<String, String> headers, List<int> body) async {",
            "    resp.statusCode = status;",
            "    for (final e in headers.entries) {",
            "      if (e.key.toLowerCase() == 'content-length') continue;",
            "      resp.headers.set(e.key, e.value);",
            "    }",
            "    resp.contentLength = body.length;",
            "    if (body.isNotEmpty) {",
            "      resp.add(body);",
            "    }",
            "    await resp.close();",
            "  }",
            "",
            "  static Future<void> _handle(io.HttpRequest req) async {",
            "    try {",
            "      final path = req.uri.path;",
            "      final query = <String, String>{};",
            "      req.uri.queryParameters.forEach((k, v) => query[k] = v);",
            "      final chunks = await req.toList();",
            "      final bytes = <int>[];",
            "      for (final c in chunks) {",
            "        bytes.addAll(c);",
            "      }",
            "      String body;",
            "      try {",
            "        body = utf8.decode(bytes);",
            "      } on FormatException {",
            "        await _send(req.response, 500, {}, []);",
            "        return;",
            "      }",
            "      final headers = <String, String>{};",
            "      req.headers.forEach((name, values) {",
            "        headers[name] = values.last;",
            "      });",
            "      List<Object?>? found;",
            "      for (final r in _routes) {",
            "        if (r[0] == req.method && r[1] == path) {",
            "          found = r;",
            "          break;",
            "        }",
            "      }",
            "      if (found == null) {",
            "        await _send(req.response, 404, {}, []);",
            "        return;",
            "      }",
            "      final values = <String, Object?>{",
            "        'method': req.method,",
            "        'path': path,",
            "        'headers': headers,",
            "        'body': body,",
            "        'query': query,",
            "      };",
            "      final reqObj =",
            "          NewrJson.buildFromShape(found[3] as List<Object?>, values);",
            "      var result = (found[2] as dynamic)(reqObj);",
            "      if (result is Future) {",
            "        result = await result;",
            "      }",
            "      final respShape = found[4] as List<Object?>;",
            "      final status = NewrJson.readField(result, respShape, 'status');",
            "      final respHeaders =",
            "          NewrJson.readField(result, respShape, 'headers');",
            "      final respBody = NewrJson.readField(result, respShape, 'body');",
            "      if (status is! int || status < 100 || status > 599) {",
            "        throw NewrError(",
            "            'http: response status must be 100 to 599, got $status');",
            "      }",
            "      if (respHeaders is! Map<String, String>) {",
            "        throw NewrError('http: response headers must be a dict');",
            "      }",
            "      if (respBody is! String) {",
            "        throw NewrError('http: response body must be a str');",
            "      }",
            "      final bodyBytes = utf8.encode(respBody);",
            "      await _send(req.response, status, respHeaders,",
            "          req.method == 'HEAD' ? <int>[] : bodyBytes);",
            "    } catch (_) {",
            "      try {",
            "        await _send(req.response, 500, {}, []);",
            "      } catch (_) {}",
            "    }",
            "  }",
            "",
            "  static Future<T> fetch<T>(String method, String url,",
            "      Object? body, int timeout, Map<String, String>? headers,",
            "      List<Object?> shape) async {",
            "    if (!_methods.contains(method)) {",
            "      throw NewrError(\"http: invalid method '$method'\");",
            "    }",
            "    if (timeout <= 0) {",
            "      throw NewrError(",
            "          'http: timeout must be a positive number of seconds, '",
            "          'got $timeout');",
            "    }",
            "    final uri = Uri.tryParse(url);",
            "    if (uri == null ||",
            "        (uri.scheme != 'http' && uri.scheme != 'https')) {",
            "      throw NewrError(",
            "          \"http: fetch URL must start with http:// or https://, \"",
            "          \"got '$url'\");",
            "    }",
            "    if (uri.host.isEmpty) {",
            "      throw NewrError(\"http: fetch URL has no host: '$url'\");",
            "    }",
            "    if (uri.userInfo.isNotEmpty) {",
            "      throw NewrError(",
            "          'http: a URL must not carry userinfo; read the token '",
            "          'from env and pass it as a header');",
            "    }",
            "    final host = uri.host.toLowerCase();",
            "    if (!_allowlist.contains(host)) {",
            "      throw NewrError(",
            "          \"http: fetch host '$host' is not on the allowlist \"",
            "          '(localhost, 127.0.0.1)');",
            "    }",
            "    final reqHeaders = <String, String>{};",
            "    if (headers != null) {",
            "      reqHeaders.addAll(headers);",
            "    }",
            "    List<int>? data;",
            "    if (body != null) {",
            "      if (body is! String) {",
            "        throw NewrError('http: body must be a str');",
            "      }",
            "      data = utf8.encode(body);",
            "      var hasContentType = false;",
            "      for (final k in reqHeaders.keys) {",
            "        if (k.toLowerCase() == 'content-type') {",
            "          hasContentType = true;",
            "        }",
            "      }",
            "      if (!hasContentType) {",
            "        reqHeaders['Content-Type'] = 'application/json';",
            "      }",
            "    }",
            "    final client = io.HttpClient();",
            "    client.connectionTimeout = Duration(seconds: timeout);",
            "    try {",
            "      final result =",
            "          await _doFetch(client, method, uri, reqHeaders, data,",
            "                  shape)",
            "              .timeout(",
            "        Duration(seconds: timeout),",
            "        onTimeout: () {",
            "          throw NewrError(",
            "              'http: fetch $method $url timed out after '",
            "              '${timeout}s');",
            "        },",
            "      );",
            "      return result as T;",
            "    } on NewrError {",
            "      rethrow;",
            "    } on TimeoutException {",
            "      throw NewrError(",
            "          'http: fetch $method $url timed out after ${timeout}s');",
            "    } catch (e) {",
            "      throw NewrError('http: fetch $method $url failed: $e');",
            "    } finally {",
            "      client.close();",
            "    }",
            "  }",
            "",
            "  static Future<Object?> _doFetch(io.HttpClient client,",
            "      String method, Uri uri, Map<String, String> headers,",
            "      List<int>? data, List<Object?> shape) async {",
            "    final req = await client.openUrl(method, uri);",
            "    headers.forEach((k, v) => req.headers.set(k, v));",
            "    if (data != null) {",
            "      req.add(data);",
            "    }",
            "    final resp = await req.close();",
            "    final chunks = await resp.toList();",
            "    final bytes = <int>[];",
            "    for (final c in chunks) {",
            "      bytes.addAll(c);",
            "    }",
            "    String text;",
            "    try {",
            "      text = utf8.decode(bytes);",
            "    } on FormatException {",
            "      throw NewrError('http: response body is not valid UTF-8');",
            "    }",
            "    final respHeaders = <String, String>{};",
            "    resp.headers.forEach((name, values) {",
            "      respHeaders[name] = values.last;",
            "    });",
            "    final values = <String, Object?>{",
            "      'status': resp.statusCode,",
            "      'headers': respHeaders,",
            "      'body': text,",
            "    };",
            "    return NewrJson.buildFromShape(shape, values);",
            "  }",
            "",
            "  static Future<T> get<T>(String url, int timeout,",
            "      Map<String, String>? headers, List<Object?> shape) {",
            "    return fetch<T>('GET', url, null, timeout, headers, shape);",
            "  }",
            "",
            "  static Future<T> post<T>(String url, String body, int timeout,",
            "      Map<String, String>? headers, List<Object?> shape) {",
            "    return fetch<T>('POST', url, body, timeout, headers, shape);",
            "  }",
            "",
            "  static Future<T> request<T>(String method, String url,",
            "      String body, int timeout, Map<String, String>? headers,",
            "      List<Object?> shape) {",
            "    return fetch<T>(method, url, body, timeout, headers, shape);",
            "  }",
            "}",
        ]

    def _emit_module_member(self, node: A.Member,
                            binding: _Binding) -> tuple[str, int]:
        """A named-module member used as a value: a constant, or a
        function (which becomes a lambda, D29). `binding.module` is
        the real module name (for dispatch); `binding.dart` is the
        Java reference (the alias, when the import is aliased) -
        except for a file module, whose class name is the file stem
        (`_file_module_class`)."""
        real = binding.module or binding.dart
        ref = self._file_module_class(binding)
        t = self._type_of(node)
        if t.name == "fn":
            if binding.kind == "file-module":
                # A function in an imported .newr file, used as a
                # value: a tear-off of the module's top-level fn.
                return (f"{ref}.{self._mangle(node.name)}",
                        _PREC["primary"])
            # A module function as a first-class value.
            if real == "file" and node.name in ("read", "write", "append"):
                self._fail(f"`file.{node.name}` cannot be used as a "
                           "value (it needs a statement - the `with` "
                           "block cannot live inside a lambda)")
            params = t.params[:-1]  # the last param is the return type
            names = [f"_a{i}" for i in range(len(params))]
            typed = ", ".join(
                f"{self._dart_type(p)} _a{i}"
                for i, p in enumerate(params))
            call, _prec = self._emit_module_call_text(
                real, ref, node.name, names)
            if names:
                return (f"({typed}) => {call}", _PREC["primary"])
            return f"() => {call}", _PREC["primary"]
        if real == "env" and node.name == "args":
            self._need_rt("envArgs")
            return "NewrRt.envArgs()", _PREC["primary"]
        if real == "math" and node.name in ("pi", "e"):
            self._need_math()
            return ("math.pi" if node.name == "pi" else "math.e",
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
            return f"{text}.id.toString()"
        if t.name == "str":
            return text
        return f"NewrRt.toStr({text})"

    def _emit_store_call(self, node: A.Call,
                         binding: _Binding) -> tuple[str, int]:
        """`economy.balance(user)` etc. -> `bot.<store>_<op>(...)`
        (D17/P11, J9's Java twin). Every mutation takes its
        idempotency `key:`; the checker enforces that it is present.
        The sqlite3 package is synchronous, so there is no `await`
        - but the §19 guard still applies: the `bot` instance only
        exists inside
        a handler, so a store call at the top level, inside a
        (sync) top-level fn, or inside a lambda is a clean E4001
        instead of uncompilable code."""
        # The Bot method is `bot.<store>_<op>` with the RAW store
        # name (the JS/Python spelling - and the name the Bot
        # class's methods use). binding.dart is the MANGLED name
        # ("economy" is emitter-reserved, so it would be
        # "economy_") - using it here would call a method that
        # does not exist.
        store_name = (node.func.obj.name
                      if isinstance(node.func, A.Member)
                      and isinstance(node.func.obj, A.Name)
                      else binding.dart)
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
            return (f"bot.{store_name}_get("
                    f"{self.emit_expr(table, 0)}, "
                    f"{self.emit_expr(node.args[1], 0)})"
                    f" as {binding.dart}",
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
        checker enforces it - E2009). The kwargs map is a `<String,
        Object?>` literal (a Map literal accepts null values)."""
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
            kwargs.append(f"{_dart_str(name)}: {self.emit_expr(value, 0)}")
        for placeholder in entry.placeholders:
            if placeholder in explicit:
                continue
            binding = self._lookup(placeholder)
            if binding is not None and binding.kind == "handler":
                kwargs.append(f"{_dart_str(placeholder)}: "
                              f"{binding.interp_form()}")
                continue
            self._fail(f"`t(\"{node.key}\")` needs an argument for "
                       f"placeholder `{placeholder}` (the checker "
                       "should have rejected it)")
        args = ", ".join([_dart_str(node.key), locale,
                          "<String, Object?>{" + ", ".join(kwargs) + "}"])
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
        (MessageBuilder.embeds takes builders directly, so there
        is no .build() step - the Java twin built a MessageEmbed
        instead). Returns the lines and the embed variable's name
        (a fresh temp per embed)."""
        p = "  " * indent
        name = self._new_temp_name()
        args: list[str] = []
        fields: list[str] = []
        for field in node.fields:
            if field.kind == "title":
                args.append(f"title: {self.emit_expr(field.value, 0)}")
            elif field.kind == "description":
                args.append("description: "
                            f"{self.emit_expr(field.value, 0)}")
            elif field.kind == "color":
                args.append("color: DiscordColor("
                            f"{self.emit_expr(field.value, 0)})")
            elif field.kind == "field":
                inline = "true" if field.inline else "false"
                # A field's name is a plain string (not an expression).
                fields.append(f"EmbedFieldBuilder(name: "
                              f"{_dart_str(field.name)}, value: "
                              f"{self.emit_expr(field.value, 0)}, "
                              f"isInline: {inline})")
        if fields:
            args.append(f"fields: [{', '.join(fields)}]")
        lines = [f"{p}final {name} = EmbedBuilder("
                 + ", ".join(args) + ");"]
        return lines, name

    # ------------------------------------------------------------ functions

    def _emit_fn(self, decl: A.FnDecl, indent: int = 0) -> list[str]:
        p = "  " * indent
        binding = self._lookup(decl.name)
        java_name = binding.dart if binding is not None \
            else self._mangle(decl.name)
        self._push_scope()
        saved_used = self._fn_used
        self._fn_used = set()
        params = [self._bind_fresh(prm.name, type=prm.type, kind="param")
                  for prm in decl.params]
        sig = ", ".join(f"{self._dart_type(b.type)} {b.dart}"
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
            else self._dart_type(ret)
        if ensures:
            # `result` in an `ensures` clause reads `_newrResult`.
            self._scopes[-1]["result"] = _Binding(dart="_newrResult",
                                                  type=ret,
                                                  kind="local")
            self._fn_used.add("_newrResult")
        self._fn_ctx.append({"return_type": ret,
                             "ensures": ensures, "in_handler": False})
        if requires or ensures:
            # The contract checks below throw NewrError.
            self._need_rt("NewrError")
        is_async = decl.name in self._async_fns
        # A fn that fetches or serves (directly, or through another
        # async fn) is emitted `Future<Ret> name(...) async`:
        # dart:io has no sync HTTP API, so the await must live
        # here, and its call sites await it (see _emit_name_call).
        self._async_ctx.append(is_async)
        if is_async:
            ret_text = f"Future<{ret_text}>"
            lines = [f"{p}{ret_text} {java_name}({sig}) async {{"]
        else:
            lines = [f"{p}{ret_text} {java_name}({sig}) {{"]
        bp = "  " * (indent + 1)
        body_lines: list[str] = []
        if ensures:
            # `_newrResult` is declared ONCE, at the top of the
            # function - every `return` assigns it (a `final` per
            # return would be a redeclaration). A void function
            # still gets one (of type Void) so its `ensures`
            # clauses can read `result`.
            result_type = "Object?" if ret is None or _is_none(ret) \
                else self._dart_type(ret)
            body_lines.append(f"{bp}{result_type} _newrResult;")
        for req in requires:
            cond = self.emit_expr(req.cond, 0)
            body_lines.append(f"{bp}if (!({cond})) {{")
            body_lines.append(
                f"{bp}  throw new NewrError("
                f"{_dart_str('requires failed: ' + cond)});")
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
        self._async_ctx.pop()
        self._fn_ctx.pop()
        self._fn_used = saved_used
        self._pop_scope()
        return lines

    # ------------------------------------------------------------ types / decls

    def _dart_default(self, t: Type) -> str:
        """The default value for a field of type `t` (a variant
        that does not declare the field)."""
        if _is_int(t):
            return "0"
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
        """A struct is a Dart class with final fields, a named-
        parameter constructor (`Point(x: 1, y: 2)` - the call
        sites pass kwargs), and ==/hashCode/toString (the newr
        rendering `Point(x: 1, y: 2)`, via NewrRt.toStr)."""
        p = "  " * indent
        binding = self._lookup(decl.name)
        dart_name = binding.dart if binding is not None \
            else self._mangle(decl.name)
        self._need_rt("toStr")
        self._need_rt("eq")
        self._need_rt("structHash")
        lines = [f"{p}class {dart_name} {{"]
        for f in decl.fields:
            lines.append(f"{p}  final {self._dart_type(f.type)} "
                         f"{self._mangle(f.name)};")
        if decl.fields:
            params = ", ".join(
                f"required this.{self._mangle(f.name)}"
                for f in decl.fields)
            lines.append(f"{p}  {dart_name}({{{params}}});")
        else:
            lines.append(f"{p}  {dart_name}();")
        lines.append("")
        lines.append(f"{p}  @override")
        lines.append(f"{p}  bool operator ==(Object other) {{")
        if decl.fields:
            cond = " && ".join(
                f"NewrRt.eq({self._mangle(f.name)}, "
                f"other.{self._mangle(f.name)})" for f in decl.fields)
            lines.append(f"{p}    return other is {dart_name} && {cond};")
        else:
            lines.append(f"{p}    return other is {dart_name};")
        lines.append(f"{p}  }}")
        lines.append("")
        lines.append(f"{p}  @override")
        if decl.fields:
            args = ", ".join(f"this.{self._mangle(f.name)}"
                             for f in decl.fields)
            lines.append(f"{p}  int get hashCode => "
                         f"NewrRt.structHash([{args}]);")
        else:
            lines.append(f"{p}  int get hashCode => 1;")
        lines.append("")
        lines.append(f"{p}  @override")
        lines.append(f"{p}  String toString() {{")
        if decl.fields:
            parts = ", ".join(
                f"{f.name}: \" + NewrRt.toStr({self._mangle(f.name)}) + \""
                for f in decl.fields)
            lines.append(f"{p}    return \"{dart_name}({parts})\";")
        else:
            lines.append(f"{p}    return \"{dart_name}()\";")
        lines.append(f"{p}  }}")
        lines.append(f"{p}}}")
        return lines

    def _emit_enum(self, decl: A.EnumDecl, indent: int = 0) -> list[str]:
        """An enum is ONE Dart class with a `String _newr_variant`
        discriminant + union fields (the Java twin's shape, K6):
        bare variants are `static final` instances, fielded
        variants are static factory methods with named required
        parameters (`Shape.Circle(radius: 1.5)`), and ==/hashCode/
        toString give the newr rendering (`Shade.Red`,
        `Shape.Circle(radius: 1.5)`)."""
        p = "  " * indent
        binding = self._lookup(decl.name)
        dart_name = binding.dart if binding is not None \
            else self._mangle(decl.name)
        self._need_rt("toStr")
        self._need_rt("eq")
        self._need_rt("structHash")
        union = self._enum_union_fields(decl)
        lines = [f"{p}class {dart_name} {{"]
        lines.append(f"{p}  final String _newr_variant;")
        # A union field a variant does NOT declare is nullable in
        # the class (the bare variant's instance leaves it null);
        # a field every variant declares stays non-nullable.
        # The constructor takes the discriminant plus every union
        # field as a named parameter: `required` when the field is
        # non-nullable (Dart requires a default or `required` for
        # a non-nullable named parameter), plain (null default)
        # when it is nullable.
        field_types: dict[str, Type] = {}
        for fname, ftype in union.items():
            declared_by_all = all(
                fname in [f.name for f in v.fields]
                for v in decl.variants)
            if declared_by_all:
                field_types[fname] = ftype
            else:
                field_types[fname] = Type(
                    ftype.name, params=list(ftype.params),
                    nullable=True)
        for fname, ftype in field_types.items():
            lines.append(f"{p}  final {self._dart_type(ftype)} "
                         f"{self._mangle(fname)};")
        ctor_params = []
        for fname, ftype in field_types.items():
            if ftype.nullable:
                ctor_params.append(f"this.{self._mangle(fname)}")
            else:
                ctor_params.append(
                    f"required this.{self._mangle(fname)}")
        lines.append(f"{p}  {dart_name}._(this._newr_variant"
                     + (f", {{{', '.join(ctor_params)}}}"
                        if ctor_params else "")
                     + ");")
        for variant in decl.variants:
            vname = self._mangle(variant.name)
            if not variant.fields:
                lines.append(f"{p}  static final {dart_name} "
                             f"{vname} = {dart_name}._("
                             f"\"{variant.name}\");")
            else:
                params = ", ".join(
                    f"required {self._dart_type(f.type)} "
                    f"{self._mangle(f.name)}"
                    for f in variant.fields)
                args = ", ".join(
                    f"{self._mangle(f.name)}: {self._mangle(f.name)}"
                    for f in variant.fields)
                lines.append(f"{p}  static {dart_name} {vname}"
                             f"({{{params}}}) => "
                             f"{dart_name}._(\"{variant.name}\", "
                             f"{args});")
        lines.append("")
        lines.append(f"{p}  @override")
        lines.append(f"{p}  bool operator ==(Object other) {{")
        conds = ["_newr_variant == other._newr_variant"]
        for fname in union:
            conds.append(
                f"NewrRt.eq({self._mangle(fname)}, "
                f"other.{self._mangle(fname)})")
        lines.append(f"{p}    return other is {dart_name} && "
                     f"{' && '.join(conds)};")
        lines.append(f"{p}  }}")
        lines.append("")
        lines.append(f"{p}  @override")
        args = ", ".join(["_newr_variant"] +
                         [f"this.{self._mangle(fn)}" for fn in union])
        lines.append(f"{p}  int get hashCode => "
                     f"NewrRt.structHash([{args}]);")
        lines.append("")
        lines.append(f"{p}  @override")
        lines.append(f"{p}  String toString() {{")
        for i, variant in enumerate(decl.variants):
            kw = "if" if i == 0 else "} else if"
            lines.append(f"{p}    {kw} (_newr_variant == "
                         f"{_dart_str(variant.name)}) {{")
            if variant.fields:
                parts = ", ".join(
                    f"{f.name}: \" + NewrRt.toStr({self._mangle(f.name)}) + \""
                    for f in variant.fields)
                lines.append(f"{p}      return \"{dart_name}.{variant.name}({parts})\";")
            else:
                lines.append(f"{p}      return \"{dart_name}.{variant.name}\";")
        lines.append(f"{p}    }}")
        lines.append(f"{p}    return \"{dart_name}.?\";")
        lines.append(f"{p}  }}")
        lines.append(f"{p}}}")
        return lines

    # ------------------------------------------------------------ file assembly

    def _header(self, main_path: str, description: str | None = None) -> list[str]:
        lines = [
            f"// Generated by newr from {main_path} - do not edit by hand.",
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
        # The dart-name -> fn-decl map (for keyword-argument reordering
        # in _emit_kwargs).
        self.fns_by_dart = {}
        for decl in program.functions:
            b = self._lookup(decl.name)
            if b is not None:
                self.fns_by_dart[b.dart] = decl
        # The top level's used-name set starts with every module-level
        # Java name, so a `let` in a top-level block cannot reuse one
        # (at module level Java has no block scope for the top level -
        # the name would clobber the module binding).
        self._fn_used = set(self._module_used)

    def _emit_general_file(self, program: A.Program, main_path: str):
        """A general (non-bot) program: header, imports, then
        top-level declarations - `late` variables for the top-level
        lets/consts (K2's Dart twin: a `late` top-level var is
        assigned in main()/newrInit() in file order, so top-level
        functions can read it and side-effecting initializers run
        in order), structs/enums as classes, top-level functions,
        then `void main(List<String> args)` with the top-level
        STATEMENTS in file order (SPEC §8). A file module gets
        `void newrInit()` instead of main() (its top-level
        statements run when the entry file calls it - the entry
        file calls newrInit() for each directly-imported file
        module that has statements, in import order; transitive
        inits happen through each module's own newrInit).
        Definitions are hoisted - a function body reads module
        globals at call time, so emitting the `function` before
        the statements is safe - but the statements themselves
        (lets, assignments, prints, ...) must run in the order
        written. (M2 evaluation #1.)"""
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
        # Top-level lets/consts: `late` top-level variables (no
        # initializers - main() / newrInit() assigns them in file
        # order). A `const` is `late final`.
        for st in program.stmts:
            if isinstance(st, (A.Let, A.Const)):
                b = self._lookup(st.name)
                t = st.type_ann or self._type_or_none(st.value) or \
                    Type("error")
                late = "late final" if isinstance(st, A.Const) \
                    else "late"
                decl_type = self._dart_type(t)
                if isinstance(st.value, A.Lambda) \
                        and st.name in self._async_lambda_names \
                        and t.name == "fn":
                    # A let-bound lambda that fetches is a
                    # Future-returning fn in Dart.
                    decl_type = self._dart_fn_type_async(t)
                body.append(f"{late} {decl_type} "
                            f"{b.dart if b is not None else self._mangle(st.name)};")
        for decl in program.structs:
            if body:
                body.append("")
            body.extend(self._emit_struct(decl))
        for decl in program.enums:
            if body:
                body.append("")
            body.extend(self._emit_enum(decl))
        for decl in program.functions:
            if body:
                body.append("")
            body.extend(self._emit_fn(decl))
        # Top-level statements: main() for the entry file, a
        # newrInit() for a file module. Emitted FIRST into
        # `stmt_lines` (they may use `db.*`, which decides whether
        # the `NewrDb` helper class is emitted at all — the class
        # must land before main(), and the flag is only final once
        # the statements AND the top-level fn bodies are emitted),
        # then appended after the declarations.
        stmt_lines: list[str] = []
        if program.stmts:
            if is_main:
                main_async = self._entry_async
                init_lines: list[str] = []
                for imp in program.imports:
                    if imp.path is None:
                        continue
                    sub = program.modules.get(imp.bound_name)
                    if sub is None or not sub.stmts:
                        continue
                    stem = self.imported_modules.get(imp.bound_name,
                                                     imp.bound_name)
                    self.dart_imports[imp.bound_name] = stem
                    init_async = _dart_entry_needs_async(
                        sub, self._async_memo)
                    if init_async:
                        main_async = True
                    init_lines.append(
                        f"  {'await ' if init_async else ''}"
                        f"{imp.bound_name}.newrInit();")
                sig = ("Future<void> main(List<String> args) async {"
                       if main_async else
                       "void main(List<String> args) {")
                stmt_lines.append(sig)
                stmt_lines.extend(init_lines)
                self._async_ctx.append(main_async)
                try:
                    stmt_lines.extend(self._emit_block(program.stmts, 1,
                                                       top_level=True))
                finally:
                    self._async_ctx.pop()
                stmt_lines.append("}")
            else:
                sig = ("Future<void> newrInit() async {"
                       if self._entry_async else "void newrInit() {")
                stmt_lines.append(sig)
                self._async_ctx.append(self._entry_async)
                try:
                    stmt_lines.extend(self._emit_block(program.stmts, 1,
                                                       top_level=True))
                finally:
                    self._async_ctx.pop()
                stmt_lines.append("}")
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
        if stmt_lines:
            if body:
                body.append("")
            body.extend(stmt_lines)
        lines = self._header(main_path)
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
        statements); a file module gets a `newrInit()` function."""
        files: dict[str, str] = {}
        self._is_main_file = is_main
        text, uses_rt = self._emit_one(program, main_path, is_main=is_main)
        files[self.output_stem + ".dart"] = text
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
            # shop_items.dart header).
            import os
            sub_path = os.path.relpath(sub_path)
            sub_em = _DartEmitter(sub, main_path=sub_path,
                                  sources=self.sources,
                                  line_comments=self.line_comments,
                                  output_stem=Path(sub_path).stem)
            sub_files, sub_rt = sub_em._emit_file_set(sub, sub_path,
                                                      is_main=False)
            files.update(sub_files)
            uses_rt = uses_rt or sub_rt
            # A `db.*` use in an imported file module needs the
            # sqlite3 dependency in the manifest too (the module's
            # own file imports package:sqlite3 through its
            # `NewrDb` class).
            self.uses_sqlite = self.uses_sqlite or sub_em.uses_sqlite
        return files, uses_rt

    def _emit_one(self, program: A.Program, main_path: str,
                  is_main: bool = True):
        """Emit a single program's file. Returns (text, uses_rt)."""
        self.program = program
        # The http async scan runs before any emission: fn
        # async-ness decides signatures, and entry async-ness
        # decides the main()/newrInit() signature.
        self._async_fns, self._async_lambda_names = _dart_async_info(
            program, self._async_memo)
        self._entry_async = _dart_entry_needs_async(
            program, self._async_memo)
        self._async_ctx = [False]
        self._async_lambda_depth = 0
        if program.bot is not None:
            return self._emit_bot_file(program, main_path)
        return self._emit_general_file(program, main_path)


    # ------------------------------------------------------------ bot emitter
    # nyxx 6.9.1 + nyxx_commands 6.1.0 bot emission (M5 Dart bot
    # phase). The Dart twin of the JS emitter's bot section: the
    # same programs, the same semantics - slash commands via
    # ChatCommand, a Bot class with sqlite3 store methods (P11),
    # component handlers inline after an await (nyxx_commands'
    # await model - no handler maps), a standalone REST deploy
    # program, and a pubspec.yaml.
    #
    # nyxx differences from discord.js (verified live against
    # the 6.9.1 / 6.1.0 packages - see tests/test_emitter_dart.py
    # and ~/.m5-dart/probe-bot/probe.dart):
    # - Commands are ChatCommand values with an `id(...)` execute
    #   closure; options are the closure's parameters (mirrors
    #   load them - @Choices for choices, a const CombineConverter
    #   for min/max).
    # - Components are await-based: ComponentId.generate carries
    #   the `only: author` gate (allowedUser) and the timeout
    #   (expirationTime); the handler body runs inline after
    #   `awaitButtonPress` / `awaitSelection` / `awaitModal`, and
    #   the on_timeout body runs in the InteractionTimeoutException
    #   catch. The timeout also disables the rows first (the
    #   collector's `end` callback, nyxx-shaped).
    # - The store is synchronous (package:sqlite3) - no await,
    #   no locks (Dart is single-threaded, like the JS emitter).
    # - Modals use LabelComponentBuilder + TextInputBuilder.
    # - `member.guild` and `interaction.guild` have no sync
    #   nyxx equivalent (they need an async fetch) - a clean
    #   E4001, use the `guild` binding instead (a known gap).
    # - The `embed` type is EmbedBuilder: MessageBuilder.embeds
    #   takes builders, there is no separate built type to pass
    #   around (unlike JDA's MessageEmbed).

    # Handler bindings: name -> (value form, interpolation form,
    # newr type name). `{self}` is the primary object of the
    # handler (`context` for commands, the component context
    # variable for component continuations); member_join /
    # member_remove / ready / message spell their locals out
    # directly. The interpolation forms are concatenations, not
    # Dart `${...}` strings, so the `{self}` replacement cannot
    # collide with a Dart interpolation brace.
    _BOT_BINDINGS: dict[str, dict[str, tuple[str, str | None, str]]] = {
        "command": {
            "user": ("{self}.user",
                     "\"<@\" + {self}.user.id.toString() + \">\"",
                     "user"),
            "guild": ("{self}.guild!",
                      # Parenthesised: a bare ternary would escape
                      # the `+` chain it is interpolated into
                      # (`"x" + g != null ? ...` reads as
                      # `("x" + g) != null ? ...`).
                      "({self}.guild != null ? {self}.guild!.name "
                      ": \"DM\")",
                      "guild"),
            "channel": ("{self}.channel",
                        "_channelText({self}.channel)",
                        "channel"),
            "interaction": ("{self}.interaction", None, "interaction"),
        },
        "member_join": {
            "user": ("event.member",
                     "\"<@\" + event.member.id.toString() + \">\"",
                     "member"),
            "guild": ("_newrGuild", "_newrGuild.name", "guild"),
        },
        "member_remove": {
            "user": ("event.user",
                     "\"<@\" + event.user.id.toString() + \">\"",
                     "user"),
            "guild": ("_newrGuild", "_newrGuild.name", "guild"),
        },
        "ready": {
            "bot": ("_newrBot", "_newrBot.username", "user"),
        },
        "message": {
            "message": ("event.message",
                        "event.message.content", "message"),
            "user": ("(event.message.author as User)",
                     "\"<@\" + event.message.author.id.toString() "
                     "+ \">\"",
                     "user"),
        },
        "component": {
            "user": ("{self}.user",
                     "\"<@\" + {self}.user.id.toString() + \">\"",
                     "user"),
            "guild": ("{self}.guild!",
                      "({self}.guild != null ? {self}.guild!.name "
                      ": \"DM\")",
                      "guild"),
            "channel": ("{self}.channel",
                        "_channelText({self}.channel)",
                        "channel"),
            "interaction": ("{self}.interaction", None, "interaction"),
        },
    }

    # newr intent name -> GatewayIntents constant(s), or None when
    # the intent is on by default (guild lifecycle events arrive
    # with the default intents, so there is nothing to enable).
    # auto_moderation needs both auto-moderation intents. All
    # names verified live against nyxx 6.9.1.
    _INTENT_MAP: dict[str, str | tuple[str, str] | None] = {
        "guilds": None,
        "members": "guildMembers",
        "moderation": "guildModeration",
        "emojis": "guildExpressions",
        "integrations": None,
        "webhooks": "guildWebhooks",
        "invites": "guildInvites",
        "voice_states": "guildVoiceStates",
        "presences": "guildPresences",
        "guild_messages": "guildMessages",
        "direct_messages": "directMessages",
        "guild_reactions": "guildMessageReactions",
        "direct_message_reactions": "directMessageReactions",
        "guild_typing": "guildMessageTyping",
        "direct_message_typing": "directMessageTyping",
        "message_content": "messageContent",
        "guild_scheduled_events": "guildScheduledEvents",
        "auto_moderation": ("autoModerationConfiguration",
                            "autoModerationExecution"),
        "guild_polls": "guildMessagePolls",
        "direct_message_polls": "directMessagePolls",
    }

    def _bot_intents(self, program: A.Program) -> list[str]:
        """The GatewayIntents constants to enable, sorted. The
        declared intents, plus the ones the events need (member
        events need members, message events need message_content)
        - the JS emitter's _bot_intents, nyxx spellings."""
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
                           "GatewayIntents mapping)")
            mapped = self._INTENT_MAP[intent]
            if isinstance(mapped, tuple):
                out.update(mapped)
            elif mapped is not None:
                out.add(mapped)
        return sorted(out)

    def _emit_bot_file(self, program: A.Program, main_path: str):
        """The bot program: a Dart library of top-level
        declarations (the static members of the general emitter:
        top-level lets as `late` vars, structs, enums, fns), the
        synthesized table row classes, the top-level Bot class
        with the sqlite3 store methods, the STRINGS table + _t,
        the ChatCommand values, the task functions, the
        newrCommandBuilders() table for the deploy program, and
        main()."""
        bot = program.bot
        self.program = program
        self.fns = {f.name: f for f in program.functions}
        self.structs = {s.name: s for s in program.structs}
        self.enums = {e.name: e for e in program.enums}
        self.stores = {s.name: s for s in program.stores}
        self._register_module_scope(program)
        self._fn_ctx = []
        self._hoists = []
        self._bot_command_vars = []
        self._bot_command_builders = []
        self._bot_event_listeners = []
        self._bot_task_fns = []
        self._bot_minmax_helpers = []
        self._bot_minmax_names = set()
        self._bot_pass_helpers = set()
        self._pending_components = []
        self._component_counter = 0
        self._used_channel_name = False
        self._table_rows = {}
        self._task_fn_names = {}
        self._task_fn_name_counts = {}
        self.uses_io = True
        self._need_discord("nyxx")
        # A store table becomes a row class, bound in the module
        # scope as a struct so _dart_type / _struct_dart_name
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
        body: list[str] = []
        for st in program.stmts:
            if isinstance(st, (A.Let, A.Const)):
                b = self._lookup(st.name)
                t = st.type_ann or self._type_or_none(st.value) or \
                    Type("error")
                late = "late final" if isinstance(st, A.Const) \
                    else "late"
                decl_type = self._dart_type(t)
                if isinstance(st.value, A.Lambda) \
                        and st.name in self._async_lambda_names \
                        and t.name == "fn":
                    # A let-bound lambda that fetches is a
                    # Future-returning fn in Dart.
                    decl_type = self._dart_fn_type_async(t)
                body.append(f"{late} {decl_type} "
                            f"{b.dart if b is not None else self._mangle(st.name)};")
        for decl in program.structs:
            if body:
                body.append("")
            body.extend(self._emit_struct(decl))
        for decl in program.enums:
            if body:
                body.append("")
            body.extend(self._emit_enum(decl))
        for table_name in self._table_rows:
            if body:
                body.append("")
            body.extend(
                self._emit_struct(self._table_rows[table_name]))
        for decl in program.functions:
            if body:
                body.append("")
            body.extend(self._emit_fn(decl))

        # The top-level Bot class (store methods) + the `bot`
        # variable.
        bot_lines = self._emit_bot_class(program)
        if bot_lines:
            if body:
                body.append("")
            body.extend(bot_lines)

        # Strings table + _t (only when strings blocks exist).
        strings_lines = self._emit_strings(program)
        if strings_lines:
            if body:
                body.append("")
            body.extend(strings_lines)

        # Commands, events, tasks (these fill the accumulators:
        # command vars + deploy builders, event listeners, task
        # functions, min/max helpers).
        for cmd in program.commands:
            self._emit_command(cmd)
        for ev in program.events:
            self._emit_event_registration(ev)
        for task in program.tasks:
            self._emit_task(task)

        # The channel helpers (only when a channel is rendered
        # as text or named - the flags are set while the commands
        # and events above are emitted).
        if self._used_channel_text:
            if body:
                body.append("")
            body.extend(self._emit_channel_text())
        if self._used_channel_name:
            if body:
                body.append("")
            body.extend(self._emit_channel_name())

        # Min/max + pass-through helpers (only when an option
        # needs a min/max).
        if self._bot_minmax_helpers or self._bot_pass_helpers:
            if body:
                body.append("")
            if "int" in self._bot_pass_helpers:
                body.append("int _newrPassInt(int value, ContextData _) "
                            "=> value;")
            if "double" in self._bot_pass_helpers:
                body.append("double _newrPassDouble(double value, "
                            "ContextData _) => value;")
            body.extend(self._bot_minmax_helpers)

        # The ChatCommand values.
        for var_lines in self._bot_command_vars:
            if body:
                body.append("")
            body.extend(var_lines)

        # The task functions.
        for fn_lines in self._bot_task_fns:
            if body:
                body.append("")
            body.extend(fn_lines)

        # The deploy builders table.
        if body:
            body.append("")
        body.extend(self._emit_commands_table())
        # main() is emitted first into a variable: it may use
        # http/json at the top level, which decides whether the
        # helper classes below are emitted at all.
        main_lines = self._emit_main(program)
        if self._file_uses_http:
            if body:
                body.append("")
            body.extend(self._emit_http_class())
        if self._file_uses_json:
            if body:
                body.append("")
            body.extend(self._emit_json_class())
        body.append("")
        body.extend(main_lines)

        lines = self._header(main_path, bot.description)
        imports = self._render_imports()
        if imports:
            lines.append("")
            lines.extend(imports)
        lines.append("")
        lines.extend(body)
        text = "\n".join(lines) + "\n"
        return text, bool(self.rt_helpers)

    def _emit_commands_table(self) -> list[str]:
        """The `newrCommandBuilders()` table the deploy program
        registers (the Dart twin of the Java COMMANDS table)."""
        lines = ["List<ApplicationCommandBuilder> "
                 "newrCommandBuilders() {",
                 "  return ["]
        for i, entry in enumerate(self._bot_command_builders):
            comma = "," if i < len(self._bot_command_builders) - 1 \
                else ""
            for j, eline in enumerate(entry):
                suffix = comma if j == len(entry) - 1 else ""
                lines.append(f"    {eline}{suffix}")
        lines.append("  ];")
        lines.append("}")
        return lines

    def _emit_strings(self, program: A.Program) -> list[str]:
        """The STRINGS table + _t (the Dart twin of the Python
        emitter's strings method): exact locale, then base
        language, then the default locale; a missing key returns
        the key itself."""
        if not program.strings:
            return []
        self._need_rt("toStr")
        default = program.strings[0].locale
        lines = [f"const String _DEFAULT_LOCALE = "
                 f"{_dart_str(default)};",
                 "final Map<String, Map<String, String>> STRINGS = "
                 "_newrStrings();",
                 "",
                 "Map<String, Map<String, String>> _newrStrings() {",
                 "  final m = <String, Map<String, String>>{};"]
        for decl in program.strings:
            pairs = ", ".join(
                f"{_dart_str(e.key)}: {_dart_str(e.template)}"
                for e in decl.entries)
            lines.append(f"  m[{_dart_str(decl.locale)}] = "
                         f"<String, String>{{{pairs}}};")
        lines.append("  return m;")
        lines.append("}")
        lines.append("")
        lines.append("String _t(String key, String? locale, "
                     "Map<String, Object?> vars) {")
        lines.append("  final loc = locale ?? _DEFAULT_LOCALE;")
        lines.append("  var table = STRINGS[loc];")
        lines.append("  if (table == null && loc.contains('-')) {")
        lines.append("    table = STRINGS[loc.split('-')[0]];")
        lines.append("  }")
        lines.append("  table ??= STRINGS[_DEFAULT_LOCALE];")
        lines.append("  final template = table?[key];")
        lines.append("  if (template == null) {")
        lines.append("    return key;")
        lines.append("  }")
        lines.append("  var out = template;")
        lines.append("  for (final e in vars.entries) {")
        lines.append("    out = out.replaceAll('{${e.key}}', "
                     "NewrRt.toStr(e.value));")
        lines.append("  }")
        lines.append("  return out;")
        lines.append("}")
        return lines

    def _emit_channel_text(self) -> list[str]:
        """The `{channel}` interpolation: the channel as text is
        its mention, or "" when there is none."""
        return [
            "String _channelText(TextChannel? channel) {",
            "  if (channel == null) {",
            "    return \"\";",
            "  }",
            "  return '<#${channel.id}>';",
            "}",
        ]

    def _emit_channel_name(self) -> list[str]:
        """The `channel.name` field: only guild channels have a
        name in nyxx (a DM channel has none), so anything else
        reads as ""."""
        return [
            "String _channelName(TextChannel channel) {",
            "  if (channel is GuildChannel) {",
            "    return channel.name;",
            "  }",
            "  return '';",
            "}",
        ]

    # ------------------------------------------------------------ store (P11)
    # One long-lived sqlite3 connection per store (created in
    # setup(), before login), a commit per mutation, the ledger
    # and the balance in one transaction, and a duplicate
    # idempotency key replays instead of double-applying - the
    # Dart twin of the JS emitter's _emit_store_methods. The
    # sqlite3 package is synchronous, so there is no await and
    # no lock (Dart is single-threaded, like the JS emitter).
    # Balances are stored as TEXT and parsed with int.parse;
    # arithmetic goes through NewrRt.add/sub, never raw +/- on
    # ints (checked - a silently wrapped balance is a money
    # bug). `db.execute` / `db.select` take the parameters
    # inline, so no prepared statement is ever left open.

    _SQL_TYPES = {"int": "TEXT", "str": "TEXT", "float": "REAL",
                  "bool": "INTEGER"}

    def _emit_bot_class(self, program: A.Program) -> list[str]:
        if not program.stores:
            return []
        self._need_sqlite()
        lines = ["class Bot {"]
        for store in program.stores:
            lines.append(f"  late Database {store.name}_db;")
        lines.append("")
        lines.append("  void setup() {")
        for store in program.stores:
            lines.append(f"    _{store.name}_connect();")
        lines.append("  }")
        for store in program.stores:
            lines.extend(self._emit_store_methods(store))
        lines.append("}")
        lines.append("")
        lines.append("late Bot bot;")
        return lines

    def _emit_store_methods(self, store: A.StoreDecl) -> list[str]:
        name = store.name
        lines = ["",
                 f"  void _{name}_connect() {{",
                 f"    {name}_db = sqlite3.open('{name}.db');",
                 f"    {name}_db.execute('PRAGMA busy_timeout = 5000');"]
        for table in store.tables:
            cols = ", ".join(
                f"{c.name} {self._SQL_TYPES.get(c.type.name, 'TEXT')}"
                f"{' PRIMARY KEY' if i == 0 else ''}"
                for i, c in enumerate(table.columns))
            lines.append(f"    {name}_db.execute('CREATE TABLE IF NOT "
                         f"EXISTS {table.name} ({cols})');")
        if store.ledger:
            lines.append(f"    {name}_db.execute('CREATE TABLE IF NOT "
                         "EXISTS ledger (id INTEGER PRIMARY KEY "
                         "AUTOINCREMENT, user TEXT NOT NULL, amount "
                         "TEXT NOT NULL, key TEXT UNIQUE, "
                         "created_at INTEGER NOT NULL)');")
        lines.append("  }")
        lines.append("")
        lines.append(f"  void _{name}_rollback() {{")
        lines.append("    try {")
        lines.append(f"      {name}_db.execute('ROLLBACK');")
        lines.append("    } catch (_) {")
        lines.append("      // The original error is the one that "
                     "matters.")
        lines.append("    }")
        lines.append("  }")
        tables = {t.name: t for t in store.tables}
        if "balances" in tables:
            lines.extend(self._emit_economy_methods(name))
        for table in store.tables:
            lines.extend(self._emit_table_methods(name, table))
        return lines

    def _sql_read_balance(self, db: str, user_expr: str, p: str,
                          var: str = "cur") -> list[str]:
        """Lines declaring `int <var>` with the balance for
        `user_expr` (0 when the row is absent). `var` exists so a
        method that reads two balances (transfer) can use two
        names in the same scope."""
        return [
            f"{p}final {var}Rows = {db}.select('SELECT balance "
            f"FROM balances WHERE user = ?', [{user_expr}]);",
            f"{p}final int {var} = {var}Rows.isEmpty ? 0 : "
            f"int.parse({var}Rows.first['balance'] as String);",
        ]

    def _sql_upsert_balance(self, db: str, user_expr: str,
                            next_expr: str, p: str) -> list[str]:
        return [
            f"{p}{db}.execute('INSERT INTO balances (user, balance) "
            "VALUES (?, ?) ON CONFLICT(user) DO UPDATE SET balance "
            f"= excluded.balance', [{user_expr}, {next_expr}"
            ".toString()]);",
        ]

    def _sql_insert_ledger(self, db: str, user_expr: str,
                           amount_expr: str, key_expr: str, p: str) \
            -> list[str]:
        return [
            f"{p}{db}.execute('INSERT INTO ledger (user, amount, "
            f"key, created_at) VALUES (?, ?, ?, ?)', [{user_expr}, "
            f"{amount_expr}.toString(), {key_expr}, "
            "DateTime.now().millisecondsSinceEpoch ~/ 1000]);",
        ]

    def _sql_ledger_has_key(self, db: str, key_expr: str, p: str) \
            -> list[str]:
        """Lines declaring `bool dup` - whether the ledger
        already holds `key_expr`."""
        return [
            f"{p}final dupRows = {db}.select('SELECT 1 FROM ledger "
            f"WHERE key = ?', [{key_expr}]);",
            f"{p}final bool dup = dupRows.isNotEmpty;",
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
                 f"  int {name}_balance(String user) {{"]
        lines.extend(self._sql_read_balance(db, "user", "    "))
        lines.append("    return cur;")
        lines.append("  }")
        # _apply: the shared credit/debit core. A duplicate key
        # replays (true); a debit with insufficient funds rolls
        # back and returns false; the ledger row is written in
        # the same transaction as the balance.
        lines.append("")
        lines.append(f"  bool _{name}_apply(String user, "
                     "int delta, String? key) {")
        lines.append("    if (key != null) {")
        lines.extend(self._sql_ledger_has_key(db, "key", "      "))
        lines.append("      if (dup) {")
        lines.append("        return true;")
        lines.append("      }")
        lines.append("    }")
        lines.append(f"    {db}.execute('BEGIN IMMEDIATE');")
        lines.append("    try {")
        lines.extend(self._sql_read_balance(db, "user", "      "))
        lines.append("      final next = NewrRt.add(cur, delta);")
        lines.append("      if (next < 0) {")
        lines.append(f"        {db}.execute('ROLLBACK');")
        lines.append("        return false;")
        lines.append("      }")
        lines.extend(self._sql_upsert_balance(
            db, "user", "next", "      "))
        lines.append("      if (key != null) {")
        lines.extend(self._sql_insert_ledger(
            db, "user", "delta", "key", "        "))
        lines.append("      }")
        lines.append(f"      {db}.execute('COMMIT');")
        lines.append("      return true;")
        lines.append("    } catch (e) {")
        lines.append(f"      _{name}_rollback();")
        lines.append("      rethrow;")
        lines.append("    }")
        lines.append("  }")
        lines.append("")
        lines.append(f"  void {name}_credit(String user, "
                     "int delta, String? key) {")
        lines.append(f"    _{name}_apply(user, delta, key);")
        lines.append("  }")
        lines.append("")
        lines.append(f"  bool {name}_debit(String user, "
                     "int delta, String? key) {")
        lines.append(f"    return _{name}_apply(user, NewrRt.sub(0, "
                     "delta), key);")
        lines.append("  }")
        lines.append("")
        lines.append(f"  bool {name}_transfer(String from, "
                     "String to, int amount, String? key) {")
        lines.append("    if (key != null) {")
        lines.extend(self._sql_ledger_has_key(db, "key", "      "))
        lines.append("      if (dup) {")
        lines.append("        return true;")
        lines.append("      }")
        lines.append("    }")
        lines.append("    if (amount <= 0) {")
        lines.append("      return false;")
        lines.append("    }")
        lines.append(f"    {db}.execute('BEGIN IMMEDIATE');")
        lines.append("    try {")
        lines.extend(self._sql_read_balance(db, "from", "      ",
                                            var="curFrom"))
        lines.append("      final fromBalance = curFrom;")
        lines.append("      if (fromBalance < amount) {")
        lines.append(f"        {db}.execute('ROLLBACK');")
        lines.append("        return false;")
        lines.append("      }")
        lines.append("      final nextFrom = NewrRt.sub(fromBalance, "
                     "amount);")
        lines.extend(self._sql_upsert_balance(
            db, "from", "nextFrom", "      "))
        lines.append("      if (key != null) {")
        lines.extend(self._sql_insert_ledger(
            db, "from", "NewrRt.sub(0, amount)", "key", "        "))
        lines.append("      }")
        lines.append("      // Heal the receiver's ledger: a "
                     "previous failed transfer may have logged the "
                     "out leg but not the in leg; replay it with "
                     "the opposite key before crediting.")
        lines.append("      var healed = false;")
        lines.append("      var sumOut = 0;")
        lines.append("      var sumIn = 0;")
        lines.append("      var sumKeys = 0;")
        lines.append(f"      final ledgerRows = {db}.select('SELECT "
                     "amount, key FROM ledger WHERE user = ?', [to]);")
        lines.append("      for (final row in ledgerRows) {")
        lines.append("        final k = row['key'] as String?;")
        lines.append("        final a = int.parse(row['amount'] "
                     "as String);")
        lines.append("        if (k != null && k.endsWith(':out')) {")
        lines.append("          sumOut = NewrRt.add(sumOut, a);")
        lines.append("        } else if (k != null && "
                     "k.endsWith(':in')) {")
        lines.append("          sumIn = NewrRt.add(sumIn, a);")
        lines.append("        } else {")
        lines.append("          sumKeys = NewrRt.add(sumKeys, a);")
        lines.append("        }")
        lines.append("      }")
        lines.append("      if (sumOut > 0 && sumIn == 0) {")
        lines.append("        final heal = amount < sumOut ? amount "
                     ": sumOut;")
        lines.append(f"        {db}.execute('INSERT INTO ledger "
                     "(user, amount, key, created_at) VALUES "
                     "(?, ?, ?, ?)', [to, heal.toString(), key != null "
                     "? key + ':in' : null, DateTime.now()"
                     ".millisecondsSinceEpoch ~/ 1000]);")
        lines.append("        healed = true;")
        lines.append("      }")
        lines.extend(self._sql_read_balance(db, "to", "      ",
                                            var="curTo"))
        lines.append("      final toBalance = curTo;")
        lines.append("      final nextTo = NewrRt.add(toBalance, amount);")
        lines.append("      if (!healed) {")
        lines.extend(self._sql_insert_ledger(
            db, "to", "amount", "key != null ? key + ':in' : null",
            "        "))
        lines.append("      }")
        lines.extend(self._sql_upsert_balance(
            db, "to", "nextTo", "      "))
        lines.append(f"      {db}.execute('COMMIT');")
        lines.append("      return true;")
        lines.append("    } catch (e) {")
        lines.append(f"      _{name}_rollback();")
        lines.append("      rethrow;")
        lines.append("    }")
        lines.append("  }")
        return lines

    def _sql_key_arg(self, col_type: str, value: str) -> str:
        """The sqlite parameter for a key/value of `col_type`
        (an int key stays an int; a bool becomes 0/1)."""
        if col_type == "int":
            return f"({value} as int)"
        if col_type == "float":
            return f"(({value} as num).toDouble())"
        if col_type == "bool":
            return f"(({value} as bool) ? 1 : 0)"
        return f"({value} as String)"

    def _sql_value_arg(self, col_type: str, field: str) -> str:
        """The sqlite parameter for a row field (an int is stored
        as its decimal TEXT; a bool as 0/1)."""
        if col_type == "int":
            return f"{field}.toString()"
        if col_type == "float":
            return field
        if col_type == "bool":
            return f"({field} ? 1 : 0)"
        return field

    def _sql_value_reader(self, col_type: str, col: str) -> str:
        """The row read for a row field (a float may come back as
        an int when the stored value is whole, so read through
        num)."""
        if col_type == "int":
            return f"int.parse(row['{col}'] as String)"
        if col_type == "float":
            return f"(row['{col}'] as num).toDouble()"
        if col_type == "bool":
            return f"(row['{col}'] as int) != 0"
        return f"row['{col}'] as String"

    def _emit_table_methods(self, name: str, table: A.TableDecl) \
            -> list[str]:
        """get / set / delete for one store, one method each
        dispatching on the table name (the JS emitter's shape).
        `get` returns the row object or null; `set` upserts;
        `delete` removes the row."""
        db = f"{name}_db"
        row = self._lookup(table.name)
        row_dart = row.dart if row is not None \
            else self._mangle(table.name)
        key_col = table.columns[0]
        cols = ", ".join(c.name for c in table.columns)
        placeholders = ", ".join("?" for _ in table.columns)
        set_clause = ", ".join(
            f"{c.name} = excluded.{c.name}" for c in table.columns)
        key_arg = self._sql_key_arg(key_col.type.name, "key")
        lines = ["",
                 f"  Object? {name}_get(String table, "
                 "Object key) {"]
        lines.append(f"    if (table == {_dart_str(table.name)}) {{")
        lines.append(f"      final rows = {db}.select('SELECT * FROM "
                     f"{table.name} WHERE {key_col.name} = ?', "
                     f"[{key_arg}]);")
        lines.append("      if (rows.isEmpty) {")
        lines.append("        return null;")
        lines.append("      }")
        lines.append("      final row = rows.first;")
        reads = ", ".join(
            f"{self._mangle(c.name)}: "
            f"{self._sql_value_reader(c.type.name, c.name)}"
            for c in table.columns)
        lines.append(f"      return {row_dart}({reads});")
        lines.append("    }")
        lines.append("    throw ArgumentError('unknown table: $table');")
        lines.append("  }")
        lines.append("")
        lines.append(f"  void {name}_set(String table, "
                     "Object row) {")
        lines.append(f"    if (table == {_dart_str(table.name)}) {{")
        lines.append(f"      final r = row as {row_dart};")
        args = ", ".join(
            self._sql_value_arg(c.type.name,
                                f"r.{self._mangle(c.name)}")
            for c in table.columns)
        lines.append(f"      {db}.execute('INSERT INTO "
                     f"{table.name} ({cols}) VALUES ({placeholders}) "
                     f"ON CONFLICT({key_col.name}) DO UPDATE SET "
                     f"{set_clause}', [{args}]);")
        lines.append("      return;")
        lines.append("    }")
        lines.append("    throw ArgumentError('unknown table: $table');")
        lines.append("  }")
        lines.append("")
        lines.append(f"  void {name}_delete(String table, "
                     "Object key) {")
        lines.append(f"    if (table == {_dart_str(table.name)}) {{")
        lines.append(f"      {db}.execute('DELETE FROM "
                     f"{table.name} WHERE {key_col.name} = ?', "
                     f"[{key_arg}]);")
        lines.append("      return;")
        lines.append("    }")
        lines.append("    throw ArgumentError('unknown table: $table');")
        lines.append("  }")
        return lines

    def _emit_deploy_file(self, program: A.Program, bot_filename: str):
        """The standalone deploy program: connect with the REST
        client, register the newrCommandBuilders() table (guild
        when DISCORD_GUILD_ID is set, else globally), print how
        many were registered, close. Run once, then run the bot."""
        stem = Path(bot_filename).stem
        lines = self._header(self.main_path,
                             "Command registration - run once, "
                             "then run the bot.")
        lines.append("")
        lines.append("import 'dart:io';")
        lines.append("")
        lines.append("import 'package:nyxx/nyxx.dart';")
        lines.append("")
        lines.append(f"import '{stem}.dart';")
        lines.append("")
        lines.append("Future<void> main(List<String> args) async {")
        lines.append("  final token = Platform.environment"
                     "['DISCORD_TOKEN'];")
        lines.append("  final clientId = Platform.environment"
                     "['DISCORD_CLIENT_ID'];")
        lines.append("  final guildId = Platform.environment"
                     "['DISCORD_GUILD_ID'];")
        lines.append("  if (token == null || token.isEmpty || "
                     "clientId == null || clientId.isEmpty) {")
        lines.append("    throw StateError('set DISCORD_TOKEN and "
                     "DISCORD_CLIENT_ID');")
        lines.append("  }")
        lines.append("  final rest = await Nyxx.connectRest(token);")
        lines.append("  final builders = newrCommandBuilders();")
        lines.append("  if (guildId != null && guildId.isNotEmpty) {")
        lines.append("    final guild = rest.guilds["
                     "Snowflake.parse(guildId)];")
        lines.append("    await guild.commands.bulkOverride(builders);")
        lines.append("    print('Registered ${builders.length} "
                     "command(s) in guild $guildId.');")
        lines.append("  } else {")
        lines.append("    await rest.commands.bulkOverride(builders);")
        lines.append("    print('Registered ${builders.length} "
                     "command(s) globally.');")
        lines.append("  }")
        lines.append("  await rest.close();")
        lines.append("}")
        return "\n".join(lines) + "\n"

    # ------------------------------------------------------------ commands
    # newr option type -> the CommandOptionType for the deploy
    # builder. A `member` option is a USER option on the wire
    # (the member converter distinguishes it); a `channel`
    # option is a CHANNEL option.
    _OPTION_TYPE = {"str": "string", "int": "integer",
                    "num": "number", "float": "number",
                    "bool": "boolean", "user": "user",
                    "member": "user", "channel": "channel",
                    "role": "role"}

    # newr option type -> the Dart type of the execute
    # closure's parameter. A `channel` option is a
    # GuildTextChannel: that is what textGuildChannelConverter
    # produces, and it IS a TextChannel, so the body can use it
    # wherever the newr `channel` type goes.
    _OPTION_PARAM_TYPE = {"str": "String", "int": "int",
                          "num": "double", "float": "double",
                          "bool": "bool", "user": "User",
                          "member": "Member",
                          "channel": "GuildTextChannel",
                          "role": "Role"}

    def _command_var_name(self, name: str) -> str:
        """`_newrCmdPing` for `/ping` - unique per file."""
        words = "".join(ch if ch.isalnum() else " " for ch in name).split()
        base = "".join(w.capitalize() for w in words) or "Command"
        return "_newrCmd" + base

    def _option_builder(self, opt: A.OptionDecl) -> str:
        """The `CommandOptionBuilder(...)` for one command
        option (the deploy table)."""
        t = opt.type
        tname = t.name if t is not None else "str"
        otype = self._OPTION_TYPE.get(tname)
        if otype is None:
            self._fail(f"option `{opt.name}` has a type that cannot "
                       "be a slash-command option")
        desc = opt.description if opt.description is not None else opt.name
        parts = [f"type: CommandOptionType.{otype}",
                 f"name: {_dart_str(opt.name)}",
                 f"description: {_dart_str(desc)}",
                 f"isRequired: {'true' if opt.required else 'false'}"]
        if opt.min_value is not None:
            parts.append("minValue: "
                         f"{self._emit_option_bound(opt.min_value, tname)}")
        if opt.max_value is not None:
            parts.append("maxValue: "
                         f"{self._emit_option_bound(opt.max_value, tname)}")
        if opt.choices:
            choices = ", ".join(
                f"CommandOptionChoiceBuilder(name: "
                f"{self._emit_choice_name(c, tname)}, value: "
                f"{self._emit_choice_value(c, tname)})"
                for c in opt.choices)
            parts.append(f"choices: [{choices}]")
        return ("CommandOptionBuilder(" + ", ".join(parts) + ")")

    def _emit_option_bound(self, expr, tname: str) -> str:
        """An option's min/max bound: an int bound is an int
        literal, a number bound a double literal."""
        if isinstance(expr, A.Lit):
            if tname == "int":
                return f"{expr.value}"
            if tname in ("num", "float"):
                return _dart_float(float(expr.value))
        text = self.emit_expr(expr, 0)
        if tname in ("num", "float") and self._type_of(expr).name == "int":
            return f"({text}).toDouble()"
        return text

    def _emit_choice_name(self, expr, tname: str) -> str:
        text = self.emit_expr(expr, 0)
        if tname == "str":
            return text
        return f"({text}).toString()"

    def _emit_choice_value(self, expr, tname: str) -> str:
        text = self.emit_expr(expr, 0)
        if tname not in ("str", "int", "num", "float"):
            self._fail("only str / int / num options can have choices")
        if tname in ("num", "float") \
                and self._type_of(expr).name == "int":
            return f"({text}).toDouble()"
        return text

    def _option_dart_type(self, opt: A.OptionDecl) -> Type:
        tname = opt.type.name if opt.type is not None else "str"
        if tname == "num":
            tname = "float"
        if opt.required:
            return Type(tname)
        # An optional option without a default is null when the
        # user did not pass it; with a default it is never null.
        return Type(tname, nullable=opt.default is None)

    def _option_param(self, cmd: A.CommandDecl,
                      opt: A.OptionDecl) -> str:
        """One parameter of the execute closure: the converter
        annotation (a const CombineConverter for a min/max int
        or number, @Choices for choices), the parameter type
        (the converter's type - a channel option is a
        GuildTextChannel), and the default for an optional
        option. An optional parameter is wrapped in `[...]`;
        the annotation goes INSIDE the brackets."""
        tname = opt.type.name if opt.type is not None else "str"
        ptype = self._OPTION_PARAM_TYPE.get(tname)
        if ptype is None:
            self._fail(f"option `{opt.name}` has a type that cannot "
                       "be a slash-command option")
        name = self._mangle(opt.name)
        ann = ""
        if opt.choices:
            choices = ", ".join(
                f"{self._emit_choice_name(c, tname)}: "
                f"{self._emit_choice_value(c, tname)}"
                for c in opt.choices)
            ann = f"@Choices({{{choices}}}) "
        elif opt.min_value is not None or opt.max_value is not None:
            helper = self._minmax_helper_name(cmd, opt)
            if tname == "int":
                conv = "int"
                base = "intConverter"
                self._bot_pass_helpers.add("int")
                pass_fn = "_newrPassInt"
            else:
                conv = "double"
                base = "doubleConverter"
                self._bot_pass_helpers.add("double")
                pass_fn = "_newrPassDouble"
            ann = (f"@UseConverter(CombineConverter<{conv}, {conv}>("
                   f"{base}, {pass_fn}, processOptionCallback: "
                   f"{helper})) ")
        if opt.required:
            return f"{ann}{ptype} {name}"
        if opt.default is not None:
            default = self.emit_expr(opt.default, 0)
            return f"[{ann}{ptype} {name} = {default}]"
        ntype = ptype + "?"
        return f"[{ann}{ntype} {name}]"

    def _minmax_helper_name(self, cmd: A.CommandDecl,
                            opt: A.OptionDecl) -> str:
        """The top-level min/max callback for one option:
        `_newrMinMaxBuyAmount`. Emitted once, into
        _bot_minmax_helpers (a numeric suffix when the same
        name repeats)."""
        cmd_words = "".join(ch if ch.isalnum() else " "
                            for ch in cmd.name).split()
        opt_words = "".join(ch if ch.isalnum() else " "
                            for ch in opt.name).split()
        base = ("_newrMinMax"
                + "".join(w.capitalize() for w in cmd_words)
                + "".join(w.capitalize() for w in opt_words))
        name = base
        n = 2
        while name in self._bot_minmax_names:
            name = f"{base}{n}"
            n += 1
        self._bot_minmax_names.add(name)
        tname = opt.type.name if opt.type is not None else "str"
        lines = [f"void {name}(CommandOptionBuilder builder) {{"]
        if opt.min_value is not None:
            lines.append("  builder.minValue = "
                         f"{self._emit_option_bound(opt.min_value, tname)};")
        if opt.max_value is not None:
            lines.append("  builder.maxValue = "
                         f"{self._emit_option_bound(opt.max_value, tname)};")
        lines.append("}")
        self._bot_minmax_helpers.extend(lines)
        return name

    def _emit_command(self, cmd: A.CommandDecl):
        """A slash command: its ApplicationCommandBuilder entry
        (for newrCommandBuilders()) and its ChatCommand value
        with an `id(...)` execute closure. Options are the
        closure's parameters; the handler bindings for them are
        bound to the parameter names."""
        desc = cmd.description if cmd.description is not None else cmd.name
        entry = [f"ApplicationCommandBuilder(",
                 f"  name: {_dart_str(cmd.name)},",
                 f"  description: {_dart_str(desc)},",
                 "  type: ApplicationCommandType.chatInput,"]
        if cmd.options:
            entry.append("  options: [")
            for i, opt in enumerate(cmd.options):
                comma = "," if i < len(cmd.options) - 1 else ""
                entry.append(f"    {self._option_builder(opt)}{comma}")
            entry.append("  ],")
        entry.append(")")
        self._bot_command_builders.append(entry)
        var = self._command_var_name(cmd.name)
        lines = [f"final ChatCommand {var} = ChatCommand(",
                 f"  {_dart_str(cmd.name)},",
                 f"  {_dart_str(desc)},"]
        params = ["InteractionChatContext context"]
        for opt in cmd.options:
            params.append(self._option_param(cmd, opt))
        lines.append(f"  id({_dart_str(cmd.name)}, ("
                     + ", ".join(params) + ") async {")
        self._push_handler(
            "command", ["user", "guild", "channel", "interaction"],
            reply="interaction",
            locale="context.interaction.locale?.identifier",
            self_expr="context", responder="context")
        try:
            for opt in cmd.options:
                tname = opt.type.name if opt.type is not None \
                    else "str"
                name = self._mangle(opt.name)
                if tname == "user" or tname == "member":
                    interp = (f"\"<@\" + {name}.id.toString() "
                              "+ \">\"")
                elif tname == "channel":
                    interp = f"_channelText({name})"
                elif tname == "role":
                    interp = f"{name}.name"
                else:
                    interp = None
                self._handler_binding(
                    opt.name, name, interp,
                    self._option_dart_type(opt))
            lines.extend(self._emit_block(cmd.effective_body(), 2,
                                          new_scope=False))
        finally:
            self._pop_handler()
        lines.append("  }),")
        lines.append("  options: const CommandOptions("
                     "type: CommandType.slashOnly),")
        lines.append(");")
        self._bot_command_vars.append(lines)

    # ------------------------------------------------------------ events / tasks

    def _emit_event_registration(self, ev: A.EventDecl):
        """A client stream listener, registered in main() (the
        lines go into _bot_event_listeners). The system channel
        is a local named `_newrSystemChannel`; member_remove
        uses `event.user` (always present); the message handler
        ignores the bot's own messages."""
        if ev.kind == "ready":
            lines = ["  client.onReady.listen((event) async {"]
            lines.append("    final User _newrBot = "
                         "await client.user.fetch();")
            self._push_handler("ready", ["bot"], reply=None,
                               locale=None, self_expr=None,
                               responder=None)
            try:
                lines.extend(self._emit_block(ev.effective_body(), 2,
                                              new_scope=False))
            finally:
                self._pop_handler()
            lines.append("  });")
            self._bot_event_listeners.extend(lines)
            return
        if ev.kind in ("member_join", "member_remove"):
            name = ("onGuildMemberAdd" if ev.kind == "member_join"
                    else "onGuildMemberRemove")
            lines = [f"  client.{name}.listen((event) async {{"]
            lines.append("    final Guild _newrGuild = "
                         "await event.guild.fetch();")
            lines.append("    final _newrSystemChannel = "
                         "_newrGuild.systemChannel;")
            lines.append("    if (_newrSystemChannel != null) {")
            ctx = self._push_handler(
                ev.kind, ["user", "guild"], reply="system_channel",
                locale="_newrGuild.preferredLocale.identifier",
                self_expr=None, responder="_newrSystemChannel")
            try:
                lines.extend(self._emit_block(ev.effective_body(), 3,
                                              new_scope=False))
            finally:
                self._pop_handler()
            lines.append("    }")
            lines.append("  });")
            self._bot_event_listeners.extend(lines)
            return
        if ev.kind == "message":
            lines = ["  client.onMessageCreate.listen((event) async {"]
            lines.append("    if (event.message.author.id == "
                         "client.user.id) {")
            lines.append("      return;")
            lines.append("    }")
            self._push_handler(
                "message", ["message", "user"],
                reply="message_channel", locale=None,
                self_expr=None, responder="event.message.channel")
            try:
                lines.extend(self._emit_block(ev.effective_body(), 2,
                                              new_scope=False))
            finally:
                self._pop_handler()
            lines.append("  });")
            self._bot_event_listeners.extend(lines)
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
        """A task function (scheduled from main() with
        Timer.periodic)."""
        name = self._task_fn_name(task)
        lines = [f"Future<void> {name}() async {{"]
        self._push_handler("task", [], reply=None, locale=None,
                           self_expr=None, responder=None)
        try:
            lines.extend(self._emit_block(task.body, 1,
                                          new_scope=False))
        finally:
            self._pop_handler()
        lines.append("}")
        self._bot_task_fns.append(lines)
        self.uses_async = True

    def _emit_main(self, program: A.Program) -> list[str]:
        """main(): the top-level statements first (they run before
        login, like the general emitter), then the token check,
        the store setup, the gateway login, the listener
        registrations, and the task schedules. The locals are
        `_newrToken` / `_newrCommands` / `_newrClient` - a
        top-level `let token` is a `late` var, and a local named
        `token` would shadow it inside main()."""
        lines = ["Future<void> main(List<String> args) async {"]
        self._fn_ctx = []
        # Bot main is async, so top-level http fetch/serve calls
        # await here (json calls are sync; http inside a bot
        # handler is not in M8b and fails in _await_call).
        self._async_ctx.append(True)
        try:
            lines.extend(self._emit_block(program.stmts, 1,
                                          top_level=True))
        finally:
            self._async_ctx.pop()
        lines.append("  final _newrToken = "
                     "Platform.environment['DISCORD_TOKEN'];")
        lines.append("  if (_newrToken == null || _newrToken.isEmpty) {")
        lines.append("    throw StateError('set the DISCORD_TOKEN "
                     "environment variable');")
        lines.append("  }")
        if program.stores:
            lines.append("  bot = Bot();")
            lines.append("  bot.setup();")
        lines.append("  final _newrCommands = CommandsPlugin(")
        lines.append("    prefix: null,")
        lines.append("    options: CommandsOptions(logErrors: true),")
        lines.append("  );")
        for cmd in program.commands:
            var = self._command_var_name(cmd.name)
            lines.append(f"  _newrCommands.addCommand({var});")
        if program.commands:
            self._need_rt("printErr")
            lines.append("  _newrCommands.onCommandError.listen("
                         "(error) async {")
            lines.append("    NewrRt.printErr('[dispatch] ' + "
                         "error.toString());")
            lines.append("    if (error is CommandInvocationException) {")
            lines.append("      final context = error.context;")
            lines.append("      if (context is InteractionChatContext) {")
            lines.append("        try {")
            lines.append("          await context.respond("
                         "MessageBuilder(content: 'Something went wrong "
                         "running that command. Try again in a "
                         "moment.'),")
            lines.append("            level: ResponseLevel.hint);")
            lines.append("        } catch (replyError) {")
            lines.append("          NewrRt.printErr('[dispatch] could "
                         "not send the error reply: ' + "
                         "replyError.toString());")
            lines.append("        }")
            lines.append("      }")
            lines.append("    }")
            lines.append("  });")
        intents = self._bot_intents(program)
        intent_expr = " | ".join(
            f"GatewayIntents.{i}" for i in intents) or \
            "GatewayIntents.guilds"
        if self._bot_event_listeners:
            # Only the event listeners use `client`; a bot with no
            # events would leave the alias unused (dart analyze
            # warning, exit 2), so the alias exists only then.
            lines.append("  final _newrClient = await "
                         "Nyxx.connectGateway(")
            lines.append("    _newrToken,")
            lines.append(f"    {intent_expr},")
            lines.append("    options: GatewayClientOptions(plugins: [")
            lines.append("      _newrCommands,")
            lines.append("      ignoreExceptions,")
            lines.append("    ]),")
            lines.append("  );")
            lines.append("  final client = _newrClient;")
        else:
            # No listener reads the client back, so await the login
            # as a bare statement rather than keep an unused local.
            lines.append("  await Nyxx.connectGateway(")
            lines.append("    _newrToken,")
            lines.append(f"    {intent_expr},")
            lines.append("    options: GatewayClientOptions(plugins: [")
            lines.append("      _newrCommands,")
            lines.append("      ignoreExceptions,")
            lines.append("    ]),")
            lines.append("  );")
        lines.extend(self._bot_event_listeners)
        for task in program.tasks:
            name = self._task_fn_name(task)
            secs = task.interval_seconds
            self._need_rt("printErr")
            lines.append(f"  Timer.periodic(Duration(seconds: {secs}), "
                         "(_) async {")
            lines.append("    try {")
            lines.append(f"      await {name}();")
            lines.append("    } catch (e) {")
            lines.append("      NewrRt.printErr(e);")
            lines.append("    }")
            lines.append("  });")
        lines.append("}")
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

    def _message_builder(self, arg: str, is_embed: bool,
                         components: str | None = None) -> str:
        """`MessageBuilder(content: ...)` / `MessageBuilder(embeds:
        [...])`, with an optional `components: [...]`."""
        if is_embed:
            inner = f"embeds: [{arg}]"
        else:
            inner = f"content: {arg}"
        if components is not None:
            inner += f", components: [{components}]"
        return f"MessageBuilder({inner})"

    def _answer_call(self, kind: str, arg: str, is_embed: bool,
                     ephemeral: bool = False) -> str:
        """The nyxx call that answers on `kind` - `await`ed, so
        the reply happens before the next statement and an
        edit/followup after a reply is safe. `reply` here is the
        interaction reply; `edit` and `followup` go through the
        interaction; the channel kinds send to the channel;
        `timeout_edit` edits the message the components were
        attached to."""
        ctx = self._handler_ctx()
        responder = ctx.get("responder")
        if kind == "interaction":
            kind = "reply"
        if kind == "reply":
            if responder is None:
                self._fail("internal: reply without a responder")
            call = (f"await {responder}.respond("
                    f"{self._message_builder(arg, is_embed)}")
            if ephemeral:
                call += ", level: ResponseLevel.hint"
            return call + ")"
        if kind == "edit":
            if responder is None:
                self._fail("internal: edit without a responder")
            update = ("MessageUpdateBuilder(embeds: ["
                      f"{arg}])" if is_embed
                      else "MessageUpdateBuilder(content: "
                      f"{arg})")
            return (f"await {responder}.interaction"
                    f".updateOriginalResponse({update})")
        if kind == "followup":
            if responder is None:
                self._fail("internal: followup without a responder")
            call = (f"await {responder}.interaction.createFollowup("
                    f"{self._message_builder(arg, is_embed)}")
            if ephemeral:
                call += ", isEphemeral: true"
            return call + ")"
        if kind in ("system_channel", "message_channel"):
            if responder is None:
                self._fail("internal: channel reply without a "
                           "responder")
            return (f"await {responder}.sendMessage("
                    f"{self._message_builder(arg, is_embed)})")
        if kind == "timeout_edit":
            var = ctx.get("message_var")
            if var is None:
                self._fail("internal: timeout reply without a message")
            update = ("MessageUpdateBuilder(embeds: ["
                      f"{arg}])" if is_embed
                      else "MessageUpdateBuilder(content: "
                      f"{arg})")
            return f"await {var}.edit({update})"
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
        ctx = self._handler_ctx()
        if ctx["reply"] != "interaction":
            self._fail("`defer` needs an interaction (a command or "
                       "component handler)")
        responder = ctx.get("responder")
        if responder is None:
            self._fail("internal: defer without a responder")
        return [f"{p}await {responder}.acknowledge();"]

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
        lines.append(f"{p}await {channel}.sendMessage("
                     f"{self._message_builder(arg, is_embed)});")
        return lines

    # ------------------------------------------------------------ components

    def _component_var_name(self, label: str) -> str:
        words = "".join(ch if ch.isalnum() else " " for ch in label).split()
        return "_".join(w.lower() for w in words) or "component"

    def _author_id_expr(self, ctx: dict) -> str | None:
        """The invoking user's id in this handler, for the
        `only: author` gate - or None when the handler has no
        user in scope (a ready/task handler)."""
        kind = ctx.get("kind")
        if kind in ("command", "component"):
            responder = ctx.get("responder")
            if responder is None:
                return None
            return f"{responder}.user.id"
        if kind == "member_join":
            return "event.member.id"
        if kind == "member_remove":
            return "event.user.id"
        if kind == "message":
            return "event.message.author.id"
        return None

    def _roles_gate_lines(self, stmt, ctx_var: str,
                          p: str) -> list[str]:
        """The `only: roles` gate at the top of a component
        handler: the invoking member's roles are read from the
        role cache (falling back to a fetch per role - a
        PartialRole carries only its id), and the gate answers
        ephemerally and returns when none matches (the JS
        message, verbatim). `p` is the indent of the gate's
        lines."""
        roles = ", ".join(_dart_str(r)
                          for r in (stmt.only_roles or []))
        member = self._new_temp_name()
        role = self._new_temp_name()
        full = self._new_temp_name()
        has = self._new_temp_name()
        return [
            f"{p}var {has} = false;",
            f"{p}final {member} = {ctx_var}.member;",
            f"{p}if ({member} != null) {{",
            f"{p}  for (final {role} in {member}.roles) {{",
            f"{p}    final {full} = {role}.manager.cache[{role}.id] "
            f"?? await {role}.fetch();",
            f"{p}    if ([{roles}].contains({full}.name)) {{",
            f"{p}      {has} = true;",
            f"{p}      break;",
            f"{p}    }}",
            f"{p}  }}",
            f"{p}}}",
            f"{p}if (!{has}) {{",
            f"{p}  await {ctx_var}.respond(MessageBuilder("
            "content: 'You do not have a role that can use "
            "this.'), level: ResponseLevel.hint);",
            f"{p}  return;",
            f"{p}}}",
        ]

    def _emit_button(self, stmt, indent: int):
        p = "  " * indent
        if stmt.style == "link" or stmt.url is not None:
            self._fail("a `link` button cannot have a `do:` body "
                       "(Discord never sends an interaction for it)")
        style = stmt.style or "primary"
        dart_style = {"primary": "ButtonStyle.primary",
                      "secondary": "ButtonStyle.secondary",
                      "success": "ButtonStyle.success",
                      "danger": "ButtonStyle.danger"}.get(style)
        if dart_style is None:
            self._fail(f"unknown button style `{style}`")
        var = self._new_component_name()
        id_var = f"{var}_id"
        row = f"{var}_row"
        btn_var = f"{var}_button"
        custom = stmt.custom_id or self._component_var_name(stmt.label)
        timeout = stmt.timeout if stmt.timeout is not None else 180
        # The handler body is emitted now (it references the
        # component context variable), but it runs after the
        # answer - _attach_components inserts the await block.
        self._push_handler(
            "component", ["user", "guild", "channel", "interaction"],
            reply="interaction",
            locale=f"{var}.interaction.locale?.identifier",
            self_expr=var, responder=var)
        try:
            handler_lines = self._emit_block(stmt.body, indent + 1,
                                             new_scope=False)
        finally:
            self._pop_handler()
        on_timeout_lines = None
        if stmt.on_timeout is not None:
            self._push_handler("timeout", [], reply="timeout_edit",
                               locale=None, self_expr=None,
                               responder=None)
            self._handler_ctx_stack[-1]["message_var"] = \
                f"{var}_message"
            try:
                on_timeout_lines = self._emit_block(stmt.on_timeout,
                                                    indent + 1,
                                                    new_scope=False)
            finally:
                self._pop_handler()
        # The ComponentId carries the `only: author` gate and the
        # timeout; the setup lines run before the answer (moved
        # there by _emit_block).
        ctx = self._handler_ctx()
        author = self._author_id_expr(ctx) \
            if stmt.only_kind == "author" else None
        if stmt.only_kind == "author" and author is None:
            self._fail("a `button` / `select` with `only: author` "
                       "needs a handler with a user (this handler "
                       "has none)")
        setup = [f"{p}final {id_var} = ComponentId.generate(",
                 f"{p}  expirationTime: Duration(seconds: {timeout}),"]
        if author is not None:
            setup.append(f"{p}  allowedUser: {author},")
        setup.append(f"{p});")
        setup.append(f"{p}final {btn_var} = ButtonBuilder(")
        setup.append(f"{p}  style: {dart_style},")
        setup.append(f"{p}  label: {_dart_str(stmt.label)},")
        setup.append(f"{p}  customId: {id_var}.toString(),")
        setup.append(f"{p});")
        setup.append(f"{p}final {row} = ActionRowBuilder(components: "
                     f"[{btn_var}]);")
        self._pending_components.append({
            "kind": "button", "var": var, "id_var": id_var,
            "row": row, "btn_var": btn_var, "custom": custom,
            "only_kind": stmt.only_kind,
            "only_roles": stmt.only_roles,
            "timeout": timeout,
            "has_timeout": stmt.on_timeout is not None,
            "handler_lines": handler_lines,
            "on_timeout_lines": on_timeout_lines,
            "stmt": stmt,
        })
        return setup

    def _emit_select(self, stmt, indent: int):
        p = "  " * indent
        var = self._new_component_name()
        id_var = f"{var}_id"
        row = f"{var}_row"
        sel_var = f"{var}_select"
        custom = self._component_var_name(stmt.label)
        timeout = stmt.timeout if stmt.timeout is not None else 180
        min_v = stmt.min_values if stmt.min_values is not None else 1
        max_v = stmt.max_values if stmt.max_values is not None else 1
        options = ", ".join(
            f"SelectMenuOptionBuilder(label: {_dart_str(o)}, "
            f"value: {_dart_str(o)})"
            for o in stmt.options)
        self._push_handler(
            "component", ["user", "guild", "channel", "interaction"],
            reply="interaction",
            locale=f"{var}.interaction.locale?.identifier",
            self_expr=var, responder=var)
        try:
            self._handler_binding(
                "values", f"{var}.selected",
                f"{var}.selected.join(', ')",
                Type("list", (Type("str"),)))
            handler_lines = self._emit_block(stmt.body, indent + 1,
                                             new_scope=False)
        finally:
            self._pop_handler()
        on_timeout_lines = None
        if stmt.on_timeout is not None:
            self._push_handler("timeout", [], reply="timeout_edit",
                               locale=None, self_expr=None,
                               responder=None)
            self._handler_ctx_stack[-1]["message_var"] = \
                f"{var}_message"
            try:
                on_timeout_lines = self._emit_block(stmt.on_timeout,
                                                    indent + 1,
                                                    new_scope=False)
            finally:
                self._pop_handler()
        ctx = self._handler_ctx()
        author = self._author_id_expr(ctx) \
            if stmt.only_kind == "author" else None
        if stmt.only_kind == "author" and author is None:
            self._fail("a `button` / `select` with `only: author` "
                       "needs a handler with a user (this handler "
                       "has none)")
        setup = [f"{p}final {id_var} = ComponentId.generate(",
                 f"{p}  expirationTime: Duration(seconds: {timeout}),"]
        if author is not None:
            setup.append(f"{p}  allowedUser: {author},")
        setup.append(f"{p});")
        setup.append(f"{p}final {sel_var} = SelectMenuBuilder"
                     ".stringSelect(")
        setup.append(f"{p}  customId: {id_var}.toString(),")
        setup.append(f"{p}  placeholder: {_dart_str(stmt.label)},")
        setup.append(f"{p}  minValues: {min_v},")
        setup.append(f"{p}  maxValues: {max_v},")
        setup.append(f"{p}  options: [{options}],")
        setup.append(f"{p});")
        setup.append(f"{p}final {row} = ActionRowBuilder(components: "
                     f"[{sel_var}]);")
        self._pending_components.append({
            "kind": "select", "var": var, "id_var": id_var,
            "row": row, "btn_var": sel_var, "custom": custom,
            "only_kind": stmt.only_kind,
            "only_roles": stmt.only_roles,
            "timeout": timeout,
            "has_timeout": stmt.on_timeout is not None,
            "handler_lines": handler_lines,
            "on_timeout_lines": on_timeout_lines,
            "stmt": stmt,
        })
        return setup

    def _emit_modal(self, stmt, indent: int):
        p = "  " * indent
        var = self._new_component_name()
        custom = stmt.custom_id or self._component_var_name(stmt.title)
        timeout = stmt.timeout if stmt.timeout is not None else 180
        ctx = self._handler_ctx()
        responder = ctx.get("responder")
        if responder is None:
            self._fail("a `modal` needs an interaction to show it "
                       "on (a command or component handler)")
        lines = [f"{p}await {responder}.interaction.respondModal(",
                 f"{p}  ModalBuilder(",
                 f"{p}    customId: {_dart_str(custom)},",
                 f"{p}    title: {_dart_str(stmt.title)},",
                 f"{p}    components: ["]
        for field in stmt.fields:
            style = ("TextInputStyle.paragraph"
                     if field.style == "paragraph"
                     else "TextInputStyle.short")
            req = "true" if field.required else "false"
            mn = (f", minLength: {field.min_length}"
                  if field.min_length is not None else "")
            mx = (f", maxLength: {field.max_length}"
                  if field.max_length is not None else "")
            lines.append(f"{p}      ActionRowBuilder(components: [")
            lines.append(f"{p}        LabelComponentBuilder(")
            lines.append(f"{p}          label: {_dart_str(field.name)},")
            lines.append(f"{p}          component: TextInputBuilder(")
            lines.append(f"{p}            customId: "
                         f"{_dart_str(field.name)},")
            lines.append(f"{p}            style: {style},")
            lines.append(f"{p}            isRequired: {req}{mn}{mx},")
            lines.append(f"{p}          ),")
            lines.append(f"{p}        ),")
            lines.append(f"{p}      ]),")
        lines.append(f"{p}    ],")
        lines.append(f"{p}  ),")
        lines.append(f"{p});")
        # The modal's submit (or its timeout) runs inline, after
        # the modal is shown - a modal attaches to no message,
        # so there is nothing for _attach_components to do.
        self._push_handler(
            "component", ["user", "guild", "channel", "interaction"],
            reply="interaction",
            locale=f"{var}.interaction.locale?.identifier",
            self_expr=var, responder=var)
        try:
            for field in stmt.fields:
                getter = f"{var}[{_dart_str(field.name)}] ?? ''"
                self._handler_binding(field.name, getter, getter,
                                      Type("str"))
            handler_lines = self._emit_block(stmt.body, indent + 1,
                                             new_scope=False)
        finally:
            self._pop_handler()
        on_timeout_lines = None
        if stmt.on_timeout is not None:
            # A modal timeout has no interaction to answer on,
            # so the timeout body runs with no reply.
            self._push_handler("timeout", [], reply=None,
                               locale=None, self_expr=None,
                               responder=None)
            try:
                on_timeout_lines = self._emit_block(stmt.on_timeout,
                                                    indent + 1,
                                                    new_scope=False)
            finally:
                self._pop_handler()
        lines.append(f"{p}try {{")
        lines.append(f"{p}  final {var} = await {responder}"
                     f".awaitModal({_dart_str(custom)}, timeout: "
                     f"Duration(seconds: {timeout}));")
        lines.extend(handler_lines)
        lines.append(f"{p}}} on InteractionTimeoutException {{")
        if on_timeout_lines:
            lines.extend(on_timeout_lines)
        lines.append(f"{p}}}")
        return lines

    def _component_await_lines(self, c: dict, p: str,
                               responder: str) -> list[str]:
        """The try/await/handler/catch block for one pending
        component, inserted after the answer it attaches to.
        The handler body was emitted at indent+1 by _emit_button
        / _emit_select; the timeout disables the row first (the
        collector's `end` callback, nyxx-shaped) and then runs
        the on_timeout body."""
        var = c["var"]
        lines = [f"{p}try {{"]
        if c["kind"] == "button":
            lines.append(f"{p}  final {var} = await {responder}"
                         f".awaitButtonPress({c['id_var']});")
        else:
            lines.append(f"{p}  final {var} = await {responder}"
                         ".awaitSelection<List<String>>"
                         f"({c['id_var']});")
        if c["only_kind"] == "roles":
            lines.extend(self._roles_gate_lines(
                c["stmt"], var, p + "  "))
        lines.extend(c["handler_lines"])
        lines.append(f"{p}}} on InteractionTimeoutException {{")
        if c.get("has_timeout"):
            # Disable the row, then run the on_timeout body.
            lines.append(f"{p}  {c['btn_var']}.isDisabled = true;")
            lines.append(f"{p}  await {var}_message.edit("
                         "MessageUpdateBuilder(components: "
                         f"[{c['row']}]));")
            lines.extend(c["on_timeout_lines"] or [])
        lines.append(f"{p}}}")
        return lines

    def _attach_components(self, lines: list[str]):
        """Attach the pending component rows to the last answer
        line: insert `components: [...]` into its MessageBuilder
        (extending an existing components list when a second
        component attaches to the same answer), capture the
        message when a timeout needs it (shared across attaches
        to the same answer - later components alias it), and
        insert each component's await block after the answer."""
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
        indent = line[:len(line) - len(line.lstrip())]
        rows = ", ".join(c["row"] for c in pending)
        comp = line.find("components: [")
        if comp != -1:
            close = line.find("]", comp)
            lines[idx] = line[:close] + ", " + rows + line[close:]
            line = lines[idx]
        else:
            for marker in ("MessageBuilder(", "MessageUpdateBuilder("):
                pos = line.find(marker)
                if pos != -1:
                    at = pos + len(marker)
                    lines[idx] = (line[:at]
                                  + f"components: [{rows}], "
                                  + line[at:])
                    line = lines[idx]
                    break
            else:
                self._fail("internal: the answer line has no "
                           "MessageBuilder")
        # The message the components live on. The first attach
        # captures it; later attaches alias their own message
        # variable to the shared one.
        ctx = self._handler_ctx_stack[-1] if self._handler_ctx_stack \
            else {}
        needs_message = any(c.get("has_timeout") for c in pending)
        message_var = ctx.get("message_var")
        aliases: list[str] = []
        if needs_message and message_var is None:
            message_var = f"{pending[0]['var']}_message"
            ctx["message_var"] = message_var
            stripped = lines[idx]
            indent_len = len(stripped) - len(stripped.lstrip())
            lines[idx] = (stripped[:indent_len]
                          + f"final {message_var} = "
                          + stripped[indent_len:])
        if needs_message and message_var is not None:
            for c in pending:
                own = f"{c['var']}_message"
                if own != message_var:
                    aliases.append(f"{indent}final {own} = "
                                   f"{message_var};")
        # The await blocks, after the answer line (and any
        # aliases).
        responder = ctx.get("responder")
        if responder is None:
            self._fail("internal: component await without a "
                       "responder")
        await_lines: list[str] = []
        for c in pending:
            await_lines.extend(
                self._component_await_lines(c, indent, responder))
        lines[idx + 1:idx + 1] = aliases + await_lines

    def _handler_ctx(self) -> dict:
        if not self._handler_ctx_stack:
            self._fail("a bot statement was emitted outside a handler")
        return self._handler_ctx_stack[-1]

    def _push_handler(self, kind: str, names: list[str],
                      reply: str | None, locale: str | None,
                      self_expr: str | None = None,
                      responder: str | None = None):
        """Push a handler context and bind its names into a fresh
        scope. The value form is what a bare name means; the
        interpolation form is what `{name}` renders as. `{self}`
        in a binding is the handler's primary object (`context`
        for a command, the component context variable for a
        component continuation); `responder` is the expression
        a reply / defer / edit / followup answers on. The event
        parameter is always named `event` (member/message/ready)
        or `context` (command/component); bindings read through
        it, so no handler local can shadow a top-level name."""
        ctx = {"kind": kind, "reply": reply, "locale": locale,
               "responder": responder}
        self._handler_ctx_stack.append(ctx)
        self._fn_ctx.append({"return_type": None, "ensures": [],
                             "in_handler": True})
        self._push_scope()
        table = self._BOT_BINDINGS.get(kind, {})
        for name in names:
            value, interp, tname = table[name]
            if self_expr is not None:
                value = value.replace("{self}", self_expr)
                if interp is not None:
                    interp = interp.replace("{self}", self_expr)
            self._scopes[-1][name] = _Binding(
                dart=value, type=Type(tname), kind="handler",
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
            dart=value, type=type, kind="handler", interp=interp)



def _dart_stem(stem: str) -> str:
    """Sanitize a file stem into a valid Dart file stem (the
    output file is `<stem>.dart`; the CLI uses this for the output
    file name too). Invalid characters become `_`; a leading digit
    gets a `_` prefix; a Dart keyword gets a trailing underscore."""
    out = []
    for ch in stem:
        if ch.isalnum() or ch in ("_", "$"):
            out.append(ch)
        else:
            out.append("_")
    name = "".join(out) or "Main"
    if name[0].isdigit():
        name = "_" + name
    if name in _DART_KEYWORDS:
        name += "_"
    return name


def emit_program_dart(program: A.Program, *, main_path: str,
                      sources: dict[str, str],
                      line_comments: bool = False,
                      output_stem: str | None = None) -> EmitResult:
    """Emit a CHECKED program to Dart - one `.dart` per `.newr` file.

    `main_path` is the input file as passed (D2: the header names
    it); `sources` maps file paths to their text (for diagnostics
    and `--line-comments`). `output_stem` is the `-o` stem (the
    output file stem - sanitized by `_dart_stem`). The result's
    `files` maps an output basename (`hello.dart`) to its text;
    `main_file` is the main output's basename; `uses_rt` says
    whether any file needs `NewrRt` (the caller copies
    `compiler/newr_rt.dart` next to the output when it is true)."""
    stem = _dart_stem(output_stem or Path(main_path).stem)
    em = _DartEmitter(program, main_path=main_path, sources=sources,
                      line_comments=line_comments, output_stem=stem)
    files, uses_rt = em._emit_file_set(program, main_path)
    # `env.args` needs main()'s `args` in the runtime - but only
    # when some file actually uses it (D4: the runtime is copied
    # only when used, so hello stays runtime-free). A module file
    # may use env.args while the main file does not, and the main
    # file is emitted first, so the line is patched in here, after
    # the whole file set is known.
    main_file = stem + ".dart"
    if any("NewrRt.envArgs()" in text for text in files.values()):
        main_text = files[main_file]
        anchor = "void main(List<String> args) {\n"
        bot_anchor = "Future<void> main(List<String> args) async {\n"
        if "setMainArgs(" not in main_text:
            if anchor in main_text:
                files[main_file] = main_text.replace(
                    anchor,
                    anchor + f"  NewrRt.setMainArgs(\"{stem}\", args);\n",
                    1)
            elif bot_anchor in main_text:
                files[main_file] = main_text.replace(
                    bot_anchor,
                    bot_anchor
                    + f"  NewrRt.setMainArgs(\"{stem}\", args);\n",
                    1)
    if program.bot is not None:
        # The standalone registration script (J10's Dart twin)
        # rides along as a second output file, and the pubspec
        # (nyxx always, sqlite3 only when a store is used - the CLI
        # writes every file in result.files, so the pubspec lands
        # next to the sources automatically).
        deploy = em._emit_deploy_file(program, stem + ".dart")
        files[stem + "_deploy.dart"] = deploy
        files["pubspec.yaml"] = _dart_pubspec(stem,
                                              uses_sqlite=em.uses_sqlite)
    elif em.uses_sqlite:
        # A plain `db.*` program (no bot) still needs a pubspec:
        # sqlite3 only, no nyxx (the program has no bot to run).
        files["pubspec.yaml"] = _dart_db_pubspec(stem)
    return EmitResult(files=files,
                      main_file=stem + ".dart",
                      uses_rt=uses_rt)


def _dart_pubspec(stem: str, *, uses_sqlite: bool) -> str:
    """The pubspec for a bot program: nyxx 6.9.1 +
    nyxx_commands 6.1.0, plus sqlite3 3.9.0 when a store is used
    (all three versions verified live on pub.dev - see
    tests/test_emitter_dart.py). Dart SDK ^3.0.0."""
    name = "".join(ch.lower() if ch.isalnum() else "_"
                   for ch in stem).strip("_") or "bot"
    lines = [
        "# Generated by newr - do not edit by hand.",
        f"name: {name}",
        "description: A newr bot.",
        "version: 1.0.0",
        "",
        "environment:",
        "  sdk: ^3.0.0",
        "",
        "dependencies:",
        "  nyxx: 6.9.1",
        "  nyxx_commands: 6.1.0",
    ]
    if uses_sqlite:
        lines.append("  sqlite3: 3.9.0")
    return "\n".join(lines) + "\n"


def _dart_db_pubspec(stem: str) -> str:
    """The pubspec for a plain `db.*` program (no bot): sqlite3
    3.9.0 only, no nyxx (the program has no bot to run). Dart
    SDK ^3.0.0."""
    name = "".join(ch.lower() if ch.isalnum() else "_"
                   for ch in stem).strip("_") or "db_app"
    lines = [
        "# Generated by newr - do not edit by hand.",
        f"name: {name}",
        "description: A newr program using the db module.",
        "version: 1.0.0",
        "",
        "environment:",
        "  sdk: ^3.0.0",
        "",
        "dependencies:",
        "  sqlite3: 3.9.0",
    ]
    return "\n".join(lines) + "\n"



# `emit_program` is the name the tests and the CLI use; the
# `_dart` suffix marks the implementation (the Python emitter's
# entry is `emit_program` in compiler/emitter.py).
emit_program = emit_program_dart
