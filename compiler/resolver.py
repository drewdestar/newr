"""Resolver for the newr language (SPEC.md §11, §17.1).

Walks a parsed Program and resolves names: imports (cycle-checked,
relative to the importing file), duplicate top-level names, module
visibility (``pub``), reserved names (builtins, handler bindings, ``fn
t``), and name existence (E3001) with rustc-style similar-name help.
Records usage for the W1001 (unused variable/parameter) and W1002
(unused import) warnings.

The resolver does NOT type anything (that is the checker's job) and does
NOT annotate nodes: the checker re-walks scopes itself. The resolver
fills in ``Import.resolved_path``, ``Program.files`` (main file first,
then imports in resolution order) and ``Program.modules`` (bound name ->
resolved Program of each imported file module).

Ownership decisions (see BUILD_REPORT.md):
- E2005 (use before definition) is raised here, not by the checker:
  name resolution is where scope order is known.
- E2005 applies to LOCAL variables used before their ``let`` in the same
  block, to a ``let``/``const`` whose RHS refers to itself (even at
  top level), and (round-2 BUG-2) to a top-level STATEMENT that uses a
  top-level ``let``/``const`` before its declaration statement
  (SPEC.md SS8: top-level statements execute in file order). Fn,
  struct, enum, and store names are still hoisted, and fn/lambda/
  handler bodies are deferred execution, so they may forward-reference
  a later top-level let.
- Assignment-target validation lives here: only ``var`` / ``param`` /
  ``option`` bindings are assignable; assigning to a fn/struct/enum/
  module/store/const binding is E3001 ("cannot assign to ...").
- Type names (``x: Nosuch``) are validated by the CHECKER, which owns
  the type environment — not here.
- A single-match similar-name suggestion counts as a use of the
  suggested binding: ``let count = 1`` + ``print coun`` is one E3001,
  not E3001 plus a spurious W1001 for ``count``.

Error codes (SPEC.md §16 starter set + M1 additions):
E3001 undefined name; E2005 use before definition; E3003 duplicate
definition; E3004 import cycle; E3005 name not exported (``pub``);
E3006 bot/command/event/task/top-level statement in an imported file;
E3007 reserved name shadowed; E3008 unknown module / missing file.
Warnings: W1001 unused variable/parameter/option; W1002 unused import;
W1004 shadowing.

No POSIX-only code: paths go through pathlib; file reads use
``encoding="utf-8"``.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass, field
from pathlib import Path

from . import ast_nodes as A
from .errors import CompilerResult, Diagnostic, Fix, NewrError, Span, Suggestion
from .errors import MAX_NESTING_DEPTH
from .modules import BUILTINS, MODULES, RESERVED_BUILTIN_NAMES

# ---------------------------------------------------------------- constants

#: Names bound only inside handler bodies (SPEC.md §13.5). A user variable
#: may not shadow them in handler scope; a function parameter may not be
#: named after them anywhere (SPEC.md §9.5).
RESERVED_HANDLER_BINDINGS = {
    "user", "guild", "channel", "bot", "interaction", "message", "values",
}

#: Named (stdlib / domain) modules available in v1 (SPEC.md §11, §12).
NAMED_MODULES = {"math", "time", "random", "file", "env", "discord", "db",
                 "http", "json", "ui"}

#: Handler bindings per context (SPEC.md §13.4–§13.5). Select handlers add
#: ``values``; modal handlers add their field names.
HANDLER_BINDINGS: dict[str, set[str]] = {
    "command": {"user", "guild", "channel", "interaction"},
    "member_join": {"user", "guild"},
    "member_remove": {"user", "guild"},
    "ready": {"bot"},
    "message": {"message", "user"},
    "component": {"user", "guild", "channel", "interaction"},
}

#: difflib similarity gate for similar-name help (SPEC.md §16). The v0
#: gate was 0.6, but 0.6 admits false positives once builtins join the
#: candidate pool (``coun`` matches ``round`` at 0.667 as well as
#: ``count`` at 0.889, so no single suggestion fires); 0.7 keeps genuine
#: typos and drops the false positives. Documented and tested, not
#: claimed as "standard".
SIMILARITY_CUTOFF = 0.7


# ---------------------------------------------------------------- scopes


@dataclass
class _Binding:
    kind: str  # var | param | const | option | fn | struct | enum |
    #            module | store | handler | builtin
    name: str
    span: Span
    node: object | None = None
    used: bool = False
    pub: bool = False


@dataclass
class _Scope:
    kind: str  # module | function | block | handler
    parent: _Scope | None = None
    bindings: dict[str, _Binding] = field(default_factory=dict)

    def lookup(self, name: str) -> _Binding | None:
        scope: _Scope | None = self
        while scope is not None:
            if name in scope.bindings:
                return scope.bindings[name]
            scope = scope.parent
        return None

    def lookup_local(self, name: str) -> _Binding | None:
        return self.bindings.get(name)


@dataclass
class _ImportRecord:
    node: A.Import
    bound_name: str
    used: bool = False
    is_named: bool = False


@dataclass
class _ModuleInfo:
    program: A.Program
    path: Path | None  # None for a sourceless program ("<string>")
    bound_name: str | None  # None for the main file
    scope: _Scope
    imports: list[_ImportRecord] = field(default_factory=list)
    bindings: list[_Binding] = field(default_factory=list)


# ---------------------------------------------------------------- resolver


class Resolver:
    def __init__(self, main_path: Path | None):
        self.main_path = main_path
        self.diagnostics: list[Diagnostic] = []
        self.files: list[str] = []
        self._resolving: list[Path] = []  # cycle-detection stack
        self._resolved_files: dict[Path, _ModuleInfo] = {}
        self._later_stack: list[dict[str, A.Stmt]] = []
        self._current_info: _ModuleInfo | None = None
        # Top-level let/const whose declaration statement has NOT been
        # walked yet (round-2 BUG-2, SPEC.md SS8: top-level statements
        # execute in file order). Set around the top-level statement
        # walk in _resolve_module, None everywhere else — fn/lambda/
        # handler bodies are deferred execution and never see it.
        self._top_level_pending: dict[str, A.Stmt] | None = None
        # Transitive top-level-let effect analysis (M2 fix #3): while
        # a module fn is being resolved, record which module-scope
        # lets it reads / writes directly and which module fns it
        # calls. After all fns are resolved the transitive closure
        # over the call graph is computed into _fn_top_reads /
        # _fn_top_writes; a top-level statement that calls such a
        # fn while one of those lets is still pending is E2005.
        self._collecting_fn: str | None = None
        self._lambda_depth: int = 0
        self._fn_direct_reads: dict[str, set[str]] = {}
        self._fn_direct_writes: dict[str, set[str]] = {}
        self._fn_direct_calls: dict[str, set[str]] = {}
        self._fn_top_reads: dict[str, set[str]] = {}
        self._fn_top_writes: dict[str, set[str]] = {}
        # (id(scope), name) pairs already reported for E3007, so a
        # top-level let pre-declared by _collect_top_level and re-seen
        # by the statement walk is not reported twice.
        self._reserved_reported: set[tuple[int, str]] = set()
        # Nesting depth of the _walk_expr/_walk_stmt recursion (M-01):
        # past MAX_NESTING_DEPTH the walk stops with E1001 instead of
        # crashing with RecursionError.
        self._depth = 0
        self._depth_reported = False
        # Resolved paths that took part in a reported import cycle
        # (E3004): their imports are not also warned as unused (W1002)
        # and their imported-file restrictions are not re-reported
        # (E3006) — both would be noise on top of the cycle error.
        self._cycle_paths: set[Path] = set()

    # ------------------------------------------------------------ diagnostics

    def _error(self, span: Span, code: str, message: str,
               help: str | None = None, note: str | None = None,
               suggestion: Suggestion | None = None,
               fix: Fix | None = None) -> None:
        self.diagnostics.append(Diagnostic(
            span=span, code=code, message=message, severity="error",
            help=help, note=note, suggestion=suggestion, fix=fix))

    def _warning(self, span: Span, code: str, message: str,
                 help: str | None = None) -> None:
        self.diagnostics.append(Diagnostic(
            span=span, code=code, message=message, severity="warning",
            help=help))

    def _similar_name_help(self, span: Span, name: str,
                           candidates: list[tuple[str, str]],
                           scope: _Scope | None = None) -> None:
        """E3001 with a rustc named-form similar-name help.

        ``candidates`` is a list of (name, kind-word) pairs. Exactly one
        close match -> help + a certain suggestion + a fix (a pure
        rename), and the matched binding counts as used (the user meant
        it — the misspelling is the use). Several close matches -> help
        listing them, no fix.
        """
        names = [c[0] for c in candidates]
        close = difflib.get_close_matches(name, names, n=3,
                                          cutoff=SIMILARITY_CUTOFF)
        if len(close) == 1:
            match = close[0]
            kind = dict(candidates)[match]
            if scope is not None:
                found = scope.lookup(match)
                if found is not None and found.kind != "module":
                    found.used = True
            suggestion = Suggestion(span=span, replacement=match,
                                    applicability="certain")
            self._error(
                span, "E3001", f"undefined name `{name}`",
                help=(f"there is a {kind} with a similar name: "
                      f"`{match}`"),
                suggestion=suggestion,
                fix=Fix(span=span, replacement=match))
        elif close:
            listed = ", ".join(f"`{c}`" for c in close)
            self._error(span, "E3001", f"undefined name `{name}`",
                        help=f"there are similar names: {listed}")
        else:
            self._error(span, "E3001", f"undefined name `{name}`")

    def _visible_names(self, scope: _Scope) -> list[tuple[str, str]]:
        """(name, kind-word) pairs visible from ``scope`` + builtins."""
        out: list[tuple[str, str]] = []
        seen: set[str] = set()
        s: _Scope | None = scope
        while s is not None:
            for name, b in s.bindings.items():
                if name not in seen:
                    seen.add(name)
                    out.append((name, _KIND_WORDS.get(b.kind, "name")))
            s = s.parent
        for name in sorted(RESERVED_BUILTIN_NAMES):
            if name not in seen:
                out.append((name, "builtin"))
        return out

    # ------------------------------------------------------------ entry points

    def resolve_program(self, program: A.Program,
                        main_path: Path | None) -> CompilerResult:
        info = self._resolve_module(program, main_path, is_main=True)
        program.files = self.files
        # program.modules was filled by _resolve_module (for every module,
        # not just the main file — nested imports included).
        return CompilerResult(program=program,
                              diagnostics=self.diagnostics)

    # ------------------------------------------------------------ modules

    def _resolve_module(self, program: A.Program, path: Path | None,
                        is_main: bool,
                        bound_name: str | None = None) -> _ModuleInfo:
        scope = _Scope("module")
        info = _ModuleInfo(program=program, path=path,
                           bound_name=bound_name, scope=scope)
        if path is not None:
            self.files.append(str(path))
        prev = self._current_info
        self._current_info = info
        pushed = False
        if path is not None:
            self._resolving.append(path)
            pushed = True
        try:
            diag_before = len(self.diagnostics)
            for imp in program.imports:
                self._resolve_import(info, imp)
            if not is_main:
                # N-05: an import cycle (E3004) already says what is
                # wrong with this file; the imported-file restrictions
                # (E3006) on top would be follow-on noise.
                cycle = any(d.code == "E3004"
                            for d in self.diagnostics[diag_before:])
                if not cycle:
                    self._check_imported_file_restrictions(program)
            self._collect_top_level(info, program)
            self._fn_direct_reads = {}
            self._fn_direct_writes = {}
            self._fn_direct_calls = {}
            for fn in program.functions:
                self._resolve_fn(info, fn)
            self._compute_fn_top_level_effects()
            # Round-2 BUG-2: top-level lets/consts are "pending" until
            # their declaration statement has been walked. A top-level
            # statement that uses one before that point is E2005
            # (SPEC.md SS8: top-level statements execute in file
            # order). Fn bodies were already resolved above and
            # commands/events/tasks resolve below, so deferred
            # execution never sees a pending map.
            prev_pending = self._top_level_pending
            self._top_level_pending = {
                st.name: st for st in program.stmts
                if isinstance(st, (A.Let, A.Const))}
            try:
                self._walk_stmts(program.stmts, scope)
            finally:
                self._top_level_pending = prev_pending
            for cmd in program.commands:
                self._resolve_command(info, cmd)
            for ev in program.events:
                self._resolve_event(info, ev)
            for task in program.tasks:
                self._resolve_task(info, task)
            self._emit_usage_warnings(info)
            self._build_modules(info)
        finally:
            if pushed:
                self._resolving.pop()
            self._current_info = prev
        return info

    def _build_modules(self, info: _ModuleInfo) -> None:
        """Fill ``program.modules`` for this module (bound name -> Program).

        Every resolved module gets its own map — not just the main file —
        so the checker can follow nested file imports (a -> b -> c)
        instead of seeing only the main file's imports.
        """
        info.program.modules = {
            rec.bound_name: self._resolved_files[
                Path(rec.node.resolved_path)].program  # type: ignore[arg-type]
            for rec in info.imports
            if not rec.is_named and rec.node.resolved_path is not None
            and Path(rec.node.resolved_path) in self._resolved_files
        }

    def _check_imported_file_restrictions(self, program: A.Program) -> None:
        if program.bot is not None:
            self._error(program.bot.span, "E3006",
                        "a `bot` block must live in the main file",
                        help="move the bot block to the file passed to "
                             "`newr build`")
        for cmd in program.commands:
            self._error(cmd.span, "E3006",
                        f"command `{cmd.name}` must live in the main file",
                        help="imported files contribute functions, structs, "
                             "enums, strings, and store tables only")
        for ev in program.events:
            self._error(ev.span, "E3006",
                        f"event `{ev.kind}` must live in the main file")
        for task in program.tasks:
            self._error(task.span, "E3006",
                        "`task` must live in the main file")
        for st in program.stmts:
            # Top-level let/const are declarations (exportable via pub),
            # not statements — only other statements are forbidden here.
            if isinstance(st, (A.Let, A.Const)):
                continue
            self._error(st.span, "E3006",
                        "top-level statements are not allowed in an "
                        "imported file",
                        help="move the statement into a function")

    def _resolve_import(self, info: _ModuleInfo, imp: A.Import) -> None:
        bound = imp.bound_name
        if imp.module is not None:
            if imp.module not in NAMED_MODULES:
                known = ", ".join(sorted(NAMED_MODULES))
                self._error(imp.span, "E3008",
                            f"unknown module `{imp.module}`",
                            help=f"known modules in v1: {known}")
            self._record_import(info, imp, bound, is_named=True)
            return
        # File module, resolved relative to the importing file.
        if info.path is None:
            self._error(imp.span, "E3008",
                        f"cannot resolve import `{imp.path}` from a program "
                        "with no file",
                        help="save the program to a file first, or pass a "
                             "file to `newr build`")
            self._record_import(info, imp, bound, is_named=False)
            return
        target = (info.path.parent / imp.path).resolve()
        if not target.is_file():
            # N-05: if a file with the same name in a different case
            # exists on disk, say so — imports are case-sensitive and
            # the generic "cannot find" hides the real problem.
            help = f"looked for {target}"
            parent = target.parent
            if parent.is_dir():
                for cand in parent.iterdir():
                    if cand.name.lower() == target.name.lower():
                        help = (f"there is a file named `{cand.name}` "
                                f"in {parent} — imports are case-"
                                "sensitive (SPEC.md §3.3)")
                        break
            self._error(imp.span, "E3008",
                        f"cannot find module file `{imp.path}`",
                        help=help)
            self._record_import(info, imp, bound, is_named=False)
            return
        if target in self._resolving:
            chain = [p.name for p in self._resolving] + [target.name]
            self._error(imp.span, "E3004",
                        "import cycle: " + " -> ".join(chain),
                        help="break the cycle by moving the shared "
                             "declarations into a third module")
            self._cycle_paths.update(self._resolving)
            self._cycle_paths.add(target)
            self._record_import(info, imp, bound, is_named=False)
            return
        self._record_import(info, imp, bound, is_named=False)
        imp.resolved_path = str(target)
        if target in self._resolved_files:
            return  # already resolved (imported twice under two names)
        from .parser import parse  # lazy: the parser may not exist yet
        try:
            content = target.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            self._error(imp.span, "E3008",
                        f"cannot read module file `{imp.path}`: not "
                        "valid UTF-8 (SPEC.md §3.1 requires UTF-8)")
            return
        except OSError as exc:
            self._error(imp.span, "E3008",
                        f"cannot read module file `{imp.path}`: {exc}")
            return
        try:
            sub_program = parse(content, str(target))
        except NewrError as exc:
            self.diagnostics.append(exc.diagnostic)
            return
        sub_info = self._resolve_module(sub_program, target,
                                        is_main=False,
                                        bound_name=bound)
        self._resolved_files[target] = sub_info

    def _record_import(self, info: _ModuleInfo, imp: A.Import,
                       bound: str, is_named: bool) -> None:
        for rec in info.imports:
            if rec.bound_name == bound:
                self._error(imp.span, "E3003",
                            f"duplicate import of `{bound}`",
                            help="each import must bind a different name")
                return
        info.imports.append(_ImportRecord(node=imp, bound_name=bound,
                                          is_named=is_named))
        # The bound name is a module binding in the module scope.
        if info.scope.lookup_local(bound) is not None:
            return  # a top-level declaration will report the clash (E3003)
        binding = _Binding(kind="module", name=bound, span=imp.span,
                           node=imp)
        info.scope.bindings[bound] = binding
        info.bindings.append(binding)

    # ------------------------------------------------------------ top level

    def _collect_top_level(self, info: _ModuleInfo,
                           program: A.Program) -> None:
        scope = info.scope
        for fn in program.functions:
            # A test block's synthetic function (`_newr_test_<n>`,
            # SPEC.md section 8.7) lives in the emitter's reserved
            # `_newr_` namespace on purpose, so it is not declared
            # here: `_declare` would reject the prefix (E3001).
            # The name is only ever called by the test harness,
            # which the checker resolves from its own function map.
            if fn.name.startswith("_newr_test_"):
                continue
            self._declare(scope, "fn", fn.name, fn.span, fn, pub=fn.pub)
        for st in program.structs:
            self._declare(scope, "struct", st.name, st.span, st,
                          pub=st.pub)
            seen: set[str] = set()
            for f in st.fields:
                if f.name in seen:
                    self._error(f.span, "E3003",
                                f"duplicate field `{f.name}` in struct "
                                f"`{st.name}`")
                seen.add(f.name)
        for en in program.enums:
            self._declare(scope, "enum", en.name, en.span, en, pub=en.pub)
            seen = set()
            for v in en.variants:
                if v.name in seen:
                    self._error(v.span or en.span, "E3003",
                                f"duplicate variant `{v.name}` in enum "
                                f"`{en.name}`")
                seen.add(v.name)
        # Top-level let/const are pre-declared so fn bodies (walked
        # before the statements) and other declarations may
        # forward-reference them. Top-level STATEMENTS may not: the
        # statement walk tracks which lets have been declared so far
        # (_top_level_pending) and a use before that point is E2005
        # (SPEC.md SS8, round-2 BUG-2). The walk hides a let's own
        # binding while checking its RHS (self-reference is E2005).
        for st in program.stmts:
            if isinstance(st, (A.Let, A.Const)):
                kind = "const" if isinstance(st, A.Const) else "var"
                self._declare(scope, kind, st.name,
                              st.name_span or st.span, st, pub=st.pub)
        for store in program.stores:
            self._declare(scope, "store", store.name, store.span, store)

    def _declare(self, scope: _Scope, kind: str, name: str, span: Span,
                 node: object | None = None,
                 pub: bool = False,
                 allow_builtin: bool = False) -> _Binding | None:
        # ``allow_builtin`` is only for catch variables: SPEC.md §10's own
        # example is ``catch err:``, and ``err`` is a builtin name. A
        # catch variable is a transient error binding, not a redefinition
        # of the builtin, so it may shadow one. Every other declaration
        # still gets E3007 for a builtin name.
        if name in RESERVED_BUILTIN_NAMES and not allow_builtin:
            key = (id(scope), name)
            if key not in self._reserved_reported:
                self._reserved_reported.add(key)
                self._error(span, "E3007",
                            f"`{name}` is a builtin name and cannot be "
                            "redefined",
                            help="rename the declaration")
            return None
        # The emitter's temporaries live in the `_newr_` namespace
        # (_newr_t0, _newr_result, ...): a user name with that prefix
        # would collide with them (M2 evaluation nice-to-have G-M2-4).
        if name.startswith("_newr_"):
            key = (id(scope), name)
            if key not in self._reserved_reported:
                self._reserved_reported.add(key)
                self._error(span, "E3001",
                            f"`{name}` uses the reserved `_newr_` prefix",
                            help="rename it — `_newr_*` is the emitter's "
                                 "temporary namespace")
            return None
        if (kind in ("var", "param", "const", "option")
                and name in RESERVED_HANDLER_BINDINGS
                and (kind == "param" or self._in_handler(scope))):
            key = (id(scope), name)
            if key not in self._reserved_reported:
                self._reserved_reported.add(key)
                self._error(span, "E3007",
                            f"`{name}` is a reserved handler binding",
                            help="rename it — handler bindings (`user`, "
                                 "`guild`, `channel`, `bot`, `interaction`, "
                                 "`message`, `values`) cannot be shadowed")
            return None
        if name in scope.bindings:
            self._error(span, "E3003",
                        f"duplicate definition of `{name}`",
                        note="the first definition is earlier in this file")
            return scope.bindings[name]
        outer = scope.parent
        while outer is not None:
            if name in outer.bindings:
                self._warning(span, "W1004",
                              f"`{name}` shadows a name from an outer scope",
                              help="rename it if the shadowing is not "
                                   "intentional")
                break
            outer = outer.parent
        binding = _Binding(kind=kind, name=name, span=span, node=node,
                           pub=pub)
        scope.bindings[name] = binding
        if self._current_info is not None:
            self._current_info.bindings.append(binding)
        return binding

    def _in_handler(self, scope: _Scope) -> bool:
        s: _Scope | None = scope
        while s is not None:
            if s.kind == "handler":
                return True
            if s.kind in ("module", "function"):
                return False
            s = s.parent
        return False

    # ------------------------------------------------------------ bodies

    def _resolve_fn(self, info: _ModuleInfo, fn: A.FnDecl) -> None:
        scope = _Scope("function", parent=info.scope)
        for p in fn.params:
            binding = self._declare(scope, "param", p.name, p.span, p)
            # A pub function's parameters are its public contract: an
            # unused one is an API choice, not a bug signal, so it does
            # not earn W1001 (SPEC.md §5.3).
            if fn.pub and binding is not None:
                binding.used = True
        # M2 fix #3: record the top-level lets this fn reads/writes
        # and the module fns it calls (for the transitive E2005
        # analysis). Reads/writes inside a nested lambda are NOT
        # recorded: a lambda body may escape and run later.
        prev_fn = self._collecting_fn
        prev_depth = self._lambda_depth
        self._collecting_fn = fn.name
        self._lambda_depth = 0
        self._fn_direct_reads[fn.name] = set()
        self._fn_direct_writes[fn.name] = set()
        self._fn_direct_calls[fn.name] = set()
        try:
            self._walk_stmts(fn.body, scope)
        finally:
            self._collecting_fn = prev_fn
            self._lambda_depth = prev_depth

    def _is_module_let(self, name: str, binding: _Binding) -> bool:
        """True if ``binding`` is a top-level let/const of the module
        currently being resolved. The ``lookup_local`` identity
        check excludes a local that shadows the top-level name."""
        info = self._current_info
        if info is None or binding.kind not in ("var", "const"):
            return False
        return info.scope.lookup_local(name) is binding

    def _record_fn_read(self, name: str, binding: _Binding) -> None:
        if (self._collecting_fn is None or self._lambda_depth > 0
                or not self._is_module_let(name, binding)):
            return
        self._fn_direct_reads[self._collecting_fn].add(name)

    def _record_fn_call(self, name: str) -> None:
        if self._collecting_fn is None or self._lambda_depth > 0:
            return
        info = self._current_info
        if info is None:
            return
        binding = info.scope.lookup_local(name)
        if binding is None or binding.kind != "fn":
            return
        self._fn_direct_calls[self._collecting_fn].add(name)

    def _compute_fn_top_level_effects(self) -> None:
        """Transitive closure over the call graph: for each module
        fn, the top-level lets it reads / writes (directly, or via
        another module fn it calls). Fixpoint iteration handles
        mutual recursion."""
        reads = {name: set(s)
                 for name, s in self._fn_direct_reads.items()}
        writes = {name: set(s)
                  for name, s in self._fn_direct_writes.items()}
        changed = True
        while changed:
            changed = False
            for fn, callees in self._fn_direct_calls.items():
                for callee in callees:
                    for target in (reads, writes):
                        before = len(target[fn])
                        target[fn] |= target.get(callee, set())
                        if len(target[fn]) != before:
                            changed = True
        self._fn_top_reads = reads
        self._fn_top_writes = writes

    def _check_fn_call_before_let(self, expr: A.Call,
                                  scope: _Scope) -> None:
        """A top-level statement that calls a module fn which
        (transitively) reads or writes a still-pending top-level
        let is E2005: the call executes in file order, before the
        let exists, and would die at runtime with a raw NameError
        (M2 evaluation #3, G-M2-3)."""
        if self._top_level_pending is None:
            return
        if self._in_deferred_scope(scope):
            return
        if not isinstance(expr.func, A.Name):
            return
        name = expr.func.name
        info = self._current_info
        if info is None:
            return
        binding = info.scope.lookup_local(name)
        if binding is None or binding.kind != "fn":
            return
        pending = set(self._top_level_pending)
        bad_reads = sorted(self._fn_top_reads.get(name, set()) & pending)
        bad_writes = sorted(self._fn_top_writes.get(name, set()) & pending)
        if not bad_reads and not bad_writes:
            return
        if bad_reads:
            target, verb = bad_reads[0], "reads"
        else:
            target, verb = bad_writes[0], "assigns to"
        self._error(expr.func.span, "E2005",
                    f"function `{name}` {verb} `{target}` before its "
                    "definition",
                    help="move the `let` above the call, or move the "
                         "call after the `let`")

    def _handler_scope(self, info: _ModuleInfo, names: set[str],
                       extra: dict[str, Span] | None = None) -> _Scope:
        scope = _Scope("handler", parent=info.scope)
        for name in sorted(names):
            scope.bindings[name] = _Binding(kind="handler", name=name,
                                            span=info.program.span)
        for name, span in (extra or {}).items():
            self._declare(scope, "option", name, span, None)
        return scope

    def _resolve_command(self, info: _ModuleInfo, cmd: A.CommandDecl) -> None:
        scope = self._handler_scope(info, HANDLER_BINDINGS["command"])
        seen: set[str] = set()
        for opt in cmd.options:
            if opt.name in seen:
                self._error(opt.name_span or opt.span, "E3003",
                            f"duplicate option `{opt.name}` in command "
                            f"`{cmd.name}`")
            seen.add(opt.name)
            self._declare(scope, "option", opt.name,
                          opt.name_span or opt.span, opt)
            for expr in (opt.default, opt.min_value, opt.max_value):
                if expr is not None:
                    self._walk_expr(expr, info.scope)
            for choice in opt.choices or []:
                self._walk_expr(choice, info.scope)
        self._walk_stmts(cmd.effective_body(), scope)

    def _resolve_event(self, info: _ModuleInfo, ev: A.EventDecl) -> None:
        names = HANDLER_BINDINGS.get(ev.kind, set())
        scope = self._handler_scope(info, names)
        self._walk_stmts(ev.effective_body(), scope)

    def _resolve_task(self, info: _ModuleInfo, task: A.TaskDecl) -> None:
        scope = _Scope("block", parent=info.scope)
        self._walk_stmts(task.body, scope)

    def _emit_usage_warnings(self, info: _ModuleInfo) -> None:
        for b in info.bindings:
            if b.kind in ("var", "param", "const", "option") and not b.used \
                    and not b.pub:
                self._warning(b.span, "W1001",
                              f"unused {b.kind} `{b.name}`",
                              help="remove it, or use it — an unused "
                                   "binding is almost always a bug")
        for rec in info.imports:
            if rec.used:
                continue
            if rec.is_named and rec.bound_name == "discord" \
                    and (info.program.bot is not None
                         or info.program.commands or info.program.events
                         or info.program.tasks or info.program.strings
                         or info.program.stores):
                continue  # bot blocks implicitly import discord
            # N-05: an import that failed (E3004 cycle / E3008 missing
            # file) or took part in a cycle is not also "unused" — the
            # error already says what is wrong with it.
            if any(d.code in ("E3004", "E3008")
                   and d.span == rec.node.span for d in self.diagnostics):
                continue
            if not rec.is_named and info.path is not None:
                target = (info.path.parent / rec.node.path).resolve()
                if target in self._cycle_paths:
                    continue
            self._warning(rec.node.span, "W1002",
                          f"unused import `{rec.bound_name}`",
                          help="remove the import")

    # ------------------------------------------------------------ statements

    def _walk_stmts(self, stmts: list[A.Stmt], scope: _Scope) -> None:
        later = {st.name: st for st in stmts
                 if isinstance(st, (A.Let, A.Const))}
        self._later_stack.append(later)
        try:
            for st in stmts:
                self._walk_stmt(st, scope)
        finally:
            self._later_stack.pop()

    def _in_later(self, name: str) -> bool:
        return any(name in later for later in self._later_stack)

    def _walk_stmt(self, st: A.Stmt, scope: _Scope) -> None:
        # M-01: stop deep nesting with E1001 instead of RecursionError.
        self._depth += 1
        if self._depth > MAX_NESTING_DEPTH:
            self._depth -= 1
            if not self._depth_reported:
                self._depth_reported = True
                self._error(st.span, "E1001", "too deeply nested",
                            help="split the block into smaller pieces — "
                                 "newr limits nesting to 200 levels so a "
                                 "bad input cannot exhaust the stack")
            return
        try:
            self._walk_stmt_inner(st, scope)
        finally:
            self._depth -= 1

    def _walk_stmt_inner(self, st: A.Stmt, scope: _Scope) -> None:
        if isinstance(st, A.Let):
            self._walk_let_const(st, scope, kind="var")
        elif isinstance(st, A.Const):
            self._walk_let_const(st, scope, kind="const")
        elif isinstance(st, A.Assign):
            self._resolve_assign_target(st, scope)
            self._walk_expr(st.value, scope)
        elif isinstance(st, A.If):
            self._walk_expr(st.cond, scope)
            self._walk_stmts(st.then_body, _Scope("block", parent=scope))
            for cond, body in st.elifs:
                self._walk_expr(cond, scope)
                self._walk_stmts(body, _Scope("block", parent=scope))
            if st.else_body is not None:
                self._walk_stmts(st.else_body,
                                 _Scope("block", parent=scope))
        elif isinstance(st, A.For):
            self._walk_expr(st.iterable, scope)
            body_scope = _Scope("block", parent=scope)
            self._declare(body_scope, "var", st.var,
                          st.var_span or st.span, st)
            self._walk_stmts(st.body, body_scope)
        elif isinstance(st, A.While):
            self._walk_expr(st.cond, scope)
            self._walk_stmts(st.body, _Scope("block", parent=scope))
        elif isinstance(st, A.Try):
            self._walk_stmts(st.body, _Scope("block", parent=scope))
            for catch_name, catch_span, catch_body in st.catches:
                catch_scope = _Scope("block", parent=scope)
                self._declare(catch_scope, "var", catch_name, catch_span,
                              st, allow_builtin=True)
                self._walk_stmts(catch_body, catch_scope)
            if st.finally_body is not None:
                self._walk_stmts(st.finally_body,
                                 _Scope("block", parent=scope))
        elif isinstance(st, A.Throw):
            self._walk_expr(st.value, scope)
        elif isinstance(st, A.Return):
            if st.value is not None:
                self._walk_expr(st.value, scope)
        elif isinstance(st, (A.Break, A.Continue, A.Pass)):
            pass
        elif isinstance(st, A.Match):
            self._walk_expr(st.scrutinee, scope)
            for arm in st.arms:
                arm_scope = _Scope("block", parent=scope)
                if arm.pattern is not None:
                    self._declare_pattern(arm.pattern, arm_scope)
                if arm.guard is not None:
                    self._walk_expr(arm.guard, arm_scope)
                self._walk_stmts(arm.body, arm_scope)
        elif isinstance(st, A.Require):
            self._walk_expr(st.cond, scope)
        elif isinstance(st, A.Ensure):
            # M-04 (SPEC.md SS9.7): `result` is bound inside an
            # `ensures` condition, with the function's return type —
            # the verbatim spec example `ensures result >= 0` must
            # resolve. `requires` does not bind it.
            ens_scope = _Scope("block", parent=scope)
            ens_scope.bindings["result"] = _Binding(
                kind="var", name="result", span=st.span, node=st)
            self._walk_expr(st.cond, ens_scope)
        elif isinstance(st, (A.Reply, A.Edit, A.Followup, A.Log)):
            if st.value is not None:
                self._walk_expr(st.value, scope)
        elif isinstance(st, A.Defer):
            pass
        elif isinstance(st, A.Send):
            self._walk_expr(st.value, scope)
            if st.channel is not None:
                self._walk_expr(st.channel, scope)
        elif isinstance(st, A.ExprStmt):
            self._walk_expr(st.value, scope)
        elif isinstance(st, A.ButtonDecl):
            self._resolve_component(st, scope)
        elif isinstance(st, A.SelectDecl):
            self._resolve_component(st, scope,
                                    extra={"values": st.span})
        elif isinstance(st, A.ModalDecl):
            extra = {f.name: f.span for f in st.fields}
            self._resolve_component(st, scope, extra=extra)
        else:  # pragma: no cover - the parser only builds known nodes
            raise AssertionError(f"unknown statement node: {type(st)!r}")

    def _walk_let_const(self, st: A.Let | A.Const, scope: _Scope,
                        kind: str) -> None:
        existing = scope.lookup_local(st.name)
        if existing is not None and existing.node is st:
            # Pre-declared by _collect_top_level (top-level let/const).
            # Hide it while walking the RHS so a self-reference is
            # E2005, then restore it. No second declaration.
            del scope.bindings[st.name]
            try:
                self._walk_expr(st.value, scope)
            finally:
                scope.bindings[st.name] = existing
        elif existing is not None and scope.kind == "module":
            # A duplicate already reported by _collect_top_level.
            self._walk_expr(st.value, scope)
        else:
            self._walk_expr(st.value, scope)
            self._declare(scope, kind, st.name,
                          st.name_span or st.span, st, pub=st.pub)
        if scope.kind == "module" and self._top_level_pending is not None:
            # The declaration statement has now been walked: later
            # top-level statements may use this name (round-2 BUG-2).
            self._top_level_pending.pop(st.name, None)

    def _resolve_component(self, comp, scope: _Scope,
                           extra: dict[str, Span] | None = None) -> None:
        comp_scope = _Scope("handler", parent=scope)
        for name in sorted(HANDLER_BINDINGS["component"]):
            comp_scope.bindings[name] = _Binding(
                kind="handler", name=name, span=comp.span)
        # Select `values` / modal field names are handler bindings, not
        # options: a handler that ignores them is not "unused".
        for name, span in (extra or {}).items():
            comp_scope.bindings[name] = _Binding(
                kind="handler", name=name, span=span)
        self._walk_stmts(comp.body, comp_scope)
        if comp.on_timeout is not None:
            self._walk_stmts(comp.on_timeout, comp_scope)

    def _resolve_assign_target(self, st: A.Assign, scope: _Scope) -> None:
        binding = scope.lookup(st.name)
        if binding is not None and self._is_pending_top_level(
                st.name, binding, scope):
            self._error(st.name_span or st.span, "E2005",
                        f"use of `{st.name}` before its definition",
                        help="move the `let` above the use, or rename "
                             "one of them")
            return
        if binding is None:
            if st.name in RESERVED_BUILTIN_NAMES:
                self._error(st.name_span or st.span, "E3001",
                            f"cannot assign to builtin `{st.name}`")
            elif self._in_later(st.name):
                self._error(st.name_span or st.span, "E2005",
                            f"use of `{st.name}` before its definition")
            else:
                self._similar_name_help(st.name_span or st.span, st.name,
                                        self._visible_names(scope),
                                        scope=scope)
            return
        if binding.kind not in ("var", "param", "option"):
            self._error(st.name_span or st.span, "E3001",
                        f"cannot assign to `{st.name}`",
                        help=f"`{st.name}` is a {_KIND_WORDS.get(binding.kind, 'name')}, "
                             "not a variable")
            return
        binding.used = True
        # M2 fix #3: a write to a top-level let is a use of it —
        # record it for the transitive E2005 analysis.
        if (self._collecting_fn is not None and self._lambda_depth == 0
                and self._is_module_let(st.name, binding)):
            self._fn_direct_writes[self._collecting_fn].add(st.name)

    def _declare_pattern(self, pat: A.Pattern, scope: _Scope) -> None:
        if isinstance(pat, A.NamePattern):
            self._declare(scope, "var", pat.name,
                          pat.name_span or pat.span, pat)
        elif isinstance(pat, A.EnumPattern):
            for _field, sub, _span in pat.bindings:
                self._declare_pattern(sub, scope)
        elif isinstance(pat, A.StructPattern):
            for _field, sub, _span in pat.fields:
                self._declare_pattern(sub, scope)
        elif isinstance(pat, A.ListPattern):
            for sub in pat.elements:
                self._declare_pattern(sub, scope)
        elif isinstance(pat, A.OrPattern):
            if pat.patterns:
                self._declare_pattern(pat.patterns[0], scope)
        # LitPattern / WildcardPattern bind nothing.

    # ------------------------------------------------------------ expressions

    def _walk_expr(self, expr: A.Expr | None, scope: _Scope) -> None:
        if expr is None:
            return
        # M-01: stop deep nesting with E1001 instead of RecursionError.
        self._depth += 1
        if self._depth > MAX_NESTING_DEPTH:
            self._depth -= 1
            if not self._depth_reported:
                self._depth_reported = True
                self._error(expr.span, "E1001", "too deeply nested",
                            help="split the expression into smaller pieces "
                                 "— newr limits nesting to 200 levels so a "
                                 "bad input cannot exhaust the stack")
            return
        try:
            self._walk_expr_inner(expr, scope)
        finally:
            self._depth -= 1

    def _walk_expr_inner(self, expr: A.Expr | None,
                         scope: _Scope) -> None:
        if isinstance(expr, A.Lit):
            pass
        elif isinstance(expr, A.Name):
            self._resolve_name(expr, scope)
        elif isinstance(expr, A.BinOp):
            self._walk_expr(expr.left, scope)
            self._walk_expr(expr.right, scope)
        elif isinstance(expr, A.UnaryOp):
            self._walk_expr(expr.operand, scope)
        elif isinstance(expr, A.Ternary):
            self._walk_expr(expr.cond, scope)
            self._walk_expr(expr.then_expr, scope)
            self._walk_expr(expr.else_expr, scope)
        elif isinstance(expr, A.Lambda):
            fn_scope = _Scope("function", parent=scope)
            for p in expr.params:
                self._declare(fn_scope, "param", p.name, p.span, p)
            # M2 fix #3: a lambda body may escape and run later, so
            # its reads/writes are not the enclosing fn's.
            self._lambda_depth += 1
            try:
                if expr.body is not None:
                    self._walk_stmts(expr.body, fn_scope)
                elif expr.expr_body is not None:
                    self._walk_expr(expr.expr_body, fn_scope)
            finally:
                self._lambda_depth -= 1
        elif isinstance(expr, A.TryExpr):
            self._walk_expr(expr.expr, scope)
        elif isinstance(expr, A.Propagate):
            self._walk_expr(expr.expr, scope)
        elif isinstance(expr, A.Call):
            self._walk_expr(expr.func, scope)
            for arg in expr.args:
                self._walk_expr(arg, scope)
            for _name, value, _span in expr.kwargs:
                self._walk_expr(value, scope)
            # M2 fix #3: record the call for the transitive E2005
            # analysis, and reject a top-level call that would read
            # or write a still-pending let.
            if isinstance(expr.func, A.Name):
                self._record_fn_call(expr.func.name)
            self._check_fn_call_before_let(expr, scope)
        elif isinstance(expr, A.TCall):
            for _name, value, _span in expr.args:
                self._walk_expr(value, scope)
        elif isinstance(expr, A.Member):
            self._walk_member(expr, scope)
        elif isinstance(expr, A.Index):
            self._walk_expr(expr.obj, scope)
            self._walk_expr(expr.index, scope)
        elif isinstance(expr, A.Interp):
            for part in expr.parts:
                if part.expr is not None:
                    self._walk_expr(part.expr, scope)
        elif isinstance(expr, A.ListLit):
            for el in expr.elements:
                self._walk_expr(el, scope)
        elif isinstance(expr, A.DictLit):
            for key, value in expr.pairs:
                self._walk_expr(key, scope)
                self._walk_expr(value, scope)
        elif isinstance(expr, A.EmbedExpr):
            for f in expr.fields:
                if f.value is not None:
                    self._walk_expr(f.value, scope)
        else:  # pragma: no cover - the parser only builds known nodes
            raise AssertionError(f"unknown expression node: {type(expr)!r}")

    def _in_deferred_scope(self, scope: _Scope) -> bool:
        """True if ``scope`` is inside a fn/lambda/handler body.

        Those bodies execute later (or on demand), not in file order,
        so they may forward-reference top-level names (round-2 BUG-2).
        """
        s: _Scope | None = scope
        while s is not None:
            if s.kind in ("function", "handler"):
                return True
            if s.kind == "module":
                return False
            s = s.parent
        return False

    def _is_pending_top_level(self, name: str, binding: _Binding,
                              scope: _Scope) -> bool:
        """True if ``binding`` is a top-level let/const whose declaration
        statement has not been walked yet (round-2 BUG-2, SPEC.md SS8).

        Only applies while the top-level statement walk is running
        (``_top_level_pending`` is set) and only to code that executes
        in file order. The ``binding.node is`` check makes sure a local
        that shadows the top-level name is not mistaken for it.
        """
        if self._top_level_pending is None:
            return False
        pending = self._top_level_pending.get(name)
        if pending is None or pending is not binding.node:
            return False
        return not self._in_deferred_scope(scope)

    def _resolve_name(self, node: A.Name, scope: _Scope) -> None:
        binding = scope.lookup(node.name)
        if binding is not None and self._is_pending_top_level(
                node.name, binding, scope):
            self._error(node.span, "E2005",
                        f"use of `{node.name}` before its definition",
                        help="move the `let` above the use, or rename "
                             "one of them")
            return
        if binding is None:
            if node.name in RESERVED_BUILTIN_NAMES:
                return  # builtins resolve without a binding
            if self._in_later(node.name):
                self._error(node.span, "E2005",
                            f"use of `{node.name}` before its definition",
                            help="move the `let` above the use, or rename "
                                 "one of them")
                return
            self._similar_name_help(node.span, node.name,
                                    self._visible_names(scope),
                                    scope=scope)
            return
        if binding.kind == "module":
            self._error(node.span, "E3001",
                        f"module `{node.name}` cannot be used as a value",
                        help="use a qualified name, e.g. "
                             f"`{node.name}.<name>`")
            return
        binding.used = True
        # M2 fix #3: record a read of a top-level let for the
        # transitive E2005 analysis.
        self._record_fn_read(node.name, binding)

    def _walk_member(self, node: A.Member, scope: _Scope) -> None:
        obj = node.obj
        if isinstance(obj, A.Name):
            binding = scope.lookup(obj.name)
            if binding is not None and binding.kind == "module":
                binding.used = True
                self._mark_import_used(obj.name)
                self._resolve_qualified(node, binding, scope)
                return
            if binding is not None and binding.kind == "store":
                binding.used = True
                return  # member validation is the discord checker's job
        self._walk_expr(obj, scope)

    def _mark_import_used(self, bound_name: str) -> None:
        if self._current_info is not None:
            for rec in self._current_info.imports:
                if rec.bound_name == bound_name:
                    rec.used = True

    def _resolve_qualified(self, node: A.Member, binding: _Binding,
                           scope: _Scope) -> None:
        imp = binding.node
        if not isinstance(imp, A.Import):
            return
        if imp.module is not None:
            return  # stdlib member validation is the checker's job
        if imp.resolved_path is None:
            return  # already errored (missing file / cycle)
        target = Path(imp.resolved_path)
        sub_info = self._resolved_files.get(target)
        if sub_info is None:
            return
        member = sub_info.scope.lookup_local(node.name)
        if member is None:
            exported = [(b.name, _KIND_WORDS.get(b.kind, "name"))
                        for b in sub_info.scope.bindings.values()
                        if b.kind != "module" and b.pub]
            self._similar_name_help(
                node.span, node.name,
                exported or [(b.name, _KIND_WORDS.get(b.kind, "name"))
                             for b in sub_info.scope.bindings.values()
                             if b.kind != "module"],
                scope=sub_info.scope)
            return
        if not member.pub:
            self._error(node.span, "E3005",
                        f"`{node.name}` is not exported by module "
                        f"`{binding.name}`",
                        help="add `pub` to the declaration, or use an "
                             "exported name")
            return
        member.used = True


_KIND_WORDS = {
    "var": "variable", "param": "parameter", "const": "constant",
    "option": "option", "fn": "function", "struct": "struct",
    "enum": "enum", "module": "module", "store": "store",
    "handler": "handler binding",
}


# ---------------------------------------------------------------- public API


def resolve_program(program: A.Program,
                    main_path: Path | str | None) -> CompilerResult:
    """Resolve an already-parsed Program (main file at ``main_path``)."""
    path = Path(main_path) if main_path is not None else None
    resolver = Resolver(path)
    return resolver.resolve_program(program, path)


def resolve_source(source: str, filename: str = "<string>") -> CompilerResult:
    """Parse + resolve source text (single-file programs)."""
    from .parser import parse
    program = parse(source, filename)
    path = None if filename == "<string>" else Path(filename)
    return resolve_program(program, path)


def resolve_file(path: Path | str) -> CompilerResult:
    """Parse + resolve the program in the file at ``path``."""
    from .parser import parse
    path = Path(path)
    content = path.read_text(encoding="utf-8")
    program = parse(content, str(path))
    return resolve_program(program, path)
