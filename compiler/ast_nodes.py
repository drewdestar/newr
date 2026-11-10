"""AST nodes for the newr language (SPEC.md §17.2).

Every node carries a Span (file + byte offsets, SPEC.md §16). Nodes are plain dataclasses;
the parser builds them, the resolver annotates names, the checker types
expressions (``expr_type`` is filled in by the checker, not the parser).

This module has no dependencies on any other compiler module.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .errors import Span


# ---------------------------------------------------------------- types


@dataclass(frozen=True)
class Type:
    """A newr type (SPEC.md §7.1).

    ``name`` is the base name (``int``, ``list``, ``Point``, ...);
    ``params`` holds generic arguments (``list[int]`` -> name ``list``,
    params ``(int,)``). ``nullable`` is the ``T?`` spelling (SPEC.md §7.2
    — exactly one absent value; there is no ``option[T]``). A function
    type has name ``"fn"`` and params ``(T1, ..., R)`` (return type last).
    ``span`` is where the type was written (not part of equality — two
    ``int`` types are equal wherever they were written).
    """

    name: str
    params: tuple["Type", ...] = ()
    span: Span | None = field(default=None, compare=False)
    nullable: bool = False

    def __str__(self) -> str:
        if self.name == "fn" and self.params:
            inner = ", ".join(str(p) for p in self.params[:-1])
            base = f"fn({inner}) -> {self.params[-1]}"
        elif not self.params:
            base = self.name
        else:
            inner = ", ".join(str(p) for p in self.params)
            base = f"{self.name}[{inner}]"
        return base + "?" if self.nullable else base


INT = Type("int")
FLOAT = Type("float")
STR = Type("str")
BOOL = Type("bool")
NONE_TYPE = Type("none")
ERROR_TYPE = Type("error")
DURATION = Type("duration")

BUILTIN_TYPE_NAMES = {
    "int", "float", "str", "bool", "list", "dict", "set", "Result", "fn",
    "error", "none", "duration", "Widget",
}

# Discord entity types (SPEC.md §7.1). Their *fields* are owned by the
# discord module (discord_limits.py) and injected into the checker as a
# type provider — the core never imports the discord tables (§2.1).
ENTITY_TYPE_NAMES = {
    "user", "member", "guild", "channel", "role", "interaction",
    "embed", "message",
}

GENERIC_ARITY = {"list": 1, "dict": 2, "set": 1, "Result": 2}


def list_of(elem: Type) -> Type:
    return Type("list", (elem,))


def dict_of(value: Type) -> Type:
    return Type("dict", (STR, value))


def nullable_of(inner: Type) -> Type:
    """``T?`` — the one nullability spelling (SPEC.md §7.2, G19)."""
    return Type(inner.name, inner.params, span=inner.span, nullable=True)


def result_of(ok_type: Type, err_type: Type) -> Type:
    return Type("Result", (ok_type, err_type))


def fn_of(params: list[Type], ret: Type) -> Type:
    """A function type ``fn(T1, ...) -> R`` (params + return last)."""
    return Type("fn", tuple(params) + (ret,))


# ---------------------------------------------------------------- base


@dataclass
class Node:
    span: Span
    # `///` doc comment attached by the parser (SPEC.md section 6,
    # M6): the text of the doc-comment lines directly above this
    # node, joined with "\n", or None when there is none. Doc
    # comments are syntax only, never semantic, so this field is
    # excluded from equality and from __init__ (init=False): no
    # constructor call site changes, and no subclass field order
    # changes (several subclasses have fields without defaults).
    doc: str | None = field(default=None, compare=False, init=False)


@dataclass
class Expr(Node):
    # Filled in by the checker; None until then.
    expr_type: Type | None = field(default=None, compare=False)


@dataclass
class Stmt(Node):
    pass


@dataclass
class Decl(Node):
    pass


# ---------------------------------------------------------------- expressions


@dataclass
class Lit(Expr):
    value: object = None  # int | float | str | bool | None


@dataclass
class Name(Expr):
    name: str = ""


@dataclass
class BinOp(Expr):
    op: str = ""  # + - * / % ** == != < <= > >= and or in
    left: Expr | None = None
    right: Expr | None = None


@dataclass
class UnaryOp(Expr):
    op: str = ""  # - not
    operand: Expr | None = None


@dataclass
class Ternary(Expr):
    """``cond ? a : b`` (SPEC.md §9.2, precedence level 0)."""

    cond: Expr | None = None
    then_expr: Expr | None = None
    else_expr: Expr | None = None


@dataclass
class Lambda(Expr):
    """A lambda / closure (SPEC.md §9.5, G4).

    Exactly one of ``body`` (block body) / ``expr_body`` (single-expression
    body) is set. Closures capture by value (a snapshot at creation).
    """

    params: list["Param"] = field(default_factory=list)
    return_type: Type | None = None
    body: list["Stmt"] | None = None
    expr_body: Expr | None = None


@dataclass
class TryExpr(Expr):
    """``try <expr>`` — returns ``Result[T, error]`` (SPEC.md §10)."""

    expr: Expr | None = None


@dataclass
class Propagate(Expr):
    """Statement-level ``?`` (SPEC.md §10, G5).

    Wraps the RHS expression of a statement. On ``Err``/``none`` the
    enclosing function returns early; on ``Ok``/``Some`` it unwraps to
    ``T``. The parser only ever creates this node as the outermost
    postfix of a statement RHS — a nested ``?`` is a parse error.
    """

    expr: Expr | None = None


@dataclass
class Call(Expr):
    func: Expr | None = None
    args: list[Expr] = field(default_factory=list)
    # kwargs: (name, value, span-of-name)
    kwargs: list[tuple[str, Expr, Span]] = field(default_factory=list)


@dataclass
class TCall(Expr):
    """The i18n lookup ``t("key", name: expr, ...)`` (SPEC.md §15 — discord module).

    The parser produces this node for calls to the builtin name ``t``;
    the checker validates the key and the argument names against the
    template. A user-defined function named ``t`` is a resolver error.
    """

    key: str = ""
    key_span: Span | None = None
    args: list[tuple[str, Expr, Span]] = field(default_factory=list)


@dataclass
class Member(Expr):
    obj: Expr | None = None
    name: str = ""
    # True when written with `?.` (optional chaining, SPEC.md §9.2).
    # Added in M1 (AST amendment): the parser used to build a plain
    # Member for `?.`, so the checker could not distinguish `a.b`
    # from `a?.b` or enforce the `?.` typing rule.
    optional: bool = False


@dataclass
class Index(Expr):
    obj: Expr | None = None
    index: Expr | None = None


@dataclass
class InterpPart:
    """One part of an interpolated string: a literal or an expression."""

    text: str | None = None  # literal text (when expr is None)
    expr: Expr | None = None


@dataclass
class Interp(Expr):
    parts: list[InterpPart] = field(default_factory=list)


@dataclass
class ListLit(Expr):
    elements: list[Expr] = field(default_factory=list)


@dataclass
class DictLit(Expr):
    # (key expression, value expression)
    pairs: list[tuple[Expr, Expr]] = field(default_factory=list)


@dataclass
class EmbedField:
    kind: str  # title | description | color | field
    name: str | None  # field name for kind == "field"
    value: Expr | None = None
    inline: bool | None = None
    span: Span | None = None


@dataclass
class EmbedExpr(Expr):
    fields: list[EmbedField] = field(default_factory=list)


# ---------------------------------------------------------------- statements


@dataclass
class Let(Stmt):
    name: str = ""
    name_span: Span | None = None
    type_ann: Type | None = None
    value: Expr | None = None
    pub: bool = False  # top-level `pub let` (SPEC.md §11)


@dataclass
class Const(Stmt):
    name: str = ""
    name_span: Span | None = None
    type_ann: Type | None = None
    value: Expr | None = None
    pub: bool = False  # top-level `pub const` (SPEC.md §11)


@dataclass
class Assign(Stmt):
    name: str = ""
    name_span: Span | None = None
    op: str = "="  # = += -= *= /=
    value: Expr | None = None


@dataclass
class If(Stmt):
    cond: Expr | None = None
    then_body: list[Stmt] = field(default_factory=list)
    # (condition, body)
    elifs: list[tuple[Expr, list[Stmt]]] = field(default_factory=list)
    else_body: list[Stmt] | None = None


@dataclass
class For(Stmt):
    var: str = ""
    var_span: Span | None = None
    iterable: Expr | None = None
    body: list[Stmt] = field(default_factory=list)


@dataclass
class While(Stmt):
    cond: Expr | None = None
    body: list[Stmt] = field(default_factory=list)


# ---------------------------------------------------------------- patterns


@dataclass
class Pattern(Node):
    pass


@dataclass
class LitPattern(Pattern):
    value: object = None  # int | float | str | bool | None


@dataclass
class WildcardPattern(Pattern):
    pass  # `_`


@dataclass
class NamePattern(Pattern):
    name: str = ""
    name_span: Span | None = None


@dataclass
class EnumPattern(Pattern):
    enum_name: str = ""
    variant: str = ""
    # (field name, sub-pattern, span of the field name)
    bindings: list[tuple[str, Pattern, Span]] = field(default_factory=list)


@dataclass
class StructPattern(Pattern):
    struct_name: str = ""
    # (field name, sub-pattern, span of the field name)
    fields: list[tuple[str, Pattern, Span]] = field(default_factory=list)


@dataclass
class ListPattern(Pattern):
    elements: list[Pattern] = field(default_factory=list)


@dataclass
class OrPattern(Pattern):
    patterns: list[Pattern] = field(default_factory=list)


@dataclass
class MatchArm:
    pattern: Pattern | None = None
    guard: Expr | None = None
    body: list[Stmt] = field(default_factory=list)
    span: Span | None = None


@dataclass
class Match(Stmt):
    """``match expr:`` — statement in v1 (SPEC.md §9.6, G12)."""

    scrutinee: Expr | None = None
    arms: list[MatchArm] = field(default_factory=list)


@dataclass
class Require(Stmt):
    """``requires <expr>`` — first statement(s) of a fn body (§9.7)."""

    cond: Expr | None = None


@dataclass
class Ensure(Stmt):
    """``ensures <expr>`` — ``result`` is bound to the return value (§9.7)."""

    cond: Expr | None = None


@dataclass
class Try(Stmt):
    body: list[Stmt] = field(default_factory=list)
    # (caught name, name span, body)
    catches: list[tuple[str, Span, list[Stmt]]] = field(default_factory=list)
    finally_body: list[Stmt] | None = None


@dataclass
class Throw(Stmt):
    value: Expr | None = None


@dataclass
class Return(Stmt):
    value: Expr | None = None


@dataclass
class Break(Stmt):
    pass


@dataclass
class Continue(Stmt):
    pass


@dataclass
class Pass(Stmt):
    pass


@dataclass
class Reply(Stmt):
    value: Expr | None = None
    ephemeral: bool = False


@dataclass
class Defer(Stmt):
    pass


@dataclass
class Edit(Stmt):
    value: Expr | None = None


@dataclass
class Followup(Stmt):
    value: Expr | None = None


@dataclass
class Send(Stmt):
    value: Expr | None = None
    channel: Expr | None = None


@dataclass
class Log(Stmt):
    value: Expr | None = None


@dataclass
class ExprStmt(Stmt):
    value: Expr | None = None


# ---------------------------------------------------------------- components


@dataclass
class TextInputDecl:
    name: str
    style: str | None  # short | paragraph
    required: bool
    min_length: int | None
    max_length: int | None
    span: Span


@dataclass
class ButtonDecl(Stmt):
    label: str = ""
    style: str | None = None  # primary | secondary | success | danger | link
    custom_id: str | None = None
    url: str | None = None
    timeout: int | None = None  # seconds; required
    timeout_span: Span | None = None
    only_kind: str = "author"  # author | anyone | roles
    only_roles: list[str] | None = None
    body: list[Stmt] = field(default_factory=list)
    on_timeout: list[Stmt] | None = None


@dataclass
class SelectDecl(Stmt):
    label: str = ""
    options: list[str] = field(default_factory=list)
    min_values: int | None = None
    max_values: int | None = None
    timeout: int | None = None
    timeout_span: Span | None = None
    only_kind: str = "author"
    only_roles: list[str] | None = None
    body: list[Stmt] = field(default_factory=list)
    on_timeout: list[Stmt] | None = None


@dataclass
class ModalDecl(Stmt):
    title: str = ""
    custom_id: str | None = None
    timeout: int | None = None
    timeout_span: Span | None = None
    fields: list[TextInputDecl] = field(default_factory=list)
    body: list[Stmt] = field(default_factory=list)
    on_timeout: list[Stmt] | None = None


# ---------------------------------------------------------------- declarations


@dataclass
class Param:
    name: str
    type: Type
    span: Span


@dataclass
class Import(Decl):
    # Exactly one of path / module is set.
    path: str | None = None  # file module: import "path.newr"
    module: str | None = None  # named module: import math / import discord
    alias: str | None = None  # import "path.newr" as <name>
    # The file this import resolves to (filled in by the resolver).
    resolved_path: str | None = field(default=None, compare=False)

    @property
    def bound_name(self) -> str:
        """The name this import binds (SPEC.md §11).

        The explicit alias, else the named module, else the file stem
        (``import "shop_items.newr"`` binds ``shop_items``).
        """
        if self.alias:
            return self.alias
        if self.module:
            return self.module
        if self.path:
            stem = self.path.rsplit("/", 1)[-1]
            return stem[:-5] if stem.endswith(".newr") else stem
        return ""


@dataclass
class StructField:
    name: str
    type: Type
    span: Span


@dataclass
class StructDecl(Decl):
    name: str = ""
    fields: list[StructField] = field(default_factory=list)
    pub: bool = False


@dataclass
class TestBlock(Decl):
    """A named test block (SPEC.md section 8.7, M6 Phase 2c).

    ``test "adds two numbers":`` plus an indented body. The display
    name is a plain string, so it may contain spaces, unlike a
    function name. The parser lowers the block to a synthetic
    function named ``func_name`` (``_newr_test_<n>``, in source
    order) with the same body, so the resolver, checker, and all
    seven emitters handle it exactly like a `fn` with no params
    and no return value. The display name is what the test runner
    reports; the synthetic name is what the harness calls. User
    code can never use that name: the resolver rejects the
    `_newr_` prefix (E3001).
    """

    name: str = ""
    body: list[Stmt] = field(default_factory=list)
    func_name: str = ""


@dataclass
class FnDecl(Decl):
    name: str = ""
    params: list[Param] = field(default_factory=list)
    return_type: Type | None = None  # None means -> none
    body: list[Stmt] = field(default_factory=list)
    pub: bool = False


@dataclass
class EnumVariant:
    name: str
    # Named associated values: (field name, type, span).
    fields: list[StructField] = field(default_factory=list)
    span: Span | None = None


@dataclass
class EnumDecl(Decl):
    """A typed enum (SPEC.md §8.6, G12). Values are value types (§7.8)."""

    name: str = ""
    variants: list[EnumVariant] = field(default_factory=list)
    pub: bool = False


@dataclass
class BotDecl(Decl):
    name: str = ""
    description: str | None = None
    intents: list[str] | None = None


@dataclass
class OptionDecl(Decl):
    name: str = ""
    name_span: Span | None = None
    type: Type | None = None
    required: bool = False
    default: Expr | None = None
    choices: list[Expr] | None = None
    min_value: Expr | None = None
    max_value: Expr | None = None
    description: str | None = None


@dataclass
class CommandDecl(Decl):
    name: str = ""
    description: str | None = None  # None means "defaults to the name"
    options: list[OptionDecl] = field(default_factory=list)
    # v0 sugar: a fixed reply string (an Interp/Lit expression).
    reply_expr: Expr | None = None
    ephemeral: bool = False
    body: list[Stmt] | None = None  # do: body; exactly one of reply/body

    def effective_body(self) -> list[Stmt]:
        """The body the checker/emitter sees: the do: body, or the
        desugared one-reply body for the v0 ``reply:`` sugar."""
        if self.body is not None:
            return self.body
        if self.reply_expr is not None:
            return [Reply(span=self.reply_expr.span,
                          value=self.reply_expr,
                          ephemeral=self.ephemeral)]
        return []


@dataclass
class EventDecl(Decl):
    kind: str = ""  # ready | member_join | member_remove | message
    log_expr: Expr | None = None  # ready sugar
    reply_expr: Expr | None = None  # member_join/member_remove sugar
    body: list[Stmt] | None = None  # do: body

    def effective_body(self) -> list[Stmt]:
        if self.body is not None:
            return self.body
        if self.log_expr is not None:
            return [Log(span=self.log_expr.span, value=self.log_expr)]
        if self.reply_expr is not None:
            return [Reply(span=self.reply_expr.span, value=self.reply_expr)]
        return []


@dataclass
class TaskDecl(Decl):
    interval_seconds: int = 0
    body: list[Stmt] = field(default_factory=list)


@dataclass
class StringsEntry:
    key: str
    template: str
    # Placeholder names in the template, in order of appearance.
    placeholders: list[str]
    span: Span


@dataclass
class StringsDecl(Decl):
    locale: str = ""
    entries: list[StringsEntry] = field(default_factory=list)


@dataclass
class TableDecl:
    name: str
    columns: list[StructField]  # first column is the key column
    span: Span


@dataclass
class StoreDecl(Decl):
    name: str = ""
    tables: list[TableDecl] = field(default_factory=list)
    ledger: bool = False


@dataclass
class Program(Node):
    # `///` file header (SPEC.md section 6, M6): the leading doc
    # block at the top of the file, followed by a blank line (or
    # end of file). None when the file has no header. Filled in by
    # the parser; a block directly above the first item is that
    # item's doc instead, not the file header.
    file_doc: str | None = None
    imports: list[Import] = field(default_factory=list)
    structs: list[StructDecl] = field(default_factory=list)
    enums: list[EnumDecl] = field(default_factory=list)
    functions: list[FnDecl] = field(default_factory=list)
    # Named test blocks, in source order (SPEC.md section 8.7).
    # Each block also has a synthetic FnDecl in `functions`
    # (same body, name `_newr_test_<n>`), which is what the
    # resolver, checker, and emitters see.
    test_blocks: list[TestBlock] = field(default_factory=list)
    bot: BotDecl | None = None
    commands: list[CommandDecl] = field(default_factory=list)
    events: list[EventDecl] = field(default_factory=list)
    tasks: list[TaskDecl] = field(default_factory=list)
    strings: list[StringsDecl] = field(default_factory=list)
    stores: list[StoreDecl] = field(default_factory=list)
    # Top-level executable statements, in file order.
    stmts: list[Stmt] = field(default_factory=list)
    # The files this program was assembled from (main file first, then
    # imports in resolution order). Filled in by the resolver.
    files: list[str] = field(default_factory=list)
    # Resolved file modules, bound name -> their Program (SPEC.md §11).
    # Empty after parsing; filled in by the resolver. The parser never
    # touches this field.
    modules: dict[str, "Program"] = field(default_factory=dict,
                                          compare=False)
