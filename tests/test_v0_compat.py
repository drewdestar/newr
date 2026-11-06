"""v0 compatibility (SPEC.md SS13.10): the two v0 examples parse +
resolve + check with ZERO errors (warnings allowed), and invalid v0
files fail with the same verdicts as the v0 compiler. Invalid programs
are inline strings run through the full front end (resolve + core
check with discord providers + discord check); the parser-error case
is asserted via the raised NewrError.
"""

import unittest
from pathlib import Path

from compiler.checker import check_program
from compiler.discord_check import check_discord, discord_providers
from compiler.errors import NewrError
from compiler.resolver import resolve_file, resolve_source

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"


def full_source(src):
    resolved = resolve_source(src)
    diags = list(resolved.diagnostics)
    diags += check_program(resolved.program,
                           providers=discord_providers(resolved.program)
                           ).diagnostics
    diags += check_discord(resolved.program)
    return diags


def full_file(path):
    resolved = resolve_file(path)
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


BOT = 'bot "TestBot":\n  description: "test bot"\n\n'


class TestV0ExamplesClean(unittest.TestCase):
    def test_hello_bot_is_clean(self):
        self.assertEqual(errs(full_file(EXAMPLES / "hello_bot.newr")), [])

    def test_showcase_is_clean(self):
        self.assertEqual(errs(full_file(EXAMPLES / "showcase.newr")), [])

    def test_shop_is_clean(self):
        # The v1 shop example exercises options, store, t(), a button.
        # members is a privileged intent, so a W1010 warning is allowed.
        self.assertEqual(errs(full_file(EXAMPLES / "shop.newr")), [])


class TestInvalidV0Files(unittest.TestCase):
    def test_bad_command_name_uppercase(self):
        src = BOT + 'command Buy:\n  description: "d"\n  reply: "hi"\n'
        d = full_source(src)
        self.assertIn("E2008", codes(d))
        self.assertIn("Buy", errs(d)[0].message)

    def test_duplicate_command(self):
        src = (BOT + 'command ping:\n  description: "a"\n  reply: "hi"\n\n'
               'command ping:\n  description: "b"\n  reply: "hi"\n')
        d = full_source(src)
        self.assertIn("E2008", codes(d))
        self.assertIn("ping", errs(d)[0].message)

    def test_fn_name_collision_foo_bar_vs_foo_bar(self):
        src = (BOT + 'command foo-bar:\n  description: "a"\n  reply: "hi"\n\n'
               'command foo_bar:\n  description: "b"\n  reply: "hi"\n')
        d = full_source(src)
        self.assertIn("E2008", codes(d))

    def test_bad_placeholder_bot_in_command(self):
        src = BOT + 'command ping:\n  description: "d"\n  reply: "hi {bot}"\n'
        d = full_source(src)
        self.assertIn("E3001", codes(d))

    def test_bad_placeholder_unknown(self):
        src = BOT + 'command ping:\n  description: "d"\n  reply: "hi {unknown}"\n'
        d = full_source(src)
        self.assertIn("E3001", codes(d))

    def test_reply_too_long(self):
        src = (BOT + 'command ping:\n  description: "d"\n'
               '  reply: "' + "x" * 2001 + '"\n')
        d = full_source(src)
        self.assertIn("E2008", codes(d))

    def test_empty_description(self):
        src = BOT + 'command ping:\n  description: ""\n  reply: "hi"\n'
        d = full_source(src)
        self.assertIn("E2008", codes(d))

    def test_unknown_key_is_parser_E1001(self):
        with self.assertRaises(NewrError) as ctx:
            resolve_source(BOT + 'command ping:\n  nonsense: 1\n')
        self.assertEqual(ctx.exception.code, "E1001")

    def test_duplicate_event(self):
        src = (BOT + 'event ready:\n  log: "a"\n\n'
               'event ready:\n  log: "b"\n')
        d = full_source(src)
        self.assertIn("E2008", codes(d))

    def test_no_bot_block(self):
        src = 'command ping:\n  description: "d"\n  reply: "hi"\n'
        d = full_source(src)
        self.assertIn("E2008", codes(d))
        self.assertIn("bot", errs(d)[0].message.lower())


if __name__ == "__main__":
    unittest.main()
