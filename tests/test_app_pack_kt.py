"""Tests for the kt app packager (SPEC.md section 22.2, M8d
Phase 2).

Builds examples/hello.newr for target kt through ./newr, packages
it with `package_kt`, and runs the jar with `java -jar`. The
scratch dirs live under SCRATCH_ROOT (~/workspace, never /tmp),
and every subprocess timeout is deliberately loose: kotlinc is
slow and this VM is small with several agents running in parallel,
so nothing here uses a 60 second timeout.
"""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

import compiler.app_pack_kt as app_pack_kt
from compiler.app_pack import SCRATCH_ROOT
from compiler.app_pack_kt import AppPackError, TOOLCHAIN, package_kt

ROOT = Path(__file__).resolve().parent.parent
LAUNCHER = ROOT / "newr"

KT_AVAILABLE = (TOOLCHAIN.get("kotlin") != "unknown"
                and TOOLCHAIN.get("java") != "unknown")

JARS_DIR = Path.home() / "jars"
SQLITE_JDBC = JARS_DIR / "sqlite-jdbc-3.54.1.0.jar"
SLF4J_API = JARS_DIR / "slf4j-api-2.0.19.jar"
DB_JARS_AVAILABLE = SQLITE_JDBC.is_file() and SLF4J_API.is_file()


def _env(work: Path) -> dict[str, str]:
    """The child environment for the test subprocesses: PATH holds
    both tool bins, TMPDIR stays under ~/workspace."""
    env = dict(os.environ)
    home = Path.home()
    bins = [str(home / "jdk-21" / "bin"),
            str(home / "kotlin" / "kotlinc" / "bin")]
    env["PATH"] = os.pathsep.join(
        bins + [env.get("PATH", "")])
    env["TMPDIR"] = str(work)
    return env


def _run_cli(*args: str, cwd: Path, env: dict[str, str]):
    return subprocess.run([sys.executable, str(LAUNCHER), *args],
                          capture_output=True, text=True, cwd=cwd,
                          timeout=600, env=env)


@contextmanager
def _isolated_scratch(work: Path):
    """Run `package_kt` with its scratch root pointed at a
    test-local dir. The packager reads the module global at call
    time, so patching it here isolates the staging dirs from any
    other process packaging on this VM at the same time (the full
    suite runs concurrently), and the test can assert exactly what
    its own call left behind."""
    scratch = work / "scratch"
    scratch.mkdir(parents=True, exist_ok=True)
    with mock.patch.object(app_pack_kt, "SCRATCH_ROOT", scratch):
        yield scratch


@unittest.skipUnless(KT_AVAILABLE,
                     "kotlinc/java not found, kt packager untestable")
class TestPackageKt(unittest.TestCase):
    """`package_kt` (SPEC.md section 22.2, M8d Phase 2)."""

    def setUp(self):
        SCRATCH_ROOT.mkdir(parents=True, exist_ok=True)

    def test_toolchain_versions_detected_live(self):
        # Live detection, not hardcoded pins: kotlinc-jvm 2.2.0 and
        # OpenJDK 21.0.13 on the build VM (EVAL section 11).
        self.assertRegex(TOOLCHAIN["kotlin"], r"^\d+\.\d+\.\d+$")
        self.assertRegex(TOOLCHAIN["java"], r"^\d+\.\d+\.\d+$")

    def test_package_kt_hello_jar_runs(self):
        with tempfile.TemporaryDirectory(
                prefix="test-app-pack-kt-",
                dir=str(SCRATCH_ROOT)) as d:
            work = Path(d)
            src = ROOT / "examples" / "hello.newr"
            prog = work / "hello.newr"
            shutil.copyfile(src, prog)
            env = _env(work)
            built = _run_cli("build", str(prog), "--target", "kt",
                             cwd=work, env=env)
            self.assertEqual(built.returncode, 0,
                             built.stdout + built.stderr)
            emitted = work / "hello.kt"
            self.assertTrue(emitted.is_file())
            with _isolated_scratch(work) as scratch:
                artifact = package_kt(
                    app_name="hello", emitted_dir=work,
                    emitted_files=["hello.kt"], main_file="hello.kt",
                    dist_dir=work / "dist" / "hello-kt")
                self.assertEqual(list(scratch.glob("hello-kt-*")),
                                 [],
                                 "staging must be removed on "
                                 "success")
            self.assertTrue(artifact.is_file())
            self.assertEqual(artifact.name, "hello.jar")
            size = artifact.stat().st_size
            # The Kotlin runtime is bundled by -include-runtime:
            # the hello jar is about 5.0 MB (EVAL section 4).
            self.assertGreater(size, 4_000_000)
            run = subprocess.run(
                ["java", "-jar", str(artifact)],
                capture_output=True, text=True, cwd=work,
                timeout=300, env=env)
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertEqual(run.stdout, "Hello, world!\n")

    def test_package_kt_compile_error_raises_and_keeps_staging(self):
        with tempfile.TemporaryDirectory(
                prefix="test-app-pack-kt-",
                dir=str(SCRATCH_ROOT)) as d:
            work = Path(d)
            (work / "broken.kt").write_text("fun main( {\n",
                                            encoding="utf-8")
            with _isolated_scratch(work) as scratch:
                with self.assertRaises(AppPackError) as ctx:
                    package_kt(app_name="broken", emitted_dir=work,
                               emitted_files=["broken.kt"],
                               main_file="broken.kt",
                               dist_dir=work / "dist" / "broken-kt")
                self.assertIn("kotlinc", str(ctx.exception))
                kept = list(scratch.glob("broken-kt-*"))
                self.assertEqual(len(kept), 1,
                                 "staging must be kept on failure")

    def test_package_kt_missing_emitted_file_raises(self):
        with tempfile.TemporaryDirectory(
                prefix="test-app-pack-kt-",
                dir=str(SCRATCH_ROOT)) as d:
            work = Path(d)
            with _isolated_scratch(work) as scratch:
                with self.assertRaises(AppPackError) as ctx:
                    package_kt(app_name="nope", emitted_dir=work,
                               emitted_files=["nope.kt"],
                               main_file="nope.kt",
                               dist_dir=work / "dist" / "nope-kt")
                self.assertIn("missing", str(ctx.exception))
                kept = list(scratch.glob("nope-kt-*"))
                self.assertEqual(len(kept), 1,
                                 "staging must be kept on failure")

    @unittest.skipUnless(
        DB_JARS_AVAILABLE,
        "sqlite-jdbc jars not in ~/jars, db program untestable")
    def test_package_kt_db_program_runs_with_driver_on_classpath(self):
        # Multi-file staging (main plus newr_rt.kt) and the
        # conformance runner's db path: the plain jar runs when the
        # driver jars are on the classpath, because `java -jar`
        # ignores -cp. The fat jar that makes plain `java -jar`
        # work for a db program is covered by
        # `test_package_kt_db_program_fat_jar_runs` below.
        with tempfile.TemporaryDirectory(
                prefix="test-app-pack-kt-",
                dir=str(SCRATCH_ROOT)) as d:
            work = Path(d)
            src = ROOT / "conformance" / "ok" / "mod_db.newr"
            prog = work / "mod_db.newr"
            shutil.copyfile(src, prog)
            env = _env(work)
            built = _run_cli("build", str(prog), "--target", "kt",
                             cwd=work, env=env)
            self.assertEqual(built.returncode, 0,
                             built.stdout + built.stderr)
            with _isolated_scratch(work) as scratch:
                artifact = package_kt(
                    app_name="mod-db", emitted_dir=work,
                    emitted_files=["mod_db.kt", "newr_rt.kt"],
                    main_file="mod_db.kt",
                    dist_dir=work / "dist" / "mod-db-kt")
                self.assertEqual(
                    list(scratch.glob("mod-db-kt-*")), [],
                    "staging must be removed on success")
            self.assertTrue(artifact.is_file())
            cp = os.pathsep.join([str(artifact), str(SQLITE_JDBC),
                                  str(SLF4J_API)])
            run = subprocess.run(
                ["java", "-cp", cp, "Mod_dbKt"],
                capture_output=True, text=True, cwd=work,
                timeout=300, env=env)
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertEqual(run.stdout, "PASS\n")

    @unittest.skipUnless(
        DB_JARS_AVAILABLE,
        "sqlite-jdbc jars not in ~/jars, db program untestable")
    def test_package_kt_db_program_fat_jar_runs(self):
        # The fat jar: `package_kt` merges sqlite-jdbc +
        # slf4j-api into the jar when the program uses `db.*`,
        # so a plain `java -jar` runs it with no classpath.
        with tempfile.TemporaryDirectory(
                prefix="test-app-pack-kt-",
                dir=str(SCRATCH_ROOT)) as d:
            work = Path(d)
            src = ROOT / "conformance" / "ok" / "mod_db.newr"
            prog = work / "mod_db.newr"
            shutil.copyfile(src, prog)
            env = _env(work)
            built = _run_cli("build", str(prog), "--target", "kt",
                             cwd=work, env=env)
            self.assertEqual(built.returncode, 0,
                             built.stdout + built.stderr)
            with _isolated_scratch(work) as scratch:
                artifact = package_kt(
                    app_name="mod-db", emitted_dir=work,
                    emitted_files=["mod_db.kt", "newr_rt.kt"],
                    main_file="mod_db.kt",
                    dist_dir=work / "dist" / "mod-db-kt")
                self.assertEqual(
                    list(scratch.glob("mod-db-kt-*")), [],
                    "staging must be removed on success")
            self.assertTrue(artifact.is_file())
            with zipfile.ZipFile(artifact) as zf:
                names = zf.namelist()
                manifest = zf.read(
                    "META-INF/MANIFEST.MF").decode("utf-8")
            self.assertIn("org/sqlite/JDBC.class", names)
            self.assertIn("Main-Class: Mod_dbKt", manifest)
            run = subprocess.run(
                ["java", "-jar", str(artifact)],
                capture_output=True, text=True, cwd=work,
                timeout=300, env=env)
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertEqual(run.stdout, "PASS\n")


if __name__ == "__main__":
    unittest.main()
