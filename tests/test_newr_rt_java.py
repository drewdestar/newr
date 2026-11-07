"""Tests for the Java runtime (compiler/newr_rt.java, M4).

Mirrors tests/test_newr_rt_js.py: compile the runtime together with
the driver in tests/java/RtChecks.java and run it. Skips when no
JDK is available (JAVA_HOME, ~/jdk-21/bin, or PATH).
"""

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RT_JAVA_SRC = ROOT / "compiler" / "newr_rt.java"
RT_CHECKS_SRC = ROOT / "tests" / "java" / "RtChecks.java"


def _find_tool(name):
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


JAVAC = _find_tool("javac")
JAVA = _find_tool("java")


class TestNewrRtJava(unittest.TestCase):
    @unittest.skipUnless(JAVAC and JAVA, "no JDK available")
    def test_rt_checks(self):
        with tempfile.TemporaryDirectory() as d:
            # The runtime ships as NewrRt.java: javac requires the
            # file name to match the public class (the repo source
            # stays compiler/newr_rt.java by convention; the CLI
            # renames on copy, same as here).
            shutil.copy(RT_JAVA_SRC, Path(d) / "NewrRt.java")
            shutil.copy(RT_CHECKS_SRC, Path(d) / RT_CHECKS_SRC.name)
            r = subprocess.run(
                [JAVAC, "NewrRt.java", "RtChecks.java"],
                capture_output=True, text=True, cwd=d)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            r = subprocess.run([JAVA, "RtChecks"],
                               capture_output=True, text=True, cwd=d)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertIn("checks passed", r.stdout)
            self.assertNotIn("FAIL", r.stdout)


if __name__ == "__main__":
    unittest.main()
