"""Tests for the js app packager (compiler/app_pack_js.py, M8d
Phase 2, SPEC.md section 22.2).

The smoke tests build examples/hello.newr for target js via ./newr
(the TestBuildApp pattern in tests/test_cli.py), call `package_js`
directly, then run the artifact both via `node <stem>.js` and via
`./run.sh`, asserting "Hello, world!" in both. Temp dirs live under
SCRATCH_ROOT (~/workspace, never /tmp: it is a 512 MB tmpfs), and
every subprocess timeout is generous: this VM has a small CPU and
several agents run in parallel.
"""

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
from compiler.app_pack_js import (
    TOOLCHAIN,
    artifact_bytes,
    artifact_sha256,
    package_js,
)

# Generous timeouts: the VM CPU is small and several agents run in
# parallel, so a build or a node run must never fail on a tight
# clock.
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


class TestPackageJsHello(unittest.TestCase):
    def test_hello_runs_via_node_and_run_sh(self):
        with temp_dir() as d:
            src = ROOT / "examples" / "hello.newr"
            f = Path(d) / "hello.newr"
            shutil.copyfile(src, f)
            r = run_cli("build", str(f), "--target", "js", cwd=d)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertTrue((Path(d) / "hello.js").is_file())
            # hello does not use the runtime: no newr_rt.js emitted.
            self.assertFalse((Path(d) / "newr_rt.js").exists())
            dist_dir = Path(d) / "dist" / "hello-js"
            artifact = package_js(
                app_name="hello", emitted_dir=Path(d),
                emitted_files=["hello.js"], main_file="hello.js",
                dist_dir=dist_dir)
            # The js artifact is a directory: package_js returns
            # dist_dir itself.
            self.assertEqual(artifact, dist_dir)
            self.assertTrue(artifact.is_dir())
            self.assertEqual(
                sorted(p.name for p in artifact.iterdir()),
                ["hello.js", "run.sh"])
            run_sh = artifact / "run.sh"
            self.assertEqual(
                run_sh.read_text(encoding="utf-8"),
                "#!/bin/sh\n"
                "exec node \"$(dirname \"$0\")/hello.js\" \"$@\"\n")
            self.assertTrue(run_sh.stat().st_mode & stat.S_IXUSR)
            node_run = subprocess.run(
                ["node", "hello.js"], capture_output=True, text=True,
                cwd=artifact, timeout=RUN_TIMEOUT)
            self.assertEqual(node_run.returncode, 0, node_run.stderr)
            self.assertEqual(node_run.stdout, "Hello, world!\n")
            sh_run = subprocess.run(
                ["./run.sh"], capture_output=True, text=True,
                cwd=artifact, timeout=RUN_TIMEOUT)
            self.assertEqual(sh_run.returncode, 0, sh_run.stderr)
            self.assertEqual(sh_run.stdout, "Hello, world!\n")
            # Receipt definitions for a directory artifact: bytes
            # are the total of all files inside, and the sha256 is
            # stable across recomputation.
            total = sum(p.stat().st_size
                        for p in artifact.iterdir() if p.is_file())
            self.assertEqual(artifact_bytes(artifact), total)
            digest = artifact_sha256(artifact)
            self.assertEqual(len(digest), 64)
            self.assertEqual(artifact_sha256(artifact), digest)

    def test_runtime_program_copies_newr_rt_and_runs(self):
        with temp_dir() as d:
            f = Path(d) / "calc.newr"
            f.write_text("let x: int = 2 + 3\nprint x\n",
                         encoding="utf-8")
            r = run_cli("build", str(f), "--target", "js", cwd=d)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertTrue((Path(d) / "newr_rt.js").is_file())
            dist_dir = Path(d) / "dist" / "calc-js"
            artifact = package_js(
                app_name="calc", emitted_dir=Path(d),
                emitted_files=["calc.js", "newr_rt.js"],
                main_file="calc.js", dist_dir=dist_dir)
            self.assertEqual(
                sorted(p.name for p in artifact.iterdir()),
                ["calc.js", "newr_rt.js", "run.sh"])
            node_run = subprocess.run(
                ["node", "calc.js"], capture_output=True, text=True,
                cwd=artifact, timeout=RUN_TIMEOUT)
            self.assertEqual(node_run.returncode, 0, node_run.stderr)
            self.assertEqual(node_run.stdout, "5\n")
            sh_run = subprocess.run(
                ["./run.sh"], capture_output=True, text=True,
                cwd=artifact, timeout=RUN_TIMEOUT)
            self.assertEqual(sh_run.returncode, 0, sh_run.stderr)
            self.assertEqual(sh_run.stdout, "5\n")


class TestPackageJsFailures(unittest.TestCase):
    def test_missing_emitted_file_raises_app_pack_error(self):
        with temp_dir() as d:
            with self.assertRaises(AppPackError) as ctx:
                package_js(
                    app_name="hello", emitted_dir=Path(d),
                    emitted_files=["hello.js"], main_file="hello.js",
                    dist_dir=Path(d) / "dist" / "hello-js")
            self.assertIn("hello.js", str(ctx.exception))

    def test_missing_main_file_in_list_raises_app_pack_error(self):
        with temp_dir() as d:
            f = Path(d) / "hello.js"
            f.write_text("console.log(\"hi\");\n", encoding="utf-8")
            with self.assertRaises(AppPackError) as ctx:
                package_js(
                    app_name="hello", emitted_dir=Path(d),
                    emitted_files=["other.js"], main_file="hello.js",
                    dist_dir=Path(d) / "dist" / "hello-js")
            self.assertIn("hello.js", str(ctx.exception))

    def test_missing_node_raises_app_pack_error(self):
        # node is not findable when PATH has no node in it.
        with temp_dir() as d:
            empty_bin = Path(d) / "empty-bin"
            empty_bin.mkdir()
            with mock.patch.dict(os.environ,
                                {"PATH": str(empty_bin)}):
                with self.assertRaises(AppPackError) as ctx:
                    package_js(
                        app_name="hello", emitted_dir=Path(d),
                        emitted_files=["hello.js"],
                        main_file="hello.js",
                        dist_dir=Path(d) / "dist" / "hello-js")
            self.assertIn("node", str(ctx.exception))
            self.assertIn("install", str(ctx.exception))

    def test_toolchain_names_live_node_version(self):
        # TOOLCHAIN is detected live, not hardcoded: it matches
        # `node --version` on this machine.
        run = subprocess.run(["node", "--version"],
                             capture_output=True, text=True,
                             timeout=RUN_TIMEOUT)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(TOOLCHAIN["node"],
                         run.stdout.strip().removeprefix("v"))


if __name__ == "__main__":
    unittest.main()
