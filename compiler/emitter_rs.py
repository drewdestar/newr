"""The Rust emitter (M5, SPEC.md §18).

``newr build --target rs`` lowers a *checked* program (the front
end has already run: parse -> resolve -> check) to readable Rust -
one `.newr` file -> one `.rs` file, structure preserved, user names
verbatim, helpers only in `newr_rt.rs` (copied next to the output
when used - the emitted code does `mod newr_rt;` and calls
`newr_rt::add(...)`, R1).

Readability is a requirement, not a nicety: the output is code a
human would write - the Rust goldens (`goldens/rs/`) are the style
oracle, and the emitter reproduces them byte-for-byte
(BUILD_REPORT.md §21, R1–Rn).

The emitter NEVER runs on an unchecked program: the CLI checks first
and exits 4 on errors. If it meets a node without a type (which only
happens on an unchecked program), it raises `EmitterError` - a clean
refusal, never a TypeError traceback.

Module system (R2): one Rust module per file, named after the file
stem verbatim. File modules become their own `.rs` files; the main
file declares `mod <stem>;` for each, and imported names are
qualified `<stem>::name`. A program with no `main` file stem gets
`Main`. Plain programs compile with plain `rustc` (the runtime is
std-only); bot/store programs get a `Cargo.toml` (poise + serenity
+ rusqlite, pins in SPEC.md §14) and build with cargo.

Ownership (R3): Rust has no GC - values are owned. The emitter
clones lvalue RHSs (`let`/`const` initializers, assignment RHS,
call/struct/list/dict args, `??` defaults) only when the node is a
`Name`/`Member`/`Index` (owned temporaries move fine). Lists/sets
are NOT aliased through calls (a clone is made) - recorded as a
known divergence (no conformance coverage).

Errors (R4): newr errors are panics carrying a `NewrError`
payload (`newr_rt::throw`); `try`/`catch` use `catch_unwind` +
`as_error`. A `try` body containing `return`/`break`/`continue`
is E4001 (none in conformance; the panic would unwind past the
`catch_unwind` boundary otherwise).

Encoding: every `open()` / `read_text()` in this file passes
`encoding="utf-8"` (CI enforces it).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import re

from . import ast_nodes as A
from .ast_nodes import Type, fn_of
from .emitter import EmitterError, EmitResult
from .errors import Span
from .modules import BUILTINS, METHODS, MODULES

_BUILTIN_NAMES = frozenset(BUILTINS)

# Names the emitter itself defines in an emitted file. A user name
# colliding with one of these is mangled with a trailing underscore
# (D20, mirrored from the Python/JS/Java emitters). The `newr_rt`
# runtime helpers are called qualified (`newr_rt::add`), so user
# names that match a helper are harmless - but the module name
# `newr_rt` itself, the `main` function, its `args` parameter, and
# the std type names the emitter writes (`String`, `Vec`, ...)
# must not be shadowed by a user struct/enum/fn name in the same
# file.
_RT_HELPERS = frozenset({
    "add", "sub", "mul", "neg", "pow_int", "pow_float", "tdiv", "tmod",
    "fmod", "fdiv", "round_half_away", "round_half_away_n",
    "round_half_away_int", "round_half_away_int_n", "to_int_from_float",
    "to_int_from_str", "to_int_from_bool", "to_float_from_str",
    "to_bool_from_str", "to_bool_from_int", "to_bool_from_float",
    "to_str", "float_str", "check_int", "as_error", "throw",
    "list_index", "list_get", "list_contains", "set_remove",
    "unwrap_opt", "try_result", "result_unwrap", "result_unwrap_or",
    "time_format", "time_now", "file_read", "file_write", "file_append",
    "file_exists", "random_int", "random_choice", "random_shuffle",
    "chars", "split_str", "str_index", "abs_long", "str_len",
    "range", "range_from", "range_step", "dict_of", "env_get",
    "env_get_or", "env_args", "gget", "gset", "print", "input",
    "coded_error", "struct_hash_u64", "newr_hash_u64",
})
_RT_CLASSES = frozenset({
    "NewrError", "Dict", "Set", "ToNewrStr", "NewrHash",
})
_RS_LANG_TYPES = frozenset({
    "String", "Vec", "Option", "Some", "None", "Ok", "Err", "Result",
    "i64", "i32", "f64", "bool", "str", "char", "u64", "usize",
    "Mutex", "Arc", "HashMap", "HashSet", "Box", "Send", "Sync",
    "Clone", "PartialEq", "Eq", "Hash", "Default", "Display",
})
_EMITTER_RESERVED = frozenset({
    "main", "args", "newr_rt", "Bot", "Store", "store", "economy",
    "commands", "modalHandlers", "bot", "Data", "Error", "Context",
    # Bot-internal names: handler parameters (the event is always
    # named `event` / `interaction` / `member` / `message` /
    # `ready_client`), the COMMANDS/STRINGS tables, the scheduler,
    # the component maps, and the startup locals. A user name that
    # collides is mangled (D20), so a top-level let can never be
    # shadowed by a handler parameter.
    "event", "interaction", "member", "message", "ready_client",
    "COMMANDS", "STRINGS", "_DEFAULT_LOCALE", "_t", "_channel_text",
    "_newr_scheduler", "_newr_token", "_newr_builder",
    "_newr_system_channel", "_newr_author_id",
    "_component_authors", "button_handlers", "select_handlers",
    "modal_handlers",
}) | _RT_CLASSES | _RS_LANG_TYPES

# Rust reserved words (2021 edition) + literals + the 2024 `gen`
# keyword, which the emitter avoids even though edition 2021
# would accept it as an identifier.
_RS_KEYWORDS = frozenset({
    "as", "break", "const", "continue", "crate", "else", "enum",
    "extern", "false", "fn", "for", "if", "impl", "in", "let",
    "loop", "match", "mod", "move", "mut", "pub", "ref", "return",
    "self", "Self", "static", "struct", "super", "trait", "true",
    "type", "unsafe", "use", "where", "while", "async", "await",
    "dyn", "abstract", "become", "box", "do", "final", "macro",
    "override", "priv", "typeof", "unsized", "virtual", "yield",
    "try", "gen",
})


def _rs_str(value: str) -> str:
    """Render a newr string value as a Rust string literal.

    Always double quotes. Non-ASCII printable characters are
    emitted literally (UTF-8 source); control characters use their
    escapes. `\\u{...}` covers the rest.
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
        elif o < 0x20 or o == 0x7F:
            out.append(f"\\u{{{o:x}}}")
        else:
            out.append(ch)
    return '"' + "".join(out) + '"'


def _rs_float(value: float) -> str:
    """Render a float literal. Python repr is also a valid Rust
    float literal ("1e+16", "5e-324", "1.5"). The non-finite
    spellings are not literals in Rust, so they become the f64
    constants (an out-of-range literal parses to inf in Python
    and would otherwise emit `inf`, a compile error)."""
    if value != value:  # NaN
        return "f64::NAN"
    if value == float("inf"):
        return "f64::INFINITY"
    if value == float("-inf"):
        return "f64::NEG_INFINITY"
    return repr(value)


# Rust precedence (higher binds tighter), loosest -> tightest.
# The emitter's precedence-climbing printer uses this table; newr's
# own table (SPEC §9.2) maps onto it, with the traps documented
# where they bite - notably unary `-`: `-x ** y` parses as
# `-(x ** y)` in newr, and Rust has no `**`, so the power helper
# call binds as a primary; and `!a == b` is `(!a) == b` in Rust,
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


def _is_entity(t: Type | None) -> bool:
    return t is not None and t.name in (
        "user", "member", "guild", "channel", "role",
        "interaction", "embed", "message")


# ---------------------------------------------------------------- scopes


@dataclass
class _Binding:
    """One visible name: its Rust rendering and its newr type.

    `rs` is the Rust name (possibly renamed); `interp` is the form
    used inside an interpolation (for handler bindings: the mention /
    name form); `value` is the form used as a value. For ordinary
    variables all three are the Rust name. `kind` is one of
    "param" / "local" / "global-let" / "fn" / "struct" / "enum" /
    "module" / "store" / "handler" / "builtin" / "named-module" /
    "file-module". `module` is the real module name for a module
    binding (the `rs` name is the alias when the import is
    aliased).
    """

    rs: str
    type: Type | None = None
    kind: str = "local"
    interp: str | None = None
    module: str | None = None
    # Handler bindings only (M5): the Rust expression for the
    # guild id of the value (e.g. `interaction.guild_id` for an
    # interaction, `new_member.guild_id` for a member) and for the
    # channel id (`interaction.channel_id`). `_emit_member` uses
    # them for `member.guild`, `interaction.guild` /
    # `interaction.channel`, which have no serenity field to read
    # directly - they are resolved through the cache.
    guild_id_expr: str | None = None
    # True when `guild_id_expr` is an `Option<GuildId>` (an
    # interaction's / a message's guild id); a plain `GuildId`
    # (a member's guild id) is wrapped in `Some(...)` first.
    guild_id_optional: bool = False
    channel_id_expr: str | None = None

    @property
    def value(self) -> str:
        return self.rs

    def interp_form(self) -> str:
        return self.interp if self.interp is not None else self.rs


class _RsEmitter:
    """State for one emitted file set (main file + its imports)."""

    def __init__(self, program: A.Program, *, main_path: str,
                 sources: dict[str, str], line_comments: bool = False,
                 output_stem: str | None = None):
        self.program = program
        self.main_path = main_path
        self.sources = sources
        self.line_comments = line_comments
        # The file stem of the main output file. The CLI passes the
        # `-o` stem; the default is the main source stem, falling
        # back to "Main".
        self.output_stem = output_stem or "Main"
        # Runtime usage collected while emitting (per output file -
        # reset for each module file).
        self.uses_discord: bool = False
        self.discord_names: set[str] = set()
        self.uses_sqlite: bool = False
        # This file uses `db.*` (set by `_emit_db_call`) — the
        # `newr_db` helper module is emitted into the file only then.
        self._file_uses_db: bool = False
        self.rt_helpers: set[str] = set()
        # Module-level declarations of the file being emitted.
        self.fns: dict[str, A.FnDecl] = {}
        self.structs: dict[str, A.StructDecl] = {}
        self.enums: dict[str, A.EnumDecl] = {}
        self.stores: dict[str, A.StoreDecl] = {}
        self.module_lets: dict[str, Type | None] = {}
        self.imported_modules: dict[str, str] = {}  # bound name -> stem
        self.fns_by_rs: dict[str, A.FnDecl] = {}
        # Scope stack: list of dicts (newr name -> _Binding). The
        # bottom scope is the module scope of the file being emitted.
        self._scopes: list[dict[str, _Binding]] = []
        # Rust names already taken at module level (for collision
        # aware allocation in _bind_module_level).
        self._module_used: set[str] = set()
        # Rust names already used in the current Rust function scope
        # (for rename allocation in _bind_fresh). Reset per function.
        self._fn_used: set[str] = set()
        # Hoists for the statement being emitted: each is a list of
        # lines (base indent 0) to emit before the statement -
        # hoisted lambda consts and lambda snapshot locals.
        self._hoists: list[list[str]] | None = None
        # True while emitting the DIRECT top-level statement list of
        # a file (main() / the module __newr_init()). A `let` there
        # assigns the static instead of declaring a local (R2) - a
        # local would shadow the static and top-level functions
        # would read the still-empty static (conformance
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
        # Whether the `_channel_text` helper is needed (set when a
        # `{channel}` interpolation is emitted).
        self._used_channel_text = False
        # Whether a `.mention()` call was emitted (set when a
        # user/member/channel mention form is used - the
        # `Mentionable` trait import is only emitted then, so a
        # bot that never mentions anyone gets no unused-import
        # warning). `_channel_text` also mentions, so the import
        # is emitted when either flag is set.
        self._used_mention = False
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
        # The last answer's details (set by _emit_reply /
        # _emit_followup / _emit_edit / _emit_send alongside
        # _last_answer, saved/restored with it per block): the
        # builder line before any `.components(...)` rewrite
        # ("builder_base"), the message variable name ("msgvar"),
        # whether it has been captured ("captured"), the rows
        # attached so far ("rows"), the answer kind ("kind"), the
        # http expression ("http") and the interaction expression
        # ("interaction", interaction answers only).
        self._last_answer_info: dict | None = None
        # Counter for unique temp names within a file.
        self._temp_counter = 0
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
        # Whether the emitted file needs the scheduler (tasks and/or
        # component timeouts) or the per-message author map (any
        # `only: author` component).
        self._uses_scheduler = False
        self._uses_author_map = False
        # Synthesized row structs, one per store table (name ->
        # StructDecl), emitted as structs by _emit_bot_file.
        self._table_rows: dict[str, A.StructDecl] = {}
        # Whether the file being emitted is the entry file (set by
        # _emit_file_set before _emit_one).
        self._is_main_file = True

    # ------------------------------------------------------------ errors

    def _fail(self, msg: str, span=None):
        raise EmitterError(msg, span=span)

    def _type_of(self, expr: A.Expr) -> Type:
        t = self._type_or_none(expr)
        if t is None:
            self._fail("an expression has no type: the emitter cannot "
                       "handle it here", span=getattr(expr, "span", None))
        return t

    def _type_or_none(self, expr: A.Expr) -> Type | None:
        return getattr(expr, "expr_type", None)

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
        """The Rust name for a newr name, before collision checks:
        Rust keywords and emitter-reserved names get a trailing
        underscore (D20, Rust twin)."""
        if name in _RS_KEYWORDS or name in _EMITTER_RESERVED:
            return name + "_"
        return name

    def _bind(self, name: str, type: Type | None = None,
              kind: str = "local", interp: str | None = None) -> _Binding:
        """Bind `name` in the current scope.

        Rust has real block scope (`let x` in an `if` block does
        not clobber the outer name), so - unlike the Python emitter
        - shadowing an enclosing LOCAL needs no renaming: the inner
        binding is a different variable, exactly as newr reads it.
        But a module-level name is a `static`, and a `let` binding
        may not shadow a static (E0530) - so a local whose mangled
        name collides with one gets a trailing underscore (the
        `fn_shadow` case). Only keyword / emitter-reserved mangling
        applies otherwise (D20).
        """
        rs = self._mangle(name)
        while rs in self._module_used:
            rs += "_"
        self._fn_used.add(rs)
        binding = _Binding(rs=rs, type=type, kind=kind, interp=interp)
        self._scopes[-1][name] = binding
        return binding

    def _bind_fresh(self, name: str, type: Type | None = None,
                    kind: str = "local") -> _Binding:
        """Bind `name` in the current scope WITHOUT renaming against
        enclosing scopes - for function/lambda parameters, where
        shadowing an enclosing name is the correct semantics. Only
        the current function's used-name set is consulted, so two
        parameters mangling to the same Rust name still get distinct
        names (duplicate parameters are a compile error)."""
        base = self._mangle(name)
        rs = base
        n = 1
        while rs in self._fn_used:
            rs = f"{base}_{n}"
            n += 1
        self._fn_used.add(rs)
        binding = _Binding(rs=rs, type=type, kind=kind)
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
        rs = self._mangle(name)
        if rs in self._module_used:
            n = 1
            while f"{rs}_{n}" in self._module_used:
                n += 1
            rs = f"{rs}_{n}"
        self._module_used.add(rs)
        binding = _Binding(rs=rs, type=type, kind=kind)
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

    def _rs_type(self, t: Type | None) -> str:
        """The Rust type for a newr type in a declaration position
        (a local, parameter, field or return type). Primitives stay
        primitive; nullable types are `Option<...>`; a `none` value
        is `()` (a `none` return is written as no return type at
        all, in _emit_fn)."""
        if t is None:
            self._fail("an expression has no type: the emitter cannot "
                       "handle it here")
        if t.nullable:
            inner = Type(t.name, t.params)
            return f"Option<{self._rs_type(inner)}>"
        if t.name == "int":
            return "i64"
        if t.name == "float":
            return "f64"
        if t.name == "bool":
            return "bool"
        if t.name == "str":
            return "String"
        if t.name == "none":
            return "()"
        if t.name == "object":
            # The union type of conflicting enum variant fields.
            # Rust has no dynamic `Object` - a clean E4001 (R5).
            self._fail(
                "an enum variant field with conflicting types "
                "across variants is not supported in the Rust "
                "emitter")
        if t.name == "error":
            self._need_rt("NewrError")
            return "newr_rt::NewrError"
        if t.name == "list":
            return f"Vec<{self._rs_boxed_type(t.params[0])}>"
        if t.name == "dict":
            self._need_rt("Dict")
            return ("newr_rt::Dict<"
                    f"{self._rs_boxed_type(t.params[1])}>")
        if t.name == "set":
            self._need_rt("Set")
            return f"newr_rt::Set<{self._rs_boxed_type(t.params[0])}>"
        if t.name == "Result":
            return ("Result<"
                    f"{self._rs_boxed_type(t.params[0])}, "
                    f"{self._rs_boxed_type(t.params[1])}>")
        if t.name == "fn":
            return self._rs_fn_type(t)
        if t.name == "duration":
            return "i64"
        if t.name in ("user", "member", "guild", "channel", "role",
                      "interaction", "embed", "message"):
            return self._rs_entity_type(t.name)
        # A struct or enum: the (possibly module-qualified) type.
        return self._struct_rs_name(t.name)

    def _rs_boxed_type(self, t: Type | None) -> str:
        """The type-argument form. Rust has no primitive/boxed
        distinction, so this is `_rs_type`."""
        return self._rs_type(t)

    def _rs_fn_type(self, t: Type) -> str:
        """`fn(A, B) -> R` -> `Arc<dyn Fn(A, B) -> R + Send + Sync>`.
        No arity limit (unlike Java's Fn0–Fn8): the dyn trait
        carries any signature (R6)."""
        params = t.params[:-1]
        ret = t.params[-1]
        args = ", ".join(self._rs_boxed_type(p) for p in params)
        ret_s = self._rs_boxed_type(ret)
        return (f"std::sync::Arc<dyn Fn({args}) -> {ret_s} "
                "+ Send + Sync>")

    def _rs_entity_type(self, name: str) -> str:
        """A Discord entity type (SPEC §7.1) -> its serenity type.
        Only reachable in bot files (R7)."""
        mapping = {
            "user": "serenity::model::user::User",
            "member": "serenity::model::guild::Member",
            "guild": "serenity::model::guild::Guild",
            "channel": "serenity::model::id::ChannelId",
            "role": "serenity::model::guild::Role",
            "interaction": "serenity::model::application::CommandInteraction",
            "embed": "serenity::builder::CreateEmbed",
            "message": "serenity::model::channel::Message",
        }
        return mapping[name]

    def _struct_rs_name(self, name: str) -> str:
        """The Rust type name for a struct/enum type: bare in its
        own file, `stem::Name` when imported (the binding records
        the module). An imported struct/enum reached only through
        its TYPE (a `let c = helper.Shade.Red` has type `Shade`
        with no module on the Type - the Type class has no module
        field) is found by searching the imported file modules for
        the declaration and qualifying with the module's stem
        (conformance import_enum)."""
        binding = self._lookup(name)
        if binding is not None and binding.kind in ("struct", "enum"):
            if binding.module:
                return f"{binding.module}::{binding.rs}"
            return binding.rs
        for bound, sub in self.program.modules.items():
            for decl in list(sub.structs) + list(sub.enums):
                if decl.name == name:
                    stem = self.imported_modules.get(bound, bound)
                    return f"{stem}::{self._mangle(name)}"
        return self._mangle(name)

    def _rs_default(self, t: Type) -> str:
        """The default value for a field of type `t` (an enum
        variant that does not declare the field, the `ensures`
        fall-through, a struct's `Default` impl)."""
        if _is_int(t):
            return "0"
        if _is_float(t):
            return "0.0"
        if _is_bool(t):
            return "false"
        if _is_str(t):
            return "String::new()"
        if _is_none(t):
            return "()"
        if _is_error(t):
            return 'newr_rt::NewrError::new("")'
        if _is_nullable(t):
            return "None"
        if _is_list(t):
            return "Vec::new()"
        if _is_dict(t):
            return "newr_rt::Dict::new()"
        if _is_set(t):
            return "newr_rt::Set::new()"
        if _is_result(t):
            return 'Err(newr_rt::NewrError::new(""))'
        if _is_fn(t):
            params = t.params[:-1]
            ret = t.params[-1]
            args = ", ".join(f"_: {self._rs_type(p)}" for p in params)
            return (f"std::sync::Arc::new(|{args}| "
                    f"{self._rs_default(ret)})")
        if t.name == "duration":
            return "0"
        if _is_struct_or_enum(t):
            return f"{self._struct_rs_name(t.name)}::default()"
        self._fail(f"no default value for type `{t}`")

    # ------------------------------------------------------------ extracted (language-agnostic, mirrored from emitter_java.py)

    def _header(self, main_path: str, description: str | None = None) -> list[str]:
            lines = [
                f"// Generated by newr from {main_path} - do not edit by hand.",
                f"// Edit the .newr file and rebuild with: newr build {main_path}",
            ]
            if description:
                lines.append(f"// {description}")
            return lines

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
            saved_answer_info = self._last_answer_info
            self._last_answer_info = None
            saved_top = self._in_top_level
            self._in_top_level = top_level
            lines: list[str] = []
            try:
                for st in stmts:
                    before = len(lines)
                    lines.extend(self._emit_stmt_with_hoists(st, indent))
                    if isinstance(st, (A.Reply, A.Followup, A.Edit,
                                       A.Send)):
                        # A `send` is an answer too: in a message
                        # event a button may attach to the sent
                        # message, exactly like a reply.
                        kind = ("reply" if isinstance(st, A.Reply)
                                else "followup" if isinstance(st, A.Followup)
                                else "edit" if isinstance(st, A.Edit)
                                else "send")
                        self._last_answer = (len(lines) - 1, kind)
                    elif isinstance(st, (A.ButtonDecl, A.SelectDecl)):
                        # The component's instantiation lines must come
                        # BEFORE the answer they attach to - the answer
                        # references the components variable, so defining
                        # it after the answer would be a compile error.
                        # They go before the answer's BUILDER line
                        # (the send line is `builder_rel` lines after
                        # it), not before the send line: inserting
                        # before the send line would leave the builder
                        # referencing a variable defined after it.
                        if self._last_answer is not None:
                            idx, kind = self._last_answer
                            info = self._last_answer_info
                            rel = (info.get("builder_rel", 1)
                                   if info else 1)
                            insert_at = idx - rel
                            added = lines[before:]
                            del lines[before:]
                            lines[insert_at:insert_at] = added
                            self._last_answer = (idx + len(added), kind)
                        self._attach_components(lines)
            finally:
                if new_scope:
                    self._pop_scope()
                self._last_answer = saved_answer
                self._last_answer_info = saved_answer_info
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

    def _emit_kwargs(self, node: A.Call, binding: _Binding) -> list[str]:
            """Keyword arguments for a user-fn call: Rust has no kwargs,
            so they are reordered into position (the checker already
            validated names and types)."""
            if not node.kwargs:
                return []
            fn = self.fns_by_rs.get(binding.rs)
            if fn is None:
                self._fail(
                    "internal error: cannot find fn "
                    f"`{binding.rs}` for keyword arguments")
            order = [p.name for p in fn.params]
            ptypes = [p.type for p in fn.params]
            values: list[str] = [""] * len(order)
            for kname, value, _span in node.kwargs:
                idx = order.index(kname)
                p = ptypes[idx]
                values[idx] = self._emit_value(
                    value, p, fn_target=p if _is_fn(p) else None)
            if any(v == "" for v in values):
                self._fail(
                    "internal error: missing keyword argument in call")
            return values

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
            ptypes = [p.type for p in fn.params]
            values: list[str] = [""] * len(order)
            for kname, value, _span in node.kwargs:
                idx = order.index(kname)
                p = ptypes[idx]
                values[idx] = self._emit_value(
                    value, p, fn_target=p if _is_fn(p) else None)
            if any(v == "" for v in values):
                self._fail(
                    "internal error: missing keyword argument in call")
            return values

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
                    stem = _rs_stem(Path(imp.resolved_path or imp.path).stem)
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
            # The rs-name -> fn-decl map (for keyword-argument reordering
            # in _emit_kwargs).
            self.fns_by_rs = {}
            for decl in program.functions:
                b = self._lookup(decl.name)
                if b is not None:
                    self.fns_by_rs[b.rs] = decl
            # The top level's used-name set starts with every module-level
            # Rust name, so a `let` in a top-level block cannot reuse one
            # (at module level Rust has no block scope for the top level -
            # the name would clobber the module binding).
            self._fn_used = set(self._module_used)

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
            files[self.output_stem + ".rs"] = text
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
                # shop_items.rs header).
                import os
                sub_path = os.path.relpath(sub_path)
                sub_em = _RsEmitter(sub, main_path=sub_path,
                                      sources=self.sources,
                                      line_comments=self.line_comments,
                                      output_stem=_rs_stem(Path(sub_path).stem))
                sub_files, sub_rt = sub_em._emit_file_set(sub, sub_path,
                                                          is_main=False)
                files.update(sub_files)
                uses_rt = uses_rt or sub_rt
                # A `db.*` call in a file module needs rusqlite in
                # the Cargo.toml too (the gate reads only the main
                # emitter — see emit_program_rs).
                self.uses_sqlite = self.uses_sqlite or sub_em.uses_sqlite
            return files, uses_rt

    def _emit_one(self, program: A.Program, main_path: str,
                      is_main: bool = True):
            """Emit a single program's file. Returns (text, uses_rt)."""
            self.program = program
            if program.bot is not None:
                return self._emit_bot_file(program, main_path)
            return self._emit_general_file(program, main_path)

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

    # ------------------------------------------------------------ ownership helpers (R3)

    def _is_copy_type(self, t: Type | None) -> bool:
        """True when a value of this type is `Copy` (a move is a
        copy, so no `.clone()` is needed when it is consumed)."""
        if t is None:
            return False
        if t.name in ("int", "float", "bool", "none", "duration"):
            return True
        if t.nullable:
            return self._is_copy_type(Type(t.name, t.params))
        if t.name == "Result":
            return (self._is_copy_type(t.params[0])
                    and self._is_copy_type(t.params[1]))
        return False

    def _owned(self, expr: A.Expr,
               fn_target: Type | None = None) -> str:
        """Emit an expression that is CONSUMED by value (a let/assign
        RHS, a call/struct/list/dict argument, a return value): a
        `Name`/`Member` of a non-`Copy` type is cloned (a move would
        steal it from later uses); an `Index` is already a fresh
        clone (the runtime helpers clone); every other node is a
        fresh owned value and moves fine. A global-let `Name` reads
        through `gget`, which already clones."""
        text = self.emit_expr(expr, 0, fn_target=fn_target)
        if isinstance(expr, A.Name):
            binding = self._lookup(expr.name)
            if binding is not None and binding.kind == "global-let":
                return text
            if not self._is_copy_type(self._type_or_none(expr)):
                return f"{text}.clone()"
            return text
        if isinstance(expr, A.Member):
            if not self._is_copy_type(self._type_or_none(expr)):
                return f"{text}.clone()"
            return text
        return text

    def _wrap_nullable(self, text: str, actual: Type | None,
                       expected: Type | None) -> str:
        """Widen a value to a nullable expected type (R4b): a `T`
        value becomes `Some(...)`, a `none` literal becomes
        `None::<T>`. Any other combination passes through."""
        if expected is None or actual is None:
            return text
        if not expected.nullable:
            return text
        inner = Type(expected.name, expected.params)
        if _is_none(actual):
            return f"None::<{self._rs_type(inner)}>"
        if not actual.nullable and actual == inner:
            return f"Some({text})"
        return text

    def _emit_value(self, expr: A.Expr,
                    expected: Type | None = None,
                    fn_target: Type | None = None) -> str:
        """Emit a value in a typed position: owned (cloned when
        needed) and widened to `expected` when it is nullable."""
        if expected is not None and expected.nullable \
                and isinstance(expr, A.Call) \
                and isinstance(expr.func, A.Name) \
                and expr.func.name in ("ok", "err"):
            # `ok`/`err` construct a bare `Ok`/`Err` - never an
            # `Option` - but the checker unifies their type with
            # the expected type, so `_wrap_nullable` below would
            # see a nullable actual type and skip the `Some` wrap.
            text = self._owned(expr, fn_target=fn_target)
            return f"Some({text})"
        text = self._owned(expr, fn_target=fn_target)
        return self._wrap_nullable(text, self._type_or_none(expr),
                                   expected)

    def _rs_fn_ptr_type(self, t: Type) -> str:
        """`fn(A, B) -> R` as a Rust function-pointer type (for the
        `as` cast when a module fn is used as a value: two fn items
        have distinct types, but fn pointers unify)."""
        params = t.params[:-1]
        ret = t.params[-1]
        args = ", ".join(self._rs_boxed_type(p) for p in params)
        return f"fn({args}) -> {self._rs_boxed_type(ret)}"

    # ------------------------------------------------------------ expressions

    def emit_expr(self, node: A.Expr, min_prec: int = 0,
                  fn_target: Type | None = None) -> str:
        """Emit an expression, parenthesised when its precedence is
        below `min_prec`. `fn_target` is the expected fn type when
        the expression is a lambda - the context's target type, so
        the lambda needs no cast (a Rust closure is not a
        `dyn Fn` until it is wrapped in an `Arc`)."""
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
                return (f"Vec::<{self._rs_boxed_type(elem)}>::new()",
                        _PREC["primary"])
            elem = self._type_of(node).params[0]
            parts = ", ".join(
                self._emit_value(i, elem,
                                 fn_target=elem if _is_fn(elem) else None)
                for i in node.elements)
            # `vec!` with an explicit element type is unnecessary:
            # the context (a typed let / a typed parameter / a
            # return) pins the type, and fn elements are already
            # cast to the fn type by _emit_lambda.
            return f"vec![{parts}]", _PREC["primary"]
        if isinstance(node, A.DictLit):
            if not node.pairs:
                val = self._type_of(node).params[1]
                return ("newr_rt::Dict::<"
                        f"{self._rs_boxed_type(val)}>::new()",
                        _PREC["primary"])
            val = self._type_of(node).params[1]
            parts = []
            for k, v in node.pairs:
                parts.append(
                    f"({self._emit_value(k, Type('str'))}, "
                    f"{self._emit_value(v, val, fn_target=val if _is_fn(val) else None)})")
            # `dict_of` takes the pairs as a Vec (insertion order
            # is kept by the runtime's Dict).
            self._need_rt("dict_of")
            return (f"newr_rt::dict_of(vec![{', '.join(parts)}])",
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
            then = self._emit_value(node.then_expr, t, fn_target=tgt)
            els = self._emit_value(node.else_expr, t, fn_target=tgt)
            return (f"if {cond} {{ {then} }} else {{ {els} }}",
                    _PREC["ternary"])
        if isinstance(node, A.Propagate):
            # `?` is statement-level (D7): the parser only creates it
            # as the outermost postfix of a statement RHS, so it is
            # handled by the statement emitter, never here.
            self._fail("`?` may only appear at the end of a statement "
                       "right-hand side (the emitter found it nested "
                       "inside an expression)")
        if isinstance(node, A.TryExpr):
            self._need_rt("try_result")
            inner_t = self._type_of(node).params[0]
            inner = self.emit_expr(
                node.expr, 0,
                fn_target=inner_t if _is_fn(inner_t) else None)
            return f"newr_rt::try_result(|| {inner})", _PREC["primary"]
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
            return "()", _PREC["primary"]
        if isinstance(value, int):
            # i64::MIN has no positive literal in Rust
            # (`9223372036854775808` is a compile error); the
            # unary-minus path special-cases it, and a bare literal
            # that large is rejected by the checker first.
            if value > 9223372036854775807:
                self._fail(
                    "integer literal out of int64 range",
                    span=node.span)
            return f"{value}", _PREC["primary"]
        if isinstance(value, float):
            return _rs_float(value), _PREC["primary"]
        if isinstance(value, str):
            return f"{_rs_str(value)}.to_string()", _PREC["primary"]
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
            # class functions, SPEC §9.5): an `Arc` over the fn
            # pointer (two fn items have distinct types, so the
            # pointer cast unifies them). A local/param/handler
            # binding is its Rust name (already an `Arc`).
            if binding.kind == "fn":
                ptr = self._rs_fn_ptr_type(binding.type) \
                    if _is_fn(binding.type) else None
                if ptr is None:
                    self._fail(f"internal error: fn `{node.name}` "
                               "has no fn type")
                return (f"std::sync::Arc::new({binding.rs} as {ptr})",
                        _PREC["primary"])
            if binding.kind == "global-let":
                self._need_rt("gget")
                return (f"newr_rt::gget(&{binding.rs})",
                        _PREC["primary"])
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
        # are small closures wrapped in an `Arc`. The parameter
        # types come from the context (fn_target) when known; the
        # checker types a bare builtin with `__unknown__` params,
        # so without a context there is nothing to build the
        # closure from - a clean E4001 (no conformance coverage).
        if not _is_fn(fn_target) or fn_target is None:
            self._fail(
                f"builtin `{name}` used as a value needs a known "
                "function type (pass it where its type is known)")
        params = fn_target.params[:-1]
        ret = fn_target.params[-1]
        if any(p.name == "__unknown__" for p in params + (ret,)):
            self._fail(
                f"builtin `{name}` used as a value needs a known "
                "function type (pass it where its type is known)")
        cast_text = self._rs_fn_type(fn_target)
        arg_names = [f"_a{i}" for i in range(len(params))]
        arg_texts = [self._rs_type(p) for p in params]

        def wrap(body: str) -> tuple[str, int]:
            plist = ", ".join(f"{n}: {t}" for n, t in
                              zip(arg_names, arg_texts))
            return (f"(std::sync::Arc::new(move |{plist}| {body}) "
                    f"as {cast_text})", _PREC["primary"])

        if name == "len":
            p0 = params[0]
            self._need_rt("str_len")
            if p0.name == "str":
                return wrap(f"newr_rt::str_len(&{arg_names[0]})")
            if p0.name in ("list", "dict", "set"):
                return wrap(f"{arg_names[0]}.len() as i64")
            self._fail(f"builtin `len` used as a value on `{p0}`")
        if name in ("min", "max"):
            a, b = arg_names[0], arg_names[1]
            if name == "min":
                return wrap(f"if {a} < {b} {{ {a} }} else {{ {b} }}")
            return wrap(f"if {a} < {b} {{ {b} }} else {{ {a} }}")
        if name == "abs":
            p0 = params[0]
            if _is_int(p0):
                self._need_rt("abs_long")
                return wrap(f"newr_rt::abs_long({arg_names[0]})")
            return wrap(f"{arg_names[0]}.abs()")
        if name == "round":
            p0 = params[0]
            ndigits = len(params) == 2
            if _is_int(p0):
                helper = ("round_half_away_int_n" if ndigits
                          else "round_half_away_int")
            elif _is_float(p0):
                helper = ("round_half_away_n" if ndigits
                          else "round_half_away")
            elif _is_bool(p0):
                helper = ("round_half_away_bool_n" if ndigits
                          else "round_half_away_bool")
            else:
                self._fail(f"builtin `round` used as a value on `{p0}`")
            self._need_rt(helper)
            args = ", ".join(arg_names)
            return wrap(f"newr_rt::{helper}({args})")
        if name == "range":
            self._need_rt("range")
            if len(params) == 1:
                return wrap(f"newr_rt::range({arg_names[0]})")
            if len(params) == 2:
                return wrap(f"newr_rt::range_from({arg_names[0]}, "
                            f"{arg_names[1]})")
            return wrap(f"newr_rt::range_step({arg_names[0]}, "
                        f"{arg_names[1]}, {arg_names[2]})")
        if name == "set":
            self._need_rt("Set")
            return wrap(f"newr_rt::Set::from_vec({arg_names[0]})")
        if name == "ok":
            return wrap(f"Ok({arg_names[0]})")
        if name == "err":
            return wrap(f"Err({arg_names[0]})")
        self._fail(f"builtin `{name}` is not a value")

    # ------------------------------------------------------------ interpolation

    def _rs_format_str(self, value: str) -> str:
        """A Rust string literal for a `format!` template: the
        literal escapes plus doubled braces (`{` -> `{{`)."""
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
            elif ch == "{":
                out.append("{{")
            elif ch == "}":
                out.append("}}")
            elif o < 0x20 or o == 0x7F:
                out.append(f"\\u{{{o:x}}}")
            else:
                out.append(ch)
        return '"' + "".join(out) + '"'

    def _emit_interp(self, node: A.Interp) -> tuple[str, int]:
        """A newr interpolation becomes `format!`: literal parts
        form the template (braces doubled), each expression part
        is a `{}` argument rendered through `_emit_interp_part`."""
        fmt_parts: list[str] = []
        args: list[str] = []
        for part in node.parts:
            if part.text is not None:
                # A literal brace becomes `{{` / `}}`; the
                # placeholders below stay single `{}`.
                fmt_parts.append(part.text.replace("{", "{{")
                                 .replace("}", "}}"))
            else:
                fmt_parts.append("{}")
                args.append(self._emit_interp_part(part.expr))
        template = "".join(fmt_parts)
        if not args:
            if not template:
                return "String::new()", _PREC["primary"]
            return f"{_rs_str(template)}.to_string()", _PREC["primary"]
        return (f"format!({_rs_str(template)}, "
                f"{', '.join(args)})", _PREC["primary"])

    def _emit_interp_part(self, expr: A.Expr) -> str:
        """One `{}` field. Bools and floats render through
        `to_str` (Python-repr semantics, J4); ints and strs are
        bare (`Display`); handler bindings use their interp form
        (the mention / name form); everything else goes through
        `to_str` (P8); a fn value renders as `"<fn>"`."""
        if isinstance(expr, A.Name):
            binding = self._lookup(expr.name)
            if binding is not None and binding.interp is not None:
                form = binding.interp_form()
                if "_channel_text" in form:
                    # The channel's mention form needs the helper.
                    self._used_channel_text = True
                if ".mention()" in form:
                    # `.mention()` needs the `Mentionable` trait.
                    self._used_mention = True
                return form
        t = self._type_of(expr)
        if _is_fn(t):
            return '"<fn>"'
        if _is_bool(t) or _is_float(t):
            self._need_rt("to_str")
            return f"newr_rt::to_str(&{self.emit_expr(expr, 0)})"
        if _is_int(t) or _is_str(t):
            return self.emit_expr(expr, 0)
        if t.nullable:
            self._need_rt("to_str")
            return f"newr_rt::to_str(&{self.emit_expr(expr, 0)})"
        if t.name in ("list", "dict", "set", "Result") or \
                t.name in self.structs or t.name in self.enums or \
                t.name == "error":
            self._need_rt("to_str")
            return f"newr_rt::to_str(&{self.emit_expr(expr, 0)})"
        return self.emit_expr(expr, 0)

    # ------------------------------------------------------------ operators

    def _emit_binop(self, node: A.BinOp) -> tuple[str, int]:
        op = node.op
        if op == "??":
            # M-09: the right side may be `T` (result `T`), `T?`
            # (result `T?`), or `none` (result `T?`). A `match`
            # keeps the right side lazy (it only runs when the
            # left is `None`), unlike `unwrap_or_else` (whose
            # closure must return `T`, not `T?`).
            result = self._type_of(node)
            left = self._owned(node.left)
            right = self._emit_value(node.right, result)
            if result.nullable:
                return (f"match {left} {{ Some(_v) => Some(_v), "
                        f"None => {right} }}", _PREC["ternary"])
            return (f"match {left} {{ Some(_v) => _v, "
                    f"None => {right} }}", _PREC["ternary"])
        if op in ("and", "or"):
            lt = self._type_of(node.left)
            if op == "or" and lt.nullable:
                # `T? or T` (SPEC §7.2): the default when the value
                # is absent. Rust `||` would also take the default
                # for a present-but-falsy value (0, ""), so it is
                # not the same - use the `??` form.
                result = self._type_of(node)
                left = self._owned(node.left)
                right = self._emit_value(node.right, result)
                return (f"match {left} {{ Some(_v) => _v, "
                        f"None => {right} }}", _PREC["ternary"])
            prec = _BINOP_PREC["&&" if op == "and" else "||"]
            left = self.emit_expr(node.left, prec)
            right = self.emit_expr(node.right, prec + 1)
            return f"{left} {'&&' if op == 'and' else '||'} {right}", prec
        if op == "in":
            rt = self._type_of(node.right)
            left = self.emit_expr(node.left, _BINOP_PREC["<"])
            if rt.name == "str":
                right = self.emit_expr(node.right, _BINOP_PREC["<"] + 1)
                return f"{right}.contains({left}.as_str())", \
                    _PREC["primary"]
            if rt.name == "list":
                self._need_rt("list_contains")
                right = self.emit_expr(node.right, _BINOP_PREC["<"] + 1)
                return (f"newr_rt::list_contains(&{right}, &{left})",
                        _PREC["primary"])
            if rt.name == "set":
                right = self.emit_expr(node.right, _BINOP_PREC["<"] + 1)
                return f"{right}.contains(&{left})", _PREC["primary"]
            if rt.name == "dict":
                right = self.emit_expr(node.right, _BINOP_PREC["<"] + 1)
                return f"{right}.contains_key(&{left})", _PREC["primary"]
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
                # Bare `==` is structural for every newr value in
                # Rust (Vec/Dict/Set/Result/Option/structs/enums
                # all derive PartialEq; f64's `==` is newr's, NaN
                # != NaN and -0.0 == 0.0; NewrError's PartialEq
                # is identity). Only fn values need pointer
                # equality (`Arc::ptr_eq`).
                if _is_fn(lt) or _is_fn(rt):
                    expr = (f"std::sync::Arc::ptr_eq(&{left}, "
                            f"&{right})")
                    if op == "!=":
                        expr = f"!{expr}"
                    return expr, _PREC["primary"]
                if _is_none(lt) and rt.nullable:
                    # `none == x` / `none != x` on a nullable value.
                    expr = (f"{right}.is_none()" if op == "=="
                            else f"{right}.is_some()")
                    return expr, _PREC["primary"]
                if _is_none(rt) and lt.nullable:
                    expr = (f"{left}.is_none()" if op == "=="
                            else f"{left}.is_some()")
                    return expr, _PREC["primary"]
                if lt.nullable and not rt.nullable and not _is_none(rt):
                    right = f"Some({right})"
                elif rt.nullable and not lt.nullable and not _is_none(lt):
                    left = f"Some({left})"
                jsop = "==" if op == "==" else "!="
                return f"{left} {jsop} {right}", prec
            # Ordering: strings compare lexicographically
            # (`cmp`); numbers compare bare.
            if _is_str(lt) or _is_str(rt):
                cmp = {"<": "< 0", "<=": "<= 0", ">": "> 0",
                       ">=": ">= 0"}[op]
                return (f"(({left}.cmp(&{right}) as i32) "
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
            helper = {"+": "add", "-": "sub", "*": "mul", "/": "tdiv",
                      "%": "tmod", "**": "pow_int"}[op]
            self._need_rt(helper)
            return (f"newr_rt::{helper}({left}, {right})",
                    _PREC["primary"])
        if op == "%" and _is_float(result):
            self._need_rt("fmod")
            return f"newr_rt::fmod({left}, {right})", _PREC["primary"]
        if op == "/" and _is_float(result):
            # P6: float /0 is an error (Rust `/` would give Infinity).
            self._need_rt("fdiv")
            return f"newr_rt::fdiv({left}, {right})", _PREC["primary"]
        if op == "**" and _is_float(result):
            self._need_rt("pow_float")
            return (f"newr_rt::pow_float({left}, {right})",
                    _PREC["primary"])
        if op == "+" and _is_str(result):
            return f"{left} + {right}.as_str()", prec
        if op == "*" and _is_str(result):
            # `str * int` / `int * str`: repeat. The count is i64;
            # `repeat` takes usize.
            lt = self._type_of(node.left)
            if _is_str(lt):
                return (f"{left}.repeat({right} as usize)", prec)
            return (f"{right}.repeat({left} as usize)", prec)
        return f"{left} {op} {right}", prec

    def _emit_unary(self, node: A.UnaryOp) -> tuple[str, int]:
        if node.op == "not":
            # Rust `!` binds tighter than newr's `not` (which is
            # looser than `==`): `!a == b` is `(!a) == b` in Rust.
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
            return "i64::MIN", _PREC["unary"]
        t = self._type_of(node.operand)
        if _is_int(t):
            self._need_rt("neg")
            return (f"newr_rt::neg({self.emit_expr(node.operand, 0)})",
                    _PREC["primary"])
        operand = self.emit_expr(node.operand, _PREC["pow"])
        # Rust traps: `--x` is not an operator, but `-(-x)` reads
        # better parenthesised, as does `-(pow(...))`.
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
                                       or obj.name == "db"):
                    # A named module used without an import
                    # (N-09/N-10): the name is both the real module
                    # and the reference.
                    fake = _Binding(rs=obj.name, kind="module",
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
        # the callee is an `Arc<dyn Fn>` and is called directly.
        ftype = self._type_of(func)
        f = self.emit_expr(func, _PREC["primary"],
                           fn_target=ftype if _is_fn(ftype) else None)
        params = ftype.params[:-1] if _is_fn(ftype) else []
        args = [self._emit_value(a, p, fn_target=p if _is_fn(p) else None)
                for a, p in zip(node.args, params)]
        args += [self.emit_expr(a, 0) for a in node.args[len(params):]]
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
            if args:
                return (f"newr_rt::input(&{args[0]})",
                        _PREC["primary"])
            return 'newr_rt::input("")', _PREC["primary"]
        if name == "len":
            return self._emit_builtin_call(node, None)
        if name in ("str", "int", "float", "bool"):
            return self._emit_conversion(node)
        if name in ("min", "max"):
            return self._emit_builtin_call(node, None)
        if name == "abs":
            return self._emit_builtin_call(node, None)
        if name == "round":
            t = self._type_of(node.args[0])
            args = [self.emit_expr(a, 0) for a in node.args]
            ndigits = len(args) == 2
            if _is_int(t):
                helper = ("round_half_away_int_n" if ndigits
                          else "round_half_away_int")
            elif _is_float(t):
                helper = ("round_half_away_n" if ndigits
                          else "round_half_away")
            elif _is_bool(t):
                helper = ("round_half_away_bool_n" if ndigits
                          else "round_half_away_bool")
            else:
                self._fail(f"`round` needs a number, not `{t}`")
            self._need_rt(helper)
            return (f"newr_rt::{helper}({', '.join(args)})",
                    _PREC["primary"])
        if name == "range":
            args = [self.emit_expr(a, 0) for a in node.args]
            if len(args) == 1:
                self._need_rt("range")
                return f"newr_rt::range({args[0]})", _PREC["primary"]
            if len(args) == 2:
                self._need_rt("range_from")
                return (f"newr_rt::range_from({args[0]}, {args[1]})",
                        _PREC["primary"])
            self._need_rt("range_step")
            return (f"newr_rt::range_step({args[0]}, {args[1]}, "
                    f"{args[2]})", _PREC["primary"])
        if name == "set":
            self._need_rt("Set")
            arg = self._owned(node.args[0])
            return (f"newr_rt::Set::from_vec({arg})",
                    _PREC["primary"])
        if name == "ok":
            self._need_rt("ok")
            t = self._type_of(node)
            expected = t.params[0]
            return (f"Ok({self._emit_value(node.args[0], expected)})",
                    _PREC["primary"])
        if name == "err":
            self._need_rt("err")
            t = self._type_of(node)
            expected = t.params[0]
            return (f"Err({self._emit_value(node.args[0], expected)})",
                    _PREC["primary"])
        binding = self._lookup(name)
        if binding is not None and binding.kind == "struct":
            # Struct construction: Rust has no kwargs, so keyword
            # arguments are reordered into declared field order
            # (the checker already validated names and types).
            decl = self.structs.get(name)
            if decl is None:
                self._fail(f"internal error: unknown struct `{name}`")
            order = [f.name for f in decl.fields]
            values: dict[str, str] = {}
            for a, f in zip(node.args, order):
                values[f] = self._emit_value(a, f.type)
            for kname, value, _span in node.kwargs:
                ftype = next(f.type for f in decl.fields
                             if f.name == kname)
                values[kname] = self._emit_value(value, ftype)
            parts = [f"{self._mangle(f)}: {values[f]}" for f in order
                     if f in values]
            if len(parts) != len(order):
                self._fail(
                    f"internal error: missing field in `{name}` "
                    "construction")
            return (f"{binding.rs} {{ {', '.join(parts)} }}",
                    _PREC["primary"])
        if binding is not None:
            if binding.kind != "fn" and _is_fn(binding.type):
                # A fn VALUE (a local, parameter or top-level let
                # holding a lambda or another fn): invoke the `Arc`
                # directly (conformance fn_as_value, fn_lambda_block,
                # fn_lambda_capture, method_as_value). A declared
                # fn (kind "fn") is a plain function and takes the
                # plain call below.
                ftype = binding.type
                params = ftype.params[:-1] if _is_fn(ftype) else []
                args = [self._emit_value(a, p,
                                         fn_target=p if _is_fn(p) else None)
                        for a, p in zip(node.args, params)]
                args += [self.emit_expr(a, 0)
                         for a in node.args[len(params):]]
                if binding.kind == "global-let":
                    # A fn value in a top-level let lives in a
                    # static `Mutex<Option<Arc<dyn Fn>>>` - read
                    # it through `gget` before calling it.
                    self._need_rt("gget")
                    return (f"newr_rt::gget(&{binding.rs})"
                            f"({', '.join(args)})", _PREC["primary"])
                return (f"{binding.rs}({', '.join(args)})",
                        _PREC["primary"])
            # A declared fn: plain call; positional args are owned
            # values, keyword args are reordered by _emit_kwargs.
            decl = self.fns.get(name)
            params = [p.type for p in decl.params] \
                if decl is not None else []
            args: list[str] = []
            for i, a in enumerate(node.args):
                if i < len(params):
                    p = params[i]
                    args.append(self._emit_value(
                        a, p, fn_target=p if _is_fn(p) else None))
                else:
                    args.append(self.emit_expr(a, 0))
            kwargs = self._emit_kwargs(node, binding)
            return (f"{binding.rs}({', '.join(args + kwargs)})",
                    _PREC["primary"])
        self._fail(f"unknown function `{name}`")

    def _emit_print(self, node: A.Call) -> tuple[str, int]:
        """`print` renders every argument with `to_str` (P8) - the
        runtime `print` helper does that mapping internally, so the
        emitted call is bare. A `?` argument propagates first (the
        temp + early return are hoisted before the statement, D7).
        When every argument is already a Rust string (or there are
        no arguments), `println!` prints them exactly as the
        runtime helper would (space-separated, no formatting), so
        no runtime import is needed for a plain `print "..."`.
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
                return "println!()", _PREC["primary"]
            fmt = " ".join("{}" for _ in parts)
            return (f'println!("{fmt}", {", ".join(parts)})',
                    _PREC["primary"])
        self._need_rt("print")
        self._need_rt("to_str")
        rendered = []
        for arg in node.args:
            if isinstance(arg, A.Propagate):
                rendered.append(
                    f"newr_rt::to_str(&{self._propagate_unwrapped(arg)})")
                continue
            t = self._type_of(arg)
            text = self.emit_expr(arg, 0)
            if _is_str(t):
                rendered.append(text)
            else:
                rendered.append(f"newr_rt::to_str(&{text})")
        return (f"newr_rt::print(&[{', '.join(rendered)}])",
                _PREC["primary"])

    def _emit_builtin_call(self, node: A.Call,
                           binding: _Binding | None) -> tuple[str, int]:
        name = node.func.name if isinstance(node.func, A.Name) else None
        args = node.args
        if name == "len":
            t = self._type_of(args[0])
            arg = self.emit_expr(args[0], 0)
            # `len` returns an int (i64). A str's length is its
            # code-point count (P5), not its byte length.
            if t.name == "str":
                self._need_rt("str_len")
                return f"newr_rt::str_len(&{arg})", _PREC["primary"]
            if t.name in ("list", "dict", "set"):
                return f"({arg}.len() as i64)", _PREC["primary"]
            return f"({arg}.len() as i64)", _PREC["primary"]
        if name == "min":
            a = self.emit_expr(args[0], 0)
            b = self.emit_expr(args[1], 0)
            return (f"if {a} < {b} {{ {a} }} else {{ {b} }}",
                    _PREC["ternary"])
        if name == "max":
            a = self.emit_expr(args[0], 0)
            b = self.emit_expr(args[1], 0)
            return (f"if {a} < {b} {{ {b} }} else {{ {a} }}",
                    _PREC["ternary"])
        if name == "abs":
            t = self._type_of(args[0])
            arg = self.emit_expr(args[0], _PREC["primary"])
            if _is_int(t):
                self._need_rt("abs_long")
                return f"newr_rt::abs_long({arg})", _PREC["primary"]
            return f"f64::abs({arg})", _PREC["primary"]
        self._fail(f"internal error: cannot emit builtin `{name}`")

    def _emit_conversion(self, node: A.Call) -> tuple[str, int]:
        name = node.func.name  # str | int | float | bool
        arg = node.args[0]
        t = self._type_of(arg)
        text = self.emit_expr(arg, 0)
        if name == "str":
            # A nullable str's runtime value is `None` when absent -
            # bare rendering would print nothing useful, not newr's
            # "none" (P8), so nullable values go through to_str.
            if _is_str(t) and not t.nullable:
                return text, _PREC["primary"]
            self._need_rt("to_str")
            return f"newr_rt::to_str(&{text})", _PREC["primary"]
        if name == "int":
            if _is_int(t):
                return text, _PREC["primary"]
            # float (truncation), str (parsing), bool (0/1) - all
            # through the runtime converters, which return an i64
            # in every case.
            if _is_float(t):
                self._need_rt("to_int_from_float")
                return (f"newr_rt::to_int_from_float({text})",
                        _PREC["primary"])
            if _is_str(t):
                self._need_rt("to_int_from_str")
                return (f"newr_rt::to_int_from_str(&{text})",
                        _PREC["primary"])
            self._need_rt("to_int_from_bool")
            return (f"newr_rt::to_int_from_bool({text})",
                    _PREC["primary"])
        if name == "float":
            if _is_float(t):
                return text, _PREC["primary"]
            if _is_str(t):
                self._need_rt("to_float_from_str")
                return (f"newr_rt::to_float_from_str(&{text})",
                        _PREC["primary"])
            if _is_int(t):
                return f"({text} as f64)", _PREC["primary"]
            return (f"if {text} {{ 1.0 }} else {{ 0.0 }}",
                    _PREC["ternary"])  # bool
        # bool
        if _is_bool(t):
            return text, _PREC["primary"]
        if _is_str(t):
            self._need_rt("to_bool_from_str")
            return (f"newr_rt::to_bool_from_str(&{text})",
                    _PREC["primary"])
        if _is_int(t):
            return f"({text} != 0)", _PREC["primary"]
        return f"({text} != 0.0)", _PREC["primary"]  # float

    def _emit_enum_call(self, enum_name: str, variant: str,
                        node: A.Call, decl=None,
                        ref: str | None = None) -> tuple[str, int]:
        """`Shape.Circle(radius: 1.5)` -> the variant factory
        function; a bare variant is the nullary factory
        (`Color.Red()`). Rust has no kwargs, so keyword arguments
        are reordered into declared field order (the checker already
        validated them). `decl` / `ref` serve the §20 qualified
        imported form (`m_helper.Shape.Circle(...)`): the
        declaration comes from the imported module and the enum
        lives under the module reference; the local form takes both
        from this file."""
        prefix = (f"{ref}::" if ref else "") + self._mangle(enum_name)
        decl = decl if decl is not None else self.enums.get(enum_name)
        if decl is not None:
            fields = []
            for var in decl.variants:
                if var.name == variant:
                    fields = [(f.name, f.type) for f in var.fields]
                    break
            if not fields:
                # A bare variant (no fields): the nullary factory.
                return (f"{prefix}::{self._mangle(variant)}()",
                        _PREC["primary"])
            values: dict[str, str] = {}
            for a, (fname, ftype) in zip(node.args, fields):
                values[fname] = self._emit_value(a, ftype)
            for kname, value, _span in node.kwargs:
                ftype = next(ft for fn_, ft in fields if fn_ == kname)
                values[kname] = self._emit_value(value, ftype)
            parts = [values[f] for f, _t in fields if f in values]
            return (f"{prefix}::{self._mangle(variant)}"
                    f"({', '.join(parts)})", _PREC["primary"])
        args = [self.emit_expr(a, 0) for a in node.args]
        return (f"{prefix}::{self._mangle(variant)}"
                f"({', '.join(args)})", _PREC["primary"])

    def _emit_log_call(self, level: str, node: A.Call) -> tuple[str, int]:
        """`log.info/warn/error(msg)` (P10): info -> stdout,
        warn/error -> stderr, no timestamp."""
        msg = self.emit_expr(node.args[0], 0)
        if level == "info":
            return f'println!("{{}}", {msg})', _PREC["primary"]
        return f'eprintln!("{{}}", {msg})', _PREC["primary"]

    def _file_module_mod(self, binding: _Binding) -> str:
        """The Rust module path for a file-module import: the file
        STEM, not the import alias (R2 - one `.rs` per file,
        named after the stem). For `import "h_fn.newr" as h` the
        module is `h_fn`, so every reference to the module must use
        the stem; using the alias emits `h::…` against a module
        named `h_fn` and does not compile."""
        if binding.kind == "file-module":
            return self.imported_modules.get(binding.rs, binding.rs)
        return binding.rs

    def _imported_enum(self, node: A.Member):
        """A qualified imported enum member, `module.Enum.Variant`.

        Returns (ref, enum_name, variant, decl) when the chain is
        exactly that shape, the inner name is an enum declared in
        the imported module, else None. The decl check matters: a
        struct-typed top-level let accessed the same way
        (`m_helper.SomeLet.field`) is NOT an enum member and must
        fall through to the ordinary typed-member path. The decl is
        returned too, so a fielded-variant call can reorder its
        keyword arguments into declared field order (Rust has no
        kwargs)."""
        obj = node.obj
        if not isinstance(obj, A.Member) or not isinstance(obj.obj, A.Name):
            return None
        binding = self._lookup(obj.obj.name)
        if binding is None or binding.kind != "file-module":
            return None
        sub = self.program.modules.get(binding.rs)
        if sub is None:
            return None
        decl = next((e for e in sub.enums if e.name == obj.name), None)
        if decl is None:
            return None
        return (self._file_module_mod(binding), obj.name, node.name,
                decl)

    def _enum_field_access(self, expr_text: str, enum_name: str,
                           field_name: str, field_type: Type | None) -> str:
        """`expr.field` for an enum field. (Rust has no `object`
        union type - conflicting field types are E4001 in
        `_rs_type` - so no cast is ever needed here.)"""
        return f"{expr_text}.{self._mangle(field_name)}"

    # Discord entity fields (SPEC §7.3, ENTITY_FIELDS in
    # discord_limits) -> the serenity expression for that field.
    # The cache-dependent ones (`channel.name`, `member.guild`,
    # `interaction.guild`/`interaction.channel`) are E4001 until
    # the bot phase wires them through the handler context (no
    # conformance or example coverage).
    _ENTITY_GETTERS: dict[str, dict[str, str]] = {
        "user": {"id": "{obj}.id.get() as i64",
                 "mention": "{obj}.mention().to_string()",
                 "name": "{obj}.name.clone()",
                 "bot": "{obj}.bot"},
        "member": {"id": "{obj}.user.id.get() as i64",
                   "mention": "{obj}.mention().to_string()",
                   "name": "{obj}.user.name.clone()"},
        "guild": {"id": "{obj}.id.get() as i64",
                  "name": "{obj}.name.clone()",
                  "member_count": "{obj}.member_count as i64"},
        "channel": {"id": "{obj}.get() as i64",
                    "mention": "{obj}.mention().to_string()"},
        "role": {"id": "{obj}.id.get() as i64",
                 "name": "{obj}.name.clone()"},
        "interaction": {"user": "{obj}.user.clone()",
                        "locale": "{obj}.locale.clone()"},
        "message": {"content": "{obj}.content.clone()",
                    "author": "{obj}.author.clone()",
                    "channel": "{obj}.channel_id"},
    }

    def _entity_field_expr(self, obj_text: str, obj_type: Type,
                           field: str) -> str | None:
        """The Rust expression for a Discord entity field, or None
        when the type is not an entity type (the ordinary struct
        field path then applies)."""
        table = self._ENTITY_GETTERS.get(obj_type.name)
        if table is None or field not in table:
            return None
        return table[field].format(obj=obj_text)

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
            if obj.name in MODULES or obj.name == "db":
                # A named module used without an import: the name is
                # both the real module and the reference.
                fake = _Binding(rs=obj.name, kind="module",
                                module=obj.name)
                return self._emit_module_member(node, fake)
        # §20: qualified imported enum member, `m_helper.Shade.Red`.
        # The inner `m_helper.Shade` has no type of its own (the
        # checker types only the full chain), so resolve the whole
        # chain here from the imported module's enum declaration.
        imported = self._imported_enum(node)
        if imported is not None:
            ref, enum_name, variant, _decl = imported
            return (f"{ref}::{self._mangle(enum_name)}::"
                    f"{self._mangle(variant)}()", _PREC["primary"])
        obj_type = self._type_of(obj)
        t = self._type_of(node)
        # The newr `error` type has exactly two fields (checker
        # ERROR_FIELDS): message (str) and code (str?). Both are
        # public fields on `NewrError` (conformance try_catch).
        if obj_type.name == "error":
            # `message` / `code` are the runtime's pub fields -
            # they are NOT mangled (`message` is emitter-reserved
            # for the bot, but the field name on NewrError is
            # literally `message`).
            obj_text = self.emit_expr(obj, _PREC["primary"])
            return f"{obj_text}.{node.name}", _PREC["primary"]
        # §16 (hole 2): a method used as a value is a closure, not a
        # Rust attribute - a Vec has no `len` field. A real struct
        # field stays a plain attribute.
        if t.name == "fn" and not self._is_plain_field(obj_type,
                                                       node.name):
            sig = self._method_value_sig(obj_type, node.name)
            if sig is not None:
                return self._emit_method_value(node, obj_type, sig,
                                               fn_target)
        obj_text = self.emit_expr(obj, _PREC["primary"])
        # Discord entity fields (SPEC §7.3) -> serenity. The
        # checker types these fields (ENTITY_FIELDS in
        # discord_limits); Rust has no `.id` field on a User - the
        # snowflake is `id.get()` (newr `id` is int, so the String
        # `id.to_string()` would be the wrong type).
        entity = self._entity_field_expr(obj_text, obj_type, node.name)
        if entity is not None:
            if ".mention()" in entity:
                # `.mention()` needs the `Mentionable` trait.
                self._used_mention = True
            return entity, _PREC["primary"]
        # Bot handlers: `member.guild`, `interaction.guild` /
        # `interaction.channel`, and `channel.name` have no serenity
        # field to read directly - they resolve through the cache
        # (M5). Anything else the table does not know is a clean
        # E4001, never a `.field` that does not exist.
        if isinstance(obj, A.Name):
            binding = self._lookup(obj.name)
            if binding is not None and binding.kind == "handler":
                handled = self._emit_handler_entity_field(
                    binding, obj_type, node.name)
                if handled is not None:
                    return handled, _PREC["primary"]
        if obj_type.name in self._ENTITY_GETTERS:
            self._fail(
                f"`{obj_type.name}.{node.name}` is not supported in "
                "the Rust emitter (SPEC §7.3 lists the fields a "
                "target must provide)", span=node.span)
        # An enum field reads through the union field.
        if self._find_enum_decl(obj_type.name) is not None:
            return (self._enum_field_access(obj_text, obj_type.name,
                                           node.name, t),
                    _PREC["primary"])
        return f"{obj_text}.{self._mangle(node.name)}", _PREC["primary"]

    def _emit_math_member(self, node: A.Member) -> tuple[str, int]:
        if node.name in ("pi", "e"):
            return ("std::f64::consts::PI" if node.name == "pi"
                    else "std::f64::consts::E", _PREC["primary"])
        self._fail(f"math has no member `{node.name}`")

    def _emit_enum_variant(self, node: A.Member,
                           binding: _Binding) -> tuple[str, int]:
        prefix = ((binding.module + "::") if binding.module else "") + \
            binding.rs
        return (f"{prefix}::{self._mangle(node.name)}()",
                _PREC["primary"])

    def _emit_method_value(self, node: A.Member, obj_type: Type,
                           sig, fn_target: Type | None = None) -> tuple[str, int]:
        """A method used as a value (§16, hole 2): a closure taking
        the method's parameters, capturing the receiver by value
        (D10). The receiver is snapshotted into a hoisted `let`
        local (so the receiver expression is evaluated once, when
        the closure is created, never again at call time), and the
        closure body is the ordinary method call on the snapshot.
        The closure is wrapped in an `Arc` and cast to its fn type."""
        params, ret = sig
        obj_text = self._owned(node.obj)
        snap_name = self._new_temp_name()
        snap_type = self._rs_type(obj_type)
        self._hoists_add([f"let {snap_name}: {snap_type} = {obj_text};"])
        self._push_scope()
        arg_bindings = []
        arg_nodes = []
        for i, ptype in enumerate(params):
            b = self._bind_fresh(f"_a{i}", type=ptype, kind="param")
            arg_bindings.append(b)
            arg_nodes.append(A.Name(span=node.span, name=f"_a{i}",
                                    expr_type=ptype))
        recv_binding = _Binding(rs=snap_name, type=obj_type,
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
        arg_names = ", ".join(b.rs for b in arg_bindings)
        cast = fn_target if _is_fn(fn_target) else self._type_of(node)
        cast_text = self._rs_fn_type(cast)
        arrow = f"move |{arg_names}| {body}"
        if hoists:
            # A hoisted block-body closure: the hoisted const's
            # initializer is the closure, and the hoists run inside
            # the closure body at call time.
            name = self._new_lambda_name()
            lines = [f"let {name}: {cast_text} = "
                     f"std::sync::Arc::new(move |{arg_names}| {{"]
            for h in hoists:
                lines.append("  " + h)
            lines.append(f"  {body}")
            lines.append("});")
            self._hoists_add(lines)
            return name, _PREC["primary"]
        return (f"(std::sync::Arc::new({arrow}) as {cast_text})",
                _PREC["primary"])

    def _global_root(self, expr: A.Expr) -> _Binding | None:
        """The global-let binding at the root of `expr` (a Name, or
        a Member/Index chain rooted at one), or None. Mutating a
        global collection must go through `gmut` (a `gget` clone
        would lose the mutation)."""
        cur = expr
        while isinstance(cur, (A.Member, A.Index)):
            cur = cur.obj
        if isinstance(cur, A.Name):
            binding = self._lookup(cur.name)
            if binding is not None and binding.kind == "global-let":
                return binding
        return None

    # ------------------------------------------------------------ method calls

    def _emit_method_call(self, node: A.Call, func: A.Member) -> tuple[str, int]:
        """Method calls, mirroring the Python emitter's tables
        exactly (SPEC §12 + METHODS in compiler/modules.py). Only
        the methods the checker allows are reachable here."""
        obj = func.obj
        obj_type = self._type_of(obj)
        if node.kwargs:
            self._fail(f"method `.{func.name}` takes no named arguments")
        name = func.name
        if obj_type.nullable:
            # Nullable methods only (is_some / is_none / unwrap). A
            # `?.` call on a nullable was handled by the chain
            # collector, so func.optional is False here.
            obj_text = self._owned(obj)
            if name == "is_some":
                return f"({obj_text}.is_some())", _PREC["primary"]
            if name == "is_none":
                return f"({obj_text}.is_none())", _PREC["primary"]
            if name == "unwrap":
                self._need_rt("unwrap_opt")
                return (f"newr_rt::unwrap_opt({obj_text})",
                        _PREC["primary"])
            self._fail(f"`{obj_type}` has no method `{name}`")
        tname = obj_type.name
        if tname == "str":
            obj_text = self.emit_expr(obj, _PREC["primary"])
            args = [self.emit_expr(a, 0) for a in node.args]
            if name == "upper":
                return f"{obj_text}.to_uppercase()", _PREC["primary"]
            if name == "lower":
                return f"{obj_text}.to_lowercase()", _PREC["primary"]
            if name == "strip":
                # Rust's trim() removes Unicode whitespace,
                # matching Python's str.strip().
                return f"{obj_text}.trim().to_string()", _PREC["primary"]
            if name == "split":
                self._need_rt("split_str")
                return (f"newr_rt::split_str(&{obj_text}, &{args[0]})",
                        _PREC["primary"])
            if name == "join":
                # newr `"sep".join(parts)` - Rust joins the LIST
                # with the separator.
                return (f"{args[0]}.join({obj_text}.as_str())",
                        _PREC["primary"])
            if name == "contains":
                return (f"{obj_text}.contains({args[0]}.as_str())",
                        _PREC["primary"])
            if name == "starts_with":
                return (f"{obj_text}.starts_with({args[0]}.as_str())",
                        _PREC["primary"])
            if name == "ends_with":
                return (f"{obj_text}.ends_with({args[0]}.as_str())",
                        _PREC["primary"])
            if name == "chars":
                # SPEC pin P5: code points, not UTF-16 code units.
                self._need_rt("chars")
                return f"newr_rt::chars(&{obj_text})", _PREC["primary"]
        elif tname == "list":
            if name == "append":
                arg = self._emit_value(node.args[0],
                                       obj_type.params[0])
                root = self._global_root(obj)
                if root is not None:
                    self._need_rt("gmut")
                    return (f"newr_rt::gmut(&{root.rs}, |_v| {{ "
                            f"_v.push({arg}); }})", _PREC["primary"])
                obj_text = self.emit_expr(obj, _PREC["primary"])
                return f"{obj_text}.push({arg})", _PREC["primary"]
            if name == "contains":
                self._need_rt("list_contains")
                obj_text = self.emit_expr(obj, _PREC["primary"])
                arg = self.emit_expr(node.args[0], 0)
                return (f"newr_rt::list_contains(&{obj_text}, &{arg})",
                        _PREC["primary"])
            if name == "get":
                self._need_rt("list_get")
                obj_text = self.emit_expr(obj, _PREC["primary"])
                idx = self.emit_expr(node.args[0], 0)
                dflt = self._emit_value(node.args[1],
                                        self._type_of(node))
                return (f"newr_rt::list_get(&{obj_text}, {idx}, "
                        f"{dflt})", _PREC["primary"])
            if name == "len":
                obj_text = self.emit_expr(obj, _PREC["primary"])
                return f"({obj_text}.len() as i64)", _PREC["primary"]
        elif tname == "dict":
            obj_text = self.emit_expr(obj, _PREC["primary"])
            if name == "keys":
                return f"{obj_text}.keys()", _PREC["primary"]
            if name == "get":
                key = self.emit_expr(node.args[0], 0)
                dflt = self._emit_value(node.args[1],
                                        self._type_of(node))
                return (f"{obj_text}.get(&{key}).unwrap_or_else(|| "
                        f"{dflt})", _PREC["primary"])
            if name == "contains":
                key = self.emit_expr(node.args[0], 0)
                return f"{obj_text}.contains_key(&{key})", \
                    _PREC["primary"]
        elif tname == "set":
            if name == "add":
                arg = self._emit_value(node.args[0],
                                       obj_type.params[0])
                root = self._global_root(obj)
                if root is not None:
                    self._need_rt("gmut")
                    return (f"newr_rt::gmut(&{root.rs}, |_v| {{ "
                            f"_v.add({arg}); }})", _PREC["primary"])
                obj_text = self.emit_expr(obj, _PREC["primary"])
                # `Set::add` returns bool; newr's `add` returns
                # none - discard it in a block.
                return f"{{ {obj_text}.add({arg}); }}", _PREC["primary"]
            if name == "remove":
                self._need_rt("set_remove")
                arg = self.emit_expr(node.args[0], 0)
                root = self._global_root(obj)
                if root is not None:
                    self._need_rt("gmut")
                    return (f"newr_rt::gmut(&{root.rs}, |_v| {{ "
                            f"newr_rt::set_remove(_v, &{arg}); }})",
                            _PREC["primary"])
                obj_text = self.emit_expr(obj, _PREC["primary"])
                return (f"newr_rt::set_remove(&mut {obj_text}, &{arg})",
                        _PREC["primary"])
            if name == "contains":
                obj_text = self.emit_expr(obj, _PREC["primary"])
                arg = self.emit_expr(node.args[0], 0)
                return f"{obj_text}.contains(&{arg})", _PREC["primary"]
        elif tname == "Result":
            if name in ("is_ok", "is_err"):
                obj_text = self.emit_expr(obj, _PREC["primary"])
                method = "is_ok" if name == "is_ok" else "is_err"
                return f"{obj_text}.{method}()", _PREC["primary"]
            if name == "unwrap":
                obj_text = self._owned(obj)
                err_type = obj_type.params[1] \
                    if len(obj_type.params) == 2 else None
                if err_type is not None and _is_error(err_type):
                    self._need_rt("result_unwrap")
                    return (f"newr_rt::result_unwrap({obj_text})",
                            _PREC["primary"])
                self._need_rt("result_unwrap_any")
                return (f"newr_rt::result_unwrap_any({obj_text})",
                        _PREC["primary"])
            if name == "unwrap_or":
                obj_text = self._owned(obj)
                dflt = self._emit_value(node.args[0],
                                        self._type_of(node))
                err_type = obj_type.params[1] \
                    if len(obj_type.params) == 2 else None
                if err_type is not None and _is_error(err_type):
                    self._need_rt("result_unwrap_or")
                    return (f"newr_rt::result_unwrap_or({obj_text}, "
                            f"{dflt})", _PREC["primary"])
                return (f"{obj_text}.unwrap_or({dflt})",
                        _PREC["primary"])
        # Rebased `?.` chains (D38): inside a `.map` closure the
        # receiver is the STRIPPED inner type, so a nullable method
        # (`is_some` / `is_none` / `unwrap`) reaches here on a
        # non-nullable, non-Result receiver. The value is proven
        # non-none inside the closure, so the answer is constant
        # (true / false / the value itself). The checker rejects
        # these calls on a plain value, so they can only come from
        # a rebased chain.
        if name == "is_some":
            return "true", _PREC["primary"]
        if name == "is_none":
            return "false", _PREC["primary"]
        if name == "unwrap":
            return self.emit_expr(obj, _PREC["primary"]), _PREC["primary"]
        self._fail(
            f"`{obj_type}` has no method `{name}` (the checker should "
            "have rejected this)")

    # ------------------------------------------------------------ index

    def _emit_index(self, node: A.Index) -> tuple[str, int]:
        obj_type = self._type_of(node.obj)
        obj_text = self.emit_expr(node.obj, _PREC["primary"])
        idx = self.emit_expr(node.index, 0)
        if obj_type.name == "dict":
            # A missing key is a KeyError (Python semantics; the
            # Java emitter's `.get()` returning null is the
            # divergence, not the rule).
            return f"{obj_text}.index(&{idx})", _PREC["primary"]
        if obj_type.name == "list":
            self._need_rt("list_index")
            # Indexing is the checked form: a negative or out-of
            # range index raises IndexError (rt list_index), exactly
            # like newr's `xs[i]`. (`list.get` is the defaulting
            # form - see _emit_method_call.)
            return (f"newr_rt::list_index(&{obj_text}, {idx})",
                    _PREC["primary"])
        if obj_type.name == "str":
            # newr indexes a string by code point (P5); a Rust
            # string index is a byte offset.
            self._need_rt("str_index")
            return (f"newr_rt::str_index(&{obj_text}, {idx})",
                    _PREC["primary"])
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
        """`?.` lowering (D9, Rust twin): one `.map(...)` call per
        optional link, nested; non-optional links after an optional
        one fold into the nearest closure body. `a?.b.c` is one call;
        `a?.b?.c` is two. The closure body is the original sub-chain
        rebased onto the closure parameter, so method dispatch,
        indexing and conversions inside it are exactly the normal
        ones. The base is cloned (`.map` consumes the `Option`)."""
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
            # Inside the closure the receiver is proven non-none
            # (`.map` only calls it then), so the parameter and
            # the rebased base carry the STRIPPED (inner) type -
            # otherwise a method call on it would dispatch as if the
            # value were still nullable.
            inner_type = Type(recv_type.name, params=recv_type.params,
                              nullable=False)
            # §16 (hole 1): a segment must also end before any link
            # whose receiver can be `none` INSIDE the closure - a
            # nullable field (`o?.inner.name`) or a nullable method
            # result (`r?.unwrap().upper()` on `Result[str?, _]?`).
            # The checker's `expr_type` conflates the `?.`
            # short-circuit with a field's own nullability, so the
            # in-closure types are walked link by link instead.
            # `in_lambda` maps each link in this segment to its
            # in-closure result type, so `_rebase` types the clones
            # exactly: a Member link's nullability inside the closure
            # is its FIELD's nullability, not the checker's chain
            # type - without this, a method after a nullable field
            # dispatches against `T?` and fails with E4001.
            in_lambda: dict[int, Type | None] = {}
            cur_type = self._in_lambda_link_type(links[i], inner_type)
            if i > first_opt and isinstance(links[i], A.Call) \
                    and isinstance(links[i].func, A.Member) \
                    and links[i].func.name == "unwrap":
                # A `.unwrap()` that is not the first optional
                # link unwraps the chain's `Option` result
                # directly (see (a) above): the value is a plain
                # `T` from here on, not an `Option<T>`.
                self._need_rt("unwrap_opt")
                cur_text = f"newr_rt::unwrap_opt({cur_text})"
                i += 1
                if i >= len(links):
                    break
                # More links follow on the plain (unwrapped)
                # value: rebase them onto a fresh parameter and
                # apply it immediately.
                unwrapped_type = self._type_of(links[i - 1])
                self._push_scope()
                param = self._bind_fresh("_v", type=unwrapped_type,
                                         kind="param")
                new_base = A.Name(span=links[i - 1].span, name="_v",
                                  expr_type=unwrapped_type)
                body_node = self._rebase(links[-1], links[i - 1],
                                         new_base, None)
                body = self.emit_expr(body_node, 0)
                self._pop_scope()
                return (f"(|{param.rs}| {body})({cur_text})",
                        _PREC["primary"])
            in_lambda[id(links[i])] = cur_type
            j = i + 1
            while j < len(links):
                if self._link_is_optional(links[j]):
                    break
                if cur_type is None or cur_type.nullable:
                    break
                if isinstance(links[j], A.Call) \
                        and isinstance(links[j].func, A.Member) \
                        and links[j].func.name == "unwrap":
                    # A later `.unwrap()` applies to the segment's
                    # `Option` result, not to the in-closure value
                    # (D38 would no-op it inside the closure and
                    # leave an `Option` where the checker's type
                    # says a plain value) - end the segment
                    # before it.
                    break
                cur_type = self._in_lambda_link_type(links[j], cur_type)
                in_lambda[id(links[j])] = cur_type
                j += 1
            seg_end = links[j - 1]
            self._push_scope()
            binding = self._bind_fresh("_v", type=inner_type,
                                       kind="param")
            # The rebased base is a Name for the NEWR name `_v` -
            # the lookup returns the binding's Rust name (which may
            # be `_v_1` in a nested chain).
            new_base = A.Name(span=links[i].span, name="_v",
                              expr_type=inner_type)
            body_node = self._rebase(seg_end, inner_recv, new_base,
                                     in_lambda)
            body_type = in_lambda.get(id(seg_end)) or self._type_of(node)
            body = self._callable_body(body_node, binding.rs,
                                       inner_type, body_type)
            self._pop_scope()
            if not body.startswith("|"):
                # A hoisted body is an `Arc<dyn Fn>` NAME - `.map`
                # / `.and_then` take a closure, not an `Arc`, so
                # wrap the call in one.
                body = f"|{binding.rs}| {body}({binding.rs})"
            method = "and_then" if body_type.nullable else "map"
            cur_text = f"({cur_text}).clone().{method}({body})"
            i = j
        return cur_text, _PREC["primary"]

    def _callable_body(self, expr: A.Expr, param_rs: str,
                       param_type: Type, body_type: Type) -> str:
        """Emit `expr` as a closure body `|_v| ...`. If the body
        needs hoists (a nested block-body lambda), the closure
        becomes a hoisted `let` const holding an `Arc<dyn Fn>` and
        its name is returned instead, so the hoisted statements run
        at call time, inside the function, not at the enclosing
        statement."""
        saved = self._hoists
        self._hoists = []
        body = self.emit_expr(expr, 0)
        hoists = self._hoists
        self._hoists = saved
        if not hoists:
            return f"|{param_rs}| {body}"
        name = self._new_lambda_name()
        fn_type = self._rs_fn_type(fn_of([param_type], body_type))
        lines = [f"let {name}: {fn_type} = std::sync::Arc::new("
                 f"move |{param_rs}| {{"]
        for h in hoists:
            lines.extend("  " + line for line in h)
        lines.append(f"  {body}")
        lines.append("});")
        self._hoists.append(lines)
        return name

    def _emit_lambda(self, node: A.Lambda,
                     fn_target: Type | None = None) -> tuple[str, int]:
        """Closures capture by value (D10, J8): free variables are
        snapshotted into hoisted `let` locals before the enclosing
        statement - a Rust closure may only capture by move when it
        is a `move` closure, so a snapshot local (never reassigned
        after the snapshot) is exactly the captured value. A block
        body cannot be an inline closure body with statements, so it
        is hoisted to a `let` const immediately before the enclosing
        statement. The closure is wrapped in an `Arc` and cast to
        its fn type (fn_target when the context provides one)."""
        free = self._lambda_free_vars(node)
        # A lambda that ASSIGNS to a captured variable would need
        # `FnMut`, which cannot coerce to `dyn Fn` - a clean E4001
        # (method-call mutation through a capture, e.g.
        # `xs.append(4)`, is undetectable here and is a known gap).
        free_names = {name for name, _b in free}
        for st in (node.body or []):
            if isinstance(st, A.Assign) and st.name in free_names:
                self._fail(
                    f"a lambda cannot assign to captured variable "
                    f"`{st.name}` (the Rust emitter needs `Fn`, not "
                    "`FnMut`)")
        self._push_scope()
        saved_used = self._fn_used
        self._fn_used = set()
        param_bindings = [self._bind_fresh(p.name, type=p.type,
                                           kind="param")
                          for p in node.params]
        # Snapshot locals: one per free variable, hoisted before the
        # enclosing statement (so the lambda captures the value at
        # creation, not a later reassignment).
        for name, b in free:
            snap = self._new_temp_name()
            snap_type = self._rs_type(b.type)
            snap_value = self._owned(
                A.Name(span=node.span, name=name, expr_type=b.type))
            self._hoists_add([f"let {snap}: {snap_type} = {snap_value};"])
            self._scopes[-1][name] = _Binding(rs=snap, type=b.type,
                                              kind="param")
        params_text = ", ".join(
            f"{b.rs}: {self._rs_type(b.type)}" for b in param_bindings)
        fn_type = self._type_of(node)
        cast = fn_target if _is_fn(fn_target) else fn_type
        cast_text = self._rs_fn_type(cast)
        if node.expr_body is not None:
            body, hoists = self._emit_expr_with_hoists(node.expr_body)
            self._fn_used = saved_used
            self._pop_scope()
            if not hoists:
                arrow = (f"std::sync::Arc::new(move |{params_text}| "
                         f"{body}) as {cast_text}")
                return arrow, _PREC["primary"]
            # The body needed hoists (a nested block-body lambda):
            # the hoisted const's initializer IS the closure, so the
            # snapshot locals are in scope inside the closure body at
            # call time.
            name = self._new_lambda_name()
            lines = [f"let {name}: {cast_text} = std::sync::Arc::new("
                     f"move |{params_text}| {{"]
            for h in hoists:
                lines.append("  " + h)
            lines.append(f"  {body}")
            lines.append("});")
            self._hoists_add(lines)
            return name, _PREC["primary"]
        # Block body: hoist a const closure.
        name = self._new_lambda_name()
        self._fn_ctx.append({"return_type": node.return_type,
                             "ensures": [], "in_handler": False})
        body_lines = self._emit_block(node.body or [], 1)
        self._fn_ctx.pop()
        self._fn_used = saved_used
        self._pop_scope()
        lines = [f"let {name}: {cast_text} = std::sync::Arc::new("
                 f"move |{params_text}| {{"]
        lines.extend(body_lines if body_lines else ["  ()"])
        lines.append("});")
        self._hoists_add(lines)
        return name, _PREC["primary"]

    # ------------------------------------------------------------ statements

    def _emit_stmt(self, stmt, indent: int) -> list[str]:
        p = "  " * indent
        if isinstance(stmt, (A.Let, A.Const)):
            binding = self._lookup(stmt.name)
            is_top = (self._in_top_level and not self._fn_ctx
                      and binding is not None
                      and binding.kind == "global-let")
            t = stmt.type_ann
            if t is None:
                t = self._type_of(stmt.value)
            if isinstance(stmt.value, A.Propagate):
                value = self._propagate_unwrapped(stmt.value)
                value = self._wrap_nullable(
                    value, self._type_or_none(stmt.value), t)
            else:
                tgt = t if _is_fn(t) else None
                value = self._emit_value(stmt.value, t, fn_target=tgt)
            if is_top:
                # A top-level `let`/`const` is a static of the
                # module (so top-level functions can read it), and
                # its initializer runs here, in file order, inside
                # main() (or the module's __newr_init()) - a
                # declaration-time initializer would run before
                # main() and break file order for side-effecting
                # initializers (R2). Emitting a local declaration
                # here would shadow the static and top-level
                # functions would read the still-empty static.
                self._need_rt("gset")
                return [f"{p}newr_rt::gset(&{binding.rs}, {value});"]
            binding = self._bind(stmt.name, type=t, kind="local")
            keyword = "let " if isinstance(stmt, A.Const) else "let mut "
            return [f"{p}{keyword}{binding.rs}: {self._rs_type(t)} = "
                    f"{value};"]
        if isinstance(stmt, A.Assign):
            return self._emit_assign(stmt, indent)
        if isinstance(stmt, A.If):
            return self._emit_if(stmt, indent)
        if isinstance(stmt, A.For):
            return self._emit_for(stmt, indent)
        if isinstance(stmt, A.While):
            cond = self.emit_expr(stmt.cond, 0)
            lines = [f"{p}while {cond} {{"]
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
            value = self._wrap_nullable(
                value, self._type_or_none(stmt.value), binding.type)
        else:
            tgt = binding.type if _is_fn(binding.type) else None
            value = self._emit_value(stmt.value, binding.type,
                                     fn_target=tgt)
        if binding.kind == "global-let":
            self._need_rt("gset")
            if stmt.op == "=":
                return [f"{p}newr_rt::gset(&{binding.rs}, {value});"]
            t = binding.type
            cur = f"newr_rt::gget(&{binding.rs})"
            self._need_rt("gget")
            if _is_int(t):
                helper = {"+=": "add", "-=": "sub", "*=": "mul",
                          "/=": "tdiv", "%=": "tmod"}[stmt.op]
                self._need_rt(helper)
                return [f"{p}newr_rt::gset(&{binding.rs}, "
                        f"newr_rt::{helper}({cur}, {value}));"]
            if _is_float(t) and stmt.op in ("/=", "%="):
                helper = "fdiv" if stmt.op == "/=" else "fmod"
                self._need_rt(helper)
                return [f"{p}newr_rt::gset(&{binding.rs}, "
                        f"newr_rt::{helper}({cur}, {value}));"]
            if _is_str(t) and stmt.op == "+=":
                return [f"{p}newr_rt::gset(&{binding.rs}, "
                        f"{cur} + {value}.as_str());"]
            return [f"{p}newr_rt::gset(&{binding.rs}, "
                    f"{cur} {stmt.op} {value});"]
        if stmt.op == "=":
            return [f"{p}{binding.rs} = {value};"]
        t = binding.type
        if _is_int(t):
            helper = {"+=": "add", "-=": "sub", "*=": "mul",
                      "/=": "tdiv", "%=": "tmod"}[stmt.op]
            self._need_rt(helper)
            return [f"{p}{binding.rs} = newr_rt::{helper}({binding.rs}, "
                    f"{value});"]
        if _is_float(t) and stmt.op in ("/=", "%="):
            helper = "fdiv" if stmt.op == "/=" else "fmod"
            self._need_rt(helper)
            return [f"{p}{binding.rs} = newr_rt::{helper}({binding.rs}, "
                    f"{value});"]
        if _is_str(t) and stmt.op == "+=":
            return [f"{p}{binding.rs} += {value}.as_str();"]
        # float / str / %= : bare augmented assignment (no overflow
        # check applies; `+=` on a str is concatenation).
        return [f"{p}{binding.rs} {stmt.op} {value};"]

    def _emit_if(self, stmt: A.If, indent: int) -> list[str]:
        p = "  " * indent
        lines = [f"{p}if {self.emit_expr(stmt.cond, 0)} {{"]
        lines.extend(self._emit_block(stmt.then_body, indent + 1))
        for cond, body in stmt.elifs:
            lines.append(f"{p}}} else if {self.emit_expr(cond, 0)} {{")
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
            header = (f"for mut {binding.rs} in "
                      f"{iterable}.keys() {{")
        elif it_type.name == "str":
            # A string iterates code points (P5), not bytes.
            self._need_rt("chars")
            header = (f"for mut {binding.rs} in "
                      f"newr_rt::chars(&{iterable}) {{")
        else:
            # Lists/sets iterate by value, cloned out of the
            # iterable (the loop must not move it).
            header = (f"for mut {binding.rs} in "
                      f"{iterable}.iter().cloned() {{")
        lines = [f"{p}{header}"]
        lines.extend(self._emit_block(stmt.body, indent + 1,
                                      new_scope=False))
        lines.append(f"{p}}}")
        self._pop_scope()
        return lines

    def _try_has_control_flow(self, stmts: list) -> bool:
        """True when a statement list contains a `return`/`break`/
        `continue` outside any nested lambda/fn (those would unwind
        past a `catch_unwind` boundary - R4)."""
        for st in stmts:
            if isinstance(st, (A.Return, A.Break, A.Continue)):
                return True
            if isinstance(st, A.If):
                if self._try_has_control_flow(st.then_body):
                    return True
                for _c, b in st.elifs:
                    if self._try_has_control_flow(b):
                        return True
                if st.else_body is not None and \
                        self._try_has_control_flow(st.else_body):
                    return True
            elif isinstance(st, (A.For, A.While)):
                if self._try_has_control_flow(st.body):
                    return True
            elif isinstance(st, A.Match):
                for arm in st.arms:
                    if self._try_has_control_flow(arm.body):
                        return True
            elif isinstance(st, A.Try):
                if self._try_has_control_flow(st.body):
                    return True
                for _n, _s, b in st.catches:
                    if self._try_has_control_flow(b):
                        return True
                if st.finally_body is not None and \
                        self._try_has_control_flow(st.finally_body):
                    return True
        return False

    def _emit_try(self, stmt: A.Try, indent: int) -> list[str]:
        p = "  " * indent
        if not stmt.catches and stmt.finally_body is None:
            # No catch and no finally: the body just runs (a panic
            # propagates unchanged).
            return self._emit_block(stmt.body, indent)
        if self._try_has_control_flow(stmt.body):
            self._fail("`return` / `break` / `continue` inside a "
                       "`try` body is not supported in the Rust "
                       "emitter (it would unwind past the catch "
                       "boundary)")
        temp = self._new_temp_name()
        lines = [f"{p}let {temp} = std::panic::catch_unwind("
                 "std::panic::AssertUnwindSafe(|| {"]
        lines.extend(self._emit_block(stmt.body, indent + 1))
        lines.append(f"{p}}}));")
        if stmt.catches:
            if len(stmt.catches) > 1:
                self._fail("`try` with more than one `catch` is not "
                           "supported in v1 (the first would catch "
                           "everything)")
            cname, _span, cbody = stmt.catches[0]
            exc_var = self._new_temp_name()
            lines.append(f"{p}match {temp} {{")
            lines.append(f"{p}  Ok(()) => {{}}")
            lines.append(f"{p}  Err({exc_var}) => {{")
            self._push_scope()
            binding = self._bind(cname, type=Type("error"), kind="local")
            self._need_rt("as_error")
            lines.append(f"{p}    let {binding.rs}: "
                         f"newr_rt::NewrError = "
                         f"newr_rt::as_error({exc_var});")
            lines.extend(self._emit_block(cbody, indent + 2,
                                          new_scope=False))
            lines.append(f"{p}  }}")
            lines.append(f"{p}}}")
            self._pop_scope()
        if stmt.finally_body is not None:
            lines.extend(self._emit_block(stmt.finally_body, indent))
        if stmt.finally_body is not None and not stmt.catches:
            # Finally-only: re-throw after the finally body.
            lines.append(f"{p}if let Err(_e) = {temp} {{")
            lines.append(f"{p}  std::panic::resume_unwind(_e);")
            lines.append(f"{p}}}")
        return lines

    def _emit_throw(self, stmt: A.Throw, indent: int) -> list[str]:
        p = "  " * indent
        t = self._type_of(stmt.value)
        text = self.emit_expr(stmt.value, 0)
        if t.name == "error":
            self._need_rt("throw")
            return [f"{p}newr_rt::throw({text});"]
        if _is_str(t):
            self._need_rt("throw")
            self._need_rt("NewrError")
            return [f"{p}newr_rt::throw(newr_rt::NewrError::new({text}));"]
        self._fail(f"`throw` needs a `str` or an `error`, not `{t}`")

    def _emit_return(self, stmt: A.Return, indent: int) -> list[str]:
        p = "  " * indent
        ctx = self._fn_ctx[-1] if self._fn_ctx else None
        if ctx is None:
            # A bare top-level `return` exits the program (D11).
            if stmt.value is None:
                return [f"{p}std::process::exit(0);"]
            self._fail("a top-level `return` with a value is not "
                       "supported - a top-level `return` exits the "
                       "program")
        ensures = ctx["ensures"]
        ret_type = ctx["return_type"]
        is_void = ret_type is None or _is_none(ret_type)
        if ctx.get("in_handler"):
            # A handler is `async fn ... -> Result<(), Error>`: a
            # bare `return` is `return Ok(());`, and returning a
            # value from a handler has no meaning (the checker
            # should have rejected it).
            if stmt.value is None:
                return [f"{p}return Ok(());"]
            self._fail("cannot return a value from a command, event, "
                       "task, or component handler (handlers return "
                       "nothing)", span=stmt.span)
        if stmt.value is None:
            if not ensures:
                return [f"{p}return;"]
            default = self._rs_default(ret_type) \
                if ret_type is not None else "()"
            lines = [f"{p}_newr_result = {default};"]
            lines.extend(self._ensures_check_lines(ensures, indent))
            lines.append(f"{p}return;" if is_void
                         else f"{p}return _newr_result;")
            return lines
        if isinstance(stmt.value, A.Propagate):
            # A propagated error returns early; the `ensures` apply
            # only to normal (Ok) returns, not to propagated errors.
            unwrapped = self._propagate_unwrapped(stmt.value)
            return [f"{p}return {unwrapped};"]
        value = self._emit_value(stmt.value, ret_type,
                                 fn_target=ret_type if _is_fn(ret_type)
                                 else None)
        if not ensures:
            return [f"{p}return {value};"]
        lines = [f"{p}_newr_result = {value};"]
        lines.extend(self._ensures_check_lines(ensures, indent))
        lines.append(f"{p}return _newr_result;")
        return lines

    def _ensures_check_lines(self, ensures: list, indent: int) -> list[str]:
        """`if (!(<cond>)) throw NewrError("ensures failed: <cond>")`
        for each `ensures` clause (D5: explicit `throw`, never
        `assert`). `result` in the condition reads `_newr_result`
        (bound in the function scope when the function has
        `ensures`)."""
        p = "  " * indent
        lines: list[str] = []
        for clause in ensures:
            cond = self.emit_expr(clause.cond, 0)
            msg = cond.replace("_newr_result", "result")
            self._need_rt("NewrError")
            self._need_rt("throw")
            lines.append(f"{p}if !({cond}) {{")
            lines.append(
                f"{p}  newr_rt::throw(newr_rt::NewrError::new("
                f"{_rs_str('ensures failed: ' + msg)}));")
            lines.append(f"{p}}}")
        return lines

    # ------------------------------------------------------------ log statement

    def _emit_log_stmt(self, stmt: A.Log, indent: int) -> list[str]:
        """`log <expr>` is info level (P10): info -> stdout."""
        p = "  " * indent
        if isinstance(stmt.value, A.Propagate):
            value = self._propagate_unwrapped(stmt.value)
        else:
            value = self.emit_expr(stmt.value, 0)
        self._used_log = True
        t = self._type_or_none(stmt.value)
        if _is_str(t):
            return [f'{p}println!("{{}}", {value});']
        self._need_rt("to_str")
        return [f'{p}println!("{{}}", newr_rt::to_str(&{value}));']

    # ------------------------------------------------------------ match (D12)

    def _emit_match(self, stmt: A.Match, indent: int) -> list[str]:
        p = "  " * indent
        temp = self._new_match_name()
        scrut = self._owned(stmt.scrutinee)
        scrut_type = self._type_of(stmt.scrutinee)
        lines = [f"{p}let {temp}: {self._rs_type(scrut_type)} = "
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
                        rs=expr_text, type=typ, kind="local")
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
                lines.append(f"{p}if {cond} {{")
            else:
                lines.append(f"{p}}} else if {cond} {{")
            self._push_scope()
            for name, expr_text, typ in bindings:
                b = self._bind(name, type=typ, kind="local")
                # The binding's type is known from the pattern in
                # almost every case; when it is not (an imported
                # enum's field the emitter could not resolve), the
                # type is inferred from the field access.
                type_text = self._rs_type(typ) if typ is not None \
                    else "let"
                # A non-Copy binding clones its source (a place);
                # a ternary (OrPattern) is already a fresh value.
                value = expr_text
                if typ is not None and not self._is_copy_type(typ) \
                        and " ? " not in expr_text:
                    value = f"{expr_text}.clone()"
                if type_text == "let":
                    lines.append(f"{p}  let mut {b.rs} = {value};")
                else:
                    lines.append(f"{p}  let mut {b.rs}: {type_text} = "
                                 f"{value};")
            lines.extend(self._emit_block(arm.body, indent + 1,
                                          new_scope=False))
            self._pop_scope()
        if arms:
            lines.append(f"{p}}}")
        if not last_uncond:
            lines.append(f"{p}else {{")
            self._need_rt("NewrError")
            self._need_rt("throw")
            lines.append(
                f"{p}  newr_rt::throw(newr_rt::NewrError::new("
                f'"non-exhaustive match"));')
            lines.append(f"{p}}}")
        return lines

    def _pattern_condition(self, pattern, expr_text: str,
                           typ: Type | None):
        """Compile a pattern against the Rust expression `expr_text`
        (usually the match temp) and the newr type `typ` of the value
        being matched. Returns (condition_text_or_None, bindings)
        where bindings is a list of (newr name, Rust expression text,
        type). A `None` condition means the arm always matches
        (wildcard / name binding)."""
        if isinstance(pattern, A.WildcardPattern):
            return None, []
        if isinstance(pattern, A.NamePattern):
            return None, [(pattern.name, expr_text, typ)]
        if isinstance(pattern, A.LitPattern):
            v = pattern.value
            if v is None:
                if typ is not None and typ.nullable:
                    return f"{expr_text}.is_none()", []
                return "false", []
            if isinstance(v, bool):
                return f"{expr_text} == {'true' if v else 'false'}", []
            if isinstance(v, int):
                return f"{expr_text} == {v}", []
            if isinstance(v, float):
                return f"{expr_text} == {_rs_float(v)}", []
            return f"{expr_text}.as_str() == {_rs_str(v)}", []
        if isinstance(pattern, A.EnumPattern):
            # The enum is ONE struct; the variant is its discriminant
            # field `_newr_variant`. A nullable scrutinee reads
            # through `as_ref()` (the `none` pattern handles the
            # absent case).
            if typ is not None and typ.nullable:
                cond = (f"({expr_text}.as_ref().is_some_and(|_e| "
                        f"_e._newr_variant == {_rs_str(pattern.variant)}))")
                base = f"({expr_text}.as_ref().unwrap())"
            else:
                cond = (f"({expr_text}._newr_variant"
                        f" == {_rs_str(pattern.variant)})")
                base = expr_text
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
                        base, pattern.enum_name, fname,
                        fields.get(fname)),
                    fields.get(fname))
                if sub_cond:
                    cond += f" && {sub_cond}"
                bindings.extend(sub_bind)
            return cond, bindings
        if isinstance(pattern, A.StructPattern):
            struct_rs = self._struct_rs_name(pattern.struct_name)
            if typ is not None and typ.nullable:
                cond = f"({expr_text}.is_some())"
                base = f"({expr_text}.as_ref().unwrap())"
            else:
                # The scrutinee's type IS the struct (the checker
                # proves it); there is no `instanceof` in Rust.
                cond = None
                base = expr_text
            bindings = []
            struct = self._find_struct_decl(pattern.struct_name)
            fields = {}
            if struct is not None:
                fields = {f.name: f.type for f in struct.fields}
            for fname, sub, _span in pattern.fields:
                sub_cond, sub_bind = self._pattern_condition(
                    sub, f"{base}.{self._mangle(fname)}",
                    fields.get(fname))
                if sub_cond:
                    cond = f"{cond} && {sub_cond}" if cond else sub_cond
                bindings.extend(sub_bind)
            return cond, bindings
        if isinstance(pattern, A.ListPattern):
            if typ is not None and typ.nullable:
                cond = (f"({expr_text}.as_ref().is_some_and(|_v| "
                        f"_v.len() == {len(pattern.elements)}))")
                base = f"({expr_text}.as_ref().unwrap())"
            else:
                cond = f"({expr_text}.len() == {len(pattern.elements)})"
                base = expr_text
            bindings = []
            elem = typ.params[0] if typ is not None and typ.params else None
            for i, sub in enumerate(pattern.elements):
                sub_cond, sub_bind = self._pattern_condition(
                    sub, f"{base}[{i}]", elem)
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
                        text = (f"if {bcond} {{ {bexpr} }} else "
                                f"{{ {text} }}")
                    bindings.append((name, text, typ))
            return cond, bindings
        self._fail(f"cannot emit pattern {type(pattern).__name__}")

    # ------------------------------------------------------------ propagate (D7)

    def _propagate_unwrapped(self, prop: A.Propagate) -> str:
        """D7: `?` at the end of a statement RHS. Emits the temp
        assignment + the early return as hoisted lines before the
        statement, and returns the text of the unwrapped value.

        A `Result` operand returns the `Err` itself from a
        Result-returning function; a nullable operand returns `None`
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
        temp_type = self._rs_type(t)
        if ctx.get("in_handler"):
            # A handler returns `Result<(), Error>`, so `?` exits
            # the handler with `Ok(())` - there is no value to
            # propagate. (A handler that may exit early without
            # answering is the discord checker's E2010, not the
            # emitter's problem.)
            if t.name == "Result" and t.params and not t.nullable:
                self._hoists_add([
                    f"let {temp}: {temp_type} = {text};",
                    f"if {temp}.is_err() {{",
                    "  return Ok(());",
                    "}",
                ])
                return f"{temp}.unwrap()"
            if t.nullable and t.name != "Result":
                self._hoists_add([
                    f"let {temp}: {temp_type} = {text};",
                    f"if {temp}.is_none() {{",
                    "  return Ok(());",
                    "}",
                ])
                return f"{temp}.unwrap()"
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
                f"let {temp}: {temp_type} = {text};",
                f"if {temp}.is_err() {{",
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
                f"let {temp}: {temp_type} = {text};",
                f"if {temp}.is_none() {{",
                "  return None;",
                "}",
            ])
            return f"{temp}.unwrap()"
        self._fail("`?` needs a `Result[T, E]` or a `T?` value")

    # ------------------------------------------------------------ module calls

    def _emit_module_call(self, node: A.Call,
                          binding: _Binding) -> tuple[str, int]:
        if binding.kind == "file-module":
            # A function in an imported .newr file: a plain call on
            # the imported module - and the import itself (R2: one
            # `.rs` per imported `.newr`, named by its module
            # name). Struct construction through a file module needs
            # the struct literal (a Rust struct is constructed
            # with `{ ... }`, not `new`).
            ref = self._file_module_mod(binding)
            name = node.func.name
            sub = self.program.modules.get(binding.rs)
            if sub is not None:
                for s in sub.structs:
                    if s.name == name:
                        order = [(f.name, f.type) for f in s.fields]
                        values: dict[str, str] = {}
                        for a, (fname, ftype) in zip(node.args, order):
                            values[fname] = self._emit_value(a, ftype)
                        for kname, value, _span in node.kwargs:
                            ftype = next(ft for fn_, ft in order
                                         if fn_ == kname)
                            values[kname] = self._emit_value(value, ftype)
                        parts = [f"{self._mangle(f)}: {values[f]}"
                                 for f, _t in order if f in values]
                        return (f"{ref}::{self._mangle(name)} {{ "
                                f"{', '.join(parts)} }}",
                                _PREC["primary"])
            decl = next((f for f in sub.functions
                         if f.name == name), None) if sub is not None \
                else None
            params = [p.type for p in decl.params] \
                if decl is not None else []
            args: list[str] = []
            for i, a in enumerate(node.args):
                if i < len(params):
                    p = params[i]
                    args.append(self._emit_value(
                        a, p, fn_target=p if _is_fn(p) else None))
                else:
                    args.append(self.emit_expr(a, 0))
            kwargs = self._emit_module_kwargs(node, sub)
            return (f"{ref}::{self._mangle(name)}"
                    f"({', '.join(args + kwargs)})", _PREC["primary"])
        real = binding.module or binding.rs
        if real == "random" and node.func.name == "shuffle":
            # A shuffle on a global collection must go through
            # `gmut` (a `gget` clone would lose the shuffle).
            root = self._global_root(node.args[0])
            arg = self.emit_expr(node.args[0], 0)
            self._need_rt("random_shuffle")
            if root is not None:
                self._need_rt("gmut")
                return (f"newr_rt::gmut(&{root.rs}, |_v| {{ "
                        f"newr_rt::random_shuffle(_v); }})",
                        _PREC["primary"])
            return (f"newr_rt::random_shuffle(&mut {arg})",
                    _PREC["primary"])
        if real == "file":
            name = node.func.name
            if name in ("read", "write", "append"):
                self._need_rt(f"file_{name}")
                args = [self.emit_expr(a, 0) for a in node.args]
                return (f"newr_rt::file_{name}(&{args[0]}, "
                        f"&{args[1]})" if len(args) == 2
                        else f"newr_rt::file_{name}(&{args[0]})",
                        _PREC["primary"])
            if name == "exists":
                self._need_rt("file_exists")
                return (f"newr_rt::file_exists"
                        f"(&{self.emit_expr(node.args[0], 0)})",
                        _PREC["primary"])
            self._fail(f"module `file` has no function `{name}`")
        if real == "db":
            args = [self.emit_expr(a, 0) for a in node.args]
            return self._emit_db_call(node, args)
        args = [self.emit_expr(a, 0) for a in node.args]
        return self._emit_module_call_text(real, binding.rs,
                                           node.func.name, args)

    def _emit_module_call_text(self, real: str, ref: str, name: str,
                               args: list[str]) -> tuple[str, int]:
        """The Rust call text for a named-module function. `real` is
        the module name (for dispatch); `ref` is the Rust reference
        (the alias, when the import is aliased)."""
        if real == "math":
            if name == "sqrt":
                return f"({args[0]} as f64).sqrt()", _PREC["primary"]
            if name == "floor":
                self._need_rt("to_int_from_float")
                return (f"newr_rt::to_int_from_float"
                        f"(({args[0]} as f64).floor())", _PREC["primary"])
            if name == "ceil":
                self._need_rt("to_int_from_float")
                return (f"newr_rt::to_int_from_float"
                        f"(({args[0]} as f64).ceil())", _PREC["primary"])
        if real == "time":
            if name == "now":
                self._need_rt("time_now")
                return "newr_rt::time_now()", _PREC["primary"]
            if name == "format":
                self._need_rt("time_format")
                return (f"newr_rt::time_format({args[0]}, &{args[1]})",
                        _PREC["primary"])
        if real == "random":
            if name == "int":
                self._need_rt("random_int")
                return (f"newr_rt::random_int({args[0]}, {args[1]})",
                        _PREC["primary"])
            if name == "choice":
                self._need_rt("random_choice")
                return (f"newr_rt::random_choice(&{args[0]})",
                        _PREC["primary"])
            if name == "shuffle":
                self._need_rt("random_shuffle")
                return (f"newr_rt::random_shuffle(&mut {args[0]})",
                        _PREC["primary"])
        if real == "env":
            if name == "get":
                if len(args) == 2:
                    self._need_rt("env_get_or")
                    return (f"newr_rt::env_get_or(&{args[0]}, "
                            f"&{args[1]})", _PREC["primary"])
                self._need_rt("env_get")
                return f"newr_rt::env_get(&{args[0]})", _PREC["primary"]
        self._fail(f"module `{real}` has no function `{name}`")

    def _emit_db_call(self, node: A.Call,
                      args: list[str]) -> tuple[str, int]:
        """`db.*` calls (SPEC.md section 12.5): the helpers live in
        the `newr_db` module emitted into this file (see
        `_emit_db_mod`) — inline, not in newr_rt.rs, because the
        runtime file is std-only and a program that never uses `db`
        must not need rusqlite or cargo. Handles are i64, fd-style.
        Bind args are passed as `&dyn ToSql` (rusqlite binds a
        bool as 0/1 itself, and `Option<T>` as NULL). The checker
        (db_check) has already validated the call, so the arity
        here is trusted. `db.query` maps each converted row to the
        row struct through `db_query_as`."""
        name = node.func.name
        self._need_sqlite()
        self._need_rt("to_str")
        self._file_uses_db = True
        if name == "open":
            return f"newr_db::db_open(&{args[0]})", _PREC["primary"]
        if name == "exec":
            binds = ", ".join(
                f"(&{a} as &dyn rusqlite::types::ToSql)"
                for a in args[2:])
            return (f"newr_db::db_exec({args[0]}, &{args[1]}, "
                    f"&[{binds}])", _PREC["primary"])
        if name == "query":
            rs_struct, decl = self._db_row_struct(node)
            names = [_rs_str(f.name) for f in decl.fields]
            types = [_rs_str(self._db_field_type(f.type))
                     for f in decl.fields]
            fields = ", ".join(
                f"{self._mangle(f.name)}: "
                f"{self._db_accessor(f.type)}(&row[{i}])"
                for i, f in enumerate(decl.fields))
            names_lit = "&[" + ", ".join(names) + "]"
            types_lit = "&[" + ", ".join(types) + "]"
            binds = ", ".join(
                f"(&{a} as &dyn rusqlite::types::ToSql)"
                for a in args[2:])
            return (f"newr_db::db_query_as::<{rs_struct}>({args[0]}, "
                    f"&{args[1]}, &[{binds}], {names_lit}, {types_lit}, "
                    f"|row| {rs_struct} {{ {fields} }})",
                    _PREC["primary"])
        if name == "begin":
            return f"newr_db::db_begin({args[0]})", _PREC["primary"]
        if name == "commit":
            return f"newr_db::db_commit({args[0]})", _PREC["primary"]
        if name == "rollback":
            return f"newr_db::db_rollback({args[0]})", _PREC["primary"]
        if name == "last_id":
            return f"newr_db::db_last_id({args[0]})", _PREC["primary"]
        if name == "close":
            return f"newr_db::db_close({args[0]})", _PREC["primary"]
        self._fail(f"module `db` has no function `{name}`")

    def _db_row_struct(self, node: A.Call) -> tuple[str, A.StructDecl]:
        """The (Rust constructor, decl) of the row struct for a
        `db.query` call. The row type comes from `node.expr_type`,
        set by db_check to `list[Row]`. A struct from an imported
        file module resolves to its qualified name."""
        t = self._type_of(node)
        if t.name != "list" or not t.params:
            self._fail("internal: db.query without a list[Row] type")
        row_name = t.params[0].name
        decl = self.structs.get(row_name)
        if decl is not None:
            return self._struct_rs_name(row_name), decl
        for bound, sub in self.program.modules.items():
            for s in sub.structs:
                if s.name == row_name:
                    return self._struct_rs_name(row_name), s
        self._fail(f"db.query: row struct `{row_name}` not found")

    @staticmethod
    def _db_field_type(t: Type) -> str:
        """The runtime type string for a row field: the base name,
        with `?` when nullable. Row fields are only int/float/str/
        bool (db_check enforces it), so no deeper shape is needed."""
        return t.name + ("?" if t.nullable else "")

    @staticmethod
    def _db_accessor(t: Type) -> str:
        """The `newr_db` accessor for a row field type: the plain
        one for a non-nullable field, the `_opt` one (returning
        `Option<...>`) for a nullable field."""
        base = {"int": "as_int", "float": "as_float",
                "str": "as_str", "bool": "as_bool"}[t.name]
        return (f"newr_db::{base}_opt" if t.nullable
                else f"newr_db::{base}")

    def _emit_db_mod(self) -> list[str]:
        """The `newr_db` helper module, emitted into a file that
        uses `db.*` (at top level, before `fn main`). The int
        handle registry (one `LazyLock<Mutex<DbState>>`, the
        program is single-threaded but the registry is global)
        plus open/exec/query/begin/commit/rollback/last_id/close
        over rusqlite (the store's library, 0.41 — the same sync
        API the store uses). Conversion mirrors the Python
        `_db_value` (newr_rt.py): NULL lands only in a nullable
        field, `?` binds are positional (the Nth argument binds
        the Nth `?` — `params_from_iter` over `&dyn ToSql`),
        bool binds as 0/1, a value that cannot convert panics
        instead of silently producing a wrong value. `db_query`
        looks rows up by column NAME (a missing column panics,
        extra columns are ignored). Transactions are raw SQL
        (`BEGIN IMMEDIATE` / `COMMIT` / `ROLLBACK` through
        `execute_batch`, exactly like the Python runtime), so no
        `&mut` borrow of the connection is ever needed."""
        return [
            "#[allow(dead_code)]",
            "mod newr_db {",
            "  use std::collections::HashMap;",
            "  use std::sync::Mutex;",
            "",
            "  struct DbState {",
            "    conns: HashMap<i64, rusqlite::Connection>,",
            "    next_id: i64,",
            "  }",
            "",
            "  static DB: std::sync::LazyLock<Mutex<DbState>> =",
            "    std::sync::LazyLock::new(|| Mutex::new(DbState { conns: HashMap::new(), next_id: 1 }));",
            "",
            "  fn with_conn<T>(handle: i64, f: impl FnOnce(&rusqlite::Connection) -> T) -> T {",
            "    let db = DB.lock().unwrap();",
            "    let conn = db.conns.get(&handle).unwrap_or_else(|| panic!(\"db: unknown or closed handle {}\", handle));",
            "    f(conn)",
            "  }",
            "",
            "  fn db_value(value: rusqlite::types::Value, type_str: &str) -> rusqlite::types::Value {",
            "    let nullable = type_str.ends_with('?');",
            "    let base = if nullable { &type_str[..type_str.len() - 1] } else { type_str };",
            "    if value == rusqlite::types::Value::Null {",
            "      if nullable {",
            "        return rusqlite::types::Value::Null;",
            "      }",
            "      panic!(\"db: NULL in non-nullable {} field\", base);",
            "    }",
            "    match base {",
            "      \"int\" => match value {",
            "        rusqlite::types::Value::Integer(i) => rusqlite::types::Value::Integer(i),",
            "        rusqlite::types::Value::Real(r) => {",
            "          if r != r.floor() || r < -9223372036854775808.0 || r > 9223372036854775807.0 {",
            "            panic!(\"db: cannot convert {} to int\", r);",
            "          }",
            "          rusqlite::types::Value::Integer(r as i64)",
            "        }",
            "        rusqlite::types::Value::Text(s) => match s.trim().parse::<i64>() {",
            "          Ok(i) => rusqlite::types::Value::Integer(i),",
            "          Err(_) => panic!(\"db: cannot convert '{}' to int\", s),",
            "        },",
            "        _ => panic!(\"db: cannot convert value to int\"),",
            "      },",
            "      \"float\" => match value {",
            "        rusqlite::types::Value::Integer(i) => rusqlite::types::Value::Real(i as f64),",
            "        rusqlite::types::Value::Real(r) => rusqlite::types::Value::Real(r),",
            "        rusqlite::types::Value::Text(s) => match s.trim().parse::<f64>() {",
            "          Ok(f) => rusqlite::types::Value::Real(f),",
            "          Err(_) => panic!(\"db: cannot convert '{}' to float\", s),",
            "        },",
            "        _ => panic!(\"db: cannot convert value to float\"),",
            "      },",
            "      \"str\" => match value {",
            "        rusqlite::types::Value::Text(s) => rusqlite::types::Value::Text(s),",
            "        rusqlite::types::Value::Integer(i) => rusqlite::types::Value::Text(i.to_string()),",
            "        rusqlite::types::Value::Real(r) => rusqlite::types::Value::Text(crate::newr_rt::to_str(&r)),",
            "        rusqlite::types::Value::Blob(b) => rusqlite::types::Value::Text(String::from_utf8_lossy(&b).into_owned()),",
            "        _ => panic!(\"db: cannot convert value to str\"),",
            "      },",
            "      \"bool\" => match value {",
            "        rusqlite::types::Value::Integer(i) => {",
            "          if i == 0 {",
            "            rusqlite::types::Value::Integer(0)",
            "          } else if i == 1 {",
            "            rusqlite::types::Value::Integer(1)",
            "          } else {",
            "            panic!(\"db: cannot convert {} to bool\", i);",
            "          }",
            "        }",
            "        rusqlite::types::Value::Real(r) => {",
            "          let w = r as i64;",
            "          if w == 0 {",
            "            rusqlite::types::Value::Integer(0)",
            "          } else if w == 1 {",
            "            rusqlite::types::Value::Integer(1)",
            "          } else {",
            "            panic!(\"db: cannot convert {} to bool\", r);",
            "          }",
            "        }",
            "        rusqlite::types::Value::Text(s) => {",
            "          if s == \"0\" {",
            "            rusqlite::types::Value::Integer(0)",
            "          } else if s == \"1\" {",
            "            rusqlite::types::Value::Integer(1)",
            "          } else {",
            "            panic!(\"db: cannot convert '{}' to bool\", s);",
            "          }",
            "        }",
            "        _ => panic!(\"db: cannot convert value to bool\"),",
            "      },",
            "      _ => panic!(\"db: unknown field type {}\", type_str),",
            "    }",
            "  }",
            "",
            "  pub fn as_int(v: &rusqlite::types::Value) -> i64 {",
            "    match v {",
            "      rusqlite::types::Value::Integer(i) => *i,",
            "      _ => panic!(\"db: internal: expected int value\"),",
            "    }",
            "  }",
            "",
            "  pub fn as_float(v: &rusqlite::types::Value) -> f64 {",
            "    match v {",
            "      rusqlite::types::Value::Real(r) => *r,",
            "      _ => panic!(\"db: internal: expected float value\"),",
            "    }",
            "  }",
            "",
            "  pub fn as_str(v: &rusqlite::types::Value) -> String {",
            "    match v {",
            "      rusqlite::types::Value::Text(s) => s.clone(),",
            "      _ => panic!(\"db: internal: expected str value\"),",
            "    }",
            "  }",
            "",
            "  pub fn as_bool(v: &rusqlite::types::Value) -> bool {",
            "    match v {",
            "      rusqlite::types::Value::Integer(i) => *i != 0,",
            "      _ => panic!(\"db: internal: expected bool value\"),",
            "    }",
            "  }",
            "",
            "  pub fn as_int_opt(v: &rusqlite::types::Value) -> Option<i64> {",
            "    match v {",
            "      rusqlite::types::Value::Null => None,",
            "      rusqlite::types::Value::Integer(i) => Some(*i),",
            "      _ => panic!(\"db: internal: expected int value\"),",
            "    }",
            "  }",
            "",
            "  pub fn as_float_opt(v: &rusqlite::types::Value) -> Option<f64> {",
            "    match v {",
            "      rusqlite::types::Value::Null => None,",
            "      rusqlite::types::Value::Real(r) => Some(*r),",
            "      _ => panic!(\"db: internal: expected float value\"),",
            "    }",
            "  }",
            "",
            "  pub fn as_str_opt(v: &rusqlite::types::Value) -> Option<String> {",
            "    match v {",
            "      rusqlite::types::Value::Null => None,",
            "      rusqlite::types::Value::Text(s) => Some(s.clone()),",
            "      _ => panic!(\"db: internal: expected str value\"),",
            "    }",
            "  }",
            "",
            "  pub fn as_bool_opt(v: &rusqlite::types::Value) -> Option<bool> {",
            "    match v {",
            "      rusqlite::types::Value::Null => None,",
            "      rusqlite::types::Value::Integer(i) => Some(*i != 0),",
            "      _ => panic!(\"db: internal: expected bool value\"),",
            "    }",
            "  }",
            "",
            "  pub fn db_open(path: &str) -> i64 {",
            "    let conn = rusqlite::Connection::open(path).unwrap_or_else(|e| panic!(\"db: {}\", e));",
            "    conn.execute_batch(\"PRAGMA foreign_keys = ON; PRAGMA busy_timeout = 5000;\").unwrap_or_else(|e| panic!(\"db: {}\", e));",
            "    let mut db = DB.lock().unwrap();",
            "    let handle = db.next_id;",
            "    db.next_id += 1;",
            "    db.conns.insert(handle, conn);",
            "    handle",
            "  }",
            "",
            "  pub fn db_exec(handle: i64, sql: &str, args: &[&dyn rusqlite::types::ToSql]) -> i64 {",
            "    with_conn(handle, |conn| {",
            "      let n = conn.execute(sql, rusqlite::params_from_iter(args.iter().copied())).unwrap_or_else(|e| panic!(\"db: {}\", e));",
            "      n as i64",
            "    })",
            "  }",
            "",
            "  pub fn db_query(handle: i64, sql: &str, args: &[&dyn rusqlite::types::ToSql], names: &[&str], types: &[&str]) -> Vec<Vec<rusqlite::types::Value>> {",
            "    with_conn(handle, |conn| {",
            "      let mut stmt = conn.prepare(sql).unwrap_or_else(|e| panic!(\"db: {}\", e));",
            "      let cols: Vec<String> = stmt.column_names().map(|s| s.to_string()).collect();",
            "      let mut rows = stmt.query(rusqlite::params_from_iter(args.iter().copied())).unwrap_or_else(|e| panic!(\"db: {}\", e));",
            "      let mut out: Vec<Vec<rusqlite::types::Value>> = Vec::new();",
            "      while let Some(row) = rows.next().unwrap_or_else(|e| panic!(\"db: {}\", e)) {",
            "        let mut by_name: HashMap<String, rusqlite::types::Value> = HashMap::new();",
            "        for (i, name) in cols.iter().enumerate() {",
            "          by_name.insert(name.clone(), row.get::<_, rusqlite::types::Value>(i).unwrap_or_else(|e| panic!(\"db: {}\", e)));",
            "        }",
            "        let mut out_row: Vec<rusqlite::types::Value> = Vec::new();",
            "        for (name, type_str) in names.iter().zip(types.iter()) {",
            "          let raw = by_name.get(*name).unwrap_or_else(|| panic!(\"db: column '{}' missing from query result\", name));",
            "          out_row.push(db_value(raw.clone(), type_str));",
            "        }",
            "        out.push(out_row);",
            "      }",
            "      out",
            "    })",
            "  }",
            "",
            "  pub fn db_query_as<T>(handle: i64, sql: &str, args: &[&dyn rusqlite::types::ToSql], names: &[&str], types: &[&str], map: impl Fn(Vec<rusqlite::types::Value>) -> T) -> Vec<T> {",
            "    db_query(handle, sql, args, names, types).into_iter().map(map).collect()",
            "  }",
            "",
            "  pub fn db_begin(handle: i64) {",
            "    with_conn(handle, |conn| {",
            "      conn.execute_batch(\"BEGIN IMMEDIATE\").unwrap_or_else(|e| panic!(\"db: {}\", e));",
            "    });",
            "  }",
            "",
            "  pub fn db_commit(handle: i64) {",
            "    with_conn(handle, |conn| {",
            "      conn.execute_batch(\"COMMIT\").unwrap_or_else(|e| panic!(\"db: {}\", e));",
            "    });",
            "  }",
            "",
            "  pub fn db_rollback(handle: i64) {",
            "    with_conn(handle, |conn| {",
            "      conn.execute_batch(\"ROLLBACK\").unwrap_or_else(|e| panic!(\"db: {}\", e));",
            "    });",
            "  }",
            "",
            "  pub fn db_last_id(handle: i64) -> i64 {",
            "    with_conn(handle, |conn| conn.last_insert_rowid())",
            "  }",
            "",
            "  pub fn db_close(handle: i64) {",
            "    let mut db = DB.lock().unwrap();",
            "    if db.conns.remove(&handle).is_none() {",
            "      panic!(\"db: unknown or closed handle {}\", handle);",
            "    }",
            "  }",
            "}",
        ]

    def _emit_module_member(self, node: A.Member,
                            binding: _Binding) -> tuple[str, int]:
        """A named-module member used as a value: a constant, or a
        function (which becomes a closure, D29). `binding.module` is
        the real module name (for dispatch); `binding.rs` is the
        Rust reference (the alias, when the import is aliased) -
        except for a file module, whose module path is the file stem
        (`_file_module_mod`)."""
        real = binding.module or binding.rs
        ref = self._file_module_mod(binding)
        t = self._type_of(node)
        if t.name == "fn":
            if binding.kind == "file-module":
                # A function in an imported .newr file, used as a
                # value: an `Arc` over the fn pointer.
                cast_text = self._rs_fn_type(t)
                ptr = self._rs_fn_ptr_type(t)
                return (f"(std::sync::Arc::new({ref}::"
                        f"{self._mangle(node.name)} as {ptr}) "
                        f"as {cast_text})", _PREC["primary"])
            # A module function as a first-class value.
            if real == "file" and node.name in ("read", "write", "append"):
                self._fail(f"`file.{node.name}` cannot be used as a "
                           "value (it needs a statement - the `with` "
                           "block cannot live inside a closure)")
            params = t.params[:-1]  # the last param is the return type
            names = [f"_a{i}" for i in range(len(params))]
            call, _prec = self._emit_module_call_text(
                real, ref, node.name, names)
            cast_text = self._rs_fn_type(t)
            if names:
                return (f"(std::sync::Arc::new(move "
                        f"({', '.join(names)}) -> {call}) "
                        f"as {cast_text})", _PREC["primary"])
            return (f"(std::sync::Arc::new(move () -> {call}) "
                    f"as {cast_text})", _PREC["primary"])
        if real == "env" and node.name == "args":
            self._need_rt("env_args")
            return "newr_rt::env_args()", _PREC["primary"]
        if real == "math" and node.name in ("pi", "e"):
            return ("std::f64::consts::PI" if node.name == "pi"
                    else "std::f64::consts::E", _PREC["primary"])
        self._fail(f"module `{real}` has no value member `{node.name}`")

    # ------------------------------------------------------------ store calls

    def _store_key_expr(self, arg) -> str:
        """The key expression for a store call: an entity (user /
        member) contributes its id string (the key column is a
        str); a str is used as-is."""
        t = self._type_of(arg)
        text = self.emit_expr(arg, 0)
        if t.name in ("user", "member"):
            return f"{text}.id.get().to_string()"
        if t.name == "str":
            return text
        return f"newr_rt::to_str(&{text})"

    def _emit_store_call(self, node: A.Call,
                         binding: _Binding) -> tuple[str, int]:
        """`economy.balance(user)` etc. -> `bot.<store>_<op>(...)`
        (D17/P11). Every mutation takes its idempotency `key:`; the
        checker enforces that it is present. The §19 guard still
        applies: the `bot` instance only exists inside a handler,
        so a store call at the top level, inside a (sync) top-level
        fn, or inside a lambda is a clean E4001 instead of
        uncompilable code."""
        # The Bot method is `bot.<store>_<op>` with the RAW store
        # name (the spelling the Bot struct's methods use).
        # binding.rs is the MANGLED name ("economy" is
        # emitter-reserved, so it would be "economy_") - using it
        # here would call a method that does not exist.
        store_name = (node.func.obj.name
                      if isinstance(node.func, A.Member)
                      and isinstance(node.func.obj, A.Name)
                      else binding.rs)
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
        if method in ("credit", "debit", "transfer") and key is None:
            # The checker enforces `key:` on credit/debit/transfer
            # (SPEC §14); without it the emitted call would carry
            # a literal `None` into Rust. Fail cleanly instead.
            self._fail(
                f"`{store_name}.{method}` needs an idempotency "
                "`key:` argument (the checker should have rejected "
                "this)", span=node.span)
        if method == "balance":
            return (f"bot.{store_name}_balance("
                    f"&({self._store_key_expr(node.args[0])}))",
                    _PREC["primary"])
        if method == "credit":
            return (f"bot.{store_name}_credit("
                    f"&{self._store_key_expr(node.args[0])}, "
                    f"{self.emit_expr(node.args[1], 0)}, &({key}))",
                    _PREC["primary"])
        if method == "debit":
            return (f"bot.{store_name}_debit("
                    f"&{self._store_key_expr(node.args[0])}, "
                    f"{self.emit_expr(node.args[1], 0)}, &({key}))",
                    _PREC["primary"])
        if method == "transfer":
            return (f"bot.{store_name}_transfer("
                    f"&{self._store_key_expr(node.args[0])}, "
                    f"&({self._store_key_expr(node.args[1])}), "
                    f"{self.emit_expr(node.args[2], 0)}, &({key}))",
                    _PREC["primary"])
        if method == "get":
            table = node.args[0]
            if not isinstance(table, A.Lit) or not isinstance(table.value, str):
                self._fail("`get`'s table must be a string literal "
                           "naming a declared table")
            # Rust has no runtime cast, so the table is known at
            # emit time and the Bot method is per-table
            # (`economy_get_balances(key) -> Option<balances>`).
            # The key argument's form follows the key column's
            # Rust type (`&str` for a str key, the value itself
            # for int/float/bool keys).
            binding = self._lookup(table.value)
            if binding is None or binding.kind != "struct":
                self._fail(f"unknown table `{table.value}`")
            row = self._table_rows.get(table.value)
            key_arg_text = self._store_table_key_arg(
                node.args[1], row)
            return (f"bot.{store_name}_get_{binding.rs}("
                    f"{key_arg_text})",
                    _PREC["primary"])
        if method == "set":
            table = node.args[0]
            if not isinstance(table, A.Lit) or not isinstance(table.value, str):
                self._fail("`set`'s table must be a string literal "
                           "naming a declared table")
            binding = self._lookup(table.value)
            if binding is None or binding.kind != "struct":
                self._fail(f"unknown table `{table.value}`")
            return (f"bot.{store_name}_set_{binding.rs}("
                    f"{self.emit_expr(node.args[1], 0)})",
                    _PREC["primary"])
        if method == "delete":
            table = node.args[0]
            if not isinstance(table, A.Lit) or not isinstance(table.value, str):
                self._fail("`delete`'s table must be a string literal "
                           "naming a declared table")
            binding = self._lookup(table.value)
            if binding is None or binding.kind != "struct":
                self._fail(f"unknown table `{table.value}`")
            row = self._table_rows.get(table.value)
            key_arg_text = self._store_table_key_arg(
                node.args[1], row)
            return (f"bot.{store_name}_delete_{binding.rs}("
                    f"{key_arg_text})",
                    _PREC["primary"])
        self._fail(f"store `{store_name}` has no operation `{method}`")

    def _store_table_key_arg(self, key_expr: A.Expr,
                             row: A.StructDecl | None) -> str:
        """The key argument for a per-table `get`/`delete`: `&<expr>`
        when the table's key column is a str, the expression itself
        otherwise (an int/float/bool key is passed by value)."""
        text = self.emit_expr(key_expr, 0)
        if row is None or not row.fields:
            return text
        if _is_str(row.fields[0].type):
            return f"&({text})"
        return text

    # ------------------------------------------------------------ t() and embeds

    def _emit_t_call(self, node: A.TCall) -> tuple[str, int]:
        """`t("key", name: expr, ...)` -> `_t("key", <locale>, {...})`
        (D16). Template placeholders that are handler bindings
        (user, guild, ...) are auto-passed in their interpolation
        form; the rest must be explicit arguments (the checker
        enforces it - E2009). The kwargs map is built with
        `dict_of`."""
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
            vt = self._type_or_none(value)
            vtext = self.emit_expr(value, 0)
            if not _is_str(vt):
                self._need_rt("to_str")
                vtext = f"newr_rt::to_str(&{vtext})"
            # `dict_of` takes `Vec<(String, V)>`: the key is a
            # `String`, not a `&str` literal.
            kwargs.append(f"{_rs_str(name)}.to_string(), {vtext}")
        for placeholder in entry.placeholders:
            if placeholder in explicit:
                continue
            binding = self._lookup(placeholder)
            if binding is not None and binding.kind == "handler":
                kwargs.append(f"{_rs_str(placeholder)}.to_string(), "
                              f"{binding.interp_form()}")
                continue
            self._fail(f"`t(\"{node.key}\")` needs an argument for "
                       f"placeholder `{placeholder}` (the checker "
                       "should have rejected it)")
        self._need_rt("dict_of")
        args = ", ".join([_rs_str(node.key), locale,
                          "newr_rt::dict_of(vec!["
                          + ", ".join(f"({k})" for k in kwargs) + "])"])
        return f"_t({args})", _PREC["primary"]

    def _emit_embed(self, node: A.EmbedExpr) -> tuple[str, int]:
        # An embed as a value (SPEC §13.7): a `CreateEmbed` built
        # in a block expression.
        lines, name = self._emit_embed_lines(node, 0)
        block = "{\n" + "\n".join(lines) + f"\n{name}\n}}"
        return block, _PREC["primary"]

    def _emit_embed_lines(self, node: A.EmbedExpr, indent: int) -> tuple[list[str], str]:
        p = "  " * indent
        name = self._new_temp_name()
        self._need_discord("serenity::builder::CreateEmbed")
        lines = [f"{p}let mut {name} = "
                 "serenity::builder::CreateEmbed::default();"]
        for field in node.fields:
            if field.kind == "title":
                lines.append(f"{p}{name}.title("
                             f"{self.emit_expr(field.value, 0)});")
            elif field.kind == "description":
                lines.append(f"{p}{name}.description("
                             f"{self.emit_expr(field.value, 0)});")
            elif field.kind == "color":
                lines.append(f"{p}{name}.colour("
                             f"{self.emit_expr(field.value, 0)} as u32);")
            elif field.kind == "field":
                inline = "true" if field.inline else "false"
                # A field's name is a plain string (not an expression).
                lines.append(f"{p}{name}.field("
                             f"{_rs_str(field.name)}, "
                             f"{self.emit_expr(field.value, 0)}, "
                             f"{inline});")
        return lines, name

    # ------------------------------------------------------------ bot (M5)
    # serenity 0.12.5 + poise 0.7.1 (SPEC §18), rusqlite 0.41.1.
    # The bot file is one Rust file per .newr file: `pub struct Bot`
    # (one rusqlite connection per store) + `pub struct Data`
    # (shared state for poise) + one `#[poise::command]` async fn
    # per slash command + component handler fns + task fns + a
    # `#[tokio::main] async fn main()` that builds the poise
    # framework. Replies go through the interaction directly (the
    # Java emitter's hook approach, Rust-spelled): an interaction
    # reply is a `CreateInteractionResponseMessage` + `create_response`,
    # an edit is `EditInteractionResponse` + `edit_response`, a
    # followup is `CreateInteractionResponseFollowup` +
    # `create_followup`, a channel send is `ChannelId::send_message`.

    # -- handler context ------------------------------------------

    def _handler_ctx(self) -> dict:
        """The handler context on top of the stack ({} outside a
        handler). Keys: "kind" (command / component / modal /
        member_join / member_remove / message / ready / task /
        timeout), "reply" (how a `reply` is answered: interaction /
        system_channel / message_channel / timeout / none),
        "http" (the CacheHttp expression), "http_owned" (the
        `Arc<Http>` expression, for spawns), "cache" (the cache
        expression, or None), "locale" (the `t()` locale
        expression), "data" (the `Data` expression), "author_id"
        (the author's id as a String, or None), "interaction"
        (the interaction expression, or None), "channel" (the
        ChannelId expression for channel replies), "msgvar" (the
        message variable for a timeout reply)."""
        return self._handler_ctx_stack[-1] \
            if self._handler_ctx_stack else {}

    def _push_handler(self, ctx: dict) -> None:
        """Push a handler context: a fresh scope (handler bindings
        are inserted directly, Java-style), a function context
        marked `in_handler` (handlers return `Result<(), Error>`,
        so `?` and bare `return` exit with `Ok(())`), and the
        handler context itself."""
        self._push_scope()
        self._fn_ctx.append({"return_type": None, "ensures": [],
                             "in_handler": True})
        self._handler_ctx_stack.append(ctx)

    def _pop_handler(self) -> None:
        self._handler_ctx_stack.pop()
        self._fn_ctx.pop()
        self._pop_scope()

    def _handler_binding(self, name: str, rs: str, type: Type,
                         interp: str | None = None,
                         guild_id_expr: str | None = None,
                         guild_id_optional: bool = False,
                         channel_id_expr: str | None = None) -> None:
        """Insert a handler binding directly into the current scope
        (bypassing `_bind`, as Java does): `user`, `guild`,
        `channel`, `bot`, `interaction`, `message`, `values`, a
        modal field, or a command option."""
        self._scopes[-1][name] = _Binding(
            rs=rs, type=type, kind="handler", interp=interp,
            guild_id_expr=guild_id_expr,
            guild_id_optional=guild_id_optional,
            channel_id_expr=channel_id_expr)

    # -- cache-resolved entity fields -----------------------------

    def _guild_expect_expr(self, guild_id_expr: str, cache: str,
                           optional: bool) -> str:
        """The owned `Guild` for a guild id: a cache lookup that
        panics with a clear message when the guild is absent (a DM,
        or a cold cache) - the documented edge for a bare `guild`
        value and for `member.guild` / `interaction.guild`
        (R-guild)."""
        gid = guild_id_expr if optional else f"Some({guild_id_expr})"
        return (f"{gid}.and_then(|id| {cache}.guild(id))"
                f".map(|g| (*g).clone())"
                f".expect(\"guild is not available "
                f"(DM or not cached)\")")

    def _guild_name_expr(self, guild_id_expr: str, cache: str,
                         optional: bool, dm_fallback: str) -> str:
        """The guild's name through the cache, or `dm_fallback`
        when there is no guild (a DM) or the cache is cold - the
        interpolation form of `guild`."""
        gid = guild_id_expr if optional else f"Some({guild_id_expr})"
        return (f"{gid}.and_then(|id| {cache}.guild(id))"
                f".map(|g| g.name.clone())"
                f".unwrap_or_else(|| {dm_fallback})")

    def _emit_handler_entity_field(self, binding: _Binding,
                                   obj_type: Type,
                                   field: str) -> str | None:
        """Cache-resolved entity fields (M5): `member.guild`,
        `interaction.guild` / `interaction.channel`, `channel.name`.
        Returns None when the field is not one of these (the caller
        falls through to the table / the clean E4001)."""
        ctx = self._handler_ctx()
        cache = ctx.get("cache")
        name = obj_type.name
        if field == "guild" and name in ("member", "interaction"):
            if binding.guild_id_expr is None or cache is None:
                self._fail(
                    f"`{name}.guild` is not available in this "
                    "handler (there is no guild here)")
            return self._guild_expect_expr(
                binding.guild_id_expr, cache,
                binding.guild_id_optional)
        if field == "channel" and name == "interaction":
            if binding.channel_id_expr is None:
                self._fail("`interaction.channel` is not available "
                           "in this handler")
            return binding.channel_id_expr
        if field == "name" and name == "channel":
            if cache is None:
                self._fail("`channel.name` needs the cache (it is "
                           "not available in this handler)")
            return (f"{cache}.channel({binding.rs})"
                    f".map(|c| c.name.clone()).unwrap_or_default()")
        return None

    # -- intents ---------------------------------------------------

    _RS_INTENT_MAP: dict[str, list[str]] = {
        # Declared intent -> GatewayIntents bits (Java's
        # _INTENT_MAP, Rust-spelled). `guilds` needs no bit: the
        # GUILDS base intent below already covers it.
        "members": ["GUILD_MEMBERS"],
        "moderation": ["GUILD_MODERATION"],
        "emojis": ["GUILD_EXPRESSIONS"],
        "webhooks": ["GUILD_WEBHOOKS"],
        "invites": ["GUILD_INVITES"],
        "voice_states": ["GUILD_VOICE_STATES"],
        "presences": ["GUILD_PRESENCES"],
        "guild_messages": ["GUILD_MESSAGES"],
        "direct_messages": ["DIRECT_MESSAGES"],
        "guild_reactions": ["GUILD_MESSAGE_REACTIONS"],
        "direct_message_reactions": ["DIRECT_MESSAGE_REACTIONS"],
        "guild_typing": ["GUILD_MESSAGE_TYPING"],
        "direct_message_typing": ["DIRECT_MESSAGE_TYPING"],
        "message_content": ["MESSAGE_CONTENT"],
        "guild_scheduled_events": ["SCHEDULED_EVENTS"],
        "auto_moderation": ["AUTO_MODERATION_CONFIGURATION",
                            "AUTO_MODERATION_EXECUTION"],
        "guild_polls": ["GUILD_MESSAGE_POLLS"],
        "direct_message_polls": ["DIRECT_MESSAGE_POLLS"],
    }

    def _bot_intents(self, program: A.Program) -> str:
        """The `GatewayIntents` expression for a bot: the declared
        intents plus the derived ones (member events -> members,
        a message event -> message_content; privileged intents are
        only ever on when the program asks for them - never
        silently), plus the GUILDS base intent. The base is a
        deliberate deviation from the Java mirror (R-intents):
        serenity has no default intents (unlike JDA's
        `createDefault`), and GUILDS is non-privileged - the
        `{guild}` fallback and the member-join system-channel
        lookup both depend on the guilds being cached."""
        bot = program.bot
        declared = list(bot.intents) if bot is not None \
            and bot.intents is not None else []
        kinds = {e.kind for e in program.events}
        if "member_join" in kinds or "member_remove" in kinds:
            declared.append("members")
        if "message" in kinds:
            declared.append("message_content")
        bits: list[str] = []
        for intent in declared:
            if intent == "guilds" or intent == "integrations":
                continue
            mapped = self._RS_INTENT_MAP.get(intent)
            if mapped is None:
                self._fail(f"unknown intent `{intent}`")
            for bit in mapped:
                if bit not in bits:
                    bits.append(bit)
        prefix = "serenity::model::gateway::GatewayIntents::"
        parts = [prefix + "GUILDS"] + [prefix + b for b in bits]
        return " | ".join(parts)

    # -- answers (reply / defer / edit / followup / send) ---------

    def _answer_parts(self, node, indent: int) -> tuple[
            str | None, str | None, list[str], bool]:
        """(content, embed_tmp, embed_lines, ephemeral) for a
        Reply/Edit/Followup/Send value: a str / interpolation /
        `t()` value is the content; an embed value builds a
        `CreateEmbed` first and rides as `.embeds(vec![tmp])`."""
        value = node.value
        if value is None:
            return None, None, [], False
        if isinstance(value, A.EmbedExpr):
            lines, tmp = self._emit_embed_lines(value, indent)
            return None, tmp, lines, False
        content = self.emit_expr(value, 0)
        ephemeral = bool(getattr(node, "ephemeral", False))
        return content, None, [], ephemeral

    def _answer_builder(self, content: str | None,
                        embed_tmp: str | None, ephemeral: bool,
                        builder_type: str, indent: int) -> tuple[str, str]:
        """The `let mut <builder> = <builder_type>::new()...;` line
        for an answer, and the builder variable name. `builder_type`
        is the fully-qualified builder struct."""
        p = "  " * indent
        builder = self._new_temp_name()
        line = f"{p}let mut {builder} = {builder_type}::new()"
        if content is not None:
            line += f".content({content})"
        if embed_tmp is not None:
            line += f".embeds(vec![{embed_tmp}])"
        if ephemeral:
            line += ".ephemeral(true)"
        line += ";"
        return line, builder

    def _new_answer_info(self, kind: str, builder_line: str,
                         http: str, interaction: str | None = None,
                         captured: bool = False) -> dict:
        """The `_last_answer_info` for a fresh answer: a message
        variable is allocated (an underscore-prefixed temp, so an
        unused one warns about nothing) and captured lazily by
        `_attach_components` (interaction answers) or eagerly by
        the send itself (channel answers)."""
        return {"kind": kind, "builder_base": builder_line,
                "msgvar": self._new_temp_name(),
                "captured": captured, "rows": [], "http": http,
                "interaction": interaction, "builder_rel": 1}

    def _emit_reply(self, node: A.Reply, indent: int) -> list[str]:
        p = "  " * indent
        ctx = self._handler_ctx()
        reply = ctx.get("reply")
        content, embed_tmp, embed_lines, ephemeral = \
            self._answer_parts(node, indent)
        if ephemeral and reply != "interaction":
            self._fail("`ephemeral` is only possible on an "
                       "interaction reply (a system-channel or "
                       "message reply cannot be ephemeral)",
                       span=node.span)
        if reply == "interaction":
            http = ctx["http"]
            interaction = ctx["interaction"]
            self._need_discord(
                "serenity::builder::CreateInteractionResponseMessage")
            line, builder = self._answer_builder(
                content, embed_tmp, ephemeral,
                "serenity::builder::CreateInteractionResponseMessage",
                indent)
            lines = list(embed_lines)
            lines.append(line)
            lines.append(
                f"{p}{interaction}.create_response({http}, "
                f"serenity::builder::CreateInteractionResponse::"
                f"Message({builder})).await?;")
            self._last_answer_info = self._new_answer_info(
                "interaction", line, http, interaction=interaction)
            return lines
        if reply in ("system_channel", "message_channel"):
            http = ctx["http"]
            channel = ctx["channel"]
            self._need_discord("serenity::builder::CreateMessage")
            line, builder = self._answer_builder(
                content, embed_tmp, False,
                "serenity::builder::CreateMessage", indent)
            lines = list(embed_lines)
            lines.append(line)
            msgvar = self._new_temp_name()
            lines.append(f"{p}let mut {msgvar} = "
                         f"{channel}.send_message({http}, {builder})"
                         f".await?;")
            info = self._new_answer_info("channel", line, http,
                                         captured=True)
            info["msgvar"] = msgvar
            self._last_answer_info = info
            return lines
        if reply == "timeout":
            http = ctx["http"]
            msgvar = ctx["msgvar"]
            self._need_discord("serenity::builder::EditMessage")
            line, builder = self._answer_builder(
                content, embed_tmp, False,
                "serenity::builder::EditMessage", indent)
            lines = list(embed_lines)
            lines.append(line)
            lines.append(f"{p}{msgvar}.edit({http}, {builder})"
                         f".await?;")
            info = self._new_answer_info("timeout", line, http,
                                         captured=True)
            info["msgvar"] = msgvar
            self._last_answer_info = info
            return lines
        self._fail("`reply` is not possible in this handler (a "
                   "reply needs a command, component, or event "
                   "handler)", span=node.span)

    def _emit_defer(self, node: A.Defer, indent: int) -> list[str]:
        p = "  " * indent
        ctx = self._handler_ctx()
        if ctx.get("reply") != "interaction":
            self._fail("`defer` is only possible in a command or "
                       "component handler (the checker should have "
                       "rejected it)", span=node.span)
        return [f"{p}{ctx['interaction']}.defer({ctx['http']})"
                f".await?;"]

    def _emit_edit(self, node: A.Edit, indent: int) -> list[str]:
        p = "  " * indent
        ctx = self._handler_ctx()
        reply = ctx.get("reply")
        content, embed_tmp, embed_lines, _eph = \
            self._answer_parts(node, indent)
        if reply == "interaction":
            http = ctx["http"]
            interaction = ctx["interaction"]
            self._need_discord(
                "serenity::builder::EditInteractionResponse")
            line, builder = self._answer_builder(
                content, embed_tmp, False,
                "serenity::builder::EditInteractionResponse", indent)
            lines = list(embed_lines)
            lines.append(line)
            lines.append(f"{p}{interaction}.edit_response({http}, "
                         f"{builder}).await?;")
            self._last_answer_info = self._new_answer_info(
                "edit", line, http, interaction=interaction)
            return lines
        if reply in ("system_channel", "message_channel", "timeout"):
            # Edit the message the last answer produced (channel
            # kinds - the message variable was captured eagerly by
            # the send) or the enclosing answer's message
            # (timeout). An edit with no previous answer is a
            # clean E4001, not an undefined variable.
            if reply == "timeout":
                msgvar = ctx["msgvar"]
            else:
                info = self._last_answer_info
                if info is None:
                    self._fail("`edit` needs a previous reply, "
                               "followup, or send in the same block "
                               "(there is no message to edit)",
                               span=node.span)
                msgvar = info["msgvar"]
            http = ctx["http"]
            self._need_discord("serenity::builder::EditMessage")
            line, builder = self._answer_builder(
                content, embed_tmp, False,
                "serenity::builder::EditMessage", indent)
            lines = list(embed_lines)
            lines.append(line)
            lines.append(f"{p}{msgvar}.edit({http}, {builder})"
                         f".await?;")
            info = self._new_answer_info("edit", line, http,
                                         captured=True)
            info["msgvar"] = msgvar
            self._last_answer_info = info
            return lines
        self._fail("`edit` is not possible in this handler (an "
                   "edit needs a command, component, or event "
                   "handler)", span=node.span)

    def _emit_followup(self, node: A.Followup, indent: int) \
            -> list[str]:
        p = "  " * indent
        ctx = self._handler_ctx()
        if ctx.get("reply") != "interaction":
            self._fail("`followup` is only possible after an "
                       "interaction reply (in a command or "
                       "component handler)", span=node.span)
        content, embed_tmp, embed_lines, _eph = \
            self._answer_parts(node, indent)
        http = ctx["http"]
        interaction = ctx["interaction"]
        self._need_discord(
            "serenity::builder::CreateInteractionResponseFollowup")
        line, builder = self._answer_builder(
            content, embed_tmp, False,
            "serenity::builder::CreateInteractionResponseFollowup",
            indent)
        lines = list(embed_lines)
        lines.append(line)
        lines.append(f"{p}{interaction}.create_followup({http}, "
                     f"{builder}).await?;")
        self._last_answer_info = self._new_answer_info(
            "followup", line, http, interaction=interaction)
        return lines

    def _emit_send(self, node: A.Send, indent: int) -> list[str]:
        p = "  " * indent
        ctx = self._handler_ctx()
        if node.channel is None:
            self._fail("`send` needs a channel", span=node.span)
        channel = self.emit_expr(node.channel, 0)
        if _is_int(self._type_of(node.channel)):
            # A bare snowflake is a channel id.
            channel = ("serenity::model::id::ChannelId::new("
                       f"{channel} as u64)")
        http = ctx.get("http")
        if http is None:
            self._fail("`send` is only possible in a message "
                       "event or a task handler (there is no "
                       "Discord connection here)", span=node.span)
        content, embed_tmp, embed_lines, _eph = \
            self._answer_parts(node, indent)
        self._need_discord("serenity::builder::CreateMessage")
        line, builder = self._answer_builder(
            content, embed_tmp, False,
            "serenity::builder::CreateMessage", indent)
        lines = list(embed_lines)
        lines.append(line)
        msgvar = self._new_temp_name()
        lines.append(f"{p}let mut {msgvar} = "
                     f"{channel}.send_message({http}, {builder})"
                     f".await?;")
        info = self._new_answer_info("send", line, http,
                                     captured=True)
        info["msgvar"] = msgvar
        self._last_answer_info = info
        return lines

    # -- components (button / select / modal) ---------------------

    _BUTTON_STYLES: dict[str, str] = {
        "primary": "Primary", "secondary": "Secondary",
        "success": "Success", "danger": "Danger",
    }

    def _component_names(self, custom_id: str) -> tuple[str, str]:
        """(variable, handler fn) for a component: `buy_again` /
        `buy_again_handler` for custom id `buy-again` - unique per
        file (a numeric suffix on collision)."""
        var = self._component_var_name(custom_id)
        if var in self._bot_component_names:
            n = 2
            while f"{var}{n}" in self._bot_component_names:
                n += 1
            var = f"{var}{n}"
        fn = f"{var}_handler"
        self._bot_component_names.add(var)
        self._bot_component_names.add(fn)
        return var, fn

    def _button_expr(self, spec: dict, disabled: bool) -> str:
        """The `CreateButton` expression for a button spec (the live
        one, or the disabled rebuild for a timeout)."""
        label = _rs_str(spec["label"])
        dis = ".disabled(true)" if disabled else ""
        if spec.get("url") is not None:
            return ("serenity::builder::CreateButton::new_link("
                    f"{_rs_str(spec['url'])}).label({label}){dis}")
        style = self._BUTTON_STYLES[spec["style"]]
        return ("serenity::builder::CreateButton::new("
                f"{_rs_str(spec['custom'])}).label({label})"
                f".style(serenity::model::application::ButtonStyle::"
                f"{style}){dis}")

    def _select_menu_expr(self, spec: dict, disabled: bool) -> str:
        """The `CreateSelectMenu` expression for a select spec."""
        opts = ", ".join(
            "serenity::builder::CreateSelectMenuOption::new("
            f"{_rs_str(o)}, {_rs_str(o)})" for o in spec["options"])
        menu = ("serenity::builder::CreateSelectMenu::new("
                f"{_rs_str(spec['custom'])}, "
                "serenity::builder::CreateSelectMenuKind::String "
                f"{{ options: vec![{opts}] }})"
                f".placeholder({_rs_str(spec['label'])})")
        if spec.get("min_values") is not None:
            menu += f".min_values({spec['min_values']})"
        if spec.get("max_values") is not None:
            menu += f".max_values({spec['max_values']})"
        if disabled:
            menu += ".disabled(true)"
        return menu

    def _disabled_row(self, spec: dict) -> str:
        """The disabled `CreateActionRow` for a component spec -
        rebuilt from the spec, not cloned (`CreateActionRow` has no
        `as_disabled`; the builders do)."""
        if spec["kind"] == "button":
            return ("serenity::builder::CreateActionRow::Buttons("
                    f"vec![{self._button_expr(spec, True)}])")
        return ("serenity::builder::CreateActionRow::SelectMenu("
                f"{self._select_menu_expr(spec, True)})")

    def _collect_timeout_captures(self, body: list) -> list[tuple]:
        """The values an `on_timeout` body needs from its enclosing
        handler: (name, capture var, expression, interp capture
        var or None, interp expression or None) per name the body
        references that resolves to a handler / local / param
        binding (a module-level name is reachable from the spawn
        as-is; `bot` is provided inside the spawn from the moved
        `Data`). The body's `user` / `guild` / `channel` bindings
        are captured by value AND by interpolation form (the
        interpolation mentions `interaction`, which does not exist
        in the spawn). `interaction` itself cannot be captured
        (it is borrowed) - referencing it in a timeout body is a
        clean E4001."""
        refs: set[str] = set()
        bound: set[str] = set()
        self._collect_names(body, refs, bound)
        caps: list[tuple] = []
        for name in sorted(refs - bound):
            binding = self._lookup(name)
            if binding is None or binding.kind not in (
                    "handler", "local", "param"):
                continue
            if name == "interaction" and binding.kind == "handler":
                self._fail("`interaction` is not available in an "
                           "`on_timeout` body (use `user`, `guild`, "
                           "or `channel` instead)")
            cap = self._new_temp_name()
            cap_i: str | None = None
            expr_i: str | None = None
            if binding.interp is not None \
                    and binding.interp != binding.rs:
                cap_i = self._new_temp_name()
                expr_i = binding.interp
            caps.append((name, cap, binding.rs, cap_i, expr_i))
        return caps

    def _collect_names(self, node, refs: set[str],
                       bound: set[str]) -> None:
        """Walk AST nodes collecting referenced names (`refs`) and
        names bound inside the walked subtree (`bound`: `let` /
        `const` names, `for` variables, lambda params). A generic
        dataclass walk - every node shape is covered the same way."""
        import dataclasses
        if isinstance(node, A.Name):
            refs.add(node.name)
            return
        if isinstance(node, (A.Let, A.Const)):
            bound.add(node.name)
            self._collect_names(node.value, refs, bound)
            return
        if isinstance(node, A.For):
            bound.add(node.var)
            self._collect_names(node.iterable, refs, bound)
            for st in node.body:
                self._collect_names(st, refs, bound)
            return
        if isinstance(node, A.Lambda):
            inner = set(bound) | {p.name for p in node.params}
            if node.expr_body is not None:
                self._collect_names(node.expr_body, refs, inner)
            elif node.body is not None:
                for st in node.body:
                    self._collect_names(st, refs, inner)
            bound |= inner
            return
        if dataclasses.is_dataclass(node):
            for f in dataclasses.fields(node):
                if f.name == "span":
                    continue
                self._collect_names(getattr(node, f.name), refs,
                                    bound)
            return
        if isinstance(node, (list, tuple)):
            for item in node:
                self._collect_names(item, refs, bound)
            return
        if isinstance(node, dict):
            for item in node.values():
                self._collect_names(item, refs, bound)

    def _emit_component_body(self, body: list, ctx: dict,
                             bindings: list[tuple]) -> list[str]:
        """Emit a component handler body (indent 0, re-indented by
        the caller): push the handler context, bind the handler
        bindings, emit the block."""
        self._push_handler(ctx)
        for args in bindings:
            self._handler_binding(*args)
        lines = self._emit_block(body, 0)
        self._pop_handler()
        return lines

    def _emit_button(self, node: A.ButtonDecl, indent: int) \
            -> list[str]:
        p = "  " * indent
        if node.style == "link":
            if node.body:
                self._fail("a link button has no body (`do:`) - it "
                           "opens the url instead", span=node.span)
            var, _fn = self._component_names(
                node.custom_id or node.label)
            spec = {"kind": "button", "custom": None,
                    "url": node.url, "label": node.label,
                    "style": "link", "var": var, "fn": None,
                    "only_kind": "anyone", "only_roles": [],
                    "timeout": None, "has_timeout": False,
                    "on_timeout_lines": [], "caps": [],
                    "interp_caps": []}
            self._pending_components.append(spec)
            return [f"{p}let {var} = "
                    f"{self._button_expr(spec, False)};"]
        if node.custom_id is None:
            self._fail("a button needs a custom id", span=node.span)
        if node.timeout is None:
            # SPEC §13.9: a timeout is required on every collector.
            self._fail("a button needs a `timeout:` (a timeout is "
                       "required on every collector)", span=node.span)
        var, fn = self._component_names(node.custom_id)
        body_ctx = self._component_ctx()
        body = self._emit_component_body(
            node.body, body_ctx,
            self._component_bindings("ctx.cache"))
        timeout_lines, caps, interp_caps = self._emit_timeout_body(
            node.on_timeout)
        spec = {"kind": "button", "custom": node.custom_id,
                "url": None, "label": node.label,
                "style": node.style, "var": var, "fn": fn,
                "only_kind": node.only_kind,
                "only_roles": list(node.only_roles or []),
                "timeout": node.timeout, "has_timeout": True,
                "on_timeout_lines": timeout_lines, "caps": caps,
                "interp_caps": interp_caps, "body_lines": body}
        self._pending_components.append(spec)
        self._bot_components.append(spec)
        if node.only_kind == "author":
            self._uses_author_map = True
        if node.timeout is not None:
            self._uses_scheduler = True
        return [f"{p}let {var} = "
                f"{self._button_expr(spec, False)};"]

    def _emit_select(self, node: A.SelectDecl, indent: int) \
            -> list[str]:
        p = "  " * indent
        # A select has no `custom_id` in the AST (unlike a button):
        # its custom id is derived from the label, as in Java
        # (`custom = stmt.custom_id or
        # self._component_var_name(stmt.label)`).
        custom = self._component_var_name(node.label)
        if node.timeout is None:
            self._fail("a select needs a `timeout:` (a timeout is "
                       "required on every collector)", span=node.span)
        var, fn = self._component_names(custom)
        body_ctx = self._component_ctx()
        bindings = self._component_bindings("ctx.cache")
        bindings.append(("values", "values", Type("list", (Type("str"),)),
                         None, None, False, None))
        body = self._emit_component_body(node.body, body_ctx,
                                         bindings)
        timeout_lines, caps, interp_caps = self._emit_timeout_body(
            node.on_timeout)
        spec = {"kind": "select", "custom": custom,
                "url": None, "label": node.label,
                "options": list(node.options),
                "min_values": node.min_values,
                "max_values": node.max_values,
                "var": var, "fn": fn, "only_kind": node.only_kind,
                "only_roles": list(node.only_roles or []),
                "timeout": node.timeout, "has_timeout": True,
                "on_timeout_lines": timeout_lines, "caps": caps,
                "interp_caps": interp_caps, "body_lines": body}
        self._pending_components.append(spec)
        self._bot_components.append(spec)
        if node.only_kind == "author":
            self._uses_author_map = True
        self._uses_scheduler = True
        return [f"{p}let {var} = "
                f"{self._select_menu_expr(spec, False)};"]

    def _emit_modal(self, node: A.ModalDecl, indent: int) \
            -> list[str]:
        p = "  " * indent
        ctx = self._handler_ctx()
        if ctx.get("reply") != "interaction":
            self._fail("a modal is only possible in a command or "
                       "component handler (it is sent as the "
                       "interaction response)", span=node.span)
        if node.custom_id is None and node.title == "":
            self._fail("a modal needs a custom id or a title",
                       span=node.span)
        # Like a select, the modal's custom id defaults to the
        # title-derived name when none is given (Java:
        # `custom = stmt.custom_id or
        # self._component_var_name(stmt.title)`).
        custom = (node.custom_id
                  or self._component_var_name(node.title))
        var, fn = self._component_names(custom)
        # The modal IS the answer: a `CreateInteractionResponse::
        # Modal` carrying a `CreateModal` whose rows each hold one
        # text input.
        rows: list[str] = []
        for field in node.fields:
            style = ("Short" if (field.style or "short") == "short"
                     else "Paragraph")
            inp = ("serenity::builder::CreateInputText::new("
                   f"serenity::model::application::InputTextStyle::"
                   f"{style}, {_rs_str(field.name)}, "
                   f"{_rs_str(field.name)})")
            inp += (".required(true)" if field.required
                    else ".required(false)")
            if field.min_length is not None:
                inp += f".min_length({field.min_length})"
            if field.max_length is not None:
                inp += f".max_length({field.max_length})"
            rows.append("serenity::builder::CreateActionRow::"
                        f"InputText({inp})")
        modal = ("serenity::builder::CreateModal::new("
                 f"{_rs_str(custom)}, {_rs_str(node.title)})"
                 f".components(vec![{', '.join(rows)}])")
        lines = [f"{p}let mut {var} = "
                 "serenity::builder::CreateInteractionResponse::"
                 f"Modal({modal});"]
        lines.append(f"{p}{ctx['interaction']}.create_response("
                     f"{ctx['http']}, {var}).await?;")
        # The modal's handler: the fields are bound by name
        # (`_modal_value` walks the submitted rows). A modal has
        # no author/roles gate and its timeout has no serenity
        # equivalent - it is ignored, as in the Java emitter
        # (documented divergence, R-modal).
        body_ctx = self._modal_ctx()
        bindings = self._component_bindings("ctx.cache")
        for field in node.fields:
            bindings.append((field.name,
                             f"_modal_value(interaction, "
                             f"{_rs_str(field.name)})",
                             Type("str"), None, None, False, None))
        body = self._emit_component_body(node.body, body_ctx,
                                         bindings)
        spec = {"kind": "modal", "custom": custom,
                "url": None, "label": node.title,
                "var": var, "fn": fn, "only_kind": "anyone",
                "only_roles": [], "timeout": None,
                "has_timeout": False, "on_timeout_lines": [],
                "caps": [], "interp_caps": [],
                "body_lines": body}
        self._bot_components.append(spec)
        self._bot_component_kinds.add("modal")
        return lines

    def _component_ctx(self) -> dict:
        """The handler context for a button/select handler: the
        component interaction is the `interaction`, the serenity
        context is the `http`."""
        return {"kind": "component", "reply": "interaction",
                "http": "ctx", "http_owned": "ctx.http.clone()",
                "cache": "ctx.cache",
                "locale": "interaction.locale.as_str()",
                "data": "data",
                "author_id": "interaction.user.id.to_string()",
                "interaction": "interaction", "channel": None,
                "msgvar": None}

    def _modal_ctx(self) -> dict:
        """The handler context for a modal handler (same shape as
        a component handler; the interaction is the modal
        interaction)."""
        ctx = self._component_ctx()
        ctx["kind"] = "modal"
        return ctx

    def _component_bindings(self, cache: str) -> list[tuple]:
        """The handler bindings shared by command, component, and
        modal handlers: `user`, `guild` (cache-resolved, DM-safe),
        `channel`, `interaction`. `cache` is the target handler's
        cache expression (`ctx.cache()` for a poise command context,
        `ctx.cache` for a serenity context)."""
        return [
            ("user", "interaction.user.clone()", Type("user"),
             "interaction.user.mention()", None, False, None),
            ("guild", self._guild_expect_expr(
                "interaction.guild_id", cache, True), Type("guild"),
             self._guild_name_expr(
                 "interaction.guild_id", cache, True,
                 "\"DM\".to_string()"),
             "interaction.guild_id", True, None),
            ("channel", "interaction.channel_id", Type("channel"),
             "_channel_text(interaction.channel_id)", None, False,
             "interaction.channel_id"),
            ("interaction", "interaction", Type("interaction"),
             None, "interaction.guild_id", True,
             "interaction.channel_id"),
        ]

    def _emit_timeout_body(self, body: list | None) -> tuple[
            list[str], list[tuple[str, str]], list[tuple[str, str]]]:
        """Emit an `on_timeout` body (indent 0, re-indented into
        the spawn by `_attach_components`): the timeout context
        answers by editing the enclosing answer's message (whose
        clone is `{msgvar}_timeout`), and the handler bindings the
        body references are rebound to captures (evaluated in the
        handler, before the spawn). Returns (lines, value
        captures, interp captures) - each capture is a (name,
        expression) pair for a `let <name> = <expr>.clone();`
        line."""
        if not body:
            return [], [], []
        info = self._last_answer_info
        if info is None:
            self._fail("an `on_timeout` body needs an answer to "
                       "attach to (there is no message to edit)")
        msgvar = info["msgvar"]
        caps = self._collect_timeout_captures(body)
        ctx = {"kind": "timeout", "reply": "timeout",
               "http": f"&{msgvar}_http",
               "http_owned": f"{msgvar}_http.clone()",
               "cache": None, "locale": "_DEFAULT_LOCALE",
               "data": f"{msgvar}_data", "author_id": None,
               "interaction": None, "channel": None,
               "msgvar": f"{msgvar}_timeout"}
        self._push_handler(ctx)
        # Rebind the captured names to their capture variables
        # (value + interpolation form) in the timeout scope. A
        # rebound member's guild id reads off the captured member
        # itself - the enclosing `new_member.guild_id` expression
        # does not exist in the spawn.
        for name, cap, _expr, cap_i, _expr_i in caps:
            binding = self._lookup(name)
            guild_id_expr = None
            if binding is not None \
                    and binding.guild_id_expr is not None \
                    and binding.type is not None \
                    and binding.type.name == "member":
                guild_id_expr = f"{cap}.guild_id"
            self._scopes[-1][name] = _Binding(
                rs=cap, type=binding.type if binding else None,
                kind="handler", interp=cap_i,
                guild_id_expr=guild_id_expr,
                guild_id_optional=False)
        lines = self._emit_block(body, 0)
        self._pop_handler()
        cap_lines = [(cap, expr)
                     for (_n, cap, expr, _ci, _ei) in caps]
        interp_cap_lines = [(cap_i, expr_i)
                            for (_n, _c, _e, cap_i, expr_i) in caps
                            if cap_i is not None]
        return lines, cap_lines, interp_cap_lines

    def _attach_components(self, lines: list[str]) -> None:
        """Attach the pending components to the last answer in the
        block (D40): rewrite the answer's builder line to carry
        `.components(vec![rows])`, capture the message variable
        when it is needed, insert the author-map entries, and
        spawn the timeout task(s). Consumes _pending_components."""
        if not self._pending_components:
            return
        if self._last_answer is None \
                or self._last_answer_info is None:
            self._fail("a button/select must follow a reply, "
                       "followup, edit, or send in the same block "
                       "(there is no answer to attach it to)")
        idx, _kind = self._last_answer
        info = self._last_answer_info
        ctx = self._handler_ctx()
        builder_idx = idx - 1
        if builder_idx < 0:
            self._fail("internal error: the last answer has no "
                       "builder line")
        p = lines[builder_idx][:len(lines[builder_idx])
                                - len(lines[builder_idx].lstrip())]
        # 1. Rows for the newly pending components (each component
        # is its own action row); the builder line is rewritten
        # from the stored base, so a second component on the same
        # answer extends the same `.components(...)`.
        for comp in self._pending_components:
            if comp["kind"] == "button":
                info["rows"].append(
                    "serenity::builder::CreateActionRow::Buttons("
                    f"vec![{comp['var']}])")
            elif comp["kind"] == "select":
                info["rows"].append(
                    "serenity::builder::CreateActionRow::SelectMenu("
                    f"{comp['var']})")
        rows_text = ", ".join(info["rows"])
        base = info["builder_base"]
        lines[builder_idx] = (base[:-1]
                              + f".components(vec![{rows_text}]);")
        # 2. Capture the message variable (interaction replies and
        # followups/edits return it lazily; channel sends and
        # timeout edits already have it).
        after: list[str] = []
        if info["kind"] == "interaction" and not info["captured"]:
            msgvar = info["msgvar"]
            after.append(f"{p}let mut {msgvar} = "
                         f"{info['interaction']}.get_response("
                         f"{info['http']}).await?;")
            info["captured"] = True
        elif info["kind"] in ("followup", "edit") \
                and not info["captured"]:
            msgvar = info["msgvar"]
            lines[idx] = (f"{p}let mut {msgvar} = "
                          + lines[idx][len(p):])
            info["captured"] = True
        # 3. Author-map entries (only: author).
        data_expr = ctx.get("data")
        author_expr = ctx.get("author_id")
        for comp in self._pending_components:
            if comp["only_kind"] != "author":
                continue
            if data_expr is None or author_expr is None:
                self._fail("`only: author` needs a command, "
                           "component, or event handler (there is "
                           "no author here)")
            after.append(
                f"{p}{data_expr}.component_authors.lock().unwrap()"
                f".insert(format!(\"{{}}:{{}}\", "
                f"{info['msgvar']}.id, {_rs_str(comp['custom'])}), "
                f"{author_expr});")
        # 4. Timeout spawns (one per attach, covering exactly the
        # newly pending components that have a timeout). The spawn
        # moves clones (the message, the data, the http, the
        # captures) - the originals stay in the handler, so a
        # second component on the same answer still works.
        timed = [c for c in self._pending_components
                 if c.get("has_timeout")]
        if timed:
            max_timeout = max(c["timeout"] for c in timed)
            msgvar = info["msgvar"]
            data_var = f"{msgvar}_data"
            http_var = f"{msgvar}_http"
            clone_var = f"{msgvar}_timeout"
            for comp in timed:
                for cap, expr in comp.get("caps") or []:
                    after.append(f"{p}let {cap} = {expr}.clone();")
                for cap, expr in comp.get("interp_caps") or []:
                    after.append(f"{p}let {cap} = {expr}.clone();")
            if data_expr is None:
                self._fail("a component timeout needs a handler "
                           "with shared state (there is no `Data` "
                           "here)")
            after.append(f"{p}let {data_var} = "
                         f"{data_expr}.clone();")
            http_owned = ctx.get("http_owned")
            if http_owned is None:
                self._fail("a component timeout needs a handler "
                           "with a Discord connection")
            after.append(f"{p}let {http_var} = {http_owned};")
            after.append(f"{p}let mut {clone_var} = "
                         f"{msgvar}.clone();")
            after.append(f"{p}tokio::spawn(async move {{")
            ip = p + "  "
            after.append(f"{ip}tokio::time::sleep("
                         f"std::time::Duration::from_secs("
                         f"{max_timeout})).await;")
            timeout_text = "\n".join(
                line for c in timed
                for line in c.get("on_timeout_lines") or [])
            if self.stores and self._uses_store(timeout_text):
                after.append(f"{ip}let bot = &{data_var}.bot;")
            disabled = ", ".join(self._disabled_row(c)
                                 for c in timed)
            after.append(f"{ip}let _ = {clone_var}.edit(&{http_var}, "
                         "serenity::builder::EditMessage::new()"
                         f".components(vec![{disabled}])).await;")
            for comp in timed:
                for line in comp.get("on_timeout_lines") or []:
                    after.append(ip + self._timeout_line(line)
                                 if line else "")
            for comp in timed:
                if comp["only_kind"] == "author":
                    after.append(
                        f"{ip}{data_var}.component_authors.lock()"
                        f".unwrap().remove(&format!(\"{{}}:{{}}\", "
                        f"{clone_var}.id, "
                        f"{_rs_str(comp['custom'])}));")
            after.append(f"{p}}});")
            self._uses_scheduler = True
        lines[idx + 1:idx + 1] = after
        self._pending_components = []

    @staticmethod
    def _uses_store(text: str) -> bool:
        """Whether emitted Rust text calls the store (`bot.<op>`):
        a word-boundary match, so `data_about_bot.user` (the
        ready event's bot user) does not count as a store call."""
        return re.search(r"\bbot\.", text) is not None

    @staticmethod
    def _timeout_line(line: str) -> str:
        """Rewrite one `on_timeout` body line for the `tokio::spawn`
        closure (which returns `()`, not `Result<(), Error>`): a
        handler's `return Ok(());` becomes a bare `return;`, and a
        trailing `.await?;` becomes a discarded `.await;` (a
        `let _ = ...` prefix when the line is a bare expression
        statement)."""
        stripped = line.strip()
        lead = line[:len(line) - len(line.lstrip())]
        if stripped == "return Ok(());":
            return lead + "return;"
        if stripped.endswith(".await?;"):
            core = stripped[:-len(".await?;")] + ".await;"
            if not core.startswith("let "):
                core = "let _ = " + core
            return lead + core
        return line

    @staticmethod
    def _unused_param_renames(sig_line: str, rest_lines: list[str],
                              params: list[str]) -> str:
        """Rename unused fn params to `_name` in the signature
        line (a param whose name never appears as a word in the
        body would warn `unused_variables`). The rename only
        touches `name:` in the signature - the body never uses
        the name, so nothing else needs rewriting."""
        rest = "\n".join(rest_lines)
        for p in params:
            if re.search(rf"\b{re.escape(p)}\b", rest) is None:
                sig_line = sig_line.replace(f"{p}:", f"_{p}:", 1)
        return sig_line

    def _emit_component_handler(self, spec: dict) -> list[str]:
        """The `async fn <name>_handler(...) -> Result<(), Error>`
        for a component spec: the bot clone, the select `values`
        binding, the author/roles gate, the body, `Ok(())`."""
        kind = spec["kind"]
        fn = spec["fn"]
        if kind == "modal":
            itype = "serenity::model::application::ModalInteraction"
        else:
            itype = ("serenity::model::application::"
                     "ComponentInteraction")
        rest: list[str] = []
        if self.stores and self._uses_store("\n".join(spec["body_lines"])):
            rest.append("  let bot = data.bot.clone();")
        if kind == "select":
            rest.append("  let values: Vec<String> = match "
                        "&interaction.data.kind {")
            rest.append("    serenity::model::application::"
                        "ComponentInteractionDataKind::StringSelect "
                        "{ values } => values.clone(),")
            rest.append("    _ => Vec::new(),")
            rest.append("  };")
        if spec["only_kind"] == "author":
            gate = self._new_temp_name()
            # Clone the entry out of the map first: a
            # `std::sync::MutexGuard` is not `Send`, so holding
            # it across the reply's `.await` below would make
            # the handler future non-`Send` (poise requires
            # `Send` futures in its event handler).
            rest.append(f"  let {gate}_entry = "
                        "data.component_authors.lock().unwrap()"
                        ".get(&format!(\"{}:{}\", "
                        f"interaction.message.id, "
                        f"{_rs_str(spec['custom'])}))")
            rest.append("    .cloned();")
            rest.append(f"  if let Some({gate}) = {gate}_entry {{")
            rest.append(f"    if {gate} != "
                        "interaction.user.id.to_string() {")
            rest.append(f"      let mut {gate}_reply = "
                        "serenity::builder::"
                        "CreateInteractionResponseMessage::new()"
                        ".content(\"This button is not for you.\")"
                        ".ephemeral(true);")
            rest.append("      interaction.create_response(ctx, "
                        "serenity::builder::CreateInteractionResponse::"
                        f"Message({gate}_reply)).await?;")
            rest.append("      return Ok(());")
            rest.append("    }")
            rest.append("  }")
        elif spec["only_kind"] == "roles":
            roles = ", ".join(_rs_str(r)
                             for r in spec["only_roles"])
            gate = self._new_temp_name()
            rest.append(f"  if !_has_any_role(&ctx.cache, "
                        f"interaction.guild_id, "
                        f"interaction.member.as_ref(), "
                        f"&[{roles}]) {{")
            rest.append(f"    let mut {gate}_reply = "
                        "serenity::builder::"
                        "CreateInteractionResponseMessage::new()"
                        ".content(\"You do not have a role that can "
                        "use this.\").ephemeral(true);")
            rest.append("    interaction.create_response(ctx, "
                        "serenity::builder::CreateInteractionResponse::"
                        f"Message({gate}_reply)).await?;")
            rest.append("    return Ok(());")
            rest.append("  }")
        for line in spec["body_lines"]:
            rest.append("  " + line if line else "")
        rest.append("  Ok(())")
        rest.append("}")
        sig = (f"async fn {fn}(ctx: &serenity::client::Context, "
               f"data: &Data, interaction: &{itype}) "
               f"-> Result<(), Error> {{")
        sig = self._unused_param_renames(
            sig, rest, ["ctx", "data", "interaction"])
        return [sig] + rest

    # -- commands / events / tasks --------------------------------

    def _option_rust_type(self, opt: A.OptionDecl) -> tuple[str, bool]:
        """(Rust type, needs a `String` shadow) for a command
        option. A str option with choices is a `&'static str`
        (poise's inline choices are index-based: the extraction
        yields the literal, so the parameter type must be the
        literal's type) and is shadowed to a `String` by the
        command prologue."""
        otype = opt.type
        if _is_str(otype):
            if opt.choices:
                return "&'static str", True
            return "String", False
        if _is_int(otype):
            return "i64", False
        if _is_float(otype):
            return "f64", False
        if otype.name == "bool":
            return "bool", False
        if otype.name == "user":
            return "serenity::model::user::User", False
        if otype.name == "member":
            return "serenity::model::guild::Member", False
        if otype.name == "channel":
            return "serenity::model::id::ChannelId", False
        if otype.name == "role":
            return "serenity::model::guild::Role", False
        self._fail(f"a command option of type `{otype}` is not "
                   "supported in the Rust emitter")

    def _emit_command(self, decl: A.CommandDecl) -> None:
        """One `#[poise::command(slash_command)]` async fn: the
        command name IS the fn name (mangled when it collides
        with a Rust keyword, with `#[rename]` keeping the slash
        name); the description is the doc comment (poise takes
        the first paragraph - `CommandArgs` has no description
        field)."""
        name = decl.name
        fn_name = self._mangle(name)
        if fn_name in self._bot_command_method_names:
            n = 2
            while f"{fn_name}{n}" in self._bot_command_method_names:
                n += 1
            fn_name = f"{fn_name}{n}"
        self._bot_command_method_names.add(fn_name)
        self._bot_command_methods_by_name[name] = fn_name
        desc = (decl.description or name).replace("\n", " ")
        params: list[str] = ["ctx: Context<'_>"]
        prologue: list[str] = []
        bindings: list[tuple] = []
        for opt in decl.options:
            pname = self._mangle(opt.name)
            rust_type, shadow = self._option_rust_type(opt)
            param_type = (rust_type if opt.required
                          else f"Option<{rust_type}>")
            attrs: list[str] = []
            attrs.append(f"#[description = "
                         f"{_rs_str(opt.description or opt.name)}]")
            if opt.choices:
                lits: list[str] = []
                for choice in opt.choices:
                    if isinstance(choice, A.Lit) \
                            and isinstance(choice.value, str):
                        lits.append(_rs_str(choice.value))
                    elif isinstance(choice, A.Lit):
                        lits.append(str(choice.value))
                    else:
                        self._fail("a command option choice must "
                                   "be a literal")
                attrs.append(f"#[choices({', '.join(lits)})]")
            if opt.min_value is not None:
                attrs.append(f"#[min = "
                             f"{self.emit_expr(opt.min_value, 0)}]")
            if opt.max_value is not None:
                attrs.append(f"#[max = "
                             f"{self.emit_expr(opt.max_value, 0)}]")
            if pname != opt.name:
                attrs.append(f"#[rename = {_rs_str(opt.name)}]")
            params.append(" ".join(attrs)
                          + f" {pname}: {param_type}")
            if not opt.required and opt.default is not None:
                default = self._emit_value(opt.default, opt.type)
                prologue.append(f"  let {pname}: {rust_type} = "
                                f"{pname}.unwrap_or({default});")
            elif shadow:
                prologue.append(f"  let {pname}: String = "
                                f"{pname}.to_string();")
            t = opt.type
            if not opt.required and opt.default is None:
                t = Type(t.name, t.params, nullable=True)
            bindings.append((opt.name, pname, t, None, None, False,
                             None))
        ctx = {"kind": "command", "reply": "interaction",
               "http": "ctx.serenity_context()",
               "http_owned": "ctx.serenity_context().http.clone()",
               "cache": "ctx.cache()",
               "locale": "interaction.locale.as_str()",
               "data": "ctx.data()",
               "author_id": "interaction.user.id.to_string()",
               "interaction": "interaction", "channel": None,
               "msgvar": None}
        self._push_handler(ctx)
        for args in self._component_bindings("ctx.cache()"):
            self._handler_binding(*args)
        for args in bindings:
            self._handler_binding(*args)
        body_lines = self._emit_block(decl.effective_body(), 1)
        self._pop_handler()
        lines = [f"/// {desc}"]
        attr = "#[poise::command(slash_command)]"
        if fn_name != name:
            attr = ("#[poise::command(slash_command, rename = "
                    f"{_rs_str(name)})]")
        lines.append(attr)
        lines.append(f"pub async fn {fn_name}({', '.join(params)}) "
                     f"-> Result<(), Error> {{")
        lines.append("  let interaction = match ctx {")
        lines.append("    poise::Context::Application(app) => "
                     "app.interaction,")
        lines.append("    _ => unreachable!(),")
        lines.append("  };")
        # The store clone only when the body actually touches the
        # store - otherwise it is an unused-variable warning.
        if self.stores and self._uses_store("\n".join(body_lines)):
            lines.append("  let bot = ctx.data().bot.clone();")
        lines.extend(prologue)
        lines.extend(body_lines)
        lines.append("  Ok(())")
        lines.append("}")
        self._bot_command_methods.append(lines)

    def _emit_event_arm(self, decl: A.EventDecl) -> list[str]:
        """One `match event` arm for an event declaration (indent
        0, re-indented by the caller into `main`'s event handler).
        poise's `event_handler` runs for EVERY event (its own
        command dispatch runs first), so the arm matches only the
        event it handles; `InteractionCreate::Command` /
        `Autocomplete` are ignored there (poise dispatched them)
        and component/modal interactions go to the dispatch arm."""
        kind = decl.kind
        if kind == "ready":
            lines = ["serenity::client::FullEvent::Ready { data_about_bot } "
                     "=> {"]
            self._push_handler(
                {"kind": "ready", "reply": "none", "http": "ctx",
                 "http_owned": "ctx.http.clone()", "cache": None,
                 "locale": "_DEFAULT_LOCALE", "data": "data",
                 "author_id": None, "interaction": None,
                 "channel": None, "msgvar": None})
            self._handler_binding(
                "bot", "(*data_about_bot.user).clone()",
                Type("user"), interp="data_about_bot.user.tag()")
            body_lines = self._emit_block(decl.effective_body(), 1)
            if self._uses_store("\n".join(body_lines)):
                # `bot` is the ready placeholder (the Discord bot
                # user) here, so a store call would resolve to the
                # user, not the store - fail cleanly instead of
                # emitting a type error.
                self._fail("a store call in a `ready` handler "
                           "collides with the `bot` placeholder "
                           "(the Discord bot user); use a command, "
                           "event, or task handler for store calls",
                           span=decl.span)
            lines.extend(body_lines)
            self._pop_handler()
            lines.append("}")
            return lines
        if kind == "member_join":
            lines = ["serenity::client::FullEvent::GuildMemberAddition "
                     "{ new_member } => {"]
            lines.append("  let mut _newr_system_channel = "
                         "ctx.cache.guild(new_member.guild_id)"
                         ".and_then(|g| g.system_channel_id);")
            lines.append("  if _newr_system_channel.is_none() {")
            lines.append("    _newr_system_channel = "
                         "new_member.guild_id.to_partial_guild(ctx)"
                         ".await.ok().and_then(|g| "
                         "g.system_channel_id);")
            lines.append("  }")
            lines.append("  if let Some(_newr_system_channel) = "
                         "_newr_system_channel {")
            self._push_handler(
                {"kind": "member_join", "reply": "system_channel",
                 "http": "ctx", "http_owned": "ctx.http.clone()",
                 "cache": "ctx.cache", "locale": "_DEFAULT_LOCALE",
                 "data": "data",
                 "author_id": "new_member.user.id.to_string()",
                 "interaction": None,
                 "channel": "_newr_system_channel", "msgvar": None})
            self._handler_binding(
                "user", "new_member.clone()", Type("member"),
                interp="new_member.user.mention()",
                guild_id_expr="new_member.guild_id")
            self._handler_binding(
                "guild", self._guild_expect_expr(
                    "new_member.guild_id", "ctx.cache", False),
                Type("guild"),
                interp=self._guild_name_expr(
                    "new_member.guild_id", "ctx.cache", False,
                    "String::new()"),
                guild_id_expr="new_member.guild_id")
            self._handler_binding(
                "channel", "_newr_system_channel", Type("channel"),
                interp="_channel_text(_newr_system_channel)")
            body_lines = self._emit_block(decl.effective_body(), 2)
            if self.stores and self._uses_store("\n".join(body_lines)):
                body_lines.insert(0, "    let bot = "
                                  "data.bot.clone();")
            lines.extend(body_lines)
            self._pop_handler()
            lines.append("  }")
            lines.append("}")
            return lines
        if kind == "member_remove":
            lines = ["serenity::client::FullEvent::GuildMemberRemoval "
                     "{ guild_id, user: _, member_data_if_available } "
                     "=> {"]
            lines.append("  let mut _newr_system_channel = "
                         "ctx.cache.guild(*guild_id)"
                         ".and_then(|g| g.system_channel_id);")
            lines.append("  if _newr_system_channel.is_none() {")
            lines.append("    _newr_system_channel = "
                         "guild_id.to_partial_guild(ctx)"
                         ".await.ok().and_then(|g| "
                         "g.system_channel_id);")
            lines.append("  }")
            lines.append("  if let Some(_newr_system_channel) = "
                         "_newr_system_channel {")
            lines.append("    if let Some(member) = "
                         "member_data_if_available {")
            self._push_handler(
                {"kind": "member_remove", "reply": "system_channel",
                 "http": "ctx", "http_owned": "ctx.http.clone()",
                 "cache": "ctx.cache", "locale": "_DEFAULT_LOCALE",
                 "data": "data",
                 "author_id": "member.user.id.to_string()",
                 "interaction": None,
                 "channel": "_newr_system_channel", "msgvar": None})
            self._handler_binding(
                "user", "member.clone()", Type("member"),
                interp="member.user.mention()",
                guild_id_expr="member.guild_id")
            self._handler_binding(
                "guild", self._guild_expect_expr(
                    "member.guild_id", "ctx.cache", False),
                Type("guild"),
                interp=self._guild_name_expr(
                    "member.guild_id", "ctx.cache", False,
                    "String::new()"),
                guild_id_expr="member.guild_id")
            self._handler_binding(
                "channel", "_newr_system_channel", Type("channel"),
                interp="_channel_text(_newr_system_channel)")
            body_lines = self._emit_block(decl.effective_body(), 3)
            if self.stores and self._uses_store("\n".join(body_lines)):
                body_lines.insert(0, "      let bot = "
                                  "data.bot.clone();")
            lines.extend(body_lines)
            self._pop_handler()
            lines.append("    }")
            lines.append("  }")
            lines.append("}")
            return lines
        if kind == "message":
            lines = ["serenity::client::FullEvent::Message { new_message } "
                     "=> {"]
            lines.append("  if new_message.author.id == "
                         "framework.bot_id() {")
            lines.append("    return Ok(());")
            lines.append("  }")
            self._push_handler(
                {"kind": "message", "reply": "message_channel",
                 "http": "ctx", "http_owned": "ctx.http.clone()",
                 "cache": "ctx.cache", "locale": "_DEFAULT_LOCALE",
                 "data": "data",
                 "author_id": "new_message.author.id.to_string()",
                 "interaction": None,
                 "channel": "new_message.channel_id", "msgvar": None})
            self._handler_binding(
                "message", "new_message.clone()", Type("message"),
                interp="new_message.content.clone()")
            self._handler_binding(
                "user", "new_message.author.clone()", Type("user"),
                interp="new_message.author.mention()")
            self._handler_binding(
                "guild", self._guild_expect_expr(
                    "new_message.guild_id", "ctx.cache", True),
                Type("guild"),
                interp=self._guild_name_expr(
                    "new_message.guild_id", "ctx.cache", True,
                    "\"DM\".to_string()"))
            self._handler_binding(
                "channel", "new_message.channel_id",
                Type("channel"),
                interp="_channel_text(new_message.channel_id)",
                channel_id_expr="new_message.channel_id")
            body_lines = self._emit_block(decl.effective_body(), 1)
            if self.stores and self._uses_store("\n".join(body_lines)):
                body_lines.insert(0, "  let bot = "
                                  "data.bot.clone();")
            lines.extend(body_lines)
            self._pop_handler()
            lines.append("}")
            return lines
        self._fail(f"unknown event kind `{kind}`")

    def _emit_interaction_arm(self) -> list[str]:
        """The `InteractionCreate` match arm: dispatch component
        and modal interactions to their handler fns by custom id
        (poise dispatches slash commands itself; anything else is
        ignored)."""
        if not self._bot_components:
            return []
        lines = ["serenity::client::FullEvent::InteractionCreate "
                 "{ interaction } => {"]
        lines.append("  match interaction {")
        buttons = [c for c in self._bot_components
                   if c["kind"] in ("button", "select")]
        modals = [c for c in self._bot_components
                  if c["kind"] == "modal"]
        if buttons:
            lines.append("    serenity::model::application::"
                         "Interaction::Component(component) => {")
            lines.append("      match component.data.custom_id"
                         ".as_str() {")
            for c in buttons:
                lines.append(f"        {_rs_str(c['custom'])} => "
                             f"{{ {c['fn']}(ctx, data, component)"
                             f".await?; }}")
            lines.append("        _ => {}")
            lines.append("      }")
            lines.append("    }")
        if modals:
            lines.append("    serenity::model::application::"
                         "Interaction::Modal(modal) => {")
            lines.append("      match modal.data.custom_id.as_str() {")
            for c in modals:
                lines.append(f"        {_rs_str(c['custom'])} => "
                             f"{{ {c['fn']}(ctx, data, modal)"
                             f".await?; }}")
            lines.append("        _ => {}")
            lines.append("      }")
            lines.append("    }")
        lines.append("    _ => {}")
        lines.append("  }")
        lines.append("}")
        return lines

    def _emit_task(self, decl: A.TaskDecl) -> None:
        """One task fn: `async fn _every_1h(http: &Http, data:
        &Data) -> Result<(), Error>` - spawned from poise's
        `setup` on a sleep-first loop (the initial delay is the
        period, as in Java's `scheduleAtFixedRate`)."""
        fn_name = self._task_fn_name(decl)
        self._push_handler(
            {"kind": "task", "reply": "none", "http": "http",
             "http_owned": "http.clone()", "cache": None,
             "locale": "_DEFAULT_LOCALE", "data": "data",
             "author_id": None, "interaction": None,
             "channel": None, "msgvar": None})
        body_lines = self._emit_block(decl.body, 1)
        self._pop_handler()
        rest: list[str] = []
        if self.stores and self._uses_store("\n".join(body_lines)):
            rest.append("  let bot = data.bot.clone();")
        rest.extend(body_lines)
        rest.append("  Ok(())")
        rest.append("}")
        sig = (f"async fn {fn_name}(http: &serenity::http::Http, "
               f"data: &Data) -> Result<(), Error> {{")
        sig = self._unused_param_renames(
            sig, rest, ["http", "data"])
        self._bot_task_methods.append([sig] + rest)

    # -- strings / small helpers ----------------------------------

    def _emit_strings(self, program: A.Program) -> list[str]:
        """`_DEFAULT_LOCALE`, the `STRINGS` table (a `OnceLock`
        map, built once), and `_t(key, locale, vars)` with the
        exact -> base-language -> default fallback (a missing key
        returns the key itself)."""
        self._need_rt("Dict")
        index, default = self._strings_index()
        map_type = ("std::collections::HashMap<&'static str, "
                    "std::collections::HashMap<&'static str, "
                    "&'static str>>")
        lines = [f"static _DEFAULT_LOCALE: &str = "
                 f"{_rs_str(default)};",
                 "",
                 "static STRINGS: std::sync::OnceLock<" + map_type
                 + "> = std::sync::OnceLock::new();",
                 "",
                 f"fn _newr_strings() -> &'static {map_type} {{",
                 "  STRINGS.get_or_init(|| {",
                 "    let mut strings = "
                 "std::collections::HashMap::new();"]
        for locale, entries in index.items():
            lines.append("    let mut locale_map = "
                         "std::collections::HashMap::new();")
            for key, entry in entries.items():
                lines.append(f"    locale_map.insert({_rs_str(key)}, "
                             f"{_rs_str(entry.template)});")
            lines.append(f"    strings.insert({_rs_str(locale)}, "
                         "locale_map);")
        lines.append("    strings")
        lines.append("  })")
        lines.append("}")
        lines.append("")
        lines.append("fn _t(key: &str, locale: &str, "
                     "vars: newr_rt::Dict<String>) -> String {")
        lines.append("  let strings = _newr_strings();")
        lines.append("  let base = locale.split('-').next()"
                     ".unwrap_or(locale);")
        lines.append("  let template = strings.get(locale)"
                     ".and_then(|m| m.get(key))")
        lines.append("    .or_else(|| strings.get(base)"
                     ".and_then(|m| m.get(key)))")
        lines.append("    .or_else(|| strings.get(_DEFAULT_LOCALE)"
                     ".and_then(|m| m.get(key)));")
        lines.append("  let template = match template {")
        lines.append("    Some(t) => *t,")
        lines.append("    None => return key.to_string(),")
        lines.append("  };")
        lines.append("  let mut out = template.to_string();")
        lines.append("  for k in vars.keys() {")
        lines.append("    if let Some(v) = vars.get(&k) {")
        lines.append("      out = out.replace(&format!(\"{{{k}}}\"), "
                     "&v);")
        lines.append("    }")
        lines.append("  }")
        lines.append("  out")
        lines.append("}")
        return lines

    def _emit_channel_text_lines(self) -> list[str]:
        """`_channel_text`: a channel's mention (the
        interpolation form of `{channel}`)."""
        return ["fn _channel_text(channel: "
                "serenity::model::id::ChannelId) -> String {",
                "  channel.mention().to_string()",
                "}"]

    def _emit_has_any_role_lines(self) -> list[str]:
        """`_has_any_role`: the `only: roles(...)` gate - true
        when the member holds a role whose name is in `roles`
        (looked up through the cache; a DM, a missing member, or
        a cold cache all deny)."""
        return [
            "fn _has_any_role(cache: &serenity::cache::Cache, "
            "guild_id: Option<serenity::model::id::GuildId>, "
            "member: Option<&serenity::model::guild::Member>, "
            "roles: &[&str]) -> bool {",
            "  let Some(guild_id) = guild_id else { return false; };",
            "  let Some(member) = member else { return false; };",
            "  let Some(guild) = cache.guild(guild_id) else "
            "{ return false; };",
            "  member.roles.iter().any(|role_id| guild.roles"
            ".get(role_id).map(|role| "
            "roles.contains(&role.name.as_str()))"
            ".unwrap_or(false))",
            "}"]

    def _emit_modal_value_lines(self) -> list[str]:
        """`_modal_value`: a modal field's submitted value by
        name (the empty string when the field is absent - the
        checker guarantees the declared fields)."""
        return [
            "fn _modal_value(interaction: "
            "&serenity::model::application::ModalInteraction, "
            "name: &str) -> String {",
            "  for row in &interaction.data.components {",
            "    for component in &row.components {",
            "      if let serenity::model::application::"
            "ActionRowComponent::InputText(input) = component {",
            "        if input.custom_id == name {",
            "          return input.value.clone()"
            ".unwrap_or_default();",
            "        }",
            "      }",
            "    }",
            "  }",
            "  String::new()",
            "}"]

    # -- the store (rusqlite) -------------------------------------

    _SQL_TYPES: dict[str, str] = {"int": "TEXT", "str": "TEXT",
                                  "float": "REAL", "bool": "INTEGER"}

    def _emit_store_methods(self, program: A.Program) -> list[str]:
        """`pub struct Bot` (one `Mutex<Connection>` per store) +
        `impl Bot`: `connect()` (opens `<store>.db`, `PRAGMA
        busy_timeout = 5000`, `CREATE TABLE IF NOT EXISTS` per
        table + the ledger table when `ledger true`) and the store
        operations. The economy methods exist only when the store
        has a `balances` table (Java's rule); balances are stored
        as TEXT and parsed back to i64, arithmetic through
        `newr_rt::add` / `newr_rt::sub` (checked, overflow is an
        error). Every mutation runs in one `BEGIN IMMEDIATE`
        transaction with its ledger row, so a crash mid-op cannot
        leave a balance without its ledger entry; a duplicate key
        replays (returns `true` without applying twice). A
        database error fails CLOSED (0 / false / None) - it never
        grants money or hides a balance."""
        lines = ["pub struct Bot {"]
        for store in program.stores:
            lines.append(f"  {store.name}_db: "
                         "std::sync::Mutex<rusqlite::Connection>,")
        lines.append("}")
        lines.append("")
        # The store API is emitted wholesale; a program that only
        # credits never calls `debit` / `transfer` / `get` - those
        # are not dead code, just unused by this program.
        lines.append("#[allow(dead_code)]")
        lines.append("impl Bot {")
        lines.append("  pub fn connect() -> Result<Self, "
                     "rusqlite::Error> {")
        for store in program.stores:
            lines.append(f"    let {store.name}_db = "
                         "rusqlite::Connection::open("
                         f"{_rs_str(store.name + '.db')})?;")
            batch = ["PRAGMA busy_timeout = 5000"]
            for table in store.tables:
                cols = ", ".join(
                    f"\"{c.name}\" "
                    f"{self._SQL_TYPES.get(c.type.name, 'TEXT')}"
                    + (" PRIMARY KEY" if i == 0 else "")
                    for i, c in enumerate(table.columns))
                batch.append("CREATE TABLE IF NOT EXISTS "
                             f"\"{table.name}\" ({cols})")
            if store.ledger:
                batch.append("CREATE TABLE IF NOT EXISTS \"ledger\" "
                             "(\"id\" INTEGER PRIMARY KEY "
                             "AUTOINCREMENT, \"user\" TEXT NOT NULL, "
                             "\"amount\" TEXT NOT NULL, \"key\" TEXT "
                             "UNIQUE, \"created_at\" INTEGER NOT "
                             "NULL)")
            sql = "; ".join(batch)
            lines.append(f"    {store.name}_db.execute_batch("
                         f"{_rs_str(sql)})?;")
        fields = ", ".join(
            f"{store.name}_db: std::sync::Mutex::new("
            f"{store.name}_db)" for store in program.stores)
        lines.append(f"    Ok(Self {{ {fields} }})")
        lines.append("  }")
        if any("balances" in {t.name for t in s.tables}
               for s in program.stores):
            lines.extend(self._emit_store_tx_helpers())
        for store in program.stores:
            lines.extend(self._emit_store_ops(store))
        lines.append("}")
        return lines

    def _emit_store_tx_helpers(self) -> list[str]:
        """Private transaction helpers shared by the economy
        methods (one copy, whatever the number of stores): read /
        upsert a balance, read / upsert a ledger amount. A
        balance row is absent -> 0; a ledger amount is absent ->
        0; a corrupt (unparsable) balance -> 0."""
        now = ("std::time::SystemTime::now()"
               ".duration_since(std::time::UNIX_EPOCH)"
               ".map(|d| d.as_secs() as i64).unwrap_or(0)")
        return [
            "",
            "  fn _balance_tx(tx: &rusqlite::Transaction, "
            "user: &str) -> Result<i64, rusqlite::Error> {",
            "    match tx.query_row(\"SELECT balance FROM balances "
            "WHERE user = ?1\", rusqlite::params![user], "
            "|r| r.get::<_, String>(0)) {",
            "      Ok(v) => Ok(v.parse::<i64>().unwrap_or(0)),",
            "      Err(rusqlite::Error::QueryReturnedNoRows) "
            "=> Ok(0),",
            "      Err(e) => Err(e),",
            "    }",
            "  }",
            "",
            "  fn _upsert_balance_tx(tx: &rusqlite::Transaction, "
            "user: &str, next: i64) -> Result<(), rusqlite::Error> {",
            "    tx.execute(\"INSERT INTO balances (user, balance) "
            "VALUES (?1, ?2) ON CONFLICT(user) DO UPDATE SET "
            "balance = excluded.balance\", "
            "rusqlite::params![user, next.to_string()])?;",
            "    Ok(())",
            "  }",
            "",
            "  fn _ledger_amount_tx(tx: &rusqlite::Transaction, "
            "key: &str) -> Result<i64, rusqlite::Error> {",
            "    match tx.query_row(\"SELECT amount FROM ledger "
            "WHERE key = ?1\", rusqlite::params![key], "
            "|r| r.get::<_, String>(0)) {",
            "      Ok(v) => Ok(v.parse::<i64>().unwrap_or(0)),",
            "      Err(rusqlite::Error::QueryReturnedNoRows) "
            "=> Ok(0),",
            "      Err(e) => Err(e),",
            "    }",
            "  }",
            "",
            "  fn _ledger_upsert_tx(tx: &rusqlite::Transaction, "
            "user: &str, amount: i64, key: &str) "
            "-> Result<(), rusqlite::Error> {",
            "    tx.execute(\"INSERT INTO ledger (user, amount, key, "
            "created_at) VALUES (?1, ?2, ?3, ?4) ON CONFLICT(key) "
            "DO UPDATE SET amount = excluded.amount\", "
            f"rusqlite::params![user, amount.to_string(), key, "
            f"{now}])?;",
            "    Ok(())",
            "  }",
        ]

    def _emit_store_ops(self, store: A.StoreDecl) -> list[str]:
        name = store.name
        lines: list[str] = []
        tables = {t.name: t for t in store.tables}
        if "balances" in tables:
            lines.extend(self._emit_economy_methods(name))
        for table in store.tables:
            lines.extend(self._emit_table_methods(name, table))
        return lines

    def _emit_economy_methods(self, name: str) -> list[str]:
        """balance / credit / debit / transfer for a store with a
        `balances` table. `credit` never refuses; `debit` and
        `transfer` refuse (`false`) on insufficient funds.
        Transfer follows the JS emitter's semantics (NOT Java's -
        Java's transfer healing is a dead branch: it logs the out
        leg under the plain key on the SENDER, then looks for
        `:out` keys on the RECEIVER, so it can never fire, and a
        normal transfer never credits the receiver at all):
        the legs are logged as `key:":out"` / `key:":in"` and
        upserted, a partial transfer is healed leg by leg, and a
        replay (both legs done) returns `true`."""
        self._need_rt("add")
        self._need_rt("sub")
        self._need_sqlite()
        db = f"{name}_db"
        lock = (f"self.{db}.lock().unwrap_or_else(|e| "
                "e.into_inner())")
        L = [
            "",
            f"  pub fn {name}_balance(&self, user: &str) -> i64 {{",
            f"    let conn = {lock};",
            "    match conn.query_row(\"SELECT balance FROM "
            "balances WHERE user = ?1\", rusqlite::params![user], "
            "|r| r.get::<_, String>(0)) {",
            "      Ok(v) => v.parse::<i64>().unwrap_or(0),",
            "      Err(_) => 0,",
            "    }",
            "  }",
            "",
            f"  pub fn {name}_credit(&self, user: &str, delta: i64, "
            f"key: &str) -> bool {{",
            f"    self._{name}_apply(user, delta, key, false)",
            "  }",
            "",
            f"  pub fn {name}_debit(&self, user: &str, amount: i64, "
            f"key: &str) -> bool {{",
            f"    self._{name}_apply(user, newr_rt::sub(0, amount), "
            "key, true)",
            "  }",
            "",
            f"  fn _{name}_apply(&self, user: &str, delta: i64, "
            "key: &str, refuse_negative: bool) -> bool {",
            f"    match self._{name}_apply_inner(user, delta, key, "
            "refuse_negative) {",
            "      Ok(v) => v,",
            "      Err(_) => false,",
            "    }",
            "  }",
            "",
            f"  fn _{name}_apply_inner(&self, user: &str, delta: i64, "
            "key: &str, refuse_negative: bool) "
            "-> Result<bool, rusqlite::Error> {",
            f"    let mut conn = {lock};",
            "    let tx = conn.transaction_with_behavior("
            "rusqlite::TransactionBehavior::Immediate)?;",
            "    let dup = match tx.query_row(\"SELECT 1 FROM "
            "ledger WHERE key = ?1\", rusqlite::params![key], "
            "|r| r.get::<_, i64>(0)) {",
            "      Ok(_) => true,",
            "      Err(rusqlite::Error::QueryReturnedNoRows) "
            "=> false,",
            "      Err(e) => return Err(e),",
            "    };",
            "    if dup {",
            "      return Ok(true);",
            "    }",
            "    let cur = Self::_balance_tx(&tx, user)?;",
            "    let next = newr_rt::add(cur, delta);",
            "    if refuse_negative && next < 0 {",
            "      return Ok(false);",
            "    }",
            "    Self::_upsert_balance_tx(&tx, user, next)?;",
            "    tx.execute(\"INSERT INTO ledger (user, amount, key, "
            "created_at) VALUES (?1, ?2, ?3, ?4)\", "
            "rusqlite::params![user, delta.to_string(), key, "
            "std::time::SystemTime::now()"
            ".duration_since(std::time::UNIX_EPOCH)"
            ".map(|d| d.as_secs() as i64).unwrap_or(0)])?;",
            "    tx.commit()?;",
            "    Ok(true)",
            "  }",
            "",
            f"  pub fn {name}_transfer(&self, from: &str, to: &str, "
            f"amount: i64, key: &str) -> bool {{",
            f"    match self._{name}_transfer_inner(from, to, "
            "amount, key) {",
            "      Ok(v) => v,",
            "      Err(_) => false,",
            "    }",
            "  }",
            "",
            f"  fn _{name}_transfer_inner(&self, from: &str, "
            "to: &str, amount: i64, key: &str) "
            "-> Result<bool, rusqlite::Error> {",
            "    if amount <= 0 {",
            "      return Ok(false);",
            "    }",
            f"    let mut conn = {lock};",
            "    let tx = conn.transaction_with_behavior("
            "rusqlite::TransactionBehavior::Immediate)?;",
            "    let out_key = format!(\"{key}:out\");",
            "    let in_key = format!(\"{key}:in\");",
            "    let out_done = Self::_ledger_amount_tx(&tx, "
            "&out_key)?;",
            "    let in_done = Self::_ledger_amount_tx(&tx, "
            "&in_key)?;",
            "    if out_done >= amount && in_done >= amount {",
            "      return Ok(true);",
            "    }",
            "    if out_done < amount {",
            "      let need = amount - out_done;",
            "      let cur_from = Self::_balance_tx(&tx, from)?;",
            "      if cur_from < need {",
            "        return Ok(false);",
            "      }",
            "      Self::_upsert_balance_tx(&tx, from, "
            "newr_rt::sub(cur_from, need))?;",
            "      Self::_ledger_upsert_tx(&tx, from, "
            "out_done + need, &out_key)?;",
            "    }",
            "    if in_done < amount {",
            "      let need = amount - in_done;",
            "      let cur_to = Self::_balance_tx(&tx, to)?;",
            "      Self::_upsert_balance_tx(&tx, to, "
            "newr_rt::add(cur_to, need))?;",
            "      Self::_ledger_upsert_tx(&tx, to, "
            "in_done + need, &in_key)?;",
            "    }",
            "    tx.commit()?;",
            "    Ok(true)",
            "  }",
        ]
        return L

    def _emit_table_methods(self, name: str,
                            table: A.TableDecl) -> list[str]:
        """Per-table `get` / `set` / `delete` (the row struct IS
        the table name): `get` returns `Option<Row>`, `set` and
        `delete` return `bool`. Ints are stored as TEXT (parsed
        back), floats as REAL, bools as INTEGER 0/1."""
        self._need_sqlite()
        db = f"{name}_db"
        lock = (f"self.{db}.lock().unwrap_or_else(|e| "
                "e.into_inner())")
        row_binding = self._lookup(table.name)
        rs_row = row_binding.rs if row_binding is not None \
            else self._mangle(table.name)
        cols = [(c.name, self._mangle(c.name), c.type)
                for c in table.columns]
        key_name, key_rs, key_type = cols[0]
        if _is_str(key_type):
            key_param = "key: &str"
            key_sql = "key"
        elif _is_int(key_type):
            key_param = "key: i64"
            key_sql = "key.to_string()"
        elif _is_float(key_type):
            key_param = "key: f64"
            key_sql = "key.to_string()"
        else:
            key_param = "key: bool"
            key_sql = "i64::from(key).to_string()"
        col_list = ", ".join(f"\\\"{c}\\\"" for c, _r, _t in cols)
        placeholders = ", ".join(f"?{i + 1}"
                                 for i in range(len(cols)))
        read_lines: list[str] = []
        for i, (_c, rs, t) in enumerate(cols):
            if _is_str(t):
                read_lines.append(
                    f"        {rs}: r.get::<_, String>({i})?,")
            elif _is_int(t):
                read_lines.append(
                    f"        {rs}: r.get::<_, String>({i})?"
                    ".parse::<i64>().unwrap_or(0),")
            elif _is_float(t):
                read_lines.append(
                    f"        {rs}: r.get::<_, f64>({i})?,")
            else:
                read_lines.append(
                    f"        {rs}: r.get::<_, i64>({i})? != 0,")
        set_vals: list[str] = []
        for _c, rs, t in cols:
            if _is_str(t):
                set_vals.append(f"row.{rs}")
            elif _is_int(t):
                set_vals.append(f"row.{rs}.to_string()")
            elif _is_float(t):
                set_vals.append(f"row.{rs}")
            else:
                set_vals.append(f"i64::from(row.{rs})")
        if len(cols) > 1:
            set_clause = ", ".join(
                f"\\\"{c}\\\" = excluded.\\\"{c}\\\"" for c, _r, _t in cols[1:])
            on_conflict = (f"ON CONFLICT(\\\"{key_name}\\\") DO UPDATE "
                           f"SET {set_clause}")
        else:
            on_conflict = f"ON CONFLICT(\\\"{key_name}\\\") DO NOTHING"
        L = [
            "",
            f"  pub fn {name}_get_{rs_row}(&self, {key_param}) "
            f"-> Option<{rs_row}> {{",
            f"    let conn = {lock};",
            f"    match conn.query_row(\"SELECT {col_list} FROM "
            f"\\\"{table.name}\\\" WHERE \\\"{key_name}\\\" = ?1\", "
            f"rusqlite::params![{key_sql}], |r| {{",
            f"      Ok({rs_row} {{",
        ]
        L.extend(read_lines)
        L.append("      })")
        L.append("    }) {")
        L.append("      Ok(v) => Some(v),")
        L.append("      Err(_) => None,")
        L.append("    }")
        L.append("  }")
        L.append("")
        L.append(f"  pub fn {name}_set_{rs_row}(&self, row: {rs_row}) "
                 "-> bool {")
        L.append(f"    let conn = {lock};")
        L.append(f"    match conn.execute(\"INSERT INTO "
                 f"\\\"{table.name}\\\" ({col_list}) VALUES "
                 f"({placeholders}) {on_conflict}\", "
                 f"rusqlite::params![{', '.join(set_vals)}]) {{")
        L.append("      Ok(_) => true,")
        L.append("      Err(_) => false,")
        L.append("    }")
        L.append("  }")
        L.append("")
        L.append(f"  pub fn {name}_delete_{rs_row}(&self, "
                 f"{key_param}) -> bool {{")
        L.append(f"    let conn = {lock};")
        L.append(f"    match conn.execute(\"DELETE FROM "
                 f"\\\"{table.name}\\\" WHERE \\\"{key_name}\\\" = "
                 f"?1\", rusqlite::params![{key_sql}]) {{")
        L.append("      Ok(n) => n > 0,")
        L.append("      Err(_) => false,")
        L.append("    }")
        L.append("  }")
        return L

    def _emit_data_struct(self, program: A.Program) -> list[str]:
        """`pub struct Data` - the shared state poise carries into
        every handler: the `Bot` (when a store exists) and the
        per-message author map (when an `only: author` component
        exists). `Clone` (an `Arc` per field), so tasks and
        timeouts can hold their own copy."""
        fields: list[str] = []
        if program.stores:
            fields.append("  bot: std::sync::Arc<Bot>,")
        if self._uses_author_map:
            fields.append("  component_authors: std::sync::Arc<"
                          "std::sync::Mutex<std::collections::"
                          "HashMap<String, String>>>,")
        lines = ["#[derive(Clone)]", "pub struct Data {"]
        lines.extend(fields)
        lines.append("}")
        return lines

    def _emit_bot_main(self, program: A.Program,
                       event_arms: list[list[str]]) -> list[str]:
        """`#[tokio::main] async fn main()`: the top-level
        statements, the `DISCORD_TOKEN` guard, the store connect,
        the poise framework (commands + the event handler), the
        task spawns in `setup`, then the serenity client."""
        # The deploy file includes the bot as a module
        # (`#[path] mod <stem>;`), so this `main` is dead code
        # there - silence that warning at the source.
        lines = ["#[allow(dead_code)]",
                 "#[tokio::main]", "async fn main() {"]
        lines.extend(self._emit_block(program.stmts, 1,
                                      top_level=True))
        lines.append("  let _newr_token = std::env::var("
                     "\"DISCORD_TOKEN\").unwrap_or_default();")
        lines.append("  if _newr_token.is_empty() {")
        lines.append("    eprintln!(\"set the DISCORD_TOKEN "
                     "environment variable\");")
        lines.append("    std::process::exit(1);")
        lines.append("  }")
        if program.stores:
            lines.append("  let bot = Bot::connect().expect("
                         "\"failed to open the store database\");")
        lines.append("  let framework = poise::Framework::builder()")
        lines.append("    .options(poise::FrameworkOptions {")
        cmds = ", ".join(
            f"{fn}()" for fn in
            self._bot_command_methods_by_name.values())
        lines.append(f"      commands: vec![{cmds}],")
        if event_arms:
            # `ctx` / `data` are only bound when an arm uses
            # them (a ready-only bot would otherwise warn
            # `unused_variables`); `framework` is only kept when
            # something uses it (`serenity_context`,
            # `user_data`, or `bot_id()`).
            arms_text = "\n".join(line for arm in event_arms
                                 for line in arm)
            use_ctx = re.search(r"\bctx\b", arms_text) is not None
            use_data = re.search(r"\bdata\b", arms_text) is not None
            use_fw = (use_ctx or use_data
                      or "framework.bot_id()" in arms_text)
            fw = "framework" if use_fw else "_framework"
            lines.append(f"      event_handler: |{fw}, event| "
                         "Box::pin(async move {")
            if use_ctx:
                lines.append("        let ctx = "
                             "framework.serenity_context;")
            if use_data:
                lines.append("        let data = framework.user_data;")
            lines.append("        match event {")
            for arm in event_arms:
                for line in arm:
                    lines.append("          " + line if line else "")
            lines.append("          _ => {}")
            lines.append("        }")
            lines.append("        Ok(())")
            lines.append("      }),")
        lines.append("      ..Default::default()")
        lines.append("    })")
        # `framework` is never used in `setup`; `ctx` is only
        # used to clone the http client for task spawns.
        setup_ctx = "ctx" if program.tasks else "_ctx"
        lines.append(f"    .setup(|{setup_ctx}, _ready, _framework| {{")
        lines.append("      Box::pin(async move {")
        data_fields: list[str] = []
        if program.stores:
            data_fields.append(
                "bot: std::sync::Arc::new(bot)")
        if self._uses_author_map:
            data_fields.append(
                "component_authors: std::sync::Arc::new("
                "std::sync::Mutex::new("
                "std::collections::HashMap::new()))")
        lines.append(f"        let data = Data {{ "
                     f"{', '.join(data_fields)} }};")
        for decl in program.tasks:
            fn_name = self._task_fn_name(decl)
            secs = decl.interval_seconds
            lines.append("        {")
            lines.append("          let http = ctx.http.clone();")
            lines.append("          let data = data.clone();")
            lines.append("          tokio::spawn(async move {")
            lines.append("            loop {")
            lines.append("              tokio::time::sleep("
                         "std::time::Duration::from_secs("
                         f"{secs})).await;")
            lines.append("              if let Err(e) = "
                         f"{fn_name}(&http, &data).await {{")
            lines.append(f"                eprintln!(\"[task] "
                         f"{fn_name} failed: {{e}}\");")
            lines.append("              }")
            lines.append("            }")
            lines.append("          });")
            lines.append("        }")
        lines.append("        Ok(data)")
        lines.append("      })")
        lines.append("    })")
        lines.append("    .build();")
        lines.append(f"  let intents = {self._bot_intents(program)};")
        lines.append("  let mut client = "
                     "serenity::Client::builder(&_newr_token, intents)")
        lines.append("    .framework(framework)")
        lines.append("    .await")
        lines.append("    .expect(\"failed to create the Discord "
                     "client\");")
        lines.append("  client.start().await.expect(\"failed to "
                     "start the Discord client\");")
        lines.append("}")
        return lines

    def _emit_bot_file(self, program: A.Program, main_path: str):
        """A bot program: header, `mod` declarations, top-level
        lets as statics, structs (including the synthesized
        per-table row structs), enums, functions, the `Bot`
        struct + store methods, `Data`, the strings table + `_t`,
        the small helpers, the command / component / task fns,
        then `main()`. Definitions are hoisted (as in a general
        file); the command / event / task emitters run first, so
        their accumulators (commands, components, dispatch) are
        filled before the file is assembled."""
        self.program = program
        self.fns = {f.name: f for f in program.functions}
        self.structs = {s.name: s for s in program.structs}
        self.enums = {e.name: e for e in program.enums}
        self.stores = {s.name: s for s in program.stores}
        self._register_module_scope(program)
        self._fn_ctx = []
        self._hoists = []
        # Reset the bot accumulators (per file).
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
        self._table_rows = {}
        self._pending_components = []
        self._component_counter = 0
        self._last_answer = None
        self._last_answer_info = None
        if program.stores:
            self._need_sqlite()
        # Table row structs (a table name colliding with a struct /
        # enum / fn is a clean E4001, as in Java).
        for store in program.stores:
            for table in store.tables:
                if table.name in self._scopes[0]:
                    self._fail(
                        f"table `{table.name}` collides with a "
                        "struct, enum, or function of the same name")
                if table.name in self._table_rows:
                    self._fail(
                        f"duplicate table `{table.name}`")
                self._bind_module_level(table.name, kind="struct")
                self._table_rows[table.name] = A.StructDecl(
                    name=table.name, fields=list(table.columns),
                    span=table.span)
        # Commands, events, tasks (in that order - commands and
        # events register components; the dispatch arm is built
        # after all of them).
        for decl in program.commands:
            self._emit_command(decl)
        event_arms: list[list[str]] = []
        for decl in program.events:
            event_arms.append(self._emit_event_arm(decl))
        for decl in program.tasks:
            self._emit_task(decl)
        interaction_arm = self._emit_interaction_arm()
        if interaction_arm:
            event_arms.append(interaction_arm)
        # Assemble the body (definitions first, main() last).
        is_main = self._is_main_file
        body: list[str] = []
        for st in program.stmts:
            if isinstance(st, (A.Let, A.Const)):
                b = self._lookup(st.name)
                t = st.type_ann or self._type_or_none(st.value) or \
                    Type("error")
                body.append(
                    f"static {b.rs if b is not None else self._mangle(st.name)}: "
                    f"std::sync::Mutex<Option<{self._rs_type(t)}>> = "
                    f"std::sync::Mutex::new(None);")
        for decl in program.structs:
            if body:
                body.append("")
            body.extend(self._emit_struct(decl))
        for row in self._table_rows.values():
            if body:
                body.append("")
            # A table name is lowercase in newr (`balances`) - a
            # Rust type name in that case warns
            # (`non_camel_case_types`); the row struct keeps the
            # table's name, with the warning silenced.
            body.append("#[allow(non_camel_case_types)]")
            body.extend(self._emit_struct(row))
        for decl in program.enums:
            if body:
                body.append("")
            body.extend(self._emit_enum(decl))
        for decl in program.functions:
            if body:
                body.append("")
            body.extend(self._emit_fn(decl))
        if program.stores:
            if body:
                body.append("")
            body.extend(self._emit_store_methods(program))
        if body:
            body.append("")
        body.extend(self._emit_data_struct(program))
        if program.strings:
            if body:
                body.append("")
            body.extend(self._emit_strings(program))
        if self._used_channel_text:
            if body:
                body.append("")
            body.extend(self._emit_channel_text_lines())
        if any(c["only_kind"] == "roles"
               for c in self._bot_components):
            if body:
                body.append("")
            body.extend(self._emit_has_any_role_lines())
        if any(c["kind"] == "modal"
               for c in self._bot_components):
            if body:
                body.append("")
            body.extend(self._emit_modal_value_lines())
        for method in self._bot_command_methods:
            if body:
                body.append("")
            body.extend(method)
        for spec in self._bot_components:
            if body:
                body.append("")
            body.extend(self._emit_component_handler(spec))
        for method in self._bot_task_methods:
            if body:
                body.append("")
            body.extend(method)
        if body:
            body.append("")
        body.extend(self._emit_bot_main(program, event_arms))
        lines = self._header(
            main_path,
            program.bot.description if program.bot else None)
        if self.rt_helpers:
            lines.append("mod newr_rt;" if is_main
                         else "use crate::newr_rt;")
        for imp in program.imports:
            if imp.path is not None:
                lines.append(
                    f"mod {self.imported_modules[imp.bound_name]};")
        if self._used_mention or self._used_channel_text:
            lines.append(
                "use serenity::model::mention::Mentionable;")
        # The poise shorthand types every command / component /
        # task fn is written against (`Context<'_>`,
        # `Result<(), Error>`).
        lines.append("type Error = Box<dyn std::error::Error "
                     "+ Send + Sync>;")
        lines.append("type Context<'a> = poise::Context<'a, Data, "
                     "Error>;")
        lines.append("")
        lines.extend(body)
        return "\n".join(lines) + "\n", bool(self.rt_helpers)

    def _emit_deploy_file(self, program: A.Program,
                          main_file: str) -> str:
        """The standalone registration script `<stem>_deploy.rs`:
        reads `DISCORD_TOKEN` + `DISCORD_CLIENT_ID` (both
        required), an optional `DISCORD_GUILD_ID` (guild commands
        when set, global commands otherwise), and registers the
        bot's commands through poise's builtins. The bot file is
        pulled in as a module (`#[path]`) so its command fns are
        callable as `<stem>::ping()`. `DISCORD_CLIENT_ID` is read
        and required (the Java deploy's interface) but the
        registration itself resolves the application id from the
        token through poise/serenity, so the value is unused
        beyond the guard."""
        file_stem = main_file[:-3] if main_file.endswith(".rs") \
            else main_file
        stem = _rs_stem(file_stem)
        lines = self._header(
            self.main_path,
            "Command registration - run once, then run the bot.")
        lines.append(f"#[path = \"{file_stem}.rs\"]")
        lines.append(f"mod {stem};")
        lines.append("")
        lines.append("#[tokio::main]")
        lines.append("async fn main() {")
        lines.append("  let token = std::env::var(\"DISCORD_TOKEN\")"
                     ".unwrap_or_default();")
        lines.append("  if token.is_empty() {")
        lines.append("    eprintln!(\"set the DISCORD_TOKEN "
                     "environment variable\");")
        lines.append("    std::process::exit(1);")
        lines.append("  }")
        lines.append("  let _client_id = std::env::var("
                     "\"DISCORD_CLIENT_ID\").unwrap_or_default();")
        lines.append("  if _client_id.is_empty() {")
        lines.append("    eprintln!(\"set the DISCORD_CLIENT_ID "
                     "environment variable\");")
        lines.append("    std::process::exit(1);")
        lines.append("  }")
        lines.append("  let http = serenity::http::Http::new(&token);")
        cmds = ", ".join(
            f"{stem}::{fn}()" for fn in
            self._bot_command_methods_by_name.values())
        lines.append(f"  let commands = vec![{cmds}];")
        # An empty `DISCORD_GUILD_ID` (e.g. `DISCORD_GUILD_ID=` in
        # a .env file) counts as unset - it must fall back to
        # global registration, not panic parsing "" as a number.
        lines.append("  match std::env::var(\"DISCORD_GUILD_ID\") {")
        lines.append("    Ok(guild_id) if !guild_id.is_empty() => {")
        lines.append("      let guild_id = "
                     "serenity::model::id::GuildId::new(guild_id"
                     ".parse::<u64>().expect(\"DISCORD_GUILD_ID must "
                     "be a number\"));")
        lines.append("      poise::builtins::register_in_guild(&http, "
                     "&commands, guild_id).await.expect(\"failed to "
                     "register commands\");")
        lines.append("      println!(\"Registered {} command(s) in "
                     "guild {}.\", commands.len(), guild_id);")
        lines.append("    }")
        lines.append("    _ => {")
        lines.append("      poise::builtins::register_globally(&http, "
                     "&commands).await.expect(\"failed to register "
                     "commands\");")
        lines.append("      println!(\"Registered {} command(s) "
                     "globally.\", commands.len());")
        lines.append("    }")
        lines.append("  }")
        lines.append("}")
        return "\n".join(lines) + "\n"

    # ------------------------------------------------------------ decls / file assembly (R2, R6)

    def _eq_expr(self, field: str, t: Type, side: str = "other") -> str:
        """`self.<field> == <side>.<field>`, except a fn-typed field,
        which compares by pointer identity (Arc::ptr_eq - the same
        rule as `==` on fn values)."""
        if _is_fn(t):
            return (f"std::sync::Arc::ptr_eq(&self.{field}, "
                    f"&{side}.{field})")
        return f"self.{field} == {side}.{field}"

    def _hash_line(self, field: str, t: Type) -> str:
        """One `NewrHash` line for a field: a fn-typed field hashes
        its pointer address (no Hash impl exists for a closure)."""
        if _is_fn(t):
            return (f"(std::sync::Arc::as_ptr(&self.{field}) as *const () "
                    f"as usize).hash(state);")
        return f"newr_rt::NewrHash::newr_hash(&self.{field}, state);"

    def _str_part(self, field: str, t: Type) -> str:
        """The `format!` argument rendering a field: a fn-typed
        field renders as `"<fn>"` (there is no ToNewrStr impl for
        a closure)."""
        if _is_fn(t):
            return '"<fn>"'
        return f"newr_rt::to_str(&self.{field})"

    def _emit_fn(self, decl: A.FnDecl, indent: int = 0) -> list[str]:
        p = "  " * indent
        binding = self._lookup(decl.name)
        rs_name = binding.rs if binding is not None \
            else self._mangle(decl.name)
        self._push_scope()
        saved_used = self._fn_used
        # The module-level names are statics - a param or a local
        # with the same name would shadow one (E0530), so the
        # used-name set starts with them and `_bind_fresh` renames
        # the collision (`x` -> `x_`).
        self._fn_used = set(self._module_used)
        # Newr params are assignable, so they are `mut` (R: a param
        # of a non-Copy type is moved in, then owned).
        params = [self._bind_fresh(prm.name, type=prm.type, kind="param")
                  for prm in decl.params]
        sig = ", ".join(f"mut {b.rs}: {self._rs_type(b.type)}"
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
        ret_text = "" if ret is None or _is_none(ret) \
            else f" -> {self._rs_type(ret)}"
        if ensures:
            # `result` in an `ensures` clause reads `_newr_result`.
            self._scopes[-1]["result"] = _Binding(rs="_newr_result",
                                                  type=ret,
                                                  kind="local")
            self._fn_used.add("_newr_result")
        self._fn_ctx.append({"return_type": ret,
                             "ensures": ensures, "in_handler": False})
        if requires or ensures:
            self._need_rt("NewrError")
        lines = [f"{p}pub fn {rs_name}({sig}){ret_text} {{"]
        bp = "  " * (indent + 1)
        body_lines: list[str] = []
        if ensures:
            # `_newr_result` is declared ONCE, at the top of the
            # function - every `return` assigns it (a `let` per
            # return would be a redeclaration). A void function
            # still gets one (of type `()`) so its `ensures`
            # clauses can read `result`.
            result_type = "()" if ret is None or _is_none(ret) \
                else self._rs_type(ret)
            body_lines.append(f"{bp}let mut _newr_result: {result_type};")
        for req in requires:
            cond = self.emit_expr(req.cond, 0)
            body_lines.append(f"{bp}if !({cond}) {{")
            body_lines.append(
                f"{bp}  newr_rt::throw(newr_rt::NewrError::new("
                f"{_rs_str('requires failed: ' + cond)}));")
            body_lines.append(f"{bp}}}")
        body_lines.extend(self._emit_block(rest, indent + 1,
                                           new_scope=False))
        if ensures and not (rest and isinstance(rest[-1], A.Return)):
            # The implicit return (falling off the end) is also a
            # return - check the `ensures` against the default.
            default = self._rs_default(ret) \
                if ret is not None else "()"
            body_lines.append(f"{bp}_newr_result = {default};")
            body_lines.extend(self._ensures_check_lines(ensures, indent + 1))
            if ret is None or _is_none(ret):
                body_lines.append(f"{bp}return;")
            else:
                body_lines.append(f"{bp}return _newr_result;")
        lines.extend(body_lines)
        lines.append(f"{p}}}")
        self._fn_ctx.pop()
        self._fn_used = saved_used
        self._pop_scope()
        return lines

    def _emit_struct(self, decl: A.StructDecl, indent: int = 0) -> list[str]:
        p = "  " * indent
        binding = self._lookup(decl.name)
        rs_name = binding.rs if binding is not None \
            else self._mangle(decl.name)
        self._need_rt("to_str")
        self._need_rt("NewrHash")
        fields = [(f.name, self._mangle(f.name), f.type)
                  for f in decl.fields]
        lines = [f"{p}#[derive(Clone)]",
                 f"{p}pub struct {rs_name} {{"]
        for _name, rs, t in fields:
            lines.append(f"{p}  pub {rs}: {self._rs_type(t)},")
        lines.append(f"{p}}}")
        # PartialEq is manual (a fn-typed field compares by
        # pointer); Eq is a marker the compiler trusts.
        if fields:
            cond = " && ".join(self._eq_expr(rs, t) for _n, rs, t in fields)
        else:
            cond = "true"
        lines.append(f"{p}impl PartialEq for {rs_name} {{")
        lines.append(f"{p}  fn eq(&self, other: &Self) -> bool {{")
        lines.append(f"{p}    {cond}")
        lines.append(f"{p}  }}")
        lines.append(f"{p}}}")
        lines.append(f"{p}impl Eq for {rs_name} {{}}")
        lines.append(f"{p}impl std::hash::Hash for {rs_name} {{")
        lines.append(f"{p}  fn hash<H: std::hash::Hasher>"
                     f"(&self, state: &mut H) {{")
        lines.append(f"{p}    newr_rt::NewrHash::newr_hash(self, state);")
        lines.append(f"{p}  }}")
        lines.append(f"{p}}}")
        lines.append(f"{p}impl newr_rt::NewrHash for {rs_name} {{")
        lines.append(f"{p}  fn newr_hash<H: std::hash::Hasher>"
                     f"(&self, state: &mut H) {{")
        for _name, rs, t in fields:
            lines.append(f"{p}    {self._hash_line(rs, t)}")
        lines.append(f"{p}  }}")
        lines.append(f"{p}}}")
        if fields:
            inits = ", ".join(f"{rs}: {self._rs_default(t)}"
                              for _n, rs, t in fields)
            default_body = f"Self {{ {inits} }}"
        else:
            default_body = "Self {}"
        lines.append(f"{p}impl Default for {rs_name} {{")
        lines.append(f"{p}  fn default() -> Self {{")
        lines.append(f"{p}    {default_body}")
        lines.append(f"{p}  }}")
        lines.append(f"{p}}}")
        if fields:
            parts = ", ".join(f"{name}: {{}}" for name, _rs, _t in fields)
            args = ", ".join(self._str_part(rs, t)
                             for _n, rs, t in fields)
            lines.append(f"{p}impl newr_rt::ToNewrStr for {rs_name} {{")
            lines.append(f"{p}  fn to_newr_str(&self) -> String {{")
            lines.append(f"{p}    format!(\"{rs_name}({parts})\", {args})")
            lines.append(f"{p}  }}")
            lines.append(f"{p}}}")
        else:
            lines.append(f"{p}impl newr_rt::ToNewrStr for {rs_name} {{")
            lines.append(f"{p}  fn to_newr_str(&self) -> String {{")
            lines.append(f"{p}    \"{rs_name}()\".to_string()")
            lines.append(f"{p}  }}")
            lines.append(f"{p}}}")
        return lines

    def _emit_enum(self, decl: A.EnumDecl, indent: int = 0) -> list[str]:
        p = "  " * indent
        binding = self._lookup(decl.name)
        rs_name = binding.rs if binding is not None \
            else self._mangle(decl.name)
        self._need_rt("to_str")
        self._need_rt("NewrHash")
        union = self._enum_union_fields(decl)
        fields = [(name, self._mangle(name), t)
                  for name, t in union.items()]
        lines = [f"{p}#[derive(Clone)]",
                 f"{p}pub struct {rs_name} {{"]
        lines.append(f"{p}  pub _newr_variant: &'static str,")
        for _name, rs, t in fields:
            lines.append(f"{p}  pub {rs}: {self._rs_type(t)},")
        lines.append(f"{p}}}")
        # The one constructor is the struct literal itself (the
        # factories below fill in the defaults for the fields a
        # variant does not declare).
        lines.append(f"{p}impl {rs_name} {{")
        for variant in decl.variants:
            vname = self._mangle(variant.name)
            if not variant.fields:
                inits = [f"_newr_variant: \"{variant.name}\""]
                inits += [f"{rs}: {self._rs_default(t)}"
                          for _n, rs, t in fields]
                lines.append(f"{p}  pub fn {vname}() -> {rs_name} {{")
                lines.append(f"{p}    {rs_name} {{ {', '.join(inits)} }}")
                lines.append(f"{p}  }}")
            else:
                params = ", ".join(
                    f"{self._mangle(f.name)}: {self._rs_type(f.type)}"
                    for f in variant.fields)
                lines.append(f"{p}  pub fn {vname}({params}) -> {rs_name} {{")
                inits = [f"_newr_variant: \"{variant.name}\""]
                declared = {f.name for f in variant.fields}
                for name, rs, t in fields:
                    if name in declared:
                        inits.append(rs)
                    else:
                        inits.append(f"{rs}: {self._rs_default(t)}")
                lines.append(f"{p}    {rs_name} {{ {', '.join(inits)} }}")
                lines.append(f"{p}  }}")
        lines.append(f"{p}}}")
        conds = ["self._newr_variant == other._newr_variant"]
        conds += [self._eq_expr(rs, t) for _n, rs, t in fields]
        lines.append(f"{p}impl PartialEq for {rs_name} {{")
        lines.append(f"{p}  fn eq(&self, other: &Self) -> bool {{")
        lines.append(f"{p}    {' && '.join(conds)}")
        lines.append(f"{p}  }}")
        lines.append(f"{p}}}")
        lines.append(f"{p}impl Eq for {rs_name} {{}}")
        lines.append(f"{p}impl std::hash::Hash for {rs_name} {{")
        lines.append(f"{p}  fn hash<H: std::hash::Hasher>"
                     f"(&self, state: &mut H) {{")
        lines.append(f"{p}    newr_rt::NewrHash::newr_hash(self, state);")
        lines.append(f"{p}  }}")
        lines.append(f"{p}}}")
        lines.append(f"{p}impl newr_rt::NewrHash for {rs_name} {{")
        lines.append(f"{p}  fn newr_hash<H: std::hash::Hasher>"
                     f"(&self, state: &mut H) {{")
        lines.append(f"{p}    newr_rt::NewrHash::newr_hash("
                     f"self._newr_variant, state);")
        for _name, rs, t in fields:
            lines.append(f"{p}    {self._hash_line(rs, t)}")
        lines.append(f"{p}  }}")
        lines.append(f"{p}}}")
        # `Default` is the first variant with defaulted fields.
        first = decl.variants[0].name if decl.variants else "?"
        inits = [f"_newr_variant: \"{first}\""]
        inits += [f"{rs}: {self._rs_default(t)}"
                  for _n, rs, t in fields]
        lines.append(f"{p}impl Default for {rs_name} {{")
        lines.append(f"{p}  fn default() -> Self {{")
        lines.append(f"{p}    Self {{ {', '.join(inits)} }}")
        lines.append(f"{p}  }}")
        lines.append(f"{p}}}")
        lines.append(f"{p}impl newr_rt::ToNewrStr for {rs_name} {{")
        lines.append(f"{p}  fn to_newr_str(&self) -> String {{")
        for i, variant in enumerate(decl.variants):
            kw = "if" if i == 0 else "} else if"
            lines.append(f"{p}    {kw} self._newr_variant == "
                         f"{_rs_str(variant.name)} {{")
            if variant.fields:
                parts = ", ".join(f"{f.name}: {{}}"
                                  for f in variant.fields)
                args = ", ".join(
                    self._str_part(self._mangle(f.name), f.type)
                    for f in variant.fields)
                lines.append(f"{p}      return format!(\"{rs_name}."
                             f"{variant.name}({parts})\", {args});")
            else:
                lines.append(f"{p}      return "
                             f"\"{rs_name}.{variant.name}\".to_string();")
        lines.append(f"{p}    }}")
        lines.append(f"{p}    \"{rs_name}.?\".to_string()")
        lines.append(f"{p}  }}")
        lines.append(f"{p}}}")
        return lines

    def _emit_general_file(self, program: A.Program, main_path: str):
        """A general (non-bot) program: header, `mod` declarations
        for the runtime and every file-module import, top-level
        lets as `static Mutex<Option<T>>` statics (R2: no
        initializer - `main()` / `__newr_init()` assigns them in
        file order, so top-level functions can read them and
        side-effecting initializers run in order), structs, enums,
        functions, then `fn main()` (the entry file, always
        emitted - rustc needs a `main`) or `pub fn __newr_init()`
        (a file module - the entry file calls it in import order).
        Definitions are hoisted - a function body reads module
        globals at call time, so emitting the `function` before
        the statements is safe - but the statements themselves
        (lets, assignments, prints, ...) must run in the order
        written."""
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
        # Statics for the top-level lets/consts (no initializers).
        for st in program.stmts:
            if isinstance(st, (A.Let, A.Const)):
                b = self._lookup(st.name)
                t = st.type_ann or self._type_or_none(st.value) or \
                    Type("error")
                body.append(f"static {b.rs if b is not None else self._mangle(st.name)}: "
                            f"std::sync::Mutex<Option<{self._rs_type(t)}>> = "
                            f"std::sync::Mutex::new(None);")
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
        # Top-level statements are emitted first (into a temp
        # list): a `db.*` call inside them — or inside a function
        # above — sets `_file_uses_db`, and the `newr_db` helper
        # module is emitted only then, before `fn main()` (or
        # `__newr_init()` for a file module).
        stmt_lines = self._emit_block(program.stmts, 1, top_level=True)
        if self._file_uses_db:
            if body:
                body.append("")
            body.extend(self._emit_db_mod())
        # Top-level statements: main() for the entry file,
        # __newr_init() for a file module. The module inits run
        # first (in import order), then this file's statements.
        if body:
            body.append("")
        if is_main:
            body.append("fn main() {")
        else:
            body.append("pub fn __newr_init() {")
        for imp in program.imports:
            if imp.path is not None:
                stem = self.imported_modules[imp.bound_name]
                body.append(f"  {stem}::__newr_init();")
        body.extend(stmt_lines)
        body.append("}")
        lines = self._header(main_path)
        if self.rt_helpers:
            # A module file reads the runtime through the crate
            # root (the entry file declares `mod newr_rt;` - the
            # patch in `emit_program_rs` adds it when only a
            # module uses the runtime).
            lines.append("mod newr_rt;" if is_main
                         else "use crate::newr_rt;")
        for imp in program.imports:
            if imp.path is not None:
                lines.append(f"mod {self.imported_modules[imp.bound_name]};")
        lines.append("")
        lines.extend(body)
        return "\n".join(lines) + "\n", bool(self.rt_helpers)

# ------------------------------------------------------------ entry point

def _rs_stem(stem: str) -> str:
    """Sanitize a file stem into a valid Rust identifier (a
    module name / binary name). Invalid characters become `_`;
    a leading digit gets a `_` prefix; a Rust keyword gets a
    trailing underscore."""
    out = []
    for ch in stem:
        if ch.isalnum() or ch == "_":
            out.append(ch)
        else:
            out.append("_")
    name = "".join(out) or "main"
    if name[0].isdigit():
        name = "_" + name
    if name in _RS_KEYWORDS:
        name += "_"
    return name


def _rs_cargo_toml(stem: str, file_stem: str, *,
                   uses_sqlite: bool) -> str:
    """The Cargo build file for a bot program: serenity 0.12.5 and
    poise 0.7.1 (both pinned, verified live - see
    tests/test_emitter_rs.py), tokio 1, rusqlite 0.41 with the
    bundled feature only when a store is used. Two binaries - the
    bot and the standalone registration script - with
    `default-run` so a bare `cargo run` still runs the bot."""
    artifact = "".join(ch.lower() if ch.isalnum() else "-"
                       for ch in stem).strip("-") or "bot"
    lines = [
        "# Generated by newr - do not edit by hand.",
        "[package]",
        f'name = "{artifact}"',
        'version = "1.0.0"',
        'edition = "2021"',
        f'default-run = "{stem}"',
        "",
        "[[bin]]",
        f'name = "{stem}"',
        f'path = "{file_stem}.rs"',
        "",
        "[[bin]]",
        f'name = "{stem}_deploy"',
        f'path = "{file_stem}_deploy.rs"',
        "",
        "[dependencies]",
        'serenity = { version = "=0.12.5", default-features = false, '
        'features = ["client", "gateway", "rustls_backend", "model"] }',
        'poise = "=0.7.1"',
        'tokio = { version = "1", features = ["macros", '
        '"rt-multi-thread"] }',
    ]
    if uses_sqlite:
        lines.append('rusqlite = { version = "0.41", features = '
                     '["bundled"] }')
    return "\n".join(lines) + "\n"


def _rs_db_cargo_toml(stem: str, file_stem: str) -> str:
    """The Cargo build file for a plain (non-bot) program that uses
    `db.*`: rusqlite 0.41 with the bundled feature only (a plain
    program otherwise builds with bare rustc and no Cargo.toml).
    One binary, no deploy script, no serenity/poise."""
    artifact = "".join(ch.lower() if ch.isalnum() else "-"
                       for ch in stem).strip("-") or "main"
    lines = [
        "# Generated by newr - do not edit by hand.",
        "[package]",
        f'name = "{artifact}"',
        'version = "1.0.0"',
        'edition = "2021"',
        "",
        "[[bin]]",
        f'name = "{stem}"',
        f'path = "{file_stem}.rs"',
        "",
        "[dependencies]",
        'rusqlite = { version = "0.41", features = ["bundled"] }',
    ]
    return "\n".join(lines) + "\n"


def emit_program_rs(program: A.Program, *, main_path: str,
                    sources: dict[str, str],
                    line_comments: bool = False,
                    output_stem: str | None = None) -> EmitResult:
    """Emit a CHECKED program to Rust - one `.rs` per `.newr` file.

    `main_path` is the input file as passed (the header names it);
    `sources` maps file paths to their text (for diagnostics and
    `--line-comments`). `output_stem` is the `-o` stem. The result's
    `files` maps an output basename (`hello.rs`) to its text; the
    main file keeps the raw stem (a file name need not be a valid
    identifier - the runner and the CLI expect `<source stem>.rs`);
    the sanitized stem is only for Rust identifiers (module names,
    binary names). `main_file` is the main output's basename;
    `uses_rt` says whether any file needs `newr_rt` (the caller
    copies `compiler/newr_rt.rs` as `newr_rt.rs` next to the output
    when it is true)."""
    file_stem = output_stem or Path(main_path).stem
    stem = _rs_stem(file_stem)
    em = _RsEmitter(program, main_path=main_path, sources=sources,
                    line_comments=line_comments, output_stem=file_stem)
    files, uses_rt = em._emit_file_set(program, main_path)
    # A module file may use the runtime while the main file does
    # not, and the main file is emitted first, so the `mod
    # newr_rt;` declaration is patched in here, after the whole
    # file set is known (the module reads it as
    # `crate::newr_rt`).
    main_file = file_stem + ".rs"
    if uses_rt and "mod newr_rt;" not in files[main_file]:
        anchor = ("// Edit the .newr file and rebuild with: "
                  f"newr build {main_path}\n")
        if anchor in files[main_file]:
            files[main_file] = files[main_file].replace(
                anchor, anchor + "mod newr_rt;\n", 1)
    if program.bot is not None or program.stores:
        # The standalone registration script rides along as a
        # second output file, and the Cargo build file (serenity
        # + poise always, rusqlite only when a store is used -
        # the CLI writes every file in result.files, so the
        # Cargo.toml lands next to the sources automatically).
        deploy = em._emit_deploy_file(program, file_stem + ".rs")
        files[file_stem + "_deploy.rs"] = deploy
        files["Cargo.toml"] = _rs_cargo_toml(stem, file_stem,
                                             uses_sqlite=em.uses_sqlite)
    elif em.uses_sqlite:
        # A plain program that uses `db.*` needs a Cargo build
        # file too (B8): rusqlite only, no serenity/poise and no
        # deploy script — plain programs otherwise build with
        # bare rustc and no Cargo.toml at all.
        files["Cargo.toml"] = _rs_db_cargo_toml(stem, file_stem)
    return EmitResult(files=files,
                      main_file=file_stem + ".rs",
                      uses_rt=uses_rt)


# `emit_program` is the name the tests and the CLI use; the `_rs`
# suffix marks the implementation (the Python emitter's entry is
# `emit_program` in compiler/emitter.py).
emit_program = emit_program_rs
