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
// in the newr repo and rebuild. The ui section at the end uses
// Swing (java.desktop, still JDK stdlib) for its native window;
// render-only programs never touch it.
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

import java.awt.Component;
import java.awt.Container;
import java.awt.HeadlessException;
import java.awt.event.FocusAdapter;
import java.awt.event.FocusEvent;
import java.awt.event.WindowAdapter;
import java.awt.event.WindowEvent;
import java.io.BufferedReader;
import java.io.IOException;
import java.io.InputStreamReader;
import java.io.OutputStream;
import java.math.BigInteger;
import java.net.InetSocketAddress;
import java.net.URI;
import java.net.URISyntaxException;
import java.net.URLDecoder;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.ByteBuffer;
import java.nio.charset.CharacterCodingException;
import java.nio.charset.CodingErrorAction;
import java.nio.charset.CharsetDecoder;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.security.SecureRandom;
import java.time.Duration;
import java.time.Instant;
import java.time.ZoneOffset;
import java.time.ZonedDateTime;
import java.sql.Connection;
import java.sql.DriverManager;
import java.sql.PreparedStatement;
import java.sql.ResultSet;
import java.sql.ResultSetMetaData;
import java.sql.SQLException;
import java.sql.Statement;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.Collection;
import java.util.Collections;
import java.util.HashSet;
import java.util.LinkedHashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.Executors;
import java.util.concurrent.ThreadLocalRandom;
import java.util.function.Function;
import java.util.function.Supplier;
import java.util.regex.Pattern;
import javax.swing.BoxLayout;
import javax.swing.JButton;
import javax.swing.JComponent;
import javax.swing.JFrame;
import javax.swing.JLabel;
import javax.swing.JPanel;
import javax.swing.JTextField;
import javax.swing.SwingUtilities;
import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpServer;

public class NewrRt {

    static {
        // Prefer the IPv4 stack for this VM's networking: Java's
        // default dual-stack sockets to 127.0.0.1 connect but never
        // deliver data here (verified live 10 Nov 2026: a plain
        // Socket to a local HttpServer connects with an IPv6 local
        // address and the read blocks until reset, while
        // -Djava.net.preferIPv4Stack=true works at once). The
        // property must be set before any socket is created, so it
        // is set when this class loads, which is before any newr
        // program can reach the network (only the http helpers use
        // sockets). An explicit user setting is never overridden.
        // The runtime only ever fetches localhost, so preferring
        // IPv4 changes nothing on a normal machine.
        if (System.getProperty("java.net.preferIPv4Stack") == null) {
            System.setProperty("java.net.preferIPv4Stack", "true");
        }
    }

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

    // ---------------------------------------------------------------- db module
    // SQLite via JDBC (SPEC.md section 12.5). The driver itself
    // (org.xerial:sqlite-jdbc, 3.54.1.0) is a runtime dependency
    // only: this file compiles against java.sql alone, and
    // `Class.forName("org.sqlite.JDBC")` loads the driver at
    // runtime. Handles are ints, fd-style: ids from 1, a registry
    // maps each id to its Connection. `?` binding is mandatory:
    // values are never interpolated into the SQL text. The
    // connection stays in autocommit, so `db.begin` runs an
    // explicit BEGIN IMMEDIATE (the store's pattern) and a bare
    // `db.exec` commits at once, mirroring the Python target.

    private static final Map<Long, Connection> _dbConnections =
        new LinkedHashMap<>();
    private static long _dbNextId = 1;

    /** newr `db.open(path)`: open (create if needed) and return a handle. */
    public static long dbOpen(String path) {
        try {
            Class.forName("org.sqlite.JDBC");
            Connection conn =
                DriverManager.getConnection("jdbc:sqlite:" + path);
            try (Statement st = conn.createStatement()) {
                st.execute("PRAGMA foreign_keys = ON");
                st.execute("PRAGMA busy_timeout = 5000");
            }
            long handle = _dbNextId;
            _dbNextId += 1;
            _dbConnections.put(handle, conn);
            return handle;
        } catch (ClassNotFoundException | SQLException e) {
            throw asError(e);
        }
    }

    private static Connection _dbConn(long handle) {
        Connection conn = _dbConnections.get(handle);
        if (conn == null) {
            throw new NewrError("db: unknown or closed handle " + handle);
        }
        return conn;
    }

    private static void _dbBind(PreparedStatement ps, int index,
                                Object value) throws SQLException {
        // Bind by newr value type: long as INTEGER, double as REAL,
        // String as TEXT, boolean as 0/1 (JDBC has no boolean column
        // type in SQLite). A value of any other type is a caller bug.
        if (value instanceof Long v) {
            ps.setLong(index, v);
        } else if (value instanceof Double v) {
            ps.setDouble(index, v);
        } else if (value instanceof String v) {
            ps.setString(index, v);
        } else if (value instanceof Boolean v) {
            ps.setInt(index, v ? 1 : 0);
        } else {
            throw new NewrError("db: cannot bind a "
                + value.getClass().getSimpleName() + " value");
        }
    }

    private static Object _dbValue(Object value, String typeStr) {
        // Convert one raw SQLite value to the newr value for a
        // `db.query` field type (`int`, `float`, `str`, `bool`,
        // each optionally `?`). Mirrors the Python `_db_value`
        // (newr_rt.py): NULL lands only in a nullable field, and
        // a value that cannot convert throws instead of silently
        // producing a wrong value.
        boolean nullable = typeStr.endsWith("?");
        String base = nullable ? typeStr.substring(0, typeStr.length() - 1)
            : typeStr;
        if (value == null) {
            if (nullable) {
                return null;
            }
            throw new NewrError("db: NULL in non-nullable " + base
                + " field");
        }
        switch (base) {
            case "int":
                if (value instanceof Boolean) {
                    throw new NewrError("db: cannot convert bool to int");
                }
                if (value instanceof Number n) {
                    double d = n.doubleValue();
                    if (d != Math.floor(d) || d < (double) Long.MIN_VALUE
                            || d > (double) Long.MAX_VALUE) {
                        throw new NewrError("db: cannot convert " + value
                            + " to int");
                    }
                    return checkInt(n.longValue());
                }
                if (value instanceof String s) {
                    try {
                        return checkInt(Long.parseLong(s.strip()));
                    } catch (NumberFormatException e) {
                        throw new NewrError("db: cannot convert " + value
                            + " to int");
                    }
                }
                throw new NewrError("db: cannot convert "
                    + value.getClass().getSimpleName() + " to int");
            case "float":
                if (value instanceof Boolean) {
                    throw new NewrError("db: cannot convert bool to float");
                }
                if (value instanceof Number n) {
                    return n.doubleValue();
                }
                if (value instanceof String s) {
                    try {
                        return Double.parseDouble(s.strip());
                    } catch (NumberFormatException e) {
                        throw new NewrError("db: cannot convert " + value
                            + " to float");
                    }
                }
                throw new NewrError("db: cannot convert "
                    + value.getClass().getSimpleName() + " to float");
            case "str":
                if (value instanceof Boolean b) {
                    return b ? "true" : "false";
                }
                if (value instanceof String s) {
                    return s;
                }
                return toStr(value);
            case "bool":
                if (value instanceof Boolean b) {
                    return b;
                }
                if (value instanceof Number n) {
                    long v = n.longValue();
                    if (v == 0) {
                        return false;
                    }
                    if (v == 1) {
                        return true;
                    }
                    throw new NewrError("db: cannot convert " + value
                        + " to bool");
                }
                if (value instanceof String s) {
                    if (s.equals("0")) {
                        return false;
                    }
                    if (s.equals("1")) {
                        return true;
                    }
                    throw new NewrError("db: cannot convert " + value
                        + " to bool");
                }
                throw new NewrError("db: cannot convert "
                    + value.getClass().getSimpleName() + " to bool");
            default:
                throw new NewrError("db: unknown field type " + typeStr);
        }
    }

    /** newr `db.exec(handle, sql, args...)`: run one statement,
     * return the rows affected (0 for DDL). */
    public static long dbExec(long handle, String sql, Object[] args) {
        try {
            PreparedStatement ps =
                _dbConn(handle).prepareStatement(sql);
            try {
                for (int i = 0; i < args.length; i++) {
                    _dbBind(ps, i + 1, args[i]);
                }
                return ps.executeUpdate();
            } finally {
                ps.close();
            }
        } catch (SQLException e) {
            throw asError(e);
        }
    }

    /** newr `db.query(handle, sql, args...)`: run a SELECT and
     * return one list per row, converted values in field order. A
     * missing column throws; extra columns are ignored. */
    public static List<Object[]> dbQuery(long handle, String sql,
            Object[] args, String[] fieldNames, String[] fieldTypes) {
        try {
            PreparedStatement ps =
                _dbConn(handle).prepareStatement(sql);
            try {
                for (int i = 0; i < args.length; i++) {
                    _dbBind(ps, i + 1, args[i]);
                }
                List<Object[]> rows = new ArrayList<>();
                try (ResultSet rs = ps.executeQuery()) {
                    ResultSetMetaData meta = rs.getMetaData();
                    java.util.Set<String> cols = new java.util.HashSet<>();
                    for (int i = 1; i <= meta.getColumnCount(); i++) {
                        cols.add(meta.getColumnName(i));
                    }
                    while (rs.next()) {
                        Object[] row = new Object[fieldNames.length];
                        for (int i = 0; i < fieldNames.length; i++) {
                            if (!cols.contains(fieldNames[i])) {
                                throw new NewrError("db: column '"
                                    + fieldNames[i]
                                    + "' missing from query result");
                            }
                            row[i] = _dbValue(rs.getObject(fieldNames[i]),
                                fieldTypes[i]);
                        }
                        rows.add(row);
                    }
                }
                return rows;
            } finally {
                ps.close();
            }
        } catch (SQLException e) {
            throw asError(e);
        }
    }

    /** newr `db.begin(handle)`: BEGIN IMMEDIATE. */
    public static Object dbBegin(long handle) {
        try {
            try (Statement st = _dbConn(handle).createStatement()) {
                st.execute("BEGIN IMMEDIATE");
            }
        } catch (SQLException e) {
            throw asError(e);
        }
        return null;
    }

    /** newr `db.commit(handle)`: COMMIT. */
    public static Object dbCommit(long handle) {
        try {
            try (Statement st = _dbConn(handle).createStatement()) {
                st.execute("COMMIT");
            }
        } catch (SQLException e) {
            throw asError(e);
        }
        return null;
    }

    /** newr `db.rollback(handle)`: ROLLBACK. */
    public static Object dbRollback(long handle) {
        try {
            try (Statement st = _dbConn(handle).createStatement()) {
                st.execute("ROLLBACK");
            }
        } catch (SQLException e) {
            throw asError(e);
        }
        return null;
    }

    /** newr `db.last_id(handle)`: the last INSERT rowid. */
    public static long dbLastId(long handle) {
        try {
            try (Statement st = _dbConn(handle).createStatement();
                 ResultSet rs =
                     st.executeQuery("SELECT last_insert_rowid()")) {
                rs.next();
                return checkInt(rs.getLong(1));
            }
        } catch (SQLException e) {
            throw asError(e);
        }
    }

    /** newr `db.close(handle)`: close and drop the handle. A second
     * close (or any use after close) raises NewrError. */
    public static Object dbClose(long handle) {
        Connection conn = _dbConnections.remove(handle);
        if (conn == null) {
            throw new NewrError("db: unknown or closed handle " + handle);
        }
        try {
            conn.close();
        } catch (SQLException e) {
            throw asError(e);
        }
        return null;
    }

    // ---------------------------------------------------------------- json
    // Typed JSON (SPEC.md section 12.7), hand-written strict RFC 8259
    // parser and serializer: the JDK has no JSON library. The emitter
    // passes a recursive shape descriptor built from the static type:
    // a scalar is the String "int" / "float" / "str" / "bool" /
    // "none", a nullable is Object[]{"?", inner}, a list is
    // Object[]{"list", elem}, a dict is Object[]{"dict", value}, a
    // struct is Object[]{"struct", factory, Object[][] fields} where
    // factory is a Function<Object[], Object> building the struct
    // from its field values in declaration order, and each field is
    // Object[]{newrName, javaFieldName, shape}. The runtime walks
    // the shape for both directions. Java longs are 64-bit, so big
    // ints round-trip exactly; integral floats render with ".0"
    // through floatStr (Python repr semantics, already in this file).

    private static String _jsonEscape(String s) {
        StringBuilder sb = new StringBuilder();
        sb.append('"');
        for (int i = 0; i < s.length(); i++) {
            char c = s.charAt(i);
            switch (c) {
                case '"': sb.append("\\\""); break;
                case '\\': sb.append("\\\\"); break;
                case '\b': sb.append("\\b"); break;
                case '\f': sb.append("\\f"); break;
                case '\n': sb.append("\\n"); break;
                case '\r': sb.append("\\r"); break;
                case '\t': sb.append("\\t"); break;
                default:
                    if (c < 0x20) {
                        sb.append(String.format("\\u%04x", (int) c));
                    } else {
                        sb.append(c);
                    }
                    break;
            }
        }
        sb.append('"');
        return sb.toString();
    }

    private static Object _structField(Object struct, String javaName) {
        try {
            return struct.getClass().getField(javaName).get(struct);
        } catch (NoSuchFieldException e) {
            try {
                return struct.getClass().getField(javaName + "_")
                    .get(struct);
            } catch (ReflectiveOperationException e2) {
                throw new NewrError("json: struct has no field "
                    + javaName);
            }
        } catch (ReflectiveOperationException e) {
            throw new NewrError("json: cannot read field " + javaName);
        }
    }

    @SuppressWarnings("unchecked")
    private static void _jsonEncodeValue(StringBuilder sb, Object value,
            Object shape) {
        if (shape instanceof String s) {
            switch (s) {
                case "int":
                    if (value instanceof Boolean) {
                        throw new NewrError("json: expected int, "
                            + "got bool");
                    }
                    if (value instanceof Long l) {
                        sb.append(l.toString());
                        return;
                    }
                    if (value instanceof BigInteger bi) {
                        if (bi.compareTo(INT64_MIN_BI) < 0
                                || bi.compareTo(INT64_MAX_BI) > 0) {
                            throw new NewrError("json: integer outside "
                                + "int64 range");
                        }
                        sb.append(bi.toString());
                        return;
                    }
                    throw new NewrError("json: expected int, got "
                        + (value == null ? "null"
                            : value.getClass().getSimpleName()));
                case "float":
                    if (value instanceof Boolean) {
                        throw new NewrError("json: expected float, "
                            + "got bool");
                    }
                    if (value instanceof Number n) {
                        double d = n.doubleValue();
                        if (Double.isNaN(d) || Double.isInfinite(d)) {
                            throw new NewrError("json: cannot encode "
                                + "a non-finite float");
                        }
                        sb.append(floatStr(d));
                        return;
                    }
                    throw new NewrError("json: expected float, got "
                        + (value == null ? "null"
                            : value.getClass().getSimpleName()));
                case "str":
                    if (!(value instanceof String str)) {
                        throw new NewrError("json: expected str, got "
                            + (value == null ? "null"
                                : value.getClass().getSimpleName()));
                    }
                    sb.append(_jsonEscape(str));
                    return;
                case "bool":
                    if (!(value instanceof Boolean b)) {
                        throw new NewrError("json: expected bool, got "
                            + (value == null ? "null"
                                : value.getClass().getSimpleName()));
                    }
                    sb.append(b ? "true" : "false");
                    return;
                case "none":
                    if (value != null) {
                        throw new NewrError("json: expected null");
                    }
                    sb.append("null");
                    return;
                default:
                    throw new NewrError("json: unknown shape " + s);
            }
        }
        if (!(shape instanceof Object[] arr) || arr.length == 0
                || !(arr[0] instanceof String kind)) {
            throw new NewrError("json: unknown shape");
        }
        switch (kind) {
            case "?":
                if (value == null) {
                    sb.append("null");
                } else {
                    _jsonEncodeValue(sb, value, arr[1]);
                }
                return;
            case "list": {
                if (!(value instanceof List<?> list)) {
                    throw new NewrError("json: expected list, got "
                        + (value == null ? "null"
                            : value.getClass().getSimpleName()));
                }
                sb.append('[');
                for (int i = 0; i < list.size(); i++) {
                    if (i > 0) {
                        sb.append(',');
                    }
                    _jsonEncodeValue(sb, list.get(i), arr[1]);
                }
                sb.append(']');
                return;
            }
            case "dict": {
                if (!(value instanceof Map<?, ?> map)) {
                    throw new NewrError("json: expected dict, got "
                        + (value == null ? "null"
                            : value.getClass().getSimpleName()));
                }
                sb.append('{');
                boolean first = true;
                for (Map.Entry<?, ?> e : map.entrySet()) {
                    if (!(e.getKey() instanceof String key)) {
                        throw new NewrError("json: dict keys must "
                            + "be str");
                    }
                    if (!first) {
                        sb.append(',');
                    }
                    first = false;
                    sb.append(_jsonEscape(key));
                    sb.append(':');
                    _jsonEncodeValue(sb, e.getValue(), arr[1]);
                }
                sb.append('}');
                return;
            }
            case "struct": {
                if (value == null) {
                    throw new NewrError("json: expected object, "
                        + "got null");
                }
                Object[][] fields = (Object[][]) arr[2];
                sb.append('{');
                for (int i = 0; i < fields.length; i++) {
                    if (i > 0) {
                        sb.append(',');
                    }
                    String name = (String) fields[i][0];
                    String javaName = (String) fields[i][1];
                    sb.append(_jsonEscape(name));
                    sb.append(':');
                    _jsonEncodeValue(sb, _structField(value, javaName),
                        fields[i][2]);
                }
                sb.append('}');
                return;
            }
            default:
                throw new NewrError("json: unknown shape " + kind);
        }
    }

    /** newr `json.encode(value)`: JSON text, compact, in struct
     * declaration order, UTF-8 output with no \\u escapes for
     * non-ASCII text. */
    public static String jsonEncode(Object value, Object shape) {
        StringBuilder sb = new StringBuilder();
        _jsonEncodeValue(sb, value, shape);
        return sb.toString();
    }

    /** A strict RFC 8259 parser. Parsed values are String,
     * Boolean, null, Long (integer syntax in int64 range),
     * BigInteger (integer syntax outside int64 range, so the shape
     * check can throw instead of wrapping), Double (fraction or
     * exponent syntax, so a JSON float into an int field always
     * throws, including integral 1.0), List, or LinkedHashMap
     * (duplicate keys: last wins). */
    private static class _JsonParser {
        private final String text;
        private int pos;

        _JsonParser(String text) {
            this.text = text;
            this.pos = 0;
        }

        Object parse() {
            Object value = parseValue();
            skipWs();
            if (pos != text.length()) {
                throw new NewrError("json: trailing data after the "
                    + "JSON value");
            }
            return value;
        }

        private void skipWs() {
            while (pos < text.length()) {
                char c = text.charAt(pos);
                if (c == ' ' || c == '\t' || c == '\n' || c == '\r') {
                    pos++;
                } else {
                    break;
                }
            }
        }

        private Object parseValue() {
            skipWs();
            if (pos >= text.length()) {
                throw new NewrError("json: unexpected end of JSON");
            }
            char c = text.charAt(pos);
            switch (c) {
                case '{': return parseObject();
                case '[': return parseArray();
                case '"': return parseString();
                case 't': return parseLiteral("true", Boolean.TRUE);
                case 'f': return parseLiteral("false", Boolean.FALSE);
                case 'n': return parseLiteral("null", null);
                default:
                    if (c == '-' || (c >= '0' && c <= '9')) {
                        return parseNumber();
                    }
                    throw new NewrError("json: unexpected character "
                        + "'" + c + "' in JSON");
            }
        }

        private Object parseLiteral(String word, Object value) {
            if (!text.startsWith(word, pos)) {
                throw new NewrError("json: invalid JSON literal");
            }
            pos += word.length();
            return value;
        }

        private LinkedHashMap<String, Object> parseObject() {
            LinkedHashMap<String, Object> map = new LinkedHashMap<>();
            pos++;
            skipWs();
            if (pos < text.length() && text.charAt(pos) == '}') {
                pos++;
                return map;
            }
            while (true) {
                skipWs();
                if (pos >= text.length() || text.charAt(pos) != '"') {
                    throw new NewrError("json: expected a string key "
                        + "in an object");
                }
                String key = parseString();
                skipWs();
                if (pos >= text.length() || text.charAt(pos) != ':') {
                    throw new NewrError("json: expected ':' in an "
                        + "object");
                }
                pos++;
                map.put(key, parseValue());
                skipWs();
                if (pos >= text.length()) {
                    throw new NewrError("json: unterminated object");
                }
                char c = text.charAt(pos);
                if (c == ',') {
                    pos++;
                    continue;
                }
                if (c == '}') {
                    pos++;
                    return map;
                }
                throw new NewrError("json: expected ',' or '}' in an "
                    + "object");
            }
        }

        private ArrayList<Object> parseArray() {
            ArrayList<Object> list = new ArrayList<>();
            pos++;
            skipWs();
            if (pos < text.length() && text.charAt(pos) == ']') {
                pos++;
                return list;
            }
            while (true) {
                list.add(parseValue());
                skipWs();
                if (pos >= text.length()) {
                    throw new NewrError("json: unterminated array");
                }
                char c = text.charAt(pos);
                if (c == ',') {
                    pos++;
                    continue;
                }
                if (c == ']') {
                    pos++;
                    return list;
                }
                throw new NewrError("json: expected ',' or ']' in an "
                    + "array");
            }
        }

        private String parseString() {
            StringBuilder sb = new StringBuilder();
            pos++;
            while (true) {
                if (pos >= text.length()) {
                    throw new NewrError("json: unterminated string");
                }
                char c = text.charAt(pos);
                if (c == '"') {
                    pos++;
                    return sb.toString();
                }
                if (c == '\\') {
                    pos++;
                    if (pos >= text.length()) {
                        throw new NewrError("json: unterminated "
                            + "escape");
                    }
                    char e = text.charAt(pos);
                    pos++;
                    switch (e) {
                        case '"': sb.append('"'); break;
                        case '\\': sb.append('\\'); break;
                        case '/': sb.append('/'); break;
                        case 'b': sb.append('\b'); break;
                        case 'f': sb.append('\f'); break;
                        case 'n': sb.append('\n'); break;
                        case 'r': sb.append('\r'); break;
                        case 't': sb.append('\t'); break;
                        case 'u': {
                            if (pos + 4 > text.length()) {
                                throw new NewrError("json: bad \\u "
                                    + "escape");
                            }
                            int code;
                            try {
                                code = Integer.parseInt(
                                    text.substring(pos, pos + 4), 16);
                            } catch (NumberFormatException ex) {
                                throw new NewrError("json: bad \\u "
                                    + "escape");
                            }
                            pos += 4;
                            sb.append((char) code);
                            break;
                        }
                        default:
                            throw new NewrError("json: bad escape "
                                + "'\\" + e + "'");
                    }
                    continue;
                }
                if (c < 0x20) {
                    throw new NewrError("json: unescaped control "
                        + "character in a string");
                }
                sb.append(c);
                pos++;
            }
        }

        private Object parseNumber() {
            int start = pos;
            if (pos < text.length() && text.charAt(pos) == '-') {
                pos++;
            }
            if (pos >= text.length()) {
                throw new NewrError("json: bad number");
            }
            if (text.charAt(pos) == '0') {
                pos++;
            } else if (text.charAt(pos) >= '1'
                    && text.charAt(pos) <= '9') {
                while (pos < text.length()
                        && text.charAt(pos) >= '0'
                        && text.charAt(pos) <= '9') {
                    pos++;
                }
            } else {
                throw new NewrError("json: bad number");
            }
            boolean isFloat = false;
            if (pos < text.length() && text.charAt(pos) == '.') {
                isFloat = true;
                pos++;
                if (pos >= text.length()
                        || text.charAt(pos) < '0'
                        || text.charAt(pos) > '9') {
                    throw new NewrError("json: bad number");
                }
                while (pos < text.length()
                        && text.charAt(pos) >= '0'
                        && text.charAt(pos) <= '9') {
                    pos++;
                }
            }
            if (pos < text.length()
                    && (text.charAt(pos) == 'e'
                        || text.charAt(pos) == 'E')) {
                isFloat = true;
                pos++;
                if (pos < text.length()
                        && (text.charAt(pos) == '+'
                            || text.charAt(pos) == '-')) {
                    pos++;
                }
                if (pos >= text.length()
                        || text.charAt(pos) < '0'
                        || text.charAt(pos) > '9') {
                    throw new NewrError("json: bad number");
                }
                while (pos < text.length()
                        && text.charAt(pos) >= '0'
                        && text.charAt(pos) <= '9') {
                    pos++;
                }
            }
            String num = text.substring(start, pos);
            if (isFloat) {
                return Double.parseDouble(num);
            }
            try {
                return Long.parseLong(num);
            } catch (NumberFormatException e) {
                return new BigInteger(num);
            }
        }
    }

    @SuppressWarnings("unchecked")
    private static Object _jsonDecodeValue(Object value, Object shape) {
        if (shape instanceof String s) {
            switch (s) {
                case "int":
                    if (value instanceof Long l) {
                        return checkInt(l);
                    }
                    if (value instanceof BigInteger) {
                        throw new NewrError("json: integer outside "
                            + "int64 range");
                    }
                    throw new NewrError("json: expected int, got "
                        + _jsonValueKind(value));
                case "float":
                    if (value instanceof Boolean) {
                        throw new NewrError("json: expected float, "
                            + "got bool");
                    }
                    if (value instanceof Long l) {
                        return (double) l;
                    }
                    if (value instanceof BigInteger bi) {
                        double d = bi.doubleValue();
                        if (Double.isInfinite(d)) {
                            throw new NewrError("json: integer too "
                                + "large for float");
                        }
                        return d;
                    }
                    if (value instanceof Double d) {
                        return d;
                    }
                    throw new NewrError("json: expected float, got "
                        + _jsonValueKind(value));
                case "str":
                    if (!(value instanceof String str)) {
                        throw new NewrError("json: expected str, got "
                            + _jsonValueKind(value));
                    }
                    return str;
                case "bool":
                    if (!(value instanceof Boolean b)) {
                        throw new NewrError("json: expected bool, got "
                            + _jsonValueKind(value));
                    }
                    return b;
                case "none":
                    if (value != null) {
                        throw new NewrError("json: expected null, got "
                            + _jsonValueKind(value));
                    }
                    return null;
                default:
                    throw new NewrError("json: unknown shape " + s);
            }
        }
        if (!(shape instanceof Object[] arr) || arr.length == 0
                || !(arr[0] instanceof String kind)) {
            throw new NewrError("json: unknown shape");
        }
        switch (kind) {
            case "?":
                if (value == null) {
                    return null;
                }
                return _jsonDecodeValue(value, arr[1]);
            case "list": {
                if (!(value instanceof List<?> list)) {
                    throw new NewrError("json: expected list, got "
                        + _jsonValueKind(value));
                }
                ArrayList<Object> out = new ArrayList<>();
                for (Object item : list) {
                    out.add(_jsonDecodeValue(item, arr[1]));
                }
                return out;
            }
            case "dict": {
                if (!(value instanceof Map<?, ?> map)) {
                    throw new NewrError("json: expected dict, got "
                        + _jsonValueKind(value));
                }
                LinkedHashMap<String, Object> out =
                    new LinkedHashMap<>();
                for (Map.Entry<?, ?> e : map.entrySet()) {
                    out.put((String) e.getKey(),
                        _jsonDecodeValue(e.getValue(), arr[1]));
                }
                return out;
            }
            case "struct": {
                if (!(value instanceof Map<?, ?> map)) {
                    throw new NewrError("json: expected object, got "
                        + _jsonValueKind(value));
                }
                Object[][] fields = (Object[][]) arr[2];
                Object[] vals = new Object[fields.length];
                for (int i = 0; i < fields.length; i++) {
                    String name = (String) fields[i][0];
                    if (!map.containsKey(name)) {
                        throw new NewrError("json: missing field '"
                            + name + "'");
                    }
                    vals[i] = _jsonDecodeValue(map.get(name),
                        fields[i][2]);
                }
                Function<Object[], Object> factory =
                    (Function<Object[], Object>) arr[1];
                return factory.apply(vals);
            }
            default:
                throw new NewrError("json: unknown shape " + kind);
        }
    }

    private static String _jsonValueKind(Object value) {
        if (value == null) {
            return "null";
        }
        if (value instanceof Long || value instanceof BigInteger) {
            return "int";
        }
        if (value instanceof Double) {
            return "float";
        }
        if (value instanceof String) {
            return "str";
        }
        if (value instanceof Boolean) {
            return "bool";
        }
        if (value instanceof List) {
            return "list";
        }
        if (value instanceof Map) {
            return "dict";
        }
        return value.getClass().getSimpleName();
    }

    /** newr `json.decode(text)`: the typed value for ``text`` under
     * ``shape``. An empty string throws, trailing data after the
     * JSON value throws, duplicate keys keep the last value, a JSON
     * fraction into an int field throws, an integer into a float
     * field is fine, null only into a nullable shape, and an
     * integer outside int64 throws. */
    public static Object jsonDecode(String text, Object shape) {
        if (text == null) {
            throw new NewrError("json: decode takes a string");
        }
        Object parsed = new _JsonParser(text).parse();
        return _jsonDecodeValue(parsed, shape);
    }

    private static Object _buildFromShape(Object shape,
            Map<String, Object> values) {
        return _jsonDecodeValue(values, shape);
    }

    // ---------------------------------------------------------------- http
    // Sync HTTP server plus fetch client (SPEC.md section 12.6),
    // JDK only: `com.sun.net.httpserver.HttpServer` for serve,
    // `java.net.http.HttpClient` for fetch. Routes live in a
    // registry, exact-match only; a request with no route, or a
    // method mismatch, gets 404 with an empty body (not 405).
    // The runtime enforces the DEFAULT localhost allowlist only
    // (localhost, 127.0.0.1); the manifest http_allowlist is a
    // static checker concern, the CLI does not pass it through.

    private static final LinkedHashSet<String> _HTTP_METHODS =
        new LinkedHashSet<>(List.of("GET", "POST", "PUT", "PATCH",
            "DELETE", "HEAD", "OPTIONS"));
    private static final LinkedHashSet<String> _DEFAULT_HTTP_ALLOWLIST =
        new LinkedHashSet<>(List.of("localhost", "127.0.0.1"));

    private static class _HttpRoute {
        final String method;
        final String path;
        final Object handler;
        final Object[] reqShape;

        _HttpRoute(String method, String path, Object handler,
                Object[] reqShape) {
            this.method = method;
            this.path = path;
            this.handler = handler;
            this.reqShape = reqShape;
        }
    }

    private static final List<_HttpRoute> _HTTP_ROUTES =
        new ArrayList<>();

    /** newr `http.route(method, path, handler)`: register one
     * exact-match route. A computed method that is not one of the
     * uppercase methods, or a computed path without a leading
     * slash, throws here (literal ones are E2012 from the
     * checker). */
    public static Object httpRoute(String method, String path,
            Object handler, Object[] reqShape) {
        if (method == null || !_HTTP_METHODS.contains(method)) {
            throw new NewrError("http: invalid method " + method);
        }
        if (path == null || !path.startsWith("/")) {
            throw new NewrError("http: route path must start with "
                + "'/', got " + path);
        }
        if (!(handler instanceof Fn1)) {
            throw new NewrError("http: route handler must be a "
                + "lambda");
        }
        if (reqShape == null) {
            throw new NewrError("http: internal: route without a "
                + "request shape");
        }
        _HTTP_ROUTES.add(new _HttpRoute(method, path, handler,
            reqShape));
        return null;
    }

    private static String _decodeUtf8Strict(byte[] bytes) {
        CharsetDecoder decoder = StandardCharsets.UTF_8.newDecoder()
            .onMalformedInput(CodingErrorAction.REPORT)
            .onUnmappableCharacter(CodingErrorAction.REPORT);
        try {
            return decoder.decode(ByteBuffer.wrap(bytes)).toString();
        } catch (CharacterCodingException e) {
            throw new NewrError("http: body is not valid UTF-8");
        }
    }

    private static LinkedHashMap<String, String> _parseQuery(
            String rawQuery) {
        LinkedHashMap<String, String> query = new LinkedHashMap<>();
        if (rawQuery == null || rawQuery.isEmpty()) {
            return query;
        }
        for (String part : rawQuery.split("&", -1)) {
            if (part.isEmpty()) {
                continue;
            }
            int eq = part.indexOf('=');
            String key;
            String value;
            if (eq < 0) {
                key = part;
                value = "";
            } else {
                key = part.substring(0, eq);
                value = part.substring(eq + 1);
            }
            // URLDecoder decodes "+" as space and %XX as the byte
            // (UTF-8); put keeps the LAST value for a repeated key.
            query.put(URLDecoder.decode(key, StandardCharsets.UTF_8),
                URLDecoder.decode(value, StandardCharsets.UTF_8));
        }
        return query;
    }

    private static void _httpSend(HttpExchange ex, int status,
            Map<String, String> headers, byte[] body) {
        try {
            if (headers != null) {
                for (Map.Entry<String, String> e : headers.entrySet()) {
                    if (e.getKey().equalsIgnoreCase("content-length")) {
                        continue;
                    }
                    ex.getResponseHeaders().set(e.getKey(), e.getValue());
                }
            }
            boolean head = "HEAD".equals(ex.getRequestMethod());
            ex.sendResponseHeaders(status, body.length);
            OutputStream os = ex.getResponseBody();
            try {
                if (!head && body.length > 0) {
                    os.write(body);
                }
            } finally {
                os.close();
            }
        } catch (IOException e) {
            try {
                ex.close();
            } catch (Exception ignored) {
            }
        }
    }

    @SuppressWarnings("unchecked")
    private static void _httpDispatch(HttpExchange ex) {
        boolean responded = false;
        try {
            String rawPath = ex.getRequestURI().getRawPath();
            if (rawPath == null) {
                rawPath = ex.getRequestURI().getPath();
            }
            LinkedHashMap<String, String> query =
                _parseQuery(ex.getRequestURI().getRawQuery());
            byte[] raw = ex.getRequestBody().readAllBytes();
            String body;
            try {
                body = _decodeUtf8Strict(raw);
            } catch (NewrError e) {
                _httpSend(ex, 500, null, new byte[0]);
                responded = true;
                return;
            }
            LinkedHashMap<String, String> headers =
                new LinkedHashMap<>();
            for (Map.Entry<String, List<String>> e
                    : ex.getRequestHeaders().entrySet()) {
                if (e.getValue() == null || e.getValue().isEmpty()) {
                    continue;
                }
                headers.put(e.getKey(),
                    e.getValue().get(e.getValue().size() - 1));
            }
            _HttpRoute found = null;
            for (_HttpRoute route : _HTTP_ROUTES) {
                if (route.method.equals(ex.getRequestMethod())
                        && route.path.equals(rawPath)) {
                    found = route;
                    break;
                }
            }
            if (found == null) {
                _httpSend(ex, 404, null, new byte[0]);
                responded = true;
                return;
            }
            LinkedHashMap<String, Object> values =
                new LinkedHashMap<>();
            values.put("method", ex.getRequestMethod());
            values.put("path", rawPath);
            values.put("headers", headers);
            values.put("body", body);
            values.put("query", query);
            Object req = _buildFromShape(found.reqShape, values);
            Object resp = ((Fn1<Object, Object>) found.handler)
                .apply(req);
            if (resp == null) {
                throw new NewrError("http: handler returned null");
            }
            Object statusObj = _structField(resp, "status");
            Object headersObj = _structField(resp, "headers");
            Object bodyObj = _structField(resp, "body");
            if (!(statusObj instanceof Long status)
                    || status < 100 || status > 599) {
                throw new NewrError("http: response status must be "
                    + "100 to 599, got " + statusObj);
            }
            if (!(headersObj instanceof Map)) {
                throw new NewrError("http: response headers must be "
                    + "a dict");
            }
            if (!(bodyObj instanceof String respBody)) {
                throw new NewrError("http: response body must be "
                    + "a str");
            }
            _httpSend(ex, status.intValue(),
                (Map<String, String>) headersObj,
                respBody.getBytes(StandardCharsets.UTF_8));
            responded = true;
        } catch (Exception e) {
            if (!responded) {
                try {
                    _httpSend(ex, 500, null, new byte[0]);
                } catch (Exception ignored) {
                }
            }
        }
    }

    /** newr `http.serve(port)` / `http.serve(port, host)`: serve
     * the registered routes forever, until the process is killed.
     * Prints `listening on <host>:<port>` (the real port when 0)
     * as its first line, then blocks. */
    public static Object httpServe(long port) {
        return httpServe(port, "127.0.0.1");
    }

    public static Object httpServe(long port, String host) {
        if (port < 0 || port > 65535) {
            throw new NewrError("http: port must be 0 to 65535, got "
                + port);
        }
        if (host == null) {
            throw new NewrError("http: host must be a str");
        }
        try {
            HttpServer server = HttpServer.create(
                new InetSocketAddress(host, (int) port), 0);
            server.createContext("/", NewrRt::_httpDispatch);
            server.setExecutor(Executors.newCachedThreadPool());
            server.start();
            int realPort = server.getAddress().getPort();
            System.out.println("listening on " + host + ":"
                + realPort);
            System.out.flush();
            new CountDownLatch(1).await();
            return null;
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
            return null;
        } catch (IOException e) {
            throw asError(e);
        }
    }

    @SuppressWarnings("unchecked")
    private static Object httpFetch(String method, String url,
            String body, long timeout, Map<String, String> headers,
            Object[] shape) {
        if (method == null || !_HTTP_METHODS.contains(method)) {
            throw new NewrError("http: invalid method " + method);
        }
        if (timeout <= 0) {
            throw new NewrError("http: timeout must be a positive "
                + "number of seconds, got " + timeout);
        }
        if (url == null) {
            throw new NewrError("http: URL must be a str");
        }
        URI uri;
        try {
            uri = new URI(url);
        } catch (URISyntaxException e) {
            throw new NewrError("http: bad URL " + url + ": "
                + e.getMessage());
        }
        String scheme = uri.getScheme();
        if (scheme == null
                || !(scheme.equalsIgnoreCase("http")
                    || scheme.equalsIgnoreCase("https"))) {
            throw new NewrError("http: fetch URL must start with "
                + "http:// or https://, got " + url);
        }
        String host = uri.getHost();
        if (host == null || host.isEmpty()) {
            throw new NewrError("http: fetch URL has no host: " + url);
        }
        if (uri.getUserInfo() != null) {
            throw new NewrError("http: a URL must not carry userinfo; "
                + "read the token from env and pass it as a header");
        }
        if (!_DEFAULT_HTTP_ALLOWLIST.contains(
                host.toLowerCase(java.util.Locale.ROOT))) {
            throw new NewrError("http: fetch host '" + host
                + "' is not on the allowlist (localhost, 127.0.0.1)");
        }
        if (shape == null) {
            throw new NewrError("http: internal: fetch without a "
                + "response shape");
        }
        LinkedHashMap<String, String> reqHeaders =
            new LinkedHashMap<>();
        if (headers != null) {
            for (Map.Entry<String, String> e : headers.entrySet()) {
                if (e.getKey() == null || e.getValue() == null) {
                    throw new NewrError("http: headers must be "
                        + "dict[str, str]");
                }
                reqHeaders.put(e.getKey(), e.getValue());
            }
        }
        boolean hasContentType = false;
        for (String key : reqHeaders.keySet()) {
            if (key.equalsIgnoreCase("content-type")) {
                hasContentType = true;
                break;
            }
        }
        HttpClient client = HttpClient.newBuilder()
            .connectTimeout(Duration.ofSeconds(timeout))
            .followRedirects(HttpClient.Redirect.NORMAL)
            .build();
        HttpRequest.Builder builder = HttpRequest.newBuilder()
            .uri(uri)
            .timeout(Duration.ofSeconds(timeout));
        for (Map.Entry<String, String> e : reqHeaders.entrySet()) {
            builder.header(e.getKey(), e.getValue());
        }
        if (body != null) {
            if (!hasContentType) {
                builder.header("Content-Type", "application/json");
            }
            builder.method(method,
                HttpRequest.BodyPublishers.ofString(body,
                    StandardCharsets.UTF_8));
        } else {
            builder.method(method, HttpRequest.BodyPublishers.noBody());
        }
        HttpResponse<byte[]> resp;
        try {
            resp = client.send(builder.build(),
                HttpResponse.BodyHandlers.ofByteArray());
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
            throw new NewrError("http: fetch " + method + " " + url
                + " failed: interrupted");
        } catch (IOException e) {
            throw new NewrError("http: fetch " + method + " " + url
                + " failed: " + e.getMessage());
        }
        LinkedHashMap<String, String> respHeaders =
            new LinkedHashMap<>();
        for (Map.Entry<String, List<String>> e
                : resp.headers().map().entrySet()) {
            if (e.getValue() == null || e.getValue().isEmpty()) {
                continue;
            }
            respHeaders.put(e.getKey(),
                e.getValue().get(e.getValue().size() - 1));
        }
        String text = _decodeUtf8Strict(resp.body());
        LinkedHashMap<String, Object> values = new LinkedHashMap<>();
        values.put("status", (long) resp.statusCode());
        values.put("headers", respHeaders);
        values.put("body", text);
        return _buildFromShape(shape, values);
    }

    /** newr `http.get(url[, timeout[, headers]])`: GET, no body,
     * no Content-Type unless the program sets one. */
    public static Object httpGet(String url, long timeout,
            Map<String, String> headers, Object[] shape) {
        return httpFetch("GET", url, null, timeout, headers, shape);
    }

    /** newr `http.post(url, body[, timeout[, headers]])`. */
    public static Object httpPost(String url, String body, long timeout,
            Map<String, String> headers, Object[] shape) {
        return httpFetch("POST", url, body, timeout, headers, shape);
    }

    /** newr `http.request(method, url, body[, timeout[, headers]])`:
     * the generic fetch primitive. */
    public static Object httpRequest(String method, String url,
            String body, long timeout, Map<String, String> headers,
            Object[] shape) {
        return httpFetch(method, url, body, timeout, headers, shape);
    }

    // ---------------------------------------------------------------- ui module
    // One small widget set (SPEC.md section 12.8), stdlib-only:
    // Swing for the native window, pure Java for the render core.
    // A Widget is an opaque value: a kind tag plus its fields.
    // Handlers are NewrRt Fn values (the lambdas the emitter
    // produces, or a method reference for a named function). The
    // render core is pure and deterministic: no Swing, no I/O,
    // byte-identical to the py reference on every input (see
    // SPEC.md SS12.8 for the format). `uiShow` builds one static
    // window and blocks until it closes; `uiRun` re-invokes the
    // view after every handler call and rebuilds the window from
    // the new tree, so state must live outside the view (a db
    // handle or an http backend), never in a captured variable.

    /** A ui widget value: `kind` plus the fields that kind uses.
     * Only the constructors below build one; programs never build
     * a Widget by hand. */
    public static class Widget {
        public final String kind;
        public final String text;
        public final String label;
        public final Fn0<Void> handler;
        public final String placeholder;
        public final String value;
        public final Fn1<String, Void> inputHandler;
        public final String url;
        public final List<Widget> children;

        public Widget(String kind, String text, String label,
                Fn0<Void> handler, String placeholder, String value,
                Fn1<String, Void> inputHandler, String url,
                List<Widget> children) {
            this.kind = kind;
            this.text = text;
            this.label = label;
            this.handler = handler;
            this.placeholder = placeholder;
            this.value = value;
            this.inputHandler = inputHandler;
            this.url = url;
            this.children = children == null ? List.of()
                    : List.copyOf(children);
        }
    }

    /** newr `ui.text(s)`. */
    public static Widget uiText(String text) {
        if (text == null) {
            throw new NewrError("ui: text takes a string");
        }
        return new Widget("text", text, "", null, "", "", null, "",
                List.of());
    }

    /** newr `ui.button(label, handler)`. */
    public static Widget uiButton(String label, Fn0<Void> handler) {
        if (label == null) {
            throw new NewrError("ui: button label takes a string");
        }
        if (handler == null) {
            throw new NewrError("ui: button handler must be callable");
        }
        return new Widget("button", "", label, handler, "", "", null,
                "", List.of());
    }

    /** newr `ui.input(placeholder, value, handler)`. */
    public static Widget uiInput(String placeholder, String value,
            Fn1<String, Void> handler) {
        if (placeholder == null || value == null) {
            throw new NewrError(
                    "ui: input takes strings for placeholder and value");
        }
        if (handler == null) {
            throw new NewrError("ui: input handler must be callable");
        }
        return new Widget("input", "", "", null, placeholder, value,
                handler, "", List.of());
    }

    private static List<Widget> _uiChildren(List<Widget> children,
            String what) {
        if (children == null) {
            throw new NewrError("ui: " + what + " takes a list of widgets");
        }
        for (Widget child : children) {
            if (child == null) {
                throw new NewrError(
                        "ui: " + what + " takes widgets, got null");
            }
        }
        return children;
    }

    /** newr `ui.list(items)`. */
    public static Widget uiList(List<Widget> children) {
        return new Widget("list", "", "", null, "", "", null, "",
                _uiChildren(children, "list"));
    }

    /** newr `ui.image(url)`. */
    public static Widget uiImage(String url) {
        if (url == null) {
            throw new NewrError("ui: image url takes a string");
        }
        return new Widget("image", "", "", null, "", "", null, url,
                List.of());
    }

    /** newr `ui.row(children)`. */
    public static Widget uiRow(List<Widget> children) {
        return new Widget("row", "", "", null, "", "", null, "",
                _uiChildren(children, "row"));
    }

    /** newr `ui.column(children)`. */
    public static Widget uiColumn(List<Widget> children) {
        return new Widget("column", "", "", null, "", "", null, "",
                _uiChildren(children, "column"));
    }

    /** The SPEC.md SS12.8 escape: & then < > ", in that order,
     * in text and attributes alike. */
    private static String _uiEscape(String s) {
        return s.replace("&", "&amp;").replace("<", "&lt;")
                .replace(">", "&gt;").replace("\"", "&quot;");
    }

    /** newr `ui.render(w)`: the widget tree as deterministic HTML
     * (SPEC.md SS12.8). No outer wrapper, no insignificant
     * whitespace, UTF-8. Button and input indices are separate
     * pre-order counters. */
    public static String uiRender(Widget widget) {
        if (widget == null) {
            throw new NewrError("ui: render takes a widget");
        }
        int[] counters = new int[]{0, 0};
        return _uiRenderWidget(widget, counters);
    }

    private static String _uiRenderWidget(Widget w, int[] counters) {
        switch (w.kind) {
            case "text":
                return "<p>" + _uiEscape(w.text) + "</p>";
            case "button": {
                int n = counters[0]++;
                return "<button data-action=\"" + n + "\">"
                        + _uiEscape(w.label) + "</button>";
            }
            case "input": {
                int n = counters[1]++;
                return "<input placeholder=\"" + _uiEscape(w.placeholder)
                        + "\" value=\"" + _uiEscape(w.value)
                        + "\" data-input=\"" + n + "\">";
            }
            case "list": {
                StringBuilder sb = new StringBuilder("<ul>");
                for (Widget c : w.children) {
                    sb.append("<li>");
                    sb.append(_uiRenderWidget(c, counters));
                    sb.append("</li>");
                }
                sb.append("</ul>");
                return sb.toString();
            }
            case "image":
                return "<img src=\"" + _uiEscape(w.url) + "\" alt=\"\">";
            case "row": {
                StringBuilder sb =
                        new StringBuilder("<div data-layout=\"row\">");
                for (Widget c : w.children) {
                    sb.append(_uiRenderWidget(c, counters));
                }
                sb.append("</div>");
                return sb.toString();
            }
            case "column": {
                StringBuilder sb = new StringBuilder(
                        "<div data-layout=\"column\">");
                for (Widget c : w.children) {
                    sb.append(_uiRenderWidget(c, counters));
                }
                sb.append("</div>");
                return sb.toString();
            }
            default:
                throw new NewrError(
                        "ui: unknown widget kind " + w.kind);
        }
    }

    private static void _uiAdd(Container parent, JComponent c) {
        c.setAlignmentX(Component.LEFT_ALIGNMENT);
        c.setAlignmentY(Component.TOP_ALIGNMENT);
        parent.add(c);
    }

    private static void _uiBuildSwing(Container parent, Widget w,
            Runnable rerender) {
        switch (w.kind) {
            case "text":
                _uiAdd(parent, new JLabel(w.text));
                break;
            case "button": {
                JButton button = new JButton(w.label);
                button.addActionListener(e -> {
                    w.handler.get();
                    if (rerender != null) {
                        rerender.run();
                    }
                });
                _uiAdd(parent, button);
                break;
            }
            case "input": {
                JTextField field = new JTextField(w.value);
                field.addActionListener(e -> {
                    w.inputHandler.apply(field.getText());
                    if (rerender != null) {
                        rerender.run();
                    }
                });
                // No native placeholder in Swing: the placeholder
                // only shows in the HTML render (uiRender). The
                // field starts with the value, empty when the
                // program passes "". Enter and focus-out both
                // commit, mirroring the py tkinter bindings.
                field.addFocusListener(new FocusAdapter() {
                    @Override
                    public void focusLost(FocusEvent e) {
                        w.inputHandler.apply(field.getText());
                        if (rerender != null) {
                            rerender.run();
                        }
                    }
                });
                _uiAdd(parent, field);
                break;
            }
            case "list": {
                JPanel frame = new JPanel();
                frame.setLayout(new BoxLayout(frame, BoxLayout.Y_AXIS));
                _uiAdd(parent, frame);
                for (Widget child : w.children) {
                    _uiBuildSwing(frame, child, rerender);
                }
                break;
            }
            case "image":
                // Swing cannot load a remote image without the
                // network, and a load failure is not an error
                // (SPEC.md U6): show the URL as a placeholder
                // label, so the widget is visible and honest
                // about it, mirroring the py reference.
                _uiAdd(parent, new JLabel("[image: " + w.url + "]"));
                break;
            case "row": {
                JPanel frame = new JPanel();
                frame.setLayout(new BoxLayout(frame, BoxLayout.X_AXIS));
                _uiAdd(parent, frame);
                for (Widget child : w.children) {
                    _uiBuildSwing(frame, child, rerender);
                }
                break;
            }
            case "column": {
                JPanel frame = new JPanel();
                frame.setLayout(new BoxLayout(frame, BoxLayout.Y_AXIS));
                _uiAdd(parent, frame);
                for (Widget child : w.children) {
                    _uiBuildSwing(frame, child, rerender);
                }
                break;
            }
            default:
                throw new NewrError(
                        "ui: unknown widget kind " + w.kind);
        }
    }

    /** newr `ui.show(w)`: one static native window, blocks until
     * the window is closed. */
    public static void uiShow(Widget widget) {
        if (widget == null) {
            throw new NewrError("ui: show takes a widget");
        }
        CountDownLatch done = new CountDownLatch(1);
        try {
            SwingUtilities.invokeAndWait(() -> {
                JFrame frame = new JFrame("newr");
                frame.setDefaultCloseOperation(JFrame.DISPOSE_ON_CLOSE);
                frame.addWindowListener(new WindowAdapter() {
                    @Override
                    public void windowClosed(WindowEvent e) {
                        done.countDown();
                    }
                });
                JPanel body = new JPanel();
                body.setLayout(new BoxLayout(body, BoxLayout.Y_AXIS));
                frame.getContentPane().add(body);
                _uiBuildSwing(body, widget, null);
                frame.pack();
                frame.setVisible(true);
            });
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
            return;
        } catch (java.lang.reflect.InvocationTargetException e) {
            Throwable cause = e.getCause();
            if (cause instanceof HeadlessException) {
                throw new NewrError(
                        "ui: a display is needed to show a window");
            }
            throw new NewrError("ui: cannot show a window: " + cause);
        }
        try {
            done.await();
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
        }
    }

    /** newr `ui.run(view)`: the Elm-style loop. Build the window
     * from `view()`, and after every handler call re-invoke the
     * view and rebuild the window from the new tree. State lives
     * outside the view (a db or http backend); the view re-reads
     * it each time. Blocks until the window is closed. */
    public static void uiRun(Fn0<Widget> view) {
        if (view == null) {
            throw new NewrError("ui: run takes a view function");
        }
        CountDownLatch done = new CountDownLatch(1);
        final Runnable[] rerenderBox = new Runnable[1];
        try {
            SwingUtilities.invokeAndWait(() -> {
                JFrame frame = new JFrame("newr");
                frame.setDefaultCloseOperation(JFrame.DISPOSE_ON_CLOSE);
                frame.addWindowListener(new WindowAdapter() {
                    @Override
                    public void windowClosed(WindowEvent e) {
                        done.countDown();
                    }
                });
                JPanel body = new JPanel();
                body.setLayout(new BoxLayout(body, BoxLayout.Y_AXIS));
                frame.getContentPane().add(body);
                rerenderBox[0] = () -> {
                    body.removeAll();
                    Widget tree = view.get();
                    if (tree == null) {
                        throw new NewrError(
                                "ui: a view must return a widget");
                    }
                    _uiBuildSwing(body, tree, rerenderBox[0]);
                    body.revalidate();
                    body.repaint();
                    frame.pack();
                };
                rerenderBox[0].run();
                frame.setVisible(true);
            });
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
            return;
        } catch (java.lang.reflect.InvocationTargetException e) {
            Throwable cause = e.getCause();
            if (cause instanceof HeadlessException) {
                throw new NewrError(
                        "ui: a display is needed to show a window");
            }
            throw new NewrError("ui: cannot show a window: " + cause);
        }
        try {
            done.await();
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
        }
    }
}
