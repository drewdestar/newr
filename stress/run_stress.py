"""Stress harness for the newr compiler.

Runs `newr check` (and --json variants) against the corpus, depth
sweeps, seeded fuzz, scale cases and CLI edge cases; classifies every
outcome as CLEAN / CRASH / TIMEOUT / BAD_JSON / WRONG_EXIT / MISMATCH.

Usage:
  python3 run_stress.py --gate                  # fix-gate only (exit 0/1)
  python3 run_stress.py --mode corpus|depth|fuzz|scale|cli|endurance|all
  python3 run_stress.py --mode fuzz --fuzz-n 3000 --seed 7
  python3 run_stress.py --mode endurance --endurance-min 30
  python3 run_stress.py --mode all --fuzz-n 3000 --endurance-min 30

Results go to stress/results-<mode>-<timestamp>.json.
Crashing inputs are copied to stress/crashes/.
"""
import argparse
import json
import os
import random
import re
import shutil
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
NEWR = os.path.join(ROOT, "newr")
CORPUS_DIR = os.path.join(HERE, "corpus")
CRASH_DIR = os.path.join(HERE, "crashes")
RESULTS_DIR = os.path.join(HERE, "results")
EXAMPLES = ["hello.newr", "hello_bot.newr", "shop.newr", "showcase.newr"]

TRACEBACK_MARKERS = ("Traceback (most recent call last)", "RecursionError",
                     "MemoryError", "Segmentation fault", "core dumped")


def peak_rss_kb(pid, stop_event, out):
    """Poll /proc/<pid>/status VmHWM while the child runs."""
    peak = 0
    while not stop_event.is_set():
        try:
            with open(f"/proc/{pid}/status") as fh:
                for line in fh:
                    if line.startswith("VmHWM:"):
                        peak = max(peak, int(line.split()[1]))
                        break
        except (FileNotFoundError, ProcessLookupError, ValueError):
            break
        time.sleep(0.02)
    out["peak_kb"] = peak


def run_check(path, extra_args=None, timeout=10, cwd=None):
    """Run `python3 newr check <path>`; return a result dict."""
    cmd = [sys.executable, NEWR, "check", path] + (extra_args or [])
    t0 = time.monotonic()
    stop = threading.Event()
    peak = {"peak_kb": 0}
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, cwd=cwd or ROOT)
    watcher = threading.Thread(target=peak_rss_kb,
                               args=(proc.pid, stop, peak), daemon=True)
    watcher.start()
    try:
        out_b, err_b = proc.communicate(timeout=timeout)
        timed_out = False
    except subprocess.TimeoutExpired:
        proc.kill()
        out_b, err_b = proc.communicate()
        timed_out = True
    finally:
        stop.set()
        watcher.join(timeout=1)
    wall = time.monotonic() - t0
    return {
        "cmd": cmd,
        "exit": proc.returncode,
        "stdout": out_b.decode("utf-8", "replace"),
        "stderr": err_b.decode("utf-8", "replace"),
        "timed_out": timed_out,
        "wall_s": round(wall, 3),
        "peak_kb": peak["peak_kb"],
    }


def run_cli(args, timeout=10):
    """Run `python3 newr <args...>` (no `check` implied)."""
    cmd = [sys.executable, NEWR] + args
    t0 = time.monotonic()
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=timeout,
                              cwd=ROOT)
        timed_out = False
    except subprocess.TimeoutExpired:
        return {"cmd": cmd, "exit": None, "stdout": "", "stderr": "",
                "timed_out": True, "wall_s": round(time.monotonic() - t0, 3),
                "peak_kb": 0}
    return {
        "cmd": cmd,
        "exit": proc.returncode,
        "stdout": proc.stdout.decode("utf-8", "replace"),
        "stderr": proc.stderr.decode("utf-8", "replace"),
        "timed_out": timed_out,
        "wall_s": round(time.monotonic() - t0, 3),
        "peak_kb": 0,
    }


def classify(result, want_json=False, extra_ok_exits=()):
    """Classify a run_check/run_cli result. extra_ok_exits is for CLI
    invocations, where exit 1 is the documented usage-error code."""
    if result["timed_out"]:
        return "TIMEOUT"
    blob = result["stdout"] + result["stderr"]
    if any(m in blob for m in TRACEBACK_MARKERS):
        return "CRASH"
    if want_json:
        try:
            obj = json.loads(result["stdout"])
            if not isinstance(obj, dict):
                return "BAD_JSON"
        except (json.JSONDecodeError, ValueError):
            return "BAD_JSON"
    if result["exit"] not in (0, 2, 4) + tuple(extra_ok_exits):
        return "WRONG_EXIT"
    return "CLEAN"


def save_crash(name, data_bytes, result, note=""):
    os.makedirs(CRASH_DIR, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", name)
    path = os.path.join(CRASH_DIR, safe + ".newr")
    if isinstance(data_bytes, str):
        data_bytes = data_bytes.encode("utf-8", "replace")
    with open(path, "wb") as fh:
        fh.write(data_bytes)
    with open(path + ".report.json", "w") as fh:
        json.dump({"name": name, "note": note,
                   "exit": result["exit"],
                   "stdout": result["stdout"][-2000:],
                   "stderr": result["stderr"][-4000:],
                   "wall_s": result["wall_s"],
                   "peak_kb": result["peak_kb"]}, fh, indent=2)
    return path


# ------------------------------------------------------------------ gate
HEADLINE_CHECKS = [
    ("nullable-assign", "let x: int? = 5\nprint x\n", True),
    ("parens-500", "print " + "("*500 + "1" + ")"*500 + "\n", None),
    ("ensures-example",
     "fn withdraw(balance: int, amount: int) -> int:\n"
     "  requires amount > 0\n"
     "  requires amount <= balance\n"
     "  ensures result >= 0\n"
     "  return balance - amount\n", True),
]


def gate_check(verbose=True):
    """Suite green + 3 headline checks. Returns (ok, details)."""
    details = {}
    proc = subprocess.run(
        [sys.executable, "-m", "unittest", "discover", "-s", "tests"],
        capture_output=True, text=True, cwd=ROOT, timeout=300)
    tail = (proc.stderr.strip().splitlines() or [""]) [-1]
    m = re.search(r"Ran (\d+) tests", proc.stderr)
    details["suite_tests"] = int(m.group(1)) if m else -1
    details["suite_tail"] = tail
    details["suite_ok"] = proc.returncode == 0
    if verbose:
        print(f"[gate] suite: {details['suite_tests']} tests -> {tail}")
    ok = details["suite_ok"]
    tmp = "/tmp/stress-gate"
    os.makedirs(tmp, exist_ok=True)
    for name, src, must_pass in HEADLINE_CHECKS:
        path = os.path.join(tmp, name + ".newr")
        with open(path, "w") as fh:
            fh.write(src)
        r = run_check(path, timeout=30)
        cls = classify(r)
        if must_pass:
            passed = (cls == "CLEAN" and r["exit"] == 0)
        else:  # parens-500: must NOT crash; clean diagnostic (exit 4) is fine
            passed = cls == "CLEAN"
        details[name] = {"class": cls, "exit": r["exit"], "passed": passed,
                         "stderr_tail": r["stderr"].strip().splitlines()[-1:]
                         if r["stderr"].strip() else []}
        if verbose:
            print(f"[gate] {name}: class={cls} exit={r['exit']} "
                  f"passed={passed}")
        ok = ok and passed
    return ok, details


def wait_for_gate(max_wait_s=45 * 60, interval_s=60):
    """Poll the gate until it passes twice in a row (guards mid-edit flips)."""
    deadline = time.monotonic() + max_wait_s
    consecutive = 0
    attempt = 0
    while time.monotonic() < deadline:
        attempt += 1
        print(f"[wait] gate attempt {attempt} ...")
        ok, details = gate_check(verbose=True)
        if ok:
            consecutive += 1
            print(f"[wait] gate GREEN ({consecutive}/2)")
            if consecutive >= 2:
                return True, details
        else:
            consecutive = 0
            print("[wait] gate RED — fixes not in yet, sleeping "
                  f"{interval_s}s")
        if time.monotonic() + interval_s > deadline:
            break
        time.sleep(interval_s)
    return False, details


# ---------------------------------------------------------------- corpus
def load_manifest():
    with open(os.path.join(CORPUS_DIR, "manifest.json")) as fh:
        return json.load(fh)


def sweep_corpus(timeout=10, want_json=False):
    manifest = load_manifest()
    results = []
    crashes = []
    mismatches = []
    for entry in manifest:
        path = os.path.join(CORPUS_DIR, entry["file"])
        extra = ["--json"] if want_json else None
        r = run_check(path, extra_args=extra, timeout=timeout)
        cls = classify(r, want_json=want_json)
        expected = entry["expected"]
        actual_ok = (r["exit"] == 0 and cls == "CLEAN")
        mismatch = None
        if expected == "valid" and not actual_ok:
            mismatch = f"expected valid, got {cls} exit={r['exit']}"
        elif expected == "error" and (actual_ok or r["exit"] == 0):
            mismatch = f"expected error, got clean exit 0"
        rec = {"name": entry["name"], "category": entry["category"],
               "expected": expected, "class": cls, "exit": r["exit"],
               "wall_s": r["wall_s"], "peak_kb": r["peak_kb"],
               "mismatch": mismatch}
        results.append(rec)
        if cls in ("CRASH", "TIMEOUT", "BAD_JSON", "WRONG_EXIT"):
            crashes.append(rec)
            with open(path, "rb") as fh:
                save_crash("corpus-" + entry["name"], fh.read(), r, mismatch or "")
        if mismatch:
            mismatches.append(rec)
    return results, crashes, mismatches

# ----------------------------------------------------------------- depth
DEPTH_GRID = [100, 150, 200, 250, 300, 400, 500, 750, 1000, 1500, 2000,
              3000, 5000]

# import builders lazily (gen_corpus lives next to this file)
sys.path.insert(0, HERE)
from gen_corpus import DEPTH_BUILDERS  # noqa: E402


def _depth_probe(builder, n, timeout=30):
    src = builder(n)
    tmp = "/tmp/stress-depth"
    os.makedirs(tmp, exist_ok=True)
    path = os.path.join(tmp, "probe.newr")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(src)
    r = run_check(path, timeout=timeout)
    return classify(r), r


def depth_sweep(timeout=30, only=None):
    """Grid sweep + binary search of the survive->break boundary per construct.

    'survive' = CLEAN (ok or clean diagnostic). 'break' = CRASH/TIMEOUT/
    BAD_JSON/WRONG_EXIT. A clean diagnostic at high depth is survival,
    not a break — the depth guard doing its job.
    """
    out = {}
    builders = {k: v for k, v in DEPTH_BUILDERS.items()
                if only is None or k in only}
    for name, builder in builders.items():
        grid = []
        for n in DEPTH_GRID:
            cls, r = _depth_probe(builder, n, timeout=timeout)
            grid.append({"n": n, "class": cls, "exit": r["exit"],
                         "wall_s": r["wall_s"]})
            print(f"[depth] {name} n={n}: {cls} exit={r['exit']} "
                  f"{r['wall_s']}s")
        # find first break in the grid (if any) and binary-search boundary
        boundary = None
        first_break = next((g for g in grid if g["class"] != "CLEAN"), None)
        if first_break is not None:
            idx = grid.index(first_break)
            lo = grid[idx - 1]["n"] if idx > 0 else 1
            hi = first_break["n"]
            # invariant: lo survives, hi breaks — binary search last survivor
            while hi - lo > 1:
                mid = (lo + hi) // 2
                cls, r = _depth_probe(builder, mid, timeout=timeout)
                print(f"[depth] {name} bisect n={mid}: {cls}")
                if cls == "CLEAN":
                    lo = mid
                else:
                    hi = mid
            boundary = {"last_survive": lo, "first_break": hi}
            cls, r = _depth_probe(builder, hi, timeout=timeout)
            src = builder(hi)
            save_crash(f"depth-{name}-{hi}", src, r,
                       f"first breaking depth for {name}")
        out[name] = {"grid": grid, "boundary": boundary}
    return out


# ------------------------------------------------------------------ fuzz
TOKENS = [
    "let", "const", "fn", "if", "elif", "else", "for", "while", "return",
    "print", "log", "try", "catch", "finally", "throw", "match", "import",
    "bot", "command", "event", "struct", "enum", "requires", "ensures",
    "true", "false", "none", "and", "or", "not", "in", "is",
    "x", "y", "foo", "bar", "n", "a1", "café", "x_2",
    "0", "1", "42", "3.14", "0xFF", "0b101", "1_000",
    '"hi"', '"{x}"', '"a\\nb"', '"🎉"',
    "+", "-", "*", "/", "%", "**", "==", "!=", "<", ">", "<=", ">=",
    "?", "?.", "??", "|>", "|", ":", ",", ".", "=", "(", ")", "[", "]",
    "{", "}", "# comment", "\n",
]

IDENT_POOL = ["a", "b", "c", "x", "y", "n", "count", "total", "name",
              "value", "item", "result", "café", "data", "tmp"]


def fuzz_program(rng):
    """Generate a random program: mostly plausible, sometimes broken."""
    kind = rng.random()
    if kind < 0.45:
        return fuzz_plausible(rng)
    if kind < 0.75:
        return fuzz_mutation(rng)
    return fuzz_soup(rng)


def fuzz_plausible(rng):
    lines = []
    nvars = rng.randint(0, 6)
    vars_ = []
    for i in range(nvars):
        v = rng.choice(IDENT_POOL) + str(i)
        vars_.append(v)
        lines.append(f"let {v} = {rng.randint(-100, 100)}")
    for _ in range(rng.randint(1, 8)):
        r = rng.random()
        if r < 0.25 and vars_:
            v = rng.choice(vars_)
            lines.append(f"print {v} + {rng.randint(0, 9)}")
        elif r < 0.4:
            lines.append(f"if {rng.choice(vars_ or ['1'])} > 0:")
            lines.append(f"  print \"pos\"")
            if rng.random() < 0.5:
                lines.append("else:")
                lines.append(f"  print \"neg\"")
        elif r < 0.55:
            lines.append(f"for i in range({rng.randint(1, 5)}):")
            lines.append("  print i")
        elif r < 0.7:
            lines.append(f"let s = \"v={rng.randint(0, 99)}\"")
            lines.append("print s")
        elif r < 0.85:
            lines.append(f"fn f{rng.randint(0, 999)}(a: int) -> int:")
            lines.append("  return a * 2")
        else:
            lines.append(f"print {rng.randint(0, 100)} ?? 0")
    return "\n".join(lines) + "\n"


def fuzz_mutation(rng):
    base = rng.choice([open(os.path.join(ROOT, "examples", f)).read()
                       for f in EXAMPLES])
    ops = rng.randint(1, 3)
    s = base
    for _ in range(ops):
        op = rng.random()
        if not s:
            break
        if op < 0.3:  # delete a random chunk
            i = rng.randrange(len(s))
            j = min(len(s), i + rng.randint(1, 20))
            s = s[:i] + s[j:]
        elif op < 0.55:  # duplicate a random line
            ls = s.split("\n")
            i = rng.randrange(len(ls))
            ls.insert(i, ls[i])
            s = "\n".join(ls)
        elif op < 0.8:  # insert a random token somewhere
            i = rng.randrange(len(s) + 1)
            s = s[:i] + rng.choice(TOKENS) + s[i:]
        else:  # replace a char with random unicode
            i = rng.randrange(len(s))
            s = s[:i] + rng.choice(["🎉", "é", "\t", "\r", "{", "}", '"',
                                    "\\", " ", " ", " "]) + s[i + 1:]
    return s


def fuzz_soup(rng):
    n = rng.randint(5, 120)
    parts = []
    for _ in range(n):
        t = rng.choice(TOKENS)
        parts.append(t)
        if rng.random() < 0.3:
            parts.append(" ")
        if rng.random() < 0.2:
            parts.append("\n")
    return "".join(parts) + "\n"


def run_fuzz(n=3000, seed=20261106, timeout=10):
    rng = random.Random(seed)
    tmp = "/tmp/stress-fuzz"
    os.makedirs(tmp, exist_ok=True)
    path = os.path.join(tmp, "case.newr")
    crashes = []
    counts = {"CLEAN": 0, "CRASH": 0, "TIMEOUT": 0, "BAD_JSON": 0,
              "WRONG_EXIT": 0}
    exits = {}
    for i in range(n):
        src = fuzz_program(rng)
        with open(path, "w", encoding="utf-8", errors="replace") as fh:
            fh.write(src)
        r = run_check(path, timeout=timeout)
        cls = classify(r)
        counts[cls] = counts.get(cls, 0) + 1
        exits[r["exit"]] = exits.get(r["exit"], 0) + 1
        if cls != "CLEAN":
            rec = {"i": i, "class": cls, "exit": r["exit"],
                   "wall_s": r["wall_s"],
                   "stderr_tail": r["stderr"][-500:]}
            crashes.append(rec)
            save_crash(f"fuzz-{seed}-{i}", src, r, f"fuzz case {i} {cls}")
        if (i + 1) % 500 == 0:
            print(f"[fuzz] {i + 1}/{n} done, crashes so far: "
                  f"{len(crashes)}")
    return {"n": n, "seed": seed, "counts": counts, "exits": exits,
            "bad_cases": crashes}


# ----------------------------------------------------------------- scale
def scale_cases():
    sys.path.insert(0, HERE)
    from gen_corpus import (scale_lines, scale_vars, scale_string,
                            scale_list, scale_fns, scale_long_ident,
                            scale_long_line, scale_bot_commands)
    cases = [
        ("lines-10k", scale_lines(10000)),
        ("vars-10k", scale_vars(10000)),
        ("string-1mb", scale_string(1_000_000)),
        ("list-100k", scale_list(100000)),
        ("fns-1000", scale_fns(1000)),
        ("long-ident-10k", scale_long_ident(10000)),
        ("long-line-100k", scale_long_line(100000)),
        ("bot-500-commands", scale_bot_commands(500)),
    ]
    # import graph: 100-file chain
    impdir = "/tmp/stress-scale-imports"
    os.makedirs(impdir, exist_ok=True)
    for i in range(100):
        nxt = f'import "imp{i + 1}.newr"\n' if i < 99 else ""
        with open(os.path.join(impdir, f"imp{i}.newr"), "w") as fh:
            fh.write(nxt + f"pub let v{i} = {i}\n")
    with open(os.path.join(impdir, "main.newr"), "w") as fh:
        fh.write('import "imp0.newr"\nimport "imp99.newr"\nprint imp99.v99\n')
    cases.append(("import-chain-100", None))  # special: file on disk
    return cases, impdir


def run_scale(timeout=120):
    cases, impdir = scale_cases()
    tmp = "/tmp/stress-scale"
    os.makedirs(tmp, exist_ok=True)
    out = []
    for name, src in cases:
        if src is None:
            path = os.path.join(impdir, "main.newr")
            size = os.path.getsize(path)
        else:
            path = os.path.join(tmp, name + ".newr")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(src)
            size = len(src.encode("utf-8"))
        r = run_check(path, timeout=timeout)
        cls = classify(r)
        rec = {"name": name, "bytes": size, "class": cls, "exit": r["exit"],
               "wall_s": r["wall_s"], "peak_kb": r["peak_kb"],
               "stderr_tail": r["stderr"][-300:]}
        out.append(rec)
        print(f"[scale] {name}: {size} bytes -> {cls} exit={r['exit']} "
              f"{r['wall_s']}s peak={r['peak_kb']}KB")
        if cls != "CLEAN":
            with open(path, "rb") as fh:
                save_crash("scale-" + name, fh.read(), r)
    return out


# ------------------------------------------------------------------- cli
def run_cli_robustness(timeout=15):
    cases = []
    tmp = "/tmp/stress-cli"
    os.makedirs(tmp, exist_ok=True)
    # byte-level inputs through `check`
    blob_cases = {
        "nonutf8": b'print "hi"\n\xff\xfe\n',
        "empty": b"",
        "lone-cr": b'print "a"\rprint "b"\n',
        "crlf-err": b'print "ok"\r\nprint nosuchvar\r\n',
        "bom": b'\xef\xbb\xbfprint "bom"\n',
        "cesu8": b'print "\xed\xa0\x80"\n',
        "nul": b'print "a\x00b"\n',
    }
    for name, data in blob_cases.items():
        path = os.path.join(tmp, name + ".newr")
        with open(path, "wb") as fh:
            fh.write(data)
        for args, label in ((None, "plain"), (["--json"], "json")):
            r = run_check(path, extra_args=args, timeout=timeout)
            cls = classify(r, want_json=(args is not None))
            cases.append({"name": f"cli-{name}-{label}", "class": cls,
                          "exit": r["exit"], "wall_s": r["wall_s"],
                          "stdout_head": r["stdout"][:200],
                          "stderr_tail": r["stderr"][-300:]})
            print(f"[cli] {name}-{label}: {cls} exit={r['exit']}")
    # --json on every failure mode: take known-error corpus files
    for entry in load_manifest():
        if entry["expected"] != "error":
            continue
        path = os.path.join(CORPUS_DIR, entry["file"])
        r = run_check(path, extra_args=["--json"], timeout=timeout)
        cls = classify(r, want_json=True)
        if cls != "CLEAN":
            cases.append({"name": f"json-{entry['name']}", "class": cls,
                          "exit": r["exit"], "wall_s": r["wall_s"],
                          "stdout_head": r["stdout"][:200],
                          "stderr_tail": r["stderr"][-300:]})
    # bad invocations
    bad_invocations = [
        (["--nope"], "bad-flag"),
        (["check"], "check-no-file"),
        (["check", "/nonexistent/x.newr"], "check-missing-file"),
        (["build", "examples/hello.newr"], "build-no-check"),
        (["explain", "E9999"], "explain-unknown"),
        (["explain", "E_BAD_ESCAPE"], "explain-lexer-code"),
        (["explain", "E1001"], "explain-e1001"),
        (["frobnicate"], "unknown-command"),
        ([], "bare-newr"),
        (["check", "examples/hello.newr", "--fast"], "fast-ok"),
        (["--json", "check", "examples/hello.newr"], "json-ok"),
        (["--json", "check", "examples/showcase.newr"], "json-showcase"),
    ]
    for args, label in bad_invocations:
        r = run_cli(args, timeout=timeout)
        cls = classify(r, want_json=(args[:1] == ["--json"]),
                       extra_ok_exits=(1,))
        cases.append({"name": f"inv-{label}", "class": cls,
                      "exit": r["exit"], "wall_s": r["wall_s"],
                      "stdout_head": r["stdout"][:200],
                      "stderr_tail": r["stderr"][-300:]})
        print(f"[cli] inv-{label}: {cls} exit={r['exit']}")
    return cases


# -------------------------------------------------------------- endurance
def run_endurance(minutes=30, timeout=15):
    """Repeat suite + examples + rotating corpus slice; watch for flakes
    and slowdowns. Returns per-iteration records."""
    manifest = load_manifest()
    files = [os.path.join(CORPUS_DIR, e["file"]) for e in manifest]
    deadline = time.monotonic() + minutes * 60
    iterations = []
    it = 0
    while time.monotonic() < deadline:
        it += 1
        t0 = time.monotonic()
        proc = subprocess.run(
            [sys.executable, "-m", "unittest", "discover", "-s", "tests"],
            capture_output=True, text=True, cwd=ROOT, timeout=300)
        suite_ok = proc.returncode == 0
        suite_tail = (proc.stderr.strip().splitlines() or [""]) [-1]
        # examples
        ex = {}
        for f in EXAMPLES:
            r = run_check(os.path.join(ROOT, "examples", f), timeout=timeout)
            ex[f] = {"class": classify(r), "exit": r["exit"]}
        # rotating corpus slice: 1/4 of the corpus per iteration
        slice_files = files[(it - 1) % 4::4]
        slice_bad = []
        for path in slice_files:
            r = run_check(path, timeout=timeout)
            if classify(r) != "CLEAN":
                slice_bad.append({"file": os.path.basename(path),
                                  "class": classify(r), "exit": r["exit"]})
        rec = {"iteration": it, "wall_s": round(time.monotonic() - t0, 1),
               "suite_ok": suite_ok, "suite_tail": suite_tail,
               "examples": ex, "slice_size": len(slice_files),
               "slice_bad": slice_bad}
        iterations.append(rec)
        print(f"[endurance] iter {it}: {rec['wall_s']}s suite_ok={suite_ok} "
              f"slice_bad={len(slice_bad)}")
        if time.monotonic() >= deadline:
            break
    return iterations

# ------------------------------------------------------------------ main
def save_results(mode, data):
    os.makedirs(RESULTS_DIR, exist_ok=True)
    ts = time.strftime("%Y%m%d-%H%M%S")
    path = os.path.join(RESULTS_DIR, f"{mode}-{ts}.json")
    with open(path, "w") as fh:
        json.dump(data, fh, indent=2)
    print(f"[results] wrote {path}")
    return path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--gate", action="store_true")
    ap.add_argument("--wait-gate", action="store_true")
    ap.add_argument("--mode", default="all",
                    choices=["corpus", "depth", "fuzz", "scale", "cli",
                             "endurance", "all"])
    ap.add_argument("--fuzz-n", type=int, default=3000)
    ap.add_argument("--seed", type=int, default=20261106)
    ap.add_argument("--endurance-min", type=float, default=30)
    ap.add_argument("--timeout", type=int, default=10)
    ap.add_argument("--constructs", default=None,
                    help="comma list for --mode depth, e.g. dict,interp")
    args = ap.parse_args()

    if args.gate:
        ok, details = gate_check()
        print(json.dumps(details, indent=2))
        sys.exit(0 if ok else 1)
    if args.wait_gate:
        ok, details = wait_for_gate()
        print(json.dumps(details, indent=2))
        sys.exit(0 if ok else 1)

    modes = ["corpus", "depth", "fuzz", "scale", "cli", "endurance"] \
        if args.mode == "all" else [args.mode]
    all_results = {}
    for mode in modes:
        print(f"===== mode: {mode} =====")
        if mode == "corpus":
            results, crashes, mismatches = sweep_corpus(
                timeout=args.timeout)
            data = {"results": results, "crashes": crashes,
                    "mismatches": mismatches}
            print(f"[corpus] {len(results)} cases, "
                  f"{len(crashes)} crashes/bad, "
                  f"{len(mismatches)} expected-mismatches")
        elif mode == "depth":
            only = (args.constructs.split(",") if args.constructs else None)
            data = depth_sweep(only=only)
            nbreak = sum(1 for v in data.values()
                         if v["boundary"] is not None)
            print(f"[depth] {len(data)} constructs, "
                  f"{nbreak} with a breaking depth")
        elif mode == "fuzz":
            data = run_fuzz(n=args.fuzz_n, seed=args.seed,
                            timeout=args.timeout)
            print(f"[fuzz] counts={data['counts']}")
        elif mode == "scale":
            data = run_scale()
        elif mode == "cli":
            data = run_cli_robustness()
            bad = [c for c in data if c["class"] != "CLEAN"]
            print(f"[cli] {len(data)} cases, {len(bad)} non-clean")
        elif mode == "endurance":
            data = run_endurance(minutes=args.endurance_min)
            print(f"[endurance] {len(data)} iterations in "
                  f"{args.endurance_min} min")
        all_results[mode] = data
        save_results(mode, data)
    save_results("all", all_results)
    print("ALL MODES DONE")


if __name__ == "__main__":
    main()
