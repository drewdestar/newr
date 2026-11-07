// Java-side unit tests for compiler/newr_rt.java (M4).
//
// Mirrors tests/js/rt_tests.js case for case (same rule, both
// targets) - same 61 checks, adapted to Java types (long instead of
// BigInt, Long/Double boxing at the helper boundaries). Run with
// `javac NewrRt.java RtChecks.java && java RtChecks` from a temp
// dir holding copies of both files, or via
// `python -m unittest tests.test_newr_rt_java`.
//
// Every failure prints the assertion and exits non-zero. Keep the
// battery aligned with the JS file: when a rule is added here, add
// it there too (or record why not).

import java.io.IOException;
import java.math.BigInteger;
import java.nio.file.Files;
import java.nio.file.NoSuchFileException;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.HashMap;
import java.util.HashSet;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;

public class RtChecks {
    static int passed = 0;

    static void check(String name, Runnable fn) {
        fn.run();
        passed++;
        System.out.println("ok " + name);
    }

    // One eq only: primitive overloads alongside the Object one
    // make (Long, long) calls ambiguous in javac (both applicable
    // in the boxing phase, neither more specific). Everything boxes;
    // Long/Double.equals do the right thing (NaN is never eq'd
    // through here - the checks use isNaN / bit comparison).
    static void eq(Object got, Object want, String what) {
        if (got == null ? want != null : !got.equals(want)) {
            throw new AssertionError(what + ": expected <" + want
                + "> (" + (want == null ? "null" : want.getClass().getSimpleName())
                + "), got <" + got + ">"
                + (got == null ? "" : " (" + got.getClass().getSimpleName() + ")"));
        }
    }

    static void ok(boolean cond, String what) {
        if (!cond) {
            throw new AssertionError("expected true: " + what);
        }
    }

    static void isTrue(boolean cond, String what) {
        ok(cond, what);
    }

    /** Expect a throw whose NewrError code equals `code` (null allowed). */
    static NewrRt.NewrError throwsCode(Runnable fn, String code) {
        try {
            fn.run();
        } catch (NewrRt.NewrError e) {
            eq(e.code, code, "error code");
            return e;
        } catch (Throwable t) {
            throw new AssertionError("expected NewrError code " + code
                + ", got " + t.getClass().getSimpleName() + ": " + t.getMessage());
        }
        throw new AssertionError("expected code " + code + ", nothing thrown");
    }

    /** Expect a throw of the named checked-int error class, code null. */
    static NewrRt.NewrError throwsNamed(Runnable fn, String name) {
        try {
            fn.run();
        } catch (NewrRt.NewrError e) {
            eq(e.getClass().getSimpleName(), name, "error class");
            eq(e.code, null, "error code (must be null)");
            return e;
        } catch (Throwable t) {
            throw new AssertionError("expected " + name + ", got "
                + t.getClass().getSimpleName() + ": " + t.getMessage());
        }
        throw new AssertionError("expected " + name + ", nothing thrown");
    }

    public static void main(String[] args) throws Exception {
        // ---------------------------------------------------------------- constants
        check("int64 bounds", () -> {
            eq(NewrRt.INT64_MIN, Long.MIN_VALUE, "INT64_MIN");
            eq(NewrRt.INT64_MAX, Long.MAX_VALUE, "INT64_MAX");
        });

        // ---------------------------------------------------------------- checked arithmetic
        check("add/sub/mul ok", () -> {
            eq(NewrRt.add(2, 3), 5L, "add");
            eq(NewrRt.sub(10, 4), 6L, "sub");
            eq(NewrRt.mul(3, 4), 12L, "mul");
        });
        check("add overflow", () -> {
            throwsNamed(() -> NewrRt.add(Long.MAX_VALUE, 1), "NewrOverflowError");
        });
        check("sub underflow", () -> {
            throwsNamed(() -> NewrRt.sub(Long.MIN_VALUE, 1), "NewrOverflowError");
        });
        check("mul overflow", () -> {
            throwsNamed(() -> NewrRt.mul(Long.MAX_VALUE, 2), "NewrOverflowError");
        });
        check("neg", () -> {
            eq(NewrRt.neg(5), -5L, "neg 5");
            eq(NewrRt.neg(-5), 5L, "neg -5");
            throwsNamed(() -> NewrRt.neg(Long.MIN_VALUE), "NewrOverflowError");
        });
        check("checkInt", () -> {
            eq(NewrRt.checkInt(0L), 0L, "checkInt 0");
            throwsNamed(() -> NewrRt.checkInt(BigInteger.ONE.shiftLeft(63)),
                "NewrOverflowError");
        });
        check("overflow error carries message", () -> {
            NewrRt.NewrError e = throwsNamed(
                () -> NewrRt.add(Long.MAX_VALUE, 1), "NewrOverflowError");
            ok(e.getMessage().contains("9223372036854775808"), "message mentions value");
            eq(NewrRt.asError(e), e, "asError passes NewrError through");
        });

        // ---------------------------------------------------------------- tdiv/tmod
        check("truncating division", () -> {
            eq(NewrRt.tdiv(7, 2), 3L, "7/2");
            eq(NewrRt.tdiv(-7, 2), -3L, "-7/2");
            eq(NewrRt.tdiv(7, -2), -3L, "7/-2");
            eq(NewrRt.tdiv(-7, -2), 3L, "-7/-2");
        });
        check("dividend-sign modulo", () -> {
            eq(NewrRt.tmod(7, 3), 1L, "7%3");
            eq(NewrRt.tmod(-7, 3), -1L, "-7%3");
            eq(NewrRt.tmod(7, -3), 1L, "7%-3");
            eq(NewrRt.tmod(-7, -3), -1L, "-7%-3");
        });
        check("division by zero", () -> {
            throwsNamed(() -> NewrRt.tdiv(1, 0), "NewrDivisionByZeroError");
            throwsNamed(() -> NewrRt.tmod(1, 0), "NewrDivisionByZeroError");
        });
        check("min div minus one", () -> {
            throwsNamed(() -> NewrRt.tdiv(Long.MIN_VALUE, -1), "NewrOverflowError");
            eq(NewrRt.tmod(Long.MIN_VALUE, -1), 0L, "MIN%-1");
        });
        check("div/mod invariant", () -> {
            long[][] cases = {{7, 3}, {-7, 3}, {7, -3}, {-7, -3}, {100, 7}, {-100, 7}};
            for (long[] c : cases) {
                eq(NewrRt.tdiv(c[0], c[1]) * c[1] + NewrRt.tmod(c[0], c[1]), c[0],
                    "invariant " + c[0] + "/" + c[1]);
            }
        });

        // ---------------------------------------------------------------- pow
        check("powInt ok", () -> {
            eq(NewrRt.powInt(2, 10), 1024L, "2^10");
            eq(NewrRt.powInt(2, 0), 1L, "2^0");
            eq(NewrRt.powInt(0, 0), 1L, "0^0");
            eq(NewrRt.powInt(0, 5), 0L, "0^5");
            eq(NewrRt.powInt(-2, 3), -8L, "(-2)^3");
            eq(NewrRt.powInt(-2, 2), 4L, "(-2)^2");
        });
        check("powInt overflow", () -> {
            throwsNamed(() -> NewrRt.powInt(2, 63), "NewrOverflowError");
        });
        check("powInt negative exponent", () -> {
            throwsCode(() -> NewrRt.powInt(2, -1), null);
        });
        check("powInt huge exponent does not hang", () -> {
            long t0 = System.currentTimeMillis();
            throwsNamed(() -> NewrRt.powInt(10, 1_000_000_000L), "NewrOverflowError");
            ok(System.currentTimeMillis() - t0 < 5000, "powInt(10, 1e9) took too long");
        });
        check("powFloat plain", () -> {
            eq(NewrRt.powFloat(2.0, 3.0), 8.0, "2.0^3");
            eq(NewrRt.powFloat(9.0, 0.5), 3.0, "9.0^0.5");
        });
        check("powFloat negative base fractional exponent is NaN", () -> {
            ok(Double.isNaN(NewrRt.powFloat(-8.0, 1.0 / 3.0)), "(-8)^(1/3) is NaN");
        });
        check("powFloat overflow is infinity", () -> {
            eq(NewrRt.powFloat(1e308, 2.0), Double.POSITIVE_INFINITY, "1e308^2");
            eq(NewrRt.powFloat(-1e308, 3.0), Double.NEGATIVE_INFINITY, "(-1e308)^3");
        });
        check("powFloat zero base negative exponent throws", () -> {
            throwsCode(() -> NewrRt.powFloat(0, -1), "ZeroDivisionError");
        });
        check("fmod dividend sign", () -> {
            eq(NewrRt.fmod(-7.0, 2.0), -1.0, "fmod(-7,2)");
            eq(NewrRt.fmod(7.0, -2.0), 1.0, "fmod(7,-2)");
        });
        check("fmod by zero", () -> {
            throwsNamed(() -> NewrRt.fmod(1.0, 0.0), "NewrDivisionByZeroError");
        });
        check("fdiv", () -> {
            eq(NewrRt.fdiv(7.0, 2.0), 3.5, "7.0/2.0");
            eq(NewrRt.fdiv(-7.0, 2.0), -3.5, "-7.0/2.0");
            eq(NewrRt.fdiv(1.0, 3.0), 1.0 / 3.0, "1.0/3.0");
        });
        check("fdiv by zero", () -> {
            throwsCode(() -> NewrRt.fdiv(1.0, 0.0), "ZeroDivisionError");
            throwsCode(() -> NewrRt.fdiv(0.0, 0.0), "ZeroDivisionError");
            throwsCode(() -> NewrRt.fdiv(1.0, -0.0), "ZeroDivisionError");
        });

        // ---------------------------------------------------------------- round (exact value)
        check("round ties go away from zero", () -> {
            eq(NewrRt.roundHalfAway(2.5), 3L, "round 2.5");
            eq(NewrRt.roundHalfAway(-2.5), -3L, "round -2.5");
            eq(NewrRt.roundHalfAway(3.5), 4L, "round 3.5");
            eq(NewrRt.roundHalfAway(-3.5), -4L, "round -3.5");
        });
        check("round plain values", () -> {
            eq(NewrRt.roundHalfAway(2.4), 2L, "round 2.4");
            eq(NewrRt.roundHalfAway(2.6), 3L, "round 2.6");
            eq(NewrRt.roundHalfAway(0.5), 1L, "round 0.5");
        });
        check("round rounds the exact float value", () -> {
            eq(NewrRt.roundHalfAway(2.675, 2), 2.67, "round 2.675,2");
            eq(NewrRt.roundHalfAway(1.005, 2), 1.0, "round 1.005,2");
            eq(NewrRt.roundHalfAway(0.125, 2), 0.13, "round 0.125,2");
            eq(NewrRt.roundHalfAway(9223372036854774784.0), 9223372036854774784L,
                "round huge integral float");
        });
        check("round ndigits returns float", () -> {
            eq(NewrRt.roundHalfAway(123.456, 1), 123.5, "round 123.456,1");
            eq(NewrRt.roundHalfAway(999.999, 2), 1000.0, "round 999.999,2");
        });
        check("round negative ndigits", () -> {
            eq(NewrRt.roundHalfAway(5, -1), 10.0, "round 5,-1");
            eq(NewrRt.roundHalfAway(15, -1), 20.0, "round 15,-1");
            eq(NewrRt.roundHalfAway(1250, -2), 1300.0, "round 1250,-2");
        });
        check("round overflow", () -> {
            throwsNamed(() -> NewrRt.roundHalfAway(1e30), "NewrOverflowError");
        });
        check("round non-finite", () -> {
            throwsCode(() -> NewrRt.roundHalfAway(Double.POSITIVE_INFINITY),
                "OverflowError");
            eq(NewrRt.roundHalfAway(Double.POSITIVE_INFINITY, 2),
                Double.POSITIVE_INFINITY, "round inf,2");
            ok(Double.isNaN(NewrRt.roundHalfAway(Double.NaN, 2)), "round nan,2");
        });
        check("round negative zero", () -> {
            eq(Double.doubleToRawLongBits(NewrRt.roundHalfAway(-0.001, 2)),
                Double.doubleToRawLongBits(-0.0), "round -0.001,2 is -0.0");
        });

        // ---------------------------------------------------------------- float rendering (J4)
        check("floatStr matches Python repr", () -> {
            Object[][] cases = {
                {0.0, "0.0"}, {-0.0, "-0.0"}, {1.0, "1.0"}, {-1.5, "-1.5"},
                {1e15, "1000000000000000.0"}, {1e16, "1e+16"}, {1e20, "1e+20"},
                {1e21, "1e+21"}, {0.0001, "0.0001"}, {0.00001, "1e-05"},
                {1.23456789e-10, "1.23456789e-10"}, {123456.789, "123456.789"},
                {2.675, "2.675"}, {0.1, "0.1"}, {0.30000000000000004, "0.30000000000000004"},
                {1e-7, "1e-07"}, {5e-324, "5e-324"},
                {1.7976931348623157e308, "1.7976931348623157e+308"},
                {3.141592653589793, "3.141592653589793"}, {100.0, "100.0"},
                {2.5, "2.5"}, {-2.5e-5, "-2.5e-05"},
                {123456789.12345679, "123456789.12345679"},
                {9.223372036854776e18, "9.223372036854776e+18"},
                {1234560000.0, "1234560000.0"},
            };
            for (Object[] c : cases) {
                eq(NewrRt.floatStr((Double) c[0]), (String) c[1],
                    "floatStr(" + c[0] + ")");
            }
            eq(NewrRt.floatStr(Double.NaN), "nan", "floatStr nan");
            eq(NewrRt.floatStr(Double.POSITIVE_INFINITY), "inf", "floatStr inf");
            eq(NewrRt.floatStr(Double.NEGATIVE_INFINITY), "-inf", "floatStr -inf");
        });

        // ---------------------------------------------------------------- conversions
        check("toInt from str", () -> {
            eq(NewrRt.toInt("42"), 42L, "toInt 42");
            eq(NewrRt.toInt("  -42 "), -42L, "toInt -42");
            eq(NewrRt.toInt("+7"), 7L, "toInt +7");
            throwsCode(() -> NewrRt.toInt("3.5"), "ValueError");
            throwsCode(() -> NewrRt.toInt("abc"), "ValueError");
        });
        check("toInt range checked", () -> {
            throwsNamed(() -> NewrRt.toInt("99999999999999999999"),
                "NewrOverflowError");
        });
        check("toInt from float truncates", () -> {
            eq(NewrRt.toInt(3.7), 3L, "toInt 3.7");
            eq(NewrRt.toInt(-3.7), -3L, "toInt -3.7");
            throwsCode(() -> NewrRt.toInt(Double.POSITIVE_INFINITY), "OverflowError");
        });
        check("toInt from bool", () -> {
            eq(NewrRt.toInt(true), 1L, "toInt true");
            eq(NewrRt.toInt(false), 0L, "toInt false");
        });
        check("toFloat", () -> {
            eq(NewrRt.toFloat(2L), 2.0, "toFloat 2");
            eq(NewrRt.toFloat("inf"), Double.POSITIVE_INFINITY, "toFloat inf");
            eq(NewrRt.toFloat("-inf"), Double.NEGATIVE_INFINITY, "toFloat -inf");
            ok(Double.isNaN(NewrRt.toFloat("nan")), "toFloat nan");
            eq(NewrRt.toFloat("1_000"), 1000.0, "toFloat 1_000");
            throwsCode(() -> NewrRt.toFloat("abc"), "ValueError");
            throwsCode(() -> NewrRt.toFloat(BigInteger.TEN.pow(400)), "OverflowError");
        });
        check("toBool parses", () -> {
            eq(NewrRt.toBool("true"), true, "toBool true");
            eq(NewrRt.toBool("false"), false, "toBool false");
            throwsCode(() -> NewrRt.toBool("yes"), "ValueError");
            eq(NewrRt.toBool(0L), false, "toBool 0");
            eq(NewrRt.toBool(1L), true, "toBool 1");
            eq(NewrRt.toBool(0.0), false, "toBool 0.0");
        });

        // ---------------------------------------------------------------- toStr
        check("toStr scalars", () -> {
            eq(NewrRt.toStr(null), "none", "toStr null");
            eq(NewrRt.toStr(true), "true", "toStr true");
            eq(NewrRt.toStr(false), "false", "toStr false");
            eq(NewrRt.toStr(42L), "42", "toStr 42");
            eq(NewrRt.toStr(1.5), "1.5", "toStr 1.5");
            eq(NewrRt.toStr(1.0), "1.0", "toStr 1.0");
            eq(NewrRt.toStr("a"), "a", "toStr a");
        });
        check("toStr collections", () -> {
            eq(NewrRt.toStr(new ArrayList<>(Arrays.asList(1L, 2L, 3L))), "[1, 2, 3]",
                "toStr list");
            eq(NewrRt.toStr(new ArrayList<>(Arrays.asList(true, false))), "[true, false]",
                "toStr bools");
            Map<String, Long> m = new LinkedHashMap<>();
            m.put("k", 1L);
            m.put("j", 2L);
            eq(NewrRt.toStr(m), "{k: 1, j: 2}", "toStr dict");
            eq(NewrRt.toStr(new HashSet<>(Arrays.asList("a"))), "{a}", "toStr set");
            eq(NewrRt.toStr(new ArrayList<>()), "[]", "toStr empty list");
        });
        check("toStr error and result", () -> {
            eq(NewrRt.toStr(new NewrRt.NewrError("boom")), "boom", "toStr error");
            eq(NewrRt.toStr(NewrRt.ok(3L)), "ok(3)", "toStr ok");
            eq(NewrRt.toStr(NewrRt.err(new NewrRt.NewrError("bad"))), "err(bad)",
                "toStr err");
        });
        check("toStr struct-like values", () -> {
            Object p = new Object() {
                @Override public String toString() {
                    return "Point(x: 1, y: 2)";
                }
            };
            eq(NewrRt.toStr(p), "Point(x: 1, y: 2)", "toStr struct-like");
        });

        // ---------------------------------------------------------------- Result
        check("Result ok/err", () -> {
            NewrRt.Result<Long, NewrRt.NewrError> r = NewrRt.ok(7L);
            eq(r.isOk(), true, "ok isOk");
            eq(r.isErr(), false, "ok isErr");
            eq(r.unwrap(), 7L, "ok unwrap");
            eq(r.unwrapOr(0L), 7L, "ok unwrapOr");
            NewrRt.Result<Long, NewrRt.NewrError> e =
                NewrRt.err(new NewrRt.NewrError("bad"));
            eq(e.isErr(), true, "err isErr");
            eq(e.unwrapOr(9L), 9L, "err unwrapOr");
            throwsCode(e::unwrap, null);
        });
        check("tryResult", () -> {
            NewrRt.Result<Long, NewrRt.NewrError> r1 =
                NewrRt.tryResult(() -> 6L);
            eq(r1.isOk(), true, "try ok");
            eq(r1.unwrap(), 6L, "try unwrap");
            NewrRt.Result<Long, NewrRt.NewrError> r2 =
                NewrRt.tryResult(() -> { throw new RuntimeException("x"); });
            eq(r2.isErr(), true, "try err");
            eq(r2._error.getMessage(), "x", "try err message");
        });

        // ---------------------------------------------------------------- equality
        check("eq primitives and collections", () -> {
            eq(NewrRt.eq(1L, 1L), true, "eq 1,1");
            eq(NewrRt.eq(1L, 2L), false, "eq 1,2");
            eq(NewrRt.eq(1L, 1), false, "eq Long,Int");
            eq(NewrRt.eq("a", "a"), true, "eq str");
            eq(NewrRt.eq(null, null), true, "eq null,null");
            eq(NewrRt.eq(null, 0L), false, "eq null,0");
            eq(NewrRt.eq(new ArrayList<>(Arrays.asList(1L, 2L)),
                new ArrayList<>(Arrays.asList(1L, 2L))), true, "eq lists");
            eq(NewrRt.eq(new ArrayList<>(Arrays.asList(1L)),
                new ArrayList<>(Arrays.asList(1L, 2L))), false, "eq lists len");
            Map<String, Long> m1 = new HashMap<>();
            m1.put("a", 1L);
            Map<String, Long> m2 = new HashMap<>();
            m2.put("a", 1L);
            Map<String, Long> m3 = new HashMap<>();
            m3.put("a", 2L);
            eq(NewrRt.eq(m1, m2), true, "eq dicts");
            eq(NewrRt.eq(m1, m3), false, "eq dicts differ");
            eq(NewrRt.eq(new HashSet<>(Arrays.asList(1L, 2L)),
                new HashSet<>(Arrays.asList(2L, 1L))), true, "eq sets");
            eq(NewrRt.eq(NewrRt.ok(1L), NewrRt.ok(1L)), true, "eq ok");
            eq(NewrRt.eq(NewrRt.ok(1L), NewrRt.err(new NewrRt.NewrError("x"))),
                false, "eq ok vs err");
            eq(NewrRt.eq(Double.NaN, Double.NaN), false, "eq NaN");
        });

        // ---------------------------------------------------------------- ?. / ?? / accessors
        check("optChain and coalesce", () -> {
            eq(NewrRt.optChain(null, (Long v) -> v + 1), null, "optChain null");
            eq(NewrRt.optChain(5L, (Long v) -> v + 1), 6L, "optChain 5");
            eq(NewrRt.coalesce(0L, () -> 5L), 0L, "coalesce 0");
            eq(NewrRt.coalesce(null, () -> 5L), 5L, "coalesce null");
            eq(NewrRt.coalesce("", () -> "d"), "", "coalesce empty str");
        });
        check("listIndex", () -> {
            eq(NewrRt.listIndex(Arrays.asList(1L, 2L), 1), 2L, "listIndex 1");
            throwsCode(() -> NewrRt.listIndex(Arrays.asList(1L), -1), "IndexError");
            throwsCode(() -> NewrRt.listIndex(Arrays.asList(1L), 1), "IndexError");
        });
        check("listGet", () -> {
            eq(NewrRt.listGet(Arrays.asList(1L, 2L), 0, 9L), 1L, "listGet 0");
            eq(NewrRt.listGet(Arrays.asList(1L, 2L), 5, 9L), 9L, "listGet 5");
            eq(NewrRt.listGet(Arrays.asList(1L, 2L), -1, 9L), 9L, "listGet -1");
        });
        check("listContains structural", () -> {
            eq(NewrRt.listContains(Arrays.asList(1L, 2L), 2L), true, "contains 2");
            eq(NewrRt.listContains(Arrays.asList(1L, 2L), 9L), false, "contains 9");
            eq(NewrRt.listContains(
                Arrays.asList(Arrays.asList(1L)), Arrays.asList(1L)),
                true, "contains nested");
        });
        check("setRemove", () -> {
            Set<Long> s = new HashSet<>(Arrays.asList(1L, 2L));
            NewrRt.setRemove(s, 1L);
            eq(s.contains(1L), false, "setRemove removed");
            throwsCode(() -> NewrRt.setRemove(s, 99L), "KeyError");
        });
        check("unwrapOpt", () -> {
            eq(NewrRt.unwrapOpt(5L), 5L, "unwrapOpt 5");
            throwsCode(() -> NewrRt.unwrapOpt(null), null);
        });
        check("range", () -> {
            eq(NewrRt.range(3), Arrays.asList(0L, 1L, 2L), "range 3");
            eq(NewrRt.range(1, 4), Arrays.asList(1L, 2L, 3L), "range 1,4");
            eq(NewrRt.range(0, 10, 3), Arrays.asList(0L, 3L, 6L, 9L), "range 0,10,3");
            eq(NewrRt.range(5, 0, -2), Arrays.asList(5L, 3L, 1L), "range 5,0,-2");
            throwsCode(() -> NewrRt.range(0, 5, 0), "ValueError");
        });

        // ---------------------------------------------------------------- file ops
        check("file read/write/append", () -> {
            try {
                Path d = Files.createTempDirectory("newr-rt-");
                Path p = d.resolve("t.txt");
                NewrRt.fileWrite(p.toString(), "hello");
                eq(NewrRt.fileExists(p.toString()), true, "file exists");
                eq(NewrRt.fileRead(p.toString()), "hello", "file read");
                NewrRt.fileAppend(p.toString(), " world");
                eq(NewrRt.fileRead(p.toString()), "hello world", "file append");
                eq(NewrRt.fileExists(d.resolve("nope").toString()), false,
                    "file missing");
            } catch (IOException e) {
                throw new RuntimeException(e);
            }
        });
        check("file read missing raises", () -> {
            try {
                NewrRt.fileRead("/definitely/not/here/xyz.txt");
                throw new AssertionError("expected a throw");
            } catch (NewrRt.NewrError e) {
                // Host-specific code (Java: NoSuchFileException;
                // Python: FileNotFoundError; JS: ENOENT).
                eq(e.code, "NoSuchFileException", "missing file code");
            }
        });

        // ---------------------------------------------------------------- time
        check("timeFormat UTC", () -> {
            eq(NewrRt.timeFormat(1700000000L, "%Y-%m-%d"), "2023-11-14", "fmt date");
            eq(NewrRt.timeFormat(0L, "%H:%M:%S"), "00:00:00", "fmt time");
            eq(NewrRt.timeFormat(1700000000L, "%d/%m/%Y"), "14/11/2023", "fmt dmy");
            throwsCode(() -> NewrRt.timeFormat(0L, "%c"), null);
        });

        // ---------------------------------------------------------------- random
        check("randomInt in range", () -> {
            for (int i = 0; i < 50; i++) {
                long r = NewrRt.randomInt(1, 10);
                ok(r >= 1 && r <= 10, "randomInt out of range: " + r);
            }
            eq(NewrRt.randomInt(5, 5), 5L, "randomInt 5,5");
            long big = NewrRt.randomInt(0, Long.MAX_VALUE);
            ok(big >= 0, "randomInt wide range");
        });
        check("randomChoice", () -> {
            List<Long> xs = Arrays.asList(1L, 2L, 3L);
            ok(xs.contains(NewrRt.randomChoice(xs)), "randomChoice in list");
            throwsCode(() -> NewrRt.randomChoice(new ArrayList<>()), "IndexError");
        });
        check("randomShuffle", () -> {
            List<Long> xs = new ArrayList<>(Arrays.asList(1L, 2L, 3L, 4L, 5L));
            NewrRt.randomShuffle(xs);
            List<Long> sorted = new ArrayList<>(xs);
            java.util.Collections.sort(sorted);
            eq(sorted, Arrays.asList(1L, 2L, 3L, 4L, 5L), "shuffle keeps elements");
        });

        // ---------------------------------------------------------------- asError
        check("asError", () -> {
            NewrRt.NewrError n = new NewrRt.NewrError("x", "E1");
            eq(NewrRt.asError(n), n, "asError passthrough");
            eq(NewrRt.asError("boom").getMessage(), "boom", "asError string");
            NewrRt.NewrError wrapped =
                NewrRt.asError(new IllegalStateException("nope"));
            eq(wrapped.getMessage(), "nope", "asError message");
            eq(wrapped.code, "IllegalStateException", "asError code");
            eq(NewrRt.asError(42).getMessage(), "42", "asError int");
        });

        // ---------------------------------------------------------------- strings (helpers added for the C# emitter, M4)
        // The C# driver has the same 10 checks, so the two runtimes
        // stay aligned.
        check("chars code points", () -> {
            eq(NewrRt.chars("a\uD83D\uDE00b"),
               Arrays.asList("a", "\uD83D\uDE00", "b"), "chars astral");
            eq(NewrRt.chars(""), new ArrayList<>(), "chars empty");
            eq(NewrRt.chars("abc"), Arrays.asList("a", "b", "c"), "chars ascii");
        });
        check("splitStr literal separator", () -> {
            eq(NewrRt.splitStr("a,b,c", ","), Arrays.asList("a", "b", "c"), "split basic");
            eq(NewrRt.splitStr("a,,b", ","), Arrays.asList("a", "", "b"), "split empty middle");
            eq(NewrRt.splitStr("a,", ","), Arrays.asList("a", ""), "split trailing empty kept");
            eq(NewrRt.splitStr("a.b", "."), Arrays.asList("a", "b"), "split literal dot, not regex");
            eq(NewrRt.splitStr("abc", ","), Arrays.asList("abc"), "split no match");
        });
        check("strIndex code points", () -> {
            eq(NewrRt.strIndex("hello", 1), "e", "strIndex 1");
            eq(NewrRt.strIndex("hello", -1), "o", "strIndex -1");
            eq(NewrRt.strIndex("a\uD83D\uDE00b", 1), "\uD83D\uDE00", "strIndex astral");
            throwsCode(() -> NewrRt.strIndex("ab", 2), "IndexError");
            throwsCode(() -> NewrRt.strIndex("ab", -3), "IndexError");
        });
        check("absLong", () -> {
            eq(NewrRt.absLong(5), 5L, "abs 5");
            eq(NewrRt.absLong(-5), 5L, "abs -5");
            eq(NewrRt.absLong(0), 0L, "abs 0");
            throwsCode(() -> NewrRt.absLong(Long.MIN_VALUE), null);
        });
        check("lenOf", () -> {
            eq(NewrRt.lenOf("a\uD83D\uDE00b"), 3L, "len str code points");
            eq(NewrRt.lenOf(Arrays.asList(1L, 2L)), 2L, "len list");
            Map<String, Long> d = new LinkedHashMap<>();
            d.put("a", 1L);
            eq(NewrRt.lenOf(d), 1L, "len dict");
            eq(NewrRt.lenOf(new HashSet<>(Arrays.asList(1L, 2L, 3L))), 3L, "len set");
        });
        check("dictOf", () -> {
            LinkedHashMap<String, Long> d = NewrRt.dictOf("a", 1L, "b", 2L);
            eq(d.get("a"), 1L, "dictOf a");
            eq(d.size(), 2, "dictOf size");
            eq(new ArrayList<>(d.keySet()), Arrays.asList("a", "b"), "dictOf insertion order");
            LinkedHashMap<String, Long> withNull = NewrRt.dictOf("a", null);
            ok(withNull.get("a") == null, "dictOf null value allowed");
        });
        check("envGet", () -> {
            eq(NewrRt.envGet("NEWR_RT_CHECK_ABSENT_XYZ"), null, "envGet absent");
            eq(NewrRt.envGet("NEWR_RT_CHECK_ABSENT_XYZ", "dflt"), "dflt", "envGet default");
            ok(NewrRt.envGet("PATH") != null, "envGet PATH present");
        });
        check("envArgs", () -> {
            eq(NewrRt.envArgs(), new ArrayList<>(), "envArgs default empty");
            NewrRt._mainArgs = new String[] { "a", "b" };
            eq(NewrRt.envArgs(), Arrays.asList("a", "b"), "envArgs set");
            NewrRt._mainArgs = new String[0];
        });
        check("hashOf", () -> {
            eq(NewrRt.hashOf(0.0), NewrRt.hashOf(-0.0), "hashOf -0.0 == 0.0");
            eq(NewrRt.hashOf(null), 0, "hashOf null");
            eq(NewrRt.hashOf("x"), NewrRt.hashOf("x"), "hashOf string stable");
            eq(NewrRt.hashOf(Arrays.asList(1L, "a")),
               NewrRt.hashOf(Arrays.asList(1L, "a")), "hashOf list by elements");
        });
        check("structHash", () -> {
            eq(NewrRt.structHash(1L, "a"), NewrRt.structHash(1L, "a"), "structHash stable");
            eq(NewrRt.structHash(-0.0), NewrRt.structHash(0.0), "structHash -0.0 == 0.0");
        });

        System.out.println("\nRtChecks.java: " + passed + " checks passed");
    }
}
