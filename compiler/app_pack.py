"""App packaging for `newr build --app` (SPEC.md section 22.2, M8d
Phase 1).

Phase 1 implements the py packager only: a stdlib `zipapp` single
file, `<name>.pyz`, built from the already emitted `.py` files plus
`newr_rt.py` when the program uses it, plus a generated `__main__.py`
entry that runs the emitted main module. The shebang is
`/usr/bin/env python3`. PyInstaller is NOT the default: system Python
has no PyInstaller (it lives only in the `m7-build/venv` venv), and
the packager never installs or downloads a toolchain.

The package step runs in a scratch dir under `~/workspace` (never
`/tmp`: it is a 512 MB tmpfs), removed after a successful package and
kept on failure for diagnosis. The artifact lands in
`dist/<name>-<target>/` under the current working directory, next to
`BUILD-RECEIPT.json`, the machine-readable proof that the artifact
came from this source (name, version, target, artifact relative path,
artifact bytes, sha256, the source files with their sha256, the
toolchain versions that ran, and the build timestamp).

A packager failure raises `AppPackError` naming the failing command;
the CLI turns it into an exit-4 build error, never a traceback.
"""

from __future__ import annotations

import hashlib
import json
import platform
import re
import shutil
import tempfile
import zipapp
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .init import PROJECT_VERSION, Manifest

#: Where packaging scratch dirs live (SPEC section 22.2: under
#: `~/workspace`, never `/tmp`).
SCRATCH_ROOT = Path.home() / "workspace" / ".newr-app-scratch"

#: The command the py packager conceptually runs (used in failure
#: diagnostics so the user can reproduce the step by hand).
ZIPAPP_COMMAND = (
    "python3 -m zipapp <staging> -o <artifact> "
    "-p \"/usr/bin/env python3\" --compress"
)


class AppPackError(Exception):
    """App packaging failed (exit 4 in the CLI, never a traceback)."""


@dataclass(frozen=True)
class AppResult:
    """The outcome of a successful `newr build --app` package step."""

    artifact_path: Path
    receipt_path: Path
    receipt: dict


def sanitize_app_name(name: str) -> str:
    """The app name as used for the artifact and dist dir (SPEC
    section 22.2): lowercased, only `[a-z0-9_-]`."""
    cleaned = re.sub(r"[^a-z0-9_-]+", "-", name.lower()).strip("-")
    return cleaned or "app"


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _relative_source_path(source: Path, main_path: Path) -> str:
    """A source file path as recorded in the receipt: relative to the
    main program's directory when possible, else relative to the
    current directory, else the file name."""
    try:
        return str(source.relative_to(main_path.parent))
    except ValueError:
        pass
    try:
        return str(source.relative_to(Path.cwd()))
    except ValueError:
        return source.name


def _build_receipt(*, app_name: str, version: str, target: str,
                   manifest: Manifest | None, artifact_name: str,
                   artifact_bytes: int, artifact_sha256: str,
                   main_path: Path,
                   source_files: list[Path]) -> dict:
    """The BUILD-RECEIPT.json contents (SPEC section 22.2)."""
    receipt: dict = {
        "name": app_name,
        "version": version,
        "target": target,
        "artifact": artifact_name,
        "artifact_bytes": artifact_bytes,
        "sha256": artifact_sha256,
        "sources": [
            {"path": _relative_source_path(source, main_path),
             "sha256": _sha256_file(source)}
            for source in source_files
        ],
        "toolchain": {
            "python": platform.python_version(),
            "zipapp": "stdlib",
        },
        "timestamp": datetime.now(timezone.utc).isoformat().replace(
            "+00:00", "Z"),
    }
    if manifest is not None:
        if manifest.description is not None:
            receipt["description"] = manifest.description
        if manifest.icon is not None:
            receipt["icon"] = manifest.icon
        if manifest.id is not None:
            receipt["id"] = manifest.id
    return receipt


def _package_py_zipapp(*, app_name: str, emitted_dir: Path,
                       emitted_files: list[str], main_file: str,
                       dist_dir: Path) -> Path:
    """Build the `<name>.pyz` zipapp for target py.

    Stages the emitted `.py` files (plus `newr_rt.py` when listed in
    `emitted_files`) and a generated `__main__.py` entry into a
    scratch dir under `~/workspace`, then runs stdlib `zipapp`.
    Returns the artifact path. The scratch dir is removed on
    success and kept on failure for diagnosis.
    """
    stem = Path(main_file).stem
    if not stem.isidentifier():
        raise AppPackError(
            f"{ZIPAPP_COMMAND} failed: the emitted main file "
            f"{main_file!r} has a stem {stem!r} that is not a valid "
            f"Python module name, so no __main__.py entry can import "
            f"it; rename the program file")
    SCRATCH_ROOT.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(
        prefix=f"{app_name}-py-", dir=str(SCRATCH_ROOT)))
    try:
        for name in emitted_files:
            source = emitted_dir / name
            if not source.is_file():
                raise AppPackError(
                    f"{ZIPAPP_COMMAND} failed: emitted file "
                    f"{source} is missing, so the zipapp cannot be "
                    f"staged; staging kept at {staging} for "
                    f"diagnosis")
            try:
                shutil.copyfile(source, staging / name)
            except OSError as exc:
                raise AppPackError(
                    f"{ZIPAPP_COMMAND} failed: cannot stage "
                    f"{source}: {exc}; staging kept at {staging} "
                    f"for diagnosis") from exc
        try:
            (staging / "__main__.py").write_text(
                "# Generated by `newr build --app`: zipapp entry "
                "point.\n"
                "import runpy\n"
                f"runpy.run_module({stem!r}, run_name=\"__main__\")\n",
                encoding="utf-8")
        except OSError as exc:
            raise AppPackError(
                f"{ZIPAPP_COMMAND} failed: cannot write the "
                f"__main__.py entry: {exc}; staging kept at "
                f"{staging} for diagnosis") from exc
        artifact = dist_dir / f"{app_name}.pyz"
        dist_dir.mkdir(parents=True, exist_ok=True)
        try:
            zipapp.create_archive(staging, artifact,
                                  interpreter="/usr/bin/env python3",
                                  compressed=True)
        except Exception as exc:
            raise AppPackError(
                f"{ZIPAPP_COMMAND} failed: {exc}; staging kept at "
                f"{staging} for diagnosis") from exc
    except BaseException:
        # Any failure above keeps the staging dir for diagnosis.
        raise
    else:
        shutil.rmtree(staging, ignore_errors=True)
        return artifact


def package_app(*, target: str, app_name: str, version: str,
                manifest: Manifest | None, main_path: Path,
                source_files: list[Path], emitted_dir: Path,
                emitted_files: list[str], main_file: str,
                dist_dir: Path) -> AppResult:
    """Package an emitted program for one target (SPEC section 22.2).

    Phase 1 implements py only. Any other target raises
    `AppPackError` naming that its packager is not implemented yet;
    the CLI reports it as an exit-4 build error.
    """
    if target != "py":
        raise AppPackError(
            f"app packaging for target {target!r} is not implemented "
            f"yet (M8d Phase 1 implements py only); build without "
            f"--app, or pick --target py")
    artifact = _package_py_zipapp(
        app_name=app_name, emitted_dir=emitted_dir,
        emitted_files=emitted_files, main_file=main_file,
        dist_dir=dist_dir)
    artifact_bytes = artifact.stat().st_size
    artifact_sha256 = _sha256_file(artifact)
    receipt = _build_receipt(
        app_name=app_name, version=version, target=target,
        manifest=manifest, artifact_name=artifact.name,
        artifact_bytes=artifact_bytes,
        artifact_sha256=artifact_sha256, main_path=main_path,
        source_files=source_files)
    receipt_path = dist_dir / "BUILD-RECEIPT.json"
    receipt_path.write_text(json.dumps(receipt, indent=2) + "\n",
                            encoding="utf-8")
    return AppResult(artifact_path=artifact, receipt_path=receipt_path,
                     receipt=receipt)


def app_name_and_version(main_path: Path,
                         manifest: Manifest | None) -> tuple[str, str]:
    """The app name (sanitized) and version for a build: the manifest
    `name` and `version` when there is a manifest, else the program
    stem and the compiler version."""
    if manifest is not None:
        return sanitize_app_name(manifest.name), manifest.version
    return sanitize_app_name(main_path.stem), PROJECT_VERSION
