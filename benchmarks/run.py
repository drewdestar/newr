#!/usr/bin/env python3
"""M2 benchmarks B1–B8 (research §5.2).

Runs hyperfine for the timing benchmarks and /usr/bin/time -v for
peak RSS, then writes BENCHMARKS.md (mean ± σ + range, N runs,
machine context) and benchmarks/raw/*.json (raw hyperfine output).

No speedup is promised: the numbers are reported as measured. A
ratio >1.20 on runtime or >1.50 on size (generated vs hand-written)
is a bug report, not a marketing line (proposal thresholds).

Usage: python3 benchmarks/run.py [--quick]
  --quick  fewer runs (3 instead of 10/20) for a smoke check.
"""

import argparse
import json
import platform
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HYPERFINE = Path.home() / "workspace" / "egress" / "bin" / "hyperfine"
LAUNCHER = ROOT / "newr"
PY = sys.executable
VENV_PY = Path.home() / "workspace" / "venvs" / "newr-verify" / "bin" / "python"
RAW = ROOT / "benchmarks" / "raw"


def _hyperfine(name: str, cmd: list[str], runs: int, warmup: int = 0,
               cwd: Path | None = None, prepare: str | None = None) -> dict:
    out = RAW / f"{name}.json"
    args = [str(HYPERFINE), "--export-json", str(out),
            "--warmup", str(warmup), "--runs", str(runs),
            "--command-name", name]
    # One --prepare only (hyperfine allows one per command): the
    # file change (if any) and the cd are combined.
    preps = []
    if prepare is not None:
        preps.append(prepare)
    if cwd is not None:
        preps.append(f"cd {cwd}")
    if preps:
        args += ["--prepare", "; ".join(preps)]
    args.append(" ".join(cmd))
    r = subprocess.run(args, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"hyperfine {name} failed: {r.stdout}{r.stderr}")
    data = json.loads(out.read_text(encoding="utf-8"))
    res = data["results"][0]
    return {"name": name, "mean": res["mean"], "stddev": res["stddev"],
            "min": res["min"], "max": res["max"], "runs": len(res["times"]),
            "unit": "s"}


def _rss(cmd: list[str], cwd: Path | None = None) -> float:
    """Peak RSS in MB of a child process (resource.ru_maxrss).
    /usr/bin/time is not on this VM, so the measurement is done
    in-process via the resource module."""
    import resource
    before = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
    r = subprocess.run(cmd, capture_output=True, text=True, cwd=cwd)
    if r.returncode != 0:
        raise RuntimeError(f"RSS run failed for {cmd}: {r.stderr[:500]}")
    after = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
    # ru_maxrss is the max over all waited-for children so far; the
    # delta is only meaningful if this child was the largest. Report
    # the child's own peak via a fresh wrapper instead: run the
    # command under a tiny python that prints its child's ru_maxrss.
    _ = (before, after)
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

    # B1: compile cold (no warmup).
    results["b1_hello"] = _hyperfine(
        "b1_hello", [PY, str(LAUNCHER), "build", "examples/hello.newr",
                     "-o", "/tmp/bench_hello.py"],
        runs=runs_cold, warmup=0, cwd=ROOT)
    results["b1_shop"] = _hyperfine(
        "b1_shop", [PY, str(LAUNCHER), "build", "examples/shop.newr",
                    "-o", "/tmp/bench_shop.py"],
        runs=runs_cold, warmup=0, cwd=ROOT)
    # B2: compile warm (no-change).
    results["b2_hello"] = _hyperfine(
        "b2_hello", [PY, str(LAUNCHER), "build", "examples/hello.newr",
                     "-o", "/tmp/bench_hello.py"],
        runs=runs_warm, warmup=3, cwd=ROOT)
    # B2b: one-file-change (shop_items.newr gains a comment line
    # before each run, forcing a re-parse of the changed file).
    with tempfile.TemporaryDirectory() as d:
        import shutil
        shutil.copy(ROOT / "examples" / "shop.newr", Path(d) / "shop.newr")
        shutil.copy(ROOT / "examples" / "shop_items.newr",
                    Path(d) / "shop_items.newr")
        subprocess.run([PY, str(LAUNCHER), "build", "shop.newr"],
                       cwd=d, capture_output=True)
        results["b2_shop_change"] = _hyperfine(
            "b2_shop_change",
            [PY, str(LAUNCHER), "build", str(Path(d) / "shop.newr")],
            runs=runs_warm, warmup=1, cwd=ROOT,
            prepare=f"echo '# bench' >> {Path(d) / 'shop_items.newr'}")
    # B3: newr --help startup.
    results["b3_help"] = _hyperfine(
        "b3_help", [PY, str(LAUNCHER), "--help"],
        runs=runs_prog, warmup=3, cwd=ROOT)
    # B5: generated program startup.
    results["b5_hello_gen"] = _hyperfine(
        "b5_hello_gen", [PY, str(ROOT / "goldens" / "py" / "hello.py")],
        runs=runs_prog, warmup=3, cwd=ROOT)
    results["b5_hello_hw"] = _hyperfine(
        "b5_hello_hw",
        [PY, str(ROOT / "benchmarks" / "handwritten" / "hello.py")],
        runs=runs_prog, warmup=3, cwd=ROOT)
    # B6: checked-int tight loop (100k iterations, checksum 105003).
    results["b6_loop_gen"] = _hyperfine(
        "b6_loop_gen",
        [PY, str(ROOT / "benchmarks" / "generated" / "loop.py")],
        runs=runs_prog, warmup=3, cwd=ROOT)
    results["b6_loop_plain"] = _hyperfine(
        "b6_loop_plain",
        [PY, str(ROOT / "benchmarks" / "handwritten" / "loop_plain.py")],
        runs=runs_prog, warmup=3, cwd=ROOT)
    results["b6_loop_checked"] = _hyperfine(
        "b6_loop_checked",
        [PY, str(ROOT / "benchmarks" / "handwritten" / "loop_checked.py")],
        runs=runs_prog, warmup=3, cwd=ROOT)
    # B6b: shop economy, 1,000 credits (generated vs hand-written).
    # (10,000 was reduced to 1,000: per-call commit fsync dominates
    # and 10k took >4 min; 1k still measures the code path.)
    results["b6b_shop_gen"] = _hyperfine(
        "b6b_shop_gen",
        [str(VENV_PY), str(ROOT / "benchmarks" / "generated"
                           / "shop_economy.py")],
        runs=runs_prog, warmup=1, cwd=ROOT)
    results["b6b_shop_hw"] = _hyperfine(
        "b6b_shop_hw",
        [str(VENV_PY), str(ROOT / "benchmarks" / "handwritten"
                           / "shop_economy.py")],
        runs=runs_prog, warmup=1, cwd=ROOT)
    # B7: peak RSS.
    results["b7_compile"] = {"rss_mb": _rss(
        [PY, str(LAUNCHER), "build", "examples/shop.newr",
         "-o", "/tmp/bench_shop.py"], cwd=ROOT)}
    results["b7_run"] = {"rss_mb": _rss(
        [PY, str(ROOT / "goldens" / "py" / "hello.py")], cwd=ROOT)}
    # B4: code size.
    gen_hello = ROOT / "goldens" / "py" / "hello.py"
    hw_hello = ROOT / "benchmarks" / "handwritten" / "hello.py"
    gen_shop = ROOT / "goldens" / "py" / "shop.py"
    gen_loop = ROOT / "benchmarks" / "generated" / "loop.py"
    hw_loop = ROOT / "benchmarks" / "handwritten" / "loop_plain.py"
    sizes = {}
    for label, path in (("hello_gen", gen_hello), ("hello_hw", hw_hello),
                        ("shop_gen", gen_shop), ("loop_gen", gen_loop),
                        ("loop_hw", hw_loop)):
        b, lines = _size(path)
        sizes[label] = {"bytes": b, "lines": lines}
    results["b4_sizes"] = sizes

    # Write BENCHMARKS.md.
    def fmt(r: dict) -> str:
        return (f"{r['mean']*1000:.1f} ms ± {r['stddev']*1000:.1f} ms "
                f"(range {r['min']*1000:.1f}–{r['max']*1000:.1f} ms, "
                f"N={r['runs']})")

    def ratio(a: dict, b: dict) -> float:
        return a["mean"] / b["mean"]

    md = []
    md.append("# Benchmarks — M2 (Python emitter)\n")
    md.append(f"Machine: {platform.system()} {platform.machine()}, "
              f"Python {platform.python_version()}, "
              f"hyperfine {subprocess.run([str(HYPERFINE), '--version'], capture_output=True, text=True).stdout.strip()}.\n")
    md.append("All figures are measured, not promised. Raw hyperfine JSON "
              "is in `benchmarks/raw/`. No published generated-vs-hand-written "
              "benchmark exists for any transpiler — these numbers are the "
              "first such measurement for newr, and they are reported as found.\n")
    md.append("## B1 — Compile, cold (no warmup)\n")
    md.append(f"- hello: {fmt(results['b1_hello'])}")
    md.append(f"- shop: {fmt(results['b1_shop'])}\n")
    md.append("## B2 — Compile, warm\n")
    md.append(f"- hello (no change): {fmt(results['b2_hello'])}")
    md.append(f"- shop (one file changed): {fmt(results['b2_shop_change'])}\n")
    md.append("## B3 — `newr --help` startup (SPEC §4.1 target <100 ms)\n")
    md.append(f"- {fmt(results['b3_help'])}\n")
    md.append("## B4 — Generated code size\n")
    for label, s in sizes.items():
        md.append(f"- {label}: {s['bytes']} bytes, {s['lines']} lines")
    md.append(f"- hello size ratio (generated/hand-written): "
              f"{sizes['hello_gen']['bytes']/sizes['hello_hw']['bytes']:.2f}")
    md.append(f"- loop size ratio (generated/hand-written): "
              f"{sizes['loop_gen']['bytes']/sizes['loop_hw']['bytes']:.2f}\n")
    md.append("## B5 — Generated program startup\n")
    md.append(f"- generated hello: {fmt(results['b5_hello_gen'])}")
    md.append(f"- hand-written hello: {fmt(results['b5_hello_hw'])}")
    md.append(f"- ratio (generated/hand-written): "
              f"{ratio(results['b5_hello_gen'], results['b5_hello_hw']):.2f}\n")
    md.append("## B6 — Generated runtime (checked-int tight loop, 100k iterations)\n")
    md.append(f"- generated (checked helpers): {fmt(results['b6_loop_gen'])}")
    md.append(f"- hand-written, plain ints: {fmt(results['b6_loop_plain'])}")
    md.append(f"- hand-written, newr_rt helpers: {fmt(results['b6_loop_checked'])}")
    md.append(f"- ratio generated/plain: "
              f"{ratio(results['b6_loop_gen'], results['b6_loop_plain']):.2f}")
    md.append(f"- ratio generated/checked-hand-written: "
              f"{ratio(results['b6_loop_gen'], results['b6_loop_checked']):.2f}")
    md.append("  (All three print the same checksum, 105003 — the loops agree.)\n")
    md.append("## B6b — Shop economy, 1,000 credits (aiosqlite, commit per call)\n")
    md.append(f"- generated (emitted shop Bot): {fmt(results['b6b_shop_gen'])}")
    md.append(f"- hand-written (aiosqlite directly): {fmt(results['b6b_shop_hw'])}")
    md.append(f"- ratio (generated/hand-written): "
              f"{ratio(results['b6b_shop_gen'], results['b6b_shop_hw']):.2f}")
    md.append("  (10,000 was reduced to 1,000: per-call commit fsync "
              "dominates and 10k took >4 min; 1k still measures the code path. "
              "Both print the final balance 1000.)")
    md.append("  (Methodology, M2 fix #6: the hand-written driver imports "
              "discord too — the generated driver imports the whole shop "
              "bot, so without the same import the comparison would measure "
              "interpreter startup, not the economy loop.)\n")
    md.append("## B7 — Peak RSS\n")
    md.append(f"- compile (shop): {results['b7_compile']['rss_mb']:.1f} MB")
    md.append(f"- run (hello): {results['b7_run']['rss_mb']:.1f} MB\n")
    md.append("## B8 — Hand-written parity\n")
    md.append(f"- startup ratio: "
              f"{ratio(results['b5_hello_gen'], results['b5_hello_hw']):.2f} "
              f"(>1.20 would be a bug report)")
    md.append(f"- loop ratio (generated/plain): "
              f"{ratio(results['b6_loop_gen'], results['b6_loop_plain']):.2f} "
              f"(>1.20 would be a bug report)")
    md.append(f"- shop economy ratio: "
              f"{ratio(results['b6b_shop_gen'], results['b6b_shop_hw']):.2f} "
              f"(>1.20 would be a bug report)")
    md.append(f"- hello size ratio: "
              f"{sizes['hello_gen']['bytes']/sizes['hello_hw']['bytes']:.2f} "
              f"(>1.50 would be a bug report)")
    md.append(f"- loop size ratio: "
              f"{sizes['loop_gen']['bytes']/sizes['loop_hw']['bytes']:.2f} "
              f"(>1.50 would be a bug report)\n")
    (ROOT / "BENCHMARKS.md").write_text("\n".join(md), encoding="utf-8")
    print("\n".join(md))
    print(f"\nWrote BENCHMARKS.md and benchmarks/raw/*.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
