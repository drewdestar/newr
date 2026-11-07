// The newr Java runtime (M4).
//
// This file is the runtime half of every emitted Java program that
// needs it: checked 64-bit integer arithmetic (long + Math.*Exact),
// truncating division, half-away rounding on the EXACT float value,
// the newr conversion/rendering rules, `Result`, and the `?.` / `??`
// helpers. It is stdlib-only (java.base) and dependency-free.
//
// It lives in the compiler package as `compiler/newr_rt.java` and
// `newr build --target java` copies it — byte-identical — next to
// an emitted `.java` file when (and only when) that program uses a
// helper from it. Do not edit a copied file by hand: edit this file
// in the newr repo and rebuild.
//
// Semantics are pinned in BUILD_REPORT.md §21 (K2-K6, mirroring
// the M2 pins P1-P12 and the M3 pins J1-J13). Every rule here is
// unit-tested in tests/java/RtChecks.java (run by
// tests/test_newr_rt_java.py).
//
// The runtime is a single public class `NewrRt` in the default
// package, so an emitted file runs with plain
// `javac *.java && java <Stem>` and no build file. Emitted code
// calls helpers as `NewrRt.add(a, b)`; error classes are
// `NewrRt.NewrError` and friends.

import java.io.BufferedReader;
import java.io.IOException;
import java.io.InputStreamReader;
import java.math.BigInteger;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.security.SecureRandom;
import java.time.Instant;
import java.time.ZoneOffset;
import java.time.ZonedDateTime;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.Collection;
import java.util.Collections;
import java.util.HashSet;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.concurrent.ThreadLocalRandom;
import java.util.function.Function;
import java.util.function.Supplier;
import java.util.regex.Pattern;

public class NewrRt {

    // ---------------------------------------------------------------- int64

    public static final long INT64_MIN = Long.MIN_VALUE;
    public static final long INT64_MAX = Long.MAX_VALUE;

    private static final BigInteger INT64_MIN_BI = BigInteger.valueOf(Long.MIN_VALUE);
    private static final BigInteger INT64_MAX_BI = BigInteger.valueOf(Long.MAX_VALUE);
    // Largest magnitude whose double conversion is finite in the
    // Python sense: float(2**1024 - 2**971) is finite, one more is
    // inf (mirrors the JS _MAX_FLOAT_BIGINT).
    private static final BigInteger MAX_FLOAT_BIGINT =
        BigInteger.ONE.shiftLeft(1024).subtract(BigInteger.ONE.shiftLeft(971));

    /** Return `v` if it fits in signed 64-bit, else raise. */
    public static long checkInt(long v) {
        return v;
    }

    /** Return `v` if it fits in signed 64-bit, else raise. */
    public static long checkInt(BigInteger v) {
        if (v.compareTo(INT64_MIN_BI) < 0 || v.compareTo(INT64_MAX_BI) > 0) {
            throw new NewrOverflowError(
                "integer overflow: " + v + " is outside int64 range");
        }
        return v.longValueExact();
    }

    // ---------------------------------------------------------------- errors

    /** Base of every newr `error` value. Carries the error payload.
     * `code` is null unless set (a caught host error keeps its own
     * code via asError). */
    public static class NewrError extends RuntimeException {
        public final String code;
        public final Object payload;

        public NewrError(String message) {
            this(message, null, null);
        }

        public NewrError(String message, String code) {
            this(message, code, null);
        }

        public NewrError(String message, String code, Object payload) {
            super(message);
            this.code = code;
            this.payload = payload;
        }
    }

    /** Checked-int overflow. The Python target's NewrOverflowError
     * is a NewrError subclass constructed WITHOUT a code, so a newr
     * `catch e:` sees e.code == none (the class is told by `name`,
     * not by `code`). The builtin OverflowError (round(inf),
     * int(inf), float(huge int)) is a different site: it throws
     * codedError("OverflowError", ...) instead. */
    public static class NewrOverflowError extends NewrError {
        public NewrOverflowError(String message) {
            super(message);
        }
    }

    /** Division or modulo by zero. Same code rule as above: the
     * Python twin is constructed without a code, so e.code is none
     * in a `catch`. The builtin sites — `0.0 ** -1`, where Python
     * raises ZeroDivisionError itself, and float `/` by zero, where
     * Python's native `/` raises it — throw
     * codedError("ZeroDivisionError", ...) in powFloat and fdiv
     * instead (§18 fix 2: fdiv was missed by the §17.4 item-4 list). */
    public static class NewrDivisionByZeroError extends NewrError {
        public NewrDivisionByZeroError(String message) {
            super(message);
        }
    }

    /** An error with a host-style code (IndexError, KeyError,
     * ValueError): the Python target raises the builtin of that
     * name, so a newr `catch e:` sees the same code on both targets. */
    public static NewrError codedError(String code, String message) {
        return new NewrError(message, code);
    }

    /** Coerce whatever a `catch` caught into a newr `error` value.
     * A `NewrError` passes through unchanged; a plain Throwable
     * keeps its message and gains its class name as the code (a
     * NoSuchFileException, an IllegalStateException); a string
     * becomes an error with that message. This is what makes
     * `.message` / `.code` total inside every `catch` body
     * (SPEC §10). */
    public static NewrError asError(Object e) {
        if (e instanceof NewrError) {
            return (NewrError) e;
        }
        if (e instanceof String) {
            return new NewrError((String) e);
        }
        if (e instanceof Throwable) {
            Throwable t = (Throwable) e;
            return new NewrError(t.getMessage(),
                t.getClass().getSimpleName());
        }
        return new NewrError(String.valueOf(e));
    }

    /** A supplier that may throw anything (for `try <expr>`). */
    @FunctionalInterface
    public interface ThrowingSupplier<T> {
        T get() throws Throwable;
    }

    /** Functional interfaces for newr fn values (Java's
     * `java.util.function.Function` covers only 1–2 arguments;
     * newr fns take any arity, so the emitter uses these uniformly).
     * The emitter writes them as `NewrRt.Fn1<Long, Long>`. */
    @FunctionalInterface
    public interface Fn0<R> { R get(); }
    @FunctionalInterface
    public interface Fn1<A, R> { R apply(A a); }
    @FunctionalInterface
    public interface Fn2<A, B, R> { R apply(A a, B b); }
    @FunctionalInterface
    public interface Fn3<A, B, C, R> { R apply(A a, B b, C c); }
    @FunctionalInterface
    public interface Fn4<A, B, C, D, R> { R apply(A a, B b, C c, D d); }
    @FunctionalInterface
    public interface Fn5<A, B, C, D, E, R> { R apply(A a, B b, C c, D d, E e); }
    @FunctionalInterface
    public interface Fn6<A, B, C, D, E, F, R> { R apply(A a, B b, C c, D d, E e, F f); }
    @FunctionalInterface
    public interface Fn7<A, B, C, D, E, F, G, R> { R apply(A a, B b, C c, D d, E e, F f, G g); }
    @FunctionalInterface
    public interface Fn8<A, B, C, D, E, F, G, H, R> { R apply(A a, B b, C c, D d, E e, F f, G g, H h); }

    // ---------------------------------------------------------------- checked int arithmetic

    public static long add(long a, long b) {
        try {
            return Math.addExact(a, b);
        } catch (ArithmeticException e) {
            throw new NewrOverflowError("integer overflow: "
                + BigInteger.valueOf(a).add(BigInteger.valueOf(b))
                + " is outside int64 range");
        }
    }

    public static long sub(long a, long b) {
        try {
            return Math.subtractExact(a, b);
        } catch (ArithmeticException e) {
            throw new NewrOverflowError("integer overflow: "
                + BigInteger.valueOf(a).subtract(BigInteger.valueOf(b))
                + " is outside int64 range");
        }
    }

    public static long mul(long a, long b) {
        try {
            return Math.multiplyExact(a, b);
        } catch (ArithmeticException e) {
            throw new NewrOverflowError("integer overflow: "
                + BigInteger.valueOf(a).multiply(BigInteger.valueOf(b))
                + " is outside int64 range");
        }
    }

    public static long neg(long a) {
        try {
            return Math.negateExact(a);
        } catch (ArithmeticException e) {
            throw new NewrOverflowError("integer overflow: "
                + BigInteger.valueOf(a).negate()
                + " is outside int64 range");
        }
    }

    public static long powInt(long a, long b) {
        // newr `**` on ints: checked, bounded before computing.
        // A negative exponent is an error (an int result cannot
        // represent it). The exponent is bounded by repeated
        // squaring with a check at every multiply — `10 ** 10**9`
        // raises instead of allocating by computing blindly.
        if (b < 0) {
            throw new NewrError("negative exponent for int pow");
        }
        if (a == 0) {
            return b == 0 ? 1 : 0;
        }
        if (a == 1) {
            return 1;
        }
        if (a == -1) {
            return (b % 2 == 1) ? -1 : 1;
        }
        long result = 1;
        long base = a;
        long exp = b;
        while (exp > 0) {
            if (exp % 2 == 1) {
                result = mul(result, base);
            }
            exp /= 2;
            if (exp > 0) {
                base = mul(base, base);
            }
        }
        return result;
    }

    public static long tdiv(long a, long b) {
        // newr `/` on ints: truncating division (toward zero).
        // Java `/` already truncates — the check is the two error
        // cases.
        if (b == 0) {
            throw new NewrDivisionByZeroError("integer division by zero");
        }
        if (a == Long.MIN_VALUE && b == -1) {
            throw new NewrOverflowError("integer overflow: INT64_MIN / -1");
        }
        return a / b;
    }

    public static long tmod(long a, long b) {
        // newr `%` on ints: remainder with the dividend's sign.
        // Java `%` already takes the dividend's sign.
        if (b == 0) {
            throw new NewrDivisionByZeroError("integer modulo by zero");
        }
        if (a == Long.MIN_VALUE && b == -1) {
            return 0; // mathematically 0; no overflow (unlike division)
        }
        return a % b;
    }

    public static double powFloat(double a, double b) {
        // newr `**` on floats: IEEE semantics. Java `Math.pow`
        // already returns NaN for a negative base with a fractional
        // exponent (Python returns a complex there — newr has no
        // complex type, so the answer is NaN) and Infinity on
        // overflow. The one case Java gets wrong is a zero base
        // with a negative exponent: Java gives Infinity, Python
        // raises ZeroDivisionError.
        if (a == 0 && b < 0) {
            // Python raises the builtin ZeroDivisionError here (not
            // the NewrError subclass), so a `catch e:` sees code
            // "ZeroDivisionError" — codedError, not the class.
            throw codedError("ZeroDivisionError",
                "0.0 cannot be raised to a negative power");
        }
        return Math.pow(a, b);
    }

    public static double fmod(double a, double b) {
        // newr `%` on floats: C `fmod` semantics (dividend's
        // sign). Java `%` already takes the dividend's sign
        // (unlike Python's `%`, which takes the divisor's).
        // Division by zero raises like the int case (Java would
        // give NaN).
        if (b == 0) {
            throw new NewrDivisionByZeroError("float modulo by zero");
        }
        return a % b;
    }

    public static double fdiv(double a, double b) {
        // newr `/` on floats: IEEE division, except division by
        // zero raises like Python (Java would give Infinity for a
        // non-zero dividend and NaN for 0/0). `b == 0` covers -0
        // as well. Python's native `/` raises the BUILTIN
        // ZeroDivisionError, so this throws the coded error
        // (§18 fix 2), not the codeless class — a `catch e:` sees
        // e.code == "ZeroDivisionError" on both targets. (Float `%`
        // is different: both targets route it through fmod, which
        // raises the codeless NewrDivisionByZeroError — that parity
        // is correct.)
        if (b == 0) {
            throw codedError("ZeroDivisionError", "float division by zero");
        }
        return a / b;
    }

    // ---------------------------------------------------------------- rounding (P2 / J3)

    /** Exact decimal expansion of a finite double: value =
     * sign * digits / 10**scale, digits a non-negative BigInteger.
     * Mirrors the JS _exactDecimal (same bit decomposition). */
    private static class ExactDecimal {
        final long sign; // -1 or 1
        final BigInteger digits;
        final int scale;

        ExactDecimal(long sign, BigInteger digits, int scale) {
            this.sign = sign;
            this.digits = digits;
            this.scale = scale;
        }
    }

    private static ExactDecimal exactDecimal(double x) {
        long bits = Double.doubleToRawLongBits(x);
        long sign = (bits < 0) ? -1 : 1;
        int expBits = (int) ((bits >>> 52) & 0x7FFL);
        long frac = bits & ((1L << 52) - 1);
        BigInteger m;
        long e;
        if (expBits == 0) {
            // Subnormal (or zero): no implicit leading 1.
            m = BigInteger.valueOf(frac);
            e = -1074;
        } else {
            m = BigInteger.valueOf(frac).or(BigInteger.ONE.shiftLeft(52));
            e = expBits - 1075L;
        }
        if (m.signum() == 0) {
            return new ExactDecimal(sign, BigInteger.ZERO, 0);
        }
        if (e >= 0) {
            return new ExactDecimal(sign, m.shiftLeft((int) e), 0);
        }
        return new ExactDecimal(sign,
            m.multiply(BigInteger.valueOf(5).pow((int) -e)), (int) -e);
    }

    /** Round the exact decimal (digits / 10**scale, digits >= 0)
     * to `n` places after the point, half away from zero.
     * Returns {rounded, places} with value = rounded / 10**places. */
    private static class RoundedDecimal {
        final BigInteger rounded;
        final long places;

        RoundedDecimal(BigInteger rounded, long places) {
            this.rounded = rounded;
            this.places = places;
        }
    }

    private static RoundedDecimal roundDecimal(BigInteger digits, long scale, long n) {
        if (scale > n) {
            BigInteger div = BigInteger.TEN.pow((int) (scale - n));
            BigInteger[] qr = digits.divideAndRemainder(div);
            BigInteger q = qr[0];
            BigInteger r = qr[1];
            if (r.shiftLeft(1).compareTo(div) >= 0) {
                q = q.add(BigInteger.ONE);
            }
            return new RoundedDecimal(q, n);
        }
        // Already exact at n places: digits / 10**scale, scale <= n.
        return new RoundedDecimal(digits, scale);
    }

    private static String decimalString(BigInteger digits, long places) {
        // `digits` is a non-negative BigInteger holding the value
        // * 10**places. `places` may be negative (round(x, -1)
        // rounds to tens): then the value is digits * 10**(-places).
        String s = digits.toString();
        if (places == 0) {
            return s;
        }
        if (places < 0) {
            return s + "0".repeat((int) -places);
        }
        if (s.length() > places) {
            return s.substring(0, (int) (s.length() - places)) + "."
                + s.substring((int) (s.length() - places));
        }
        return "0." + "0".repeat((int) (places - s.length())) + s;
    }

    /** Round the exact decimal (digits / 10**scale) to `n` places,
     * half away from zero, as a double. `n` below -308 rounds every
     * finite double to (signed) zero — 10**309 already exceeds the
     * largest finite double, so no huge power is ever built. */
    private static double roundExactToDouble(long sign, BigInteger digits,
                                             long scale, long n) {
        if (n < -308) {
            return sign < 0 ? -0.0 : 0.0;
        }
        RoundedDecimal rd = roundDecimal(digits, scale, n);
        if (rd.rounded.signum() == 0) {
            // Preserve a negative zero: round(-0.001, 2) is -0.0.
            return sign < 0 ? -0.0 : 0.0;
        }
        String text = decimalString(rd.rounded, rd.places);
        return Double.parseDouble((sign < 0 ? "-" : "") + text);
    }

    /** newr `round()`: half-away-from-zero (schoolbook), on the
     * EXACT value of the float — not its shortest repr.
     * `round(2.675, 2)` is 2.67, because the float 2.675 is stored
     * slightly below 2.675. `round(x)` returns an `int` (int64
     * range-checked); `round(x, n)` returns a `float`. Non-finite
     * input: `round(inf)` raises (Python's behaviour);
     * `round(inf, n)` returns `inf`, `round(nan, n)` returns `nan`.
     * An int input rounds its exact value too (unlike the JS
     * runtime, which returns an int input unchanged when ndigits
     * is given — `round(5, -1)` is 10 in Python, 5 in JS). */
    public static long roundHalfAway(double x) {
        if (!Double.isFinite(x)) {
            // Python raises the builtin OverflowError here.
            throw codedError("OverflowError",
                "cannot round infinite or NaN value");
        }
        ExactDecimal d = exactDecimal(x);
        RoundedDecimal rd = roundDecimal(d.digits, d.scale, 0);
        return checkInt(d.sign < 0 ? rd.rounded.negate() : rd.rounded);
    }

    public static double roundHalfAway(double x, long n) {
        if (!Double.isFinite(x)) {
            return x;
        }
        ExactDecimal d = exactDecimal(x);
        return roundExactToDouble(d.sign, d.digits, d.scale, n);
    }

    public static long roundHalfAway(long x) {
        return x;
    }

    public static double roundHalfAway(long x, long n) {
        BigInteger digits = BigInteger.valueOf(x).abs();
        return roundExactToDouble(x < 0 ? -1 : 1, digits, 0, n);
    }

    public static long roundHalfAway(boolean x) {
        return x ? 1 : 0;
    }

    public static double roundHalfAway(boolean x, long n) {
        return roundHalfAway(x ? 1L : 0L, n);
    }

    // ---------------------------------------------------------------- conversions

    private static String jsonString(String x) {
        // JSON-style quoting for error messages (mirrors JS
        // JSON.stringify on a string).
        StringBuilder sb = new StringBuilder("\"");
        for (int i = 0; i < x.length(); i++) {
            char c = x.charAt(i);
            switch (c) {
                case '"': sb.append("\\\""); break;
                case '\\': sb.append("\\\\"); break;
                case '\n': sb.append("\\n"); break;
                case '\r': sb.append("\\r"); break;
                case '\t': sb.append("\\t"); break;
                default:
                    if (c < 0x20) {
                        sb.append(String.format("\\u%04x", (int) c));
                    } else {
                        sb.append(c);
                    }
            }
        }
        return sb.append("\"").toString();
    }

    private static final Pattern INT_STRING =
        Pattern.compile("^[+-]?\\d+$");
    private static final Pattern FLOAT_STRING =
        Pattern.compile("^[+-]?(\\d+(\\.\\d*)?|\\.\\d+)([eE][+-]?\\d+)?$");

    /** newr `int(x)`: from str (parses; throws on invalid) or
     * float (truncates toward zero). The result is int64
     * range-checked. `int("3.5")` raises ValueError. */
    public static long toInt(Object x) {
        if (x instanceof Boolean) {
            return (Boolean) x ? 1 : 0;
        }
        if (x instanceof BigInteger) {
            return checkInt((BigInteger) x);
        }
        if (x instanceof Long) {
            return (Long) x;
        }
        if (x instanceof Integer) {
            return ((Integer) x).longValue();
        }
        if (x instanceof Double) {
            double d = (Double) x;
            if (!Double.isFinite(d)) {
                // Python raises the builtin OverflowError here.
                throw codedError("OverflowError",
                    "cannot convert infinite or NaN to int");
            }
            if (d >= 9223372036854775808.0 || d <= -9223372036854775808.0) {
                throw new NewrOverflowError(
                    "integer overflow: " + d + " is outside int64 range");
            }
            return (long) d;
        }
        if (x instanceof String) {
            String t = ((String) x).trim();
            if (INT_STRING.matcher(t).matches()) {
                return checkInt(new BigInteger(t));
            }
            throw codedError("ValueError",
                "invalid literal for int(): " + jsonString((String) x));
        }
        throw new NewrError("cannot convert " + typeName(x) + " to int");
    }

    /** newr `float(x)`: from str or int. `float("inf")` /
     * `float("nan")` are accepted (Python behaviour); a huge int
     * raises OverflowError. */
    public static double toFloat(Object x) {
        if (x instanceof Boolean) {
            return (Boolean) x ? 1.0 : 0.0;
        }
        if (x instanceof BigInteger) {
            BigInteger b = (BigInteger) x;
            if (b.compareTo(MAX_FLOAT_BIGINT) > 0
                || b.compareTo(MAX_FLOAT_BIGINT.negate()) < 0) {
                // Python raises the builtin OverflowError here.
                throw codedError("OverflowError",
                    "int too large to convert to float");
            }
            return b.doubleValue();
        }
        if (x instanceof Long || x instanceof Integer) {
            return ((Number) x).doubleValue();
        }
        if (x instanceof Double) {
            return (Double) x;
        }
        if (x instanceof String) {
            String t = ((String) x).trim().replace("_", "");
            String low = t.toLowerCase();
            if (low.equals("inf") || low.equals("+inf")
                || low.equals("infinity") || low.equals("+infinity")) {
                return Double.POSITIVE_INFINITY;
            }
            if (low.equals("-inf") || low.equals("-infinity")) {
                return Double.NEGATIVE_INFINITY;
            }
            if (low.equals("nan") || low.equals("+nan") || low.equals("-nan")) {
                return Double.NaN;
            }
            if (FLOAT_STRING.matcher(t).matches()) {
                return Double.parseDouble(t);
            }
            throw codedError("ValueError",
                "could not convert string to float: " + jsonString((String) x));
        }
        throw new NewrError("cannot convert " + typeName(x) + " to float");
    }

    /** newr `bool(x)`: from str (`"true"`/`"false"`, exact
     * lowercase) or int (0 -> false). P7: parsing, not truthiness. */
    public static boolean toBool(Object x) {
        if (x instanceof Boolean) {
            return (Boolean) x;
        }
        if (x instanceof String) {
            if (x.equals("true")) {
                return true;
            }
            if (x.equals("false")) {
                return false;
            }
            throw codedError("ValueError",
                "invalid bool: " + jsonString((String) x)
                + " (expected \"true\" or \"false\")");
        }
        if (x instanceof BigInteger) {
            return ((BigInteger) x).signum() != 0;
        }
        if (x instanceof Number) {
            if (x instanceof Double) {
                return (Double) x != 0;
            }
            return ((Number) x).longValue() != 0;
        }
        throw new NewrError("cannot convert " + typeName(x) + " to bool");
    }

    private static String typeName(Object x) {
        if (x == null) {
            return "none";
        }
        if (x instanceof String) {
            return "str";
        }
        if (x instanceof Boolean) {
            return "bool";
        }
        if (x instanceof Double) {
            return "float";
        }
        if (x instanceof Number) {
            return "int";
        }
        if (x instanceof List) {
            return "list";
        }
        if (x instanceof Map) {
            return "dict";
        }
        if (x instanceof Set) {
            return "set";
        }
        return x.getClass().getSimpleName();
    }

    /** Float rendering with PYTHON repr semantics (J4): shortest
     * round-trip digits, scientific notation iff the decimal
     * exponent is < -4 or >= 16, ".0" for integral floats. Java
     * Double.toString differs twice over: its exponent cutoffs
     * differ (1e7 -> "1.0E7", Python "10000000.0"), and its
     * shortest digits differ from Python's in rare edge cases
     * (Double.toString(5e-324) is "4.9E-324", Python repr is
     * "5e-324"). So the digits are derived here from the EXACT
     * decimal expansion: the shortest prefix that parses back to
     * the same double (round to nearest, ties to even — David
     * Gay's dtoa mode 0, the rule behind Python's repr). */
    public static String floatStr(double x) {
        if (Double.isNaN(x)) {
            return "nan";
        }
        if (x == Double.POSITIVE_INFINITY) {
            return "inf";
        }
        if (x == Double.NEGATIVE_INFINITY) {
            return "-inf";
        }
        if (x == 0) {
            return Double.doubleToRawLongBits(x) < 0 ? "-0.0" : "0.0";
        }
        boolean neg = x < 0;
        double ax = Math.abs(x);
        // Exact decimal digits of ax (finite expansion — every
        // binary fraction has one) plus the decimal exponent of
        // the leading digit.
        java.math.BigDecimal exact = new java.math.BigDecimal(ax);
        String digits = exact.unscaledValue().toString();
        int scale = exact.scale();
        int point = digits.length() - scale; // value = digits with . after `point`
        int e10 = point - 1;
        // Strip trailing zeros (they carry no information).
        int end = digits.length();
        while (end > 1 && digits.charAt(end - 1) == '0') {
            end--;
        }
        digits = digits.substring(0, end);
        // Shortest prefix that round-trips.
        String best = null;
        int bestE10 = e10;
        for (int k = 1; k <= digits.length(); k++) {
            String prefix = digits.substring(0, k);
            String rounded = prefix;
            int re10 = e10;
            if (k < digits.length()) {
                String rest = digits.substring(k);
                boolean roundUp;
                char first = rest.charAt(0);
                if (first > '5') {
                    roundUp = true;
                } else if (first < '5') {
                    roundUp = false;
                } else if (rest.chars().skip(1).anyMatch(c -> c != '0')) {
                    roundUp = true; // more than half
                } else {
                    // Exactly half: ties to even.
                    roundUp = (prefix.charAt(prefix.length() - 1) - '0') % 2 == 1;
                }
                if (roundUp) {
                    BigInteger bumped = new BigInteger(prefix).add(BigInteger.ONE);
                    rounded = bumped.toString();
                    if (rounded.length() > prefix.length()) {
                        re10 = e10 + 1; // 999 -> 1000
                    }
                }
            }
            // Does this candidate parse back to ax?
            String cand = toDecimalString(rounded, re10);
            if (Double.parseDouble(cand) == ax) {
                best = rounded;
                bestE10 = re10;
                break;
            }
        }
        if (best == null) {
            // Unreachable: 17 significant digits always round-trip.
            best = digits;
            bestE10 = e10;
        }
        // Strip trailing zeros from the rounded digits (rounding up
        // can produce one, e.g. "10" for 1e-7); the exponent refers
        // to the leading digit and is unaffected.
        int bend = best.length();
        while (bend > 1 && best.charAt(bend - 1) == '0') {
            bend--;
        }
        best = best.substring(0, bend);
        String sign = neg ? "-" : "";
        if (bestE10 < -4 || bestE10 >= 16) {
            // Scientific: d[.ddd]e±XX with at least two exponent digits.
            String mant = best.length() > 1
                ? best.charAt(0) + "." + best.substring(1)
                : best.substring(0, 1);
            String esign = bestE10 < 0 ? "-" : "+";
            String eabs = String.format("%02d", Math.abs(bestE10));
            return sign + mant + "e" + esign + eabs;
        }
        // Fixed: decimal point after bestE10+1 digits.
        int point2 = bestE10 + 1;
        String out;
        if (point2 <= 0) {
            out = "0." + "0".repeat(-point2) + best;
        } else if (point2 >= best.length()) {
            out = best + "0".repeat(point2 - best.length()) + ".0";
        } else {
            out = best.substring(0, point2) + "." + best.substring(point2);
        }
        return sign + out;
    }

    /** Render a digit string + decimal exponent as a plain decimal
     * literal (no exponent) for parsing/comparison. */
    private static String toDecimalString(String digits, int e10) {
        int point = e10 + 1;
        if (point <= 0) {
            return "0." + "0".repeat(-point) + digits;
        }
        if (point >= digits.length()) {
            return digits + "0".repeat(point - digits.length());
        }
        return digits.substring(0, point) + "." + digits.substring(point);
    }

    /** newr `str(x)`: the display rendering (P8). `true`/`false`/
     * `none` are lowercase; floats use Python repr (floatStr);
     * collections render with elements rendered recursively and
     * strings bare (`[1, true, a]`); an `error` renders as its
     * message; a struct/enum value renders via its `toString`
     * (emitted by the compiler for every struct/enum type). */
    public static String toStr(Object x) {
        if (x == null) {
            return "none";
        }
        if (x instanceof Boolean) {
            return (Boolean) x ? "true" : "false";
        }
        if (x instanceof String) {
            return (String) x;
        }
        if (x instanceof BigInteger) {
            return x.toString();
        }
        if (x instanceof Number) {
            if (x instanceof Double || x instanceof Float) {
                return floatStr(((Number) x).doubleValue());
            }
            return x.toString();
        }
        if (x instanceof NewrError) {
            return ((NewrError) x).getMessage();
        }
        if (x instanceof Result) {
            Result<?, ?> r = (Result<?, ?>) x;
            if (r.isOk()) {
                return "ok(" + toStr(r._value) + ")";
            }
            return "err(" + toStr(r._error) + ")";
        }
        if (x instanceof List) {
            List<?> xs = (List<?>) x;
            StringBuilder sb = new StringBuilder("[");
            for (int i = 0; i < xs.size(); i++) {
                if (i > 0) {
                    sb.append(", ");
                }
                sb.append(toStr(xs.get(i)));
            }
            return sb.append("]").toString();
        }
        if (x instanceof Map) {
            Map<?, ?> m = (Map<?, ?>) x;
            StringBuilder sb = new StringBuilder("{");
            boolean first = true;
            for (Map.Entry<?, ?> e : m.entrySet()) {
                if (!first) {
                    sb.append(", ");
                }
                first = false;
                sb.append(toStr(e.getKey())).append(": ").append(toStr(e.getValue()));
            }
            return sb.append("}").toString();
        }
        if (x instanceof Set) {
            Set<?> s = (Set<?>) x;
            StringBuilder sb = new StringBuilder("{");
            boolean first = true;
            for (Object e : s) {
                if (!first) {
                    sb.append(", ");
                }
                first = false;
                sb.append(toStr(e));
            }
            return sb.append("}").toString();
        }
        return String.valueOf(x);
    }

    // ---------------------------------------------------------------- Result

    /** newr `Result[T, E]` (SPEC §10): the value form of errors. */
    public static class Result<T, E> {
        public final boolean _isOk;
        public final T _value;
        public final E _error;

        public Result(boolean isOk, T value, E error) {
            this._isOk = isOk;
            this._value = value;
            this._error = error;
        }

        public boolean isOk() {
            return _isOk;
        }

        public boolean isErr() {
            return !_isOk;
        }

        @SuppressWarnings("unchecked")
        public T unwrap() {
            if (_isOk) {
                return _value;
            }
            if (_error instanceof Throwable) {
                Throwable t = (Throwable) _error;
                if (t instanceof RuntimeException) {
                    throw (RuntimeException) t;
                }
                throw new NewrError(String.valueOf(_error));
            }
            throw new NewrError(String.valueOf(_error));
        }

        public T unwrapOr(T dflt) {
            return _isOk ? _value : dflt;
        }

        @Override
        public boolean equals(Object o) {
            if (this == o) {
                return true;
            }
            if (!(o instanceof Result)) {
                return false;
            }
            Result<?, ?> other = (Result<?, ?>) o;
            if (_isOk != other._isOk) {
                return false;
            }
            return _isOk ? eq(_value, other._value) : eq(_error, other._error);
        }

        @Override
        public int hashCode() {
            // hashOf (not Objects.hashCode): NewrRt.eq treats -0.0
            // and 0.0 as equal, so their hash codes must agree too
            // (the equals/hashCode contract).
            return _isOk ? hashOf(_value) : hashOf(_error);
        }
    }

    /** A hash consistent with `eq`: -0.0 and 0.0 hash alike (eq
     * says they are equal), and collections hash by their elements
     * (a List's hashCode would use Double.hashCode for a -0.0
     * element, which disagrees with eq). NaN needs no agreement:
     * eq says NaN != NaN, so unequal values may share a hash. */
    public static int hashOf(Object x) {
        if (x == null) {
            return 0;
        }
        if (x instanceof Double d) {
            double v = d == 0.0 ? 0.0 : d;  // normalizes -0.0 to 0.0
            return Double.hashCode(v);
        }
        if (x instanceof List<?> l) {
            int h = 1;
            for (Object e : l) {
                h = 31 * h + hashOf(e);
            }
            return h;
        }
        if (x instanceof Map<?, ?> m) {
            int h = 0;
            for (Map.Entry<?, ?> e : m.entrySet()) {
                h += hashOf(e.getKey()) ^ hashOf(e.getValue());
            }
            return h;
        }
        if (x instanceof Set<?> s) {
            int h = 0;
            for (Object e : s) {
                h += hashOf(e);
            }
            return h;
        }
        return x.hashCode();
    }

    /** Hash of a struct/enum's fields, consistent with `eq`. */
    public static int structHash(Object... fields) {
        int h = 1;
        for (Object f : fields) {
            h = 31 * h + hashOf(f);
        }
        return h;
    }

    /** `env.get(name)` / `env.get(name, default)`: null when the
     * variable is absent and no default is given. */
    public static String envGet(String name) {
        return System.getenv(name);
    }

    public static String envGet(String name, String dflt) {
        String v = System.getenv(name);
        return v != null ? v : dflt;
    }

    /** `env.args`: the command-line arguments, set by the
     * generated main() (a module file has no main, so the runtime
     * holds the copy). Defaults to an empty list before main runs.
     * Java's main() args exclude the program name, but newr's
     * env.args is sys.argv 1:1 (Python: sys.argv, JS:
     * process.argv.slice(1)) — argv[0] is the program, so
     * setMainArgs prepends the main class name. */
    public static String[] _mainArgs = new String[0];

    public static void setMainArgs(String prog, String[] args) {
        String[] all = new String[args.length + 1];
        all[0] = prog;
        System.arraycopy(args, 0, all, 1, args.length);
        _mainArgs = all;
    }

    public static List<String> envArgs() {
        return Arrays.asList(_mainArgs);
    }

    public static <T> Result<T, NewrError> ok(T value) {
        return new Result<>(true, value, null);
    }

    public static <T> Result<T, NewrError> err(NewrError error) {
        return new Result<>(false, null, error);
    }

    /** newr `try <expr>`: Ok-wrap the value, catch a thrown error
     * into `err(...)`. The caught error is coerced with `asError`
     * so `.message` / `.code` are always available. */
    public static <T> Result<T, NewrError> tryResult(ThrowingSupplier<T> f) {
        try {
            return ok(f.get());
        } catch (Throwable exc) {
            return err(asError(exc));
        }
    }

    // ---------------------------------------------------------------- equality (J6)

    /** newr `==`: structural equality (§7.7). Primitives compare
     * by value; lists compare element-wise, dicts by key
     * (order-insensitive), sets unordered, structs/enums by
     * constructor + fields (their generated `equals`), Results by
     * kind + payload. Functions and errors compare by identity.
     * NaN is never equal to NaN (not even inside a collection). */
    public static boolean eq(Object a, Object b) {
        if (a == b) {
            // Same reference — but NaN is not == itself even as a
            // boxed Double, and `a == b` on two distinct NaN boxes
            // is false anyway; a single NaN box compared to itself
            // reaches here, so exclude it.
            if (a instanceof Double && Double.isNaN((Double) a)) {
                return false;
            }
            return true;
        }
        if (a == null || b == null) {
            return false;
        }
        if (a instanceof Double || b instanceof Double) {
            if (!(a instanceof Double) || !(b instanceof Double)) {
                return false;
            }
            // Unboxed == : -0.0 == 0.0 is true, NaN == NaN is false
            // (exactly the JS `===` semantics for numbers).
            return (Double) a == (Double) b;
        }
        if (a instanceof List || b instanceof List) {
            if (!(a instanceof List) || !(b instanceof List)) {
                return false;
            }
            List<?> la = (List<?>) a;
            List<?> lb = (List<?>) b;
            if (la.size() != lb.size()) {
                return false;
            }
            for (int i = 0; i < la.size(); i++) {
                if (!eq(la.get(i), lb.get(i))) {
                    return false;
                }
            }
            return true;
        }
        if (a instanceof Map || b instanceof Map) {
            if (!(a instanceof Map) || !(b instanceof Map)) {
                return false;
            }
            Map<?, ?> ma = (Map<?, ?>) a;
            Map<?, ?> mb = (Map<?, ?>) b;
            if (ma.size() != mb.size()) {
                return false;
            }
            for (Map.Entry<?, ?> e : ma.entrySet()) {
                if (!mb.containsKey(e.getKey())
                    || !eq(e.getValue(), mb.get(e.getKey()))) {
                    return false;
                }
            }
            return true;
        }
        if (a instanceof Set || b instanceof Set) {
            if (!(a instanceof Set) || !(b instanceof Set)) {
                return false;
            }
            Set<?> sa = (Set<?>) a;
            Set<?> sb = (Set<?>) b;
            if (sa.size() != sb.size()) {
                return false;
            }
            outer:
            for (Object x : sa) {
                for (Object y : sb) {
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
            Result<?, ?> ra = (Result<?, ?>) a;
            Result<?, ?> rb = (Result<?, ?>) b;
            if (ra._isOk != rb._isOk) {
                return false;
            }
            return ra._isOk ? eq(ra._value, rb._value) : eq(ra._error, rb._error);
        }
        if (a instanceof NewrError || b instanceof NewrError) {
            return false; // errors compare by identity (a == b above)
        }
        return a.equals(b);
    }

    // ---------------------------------------------------------------- ?. / ?? / accessors

    /** newr `?.`: `null` when `v` is `null`, else `f(v)`. The test
     * is `== null`, never truthiness — `0`, `""` and `[]` are
     * values, not absences. A whole `?.` chain collapses into one
     * call with the full postfix path inside `f`, so the base
     * expression is evaluated exactly once. */
    public static <T, R> R optChain(T v, Function<T, R> f) {
        return v == null ? null : f.apply(v);
    }

    /** newr `??`: `v` when it is not `null`, else `defaultFn()`.
     * The default is a thunk so it is evaluated lazily. */
    public static <T> T coalesce(T v, Supplier<T> dflt) {
        return v != null ? v : dflt.get();
    }

    /** newr `list[i]`: SPEC §9.2 — out-of-range indexing is a
     * runtime error. Valid indices are `0 <= i < len(lst)`; a
     * negative index is out of range (never a wrap). */
    public static Object listIndex(List<?> lst, long i) {
        if (i < 0 || i >= lst.size()) {
            throw codedError("IndexError", "list index out of range");
        }
        return lst.get((int) i);
    }

    /** newr `list.get(i, default)`: P9 — a negative index is
     * out-of-range (no wrap-around); anything outside
     * `0 <= i < len(lst)` returns `default`. */
    public static Object listGet(List<?> lst, long i, Object dflt) {
        return i >= 0 && i < lst.size() ? lst.get((int) i) : dflt;
    }

    /** newr `x in [..]`: structural equality (a list of structs
     * contains an equal-but-different instance). */
    public static boolean listContains(List<?> lst, Object x) {
        for (Object e : lst) {
            if (eq(e, x)) {
                return true;
            }
        }
        return false;
    }

    /** newr `set.remove(x)`: removing a missing element is an
     * error (Python's `set.remove` raises KeyError; Java's
     * `Set.remove` silently returns false). */
    public static void setRemove(Set<?> s, Object x) {
        if (!s.contains(x)) {
            throw codedError("KeyError", toStr(x));
        }
        s.remove(x);
    }

    /** newr `T?.unwrap()`: the value, or a thrown error on `null`. */
    public static <T> T unwrapOpt(T v) {
        if (v == null) {
            throw new NewrError("unwrap of none");
        }
        return v;
    }

    /** newr `range(...)`: like Python's range, materialised as a
     * list of longs. range(stop) / range(start, stop) /
     * range(start, stop, step); a zero step is a ValueError. */
    public static List<Long> range(long stop) {
        return range(0, stop, 1);
    }

    public static List<Long> range(long start, long stop) {
        return range(start, stop, 1);
    }

    public static List<Long> range(long start, long stop, long step) {
        if (step == 0) {
            throw codedError("ValueError", "range() arg 3 must not be zero");
        }
        List<Long> out = new ArrayList<>();
        if (step > 0) {
            for (long i = start; i < stop; i += step) {
                out.add(i);
            }
        } else {
            for (long i = start; i > stop; i += step) {
                out.add(i);
            }
        }
        return out;
    }

    /** `len(x)` for any lenable value (str: code points, P5). */
    public static long lenOf(Object x) {
        if (x instanceof String s) {
            return chars(s).size();
        }
        if (x instanceof Collection<?> c) {
            return c.size();
        }
        if (x instanceof Map<?, ?> m) {
            return m.size();
        }
        throw new NewrError("len() of unsupported value: " + x,
                            "TypeError", null);
    }

    /** `{"k": v, ...}` — alternating key/value pairs into a
     * LinkedHashMap (insertion order). Unlike `Map.ofEntries`,
     * null keys and values are allowed. */
    @SuppressWarnings("unchecked")
    public static <V> LinkedHashMap<String, V> dictOf(Object... kvs) {
        LinkedHashMap<String, V> out = new LinkedHashMap<>();
        for (int i = 0; i < kvs.length; i += 2) {
            out.put((String) kvs[i], (V) kvs[i + 1]);
        }
        return out;
    }

    // ---------------------------------------------------------------- strings

    /** Code-point list of a string (SPEC pin P5: `str.chars()`). */
    public static List<String> chars(String s) {
        List<String> out = new ArrayList<>();
        s.codePoints().forEach(cp -> out.add(new String(Character.toChars(cp))));
        return out;
    }

    /** newr string ordering: lexicographic by Unicode code point
     * (SPEC 7.6). Java's String.compareTo compares UTF-16 code
     * units, which orders an astral character below BMP characters
     * at U+E000 and above (a high surrogate is below U+FFFD, while
     * by code points U+1F600 is above U+FFFD). Compare code point
     * by code point; a strict prefix is smaller. */
    public static int strCmp(String a, String b) {
        int[] ac = a.codePoints().toArray();
        int[] bc = b.codePoints().toArray();
        int n = Math.min(ac.length, bc.length);
        for (int i = 0; i < n; i++) {
            int d = ac[i] - bc[i];
            if (d != 0) {
                return d;
            }
        }
        return ac.length - bc.length;
    }

    /** `s.split(sep)` with a LITERAL separator (Java's String.split
     * takes a regex) and Python's trailing-empty-strings behaviour
     * (`split(regex, -1)` keeps them). */
    public static List<String> splitStr(String s, String sep) {
        List<String> out = new ArrayList<>();
        for (String part : s.split(java.util.regex.Pattern.quote(sep), -1)) {
            out.add(part);
        }
        return out;
    }

    /** `s[i]` by code point (P5). Negative indices wrap like
     * Python; out of range raises IndexError (the JS runtime's
     * `[...s][i]` returns undefined there — a JS latent bug). */
    public static String strIndex(String s, long idx) {
        List<String> cps = chars(s);
        long i = idx < 0 ? cps.size() + idx : idx;
        if (i < 0 || i >= cps.size()) {
            throw new NewrError(
                "string index out of range: " + idx,
                "IndexError", null);
        }
        return cps.get((int) i);
    }

    /** `abs(x)` for int64: `Math.abs(Long.MIN_VALUE)` wraps
     * silently in Java, so the minimum is an overflow error
     * (Python: abs(-2**63) = 2**63, out of int64 range). */
    public static long absLong(long x) {
        if (x == Long.MIN_VALUE) {
            throw new NewrOverflowError(
                "integer overflow: 9223372036854775808 is outside int64 range");
        }
        return Math.abs(x);
    }

    // ---------------------------------------------------------------- print / input

    /** newr `print`: every argument rendered with `toStr`, joined
     * with a single space (Python's print). */
    public static void print(Object... args) {
        StringBuilder sb = new StringBuilder();
        for (int i = 0; i < args.length; i++) {
            if (i > 0) {
                sb.append(" ");
            }
            sb.append(toStr(args[i]));
        }
        System.out.println(sb);
    }

    private static BufferedReader stdinReader = null;

    /** newr `input(prompt = "")`: read one line from stdin, like
     * Python's input(). The prompt (if any) is written with no
     * newline first. At EOF, returns "" (mirrors the JS runtime). */
    public static String input(String prompt) {
        if (prompt != null && !prompt.isEmpty()) {
            System.out.print(prompt);
            System.out.flush();
        }
        try {
            if (stdinReader == null) {
                stdinReader = new BufferedReader(new InputStreamReader(
                    System.in, StandardCharsets.UTF_8));
            }
            String line = stdinReader.readLine();
            if (line == null) {
                return "";
            }
            return line;
        } catch (IOException e) {
            return "";
        }
    }

    // ---------------------------------------------------------------- random

    private static final SecureRandom SECURE_RANDOM = new SecureRandom();

    /** newr `random.int(lo, hi)`: inclusive on both ends
     * (Python's randint). Exact for any int64 range: rejection
     * sampling over random bytes (ThreadLocalRandom cannot cover a
     * 64-bit range). */
    public static long randomInt(long lo, long hi) {
        if (hi < lo) {
            throw codedError("ValueError", "empty range for random.int()");
        }
        BigInteger span = BigInteger.valueOf(hi).subtract(BigInteger.valueOf(lo))
            .add(BigInteger.ONE);
        if (span.equals(BigInteger.ONE)) {
            return lo;
        }
        int bits = span.bitLength();
        int bytes = (bits + 7) / 8;
        byte[] buf = new byte[bytes];
        for (;;) {
            SECURE_RANDOM.nextBytes(buf);
            BigInteger v = new BigInteger(1, buf);
            if (v.compareTo(span) < 0) {
                return BigInteger.valueOf(lo).add(v).longValueExact();
            }
        }
    }

    public static <T> T randomChoice(List<T> xs) {
        if (xs.isEmpty()) {
            throw codedError("IndexError",
                "Cannot choose from an empty sequence");
        }
        return xs.get(ThreadLocalRandom.current().nextInt(xs.size()));
    }

    /** Returns null (the newr value `none`) so the call stays
     * usable in expression position, like the JS runtime. */
    public static Object randomShuffle(List<?> xs) {
        Collections.shuffle(xs, ThreadLocalRandom.current());
        return null;
    }

    // ---------------------------------------------------------------- time (P4)

    private static final Set<String> TIME_FORMATS = new HashSet<>(List.of(
        "%Y-%m-%d", "%H:%M", "%Y-%m-%d %H:%M", "%d/%m/%Y", "%H:%M:%S"));

    /** newr `time.format(ts, fmt)`: UTC, never local time (P4) —
     * the same program prints the same output on every machine.
     * The format must be one of the spec's fixed set (§12.2). */
    public static String timeFormat(long ts, String fmt) {
        if (!TIME_FORMATS.contains(fmt)) {
            throw new NewrError("unsupported time format: " + jsonString(fmt));
        }
        ZonedDateTime d = Instant.ofEpochSecond(ts).atZone(ZoneOffset.UTC);
        String y = String.format("%04d", d.getYear());
        String mo = String.format("%02d", d.getMonthValue());
        String day = String.format("%02d", d.getDayOfMonth());
        String h = String.format("%02d", d.getHour());
        String mi = String.format("%02d", d.getMinute());
        String s = String.format("%02d", d.getSecond());
        switch (fmt) {
            case "%Y-%m-%d":
                return y + "-" + mo + "-" + day;
            case "%H:%M":
                return h + ":" + mi;
            case "%Y-%m-%d %H:%M":
                return y + "-" + mo + "-" + day + " " + h + ":" + mi;
            case "%d/%m/%Y":
                return day + "/" + mo + "/" + y;
            case "%H:%M:%S":
                return h + ":" + mi + ":" + s;
            default:
                throw new NewrError("unsupported time format: " + jsonString(fmt));
        }
    }

    // ---------------------------------------------------------------- file I/O

    /** newr `file.read(path)`: the whole file as UTF-8 text. A
     * missing file throws a NewrError whose code is the host
     * exception name (Java: "NoSuchFileException"; the Python
     * target reports "FileNotFoundError" there, the JS target
     * "ENOENT"; the code for host IO errors is host-specific,
     * BUILD_REPORT §21). Unchecked, so emitted code needs no
     * try/catch around a plain read. */
    public static String fileRead(String path) {
        try {
            return Files.readString(Path.of(path), StandardCharsets.UTF_8);
        } catch (IOException e) {
            throw asError(e);
        }
    }

    /** newr `file.write(path, content)`: overwrite (create if
     * needed). Returns null (the newr value `none`). */
    public static Object fileWrite(String path, String content) {
        try {
            Files.writeString(Path.of(path), content, StandardCharsets.UTF_8,
                java.nio.file.StandardOpenOption.CREATE,
                java.nio.file.StandardOpenOption.TRUNCATE_EXISTING);
        } catch (IOException e) {
            throw asError(e);
        }
        return null;
    }

    /** newr `file.append(path, content)`: append (create if
     * needed). Returns null (the newr value `none`). */
    public static Object fileAppend(String path, String content) {
        try {
            Files.writeString(Path.of(path), content, StandardCharsets.UTF_8,
                java.nio.file.StandardOpenOption.CREATE,
                java.nio.file.StandardOpenOption.APPEND);
        } catch (IOException e) {
            throw asError(e);
        }
        return null;
    }

    /** newr `file.exists(path)`. */
    public static boolean fileExists(String path) {
        return Files.exists(Path.of(path));
    }
}
