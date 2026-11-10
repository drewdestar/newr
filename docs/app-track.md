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
Ktor or HttpServer, Dart shelf. The frontend target (JS in a browser)
cannot serve; on that target `http.serve` is a checker error, and the
program is split so the server part builds only for server targets
(see section 5).

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

Draft surface:

- `app.manifest(name: str, version: str, ...)` declares the app: name,
  version, icon, permissions, entry point.
- `newr build --target <t> --app` (a CLI flag, not a module call)
  produces the installable artifact for that target.

Packaging per OS: Android apk via the Kotlin or Dart target (needs the
Android SDK); Windows exe/msi via the C# target; macOS dmg via the Dart
target (needs Xcode on a Mac for signing, unsigned otherwise); Linux
AppImage or deb via the Rust or Python target; web via the JS target
plus the M7 playground hosting story. M7 already covers standalone
binaries; M8 adds the manifest, the icon and permission declarations,
and the per-OS artifact step. Signing and store submission (Play,
App Store, Microsoft Store) are explicitly out of M8: they need the
user's own developer accounts.

## 5. Full-stack shape: one source, split by target

A full-stack app in M8 is three programs sharing one contract, not one
file doing everything:

- `shared.newr`: the data shapes (structs) and the route table both
  sides agree on. Imported by the other two.
- `backend.newr`: uses `db` and `http`, serves the routes, talks to
  sqlite. Builds for py, java, cs, rs, kt (server targets).
- `frontend.newr`: uses `http` (fetch) and `ui`, calls the backend,
  renders screens. Builds for js, dart, kt (client targets).

The checker enforces the split: `http.serve` and `db` in a program
built for a browser target is an error with a plain message, and
`ui.show` in a program built for a server-only target is the same.
Shared types are checked once and must match on both sides; a drift
between frontend and backend types is a build error, not a runtime
surprise.

A single-file full-stack program (backend plus frontend in one file,
compiler splits it) is the M9 goal, not M8. M8 proves the three-file
shape end to end first.

## 6. Per-target map (summary)

| Target | db | http serve | http fetch | ui | app package |
|---|---|---|---|---|---|
| py | sqlite3 | yes | yes | tkinter 8.6 | AppImage, deb |
| js | sql.js / IndexedDB | no (checker error) | yes | HTML sink (render only) | web |
| java | sqlite-jdbc | yes | yes | Swing | jar, deb |
| cs | Microsoft.Data.Sqlite | yes | yes | Avalonia 12.1.6 | exe, msi |
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
- M8d `app` plus full-stack example: manifest, packaging flags, then
  the three-file shop app (shared types, backend with db and http,
  frontend with ui), verified live on at least one server target and
  one client target.

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
- The browser target cannot serve HTTP or write a plain sqlite file;
  sql.js plus IndexedDB is the documented shape there.

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
