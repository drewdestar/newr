// The newr Rust runtime (M5).
//
// This file is the runtime half of every emitted Rust program that
// needs it: checked 64-bit integer arithmetic (i64 + checked_*),
// truncating division, half-away rounding on the EXACT float value,
// the newr conversion/rendering rules, `Result`, and the `?.` / `??`
// helpers. It is std-only and dependency-free, so a plain program
// compiles with plain `rustc <main>.rs` and no Cargo.toml.
//
// It lives in the compiler package as `compiler/newr_rt.rs` and
// `newr build --target rs` copies it - byte-identical - next to
// an emitted `.rs` file when (and only when) that program uses a
// helper from it. Do not edit a copied file by hand: edit this file
// in the newr repo and rebuild.
//
// Semantics are pinned in BUILD_REPORT.md §21 (K2-K6, mirroring
// the M2 pins P1-P12 and the M3 pins J1-J13) and the M5 pins
// R1-Rn. Every rule here is unit-tested in tests/rs/RtChecks.rs
// (run by tests/test_newr_rt_rs.py).
//
// The runtime is a single module `newr_rt` in the crate root, so
// an emitted file runs with plain `rustc <stem>.rs`. Emitted code
// calls helpers as `newr_rt::add(a, b)`; the error type is
// `newr_rt::NewrError`.
//
// Errors are Rust panics carrying a NewrError payload
// (`std::panic::panic_any`): a `try` block catches them with
// `catch_unwind` and coerces them with `as_error` (R2). A panic
// that escapes `main` exits with code 101, like an uncaught
// exception on the other targets.

#![allow(dead_code)]

use std::any::Any;
use std::collections::HashMap;
use std::collections::hash_map::DefaultHasher;
use std::hash::{BuildHasherDefault, Hash, Hasher};
use std::io::{BufRead, Write};
use std::sync::Mutex;
use std::sync::OnceLock;
use std::sync::atomic::{AtomicU64, Ordering};

// ---------------------------------------------------------------- errors

static NEXT_ERROR_ID: AtomicU64 = AtomicU64::new(1);

/// Base of every newr `error` value. Carries the error payload.
/// `code` is None unless set (a caught host error keeps its own
/// code via as_error). Two errors are equal only when they are the
/// same error (identity, by `id` - a clone keeps the id).
#[derive(Clone, Debug)]
pub struct NewrError {
    pub message: String,
    pub code: Option<String>,
    id: u64,
}

impl NewrError {
    pub fn new(message: impl Into<String>) -> Self {
        NewrError {
            message: message.into(),
            code: None,
            id: NEXT_ERROR_ID.fetch_add(1, Ordering::Relaxed),
        }
    }

    pub fn coded(code: impl Into<String>, message: impl Into<String>) -> Self {
        NewrError {
            message: message.into(),
            code: Some(code.into()),
            id: NEXT_ERROR_ID.fetch_add(1, Ordering::Relaxed),
        }
    }

    pub fn message(&self) -> &str {
        &self.message
    }

    pub fn code(&self) -> Option<&str> {
        self.code.as_deref()
    }
}

impl PartialEq for NewrError {
    fn eq(&self, other: &Self) -> bool {
        self.id == other.id
    }
}

impl Eq for NewrError {}

impl std::fmt::Display for NewrError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&self.message)
    }
}

impl std::error::Error for NewrError {}

/// An error with a host-style code (IndexError, KeyError,
/// ValueError): the Python target raises the builtin of that name,
/// so a newr `catch e:` sees the same code on every target.
pub fn coded_error(code: &str, message: &str) -> NewrError {
    NewrError::coded(code, message)
}

/// Throw a newr error (panics with the error as its payload).
pub fn throw(err: NewrError) -> ! {
    std::panic::panic_any(err)
}

/// Coerce whatever a `catch` caught into a newr `error` value.
/// A `NewrError` passes through unchanged; a string payload keeps
/// its message; anything else becomes an error with a generic
/// message. This is what makes `.message` / `.code` total inside
/// every `catch` body (SPEC §10).
pub fn as_error(payload: Box<dyn Any + Send>) -> NewrError {
    match payload.downcast::<NewrError>() {
        Ok(err) => *err,
        Err(payload) => match payload.downcast::<String>() {
            Ok(s) => NewrError::new(*s),
            Err(payload) => match payload.downcast::<&str>() {
                Ok(s) => NewrError::new(*s),
                Err(_) => {
                    NewrError::new("internal error (unreadable panic payload)")
                }
            },
        },
    }
}

// ---------------------------------------------------------------- globals

/// Read a module-level `let`/`const` (R3): the static holds
/// `Option<T>` behind a Mutex; a read before any assignment
/// panics (the checker only lets a function *reference* a later
/// let, never read it before its statement runs).
pub fn gget<T: Clone>(cell: &Mutex<Option<T>>) -> T {
    cell.lock()
        .unwrap()
        .as_ref()
        .expect("read of a top-level let before its assignment")
        .clone()
}

/// Assign a module-level `let`/`const`.
pub fn gset<T>(cell: &Mutex<Option<T>>, value: T) {
    *cell.lock().unwrap() = Some(value);
}

/// Mutate a module-level `let` in place (list.append / set.add /
/// set.remove / random.shuffle on a global collection): lock,
/// apply the closure to the stored value, drop the lock. A `gget`
/// clone would lose the mutation, so mutating calls go through
/// this helper instead.
pub fn gmut<T, F: FnOnce(&mut T)>(cell: &Mutex<Option<T>>, f: F) {
    let mut guard = cell.lock().unwrap();
    let v = guard
        .as_mut()
        .expect("mutation of a top-level let before its assignment");
    f(v);
}

// ---------------------------------------------------------------- int64

/// Return `v` if it fits in signed 64-bit, else raise. (i64 is
/// already the type, so this is the identity - kept for parity
/// with the other runtimes' call sites.)
pub fn check_int(v: i64) -> i64 {
    v
}

fn overflow_i128(v: i128) -> ! {
    throw(NewrError::new(format!(
        "integer overflow: {v} is outside int64 range"
    )))
}

pub fn add(a: i64, b: i64) -> i64 {
    match a.checked_add(b) {
        Some(v) => v,
        None => overflow_i128(a as i128 + b as i128),
    }
}

pub fn sub(a: i64, b: i64) -> i64 {
    match a.checked_sub(b) {
        Some(v) => v,
        None => overflow_i128(a as i128 - b as i128),
    }
}

pub fn mul(a: i64, b: i64) -> i64 {
    match a.checked_mul(b) {
        Some(v) => v,
        None => overflow_i128(a as i128 * b as i128),
    }
}

pub fn neg(a: i64) -> i64 {
    match a.checked_neg() {
        Some(v) => v,
        None => overflow_i128(-(a as i128)),
    }
}

pub fn pow_int(a: i64, b: i64) -> i64 {
    // newr `**` on ints: checked, bounded before computing.
    // A negative exponent is an error (an int result cannot
    // represent it). Repeated squaring with a check at every
    // multiply - `10 ** 10**9` raises instead of computing blindly.
    if b < 0 {
        throw(NewrError::new("negative exponent for int pow"));
    }
    if a == 0 {
        return if b == 0 { 1 } else { 0 };
    }
    if a == 1 {
        return 1;
    }
    if a == -1 {
        return if b % 2 == 1 { -1 } else { 1 };
    }
    let mut result: i64 = 1;
    let mut base = a;
    let mut exp = b;
    while exp > 0 {
        if exp % 2 == 1 {
            result = mul(result, base);
        }
        exp /= 2;
        if exp > 0 {
            base = mul(base, base);
        }
    }
    result
}

pub fn tdiv(a: i64, b: i64) -> i64 {
    // newr `/` on ints: truncating division (toward zero).
    // Rust `/` already truncates - the check is the two error cases.
    if b == 0 {
        throw(NewrError::new("integer division by zero"));
    }
    if a == i64::MIN && b == -1 {
        throw(NewrError::new("integer overflow: INT64_MIN / -1"));
    }
    a / b
}

pub fn tmod(a: i64, b: i64) -> i64 {
    // newr `%` on ints: remainder with the dividend's sign.
    // Rust `%` already takes the dividend's sign.
    if b == 0 {
        throw(NewrError::new("integer modulo by zero"));
    }
    if a == i64::MIN && b == -1 {
        return 0; // mathematically 0; no overflow (unlike division)
    }
    a % b
}

pub fn pow_float(a: f64, b: f64) -> f64 {
    // newr `**` on floats: IEEE semantics. Rust `powf` already
    // returns NaN for a negative base with a fractional exponent
    // (Python returns a complex there - newr has no complex type,
    // so the answer is NaN) and Infinity on overflow. The one
    // case Rust gets wrong is a zero base with a negative
    // exponent: Rust gives Infinity, Python raises ZeroDivisionError.
    if a == 0.0 && b < 0.0 {
        throw(coded_error(
            "ZeroDivisionError",
            "0.0 cannot be raised to a negative power",
        ));
    }
    a.powf(b)
}

pub fn fmod(a: f64, b: f64) -> f64 {
    // newr `%` on floats: C `fmod` semantics (dividend's sign).
    // Rust `%` already takes the dividend's sign (unlike Python's
    // `%`, which takes the divisor's). Division by zero raises
    // like the int case (Rust would give NaN).
    if b == 0.0 {
        throw(NewrError::new("float modulo by zero"));
    }
    a % b
}

pub fn fdiv(a: f64, b: f64) -> f64 {
    // newr `/` on floats: IEEE division, except division by zero
    // raises like Python (Rust would give Infinity for a non-zero
    // dividend and NaN for 0/0). Python's native `/` raises the
    // BUILTIN ZeroDivisionError, so this throws the coded error,
    // not the codeless class - a `catch e:` sees e.code ==
    // "ZeroDivisionError" on every target.
    if b == 0.0 {
        throw(coded_error("ZeroDivisionError", "float division by zero"));
    }
    a / b
}

// ---------------------------------------------------------------- exact decimal (P2 / J3)

/// A minimal big unsigned integer (little-endian base-2^32 limbs),
/// just enough for the exact-decimal work below: shift left,
/// multiply by a small int, divide by a small int, add one, and
/// render as a decimal string.
#[derive(Clone, Debug, PartialEq, Eq)]
struct BigUint {
    limbs: Vec<u32>,
}

impl BigUint {
    fn zero() -> Self {
        BigUint { limbs: Vec::new() }
    }

    fn from_u64(mut v: u64) -> Self {
        let mut limbs = Vec::new();
        while v > 0 {
            limbs.push(v as u32);
            v >>= 32;
        }
        BigUint { limbs }
    }

    fn is_zero(&self) -> bool {
        self.limbs.is_empty()
    }

    fn normalize(&mut self) {
        while let Some(&0) = self.limbs.last() {
            self.limbs.pop();
        }
    }

    fn shl(&mut self, bits: usize) {
        if self.is_zero() || bits == 0 {
            return;
        }
        let limb_shift = bits / 32;
        let bit_shift = bits % 32;
        let mut limbs = vec![0u32; limb_shift];
        limbs.extend_from_slice(&self.limbs);
        if bit_shift != 0 {
            let mut carry = 0u32;
            for limb in limbs.iter_mut().skip(limb_shift) {
                let next = *limb >> (32 - bit_shift);
                *limb = (*limb << bit_shift) | carry;
                carry = next;
            }
            if carry != 0 {
                limbs.push(carry);
            }
        }
        self.limbs = limbs;
    }

    fn mul_small(&mut self, m: u32) {
        if m == 0 || self.is_zero() {
            self.limbs.clear();
            return;
        }
        let mut carry: u64 = 0;
        for limb in self.limbs.iter_mut() {
            let v = *limb as u64 * m as u64 + carry;
            *limb = v as u32;
            carry = v >> 32;
        }
        while carry > 0 {
            self.limbs.push(carry as u32);
            carry >>= 32;
        }
    }

    /// Divide by a small int; self becomes the quotient, the
    /// remainder is returned.
    fn divmod_small(&mut self, d: u32) -> u32 {
        let mut rem: u64 = 0;
        for limb in self.limbs.iter_mut().rev() {
            let v = (rem << 32) | *limb as u64;
            *limb = (v / d as u64) as u32;
            rem = v % d as u64;
        }
        self.normalize();
        rem as u32
    }

    fn add_one(&mut self) {
        let mut carry: u64 = 1;
        for limb in self.limbs.iter_mut() {
            let v = *limb as u64 + carry;
            *limb = v as u32;
            carry = v >> 32;
            if carry == 0 {
                break;
            }
        }
        if carry != 0 {
            self.limbs.push(carry as u32);
        }
    }

    fn to_decimal_string(&self) -> String {
        if self.is_zero() {
            return "0".to_string();
        }
        let mut tmp = self.clone();
        let mut chunks: Vec<u32> = Vec::new();
        while !tmp.is_zero() {
            chunks.push(tmp.divmod_small(1_000_000_000));
        }
        let mut out = String::new();
        let mut first = true;
        for chunk in chunks.iter().rev() {
            if first {
                out.push_str(&chunk.to_string());
                first = false;
            } else {
                out.push_str(&format!("{chunk:09}"));
            }
        }
        out
    }
}

/// Exact decimal expansion of a finite double: value =
/// sign * digits / 10**scale, digits a non-negative BigUint.
/// Mirrors the other runtimes' exactDecimal (same bit decomposition).
fn exact_decimal(x: f64) -> (i32, BigUint, u32) {
    let bits = x.to_bits();
    let sign: i32 = if bits >> 63 == 1 { -1 } else { 1 };
    let exp_bits = ((bits >> 52) & 0x7FF) as i64;
    let frac = bits & ((1u64 << 52) - 1);
    let (m, e): (u64, i64) = if exp_bits == 0 {
        // Subnormal (or zero): no implicit leading 1.
        (frac, -1074)
    } else {
        (frac | (1u64 << 52), exp_bits - 1075)
    };
    if m == 0 {
        return (sign, BigUint::zero(), 0);
    }
    if e >= 0 {
        let mut digits = BigUint::from_u64(m);
        digits.shl(e as usize);
        (sign, digits, 0)
    } else {
        let mut digits = BigUint::from_u64(m);
        for _ in 0..(-e) {
            digits.mul_small(5);
        }
        (sign, digits, (-e) as u32)
    }
}

/// Round the exact decimal (digits / 10**scale, digits >= 0) to
/// `n` places after the point, half away from zero. Returns
/// (rounded, places) with value = rounded / 10**places.
/// The dropped digits decide: the most significant dropped digit
/// is >= 5 exactly when the remainder is >= half the divisor.
fn round_decimal(mut digits: BigUint, scale: u32, n: i64) -> (BigUint, i64) {
    let scale_i = scale as i64;
    if scale_i > n {
        let drop = (scale_i - n) as usize;
        let mut last = 0u32;
        for _ in 0..drop {
            last = digits.divmod_small(10);
        }
        if last >= 5 {
            digits.add_one();
        }
        return (digits, n);
    }
    // Already exact at n places: digits / 10**scale, scale <= n.
    (digits, scale_i)
}

fn decimal_string(digits: &BigUint, places: i64) -> String {
    // `digits` is a non-negative BigUint holding the value
    // * 10**places. `places` may be negative (round(x, -1)
    // rounds to tens): then the value is digits * 10**(-places).
    let s = digits.to_decimal_string();
    if places == 0 {
        return s;
    }
    if places < 0 {
        let mut out = s;
        out.push_str(&"0".repeat((-places) as usize));
        return out;
    }
    let places = places as usize;
    if s.len() > places {
        let (a, b) = s.split_at(s.len() - places);
        format!("{a}.{b}")
    } else {
        format!("0.{}{}", "0".repeat(places - s.len()), s)
    }
}

/// Round the exact decimal (digits / 10**scale) to `n` places,
/// half away from zero, as a double. `n` below -308 rounds every
/// finite double to (signed) zero - 10**309 already exceeds the
/// largest finite double, so no huge power is ever built.
fn round_exact_to_double(sign: i32, digits: BigUint, scale: u32, n: i64) -> f64 {
    if n < -308 {
        return if sign < 0 { -0.0 } else { 0.0 };
    }
    let (rounded, places) = round_decimal(digits, scale, n);
    if rounded.is_zero() {
        // Preserve a negative zero: round(-0.001, 2) is -0.0.
        return if sign < 0 { -0.0 } else { 0.0 };
    }
    let text = decimal_string(&rounded, places);
    let parsed: f64 = text.parse().unwrap_or(f64::INFINITY);
    if sign < 0 { -parsed } else { parsed }
}

/// newr `round()`: half-away-from-zero (schoolbook), on the
/// EXACT value of the float - not its shortest repr.
/// `round(2.675, 2)` is 2.67, because the float 2.675 is stored
/// slightly below 2.675. `round(x)` returns an `int` (int64
/// range-checked); `round(x, n)` returns a `float`. Non-finite
/// input: `round(inf)` raises (Python's behaviour);
/// `round(inf, n)` returns `inf`, `round(nan, n)` returns `nan`.
/// An int input rounds its exact value too (`round(5, -1)` is 10).
pub fn round_half_away(x: f64) -> i64 {
    if !x.is_finite() {
        throw(coded_error(
            "OverflowError",
            "cannot round infinite or NaN value",
        ));
    }
    let (sign, digits, scale) = exact_decimal(x);
    let (rounded, _places) = round_decimal(digits, scale, 0);
    if rounded.is_zero() {
        return 0;
    }
    let mut text = rounded.to_decimal_string();
    if sign < 0 {
        text = format!("-{text}");
    }
    match text.parse::<i64>() {
        Ok(v) => v,
        Err(_) => throw(NewrError::new(format!(
            "integer overflow: {text} is outside int64 range"
        ))),
    }
}

pub fn round_half_away_n(x: f64, n: i64) -> f64 {
    if !x.is_finite() {
        return x;
    }
    let (sign, digits, scale) = exact_decimal(x);
    round_exact_to_double(sign, digits, scale, n)
}

pub fn round_half_away_int(x: i64) -> i64 {
    x
}

pub fn round_half_away_int_n(x: i64, n: i64) -> f64 {
    let sign: i32 = if x < 0 { -1 } else { 1 };
    let digits = BigUint::from_u64(x.unsigned_abs());
    round_exact_to_double(sign, digits, 0, n)
}

pub fn round_half_away_bool(x: bool) -> i64 {
    if x { 1 } else { 0 }
}

pub fn round_half_away_bool_n(x: bool, n: i64) -> f64 {
    round_half_away_int_n(if x { 1 } else { 0 }, n)
}

// ---------------------------------------------------------------- conversions

fn json_string(x: &str) -> String {
    // JSON-style quoting for error messages.
    let mut out = String::from("\"");
    for c in x.chars() {
        match c {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            c if (c as u32) < 0x20 => {
                out.push_str(&format!("\\u{:04x}", c as u32));
            }
            c => out.push(c),
        }
    }
    out.push('"');
    out
}

fn is_int_string(t: &str) -> bool {
    let b = t.as_bytes();
    let mut i = 0;
    if i < b.len() && (b[i] == b'+' || b[i] == b'-') {
        i += 1;
    }
    if i >= b.len() {
        return false;
    }
    b[i..].iter().all(|c| c.is_ascii_digit())
}

fn is_float_string(t: &str) -> bool {
    // ^[+-]?(\d+(\.\d*)?|\.\d+)([eE][+-]?\d+)?$
    let b = t.as_bytes();
    let mut i = 0;
    if i < b.len() && (b[i] == b'+' || b[i] == b'-') {
        i += 1;
    }
    let mut digits = 0;
    while i < b.len() && b[i].is_ascii_digit() {
        i += 1;
        digits += 1;
    }
    let mut has_dot = false;
    if i < b.len() && b[i] == b'.' {
        has_dot = true;
        i += 1;
        while i < b.len() && b[i].is_ascii_digit() {
            i += 1;
            digits += 1;
        }
    }
    if digits == 0 {
        return false;
    }
    let _ = has_dot;
    if i < b.len() && (b[i] == b'e' || b[i] == b'E') {
        i += 1;
        if i < b.len() && (b[i] == b'+' || b[i] == b'-') {
            i += 1;
        }
        let mut e_digits = 0;
        while i < b.len() && b[i].is_ascii_digit() {
            i += 1;
            e_digits += 1;
        }
        if e_digits == 0 {
            return false;
        }
    }
    i == b.len()
}

/// newr `int(x)` from a float (truncates toward zero). The
/// result is int64 range-checked.
pub fn to_int_from_float(x: f64) -> i64 {
    if !x.is_finite() {
        throw(coded_error(
            "OverflowError",
            "cannot convert infinite or NaN to int",
        ));
    }
    if x >= 9223372036854775808.0 || x <= -9223372036854775808.0 {
        throw(NewrError::new(format!(
            "integer overflow: {} is outside int64 range",
            float_str(x)
        )));
    }
    x as i64
}

/// newr `int(x)` from a str (parses; throws on invalid).
pub fn to_int_from_str(x: &str) -> i64 {
    let t = x.trim();
    if is_int_string(t) {
        return match t.parse::<i64>() {
            Ok(v) => v,
            Err(_) => throw(NewrError::new(format!(
                "integer overflow: {t} is outside int64 range"
            ))),
        };
    }
    throw(coded_error(
        "ValueError",
        &format!("invalid literal for int(): {}", json_string(x)),
    ));
}

/// newr `int(x)` from a bool (0/1).
pub fn to_int_from_bool(x: bool) -> i64 {
    if x { 1 } else { 0 }
}

/// newr `float(x)` from a str. `float("inf")` / `float("nan")`
/// are accepted (Python behaviour).
pub fn to_float_from_str(x: &str) -> f64 {
    let t = x.trim().replace('_', "");
    let low = t.to_lowercase();
    match low.as_str() {
        "inf" | "+inf" | "infinity" | "+infinity" => return f64::INFINITY,
        "-inf" | "-infinity" => return f64::NEG_INFINITY,
        "nan" | "+nan" | "-nan" => return f64::NAN,
        _ => {}
    }
    if is_float_string(&t) {
        return t.parse::<f64>().unwrap_or(f64::NAN);
    }
    throw(coded_error(
        "ValueError",
        &format!("could not convert string to float: {}", json_string(x)),
    ));
}

/// newr `bool(x)` from a str (`"true"`/`"false"`, exact
/// lowercase) or an int (0 -> false). Parsing, not truthiness.
pub fn to_bool_from_str(x: &str) -> bool {
    if x == "true" {
        return true;
    }
    if x == "false" {
        return false;
    }
    throw(coded_error(
        "ValueError",
        &format!(
            "invalid bool: {} (expected \"true\" or \"false\")",
            json_string(x)
        ),
    ));
}

pub fn to_bool_from_int(x: i64) -> bool {
    x != 0
}

pub fn to_bool_from_float(x: f64) -> bool {
    x != 0.0
}

/// Float rendering with PYTHON repr semantics (J4): shortest
/// round-trip digits, scientific notation iff the decimal
/// exponent is < -4 or >= 16, ".0" for integral floats. The
/// digits are derived from the EXACT decimal expansion: the
/// shortest prefix that parses back to the same double (round
/// to nearest, ties to even - David Gay's dtoa mode 0, the rule
/// behind Python's repr).
pub fn float_str(x: f64) -> String {
    if x.is_nan() {
        return "nan".to_string();
    }
    if x == f64::INFINITY {
        return "inf".to_string();
    }
    if x == f64::NEG_INFINITY {
        return "-inf".to_string();
    }
    if x == 0.0 {
        return if x.to_bits() >> 63 == 1 {
            "-0.0".to_string()
        } else {
            "0.0".to_string()
        };
    }
    let neg = x < 0.0;
    let ax = x.abs();
    // Exact decimal digits of ax (finite expansion - every
    // binary fraction has one) plus the decimal exponent of
    // the leading digit.
    let (_sign, digits_big, scale) = exact_decimal(ax);
    let mut digits = digits_big.to_decimal_string();
    let point = digits.len() as i64 - scale as i64; // value = digits with . after `point`
    let e10 = point - 1;
    // Strip trailing zeros (they carry no information).
    while digits.len() > 1 && digits.ends_with('0') {
        digits.pop();
    }
    // Shortest prefix that round-trips.
    let mut best: Option<(String, i64)> = None;
    for k in 1..=digits.len() {
        let prefix = &digits[..k];
        let mut rounded = prefix.to_string();
        let mut re10 = e10;
        if k < digits.len() {
            let rest = &digits[k..];
            let first = rest.as_bytes()[0];
            let round_up = if first > b'5' {
                true
            } else if first < b'5' {
                false
            } else if rest.as_bytes()[1..].iter().any(|c| *c != b'0') {
                true // more than half
            } else {
                // Exactly half: ties to even.
                (prefix.as_bytes()[prefix.len() - 1] - b'0') % 2 == 1
            };
            if round_up {
                // Add one to the decimal digit string.
                let mut chars: Vec<u8> = prefix.as_bytes().to_vec();
                let mut i = chars.len();
                let mut carry = true;
                while carry && i > 0 {
                    i -= 1;
                    if chars[i] == b'9' {
                        chars[i] = b'0';
                    } else {
                        chars[i] += 1;
                        carry = false;
                    }
                }
                if carry {
                    rounded = format!("1{}", "0".repeat(prefix.len()));
                    re10 = e10 + 1; // 999 -> 1000
                } else {
                    rounded = String::from_utf8(chars).unwrap();
                }
            }
        }
        // Does this candidate parse back to ax?
        let cand = to_decimal_string(&rounded, re10);
        if let Ok(v) = cand.parse::<f64>() {
            if v == ax {
                best = Some((rounded, re10));
                break;
            }
        }
    }
    let (mut best, best_e10) = match best {
        Some(b) => b,
        None => (digits, e10), // Unreachable: 17 digits always round-trip.
    };
    // Strip trailing zeros from the rounded digits (rounding up
    // can produce one, e.g. "10" for 1e-7); the exponent refers
    // to the leading digit and is unaffected.
    while best.len() > 1 && best.ends_with('0') {
        best.pop();
    }
    let sign = if neg { "-" } else { "" };
    if best_e10 < -4 || best_e10 >= 16 {
        // Scientific: d[.ddd]e±XX with at least two exponent digits.
        let mant = if best.len() > 1 {
            format!("{}.{}", &best[..1], &best[1..])
        } else {
            best.clone()
        };
        let esign = if best_e10 < 0 { "-" } else { "+" };
        let eabs = format!("{:02}", best_e10.abs());
        return format!("{sign}{mant}e{esign}{eabs}");
    }
    // Fixed: decimal point after best_e10+1 digits.
    let point2 = best_e10 + 1;
    if point2 <= 0 {
        format!("{sign}0.{}{}", "0".repeat((-point2) as usize), best)
    } else if point2 as usize >= best.len() {
        format!(
            "{sign}{best}{}.0",
            "0".repeat(point2 as usize - best.len())
        )
    } else {
        let (a, b) = best.split_at(point2 as usize);
        format!("{sign}{a}.{b}")
    }
}

/// Render a digit string + decimal exponent as a plain decimal
/// literal (no exponent) for parsing/comparison.
fn to_decimal_string(digits: &str, e10: i64) -> String {
    let point = e10 + 1;
    if point <= 0 {
        format!("0.{}{}", "0".repeat((-point) as usize), digits)
    } else if point as usize >= digits.len() {
        format!("{}{}", digits, "0".repeat(point as usize - digits.len()))
    } else {
        let (a, b) = digits.split_at(point as usize);
        format!("{a}.{b}")
    }
}

// ---------------------------------------------------------------- rendering (P8)

/// The newr display rendering of a value. `true`/`false`/`none`
/// are lowercase; floats use Python repr (float_str);
/// collections render with elements rendered recursively and
/// strings bare (`[1, true, a]`); an `error` renders as its
/// message; a struct/enum value renders via its generated impl.
pub trait ToNewrStr {
    fn to_newr_str(&self) -> String;
}

/// newr `str(x)`: the display rendering (P8).
pub fn to_str<T: ToNewrStr + ?Sized>(x: &T) -> String {
    x.to_newr_str()
}

impl ToNewrStr for i64 {
    fn to_newr_str(&self) -> String {
        self.to_string()
    }
}

impl ToNewrStr for f64 {
    fn to_newr_str(&self) -> String {
        float_str(*self)
    }
}

impl ToNewrStr for bool {
    fn to_newr_str(&self) -> String {
        if *self { "true".to_string() } else { "false".to_string() }
    }
}

impl ToNewrStr for String {
    fn to_newr_str(&self) -> String {
        self.clone()
    }
}

impl ToNewrStr for () {
    fn to_newr_str(&self) -> String {
        "none".to_string()
    }
}

impl ToNewrStr for NewrError {
    fn to_newr_str(&self) -> String {
        self.message.clone()
    }
}

impl<T: ToNewrStr> ToNewrStr for Option<T> {
    fn to_newr_str(&self) -> String {
        match self {
            None => "none".to_string(),
            Some(v) => v.to_newr_str(),
        }
    }
}

impl<T: ToNewrStr> ToNewrStr for Vec<T> {
    fn to_newr_str(&self) -> String {
        let parts: Vec<String> = self.iter().map(|e| e.to_newr_str()).collect();
        format!("[{}]", parts.join(", "))
    }
}

impl<T: ToNewrStr, E: ToNewrStr> ToNewrStr for Result<T, E> {
    fn to_newr_str(&self) -> String {
        match self {
            Ok(v) => format!("ok({})", v.to_newr_str()),
            Err(e) => format!("err({})", e.to_newr_str()),
        }
    }
}

// ---------------------------------------------------------------- hashing (J6)

/// A hash consistent with newr `==`: -0.0 and 0.0 hash alike
/// (eq says they are equal), and collections hash by their
/// elements. NaN needs no agreement: eq says NaN != NaN, so
/// unequal values may share a hash.
pub trait NewrHash {
    fn newr_hash<H: Hasher>(&self, state: &mut H);
}

/// The integer hash of a value (via DefaultHasher), for combining
/// (dict/set hashing uses sums/xors like the other runtimes).
pub fn newr_hash_u64<T: NewrHash + ?Sized>(v: &T) -> u64 {
    let mut h = DefaultHasher::new();
    v.newr_hash(&mut h);
    h.finish()
}

impl NewrHash for i64 {
    fn newr_hash<H: Hasher>(&self, state: &mut H) {
        state.write_i64(*self);
    }
}

impl NewrHash for f64 {
    fn newr_hash<H: Hasher>(&self, state: &mut H) {
        let v = if *self == 0.0 { 0.0 } else { *self }; // normalizes -0.0
        state.write_u64(v.to_bits());
    }
}

impl NewrHash for bool {
    fn newr_hash<H: Hasher>(&self, state: &mut H) {
        state.write_u8(if *self { 1 } else { 0 });
    }
}

impl NewrHash for str {
    fn newr_hash<H: Hasher>(&self, state: &mut H) {
        Hash::hash(self, state);
    }
}

impl NewrHash for String {
    fn newr_hash<H: Hasher>(&self, state: &mut H) {
        state.write(self.as_bytes());
        state.write_u8(0xff);
    }
}

impl NewrHash for () {
    fn newr_hash<H: Hasher>(&self, state: &mut H) {
        state.write_u8(0);
    }
}

impl NewrHash for NewrError {
    fn newr_hash<H: Hasher>(&self, state: &mut H) {
        // Identity hash: equal errors are the same error.
        state.write_u64(self.id);
    }
}

impl<T: NewrHash> NewrHash for Option<T> {
    fn newr_hash<H: Hasher>(&self, state: &mut H) {
        match self {
            None => state.write_u8(0),
            Some(v) => {
                state.write_u8(1);
                v.newr_hash(state);
            }
        }
    }
}

impl<T: NewrHash> NewrHash for Vec<T> {
    fn newr_hash<H: Hasher>(&self, state: &mut H) {
        // 31 * h + elem, like the other runtimes' list hash.
        let mut h: u64 = 1;
        for e in self {
            h = h
                .wrapping_mul(31)
                .wrapping_add(newr_hash_u64(e));
        }
        state.write_u64(h);
    }
}

impl<T: NewrHash, E: NewrHash> NewrHash for Result<T, E> {
    fn newr_hash<H: Hasher>(&self, state: &mut H) {
        match self {
            Ok(v) => {
                state.write_u8(1);
                v.newr_hash(state);
            }
            Err(e) => {
                state.write_u8(0);
                e.newr_hash(state);
            }
        }
    }
}

/// Hash of a struct/enum's fields, consistent with `eq`.
pub fn struct_hash_u64(fields: &[u64]) -> u64 {
    let mut h: u64 = 1;
    for f in fields {
        h = h.wrapping_mul(31).wrapping_add(*f);
    }
    h
}

// ---------------------------------------------------------------- dict / set

/// A newr `dict[str, V]`: insertion-ordered like Python's dict
/// (and Java's LinkedHashMap) - iteration and rendering follow
/// insertion order; lookup is a hash map. Equality is
/// order-insensitive (by key).
#[derive(Clone, Debug)]
pub struct Dict<V> {
    map: HashMap<String, V, BuildHasherDefault<DefaultHasher>>,
    order: Vec<String>,
}

impl<V> Dict<V> {
    pub fn new() -> Self {
        Dict {
            map: HashMap::default(),
            order: Vec::new(),
        }
    }

    pub fn from_pairs(pairs: Vec<(String, V)>) -> Self {
        let mut d = Dict::new();
        for (k, v) in pairs {
            d.insert(k, v);
        }
        d
    }

    pub fn insert(&mut self, key: String, value: V) {
        if !self.map.contains_key(&key) {
            self.order.push(key.clone());
        }
        self.map.insert(key, value);
    }

    pub fn contains_key(&self, key: &str) -> bool {
        self.map.contains_key(key)
    }

    pub fn keys(&self) -> Vec<String> {
        self.order.clone()
    }

    pub fn len(&self) -> usize {
        self.map.len()
    }

    pub fn is_empty(&self) -> bool {
        self.map.is_empty()
    }
}

impl<V: Clone> Dict<V> {
    pub fn get(&self, key: &str) -> Option<V> {
        self.map.get(key).cloned()
    }
}

impl<V: Clone> Dict<V> {
    /// `d[k]`: a missing key is a KeyError (Python semantics -
    /// the reference target raises; Java's `.get()` returning
    /// null is the divergence, not the rule).
    pub fn index(&self, key: &str) -> V {
        match self.map.get(key) {
            Some(v) => v.clone(),
            None => throw(coded_error("KeyError", key)),
        }
    }
}

impl<V: PartialEq> PartialEq for Dict<V> {
    fn eq(&self, other: &Self) -> bool {
        if self.map.len() != other.map.len() {
            return false;
        }
        self.map
            .iter()
            .all(|(k, v)| other.map.get(k).map_or(false, |ov| ov == v))
    }
}

impl<V: PartialEq> Eq for Dict<V> {}

impl<V: NewrHash> NewrHash for Dict<V> {
    fn newr_hash<H: Hasher>(&self, state: &mut H) {
        let mut h: u64 = 0;
        for (k, v) in self.map.iter() {
            h = h.wrapping_add(newr_hash_u64(k) ^ newr_hash_u64(v));
        }
        state.write_u64(h);
    }
}

impl<V: NewrHash> Hash for Dict<V> {
    fn hash<H: Hasher>(&self, state: &mut H) {
        self.newr_hash(state);
    }
}

impl<V: ToNewrStr> ToNewrStr for Dict<V> {
    fn to_newr_str(&self) -> String {
        let parts: Vec<String> = self
            .order
            .iter()
            .map(|k| {
                let v = self.map.get(k).unwrap();
                format!("{}: {}", k, v.to_newr_str())
            })
            .collect();
        format!("{{{}}}", parts.join(", "))
    }
}

impl<V> Default for Dict<V> {
    fn default() -> Self {
        Dict::new()
    }
}

/// `{"k": v, ...}` - alternating key/value pairs into a Dict
/// (insertion order). Null values are allowed.
pub fn dict_of<V>(pairs: Vec<(String, V)>) -> Dict<V> {
    Dict::from_pairs(pairs)
}

/// A newr `set[T]`: insertion-ordered iteration (deterministic -
/// Rust's default HashSet is randomly seeded, which would make
/// `str(set)` flaky), membership by newr `==` (NaN is never a
/// member of itself, -0.0 equals 0.0). Equality is
/// order-insensitive.
#[derive(Clone, Debug)]
pub struct Set<T> {
    items: Vec<T>,
}

impl<T> Set<T> {
    pub fn new() -> Self {
        Set { items: Vec::new() }
    }

    pub fn from_vec(items: Vec<T>) -> Self
    where
        T: PartialEq,
    {
        let mut s = Set::new();
        for item in items {
            s.add(item);
        }
        s
    }

    pub fn len(&self) -> usize {
        self.items.len()
    }

    pub fn is_empty(&self) -> bool {
        self.items.is_empty()
    }

    pub fn iter(&self) -> std::slice::Iter<'_, T> {
        self.items.iter()
    }
}

impl<T: PartialEq> Set<T> {
    pub fn contains(&self, x: &T) -> bool {
        self.items.iter().any(|e| e == x)
    }

    /// Adds `x` unless already present. Returns true when added.
    pub fn add(&mut self, x: T) -> bool {
        if self.contains(&x) {
            return false;
        }
        self.items.push(x);
        true
    }

    /// Removes `x`. Returns true when present.
    pub fn remove(&mut self, x: &T) -> bool {
        if let Some(pos) = self.items.iter().position(|e| e == x) {
            self.items.remove(pos);
            true
        } else {
            false
        }
    }
}

impl<T: PartialEq> PartialEq for Set<T> {
    fn eq(&self, other: &Self) -> bool {
        if self.items.len() != other.items.len() {
            return false;
        }
        self.items.iter().all(|e| other.contains(e))
    }
}

impl<T: PartialEq> Eq for Set<T> {}

impl<T: NewrHash> NewrHash for Set<T> {
    fn newr_hash<H: Hasher>(&self, state: &mut H) {
        let mut h: u64 = 0;
        for e in &self.items {
            h = h.wrapping_add(newr_hash_u64(e));
        }
        state.write_u64(h);
    }
}

impl<T: PartialEq + NewrHash> Hash for Set<T> {
    fn hash<H: Hasher>(&self, state: &mut H) {
        self.newr_hash(state);
    }
}

impl<T: ToNewrStr> ToNewrStr for Set<T> {
    fn to_newr_str(&self) -> String {
        let parts: Vec<String> = self.items.iter().map(|e| e.to_newr_str()).collect();
        format!("{{{}}}", parts.join(", "))
    }
}

impl<T> Default for Set<T> {
    fn default() -> Self {
        Set::new()
    }
}

// ---------------------------------------------------------------- list helpers

/// newr `list[i]`: out-of-range indexing is a runtime error.
/// Valid indices are `0 <= i < len(lst)`; a negative index is
/// out of range (never a wrap).
pub fn list_index<T: Clone>(xs: &[T], i: i64) -> T {
    if i < 0 || i as usize >= xs.len() {
        throw(coded_error("IndexError", "list index out of range"));
    }
    xs[i as usize].clone()
}

/// newr `list.get(i, default)`: a negative index is out of
/// range (no wrap-around); anything outside `0 <= i < len(lst)`
/// returns `default`.
pub fn list_get<T: Clone>(xs: &[T], i: i64, dflt: T) -> T {
    if i >= 0 && (i as usize) < xs.len() {
        xs[i as usize].clone()
    } else {
        dflt
    }
}

/// newr `x in [..]`: structural equality (a list of structs
/// contains an equal-but-different instance).
pub fn list_contains<T: PartialEq>(xs: &[T], x: &T) -> bool {
    xs.iter().any(|e| e == x)
}

/// newr `set.remove(x)`: removing a missing element is an error
/// (Python's `set.remove` raises KeyError).
pub fn set_remove<T: PartialEq + ToNewrStr>(s: &mut Set<T>, x: &T) {
    if !s.remove(x) {
        throw(coded_error("KeyError", &to_str(x)));
    }
}

/// newr `T?.unwrap()`: the value, or a thrown error on `null`.
pub fn unwrap_opt<T>(v: Option<T>) -> T {
    match v {
        Some(x) => x,
        None => throw(NewrError::new("unwrap of none")),
    }
}

/// newr `range(...)`: like Python's range, materialised as a
/// list of longs. range(stop) / range(start, stop) /
/// range(start, stop, step); a zero step is a ValueError.
pub fn range(stop: i64) -> Vec<i64> {
    range_step(0, stop, 1)
}

pub fn range_from(start: i64, stop: i64) -> Vec<i64> {
    range_step(start, stop, 1)
}

pub fn range_step(start: i64, stop: i64, step: i64) -> Vec<i64> {
    if step == 0 {
        throw(coded_error("ValueError", "range() arg 3 must not be zero"));
    }
    let mut out = Vec::new();
    let mut i = start;
    if step > 0 {
        while i < stop {
            out.push(i);
            i = i.wrapping_add(step);
            if i < start {
                break; // wrapped past the end (Java wraps too)
            }
        }
    } else {
        while i > stop {
            out.push(i);
            i = i.wrapping_add(step);
            if i > start {
                break;
            }
        }
    }
    out
}

/// `len(x)` for a str (code points, P5).
pub fn str_len(s: &str) -> i64 {
    s.chars().count() as i64
}

// ---------------------------------------------------------------- Result

/// newr `try <expr>`: Ok-wrap the value, catch a thrown error
/// into `err(...)`. The caught error is coerced with `as_error`
/// so `.message` / `.code` are always available.
pub fn try_result<T, F: FnOnce() -> T>(f: F) -> Result<T, NewrError> {
    match std::panic::catch_unwind(std::panic::AssertUnwindSafe(f)) {
        Ok(v) => Ok(v),
        Err(payload) => Err(as_error(payload)),
    }
}

/// newr `Result.unwrap()`: the Ok value, or the stored error
/// re-thrown (the error itself, code and all - not a panic
/// message).
pub fn result_unwrap<T>(r: Result<T, NewrError>) -> T {
    match r {
        Ok(v) => v,
        Err(e) => throw(e),
    }
}

/// `Result.unwrap()` for a non-error error type: re-thrown as a
/// plain error carrying the rendered value.
pub fn result_unwrap_any<T, E: ToNewrStr>(r: Result<T, E>) -> T {
    match r {
        Ok(v) => v,
        Err(e) => throw(NewrError::new(to_str(&e))),
    }
}

pub fn result_unwrap_or<T>(r: Result<T, NewrError>, dflt: T) -> T {
    r.unwrap_or(dflt)
}

// ---------------------------------------------------------------- strings

/// Code-point list of a string (SPEC pin P5: `str.chars()`).
pub fn chars(s: &str) -> Vec<String> {
    s.chars().map(|c| c.to_string()).collect()
}

/// `s.split(sep)` with a LITERAL separator and Python's
/// trailing-empty-strings behaviour.
pub fn split_str(s: &str, sep: &str) -> Vec<String> {
    s.split(sep).map(|p| p.to_string()).collect()
}

/// `s[i]` by code point (P5). Negative indices wrap like
/// Python; out of range raises IndexError.
pub fn str_index(s: &str, idx: i64) -> String {
    let cps: Vec<char> = s.chars().collect();
    let i = if idx < 0 {
        cps.len() as i64 + idx
    } else {
        idx
    };
    if i < 0 || i as usize >= cps.len() {
        throw(coded_error(
            "IndexError",
            &format!("string index out of range: {idx}"),
        ));
    }
    cps[i as usize].to_string()
}

/// `abs(x)` for int64: `abs(i64::MIN)` is an overflow error
/// (Python: abs(-2**63) = 2**63, out of int64 range).
pub fn abs_long(x: i64) -> i64 {
    if x == i64::MIN {
        throw(NewrError::new(
            "integer overflow: 9223372036854775808 is outside int64 range",
        ));
    }
    x.abs()
}

// ---------------------------------------------------------------- print / input

/// newr `print`: every argument rendered with `to_str`, joined
/// with a single space (Python's print).
pub fn print(args: &[String]) {
    println!("{}", args.join(" "));
}

static STDIN_READER: OnceLock<Mutex<std::io::BufReader<std::io::Stdin>>> =
    OnceLock::new();

fn stdin_reader() -> &'static Mutex<std::io::BufReader<std::io::Stdin>> {
    STDIN_READER.get_or_init(|| Mutex::new(std::io::BufReader::new(std::io::stdin())))
}

/// newr `input(prompt = "")`: read one line from stdin, like
/// Python's input(). The prompt (if any) is written with no
/// newline first. At EOF, returns "".
pub fn input(prompt: &str) -> String {
    if !prompt.is_empty() {
        print!("{prompt}");
        let _ = std::io::stdout().flush();
    }
    let mut reader = stdin_reader().lock().unwrap();
    let mut line = String::new();
    match reader.read_line(&mut line) {
        Ok(_) => {
            if line.ends_with('\n') {
                line.pop();
                if line.ends_with('\r') {
                    line.pop();
                }
            }
            line
        }
        Err(_) => String::new(),
    }
}

// ---------------------------------------------------------------- random

fn random_bytes(buf: &mut [u8]) {
    // /dev/urandom is the std-only entropy source on Linux.
    // A time-seeded xorshift fallback keeps the function total
    // if urandom is ever unavailable.
    use std::io::Read;
    if let Ok(mut f) = std::fs::File::open("/dev/urandom") {
        if f.read_exact(buf).is_ok() {
            return;
        }
    }
    let seed = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_nanos() as u64)
        .unwrap_or(0x9e3779b97f4a7c15)
        ^ (std::process::id() as u64) << 32;
    let mut state = seed | 1;
    for b in buf.iter_mut() {
        state ^= state << 13;
        state ^= state >> 7;
        state ^= state << 17;
        *b = state as u8;
    }
}

/// newr `random.int(lo, hi)`: inclusive on both ends
/// (Python's randint). Exact for any int64 range: rejection
/// sampling over random bytes.
pub fn random_int(lo: i64, hi: i64) -> i64 {
    if hi < lo {
        throw(coded_error("ValueError", "empty range for random.int()"));
    }
    let span = hi as i128 - lo as i128 + 1;
    if span == 1 {
        return lo;
    }
    let bits = 128 - (span - 1).leading_zeros();
    let bytes = ((bits + 7) / 8) as usize;
    let mut buf = [0u8; 16];
    loop {
        random_bytes(&mut buf[..bytes]);
        let mut v: i128 = 0;
        for &b in buf[..bytes].iter() {
            v = (v << 8) | b as i128;
        }
        if v < span {
            return (lo as i128 + v) as i64;
        }
    }
}

pub fn random_choice<T: Clone>(xs: &[T]) -> T {
    if xs.is_empty() {
        throw(coded_error(
            "IndexError",
            "Cannot choose from an empty sequence",
        ));
    }
    let idx = random_int(0, xs.len() as i64 - 1) as usize;
    xs[idx].clone()
}

/// Returns () (the newr value `none`) so the call stays usable
/// in expression position, like the other runtimes.
pub fn random_shuffle<T>(xs: &mut [T]) {
    let n = xs.len();
    if n < 2 {
        return;
    }
    for i in (1..n).rev() {
        let j = random_int(0, i as i64) as usize;
        xs.swap(i, j);
    }
}

// ---------------------------------------------------------------- time (P4)

const TIME_FORMATS: [&str; 5] = [
    "%Y-%m-%d",
    "%H:%M",
    "%Y-%m-%d %H:%M",
    "%d/%m/%Y",
    "%H:%M:%S",
];

/// Seconds since the Unix epoch (UTC).
pub fn time_now() -> i64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs() as i64)
        .unwrap_or(0)
}

/// newr `time.format(ts, fmt)`: UTC, never local time (P4) -
/// the same program prints the same output on every machine.
/// The format must be one of the spec's fixed set (§12.2).
/// Civil-date conversion is Howard Hinnant's days-from-epoch
/// algorithm (no chrono in std).
pub fn time_format(ts: i64, fmt: &str) -> String {
    if !TIME_FORMATS.contains(&fmt) {
        throw(NewrError::new(format!(
            "unsupported time format: {}",
            json_string(fmt)
        )));
    }
    let days = ts.div_euclid(86400);
    let secs = ts.rem_euclid(86400);
    let hour = secs / 3600;
    let minute = (secs % 3600) / 60;
    let second = secs % 60;
    // days -> civil (Hinnant): z = days + 719468.
    let z = days + 719468;
    let era = if z >= 0 { z } else { z - 146096 } / 146097;
    let doe = z - era * 146097; // [0, 146096]
    let yoe = (doe - doe / 1460 + doe / 36524 - doe / 146096) / 365; // [0, 399]
    let y = yoe + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100); // [0, 365]
    let mp = (5 * doy + 2) / 153; // [0, 11]
    let day = doy - (153 * mp + 2) / 5 + 1; // [1, 31]
    let month = if mp < 10 { mp + 3 } else { mp - 9 }; // [1, 12]
    let year = if month <= 2 { y + 1 } else { y };
    let y = format!("{year:04}");
    let mo = format!("{month:02}");
    let d = format!("{day:02}");
    let h = format!("{hour:02}");
    let mi = format!("{minute:02}");
    let s = format!("{second:02}");
    match fmt {
        "%Y-%m-%d" => format!("{y}-{mo}-{d}"),
        "%H:%M" => format!("{h}:{mi}"),
        "%Y-%m-%d %H:%M" => format!("{y}-{mo}-{d} {h}:{mi}"),
        "%d/%m/%Y" => format!("{d}/{mo}/{y}"),
        "%H:%M:%S" => format!("{h}:{mi}:{s}"),
        _ => throw(NewrError::new(format!(
            "unsupported time format: {}",
            json_string(fmt)
        ))),
    }
}

// ---------------------------------------------------------------- file I/O

fn io_code(e: &std::io::Error) -> String {
    // Host IO errors are host-specific on every target (Java:
    // "NoSuchFileException", Python: "FileNotFoundError", JS:
    // "ENOENT"); Rust reports the io::ErrorKind name.
    format!("{:?}", e.kind())
}

/// newr `file.read(path)`: the whole file as UTF-8 text. A
/// missing file throws a NewrError whose code is the host
/// error kind.
pub fn file_read(path: &str) -> String {
    match std::fs::read_to_string(path) {
        Ok(s) => s,
        Err(e) => {
            let msg = e.to_string();
            throw(NewrError::coded(io_code(&e), msg))
        }
    }
}

/// newr `file.write(path, content)`: overwrite (create if
/// needed). Returns () (the newr value `none`).
pub fn file_write(path: &str, content: &str) {
    if let Err(e) = std::fs::write(path, content) {
        let msg = e.to_string();
        throw(NewrError::coded(io_code(&e), msg));
    }
}

/// newr `file.append(path, content)`: append (create if needed).
/// Returns () (the newr value `none`).
pub fn file_append(path: &str, content: &str) {
    use std::io::Write as _;
    match std::fs::OpenOptions::new().create(true).append(true).open(path) {
        Ok(mut f) => {
            if let Err(e) = f.write_all(content.as_bytes()) {
                let msg = e.to_string();
                throw(NewrError::coded(io_code(&e), msg));
            }
        }
        Err(e) => {
            let msg = e.to_string();
            throw(NewrError::coded(io_code(&e), msg));
        }
    }
}

/// newr `file.exists(path)`.
pub fn file_exists(path: &str) -> bool {
    std::path::Path::new(path).exists()
}

// ---------------------------------------------------------------- env

/// `env.get(name)` / `env.get(name, default)`: None when the
/// variable is absent and no default is given.
pub fn env_get(name: &str) -> Option<String> {
    std::env::var(name).ok()
}

pub fn env_get_or(name: &str, dflt: &str) -> String {
    std::env::var(name).unwrap_or_else(|_| dflt.to_string())
}

/// `env.args`: the command-line arguments, argv[0] included
/// (Rust's `std::env::args` already carries argv[0], unlike
/// Java's main-args - so no setMainArgs patch is needed).
pub fn env_args() -> Vec<String> {
    std::env::args().collect()
}
