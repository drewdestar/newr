"""Canonical formatter for newr source (SPEC.md section 19, M6 Phase 2a).

``newr fmt`` rewrites a .newr file in the canonical style; ``newr fmt
--check`` reports files that are not canonical yet (CI). The formatter
works from the parser/AST only, no checker changes, and a file with
type errors still formats as long as it parses.

Canonical style (pinned by tests/test_fmt.py):

- 2-space indent (the parser enforces this anyway).
- Single spaces around binary operators, after commas, and after the
  ``:`` in headers; no space inside ``()``, ``[]``, ``{}``.
- Top-level items stay in source order; blank lines are preserved
  (0 or 1) between sibling statements and declarations, and runs of
  2+ blank lines collapse to one. No blank line is added after a
  block header or before a block ends; the header lines inside
  struct/enum/bot/command/store/strings blocks stay dense.
- Literals keep their source spelling: numbers (``0x10``, ``1_000``),
  strings (quotes, escapes, interpolation), and durations (``60s``,
  ``1h``) are sliced verbatim from the source. Without source text
  (a programmatically built AST) they render from their values.
- ``#`` comments are preserved: a full-line comment keeps its text
  and indentation; a trailing comment stays on its line. The lexer
  drops comments, so they are recovered from the source text.
- ``print`` is written without parentheses (``print "x"``), the
  canonical form; ``print("x")`` formats to the same text.
- The file ends with exactly one trailing newline; line endings are
  LF (a CRLF file formats to LF).
- Idempotent: fmt(fmt(x)) == fmt(x).

Known limits (documented, not silently wrong):

- Boolean flags that default to false (``ephemeral: false``,
  ``ledger false``, an absent ``required``) are omitted.
  ``only: author`` is always written: it is the explicit default in
  the corpus.
- The lines inside command/event/task/component blocks are emitted in
  a fixed order (description, options, reply, ephemeral, do / only,
  do, on_timeout); the parser does not keep their source order.
- A block-bodied lambda or embed used as a method receiver or as a
  binary/unary operand cannot be parenthesized (parentheses would
  break the block), so it is emitted bare. That shape never occurs in
  real code and is not in the corpus.
- A trailing comment (code plus ``#`` on one line) is kept on its
  line when that line maps to one emitted line; otherwise it is kept
  as a full-line comment just below it, at its original indent.
"""

from __future__ import annotations

import bisect
import math
import re

from . import ast_nodes as A
from .errors import Span
from .lexer import tokenize
from .parser import _PLACEHOLDER_RE, parse

INDENT = "  "

# Binary operator precedence, the parser table (SPEC.md section 9.2).
_BINOP_PREC = {
    "|>": 1, "??": 2, "or": 3, "and": 4,
    "==": 5, "!=": 5, "<": 5, "<=": 5, ">": 5, ">=": 5, "in": 5,
    "+": 6, "-": 6, "*": 7, "/": 7, "%": 7, "**": 8,
}
_COMPARISONS = {"==", "!=", "<", "<=", ">", ">=", "in"}
_SKIP_TOKEN_KINDS = {"NEWLINE", "INDENT", "DEDENT", "EOF", "DOC_COMMENT"}


def _escape_text(text: str) -> str:
    """Escape literal text for a double-quoted newr string.

    Mirrors the escapes the lexer accepts (SPEC.md SS6): ``\\"``,
    ``\\\\``, ``\\n``, ``\\t``, ``\\r``, ``\\u{XXXX}``. A literal brace
    is written doubled (``{{`` / ``}}``) so it is not read as an
    interpolation start.
    """
    out: list[str] = []
    for ch in text:
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
        elif ord(ch) < 0x20 or ord(ch) == 0x7F:
            out.append("\\u{%x}" % ord(ch))
        else:
            out.append(ch)
    return "".join(out)


def _quote(text: str) -> str:
    """Render a plain string value as a double-quoted literal."""
    return '"' + _escape_text(text) + '"'


def _quote_template(template: str, placeholders: list[str]) -> str:
    """Render a strings-block template as a double-quoted literal.

    ``{name}`` placeholders (in order, as recorded by the parser)
    stay single; every other brace is doubled, so a literal brace
    survives a re-parse.
    """
    out: list[str] = []
    pos = 0
    todo = list(placeholders)
    for match in _PLACEHOLDER_RE.finditer(template):
        name = match.group(1)
        if todo and todo[0] == name:
            todo.pop(0)
            out.append(_escape_text(template[pos:match.start()]))
            out.append("{" + name + "}")
            pos = match.end()
    out.append(_escape_text(template[pos:]))
    return '"' + "".join(out) + '"'


def _format_float(value: float) -> str:
    """Render a float value as a literal (``2.0`` stays ``2.0``)."""
    text = repr(value)
    if text in ("inf", "-inf", "nan"):
        # No float literal spells these; only reachable from a
        # hand-built AST, never from parsed source (slicing wins).
        return "1e999" if text == "inf" else "-1e999" if text == "-inf" \
            else "0.0"
    return text


class _Formatter:
    """Formats one parsed program, optionally with its source text.

    With source text, literals and durations are sliced verbatim and
    ``#`` comments are preserved. Without it, literals render from
    their values and comments are dropped (only used for
    programmatically built ASTs in tests).
    """

    def __init__(self, program: A.Program, source: str | None,
                 filename: str):
        self.program = program
        self.source = source
        self.filename = filename
        self._lines: list[str] = []
        self._indent = 0
        # Source analysis, only when source text is available.
        self._src_bytes: bytes | None = None
        self._line_starts: list[int] = [0]
        self._tokens: list = []
        self._token_ends: list[int] = []
        self._src_lines: list[str] = []
        # (line, text, indent spaces), in source order.
        self._comments: list[tuple[int, str, int]] = []
        # line -> (text, indent spaces) for trailing comments.
        self._trailing: dict[int, tuple[str, int]] = {}
        self._comment_idx = 0
        self._consumed_trailing: set[int] = set()
        self._last_consumed_line = 0
        if source is not None:
            self._analyse_source(source)

    # ---------------------------------------------------------- source

    def _analyse_source(self, source: str) -> None:
        self._src_bytes = source.encode("utf-8")
        self._src_lines = source.split("\n")
        starts = [0]
        pos = 0
        for line in self._src_lines[:-1]:
            pos += len(line.encode("utf-8")) + 1
            starts.append(pos)
        self._line_starts = starts
        self._tokens = tokenize(source, self.filename)
        self._token_ends = [t.span.end_byte for t in self._tokens]
        # Tokens per start line, for comment detection. Structural
        # tokens (NEWLINE/INDENT/DEDENT/EOF) do not count: the NEWLINE
        # ending a comment-only line would otherwise hide its comment.
        by_line: dict[int, list] = {}
        for tok in self._tokens:
            if tok.kind in _SKIP_TOKEN_KINDS:
                continue
            line = self._line_no(tok.span.start_byte)
            by_line.setdefault(line, []).append(tok)
        for i, raw in enumerate(self._src_lines, start=1):
            stripped = raw.strip()
            toks = by_line.get(i, [])
            if not toks:
                if stripped.startswith("#") or stripped.startswith("///"):
                    indent = len(raw) - len(raw.lstrip(" "))
                    self._comments.append((i, stripped, indent))
                continue
            last_end = max(t.span.end_byte for t in toks)
            line_end = self._line_starts[i - 1] + \
                len(raw.encode("utf-8"))
            rest = self._src_bytes[last_end:line_end].decode(
                "utf-8").strip()
            if rest.startswith("#"):
                indent = len(raw) - len(raw.lstrip(" "))
                self._trailing[i] = (rest, indent)

    def _line_no(self, byte_offset: int) -> int:
        """1-based line of a byte offset."""
        return bisect.bisect_right(self._line_starts, byte_offset)

    def _slice(self, span: Span) -> str:
        """Source text covered by a span, verbatim."""
        assert self._src_bytes is not None
        return self._src_bytes[span.start_byte:span.end_byte].decode(
            "utf-8")

    def _start_line(self, node) -> int:
        if self.source is None:
            return 0
        return self._line_no(node.span.start_byte)

    def _end_line(self, node) -> int:
        """Line of the last content token inside a node's span.

        NEWLINE/INDENT/DEDENT tokens are skipped: a block node's span
        ends at the DEDENT token, which sits on the following line.
        """
        if self.source is None:
            return self._start_line(node)
        end = node.span.end_byte
        idx = bisect.bisect_right(self._token_ends, end) - 1
        while idx >= 0:
            tok = self._tokens[idx]
            if tok.span.end_byte > end:
                idx -= 1
                continue
            if tok.kind in _SKIP_TOKEN_KINDS:
                idx -= 1
                continue
            return self._line_no(tok.span.end_byte - 1)
        return self._line_no(node.span.start_byte)

    def _blank_lines_between(self, after_line: int,
                             before_line: int) -> int:
        """Blank source lines strictly between two line numbers."""
        if self.source is None:
            return 0
        count = 0
        for line in range(after_line + 1, before_line):
            if self._src_lines[line - 1].strip() == "":
                count += 1
        return count

    # ---------------------------------------------------------- output

    def _line(self, text: str, src_line: int | None = None) -> None:
        """Append one output line at the current indent.

        A trailing comment on the matching source line stays on it.
        """
        line = INDENT * self._indent + text
        if src_line is not None and src_line in self._trailing \
                and src_line not in self._consumed_trailing:
            comment, _indent = self._trailing[src_line]
            self._consumed_trailing.add(src_line)
            line += "  " + comment
            self._last_consumed_line = max(self._last_consumed_line,
                                           src_line)
        self._lines.append(line)

    def _raw_lines(self, text: str) -> None:
        """Append pre-indented lines (a multi-line expression)."""
        self._lines.extend(text.split("\n"))

    def _capture_lines(self, fn) -> list[str]:
        """Run fn with a fresh line buffer; return its lines.

        Block-bodied lambdas and embeds emit their bodies into the
        buffer so the caller gets one multi-line string.
        """
        saved = self._lines
        self._lines = []
        try:
            fn()
        finally:
            captured, self._lines = self._lines, saved
        return captured

    def _maybe_blank(self, target_line: int, first: bool) -> None:
        """One blank line before an item, if the source had one.

        Never before the first item of a list: no blank line after a
        block header or at the top of a file.
        """
        if first or self.source is None:
            return
        if self._blank_lines_between(self._last_consumed_line,
                                     target_line) >= 1:
            self._lines.append("")

    def _flush_comments_before(self, line: int, first: bool = False,
                             allow_blank: bool = True) -> None:
        """Emit pending comments (full-line, then unconsumed trailing
        ones) that sit before a source line, at their original indent
        (rounded down to an even number of spaces). ``first`` marks
        the first item of a list (no blank line after a block header);
        ``allow_blank`` is false inside dense header-line blocks."""
        pending: list[tuple[int, str, int]] = []
        while self._comment_idx < len(self._comments) and \
                self._comments[self._comment_idx][0] < line:
            pending.append(self._comments[self._comment_idx])
            self._comment_idx += 1
        for tline, (text, _indent) in sorted(self._trailing.items()):
            if tline < line and tline not in self._consumed_trailing:
                pending.append((tline, text, _indent))
                self._consumed_trailing.add(tline)
        pending.sort(key=lambda item: item[0])
        for j, (cline, text, indent) in enumerate(pending):
            if allow_blank:
                # Only the first flushed comment sits right after a
                # block header (or at the top of a file); a blank
                # between two comments is grouping, keep it.
                self._maybe_blank(cline, first=(first and j == 0)
                                  or not self._lines)
            spaces = " " * (indent // 2 * 2)
            self._lines.append(spaces + text)
            self._last_consumed_line = max(self._last_consumed_line,
                                           cline)

    def _flush_remaining_comments(self) -> None:
        """Emit every comment left after the last item."""
        rest = self._comments[self._comment_idx:]
        self._comment_idx = len(self._comments)
        pending = list(rest)
        for tline, (text, indent) in sorted(self._trailing.items()):
            if tline not in self._consumed_trailing:
                pending.append((tline, text, indent))
                self._consumed_trailing.add(tline)
        pending.sort(key=lambda item: item[0])
        for cline, text, indent in pending:
            self._maybe_blank(cline, first=not self._lines)
            spaces = " " * (indent // 2 * 2)
            self._lines.append(spaces + text)
            self._last_consumed_line = max(self._last_consumed_line,
                                           cline)

    def _begin_block(self) -> None:
        self._indent += 1

    def _end_block(self) -> None:
        self._indent -= 1

    def _emit_block(self, stmts: list) -> None:
        """Emit a statement block at one deeper indent."""
        self._begin_block()
        for i, stmt in enumerate(stmts):
            self._emit_stmt(stmt, first=(i == 0))
        self._end_block()


    # -------------------------------------------------------- literals

    def _lit_text(self, node: A.Lit) -> str:
        """A literal, sliced verbatim from source when available."""
        if self.source is not None:
            return self._slice(node.span)
        value = node.value
        if isinstance(value, bool):
            return "true" if value else "false"
        if value is None:
            return "none"
        if isinstance(value, float):
            return _format_float(value)
        if isinstance(value, int):
            return str(value)
        return _quote(str(value))

    def _duration_text(self, seconds: int, span: Span | None) -> str:
        """A duration, sliced verbatim from source when available."""
        if self.source is not None and span is not None:
            return self._slice(span)
        if self.source is not None:
            for tok in self._tokens:
                if tok.kind == "DURATION" and tok.value == seconds:
                    return self._slice(tok.span)
        return f"{seconds}s"

    def _find_duration_span(self, span: Span) -> Span | None:
        """The DURATION token inside a span (a task header)."""
        if self.source is None:
            return None
        for tok in self._tokens:
            if tok.kind == "DURATION" and \
                    span.start_byte <= tok.span.start_byte and \
                    tok.span.end_byte <= span.end_byte:
                return tok.span
        return None

    # ------------------------------------------------------ expressions

    def _expr(self, node) -> str:
        """An expression in a free (parse_expr(0)) position: no outer
        parentheses are ever needed here."""
        if isinstance(node, A.Lit):
            return self._lit_text(node)
        if isinstance(node, A.Interp):
            if self.source is not None:
                return self._slice(node.span)
            parts: list[str] = []
            for part in node.parts:
                if part.expr is not None:
                    parts.append("{" + self._expr(part.expr) + "}")
                else:
                    parts.append(_escape_text(part.text or ""))
            return '"' + "".join(parts) + '"'
        if isinstance(node, A.Name):
            return node.name
        if isinstance(node, A.BinOp):
            return self._emit_binop(node)
        if isinstance(node, A.UnaryOp):
            if node.op == "-":
                return "-" + self._expr_unary_operand(node.operand, "-")
            return "not " + self._expr_unary_operand(node.operand, "not")
        if isinstance(node, A.Ternary):
            cond = self._expr_ternary_cond(node.cond)
            then = self._expr(node.then_expr)
            els = self._expr(node.else_expr)
            return f"{cond} ? {then} : {els}"
        if isinstance(node, A.Lambda):
            return self._emit_lambda(node)
        if isinstance(node, A.TryExpr):
            return "try " + self._expr(node.expr)
        if isinstance(node, A.Propagate):
            return self._expr_propagate(node)
        if isinstance(node, A.Call):
            return self._emit_call(node)
        if isinstance(node, A.TCall):
            args = ", ".join(
                f"{name}: {self._expr(value)}"
                for name, value, _span in node.args)
            key = self._slice(node.key_span) if \
                self.source is not None and node.key_span is not None \
                else _quote(node.key)
            inner = key + (", " + args if args else "")
            return f"t({inner})"
        if isinstance(node, A.Member):
            recv = self._expr_receiver(node.obj)
            return recv + ("?." if node.optional else ".") + node.name
        if isinstance(node, A.Index):
            recv = self._expr_receiver(node.obj)
            return recv + "[" + self._expr(node.index) + "]"
        if isinstance(node, A.ListLit):
            return "[" + ", ".join(self._expr(e)
                                  for e in node.elements) + "]"
        if isinstance(node, A.DictLit):
            pairs = ", ".join(
                self._expr(k) + ": " + self._expr(v)
                for k, v in node.pairs)
            return "{" + pairs + "}"
        if isinstance(node, A.EmbedExpr):
            return self._emit_embed(node)
        raise TypeError(f"cannot format expression node "
                        f"{type(node).__name__}")

    def _expr_propagate(self, node: A.Propagate) -> str:
        """``expr?``, statement-level only in the parser; the ``?``
        follows the expression on the same line, or on its own line
        after a multi-line (block lambda / embed) expression."""
        inner = self._expr(node.expr)
        if "\n" in inner:
            return inner + "\n" + INDENT * self._indent + "?"
        return inner + "?"

    def _expr_operand(self, child, parent_op: str,
                      is_left: bool) -> str:
        """A child of a binary operator: parenthesize exactly when
        the re-parse would regroup it.

        - Lower precedence than the parent: parens.
        - Equal precedence: parens on the right of a left-associative
          operator, on the left of ``**`` (right-associative), and on
          both sides of a comparison (non-associative).
        - ``??`` mixed with ``and``/``or`` needs parens either way.
        - A unary operand on the left is swallowed by a parent
          operator that binds at or above the unary's own power
          (``(-a) + b`` is needed, ``-a != b`` is not): parens
          exactly then. On the right it is fine. Same for a ternary,
          a lambda, or ``try``.
        """
        if isinstance(child, A.BinOp):
            p = _BINOP_PREC[parent_op]
            q = _BINOP_PREC[child.op]
            need = False
            if q < p:
                need = True
            elif q == p:
                if parent_op == "**":
                    need = is_left
                elif parent_op in _COMPARISONS:
                    need = True
                else:
                    need = not is_left
            if {parent_op, child.op} in ({"??", "and"}, {"??", "or"}):
                need = True
            if need:
                return "(" + self._expr(child) + ")"
            return self._expr(child)
        if isinstance(child, A.Ternary):
            return "(" + self._expr(child) + ")"
        if isinstance(child, A.UnaryOp):
            if is_left:
                limit = 6 if child.op == "-" else 3
                if _BINOP_PREC[parent_op] >= limit:
                    return "(" + self._expr(child) + ")"
            return self._expr(child)
        if isinstance(child, A.Lambda):
            if child.expr_body is not None:
                return "(" + self._expr(child) + ")"
            return self._expr(child)
        if isinstance(child, A.TryExpr):
            return "(" + self._expr(child) + ")"
        return self._expr(child)

    def _expr_unary_operand(self, child, op: str) -> str:
        """The operand of ``-`` (binds at 6) or ``not`` (binds at 3):
        a weaker binary operator or a ternary needs parens, because
        the operand would otherwise swallow it (``-a + b`` is
        ``-(a + b)``; ``not a and b`` is ``not (a and b)``)."""
        limit = 6 if op == "-" else 3
        if isinstance(child, A.BinOp):
            if _BINOP_PREC[child.op] < limit:
                return "(" + self._expr(child) + ")"
            return self._expr(child)
        if isinstance(child, A.Ternary):
            return "(" + self._expr(child) + ")"
        if isinstance(child, A.Lambda):
            if child.expr_body is not None:
                return "(" + self._expr(child) + ")"
            return self._expr(child)
        if isinstance(child, A.TryExpr):
            return "(" + self._expr(child) + ")"
        return self._expr(child)

    def _expr_ternary_cond(self, child) -> str:
        """The condition of a ternary: a nested ternary, lambda, or
        ``try`` would swallow the ``?`` without parentheses."""
        if isinstance(child, (A.Ternary, A.TryExpr)):
            return "(" + self._expr(child) + ")"
        if isinstance(child, A.Lambda):
            if child.expr_body is not None:
                return "(" + self._expr(child) + ")"
            return self._expr(child)
        return self._expr(child)

    def _expr_receiver(self, child) -> str:
        """The receiver of ``.`` / ``?.`` / ``[]`` or a call target:
        anything looser than a postfix binds wrong without parens."""
        if isinstance(child, (A.BinOp, A.Ternary, A.UnaryOp,
                              A.TryExpr)):
            return "(" + self._expr(child) + ")"
        if isinstance(child, A.Lambda):
            if child.expr_body is not None:
                return "(" + self._expr(child) + ")"
            return self._expr(child)
        return self._expr(child)

    def _emit_binop(self, node: A.BinOp) -> str:
        left = self._expr_operand(node.left, node.op, is_left=True)
        right = self._expr_operand(node.right, node.op, is_left=False)
        return f"{left} {node.op} {right}"

    def _emit_call(self, node: A.Call) -> str:
        func = self._expr_receiver(node.func)
        parts = [self._expr(arg) for arg in node.args]
        parts += [f"{name}: {self._expr(value)}"
                  for name, value, _span in node.kwargs]
        return func + "(" + ", ".join(parts) + ")"

    def _emit_lambda(self, node: A.Lambda) -> str:
        params = ", ".join(
            f"{p.name}: {p.type}" for p in node.params)
        ret = f" -> {node.return_type}" if node.return_type else ""
        head = f"fn({params}){ret}:"
        if node.body is not None:
            def body() -> None:
                self._begin_block()
                for i, stmt in enumerate(node.body):
                    self._emit_stmt(stmt, first=(i == 0))
                self._end_block()
            return "\n".join([head] + self._capture_lines(body))
        return head + " " + self._expr(node.expr_body)

    def _emit_embed(self, node: A.EmbedExpr) -> str:
        def fields() -> None:
            self._begin_block()
            for field in node.fields:
                if field.kind == "field":
                    text = (f"field {_quote(field.name or '')}: "
                            f"{self._expr(field.value)}")
                    if field.inline is not None:
                        text += (" (inline: true)" if field.inline
                                 else " (inline: false)")
                else:
                    text = f"{field.kind}: {self._expr(field.value)}"
                self._line(text, src_line=self._line_no(
                    field.span.start_byte) if self.source is not None
                    and field.span is not None else None)
            self._end_block()
        return "\n".join(["embed:"] + self._capture_lines(fields))

    # ------------------------------------------------------ statements

    def _emit_prefixed_expr(self, prefix: str, expr_text: str,
                            src_line: int | None) -> None:
        """Emit ``prefix + expr``; a multi-line expression (block
        lambda, embed) keeps its continuation lines as-is."""
        parts = expr_text.split("\n")
        self._line(prefix + parts[0], src_line=src_line)
        self._lines.extend(parts[1:])

    def _emit_stmt(self, node, first: bool = False) -> None:
        start = self._start_line(node)
        before = len(self._lines)
        self._flush_comments_before(start, first=first)
        # A blank between a flushed comment and this item is kept
        # even for the first item; only suppress it when the flush
        # emitted nothing (blank right after a block header).
        self._maybe_blank(start, first and len(self._lines) == before)
        self._emit_stmt_inner(node, start)
        self._last_consumed_line = max(self._last_consumed_line,
                                       self._end_line(node))

    def _emit_stmt_inner(self, node, start: int) -> None:
        if isinstance(node, A.Let):
            self._emit_let_const(node, "let", start)
        elif isinstance(node, A.Const):
            self._emit_let_const(node, "const", start)
        elif isinstance(node, A.Assign):
            self._emit_prefixed_expr(
                f"{node.name} {node.op} ", self._expr(node.value), start)
        elif isinstance(node, A.If):
            self._line(f"if {self._expr(node.cond)}:", src_line=start)
            self._emit_block(node.then_body)
            for econd, ebody in node.elifs:
                eline = self._line_no(econd.span.start_byte) \
                    if self.source is not None else start
                self._flush_comments_before(eline)
                self._line(f"elif {self._expr(econd)}:",
                           src_line=eline)
                self._emit_block(ebody)
            if node.else_body is not None:
                eline = self._start_line(node.else_body[0]) \
                    if node.else_body else start
                self._flush_comments_before(eline)
                self._line("else:", src_line=None)
                self._emit_block(node.else_body)
        elif isinstance(node, A.For):
            self._line(f"for {node.var} in "
                       f"{self._expr(node.iterable)}:",
                       src_line=start)
            self._emit_block(node.body)
        elif isinstance(node, A.While):
            self._line(f"while {self._expr(node.cond)}:",
                       src_line=start)
            self._emit_block(node.body)
        elif isinstance(node, A.Match):
            self._line(f"match {self._expr(node.scrutinee)}:",
                       src_line=start)
            self._begin_block()
            for i, arm in enumerate(node.arms):
                aline = self._line_no(arm.pattern.span.start_byte) \
                    if self.source is not None else start
                self._flush_comments_before(aline, first=(i == 0))
                self._maybe_blank(aline, first=(i == 0))
                text = self._pattern(arm.pattern)
                if arm.guard is not None:
                    text += f" if {self._expr(arm.guard)}"
                self._line(text + ":", src_line=aline)
                self._emit_block(arm.body)
            self._end_block()
        elif isinstance(node, A.Try):
            self._line("try:", src_line=start)
            self._emit_block(node.body)
            for name, name_span, cbody in node.catches:
                cline = self._line_no(name_span.start_byte) \
                    if self.source is not None else start
                self._flush_comments_before(cline)
                self._line(f"catch {name}:", src_line=cline)
                self._emit_block(cbody)
            if node.finally_body is not None:
                fline = self._start_line(node.finally_body[0]) \
                    if node.finally_body else start
                self._flush_comments_before(fline)
                self._line("finally:", src_line=None)
                self._emit_block(node.finally_body)
        elif isinstance(node, A.Require):
            self._line(f"requires {self._expr(node.cond)}",
                       src_line=start)
        elif isinstance(node, A.Ensure):
            self._line(f"ensures {self._expr(node.cond)}",
                       src_line=start)
        elif isinstance(node, A.Return):
            if node.value is None:
                self._line("return", src_line=start)
            else:
                self._emit_prefixed_expr(
                    "return ", self._expr(node.value), start)
        elif isinstance(node, A.Throw):
            self._emit_prefixed_expr(
                "throw ", self._expr(node.value), start)
        elif isinstance(node, A.Break):
            self._line("break", src_line=start)
        elif isinstance(node, A.Continue):
            self._line("continue", src_line=start)
        elif isinstance(node, A.Pass):
            self._line("pass", src_line=start)
        elif isinstance(node, A.Defer):
            self._line("defer", src_line=start)
        elif isinstance(node, A.Reply):
            text = f"reply {self._expr(node.value)}"
            if node.ephemeral:
                text += " ephemeral"
            self._line(text, src_line=start)
        elif isinstance(node, A.Send):
            self._line(f"send {self._expr(node.value)} to "
                       f"{self._expr(node.channel)}", src_line=start)
        elif isinstance(node, A.Log):
            self._emit_prefixed_expr(
                "log ", self._expr(node.value), start)
        elif isinstance(node, A.Edit):
            self._emit_prefixed_expr(
                "edit ", self._expr(node.value), start)
        elif isinstance(node, A.Followup):
            self._emit_prefixed_expr(
                "followup ", self._expr(node.value), start)
        elif isinstance(node, A.ExprStmt):
            self._emit_expr_stmt(node, start)
        elif isinstance(node, A.ButtonDecl):
            self._emit_button(node, start)
        elif isinstance(node, A.SelectDecl):
            self._emit_select(node, start)
        elif isinstance(node, A.ModalDecl):
            self._emit_modal(node, start)
        else:
            raise TypeError(f"cannot format statement node "
                            f"{type(node).__name__}")

    def _emit_let_const(self, node, keyword: str, start: int) -> None:
        head = "pub " if node.pub else ""
        head += f"{keyword} {node.name}"
        if node.type_ann is not None:
            head += f": {node.type_ann}"
        self._emit_prefixed_expr(head + " = ", self._expr(node.value),
                                 start)

    def _emit_expr_stmt(self, node: A.ExprStmt, start: int) -> None:
        value = node.value
        if isinstance(value, A.Call) and isinstance(value.func, A.Name) \
                and value.func.name == "print" and not value.kwargs:
            args = ", ".join(self._expr(a) for a in value.args)
            text = "print" + (" " + args if args else "")
            self._line(text, src_line=start)
            return
        self._emit_prefixed_expr("", self._expr(value), start)

    # ------------------------------------------------------ components

    def _component_mods(self, parts: list[str]) -> str:
        return " (" + ", ".join(parts) + ")" if parts else ""

    def _emit_only(self, kind: str, roles: list[str] | None) -> None:
        if kind == "roles":
            quoted = ", ".join(_quote(r) for r in (roles or []))
            self._line(f"only: roles([{quoted}])", src_line=None)
        else:
            self._line(f"only: {kind}", src_line=None)

    def _emit_do(self, body, start: int) -> None:
        # No do: line when the block is absent (None) or empty ([]):
        # buttons/selects/modals default body to [], commands/events
        # to None. An empty do: block is checker-rejected anyway.
        if not body:
            return
        if self.source is not None:
            self._flush_comments_before(self._start_line(body[0]))
        self._line("do:", src_line=None)
        self._emit_block(body)

    def _emit_on_timeout(self, body, start: int) -> None:
        if not body:
            return
        self._flush_comments_before(
            self._start_line(body[0]) if self.source is not None
            else start)
        self._line("on_timeout:", src_line=None)
        self._emit_block(body)

    def _emit_button(self, node: A.ButtonDecl, start: int) -> None:
        mods: list[str] = []
        if node.style is not None:
            mods.append(f"style: {node.style}")
        if node.custom_id is not None:
            mods.append(f"id: {_quote(node.custom_id)}")
        if node.url is not None:
            mods.append(f"url: {_quote(node.url)}")
        if node.timeout is not None:
            mods.append("timeout: " + self._duration_text(
                node.timeout, node.timeout_span))
        self._line(f"button {_quote(node.label)}"
                   f"{self._component_mods(mods)}:",
                   src_line=start)
        self._begin_block()
        self._emit_only(node.only_kind, node.only_roles)
        self._emit_do(node.body, start)
        self._emit_on_timeout(node.on_timeout, start)
        self._end_block()

    def _emit_select(self, node: A.SelectDecl, start: int) -> None:
        mods: list[str] = []
        if node.options:
            quoted = ", ".join(_quote(o) for o in node.options)
            mods.append(f"options: [{quoted}]")
        if node.min_values is not None:
            mods.append(f"min: {node.min_values}")
        if node.max_values is not None:
            mods.append(f"max: {node.max_values}")
        if node.timeout is not None:
            mods.append("timeout: " + self._duration_text(
                node.timeout, node.timeout_span))
        self._line(f"select {_quote(node.label)}"
                   f"{self._component_mods(mods)}:",
                   src_line=start)
        self._begin_block()
        self._emit_only(node.only_kind, node.only_roles)
        self._emit_do(node.body, start)
        self._emit_on_timeout(node.on_timeout, start)
        self._end_block()

    def _emit_modal(self, node: A.ModalDecl, start: int) -> None:
        mods: list[str] = []
        if node.custom_id is not None:
            mods.append(f"id: {_quote(node.custom_id)}")
        if node.timeout is not None:
            mods.append("timeout: " + self._duration_text(
                node.timeout, node.timeout_span))
        self._line(f"modal {_quote(node.title)}"
                   f"{self._component_mods(mods)}:",
                   src_line=start)
        self._begin_block()
        for field in node.fields:
            fmods: list[str] = []
            if field.style is not None:
                fmods.append(f"style: {field.style}")
            if field.required:
                fmods.append("required")
            if field.min_length is not None:
                fmods.append(f"min_length: {field.min_length}")
            if field.max_length is not None:
                fmods.append(f"max_length: {field.max_length}")
            fline = self._line_no(field.span.start_byte) \
                if self.source is not None else None
            self._line(f"text_input {_quote(field.name)}"
                       f"{self._component_mods(fmods)}:",
                       src_line=fline)
        self._emit_do(node.body, start)
        self._emit_on_timeout(node.on_timeout, start)
        self._end_block()

    # -------------------------------------------------------- patterns

    def _pattern(self, node) -> str:
        if isinstance(node, A.LitPattern):
            if self.source is not None:
                return self._slice(node.span)
            value = node.value
            if isinstance(value, bool):
                return "true" if value else "false"
            if value is None:
                return "none"
            if isinstance(value, float):
                return _format_float(value)
            if isinstance(value, int):
                return str(value)
            return _quote(str(value))
        if isinstance(node, A.WildcardPattern):
            return "_"
        if isinstance(node, A.NamePattern):
            return node.name
        if isinstance(node, A.EnumPattern):
            text = f"{node.enum_name}.{node.variant}"
            if node.bindings:
                inner = ", ".join(
                    f"{name}: {self._pattern(sub)}"
                    for name, sub, _span in node.bindings)
                text += f"({inner})"
            return text
        if isinstance(node, A.StructPattern):
            inner = ", ".join(
                f"{name}: {self._pattern(sub)}"
                for name, sub, _span in node.fields)
            return f"{node.struct_name}({inner})"
        if isinstance(node, A.ListPattern):
            return "[" + ", ".join(self._pattern(p)
                                  for p in node.elements) + "]"
        if isinstance(node, A.OrPattern):
            return " | ".join(self._pattern(p)
                              for p in node.patterns)
        raise TypeError(f"cannot format pattern node "
                        f"{type(node).__name__}")

    # ----------------------------------------------------- declarations

    def _emit_decl(self, node, first: bool = False) -> None:
        start = self._start_line(node)
        before = len(self._lines)
        self._flush_comments_before(start, first=first)
        self._maybe_blank(start, first and len(self._lines) == before)
        self._emit_decl_inner(node, start)
        self._last_consumed_line = max(self._last_consumed_line,
                                       self._end_line(node))

    def _emit_decl_inner(self, node, start: int) -> None:
        if isinstance(node, A.Import):
            if node.path is not None:
                text = f"import {_quote(node.path)}"
            else:
                text = f"import {node.module}"
            if node.alias is not None:
                text += f" as {node.alias}"
            self._line(text, src_line=start)
        elif isinstance(node, A.StructDecl):
            head = "pub " if node.pub else ""
            self._line(f"{head}struct {node.name}:", src_line=start)
            self._begin_block()
            for field in node.fields:
                fline = self._line_no(field.span.start_byte) \
                    if self.source is not None else None
                self._flush_comments_before(
                    fline if fline is not None else start,
                    allow_blank=False)
                self._line(f"{field.name}: {field.type}",
                           src_line=fline)
            self._end_block()
        elif isinstance(node, A.EnumDecl):
            head = "pub " if node.pub else ""
            self._line(f"{head}enum {node.name}:", src_line=start)
            self._begin_block()
            for variant in node.variants:
                vline = self._line_no(variant.span.start_byte) \
                    if self.source is not None \
                    and variant.span is not None else None
                self._flush_comments_before(
                    vline if vline is not None else start,
                    allow_blank=False)
                text = variant.name
                if variant.fields:
                    inner = ", ".join(
                        f"{f.name}: {f.type}" for f in variant.fields)
                    text += f"({inner})"
                self._line(text, src_line=vline)
            self._end_block()
        elif isinstance(node, A.TestBlock):
            self._line(f"test {_quote(node.name)}:", src_line=start)
            self._emit_block(node.body)
        elif isinstance(node, A.FnDecl):
            head = "pub " if node.pub else ""
            params = ", ".join(
                f"{p.name}: {p.type}" for p in node.params)
            ret = f" -> {node.return_type}" if node.return_type else ""
            self._line(f"{head}fn {node.name}({params}){ret}:",
                       src_line=start)
            self._emit_block(node.body)
        elif isinstance(node, A.BotDecl):
            self._line(f"bot {_quote(node.name)}:", src_line=start)
            self._begin_block()
            if node.description is not None:
                self._line(f"description: {_quote(node.description)}",
                           src_line=None)
            if node.intents is not None:
                self._line(f"intents: [{', '.join(node.intents)}]",
                           src_line=None)
            self._end_block()
        elif isinstance(node, A.CommandDecl):
            self._emit_command(node, start)
        elif isinstance(node, A.EventDecl):
            self._emit_event(node, start)
        elif isinstance(node, A.TaskDecl):
            span = self._find_duration_span(node.span)
            dur = self._duration_text(node.interval_seconds, span)
            self._line(f"task every {dur}:", src_line=start)
            self._begin_block()
            self._emit_do(node.body, start)
            self._end_block()
        elif isinstance(node, A.StringsDecl):
            self._line(f"strings {node.locale}:", src_line=start)
            self._begin_block()
            for entry in node.entries:
                eline = self._line_no(entry.span.start_byte) \
                    if self.source is not None else None
                self._flush_comments_before(
                    eline if eline is not None else start,
                    allow_blank=False)
                self._line(f"{_quote(entry.key)}: "
                           f"{_quote_template(entry.template, entry.placeholders)}",
                           src_line=eline)
            self._end_block()
        elif isinstance(node, A.StoreDecl):
            self._line(f"store {node.name}:", src_line=start)
            self._begin_block()
            for table in node.tables:
                tline = self._line_no(table.span.start_byte) \
                    if self.source is not None else None
                self._flush_comments_before(
                    tline if tline is not None else start,
                    allow_blank=False)
                cols = ", ".join(
                    f"{c.name}: {c.type}" for c in table.columns)
                self._line(f"table {table.name} ({cols})",
                           src_line=tline)
            if node.ledger:
                self._line("ledger true", src_line=None)
            self._end_block()
        else:
            raise TypeError(f"cannot format declaration node "
                            f"{type(node).__name__}")

    def _emit_command(self, node: A.CommandDecl, start: int) -> None:
        self._line(f"command {node.name}:", src_line=start)
        self._begin_block()
        if node.description is not None:
            self._line(f"description: {_quote(node.description)}",
                       src_line=None)
        for opt in node.options:
            self._emit_option(opt)
        if node.reply_expr is not None:
            self._line(f"reply: {self._expr(node.reply_expr)}",
                       src_line=None)
        if node.ephemeral:
            self._line("ephemeral: true", src_line=None)
        if node.body is not None:
            self._emit_do(node.body, start)
        self._end_block()

    def _emit_option(self, node: A.OptionDecl) -> None:
        mods: list[str] = []
        if node.required:
            mods.append("required")
        if node.default is not None:
            mods.append(f"default: {self._expr(node.default)}")
        if node.choices is not None:
            inner = ", ".join(self._expr(c) for c in node.choices)
            mods.append(f"choices: [{inner}]")
        if node.min_value is not None:
            mods.append(f"min: {self._expr(node.min_value)}")
        if node.max_value is not None:
            mods.append(f"max: {self._expr(node.max_value)}")
        if node.description is not None:
            mods.append(f"description: {_quote(node.description)}")
        text = f"option {node.name}: {node.type}"
        text += self._component_mods(mods)
        self._line(text, src_line=self._line_no(node.span.start_byte)
                   if self.source is not None else None)

    def _emit_event(self, node: A.EventDecl, start: int) -> None:
        self._line(f"event {node.kind}:", src_line=start)
        self._begin_block()
        if node.log_expr is not None:
            self._line(f"log: {self._expr(node.log_expr)}",
                       src_line=None)
        elif node.reply_expr is not None:
            self._line(f"reply: {self._expr(node.reply_expr)}",
                       src_line=None)
        elif node.body is not None:
            self._emit_do(node.body, start)
        self._end_block()

    # -------------------------------------------------------- top level

    def format(self) -> str:
        """Format the whole program; exactly one trailing newline."""
        prog = self.program
        # (start byte, node, is-statement): top-level items are kept
        # in source order, not grouped by kind.
        items: list[tuple[int, object, bool]] = []
        for node in prog.imports:
            items.append((node.span.start_byte, node, False))
        for node in prog.structs:
            items.append((node.span.start_byte, node, False))
        for node in prog.enums:
            items.append((node.span.start_byte, node, False))
        # A test block's synthetic function (`_newr_test_<n>`) is
        # never printed: the block itself renders in its place
        # (same span, so it sorts to the same position).
        synthetic = {tb.func_name for tb in prog.test_blocks}
        for node in prog.functions:
            if node.name in synthetic:
                continue
            items.append((node.span.start_byte, node, False))
        for node in prog.test_blocks:
            items.append((node.span.start_byte, node, False))
        if prog.bot is not None:
            items.append((prog.bot.span.start_byte, prog.bot, False))
        for node in prog.commands:
            items.append((node.span.start_byte, node, False))
        for node in prog.events:
            items.append((node.span.start_byte, node, False))
        for node in prog.tasks:
            items.append((node.span.start_byte, node, False))
        for node in prog.strings:
            items.append((node.span.start_byte, node, False))
        for node in prog.stores:
            items.append((node.span.start_byte, node, False))
        for node in prog.stmts:
            items.append((node.span.start_byte, node, True))
        items.sort(key=lambda item: item[0])
        for i, (_byte, node, is_stmt) in enumerate(items):
            if is_stmt:
                self._emit_stmt(node, first=(i == 0))
            else:
                self._emit_decl(node, first=(i == 0))
        self._flush_remaining_comments()
        text = "\n".join(self._lines).rstrip("\n")
        return text + "\n" if text else ""


def format_program(program: A.Program, source: str | None = None,
                   filename: str = "<string>") -> str:
    """Format an already-parsed program.

    Pass the source text when available: literals and durations are
    sliced verbatim from it and ``#`` comments are preserved. Without
    source, literals render from their values and comments are lost.
    """
    return _Formatter(program, source, filename).format()


def format_source(source: str, filename: str = "<string>") -> str:
    """Parse newr source and format it. Raises NewrError (E1001) on
    the first syntax error, like ``parse``."""
    return format_program(parse(source, filename), source, filename)


def is_formatted(source: str, filename: str = "<string>") -> bool:
    """True when the source is already in canonical form."""
    return format_source(source, filename) == source
