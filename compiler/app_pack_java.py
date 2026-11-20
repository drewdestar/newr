"""App packaging for `newr build --app`, target java (SPEC.md
section 22.2, M8d Phase 2).

Builds a `<name>.jar` from the already emitted `.java` files: compile
them with `javac`, then pack ONLY the resulting `.class` files with
`jar --create --file <name>.jar --main-class <Stem>`. The probe in
the eval used `jar ... -C . .`, which also swept the `.java` source
into the jar; this packager does not do that, it lists the `.class`
files explicitly.

The main class is the stem of the emitted main file (the emitter
names the public class after the file stem, K1), so no name lookup
is needed: `hello.java` means main class `hello`.

The package step runs in a scratch dir under `~/workspace` (never
`/tmp`: it is a 512 MB tmpfs), removed after a successful package
and kept on failure for diagnosis. The artifact lands in
`dist/<name>-java/` under the current working directory. The
packager never downloads a toolchain: if `javac` or `jar` is
missing, it fails with a plain message naming what to install
(OpenJDK 21).

Known limit (eval section 12, still open): a program using `db.*`
compiles fine (compile needs only `java.sql`, which is in the JDK),
but the jar does not bundle sqlite-jdbc, so `java -jar` fails at
run time with `ClassNotFoundException: org.sqlite.JDBC`. A db
program needs a fat jar (sqlite-jdbc unpacked into the app jar) or
the driver shipped alongside with a `Class-Path` manifest entry.
That bundling is not part of this packager yet; see the Phase 2
report for the fat-jar experiment.

`TOOLCHAIN` carries the live-detected JDK version for the build
receipt (the integration agent passes it into `_build_receipt`,
whose toolchain block currently hardcodes py).
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from .app_pack import (
    AppPackError, SCRATCH_ROOT, _bundle_sqlite_jars, _uses_db,
)

#: The javac step, as shown in failure diagnostics so the user can
#: reproduce it by hand.
JAVAC_COMMAND = "javac <staging>/*.java"

#: The jar step, as shown in failure diagnostics so the user can
#: reproduce it by hand.
JAR_COMMAND = (
    "jar --create --file <artifact>.jar --main-class <Stem> "
    "-C <staging> <classes>"
)

#: How long the compile and jar steps may run. The build VM has a
#: small CPU and several agents run in parallel, so these are loose
#: on purpose; a timeout is an AppPackError, never a hang.
JAVAC_TIMEOUT_SECONDS = 300
JAR_TIMEOUT_SECONDS = 120


def _jdk_bin_dir() -> Path | None:
    """The JDK `bin` dir: `$JAVA_HOME/bin` first (JAVA_HOME is the
    JDK root, the same rule as `tests/test_emitter_java.py`), then
    `~/jdk-21/bin`, else None (fall back to the PATH)."""
    java_home = os.environ.get("JAVA_HOME")
    if java_home:
        home = Path(java_home)
        if (home / "bin" / "javac").is_file():
            return home / "bin"
        if (home / "javac").is_file():
            return home
    candidate = Path.home() / "jdk-21" / "bin"
    if (candidate / "javac").is_file():
        return candidate
    return None


def _find_tool(name: str, jdk_bin: Path | None) -> str | None:
    """Find a JDK tool: the JDK bin dir first, then the PATH."""
    if jdk_bin is not None:
        found = shutil.which(name, path=str(jdk_bin))
        if found:
            return found
    return shutil.which(name)


def _tool_env(jdk_bin: Path | None,
              tmpdir: Path | None = None) -> dict[str, str]:
    """The env for a JDK subprocess: PATH with the JDK bin dir
    first, and TMPDIR under `~/workspace` (never `/tmp`) when a
    scratch dir is given."""
    env = dict(os.environ)
    if jdk_bin is not None:
        env["PATH"] = str(jdk_bin) + os.pathsep + env.get("PATH", "")
    if tmpdir is not None:
        env["TMPDIR"] = str(tmpdir)
    return env


def _parse_javac_version(text: str) -> str | None:
    """The version from `javac -version` output (`javac 21.0.13`)."""
    parts = text.strip().split()
    if len(parts) >= 2 and parts[0] == "javac":
        return parts[1]
    return None


def _parse_java_version(text: str) -> str | None:
    """The version from `java -version` output
    (`openjdk version "21.0.13" ...`)."""
    match = re.search(r'"([^"]+)"', text)
    return match.group(1) if match else None


def _detect_toolchain() -> dict[str, str]:
    """The live-detected JDK version: `javac -version` first,
    `java -version` as fallback. Never raises: when no JDK is
    found the version is reported as not installed, and
    `package_java` raises the proper AppPackError later."""
    jdk_bin = _jdk_bin_dir()
    env = _tool_env(jdk_bin)
    for command, parse in (("javac", _parse_javac_version),
                           ("java", _parse_java_version)):
        tool = _find_tool(command, jdk_bin)
        if tool is None:
            continue
        try:
            proc = subprocess.run(
                [tool, "-version"], capture_output=True, text=True,
                timeout=30, env=env)
        except (OSError, subprocess.TimeoutExpired):
            continue
        version = parse(proc.stdout) or parse(proc.stderr)
        if version:
            return {"java": version}
    return {"java": "not installed"}


try:
    #: The toolchain versions for the build receipt, detected live
    #: at import (see `_detect_toolchain`).
    TOOLCHAIN: dict[str, str] = _detect_toolchain()
except Exception:  # noqa: BLE001 - import must never fail on this
    TOOLCHAIN = {"java": "not installed"}


def _run_or_raise(argv: list[str], *, cwd: Path, timeout: int,
                  env: dict[str, str], staging: Path,
                  label: str) -> subprocess.CompletedProcess:
    """Run one JDK step. A failure raises AppPackError naming the
    command (with its stderr, trimmed) and keeping the staging dir
    for diagnosis; never a traceback, never a hang."""
    try:
        proc = subprocess.run(argv, capture_output=True, text=True,
                              cwd=str(cwd), timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        raise AppPackError(
            f"{label} failed: timed out after {timeout}s; staging "
            f"kept at {staging} for diagnosis") from None
    except OSError as exc:
        raise AppPackError(
            f"{label} failed: cannot run {argv[0]}: {exc}; staging "
            f"kept at {staging} for diagnosis") from exc
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        raise AppPackError(
            f"{label} failed (exit {proc.returncode}): "
            f"{detail[-1000:] if detail else 'no output'}; "
            f"staging kept at {staging} for diagnosis")
    return proc


def package_java(*, app_name: str, emitted_dir: Path,
                 emitted_files: list[str], main_file: str,
                 dist_dir: Path) -> Path:
    """Build the `<name>.jar` app for target java.

    Stages the emitted `.java` files (plus `NewrRt.java` when
    listed in `emitted_files`) into a scratch dir under
    `~/workspace`, compiles them with `javac`, then packs ONLY
    the resulting `.class` files into the jar with `--main-class`
    set to the main file stem. Non-`.java` emitted files (for
    example `pom.xml` for a bot program, a Maven build file) are
    not staged: they are not compilable and do not belong in the
    jar. When the program uses `db.*`, the sqlite-jdbc + slf4j
    jars from `~/jars` are merged into the jar (fat jar), so it
    runs with a plain `java -jar`. Returns the artifact path. The
    scratch dir is removed on success and kept on failure for
    diagnosis.
    """
    stem = Path(main_file).stem
    if not re.fullmatch(r"[A-Za-z_$][A-Za-z0-9_$]*", stem):
        raise AppPackError(
            f"{JAR_COMMAND} failed: the emitted main file "
            f"{main_file!r} has a stem {stem!r} that is not a valid "
            f"Java class name, so no Main-Class entry can name it; "
            f"rename the program file")
    jdk_bin = _jdk_bin_dir()
    javac = _find_tool("javac", jdk_bin)
    if javac is None:
        raise AppPackError(
            f"{JAVAC_COMMAND} failed: no javac found (JAVA_HOME / "
            f"~/jdk-21/bin / PATH); install OpenJDK 21 (javac, "
            f"java, and jar ship together in the JDK bin dir) and "
            f"run `newr build --app` again")
    jar = _find_tool("jar", jdk_bin)
    if jar is None:
        raise AppPackError(
            f"{JAR_COMMAND} failed: no jar found (JAVA_HOME / "
            f"~/jdk-21/bin / PATH); install OpenJDK 21 (javac, "
            f"java, and jar ship together in the JDK bin dir) and "
            f"run `newr build --app` again")
    java_files = [name for name in emitted_files
                  if name.endswith(".java")]
    if not java_files:
        raise AppPackError(
            f"{JAVAC_COMMAND} failed: no .java files were emitted "
            f"for {app_name!r}, so there is nothing to compile; "
            f"build without --app, or pick a program that emits "
            f"Java")
    SCRATCH_ROOT.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(
        prefix=f"{app_name}-java-", dir=str(SCRATCH_ROOT)))
    env = _tool_env(jdk_bin, tmpdir=SCRATCH_ROOT)
    try:
        for name in java_files:
            source = emitted_dir / name
            if not source.is_file():
                raise AppPackError(
                    f"{JAVAC_COMMAND} failed: emitted file "
                    f"{source} is missing, so the jar cannot be "
                    f"staged; staging kept at {staging} for "
                    f"diagnosis")
            try:
                shutil.copyfile(source, staging / name)
            except OSError as exc:
                raise AppPackError(
                    f"{JAVAC_COMMAND} failed: cannot stage "
                    f"{source}: {exc}; staging kept at {staging} "
                    f"for diagnosis") from exc
        dist_dir.mkdir(parents=True, exist_ok=True)
        artifact = dist_dir / f"{app_name}.jar"
        _run_or_raise(
            [javac, *java_files], cwd=staging,
            timeout=JAVAC_TIMEOUT_SECONDS, env=env, staging=staging,
            label=f"javac {' '.join(java_files)}")
        class_files = sorted(p.name for p in staging.glob("*.class"))
        if not class_files:
            raise AppPackError(
                f"{JAVAC_COMMAND} failed: javac succeeded but "
                f"produced no .class files; staging kept at "
                f"{staging} for diagnosis")
        artifact_arg = str(
            artifact if artifact.is_absolute()
            else Path.cwd() / artifact)
        _run_or_raise(
            [jar, "--create", "--file", artifact_arg,
             "--main-class", stem, "-C", str(staging),
             *class_files],
            cwd=staging, timeout=JAR_TIMEOUT_SECONDS, env=env,
            staging=staging,
            label=f"jar --create --file {artifact_arg} "
                  f"--main-class {stem} -C {staging} "
                  f"{' '.join(class_files)}")
        if _uses_db(emitted_dir, emitted_files):
            # A `db.*` program: `java -jar` ignores any
            # classpath, so the driver jars are merged into the
            # jar itself (fat jar).
            _bundle_sqlite_jars(artifact)
    except BaseException:
        # Any failure above keeps the staging dir for diagnosis.
        raise
    else:
        shutil.rmtree(staging, ignore_errors=True)
        return artifact
