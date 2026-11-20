// The newr Dart runtime (M5).
//
// This file is the runtime half of every emitted Dart program that
// needs it: checked 64-bit integer arithmetic (Dart ints wrap
// silently, so every op is checked with BigInt), truncating
// division, half-away rounding on the EXACT float value, the newr
// conversion/rendering rules, `Result`, and the `?.` / `??`
// helpers. It is stdlib-only (`dart:io`, `dart:math`) and
// dependency-free.
//
// It lives in the compiler package as `compiler/newr_rt.dart` and
// `newr build --target dart` copies it - byte-identical - next to
// an emitted `.dart` file when (and only when) that program uses a
// helper from it. Do not edit a copied file by hand: edit this file
// in the newr repo and rebuild.
//
// Semantics are pinned in BUILD_REPORT.md §21 (K2-K6, mirroring
// the M2 pins P1-P12 and the M3 pins J1-J13). Every rule here is
// unit-tested in tests/dart/RtChecks.dart (run by
// tests/test_newr_rt_dart.py).
//
// The runtime is a single class `NewrRt` with static methods, so
// an emitted file runs with plain `dart <stem>.dart` and no build
// file. Emitted code calls helpers as `NewrRt.add(a, b)`; error
// classes are `NewrError` and friends.

import 'dart:io';
import 'dart:math' as math;

/// Base of every newr `error` value. Carries the error payload.
/// `code` is null unless set (a caught host error keeps its own
/// code via asError).
class NewrError extends Error {
  final String message;
  final String? code;
  final Object? payload;

  NewrError(this.message, [this.code, this.payload]);

  @override
  String toString() => message;
}

/// Checked-int overflow. The Python target's NewrOverflowError is
/// a NewrError subclass constructed WITHOUT a code, so a newr
/// `catch e:` sees e.code == none (the class is told by `name`,
/// not by `code`). The builtin OverflowError (round(inf), int(inf),
/// float(huge int)) is a different site: it throws
/// codedError("OverflowError", ...) instead.
class NewrOverflowError extends NewrError {
  NewrOverflowError(String message) : super(message);
}

/// Division or modulo by zero. Same code rule as above.
class NewrDivisionByZeroError extends NewrError {
  NewrDivisionByZeroError(String message) : super(message);
}

class NewrRt {
  // ---------------------------------------------------------------- int64

  static const int int64Min = -9223372036854775808;
  static const int int64Max = 9223372036854775807;

  static final BigInt _int64MinBi = BigInt.from(int64Min);
  static final BigInt _int64MaxBi = BigInt.from(int64Max);
  // Largest magnitude whose double conversion is finite in the
  // Python sense: float(2**1024 - 2**971) is finite, one more is
  // inf (mirrors the JS _MAX_FLOAT_BIGINT).
  static final BigInt _maxFloatBigInt =
      (BigInt.one << 1024) - (BigInt.one << 971);

  /// Return `v` if it fits in signed 64-bit, else raise.
  static int checkInt(Object v) {
    if (v is int) return v;
    if (v is BigInt) {
      if (v < _int64MinBi || v > _int64MaxBi) {
        throw NewrOverflowError('integer overflow: $v is outside int64 range');
      }
      return v.toInt();
    }
    throw NewrError('cannot check int of ${typeName(v)}');
  }

  // ---------------------------------------------------------------- errors

  /// An error with a host-style code (IndexError, KeyError,
  /// ValueError): the Python target raises the builtin of that
  /// name, so a newr `catch e:` sees the same code on both targets.
  static NewrError codedError(String code, String message) =>
      NewrError(message, code);

  /// Coerce whatever a `catch` caught into a newr `error` value.
  /// A `NewrError` passes through unchanged; a plain error keeps
  /// its message and gains its runtime type name as the code (a
  /// FileSystemException, a StateError); a string becomes an error
  /// with that message. This is what makes `.message` / `.code`
  /// total inside every `catch` body (SPEC §10).
  static NewrError asError(Object e) {
    if (e is NewrError) return e;
    if (e is String) return NewrError(e);
    if (e is Error || e is Exception) {
      return NewrError(e.toString(), e.runtimeType.toString());
    }
    return NewrError(e.toString());
  }

  // ---------------------------------------------------------------- checked int arithmetic

  static int add(int a, int b) {
    final r = BigInt.from(a) + BigInt.from(b);
    if (r < _int64MinBi || r > _int64MaxBi) {
      throw NewrOverflowError('integer overflow: $r is outside int64 range');
    }
    return r.toInt();
  }

  static int sub(int a, int b) {
    final r = BigInt.from(a) - BigInt.from(b);
    if (r < _int64MinBi || r > _int64MaxBi) {
      throw NewrOverflowError('integer overflow: $r is outside int64 range');
    }
    return r.toInt();
  }

  static int mul(int a, int b) {
    final r = BigInt.from(a) * BigInt.from(b);
    if (r < _int64MinBi || r > _int64MaxBi) {
      throw NewrOverflowError('integer overflow: $r is outside int64 range');
    }
    return r.toInt();
  }

  static int neg(int a) {
    if (a == int64Min) {
      throw NewrOverflowError(
        'integer overflow: 9223372036854775808 is outside int64 range',
      );
    }
    return -a;
  }

  static int powInt(int a, int b) {
    // newr `**` on ints: checked, bounded before computing.
    // A negative exponent is an error (an int result cannot
    // represent it). The exponent is bounded by repeated
    // squaring with a check at every multiply - `10 ** 10**9`
    // raises instead of allocating by computing blindly.
    if (b < 0) {
      throw NewrError('negative exponent for int pow');
    }
    if (a == 0) return b == 0 ? 1 : 0;
    if (a == 1) return 1;
    if (a == -1) return (b % 2 == 1) ? -1 : 1;
    var result = 1;
    var base = a;
    var exp = b;
    while (exp > 0) {
      if (exp % 2 == 1) {
        result = mul(result, base);
      }
      exp ~/= 2;
      if (exp > 0) {
        base = mul(base, base);
      }
    }
    return result;
  }

  static int tdiv(int a, int b) {
    // newr `/` on ints: truncating division (toward zero).
    // Dart `~/` already truncates - the check is the two error
    // cases.
    if (b == 0) {
      throw NewrDivisionByZeroError('integer division by zero');
    }
    if (a == int64Min && b == -1) {
      throw NewrOverflowError('integer overflow: INT64_MIN / -1');
    }
    return a ~/ b;
  }

  static int tmod(int a, int b) {
    // newr `%` on ints: remainder with the dividend's sign.
    // Dart `%` is Euclidean, so use `remainder` (the truncating
    // remainder, like C) instead.
    if (b == 0) {
      throw NewrDivisionByZeroError('integer modulo by zero');
    }
    if (a == int64Min && b == -1) {
      return 0; // mathematically 0; no overflow (unlike division)
    }
    return a.remainder(b);
  }

  static double powFloat(double a, double b) {
    // newr `**` on floats: IEEE semantics. Dart `math.pow`
    // already returns NaN for a negative base with a fractional
    // exponent (Python returns a complex there - newr has no
    // complex type, so the answer is NaN) and Infinity on
    // overflow. The one case Dart gets wrong is a zero base
    // with a negative exponent: Dart gives Infinity, Python
    // raises ZeroDivisionError.
    if (a == 0 && b < 0) {
      throw codedError(
        'ZeroDivisionError',
        '0.0 cannot be raised to a negative power',
      );
    }
    return math.pow(a, b).toDouble();
  }

  static double fmod(double a, double b) {
    // newr `%` on floats: C `fmod` semantics (dividend's
    // sign). Dart `remainder` is the truncating remainder.
    // Division by zero raises like the int case (Dart would
    // give NaN).
    if (b == 0) {
      throw NewrDivisionByZeroError('float modulo by zero');
    }
    return a.remainder(b);
  }

  static double fdiv(double a, double b) {
    // newr `/` on floats: IEEE division, except division by
    // zero raises like Python (Dart would give Infinity for a
    // non-zero dividend and NaN for 0/0). `b == 0` covers -0
    // as well. Python's native `/` raises the BUILTIN
    // ZeroDivisionError, so this throws the coded error, not
    // the codeless class - a `catch e:` sees
    // e.code == "ZeroDivisionError" on both targets.
    if (b == 0) {
      throw codedError('ZeroDivisionError', 'float division by zero');
    }
    return a / b;
  }

  // ---------------------------------------------------------------- rounding (P2 / J3)

  /// Exact decimal expansion of a finite double: value =
  /// sign * digits / 10**scale, digits a non-negative BigInt.
  /// Mirrors the JS _exactDecimal (same bit decomposition),
  /// recovered without raw double bits: scale the double by
  /// powers of two until integral (exact in binary), then peel
  /// its bits off one by one (exact for integer-valued doubles).
  static ({int sign, BigInt digits, int scale}) _exactDecimal(double x) {
    final neg = x < 0 || (x == 0 && (1 / x) < 0);
    var ax = x.abs();
    if (ax == 0) return (sign: neg ? -1 : 1, digits: BigInt.zero, scale: 0);
    // Find e such that ax * 2**(-e) is an integer: double ax by
    // 2 until integral (exact), tracking the exponent.
    var e = 0;
    while (ax != ax.truncateToDouble()) {
      ax *= 2;
      e -= 1;
      if (e < -1100) break; // unreachable for finite doubles
    }
    // ax is now an integer-valued double. Extract its exact
    // integer value: repeatedly take the low 53 bits via
    // remainder (exact while v >= 2**53 the remainder is still
    // exact - v is a multiple of 2**(53-k)...). The robust
    // exact method: peel off bits from the top using
    // frexp-like halving. Halving an integer-valued double by
    // 2 is exact down to 1; the parity of each halving step
    // gives the bits from the top down.
    var m = BigInt.zero;
    var v = ax;
    // Collect bits: v is an integer; while v > 0, the lowest
    // bit is v % 2 (exact for integer-valued doubles - v % 2
    // is computed exactly in binary).
    var shift = 0;
    while (v > 0) {
      final bit = v.remainder(2.0);
      if (bit == 1.0) {
        m |= (BigInt.one << shift);
      }
      v = (v / 2).truncateToDouble();
      shift += 1;
    }
    // m is now the exact integer mantissa, ax = m * 2**e.
    if (e >= 0) {
      return (sign: neg ? -1 : 1, digits: m << e, scale: 0);
    }
    // value = m / 2**(-e) = m * 5**(-e) / 10**(-e).
    return (sign: neg ? -1 : 1, digits: m * BigInt.from(5).pow(-e), scale: -e);
  }

  /// Round the exact decimal (digits / 10**scale, digits >= 0)
  /// to `n` places after the point, half away from zero.
  /// Returns (rounded, places) with value = rounded / 10**places.
  static ({BigInt rounded, int places}) _roundDecimal(
    BigInt digits,
    int scale,
    int n,
  ) {
    if (scale > n) {
      final div = BigInt.from(10).pow(scale - n);
      final q = digits ~/ div;
      final r = digits % div;
      var qq = q;
      if ((r << 1) >= div) {
        qq = q + BigInt.one;
      }
      return (rounded: qq, places: n);
    }
    // Already exact at n places: digits / 10**scale, scale <= n.
    return (rounded: digits, places: scale);
  }

  static String _decimalString(BigInt digits, int places) {
    // `digits` is a non-negative BigInt holding the value
    // * 10**places. `places` may be negative (round(x, -1)
    // rounds to tens): then the value is digits * 10**(-places).
    final s = digits.toString();
    if (places == 0) return s;
    if (places < 0) return s + ('0' * -places);
    if (s.length > places) {
      return '${s.substring(0, s.length - places)}.'
          '${s.substring(s.length - places)}';
    }
    return '0.${'0' * (places - s.length)}$s';
  }

  /// Round the exact decimal (digits / 10**scale) to `n` places,
  /// half away from zero, as a double. `n` below -308 rounds every
  /// finite double to (signed) zero - 10**309 already exceeds the
  /// largest finite double, so no huge power is ever built.
  static double _roundExactToDouble(int sign, BigInt digits, int scale, int n) {
    if (n < -308) {
      return sign < 0 ? -0.0 : 0.0;
    }
    final rd = _roundDecimal(digits, scale, n);
    if (rd.rounded == BigInt.zero) {
      // Preserve a negative zero: round(-0.001, 2) is -0.0.
      return sign < 0 ? -0.0 : 0.0;
    }
    final text = _decimalString(rd.rounded, rd.places);
    return double.parse((sign < 0 ? '-' : '') + text);
  }

  /// newr `round()`: half-away-from-zero (schoolbook), on the
  /// EXACT value of the float - not its shortest repr.
  /// `round(2.675, 2)` is 2.67, because the float 2.675 is stored
  /// slightly below 2.675. `round(x)` returns an `int` (int64
  /// range-checked); `round(x, n)` returns a `float`. Non-finite
  /// input: `round(inf)` raises (Python's behaviour);
  /// `round(inf, n)` returns `inf`, `round(nan, n)` returns `nan`.
  /// An int input rounds its exact value too
  /// (`round(5, -1)` is 10 in Python).
  static int roundHalfAway(Object x) {
    if (x is bool) return x ? 1 : 0;
    if (x is int) return x;
    if (x is double) {
      if (!x.isFinite) {
        // Python raises the builtin OverflowError here.
        throw codedError('OverflowError', 'cannot round infinite or NaN value');
      }
      final d = _exactDecimal(x);
      final rd = _roundDecimal(d.digits, d.scale, 0);
      final signed = d.sign < 0 ? -rd.rounded : rd.rounded;
      return checkInt(signed);
    }
    throw NewrError('cannot round ${typeName(x)}');
  }

  /// `round(x, n)` -> double (Dart has no overloading, so the
  /// two-argument form is a separate method).
  static double roundHalfAwayN(Object x, int n) {
    if (x is bool) return roundHalfAwayN(x ? 1 : 0, n);
    if (x is int) {
      final digits = BigInt.from(x).abs();
      return _roundExactToDouble(x < 0 ? -1 : 1, digits, 0, n);
    }
    if (x is double) {
      if (!x.isFinite) return x;
      final d = _exactDecimal(x);
      return _roundExactToDouble(d.sign, d.digits, d.scale, n);
    }
    throw NewrError('cannot round ${typeName(x)}');
  }

  // ---------------------------------------------------------------- conversions

  static String _jsonString(String x) {
    // JSON-style quoting for error messages (mirrors JS
    // JSON.stringify on a string).
    final sb = StringBuffer('"');
    for (var i = 0; i < x.length; i++) {
      final c = x[i];
      switch (c) {
        case '"':
          sb.write('\\"');
        case '\\':
          sb.write('\\\\');
        case '\n':
          sb.write('\\n');
        case '\r':
          sb.write('\\r');
        case '\t':
          sb.write('\\t');
        default:
          if (c.codeUnitAt(0) < 0x20) {
            sb.write('\\u${c.codeUnitAt(0).toRadixString(16).padLeft(4, '0')}');
          } else {
            sb.write(c);
          }
      }
    }
    sb.write('"');
    return sb.toString();
  }

  static final RegExp _intString = RegExp(r'^[+-]?\d+$');
  static final RegExp _floatString = RegExp(
    r'^[+-]?(\d+(\.\d*)?|\.\d+)([eE][+-]?\d+)?$',
  );

  /// newr `int(x)`: from str (parses; throws on invalid) or
  /// float (truncates toward zero). The result is int64
  /// range-checked. `int("3.5")` raises ValueError.
  static int toInt(Object? x) {
    if (x is bool) return x ? 1 : 0;
    if (x is BigInt) return checkInt(x);
    if (x is int) return x;
    if (x is double) {
      if (!x.isFinite) {
        // Python raises the builtin OverflowError here.
        throw codedError(
          'OverflowError',
          'cannot convert infinite or NaN to int',
        );
      }
      if (x >= 9223372036854775808.0 || x <= -9223372036854775808.0) {
        throw NewrOverflowError('integer overflow: $x is outside int64 range');
      }
      return x.toInt();
    }
    if (x is String) {
      final t = x.trim();
      if (_intString.hasMatch(t)) {
        return checkInt(BigInt.parse(t));
      }
      throw codedError(
        'ValueError',
        'invalid literal for int(): ${_jsonString(x)}',
      );
    }
    throw NewrError('cannot convert ${typeName(x)} to int');
  }

  /// newr `float(x)`: from str or int. `float("inf")` /
  /// `float("nan")` are accepted (Python behaviour); a huge int
  /// raises OverflowError.
  static double toFloat(Object? x) {
    if (x is bool) return x ? 1.0 : 0.0;
    if (x is BigInt) {
      if (x > _maxFloatBigInt || x < -_maxFloatBigInt) {
        // Python raises the builtin OverflowError here.
        throw codedError('OverflowError', 'int too large to convert to float');
      }
      return x.toDouble();
    }
    if (x is int) return x.toDouble();
    if (x is double) return x;
    if (x is String) {
      final t = x.trim().replaceAll('_', '');
      final low = t.toLowerCase();
      if (low == 'inf' ||
          low == '+inf' ||
          low == 'infinity' ||
          low == '+infinity') {
        return double.infinity;
      }
      if (low == '-inf' || low == '-infinity') {
        return double.negativeInfinity;
      }
      if (low == 'nan' || low == '+nan' || low == '-nan') {
        return double.nan;
      }
      if (_floatString.hasMatch(t)) {
        return double.parse(t);
      }
      throw codedError(
        'ValueError',
        'could not convert string to float: ${_jsonString(x)}',
      );
    }
    throw NewrError('cannot convert ${typeName(x)} to float');
  }

  /// newr `bool(x)`: from str (`"true"`/`"false"`, exact
  /// lowercase) or int (0 -> false). P7: parsing, not truthiness.
  static bool toBool(Object? x) {
    if (x is bool) return x;
    if (x is String) {
      if (x == 'true') return true;
      if (x == 'false') return false;
      throw codedError(
        'ValueError',
        'invalid bool: ${_jsonString(x)} (expected "true" or "false")',
      );
    }
    if (x is BigInt) return x != BigInt.zero;
    if (x is num) {
      if (x is double) return x != 0;
      return x != 0;
    }
    throw NewrError('cannot convert ${typeName(x)} to bool');
  }

  static String typeName(Object? x) {
    if (x == null) return 'none';
    if (x is String) return 'str';
    if (x is bool) return 'bool';
    if (x is double) return 'float';
    if (x is int) return 'int';
    if (x is List) return 'list';
    if (x is Map) return 'dict';
    if (x is Set) return 'set';
    return x.runtimeType.toString();
  }

  /// Float rendering with PYTHON repr semantics (J4): shortest
  /// round-trip digits, scientific notation iff the decimal
  /// exponent is < -4 or >= 16, ".0" for integral floats. Dart
  /// double.toString differs twice over: its exponent cutoffs
  /// differ (1e16 -> "10000000000000000.0", Python "1e+16"), and
  /// its shortest digits differ from Python's in rare edge cases
  /// (Double.toString(5e-324) is "4.9E-324", Python repr is
  /// "5e-324"). So the digits are derived here from the EXACT
  /// decimal expansion: the shortest prefix that parses back to
  /// the same double (round to nearest, ties to even - David
  /// Gay's dtoa mode 0, the rule behind Python's repr).
  static String floatStr(double x) {
    if (x.isNaN) return 'nan';
    if (x == double.infinity) return 'inf';
    if (x == double.negativeInfinity) return '-inf';
    if (x == 0) {
      // Preserve a negative zero: 1 / -0.0 is -inf.
      return (1 / x) < 0 ? '-0.0' : '0.0';
    }
    final neg = x < 0;
    final ax = x.abs();
    // Exact decimal digits of ax (finite expansion - every
    // binary fraction has one) plus the decimal exponent of
    // the leading digit.
    final exact = _exactDecimal(ax);
    var digits = exact.digits.toString();
    final scale = exact.scale;
    final point = digits.length - scale; // value = digits with . after `point`
    final e10 = point - 1;
    // Strip trailing zeros (they carry no information).
    var end = digits.length;
    while (end > 1 && digits[end - 1] == '0') {
      end--;
    }
    digits = digits.substring(0, end);
    // Shortest prefix that round-trips.
    String? best;
    var bestE10 = e10;
    for (var k = 1; k <= digits.length; k++) {
      var prefix = digits.substring(0, k);
      var rounded = prefix;
      var re10 = e10;
      if (k < digits.length) {
        final rest = digits.substring(k);
        bool roundUp;
        final first = rest[0];
        if (first.compareTo('5') > 0) {
          roundUp = true;
        } else if (first.compareTo('5') < 0) {
          roundUp = false;
        } else if (rest.substring(1).split('').any((c) => c != '0')) {
          roundUp = true; // more than half
        } else {
          // Exactly half: ties to even.
          roundUp = (prefix.codeUnitAt(prefix.length - 1) - 48) % 2 == 1;
        }
        if (roundUp) {
          final bumped = BigInt.parse(prefix) + BigInt.one;
          rounded = bumped.toString();
          if (rounded.length > prefix.length) {
            re10 = e10 + 1; // 999 -> 1000
          }
        }
      }
      // Does this candidate parse back to ax?
      final cand = _toDecimalString(rounded, re10);
      if (double.parse(cand) == ax) {
        best = rounded;
        bestE10 = re10;
        break;
      }
    }
    best ??= digits;
    // Strip trailing zeros from the rounded digits (rounding up
    // can produce one, e.g. "10" for 1e-7); the exponent refers
    // to the leading digit and is unaffected.
    var bend = best.length;
    while (bend > 1 && best[bend - 1] == '0') {
      bend--;
    }
    best = best.substring(0, bend);
    final sign = neg ? '-' : '';
    if (bestE10 < -4 || bestE10 >= 16) {
      // Scientific: d[.ddd]e±XX with at least two exponent digits.
      final mant = best.length > 1
          ? '${best[0]}.${best.substring(1)}'
          : best.substring(0, 1);
      final esign = bestE10 < 0 ? '-' : '+';
      final eabs = bestE10.abs().toString().padLeft(2, '0');
      return '$sign$mant'
          'e$esign$eabs';
    }
    // Fixed: decimal point after bestE10+1 digits.
    final point2 = bestE10 + 1;
    String out;
    if (point2 <= 0) {
      out = '0.${'0' * -point2}$best';
    } else if (point2 >= best.length) {
      out = '$best${'0' * (point2 - best.length)}.0';
    } else {
      out = '${best.substring(0, point2)}.${best.substring(point2)}';
    }
    return sign + out;
  }

  /// Render a digit string + decimal exponent as a plain decimal
  /// literal (no exponent) for parsing/comparison.
  static String _toDecimalString(String digits, int e10) {
    final point = e10 + 1;
    if (point <= 0) {
      return '0.${'0' * -point}$digits';
    }
    if (point >= digits.length) {
      return '$digits${'0' * (point - digits.length)}';
    }
    return '${digits.substring(0, point)}.${digits.substring(point)}';
  }

  /// newr `str(x)`: the display rendering (P8). `true`/`false`/
  /// `none` are lowercase; floats use Python repr (floatStr);
  /// collections render with elements rendered recursively and
  /// strings bare (`[1, true, a]`); an `error` renders as its
  /// message; a struct/enum value renders via its `toString`
  /// (emitted by the compiler for every struct/enum type).
  static String toStr(Object? x) {
    if (x == null) return 'none';
    if (x is bool) return x ? 'true' : 'false';
    if (x is String) return x;
    if (x is BigInt) return x.toString();
    if (x is num) {
      if (x is double) return floatStr(x);
      return x.toString();
    }
    if (x is NewrError) return x.message;
    if (x is Result) {
      if (x.isOk()) {
        return 'ok(${toStr(x.value)})';
      }
      return 'err(${toStr(x.error)})';
    }
    if (x is List) {
      final sb = StringBuffer('[');
      for (var i = 0; i < x.length; i++) {
        if (i > 0) sb.write(', ');
        sb.write(toStr(x[i]));
      }
      sb.write(']');
      return sb.toString();
    }
    if (x is Map) {
      final sb = StringBuffer('{');
      var first = true;
      for (final e in x.entries) {
        if (!first) sb.write(', ');
        first = false;
        sb.write(toStr(e.key));
        sb.write(': ');
        sb.write(toStr(e.value));
      }
      sb.write('}');
      return sb.toString();
    }
    if (x is Set) {
      final sb = StringBuffer('{');
      var first = true;
      for (final e in x) {
        if (!first) sb.write(', ');
        first = false;
        sb.write(toStr(e));
      }
      sb.write('}');
      return sb.toString();
    }
    return x.toString();
  }

  // ---------------------------------------------------------------- Result

  /// newr `Result[T, E]` (SPEC §10): the value form of errors.
  static Result<T, NewrError> ok<T>(T value) =>
      Result<T, NewrError>(true, value, null);

  static Result<T, NewrError> err<T>(NewrError error) =>
      Result<T, NewrError>(false, null, error);

  /// newr `try <expr>`: Ok-wrap the value, catch a thrown error
  /// into `err(...)`. The caught error is coerced with `asError`
  /// so `.message` / `.code` are always available.
  static Result<T, NewrError> tryResult<T>(T Function() f) {
    try {
      return ok(f());
    } catch (e) {
      return err(asError(e));
    }
  }

  // ---------------------------------------------------------------- equality (J6)

  /// newr `==`: structural equality (§7.7). Primitives compare
  /// by value; lists compare element-wise, dicts by key
  /// (order-insensitive), sets unordered, structs/enums by
  /// constructor + fields (their generated `==`), Results by
  /// kind + payload. Functions and errors compare by identity.
  /// NaN is never equal to NaN (not even inside a collection).
  static bool eq(Object? a, Object? b) {
    if (identical(a, b)) {
      // Same reference - but NaN is not == itself even as a
      // boxed double, and `identical` on two NaN doubles IS
      // true in Dart (same bit pattern), so exclude it.
      if (a is double && a.isNaN) {
        return false;
      }
      return true;
    }
    if (a == null || b == null) {
      return false;
    }
    if (a is double || b is double) {
      if (a is! double || b is! double) {
        return false;
      }
      // -0.0 == 0.0 is true, NaN == NaN is false
      // (exactly the JS `===` semantics for numbers).
      return a == b;
    }
    if (a is List || b is List) {
      if (a is! List || b is! List) {
        return false;
      }
      if (a.length != b.length) {
        return false;
      }
      for (var i = 0; i < a.length; i++) {
        if (!eq(a[i], b[i])) {
          return false;
        }
      }
      return true;
    }
    if (a is Map || b is Map) {
      if (a is! Map || b is! Map) {
        return false;
      }
      if (a.length != b.length) {
        return false;
      }
      for (final e in a.entries) {
        if (!b.containsKey(e.key) || !eq(e.value, b[e.key])) {
          return false;
        }
      }
      return true;
    }
    if (a is Set || b is Set) {
      if (a is! Set || b is! Set) {
        return false;
      }
      if (a.length != b.length) {
        return false;
      }
      outer:
      for (final x in a) {
        for (final y in b) {
          if (eq(x, y)) {
            continue outer;
          }
        }
        return false;
      }
      return true;
    }
    if (a is Result || b is Result) {
      if (a is! Result || b is! Result) {
        return false;
      }
      if (a.isOk() != b.isOk()) {
        return false;
      }
      return a.isOk() ? eq(a.value, b.value) : eq(a.error, b.error);
    }
    if (a is NewrError || b is NewrError) {
      return false; // errors compare by identity (identical above)
    }
    return a == b;
  }

  // ---------------------------------------------------------------- ?. / ?? / accessors

  /// newr `?.`: `null` when `v` is `null`, else `f(v)`. The test
  /// is `== null`, never truthiness - `0`, `""` and `[]` are
  /// values, not absences. A whole `?.` chain collapses into one
  /// call with the full postfix path inside `f`, so the base
  /// expression is evaluated exactly once.
  static R? optChain<T, R>(T? v, R Function(T) f) {
    return v == null ? null : f(v);
  }

  /// newr `??`: `v` when it is not `null`, else `defaultFn()`.
  /// The default is a thunk so it is evaluated lazily.
  static T coalesce<T>(T? v, T Function() dflt) {
    return v != null ? v : dflt();
  }

  /// newr `list[i]`: SPEC §9.2 - out-of-range indexing is a
  /// runtime error. Valid indices are `0 <= i < len(lst)`; a
  /// negative index is out of range (never a wrap).
  static T listIndex<T>(List<T> lst, int i) {
    if (i < 0 || i >= lst.length) {
      throw codedError('IndexError', 'list index out of range');
    }
    return lst[i];
  }

  /// newr `list.get(i, default)`: P9 - a negative index is
  /// out-of-range (no wrap-around); anything outside
  /// `0 <= i < len(lst)` returns `default`.
  static T listGet<T>(List<T> lst, int i, T dflt) {
    return i >= 0 && i < lst.length ? lst[i] : dflt;
  }

  /// newr `x in [..]`: structural equality (a list of structs
  /// contains an equal-but-different instance).
  static bool listContains(List<Object?> lst, Object? x) {
    for (final e in lst) {
      if (eq(e, x)) {
        return true;
      }
    }
    return false;
  }

  /// newr `set.remove(x)`: removing a missing element is an
  /// error (Python's `set.remove` raises KeyError; Dart's
  /// `Set.remove` silently returns false).
  static void setRemove(Set<Object?> s, Object? x) {
    if (!s.contains(x)) {
      throw codedError('KeyError', toStr(x));
    }
    s.remove(x);
  }

  /// newr `T?.unwrap()`: the value, or a thrown error on `null`.
  static T unwrapOpt<T>(T? v) {
    if (v == null) {
      throw NewrError('unwrap of none');
    }
    return v;
  }

  /// newr `range(...)`: like Python's range, materialised as a
  /// list of ints. range(stop) / range(start, stop) /
  /// range(start, stop, step); a zero step is a ValueError.
  static List<int> range([int a = 0, int? b, int step = 1]) {
    final int start;
    final int stop;
    if (b == null) {
      start = 0;
      stop = a;
    } else {
      start = a;
      stop = b;
    }
    if (step == 0) {
      throw codedError('ValueError', 'range() arg 3 must not be zero');
    }
    final out = <int>[];
    if (step > 0) {
      for (var i = start; i < stop; i += step) {
        out.add(i);
      }
    } else {
      for (var i = start; i > stop; i += step) {
        out.add(i);
      }
    }
    return out;
  }

  /// `len(x)` for any lenable value (str: code points, P5).
  static int lenOf(Object? x) {
    if (x is String) {
      return chars(x).length;
    }
    if (x is List) {
      return x.length;
    }
    if (x is Map) {
      return x.length;
    }
    if (x is Set) {
      return x.length;
    }
    throw NewrError('len() of unsupported value: $x', 'TypeError');
  }

  /// `{"k": v, ...}` - alternating key/value pairs into a
  /// LinkedHashMap (insertion order). Null keys and values are
  /// allowed. (Dart has no varargs, so the pairs arrive as a
  /// list.)
  static Map<String, V> dictOf<V>(List<Object?> kvs) {
    final out = <String, V>{};
    for (var i = 0; i < kvs.length; i += 2) {
      out[kvs[i] as String] = kvs[i + 1] as V;
    }
    return out;
  }

  // ---------------------------------------------------------------- strings

  /// Code-point list of a string (SPEC pin P5: `str.chars()`).
  static List<String> chars(String s) =>
      s.runes.map((cp) => String.fromCharCode(cp)).toList();

  /// newr string ordering: lexicographic by Unicode code point
  /// (SPEC 7.6). Dart's String.compareTo compares UTF-16 code
  /// units, which orders an astral character below BMP characters
  /// at U+E000 and above (a high surrogate is below U+FFFD, while
  /// by code points U+1F600 is above U+FFFD). Compare code point
  /// by code point; a strict prefix is smaller. Mirrors Java's
  /// strCmp (L11, type_model.newr, 7 Nov).
  static int strCmp(String a, String b) {
    final ac = a.runes.toList();
    final bc = b.runes.toList();
    final n = ac.length < bc.length ? ac.length : bc.length;
    for (var i = 0; i < n; i++) {
      final d = ac[i] - bc[i];
      if (d != 0) return d;
    }
    return ac.length - bc.length;
  }

  /// `s.split(sep)` with a LITERAL separator (Dart's
  /// String.split with a String pattern is already literal) and
  /// Python's trailing-empty-strings behaviour (Dart keeps them).
  static List<String> splitStr(String s, String sep) => s.split(sep);

  /// `s[i]` by code point (P5). Negative indices wrap like
  /// Python; out of range raises IndexError.
  static String strIndex(String s, int idx) {
    final cps = chars(s);
    final i = idx < 0 ? cps.length + idx : idx;
    if (i < 0 || i >= cps.length) {
      throw NewrError('string index out of range: $idx', 'IndexError');
    }
    return cps[i];
  }

  /// `abs(x)` for int64: `int64Min.abs()` wraps silently in
  /// Dart, so the minimum is an overflow error (Python:
  /// abs(-2**63) = 2**63, out of int64 range).
  static int absLong(int x) {
    if (x == int64Min) {
      throw NewrOverflowError(
        'integer overflow: 9223372036854775808 is outside int64 range',
      );
    }
    return x.abs();
  }

  // ---------------------------------------------------------------- print / input

  /// newr `print`: every argument rendered with `toStr`, joined
  /// with a single space (Python's print). Dart has no varargs,
  /// so the arguments arrive as a list.
  static void print(List<Object?> args) {
    final sb = StringBuffer();
    for (var i = 0; i < args.length; i++) {
      if (i > 0) sb.write(' ');
      sb.write(toStr(args[i]));
    }
    // (stdout, not the top-level print: this method IS named
    // `print`, so an unqualified call would recurse into it.)
    stdout.writeln(sb.toString());
  }

  /// newr `input(prompt = "")`: read one line from stdin, like
  /// Python's input(). The prompt (if any) is written with no
  /// newline first. At EOF, returns "" (mirrors the JS runtime).
  static String input([String prompt = '']) {
    if (prompt.isNotEmpty) {
      stdout.write(prompt);
    }
    try {
      final line = stdin.readLineSync(encoding: const SystemEncoding());
      if (line == null) {
        return '';
      }
      return line;
    } catch (_) {
      return '';
    }
  }

  /// newr `print` to stderr (the emitter's `System.err.println`
  /// twin).
  static void printErr(Object? x) {
    stderr.writeln(toStr(x));
  }

  // ---------------------------------------------------------------- random

  static final math.Random _secureRandom = math.Random.secure();

  /// newr `random.int(lo, hi)`: inclusive on both ends
  /// (Python's randint). Exact for any int64 range: rejection
  /// sampling over random bytes.
  static int randomInt(int lo, int hi) {
    if (hi < lo) {
      throw codedError('ValueError', 'empty range for random.int()');
    }
    final span = BigInt.from(hi) - BigInt.from(lo) + BigInt.one;
    if (span == BigInt.one) {
      return lo;
    }
    final bits = span.bitLength;
    final bytes = (bits + 7) ~/ 8;
    for (;;) {
      var v = BigInt.zero;
      for (var i = 0; i < bytes; i++) {
        v = (v << 8) | BigInt.from(_secureRandom.nextInt(256));
      }
      if (v < span) {
        return (BigInt.from(lo) + v).toInt();
      }
    }
  }

  static T randomChoice<T>(List<T> xs) {
    if (xs.isEmpty) {
      throw codedError('IndexError', 'Cannot choose from an empty sequence');
    }
    return xs[math.Random().nextInt(xs.length)];
  }

  /// Returns null (the newr value `none`) so the call stays
  /// usable in expression position, like the JS runtime.
  static Object? randomShuffle(List<Object?> xs) {
    xs.shuffle();
    return null;
  }

  // ---------------------------------------------------------------- time (P4)

  static const Set<String> _timeFormats = {
    '%Y-%m-%d',
    '%H:%M',
    '%Y-%m-%d %H:%M',
    '%d/%m/%Y',
    '%H:%M:%S',
  };

  /// newr `time.format(ts, fmt)`: UTC, never local time (P4) -
  /// the same program prints the same output on every machine.
  /// The format must be one of the spec's fixed set (§12.2).
  static String timeFormat(int ts, String fmt) {
    if (!_timeFormats.contains(fmt)) {
      throw NewrError('unsupported time format: ${_jsonString(fmt)}');
    }
    final d = DateTime.fromMillisecondsSinceEpoch(ts * 1000, isUtc: true);
    final y = d.year.toString().padLeft(4, '0');
    final mo = d.month.toString().padLeft(2, '0');
    final day = d.day.toString().padLeft(2, '0');
    final h = d.hour.toString().padLeft(2, '0');
    final mi = d.minute.toString().padLeft(2, '0');
    final s = d.second.toString().padLeft(2, '0');
    switch (fmt) {
      case '%Y-%m-%d':
        return '$y-$mo-$day';
      case '%H:%M':
        return '$h:$mi';
      case '%Y-%m-%d %H:%M':
        return '$y-$mo-$day $h:$mi';
      case '%d/%m/%Y':
        return '$day/$mo/$y';
      case '%H:%M:%S':
        return '$h:$mi:$s';
      default:
        throw NewrError('unsupported time format: ${_jsonString(fmt)}');
    }
  }

  // ---------------------------------------------------------------- file I/O

  /// newr `file.read(path)`: the whole file as UTF-8 text. A
  /// missing file throws a NewrError whose code is the host
  /// exception name (Dart: "PathNotFoundException"; the Python
  /// target reports "FileNotFoundError" there, the JS target
  /// "ENOENT"; the code for host IO errors is host-specific,
  /// BUILD_REPORT §21).
  static String fileRead(String path) {
    try {
      return File(path).readAsStringSync();
    } catch (e) {
      throw asError(e);
    }
  }

  /// newr `file.write(path, content)`: overwrite (create if
  /// needed). Returns null (the newr value `none`).
  static Object? fileWrite(String path, String content) {
    try {
      File(path).writeAsStringSync(content);
    } catch (e) {
      throw asError(e);
    }
    return null;
  }

  /// newr `file.append(path, content)`: append (create if
  /// needed). Returns null (the newr value `none`).
  static Object? fileAppend(String path, String content) {
    try {
      File(path).writeAsStringSync(content, mode: FileMode.append);
    } catch (e) {
      throw asError(e);
    }
    return null;
  }

  /// newr `file.exists(path)`.
  static bool fileExists(String path) => File(path).existsSync();

  // ---------------------------------------------------------------- ui

  /// newr `ui.text(s)` (SPEC.md section 12.8): builds a Widget.
  static Widget uiText(String text) => Widget('text', text: text);

  /// newr `ui.button(label, handler)`: builds a Widget. The
  /// handler runs only on a native target; on dart the tree is
  /// render-only, so the handler is stored and never called.
  static Widget uiButton(String label, void Function() handler) =>
      Widget('button', label: label, handler: handler);

  /// newr `ui.input(placeholder, value, handler)`: builds a
  /// Widget. The handler runs only on a native target; on dart
  /// the tree is render-only, so the handler is stored and never
  /// called.
  static Widget uiInput(String placeholder, String value,
          void Function(String) handler) =>
      Widget('input',
          placeholder: placeholder, value: value, handler: handler);

  /// newr `ui.list(items)`: builds a Widget.
  static Widget uiList(List<Widget> children) =>
      Widget('list', children: List<Widget>.unmodifiable(children));

  /// newr `ui.image(url)`: builds a Widget.
  static Widget uiImage(String url) => Widget('image', url: url);

  /// newr `ui.row(children)`: builds a Widget.
  static Widget uiRow(List<Widget> children) =>
      Widget('row', children: List<Widget>.unmodifiable(children));

  /// newr `ui.column(children)`: builds a Widget.
  static Widget uiColumn(List<Widget> children) => Widget('column',
      children: List<Widget>.unmodifiable(children));

  /// The SPEC.md section 12.8 escape: `&` then `<` `>` `"`, in
  /// that order, in text content and attribute values alike.
  static String _uiEscape(String s) => s
      .replaceAll('&', '&amp;')
      .replaceAll('<', '&lt;')
      .replaceAll('>', '&gt;')
      .replaceAll('"', '&quot;');

  /// newr `ui.render(w)`: the widget tree as deterministic HTML
  /// (SPEC.md section 12.8). No outer wrapper, no insignificant
  /// whitespace, UTF-8. Button and input indices are separate
  /// pre-order counters.
  static String uiRender(Widget widget) {
    final counters = <int>[0, 0];
    return _uiRenderWidget(widget, counters);
  }

  static String _uiRenderWidget(Widget w, List<int> counters) {
    switch (w.kind) {
      case 'text':
        return '<p>${_uiEscape(w.text)}</p>';
      case 'button':
        final n = counters[0];
        counters[0] = n + 1;
        return '<button data-action="$n">${_uiEscape(w.label)}</button>';
      case 'input':
        final n = counters[1];
        counters[1] = n + 1;
        return '<input placeholder="${_uiEscape(w.placeholder)}" '
            'value="${_uiEscape(w.value)}" data-input="$n">';
      case 'list':
        final inner = w.children
            .map((c) => '<li>${_uiRenderWidget(c, counters)}</li>')
            .join();
        return '<ul>$inner</ul>';
      case 'image':
        return '<img src="${_uiEscape(w.url)}" alt="">';
      case 'row':
        final inner =
            w.children.map((c) => _uiRenderWidget(c, counters)).join();
        return '<div data-layout="row">$inner</div>';
      case 'column':
        final inner =
            w.children.map((c) => _uiRenderWidget(c, counters)).join();
        return '<div data-layout="column">$inner</div>';
      default:
        throw NewrError('ui: unknown widget kind ${w.kind}');
    }
  }

  /// The HTML sink path for `ui.show` / `ui.run` on dart: the
  /// program stem with `.html`, next to the program file
  /// (Platform.script is the emitted `.dart` file when run with
  /// `dart` or `dart run`). Falls back to `newr.html` in the
  /// current directory when the script path is unavailable.
  static String _uiHtmlPath() {
    try {
      final script = Platform.script.toFilePath();
      if (script.endsWith('.dart')) {
        return '${script.substring(0, script.length - 5)}.html';
      }
      return '$script.html';
    } catch (_) {
      return 'newr.html';
    }
  }

  /// newr `ui.show(w)` on dart: render once, write the HTML to
  /// `<stem>.html` next to the program, print the path, return.
  /// It never opens a browser itself. There is no native window
  /// on dart in M8c (SPEC.md section 12.8).
  static void uiShow(Widget widget) {
    final path = _uiHtmlPath();
    File(path).writeAsStringSync(uiRender(widget));
    stdout.writeln(path);
  }

  /// newr `ui.run(view)` on dart: call the view once, render,
  /// write the same HTML sink, print the path, return. The
  /// interactive loop is a documented no-op on dart (SPEC.md
  /// section 12.8).
  static void uiRun(Widget Function() view) {
    final path = _uiHtmlPath();
    File(path).writeAsStringSync(uiRender(view()));
    stdout.writeln(path);
  }

  // ---------------------------------------------------------------- env

  /// `env.get(name)` / `env.get(name, default)`: null when the
  /// variable is absent and no default is given.
  static String? envGet(String name, [String? dflt]) {
    final v = Platform.environment[name];
    return v ?? dflt;
  }

  /// `env.args`: the command-line arguments, set by the
  /// generated main() (a module file has no main, so the runtime
  /// holds the copy). Defaults to an empty list before main runs.
  /// Dart's main() args exclude the program name, but newr's
  /// env.args is sys.argv 1:1 (Python: sys.argv, JS:
  /// process.argv.slice(1)) - argv[0] is the program, so
  /// setMainArgs prepends the main stem.
  static List<String> _mainArgs = <String>[];

  static void setMainArgs(String prog, List<String> args) {
    _mainArgs = [prog, ...args];
  }

  static List<String> envArgs() => _mainArgs;

  /// A hash consistent with `eq`: -0.0 and 0.0 hash alike (eq
  /// says they are equal), and collections hash by their elements
  /// (a List's hashCode would use double.hashCode for a -0.0
  /// element, which disagrees with eq). NaN needs no agreement:
  /// eq says NaN != NaN, so unequal values may share a hash.
  static int hashOf(Object? x) {
    if (x == null) {
      return 0;
    }
    if (x is double) {
      final v = x == 0.0 ? 0.0 : x; // normalizes -0.0 to 0.0
      return v.hashCode;
    }
    if (x is List) {
      var h = 1;
      for (final e in x) {
        h = 31 * h + hashOf(e);
      }
      return h;
    }
    if (x is Map) {
      var h = 0;
      for (final e in x.entries) {
        h += hashOf(e.key) ^ hashOf(e.value);
      }
      return h;
    }
    if (x is Set) {
      var h = 0;
      for (final e in x) {
        h += hashOf(e);
      }
      return h;
    }
    return x.hashCode;
  }

  /// Hash of a struct/enum's fields, consistent with `eq`.
  /// (Dart has no varargs, so the fields arrive as a list.)
  static int structHash(List<Object?> fields) {
    var h = 1;
    for (final f in fields) {
      h = 31 * h + hashOf(f);
    }
    return h;
  }
}

/// newr `Result[T, E]` (SPEC §10): the value form of errors.
/// A top-level class (not nested in NewrRt - Dart has no nested
/// classes); the emitter writes `Result<T, E>` directly.
class Result<T, E> {
  final bool _isOk;
  final T? _value;
  final E? _error;

  Result(this._isOk, this._value, this._error);

  bool isOk() => _isOk;

  bool isErr() => !_isOk;

  T unwrap() {
    if (_isOk) {
      return _value as T;
    }
    if (_error is Error || _error is Exception) {
      throw _error as Object;
    }
    throw NewrError(_error.toString());
  }

  T unwrapOr(T dflt) => _isOk ? _value as T : dflt;

  /// The payloads (used by NewrRt.toStr / NewrRt.eq).
  T? get value => _value;
  E? get error => _error;

  @override
  bool operator ==(Object other) {
    if (identical(this, other)) return true;
    if (other is! Result) return false;
    if (_isOk != other._isOk) return false;
    return _isOk
        ? NewrRt.eq(_value, other._value)
        : NewrRt.eq(_error, other._error);
  }

  @override
  int get hashCode =>
      // hashOf (not Object.hashCode): NewrRt.eq treats -0.0
      // and 0.0 as equal, so their hash codes must agree too
      // (the equals/hashCode contract).
      _isOk ? NewrRt.hashOf(_value) : NewrRt.hashOf(_error);
}

/// newr `Widget` (SPEC.md section 12.8): an opaque ui value, a
/// `kind` tag plus the fields that kind uses. Only the
/// `NewrRt.ui*` constructors build one; programs never build a
/// Widget by hand. A top-level class (not nested in NewrRt -
/// Dart has no nested classes); the emitter writes `Widget`
/// directly.
class Widget {
  final String kind;
  final String text;
  final String label;
  final Function? handler;
  final String placeholder;
  final String value;
  final String url;
  final List<Widget> children;

  const Widget(this.kind,
      {this.text = '',
      this.label = '',
      this.handler,
      this.placeholder = '',
      this.value = '',
      this.url = '',
      this.children = const <Widget>[]});
}
