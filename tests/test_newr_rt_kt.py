"""Tests for the Kotlin runtime (compiler/newr_rt.kt, M5).

Mirrors tests/test_newr_rt_java.py: compile the runtime together
with the driver in tests/kt/RtChecks.kt and run it. Skips when no
kotlinc/java is available (KOTLIN_HOME, ~/kotlin, or PATH, plus
JAVA_HOME, ~/jdk-21/bin, or PATH — kotlinc's script needs `java`
on PATH).
"""

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RT_KT_SRC = ROOT / "compiler" / "newr_rt.kt"
RT_CHECKS_SRC = ROOT / "tests" / "kt" / "RtChecks.kt"


def _find_java(name):
    """Find a JDK tool: JAVA_HOME/bin, ~/jdk-21/bin, then PATH."""
    java_home = os.environ.get("JAVA_HOME")
    if java_home:
        p = Path(java_home) / "bin" / name
        if p.exists():
            return str(p)
    p = Path.home() / "jdk-21" / "bin" / name
    if p.exists():
        return str(p)
    return shutil.which(name)


def _find_kotlinc():
    """Find kotlinc: KOTLIN_HOME/kotlinc/bin, ~/kotlin/kotlinc/bin,
    then PATH."""
    kotlin_home = os.environ.get("KOTLIN_HOME")
    if kotlin_home:
        p = Path(kotlin_home) / "kotlinc" / "bin" / "kotlinc"
        if p.exists():
            return str(p)
    p = Path.home() / "kotlin" / "kotlinc" / "bin" / "kotlinc"
    if p.exists():
        return str(p)
    return shutil.which("kotlinc")


KOTLINC = _find_kotlinc()
JAVA = _find_java("java")


def _kt_env():
    """kotlinc's script needs `java` on PATH."""
    merged = dict(os.environ)
    jdk_bin = str(Path.home() / "jdk-21" / "bin")
    if jdk_bin not in merged.get("PATH", "").split(os.pathsep):
        merged["PATH"] = jdk_bin + os.pathsep + merged.get("PATH", "")
    return merged


class TestNewrRtKt(unittest.TestCase):
    @unittest.skipUnless(KOTLINC and JAVA, "no kotlinc/java available")
    def test_rt_checks(self):
        with tempfile.TemporaryDirectory() as d:
            shutil.copy(RT_KT_SRC, Path(d) / "newr_rt.kt")
            shutil.copy(RT_CHECKS_SRC, Path(d) / RT_CHECKS_SRC.name)
            r = subprocess.run(
                [KOTLINC, "newr_rt.kt", "RtChecks.kt",
                 "-include-runtime", "-d", "rtchecks.jar"],
                capture_output=True, text=True, cwd=d, env=_kt_env())
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            r = subprocess.run([JAVA, "-jar", "rtchecks.jar"],
                               capture_output=True, text=True, cwd=d,
                               env=_kt_env())
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertIn("checks passed", r.stdout)
            self.assertNotIn("FAIL", r.stdout)


if __name__ == "__main__":
    unittest.main()
