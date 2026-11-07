// Rust-side unit tests for compiler/newr_rt.rs (M5).
//
// Mirrors tests/java/RtChecks.java case for case (same rule, both
// targets) — same checks, adapted to Rust types (i64 instead of
// long, Option instead of null, Result instead of NewrRt.Result).
// Run with `rustc newr_rt.rs RtChecks.rs -o RtChecks && ./RtChecks`
// from a temp dir holding copies of both files, or via
// `python -m unittest tests.test_newr_rt_rs`.
//
// Every failure prints the assertion and exits non-zero. Keep the
// battery aligned with the Java file: when a rule is added here,
// add it there too (or record why not).

#[path = "newr_rt.rs"]
mod newr_rt;

use newr_rt::*;
use std::panic::{AssertUnwindSafe, catch_unwind};
use std::sync::Mutex;

static mut PASSED: i32 = 0;

fn check(name: &str, f: impl FnOnce()) {
    f();
    unsafe {
        PASSED += 1;
    }
    println!("ok {name}");
}

fn eq<T: PartialEq + std::fmt::Debug>(got: T, want: T, what: &str) {
    assert_eq!(got, want, "{what}");
}

fn ok(cond: bool, what: &str) {
    assert!(cond, "expected true: {what}");
}

/// Expect a panic whose NewrError code equals `code` (None allowed).
fn throws_code(f: impl FnOnce(), code: Option<&str>) -> NewrError {
    match catch_unwind(AssertUnwindSafe(f)) {
        Ok(_) => panic!("expected error with code {code:?}, nothing thrown"),
        Err(payload) => {
            let e = as_error(payload);
            assert_eq!(e.code(), code, "error code");
            e
        }
    }
}

/// Expect a panic carrying a NewrError with no code.
fn throws_plain(f: impl FnOnce()) -> NewrError {
    throws_code(f, None)
}

fn main() {
    // ---------------------------------------------------------------- constants
    check("int64 bounds", || {
        eq(i64::MIN, i64::MIN, "INT64_MIN");
        eq(i64::MAX, i64::MAX, "INT64_MAX");
    });

    // ---------------------------------------------------------------- checked arithmetic
    check("add/sub/mul ok", || {
        eq(add(2, 3), 5, "add");
        eq(sub(10, 4), 6, "sub");
        eq(mul(3, 4), 12, "mul");
    });
    check("add overflow", || {
        let e = throws_plain(|| {
            add(i64::MAX, 1);
        });
        ok(
            e.message().contains("9223372036854775808"),
            "add overflow message carries the exact sum",
        );
    });
    check("sub underflow", || {
        throws_plain(|| {
            sub(i64::MIN, 1);
        });
    });
    check("mul overflow", || {
        throws_plain(|| {
            mul(i64::MAX, 2);
        });
    });
    check("neg", || {
        eq(neg(5), -5, "neg 5");
        eq(neg(-5), 5, "neg -5");
        throws_plain(|| {
            neg(i64::MIN);
        });
    });
    check("pow_int", || {
        eq(pow_int(2, 10), 1024, "2 ** 10");
        eq(pow_int(0, 0), 1, "0 ** 0");
        eq(pow_int(0, 5), 0, "0 ** 5");
        eq(pow_int(-1, 3), -1, "-1 ** 3");
        eq(pow_int(-1, 4), 1, "-1 ** 4");
        throws_plain(|| {
            pow_int(2, -1);
        });
        throws_plain(|| {
            pow_int(10, 19);
        });
    });
    check("tdiv/tmod", || {
        eq(tdiv(7, 2), 3, "7 / 2");
        eq(tdiv(-7, 2), -3, "-7 / 2");
        eq(tmod(-7, 2), -1, "-7 % 2");
        eq(tmod(7, -3), 1, "7 % -3");
        throws_plain(|| {
            tdiv(1, 0);
        });
        throws_plain(|| {
            tmod(1, 0);
        });
        throws_plain(|| {
            tdiv(i64::MIN, -1);
        });
        eq(tmod(i64::MIN, -1), 0, "MIN % -1 is 0");
    });
    check("pow_float", || {
        eq(pow_float(2.0, 10.0), 1024.0, "2.0 ** 10");
        ok(pow_float(-2.0, 0.5).is_nan(), "negative base frac exp is NaN");
        ok(pow_float(10.0, 400.0).is_infinite(), "overflow is inf");
        throws_code(
            || {
                pow_float(0.0, -1.0);
            },
            Some("ZeroDivisionError"),
        );
    });
    check("fmod/fdiv", || {
        eq(fmod(7.5, 2.0), 1.5, "7.5 % 2.0");
        eq(fmod(-7.5, 2.0), -1.5, "-7.5 % 2.0");
        throws_plain(|| {
            fmod(1.0, 0.0);
        });
        eq(fdiv(7.0, 2.0), 3.5, "7.0 / 2.0");
        throws_code(
            || {
                fdiv(1.0, 0.0);
            },
            Some("ZeroDivisionError"),
        );
        throws_code(
            || {
                fdiv(0.0, 0.0);
            },
            Some("ZeroDivisionError"),
        );
    });

    // ---------------------------------------------------------------- rounding (P2 / J3)
    check("round half away int", || {
        eq(round_half_away(2.5), 3, "round(2.5)");
        eq(round_half_away(-2.5), -3, "round(-2.5)");
        eq(round_half_away(2.4), 2, "round(2.4)");
        eq(round_half_away(-2.4), -2, "round(-2.4)");
        eq(round_half_away(0.5), 1, "round(0.5)");
        eq(round_half_away(-0.5), -1, "round(-0.5)");
        throws_code(|| {
            round_half_away(f64::INFINITY);
        }, Some("OverflowError"));
        throws_code(|| {
            round_half_away(f64::NAN);
        }, Some("OverflowError"));
    });
    check("round half away exact float", || {
        // The float 2.675 is stored slightly BELOW 2.675.
        eq(round_half_away_n(2.675, 2), 2.67, "round(2.675, 2) is 2.67");
        eq(round_half_away_n(2.685, 2), 2.69, "round(2.685, 2) is 2.69");
        eq(round_half_away_n(1.005, 2), 1.0, "round(1.005, 2) is 1.0");
        eq(round_half_away_n(-2.5, 0), -3.0, "round(-2.5, 0)");
        eq(round_half_away_n(123.456, -1), 120.0, "round(123.456, -1)");
        eq(round_half_away_n(15.0, -1), 20.0, "round(15, -1)");
        eq(round_half_away_n(-0.001, 2), -0.0, "round(-0.001, 2) is -0.0");
        ok(round_half_away_n(f64::INFINITY, 2).is_infinite(), "round(inf, n)");
        ok(round_half_away_n(f64::NAN, 2).is_nan(), "round(nan, n)");
        eq(round_half_away_int_n(5, -1), 10.0, "round(5, -1) is 10");
        eq(round_half_away_int(7), 7, "round int passthrough");
        eq(round_half_away_bool(true), 1, "round(true)");
    });

    // ---------------------------------------------------------------- float rendering (J4)
    check("float_str", || {
        eq(float_str(0.0), "0.0".to_string(), "0.0");
        eq(float_str(-0.0), "-0.0".to_string(), "-0.0");
        eq(float_str(1.5), "1.5".to_string(), "1.5");
        eq(float_str(10000000.0), "10000000.0".to_string(), "1e7 fixed");
        eq(float_str(1e16), "1e+16".to_string(), "1e16 scientific");
        eq(float_str(1e-5), "1e-05".to_string(), "1e-5 scientific");
        eq(float_str(0.0001), "0.0001".to_string(), "0.0001 fixed");
        eq(float_str(5e-324), "5e-324".to_string(), "smallest subnormal");
        eq(float_str(f64::NAN), "nan".to_string(), "nan");
        eq(float_str(f64::INFINITY), "inf".to_string(), "inf");
        eq(float_str(f64::NEG_INFINITY), "-inf".to_string(), "-inf");
        eq(float_str(2.675), "2.675".to_string(), "2.675 repr");
        eq(float_str(0.1), "0.1".to_string(), "0.1");
        eq(float_str(123456789.123456789), "123456789.12345679".to_string(), "long digits");
    });

    // ---------------------------------------------------------------- conversions
    check("to_int", || {
        eq(to_int_from_float(3.9), 3, "int(3.9) truncates");
        eq(to_int_from_float(-3.9), -3, "int(-3.9) truncates");
        eq(to_int_from_str("42"), 42, "int(\"42\")");
        eq(to_int_from_str("  -7  "), -7, "int(\"  -7  \") trims");
        eq(to_int_from_bool(true), 1, "int(true)");
        throws_code(|| {
            to_int_from_str("3.5");
        }, Some("ValueError"));
        throws_code(|| {
            to_int_from_str("abc");
        }, Some("ValueError"));
        throws_code(|| {
            to_int_from_float(f64::INFINITY);
        }, Some("OverflowError"));
        throws_code(|| {
            to_int_from_float(1e30);
        }, None);
    });
    check("to_float", || {
        eq(to_float_from_str("1.5"), 1.5, "float(\"1.5\")");
        eq(to_float_from_str("inf"), f64::INFINITY, "float(\"inf\")");
        eq(to_float_from_str("-infinity"), f64::NEG_INFINITY, "float(\"-infinity\")");
        ok(to_float_from_str("nan").is_nan(), "float(\"nan\")");
        eq(to_float_from_str("1_000"), 1000.0, "float underscores");
        throws_code(|| {
            to_float_from_str("abc");
        }, Some("ValueError"));
    });
    check("to_bool", || {
        eq(to_bool_from_str("true"), true, "bool(\"true\")");
        eq(to_bool_from_str("false"), false, "bool(\"false\")");
        eq(to_bool_from_int(0), false, "bool(0)");
        eq(to_bool_from_int(3), true, "bool(3)");
        eq(to_bool_from_float(0.0), false, "bool(0.0)");
        throws_code(|| {
            to_bool_from_str("yes");
        }, Some("ValueError"));
    });

    // ---------------------------------------------------------------- to_str (P8)
    check("to_str", || {
        eq(to_str(&true), "true".to_string(), "str(true)");
        eq(to_str(&false), "false".to_string(), "str(false)");
        eq(to_str(&42i64), "42".to_string(), "str(42)");
        eq(to_str(&1.5f64), "1.5".to_string(), "str(1.5)");
        eq(to_str(&"a".to_string()), "a".to_string(), "str str");
        eq(to_str(&None::<i64>), "none".to_string(), "str(none)");
        eq(to_str(&Some(3i64)), "3".to_string(), "str(Some)");
        eq(
            to_str(&vec![1i64, 2, 3]),
            "[1, 2, 3]".to_string(),
            "str list",
        );
        eq(
            to_str(&vec![true, false]),
            "[true, false]".to_string(),
            "str bool list",
        );
        eq(to_str(&Vec::<i64>::new()), "[]".to_string(), "str empty list");
        let mut d = Dict::new();
        d.insert("k".to_string(), 1i64);
        d.insert("j".to_string(), 2i64);
        eq(to_str(&d), "{k: 1, j: 2}".to_string(), "str dict insertion order");
        let s = Set::from_vec(vec!["a".to_string()]);
        eq(to_str(&s), "{a}".to_string(), "str set");
        eq(
            to_str(&Ok::<i64, NewrError>(5)),
            "ok(5)".to_string(),
            "str ok",
        );
        eq(
            to_str(&Err::<i64, NewrError>(NewrError::new("boom"))),
            "err(boom)".to_string(),
            "str err",
        );
        eq(to_str(&NewrError::new("boom")), "boom".to_string(), "str error");
    });

    // ---------------------------------------------------------------- Result / try
    check("try_result", || {
        let r1 = try_result(|| 6i64 / 2);
        eq(r1, Ok(3), "try ok");
        let r2: Result<i64, NewrError> = try_result(|| tdiv(6, 0));
        ok(r2.is_err(), "try err");
        eq(
            result_unwrap(Ok::<i64, NewrError>(5)),
            5,
            "result_unwrap ok",
        );
        let e = throws_plain(|| {
            result_unwrap(Err::<i64, NewrError>(NewrError::new("x")));
        });
        eq(e.message(), "x", "result_unwrap rethrows the error");
        eq(
            result_unwrap_or(Err::<i64, NewrError>(NewrError::new("x")), 9),
            9,
            "unwrap_or",
        );
    });
    check("error identity", || {
        let a = NewrError::new("same");
        let b = NewrError::new("same");
        ok(a != b, "distinct errors are not equal");
        ok(a == a.clone(), "a clone is the same error");
        let c = coded_error("ValueError", "v");
        eq(c.code(), Some("ValueError"), "coded code");
        eq(c.message(), "v", "coded message");
        eq(NewrError::new("x").code(), None, "plain error has no code");
    });
    check("as_error", || {
        let e = as_error(Box::new(NewrError::coded("E", "m")));
        eq(e.code(), Some("E"), "as_error passes NewrError through");
        let e2 = as_error(Box::new("plain string".to_string()));
        eq(e2.message(), "plain string", "as_error string payload");
    });

    // ---------------------------------------------------------------- lists
    check("list helpers", || {
        let xs = vec![1i64, 2, 3];
        eq(list_index(&xs, 0), 1, "list_index 0");
        eq(list_index(&xs, 2), 3, "list_index 2");
        throws_code(|| {
            list_index(&xs, 3);
        }, Some("IndexError"));
        throws_code(|| {
            list_index(&xs, -1);
        }, Some("IndexError"));
        eq(list_get(&xs, 9, -1), -1, "list_get default");
        eq(list_get(&xs, -1, -1), -1, "list_get negative default");
        eq(list_get(&xs, 1, -1), 2, "list_get hit");
        ok(list_contains(&xs, &2), "list contains");
        ok(!list_contains(&xs, &9), "list not contains");
        ok(!list_contains(&vec![f64::NAN], &f64::NAN), "NaN never contained");
        eq(range(3), vec![0, 1, 2], "range(3)");
        eq(range_from(2, 5), vec![2, 3, 4], "range(2, 5)");
        eq(range_step(10, 0, -3), vec![10, 7, 4, 1], "range step -3");
        throws_code(|| {
            range_step(0, 5, 0);
        }, Some("ValueError"));
    });

    // ---------------------------------------------------------------- dict / set
    check("dict", || {
        let d = dict_of(vec![
            ("a".to_string(), 1i64),
            ("b".to_string(), 2i64),
        ]);
        eq(d.keys(), vec!["a".to_string(), "b".to_string()], "dict keys order");
        eq(d.get("a"), Some(1), "dict get hit");
        eq(d.get("z"), None, "dict get miss");
        ok(d.contains_key("b"), "dict contains");
        ok(!d.contains_key("z"), "dict not contains");
        eq(d.index("b"), 2, "dict index");
        throws_code(|| {
            d.index("z");
        }, Some("KeyError"));
        let d2 = dict_of(vec![
            ("b".to_string(), 2i64),
            ("a".to_string(), 1i64),
        ]);
        ok(d == d2, "dict eq is order-insensitive");
        let mut d3 = Dict::new();
        d3.insert("a".to_string(), None::<i64>);
        ok(d3.get("a") == Some(None), "dict null value allowed");
    });
    check("set", || {
        let mut s = Set::from_vec(vec![1i64, 2, 2, 3]);
        eq(s.len(), 3, "set dedups");
        ok(s.contains(&2), "set contains");
        ok(!s.contains(&9), "set not contains");
        ok(s.add(4), "set add new");
        ok(!s.add(4), "set add dup is false");
        ok(s.remove(&1), "set remove hit");
        ok(!s.remove(&99), "set remove miss is false");
        set_remove(&mut s, &2);
        ok(!s.contains(&2), "set_remove removes");
        throws_code(|| {
            set_remove(&mut s, &99);
        }, Some("KeyError"));
        let s2 = Set::from_vec(vec![3i64, 4]);
        ok(s == s2, "set eq is order-insensitive");
    });

    // ---------------------------------------------------------------- strings
    check("chars/split/str_index", || {
        eq(
            chars("héllo"),
            vec![
                "h".to_string(),
                "é".to_string(),
                "l".to_string(),
                "l".to_string(),
                "o".to_string(),
            ],
            "chars code points",
        );
        eq(str_len("héllo"), 5, "str len is code points");
        eq(
            split_str("a,b,", ","),
            vec!["a".to_string(), "b".to_string(), "".to_string()],
            "split keeps trailing empty",
        );
        eq(split_str("abc", "x"), vec!["abc".to_string()], "split no match");
        eq(str_index("héllo", 1), "é".to_string(), "str index code point");
        eq(str_index("héllo", -1), "o".to_string(), "str index negative wraps");
        throws_code(|| {
            str_index("ab", 2);
        }, Some("IndexError"));
    });
    check("abs_long", || {
        eq(abs_long(-5), 5, "abs -5");
        eq(abs_long(5), 5, "abs 5");
        throws_plain(|| {
            abs_long(i64::MIN);
        });
    });

    // ---------------------------------------------------------------- random
    check("random bounds", || {
        for _ in 0..50 {
            let v = random_int(1, 10);
            ok((1..=10).contains(&v), "random_int in range");
        }
        eq(random_int(7, 7), 7, "random_int single value");
        eq(random_choice(&vec![42i64]), 42, "random_choice single");
        throws_code(|| {
            random_choice(&Vec::<i64>::new());
        }, Some("IndexError"));
        let mut xs = vec![1i64, 2, 3, 4, 5];
        random_shuffle(&mut xs);
        xs.sort();
        eq(xs, vec![1, 2, 3, 4, 5], "shuffle keeps elements");
    });

    // ---------------------------------------------------------------- time
    check("time_format", || {
        eq(time_format(0, "%Y-%m-%d"), "1970-01-01".to_string(), "epoch date");
        eq(time_format(0, "%H:%M"), "00:00".to_string(), "epoch time");
        eq(
            time_format(0, "%Y-%m-%d %H:%M"),
            "1970-01-01 00:00".to_string(),
            "epoch datetime",
        );
        eq(time_format(0, "%d/%m/%Y"), "01/01/1970".to_string(), "epoch dmy");
        eq(time_format(0, "%H:%M:%S"), "00:00:00".to_string(), "epoch hms");
        eq(
            time_format(1_700_000_000, "%Y-%m-%d %H:%M"),
            "2023-11-14 22:13".to_string(),
            "2023-11-14 UTC",
        );
        eq(
            time_format(-1, "%H:%M:%S"),
            "23:59:59".to_string(),
            "before epoch",
        );
        eq(time_now() > 1_700_000_000, true, "time_now is current");
        throws_plain(|| {
            time_format(0, "%Y");
        });
    });

    // ---------------------------------------------------------------- file / env
    check("file io", || {
        let dir = std::env::temp_dir().join("newr_rt_checks");
        let _ = std::fs::create_dir_all(&dir);
        let path = dir.join("t.txt");
        let path_s = path.to_string_lossy().to_string();
        file_write(&path_s, "hello");
        ok(file_exists(&path_s), "file exists after write");
        eq(file_read(&path_s), "hello".to_string(), "file read");
        file_append(&path_s, " world");
        eq(file_read(&path_s), "hello world".to_string(), "file append");
        ok(!file_exists(&dir.join("nope.txt").to_string_lossy()), "missing absent");
        let e = throws_code(
            || {
                file_read(&dir.join("nope.txt").to_string_lossy());
            },
            Some("NotFound"),
        );
        ok(e.message().contains("nope.txt") || !e.message().is_empty(), "read error message");
        let _ = std::fs::remove_dir_all(&dir);
    });
    check("env", || {
        eq(env_get("NEWR_RT_CHECK_ABSENT_XYZ"), None, "env_get absent");
        eq(
            env_get_or("NEWR_RT_CHECK_ABSENT_XYZ", "dflt"),
            "dflt".to_string(),
            "env_get default",
        );
        ok(env_get("PATH").is_some(), "env_get PATH present");
        ok(!env_args().is_empty(), "env_args non-empty");
    });

    // ---------------------------------------------------------------- hashing
    check("newr hash", || {
        eq(
            newr_hash_u64(&0.0f64),
            newr_hash_u64(&-0.0f64),
            "hash -0.0 == 0.0",
        );
        eq(newr_hash_u64(&"x".to_string()), newr_hash_u64(&"x".to_string()), "hash stable");
        eq(
            newr_hash_u64(&vec![1i64]),
            newr_hash_u64(&vec![1i64]),
            "hash list by elements",
        );
        eq(
            struct_hash_u64(&[newr_hash_u64(&1i64), newr_hash_u64(&"a".to_string())]),
            struct_hash_u64(&[newr_hash_u64(&1i64), newr_hash_u64(&"a".to_string())]),
            "struct_hash stable",
        );
        eq(
            struct_hash_u64(&[newr_hash_u64(&-0.0f64)]),
            struct_hash_u64(&[newr_hash_u64(&0.0f64)]),
            "struct_hash -0.0 == 0.0",
        );
    });
    check("unwrap_opt", || {
        eq(unwrap_opt(Some(3i64)), 3, "unwrap_opt some");
        throws_plain(|| {
            unwrap_opt(None::<i64>);
        });
    });
    check("globals", || {
        static CELL: Mutex<Option<i64>> = Mutex::new(None);
        gset(&CELL, 41);
        eq(gget(&CELL), 41, "gget after gset");
        gset(&CELL, 42);
        eq(gget(&CELL), 42, "gset overwrites");
    });

    unsafe {
        println!("\nRtChecks.rs: {PASSED} checks passed");
    }
}
