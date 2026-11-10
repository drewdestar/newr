"""App packaging for `newr build --app` (SPEC.md section 22.2, M8d
Phase 1 and Phase 2).

Phase 1 implements the py packager only: a stdlib `zipapp` single
file, `<name>.pyz`, built from the already emitted `.py` files plus
`newr_rt.py` when the program uses it, plus a generated `__main__.py`
entry that runs the emitted main module. The shebang is
`/usr/bin/env python3`. PyInstaller is NOT the default: system Python
has no PyInstaller (it lives only in the `m7-build/venv` venv), and
the packager never installs or downloads a toolchain.

Phase 2 adds one packager module per remaining target
(`compiler/app_pack_<target>.py`): dart (`dart compile exe`, or
`dart build cli` for a db program, whose sqlite3 native assets
`dart compile exe` cannot bundle), js (a directory copy plus a
`run.sh` entry, run with node), java (a jar, fattened with
sqlite-jdbc + slf4j when the program uses `db.*`), kt (a
`kotlinc -include-runtime` jar, fattened the same way), rs
(`rustc -O`, or `cargo build --release` when the program ships a
Cargo.toml), and cs (a self-contained single-file `dotnet publish`).
Each packager module is imported LAZILY inside `package_app`, never
at the top of this module: every packager detects its toolchain
version live at import time, and `kotlinc -version` costs about
6 seconds of JVM startup, which a py build must never pay.

The package step runs in a scratch dir under `~/workspace` (never
`/tmp`: it is a 512 MB tmpfs), removed after a successful package and
kept on failure for diagnosis. The artifact lands in
`dist/<name>-<target>/` under the current working directory, next to
`BUILD-RECEIPT.json`, the machine-readable proof that the artifact
came from this source (name, version, target, artifact relative path,
artifact bytes, sha256, the source files with their sha256, the
toolchain versions that ran, and the build timestamp). A file
artifact reports its size and hashes the file; a directory artifact
reports the total byte count of every file inside it and hashes the
sorted list of `<relative path>\0<per-file sha256>\n` lines. The
receipt file itself (`BUILD-RECEIPT.json`, written into the
artifact directory by `package_app`) is NOT part of those numbers:
it is build metadata about the artifact, not part of the program,
and including it would make the receipt hash itself.

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
import zipfile
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


#: The receipt file name, written by `package_app` into the
#: artifact directory (`dist/<name>-<target>/`).
_RECEIPT_NAME = "BUILD-RECEIPT.json"


def _artifact_bytes(artifact: Path) -> int:
    """The receipt `artifact_bytes` for any artifact. A file
    artifact reports its size; a directory artifact reports the
    total byte count of every file inside it, summed recursively
    (the js packager rule, shared by every packager), EXCLUDING
    the receipt file itself (`BUILD-RECEIPT.json` at the artifact
    root: it is build metadata, not part of the program)."""
    if artifact.is_file():
        return artifact.stat().st_size
    total = 0
    for path in sorted(artifact.rglob("*")):
        if path.is_file() and not _is_receipt(path, artifact):
            total += path.stat().st_size
    return total


def _artifact_sha256(artifact: Path) -> str:
    """The receipt `sha256` for any artifact. A file artifact
    hashes the file; a directory artifact hashes the sorted list
    of `<relative path>\\0<per-file sha256>\\n` lines, one line per
    file inside the directory (relative paths use `/`
    separators), EXCLUDING the receipt file itself
    (`BUILD-RECEIPT.json` at the artifact root). File modes are
    not part of the hash, only file names and contents, so the
    value is stable across copies that preserve content."""
    if artifact.is_file():
        return _sha256_file(artifact)
    digest = hashlib.sha256()
    for path in sorted(artifact.rglob("*")):
        if not path.is_file() or _is_receipt(path, artifact):
            continue
        rel = path.relative_to(artifact).as_posix()
        digest.update(f"{rel}\0{_sha256_file(path)}\n".encode("utf-8"))
    return digest.hexdigest()


def _is_receipt(path: Path, artifact: Path) -> bool:
    """Whether `path` is the receipt file at the artifact root:
    `BUILD-RECEIPT.json` directly inside the artifact directory.
    Only the receipt `package_app` writes is excluded, never a
    file of the same name nested deeper."""
    return path.name == _RECEIPT_NAME and path.parent == artifact


#: The sqlite-jdbc + slf4j jars for a db program's fat jar (M8a
#: pins, verified live: sqlite-jdbc 3.54.1.0, slf4j-api 2.0.19).
#: They live in `~/jars` on the build VM; the packager never
#: downloads them (see `conformance/run.py`, which downloads them
#: into the same cache when absent).
_JDBC_DIR = Path.home() / "jars"
_JDBC_JARS = (
    _JDBC_DIR / "sqlite-jdbc-3.54.1.0.jar",
    _JDBC_DIR / "slf4j-api-2.0.19.jar",
)


def _uses_db(emitted_dir: Path, emitted_files: list[str]) -> bool:
    """Whether an emitted program uses `db.*`: any emitted file
    contains a `NewrRt.dbOpen(` call (the same check the
    conformance runner uses to put sqlite-jdbc on the classpath).
    The runtime files themselves never contain that literal: it
    appears only at a program's own call sites."""
    for name in emitted_files:
        path = emitted_dir / name
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if "NewrRt.dbOpen(" in text:
            return True
    return False


def _bundle_sqlite_jars(jar_path: Path) -> None:
    """Merge the sqlite-jdbc + slf4j jars from `~/jars` into the
    jar at `jar_path`, so a db program runs with a plain
    `java -jar` (which ignores any classpath). The app jar's own
    entries win: its manifest is kept as the first entry, driver
    entries already present in the app jar are skipped, duplicate
    manifests and signature files (`.SF`, `.RSA`, `.DSA`) from
    the driver jars are skipped. Raises `AppPackError` naming the
    missing jar when a driver jar is absent."""
    for driver in _JDBC_JARS:
        if not driver.is_file():
            raise AppPackError(
                "fat jar bundling failed: driver jar "
                f"{driver} is missing; put sqlite-jdbc "
                "3.54.1.0 and slf4j-api 2.0.19 in ~/jars and "
                "run `newr build --app` again")
    with zipfile.ZipFile(jar_path) as app_zip:
        entries = [(info, app_zip.read(info.filename))
                   for info in app_zip.infolist()]
    seen = {info.filename for info, _ in entries}
    for driver in _JDBC_JARS:
        with zipfile.ZipFile(driver) as driver_zip:
            for info in driver_zip.infolist():
                name = info.filename
                if name in seen:
                    continue
                if name == "META-INF/MANIFEST.MF":
                    continue
                if name.startswith("META-INF/") and \
                        name.rsplit(".", 1)[-1].upper() in (
                            "SF", "RSA", "DSA"):
                    continue
                entries.append((info, driver_zip.read(name)))
                seen.add(name)
    tmp_path = jar_path.with_name(jar_path.name + ".tmp")
    try:
        with zipfile.ZipFile(tmp_path, "w",
                             zipfile.ZIP_DEFLATED) as out:
            for info, data in entries:
                out.writestr(info, data)
    except OSError as exc:
        raise AppPackError(
            f"fat jar bundling failed: cannot write {tmp_path}: "
            f"{exc}") from exc
    try:
        tmp_path.replace(jar_path)
    except OSError as exc:
        raise AppPackError(
            f"fat jar bundling failed: cannot replace {jar_path}: "
            f"{exc}") from exc


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
                   toolchain: dict[str, str],
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
        "toolchain": toolchain,
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

    Dispatches to the packager module for the target. Every
    packager module is imported lazily inside its branch: each one
    detects its toolchain version live at import time, and
    `kotlinc -version` costs about 6 seconds of JVM startup, which
    a build for another target must never pay. An unknown target
    raises `AppPackError`; the CLI reports it as an exit-4 build
    error.
    """
    if target == "py":
        artifact = _package_py_zipapp(
            app_name=app_name, emitted_dir=emitted_dir,
            emitted_files=emitted_files, main_file=main_file,
            dist_dir=dist_dir)
        toolchain = {
            "python": platform.python_version(),
            "zipapp": "stdlib",
        }
    elif target == "dart":
        from .app_pack_dart import TOOLCHAIN, package_dart
        artifact = package_dart(
            app_name=app_name, emitted_dir=emitted_dir,
            emitted_files=emitted_files, main_file=main_file,
            dist_dir=dist_dir)
        toolchain = dict(TOOLCHAIN)
    elif target == "js":
        from .app_pack_js import TOOLCHAIN, package_js
        artifact = package_js(
            app_name=app_name, emitted_dir=emitted_dir,
            emitted_files=emitted_files, main_file=main_file,
            dist_dir=dist_dir)
        toolchain = dict(TOOLCHAIN)
    elif target == "java":
        from .app_pack_java import TOOLCHAIN, package_java
        artifact = package_java(
            app_name=app_name, emitted_dir=emitted_dir,
            emitted_files=emitted_files, main_file=main_file,
            dist_dir=dist_dir)
        toolchain = dict(TOOLCHAIN)
    elif target == "kt":
        from .app_pack_kt import TOOLCHAIN, package_kt
        artifact = package_kt(
            app_name=app_name, emitted_dir=emitted_dir,
            emitted_files=emitted_files, main_file=main_file,
            dist_dir=dist_dir)
        toolchain = dict(TOOLCHAIN)
    elif target == "rs":
        from .app_pack_rs import TOOLCHAIN, package_rs
        artifact = package_rs(
            app_name=app_name, emitted_dir=emitted_dir,
            emitted_files=emitted_files, main_file=main_file,
            dist_dir=dist_dir)
        toolchain = dict(TOOLCHAIN)
    elif target == "cs":
        from .app_pack_cs import TOOLCHAIN, package_cs
        artifact = package_cs(
            app_name=app_name, emitted_dir=emitted_dir,
            emitted_files=emitted_files, main_file=main_file,
            dist_dir=dist_dir)
        toolchain = dict(TOOLCHAIN)
    else:
        raise AppPackError(
            f"app packaging for target {target!r} is not "
            f"implemented; build without --app, or pick one of "
            f"py, js, java, cs, rs, kt, dart")
    receipt = _build_receipt(
        app_name=app_name, version=version, target=target,
        manifest=manifest, artifact_name=artifact.name,
        artifact_bytes=_artifact_bytes(artifact),
        artifact_sha256=_artifact_sha256(artifact),
        toolchain=toolchain, main_path=main_path,
        source_files=source_files)
    receipt_path = dist_dir / _RECEIPT_NAME
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
