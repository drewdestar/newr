"""End-to-end `newr build --app` tests for all seven targets
(SPEC.md section 22.2, M8d Phase 2 integration).

Builds examples/hello.newr for each target through ./newr, runs
the artifact, and checks the receipt: name, version, target,
artifact name, artifact_bytes and sha256 recomputed with the
shared helpers, and the toolchain keys for that target.

Temp dirs live under SCRATCH_ROOT (`~/workspace`, never `/tmp`:
it is a 512 MB tmpfs), and every subprocess runs with TMPDIR
under `~/workspace` and PATH with every tool bin dir first
(JDK, kotlinc, dotnet, cargo, dart). Timeouts are loose on
purpose: kotlinc and dotnet publish are slow, and the build VM
has a small CPU with several agents running in parallel.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from compiler.app_pack import (
    SCRATCH_ROOT, AppPackError, _artifact_bytes, _artifact_sha256,
    package_app,
)
from compiler.init import PROJECT_VERSION

ROOT = Path(__file__).resolve().parent.parent
LAUNCHER = ROOT / "newr"

HOME = Path.home()
_JAVA_BIN = HOME / "jdk-21" / "bin"
_KOTLIN_BIN = HOME / "kotlin" / "kotlinc" / "bin"
_DOTNET_BIN = HOME / ".dotnet"
_CARGO_BIN = HOME / ".cargo" / "bin"
_DART_BIN = HOME / "dart-sdk" / "dart-sdk" / "bin"

JAVA = shutil.which("java", path=str(_JAVA_BIN)) or shutil.which("java")
JAVAC = shutil.which("javac", path=str(_JAVA_BIN)) \
    or shutil.which("javac")
KOTLINC = shutil.which("kotlinc", path=str(_KOTLIN_BIN))
NODE = shutil.which("node")
DOTNET = shutil.which("dotnet", path=str(_DOTNET_BIN)) \
    or shutil.which("dotnet")
RUSTC = shutil.which("rustc", path=str(_CARGO_BIN)) \
    or shutil.which("rustc")
DART = shutil.which("dart", path=str(_DART_BIN)) \
    or shutil.which("dart")

#: Loose timeouts: the VM CPU is small and several agents run in
#: parallel, so a build or an artifact run must not fail on a slow
#: tick. The kt build alone can take over 100 seconds under load.
BUILD_TIMEOUT_SECONDS = 900
RUN_TIMEOUT_SECONDS = 300


def _env() -> dict[str, str]:
    """The env for a subprocess in these tests: PATH with every
    tool bin dir first, TMPDIR under `~/workspace`."""
    SCRATCH_ROOT.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    bins = [str(_JAVA_BIN), str(_KOTLIN_BIN), str(_DOTNET_BIN),
            str(_CARGO_BIN), str(_DART_BIN)]
    env["PATH"] = os.pathsep.join(bins + [env.get("PATH", "")])
    env["TMPDIR"] = str(SCRATCH_ROOT)
    return env


def _run_cli(*args, cwd):
    return subprocess.run([sys.executable, str(LAUNCHER), *args],
                          capture_output=True, text=True, cwd=cwd,
                          timeout=BUILD_TIMEOUT_SECONDS, env=_env())


#: Per target: the tool availability flag, the artifact path
#: relative to the work dir, the command that runs the artifact
#: (as a function of the work dir and the java binary), and the
#: toolchain keys the receipt must carry.
TARGETS = {
    "py": {
        "available": True,
        "artifact": "dist/hello-py/hello.pyz",
        "run": lambda work, java: [sys.executable,
                                   str(work / "dist/hello-py"
                                       "/hello.pyz")],
        "toolchain": {"python", "zipapp"},
    },
    "js": {
        "available": NODE is not None,
        "artifact": "dist/hello-js",
        "run": lambda work, java: ["node",
                                   str(work / "dist/hello-js"
                                       "/hello.js")],
        "toolchain": {"node"},
    },
    "java": {
        "available": bool(JAVA and JAVAC),
        "artifact": "dist/hello-java/hello.jar",
        "run": lambda work, java: [java, "-jar",
                                   str(work / "dist/hello-java"
                                       "/hello.jar")],
        "toolchain": {"java"},
    },
    "kt": {
        "available": bool(KOTLINC and JAVA),
        "artifact": "dist/hello-kt/hello.jar",
        "run": lambda work, java: [java, "-jar",
                                   str(work / "dist/hello-kt"
                                       "/hello.jar")],
        "toolchain": {"kotlin", "java"},
    },
    "rs": {
        "available": RUSTC is not None,
        "artifact": "dist/hello-rs/hello",
        "run": lambda work, java: [str(work / "dist/hello-rs"
                                       "/hello")],
        "toolchain": {"rustc", "cargo"},
    },
    "cs": {
        "available": DOTNET is not None,
        "artifact": "dist/hello-cs/hello",
        "run": lambda work, java: [str(work / "dist/hello-cs"
                                       "/hello")],
        "toolchain": {"dotnet"},
    },
    "dart": {
        "available": DART is not None,
        "artifact": "dist/hello-dart/hello",
        "run": lambda work, java: [str(work / "dist/hello-dart"
                                       "/hello")],
        "toolchain": {"dart"},
    },
}


class TestBuildAppTargets(unittest.TestCase):
    def test_package_app_unknown_target_raises(self):
        # The dispatch else-branch: an unknown target is an
        # AppPackError naming the target, never a traceback. The
        # CLI validates the target first (exit 1), so this branch
        # is only reachable through the API.
        with tempfile.TemporaryDirectory(
                prefix="test-build-app-",
                dir=str(SCRATCH_ROOT)) as d:
            work = Path(d)
            prog = work / "hello.newr"
            shutil.copyfile(ROOT / "examples" / "hello.newr", prog)
            with self.assertRaises(AppPackError) as ctx:
                package_app(
                    target="bogus", app_name="hello",
                    version=PROJECT_VERSION, manifest=None,
                    main_path=prog, source_files=[prog],
                    emitted_dir=work, emitted_files=["hello.py"],
                    main_file="hello.py",
                    dist_dir=work / "dist" / "hello-bogus")
            self.assertIn("bogus", str(ctx.exception))
            self.assertNotIn("Traceback", str(ctx.exception))

    def test_build_app_hello_all_targets(self):
        # One subTest per target, so a slow or failing target
        # does not hide the result of the others.
        for target, spec in TARGETS.items():
            with self.subTest(target=target):
                if not spec["available"]:
                    self.skipTest(f"toolchain for {target} "
                                  "is not installed")
                with tempfile.TemporaryDirectory(
                        prefix="test-build-app-",
                        dir=str(SCRATCH_ROOT)) as d:
                    work = Path(d)
                    prog = work / "hello.newr"
                    shutil.copyfile(ROOT / "examples" / "hello.newr",
                                    prog)
                    built = _run_cli("build", str(prog), "--target",
                                     target, "--app", cwd=work)
                    self.assertEqual(built.returncode, 0,
                                     built.stdout + built.stderr)
                    artifact = work / spec["artifact"]
                    self.assertTrue(artifact.exists(),
                                    f"artifact missing: {artifact}")
                    receipt_path = (work / "dist"
                                    / f"hello-{target}"
                                    / "BUILD-RECEIPT.json")
                    self.assertTrue(receipt_path.is_file(),
                                    "receipt missing")
                    receipt = json.loads(
                        receipt_path.read_text(encoding="utf-8"))
                    self.assertEqual(receipt["name"], "hello")
                    self.assertEqual(receipt["version"],
                                     PROJECT_VERSION)
                    self.assertEqual(receipt["target"], target)
                    self.assertEqual(receipt["artifact"],
                                     artifact.name)
                    self.assertEqual(
                        receipt["artifact_bytes"],
                        _artifact_bytes(artifact))
                    self.assertEqual(
                        receipt["sha256"],
                        _artifact_sha256(artifact))
                    self.assertEqual(set(receipt["toolchain"]),
                                     spec["toolchain"])
                    self.assertEqual(
                        [s["path"] for s in receipt["sources"]],
                        ["hello.newr"])
                    run = subprocess.run(
                        spec["run"](work, JAVA), capture_output=True,
                        text=True, cwd=work,
                        timeout=RUN_TIMEOUT_SECONDS, env=_env())
                    self.assertEqual(run.returncode, 0, run.stderr)
                    self.assertEqual(run.stdout, "Hello, world!\n")


if __name__ == "__main__":
    unittest.main()
