"""App packaging for `newr build --app --target rs` (SPEC.md section
22.2, M8d Phase 2).

The rs artifact is a single native binary, `<name>`, with no file
extension:

- A plain program (no `Cargo.toml` among the emitted files) builds
  with bare `rustc -O` (EVAL.md section 4: 0.8 s, 4.5 MB for hello
  on this VM). The binary is built in the staging dir and then
  copied into `dist_dir`, so `dist_dir` only ever receives a
  complete artifact.
- A program with a `Cargo.toml` (the emitter writes one when the
  program uses `db.*`, `http.*`, `json.*`, `ui.show`/`ui.run`, or a
  bot) builds with `cargo build --release --offline` and the binary
  is taken from `target/release/<stem>` under the persistent
  `CARGO_TARGET_DIR`. `--offline` enforces the rule that the
  packager never downloads a crate: every crate must already be in
  the cargo registry cache (the M8b pins are, the conformance
  runner and the emitter tests build them this way). The binary
  name is the first `[[bin]]` name in the staged `Cargo.toml`
  (the emitter always writes one), falling back to the `[package]`
  name; the packager never guesses it from the file stem.

`CARGO_TARGET_DIR` is `~/workspace/.cargo-target` (SPEC section
22.2: a persistent dir under `~/workspace`, never `/tmp`). It is
shared across builds on purpose, so the compiled dependencies
(ring, rusqlite, and friends) stay warm between runs. Cargo locks
the target dir itself when two builds run at once. This differs
from the M8b test helper, which uses `$HOME/.cargo-target`; the
packager follows SPEC section 22.2 and the task brief, which put
the dir under `~/workspace`.

The package step stages the emitted files into a scratch dir
under `SCRATCH_ROOT` (never `/tmp`: it is a 512 MB tmpfs), runs
the build with `TMPDIR` pointed at that same staging dir, and
removes the staging dir after a successful package, keeping it on
failure for diagnosis, mirroring the py packager in `app_pack.py`.

`TOOLCHAIN` is the toolchain that ran, detected live at import
time from `rustc --version` and `cargo --version` (the agent that
wires this packager into `package_app` passes it into the build
receipt; `app_pack.py` currently hardcodes the py toolchain, and
the wiring agent adds the per-target parameter). When a tool is
missing the value is "unknown" and `package_rs` raises
`AppPackError` before any receipt is built, so "unknown" never
reaches a receipt.

A packager failure raises `AppPackError` naming the failing
command; the CLI turns it into an exit-4 build error, never a
traceback. The build subprocesses run with no timeout: a release
build of a dependency program takes minutes on this VM (ring, cold
cache), and a timeout short enough to catch a hang would also kill
a legitimate slow build. The caller (CLI, test, packaging suite)
owns the timeout.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from .app_pack import AppPackError, SCRATCH_ROOT

#: The commands the rs packager runs (used in failure diagnostics
#: so the user can reproduce the step by hand).
RUSTC_COMMAND = "rustc -O <main.rs> -o <binary>"
CARGO_COMMAND = "cargo build --release --offline"

#: The persistent cargo target dir (SPEC section 22.2): under
#: `~/workspace`, never `/tmp`. Shared across builds so compiled
#: dependencies stay warm.
CARGO_TARGET_DIR = Path.home() / "workspace" / ".cargo-target"

#: How much of a failed build's stderr to keep in the diagnostic:
#: enough to name the real error, short enough to stay readable.
_STDERR_TAIL_CHARS = 2000


def _cargo_home_bin() -> Path:
    """The rustup bin dir: `$CARGO_HOME/bin` when set, else
    `~/.cargo/bin` (the conformance runner's rule)."""
    cargo_home = os.environ.get("CARGO_HOME")
    if cargo_home:
        return Path(cargo_home) / "bin"
    return Path.home() / ".cargo" / "bin"


def _find_tool(name: str) -> str | None:
    """The path of a Rust tool: the rustup bin dir first, then
    PATH (the conformance runner's rule in `conformance/run.py`).
    None when the tool is nowhere to be found."""
    found = shutil.which(name, path=str(_cargo_home_bin()))
    if found is not None:
        return found
    return shutil.which(name)


#: The resolved tool paths, found live at import time (None when
#: a tool is missing; `package_rs` raises before building then).
RUSTC = _find_tool("rustc")
CARGO = _find_tool("cargo")


def _detect_version(binary: str | None) -> str | None:
    """The version number of a Rust tool binary (`1.99.1` style),
    detected live from `<binary> --version`, or None when the
    binary is missing or the check fails. The full output looks
    like `rustc 1.99.1 (58cf8f9c3 2026-10-13)`; only the number
    is kept."""
    if binary is None:
        return None
    try:
        run = subprocess.run([binary, "--version"],
                             capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if run.returncode != 0:
        return None
    match = re.search(r"(\d+\.\d+\.\d+)", run.stdout.strip())
    if match is not None:
        return match.group(1)
    return run.stdout.strip() or None


#: The toolchain that ran, detected live at import time from
#: `rustc --version` and `cargo --version`. When a tool is
#: missing the value is "unknown" and `package_rs` raises
#: `AppPackError` before any receipt is built, so "unknown"
#: never reaches a receipt.
_TOOLCHAIN_VERSIONS = {
    "rustc": _detect_version(RUSTC),
    "cargo": _detect_version(CARGO),
}
TOOLCHAIN = {name: (version if version is not None else "unknown")
             for name, version in _TOOLCHAIN_VERSIONS.items()}


def _require_rustc() -> str:
    """The live rustc path, or raise `AppPackError` naming what to
    install when rustc is missing."""
    rustc = _find_tool("rustc")
    if rustc is None:
        raise AppPackError(
            f"{RUSTC_COMMAND} failed: rustc is not installed or "
            f"not on PATH (checked {_cargo_home_bin()} and PATH); "
            f"install the Rust toolchain with rustup "
            f"(https://rustup.rs, rustc 1.99.1 per EVAL.md "
            f"section 11) and retry `newr build --app --target rs`")
    return rustc


def _require_cargo() -> str:
    """The live cargo path, or raise `AppPackError` naming what to
    install when cargo is missing."""
    cargo = _find_tool("cargo")
    if cargo is None:
        raise AppPackError(
            f"{CARGO_COMMAND} failed: cargo is not installed or "
            f"not on PATH (checked {_cargo_home_bin()} and PATH); "
            f"install the Rust toolchain with rustup "
            f"(https://rustup.rs, cargo 1.99.1 per EVAL.md "
            f"section 11) and retry `newr build --app --target rs`")
    return cargo


def _bin_name_from_cargo_toml(text: str) -> str | None:
    """The binary `cargo build` produces for a Cargo.toml: the
    first `[[bin]]` name, else the `[package]` name (cargo builds
    a binary named after the package when no `[[bin]]` is given).
    None when the file names no binary at all. The emitter always
    writes a `[[bin]]`, so the fallback only fires for a
    hand-written Cargo.toml."""
    section = ""
    package_name: str | None = None
    for line in text.splitlines():
        stripped = line.strip()
        section_match = re.match(r"^\[\[?([^\]]+)\]?\]\s*$", stripped)
        if section_match is not None:
            section = section_match.group(1).strip()
            continue
        name_match = re.match(r"^name\s*=\s*\"([^\"]+)\"", stripped)
        if name_match is None:
            continue
        if section == "bin":
            return name_match.group(1)
        if section == "package" and package_name is None:
            package_name = name_match.group(1)
    return package_name


def _stderr_tail(proc: subprocess.CompletedProcess) -> str:
    """The tail of a failed build's stderr, for the diagnostic."""
    err = (proc.stderr or "").strip()
    if len(err) > _STDERR_TAIL_CHARS:
        err = "...\n" + err[-_STDERR_TAIL_CHARS:]
    return err


def _build_env(staging: Path) -> dict:
    """The environment for a build subprocess: the persistent
    cargo target dir, and TMPDIR under `~/workspace` (the staging
    dir itself, removed after a successful package) instead of
    the 512 MB /tmp tmpfs."""
    env = dict(os.environ)
    env["CARGO_TARGET_DIR"] = str(CARGO_TARGET_DIR)
    env["TMPDIR"] = str(staging)
    return env


def package_rs(*, app_name: str, emitted_dir: Path,
               emitted_files: list[str], main_file: str,
               dist_dir: Path) -> Path:
    """Build the rs app artifact for target rs: a single native
    binary, `<name>`, in `dist_dir`.

    A plain program (no `Cargo.toml` in `emitted_files`) builds
    with bare `rustc -O`; a program with a `Cargo.toml` builds
    with `cargo build --release --offline` and the binary is
    taken from `target/release/<stem>` under the persistent
    `CARGO_TARGET_DIR`. Returns the artifact path. The staging
    dir is removed on success and kept on failure for diagnosis.
    """
    if not app_name or "/" in app_name or app_name in (".", ".."):
        raise AppPackError(
            f"cannot package target rs: the app name {app_name!r} "
            f"is not a bare file name, so no binary named after it "
            f"can be written to {dist_dir}")
    if Path(main_file).name != main_file:
        raise AppPackError(
            f"{RUSTC_COMMAND} failed: the emitted main file "
            f"{main_file!r} is not a bare file name, so it cannot "
            f"be staged for the rs build; rebuild the program")
    if main_file not in emitted_files:
        raise AppPackError(
            f"{RUSTC_COMMAND} failed: the emitted main file "
            f"{main_file!r} is not listed in the emitted files, so "
            f"the rs binary cannot be staged")
    has_cargo_toml = "Cargo.toml" in emitted_files
    # Input validation (main file, emitted files on disk) runs
    # before the toolchain requirement, so a missing file is
    # reported by name even on a machine without rustc (CI).
    SCRATCH_ROOT.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(
        prefix=f"{app_name}-rs-", dir=str(SCRATCH_ROOT)))
    try:
        for name in emitted_files:
            if Path(name).name != name:
                raise AppPackError(
                    f"{RUSTC_COMMAND} failed: emitted file {name!r} "
                    f"is not a bare file name, so it cannot be "
                    f"staged for the rs build; staging kept at "
                    f"{staging} for diagnosis")
            source = emitted_dir / name
            if not source.is_file():
                raise AppPackError(
                    f"{RUSTC_COMMAND} failed: emitted file "
                    f"{source} is missing, so the rs binary cannot "
                    f"be built; staging kept at {staging} for "
                    f"diagnosis")
            try:
                shutil.copyfile(source, staging / name)
            except OSError as exc:
                raise AppPackError(
                    f"{RUSTC_COMMAND} failed: cannot stage "
                    f"{source}: {exc}; staging kept at {staging} "
                    f"for diagnosis") from exc
        if has_cargo_toml:
            _require_cargo()
        else:
            _require_rustc()
        if has_cargo_toml:
            artifact = _build_with_cargo(
                app_name=app_name, staging=staging)
        else:
            artifact = _build_with_rustc(
                app_name=app_name, staging=staging,
                main_file=main_file)
        dist_dir.mkdir(parents=True, exist_ok=True)
        dest = dist_dir / app_name
        try:
            shutil.copyfile(artifact, dest)
            os.chmod(dest, 0o755)
        except OSError as exc:
            raise AppPackError(
                f"cannot install the rs binary into {dest}: {exc}; "
                f"staging kept at {staging} for diagnosis") from exc
    except BaseException:
        # Any failure above keeps the staging dir for diagnosis.
        raise
    else:
        shutil.rmtree(staging, ignore_errors=True)
        return dest


def _build_with_rustc(*, app_name: str, staging: Path,
                      main_file: str) -> Path:
    """Compile a plain program with bare `rustc -O` in the staging
    dir. Returns the built binary path (inside staging)."""
    rustc = _require_rustc()
    built = staging / app_name
    try:
        proc = subprocess.run(
            [rustc, "-O", str(staging / main_file), "-o",
             str(built)],
            capture_output=True, text=True, cwd=staging,
            env=_build_env(staging))
    except OSError as exc:
        raise AppPackError(
            f"{RUSTC_COMMAND} failed: cannot run rustc ({rustc}): "
            f"{exc}; staging kept at {staging} for diagnosis"
            ) from exc
    if proc.returncode != 0:
        raise AppPackError(
            f"{RUSTC_COMMAND} failed (rustc exit "
            f"{proc.returncode}): {_stderr_tail(proc) or proc.stdout.strip()}; "
            f"staging kept at {staging} for diagnosis")
    if not built.is_file():
        raise AppPackError(
            f"{RUSTC_COMMAND} failed: rustc exited 0 but no binary "
            f"appeared at {built}; staging kept at {staging} for "
            f"diagnosis")
    return built


def _build_with_cargo(*, app_name: str, staging: Path) -> Path:
    """Build a Cargo.toml program with `cargo build --release
    --offline` in the staging dir. Returns the built binary path
    (under the persistent `CARGO_TARGET_DIR`)."""
    cargo = _require_cargo()
    CARGO_TARGET_DIR.mkdir(parents=True, exist_ok=True)
    try:
        toml_text = (staging / "Cargo.toml").read_text(
            encoding="utf-8")
    except OSError as exc:
        raise AppPackError(
            f"{CARGO_COMMAND} failed: cannot read the staged "
            f"Cargo.toml: {exc}; staging kept at {staging} for "
            f"diagnosis") from exc
    bin_name = _bin_name_from_cargo_toml(toml_text)
    if bin_name is None:
        raise AppPackError(
            f"{CARGO_COMMAND} failed: the staged Cargo.toml names "
            f"no [[bin]] and no [package], so no binary can be "
            f"taken from target/release; staging kept at {staging} "
            f"for diagnosis")
    try:
        proc = subprocess.run(
            [cargo, "build", "--release", "--offline", "--quiet"],
            capture_output=True, text=True, cwd=staging,
            env=_build_env(staging))
    except OSError as exc:
        raise AppPackError(
            f"{CARGO_COMMAND} failed: cannot run cargo ({cargo}): "
            f"{exc}; staging kept at {staging} for diagnosis"
            ) from exc
    if proc.returncode != 0:
        raise AppPackError(
            f"{CARGO_COMMAND} failed (cargo exit "
            f"{proc.returncode}): {_stderr_tail(proc) or proc.stdout.strip()}; "
            f"staging kept at {staging} for diagnosis")
    built = CARGO_TARGET_DIR / "release" / bin_name
    if not built.is_file():
        raise AppPackError(
            f"{CARGO_COMMAND} failed: cargo exited 0 but no binary "
            f"appeared at {built} (expected the [[bin]] "
            f"{bin_name!r} from the staged Cargo.toml); staging "
            f"kept at {staging} for diagnosis")
    return built
