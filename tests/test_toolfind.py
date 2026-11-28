"""Tests for tool lookup (compiler/toolfind.py).

On Windows, `shutil.which` builds candidates from PATHEXT, whose
entries are uppercase (`.COM;.EXE;.BAT;...`), and the filesystem
is case-insensitive, so `which("dart")` returns `...\\dart.EXE`
even though the file on disk is `dart.exe`. That spelling works
when executed directly, but tools that derive a path from it
break: Dart's build-hook runner appends `.exe` itself and looks
for `dart.EXE.exe`, which does not exist (CI run 46053910369,
dart-win: `test_db_probe_runs`, `test_db_program_bundle_runs`,
`test_find_tool_uses_dart_sdk_dir`). `on_disk_spelling` returns
the spelling the directory actually holds, so every caller gets
the real file name.
"""

import os
import stat
import tempfile
import unittest
from pathlib import Path

from compiler.toolfind import on_disk_spelling, which

ROOT = Path(__file__).resolve().parent.parent


class TestOnDiskSpelling(unittest.TestCase):
    def test_returns_real_spelling_for_uppercase_extension(self):
        # The Windows case: the directory holds `dart.exe`, the
        # caller passes the PATHEXT spelling `dart.EXE`.
        with tempfile.TemporaryDirectory() as d:
            real = Path(d) / "dart.exe"
            real.write_text("", encoding="utf-8")
            self.assertEqual(
                on_disk_spelling(str(Path(d) / "dart.EXE")),
                str(real))

    def test_keeps_exact_spelling(self):
        with tempfile.TemporaryDirectory() as d:
            real = Path(d) / "dart.exe"
            real.write_text("", encoding="utf-8")
            self.assertEqual(
                on_disk_spelling(str(real)), str(real))

    def test_missing_file_is_returned_unchanged(self):
        with tempfile.TemporaryDirectory() as d:
            missing = str(Path(d) / "dart.EXE")
            self.assertEqual(on_disk_spelling(missing), missing)

    def test_missing_directory_is_returned_unchanged(self):
        missing = str(Path(tempfile.gettempdir())
                      / "newr-no-such-dir" / "dart.EXE")
        self.assertEqual(on_disk_spelling(missing), missing)

    def test_bare_name_is_returned_unchanged(self):
        self.assertEqual(on_disk_spelling("dart"), "dart")


class TestWhich(unittest.TestCase):
    def test_finds_tool_in_given_path(self):
        # On Windows, `shutil.which` only finds names with an
        # extension in PATHEXT: an extensionless file is never
        # returned, even when it sits in the given path (CI run
        # 46133702014, every Windows job: `which("mydart")`
        # returned None for a `mydart` file). A real tool on
        # Windows has an extension, so the fixture uses one
        # there and the bare name elsewhere.
        name = "mydart.exe" if os.name == "nt" else "mydart"
        with tempfile.TemporaryDirectory() as d:
            tool = Path(d) / name
            tool.write_text("#!/bin/sh\n", encoding="utf-8")
            tool.chmod(tool.stat().st_mode | stat.S_IEXEC)
            self.assertEqual(which("mydart", path=d), str(tool))

    def test_returns_none_when_missing(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertIsNone(which("newr-no-such-tool", path=d))

    def test_falls_back_to_path(self):
        # `which` with an explicit path searches only that path
        # (the same as `shutil.which`); callers fall back to the
        # PATH with an `or` chain, as every finder in this repo
        # does. `python3` is on the PATH of this VM, so the chain
        # finds it.
        with tempfile.TemporaryDirectory() as d:
            found = which("python3", path=d) or which("python3")
            self.assertIsNotNone(found)
            self.assertTrue(Path(found).is_file())


if __name__ == "__main__":
    unittest.main()
