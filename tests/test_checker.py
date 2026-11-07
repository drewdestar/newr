"""Tests for the core checker (SPEC.md SS5.1, SS7, SS9, SS10, SS16).

TDD: these tests were written FIRST (RED) against the checker API in
``compiler/checker.py``: ``check_program(program, providers=None)``.
They go through ``resolve_source`` (so names/scopes are already
resolved). The ``check()`` helper returns the COMBINED resolver +
checker diagnostics (M-04): a checker-only verdict is not a clean
program, and a test that ignored the resolver was false-green.
"""

import tempfile
import unittest
from pathlib import Path

from compiler.checker import check_program
from compiler.discord_check import discord_providers
from compiler.errors import line_col
from compiler.parser import parse
from compiler.errors import NewrError
from compiler.resolver import resolve_file, resolve_source


def check(src):
    """Parse + resolve + core-check a source string; return the result.

    The returned diagnostics COMBINE the resolver and the checker
    (M-04): a helper that silently dropped resolver diagnostics made
    the `ensures result` test false-green - the checker said "ok"
    while the resolver had already failed the program. Any test that
    asserts a program is clean through this helper now asserts the
    whole front end, not just the checker.
    """
    resolved = resolve_source(src)
    result = check_program(resolved.program,
                           providers=discord_providers(resolved.program))
    result.diagnostics = list(resolved.diagnostics) + result.diagnostics
    return result


def check_file(path):
    """Resolve + core-check the file at ``path``; return the result.

    Resolver diagnostics are included, same as check() (M-04).
    """
    resolved = resolve_file(path)
    result = check_program(resolved.program,
                           providers=discord_providers(resolved.program))
    result.diagnostics = list(resolved.diagnostics) + result.diagnostics
    return result


def codes(result):
    return [d.code for d in result.diagnostics]


def errs(result):
    return result.errors


class TestNoCoercion(unittest.TestCase):
    def test_float_annotation_from_int_is_E2001(self):
        r = check('let x: float = 5\nprint x\n')
        self.assertIn("E2001", codes(r))
        d = [d for d in r.errors if d.code == "E2001"][0]
        self.assertEqual(d.expected, "float")
        self.assertEqual(d.found, "int")

    def test_int_plus_float_is_E2001_with_conversion_help(self):
        r = check('let x = 1 + 2.5\nprint x\n')
        self.assertIn("E2001", codes(r))
        d = r.errors[0]
        self.assertIn("float(", d.help or "")

    def test_int_eq_float_is_E2001(self):
        r = check('let b = 1 == 1.0\nprint b\n')
        self.assertIn("E2001", codes(r))

    def test_int_division_is_ok_and_float_division_is_ok(self):
        r = check('let a = 7 / 2\nlet b = 7.0 / 2.0\nprint a\nprint b\n')
        self.assertEqual(errs(r), [])

    def test_explicit_float_conversion_is_ok(self):
        r = check('let x: float = float(5)\nprint x\n')
        self.assertEqual(errs(r), [])


class TestConditionsAndReturns(unittest.TestCase):
    def test_non_bool_if_condition_is_E2004(self):
        r = check('if "yes":\n  print "x"\n')
        self.assertEqual(codes(r), ["E2004"])

    def test_non_bool_while_condition_is_E2004(self):
        r = check('while 1:\n  print "x"\n')
        self.assertEqual(codes(r), ["E2004"])

    def test_return_type_mismatch_is_E2001(self):
        r = check('fn f() -> int:\n  return "nope"\n')
        self.assertIn("E2001", codes(r))
        d = r.errors[0]
        self.assertEqual(d.expected, "int")
        self.assertEqual(d.found, "str")

    def test_missing_return_is_an_error(self):
        r = check('fn f() -> int:\n  let x = 1\n  print x\n')
        self.assertIn("E2001", codes(r))
        self.assertIn("f", r.errors[0].message)

    def test_return_value_at_top_level_is_E2007(self):
        r = check('return 1\n')
        self.assertEqual(codes(r), ["E2007"])

    def test_bare_return_at_top_level_is_allowed(self):
        r = check('print "a"\nreturn\nprint "b"\n')
        self.assertEqual(errs(r), [])

    def test_break_outside_loop_is_E2007(self):
        r = check('break\n')
        self.assertEqual(codes(r), ["E2007"])

    def test_continue_outside_loop_is_E2007(self):
        r = check('fn f() -> int:\n  continue\n  return 1\n')
        # The `return` after the `continue` is also unreachable (W1003);
        # the E2007 is the error under test.
        self.assertIn("E2007", codes(r))

    def test_break_inside_loop_is_ok(self):
        r = check('while true:\n  break\n')
        self.assertEqual(errs(r), [])


class TestCollections(unittest.TestCase):
    def test_empty_list_without_annotation_is_E2002(self):
        r = check('let xs = []\nprint xs\n')
        self.assertIn("E2002", codes(r))

    def test_empty_list_with_annotation_is_ok(self):
        r = check('let xs: list[int] = []\nprint xs\n')
        self.assertEqual(errs(r), [])

    def test_heterogeneous_list_is_E2003(self):
        r = check('let xs = [1, "a"]\nprint xs\n')
        self.assertIn("E2003", codes(r))

    def test_heterogeneous_dict_is_E2003(self):
        r = check('let d = {"a": 1, "b": "x"}\nprint d\n')
        self.assertIn("E2003", codes(r))

    def test_dict_with_non_str_key_is_E2001(self):
        r = check('let d = {1: "x"}\nprint d\n')
        self.assertIn("E2001", codes(r))

    def test_list_int_is_not_list_float(self):
        r = check('let xs: list[float] = [1, 2]\nprint xs\n')
        self.assertIn("E2001", codes(r))

    def test_string_index_is_E2001_with_chars_help(self):
        r = check('let s = "abc"\nlet c = s[0]\nprint c\n')
        self.assertIn("E2001", codes(r))
        self.assertIn("chars", r.errors[0].help or "")

    def test_set_index_is_E2001(self):
        r = check('let xs = set([1, 2])\nlet y = xs[0]\nprint y\n')
        self.assertIn("E2001", codes(r))

    def test_list_index_types(self):
        r = check('let xs = [10, 20]\nlet y: int = xs[0]\nprint y\n')
        self.assertEqual(errs(r), [])

    def test_dict_index_types(self):
        r = check('let d = {"a": 1}\nlet y: int = d["a"]\nprint y\n')
        self.assertEqual(errs(r), [])


class TestCalls(unittest.TestCase):
    def test_wrong_arity_is_E3002(self):
        r = check('fn f(a: int) -> int:\n  return a\nlet y = f(1, 2)\nprint y\n')
        self.assertIn("E3002", codes(r))

    def test_arg_type_mismatch_is_E2001(self):
        r = check('fn f(a: int) -> int:\n  return a\nlet y = f("x")\nprint y\n')
        self.assertIn("E2001", codes(r))

    def test_unknown_kwarg_is_E3001(self):
        r = check('fn f(a: int) -> int:\n  return a\nlet y = f(b: 1)\nprint y\n')
        self.assertIn("E3001", codes(r))

    def test_calling_a_non_function_is_E2001(self):
        r = check('let x = 1\nlet y = x()\nprint y\n')
        self.assertIn("E2001", codes(r))

    def test_unknown_type_annotation_is_E3001(self):
        r = check('let x: Nosuch = 1\nprint x\n')
        self.assertIn("E3001", codes(r))
        self.assertIn("Nosuch", r.errors[0].message)

    def test_len_on_int_is_E2001(self):
        r = check('let n = len(1)\nprint n\n')
        self.assertIn("E2001", codes(r))

    def test_math_sqrt_needs_float(self):
        r = check('import math\nlet x = math.sqrt(4)\nprint x\n')
        self.assertIn("E2001", codes(r))
        r2 = check('import math\nlet x = math.sqrt(4.0)\nprint x\n')
        self.assertEqual(errs(r2), [])

    def test_unknown_module_member_is_E3001_with_help(self):
        r = check('import math\nlet x = math.sqrrt(4.0)\nprint x\n')
        self.assertIn("E3001", codes(r))
        self.assertIn("sqrt", r.errors[0].help or "")

    def test_print_is_variadic(self):
        r = check('print 1, "two", 3.0, true\n')
        self.assertEqual(errs(r), [])


class TestStructsAndEnums(unittest.TestCase):
    def test_struct_field_access_ok(self):
        r = check('struct Point:\n  x: int\n  y: int\n'
                  'let p = Point(x: 1, y: 2)\nlet n: int = p.x\nprint n\n')
        self.assertEqual(errs(r), [])

    def test_unknown_struct_field_is_E3001_with_similar_help(self):
        # `pos_z` is >= 0.7 similar to `pos_x` / `pos_y` (the SPEC SS16
        # gate), so the rustc named-form help names them.
        r = check('struct Point:\n  pos_x: int\n  pos_y: int\n'
                  'let p = Point(pos_x: 1, pos_y: 2)\nlet n = p.pos_z\nprint n\n')
        self.assertIn("E3001", codes(r))
        self.assertIn("pos_x", r.errors[0].help or "")

    def test_struct_construction_missing_field_is_E3002(self):
        r = check('struct Point:\n  x: int\n  y: int\n'
                  'let p = Point(x: 1)\nprint p\n')
        self.assertIn("E3002", codes(r))

    def test_struct_construction_unknown_field_is_E3001(self):
        r = check('struct Point:\n  x: int\n  y: int\n'
                  'let p = Point(x: 1, y: 2, z: 3)\nprint p\n')
        self.assertIn("E3001", codes(r))

    def test_struct_field_assignment_is_a_parser_error(self):
        # Parser rejects `p.x = 5` (assignment target must be a name) --
        # the checker never sees it. Recorded here so the decision is
        # pinned: the value-type error is enforced at parse time.
        with self.assertRaises(NewrError) as ctx:
            parse('struct Point:\n  x: int\n'
                  'let p = Point(x: 1)\np.x = 5\n', "<string>")
        self.assertEqual(ctx.exception.code, "E1001")

    def test_enum_construction_ok(self):
        r = check('enum Color:\n  Red\n  Green\n'
                  'let c = Color.Red\nprint c\n')
        self.assertEqual(errs(r), [])

    def test_unknown_enum_variant_is_E3001(self):
        r = check('enum Color:\n  Red\n  Green\n'
                  'let c = Color.Reed\nprint c\n')
        self.assertIn("E3001", codes(r))
        self.assertIn("Red", r.errors[0].help or "")

    def test_enum_variant_wrong_field_type_is_E2001(self):
        r = check('enum Shape:\n  Circle(radius: float)\n'
                  'let s = Shape.Circle(radius: 1)\nprint s\n')
        self.assertIn("E2001", codes(r))

    def test_enum_variant_missing_field_is_E3002(self):
        r = check('enum Shape:\n  Circle(radius: float)\n'
                  'let s = Shape.Circle()\nprint s\n')
        self.assertIn("E3002", codes(r))

    def test_unknown_method_is_E3001_with_similar_help(self):
        r = check('let s = "abc"\nlet u = s.upperr()\nprint u\n')
        self.assertIn("E3001", codes(r))
        self.assertIn("upper", r.errors[0].help or "")

    def test_list_append_wrong_type_is_E2001(self):
        r = check('let xs: list[int] = [1]\nxs.append("a")\nprint xs\n')
        self.assertIn("E2001", codes(r))


class TestNullability(unittest.TestCase):
    def test_none_is_compatible_with_nullable(self):
        r = check('let x: int? = none\nprint x\n')
        self.assertEqual(errs(r), [])

    def test_none_is_not_compatible_with_plain_int(self):
        r = check('let x: int = none\nprint x\n')
        self.assertIn("E2001", codes(r))

    def test_plain_dot_on_nullable_is_E2001(self):
        r = check('struct Point:\n  x: int\n'
                  'let p: Point? = none\nlet n = p.x\nprint n\n')
        self.assertIn("E2001", codes(r))

    def test_optional_chain_wraps_field_type_in_nullable(self):
        # p?.x is int? -- assigning it to int? is fine, to int is not.
        r = check('struct Point:\n  x: int\n'
                  'let p: Point? = none\nlet n: int? = p?.x\nprint n\n')
        self.assertEqual(errs(r), [])
        r2 = check('struct Point:\n  x: int\n'
                   'let p: Point? = none\nlet n: int = p?.x\nprint n\n')
        self.assertIn("E2001", codes(r2))

    def test_optional_chain_method_wraps_result(self):
        r = check('import env\nlet s: str? = env.get("HOME")\n'
                  'let u: str? = s?.upper()\nprint u\n')
        self.assertEqual(errs(r), [])

    def test_coalesce_ok(self):
        r = check('import env\nlet s: str = env.get("HOME") ?? "/tmp"\nprint s\n')
        self.assertEqual(errs(r), [])

    def test_coalesce_on_non_nullable_is_E2001(self):
        r = check('let s: str = "a" ?? "b"\nprint s\n')
        self.assertIn("E2001", codes(r))

    def test_coalesce_rhs_wrong_type_is_E2001(self):
        r = check('import env\nlet s: str = env.get("HOME") ?? 5\nprint s\n')
        self.assertIn("E2001", codes(r))

    def test_nullable_or_unwraps_with_default(self):
        r = check('import env\nlet s: str = env.get("HOME") or "/tmp"\nprint s\n')
        self.assertEqual(errs(r), [])

    def test_nullable_methods(self):
        r = check('import env\nlet s: str? = env.get("HOME")\n'
                  'let b: bool = s.is_some()\nprint b\n')
        self.assertEqual(errs(r), [])

    def test_optional_nullable_methods_are_nullable(self):
        # M2 evaluation must-fix #5 (G-M2-2 / D31): `?.is_some()` /
        # `?.is_none()` / `?.unwrap()` must type as nullable - the
        # chain short-circuits on `none`.
        r = check('let v: int? = none\n'
                  'let b: bool? = v?.is_some()\n'
                  'let c: bool? = v?.is_none()\n'
                  'let n: int? = v?.unwrap()\n'
                  'print b\nprint c\nprint n\n')
        self.assertEqual(errs(r), [])

    def test_optional_nullable_methods_assigned_to_non_nullable_is_E2001(self):
        r = check('let v: int? = none\n'
                  'let b: bool = v?.is_some()\n'
                  'print b\n')
        self.assertIn("E2001", codes(r))
        r2 = check('let v: int? = none\n'
                   'let n: int = v?.unwrap()\n'
                   'print n\n')
        self.assertIn("E2001", codes(r2))

    def test_optional_unwrap_on_nullable_result_is_nullable_value(self):
        # `?.unwrap()` on a nullable Result: the `?` unwraps the
        # outer nullable, `.unwrap()` unwraps the Result inside.
        r = check('let r: Result[int, error]? = ok(5)\n'
                  'let n: int? = r?.unwrap()\n'
                  'print n\n')
        self.assertEqual(errs(r), [])

    def test_member_flag_qdot_vs_dot(self):
        # AST amendment: Member carries optional=True exactly for `?.`.
        from compiler.ast_nodes import Member
        prog = parse('let p = a?.b\nlet q = a.b\n', "<string>")
        self.assertTrue(isinstance(prog.stmts[0].value, Member))
        self.assertTrue(prog.stmts[0].value.optional)
        self.assertFalse(prog.stmts[1].value.optional)

    def test_nullable_method_as_value_is_fn_not_bool(self):
        # §15.8 hole 2: a bare nullable-method name is a FUNCTION
        # value, not its return type. `v.is_some` is `fn() -> bool`,
        # so assigning it to a bool is E2001 and calling the value
        # is fine.
        r = check('let v: int? = 5\nlet b: bool = v.is_some\nprint b\n')
        self.assertIn("E2001", codes(r))
        r = check('let v: int? = 5\nlet f = v.is_some\nprint f()\n')
        self.assertEqual(errs(r), [])
        r = check('let v: int? = 5\nlet f: fn() -> bool = v.is_some\n'
                  'print f()\n')
        self.assertEqual(errs(r), [])

    def test_optional_nullable_method_as_value_is_nullable_fn(self):
        # `v?.is_some` is a NULLABLE fn value: none when `v` is
        # none, else the method. Assigning it to a plain fn is
        # E2001, and calling it directly is E2001 (it may be none).
        r = check('let v: int? = 5\n'
                  'let f: fn() -> bool = v?.is_some\nprint f\n')
        self.assertIn("E2001", codes(r))
        r = check('let v: int? = 5\nlet f = v?.is_some\nprint f()\n')
        self.assertIn("E2001", codes(r))
        r = check('let v: int? = 5\nlet f = v?.is_some\n'
                  'let g = f.unwrap()\nprint g()\n')
        self.assertEqual(errs(r), [])

    def test_optional_chain_method_as_value_is_nullable_fn(self):
        # Same rule for an inner-type method reached through `?.`:
        # `xs?.len` is a nullable fn value, not a plain fn.
        r = check('let xs: list[int]? = [1, 2]\n'
                  'let f: fn() -> int = xs?.len\nprint f\n')
        self.assertIn("E2001", codes(r))
        r = check('let xs: list[int]? = [1, 2]\nlet f = xs?.len\n'
                  'print f()\n')
        self.assertIn("E2001", codes(r))
        r = check('let xs: list[int] = [1, 2]\nlet f = xs.len\n'
                  'print f()\n')
        self.assertEqual(errs(r), [])

    def test_optional_chain_nullable_field_type(self):
        # §15.8 hole 1, checker side: `o?.inner.name` types as
        # `str?` (the chain short-circuits on either `none`).
        r = check('struct Inner:\n  name: str\n'
                  'struct Outer:\n  inner: Inner?\n'
                  'let o: Outer? = Outer(inner: none)\n'
                  'let n: str? = o?.inner.name\nprint n\n')
        self.assertEqual(errs(r), [])
        r = check('struct Inner:\n  name: str\n'
                  'struct Outer:\n  inner: Inner?\n'
                  'let o: Outer? = Outer(inner: none)\n'
                  'let n: str = o?.inner.name\nprint n\n')
        self.assertIn("E2001", codes(r))


class TestResultAndPropagate(unittest.TestCase):
    def test_try_expr_returns_result(self):
        r = check('let r: Result[int, error] = try 5\nprint r\n')
        self.assertEqual(errs(r), [])

    def test_try_expr_wrong_annotation_is_E2001(self):
        r = check('let x: int = try 5\nprint x\n')
        self.assertIn("E2001", codes(r))

    def test_ok_builds_result(self):
        r = check('let r: Result[int, error] = ok(5)\nprint r\n')
        self.assertEqual(errs(r), [])

    def test_ok_wrong_annotation_is_E2001(self):
        r = check('let r: Result[str, error] = ok(5)\nprint r\n')
        self.assertIn("E2001", codes(r))

    def test_result_methods(self):
        r = check('let r: Result[int, error] = ok(5)\n'
                  'let b: bool = r.is_ok()\nlet v: int = r.unwrap()\n'
                  'print b\nprint v\n')
        self.assertEqual(errs(r), [])

    def test_result_unwrap_or_wrong_type_is_E2001(self):
        r = check('let r: Result[int, error] = ok(5)\n'
                  'let v = r.unwrap_or("x")\nprint v\n')
        self.assertIn("E2001", codes(r))

    def test_propagate_in_non_result_fn_is_E2001(self):
        r = check('fn g() -> Result[int, error]:\n  return ok(1)\n'
                  'fn f() -> int:\n  let v = g()?\n  return v\n')
        self.assertIn("E2001", codes(r))

    def test_propagate_in_result_fn_is_ok(self):
        r = check('fn g() -> Result[int, error]:\n  return ok(1)\n'
                  'fn f() -> Result[int, error]:\n  let v = g()?\n  return ok(v)\n')
        self.assertEqual(errs(r), [])

    def test_propagate_nullable_into_result_fn_is_E2001(self):
        # M2 evaluation must-fix #4 (G-M2-1 / D30): `?` on a nullable
        # operand inside a Result fn was accepted by the checker but
        # failed at build with E4001. The checker must E2001 it.
        r = check('fn f() -> Result[int, error]:\n'
                  '  let v: int? = none\n'
                  '  let w = v?\n'
                  '  return ok(w)\n')
        self.assertIn("E2001", codes(r))

    def test_propagate_result_into_nullable_fn_is_E2001(self):
        # The mirror image: a Result operand inside a nullable fn.
        r = check('fn f() -> int?:\n'
                  '  let v: Result[int, error] = ok(1)\n'
                  '  let w = v?\n'
                  '  return w\n')
        self.assertIn("E2001", codes(r))

    def test_propagate_nullable_result_into_result_fn_is_E2001(self):
        # A nullable Result operand fails BOTH kind rules: it is
        # nullable AND a Result.
        r = check('fn f() -> Result[int, error]:\n'
                  '  let v: Result[int, error]? = none\n'
                  '  let w = v?\n'
                  '  return ok(w)\n')
        self.assertIn("E2001", codes(r))

    def test_propagate_nullable_result_into_nullable_fn_is_E2001(self):
        r = check('fn f() -> int?:\n'
                  '  let v: Result[int, error]? = none\n'
                  '  let w = v?\n'
                  '  return w\n')
        self.assertIn("E2001", codes(r))

    def test_propagate_nullable_into_nullable_fn_is_ok(self):
        r = check('fn f() -> int?:\n'
                  '  let v: int? = none\n'
                  '  let w = v?\n'
                  '  return w\n')
        self.assertEqual(errs(r), [])

    def test_propagate_result_in_handler_is_ok(self):
        # A handler returns nothing, so `?` there just exits the
        # handler - either operand kind is allowed (matches the
        # emitter's handler branch).
        r = check('fn g() -> Result[int, error]:\n  return ok(1)\n'
                  'command ping:\n'
                  '  description: "x"\n'
                  '  do:\n'
                  '    let v = g()?\n'
                  '    print v\n')
        self.assertEqual(errs(r), [])

    def test_propagate_nullable_in_handler_is_ok(self):
        r = check('command ping:\n'
                  '  description: "x"\n'
                  '  do:\n'
                  '    let v: int? = none\n'
                  '    let w = v?\n'
                  '    print w\n')
        self.assertEqual(errs(r), [])

    def test_propagate_in_lambda_in_handler_is_E2001(self):
        # A lambda is a function boundary: `?` inside it is judged
        # against the lambda's own return type (`int`, not a
        # Result), not the enclosing handler.
        r = check('fn g() -> Result[int, error]:\n  return ok(1)\n'
                  'command ping:\n'
                  '  description: "x"\n'
                  '  do:\n'
                  '    let f = fn() -> int:\n'
                  '      return g()?\n'
                  '    print f()\n')
        self.assertIn("E2001", codes(r))


class TestMatch(unittest.TestCase):
    def test_enum_match_exhaustive_ok(self):
        r = check('enum Color:\n  Red\n  Green\n'
                  'let c = Color.Red\n'
                  'match c:\n  Color.Red:\n    print "red"\n'
                  '  Color.Green:\n    print "green"\n')
        self.assertEqual(errs(r), [])

    def test_enum_match_missing_variant_is_E2006(self):
        r = check('enum Color:\n  Red\n  Green\n'
                  'let c = Color.Red\n'
                  'match c:\n  Color.Red:\n    print "red"\n')
        self.assertIn("E2006", codes(r))
        self.assertIn("Green", r.errors[0].message)

    def test_enum_match_with_wildcard_is_ok(self):
        r = check('enum Color:\n  Red\n  Green\n'
                  'let c = Color.Red\n'
                  'match c:\n  Color.Red:\n    print "red"\n'
                  '  _:\n    print "other"\n')
        self.assertEqual(errs(r), [])

    def test_non_enum_match_without_wildcard_is_E2006(self):
        r = check('let x = 1\n'
                  'match x:\n  1:\n    print "one"\n')
        self.assertIn("E2006", codes(r))

    def test_match_literal_pattern_wrong_type_is_E2001(self):
        r = check('let x = 1\n'
                  'match x:\n  "one":\n    print "one"\n'
                  '  _:\n    print "other"\n')
        self.assertIn("E2001", codes(r))

    def test_match_guard_must_be_bool(self):
        r = check('enum Color:\n  Red\n  Green\n'
                  'let c = Color.Red\n'
                  'match c:\n  Color.Red if 1:\n    print "red"\n'
                  '  _:\n    print "other"\n')
        self.assertIn("E2004", codes(r))

    def test_match_enum_pattern_binds_fields(self):
        r = check('enum Shape:\n  Circle(radius: float)\n  Rect(width: float, height: float)\n'
                  'let s = Shape.Circle(radius: 1.5)\n'
                  'match s:\n  Shape.Circle(radius: r):\n    print r\n'
                  '  Shape.Rect(width: w, height: h):\n    print w\n')
        self.assertEqual(errs(r), [])

    def test_match_struct_pattern_unknown_field_is_E3001(self):
        r = check('struct Point:\n  x: int\n  y: int\n'
                  'let p = Point(x: 1, y: 2)\n'
                  'match p:\n  Point(x: a, z: b):\n    print a\n'
                  '  _:\n    print "other"\n')
        self.assertIn("E3001", codes(r))


class TestContracts(unittest.TestCase):
    def test_contracts_first_are_ok(self):
        r = check('fn withdraw(balance: int, amount: int) -> int:\n'
                  '  requires amount > 0\n  ensures result >= 0\n'
                  '  return balance - amount\n')
        self.assertEqual(errs(r), [])

    def test_contract_after_statement_is_E2001(self):
        r = check('fn f(x: int) -> int:\n  let y = x + 1\n'
                  '  requires x > 0\n  return y\n')
        self.assertIn("E2001", codes(r))

    def test_requires_non_bool_is_E2004(self):
        r = check('fn f(x: int) -> int:\n  requires x\n  return x\n')
        self.assertIn("E2004", codes(r))

    def test_ensures_result_has_return_type(self):
        r = check('fn f(x: int) -> int:\n  ensures result >= "x"\n  return x\n')
        self.assertIn("E2001", codes(r))


class TestWarningsAndRecovery(unittest.TestCase):
    def test_unreachable_code_is_W1003(self):
        r = check('fn f() -> int:\n  return 1\n  let x = 2\n  print x\n')
        self.assertIn("W1003", codes(r))
        self.assertEqual(errs(r), [])

    def test_always_true_condition_is_W1005(self):
        r = check('if true:\n  print "x"\n')
        self.assertIn("W1005", codes(r))
        self.assertEqual(errs(r), [])

    def test_always_false_while_is_W1005(self):
        r = check('while false:\n  print "x"\n')
        self.assertIn("W1005", codes(r))

    def test_int_division_in_float_context_warns_W1008(self):
        r = check('let x: float = float(1 / 2)\nprint x\n')
        self.assertIn("W1008", codes(r))
        self.assertEqual(errs(r), [])
        d = [d for d in r.diagnostics if d.code == "W1008"][0]
        self.assertIn("newr-specific", d.message)

    def test_int_division_assigned_to_float_warns_W1008(self):
        r = check('let x: float = 1 / 2\nprint x\n')
        self.assertIn("W1008", codes(r))

    def test_recovery_one_error_per_function_body(self):
        r = check('fn f() -> int:\n  let a: int = "x"\n  let b: int = "y"\n'
                  '  return a\n')
        self.assertEqual(len(errs(r)), 1)
        self.assertEqual(errs(r)[0].code, "E2001")

    def test_recovery_two_bodies_each_report(self):
        r = check('fn f() -> int:\n  let a: int = "x"\n  return a\n'
                  'fn g() -> int:\n  let b: int = "y"\n  return b\n')
        self.assertEqual(len(errs(r)), 2)

    def test_diagnostic_carries_file_and_line(self):
        src = 'let a = 1\nlet x: float = 5\nprint x\n'
        r = check(src)
        d = r.errors[0]
        self.assertEqual(d.span.file, "<string>")
        line, col = line_col(src, d.span.start_byte)
        self.assertEqual(line, 2)
        self.assertGreaterEqual(col, 1)

    def test_error_carries_expected_and_found(self):
        r = check('let x: int = "hello"\nprint x\n')
        d = r.errors[0]
        self.assertEqual(d.expected, "int")
        self.assertEqual(d.found, "str")
        self.assertIsNotNone(d.help)


class TestMiscRules(unittest.TestCase):
    def test_for_loop_var_typing(self):
        r = check('for x in [1, 2]:\n  let y: str = x\n  print y\n')
        self.assertIn("E2001", codes(r))
        r2 = check('for x in [1, 2]:\n  let y: int = x\n  print y\n')
        self.assertEqual(errs(r2), [])

    def test_for_over_dict_yields_str_keys(self):
        r = check('let d = {"a": 1}\nfor k in d:\n  let s: str = k\n  print s\n')
        self.assertEqual(errs(r), [])

    def test_for_over_str_yields_str(self):
        r = check('for c in "abc":\n  let s: str = c\n  print s\n')
        self.assertEqual(errs(r), [])

    def test_for_over_int_is_E2001(self):
        r = check('for x in 5:\n  print x\n')
        self.assertIn("E2001", codes(r))

    def test_reassignment_wrong_type_is_E2001(self):
        r = check('let x = 1\nx = "a"\nprint x\n')
        self.assertIn("E2001", codes(r))

    def test_augmented_assign_wrong_type_is_E2001(self):
        r = check('let x = 1\nx += "a"\nprint x\n')
        self.assertIn("E2001", codes(r))

    def test_throw_non_str_is_E2001(self):
        r = check('throw 5\n')
        self.assertIn("E2001", codes(r))

    def test_throw_str_is_ok(self):
        r = check('throw "boom"\n')
        self.assertEqual(errs(r), [])

    def test_ternary_branch_mismatch_is_E2001(self):
        r = check('let x = true ? 1 : "a"\nprint x\n')
        self.assertIn("E2001", codes(r))

    def test_ternary_ok(self):
        r = check('let x = true ? 1 : 2\nprint x\n')
        self.assertEqual(errs(r), [])

    def test_unary_minus_on_str_is_E2001(self):
        r = check('let x = -"a"\nprint x\n')
        self.assertIn("E2001", codes(r))

    def test_not_on_int_is_E2001(self):
        r = check('let b = not 1\nprint b\n')
        self.assertIn("E2001", codes(r))

    def test_in_operator(self):
        r = check('let a = 1 in [1, 2]\nlet b = "a" in "abc"\nprint a\nprint b\n')
        self.assertEqual(errs(r), [])
        r2 = check('let a = 1 in "abc"\nprint a\n')
        self.assertIn("E2001", codes(r2))

    def test_lambda_types(self):
        r = check('let f = fn(x: int) -> int: x + 1\nlet y: int = f(2)\nprint y\n')
        self.assertEqual(errs(r), [])
        r2 = check('let f = fn(x: int) -> int: x + 1\nlet y: str = f(2)\nprint y\n')
        self.assertIn("E2001", codes(r2))
        r3 = check('let f = fn(x: int) -> int: x + 1\nlet y = f("a")\nprint y\n')
        self.assertIn("E2001", codes(r3))

    def test_interpolation_of_nullable_is_E2001(self):
        r = check('import env\nlet s: str? = env.get("HOME")\nprint "{s}"\n')
        self.assertIn("E2001", codes(r))

    def test_interpolation_of_list_is_E2001(self):
        r = check('let xs = [1, 2]\nprint "{xs}"\n')
        self.assertIn("E2001", codes(r))

    def test_clean_hello_has_no_errors(self):
        r = check('print "Hello, world!"\n')
        self.assertEqual(errs(r), [])


class TestNestedImports(unittest.TestCase):
    def _write(self, d, name, text):
        path = Path(d) / name
        path.write_text(text, encoding="utf-8")
        return path

    def test_nested_import_clean_program_checks(self):
        with tempfile.TemporaryDirectory() as d:
            self._write(d, "c.newr",
                        "pub fn triple(x: int) -> int:\n"
                        "  return x * 3\n")
            self._write(d, "b.newr",
                        'import "c.newr"\n'
                        "pub fn use_it(x: int) -> int:\n"
                        "  return c.triple(x)\n")
            main = self._write(
                d, "main.newr",
                'import "b.newr"\n'
                "let y = b.use_it(2)\n"
                "print y\n")
            self.assertEqual(errs(check_file(main)), [])

    def test_nested_import_type_error_is_caught_once(self):
        with tempfile.TemporaryDirectory() as d:
            self._write(d, "c.newr",
                        "pub fn triple(x: int) -> int:\n"
                        "  return x * 3\n")
            self._write(d, "b.newr",
                        'import "c.newr"\n'
                        "pub fn use_it(x: int) -> int:\n"
                        '  return c.triple("s")\n')
            main = self._write(
                d, "main.newr",
                'import "b.newr"\n'
                "let y = b.use_it(2)\n"
                "print y\n")
            r = check_file(main)
            self.assertEqual([e.code for e in r.errors], ["E2001"])

    def test_same_file_imported_twice_checked_once(self):
        with tempfile.TemporaryDirectory() as d:
            self._write(d, "x.newr",
                        "pub fn f() -> int:\n"
                        '  let bad: int = "s"\n'
                        "  return 1\n")
            main = self._write(
                d, "main.newr",
                'import "x.newr"\n'
                'import "x.newr" as x2\n'
                "print x.f()\n"
                "print x2.f()\n")
            r = check_file(main)
            self.assertEqual([e.code for e in r.errors], ["E2001"])


class TestImportedEnumMember(unittest.TestCase):
    """§20: a qualified imported enum member (`m.Shade.Red`) is a
    value - the checker must type it from the imported module's enum
    declaration, not leave it untyped (which made the emitter fail
    with E4001 on a program `check` had accepted)."""

    HELPER = ('pub enum Shade:\n'
              '  Red\n'
              '  Blue\n'
              '\n'
              'pub fn is_red(s: Shade) -> bool:\n'
              '  return s == Shade.Red\n')

    HELPER_SHAPE = ('pub enum Shape:\n'
                    '  Circle(radius: float)\n'
                    '  Square(side: float)\n')

    def _check_pair(self, helper_src, main_src):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "helper.newr").write_text(helper_src,
                                                 encoding="utf-8")
            main = Path(d) / "main.newr"
            main.write_text(main_src, encoding="utf-8")
            return check_file(main)

    def _outer_member_type(self, result):
        """Type of the outermost Member in the main program (the
        qualified enum member itself). None means the checker left
        it untyped - the exact gap this round closes."""
        import dataclasses
        from compiler import ast_nodes as A
        found = []

        def walk(node):
            if isinstance(node, A.Member):
                found.append(node)
            if dataclasses.is_dataclass(node):
                for f in dataclasses.fields(node):
                    v = getattr(node, f.name)
                    if isinstance(v, list):
                        for item in v:
                            if dataclasses.is_dataclass(item):
                                walk(item)
                    elif dataclasses.is_dataclass(v):
                        walk(v)

        walk(result.program)
        self.assertTrue(found, "no Member node in the program")
        # The qualified enum member is the Member whose object is
        # itself a Member (`module.Enum.Variant`); a plain
        # `module.fn` callee Member is never typed and must not be
        # mistaken for it.
        target = next((m for m in found if isinstance(m.obj, A.Member)),
                      found[0])
        t = target.expr_type
        return None if t is None else t.name

    def test_bare_variant_types_clean(self):
        r = self._check_pair(
            self.HELPER, 'import "helper.newr"\nprint helper.Shade.Red\n')
        self.assertEqual([e.code for e in r.errors], [])
        self.assertEqual(self._outer_member_type(r), "Shade")

    def test_variant_as_argument_types_clean(self):
        r = self._check_pair(
            self.HELPER,
            'import "helper.newr"\n'
            'print helper.is_red(helper.Shade.Red)\n')
        self.assertEqual([e.code for e in r.errors], [])
        self.assertEqual(self._outer_member_type(r), "Shade")

    def test_aliased_import_variant_types_clean(self):
        r = self._check_pair(
            self.HELPER,
            'import "helper.newr" as h\nprint h.Shade.Red\n')
        self.assertEqual([e.code for e in r.errors], [])
        self.assertEqual(self._outer_member_type(r), "Shade")

    def test_unknown_variant_is_E3001(self):
        r = self._check_pair(
            self.HELPER, 'import "helper.newr"\nprint helper.Shade.Green\n')
        self.assertEqual([e.code for e in r.errors], ["E3001"])

    def test_fielded_variant_bare_is_E2001(self):
        r = self._check_pair(
            self.HELPER_SHAPE,
            'import "helper.newr"\nprint helper.Shape.Circle\n')
        self.assertEqual([e.code for e in r.errors], ["E2001"])

    def test_fielded_variant_call_types_clean(self):
        r = self._check_pair(
            self.HELPER_SHAPE,
            'import "helper.newr"\n'
            'print helper.Shape.Circle(radius: 1.5)\n')
        self.assertEqual([e.code for e in r.errors], [])

    def test_fielded_variant_call_wrong_field_is_E3001(self):
        # Same code as the local-enum path: _check_args reports an
        # unknown named argument as E3001 ("has no parameter").
        r = self._check_pair(
            self.HELPER_SHAPE,
            'import "helper.newr"\n'
            'print helper.Shape.Circle(width: 1.5)\n')
        self.assertIn("E3001", [e.code for e in r.errors])


if __name__ == "__main__":
    unittest.main()
