"""App packaging for `newr build --app --target cs` (SPEC.md section
22.2, M8d Phase 2).

The cs artifact is a SINGLE FILE, not a publish directory (EVAL.md
section 12, verified live 10 Nov 2026, see below): the self-contained
publish output with `-p:PublishSingleFile=true`, which is one
executable file named after the assembly (plus a `.pdb` next to it,
debug symbols that are not shipped). The plain self-contained
publish directory (71 MB, 187 files for hello) is not used: the
single file is smaller, one file to copy, and runs the same way.

Single-file verification, run live on this VM 10 Nov 2026 with
`examples/hello.newr`::

    dotnet publish -c Release --self-contained -r linux-x64 \
        -p:PublishSingleFile=true -o publish-single

Publish took 11.0 s cold (6.7 s restore, no PackageReference to
restore for hello). The output was one executable file `hello`,
66,800,454 bytes, plus `hello.pdb` (10,364 bytes). Running
`./publish-single/hello` printed "Hello, world!" and exited 0.
The plain publish for comparison took 3.4 s warm, 71 MB, 187
files. Single-file is therefore the chosen artifact shape.

Framework-dependent publish does NOT run on this VM (EVAL.md
section 4, correction 5): its apphost cannot find the runtime,
because the runtime lives in `~/.dotnet`, which is not on PATH.
Only the self-contained publish is a valid app artifact here.
The runtime identifier is `linux-x64` only on this VM; `win-x64`
and `osx-arm64` are not claimed.

The package step stages the emitted `.cs` files, the `.csproj`,
and `newr_rt.cs` when listed in `emitted_files` into a scratch
dir under `~/workspace` (`SCRATCH_ROOT`, never `/tmp`: it is a
512 MB tmpfs), runs `dotnet publish` there, then installs the
single published file as `dist_dir/<app_name>`. The staging dir
is removed after a successful package and kept on failure for
diagnosis, mirroring the py packager in `app_pack.py`.

Receipt definitions for the build receipt (the agent that wires
this packager into `package_app` uses these rules):

- `artifact_bytes` for a file artifact is the file size, see
  `artifact_bytes()`. For a directory artifact it is the total
  byte count of every file inside it, summed recursively. The cs
  artifact is a file, so the directory rule is the fallback
  definition only, kept identical to the js packager.
- `sha256` for a file artifact is the sha256 of the file, see
  `artifact_sha256()`. For a directory artifact it is the
  deterministic hash of the directory contents, identical to the
  js packager: the sha256 over the sorted list of
  `<relative path>\\0<per-file sha256>\\n` lines, one line per
  file. File modes are not part of the hash, only names and
  contents.

The packager never downloads a toolchain. If `dotnet` is missing,
the build fails with a plain message naming what to install.

A packager failure raises `AppPackError` naming the failing
command; the CLI turns it into an exit-4 build error, never a
traceback.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from .app_pack import (
    AppPackError, SCRATCH_ROOT, _artifact_bytes, _artifact_sha256,
)

#: The .NET runtime identifier for this VM (SPEC.md section 22.2).
#: Only linux-x64 is claimed; win-x64 and osx-arm64 are not
#: verified here.
RID = "linux-x64"

#: Where dotnet's temp files go for every dotnet subprocess
#: (SPEC.md section 22.2: under `~/workspace`, never `/tmp`).
DOTNET_TMPDIR = Path.home() / "workspace" / ".newr-dotnet-tmp"

#: The command the cs packager runs (used in failure diagnostics
#: so the user can reproduce the step by hand).
PUBLISH_COMMAND = (
    "dotnet publish -c Release --self-contained -r linux-x64 "
    "-p:PublishSingleFile=true -o <dir>"
)

#: How long a publish may run before it is a failure. Generous on
#: purpose: a cold NuGet restore took 96 s in the M8c proof, and
#: this VM has a small CPU with several agents running in parallel.
PUBLISH_TIMEOUT = 600

#: How long the `dotnet --version` probe may run.
VERSION_TIMEOUT = 120


def _detect_dotnet() -> str | None:
    """The `dotnet` executable for this machine, or None when it
    is not installed. `dotnet` lives in `~/.dotnet` on this VM
    (installed for M4), which is not on the default PATH, so that
    dir is checked first, then the PATH (e.g. CI, where
    setup-dotnet provides it). `$DOTNET_ROOT` overrides the home
    dir when set."""
    root = (Path(os.environ["DOTNET_ROOT"])
            if os.environ.get("DOTNET_ROOT")
            else Path.home() / ".dotnet")
    return (shutil.which("dotnet", path=str(root))
            or shutil.which("dotnet"))


def _detect_dotnet_version() -> str | None:
    """The installed .NET SDK version (`8.0.427` style), detected
    live from `dotnet --version`, or None when dotnet is missing
    or the check fails."""
    dotnet = _detect_dotnet()
    if dotnet is None:
        return None
    try:
        run = subprocess.run([dotnet, "--version"],
                             capture_output=True, text=True,
                             timeout=VERSION_TIMEOUT)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if run.returncode != 0:
        return None
    version = (run.stdout.strip().splitlines()[0]
               if run.stdout.strip() else "")
    return version or None


#: The toolchain that ran, detected live at import time from
#: `dotnet --version`. When dotnet is missing the value is
#: "unknown" and `package_cs` raises `AppPackError` before any
#: receipt is built, so "unknown" never reaches a receipt.
_DOTNET_VERSION = _detect_dotnet_version()
TOOLCHAIN = {"dotnet": _DOTNET_VERSION
             if _DOTNET_VERSION is not None else "unknown"}


def _require_dotnet() -> str:
    """The `dotnet` executable, or raise `AppPackError` naming
    what to install when dotnet is missing."""
    dotnet = _detect_dotnet()
    if dotnet is None:
        raise AppPackError(
            f"{PUBLISH_COMMAND} failed: dotnet is not installed or "
            f"not on PATH (`dotnet --version` failed); install the "
            f".NET SDK 8.0 (see EVAL.md section 11) and retry "
            f"`newr build --app --target cs`")
    return dotnet


def _dotnet_env(dotnet: str) -> dict[str, str]:
    """The environment for a dotnet subprocess: telemetry off, temp
    files under `~/workspace` (never `/tmp`), and the dotnet dir
    first on PATH so child processes find the same dotnet."""
    DOTNET_TMPDIR.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env["DOTNET_CLI_TELEMETRY_OPTOUT"] = "1"
    env["TMPDIR"] = str(DOTNET_TMPDIR)
    dotnet_dir = str(Path(dotnet).parent)
    env["PATH"] = dotnet_dir + os.pathsep + env.get("PATH", "")
    return env


#: The receipt helpers, shared with every packager (see
#: `compiler/app_pack.py`): a file artifact reports its size and
#: hashes the file; a directory artifact sums its files and hashes
#: the sorted `<relative path>\0<per-file sha256>\n` lines, always
#: excluding the receipt file itself. Kept as module names so the
#: packager tests can import them.
artifact_bytes = _artifact_bytes
artifact_sha256 = _artifact_sha256


def _find_csproj(emitted_files: list[str], main_file: str) -> str:
    """The `.csproj` basename to build: the one matching the main
    file stem when listed, else the single `.csproj` in the
    emitted files. Raises `AppPackError` when no `.csproj` can
    be found."""
    stem = Path(main_file).stem
    csproj_name = stem + ".csproj"
    if csproj_name in emitted_files:
        return csproj_name
    csprojs = [name for name in emitted_files
               if name.endswith(".csproj")]
    if len(csprojs) == 1:
        return csprojs[0]
    raise AppPackError(
        f"{PUBLISH_COMMAND} failed: no .csproj among the emitted "
        f"files for main file {main_file!r} (emitted files: "
        f"{sorted(emitted_files)}), so there is no project to "
        f"publish; rebuild the program")


def package_cs(*, app_name: str, emitted_dir: Path,
               emitted_files: list[str], main_file: str,
               dist_dir: Path) -> Path:
    """Build the cs app artifact for target cs.

    Stages the emitted `.cs` files, the `.csproj`, and
    `newr_rt.cs` when listed in `emitted_files` into a scratch
    dir under `SCRATCH_ROOT`, runs the self-contained single-file
    publish there, and installs the single published executable
    as `dist_dir/<app_name>`. The artifact is one file, so this
    returns `dist_dir / app_name` (the receipt `artifact_bytes`
    for it is `artifact_bytes(artifact)` and its `sha256` is
    `artifact_sha256(artifact)`).

    The `.pdb` the publish leaves next to the executable is
    debug symbols only and is not shipped: the artifact is one
    file, and the pdb is regenerated on every build.

    The staging dir is removed on success and kept on failure
    for diagnosis. Any previous file or directory at the artifact
    path is replaced, so a rebuild never leaves a stale artifact
    behind.

    Input validation (main file, csproj, emitted files on disk)
    runs before the dotnet requirement, so a missing file is
    reported by name even on a machine without dotnet (CI).
    """
    if Path(main_file).name != main_file:
        raise AppPackError(
            f"{PUBLISH_COMMAND} failed: the emitted main file "
            f"{main_file!r} is not a bare file name, so it cannot "
            f"be staged; rebuild the program")
    if main_file not in emitted_files:
        raise AppPackError(
            f"{PUBLISH_COMMAND} failed: the emitted main file "
            f"{main_file!r} is not listed in the emitted files, so "
            f"the cs app cannot be staged")
    csproj_name = _find_csproj(emitted_files, main_file)
    SCRATCH_ROOT.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(
        prefix=f"{app_name}-cs-", dir=str(SCRATCH_ROOT)))
    try:
        for name in emitted_files:
            if Path(name).name != name:
                raise AppPackError(
                    f"{PUBLISH_COMMAND} failed: emitted file {name!r} "
                    f"is not a bare file name, so it cannot be "
                    f"staged into the cs project; staging kept at "
                    f"{staging} for diagnosis")
            source = emitted_dir / name
            if not source.is_file():
                raise AppPackError(
                    f"{PUBLISH_COMMAND} failed: emitted file "
                    f"{source} is missing, so the cs app cannot be "
                    f"staged; staging kept at {staging} for "
                    f"diagnosis")
            try:
                shutil.copyfile(source, staging / name)
            except OSError as exc:
                raise AppPackError(
                    f"{PUBLISH_COMMAND} failed: cannot stage "
                    f"{source}: {exc}; staging kept at {staging} "
                    f"for diagnosis") from exc
        publish_out = staging / "publish"
        dotnet = _require_dotnet()
        args = [dotnet, "publish", str(staging / csproj_name),
                "-c", "Release", "--self-contained",
                "-r", RID, "-p:PublishSingleFile=true",
                "-o", str(publish_out)]
        try:
            proc = subprocess.run(args, capture_output=True,
                                  text=True, cwd=staging,
                                  env=_dotnet_env(dotnet),
                                  timeout=PUBLISH_TIMEOUT)
        except subprocess.TimeoutExpired:
            raise AppPackError(
                f"{PUBLISH_COMMAND} failed: the publish timed out "
                f"after {PUBLISH_TIMEOUT} s; staging kept at "
                f"{staging} for diagnosis") from None
        except OSError as exc:
            raise AppPackError(
                f"{PUBLISH_COMMAND} failed: cannot run dotnet: "
                f"{exc}; staging kept at {staging} for "
                f"diagnosis") from exc
        if proc.returncode != 0:
            tail = (proc.stdout + proc.stderr).strip().splitlines()
            tail = tail[-20:] if len(tail) > 20 else tail
            detail = "\n".join(tail)
            raise AppPackError(
                f"{PUBLISH_COMMAND} failed with exit "
                f"{proc.returncode}:\n{detail}\n"
                f"staging kept at {staging} for diagnosis")
        if not publish_out.is_dir():
            raise AppPackError(
                f"{PUBLISH_COMMAND} failed: dotnet reported success "
                f"but produced no publish output dir at "
                f"{publish_out}; staging kept at {staging} for "
                f"diagnosis")
        # Single-file publish leaves exactly the executable plus
        # its .pdb (debug symbols, not shipped). Anything else
        # means the publish did not produce the expected single
        # file, so fail loudly instead of guessing which file is
        # the app.
        produced = sorted(p for p in publish_out.iterdir()
                          if p.is_file())
        exe_files = [p for p in produced
                     if p.suffix.lower() != ".pdb"]
        if len(exe_files) != 1:
            names = [p.name for p in produced]
            raise AppPackError(
                f"{PUBLISH_COMMAND} failed: the publish produced "
                f"{names}, not exactly one executable file plus a "
                f".pdb, so no single-file artifact can be picked; "
                f"staging kept at {staging} for diagnosis")
        artifact = dist_dir / app_name
        try:
            dist_dir.mkdir(parents=True, exist_ok=True)
            if artifact.is_dir() and not artifact.is_symlink():
                shutil.rmtree(artifact)
            elif artifact.exists() or artifact.is_symlink():
                artifact.unlink()
            shutil.copy2(exe_files[0], artifact)
        except OSError as exc:
            raise AppPackError(
                f"{PUBLISH_COMMAND} failed: cannot install the "
                f"published file into {artifact}: {exc}; staging "
                f"kept at {staging} for diagnosis") from exc
    except BaseException:
        # Any failure above keeps the staging dir for diagnosis.
        raise
    else:
        shutil.rmtree(staging, ignore_errors=True)
        return artifact
