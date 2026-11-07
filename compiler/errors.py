"""Diagnostics for the newr compiler (SPEC.md §16).

Rustc form, Elm tone (G10):

    error[E2001]: mismatched types
      --> file.newr:12:9
       |
    12 |     let x: int = "hello"
       |         ^^^^^^   ^^^^^^^ expected `int`, found `str`
       |
       = help: change the annotation to `str`, or change the value to an int
       = note: `x` was declared as `int` here (file.newr:12:9)

Spans are **byte offsets** (SPEC.md §16, research §4.3): a Span is
``Span(file, start_byte, end_byte)``. Line/column are *derived* at render
time from the source text via ``line_col`` - no node ever stores line/col.

A Diagnostic carries a span, a stable machine code (``E1xxx`` syntax,
``E2xxx`` types, ``E3xxx`` resolver, ``W1xxx`` warnings), a message, and
- where they exist - ``expected`` / ``found`` (in user terms, never
internal type-variable names), ``help`` (one concrete edit), ``note``,
and a ``suggestion`` (a span + replacement + applicability
``"certain"`` | ``"maybe"``; only ``"certain"`` suggestions may ever be
auto-applied by a future ``--fix`` / LSP code action). Similar-name
suggestions use the rustc *named* form ("there is a variable with a
similar name: `count`"), never the folk "did you mean" wording. The
similarity gate is documented and tested, not claimed as "standard":
difflib ratio >= 0.7 (SPEC.md §16; the v0 value was 0.6).

``Fix`` is the separate ``newr fix`` payload (SPEC.md §5.2): a span and a
replacement, attached only when the fix is unambiguous (exactly one
candidate) and safe (a pure rename or a pure deletion of a provably-dead
token - never a change to program meaning).

This module has no dependencies on any other compiler module.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Maximum nesting depth of expressions / blocks / types (M-01, SPEC.md
# SS16): the parser, resolver, checker, and discord validator all stop
# with E1001 ("too deeply nested") past this depth instead of crashing
# with RecursionError. 200 is far above anything a human writes, and
# far below the interpreter's recursion limit.
MAX_NESTING_DEPTH = 200


def line_col(source: str, byte_offset: int) -> tuple[int, int]:
    """(line, col) - both 1-based, col in characters - of a byte offset.

    ``byte_offset`` must be at a UTF-8 character boundary (spans always
    are: the lexer only emits boundaries). Decoding only the prefix keeps
    this correct for non-ASCII source.
    """
    prefix = source.encode("utf-8")[:byte_offset].decode("utf-8")
    line = prefix.count("\n") + 1
    col = len(prefix.rsplit("\n", 1)[-1]) + 1
    return line, col


@dataclass(frozen=True)
class Span:
    """A source span: file + start/end byte offsets (end exclusive)."""

    file: str
    start_byte: int
    end_byte: int

    def __post_init__(self) -> None:
        if self.end_byte < self.start_byte:
            raise ValueError("span end precedes start")

    def __str__(self) -> str:
        return f"{self.file}:{self.start_byte}..{self.end_byte}"

    def line_col(self, source: str) -> tuple[int, int, int, int]:
        """(line, col, end_line, end_col) derived from the source text."""
        line, col = line_col(source, self.start_byte)
        end_line, end_col = line_col(source, self.end_byte)
        return line, col, end_line, end_col


@dataclass(frozen=True)
class Fix:
    """A safe, unambiguous fix for a diagnostic (SPEC.md §5.2).

    ``span`` is the text to replace; ``replacement`` is what replaces it.
    A fix is only attached when it is unambiguous (exactly one candidate)
    and safe (a pure rename or a pure deletion of a provably-dead token -
    never a change to program meaning).
    """

    span: Span
    replacement: str


@dataclass(frozen=True)
class Suggestion:
    """A machine-applicable suggestion (SPEC.md §16).

    Only ``applicability == "certain"`` suggestions may ever be
    auto-applied by a future ``--fix`` / LSP code action; ``"maybe"``
    suggestions are shown to the user and never applied automatically.
    """

    span: Span
    replacement: str
    applicability: str = "certain"  # "certain" | "maybe"

    def __post_init__(self) -> None:
        if self.applicability not in ("certain", "maybe"):
            raise ValueError(
                f"bad suggestion applicability: {self.applicability!r}")


@dataclass
class Diagnostic:
    """One diagnostic: an error, a warning, a note, or a help."""

    span: Span
    code: str
    message: str
    severity: str = "error"  # "error" | "warning" | "note" | "help"
    expected: str | None = None
    found: str | None = None
    help: str | None = None
    note: str | None = None
    suggestion: Suggestion | None = None
    fix: Fix | None = None

    def is_error(self) -> bool:
        return self.severity == "error"

    def format(self, source_text: str | None = None) -> str:
        """Render the diagnostic rustc-style (SPEC.md §16).

        With source text, renders the ``--> file:line:col`` header, the
        source line, and the caret span; without it, renders the header
        line only (the byte offsets are in the span).
        """
        lines = [f"{self.severity}[{self.code}]: {self.message}"]
        if source_text is not None:
            line, col, end_line, end_col = self.span.line_col(source_text)
            lines.append(f"  --> {self.span.file}:{line}:{col}")
            src_lines = source_text.split("\n")
            gutter = " " * len(str(line))
            lines.append(f"{gutter} |")
            if 0 <= line - 1 < len(src_lines):
                src = src_lines[line - 1].rstrip("\n")
                lines.append(f"{line} | {src}")
                if end_line == line:
                    width = max(1, end_col - col)
                else:
                    # Multi-line span: caret to the end of the first line.
                    width = max(1, len(src) - (col - 1))
                carets = " " * (col - 1) + "^" * width
                if self.expected is not None and self.found is not None:
                    carets += (f" expected `{self.expected}`, "
                               f"found `{self.found}`")
                lines.append(f"{gutter} | {carets}")
            lines.append(f"{gutter} |")
        if self.help:
            lines.append(f"  = help: {self.help}")
        if self.note:
            lines.append(f"  = note: {self.note}")
        return "\n".join(lines)

    def to_json(self, source_text: str | None = None) -> dict:
        """The JSON form (``--error-format=json``).

        Line/col are derived from the byte offsets when source text is
        given; without source text only the byte offsets are present.
        """
        out: dict = {
            "file": self.span.file,
            "start_byte": self.span.start_byte,
            "end_byte": self.span.end_byte,
            "code": self.code,
            "message": self.message,
            "severity": self.severity,
        }
        if source_text is not None:
            line, col, end_line, end_col = self.span.line_col(source_text)
            out.update({"line": line, "col": col,
                        "end_line": end_line, "end_col": end_col})
        if self.expected is not None:
            out["expected"] = self.expected
        if self.found is not None:
            out["found"] = self.found
        if self.help:
            out["help"] = self.help
        if self.note:
            out["note"] = self.note
        if self.suggestion is not None:
            sugg: dict = {
                "file": self.suggestion.span.file,
                "start_byte": self.suggestion.span.start_byte,
                "end_byte": self.suggestion.span.end_byte,
                "replacement": self.suggestion.replacement,
                "applicability": self.suggestion.applicability,
            }
            if source_text is not None:
                sl, sc, el, ec = self.suggestion.span.line_col(source_text)
                sugg.update({"line": sl, "col": sc,
                             "end_line": el, "end_col": ec})
            out["suggestion"] = sugg
        if self.fix is not None:
            fix: dict = {
                "file": self.fix.span.file,
                "start_byte": self.fix.span.start_byte,
                "end_byte": self.fix.span.end_byte,
                "replacement": self.fix.replacement,
            }
            if source_text is not None:
                fl, fc, fel, fec = self.fix.span.line_col(source_text)
                fix.update({"line": fl, "col": fc,
                            "end_line": fel, "end_col": fec})
            out["fix"] = fix
        return out


class NewrError(Exception):
    """A compile error carrying one diagnostic (the first problem found).

    Recovery (SPEC.md §16) is a checker/resolver rule only: at most one
    error per function body, so one bad body never drowns the rest of
    the file in follow-on errors. The PARSER has no recovery - it stops
    at the first syntax error. Warnings are reported separately (see
    CompilerResult).
    """

    def __init__(self, diagnostic: Diagnostic):
        super().__init__(diagnostic.message)
        self.diagnostic = diagnostic

    @property
    def code(self) -> str:
        return self.diagnostic.code

    @property
    def span(self) -> Span:
        return self.diagnostic.span


@dataclass
class CompilerResult:
    """The result of running the front end on one program."""

    program: object | None = None  # Program, when parsing succeeded
    diagnostics: list[Diagnostic] = field(default_factory=list)

    @property
    def errors(self) -> list[Diagnostic]:
        return [d for d in self.diagnostics if d.is_error()]

    @property
    def warnings(self) -> list[Diagnostic]:
        return [d for d in self.diagnostics if not d.is_error()]

    @property
    def ok(self) -> bool:
        return not self.errors
