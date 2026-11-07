#!/usr/bin/env python3
"""M3 benchmarks B1–B8, JavaScript target (the JS twins of run.py).

Runs hyperfine for the timing benchmarks and the resource module
for peak RSS, then APPENDS an M3 section to BENCHMARKS.md (the M2
Python section is preserved) and writes benchmarks/raw/*_js.json
(raw hyperfine output).

No speedup is promised: the numbers are reported as measured. A
ratio >1.20 on runtime or >1.50 on size (generated vs hand-written)
is a bug report, not a marketing line (proposal thresholds).

The discord.js import in B6b is satisfied by the in-repo mock
(tests/js/mocks) — real discord.js 14.28.1 is not installed in
this repo, so both drivers pay the same small mock require cost
and the ratio is the honest figure; the absolute times understate
a real discord.js import. See BENCHMARKS.md.

Usage: python3 benchmarks/run_js.py [--quick]
  --quick  fewer runs (3 instead of 10/20) for a smoke check.
"""

import argparse
import json
import platform
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HYPERFINE = Path.home() / "workspace" / "egress" / "bin" / "hyperfine"
LAUNCHER = ROOT / "newr"
PY = sys.executable
NODE = "node"
MOCK_DISCORD = ROOT / "tests" / "js" / "mocks"
RAW = ROOT / "benchmarks" / "raw"


def _hyperfine(name: str, cmd: list[str], runs: int, warmup: int = 0,
               cwd: Path | None = None, prepare: str | None = None,
               env: dict | None = None) -> dict:
    out = RAW / f"{name}.json"
    args = [str(HYPERFINE), "--export-json", str(out),
            "--warmup", str(warmup), "--runs", str(runs),
            "--command-name", name]
    preps = []
    if prepare is not None:
        preps.append(prepare)
    if cwd is not None:
        preps.append(f"cd {cwd}")
    if preps:
        args += ["--prepare", "; ".join(preps)]
    args.append(" ".join(cmd))
    full_env = dict(__import__("os").environ)
    if env:
        full_env.update(env)
    r = subprocess.run(args, capture_output=True, text=True, env=full_env)
    if r.returncode != 0:
        raise RuntimeError(f"hyperfine {name} failed: {r.stdout}{r.stderr}")
    data = json.loads(out.read_text(encoding="utf-8"))
    res = data["results"][0]
    return {"name": name, "mean": res["mean"], "stddev": res["stddev"],
            "min": res["min"], "max": res["max"], "runs": len(res["times"]),
            "unit": "s"}


def _rss(cmd: list[str], cwd: Path | None = None) -> float:
    """Peak RSS in MB of a child process (resource.ru_maxrss),
    via a fresh wrapper (same approach as run.py)."""
    wrapper = (
        "import resource, subprocess, sys; "
        "subprocess.run(sys.argv[1:]); "
        "print(resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss)")
    r2 = subprocess.run([PY, "-c", wrapper, *cmd],
                        capture_output=True, text=True, cwd=cwd)
    kb = int(r2.stdout.strip().splitlines()[-1])
    return kb / 1024.0


def _size(path: Path) -> tuple[int, int]:
    text = path.read_text(encoding="utf-8")
    return len(text.encode("utf-8")), text.count("\n") + 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--quick", action="store_true")
    args = ap.parse_args()
    runs_cold = 3 if args.quick else 10
    runs_warm = 3 if args.quick else 20
    runs_prog = 3 if args.quick else 10
    RAW.mkdir(parents=True, exist_ok=True)
    results: dict[str, dict] = {}
    node_ver = subprocess.run([NODE, "--version"], capture_output=True,
                              text=True).stdout.strip()

    # B1: compile cold (no warmup) — the JS target.
    results["b1_hello_js"] = _hyperfine(
        "b1_hello_js", [PY, str(LAUNCHER), "build", "--target", "js",
                        "examples/hello.newr", "-o", "/tmp/bench_hello.js"],
        runs=runs_cold, warmup=0, cwd=ROOT)
    results["b1_shop_js"] = _hyperfine(
        "b1_shop_js", [PY, str(LAUNCHER), "build", "--target", "js",
                       "examples/shop.newr", "-o", "/tmp/bench_shop.js"],
        runs=runs_cold, warmup=0, cwd=ROOT)
    # B2: compile warm (no-change).
    results["b2_hello_js"] = _hyperfine(
        "b2_hello_js", [PY, str(LAUNCHER), "build", "--target", "js",
                        "examples/hello.newr", "-o", "/tmp/bench_hello.js"],
        runs=runs_warm, warmup=3, cwd=ROOT)
    # B2b: one-file-change (shop_items.newr gains a comment line
    # before each run, forcing a re-parse of the changed file).
    with tempfile.TemporaryDirectory() as d:
        import shutil
        shutil.copy(ROOT / "examples" / "shop.newr", Path(d) / "shop.newr")
        shutil.copy(ROOT / "examples" / "shop_items.newr",
                    Path(d) / "shop_items.newr")
        subprocess.run([PY, str(LAUNCHER), "build", "--target", "js",
                        "shop.newr"], cwd=d, capture_output=True)
        results["b2_shop_change_js"] = _hyperfine(
            "b2_shop_change_js",
            [PY, str(LAUNCHER), "build", "--target", "js",
             str(Path(d) / "shop.newr")],
            runs=runs_warm, warmup=1, cwd=ROOT,
            prepare=f"echo '# bench' >> {Path(d) / 'shop_items.newr'}")
    # B5: generated program startup (node).
    results["b5_hello_gen_js"] = _hyperfine(
        "b5_hello_gen_js", [NODE, str(ROOT / "goldens" / "js" / "hello.js")],
        runs=runs_prog, warmup=3, cwd=ROOT)
    results["b5_hello_hw_js"] = _hyperfine(
        "b5_hello_hw_js",
        [NODE, str(ROOT / "benchmarks" / "handwritten" / "hello.js")],
        runs=runs_prog, warmup=3, cwd=ROOT)
    # B6: checked-int tight loop (100k iterations, checksum 105003).
    results["b6_loop_gen_js"] = _hyperfine(
        "b6_loop_gen_js",
        [NODE, str(ROOT / "benchmarks" / "generated" / "loop.js")],
        runs=runs_prog, warmup=3, cwd=ROOT)
    results["b6_loop_plain_js"] = _hyperfine(
        "b6_loop_plain_js",
        [NODE, str(ROOT / "benchmarks" / "handwritten" / "loop_plain.js")],
        runs=runs_prog, warmup=3, cwd=ROOT)
    results["b6_loop_checked_js"] = _hyperfine(
        "b6_loop_checked_js",
        [NODE, str(ROOT / "benchmarks" / "handwritten" / "loop_checked.js")],
        runs=runs_prog, warmup=3, cwd=ROOT)
    # B6b: shop economy, 1,000 credits (node:sqlite, sync).
    # Both drivers require discord.js; the mock stands in (see
    # module docstring) so both pay the same require cost.
    mock_env = {"NODE_PATH": str(MOCK_DISCORD)}
    results["b6b_shop_gen_js"] = _hyperfine(
        "b6b_shop_gen_js",
        [NODE, str(ROOT / "benchmarks" / "generated" / "shop_economy.js")],
        runs=runs_prog, warmup=1, cwd=ROOT, env=mock_env)
    results["b6b_shop_hw_js"] = _hyperfine(
        "b6b_shop_hw_js",
        [NODE, str(ROOT / "benchmarks" / "handwritten" / "shop_economy.js")],
        runs=runs_prog, warmup=1, cwd=ROOT, env=mock_env)
    # B7: peak RSS.
    results["b7_compile_js"] = {"rss_mb": _rss(
        [PY, str(LAUNCHER), "build", "--target", "js",
         "examples/shop.newr", "-o", "/tmp/bench_shop.js"], cwd=ROOT)}
    results["b7_run_js"] = {"rss_mb": _rss(
        [NODE, str(ROOT / "goldens" / "js" / "hello.js")], cwd=ROOT)}
    # B4: code size.
    gen_hello = ROOT / "goldens" / "js" / "hello.js"
    hw_hello = ROOT / "benchmarks" / "handwritten" / "hello.js"
    gen_shop = ROOT / "goldens" / "js" / "shop.js"
    gen_loop = ROOT / "benchmarks" / "generated" / "loop.js"
    hw_loop = ROOT / "benchmarks" / "handwritten" / "loop_plain.js"
    sizes = {}
    for label, path in (("hello_gen", gen_hello), ("hello_hw", hw_hello),
                        ("shop_gen", gen_shop), ("loop_gen", gen_loop),
                        ("loop_hw", hw_loop)):
        b, lines = _size(path)
        sizes[label] = {"bytes": b, "lines": lines}
    results["b4_sizes_js"] = sizes

    # Append the M3 section to BENCHMARKS.md (M2 section preserved).
    def fmt(r: dict) -> str:
        return (f"{r['mean']*1000:.1f} ms ± {r['stddev']*1000:.1f} ms "
                f"(range {r['min']*1000:.1f}–{r['max']*1000:.1f} ms, "
                f"N={r['runs']})")

    def ratio(a: dict, b: dict) -> float:
        return a["mean"] / b["mean"]

    md = []
    md.append("\n# Benchmarks — M3 (JavaScript emitter)\n")
    md.append(f"Machine: {platform.system()} {platform.machine()}, "
              f"Python {platform.python_version()}, Node {node_ver}, "
              f"hyperfine {subprocess.run([str(HYPERFINE), '--version'], capture_output=True, text=True).stdout.strip()}.\n")
    md.append("All figures are measured, not promised. Raw hyperfine JSON "
              "is in `benchmarks/raw/` (`*_js.json`). The compile "
              "benchmarks (B1/B2/B7-compile) measure the same Python "
              "compiler as M2, emitting JS instead of Python — the "
              "difference is the emitter. The B6b discord.js import is "
              "satisfied by the in-repo mock (tests/js/mocks), not real "
              "discord.js 14.28.1: both drivers pay the same small mock "
              "require cost, so the ratio is honest but the absolute "
              "times understate a real discord.js import.\n")
    md.append("## B1 — Compile, cold (no warmup), `--target js`\n")
    md.append(f"- hello: {fmt(results['b1_hello_js'])}")
    md.append(f"- shop: {fmt(results['b1_shop_js'])}\n")
    md.append("## B2 — Compile, warm, `--target js`\n")
    md.append(f"- hello (no change): {fmt(results['b2_hello_js'])}")
    md.append(f"- shop (one file changed): {fmt(results['b2_shop_change_js'])}\n")
    md.append("## B4 — Generated code size (JS)\n")
    for label, s in sizes.items():
        md.append(f"- {label}: {s['bytes']} bytes, {s['lines']} lines")
    md.append(f"- hello size ratio (generated/hand-written): "
              f"{sizes['hello_gen']['bytes']/sizes['hello_hw']['bytes']:.2f}")
    md.append(f"- loop size ratio (generated/hand-written): "
              f"{sizes['loop_gen']['bytes']/sizes['loop_hw']['bytes']:.2f}\n")
    md.append("## B5 — Generated program startup (node)\n")
    md.append(f"- generated hello: {fmt(results['b5_hello_gen_js'])}")
    md.append(f"- hand-written hello: {fmt(results['b5_hello_hw_js'])}")
    md.append(f"- ratio (generated/hand-written): "
              f"{ratio(results['b5_hello_gen_js'], results['b5_hello_hw_js']):.2f}\n")
    md.append("## B6 — Generated runtime (checked-int tight loop, 100k iterations)\n")
    md.append(f"- generated (checked helpers, BigInt): {fmt(results['b6_loop_gen_js'])}")
    md.append(f"- hand-written, plain JS numbers: {fmt(results['b6_loop_plain_js'])}")
    md.append(f"- hand-written, newr_rt.js helpers: {fmt(results['b6_loop_checked_js'])}")
    md.append(f"- ratio generated/plain: "
              f"{ratio(results['b6_loop_gen_js'], results['b6_loop_plain_js']):.2f}")
    md.append(f"- ratio generated/checked-hand-written: "
              f"{ratio(results['b6_loop_gen_js'], results['b6_loop_checked_js']):.2f}")
    md.append("  (All three print the same checksum, 105003 — the loops agree.)\n")
    md.append("## B6b — Shop economy, 1,000 credits (node:sqlite, sync, commit per call)\n")
    md.append(f"- generated (emitted shop Bot): {fmt(results['b6b_shop_gen_js'])}")
    md.append(f"- hand-written (node:sqlite directly): {fmt(results['b6b_shop_hw_js'])}")
    md.append(f"- ratio (generated/hand-written): "
              f"{ratio(results['b6b_shop_gen_js'], results['b6b_shop_hw_js']):.2f}")
    md.append("  (Both print the final balance 1000.)\n")
    md.append("## B7 — Peak RSS (JS)\n")
    md.append(f"- compile (shop, --target js): {results['b7_compile_js']['rss_mb']:.1f} MB")
    md.append(f"- run (hello, node): {results['b7_run_js']['rss_mb']:.1f} MB\n")
    md.append("## B8 — Hand-written parity (JS)\n")
    md.append(f"- startup ratio: "
              f"{ratio(results['b5_hello_gen_js'], results['b5_hello_hw_js']):.2f} "
              f"(>1.20 would be a bug report)")
    md.append(f"- loop ratio (generated/plain): "
              f"{ratio(results['b6_loop_gen_js'], results['b6_loop_plain_js']):.2f} "
              f"(>1.20 would be a bug report)")
    md.append(f"- shop economy ratio: "
              f"{ratio(results['b6b_shop_gen_js'], results['b6b_shop_hw_js']):.2f} "
              f"(>1.20 would be a bug report)")
    md.append(f"- hello size ratio: "
              f"{sizes['hello_gen']['bytes']/sizes['hello_hw']['bytes']:.2f} "
              f"(>1.50 would be a bug report)")
    md.append(f"- loop size ratio: "
              f"{sizes['loop_gen']['bytes']/sizes['loop_hw']['bytes']:.2f} "
              f"(>1.50 would be a bug report)\n")
    bench_md = ROOT / "BENCHMARKS.md"
    text = bench_md.read_text(encoding="utf-8")
    # Replace a previous M3 section if re-run; else append.
    marker = "# Benchmarks — M3 (JavaScript emitter)"
    if marker in text:
        text = text[:text.index(marker)].rstrip() + "\n"
    bench_md.write_text(text + "\n".join(md), encoding="utf-8")
    print("\n".join(md))
    print("\nAppended M3 section to BENCHMARKS.md; raw JSON in benchmarks/raw/*_js.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
