// Node-side unit tests for compiler/newr_rt.js (M3).
//
// Mirrors tests/test_newr_rt.py case for case (same rule, both
// targets). Run with `node tests/js/rt_tests.js` from the repo root,
// or via `python -m unittest tests.test_newr_rt_js`.
//
// Uses node:assert/strict; every failure prints the assertion and
// exits non-zero. Keep the battery aligned with the Python file:
// when a rule is added here, add it there too (or record why not).

"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const rt = require("../../compiler/newr_rt.js");

let passed = 0;
function check(name, fn) {
  fn();
  passed++;
  console.log(`ok ${name}`);
}

function throwsCode(fn, code) {
  try {
    fn();
  } catch (e) {
    assert.equal(e.code, code, `expected code ${code}, got ${e.code}: ${e.message}`);
    return e;
  }
  assert.fail(`expected ${code}, nothing thrown`);
}

// Like throwsCode, but for the checked-int error classes: the
// Python target's NewrOverflowError / NewrDivisionByZeroError carry
// code None (they are NewrError subclasses constructed without a
// code), so a newr `catch e:` sees e.code == none. The class is
// told by `name`, not by `code` (conformance/ok/try_catch.newr
// pins e.code.is_none() == true for a division by zero).
function throwsNamed(fn, name) {
  try {
    fn();
  } catch (e) {
    assert.equal(e.name, name, `expected ${name}, got ${e.name}: ${e.message}`);
    assert.equal(e.code, null, `expected code null, got ${e.code}`);
    return e;
  }
  assert.fail(`expected ${name}, nothing thrown`);
}

// ---------------------------------------------------------------- constants
check("int64 bounds", () => {
  assert.equal(rt.INT64_MIN, -(2n ** 63n));
  assert.equal(rt.INT64_MAX, 2n ** 63n - 1n);
});

// ---------------------------------------------------------------- checked arithmetic
check("add/sub/mul ok", () => {
  assert.equal(rt.add(2n, 3n), 5n);
  assert.equal(rt.sub(10n, 4n), 6n);
  assert.equal(rt.mul(3n, 4n), 12n);
});

check("add overflow", () => {
  throwsNamed(() => rt.add(rt.INT64_MAX, 1n), "NewrOverflowError");
});

check("sub underflow", () => {
  throwsNamed(() => rt.sub(rt.INT64_MIN, 1n), "NewrOverflowError");
});

check("mul overflow", () => {
  throwsNamed(() => rt.mul(rt.INT64_MAX, 2n), "NewrOverflowError");
});

check("neg", () => {
  assert.equal(rt.neg(5n), -5n);
  assert.equal(rt.neg(-5n), 5n);
  throwsNamed(() => rt.neg(rt.INT64_MIN), "NewrOverflowError");
});

check("checkInt", () => {
  assert.equal(rt.checkInt(0n), 0n);
  throwsNamed(() => rt.checkInt(2n ** 63n), "NewrOverflowError");
});

check("overflow error carries message", () => {
  const e = throwsNamed(() => rt.add(rt.INT64_MAX, 1n), "NewrOverflowError");
  assert.match(e.message, /9223372036854775808/);
  assert.equal(rt.asError(e), e);
});

// ---------------------------------------------------------------- tdiv/tmod
check("truncating division", () => {
  assert.equal(rt.tdiv(7n, 2n), 3n);
  assert.equal(rt.tdiv(-7n, 2n), -3n);
  assert.equal(rt.tdiv(7n, -2n), -3n);
  assert.equal(rt.tdiv(-7n, -2n), 3n);
});

check("dividend-sign modulo", () => {
  assert.equal(rt.tmod(7n, 3n), 1n);
  assert.equal(rt.tmod(-7n, 3n), -1n);
  assert.equal(rt.tmod(7n, -3n), 1n);
  assert.equal(rt.tmod(-7n, -3n), -1n);
});

check("division by zero", () => {
  throwsNamed(() => rt.tdiv(1n, 0n), "NewrDivisionByZeroError");
  throwsNamed(() => rt.tmod(1n, 0n), "NewrDivisionByZeroError");
});

check("min div minus one", () => {
  throwsNamed(() => rt.tdiv(rt.INT64_MIN, -1n), "NewrOverflowError");
  assert.equal(rt.tmod(rt.INT64_MIN, -1n), 0n);
});

check("div/mod invariant", () => {
  for (const [a, b] of [[7n, 3n], [-7n, 3n], [7n, -3n], [-7n, -3n],
    [100n, 7n], [-100n, 7n]]) {
    assert.equal(rt.tdiv(a, b) * b + rt.tmod(a, b), a);
  }
});

// ---------------------------------------------------------------- pow
check("powInt ok", () => {
  assert.equal(rt.powInt(2n, 10n), 1024n);
  assert.equal(rt.powInt(2n, 0n), 1n);
  assert.equal(rt.powInt(0n, 0n), 1n);
  assert.equal(rt.powInt(0n, 5n), 0n);
  assert.equal(rt.powInt(-2n, 3n), -8n);
  assert.equal(rt.powInt(-2n, 2n), 4n);
});

check("powInt overflow", () => {
  throwsNamed(() => rt.powInt(2n, 63n), "NewrOverflowError");
});

check("powInt negative exponent", () => {
  throwsCode(() => rt.powInt(2n, -1n), null);
});

check("powInt huge exponent does not hang", () => {
  const t0 = Date.now();
  throwsNamed(() => rt.powInt(10n, 10n ** 9n), "NewrOverflowError");
  assert.ok(Date.now() - t0 < 5000, "powInt(10, 1e9) took too long");
});

check("powFloat plain", () => {
  assert.equal(rt.powFloat(2.0, 3.0), 8.0);
  assert.equal(rt.powFloat(9.0, 0.5), 3.0);
});

check("powFloat negative base fractional exponent is NaN", () => {
  assert.ok(Number.isNaN(rt.powFloat(-8.0, 1 / 3)));
});

check("powFloat overflow is infinity", () => {
  assert.equal(rt.powFloat(1e308, 2.0), Infinity);
  assert.equal(rt.powFloat(-1e308, 3.0), -Infinity);
});

check("powFloat zero base negative exponent throws", () => {
  throwsCode(() => rt.powFloat(0, -1), "ZeroDivisionError");
});

check("fmod dividend sign", () => {
  assert.equal(rt.fmod(-7.0, 2.0), -1.0);
  assert.equal(rt.fmod(7.0, -2.0), 1.0);
});

check("fmod by zero", () => {
  throwsNamed(() => rt.fmod(1.0, 0.0), "NewrDivisionByZeroError");
});

check("fdiv", () => {
  assert.equal(rt.fdiv(7.0, 2.0), 3.5);
  assert.equal(rt.fdiv(-7.0, 2.0), -3.5);
  assert.equal(rt.fdiv(1.0, 3.0), 1.0 / 3.0);
});

check("fdiv by zero", () => {
  // P6: float /0 is an error (JS `/` would give Infinity/NaN).
  throwsNamed(() => rt.fdiv(1.0, 0.0), "NewrDivisionByZeroError");
  throwsNamed(() => rt.fdiv(0.0, 0.0), "NewrDivisionByZeroError");
  throwsNamed(() => rt.fdiv(1.0, -0.0), "NewrDivisionByZeroError");
});

// ---------------------------------------------------------------- round (exact value)
check("round ties go away from zero", () => {
  assert.equal(rt.roundHalfAway(2.5), 3n);
  assert.equal(rt.roundHalfAway(-2.5), -3n);
  assert.equal(rt.roundHalfAway(3.5), 4n);
  assert.equal(rt.roundHalfAway(-3.5), -4n);
});

check("round plain values", () => {
  assert.equal(rt.roundHalfAway(2.4), 2n);
  assert.equal(rt.roundHalfAway(2.6), 3n);
  assert.equal(rt.roundHalfAway(0.5), 1n);
});

check("round rounds the exact float value", () => {
  // The float 2.675 is stored slightly below 2.675.
  assert.equal(rt.roundHalfAway(2.675, 2), 2.67);
  assert.equal(rt.roundHalfAway(1.005, 2), 1);
  assert.equal(rt.roundHalfAway(0.125, 2), 0.13);
  // An already-integral large float keeps its exact value.
  assert.equal(rt.roundHalfAway(9223372036854774784.0),
    9223372036854774784n);
});

check("round ndigits returns float", () => {
  assert.equal(rt.roundHalfAway(123.456, 1), 123.5);
  assert.equal(rt.roundHalfAway(999.999, 2), 1000);
  assert.equal(typeof rt.roundHalfAway(1.234, 2), "number");
});

check("round negative ndigits", () => {
  assert.equal(rt.roundHalfAway(5, -1), 10);
  assert.equal(rt.roundHalfAway(15, -1), 20);
  assert.equal(rt.roundHalfAway(1250, -2), 1300);
});

check("round overflow", () => {
  throwsNamed(() => rt.roundHalfAway(1e30), "NewrOverflowError");
});

check("round non-finite", () => {
  throwsCode(() => rt.roundHalfAway(Infinity), "OverflowError");
  assert.equal(rt.roundHalfAway(Infinity, 2), Infinity);
  assert.ok(Number.isNaN(rt.roundHalfAway(NaN, 2)));
});

check("round negative zero", () => {
  assert.ok(Object.is(rt.roundHalfAway(-0.001, 2), -0));
});

// ---------------------------------------------------------------- float rendering (J4)
check("floatStr matches Python repr", () => {
  const cases = [
    [0.0, "0.0"], [-0.0, "-0.0"], [1.0, "1.0"], [-1.5, "-1.5"],
    [1e15, "1000000000000000.0"], [1e16, "1e+16"], [1e20, "1e+20"],
    [1e21, "1e+21"], [0.0001, "0.0001"], [0.00001, "1e-05"],
    [1.23456789e-10, "1.23456789e-10"], [123456.789, "123456.789"],
    [2.675, "2.675"], [0.1, "0.1"], [0.30000000000000004, "0.30000000000000004"],
    [1e-7, "1e-07"], [5e-324, "5e-324"],
    [1.7976931348623157e308, "1.7976931348623157e+308"],
    [3.141592653589793, "3.141592653589793"], [100.0, "100.0"],
    [2.5, "2.5"], [-2.5e-5, "-2.5e-05"],
    [123456789.12345679, "123456789.12345679"],
    [9.223372036854776e18, "9.223372036854776e+18"],
    [1234560000.0, "1234560000.0"],
  ];
  for (const [v, want] of cases) {
    assert.equal(rt.floatStr(v), want, `floatStr(${v})`);
  }
  assert.equal(rt.floatStr(NaN), "nan");
  assert.equal(rt.floatStr(Infinity), "inf");
  assert.equal(rt.floatStr(-Infinity), "-inf");
});

// ---------------------------------------------------------------- conversions
check("toInt from str", () => {
  assert.equal(rt.toInt("42"), 42n);
  assert.equal(rt.toInt("  -42 "), -42n);
  assert.equal(rt.toInt("+7"), 7n);
  throwsCode(() => rt.toInt("3.5"), "ValueError");
  throwsCode(() => rt.toInt("abc"), "ValueError");
});

check("toInt range checked", () => {
  throwsNamed(() => rt.toInt("99999999999999999999"), "NewrOverflowError");
});

check("toInt from float truncates", () => {
  assert.equal(rt.toInt(3.7), 3n);
  assert.equal(rt.toInt(-3.7), -3n);
  throwsCode(() => rt.toInt(Infinity), "OverflowError");
});

check("toInt from bool", () => {
  assert.equal(rt.toInt(true), 1n);
  assert.equal(rt.toInt(false), 0n);
});

check("toFloat", () => {
  assert.equal(rt.toFloat(2n), 2.0);
  assert.equal(rt.toFloat("inf"), Infinity);
  assert.equal(rt.toFloat("-inf"), -Infinity);
  assert.ok(Number.isNaN(rt.toFloat("nan")));
  assert.equal(rt.toFloat("1_000"), 1000);
  throwsCode(() => rt.toFloat("abc"), "ValueError");
  throwsCode(() => rt.toFloat(10n ** 400n), "OverflowError");
});

check("toBool parses", () => {
  assert.equal(rt.toBool("true"), true);
  assert.equal(rt.toBool("false"), false);
  throwsCode(() => rt.toBool("yes"), "ValueError");
  assert.equal(rt.toBool(0n), false);
  assert.equal(rt.toBool(1n), true);
  assert.equal(rt.toBool(0.0), false);
});

// ---------------------------------------------------------------- toStr
check("toStr scalars", () => {
  assert.equal(rt.toStr(null), "none");
  assert.equal(rt.toStr(true), "true");
  assert.equal(rt.toStr(false), "false");
  assert.equal(rt.toStr(42n), "42");
  assert.equal(rt.toStr(1.5), "1.5");
  assert.equal(rt.toStr(1.0), "1.0");
  assert.equal(rt.toStr("a"), "a");
});

check("toStr collections", () => {
  assert.equal(rt.toStr([1n, 2n, 3n]), "[1, 2, 3]");
  assert.equal(rt.toStr([true, false]), "[true, false]");
  assert.equal(rt.toStr(new Map([["k", 1n], ["j", 2n]])), "{k: 1, j: 2}");
  assert.equal(rt.toStr(new Set(["a"])), "{a}");
  assert.equal(rt.toStr([]), "[]");
});

check("toStr error and result", () => {
  assert.equal(rt.toStr(new rt.NewrError("boom")), "boom");
  assert.equal(rt.toStr(rt.ok(3n)), "ok(3)");
  assert.equal(rt.toStr(rt.err(new rt.NewrError("bad"))), "err(bad)");
});

check("toStr struct-like values", () => {
  const p = { _newrRepr: () => "Point(x: 1, y: 2)" };
  assert.equal(rt.toStr(p), "Point(x: 1, y: 2)");
});

// ---------------------------------------------------------------- Result
check("Result ok/err", () => {
  const r = rt.ok(7n);
  assert.equal(r.isOk(), true);
  assert.equal(r.isErr(), false);
  assert.equal(r.unwrap(), 7n);
  assert.equal(r.unwrapOr(0n), 7n);
  const e = rt.err(new rt.NewrError("bad"));
  assert.equal(e.isErr(), true);
  assert.equal(e.unwrapOr(9n), 9n);
  throwsCode(() => e.unwrap(), null);
});

check("tryResult", () => {
  const r1 = rt.tryResult(() => 6n);
  assert.equal(r1.isOk(), true);
  assert.equal(r1.unwrap(), 6n);
  const r2 = rt.tryResult(() => { throw new Error("x"); });
  assert.equal(r2.isErr(), true);
  assert.equal(r2._error.message, "x");
});

// ---------------------------------------------------------------- equality
check("eq primitives and collections", () => {
  assert.equal(rt.eq(1n, 1n), true);
  assert.equal(rt.eq(1n, 2n), false);
  assert.equal(rt.eq(1n, 1), false);
  assert.equal(rt.eq("a", "a"), true);
  assert.equal(rt.eq(null, null), true);
  assert.equal(rt.eq(null, 0n), false);
  assert.equal(rt.eq([1n, 2n], [1n, 2n]), true);
  assert.equal(rt.eq([1n], [1n, 2n]), false);
  assert.equal(rt.eq(new Map([["a", 1n]]), new Map([["a", 1n]])), true);
  assert.equal(rt.eq(new Map([["a", 1n]]), new Map([["a", 2n]])), false);
  assert.equal(rt.eq(new Set([1n, 2n]), new Set([2n, 1n])), true);
  assert.equal(rt.eq(rt.ok(1n), rt.ok(1n)), true);
  assert.equal(rt.eq(rt.ok(1n), rt.err(new rt.NewrError("x"))), false);
});

// ---------------------------------------------------------------- ?. / ?? / accessors
check("optChain and coalesce", () => {
  assert.equal(rt.optChain(null, (v) => v + 1n), null);
  assert.equal(rt.optChain(5n, (v) => v + 1n), 6n);
  assert.equal(rt.coalesce(0n, () => 5n), 0n);
  assert.equal(rt.coalesce(null, () => 5n), 5n);
  assert.equal(rt.coalesce("", () => "d"), "");
});

check("listIndex", () => {
  assert.equal(rt.listIndex([1n, 2n], 1n), 2n);
  throwsCode(() => rt.listIndex([1n], -1n), "IndexError");
  throwsCode(() => rt.listIndex([1n], 1n), "IndexError");
});

check("listGet", () => {
  assert.equal(rt.listGet([1n, 2n], 0n, 9n), 1n);
  assert.equal(rt.listGet([1n, 2n], 5n, 9n), 9n);
  assert.equal(rt.listGet([1n, 2n], -1n, 9n), 9n);
});

check("listContains structural", () => {
  assert.equal(rt.listContains([1n, 2n], 2n), true);
  assert.equal(rt.listContains([1n, 2n], 9n), false);
  assert.equal(rt.listContains([[1n]], [1n]), true);
});

check("setRemove", () => {
  const s = new Set([1n, 2n]);
  rt.setRemove(s, 1n);
  assert.equal(s.has(1n), false);
  throwsCode(() => rt.setRemove(s, 99n), "KeyError");
});

check("unwrapOpt", () => {
  assert.equal(rt.unwrapOpt(5n), 5n);
  throwsCode(() => rt.unwrapOpt(null), null);
});

check("range", () => {
  assert.deepEqual(rt.range(3n), [0n, 1n, 2n]);
  assert.deepEqual(rt.range(1n, 4n), [1n, 2n, 3n]);
  assert.deepEqual(rt.range(0n, 10n, 3n), [0n, 3n, 6n, 9n]);
  assert.deepEqual(rt.range(5n, 0n, -2n), [5n, 3n, 1n]);
  throwsCode(() => rt.range(0n, 5n, 0n), "ValueError");
});

// ---------------------------------------------------------------- file ops
check("file read/write/append", () => {
  const d = fs.mkdtempSync(path.join(os.tmpdir(), "newr-rt-"));
  const p = path.join(d, "t.txt");
  rt.fileWrite(p, "hello");
  assert.equal(rt.fileExists(p), true);
  assert.equal(rt.fileRead(p), "hello");
  rt.fileAppend(p, " world");
  assert.equal(rt.fileRead(p), "hello world");
  assert.equal(rt.fileExists(path.join(d, "nope")), false);
  fs.rmSync(d, { recursive: true, force: true });
});

check("file read missing raises", () => {
  const e = (() => {
    try {
      rt.fileRead("/definitely/not/here/xyz.txt");
    } catch (err) {
      return err;
    }
    return null;
  })();
  assert.ok(e, "expected a throw");
  assert.equal(e.code, "ENOENT");
});

// ---------------------------------------------------------------- time
check("timeFormat UTC", () => {
  assert.equal(rt.timeFormat(1700000000n, "%Y-%m-%d"), "2023-11-14");
  assert.equal(rt.timeFormat(0n, "%H:%M:%S"), "00:00:00");
  assert.equal(rt.timeFormat(1700000000n, "%d/%m/%Y"), "14/11/2023");
  throwsCode(() => rt.timeFormat(0n, "%c"), null);
});

// ---------------------------------------------------------------- random
check("randomInt in range", () => {
  for (let i = 0; i < 50; i++) {
    const r = rt.randomInt(1n, 10n);
    assert.ok(r >= 1n && r <= 10n, `randomInt out of range: ${r}`);
  }
  assert.equal(rt.randomInt(5n, 5n), 5n);
  const big = rt.randomInt(0n, 2n ** 64n - 1n);
  assert.equal(typeof big, "bigint");
});

check("randomChoice", () => {
  const xs = [1n, 2n, 3n];
  assert.ok(xs.includes(rt.randomChoice(xs)));
  throwsCode(() => rt.randomChoice([]), "IndexError");
});

check("randomShuffle", () => {
  const xs = [1n, 2n, 3n, 4n, 5n];
  rt.randomShuffle(xs);
  assert.deepEqual([...xs].sort(), [1n, 2n, 3n, 4n, 5n]);
});

// ---------------------------------------------------------------- asError
check("asError", () => {
  const n = new rt.NewrError("x", "E1");
  assert.equal(rt.asError(n), n);
  assert.equal(rt.asError("boom").message, "boom");
  const wrapped = rt.asError(new TypeError("nope"));
  assert.equal(wrapped.message, "nope");
  assert.equal(wrapped.code, "TypeError");
  assert.equal(rt.asError(42).message, "42");
});

console.log(`\nrt_tests.js: ${passed} checks passed`);
