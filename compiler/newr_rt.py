"""The newr Python runtime (M2).

This module is the runtime half of every emitted Python program that
needs it: checked 64-bit integer arithmetic, truncating division,
half-away rounding, the newr conversion/rendering rules, `Result`,
and the `?.` / `??` helpers. It is stdlib-only and dependency-free.

It lives in the compiler package as `compiler/newr_rt.py` and `newr
build` copies it - byte-identical - next to an emitted `.py` file
when (and only when) that program uses a helper from it. Do not edit
a copied file by hand: edit this file in the newr repo and rebuild.

Semantics are pinned in BUILD_REPORT.md §14.1/§14.2 (P2, P6–P9,
D5–D9, D24). Every rule here is unit-tested in tests/test_newr_rt.py.
"""

from __future__ import annotations

import math
import os
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal, localcontext

# ---------------------------------------------------------------- int64

INT64_MIN = -(2 ** 63)
INT64_MAX = 2 ** 63 - 1


def check_int(v: int) -> int:
    """Return ``v`` if it fits in signed 64-bit, else raise."""
    if INT64_MIN <= v <= INT64_MAX:
        return v
    raise NewrOverflowError(f"integer overflow: {v} is outside int64 range")


# ---------------------------------------------------------------- errors


class NewrError(Exception):
    """Base of every newr `error` value. Carries the error payload."""

    def __init__(self, message, *, code=None, payload=None):
        super().__init__(message)
        self.message = message
        self.code = code
        self.payload = payload


class NewrOverflowError(NewrError, OverflowError):
    """Checked-int overflow. Subclassing OverflowError means a newr
    `catch e:` and a bare `except OverflowError` both see it.

    The base order matters (BUG-M2-2): NewrError must come FIRST,
    or constructing one never runs NewrError.__init__ and `.message`
    is never set - a `catch e:` body reading `e.message` would
    raise AttributeError instead of printing the overflow."""


class NewrDivisionByZeroError(NewrError, ZeroDivisionError):
    """Division or modulo by zero. (Same base order as above.)"""


def as_error(e) -> NewrError:
    """Coerce whatever a `catch` caught into a newr `error` value.

    A `NewrError` passes through unchanged; a plain Python exception
    keeps its message and gains its type name as `.code`; a bare
    string becomes an error with that message. This is what makes
    `.message` / `.code` total inside every `catch` body (SPEC §10).
    """
    if isinstance(e, NewrError):
        return e
    if isinstance(e, str):
        return NewrError(e)
    if isinstance(e, BaseException):
        return NewrError(str(e), code=type(e).__name__)
    return NewrError(str(e))


# ---------------------------------------------------------------- checked int arithmetic


def add(a: int, b: int) -> int:
    return check_int(a + b)


def sub(a: int, b: int) -> int:
    return check_int(a - b)


def mul(a: int, b: int) -> int:
    return check_int(a * b)


def neg(a: int) -> int:
    return check_int(-a)


def pow_int(a: int, b: int) -> int:
    """newr `**` on ints: checked, bounded before computing.

    A negative exponent is an error (an int result cannot represent
    it). The exponent is bounded by repeated squaring with a check at
    every multiply - `10 ** 10**9` raises instead of allocating
    gigabytes by computing blindly.
    """
    if b < 0:
        raise NewrError("negative exponent for int pow")
    if a == 0:
        return 1 if b == 0 else 0
    if a == 1:
        return 1
    if a == -1:
        return -1 if b % 2 == 1 else 1
    result = 1
    base = a
    exp = b
    while exp > 0:
        if exp % 2 == 1:
            result = check_int(result * base)
        exp //= 2
        if exp > 0:
            base = check_int(base * base)
    return result


def tdiv(a: int, b: int) -> int:
    """newr `/` on ints: truncating division (toward zero).

    Raises NewrDivisionByZeroError on b == 0 and NewrOverflowError
    on INT64_MIN / -1. Never emit Python `//` on possibly-negative
    operands (it floors) and never `int(a / b)` (it loses precision
    above 2**53).
    """
    if b == 0:
        raise NewrDivisionByZeroError("integer division by zero")
    if a == INT64_MIN and b == -1:
        raise NewrOverflowError("integer overflow: INT64_MIN / -1")
    q = abs(a) // abs(b)
    return q if (a < 0) == (b < 0) else -q


def tmod(a: int, b: int) -> int:
    """newr `%` on ints: remainder with the dividend's sign."""
    if b == 0:
        raise NewrDivisionByZeroError("integer modulo by zero")
    if a == INT64_MIN and b == -1:
        return 0  # mathematically 0; no overflow (unlike division)
    return a - tdiv(a, b) * b


def pow_float(a: float, b: float) -> float:
    """newr `**` on floats: IEEE semantics.

    Python's float `**` differs in two edge cases, both fixed here:
    a negative base with a fractional exponent returns a complex
    (newr has no complex type, so the answer is NaN), and overflow
    raises OverflowError (IEEE float overflow is +/-inf, with the
    sign from a negative base and an odd integer exponent).
    """
    try:
        result = a ** b
    except OverflowError:
        negative = a < 0.0 and float(b).is_integer() and int(b) % 2 != 0
        return float("-inf") if negative else float("inf")
    if isinstance(result, complex):
        return float("nan")
    return result


def fmod(a: float, b: float) -> float:
    """newr `%` on floats: C `fmod` semantics (dividend's sign).

    Python's float `%` takes the divisor's sign, so it is not used.
    Division by zero raises like the int case.
    """
    if b == 0.0:
        raise NewrDivisionByZeroError("float modulo by zero")
    return math.fmod(a, b)


# ---------------------------------------------------------------- rounding (P2)


def round_half_away(x, ndigits=None):
    """newr `round()`: half-away-from-zero (schoolbook).

    Rounds the EXACT value of the float: `Decimal(x)` (the exact
    binary expansion), not `Decimal(str(x))`. The shortest repr is
    not the value - `str(9223372036854774784.0)` is
    `"9.223372036854775e+18"`, and rounding that would change an
    already-integral float. The consequence is the representation
    fact every target shares: `round(2.675, 2)` is 2.67, because the
    float 2.675 is stored slightly below 2.675. `round(x)` returns
    an `int` (int64 range-checked); `round(x, n)` returns a `float`.
    Non-finite input: `round(inf)` raises `OverflowError` (Python's
    behaviour); `round(inf, n)` returns `inf`, `round(nan, n)`
    returns `nan`.
    """
    if isinstance(x, bool):
        x = int(x)
    if isinstance(x, int):
        if ndigits is None:
            return check_int(x)
        with localcontext() as ctx:
            ctx.prec = 28
            q = Decimal(x).quantize(Decimal(1).scaleb(-ndigits),
                                    rounding=ROUND_HALF_UP)
        return float(q)
    if not math.isfinite(x):
        if ndigits is None:
            raise OverflowError("cannot round infinite or NaN value")
        return x
    d = Decimal(x)
    if ndigits is None:
        prec = max(28, d.adjusted() + 2)
        with localcontext() as ctx:
            ctx.prec = prec
            q = d.quantize(Decimal("1"), rounding=ROUND_HALF_UP)
        return check_int(int(q))
    prec = max(28, d.adjusted() + max(ndigits, 0) + 2)
    with localcontext() as ctx:
        ctx.prec = prec
        q = d.quantize(Decimal(1).scaleb(-ndigits),
                       rounding=ROUND_HALF_UP)
    return float(q)


# ---------------------------------------------------------------- conversions


def to_int(x) -> int:
    """newr `int(x)`: from str (parses; throws on invalid) or float
    (truncates toward zero). The result is int64 range-checked -
    `int("99999999999999999999")` succeeds in Python and must throw
    here. `int("3.5")` raises ValueError; `int("abc")` raises
    ValueError (the natural Python mapping, research §3.3)."""
    if isinstance(x, bool):
        return 1 if x else 0
    if isinstance(x, int):
        return check_int(x)
    if isinstance(x, float):
        if not math.isfinite(x):
            raise OverflowError("cannot convert infinite or NaN to int")
        return check_int(int(x))
    if isinstance(x, str):
        return check_int(int(x, 10))
    raise NewrError(f"cannot convert {type(x).__name__} to int")


def to_float(x) -> float:
    """newr `float(x)`: from str or int. `float("inf")` / `float("nan")`
    are accepted (Python behaviour); a huge int raises OverflowError."""
    if isinstance(x, bool):
        return 1.0 if x else 0.0
    if isinstance(x, (int, float)):
        return float(x)
    if isinstance(x, str):
        return float(x)
    raise NewrError(f"cannot convert {type(x).__name__} to float")


def to_bool(x) -> bool:
    """newr `bool(x)`: from str (`"true"`/`"false"`, exact lowercase)
    or int (0 -> false). P7: parsing, not truthiness - `bool("false")`
    is False, and any other string raises ValueError."""
    if isinstance(x, bool):
        return x
    if isinstance(x, str):
        if x == "true":
            return True
        if x == "false":
            return False
        raise ValueError(
            f'invalid bool: {x!r} (expected "true" or "false")')
    if isinstance(x, int):
        return x != 0
    if isinstance(x, float):
        return x != 0.0
    raise NewrError(f"cannot convert {type(x).__name__} to bool")


def to_str(x) -> str:
    """newr `str(x)`: the display rendering (P8).

    `true`/`false`/`none` are lowercase; floats use the shortest
    round-trip form; collections render with elements rendered
    recursively and strings bare (`[1, true, a]`); an `error` renders
    as its message; a struct/enum value renders via its `_newr_repr`
    method (emitted by the compiler for every struct/enum type).
    Anything else falls back to Python `str()`.
    """
    if x is None:
        return "none"
    if isinstance(x, bool):
        return "true" if x else "false"
    if isinstance(x, str):
        return x
    if isinstance(x, (int, float)):
        return str(x)
    if isinstance(x, NewrError):
        return x.message
    if isinstance(x, Result):
        if x.is_ok():
            return f"ok({to_str(x._value)})"
        return f"err({to_str(x._error)})"
    if isinstance(x, list):
        return "[" + ", ".join(to_str(e) for e in x) + "]"
    if isinstance(x, dict):
        return "{" + ", ".join(
            f"{to_str(k)}: {to_str(v)}" for k, v in x.items()) + "}"
    if isinstance(x, (set, frozenset)):
        return "{" + ", ".join(to_str(e) for e in x) + "}"
    if hasattr(x, "_newr_repr"):
        return x._newr_repr()
    return str(x)


# ---------------------------------------------------------------- Result


class Result:
    """newr `Result[T, E]` (SPEC §10): the value form of errors.

    Built with `ok(v)` / `err(e)`; `is_ok()` / `is_err()` /
    `unwrap()` (throws the error on `err`) / `unwrap_or(default)`.
    Equality is structural (like the rest of newr - §7.7); results
    are not hashable, like a Python dataclass with `eq=True`.
    """

    __slots__ = ("_is_ok", "_value", "_error")

    def __init__(self, is_ok, value=None, error=None):
        self._is_ok = is_ok
        self._value = value
        self._error = error

    def is_ok(self) -> bool:
        return self._is_ok

    def is_err(self) -> bool:
        return not self._is_ok

    def unwrap(self):
        if self._is_ok:
            return self._value
        raise self._error

    def unwrap_or(self, default):
        return self._value if self._is_ok else default

    def __eq__(self, other):
        if not isinstance(other, Result):
            return NotImplemented
        if self._is_ok != other._is_ok:
            return False
        if self._is_ok:
            return self._value == other._value
        return self._error == other._error

    def __repr__(self):
        if self._is_ok:
            return f"ok({self._value!r})"
        return f"err({self._error!r})"


def ok(value) -> Result:
    return Result(True, value=value)


def err(error) -> Result:
    return Result(False, error=error)


def try_result(f) -> Result:
    """newr `try <expr>`: Ok-wrap the value, catch a thrown error
    into `err(...)`. The caught error is coerced with `as_error`
    so `.message` / `.code` are always available."""
    try:
        return ok(f())
    except Exception as exc:
        return err(as_error(exc))


# ---------------------------------------------------------------- ?. / ?? / accessors


def opt_chain(v, f):
    """newr `?.`: `None` when `v` is `None`, else `f(v)`.

    The test is `is not None`, never truthiness - `0`, `""` and `[]`
    are values, not absences. A whole `?.` chain collapses into one
    call with the full postfix path inside `f`, so the base
    expression is evaluated exactly once.
    """
    return None if v is None else f(v)


def coalesce(v, default_fn):
    """newr `??`: `v` when it is not `None`, else `default_fn()`.

    The default is a thunk so it is evaluated lazily - only when
    the value is actually absent.
    """
    return v if v is not None else default_fn()


def list_index(lst, i):
    """newr `list[i]`: SPEC §9.2 - out-of-range indexing is a
    runtime error. Valid indices are `0 <= i < len(lst)`; a negative
    index is out of range (Python would silently wrap it to the
    last element - the silent behavior §1.1 forbids). Raises the
    same `IndexError` Python raises for an out-of-range index, so
    `as_error` gives it code `"IndexError"` in a `catch` clause.
    """
    if i < 0 or i >= len(lst):
        raise IndexError("list index out of range")
    return lst[i]


def list_get(lst, i, default):
    """newr `list.get(i, default)`: P9 - a negative index is
    out-of-range (no Python wrap-around); anything outside
    `0 <= i < len(lst)` returns `default`."""
    return lst[i] if 0 <= i < len(lst) else default


def unwrap_opt(v):
    """newr `T?.unwrap()`: the value, or a thrown error on `none`."""
    if v is None:
        raise NewrError("unwrap of none")
    return v


# ---------------------------------------------------------------- time (P4)


_TIME_FORMATS = {"%Y-%m-%d", "%H:%M", "%Y-%m-%d %H:%M", "%d/%m/%Y",
                 "%H:%M:%S"}


def time_format(ts: int, fmt: str) -> str:
    """newr `time.format(ts, fmt)`: UTC, never local time (P4) -
    the same program prints the same output on every machine. The
    format must be one of the spec's fixed set (§12.2)."""
    if fmt not in _TIME_FORMATS:
        raise NewrError(f"unsupported time format: {fmt!r}")
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime(fmt)


# ---------------------------------------------------------------- file I/O (D23)


def file_read(path: str) -> str:
    """newr `file.read(path)`: the whole file as UTF-8 text.

    A helper (not an inline `with` block) so a read inside a lambda
    body or an optional chain still happens at the right time - a
    hoisted `with` would run at the enclosing statement, before the
    chain is even known to be non-none. `FileNotFoundError` etc.
    propagate as Python exceptions (wrapped by `as_error` in a
    `catch` clause, D23).
    """
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def file_write(path: str, content: str) -> None:
    """newr `file.write(path, content)`: overwrite (create if needed)."""
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content)


def file_append(path: str, content: str) -> None:
    """newr `file.append(path, content)`: append (create if needed)."""
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(content)


def file_exists(path: str) -> bool:
    """newr `file.exists(path)`."""
    return os.path.exists(path)
