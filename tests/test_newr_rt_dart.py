"""Tests for the Dart runtime (compiler/newr_rt.dart, M5).

Mirrors tests/test_newr_rt_java.py: copy the runtime together with
the driver in tests/dart/RtChecks.dart into a temp dir and run it
with dart. Skips when no Dart SDK is available (~/dart-sdk/bin, or
PATH).
"""

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RT_DART_SRC = ROOT / "compiler" / "newr_rt.dart"
RT_CHECKS_SRC = ROOT / "tests" / "dart" / "RtChecks.dart"


def _find_dart():
    """Find the Dart SDK: ~/dart-sdk/bin/dart, then PATH."""
    p = Path.home() / "dart-sdk" / "dart-sdk" / "bin" / "dart"
    if p.exists():
        return str(p)
    return shutil.which("dart")


DART = _find_dart()


class TestNewrRtDart(unittest.TestCase):
    @unittest.skipUnless(DART, "no Dart SDK available")
    def test_rt_checks(self):
        with tempfile.TemporaryDirectory() as d:
            shutil.copy(RT_DART_SRC, Path(d) / "newr_rt.dart")
            shutil.copy(RT_CHECKS_SRC, Path(d) / RT_CHECKS_SRC.name)
            r = subprocess.run(
                [DART, "analyze", "newr_rt.dart", "RtChecks.dart"],
                capture_output=True, text=True, cwd=d)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            r = subprocess.run([DART, "RtChecks.dart"],
                               capture_output=True, text=True, cwd=d)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertIn("71 checks passed", r.stdout)
            self.assertNotIn("FAIL", r.stdout)


if __name__ == "__main__":
    unittest.main()
