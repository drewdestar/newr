"""Python driver for the Node-side runtime tests (tests/js/rt_tests.js).

The runtime's rules are tested in JavaScript (node:assert) because
they are JavaScript semantics (BigInt, float rendering, exact-decimal
rounding). This module just runs that file under node and asserts
exit 0 — the per-rule assertions live in the JS file.
"""

import shutil
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
NODE = shutil.which("node")


@unittest.skipUnless(NODE, "node is not installed")
class TestNewrRtJs(unittest.TestCase):
    def test_rt_js_suite(self):
        r = subprocess.run(
            [NODE, str(ROOT / "tests" / "js" / "rt_tests.js")],
            capture_output=True, text=True, cwd=ROOT)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("checks passed", r.stdout)


if __name__ == "__main__":
    unittest.main()
