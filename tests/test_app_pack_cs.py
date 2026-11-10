"""Tests for the cs app packager (compiler/app_pack_cs.py, M8d
Phase 2, SPEC.md section 22.2).

The smoke tests build examples/hello.newr for target cs via ./newr
(the TestBuildApp pattern in tests/test_cli.py), call `package_cs`
directly, then run the artifact and assert "Hello, world!". The
artifact is a single self-contained file (EVAL.md section 12,
verified live: `dotnet publish -p:PublishSingleFile=true` for
hello produces one 66,800,454 byte executable that runs and prints
"Hello, world!"). Temp dirs live under SCRATCH_ROOT (~/workspace,
never /tmp: it is a 512 MB tmpfs), and every subprocess timeout is
generous: dotnet publish took 11.0 s for hello cold (9.7 s in the
eval), a cold NuGet restore can take 96 s, this VM has a small CPU,
and several agents run in parallel.
"""

import hashlib
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
LAUNCHER = ROOT / "newr"

from compiler.app_pack import AppPackError, SCRATCH_ROOT
from compiler.app_pack_cs import (
    TOOLCHAIN,
    artifact_bytes,
    artifact_sha256,
    package_cs,
)

# dotnet lives in ~/.dotnet on this VM (installed for M4), which is
# not on the default PATH; fall back to the PATH when it is not
# there (e.g. CI, where setup-dotnet provides it). Same resolution
# as tests/test_emitter_cs.py.
_DOTNET_ROOT = Path(os.environ["DOTNET_ROOT"]) \
    if os.environ.get("DOTNET_ROOT") else (Path.home() / ".dotnet")
DOTNET = shutil.which("dotnet", path=str(_DOTNET_ROOT)) \
    or shutil.which("dotnet")

# Generous timeouts: the VM CPU is small and several agents run in
# parallel, so a build or a run must never fail on a tight clock.
# The publish itself has its own 600 s timeout inside package_cs.
BUILD_TIMEOUT = 300
RUN_TIMEOUT = 120


def run_cli(*args, cwd=None):
    return subprocess.run([sys.executable, str(LAUNCHER), *args],
                          capture_output=True, text=True, cwd=cwd,
                          timeout=BUILD_TIMEOUT)


def temp_dir():
    """A temp dir under ~/workspace (SCRATCH_ROOT), never /tmp."""
    SCRATCH_ROOT.mkdir(parents=True, exist_ok=True)
    return tempfile.TemporaryDirectory(dir=str(SCRATCH_ROOT))


def _staging_dirs(app_name):
    """Staging dirs for one app name under SCRATCH_ROOT."""
    return {p for p in SCRATCH_ROOT.iterdir()
            if p.is_dir() and p.name.startswith(f"{app_name}-cs-")}


@unittest.skipUnless(DOTNET, "dotnet is not installed")
class TestPackageCsHello(unittest.TestCase):
    def test_hello_single_file_runs(self):
        with temp_dir() as d:
            src = ROOT / "examples" / "hello.newr"
            f = Path(d) / "hello.newr"
            shutil.copyfile(src, f)
            r = run_cli("build", str(f), "--target", "cs", cwd=d)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertTrue((Path(d) / "hello.cs").is_file())
            self.assertTrue((Path(d) / "hello.csproj").is_file())
            # hello does not use the runtime: no newr_rt.cs emitted.
            self.assertFalse((Path(d) / "newr_rt.cs").exists())
            dist_dir = Path(d) / "dist" / "hello-cs"
            # Failure-path tests keep their staging dirs by design
            # (for diagnosis), and sibling agents share this root,
            # so snapshot before the call and assert this call adds
            # none: the staging dir is removed on success.
            before = _staging_dirs("hello")
            artifact = package_cs(
                app_name="hello", emitted_dir=Path(d),
                emitted_files=["hello.cs", "hello.csproj"],
                main_file="hello.cs", dist_dir=dist_dir)
            self.assertEqual(_staging_dirs("hello"), before)
            # The cs artifact is a single self-contained file.
            self.assertEqual(artifact, dist_dir / "hello")
            self.assertTrue(artifact.is_file())
            self.assertEqual(
                sorted(p.name for p in dist_dir.iterdir()),
                ["hello"])
            self.assertTrue(artifact.stat().st_mode & stat.S_IXUSR)
            # Self-contained single file: the .NET runtime is
            # bundled, so the artifact is tens of MB, not KB.
            self.assertGreater(artifact.stat().st_size, 50_000_000)
            # Run from another directory, with dotnet nowhere on
            # PATH: a self-contained artifact must not need it.
            run = subprocess.run(
                [str(artifact)], capture_output=True, text=True,
                cwd=d, env={"PATH": "/usr/bin:/bin",
                            "HOME": str(Path.home())},
                timeout=RUN_TIMEOUT)
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertEqual(run.stdout, "Hello, world!\n")
            # Receipt definitions for a file artifact: bytes are
            # the file size, sha256 is the file hash, both stable.
            self.assertEqual(artifact_bytes(artifact),
                             artifact.stat().st_size)
            digest = artifact_sha256(artifact)
            self.assertEqual(len(digest), 64)
            self.assertEqual(
                digest,
                hashlib.sha256(artifact.read_bytes()).hexdigest())
            self.assertEqual(artifact_sha256(artifact), digest)

    def test_runtime_program_copies_newr_rt_and_runs(self):
        with temp_dir() as d:
            f = Path(d) / "calc.newr"
            f.write_text("let x: int = 2 + 3\nprint x\n",
                         encoding="utf-8")
            r = run_cli("build", str(f), "--target", "cs", cwd=d)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertTrue((Path(d) / "newr_rt.cs").is_file())
            dist_dir = Path(d) / "dist" / "calc-cs"
            artifact = package_cs(
                app_name="calc", emitted_dir=Path(d),
                emitted_files=["calc.cs", "calc.csproj",
                               "newr_rt.cs"],
                main_file="calc.cs", dist_dir=dist_dir)
            self.assertTrue(artifact.is_file())
            self.assertEqual(artifact.name, "calc")
            run = subprocess.run(
                [str(artifact)], capture_output=True, text=True,
                cwd=d, timeout=RUN_TIMEOUT)
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertEqual(run.stdout, "5\n")


class TestPackageCsFailures(unittest.TestCase):
    def test_missing_emitted_file_raises_app_pack_error(self):
        with temp_dir() as d:
            with self.assertRaises(AppPackError) as ctx:
                package_cs(
                    app_name="hello", emitted_dir=Path(d),
                    emitted_files=["hello.cs", "hello.csproj"],
                    main_file="hello.cs",
                    dist_dir=Path(d) / "dist" / "hello-cs")
            self.assertIn("hello.cs", str(ctx.exception))

    def test_missing_main_file_in_list_raises_app_pack_error(self):
        with temp_dir() as d:
            f = Path(d) / "hello.cs"
            f.write_text("class hello {}\n", encoding="utf-8")
            with self.assertRaises(AppPackError) as ctx:
                package_cs(
                    app_name="hello", emitted_dir=Path(d),
                    emitted_files=["other.cs", "hello.csproj"],
                    main_file="hello.cs",
                    dist_dir=Path(d) / "dist" / "hello-cs")
            self.assertIn("hello.cs", str(ctx.exception))

    def test_missing_csproj_raises_app_pack_error(self):
        with temp_dir() as d:
            f = Path(d) / "hello.cs"
            f.write_text("class hello {}\n", encoding="utf-8")
            with self.assertRaises(AppPackError) as ctx:
                package_cs(
                    app_name="hello", emitted_dir=Path(d),
                    emitted_files=["hello.cs"], main_file="hello.cs",
                    dist_dir=Path(d) / "dist" / "hello-cs")
            self.assertIn("csproj", str(ctx.exception))

    def test_missing_dotnet_raises_app_pack_error(self):
        # dotnet is not findable when neither ~/.dotnet (via
        # DOTNET_ROOT) nor PATH has it in it.
        with temp_dir() as d:
            empty_bin = Path(d) / "empty-bin"
            empty_bin.mkdir()
            with mock.patch.dict(os.environ,
                                {"PATH": str(empty_bin),
                                 "DOTNET_ROOT": str(empty_bin)}):
                with self.assertRaises(AppPackError) as ctx:
                    package_cs(
                        app_name="hello", emitted_dir=Path(d),
                        emitted_files=["hello.cs", "hello.csproj"],
                        main_file="hello.cs",
                        dist_dir=Path(d) / "dist" / "hello-cs")
            self.assertIn("dotnet", str(ctx.exception))
            self.assertIn("install", str(ctx.exception))

    @unittest.skipUnless(DOTNET, "dotnet is not installed")
    def test_toolchain_names_live_dotnet_version(self):
        # TOOLCHAIN is detected live, not hardcoded: it matches
        # `dotnet --version` on this machine.
        run = subprocess.run([DOTNET, "--version"],
                             capture_output=True, text=True,
                             timeout=RUN_TIMEOUT)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(TOOLCHAIN["dotnet"],
                         run.stdout.strip().splitlines()[0])


class TestArtifactHelpers(unittest.TestCase):
    def test_file_and_directory_definitions(self):
        with temp_dir() as d:
            base = Path(d) / "tree"
            (base / "sub").mkdir(parents=True)
            a = base / "a.txt"
            a.write_bytes(b"hello")
            b = base / "sub" / "b.txt"
            b.write_bytes(b"world!")
            # File case: size and plain file hash.
            self.assertEqual(artifact_bytes(a), 5)
            self.assertEqual(
                artifact_sha256(a),
                hashlib.sha256(b"hello").hexdigest())
            # Directory case: total bytes of all files inside,
            # and a hash stable across recomputation.
            self.assertEqual(artifact_bytes(base), 11)
            digest = artifact_sha256(base)
            self.assertEqual(len(digest), 64)
            self.assertEqual(artifact_sha256(base), digest)
            # The directory hash changes when a file changes.
            b.write_bytes(b"world?")
            self.assertNotEqual(artifact_sha256(base), digest)
            self.assertEqual(artifact_bytes(base), 11)


if __name__ == "__main__":
    unittest.main()
