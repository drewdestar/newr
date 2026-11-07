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
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LAUNCHER = ROOT / "newr"
GOLDEN_DIR = ROOT / "goldens" / "py"
GOLDEN_DIR_JS = ROOT / "goldens" / "js"

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


def _build_to_temp(source: Path, target: str = "py") -> dict[str, str]:
    """Build a source into a temp dir; return {basename: text}.

    The source is passed repo-relative (e.g. `examples/shop.newr`)
    so the emitted header names it that way (D2) — the golden
    header is deterministic.
    """
    suffix = ".js" if target == "js" else ".py"
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
            if p.name in ("newr_rt.py", "newr_rt.js"):
                continue
            files[p.name] = p.read_text(encoding="utf-8")
        return files


def bless(only: str | None = None, target: str = "py") -> int:
    goldens = GOLDENS_JS if target == "js" else GOLDENS
    golden_dir = GOLDEN_DIR_JS if target == "js" else GOLDEN_DIR
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
    suffix = ".js" if target == "js" else ".py"
    rt_name = "newr_rt.js" if target == "js" else "newr_rt.py"
    for path in sorted(ok_dir.glob("*.newr")):
        files = _build_to_temp(Path(str(path.relative_to(ROOT))),
                               target=target)
        main = path.stem + suffix
        with tempfile.TemporaryDirectory() as d:
            for name, text in files.items():
                (Path(d) / name).write_text(text, encoding="utf-8")
            rt = ROOT / "compiler" / rt_name
            (Path(d) / rt_name).write_text(
                rt.read_text(encoding="utf-8"), encoding="utf-8")
            cmd = (["node", str(Path(d) / main)] if target == "js"
                   else [sys.executable, str(Path(d) / main)])
            r = subprocess.run(cmd, capture_output=True, text=True,
                               cwd=d)
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
    parser.add_argument("--target", default="py", choices=("py", "js"),
                        help="target to build/run (default py)")
    parser.add_argument("--only", metavar="NAME",
                        help="with --bless, regenerate one golden")
    args = parser.parse_args(argv)
    if args.bless:
        return bless(args.only, target=args.target)
    return run_conformance(target=args.target)


if __name__ == "__main__":
    raise SystemExit(main())
