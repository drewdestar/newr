"""Parser for the newr language (SPEC.md §6-§16).

Recursive descent + Pratt expressions, producing the AST in
``compiler/ast_nodes.py``. Every node carries a byte-offset Span; line/col
are derived at render time and never stored on nodes.

Grammar decisions (SPEC.md is the contract; where it leaves a gap the
decision is recorded here and in the build report):

- ``?.`` is lexed as QDOT and the AST carries it on ``Member.optional``
  (M1 amendment); the checker enforces the ``?.`` typing rule.
- ``??`` is a ``BinOp`` with op ``"??"``; ``|>`` is desugared to a
  ``Call`` (piped value first); ``print`` with no parens is a
  ``Call(Name("print"), args)`` wrapped in ``ExprStmt`` (there is no
  ``Print`` node); bare ``log <expr>`` is a ``Log`` node, while
  ``log.info(...)`` is an ordinary call.
- Unary operand binding powers follow the brief literally: ``-`` parses
  its operand at bp 6 and ``not`` at bp 3, so ``-x * 2`` parses as
  ``-(x * 2)`` and ``not a or b`` as ``not (a or b)``.
- A lone ``|`` lexes as BAR (the match or-pattern separator); the
  parser rejects BAR anywhere else with E1001.
"""

from __future__ import annotations

import bisect
import re
from types import MappingProxyType

from .ast_nodes import (
    Assign, BinOp, BotDecl, Break, ButtonDecl, Call, CommandDecl, Const,
    Continue, Defer, DictLit, Edit, EmbedExpr, EmbedField, Ensure,
    EnumDecl, EnumPattern, EnumVariant, EventDecl, ExprStmt, FnDecl, Followup,
    For, If, Import, Index, Interp, InterpPart, Lambda, Let, ListLit, ListPattern,
    Lit, LitPattern, Log, Match, MatchArm, Member, ModalDecl, Name,
    NamePattern, OptionDecl, OrPattern, Param, Pass, Program, Propagate,
    Reply, Require, Return, SelectDecl, Send, StoreDecl, StructDecl,
    StructField, StructPattern, StringsDecl, StringsEntry, TableDecl,
    TaskDecl, TCall, Ternary, TestBlock, TextInputDecl, Throw, Try, TryExpr, Type,
    UnaryOp, While, WildcardPattern, list_of, nullable_of,
)
from .errors import Diagnostic, NewrError, Span
from .errors import MAX_NESTING_DEPTH
from .lexer import Token, tokenize
from .modules import RESERVED_BUILTIN_NAMES

_PLACEHOLDER_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")

_TOP_DECL_KEYWORDS = {
    "fn", "struct", "enum", "import", "bot", "command", "event", "task",
    "strings", "store", "test",
}
# Keywords that may only appear at the top level (using one inside a
# body is E1001).
_DECL_KEYWORDS = _TOP_DECL_KEYWORDS | {"pub"}

# Binding powers for binary operators (SPEC.md §9.2). Frozen: the
# table is part of the language definition, not a tunable.
# Key: (token kind, keyword value or None).
PRECEDENCE = MappingProxyType({
    ("PIPE", None): 1, ("QQ", None): 2,
    ("KEYWORD", "or"): 3, ("KEYWORD", "and"): 4,
    ("EQ", None): 5, ("NEQ", None): 5, ("LT", None): 5,
    ("LTE", None): 5, ("GT", None): 5, ("GTE", None): 5,
    ("KEYWORD", "in"): 5,
    ("PLUS", None): 6, ("MINUS", None): 6,
    ("STAR", None): 7, ("SLASH", None): 7, ("PERCENT", None): 7,
    ("POWER", None): 8,
})
_CMP_KINDS = {"EQ", "NEQ", "LT", "LTE", "GT", "GTE"}


def _describe(tok: Token) -> str:
    if tok.kind == "EOF":
        return "end of file"
    if tok.kind == "NEWLINE":
        return "end of line"
    if tok.kind == "KEYWORD":
        return f"keyword `{tok.value}`"
    if tok.kind == "IDENT":
        return f"`{tok.value}`"
    if tok.value is not None:
        return f"`{tok.value}`"
    return tok.kind.lower()


class _Parser:
    def __init__(self, source: str, filename: str):
        self.filename = filename
        self.tokens = tokenize(source, filename)
        self.pos = 0
        # Nesting depth of the parse_expr / parse_block / parse_type
        # recursion (M-01): past MAX_NESTING_DEPTH the parser stops
        # with E1001 instead of crashing with RecursionError.
        self._depth = 0
        # Raw byte offset where each 1-based line starts (spans are
        # raw byte offsets, SPEC.md section 16, so this is computed
        # on the source as given, not on the lexer's normalized
        # copy). Used only by _collect_doc to tell whether a doc
        # comment sits directly above its item.
        self._line_starts = [0]
        pos = 0
        for line in source.split("\n")[:-1]:
            pos += len(line.encode("utf-8")) + 1
            self._line_starts.append(pos)

    def _line_no(self, byte_offset: int) -> int:
        """1-based line of a raw byte offset."""
        return bisect.bisect_right(self._line_starts, byte_offset)

    # ------------------------------------------------------------ cursor

    @property
    def cur(self) -> Token:
        return self.tokens[self.pos]

    def peek(self, offset: int = 1) -> Token:
        idx = min(self.pos + offset, len(self.tokens) - 1)
        return self.tokens[idx]

    def advance(self) -> Token:
        tok = self.tokens[self.pos]
        if self.pos < len(self.tokens) - 1:
            self.pos += 1
        return tok

    def at(self, kind: str, value: object = None) -> bool:
        tok = self.cur
        if tok.kind != kind:
            return False
        return value is None or tok.value == value

    def at_kw(self, value: str) -> bool:
        return self.at("KEYWORD", value)

    def at_ident(self, value: str) -> bool:
        return self.at("IDENT", value)

    def span(self, start: Token, end: Token) -> Span:
        return Span(self.filename, start.span.start_byte,
                    end.span.end_byte)

    def span_from(self, start_byte: int, end: Token) -> Span:
        return Span(self.filename, start_byte, end.span.end_byte)

    def err(self, tok: Token, message: str, help: str | None = None):
        raise NewrError(Diagnostic(span=tok.span, code="E1001",
                                   message=message, help=help))

    def _err_span(self, span: Span, message: str,
                  help: str | None = None):
        """Like err(), but for a span that is already computed (e.g.
        ``func.span`` / ``args[i].span``). Passing a Span to err()
        crashes: err() reads ``tok.span`` and a Span has no ``span``
        attribute (CRASH-2: malformed ``t(...)`` calls)."""
        raise NewrError(Diagnostic(span=span, code="E1001",
                                   message=message, help=help))

    def _err_code(self, tok: Token, code: str, message: str,
                  help: str | None = None):
        """Like err(), but with a code other than E1001 (e.g. E3007
        for a reserved builtin name used as a declaration name)."""
        raise NewrError(Diagnostic(span=tok.span, code=code,
                                   message=message, help=help))

    def _check_depth(self, tok: Token) -> None:
        """Raise E1001 once the nesting limit is exceeded (M-01)."""
        self._depth += 1
        if self._depth > MAX_NESTING_DEPTH:
            self._depth -= 1
            self.err(tok, "too deeply nested",
                     help="split the expression or block into smaller "
                          "pieces — newr limits nesting to 200 levels "
                          "so a bad input cannot exhaust the stack")

    def expect(self, kind: str, value: object = None, what: str = "") -> Token:
        if self.at(kind, value):
            return self.advance()
        want = what or _describe(Token(kind, value, self.cur.span))
        self.err(self.cur, f"expected {want}, found {_describe(self.cur)}")

    def expect_kw(self, value: str) -> Token:
        return self.expect("KEYWORD", value, f"`{value}`")

    def expect_ident(self, what: str = "a name") -> Token:
        if self.at("IDENT"):
            return self.advance()
        self.err(self.cur, f"expected {what}, found {_describe(self.cur)}")

    def _skip_newlines(self) -> None:
        while self.at("NEWLINE"):
            self.advance()

    def _expect_eol(self) -> None:
        if self.tokens[self.pos - 1].kind == "DEDENT":
            # A block just ended (e.g. a block-bodied lambda as an RHS):
            # the next token already starts a new statement.
            return
        if not self.at("NEWLINE", None) and self.cur.kind not in (
                "NEWLINE", "DEDENT", "EOF"):
            help = None
            if self.cur.kind == "QMARK":
                # N-05: a `?` here follows a complete statement, so it
                # is not a ternary or a propagate — say what `?` does.
                help = ("`?` propagates a `Result`/`T?` value and goes "
                        "right after the value, like `let x = f()?` — "
                        "here it follows a complete statement")
            self.err(self.cur,
                     f"expected end of line, found {_describe(self.cur)}",
                     help=help)

    def _begin_block(self) -> None:
        if not self.at("NEWLINE"):
            self.err(self.cur,
                     "expected an indented block after `:`",
                     help="put the block body on the next line, "
                          "indented by 2 spaces")
        self.advance()
        if not self.at("INDENT"):
            self.err(self.cur, "expected an indented block after `:`",
                     help="indent the block body by 2 spaces")
        self.advance()

    def _collect_file_doc(self) -> str | None:
        """The file header: a leading `///` block (SPEC.md section 6,
        M6) that documents the file itself.

        A file header is a `///` block before the first item,
        followed by a blank line (or end of file). When the first
        item follows directly below the block, the block is that
        item's doc instead: rewind so the normal loop attaches it.
        Blank lines inside the header are kept (as blank lines in
        the text). Returns None when there is no file header.
        """
        if not self.at("DOC_COMMENT"):
            return None
        save = self.pos
        lines: list[tuple[int, str]] = []
        while self.at("DOC_COMMENT"):
            tok = self.cur
            lines.append((self._line_no(tok.span.start_byte),
                          tok.value or ""))
            self.advance()
            if self.at("NEWLINE"):
                self.advance()
            else:
                break
        if self.at("EOF"):
            return self._join_doc_lines(lines)
        item_line = self._line_no(self.cur.span.start_byte)
        if item_line == lines[-1][0] + 1:
            self.pos = save
            return None
        return self._join_doc_lines(lines)

    @staticmethod
    def _join_doc_lines(lines: list[tuple[int, str]]) -> str:
        """Join (line, text) doc lines, keeping blank lines between
        them."""
        parts: list[str] = []
        prev: int | None = None
        for line_no, text in lines:
            if prev is not None:
                parts.append("\n" * (line_no - prev))
            parts.append(text)
            prev = line_no
        return "".join(parts)

    def _collect_doc(self) -> str | None:
        """Collect consecutive `///` doc-comment lines (SPEC.md
        section 6, M6) and return their text joined with "\\n".

        A doc comment documents the item directly below it: the doc
        is the trailing run of `///` lines ending on the line right
        above the item. A blank line anywhere in the run, or between
        the run and the item, breaks the attachment: only the lines
        directly above the item count, the rest document nothing.
        The lexer collapses blank lines into one NEWLINE token, so
        adjacency is judged by line numbers, not by tokens. Returns
        None when no `///` line sits directly above the next item.
        """
        if not self.at("DOC_COMMENT"):
            return None
        lines: list[tuple[int, str]] = []
        while self.at("DOC_COMMENT"):
            tok = self.cur
            lines.append((self._line_no(tok.span.start_byte),
                          tok.value or ""))
            self.advance()
            if self.at("NEWLINE"):
                self.advance()
            else:
                break
        if self.at("EOF") or self.at("DEDENT"):
            return None
        item_line = self._line_no(self.cur.span.start_byte)
        doc_lines = []
        expect = item_line - 1
        for line_no, text in reversed(lines):
            if line_no != expect:
                break
            doc_lines.append(text)
            expect -= 1
        if not doc_lines:
            return None
        doc_lines.reverse()
        return "\n".join(doc_lines)

    def _parse_lines(self, line_fn) -> None:
        """Parse an indented key/value-style block (one line_fn per line)."""
        self._begin_block()
        while True:
            self._skip_newlines()
            if self.at("DEDENT"):
                self.advance()
                return
            if self.at("EOF"):
                self.err(self.cur,
                         "unexpected end of file; expected the end of "
                         "the block")
            line_fn()

    def parse_block(self) -> list:
        """Parse an indented statement block; returns the statements."""
        self._check_depth(self.cur)
        try:
            return self._parse_block_inner()
        finally:
            self._depth -= 1

    def _parse_block_inner(self) -> list:
        self._begin_block()
        stmts = []
        while True:
            self._skip_newlines()
            if self.at("DEDENT"):
                self.advance()
                return stmts
            if self.at("EOF"):
                self.err(self.cur,
                         "unexpected end of file; expected the end of "
                         "the block")
            doc = self._collect_doc()
            self._skip_newlines()
            if self.at("DEDENT"):
                self.advance()
                return stmts
            if self.at("EOF"):
                self.err(self.cur,
                         "unexpected end of file; expected the end of "
                         "the block")
            stmt = self._parse_stmt()
            if doc is not None:
                stmt.doc = doc
            stmts.append(stmt)
            if self.at("NEWLINE"):
                self.advance()

    # ------------------------------------------------------------ program

    def parse_program(self) -> Program:
        start_byte = 0
        prog = Program(span=Span(self.filename, 0, 0))
        self._skip_newlines()
        prog.file_doc = self._collect_file_doc()
        self._skip_newlines()
        while True:
            self._skip_newlines()
            if self.at("EOF"):
                break
            if self.at("INDENT"):
                self.err(self.cur,
                         "indented line outside any block",
                         help="top-level lines start at column 0")
            doc = self._collect_doc()
            self._skip_newlines()
            if self.at("EOF"):
                break
            self._parse_top_level(prog, doc)
            if self.at("NEWLINE"):
                self.advance()
        end_byte = self.tokens[-1].span.end_byte
        prog.span = Span(self.filename, start_byte, end_byte)
        return prog

    def _parse_top_level(self, prog: Program,
                         doc: str | None = None) -> None:
        node = self._parse_top_level_node(prog)
        if doc is not None and node is not None:
            node.doc = doc

    def _parse_top_level_node(self, prog: Program):
        if self.at_kw("pub"):
            start = self.advance()
            nxt = self.cur
            if self.at_kw("fn"):
                node = self._parse_fn(pub=True)
                prog.functions.append(node)
                return node
            elif self.at_kw("struct"):
                node = self._parse_struct(pub=True)
                prog.structs.append(node)
                return node
            elif self.at_kw("enum"):
                node = self._parse_enum(pub=True)
                prog.enums.append(node)
                return node
            elif self.at_kw("let") or self.at_kw("const"):
                node = self._parse_let_const(pub=True)
                prog.stmts.append(node)
                return node
            else:
                self.err(nxt, f"expected a declaration after `pub`, "
                              f"found {_describe(nxt)}",
                         help="`pub` goes before `fn`, `struct`, `enum`, "
                              "`let`, or `const`")
            return
        if self.at_kw("import"):
            node = self._parse_import()
            prog.imports.append(node)
            return node
        elif self.at_kw("fn"):
            node = self._parse_fn()
            prog.functions.append(node)
            return node
        elif self.at_kw("test"):
            node = self._parse_test_block(prog)
            prog.test_blocks.append(node)
            return node
        elif self.at_kw("struct"):
            node = self._parse_struct()
            prog.structs.append(node)
            return node
        elif self.at_kw("enum"):
            node = self._parse_enum()
            prog.enums.append(node)
            return node
        elif self.at_kw("bot"):
            if prog.bot is not None:
                self.err(self.cur, "only one `bot` block is allowed "
                                   "per file")
            node = self._parse_bot()
            prog.bot = node
            return node
        elif self.at_kw("command"):
            node = self._parse_command()
            prog.commands.append(node)
            return node
        elif self.at_kw("event"):
            node = self._parse_event()
            prog.events.append(node)
            return node
        elif self.at_kw("task"):
            node = self._parse_task()
            prog.tasks.append(node)
            return node
        elif self.at_kw("strings"):
            node = self._parse_strings()
            prog.strings.append(node)
            return node
        elif self.at_kw("store"):
            node = self._parse_store()
            prog.stores.append(node)
            return node
        else:
            node = self._parse_stmt()
            prog.stmts.append(node)
            return node

    # ------------------------------------------------------------ imports

    def _parse_import(self):
        start = self.expect_kw("import")
        path = module = alias = None
        if self.at("STR_START"):
            value, _span = self._parse_plain_string()
            path = value
        elif self.at("IDENT"):
            parts = [self.advance().value]
            while self.at("DOT"):
                self.advance()
                parts.append(self.expect_ident("a module name").value)
            module = ".".join(parts)
        else:
            self.err(self.cur,
                     f"expected a module name or a file path after "
                     f"`import`, found {_describe(self.cur)}")
        if self.at_kw("as"):
            self.advance()
            alias = self.expect_ident("a name after `as`").value
        return Import(span=self.span(start, self.tokens[self.pos - 1]),
                      path=path, module=module, alias=alias)

    # ------------------------------------------------------------ types

    def parse_type(self) -> Type:
        self._check_depth(self.cur)
        try:
            return self._parse_type_inner()
        finally:
            self._depth -= 1

    def _parse_type_inner(self) -> Type:
        start = self.cur
        if self.at_kw("fn"):
            self.advance()
            self.expect("LPAREN", None, "`(`")
            params = []
            if not self.at("RPAREN"):
                while True:
                    params.append(self.parse_type())
                    if self.at("COMMA"):
                        self.advance()
                        if self.at("RPAREN"):
                            break
                        continue
                    break
            self.expect("RPAREN", None, "`)`")
            self.expect("ARROW", None, "`->`")
            ret = self.parse_type()
            base = Type("fn", tuple(params) + (ret,))
        elif self.at("IDENT") or self.at_kw("none") or self.at_kw("embed"):
            # `embed` is a keyword but also a type name (§7.1); `none`
            # is a keyword and the type of the `none` literal.
            name = self.advance().value
            params = []
            if self.at("LBRACK"):
                self.advance()
                while True:
                    params.append(self.parse_type())
                    if self.at("COMMA"):
                        self.advance()
                        continue
                    break
                self.expect("RBRACK", None, "`]`")
            base = Type(name, tuple(params))
        else:
            self.err(self.cur,
                     f"expected a type, found {_describe(self.cur)}")
        end = self.tokens[self.pos - 1]
        base = Type(base.name, base.params,
                    span=self.span(start, end), nullable=base.nullable)
        if self.at("QMARK"):
            q = self.advance()
            base = Type(base.name, base.params,
                        span=self.span(start, q), nullable=True)
        return base

    def _parse_params(self) -> list[Param]:
        self.expect("LPAREN", None, "`(`")
        params = []
        if not self.at("RPAREN"):
            while True:
                name_tok = self.expect_ident("a parameter name")
                self.expect("COLON", None, "`:`")
                typ = self.parse_type()
                params.append(Param(name=name_tok.value, type=typ,
                                    span=self.span(name_tok,
                                                   self.tokens[self.pos - 1])))
                if self.at("COMMA"):
                    self.advance()
                    if self.at("RPAREN"):
                        break
                    continue
                break
        self.expect("RPAREN", None, "`)`")
        return params

    # ------------------------------------------------------------ fn / struct / enum

    def _parse_fn(self, pub: bool = False) -> FnDecl:
        start = self.expect_kw("fn")
        name_tok = self.expect_ident("a function name")
        params = self._parse_params()
        return_type = None
        if self.at("ARROW"):
            self.advance()
            return_type = self.parse_type()
        self.expect("COLON", None, "`:` after the function header")
        body = self.parse_block()
        end = self.tokens[self.pos - 1]
        return FnDecl(span=self.span(start, end), name=name_tok.value,
                      params=params, return_type=return_type, body=body,
                      pub=pub)

    def _parse_test_block(self, prog: Program) -> TestBlock:
        """Parse `test "display name":` plus an indented body
        (SPEC.md section 8.7). The name is a plain string (no
        interpolation, via `_parse_plain_string`), so it may
        contain spaces. The block is lowered to a synthetic
        function named `_newr_test_<n>` (n is the block's index
        in source order) sharing the same body list, and that
        function is appended to `prog.functions` so the resolver,
        checker, and emitters handle the block exactly like a
        parameterless `fn` with no return value. The display name
        is what `newr test` reports; the synthetic name is what
        its harness calls."""
        start = self.expect_kw("test")
        name, name_span = self._parse_plain_string()
        if not name:
            self._err_span(name_span,
                           "a test block needs a non-empty name",
                           help="write a name, like "
                                "`test \"adds two numbers\":`")
        if "\n" in name or "\r" in name:
            self._err_span(
                name_span,
                "a test block name cannot contain a newline",
                help="a test name is one line: the runner prints it "
                     "as one marker line")
        self.expect("COLON", None, "`:` after the test name")
        body = self.parse_block()
        end = self.tokens[self.pos - 1]
        span = self.span(start, end)
        func_name = f"_newr_test_{len(prog.test_blocks)}"
        prog.functions.append(FnDecl(
            span=span, name=func_name, params=[], return_type=None,
            body=body, pub=False))
        return TestBlock(span=span, name=name, body=body,
                         func_name=func_name)

    def _parse_struct(self, pub: bool = False) -> StructDecl:
        start = self.expect_kw("struct")
        name_tok = self.expect_ident("a struct name")
        self.expect("COLON", None, "`:` after the struct name")
        fields: list[StructField] = []

        def line() -> None:
            name = self.expect_ident("a field name")
            self.expect("COLON", None, "`:`")
            typ = self.parse_type()
            fields.append(StructField(
                name=name.value, type=typ,
                span=self.span(name, self.tokens[self.pos - 1])))

        self._parse_lines(line)
        return StructDecl(span=self.span(start, self.tokens[self.pos - 1]),
                          name=name_tok.value, fields=fields, pub=pub)

    def _parse_enum(self, pub: bool = False) -> EnumDecl:
        start = self.expect_kw("enum")
        name_tok = self.expect_ident("an enum name")
        self.expect("COLON", None, "`:` after the enum name")
        variants: list[EnumVariant] = []

        def line() -> None:
            name = self.expect_ident("a variant name")
            fields: list[StructField] = []
            if self.at("LPAREN"):
                self.advance()
                if not self.at("RPAREN"):
                    while True:
                        fname = self.expect_ident("a field name")
                        self.expect("COLON", None, "`:`")
                        typ = self.parse_type()
                        fields.append(StructField(
                            name=fname.value, type=typ,
                            span=self.span(fname,
                                           self.tokens[self.pos - 1])))
                        if self.at("COMMA"):
                            self.advance()
                            continue
                        break
                self.expect("RPAREN", None, "`)`")
            variants.append(EnumVariant(
                name=name.value, fields=fields,
                span=self.span(name, self.tokens[self.pos - 1])))

        self._parse_lines(line)
        return EnumDecl(span=self.span(start, self.tokens[self.pos - 1]),
                        name=name_tok.value, variants=variants, pub=pub)

    # ------------------------------------------------------------ strings

    def _parse_string_expr(self):
        """Parse STR_START ... STR_END into a Lit or an Interp."""
        start = self.expect("STR_START", None, "a string")
        parts: list[InterpPart] = []
        while True:
            if self.at("STR_PART"):
                parts.append(InterpPart(text=self.advance().value))
            elif self.at("INTERP_START"):
                self.advance()
                expr = self.parse_expr(0)
                self.expect("INTERP_END", None, "`}`")
                parts.append(InterpPart(expr=expr))
            elif self.at("STR_END"):
                end = self.advance()
                break
            else:
                self.err(self.cur, "unterminated string")
        span = self.span(start, end)
        if not parts:
            return Lit(span=span, value="")
        if len(parts) == 1 and parts[0].text is not None:
            return Lit(span=span, value=parts[0].text)
        return Interp(span=span, parts=parts)

    def _parse_plain_string(self) -> tuple[str, Span]:
        expr = self._parse_string_expr()
        if not isinstance(expr, Lit) or not isinstance(expr.value, str):
            self.err(self.cur,
                     "expected a plain string here (no interpolation)",
                     help="use a string without `{...}` in it")
        return expr.value, expr.span

    def _parse_template_string(self) -> tuple[str, Span]:
        """A strings-block template: rebuild the raw text, keeping
        ``{name}`` placeholders (they must be simple names)."""
        start = self.expect("STR_START", None, "a string")
        text = ""
        while True:
            if self.at("STR_PART"):
                text += self.advance().value
            elif self.at("INTERP_START"):
                self.advance()
                if self.at("IDENT") and self.peek().kind == "INTERP_END":
                    text += "{" + self.advance().value + "}"
                    self.advance()
                else:
                    self.err(self.cur,
                             "a strings template placeholder must be a "
                             "simple name, like `{item}`")
            elif self.at("STR_END"):
                end = self.advance()
                break
            else:
                self.err(self.cur, "unterminated string")
        return text, self.span(start, end)

    def _parse_bool_lit(self) -> bool:
        if self.at_kw("true"):
            self.advance()
            return True
        if self.at_kw("false"):
            self.advance()
            return False
        self.err(self.cur,
                 f"expected `true` or `false`, found {_describe(self.cur)}")

    def _parse_duration(self, what: str = "a duration") -> tuple[int, Span]:
        if not self.at("DURATION"):
            self.err(self.cur, f"expected {what}, found "
                               f"{_describe(self.cur)}",
                     help="durations look like `60s`, `5m`, `1h`, `1d`")
        tok = self.advance()
        return tok.value, tok.span

    def _parse_name_list(self) -> list[str]:
        """``[a, b]`` of bare names or string literals."""
        self.expect("LBRACK", None, "`[`")
        names: list[str] = []
        if not self.at("RBRACK"):
            while True:
                if self.at("IDENT"):
                    names.append(self.advance().value)
                elif self.at("STR_START"):
                    value, _ = self._parse_plain_string()
                    names.append(value)
                else:
                    self.err(self.cur,
                             f"expected a name, found "
                             f"{_describe(self.cur)}")
                if self.at("COMMA"):
                    self.advance()
                    continue
                break
        self.expect("RBRACK", None, "`]`")
        return names

    # ------------------------------------------------------------ bot / command / event

    def _parse_bot(self) -> BotDecl:
        start = self.expect_kw("bot")
        name, _ = self._parse_plain_string()
        self.expect("COLON", None, "`:` after the bot name")
        description: str | None = None
        intents: list[str] | None = None

        def line() -> None:
            nonlocal description, intents
            if self.at_ident("description"):
                self.advance()
                self.expect("COLON", None, "`:`")
                expr = self.parse_expr(0)
                if isinstance(expr, Lit) and isinstance(expr.value, str):
                    description = expr.value
                else:
                    self.err(self.cur,
                             "a bot description must be a string")
            elif self.at_ident("intents"):
                self.advance()
                self.expect("COLON", None, "`:`")
                intents = self._parse_name_list()
            else:
                self.err(self.cur,
                         f"unknown line in a `bot` block: "
                         f"{_describe(self.cur)}",
                         help="a `bot` block takes `description:` and "
                              "`intents:`")

        self._parse_lines(line)
        return BotDecl(span=self.span(start, self.tokens[self.pos - 1]),
                       name=name, description=description, intents=intents)

    def _parse_command_name(self) -> str:
        first = self.cur
        if first.kind not in ("IDENT", "MINUS", "INT"):
            self.err(first,
                     f"expected a command name, found {_describe(first)}")
        parts: list[str] = []
        prev_end = None
        while self.cur.kind in ("IDENT", "MINUS", "INT"):
            if prev_end is not None and \
                    self.cur.span.start_byte != prev_end:
                break
            parts.append(str(self.cur.value))
            prev_end = self.advance().span.end_byte
        if self.cur.kind in ("IDENT", "MINUS", "INT"):
            self.err(self.cur,
                     "subcommands arrive in v1.1",
                     help="use a single command name, like `command buy:`")
        return "".join(parts)

    def _parse_option(self) -> OptionDecl:
        start = self.expect_ident("option")
        name_tok = self.expect_ident("an option name")
        self.expect("COLON", None, "`:`")
        typ = self.parse_type()
        if typ.name == "num":
            typ = Type("float", span=typ.span)
        required = False
        default = choices = min_value = max_value = None
        description = None
        if self.at("LPAREN"):
            self.advance()
            if not self.at("RPAREN"):
                while True:
                    key_tok = self.expect_ident("an option modifier")
                    key = key_tok.value
                    if key == "required" and not self.at("COLON"):
                        required = True
                    elif self.at("COLON"):
                        self.advance()
                        if key == "default":
                            default = self.parse_expr(0)
                        elif key == "choices":
                            expr = self.parse_expr(0)
                            if not isinstance(expr, ListLit):
                                self.err(self.cur,
                                         "`choices:` must be a list, "
                                         "like `[\"a\", \"b\"]`")
                            choices = expr.elements
                        elif key == "min":
                            min_value = self.parse_expr(0)
                        elif key == "max":
                            max_value = self.parse_expr(0)
                        elif key == "description":
                            expr = self.parse_expr(0)
                            if not isinstance(expr, Lit) or \
                                    not isinstance(expr.value, str):
                                self.err(self.cur,
                                         "`description:` must be a string")
                            description = expr.value
                        else:
                            self.err(key_tok,
                                     f"unknown option modifier `{key}`",
                                     help="modifiers are `required`, "
                                          "`default:`, `choices:`, `min:`, "
                                          "`max:`, `description:`")
                    else:
                        self.err(key_tok,
                                 f"unknown option modifier `{key}`",
                                 help="a bare modifier must be `required`")
                    if self.at("COMMA"):
                        self.advance()
                        continue
                    break
            self.expect("RPAREN", None, "`)`")
        return OptionDecl(
            span=self.span(start, self.tokens[self.pos - 1]),
            name=name_tok.value, name_span=name_tok.span, type=typ,
            required=required, default=default, choices=choices,
            min_value=min_value, max_value=max_value,
            description=description)

    def _parse_command(self) -> CommandDecl:
        start = self.expect_kw("command")
        name = self._parse_command_name()
        self.expect("COLON", None, "`:` after the command name")
        description = None
        options: list[OptionDecl] = []
        reply_expr = None
        ephemeral = False
        body = None

        def line() -> None:
            nonlocal description, reply_expr, ephemeral, body
            if self.at_ident("description"):
                self.advance()
                self.expect("COLON", None, "`:`")
                expr = self.parse_expr(0)
                if isinstance(expr, Lit) and isinstance(expr.value, str):
                    description = expr.value
                else:
                    self.err(self.cur,
                             "a command description must be a string")
            elif self.at_kw("reply"):
                if reply_expr is not None or body is not None:
                    self.err(self.cur,
                             "a command may have at most one of `reply:` "
                             "and `do:`")
                self.advance()
                self.expect("COLON", None, "`:`")
                reply_expr = self.parse_expr(0)
            elif self.at_ident("ephemeral"):
                self.advance()
                self.expect("COLON", None, "`:`")
                ephemeral = self._parse_bool_lit()
            elif self.at_ident("option"):
                options.append(self._parse_option())
            elif self.at_kw("do"):
                if reply_expr is not None or body is not None:
                    self.err(self.cur,
                             "a command may have at most one of `reply:` "
                             "and `do:`")
                self.advance()
                self.expect("COLON", None, "`:` after `do`")
                body = self.parse_block()
            else:
                self.err(self.cur,
                         f"unknown line in a `command` block: "
                         f"{_describe(self.cur)}",
                         help="a `command` block takes `description:`, "
                              "`reply:`, `ephemeral:`, `option` lines, "
                              "and `do:`")

        self._parse_lines(line)
        if reply_expr is None and body is None:
            self.err(start,
                     f"command `{name}` must have either `reply:` or `do:`",
                     help="add a `reply: \"...\"` line or a `do:` body")
        return CommandDecl(
            span=self.span(start, self.tokens[self.pos - 1]),
            name=name, description=description, options=options,
            reply_expr=reply_expr, ephemeral=ephemeral, body=body)

    def _parse_event(self) -> EventDecl:
        start = self.expect_kw("event")
        kind_tok = self.expect_ident("an event kind")
        kind = kind_tok.value
        if kind not in ("ready", "member_join", "member_remove", "message"):
            self.err(kind_tok, f"unknown event kind `{kind}`",
                     help="events are `ready`, `member_join`, "
                          "`member_remove`, `message`")
        self.expect("COLON", None, "`:` after the event kind")
        log_expr = reply_expr = None
        body = None

        def line() -> None:
            nonlocal log_expr, reply_expr, body
            if self.at_kw("log"):
                if log_expr is not None or reply_expr is not None or \
                        body is not None:
                    self.err(self.cur,
                             "an event may have only one of `log:`, "
                             "`reply:`, `do:`")
                self.advance()
                self.expect("COLON", None, "`:`")
                log_expr = self.parse_expr(0)
            elif self.at_kw("reply"):
                if log_expr is not None or reply_expr is not None or \
                        body is not None:
                    self.err(self.cur,
                             "an event may have only one of `log:`, "
                             "`reply:`, `do:`")
                self.advance()
                self.expect("COLON", None, "`:`")
                reply_expr = self.parse_expr(0)
            elif self.at_kw("do"):
                if log_expr is not None or reply_expr is not None or \
                        body is not None:
                    self.err(self.cur,
                             "an event may have only one of `log:`, "
                             "`reply:`, `do:`")
                self.advance()
                self.expect("COLON", None, "`:` after `do`")
                body = self.parse_block()
            else:
                self.err(self.cur,
                         f"unknown line in an `event` block: "
                         f"{_describe(self.cur)}",
                         help="an `event` block takes `log:`, `reply:`, "
                              "and `do:`")

        self._parse_lines(line)
        if log_expr is None and reply_expr is None and body is None:
            self.err(start,
                     f"event `{kind}` must have `log:`, `reply:`, or `do:`")
        return EventDecl(span=self.span(start, self.tokens[self.pos - 1]),
                         kind=kind, log_expr=log_expr,
                         reply_expr=reply_expr, body=body)

    # ------------------------------------------------------------ task / strings / store

    def _parse_task(self) -> TaskDecl:
        start = self.expect_kw("task")
        if not self.at_ident("every"):
            self.err(self.cur,
                     f"expected `every` after `task`, found "
                     f"{_describe(self.cur)}",
                     help="tasks look like `task every 1h:`")
        self.advance()
        interval, _span = self._parse_duration("a duration after `every`")
        self.expect("COLON", None, "`:` after the task header")
        body = None

        def line() -> None:
            nonlocal body
            if self.at_kw("do"):
                if body is not None:
                    self.err(self.cur, "a task may have only one `do:`")
                self.advance()
                self.expect("COLON", None, "`:` after `do`")
                body = self.parse_block()
            else:
                self.err(self.cur,
                         f"unknown line in a `task` block: "
                         f"{_describe(self.cur)}",
                         help="a `task` block takes a `do:` body")

        self._parse_lines(line)
        if body is None:
            self.err(start, "a task must have a `do:` body")
        return TaskDecl(span=self.span(start, self.tokens[self.pos - 1]),
                        interval_seconds=interval, body=body)

    def _parse_locale(self) -> str:
        first = self.expect_ident("a locale, like `en` or `en-US`")
        parts = [first.value]
        prev_end = first.span.end_byte
        while self.at("MINUS") and \
                self.cur.span.start_byte == prev_end:
            self.advance()
            tok = self.expect_ident("a locale part after `-`")
            if tok.span.start_byte != prev_end + 1:
                break
            parts.append(tok.value)
            prev_end = tok.span.end_byte
        return "-".join(parts)

    def _parse_strings(self) -> StringsDecl:
        start = self.expect_kw("strings")
        locale = self._parse_locale()
        self.expect("COLON", None, "`:` after the locale")
        entries: list[StringsEntry] = []

        def line() -> None:
            key, key_span = self._parse_plain_string()
            self.expect("COLON", None, "`:`")
            template, _ = self._parse_template_string()
            placeholders = _PLACEHOLDER_RE.findall(template)
            entries.append(StringsEntry(
                key=key, template=template, placeholders=placeholders,
                span=self.span_from(key_span.start_byte,
                                    self.tokens[self.pos - 1])))

        self._parse_lines(line)
        return StringsDecl(span=self.span(start, self.tokens[self.pos - 1]),
                           locale=locale, entries=entries)

    def _parse_store(self) -> StoreDecl:
        start = self.expect_kw("store")
        name_tok = self.expect_ident("a store name")
        self.expect("COLON", None, "`:` after the store name")
        tables: list[TableDecl] = []
        ledger = False

        def line() -> None:
            nonlocal ledger
            if self.at_ident("table"):
                tstart = self.advance()
                tname = self.expect_ident("a table name")
                self.expect("LPAREN", None, "`(`")
                columns: list[StructField] = []
                if not self.at("RPAREN"):
                    while True:
                        cname = self.expect_ident("a column name")
                        self.expect("COLON", None, "`:`")
                        typ = self.parse_type()
                        columns.append(StructField(
                            name=cname.value, type=typ,
                            span=self.span(cname,
                                           self.tokens[self.pos - 1])))
                        if self.at("COMMA"):
                            self.advance()
                            continue
                        break
                self.expect("RPAREN", None, "`)`")
                tables.append(TableDecl(
                    name=tname.value, columns=columns,
                    span=self.span(tstart, self.tokens[self.pos - 1])))
            elif self.at_ident("ledger"):
                self.advance()
                # `ledger true` — no colon (SPEC.md §14).
                ledger = self._parse_bool_lit()
            else:
                self.err(self.cur,
                         f"unknown line in a `store` block: "
                         f"{_describe(self.cur)}",
                         help="a `store` block takes `table ...` lines "
                              "and `ledger true/false`")

        self._parse_lines(line)
        return StoreDecl(span=self.span(start, self.tokens[self.pos - 1]),
                         name=name_tok.value, tables=tables, ledger=ledger)

    # ------------------------------------------------------------ statements

    def _maybe_propagate(self, expr):
        """Statement-level ``?``: wrap the RHS in a Propagate node."""
        if self.at("QMARK"):
            q = self.advance()
            return Propagate(span=self.span_from(expr.span.start_byte, q),
                             expr=expr)
        return expr

    def _parse_let_const(self, pub: bool = False):
        start = self.cur
        is_const = self.at_kw("const")
        self.advance()
        # N-05: `log` lexes as a keyword, so `let log = 1` used to die
        # as E1001 while `let print = 1` correctly gets E3007 from the
        # resolver. A reserved builtin name is E3007 everywhere.
        if self.at("KEYWORD") and self.cur.value in RESERVED_BUILTIN_NAMES:
            self._err_code(self.cur, "E3007",
                           f"`{self.cur.value}` is a builtin name and "
                           "cannot be redefined",
                           help="rename the declaration")
        name_tok = self.expect_ident("a variable name")
        type_ann = None
        if self.at("COLON"):
            self.advance()
            type_ann = self.parse_type()
        self.expect("ASSIGN", None, "`=`")
        value = self.parse_expr(0)
        value = self._maybe_propagate(value)
        self._expect_eol()
        span = self.span(start, self.tokens[self.pos - 1])
        if is_const:
            return Const(span=span, name=name_tok.value,
                         name_span=name_tok.span, type_ann=type_ann,
                         value=value, pub=pub)
        return Let(span=span, name=name_tok.value,
                   name_span=name_tok.span, type_ann=type_ann,
                   value=value, pub=pub)

    def _parse_stmt(self):
        tok = self.cur
        if tok.kind == "KEYWORD":
            kw = tok.value
            if kw in ("let", "const"):
                return self._parse_let_const()
            if kw == "if":
                return self._parse_if()
            if kw == "for":
                return self._parse_for()
            if kw == "while":
                return self._parse_while()
            if kw == "try" and self.peek().kind == "COLON":
                return self._parse_try()
            if kw == "throw":
                self.advance()
                value = self.parse_expr(0)
                self._expect_eol()
                return Throw(span=self.span(tok, self.tokens[self.pos - 1]),
                             value=value)
            if kw == "return":
                self.advance()
                if self.cur.kind in ("NEWLINE", "DEDENT", "EOF"):
                    self._expect_eol()
                    return Return(span=self.span(tok, tok), value=None)
                value = self.parse_expr(0)
                value = self._maybe_propagate(value)
                self._expect_eol()
                return Return(span=self.span(tok, self.tokens[self.pos - 1]),
                              value=value)
            if kw == "break":
                self.advance()
                self._expect_eol()
                return Break(span=tok.span)
            if kw == "continue":
                self.advance()
                self._expect_eol()
                return Continue(span=tok.span)
            if kw == "pass":
                self.advance()
                self._expect_eol()
                return Pass(span=tok.span)
            if kw == "match":
                return self._parse_match()
            if kw == "requires":
                self.advance()
                cond = self.parse_expr(0)
                self._expect_eol()
                return Require(span=self.span(tok, self.tokens[self.pos - 1]),
                               cond=cond)
            if kw == "ensures":
                self.advance()
                cond = self.parse_expr(0)
                self._expect_eol()
                return Ensure(span=self.span(tok, self.tokens[self.pos - 1]),
                              cond=cond)
            if kw == "reply":
                self.advance()
                value = self.parse_expr(0)
                value = self._maybe_propagate(value)
                ephemeral = False
                if self.at_ident("ephemeral"):
                    self.advance()
                    ephemeral = True
                self._expect_eol()
                return Reply(span=self.span(tok, self.tokens[self.pos - 1]),
                             value=value, ephemeral=ephemeral)
            if kw == "send":
                self.advance()
                value = self.parse_expr(0)
                value = self._maybe_propagate(value)
                if not self.at_ident("to"):
                    self.err(self.cur,
                             f"expected `to` after the message in `send`, "
                             f"found {_describe(self.cur)}",
                             help="`send <message> to <channel>`")
                self.advance()
                channel = self.parse_expr(0)
                self._expect_eol()
                return Send(span=self.span(tok, self.tokens[self.pos - 1]),
                            value=value, channel=channel)
            if kw == "log":
                return self._parse_log()
            if kw == "button":
                return self._parse_button()
            if kw == "select":
                return self._parse_select()
            if kw == "modal":
                return self._parse_modal()
            if kw == "fn" and self.peek().kind == "LPAREN":
                value = self.parse_expr(0)
                self._expect_eol()
                return ExprStmt(span=value.span, value=value)
            if kw in _DECL_KEYWORDS:
                self.err(tok, f"`{kw}` may only appear at the top level",
                         help="declarations cannot appear inside a body")
            self.err(tok, f"unexpected keyword `{kw}` here")
        if tok.kind == "IDENT":
            word = tok.value
            if word == "defer":
                self.advance()
                self._expect_eol()
                return Defer(span=tok.span)
            if word == "edit":
                self.advance()
                value = self.parse_expr(0)
                value = self._maybe_propagate(value)
                self._expect_eol()
                return Edit(span=self.span(tok, self.tokens[self.pos - 1]),
                            value=value)
            if word == "followup":
                self.advance()
                value = self.parse_expr(0)
                value = self._maybe_propagate(value)
                self._expect_eol()
                return Followup(
                    span=self.span(tok, self.tokens[self.pos - 1]),
                    value=value)
            if word == "print" and not self.peek().kind == "LPAREN":
                return self._parse_print()
            nxt = self.peek()
            if nxt.kind in ("ASSIGN", "PLUS_EQ", "MINUS_EQ", "STAR_EQ",
                            "SLASH_EQ"):
                self.advance()
                op_tok = self.advance()
                value = self.parse_expr(0)
                value = self._maybe_propagate(value)
                self._expect_eol()
                return Assign(span=self.span(tok, self.tokens[self.pos - 1]),
                              name=tok.value, name_span=tok.span,
                              op=op_tok.value, value=value)
        # Expression statement (also the `try <expr>` form).
        value = self.parse_expr(0)
        if self.at("ASSIGN", None) or self.cur.kind in (
                "PLUS_EQ", "MINUS_EQ", "STAR_EQ", "SLASH_EQ"):
            self.err(self.cur,
                     "assignment target must be a name",
                     help="assign to a variable, like `x = 1`")
        value = self._maybe_propagate(value)
        self._expect_eol()
        return ExprStmt(span=value.span, value=value)

    def _parse_print(self):
        start = self.expect_ident("print")
        args: list = []
        if not self.at("NEWLINE") and self.cur.kind not in ("DEDENT", "EOF"):
            while True:
                args.append(self.parse_expr(0))
                if self.at("COMMA"):
                    self.advance()
                    continue
                break
        self._expect_eol()
        span = self.span(start, self.tokens[self.pos - 1]) \
            if args else start.span
        func = Name(span=start.span, name="print")
        return ExprStmt(span=span,
                        value=Call(span=span, func=func, args=args))

    def _parse_log(self):
        start = self.cur
        if self.peek().kind in ("DOT", "LPAREN"):
            # `log.info(...)` / `log(...)`: a normal call. Do not consume
            # `log` here — parse_expr treats the keyword as a Name.
            value = self.parse_expr(0)
            value = self._maybe_propagate(value)
            self._expect_eol()
            return ExprStmt(span=value.span, value=value)
        self.advance()  # `log`
        value = self.parse_expr(0)
        value = self._maybe_propagate(value)
        self._expect_eol()
        return Log(span=self.span(start, self.tokens[self.pos - 1]),
                   value=value)

    def _parse_if(self) -> If:
        start = self.expect_kw("if")
        cond = self.parse_expr(0)
        self.expect("COLON", None, "`:` after the `if` condition")
        then_body = self.parse_block()
        elifs = []
        else_body = None
        while self.at_kw("elif"):
            self.advance()
            econd = self.parse_expr(0)
            self.expect("COLON", None, "`:` after the `elif` condition")
            ebody = self.parse_block()
            elifs.append((econd, ebody))
        if self.at_kw("else"):
            self.advance()
            self.expect("COLON", None, "`:` after `else`")
            else_body = self.parse_block()
        return If(span=self.span(start, self.tokens[self.pos - 1]),
                  cond=cond, then_body=then_body, elifs=elifs,
                  else_body=else_body)

    def _parse_for(self) -> For:
        start = self.expect_kw("for")
        var_tok = self.expect_ident("a loop variable")
        self.expect_kw("in")
        iterable = self.parse_expr(0)
        self.expect("COLON", None, "`:` after the `for` header")
        body = self.parse_block()
        return For(span=self.span(start, self.tokens[self.pos - 1]),
                   var=var_tok.value, var_span=var_tok.span,
                   iterable=iterable, body=body)

    def _parse_while(self) -> While:
        start = self.expect_kw("while")
        cond = self.parse_expr(0)
        self.expect("COLON", None, "`:` after the `while` condition")
        body = self.parse_block()
        return While(span=self.span(start, self.tokens[self.pos - 1]),
                     cond=cond, body=body)

    def _parse_try(self) -> Try:
        start = self.expect_kw("try")
        self.expect("COLON", None, "`:` after `try`")
        body = self.parse_block()
        catches = []
        finally_body = None
        while self.at_kw("catch"):
            self.advance()
            name_tok = self.expect_ident("a name for the caught error")
            self.expect("COLON", None, "`:` after `catch <name>`")
            cbody = self.parse_block()
            catches.append((name_tok.value, name_tok.span, cbody))
        if self.at_kw("finally"):
            self.advance()
            self.expect("COLON", None, "`:` after `finally`")
            finally_body = self.parse_block()
        if not catches and finally_body is None:
            self.err(start,
                     "`try:` needs at least one `catch <name>:` or a "
                     "`finally:`",
                     help="add `catch err:` or `finally:` after the "
                          "`try:` body")
        return Try(span=self.span(start, self.tokens[self.pos - 1]),
                   body=body, catches=catches, finally_body=finally_body)

    # ------------------------------------------------------------ match / patterns

    def _parse_match(self) -> Match:
        start = self.expect_kw("match")
        scrutinee = self.parse_expr(0)
        self.expect("COLON", None, "`:` after the `match` expression")
        self._begin_block()
        arms: list[MatchArm] = []
        while True:
            self._skip_newlines()
            if self.at("DEDENT"):
                self.advance()
                break
            if self.at("EOF"):
                self.err(self.cur,
                         "unexpected end of file in a `match` block")
            pattern = self._parse_or_pattern()
            guard = None
            if self.at_kw("if"):
                self.advance()
                guard = self.parse_expr(0)
            self.expect("COLON", None, "`:` after the match arm pattern")
            body = self.parse_block()
            end = self.tokens[self.pos - 1]
            arms.append(MatchArm(pattern=pattern, guard=guard, body=body,
                                 span=self.span_from(
                                     pattern.span.start_byte, end)))
        return Match(span=self.span(start, self.tokens[self.pos - 1]),
                     scrutinee=scrutinee, arms=arms)

    def _parse_or_pattern(self):
        first = self._parse_single_pattern()
        patterns = [first]
        while self.at("BAR"):
            self.advance()
            patterns.append(self._parse_single_pattern())
        if len(patterns) == 1:
            return first
        return OrPattern(span=self.span_from(
            first.span.start_byte, self.tokens[self.pos - 1]),
            patterns=patterns)

    def _parse_single_pattern(self):
        tok = self.cur
        if tok.kind in ("INT", "FLOAT"):
            self.advance()
            return LitPattern(span=tok.span, value=tok.value)
        if tok.kind == "STR_START":
            value, _ = self._parse_plain_string()
            return LitPattern(span=self.tokens[self.pos - 1].span
                              if False else Span(self.filename,
                                                 tok.span.start_byte,
                                                 self.tokens[self.pos - 1].span.end_byte),
                              value=value)
        if tok.kind == "KEYWORD" and tok.value in ("true", "false"):
            self.advance()
            return LitPattern(span=tok.span, value=tok.value == "true")
        if tok.kind == "KEYWORD" and tok.value == "none":
            self.advance()
            return LitPattern(span=tok.span, value=None)
        if tok.kind == "MINUS" and self.peek().kind in ("INT", "FLOAT"):
            self.advance()
            num = self.advance()
            return LitPattern(span=self.span(tok, num), value=-num.value)
        if tok.kind == "IDENT":
            if tok.value == "_":
                self.advance()
                return WildcardPattern(span=tok.span)
            name_tok = self.advance()
            if self.at("DOT"):
                self.advance()
                variant_tok = self.expect_ident("a variant name")
                bindings = []
                if self.at("LPAREN"):
                    self.advance()
                    if not self.at("RPAREN"):
                        while True:
                            fname = self.expect_ident("a field name")
                            self.expect("COLON", None, "`:`")
                            sub = self._parse_or_pattern()
                            bindings.append((fname.value, sub, fname.span))
                            if self.at("COMMA"):
                                self.advance()
                                continue
                            break
                    self.expect("RPAREN", None, "`)`")
                return EnumPattern(
                    span=self.span(name_tok, self.tokens[self.pos - 1]),
                    enum_name=name_tok.value, variant=variant_tok.value,
                    bindings=bindings)
            if self.at("LPAREN"):
                self.advance()
                fields = []
                if not self.at("RPAREN"):
                    while True:
                        fname = self.expect_ident("a field name")
                        self.expect("COLON", None, "`:`")
                        sub = self._parse_or_pattern()
                        fields.append((fname.value, sub, fname.span))
                        if self.at("COMMA"):
                            self.advance()
                            continue
                        break
                self.expect("RPAREN", None, "`)`")
                return StructPattern(
                    span=self.span(name_tok, self.tokens[self.pos - 1]),
                    struct_name=name_tok.value, fields=fields)
            return NamePattern(span=name_tok.span, name=name_tok.value,
                               name_span=name_tok.span)
        if tok.kind == "LBRACK":
            self.advance()
            elements = []
            if not self.at("RBRACK"):
                while True:
                    elements.append(self._parse_or_pattern())
                    if self.at("COMMA"):
                        self.advance()
                        continue
                    break
            end = self.expect("RBRACK", None, "`]`")
            return ListPattern(span=self.span(tok, end), elements=elements)
        if tok.kind == "LPAREN":
            self.advance()
            inner = self._parse_or_pattern()
            self.expect("RPAREN", None, "`)`")
            return inner
        self.err(tok, f"expected a pattern, found {_describe(tok)}")

    # ------------------------------------------------------------ components

    def _parse_component_modifiers(self) -> dict:
        """``(key: value, bare_flag, ...)`` after a component header."""
        mods: dict = {}
        if not self.at("LPAREN"):
            return mods
        self.advance()
        if not self.at("RPAREN"):
            while True:
                key_tok = self.expect_ident("a modifier name")
                key = key_tok.value
                if self.at("COLON"):
                    self.advance()
                    if key == "timeout":
                        value, span = self._parse_duration(
                            "a duration for `timeout:`")
                        mods[key] = value
                        mods[key + "_span"] = span
                    elif key in ("min", "max", "min_length", "max_length"):
                        expr = self.parse_expr(0)
                        if not isinstance(expr, Lit) or \
                                not isinstance(expr.value, int):
                            self.err(self.cur,
                                     f"`{key}:` must be an integer")
                        mods[key] = expr.value
                    elif key in ("id", "url"):
                        value, _ = self._parse_plain_string()
                        mods[key] = value
                    elif key == "style":
                        mods[key] = self.expect_ident(
                            "a style name").value
                    elif key == "options":
                        expr = self.parse_expr(0)
                        if not isinstance(expr, ListLit):
                            self.err(self.cur,
                                     "`options:` must be a list of strings")
                        opts = []
                        for el in expr.elements:
                            if not isinstance(el, Lit) or \
                                    not isinstance(el.value, str):
                                self.err(self.cur,
                                         "`options:` must be a list of "
                                         "strings")
                            opts.append(el.value)
                        mods[key] = opts
                    else:
                        mods[key] = self.parse_expr(0)
                else:
                    mods[key] = True
                if self.at("COMMA"):
                    self.advance()
                    continue
                break
        self.expect("RPAREN", None, "`)`")
        return mods

    def _parse_only_line(self) -> tuple[str, list[str] | None]:
        self.advance()  # `only`
        self.expect("COLON", None, "`:`")
        if self.at_ident("author"):
            self.advance()
            return "author", None
        if self.at_ident("anyone"):
            self.advance()
            return "anyone", None
        if self.at_ident("roles"):
            self.advance()
            self.expect("LPAREN", None, "`(`")
            expr = self.parse_expr(0)
            self.expect("RPAREN", None, "`)`")
            if not isinstance(expr, ListLit):
                self.err(self.cur, "`roles(...)` takes a list of strings")
            roles = []
            for el in expr.elements:
                if not isinstance(el, Lit) or \
                        not isinstance(el.value, str):
                    self.err(self.cur,
                             "`roles(...)` takes a list of strings")
                roles.append(el.value)
            return "roles", roles
        self.err(self.cur,
                 f"expected `author`, `anyone`, or `roles([...])` after "
                 f"`only:`, found {_describe(self.cur)}")

    def _parse_button(self) -> ButtonDecl:
        start = self.expect_kw("button")
        label, _ = self._parse_plain_string()
        mods = self._parse_component_modifiers()
        self.expect("COLON", None, "`:` after the button header")
        only_kind, only_roles, body, on_timeout = "author", None, [], None

        def line() -> None:
            nonlocal only_kind, only_roles, body, on_timeout
            if self.at_ident("only") or self.at_kw("only"):
                only_kind, only_roles = self._parse_only_line()
            elif self.at_kw("do"):
                self.advance()
                self.expect("COLON", None, "`:` after `do`")
                body = self.parse_block()
            elif self.at_ident("on_timeout"):
                self.advance()
                self.expect("COLON", None, "`:` after `on_timeout`")
                on_timeout = self.parse_block()
            else:
                self.err(self.cur,
                         f"unknown line in a `button` block: "
                         f"{_describe(self.cur)}",
                         help="a `button` block takes `only:`, `do:`, "
                              "and `on_timeout:`")

        self._parse_lines(line)
        return ButtonDecl(
            span=self.span(start, self.tokens[self.pos - 1]),
            label=label, style=mods.get("style"),
            custom_id=mods.get("id"), url=mods.get("url"),
            timeout=mods.get("timeout"),
            timeout_span=mods.get("timeout_span"),
            only_kind=only_kind, only_roles=only_roles, body=body,
            on_timeout=on_timeout)

    def _parse_select(self) -> SelectDecl:
        start = self.expect_kw("select")
        label, _ = self._parse_plain_string()
        mods = self._parse_component_modifiers()
        self.expect("COLON", None, "`:` after the select header")
        only_kind, only_roles, body, on_timeout = "author", None, [], None

        def line() -> None:
            nonlocal only_kind, only_roles, body, on_timeout
            if self.at_ident("only") or self.at_kw("only"):
                only_kind, only_roles = self._parse_only_line()
            elif self.at_kw("do"):
                self.advance()
                self.expect("COLON", None, "`:` after `do`")
                body = self.parse_block()
            elif self.at_ident("on_timeout"):
                self.advance()
                self.expect("COLON", None, "`:` after `on_timeout`")
                on_timeout = self.parse_block()
            else:
                self.err(self.cur,
                         f"unknown line in a `select` block: "
                         f"{_describe(self.cur)}",
                         help="a `select` block takes `only:`, `do:`, "
                              "and `on_timeout:`")

        self._parse_lines(line)
        return SelectDecl(
            span=self.span(start, self.tokens[self.pos - 1]),
            label=label, options=mods.get("options", []),
            min_values=mods.get("min"), max_values=mods.get("max"),
            timeout=mods.get("timeout"),
            timeout_span=mods.get("timeout_span"),
            only_kind=only_kind, only_roles=only_roles, body=body,
            on_timeout=on_timeout)

    def _parse_modal(self) -> ModalDecl:
        start = self.expect_kw("modal")
        title, _ = self._parse_plain_string()
        mods = self._parse_component_modifiers()
        self.expect("COLON", None, "`:` after the modal header")
        fields: list[TextInputDecl] = []
        body, on_timeout = [], None

        def line() -> None:
            nonlocal body, on_timeout
            if self.at_ident("text_input"):
                tstart = self.advance()
                name, _ = self._parse_plain_string()
                fmods = self._parse_component_modifiers()
                self.expect("COLON", None, "`:` after the text input")
                fields.append(TextInputDecl(
                    name=name, style=fmods.get("style"),
                    required=bool(fmods.get("required", False)),
                    min_length=fmods.get("min_length"),
                    max_length=fmods.get("max_length"),
                    span=self.span(tstart, self.tokens[self.pos - 1])))
            elif self.at_kw("do"):
                self.advance()
                self.expect("COLON", None, "`:` after `do`")
                body = self.parse_block()
            elif self.at_ident("on_timeout"):
                self.advance()
                self.expect("COLON", None, "`:` after `on_timeout`")
                on_timeout = self.parse_block()
            else:
                self.err(self.cur,
                         f"unknown line in a `modal` block: "
                         f"{_describe(self.cur)}",
                         help="a `modal` block takes `text_input ...` "
                              "lines, `do:`, and `on_timeout:`")

        self._parse_lines(line)
        return ModalDecl(
            span=self.span(start, self.tokens[self.pos - 1]),
            title=title, custom_id=mods.get("id"),
            timeout=mods.get("timeout"),
            timeout_span=mods.get("timeout_span"),
            fields=fields, body=body, on_timeout=on_timeout)

    # ------------------------------------------------------------ expressions

    def _binary_bp(self, tok: Token):
        # The live table is the frozen PRECEDENCE (SPEC.md §9.2); a
        # keyword token keys on its value, any other token on its kind.
        if tok.kind == "KEYWORD":
            return PRECEDENCE.get((tok.kind, tok.value))
        return PRECEDENCE.get((tok.kind, None))

    def _binary_op(self, tok: Token) -> str:
        if tok.kind == "KEYWORD":
            return tok.value  # and / or / in
        return tok.value  # + - * / % ** == != < <= > >= |> ??

    def parse_expr(self, min_bp: int = 0, no_qq: bool = False,
                   no_bool: bool = False):
        self._check_depth(self.cur)
        try:
            return self._parse_expr_inner(min_bp, no_qq, no_bool)
        finally:
            self._depth -= 1

    def _parse_expr_inner(self, min_bp: int = 0, no_qq: bool = False,
                          no_bool: bool = False):
        left = self._parse_prefix()
        left = self._parse_postfix(left)
        saw_cmp = False
        saw_coalesce = False
        saw_bool = False
        while True:
            tok = self.cur
            if tok.kind == "QMARK" and min_bp == 0:
                nxt = self.peek()
                if nxt.kind in ("NEWLINE", "DEDENT", "EOF", "COMMA",
                                "RPAREN", "RBRACK", "COLON"):
                    # Not a ternary: leave it for the statement parser
                    # (statement-level `?`) or the enclosing construct.
                    break
                self.advance()
                then_expr = self.parse_expr(0)
                self.expect("COLON", None, "`:` in a ternary expression")
                else_expr = self.parse_expr(0)
                left = Ternary(
                    span=self.span_from(left.span.start_byte,
                                        self.tokens[self.pos - 1]),
                    cond=left, then_expr=then_expr, else_expr=else_expr)
                continue
            bp = self._binary_bp(tok)
            if bp is None or bp < min_bp:
                break
            if tok.kind == "QQ":
                if no_qq or saw_bool:
                    self.err(tok, "`??` may not be mixed with `and`/`or` "
                                  "without parentheses",
                             help="parenthesize the `??` expression or "
                                  "the `and`/`or` expression")
                saw_coalesce = True
            if tok.kind == "KEYWORD" and tok.value in ("and", "or"):
                if no_bool or saw_coalesce:
                    self.err(tok, "`and`/`or` may not be mixed with `??` "
                                  "without parentheses",
                             help="parenthesize the `??` expression or "
                                  "the `and`/`or` expression")
                saw_bool = True
            if bp == 5:
                if saw_cmp:
                    self.err(tok,
                             "comparison operators are non-associative; "
                             "parenthesize chained comparisons",
                             help="write `(a < b) and (b < c)` instead of "
                                  "`a < b < c`")
                saw_cmp = True
            op = self._binary_op(tok)
            self.advance()
            if tok.kind == "PIPE":
                rhs = self._parse_postfix(self._parse_prefix())
                if isinstance(rhs, Name):
                    left = Call(
                        span=self.span_from(left.span.start_byte,
                                            self.tokens[self.pos - 1]),
                        func=rhs, args=[left])
                elif isinstance(rhs, Call):
                    left = Call(
                        span=self.span_from(left.span.start_byte,
                                            self.tokens[self.pos - 1]),
                        func=rhs.func, args=[left] + rhs.args,
                        kwargs=rhs.kwargs)
                else:
                    self.err(self.tokens[self.pos - 1],
                             "the right side of `|>` must be a function "
                             "name or a call, like `f` or `f(b, c)`")
                continue
            right_bp = bp if op == "**" else bp + 1
            rhs = self.parse_expr(
                right_bp,
                no_qq=(op in ("and", "or")),
                no_bool=(op == "??"))
            left = BinOp(
                span=self.span_from(left.span.start_byte,
                                    self.tokens[self.pos - 1]),
                op=op, left=left, right=rhs)
        return left

    def _parse_prefix(self):
        tok = self.cur
        if tok.kind in ("INT", "FLOAT"):
            self.advance()
            return Lit(span=tok.span, value=tok.value)
        if tok.kind == "STR_START":
            return self._parse_string_expr()
        if tok.kind == "KEYWORD" and tok.value in ("true", "false"):
            self.advance()
            return Lit(span=tok.span, value=tok.value == "true")
        if tok.kind == "KEYWORD" and tok.value == "none":
            self.advance()
            return Lit(span=tok.span, value=None)
        if tok.kind == "KEYWORD" and tok.value in ("bot", "log"):
            # Handler bindings / the logger are keywords, but they are
            # also names in expressions (`{bot}`, `log.info(...)`).
            self.advance()
            return Name(span=tok.span, name=tok.value)
        if tok.kind == "KEYWORD" and tok.value == "fn":
            return self._parse_lambda()
        if tok.kind == "KEYWORD" and tok.value == "embed":
            return self._parse_embed()
        if tok.kind == "KEYWORD" and tok.value == "try":
            self.advance()
            operand = self.parse_expr(0)
            return TryExpr(span=self.span(tok, self.tokens[self.pos - 1]),
                           expr=operand)
        if tok.kind == "KEYWORD" and tok.value == "not":
            self.advance()
            operand = self.parse_expr(3)
            return UnaryOp(span=self.span(tok, self.tokens[self.pos - 1]),
                           op="not", operand=operand)
        if tok.kind == "MINUS":
            self.advance()
            operand = self.parse_expr(6)
            return UnaryOp(span=self.span(tok, self.tokens[self.pos - 1]),
                           op="-", operand=operand)
        if tok.kind == "LPAREN":
            self.advance()
            expr = self.parse_expr(0)
            self.expect("RPAREN", None, "`)`")
            return expr
        if tok.kind == "LBRACK":
            start = self.advance()
            elements = []
            if not self.at("RBRACK"):
                while True:
                    elements.append(self.parse_expr(0))
                    if self.at("COMMA"):
                        self.advance()
                        if self.at("RBRACK"):
                            break
                        continue
                    break
            end = self.expect("RBRACK", None, "`]`")
            return ListLit(span=self.span(start, end), elements=elements)
        if tok.kind == "LBRACE":
            start = self.advance()
            pairs = []
            if not self.at("RBRACE"):
                while True:
                    key = self.parse_expr(0)
                    self.expect("COLON", None, "`:`")
                    value = self.parse_expr(0)
                    pairs.append((key, value))
                    if self.at("COMMA"):
                        self.advance()
                        if self.at("RBRACE"):
                            break
                        continue
                    break
            end = self.expect("RBRACE", None, "`}`")
            return DictLit(span=self.span(start, end), pairs=pairs)
        if tok.kind == "IDENT":
            self.advance()
            return Name(span=tok.span, name=tok.value)
        if tok.kind == "DURATION":
            self.err(tok,
                     "a duration literal is only valid in `task every ...` "
                     "and in `timeout:`",
                     help="write `60s` only after `task every` or "
                          "`timeout:`")
        self.err(tok, f"unexpected {_describe(tok)}")

    def _parse_lambda(self) -> Lambda:
        start = self.expect_kw("fn")
        params = self._parse_params()
        return_type = None
        if self.at("ARROW"):
            self.advance()
            return_type = self.parse_type()
        self.expect("COLON", None, "`:` after the lambda header")
        if self.at("NEWLINE"):
            body = self.parse_block()
            return Lambda(span=self.span(start, self.tokens[self.pos - 1]),
                          params=params, return_type=return_type,
                          body=body, expr_body=None)
        expr_body = self.parse_expr(0)
        return Lambda(span=self.span(start, self.tokens[self.pos - 1]),
                      params=params, return_type=return_type,
                      body=None, expr_body=expr_body)

    def _parse_embed(self) -> EmbedExpr:
        start = self.expect_kw("embed")
        self.expect("COLON", None, "`:` after `embed`")
        fields: list[EmbedField] = []

        def line() -> None:
            if self.at_ident("title") or self.at_ident("description") \
                    or self.at_ident("color"):
                kind_tok = self.advance()
                self.expect("COLON", None, "`:`")
                value = self.parse_expr(0)
                fields.append(EmbedField(
                    kind=kind_tok.value, name=None, value=value,
                    inline=None,
                    span=self.span(kind_tok, self.tokens[self.pos - 1])))
            elif self.at_ident("field"):
                fstart = self.advance()
                name, _ = self._parse_plain_string()
                self.expect("COLON", None, "`:`")
                value = self.parse_expr(0)
                inline = None
                if isinstance(value, Call) and \
                        isinstance(value.func, (Lit, Interp)) and \
                        not value.args and len(value.kwargs) == 1 and \
                        value.kwargs[0][0] == "inline" and \
                        isinstance(value.kwargs[0][1], Lit) and \
                        isinstance(value.kwargs[0][1].value, bool):
                    # `"Value" (inline: true)` parses as a call on the
                    # string literal; unwrap it back into the field.
                    inline = value.kwargs[0][1].value
                    value = value.func
                if self.at("LPAREN"):
                    self.advance()
                    key_tok = self.expect_ident("`inline`")
                    if key_tok.value != "inline":
                        self.err(key_tok,
                                 "an embed field only takes "
                                 "`(inline: true/false)`")
                    self.expect("COLON", None, "`:`")
                    inline = self._parse_bool_lit()
                    self.expect("RPAREN", None, "`)`")
                fields.append(EmbedField(
                    kind="field", name=name, value=value, inline=inline,
                    span=self.span(fstart, self.tokens[self.pos - 1])))
            else:
                self.err(self.cur,
                         f"unknown line in an `embed:` block: "
                         f"{_describe(self.cur)}",
                         help="an embed takes `title:`, `description:`, "
                              "`color:`, and `field \"Name\": ...`")

        self._parse_lines(line)
        return EmbedExpr(span=self.span(start, self.tokens[self.pos - 1]),
                         fields=fields)

    def _parse_postfix(self, expr):
        while True:
            tok = self.cur
            if tok.kind == "DOT":
                self.advance()
                name_tok = self.expect_ident("a field or method name")
                expr = Member(span=self.span_from(expr.span.start_byte,
                                                  name_tok),
                              obj=expr, name=name_tok.value)
            elif tok.kind == "QDOT":
                self.advance()
                name_tok = self.expect_ident("a field or method name")
                # M1 AST amendment: `?.` builds a Member with
                # optional=True (see ast_nodes.Member); the checker
                # needs the flag to enforce the `?.` typing rule.
                expr = Member(span=self.span_from(expr.span.start_byte,
                                                  name_tok),
                              obj=expr, name=name_tok.value,
                              optional=True)
            elif tok.kind == "LBRACK":
                self.advance()
                index = self.parse_expr(0)
                end = self.expect("RBRACK", None, "`]`")
                expr = Index(span=self.span_from(expr.span.start_byte, end),
                             obj=expr, index=index)
            elif tok.kind == "LPAREN":
                expr = self._parse_call(expr)
            else:
                return expr
        return expr

    def _parse_call(self, func):
        self.expect("LPAREN", None, "`(`")
        args: list = []
        kwargs: list[tuple[str, object, Span]] = []
        if not self.at("RPAREN"):
            while True:
                if self.at("IDENT") and self.peek().kind == "COLON":
                    name_tok = self.advance()
                    self.advance()  # `:`
                    value = self.parse_expr(0)
                    kwargs.append((name_tok.value, value, name_tok.span))
                else:
                    args.append(self.parse_expr(0))
                if self.at("COMMA"):
                    self.advance()
                    if self.at("RPAREN"):
                        break
                    continue
                break
        end = self.expect("RPAREN", None, "`)`")
        span = self.span_from(func.span.start_byte, end)
        if isinstance(func, Name) and func.name == "t":
            if not args or not isinstance(args[0], Lit) or \
                    not isinstance(args[0].value, str):
                self._err_span(func.span,
                               "`t(...)` takes a string key as its first "
                               "argument, like `t(\"buy.ok\", ...)`")
            if len(args) > 1:
                self._err_span(args[1].span,
                               "`t(...)` takes only named arguments after the "
                               "key, like `t(\"buy.ok\", item: item)`")
            return TCall(span=span, key=args[0].value,
                         key_span=args[0].span, args=kwargs)
        return Call(span=span, func=func, args=args, kwargs=kwargs)


def parse(source: str, filename: str = "<string>") -> Program:
    """Parse newr source into a Program. Raises NewrError (E1001) on the
    first syntax error."""
    return _Parser(source, filename).parse_program()
