// C#-side unit tests for compiler/newr_rt.cs (M4).
#nullable enable annotations
//
// Mirrors tests/js/rt_tests.js case for case (same rule, both
// targets) — same 61 checks, adapted to C# types (long instead of
// BigInt). Run with `dotnet run` from a temp project holding copies
// of newr_rt.cs and this file, or via
// `python -m unittest tests.test_newr_rt_cs`.
//
// Every failure prints the assertion and exits non-zero. Keep the
// battery aligned with the JS file: when a rule is added here, add
// it there too (or record why not).

using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Numerics;

int passed = 0;

void Check(string name, Action fn)
{
    fn();
    passed++;
    Console.WriteLine("ok " + name);
}

void Eq(object? got, object? want, string what)
{
    // List<T>.Equals is reference equality, so compare lists
    // element-wise here (the NewrRt.Eq check itself tests the
    // runtime's structural equality separately).
    bool equal;
    if (got is System.Collections.IList gl && want is System.Collections.IList wl)
        equal = gl.Cast<object?>().SequenceEqual(wl.Cast<object?>());
    else
        equal = Equals(got, want);
    if (!equal)
        throw new Exception($"{what}: expected <{want}>, got <{got}>");
}

void Ok(bool cond, string what)
{
    if (!cond) throw new Exception("expected true: " + what);
}

/// <summary>Expect a throw whose NewrError code equals <paramref name="code"/> (null allowed).</summary>
NewrRt.NewrError ThrowsCode(Action fn, string? code)
{
    try { fn(); }
    catch (NewrRt.NewrError e)
    {
        Eq((object?)e.Code, (object?)code, "error code");
        return e;
    }
    catch (Exception t)
    {
        throw new Exception($"expected NewrError code {code}, got {t.GetType().Name}: {t.Message}");
    }
    throw new Exception($"expected code {code}, nothing thrown");
}

/// <summary>Expect a throw of the named checked-int error class, code null.</summary>
NewrRt.NewrError ThrowsNamed(Action fn, string name)
{
    try { fn(); }
    catch (NewrRt.NewrError e)
    {
        Eq(e.GetType().Name, name, "error class");
        Eq((object?)e.Code, null, "error code (must be null)");
        return e;
    }
    catch (Exception t)
    {
        throw new Exception($"expected {name}, got {t.GetType().Name}: {t.Message}");
    }
    throw new Exception($"expected {name}, nothing thrown");
}

// ---------------------------------------------------------------- constants
Check("int64 bounds", () =>
{
    Eq(NewrRt.Int64Min, long.MinValue, "INT64_MIN");
    Eq(NewrRt.Int64Max, long.MaxValue, "INT64_MAX");
});

// ---------------------------------------------------------------- checked arithmetic
Check("add/sub/mul ok", () =>
{
    Eq(NewrRt.Add(2, 3), 5L, "add");
    Eq(NewrRt.Sub(10, 4), 6L, "sub");
    Eq(NewrRt.Mul(3, 4), 12L, "mul");
});
Check("add overflow", () => { ThrowsNamed(() => NewrRt.Add(long.MaxValue, 1), "NewrOverflowError"); });
Check("sub underflow", () => { ThrowsNamed(() => NewrRt.Sub(long.MinValue, 1), "NewrOverflowError"); });
Check("mul overflow", () => { ThrowsNamed(() => NewrRt.Mul(long.MaxValue, 2), "NewrOverflowError"); });
Check("neg", () =>
{
    Eq(NewrRt.Neg(5), -5L, "neg 5");
    Eq(NewrRt.Neg(-5), 5L, "neg -5");
    ThrowsNamed(() => NewrRt.Neg(long.MinValue), "NewrOverflowError");
});
Check("checkInt", () =>
{
    Eq(NewrRt.CheckInt(0L), 0L, "checkInt 0");
    ThrowsNamed(() => NewrRt.CheckInt(BigInteger.One << 63), "NewrOverflowError");
});
Check("overflow error carries message", () =>
{
    var e = ThrowsNamed(() => NewrRt.Add(long.MaxValue, 1), "NewrOverflowError");
    Ok(e.Message.Contains("9223372036854775808"), "message mentions value");
    Eq((object)NewrRt.AsError(e), (object)e, "asError passes NewrError through");
});

// ---------------------------------------------------------------- tdiv/tmod
Check("truncating division", () =>
{
    Eq(NewrRt.Tdiv(7, 2), 3L, "7/2");
    Eq(NewrRt.Tdiv(-7, 2), -3L, "-7/2");
    Eq(NewrRt.Tdiv(7, -2), -3L, "7/-2");
    Eq(NewrRt.Tdiv(-7, -2), 3L, "-7/-2");
});
Check("dividend-sign modulo", () =>
{
    Eq(NewrRt.Tmod(7, 3), 1L, "7%3");
    Eq(NewrRt.Tmod(-7, 3), -1L, "-7%3");
    Eq(NewrRt.Tmod(7, -3), 1L, "7%-3");
    Eq(NewrRt.Tmod(-7, -3), -1L, "-7%-3");
});
Check("division by zero", () =>
{
    ThrowsNamed(() => NewrRt.Tdiv(1, 0), "NewrDivisionByZeroError");
    ThrowsNamed(() => NewrRt.Tmod(1, 0), "NewrDivisionByZeroError");
});
Check("min div minus one", () =>
{
    ThrowsNamed(() => NewrRt.Tdiv(long.MinValue, -1), "NewrOverflowError");
    Eq(NewrRt.Tmod(long.MinValue, -1), 0L, "MIN%-1");
});
Check("div/mod invariant", () =>
{
    foreach (var (a, b) in new[] { (7L, 3L), (-7L, 3L), (7L, -3L), (-7L, -3L), (100L, 7L), (-100L, 7L) })
        Eq(NewrRt.Tdiv(a, b) * b + NewrRt.Tmod(a, b), a, $"invariant {a}/{b}");
});

// ---------------------------------------------------------------- pow
Check("powInt ok", () =>
{
    Eq(NewrRt.PowInt(2, 10), 1024L, "2^10");
    Eq(NewrRt.PowInt(2, 0), 1L, "2^0");
    Eq(NewrRt.PowInt(0, 0), 1L, "0^0");
    Eq(NewrRt.PowInt(0, 5), 0L, "0^5");
    Eq(NewrRt.PowInt(-2, 3), -8L, "(-2)^3");
    Eq(NewrRt.PowInt(-2, 2), 4L, "(-2)^2");
});
Check("powInt overflow", () => { ThrowsNamed(() => NewrRt.PowInt(2, 63), "NewrOverflowError"); });
Check("powInt negative exponent", () => { ThrowsCode(() => NewrRt.PowInt(2, -1), null); });
Check("powInt huge exponent does not hang", () =>
{
    var t0 = DateTime.UtcNow;
    ThrowsNamed(() => NewrRt.PowInt(10, 1_000_000_000L), "NewrOverflowError");
    Ok((DateTime.UtcNow - t0).TotalMilliseconds < 5000, "powInt(10, 1e9) took too long");
});
Check("powFloat plain", () =>
{
    Eq(NewrRt.PowFloat(2.0, 3.0), 8.0, "2.0^3");
    Eq(NewrRt.PowFloat(9.0, 0.5), 3.0, "9.0^0.5");
});
Check("powFloat negative base fractional exponent is NaN", () =>
{
    Ok(double.IsNaN(NewrRt.PowFloat(-8.0, 1.0 / 3.0)), "(-8)^(1/3) is NaN");
});
Check("powFloat overflow is infinity", () =>
{
    Eq(NewrRt.PowFloat(1e308, 2.0), double.PositiveInfinity, "1e308^2");
    Eq(NewrRt.PowFloat(-1e308, 3.0), double.NegativeInfinity, "(-1e308)^3");
});
Check("powFloat zero base negative exponent throws", () =>
{
    ThrowsCode(() => NewrRt.PowFloat(0, -1), "ZeroDivisionError");
});
Check("fmod dividend sign", () =>
{
    Eq(NewrRt.Fmod(-7.0, 2.0), -1.0, "fmod(-7,2)");
    Eq(NewrRt.Fmod(7.0, -2.0), 1.0, "fmod(7,-2)");
});
Check("fmod by zero", () => { ThrowsNamed(() => NewrRt.Fmod(1.0, 0.0), "NewrDivisionByZeroError"); });
Check("fdiv", () =>
{
    Eq(NewrRt.Fdiv(7.0, 2.0), 3.5, "7.0/2.0");
    Eq(NewrRt.Fdiv(-7.0, 2.0), -3.5, "-7.0/2.0");
    Eq(NewrRt.Fdiv(1.0, 3.0), 1.0 / 3.0, "1.0/3.0");
});
Check("fdiv by zero", () =>
{
    ThrowsCode(() => NewrRt.Fdiv(1.0, 0.0), "ZeroDivisionError");
    ThrowsCode(() => NewrRt.Fdiv(0.0, 0.0), "ZeroDivisionError");
    ThrowsCode(() => NewrRt.Fdiv(1.0, -0.0), "ZeroDivisionError");
});

// ---------------------------------------------------------------- round (exact value)
Check("round ties go away from zero", () =>
{
    Eq(NewrRt.RoundHalfAway(2.5), 3L, "round 2.5");
    Eq(NewrRt.RoundHalfAway(-2.5), -3L, "round -2.5");
    Eq(NewrRt.RoundHalfAway(3.5), 4L, "round 3.5");
    Eq(NewrRt.RoundHalfAway(-3.5), -4L, "round -3.5");
});
Check("round plain values", () =>
{
    Eq(NewrRt.RoundHalfAway(2.4), 2L, "round 2.4");
    Eq(NewrRt.RoundHalfAway(2.6), 3L, "round 2.6");
    Eq(NewrRt.RoundHalfAway(0.5), 1L, "round 0.5");
});
Check("round rounds the exact float value", () =>
{
    Eq(NewrRt.RoundHalfAway(2.675, 2), 2.67, "round 2.675,2");
    Eq(NewrRt.RoundHalfAway(1.005, 2), 1.0, "round 1.005,2");
    Eq(NewrRt.RoundHalfAway(0.125, 2), 0.13, "round 0.125,2");
    Eq(NewrRt.RoundHalfAway(9223372036854774784.0), 9223372036854774784L, "round huge integral float");
});
Check("round ndigits returns float", () =>
{
    Eq(NewrRt.RoundHalfAway(123.456, 1), 123.5, "round 123.456,1");
    Eq(NewrRt.RoundHalfAway(999.999, 2), 1000.0, "round 999.999,2");
});
Check("round negative ndigits", () =>
{
    Eq(NewrRt.RoundHalfAway(5, -1), 10.0, "round 5,-1");
    Eq(NewrRt.RoundHalfAway(15, -1), 20.0, "round 15,-1");
    Eq(NewrRt.RoundHalfAway(1250, -2), 1300.0, "round 1250,-2");
});
Check("round overflow", () => { ThrowsNamed(() => NewrRt.RoundHalfAway(1e30), "NewrOverflowError"); });
Check("round non-finite", () =>
{
    ThrowsCode(() => NewrRt.RoundHalfAway(double.PositiveInfinity), "OverflowError");
    Eq(NewrRt.RoundHalfAway(double.PositiveInfinity, 2), double.PositiveInfinity, "round inf,2");
    Ok(double.IsNaN(NewrRt.RoundHalfAway(double.NaN, 2)), "round nan,2");
});
Check("round negative zero", () =>
{
    Eq(BitConverter.DoubleToInt64Bits(NewrRt.RoundHalfAway(-0.001, 2)),
        BitConverter.DoubleToInt64Bits(-0.0), "round -0.001,2 is -0.0");
});

// ---------------------------------------------------------------- float rendering (J4)
Check("floatStr matches Python repr", () =>
{
    var cases = new (double v, string want)[]
    {
        (0.0, "0.0"), (-0.0, "-0.0"), (1.0, "1.0"), (-1.5, "-1.5"),
        (1e15, "1000000000000000.0"), (1e16, "1e+16"), (1e20, "1e+20"),
        (1e21, "1e+21"), (0.0001, "0.0001"), (0.00001, "1e-05"),
        (1.23456789e-10, "1.23456789e-10"), (123456.789, "123456.789"),
        (2.675, "2.675"), (0.1, "0.1"), (0.30000000000000004, "0.30000000000000004"),
        (1e-7, "1e-07"), (5e-324, "5e-324"),
        (1.7976931348623157e308, "1.7976931348623157e+308"),
        (3.141592653589793, "3.141592653589793"), (100.0, "100.0"),
        (2.5, "2.5"), (-2.5e-5, "-2.5e-05"),
        (123456789.12345679, "123456789.12345679"),
        (9.223372036854776e18, "9.223372036854776e+18"),
        (1234560000.0, "1234560000.0"),
    };
    foreach (var (v, want) in cases)
        Eq(NewrRt.FloatStr(v), want, $"floatStr({v})");
    Eq(NewrRt.FloatStr(double.NaN), "nan", "floatStr nan");
    Eq(NewrRt.FloatStr(double.PositiveInfinity), "inf", "floatStr inf");
    Eq(NewrRt.FloatStr(double.NegativeInfinity), "-inf", "floatStr -inf");
});

// ---------------------------------------------------------------- conversions
Check("toInt from str", () =>
{
    Eq(NewrRt.ToInt("42"), 42L, "toInt 42");
    Eq(NewrRt.ToInt("  -42 "), -42L, "toInt -42");
    Eq(NewrRt.ToInt("+7"), 7L, "toInt +7");
    ThrowsCode(() => NewrRt.ToInt("3.5"), "ValueError");
    ThrowsCode(() => NewrRt.ToInt("abc"), "ValueError");
});
Check("toInt range checked", () =>
{
    ThrowsNamed(() => NewrRt.ToInt("99999999999999999999"), "NewrOverflowError");
});
Check("toInt from float truncates", () =>
{
    Eq(NewrRt.ToInt(3.7), 3L, "toInt 3.7");
    Eq(NewrRt.ToInt(-3.7), -3L, "toInt -3.7");
    ThrowsCode(() => NewrRt.ToInt(double.PositiveInfinity), "OverflowError");
});
Check("toInt from bool", () =>
{
    Eq(NewrRt.ToInt(true), 1L, "toInt true");
    Eq(NewrRt.ToInt(false), 0L, "toInt false");
});
Check("toFloat", () =>
{
    Eq(NewrRt.ToFloat(2L), 2.0, "toFloat 2");
    Eq(NewrRt.ToFloat("inf"), double.PositiveInfinity, "toFloat inf");
    Eq(NewrRt.ToFloat("-inf"), double.NegativeInfinity, "toFloat -inf");
    Ok(double.IsNaN(NewrRt.ToFloat("nan")), "toFloat nan");
    Eq(NewrRt.ToFloat("1_000"), 1000.0, "toFloat 1_000");
    ThrowsCode(() => NewrRt.ToFloat("abc"), "ValueError");
    ThrowsCode(() => NewrRt.ToFloat(BigInteger.Pow(10, 400)), "OverflowError");
});
Check("toBool parses", () =>
{
    Eq(NewrRt.ToBool("true"), true, "toBool true");
    Eq(NewrRt.ToBool("false"), false, "toBool false");
    ThrowsCode(() => NewrRt.ToBool("yes"), "ValueError");
    Eq(NewrRt.ToBool(0L), false, "toBool 0");
    Eq(NewrRt.ToBool(1L), true, "toBool 1");
    Eq(NewrRt.ToBool(0.0), false, "toBool 0.0");
});

// ---------------------------------------------------------------- toStr
Check("toStr scalars", () =>
{
    Eq(NewrRt.ToStr(null), "none", "toStr null");
    Eq(NewrRt.ToStr(true), "true", "toStr true");
    Eq(NewrRt.ToStr(false), "false", "toStr false");
    Eq(NewrRt.ToStr(42L), "42", "toStr 42");
    Eq(NewrRt.ToStr(1.5), "1.5", "toStr 1.5");
    Eq(NewrRt.ToStr(1.0), "1.0", "toStr 1.0");
    Eq(NewrRt.ToStr("a"), "a", "toStr a");
});
Check("toStr collections", () =>
{
    Eq(NewrRt.ToStr(new List<long> { 1, 2, 3 }), "[1, 2, 3]", "toStr list");
    Eq(NewrRt.ToStr(new List<bool> { true, false }), "[true, false]", "toStr bools");
    Eq(NewrRt.ToStr(new Dictionary<string, long> { ["k"] = 1, ["j"] = 2 }), "{k: 1, j: 2}", "toStr dict");
    Eq(NewrRt.ToStr(new HashSet<string> { "a" }), "{a}", "toStr set");
    Eq(NewrRt.ToStr(new List<long>()), "[]", "toStr empty list");
});
Check("toStr error and result", () =>
{
    Eq(NewrRt.ToStr(new NewrRt.NewrError("boom")), "boom", "toStr error");
    Eq(NewrRt.ToStr(NewrRt.Ok(3L)), "ok(3)", "toStr ok");
    Eq(NewrRt.ToStr(NewrRt.Err<long>(new NewrRt.NewrError("bad"))), "err(bad)", "toStr err");
});
Check("toStr struct-like values", () =>
{
    var p = new PointLike();
    Eq(NewrRt.ToStr(p), "Point(x: 1, y: 2)", "toStr struct-like");
});

// ---------------------------------------------------------------- Result
Check("Result ok/err", () =>
{
    var r = NewrRt.Ok(7L);
    Eq(r.IsOk(), true, "ok isOk");
    Eq(r.IsErr(), false, "ok isErr");
    Eq(r.Unwrap(), 7L, "ok unwrap");
    Eq(r.UnwrapOr(0L), 7L, "ok unwrapOr");
    var e = NewrRt.Err<long>(new NewrRt.NewrError("bad"));
    Eq(e.IsErr(), true, "err isErr");
    Eq(e.UnwrapOr(9L), 9L, "err unwrapOr");
    ThrowsCode(() => e.Unwrap(), null);
});
Check("tryResult", () =>
{
    var r1 = NewrRt.TryResult(() => 6L);
    Eq(r1.IsOk(), true, "try ok");
    Eq(r1.Unwrap(), 6L, "try unwrap");
    var r2 = NewrRt.TryResult<long>(() => throw new Exception("x"));
    Eq(r2.IsErr(), true, "try err");
    Eq(r2._error.Message, "x", "try err message");
});

// ---------------------------------------------------------------- equality
Check("eq primitives and collections", () =>
{
    Eq(NewrRt.Eq(1L, 1L), true, "eq 1,1");
    Eq(NewrRt.Eq(1L, 2L), false, "eq 1,2");
    Eq(NewrRt.Eq(1L, 1), false, "eq Long,Int");
    Eq(NewrRt.Eq("a", "a"), true, "eq str");
    Eq(NewrRt.Eq(null, null), true, "eq null,null");
    Eq(NewrRt.Eq(null, 0L), false, "eq null,0");
    Eq(NewrRt.Eq(new List<long> { 1, 2 }, new List<long> { 1, 2 }), true, "eq lists");
    Eq(NewrRt.Eq(new List<long> { 1 }, new List<long> { 1, 2 }), false, "eq lists len");
    Eq(NewrRt.Eq(new Dictionary<string, long> { ["a"] = 1 }, new Dictionary<string, long> { ["a"] = 1 }), true, "eq dicts");
    Eq(NewrRt.Eq(new Dictionary<string, long> { ["a"] = 1 }, new Dictionary<string, long> { ["a"] = 2 }), false, "eq dicts differ");
    Eq(NewrRt.Eq(new HashSet<long> { 1, 2 }, new HashSet<long> { 2, 1 }), true, "eq sets");
    Eq(NewrRt.Eq(NewrRt.Ok(1L), NewrRt.Ok(1L)), true, "eq ok");
    Eq(NewrRt.Eq(NewrRt.Ok(1L), NewrRt.Err<long>(new NewrRt.NewrError("x"))), false, "eq ok vs err");
    Eq(NewrRt.Eq(double.NaN, double.NaN), false, "eq NaN");
});

// ---------------------------------------------------------------- ?. / ?? / accessors
Check("optChain and coalesce", () =>
{
    Eq(NewrRt.OptChainVal<long, long>((long?)null, v => v + 1), null, "optChain null");
    Eq(NewrRt.OptChainVal<long, long>((long?)5, v => v + 1), 6L, "optChain 5");
    Eq(NewrRt.CoalesceVal(0L, () => 5L), 0L, "coalesce 0");
    Eq(NewrRt.CoalesceVal((long?)null, () => 5L), 5L, "coalesce null");
    Eq(NewrRt.Coalesce("", () => "d"), "", "coalesce empty str");
});
Check("listIndex", () =>
{
    Eq(NewrRt.ListIndex(new List<long> { 1, 2 }, 1), 2L, "listIndex 1");
    ThrowsCode(() => NewrRt.ListIndex(new List<long> { 1 }, -1), "IndexError");
    ThrowsCode(() => NewrRt.ListIndex(new List<long> { 1 }, 1), "IndexError");
});
Check("listGet", () =>
{
    Eq(NewrRt.ListGet(new List<long> { 1, 2 }, 0, 9L), 1L, "listGet 0");
    Eq(NewrRt.ListGet(new List<long> { 1, 2 }, 5, 9L), 9L, "listGet 5");
    Eq(NewrRt.ListGet(new List<long> { 1, 2 }, -1, 9L), 9L, "listGet -1");
});
Check("listContains structural", () =>
{
    Eq(NewrRt.ListContains(new List<long> { 1, 2 }, 2L), true, "contains 2");
    Eq(NewrRt.ListContains(new List<long> { 1, 2 }, 9L), false, "contains 9");
    Eq(NewrRt.ListContains(
        new List<List<long>> { new List<long> { 1 } }, new List<long> { 1 }),
        true, "contains nested");
});
Check("setRemove", () =>
{
    var s = new HashSet<long> { 1, 2 };
    NewrRt.SetRemove(s, 1L);
    Eq(s.Contains(1L), false, "setRemove removed");
    ThrowsCode(() => NewrRt.SetRemove(s, 99L), "KeyError");
});
Check("unwrapOpt", () =>
{
    Eq(NewrRt.UnwrapOptVal<long>(5L), 5L, "unwrapOpt 5");
    ThrowsCode(() => NewrRt.UnwrapOptVal((long?)null), null);
});
Check("range", () =>
{
    Eq(NewrRt.Range(3), new List<long> { 0, 1, 2 }, "range 3");
    Eq(NewrRt.Range(1, 4), new List<long> { 1, 2, 3 }, "range 1,4");
    Eq(NewrRt.Range(0, 10, 3), new List<long> { 0, 3, 6, 9 }, "range 0,10,3");
    Eq(NewrRt.Range(5, 0, -2), new List<long> { 5, 3, 1 }, "range 5,0,-2");
    ThrowsCode(() => NewrRt.Range(0, 5, 0), "ValueError");
});

// ---------------------------------------------------------------- file ops
Check("file read/write/append", () =>
{
    var d = Directory.CreateDirectory(Path.Combine(Path.GetTempPath(), "newr-rt-" + Guid.NewGuid().ToString("N")));
    var p = Path.Combine(d.FullName, "t.txt");
    NewrRt.FileWrite(p, "hello");
    Eq(NewrRt.FileExists(p), true, "file exists");
    Eq(NewrRt.FileRead(p), "hello", "file read");
    NewrRt.FileAppend(p, " world");
    Eq(NewrRt.FileRead(p), "hello world", "file append");
    Eq(NewrRt.FileExists(Path.Combine(d.FullName, "nope")), false, "file missing");
    Directory.Delete(d.FullName, true);
});
Check("file read missing raises", () =>
{
    // Parent directory exists, file does not: .NET raises
    // FileNotFoundException (a missing parent would raise
    // DirectoryNotFoundException instead).
    var missing = Path.Combine(Path.GetTempPath(), "definitely-not-here-xyz.txt");
    try
    {
        NewrRt.FileRead(missing);
        throw new Exception("expected a throw");
    }
    catch (NewrRt.NewrError e)
    {
        // Host-specific code (C#: FileNotFoundException;
        // Python: FileNotFoundError; JS: ENOENT).
        Eq((object?)e.Code, "FileNotFoundException", "missing file code");
    }
});

// ---------------------------------------------------------------- time
Check("timeFormat UTC", () =>
{
    Eq(NewrRt.TimeFormat(1700000000L, "%Y-%m-%d"), "2023-11-14", "fmt date");
    Eq(NewrRt.TimeFormat(0L, "%H:%M:%S"), "00:00:00", "fmt time");
    Eq(NewrRt.TimeFormat(1700000000L, "%d/%m/%Y"), "14/11/2023", "fmt dmy");
    ThrowsCode(() => NewrRt.TimeFormat(0L, "%c"), null);
});

// ---------------------------------------------------------------- random
Check("randomInt in range", () =>
{
    for (int i = 0; i < 50; i++)
    {
        long r = NewrRt.RandomInt(1, 10);
        Ok(r >= 1 && r <= 10, "randomInt out of range: " + r);
    }
    Eq(NewrRt.RandomInt(5, 5), 5L, "randomInt 5,5");
    long big = NewrRt.RandomInt(0, long.MaxValue);
    Ok(big >= 0, "randomInt wide range");
});
Check("randomChoice", () =>
{
    var xs = new List<long> { 1, 2, 3 };
    Ok(xs.Contains(NewrRt.RandomChoice(xs)), "randomChoice in list");
    ThrowsCode(() => NewrRt.RandomChoice(new List<long>()), "IndexError");
});
Check("randomShuffle", () =>
{
    var xs = new List<long> { 1, 2, 3, 4, 5 };
    NewrRt.RandomShuffle(xs);
    var sorted = new List<long>(xs);
    sorted.Sort();
    Eq(sorted, new List<long> { 1, 2, 3, 4, 5 }, "shuffle keeps elements");
});

// ---------------------------------------------------------------- asError
Check("asError", () =>
{
    var n = new NewrRt.NewrError("x", "E1");
    Eq((object)NewrRt.AsError(n), (object)n, "asError passthrough");
    Eq(NewrRt.AsError("boom").Message, "boom", "asError string");
    var wrapped = NewrRt.AsError(new InvalidOperationException("nope"));
    Eq(wrapped.Message, "nope", "asError message");
    Eq((object?)wrapped.Code, "InvalidOperationException", "asError code");
    Eq(NewrRt.AsError(42).Message, "42", "asError int");
});

// ---------------------------------------------------------------- strings (helpers added for the C# emitter, M4)
// Ported from compiler/newr_rt.java; the Java driver has the same
// 10 checks, so the two runtimes stay aligned.
Check("chars code points", () =>
{
    Eq(NewrRt.Chars("a😀b"), new List<string> { "a", "😀", "b" }, "chars astral");
    Eq(NewrRt.Chars(""), new List<string>(), "chars empty");
    Eq(NewrRt.Chars("abc"), new List<string> { "a", "b", "c" }, "chars ascii");
});
Check("strCmp code points", () =>
{
    Eq(NewrRt.StrCmp("😀", "�") > 0, true, "strCmp astral gt");
    Eq(NewrRt.StrCmp("�", "😀") < 0, true, "strCmp astral lt");
    Eq(NewrRt.StrCmp("a", "a"), 0, "strCmp equal");
    Eq(NewrRt.StrCmp("ab", "a") > 0, true, "strCmp prefix longer");
    Eq(NewrRt.StrCmp("a", "ab") < 0, true, "strCmp prefix shorter");
    Eq(NewrRt.StrCmp("", ""), 0, "strCmp empty");
});
Check("splitStr literal separator", () =>
{
    Eq(NewrRt.SplitStr("a,b,c", ","), new List<string> { "a", "b", "c" }, "split basic");
    Eq(NewrRt.SplitStr("a,,b", ","), new List<string> { "a", "", "b" }, "split empty middle");
    Eq(NewrRt.SplitStr("a,", ","), new List<string> { "a", "" }, "split trailing empty kept");
    Eq(NewrRt.SplitStr("a.b", "."), new List<string> { "a", "b" }, "split literal dot, not regex");
    Eq(NewrRt.SplitStr("abc", ","), new List<string> { "abc" }, "split no match");
});
Check("strIndex code points", () =>
{
    Eq(NewrRt.StrIndex("hello", 1), "e", "strIndex 1");
    Eq(NewrRt.StrIndex("hello", -1), "o", "strIndex -1");
    Eq(NewrRt.StrIndex("a😀b", 1), "😀", "strIndex astral");
    ThrowsCode(() => NewrRt.StrIndex("ab", 2), "IndexError");
    ThrowsCode(() => NewrRt.StrIndex("ab", -3), "IndexError");
});
Check("absLong", () =>
{
    Eq(NewrRt.AbsLong(5), 5L, "abs 5");
    Eq(NewrRt.AbsLong(-5), 5L, "abs -5");
    Eq(NewrRt.AbsLong(0), 0L, "abs 0");
    ThrowsCode(() => NewrRt.AbsLong(long.MinValue), null);
});
Check("lenOf", () =>
{
    Eq(NewrRt.LenOf("a😀b"), 3L, "len str code points");
    Eq(NewrRt.LenOf(new List<long> { 1, 2 }), 2L, "len list");
    Eq(NewrRt.LenOf(new Dictionary<string, long> { ["a"] = 1 }), 1L, "len dict");
    Eq(NewrRt.LenOf(new HashSet<long> { 1, 2, 3 }), 3L, "len set");
});
Check("dictOf", () =>
{
    var d = NewrRt.DictOf<long>("a", 1L, "b", 2L);
    Eq(d["a"], 1L, "dictOf a");
    Eq(d.Count, 2, "dictOf size");
    Eq(new List<string>(d.Keys), new List<string> { "a", "b" }, "dictOf insertion order");
    var withNull = NewrRt.DictOf<long?>("a", null);
    Ok(withNull["a"] is null, "dictOf null value allowed");
});
Check("envGet", () =>
{
    Eq((object?)NewrRt.EnvGet("NEWR_RT_CHECK_ABSENT_XYZ"), null, "envGet absent");
    Eq(NewrRt.EnvGet("NEWR_RT_CHECK_ABSENT_XYZ", "dflt"), "dflt", "envGet default");
    Ok(NewrRt.EnvGet("PATH") is not null, "envGet PATH present");
});
Check("envArgs", () =>
{
    Eq(NewrRt.EnvArgs(), new List<string>(), "envArgs default empty");
    NewrRt._mainArgs = new[] { "a", "b" };
    Eq(NewrRt.EnvArgs(), new List<string> { "a", "b" }, "envArgs set");
    // SetMainArgs prepends the program name as argv[0] (newr's
    // env.args is sys.argv 1:1; .NET's Main args exclude it).
    NewrRt.SetMainArgs("prog", new string[] { "a", "b" });
    Eq(NewrRt.EnvArgs(), new List<string> { "prog", "a", "b" }, "envArgs argv0");
    NewrRt._mainArgs = Array.Empty<string>();
});
Check("hashOf", () =>
{
    Eq(NewrRt.HashOf(0.0), NewrRt.HashOf(-0.0), "hashOf -0.0 == 0.0");
    Eq(NewrRt.HashOf(null), 0, "hashOf null");
    Eq(NewrRt.HashOf("x"), NewrRt.HashOf("x"), "hashOf string stable");
    Eq(NewrRt.HashOf(new List<object?> { 1L, "a" }),
       NewrRt.HashOf(new List<object?> { 1L, "a" }), "hashOf list by elements");
});
Check("structHash", () =>
{
    Eq(NewrRt.StructHash(1L, "a"), NewrRt.StructHash(1L, "a"), "structHash stable");
    Eq(NewrRt.StructHash(-0.0), NewrRt.StructHash(0.0), "structHash -0.0 == 0.0");
});

Check("optChain", () =>
{
    Eq(NewrRt.OptChain<long, string>((long?)null, v => "x"), null, "optChain null value recv");
    Eq(NewrRt.OptChain<long, string>((long?)5, v => "x"), "x", "optChain value recv ref result");
    Eq(NewrRt.OptChain<string, string>((string)null, v => v + "!"), null, "optChain null ref recv");
    Eq(NewrRt.OptChain<string, string>("hi", v => v + "!"), "hi!", "optChain ref recv");
});
Check("optChainVal", () =>
{
    Eq(NewrRt.OptChainVal<long, long>((long?)null, v => v + 1), null, "optChainVal null");
    Eq(NewrRt.OptChainVal<long, long>((long?)5, v => v + 1), 6L, "optChainVal value");
    Eq(NewrRt.OptChainVal<string, long>("hi", v => (long)v.Length), 2L, "optChainVal ref recv");
});
Check("optChainNullable", () =>
{
    Eq(NewrRt.OptChainNullable<long, long>((long?)null, v => (long?)(v + 1)), null, "optChainNullable null recv");
    Eq(NewrRt.OptChainNullable<long, long>((long?)5, v => (long?)(v + 1)), 6L, "optChainNullable value");
    Eq(NewrRt.OptChainNullable<long, long>((long?)5, v => null), null, "optChainNullable null result");
    Eq(NewrRt.OptChainNullable<string, long>("hi", v => (long?)2), 2L, "optChainNullable ref recv");
});
Check("coalesceVal", () =>
{
    Eq(NewrRt.CoalesceVal<long>((long?)null, () => 7L), 7L, "coalesceVal null");
    Eq(NewrRt.CoalesceVal<long>((long?)3, () => 7L), 3L, "coalesceVal value");
    Eq(NewrRt.CoalesceVal<bool>((bool?)null, () => true), true, "coalesceVal bool");
});

Console.WriteLine($"\nRtChecks.cs: {passed} checks passed");

class PointLike
{
    public override string ToString() => "Point(x: 1, y: 2)";
}
