"""Discord checker for the newr language (SPEC.md SS13, SS14, SS15).

Owns ALL Discord rules: bot/commands/events, reply lengths, the
"every path answers" check, intents, components, embeds, store /
economy, strings / ``t()``, and the handler-hang warning. This is the
ONLY compiler module (besides the CLI) that imports
``discord_limits`` -- the core checker never does (SPEC.md SS2.1).

Public API
----------
``check_discord(program) -> list[Diagnostic]``
    Discord diagnostics for a resolver-output Program (run AFTER the
    core checker, so ``expr_type`` is filled in on expressions -- the
    reply-length check uses it for scalar interpolation widths).

``DISCORD_TYPE_PROVIDERS``
    The base type-provider tables for entity member access, built from
    ``discord_limits.ENTITY_FIELDS``.

``discord_providers(program) -> dict``
    ``DISCORD_TYPE_PROVIDERS`` plus per-program store tables built
    from the program's ``store`` declarations (``store.get`` returns
    ``RowType?`` where ``RowType`` is the table's row type). Pass the
    result as ``providers`` to ``compiler.checker.check_program``.

CLI composition seam (see also ``checker.check_program``)::

    resolved = resolve_file(path)                 # or resolve_source
    providers = discord_providers(resolved.program)
    core = check_program(resolved.program, providers=providers)
    discord_diags = check_discord(resolved.program)
    diagnostics = resolved.diagnostics + core.diagnostics + discord_diags

Error-code map for the codes this module introduces
--------------------------------------------------
E2008  Discord rule violation (SPEC.md SS13/SS14): no bot block, bad
       command name, duplicate command / fn-name collision, empty or
       over-long description, reply over 2000 chars, bad/unknown/
       missing intent, component/ embed limit, store shape error.
E2009  i18n rule violation (SPEC.md SS15): locale key-set mismatch,
       unknown ``t()`` key, unknown / missing ``t()`` argument.
E2010  "Every path answers" (SPEC.md SS13.2/SS13.8): a command handler
       whose ``do:`` body may finish without answering.
W1006  Handler hang (SPEC.md SS13.8, G3): an unbounded loop in a
       handler body (warning, not a ban).
W1007  Extra ``t()`` argument beyond the template's placeholders.
W1009  Reply/embed length cannot be verified (an unknown-length ``str``
       is interpolated).
W1010  Privileged intent declared (SPEC.md SS13.6): needs approval
       above 100 guilds -- the 10,000-user rule.
"""

from __future__ import annotations

from . import ast_nodes as A
from .ast_nodes import BOOL, FLOAT, INT, STR, Type
from .discord_limits import (
    ACTION_ROWS_PER_MESSAGE_MAX,  # noqa: F401  (documented, unused in v1)
    BOT_NAME_MAX, BUTTON_STYLES, COMMAND_DESCRIPTION_MAX,
    COMMAND_NAME_MAX, COMMAND_NAME_RE, COMPONENTS_PER_MODAL_MAX,
    CUSTOM_ID_MAX, EMBED_DESCRIPTION_MAX, EMBED_FIELDS_MAX,
    EMBED_FIELD_NAME_MAX, EMBED_FIELD_VALUE_MAX, EMBED_TITLE_MAX,
    EMBED_TOTAL_MAX, EMBED_COLOR_MAX, EMBED_COLOR_MIN, ENTITY_FIELD_MAX,
    EVENT_KINDS,  # noqa: F401
    FLOAT_MAX_LEN, INT_MAX_LEN, BOOL_MAX_LEN,
    INTENT_REQUIRED, KNOWN_INTENTS, OPTION_CHOICES_MAX,
    OPTION_DESCRIPTION_MAX, OPTION_NAME_MAX, OPTIONS_PER_COMMAND_MAX,
    PLACEHOLDER_MAX, PRIVILEGED_INTENTS, REPLY_MAX, SELECT_OPTIONS_MAX,
    TEXT_INPUT_STYLES, ENTITY_FIELDS, fn_name,
)
from .errors import Diagnostic, Span
from .errors import MAX_NESTING_DEPTH

# ---------------------------------------------------------------- providers

_TYPE_BY_NAME = {
    "int": INT, "float": FLOAT, "str": STR, "bool": BOOL,
}


def _type_from_name(name: str) -> Type:
    if name in _TYPE_BY_NAME:
        return _TYPE_BY_NAME[name]
    return Type(name)


DISCORD_TYPE_PROVIDERS: dict = {
    "fields": {
        tname: {fname: _type_from_name(ftype)
                for fname, ftype in fields.items()}
        for tname, fields in ENTITY_FIELDS.items()
    },
    "methods": {},
    "stores": {},
}


def discord_providers(program: A.Program) -> dict:
    """Base entity providers + per-store tables for ``program``."""
    fields: dict[str, dict[str, Type]] = {
        t: dict(f) for t, f in DISCORD_TYPE_PROVIDERS["fields"].items()}
    stores: dict[str, dict] = {}
    programs = [program] + list(program.modules.values())
    for prog in programs:
        for store in prog.stores:
            tables: dict[str, dict] = {}
            for table in store.tables:
                cols = [(c.name, c.type) for c in table.columns]
                fields[table.name] = {c.name: c.type
                                      for c in table.columns}
                tables[table.name] = {
                    "columns": cols,
                    "row_type": Type(table.name),
                    "key": cols[0][0] if cols else None,
                }
            stores[store.name] = {"tables": tables,
                                  "ledger": store.ledger}
    return {"fields": fields, "methods": {}, "stores": stores}


# ---------------------------------------------------------------- checker

#: Handler binding names available in each handler context, for
#: ``t()`` placeholder satisfaction (SPEC.md SS15) and reply-length
#: expansion of bare placeholders.
_HANDLER_BINDINGS: dict[str, set[str]] = {
    "command": {"user", "guild", "channel", "interaction"},
    "member_join": {"user", "guild"},
    "member_remove": {"user", "guild"},
    "ready": {"bot"},
    "message": {"message", "user"},
    "component": {"user", "guild", "channel", "interaction"},
}


class DiscordChecker:
    def __init__(self, program: A.Program):
        self.program = program
        self.diagnostics: list[Diagnostic] = []
        self._strings: dict[str, dict[str, A.StringsEntry]] = {}
        for prog in [program] + list(program.modules.values()):
            for decl in prog.strings:
                self._strings.setdefault(decl.locale, {})
                for entry in decl.entries:
                    self._strings[decl.locale][entry.key] = entry
        self._store_names: set[str] = set()
        self._top_level_names: set[str] = set()
        for prog in [program] + list(program.modules.values()):
            for store in prog.stores:
                self._store_names.add(store.name)
            for fn in prog.functions:
                self._top_level_names.add(fn.name)
            for st in prog.structs:
                self._top_level_names.add(st.name)
            for en in prog.enums:
                self._top_level_names.add(en.name)
            for s in prog.stmts:
                if isinstance(s, (A.Let, A.Const)):
                    self._top_level_names.add(s.name)
        self._providers = discord_providers(program)
        self._economy_calls: list[A.Call] = []
        # Nesting depth of the _walk_stmt/_walk_expr recursion (M-01):
        # past MAX_NESTING_DEPTH the walk stops silently - the core
        # checker already reported E1001, so no second diagnostic.
        self._depth = 0

    # ------------------------------------------------------------ diagnostics

    def _error(self, span: Span, code: str, message: str,
               help: str | None = None, note: str | None = None) -> None:
        self.diagnostics.append(Diagnostic(
            span=span, code=code, message=message, severity="error",
            help=help, note=note))

    def _warn(self, span: Span, code: str, message: str,
              help: str | None = None) -> None:
        self.diagnostics.append(Diagnostic(
            span=span, code=code, message=message, severity="warning",
            help=help))

    # ------------------------------------------------------------ entry point

    def run(self) -> list[Diagnostic]:
        prog = self.program
        has_discord = bool(prog.bot is not None or prog.commands
                           or prog.events or prog.tasks or prog.strings
                           or prog.stores)
        if has_discord and prog.bot is None:
            span = (prog.commands[0].span if prog.commands
                    else prog.events[0].span if prog.events
                    else prog.tasks[0].span if prog.tasks
                    else prog.strings[0].span if prog.strings
                    else prog.stores[0].span)
            self._error(span, "E2008",
                        "commands, events, tasks, strings, and stores "
                        "need a `bot` block",
                        help="add a `bot \"Name\":` block to this file")
        if prog.bot is not None:
            self._check_bot(prog.bot)
        self._check_commands(prog.commands)
        self._check_events(prog.events)
        self._check_tasks(prog.tasks)
        self._check_stores(prog.stores)
        self._check_strings()
        # Walk every body for components / embeds / reply lengths /
        # handler-hang / statement-context rules / t() calls.
        for fn in prog.functions:
            self._walk_body(fn.body, context="fn", bindings=set())
        for st in prog.stmts:
            self._walk_stmt(st, context="top", bindings=set())
        for cmd in prog.commands:
            bindings = set(_HANDLER_BINDINGS["command"])
            bindings |= {o.name for o in cmd.options}
            self._check_duplicate_custom_ids(cmd.effective_body())
            self._walk_body(cmd.effective_body(), context="command",
                            bindings=bindings)
        for ev in prog.events:
            bindings = set(_HANDLER_BINDINGS.get(ev.kind, set()))
            self._check_duplicate_custom_ids(ev.effective_body())
            self._walk_body(ev.effective_body(), context="event:" + ev.kind,
                            bindings=bindings)
        for task in prog.tasks:
            self._walk_body(task.body, context="task", bindings=set())
        self._check_economy_shape()
        return self.diagnostics

    # ------------------------------------------------------------ bot

    def _check_bot(self, bot: A.BotDecl) -> None:
        if not (1 <= len(bot.name) <= BOT_NAME_MAX):
            self._error(bot.span, "E2008",
                        f"bot name must be 1-{BOT_NAME_MAX} characters, "
                        f"got {len(bot.name)}",
                        help="shorten the bot name")
        if bot.description is not None and len(bot.description) > 100:
            self._error(bot.span, "E2008",
                        "bot description must be at most 100 characters, "
                        f"got {len(bot.description)}",
                        help="shorten the description")
        if bot.intents is not None:
            for intent in bot.intents:
                if intent not in KNOWN_INTENTS:
                    self._error(bot.span, "E2008",
                                f"unknown intent `{intent}`",
                                help="known intents: "
                                     + ", ".join(sorted(KNOWN_INTENTS)))
                elif intent in PRIVILEGED_INTENTS:
                    self._warn(bot.span, "W1010",
                               f"privileged intent `{intent}` declared: "
                               "Discord requires verification/approval for "
                               "privileged intents in bots in 10,000+ "
                               "servers",
                               help="only declare it if the bot needs it")
            declared = set(bot.intents)
            for ev in self.program.events:
                for needed in INTENT_REQUIRED.get(ev.kind, set()):
                    if needed not in declared:
                        self._error(bot.span, "E2008",
                                    f"event `{ev.kind}` needs the "
                                    f"`{needed}` intent",
                                    help=f"add `{needed}` to `intents:`")

    # ------------------------------------------------------------ commands

    def _check_commands(self, commands: list[A.CommandDecl]) -> None:
        seen_names: dict[str, A.CommandDecl] = {}
        seen_fn_names: dict[str, str] = {}
        for cmd in commands:
            if cmd.name in seen_names:
                self._error(cmd.span, "E2008",
                            f"duplicate command `{cmd.name}`",
                            help="each command name may be declared once")
            else:
                seen_names[cmd.name] = cmd
            fn = fn_name(cmd.name)
            if fn in seen_fn_names and seen_fn_names[fn] != cmd.name:
                self._error(cmd.span, "E2008",
                            f"commands `{seen_fn_names[fn]}` and "
                            f"`{cmd.name}` both map to the function name "
                            f"`{fn}`",
                            help="rename one of the commands so their "
                                 "names differ after `-` becomes `_`")
            else:
                seen_fn_names[fn] = cmd.name
            if not COMMAND_NAME_RE.match(cmd.name) or \
                    len(cmd.name) > COMMAND_NAME_MAX:
                self._error(cmd.span, "E2008",
                            f"command name `{cmd.name}` is not a valid "
                            "Discord command name",
                            help="use 1-32 lowercase letters, digits, "
                                 "`-`, or `_`")
            elif cmd.name != cmd.name.lower():
                self._error(cmd.span, "E2008",
                            f"command name `{cmd.name}` must be lowercase",
                            help=f"rename it to `{cmd.name.lower()}`")
            desc = cmd.description if cmd.description is not None else cmd.name
            if desc == "":
                self._error(cmd.span, "E2008",
                            f"command `{cmd.name}` has an empty description",
                            help="write a description, or remove the "
                                 "`description:` line to use the name")
            elif len(desc) > COMMAND_DESCRIPTION_MAX:
                self._error(cmd.span, "E2008",
                            f"command `{cmd.name}` description must be at "
                            f"most {COMMAND_DESCRIPTION_MAX} characters, "
                            f"got {len(desc)}",
                            help="shorten the description")
            if len(cmd.options) > OPTIONS_PER_COMMAND_MAX:
                self._error(cmd.span, "E2008",
                            f"command `{cmd.name}` has "
                            f"{len(cmd.options)} options (at most "
                            f"{OPTIONS_PER_COMMAND_MAX})",
                            help="remove some options, or split the "
                                 "command")
            for opt in cmd.options:
                self._check_option(cmd, opt)
            # Every path answers (only for a do: body; a reply: sugar
            # command answers by construction).
            if cmd.body is not None and not self._answers(cmd.body):
                self._error(cmd.span, "E2010",
                            f"command `{cmd.name}` may not answer on some "
                            "path -- every branch must end in a reply",
                            help="end every branch of the handler with "
                                 "`reply`, `defer`, `edit`, or `followup` "
                                 "(or `throw`, which the error handler "
                                 "answers)")

    def _check_option(self, cmd: A.CommandDecl, opt: A.OptionDecl) -> None:
        if not (1 <= len(opt.name) <= OPTION_NAME_MAX):
            self._error(opt.name_span or opt.span, "E2008",
                        f"option name `{opt.name}` must be 1-"
                        f"{OPTION_NAME_MAX} characters")
        if opt.description is not None and \
                len(opt.description) > OPTION_DESCRIPTION_MAX:
            self._error(opt.span, "E2008",
                        f"description of option `{opt.name}` must be at "
                        f"most {OPTION_DESCRIPTION_MAX} characters")
        if opt.choices is not None and \
                len(opt.choices) > OPTION_CHOICES_MAX:
            self._error(opt.span, "E2008",
                        f"option `{opt.name}` has more than "
                        f"{OPTION_CHOICES_MAX} choices")
        if opt.required and opt.default is not None:
            self._error(opt.name_span or opt.span, "E2008",
                        f"option `{opt.name}` is `required` and has a "
                        "default -- a default makes it not required",
                        help="remove `required`, or remove the `default:`")
        if opt.type is not None and opt.type.name not in ("int", "float"):
            if opt.min_value is not None or opt.max_value is not None:
                self._error(opt.span, "E2008",
                            f"`min:` / `max:` only apply to numeric "
                            f"options, but `{opt.name}` is `{opt.type}`",
                            help="remove the `min:` / `max:`")
        if opt.min_value is not None and opt.max_value is not None:
            lo = self._literal_number(opt.min_value)
            hi = self._literal_number(opt.max_value)
            if lo is not None and hi is not None and lo > hi:
                self._error(opt.span, "E2008",
                            f"option `{opt.name}` has `min:` {lo} greater "
                            f"than `max:` {hi}",
                            help="make `min:` less than or equal to `max:`")

    @staticmethod
    def _literal_number(expr) -> float | int | None:
        if isinstance(expr, A.Lit) and isinstance(expr.value, (int, float)) \
                and not isinstance(expr.value, bool):
            return expr.value
        if isinstance(expr, A.UnaryOp) and expr.op == "-" and \
                isinstance(expr.operand, A.Lit) and \
                isinstance(expr.operand.value, (int, float)):
            return -expr.operand.value
        return None

    # ------------------------------------------------------------ events / tasks

    def _check_events(self, events: list[A.EventDecl]) -> None:
        seen: set[str] = set()
        for ev in events:
            if ev.kind in seen:
                self._error(ev.span, "E2008",
                            f"duplicate event `{ev.kind}`",
                            help="each event kind may be declared once "
                                 "per file")
            seen.add(ev.kind)
            if ev.kind == "ready" and ev.reply_expr is not None:
                self._error(ev.span, "E2008",
                            "`ready` cannot reply -- it has no interaction",
                            help="use `log:` in a `ready` event")
            if ev.kind in ("member_join", "member_remove") and \
                    ev.log_expr is not None:
                self._error(ev.span, "E2008",
                            f"`{ev.kind}` cannot use `log:`",
                            help="use `reply:` in a member event")
            if ev.kind == "message" and (ev.reply_expr is not None or
                                         ev.log_expr is not None):
                self._error(ev.span, "E2008",
                            "`message` events need a `do:` body -- they "
                            "answer with `send`, not with a reply",
                            help="replace the sugar with a `do:` body "
                                 "using `send ... to ...`")

    def _check_tasks(self, tasks: list[A.TaskDecl]) -> None:
        return None  # task bodies are checked by the body walk

    # ------------------------------------------------------------ stores

    def _check_stores(self, stores: list[A.StoreDecl]) -> None:
        for store in stores:
            seen_tables: set[str] = set()
            for table in store.tables:
                if table.name in seen_tables:
                    self._error(table.span, "E2008",
                                f"duplicate table `{table.name}` in store "
                                f"`{store.name}`",
                                help="each table name may be declared once "
                                     "per store")
                seen_tables.add(table.name)
                if not table.columns:
                    self._error(table.span, "E2008",
                                f"table `{table.name}` has no columns -- "
                                "the first column is the key column",
                                help="declare at least one column")
                seen_cols: set[str] = set()
                for col in table.columns:
                    if col.name in seen_cols:
                        self._error(col.span, "E2008",
                                    f"duplicate column `{col.name}` in "
                                    f"table `{table.name}`",
                                    help="each column name may be declared "
                                         "once per table")
                    seen_cols.add(col.name)

    def _check_economy_shape(self) -> None:
        if not self._economy_calls:
            return
        stores = self._providers.get("stores", {})
        info = stores.get("economy")
        tables = info.get("tables", {}) if info else {}
        balances = tables.get("balances")
        ok = balances is not None and balances["columns"] == [
            ("user", STR), ("balance", INT)]
        if not ok:
            for call in self._economy_calls:
                self._error(call.span, "E2008",
                            "`economy.balance` / `credit` / `debit` / "
                            "`transfer` need a `balances` table with "
                            "columns `(user: str, balance: int)`",
                            help="declare `table balances (user: str, "
                                 "balance: int)` in `store economy:`")

    # ------------------------------------------------------------ strings / t()

    def _check_strings(self) -> None:
        # Duplicate keys within one locale.
        for locale, entries in self._strings.items():
            seen: set[str] = set()
            for prog in [self.program] + list(self.program.modules.values()):
                for decl in prog.strings:
                    if decl.locale != locale:
                        continue
                    for entry in decl.entries:
                        if entry.key in seen:
                            self._error(entry.span, "E2009",
                                        f"duplicate key `{entry.key}` in "
                                        f"`strings {locale}:`",
                                        help="each key may be defined once "
                                             "per locale")
                        seen.add(entry.key)
        if len(self._strings) <= 1:
            return
        locales = sorted(self._strings)
        all_keys: set[str] = set()
        for entries in self._strings.values():
            all_keys |= set(entries)
        for locale in locales:
            missing = sorted(all_keys - set(self._strings[locale]))
            for key in missing:
                self._error(self.program.span, "E2009",
                            f"locale `{locale}` is missing the key "
                            f"`{key}` -- every locale must define the same "
                            "key set",
                            help=f"add `\"{key}\": \"...\"` to the "
                                 f"`strings {locale}:` block")

    def _check_tcall(self, expr: A.TCall, bindings: set[str]) -> None:
        if not self._strings:
            self._error(expr.key_span or expr.span, "E2009",
                        f"unknown strings key `{expr.key}` -- no `strings` "
                        "blocks are defined",
                        help="define the key in a `strings <locale>:` block")
            return
        first_locale = sorted(self._strings)[0]
        if expr.key not in self._strings[first_locale]:
            # Unknown in every locale, or missing in the first: the
            # key-set check already reports per-locale gaps; report an
            # unknown key only when no locale defines it.
            if not any(expr.key in e for e in self._strings.values()):
                self._error(expr.key_span or expr.span, "E2009",
                            f"unknown strings key `{expr.key}`",
                            help="define the key in every `strings` block, "
                                 "or fix the key spelling")
                return
        arg_names = {name for name, _v, _s in expr.args}
        for locale in sorted(self._strings):
            entries = self._strings[locale]
            if expr.key not in entries:
                continue
            template = entries[expr.key]
            for ph in template.placeholders:
                if ph not in arg_names and ph not in bindings:
                    self._error(expr.key_span or expr.span, "E2009",
                                f"`t(\"{expr.key}\")` is missing the "
                                f"argument `{ph}`",
                                help=f"pass `{ph}: ...` to `t(...)`, or "
                                     "use a handler binding of that name")
                    break
            for name, _v, span in expr.args:
                if name not in template.placeholders:
                    self._error(span, "E2009",
                                f"`t(\"{expr.key}\")` has no placeholder "
                                f"`{{{name}}}`",
                                help="remove the argument, or add "
                                     f"`{{{name}}}` to the template")
        # Extra arguments beyond the template: a warning, not an error.
        # This is the count case -- more arguments than the template
        # has placeholders (e.g. a duplicated argument). An argument
        # whose NAME is not a placeholder at all is the E2009 above.
        template = self._strings[first_locale].get(expr.key)
        if template is not None and len(expr.args) > len(
                template.placeholders):
            self._warn(expr.span, "W1007",
                       f"`t(\"{expr.key}\")` was given "
                       f"{len(expr.args)} argument(s) but the template "
                       f"has only {len(template.placeholders)} "
                       "placeholder(s)",
                       help="remove the extra argument(s)")

    def _check_duplicate_custom_ids(self, body: list) -> None:
        seen: dict[str, Span] = {}
        for st in self._iter_stmts(body):
            if isinstance(st, (A.ButtonDecl, A.ModalDecl)):
                cid = st.custom_id
                if cid is None:
                    continue
                if cid in seen:
                    self._error(st.span, "E2008",
                                f"duplicate custom id `{cid}` in this "
                                "handler",
                                help="give each component its own `id:`")
                else:
                    seen[cid] = st.span

    @staticmethod
    def _iter_stmts(body: list):
        for st in body:
            yield st
            if isinstance(st, A.If):
                yield from DiscordChecker._iter_stmts(st.then_body)
                for _c, b in st.elifs:
                    yield from DiscordChecker._iter_stmts(b)
                if st.else_body is not None:
                    yield from DiscordChecker._iter_stmts(st.else_body)
            elif isinstance(st, (A.For, A.While)):
                yield from DiscordChecker._iter_stmts(st.body)
            elif isinstance(st, A.Try):
                yield from DiscordChecker._iter_stmts(st.body)
                for _n, _s, b in st.catches:
                    yield from DiscordChecker._iter_stmts(b)
                if st.finally_body is not None:
                    yield from DiscordChecker._iter_stmts(st.finally_body)
            elif isinstance(st, A.Match):
                for arm in st.arms:
                    yield from DiscordChecker._iter_stmts(arm.body)

    # ------------------------------------------------------------ every path answers

    def _answers(self, body: list) -> bool:
        for st in body:
            if isinstance(st, (A.Reply, A.Defer, A.Edit, A.Followup,
                               A.Throw, A.ModalDecl)):
                # A modal answers: it sends a response directly
                # (send_modal). A button/select does not - it only
                # attaches to a reply. (BUG-M2-3.)
                return True
            if isinstance(st, A.If):
                if st.else_body is None:
                    continue
                if self._answers(st.then_body) and \
                        all(self._answers(b) for _c, b in st.elifs) and \
                        self._answers(st.else_body):
                    return True
                continue
            if isinstance(st, A.Try):
                if st.catches and self._answers(st.body) and \
                        all(self._answers(b)
                            for _n, _s, b in st.catches):
                    return True
                continue
            if isinstance(st, A.Match):
                if st.arms and all(self._answers(a.body)
                                   for a in st.arms) and \
                        any(isinstance(a.pattern, (A.WildcardPattern,
                                                  A.NamePattern))
                            for a in st.arms):
                    return True
                continue
            # for / while never count; a component declaration does not
            # answer; anything else (let, log, return, ...) does not.
        return False

    # ------------------------------------------------------------ body walk

    def _walk_body(self, body: list, context: str,
                   bindings: set[str]) -> None:
        for st in body:
            self._walk_stmt(st, context, bindings)

    def _walk_stmt(self, st, context: str, bindings: set[str]) -> None:
        # M-01: stop silently past the nesting limit - the core
        # checker already reported E1001 for this program.
        self._depth += 1
        if self._depth > MAX_NESTING_DEPTH:
            self._depth -= 1
            return
        try:
            self._walk_stmt_inner(st, context, bindings)
        finally:
            self._depth -= 1

    def _walk_stmt_inner(self, st, context: str,
                         bindings: set[str]) -> None:
        if isinstance(st, A.If):
            self._walk_body(st.then_body, context, bindings)
            for _c, b in st.elifs:
                self._walk_body(b, context, bindings)
            if st.else_body is not None:
                self._walk_body(st.else_body, context, bindings)
        elif isinstance(st, A.For):
            self._walk_body(st.body, context, bindings)
        elif isinstance(st, A.While):
            if context.startswith(("command", "event", "component")):
                if not (isinstance(st.cond, A.Lit)
                        and st.cond.value is False):
                    self._warn(st.span, "W1006",
                               "unbounded loop in a handler body -- the "
                               "interaction token expires after 15 minutes",
                               help="answer first (with `reply` or "
                                    "`defer`), or make the loop bounded")
            self._walk_body(st.body, context, bindings)
        elif isinstance(st, A.Try):
            self._walk_body(st.body, context, bindings)
            for _n, _s, b in st.catches:
                self._walk_body(b, context, bindings)
            if st.finally_body is not None:
                self._walk_body(st.finally_body, context, bindings)
        elif isinstance(st, A.Match):
            for arm in st.arms:
                self._walk_body(arm.body, context, bindings)
        elif isinstance(st, (A.Reply, A.Edit, A.Followup)):
            self._walk_expr(st.value, context, bindings)
            self._check_reply(st, context)
            self._check_reply_context(st, context)
        elif isinstance(st, A.Defer):
            self._check_reply_context(st, context)
        elif isinstance(st, A.Send):
            self._walk_expr(st.value, context, bindings)
            if st.channel is not None:
                self._walk_expr(st.channel, context, bindings)
            self._check_send_context(st, context)
        elif isinstance(st, (A.ButtonDecl, A.SelectDecl, A.ModalDecl)):
            self._check_component(st, context, bindings)
        elif isinstance(st, A.Let) or isinstance(st, A.Const):
            self._walk_expr(st.value, context, bindings)
        elif isinstance(st, A.Assign):
            self._walk_expr(st.value, context, bindings)
        elif isinstance(st, A.ExprStmt):
            self._walk_expr(st.value, context, bindings)
        elif isinstance(st, A.Return):
            if st.value is not None:
                self._walk_expr(st.value, context, bindings)
        elif isinstance(st, A.Throw):
            self._walk_expr(st.value, context, bindings)
        elif isinstance(st, A.Log):
            self._walk_expr(st.value, context, bindings)
        # Other statements (break/continue/pass/require/ensure) carry
        # no Discord-relevant content.

    def _check_reply_context(self, st, context: str) -> None:
        if context in ("command", "component") or \
                context.startswith("event:member_join") or \
                context.startswith("event:member_remove"):
            return
        if context.startswith("event:ready"):
            self._error(st.span, "E2008",
                        "`ready` cannot reply -- it has no interaction",
                        help="use `log:` in a `ready` event")
            return
        if context.startswith("event:message"):
            self._error(st.span, "E2008",
                        "a `message` event cannot use interaction replies "
                        "-- it must answer with `send`",
                        help="use `send ... to ...` instead")
            return
        self._error(st.span, "E2008",
                    "interaction replies (`reply` / `defer` / `edit` / "
                    "`followup`) are only allowed in command and "
                    "component handlers",
                    help="move the reply into a command handler")

    def _check_send_context(self, st, context: str) -> None:
        if context.startswith("event:message") or context == "task":
            return
        self._error(st.span, "E2008",
                    "`send` is only allowed in a `message` event or a "
                    "task body",
                    help="use `reply` in a command handler, or move the "
                         "`send` into a `message` event or a `task`")

    # ------------------------------------------------------------ reply length

    def _check_reply(self, st, context: str) -> None:
        if st.value is None:
            return
        length, unknown, _desc = self._max_len(st.value)
        if unknown:
            self._warn(st.span, "W1009",
                       "reply length cannot be verified: part of the "
                       "message is an unknown-length `str`",
                       help="reply with a literal string (or a short "
                            "known value) if the message must be provably "
                            "under 2000 characters")
        if length > REPLY_MAX:
            self._error(st.span, "E2008",
                        f"this reply may be {length} characters long -- "
                        f"Discord allows at most {REPLY_MAX}",
                        help="shorten the message")

    def _max_len(self, expr) -> tuple[int, bool, str]:
        """(max expanded length, unknown-length-present, description)."""
        if isinstance(expr, A.Lit):
            if isinstance(expr.value, str):
                return len(expr.value), False, ""
            if isinstance(expr.value, bool):
                return BOOL_MAX_LEN, False, ""
            if isinstance(expr.value, int):
                return INT_MAX_LEN, False, ""
            if isinstance(expr.value, float):
                return FLOAT_MAX_LEN, False, ""
            return 0, False, ""
        if isinstance(expr, A.Interp):
            total, unknown = 0, False
            for part in expr.parts:
                if part.text is not None:
                    total += len(part.text)
                elif part.expr is not None:
                    n, u, _d = self._max_len(part.expr)
                    total += n
                    unknown = unknown or u
            return total, unknown, ""
        if isinstance(expr, A.Name):
            if expr.name in PLACEHOLDER_MAX:
                return PLACEHOLDER_MAX[expr.name], False, ""
            t = expr.expr_type
            if t is not None:
                return self._type_max_len(t, expr.name)
            return 0, True, expr.name
        if isinstance(expr, A.Member):
            key = None
            if isinstance(expr.obj, A.Name):
                key = f"{expr.obj.name}.{expr.name}"
            if key in ENTITY_FIELD_MAX:
                return ENTITY_FIELD_MAX[key], False, ""
            t = expr.obj.expr_type if expr.obj is not None else None
            if t is not None:
                key2 = f"{t.name}.{expr.name}"
                if key2 in ENTITY_FIELD_MAX:
                    return ENTITY_FIELD_MAX[key2], False, ""
            t2 = expr.expr_type
            if t2 is not None:
                return self._type_max_len(t2, expr.name)
            return 0, True, expr.name
        if isinstance(expr, A.TCall):
            return self._tcall_max_len(expr)
        if isinstance(expr, A.BinOp) and expr.op == "+":
            ln, lu, _ = self._max_len(expr.left)
            rn, ru, _ = self._max_len(expr.right)
            return ln + rn, lu or ru, ""
        if isinstance(expr, A.Ternary):
            ln, lu, _ = self._max_len(expr.then_expr)
            rn, ru, _ = self._max_len(expr.else_expr)
            return max(ln, rn), lu or ru, ""
        t = expr.expr_type
        if t is not None:
            return self._type_max_len(t, "?")
        return 0, True, "?"

    def _type_max_len(self, t: Type, name: str) -> tuple[int, bool, str]:
        if t == INT:
            return INT_MAX_LEN, False, ""
        if t == FLOAT:
            return FLOAT_MAX_LEN, False, ""
        if t == BOOL:
            return BOOL_MAX_LEN, False, ""
        if t.name in ("user", "member", "channel", "bot"):
            return 22, False, ""
        if t.name in ("guild", "role"):
            return 100, False, ""
        if t == STR:
            return 0, True, name
        return 0, True, name

    def _tcall_max_len(self, expr: A.TCall) -> tuple[int, bool, str]:
        if not self._strings:
            return 0, True, expr.key
        arg_lens: dict[str, tuple[int, bool]] = {}
        for name, value, _s in expr.args:
            arg_lens[name] = self._max_len(value)[:2]
        best, unknown = 0, False
        for locale in sorted(self._strings):
            entry = self._strings[locale].get(expr.key)
            if entry is None:
                continue
            total, unk = 0, False
            text = entry.template
            # Rebuild the expanded length from the template text.
            import re
            pos = 0
            for m in re.finditer(r"\{([A-Za-z_][A-Za-z0-9_]*)\}", text):
                total += len(text[pos:m.start()])
                ph = m.group(1)
                if ph in arg_lens:
                    n, u = arg_lens[ph]
                    total += n
                    unk = unk or u
                elif ph in PLACEHOLDER_MAX:
                    total += PLACEHOLDER_MAX[ph]
                else:
                    # A handler binding ({user} etc.) expands to its
                    # placeholder width; anything else is unknown.
                    key = ph
                    total += PLACEHOLDER_MAX.get(key, 0)
                    if key not in PLACEHOLDER_MAX:
                        unk = True
                pos = m.end()
            total += len(text[pos:])
            best = max(best, total)
            unknown = unknown or unk
        return best, unknown, ""

    # ------------------------------------------------------------ components

    def _check_component(self, st, context: str, bindings: set[str]) -> None:
        if context not in ("command", "component") and \
                not context.startswith("event:"):
            self._error(st.span, "E2008",
                        "components (`button` / `select` / `modal`) are "
                        "only allowed in handler bodies",
                        help="move the component into a command handler")
        if st.timeout is None:
            self._error(st.span, "E2008",
                        f"a {type(st).__name__.replace('Decl', '').lower()} "
                        "needs a `timeout:` -- a collector without a "
                        "timeout never ends",
                        help="add `timeout: 60s` (or another duration) to "
                             "the component header")
        if getattr(st, "only_kind", "author") == "roles" and \
                not getattr(st, "only_roles", None):
            self._error(st.span, "E2008",
                        "`only: roles(...)` needs a non-empty list of roles",
                        help="list at least one role, or use `only: author`")
        custom_id = getattr(st, "custom_id", None)
        if custom_id is not None and len(custom_id) > CUSTOM_ID_MAX:
            self._error(st.span, "E2008",
                        f"custom id must be at most {CUSTOM_ID_MAX} "
                        f"characters, got {len(custom_id)}")
        if isinstance(st, A.ButtonDecl):
            if st.style is not None and st.style not in BUTTON_STYLES:
                self._error(st.span, "E2008",
                            f"unknown button style `{st.style}`",
                            help="styles are "
                                 + ", ".join(sorted(BUTTON_STYLES)))
            elif st.style == "link" and not st.url:
                self._error(st.span, "E2008",
                            "a `link` button needs a `url:`",
                            help="add `url: \"https://...\"`")
            elif st.style != "link" and st.style is not None \
                    and not st.custom_id:
                self._error(st.span, "E2008",
                            "a non-link button needs an `id:`",
                            help="add `id: \"...\"` to the button header")
        if isinstance(st, A.SelectDecl):
            if not st.options:
                self._error(st.span, "E2008",
                            "a `select` needs at least one option",
                            help="add `options: [\"a\", \"b\"]`")
            elif len(st.options) > SELECT_OPTIONS_MAX:
                self._error(st.span, "E2008",
                            f"a `select` has at most {SELECT_OPTIONS_MAX} "
                            "options")
            if st.min_values is not None and st.max_values is not None \
                    and st.min_values > st.max_values:
                self._error(st.span, "E2008",
                            "`min:` must not be greater than `max:` on a "
                            "`select`")
        if isinstance(st, A.ModalDecl):
            if len(st.fields) > COMPONENTS_PER_MODAL_MAX:
                self._error(st.span, "E2008",
                            f"a modal has at most {COMPONENTS_PER_MODAL_MAX} "
                            "fields")
            for f in st.fields:
                if f.style is not None and f.style not in TEXT_INPUT_STYLES:
                    self._error(f.span, "E2008",
                                f"unknown text input style `{f.style}`",
                                help="styles are `short` and `paragraph`")
                if f.min_length is not None and f.max_length is not None \
                        and f.min_length > f.max_length:
                    self._error(f.span, "E2008",
                                f"text input `{f.name}` has `min_length:` "
                                "greater than `max_length:`")
                if (f.min_length is not None and f.min_length < 0) or \
                        (f.max_length is not None and f.max_length < 0):
                    self._error(f.span, "E2008",
                                f"text input `{f.name}` has a negative "
                                "length limit")
        # Duplicate custom_id within this handler is checked by the
        # caller over the whole handler body; here we only validate
        # this component itself.
        comp_bindings = set(bindings)
        comp_bindings |= set(_HANDLER_BINDINGS["component"])
        if isinstance(st, A.SelectDecl):
            comp_bindings.add("values")
        if isinstance(st, A.ModalDecl):
            comp_bindings |= {f.name for f in st.fields}
        self._walk_body(st.body, "component", comp_bindings)
        if st.on_timeout is not None:
            self._walk_body(st.on_timeout, "component", comp_bindings)

    # ------------------------------------------------------------ embeds / exprs

    def _walk_expr(self, expr, context: str, bindings: set[str]) -> None:
        if expr is None:
            return
        # M-01: stop silently past the nesting limit - the core
        # checker already reported E1001 for this program.
        self._depth += 1
        if self._depth > MAX_NESTING_DEPTH:
            self._depth -= 1
            return
        try:
            self._walk_expr_inner(expr, context, bindings)
        finally:
            self._depth -= 1

    def _walk_expr_inner(self, expr, context: str,
                         bindings: set[str]) -> None:
        if isinstance(expr, A.TCall):
            self._check_tcall(expr, bindings)
            for _n, v, _s in expr.args:
                self._walk_expr(v, context, bindings)
            return
        if isinstance(expr, A.EmbedExpr):
            self._check_embed(expr)
            for f in expr.fields:
                if f.value is not None:
                    self._walk_expr(f.value, context, bindings)
            return
        if isinstance(expr, A.Call):
            self._walk_expr(expr.func, context, bindings)
            for arg in expr.args:
                self._walk_expr(arg, context, bindings)
            for _n, v, _s in expr.kwargs:
                self._walk_expr(v, context, bindings)
            self._note_store_call(expr)
            return
        if isinstance(expr, A.Member):
            self._walk_expr(expr.obj, context, bindings)
            return
        if isinstance(expr, A.BinOp):
            self._walk_expr(expr.left, context, bindings)
            self._walk_expr(expr.right, context, bindings)
            return
        if isinstance(expr, A.UnaryOp):
            self._walk_expr(expr.operand, context, bindings)
            return
        if isinstance(expr, A.Ternary):
            self._walk_expr(expr.cond, context, bindings)
            self._walk_expr(expr.then_expr, context, bindings)
            self._walk_expr(expr.else_expr, context, bindings)
            return
        if isinstance(expr, A.Interp):
            for part in expr.parts:
                if part.expr is not None:
                    self._walk_expr(part.expr, context, bindings)
            return
        if isinstance(expr, A.ListLit):
            for el in expr.elements:
                self._walk_expr(el, context, bindings)
            return
        if isinstance(expr, A.DictLit):
            for k, v in expr.pairs:
                self._walk_expr(k, context, bindings)
                self._walk_expr(v, context, bindings)
            return
        if isinstance(expr, A.Index):
            self._walk_expr(expr.obj, context, bindings)
            self._walk_expr(expr.index, context, bindings)
            return
        if isinstance(expr, A.TryExpr):
            self._walk_expr(expr.expr, context, bindings)
            return
        if isinstance(expr, A.Propagate):
            self._walk_expr(expr.expr, context, bindings)
            return
        if isinstance(expr, A.Lambda):
            if expr.body is not None:
                self._walk_body(expr.body, context, bindings)
            elif expr.expr_body is not None:
                self._walk_expr(expr.expr_body, context, bindings)
            return

    def _note_store_call(self, expr: A.Call) -> None:
        func = expr.func
        if not isinstance(func, A.Member) or \
                not isinstance(func.obj, A.Name):
            return
        if func.obj.name == "economy" and func.name in (
                "balance", "credit", "debit", "transfer"):
            self._economy_calls.append(expr)
        if func.name in ("get", "set", "delete", "balance", "credit",
                         "debit", "transfer") and \
                func.obj.name not in self._store_names and \
                func.obj.name != "store" and expr.expr_type is None:
            # The core checker could not type this call (it is not a
            # known method on the receiver), and the receiver is not a
            # declared store: a store operation on a non-store. (A
            # genuine method call such as `list.get(...)` types fine
            # in the core checker, so its expr_type is set and this
            # does not fire.)
            self._error(expr.span, "E2008",
                        f"`{func.obj.name}` is not a store -- store "
                        "operations (`get` / `set` / `delete` / "
                        "`balance` / `credit` / `debit` / `transfer`) "
                        "only work on a declared `store`",
                        help="declare a `store` block, or call the "
                             "operation on a store's name")

    def _check_embed(self, expr: A.EmbedExpr) -> None:
        kinds: dict[str, int] = {}
        total, unknown = 0, False
        for f in expr.fields:
            kinds[f.kind] = kinds.get(f.kind, 0) + 1
            if f.kind == "title" and f.value is not None:
                n, u, _d = self._max_len(f.value)
                total += n
                unknown = unknown or u
                if n > EMBED_TITLE_MAX:
                    self._error(f.span, "E2008",
                                f"embed title must be at most "
                                f"{EMBED_TITLE_MAX} characters, got {n}")
            elif f.kind == "description" and f.value is not None:
                n, u, _d = self._max_len(f.value)
                total += n
                unknown = unknown or u
                if n > EMBED_DESCRIPTION_MAX:
                    self._error(f.span, "E2008",
                                "embed description must be at most "
                                f"{EMBED_DESCRIPTION_MAX} characters, "
                                f"got {n}")
            elif f.kind == "color" and isinstance(f.value, A.Lit) and \
                    isinstance(f.value.value, int):
                if not (EMBED_COLOR_MIN <= f.value.value <= EMBED_COLOR_MAX):
                    self._error(f.span, "E2008",
                                "embed color must be between 0 and "
                                "0xFFFFFF")
            elif f.kind == "field":
                if f.name is not None:
                    total += len(f.name)
                    if len(f.name) > EMBED_FIELD_NAME_MAX:
                        self._error(f.span, "E2008",
                                    "embed field name must be at most "
                                    f"{EMBED_FIELD_NAME_MAX} characters")
                if f.value is not None:
                    n, u, _d = self._max_len(f.value)
                    total += n
                    unknown = unknown or u
                    if n > EMBED_FIELD_VALUE_MAX:
                        self._error(f.span, "E2008",
                                    "embed field value must be at most "
                                    f"{EMBED_FIELD_VALUE_MAX} characters")
        if kinds.get("field", 0) > EMBED_FIELDS_MAX:
            self._error(expr.span, "E2008",
                        f"an embed has at most {EMBED_FIELDS_MAX} fields, "
                        f"got {kinds['field']}")
        for kind in ("title", "description", "color"):
            if kinds.get(kind, 0) > 1:
                self._error(expr.span, "E2008",
                            f"an embed has at most one `{kind}:`")
        if total > EMBED_TOTAL_MAX:
            self._error(expr.span, "E2008",
                        f"an embed may total at most {EMBED_TOTAL_MAX} "
                        f"characters, got {total}")
        if unknown:
            self._warn(expr.span, "W1009",
                       "embed length cannot be verified: part of the "
                       "embed is an unknown-length `str`",
                       help="use literal strings in the embed if its size "
                            "must be provably under the Discord limits")


# ---------------------------------------------------------------- public API


def check_discord(program: A.Program) -> list[Diagnostic]:
    """Discord diagnostics for a resolver-output Program."""
    return DiscordChecker(program).run()
