"""Apply safe, unambiguous diagnostic fixes (SPEC.md section 5.2,
M6 Phase 2d).

``newr fix`` applies the fixes the diagnostics already carry (a span
plus a replacement, SPEC.md section 16). It only applies a fix when
the diagnostic's ``fix`` field says the fix is unambiguous (exactly
one candidate) and safe (a pure rename or a pure deletion of a
provably-dead token, never a change to program meaning). It prints
every change it makes, and it refuses to touch a file with any error
that has no safe fix: it fixes nothing in that file and reports the
blocking errors, never a silent half-fix.

Minimal scope in M6: the command is general (it applies any
diagnostic ``fix``), but today only the resolver attaches one, the
E3001 similar-name rename for an undefined name with exactly one
close match. The checker attaches no fixes yet; checker-attached
fixes are deferred to v1.1 with the reason in SPEC.md section 5.2.
When they land, they flow through this same code path with no change
here, because the command trusts the diagnostic's ``fix`` field and
never invents a fix of its own.

How a file is fixed (``fix_text``):

1. Run the full front end (parse + resolve + check + discord) on the
   source text and collect the diagnostics.
2. Consider only error diagnostics whose span is in this file.
   Diagnostics from imported files belong to those files; running
   ``newr fix`` on them fixes them there. Warnings never block: they
   carry no fix and never fail the build.
3. If any error in this file has no fix, raise ``FixRefused`` with
   every error diagnostic for the file, and change nothing.
4. Otherwise apply every fix as a byte-span replacement (spans are
   byte offsets, SPEC.md section 16), then re-run the front end on
   the fixed text. If the file still has errors, raise ``FixRefused``
   with them: the fixed text is never written half-clean. Only when
   the fixed text is clean does the caller write it back.
"""

from __future__ import annotations

from dataclasses import dataclass

from .checker import check_program
from .discord_check import check_discord, discord_providers
from .errors import Diagnostic
from .resolver import resolve_source


@dataclass(frozen=True)
class AppliedFix:
    """One fix that was applied: what changed, and where."""

    code: str  # diagnostic code, e.g. "E3001"
    line: int  # 1-based, from the diagnostic span
    col: int  # 1-based, in characters
    old: str  # the source text the fix replaced
    new: str  # the replacement text


class FixRefused(Exception):
    """The file cannot be fixed: some error has no safe fix.

    ``diagnostics`` is every error diagnostic for the file (fixable
    and unfixable alike), so the caller can show the full picture.
    ``reason`` names the refusal when it is not a plain unfixable
    error (overlapping fix spans, a span outside the source, or a
    fixed text that still has errors).
    """

    def __init__(self, diagnostics: list[Diagnostic],
                 reason: str | None = None) -> None:
        super().__init__(reason or "cannot fix: an error has no safe fix")
        self.diagnostics = list(diagnostics)
        self.reason = reason  # None for a plain unfixable error


def front_end_diagnostics(source: str, filename: str) -> list[Diagnostic]:
    """Parse + resolve + check + discord-check source text.

    Returns the diagnostics in deterministic order. A parse error
    raises NewrError carrying the single diagnostic, like the
    pipeline in compiler/cli.py.
    """
    resolved = resolve_source(source, filename)
    diagnostics: list[Diagnostic] = list(resolved.diagnostics)
    providers = discord_providers(resolved.program)
    diagnostics += check_program(resolved.program,
                                 providers=providers).diagnostics
    diagnostics += check_discord(resolved.program)
    return diagnostics


def apply_fixes(source: str, diagnostics: list[Diagnostic], *,
                filename: str) -> tuple[str, list[AppliedFix]]:
    """Apply every fixable diagnostic fix to ``source``.

    Only error diagnostics whose span is in ``filename`` are
    considered. If any such error has no ``fix``, raise ``FixRefused``
    with every error diagnostic for the file and change nothing.
    Overlapping fix spans, or a span outside the source, also refuse:
    two fixes never fight over the same bytes.

    Replacements are applied to the UTF-8 bytes, in descending span
    order, so earlier spans stay valid while later ones are replaced.
    """
    errors = [d for d in diagnostics
              if d.is_error() and d.span.file == filename]
    without_fix = [d for d in errors if d.fix is None]
    if without_fix:
        raise FixRefused(errors)
    fixes = sorted((d.fix for d in errors),
                   key=lambda f: (f.span.start_byte, f.span.end_byte))
    raw = source.encode("utf-8")
    for fix in fixes:
        if (fix.span.start_byte < 0
                or fix.span.end_byte > len(raw)
                or fix.span.end_byte < fix.span.start_byte):
            raise FixRefused(
                errors,
                reason=f"fix span {fix.span.start_byte}.."
                       f"{fix.span.end_byte} is outside the source")
    for prev, cur in zip(fixes, fixes[1:]):
        if cur.span.start_byte < prev.span.end_byte:
            raise FixRefused(
                errors,
                reason="two fixes overlap at "
                       f"{cur.span.start_byte}..{cur.span.end_byte}")
    applied: list[AppliedFix] = []
    for diag in sorted(errors, key=lambda d: d.fix.span.start_byte):
        fix = diag.fix
        old = raw[fix.span.start_byte:fix.span.end_byte].decode("utf-8")
        line, col, _el, _ec = fix.span.line_col(source)
        applied.append(AppliedFix(code=diag.code, line=line, col=col,
                                  old=old, new=fix.replacement))
    new_raw = raw
    for fix in sorted(fixes, key=lambda f: f.span.start_byte,
                      reverse=True):
        new_raw = (new_raw[:fix.span.start_byte]
                   + fix.replacement.encode("utf-8")
                   + new_raw[fix.span.end_byte:])
    return new_raw.decode("utf-8"), applied


def fix_text(source: str, filename: str) -> tuple[str, list[AppliedFix]]:
    """Fix ``source`` text and return (new source, applied fixes).

    Runs the front end, applies every fix, then re-runs the front
    end on the fixed text: only a fixed text with no errors left in
    this file is returned. Raises ``FixRefused`` (nothing applied)
    when an error has no safe fix, when fixes overlap, or when the
    fixed text still has errors. Raises NewrError on a parse error.
    """
    diagnostics = front_end_diagnostics(source, filename)
    new_source, applied = apply_fixes(source, diagnostics,
                                      filename=filename)
    if not applied:
        return source, []
    remaining = [d for d in front_end_diagnostics(new_source, filename)
                 if d.is_error() and d.span.file == filename]
    if remaining:
        raise FixRefused(
            remaining,
            reason="the fixed text still has errors")
    return new_source, applied
