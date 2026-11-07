// Kotlin-side unit tests for compiler/newr_rt.kt (M5).
//
// Mirrors tests/java/RtChecks.java case for case (same rule, both
// targets) — the same 61 checks, adapted to Kotlin types (Long
// instead of BigInt, `==` on boxed values goes through
// NewrRt.eq). Run with
// `kotlinc newr_rt.kt RtChecks.kt -include-runtime -d rtchecks.jar
// && java -jar rtchecks.jar` from a temp dir holding copies of
// both files, or via `python -m unittest tests.test_newr_rt_kt`.
//
// Every failure prints the assertion and exits non-zero. Keep the
// battery aligned with the Java file: when a rule is added here,
// add it there too (or record why not).

import java.math.BigInteger
import java.nio.file.Files

var passed = 0

fun check(name: String, fn: () -> Unit) {
    fn()
    passed++
    println("ok $name")
}

// Deep comparison via the runtime's own eq (structural, NaN
// never equal, -0.0 == 0.0).
fun eq(got: Any?, want: Any?, what: String) {
    if (!NewrRt.eq(got, want)) {
        throw AssertionError("$what: expected <$want>, got <$got>")
    }
}

fun ok(cond: Boolean, what: String) {
    if (!cond) {
        throw AssertionError("expected true: $what")
    }
}

/** Expect a throw whose NewrError code equals `code` (null allowed). */
fun throwsCode(fn: () -> Unit, code: String?): NewrRt.NewrError {
    try {
        fn()
    } catch (e: NewrRt.NewrError) {
        eq(e.code, code, "error code")
        return e
    } catch (t: Throwable) {
        throw AssertionError("expected NewrError code $code, got " +
            "${t.javaClass.simpleName}: ${t.message}")
    }
    throw AssertionError("expected code $code, nothing thrown")
}

/** Expect a throw of the named checked-int error class, code null. */
fun throwsNamed(fn: () -> Unit, name: String): NewrRt.NewrError {
    try {
        fn()
    } catch (e: NewrRt.NewrError) {
        eq(e.javaClass.simpleName, name, "error class")
        eq(e.code, null, "error code (must be null)")
        return e
    } catch (t: Throwable) {
        throw AssertionError("expected $name, got " +
            "${t.javaClass.simpleName}: ${t.message}")
    }
    throw AssertionError("expected $name, nothing thrown")
}

fun main() {
    // ---------------------------------------------------------------- constants
    check("int64 bounds") {
        eq(NewrRt.INT64_MIN, Long.MIN_VALUE, "INT64_MIN")
        eq(NewrRt.INT64_MAX, Long.MAX_VALUE, "INT64_MAX")
    }

    // ---------------------------------------------------------------- checked arithmetic
    check("add/sub/mul ok") {
        eq(NewrRt.add(2, 3), 5L, "add")
        eq(NewrRt.sub(10, 4), 6L, "sub")
        eq(NewrRt.mul(3, 4), 12L, "mul")
    }
    check("add overflow") {
        throwsNamed({ NewrRt.add(Long.MAX_VALUE, 1) }, "NewrOverflowError")
    }
    check("sub underflow") {
        throwsNamed({ NewrRt.sub(Long.MIN_VALUE, 1) }, "NewrOverflowError")
    }
    check("mul overflow") {
        throwsNamed({ NewrRt.mul(Long.MAX_VALUE, 2) }, "NewrOverflowError")
    }
    check("neg") {
        eq(NewrRt.neg(5), -5L, "neg 5")
        eq(NewrRt.neg(-5), 5L, "neg -5")
        throwsNamed({ NewrRt.neg(Long.MIN_VALUE) }, "NewrOverflowError")
    }
    check("checkInt") {
        eq(NewrRt.checkInt(0L), 0L, "checkInt 0")
        throwsNamed({ NewrRt.checkInt(BigInteger.ONE.shiftLeft(63)) },
            "NewrOverflowError")
    }
    check("overflow error carries message") {
        val e = throwsNamed(
            { NewrRt.add(Long.MAX_VALUE, 1) }, "NewrOverflowError")
        ok(e.message!!.contains("9223372036854775808"), "message mentions value")
        eq(NewrRt.asError(e), e, "asError passes NewrError through")
    }

    // ---------------------------------------------------------------- tdiv/tmod
    check("truncating division") {
        eq(NewrRt.tdiv(7, 2), 3L, "7/2")
        eq(NewrRt.tdiv(-7, 2), -3L, "-7/2")
        eq(NewrRt.tdiv(7, -2), -3L, "7/-2")
        eq(NewrRt.tdiv(-7, -2), 3L, "-7/-2")
    }
    check("dividend-sign modulo") {
        eq(NewrRt.tmod(7, 3), 1L, "7%3")
        eq(NewrRt.tmod(-7, 3), -1L, "-7%3")
        eq(NewrRt.tmod(7, -3), 1L, "7%-3")
        eq(NewrRt.tmod(-7, -3), -1L, "-7%-3")
    }
    check("division by zero") {
        throwsNamed({ NewrRt.tdiv(1, 0) }, "NewrDivisionByZeroError")
        throwsNamed({ NewrRt.tmod(1, 0) }, "NewrDivisionByZeroError")
    }
    check("min div minus one") {
        throwsNamed({ NewrRt.tdiv(Long.MIN_VALUE, -1) }, "NewrOverflowError")
        eq(NewrRt.tmod(Long.MIN_VALUE, -1), 0L, "MIN%-1")
    }
    check("div/mod invariant") {
        val cases = listOf(
            longArrayOf(7, 3), longArrayOf(-7, 3),
            longArrayOf(7, -3), longArrayOf(-7, -3),
            longArrayOf(100, 7), longArrayOf(-100, 7))
        for (c in cases) {
            eq(NewrRt.tdiv(c[0], c[1]) * c[1] + NewrRt.tmod(c[0], c[1]), c[0],
                "invariant ${c[0]}/${c[1]}")
        }
    }

    // ---------------------------------------------------------------- pow
    check("powInt ok") {
        eq(NewrRt.powInt(2, 10), 1024L, "2^10")
        eq(NewrRt.powInt(2, 0), 1L, "2^0")
        eq(NewrRt.powInt(0, 0), 1L, "0^0")
        eq(NewrRt.powInt(0, 5), 0L, "0^5")
        eq(NewrRt.powInt(-2, 3), -8L, "(-2)^3")
        eq(NewrRt.powInt(-2, 2), 4L, "(-2)^2")
    }
    check("powInt overflow") {
        throwsNamed({ NewrRt.powInt(2, 63) }, "NewrOverflowError")
    }
    check("powInt negative exponent") {
        throwsCode({ NewrRt.powInt(2, -1) }, null)
    }
    check("powInt huge exponent does not hang") {
        val t0 = System.currentTimeMillis()
        throwsNamed({ NewrRt.powInt(10, 1_000_000_000L) }, "NewrOverflowError")
        ok(System.currentTimeMillis() - t0 < 5000, "powInt(10, 1e9) took too long")
    }
    check("powFloat plain") {
        eq(NewrRt.powFloat(2.0, 3.0), 8.0, "2.0^3")
        eq(NewrRt.powFloat(9.0, 0.5), 3.0, "9.0^0.5")
    }
    check("powFloat negative base fractional exponent is NaN") {
        ok(NewrRt.powFloat(-8.0, 1.0 / 3.0).isNaN(), "(-8)^(1/3) is NaN")
    }
    check("powFloat overflow is infinity") {
        eq(NewrRt.powFloat(1e308, 2.0), Double.POSITIVE_INFINITY, "1e308^2")
        eq(NewrRt.powFloat(-1e308, 3.0), Double.NEGATIVE_INFINITY, "(-1e308)^3")
    }
    check("powFloat zero base negative exponent throws") {
        throwsCode({ NewrRt.powFloat(0.0, -1.0) }, "ZeroDivisionError")
    }
    check("fmod dividend sign") {
        eq(NewrRt.fmod(-7.0, 2.0), -1.0, "fmod(-7,2)")
        eq(NewrRt.fmod(7.0, -2.0), 1.0, "fmod(7,-2)")
    }
    check("fmod by zero") {
        throwsNamed({ NewrRt.fmod(1.0, 0.0) }, "NewrDivisionByZeroError")
    }
    check("fdiv") {
        eq(NewrRt.fdiv(7.0, 2.0), 3.5, "7.0/2.0")
        eq(NewrRt.fdiv(-7.0, 2.0), -3.5, "-7.0/2.0")
        eq(NewrRt.fdiv(1.0, 3.0), 1.0 / 3.0, "1.0/3.0")
    }
    check("fdiv by zero") {
        throwsCode({ NewrRt.fdiv(1.0, 0.0) }, "ZeroDivisionError")
        throwsCode({ NewrRt.fdiv(0.0, 0.0) }, "ZeroDivisionError")
        throwsCode({ NewrRt.fdiv(1.0, -0.0) }, "ZeroDivisionError")
    }

    // ---------------------------------------------------------------- round (exact value)
    check("round ties go away from zero") {
        eq(NewrRt.roundHalfAway(2.5), 3L, "round 2.5")
        eq(NewrRt.roundHalfAway(-2.5), -3L, "round -2.5")
        eq(NewrRt.roundHalfAway(3.5), 4L, "round 3.5")
        eq(NewrRt.roundHalfAway(-3.5), -4L, "round -3.5")
    }
    check("round plain values") {
        eq(NewrRt.roundHalfAway(2.4), 2L, "round 2.4")
        eq(NewrRt.roundHalfAway(2.6), 3L, "round 2.6")
        eq(NewrRt.roundHalfAway(0.5), 1L, "round 0.5")
    }
    check("round rounds the exact float value") {
        eq(NewrRt.roundHalfAway(2.675, 2), 2.67, "round 2.675,2")
        eq(NewrRt.roundHalfAway(1.005, 2), 1.0, "round 1.005,2")
        eq(NewrRt.roundHalfAway(0.125, 2), 0.13, "round 0.125,2")
        eq(NewrRt.roundHalfAway(9223372036854774784.0), 9223372036854774784L,
            "round huge integral float")
    }
    check("round ndigits returns float") {
        eq(NewrRt.roundHalfAway(123.456, 1), 123.5, "round 123.456,1")
        eq(NewrRt.roundHalfAway(999.999, 2), 1000.0, "round 999.999,2")
    }
    check("round negative ndigits") {
        eq(NewrRt.roundHalfAway(5L, -1L), 10.0, "round 5,-1")
        eq(NewrRt.roundHalfAway(15L, -1L), 20.0, "round 15,-1")
        eq(NewrRt.roundHalfAway(1250L, -2L), 1300.0, "round 1250,-2")
    }
    check("round overflow") {
        throwsNamed({ NewrRt.roundHalfAway(1e30) }, "NewrOverflowError")
    }
    check("round non-finite") {
        throwsCode({ NewrRt.roundHalfAway(Double.POSITIVE_INFINITY) },
            "OverflowError")
        eq(NewrRt.roundHalfAway(Double.POSITIVE_INFINITY, 2),
            Double.POSITIVE_INFINITY, "round inf,2")
        ok(NewrRt.roundHalfAway(Double.NaN, 2).isNaN(), "round nan,2")
    }
    check("round negative zero") {
        eq(java.lang.Double.doubleToRawLongBits(NewrRt.roundHalfAway(-0.001, 2)),
            java.lang.Double.doubleToRawLongBits(-0.0), "round -0.001,2 is -0.0")
    }

    // ---------------------------------------------------------------- float rendering (J4)
    check("floatStr matches Python repr") {
        val cases: List<Pair<Double, String>> = listOf(
            0.0 to "0.0", -0.0 to "-0.0", 1.0 to "1.0", -1.5 to "-1.5",
            1e15 to "1000000000000000.0", 1e16 to "1e+16", 1e20 to "1e+20",
            1e21 to "1e+21", 0.0001 to "0.0001", 0.00001 to "1e-05",
            1.23456789e-10 to "1.23456789e-10", 123456.789 to "123456.789",
            2.675 to "2.675", 0.1 to "0.1",
            0.30000000000000004 to "0.30000000000000004",
            1e-7 to "1e-07", 5e-324 to "5e-324",
            1.7976931348623157e308 to "1.7976931348623157e+308",
            3.141592653589793 to "3.141592653589793", 100.0 to "100.0",
            2.5 to "2.5", -2.5e-5 to "-2.5e-05",
            123456789.12345679 to "123456789.12345679",
            9.223372036854776e18 to "9.223372036854776e+18",
            1234560000.0 to "1234560000.0",
        )
        for ((v, want) in cases) {
            eq(NewrRt.floatStr(v), want, "floatStr($v)")
        }
        eq(NewrRt.floatStr(Double.NaN), "nan", "floatStr nan")
        eq(NewrRt.floatStr(Double.POSITIVE_INFINITY), "inf", "floatStr inf")
        eq(NewrRt.floatStr(Double.NEGATIVE_INFINITY), "-inf", "floatStr -inf")
    }

    // ---------------------------------------------------------------- conversions
    check("toInt from str") {
        eq(NewrRt.toInt("42"), 42L, "toInt 42")
        eq(NewrRt.toInt("  -42 "), -42L, "toInt -42")
        eq(NewrRt.toInt("+7"), 7L, "toInt +7")
        throwsCode({ NewrRt.toInt("3.5") }, "ValueError")
        throwsCode({ NewrRt.toInt("abc") }, "ValueError")
    }
    check("toInt range checked") {
        throwsNamed({ NewrRt.toInt("99999999999999999999") },
            "NewrOverflowError")
    }
    check("toInt from float truncates") {
        eq(NewrRt.toInt(3.7), 3L, "toInt 3.7")
        eq(NewrRt.toInt(-3.7), -3L, "toInt -3.7")
        throwsCode({ NewrRt.toInt(Double.POSITIVE_INFINITY) }, "OverflowError")
    }
    check("toInt from bool") {
        eq(NewrRt.toInt(true), 1L, "toInt true")
        eq(NewrRt.toInt(false), 0L, "toInt false")
    }
    check("toFloat") {
        eq(NewrRt.toFloat(2L), 2.0, "toFloat 2")
        eq(NewrRt.toFloat("inf"), Double.POSITIVE_INFINITY, "toFloat inf")
        eq(NewrRt.toFloat("-inf"), Double.NEGATIVE_INFINITY, "toFloat -inf")
        ok(NewrRt.toFloat("nan").isNaN(), "toFloat nan")
        eq(NewrRt.toFloat("1_000"), 1000.0, "toFloat 1_000")
        throwsCode({ NewrRt.toFloat("abc") }, "ValueError")
        throwsCode({ NewrRt.toFloat(BigInteger.TEN.pow(400)) }, "OverflowError")
    }
    check("toBool parses") {
        eq(NewrRt.toBool("true"), true, "toBool true")
        eq(NewrRt.toBool("false"), false, "toBool false")
        throwsCode({ NewrRt.toBool("yes") }, "ValueError")
        eq(NewrRt.toBool(0L), false, "toBool 0")
        eq(NewrRt.toBool(1L), true, "toBool 1")
        eq(NewrRt.toBool(0.0), false, "toBool 0.0")
    }

    // ---------------------------------------------------------------- toStr
    check("toStr scalars") {
        eq(NewrRt.toStr(null), "none", "toStr null")
        eq(NewrRt.toStr(true), "true", "toStr true")
        eq(NewrRt.toStr(false), "false", "toStr false")
        eq(NewrRt.toStr(42L), "42", "toStr 42")
        eq(NewrRt.toStr(1.5), "1.5", "toStr 1.5")
        eq(NewrRt.toStr(1.0), "1.0", "toStr 1.0")
        eq(NewrRt.toStr("a"), "a", "toStr a")
    }
    check("toStr collections") {
        eq(NewrRt.toStr(listOf(1L, 2L, 3L)), "[1, 2, 3]", "toStr list")
        eq(NewrRt.toStr(listOf(true, false)), "[true, false]", "toStr bools")
        val m = linkedMapOf<String, Long>("k" to 1L, "j" to 2L)
        eq(NewrRt.toStr(m), "{k: 1, j: 2}", "toStr dict")
        eq(NewrRt.toStr(setOf("a")), "{a}", "toStr set")
        eq(NewrRt.toStr(emptyList<Any?>()), "[]", "toStr empty list")
    }
    check("toStr error and result") {
        eq(NewrRt.toStr(NewrRt.NewrError("boom")), "boom", "toStr error")
        eq(NewrRt.toStr(NewrRt.ok(3L)), "ok(3)", "toStr ok")
        eq(NewrRt.toStr(NewrRt.err<Long>(NewrRt.NewrError("bad"))), "err(bad)",
            "toStr err")
    }
    check("toStr struct-like values") {
        val p = object {
            override fun toString(): String = "Point(x: 1, y: 2)"
        }
        eq(NewrRt.toStr(p), "Point(x: 1, y: 2)", "toStr struct-like")
    }

    // ---------------------------------------------------------------- Result
    check("Result ok/err") {
        val r: NewrRt.Result<Long, NewrRt.NewrError> = NewrRt.ok(7L)
        eq(r.isOk(), true, "ok isOk")
        eq(r.isErr(), false, "ok isErr")
        eq(r.unwrap(), 7L, "ok unwrap")
        eq(r.unwrapOr(0L), 7L, "ok unwrapOr")
        val e: NewrRt.Result<Long, NewrRt.NewrError> =
            NewrRt.err(NewrRt.NewrError("bad"))
        eq(e.isErr(), true, "err isErr")
        eq(e.unwrapOr(9L), 9L, "err unwrapOr")
        throwsCode({ e.unwrap() }, null)
    }
    check("tryResult") {
        val r1: NewrRt.Result<Long, NewrRt.NewrError> =
            NewrRt.tryResult { 6L }
        eq(r1.isOk(), true, "try ok")
        eq(r1.unwrap(), 6L, "try unwrap")
        val r2: NewrRt.Result<Long, NewrRt.NewrError> =
            NewrRt.tryResult<Long> { throw RuntimeException("x") }
        eq(r2.isErr(), true, "try err")
        eq(r2._error.message, "x", "try err message")
    }

    // ---------------------------------------------------------------- equality
    check("eq primitives and collections") {
        eq(NewrRt.eq(1L, 1L), true, "eq 1,1")
        eq(NewrRt.eq(1L, 2L), false, "eq 1,2")
        eq(NewrRt.eq(1L, 1), false, "eq Long,Int")
        eq(NewrRt.eq("a", "a"), true, "eq str")
        eq(NewrRt.eq(null, null), true, "eq null,null")
        eq(NewrRt.eq(null, 0L), false, "eq null,0")
        eq(NewrRt.eq(listOf(1L, 2L), listOf(1L, 2L)), true, "eq lists")
        eq(NewrRt.eq(listOf(1L), listOf(1L, 2L)), false, "eq lists len")
        val m1 = mapOf("a" to 1L)
        val m2 = mapOf("a" to 1L)
        val m3 = mapOf("a" to 2L)
        eq(NewrRt.eq(m1, m2), true, "eq dicts")
        eq(NewrRt.eq(m1, m3), false, "eq dicts differ")
        eq(NewrRt.eq(setOf(1L, 2L), setOf(2L, 1L)), true, "eq sets")
        eq(NewrRt.eq(NewrRt.ok(1L), NewrRt.ok(1L)), true, "eq ok")
        eq(NewrRt.eq(NewrRt.ok(1L), NewrRt.err<Long>(NewrRt.NewrError("x"))),
            false, "eq ok vs err")
        eq(NewrRt.eq(Double.NaN, Double.NaN), false, "eq NaN")
    }

    // ---------------------------------------------------------------- ?. / ?? / accessors
    check("optChain and coalesce") {
        eq(NewrRt.optChain(null, { v: Long -> v + 1 }), null, "optChain null")
        eq(NewrRt.optChain(5L, { v: Long -> v + 1 }), 6L, "optChain 5")
        eq(NewrRt.coalesce(0L, { 5L }), 0L, "coalesce 0")
        eq(NewrRt.coalesce(null, { 5L }), 5L, "coalesce null")
        eq(NewrRt.coalesce("", { "d" }), "", "coalesce empty str")
    }
    check("listIndex") {
        eq(NewrRt.listIndex(listOf(1L, 2L), 1), 2L, "listIndex 1")
        throwsCode({ NewrRt.listIndex(listOf(1L), -1) }, "IndexError")
        throwsCode({ NewrRt.listIndex(listOf(1L), 1) }, "IndexError")
    }
    check("listGet") {
        eq(NewrRt.listGet(listOf(1L, 2L), 0, 9L), 1L, "listGet 0")
        eq(NewrRt.listGet(listOf(1L, 2L), 5, 9L), 9L, "listGet 5")
        eq(NewrRt.listGet(listOf(1L, 2L), -1, 9L), 9L, "listGet -1")
    }
    check("listContains structural") {
        eq(NewrRt.listContains(listOf(1L, 2L), 2L), true, "contains 2")
        eq(NewrRt.listContains(listOf(1L, 2L), 9L), false, "contains 9")
        eq(NewrRt.listContains(
            listOf(listOf(1L)), listOf(1L)),
            true, "contains nested")
    }
    check("setRemove") {
        val s = mutableSetOf(1L, 2L)
        NewrRt.setRemove(s, 1L)
        eq(s.contains(1L), false, "setRemove removed")
        throwsCode({ NewrRt.setRemove(s, 99L) }, "KeyError")
    }
    check("unwrapOpt") {
        eq(NewrRt.unwrapOpt(5L), 5L, "unwrapOpt 5")
        throwsCode({ NewrRt.unwrapOpt(null) }, null)
    }
    check("range") {
        eq(NewrRt.range(3), listOf(0L, 1L, 2L), "range 3")
        eq(NewrRt.range(1, 4), listOf(1L, 2L, 3L), "range 1,4")
        eq(NewrRt.range(0, 10, 3), listOf(0L, 3L, 6L, 9L), "range 0,10,3")
        eq(NewrRt.range(5, 0, -2), listOf(5L, 3L, 1L), "range 5,0,-2")
        throwsCode({ NewrRt.range(0, 5, 0) }, "ValueError")
    }

    // ---------------------------------------------------------------- file ops
    check("file read/write/append") {
        val d = Files.createTempDirectory("newr-rt-")
        val p = d.resolve("t.txt")
        NewrRt.fileWrite(p.toString(), "hello")
        eq(NewrRt.fileExists(p.toString()), true, "file exists")
        eq(NewrRt.fileRead(p.toString()), "hello", "file read")
        NewrRt.fileAppend(p.toString(), " world")
        eq(NewrRt.fileRead(p.toString()), "hello world", "file append")
        eq(NewrRt.fileExists(d.resolve("nope").toString()), false,
            "file missing")
    }
    check("file read missing raises") {
        try {
            NewrRt.fileRead("/definitely/not/here/xyz.txt")
            throw AssertionError("expected a throw")
        } catch (e: NewrRt.NewrError) {
            // Host-specific code (Java: NoSuchFileException;
            // Python: FileNotFoundError; JS: ENOENT).
            eq(e.code, "NoSuchFileException", "missing file code")
        }
    }

    // ---------------------------------------------------------------- time
    check("timeFormat UTC") {
        eq(NewrRt.timeFormat(1700000000L, "%Y-%m-%d"), "2023-11-14", "fmt date")
        eq(NewrRt.timeFormat(0L, "%H:%M:%S"), "00:00:00", "fmt time")
        eq(NewrRt.timeFormat(1700000000L, "%d/%m/%Y"), "14/11/2023", "fmt dmy")
        throwsCode({ NewrRt.timeFormat(0L, "%c") }, null)
    }

    // ---------------------------------------------------------------- random
    check("randomInt in range") {
        for (i in 0 until 50) {
            val r = NewrRt.randomInt(1, 10)
            ok(r in 1..10, "randomInt out of range: $r")
        }
        eq(NewrRt.randomInt(5, 5), 5L, "randomInt 5,5")
        val big = NewrRt.randomInt(0, Long.MAX_VALUE)
        ok(big >= 0, "randomInt wide range")
    }
    check("randomChoice") {
        val xs = listOf(1L, 2L, 3L)
        ok(xs.contains(NewrRt.randomChoice(xs)), "randomChoice in list")
        throwsCode({ NewrRt.randomChoice(emptyList<Long>()) }, "IndexError")
    }
    check("randomShuffle") {
        val xs = mutableListOf(1L, 2L, 3L, 4L, 5L)
        NewrRt.randomShuffle(xs)
        eq(xs.sorted(), listOf(1L, 2L, 3L, 4L, 5L), "shuffle keeps elements")
    }

    // ---------------------------------------------------------------- asError
    check("asError") {
        val n = NewrRt.NewrError("x", "E1")
        eq(NewrRt.asError(n), n, "asError passthrough")
        eq(NewrRt.asError("boom").message, "boom", "asError string")
        val wrapped = NewrRt.asError(IllegalStateException("nope"))
        eq(wrapped.message, "nope", "asError message")
        eq(wrapped.code, "IllegalStateException", "asError code")
        eq(NewrRt.asError(42).message, "42", "asError int")
    }

    // ---------------------------------------------------------------- strings (helpers added for the C# emitter, M4)
    // The C# driver has the same 10 checks, so the two runtimes
    // stay aligned.
    check("chars code points") {
        eq(NewrRt.chars("a\uD83D\uDE00b"),
            listOf("a", "\uD83D\uDE00", "b"), "chars astral")
        eq(NewrRt.chars(""), emptyList<String>(), "chars empty")
        eq(NewrRt.chars("abc"), listOf("a", "b", "c"), "chars ascii")
    }
    check("splitStr literal separator") {
        eq(NewrRt.splitStr("a,b,c", ","), listOf("a", "b", "c"), "split basic")
        eq(NewrRt.splitStr("a,,b", ","), listOf("a", "", "b"), "split empty middle")
        eq(NewrRt.splitStr("a,", ","), listOf("a", ""), "split trailing empty kept")
        eq(NewrRt.splitStr("a.b", "."), listOf("a", "b"), "split literal dot, not regex")
        eq(NewrRt.splitStr("abc", ","), listOf("abc"), "split no match")
    }
    check("strIndex code points") {
        eq(NewrRt.strIndex("hello", 1), "e", "strIndex 1")
        eq(NewrRt.strIndex("hello", -1), "o", "strIndex -1")
        eq(NewrRt.strIndex("a\uD83D\uDE00b", 1), "\uD83D\uDE00", "strIndex astral")
        throwsCode({ NewrRt.strIndex("ab", 2) }, "IndexError")
        throwsCode({ NewrRt.strIndex("ab", -3) }, "IndexError")
    }
    check("absLong") {
        eq(NewrRt.absLong(5), 5L, "abs 5")
        eq(NewrRt.absLong(-5), 5L, "abs -5")
        eq(NewrRt.absLong(0), 0L, "abs 0")
        throwsCode({ NewrRt.absLong(Long.MIN_VALUE) }, null)
    }
    check("lenOf") {
        eq(NewrRt.lenOf("a\uD83D\uDE00b"), 3L, "len str code points")
        eq(NewrRt.lenOf(listOf(1L, 2L)), 2L, "len list")
        val d = linkedMapOf("a" to 1L)
        eq(NewrRt.lenOf(d), 1L, "len dict")
        eq(NewrRt.lenOf(setOf(1L, 2L, 3L)), 3L, "len set")
    }
    check("dictOf") {
        val d: LinkedHashMap<String, Long> = NewrRt.dictOf("a", 1L, "b", 2L)
        eq(d["a"], 1L, "dictOf a")
        eq(d.size, 2, "dictOf size")
        eq(d.keys.toList(), listOf("a", "b"), "dictOf insertion order")
        val withNull: LinkedHashMap<String, Long?> = NewrRt.dictOf("a", null)
        ok(withNull["a"] == null, "dictOf null value allowed")
    }
    check("envGet") {
        eq(NewrRt.envGet("NEWR_RT_CHECK_ABSENT_XYZ"), null, "envGet absent")
        eq(NewrRt.envGet("NEWR_RT_CHECK_ABSENT_XYZ", "dflt"), "dflt", "envGet default")
        ok(NewrRt.envGet("PATH") != null, "envGet PATH present")
    }
    check("envArgs") {
        eq(NewrRt.envArgs(), emptyList<String>(), "envArgs default empty")
        NewrRt._mainArgs = arrayOf("a", "b")
        eq(NewrRt.envArgs(), listOf("a", "b"), "envArgs set")
        NewrRt._mainArgs = arrayOf()
    }
    check("hashOf") {
        eq(NewrRt.hashOf(0.0), NewrRt.hashOf(-0.0), "hashOf -0.0 == 0.0")
        eq(NewrRt.hashOf(null), 0, "hashOf null")
        eq(NewrRt.hashOf("x"), NewrRt.hashOf("x"), "hashOf string stable")
        eq(NewrRt.hashOf(listOf(1L, "a")),
            NewrRt.hashOf(listOf(1L, "a")), "hashOf list by elements")
    }
    check("structHash") {
        eq(NewrRt.structHash(1L, "a"), NewrRt.structHash(1L, "a"), "structHash stable")
        eq(NewrRt.structHash(-0.0), NewrRt.structHash(0.0), "structHash -0.0 == 0.0")
    }

    println("\nRtChecks.kt: $passed checks passed")
}
