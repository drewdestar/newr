"""Tests for the C# runtime (compiler/newr_rt.cs, M4).

Mirrors tests/test_newr_rt_js.py: build the runtime together with
the driver in tests/cs/RtChecks.cs in a throwaway console project
and run it. Skips when no .NET SDK is available (~/.dotnet or PATH).
"""

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RT_CS_SRC = ROOT / "compiler" / "newr_rt.cs"
RT_CHECKS_SRC = ROOT / "tests" / "cs" / "RtChecks.cs"

CSPROJ = """<Project Sdk="Microsoft.NET.Sdk">
  <PropertyGroup>
    <OutputType>Exe</OutputType>
    <TargetFramework>net8.0</TargetFramework>
    <Nullable>disable</Nullable>
    <ImplicitUsings>disable</ImplicitUsings>
    <AssemblyName>RtChecks</AssemblyName>
  </PropertyGroup>
</Project>
"""


def _find_dotnet():
    p = Path.home() / ".dotnet" / "dotnet"
    if p.exists():
        return str(p)
    return shutil.which("dotnet")


DOTNET = _find_dotnet()


class TestNewrRtCs(unittest.TestCase):
    @unittest.skipUnless(DOTNET, "no .NET SDK available")
    def test_rt_checks(self):
        env = dict(os.environ)
        env["DOTNET_CLI_TELEMETRY_OPTOUT"] = "1"
        with tempfile.TemporaryDirectory() as d:
            for src in (RT_CS_SRC, RT_CHECKS_SRC):
                shutil.copy(src, Path(d) / src.name)
            (Path(d) / "RtChecks.csproj").write_text(
                CSPROJ, encoding="utf-8")
            r = subprocess.run(
                [DOTNET, "run", "--project", "RtChecks.csproj"],
                capture_output=True, text=True, cwd=d, env=env,
                timeout=300)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertIn("checks passed", r.stdout)
            self.assertNotIn("FAIL", r.stdout)


if __name__ == "__main__":
    unittest.main()
