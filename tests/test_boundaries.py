"""Boundary test (SPEC.md SS2.1): the core checker never imports
anything Discord. After ``import compiler.checker``, neither
``compiler.discord_check`` nor ``compiler.discord_limits`` may be in
``sys.modules``, and a source scan of ``compiler/checker.py`` must
find no ``import`` of a ``discord*`` module.

The by-name exemption (SPEC.md SS2.1) is also asserted here: the
literal ``"discord"`` may occur in core only at the documented
exemption sites — the two bypasses in checker.py, the NAMED_MODULES
registry + W1002 exemption in resolver.py, and RESERVED_BUILTIN_NAMES
in modules.py. Entity type *names* (``user``/``guild``) are core types
-- the test targets imports and the exact exemption list, not words
in general.
"""

import sys
import unittest
from pathlib import Path


def _root():
    return Path(__file__).resolve().parent.parent


def _discord_literal_lines(path: Path) -> list[tuple[int, str]]:
    out = []
    for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(),
                            start=1):
        if '"discord"' in line:
            out.append((i, line.strip()))
    return out


class TestCoreDiscordBoundary(unittest.TestCase):
    def test_checker_import_does_not_load_discord_modules(self):
        for mod in ("compiler.discord_check", "compiler.discord_limits"):
            sys.modules.pop(mod, None)
        import compiler.checker  # noqa: F401
        self.assertNotIn("compiler.discord_check", sys.modules)
        self.assertNotIn("compiler.discord_limits", sys.modules)

    def test_checker_source_has_no_discord_import(self):
        path = _root() / "compiler" / "checker.py"
        src = path.read_text(encoding="utf-8")
        for line in src.splitlines():
            stripped = line.strip()
            if stripped.startswith(("import ", "from ")):
                self.assertNotIn("discord", stripped,
                                 f"core checker imports a discord module: {line!r}")

    def test_discord_check_is_the_only_discord_importer(self):
        # discord_check.py is allowed (and expected) to import the
        # discord limits; the core checker is not.
        path = _root() / "compiler" / "discord_check.py"
        src = path.read_text(encoding="utf-8")
        self.assertIn("discord_limits", src)

    def test_checker_discord_literal_only_at_bypass_sites(self):
        # SPEC.md SS2.1: exactly two bypasses, both `if mod ==
        # "discord": return UNKNOWN` — never a Discord rule.
        hits = _discord_literal_lines(_root() / "compiler" / "checker.py")
        self.assertEqual(len(hits), 2, hits)
        for _lineno, line in hits:
            self.assertEqual(line, 'if mod == "discord":', line)

    def test_resolver_discord_literal_only_at_exemption_sites(self):
        hits = _discord_literal_lines(_root() / "compiler" / "resolver.py")
        # NAMED_MODULES registry + the W1002 bot-block exemption.
        self.assertEqual(len(hits), 2, hits)
        joined = " ".join(line for _n, line in hits)
        self.assertIn("NAMED_MODULES", joined)
        self.assertIn('bound_name == "discord"', joined)

    def test_modules_discord_literal_only_in_reserved_names(self):
        # SPEC.md SS2.1: the one occurrence is the RESERVED_BUILTIN_NAMES
        # entry (a module name a program may not redefine).
        path = _root() / "compiler" / "modules.py"
        hits = _discord_literal_lines(path)
        self.assertEqual(len(hits), 1, hits)
        src = path.read_text(encoding="utf-8")
        reserved = src.split("RESERVED_BUILTIN_NAMES", 1)[1]
        self.assertIn('"discord"', reserved[:300])


if __name__ == "__main__":
    unittest.main()
