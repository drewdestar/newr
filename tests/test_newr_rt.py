"""Unit tests for the checked-int runtime (compiler/newr_rt.py, M2).

TDD RED first: this file was written before the runtime module
existed. Every test pins a decision from BUILD_REPORT.md §14.1/§14.2
(P2, P6–P9, D5–D9) or the research §3.5/§3.6 sketches — checked
overflow, truncating division, half-away rounding, and the
conversion/rendering helpers.
"""

import math
import os
import tempfile
import threading
import unittest

from compiler.newr_rt import (
    INT64_MAX,
    INT64_MIN,
    NewrDivisionByZeroError,
    NewrError,
    NewrOverflowError,
    Result,
    add,
    as_error,
    check_int,
    coalesce,
    db_begin,
    db_close,
    db_commit,
    db_exec,
    db_last_id,
    db_open,
    db_query,
    db_rollback,
    err,
    file_append,
    file_exists,
    file_read,
    file_write,
    fmod,
    list_get,
    list_index,
    mul,
    neg,
    ok,
    opt_chain,
    pow_float,
    pow_int,
    round_half_away,
    sub,
    tdiv,
    tmod,
    to_bool,
    to_float,
    to_int,
    to_str,
    try_result,
    unwrap_opt,
)


class TestConstants(unittest.TestCase):
    def test_int64_bounds(self):
        self.assertEqual(INT64_MIN, -(2 ** 63))
        self.assertEqual(INT64_MAX, 2 ** 63 - 1)


class TestCheckedArithmetic(unittest.TestCase):
    def test_add_sub_mul_ok(self):
        self.assertEqual(add(2, 3), 5)
        self.assertEqual(sub(2, 3), -1)
        self.assertEqual(mul(6, 7), 42)
        self.assertEqual(add(INT64_MAX, 0), INT64_MAX)
        self.assertEqual(sub(INT64_MIN, 0), INT64_MIN)

    def test_add_overflow(self):
        with self.assertRaises(NewrOverflowError):
            add(INT64_MAX, 1)
        with self.assertRaises(NewrOverflowError):
            add(INT64_MAX, INT64_MAX)

    def test_sub_underflow(self):
        with self.assertRaises(NewrOverflowError):
            sub(INT64_MIN, 1)

    def test_mul_overflow(self):
        with self.assertRaises(NewrOverflowError):
            mul(INT64_MAX, 2)
        with self.assertRaises(NewrOverflowError):
            mul(INT64_MIN, -1)

    def test_neg(self):
        self.assertEqual(neg(5), -5)
        self.assertEqual(neg(-5), 5)
        self.assertEqual(neg(0), 0)
        with self.assertRaises(NewrOverflowError):
            neg(INT64_MIN)

    def test_check_int(self):
        self.assertEqual(check_int(0), 0)
        self.assertEqual(check_int(INT64_MAX), INT64_MAX)
        with self.assertRaises(NewrOverflowError):
            check_int(INT64_MAX + 1)
        with self.assertRaises(NewrOverflowError):
            check_int(INT64_MIN - 1)

    def test_overflow_is_overflow_error(self):
        # Research §3.3: raising OverflowError for checked-int
        # overflow is idiomatic — a bare `except OverflowError`
        # sees it too.
        self.assertIsInstance(NewrOverflowError("x"), OverflowError)
        self.assertIsInstance(NewrOverflowError("x"), NewrError)

    def test_overflow_error_carries_message(self):
        # BUG-M2-2 (found by the emitter's overflow test): with the
        # bases ordered (OverflowError, NewrError), constructing a
        # NewrOverflowError never ran NewrError.__init__, so `.message`
        # was never set — and a `catch e:` body reading `e.message`
        # raised AttributeError instead of printing the overflow.
        # The same applies to NewrDivisionByZeroError.
        e = NewrOverflowError("integer overflow: 1 is outside int64 range")
        self.assertEqual(e.message,
                         "integer overflow: 1 is outside int64 range")
        self.assertIs(as_error(e), e)
        d = NewrDivisionByZeroError("division by zero")
        self.assertEqual(d.message, "division by zero")
        self.assertIsInstance(d, ZeroDivisionError)
        self.assertIs(as_error(d), d)


class TestTdivTmod(unittest.TestCase):
    def test_truncating_division(self):
        self.assertEqual(tdiv(7, 2), 3)
        self.assertEqual(tdiv(-7, 2), -3)  # Python // gives -4
        self.assertEqual(tdiv(7, -2), -3)
        self.assertEqual(tdiv(-7, -2), 3)
        self.assertEqual(tdiv(0, 5), 0)
        self.assertEqual(tdiv(5, 5), 1)

    def test_dividend_sign_modulo(self):
        self.assertEqual(tmod(7, 2), 1)
        self.assertEqual(tmod(-7, 2), -1)  # Python % gives 1
        self.assertEqual(tmod(7, -2), 1)
        self.assertEqual(tmod(-7, -2), -1)
        self.assertEqual(tmod(0, 5), 0)

    def test_division_by_zero(self):
        with self.assertRaises(NewrDivisionByZeroError):
            tdiv(1, 0)
        with self.assertRaises(NewrDivisionByZeroError):
            tmod(1, 0)
        self.assertIsInstance(NewrDivisionByZeroError("x"),
                              ZeroDivisionError)

    def test_min_div_minus_one(self):
        with self.assertRaises(NewrOverflowError):
            tdiv(INT64_MIN, -1)
        # The remainder is mathematically 0 — no overflow.
        self.assertEqual(tmod(INT64_MIN, -1), 0)

    def test_invariant(self):
        for a in (-100, -7, -1, 0, 1, 7, 100):
            for b in (-7, -2, 2, 7):
                self.assertEqual(tdiv(a, b) * b + tmod(a, b), a)


class TestPowInt(unittest.TestCase):
    def test_pow_ok(self):
        self.assertEqual(pow_int(2, 10), 1024)
        self.assertEqual(pow_int(-2, 3), -8)
        self.assertEqual(pow_int(5, 0), 1)
        self.assertEqual(pow_int(0, 0), 1)
        self.assertEqual(pow_int(2, 62), 2 ** 62)

    def test_pow_overflow(self):
        with self.assertRaises(NewrOverflowError):
            pow_int(2, 63)
        with self.assertRaises(NewrOverflowError):
            pow_int(10, 19)

    def test_pow_negative_exponent(self):
        # An int result cannot represent 2**-1 — an error, not 0.
        with self.assertRaises(NewrError):
            pow_int(2, -1)

    def test_pow_huge_exponent_does_not_hang(self):
        # Must bound/early-abort BEFORE computing: 10**10**9 would
        # allocate gigabytes if computed blindly.
        with self.assertRaises(NewrOverflowError):
            pow_int(10, 10 ** 9)


class TestFileOps(unittest.TestCase):
    def test_read_write_append(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "x.txt")
            file_write(path, "hello")
            self.assertEqual(file_read(path), "hello")
            file_append(path, " world")
            self.assertEqual(file_read(path), "hello world")
            self.assertTrue(file_exists(path))
            self.assertFalse(file_exists(os.path.join(d, "nope.txt")))

    def test_read_missing_raises(self):
        with self.assertRaises(FileNotFoundError):
            file_read("/nonexistent-dir-xyz/missing.txt")

    def test_write_returns_none(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "x.txt")
            self.assertIsNone(file_write(path, "a"))
            self.assertIsNone(file_append(path, "b"))


class TestListIndex(unittest.TestCase):
    def test_plain(self):
        self.assertEqual(list_index([10, 20, 30], 0), 10)
        self.assertEqual(list_index([10, 20, 30], 2), 30)

    def test_negative_is_out_of_range(self):
        # SPEC §9.2: out-of-range indexing is an error. A negative
        # index is out of range (valid indices are 0..len-1) — Python
        # would silently wrap xs[-1] to the last element, which is
        # exactly the silent behavior §1.1 forbids.
        with self.assertRaises(IndexError):
            list_index([10, 20, 30], -1)

    def test_out_of_range(self):
        with self.assertRaises(IndexError):
            list_index([10, 20, 30], 3)


class TestPowFloat(unittest.TestCase):
    def test_plain(self):
        self.assertEqual(pow_float(2.0, 3.0), 8.0)
        self.assertEqual(pow_float(2.0, 0.5), 2.0 ** 0.5)
        self.assertEqual(pow_float(0.0, 0.0), 1.0)
        self.assertEqual(pow_float(5.0, 0.0), 1.0)

    def test_negative_base_fractional_exponent_is_nan(self):
        # Python's float ** returns a complex here; newr has no
        # complex type, so the IEEE answer is NaN.
        self.assertTrue(math.isnan(pow_float(-2.0, 0.5)))

    def test_overflow_is_infinity(self):
        # Python raises OverflowError; IEEE float overflow is inf.
        self.assertEqual(pow_float(1e308, 2.0), float("inf"))
        self.assertEqual(pow_float(-1e308, 3.0), float("-inf"))
        self.assertEqual(pow_float(-1e308, 2.0), float("inf"))


class TestFmod(unittest.TestCase):
    def test_float_modulo_dividend_sign(self):
        self.assertEqual(fmod(-7.0, 2.0), -1.0)  # Python % gives 1.0
        self.assertEqual(fmod(7.0, -2.0), 1.0)
        self.assertEqual(fmod(7.5, 2.0), 1.5)

    def test_float_modulo_by_zero(self):
        with self.assertRaises(NewrDivisionByZeroError):
            fmod(1.0, 0.0)


class TestRoundHalfAway(unittest.TestCase):
    def test_ties_go_away_from_zero(self):
        self.assertEqual(round_half_away(2.5), 3)
        self.assertEqual(round_half_away(3.5), 4)
        self.assertEqual(round_half_away(-2.5), -3)
        self.assertEqual(round_half_away(0.5), 1)
        self.assertEqual(round_half_away(-0.5), -1)

    def test_plain_values(self):
        self.assertEqual(round_half_away(2.4), 2)
        self.assertEqual(round_half_away(2.6), 3)
        self.assertEqual(round_half_away(-2.4), -2)
        self.assertEqual(round_half_away(5), 5)
        self.assertIsInstance(round_half_away(5), int)

    def test_rounds_the_exact_float_value(self):
        # 2.675 is stored slightly BELOW 2.675, so rounding the
        # exact float value gives 2.67 — the representation fact
        # every target shares (research §3.4). This is deliberate:
        # rounding the shortest repr instead would break identity
        # for large integral floats (see test_round_overflow).
        self.assertEqual(round_half_away(2.675, 2), 2.67)
        self.assertEqual(round_half_away(1.005, 2), 1.0)

    def test_ndigits_returns_float(self):
        self.assertEqual(round_half_away(3.14159, 2), 3.14)
        self.assertIsInstance(round_half_away(3.14159, 2), float)
        self.assertEqual(round_half_away(5, 2), 5.0)
        self.assertEqual(round_half_away(123.0, -1), 120.0)

    def test_round_overflow(self):
        # The largest float below 2**63 rounds to itself, in range.
        self.assertEqual(round_half_away(float(2 ** 63 - 1024)),
                         2 ** 63 - 1024)
        # float(INT64_MAX) is actually 2**63 (floats cannot represent
        # the maximum) — rounding it is an overflow, not a silent wrap.
        with self.assertRaises(NewrOverflowError):
            round_half_away(float(INT64_MAX))
        with self.assertRaises(NewrOverflowError):
            round_half_away(9.3e18)

    def test_round_non_finite(self):
        with self.assertRaises(OverflowError):
            round_half_away(math.inf)
        self.assertEqual(round_half_away(math.inf, 2), math.inf)
        self.assertTrue(math.isnan(round_half_away(math.nan, 2)))


class TestConversions(unittest.TestCase):
    def test_to_int_from_str(self):
        self.assertEqual(to_int("42"), 42)
        self.assertEqual(to_int("  -12 "), -12)
        with self.assertRaises(ValueError):
            to_int("3.5")
        with self.assertRaises(ValueError):
            to_int("abc")

    def test_to_int_range_checked(self):
        with self.assertRaises(NewrOverflowError):
            to_int("99999999999999999999")

    def test_to_int_from_float_truncates(self):
        self.assertEqual(to_int(3.9), 3)
        self.assertEqual(to_int(-3.9), -3)

    def test_to_float(self):
        self.assertEqual(to_float("3.5"), 3.5)
        self.assertEqual(to_float(5), 5.0)
        self.assertEqual(to_float("inf"), math.inf)
        with self.assertRaises(ValueError):
            to_float("abc")
        with self.assertRaises(OverflowError):
            to_float(10 ** 400)

    def test_to_bool_parses(self):
        # P7: parsing, not truthiness — bool("false") is False.
        self.assertIs(to_bool("true"), True)
        self.assertIs(to_bool("false"), False)
        with self.assertRaises(ValueError):
            to_bool("True")
        with self.assertRaises(ValueError):
            to_bool("")
        self.assertIs(to_bool(0), False)
        self.assertIs(to_bool(1), True)
        self.assertIs(to_bool(-1), True)
        self.assertIs(to_bool(0.0), False)
        self.assertIs(to_bool(True), True)


class TestToStr(unittest.TestCase):
    def test_scalars(self):
        self.assertEqual(to_str(True), "true")
        self.assertEqual(to_str(False), "false")
        self.assertEqual(to_str(None), "none")
        self.assertEqual(to_str(5), "5")
        self.assertEqual(to_str(-7), "-7")
        self.assertEqual(to_str(1.5), "1.5")
        self.assertEqual(to_str(1.0), "1.0")
        self.assertEqual(to_str("hello"), "hello")

    def test_collections(self):
        self.assertEqual(to_str([1, True, "a"]), "[1, true, a]")
        self.assertEqual(to_str([]), "[]")
        self.assertEqual(to_str({"k": 1, "j": True}), "{k: 1, j: true}")
        self.assertEqual(to_str({}), "{}")
        self.assertEqual(to_str([[1, 2], [3]]), "[[1, 2], [3]]")

    def test_set_contains_both(self):
        # Set order is unspecified (P3) — assert membership, not order.
        rendered = to_str({1, 2})
        self.assertTrue(rendered.startswith("{") and rendered.endswith("}"))
        self.assertIn("1", rendered)
        self.assertIn("2", rendered)

    def test_error_and_result(self):
        self.assertEqual(to_str(NewrError("boom")), "boom")
        self.assertEqual(to_str(ok(5)), "ok(5)")
        self.assertEqual(to_str(ok(True)), "ok(true)")
        self.assertEqual(to_str(err(NewrError("boom"))), "err(boom)")

    def test_struct_like_values(self):
        # Structs/enums render as name + named fields (P8). The
        # runtime cannot know a user struct's type, so objects with
        # a _newr_repr hook use it; anything else falls back to str().
        class Point:
            def __init__(self, x, y):
                self.x, self.y = x, y

            def _newr_repr(self):
                return f"Point(x: {to_str(self.x)}, y: {to_str(self.y)})"

        self.assertEqual(to_str(Point(1, 2)), "Point(x: 1, y: 2)")

        class Plain:
            def __str__(self):
                return "plain-obj"

        self.assertEqual(to_str(Plain()), "plain-obj")


class TestResult(unittest.TestCase):
    def test_ok(self):
        r = ok(5)
        self.assertTrue(r.is_ok())
        self.assertFalse(r.is_err())
        self.assertEqual(r.unwrap(), 5)
        self.assertEqual(r.unwrap_or(9), 5)
        self.assertEqual(r, ok(5))
        self.assertNotEqual(r, ok(6))

    def test_ok_none_is_still_ok(self):
        # The sentinel distinguishes ok(None) from err.
        r = ok(None)
        self.assertTrue(r.is_ok())
        self.assertIsNone(r.unwrap())

    def test_err(self):
        e = NewrError("boom")
        r = err(e)
        self.assertFalse(r.is_ok())
        self.assertTrue(r.is_err())
        self.assertEqual(r.unwrap_or(9), 9)
        with self.assertRaises(NewrError):
            r.unwrap()

    def test_try_result(self):
        self.assertEqual(try_result(lambda: 5), ok(5))
        r = try_result(lambda: 1 / 0)
        self.assertTrue(r.is_err())
        # as_error coerced the ZeroDivisionError into a NewrError
        # whose .code names the Python type — unwrap raises that.
        with self.assertRaises(NewrError) as ctx:
            r.unwrap()
        self.assertEqual(ctx.exception.code, "ZeroDivisionError")
        # A NewrError passes through unchanged.
        e = NewrError("x")

        def _raise():
            raise e

        r2 = try_result(_raise)
        self.assertTrue(r2.is_err())
        with self.assertRaises(NewrError):
            r2.unwrap()


class TestOptionalHelpers(unittest.TestCase):
    def test_opt_chain(self):
        class Obj:
            x = 3

        self.assertEqual(opt_chain(Obj(), lambda v: v.x), 3)
        self.assertIsNone(opt_chain(None, lambda v: v.x))

    def test_coalesce_is_lazy(self):
        self.assertEqual(coalesce(None, lambda: 7), 7)
        self.assertEqual(coalesce(5, lambda: 7), 5)
        # The default is not evaluated when the value is present.
        self.assertEqual(coalesce(5, lambda: 1 / 0), 5)

    def test_list_get_no_wrap(self):
        # P9: negative index is out-of-range -> default, never wraps.
        xs = [1, 2, 3]
        self.assertEqual(list_get(xs, 1, 9), 2)
        self.assertEqual(list_get(xs, 5, 9), 9)
        self.assertEqual(list_get(xs, -1, 9), 9)
        self.assertEqual(list_get([], 0, None), None)

    def test_unwrap_opt(self):
        self.assertEqual(unwrap_opt(5), 5)
        with self.assertRaises(NewrError):
            unwrap_opt(None)


class TestAsError(unittest.TestCase):
    def test_passthrough(self):
        e = NewrError("x", code="E1")
        self.assertIs(as_error(e), e)

    def test_wraps_python_exceptions(self):
        wrapped = as_error(FileNotFoundError("no such file"))
        self.assertIsInstance(wrapped, NewrError)
        self.assertEqual(wrapped.code, "FileNotFoundError")
        self.assertIn("no such file", wrapped.message)

    def test_str_becomes_error(self):
        wrapped = as_error("plain message")
        self.assertIsInstance(wrapped, NewrError)
        self.assertEqual(wrapped.message, "plain message")


class TestDb(unittest.TestCase):
    """db helpers (SPEC.md section 12.5, M8a): open/exec/query,
    transactions, last_id, close. Mirrored in tests/js/rt_tests.js."""

    def test_open_exec_query_close(self):
        h = db_open(":memory:")
        self.assertEqual(db_exec(h, "CREATE TABLE t (id INTEGER PRIMARY "
                                 "KEY AUTOINCREMENT, title TEXT NOT "
                                 "NULL, done INTEGER NOT NULL)", []), 0)
        self.assertEqual(db_exec(h, "INSERT INTO t (title, done) "
                                 "VALUES (?, ?)",
                                 ["beli susu", False]), 1)
        self.assertEqual(db_last_id(h), 1)
        rows = db_query(h, "SELECT id, title, done FROM t", [],
                        ["id", "title", "done"],
                        ["int", "str", "bool"])
        self.assertEqual(rows, [[1, "beli susu", False]])
        db_close(h)

    def test_transaction_commit_and_rollback(self):
        h = db_open(":memory:")
        db_exec(h, "CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)",
                [])
        db_exec(h, "INSERT INTO t (v) VALUES (?)", ["keep"])
        db_begin(h)
        db_exec(h, "INSERT INTO t (v) VALUES (?)", ["drop"])
        db_rollback(h)
        rows = db_query(h, "SELECT v FROM t", [], ["v"], ["str"])
        self.assertEqual(rows, [["keep"]])
        db_begin(h)
        db_exec(h, "INSERT INTO t (v) VALUES (?)", ["kept"])
        db_commit(h)
        rows = db_query(h, "SELECT v FROM t ORDER BY id", [],
                        ["v"], ["str"])
        self.assertEqual(rows, [["keep"], ["kept"]])
        db_close(h)

    def test_closed_handle_throws(self):
        h = db_open(":memory:")
        db_close(h)
        with self.assertRaises(NewrError):
            db_exec(h, "SELECT 1", [])
        with self.assertRaises(NewrError):
            db_close(h)

    def test_missing_column_and_null(self):
        h = db_open(":memory:")
        db_exec(h, "CREATE TABLE t (id INTEGER, note TEXT)", [])
        db_exec(h, "INSERT INTO t (id) VALUES (?)", [7])
        with self.assertRaises(NewrError):
            db_query(h, "SELECT id FROM t", [], ["id", "nope"],
                     ["int", "str"])
        with self.assertRaises(NewrError):
            db_query(h, "SELECT id, note FROM t", [], ["id", "note"],
                     ["int", "str"])
        rows = db_query(h, "SELECT id, note FROM t", [],
                        ["id", "note"], ["int", "str?"])
        self.assertEqual(rows, [[7, None]])
        db_close(h)

    def test_file_database(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "t.db")
            h = db_open(path)
            db_exec(h, "CREATE TABLE t (v INTEGER)", [])
            db_exec(h, "INSERT INTO t (v) VALUES (?)", [42])
            db_close(h)
            h = db_open(path)
            rows = db_query(h, "SELECT v FROM t", [], ["v"], ["int"])
            self.assertEqual(rows, [[42]])
            db_close(h)

    def test_handle_usable_from_another_thread(self):
        # M9 L8: a handle opened on the main thread must work from
        # a worker thread (the http server runs handlers on worker
        # threads). A file database: the worker sees the main
        # thread's rows through its own per-thread connection.
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "t.db")
            h = db_open(path)
            db_exec(h, "CREATE TABLE t (v INTEGER)", [])
            db_exec(h, "INSERT INTO t (v) VALUES (?)", [42])
            seen: list = []
            errors: list = []

            def worker():
                try:
                    seen.append(db_query(h, "SELECT v FROM t", [],
                                         ["v"], ["int"]))
                except Exception as exc:  # noqa: BLE001 - re-raised
                    errors.append(exc)

            t = threading.Thread(target=worker)
            t.start()
            t.join()
            self.assertEqual(errors, [])
            self.assertEqual(seen, [[[42]]])
            db_close(h)

    def test_concurrent_writes_from_many_threads(self):
        # M9 L8: many threads writing through one handle at once
        # serialize through SQLite's locking (busy_timeout 5000);
        # every insert lands.
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "t.db")
            h = db_open(path)
            db_exec(h, "CREATE TABLE t (v INTEGER)", [])
            errors: list = []

            def worker(i: int):
                try:
                    db_exec(h, "INSERT INTO t (v) VALUES (?)", [i])
                except Exception as exc:  # noqa: BLE001 - re-raised
                    errors.append(exc)

            threads = [threading.Thread(target=worker, args=(i,))
                       for i in range(8)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            self.assertEqual(errors, [])
            rows = db_query(h, "SELECT COUNT(*) AS n FROM t", [],
                            ["n"], ["int"])
            self.assertEqual(rows, [[8]])
            db_close(h)


if __name__ == "__main__":
    unittest.main()
