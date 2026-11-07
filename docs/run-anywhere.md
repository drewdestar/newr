# M7: Run Anywhere

Status: design + feasibility only. No compiler changes were made for this
doc. No file besides this one was touched.

Date: 7 November 2026. Author: design agent (subagent of chie).

## 0. The requirement, in one paragraph

The user (7 Nov 2026, 16:01 WIB): newr must run on ALL devices and
OSes, modern AND old/obsolete ones, so it is never impossible to run the
language. You only need a device to run it, plus internet when
downloading the language. Download once, run offline after that.

## 1. Verdict up front

- **Route ranking** (by the user's rule, "only need a device plus
  internet at download"):
  1. Browser playground (Pyodide/WASM): zero install, any device with
     a browser, offline after first load via cache. PROVEN to run the
     real compiler in this session (see section 5).
  2. Standalone one-file binary (PyInstaller): one file, no Python
     needed. PROVEN on Linux x86_64 in this session (8.6 MB, check and
     build work).
  3. PyPI + zipapp (today's route): smallest download (2.4 MB) but
     needs a CPython 3.10+ install first, which is the biggest friction
     on old and locked-down devices.
- **Python floor: KEEP 3.10.** Dropping to 3.9 or 3.8 is technically
  almost free for the compiler source (see section 4), but it buys
  almost nothing: the machines that cannot install Python 3.10 also
  cannot install Python 3.9/3.8 from python.org anymore, and they are
  served better by the binary route (no Python at all) and the browser
  route (no install at all).
- **Old devices are served, with a hard limit.** Any device with a
  Python 3.10+, or a WASM-capable browser, or an OS the binary was
  built for, can run newr. Devices with none of those three cannot,
  and no distribution trick fixes that (see section 2).

## 2. Support tiers

Honest tiers, not marketing. "Old device" means a device that is still
usable and still gets security updates for its browser or OS, or that
can at least run a Python 3.10+ or a modern browser. It does NOT mean
every device ever made.

### Tier 1: must verify via CI (blocking)

| Platform | Versions | Arch | Python |
| --- | --- | --- | --- |
| Linux | Ubuntu 22.04, 24.04 LTS; Debian 12 | x86_64, ARM64 | 3.10, 3.11, 3.12, 3.13 |
| macOS | 13 Ventura, 14 Sonoma, 15 Sequoia | Apple Silicon, Intel | 3.10, 3.11, 3.12, 3.13 |
| Windows | 10 (21H2+), 11, Server 2022 | x86_64 | 3.10, 3.11, 3.12, 3.13 |

Proof for Tier 1: the existing CI matrix goes green once (it has never
run), then stays green. Record the exact runner OS versions from the
runner logs on the first green run.

### Tier 2: best effort (verified by hand or by a dedicated job, not blocking)

| Platform | Notes |
| --- | --- |
| Ubuntu 20.04 LTS | System Python is 3.8. Needs deadsnakes PPA, or the binary route, or the browser route. |
| Debian 11 | System Python is 3.9. Same as above. |
| macOS 10.13 to 12 | CPython 3.12/3.13 installers need 10.13+. Older macOS needs the binary or browser route. |
| Windows 8.1 | Last CPython with an installer is 3.12 (UNVERIFIED, check python.org at build time). No 3.13 installer. |
| Windows / Linux 32-bit x86 | CPython ships 32-bit Windows installers through 3.12 (UNVERIFIED, check at build time). 3.13 dropped them, as far as this doc could establish without a live check. |
| ARMv7 (32-bit ARM, e.g. Raspberry Pi 2/3 in 32-bit mode) | No GitHub runner. Verify on real hardware or in QEMU. |
| Android 7+ via Termux | `pkg install python`, then pip install or the .pyz. No CI runner; proof is a recorded terminal session. |
| ChromeOS with Linux container | Same as Linux Tier 1 once the container is on. |
| 512 MB RAM devices (old laptops, Raspberry Pi Zero 2 W) | The compiler itself needs about 18 MB max RSS (measured, section 3). The binary needs about 19 MB. The browser route needs much more (see section 5). |

### Tier 3: browser only (no native install possible)

| Device | Why only the browser |
| --- | --- |
| iOS / iPadOS | No sideloaded CLI, no Python install. Safari runs WASM. |
| ChromeOS without Linux | No terminal, no Python. Browser only. |
| Windows 7 | No CPython 3.10+ installer exists for it. A current Chrome/Firefox still runs the playground (UNVERIFIED on real Win7 hardware). |
| macOS 10.12 and older | No current CPython installer. Browser only. |
| 32-bit ARMv6, smart TVs, locked-down school PCs | No Python, no native binary path. Browser only. |

### What "old device" can NOT mean

Plainly: newr cannot run on a device that has no Python 3.10+, no
WASM-capable browser, and no OS the binary was built for. Concrete
examples: Windows XP and older, macOS 10.8 and older with no modern
browser, feature phones, devices with under about 256 MB of free RAM
for the browser route, and any device with no internet even once (the
first download needs internet; there is no way to get the language onto
a device without any transfer at all). iOS can only ever be Tier 3:
Apple's rules block a downloadable CLI interpreter, so the playground
is the whole story there.

## 3. Measured facts (this session, Linux x86_64, Python 3.12.3)

Everything in this section was measured on this VM on 7 Nov 2026.
Nothing here is an estimate except where it says so.

- Compiler source: `compiler/` is 4.8 MB on disk, 46,620 lines of
  Python. All `.py` in the repo: 2.9 MB, 62,939 lines. Imports are
  stdlib only: `dataclasses`, `datetime`, `decimal`, `pathlib`,
  `types`, `difflib`, `json`, `keyword`, `math`, `os`, `re`, `sys`,
  `unicodedata`, plus internal modules.
- Clean zipapp (compiler + launcher, no `__pycache__`):
  2,430,019 bytes (about 2.3 MB). Verified working:
  `python3 newr-clean.pyz check examples/hello.newr` prints ok.
  A full-tree zipapp (tests, benchmarks, goldens included) is
  46,540,502 bytes and is NOT the distribution form.
- CPython 3.12 on disk here: `/usr/lib/python3.12` is 55 MB.
  That is disk, not RAM.
- Compiler memory: `newr check` on hello, max RSS 15.2 MB;
  on shop, 15.4 MB; `newr build --target py` on shop, 17.5 MB.
  The PyInstaller binary checks shop at 18.5 MB max RSS.
  A 512 MB device has room for the compiler with about 490 MB to
  spare. RAM is not the constraint; the install is.
- Target runtimes on this VM (needed to RUN emitted programs, not to
  compile them): Temurin JDK 21 is 347 MB, .NET SDK 8 is 575 MB,
  the Node binary is 126,879,696 bytes (about 121 MB). These dwarf
  newr itself and are the real download cost of the Java/C#/JS
  targets.

## 4. Distribution routes

### Route A: PyPI + zipapp (today's route)

- Download: the `.pyz` is 2,430,019 bytes measured. The pip package
  would be about the same size plus metadata (UNVERIFIED, not
  published yet).
- Needs first: CPython 3.10+ installed (55 MB of stdlib on disk here;
  the Windows installer download is roughly 28 MB, UNVERIFIED).
  Then `pip install newr-lang` or download the `.pyz` and run
  `python newr.pyz ...`.
- RAM: about 18 MB. Offline after install: yes, fully.
- Trade-offs: smallest download, the standard Python distribution
  story, and the only route that is already designed (SPEC section
  13.10). But the Python install is the whole friction: on old
  machines there may be no installer at all (Windows 7, 32-bit
  3.13, macOS 10.12), on locked-down machines the user may not be
  allowed to install anything, and on phones there is no Python to
  install. Rank: 3rd for the user's rule.

### Route B: standalone one-file binary (PyInstaller)

- Download: measured 8,555,776 bytes (about 8.2 MB) for Linux
  x86_64, built in this session with PyInstaller 6.22.3 from the
  clean tree. Windows and macOS builds are UNVERIFIED; estimate
  8 to 12 MB each based on the Linux measurement.
- Needs first: nothing. No Python, no install step. Download one
  file, run it.
- Verified working in this session: `check` on hello and shop,
  `build --target py/js/java/cs` on shop, all exit 0 (shop carries
  its 3 known baseline warnings). Binary max RSS 18.5 MB.
- One real gotcha, found by actually building it: a naive onefile
  build FAILS at `build` time with
  `FileNotFoundError: .../compiler/newr_rt.py`, because the emitter
  reads the `newr_rt.*` runtime files from disk
  (`compiler/cli.py`, `importlib.resources.files("compiler")` with a
  `Path(__file__).parent` fallback) and PyInstaller only bundles
  imported modules, not data files. The fix is verified: add all
  seven runtime files with `--add-data`, e.g.
  `--add-data "compiler/newr_rt.py:compiler"` (and `.js`, `.java`,
  `.cs`, `.rs`, `.kt`, `.dart`). Without the data files the binary
  is 8,471,464 bytes and `check` still works, but `build` does not.
  The M5 (Rust/Kotlin/Dart) targets were NOT tested through the
  binary in this session (M5 is still being built): UNVERIFIED.
- Trade-offs: the best native experience, and the only native route
  for machines without Python. But one build per OS and arch, and
  the old-OS coverage is set by the BUILD machine, not the target
  machine: a Linux binary built on a glibc 2.35 machine does not run
  on a glibc 2.17 machine ("version `GLIBC_2.xx' not found"). So
  Linux binaries must be built in the oldest container they must
  support (manylinux_2_17, glibc 2.17, circa 2014), macOS binaries
  need `MACOSX_DEPLOYMENT_TARGET=10.13`, and the Windows toolchain
  floor (PyInstaller 6.x) still needs a live check. Onefile binaries
  also extract to a temp dir on every cold start, so startup is
  slower than the .pyz (startup time NOT measured: UNVERIFIED).
  Rank: 2nd for the user's rule.

### Route C: browser playground (Pyodide/WASM)

- Download on first load: Pyodide 0.28.0 payload is
  `pyodide.asm.wasm` 8.3 MB + `pyodide.asm.js` 1.1 MB +
  `python_stdlib.zip` 2.4 MB, about 12 MB total, plus the newr
  compiler at about 2.4 MB as a `.pyz` or source tree. Total first
  load about 14 to 15 MB, then cached (Service Worker / Cache API)
  for offline use.
- Needs first: nothing but a browser. Zero install.
- VERIFIED working in this session (section 5): the real compiler
  runs inside Pyodide (CPython 3.13.2 in WASM): lex, parse, check,
  and build for py/js/java/cs, plus a clean error diagnostic.
- Trade-offs: the widest reach of any route, including phones,
  tablets, Chromebooks, and locked-down PCs, and the only route for
  iOS. But it needs a WASM-capable browser (Chrome 57+, Firefox
  52+, Safari 11+, Edge 16+: standard browser history, re-check at
  playground build time) and roughly 150 MB of free RAM for the WASM
  runtime, so it is tight on a 512 MB device and is the weakest
  route there (UNVERIFIED on real low-end hardware). Emitted
  programs have limits in the page: emitted Python can run in the
  same Pyodide, emitted JS can run in the page, but emitted
  Java/C#/Rust/Kotlin/Dart need their native toolchains, which do
  not exist in the browser. File access is sandboxed (File System
  Access API, or download/upload). Rank: 1st for the user's rule.

## 5. Browser route feasibility probe (actually run)

Probe date: 7 Nov 2026. Tooling: Node v24.22.1, npm 10.9.4,
Pyodide 0.28.0 installed from npm into `/tmp/pyodide-probe`
(13 MB on disk). The repo was mounted read-only through NODEFS;
no repo file was modified. Probe scripts:
`/tmp/pyodide-probe/probe.mjs` and `probe2.mjs` (scratch, not in
the repo).

Results, all measured in Node (not a real browser):

- Pyodide ready in 5,508 to 6,391 ms across runs (CPython 3.13.2
  in WASM). Total probe wall time about 10 s.
- Lex `examples/hello.newr`: 7 tokens, 219 ms (includes first
  import of the compiler).
- Parse hello: OK, 294 ms.
- `check` hello: exit 0, 3 ms. `check` showcase: exit 0, 6 ms.
- `build --target py` hello: exit 0, 379 ms, emitted the correct
  `print("Hello, world!")`.
- `build --target js` hello: exit 0, 505 ms.
- `build` shop (the big example, with its 3 baseline warnings):
  py exit 0 in 667 ms, js exit 0 in 826 ms, java exit 0 in 962 ms,
  cs exit 0 in 1,333 ms.
- Error path works: an unterminated string gives a clean
  `error[E_UNTERMINATED_STRING]` diagnostic with span and source
  line, exit 4, in 1 ms. No traceback.
- Conclusion: the browser route is PROVEN feasible for check and
  build on all four finished targets. No compiler change was needed
  to run under Pyodide: the compiler is pure stdlib, and every
  stdlib module it imports (`unicodedata` included) loads in
  Pyodide 0.28.0.

What the probe did NOT prove (UNVERIFIED): timing and behaviour
inside a real browser (Chrome/Firefox/Safari), on a real phone,
first-load time on a slow connection, Service Worker offline
caching (not built), the rs/kt/dart targets in the playground (M5
in progress), and running emitted Python inside the same Pyodide
(not attempted; the emitted file was only generated, not executed).

To reproduce: `cd /tmp/pyodide-probe && npm install pyodide@0.28.0`
then `node probe.mjs` (hello) and `node probe2.mjs` (shop, all
four targets, error path). If the probe directory is gone, the
steps are: install Pyodide from npm, `loadPyodide()`, mount the
repo with NODEFS at `/newr`, `sys.path.insert(0, "/newr")`, then
`from compiler.cli import main as cli_main` and call it with
argument lists exactly like the command line.

## 6. Python floor analysis (by grepping the code, not guessing)

Floor today: 3.10 (pin P1, SPEC and BUILD_REPORT.md section 14.1).
Method: `grep` plus an AST walk over every `.py` in the repo
(excluding `.git`), plus `ast.parse(feature_version=(3, 9))` over
all of them.

### What the compiler source actually uses

- `X | Y` union syntax: 502 occurrences in `compiler/`, ALL of
  them in annotations, ALL in files that start with
  `from __future__ import annotations` (18 compiler files carry
  that import). Samples: `compiler/errors.py:126-131`
  (`expected: str | None = None` and siblings),
  `compiler/ast_nodes.py:35` (`span: Span | None`),
  `compiler/ast_nodes.py:165` (`body: list["Stmt"] | None`),
  `compiler/emitter.py:253`
  (`self.module_lets: dict[str, Type | None] = {}`),
  `compiler/cli.py:392` (return annotation
  `tuple[object | None, list[Diagnostic], str]`),
  `compiler/parser.py:1388`
  (`-> tuple[str, list[str] | None]`),
  `compiler/resolver.py:1197` (`main_path: Path | str | None`),
  `compiler/checker.py:86`
  (`self.top_lets: dict[str, A.Let | A.Const]`),
  `compiler/discord_check.py:364`
  (`-> float | int | None`). With the future import, annotations
  are strings and are never evaluated, so none of these need 3.10.
  The only compiler file WITHOUT the future import is
  `compiler/__init__.py`, which contains no union syntax at all.
- `match` statements: 0 in the whole repo (the newr language has
  `match`; the compiler itself uses none).
- `dataclass(slots=True)`: 0. All dataclasses are plain
  `@dataclass` or `@dataclass(frozen=True)`.
- `str.removeprefix` / `removesuffix`: 0.
- `get_type_hints`, `dataclass(kw_only=...)`, `except*`,
  `tomllib`, `typing.Self`, `TypeAlias`, `ParamSpec`,
  `datetime.UTC`, `int.bit_count`, `zip(strict=True)`: 0 each.
- Walrus `:=`: 0 in `compiler/`.
- `cast(...)`, runtime `list[...]` / `dict[...]` subscripts in
  `isinstance`: 0. `Optional` / `Union` appear only in comments.
- Runtime `|` uses are set/frozenset unions only (e.g.
  `compiler/emitter.py:95`,
  `frozenset(keyword.kwlist) | {"match", "case", "type"}`), which
  work on every Python 3.
- `ast.parse(feature_version=(3, 9))` over every `.py` in the repo:
  0 files fail. Caveat: that check is syntax-only and is
  INCONCLUSIVE for 3.12-only f-string forms (verified: a
  nested-quote f-string is accepted by the parser even with
  `feature_version=(3, 10)`). A targeted grep for backslashes and
  nested quotes inside f-string expressions found none in the
  compiler source: every hit is a backslash or escaped quote in the
  literal part of the string (e.g. `compiler/lexer.py:702`,
  `compiler/discord_check.py:482`, `compiler/emitter.py:2486`,
  which builds generated code as a string). So no 3.12-only
  f-string form was found, by grep, not by parser proof.

### Where 3.10 IS actually required today

Two places, neither of them the compiler source:

1. Generated Python code. `goldens/py/shop.py:47, 104, 132, 135,
   160, 209` uses `int | None`, `str | None`, and
   `types.SimpleNamespace | None` in function signatures, and the
   generated file has NO `from __future__ import annotations` (the
   emitter, `compiler/emitter.py:27`, has the import for itself; it
   does not emit it). Annotations in generated code are evaluated
   at function-definition time, so emitted `.py` needs Python 3.10
   at runtime. This is the one real 3.10 pin on the user side.
2. Dev-only scripts without the future import that use `X | Y`
   annotations: `benchmarks/run.py:34, 59`,
   `benchmarks/run_cs.py:66, 67, 93, 94, 164`,
   `benchmarks/run_java.py:58, 59, 85, 103, 212`,
   `benchmarks/run_js.py:41, 42, 68`,
   `conformance/run.py:233, 395`. These are not shipped, but CI
   runs the conformance harness, so they count for the CI matrix.

### Cost of dropping the floor

- To 3.9: one emitter change (emit
  `from __future__ import annotations` at the top of every
  generated `.py`, or emit `Optional[...]` instead of `X | Y`),
  then regenerate all goldens with `--bless` and re-verify the
  full suite plus conformance on every target; add the future
  import to the five dev scripts above (or exclude them from the
  floor claim); add Python 3.9 to the CI matrix (3 more jobs in
  the matrix). Compiler source: zero changes. Estimated as a
  small, mechanical change; NOT implemented, NOT measured.
- To 3.8: the same as 3.9, plus a re-check for builtin-generic
  runtime use (none found in this audit) and for `str.removeprefix`
  (none found). Nearly identical cost. Also NOT implemented.
- To 3.7 or older: not analyzed. `from __future__ import
  annotations` exists since 3.7, but dataclasses, `dict` ordering
  guarantees, and several stdlib behaviours the compiler leans on
  would each need a line-by-line audit. Not worth it; see verdict.

### Verdict: keep 3.10

1. Python 3.8 and 3.9 are end-of-life (no security fixes since
   October 2024 and October 2025). Shipping a floor on dead
   interpreters is a liability, not a feature.
2. The audience a lower floor would serve is tiny and mostly
   unreachable anyway: a machine that cannot install Python 3.10
   from python.org (Windows 7, 32-bit with 3.13, macOS 10.12)
   also cannot install Python 3.9/3.8 from python.org today in
   any supported way. The one real exception is old Linux
   distributions whose system Python is 3.8/3.9 (Ubuntu 20.04,
   Debian 11): those users are served better by the binary route
   (no Python at all) than by a floor drop.
3. The binary and browser routes cover the old-device cases a
   floor drop cannot: no Python installed, no permission to
   install, phones and tablets.
4. Every floor drop adds permanent CI cost (a matrix row on every
   job) and golden churn for the emitter change.

Revisit only if a real user on Ubuntu 20.04 / Debian 11 asks for
system-Python support and cannot use the binary or the browser.
Then do the 3.9 drop as a single milestone: emitter future-import,
golden regen, suite + conformance green, CI row 3.9 green, and
record it in BUILD_REPORT.md.

## 7. CI matrix expansion plan

Current state (`.github/workflows/ci.yml`, 9 jobs, NEVER run):
`front-end`, `v0-conformance`, `build-zipapp`, `zipapp-diff`,
`golden-rebuild`, `python-emitter`, `js-emitter`, `java-emitter`,
`cs-emitter`. Matrix: ubuntu/macos/windows-latest times Python
3.10, 3.11, 3.12, 3.13, `fail-fast: false`. No rs/kt/dart jobs
yet (M5 in progress). The `build-zipapp` job builds the `.pyz`
once on Ubuntu and the `zipapp-diff` job runs that same artifact
on all three OSes, which is the right portability proof.

Order of work (do not skip step 1):

1. First green run of the existing matrix. Proof: all 9 jobs
   green; record the exact runner OS versions and Python versions
   from the logs. Until this is green, every other step is
   speculation.
2. Pin the runner labels that matter: add an explicit
   `macos-13` (Intel) job and an `ubuntu-22.04` job next to the
   `*-latest` jobs, so an Apple Silicon-only or Ubuntu 24-only
   pass cannot hide an Intel or 22.04 failure. Proof: green on
   both.
3. Binary jobs: one PyInstaller build per OS and arch
   (linux x86_64, macos arm64, macos x86_64, windows x86_64),
   each followed by a smoke test in the same job: `check` hello
   and shop, `build --target py/js/java/cs` on shop, exit 0.
   Linux binary built in a manylinux_2_17 container. Proof:
   the artifact plus a green smoke test on each runner.
4. Playground job: Playwright (Chromium, Firefox, WebKit)
   loading the playground page, running `check` and `build` on
   hello in-page, asserting exit 0 and the expected output.
   Proof: green on all three engines, with first-load timings
   recorded.
5. ARM64: GitHub Linux ARM64 runner for the suite and
   conformance; a Raspberry Pi self-hosted runner (or QEMU) for
   ARMv7 32-bit. Proof: suite green on ARM64; a recorded run on
   real Pi hardware for ARMv7.
6. 32-bit: Windows x86 Python 3.10/3.11/3.12 job (only if the
   installers still exist at build time; check python.org first)
   and a Linux i386 container job. Proof: suite green.
7. Android/Termux: no CI runner exists. A documented manual
   check: `pkg install python`, install the `.pyz`, run the
   suite's smoke commands, record the Termux and Python versions
   and the output. Re-do on each release.
8. Old-OS spot checks (manual, per release, not per commit):
   Windows 8.1 with the binary, macOS 10.13 with the binary,
   Windows 7 with the playground in a current browser. Proof: a
   dated note with versions and results, filed in this doc's
   UNVERIFIED list as it clears.

What counts as proof for each tier: Tier 1 needs the CI matrix
green. Tier 2 needs either a green dedicated job or a dated,
recorded manual run. Tier 3 needs a green Playwright job plus a
recorded run on one real device of each kind (an iPhone, a
Chromebook without Linux, a Windows 7 machine).

## 8. The offline-after-download rule

The promise is: after the one download, everything works with no
internet. The split is:

Works offline, and must be tested offline (airplane mode, or a
container with no network, before each release):

- The compiler: `check` and `build` for every target. Emitters
  are local code; emitting never touches the network.
- The docs: `docs/` ships in the repo and in the pip package;
  the playground bundles the docs page so it works from cache.
- The examples, the conformance corpus, and the goldens.
- The `.pyz` and the binary: no network at runtime.

Legitimately needs internet, and must be labeled as such wherever
it appears:

- Downloading newr itself, the first time (PyPI, the `.pyz`,
  the binary, or the playground page).
- Target runtimes to RUN emitted programs: Node for JS
  (binary about 121 MB), JDK 21 for Java (347 MB here), .NET 8
  for C# (575 MB here), plus cargo/rustc, kotlinc, and the Dart
  SDK for the M5 targets (sizes UNVERIFIED). These are the
  targets' costs, not newr's, and the docs must say so next to
  every "run your program" instruction.
- The libraries an emitted program imports at runtime:
  discord.py / discord.js for bots (pip / npm), and any other
  target library. Download once, then offline.
- PyPI / npm themselves, when installing those libraries.

What must never need internet: compiling, checking, emitting,
reading the docs, and running the playground after its first
load. If any of those ever phones home, that is a bug.

## 9. UNVERIFIED list

Everything in this doc that was not proven live in this session.
Do not present any of these as fact until the check in brackets
is done.

- CI has never run. All 9 jobs, all three OSes, all four Python
  versions. [First green run.]
- Windows and macOS execution of the compiler, any route.
  [CI step 1.]
- PyInstaller binary size and behaviour on Windows and macOS.
  [CI step 3.]
- PyInstaller binary cold-start time on any OS. [Measure in the
  binary job.]
- Linux binary behaviour on old glibc (manylinux_2_17 build).
  [CI step 3, old-container build.]
- Pyodide in a real browser (Chrome/Firefox/Safari), on a real
  phone, and first-load time on a slow connection. [CI step 4
  plus one real-device run.]
- Service Worker / Cache API offline caching of the playground.
  [Playground build.]
- Behaviour on a real 512 MB device, and on ARMv7 / 32-bit
  hardware. [CI steps 5 and 6, plus a real Pi run.]
- Termux on Android. [CI step 7 manual check.]
- Windows 7 with the playground in a current browser; macOS
  10.12 and older in the browser. [CI step 8 spot checks.]
- Exact CPython installer availability per old OS: Windows 7
  (none for 3.10+), Windows 8.1 (last version with an installer),
  32-bit Windows (last version with an installer), macOS 10.12
  and 10.13 floors. [Check python.org download pages at build
  time and record the versions.]
- Exact GitHub runner OS versions behind `ubuntu-latest`,
  `macos-latest`, `windows-latest`. [Record from the first green
  run's logs.]
- Running emitted Python inside the same Pyodide page, and
  emitted JS in the page. [Playground build; generation only was
  proven here.]
- The rs/kt/dart targets through the PyInstaller binary and in
  the playground. [M5 completion, then re-run both probes.]
- The cost of a 3.9 floor drop (emitter future-import change,
  golden regen, re-verification). [Only if the floor decision
  ever changes.]
- Sizes quoted as estimates: Windows installer download
  (about 28 MB), per-OS binary sizes (8 to 12 MB), WASM
  browser RAM (about 150 MB free). [Measure at build time.]
- iOS App Store rules as the reason iOS is browser-only.
  [Standard platform policy; re-check if Apple changes it.]

## 10. Sources for the numbers in this doc

- Repo tree at the time of writing: M4 pushed as `23c61e3`
  (suite 902/902, conformance 74/74 on py/js/java/cs); M5
  (Rust/Kotlin/Dart) in progress in the same tree; this doc
  touches nothing in the tree.
- Sizes: `du` and `ls -la` on this VM, 7 Nov 2026.
- Zipapp: `python3 -m zipapp` run in this session; the clean
  `.pyz` was executed and printed ok.
- PyInstaller: version 6.22.3, installed in a venv in this
  session; onefile build run twice (once without data files,
  once with), both binaries executed.
- Pyodide: version 0.28.0 from npm, run under Node v24.22.1;
  probe scripts in `/tmp/pyodide-probe/` (scratch, not in the
  repo).
- Python floor: `grep -rn` and an AST walk over every `.py`
  in the repo, plus `ast.parse(feature_version=(3, 9))`.
- CI: `.github/workflows/ci.yml` as read on 7 Nov 2026.
- SPEC.md section 13.10 (distribution) and BUILD_REPORT.md
  section 14.1 (pin P1, Python floor 3.10) as the standing
  decisions this doc does not change.
