// The newr C# runtime (M4).
#nullable enable annotations
//
// This file is the runtime half of every emitted C# program that
// needs it: checked 64-bit integer arithmetic (long + checked{}),
// truncating division, half-away rounding on the EXACT float value,
// the newr conversion/rendering rules, `Result`, and the `?.` / `??`
// helpers. It is stdlib-only (System.*) and dependency-free.
//
// It lives in the compiler package as `compiler/newr_rt.cs` and
// `newr build --target cs` copies it - byte-identical - next to an
// emitted `.cs` file when (and only when) that program uses a helper
// from it. Do not edit a copied file by hand: edit this file in the
// newr repo and rebuild.
//
// Semantics are pinned in BUILD_REPORT.md §21 (K2-K6, mirroring the
// M2 pins P1-P12 and the M3 pins J1-J13). Every rule here is
// unit-tested in tests/cs/RtChecks.cs (run by
// tests/test_newr_rt_cs.py).
//
// The runtime is a single public static class `NewrRt` in the
// global namespace, so an emitted file runs with plain
// `dotnet run` on its generated project and no extra packages.
// Emitted code calls helpers as `NewrRt.Add(a, b)`; error classes
// are `NewrRt.NewrError` and friends.

using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Numerics;
using System.Security.Cryptography;
using System.Text;
using System.Text.RegularExpressions;

/// <summary>Implemented by every value whose newr display
/// spelling is not its <c>ToString</c> default: Results, and
/// every struct/enum the compiler emits (which override
/// <c>ToString</c> to return the same spelling).</summary>
/// <remarks>Top-level (not nested in <c>NewrRt</c>): the
/// emitted structs/enums live in the global namespace and
/// implement it as <c>INewrRepr</c>.</remarks>
public interface INewrRepr
{
    string NewrRepr();
}

public static class NewrRt
{
    // ---------------------------------------------------------------- int64

    public const long Int64Min = long.MinValue;
    public const long Int64Max = long.MaxValue;

    private static readonly BigInteger Int64MinBi = new BigInteger(long.MinValue);
    private static readonly BigInteger Int64MaxBi = new BigInteger(long.MaxValue);
    // Largest magnitude whose double conversion is finite in the
    // Python sense: float(2**1024 - 2**971) is finite, one more is
    // inf (mirrors the JS _MAX_FLOAT_BIGINT).
    private static readonly BigInteger MaxFloatBigInt =
        (BigInteger.One << 1024) - (BigInteger.One << 971);

    /// <summary>Return <paramref name="v"/> if it fits in signed 64-bit, else raise.</summary>
    public static long CheckInt(long v) => v;

    /// <summary>Return <paramref name="v"/> if it fits in signed 64-bit, else raise.</summary>
    public static long CheckInt(BigInteger v)
    {
        if (v < Int64MinBi || v > Int64MaxBi)
            throw new NewrOverflowError(
                $"integer overflow: {v} is outside int64 range");
        return (long)v;
    }

    // ---------------------------------------------------------------- errors

    /// <summary>Base of every newr <c>error</c> value. Carries the
    /// error payload. <c>Code</c> is null unless set (a caught host
    /// error keeps its own code via AsError).</summary>
    public class NewrError : Exception
    {
        public string? Code { get; }
        public object? Payload { get; }

        public NewrError(string message) : this(message, null, null) { }

        public NewrError(string message, string? code) : this(message, code, null) { }

        public NewrError(string message, string? code, object? payload)
            : base(message)
        {
            Code = code;
            Payload = payload;
        }
    }

    /// <summary>Checked-int overflow. The Python target's
    /// NewrOverflowError is a NewrError subclass constructed WITHOUT
    /// a code, so a newr <c>catch e:</c> sees e.code == none (the
    /// class is told by name, not by code). The builtin
    /// OverflowError (round(inf), int(inf), float(huge int)) is a
    /// different site: it throws CodedError("OverflowError", ...)
    /// instead.</summary>
    public class NewrOverflowError : NewrError
    {
        public NewrOverflowError(string message) : base(message) { }
    }

    /// <summary>Division or modulo by zero. Same code rule as
    /// above: the Python twin is constructed without a code, so
    /// e.code is none in a <c>catch</c>. The builtin sites -
    /// <c>0.0 ** -1</c>, where Python raises ZeroDivisionError
    /// itself, and float <c>/</c> by zero, where Python's native
    /// <c>/</c> raises it - throw
    /// CodedError("ZeroDivisionError", ...) in PowFloat and Fdiv
    /// instead (§18 fix 2: fdiv was missed by the §17.4 item-4 list).</summary>
    public class NewrDivisionByZeroError : NewrError
    {
        public NewrDivisionByZeroError(string message) : base(message) { }
    }

    /// <summary>An error with a host-style code (IndexError,
    /// KeyError, ValueError): the Python target raises the builtin
    /// of that name, so a newr <c>catch e:</c> sees the same code on
    /// both targets.</summary>
    public static NewrError CodedError(string code, string message) =>
        new NewrError(message, code);

    /// <summary>Coerce whatever a <c>catch</c> caught into a newr
    /// <c>error</c> value. A <c>NewrError</c> passes through
    /// unchanged; a plain Exception keeps its message and gains its
    /// class name as the code (a FileNotFoundException, an
    /// InvalidOperationException); a string becomes an error with
    /// that message. This is what makes <c>.message</c> /
    /// <c>.code</c> total inside every <c>catch</c> body
    /// (SPEC §10).</summary>
    public static NewrError AsError(object? e)
    {
        if (e is NewrError n) return n;
        if (e is string s) return new NewrError(s);
        if (e is Exception ex) return new NewrError(ex.Message, ex.GetType().Name);
        return new NewrError(e?.ToString() ?? "none");
    }

    // ---------------------------------------------------------------- checked int arithmetic

    public static long Add(long a, long b)
    {
        try { return checked(a + b); }
        catch (OverflowException)
        {
            throw new NewrOverflowError($"integer overflow: "
                + ((BigInteger)a + b) + " is outside int64 range");
        }
    }

    public static long Sub(long a, long b)
    {
        try { return checked(a - b); }
        catch (OverflowException)
        {
            throw new NewrOverflowError($"integer overflow: "
                + ((BigInteger)a - b) + " is outside int64 range");
        }
    }

    public static long Mul(long a, long b)
    {
        try { return checked(a * b); }
        catch (OverflowException)
        {
            throw new NewrOverflowError($"integer overflow: "
                + ((BigInteger)a * b) + " is outside int64 range");
        }
    }

    public static long Neg(long a)
    {
        try { return checked(-a); }
        catch (OverflowException)
        {
            throw new NewrOverflowError($"integer overflow: "
                + (-(BigInteger)a) + " is outside int64 range");
        }
    }

    public static long PowInt(long a, long b)
    {
        // newr `**` on ints: checked, bounded before computing.
        // A negative exponent is an error (an int result cannot
        // represent it). The exponent is bounded by repeated
        // squaring with a check at every multiply - `10 ** 10**9`
        // raises instead of allocating by computing blindly.
        if (b < 0) throw new NewrError("negative exponent for int pow");
        if (a == 0) return b == 0 ? 1 : 0;
        if (a == 1) return 1;
        if (a == -1) return (b % 2 == 1) ? -1 : 1;
        long result = 1;
        long bas = a;
        long exp = b;
        while (exp > 0)
        {
            if (exp % 2 == 1) result = Mul(result, bas);
            exp /= 2;
            if (exp > 0) bas = Mul(bas, bas);
        }
        return result;
    }

    public static long Tdiv(long a, long b)
    {
        // newr `/` on ints: truncating division (toward zero).
        // C# `/` already truncates - the check is the two error cases.
        if (b == 0) throw new NewrDivisionByZeroError("integer division by zero");
        if (a == long.MinValue && b == -1)
            throw new NewrOverflowError("integer overflow: INT64_MIN / -1");
        return a / b;
    }

    public static long Tmod(long a, long b)
    {
        // newr `%` on ints: remainder with the dividend's sign.
        // C# `%` already takes the dividend's sign.
        if (b == 0) throw new NewrDivisionByZeroError("integer modulo by zero");
        if (a == long.MinValue && b == -1) return 0; // mathematically 0
        return a % b;
    }

    public static double PowFloat(double a, double b)
    {
        // newr `**` on floats: IEEE semantics. C# `Math.Pow`
        // already returns NaN for a negative base with a fractional
        // exponent (Python returns a complex there - newr has no
        // complex type, so the answer is NaN) and Infinity on
        // overflow. The one case C# gets wrong is a zero base with
        // a negative exponent: C# gives Infinity, Python raises
        // ZeroDivisionError.
        if (a == 0 && b < 0)
        {
            // Python raises the builtin ZeroDivisionError here (not
            // the NewrError subclass), so a `catch e:` sees code
            // "ZeroDivisionError" - CodedError, not the class.
            throw CodedError("ZeroDivisionError",
                "0.0 cannot be raised to a negative power");
        }
        return Math.Pow(a, b);
    }

    public static double Fmod(double a, double b)
    {
        // newr `%` on floats: C `fmod` semantics (dividend's
        // sign). C# `%` already takes the dividend's sign (unlike
        // Python's `%`, which takes the divisor's). Division by
        // zero raises like the int case (C# would give NaN).
        if (b == 0) throw new NewrDivisionByZeroError("float modulo by zero");
        return a % b;
    }

    public static double Fdiv(double a, double b)
    {
        // newr `/` on floats: IEEE division, except division by
        // zero raises like Python (C# would give Infinity for a
        // non-zero dividend and NaN for 0/0). `b == 0` covers -0
        // as well. Python's native `/` raises the BUILTIN
        // ZeroDivisionError, so this throws the coded error
        // (§18 fix 2), not the codeless class - a `catch e:` sees
        // e.code == "ZeroDivisionError" on both targets. (Float `%`
        // is different: both targets route it through Fmod, which
        // raises the codeless NewrDivisionByZeroError - that parity
        // is correct.)
        if (b == 0) throw CodedError("ZeroDivisionError", "float division by zero");
        return a / b;
    }

    // ---------------------------------------------------------------- rounding (P2 / J3)

    /// <summary>Exact decimal expansion of a finite double: value =
    /// sign * digits / 10**scale, digits a non-negative BigInteger.
    /// Mirrors the JS _exactDecimal (same bit decomposition).</summary>
    private readonly struct ExactDecimal
    {
        public readonly long Sign; // -1 or 1
        public readonly BigInteger Digits;
        public readonly int Scale;
        public ExactDecimal(long sign, BigInteger digits, int scale)
        {
            Sign = sign; Digits = digits; Scale = scale;
        }
    }

    private static ExactDecimal ExactDecimalOf(double x)
    {
        long bits = BitConverter.DoubleToInt64Bits(x);
        long sign = bits < 0 ? -1 : 1;
        int expBits = (int)((bits >> 52) & 0x7FFL);
        long frac = bits & ((1L << 52) - 1);
        BigInteger m;
        long e;
        if (expBits == 0)
        {
            // Subnormal (or zero): no implicit leading 1.
            m = new BigInteger(frac);
            e = -1074;
        }
        else
        {
            m = new BigInteger(frac) | (BigInteger.One << 52);
            e = expBits - 1075L;
        }
        if (m.Sign == 0) return new ExactDecimal(sign, BigInteger.Zero, 0);
        if (e >= 0) return new ExactDecimal(sign, m << (int)e, 0);
        return new ExactDecimal(sign, m * BigInteger.Pow(5, (int)-e), (int)-e);
    }

    /// <summary>Round the exact decimal (digits / 10**scale,
    /// digits &gt;= 0) to <paramref name="n"/> places after the
    /// point, half away from zero. Returns (rounded, places) with
    /// value = rounded / 10**places.</summary>
    private static (BigInteger Rounded, long Places) RoundDecimal(
        BigInteger digits, long scale, long n)
    {
        if (scale > n)
        {
            BigInteger div = BigInteger.Pow(10, (int)(scale - n));
            BigInteger q = BigInteger.DivRem(digits, div, out BigInteger r);
            if (r * 2 >= div) q += BigInteger.One;
            return (q, n);
        }
        // Already exact at n places: digits / 10**scale, scale <= n.
        return (digits, scale);
    }

    private static string DecimalString(BigInteger digits, long places)
    {
        // `digits` is a non-negative BigInteger holding the value
        // * 10**places. `places` may be negative (round(x, -1)
        // rounds to tens): then the value is digits * 10**(-places).
        string s = digits.ToString();
        if (places == 0) return s;
        if (places < 0) return s + new string('0', (int)-places);
        if (s.Length > places)
            return s.Substring(0, (int)(s.Length - places)) + "."
                + s.Substring((int)(s.Length - places));
        return "0." + new string('0', (int)(places - s.Length)) + s;
    }

    /// <summary>Round the exact decimal (digits / 10**scale) to
    /// <paramref name="n"/> places, half away from zero, as a
    /// double. <paramref name="n"/> below -308 rounds every finite
    /// double to (signed) zero - 10**309 already exceeds the
    /// largest finite double, so no huge power is ever built.</summary>
    private static double RoundExactToDouble(long sign, BigInteger digits,
        long scale, long n)
    {
        if (n < -308) return sign < 0 ? -0.0 : 0.0;
        var (rounded, places) = RoundDecimal(digits, scale, n);
        if (rounded.Sign == 0)
        {
            // Preserve a negative zero: round(-0.001, 2) is -0.0.
            return sign < 0 ? -0.0 : 0.0;
        }
        string text = DecimalString(rounded, places);
        return double.Parse((sign < 0 ? "-" : "") + text,
            CultureInfo.InvariantCulture);
    }

    /// <summary>newr <c>round()</c>: half-away-from-zero
    /// (schoolbook), on the EXACT value of the float - not its
    /// shortest repr. <c>round(2.675, 2)</c> is 2.67, because the
    /// float 2.675 is stored slightly below 2.675. <c>round(x)</c>
    /// returns an <c>int</c> (int64 range-checked);
    /// <c>round(x, n)</c> returns a <c>float</c>. Non-finite input:
    /// <c>round(inf)</c> raises (Python's behaviour);
    /// <c>round(inf, n)</c> returns <c>inf</c>,
    /// <c>round(nan, n)</c> returns <c>nan</c>. An int input rounds
    /// its exact value too (unlike the JS runtime, which returns an
    /// int input unchanged when ndigits is given - <c>round(5,
    /// -1)</c> is 10 in Python, 5 in JS).</summary>
    public static long RoundHalfAway(double x)
    {
        if (!double.IsFinite(x))
        {
            // Python raises the builtin OverflowError here.
            throw CodedError("OverflowError",
                "cannot round infinite or NaN value");
        }
        var d = ExactDecimalOf(x);
        var (rounded, _) = RoundDecimal(d.Digits, d.Scale, 0);
        return CheckInt(d.Sign < 0 ? -rounded : rounded);
    }

    public static double RoundHalfAway(double x, long n)
    {
        if (!double.IsFinite(x)) return x;
        var d = ExactDecimalOf(x);
        return RoundExactToDouble(d.Sign, d.Digits, d.Scale, n);
    }

    public static long RoundHalfAway(long x) => x;

    public static double RoundHalfAway(long x, long n)
    {
        BigInteger digits = BigInteger.Abs(new BigInteger(x));
        return RoundExactToDouble(x < 0 ? -1 : 1, digits, 0, n);
    }

    public static long RoundHalfAway(bool x) => x ? 1 : 0;

    public static double RoundHalfAway(bool x, long n) =>
        RoundHalfAway(x ? 1L : 0L, n);

    // ---------------------------------------------------------------- conversions

    private static string JsonString(string x)
    {
        // JSON-style quoting for error messages (mirrors JS
        // JSON.stringify on a string).
        var sb = new StringBuilder("\"");
        foreach (char c in x)
        {
            switch (c)
            {
                case '"': sb.Append("\\\""); break;
                case '\\': sb.Append("\\\\"); break;
                case '\n': sb.Append("\\n"); break;
                case '\r': sb.Append("\\r"); break;
                case '\t': sb.Append("\\t"); break;
                default:
                    if (c < 0x20) sb.Append($"\\u{(int)c:0000}");
                    else sb.Append(c);
                    break;
            }
        }
        return sb.Append("\"").ToString();
    }

    private static readonly Regex IntString =
        new Regex(@"^[+-]?\d+$", RegexOptions.Compiled);
    private static readonly Regex FloatString =
        new Regex(@"^[+-]?(\d+(\.\d*)?|\.\d+)([eE][+-]?\d+)?$",
            RegexOptions.Compiled);

    /// <summary>newr <c>int(x)</c>: from str (parses; throws on
    /// invalid) or float (truncates toward zero). The result is
    /// int64 range-checked. <c>int("3.5")</c> raises
    /// ValueError.</summary>
    public static long ToInt(object? x)
    {
        if (x is bool b) return b ? 1 : 0;
        if (x is BigInteger bi) return CheckInt(bi);
        if (x is long l) return l;
        if (x is int i) return i;
        if (x is double d)
        {
            if (!double.IsFinite(d))
            {
                // Python raises the builtin OverflowError here.
                throw CodedError("OverflowError",
                    "cannot convert infinite or NaN to int");
            }
            if (d >= 9223372036854775808.0 || d <= -9223372036854775808.0)
                throw new NewrOverflowError(
                    $"integer overflow: {d} is outside int64 range");
            return (long)d;
        }
        if (x is string s)
        {
            string t = s.Trim();
            if (IntString.IsMatch(t)) return CheckInt(BigInteger.Parse(t));
            throw CodedError("ValueError",
                "invalid literal for int(): " + JsonString(s));
        }
        throw new NewrError($"cannot convert {TypeName(x)} to int");
    }

    /// <summary>newr <c>float(x)</c>: from str or int.
    /// <c>float("inf")</c> / <c>float("nan")</c> are accepted
    /// (Python behaviour); a huge int raises OverflowError.</summary>
    public static double ToFloat(object? x)
    {
        if (x is bool b) return b ? 1.0 : 0.0;
        if (x is BigInteger bi)
        {
            if (bi > MaxFloatBigInt || bi < -MaxFloatBigInt)
            {
                // Python raises the builtin OverflowError here.
                throw CodedError("OverflowError",
                    "int too large to convert to float");
            }
            return (double)bi;
        }
        if (x is long l) return l;
        if (x is int i) return i;
        if (x is double d) return d;
        if (x is string s)
        {
            string t = s.Trim().Replace("_", "");
            string low = t.ToLowerInvariant();
            if (low is "inf" or "+inf" or "infinity" or "+infinity")
                return double.PositiveInfinity;
            if (low is "-inf" or "-infinity")
                return double.NegativeInfinity;
            if (low is "nan" or "+nan" or "-nan")
                return double.NaN;
            if (FloatString.IsMatch(t))
                return double.Parse(t, CultureInfo.InvariantCulture);
            throw CodedError("ValueError",
                "could not convert string to float: " + JsonString(s));
        }
        throw new NewrError($"cannot convert {TypeName(x)} to float");
    }

    /// <summary>newr <c>bool(x)</c>: from str
    /// (<c>"true"</c>/<c>"false"</c>, exact lowercase) or int (0
    /// -&gt; false). P7: parsing, not truthiness.</summary>
    public static bool ToBool(object? x)
    {
        if (x is bool b) return b;
        if (x is string s)
        {
            if (s == "true") return true;
            if (s == "false") return false;
            throw CodedError("ValueError",
                $"invalid bool: {JsonString(s)} (expected \"true\" or \"false\")");
        }
        if (x is BigInteger bi) return bi.Sign != 0;
        if (x is double d) return d != 0;
        if (x is long l) return l != 0;
        if (x is int i) return i != 0;
        throw new NewrError($"cannot convert {TypeName(x)} to bool");
    }

    private static string TypeName(object? x)
    {
        return x switch
        {
            null => "none",
            string => "str",
            bool => "bool",
            double => "float",
            long or int or BigInteger => "int",
            System.Collections.IList => "list",
            System.Collections.IDictionary => "dict",
            System.Collections.ICollection => "set",
            _ => x.GetType().Name,
        };
    }

    /// <summary>Float rendering with PYTHON repr semantics (J4):
    /// shortest round-trip digits, scientific notation iff the
    /// decimal exponent is &lt; -4 or &gt;= 16, ".0" for integral
    /// floats. .NET's "R" format differs: (1e7).ToString("R") is
    /// "1E+07" where Python gives "10000000.0".</summary>
    public static string FloatStr(double x)
    {
        if (double.IsNaN(x)) return "nan";
        if (x == double.PositiveInfinity) return "inf";
        if (x == double.NegativeInfinity) return "-inf";
        if (x == 0)
            return BitConverter.DoubleToInt64Bits(x) < 0 ? "-0.0" : "0.0";
        bool neg = x < 0;
        // Shortest digits + exponent from the "R" format (same
        // shortest-round-trip digits as Python's repr).
        string s = Math.Abs(x).ToString("R", CultureInfo.InvariantCulture);
        string digits;
        int e10;
        int ePos = s.IndexOf('E');
        if (ePos >= 0)
        {
            string mant = s.Substring(0, ePos);
            e10 = int.Parse(s.Substring(ePos + 1), CultureInfo.InvariantCulture);
            digits = mant.Replace(".", "");
        }
        else
        {
            int dot = s.IndexOf('.');
            if (dot >= 0)
            {
                e10 = dot - 1;
                digits = s.Substring(0, dot) + s.Substring(dot + 1);
            }
            else
            {
                e10 = s.Length - 1;
                digits = s;
            }
        }
        // Strip trailing zeros (keep at least one digit).
        digits = digits.TrimEnd('0');
        if (digits.Length == 0) digits = "0";
        string sign = neg ? "-" : "";
        if (e10 < -4 || e10 >= 16)
        {
            // Scientific: d[.ddd]e±XX with at least two exponent digits.
            string mant = digits.Length > 1
                ? digits[0] + "." + digits.Substring(1)
                : digits.Substring(0, 1);
            string esign = e10 < 0 ? "-" : "+";
            string eabs = Math.Abs(e10).ToString("D2", CultureInfo.InvariantCulture);
            return $"{sign}{mant}e{esign}{eabs}";
        }
        // Fixed: decimal point after e10+1 digits.
        int point = e10 + 1;
        string output;
        if (point <= 0)
            output = "0." + new string('0', -point) + digits;
        else if (point >= digits.Length)
            output = digits + new string('0', point - digits.Length) + ".0";
        else
            output = digits.Substring(0, point) + "." + digits.Substring(point);
        return sign + output;
    }

    /// <summary>newr <c>str(x)</c>: the display rendering (P8).
    /// <c>true</c>/<c>false</c>/<c>none</c> are lowercase; floats
    /// use Python repr (FloatStr); collections render with elements
    /// rendered recursively and strings bare (<c>[1, true, a]</c>);
    /// an <c>error</c> renders as its message; a struct/enum value
    /// renders via its <c>ToString</c> (emitted by the compiler for
    /// every struct/enum type).</summary>
    public static string ToStr(object? x)
    {
        if (x == null) return "none";
        if (x is bool b) return b ? "true" : "false";
        if (x is string s) return s;
        if (x is BigInteger bi) return bi.ToString();
        if (x is double d) return FloatStr(d);
        if (x is float f) return FloatStr(f);
        if (x is long l) return l.ToString(CultureInfo.InvariantCulture);
        if (x is int i) return i.ToString(CultureInfo.InvariantCulture);
        if (x is NewrError e) return e.Message;
        if (x is INewrRepr repr) return repr.NewrRepr();
        if (x is System.Collections.IList list)
        {
            var parts = new List<string>();
            foreach (object? item in list) parts.Add(ToStr(item));
            return "[" + string.Join(", ", parts) + "]";
        }
        if (x is System.Collections.IDictionary dict)
        {
            var parts = new List<string>();
            foreach (System.Collections.DictionaryEntry kv in dict)
                parts.Add($"{ToStr(kv.Key)}: {ToStr(kv.Value)}");
            return "{" + string.Join(", ", parts) + "}";
        }
        if (x is System.Collections.IEnumerable en
            && x is not string)
        {
            // Sets (and any other enumerable): unordered rendering.
            var parts = new List<string>();
            foreach (object? item in en) parts.Add(ToStr(item));
            return "{" + string.Join(", ", parts) + "}";
        }
        return x.ToString() ?? "none";
    }

    // ---------------------------------------------------------------- Result

    /// <summary>newr <c>Result[T, E]</c> (SPEC §10): the value form
    /// of errors.</summary>
    public class Result<T, E> : INewrRepr
    {
        public readonly bool _isOk;
        public readonly T? _value;
        public readonly E? _error;

        public Result(bool isOk, T? value, E? error)
        {
            _isOk = isOk;
            _value = value;
            _error = error;
        }

        public bool IsOk() => _isOk;
        public bool IsErr() => !_isOk;

        public T? Unwrap()
        {
            if (_isOk) return _value;
            if (_error is Exception ex) throw ex;
            throw new NewrError(_error?.ToString() ?? "none");
        }

        public T? UnwrapOr(T? dflt) => _isOk ? _value : dflt;

        public string NewrRepr() =>
            _isOk ? $"ok({ToStr(_value)})" : $"err({ToStr(_error)})";

        public override string ToString() => NewrRepr();

        public override bool Equals(object? o)
        {
            if (ReferenceEquals(this, o)) return true;
            if (o is not Result<T, E> other) return false;
            if (_isOk != other._isOk) return false;
            return _isOk ? Eq(_value, other._value) : Eq(_error, other._error);
        }

        public override int GetHashCode() =>
            _isOk ? HashOf(_value) : HashOf(_error);
    }

    /// <summary>A hash consistent with <c>Eq</c>: -0.0 and 0.0
    /// hash alike (Eq says they are equal), and collections hash
    /// by their elements (a List's GetHashCode is identity-based,
    /// and Double.GetHashCode(-0.0) differs from 0.0, both of
    /// which would disagree with Eq). NaN needs no agreement: Eq
    /// says NaN != NaN, so unequal values may share a hash.</summary>
    public static int HashOf(object? x)
    {
        if (x == null) return 0;
        if (x is double d)
        {
            double v = d == 0.0 ? 0.0 : d;  // normalizes -0.0 to 0.0
            return v.GetHashCode();
        }
        if (x is System.Collections.IList l)
        {
            int h = 1;
            foreach (object? e in l) h = 31 * h + HashOf(e);
            return h;
        }
        if (x is System.Collections.IDictionary m)
        {
            int h = 0;
            foreach (System.Collections.DictionaryEntry e in m)
                h += HashOf(e.Key) ^ HashOf(e.Value);
            return h;
        }
        if (x is System.Collections.IEnumerable s
            && x is not string)
        {
            // Sets (HashSet<T>) and any other enumerable: order-
            // insensitive sum, like the Java runtime.
            int h = 0;
            foreach (object? e in s) h += HashOf(e);
            return h;
        }
        return x.GetHashCode();
    }

    /// <summary>Hash of a struct/enum's fields, consistent with
    /// <c>Eq</c>.</summary>
    public static int StructHash(params object?[] fields)
    {
        int h = 1;
        foreach (object? f in fields) h = 31 * h + HashOf(f);
        return h;
    }

    /// <summary><c>env.get(name)</c> /
    /// <c>env.get(name, default)</c>: null when the variable is
    /// absent and no default is given.</summary>
    public static string? EnvGet(string name) =>
        Environment.GetEnvironmentVariable(name);

    public static string? EnvGet(string name, string? dflt) =>
        Environment.GetEnvironmentVariable(name) ?? dflt;

    /// <summary><c>env.args</c>: the command-line arguments, set
    /// by the generated Main() (a module file has no Main, so the
    /// runtime holds the copy). Defaults to an empty list before
    /// Main runs.</summary>
    public static string[] _mainArgs = Array.Empty<string>();

    /// <summary>Called by the generated Main(): <c>env.args</c>
    /// is <c>sys.argv</c> 1:1 (Python: <c>sys.argv</c>; JS:
    /// <c>process.argv.slice(1)</c> - both include the program as
    /// argv[0]), but .NET's Main args exclude the program name, so
    /// SetMainArgs prepends the main class name.</summary>
    public static void SetMainArgs(string prog, string[] args)
    {
        var all = new string[args.Length + 1];
        all[0] = prog;
        Array.Copy(args, 0, all, 1, args.Length);
        _mainArgs = all;
    }

    public static List<string> EnvArgs() => new List<string>(_mainArgs);

    public static Result<T, NewrError> Ok<T>(T value) =>
        new Result<T, NewrError>(true, value, null);

    public static Result<T, NewrError> Err<T>(NewrError error) =>
        new Result<T, NewrError>(false, default, error);

    /// <summary>newr <c>try &lt;expr&gt;</c>: Ok-wrap the value,
    /// catch a thrown error into <c>err(...)</c>. The caught error
    /// is coerced with <c>AsError</c> so <c>.message</c> /
    /// <c>.code</c> are always available.</summary>
    public static Result<T, NewrError> TryResult<T>(Func<T> f)
    {
        try { return Ok(f()); }
        catch (Exception ex) { return Err<T>(AsError(ex)); }
    }

    // ---------------------------------------------------------------- equality (J6)

    /// <summary>newr <c>==</c>: structural equality (§7.7).
    /// Primitives compare by value; lists compare element-wise,
    /// dicts by key (order-insensitive), sets unordered,
    /// structs/enums by constructor + fields (their generated
    /// <c>Equals</c>), Results by kind + payload. Functions and
    /// errors compare by identity. NaN is never equal to NaN (not
    /// even inside a collection).</summary>
    public static bool Eq(object? a, object? b)
    {
        if (ReferenceEquals(a, b))
        {
            // Same reference - but NaN is not == itself even as a
            // boxed double, and two distinct NaN boxes are not
            // ReferenceEquals anyway; a single NaN box compared to
            // itself reaches here, so exclude it.
            if (a is double da && double.IsNaN(da)) return false;
            return true;
        }
        if (a == null || b == null) return false;
        if (a is double || b is double)
        {
            if (a is not double || b is not double) return false;
            // Unboxed == : -0.0 == 0.0 is true, NaN == NaN is false
            // (exactly the JS `===` semantics for numbers).
            return (double)a == (double)b;
        }
        if (a is System.Collections.IList || b is System.Collections.IList)
        {
            if (a is not System.Collections.IList
                || b is not System.Collections.IList) return false;
            var la = (System.Collections.IList)a;
            var lb = (System.Collections.IList)b;
            if (la.Count != lb.Count) return false;
            for (int i = 0; i < la.Count; i++)
                if (!Eq(la[i], lb[i])) return false;
            return true;
        }
        if (a is System.Collections.IDictionary
            || b is System.Collections.IDictionary)
        {
            if (a is not System.Collections.IDictionary
                || b is not System.Collections.IDictionary) return false;
            var dictA = (System.Collections.IDictionary)a;
            var dictB = (System.Collections.IDictionary)b;
            if (dictA.Count != dictB.Count) return false;
            foreach (System.Collections.DictionaryEntry kv in dictA)
            {
                if (!dictB.Contains(kv.Key) || !Eq(kv.Value, dictB[kv.Key])) return false;
            }
            return true;
        }
        if ((a is System.Collections.IEnumerable && a is not string)
            || (b is System.Collections.IEnumerable && b is not string))
        {
            // Sets: unordered pairwise comparison. (Lists and dicts
            // were handled above, so only sets reach here.)
            if (a is string || b is string) return false;
            if (a is not System.Collections.IEnumerable
                || b is not System.Collections.IEnumerable) return false;
            var enumA = (System.Collections.IEnumerable)a;
            var enumB = (System.Collections.IEnumerable)b;
            var listA = new List<object?>();
            var listB = new List<object?>();
            foreach (object? item in enumA) listA.Add(item);
            foreach (object? item in enumB) listB.Add(item);
            if (listA.Count != listB.Count) return false;
            foreach (object? x in listA)
            {
                bool found = false;
                foreach (object? y in listB)
                {
                    if (Eq(x, y)) { found = true; break; }
                }
                if (!found) return false;
            }
            return true;
        }
        if (a is NewrError || b is NewrError)
            return false; // errors compare by identity (above)
        // Results (kind + payload) and structs/enums (constructor +
        // fields) reach here via their generated Equals.
        return a.Equals(b);
    }

    // ---------------------------------------------------------------- ?. / ?? / accessors

    /// <summary>newr <c>?.</c>: <c>null</c> when <c>v</c> is
    /// <c>null</c>, else <c>f(v)</c>. The test is <c>== null</c>,
    /// never truthiness - <c>0</c>, <c>""</c> and <c>[]</c> are
    /// values, not absences. A whole <c>?.</c> chain collapses into
    /// one call with the full postfix path inside <c>f</c>, so the
    /// base expression is evaluated exactly once.
    ///
    /// C# cannot spell "nullable R" for an unconstrained R
    /// (<c>R?</c> collapses to <c>R</c> unless R is
    /// struct-constrained), so the chain helper comes in two
    /// names by result kind - <c>OptChain</c> for a reference
    /// result, <c>OptChainVal</c> for a value result - each with a
    /// class/struct pair for the receiver (the pair differs in the
    /// parameter type, <c>T</c> vs <c>Nullable&lt;T&gt;</c>, so the
    /// overloads are legal). The emitter picks by the receiver's
    /// and the result's newr types.</summary>
    public static R? OptChain<T, R>(T? v, Func<T, R> f)
        where T : class where R : class =>
        v == null ? default : f(v);

    public static R? OptChain<T, R>(T? v, Func<T, R> f)
        where T : struct where R : class =>
        v == null ? default : f(v.Value);

    public static R? OptChainVal<T, R>(T? v, Func<T, R> f)
        where T : class where R : struct
    {
        // NOTE: `v == null ? default : f(v)` is wrong here - the
        // ternary types `default` as R (0), not R? (null). The
        // explicit `return null` below is load-bearing.
        if (v == null) return null;
        return f(v);
    }

    public static R? OptChainVal<T, R>(T? v, Func<T, R> f)
        where T : struct where R : struct
    {
        if (v == null) return null;
        return f(v.Value);
    }

    /// <summary>newr <c>?.</c> where the chain's own result is a
    /// nullable VALUE type (a nullable field or method result at
    /// the end of a segment, e.g. <c>o?.maybe_count</c> where
    /// <c>maybe_count</c> is <c>long?</c>). The lambda already
    /// returns <c>R?</c>, so the method returns it as-is - the
    /// <c>OptChainVal</c> shape (lambda returns <c>R</c>, method
    /// returns <c>R?</c>) cannot spell this (a <c>R??</c> is not a
    /// legal type).</summary>
    public static R? OptChainNullable<T, R>(T? v, Func<T, R?> f)
        where T : class where R : struct =>
        v == null ? null : f(v);

    public static R? OptChainNullable<T, R>(T? v, Func<T, R?> f)
        where T : struct where R : struct =>
        v == null ? null : f(v.Value);

    /// <summary>newr <c>??</c>: <c>v</c> when it is not
    /// <c>null</c>, else <c>defaultFn()</c>. The default is a thunk
    /// so it is evaluated lazily. Two names by value kind - the
    /// class/struct overload pair for one name resolves
    /// unpredictably at some call shapes (the compiler picks the
    /// class overload for a <c>long</c> argument and then fails its
    /// constraint), so the emitter calls <c>Coalesce</c> for
    /// reference types and <c>CoalesceVal</c> for value types.</summary>
    public static T? Coalesce<T>(T? v, Func<T> dflt) where T : class =>
        v ?? dflt();

    /// <summary>The value-type twin of <c>Coalesce</c>. The return
    /// is <c>T</c> (not <c>T?</c>): a <c>??</c> always produces a
    /// value (the default is always provided), so the result is
    /// non-nullable - and a <c>long?</c> result cannot be assigned
    /// to a <c>long</c> variable without a cast (CS0266).</summary>
    public static T CoalesceVal<T>(T? v, Func<T> dflt) where T : struct =>
        v ?? dflt();

    /// <summary>newr <c>list[i]</c>: SPEC §9.2 - out-of-range
    /// indexing is a runtime error. Valid indices are
    /// <c>0 &lt;= i &lt; len(lst)</c>; a negative index is out of
    /// range (never a wrap).</summary>
    public static object? ListIndex(System.Collections.IList lst, long i)
    {
        if (i < 0 || i >= lst.Count)
            throw CodedError("IndexError", "list index out of range");
        return lst[(int)i];
    }

    /// <summary>newr <c>list.get(i, default)</c>: P9 - a negative
    /// index is out-of-range (no wrap-around); anything outside
    /// <c>0 &lt;= i &lt; len(lst)</c> returns <c>default</c>.</summary>
    public static object? ListGet(System.Collections.IList lst, long i, object? dflt) =>
        i >= 0 && i < lst.Count ? lst[(int)i] : dflt;

    /// <summary>newr <c>x in [..]</c>: structural equality (a list
    /// of structs contains an equal-but-different instance).</summary>
    public static bool ListContains(System.Collections.IList lst, object? x)
    {
        foreach (object? e in lst)
            if (Eq(e, x)) return true;
        return false;
    }

    /// <summary>newr <c>set.remove(x)</c>: removing a missing
    /// element is an error (Python's <c>set.remove</c> raises
    /// KeyError; .NET's <c>HashSet.Remove</c> silently returns
    /// false). Sets hash by value in C# (like Python), unlike the
    /// JS target's identity sets (J6).</summary>
    public static void SetRemove<T>(HashSet<T> s, T x)
    {
        if (!s.Contains(x)) throw CodedError("KeyError", ToStr(x));
        s.Remove(x);
    }

    /// <summary>newr <c>T?.unwrap()</c>: the value, or a thrown
    /// error on <c>null</c>. Two names by value kind, same reason
    /// as Coalesce/CoalesceVal above.</summary>
    public static T? UnwrapOpt<T>(T? v) where T : class =>
        v ?? throw new NewrError("unwrap of none");

    public static T UnwrapOptVal<T>(T? v) where T : struct =>
        v ?? throw new NewrError("unwrap of none");

    /// <summary><c>len(x)</c> for any lenable value (str: code
    /// points, P5).</summary>
    public static long LenOf(object? x)
    {
        if (x is string s) return Chars(s).Count;
        if (x is System.Collections.IDictionary m) return m.Count;
        // Lists AND sets: HashSet<T> implements IEnumerable but
        // NOT the non-generic ICollection, so count by enumeration
        // (a List<T> is also IEnumerable - one branch covers both).
        if (x is System.Collections.IEnumerable e)
        {
            long n = 0;
            foreach (object? _ in e) n++;
            return n;
        }
        throw new NewrError("len() of unsupported value: " + ToStr(x),
                            "TypeError");
    }

    /// <summary><c>{"k": v, ...}</c> - alternating key/value pairs
    /// into a Dictionary (insertion order in practice on .NET).
    /// Unlike the Java runtime's Map.ofEntries, null values are
    /// allowed (a null key still throws - the checker only ever
    /// emits str keys, which are non-null).</summary>
    public static Dictionary<string, V> DictOf<V>(params object?[] kvs)
    {
        var outDict = new Dictionary<string, V>();
        for (int i = 0; i < kvs.Length; i += 2)
            outDict[(string)kvs[i]!] = (V)kvs[i + 1]!;
        return outDict;
    }

    // ---------------------------------------------------------------- strings

    /// <summary>Code-point list of a string (SPEC pin P5:
    /// <c>str.chars()</c>). .NET strings are UTF-16; EnumerateRunes
    /// yields one Rune per code point, so an astral character is a
    /// single element (not two surrogates).</summary>
    public static List<string> Chars(string s)
    {
        var outList = new List<string>();
        foreach (var rune in s.EnumerateRunes())
            outList.Add(rune.ToString());
        return outList;
    }

    /// <summary><c>s.split(sep)</c> with a LITERAL separator
    /// (unlike Java's String.split, .NET's string.Split with a
    /// string[] separator is already literal) and Python's
    /// trailing-empty-strings behaviour (StringSplitOptions.None
    /// keeps them).</summary>
    public static List<string> SplitStr(string s, string sep) =>
        new List<string>(s.Split(new[] { sep }, StringSplitOptions.None));

    /// <summary><c>s[i]</c> by code point (P5). Negative indices
    /// wrap like Python; out of range raises IndexError (the JS
    /// runtime's <c>[...s][i]</c> returns undefined there - a JS
    /// latent bug).</summary>
    public static string StrIndex(string s, long idx)
    {
        var cps = Chars(s);
        long i = idx < 0 ? cps.Count + idx : idx;
        if (i < 0 || i >= cps.Count)
            throw new NewrError("string index out of range: " + idx,
                                "IndexError");
        return cps[(int)i];
    }

    /// <summary><c>abs(x)</c> for int64: <c>Math.Abs(long.MinValue)</c>
    /// throws OverflowException in .NET, so the minimum is
    /// reported as the newr overflow error (Python:
    /// abs(-2**63) = 2**63, out of int64 range).</summary>
    public static long AbsLong(long x)
    {
        if (x == long.MinValue)
            throw new NewrOverflowError(
                "integer overflow: 9223372036854775808 is outside int64 range");
        return Math.Abs(x);
    }

    /// <summary>newr <c>range(...)</c>: like Python's range,
    /// materialised as a list of longs. range(stop) /
    /// range(start, stop) / range(start, stop, step); a zero step
    /// is a ValueError.</summary>
    public static List<long> Range(long stop) => Range(0, stop, 1);

    public static List<long> Range(long start, long stop) => Range(start, stop, 1);

    public static List<long> Range(long start, long stop, long step)
    {
        if (step == 0)
            throw CodedError("ValueError", "range() arg 3 must not be zero");
        var outList = new List<long>();
        if (step > 0)
        {
            for (long i = start; i < stop; i += step) outList.Add(i);
        }
        else
        {
            for (long i = start; i > stop; i += step) outList.Add(i);
        }
        return outList;
    }

    // ---------------------------------------------------------------- print / input

    /// <summary>newr <c>print</c>: every argument rendered with
    /// <c>ToStr</c>, joined with a single space (Python's
    /// print).</summary>
    public static void Print(params object?[] args)
    {
        var parts = new List<string>();
        foreach (object? a in args) parts.Add(ToStr(a));
        Console.WriteLine(string.Join(" ", parts));
    }

    /// <summary>newr <c>input(prompt = "")</c>: read one line from
    /// stdin, like Python's input(). The prompt (if any) is written
    /// with no newline first. At EOF, returns "" (mirrors the JS
    /// runtime).</summary>
    public static string Input(string prompt = "")
    {
        if (!string.IsNullOrEmpty(prompt)) Console.Write(prompt);
        return Console.ReadLine() ?? "";
    }

    // ---------------------------------------------------------------- random

    /// <summary>newr <c>random.int(lo, hi)</c>: inclusive on both
    /// ends (Python's randint). Exact for any int64 range:
    /// rejection sampling over random bytes
    /// (Random.NextInt64 cannot cover a 64-bit range).</summary>
    public static long RandomInt(long lo, long hi)
    {
        if (hi < lo)
            throw CodedError("ValueError", "empty range for random.int()");
        BigInteger span = (BigInteger)hi - lo + 1;
        if (span == BigInteger.One) return lo;
        int bytes = (int)((span.GetBitLength() + 7) / 8);
        byte[] buf = new byte[bytes];
        for (;;)
        {
            RandomNumberGenerator.Fill(buf);
            var v = new BigInteger(buf, isUnsigned: true, isBigEndian: true);
            if (v < span) return (long)((BigInteger)lo + v);
        }
    }

    public static T RandomChoice<T>(List<T> xs)
    {
        if (xs.Count == 0)
            throw CodedError("IndexError",
                "Cannot choose from an empty sequence");
        return xs[Random.Shared.Next(xs.Count)];
    }

    /// <summary>Returns null (the newr value <c>none</c>) so the
    /// call stays usable in expression position, like the JS
    /// runtime.</summary>
    public static object? RandomShuffle<T>(List<T> xs)
    {
        for (int i = xs.Count - 1; i > 0; i--)
        {
            int j = Random.Shared.Next(i + 1);
            (xs[i], xs[j]) = (xs[j], xs[i]);
        }
        return null;
    }

    // ---------------------------------------------------------------- time (P4)

    private static readonly HashSet<string> TimeFormats = new()
    {
        "%Y-%m-%d", "%H:%M", "%Y-%m-%d %H:%M", "%d/%m/%Y", "%H:%M:%S",
    };

    /// <summary>newr <c>time.format(ts, fmt)</c>: UTC, never local
    /// time (P4) - the same program prints the same output on every
    /// machine. The format must be one of the spec's fixed set
    /// (§12.2).</summary>
    public static string TimeFormat(long ts, string fmt)
    {
        if (!TimeFormats.Contains(fmt))
            throw new NewrError("unsupported time format: " + JsonString(fmt));
        var d = DateTimeOffset.FromUnixTimeSeconds(ts).UtcDateTime;
        string y = d.Year.ToString("D4", CultureInfo.InvariantCulture);
        string mo = d.Month.ToString("D2", CultureInfo.InvariantCulture);
        string day = d.Day.ToString("D2", CultureInfo.InvariantCulture);
        string h = d.Hour.ToString("D2", CultureInfo.InvariantCulture);
        string mi = d.Minute.ToString("D2", CultureInfo.InvariantCulture);
        string s = d.Second.ToString("D2", CultureInfo.InvariantCulture);
        return fmt switch
        {
            "%Y-%m-%d" => $"{y}-{mo}-{day}",
            "%H:%M" => $"{h}:{mi}",
            "%Y-%m-%d %H:%M" => $"{y}-{mo}-{day} {h}:{mi}",
            "%d/%m/%Y" => $"{day}/{mo}/{y}",
            "%H:%M:%S" => $"{h}:{mi}:{s}",
            _ => throw new NewrError("unsupported time format: " + JsonString(fmt)),
        };
    }

    // ---------------------------------------------------------------- file I/O

    /// <summary>newr <c>file.read(path)</c>: the whole file as
    /// UTF-8 text. A missing file throws a NewrError whose code is
    /// the host exception name (C#: "FileNotFoundException"; the
    /// Python target reports "FileNotFoundError" there, the JS
    /// target "ENOENT"; the code for host IO errors is
    /// host-specific, BUILD_REPORT §21).</summary>
    public static string FileRead(string path)
    {
        try { return File.ReadAllText(path, Encoding.UTF8); }
        catch (Exception e) { throw AsError(e); }
    }

    /// <summary>newr <c>file.write(path, content)</c>: overwrite
    /// (create if needed). Returns null (the newr value
    /// <c>none</c>).</summary>
    public static object? FileWrite(string path, string content)
    {
        try { File.WriteAllText(path, content, Encoding.UTF8); }
        catch (Exception e) { throw AsError(e); }
        return null;
    }

    /// <summary>newr <c>file.append(path, content)</c>: append
    /// (create if needed). Returns null (the newr value
    /// <c>none</c>).</summary>
    public static object? FileAppend(string path, string content)
    {
        try { File.AppendAllText(path, content, Encoding.UTF8); }
        catch (Exception e) { throw AsError(e); }
        return null;
    }

    /// <summary>newr <c>file.exists(path)</c>.</summary>
    public static bool FileExists(string path) => File.Exists(path);
}
