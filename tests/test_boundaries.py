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


def _db_literal_lines(path: Path) -> list[tuple[int, str]]:
    out = []
    for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(),
                            start=1):
        if '"db"' in line:
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


class TestCoreDbBoundary(unittest.TestCase):
    """The db twin of the discord boundary (SPEC.md SS2.1, SS12.5):
    the core checker never imports anything db, and the literal
    "db" occurs in core only at the documented exemption sites."""

    def test_checker_import_does_not_load_db_modules(self):
        for mod in ("compiler.db_check", "compiler.db_limits"):
            sys.modules.pop(mod, None)
        import compiler.checker  # noqa: F401
        self.assertNotIn("compiler.db_check", sys.modules)
        self.assertNotIn("compiler.db_limits", sys.modules)

    def test_checker_source_has_no_db_import(self):
        path = _root() / "compiler" / "checker.py"
        src = path.read_text(encoding="utf-8")
        for line in src.splitlines():
            stripped = line.strip()
            if stripped.startswith(("import ", "from ")):
                self.assertNotIn("db_", stripped,
                                 f"core checker imports a db module: {line!r}")

    def test_db_check_is_the_only_db_limits_importer(self):
        # db_check.py imports the db limits (and docs.py renders the
        # surface from them into api.md); the core checker does not.
        path = _root() / "compiler" / "db_check.py"
        src = path.read_text(encoding="utf-8")
        self.assertIn("db_limits", src)
        docs = (_root() / "compiler" / "docs.py").read_text(
            encoding="utf-8")
        self.assertIn("db_limits", docs)

    def test_checker_db_literal_only_at_bypass_sites(self):
        # SPEC.md SS2.1 + SS12.2 + SS12.5: `db` is usable by name
        # without an import, and the core checker returns UNKNOWN
        # for db calls (the rules live in db_check.py). The name
        # occurs at exactly six sites: two UNKNOWN bypasses (`if
        # mod == "db": return UNKNOWN`) and four no-import fallback
        # guards (the N-09/N-10 pattern, mirroring the emitters)
        # that route a bare `db` to the bypass — never a db rule.
        hits = _db_literal_lines(_root() / "compiler" / "checker.py")
        self.assertEqual(len(hits), 6, hits)
        bypasses = [line for _n, line in hits
                    if line == 'if mod == "db":']
        self.assertEqual(len(bypasses), 2, hits)
        fallbacks = [line for _n, line in hits
                     if line != 'if mod == "db":']
        self.assertEqual(len(fallbacks), 4, hits)
        for line in fallbacks:
            self.assertTrue(
                line.startswith(("or obj.name",
                                 "elif target is None")),
                line)

    def test_resolver_db_literal_only_at_exemption_sites(self):
        hits = _db_literal_lines(_root() / "compiler" / "resolver.py")
        # The NAMED_MODULES registry only; db needs no W1002-style
        # exemption (no implicit import).
        self.assertEqual(len(hits), 1, hits)
        joined = " ".join(line for _n, line in hits)
        self.assertIn("NAMED_MODULES", joined)

    def test_modules_db_literal_only_in_reserved_names(self):
        # SPEC.md SS2.1: the one occurrence is the RESERVED_BUILTIN_NAMES
        # entry (a module name a program may not redefine).
        path = _root() / "compiler" / "modules.py"
        hits = _db_literal_lines(path)
        self.assertEqual(len(hits), 1, hits)
        src = path.read_text(encoding="utf-8")
        reserved = src.split("RESERVED_BUILTIN_NAMES", 1)[1]
        self.assertIn('"db"', reserved[:300])


def _http_literal_lines(path: Path) -> list[tuple[int, str]]:
    out = []
    for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(),
                            start=1):
        if '"http"' in line:
            out.append((i, line.strip()))
    return out


def _json_literal_lines(path: Path) -> list[tuple[int, str]]:
    out = []
    for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(),
                            start=1):
        if '"json"' in line:
            out.append((i, line.strip()))
    return out


class TestCoreHttpBoundary(unittest.TestCase):
    """The http twin of the db boundary (SPEC.md SS2.1, SS12.6):
    the core checker never imports anything http, and the literal
    "http" occurs in core only at the documented exemption sites."""

    def test_checker_import_does_not_load_http_modules(self):
        for mod in ("compiler.http_check", "compiler.http_limits"):
            sys.modules.pop(mod, None)
        import compiler.checker  # noqa: F401
        self.assertNotIn("compiler.http_check", sys.modules)
        self.assertNotIn("compiler.http_limits", sys.modules)

    def test_checker_source_has_no_http_import(self):
        path = _root() / "compiler" / "checker.py"
        src = path.read_text(encoding="utf-8")
        for line in src.splitlines():
            stripped = line.strip()
            if stripped.startswith(("import ", "from ")):
                self.assertNotIn("http_", stripped,
                                 f"core checker imports an http module: {line!r}")

    def test_http_check_is_the_only_http_limits_importer(self):
        # http_check.py imports the http limits (and docs.py renders
        # the surface from them into api.md); the core checker does
        # not.
        path = _root() / "compiler" / "http_check.py"
        src = path.read_text(encoding="utf-8")
        self.assertIn("http_limits", src)
        docs = (_root() / "compiler" / "docs.py").read_text(
            encoding="utf-8")
        self.assertIn("http_limits", docs)

    def test_checker_http_literal_only_at_bypass_sites(self):
        # SPEC.md SS2.1 + SS12.2 + SS12.6: `http` is usable by name
        # without an import, and the core checker returns UNKNOWN
        # for http calls (the rules live in http_check.py). The name
        # occurs at exactly six sites: two UNKNOWN bypasses (`if
        # mod == "http": return UNKNOWN`) and four no-import fallback
        # guards (the N-09/N-10 pattern, mirroring the emitters)
        # that route a bare `http` to the bypass — never an http
        # rule. The fallback lines start with "or obj.name" (the
        # `or obj.name in ("http", "json")` guard), "elif target is
        # None" (the `bound in MODULES` guard), or "or bound"
        # (the continuation of that guard naming http/json).
        hits = _http_literal_lines(_root() / "compiler" / "checker.py")
        self.assertEqual(len(hits), 6, hits)
        bypasses = [line for _n, line in hits
                    if line == 'if mod == "http":']
        self.assertEqual(len(bypasses), 2, hits)
        fallbacks = [line for _n, line in hits
                     if line != 'if mod == "http":']
        self.assertEqual(len(fallbacks), 4, hits)
        for line in fallbacks:
            self.assertTrue(
                line.startswith(("or obj.name",
                                 "elif target is None",
                                 "or bound")),
                line)

    def test_resolver_http_literal_only_at_exemption_sites(self):
        path = _root() / "compiler" / "resolver.py"
        hits = _http_literal_lines(path)
        # The NAMED_MODULES registry only; http needs no W1002-style
        # exemption (no implicit import). The registry spans two
        # lines, so the check is on the assignment block, not the
        # hit line alone.
        self.assertEqual(len(hits), 1, hits)
        src = path.read_text(encoding="utf-8")
        registry = src.split("NAMED_MODULES", 1)[1].split("}", 1)[0]
        self.assertIn('"http"', registry)

    def test_modules_http_literal_only_in_reserved_names(self):
        # SPEC.md SS2.1: the one occurrence is the RESERVED_BUILTIN_NAMES
        # entry (a module name a program may not redefine).
        path = _root() / "compiler" / "modules.py"
        hits = _http_literal_lines(path)
        self.assertEqual(len(hits), 1, hits)
        src = path.read_text(encoding="utf-8")
        reserved = src.split("RESERVED_BUILTIN_NAMES", 1)[1]
        self.assertIn('"http"', reserved[:300])


class TestCoreJsonBoundary(unittest.TestCase):
    """The json twin of the db boundary (SPEC.md SS2.1, SS12.7):
    the core checker never imports anything json, and the literal
    "json" occurs in core only at the documented exemption sites."""

    def test_checker_import_does_not_load_json_modules(self):
        for mod in ("compiler.json_check", "compiler.json_limits"):
            sys.modules.pop(mod, None)
        import compiler.checker  # noqa: F401
        self.assertNotIn("compiler.json_check", sys.modules)
        self.assertNotIn("compiler.json_limits", sys.modules)

    def test_checker_source_has_no_json_import(self):
        path = _root() / "compiler" / "checker.py"
        src = path.read_text(encoding="utf-8")
        for line in src.splitlines():
            stripped = line.strip()
            if stripped.startswith(("import ", "from ")):
                self.assertNotIn("json_", stripped,
                                 f"core checker imports a json module: {line!r}")

    def test_json_check_is_the_only_json_limits_importer(self):
        # json_check.py imports the json limits (and docs.py renders
        # the surface from them into api.md); the core checker does
        # not.
        path = _root() / "compiler" / "json_check.py"
        src = path.read_text(encoding="utf-8")
        self.assertIn("json_limits", src)
        docs = (_root() / "compiler" / "docs.py").read_text(
            encoding="utf-8")
        self.assertIn("json_limits", docs)

    def test_checker_json_literal_only_at_bypass_sites(self):
        # SPEC.md SS2.1 + SS12.2 + SS12.7: `json` is usable by name
        # without an import, and the core checker returns UNKNOWN
        # for json calls (the rules live in json_check.py). The name
        # occurs at exactly six sites: two UNKNOWN bypasses (`if
        # mod == "json": return UNKNOWN`) and four no-import fallback
        # guards (the N-09/N-10 pattern, mirroring the emitters)
        # that route a bare `json` to the bypass — never a json
        # rule. The fallback lines start with "or obj.name" (the
        # `or obj.name in ("http", "json")` guard), "elif target is
        # None" (the `bound in MODULES` guard), or "or bound"
        # (the continuation of that guard naming http/json).
        hits = _json_literal_lines(_root() / "compiler" / "checker.py")
        self.assertEqual(len(hits), 6, hits)
        bypasses = [line for _n, line in hits
                    if line == 'if mod == "json":']
        self.assertEqual(len(bypasses), 2, hits)
        fallbacks = [line for _n, line in hits
                     if line != 'if mod == "json":']
        self.assertEqual(len(fallbacks), 4, hits)
        for line in fallbacks:
            self.assertTrue(
                line.startswith(("or obj.name",
                                 "elif target is None",
                                 "or bound")),
                line)

    def test_resolver_json_literal_only_at_exemption_sites(self):
        path = _root() / "compiler" / "resolver.py"
        hits = _json_literal_lines(path)
        # The NAMED_MODULES registry only; json needs no W1002-style
        # exemption (no implicit import). The old E3008 special case
        # (import json is not available in v1) was removed in M8b.
        # The registry spans two lines, so the check is on the
        # assignment block, not the hit line alone.
        self.assertEqual(len(hits), 1, hits)
        src = path.read_text(encoding="utf-8")
        registry = src.split("NAMED_MODULES", 1)[1].split("}", 1)[0]
        self.assertIn('"json"', registry)

    def test_modules_json_literal_only_in_reserved_names(self):
        # SPEC.md SS2.1: the one occurrence is the RESERVED_BUILTIN_NAMES
        # entry (a module name a program may not redefine).
        path = _root() / "compiler" / "modules.py"
        hits = _json_literal_lines(path)
        self.assertEqual(len(hits), 1, hits)
        src = path.read_text(encoding="utf-8")
        reserved = src.split("RESERVED_BUILTIN_NAMES", 1)[1]
        self.assertIn('"json"', reserved[:300])


if __name__ == "__main__":
    unittest.main()
