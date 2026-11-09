// The newr JavaScript runtime (M3).
//
// This module is the runtime half of every emitted JavaScript program
// that needs it: checked 64-bit integer arithmetic (BigInt), truncating
// division, half-away rounding on the EXACT float value, the newr
// conversion/rendering rules, `Result`, and the `?.` / `??` helpers.
// It is stdlib-only (node:fs, node:crypto) and dependency-free.
//
// It lives in the compiler package as `compiler/newr_rt.js` and `newr
// build --target js` copies it — byte-identical — next to an emitted
// `.js` file when (and only when) that program uses a helper from it.
// Do not edit a copied file by hand: edit this file in the newr repo
// and rebuild.
//
// Semantics are pinned in BUILD_REPORT.md §17 (J2–J6). Every rule here
// is unit-tested in tests/js/rt_tests.js (run by
// tests/test_newr_rt_js.py).
//
// Module system (J1): CommonJS — `require("./newr_rt.js")` — so an
// emitted file runs with plain `node file.js` and no package.json.
// The runtime is required with a single destructured require:
// `const { add, ... } = require("./newr_rt.js")`.

"use strict";

const fs = require("node:fs");
const { randomBytes } = require("node:crypto");

// ---------------------------------------------------------------- int64

const INT64_MIN = -(2n ** 63n);
const INT64_MAX = 2n ** 63n - 1n;

function checkInt(v) {
  // Return `v` if it fits in signed 64-bit, else raise.
  if (typeof v !== "bigint") {
    throw new TypeError(`checkInt got ${typeof v}, expected bigint`);
  }
  if (INT64_MIN <= v && v <= INT64_MAX) {
    return v;
  }
  throw new NewrOverflowError(
    `integer overflow: ${v} is outside int64 range`);
}

// ---------------------------------------------------------------- errors

class NewrError extends Error {
  // Base of every newr `error` value. Carries the error payload.
  // `code` is null unless set (a caught host error keeps its own
  // code via asError).
  constructor(message, code = null, payload = undefined) {
    super(message);
    this.name = "NewrError";
    this.message = message;
    this.code = code;
    this.payload = payload;
  }
}

class NewrOverflowError extends NewrError {
  // Checked-int overflow. The Python target's NewrOverflowError is
  // a NewrError subclass constructed WITHOUT a code, so a newr
  // `catch e:` sees e.code == none (the class is told by `name`,
  // not by `code`). The builtin OverflowError (round(inf),
  // int(inf), float(huge int)) is a different site: it throws
  // codedError("OverflowError", ...) instead.
  constructor(message) {
    super(message);
    this.name = "NewrOverflowError";
  }
}

class NewrDivisionByZeroError extends NewrError {
  // Division or modulo by zero. Same code rule as above: the
  // Python twin is constructed without a code, so e.code is none
  // in a `catch`. The builtin sites — `0.0 ** -1`, where Python
  // raises ZeroDivisionError itself, and float `/` by zero, where
  // Python's native `/` raises it — throw
  // codedError("ZeroDivisionError", ...) in powFloat and fdiv
  // instead (§18 fix 2: fdiv was missed by the §17.4 item-4 list).
  constructor(message) {
    super(message);
    this.name = "NewrDivisionByZeroError";
  }
}

// An error with a host-style code (IndexError, KeyError, ValueError):
// the Python target raises the builtin of that name, so a newr
// `catch e:` sees the same code on both targets.
function codedError(code, message) {
  const e = new Error(message);
  e.name = code;
  e.code = code;
  return e;
}

function asError(e) {
  // Coerce whatever a `catch` caught into a newr `error` value.
  // A `NewrError` passes through unchanged; a plain Error keeps its
  // message and gains its code (a Node errno such as "ENOENT", or
  // its constructor name); a string becomes an error with that
  // message. This is what makes `.message` / `.code` total inside
  // every `catch` body (SPEC §10).
  if (e instanceof NewrError) {
    return e;
  }
  if (typeof e === "string") {
    return new NewrError(e);
  }
  if (e instanceof Error) {
    const code = typeof e.code === "string" && e.code
      ? e.code : e.name || "Error";
    return new NewrError(e.message, code);
  }
  return new NewrError(String(e));
}

// ---------------------------------------------------------------- checked int arithmetic

function add(a, b) {
  return checkInt(a + b);
}

function sub(a, b) {
  return checkInt(a - b);
}

function mul(a, b) {
  return checkInt(a * b);
}

function neg(a) {
  return checkInt(-a);
}

function powInt(a, b) {
  // newr `**` on ints: checked, bounded before computing.
  // A negative exponent is an error (an int result cannot represent
  // it). The exponent is bounded by repeated squaring with a check
  // at every multiply — `10 ** 10**9` raises instead of allocating
  // by computing blindly.
  if (b < 0n) {
    throw new NewrError("negative exponent for int pow");
  }
  if (a === 0n) {
    return b === 0n ? 1n : 0n;
  }
  if (a === 1n) {
    return 1n;
  }
  if (a === -1n) {
    return b % 2n === 1n ? -1n : 1n;
  }
  let result = 1n;
  let base = a;
  let exp = b;
  while (exp > 0n) {
    if (exp % 2n === 1n) {
      result = checkInt(result * base);
    }
    exp /= 2n;
    if (exp > 0n) {
      base = checkInt(base * base);
    }
  }
  return result;
}

function tdiv(a, b) {
  // newr `/` on ints: truncating division (toward zero). BigInt `/`
  // already truncates — the check is that the operands ARE BigInts
  // (the checker guarantees it) and the two error cases.
  if (b === 0n) {
    throw new NewrDivisionByZeroError("integer division by zero");
  }
  if (a === INT64_MIN && b === -1n) {
    throw new NewrOverflowError("integer overflow: INT64_MIN / -1");
  }
  return a / b;
}

function tmod(a, b) {
  // newr `%` on ints: remainder with the dividend's sign. BigInt `%`
  // already takes the dividend's sign.
  if (b === 0n) {
    throw new NewrDivisionByZeroError("integer modulo by zero");
  }
  if (a === INT64_MIN && b === -1n) {
    return 0n; // mathematically 0; no overflow (unlike division)
  }
  return a % b;
}

function powFloat(a, b) {
  // newr `**` on floats: IEEE semantics. JS `**` already returns
  // NaN for a negative base with a fractional exponent (Python
  // returns a complex there — newr has no complex type, so the
  // answer is NaN) and Infinity on overflow. The one case JS gets
  // wrong is a zero base with a negative exponent: JS gives
  // Infinity, Python raises ZeroDivisionError.
  if (a === 0 && b < 0) {
    // Python raises the builtin ZeroDivisionError here (not the
    // NewrError subclass), so a `catch e:` sees code
    // "ZeroDivisionError" — codedError, not the class.
    throw codedError("ZeroDivisionError",
      "0.0 cannot be raised to a negative power");
  }
  return a ** b;
}

function fmod(a, b) {
  // newr `%` on floats: C `fmod` semantics (dividend's sign).
  // JS `%` already takes the dividend's sign (unlike Python's `%`,
  // which takes the divisor's). Division by zero raises like the
  // int case (JS would give NaN).
  if (b === 0) {
    throw new NewrDivisionByZeroError("float modulo by zero");
  }
  return a % b;
}

function fdiv(a, b) {
  // newr `/` on floats: IEEE division, except division by zero
  // raises like Python (JS would give Infinity for a non-zero
  // dividend and NaN for 0/0). `b === 0` covers -0 as well.
  // (P6: float /0 is an error — the Python target gets this free
  // from Python's own `/`; JS needs the check.) Python's native
  // `/` raises the BUILTIN ZeroDivisionError, so this throws the
  // coded error (§18 fix 2), not the codeless class — a `catch e:`
  // sees e.code == "ZeroDivisionError" on both targets. (Float `%`
  // is different: both targets route it through fmod, which raises
  // the codeless NewrDivisionByZeroError — that parity is correct.)
  if (b === 0) {
    throw codedError("ZeroDivisionError", "float division by zero");
  }
  return a / b;
}

// ---------------------------------------------------------------- rounding (P2 / J3)

// Decompose a finite JS number into its EXACT decimal value as
// (digits, scale): value = sign * digits / 10**scale, digits a
// non-negative BigInt, scale a non-negative integer. The float's
// binary expansion is exact: value = m * 2**e, and for e < 0 the
// exact decimal digits are m * 5**(-e) with -e places after the
// point.
function _exactDecimal(x) {
  const buf = new ArrayBuffer(8);
  const view = new DataView(buf);
  view.setFloat64(0, x);
  const bits = view.getBigUint64(0);
  const sign = bits >> 63n === 1n ? -1n : 1n;
  const expBits = Number((bits >> 52n) & 0x7ffn);
  const frac = bits & ((1n << 52n) - 1n);
  let m;
  let e;
  if (expBits === 0) {
    // Subnormal (or zero): no implicit leading 1.
    m = frac;
    e = -1074;
  } else {
    m = frac | (1n << 52n);
    e = expBits - 1075;
  }
  if (m === 0n) {
    return { sign, digits: 0n, scale: 0 };
  }
  if (e >= 0) {
    return { sign, digits: m * 2n ** BigInt(e), scale: 0 };
  }
  return { sign, digits: m * 5n ** BigInt(-e), scale: -e };
}

// Round the exact decimal (digits / 10**scale, digits >= 0) to `n`
// places after the point, half away from zero. Returns the rounded
// value as a decimal string (no exponent) for the float form, or as
// a BigInt for the integer form.
function _roundDecimal(digits, scale, n) {
  // We need round(digits / 10**scale * 10**n) = round(digits /
  // 10**(scale - n)); then divide by 10**n.
  if (scale > n) {
    const div = 10n ** BigInt(scale - n);
    let q = digits / div;
    const r = digits % div;
    if (r * 2n >= div) {
      q += 1n;
    }
    return { rounded: q, places: n };
  }
  // Already exact at n places: digits / 10**scale, with scale <= n.
  return { rounded: digits, places: scale };
}

function _decimalString(digits, places) {
  // `digits` is a non-negative BigInt holding the value * 10**places.
  // `places` may be negative (round(x, -1) rounds to tens): then
  // the value is digits * 10**(-places).
  const s = digits.toString();
  if (places === 0) {
    return s;
  }
  if (places < 0) {
    return s + "0".repeat(-places);
  }
  if (s.length > places) {
    return s.slice(0, s.length - places) + "." + s.slice(s.length - places);
  }
  return "0." + "0".repeat(places - s.length) + s;
}

function roundHalfAway(x, ndigits = undefined) {
  // newr `round()`: half-away-from-zero (schoolbook), on the EXACT
  // value of the float — not its shortest repr. `round(2.675, 2)`
  // is 2.67, because the float 2.675 is stored slightly below
  // 2.675. `round(x)` returns an `int` (int64 range-checked);
  // `round(x, n)` returns a `float`. Non-finite input:
  // `round(inf)` raises (Python's behaviour); `round(inf, n)`
  // returns `inf`, `round(nan, n)` returns `nan`.
  if (typeof x === "boolean") {
    x = x ? 1n : 0n;
  }
  if (typeof x === "bigint") {
    if (ndigits === undefined) {
      return checkInt(x);
    }
    return Number(x);
  }
  if (!Number.isFinite(x)) {
    if (ndigits === undefined) {
      // Python raises the builtin OverflowError here.
      throw codedError("OverflowError",
        "cannot round infinite or NaN value");
    }
    return x;
  }
  const n = ndigits === undefined ? 0 : Number(ndigits);
  const { sign, digits, scale } = _exactDecimal(x);
  const { rounded, places } = _roundDecimal(digits, scale, n);
  if (ndigits === undefined) {
    const v = sign * rounded;
    return checkInt(v);
  }
  if (rounded === 0n) {
    // Preserve a negative zero: round(-0.001, 2) is -0.0 in Python.
    return sign < 0n ? -0 : 0;
  }
  const text = _decimalString(rounded, places);
  const v = Number((sign < 0n ? "-" : "") + text);
  return v;
}

// ---------------------------------------------------------------- conversions

function toInt(x) {
  // newr `int(x)`: from str (parses; throws on invalid) or float
  // (truncates toward zero). The result is int64 range-checked.
  // `int("3.5")` raises ValueError; `int("abc")` raises ValueError.
  if (typeof x === "boolean") {
    return x ? 1n : 0n;
  }
  if (typeof x === "bigint") {
    return checkInt(x);
  }
  if (typeof x === "number") {
    if (!Number.isFinite(x)) {
      // Python raises the builtin OverflowError here.
      throw codedError("OverflowError",
        "cannot convert infinite or NaN to int");
    }
    return checkInt(BigInt(Math.trunc(x)));
  }
  if (typeof x === "string") {
    const t = x.trim();
    if (/^[+-]?\d+$/.test(t)) {
      return checkInt(BigInt(t));
    }
    throw codedError("ValueError",
      `invalid literal for int(): ${JSON.stringify(x)}`);
  }
  throw new NewrError(`cannot convert ${typeof x} to int`);
}

const _MAX_FLOAT_BIGINT = (2n ** 1024n) - (2n ** 971n);

function toFloat(x) {
  // newr `float(x)`: from str or int. `float("inf")` / `float("nan")`
  // are accepted (Python behaviour); a huge int raises OverflowError.
  if (typeof x === "boolean") {
    return x ? 1.0 : 0.0;
  }
  if (typeof x === "bigint") {
    if (x > _MAX_FLOAT_BIGINT || x < -_MAX_FLOAT_BIGINT) {
      // Python raises the builtin OverflowError here (float(huge)).
      throw codedError("OverflowError",
        "int too large to convert to float");
    }
    return Number(x);
  }
  if (typeof x === "number") {
    return x;
  }
  if (typeof x === "string") {
    const t = x.trim().replace(/_/g, "");
    const low = t.toLowerCase();
    if (low === "inf" || low === "+inf" || low === "infinity"
      || low === "+infinity") {
      return Infinity;
    }
    if (low === "-inf" || low === "-infinity") {
      return -Infinity;
    }
    if (low === "nan" || low === "+nan" || low === "-nan") {
      return NaN;
    }
    if (/^[+-]?(\d+(\.\d*)?|\.\d+)([eE][+-]?\d+)?$/.test(t)) {
      return Number(t);
    }
    throw codedError("ValueError",
      `could not convert string to float: ${JSON.stringify(x)}`);
  }
  throw new NewrError(`cannot convert ${typeof x} to float`);
}

function toBool(x) {
  // newr `bool(x)`: from str (`"true"`/`"false"`, exact lowercase)
  // or int (0 -> false). P7: parsing, not truthiness.
  if (typeof x === "boolean") {
    return x;
  }
  if (typeof x === "string") {
    if (x === "true") {
      return true;
    }
    if (x === "false") {
      return false;
    }
    throw codedError("ValueError",
      `invalid bool: ${JSON.stringify(x)} (expected "true" or "false")`);
  }
  if (typeof x === "bigint") {
    return x !== 0n;
  }
  if (typeof x === "number") {
    return x !== 0;
  }
  throw new NewrError(`cannot convert ${typeof x} to bool`);
}

// Float rendering with PYTHON repr semantics (J4): shortest
// round-trip digits, scientific notation iff the decimal exponent
// is < -4 or >= 16, ".0" for integral floats. JS String() differs:
// String(1.0) is "1", String(1e16) is "10000000000000000",
// String(0.00001) is "0.00001" — Python gives "1.0", "1e+16",
// "1e-05".
function floatStr(x) {
  if (Number.isNaN(x)) {
    return "nan";
  }
  if (x === Infinity) {
    return "inf";
  }
  if (x === -Infinity) {
    return "-inf";
  }
  if (x === 0) {
    return Object.is(x, -0) ? "-0.0" : "0.0";
  }
  const neg = x < 0;
  // Shortest digits + exponent: toExponential() gives "d.ddde±XX"
  // (of the absolute value — the sign is handled separately).
  const exp = Math.abs(x).toExponential();
  const m = /^(\d)(?:\.(\d+))?e([+-]?\d+)$/.exec(exp);
  const digits = (m[1] + (m[2] || "")).replace(/0+$/, "") || "0";
  const e10 = parseInt(m[3], 10); // value = 0.digits * 10**(e10+1)
  const sign = neg ? "-" : "";
  if (e10 < -4 || e10 >= 16) {
    // Scientific: d[.ddd]e±XX with at least two exponent digits.
    const mant = digits.length > 1
      ? digits[0] + "." + digits.slice(1) : digits[0];
    const esign = e10 < 0 ? "-" : "+";
    const eabs = String(Math.abs(e10)).padStart(2, "0");
    return `${sign}${mant}e${esign}${eabs}`;
  }
  // Fixed: decimal point after e10+1 digits.
  const point = e10 + 1;
  let out;
  if (point <= 0) {
    out = "0." + "0".repeat(-point) + digits;
  } else if (point >= digits.length) {
    out = digits + "0".repeat(point - digits.length) + ".0";
  } else {
    out = digits.slice(0, point) + "." + digits.slice(point);
  }
  return sign + out;
}

function toStr(x) {
  // newr `str(x)`: the display rendering (P8). `true`/`false`/
  // `none` are lowercase; floats use Python repr (floatStr);
  // collections render with elements rendered recursively and
  // strings bare (`[1, true, a]`); an `error` renders as its
  // message; a struct/enum value renders via its `_newrRepr`
  // method (emitted by the compiler for every struct/enum type).
  if (x === null || x === undefined) {
    return "none";
  }
  if (typeof x === "boolean") {
    return x ? "true" : "false";
  }
  if (typeof x === "string") {
    return x;
  }
  if (typeof x === "bigint") {
    return x.toString();
  }
  if (typeof x === "number") {
    return floatStr(x);
  }
  if (x instanceof NewrError) {
    return x.message;
  }
  if (x instanceof Result) {
    if (x.isOk()) {
      return `ok(${toStr(x._value)})`;
    }
    return `err(${toStr(x._error)})`;
  }
  if (Array.isArray(x)) {
    return "[" + x.map(toStr).join(", ") + "]";
  }
  if (x instanceof Map) {
    const parts = [];
    for (const [k, v] of x) {
      parts.push(`${toStr(k)}: ${toStr(v)}`);
    }
    return "{" + parts.join(", ") + "}";
  }
  if (x instanceof Set) {
    return "{" + [...x].map(toStr).join(", ") + "}";
  }
  if (typeof x._newrRepr === "function") {
    return x._newrRepr();
  }
  return String(x);
}

// ---------------------------------------------------------------- Result

class Result {
  // newr `Result[T, E]` (SPEC §10): the value form of errors.
  constructor(isOk, value = undefined, error = undefined) {
    this._isOk = isOk;
    this._value = value;
    this._error = error;
  }

  isOk() {
    return this._isOk;
  }

  isErr() {
    return !this._isOk;
  }

  unwrap() {
    if (this._isOk) {
      return this._value;
    }
    if (this._error instanceof Error) {
      throw this._error;
    }
    throw new NewrError(String(this._error));
  }

  unwrapOr(dflt) {
    return this._isOk ? this._value : dflt;
  }
}

function ok(value) {
  return new Result(true, value);
}

function err(error) {
  return new Result(false, undefined, error);
}

function tryResult(f) {
  // newr `try <expr>`: Ok-wrap the value, catch a thrown error
  // into `err(...)`. The caught error is coerced with `asError`
  // so `.message` / `.code` are always available.
  try {
    return ok(f());
  } catch (exc) {
    return err(asError(exc));
  }
}

// ---------------------------------------------------------------- equality (J6)

function eq(a, b) {
  // newr `==`: structural equality (§7.7). Primitives compare by
  // value (BigInt 1n is not == number 1 — the checker forbids mixed
  // comparisons, so that never arises from a checked program);
  // lists compare element-wise, dicts by key (order-insensitive),
  // sets unordered, structs/enums by constructor + fields, Results
  // by kind + payload. Functions and errors compare by identity.
  if (a === b) {
    return true;
  }
  if (a === null || b === null || a === undefined || b === undefined) {
    return false;
  }
  if (typeof a !== typeof b) {
    return false;
  }
  if (typeof a === "number") {
    return false; // NaN (a === b already handled every other number)
  }
  if (Array.isArray(a) || Array.isArray(b)) {
    if (!Array.isArray(a) || !Array.isArray(b)
      || a.length !== b.length) {
      return false;
    }
    for (let i = 0; i < a.length; i++) {
      if (!eq(a[i], b[i])) {
        return false;
      }
    }
    return true;
  }
  if (a instanceof Map || b instanceof Map) {
    if (!(a instanceof Map) || !(b instanceof Map)
      || a.size !== b.size) {
      return false;
    }
    for (const [k, v] of a) {
      if (!b.has(k) || !eq(v, b.get(k))) {
        return false;
      }
    }
    return true;
  }
  if (a instanceof Set || b instanceof Set) {
    if (!(a instanceof Set) || !(b instanceof Set)
      || a.size !== b.size) {
      return false;
    }
    outer: for (const x of a) {
      for (const y of b) {
        if (eq(x, y)) {
          continue outer;
        }
      }
      return false;
    }
    return true;
  }
  if (a instanceof Result || b instanceof Result) {
    if (!(a instanceof Result) || !(b instanceof Result)) {
      return false;
    }
    if (a._isOk !== b._isOk) {
      return false;
    }
    return a._isOk ? eq(a._value, b._value) : eq(a._error, b._error);
  }
  if (a instanceof NewrError || b instanceof NewrError) {
    return false; // errors compare by identity (a === b above)
  }
  if (typeof a === "object") {
    if (a.constructor !== b.constructor) {
      return false;
    }
    const ka = Object.keys(a);
    const kb = Object.keys(b);
    if (ka.length !== kb.length) {
      return false;
    }
    for (const k of ka) {
      if (!Object.prototype.hasOwnProperty.call(b, k)
        || !eq(a[k], b[k])) {
        return false;
      }
    }
    return true;
  }
  return false;
}

// ---------------------------------------------------------------- ?. / ?? / accessors

function optChain(v, f) {
  // newr `?.`: `null` when `v` is `null`, else `f(v)`. The test is
  // `=== null`, never truthiness — `0n`, `""` and `[]` are values,
  // not absences. A whole `?.` chain collapses into one call with
  // the full postfix path inside `f`, so the base expression is
  // evaluated exactly once.
  return v === null ? null : f(v);
}

function coalesce(v, defaultFn) {
  // newr `??`: `v` when it is not `null`, else `defaultFn()`.
  // The default is a thunk so it is evaluated lazily.
  return v !== null ? v : defaultFn();
}

function listIndex(lst, i) {
  // newr `list[i]`: SPEC §9.2 — out-of-range indexing is a runtime
  // error. Valid indices are `0 <= i < len(lst)`; a negative index
  // is out of range (never a wrap).
  if (i < 0n || i >= BigInt(lst.length)) {
    throw codedError("IndexError", "list index out of range");
  }
  return lst[Number(i)];
}

function listGet(lst, i, dflt) {
  // newr `list.get(i, default)`: P9 — a negative index is
  // out-of-range (no wrap-around); anything outside
  // `0 <= i < len(lst)` returns `default`.
  return i >= 0n && i < BigInt(lst.length) ? lst[Number(i)] : dflt;
}

function listContains(lst, x) {
  // newr `x in [..]`: structural equality (a list of structs
  // contains an equal-but-different instance).
  for (const e of lst) {
    if (eq(e, x)) {
      return true;
    }
  }
  return false;
}

function setAdd(s, x) {
  // newr `set.add(x)`: a set holds no duplicates (SPEC 7.6/7.8).
  // A native JS Set uses identity for objects, so adding two
  // equal-but-distinct struct values would store both. Check
  // structurally first and skip when an equal element is present.
  for (const e of s) {
    if (eq(e, x)) {
      return;
    }
  }
  s.add(x);
}

function setContains(s, x) {
  // newr `x in s` / `s.contains(x)`: structural equality, like
  // listContains. Native Set.has is identity for objects, so it
  // would miss an equal-but-distinct struct value.
  for (const e of s) {
    if (eq(e, x)) {
      return true;
    }
  }
  return false;
}

function setRemove(s, x) {
  // newr `set.remove(x)`: removing a missing element is an error
  // (Python's `set.remove` raises KeyError; JS `Set.delete`
  // silently returns false). The match is structural: find the
  // stored element equal to x and delete THAT one, because native
  // Set.has/delete are identity for objects.
  for (const e of s) {
    if (eq(e, x)) {
      s.delete(e);
      return;
    }
  }
  throw codedError("KeyError", toStr(x));
}

function toSet(xs) {
  // newr `set(xs)`: build a set from a list, deduplicating by
  // structural equality (SPEC 7.6/7.8). A bare `new Set(xs)` would
  // keep two equal-but-distinct struct values as two elements.
  const s = new Set();
  for (const x of xs) {
    setAdd(s, x);
  }
  return s;
}

function strCmp(a, b) {
  // newr string ordering: lexicographic by Unicode code point
  // (SPEC 7.6). JS `<` compares UTF-16 code units, which orders an
  // astral character below BMP characters at U+E000 and above
  // (high surrogate D83D < FFFD, while by code points
  // U+1F600 > U+FFFD). Compare code point by code point; a strict
  // prefix is smaller.
  const ac = [...a];
  const bc = [...b];
  const n = Math.min(ac.length, bc.length);
  for (let i = 0; i < n; i++) {
    const d = ac[i].codePointAt(0) - bc[i].codePointAt(0);
    if (d !== 0) {
      return d;
    }
  }
  return ac.length - bc.length;
}

function lenOf(x) {
  // newr `len` as a first-class value: the same answer as the
  // `len(x)` call. A str counts code points (SPEC 7.6), not UTF-16
  // units; a list counts elements; a dict/set counts entries.
  if (typeof x === "string") {
    return BigInt([...x].length);
  }
  if (Array.isArray(x)) {
    return BigInt(x.length);
  }
  if (x instanceof Map || x instanceof Set) {
    return BigInt(x.size);
  }
  throw new NewrError("len() of unsupported value: " + toStr(x));
}

function unwrapOpt(v) {
  // newr `T?.unwrap()`: the value, or a thrown error on `null`.
  if (v === null || v === undefined) {
    throw new NewrError("unwrap of none");
  }
  return v;
}

function range(...args) {
  // newr `range(...)`: like Python's range, materialised as a list
  // of BigInts. range(stop) / range(start, stop) /
  // range(start, stop, step); a zero step is a ValueError.
  let start;
  let stop;
  let step;
  if (args.length === 1) {
    [start, stop, step] = [0n, args[0], 1n];
  } else if (args.length === 2) {
    [start, stop, step] = [args[0], args[1], 1n];
  } else {
    [start, stop, step] = args;
  }
  if (step === 0n) {
    throw codedError("ValueError", "range() arg 3 must not be zero");
  }
  const out = [];
  if (step > 0n) {
    for (let i = start; i < stop; i += step) {
      out.push(i);
    }
  } else {
    for (let i = start; i > stop; i += step) {
      out.push(i);
    }
  }
  return out;
}

// ---------------------------------------------------------------- print / input

function print(...args) {
  // newr `print`: every argument rendered with `toStr`, joined with
  // a single space (Python's print). A single string argument is
  // printed literally (no %-formatting: console.log with one arg
  // does not format).
  console.log(args.map(toStr).join(" "));
}

let _stdinLines = null;
let _stdinPos = 0;

function input(prompt = "") {
  // newr `input(prompt = "")`: read one line from stdin, like
  // Python's input(). The prompt (if any) is written with no
  // newline first. Bytes are read synchronously one at a time until
  // a newline, so this works on a pipe and on a terminal alike.
  if (prompt) {
    process.stdout.write(prompt);
  }
  if (_stdinLines === null) {
    // First call: slurp stdin lazily is wrong on a terminal (it
    // would block until EOF), so read byte-by-byte instead.
    _stdinLines = [];
  }
  let line = "";
  const buf = Buffer.alloc(1);
  for (;;) {
    let n;
    try {
      n = fs.readSync(0, buf, 0, 1);
    } catch (e) {
      break;
    }
    if (n === 0) {
      break; // EOF
    }
    const ch = buf.toString("utf-8");
    if (ch === "\n") {
      break;
    }
    line += ch;
  }
  return line.replace(/\r$/, "");
}

// ---------------------------------------------------------------- random

function randomInt(lo, hi) {
  // newr `random.int(lo, hi)`: inclusive on both ends (Python's
  // randint). Exact for any int64 range: rejection sampling over
  // random bytes (Math.random cannot cover a 64-bit range).
  if (hi < lo) {
    throw codedError("ValueError",
      "empty range for random.int()");
  }
  const span = hi - lo + 1n;
  const bits = span.toString(2).length;
  const bytes = Math.ceil(bits / 8);
  for (;;) {
    const buf = randomBytes(bytes);
    const v = BigInt("0x" + buf.toString("hex"));
    if (v < span) {
      return lo + v;
    }
  }
}

function randomChoice(xs) {
  if (xs.length === 0) {
    throw codedError("IndexError",
      "Cannot choose from an empty sequence");
  }
  return xs[Math.floor(Math.random() * xs.length)];
}

function randomShuffle(xs) {
  for (let i = xs.length - 1; i > 0; i--) {
    const j = Math.floor(Math.random() * (i + 1));
    [xs[i], xs[j]] = [xs[j], xs[i]];
  }
  return null;
}

// ---------------------------------------------------------------- time (P4)

const _TIME_FORMATS = new Set(["%Y-%m-%d", "%H:%M", "%Y-%m-%d %H:%M",
  "%d/%m/%Y", "%H:%M:%S"]);

function timeFormat(ts, fmt) {
  // newr `time.format(ts, fmt)`: UTC, never local time (P4) — the
  // same program prints the same output on every machine. The
  // format must be one of the spec's fixed set (§12.2).
  if (!_TIME_FORMATS.has(fmt)) {
    throw new NewrError(`unsupported time format: ${JSON.stringify(fmt)}`);
  }
  const d = new Date(Number(ts) * 1000);
  const p2 = (n) => String(n).padStart(2, "0");
  const p4 = (n) => String(n).padStart(4, "0");
  const y = p4(d.getUTCFullYear());
  const mo = p2(d.getUTCMonth() + 1);
  const day = p2(d.getUTCDate());
  const h = p2(d.getUTCHours());
  const mi = p2(d.getUTCMinutes());
  const s = p2(d.getUTCSeconds());
  switch (fmt) {
    case "%Y-%m-%d":
      return `${y}-${mo}-${day}`;
    case "%H:%M":
      return `${h}:${mi}`;
    case "%Y-%m-%d %H:%M":
      return `${y}-${mo}-${day} ${h}:${mi}`;
    case "%d/%m/%Y":
      return `${day}/${mo}/${y}`;
    case "%H:%M:%S":
      return `${h}:${mi}:${s}`;
    default:
      throw new NewrError(`unsupported time format: ${JSON.stringify(fmt)}`);
  }
}

// ---------------------------------------------------------------- file I/O

function fileRead(path) {
  // newr `file.read(path)`: the whole file as UTF-8 text. A helper
  // (not an inline read) so a read inside a lambda body or an
  // optional chain still happens at the right time. A missing file
  // throws a Node error (code "ENOENT" — the Python target reports
  // "FileNotFoundError" there; the code for host IO errors is
  // host-specific, BUILD_REPORT §17).
  return fs.readFileSync(path, "utf-8");
}

function fileWrite(path, content) {
  // newr `file.write(path, content)`: overwrite (create if needed).
  fs.writeFileSync(path, content, "utf-8");
  return null;
}

function fileAppend(path, content) {
  // newr `file.append(path, content)`: append (create if needed).
  fs.appendFileSync(path, content, "utf-8");
  return null;
}

function fileExists(path) {
  // newr `file.exists(path)`.
  return fs.existsSync(path);
}

// ---------------------------------------------------------------- db module
// SQLite via the `node:sqlite` builtin, synchronous (SPEC.md section
// 12.5). Handles are ints, fd-style: ids from 1, a registry maps
// each id to its DatabaseSync. `?` binding is mandatory: values are
// never interpolated into the SQL text. Int columns read back
// through BigInt plus checkInt (J9: node:sqlite returns JS numbers
// and throws above 2^53); bool binds as 0/1 because node:sqlite
// does not accept boolean bind parameters.

const _dbConnections = new Map();
let _dbNextId = 1;

function dbOpen(path) {
  // newr `db.open(path)`: open (create if needed) and return a handle.
  const { DatabaseSync } = require("node:sqlite");
  const db = new DatabaseSync(path);
  db.exec("PRAGMA foreign_keys = ON");
  db.exec("PRAGMA busy_timeout = 5000");
  const handle = _dbNextId;
  _dbNextId += 1;
  _dbConnections.set(handle, db);
  return checkInt(BigInt(handle));
}

function _dbConn(handle) {
  const db = _dbConnections.get(Number(handle));
  if (db === undefined) {
    throw new NewrError(`db: unknown or closed handle ${handle}`);
  }
  return db;
}

function _dbBindArg(value) {
  // node:sqlite binds null, number, string, bigint, Uint8Array.
  // A newr bool binds as 0/1; a newr int is already a BigInt.
  if (typeof value === "boolean") {
    return value ? 1 : 0;
  }
  return value;
}

function _dbValue(value, typeStr) {
  // Convert one raw SQLite value to the newr value for a `db.query`
  // field type (`int`, `float`, `str`, `bool`, each optionally `?`).
  // Mirrors the Python `_db_value` (newr_rt.py): NULL lands only in
  // a nullable field, and a value that cannot convert throws
  // instead of silently producing a wrong value.
  const nullable = typeStr.endsWith("?");
  const base = nullable ? typeStr.slice(0, -1) : typeStr;
  if (value === null || value === undefined) {
    if (nullable) {
      return null;
    }
    throw new NewrError(`db: NULL in non-nullable ${base} field`);
  }
  if (base === "int") {
    if (typeof value === "boolean") {
      throw new NewrError("db: cannot convert bool to int");
    }
    if (typeof value === "bigint") {
      return checkInt(value);
    }
    if (typeof value === "number") {
      if (!Number.isInteger(value)) {
        throw new NewrError(`db: cannot convert ${value} to int`);
      }
      return checkInt(BigInt(value));
    }
    if (typeof value === "string") {
      const s = value.trim();
      if (!/^-?\d+$/.test(s)) {
        throw new NewrError(`db: cannot convert ${value} to int`);
      }
      return checkInt(BigInt(s));
    }
    throw new NewrError(`db: cannot convert ${typeof value} to int`);
  }
  if (base === "float") {
    if (typeof value === "boolean") {
      throw new NewrError("db: cannot convert bool to float");
    }
    if (typeof value === "bigint") {
      return Number(value);
    }
    if (typeof value === "number") {
      return value;
    }
    if (typeof value === "string") {
      const s = value.trim();
      if (s === "") {
        throw new NewrError(`db: cannot convert ${value} to float`);
      }
      const n = Number(s);
      if (Number.isNaN(n)) {
        throw new NewrError(`db: cannot convert ${value} to float`);
      }
      return n;
    }
    throw new NewrError(`db: cannot convert ${typeof value} to float`);
  }
  if (base === "str") {
    if (typeof value === "boolean") {
      return value ? "true" : "false";
    }
    if (typeof value === "string") {
      return value;
    }
    return toStr(value);
  }
  if (base === "bool") {
    if (typeof value === "boolean") {
      return value;
    }
    if (typeof value === "bigint") {
      if (value === 0n) return false;
      if (value === 1n) return true;
      throw new NewrError(`db: cannot convert ${value} to bool`);
    }
    if (typeof value === "number") {
      if (value === 0) return false;
      if (value === 1) return true;
      throw new NewrError(`db: cannot convert ${value} to bool`);
    }
    if (typeof value === "string") {
      if (value === "0") return false;
      if (value === "1") return true;
      throw new NewrError(`db: cannot convert ${value} to bool`);
    }
    throw new NewrError(`db: cannot convert ${typeof value} to bool`);
  }
  throw new NewrError(`db: unknown field type ${typeStr}`);
}

function dbExec(handle, sql, args) {
  // newr `db.exec(handle, sql, args...)`: run one statement, return
  // the rows affected (0 for DDL).
  const info = _dbConn(handle).prepare(sql).run(...args.map(_dbBindArg));
  return checkInt(BigInt(info.changes));
}

function dbQuery(handle, sql, args, fieldNames, fieldTypes) {
  // newr `db.query(handle, sql, args...)`: run a SELECT and return
  // one list per row, converted values in field order. A missing
  // column throws; extra columns are ignored.
  const rows = _dbConn(handle).prepare(sql).all(...args.map(_dbBindArg));
  const out = [];
  for (const raw of rows) {
    const row = [];
    for (let i = 0; i < fieldNames.length; i++) {
      const name = fieldNames[i];
      if (!(name in raw)) {
        throw new NewrError(`db: column ${name} missing from query result`);
      }
      row.push(_dbValue(raw[name], fieldTypes[i]));
    }
    out.push(row);
  }
  return out;
}

function dbBegin(handle) {
  // newr `db.begin(handle)`: BEGIN IMMEDIATE.
  _dbConn(handle).exec("BEGIN IMMEDIATE");
  return null;
}

function dbCommit(handle) {
  // newr `db.commit(handle)`: COMMIT.
  _dbConn(handle).exec("COMMIT");
  return null;
}

function dbRollback(handle) {
  // newr `db.rollback(handle)`: ROLLBACK.
  _dbConn(handle).exec("ROLLBACK");
  return null;
}

function dbLastId(handle) {
  // newr `db.last_id(handle)`: the last INSERT rowid.
  const row = _dbConn(handle).prepare("SELECT last_insert_rowid() AS id").get();
  return checkInt(BigInt(row.id));
}

function dbClose(handle) {
  // newr `db.close(handle)`: close and drop the handle. A second
  // close (or any use after close) raises NewrError.
  const db = _dbConnections.get(Number(handle));
  if (db === undefined) {
    throw new NewrError(`db: unknown or closed handle ${handle}`);
  }
  _dbConnections.delete(Number(handle));
  db.close();
  return null;
}

module.exports = {
  INT64_MIN,
  INT64_MAX,
  checkInt,
  add,
  sub,
  mul,
  neg,
  powInt,
  powFloat,
  tdiv,
  tmod,
  fmod,
  fdiv,
  roundHalfAway,
  toInt,
  toFloat,
  toBool,
  toStr,
  floatStr,
  NewrError,
  NewrOverflowError,
  NewrDivisionByZeroError,
  asError,
  Result,
  ok,
  err,
  tryResult,
  optChain,
  coalesce,
  listIndex,
  listGet,
  listContains,
  setAdd,
  setContains,
  setRemove,
  toSet,
  strCmp,
  lenOf,
  unwrapOpt,
  eq,
  range,
  print,
  input,
  randomInt,
  randomChoice,
  randomShuffle,
  timeFormat,
  fileRead,
  fileWrite,
  fileAppend,
  fileExists,
  dbOpen,
  dbExec,
  dbQuery,
  dbBegin,
  dbCommit,
  dbRollback,
  dbLastId,
  dbClose,
};
