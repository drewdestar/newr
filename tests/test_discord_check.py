"""Tests for the Discord checker (SPEC.md SS13, SS14, SS15).

Written FIRST (TDD RED). The helper runs the full front end:
resolve -> core check (with discord providers) -> discord check.
"""

import unittest

from compiler.checker import check_program
from compiler.discord_check import check_discord, discord_providers
from compiler.errors import NewrError
from compiler.resolver import resolve_source

BOT = 'bot "TestBot":\n  description: "test bot"\n\n'


def full(src):
    """All diagnostics from resolve + core check + discord check."""
    resolved = resolve_source(src)
    diags = list(resolved.diagnostics)
    diags += check_program(resolved.program,
                           providers=discord_providers(resolved.program)
                           ).diagnostics
    diags += check_discord(resolved.program)
    return diags


def codes(diags):
    return [d.code for d in diags]


def errs(diags):
    return [d for d in diags if d.is_error()]


def cmd(body, name="ping", desc=None, options=""):
    d = f'  description: "{desc}"\n' if desc else ""
    return (BOT + f'command {name}:\n{d}{options}  do:\n' + body)


class TestEveryPathAnswers(unittest.TestCase):
    def test_plain_reply_answers(self):
        self.assertEqual(errs(full(cmd('    reply "hi"\n'))), [])

    def test_if_without_else_does_not_answer(self):
        d = full(cmd('    if true:\n      reply "hi"\n'))
        self.assertIn("E2010", codes(d))

    def test_if_else_both_reply_answers(self):
        d = full(cmd('    if true:\n      reply "hi"\n    else:\n      reply "bye"\n'))
        self.assertEqual(errs(d), [])

    def test_throw_counts_as_answered(self):
        d = full(cmd('    if true:\n      reply "hi"\n    else:\n      throw "no"\n'))
        self.assertEqual(errs(d), [])

    def test_for_loop_reply_does_not_count(self):
        d = full(cmd('    for x in [1]:\n      reply "hi"\n'))
        self.assertIn("E2010", codes(d))

    def test_try_answers_iff_try_and_catch_answer(self):
        # NOTE: `catch err:` is rejected by the resolver (E3007 -- `err`
        # is a builtin name) even though SPEC.md SS10's own example uses
        # it; that resolver gap is reported separately. Use `catch e:`.
        ok = cmd('    try:\n      reply "hi"\n    catch e:\n      reply "err"\n')
        self.assertEqual(errs(full(ok)), [])
        bad = cmd('    try:\n      reply "hi"\n    catch e:\n      log "oops"\n')
        self.assertIn("E2010", codes(full(bad)))

    def test_defer_answers(self):
        self.assertEqual(errs(full(cmd('    defer\n'))), [])

    def test_modal_answers(self):
        # BUG-M2-3: a modal sends a response directly (send_modal),
        # so it answers the interaction — unlike a button/select,
        # which only attach to a reply. A command whose only action
        # is showing a modal must not be E2010.
        body = ('    modal "Feedback" (timeout: 120s):\n'
                '      text_input "name" (style: short, required):\n'
                '      do:\n        reply "thanks {name}"\n')
        self.assertEqual(errs(full(cmd(body))), [])


class TestComponents(unittest.TestCase):
    def test_button_without_timeout_is_E2008(self):
        body = ('    button "Buy" (style: primary, id: "buy"):\n'
                '      do:\n        reply "ok"\n')
        d = full(cmd(body))
        self.assertIn("E2008", codes(d))

    def test_button_with_timeout_is_ok(self):
        # The command itself answers with the reply; the button is an
        # additional component (a button alone does not answer -- E2010).
        body = ('    reply "ok"\n'
                '    button "Buy" (style: primary, id: "buy", timeout: 60s):\n'
                '      do:\n        reply "ok"\n')
        self.assertEqual(errs(full(cmd(body))), [])

    def test_only_roles_empty_is_E2008(self):
        body = ('    button "Buy" (style: primary, id: "buy", timeout: 60s):\n'
                '      only: roles([])\n'
                '      do:\n        reply "ok"\n')
        d = full(cmd(body))
        self.assertIn("E2008", codes(d))

    def test_duplicate_custom_id_in_handler_is_E2008(self):
        body = ('    button "A" (style: primary, id: "same", timeout: 60s):\n'
                '      do:\n        reply "a"\n'
                '    button "B" (style: primary, id: "same", timeout: 60s):\n'
                '      do:\n        reply "b"\n')
        d = full(cmd(body))
        self.assertIn("E2008", codes(d))

    def test_modal_bad_style_is_E2008(self):
        body = ('    modal "Form" (id: "form", timeout: 60s):\n'
                '      text_input "Name" (style: huge):\n'
                '      do:\n        reply "ok"\n')
        d = full(cmd(body))
        self.assertIn("E2008", codes(d))

    def test_modal_min_greater_than_max_is_E2008(self):
        body = ('    modal "Form" (id: "form", timeout: 60s):\n'
                '      text_input "Name" (style: short, min_length: 10, max_length: 5):\n'
                '      do:\n        reply "ok"\n')
        d = full(cmd(body))
        self.assertIn("E2008", codes(d))


class TestEmbeds(unittest.TestCase):
    def test_embed_title_too_long_is_E2008(self):
        long_title = "x" * 257
        src = ('fn f() -> embed:\n  let e: embed = embed:\n'
               f'    title: "{long_title}"\n  return e\n')
        d = full(src)
        self.assertIn("E2008", codes(d))

    def test_embed_field_value_too_long_is_E2008(self):
        long_val = "x" * 1025
        src = ('fn f() -> embed:\n  let e: embed = embed:\n'
               f'    field "F": "{long_val}"\n  return e\n')
        d = full(src)
        self.assertIn("E2008", codes(d))

    def test_embed_too_many_fields_is_E2008(self):
        fields = "".join(f'    field "F{i}": "v"\n' for i in range(26))
        src = ('fn f() -> embed:\n  let e: embed = embed:\n' + fields +
               '  return e\n')
        d = full(src)
        self.assertIn("E2008", codes(d))


class TestStringsAndT(unittest.TestCase):
    def test_missing_key_in_locale_is_E2009(self):
        src = (BOT + 'strings en:\n  "a": "A {x}"\n  "b": "B"\n'
               'strings id:\n  "a": "A {x}"\n')
        self.assertIn("E2009", codes(full(src)))

    def test_same_key_set_is_ok(self):
        src = (BOT + 'strings en:\n  "a": "A {x}"\n'
               'strings id:\n  "a": "A {x}"\n')
        self.assertEqual(errs(full(src)), [])

    def test_t_unknown_arg_is_E2009(self):
        src = (BOT + 'strings en:\n  "a": "A {x}"\n'
               'command ping:\n  description: "p"\n  do:\n'
               '    reply t("a", y: 1)\n')
        self.assertIn("E2009", codes(full(src)))

    def test_t_missing_arg_is_E2009(self):
        src = (BOT + 'strings en:\n  "a": "A {x}"\n'
               'command ping:\n  description: "p"\n  do:\n'
               '    reply t("a")\n')
        self.assertIn("E2009", codes(full(src)))

    def test_t_extra_arg_is_W1007_warning(self):
        # More arguments than the template has placeholders (a
        # duplicated argument) is W1007, not an error. An argument
        # whose NAME is not a placeholder at all is E2009 instead.
        src = (BOT + 'strings en:\n  "a": "A {x}"\n'
               'command ping:\n  description: "p"\n  do:\n'
               '    reply t("a", x: 1, x: 2)\n')
        d = full(src)
        self.assertIn("W1007", codes(d))
        self.assertEqual(errs(d), [])

    def test_t_unknown_key_is_E2009(self):
        src = (BOT + 'strings en:\n  "a": "A {x}"\n'
               'command ping:\n  description: "p"\n  do:\n'
               '    reply t("nope", x: 1)\n')
        self.assertIn("E2009", codes(full(src)))

    def test_t_placeholder_satisfied_by_handler_binding(self):
        # {user} in a template is satisfied by the command's `user`
        # binding, not by a t() argument (SPEC.md SS15).
        src = (BOT + 'strings en:\n  "a": "Hi {user}"\n'
               'command ping:\n  description: "p"\n  do:\n'
               '    reply t("a")\n')
        self.assertEqual(errs(full(src)), [])


class TestIntents(unittest.TestCase):
    def test_unknown_intent_is_E2008(self):
        src = 'bot "B":\n  description: "x"\n  intents: [nope]\n'
        self.assertIn("E2008", codes(full(src)))

    def test_privileged_intent_warns_W1010(self):
        src = 'bot "B":\n  description: "x"\n  intents: [guilds, members]\n'
        d = full(src)
        self.assertIn("W1010", codes(d))
        self.assertEqual(errs(d), [])

    def test_missing_required_intent_is_E2008(self):
        src = ('bot "B":\n  description: "x"\n  intents: [guilds]\n\n'
               'event member_join:\n  reply: "hi {user}"\n')
        self.assertIn("E2008", codes(full(src)))

    def test_no_intents_declared_is_ok(self):
        src = BOT + 'event member_join:\n  reply: "hi {user}"\n'
        self.assertEqual(errs(full(src)), [])


class TestStore(unittest.TestCase):
    def test_duplicate_table_name_is_E2008(self):
        src = ('store economy:\n  table balances (user: str, balance: int)\n'
               '  table balances (user: str, balance: int)\n')
        self.assertIn("E2008", codes(full(src)))

    def test_duplicate_column_name_is_E2008(self):
        src = ('store economy:\n'
               '  table balances (user: str, user: int)\n')
        self.assertIn("E2008", codes(full(src)))

    def test_store_get_on_non_store_is_E2008(self):
        src = (BOT + 'command ping:\n  description: "p"\n  do:\n'
               '    let foo: str = "x"\n'
               '    let v = foo.get("balances", "k")\n'
               '    print v\n')
        self.assertIn("E2008", codes(full(src)))

    def test_economy_balance_without_balances_table_is_E2008(self):
        src = (BOT + 'store economy:\n  table items (id: str, price: int)\n'
               'command bal:\n  description: "b"\n  do:\n'
               '    reply "Balance: {economy.balance(user)} gold."\n')
        self.assertIn("E2008", codes(full(src)))


class TestHandlerRules(unittest.TestCase):
    def test_ready_with_reply_is_E2008(self):
        src = BOT + 'event ready:\n  reply: "hi"\n'
        self.assertIn("E2008", codes(full(src)))

    def test_member_join_with_log_is_E2008(self):
        src = BOT + 'event member_join:\n  log: "hi"\n'
        self.assertIn("E2008", codes(full(src)))

    def test_message_with_reply_sugar_is_E2008(self):
        src = BOT + 'event message:\n  reply: "hi"\n'
        self.assertIn("E2008", codes(full(src)))

    def test_duplicate_event_is_E2008(self):
        src = (BOT + 'event ready:\n  log: "a"\n\nevent ready:\n  log: "b"\n')
        self.assertIn("E2008", codes(full(src)))

    def test_reply_in_plain_fn_is_E2008(self):
        src = 'fn f() -> int:\n  reply "hi"\n  return 1\n'
        self.assertIn("E2008", codes(full(src)))

    def test_send_in_command_is_E2008(self):
        d = full(cmd('    send "hi" to channel\n'))
        self.assertIn("E2008", codes(d))

    def test_send_in_message_event_is_ok(self):
        src = (BOT + 'event message:\n  do:\n'
               '    send "hi" to message.channel\n')
        self.assertEqual(errs(full(src)), [])

    def test_reply_in_task_is_E2008(self):
        src = BOT + 'task every 1h:\n  do:\n    reply "hi"\n'
        self.assertIn("E2008", codes(full(src)))

    def test_handler_hang_while_true_warns_W1006(self):
        d = full(cmd('    while true:\n      reply "hi"\n'))
        # every-path-answers also fires (for never counts); the hang
        # warning is the one asserted here.
        self.assertIn("W1006", codes(d))

    def test_handler_hang_while_variable_warns_W1006(self):
        d = full(cmd('    let go: bool = true\n    while go:\n      reply "hi"\n'))
        self.assertIn("W1006", codes(d))

    def test_while_false_in_handler_does_not_warn_W1006(self):
        d = full(cmd('    while false:\n      reply "hi"\n'))
        self.assertNotIn("W1006", codes(d))


class TestReplyLength(unittest.TestCase):
    def test_reply_with_unknown_length_str_warns_W1009(self):
        d = full(cmd('    let msg: str = "hello"\n    reply msg\n'))
        self.assertIn("W1009", codes(d))
        self.assertEqual(errs(d), [])

    def test_reply_with_known_placeholders_has_no_W1009(self):
        d = full(cmd('    reply "Hi {user} in {guild}"\n'))
        self.assertNotIn("W1009", codes(d))
        self.assertEqual(errs(d), [])

    def test_reply_too_long_is_E2008(self):
        long_text = "x" * 2001
        src = (BOT + 'command ping:\n  description: "p"\n'
               f'  reply: "{long_text}"\n')
        self.assertIn("E2008", codes(full(src)))

    def test_reply_at_exactly_2000_is_ok(self):
        text = "x" * 2000
        src = (BOT + 'command ping:\n  description: "p"\n'
               f'  reply: "{text}"\n')
        self.assertEqual(errs(full(src)), [])


class TestCommandsAndBot(unittest.TestCase):
    def test_bot_name_too_long_is_E2008(self):
        src = 'bot "' + "B" * 33 + '":\n  description: "x"\n'
        self.assertIn("E2008", codes(full(src)))

    def test_bot_description_too_long_is_E2008(self):
        src = 'bot "B":\n  description: "' + "x" * 101 + '"\n'
        self.assertIn("E2008", codes(full(src)))

    def test_option_required_with_default_is_E2008(self):
        src = (BOT + 'command buy:\n  description: "b"\n'
               '  option amount: int (required, default: 1)\n'
               '  do:\n    reply "ok"\n')
        self.assertIn("E2008", codes(full(src)))

    def test_option_min_greater_than_max_is_E2008(self):
        src = (BOT + 'command buy:\n  description: "b"\n'
               '  option amount: int (min: 10, max: 1)\n'
               '  do:\n    reply "ok"\n')
        self.assertIn("E2008", codes(full(src)))

    def test_option_min_on_str_is_E2008(self):
        src = (BOT + 'command buy:\n  description: "b"\n'
               '  option item: str (min: 1)\n'
               '  do:\n    reply "ok"\n')
        self.assertIn("E2008", codes(full(src)))

    def test_too_many_options_is_E2008(self):
        opts = "".join(f'  option o{i}: str\n' for i in range(26))
        src = (BOT + 'command buy:\n  description: "b"\n' + opts +
               '  do:\n    reply "ok"\n')
        self.assertIn("E2008", codes(full(src)))


if __name__ == "__main__":
    unittest.main()
