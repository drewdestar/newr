// Dart-side unit tests for compiler/newr_rt.dart (M5).
//
// Mirrors tests/java/RtChecks.java case for case (same rule, both
// targets) - the same 71 checks, adapted to Dart types (int is
// already 64-bit, no boxing at the helper boundaries). Run with
// `dart RtChecks.dart` from a temp dir holding a copy of
// newr_rt.dart, or via `python -m unittest
// tests.test_newr_rt_dart`.
//
// Every failure prints the assertion and exits non-zero. Keep the
// battery aligned with the Java file: when a rule is added here,
// add it there too (or record why not).
//
// Dart host differences from Java (pinned here, not bugs):
// - A missing file raises with code "FileSystemException" (Dart's
//   host exception name; Java says "NoSuchFileException").
// - asError wraps a host error with its toString() as the message
//   (Dart errors have no getMessage(); e.g. StateError('nope')
//   becomes "Bad state: nope") and its runtime type as the code.
// - roundHalfAway / roundHalfAwayN are two methods (Dart has no
//   overloading); dictOf / structHash take a List (no varargs);
//   print takes a List; range takes optional positional args.

import 'dart:io';

import 'newr_rt.dart';

int passed = 0;

void check(String name, void Function() fn) {
  fn();
  passed++;
  print('ok $name');
}

/// Deep equality via NewrRt.eq (Dart List/Map/Set `==` is
/// identity, so a plain `==` would fail the collection cases).
/// NaN is never eq'd through here - the checks use isNaN.
void eq(Object? got, Object? want, String what) {
  if (!NewrRt.eq(got, want)) {
    throw AssertionError(
        '$what: expected <$want> '
        '(${want == null ? 'null' : want.runtimeType}), '
        'got <$got>'
        '${got == null ? '' : ' (${got.runtimeType})'}');
  }
}

void ok(bool cond, String what) {
  if (!cond) {
    throw AssertionError('expected true: $what');
  }
}

/// Expect a throw whose NewrError code equals `code` (null allowed).
NewrError throwsCode(void Function() fn, String? code) {
  try {
    fn();
  } on NewrError catch (e) {
    eq(e.code, code, 'error code');
    return e;
  } catch (e) {
    throw AssertionError(
        'expected NewrError code $code, got ${e.runtimeType}: $e');
  }
  throw AssertionError('expected code $code, nothing thrown');
}

/// Expect a throw of the named checked-int error class, code null.
NewrError throwsNamed(void Function() fn, String name) {
  try {
    fn();
  } on NewrError catch (e) {
    eq(e.runtimeType.toString(), name, 'error class');
    eq(e.code, null, 'error code (must be null)');
    return e;
  } catch (e) {
    throw AssertionError(
        'expected $name, got ${e.runtimeType}: $e');
  }
  throw AssertionError('expected $name, nothing thrown');
}

class _PointLike {
  @override
  String toString() => 'Point(x: 1, y: 2)';
}

void main() {
  // ---------------------------------------------------------------- constants
  check('int64 bounds', () {
    eq(NewrRt.int64Min, -9223372036854775808, 'int64Min');
    eq(NewrRt.int64Max, 9223372036854775807, 'int64Max');
  });

  // ---------------------------------------------------------------- checked arithmetic
  check('add/sub/mul ok', () {
    eq(NewrRt.add(2, 3), 5, 'add');
    eq(NewrRt.sub(10, 4), 6, 'sub');
    eq(NewrRt.mul(3, 4), 12, 'mul');
  });
  check('add overflow', () {
    throwsNamed(() => NewrRt.add(NewrRt.int64Max, 1),
        'NewrOverflowError');
  });
  check('sub underflow', () {
    throwsNamed(() => NewrRt.sub(NewrRt.int64Min, 1),
        'NewrOverflowError');
  });
  check('mul overflow', () {
    throwsNamed(() => NewrRt.mul(NewrRt.int64Max, 2),
        'NewrOverflowError');
  });
  check('neg', () {
    eq(NewrRt.neg(5), -5, 'neg 5');
    eq(NewrRt.neg(-5), 5, 'neg -5');
    throwsNamed(() => NewrRt.neg(NewrRt.int64Min), 'NewrOverflowError');
  });
  check('checkInt', () {
    eq(NewrRt.checkInt(0), 0, 'checkInt 0');
    throwsNamed(() => NewrRt.checkInt(BigInt.one << 63),
        'NewrOverflowError');
  });
  check('overflow error carries message', () {
    final e = throwsNamed(() => NewrRt.add(NewrRt.int64Max, 1),
        'NewrOverflowError');
    ok(e.message.contains('9223372036854775808'),
        'message mentions value');
    eq(NewrRt.asError(e), e, 'asError passes NewrError through');
  });

  // ---------------------------------------------------------------- tdiv/tmod
  check('truncating division', () {
    eq(NewrRt.tdiv(7, 2), 3, '7/2');
    eq(NewrRt.tdiv(-7, 2), -3, '-7/2');
    eq(NewrRt.tdiv(7, -2), -3, '7/-2');
    eq(NewrRt.tdiv(-7, -2), 3, '-7/-2');
  });
  check('dividend-sign modulo', () {
    eq(NewrRt.tmod(7, 3), 1, '7%3');
    eq(NewrRt.tmod(-7, 3), -1, '-7%3');
    eq(NewrRt.tmod(7, -3), 1, '7%-3');
    eq(NewrRt.tmod(-7, -3), -1, '-7%-3');
  });
  check('division by zero', () {
    throwsNamed(() => NewrRt.tdiv(1, 0), 'NewrDivisionByZeroError');
    throwsNamed(() => NewrRt.tmod(1, 0), 'NewrDivisionByZeroError');
  });
  check('min div minus one', () {
    throwsNamed(() => NewrRt.tdiv(NewrRt.int64Min, -1),
        'NewrOverflowError');
    eq(NewrRt.tmod(NewrRt.int64Min, -1), 0, 'MIN%-1');
  });
  check('div/mod invariant', () {
    final cases = [
      [7, 3],
      [-7, 3],
      [7, -3],
      [-7, -3],
      [100, 7],
      [-100, 7],
    ];
    for (final c in cases) {
      eq(NewrRt.tdiv(c[0], c[1]) * c[1] + NewrRt.tmod(c[0], c[1]),
          c[0], 'invariant ${c[0]}/${c[1]}');
    }
  });

  // ---------------------------------------------------------------- pow
  check('powInt ok', () {
    eq(NewrRt.powInt(2, 10), 1024, '2^10');
    eq(NewrRt.powInt(2, 0), 1, '2^0');
    eq(NewrRt.powInt(0, 0), 1, '0^0');
    eq(NewrRt.powInt(0, 5), 0, '0^5');
    eq(NewrRt.powInt(-2, 3), -8, '(-2)^3');
    eq(NewrRt.powInt(-2, 2), 4, '(-2)^2');
  });
  check('powInt overflow', () {
    throwsNamed(() => NewrRt.powInt(2, 63), 'NewrOverflowError');
  });
  check('powInt negative exponent', () {
    throwsCode(() => NewrRt.powInt(2, -1), null);
  });
  check('powInt huge exponent does not hang', () {
    final t0 = DateTime.now().millisecondsSinceEpoch;
    throwsNamed(() => NewrRt.powInt(10, 1000000000),
        'NewrOverflowError');
    ok(DateTime.now().millisecondsSinceEpoch - t0 < 5000,
        'powInt(10, 1e9) took too long');
  });
  check('powFloat plain', () {
    eq(NewrRt.powFloat(2.0, 3.0), 8.0, '2.0^3');
    eq(NewrRt.powFloat(9.0, 0.5), 3.0, '9.0^0.5');
  });
  check('powFloat negative base fractional exponent is NaN', () {
    ok(NewrRt.powFloat(-8.0, 1.0 / 3.0).isNaN, '(-8)^(1/3) is NaN');
  });
  check('powFloat overflow is infinity', () {
    eq(NewrRt.powFloat(1e308, 2.0), double.infinity, '1e308^2');
    eq(NewrRt.powFloat(-1e308, 3.0), double.negativeInfinity,
        '(-1e308)^3');
  });
  check('powFloat zero base negative exponent throws', () {
    throwsCode(() => NewrRt.powFloat(0, -1), 'ZeroDivisionError');
  });
  check('fmod dividend sign', () {
    eq(NewrRt.fmod(-7.0, 2.0), -1.0, 'fmod(-7,2)');
    eq(NewrRt.fmod(7.0, -2.0), 1.0, 'fmod(7,-2)');
  });
  check('fmod by zero', () {
    throwsNamed(() => NewrRt.fmod(1.0, 0.0), 'NewrDivisionByZeroError');
  });
  check('fdiv', () {
    eq(NewrRt.fdiv(7.0, 2.0), 3.5, '7.0/2.0');
    eq(NewrRt.fdiv(-7.0, 2.0), -3.5, '-7.0/2.0');
    eq(NewrRt.fdiv(1.0, 3.0), 1.0 / 3.0, '1.0/3.0');
  });
  check('fdiv by zero', () {
    throwsCode(() => NewrRt.fdiv(1.0, 0.0), 'ZeroDivisionError');
    throwsCode(() => NewrRt.fdiv(0.0, 0.0), 'ZeroDivisionError');
    throwsCode(() => NewrRt.fdiv(1.0, -0.0), 'ZeroDivisionError');
  });

  // ---------------------------------------------------------------- round (exact value)
  check('round ties go away from zero', () {
    eq(NewrRt.roundHalfAway(2.5), 3, 'round 2.5');
    eq(NewrRt.roundHalfAway(-2.5), -3, 'round -2.5');
    eq(NewrRt.roundHalfAway(3.5), 4, 'round 3.5');
    eq(NewrRt.roundHalfAway(-3.5), -4, 'round -3.5');
  });
  check('round plain values', () {
    eq(NewrRt.roundHalfAway(2.4), 2, 'round 2.4');
    eq(NewrRt.roundHalfAway(2.6), 3, 'round 2.6');
    eq(NewrRt.roundHalfAway(0.5), 1, 'round 0.5');
  });
  check('round rounds the exact float value', () {
    eq(NewrRt.roundHalfAwayN(2.675, 2), 2.67, 'round 2.675,2');
    eq(NewrRt.roundHalfAwayN(1.005, 2), 1.0, 'round 1.005,2');
    eq(NewrRt.roundHalfAwayN(0.125, 2), 0.13, 'round 0.125,2');
    eq(NewrRt.roundHalfAway(9223372036854774784.0),
        9223372036854774784, 'round huge integral float');
  });
  check('round ndigits returns float', () {
    eq(NewrRt.roundHalfAwayN(123.456, 1), 123.5, 'round 123.456,1');
    eq(NewrRt.roundHalfAwayN(999.999, 2), 1000.0, 'round 999.999,2');
  });
  check('round negative ndigits', () {
    eq(NewrRt.roundHalfAwayN(5, -1), 10.0, 'round 5,-1');
    eq(NewrRt.roundHalfAwayN(15, -1), 20.0, 'round 15,-1');
    eq(NewrRt.roundHalfAwayN(1250, -2), 1300.0, 'round 1250,-2');
  });
  check('round overflow', () {
    throwsNamed(() => NewrRt.roundHalfAway(1e30), 'NewrOverflowError');
  });
  check('round non-finite', () {
    throwsCode(() => NewrRt.roundHalfAway(double.infinity),
        'OverflowError');
    eq(NewrRt.roundHalfAwayN(double.infinity, 2), double.infinity,
        'round inf,2');
    ok(NewrRt.roundHalfAwayN(double.nan, 2).isNaN, 'round nan,2');
  });
  check('round negative zero', () {
    // Dart has no raw-bits accessor: a negative zero is told by
    // its reciprocal being -infinity.
    ok((1 / NewrRt.roundHalfAwayN(-0.001, 2)) ==
        double.negativeInfinity, 'round -0.001,2 is -0.0');
  });

  // ---------------------------------------------------------------- float rendering (J4)
  check('floatStr matches Python repr', () {
    final cases = <(double, String)>[
      (0.0, '0.0'),
      (-0.0, '-0.0'),
      (1.0, '1.0'),
      (-1.5, '-1.5'),
      (1e15, '1000000000000000.0'),
      (1e16, '1e+16'),
      (1e20, '1e+20'),
      (1e21, '1e+21'),
      (0.0001, '0.0001'),
      (0.00001, '1e-05'),
      (1.23456789e-10, '1.23456789e-10'),
      (123456.789, '123456.789'),
      (2.675, '2.675'),
      (0.1, '0.1'),
      (0.30000000000000004, '0.30000000000000004'),
      (1e-7, '1e-07'),
      (5e-324, '5e-324'),
      (1.7976931348623157e308, '1.7976931348623157e+308'),
      (3.141592653589793, '3.141592653589793'),
      (100.0, '100.0'),
      (2.5, '2.5'),
      (-2.5e-5, '-2.5e-05'),
      (123456789.12345679, '123456789.12345679'),
      (9.223372036854776e18, '9.223372036854776e+18'),
      (1234560000.0, '1234560000.0'),
    ];
    for (final c in cases) {
      eq(NewrRt.floatStr(c.$1), c.$2, 'floatStr(${c.$1})');
    }
    eq(NewrRt.floatStr(double.nan), 'nan', 'floatStr nan');
    eq(NewrRt.floatStr(double.infinity), 'inf', 'floatStr inf');
    eq(NewrRt.floatStr(double.negativeInfinity), '-inf',
        'floatStr -inf');
  });

  // ---------------------------------------------------------------- conversions
  check('toInt from str', () {
    eq(NewrRt.toInt('42'), 42, 'toInt 42');
    eq(NewrRt.toInt('  -42 '), -42, 'toInt -42');
    eq(NewrRt.toInt('+7'), 7, 'toInt +7');
    throwsCode(() => NewrRt.toInt('3.5'), 'ValueError');
    throwsCode(() => NewrRt.toInt('abc'), 'ValueError');
  });
  check('toInt range checked', () {
    throwsNamed(() => NewrRt.toInt('99999999999999999999'),
        'NewrOverflowError');
  });
  check('toInt from float truncates', () {
    eq(NewrRt.toInt(3.7), 3, 'toInt 3.7');
    eq(NewrRt.toInt(-3.7), -3, 'toInt -3.7');
    throwsCode(() => NewrRt.toInt(double.infinity), 'OverflowError');
  });
  check('toInt from bool', () {
    eq(NewrRt.toInt(true), 1, 'toInt true');
    eq(NewrRt.toInt(false), 0, 'toInt false');
  });
  check('toFloat', () {
    eq(NewrRt.toFloat(2), 2.0, 'toFloat 2');
    eq(NewrRt.toFloat('inf'), double.infinity, 'toFloat inf');
    eq(NewrRt.toFloat('-inf'), double.negativeInfinity, 'toFloat -inf');
    ok(NewrRt.toFloat('nan').isNaN, 'toFloat nan');
    eq(NewrRt.toFloat('1_000'), 1000.0, 'toFloat 1_000');
    throwsCode(() => NewrRt.toFloat('abc'), 'ValueError');
    throwsCode(() => NewrRt.toFloat(BigInt.from(10).pow(400)),
        'OverflowError');
  });
  check('toBool parses', () {
    eq(NewrRt.toBool('true'), true, 'toBool true');
    eq(NewrRt.toBool('false'), false, 'toBool false');
    throwsCode(() => NewrRt.toBool('yes'), 'ValueError');
    eq(NewrRt.toBool(0), false, 'toBool 0');
    eq(NewrRt.toBool(1), true, 'toBool 1');
    eq(NewrRt.toBool(0.0), false, 'toBool 0.0');
  });

  // ---------------------------------------------------------------- toStr
  check('toStr scalars', () {
    eq(NewrRt.toStr(null), 'none', 'toStr null');
    eq(NewrRt.toStr(true), 'true', 'toStr true');
    eq(NewrRt.toStr(false), 'false', 'toStr false');
    eq(NewrRt.toStr(42), '42', 'toStr 42');
    eq(NewrRt.toStr(1.5), '1.5', 'toStr 1.5');
    eq(NewrRt.toStr(1.0), '1.0', 'toStr 1.0');
    eq(NewrRt.toStr('a'), 'a', 'toStr a');
  });
  check('toStr collections', () {
    eq(NewrRt.toStr([1, 2, 3]), '[1, 2, 3]', 'toStr list');
    eq(NewrRt.toStr([true, false]), '[true, false]', 'toStr bools');
    final m = <String, int>{'k': 1, 'j': 2};
    eq(NewrRt.toStr(m), '{k: 1, j: 2}', 'toStr dict');
    eq(NewrRt.toStr({'a'}), '{a}', 'toStr set');
    eq(NewrRt.toStr([]), '[]', 'toStr empty list');
  });
  check('toStr error and result', () {
    eq(NewrRt.toStr(NewrError('boom')), 'boom', 'toStr error');
    eq(NewrRt.toStr(NewrRt.ok(3)), 'ok(3)', 'toStr ok');
    eq(NewrRt.toStr(NewrRt.err(NewrError('bad'))), 'err(bad)',
        'toStr err');
  });
  check('toStr struct-like values', () {
    eq(NewrRt.toStr(_PointLike()), 'Point(x: 1, y: 2)',
        'toStr struct-like');
  });

  // ---------------------------------------------------------------- Result
  check('Result ok/err', () {
    final r = NewrRt.ok(7);
    eq(r.isOk(), true, 'ok isOk');
    eq(r.isErr(), false, 'ok isErr');
    eq(r.unwrap(), 7, 'ok unwrap');
    eq(r.unwrapOr(0), 7, 'ok unwrapOr');
    final e = NewrRt.err<int>(NewrError('bad'));
    eq(e.isErr(), true, 'err isErr');
    eq(e.unwrapOr(9), 9, 'err unwrapOr');
    throwsCode(() => e.unwrap(), null);
  });
  check('tryResult', () {
    final r1 = NewrRt.tryResult(() => 6);
    eq(r1.isOk(), true, 'try ok');
    eq(r1.unwrap(), 6, 'try unwrap');
    final r2 = NewrRt.tryResult<int>(() {
      throw StateError('x');
    });
    eq(r2.isErr(), true, 'try err');
    // Dart's StateError stringifies as "Bad state: x" (its
    // toString, not a bare message - the Dart host form).
    eq(r2.error!.message, 'Bad state: x', 'try err message');
  });

  // ---------------------------------------------------------------- equality
  check('eq primitives and collections', () {
    eq(NewrRt.eq(1, 1), true, 'eq 1,1');
    eq(NewrRt.eq(1, 2), false, 'eq 1,2');
    // Dart has one int type, so the Java Long/Integer case is
    // replaced by the int/float case: 1 != 1.0.
    eq(NewrRt.eq(1, 1.0), false, 'eq int,double');
    eq(NewrRt.eq('a', 'a'), true, 'eq str');
    eq(NewrRt.eq(null, null), true, 'eq null,null');
    eq(NewrRt.eq(null, 0), false, 'eq null,0');
    eq(NewrRt.eq([1, 2], [1, 2]), true, 'eq lists');
    eq(NewrRt.eq([1], [1, 2]), false, 'eq lists len');
    eq(NewrRt.eq({'a': 1}, {'a': 1}), true, 'eq dicts');
    eq(NewrRt.eq({'a': 1}, {'a': 2}), false, 'eq dicts differ');
    eq(NewrRt.eq({1, 2}, {2, 1}), true, 'eq sets');
    eq(NewrRt.eq(NewrRt.ok(1), NewrRt.ok(1)), true, 'eq ok');
    eq(NewrRt.eq(NewrRt.ok(1), NewrRt.err(NewrError('x'))), false,
        'eq ok vs err');
    eq(NewrRt.eq(double.nan, double.nan), false, 'eq NaN');
  });

  // ---------------------------------------------------------------- ?. / ?? / accessors
  check('optChain and coalesce', () {
    eq(NewrRt.optChain<int, int>(null, (v) => v + 1), null,
        'optChain null');
    eq(NewrRt.optChain<int, int>(5, (v) => v + 1), 6, 'optChain 5');
    eq(NewrRt.coalesce<int>(0, () => 5), 0, 'coalesce 0');
    eq(NewrRt.coalesce<int>(null, () => 5), 5, 'coalesce null');
    eq(NewrRt.coalesce<String>('', () => 'd'), '', 'coalesce empty str');
  });
  check('listIndex', () {
    eq(NewrRt.listIndex<int>([1, 2], 1), 2, 'listIndex 1');
    throwsCode(() => NewrRt.listIndex<int>([1], -1), 'IndexError');
    throwsCode(() => NewrRt.listIndex<int>([1], 1), 'IndexError');
  });
  check('listGet', () {
    eq(NewrRt.listGet<int>([1, 2], 0, 9), 1, 'listGet 0');
    eq(NewrRt.listGet<int>([1, 2], 5, 9), 9, 'listGet 5');
    eq(NewrRt.listGet<int>([1, 2], -1, 9), 9, 'listGet -1');
  });
  check('listContains structural', () {
    eq(NewrRt.listContains([1, 2], 2), true, 'contains 2');
    eq(NewrRt.listContains([1, 2], 9), false, 'contains 9');
    eq(NewrRt.listContains([
      [1]
    ], [
      1
    ]), true, 'contains nested');
  });
  check('setRemove', () {
    final s = <int>{1, 2};
    NewrRt.setRemove(s, 1);
    eq(s.contains(1), false, 'setRemove removed');
    throwsCode(() => NewrRt.setRemove(s, 99), 'KeyError');
  });
  check('unwrapOpt', () {
    eq(NewrRt.unwrapOpt<int>(5), 5, 'unwrapOpt 5');
    throwsCode(() => NewrRt.unwrapOpt<int>(null), null);
  });
  check('range', () {
    eq(NewrRt.range(3), [0, 1, 2], 'range 3');
    eq(NewrRt.range(1, 4), [1, 2, 3], 'range 1,4');
    eq(NewrRt.range(0, 10, 3), [0, 3, 6, 9], 'range 0,10,3');
    eq(NewrRt.range(5, 0, -2), [5, 3, 1], 'range 5,0,-2');
    throwsCode(() => NewrRt.range(0, 5, 0), 'ValueError');
  });

  // ---------------------------------------------------------------- file ops
  check('file read/write/append', () {
    final d = Directory.systemTemp.createTempSync('newr-rt-');
    try {
      final p = '${d.path}/t.txt';
      NewrRt.fileWrite(p, 'hello');
      eq(NewrRt.fileExists(p), true, 'file exists');
      eq(NewrRt.fileRead(p), 'hello', 'file read');
      NewrRt.fileAppend(p, ' world');
      eq(NewrRt.fileRead(p), 'hello world', 'file append');
      eq(NewrRt.fileExists('${d.path}/nope'), false, 'file missing');
    } finally {
      d.deleteSync(recursive: true);
    }
  });
  check('file read missing raises', () {
    try {
      NewrRt.fileRead('/definitely/not/here/xyz.txt');
      throw AssertionError('expected a throw');
    } on NewrError catch (e) {
      // Host-specific code (Dart: PathNotFoundException - the
      // concrete FileSystemException subclass for a missing path;
      // Java: NoSuchFileException; Python: FileNotFoundError).
      eq(e.code, 'PathNotFoundException', 'missing file code');
    }
  });

  // ---------------------------------------------------------------- time
  check('timeFormat UTC', () {
    eq(NewrRt.timeFormat(1700000000, '%Y-%m-%d'), '2023-11-14',
        'fmt date');
    eq(NewrRt.timeFormat(0, '%H:%M:%S'), '00:00:00', 'fmt time');
    eq(NewrRt.timeFormat(1700000000, '%d/%m/%Y'), '14/11/2023',
        'fmt dmy');
    throwsCode(() => NewrRt.timeFormat(0, '%c'), null);
  });

  // ---------------------------------------------------------------- random
  check('randomInt in range', () {
    for (var i = 0; i < 50; i++) {
      final r = NewrRt.randomInt(1, 10);
      ok(r >= 1 && r <= 10, 'randomInt out of range: $r');
    }
    eq(NewrRt.randomInt(5, 5), 5, 'randomInt 5,5');
    final big = NewrRt.randomInt(0, NewrRt.int64Max);
    ok(big >= 0, 'randomInt wide range');
  });
  check('randomChoice', () {
    final xs = [1, 2, 3];
    ok(xs.contains(NewrRt.randomChoice(xs)), 'randomChoice in list');
    throwsCode(() => NewrRt.randomChoice(<int>[]), 'IndexError');
  });
  check('randomShuffle', () {
    final xs = [1, 2, 3, 4, 5];
    NewrRt.randomShuffle(xs);
    final sorted = [...xs]..sort();
    eq(sorted, [1, 2, 3, 4, 5], 'shuffle keeps elements');
  });

  // ---------------------------------------------------------------- asError
  check('asError', () {
    final n = NewrError('x', 'E1');
    eq(NewrRt.asError(n), n, 'asError passthrough');
    eq(NewrRt.asError('boom').message, 'boom', 'asError string');
    final wrapped = NewrRt.asError(StateError('nope'));
    // Dart host form: StateError stringifies as "Bad state: nope".
    eq(wrapped.message, 'Bad state: nope', 'asError message');
    eq(wrapped.code, 'StateError', 'asError code');
    eq(NewrRt.asError(42).message, '42', 'asError int');
  });

  // ---------------------------------------------------------------- strings
  check('chars code points', () {
    eq(NewrRt.chars('a😀b'), ['a', '😀', 'b'], 'chars astral');
    eq(NewrRt.chars(''), [], 'chars empty');
    eq(NewrRt.chars('abc'), ['a', 'b', 'c'], 'chars ascii');
  });
  check('splitStr literal separator', () {
    eq(NewrRt.splitStr('a,b,c', ','), ['a', 'b', 'c'], 'split basic');
    eq(NewrRt.splitStr('a,,b', ','), ['a', '', 'b'],
        'split empty middle');
    eq(NewrRt.splitStr('a,', ','), ['a', ''], 'split trailing empty kept');
    eq(NewrRt.splitStr('a.b', '.'), ['a', 'b'],
        'split literal dot, not regex');
    eq(NewrRt.splitStr('abc', ','), ['abc'], 'split no match');
  });
  check('strIndex code points', () {
    eq(NewrRt.strIndex('hello', 1), 'e', 'strIndex 1');
    eq(NewrRt.strIndex('hello', -1), 'o', 'strIndex -1');
    eq(NewrRt.strIndex('a😀b', 1), '😀', 'strIndex astral');
    throwsCode(() => NewrRt.strIndex('ab', 2), 'IndexError');
    throwsCode(() => NewrRt.strIndex('ab', -3), 'IndexError');
  });
  check('absLong', () {
    eq(NewrRt.absLong(5), 5, 'abs 5');
    eq(NewrRt.absLong(-5), 5, 'abs -5');
    eq(NewrRt.absLong(0), 0, 'abs 0');
    throwsCode(() => NewrRt.absLong(NewrRt.int64Min), null);
  });
  check('lenOf', () {
    eq(NewrRt.lenOf('a😀b'), 3, 'len str code points');
    eq(NewrRt.lenOf([1, 2]), 2, 'len list');
    eq(NewrRt.lenOf({'a': 1}), 1, 'len dict');
    eq(NewrRt.lenOf({1, 2, 3}), 3, 'len set');
  });
  check('strCmp', () {
    eq(NewrRt.strCmp('😀', '�') > 0, true, 'strCmp astral gt');
    eq(NewrRt.strCmp('�', '😀') < 0, true, 'strCmp astral lt');
    eq(NewrRt.strCmp('a', 'a'), 0, 'strCmp equal');
    eq(NewrRt.strCmp('ab', 'a') > 0, true, 'strCmp prefix longer');
    eq(NewrRt.strCmp('a', 'ab') < 0, true, 'strCmp prefix shorter');
    eq(NewrRt.strCmp('', ''), 0, 'strCmp empty');
  });
  check('dictOf', () {
    final d = NewrRt.dictOf<int>(['a', 1, 'b', 2]);
    eq(d['a'], 1, 'dictOf a');
    eq(d.length, 2, 'dictOf size');
    eq(d.keys.toList(), ['a', 'b'], 'dictOf insertion order');
    final withNull = NewrRt.dictOf<int?>(['a', null]);
    ok(withNull['a'] == null, 'dictOf null value allowed');
  });
  check('envGet', () {
    eq(NewrRt.envGet('NEWR_RT_CHECK_ABSENT_XYZ'), null, 'envGet absent');
    eq(NewrRt.envGet('NEWR_RT_CHECK_ABSENT_XYZ', 'dflt'), 'dflt',
        'envGet default');
    ok(NewrRt.envGet('PATH') != null, 'envGet PATH present');
  });
  check('envArgs', () {
    eq(NewrRt.envArgs(), [], 'envArgs default empty');
    // setMainArgs prepends the program name (argv[0]).
    NewrRt.setMainArgs('a', ['b']);
    eq(NewrRt.envArgs(), ['a', 'b'], 'envArgs set');
    NewrRt.setMainArgs('prog', []);
    eq(NewrRt.envArgs(), ['prog'], 'envArgs reset');
  });
  check('hashOf', () {
    eq(NewrRt.hashOf(0.0), NewrRt.hashOf(-0.0), 'hashOf -0.0 == 0.0');
    eq(NewrRt.hashOf(null), 0, 'hashOf null');
    eq(NewrRt.hashOf('x'), NewrRt.hashOf('x'), 'hashOf string stable');
    eq(NewrRt.hashOf([1, 'a']), NewrRt.hashOf([1, 'a']),
        'hashOf list by elements');
  });
  check('structHash', () {
    eq(NewrRt.structHash([1, 'a']), NewrRt.structHash([1, 'a']),
        'structHash stable');
    eq(NewrRt.structHash([-0.0]), NewrRt.structHash([0.0]),
        'structHash -0.0 == 0.0');
  });

  print('\nRtChecks.dart: $passed checks passed');
}
