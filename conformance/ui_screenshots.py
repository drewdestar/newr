#!/usr/bin/env python3
"""Native-window screenshot harness (M8c, SPEC.md SS12.8).

Usage:
  python3 conformance/ui_screenshots.py [--target py|java|kt|cs|rs]
      [--program examples/hello_ui.newr] [--out DIR] [--wait SECONDS]

Builds the program for each native target (py, java, kt, cs, rs),
runs it under `xvfb-run -a` with a 60 second timeout, waits for
the first paint, screenshots the root window, and checks the shot
with PIL: it must be 1024x768 and contain non-black pixels (a real
window rendered, not a blank screen). Prints one PASS/FAIL line
per target with the numbers, and exits 0 only when every target
passed.

The ok corpus is render-only by design (a show/run program blocks
forever), so this harness is the only verification native windows
get. js and dart have no native window (HTML sink), so they are
not targets here.

Environment notes, all learned the painful way in M8c:
- /tmp is a 512 MB tmpfs: the scratch dirs live under the build
  dir on the home disk, never /tmp. Set TMPDIR there too when the
  harness builds (cs copies about 480 MB of SkiaSharp assets into
  bin/, which alone fills /tmp).
- cs needs NUGET_PACKAGES pointed at the proof cache
  (~/workspace/newr-research/m8c-ui-build/proofs/avalonia/.nuget)
  unless a cold nuget.org restore is what is being tested.
- rs needs CARGO_TARGET_DIR pointed at a persistent dir, or the
  first eframe build takes about 20 minutes. The rs proof target
  dir is reused by default.
- libXtst6/libXi6/libXrender1/libXt6 must be installed for Swing;
  they do not survive a VM replacement, so the harness checks for
  them and fails loudly when they are missing.
- The screenshot tool is ImageMagick `import` when present. When
  it is missing (it is not preinstalled, and apt installs do not
  survive a VM replacement either), the harness falls back to a
  Java Robot capture (Shot.java, written to the scratch dir and
  compiled with javac), which needs only the JDK.
- First paint is slow on this loaded VM: cs needs about 25 s,
  java/kt about 10-15 s. The per-target wait is a default, and
  --wait overrides it for every target.
"""

import argparse
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LAUNCHER = ROOT / "newr"
BUILD_DIR = Path.home() / "workspace" / "newr-research" / "m8c-ui-build"
SCRATCH = BUILD_DIR / "screenshots"
PROOF_NUGET = BUILD_DIR / "proofs" / "avalonia" / ".nuget"
RS_TARGET = BUILD_DIR / "emitters" / "rs" / "target"

NATIVE_TARGETS = ("py", "java", "kt", "cs", "rs")

# Seconds to wait after starting the program before the shot:
# first paint is slow on this loaded VM (cs about 25 s, java/kt
# about 10-15 s, py/rs faster).
WAIT = {"py": 5, "java": 12, "kt": 12, "cs": 25, "rs": 10}

# The runtime file copied next to the emitted program (Java renames
# on copy, K1; the others keep their name).
_RT_SRC = {"py": "newr_rt.py", "java": "newr_rt.java",
           "kt": "newr_rt.kt", "cs": "newr_rt.cs", "rs": "newr_rt.rs"}
_RT_COPY = {"py": "newr_rt.py", "java": "NewrRt.java", "kt": "newr_rt.kt",
            "cs": "newr_rt.cs", "rs": "newr_rt.rs"}
_SUFFIX = {"py": ".py", "java": ".java", "kt": ".kt", "cs": ".cs",
           "rs": ".rs"}

_JAVA_HOME = Path(os.environ["JAVA_HOME"]) \
    if os.environ.get("JAVA_HOME") else (Path.home() / "jdk-21")
_JAVA_BIN = _JAVA_HOME / "bin"
JAVA = shutil.which("java", path=str(_JAVA_BIN)) or shutil.which("java")
JAVAC = shutil.which("javac", path=str(_JAVA_BIN)) or shutil.which("javac")
_DOTNET_ROOT = Path(os.environ["DOTNET_ROOT"]) \
    if os.environ.get("DOTNET_ROOT") else (Path.home() / ".dotnet")
DOTNET = shutil.which("dotnet", path=str(_DOTNET_ROOT)) \
    or shutil.which("dotnet")
_CARGO_HOME = Path(os.environ["CARGO_HOME"]) \
    if os.environ.get("CARGO_HOME") else (Path.home() / ".cargo")
CARGO = shutil.which("cargo", path=str(_CARGO_HOME / "bin")) \
    or shutil.which("cargo")
RUSTC = shutil.which("rustc", path=str(_CARGO_HOME / "bin")) \
    or shutil.which("rustc")
_KOTLIN_HOME = Path(os.environ["KOTLIN_HOME"]) \
    if os.environ.get("KOTLIN_HOME") else (Path.home() / "kotlin")
KOTLINC = shutil.which("kotlinc", path=str(_KOTLIN_HOME / "kotlinc"
                                           / "bin")) \
    or shutil.which("kotlinc")
_DART_HOME = Path(os.environ["DART_HOME"]) \
    if os.environ.get("DART_HOME") else (Path.home() / "dart-sdk")

# Java Robot screenshot (JDK only, no apt install): written to the
# scratch dir and compiled with javac when ImageMagick `import`
# is missing.
_SHOT_JAVA = """import java.awt.*;
import java.awt.image.BufferedImage;
import java.io.File;
import javax.imageio.ImageIO;

public class Shot {
    public static void main(String[] args) throws Exception {
        String out = args.length > 0 ? args[0] : "shot.png";
        Robot robot = new Robot();
        Rectangle screen = new Rectangle(
            Toolkit.getDefaultToolkit().getScreenSize());
        BufferedImage img = robot.createScreenCapture(screen);
        ImageIO.write(img, "png", new File(out));
        System.out.println("shot ok: " + out + " " + img.getWidth()
                           + "x" + img.getHeight());
    }
}
"""


def ensure_tool_path() -> None:
    """Prepend the toolchain bin dirs to PATH: kotlinc invokes
    `java` from the shell PATH (not from JAVA_HOME), so a kt build
    fails with "java: command not found" unless ~/jdk-21/bin is
    on PATH. Same for the kotlinc, dotnet, cargo, and dart bin
    dirs, and for the `java` Click/Shot helpers run inside
    xvfb-run."""
    _extra = []
    for _d in (str(_JAVA_BIN),
               str(_KOTLIN_HOME / "kotlinc" / "bin"),
               str(_DOTNET_ROOT),
               str(_CARGO_HOME / "bin"),
               str(_DART_HOME / "dart-sdk" / "bin")):
        if Path(_d).exists() and _d not in os.environ.get("PATH", ""):
            _extra.append(_d)
    if _extra:
        os.environ["PATH"] = os.pathsep.join(
            _extra + [os.environ.get("PATH", "")])


def _fail(target: str, why: str) -> int:
    print(f"FAIL {target}: {why}")
    return 1


def _check_x11_libs() -> str | None:
    """The four X11 client libraries Swing needs, checked by the
    .so they provide (the package name for libXt is libxt6t64 on
    Ubuntu 24.04, not libxt6). Missing ones mean a VM replacement
    wiped the apt install: fail loudly with the reinstall command
    instead of a confusing window failure."""
    r = subprocess.run(["ldconfig", "-p"], capture_output=True, text=True)
    libs = r.stdout
    missing = []
    for soname, pkg in (("libXtst.so.6", "libxtst6"),
                        ("libXi.so.6", "libxi6"),
                        ("libXrender.so.1", "libxrender1"),
                        ("libXt.so.6", "libxt6")):
        if soname not in libs:
            missing.append(pkg)
    if missing:
        return ("missing X11 libs: " + ", ".join(missing)
                + "; reinstall with: sudo apt-get install -y "
                "libxtst6 libxi6 libxrender1 libxt6")
    return None


def _build(target: str, program: Path, work: Path) -> str | None:
    """Build the program into work/ (the build also writes its
    scaffolding there: .csproj for cs, Cargo.toml for rs). The
    runtime is copied in from compiler/, overwriting anything the
    build wrote. Returns an error string, or None."""
    suffix = _SUFFIX[target]
    out = work / (program.stem + suffix)
    env = dict(os.environ)
    env["TMPDIR"] = str(SCRATCH / "tmp")
    r = subprocess.run(
        [sys.executable, str(LAUNCHER), "build", "--target", target,
         str(program), "-o", str(out)],
        capture_output=True, text=True, cwd=ROOT, env=env)
    if r.returncode != 0:
        return f"build failed: {r.stdout}{r.stderr}"
    rt_src = ROOT / "compiler" / _RT_SRC[target]
    (work / _RT_COPY[target]).write_text(
        rt_src.read_text(encoding="utf-8"), encoding="utf-8")
    return None


def _compile(target: str, work: Path, program: Path) -> tuple[
        list[str] | None, str | None]:
    """The per-target compile step for a show program. Returns
    (argv, error): argv runs the built program, error is set when
    the compile failed. py needs no compile."""
    stem = program.stem
    if target == "py":
        return [sys.executable, str(work / (stem + ".py"))], None
    if target == "java":
        files = [str(p) for p in work.glob("*.java")]
        r = subprocess.run([JAVAC, *files], capture_output=True,
                           text=True, cwd=work)
        if r.returncode != 0:
            return None, f"javac exit {r.returncode}\n{r.stderr}"
        return [JAVA, stem], None
    if target == "kt":
        files = [str(p) for p in work.glob("*.kt")]
        r = subprocess.run(
            [KOTLINC, *files, "-include-runtime",
             "-d", str(work / "app.jar")],
            capture_output=True, text=True, cwd=work)
        if r.returncode != 0:
            return None, f"kotlinc exit {r.returncode}\n{r.stderr}"
        return [JAVA, "-jar", str(work / "app.jar")], None
    if target == "cs":
        env = dict(os.environ)
        env["DOTNET_CLI_TELEMETRY_OPTOUT"] = "1"
        env["TMPDIR"] = str(SCRATCH / "tmp")
        if PROOF_NUGET.exists():
            env["NUGET_PACKAGES"] = str(PROOF_NUGET)
        proj = work / (stem + ".csproj")
        r = subprocess.run(
            [DOTNET, "build", str(proj), "-c", "Release"],
            capture_output=True, text=True, cwd=work, env=env)
        if r.returncode != 0:
            return None, f"dotnet build exit {r.returncode}\n{r.stderr}"
        dll = work / "bin" / "Release" / "net8.0" / (stem + ".dll")
        return [DOTNET, str(dll)], None
    # rs: a show program always has a Cargo.toml (eframe). Build
    # with the shared persistent target dir, or the first build
    # compiles eframe from scratch (about 20 minutes).
    env = dict(os.environ)
    env["CARGO_TARGET_DIR"] = str(RS_TARGET)
    env["TMPDIR"] = str(SCRATCH / "tmp")
    r = subprocess.run([CARGO, "build"], capture_output=True,
                       text=True, cwd=work, env=env)
    if r.returncode != 0:
        return None, f"cargo build exit {r.returncode}\n{r.stderr}"
    binary = work / "target" / "debug" / stem
    if not binary.exists():
        binary = RS_TARGET / "debug" / stem
    return [str(binary)], None


def _screenshot(work: Path, shot: Path) -> str | None:
    """Run the screenshot inside the Xvfb session: the run.sh
    wrapper starts the program, waits, captures the root window,
    and kills the program. Returns an error string, or None."""
    r = subprocess.run(
        ["xvfb-run", "-a", "-s", "-screen 0 1024x768x24",
         "sh", str(work / "run.sh")],
        capture_output=True, text=True, cwd=work, timeout=60)
    if not shot.exists():
        return (f"no screenshot produced (xvfb-run exit "
                f"{r.returncode}): {r.stdout}{r.stderr}")
    return None


def _check_shot(shot: Path) -> tuple[int, tuple[int, int, int, int]]:
    """PIL check: the shot must be 1024x768 with non-black pixels.
    Returns (non-black pixel count, bbox). Raises when the shot
    is unreadable or blank."""
    from PIL import Image
    im = Image.open(shot).convert("RGB")
    if im.size != (1024, 768):
        raise ValueError(f"shot is {im.size}, not 1024x768")
    px = im.load()
    count = 0
    xs: list[int] = []
    ys: list[int] = []
    for y in range(0, 768, 2):
        for x in range(0, 1024, 2):
            if px[x, y] != (0, 0, 0):
                count += 1
                xs.append(x)
                ys.append(y)
    if not count:
        raise ValueError("shot is all black: no window rendered")
    bbox = (min(xs), min(ys), max(xs), max(ys))
    return count, bbox


def _write_run_script(work: Path, argv: list[str], shot: Path,
                      wait: float) -> None:
    """The wrapper run inside xvfb-run: start the program, wait
    for the first paint, screenshot, kill the program."""
    import_tool = shutil.which("import")
    if import_tool:
        shot_cmd = f"import -window root \"{shot}\""
    else:
        # Java Robot fallback: compile Shot.java once per run.
        shot_java = work / "Shot.java"
        shot_java.write_text(_SHOT_JAVA, encoding="utf-8")
        c = subprocess.run([JAVAC, str(shot_java)], capture_output=True,
                           text=True, cwd=work)
        if c.returncode != 0:
            raise RuntimeError(
                f"no `import` tool and Shot.java did not compile: "
                f"{c.stderr}")
        shot_cmd = f"java Shot \"{shot}\""
    prog = " ".join(f'"{a}"' for a in argv)
    script = (
        "#!/bin/sh\n"
        f"{prog} > program.log 2>&1 &\n"
        "PROG_PID=$!\n"
        f"sleep {wait}\n"
        f"{shot_cmd} > shot.log 2>&1\n"
        "kill $PROG_PID 2>/dev/null\n"
        "wait $PROG_PID 2>/dev/null\n"
        "exit 0\n"
    )
    (work / "run.sh").write_text(script, encoding="utf-8")


def screenshot_target(target: str, program: Path, out: Path,
                      wait: float) -> int:
    """One target: build, compile, run under Xvfb, screenshot,
    check. Returns 0 on PASS, 1 on FAIL."""
    work = SCRATCH / target
    work.mkdir(parents=True, exist_ok=True)
    shot = out / f"hello_ui_{target}.png"
    err = _build(target, program, work)
    if err:
        return _fail(target, err)
    argv, err = _compile(target, work, program)
    if err:
        return _fail(target, err)
    try:
        _write_run_script(work, argv, shot, wait)
    except RuntimeError as exc:
        return _fail(target, str(exc))
    err = _screenshot(work, shot)
    if err:
        return _fail(target, err)
    try:
        count, bbox = _check_shot(shot)
    except Exception as exc:  # noqa: BLE001 - report, do not crash
        return _fail(target, f"shot check failed: {exc}")
    print(f"PASS {target}: {shot.name} {count} non-black pixels "
          f"(sampled), bbox x {bbox[0]}-{bbox[2]} y {bbox[1]}-{bbox[3]}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", choices=NATIVE_TARGETS,
                        action="append", default=None,
                        help="target to screenshot (repeatable; "
                             "default all five)")
    parser.add_argument("--program", default="examples/hello_ui.newr",
                        help="program to show (default "
                             "examples/hello_ui.newr)")
    parser.add_argument("--out", default=str(SCRATCH),
                        help="directory for the screenshots "
                             "(default the build screenshots dir)")
    parser.add_argument("--wait", type=float, default=None,
                        help="seconds to wait before the shot "
                             "(default per-target)")
    args = parser.parse_args(argv)
    ensure_tool_path()
    program = ROOT / args.program
    if not program.exists():
        print(f"no such program: {program}", file=sys.stderr)
        return 1
    if not shutil.which("xvfb-run"):
        print("no xvfb-run found", file=sys.stderr)
        return 1
    lib_err = _check_x11_libs()
    if lib_err:
        print(lib_err, file=sys.stderr)
        return 1
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (SCRATCH / "tmp").mkdir(parents=True, exist_ok=True)
    targets = args.target or list(NATIVE_TARGETS)
    failures = 0
    for target in targets:
        wait = args.wait if args.wait is not None else WAIT[target]
        failures += screenshot_target(target, program, out, wait)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
