#!/usr/bin/env python3
"""Conformance harness + golden regenerator (M2).

Usage:
  python3 conformance/run.py                 run the conformance corpus
  python3 conformance/run.py --target js     run it for the JS target
  python3 conformance/run.py --bless         regenerate goldens/py/*.py
  python3 conformance/run.py --bless --target js
                                             regenerate goldens/js/*.js
  python3 conformance/run.py --bless --only <name>
                                             regenerate one golden

Golden policy (BUILD_REPORT.md D3): goldens are compare-only by
default — `tests/test_goldens.py` builds each golden's source to a
temp dir and diffs it against the golden file. Regeneration needs
an explicit `--bless`; never hand-edit a golden. Every golden diff
is reviewed line-by-line.

The header names the input file as passed (D2), so --bless always
builds from the repo-relative `examples/<name>.newr` path — that
keeps the golden header deterministic.
"""

import argparse
import difflib
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LAUNCHER = ROOT / "newr"
GOLDEN_DIR = ROOT / "goldens" / "py"
GOLDEN_DIR_JS = ROOT / "goldens" / "js"
GOLDEN_DIR_JAVA = ROOT / "goldens" / "java"
GOLDEN_DIR_CS = ROOT / "goldens" / "cs"
GOLDEN_DIR_RS = ROOT / "goldens" / "rs"
GOLDEN_DIR_KT = ROOT / "goldens" / "kt"
GOLDEN_DIR_DART = ROOT / "goldens" / "dart"

# Toolchains (M4): the JDK lives in ~/jdk-21 on the build VM (or
# JAVA_HOME / the PATH, e.g. CI's setup-java); dotnet lives in
# ~/.dotnet (or DOTNET_ROOT / the PATH, e.g. CI's setup-dotnet).
_JAVA_HOME = Path(os.environ["JAVA_HOME"]) \
    if os.environ.get("JAVA_HOME") else (Path.home() / "jdk-21")
_JAVA_BIN = _JAVA_HOME / "bin"
JAVA = shutil.which("java", path=str(_JAVA_BIN)) or shutil.which("java")
JAVAC = shutil.which("javac", path=str(_JAVA_BIN)) or shutil.which("javac")
_DOTNET_ROOT = Path(os.environ["DOTNET_ROOT"]) \
    if os.environ.get("DOTNET_ROOT") else (Path.home() / ".dotnet")
DOTNET = shutil.which("dotnet", path=str(_DOTNET_ROOT)) \
    or shutil.which("dotnet")

# Toolchains (M5): rustc/cargo live in ~/.cargo/bin (rustup) or the
# PATH (CI's rust toolchain); kotlinc in ~/kotlin/kotlinc/bin or
# the PATH; dart in ~/dart-sdk/dart-sdk/bin or the PATH.
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
DART = shutil.which("dart", path=str(_DART_HOME / "dart-sdk" / "bin")) \
    or shutil.which("dart")

# name -> (source file, output files the build produces)
GOLDENS = {
    "hello": ("examples/hello.newr", ["hello.py"]),
    "hello_bot": ("examples/hello_bot.newr", ["hello_bot.py"]),
    "showcase": ("examples/showcase.newr", ["showcase.py"]),
    "shop": ("examples/shop.newr", ["shop.py", "shop_items.py"]),
    "shop_items": ("examples/shop_items.newr", ["shop_items.py"]),
}

# The JS twin of GOLDENS (M3): the same sources, `.js` outputs.
# Bot programs also produce their standalone `<stem>_deploy.js`
# registration script, which is a golden too.
GOLDENS_JS = {
    "hello": ("examples/hello.newr", ["hello.js"]),
    "hello_bot": ("examples/hello_bot.newr",
                  ["hello_bot.js", "hello_bot_deploy.js"]),
    "showcase": ("examples/showcase.newr", ["showcase.js"]),
    "shop": ("examples/shop.newr",
             ["shop.js", "shop_items.js", "shop_deploy.js"]),
    "shop_items": ("examples/shop_items.newr", ["shop_items.js"]),
}

# The Java twin of GOLDENS (M4): the same sources, `.java` outputs.
# The runtime is copied as NewrRt.java (K1) and is not a golden.
GOLDENS_JAVA = {
    "hello": ("examples/hello.newr", ["hello.java"]),
    "hello_bot": ("examples/hello_bot.newr",
                  ["hello_bot.java", "hello_bot_deploy.java"]),
    "showcase": ("examples/showcase.newr", ["showcase.java"]),
    "shop": ("examples/shop.newr",
             ["shop.java", "shop_items.java", "shop_deploy.java"]),
    "shop_items": ("examples/shop_items.newr", ["shop_items.java"]),
}

# The C# twin of GOLDENS (M4): the same sources, `.cs` outputs.
GOLDENS_CS = {
    "hello": ("examples/hello.newr", ["hello.cs"]),
    "hello_bot": ("examples/hello_bot.newr",
                  ["hello_bot.cs", "hello_bot_deploy.cs"]),
    "showcase": ("examples/showcase.newr", ["showcase.cs"]),
    "shop": ("examples/shop.newr",
             ["shop.cs", "shop_items.cs", "shop_deploy.cs"]),
    "shop_items": ("examples/shop_items.newr", ["shop_items.cs"]),
}

# The Rust twin of GOLDENS (M5): the same sources, `.rs` outputs.
# Bot programs also produce their standalone `<stem>_deploy.rs`
# registration script. The Cargo.toml is build scaffolding, not a
# golden (bless() ignores it; the conformance runner needs it in
# the temp dir to `cargo run` a bot program).
GOLDENS_RS = {
    "hello": ("examples/hello.newr", ["hello.rs"]),
    "hello_bot": ("examples/hello_bot.newr",
                  ["hello_bot.rs", "hello_bot_deploy.rs"]),
    "showcase": ("examples/showcase.newr", ["showcase.rs"]),
    "shop": ("examples/shop.newr",
             ["shop.rs", "shop_deploy.rs"]),
    "shop_items": ("examples/shop_items.newr", ["shop_items.rs"]),
}

# The Kotlin twin of GOLDENS (M5): the same sources, `.kt` outputs.
# Bot programs also produce their standalone `<stem>_deploy.kt`
# registration script.
GOLDENS_KT = {
    "hello": ("examples/hello.newr", ["hello.kt"]),
    "hello_bot": ("examples/hello_bot.newr",
                  ["hello_bot.kt", "hello_bot_deploy.kt"]),
    "showcase": ("examples/showcase.newr", ["showcase.kt"]),
    "shop": ("examples/shop.newr",
             ["shop.kt", "shop_deploy.kt"]),
    "shop_items": ("examples/shop_items.newr", ["shop_items.kt"]),
}

# The Dart twin of GOLDENS (M5): the same sources, `.dart` outputs.
# Bot programs also produce their standalone `<stem>_deploy.dart`
# registration script. The pubspec.yaml is build scaffolding, not a
# golden (bless() ignores it; the conformance runner needs it in
# the temp dir to `dart pub get` a bot program).
GOLDENS_DART = {
    "hello": ("examples/hello.newr", ["hello.dart"]),
    "hello_bot": ("examples/hello_bot.newr",
                  ["hello_bot.dart", "hello_bot_deploy.dart"]),
    "showcase": ("examples/showcase.newr", ["showcase.dart"]),
    "shop": ("examples/shop.newr",
             ["shop.dart", "shop_items.dart", "shop_deploy.dart"]),
    "shop_items": ("examples/shop_items.newr", ["shop_items.dart"]),
}

_SUFFIX = {"py": ".py", "js": ".js", "java": ".java", "cs": ".cs",
           "rs": ".rs", "kt": ".kt", "dart": ".dart"}
_RT_SRC = {"py": "newr_rt.py", "js": "newr_rt.js",
           "java": "newr_rt.java", "cs": "newr_rt.cs",
           "rs": "newr_rt.rs", "kt": "newr_rt.kt",
           "dart": "newr_rt.dart"}
# The runtime's file name next to the output (Java renames on copy,
# K1; the others keep their name).
_RT_COPY = {"py": "newr_rt.py", "js": "newr_rt.js",
            "java": "NewrRt.java", "cs": "newr_rt.cs",
            "rs": "newr_rt.rs", "kt": "newr_rt.kt",
            "dart": "newr_rt.dart"}
_GOLDENS_BY_TARGET = {"py": GOLDENS, "js": GOLDENS_JS,
                      "java": GOLDENS_JAVA, "cs": GOLDENS_CS,
                      "rs": GOLDENS_RS, "kt": GOLDENS_KT,
                      "dart": GOLDENS_DART}
_GOLDEN_DIR_BY_TARGET = {"py": GOLDEN_DIR, "js": GOLDEN_DIR_JS,
                         "java": GOLDEN_DIR_JAVA, "cs": GOLDEN_DIR_CS,
                         "rs": GOLDEN_DIR_RS, "kt": GOLDEN_DIR_KT,
                         "dart": GOLDEN_DIR_DART}


def _build_to_temp(source: Path, target: str = "py") -> dict[str, str]:
    """Build a source into a temp dir; return {basename: text}.

    The source is passed repo-relative (e.g. `examples/shop.newr`)
    so the emitted header names it that way (D2) — the golden
    header is deterministic.
    """
    suffix = _SUFFIX[target]
    with tempfile.TemporaryDirectory() as d:
        out = Path(d) / (source.stem + suffix)
        r = subprocess.run(
            [sys.executable, str(LAUNCHER), "build", "--target", target,
             str(source), "-o", str(out)],
            capture_output=True, text=True, cwd=ROOT)
        if r.returncode != 0:
            raise RuntimeError(
                f"build failed for {source}: {r.stdout}{r.stderr}")
        files = {}
        for p in Path(d).rglob(f"*{suffix}"):
            if p.name in ("newr_rt.py", "newr_rt.js", "newr_rt.java",
                          "NewrRt.java", "newr_rt.cs", "newr_rt.rs",
                          "newr_rt.kt", "newr_rt.dart"):
                continue
            files[p.name] = p.read_text(encoding="utf-8")
        if target == "cs":
            # The .csproj is build scaffolding, not a golden — but
            # the conformance runner needs it in the temp dir to
            # `dotnet run` the program (the glob above only matches
            # *.cs). bless() ignores it: GOLDENS_CS lists only .cs.
            for p in Path(d).rglob("*.csproj"):
                files[p.name] = p.read_text(encoding="utf-8")
        if target == "rs":
            # The Cargo.toml is build scaffolding, not a golden —
            # the conformance runner needs it in the temp dir to
            # `cargo run` a bot program (plain programs compile
            # with plain rustc and need no Cargo.toml).
            for p in Path(d).rglob("Cargo.toml"):
                files[p.name] = p.read_text(encoding="utf-8")
        if target == "dart":
            # The pubspec.yaml is build scaffolding, not a golden —
            # the conformance runner needs it in the temp dir to
            # `dart pub get` a bot program (plain programs run with
            # plain `dart` and need no pubspec).
            for p in Path(d).rglob("pubspec.yaml"):
                files[p.name] = p.read_text(encoding="utf-8")
        return files


def bless(only: str | None = None, target: str = "py") -> int:
    goldens = _GOLDENS_BY_TARGET[target]
    golden_dir = _GOLDEN_DIR_BY_TARGET[target]
    names = [only] if only else sorted(goldens)
    for name in names:
        if name not in goldens:
            print(f"unknown golden `{name}` (known: "
                  + ", ".join(sorted(goldens)) + ")", file=sys.stderr)
            return 1
        source, outputs = goldens[name]
        files = _build_to_temp(Path(source), target=target)
        golden_dir.mkdir(parents=True, exist_ok=True)
        for out in outputs:
            if out not in files:
                print(f"build of {source} did not produce {out}",
                      file=sys.stderr)
                return 1
            dest = golden_dir / out
            dest.write_text(files[out], encoding="utf-8")
            print(f"blessed {dest.relative_to(ROOT)} from {source}")
    return 0


def run_conformance(target: str = "py") -> int:
    """Build + run every ok program; each must exit 0 and print
    `PASS` as its last line (the program self-verifies — the
    honest equivalent of Gleam's model, since v1 has no in-language
    test framework). A program may print other lines before its
    verdict."""
    ok_dir = ROOT / "conformance" / "ok"
    failures = 0
    suffix = _SUFFIX[target]
    rt_src = _RT_SRC[target]
    rt_copy = _RT_COPY[target]
    for path in sorted(ok_dir.glob("*.newr")):
        files = _build_to_temp(Path(str(path.relative_to(ROOT))),
                               target=target)
        main = path.stem + suffix
        with tempfile.TemporaryDirectory() as d:
            for name, text in files.items():
                (Path(d) / name).write_text(text, encoding="utf-8")
            rt = ROOT / "compiler" / rt_src
            (Path(d) / rt_copy).write_text(
                rt.read_text(encoding="utf-8"), encoding="utf-8")
            if target == "js":
                cmd = ["node", str(Path(d) / main)]
                r = subprocess.run(cmd, capture_output=True, text=True,
                                   cwd=d)
            elif target == "py":
                cmd = [sys.executable, str(Path(d) / main)]
                r = subprocess.run(cmd, capture_output=True, text=True,
                                   cwd=d)
            elif target == "java":
                if JAVAC is None or JAVA is None:
                    print("SKIP java: no JDK found "
                          "(JAVA_HOME / ~/jdk-21 / PATH)")
                    return 0
                java_files = [str(p) for p in Path(d).glob("*.java")]
                c = subprocess.run([JAVAC, *java_files],
                                   capture_output=True, text=True, cwd=d)
                if c.returncode != 0:
                    print(f"FAIL {path.name}: javac exit "
                          f"{c.returncode}\n{c.stderr}")
                    failures += 1
                    continue
                r = subprocess.run([JAVA, path.stem],
                                   capture_output=True, text=True, cwd=d)
            elif target == "rs":
                if RUSTC is None:
                    print("SKIP rs: no rustc found "
                          "(CARGO_HOME / ~/.cargo/bin / PATH)")
                    return 0
                if "Cargo.toml" in files:
                    # A bot program (or a store program): the crate
                    # needs cargo (serenity/poise, rusqlite).
                    if CARGO is None:
                        print("SKIP rs: no cargo found "
                              "(CARGO_HOME / ~/.cargo/bin / PATH)")
                        return 0
                    r = subprocess.run(
                        [CARGO, "run", "--quiet"],
                        capture_output=True, text=True, cwd=d)
                else:
                    # Plain program: rustc compiles the bin
                    # directly; `mod newr_rt;` resolves from the
                    # same directory.
                    c = subprocess.run(
                        [RUSTC, str(Path(d) / main), "-o",
                         str(Path(d) / path.stem)],
                        capture_output=True, text=True, cwd=d)
                    if c.returncode != 0:
                        print(f"FAIL {path.name}: rustc exit "
                              f"{c.returncode}\n{c.stderr}")
                        failures += 1
                        continue
                    r = subprocess.run([str(Path(d) / path.stem)],
                                       capture_output=True, text=True,
                                       cwd=d)
            elif target == "kt":
                if KOTLINC is None or JAVA is None:
                    print("SKIP kt: no kotlinc/java found "
                          "(KOTLIN_HOME / ~/kotlin / PATH)")
                    return 0
                kt_files = [str(p) for p in Path(d).glob("*.kt")]
                c = subprocess.run(
                    [KOTLINC, *kt_files, "-include-runtime",
                     "-d", str(Path(d) / "app.jar")],
                    capture_output=True, text=True, cwd=d)
                if c.returncode != 0:
                    print(f"FAIL {path.name}: kotlinc exit "
                          f"{c.returncode}\n{c.stderr}")
                    failures += 1
                    continue
                r = subprocess.run([JAVA, "-jar",
                                    str(Path(d) / "app.jar")],
                                   capture_output=True, text=True, cwd=d)
            elif target == "dart":
                if DART is None:
                    print("SKIP dart: no dart found "
                          "(DART_HOME / ~/dart-sdk / PATH)")
                    return 0
                if "pubspec.yaml" in files:
                    # A bot program (nyxx): resolve packages first.
                    g = subprocess.run(
                        [DART, "pub", "get"],
                        capture_output=True, text=True, cwd=d)
                    if g.returncode != 0:
                        print(f"FAIL {path.name}: dart pub get exit "
                              f"{g.returncode}\n{g.stderr}")
                        failures += 1
                        continue
                    r = subprocess.run([DART, "run", str(Path(d) / main)],
                                       capture_output=True, text=True,
                                       cwd=d)
                else:
                    r = subprocess.run([DART, str(Path(d) / main)],
                                       capture_output=True, text=True,
                                       cwd=d)
            else:  # cs
                if DOTNET is None:
                    print("SKIP cs: no dotnet found "
                          "(DOTNET_ROOT / ~/.dotnet / PATH)")
                    return 0
                env = dict(os.environ)
                env["DOTNET_CLI_TELEMETRY_OPTOUT"] = "1"
                r = subprocess.run(
                    [DOTNET, "run", "--project",
                     str(Path(d) / (path.stem + ".csproj"))],
                    capture_output=True, text=True, cwd=d, env=env)
        last = r.stdout.strip().splitlines()[-1] if r.stdout.strip() else ""
        if r.returncode != 0:
            print(f"FAIL {path.name}: exit {r.returncode}\n{r.stderr}")
            failures += 1
        elif last != "PASS":
            print(f"FAIL {path.name}: last line {last!r} != 'PASS' "
                  f"(stdout {r.stdout!r})")
            failures += 1
        else:
            print(f"ok {path.name}")
    return 1 if failures else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bless", action="store_true",
                        help="regenerate goldens for the target")
    parser.add_argument("--target", default="py",
                        choices=("py", "js", "java", "cs", "rs", "kt",
                                 "dart"),
                        help="target to build/run (default py)")
    parser.add_argument("--only", metavar="NAME",
                        help="with --bless, regenerate one golden")
    args = parser.parse_args(argv)
    if args.bless:
        return bless(args.only, target=args.target)
    return run_conformance(target=args.target)


if __name__ == "__main__":
    raise SystemExit(main())
