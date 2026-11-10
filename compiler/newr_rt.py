"""The newr Python runtime (M2).

This module is the runtime half of every emitted Python program that
needs it: checked 64-bit integer arithmetic, truncating division,
half-away rounding, the newr conversion/rendering rules, `Result`,
and the `?.` / `??` helpers. It is stdlib-only and dependency-free.

It lives in the compiler package as `compiler/newr_rt.py` and `newr
build` copies it — byte-identical — next to an emitted `.py` file
when (and only when) that program uses a helper from it. Do not edit
a copied file by hand: edit this file in the newr repo and rebuild.

Semantics are pinned in BUILD_REPORT.md §14.1/§14.2 (P2, P6–P9,
D5–D9, D24). Every rule here is unit-tested in tests/test_newr_rt.py.
"""

from __future__ import annotations

import json
import math
import os
import sqlite3
import urllib.error
import urllib.request
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal, localcontext
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qsl, urlparse, urlsplit

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
    is never set — a `catch e:` body reading `e.message` would
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
    every multiply — `10 ** 10**9` raises instead of allocating
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
    not the value — `str(9223372036854774784.0)` is
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
    (truncates toward zero). The result is int64 range-checked —
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
    or int (0 -> false). P7: parsing, not truthiness — `bool("false")`
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
    Equality is structural (like the rest of newr — §7.7); results
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

    The test is `is not None`, never truthiness — `0`, `""` and `[]`
    are values, not absences. A whole `?.` chain collapses into one
    call with the full postfix path inside `f`, so the base
    expression is evaluated exactly once.
    """
    return None if v is None else f(v)


def coalesce(v, default_fn):
    """newr `??`: `v` when it is not `None`, else `default_fn()`.

    The default is a thunk so it is evaluated lazily — only when
    the value is actually absent.
    """
    return v if v is not None else default_fn()


def list_index(lst, i):
    """newr `list[i]`: SPEC §9.2 — out-of-range indexing is a
    runtime error. Valid indices are `0 <= i < len(lst)`; a negative
    index is out of range (Python would silently wrap it to the
    last element — the silent behavior §1.1 forbids). Raises the
    same `IndexError` Python raises for an out-of-range index, so
    `as_error` gives it code `"IndexError"` in a `catch` clause.
    """
    if i < 0 or i >= len(lst):
        raise IndexError("list index out of range")
    return lst[i]


def list_get(lst, i, default):
    """newr `list.get(i, default)`: P9 — a negative index is
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
    """newr `time.format(ts, fmt)`: UTC, never local time (P4) —
    the same program prints the same output on every machine. The
    format must be one of the spec's fixed set (§12.2)."""
    if fmt not in _TIME_FORMATS:
        raise NewrError(f"unsupported time format: {fmt!r}")
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime(fmt)


# ---------------------------------------------------------------- file I/O (D23)


def file_read(path: str) -> str:
    """newr `file.read(path)`: the whole file as UTF-8 text.

    A helper (not an inline `with` block) so a read inside a lambda
    body or an optional chain still happens at the right time — a
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


# ------------------------------------------------------------ db module
# SQLite via the stdlib `sqlite3`, synchronous (SPEC.md section 12.5).
# Handles are ints, fd-style: ids from 1, a registry maps each id to
# its connection. `isolation_level=None` is autocommit, so explicit
# BEGIN IMMEDIATE / COMMIT / ROLLBACK do the transaction work and a
# bare `db.exec` commits at once. `?` binding is mandatory: values
# are never interpolated into the SQL text.

_DB_CONNECTIONS: dict[int, sqlite3.Connection] = {}
_DB_NEXT_ID = 1


def db_open(path: str) -> int:
    """newr `db.open(path)`: open (create if needed) and return a handle."""
    global _DB_NEXT_ID
    conn = sqlite3.connect(path, isolation_level=None)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    handle = _DB_NEXT_ID
    _DB_NEXT_ID += 1
    _DB_CONNECTIONS[handle] = conn
    return handle


def _db_conn(handle: int) -> sqlite3.Connection:
    conn = _DB_CONNECTIONS.get(handle)
    if conn is None:
        raise NewrError(f"db: unknown or closed handle {handle}")
    return conn


def _db_value(value, type_str: str):
    """Convert one raw SQLite value to the newr value for a `db.query`
    field type (`int`, `float`, `str`, `bool`, each optionally `?`).

    A NULL lands only in a nullable field. An int column stored as
    TEXT parses strictly (`int("42")` works, `int("abc")` throws); a
    REAL 3.0 converts to int 3 but 3.5 throws; a bool field accepts
    0/1 (or "0"/"1") and nothing else. Anything that cannot convert
    raises NewrError instead of silently producing a wrong value.
    """
    nullable = type_str.endswith("?")
    base = type_str[:-1] if nullable else type_str
    if value is None:
        if nullable:
            return None
        raise NewrError(f"db: NULL in non-nullable {base} field")
    if base == "int":
        if isinstance(value, bool):
            raise NewrError("db: cannot convert bool to int")
        if isinstance(value, int):
            return check_int(value)
        if isinstance(value, float):
            if not value.is_integer():
                raise NewrError(f"db: cannot convert {value} to int")
            return check_int(int(value))
        if isinstance(value, str):
            try:
                return check_int(int(value.strip()))
            except ValueError:
                raise NewrError(f"db: cannot convert {value!r} to int")
        raise NewrError(f"db: cannot convert {type(value).__name__} to int")
    if base == "float":
        if isinstance(value, bool):
            raise NewrError("db: cannot convert bool to float")
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            try:
                return float(value.strip())
            except ValueError:
                raise NewrError(
                    f"db: cannot convert {value!r} to float")
        raise NewrError(f"db: cannot convert {type(value).__name__} to float")
    if base == "str":
        if isinstance(value, bool):
            return "true" if value else "false"
        if isinstance(value, str):
            return value
        return to_str(value)
    if base == "bool":
        if isinstance(value, bool):
            return value
        if isinstance(value, int):
            if value == 0:
                return False
            if value == 1:
                return True
            raise NewrError(f"db: cannot convert {value} to bool")
        if isinstance(value, str):
            if value == "0":
                return False
            if value == "1":
                return True
            raise NewrError(f"db: cannot convert {value!r} to bool")
        raise NewrError(f"db: cannot convert {type(value).__name__} to bool")
    raise NewrError(f"db: unknown field type {type_str}")


def db_exec(handle: int, sql: str, args: list) -> int:
    """newr `db.exec(handle, sql, args...)`: run one statement, return
    the rows affected (0 for DDL, whose rowcount is -1)."""
    cur = _db_conn(handle).execute(sql, args)
    return cur.rowcount if cur.rowcount >= 0 else 0


def db_query(handle: int, sql: str, args: list, field_names: list,
             field_types: list) -> list:
    """newr `db.query(handle, sql, args...)`: run a SELECT and return
    one list per row, converted values in field order. A missing
    column throws; extra columns are ignored."""
    cur = _db_conn(handle).execute(sql, args)
    cols = [d[0] for d in cur.description] if cur.description else []
    rows = []
    for raw in cur.fetchall():
        by_name = dict(zip(cols, raw))
        row = []
        for name, type_str in zip(field_names, field_types):
            if name not in by_name:
                raise NewrError(
                    f"db: column {name!r} missing from query result")
            row.append(_db_value(by_name[name], type_str))
        rows.append(row)
    return rows


def db_begin(handle: int) -> None:
    """newr `db.begin(handle)`: BEGIN IMMEDIATE."""
    _db_conn(handle).execute("BEGIN IMMEDIATE")


def db_commit(handle: int) -> None:
    """newr `db.commit(handle)`: COMMIT."""
    _db_conn(handle).execute("COMMIT")


def db_rollback(handle: int) -> None:
    """newr `db.rollback(handle)`: ROLLBACK."""
    _db_conn(handle).execute("ROLLBACK")


def db_last_id(handle: int) -> int:
    """newr `db.last_id(handle)`: the last INSERT rowid."""
    cur = _db_conn(handle).execute("SELECT last_insert_rowid()")
    row = cur.fetchone()
    return check_int(row[0])


def db_close(handle: int) -> None:
    """newr `db.close(handle)`: close and drop the handle. A second
    close (or any use after close) raises NewrError."""
    conn = _DB_CONNECTIONS.pop(handle, None)
    if conn is None:
        raise NewrError(f"db: unknown or closed handle {handle}")
    conn.close()


# ------------------------------------------------------------ json module
# Typed JSON (SPEC.md section 12.7). The emitter passes a recursive
# shape descriptor built from the static type: a scalar is "int" /
# "float" / "str" / "bool" / "none", a nullable is ["?", inner], a
# list is ["list", elem], a dict is ["dict", value], a struct is
# ["struct", <class>, [[field, shape], ...]] in declaration order,
# where <class> is the emitted struct class itself (the runtime
# cannot name a user struct, so the emitter hands it the class).
# Structs are built positionally, in field order, so a field whose
# newr name is a Python keyword still constructs correctly.

def _json_reject_constant(value: str):
    """Reject the non-JSON literals Python accepts by default
    (NaN, Infinity, -Infinity): RFC 8259 has no such numbers."""
    raise ValueError(f"invalid JSON constant {value!r}")


def _struct_field_value(value, field_name: str):
    """One struct field, by its newr name. A field whose name is a
    Python keyword is emitted with a trailing underscore, so fall
    back to that spelling when the plain name is missing."""
    try:
        return getattr(value, field_name)
    except AttributeError:
        return getattr(value, field_name + "_")


def _json_encode_value(value, shape):
    """The plain Python value (dicts, lists, scalars, None) for
    ``value`` under ``shape``, in struct declaration order."""
    if isinstance(shape, str):
        if shape == "int":
            if isinstance(value, bool) or not isinstance(value, int):
                raise NewrError(
                    f"json: expected int, got {type(value).__name__}")
            return check_int(value)
        if shape == "float":
            if isinstance(value, bool):
                raise NewrError("json: expected float, got bool")
            if isinstance(value, int):
                return float(value)
            if isinstance(value, float):
                return value
            raise NewrError(
                f"json: expected float, got {type(value).__name__}")
        if shape == "str":
            if not isinstance(value, str):
                raise NewrError(
                    f"json: expected str, got {type(value).__name__}")
            return value
        if shape == "bool":
            if not isinstance(value, bool):
                raise NewrError(
                    f"json: expected bool, got {type(value).__name__}")
            return value
        if shape == "none":
            if value is not None:
                raise NewrError("json: expected none")
            return None
        raise NewrError(f"json: unknown shape {shape!r}")
    kind = shape[0]
    if kind == "?":
        if value is None:
            return None
        return _json_encode_value(value, shape[1])
    if kind == "list":
        if not isinstance(value, list):
            raise NewrError(
                f"json: expected list, got {type(value).__name__}")
        return [_json_encode_value(v, shape[1]) for v in value]
    if kind == "dict":
        if not isinstance(value, dict):
            raise NewrError(
                f"json: expected dict, got {type(value).__name__}")
        out = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise NewrError("json: dict keys must be str")
            out[key] = _json_encode_value(item, shape[1])
        return out
    if kind == "struct":
        out = {}
        for field_name, field_shape in shape[2]:
            out[field_name] = _json_encode_value(
                _struct_field_value(value, field_name), field_shape)
        return out
    raise NewrError(f"json: unknown shape {shape!r}")


def _json_decode_value(value, shape):
    """The newr value for a parsed JSON value under ``shape``. A
    struct is built positionally from its fields, in declaration
    order; a missing field throws, an extra field is ignored."""
    if isinstance(shape, str):
        if shape == "int":
            if isinstance(value, bool) or not isinstance(value, int):
                raise NewrError(
                    f"json: expected int, got {type(value).__name__}")
            return check_int(value)
        if shape == "float":
            if isinstance(value, bool):
                raise NewrError("json: expected float, got bool")
            if isinstance(value, int):
                try:
                    return float(value)
                except OverflowError:
                    raise NewrError(
                        "json: integer too large for float")
            if isinstance(value, float):
                return value
            raise NewrError(
                f"json: expected float, got {type(value).__name__}")
        if shape == "str":
            if not isinstance(value, str):
                raise NewrError(
                    f"json: expected str, got {type(value).__name__}")
            return value
        if shape == "bool":
            if not isinstance(value, bool):
                raise NewrError(
                    f"json: expected bool, got {type(value).__name__}")
            return value
        if shape == "none":
            if value is not None:
                raise NewrError("json: expected null")
            return None
        raise NewrError(f"json: unknown shape {shape!r}")
    kind = shape[0]
    if kind == "?":
        if value is None:
            return None
        return _json_decode_value(value, shape[1])
    if kind == "list":
        if not isinstance(value, list):
            raise NewrError(
                f"json: expected list, got {type(value).__name__}")
        return [_json_decode_value(v, shape[1]) for v in value]
    if kind == "dict":
        if not isinstance(value, dict):
            raise NewrError(
                f"json: expected dict, got {type(value).__name__}")
        return {k: _json_decode_value(v, shape[1])
                for k, v in value.items()}
    if kind == "struct":
        if not isinstance(value, dict):
            raise NewrError(
                f"json: expected object, got {type(value).__name__}")
        cls = shape[1]
        args = []
        for field_name, field_shape in shape[2]:
            if field_name not in value:
                raise NewrError(
                    f"json: missing field {field_name!r}")
            args.append(_json_decode_value(value[field_name],
                                           field_shape))
        return cls(*args)
    raise NewrError(f"json: unknown shape {shape!r}")


def json_encode(value, shape) -> str:
    """newr `json.encode(value)`: JSON text, compact, in struct
    declaration order. Floats use the shortest round-trip decimal,
    with ".0" for integral floats (Python `json.dumps` already does
    this: `json.dumps(1.0)` is "1.0"); ints are exact decimal,
    including int64 and beyond (Python ints are arbitrary
    precision, and the shape check keeps them in int64 range).
    Non-ASCII text is written as UTF-8, not \\u escapes, matching
    the other targets. `allow_nan=False` keeps inf/nan out of the
    output: they are not valid JSON numbers, so encoding one
    throws instead of writing Infinity."""
    plain = _json_encode_value(value, shape)
    try:
        return json.dumps(plain, ensure_ascii=False,
                          separators=(",", ":"), allow_nan=False)
    except (ValueError, TypeError) as exc:
        raise NewrError(f"json: cannot encode value: {exc}") from exc


def json_decode(text: str, shape):
    """newr `json.decode(text)`: the typed value for ``text`` under
    ``shape``. An empty string throws, trailing data after the JSON
    value throws, duplicate keys keep the last value (the parser
    behaviour), a JSON fraction into an int field throws, an integer
    into a float field is fine, null only into a nullable shape,
    and an integer outside int64 throws."""
    if not isinstance(text, str):
        raise NewrError("json: decode takes a string")
    try:
        parsed = json.loads(text,
                            parse_constant=_json_reject_constant)
    except ValueError as exc:
        raise NewrError(f"json: invalid JSON: {exc}") from exc
    return _json_decode_value(parsed, shape)


def _build_from_shape(shape, values: dict):
    """Build a struct from a plain dict of field values, converting
    each field through its shape. Used for the fetch response and
    the server request, whose fields arrive as plain values, not
    as JSON text."""
    if not isinstance(shape, (list, tuple)) or len(shape) != 3 \
            or shape[0] != "struct":
        raise NewrError("http: internal: expected a struct shape")
    cls = shape[1]
    args = []
    for field_name, field_shape in shape[2]:
        if field_name not in values:
            raise NewrError(f"http: missing field {field_name!r}")
        args.append(_json_decode_value(values[field_name], field_shape))
    return cls(*args)


# ------------------------------------------------------------ http module
# Sync HTTP server plus fetch client (SPEC.md section 12.6), stdlib
# only: `http.server` (ThreadingHTTPServer) for serve, `urllib`
# for fetch. Routes live in a module-level registry, exact-match
# only; a request with no route, or a method mismatch, gets 404
# with an empty body (not 405).

_HTTP_METHODS = {
    "GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS",
}

#: The fetch allowlist the runtime can enforce without a manifest
#: (SPEC.md section 12.6): localhost only. The checker enforces the
#: manifest allowlist for literal URLs; the runtime enforces this
#: default list for computed URLs, and for every URL at call time.
#: A manifest that allows another host is honoured by the checker
#: for literal URLs, but the runtime has no manifest access, so a
#: fetch to such a host still throws here (see the Phase B report).
_DEFAULT_HTTP_ALLOWLIST = ("localhost", "127.0.0.1")

# (method, path, handler, request shape), in registration order.
_HTTP_ROUTES: list = []


def http_route(method: str, path: str, handler, req_shape) -> None:
    """newr `http.route(method, path, handler)`: register one
    exact-match route. A computed method that is not one of the
    uppercase methods, or a computed path without a leading slash,
    throws here (literal ones are E2012 from the checker)."""
    if not isinstance(method, str) or method not in _HTTP_METHODS:
        raise NewrError(f"http: invalid method {method!r}")
    if not isinstance(path, str) or not path.startswith("/"):
        raise NewrError(
            f"http: route path must start with '/', got {path!r}")
    if not callable(handler):
        raise NewrError("http: route handler must be callable")
    _HTTP_ROUTES.append((method, path, handler, req_shape))


class _NewrHttpHandler(BaseHTTPRequestHandler):
    """One request: read the whole body, build the request struct,
    call the handler, write the response. Handler exceptions and an
    out-of-range status surface as a 500 with an empty body, so one
    bad request never takes the server down."""

    def log_message(self, fmt, *args):
        pass

    def do_GET(self):
        self._dispatch()

    def do_POST(self):
        self._dispatch()

    def do_PUT(self):
        self._dispatch()

    def do_PATCH(self):
        self._dispatch()

    def do_DELETE(self):
        self._dispatch()

    def do_HEAD(self):
        self._dispatch()

    def do_OPTIONS(self):
        self._dispatch()

    def _send(self, status: int, headers: dict, body: bytes) -> None:
        try:
            self.send_response(status)
            for key, value in headers.items():
                if key.lower() == "content-length":
                    continue
                self.send_header(key, value)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if self.command != "HEAD" and body:
                self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _dispatch(self) -> None:
        try:
            parsed = urlsplit(self.path)
            path = parsed.path
            # parse_qsl decodes "+" as space and %XX as the byte
            # (UTF-8); dict() keeps the LAST value for a repeated key.
            query = dict(parse_qsl(parsed.query,
                                   keep_blank_values=True))
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = 0
            raw = self.rfile.read(length) if length > 0 else b""
            try:
                body = raw.decode("utf-8")
            except UnicodeDecodeError:
                self._send(500, {}, b"")
                return
            headers = {k: v for k, v in self.headers.items()}
            found = None
            for route in _HTTP_ROUTES:
                if route[0] == self.command and route[1] == path:
                    found = route
                    break
            if found is None:
                self._send(404, {}, b"")
                return
            _method, _path, handler, req_shape = found
            values = {"method": self.command, "path": path,
                      "headers": headers, "body": body, "query": query}
            req = _build_from_shape(req_shape, values)
            resp = handler(req)
            status = _struct_field_value(resp, "status")
            resp_headers = _struct_field_value(resp, "headers")
            resp_body = _struct_field_value(resp, "body")
            if isinstance(status, bool) or not isinstance(status, int) \
                    or not 100 <= status <= 599:
                raise NewrError(
                    f"http: response status must be 100 to 599, "
                    f"got {status!r}")
            if not isinstance(resp_headers, dict):
                raise NewrError("http: response headers must be a dict")
            if not isinstance(resp_body, str):
                raise NewrError("http: response body must be a str")
            self._send(status, resp_headers, resp_body.encode("utf-8"))
        except Exception:
            try:
                self._send(500, {}, b"")
            except Exception:
                pass


def http_serve(port: int, host: str = "127.0.0.1") -> None:
    """newr `http.serve(port)` / `http.serve(port, host)`: serve the
    registered routes forever, until the process is killed. Prints
    `listening on <host>:<port>` (the real port when 0) as its first
    line, then blocks. A KeyboardInterrupt exits 0."""
    if isinstance(port, bool) or not isinstance(port, int) \
            or not 0 <= port <= 65535:
        raise NewrError(
            f"http: port must be 0 to 65535, got {port!r}")
    if not isinstance(host, str):
        raise NewrError(f"http: host must be a str, got {host!r}")
    server = ThreadingHTTPServer((host, port), _NewrHttpHandler)
    real_port = server.server_address[1]
    print(f"listening on {host}:{real_port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def http_fetch(method: str, url: str, body, timeout: int = 10,
               headers=None, shape=None):
    """The fetch primitive behind `http.get` / `http.post` /
    `http.request`: one request, whole-request timeout in seconds
    (default 10, connect plus body). POST/request set
    `Content-Type: application/json` unless the program sets one;
    GET sets none. A 4xx/5xx status is a response, not a throw: the
    caller reads `resp.status`. Anything else that fails (an
    unreachable host, a timeout, a bad URL, a host outside the
    allowlist) throws."""
    if not isinstance(method, str) or method not in _HTTP_METHODS:
        raise NewrError(f"http: invalid method {method!r}")
    if isinstance(timeout, bool) or not isinstance(timeout, int) \
            or timeout <= 0:
        raise NewrError(
            f"http: timeout must be a positive number of seconds, "
            f"got {timeout!r}")
    if not isinstance(url, str):
        raise NewrError(f"http: URL must be a str, got {url!r}")
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise NewrError(
            f"http: fetch URL must start with http:// or https://, "
            f"got {url!r}")
    if not parsed.hostname:
        raise NewrError(f"http: fetch URL has no host: {url!r}")
    if parsed.username is not None:
        raise NewrError(
            "http: a URL must not carry userinfo; read the token "
            "from env and pass it as a header")
    host = parsed.hostname.lower()
    if host not in _DEFAULT_HTTP_ALLOWLIST:
        raise NewrError(
            f"http: fetch host {host!r} is not on the allowlist "
            f"(localhost, 127.0.0.1)")
    if shape is None:
        raise NewrError("http: internal: fetch without a response shape")
    req_headers: dict[str, str] = {}
    if headers is not None:
        if not isinstance(headers, dict):
            raise NewrError("http: headers must be a dict")
        for key, value in headers.items():
            if not isinstance(key, str) or not isinstance(value, str):
                raise NewrError("http: headers must be dict[str, str]")
            req_headers[key] = value
    data = None
    if body is not None:
        if not isinstance(body, str):
            raise NewrError("http: body must be a str")
        data = body.encode("utf-8")
        if not any(k.lower() == "content-type"
                   for k in req_headers):
            req_headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, method=method,
                                     headers=req_headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            status = resp.status
            resp_headers = dict(resp.headers.items())
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        status = exc.code
        resp_headers = dict(exc.headers.items()) if exc.headers else {}
        raw = exc.read()
    except Exception as exc:
        raise NewrError(
            f"http: fetch {method} {url} failed: {exc}") from exc
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise NewrError("http: response body is not valid UTF-8") \
            from exc
    values = {"status": status, "headers": resp_headers, "body": text}
    return _build_from_shape(shape, values)


def http_get(url: str, timeout: int = 10, headers=None,
             shape=None):
    """newr `http.get(url[, timeout[, headers]])`: GET, no body,
    no Content-Type unless the program sets one."""
    return http_fetch("GET", url, None, timeout, headers, shape)


def http_post(url: str, body: str, timeout: int = 10, headers=None,
              shape=None):
    """newr `http.post(url, body[, timeout[, headers]])`."""
    return http_fetch("POST", url, body, timeout, headers, shape)


def http_request(method: str, url: str, body: str, timeout: int = 10,
                 headers=None, shape=None):
    """newr `http.request(method, url, body[, timeout[, headers]])`:
    the generic fetch primitive."""
    return http_fetch(method, url, body, timeout, headers, shape)


# ------------------------------------------------------------ ui module
# One small widget set (SPEC.md section 12.8), stdlib-only:
# tkinter for the native window, pure Python for the render core.
# A Widget is an opaque value: a kind tag plus its fields. Handlers
# are plain Python callables (the lambdas the emitter produces).
# The render core is pure and deterministic: no tkinter, no I/O,
# byte-identical on every target (see SPEC.md SS12.8 for the format).
# `ui.show` builds one static window and blocks in mainloop;
# `ui.run` re-invokes the view after every handler call and rebuilds
# the window from the new tree, so state must live outside the view
# (a db handle or an http backend), never in a captured variable.

class Widget:
    """A ui widget value: `kind` plus the fields that kind uses.
    Only the constructors below build one; programs never build a
    Widget by hand."""

    def __init__(self, kind: str, text: str = "", label: str = "",
                 handler=None, placeholder: str = "", value: str = "",
                 url: str = "", children=()):
        self.kind = kind
        self.text = text
        self.label = label
        self.handler = handler
        self.placeholder = placeholder
        self.value = value
        self.url = url
        self.children = tuple(children)


def ui_text(text: str) -> Widget:
    """newr `ui.text(s)`."""
    if not isinstance(text, str):
        raise NewrError(f"ui: text takes a string, got {type(text)}")
    return Widget("text", text=text)


def ui_button(label: str, handler) -> Widget:
    """newr `ui.button(label, handler)`."""
    if not isinstance(label, str):
        raise NewrError(f"ui: button label takes a string, got {type(label)}")
    if not callable(handler):
        raise NewrError("ui: button handler must be callable")
    return Widget("button", label=label, handler=handler)


def ui_input(placeholder: str, value: str, handler) -> Widget:
    """newr `ui.input(placeholder, value, handler)`."""
    if not isinstance(placeholder, str) or not isinstance(value, str):
        raise NewrError("ui: input takes strings for placeholder and value")
    if not callable(handler):
        raise NewrError("ui: input handler must be callable")
    return Widget("input", placeholder=placeholder, value=value,
                  handler=handler)


def ui_list(children) -> Widget:
    """newr `ui.list(children)`."""
    return Widget("list", children=_ui_children(children, "list"))


def ui_image(url: str) -> Widget:
    """newr `ui.image(url)`."""
    if not isinstance(url, str):
        raise NewrError(f"ui: image url takes a string, got {type(url)}")
    return Widget("image", url=url)


def ui_row(children) -> Widget:
    """newr `ui.row(children)`."""
    return Widget("row", children=_ui_children(children, "row"))


def ui_column(children) -> Widget:
    """newr `ui.column(children)`."""
    return Widget("column", children=_ui_children(children, "column"))


def _ui_children(children, what: str) -> tuple:
    if not isinstance(children, (list, tuple)):
        raise NewrError(f"ui: {what} takes a list of widgets")
    for child in children:
        if not isinstance(child, Widget):
            raise NewrError(f"ui: {what} takes widgets, got {type(child)}")
    return tuple(children)


def _ui_escape(s: str) -> str:
    """The SPEC.md SS12.8 escape: & then < > ", in that order,
    in text and attributes alike."""
    return (s.replace("&", "&amp;").replace("<", "&lt;")
             .replace(">", "&gt;").replace('"', "&quot;"))


def ui_render(widget: Widget) -> str:
    """newr `ui.render(w)`: the widget tree as deterministic HTML
    (SPEC.md SS12.8). No outer wrapper, no insignificant
    whitespace, UTF-8. Button and input indices are separate
    pre-order counters."""
    if not isinstance(widget, Widget):
        raise NewrError("ui: render takes a widget")
    counters = {"button": 0, "input": 0}
    return _ui_render_widget(widget, counters)


def _ui_render_widget(w: Widget, counters: dict) -> str:
    kind = w.kind
    if kind == "text":
        return f"<p>{_ui_escape(w.text)}</p>"
    if kind == "button":
        n = counters["button"]
        counters["button"] += 1
        return (f"<button data-action=\"{n}\">"
                f"{_ui_escape(w.label)}</button>")
    if kind == "input":
        n = counters["input"]
        counters["input"] += 1
        return (f"<input placeholder=\"{_ui_escape(w.placeholder)}\" "
                f"value=\"{_ui_escape(w.value)}\" data-input=\"{n}\">")
    if kind == "list":
        inner = "".join(f"<li>{_ui_render_widget(c, counters)}</li>"
                        for c in w.children)
        return f"<ul>{inner}</ul>"
    if kind == "image":
        return f"<img src=\"{_ui_escape(w.url)}\" alt=\"\">"
    if kind == "row":
        inner = "".join(_ui_render_widget(c, counters) for c in w.children)
        return f"<div data-layout=\"row\">{inner}</div>"
    if kind == "column":
        inner = "".join(_ui_render_widget(c, counters) for c in w.children)
        return f"<div data-layout=\"column\">{inner}</div>"
    raise NewrError(f"ui: unknown widget kind {kind!r}")


def _ui_tk():
    """tkinter, imported lazily so a render-only program never
    touches the GUI stack."""
    try:
        import tkinter as tk
    except ImportError as exc:
        raise NewrError(
            "ui: tkinter is needed to show a window") from exc
    return tk


def _ui_build_tk(tk, parent, w: Widget, rerender) -> None:
    """Build the tkinter widgets for one tree under `parent`.
    `rerender` rebuilds the window after a handler runs (ui.run);
    it is None for ui.show, whose window is static."""
    kind = w.kind
    if kind == "text":
        tk.Label(parent, text=w.text).pack(anchor="w")
    elif kind == "button":
        def _on_click(handler=w.handler):
            handler()
            if rerender is not None:
                rerender()
        tk.Button(parent, text=w.label,
                  command=_on_click).pack(anchor="w")
    elif kind == "input":
        var = tk.StringVar(value=w.value)
        entry = tk.Entry(parent, textvariable=var)
        # No native placeholder in tkinter: the placeholder only
        # shows in the HTML render (ui.render). The entry starts
        # with the value, empty when the program passes "".
        entry.pack(anchor="w", fill="x")

        def _on_input(_event=None, handler=w.handler, var=var):
            handler(var.get())
            if rerender is not None:
                rerender()
        entry.bind("<Return>", _on_input)
        entry.bind("<FocusOut>", _on_input)
    elif kind == "list":
        frame = tk.Frame(parent)
        frame.pack(anchor="w", fill="x")
        for child in w.children:
            _ui_build_tk(tk, frame, child, rerender)
    elif kind == "image":
        # tkinter reads GIF/PPM natively only, and fetching the URL
        # would need the network: show the URL as a placeholder
        # label, so the widget is visible and honest about it.
        tk.Label(parent, text=f"[image: {w.url}]").pack(anchor="w")
    elif kind == "row":
        frame = tk.Frame(parent)
        frame.pack(anchor="w", fill="x")
        for child in w.children:
            cell = tk.Frame(frame)
            cell.pack(side="left", anchor="n")
            _ui_build_tk(tk, cell, child, rerender)
    elif kind == "column":
        frame = tk.Frame(parent)
        frame.pack(anchor="w", fill="x")
        for child in w.children:
            _ui_build_tk(tk, frame, child, rerender)
    else:
        raise NewrError(f"ui: unknown widget kind {kind!r}")


def ui_show(widget: Widget) -> None:
    """newr `ui.show(w)`: one static native window, blocks in the
    tkinter mainloop until the window is closed."""
    if not isinstance(widget, Widget):
        raise NewrError("ui: show takes a widget")
    tk = _ui_tk()
    root = tk.Tk()
    root.title("newr")
    _ui_build_tk(tk, root, widget, None)
    root.mainloop()


def ui_run(view) -> None:
    """newr `ui.run(view)`: the Elm-style loop. Build the window
    from `view()`, and after every handler call re-invoke the view
    and rebuild the window from the new tree. State lives outside
    the view (a db or http backend); the view re-reads it each
    time. Blocks in the tkinter mainloop until the window closes."""
    if not callable(view):
        raise NewrError("ui: run takes a view function")
    tk = _ui_tk()
    root = tk.Tk()
    root.title("newr")
    body = tk.Frame(root)
    body.pack(fill="both", expand=True)

    def rerender():
        for child in body.winfo_children():
            child.destroy()
        tree = view()
        if not isinstance(tree, Widget):
            raise NewrError("ui: a view must return a widget")
        _ui_build_tk(tk, body, tree, rerender)

    rerender()
    root.mainloop()
