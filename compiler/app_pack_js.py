"""App packaging for `newr build --app --target js` (SPEC.md section
22.2, M8d Phase 2).

The js artifact is a DIRECTORY, not a single file (EVAL.md section
4): the emitted `<stem>.js` plus `newr_rt.js` when the program uses
it, plus a generated `run.sh` launcher. There is no bundler: npm
10.9.4 exists on this VM but no bundler is installed, and M8d does
not add one. The packager only copies files and writes `run.sh`;
it never downloads a toolchain.

`run.sh` is two lines::

    #!/bin/sh
    exec node "$(dirname "$0")/<stem>.js" "$@"

so the artifact runs from any directory, with extra arguments passed
through to the program.

The package step stages into a scratch dir under `~/workspace`
(`SCRATCH_ROOT`, never `/tmp`: it is a 512 MB tmpfs), then installs
the staged files into `dist_dir`, replacing any previous contents
of that dir so a rebuild never leaves stale files behind. The
staging dir is removed after a successful package and kept on
failure for diagnosis, mirroring the py packager in `app_pack.py`.

Directory-artifact definitions for the build receipt (the agent
that wires this packager into `package_app` uses these rules):

- `artifact_bytes` is the total byte count of every file inside the
  artifact directory, see `artifact_bytes()`. It is NOT the size of
  a single file, because the artifact is a directory.
- `sha256` is a deterministic hash of the directory contents, see
  `artifact_sha256()`: the sha256 over the sorted list of
  `<relative path>\\0<per-file sha256>\\n` lines, one line per file.
  File modes (such as the executable bit on `run.sh`) are not part
  of the hash, only names and contents.

A packager failure raises `AppPackError` naming the failing step;
the CLI turns it into an exit-4 build error, never a traceback.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from .app_pack import (
    AppPackError, SCRATCH_ROOT, _artifact_bytes, _artifact_sha256,
)

#: The steps the js packager conceptually runs (used in failure
#: diagnostics so the user can reproduce them by hand).
JS_COMMAND = ("cp <emitted .js files> <artifact>/ "
              "&& write <artifact>/run.sh (exec node <stem>.js)")

#: A safe emitted main-file stem: letters, digits, underscore, dot,
#: dash, not starting with a dash or a dot. Anything else cannot be
#: named safely in `run.sh`.
_STEM_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]*\Z")


def _detect_node_version() -> str | None:
    """The installed Node version (``24.22.1`` style, no leading
    ``v``), detected live from `node --version`, or None when node
    is missing or the check fails."""
    try:
        run = subprocess.run(["node", "--version"],
                             capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if run.returncode != 0:
        return None
    version = run.stdout.strip().removeprefix("v")
    return version or None


#: The toolchain that ran, detected live at import time from
#: `node --version`. When node is missing the value is "unknown"
#: and `package_js` raises `AppPackError` before any receipt is
#: built, so "unknown" never reaches a receipt.
_NODE_VERSION = _detect_node_version()
TOOLCHAIN = {"node": _NODE_VERSION if _NODE_VERSION is not None
             else "unknown"}


def _require_node() -> str:
    """The live Node version, or raise `AppPackError` naming what
    to install when node is missing."""
    version = _detect_node_version()
    if version is None:
        raise AppPackError(
            f"{JS_COMMAND} failed: node is not installed or not on "
            f"PATH (`node --version` failed); install Node.js "
            f"(v24.x, see EVAL.md section 11) and retry "
            f"`newr build --app --target js`")
    return version


#: The receipt helpers, shared with every packager (see
#: `compiler/app_pack.py`): a file artifact reports its size and
#: hashes the file; a directory artifact sums its files and hashes
#: the sorted `<relative path>\0<per-file sha256>\n` lines, always
#: excluding the receipt file itself. A js artifact is always a
#: directory, so only the directory rule applies here. Kept as
#: module names so the packager tests can import them.
artifact_bytes = _artifact_bytes
artifact_sha256 = _artifact_sha256


def _run_sh_text(stem: str) -> str:
    return (f"#!/bin/sh\n"
            f"exec node \"$(dirname \"$0\")/{stem}.js\" \"$@\"\n")


def package_js(*, app_name: str, emitted_dir: Path,
               emitted_files: list[str], main_file: str,
               dist_dir: Path) -> Path:
    """Build the js app artifact for target js.

    Copies the emitted `.js` files (the main file plus `newr_rt.js`
    when listed in `emitted_files`) into the artifact directory and
    writes an executable `run.sh` launcher next to them. The
    artifact is a directory, so this returns `dist_dir` itself (the
    receipt `artifact_bytes` for it is `artifact_bytes(dist_dir)`
    and its `sha256` is `artifact_sha256(dist_dir)`).

    The copy goes through a staging dir under `SCRATCH_ROOT`, which
    is removed on success and kept on failure for diagnosis. Any
    previous contents of `dist_dir` are replaced, so a rebuild
    never leaves stale files behind.
    """
    _require_node()
    if Path(main_file).name != main_file:
        raise AppPackError(
            f"{JS_COMMAND} failed: the emitted main file "
            f"{main_file!r} is not a bare file name, so no run.sh "
            f"launcher can name it; rebuild the program")
    stem = Path(main_file).stem
    if not _STEM_RE.fullmatch(stem):
        raise AppPackError(
            f"{JS_COMMAND} failed: the emitted main file "
            f"{main_file!r} has a stem {stem!r} that cannot be "
            f"named safely in run.sh; rename the program file")
    if main_file not in emitted_files:
        raise AppPackError(
            f"{JS_COMMAND} failed: the emitted main file "
            f"{main_file!r} is not listed in the emitted files, so "
            f"the js app directory cannot be staged")
    SCRATCH_ROOT.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(
        prefix=f"{app_name}-js-", dir=str(SCRATCH_ROOT)))
    try:
        for name in emitted_files:
            if Path(name).name != name:
                raise AppPackError(
                    f"{JS_COMMAND} failed: emitted file {name!r} is "
                    f"not a bare file name, so it cannot be staged "
                    f"into the js app directory; staging kept at "
                    f"{staging} for diagnosis")
            source = emitted_dir / name
            if not source.is_file():
                raise AppPackError(
                    f"{JS_COMMAND} failed: emitted file {source} "
                    f"is missing, so the js app directory cannot "
                    f"be staged; staging kept at {staging} for "
                    f"diagnosis")
            try:
                shutil.copyfile(source, staging / name)
            except OSError as exc:
                raise AppPackError(
                    f"{JS_COMMAND} failed: cannot stage {source}: "
                    f"{exc}; staging kept at {staging} for "
                    f"diagnosis") from exc
        run_sh = staging / "run.sh"
        try:
            run_sh.write_text(_run_sh_text(stem), encoding="utf-8")
            os.chmod(run_sh, 0o755)
        except OSError as exc:
            raise AppPackError(
                f"{JS_COMMAND} failed: cannot write the run.sh "
                f"launcher: {exc}; staging kept at {staging} for "
                f"diagnosis") from exc
        try:
            if dist_dir.exists():
                shutil.rmtree(dist_dir)
            shutil.copytree(staging, dist_dir)
        except OSError as exc:
            raise AppPackError(
                f"{JS_COMMAND} failed: cannot install the staged "
                f"js app directory into {dist_dir}: {exc}; staging "
                f"kept at {staging} for diagnosis") from exc
    except BaseException:
        # Any failure above keeps the staging dir for diagnosis.
        raise
    else:
        shutil.rmtree(staging, ignore_errors=True)
        return dist_dir
