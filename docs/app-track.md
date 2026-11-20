# M8 Full Stack: database, backend, frontend, UI/UX, one source, all OSes

Status: draft design doc, 7 Nov 2026, by chie. Not built yet. This doc is the
research step for M8, written because the user asked for the way ("cari cara")
to make newr build database-backed backends, frontends, UI/UX, and full-stack
apps on every compatible OS.

Standing process rule (user, 7 Nov 2026 16:59 WIB): every new M milestone
starts with evaluation and research first, before building. This doc is that
step for M8. Building starts only after this doc is reviewed against the real
codebase, and each M8 phase repeats the same order: research and eval, build,
verify.

M numbering: M7 is not a stop. It is only the last milestone written down so
far. M8 is the next number. The numbering continues as needed (M9, M10, ...),
and the real rule is the user's own order: keep going until absolute
("sampai mutlak").

## 1. Goal

One readable newr source builds a complete app:

- a database layer (store data, query it, transactions),
- a backend (HTTP server, routes, JSON in and out),
- a frontend (pages or screens that call the backend),
- UI/UX (widgets and layout the user actually sees and taps),
- packaged for the OS it runs on (apk, exe, dmg, AppImage, web).

The same source must keep working on all seven emitter targets
(py, js, java, cs, rs, kt, dart). Where a target cannot do something
natively, the doc says so plainly instead of pretending otherwise.

## 2. Where newr stands today (checked live, 7 Nov 2026)

What already exists:

- Seven emitters: py (M2), js (M3), java and cs (M4), rs, kt, dart (M5,
  in flight). The emitters cover every OS between them: Dart/Flutter for
  mobile, desktop and web; Kotlin for Android; C# for Windows and desktop;
  Java for Android and desktop; JS for web; Rust native; Python everywhere.
- A module system with a proven domain-module pattern. Core modules live
  as signature tables in `compiler/modules.py` (math, time, random, file,
  env). The `discord` module is the proof that a domain module works: its
  signatures and limits live in their own files
  (`compiler/discord_check.py`, 1115 lines; `compiler/discord_limits.py`,
  190 lines), the resolver lists it in `NAMED_MODULES`
  (`compiler/resolver.py`), and the Python emitter has a dedicated
  bot-program path that emits a real discord.py program
  (`compiler/emitter.py`, bot path near line 2637).
- A store pattern already exists in generated code: the emitted discord.py
  bot uses aiosqlite as its store (the emitter's third-party set includes
  `discord`, `discord.ext`, `aiosqlite`).
- SPEC already plans two pieces of this: HTTP fetch is v1.1 and the `json`
  module is v1.1 (SPEC section 20).

What does not exist yet:

- No `db` module, no `http` module, no `ui` module, no `app` module.
  Modules today are only math, time, random, file, env, t, discord, log.
- No app example (examples are hello, hello_bot, shop, showcase only).
- No packaging story beyond M7 (standalone binaries and the browser
  playground, see `docs/run-anywhere.md`).

So the gap is modules plus packaging, not a new compiler. That is the way.

## 3. The way: copy the discord pattern, four times

Every new module follows the exact path `discord` already walked:

1. Signatures: a table in the checker style (`compiler/modules.py` for
   core, a new `compiler/<name>_check.py` for a domain module).
2. Resolver: add the name to `NAMED_MODULES` in `compiler/resolver.py`
   and to `RESERVED_BUILTIN_NAMES` in `compiler/modules.py`.
3. Limits: a `<name>_limits.py` file with the honest limits of the
   module, the way `discord_limits.py` holds Discord's limits.
4. Emitters: each of the seven emitters maps the module calls to the
   native library on its target, plus runtime helpers in `newr_rt.*`
   where the mapping needs shared code.
5. Conformance: `ok` programs in `conformance/ok/` that print PASS on
   every target the module claims to support. A target that cannot pass
   does not ship the module (SPEC section 18).
6. Goldens: pinned generated outputs for the module's example programs,
   blessed only from a green run, never hand-edited.

No module ships on fewer than all seven targets unless the doc for that
module names the missing target and why, and the gap is tracked as an
open item, not hidden.

## 4. The four new modules (draft API, not final)

### 4.1 `db`: database, sqlite first

Why sqlite first: it is a single file, needs no server, and every one
of the seven targets has a sqlite library. Postgres and MySQL come
later, as a second driver, not in M8.

Draft surface (newr syntax, same style as `file.read` and `math.sqrt`):

- `db.open(path: str)` opens or creates a database file, returns a handle.
- `db.exec(handle, sql: str, args: list)` runs a statement, returns rows
  affected.
- `db.query(handle, sql: str, args: list)` runs a select, returns a list
  of dicts.
- `db.close(handle)` closes the handle.

Rules: one connection per handle, like the bot store today. Writes that
belong together go in one transaction; a failed step rolls the whole
transaction back. SQL stays SQL (a string), the module does not invent
its own query language in M8. Parameter binding is mandatory in the
generated code, never string-built SQL, so injection is closed by
construction.

Per-target natives: Python aiosqlite/sqlite3, JS better-sqlite3 (Node)
and sql.js (browser), Java sqlite-jdbc, C# Microsoft.Data.Sqlite, Rust
rusqlite, Kotlin sqlite-jdbc (JVM) and android.database.sqlite
(Android), Dart sqflite (mobile) and sqlite3 (desktop). The browser
target stores in IndexedDB via sql.js where a plain file is impossible;
that difference is documented per target, not papered over.

### 4.2 `http`: backend server plus fetch client

Draft surface:

- `http.serve(port: int)` starts a server for the routes of this program.
- `http.route(method: str, path: str, handler: fn)` registers one route.
  Handlers take a request (method, path, headers, body) and return a
  response (status, headers, body).
- `http.get(url: str)` and `http.post(url: str, body: str)` fetch a URL,
  return a response value. This is the SPEC v1.1 fetch item, pulled into
  M8 because the frontend needs it to call the backend.

Rules: JSON is the only body format in M8 (the `json` module lands with
it: `json.encode`, `json.decode`). No auth, no sessions, no cookies in
M8; those are M9 material. The server binds localhost by default and
only listens on all interfaces when the program says so explicitly.

Per-target natives: Python stdlib http.server or a tiny WSGI host,
JS Node http, Java built-in HttpServer, C# ASP.NET minimal API or
HttpListener, Rust a small embedded server (axum or std-only), Kotlin
Ktor or HttpServer, Dart shelf. The js target in newr is Node, not a
browser: `http.serve` works on js (proven live in the M8d eval, which
served `examples/hello_api_server.newr` under Node and fetched it with
curl). There is no browser target in newr, so there is no target on
which `http.serve` is a checker error; the frontend/backend split is a
documented convention, not a checker rule (see section 5 and SPEC
§22.4).

### 4.3 `ui`: widgets and layout

This is the hardest module, and the doc is honest about why: no single
UI API looks and behaves identically on every OS. Two shapes were
considered:

- (a) One small widget set mapped per target (text, button, input,
  list, image, stack, row, column). Each target renders it with its
  native toolkit. This is the recommendation.
- (b) One target-specific UI module per platform. Rejected for M8: it
  splits the language into seven dialects, which breaks the one-source
  promise.

Draft surface (shape a), corrected in the M8c eval
(`newr-research/m8c-ui-eval/EVAL.md`) and pinned in SPEC §12.8:

- `ui.text(s: str)`, `ui.button(label: str, on_tap: fn)`,
  `ui.input(placeholder: str, value: str, on_change: fn)`,
  `ui.list(items: list)`, `ui.image(url: str)`,
  `ui.row(children: list)`, `ui.column(children: list)`,
  `ui.render(root: Widget) -> str` (deterministic HTML, the
  conformance-testable core), `ui.show(root)` (native window where
  a toolkit exists, HTML file on js and dart),
  `ui.run(view: fn() -> Widget)` (the interactive loop).

Rules: the widget set stays small in M8 (seven widgets plus two layout
containers). Styling is platform defaults in v1, not tokens and not
pixels: no colors, fonts, sizes, or padding arguments. No custom
drawing, no animation API in M8. State is a view function plus
external state: `ui.run(view)` re-invokes the view after each event,
and state lives in the `db` module or an `http` backend. A tap
handler never assigns to a captured variable: closures capture by
value (SPEC §7.2), so that assignment is a silent no-op at runtime,
proven by probe in the M8c eval. The draft sentence "a tap handler
mutates state and the tree re-renders" is wrong and is corrected in
SPEC §12.8.

Per-target natives (corrected in the M8c eval; the draft lines below
were wrong): JS renders to HTML (no window; `ui.show` writes a file);
Dart renders to HTML only in M8c (Flutter is NOT installed on the
build VM and needs a 1.5 to 4 GB install plus apt packages plus a
runner change, so it is deferred to a later phase); Kotlin renders
to Swing on the JVM (Jetpack Compose needs Gradle and the Compose
plugin, not plain kotlinc); C# renders to Avalonia 12.1.6 (WinUI is
Windows only, MAUI has no official Linux desktop target); Java
renders to Swing (JavaFX is not in the JDK and needs an OpenJFX
download); Rust renders to egui 0.37.0 plus eframe; Python renders
to tkinter 8.6 stdlib. Where a target cannot render a widget
natively in M8, the module falls back to the HTML sink and the
conformance program for that target says so.

### 4.4 `app`: manifest and packaging

CORRECTED in the M8d eval (`newr-research/m8d-app-eval/EVAL.md`,
sections 1 and 2): there is NO `app.manifest(...)` language call. The
draft API below is withdrawn. `newr.json` already declares name and
version (M6 Phase 2b), and the build already reads it; a language call
would create two sources of truth for the same facts. The manifest
file IS the declaration. M8d adds three optional keys to `newr.json`
(`description`, `icon`, `id`, all validated by `load_manifest` with
`ManifestError`, exit 1 on a bad manifest), and a `newr build --app`
flag (a CLI flag, not a module call) that emits then packages one
program for one explicit target into `dist/<name>-<target>/` plus a
`BUILD-RECEIPT.json`. Full spec: SPEC §22. There is no `app` module in
the language, and no new error code for it.

Packaging per OS: Android apk via the Kotlin or Dart target (needs the
Android SDK); Windows exe/msi via the C# target; macOS dmg via the Dart
target (needs Xcode on a Mac for signing, unsigned otherwise); Linux
AppImage or deb via the Rust or Python target; web via the JS target
plus the M7 playground hosting story. M7 already covers standalone
binaries; M8 adds the manifest, the icon and permission declarations,
and the per-OS artifact step. Signing and store submission (Play,
App Store, Microsoft Store) are explicitly out of M8: they need the
user's own developer accounts. M8d claims only the per-target artifacts
in SPEC §22.2: no apk, dmg, msi, deb, or AppImage is built on this VM
(no toolchains or keys for them here), and the cs artifact is a
self-contained publish directory, because a framework-dependent
`dotnet publish` does NOT produce a runnable artifact on this VM (its
apphost cannot find the runtime, which lives in `~/.dotnet`, not on
PATH; verified live in the M8d eval).

## 5. Full-stack shape: one source, split by target

A full-stack app in M8 is five files sharing one contract, not one
file doing everything:

- `shared.newr`: the data shapes (structs) and the route table both
  sides agree on. Imported by the other four.
- `view.newr`: the view builder both frontends import. It builds the
  widget tree from a product list; it holds no state and no entry.
- `backend.newr`: uses `db` and `http`, serves the routes, talks to
  sqlite. Builds for py, java, cs, rs, kt (server targets).
- `frontend_js.newr`: the js server frontend. Uses `http` (fetch) and
  `ui`, calls the backend, serves the rendered page. Builds for js.
- `frontend_py.newr`: the py native frontend. Uses `http` (fetch) and
  `ui`, calls the backend, shows a native window through `ui.run`.
  Builds for py.

CORRECTED in the M8d eval (sections 1, 5.1, and 6): the checker does
NOT enforce the split, and it cannot as written. No target reaches any
module checker, `newr check` has no `--target` flag, and a program that
calls `http.serve` on js checks clean. The split is a documented
project convention in M8d (SPEC §22.4), not a compiler rule: the shop's
README and SPEC say which file builds for which target, and the
conformance suite builds each file for its intended targets only. The
reason is that js is a Node target that serves, fetches, and renders,
not a browser-only target, so a rule forbidding `http.serve` on js
would forbid working programs.

The js frontend is a Node server program, not a static page (eval
§5.1, SPEC §22.3): it fetches the product list from the backend,
serves the rendered page over `http.serve`, and a small static bridge
script turns button clicks into requests to an `/action` route that
dispatches by button index and re-renders. The static page is only the
fallback if the bridge is cut. The py frontend is a native window via
`ui.run`. Shared types are checked once and must match on both sides;
a drift between frontend and backend types is a build error, not a
runtime surprise. (One blocker, eval §5.3: shared structs must be
constructible from an importing module, which today fails with
`error[E3001]`; the Phase 0 fix lands before any shop code is written.)

A single-file full-stack program (backend plus frontend in one file,
compiler splits it) is the M9 goal, not M8. M8 proves the five-file
shape end to end first.

## 6. Per-target map (summary)

| Target | db | http serve | http fetch | ui | app package |
|---|---|---|---|---|---|
| py | sqlite3 | yes | yes | tkinter 8.6 | AppImage, deb |
| js | sql.js / IndexedDB | yes (Node serves) | yes | HTML sink (render only) | web |
| java | sqlite-jdbc | yes | yes | Swing | jar, deb |
| cs | Microsoft.Data.Sqlite | yes | yes | Avalonia 12.1.6 | self-contained publish dir (framework-dependent does NOT run on this VM; no msi in M8d) |
| rs | rusqlite | yes | yes | egui 0.37.0 + eframe | binary, AppImage |
| kt | sqlite-jdbc / android sqlite | yes | yes | Swing | apk, jar |
| dart | sqflite / sqlite3 | yes | yes | HTML sink (render only, Flutter deferred) | apk, dmg, web |

"Yes" means a conformance program prints PASS on that target.
"Fallback" and "checker error" are stated in the module doc, not hidden.

## 7. Phases (each: research and eval, build, verify)

- M8a `db`: LANDED 10 Nov 2026 (research in `newr-research/m8a-db-eval/EVAL.md`,
  build report in `newr-research/m8a-db-build/BUILD_REPORT.md`). SQLite,
  sync, on all seven targets: py stdlib sqlite3, js node:sqlite
  (Node floor 24.17.0), java/kt sqlite-jdbc 3.54.1.0 + slf4j-api
  2.0.19, cs Microsoft.Data.Sqlite 8.0.0 (net8.0), rs rusqlite 0.41.1,
  dart package:sqlite3 3.9.0. Handles are ints, fd style; `?` binding
  mandatory; BEGIN IMMEDIATE plus COMMIT/ROLLBACK; PRAGMA busy_timeout
  5000. `db.query` returns a typed struct list via expected-type
  inference (the open question in section 9 is settled: typed struct
  list, never stringified dicts). Suite 1276 tests OK (skipped=15);
  conformance 80/80 on all seven targets; `examples/todo.newr` stores
  and lists rows, goldens pinned for it on all seven targets.
- M8b `http` plus `json`: LANDED 10 Nov 2026 (research in
  `newr-research/m8b-http-eval/EVAL.md`, build report in
  `newr-research/m8b-http-build/BUILD_REPORT.md`). `http` server
  plus fetch client and typed `json` encode/decode, on all seven
  targets: py stdlib http.server/urllib/json, js node:http plus
  a subprocess fetch (Node has no sync fetch) plus a
  number-preserving JSON pre-pass, java/kt JDK HttpServer plus
  HttpClient with hand-written strict RFC 8259 JSON
  parser/serializer, cs HttpListener/HttpClient/System.Text.Json
  (no NuGet), rs tiny_http 0.12.0 plus ureq 3.5.1 (rustls) plus
  serde_json 1.0.154, dart dart:io plus dart:convert (no pubspec
  change). Routes are exact-match only; fetch returns a typed
  HttpResponse struct via expected-type inference; timeout
  default 10s; allowlist localhost only by default (manifest
  `http_allowlist` is a static checker concern); E2012 (http)
  and E2013 (json) are the two new codes (error index 41).
  Suite 1392 tests OK (skipped=15); conformance 83/83 on all
  seven targets (new mod_json, mod_http_server/client pair
  with a two-process harness in conformance/run.py, two err
  programs); `examples/hello_api_server.newr` plus
  `examples/hello_api_client.newr`, goldens pinned for the
  client on all seven targets, pair verified live on py and js.
- M8c `ui`: eval DONE 10 Nov 2026 (research in
  `newr-research/m8c-ui-eval/EVAL.md`, verdict conditionally
  feasible, shape (a) with corrections). The draft's WinUI/MAUI,
  Compose, Flutter, and state-model claims were all wrong and are
  corrected in SPEC §12.8 and in §4.3 above. Build in progress:
  `ui.render` deterministic HTML on all seven targets, `ui.show`
  native window on py/java/kt/cs/rs, HTML sink on js/dart,
  `ui.run` Elm-style loop with db/http state. E2014 is the ui code.
  Pins: Avalonia 12.1.6, egui 0.37.0, tkinter 8.6, Swing.
- M8d `app` plus full-stack example: manifest extension (three
  optional `newr.json` keys, SPEC §22.1), `newr build --app` packaging
  flag with `BUILD-RECEIPT.json` (SPEC §22.2), then the five-file shop
  app (shared types, view builder, backend with db and http, js server
  frontend and py native frontend with ui;
  js frontend is a Node server program with the bridge contract,
  SPEC §22.3; split documented, not enforced, SPEC §22.4), verified
  live on at least one server target and one client target. The eval
  is in `newr-research/m8d-app-eval/EVAL.md` (verdict: conditionally
  feasible). Phase 0 also fixes the imported-struct gap (eval §5.3)
  and ports the `http_check.py` double-walk fix plus the `test_fmt.py`
  `_strip_spans` fix from the M8c follow-ups.

Each phase lands as real pull requests, one per module where feasible,
the same way M5 is landing, so the achievement work (Pull Shark) keeps
moving with honest PRs.

## 8. Verification bar (same as M4, no exceptions)

- Suite green (`python3 -m unittest discover -s tests`) in system
  Python and a fresh venv.
- Conformance green on every target the phase claims, counted exactly
  (ok and FAIL lines per target, not a tail sample).
- Goldens pinned for the new example programs, blessed only from green.
- Live proof: the example program actually runs (backend serves, client
  fetches, UI renders) and the output is shown, not just asserted.
- Red-check: breaking one mapping reproduces a real failure, then the
  fix restores the exact green hash.

## 9. Honest limits and open questions

Limits:

- M8a `db` limits (10 Nov 2026): SQLite only, no Postgres/MySQL.
  The js target is Node only (node:sqlite); the browser target
  cannot run `db` (sql.js plus IndexedDB is the documented shape,
  not built). On cs/rs/dart the db helpers are emitted inline into
  general files only, so a bot program's handler using `db.*` fails
  at compile time with an undefined reference (java/kt/py/js are
  fine, their helpers live in the runtime); a bot that needs a
  database today uses a `store`. On dart the program must run with
  `dart run`, not plain `dart <file>`, so the sqlite3 build hook
  downloads the native libsqlite3. On java/kt the user supplies the
  sqlite-jdbc jar on the runtime classpath (the conformance runner
  downloads it automatically; `newr build` does not).
- M8b `http` plus `json` limits (10 Nov 2026): the runtime
  enforces the default localhost allowlist only; the manifest
  `http_allowlist` is a static checker concern (the CLI does not
  pass it to the emitter). Redirect policy is unpinned across
  targets (java follows redirects like py urllib; the others are
  unverified). `http` inside a bot handler is not in M8b
  (general programs only); `json.encode`/`decode` are pure and
  work anywhere. A JSON float into an int field always throws,
  including an integral 1.0. On js, fetch runs in a subprocess
  (newr JS programs are synchronous top-level code; a proper
  async design is a follow-up) and `http.serve` returns after
  listening instead of blocking (the listening server keeps the
  event loop and the process alive). On this VM, java/kt need
  `java.net.preferIPv4Stack=true` and cs needs an explicit IPv4
  socket with the proxy disabled (the VM mangles
  dual-stack/unspecified-family sockets); each target carries
  its own equivalent workaround.
- UI parity is the hard part. A button will not look identical on all
  seven targets in M8, and the doc does not claim it will. Same layout,
  same behavior, native look per target.
- Packaging needs per-OS toolchains this VM does not have: Xcode for
  signed macOS and iOS artifacts, Android SDK for apk. Unsigned and
  web artifacts are provable here; signed store artifacts need the
  user's machine or CI runners on those OSes.
- No auth, no sessions, no Postgres/MySQL, no realtime (websockets),
  no file uploads in M8. Each is a named later item, not a silent gap.
- There is no browser target in newr: js is a Node target that serves
  HTTP and fetches (M8d eval §5.1, SPEC §22.4), so the "browser target
  cannot serve" shape in earlier drafts does not apply. sql.js plus
  IndexedDB remains the documented shape only if a browser target ever
  lands.

Open questions (to settle in the M8a research, before building):

- Should `db.query` return a list of dicts or a typed struct list?
  SETTLED in M8a (10 Nov 2026): a typed struct list, resolved from
  the expected type at the call site (`let todos: list[Todo] =
  db.query(...)`). Columns match struct fields by name; a missing
  column or a value that cannot convert throws. Never stringified
  dicts, never `list[dict[str, str]]`; the honest fallback (had
  inference failed) would have been a cursor-style API, not dicts.
- Should the frontend fetch go through `http` only, or also a
  generated client from the shared route table? SETTLED in M8b
  (10 Nov 2026): fetch goes through `http` only. A generated
  client from the shared route table is M8d/M9 material, not
  M8b; the M8b example client (`examples/hello_api_client.newr`)
  calls `http.get`/`http.post` directly.
- Which UI toolkit per target is the M8 default where two exist
  (for example WinUI vs MAUI on C#, JavaFX vs Compose on desktop)?
- Single-file full-stack in M9: what syntax marks the split?
