"""App packaging for `newr build --app --target dart` (SPEC.md section
22.2, M8d Phase 2).

The dart artifact depends on whether the program uses `db.*`:

- A plain program (no `pubspec.yaml` among the emitted files)
  builds with `dart compile exe <stem>.dart -o <name>` (EVAL.md
  section 4: 1.7 s, 6.5 MB for hello on this VM). The artifact is
  a single native binary, `<name>`, in `dist_dir`, and
  `package_dart` returns that binary path. The binary is built in
  the staging dir and then copied into `dist_dir`, so `dist_dir`
  only ever receives a complete artifact.
- A db program (the emitter writes a `pubspec.yaml` with sqlite3
  3.9.0 when the program uses `db.*`) runs `dart pub get` first,
  then builds with `dart build cli -t <stem>.dart -o <dir>`. The
  artifact is the bundle directory: `bin/<name>` plus
  `lib/libsqlite3.so` next to it, copied into `dist_dir`, and
  `package_dart` returns `dist_dir` itself (the js packager's
  rule for a directory artifact). The binary inside the bundle is
  renamed from the entry stem to the app name when they differ;
  the bundled `.so` is found relative to the binary, so the
  rename is safe and the bundle still runs from any directory.

Why `dart build cli` for db programs, not `dart compile exe`
(the EVAL.md section 4 and SPEC.md section 22.2 command): on
this VM, Dart SDK 3.13.7, `dart compile exe` does NOT bundle
the sqlite3 native asset. Verified live 10 November 2026: the
compile exits 0 and produces a 6.7 MB binary, but running it
fails with exit 255, "No asset with id
'package:sqlite3/src/ffi/libsqlite3.g.dart' found. No available
native assets", even when run from the project dir, and even
after a fresh `dart pub get` in a clean directory. `dart build
cli` runs the build hooks, copies the asset into `lib/`, and the
bundled binary runs and prints the expected output (16 s for
the todo program, 8.2 MB bundle). Shipping the `compile exe`
binary for a db program would ship a binary that cannot open a
database, so the packager uses `dart build cli` for any program
with a pubspec instead. (A `LD_PRELOAD` of the system
libsqlite3.so.0 makes the broken binary run, but that depends
on a system library at runtime and is not used.) This is a
deviation from the EVAL.md table, recorded here so the wiring
agent and the build report can see it.

The package step stages the emitted files into a scratch dir
under `SCRATCH_ROOT` (never `/tmp`: it is a 512 MB tmpfs), runs
the builds with `TMPDIR` pointed at that staging dir, and
removes the staging dir after a successful package, keeping it
on failure for diagnosis, mirroring the py packager in
`app_pack.py`.

`TOOLCHAIN` is the toolchain that ran, detected live at import
time from `dart --version` (the agent that wires this packager
into `package_app` passes it into the build receipt;
`app_pack.py` currently hardcodes the py toolchain, and the
wiring agent adds the per-target parameter). When dart is
missing the value is "unknown" and `package_dart` raises
`AppPackError` before any receipt is built, so "unknown" never
reaches a receipt.

A packager failure raises `AppPackError` naming the failing
command; the CLI turns it into an exit-4 build error, never a
traceback. The build subprocesses run with no timeout: a `dart
pub get` plus `dart build cli` run takes tens of seconds on a
loaded VM, and a timeout short enough to catch a hang would also
kill a legitimate slow build. The caller (CLI, test, packaging
suite) owns the timeout.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from .app_pack import AppPackError, SCRATCH_ROOT

#: The commands the dart packager runs (used in failure
#: diagnostics so the user can reproduce the step by hand).
COMPILE_COMMAND = "dart compile exe <main.dart> -o <name>"
PUB_GET_COMMAND = "dart pub get"
BUILD_CLI_COMMAND = "dart build cli -t <main.dart> -o <dir>"

#: How much of a failed build's stderr to keep in the diagnostic:
#: enough to name the real error, short enough to stay readable.
_STDERR_TAIL_CHARS = 2000


def _find_tool(name: str) -> str | None:
    """The path of the dart binary: the `DART_HOME` directory
    first, then `~/dart-sdk/dart-sdk/bin`, then PATH (the same
    rule as `compiler/test.py` `_which`, which passes
    `DART_HOME` and `dart-sdk/dart-sdk/bin`). None when dart is
    nowhere to be found."""
    dart_home = os.environ.get("DART_HOME")
    if dart_home:
        found = shutil.which(name, path=dart_home)
        if found is not None:
            return found
    found = shutil.which(
        name, path=str(Path.home() / "dart-sdk" / "dart-sdk" / "bin"))
    if found is not None:
        return found
    return shutil.which(name)


#: The resolved dart path, found live at import time (None when
#: dart is missing; `package_dart` raises before building then).
DART = _find_tool("dart")


def _detect_version(binary: str | None) -> str | None:
    """The version number of the dart binary (`3.13.7` style),
    detected live from `dart --version`, or None when the binary
    is missing or the check fails. The full output looks like
    `Dart SDK version: 3.13.7 (stable) (Thu Oct 22 14:28:57 2026
    -0700) on "linux_x64"`; only the number is kept. The version
    line arrives on stdout on this SDK."""
    if binary is None:
        return None
    try:
        run = subprocess.run([binary, "--version"],
                             capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if run.returncode != 0:
        return None
    text = (run.stdout + run.stderr).strip()
    match = re.search(r"(\d+\.\d+\.\d+)", text)
    if match is not None:
        return match.group(1)
    return text or None


#: The toolchain that ran, detected live at import time from
#: `dart --version`. When dart is missing the value is
#: "unknown" and `package_dart` raises `AppPackError` before any
#: receipt is built, so "unknown" never reaches a receipt.
_TOOLCHAIN_VERSION = _detect_version(DART)
TOOLCHAIN = {"dart": _TOOLCHAIN_VERSION
             if _TOOLCHAIN_VERSION is not None else "unknown"}


def _require_dart() -> str:
    """The live dart path, or raise `AppPackError` naming what to
    install when dart is missing."""
    dart = _find_tool("dart")
    if dart is None:
        raise AppPackError(
            f"{COMPILE_COMMAND} failed: no Dart SDK found "
            f"(checked DART_HOME, ~/dart-sdk/dart-sdk/bin, and "
            f"PATH); install the Dart SDK (3.13.7 per EVAL.md "
            f"section 11) and retry `newr build --app "
            f"--target dart`")
    return dart


def _stderr_tail(proc: subprocess.CompletedProcess) -> str:
    """The tail of a failed build's stderr, for the diagnostic."""
    err = (proc.stderr or "").strip()
    if len(err) > _STDERR_TAIL_CHARS:
        err = "...\n" + err[-_STDERR_TAIL_CHARS:]
    return err


def _build_env(staging: Path) -> dict:
    """The environment for a build subprocess: TMPDIR under
    `~/workspace` (the staging dir itself, removed after a
    successful package) instead of the 512 MB /tmp tmpfs."""
    env = dict(os.environ)
    env["TMPDIR"] = str(staging)
    return env


def _run_dart(argv: list[str], *, command: str, staging: Path,
              env: dict) -> None:
    """Run one dart step in the staging dir. A failure raises
    `AppPackError` naming the command, the exit code, and the
    tail of the tool output; the staging dir is kept for
    diagnosis by the caller."""
    try:
        proc = subprocess.run(argv, capture_output=True, text=True,
                              cwd=staging, env=env)
    except OSError as exc:
        raise AppPackError(
            f"{command} failed: cannot run dart ({argv[0]}): "
            f"{exc}; staging kept at {staging} for diagnosis"
            ) from exc
    if proc.returncode != 0:
        detail = _stderr_tail(proc) or proc.stdout.strip()
        raise AppPackError(
            f"{command} failed (dart exit {proc.returncode}): "
            f"{detail}; staging kept at {staging} for diagnosis")


def package_dart(*, app_name: str, emitted_dir: Path,
                 emitted_files: list[str], main_file: str,
                 dist_dir: Path) -> Path:
    """Build the dart app artifact for target dart.

    A plain program (no `pubspec.yaml` in `emitted_files`)
    builds with `dart compile exe` and returns the single binary
    `dist_dir/<name>`. A db program (a `pubspec.yaml` in
    `emitted_files`, sqlite3 3.9.0) runs `dart pub get` and then
    `dart build cli`, and returns `dist_dir` itself, which holds
    the bundle (`bin/<name>` plus `lib/libsqlite3.so`); see the
    module docstring for why `dart compile exe` cannot build a
    db program on this SDK. The staging dir is removed on
    success and kept on failure for diagnosis.
    """
    if not app_name or "/" in app_name or app_name in (".", ".."):
        raise AppPackError(
            f"cannot package target dart: the app name "
            f"{app_name!r} is not a bare file name, so no binary "
            f"named after it can be written to {dist_dir}")
    if Path(main_file).name != main_file:
        raise AppPackError(
            f"{COMPILE_COMMAND} failed: the emitted main file "
            f"{main_file!r} is not a bare file name, so it cannot "
            f"be staged for the dart build; rebuild the program")
    if main_file not in emitted_files:
        raise AppPackError(
            f"{COMPILE_COMMAND} failed: the emitted main file "
            f"{main_file!r} is not listed in the emitted files, so "
            f"the dart binary cannot be staged")
    has_pubspec = "pubspec.yaml" in emitted_files
    # Input validation (main file, emitted files on disk) runs
    # before the dart requirement, so a missing file is reported
    # by name even on a machine without dart (CI).
    SCRATCH_ROOT.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(
        prefix=f"{app_name}-dart-", dir=str(SCRATCH_ROOT)))
    try:
        for name in emitted_files:
            if Path(name).name != name:
                raise AppPackError(
                    f"{COMPILE_COMMAND} failed: emitted file "
                    f"{name!r} is not a bare file name, so it "
                    f"cannot be staged for the dart build; staging "
                    f"kept at {staging} for diagnosis")
            source = emitted_dir / name
            if not source.is_file():
                raise AppPackError(
                    f"{COMPILE_COMMAND} failed: emitted file "
                    f"{source} is missing, so the dart binary "
                    f"cannot be built; staging kept at {staging} "
                    f"for diagnosis")
            try:
                shutil.copyfile(source, staging / name)
            except OSError as exc:
                raise AppPackError(
                    f"{COMPILE_COMMAND} failed: cannot stage "
                    f"{source}: {exc}; staging kept at {staging} "
                    f"for diagnosis") from exc
        _require_dart()
        if has_pubspec:
            artifact = _build_db_bundle(
                app_name=app_name, staging=staging,
                main_file=main_file, dist_dir=dist_dir)
        else:
            artifact = _build_plain_binary(
                app_name=app_name, staging=staging,
                main_file=main_file, dist_dir=dist_dir)
    except BaseException:
        # Any failure above keeps the staging dir for diagnosis.
        raise
    else:
        shutil.rmtree(staging, ignore_errors=True)
        return artifact


def _build_plain_binary(*, app_name: str, staging: Path,
                        main_file: str, dist_dir: Path) -> Path:
    """Compile a plain program with `dart compile exe` in the
    staging dir, then install the binary into `dist_dir`.
    Returns the installed binary path."""
    dart = _require_dart()
    built = staging / app_name
    _run_dart([dart, "compile", "exe", str(staging / main_file),
               "-o", str(built)],
              command=COMPILE_COMMAND, staging=staging,
              env=_build_env(staging))
    if not built.is_file():
        raise AppPackError(
            f"{COMPILE_COMMAND} failed: dart exited 0 but no "
            f"binary appeared at {built}; staging kept at "
            f"{staging} for diagnosis")
    dist_dir.mkdir(parents=True, exist_ok=True)
    dest = dist_dir / app_name
    try:
        shutil.copyfile(built, dest)
        os.chmod(dest, 0o755)
    except OSError as exc:
        raise AppPackError(
            f"cannot install the dart binary into {dest}: {exc}; "
            f"staging kept at {staging} for diagnosis") from exc
    return dest


def _build_db_bundle(*, app_name: str, staging: Path,
                     main_file: str, dist_dir: Path) -> Path:
    """Build a db program (a `pubspec.yaml` is staged) with
    `dart pub get` plus `dart build cli` in the staging dir,
    then install the bundle (`bin/<name>` plus `lib/*.so`) into
    `dist_dir`. Returns `dist_dir` itself, the bundle root.

    `dart compile exe` cannot build this program: it does not
    bundle the sqlite3 native asset on this SDK, and the binary
    it produces fails at runtime (see the module docstring).
    """
    dart = _require_dart()
    stem = Path(main_file).stem
    _run_dart([dart, "pub", "get"],
              command=PUB_GET_COMMAND, staging=staging,
              env=_build_env(staging))
    build_out = staging / "build-out"
    _run_dart([dart, "build", "cli", "-t", str(staging / main_file),
               "-o", str(build_out)],
              command=BUILD_CLI_COMMAND, staging=staging,
              env=_build_env(staging))
    bundle = build_out / "bundle"
    built = bundle / "bin" / stem
    if not built.is_file():
        raise AppPackError(
            f"{BUILD_CLI_COMMAND} failed: dart exited 0 but no "
            f"binary appeared at {built} (expected the bundle "
            f"binary named after the entry stem {stem!r}); "
            f"staging kept at {staging} for diagnosis")
    libs = sorted((bundle / "lib").glob("*.so")) if (
        bundle / "lib").is_dir() else []
    if not libs:
        raise AppPackError(
            f"{BUILD_CLI_COMMAND} failed: the bundle at {bundle} "
            f"has no native library in lib/, so the binary would "
            f"fail at runtime with no sqlite3 native asset; "
            f"staging kept at {staging} for diagnosis")
    if dist_dir.is_dir():
        shutil.rmtree(dist_dir)
    elif dist_dir.exists():
        dist_dir.unlink()
    dist_dir.parent.mkdir(parents=True, exist_ok=True)
    try:
        shutil.copytree(bundle, dist_dir)
    except OSError as exc:
        raise AppPackError(
            f"cannot install the dart bundle into {dist_dir}: "
            f"{exc}; staging kept at {staging} for diagnosis"
            ) from exc
    binary = dist_dir / "bin" / stem
    if stem != app_name:
        dest = dist_dir / "bin" / app_name
        try:
            binary.rename(dest)
        except OSError as exc:
            raise AppPackError(
                f"cannot rename the dart bundle binary to "
                f"{dest}: {exc}; staging kept at {staging} for "
                f"diagnosis") from exc
        binary = dest
    try:
        os.chmod(binary, 0o755)
    except OSError as exc:
        raise AppPackError(
            f"cannot make the dart bundle binary executable at "
            f"{binary}: {exc}; staging kept at {staging} for "
            f"diagnosis") from exc
    return dist_dir
