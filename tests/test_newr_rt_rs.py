"""Tests for the Rust runtime (compiler/newr_rt.rs, M5).

Mirrors tests/test_newr_rt_java.py: compile the runtime together
with the driver in tests/rs/RtChecks.rs and run it. Skips when no
rustc is available (~/.cargo/bin, CARGO_HOME, or PATH).
"""

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RT_RS_SRC = ROOT / "compiler" / "newr_rt.rs"
RT_CHECKS_SRC = ROOT / "tests" / "rs" / "RtChecks.rs"


def _find_tool(name):
    """Find a Rust tool: CARGO_HOME/bin, ~/.cargo/bin, then PATH."""
    cargo_home = os.environ.get("CARGO_HOME")
    if cargo_home:
        p = Path(cargo_home) / "bin" / name
        if p.exists():
            return str(p)
    p = Path.home() / ".cargo" / "bin" / name
    if p.exists():
        return str(p)
    return shutil.which(name)


RUSTC = _find_tool("rustc")


class TestNewrRtRs(unittest.TestCase):
    @unittest.skipUnless(RUSTC, "no rustc available")
    def test_rt_checks(self):
        with tempfile.TemporaryDirectory() as d:
            shutil.copy(RT_RS_SRC, Path(d) / "newr_rt.rs")
            shutil.copy(RT_CHECKS_SRC, Path(d) / RT_CHECKS_SRC.name)
            r = subprocess.run(
                [RUSTC, "RtChecks.rs", "-o", "RtChecks"],
                capture_output=True, text=True, cwd=d)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            r = subprocess.run(["./RtChecks"],
                               capture_output=True, text=True, cwd=d)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertIn("checks passed", r.stdout)
            self.assertNotIn("FAIL", r.stdout)
            self.assertNotIn("panicked", r.stdout)


if __name__ == "__main__":
    unittest.main()
