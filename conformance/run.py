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
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
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

# JDBC driver jars (M8a): a `db.*` program needs sqlite-jdbc on the
# runtime classpath (compiling needs only java.sql, which is in the
# JDK). The jars live in ~/jars on the build VM; otherwise they are
# downloaded from Maven Central into that cache (once) and reused.
# Pins verified live (M8a): sqlite-jdbc 3.54.1.0, slf4j-api 2.0.19
# (the version the repo's bot pom and the emitter tests use).
_JDBC_DIR = Path.home() / "jars"
_JDBC_JARS = {
    "sqlite-jdbc": ("sqlite-jdbc-3.54.1.0.jar",
                    "https://repo1.maven.org/maven2/org/xerial/"
                    "sqlite-jdbc/3.54.1.0/sqlite-jdbc-3.54.1.0.jar"),
    "slf4j-api": ("slf4j-api-2.0.19.jar",
                  "https://repo1.maven.org/maven2/org/slf4j/"
                  "slf4j-api/2.0.19/slf4j-api-2.0.19.jar"),
}


def _jdbc_classpath() -> list[str] | None:
    """The sqlite-jdbc + slf4j jars for a db program's runtime
    classpath, downloading them into ~/jars when absent. Returns
    None when they cannot be obtained (the caller skips the db
    program then, instead of failing the whole corpus)."""
    paths = []
    for _name, (fname, url) in _JDBC_JARS.items():
        dest = _JDBC_DIR / fname
        if not dest.exists():
            try:
                import urllib.request
                _JDBC_DIR.mkdir(parents=True, exist_ok=True)
                urllib.request.urlretrieve(url, dest)
            except Exception as exc:  # noqa: BLE001 - network is best-effort
                print(f"WARN: could not download {url}: {exc}")
                return None
        paths.append(str(dest))
    return paths

# name -> (source file, output files the build produces)
GOLDENS = {
    "hello": ("examples/hello.newr", ["hello.py"]),
    "hello_bot": ("examples/hello_bot.newr", ["hello_bot.py"]),
    "showcase": ("examples/showcase.newr", ["showcase.py"]),
    "shop": ("examples/shop.newr", ["shop.py", "shop_items.py"]),
    "shop_items": ("examples/shop_items.newr", ["shop_items.py"]),
    "todo": ("examples/todo.newr", ["todo.py"]),
    "hello_api_client": ("examples/hello_api_client.newr",
                         ["hello_api_client.py"]),
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
    "todo": ("examples/todo.newr", ["todo.js"]),
    "hello_api_client": ("examples/hello_api_client.newr",
                         ["hello_api_client.js"]),
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
    "todo": ("examples/todo.newr", ["todo.java"]),
    "hello_api_client": ("examples/hello_api_client.newr",
                         ["hello_api_client.java"]),
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
    "todo": ("examples/todo.newr", ["todo.cs"]),
    "hello_api_client": ("examples/hello_api_client.newr",
                         ["hello_api_client.cs"]),
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
    "todo": ("examples/todo.newr", ["todo.rs"]),
    "hello_api_client": ("examples/hello_api_client.newr",
                         ["hello_api_client.rs"]),
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
    "todo": ("examples/todo.newr", ["todo.kt"]),
    "hello_api_client": ("examples/hello_api_client.newr",
                         ["hello_api_client.kt"]),
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
    "todo": ("examples/todo.newr", ["todo.dart"]),
    "hello_api_client": ("examples/hello_api_client.newr",
                         ["hello_api_client.dart"]),
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


def _wait_listening(proc: subprocess.Popen, timeout: float = 10.0) -> str | None:
    """Read the server's stdout until its `listening on
    127.0.0.1:PORT` line (the first line it prints), and return
    the port. None when the line never arrives: the server exited
    early, or the wait timed out (a runtime that does not flush
    the line stalls here, which is a real bug, not a slow box)."""
    lines: queue.Queue[str | None] = queue.Queue()

    def _read() -> None:
        try:
            for line in proc.stdout:  # type: ignore[union-attr]
                lines.put(line)
        except Exception:  # noqa: BLE001 - reader thread, just end
            pass
        lines.put(None)  # EOF: the server exited

    threading.Thread(target=_read, daemon=True).start()
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        try:
            line = lines.get(timeout=remaining)
        except queue.Empty:
            return None
        if line is None:
            return None
        m = re.match(r"listening on 127\.0\.0\.1:(\d+)\s*$",
                     line.strip())
        if m:
            return m.group(1)
        # Any other line before the listening line is ignored.


def _pair_cmds(target: str, server_dir: Path,
               client_dir: Path) -> tuple[list[str], list[str],
                                         dict | None] | None:
    """(server argv, client argv without the port, extra env) for
    the http pair test, after any per-target compile step. Returns
    None (after printing SKIP) when the target toolchain is
    missing, matching the single-process loop. Raises
    RuntimeError on a compile failure."""
    suffix = _SUFFIX[target]
    server_main = str(server_dir / (_PAIR_SERVER + suffix))
    client_main = str(client_dir / (_PAIR_CLIENT + suffix))
    if target == "py":
        return ([sys.executable, server_main],
                [sys.executable, client_main], None)
    if target == "js":
        return (["node", server_main], ["node", client_main], None)
    if target == "java":
        if JAVAC is None or JAVA is None:
            print("SKIP java: no JDK found "
                  "(JAVA_HOME / ~/jdk-21 / PATH)")
            return None
        for d in (server_dir, client_dir):
            c = subprocess.run(
                [JAVAC, *[str(p) for p in d.glob("*.java")]],
                capture_output=True, text=True, cwd=d)
            if c.returncode != 0:
                raise RuntimeError(f"javac exit {c.returncode}\n"
                                   f"{c.stderr}")
        return ([JAVA, _PAIR_SERVER], [JAVA, _PAIR_CLIENT], None)
    if target == "rs":
        if RUSTC is None:
            print("SKIP rs: no rustc found "
                  "(CARGO_HOME / ~/.cargo/bin / PATH)")
            return None
        if (server_dir / "Cargo.toml").exists():
            # An http/json program: the crate needs cargo
            # (tiny_http, ureq, serde_json). Build FIRST and run
            # the binary directly: `cargo run` would compile
            # inside the server start, and the 10s listening
            # wait would expire while cargo is still building
            # (ring takes minutes on a cold cache). The [[bin]]
            # name is the program stem.
            if CARGO is None:
                print("SKIP rs: no cargo found "
                      "(CARGO_HOME / ~/.cargo/bin / PATH)")
                return None
            for d in (server_dir, client_dir):
                c = subprocess.run(
                    [CARGO, "build", "--quiet"],
                    capture_output=True, text=True, cwd=d)
                if c.returncode != 0:
                    raise RuntimeError(f"cargo build exit "
                                       f"{c.returncode}\n{c.stderr}")
            return ([str(server_dir / "target" / "debug"
                         / _PAIR_SERVER)],
                    [str(client_dir / "target" / "debug"
                         / _PAIR_CLIENT)], None)
        # Plain program: rustc compiles the bin directly.
        for d, stem in ((server_dir, _PAIR_SERVER),
                        (client_dir, _PAIR_CLIENT)):
            c = subprocess.run(
                [RUSTC, str(d / (stem + suffix)), "-o",
                 str(d / stem)],
                capture_output=True, text=True, cwd=d)
            if c.returncode != 0:
                raise RuntimeError(f"rustc exit {c.returncode}\n"
                                   f"{c.stderr}")
        return ([str(server_dir / _PAIR_SERVER)],
                [str(client_dir / _PAIR_CLIENT)], None)
    if target == "kt":
        if KOTLINC is None or JAVA is None:
            print("SKIP kt: no kotlinc/java found "
                  "(KOTLIN_HOME / ~/kotlin / PATH)")
            return None
        for d in (server_dir, client_dir):
            c = subprocess.run(
                [KOTLINC, *[str(p) for p in d.glob("*.kt")],
                 "-include-runtime", "-d", str(d / "app.jar")],
                capture_output=True, text=True, cwd=d)
            if c.returncode != 0:
                raise RuntimeError(f"kotlinc exit {c.returncode}\n"
                                   f"{c.stderr}")
        return ([JAVA, "-jar", str(server_dir / "app.jar")],
                [JAVA, "-jar", str(client_dir / "app.jar")], None)
    if target == "dart":
        if DART is None:
            print("SKIP dart: no dart found "
                  "(DART_HOME / ~/dart-sdk / PATH)")
            return None
        return ([DART, server_main], [DART, client_main], None)
    # cs
    if DOTNET is None:
        print("SKIP cs: no dotnet found "
              "(DOTNET_ROOT / ~/.dotnet / PATH)")
        return None
    env = dict(os.environ)
    env["DOTNET_CLI_TELEMETRY_OPTOUT"] = "1"
    return ([DOTNET, "run", "--project",
             str(server_dir / (_PAIR_SERVER + ".csproj"))],
            [DOTNET, "run", "--project",
             str(client_dir / (_PAIR_CLIENT + ".csproj")), "--"], env)


def _run_http_pair(target: str) -> int:
    """The http pair test (M8b): mod_http_server binds an
    ephemeral port and blocks forever, so the single-process loop
    cannot run it. Start the server, wait up to 10s for its
    `listening on 127.0.0.1:PORT` line, run mod_http_client with
    the port as argv[1], expect exit 0 with PASS as its last
    line, then kill the server. The server's exit code is never
    checked (it is killed, not exited). Returns 0 on PASS, 1 on
    failure (the FAIL line is printed here)."""
    try:
        server_files = _build_to_temp(
            ROOT / "conformance" / "ok" / (_PAIR_SERVER + ".newr"),
            target=target)
        client_files = _build_to_temp(
            ROOT / "conformance" / "ok" / (_PAIR_CLIENT + ".newr"),
            target=target)
    except RuntimeError as exc:
        print(f"FAIL {_PAIR_CLIENT}.newr (pair): {exc}")
        return 1
    rt_src = _RT_SRC[target]
    rt_copy = _RT_COPY[target]
    rt_text = (ROOT / "compiler" / rt_src).read_text(encoding="utf-8")
    with tempfile.TemporaryDirectory() as sd, \
            tempfile.TemporaryDirectory() as cd:
        server_dir, client_dir = Path(sd), Path(cd)
        for name, text in server_files.items():
            (server_dir / name).write_text(text, encoding="utf-8")
        (server_dir / rt_copy).write_text(rt_text, encoding="utf-8")
        for name, text in client_files.items():
            (client_dir / name).write_text(text, encoding="utf-8")
        (client_dir / rt_copy).write_text(rt_text, encoding="utf-8")
        try:
            cmds = _pair_cmds(target, server_dir, client_dir)
        except RuntimeError as exc:
            print(f"FAIL {_PAIR_CLIENT}.newr (pair): {exc}")
            return 1
        if cmds is None:
            return 0  # toolchain SKIP, printed by _pair_cmds
        server_cmd, client_cmd, env = cmds
        proc = subprocess.Popen(
            server_cmd, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, cwd=server_dir,
            env=env)
        try:
            port = _wait_listening(proc)
            if port is None:
                err = ""
                if proc.poll() is not None:
                    err = proc.stderr.read() if proc.stderr else ""
                print(f"FAIL {_PAIR_CLIENT}.newr (pair): server "
                      f"never printed its listening line "
                      f"(exit {proc.poll()})\n{err}")
                return 1
            try:
                r = subprocess.run(
                    [*client_cmd, port], capture_output=True,
                    text=True, cwd=client_dir, env=env, timeout=60)
            except subprocess.TimeoutExpired:
                print(f"FAIL {_PAIR_CLIENT}.newr (pair): client "
                      "timed out after 60s")
                return 1
            last = (r.stdout.strip().splitlines()[-1]
                    if r.stdout.strip() else "")
            if r.returncode != 0:
                print(f"FAIL {_PAIR_CLIENT}.newr (pair): client "
                      f"exit {r.returncode}\n{r.stderr}")
                return 1
            if last != "PASS":
                print(f"FAIL {_PAIR_CLIENT}.newr (pair): client "
                      f"last line {last!r} != 'PASS' "
                      f"(stdout {r.stdout!r})")
                return 1
            print(f"ok {_PAIR_SERVER}.newr (pair)")
            print(f"ok {_PAIR_CLIENT}.newr (pair, port {port})")
            return 0
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
            if proc.stdout:
                proc.stdout.close()
            if proc.stderr:
                proc.stderr.close()


# The http pair programs (M8b): the server binds an ephemeral
# port and blocks forever, so the single-process loop cannot run
# it; the client takes the port as argv[1]. Handled together by
# _run_http_pair.
_PAIR_SERVER = "mod_http_server"
_PAIR_CLIENT = "mod_http_client"


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
        if path.stem == _PAIR_SERVER:
            # Runs only as half of the pair test (it blocks
            # forever); skip it in the single-process loop.
            continue
        if path.stem == _PAIR_CLIENT:
            failures += _run_http_pair(target)
            continue
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
                if any("NewrRt.dbOpen(" in t for t in files.values()):
                    # A `db.*` program: the driver jar is needed at
                    # run time (compile needs only java.sql).
                    jars = _jdbc_classpath()
                    if jars is None:
                        print(f"SKIP {path.name}: sqlite-jdbc jars "
                              "unavailable")
                        continue
                    cp = os.pathsep.join([str(d), *jars])
                    r = subprocess.run([JAVA, "-cp", cp, path.stem],
                                       capture_output=True, text=True,
                                       cwd=d)
                else:
                    r = subprocess.run([JAVA, path.stem],
                                       capture_output=True, text=True,
                                       cwd=d)
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
                if any("NewrRt.dbOpen(" in t for t in files.values()):
                    # A `db.*` program: the driver jar is needed at
                    # run time. `java -jar` ignores -cp, so run the
                    # main class directly on the classpath (the
                    # facade class is <Stem>Kt, first letter upper).
                    jars = _jdbc_classpath()
                    if jars is None:
                        print(f"SKIP {path.name}: sqlite-jdbc jars "
                              "unavailable")
                        continue
                    cp = os.pathsep.join(
                        [str(Path(d) / "app.jar"), *jars])
                    main_class = (path.stem[:1].upper()
                                  + path.stem[1:] + "Kt")
                    r = subprocess.run([JAVA, "-cp", cp, main_class],
                                       capture_output=True, text=True,
                                       cwd=d)
                else:
                    r = subprocess.run([JAVA, "-jar",
                                        str(Path(d) / "app.jar")],
                                       capture_output=True, text=True,
                                       cwd=d)
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
