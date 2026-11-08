"""Lexer for the newr language (SPEC.md §6).

Produces tokens with byte-offset spans (SPEC.md §16 — line/col are
derived from the byte offsets at render time). Handles:

- 2-space indentation (INDENT/DEDENT tokens; tabs banned; indent steps
  must be exactly +2; dedents must land on a level on the stack).
- ``#`` comments and blank lines (ignored, and they do not affect the
  indent stack).
- CRLF normalized to LF; a lone CR is an error; a BOM is stripped.
  Spans stay **raw-file byte offsets** (SPEC.md §16): the lexer
  normalizes CRLF → LF for tokenizing, but every span counts the CR
  bytes, so JSON offsets match the bytes on disk.
- Double-quoted strings with escapes (``\\" \\\\ \\n \\t \\r \\u{XXXX}``)
  and interpolation (``{expr}``; ``{{``/``}}`` escape to literal braces).
  The lexer emits STR_START / STR_PART / INTERP_START / INTERP_END /
  STR_END so the parser can build an Interp node with correct spans.
- Numbers: decimal ints (``1_000``), hex (``0xFF``), binary (``0b1010``),
  floats (``3.14``, ``1e3``). ``int`` is a 64-bit signed integer.
- Duration literals: ``60s``, ``5m``, ``1h``, ``1d`` (positive integers
  with a unit suffix, no space). Durations are syntax-only literals in
  v1 — valid only in ``task every ...`` and ``timeout:`` positions (the
  parser enforces this).
- Newlines inside brackets (``(``/``[``/``{``) are skipped (expressions
  may span lines inside brackets only).

No POSIX-only code: this module is pure Python string processing, and it
works identically on Linux, macOS, and Windows (SPEC.md §3).
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from .errors import Diagnostic, NewrError, Span


def _is_name_start(ch: str) -> bool:
    """First character of an identifier: a Unicode letter or ``_``.

    Round-2 CRASH-1: ``_peek()`` returns ``""`` at EOF, and
    ``unicodedata.category("")`` raises TypeError. A non-single
    character is never a name start.
    """
    return len(ch) == 1 and (ch == "_" or unicodedata.category(ch)[0] == "L")


def _is_name_continue(ch: str) -> bool:
    """Later characters of an identifier: letters, marks, numbers, ``_``.

    Marks (category M) are included deliberately: Discord command names
    may contain Thai/Devanagari combining marks (SPEC.md §13.2.1, ported
    from the v0 regex), and identifiers in those scripts need them too.

    Round-2 CRASH-1: see _is_name_start — ``""`` at EOF is not a
    name-continue character.
    """
    return len(ch) == 1 and (ch == "_" or
                             unicodedata.category(ch)[0] in "LMN")


def _is_hex_digit(ch: str) -> bool:
    return ch in "0123456789abcdefABCDEF"


def _is_bin_digit(ch: str) -> bool:
    return ch in "01"

KEYWORDS = {
    "and", "as", "bot", "break", "button", "catch", "command", "const",
    "continue", "do", "elif", "else", "embed", "enum", "event", "false",
    "finally", "fn", "for", "from", "if", "import", "in", "let", "log",
    "match", "modal", "none", "not", "only", "or", "pass", "pub",
    "reply", "requires", "ensures", "return", "select", "send",
    "store", "strings", "struct", "task", "test", "throw", "true", "try",
    "while",
}

INT64_MIN = -(2 ** 63)
INT64_MAX = 2 ** 63 - 1

_DURATION_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400}

#: Whitespace run (spaces/tabs) — C-speed scan for _process_indent and
#: _handle_newline (round-2 perf fix). The old per-char loops hung at
#: EOF because "" in " \t" is True in Python.
_WS_RUN = re.compile(r"[ \t]*")

#: ASCII identifier run — fast path for _lex_name (round-2 perf fix).
#: Only used when the character after the run is not a name-continue
#: character, so Unicode identifiers still take the per-char path.
_ASCII_NAME_RUN = re.compile(r"[A-Za-z0-9_]+")

_SINGLE_CHAR_TOKENS = {
    "(": "LPAREN", ")": "RPAREN",
    "[": "LBRACK", "]": "RBRACK",
    "{": "LBRACE", "}": "RBRACE",
    ",": "COMMA", ":": "COLON", ".": "DOT",
    "+": "PLUS", "-": "MINUS", "*": "STAR",
    "/": "SLASH", "%": "PERCENT",
    "?": "QMARK",
    # A lone "|" is the or-pattern separator (SPEC.md §9.6, `1 | 2 | 3`).
    # It is only valid inside a match pattern; anywhere else the parser
    # rejects it with E1001. ("|>" is lexed as PIPE above.)
    "|": "BAR",
}

_TWO_CHAR_TOKENS = {
    "**": "POWER", "==": "EQ", "!=": "NEQ",
    "<=": "LTE", ">=": "GTE", "+=": "PLUS_EQ", "-=": "MINUS_EQ",
    "*=": "STAR_EQ", "/=": "SLASH_EQ", "->": "ARROW",
    "?.": "QDOT", "??": "QQ", "|>": "PIPE",
}


@dataclass
class Token:
    kind: str
    value: object
    span: Span

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Token({self.kind}, {self.value!r}, {self.span})"


class _Lexer:
    def __init__(self, source: str, filename: str):
        self.filename = filename
        bom_bytes = 0
        if source.startswith("\ufeff"):
            source = source[1:]
            bom_bytes = 3
        # Normalize CRLF -> LF for tokenizing, tracking the RAW byte
        # length of every normalized character so spans stay raw-file
        # byte offsets (SPEC.md §16, M-06): a CRLF newline is 2 bytes
        # on disk, 1 character after normalization. A lone CR is an
        # error (E_BAD_LINE_ENDING) reported at its raw byte offset.
        if "\r" not in source and source.isascii():
            # Fast path (round-2 perf fix): no CR to normalize and
            # every character is 1 byte, so the source is already
            # normalized and byte offset == char index + bom_bytes.
            # _byte_prefix is a range (O(1) memory, O(1) indexing)
            # instead of a per-character list — a 25 MB file would
            # otherwise build a 25M-entry prefix list.
            self.source = source
            self._byte_prefix = range(bom_bytes,
                                      bom_bytes + len(source) + 1)
        else:
            norm: list[str] = []
            raw_lens: list[int] = []
            byte = bom_bytes
            i = 0
            while i < len(source):
                ch = source[i]
                if ch == "\r":
                    if i + 1 < len(source) and source[i + 1] == "\n":
                        norm.append("\n")
                        raw_lens.append(2)
                        byte += 2
                        i += 2
                    else:
                        raise NewrError(Diagnostic(
                            span=Span(self.filename, byte, byte + 1),
                            code="E_BAD_LINE_ENDING",
                            message="a lone carriage return (CR) is not a line "
                                    "ending; use LF or CRLF"))
                else:
                    norm.append(ch)
                    n = len(ch.encode("utf-8"))
                    raw_lens.append(n)
                    byte += n
                    i += 1
            source = "".join(norm)
            self.source = source
            # Byte offset of every char index (prefix sums of RAW byte
            # lengths), so a (line, col) pair converts to a raw-file
            # byte-offset span in O(1).
            self._byte_prefix = [bom_bytes]
            for n in raw_lens:
                self._byte_prefix.append(self._byte_prefix[-1] + n)
        self.pos = 0
        self.line = 1
        self.col = 1
        # Char index where each line starts (index 0 = line 1). Entries
        # are appended as newlines are crossed; the guard in _advance
        # keeps re-crossed newlines (after a lookahead restore) from
        # duplicating an entry.
        self._line_starts = [0]
        self.tokens: list[Token] = []
        self.indent_stack: list[int] = [0]
        self.paren_depth = 0
        # True when the next token starts a logical line: the indentation
        # of that line must be processed before the token is lexed.
        self.at_line_start = True
        # Mode stack: "normal" | ("string",) | ("interp", brace_depth)
        self.modes: list = ["normal"]
        # Accumulator for the current string literal part.
        self.str_part: list[str] = []
        self.str_part_start: tuple[int, int] | None = None

    # ------------------------------------------------------------ helpers

    def _byte(self, line: int, col: int) -> int:
        """Byte offset of a 1-based (line, col) pair (col in characters).

        The exclusive end of a token at EOF is one past the last
        character; it clamps to the end of the source.
        """
        line_start = self._line_starts[line - 1]
        char_pos = min(line_start + (col - 1), len(self.source))
        return self._byte_prefix[char_pos]

    def _err(self, code: str, line: int, col: int, message: str,
             help: str | None = None) -> NewrError:
        span = Span(self.filename, self._byte(line, col),
                    self._byte(line, col + 1))
        return NewrError(Diagnostic(span=span, code=code, message=message,
                                    help=help))

    def _peek(self, offset: int = 0) -> str:
        idx = self.pos + offset
        return self.source[idx] if idx < len(self.source) else ""

    def _advance(self, count: int = 1) -> str:
        out = self.source[self.pos:self.pos + count]
        if "\n" not in out:
            # Fast path (round-2 perf fix): no newline crossed, so the
            # line does not change and the column advances by the run
            # length. (At EOF `out` may be shorter than `count`; the
            # column advances by what was actually consumed, exactly
            # like the loop below, while `pos` still advances by
            # `count` as before.)
            self.col += len(out)
            self.pos += count
            return out
        for i, ch in enumerate(out):
            if ch == "\n":
                self.line += 1
                self.col = 1
                if len(self._line_starts) == self.line - 1:
                    self._line_starts.append(self.pos + i + 1)
            else:
                self.col += 1
        self.pos += count
        return out

    def _emit(self, kind: str, value: object, line: int, col: int,
              end_line: int | None = None, end_col: int | None = None):
        span = Span(self.filename, self._byte(line, col),
                    self._byte(end_line if end_line is not None else line,
                               end_col if end_col is not None else col + 1))
        self.tokens.append(Token(kind, value, span))

    # ------------------------------------------------------------ main loop

    def tokenize(self) -> list[Token]:
        while self.pos < len(self.source):
            mode = self.modes[-1]
            if mode == "normal":
                if self.at_line_start:
                    self._process_indent()
                    continue
                self._lex_normal()
            elif mode == "string":
                self._lex_string_char()
            else:  # ("interp", depth)
                self._lex_interp()
        # Flush a pending string part (a string at EOF is an error, raised
        # by _lex_string_char before we get here).
        if self.modes[-1] != "normal":
            raise self._err("E_UNTERMINATED_STRING", self.line, self.col,
                            "unterminated string")
        # Emit remaining dedents, then EOF.
        while len(self.indent_stack) > 1:
            self.indent_stack.pop()
            self._emit("DEDENT", None, self.line, self.col)
        self._emit("EOF", None, self.line, self.col)
        return self.tokens

    # ------------------------------------------------------------ normal mode

    def _lex_normal(self) -> None:
        ch = self._peek()
        line, col = self.line, self.col

        if ch == "\n":
            self._handle_newline()
            return

        if ch in " \t":
            # Plain whitespace between tokens (indentation was already
            # handled by _process_indent at the line start).
            self._advance()
            return

        if ch == "#":
            # Bulk comment skip (round-2 perf fix): find the newline in
            # C instead of advancing one character at a time.
            end = self.source.find("\n", self.pos)
            if end == -1:
                end = len(self.source)
            self._advance(end - self.pos)
            return

        if ch == '"':
            self._advance()
            self.modes.append("string")
            self.str_part = []
            self.str_part_start = (line, col)
            self._emit("STR_START", None, line, col, line, col + 1)
            return

        if ch.isdigit():
            self._lex_number()
            return

        if _is_name_start(ch):
            self._lex_name()
            return

        two = self.source[self.pos:self.pos + 2]
        if two in _TWO_CHAR_TOKENS:
            self._advance(2)
            self._emit(_TWO_CHAR_TOKENS[two], two, line, col,
                       line, col + 2)
            return

        if ch in _SINGLE_CHAR_TOKENS:
            self._advance()
            kind = _SINGLE_CHAR_TOKENS[ch]
            if ch in "([{":
                self.paren_depth += 1
            elif ch in ")]}":
                self.paren_depth -= 1
            self._emit(kind, ch, line, col, line, col + 1)
            return

        if ch == "<":
            self._advance()
            self._emit("LT", "<", line, col, line, col + 1)
            return
        if ch == ">":
            self._advance()
            self._emit("GT", ">", line, col, line, col + 1)
            return
        if ch == "=":
            self._advance()
            self._emit("ASSIGN", "=", line, col, line, col + 1)
            return

        raise self._err("E_UNEXPECTED_CHAR", line, col,
                        f"unexpected character {ch!r}")

    def _handle_newline(self) -> None:
        line, col = self.line, self.col
        self._advance()  # consume "\n"
        if self.paren_depth > 0:
            # Inside brackets: newlines are skipped.
            self.at_line_start = True
            return
        # Skip blank lines and comment-only lines (they do not affect
        # the indent stack and produce no NEWLINE token).
        while self.pos < len(self.source):
            save_pos, save_line, save_col = self.pos, self.line, self.col
            # Bulk whitespace skip (round-2 perf fix): the old per-char
            # loop hung at EOF because "" in " \t" is True. The regex
            # scan is C-speed and stops at end-of-source.
            run = _WS_RUN.match(self.source, self.pos).group(0)
            if run:
                self._advance(len(run))
            nxt = self._peek()
            if nxt == "\n":
                self._advance()
                continue
            if nxt == "#":
                end = self.source.find("\n", self.pos)
                if end == -1:
                    end = len(self.source)
                self._advance(end - self.pos)
                if self.pos < len(self.source):
                    self._advance()  # the newline after the comment
                    continue
                break
            # A real line: restore to its first character (the indent
            # processor measures the leading whitespace).
            self.pos, self.line, self.col = save_pos, save_line, save_col
            break
        self._emit("NEWLINE", "\n", line, col, line, col + 1)
        self.at_line_start = True

    def _process_indent(self) -> None:
        """Process the indentation of the logical line at the cursor.

        Called once per logical line (when ``at_line_start`` is set).
        Emits INDENT/DEDENT tokens, or raises on bad indentation. Blank
        and comment-only lines are ignored here (the newline handler
        skips them); inside brackets, indentation is ignored entirely.
        """
        self.at_line_start = False
        if self.pos >= len(self.source):
            return
        line, col = self.line, self.col
        start_col = self.col
        # Bulk whitespace scan (round-2 perf fix): the old per-char
        # loop hung at EOF because "" in " \t" is True. A tab anywhere
        # in the run is still E_TAB_INDENT at the tab's own position.
        run = _WS_RUN.match(self.source, self.pos).group(0)
        if "\t" in run:
            raise self._err(
                "E_TAB_INDENT", self.line, self.col + run.index("\t"),
                "tab indentation is not allowed; use spaces")
        if run:
            self._advance(len(run))
        if self.paren_depth > 0:
            return  # inside brackets: indentation is ignored
        nxt = self._peek()
        if nxt in ("", "\n", "#"):
            # Blank, comment-only, or EOF line: nothing to do here — but
            # leave at_line_start False so _lex_normal runs and consumes
            # the newline / comment. (Setting it True again would loop
            # forever: tokenize only calls _lex_normal when it is False,
            # so a leading comment line used to hang the lexer.)
            self.at_line_start = False if nxt != "" else True
            return
        indent = self.col - start_col
        current = self.indent_stack[-1]
        if indent > current:
            if indent != current + 2:
                raise self._err(
                    "E_BAD_INDENT", line, col,
                    f"expected {current + 2} spaces of indentation, "
                    f"got {indent}",
                    help="indent by exactly 2 spaces per level")
            self.indent_stack.append(indent)
            self._emit("INDENT", None, line, col, line, col + 1)
        elif indent < current:
            while self.indent_stack and self.indent_stack[-1] > indent:
                self.indent_stack.pop()
                self._emit("DEDENT", None, line, col, line, col + 1)
            if self.indent_stack[-1] != indent:
                raise self._err(
                    "E_BAD_INDENT", line, col,
                    f"dedent to {indent} spaces does not match any "
                    f"indentation level",
                    help="dedent to a level the block was opened at")
        # indent == current: nothing to do.

    def _lex_name(self) -> None:
        line, col = self.line, self.col
        start = self.pos
        # Fast path (round-2 perf fix): an ASCII name run is matched in
        # C. It is only used when the character after the run is not a
        # name-continue character (a Unicode letter/mark/number would
        # extend the name) — otherwise the per-char loop below runs.
        m = _ASCII_NAME_RUN.match(self.source, self.pos)
        if m is not None:
            end = m.end()
            if end >= len(self.source) or \
                    not _is_name_continue(self.source[end]):
                self._advance(end - start)
                word = self.source[start:self.pos]
                kind = "KEYWORD" if word in KEYWORDS else "IDENT"
                self._emit(kind, word, line, col, self.line, self.col)
                return
        while _is_name_continue(self._peek()):
            self._advance()
        word = self.source[start:self.pos]
        kind = "KEYWORD" if word in KEYWORDS else "IDENT"
        self._emit(kind, word, line, col, self.line, self.col)

    def _check_underscores(self, run: str, display: str, is_digit,
                           line: int, col: int) -> None:
        """Reject a misplaced underscore in a number literal (round-2).

        SPEC.md SS6: underscores group digits — so one must sit
        between two digits of the same run (``1_000``, ``0xF_F``). A
        leading, trailing, or doubled underscore groups nothing.
        """
        for j, ch in enumerate(run):
            if ch == "_" and not (0 < j < len(run) - 1
                                  and is_digit(run[j - 1])
                                  and is_digit(run[j + 1])):
                raise self._err(
                    "E_BAD_NUMBER", line, col,
                    "invalid underscore placement in number literal "
                    f"`{display}`",
                    help="underscores group digits: write `1_000`, "
                         "not `1__2` or `1_`")

    def _lex_number(self) -> None:
        line, col = self.line, self.col
        start = self.pos
        # Hex / binary.
        if self.source[self.pos:self.pos + 2] in ("0x", "0X"):
            self._advance(2)
            digits = ""
            # Round-2 CRASH-1: the loop needs a pos guard — at EOF
            # "" in "0123..._" is True, so the loop spun forever.
            while self.pos < len(self.source) and \
                    self._peek() in "0123456789abcdefABCDEF_":
                digits += self._advance()
            if not digits.replace("_", ""):
                raise self._err("E_BAD_NUMBER", line, col,
                                "hex literal has no digits")
            self._check_underscores(digits, "0x" + digits,
                                    _is_hex_digit, line, col)
            try:
                value = int(digits.replace("_", ""), 16)
            except ValueError:
                raise self._err("E_BAD_NUMBER", line, col,
                                f"invalid hex literal 0x{digits}")
            # The int64 range check (M-05): 2**63 is allowed here — the
            # checker rejects it unless it is directly negated (M-08,
            # `-9223372036854775808`); anything larger is an error now.
            if value > INT64_MAX + 1:
                raise self._err(
                    "E_INT_OVERFLOW", line, col,
                    f"integer literal 0x{digits} is outside the 64-bit "
                    f"signed range",
                    help="int is a 64-bit signed integer on every target")
            self._emit("INT", value, line, col, self.line, self.col)
            return
        if self.source[self.pos:self.pos + 2] in ("0b", "0B"):
            self._advance(2)
            digits = ""
            # Round-2 CRASH-1: same EOF guard as the hex loop above.
            while self.pos < len(self.source) and \
                    self._peek() in "01_":
                digits += self._advance()
            if not digits.replace("_", ""):
                raise self._err("E_BAD_NUMBER", line, col,
                                "binary literal has no digits")
            self._check_underscores(digits, "0b" + digits,
                                    _is_bin_digit, line, col)
            value = int(digits.replace("_", ""), 2)
            if value > INT64_MAX + 1:
                raise self._err(
                    "E_INT_OVERFLOW", line, col,
                    f"integer literal 0b{digits} is outside the 64-bit "
                    f"signed range",
                    help="int is a 64-bit signed integer on every target")
            self._emit("INT", value,
                       line, col, self.line, self.col)
            return
        # Decimal / float.
        is_float = False
        while self._peek().isdigit() or self._peek() == "_":
            self._advance()
        if self._peek() == "." and self._peek(1).isdigit():
            is_float = True
            self._advance()  # "."
            while self._peek().isdigit() or self._peek() == "_":
                self._advance()
        # Round-2 CRASH-1: harden the exponent check with a pos guard.
        # (It was safe by luck — the inner digit test fails at EOF —
        # but "" in "eE" is True, so the trap was live.)
        if self.pos < len(self.source) and self._peek() in "eE":
            nxt = self._peek(1)
            nxt2 = self._peek(2)
            if nxt.isdigit() or (nxt in "+-" and nxt2.isdigit()):
                is_float = True
                self._advance()  # e/E
                if self._peek() in "+-":
                    self._advance()
                while self._peek().isdigit() or self._peek() == "_":
                    self._advance()
        raw = self.source[start:self.pos]
        self._check_underscores(raw, raw, str.isdigit, line, col)
        text = raw.replace("_", "")
        # A duration literal: an integer immediately followed by s/m/h/d
        # and then a non-identifier character.
        unit = self._peek()
        after = self._peek(1)
        if (not is_float and unit in _DURATION_SECONDS
                and not (after.isalnum() or after == "_")):
            self._advance()
            value = int(text) * _DURATION_SECONDS[unit]
            self._emit("DURATION", value, line, col, self.line, self.col)
            return
        if is_float:
            self._emit("FLOAT", float(text), line, col, self.line, self.col)
            return
        value = int(text)
        # 2**63 is allowed here — the checker rejects it unless it is
        # directly negated (M-08, `-9223372036854775808`); anything
        # larger is an error now.
        if value > INT64_MAX + 1:
            raise self._err(
                "E_INT_OVERFLOW", line, col,
                f"integer literal {text} is outside the 64-bit signed "
                f"range",
                help="int is a 64-bit signed integer on every target")
        self._emit("INT", value, line, col, self.line, self.col)

    # ------------------------------------------------------------ string mode

    def _flush_str_part(self) -> None:
        if not self.str_part:
            return
        text = "".join(self.str_part)
        start_line, start_col = self.str_part_start or (self.line, self.col)
        self._emit("STR_PART", text, start_line, start_col,
                   self.line, self.col)
        self.str_part = []
        self.str_part_start = None

    def _lex_string_char(self) -> None:
        ch = self._peek()
        line, col = self.line, self.col
        if ch == "":
            raise self._err("E_UNTERMINATED_STRING", line, col,
                            "unterminated string")
        if ch == "\n":
            raise self._err("E_UNTERMINATED_STRING", line, col,
                            "unterminated string (strings may not span "
                            "lines; use \\n)")
        if ch == "\x00":
            # Round-2: a raw NUL byte inside a string is rejected, like
            # a raw NUL outside one (E_UNEXPECTED_CHAR) and like a raw
            # newline inside one. SPEC.md SS6 lists the escapes; the
            # way to write a NUL is \u{0}.
            raise self._err(
                "E_UNEXPECTED_CHAR", line, col,
                "NUL byte is not allowed in a string literal",
                help="write \\u{0} for a NUL character")
        if ch == '"':
            self._flush_str_part()
            self._advance()
            self.modes.pop()
            self._emit("STR_END", None, line, col, line, col + 1)
            return
        if ch == "{":
            if self._peek(1) == "{":
                # "{{" -> a literal "{".
                if self.str_part_start is None:
                    self.str_part_start = (line, col)
                self.str_part.append("{")
                self._advance(2)
                return
            self._flush_str_part()
            self._advance()
            self.modes.append(("interp", 0))
            self._emit("INTERP_START", "{", line, col, line, col + 1)
            return
        if ch == "}":
            if self._peek(1) == "}":
                if self.str_part_start is None:
                    self.str_part_start = (line, col)
                self.str_part.append("}")
                self._advance(2)
                return
            raise self._err(
                "E_STRAY_BRACE", line, col,
                "stray } in string (use }} for a literal })")
        if ch == "\\":
            if self.str_part_start is None:
                self.str_part_start = (line, col)
            self._advance()  # backslash
            esc = self._peek()
            esc_line, esc_col = self.line, self.col
            if esc == '"':
                self.str_part.append('"')
                self._advance()
            elif esc == "\\":
                self.str_part.append("\\")
                self._advance()
            elif esc == "n":
                self.str_part.append("\n")
                self._advance()
            elif esc == "t":
                self.str_part.append("\t")
                self._advance()
            elif esc == "r":
                self.str_part.append("\r")
                self._advance()
            elif esc == "u":
                self._advance()  # "u"
                if self._peek() != "{":
                    raise self._err(
                        "E_BAD_ESCAPE", esc_line, esc_col,
                        "expected \\u{XXXX} (a Unicode code point in "
                        "braces)")
                self._advance()  # "{"
                digits = ""
                while self._peek() not in ("", "}"):
                    digits += self._advance()
                if self._peek() != "}":
                    raise self._err(
                        "E_BAD_ESCAPE", esc_line, esc_col,
                        "unterminated \\u{...} escape")
                self._advance()  # "}"
                try:
                    code = int(digits, 16)
                    if not (0 <= code <= 0x10FFFF) or len(digits) > 6:
                        raise ValueError
                    self.str_part.append(chr(code))
                except ValueError:
                    raise self._err(
                        "E_BAD_ESCAPE", esc_line, esc_col,
                        f"invalid Unicode escape \\u{{{digits}}}")
            else:
                raise self._err(
                    "E_BAD_ESCAPE", esc_line, esc_col,
                    f"unknown escape \\{esc}",
                    help="valid escapes: \\\" \\\\ \\n \\t \\r \\u{XXXX}")
            return
        if self.str_part_start is None:
            self.str_part_start = (line, col)
        self.str_part.append(self._advance())

    # ------------------------------------------------------------ interp mode

    def _lex_interp(self) -> None:
        mode = self.modes[-1]
        _, depth = mode
        ch = self._peek()
        line, col = self.line, self.col
        if ch == "":
            raise self._err("E_UNTERMINATED_STRING", line, col,
                            "unterminated string (unclosed { in "
                            "interpolation)")
        if ch == "\n":
            raise self._err("E_UNTERMINATED_STRING", line, col,
                            "unterminated string (interpolation must fit "
                            "on one line)")
        if ch == "{":
            self.modes[-1] = ("interp", depth + 1)
            self._advance()
            if ch in "([{":
                self.paren_depth += 1
            self._emit("LBRACE", "{", line, col, line, col + 1)
            return
        if ch == "}":
            if depth == 0:
                self.modes.pop()
                self._advance()
                self._emit("INTERP_END", "}", line, col, line, col + 1)
                self.str_part_start = (self.line, self.col)
                return
            self.modes[-1] = ("interp", depth - 1)
            self._advance()
            self.paren_depth -= 1
            self._emit("RBRACE", "}", line, col, line, col + 1)
            return
        # Otherwise lex one normal token (ident, number, operator, string,
        # punctuation). A nested string pushes a new string mode.
        self._lex_normal()


def tokenize(source: str, filename: str = "<string>") -> list[Token]:
    """Tokenize newr source. Raises NewrError on the first lex error."""
    return _Lexer(source, filename).tokenize()
