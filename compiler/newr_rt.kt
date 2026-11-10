// The newr Kotlin runtime (M5).
//
// This file is the runtime half of every emitted Kotlin program that
// needs it: checked 64-bit integer arithmetic (Long + Math.*Exact),
// truncating division, half-away rounding on the EXACT float value,
// the newr conversion/rendering rules, `Result`, and the `?.` / `??`
// helpers. It is stdlib-only (java.base) and dependency-free.
//
// It lives in the compiler package as `compiler/newr_rt.kt` and
// `newr build --target kt` copies it - byte-identical - next to
// an emitted `.kt` file when (and only when) that program uses a
// helper from it. Do not edit a copied file by hand: edit this file
// in the newr repo and rebuild.
//
// Semantics are pinned in BUILD_REPORT.md §22 (N1–Nn, mirroring
// the M2 pins P1–P12, the M3 pins J1–J13 and the M4 pins K1–K13).
// Every rule here is unit-tested in tests/kt/RtChecks.kt (run by
// tests/test_newr_rt_kt.py).
//
// The runtime is a single `object NewrRt` in the default package,
// so an emitted file runs with plain
// `kotlinc *.kt -include-runtime -d app.jar && java -jar app.jar`
// and no build file. Emitted code calls helpers as
// `NewrRt.add(a, b)`; error classes are `NewrRt.NewrError` and
// friends.

import com.sun.net.httpserver.HttpExchange
import com.sun.net.httpserver.HttpServer
import java.io.BufferedReader
import java.io.IOException
import java.io.InputStreamReader
import java.math.BigDecimal
import java.math.BigInteger
import java.net.InetSocketAddress
import java.net.URI
import java.net.http.HttpClient
import java.net.http.HttpRequest
import java.net.http.HttpResponse
import java.nio.ByteBuffer
import java.nio.charset.CharacterCodingException
import java.nio.charset.CodingErrorAction
import java.nio.charset.StandardCharsets
import java.nio.file.Files
import java.nio.file.Path
import java.nio.file.StandardOpenOption
import java.security.SecureRandom
import java.sql.Connection
import java.sql.DriverManager
import java.sql.PreparedStatement
import java.sql.SQLException
import java.time.Duration
import java.time.Instant
import java.time.ZoneOffset
import java.time.ZonedDateTime
import java.util.concurrent.CountDownLatch
import java.util.concurrent.Executors
import java.util.concurrent.ThreadLocalRandom
import java.util.regex.Pattern

object NewrRt {

    // ---------------------------------------------------------------- int64

    val INT64_MIN: Long = Long.MIN_VALUE
    val INT64_MAX: Long = Long.MAX_VALUE

    private val INT64_MIN_BI: BigInteger = BigInteger.valueOf(Long.MIN_VALUE)
    private val INT64_MAX_BI: BigInteger = BigInteger.valueOf(Long.MAX_VALUE)
    // Largest magnitude whose double conversion is finite in the
    // Python sense: float(2**1024 - 2**971) is finite, one more is
    // inf (mirrors the JS _MAX_FLOAT_BIGINT).
    private val MAX_FLOAT_BIGINT: BigInteger =
        BigInteger.ONE.shiftLeft(1024).subtract(BigInteger.ONE.shiftLeft(971))

    /** Return `v` if it fits in signed 64-bit, else raise. */
    fun checkInt(v: Long): Long {
        return v
    }

    /** Return `v` if it fits in signed 64-bit, else raise. */
    fun checkInt(v: BigInteger): Long {
        if (v.compareTo(INT64_MIN_BI) < 0 || v.compareTo(INT64_MAX_BI) > 0) {
            throw NewrOverflowError(
                "integer overflow: " + v + " is outside int64 range")
        }
        return v.longValueExact()
    }

    // ---------------------------------------------------------------- errors

    /** Base of every newr `error` value. Carries the error payload.
     * `code` is null unless set (a caught host error keeps its own
     * code via asError). `message` is nullable: a host error may
     * have none, and `toStr` renders that as "null" (the Java
     * twin's `StringBuilder.append((String) null)`). */
    open class NewrError(
        message: String?,
        val code: String? = null,
        val payload: Any? = null,
    ) : RuntimeException(message)

    /** Checked-int overflow. The Python target's NewrOverflowError
     * is a NewrError subclass constructed WITHOUT a code, so a newr
     * `catch e:` sees e.code == none (the class is told by `name`,
     * not by `code`). The builtin OverflowError (round(inf),
     * int(inf), float(huge int)) is a different site: it throws
     * codedError("OverflowError", ...) instead. */
    class NewrOverflowError(message: String) : NewrError(message)

    /** Division or modulo by zero. Same code rule as above: the
     * Python twin is constructed without a code, so e.code is none
     * in a `catch`. The builtin sites - `0.0 ** -1`, where Python
     * raises ZeroDivisionError itself, and float `/` by zero, where
     * Python's native `/` raises it - throw
     * codedError("ZeroDivisionError", ...) in powFloat and fdiv
     * instead (§18 fix 2: fdiv was missed by the §17.4 item-4 list). */
    class NewrDivisionByZeroError(message: String) : NewrError(message)

    /** An error with a host-style code (IndexError, KeyError,
     * ValueError): the Python target raises the builtin of that
     * name, so a newr `catch e:` sees the same code on both targets. */
    fun codedError(code: String, message: String): NewrError {
        return NewrError(message, code)
    }

    /** Coerce whatever a `catch` caught into a newr `error` value.
     * A `NewrError` passes through unchanged; a plain Throwable
     * keeps its message and gains its class name as the code (a
     * NoSuchFileException, an IllegalStateException); a string
     * becomes an error with that message. This is what makes
     * `.message` / `.code` total inside every `catch` body
     * (SPEC §10). */
    fun asError(e: Any?): NewrError {
        if (e is NewrError) {
            return e
        }
        if (e is String) {
            return NewrError(e)
        }
        if (e is Throwable) {
            return NewrError(e.message, e.javaClass.simpleName)
        }
        return NewrError(e.toString())
    }

    /** A null of any type, for the absent side of a `Result`
     * (`ok`'s error slot, `err`'s value slot). The cast is to a
     * type parameter, so it is erased at runtime and never throws
     * - unlike `null as NewrError`, which would. */
    @Suppress("UNCHECKED_CAST")
    private fun <T> nullValue(): T = null as T

    // ---------------------------------------------------------------- checked int arithmetic

    fun add(a: Long, b: Long): Long {
        try {
            return Math.addExact(a, b)
        } catch (e: ArithmeticException) {
            throw NewrOverflowError("integer overflow: "
                + BigInteger.valueOf(a).add(BigInteger.valueOf(b))
                + " is outside int64 range")
        }
    }

    fun sub(a: Long, b: Long): Long {
        try {
            return Math.subtractExact(a, b)
        } catch (e: ArithmeticException) {
            throw NewrOverflowError("integer overflow: "
                + BigInteger.valueOf(a).subtract(BigInteger.valueOf(b))
                + " is outside int64 range")
        }
    }

    fun mul(a: Long, b: Long): Long {
        try {
            return Math.multiplyExact(a, b)
        } catch (e: ArithmeticException) {
            throw NewrOverflowError("integer overflow: "
                + BigInteger.valueOf(a).multiply(BigInteger.valueOf(b))
                + " is outside int64 range")
        }
    }

    fun neg(a: Long): Long {
        try {
            return Math.negateExact(a)
        } catch (e: ArithmeticException) {
            throw NewrOverflowError("integer overflow: "
                + BigInteger.valueOf(a).negate()
                + " is outside int64 range")
        }
    }

    fun powInt(a: Long, b: Long): Long {
        // newr `**` on ints: checked, bounded before computing.
        // A negative exponent is an error (an int result cannot
        // represent it). The exponent is bounded by repeated
        // squaring with a check at every multiply - `10 ** 10**9`
        // raises instead of allocating by computing blindly.
        if (b < 0) {
            throw NewrError("negative exponent for int pow")
        }
        if (a == 0L) {
            return if (b == 0L) 1L else 0L
        }
        if (a == 1L) {
            return 1L
        }
        if (a == -1L) {
            return if (b % 2L == 1L) -1L else 1L
        }
        var result = 1L
        var base = a
        var exp = b
        while (exp > 0) {
            if (exp % 2L == 1L) {
                result = mul(result, base)
            }
            exp /= 2
            if (exp > 0) {
                base = mul(base, base)
            }
        }
        return result
    }

    fun tdiv(a: Long, b: Long): Long {
        // newr `/` on ints: truncating division (toward zero).
        // Kotlin `/` already truncates - the check is the two error
        // cases.
        if (b == 0L) {
            throw NewrDivisionByZeroError("integer division by zero")
        }
        if (a == Long.MIN_VALUE && b == -1L) {
            throw NewrOverflowError("integer overflow: INT64_MIN / -1")
        }
        return a / b
    }

    fun tmod(a: Long, b: Long): Long {
        // newr `%` on ints: remainder with the dividend's sign.
        // Kotlin `%` already takes the dividend's sign.
        if (b == 0L) {
            throw NewrDivisionByZeroError("integer modulo by zero")
        }
        if (a == Long.MIN_VALUE && b == -1L) {
            return 0L // mathematically 0; no overflow (unlike division)
        }
        return a % b
    }

    fun powFloat(a: Double, b: Double): Double {
        // newr `**` on floats: IEEE semantics. Java `Math.pow`
        // already returns NaN for a negative base with a fractional
        // exponent (Python returns a complex there - newr has no
        // complex type, so the answer is NaN) and Infinity on
        // overflow. The one case Java gets wrong is a zero base
        // with a negative exponent: Java gives Infinity, Python
        // raises ZeroDivisionError.
        if (a == 0.0 && b < 0) {
            // Python raises the builtin ZeroDivisionError here (not
            // the NewrError subclass), so a `catch e:` sees code
            // "ZeroDivisionError" - codedError, not the class.
            throw codedError("ZeroDivisionError",
                "0.0 cannot be raised to a negative power")
        }
        return Math.pow(a, b)
    }

    fun fmod(a: Double, b: Double): Double {
        // newr `%` on floats: C `fmod` semantics (dividend's
        // sign). Kotlin `%` already takes the dividend's sign
        // (unlike Python's `%`, which takes the divisor's).
        // Division by zero raises like the int case (Kotlin would
        // give NaN).
        if (b == 0.0) {
            throw NewrDivisionByZeroError("float modulo by zero")
        }
        return a % b
    }

    fun fdiv(a: Double, b: Double): Double {
        // newr `/` on floats: IEEE division, except division by
        // zero raises like Python (Kotlin would give Infinity for a
        // non-zero dividend and NaN for 0/0). `b == 0` covers -0
        // as well. Python's native `/` raises the BUILTIN
        // ZeroDivisionError, so this throws the coded error
        // (§18 fix 2), not the codeless class - a `catch e:` sees
        // e.code == "ZeroDivisionError" on both targets. (Float `%`
        // is different: both targets route it through fmod, which
        // raises the codeless NewrDivisionByZeroError - that parity
        // is correct.)
        if (b == 0.0) {
            throw codedError("ZeroDivisionError", "float division by zero")
        }
        return a / b
    }

    // ---------------------------------------------------------------- rounding (P2 / J3)

    /** Exact decimal expansion of a finite double: value =
     * sign * digits / 10**scale, digits a non-negative BigInteger.
     * Mirrors the JS _exactDecimal (same bit decomposition). */
    private class ExactDecimal(val sign: Long, val digits: BigInteger, val scale: Int)

    private fun exactDecimal(x: Double): ExactDecimal {
        val bits = java.lang.Double.doubleToRawLongBits(x)
        val sign: Long = if (bits < 0) -1 else 1
        val expBits = ((bits ushr 52) and 0x7FFL).toInt()
        val frac = bits and ((1L shl 52) - 1)
        val m: BigInteger
        val e: Long
        if (expBits == 0) {
            // Subnormal (or zero): no implicit leading 1.
            m = BigInteger.valueOf(frac)
            e = -1074
        } else {
            m = BigInteger.valueOf(frac).or(BigInteger.ONE.shiftLeft(52))
            e = expBits - 1075L
        }
        if (m.signum() == 0) {
            return ExactDecimal(sign, BigInteger.ZERO, 0)
        }
        if (e >= 0) {
            return ExactDecimal(sign, m.shiftLeft(e.toInt()), 0)
        }
        return ExactDecimal(sign,
            m.multiply(BigInteger.valueOf(5).pow((-e).toInt())), (-e).toInt())
    }

    /** Round the exact decimal (digits / 10**scale, digits >= 0)
     * to `n` places after the point, half away from zero.
     * Returns {rounded, places} with value = rounded / 10**places. */
    private class RoundedDecimal(val rounded: BigInteger, val places: Long)

    private fun roundDecimal(digits: BigInteger, scale: Long, n: Long): RoundedDecimal {
        if (scale > n) {
            val div = BigInteger.TEN.pow((scale - n).toInt())
            val qr = digits.divideAndRemainder(div)
            var q = qr[0]
            val r = qr[1]
            if (r.shiftLeft(1).compareTo(div) >= 0) {
                q = q.add(BigInteger.ONE)
            }
            return RoundedDecimal(q, n)
        }
        // Already exact at n places: digits / 10**scale, scale <= n.
        return RoundedDecimal(digits, scale)
    }

    private fun decimalString(digits: BigInteger, places: Long): String {
        // `digits` is a non-negative BigInteger holding the value
        // * 10**places. `places` may be negative (round(x, -1)
        // rounds to tens): then the value is digits * 10**(-places).
        val s = digits.toString()
        if (places == 0L) {
            return s
        }
        if (places < 0) {
            return s + "0".repeat((-places).toInt())
        }
        if (s.length > places) {
            return s.substring(0, (s.length - places).toInt()) + "." +
                s.substring((s.length - places).toInt())
        }
        return "0." + "0".repeat((places - s.length).toInt()) + s
    }

    /** Round the exact decimal (digits / 10**scale) to `n` places,
     * half away from zero, as a double. `n` below -308 rounds every
     * finite double to (signed) zero - 10**309 already exceeds the
     * largest finite double, so no huge power is ever built. */
    private fun roundExactToDouble(sign: Long, digits: BigInteger,
                                   scale: Long, n: Long): Double {
        if (n < -308) {
            return if (sign < 0) -0.0 else 0.0
        }
        val rd = roundDecimal(digits, scale, n)
        if (rd.rounded.signum() == 0) {
            // Preserve a negative zero: round(-0.001, 2) is -0.0.
            return if (sign < 0) -0.0 else 0.0
        }
        val text = decimalString(rd.rounded, rd.places)
        return java.lang.Double.parseDouble((if (sign < 0) "-" else "") + text)
    }

    /** newr `round()`: half-away-from-zero (schoolbook), on the
     * EXACT value of the float - not its shortest repr.
     * `round(2.675, 2)` is 2.67, because the float 2.675 is stored
     * slightly below 2.675. `round(x)` returns an `int` (int64
     * range-checked); `round(x, n)` returns a `float`. Non-finite
     * input: `round(inf)` raises (Python's behaviour);
     * `round(inf, n)` returns `inf`, `round(nan, n)` returns `nan`.
     * An int input rounds its exact value too (unlike the JS
     * runtime, which returns an int input unchanged when ndigits
     * is given - `round(5, -1)` is 10 in Python, 5 in JS). */
    fun roundHalfAway(x: Double): Long {
        if (!x.isFinite()) {
            // Python raises the builtin OverflowError here.
            throw codedError("OverflowError",
                "cannot round infinite or NaN value")
        }
        val d = exactDecimal(x)
        val rd = roundDecimal(d.digits, d.scale.toLong(), 0)
        return checkInt(if (d.sign < 0) rd.rounded.negate() else rd.rounded)
    }

    fun roundHalfAway(x: Double, n: Long): Double {
        if (!x.isFinite()) {
            return x
        }
        val d = exactDecimal(x)
        return roundExactToDouble(d.sign, d.digits, d.scale.toLong(), n)
    }

    fun roundHalfAway(x: Long): Long {
        return x
    }

    fun roundHalfAway(x: Long, n: Long): Double {
        val digits = BigInteger.valueOf(x).abs()
        return roundExactToDouble(if (x < 0) -1 else 1, digits, 0, n)
    }

    fun roundHalfAway(x: Boolean): Long {
        return if (x) 1L else 0L
    }

    fun roundHalfAway(x: Boolean, n: Long): Double {
        return roundHalfAway(if (x) 1L else 0L, n)
    }

    // ---------------------------------------------------------------- conversions

    private fun jsonString(x: String): String {
        // JSON-style quoting for error messages (mirrors JS
        // JSON.stringify on a string).
        val sb = StringBuilder("\"")
        for (c in x) {
            when (c) {
                '"' -> sb.append("\\\"")
                '\\' -> sb.append("\\\\")
                '\n' -> sb.append("\\n")
                '\r' -> sb.append("\\r")
                '\t' -> sb.append("\\t")
                else -> {
                    if (c.code < 0x20) {
                        sb.append("\\u" + c.code.toString(16).padStart(4, '0'))
                    } else {
                        sb.append(c)
                    }
                }
            }
        }
        return sb.append("\"").toString()
    }

    private val INT_STRING: Pattern =
        Pattern.compile("^[+-]?\\d+$")
    private val FLOAT_STRING: Pattern =
        Pattern.compile("^[+-]?(\\d+(\\.\\d*)?|\\.\\d+)([eE][+-]?\\d+)?$")

    /** newr `int(x)`: from str (parses; throws on invalid) or
     * float (truncates toward zero). The result is int64
     * range-checked. `int("3.5")` raises ValueError. */
    fun toInt(x: Any?): Long {
        if (x is Boolean) {
            return if (x) 1L else 0L
        }
        if (x is BigInteger) {
            return checkInt(x)
        }
        if (x is Long) {
            return x
        }
        if (x is Int) {
            return x.toLong()
        }
        if (x is Double) {
            val d = x
            if (!d.isFinite()) {
                // Python raises the builtin OverflowError here.
                throw codedError("OverflowError",
                    "cannot convert infinite or NaN to int")
            }
            if (d >= 9223372036854775808.0 || d <= -9223372036854775808.0) {
                throw NewrOverflowError(
                    "integer overflow: " + d + " is outside int64 range")
            }
            return d.toLong()
        }
        if (x is String) {
            val t = x.trim()
            if (INT_STRING.matcher(t).matches()) {
                return checkInt(BigInteger(t))
            }
            throw codedError("ValueError",
                "invalid literal for int(): " + jsonString(x))
        }
        throw NewrError("cannot convert " + typeName(x) + " to int")
    }

    /** newr `float(x)`: from str or int. `float("inf")` /
     * `float("nan")` are accepted (Python behaviour); a huge int
     * raises OverflowError. */
    fun toFloat(x: Any?): Double {
        if (x is Boolean) {
            return if (x) 1.0 else 0.0
        }
        if (x is BigInteger) {
            val b = x
            if (b.compareTo(MAX_FLOAT_BIGINT) > 0
                || b.compareTo(MAX_FLOAT_BIGINT.negate()) < 0) {
                // Python raises the builtin OverflowError here.
                throw codedError("OverflowError",
                    "int too large to convert to float")
            }
            return b.toDouble()
        }
        if (x is Long || x is Int) {
            return (x as Number).toDouble()
        }
        if (x is Double) {
            return x
        }
        if (x is String) {
            val t = x.trim().replace("_", "")
            val low = t.lowercase()
            if (low == "inf" || low == "+inf"
                || low == "infinity" || low == "+infinity") {
                return Double.POSITIVE_INFINITY
            }
            if (low == "-inf" || low == "-infinity") {
                return Double.NEGATIVE_INFINITY
            }
            if (low == "nan" || low == "+nan" || low == "-nan") {
                return Double.NaN
            }
            if (FLOAT_STRING.matcher(t).matches()) {
                return java.lang.Double.parseDouble(t)
            }
            throw codedError("ValueError",
                "could not convert string to float: " + jsonString(x))
        }
        throw NewrError("cannot convert " + typeName(x) + " to float")
    }

    /** newr `bool(x)`: from str (`"true"`/`"false"`, exact
     * lowercase) or int (0 -> false). P7: parsing, not truthiness. */
    fun toBool(x: Any?): Boolean {
        if (x is Boolean) {
            return x
        }
        if (x is String) {
            if (x == "true") {
                return true
            }
            if (x == "false") {
                return false
            }
            throw codedError("ValueError",
                "invalid bool: " + jsonString(x)
                + " (expected \"true\" or \"false\")")
        }
        if (x is BigInteger) {
            return x.signum() != 0
        }
        if (x is Number) {
            if (x is Double) {
                return x != 0.0
            }
            return x.toLong() != 0L
        }
        throw NewrError("cannot convert " + typeName(x) + " to bool")
    }

    private fun typeName(x: Any?): String {
        if (x == null) {
            return "none"
        }
        if (x is String) {
            return "str"
        }
        if (x is Boolean) {
            return "bool"
        }
        if (x is Double) {
            return "float"
        }
        if (x is Number) {
            return "int"
        }
        if (x is List<*>) {
            return "list"
        }
        if (x is Map<*, *>) {
            return "dict"
        }
        if (x is Set<*>) {
            return "set"
        }
        return x::class.java.simpleName
    }

    /** Float rendering with PYTHON repr semantics (J4): shortest
     * round-trip digits, scientific notation iff the decimal
     * exponent is < -4 or >= 16, ".0" for integral floats. Kotlin
     * `Double.toString` differs twice over: its exponent cutoffs
     * differ (1e7 -> "1.0E7", Python "10000000.0"), and its
     * shortest digits differ from Python's in rare edge cases
     * (Double.toString(5e-324) is "4.9E-324", Python repr is
     * "5e-324"). So the digits are derived here from the EXACT
     * decimal expansion: the shortest prefix that parses back to
     * the same double (round to nearest, ties to even - David
     * Gay's dtoa mode 0, the rule behind Python's repr). */
    fun floatStr(x: Double): String {
        if (x.isNaN()) {
            return "nan"
        }
        if (x == Double.POSITIVE_INFINITY) {
            return "inf"
        }
        if (x == Double.NEGATIVE_INFINITY) {
            return "-inf"
        }
        if (x == 0.0) {
            return if (java.lang.Double.doubleToRawLongBits(x) < 0) "-0.0" else "0.0"
        }
        val neg = x < 0
        val ax = Math.abs(x)
        // Exact decimal digits of ax (finite expansion - every
        // binary fraction has one) plus the decimal exponent of
        // the leading digit.
        val exact = BigDecimal(ax)
        var digits = exact.unscaledValue().toString()
        val scale = exact.scale()
        val point = digits.length - scale // value = digits with . after `point`
        val e10 = point - 1
        // Strip trailing zeros (they carry no information).
        var end = digits.length
        while (end > 1 && digits[end - 1] == '0') {
            end--
        }
        digits = digits.substring(0, end)
        // Shortest prefix that round-trips.
        var best: String? = null
        var bestE10 = e10
        for (k in 1..digits.length) {
            val prefix = digits.substring(0, k)
            var rounded = prefix
            var re10 = e10
            if (k < digits.length) {
                val rest = digits.substring(k)
                val roundUp: Boolean
                val first = rest[0]
                if (first > '5') {
                    roundUp = true
                } else if (first < '5') {
                    roundUp = false
                } else if (rest.substring(1).any { c -> c != '0' }) {
                    roundUp = true // more than half
                } else {
                    // Exactly half: ties to even.
                    roundUp = (prefix[prefix.length - 1] - '0') % 2 == 1
                }
                if (roundUp) {
                    val bumped = BigInteger(prefix).add(BigInteger.ONE)
                    rounded = bumped.toString()
                    if (rounded.length > prefix.length) {
                        re10 = e10 + 1 // 999 -> 1000
                    }
                }
            }
            // Does this candidate parse back to ax?
            val cand = toDecimalString(rounded, re10)
            if (java.lang.Double.parseDouble(cand) == ax) {
                best = rounded
                bestE10 = re10
                break
            }
        }
        var bestStr = best
        if (bestStr == null) {
            // Unreachable: 17 significant digits always round-trip.
            bestStr = digits
            bestE10 = e10
        }
        // Strip trailing zeros from the rounded digits (rounding up
        // can produce one, e.g. "10" for 1e-7); the exponent refers
        // to the leading digit and is unaffected.
        var bend = bestStr.length
        while (bend > 1 && bestStr[bend - 1] == '0') {
            bend--
        }
        bestStr = bestStr.substring(0, bend)
        val sign = if (neg) "-" else ""
        if (bestE10 < -4 || bestE10 >= 16) {
            // Scientific: d[.ddd]e±XX with at least two exponent digits.
            val mant = if (bestStr.length > 1)
                bestStr[0] + "." + bestStr.substring(1)
            else
                bestStr.substring(0, 1)
            val esign = if (bestE10 < 0) "-" else "+"
            val eabs = String.format("%02d", Math.abs(bestE10))
            return sign + mant + "e" + esign + eabs
        }
        // Fixed: decimal point after bestE10+1 digits.
        val point2 = bestE10 + 1
        val out: String
        if (point2 <= 0) {
            out = "0." + "0".repeat(-point2) + bestStr
        } else if (point2 >= bestStr.length) {
            out = bestStr + "0".repeat(point2 - bestStr.length) + ".0"
        } else {
            out = bestStr.substring(0, point2) + "." + bestStr.substring(point2)
        }
        return sign + out
    }

    /** Render a digit string + decimal exponent as a plain decimal
     * literal (no exponent) for parsing/comparison. */
    private fun toDecimalString(digits: String, e10: Int): String {
        val point = e10 + 1
        if (point <= 0) {
            return "0." + "0".repeat(-point) + digits
        }
        if (point >= digits.length) {
            return digits + "0".repeat(point - digits.length)
        }
        return digits.substring(0, point) + "." + digits.substring(point)
    }

    /** newr `str(x)`: the display rendering (P8). `true`/`false`/
     * `none` are lowercase; floats use Python repr (floatStr);
     * collections render with elements rendered recursively and
     * strings bare (`[1, true, a]`); an `error` renders as its
     * message; a struct/enum value renders via its `toString`
     * (emitted by the compiler for every struct/enum type); a
     * Kotlin `enum class` value renders as `Color.Red` (its plain
     * `toString` would be just `Red`). */
    fun toStr(x: Any?): String {
        if (x == null) {
            return "none"
        }
        if (x is Boolean) {
            return if (x) "true" else "false"
        }
        if (x is String) {
            return x
        }
        if (x is BigInteger) {
            return x.toString()
        }
        if (x is Number) {
            if (x is Double || x is Float) {
                return floatStr(x.toDouble())
            }
            return x.toString()
        }
        if (x is NewrError) {
            return x.message ?: "null"
        }
        if (x is Result<*, *>) {
            val r = x
            if (r.isOk()) {
                return "ok(" + toStr(r._value) + ")"
            }
            return "err(" + toStr(r._error) + ")"
        }
        if (x is Enum<*>) {
            return "${x::class.simpleName}.${x.name}"
        }
        if (x is List<*>) {
            val sb = StringBuilder("[")
            for (i in x.indices) {
                if (i > 0) {
                    sb.append(", ")
                }
                sb.append(toStr(x[i]))
            }
            return sb.append("]").toString()
        }
        if (x is Map<*, *>) {
            val sb = StringBuilder("{")
            var first = true
            for ((k, v) in x.entries) {
                if (!first) {
                    sb.append(", ")
                }
                first = false
                sb.append(toStr(k)).append(": ").append(toStr(v))
            }
            return sb.append("}").toString()
        }
        if (x is Set<*>) {
            val sb = StringBuilder("{")
            var first = true
            for (e in x) {
                if (!first) {
                    sb.append(", ")
                }
                first = false
                sb.append(toStr(e))
            }
            return sb.append("}").toString()
        }
        return x.toString()
    }

    // ---------------------------------------------------------------- Result

    /** newr `Result[T, E]` (SPEC §10): the value form of errors.
     * `_value` / `_error` are typed `T` / `E` (not `T?` / `E?`)
     * so emitted code reads them without a null assertion - the
     * absent side holds `null` at runtime via `nullValue()` (the
     * Java twin's `new Result<>(true, value, null)`). */
    class Result<T, E>(val _isOk: Boolean, val _value: T, val _error: E) {

        fun isOk(): Boolean {
            return _isOk
        }

        fun isErr(): Boolean {
            return !_isOk
        }

        fun unwrap(): T {
            if (_isOk) {
                return _value
            }
            val e = _error
            if (e is RuntimeException) {
                throw e
            }
            if (e is Throwable) {
                throw NewrError(e.toString())
            }
            throw NewrError(e.toString())
        }

        fun unwrapOr(dflt: T): T {
            return if (_isOk) _value else dflt
        }

        override fun equals(other: Any?): Boolean {
            if (this === other) {
                return true
            }
            if (other !is Result<*, *>) {
                return false
            }
            if (_isOk != other._isOk) {
                return false
            }
            return if (_isOk) eq(_value, other._value) else eq(_error, other._error)
        }

        override fun hashCode(): Int {
            // hashOf (not Objects.hashCode): NewrRt.eq treats -0.0
            // and 0.0 as equal, so their hash codes must agree too
            // (the equals/hashCode contract).
            return if (_isOk) hashOf(_value) else hashOf(_error)
        }
    }

    /** A hash consistent with `eq`: -0.0 and 0.0 hash alike (eq
     * says they are equal), and collections hash by their elements
     * (a List's hashCode would use Double.hashCode for a -0.0
     * element, which disagrees with eq). NaN needs no agreement:
     * eq says NaN != NaN, so unequal values may share a hash. */
    fun hashOf(x: Any?): Int {
        if (x == null) {
            return 0
        }
        if (x is Double) {
            val v = if (x == 0.0) 0.0 else x // normalizes -0.0 to 0.0
            return v.hashCode()
        }
        if (x is List<*>) {
            var h = 1
            for (e in x) {
                h = 31 * h + hashOf(e)
            }
            return h
        }
        if (x is Map<*, *>) {
            var h = 0
            for ((k, v) in x.entries) {
                h += hashOf(k) xor hashOf(v)
            }
            return h
        }
        if (x is Set<*>) {
            var h = 0
            for (e in x) {
                h += hashOf(e)
            }
            return h
        }
        return x.hashCode()
    }

    /** Hash of a struct/enum's fields, consistent with `eq`. */
    fun structHash(vararg fields: Any?): Int {
        var h = 1
        for (f in fields) {
            h = 31 * h + hashOf(f)
        }
        return h
    }

    /** `env.get(name)` / `env.get(name, default)`: null when the
     * variable is absent and no default is given. */
    fun envGet(name: String): String? {
        return System.getenv(name)
    }

    fun envGet(name: String, dflt: String): String {
        val v = System.getenv(name)
        return v ?: dflt
    }

    /** `env.args`: the command-line arguments, set by the
     * generated main() (a module file has no main, so the runtime
     * holds the copy). Defaults to an empty list before main runs.
     * The JVM's main() args exclude the program name, but newr's
     * env.args is sys.argv 1:1 (Python: sys.argv, JS:
     * process.argv.slice(1)) - argv[0] is the program, so
     * setMainArgs prepends the main object name. */
    var _mainArgs: Array<String> = arrayOf()

    fun setMainArgs(prog: String, args: Array<String>) {
        _mainArgs = arrayOf(prog) + args
    }

    fun envArgs(): MutableList<String> {
        return _mainArgs.toMutableList()
    }

    fun <T> ok(value: T): Result<T, NewrError> {
        return Result(true, value, nullValue())
    }

    fun <T> err(error: NewrError): Result<T, NewrError> {
        return Result(false, nullValue(), error)
    }

    /** newr `try <expr>`: Ok-wrap the value, catch a thrown error
     * into `err(...)`. The caught error is coerced with `asError`
     * so `.message` / `.code` are always available. */
    fun <T> tryResult(f: () -> T): Result<T, NewrError> {
        try {
            return ok(f())
        } catch (exc: Throwable) {
            return err(asError(exc))
        }
    }

    // ---------------------------------------------------------------- equality (J6)

    /** newr `==`: structural equality (§7.7). Primitives compare
     * by value; lists compare element-wise, dicts by key
     * (order-insensitive), sets unordered, structs/enums by
     * constructor + fields (their generated `equals`), Results by
     * kind + payload. Functions and errors compare by identity.
     * NaN is never equal to NaN (not even inside a collection). */
    fun eq(a: Any?, b: Any?): Boolean {
        if (a === b) {
            // Same reference - but NaN is not == itself even as a
            // boxed Double, and `a === b` on two distinct NaN boxes
            // is false anyway; a single NaN box compared to itself
            // reaches here, so exclude it.
            if (a is Double && a.isNaN()) {
                return false
            }
            return true
        }
        if (a == null || b == null) {
            return false
        }
        if (a is Double || b is Double) {
            if (a !is Double || b !is Double) {
                return false
            }
            // Unboxed == : -0.0 == 0.0 is true, NaN == NaN is false
            // (exactly the JS `===` semantics for numbers).
            return a == b
        }
        if (a is List<*> || b is List<*>) {
            if (a !is List<*> || b !is List<*>) {
                return false
            }
            if (a.size != b.size) {
                return false
            }
            for (i in a.indices) {
                if (!eq(a[i], b[i])) {
                    return false
                }
            }
            return true
        }
        if (a is Map<*, *> || b is Map<*, *>) {
            if (a !is Map<*, *> || b !is Map<*, *>) {
                return false
            }
            if (a.size != b.size) {
                return false
            }
            for ((k, v) in a.entries) {
                if (!b.containsKey(k) || !eq(v, b[k])) {
                    return false
                }
            }
            return true
        }
        if (a is Set<*> || b is Set<*>) {
            if (a !is Set<*> || b !is Set<*>) {
                return false
            }
            if (a.size != b.size) {
                return false
            }
            outer@ for (x in a) {
                for (y in b) {
                    if (eq(x, y)) {
                        continue@outer
                    }
                }
                return false
            }
            return true
        }
        if (a is Result<*, *> || b is Result<*, *>) {
            if (a !is Result<*, *> || b !is Result<*, *>) {
                return false
            }
            if (a._isOk != b._isOk) {
                return false
            }
            return if (a._isOk) eq(a._value, b._value) else eq(a._error, b._error)
        }
        if (a is NewrError || b is NewrError) {
            return false // errors compare by identity (a === b above)
        }
        return a == b
    }

    // ---------------------------------------------------------------- ?. / ?? / accessors

    /** newr `?.`: `null` when `v` is `null`, else `f(v)`. The test
     * is `== null`, never truthiness - `0`, `""` and `[]` are
     * values, not absences. A whole `?.` chain collapses into one
     * call with the full postfix path inside `f`, so the base
     * expression is evaluated exactly once. */
    fun <T, R> optChain(v: T?, f: (T) -> R): R? {
        return if (v == null) null else f(v)
    }

    /** newr `??`: `v` when it is not `null`, else `defaultFn()`.
     * The default is a thunk so it is evaluated lazily. */
    fun <T> coalesce(v: T?, dflt: () -> T): T {
        return v ?: dflt()
    }

    /** newr `list[i]`: SPEC §9.2 - out-of-range indexing is a
     * runtime error. Valid indices are `0 <= i < len(lst)`; a
     * negative index is out of range (never a wrap). */
    fun listIndex(lst: List<*>, i: Long): Any? {
        if (i < 0 || i >= lst.size) {
            throw codedError("IndexError", "list index out of range")
        }
        return lst[i.toInt()]
    }

    /** newr `list.get(i, default)`: P9 - a negative index is
     * out-of-range (no wrap-around); anything outside
     * `0 <= i < len(lst)` returns `default`. */
    fun listGet(lst: List<*>, i: Long, dflt: Any?): Any? {
        return if (i >= 0 && i < lst.size) lst[i.toInt()] else dflt
    }

    /** newr `x in [..]`: structural equality (a list of structs
     * contains an equal-but-different instance). */
    fun listContains(lst: List<*>, x: Any?): Boolean {
        for (e in lst) {
            if (eq(e, x)) {
                return true
            }
        }
        return false
    }

    /** newr `set.remove(x)`: removing a missing element is an
     * error (Python's `set.remove` raises KeyError; the JVM's
     * `Set.remove` silently returns false). */
    fun setRemove(s: MutableSet<*>, x: Any?) {
        if (!s.contains(x)) {
            throw codedError("KeyError", toStr(x))
        }
        @Suppress("UNCHECKED_CAST")
        (s as MutableSet<Any?>).remove(x)
    }

    /** newr `T?.unwrap()`: the value, or a thrown error on `null`. */
    fun <T> unwrapOpt(v: T?): T {
        if (v == null) {
            throw NewrError("unwrap of none")
        }
        return v
    }

    /** newr `range(...)`: like Python's range, materialised as a
     * list of longs. range(stop) / range(start, stop) /
     * range(start, stop, step); a zero step is a ValueError. */
    fun range(stop: Long): MutableList<Long> {
        return range(0, stop, 1)
    }

    fun range(start: Long, stop: Long): MutableList<Long> {
        return range(start, stop, 1)
    }

    fun range(start: Long, stop: Long, step: Long): MutableList<Long> {
        if (step == 0L) {
            throw codedError("ValueError", "range() arg 3 must not be zero")
        }
        val out = mutableListOf<Long>()
        if (step > 0) {
            var i = start
            while (i < stop) {
                out.add(i)
                i += step
            }
        } else {
            var i = start
            while (i > stop) {
                out.add(i)
                i += step
            }
        }
        return out
    }

    /** `len(x)` for any lenable value (str: code points, P5). */
    fun lenOf(x: Any?): Long {
        if (x is String) {
            return chars(x).size.toLong()
        }
        if (x is Collection<*>) {
            return x.size.toLong()
        }
        if (x is Map<*, *>) {
            return x.size.toLong()
        }
        throw NewrError("len() of unsupported value: " + x,
            "TypeError", null)
    }

    /** `{"k": v, ...}` - alternating key/value pairs into a
     * LinkedHashMap (insertion order). Unlike `Map.ofEntries`,
     * null keys and values are allowed. */
    @Suppress("UNCHECKED_CAST")
    fun <V> dictOf(vararg kvs: Any?): LinkedHashMap<String, V> {
        val out = LinkedHashMap<String, V>()
        var i = 0
        while (i < kvs.size) {
            out[kvs[i] as String] = kvs[i + 1] as V
            i += 2
        }
        return out
    }

    // ---------------------------------------------------------------- strings

    /** Code-point list of a string (SPEC pin P5: `str.chars()`). */
    fun chars(s: String): MutableList<String> {
        val out = mutableListOf<String>()
        s.codePoints().forEach { cp -> out.add(String(Character.toChars(cp))) }
        return out
    }

    /** newr string ordering: lexicographic by Unicode code point
     * (SPEC 7.6). The JVM's String.compareTo compares UTF-16 code
     * units, which orders an astral character below BMP characters
     * at U+E000 and above (a high surrogate is below U+FFFD, while
     * by code points U+1F600 is above U+FFFD). Compare code point
     * by code point; a strict prefix is smaller. Mirrors Java's
     * strCmp (L10, type_model.newr, 7 Nov). */
    fun strCmp(a: String, b: String): Int {
        val ac = a.codePoints().toArray()
        val bc = b.codePoints().toArray()
        val n = minOf(ac.size, bc.size)
        for (i in 0 until n) {
            val d = ac[i] - bc[i]
            if (d != 0) {
                return d
            }
        }
        return ac.size - bc.size
    }

    /** `s.split(sep)` with a LITERAL separator (the JVM's
     * String.split takes a regex) and Python's trailing-empty-
     * strings behaviour (`split(regex, -1)` keeps them). */
    fun splitStr(s: String, sep: String): MutableList<String> {
        return Pattern.compile(Pattern.quote(sep)).split(s, -1).toMutableList()
    }

    /** `s[i]` by code point (P5). Negative indices wrap like
     * Python; out of range raises IndexError (the JS runtime's
     * `[...s][i]` returns undefined there - a JS latent bug). */
    fun strIndex(s: String, idx: Long): String {
        val cps = chars(s)
        val i = if (idx < 0) cps.size + idx else idx
        if (i < 0 || i >= cps.size) {
            throw NewrError(
                "string index out of range: " + idx,
                "IndexError", null)
        }
        return cps[i.toInt()]
    }

    /** `abs(x)` for int64: `Math.abs(Long.MIN_VALUE)` wraps
     * silently on the JVM, so the minimum is an overflow error
     * (Python: abs(-2**63) = 2**63, out of int64 range). */
    fun absLong(x: Long): Long {
        if (x == Long.MIN_VALUE) {
            throw NewrOverflowError(
                "integer overflow: 9223372036854775808 is outside int64 range")
        }
        return Math.abs(x)
    }

    // ---------------------------------------------------------------- print / input

    /** newr `print`: every argument rendered with `toStr`, joined
     * with a single space (Python's print). */
    fun print(vararg args: Any?) {
        val sb = StringBuilder()
        for (i in args.indices) {
            if (i > 0) {
                sb.append(" ")
            }
            sb.append(toStr(args[i]))
        }
        System.out.println(sb.toString())
    }

    private var stdinReader: BufferedReader? = null

    /** newr `input(prompt = "")`: read one line from stdin, like
     * Python's input(). The prompt (if any) is written with no
     * newline first. At EOF, returns "" (mirrors the JS runtime). */
    fun input(prompt: String = ""): String {
        if (prompt.isNotEmpty()) {
            System.out.print(prompt)
            System.out.flush()
        }
        try {
            if (stdinReader == null) {
                stdinReader = BufferedReader(InputStreamReader(
                    System.`in`, StandardCharsets.UTF_8))
            }
            val line = stdinReader!!.readLine()
            if (line == null) {
                return ""
            }
            return line
        } catch (e: IOException) {
            return ""
        }
    }

    // ---------------------------------------------------------------- random

    private val SECURE_RANDOM = SecureRandom()

    /** newr `random.int(lo, hi)`: inclusive on both ends
     * (Python's randint). Exact for any int64 range: rejection
     * sampling over random bytes (ThreadLocalRandom cannot cover a
     * 64-bit range). */
    fun randomInt(lo: Long, hi: Long): Long {
        if (hi < lo) {
            throw codedError("ValueError", "empty range for random.int()")
        }
        val span = BigInteger.valueOf(hi).subtract(BigInteger.valueOf(lo))
            .add(BigInteger.ONE)
        if (span == BigInteger.ONE) {
            return lo
        }
        val bits = span.bitLength()
        val bytes = (bits + 7) / 8
        val buf = ByteArray(bytes)
        while (true) {
            SECURE_RANDOM.nextBytes(buf)
            val v = BigInteger(1, buf)
            if (v.compareTo(span) < 0) {
                return BigInteger.valueOf(lo).add(v).longValueExact()
            }
        }
    }

    fun <T> randomChoice(xs: List<T>): T {
        if (xs.isEmpty()) {
            throw codedError("IndexError",
                "Cannot choose from an empty sequence")
        }
        return xs[ThreadLocalRandom.current().nextInt(xs.size)]
    }

    /** Returns null (the newr value `none`) so the call stays
     * usable in expression position, like the JS runtime. */
    fun randomShuffle(xs: MutableList<*>): Any? {
        @Suppress("UNCHECKED_CAST")
        java.util.Collections.shuffle(xs as MutableList<Any?>,
            ThreadLocalRandom.current())
        return null
    }

    // ---------------------------------------------------------------- time (P4)

    private val TIME_FORMATS: Set<String> = setOf(
        "%Y-%m-%d", "%H:%M", "%Y-%m-%d %H:%M", "%d/%m/%Y", "%H:%M:%S")

    /** newr `time.format(ts, fmt)`: UTC, never local time (P4) -
     * the same program prints the same output on every machine.
     * The format must be one of the spec's fixed set (§12.2). */
    fun timeFormat(ts: Long, fmt: String): String {
        if (!TIME_FORMATS.contains(fmt)) {
            throw NewrError("unsupported time format: " + jsonString(fmt))
        }
        val d = Instant.ofEpochSecond(ts).atZone(ZoneOffset.UTC)
        val y = String.format("%04d", d.year)
        val mo = String.format("%02d", d.monthValue)
        val day = String.format("%02d", d.dayOfMonth)
        val h = String.format("%02d", d.hour)
        val mi = String.format("%02d", d.minute)
        val s = String.format("%02d", d.second)
        when (fmt) {
            "%Y-%m-%d" -> return y + "-" + mo + "-" + day
            "%H:%M" -> return h + ":" + mi
            "%Y-%m-%d %H:%M" -> return y + "-" + mo + "-" + day + " " + h + ":" + mi
            "%d/%m/%Y" -> return day + "/" + mo + "/" + y
            "%H:%M:%S" -> return h + ":" + mi + ":" + s
            else -> throw NewrError("unsupported time format: " + jsonString(fmt))
        }
    }

    // ---------------------------------------------------------------- file I/O

    /** newr `file.read(path)`: the whole file as UTF-8 text. A
     * missing file throws a NewrError whose code is the host
     * exception name (JVM: "NoSuchFileException"; the Python
     * target reports "FileNotFoundError" there, the JS target
     * "ENOENT"; the code for host IO errors is host-specific,
     * BUILD_REPORT §22). Unchecked, so emitted code needs no
     * try/catch around a plain read. */
    fun fileRead(path: String): String {
        try {
            return Files.readString(Path.of(path), StandardCharsets.UTF_8)
        } catch (e: IOException) {
            throw asError(e)
        }
    }

    /** newr `file.write(path, content)`: overwrite (create if
     * needed). Returns null (the newr value `none`). */
    fun fileWrite(path: String, content: String): Any? {
        try {
            Files.writeString(Path.of(path), content, StandardCharsets.UTF_8,
                StandardOpenOption.CREATE,
                StandardOpenOption.TRUNCATE_EXISTING)
        } catch (e: IOException) {
            throw asError(e)
        }
        return null
    }

    /** newr `file.append(path, content)`: append (create if
     * needed). Returns null (the newr value `none`). */
    fun fileAppend(path: String, content: String): Any? {
        try {
            Files.writeString(Path.of(path), content, StandardCharsets.UTF_8,
                StandardOpenOption.CREATE,
                StandardOpenOption.APPEND)
        } catch (e: IOException) {
            throw asError(e)
        }
        return null
    }

    /** newr `file.exists(path)`. */
    fun fileExists(path: String): Boolean {
        return Files.exists(Path.of(path))
    }

    // -- db (SPEC.md section 12.5, M8a) ---------------------------
    // The SQLite helpers over JDBC, the Kotlin twin of the Java
    // ones in NewrRt.java. `Class.forName("org.sqlite.JDBC")`
    // loads the driver at runtime. Handles are Longs, fd-style:
    // ids from 1, a registry maps each id to its Connection. `?`
    // binding is mandatory: values are never interpolated into
    // the SQL text. The connection stays in autocommit, so
    // `db.begin` runs an explicit BEGIN IMMEDIATE (the store's
    // pattern) and a bare `db.exec` commits at once, mirroring
    // the Python target. The imports above are java.sql (part of
    // the JDK, so this file still compiles with plain kotlinc);
    // only the driver jar is needed at runtime.

    private val _dbConnections = LinkedHashMap<Long, Connection>()
    private var _dbNextId = 1L

    /** newr `db.open(path)`: open (create if needed) and return a handle. */
    fun dbOpen(path: String): Long {
        try {
            Class.forName("org.sqlite.JDBC")
            val conn = DriverManager.getConnection("jdbc:sqlite:" + path)
            conn.createStatement().use { st ->
                st.execute("PRAGMA foreign_keys = ON")
                st.execute("PRAGMA busy_timeout = 5000")
            }
            val handle = _dbNextId
            _dbNextId += 1
            _dbConnections[handle] = conn
            return handle
        } catch (e: ClassNotFoundException) {
            throw asError(e)
        } catch (e: SQLException) {
            throw asError(e)
        }
    }

    private fun _dbConn(handle: Long): Connection {
        return _dbConnections[handle]
            ?: throw NewrError("db: unknown or closed handle " + handle)
    }

    private fun _dbBind(ps: PreparedStatement, index: Int, value: Any?) {
        // Bind by newr value type: Long as INTEGER, Double as REAL,
        // String as TEXT, Boolean as 0/1 (JDBC has no boolean column
        // type in SQLite). A value of any other type is a caller bug
        // (the checker only allows int/float/str/bool binds, and
        // never a nullable one, so null is unreachable here).
        when (value) {
            null -> throw NewrError("db: cannot bind a null value")
            is Long -> ps.setLong(index, value)
            is Double -> ps.setDouble(index, value)
            is String -> ps.setString(index, value)
            is Boolean -> ps.setInt(index, if (value) 1 else 0)
            else -> throw NewrError("db: cannot bind a "
                + value.javaClass.simpleName + " value")
        }
    }

    private fun _dbValue(value: Any?, typeStr: String): Any? {
        // Convert one raw SQLite value to the newr value for a
        // `db.query` field type (`int`, `float`, `str`, `bool`,
        // each optionally `?`). Mirrors the Python `_db_value`
        // (newr_rt.py): NULL lands only in a nullable field, and
        // a value that cannot convert throws instead of silently
        // producing a wrong value.
        val nullable = typeStr.endsWith("?")
        val base = if (nullable) typeStr.substring(0, typeStr.length - 1)
            else typeStr
        if (value == null) {
            if (nullable) {
                return null
            }
            throw NewrError("db: NULL in non-nullable " + base + " field")
        }
        when (base) {
            "int" -> {
                if (value is Boolean) {
                    throw NewrError("db: cannot convert bool to int")
                }
                if (value is Number) {
                    val d = value.toDouble()
                    if (d != Math.floor(d) || d < Long.MIN_VALUE.toDouble()
                            || d > Long.MAX_VALUE.toDouble()) {
                        throw NewrError("db: cannot convert " + value
                            + " to int")
                    }
                    return checkInt(value.toLong())
                }
                if (value is String) {
                    try {
                        return checkInt(value.trim().toLong())
                    } catch (e: NumberFormatException) {
                        throw NewrError("db: cannot convert " + value
                            + " to int")
                    }
                }
                throw NewrError("db: cannot convert "
                    + value.javaClass.simpleName + " to int")
            }
            "float" -> {
                if (value is Boolean) {
                    throw NewrError("db: cannot convert bool to float")
                }
                if (value is Number) {
                    return value.toDouble()
                }
                if (value is String) {
                    try {
                        return value.trim().toDouble()
                    } catch (e: NumberFormatException) {
                        throw NewrError("db: cannot convert " + value
                            + " to float")
                    }
                }
                throw NewrError("db: cannot convert "
                    + value.javaClass.simpleName + " to float")
            }
            "str" -> {
                if (value is Boolean) {
                    return if (value) "true" else "false"
                }
                if (value is String) {
                    return value
                }
                return toStr(value)
            }
            "bool" -> {
                if (value is Boolean) {
                    return value
                }
                if (value is Number) {
                    val v = value.toLong()
                    if (v == 0L) {
                        return false
                    }
                    if (v == 1L) {
                        return true
                    }
                    throw NewrError("db: cannot convert " + value
                        + " to bool")
                }
                if (value is String) {
                    if (value == "0") {
                        return false
                    }
                    if (value == "1") {
                        return true
                    }
                    throw NewrError("db: cannot convert " + value
                        + " to bool")
                }
                throw NewrError("db: cannot convert "
                    + value.javaClass.simpleName + " to bool")
            }
            else -> throw NewrError("db: unknown field type " + typeStr)
        }
    }

    /** newr `db.exec(handle, sql, args...)`: run one statement,
     * return the rows affected (0 for DDL). */
    fun dbExec(handle: Long, sql: String, args: Array<Any?>): Long {
        try {
            _dbConn(handle).prepareStatement(sql).use { ps ->
                for (i in args.indices) {
                    _dbBind(ps, i + 1, args[i])
                }
                return ps.executeUpdate().toLong()
            }
        } catch (e: SQLException) {
            throw asError(e)
        }
    }

    /** newr `db.query(handle, sql, args...)`: run a SELECT and
     * return one list per row, converted values in field order. A
     * missing column throws; extra columns are ignored. */
    fun dbQuery(handle: Long, sql: String, args: Array<Any?>,
            fieldNames: Array<String>,
            fieldTypes: Array<String>): List<Array<Any?>> {
        try {
            _dbConn(handle).prepareStatement(sql).use { ps ->
                for (i in args.indices) {
                    _dbBind(ps, i + 1, args[i])
                }
                val rows = ArrayList<Array<Any?>>()
                ps.executeQuery().use { rs ->
                    val meta = rs.metaData
                    val cols = HashSet<String>()
                    for (i in 1..meta.columnCount) {
                        cols.add(meta.getColumnName(i))
                    }
                    while (rs.next()) {
                        val row = arrayOfNulls<Any?>(fieldNames.size)
                        for (i in fieldNames.indices) {
                            if (!cols.contains(fieldNames[i])) {
                                throw NewrError("db: column '"
                                    + fieldNames[i]
                                    + "' missing from query result")
                            }
                            row[i] = _dbValue(rs.getObject(fieldNames[i]),
                                fieldTypes[i])
                        }
                        rows.add(row)
                    }
                }
                return rows
            }
        } catch (e: SQLException) {
            throw asError(e)
        }
    }

    /** newr `db.begin(handle)`: BEGIN IMMEDIATE. */
    fun dbBegin(handle: Long) {
        try {
            _dbConn(handle).createStatement().use { st ->
                st.execute("BEGIN IMMEDIATE")
            }
        } catch (e: SQLException) {
            throw asError(e)
        }
    }

    /** newr `db.commit(handle)`: COMMIT. */
    fun dbCommit(handle: Long) {
        try {
            _dbConn(handle).createStatement().use { st ->
                st.execute("COMMIT")
            }
        } catch (e: SQLException) {
            throw asError(e)
        }
    }

    /** newr `db.rollback(handle)`: ROLLBACK. */
    fun dbRollback(handle: Long) {
        try {
            _dbConn(handle).createStatement().use { st ->
                st.execute("ROLLBACK")
            }
        } catch (e: SQLException) {
            throw asError(e)
        }
    }

    /** newr `db.last_id(handle)`: the last INSERT rowid. */
    fun dbLastId(handle: Long): Long {
        try {
            val st = _dbConn(handle).createStatement()
            try {
                val rs = st.executeQuery("SELECT last_insert_rowid()")
                try {
                    rs.next()
                    return checkInt(rs.getLong(1))
                } finally {
                    rs.close()
                }
            } finally {
                st.close()
            }
        } catch (e: SQLException) {
            throw asError(e)
        }
    }

    /** newr `db.close(handle)`: close and drop the handle. A second
     * close (or any use after close) raises NewrError. */
    fun dbClose(handle: Long) {
        val conn = _dbConnections.remove(handle)
            ?: throw NewrError("db: unknown or closed handle " + handle)
        try {
            conn.close()
        } catch (e: SQLException) {
            throw asError(e)
        }
    }

    // -- json / http (SPEC.md sections 12.6, 12.7, M8b) -----------
    // Typed JSON over a shape descriptor built by the emitter: a
    // scalar is "int" / "float" / "str" / "bool" / "none", a
    // nullable is arrayOf("?", inner), a list is arrayOf("list",
    // elem), a dict is arrayOf("dict", value), a struct is
    // arrayOf("struct", factory, arrayOf(arrayOf(field, shape),
    // ...)) in declaration order, where factory builds the struct
    // from its field values in order. The JSON parser and
    // serializer are hand-written (strict RFC 8259): the JDK has
    // no JSON library, and the parser must tell a JSON float from
    // a JSON int by syntax (1.0 into an int field throws) and keep
    // big ints exact (Long is 64-bit, so 9007199254740993
    // round-trips). A JSON integer outside int64 parses as a
    // BigInteger and throws only when a shape actually consumes
    // it as an int or a float, so an extra field holding one is
    // still ignored, like on the Python target.

    private class JsonState(val text: String, var pos: Int = 0)

    private fun _jsonSkipWs(st: JsonState) {
        while (st.pos < st.text.length) {
            val c = st.text[st.pos]
            if (c == ' ' || c == '\t' || c == '\n' || c == '\r') {
                st.pos++
            } else {
                break
            }
        }
    }

    private fun _jsonParseHex4(st: JsonState): Int {
        if (st.pos + 4 > st.text.length) {
            throw NewrError("json: invalid JSON: bad unicode escape")
        }
        var v = 0
        for (i in 0 until 4) {
            val d = st.text[st.pos++].digitToIntOrNull(16)
                ?: throw NewrError("json: invalid JSON: bad unicode escape")
            v = v * 16 + d
        }
        return v
    }

    private fun _jsonParseString(st: JsonState): String {
        // st.text[st.pos] is the opening quote.
        st.pos++
        val sb = StringBuilder()
        while (true) {
            if (st.pos >= st.text.length) {
                throw NewrError("json: invalid JSON: unterminated string")
            }
            val c = st.text[st.pos]
            st.pos++
            when (c) {
                '"' -> return sb.toString()
                '\\' -> {
                    if (st.pos >= st.text.length) {
                        throw NewrError(
                            "json: invalid JSON: unterminated escape")
                    }
                    val e = st.text[st.pos]
                    st.pos++
                    when (e) {
                        '"' -> sb.append('"')
                        '\\' -> sb.append('\\')
                        '/' -> sb.append('/')
                        'b' -> sb.append('\b')
                        'f' -> sb.append('\u000C')
                        'n' -> sb.append('\n')
                        'r' -> sb.append('\r')
                        't' -> sb.append('\t')
                        'u' -> {
                            val cp = _jsonParseHex4(st)
                            if (cp in 0xD800..0xDBFF) {
                                // A high surrogate followed by a low
                                // surrogate combines into one code
                                // point; a lone high surrogate stays
                                // as-is.
                                if (st.pos + 1 < st.text.length
                                    && st.text[st.pos] == '\\'
                                    && st.text[st.pos + 1] == 'u') {
                                    val save = st.pos
                                    st.pos += 2
                                    val lo = _jsonParseHex4(st)
                                    if (lo in 0xDC00..0xDFFF) {
                                        sb.append(String(Character.toChars(
                                            Character.toCodePoint(
                                                cp.toChar(), lo.toChar()))))
                                    } else {
                                        st.pos = save
                                        sb.append(cp.toChar())
                                    }
                                } else {
                                    sb.append(cp.toChar())
                                }
                            } else {
                                sb.append(cp.toChar())
                            }
                        }
                        else -> throw NewrError(
                            "json: invalid JSON: bad escape \\" + e)
                    }
                }
                else -> {
                    if (c.code < 0x20) {
                        throw NewrError(
                            "json: invalid JSON: control character "
                            + "in string")
                    }
                    sb.append(c)
                }
            }
        }
    }

    private fun _jsonParseNumber(st: JsonState): Any? {
        val start = st.pos
        if (st.pos < st.text.length && st.text[st.pos] == '-') {
            st.pos++
        }
        if (st.pos >= st.text.length) {
            throw NewrError("json: invalid JSON: bad number")
        }
        if (st.text[st.pos] == '0') {
            st.pos++
        } else if (st.text[st.pos] in '1'..'9') {
            while (st.pos < st.text.length
                && st.text[st.pos] in '0'..'9') {
                st.pos++
            }
        } else {
            throw NewrError("json: invalid JSON: bad number")
        }
        var isFloat = false
        if (st.pos < st.text.length && st.text[st.pos] == '.') {
            isFloat = true
            st.pos++
            if (st.pos >= st.text.length
                || st.text[st.pos] !in '0'..'9') {
                throw NewrError("json: invalid JSON: bad number")
            }
            while (st.pos < st.text.length
                && st.text[st.pos] in '0'..'9') {
                st.pos++
            }
        }
        if (st.pos < st.text.length
            && (st.text[st.pos] == 'e' || st.text[st.pos] == 'E')) {
            isFloat = true
            st.pos++
            if (st.pos < st.text.length
                && (st.text[st.pos] == '+' || st.text[st.pos] == '-')) {
                st.pos++
            }
            if (st.pos >= st.text.length
                || st.text[st.pos] !in '0'..'9') {
                throw NewrError("json: invalid JSON: bad number")
            }
            while (st.pos < st.text.length
                && st.text[st.pos] in '0'..'9') {
                st.pos++
            }
        }
        val raw = st.text.substring(start, st.pos)
        if (!isFloat) {
            val asLong = raw.toLongOrNull()
            if (asLong != null) {
                return asLong
            }
            return BigInteger(raw)
        }
        return java.lang.Double.parseDouble(raw)
    }

    private fun _jsonParseLiteral(st: JsonState, word: String,
                                  value: Any?): Any? {
        if (!st.text.startsWith(word, st.pos)) {
            throw NewrError("json: invalid JSON: bad literal")
        }
        st.pos += word.length
        return value
    }

    private fun _jsonParseObject(st: JsonState): LinkedHashMap<String, Any?> {
        st.pos++ // '{'
        val out = LinkedHashMap<String, Any?>()
        _jsonSkipWs(st)
        if (st.pos < st.text.length && st.text[st.pos] == '}') {
            st.pos++
            return out
        }
        while (true) {
            _jsonSkipWs(st)
            if (st.pos >= st.text.length || st.text[st.pos] != '"') {
                throw NewrError(
                    "json: invalid JSON: object key must be a string")
            }
            val key = _jsonParseString(st)
            _jsonSkipWs(st)
            if (st.pos >= st.text.length || st.text[st.pos] != ':') {
                throw NewrError(
                    "json: invalid JSON: expected ':' in object")
            }
            st.pos++
            _jsonSkipWs(st)
            out[key] = _jsonParseValue(st)
            _jsonSkipWs(st)
            if (st.pos >= st.text.length) {
                throw NewrError("json: invalid JSON: unterminated object")
            }
            val c = st.text[st.pos]
            st.pos++
            if (c == '}') {
                return out
            }
            if (c != ',') {
                throw NewrError(
                    "json: invalid JSON: expected ',' in object")
            }
        }
    }

    private fun _jsonParseArray(st: JsonState): ArrayList<Any?> {
        st.pos++ // '['
        val out = ArrayList<Any?>()
        _jsonSkipWs(st)
        if (st.pos < st.text.length && st.text[st.pos] == ']') {
            st.pos++
            return out
        }
        while (true) {
            _jsonSkipWs(st)
            out.add(_jsonParseValue(st))
            _jsonSkipWs(st)
            if (st.pos >= st.text.length) {
                throw NewrError("json: invalid JSON: unterminated array")
            }
            val c = st.text[st.pos]
            st.pos++
            if (c == ']') {
                return out
            }
            if (c != ',') {
                throw NewrError(
                    "json: invalid JSON: expected ',' in array")
            }
        }
    }

    private fun _jsonParseValue(st: JsonState): Any? {
        if (st.pos >= st.text.length) {
            throw NewrError("json: invalid JSON: unexpected end of input")
        }
        return when (val c = st.text[st.pos]) {
            '{' -> _jsonParseObject(st)
            '[' -> _jsonParseArray(st)
            '"' -> _jsonParseString(st)
            't' -> _jsonParseLiteral(st, "true", true)
            'f' -> _jsonParseLiteral(st, "false", false)
            'n' -> _jsonParseLiteral(st, "null", null)
            '-', in '0'..'9' -> _jsonParseNumber(st)
            else -> throw NewrError(
                "json: invalid JSON: unexpected character "
                + jsonString(c.toString()))
        }
    }

    private fun _jsonParse(text: String): Any? {
        val st = JsonState(text)
        _jsonSkipWs(st)
        val v = _jsonParseValue(st)
        _jsonSkipWs(st)
        if (st.pos != text.length) {
            throw NewrError("json: trailing data after JSON value")
        }
        return v
    }

    private fun _jsonWriteString(sb: StringBuilder, s: String) {
        sb.append('"')
        for (c in s) {
            when (c) {
                '"' -> sb.append("\\\"")
                '\\' -> sb.append("\\\\")
                '\n' -> sb.append("\\n")
                '\r' -> sb.append("\\r")
                '\t' -> sb.append("\\t")
                '\b' -> sb.append("\\b")
                '\u000C' -> sb.append("\\f")
                else -> {
                    if (c.code < 0x20) {
                        sb.append("\\u"
                            + c.code.toString(16).padStart(4, '0'))
                    } else {
                        sb.append(c)
                    }
                }
            }
        }
        sb.append('"')
    }

    private fun _jsonWrite(sb: StringBuilder, v: Any?) {
        when (v) {
            null -> sb.append("null")
            is Boolean -> sb.append(if (v) "true" else "false")
            is Long -> sb.append(v.toString())
            is Int -> sb.append(v.toString())
            is BigInteger -> sb.append(v.toString())
            is Double -> {
                if (!v.isFinite()) {
                    throw NewrError(
                        "json: cannot encode a non-finite float")
                }
                sb.append(floatStr(v))
            }
            is String -> _jsonWriteString(sb, v)
            is List<*> -> {
                sb.append('[')
                for (i in v.indices) {
                    if (i > 0) {
                        sb.append(',')
                    }
                    _jsonWrite(sb, v[i])
                }
                sb.append(']')
            }
            is Map<*, *> -> {
                sb.append('{')
                var first = true
                for ((k, item) in v.entries) {
                    if (!first) {
                        sb.append(',')
                    }
                    first = false
                    _jsonWriteString(sb, k as String)
                    sb.append(':')
                    _jsonWrite(sb, item)
                }
                sb.append('}')
            }
            else -> throw NewrError(
                "json: cannot encode " + typeName(v))
        }
    }

    /** One struct field, by its newr name. A field whose name is a
     * Kotlin keyword is emitted with a trailing underscore, so fall
     * back to that spelling when the plain name is missing; the
     * getter is the last resort. Used for json.encode and for
     * reading a handler's response struct. */
    private fun _structField(value: Any?, name: String): Any? {
        if (value == null) {
            throw NewrError("json: expected a struct, got none")
        }
        val cls = value.javaClass
        for (fname in arrayOf(name, name + "_")) {
            try {
                val field = cls.getDeclaredField(fname)
                field.isAccessible = true
                return field.get(value)
            } catch (e: NoSuchFieldException) {
                // Try the next spelling.
            }
        }
        val getter = "get" + name.replaceFirstChar { it.uppercaseChar() }
        try {
            return cls.getMethod(getter).invoke(value)
        } catch (e: Exception) {
            throw NewrError("json: struct has no field " + jsonString(name))
        }
    }

    private fun _jsonKindName(value: Any?): String {
        if (value == null) {
            return "null"
        }
        if (value is Boolean) {
            return "bool"
        }
        if (value is Long || value is Int || value is BigInteger) {
            return "int"
        }
        if (value is Double) {
            return "float"
        }
        if (value is String) {
            return "str"
        }
        if (value is List<*>) {
            return "list"
        }
        if (value is Map<*, *>) {
            return "object"
        }
        return typeName(value)
    }

    private fun _jsonEncodeValue(value: Any?, shape: Any?): Any? {
        if (shape is String) {
            when (shape) {
                "int" -> {
                    when (value) {
                        is Boolean -> throw NewrError(
                            "json: expected int, got bool")
                        is Long -> return checkInt(value)
                        is Int -> return value.toLong()
                        is BigInteger -> return checkInt(value)
                        else -> throw NewrError(
                            "json: expected int, got "
                            + _jsonKindName(value))
                    }
                }
                "float" -> {
                    when (value) {
                        is Boolean -> throw NewrError(
                            "json: expected float, got bool")
                        is Double -> return value
                        is Long -> return value.toDouble()
                        is Int -> return value.toDouble()
                        is BigInteger -> return toFloat(value)
                        else -> throw NewrError(
                            "json: expected float, got "
                            + _jsonKindName(value))
                    }
                }
                "str" -> {
                    if (value !is String) {
                        throw NewrError("json: expected str, got "
                            + _jsonKindName(value))
                    }
                    return value
                }
                "bool" -> {
                    if (value !is Boolean) {
                        throw NewrError("json: expected bool, got "
                            + _jsonKindName(value))
                    }
                    return value
                }
                "none" -> {
                    if (value != null) {
                        throw NewrError("json: expected null")
                    }
                    return null
                }
                else -> throw NewrError("json: unknown shape " + shape)
            }
        }
        val arr = shape as? Array<*>
            ?: throw NewrError("json: unknown shape " + shape)
        when (arr[0] as? String) {
            "?" -> {
                if (value == null) {
                    return null
                }
                return _jsonEncodeValue(value, arr[1])
            }
            "list" -> {
                if (value !is List<*>) {
                    throw NewrError("json: expected list, got "
                        + _jsonKindName(value))
                }
                val out = ArrayList<Any?>()
                for (e in value) {
                    out.add(_jsonEncodeValue(e, arr[1]))
                }
                return out
            }
            "dict" -> {
                if (value !is Map<*, *>) {
                    throw NewrError("json: expected dict, got "
                        + _jsonKindName(value))
                }
                val out = LinkedHashMap<String, Any?>()
                for ((k, item) in value.entries) {
                    if (k !is String) {
                        throw NewrError("json: dict keys must be str")
                    }
                    out[k] = _jsonEncodeValue(item, arr[1])
                }
                return out
            }
            "struct" -> {
                val fields = arr[2] as? Array<*>
                    ?: throw NewrError("json: unknown shape " + shape)
                val out = LinkedHashMap<String, Any?>()
                for (f in fields) {
                    val fe = f as Array<*>
                    val fname = fe[0] as String
                    out[fname] = _jsonEncodeValue(
                        _structField(value, fname), fe[1])
                }
                return out
            }
            else -> throw NewrError("json: unknown shape " + shape)
        }
    }

    private fun _jsonDecodeValue(value: Any?, shape: Any?): Any? {
        if (shape is String) {
            when (shape) {
                "int" -> {
                    if (value is Long) {
                        return checkInt(value)
                    }
                    throw NewrError("json: expected int, got "
                        + _jsonKindName(value))
                }
                "float" -> {
                    when (value) {
                        is Boolean -> throw NewrError(
                            "json: expected float, got bool")
                        is Long -> return value.toDouble()
                        is Int -> return value.toDouble()
                        is Double -> return value
                        is BigInteger -> {
                            val d = value.toDouble()
                            if (!d.isFinite()) {
                                throw NewrError(
                                    "json: integer too large for float")
                            }
                            return d
                        }
                        else -> throw NewrError(
                            "json: expected float, got "
                            + _jsonKindName(value))
                    }
                }
                "str" -> {
                    if (value !is String) {
                        throw NewrError("json: expected str, got "
                            + _jsonKindName(value))
                    }
                    return value
                }
                "bool" -> {
                    if (value !is Boolean) {
                        throw NewrError("json: expected bool, got "
                            + _jsonKindName(value))
                    }
                    return value
                }
                "none" -> {
                    if (value != null) {
                        throw NewrError("json: expected null")
                    }
                    return null
                }
                else -> throw NewrError("json: unknown shape " + shape)
            }
        }
        val arr = shape as? Array<*>
            ?: throw NewrError("json: unknown shape " + shape)
        when (arr[0] as? String) {
            "?" -> {
                if (value == null) {
                    return null
                }
                return _jsonDecodeValue(value, arr[1])
            }
            "list" -> {
                if (value !is List<*>) {
                    throw NewrError("json: expected list, got "
                        + _jsonKindName(value))
                }
                val out = ArrayList<Any?>()
                for (e in value) {
                    out.add(_jsonDecodeValue(e, arr[1]))
                }
                return out.toMutableList()
            }
            "dict" -> {
                if (value !is Map<*, *>) {
                    throw NewrError("json: expected dict, got "
                        + _jsonKindName(value))
                }
                val out = LinkedHashMap<String, Any?>()
                for ((k, item) in value.entries) {
                    if (k !is String) {
                        throw NewrError("json: dict keys must be str")
                    }
                    out[k] = _jsonDecodeValue(item, arr[1])
                }
                return out
            }
            "struct" -> {
                if (value !is Map<*, *>) {
                    throw NewrError("json: expected object, got "
                        + _jsonKindName(value))
                }
                @Suppress("UNCHECKED_CAST")
                val factory = arr[1] as (Array<Any?>) -> Any?
                val fields = arr[2] as? Array<*>
                    ?: throw NewrError("json: unknown shape " + shape)
                val args = arrayOfNulls<Any?>(fields.size)
                for (i in fields.indices) {
                    val fe = fields[i] as Array<*>
                    val fname = fe[0] as String
                    if (!value.containsKey(fname)) {
                        throw NewrError(
                            "json: missing field " + jsonString(fname))
                    }
                    args[i] = _jsonDecodeValue(value[fname], fe[1])
                }
                return factory(args)
            }
            else -> throw NewrError("json: unknown shape " + shape)
        }
    }

    /** Build a struct from a plain map of field values,
     * converting each field through its shape. Used for the fetch
     * response and the server request, whose fields arrive as
     * plain values, not as JSON text. */
    private fun _buildFromShape(shape: Any?,
                                values: Map<String, Any?>): Any? {
        val arr = shape as? Array<*>
            ?: throw NewrError("http: internal: expected a struct shape")
        if (arr[0] != "struct") {
            throw NewrError("http: internal: expected a struct shape")
        }
        @Suppress("UNCHECKED_CAST")
        val factory = arr[1] as (Array<Any?>) -> Any?
        val fields = arr[2] as? Array<*>
            ?: throw NewrError("http: internal: expected a struct shape")
        val args = arrayOfNulls<Any?>(fields.size)
        for (i in fields.indices) {
            val fe = fields[i] as Array<*>
            val fname = fe[0] as String
            if (!values.containsKey(fname)) {
                throw NewrError(
                    "http: missing field " + jsonString(fname))
            }
            args[i] = _jsonDecodeValue(values[fname], fe[1])
        }
        return factory(args)
    }

    /** newr `json.encode(value)`: JSON text, compact, in struct
     * declaration order. Floats use the shortest round-trip
     * decimal, with ".0" for integral floats (floatStr, the Python
     * repr rule); ints are exact decimal, including int64.
     * Non-ASCII text is written as UTF-8, not \u escapes. */
    fun jsonEncode(value: Any?, shape: Any?): String {
        val sb = StringBuilder()
        _jsonWrite(sb, _jsonEncodeValue(value, shape))
        return sb.toString()
    }

    /** newr `json.decode(text)`: the typed value for `text` under
     * `shape`. An empty string throws, trailing data after the
     * JSON value throws, duplicate keys keep the last value, a
     * JSON fraction into an int field throws, an integer into a
     * float field is fine, null only into a nullable shape, and an
     * integer outside int64 throws when consumed. */
    fun jsonDecode(text: String, shape: Any?): Any? {
        return _jsonDecodeValue(_jsonParse(text), shape)
    }

    // ------------------------------------------------------------ http
    // Sync HTTP server plus fetch client (SPEC.md section 12.6),
    // JDK only: `com.sun.net.httpserver.HttpServer` for serve,
    // `java.net.http.HttpClient` for fetch. Routes live in a
    // registry, exact-match only; a request with no route, or a
    // method mismatch, gets 404 with an empty body (not 405).

    private val HTTP_METHODS: Set<String> = setOf(
        "GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS",
    )

    private val DEFAULT_HTTP_ALLOWLIST: Set<String> = setOf(
        "localhost", "127.0.0.1",
    )

    private class HttpRouteEntry(
        val method: String,
        val path: String,
        val handler: (Any?) -> Any?,
        val reqShape: Any?,
    )

    private val _httpRoutes = ArrayList<HttpRouteEntry>()

    /** newr `http.route(method, path, handler)`: register one
     * exact-match route. A computed method that is not one of the
     * uppercase methods, or a computed path without a leading
     * slash, throws here (literal ones are E2012 from the
     * checker). The handler is a lambda taking the request struct;
     * the generic parameters let a let-bound or inline lambda pass
     * without a cast at the call site. */
    fun <Req, Resp> httpRoute(method: String, path: String,
                              handler: (Req) -> Resp, reqShape: Any?) {
        if (method !in HTTP_METHODS) {
            throw NewrError("http: invalid method " + jsonString(method))
        }
        if (!path.startsWith("/")) {
            throw NewrError(
                "http: route path must start with '/', got "
                + jsonString(path))
        }
        @Suppress("UNCHECKED_CAST")
        val h: (Any?) -> Any? = { req -> handler(req as Req) as Any? }
        _httpRoutes.add(HttpRouteEntry(method, path, h, reqShape))
    }

    private fun _queryDecode(s: String): String {
        // "+" decodes as a space, %XX decodes as the byte (UTF-8);
        // a bad % sequence stays literal, like the Python target.
        val bytes = ArrayList<Byte>()
        var i = 0
        while (i < s.length) {
            val c = s[i]
            if (c == '+') {
                bytes.add(' '.code.toByte())
                i++
            } else if (c == '%' && i + 2 < s.length) {
                val hi = s[i + 1].digitToIntOrNull(16)
                val lo = s[i + 2].digitToIntOrNull(16)
                if (hi == null || lo == null) {
                    bytes.add('%'.code.toByte())
                    i++
                } else {
                    bytes.add(((hi shl 4) + lo).toByte())
                    i += 3
                }
            } else {
                for (b in c.toString().toByteArray(StandardCharsets.UTF_8)) {
                    bytes.add(b)
                }
                i++
            }
        }
        return String(bytes.toByteArray(), StandardCharsets.UTF_8)
    }

    private fun _parseQuery(rawQuery: String?): LinkedHashMap<String, String> {
        val out = LinkedHashMap<String, String>()
        if (rawQuery == null || rawQuery.isEmpty()) {
            return out
        }
        for (pair in rawQuery.split('&')) {
            val idx = pair.indexOf('=')
            val rawKey = if (idx < 0) pair else pair.substring(0, idx)
            val rawVal = if (idx < 0) "" else pair.substring(idx + 1)
            out[_queryDecode(rawKey)] = _queryDecode(rawVal)
        }
        return out
    }

    private fun _httpSend(ex: HttpExchange, status: Int,
                          headers: Map<String, String>,
                          body: ByteArray) {
        try {
            for ((k, v) in headers) {
                if (k.equals("Content-Length", ignoreCase = true)) {
                    continue
                }
                ex.responseHeaders.set(k, v)
            }
            ex.sendResponseHeaders(status, body.size.toLong())
            if (ex.requestMethod != "HEAD" && body.isNotEmpty()) {
                ex.responseBody.use { it.write(body) }
            } else {
                ex.responseBody.close()
            }
        } catch (e: IOException) {
            // The client went away; nothing to do.
        } finally {
            try {
                ex.close()
            } catch (ignored: Exception) {
            }
        }
    }

    private fun _httpDispatch(ex: HttpExchange) {
        try {
            val uri = ex.requestURI
            val path = uri.rawPath ?: uri.path ?: "/"
            val query = _parseQuery(uri.rawQuery)
            val raw = try {
                ex.requestBody.readAllBytes()
            } catch (e: IOException) {
                ByteArray(0)
            }
            val body: String = try {
                StandardCharsets.UTF_8.newDecoder()
                    .onMalformedInput(CodingErrorAction.REPORT)
                    .onUnmappableCharacter(CodingErrorAction.REPORT)
                    .decode(ByteBuffer.wrap(raw)).toString()
            } catch (e: CharacterCodingException) {
                _httpSend(ex, 500, emptyMap(), ByteArray(0))
                return
            }
            val headers = LinkedHashMap<String, String>()
            for ((k, vs) in ex.requestHeaders) {
                if (vs.isNotEmpty()) {
                    headers[k] = vs[0]
                }
            }
            var found: HttpRouteEntry? = null
            for (route in _httpRoutes) {
                if (route.method == ex.requestMethod
                    && route.path == path) {
                    found = route
                    break
                }
            }
            if (found == null) {
                _httpSend(ex, 404, emptyMap(), ByteArray(0))
                return
            }
            val values = LinkedHashMap<String, Any?>()
            values["method"] = ex.requestMethod
            values["path"] = path
            values["headers"] = headers
            values["body"] = body
            values["query"] = query
            val req = _buildFromShape(found.reqShape, values)
            val resp = found.handler(req)
            val statusRaw = _structField(resp, "status")
            val respHeadersRaw = _structField(resp, "headers")
            val respBodyRaw = _structField(resp, "body")
            if (statusRaw !is Long || statusRaw < 100 || statusRaw > 599) {
                throw NewrError(
                    "http: response status must be 100 to 599, got "
                    + toStr(statusRaw))
            }
            if (respHeadersRaw !is Map<*, *>) {
                throw NewrError("http: response headers must be a dict")
            }
            if (respBodyRaw !is String) {
                throw NewrError("http: response body must be a str")
            }
            val respHeaders = LinkedHashMap<String, String>()
            for ((k, v) in respHeadersRaw.entries) {
                if (k !is String || v !is String) {
                    throw NewrError(
                        "http: response headers must be dict[str, str]")
                }
                respHeaders[k] = v
            }
            _httpSend(ex, statusRaw.toInt(), respHeaders,
                respBodyRaw.toByteArray(StandardCharsets.UTF_8))
        } catch (e: Exception) {
            try {
                _httpSend(ex, 500, emptyMap(), ByteArray(0))
            } catch (ignored: Exception) {
            }
        }
    }

    /** newr `http.serve(port)` / `http.serve(port, host)`: serve the
     * registered routes forever, until the process is killed.
     * Prints `listening on <host>:<port>` (the real port when 0)
     * as its first line, then blocks. */
    fun httpServe(port: Long, host: String = "127.0.0.1") {
        if (port < 0 || port > 65535) {
            throw NewrError(
                "http: port must be 0 to 65535, got " + port)
        }
        val server: HttpServer
        try {
            server = HttpServer.create(
                InetSocketAddress(host, port.toInt()), 0)
        } catch (e: IOException) {
            throw asError(e)
        }
        server.createContext("/") { ex -> _httpDispatch(ex) }
        server.executor = Executors.newCachedThreadPool()
        server.start()
        val realPort = server.address.port
        System.out.println("listening on " + host + ":" + realPort)
        System.out.flush()
        try {
            CountDownLatch(1).await()
        } catch (e: InterruptedException) {
            Thread.currentThread().interrupt()
        } finally {
            server.stop(0)
        }
    }

    /** The fetch primitive behind `http.get` / `http.post` /
     * `http.request`: one request, whole-request timeout in
     * seconds (default 10, connect plus body). POST/request set
     * `Content-Type: application/json` unless the program sets
     * one; GET sets none. A 4xx/5xx status is a response, not a
     * throw: the caller reads `resp.status`. Anything else that
     * fails (an unreachable host, a timeout, a bad URL, a host
     * outside the allowlist) throws. */
    private fun httpFetch(method: String, url: String, body: String?,
                          timeout: Long,
                          headers: Map<String, String>?,
                          shape: Any?): Any? {
        // This VM's container network drops IPv6-socket connects to
        // an IPv4 loopback (a plain JDK HttpClient probe times out
        // there, while the same probe with
        // -Djava.net.preferIPv4Stack=true answers at once). Ask for
        // IPv4 sockets before creating any: in a fetch program no
        // socket exists yet, so the property still takes effect.
        System.setProperty("java.net.preferIPv4Stack", "true")
        if (method !in HTTP_METHODS) {
            throw NewrError("http: invalid method " + jsonString(method))
        }
        if (timeout <= 0) {
            throw NewrError(
                "http: timeout must be a positive number of seconds, "
                + "got " + timeout)
        }
        val parsed: URI
        try {
            parsed = URI(url)
        } catch (e: Exception) {
            throw NewrError(
                "http: fetch URL must start with http:// or https://, "
                + "got " + jsonString(url))
        }
        val scheme = parsed.scheme?.lowercase()
        if (scheme != "http" && scheme != "https") {
            throw NewrError(
                "http: fetch URL must start with http:// or https://, "
                + "got " + jsonString(url))
        }
        val host = parsed.host
        if (host == null) {
            throw NewrError(
                "http: fetch URL has no host: " + jsonString(url))
        }
        if (parsed.userInfo != null) {
            throw NewrError(
                "http: a URL must not carry userinfo; read the token "
                + "from env and pass it as a header")
        }
        if (!DEFAULT_HTTP_ALLOWLIST.contains(host.lowercase())) {
            throw NewrError(
                "http: fetch host " + jsonString(host.lowercase())
                + " is not on the allowlist (localhost, 127.0.0.1)")
        }
        val reqHeaders = LinkedHashMap<String, String>()
        if (headers != null) {
            for ((k, v) in headers) {
                reqHeaders[k] = v
            }
        }
        val client = HttpClient.newBuilder()
            .connectTimeout(Duration.ofSeconds(timeout))
            .build()
        val builder = HttpRequest.newBuilder()
            .uri(parsed)
            .timeout(Duration.ofSeconds(timeout))
        if (body != null) {
            var hasContentType = false
            for (k in reqHeaders.keys) {
                if (k.equals("Content-Type", ignoreCase = true)) {
                    hasContentType = true
                }
            }
            if (!hasContentType) {
                reqHeaders["Content-Type"] = "application/json"
            }
            builder.method(method,
                HttpRequest.BodyPublishers.ofString(
                    body, StandardCharsets.UTF_8))
        } else {
            builder.method(method, HttpRequest.BodyPublishers.noBody())
        }
        for ((k, v) in reqHeaders) {
            builder.header(k, v)
        }
        val resp: HttpResponse<ByteArray>
        try {
            resp = client.send(builder.build(),
                HttpResponse.BodyHandlers.ofByteArray())
        } catch (e: Exception) {
            throw NewrError(
                "http: fetch " + method + " " + url + " failed: "
                + (e.message ?: e.javaClass.simpleName))
        }
        val text: String
        try {
            text = StandardCharsets.UTF_8.newDecoder()
                .onMalformedInput(CodingErrorAction.REPORT)
                .onUnmappableCharacter(CodingErrorAction.REPORT)
                .decode(ByteBuffer.wrap(resp.body())).toString()
        } catch (e: CharacterCodingException) {
            throw NewrError("http: response body is not valid UTF-8")
        }
        val respHeaders = LinkedHashMap<String, String>()
        for ((k, vs) in resp.headers().map()) {
            if (vs.isNotEmpty()) {
                respHeaders[k] = vs[0]
            }
        }
        val values = LinkedHashMap<String, Any?>()
        values["status"] = resp.statusCode().toLong()
        values["headers"] = respHeaders
        values["body"] = text
        return _buildFromShape(shape, values)
    }

    /** newr `http.get(url[, timeout[, headers]])`: GET, no body,
     * no Content-Type unless the program sets one. */
    fun httpGet(url: String, timeout: Long = 10,
                headers: Map<String, String>? = null,
                shape: Any?): Any? {
        return httpFetch("GET", url, null, timeout, headers, shape)
    }

    /** newr `http.post(url, body[, timeout[, headers]])`. */
    fun httpPost(url: String, body: String, timeout: Long = 10,
                 headers: Map<String, String>? = null,
                 shape: Any?): Any? {
        return httpFetch("POST", url, body, timeout, headers, shape)
    }

    /** newr `http.request(method, url, body[, timeout[, headers]])`:
     * the generic fetch primitive. */
    fun httpRequest(method: String, url: String, body: String,
                    timeout: Long = 10,
                    headers: Map<String, String>? = null,
                    shape: Any?): Any? {
        return httpFetch(method, url, body, timeout, headers, shape)
    }
}
