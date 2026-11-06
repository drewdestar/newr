"""Regression tests for BUG-M2-1 (found while building the M2 emitter).

The checker's `_in_top_level` flag stayed True while walking a
nested block (an `if` / `for` / `while` body at the top level), so a
`let` inside that block was treated as a top-level `let`:

* if it shadowed an already-checked top-level name, `_check_let_const`
  hit its `top_checked` early return and never checked the value at
  all — the value's `expr_type` stayed `None`, and the emitter then
  refused the program ("an expression has no type");
* if it did not shadow, the name was registered in the MODULE env
  instead of the block's local scope, so it leaked out of the block.

The fix is in `compiler/checker.py::_walk_block`: a nested block is
never top-level, so the flag is cleared while walking it.
"""

import unittest

import compiler.ast_nodes as A
from tests.test_checker import check


class TestTopLevelShadow(unittest.TestCase):
    def test_shadowed_let_inside_top_level_if_is_checked(self):
        # The inner `let x = 2` shadows the outer `let x = 1`. Its
        # value must be type-checked (expr_type set) — otherwise the
        # emitter cannot emit it.
        result = check("let x = 1\n"
                       "if true:\n"
                       "  let x = 2\n"
                       "  print x\n"
                       "print x\n")
        self.assertEqual(result.errors, [])
        inner_let = result.program.stmts[1].then_body[0]
        self.assertIsInstance(inner_let, A.Let)
        self.assertIsNotNone(inner_let.value.expr_type,
                             "the shadowed let's value was never checked")
        self.assertEqual(str(inner_let.value.expr_type), "int")

    def test_let_inside_top_level_if_does_not_leak(self):
        # `y` is declared inside the `if` block; using it after the
        # block must be an undefined-name error (E3001, from the
        # resolver — the name no longer resolves once the block ends).
        result = check("if true:\n"
                       "  let y = 2\n"
                       "  print y\n"
                       "print y\n")
        self.assertTrue(result.errors,
                        "a block-local let leaked into the module scope")
        self.assertIn("E3001", [d.code for d in result.errors])

    def test_shadowed_let_inside_top_level_for_is_checked(self):
        # The same leak through a `for` body.
        result = check("let x = 1\n"
                       "for i in [1, 2]:\n"
                       "  let x = 2\n"
                       "  print x\n"
                       "print x\n")
        self.assertEqual(result.errors, [])
        inner_let = result.program.stmts[1].body[0]
        self.assertIsInstance(inner_let, A.Let)
        self.assertIsNotNone(inner_let.value.expr_type)


if __name__ == "__main__":
    unittest.main()
