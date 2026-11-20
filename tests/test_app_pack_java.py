"""Tests for the Java app packager (compiler/app_pack_java.py,
M8d Phase 2).

The smoke test builds examples/hello.newr for target java via
./newr (the plain build, no --app: the CLI dispatch for java is
wired by the integration agent later), calls `package_java` on
the emitted files, runs the jar with `java -jar`, and checks the
jar holds only `.class` files.

Temp dirs live under SCRATCH_ROOT (`~/workspace`, never `/tmp`:
it is a 512 MB tmpfs), and every subprocess runs with TMPDIR
under `~/workspace` and PATH with `~/jdk-21/bin` first. Timeouts
are loose on purpose: the build VM has a small CPU and several
agents run in parallel.
"""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

from compiler.app_pack import SCRATCH_ROOT, AppPackError
from compiler.app_pack_java import TOOLCHAIN, package_java

ROOT = Path(__file__).resolve().parent.parent
LAUNCHER = ROOT / "newr"

JARS_DIR = Path.home() / "jars"
SQLITE_JDBC = JARS_DIR / "sqlite-jdbc-3.54.1.0.jar"
SLF4J_API = JARS_DIR / "slf4j-api-2.0.19.jar"
DB_JARS_AVAILABLE = SQLITE_JDBC.is_file() and SLF4J_API.is_file()

# The JDK lives in ~/jdk-21 (installed for M4); fall back to the
# PATH when it is not there (e.g. CI, where setup-java provides it).
_JAVA_HOME = Path(os.environ["JAVA_HOME"]) \
    if os.environ.get("JAVA_HOME") else (Path.home() / "jdk-21")
_JAVA_BIN = _JAVA_HOME / "bin"
JAVA = shutil.which("java", path=str(_JAVA_BIN)) or shutil.which("java")
JAVAC = shutil.which("javac", path=str(_JAVA_BIN)) or shutil.which("javac")

#: Loose timeouts: the VM CPU is small and several agents run in
#: parallel, so a build or a jar run must not fail on a slow tick.
BUILD_TIMEOUT_SECONDS = 300
RUN_TIMEOUT_SECONDS = 120


def _env() -> dict[str, str]:
    """The env for a subprocess in these tests: PATH with the JDK
    bin dir first, TMPDIR under `~/workspace`."""
    SCRATCH_ROOT.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env["PATH"] = str(_JAVA_BIN) + os.pathsep + env.get("PATH", "")
    env["TMPDIR"] = str(SCRATCH_ROOT)
    return env


def _run_cli(*args, cwd=None):
    return subprocess.run([sys.executable, str(LAUNCHER), *args],
                          capture_output=True, text=True, cwd=cwd,
                          timeout=BUILD_TIMEOUT_SECONDS, env=_env())


def _temp_dir():
    """A temp dir under SCRATCH_ROOT (never /tmp)."""
    SCRATCH_ROOT.mkdir(parents=True, exist_ok=True)
    return tempfile.TemporaryDirectory(dir=str(SCRATCH_ROOT))


@unittest.skipUnless(JAVA and JAVAC, "a JDK is not installed")
class TestPackageJava(unittest.TestCase):
    def test_toolchain_version_detected_live(self):
        # TOOLCHAIN is detected from a live `javac -version` /
        # `java -version` run, not hardcoded.
        self.assertRegex(TOOLCHAIN["java"], r"^\d+\.\d+")

    def test_hello_jar_runs_and_contains_only_classes(self):
        with _temp_dir() as d:
            src = ROOT / "examples" / "hello.newr"
            f = Path(d) / "hello.newr"
            shutil.copyfile(src, f)
            built = _run_cli("build", str(f), "--target", "java",
                             cwd=d)
            self.assertEqual(built.returncode, 0,
                             built.stdout + built.stderr)
            self.assertTrue((Path(d) / "hello.java").is_file())
            dist_dir = Path(d) / "dist" / "hello-java"
            before = {p.name
                      for p in SCRATCH_ROOT.glob("hello-java-*")}
            artifact = package_java(
                app_name="hello", emitted_dir=Path(d),
                emitted_files=["hello.java"], main_file="hello.java",
                dist_dir=dist_dir)
            self.assertEqual(artifact, dist_dir / "hello.jar")
            self.assertTrue(artifact.is_file())
            with zipfile.ZipFile(artifact) as zf:
                names = zf.namelist()
                manifest = zf.read(
                    "META-INF/MANIFEST.MF").decode("utf-8")
            # Only .class files in the jar (plus the manifest the
            # jar tool generates); no .java source was swept in.
            for name in names:
                if name in ("META-INF/", "META-INF/MANIFEST.MF"):
                    continue
                self.assertTrue(name.endswith(".class"), name)
            self.assertNotIn("hello.java", names)
            self.assertIn("hello.class", names)
            self.assertIn("Main-Class: hello", manifest)
            run = subprocess.run(
                [JAVA, "-jar", str(artifact)], capture_output=True,
                text=True, cwd=d, timeout=RUN_TIMEOUT_SECONDS,
                env=_env())
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertEqual(run.stdout, "Hello, world!\n")
            # The staging dir is removed on success.
            after = {p.name
                     for p in SCRATCH_ROOT.glob("hello-java-*")}
            self.assertEqual(after, before)

    def test_runtime_program_jar_runs(self):
        # A program that uses the runtime (`len` needs NewrRt):
        # NewrRt.java is staged and compiled into the same jar.
        with _temp_dir() as d:
            f = Path(d) / "lenprog.newr"
            f.write_text('print len("abc")\n', encoding="utf-8")
            built = _run_cli("build", str(f), "--target", "java",
                             cwd=d)
            self.assertEqual(built.returncode, 0,
                             built.stdout + built.stderr)
            self.assertTrue((Path(d) / "NewrRt.java").is_file())
            artifact = package_java(
                app_name="lenprog", emitted_dir=Path(d),
                emitted_files=["lenprog.java", "NewrRt.java"],
                main_file="lenprog.java",
                dist_dir=Path(d) / "dist" / "lenprog-java")
            with zipfile.ZipFile(artifact) as zf:
                names = zf.namelist()
            self.assertIn("lenprog.class", names)
            self.assertIn("NewrRt.class", names)
            self.assertNotIn("lenprog.java", names)
            self.assertNotIn("NewrRt.java", names)
            run = subprocess.run(
                [JAVA, "-jar", str(artifact)], capture_output=True,
                text=True, cwd=d, timeout=RUN_TIMEOUT_SECONDS,
                env=_env())
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertEqual(run.stdout, "3\n")

    def test_missing_emitted_file_raises_app_pack_error(self):
        with _temp_dir() as d:
            with self.assertRaises(AppPackError) as ctx:
                package_java(
                    app_name="hello", emitted_dir=Path(d),
                    emitted_files=["hello.java"],
                    main_file="hello.java",
                    dist_dir=Path(d) / "dist" / "hello-java")
            self.assertIn("hello.java", str(ctx.exception))
            self.assertNotIn("Traceback", str(ctx.exception))

    def test_no_java_files_raises_app_pack_error(self):
        with _temp_dir() as d:
            with self.assertRaises(AppPackError) as ctx:
                package_java(
                    app_name="hello", emitted_dir=Path(d),
                    emitted_files=["pom.xml"],
                    main_file="hello.java",
                    dist_dir=Path(d) / "dist" / "hello-java")
            self.assertIn("javac", str(ctx.exception))

    def test_bad_main_stem_raises_app_pack_error(self):
        with _temp_dir() as d:
            with self.assertRaises(AppPackError) as ctx:
                package_java(
                    app_name="hello", emitted_dir=Path(d),
                    emitted_files=["hello.java"],
                    main_file="123bad.java",
                    dist_dir=Path(d) / "dist" / "hello-java")
            self.assertIn("123bad", str(ctx.exception))

    @unittest.skipUnless(
        DB_JARS_AVAILABLE,
        "sqlite-jdbc jars not in ~/jars, db program untestable")
    def test_db_program_fat_jar_runs_with_plain_java_jar(self):
        # A db program's jar is fattened with sqlite-jdbc +
        # slf4j-api, so a plain `java -jar` runs it (a plain jar
        # fails with `org.sqlite.JDBC` because `java -jar`
        # ignores any classpath).
        with _temp_dir() as d:
            src = ROOT / "conformance" / "ok" / "mod_db.newr"
            f = Path(d) / "mod_db.newr"
            shutil.copyfile(src, f)
            built = _run_cli("build", str(f), "--target", "java",
                             cwd=d)
            self.assertEqual(built.returncode, 0,
                             built.stdout + built.stderr)
            artifact = package_java(
                app_name="mod-db", emitted_dir=Path(d),
                emitted_files=["mod_db.java", "NewrRt.java"],
                main_file="mod_db.java",
                dist_dir=Path(d) / "dist" / "mod-db-java")
            with zipfile.ZipFile(artifact) as zf:
                names = zf.namelist()
                manifest = zf.read(
                    "META-INF/MANIFEST.MF").decode("utf-8")
            self.assertIn("org/sqlite/JDBC.class", names)
            self.assertIn("Main-Class: mod_db", manifest)
            run = subprocess.run(
                [JAVA, "-jar", str(artifact)], capture_output=True,
                text=True, cwd=d, timeout=RUN_TIMEOUT_SECONDS,
                env=_env())
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertEqual(run.stdout, "PASS\n")

    def test_javac_failure_raises_and_keeps_staging(self):
        with _temp_dir() as d:
            bad = Path(d) / "bad.java"
            bad.write_text("this is not java\n", encoding="utf-8")
            before = set(SCRATCH_ROOT.glob("bad-java-*"))
            with self.assertRaises(AppPackError) as ctx:
                package_java(
                    app_name="bad", emitted_dir=Path(d),
                    emitted_files=["bad.java"], main_file="bad.java",
                    dist_dir=Path(d) / "dist" / "bad-java")
            self.assertIn("javac", str(ctx.exception))
            self.assertNotIn("Traceback", str(ctx.exception))
            kept = [p for p in SCRATCH_ROOT.glob("bad-java-*")
                    if p not in before]
            self.assertEqual(len(kept), 1)
            self.assertTrue((kept[0] / "bad.java").is_file())
            # Clean up the kept staging dir so the test does not
            # litter the scratch root.
            shutil.rmtree(kept[0], ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
