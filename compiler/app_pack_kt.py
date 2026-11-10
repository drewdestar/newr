"""App packaging for `newr build --app`, Kotlin target (SPEC.md
section 22.2, M8d Phase 2).

The kt packager compiles the emitted `.kt` files with the same
command the conformance runner uses (`conformance/run.py`):

    kotlinc <file>.kt -include-runtime -d <name>.jar

and the artifact is `<name>.jar` in `dist/<name>-kt/`, runnable with
`java -jar <name>.jar`. The 5.0 MB hello jar is the Kotlin runtime
that `-include-runtime` bundles. A `db.*` program needs sqlite-jdbc
inside the jar (fat jar) or on the classpath at run time; that
bundling is NOT done here yet (EVAL section 12), so the
jar this packager builds holds the program plus the Kotlin runtime
only, and a db program run with plain `java -jar` fails to load the
driver. The fat-jar recipe itself (unpack sqlite-jdbc plus
slf4j-api into the jar, keep the app manifest) was verified by hand
on this VM and prints PASS; wiring it into this packager is a
follow-up for the integration agent.

Staging lives under `SCRATCH_ROOT` (`~/workspace/.newr-app-scratch`,
never `/tmp`), removed after a successful package and kept on
failure for diagnosis, mirroring the py packager in
`compiler/app_pack.py`. A packager failure raises `AppPackError`
naming the failing command, never a traceback. The packager never
downloads a toolchain: kotlinc lives in `~/kotlin/kotlinc/bin` and
java in `~/jdk-21/bin`, and both must be on PATH when kotlinc runs.
If either is missing, the failure names what to install.

Importing this module runs `kotlinc -version` and `java -version`
once to fill `TOOLCHAIN` (about 6 s on this VM, the kotlinc probe
starts a JVM). Import it lazily inside the kt branch of the
packaging dispatch, never at `app_pack` top level, so other targets
never pay that cost.
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

#: The command the kt packager conceptually runs (used in failure
#: diagnostics so the user can reproduce the step by hand).
KOTLINC_COMMAND = "kotlinc <file>.kt -include-runtime -d <name>.jar"

#: Where kotlinc lives on the build VM (or KOTLIN_HOME, e.g. CI),
#: mirroring conformance/run.py.
_KOTLIN_HOME = Path(os.environ["KOTLIN_HOME"]) \
    if os.environ.get("KOTLIN_HOME") else (Path.home() / "kotlin")
_KOTLIN_BIN = _KOTLIN_HOME / "kotlinc" / "bin"

#: Where java lives on the build VM (or JAVA_HOME, e.g. CI's
#: setup-java), mirroring conformance/run.py.
_JAVA_HOME = Path(os.environ["JAVA_HOME"]) \
    if os.environ.get("JAVA_HOME") else (Path.home() / "jdk-21")
_JAVA_BIN = _JAVA_HOME / "bin"

_KOTLIN_VERSION_RE = re.compile(r"kotlinc-jvm (\d+\.\d+\.\d+)")
_JAVA_VERSION_RE = re.compile(r'version "([^"]+)"')

#: How long a version probe may take before it counts as missing
#: (kotlinc -version starts a JVM and takes seconds on this VM).
_VERSION_TIMEOUT = 120

#: How much of kotlinc stderr a failure message keeps, so a long
#: compiler dump does not flood the diagnostic.
_STDERR_TAIL = 2000


def _resolve_kotlinc() -> str | None:
    """The kotlinc binary, or None when it cannot be found."""
    return shutil.which("kotlinc", path=str(_KOTLIN_BIN)) \
        or shutil.which("kotlinc")


def _resolve_java() -> str | None:
    """The java binary, or None when it cannot be found."""
    return shutil.which("java", path=str(_JAVA_BIN)) \
        or shutil.which("java")


def _tool_env() -> dict[str, str]:
    """The child environment for kotlinc and java: PATH holds both
    tool bins, TMPDIR stays under ~/workspace, JAVA_HOME points at
    the JDK kotlinc must use."""
    SCRATCH_ROOT.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    path = env.get("PATH", "")
    for bin_dir in (str(_KOTLIN_BIN), str(_JAVA_BIN)):
        if bin_dir not in path.split(os.pathsep):
            path = bin_dir + os.pathsep + path if path else bin_dir
    env["PATH"] = path
    env["TMPDIR"] = str(SCRATCH_ROOT)
    env["JAVA_HOME"] = str(_JAVA_HOME)
    return env


def _version(binary: str | None, pattern: re.Pattern[str],
             env: dict[str, str]) -> str:
    """The version a tool reports live, or "unknown" when the tool
    is missing, fails, or prints nothing matching. Both tools print
    their version to stderr."""
    if binary is None:
        return "unknown"
    try:
        proc = subprocess.run([binary, "-version"],
                              capture_output=True, text=True,
                              timeout=_VERSION_TIMEOUT, env=env)
    except (OSError, subprocess.TimeoutExpired):
        return "unknown"
    match = pattern.search(proc.stdout + proc.stderr)
    return match.group(1) if match else "unknown"


def _detect_toolchain() -> dict[str, str]:
    """The toolchain versions, detected live from the tools."""
    env = _tool_env()
    return {
        "kotlin": _version(_resolve_kotlinc(), _KOTLIN_VERSION_RE,
                           env),
        "java": _version(_resolve_java(), _JAVA_VERSION_RE, env),
    }


#: The toolchain versions that ran, detected live at import time
#: (`kotlinc -version`, `java -version`). A tool that cannot be
#: found or probed reports "unknown" here; `package_kt` still raises
#: `AppPackError` naming what to install when it needs the missing
#: tool. The integration agent carries this into the build receipt,
#: replacing the hardcoded py toolchain there.
TOOLCHAIN: dict[str, str] = _detect_toolchain()


def package_kt(*, app_name: str, emitted_dir: Path,
               emitted_files: list[str], main_file: str,
               dist_dir: Path) -> Path:
    """Build the `<name>.jar` for target kt.

    Stages the emitted `.kt` files into a scratch dir under
    `~/workspace`, then runs `kotlinc <file>.kt -include-runtime
    -d <name>.jar` there, the same command the conformance runner
    uses, and copies the jar into `dist_dir`. When the program
    uses `db.*`, the sqlite-jdbc + slf4j jars from `~/jars` are
    merged into the jar (fat jar), so it runs with a plain
    `java -jar`. Returns the artifact path. The scratch dir is
    removed on success and kept on failure for diagnosis.
    """
    kotlinc = _resolve_kotlinc()
    if kotlinc is None:
        raise AppPackError(
            f"{KOTLINC_COMMAND} failed: kotlinc was not found "
            f"(looked in {_KOTLIN_BIN} and on PATH); install "
            f"Kotlin (kotlinc-jvm 2.2.0 on the build VM, see "
            f"SPEC.md section 22.2) and retry")
    java = _resolve_java()
    if java is None:
        raise AppPackError(
            f"{KOTLINC_COMMAND} failed: java was not found "
            f"(looked in {_JAVA_BIN} and on PATH); kotlinc needs "
            f"a JDK to run, install OpenJDK 21 (~/jdk-21 on the "
            f"build VM) and retry")
    main_name = Path(main_file).name
    if not main_name.endswith(".kt"):
        raise AppPackError(
            f"{KOTLINC_COMMAND} failed: the emitted main file "
            f"{main_file!r} is not a .kt file, so kotlinc cannot "
            f"compile it")
    SCRATCH_ROOT.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(
        prefix=f"{app_name}-kt-", dir=str(SCRATCH_ROOT)))
    try:
        staged: list[str] = []
        for name in emitted_files:
            source = emitted_dir / name
            if not source.is_file():
                raise AppPackError(
                    f"{KOTLINC_COMMAND} failed: emitted file "
                    f"{source} is missing, so the jar cannot be "
                    f"staged; staging kept at {staging} for "
                    f"diagnosis")
            if not name.endswith(".kt"):
                # kotlinc only compiles .kt inputs; anything else
                # cannot go into the jar through this command.
                continue
            if name in staged:
                continue
            try:
                shutil.copyfile(source, staging / name)
            except OSError as exc:
                raise AppPackError(
                    f"{KOTLINC_COMMAND} failed: cannot stage "
                    f"{source}: {exc}; staging kept at {staging} "
                    f"for diagnosis") from exc
            staged.append(name)
        if main_name not in staged:
            raise AppPackError(
                f"{KOTLINC_COMMAND} failed: the emitted main file "
                f"{main_file!r} is not among the emitted .kt "
                f"files, so kotlinc has no entry point; staging "
                f"kept at {staging} for diagnosis")
        # Main file first, then the rest sorted, so the command is
        # stable run to run.
        ordered = [main_name] + sorted(n for n in staged
                                       if n != main_name)
        jar_name = f"{app_name}.jar"
        staged_jar = staging / jar_name
        command = [kotlinc, *ordered, "-include-runtime",
                   "-d", str(staged_jar)]
        try:
            proc = subprocess.run(command, capture_output=True,
                                  text=True, cwd=staging,
                                  env=_tool_env())
        except OSError as exc:
            raise AppPackError(
                f"{KOTLINC_COMMAND} failed: cannot run kotlinc: "
                f"{exc}; staging kept at {staging} for "
                f"diagnosis") from exc
        if proc.returncode != 0:
            detail = proc.stderr.strip()[-_STDERR_TAIL:]
            raise AppPackError(
                f"{KOTLINC_COMMAND} failed: kotlinc exited "
                f"{proc.returncode}; staging kept at {staging} "
                f"for diagnosis"
                + (f"\n{detail}" if detail else ""))
        artifact = dist_dir / jar_name
        dist_dir.mkdir(parents=True, exist_ok=True)
        try:
            shutil.copyfile(staged_jar, artifact)
        except OSError as exc:
            raise AppPackError(
                f"{KOTLINC_COMMAND} failed: cannot write the "
                f"artifact {artifact}: {exc}; staging kept at "
                f"{staging} for diagnosis") from exc
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
