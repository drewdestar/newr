"""Tests for the dart app packager (SPEC.md section 22.2, M8d Phase 2).

The smoke test builds examples/hello.newr for target dart via the
`./newr` launcher (the same pattern as TestBuildApp in
test_cli.py), calls `package_dart` on the emitted files, runs the
artifact binary, and asserts the output. The db test builds
examples/todo.newr (a `db.*` program, so the emitter writes a
pubspec.yaml with sqlite3 3.9.0), packages it through the `dart
pub get` plus `dart build cli` path, and runs the bundled binary:
`dart compile exe` cannot build a db program on this SDK (it
produces a binary with no sqlite3 native asset, exit 255 at
runtime), so the bundle path is the one the test proves.

Timeouts here are deliberately very loose: this VM has a small
CPU and several agents run in parallel, so even a two-second
dart compile can take far longer wall-clock. Temp dirs live
under SCRATCH_ROOT (in `~/workspace`), never /tmp.
"""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
LAUNCHER = ROOT / "newr"

from compiler.app_pack import AppPackError, SCRATCH_ROOT
from compiler.app_pack_dart import (
    TOOLCHAIN,
    _build_env,
    _find_tool,
    _require_dart,
    package_dart,
)

#: Temp dirs for these tests live under the workspace scratch
#: root, never /tmp (512 MB tmpfs).
TEST_TMP_ROOT = SCRATCH_ROOT / "tests"


def run_cli(*args, cwd=None):
    # The build step is check plus emit only, seconds even on a
    # loaded VM; 600 s is a generous ceiling, not an expectation.
    env = dict(os.environ)
    env["TMPDIR"] = str(TEST_TMP_ROOT)
    return subprocess.run([sys.executable, str(LAUNCHER), *args],
                          capture_output=True, text=True, cwd=cwd,
                          timeout=600, env=env)


def _tempdir():
    """A temp dir under `~/workspace`, for one test."""
    TEST_TMP_ROOT.mkdir(parents=True, exist_ok=True)
    return tempfile.TemporaryDirectory(dir=str(TEST_TMP_ROOT))


def _staging_leftovers(prefix):
    """Staging dirs under SCRATCH_ROOT with the given prefix."""
    if not SCRATCH_ROOT.is_dir():
        return set()
    return {p.name for p in SCRATCH_ROOT.iterdir()
            if p.is_dir() and p.name.startswith(prefix)}


class TestToolchainDetectedLive(unittest.TestCase):
    def test_toolchain_version_is_live(self):
        # dart is installed on this VM (EVAL.md section 11 pins
        # 3.13.7); TOOLCHAIN must carry the live version, not a
        # hardcoded guess and not "unknown".
        if TOOLCHAIN["dart"] == "unknown":
            self.skipTest("dart is not installed on this VM")
        self.assertRegex(TOOLCHAIN["dart"], r"^\d+\.\d+\.\d+$",
                         f"dart version {TOOLCHAIN['dart']!r} is not "
                         f"a live version number")

    def test_missing_dart_raises_naming_install(self):
        with mock.patch("compiler.app_pack_dart._find_tool",
                        return_value=None):
            with self.assertRaises(AppPackError) as caught:
                _require_dart()
            self.assertIn("dart", str(caught.exception))
            self.assertIn("install", str(caught.exception).lower())
            self.assertIn("Dart SDK", str(caught.exception))


class TestPackageDartHelloSmoke(unittest.TestCase):
    def test_hello_smoke_binary_runs(self):
        if TOOLCHAIN["dart"] == "unknown":
            self.skipTest("dart is not installed on this VM")
        before = _staging_leftovers("hello-dart-")
        with _tempdir() as d:
            src = ROOT / "examples" / "hello.newr"
            f = Path(d) / "hello.newr"
            shutil.copyfile(src, f)
            r = run_cli("build", str(f), "--target", "dart", cwd=d)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            emitted = Path(d) / "hello.dart"
            self.assertTrue(emitted.is_file())
            # hello is a plain program: no pubspec, no runtime.
            self.assertFalse((Path(d) / "pubspec.yaml").exists())
            self.assertFalse((Path(d) / "newr_rt.dart").exists())
            dist_dir = Path(d) / "dist" / "hello-dart"
            artifact = package_dart(
                app_name="hello", emitted_dir=Path(d),
                emitted_files=["hello.dart"], main_file="hello.dart",
                dist_dir=dist_dir)
            self.assertEqual(artifact, dist_dir / "hello")
            self.assertTrue(artifact.is_file())
            size = artifact.stat().st_size
            self.assertGreater(size, 0)
            print(f"\nhello artifact bytes: {size}")
            run = subprocess.run([str(artifact)], capture_output=True,
                                 text=True, timeout=300)
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertEqual(run.stdout, "Hello, world!\n")
        after = _staging_leftovers("hello-dart-")
        self.assertEqual(after - before, set(),
                         "a successful package must remove its "
                         "staging dir")

    def test_missing_main_file_raises_app_pack_error(self):
        with _tempdir() as d:
            with self.assertRaises(AppPackError) as caught:
                package_dart(
                    app_name="hello", emitted_dir=Path(d),
                    emitted_files=["nope.dart"], main_file="nope.dart",
                    dist_dir=Path(d) / "dist" / "hello-dart")
            # The diagnostic names the failing step, never a
            # traceback: the exception message carries the dart
            # compile command and the missing file.
            self.assertIn("dart compile exe", str(caught.exception))
            self.assertIn("nope.dart", str(caught.exception))

    def test_missing_emitted_file_keeps_staging(self):
        # The main file is listed but not on disk: staging is
        # created, the copy fails, and the staging dir is kept
        # for diagnosis (and cleaned up here, so the test leaves
        # no litter behind).
        before = _staging_leftovers("hello-dart-")
        with _tempdir() as d:
            with self.assertRaises(AppPackError) as caught:
                package_dart(
                    app_name="hello", emitted_dir=Path(d),
                    emitted_files=["hello.dart"],
                    main_file="hello.dart",
                    dist_dir=Path(d) / "dist" / "hello-dart")
            self.assertIn("dart compile exe", str(caught.exception))
            self.assertIn("hello.dart", str(caught.exception))
        kept = _staging_leftovers("hello-dart-") - before
        self.assertEqual(len(kept), 1,
                         f"one staging dir must be kept, got {kept}")
        for name in kept:
            shutil.rmtree(SCRATCH_ROOT / name, ignore_errors=True)


class TestPackageDartDbTodo(unittest.TestCase):
    def test_db_program_bundle_runs(self):
        # todo.newr uses db.* on :memory:, so the emitter writes
        # a pubspec.yaml (sqlite3 3.9.0) and newr_rt.dart. The
        # artifact is the bundle directory: bin/todo plus
        # lib/libsqlite3.so, and the bundled binary runs from any
        # directory and prints the expected output.
        if TOOLCHAIN["dart"] == "unknown":
            self.skipTest("dart is not installed on this VM")
        before = _staging_leftovers("todo-dart-")
        with _tempdir() as d:
            src = ROOT / "examples" / "todo.newr"
            f = Path(d) / "todo.newr"
            shutil.copyfile(src, f)
            r = run_cli("build", str(f), "--target", "dart", cwd=d)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertTrue((Path(d) / "pubspec.yaml").is_file())
            self.assertTrue((Path(d) / "newr_rt.dart").is_file())
            dist_dir = Path(d) / "dist" / "todo-dart"
            artifact = package_dart(
                app_name="todo", emitted_dir=Path(d),
                emitted_files=["todo.dart", "newr_rt.dart",
                               "pubspec.yaml"],
                main_file="todo.dart", dist_dir=dist_dir)
            self.assertEqual(artifact, dist_dir)
            binary = dist_dir / "bin" / "todo"
            self.assertTrue(binary.is_file())
            size = binary.stat().st_size
            self.assertGreater(size, 0)
            print(f"\ntodo bundle binary bytes: {size}")
            lib = dist_dir / "lib" / "libsqlite3.so"
            self.assertTrue(lib.is_file(),
                            "the bundle must carry the sqlite3 "
                            "native library, or the binary fails "
                            "at runtime with no native asset")
            run = subprocess.run([str(binary)], capture_output=True,
                                 text=True, timeout=300, cwd="/")
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertIn("Todos:\n", run.stdout)
            self.assertIn("[ ] beli susu\n", run.stdout)
            self.assertIn("[x] beli susu\n", run.stdout)
            self.assertIn("After marking the first one done:\n",
                          run.stdout)
        after = _staging_leftovers("todo-dart-")
        self.assertEqual(after - before, set(),
                         "a successful package must remove its "
                         "staging dir")


class TestBuildEnv(unittest.TestCase):
    def test_tmpdir_is_under_workspace(self):
        # SPEC section 22.2: build temp files live under
        # ~/workspace, never /tmp.
        env = _build_env(Path.home() / "workspace" / "x")
        tmpdir = Path(env["TMPDIR"])
        self.assertTrue(
            tmpdir.is_relative_to(Path.home() / "workspace"),
            f"TMPDIR {tmpdir} is not under ~/workspace")
        self.assertNotEqual(tmpdir.parts[1], "tmp")

    def test_find_tool_uses_dart_sdk_dir(self):
        # dart is not on the default PATH on this VM; it is found
        # through ~/dart-sdk/dart-sdk/bin.
        found = _find_tool("dart")
        if found is None:
            self.skipTest("dart is not installed on this VM")
        self.assertTrue(Path(found).is_file())
        self.assertEqual(Path(found).name, "dart")


if __name__ == "__main__":
    unittest.main()
