"""Tests for imported-struct construction (M8d Phase 0 blocker).

``shared.Product(...)`` builds the struct declared in the imported
file: the checker takes the declaration from the imported module
context (`_call_module_member` struct branch, modeled on the
fielded-variant branch), and the struct's fields read in the
importing file (`_member_field_type` imported fallback). The
resolver already enforces `pub` (E3005), so no export rule is
tested here beyond the combined diagnostic.
"""

import tempfile
import unittest
from pathlib import Path

from compiler.checker import check_program
from compiler.discord_check import discord_providers
from compiler.resolver import resolve_file

SHARED = (
    'pub struct Product:\n'
    '  id: int\n'
    '  name: str\n'
    '  price: int\n'
    '\n'
    'pub fn label(p: Product) -> str:\n'
    '  return p.name + " - " + str(p.price)\n'
)

PRIVATE_SHARED = (
    'struct Secret:\n'
    '  code: int\n'
)


def check_files(files):
    """Write {name: source} to a temp dir, resolve + core-check
    ``main.newr`` there, and return the combined diagnostics."""
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        for name, src in files.items():
            (root / name).write_text(src)
        resolved = resolve_file(root / "main.newr")
        result = check_program(
            resolved.program,
            providers=discord_providers(resolved.program))
        result.diagnostics = list(resolved.diagnostics) + result.diagnostics
        return result


def codes(result):
    return [d.code for d in result.diagnostics]


class TestImportedStruct(unittest.TestCase):
    def test_construct_and_read_fields_and_pass_to_fn_is_ok(self):
        r = check_files({
            "shared.newr": SHARED,
            "main.newr": (
                'import "shared.newr"\n'
                'let p = shared.Product(id: 1, name: "Tea", price: 5000)\n'
                'print p.name\n'
                'print p.price\n'
                'print shared.label(p)\n'),
        })
        self.assertEqual(r.errors, [])

    def test_missing_field_is_E3002(self):
        r = check_files({
            "shared.newr": SHARED,
            "main.newr": (
                'import "shared.newr"\n'
                'let p = shared.Product(id: 1, name: "Tea")\n'
                'print p\n'),
        })
        self.assertIn("E3002", codes(r))

    def test_wrong_field_type_is_E2001(self):
        r = check_files({
            "shared.newr": SHARED,
            "main.newr": (
                'import "shared.newr"\n'
                'let p = shared.Product(id: "one", name: "Tea", '
                'price: 5000)\n'
                'print p\n'),
        })
        self.assertIn("E2001", codes(r))

    def test_positional_arg_is_E3002(self):
        r = check_files({
            "shared.newr": SHARED,
            "main.newr": (
                'import "shared.newr"\n'
                'let p = shared.Product(1, "Tea", 5000)\n'
                'print p\n'),
        })
        self.assertIn("E3002", codes(r))

    def test_unknown_member_is_E3001(self):
        r = check_files({
            "shared.newr": SHARED,
            "main.newr": (
                'import "shared.newr"\n'
                'let p = shared.Nope(id: 1)\n'
                'print p\n'),
        })
        self.assertIn("E3001", codes(r))

    def test_unknown_field_on_imported_struct_is_E3001(self):
        r = check_files({
            "shared.newr": SHARED,
            "main.newr": (
                'import "shared.newr"\n'
                'let p = shared.Product(id: 1, name: "Tea", price: 5000)\n'
                'print p.weight\n'),
        })
        self.assertIn("E3001", codes(r))

    def test_non_pub_struct_is_E3005(self):
        r = check_files({
            "shared.newr": PRIVATE_SHARED,
            "main.newr": (
                'import "shared.newr"\n'
                'let s = shared.Secret(code: 7)\n'
                'print s\n'),
        })
        self.assertIn("E3005", codes(r))
